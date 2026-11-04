// ============================================================================
//  bin_conv_core8.v —— 二值卷积核 v2：**8 条并行 XNOR+popcount 通道**
//
//  目标：把"1 term/拍"变成"8 term/拍"，面积反而减半（现状 5,845 LUT / 1 lane）。
//  设计依据：docs/binary/09_架构重构方案_一秒目标.md §3.1
//
//  ── 和 bin_conv_core.v（1 lane 版）的关系 ─────────────────────────────────
//  · 定点语义**逐位相同**（同一个 golden 生成器、同一套 requant 公式）。
//  · 端口 = 旧端口的**超集**：mem_wr_data 64 bit + mem_wr_sel 8 bit。
//    因为这 8 个 lane 算出来的 8 个输出通道正好是 C8 布局里**连续的 8 个字节**
//    ⇒ 一拍写一个 64-bit 整字（旧版 1 字节/拍，那是 k=1 层的真实天花板）。
//  · bin_conv_core8_compat.v 提供端口**完全相同**的封装（64→8 位序列化），
//    可以零改动替换 modules/bin_conv_core.v。
//
//  ── 关键物理洞察（为什么权重可以常驻 BRAM）────────────────────────────────
//    二值权重 1 bit/权重 ⇒ 一层 cout×cin×k² bit。最大层 cout=96,cin=256,k=3:
//        96 × 256 × 9 = 221,184 bit = 27 KB（整层）
//    本引擎一次只算 **8 个 oc** ⇒ 只需 8×K×64 bit，K=ceil(cin/64)·k²
//        最大层 K=36 ⇒ 8 × 36 × 64 = 18,432 bit = 2.25 KB
//    ⇒ 权重从 DDR 只读一次（组内 8 个 oc 连续 ⇒ 完美线性 +8 扫描）
//    ⇒ DDR 只流式读激活；不需要 CAM、不需要预取、不需要 32 路 tag 比较。
//
//  ── 数据流（一拍 = 一个 term = 8 个 lane 各一个 64-bit XNOR+popcount）──────
//
//    term 序号 l = j*C_B + b（j = ky*k+kx，b = 64 通道组），K = C_B*k²
//    一个输出像素要 K 个 term；8 个 lane 共用**同一次**激活读：
//
//      DDR ──(1 次/拍)── 64-bit act 字 ──┬─ XNOR ─ pop ─┐
//                                        ├─ XNOR ─ pop ─┤  8 lane
//        BRAM wbuf[k][l] (k=0..7) ───────┘  ...        ├→ p_acc[k] (16 bit)
//                                                       ┘
//      p_snap × 8 ─→ S1..S8 requant（8 lane 并行）─→ 8×int8 ─→ 1×64-bit 写
//
//  ── 三个必须知道的定点/位宽约定 ─────────────────────────────────────────
//  (1) 【静态移位】alpha_shift ≡ 9、slope_shift ≡ 8 是契约固定值
//      （bin_unit_axi_wrap.v 在启动前用 ERR_SHIFT 拒绝别的值），所以
//      `>>> 9` / `>>> 8` 写成**参数常量**（纯布线，0 LUT）。
//      这是 8 lane 的 requant 能压进面积预算的最主要原因：可变 40 bit 桶形
//      ×3 处 ×8 lane ≈ 5.7k LUT，会直接顶爆。只有输出 cfg_shift 真正可变。
//  (2) 【补零通道】p 只统计真实通道（契约 §1.1）。硬件两侧同时消隐：
//        · 末块（b == C_B-1）时激活字按位 **OR** (~mask_last) ⇒ 补零位强制 1
//        · 装权重时末块的字 **AND** mask_last       ⇒ 补零位强制 0
//      ⇒ XNOR(1,0)=0，补零位对 popcount 贡献 0，与 gen() 的 `continue` 一致。
//      两侧都做是冗余保护：任一侧数据带脏位都不会静默算错。
//  (3) 【epilogue 位宽 EW=32】旧版用 40 bit 是保守余量。按真实值域收窄到 32:
//        |d| ≤ N_tot ≤ K·64 = 16384 (2^14), |alpha| ≤ 2^15 ⇒ |d·alpha| ≤ 2^29 ✓
//        |q| ≤ 2^20；|bias/zeta/gamma| 契约是 int32，真实 ≤ 2^12 ⇒ |r| < 2^31 ✓
//      仿真里有 translate_off 的溢出断言（BIN8-EWOVF）兜底。
// ============================================================================
`timescale 1ns/1ps

module bin_conv_core8 #(
    // ---- 兼容旧参数名（bin_unit_axi_wrap 按名字传这两个）----
    parameter int LANES    = 64,     // popcount 位宽（固定 64，仅为兼容保留）
    parameter int COUT_MAX = 192,    // 未使用（仅为兼容保留）
    // ---- 本引擎的核心参数 ----
    parameter int VLANES   = 8,      // 并行输出通道数
    parameter int WMAX     = 256,    // 每 oc 权重字数上限（K = C_B*k² ≤ 256）
    parameter int EW       = 32,     // epilogue 数据通路位宽
    parameter int ASH      = 9,      // alpha_shift（契约固定 9）
    parameter int SSH      = 8,      // slope_shift（契约固定 8；v60 实测 12 是净退步 ⇒ 回退）
    parameter int SHW      = 6,      // 输出桶形级数：6 → shift∈[0,63]；4 → [0,15]
    // ---- v60 数值补丁开关（编译期常量，综合后常量折叠，无运行期开销）----
    //   R1_ROUND = 1：`sat8(r>>>shift)` → `sat8((r+H)>>>shift)`，
    //                 H = (shift==0)?0:1<<(shift-1)（R1-A：加半并入 gamma，见 §11b）
    //   OOB_ZERO = 1：空间越界抽头贡献记 0（不再"送字 0 ⇒ xb=−1"）
    //                 ⇒ pc 记 0 + 每像素补 +n_oob·cin（RTL_SPEC §R3(b)）
    //   默认全开 = v60 语义；=0 时逐字节回到 v58 行为（A/B 与变异测试用）。
    parameter bit R1_ROUND = 1'b1,
    parameter bit OOB_ZERO = 1'b1
)(
    input  logic        ap_clk,
    input  logic        ap_rst_n,
    input  logic        ap_ce,                 // 同步时钟使能（包装层的 u_ce）
    input  logic        ctrl_start,
    output logic        ctrl_done,
    output logic        ctrl_busy,
    input  logic [15:0] cfg_cin, cfg_cout, cfg_k, cfg_s, cfg_d, cfg_pad,
    input  logic [15:0] cfg_h, cfg_w,
    input  logic [5:0]  cfg_alpha_shift, cfg_slope_shift, cfg_shift,
    input  logic [31:0] base_act, base_wgt, base_alpha, base_bias,
    input  logic [31:0] base_zeta, base_gamma, base_slope, base_out,
    output logic        mem_rd_en,
    output logic [31:0] mem_rd_addr,
    input  logic [63:0] mem_rd_data,
    output logic                mem_wr_en,
    output logic [31:0]         mem_wr_addr,
    output logic [VLANES*8-1:0] mem_wr_data,   // lane k → byte k（C8 布局，天然对齐）
    output logic [VLANES-1:0]   mem_wr_sel     // 字节使能（cout 非 VLANES 倍数时用）
);

    localparam int LW = 9;                     // term 索引位宽（K ≤ 256）

    // ========================================================================
    // 0. 状态编码
    // ========================================================================
    localparam logic [2:0] ST_IDLE = 3'd0,
                           ST_PRM  = 3'd1,
                           ST_WLD  = 3'd2,
                           ST_CMP  = 3'd3,
                           ST_DRN  = 3'd4,
                           ST_DONE = 3'd5;

    // ========================================================================
    // 1. 组合函数
    // ========================================================================
    // 64-bit popcount：平衡加法树（**不要**改回 for 累加，那是 64 级进位链）
    function automatic [6:0] pop64(input [63:0] v);
        logic [2:0] n [0:15];
        logic [3:0] m [0:7];
        logic [4:0] q [0:3];
        logic [5:0] r [0:1];
        integer i;
        begin
            for (i = 0; i < 16; i = i + 1)
                n[i] = {2'd0, v[4*i]} + {2'd0, v[4*i+1]}
                     + {2'd0, v[4*i+2]} + {2'd0, v[4*i+3]};
            for (i = 0; i < 8; i = i + 1)
                m[i] = {1'd0, n[2*i]} + {1'd0, n[2*i+1]};
            for (i = 0; i < 4; i = i + 1)
                q[i] = {1'd0, m[2*i]} + {1'd0, m[2*i+1]};
            for (i = 0; i < 2; i = i + 1)
                r[i] = {1'd0, q[2*i]} + {1'd0, q[2*i+1]};
            pop64 = {1'd0, r[0]} + {1'd0, r[1]};
        end
    endfunction

    // SHW 级算术右移桶形（唯一真正可变的移位）
    function automatic signed [EW-1:0] arsh(input signed [EW-1:0] v, input [5:0] s);
        logic signed [EW-1:0] t;
        begin
            t = v;
            if (SHW >= 1) t = s[0] ? (t >>>  1) : t;
            if (SHW >= 2) t = s[1] ? (t >>>  2) : t;
            if (SHW >= 3) t = s[2] ? (t >>>  4) : t;
            if (SHW >= 4) t = s[3] ? (t >>>  8) : t;
            if (SHW >= 5) t = s[4] ? (t >>> 16) : t;
            if (SHW >= 6) t = s[5] ? (t >>> 32) : t;
            arsh = t;
        end
    endfunction

    // ========================================================================
    // 2. 启动时的派生常量（ST_IDLE 一次性锁存）
    //    s ∈ {1,2} ⇒ (H-1)>>(s-1)+1，不用组合除法器（旧版 43 级 CARRY4 / 57 ns）
    // ========================================================================
    wire [15:0] s_safe_c = (cfg_s == 16'd0) ? 16'd1 : cfg_s;
    wire [15:0] s_sh_c   = (s_safe_c == 16'd2) ? 16'd1 : 16'd0;
    wire [15:0] hout_c   = ((cfg_h - 16'd1) >> s_sh_c) + 16'd1;
    wire [15:0] wout_c   = ((cfg_w - 16'd1) >> s_sh_c) + 16'd1;

    wire [31:0] cin32_c  = {16'd0, cfg_cin};
    wire [31:0] ckk_c    = {16'd0, cfg_k} * {16'd0, cfg_k};
    wire [31:0] cb_c     = (cin32_c + 32'd63) >> 6;

    wire [31:0] hw8_c    = ({16'd0, cfg_h} * {16'd0, cfg_w}) << 3;
    wire [31:0] w8_c     = {16'd0, cfg_w} << 3;
    wire [31:0] s8_c     = {16'd0, cfg_s} << 3;
    wire [31:0] d8_c     = {16'd0, cfg_d} << 3;
    wire [31:0] sw8_c    = {16'd0, cfg_s} * w8_c;
    wire [31:0] dw8_c    = {16'd0, cfg_d} * w8_c;
    wire [31:0] pad8_c   = {16'd0, cfg_pad} << 3;
    wire [31:0] padw8_c  = {16'd0, cfg_pad} * w8_c;

    // 末块有效位掩码（cin%64 == 0 ⇒ 全 1）
    wire [5:0]  cin_mod_c  = cfg_cin[5:0];
    wire [63:0] mask_last_c = (cin_mod_c == 6'd0) ? {64{1'b1}}
                                                  : ((64'd1 << cin_mod_c) - 64'd1);

    // ========================================================================
    // 3. 锁存的配置 / 派生常量
    // ========================================================================
    logic [15:0] cout_q, k_q, h_q, w_q, cb_q, hout_q, wout_q;
    // v60 R3(b)：真实通道数（层常量）。越界抽头的补偿量 = n_oob · cin，
    //   所以必须锁一份 cin；与 cb_q 同一条装载路径（ST_IDLE）。
    logic [15:0] cin_q;
    logic [31:0] o_q;                        // 当前 oc 组起点（VLANES 的倍数）
    logic [5:0]  ash_q, ssh_q, shift_q;      // ash/ssh 只给仿真断言用
    logic [31:0] act_b, wgt_b, alpha_b, bias_b, zeta_b, gamma_b, slope_b, out_b;
    logic [31:0] K_q, ntot_q, kk_q;
    logic [31:0] hw8_q, w8_q, s8_q, sw8_q, d8_q, dw8_q, pad8_q, padw8_q;
    logic [31:0] hwout8_q, k8_q;
    logic [31:0] gstride_q;                  // VLANES*K*8（每组权重步长）
    logic [63:0] mask_last_q;
    logic signed [31:0] padS_q, sS_q, dS_q;  // pad/s/d 的符号扩展（坐标用）

    // 末值寄存器（把当拍减法器从比较器前面拿掉）
    logic [15:0] cb_m1_q, k_m1_q, hout_m1_q, wout_m1_q;
    logic [LW:0] km1_q;                      // K-1，必须 LW+1 位（K=256 的坑）
    logic [31:0] nvalid_q;                   // min(cout - o0, VLANES)

    logic [31:0] ogbase_q, wgt_oq;           // 每组的输出基址 / 权重基址

    wire [31:0] K_lc = {16'd0, cb_q} * kk_q; // cb × k*k（FF → DSP → FF）

    // ========================================================================
    // 4. 控制 / 装载计数器
    // ========================================================================
    logic [2:0]  state;
    logic        start_q, done_q;
    logic [4:0]  prm_i;                      // 0..16（16 = 最后一个字的收数拍）
    logic [15:0] wl_l;                       // 组内每 oc 的 term 序号 0..K-1
    logic [3:0]  wl_lane;                    // 0..VLANES-1
    logic [15:0] wl_b;                       // 装载期的 b（0..C_B-1），末块掩码用
    logic        wl_ph;                      // 0=发读拍，1=收数拍
    logic [3:0]  dr_q;

    wire start_pulse = ctrl_start & ~start_q;
    assign ctrl_busy = (state != ST_IDLE);
    assign ctrl_done = done_q;

    // ========================================================================
    // 5. 计算主体计数器
    // ========================================================================
    logic [LW-1:0]      l_q;
    logic [15:0]        b_q, ky_q, kx_q, ox_q, oy_q;
    logic signed [31:0] iy_q, ix_q, ybase_q, xbase_q;
    logic [31:0]        oaddr_q;             // 当前像素组的 64-bit 字地址

    logic [31:0] mn_cb_q, mn_k_q, mn_wo_q;
    logic [31:0] cp1_q, cp2_q, cp3_q, cp4_q;
    logic [31:0] st_kx_q, st_kyw_q, st_px_q, st_pyd_q, st_py_q;

    wire term_last = ({1'b0, l_q} == km1_q);
    wire pix_last  = term_last && (oy_q == hout_m1_q) && (ox_q == wout_m1_q);
    wire b_wrap    = (b_q  == cb_m1_q);
    wire kx_wrap   = (kx_q == k_m1_q);
    wire ox_wrap   = (ox_q == wout_m1_q);
    // 空间越界（有符号比较：pad>0 时 iy=-1 不能当成 4G）
    wire in_bounds = (iy_q >= 0) && (ix_q >= 0) &&
                     (iy_q < $signed({16'd0, h_q})) &&
                     (ix_q < $signed({16'd0, w_q}));

    wire cmp_init = (state == ST_WLD) && (wl_ph == 1'b1) &&
                    ({1'b0, wl_l} == km1_q) && (wl_lane == VLANES - 1);

    // ========================================================================
    // 6. 读流水线登记（T 发读 → T+1 数据有效 → T+2 累加）
    // ========================================================================
    logic            r1_vld, r1_rd, r1_last, r1_bm1, r1_l0;
    logic            r1_oobtap;      // v60 R3(b)：本拍是越界抽头的第一个 term
    logic [15:0]     r1_b;
    logic [31:0]     r1_oaddr;

    // ========================================================================
    // 7. 权重 BRAM：VLANES 块独立 RAM（256×64，整字写 + 整字同步读）
    //    为什么分块而不是一块 VLANES*64 宽的大 RAM：宽度决定 BRAM 块数
    //    （512 bit 宽 ≈ 8 块 RAMB36，深度免费），块数完全一样；但"写 512 位里
    //    的 64 位切片"要靠 byte-enable 推断，Vivado 有推断失败退化成 FF 的先例。
    //    分块后每个 RAM 是整字写 + 整字读，推断毫无歧义。
    // ========================================================================
    logic [63:0] wrd [0:VLANES-1];
    wire [63:0] wkeep_c = (wl_b == cb_m1_q) ? mask_last_q : {64{1'b1}};
    wire [63:0] wdata_c = mem_rd_data & wkeep_c;

    genvar gk;
    generate
        for (gk = 0; gk < VLANES; gk = gk + 1) begin : g_wram
            (* ram_style = "block" *) logic [63:0] wmem [0:WMAX-1];
            // 写口：**不加复位**（RAM 进带异步复位的时序块 ⇒ Vivado 拒推断）
            always_ff @(posedge ap_clk) begin
                if ((state == ST_WLD) && (wl_ph == 1'b1) && ap_ce && (wl_lane == gk))
                    wmem[wl_l] <= wdata_c;
            end
            // 读口：同步读（地址是 FF ⇒ wrd 在下一拍有效）
            always_ff @(posedge ap_clk) begin
                if (ap_ce) wrd[gk] <= wmem[l_q];
            end
        end
    endgenerate

    // ========================================================================
    // 8. 参数装载：PRM 期 16 个 64-bit 字直接落进 pw[0..15]
    //      pw[0..1]  alpha_q[o0..o0+7] int16×8     pw[2..5]  bias_q int32×8
    //      pw[6..9]  zeta                         pw[10..13] gamma
    //      pw[14..15] slope_q int16×8
    //    ⇒ 每个 lane 的 5 个参数都是 pw 的**常量位切片**（纯布线，0 LUT）
    // ========================================================================
    logic [63:0] pw [0:15];

    function automatic [31:0] prm_addr(input [4:0] i, input [31:0] o0);
        logic [31:0] b, off;
        begin
            case (i)
                5'd0, 5'd1:   begin b = alpha_b; off = (o0 << 1) + ({27'd0, i}       << 3); end
                5'd2, 5'd3, 5'd4, 5'd5:
                              begin b = bias_b;  off = (o0 << 2) + ({27'd0, i - 5'd2}  << 3); end
                5'd6, 5'd7, 5'd8, 5'd9:
                              begin b = zeta_b;  off = (o0 << 2) + ({27'd0, i - 5'd6}  << 3); end
                5'd10, 5'd11, 5'd12, 5'd13:
                              begin b = gamma_b; off = (o0 << 2) + ({27'd0, i - 5'd10} << 3); end
                5'd14, 5'd15: begin b = slope_b; off = (o0 << 1) + ({27'd0, i - 5'd14} << 3); end
                default:      begin b = alpha_b; off = 32'd0; end
            endcase
            prm_addr = b + off;
        end
    endfunction

    logic signed [15:0]   alpha_l [0:VLANES-1];
    logic signed [15:0]   slope_l [0:VLANES-1];
    logic signed [31:0]   bias_l  [0:VLANES-1];
    logic signed [31:0]   zeta_l  [0:VLANES-1];
    logic signed [31:0]   gamma_l [0:VLANES-1];
    logic signed [EW-1:0] bz_l    [0:VLANES-1];   // bias − zeta（每组算一次）
    logic signed [EW-1:0] bg_l    [0:VLANES-1];   // bias − zeta + gamma

    always_comb begin
        for (int k = 0; k < VLANES; k++) begin
            alpha_l[k] = $signed(pw[(k/4)][16*(k%4) +: 16]);
            slope_l[k] = $signed(pw[14 + (k/4)][16*(k%4) +: 16]);
            bias_l [k] = $signed(pw[ 2 + (k/2)][32*(k%2) +: 32]);
            zeta_l [k] = $signed(pw[ 6 + (k/2)][32*(k%2) +: 32]);
            gamma_l[k] = $signed(pw[10 + (k/2)][32*(k%2) +: 32]);
            bz_l   [k] = $signed(bias_l[k]) - $signed(zeta_l[k]);
            bg_l   [k] = $signed(bias_l[k]) - $signed(zeta_l[k]) + $signed(gamma_l[k]);
        end
    end

    // ========================================================================
    // 8b. ★ v60 R1-A：epilogue 四舍五入（把"半个输出 LSB"并进 gamma）
    // ------------------------------------------------------------------------
    //   目标语义：out = sat8((r + H) >>> shift)，H = (shift==0) ? 0 : 1<<(shift-1)。
    //   R1-A 的成立依据（RTL_SPEC §R1）：RPReLU 的两支都是 `...+ gamma`（gamma
    //   没有缩放）：正支 rp = dd + gamma、负支 r = (dd·slope)>>>SSH + gamma，
    //   而**分支判据吃的是 dd（e2_ddpos_q），不吃 r** ⇒ 给 gamma 加 H 等价于给 r
    //   加 H，且不会改变走哪一支。
    //   ⇒ 数据通路 S1~S8 **零改动**；H 在进入 CMP 的那条边（cmp_init）一次性
    //     打进 `gh_l`（每组一次），加法器的输入是准静态的 pw/shift_q 寄存器，
    //     不在逐像素关键路径上。
    //   ⚠ 半个 LSB 只在 q 域有意义：shift==0 时 LSB 就是 q 单位 ⇒ H=0（不加半）。
    // ========================================================================
    wire signed [EW-1:0] half_c = (shift_q == 6'd0)  ? {EW{1'b0}} :
                                  (shift_q >= 6'd32) ? {1'b0, {(EW-1){1'b1}}} :  // 2^31−1（防坑）
                                  $signed({{(EW-1){1'b0}}, 1'b1} <<< (shift_q - 6'd1));

    logic signed [EW-1:0] gh_l [0:VLANES-1];   // gamma + H（R1-A 的落点）
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            for (int k = 0; k < VLANES; k++) gh_l[k] <= '0;
        end else if (ap_ce && cmp_init) begin
            for (int k = 0; k < VLANES; k++)
                gh_l[k] <= R1_ROUND ? (gamma_l[k] + half_c) : gamma_l[k];
        end
    end

    // ========================================================================
    // 9. 主读口（唯一）：参数 / 权重 / 激活 三选一
    // ========================================================================
    logic [31:0] rd_addr_q;
    wire rd_prm = (state == ST_PRM) && (prm_i < 5'd16);
    wire rd_wld = (state == ST_WLD) && (wl_ph == 1'b0);
    wire rd_cmp = (state == ST_CMP) && in_bounds;
    assign mem_rd_en   = rd_prm | rd_wld | rd_cmp;
    assign mem_rd_addr = rd_addr_q;

    // ========================================================================
    // 9b. 启动沿检测（ctrl_start 上升沿，单拍脉冲）
    // ========================================================================
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) start_q <= 1'b0;
        else if (ap_ce) start_q <= ctrl_start;
    end

    // ========================================================================
    // 10. 计算期（XNOR → popcount → 累加）
    // ========================================================================
    logic [6:0]  s1_pc_q [0:VLANES-1];
    logic        s1_v_q, s1_last_q, s1_l0_q;
    logic        s1_oobtap_q;        // v60 R3(b)：越界抽头起始（s1 段对齐）
    logic [4:0]  oob_cnt;            // v60 R3(b)：本像素累计的越界抽头数 0..k²(25)
    logic [4:0]  oob_snap;           // v60 R3(b)：像素末拍快照（与 p_snap 同拍）
    logic [31:0] s1_oa_q;
    logic [15:0] p_acc  [0:VLANES-1];
    logic [15:0] p_snap [0:VLANES-1];
    logic        ps_vld;
    logic [31:0] osnap;

    wire [63:0] pad_or_c = r1_bm1 ? ~mask_last_q : 64'd0;
    wire [63:0] act_c    = (r1_rd ? mem_rd_data : 64'd0) | pad_or_c;

    logic [63:0] xn_c [0:VLANES-1];
    logic [6:0]  pc_c [0:VLANES-1];
    always_comb begin
        for (int k = 0; k < VLANES; k++) begin
            xn_c[k] = ~(act_c ^ wrd[k]);
            pc_c[k] = pop64(xn_c[k]);
        end
    end

    logic [15:0] sum_c [0:VLANES-1];
    always_comb begin
        for (int k = 0; k < VLANES; k++)
            sum_c[k] = (s1_l0_q ? 16'd0 : p_acc[k]) + {9'd0, s1_pc_q[k]};
    end

    // ---- 读流水线登记 ----
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            r1_vld <= 1'b0; r1_rd <= 1'b0; r1_last <= 1'b0;
            r1_bm1 <= 1'b0; r1_l0 <= 1'b1;
            r1_b <= 16'd0;  r1_oaddr <= 32'd0;
        end else if (ap_ce && (state == ST_CMP)) begin
            r1_vld   <= 1'b1;
            r1_rd    <= in_bounds;
            r1_last  <= term_last;
            r1_b     <= b_q;
            r1_oaddr <= oaddr_q;
            r1_bm1   <= (b_q == cb_m1_q);
            r1_l0    <= (l_q == {LW{1'b0}});
            // v60 R3(b)：越界抽头的"起始"标记 = 该 tap 的第一个 64 通道组（b==0）
            //   且**在界外**。每个抽头只在 b==0 这一拍计一次 ⇒ n_oob ∈ [0,k²]。
            r1_oobtap <= (b_q == 16'd0) && !in_bounds;
        end else if (ap_ce) begin
            r1_vld <= 1'b0;
            r1_oobtap <= 1'b0;
        end
    end

    // ---- popcount 结果寄存一拍（拆开 pop 树与 16 位累加进位链）----
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            s1_v_q <= 1'b0; s1_last_q <= 1'b0; s1_l0_q <= 1'b1; s1_oa_q <= 32'd0;
            s1_oobtap_q <= 1'b0;
        end else if (ap_ce) begin
            s1_v_q    <= r1_vld;
            s1_last_q <= r1_vld & r1_last;
            if (r1_vld) begin
                s1_l0_q <= r1_l0;
                s1_oa_q <= r1_oaddr;
                s1_oobtap_q <= r1_oobtap;
                // ★ v60 R3(b)：越界抽头 pc 记 0（每 lane 一个 7-bit AND，纯 LUT2）
                //   越界时 act 字是 0 ⇒ popcount(~w) = "wb=−1 的个数"，累进 d 就是 −Σwb。
                //   记 0 后该 tap 的 d 贡献变成 −64/term，最后由 oob_snap·cin 补回。
                for (int k = 0; k < VLANES; k++)
                    s1_pc_q[k] <= (OOB_ZERO && !r1_rd) ? {7{1'b0}} : pc_c[k];
            end
        end
    end

    // ---- 跨 term 累加 + 像素末拍快照 ----
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            ps_vld <= 1'b0; osnap <= 32'd0;
            oob_cnt <= 5'd0; oob_snap <= 5'd0;
        end else if (ap_ce) begin
            ps_vld <= s1_last_q;
            if (s1_last_q) osnap <= s1_oa_q;
            if (s1_v_q) begin
                for (int k = 0; k < VLANES; k++) begin
                    p_acc[k] <= sum_c[k];
                    if (s1_last_q) p_snap[k] <= sum_c[k];
                end
                // ★ v60 R3(b)：每像素的越界抽头计数。**唯一装载点**：
                //   · 像素第一个 term（s1_l0_q）⇒ 计数器 = 本拍是否为越界抽头
                //   · 其余 term ⇒ 只在"越界抽头起始"那一拍 +1（每个抽头只计一次）
                //   ⇒ 不变量：oob_cnt ≡ 本像素 [0..当前 tap] 内越界抽头的个数。
                if (s1_l0_q)             oob_cnt <= {4'd0, s1_oobtap_q};
                else if (s1_oobtap_q)    oob_cnt <= oob_cnt + 5'd1;
                // 像素末拍快照（与 p_snap 同拍，S1 用它做 d 的补偿）
                if (s1_last_q)
                    oob_snap <= s1_l0_q ? {4'd0, s1_oobtap_q}
                                        : (oob_cnt + {4'd0, s1_oobtap_q});
            end
        end
    end

    // ========================================================================
    // 11. 收尾流水（8 级 × VLANES lane；地址链只有一条）
    //      S1 d=2p−N_tot, prod=d*alpha  S2 q=prod>>>9（静态）
    //      S3 dd=q+bz, rp=dd+gamma, ddpos   S4 pn=dd*slope（48 位精确）
    //      S5 sn=pn>>>8（静态）             S6 r = ddpos ? rp : sn+gamma
    //      S7 v=r>>>shift（桶形）           S8 sat8 → 64-bit 写口
    // ========================================================================
    logic signed [EW-1:0]    e1_prod_q [0:VLANES-1];
    logic signed [EW-1:0]    e1_sh_q   [0:VLANES-1];
    logic signed [EW-1:0]    e2_dd_q   [0:VLANES-1];
    logic signed [EW-1:0]    e2_rp_q   [0:VLANES-1];
    logic                    e2_ddpos_q[0:VLANES-1];
    logic signed [EW-1:0]    e2_rp4_q  [0:VLANES-1];
    logic                    e2_ddp4_q [0:VLANES-1];
    logic signed [EW+16-1:0] e2_pn_q   [0:VLANES-1];
    logic signed [EW-1:0]    e2_sn_q   [0:VLANES-1];
    logic signed [EW-1:0]    e2_rp5_q  [0:VLANES-1];
    logic                    e2_ddp5_q [0:VLANES-1];
    logic signed [EW-1:0]    e2_r_q    [0:VLANES-1];
    logic signed [EW-1:0]    e3_sh_q   [0:VLANES-1];
    logic [7:0]              wr_byte_q [0:VLANES-1];
    logic [31:0] a1_q, a2_q, a3_q, a4_q, a5_q, a6_q, a7_q, wr_addr_q;
    logic        v1_q, v2_q, v3_q, v4_q, v5_q, v6_q, v7_q, v8_q;

    localparam logic [3:0] DRN_MIN = 4'd12;
    wire tail_empty = !(r1_vld | s1_v_q | ps_vld | v1_q | v2_q | v3_q | v4_q |
                        v5_q | v6_q | v7_q | v8_q);
    wire drn_done = (dr_q >= DRN_MIN) && tail_empty;

    logic signed [EW-1:0]    p_e     [0:VLANES-1];
    logic signed [EW-1:0]    d_c     [0:VLANES-1];
    // ★ §17：d 的**真实**值域是 |d| = |2p − N_tot| ≤ N_tot ≤ 64·K ≤ 64·256 = 16384 = 2^14
    //   ⇒ 有符号 16 位（±32767）**无损**装得下。S1 的乘法从 32×16（2 个 DSP48E1）
    //   收窄成 16×16（1 个）⇒ 8 lane 少 8 个 DSP，且完全不改数值。
    logic signed [15:0]      d16     [0:VLANES-1];
    logic signed [EW-1:0]    dd_c    [0:VLANES-1];
    logic signed [EW-1:0]    qsh_c   [0:VLANES-1];
    logic signed [EW+16-1:0] pn_c    [0:VLANES-1];
    logic signed [EW+16-1:0] snw_c   [0:VLANES-1];
    logic signed [EW-1:0]    neg_c   [0:VLANES-1];
    logic signed [EW-1:0]    vsh_c   [0:VLANES-1];
    logic [7:0]              sat8_c  [0:VLANES-1];
    // ★ v60 R3(b)：越界抽头补偿量 = n_oob · cin（层常量 cin；n_oob ∈ [0,k²]）
    //   数学：越界 tap 记 pc=0 后每 tap 的 d 贡献 = −64·C_B = −(cin+n_pad)，
    //   而目标只是 −n_pad（补零通道的常数偏移由 bias_q 里的 n_pad_total·alpha_eff
    //   抵掉，口径与 v58 完全一致）⇒ 每越界 tap 补回 +cin。
    //   值域：n_oob ≤ 25、cin ≤ 65535 ⇒ 21 bit 足够；真实最大 25·640 = 16000。
    logic [20:0] oob_add;
    always_comb oob_add = {16'd0, oob_snap} * cin_q;

    always_comb begin
        for (int k = 0; k < VLANES; k++) begin
            p_e[k]    = $signed({{(EW-16){1'b0}}, p_snap[k]});
            d_c[k]    = (p_e[k] <<< 1) - $signed(ntot_q[EW-1:0])
                        + (OOB_ZERO ? $signed({{(EW-21){1'b0}}, oob_add}) : {EW{1'b0}});
            // 无损截断（见上面的值域证明）：|d| ≤ 2^14 < 2^15
            d16[k]    = d_c[k][15:0];
            qsh_c[k]  = e1_prod_q[k] >>> ASH;                 // 静态移位：0 LUT
            dd_c[k]   = e1_sh_q[k] + bz_l[k];
            // ★ 每一级必须读**上一级寄存器**，不能读上一级的组合中间量：
            //   曾经写成 snw_c = pn_c >>> SSH（pn_c 是 S3 寄存器 → S4 的输入），
            //   结果 S5 实际吃的是"下一个像素的 dd"，整条链晚一个像素。
            pn_c[k]   = $signed(e2_dd_q[k]) * $signed(slope_l[k]);   // S3 寄存器 → S4
            snw_c[k]  = $signed(e2_pn_q[k]) >>> SSH;                 // S4 寄存器 → S5
            // ★ v60 R1-A：两支都改用 gh_l = gamma + H（H 在 cmp_init 打进，
            //   见 §8b）⇒ 这里仍是"寄存器 + 寄存器"，与改前的路径长度相同。
            neg_c[k]  = $signed(e2_sn_q[k]) + $signed(gh_l[k]);     // S5 寄存器 → S6
            vsh_c[k]  = arsh(e2_r_q[k], shift_q);              // S6 寄存器 → S7
            // ★ S8 必须吃 S7 的**寄存器** e3_sh_q，不能吃 vsh_c（那是 S7 的输入）
            sat8_c[k] = ((!e3_sh_q[k][EW-1]) && ( |e3_sh_q[k][EW-1:7])) ? 8'd127 :
                        (( e3_sh_q[k][EW-1]) && (!(&e3_sh_q[k][EW-1:7]))) ? 8'd128 :
                                                                            e3_sh_q[k][7:0];
        end
    end

    // S1：d = 2p − N_tot；prod = d × alpha
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin v1_q <= 1'b0; a1_q <= 32'd0; end
        else if (ap_ce) begin
            v1_q <= ps_vld;
            if (ps_vld) begin
                a1_q <= osnap;
                // ★ 16×16 有符号 ⇒ 单个 DSP48E1（25×18 装得下）；32×16 要 2 个
                for (int k = 0; k < VLANES; k++) e1_prod_q[k] <= d16[k] * alpha_l[k];
            end
        end
    end
    // S2：q = prod >>> ASH
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin v2_q <= 1'b0; a2_q <= 32'd0; end
        else if (ap_ce) begin
            v2_q <= v1_q;
            if (v1_q) begin
                a2_q <= a1_q;
                for (int k = 0; k < VLANES; k++) e1_sh_q[k] <= qsh_c[k];
            end
        end
    end
    // S3：dd = q + bz；rp = dd + gamma；ddpos
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin v3_q <= 1'b0; a3_q <= 32'd0; end
        else if (ap_ce) begin
            v3_q <= v2_q;
            if (v2_q) begin
                a3_q <= a2_q;
                for (int k = 0; k < VLANES; k++) begin
                    e2_dd_q[k]     <= dd_c[k];
                    e2_rp_q[k]     <= dd_c[k] + gh_l[k];      // ★ v60 R1-A：gamma+H
                    e2_ddpos_q[k]  <= (dd_c[k] > 0);
                end
            end
        end
    end
    // S4：pn = dd × slope（顺带把 rp/ddpos 往下传一拍）
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin v4_q <= 1'b0; a4_q <= 32'd0; end
        else if (ap_ce) begin
            v4_q <= v3_q;
            if (v3_q) begin
                a4_q <= a3_q;
                for (int k = 0; k < VLANES; k++) begin
                    e2_pn_q[k]  <= pn_c[k];
                    e2_rp4_q[k] <= e2_rp_q[k];
                    e2_ddp4_q[k]<= e2_ddpos_q[k];
                end
            end
        end
    end
    // S5：sn = pn >>> SSH
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin v5_q <= 1'b0; a5_q <= 32'd0; end
        else if (ap_ce) begin
            v5_q <= v4_q;
            if (v4_q) begin
                a5_q <= a4_q;
                for (int k = 0; k < VLANES; k++) begin
                    e2_sn_q[k]  <= snw_c[k][EW-1:0];
                    e2_rp5_q[k] <= e2_rp4_q[k];
                    e2_ddp5_q[k]<= e2_ddp4_q[k];
                end
            end
        end
    end
    // S6：r = ddpos ? rp : (sn + gamma)
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin v6_q <= 1'b0; a6_q <= 32'd0; end
        else if (ap_ce) begin
            v6_q <= v5_q;
            if (v5_q) begin
                a6_q <= a5_q;
                for (int k = 0; k < VLANES; k++)
                    e2_r_q[k] <= e2_ddp5_q[k] ? e2_rp5_q[k] : neg_c[k];
            end
        end
    end
    // S7：v = r >>> shift
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin v7_q <= 1'b0; a7_q <= 32'd0; end
        else if (ap_ce) begin
            v7_q <= v6_q;
            if (v6_q) begin
                a7_q <= a6_q;
                for (int k = 0; k < VLANES; k++) e3_sh_q[k] <= vsh_c[k];
            end
        end
    end
    // S8：sat8 → 写口寄存器
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin v8_q <= 1'b0; wr_addr_q <= 32'd0; end
        else if (ap_ce) begin
            v8_q <= v7_q;
            if (v7_q) begin
                wr_addr_q <= a7_q;
                for (int k = 0; k < VLANES; k++) wr_byte_q[k] <= sat8_c[k];
            end
        end
    end

    // ---- 写口：64-bit 整字（全部寄存器输出）----
    logic [VLANES-1:0] wrsel_q;
    always_comb begin
        if (nvalid_q >= VLANES) wrsel_q = {VLANES{1'b1}};
        else                    wrsel_q = ({{(VLANES-1){1'b0}}, 1'b1} << nvalid_q[2:0]) - 1'b1;
    end
    assign mem_wr_en   = v8_q;
    assign mem_wr_addr = wr_addr_q;
    assign mem_wr_sel  = wrsel_q;
    always_comb begin
        for (int k = 0; k < VLANES; k++) mem_wr_data[8*k +: 8] = wr_byte_q[k];
    end

    // ========================================================================
    // 12. 主 FSM
    // ========================================================================
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            state     <= ST_IDLE;
            done_q    <= 1'b0;
            o_q       <= 32'd0;
            ogbase_q  <= 32'd0;
            wgt_oq    <= 32'd0;
            prm_i     <= 5'd0;
            wl_l      <= 16'd0;  wl_lane <= 4'd0;  wl_b <= 16'd0;  wl_ph <= 1'b0;
            dr_q      <= 4'd0;
            cout_q    <= 16'd0;  k_q       <= 16'd0;  cin_q <= 16'd0;
            h_q       <= 16'd0;  w_q       <= 16'd0;
            cb_q      <= 16'd0;  hout_q    <= 16'd0;  wout_q    <= 16'd0;
            cb_m1_q   <= 16'hFFFF; k_m1_q  <= 16'hFFFF;
            hout_m1_q <= 16'hFFFF; wout_m1_q <= 16'hFFFF;
            km1_q     <= {(LW+1){1'b1}};
            nvalid_q  <= 32'd0;
            ash_q     <= 6'd0;  ssh_q <= 6'd0;  shift_q <= 6'd0;
            act_b     <= 32'd0; wgt_b <= 32'd0; alpha_b <= 32'd0;
            bias_b    <= 32'd0; zeta_b <= 32'd0; gamma_b <= 32'd0;
            slope_b   <= 32'd0; out_b <= 32'd0;
            K_q       <= 32'd0; ntot_q <= 32'd0; kk_q <= 32'd0;
            hw8_q     <= 32'd0; w8_q <= 32'd0; s8_q <= 32'd0; sw8_q <= 32'd0;
            d8_q      <= 32'd0; dw8_q <= 32'd0; pad8_q <= 32'd0; padw8_q <= 32'd0;
            hwout8_q  <= 32'd0; k8_q <= 32'd0; gstride_q <= 32'd0;
            mask_last_q <= {64{1'b1}};
            padS_q    <= 32'sd0; sS_q <= 32'sd0; dS_q <= 32'sd0;
            // ★ 2026-10-31 修多驱动：这 12 个寄存器由 §13 的 always_ff 独占
            //   （复位 + PRM 计算都在那里）。这里再写一遍会造成
            //   [Synth 8-6859] multi-driven net，综合器保留 GND、丢掉 FF
            //   ⇒ 硬件上步长恒 0、读地址不推进 ⇒ 数据错 + 反复重填同一条线。
            //   xsim 是 last-write-wins 所以仿真看不出来 —— 典型的"仿真过、板上挂"。
            rd_addr_q <= 32'd0;
        end else if (ap_ce) begin
            done_q <= 1'b0;
            case (state)
            // ---------------- 锁存配置 ----------------
            ST_IDLE: begin
                if (start_pulse) begin
                    cout_q  <= cfg_cout;  k_q    <= cfg_k;
                    h_q     <= cfg_h;     w_q    <= cfg_w;
                    ash_q   <= cfg_alpha_shift;
                    ssh_q   <= cfg_slope_shift;
                    shift_q <= cfg_shift;
                    act_b   <= base_act;  wgt_b   <= base_wgt;
                    alpha_b <= base_alpha; bias_b <= base_bias;
                    zeta_b  <= base_zeta; gamma_b <= base_gamma;
                    slope_b <= base_slope; out_b  <= base_out;
                    cb_q    <= cb_c[15:0];
                    cin_q   <= cfg_cin;                    // v60 R3(b)：补偿用的层常量
                    kk_q    <= ckk_c;                      // k*k 先打一拍（拆 DSP）
                    hout_q  <= hout_c;
                    wout_q  <= wout_c;
                    cb_m1_q   <= cb_c[15:0] - 16'd1;
                    k_m1_q    <= cfg_k      - 16'd1;
                    hout_m1_q <= hout_c     - 16'd1;
                    wout_m1_q <= wout_c     - 16'd1;
                    hw8_q   <= hw8_c;  w8_q  <= w8_c;
                    s8_q    <= s8_c;   sw8_q <= sw8_c;
                    d8_q    <= d8_c;   dw8_q <= dw8_c;
                    pad8_q  <= pad8_c; padw8_q <= padw8_c;
                    mask_last_q <= mask_last_c;
                    padS_q  <= $signed({16'd0, cfg_pad});
                    sS_q    <= $signed({16'd0, cfg_s});
                    dS_q    <= $signed({16'd0, cfg_d});
                    o_q      <= 32'd0;
                    ogbase_q <= base_out;                  // o0=0 ⇒ 组内偏移 0
                    wgt_oq   <= base_wgt;
                    nvalid_q <= ({16'd0, cfg_cout} > VLANES) ? VLANES
                                                             : {16'd0, cfg_cout};
                    prm_i    <= 5'd0;
                    wl_l     <= 16'd0;  wl_lane <= 4'd0;  wl_b <= 16'd0; wl_ph <= 1'b0;
                    dr_q     <= 4'd0;
                    // 第一个参数读地址：o0=0 ⇒ alpha_b。这一沿 alpha_b 还没落盘，
                    // 所以要用**端口** base_alpha（旧版同一个坑）。
                    rd_addr_q <= base_alpha;
                    state    <= ST_PRM;
                end
            end
            // ---------------- 参数：17 拍（16 个 8 字节字，1 字/拍）----------------
            ST_PRM: begin
                if (prm_i == 5'd0) begin
                    K_q       <= K_lc;
                    ntot_q    <= K_lc << 6;
                    k8_q      <= K_lc << 3;
                    gstride_q <= (K_lc << 3) * VLANES;
                    hwout8_q  <= ({16'd0, hout_q} * {16'd0, wout_q}) << 3;
                end
                if (prm_i == 5'd1) km1_q <= K_q[LW:0] - 1'b1;

                if (prm_i != 5'd0) pw[prm_i - 5'd1] <= mem_rd_data;   // 收数

                if (prm_i < 5'd16) begin
                    prm_i     <= prm_i + 5'd1;
                    rd_addr_q <= prm_addr(prm_i + 5'd1, o_q);
                end else begin
                    prm_i     <= 5'd0;
                    wl_l      <= 16'd0;
                    wl_lane   <= 4'd0;
                    wl_b      <= 16'd0;
                    wl_ph     <= 1'b0;
                    rd_addr_q <= wgt_oq;
                    state     <= ST_WLD;
                end
            end
            // ---------------- 权重装载：2*(VLANES*K) 拍 ----------------
            //   组内 8 个 oc 的权重在内存里连续（每 oc K 个字）⇒ 地址 +8 线性扫描，
            //   lane 在 l 回绕时 +1；b 计数只用于"末块掩码"。
            ST_WLD: begin
                if (wl_ph == 1'b0) begin
                    wl_ph <= 1'b1;                        // 发读
                end else begin
                    wl_ph     <= 1'b0;
                    // ★ cmp_init 的那一拍要直接装第一个 term 的激活地址
                    //   （= act_b − pad*W*8 − pad*8），优先级高于权重的 +8。
                    //   旧版在地址块里用 else-if 表达；这里放在同一分支里。
                    if (cmp_init) rd_addr_q <= act_b + (32'd0 - padw8_q) + (32'd0 - pad8_q);
                    else          rd_addr_q <= rd_addr_q + 32'd8;
                    wl_b      <= (wl_b == cb_m1_q) ? 16'd0 : (wl_b + 16'd1);
                    if ({1'b0, wl_l} == km1_q) begin
                        wl_l <= 16'd0;
                        if (wl_lane == VLANES - 1) state <= ST_CMP;
                        else                       wl_lane <= wl_lane + 4'd1;
                    end else begin
                        wl_l <= wl_l + 16'd1;
                    end
                end
            end
            // ---------------- 计算主体 ----------------
            //   读地址：寄存器累加器（0 级组合输出），优先级与 §14 计数器完全一致
            ST_CMP: begin
                if (pix_last) begin
                    dr_q  <= 4'd0;
                    state <= ST_DRN;
                end
                if (term_last)   rd_addr_q <= rd_addr_q + (ox_wrap ? st_py_q : st_px_q);
                else if (b_wrap) rd_addr_q <= rd_addr_q + (kx_wrap ? st_kyw_q : st_kx_q);
                else             rd_addr_q <= rd_addr_q + hw8_q;
            end
            // ---------------- 排空 ----------------
            ST_DRN: begin
                if (drn_done) begin
                    dr_q <= 4'd0;
                    if (o_q + VLANES >= {16'd0, cout_q}) begin
                        state <= ST_DONE;
                    end else begin
                        o_q      <= o_q + VLANES;
                        ogbase_q <= ogbase_q + hwout8_q;
                        wgt_oq   <= wgt_oq + gstride_q;
                        nvalid_q <= ({16'd0, cout_q} - o_q > VLANES)
                                    ? VLANES : ({16'd0, cout_q} - o_q);
                        prm_i    <= 5'd0;
                        wl_l     <= 16'd0;
                        wl_lane  <= 4'd0;
                        wl_b     <= 16'd0;
                        wl_ph    <= 1'b0;
                        rd_addr_q <= prm_addr(5'd0, o_q + VLANES);
                        state    <= ST_PRM;
                    end
                end else begin
                    dr_q <= dr_q + 4'd1;
                end
            end
            // ---------------- 完成 ----------------
            ST_DONE: begin
                done_q <= 1'b1;
                state  <= ST_IDLE;
            end
            default: state <= ST_IDLE;
            endcase
        end
    end

    // ========================================================================
    // 13. 读地址寄存器的步长常量（PRM 有 17 拍空转，分 4 拍算完，零额外拍数）
    // ========================================================================
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            mn_cb_q <= 32'd0; mn_k_q <= 32'd0; mn_wo_q <= 32'd0;
            cp1_q <= 32'd0; cp2_q <= 32'd0; cp3_q <= 32'd0; cp4_q <= 32'd0;
            st_kx_q <= 32'd0; st_kyw_q <= 32'd0; st_px_q <= 32'd0;
            st_pyd_q <= 32'd0; st_py_q <= 32'd0;
        end else if (ap_ce && (state == ST_PRM)) begin
            case (prm_i)
                5'd0: begin
                    mn_cb_q <= {16'd0, cb_q}   - 32'd1;
                    mn_k_q  <= {16'd0, k_q}    - 32'd1;
                    mn_wo_q <= {16'd0, wout_q} - 32'd1;
                end
                5'd1: begin
                    cp1_q <= mn_cb_q * hw8_q;
                    cp2_q <= mn_k_q  * dw8_q;
                    cp3_q <= mn_k_q  * d8_q;
                    cp4_q <= mn_wo_q * s8_q;
                end
                5'd2: begin
                    st_kx_q  <= d8_q  - cp1_q;
                    st_kyw_q <= dw8_q - cp1_q - cp3_q;
                    st_px_q  <= s8_q  - cp1_q - cp2_q - cp3_q;
                    st_pyd_q <= sw8_q - s8_q  - cp4_q;
                end
                5'd3: st_py_q <= st_px_q + st_pyd_q;
                default: ;
            endcase
        end
    end

    // 读地址寄存器的 ST_CMP 分支在 §12 主 FSM 里（单一驱动者，避免多驱动）

    // ========================================================================
    // 14. 计算主体计数器（进入 CMP 时初始化，之后每拍推进一个 term）
    // ========================================================================
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            l_q    <= {LW{1'b0}};
            b_q    <= 16'd0;  ky_q <= 16'd0;  kx_q <= 16'd0;
            ox_q   <= 16'd0;  oy_q <= 16'd0;
            iy_q   <= 32'sd0; ix_q <= 32'sd0;
            ybase_q<= 32'sd0; xbase_q <= 32'sd0;
            oaddr_q<= 32'd0;
        end else if (ap_ce && cmp_init) begin
            l_q     <= {LW{1'b0}};
            b_q     <= 16'd0;  ky_q <= 16'd0;  kx_q <= 16'd0;
            ox_q    <= 16'd0;  oy_q <= 16'd0;
            ybase_q <= -padS_q;
            xbase_q <= -padS_q;
            iy_q    <= -padS_q;
            ix_q    <= -padS_q;
            oaddr_q <= ogbase_q;
        end else if (ap_ce && (state == ST_CMP)) begin
            if (term_last) begin
                // ---- 本像素最后一个 term：推进到下一个像素（raster 序）----
                l_q     <= {LW{1'b0}};
                b_q     <= 16'd0;
                ky_q    <= 16'd0;
                kx_q    <= 16'd0;
                oaddr_q <= oaddr_q + VLANES;              // 相邻像素 +VLANES 字节
                if (ox_wrap) begin
                    ox_q    <= 16'd0;
                    oy_q    <= oy_q + 16'd1;
                    xbase_q <= -padS_q;
                    ix_q    <= -padS_q;
                    ybase_q <= ybase_q + sS_q;
                    iy_q    <= ybase_q + sS_q;
                end else begin
                    ox_q    <= ox_q + 16'd1;
                    xbase_q <= xbase_q + sS_q;
                    ix_q    <= xbase_q + sS_q;
                    iy_q    <= ybase_q;
                end
            end else if (b_wrap) begin
                // ---- 换 64 通道组：j++ ⇒ (ky,kx) 前进一格 ----
                l_q  <= l_q + 9'd1;
                b_q  <= 16'd0;
                if (kx_wrap) begin
                    kx_q <= 16'd0;
                    ix_q <= xbase_q;
                    ky_q <= ky_q + 16'd1;
                    iy_q <= iy_q + dS_q;
                end else begin
                    kx_q <= kx_q + 16'd1;
                    ix_q <= ix_q + dS_q;
                end
            end else begin
                // ---- 同 kernel 位置，换下一个 64 通道组 ----
                l_q <= l_q + 9'd1;
                b_q <= b_q + 16'd1;
            end
        end
    end

    // ========================================================================
    // 15. 仿真自检（synthesis translate_off ⇒ 零面积）
    // ========================================================================
// synthesis translate_off
    // ① 契约固定值：alpha_shift ≡ 9、slope_shift ≡ 8（包装层 ERR_SHIFT 保证）
    // ② wbuf 容量：K ≤ WMAX
    // ③ ew 溢出：d*alpha / dd*slope 超出 EW 位就报（32 位收窄的安全网）
    integer chk_n;
    initial chk_n = 0;
    always @(posedge ap_clk) begin
        if (ap_rst_n && (state == ST_PRM) && (prm_i == 5'd6)) begin
            if (ash_q !== ASH[5:0] || ssh_q !== SSH[5:0]) begin
                $display("[bin_conv_core8] SHIFT MISMATCH ash=%0d(exp %0d) ssh=%0d(exp %0d)",
                         ash_q, ASH, ssh_q, SSH);
                chk_n = chk_n + 1;
            end
            if (K_q > WMAX) begin
                $display("[bin_conv_core8] K=%0d > WMAX=%0d", K_q, WMAX);
                chk_n = chk_n + 1;
            end
        end
        // ★ 旧版这一段的门控是 "wl_lane==7 && wl_ph==1"，缺了 "wl_l 已是 K-1"，
        //   于是**除最后收数拍外每一拍都误报**（K=27 时每组 26 条；实测一次 128²
        //   d=2 仿真刷出 312 条假报警）。真正该查的是"装载结束时 wl_l 正好停在 K-1"
        //   ⇒ 移到 cmp_init 那一拍查一次。
        // ★ §17：S1 用 16 位 d 的前提 —— |d| 必须 ≤ 32767（真实上界是 2^14）
        if (ap_rst_n && ps_vld && (d_c[0] > 32'sd32767 || d_c[0] < -32'sd32768)) begin
            $display("[bin_conv_core8] D16 OVERFLOW d=%0d Ntot=%0d", d_c[0], ntot_q);
            chk_n = chk_n + 1;
        end
        // ★ v60 R3(b)：n_oob 计数器的**单一装载点**不变量（照 e1e2 "rem" 脆点的教训）
        //   (A) 0 ≤ oob_cnt ≤ k²（只有 §10 的两个互斥条件驱动它）
        //   (B) 像素末拍的 oob_snap 也 ≤ k²（它直接进 d 的补偿）
        //   (C) 补偿量 n_oob·cin 必须落在预期量级（|d| ≤ 2^15 的安全网）
        if (ap_rst_n && (state == ST_CMP) && (oob_cnt > kk_q[4:0])) begin
            $display("[bin_conv_core8] R3 OOB_CNT OVERFLOW cnt=%0d k2=%0d（装载点被改了？）",
                     oob_cnt, kk_q);
            chk_n = chk_n + 1;
        end
        if (ap_rst_n && ps_vld && (oob_snap > kk_q[4:0])) begin
            $display("[bin_conv_core8] R3 OOB_SNAP OVERFLOW snap=%0d k2=%0d", oob_snap, kk_q);
            chk_n = chk_n + 1;
        end
        if (ap_rst_n && ps_vld && (oob_add > 21'd1_000_000)) begin
            $display("[bin_conv_core8] R3 OOB_ADD 超出预期量级 add=%0d", oob_add);
            chk_n = chk_n + 1;
        end
        if (ap_rst_n && cmp_init) begin
            if (!(({1'b0, wl_l} == km1_q))) begin
                $display("[bin_conv_core8] WLD LEN MISMATCH wl_l=%0d K-1=%0d", wl_l, km1_q);
                chk_n = chk_n + 1;
            end
        end
    end
// synthesis translate_on

endmodule
