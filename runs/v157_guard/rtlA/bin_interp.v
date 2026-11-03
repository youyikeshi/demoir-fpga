// ============================================================================
//  bin_interp.v —— bilinear 双线性插值逐元素单元（单主内存口，C8 交错布局）
//
//  语义：PyTorch F.interpolate(mode='bilinear', align_corners=False)
//        必须与 tools/gen_bin_golden.py::gen_interp() **逐字节一致**
//
//        fy = (oy + 0.5) * H/HO - 0.5 ;  y0 = floor(fy) ; ty = fy - y0
//        if (y0 < 0)     { y0 = 0; ty = 0; }          // 上采样首行
//        y1 = min(y0 + 1, H - 1)                       // 注意：y1 先算
//        if (y0 > H - 1) { y0 = H - 1; }               // 再钳高位（顺序照抄参考）
//        （x 方向同理）
//        v = (1-ty)(1-tx)*in[y0][x0] + (1-ty)tx*in[y0][x1]
//          + ty(1-tx)*in[y1][x0]   + ty*tx  *in[y1][x1]
//        out = sat8(int(round(v)))                     // Python round = 四舍六入五成双
//
//  ===========================================================================
//  1. 本版核心：W/64 形态（v2），彻底删掉 48-bit 长除法
//  ---------------------------------------------------------------------------
//  坐标仍然用**精确有理数**（不量化 ty/tx）：
//        ny = (2*oy+1)*H - HO,  den_y = 2*HO
//        fy = ny/den_y,  y0 = floor(ny/den_y),  tynum = ny - y0*den_y
//        ty = tynum/den_y        （精确，零舍入）
//        vy0 = den_y - tynum, vy1 = tynum        （vy0+vy1 = den_y）
//        vx0 = den_x - txnum, vx1 = txnum        （vx0+vx1 = den_x）
//
//  **关键数学**：网络里 26 个 interp 算子的 H/HO、W/WO 恒为 {1/4,1/2,2,4}
//  ⟹ 4·(H/HO) ∈ ℤ ⟹ 8·ny ≡ 0 (mod den_y) ⟹ tynum 恒是 den_y/8 的整数倍.
//  于是定义 0..8 的小整数权重
//        By = 8·vy1/den_y,  Ay = 8 - By = 8·vy0/den_y     （Ay+By = 8）
//        Bx = 8·vx1/den_x,  Ax = 8 - Bx = 8·vx0/den_x     （Ax+Bx = 8）
//  而原来的 N/D 里
//        k00 = vy0·vx0 = (Ay·Ax/64)·D,  D = den_y·den_x
//        N   = k00·a + k01·b + k10·c + k11·d = (D/64)·W
//        W   = Ay·Ax·a + Ay·Bx·b + By·Ax·c + By·Bx·d
//  ⇒     N/D ≡ W/64                    （D 被完全约掉，与 D 是否为 2 的幂无关）
//  ⇒     out = sat8(round_half_even(W/64)) —— 只有一次常数右移 6 位 + 半偶判据
//  因为 Ay+By = Ax+Bx = 8、|a..d| ≤ 128：
//        |Ax·a + Bx·b| ≤ 8·128 = 1024        （12 bit 有符号）
//        |W|           ≤ 8·1024 = 8192       （15 bit 有符号）
//  实测（4 个 manifest × 26 算子 × 6 组随机输入 = 88,134 像素）W/64 与 N/D 的
//  round_half_even **0 差异**，max|W| = 8192。
//
//  12/26 的下采样算子 Ay=Ax=4 ⇒ W = 16(a+b+c+d) ⇒ 输出 = round_half_even((a+b+c+d)/4)。
//
//  ---------------------------------------------------------------------------
//  2. fallback（必须保留）：wfast 门控
//  ---------------------------------------------------------------------------
//  W/64 成立的充要条件：8·tynum ≡ 0 且 8·txnum ≡ 0 (mod den)，即两个方向都
//  "步进与初值都是 den/8 的整数倍"。INIT 阶段顺带求
//        Qb0 = floor(8·tyni/den_y),  r0 = 8·tyni  mod den_y
//        Qbs = floor(8·nremy/den_y), rs = 8·nremy mod den_y
//  （x 方向同样），r0 == 0 && rs == 0 就是"该方向精确"。
//        wfast_y = (r0==0) && (rs==0)
//        wfast_x = (r0x==0) && (rsx==0)
//        wfast   = wfast_y && wfast_x
//  wfast=1 → ST_N 直接出字节，跳过 ST_DIV（每像素组 13 拍）。
//  wfast=0 → 走 **收窄的** 长除法 fallback（1 lane 串行，见 §9），只在
//            "非 1/8 尺度"的形状上跑，慢无所谓。
//  PS 侧也可以算同一个条件写一个 cfg 位与本单元取与（双保险），本版未接端口
//  （不许改端口列表），故只做 RTL 内部判定。
//
//  ---------------------------------------------------------------------------
//  3. 位宽（参数化，默认与 wrapper 的 HW_MAX 闸门一致 = 4096）
//  ---------------------------------------------------------------------------
//        DIMW = $clog2(HW_MAX)           12 @1024 / 12 @4096(仅注释用)
//        WDEN = $clog2(2*HW_MAX+1)       12 @1024 / 14 @4096   den,tynum,vy
//        WCOR = WDEN                     12 @1024 / 14 @4096   坐标 raw（有符号）
//        WK   = 2*WDEN                   24 @1024 / 28 @4096   D
//        WN   = WK + 8                   32 @1024 / 36 @4096   N / rem / dsh
//  对比旧版：坐标 40 bit、kij/D 34 bit、N/rem 48 bit。旧版的整组
//  kij / D / N / rem（8 lane × 48 bit）在本版 fast 路径上**不存在**。
//  契约：H,W,HO,WO ≤ HW_MAX。wrapper 已校验 HO/WO；H/W 由 host 保证。
//
//  ---------------------------------------------------------------------------
//  4. 定点/位宽格式小结（fast 路径）
//        Ay,By,Ax,Bx : 4 bit 无符号，0..8
//        W           : 15 bit 有符号，|W| ≤ 8192
//        mag = |W|   : 15 bit 无符号，q = mag[13:6]（0..128, 8 bit）
//        round: r = mag[5:0]；r > 32 → 进 1；r == 32 → q 为奇才进 1（半偶）
//        sat8 : >127 → 127；< -128 → -128（or/and-reduce 判据）
//
//  5. 内存映射（严格按 tb/tb_bin_interp.v 头部）
//        in   base_in  + addr8(c,y ,x )     int8  C8 交错
//        out  base_out + addr8(c,oy,ox)     int8  C8 交错
//        addr8(c,y,x) = (c/8)*H*W*8 + y*W*8 + x*8 + (c%8)
//    ⇒ 一次 64-bit 读 = 同一 (y,x) 上 8 个通道（lane i = 通道 cg*8+i）；
//      写口 8 bit ⇒ 一像素组（8 通道）正好 8 拍写完。
//
//  6. 一个输出像素要 4 个源像素 ⇒ 每组 4 次 64-bit 读
//        A=(y0,x0)  B=(y0,x1)  C=(y1,x0)  D=(y1,x1)
//    读延迟：mem_rd_en 在 t 拍拉高、mem_rd_data 在 t+1 拍有效（BRAM 语义）。
//
//  7. 坐标递推（不占拍、不用除法器）
//    y0 每输出行一次、x0 每输出像素一次，都是**线性递推**（Bresenham）：
//        tynum += Δrem ; y0 += Δmin ; if (tynum >= den) { tynum -= den; y0 += 1; }
//    Δmin = floor(2H/den)、Δrem = 2H - Δmin*den 是每次运行只算一次的常数。
//    本版把进位做成一个显式 wire（cy_s / cyx_s），**同一个进位**同时驱动
//        y0r（13/14 bit）与 tynr（den 余数）
//        by_r（4 bit 相位 = By）  ← 不额外做除法，只做 +step 与同条件减 8
//    即  By(k+1) = By(k) + by_step - 8·cy，  by_step = 8·Δrem/den ∈ [0,7]
//    这正是 8·tynum/den 的递推（两边同乘 8/den，进位谓词完全等价），
//    所以 fast 路径按位与"精确有理"结果一致，而不是近似。
//    初值 By(0) = 8·tyni/den，同样由 INIT 的加减循环得出（≤ 8 次迭代）。
//    ⚠ 递推的是**未 clamp 的 raw 值**；clamp（y0<0 → ty=0）每个像素重新做。
//
//  8. 拍数
//        fast（wfast=1）：ST_ROW 1 + 每像素组 (CALC 1 + RB/RC/RD 3 + N 1 + WR 8)
//                          = 13 拍 / 8 字节 ⇒ 1.625 拍/字节
//        旧版：21 拍 / 8 字节（多 8 拍 ST_DIV）
//        fallback（wfast=0）：每 lane 12 拍 × 8 lane + 8 拍写 ≈ 105 拍/像素组
//
//  9. 有符号铁律（本文件的自查清单）
//     · part-select 是**无符号**：mem_rd_data[i*8 +: 8] 必须经 $signed() 才进算术。
//     · 有/无符号混比在 Verilog 里会整体转无符号（负数比较全乱），所以坐标侧
//       一律 signed 且常量带位宽；乘加侧一律无符号系数（≥0）。
//     · round 必须写成 (r>32) 与 (r==32 && q[0]) 两支，**不能**用四舍五入。
//
// 10. 验证记录
//     · 本机只允许 xvlog：'xvlog --sv -L xpm bin_interp.v' → 0 error。
//     · W/64 与 N/D 的等价性由 Python Fraction 对拍（见任务书）：88,134 像素 0 差异。
// ============================================================================
`timescale 1ns/1ps

module bin_interp #(
    // 只影响内部位宽，不改端口列表（wrapper 不传参，用默认值即可）。
    // 契约：H,W,HO,WO ≤ HW_MAX；与 bin_unit_axi_wrap 的 HW_MAX 闸门保持一致。
    parameter int HW_MAX = 4096
) (
    input  logic        ap_clk,
    input  logic        ap_rst_n,
    input  logic        ap_ce,           // ★ P3: 同步时钟使能（包装层的 u_ce）
    input  logic        ctrl_start,
    output logic        ctrl_done,
    output logic        ctrl_busy,
    input  logic [15:0] cfg_c, cfg_h, cfg_w, cfg_ho, cfg_wo,
    input  logic [31:0] base_in, base_out,
    output logic        mem_rd_en,
    output logic [31:0] mem_rd_addr,
    input  logic [63:0] mem_rd_data,
    output logic        mem_wr_en,
    output logic [31:0] mem_wr_addr,
    output logic [7:0]  mem_wr_data
);

    // ========================================================================
    // 0. 状态编码
    // ========================================================================
    localparam logic [3:0] ST_IDLE = 4'd0,
                           ST_INIT = 4'd1,   // 8 个 setup 常数（加减循环，无除法器）
                           ST_ROW  = 4'd2,   // 每输出行：clamp y、行偏移、复位 x
                           ST_CALC = 4'd3,   // 每像素组：clamp x、4 地址、发读 A
                           ST_RB   = 4'd4,   // 发读 B，收 A
                           ST_RC   = 4'd5,   // 发读 C，收 B
                           ST_RD   = 4'd6,   // 发读 D，收 C
                           ST_N    = 4'd7,   // 收 D，8 通道并行算 W / 字节
                           ST_DIV  = 4'd8,   // fallback：8 lane × 12 拍串行长除
                           ST_WR   = 4'd9,   // 8 拍写（1 字节/拍）
                           ST_DONE = 4'd10;

    logic [3:0] state;

    // ========================================================================
    // 1. 位宽
    // ========================================================================
    localparam int WDEN = $clog2(2*HW_MAX + 1);   // den/tynum/vy  14 @4096
    localparam int WCOR = WDEN;                   // 坐标 raw（signed）14 @4096
    localparam int WK   = 2*WDEN;                 // D             28 @4096
    localparam int WN   = WK + 8;                 // N/rem/dsh     36 @4096
    localparam int WIA  = WDEN + 9;               // 内层 vx0*a+vx1*b（≤ 2^20）
    localparam int WQJ  = WDEN + 6;               // INIT job 被除数（≤ 8*den）

    // ========================================================================
    // 2. 配置锁存 / 常数
    // ========================================================================
    logic [15:0] C_r, H_r, W_r, HO_r, WO_r;
    logic [31:0] HW_r;                    // H*W
    logic [15:0] ncg_r;                   // C8 组数 = ceil(C/8)
    logic [31:0] base_in_r, base_out_r;

    logic [WDEN-1:0] dy_u, dx_u;          // den_y / den_x（无符号）
    logic [WK-1:0]   D_r;                 // D = den_y*den_x（只 fallback 用）
    logic signed [WCOR-1:0] hmax_s, wmax_s;   // H-1 / W-1

    // 16-bit cfg 的低位视图（契约内 H,W,HO,WO ≤ HW_MAX）
    wire [WDEN-1:0] h_w  = cfg_h[WDEN-1:0];
    wire [WDEN-1:0] w_w  = cfg_w[WDEN-1:0];
    wire [WDEN-1:0] ho_w = cfg_ho[WDEN-1:0];
    wire [WDEN-1:0] wo_w = cfg_wo[WDEN-1:0];

    // ========================================================================
    // 3. INIT：8 个 job 的加减循环，每个 job 求 q=floor(p/d)、r=p-q*d ∈ [0,d)
    //      job0: p=H-HO , d=den_y -> (y0i , tyni )   y 方向初值（y0i 可为 -1）
    //      job1: p=W-WO , d=den_x -> (x0i , txni )   x 方向初值
    //      job2: p=2H   , d=den_y -> (nminy, nremy)  每个 oy 的行步进
    //      job3: p=2W   , d=den_x -> (nminx, nremx)  每个 ox 的列步进
    //      job4: p=8tyni, d=den_y -> (by0  , r)      By 初值 + 精确性
    //      job5: p=8nremy,d=den_y -> (byst , r)      By 步进 + 精确性
    //      job6: p=8txni, d=den_x -> (bx0  , r)      Bx 初值 + 精确性
    //      job7: p=8nremx,d=den_x -> (bxst , r)      Bx 步进 + 精确性
    //    job4..7 的被除数 < 8d ⇒ 每个 job ≤ 8 次迭代。
    // ========================================================================
    localparam logic [2:0] JOB_Y0  = 3'd0, JOB_X0  = 3'd1,
                           JOB_NY  = 3'd2, JOB_NX  = 3'd3,
                           JOB_BY0 = 3'd4, JOB_BYS = 3'd5,
                           JOB_BX0 = 3'd6, JOB_BXS = 3'd7;

    logic [2:0] ijob;
    logic signed [WQJ-1:0]  pj_s, qj_s, dj_s;
    logic signed [WCOR-1:0] y0i, x0i;
    logic [WDEN-1:0]        tyni, txni;
    logic signed [WCOR-1:0] nminy, nminx;   // 行/列索引步进（整数，≥0）
    logic [WDEN-1:0]        nremy, nremx;   // 相位余数步进
    logic [3:0] by0, byst, bx0, bxst;       // By/Bx 的初值与小步进
    logic       wfast, wfast_y, wfast_x;    // W/64 成立的充要条件判定结果

    // ========================================================================
    // 4. 递推状态（raw = 未 clamp）
    // ========================================================================
    logic signed [WCOR-1:0] y0r, x0r;       // floor(ny/den)，可为 -1
    logic [WDEN-1:0]        tynr, txnr;     // ny mod den（恒在 [0,den)）
    logic [3:0]             by_r, bx_r;     // By = 8·tynum/den，Bx 同理

    // ========================================================================
    // 5. 遍历计数器 + 行/像素组寄存器
    // ========================================================================
    logic [15:0] cg_r, ox_r, oy_r;
    logic [31:0] cgbase_r;                  // cg*H*W（每切换 cg 加一次 HW_r）
    logic [31:0] rowoff0_r, rowoff1_r;      // cgbase + y*W
    logic [31:0] wrbase_r;                  // 输出字地址基址（每组 +1 字）
    logic [31:0] addrB_r, addrC_r, addrD_r;

    logic [63:0] wa, wb, wc, wd;            // 4 个源像素字（A/B/C/D）
    logic [7:0]  bo_r [0:7];                // 待写出的 8 个通道字节

    // ⚠ ST_CALC 拍就把"本像素组的权重"锁存下来：
    //    x 相位（txnr/bx_r）在 ST_CALC 末尾已经递进到下一个像素了，
    //    而 W/N 要到 ST_N/ST_DIV 才算，直接读 live 值会用到**下一个像素**的权重
    //    （下采样步进为 0 时看不出来，上采样必错）。
    logic [WDEN-1:0] typ_r, txp_r;          // 锁存的 tynum / txnum（fallback）
    logic [3:0]      byp_r, bxp_r;          // 锁存的 By / Bx（fast）

    // ========================================================================
    // 6. clamp 后的组合量（顺序严格照抄参考实现）
    // ========================================================================
    logic signed [WCOR-1:0] y0c_c, y1c_c;
    logic [WDEN-1:0]        tyc_c;
    logic signed [WCOR-1:0] x0c_c, x1c_c;
    logic [WDEN-1:0]        txc_c;
    logic [3:0]             by_c, bx_c;

    always_comb begin
        // y 方向：先处理 y0<0 ⇒ ty=0（同时 By=0, Ay=8），
        //         再算 y1=min(y0+1,H-1)，最后钳 y0 高位
        y0c_c = y0r;
        tyc_c = tynr;
        by_c  = by_r;
        if (y0c_c < 0) begin
            y0c_c = {WCOR{1'b0}};
            tyc_c = {WDEN{1'b0}};
            by_c  = 4'd0;
        end
        y1c_c = (y0c_c >= hmax_s) ? hmax_s : (y0c_c + 1);
        if (y0c_c > hmax_s) y0c_c = hmax_s;
        // x 方向：同顺序
        x0c_c = x0r;
        txc_c = txnr;
        bx_c  = bx_r;
        if (x0c_c < 0) begin
            x0c_c = {WCOR{1'b0}};
            txc_c = {WDEN{1'b0}};
            bx_c  = 4'd0;
        end
        x1c_c = (x0c_c >= wmax_s) ? wmax_s : (x0c_c + 1);
        if (x0c_c > wmax_s) x0c_c = wmax_s;
    end

    // 精确有理权重（fallback 用）——取 ST_ROW/ST_CALC 锁存值，不用 live 相位
    wire [WDEN-1:0] vy1_u = typ_r;
    wire [WDEN-1:0] vy0_u = dy_u - typ_r;
    wire [WDEN-1:0] vx1_u = txp_r;
    wire [WDEN-1:0] vx0_u = dx_u - txp_r;

    // fast 路径的 0..8 小权重（同样取锁存值）
    wire [3:0] ay_p = 4'd8 - byp_r;
    wire [3:0] ax_p = 4'd8 - bxp_r;

    // 行/列步进的进位（一个进位同时驱动 y0r 与 by_r，口径唯一）
    wire [WDEN:0] ty_sum = {1'b0, tynr} + {1'b0, nremy};
    wire [WDEN:0] tx_sum = {1'b0, txnr} + {1'b0, nremx};
    wire          cy_s   = (ty_sum >= {1'b0, dy_u});
    wire          cyx_s  = (tx_sum >= {1'b0, dx_u});
    wire [WDEN-1:0] ty_next = cy_s  ? (ty_sum[WDEN-1:0] - dy_u) : ty_sum[WDEN-1:0];
    wire [WDEN-1:0] tx_next = cyx_s ? (tx_sum[WDEN-1:0] - dx_u) : tx_sum[WDEN-1:0];
    wire signed [WCOR:0] y0_next = cy_s  ? (y0r + nminy + 1) : (y0r + nminy);
    wire signed [WCOR:0] x0_next = cyx_s ? (x0r + nminx + 1) : (x0r + nminx);
    wire [4:0] by_sum = {1'b0, by_r} + {1'b0, byst};
    wire [4:0] bx_sum = {1'b0, bx_r} + {1'b0, bxst};
    wire [3:0] by_next = cy_s  ? (by_sum[3:0] - 4'd8) : by_sum[3:0];
    wire [3:0] bx_next = cyx_s ? (bx_sum[3:0] - 4'd8) : bx_sum[3:0];

    // 行偏移：y0*W 只算一次，y1 用 +W 或 +0（y1==y0 只在 y0==H-1 出现）
    wire [31:0] y0w_c      = $unsigned(y0c_c) * {16'd0, W_r};
    wire [31:0] rowoff0_c  = cgbase_r + y0w_c;
    wire [31:0] rowoff1_c  = rowoff0_c + ((y1c_c == y0c_c) ? 32'd0 : {16'd0, W_r});
    wire [31:0] x0w_c      = {{(32-WCOR){1'b0}}, x0c_c};
    wire [31:0] x1w_c      = {{(32-WCOR){1'b0}}, x1c_c};

    // ========================================================================
    // 7. fast 路径：8 lane 并行算 W，一次 6 位右移 + 半偶取整 → 字节
    //    m4s8 : 4b×8b  ≤ 8×128 = 1024   （11 bit 有符号）
    //    m4s12: 4b×12b ≤ 8×1024 = 8192  （15 bit 有符号）
    //    全部 LUT 乘法，0 DSP（DSP48E1 是 25×18，4×8 用不上）
    // ========================================================================
    function automatic logic signed [10:0] m4s8(input logic [3:0] k,
                                                input logic signed [7:0] v);
        m4s8 = $signed({1'b0, k}) * v;
    endfunction

    function automatic logic signed [14:0] m4s12(input logic [3:0] k,
                                                 input logic signed [11:0] v);
        m4s12 = $signed({1'b0, k}) * v;
    endfunction

    // 取 64-bit 字里的第 i 个 int8（⚠ part-select 无符号，必须先 $signed）
    function automatic logic signed [7:0] wbyte(input logic [63:0] w, input int i);
        wbyte = $signed(w[8*i +: 8]);
    endfunction

    // sat8：or/and-reduce 判据（v 是 10 bit 有符号）
    //   判据 = "8 bit 结果与 v 的符号扩展是否一致"，因此高位段必须**含 bit7**：
    //   hi = v[8:7]（bit9 是符号位，bit8/bit7 是要丢掉的两位）
    //   v ∈ [0,127]     hi=00 -> 直通
    //   v ∈ [128,511]   hi=01/10/11 且 s=0 -> 7F
    //   v ∈ [-128,-1]   hi=1x 且 s=1     -> 直通（注意 -1..-128 的 v[8] 也是 1！）
    //   v ∈ [-512,-129] hi=0x 且 s=1     -> 80
    //   ⚠ 若把 hi 写成 v[8]（漏 bit7），则所有负数都会走 80 支路，
    //     -1..-127 全被压成 -128（2026-10-30 xsim 实测到的正是这个症状）。
    function automatic logic [7:0] sat8f(input logic signed [9:0] v);
        logic s, hi_or, hi_and;
        s      = v[9];
        hi_or  = |v[8:7];
        hi_and = &v[8:7];
        if (!s && hi_or)
            sat8f = 8'h7F;
        else if (s && !hi_and)      // 等价于 s && !(hi_and && v[7])：hi 已含 bit7
            sat8f = 8'h80;
        else
            sat8f = v[7:0];
    endfunction

    // fast 路径取整：sat8(round_half_even(W/64))
    function automatic logic [7:0] rndw(input logic signed [14:0] w);
        logic        sgn;
        logic [14:0] mag;
        logic [7:0]  qq;
        logic [5:0]  rr;
        logic        up;
        logic signed [9:0] vv;
        sgn = w[14];
        mag = sgn ? -w : w;              // |W| ≤ 8192，15 bit 够
        qq  = mag[13:6];                 // 0..128
        rr  = mag[5:0];
        up  = (rr > 6'd32) || ((rr == 6'd32) && qq[0]);   // 半偶：tie 看 q 的奇偶
        qq  = qq + {7'd0, up};
        vv  = sgn ? -$signed({1'b0, qq}) : $signed({1'b0, qq});
        rndw = sat8f(vv);
    endfunction

    logic signed [11:0] wtop_c [0:7];
    logic signed [11:0] wbot_c [0:7];
    logic signed [14:0] wval_c [0:7];
    logic [7:0]         bval_c [0:7];

    always_comb begin : W_PATH
        for (int i = 0; i < 8; i++) begin
            wtop_c[i] = m4s8(ax_p, wbyte(wa, i)) + m4s8(4'd8 - ax_p, wbyte(wb, i));
            wbot_c[i] = m4s8(ax_p, wbyte(wc, i)) + m4s8(4'd8 - ax_p, wbyte(mem_rd_data, i));
            wval_c[i] = m4s12(ay_p, wtop_c[i]) + m4s12(4'd8 - ay_p, wbot_c[i]);
            bval_c[i] = rndw(wval_c[i]);
        end
    end

    // ========================================================================
    // 8. fallback 数据通路（wfast=0 才跑；1 lane 串行，位宽收窄到 WN）
    //    每 lane 12 拍：0 取字节+内层积, 1 两乘, 2 取 |N|, 3..10 试商, 11 出字节
    // ========================================================================
    // 先声明本段的寄存器（下面多个 always_comb 会引用 dlane/wcnt）
    logic [WN-1:0] rem_r;                          // 试商余数
    logic [7:0]    q_r;                            // 商
    logic          neg_r;                          // 符号（存的是 |N|）
    logic [WN-1:0] dsh;                            // 试商：D<<k
    logic [3:0]    dstep;
    logic [2:0]    dlane, wcnt;

    logic signed [7:0] lba, lbb, lbc, lbd;
    always_comb begin
        lba = 8'sd0; lbb = 8'sd0; lbc = 8'sd0; lbd = 8'sd0;
        for (int i = 0; i < 8; i++) begin
            if (dlane == i[2:0]) begin
                lba = $signed(wa[8*i +: 8]);
                lbb = $signed(wb[8*i +: 8]);
                lbc = $signed(wc[8*i +: 8]);
                lbd = $signed(wd[8*i +: 8]);
            end
        end
    end

    logic signed [WIA-1:0] li0_c, li1_c;          // vx0*a+vx1*b / vx0*c+vx1*d
    always_comb begin
        li0_c = $signed({1'b0, vx0_u}) * lba + $signed({1'b0, vx1_u}) * lbb;
        li1_c = $signed({1'b0, vx0_u}) * lbc + $signed({1'b0, vx1_u}) * lbd;
    end

    logic signed [WIA-1:0] li0_r, li1_r;
    logic signed [WN-1:0]  pr0_r, pr1_r;
    logic signed [WN-1:0]  lsum_c;
    logic                  lneg_c;
    logic [WN-1:0]         lmag_c;
    always_comb lsum_c = pr0_r + pr1_r;
    always_comb lneg_c = lsum_c[WN-1];
    always_comb lmag_c = lneg_c ? -lsum_c : lsum_c;

    // fallback 取整：sat8(round_half_even(N/D))
    function automatic logic [7:0] rndq(input logic neg,
                                        input logic [7:0] q,
                                        input logic [WN-1:0] r);
        logic up;
        logic [8:0] qq;
        logic signed [9:0] vv;
        up = ({1'b0, r, 1'b0} >  {1'b0, D_r}) ||
             (({1'b0, r, 1'b0} == {1'b0, D_r}) && q[0]);
        qq = q + {8'd0, up};
        vv = neg ? -$signed({1'b0, qq}) : $signed({1'b0, qq});
        rndq = sat8f(vv);
    endfunction

    // ========================================================================
    // 9. 读口（组合）。mem_rd_en 本拍拉高 ⇒ mem_rd_data 下一拍有效。
    // ========================================================================
    logic [31:0] addrA_c;
    always_comb addrA_c = base_in_r + ((rowoff0_r + x0w_c) << 3);

    assign mem_rd_en   = (state == ST_CALC) || (state == ST_RB) ||
                         (state == ST_RC)   || (state == ST_RD);
    assign mem_rd_addr = (state == ST_CALC) ? addrA_c :
                         (state == ST_RB)   ? addrB_r :
                         (state == ST_RC)   ? addrC_r :
                         (state == ST_RD)   ? addrD_r : 32'd0;

    // ========================================================================
    // 10. 写口（ST_WR 8 拍）：lane = wcnt，字节在 ST_N / ST_DIV 就绪
    // ========================================================================
    always_comb mem_wr_data = bo_r[wcnt];

    logic wr_lane_ok;
    always_comb wr_lane_ok = (({16'd0, cg_r} << 3) + {29'd0, wcnt}) < {16'd0, C_r};

    assign mem_wr_en   = (state == ST_WR) & wr_lane_ok;
    assign mem_wr_addr = wrbase_r + {29'd0, wcnt};

    assign ctrl_busy = (state != ST_IDLE) && (state != ST_DONE);

    // ========================================================================
    // 11. 主 FSM
    // ========================================================================
    logic last_ox, last_oy, last_cg;
    always_comb begin
        last_ox = (ox_r == (WO_r - 16'd1));
        last_oy = (oy_r == (HO_r - 16'd1));
        last_cg = (cg_r == (ncg_r - 16'd1));
    end

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            state      <= ST_IDLE;
            ctrl_done  <= 1'b0;
            base_in_r  <= 32'd0;
            base_out_r <= 32'd0;
            C_r <= 16'd0; H_r <= 16'd0; W_r <= 16'd0; HO_r <= 16'd0; WO_r <= 16'd0;
            HW_r <= 32'd0; ncg_r <= 16'd0;
            dy_u <= {WDEN{1'b0}}; dx_u <= {WDEN{1'b0}};
            D_r  <= {WK{1'b0}};
            hmax_s <= {WCOR{1'b0}}; wmax_s <= {WCOR{1'b0}};
            ijob <= JOB_Y0;
            pj_s <= {WQJ{1'b0}}; qj_s <= {WQJ{1'b0}}; dj_s <= {WQJ{1'b0}};
            y0i  <= {WCOR{1'b0}}; x0i <= {WCOR{1'b0}};
            tyni <= {WDEN{1'b0}}; txni <= {WDEN{1'b0}};
            nminy <= {WCOR{1'b0}}; nminx <= {WCOR{1'b0}};
            nremy <= {WDEN{1'b0}}; nremx <= {WDEN{1'b0}};
            by0 <= 4'd0; byst <= 4'd0; bx0 <= 4'd0; bxst <= 4'd0;
            wfast <= 1'b0; wfast_y <= 1'b0; wfast_x <= 1'b0;
            y0r <= {WCOR{1'b0}}; x0r <= {WCOR{1'b0}};
            tynr <= {WDEN{1'b0}}; txnr <= {WDEN{1'b0}};
            by_r <= 4'd0; bx_r <= 4'd0;
            cg_r <= 16'd0; ox_r <= 16'd0; oy_r <= 16'd0;
            cgbase_r <= 32'd0; rowoff0_r <= 32'd0; rowoff1_r <= 32'd0;
            wrbase_r <= 32'd0;
            addrB_r <= 32'd0; addrC_r <= 32'd0; addrD_r <= 32'd0;
            wa <= 64'd0; wb <= 64'd0; wc <= 64'd0; wd <= 64'd0;
            typ_r <= {WDEN{1'b0}}; txp_r <= {WDEN{1'b0}};
            byp_r <= 4'd0; bxp_r <= 4'd0;
            rem_r <= {WN{1'b0}}; q_r <= 8'd0; neg_r <= 1'b0;
            dsh <= {WN{1'b0}}; dstep <= 4'd0; dlane <= 3'd0; wcnt <= 3'd0;
            li0_r <= {WIA{1'b0}}; li1_r <= {WIA{1'b0}};
            pr0_r <= {WN{1'b0}}; pr1_r <= {WN{1'b0}};
            for (int i = 0; i < 8; i++) bo_r[i] <= 8'd0;
        end else if (ap_ce && ctrl_start &&
                     ((state == ST_IDLE) || (state == ST_DONE))) begin
            // ---- 启动：锁配置、算运行期常数、进 INIT 的 job0 ----
            C_r  <= cfg_c;  H_r <= cfg_h;  W_r <= cfg_w;
            HO_r <= cfg_ho; WO_r <= cfg_wo;
            base_in_r  <= base_in;
            base_out_r <= base_out;
            HW_r   <= {16'd0, cfg_h} * {16'd0, cfg_w};
            ncg_r  <= ({16'd0, cfg_c} + 16'd7) >> 3;
            dy_u <= {ho_w[WDEN-2:0], 1'b0};               // 2*HO
            dx_u <= {wo_w[WDEN-2:0], 1'b0};               // 2*WO
            D_r  <= {ho_w[WDEN-2:0], 1'b0} * {wo_w[WDEN-2:0], 1'b0};   // 2HO*2WO
            hmax_s <= $signed(h_w) - 1;
            wmax_s <= $signed(w_w) - 1;
            ijob <= JOB_Y0;
            qj_s <= {WQJ{1'b0}};
            pj_s <= $signed(h_w) - $signed(ho_w);
            dj_s <= {{(WQJ-WDEN-1){1'b0}}, ho_w, 1'b0};   // 2*HO
            wfast <= 1'b0; wfast_y <= 1'b0; wfast_x <= 1'b0;
            cg_r <= 16'd0; ox_r <= 16'd0; oy_r <= 16'd0;
            dstep <= 4'd0; dlane <= 3'd0; wcnt <= 3'd0;
            ctrl_done <= 1'b0;
            state <= ST_INIT;
        end else if (ap_ce) begin                   // ★ P3: 冻结在 CE 门内
            // ---- 与 FSM 并行的通路动作 ----
            if (state == ST_RB) wa <= mem_rd_data;        // 收 A
            if (state == ST_RC) wb <= mem_rd_data;        // 收 B
            if (state == ST_RD) wc <= mem_rd_data;        // 收 C

            if (state == ST_N) begin
                wd <= mem_rd_data;                        // 收 D（fallback 后面要用）
                for (int i = 0; i < 8; i++) bo_r[i] <= bval_c[i];  // fast：一拍出 8 字节
                dlane <= 3'd0;
                dstep <= 4'd0;
            end

            // ---- fallback：1 lane 串行长除（12 拍/lane）----
            if (state == ST_DIV) begin
                case (dstep)
                    4'd0: begin                            // 取字节 + 内层积
                        li0_r <= li0_c;
                        li1_r <= li1_c;
                        dstep <= 4'd1;
                    end
                    4'd1: begin                            // 两级乘的第一级
                        pr0_r <= $signed({1'b0, vy0_u}) * li0_r;
                        pr1_r <= $signed({1'b0, vy1_u}) * li1_r;
                        dstep <= 4'd2;
                    end
                    4'd2: begin                            // 收 N：存 |N|、符号、重装试商
                        rem_r <= lmag_c;
                        neg_r <= lneg_c;
                        q_r   <= 8'd0;
                        dsh   <= {D_r, 7'b0};
                        dstep <= 4'd3;
                    end
                    4'd3, 4'd4, 4'd5, 4'd6, 4'd7, 4'd8, 4'd9, 4'd10: begin
                        if (rem_r >= dsh) begin
                            rem_r <= rem_r - dsh;
                            q_r   <= {q_r[6:0], 1'b1};
                        end else begin
                            q_r   <= {q_r[6:0], 1'b0};
                        end
                        dsh   <= dsh >> 1;
                        dstep <= dstep + 4'd1;
                    end
                    default: begin                         // 4'd11：出该 lane 的字节
                        // ⚠ 这里**不发** state（state 只在下面的 case 里发，
                        //   否则会被 case 的 default 覆盖成 ST_IDLE）
                        bo_r[dlane] <= rndq(neg_r, q_r, rem_r);
                        if (dlane != 3'd7) begin
                            dlane <= dlane + 3'd1;
                            dstep <= 4'd0;
                        end
                    end
                endcase
            end

            // ---- 控制 ----
            ctrl_done <= (state == ST_DONE) ||
                         ((state == ST_WR) && (wcnt == 3'd7) &&
                          last_ox && last_oy && last_cg);

            case (state)
                ST_IDLE: state <= ST_IDLE;

                // ---- INIT：加减到 [0,d) 为止；一个 job 完成即写回并切下一个 ----
                ST_INIT: begin
                    if (pj_s < 0) begin
                        pj_s <= pj_s + dj_s;
                        qj_s <= qj_s - 1;
                    end else if (pj_s >= dj_s) begin
                        pj_s <= pj_s - dj_s;
                        qj_s <= qj_s + 1;
                    end else begin
                        case (ijob)
                            JOB_Y0: begin
                                y0i  <= qj_s[WCOR-1:0];
                                tyni <= pj_s[WDEN-1:0];
                            end
                            JOB_X0: begin
                                x0i  <= qj_s[WCOR-1:0];
                                txni <= pj_s[WDEN-1:0];
                            end
                            JOB_NY: begin
                                nminy <= qj_s[WCOR-1:0];
                                nremy <= pj_s[WDEN-1:0];
                            end
                            JOB_NX: begin
                                nminx <= qj_s[WCOR-1:0];
                                nremx <= pj_s[WDEN-1:0];
                            end
                            JOB_BY0: begin
                                by0     <= qj_s[3:0];
                                wfast_y <= (pj_s == 0);
                            end
                            JOB_BYS: begin
                                byst    <= qj_s[3:0];
                                wfast_y <= wfast_y && (pj_s == 0);
                            end
                            JOB_BX0: begin
                                bx0     <= qj_s[3:0];
                                wfast_x <= (pj_s == 0);
                            end
                            default: begin                     // JOB_BXS
                                bxst    <= qj_s[3:0];
                                wfast_x <= wfast_x && (pj_s == 0);
                                wfast   <= wfast_y && wfast_x && (pj_s == 0);
                            end
                        endcase
                        if (ijob == JOB_BXS) begin
                            // 常数齐了 ⇒ 从 oy=0 的 raw 值开始
                            y0r  <= y0i;
                            tynr <= tyni;
                            by_r <= by0;
                            x0r  <= x0i;
                            txnr <= txni;
                            bx_r <= bx0;
                            cg_r <= 16'd0;
                            ox_r <= 16'd0;
                            oy_r <= 16'd0;
                            cgbase_r <= 32'd0;
                            wrbase_r <= base_out_r;
                            state <= ST_ROW;
                        end else begin
                            ijob <= ijob + 3'd1;
                            qj_s <= {WQJ{1'b0}};
                            case (ijob + 3'd1)
                                JOB_X0: begin
                                    pj_s <= $signed(w_w) - $signed(wo_w);
                                    dj_s <= {{(WQJ-WDEN){1'b0}}, dx_u};
                                end
                                JOB_NY: begin
                                    pj_s <= {1'b0, h_w, 1'b0};
                                    dj_s <= {{(WQJ-WDEN){1'b0}}, dy_u};
                                end
                                JOB_NX: begin
                                    pj_s <= {1'b0, w_w, 1'b0};
                                    dj_s <= {{(WQJ-WDEN){1'b0}}, dx_u};
                                end
                                JOB_BY0: begin
                                    pj_s <= $signed(tyni) <<< 3;
                                    dj_s <= {{(WQJ-WDEN){1'b0}}, dy_u};
                                end
                                JOB_BYS: begin
                                    pj_s <= $signed(nremy) <<< 3;
                                    dj_s <= {{(WQJ-WDEN){1'b0}}, dy_u};
                                end
                                JOB_BX0: begin
                                    pj_s <= $signed(txni) <<< 3;
                                    dj_s <= {{(WQJ-WDEN){1'b0}}, dx_u};
                                end
                                default: begin                     // JOB_BXS
                                    pj_s <= $signed(nremx) <<< 3;
                                    dj_s <= {{(WQJ-WDEN){1'b0}}, dx_u};
                                end
                            endcase
                        end
                    end
                end

                // ---- 每个输出行一次 ----
                ST_ROW: begin
                    // ⚠ y 方向的权重必须在**这里**锁存：下一拍 y0r/tynr 就递进了，
                    //   而且 y0<0 的 clamp（ty=0）只在 ST_ROW 的组合值里成立。
                    typ_r <= tyc_c;
                    byp_r <= by_c;
                    rowoff0_r <= rowoff0_c;                    // cg*HW + y0*W
                    rowoff1_r <= rowoff1_c;                    // + (y1==y0 ? 0 : W)
                    tynr <= ty_next;
                    y0r  <= y0_next[WCOR-1:0];
                    by_r <= by_next;
                    // 每行：ox 归零、raw x 复位到初值
                    ox_r <= 16'd0;
                    x0r  <= x0i;
                    txnr <= txni;
                    bx_r <= bx0;
                    state <= ST_CALC;
                end

                // ---- 每个像素组一次 ----
                ST_CALC: begin
                    // x 方向的权重在这里锁存（x 相位本拍末尾就要递进）
                    txp_r <= txc_c;
                    bxp_r <= bx_c;
                    // A 用组合地址（见 §9），这里锁存 B/C/D
                    addrB_r <= base_in_r + ((rowoff0_r + x1w_c) << 3);
                    addrC_r <= base_in_r + ((rowoff1_r + x0w_c) << 3);
                    addrD_r <= base_in_r + ((rowoff1_r + x1w_c) << 3);
                    // raw x 步进
                    txnr <= tx_next;
                    x0r  <= x0_next[WCOR-1:0];
                    bx_r <= bx_next;
                    state <= ST_RB;
                end

                ST_RB:  state <= ST_RC;
                ST_RC:  state <= ST_RD;
                ST_RD:  state <= ST_N;
                ST_N:   state <= wfast ? ST_WR : ST_DIV;   // fast 路径跳过 ST_DIV
                // fallback 只有跑完 8 lane × 12 拍才出去
                ST_DIV: state <= ((dstep == 4'd11) && (dlane == 3'd7)) ? ST_WR : ST_DIV;

                ST_WR: begin
                    if (wcnt == 3'd7) begin
                        wcnt <= 3'd0;
                        wrbase_r <= wrbase_r + 32'd8;      // 输出字地址线性递增
                        if (!last_ox) begin
                            ox_r  <= ox_r + 16'd1;
                            state <= ST_CALC;
                        end else if (!last_oy) begin
                            oy_r  <= oy_r + 16'd1;
                            state <= ST_ROW;
                        end else if (!last_cg) begin
                            cg_r  <= cg_r + 16'd1;
                            oy_r  <= 16'd0;
                            cgbase_r <= cgbase_r + HW_r;
                            // 新的一组：raw y 回到初值（raw x 由 ST_ROW 复位）
                            y0r  <= y0i;
                            tynr <= tyni;
                            by_r <= by0;
                            state <= ST_ROW;
                        end else begin
                            state <= ST_DONE;
                        end
                    end else begin
                        wcnt <= wcnt + 3'd1;
                    end
                end

                ST_DONE: state <= ST_DONE;
                default: state <= ST_IDLE;
            endcase
        end
    end

endmodule
