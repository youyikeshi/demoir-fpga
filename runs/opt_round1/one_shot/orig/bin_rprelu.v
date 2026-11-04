// ============================================================================
//  bin_rprelu.v —— RPReLU 逐元素单元（单主内存口，C8 交错布局）
//
//  1. 语义（docs/binary/03 §1；必须与 tools/gen_bin_golden.py::gen_rprelu 逐位一致）
//
//        dd = x - zeta[c]                                     x:int8, zeta:int32
//        r  = dd + gamma[c]                          if dd > 0
//        r  = ((dd * slope[c]) >>> slope_shift) + gamma[c]    otherwise (dd <= 0)
//        y  = sat8(r >>> shift)                               >>> 是算术右移
//
//     · dd == 0 走**负分支**：判据严格是 'dd > 0'，不是 '>= 0'。
//       （dd==0 时负分支 = (0*slope)>>8 + gamma = gamma，与正分支数值相同；
//        这里仍按契约写严格 '> 0'，避免把判据写反后只在边界值上“碰巧全对”。）
//     · 中间量按**真实范围**给位宽（不再用 int64：XC7Z015 的 DSP48E1 只有 25×18，
//       操作数一宽就退化成 LUT 乘法器 / LUT 移位器，这正是本轮要打掉的 LUT 黑洞）：
//           dd   = x - zeta     : 18 bit   （前提 P1：|zeta| <= 130943）
//           prod = dd * slope   : 34 bit = 18+16，对**任意 int16 slope** 都精确
//           r    = (prod>>>8)+gamma : 34 bit（gamma 是 int32，见前提 P1'）
//           v    = r >>> shift  : 34 bit   （前提 P2：cfg_shift <= 15）
//       在上述前提下与 Python 任意精度逐位一致（证明见 §6 前提表）。
//     · sat8: >127→127，<-128→-128，否则取低 8 位（用 or/and-reduce 实现，见 §6.1）。
//
//  2. 内存映射（严格按 tb/tb_bin_rprelu.v 头部；地址相对 base_*）
//
//        in     base_in    + addr8(c,y,x)      int8   C8 交错
//        zeta   base_zeta  + c*4               int32
//        gamma  base_gamma + c*4               int32
//        slope  base_slope + c*2               int16
//        out    base_out   + addr8(c,y,x)      int8   C8 交错
//        addr8(c,y,x) = (c/8)*H*W*8 + y*W*8 + x*8 + (c%8)
//
//  3. 关键观察：C8 下按 64-bit 字遍历，地址是**线性**的
//
//        令组 cg=c/8、像素 p=y*W+x ∈ [0,H*W)，则
//            addr8(c,y,x) = (cg*H*W + p)*8 + (c%8)          // “字号 = cg*H*W + p”
//        所以一次 64-bit 读正好拿到**同一像素上 8 个通道**的 int8
//        （字节 i = 通道 cg*8+i），而写口是 8-bit，一像素组正好 8 拍写完。
//        ⇒ FSM 只维护 (cg, pix) 两个线性计数器，**把 y/x 彻底消掉**：
//             读地址 = base_in  + ((cg*H*W + pix) << 3)
//             写地址 = base_out + ((cg*H*W + pix) << 3) + lane
//        这样既不会把 C8 当连续布局，也不会出现“y/x 解算差一”的错误。
//        前提：C 是 8 的倍数（验收形状 64/96/48 均满足；golden 的
//        bytearray(C*H*W) 本身也要求这一条），且 H*W>0。
//
//  4. 结构与流水（1 个 64-bit 读口 + 1 个 8-bit 写口）
//
//        稳态：8 拍一个像素组（8 个通道），即**每拍 1 个输出字节 / 1 个元素**。
//        写口带宽是硬上限：N=C*H*W 个元素至少 N 拍，任何实现都低不过它。
//
//        计算分两级，**塞在写窗口里**，不额外占拍：
//          拍 wcnt=0 : 写 ob[0]；同时发起下一像素组的 64-bit 读
//          拍 wcnt=1 : mem_rd_data 已有效（读 1 拍延迟）
//                      E1: x 符号扩展 → dd=x-zeta → dd_r；dd*slope → prod_r
//          拍 wcnt=2 : E2: 选支路 +gamma、>>>8（常量）、>>>shift、sat8 → nb
//          拍 wcnt=3..6: 空闲（余量，必要时可再插一级流水）
//          拍 wcnt=7 : 写 ob[7]；ob <= nb（下一像素组就绪）、pix++、wo++、wcnt=0
//        像素组 0 没有“上一个 burst 的预取”，走 ST_PRD/ST_PWT/ST_PE2 三步 prologue。
//
//        参数（8 通道的 zeta/gamma/slope）每个 C8 组装一次：
//          zeta : 4 个 64-bit 对齐字（每字 2 个 int32，通道 2j / 2j+1）
//          gamma: 4 个 64-bit 对齐字
//          slope: 2 个 64-bit 对齐字（每字 4 个 int16，通道 4j..4j+3）
//        共 10 个读，每字 2 拍（ST_PLP 发地址 / ST_PLW 收数据）；
//        H*W=64 时每组约 23 拍开销，约占 4%。
//
//  5. 读时序（最容易错的一条）
//
//        mem_rd_en 在 cycle t 拉高、mem_rd_addr 在 t 有效
//        ⇒ TB/BRAM 在 t+1 才给出 mem_rd_data
//        ⇒ 所有“收数据”的寄存器都在 t+1 的**时钟沿**采样（即 ST_PLW / ST_PWT /
//          wcnt==1 那一拍）。所以参数读是 2 拍一个（发/收），像素在 wcnt=1 采 E1。
//
//  6. 有符号 / 位宽铁律（本文件重点检查项）
//     · part-select 是**无符号**：'mem_rd_data[8*i +: 8]' 必须先 $signed()
//       再参与减法，否则负输入变 0..255（int8 语义全错）。
//     · 乘法/加法/移位都在**显式给定位宽**的有符号上下文里做；两个操作数
//       先显式符号扩展到目标宽度再乘（Verilog '*' 的结果宽度依赖上下文，不能赌）。
//     · '>>>' 只有在左操作数**声明为 signed** 时才是算术右移（负数向 -∞）。
//     · dd>0 用严格大于；dd==0 归负分支。
//     · 预取只在 !last_pix 时发起；wcnt=1/2 也只在 !last_pix 时收/算，
//       避免最后一像素拿到上一像素的陈旧 mem_rd_data（即便被丢弃也不留含糊）。
//
//  6.1 位宽前提表（超出前提则与 golden 不再等价；数值推导见各条注释）
//     P0  slope_shift ≡ 8  ：wrapper 在 bin_unit_axi_wrap.v:650 对 UNIT=1 强制
//                            'r_slope_shift != 8 → ERR_SHIFT'，复位值也是 8
//                            （同一文件 :453）。⇒ 'prod >>> 8' 写成**常量**
//                            移位，纯布线 0 LUT；原来的 'prod_r >>> sshift_r'
//                            是 32b × 6 级桶形移位器（约 1500 LUT / 8 lane）。
//     P1  |zeta| <= 130943 ：dd = x - zeta 用 18 bit 有符号承载（±131071）。
//                            x∈[-128,127] ⇒ |dd| <= |zeta|+128，故需
//                            |zeta| <= 131071-128 = 130943（= 2^17-129）。
//                            契约里 zeta 是 int32；**若上板真的给到 |zeta| 接近
//                            2^31，dd 要回到 33 bit**（golden 的 zeta 是 ±1000，
//                            验收形状远在前提内）。
//                            对照：dd 用 16 bit 只能撑 |zeta| <= 32512（这就是上一轮
//                            16 位版本在契约上不成立的原因）。
//     P1' gamma 任意 int32 ：r 给 34 bit：|prod>>>8| <= 2^25、|gamma| <= 2^31
//                            ⇒ |r| < 2^31 + 2^25 < 2^32，34 bit 有符号够。
//     P2  cfg_shift <= 15  ：v = r >>> shift 用 4 级桶形移位器（shift[3:0]）。
//                            wrapper 只查 'r_shift <= 63'（:613），所以 16..63
//                            是**契约未收紧的区间**；验收形状 shift=6。
//                            若要覆盖到 63，把 oshift_r[3:0] 改成 oshift_r 并
//                            接受 2 级额外延迟（约 +330 LUT）。
//     P3  |prod| 用 34 bit ：dd(18b) × slope(int16) 的完整乘积需要 18+16=34 bit；
//                            32 bit 只在 |slope| <= 512 时安全（|dd| 可达 2^17，
//                            2^17·512 = 2^26 ... 但当 |dd| = 2^17 且 |slope| > 512
//                            时 32 bit 就溢出），故按最坏情况取 34 bit。
//                            有效位宽 18b×16b 在 DSP48E1 的 25×18 之内；34 位只是
//                            P 口宽度（DSP 的 P 有 48 位）。
//
//  7. 拍数预算（rprelu_64_8x8：C=64,H=W=8,N=4096）
//        写：8 组 × 64 像素 × 8 拍 = 4096
//        参数+prologue：8 组 × (10*2 + 3) = 184
//        ⇒ 约 4280 拍（≈1.045 拍/字节；写带宽下限 4096 拍）。
// ============================================================================
`timescale 1ns/1ps

module bin_rprelu (
    input  logic        ap_clk,
    input  logic        ap_rst_n,
    input  logic        ap_ce,           // ★ P3: 同步时钟使能（包装层的 u_ce）
    input  logic        ctrl_start,
    output logic        ctrl_done,
    output logic        ctrl_busy,
    input  logic [15:0] cfg_c, cfg_h, cfg_w,
    input  logic [5:0]  cfg_slope_shift, cfg_shift,
    input  logic [31:0] base_in, base_zeta, base_gamma, base_slope, base_out,
    output logic        mem_rd_en,
    output logic [31:0] mem_rd_addr,
    input  logic [63:0] mem_rd_data,
    output logic        mem_wr_en,
    output logic [31:0] mem_wr_addr,
    output logic [7:0]  mem_wr_data
);

    // ========================================================================
    // 0. 常量 / 状态编码
    // ========================================================================
    localparam int NPW = 10;            // 每 C8 组的参数 64-bit 字数：4 zeta + 4 gamma + 2 slope

    localparam logic [2:0] ST_IDLE = 3'd0,
                           ST_PLP  = 3'd1,   // 参数：发读地址
                           ST_PLW  = 3'd2,   // 参数：收数据（mem_rd_data 在上一拍的沿之后有效）
                           ST_PRD  = 3'd3,   // 像素组 0：发读地址
                           ST_PWT  = 3'd4,   // 像素组 0：收数据 + E1
                           ST_PE2  = 3'd5,   // 像素组 0：E2 → ob
                           ST_WR   = 3'd6,   // 8 拍写 + 预取 + E1/E2
                           ST_DONE = 3'd7;

    // ========================================================================
    // 1. 配置锁存 / 计数器
    // ========================================================================
    logic [2:0]  state;
    logic [31:0] base_in_r, base_zeta_r, base_gamma_r, base_slope_r, base_out_r;
    logic [31:0] hw_r;                  // H*W：每个 C8 组的像素数
    logic [31:0] ncg_r;                 // C8 组数 = ceil(C/8)
    logic [31:0] cg_r;                  // 当前 C8 组
    logic [31:0] pix_r;                 // 当前组内像素 p=y*W+x
    logic [3:0]  p_idx;                 // 参数读序号 0..NPW-1
    logic [2:0]  wcnt;                  // 写 burst 内的字节号 0..7
    logic [5:0]  sshift_r, oshift_r;    // slope_shift / shift
    // ⚠ sshift_r 现在只做契约可见性保留（wrapper 强制 =8，数据通路用常量 8；
    //   见 §6.1 P0）。留着它不产生任何数据路径逻辑，综合会自行裁掉。
    // 字号计数器（替代 cg_r*hw_r 的 32×32 乘法，见 §2）
    logic [31:0] wo;                    // 线性字号 = cg*H*W + pix，恒为自增计数器

    // 每 C8 组 8 个通道的参数
    logic signed [31:0] zeta_g  [0:7];
    logic signed [31:0] gamma_g [0:7];
    logic signed [15:0] slope_g [0:7];

    // E1（乘/减）与 E2（选支路/移位/饱和）之间的流水寄存器
    // ⚠ 位宽按**真实范围**给：DSP48E1 原生只有 25×18，操作数超过它就退化成
    //   LUT 乘法器（LUT 暴涨 + 时序崩）。踩过的教训。
    logic signed [17:0] dd_r   [0:7];     // dd = x - zeta，18 bit（前提 P1：|zeta|<=130943）
    logic signed [33:0] prod_r [0:7];     // dd(18b) × slope(int16) ⇒ 34 bit（前提 P3）

    // 写 burst 缓冲
    logic [7:0] ob [0:7];               // 本像素组待写的 8 字节
    logic [7:0] nb [0:7];               // 预取的下一个像素组

    // ========================================================================
    // 2. 地址生成（C8 线性字号；见文件头 §3）
    //
    //  ⚠ 原实现 'wo_cur = cg_r * hw_r + pix_r' 是一个 **32×32 乘法器**，而且组合
    //    直接喂 mem_rd_addr ⇒ 既吃 800~1000 LUT，又是读地址的关键路径。
    //    关键洞察：cg*H*W + pix 在遍历中**恒等于一个自增计数器**——
    //      组内推进：pix→pix+1 ⇒ 字号 +1；
    //      换组：   pix 归零、cg→cg+1 ⇒ 字号 = cg*HW+0 恰好接上 (cg-1)*HW+(HW-1)+1。
    //    所以只要在"推进一个像素组"的那一拍做 wo+1，地址就是纯接线（0 LUT）。
    //    组/像素边界判断仍然用 cg_r/pix_r（不参与地址计算）。
    //    off_cur = wo<<3 是接线；wo < 2^29 即可（= 512 MB 偏移，所有验收形状远小于此）。
    // ========================================================================
    wire wo_step = (state == ST_WR) && (wcnt == 3'd7);   // 组尾：推进一个像素组

    wire [31:0] off_cur      = {wo[28:0], 3'b000};      // 字节偏移 = 字号*8（<<3 即接线）
    wire [31:0] in_addr_cur  = base_in_r  + off_cur;
    wire [31:0] in_addr_nxt  = base_in_r  + off_cur + 32'd8;   // 下一像素组 = +8
    wire [31:0] out_addr_cur = base_out_r + off_cur + {29'd0, wcnt};

    wire last_pix = (pix_r == (hw_r  - 32'd1));
    wire last_cg  = (cg_r  == (ncg_r - 32'd1));

    // 参数读地址：10 个对齐 8 字节字。
    //   p_idx 0..3 : zeta  组 cg 的第 p 字   → base_zeta  + 32*cg + 8*p
    //   p_idx 4..7 : gamma 组 cg 的第 p-4 字 → base_gamma + 32*cg + 8*(p-4)
    //   p_idx 8..9 : slope 组 cg 的第 p-8 字 → base_slope + 16*cg + 8*(p-8)
    // 用 (cg<<2) / (cg<<1) 显式 32 位算术，避开自决定位宽。
    logic [31:0] p_addr;
    always_comb begin
        if (p_idx < 4'd4)
            p_addr = base_zeta_r  + (((cg_r << 2) + {28'd0, p_idx}) << 3);
        else if (p_idx < 4'd8)
            p_addr = base_gamma_r + (((cg_r << 2) + {28'd0, p_idx} - 32'd4) << 3);
        else
            p_addr = base_slope_r + (((cg_r << 1) + {28'd0, p_idx} - 32'd8) << 3);
    end

    // ========================================================================
    // 3. 读端口（三选一：参数 / 像素组 0 / 写 burst 内预取）
    //    mem_rd_en 本拍拉高 ⇒ mem_rd_data 下一拍有效（BRAM 语义）
    // ========================================================================
    wire rd_param    = (state == ST_PLP);
    wire rd_pix_0    = (state == ST_PRD);
    wire rd_pix_pf   = (state == ST_WR) && (wcnt == 3'd0) && !last_pix;

    assign mem_rd_en   = rd_param | rd_pix_0 | rd_pix_pf;
    assign mem_rd_addr = rd_param  ? p_addr :
                         rd_pix_0  ? in_addr_cur :
                         rd_pix_pf ? in_addr_nxt : 32'd0;

    // ========================================================================
    // 4. 写端口（ST_WR 的 8 拍，一字节/拍）
    // ========================================================================
    assign mem_wr_en   = (state == ST_WR);
    assign mem_wr_addr = out_addr_cur;
    assign mem_wr_data = ob[wcnt];

    assign ctrl_busy   = (state != ST_IDLE) && (state != ST_DONE);

    // ========================================================================
    // 5. E1：x → dd → dd*slope（组合，结果在 e1_en 的时钟沿进 dd_r/prod_r）
    //
    //  ⚠ mem_rd_data 的字节 lane i 是**无符号** part-select，先拼成 18 位再
    //    $signed；等价于把 int8 符号扩展到 dd 的宽度。
    //  ⚠ 位宽（P1/P3，见 §6.1）：
    //      dd   = x - zeta    : 18 bit 有符号（前提 |zeta| <= 130943）
    //      prod = dd * slope  : 34 bit = 18+16 的**完整**乘积，对任意 int16 slope
    //                           精确；32 bit 在 |slope| > 512 时会溢出。
    //    有效位宽 18b × 16b 落在 DSP48E1 的 25×18 之内（34 位只影响 P 口，P 有
    //    48 位）—— 但操作数在 Verilog 里是 34 位常量符号扩展，请在综合报告里
    //    确认乘法器确实落在 DSP 上，而不是退化成 LUT 乘法器。
    // ========================================================================
    logic signed [17:0] dd_c   [0:7];
    logic signed [33:0] prod_c [0:7];

    always_comb begin
        for (int i = 0; i < 8; i++) begin
            // 18 位上下文：两个操作数都显式给到 18 位，减法不会自我扩宽
            dd_c[i] = $signed({{10{mem_rd_data[8*i + 7]}}, mem_rd_data[8*i +: 8]})
                      - $signed(zeta_g[i][17:0]);
            // 34 位上下文：18 位 × 34 位 ⇒ 表达式宽度 34，乘积不截断
            prod_c[i] = $signed({{16{dd_c[i][17]}}, dd_c[i]})
                        * $signed({{18{slope_g[i][15]}}, slope_g[i]});
        end
    end

    // E1 使能：像素组 0 的等待拍，或写 burst 的 wcnt==1（预取数据此时有效）
    wire e1_en = (state == ST_PWT) ||
                 ((state == ST_WR) && (wcnt == 3'd1) && !last_pix);

    // ========================================================================
    // 6. E2：选支路 +gamma → >>>slope_shift(常量8) → >>>shift → sat8
    //    dd_r/prod_r 在上一拍已稳定，本拍组合出 8 字节，在 e2_* 的沿进 ob/nb。
    //
    //  ⚠ 两处 LUT 黑洞在这里：
    //    (1) 'prod_r >>> sshift_r'：sshift_r 恒为 8（P0）⇒ 改成常量移位，
    //        综合后就是接线，0 LUT。
    //    (2) 'r_c >>> oshift_r'：原 40 位 × 6 级桶形移位器（约 1900 LUT）。
    //        收窄到 34 位 × 4 级（shift<=15，P2）。34 位是"int32 gamma + 2^25
    //        乘积"的最小精确宽度，再窄就不是等价变换了。
    // ========================================================================

    // ---- sat8：or/and-reduce 版（省掉两条 34 位大小比较器）----
    //  记 v 为 W 位有符号数，s = v[W-1]（符号位）：
    //    · v >= 128  ⇔  !s && |v[W-2:7]      （权重 >=128 的位里有 1）
    //    · v < -128  ⇔  s && !(&v[W-1:7])   （负数在 [-128,-1] 内时高位**全 1**
    //                                        且 bit7=1；展开即 s&&!(hi_and&&v[7])）
    //  ⇒ 三条分支各 1~2 个 LUT + 一个 26 位 or-reduce / and-reduce，
    //    取代 v>127 / v<-128 两条 34 位比较器（每条约 34 LUT）。
    //  ⚠ 注意 '|v[W-2:7]' 的**下界必须是 7 而不是 8**：
    //    - 正数 128..255 的判别只靠 bit7（漏掉 bit7 会把 200 当成 -56 输出）；
    //    - 负数除了高位全 1 还要求 bit7=1（漏掉 bit7 会把 -5 当成 -128 输出）。
    function automatic logic [7:0] sat8(input logic signed [33:0] v);
        logic hi_or, hi_and;
        begin
            hi_or  = |v[32:7];        // 权重 >=128 的位里有 1
            hi_and = &v[32:7];        // 权重 >=128 的位全 1
            sat8 = (!v[33] && hi_or)             ? 8'h7F :   // +127（v > 127）
                   ( v[33] && !(hi_and && v[7])) ? 8'h80 :   // -128（v < -128）
                   v[7:0];                                     // 在范围内，取低 8 位
        end
    endfunction

    logic signed [33:0] r_c [0:7];        // (prod>>>8) + gamma ⇒ |r| < 2^32，34 bit 精确
    logic signed [33:0] v_c [0:7];        // r >>> shift

    always_comb begin
        for (int i = 0; i < 8; i++) begin
            // dd==0 走负分支：严格 >0（见文件头 §1）
            if (dd_r[i] > 18'sd0)
                r_c[i] = dd_r[i] + $signed({{2{gamma_g[i][31]}}, gamma_g[i]});
            else
                // slope_shift ≡ 8（P0）⇒ 常量算术右移，纯布线 0 LUT。
                // prod_r 是 signed，'>>>' 就是算术右移（向 -∞，与 Python 一致）。
                r_c[i] = (prod_r[i] >>> 8)
                         + $signed({{2{gamma_g[i][31]}}, gamma_g[i]});
            // 算术右移（r_c 是 signed）；4 级桶形（P2：shift <= 15）
            v_c[i] = r_c[i] >>> oshift_r[3:0];
        end
    end

    wire e2_ob = (state == ST_PE2);
    wire e2_nb = ((state == ST_WR) && (wcnt == 3'd2) && !last_pix);

    // ========================================================================
    // 7. 主 FSM
    // ========================================================================
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            state      <= ST_IDLE;
            ctrl_done  <= 1'b0;
            base_in_r    <= 32'd0;
            base_zeta_r  <= 32'd0;
            base_gamma_r <= 32'd0;
            base_slope_r <= 32'd0;
            base_out_r   <= 32'd0;
            hw_r    <= 32'd0;
            ncg_r   <= 32'd0;
            cg_r    <= 32'd0;
            pix_r   <= 32'd0;
            p_idx   <= 4'd0;
            wcnt    <= 3'd0;
            sshift_r <= 6'd0;
            oshift_r <= 6'd0;
            wo      <= 32'd0;
        end else if (ap_ce && ctrl_start &&
                     ((state == ST_IDLE) || (state == ST_DONE))) begin
            // ---- 统一启动：锁配置、清遍历计数器 ----
            base_in_r    <= base_in;
            base_zeta_r  <= base_zeta;
            base_gamma_r <= base_gamma;
            base_slope_r <= base_slope;
            base_out_r   <= base_out;
            // H*W 与 C8 组数：显式 32 位上下文
            hw_r    <= {16'd0, cfg_h} * {16'd0, cfg_w};
            ncg_r   <= ({16'd0, cfg_c} + 32'd7) >> 3;
            sshift_r <= cfg_slope_shift;
            oshift_r <= cfg_shift;
            cg_r    <= 32'd0;
            pix_r   <= 32'd0;
            p_idx   <= 4'd0;
            wcnt    <= 3'd0;
            wo      <= 32'd0;
            ctrl_done <= 1'b0;
            state   <= ST_PLP;
        end else if (ap_ce) begin                   // ★ P3: 冻结在 CE 门内
            // ---- 计算流水（与 FSM 并行；条件由 state/wcnt 组合译码）----
            // 字号计数器：每写完一个像素组（= 组尾）推进 1。换组时 pix 归零、
            // cg+1，字号恰好接上，所以这里不需要任何乘法（见 §2）。
            if (wo_step) wo <= wo + 32'd1;

            if (e1_en) begin
                for (int i = 0; i < 8; i++) begin
                    dd_r[i]   <= dd_c[i];
                    prod_r[i] <= prod_c[i];
                end
            end
            if (e2_ob) begin
                for (int i = 0; i < 8; i++) ob[i] <= sat8(v_c[i]);
            end
            if (e2_nb) begin
                for (int i = 0; i < 8; i++) nb[i] <= sat8(v_c[i]);
            end

            // ---- 控制 ----
            // done 单一赋值源：最后一块的最后 1 字节写完的那拍置 1，DONE 保持，
            // 直到下一次 start 清 0（TB 在 negedge 轮询，1 拍脉冲也不会漏，
            // 但保持为高对后续 AXI wrap / PS 轮询更稳）。
            ctrl_done <= (state == ST_DONE) ||
                         ((state == ST_WR) && (wcnt == 3'd7) && last_pix && last_cg);
            case (state)
                ST_IDLE: state <= ST_IDLE;

                ST_PLP: state <= ST_PLW;

                ST_PLW: begin
                    // mem_rd_data 对应本拍 p_idx 发起的读（上一拍发地址）
                    case (p_idx)
                        4'd0: begin
                            zeta_g[0] <= $signed(mem_rd_data[31:0]);
                            zeta_g[1] <= $signed(mem_rd_data[63:32]);
                        end
                        4'd1: begin
                            zeta_g[2] <= $signed(mem_rd_data[31:0]);
                            zeta_g[3] <= $signed(mem_rd_data[63:32]);
                        end
                        4'd2: begin
                            zeta_g[4] <= $signed(mem_rd_data[31:0]);
                            zeta_g[5] <= $signed(mem_rd_data[63:32]);
                        end
                        4'd3: begin
                            zeta_g[6] <= $signed(mem_rd_data[31:0]);
                            zeta_g[7] <= $signed(mem_rd_data[63:32]);
                        end
                        4'd4: begin
                            gamma_g[0] <= $signed(mem_rd_data[31:0]);
                            gamma_g[1] <= $signed(mem_rd_data[63:32]);
                        end
                        4'd5: begin
                            gamma_g[2] <= $signed(mem_rd_data[31:0]);
                            gamma_g[3] <= $signed(mem_rd_data[63:32]);
                        end
                        4'd6: begin
                            gamma_g[4] <= $signed(mem_rd_data[31:0]);
                            gamma_g[5] <= $signed(mem_rd_data[63:32]);
                        end
                        4'd7: begin
                            gamma_g[6] <= $signed(mem_rd_data[31:0]);
                            gamma_g[7] <= $signed(mem_rd_data[63:32]);
                        end
                        4'd8: begin
                            slope_g[0] <= $signed(mem_rd_data[15:0]);
                            slope_g[1] <= $signed(mem_rd_data[31:16]);
                            slope_g[2] <= $signed(mem_rd_data[47:32]);
                            slope_g[3] <= $signed(mem_rd_data[63:48]);
                        end
                        4'd9: begin
                            slope_g[4] <= $signed(mem_rd_data[15:0]);
                            slope_g[5] <= $signed(mem_rd_data[31:16]);
                            slope_g[6] <= $signed(mem_rd_data[47:32]);
                            slope_g[7] <= $signed(mem_rd_data[63:48]);
                        end
                        default: ;
                    endcase

                    if (p_idx == NPW - 1) begin
                        p_idx  <= 4'd0;
                        pix_r  <= 32'd0;
                        wcnt   <= 3'd0;
                        state  <= ST_PRD;
                    end else begin
                        p_idx <= p_idx + 4'd1;
                        state <= ST_PLP;
                    end
                end

                ST_PRD: state <= ST_PWT;
                ST_PWT: state <= ST_PE2;
                ST_PE2: begin
                    wcnt  <= 3'd0;
                    state <= ST_WR;
                end

                ST_WR: begin
                    if (wcnt == 3'd7) begin
                        if (last_pix) begin
                            if (last_cg) begin
                                wcnt  <= 3'd0;
                                state <= ST_DONE;
                            end else begin
                                // 下一 C8 组：重新装 8 通道参数
                                cg_r  <= cg_r + 32'd1;
                                pix_r <= 32'd0;
                                p_idx <= 4'd0;
                                wcnt  <= 3'd0;
                                state <= ST_PLP;
                            end
                        end else begin
                            // 本组下一像素组：预取结果 nb 转正
                            for (int i = 0; i < 8; i++) ob[i] <= nb[i];
                            pix_r <= pix_r + 32'd1;
                            wcnt  <= 3'd0;
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
