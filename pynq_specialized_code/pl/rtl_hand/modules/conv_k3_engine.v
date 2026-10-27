// ============================================================================
//  conv_k3_engine.v —— 手写 3×3 卷积加速器 IP（k=3；stride 1/2；dilation 1/2/3）
//
//  契约：docs/rtl_hand/00_SPEC_接口契约.md §3（端口名/方向严格一致）
//  设计：docs/rtl_hand/conv_k3_engine_设计.md
//  骨架来源：modules/conv_k1_engine.v（权重 BRAM / 收尾 / 多 pass / 输出队列）
//            modules/conv_k5_engine.v（行缓冲 / 内联零填充 / AXI 读引擎）
//
//  ---------------------------------------------------------------------------
//  覆盖：全部 114 个 k=3 层
//    cin ≤ 192（24 个 C8 组，%8==0）；cout ≤ 192（24 pass）；H,W ≤ 320
//    stride ∈ {1,2}；dilation ∈ {1,2,3}；pad = dilation（same padding）
//    relu / shift 运行期
//  结构：
//    8 oc 车道 × 9 抽头 = 72 DSP，II=1；3 bank 行缓冲（BRAM，每 bank 1R1W）
//    9 抽头双缓冲（每个 (像素,组) 现读 9 字；stride/dilation 只体现为地址）
//    权重 576b × CIN_MAX（8 lane × 9 tap 打包）；多 pass 串行重装
//    收尾 2 拍；输出 1 个 C8 组 = 1 整 beat，按行 burst
//
//  分阶段落盘：
//    stage 1（本文件）：配置/FSM/行缓冲/抽头预取/填充/权重与 bias 装载
//                        §11 的 MAC 与输出先"零驱动"占位（不写总线）
//    stage 2：MAC 阵列（9→1 树 + 奇偶双链 + last 快照）
//    stage 3：收尾（bias/shift/relu/sat）+ 输出 FIFO + AXI4 写 burst
//
//  Verilog 铁律（踩过的坑）：
//   1. RAM 模板：读寄存器单一赋值源、不复位、不挂旁路（否则 FF + 256:1 mux）。
//   2. part-select 基址是自决定表达式（位宽取 max），乘法索引一律拼接加宽。
//   3. 每个 reg 只能有一个 always 驱动。
//   4. BD 顶层不能用 SV ⇒ 本文件 SV（同 k1/k5），适配层另写 Verilog-2001。
//   5. 8×8 乘要 `use_dsp="yes"`（否则推给 LUT）。
//   6. 逐拍相位表在 tools/model_k3_cycles.py 的 PHASES 里，改这里同步改那里
//      （k1 的 `wr_pop` 漏盖一拍 = 模型 PASS、RTL 错）。
// ============================================================================
`timescale 1ns / 1ps

module conv_k3_engine #(
    parameter int LANES    = 8,
    parameter int CIN_MAX  = 192,
    parameter int COUT_MAX = 192,
    parameter int W_MAX    = 320,
    parameter int H_MAX    = 320,
    parameter int CAP_B    = 4608
)(
    input  logic        ap_clk,
    input  logic        ap_rst_n,

    output logic        m_axi_in_arvalid,  input logic m_axi_in_arready,
    output logic [31:0] m_axi_in_araddr,   output logic [7:0]  m_axi_in_arlen,
    input  logic        m_axi_in_rvalid,   output logic m_axi_in_rready,
    input  logic [63:0] m_axi_in_rdata,    input logic m_axi_in_rlast,

    output logic        m_axi_w_arvalid,   input logic m_axi_w_arready,
    output logic [31:0] m_axi_w_araddr,    output logic [7:0]  m_axi_w_arlen,
    input  logic        m_axi_w_rvalid,    output logic m_axi_w_rready,
    input  logic [63:0] m_axi_w_rdata,     input logic m_axi_w_rlast,

    output logic        m_axi_b_arvalid,   input logic m_axi_b_arready,
    output logic [31:0] m_axi_b_araddr,    output logic [7:0]  m_axi_b_arlen,
    input  logic        m_axi_b_rvalid,    output logic m_axi_b_rready,
    input  logic [63:0] m_axi_b_rdata,     input logic m_axi_b_rlast,

    output logic        m_axi_out_awvalid, input logic m_axi_out_awready,
    output logic [31:0] m_axi_out_awaddr,  output logic [7:0]  m_axi_out_awlen,
    output logic        m_axi_out_wvalid,  input logic m_axi_out_wready,
    output logic [63:0] m_axi_out_wdata,   output logic [7:0]  m_axi_out_wstrb,
    output logic        m_axi_out_wlast,
    input  logic        m_axi_out_bvalid,  output logic m_axi_out_bready,

    input  logic [31:0] ctrl_in_addr,
    input  logic [31:0] ctrl_wgt_addr,
    input  logic [31:0] ctrl_bias_addr,
    input  logic [31:0] ctrl_out_addr,
    input  logic [31:0] ctrl_shift,
    input  logic [31:0] ctrl_cin,
    input  logic [31:0] ctrl_cout,
    input  logic [31:0] ctrl_h,
    input  logic [31:0] ctrl_w,
    input  logic [3:0]  ctrl_stride,   // 1/2（契约外新增，主控 2026-10-26 已批）
    input  logic [3:0]  ctrl_dil,      // 1/2/3（同上）
    input  logic        ctrl_relu,     // 运行期 ReLU（同 k1）
    input  logic        ctrl_start,
    output logic        ctrl_done,
    output logic        ctrl_busy,

    // ---- 只读诊断计数器（设计文档 §18.3：区分"DDR 供不上"与"逻辑慢"）----
    output logic [31:0] dbg_stall_fill,   // 行末等填充（DDR 供数）的拍数
    output logic [31:0] dbg_stall_mac     // 抽头/输出 FIFO 未就绪的拍数（应≈0）
);

    // ========================================================================
    // 0. 常量
    // ========================================================================
    localparam int NGW = 5;                  // 组号 0..23
    localparam int PW  = 5;                  // pass 0..23
    `ifdef K3_DUMP
    integer   dbg_cyc = 0;          // K3_DUMP 用：必须先声明后使用
    `endif
    localparam int AW  = 9;                  // 列 0..319
    localparam int AH  = 9;                  // 行 0..319
    localparam int BW  = 13;                 // 行缓冲地址 0..8191
    localparam int WW  = LANES * 9 * 8;      // 576 bit
    localparam int WB  = 72;                 // 一行 72 字节 = 9×LANES

    // ========================================================================
    // 1. 配置 / 派生
    // ========================================================================
    logic [31:0]    in_base_r, wgt_base_r, bias_base_r, out_base_r;
    logic [NGW-1:0] ngrp_r;
    logic [AW-1:0]  w_r;
    logic [AH-1:0]  h_r;
    logic [AW-1:0]  wout_r;
    logic [AH-1:0]  hout_r;
    logic [PW-1:0]  p_cnt_r, p_r;
    logic [4:0]     shift_r;
    logic           relu_r;
    logic [1:0]     stride_r, dil_r;
    logic           mode_pp;                 // 1 = 乒乓（d≠1），0 = 滚动（d=1）
    logic [BW-1:0]  ngrp_cols_r;             // NGRP × W
    logic [31:0]    hw8_r, w8_r;             // H*W*8 / W*8（填充地址的组/行步长）
    logic [8:0]     w_n_r;                   // W（9 位）——只为把行偏移乘法缩成 9×9
    // ⚠ 不要给这条加 use_dsp="no"：实测 LUT 版乘法**没省下 DSP（73→73）却掉了 5 MHz**
    //   （150→145）。DSP 预算是 k1 33 + k3 73 + k5 25 = 131 ≤ 160，余 29 个，够用。
    logic [17:0]    f_row_w_r;               // f_row * W（≤ 319*320 < 2^17），<<3 用拼接
    logic           f_row_wait;              // 作业参数已装、下一拍算 f_row*W（时序：让乘法输入全来自寄存器）
    // ⚠ 时序（2026-10-27，−0.224ns 那刀的根因）：`f_job_i` 的进位条件原来直接算
    //   `job_row(f_job_i) >= h_r`，形成 **f_job_i → i·d 加法 → 比较 → 使能 → f_job_i** 的自反馈环
    //   （5 个 CARRY4）。改成**两相**：先把行号算进寄存器 job_row_q（端点=寄存器），
    //   下一拍只做 9 位比较判定。顺带把 f_row 的第二次乘法也省掉（直接用 job_row_q）。
    logic           jr_wait;                 // job_row_q 已装、待判定
    logic [AH-1:0]  job_row_q;
    logic [31:0]    cin_x12_r;               // cin*12
    logic [7:0]     bias_nbeat_r;            // ceil(cout/2)
    logic [7:0]     cout_r;
    logic [31:0]    pass_off_r;              // p * H*W*8（写地址的 pass 偏移）
    logic [31:0]    row_off_r;               // y * W*8（**输入**行偏移，填充用）
    logic [31:0]    ow8_r, ohw8_r;           // Wout*8 / Hout*Wout*8（**输出**步长）
    logic [31:0]    orow_off_r;              // y * Wout*8（写地址的行偏移）

    // ⚠⚠ 拼接里的算术是**自决定位宽**（LRM：concat 的操作数 self-determined）！
    //    `{11'd0, (h_r[8:0] * w_r[8:0]), 3'b000}` 里乘法只按 9 位求值 ⇒ 640 截成 128 ⇒ hw8=1024。
    //  2026-10-27 实测症状：hw8 打出来是 1024（应为 5120），只有 H·W > 511 的层才暴露
    //  ⇒ **必须先把乘法赋给显式位宽的 wire，再拼接**。
    wire [17:0] hw_prod  = h_r[8:0] * w_r[8:0];          // H,W ≤ 320 ⇒ 18b
    wire [19:0] ohw_prod = {1'b0,hout_r[AW-1:0]} * {1'b0,wout_r[AW-1:0]};  // ≤320 ⇒ 20b

    // ⚠ 步长派生（hw8/w8/ow8/ohw8/ngrp_cols/cin_x12）**不在组合里算**，
    //   全部挪到 ST_CFG 用本地寄存器算（见主 FSM），避免乘法起点落在跨模块端口上
    //   （2026-10-27：−2.2ns，9 级逻辑但被走线+DSP 建立时间吃掉）。
    // pass 数 = ceil(cout/8)，**不是** cout>>3：
    //   cout=12 ⇒ 需要 2 个 pass（第 2 个只有 4 个有效 lane），
    //   用 >>3 会算出 1 ⇒ 最后一个 pass 根本不做 ⇒ 那 4 个通道全 0（BUG#16）
    wire [8:0]  p_cnt_c = ({1'b0, ctrl_cout[7:0]} + 9'd7) >> 3;
    // ⚠ 时序/LUT：原来是运行时除法 `(X-1)/ctrl_stride + 1`，综合器不知道 stride∈{1,2}
    //   ⇒ 实例化通用除法器（几百 LUT + 一条极长路径）。
    //   k3_cfg_check 已把 stride 限在 {1,2}，所以 (X-1)/S == (X-1) >> (S==2) ✓ 严格等价。
    // ⚠ 移位量是 **stride-1**，不能用 `stride[0]`：
    //   stride=1(01) 的 bit0 是 1、stride=2(10) 是 0 ⇒ 用 bit0 会**刚好反掉**
    //   （2026-10-26：这个反了之后 W=5 的用例 wout 算成 3 ⇒ 少算 2 列 ⇒
    //    冲激响应所在像素根本没被计算 ⇒ "响应完全消失"且总拍数变少）
    wire [15:0] stride_sh = {12'd0, ctrl_stride} - 16'd1;   // stride=1→0, 2→1
    wire [15:0] hout_c  = ((ctrl_h[15:0] - 16'd1) >> stride_sh) + 16'd1;
    wire [15:0] wout_c  = ((ctrl_w[15:0] - 16'd1) >> stride_sh) + 16'd1;
    wire [7:0]  bias_nb = (ctrl_cout[7:0] + 8'd1) >> 1;          // ceil(cout/2)

    // ========================================================================
    // 2. 主 FSM 状态 / 计数器
    // ========================================================================
    localparam logic [3:0] ST_IDLE   = 4'd0,
                           ST_CFG    = 4'd1,
                           ST_BIAS   = 4'd2,
                           ST_WLOAD  = 4'd3,
                           ST_PROLOG = 4'd4,
                           ST_ROW    = 4'd5,
                           ST_ROWEND = 4'd6,
                           ST_NEXT   = 4'd7,
                           ST_DRAIN  = 4'd8,
                           ST_DONE   = 4'd9;

    // 输出流水"已排空"标志（在收尾 always 里赋值，FSM 提前用它 ⇒ 晚 1 拍，只多等一拍）
    // ⚠ 加 tsum_r 那级后，最后一个 beat 的 E1 比原来晚 1 拍才用到 bias_lane；
    //   若此时换 pass 重装 bias_lane，最后那个像素会拿到**下一个 pass 的 bias**
    //   （bl_lane[0] 最先写 ⇒ 恰好只有 lane0 错；2026-10-26 真实层实测 1 字节错）。
    logic           e_pipe_empty_r;
`ifdef K3_DUMP
    integer         dbg_wait_n = 0;
`endif
    logic [1:0]     ob_inf;
    logic [3:0]     state;
    logic           start_q, start_pulse;
    logic [AH-1:0]  y_r;
    logic [AW-1:0]  x_r;
    logic [10:0]    sy_r;                    // S*y
    logic [10:0]    col_x_q;                 // S*x
    logic [NGW-1:0] icg_r;
    logic [2:0]     b_r;

    localparam int OB_D = 4;
    logic        push_vld;
    logic [7:0]  b_cnt;                   // 未回 B 的笔数
    assign ctrl_busy   = (state != ST_IDLE);
    assign start_pulse = ctrl_start & ~start_q;

    // 2026-10-26：从 §15 前移此处——主 FSM(§11) 在 918 行就要用它，
    // Verilog 要求先声明后使用。
    logic [2:0]  ob_cnt;
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) start_q <= 1'b0;
        else           start_q <= ctrl_start;
    end

    wire [NGW-1:0] ngrp_m1 = ngrp_r - 1'b1;

    // 权重读地址 = **下一个 ic**（配合"像素字节也打一拍"的流水线）
    //
    //   相位推导（BRAM 同步读：地址在拍 c 给，数据在拍 c+1 出）：
    //     · MAC 在拍 c 乘 pb_r(c) × w_row_q(c)，pb_r(c) = 拍 c-1 的 pb = X(c-1) 的字节
    //     · 所以 w_row_q(c) 必须是 weights(X(c-1))
    //     · w_row_q(c) = mem[w_rd_ic(c-1)] ⇒ w_rd_ic 在拍 c-1 必须等于 X(c-1)
    //     · w_rd_ic 在拍 c-1 的值是拍 c-2 边沿装进去的 ⇒ 装的必须是 X(c-2)+1 = ic_nxt
    //
    //   ⚠ 2026-10-26 主控转储实测的历史教训：上一版**没有** pb_r，乘法用组合 pb(c)，
    //     那时 `ic_nxt` 只够到"差一拍"，转储显示 MAC 在 ic 拍读到 ic-1 的权重。
    //     正解不是"地址再多提前一个"（那样第一拍会错），而是**把像素字节也打一拍**，
    //     让 pb_r 与 w_row_q 天然同相 —— 这也是 k1 的做法（p_a 与 w_row_q 同相）。
    wire [NGW-1:0] icg_nxt = (icg_r == ngrp_m1) ? {NGW{1'b0}} : (icg_r + 1'b1);
    wire [7:0]     ic_nxt  = (b_r == 3'd7) ? {icg_nxt, 3'd0}
                                           : {icg_r, (b_r + 3'd1)};

    // ========================================================================
    // 3. 小函数（全部放在使用它们之前）
    // ========================================================================
    // 槽 0..2 = 区域 0，槽 3..5 = 区域 1。
    // ⚠ 必须用 `s >= 3` 判区域，不能用 s[2]：槽 3 的 s[2] 仍是 0，
    //   会把乒乓 B 组的首槽算到区域 0、和槽 0 撞车（2026-10-26：d≠1 全挂）。
    function automatic [BW-1:0] sl_base(input [2:0] s);
        sl_base = (s >= 3'd3) ? ngrp_cols_r : {BW{1'b0}};
    endfunction

    function automatic [2:0] rot3(input [2:0] s, input [1:0] d);
        reg [2:0] t;
        begin
            t = s + {1'b0, d};
            if (t >= 3'd3) t = t - 3'd3;
            if (t >= 3'd3) t = t - 3'd3;
            rot3 = t;
        end
    endfunction

    // 滚动模式：y+1 行以后要灌的第一行 = S*y + D + 1
    function automatic [AH-1:0] ro_base();
        ro_base = sy_r + {1'b0, dil_r} + 11'd1;
    endfunction

    // 乒乓模式：y+1 的抽头行起点 = (y+1) - D
    function automatic [AH-1:0] pp_base();
        pp_base = sy_r + 11'd1 - {1'b0, dil_r};
    endfunction

    // 乒乓模式：第 i 个作业的行号 = base + i*D
    function automatic [AH-1:0] pp_row(input [2:0] i);
        reg [10:0] t;
        begin
            t = {2'd0, pp_base()} + {8'd0, i} * {9'd0, dil_r};
            pp_row = t[AH-1:0];
        end
    endfunction

    // 权重每 lane 的 beat 数 = cin*12/8 = cin*3/2（cin 是 8 的倍数 ⇒ 整除）
    function automatic [15:0] wgt_lane_beats();
        wgt_lane_beats = {4'd0, ngrp_r} * 16'd12;   // NGRP*8*3/2 = NGRP*12
    endfunction

    // ========================================================================
    // 4. 行缓冲：3 bank × (1 读 + 1 写)
    //    槽 s → bank (s mod 3)，槽内偏移 (s/3)*NGRP*W。
    //    ⚠ 读寄存器 lb_q* 只在本 always 里由 mem 赋值（不复位/无旁路/单源）。
    // ========================================================================
    (* ram_style = "block" *) logic [63:0] lb_mem0 [0:CAP_B-1];
    (* ram_style = "block" *) logic [63:0] lb_mem1 [0:CAP_B-1];
    (* ram_style = "block" *) logic [63:0] lb_mem2 [0:CAP_B-1];

    logic [BW-1:0] lb_raddr0, lb_raddr1, lb_raddr2;
    logic [63:0]   lb_q0, lb_q1, lb_q2;
    logic          lb_we;
    logic [1:0]    lb_wbank;
    logic [BW-1:0] lb_waddr;
    logic [63:0]   lb_wdata;

    always_ff @(posedge ap_clk) begin
        if (lb_we) begin
            if (lb_wbank == 2'd0)      lb_mem0[lb_waddr] <= lb_wdata;
            else if (lb_wbank == 2'd1) lb_mem1[lb_waddr] <= lb_wdata;
            else                       lb_mem2[lb_waddr] <= lb_wdata;
        end
        lb_q0 <= lb_mem0[lb_raddr0];
        lb_q1 <= lb_mem1[lb_raddr1];
        lb_q2 <= lb_mem2[lb_raddr2];
    end

    // ========================================================================
    // 5. 地址生成（列 cx±D / 行 Sy±D）
    // ========================================================================
    logic [2:0] ts0, ts1, ts2;

    wire signed [11:0] cx_s = $signed({1'b0, col_x_q});
    wire signed [11:0] c0_s = cx_s - $signed({10'd0, dil_r});
    wire signed [11:0] c1_s = cx_s;
    wire signed [11:0] c2_s = cx_s + $signed({10'd0, dil_r});
    wire c0_ok = (c0_s >= 12'sd0) && (c0_s < $signed({3'd0, w_r}));
    wire c1_ok = (c1_s >= 12'sd0) && (c1_s < $signed({3'd0, w_r}));
    wire c2_ok = (c2_s >= 12'sd0) && (c2_s < $signed({3'd0, w_r}));
    wire [AW-1:0] c0_a = c0_ok ? c0_s[AW-1:0] : {AW{1'b0}};
    wire [AW-1:0] c1_a = c1_ok ? c1_s[AW-1:0] : {AW{1'b0}};
    wire [AW-1:0] c2_a = c2_ok ? c2_s[AW-1:0] : {AW{1'b0}};

    wire signed [11:0] r0_s = $signed({1'b0, sy_r}) - $signed({10'd0, dil_r});
    wire signed [11:0] r1_s = $signed({1'b0, sy_r});
    wire signed [11:0] r2_s = $signed({1'b0, sy_r}) + $signed({10'd0, dil_r});
    wire r0_ok = (r0_s >= 12'sd0) && (r0_s < $signed({3'd0, h_r}));
    wire r1_ok = (r1_s >= 12'sd0) && (r1_s < $signed({3'd0, h_r}));
    wire r2_ok = (r2_s >= 12'sd0) && (r2_s < $signed({3'd0, h_r}));

    // 本段突发还能读多少拍 = min(256, W - 已读列数)
    //   ⚠ BUG#18：chunk 续读分支原来**直接回 F_AR 而没重算 chunk**，
    //   此时 f_chunk 已被减到 0 ⇒ arlen = 0-1 = 255 ⇒ 发 256 拍长突发，
    //   而引擎只等剩下的 64 拍 ⇒ TB 还在突发里、arready 恒低 ⇒ 互锁挂死。
    //   （preconv_1: W=320 ⇒ 唯一跨 chunk 边界的层，20M 拍看门狗超时）
    function automatic [8:0] chunk_of(input [9:0] col);
        logic [9:0] rem;
        rem = {1'b0, w_r} - col;                 // col < w_r 由调用点保证（否则会下溢）
        chunk_of = (rem > 10'd256) ? 9'd256 : rem[8:0];
    endfunction

    // 作业 i 的目标行 / 目标槽（前导与正常行共用一套公式，避免两处语义分叉）
    function automatic [AH-1:0] job_row(input [2:0] i);
        if (state == ST_PROLOG)
            // 前导要装的是 y=0 这一行自己要读的**已存在**抽头行：行 0, d, 2d...
            // ⚠ 不能用 pp_row()：那个公式是"行内给下一行补 (Sy+1-d)+i*d"，
            //   放在前导里会算成 -1/1/3 这种，行 0 读不到 ⇒ 只有 y=0 那一行错。
            //   （2026-10-26：k3_d2_tap00 恰好只错 8 字节 = y=0 的 5 像素 + 边界）
            job_row = mode_pp ? ({8'd0, i} * {9'd0, dil_r})
                              : {{(AH-3){1'b0}}, i};
        else
            job_row = mode_pp ? pp_row(i) : (ro_base() + {{(AH-3){1'b0}}, i});
    endfunction

    function automatic [2:0] job_slot(input [2:0] i);
        if (state == ST_PROLOG)
            job_slot = mode_pp ? ((i == 3'd0) ? 3'd1 : 3'd2) : {1'b0, i[1:0]};
        else
            // ⚠「当前在哪一组」必须用 `ts0 >= 3` 判，**不能用 ts0[2]**：
            //   槽 3 = 3'b011，bit2 = 0 ⇒ ts0[2] 会把 B 组的首槽判成 A 组
            //   （2026-10-26：d=2 的 ts 三元组卡在 (3,1,2)，ts0 永远翻不回 0）。
            job_slot = mode_pp ? (((ts0 >= 3'd3) ? 3'd0 : 3'd3) + i)
                               : ((i == 3'd0) ? ts0 :
                                  (i == 3'd1) ? ts1 : ts2);
    endfunction

    // ========================================================================
    // 6. 抽头预取：下一个 (像素,组) 的 9 个字（3 拍 × 3 bank）
    // ========================================================================
    // 抽头双缓冲：buf_sel 选"当前读哪一组"，预取永远写另一组
    logic [63:0] tapw_a [0:8];
    logic [63:0] tapw_b [0:8];
    logic [8:0]  tapv_a;                     // 9 个抽头的有效位（pad/越界 ⇒ 0）
    logic [8:0]  tapv_b;
    logic        buf_sel;
    logic [63:0] tw [0:8];                   // 当前组（2:1 选择）
    logic [8:0]  tv;
    logic        cur_ok, next_ok;
    logic [1:0]  pf_kx;                      // 正在捕获第几列（0/1/2）
    logic [2:0]  pf_stage;                   // 0=空闲 1=发 col0 地址 2/3/4=捕获 col0/1/2
    logic        pf_go;                      // 启动一次预取
    logic [10:0] pf_cx;
    logic [BW-1:0] pg_off;                   // = 预取组号 * W

    wire signed [11:0] pc_s  = $signed({1'b0, pf_cx});
    wire signed [11:0] pc0_s = pc_s - $signed({10'd0, dil_r});
    wire signed [11:0] pc2_s = pc_s + $signed({10'd0, dil_r});
    wire pc0_ok = (pc0_s >= 12'sd0) && (pc0_s < $signed({3'd0, w_r}));
    wire pc1_ok = (pc_s  >= 12'sd0) && (pc_s  < $signed({3'd0, w_r}));
    wire pc2_ok = (pc2_s >= 12'sd0) && (pc2_s < $signed({3'd0, w_r}));
    wire [AW-1:0] pc0_a = pc0_ok ? pc0_s[AW-1:0] : {AW{1'b0}};
    wire [AW-1:0] pc1_a = pc1_ok ? pc_s[AW-1:0]  : {AW{1'b0}};
    wire [AW-1:0] pc2_a = pc2_ok ? pc2_s[AW-1:0] : {AW{1'b0}};

    wire [AW-1:0] pf_col = (pf_kx == 2'd0) ? pc0_a :
                           (pf_kx == 2'd1) ? pc1_a : pc2_a;

    // 捕获列号 = stage-2（2 位减法自然回绕：0→2, 1→3→1, 2→2 ✓）
    wire [1:0] cap_k = pf_stage[1:0] - 2'd2;

    function automatic pc_ok_k(input [1:0] k);
        pc_ok_k = (k == 2'd0) ? pc0_ok :
                  (k == 2'd1) ? pc1_ok : pc2_ok;
    endfunction

    // 读地址（组合版：sl_base(槽) + 组偏移 + 列）
    assign lb_raddr0 = sl_base(ts0) + pg_off + pf_col;
    assign lb_raddr1 = sl_base(ts1) + pg_off + pf_col;
    assign lb_raddr2 = sl_base(ts2) + pg_off + pf_col;

    // ⚠ 槽号是**轮转**的：抽头行 k 的数据落在 bank(bank_of(ts_k))，不是 bank k。
    //   原来直接把 lb_q0/1/2 当成 tap 行 0/1/2 ⇒ 每换一行就取到隔壁槽的数据
    //   （2026-10-26 第 3 轮自查抓到的死数据错误）。
    //   代价：3 个 64-bit 的 3:1 mux（≈400 LUT），换来"槽随便轮转"。
    function automatic [1:0] bank_of(input [2:0] s);
        bank_of = (s >= 3'd3) ? (s - 3'd3) : s[1:0];
    endfunction

    wire [63:0] lb_bank [0:2];
    assign lb_bank[0] = lb_q0;
    assign lb_bank[1] = lb_q1;
    assign lb_bank[2] = lb_q2;

    wire [63:0] tq0 = lb_bank[bank_of(ts0)];      // = tap 行 0（S*y-D）
    wire [63:0] tq1 = lb_bank[bank_of(ts1)];      // = tap 行 1（S*y）
    wire [63:0] tq2 = lb_bank[bank_of(ts2)];      // = tap 行 2（S*y+D）

    // 本拍是否消费一个 ic（= MAC 推进）。抽头就绪即在算，没有别的 stall 条件；
    // 输出侧只靠 FIFO 深度吸收（4 深 vs 每 cin 拍一个 beat，永远不会满）。
    logic issue_en;
    // 组/像素边界必须等下一组抽头装好（next_ok），否则会读到只写了一半的缓冲。
    //   抽头预取要 5 拍，而一组 MAC 只有 8 拍：`cin=8`（单组）时预取在第 5 拍完成，
    //   恰好赶在像素边界前 ⇒ 全过；`cin>8`（多组）时下一组的预取是**前一组结束后**
    //   才启动的，8 拍不够 ⇒ 边界时 next_ok=0，原代码仍翻转 buf_sel ⇒ 第 2 组起
    //   读到半成品/上次残留。（2026-10-26：cin=16/24/40/192 全挂、cin=8 全过）
    assign issue_en = cur_ok && (state == ST_ROW)
                      && (next_ok || (b_r != 3'd7));

    // ========================================================================
    // 7. 权重 RAM：576 bit × CIN_MAX（行 = ic，行内字节 = 9*lane + tap）
    //    ⚠ 读寄存器 w_row_q 单一赋值源、无复位。
    // ========================================================================
    (* ram_style = "block" *) logic [WW-1:0] wgt_mem [0:CIN_MAX-1];
    logic [WW-1:0] w_row_q;
    logic [7:0]    w_rd_ic;                 // ic = 8*组号 + 字节号（0..191）
    logic          w_ld_we;
    logic [7:0]    w_wr_ic;
    logic [6:0]    w_ld_byte;
    logic [7:0]    w_ld_data;

    wire [WB-1:0] w_be = {WB{1'b1}} << w_ld_byte;    // 单字节写使能

    always_ff @(posedge ap_clk) begin
        if (w_ld_we) begin
            for (int c = 0; c < WB; c = c + 1) begin
                if (w_be[c])
                    wgt_mem[w_wr_ic][c*8 +: 8] <= w_ld_data;
            end
        end
        w_row_q <= wgt_mem[w_rd_ic];
    end

    // ========================================================================
    // 8. 偏置 RAM（COUT_MAX × 32 分布式）
    // ========================================================================
    (* ram_style = "distributed" *) logic signed [31:0] bias_mem [0:COUT_MAX-1];
    logic [7:0]  bi_waddr;
    logic        bi_we;
    logic [31:0] bi_wdata;

    // 每 pass 锁存 8 个 lane 的 bias（bias 只依赖 (pass,lane)，整个 pass 不变）
    logic [3:0]         bl_lane;
    logic signed [31:0] bias_lane [0:LANES-1];
    wire [7:0]          bias_oc = {p_r, 3'b000} + {4'd0, bl_lane[2:0]};

    always_ff @(posedge ap_clk) begin
        if (bi_we) bias_mem[bi_waddr] <= bi_wdata;
    end

    // ========================================================================
    // 9. 填充引擎（读侧 F_IDLE/F_AR/F_R + 写侧）
    // ========================================================================
    localparam logic [1:0] F_IDLE = 2'd0, F_AR = 2'd1, F_R = 2'd2;

    logic [1:0]     f_state;
    logic [2:0]     f_njob, f_job_i;
    logic [AH-1:0]  f_row;
    logic [2:0]     f_slot;
    logic           f_param_ok;              // 当前作业的 (行,槽) 已装好
    logic [NGW-1:0] f_rd_grp, f_wr_grp;
    logic [AW-1:0]  f_rd_col, f_wr_col;
    logic [31:0]    f_raddr;
    logic [31:0]    f_grp_off;               // 读侧 g*H*W*8
    logic [31:0]    f_wr_goff;               // 写侧 g*W
    logic [63:0]    f_fifo [0:15];
    logic [3:0]     f_wp, f_rp;
    logic [4:0]     f_cnt;
    logic [8:0]     f_chunk;
    logic           fill_idle;

    // 作业全部灌完：所有作业走完 + 读侧空闲 + FIFO 空
    assign fill_idle = (f_job_i == f_njob) && (f_state == F_IDLE) && (f_cnt == 5'd0);

    wire [BW-1:0] f_waddr  = sl_base(f_slot) + f_wr_goff[BW-1:0] + f_wr_col;
    // ⚠ 写 bank 必须用 bank_of()：乒乓 B 组是槽 3,4,5，f_slot[1:0] 会给出 3/0/1
    //   （槽 3 落到不存在的 bank3，槽 4/5 落到 bank0/1）⇒ 写错位置（BUG#12）。
    wire [1:0]    f_wbank  = bank_of(f_slot);
    // 滚动模式"写后写"闸门（设计文档 §4.3）：
    //   列 c 只有在读游标走过 2 列以后才可以被新行覆盖。
    // ⚠ 闸门**只对 ST_ROW 有意义**：前导(ST_PROLOG)/行末(ST_ROWEND)/换 pass 期间
    //   没有任何读者，写必须全开。原来只放开了 ST_ROWEND，于是 PROLOG 里
    //   col_x_q=0 ⇒ 闸门恒关 ⇒ 填充 FIFO 排不空 ⇒ fill_idle 永假 ⇒ 死锁
    //   （2026-10-26 主控首轮仿真抓到，信号证据 f_cnt 卡住、f_job_i 不前进）。
    wire          f_gate_ok = mode_pp || (state != ST_ROW)
                             || (({1'b0, f_wr_col} + 12'd2) <= col_x_q);
    // 读/写同拍握手（用于 FIFO 计数的**单点**更新，见下）
    wire f_rd_hs = (f_state == F_R) && m_axi_in_rvalid && m_axi_in_rready;
    wire f_wr_en = (f_cnt != 5'd0) && f_gate_ok && (f_job_i < f_njob);

    // 填充引擎空闲且读写指针都归零
    wire          f_fill_idle_now = (f_state == F_IDLE) && (f_cnt == 5'd0)
                                    && (f_wr_col == {AW{1'b0}});
    // 参数可装载（还没装）/ 作业可启动（已装好）
    // ⚠ 这两个条件必须分开：原来参数比 f_job_i 晚一拍落盘，填充引擎在同一拍看到
    //   f_job_valid 还是旧值 0，就把作业当成"行号越界"跳过（+1），于是
    //   "作业计数器前进了但一行还没写完" —— 2026-10-26 第 3 轮仿真抓到的状态不一致。
    // ⚠ 启动 AR 只要求 FIFO 排空 + 参数就绪，**不要求 wcol==0**：
    //   否则同一作业内第 2..N 个组要等整行写完才能发 AR（滚动模式写侧被闸门
    //   限速 ⇒ 填充会退化成"行末才开始读剩下的组"）。
    wire          f_param_load_ok = f_fill_idle_now && !f_param_ok;
    wire          f_job_start_ok  = (f_state == F_IDLE) && (f_cnt == 5'd0)
                                    && f_param_ok;

    // ========================================================================
    // 10. 权重装载 / bias 装载 FSM 状态
    // ========================================================================
    localparam logic [1:0] W_IDLE = 2'd0, W_AR = 2'd1, W_R = 2'd2, W_BYTE = 2'd3;
    logic [1:0]  wl_state;
    logic [3:0]  wl_lane;
    logic [31:0] wl_addr;
    logic [15:0] wl_beat_left;               // 本 lane 剩余 beat
    logic [15:0] wl_burst_left;              // 本笔 AR 剩余 beat（≤256）
    logic [63:0] wl_beat;
    logic [2:0]  wl_bi;
    logic [7:0]  wl_ic;
    logic [3:0]  wl_rem;
    logic        wl_done;

    localparam logic [1:0] B_IDLE = 2'd0, B_AR = 2'd1, B_R = 2'd2, B_BYTE = 2'd3;
    logic [1:0]  bl_state;
    logic [7:0]  bl_bi, bl_nb;
    logic [63:0] bl_beat;
    logic        bl_half;
    logic        bl_done;

    // ========================================================================
    // 11. 主 always（单一驱动源：所有 reg 只在这里赋值）
    // ========================================================================
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            state        <= ST_IDLE;
            ctrl_done    <= 1'b0;
            in_base_r    <= 32'd0;
            wgt_base_r   <= 32'd0;
            bias_base_r  <= 32'd0;
            out_base_r   <= 32'd0;
            ngrp_r       <= '0;
            w_r          <= '0;
            h_r          <= '0;
            wout_r       <= '0;
            hout_r       <= '0;
            p_cnt_r      <= '0;
            p_r          <= '0;
            shift_r      <= 5'd0;
            relu_r       <= 1'b1;
            stride_r     <= 2'd1;
            dil_r        <= 2'd1;
            mode_pp      <= 1'b0;
            ngrp_cols_r  <= '0;
            hw8_r        <= 32'd0;
            w8_r         <= 32'd0;
            w_n_r        <= 9'd0;
            f_row_w_r    <= 18'd0;
            f_row_wait   <= 1'b0;
            jr_wait      <= 1'b0;
            job_row_q    <= {AH{1'b0}};
            cin_x12_r    <= 32'd0;
            bias_nbeat_r <= 8'd1;
            cout_r       <= 8'd0;
            pass_off_r   <= 32'd0;
            row_off_r    <= 32'd0;
            ow8_r        <= 32'd0;
            ohw8_r       <= 32'd0;
            orow_off_r   <= 32'd0;
            y_r          <= '0;
            x_r          <= '0;
            sy_r         <= 11'd0;
            col_x_q      <= 11'd0;
            icg_r        <= '0;
            b_r          <= 3'd0;
            ts0          <= 3'd2;
            ts1          <= 3'd0;
            ts2          <= 3'd1;
            cur_ok       <= 1'b0;
            next_ok      <= 1'b0;
            buf_sel      <= 1'b0;
            pf_kx        <= 2'd0;
            pf_stage     <= 3'd0;
            pf_go        <= 1'b0;
            pf_cx        <= 11'd0;
            pg_off       <= '0;
            w_rd_ic      <= 8'd0;
            w_ld_we      <= 1'b0;
            w_wr_ic      <= 8'd0;
            w_ld_byte    <= 7'd0;
            w_ld_data    <= 8'd0;
            bi_we        <= 1'b0;
            bi_waddr     <= 8'd0;
            bi_wdata     <= 32'd0;
            f_state      <= F_IDLE;
            f_njob       <= 3'd0;
            f_job_i      <= 3'd0;
            f_row        <= '0;
            f_slot       <= 3'd0;
            f_param_ok   <= 1'b0;
            f_rd_grp     <= '0;
            f_wr_grp     <= '0;
            f_rd_col     <= '0;
            f_wr_col     <= '0;
            f_raddr      <= 32'd0;
            f_grp_off    <= 32'd0;
            f_wr_goff    <= 32'd0;
            f_wp         <= 4'd0;
            f_rp         <= 4'd0;
            f_cnt        <= 5'd0;
            f_chunk      <= 9'd0;
            wl_state     <= W_IDLE;
            wl_lane      <= 4'd0;
            wl_addr      <= 32'd0;      // 复位清 0；ST_IDLE 再装成 ctrl_wgt_addr
            wl_beat_left <= 16'd0;
            wl_burst_left<= 16'd0;
            wl_beat      <= 64'd0;
            wl_bi        <= 3'd0;
            wl_ic        <= 8'd0;
            wl_rem       <= 4'd0;
            wl_done      <= 1'b0;
            bl_state     <= B_IDLE;
            bl_bi        <= 8'd0;
            bl_nb        <= 8'd0;
            bl_beat      <= 64'd0;
            bl_half      <= 1'b0;
            bl_done      <= 1'b0;
        end else begin
            ctrl_done <= 1'b0;

            // ------------------------------------------------------------
            // 11.1 填充：读侧
            // ------------------------------------------------------------
            case (f_state)
                F_IDLE: begin
                    if ((f_job_i < f_njob) && f_job_start_ok) begin
                        f_raddr <= in_base_r + f_grp_off + {f_row_w_r, 3'b000};
                        f_chunk <= chunk_of({1'b0, f_rd_col});
                        f_state <= F_AR;
                    end
                    // 越界行不再在这里跳过：参数装载块（唯一天然入口）会直接 +1
                end
                F_AR: begin
                    if (m_axi_in_arvalid && m_axi_in_arready) f_state <= F_R;
                end
                F_R: begin
                    if (m_axi_in_rvalid && m_axi_in_rready) begin
                        f_fifo[f_wp] <= m_axi_in_rdata;
                        f_wp         <= f_wp + 4'd1;
                        f_raddr      <= f_raddr + 32'd8;
                        f_rd_col     <= f_rd_col + 1'b1;
                        f_chunk      <= f_chunk - 9'd1;
                        if (f_chunk == 9'd1) begin
                            if ((f_rd_col + 1'b1) >= w_r) begin
                                // 本组读完
                                f_rd_col <= {AW{1'b0}};
                                if (f_rd_grp == ngrp_m1) begin
                                    f_rd_grp  <= '0;
                                    f_grp_off <= 32'd0;
                                end else begin
                                    f_rd_grp  <= f_rd_grp + 1'b1;
                                    f_grp_off <= f_grp_off + hw8_r;
                                end
                                f_state <= F_IDLE;
                            end else begin
                                // 还有下一段：**必须重算 chunk**（此刻 f_chunk 已是 0），
                                // 否则 arlen 会变成 255 ⇒ 长突发 ⇒ 与 TB 互锁（BUG#18）
                                f_chunk <= chunk_of({1'b0, f_rd_col} + 10'd1);
                                f_state <= F_AR;
                            end
                        end
                    end
                end
                default: f_state <= F_IDLE;
            endcase

            // ------------------------------------------------------------
            // 11.2 填充：写侧（FIFO → 行缓冲）
            // ------------------------------------------------------------
            lb_we <= 1'b0;
            if ((f_cnt != 5'd0) && f_gate_ok && (f_job_i < f_njob)) begin
                lb_we    <= 1'b1;
                lb_wbank <= f_wbank;
                lb_waddr <= f_waddr;
                lb_wdata <= f_fifo[f_rp];
                f_rp     <= f_rp + 4'd1;
                f_wr_col <= f_wr_col + 1'b1;
                if ((f_wr_col + 1'b1) >= w_r) begin
                    f_wr_col  <= {AW{1'b0}};
                    f_wr_goff <= 32'd0;
                    if (f_wr_grp == ngrp_m1) begin
                        f_wr_grp   <= '0;
                        f_job_i    <= f_job_i + 3'd1;
                        // 作业完成 ⇒ 参数作废，由装载块给下一个作业装 (行,槽)
                        f_param_ok <= 1'b0;
                    end else begin
                        f_wr_grp  <= f_wr_grp + 1'b1;
                        f_wr_goff <= f_wr_goff + {19'd0, w_r};
                    end
                end
            end

            // ------------------------------------------------------------
            // 11.2b 填充 FIFO 计数（**单点更新**）
            //   ⚠ 原来读侧 +1、写侧 -1 各写一次，同拍既读又写时后者覆盖前者
            //     ⇒ f_cnt 系统性偏小 ⇒ 指针追尾 / 死锁（第 3 轮自查抓到）
            // ------------------------------------------------------------
            f_cnt <= f_cnt + {4'd0, f_rd_hs} - {4'd0, f_wr_en};

            // ------------------------------------------------------------
            // 11.3 抽头预取：3 拍读 3 列 × 3 bank，写进 tapw_next
            //      pf_stage=1/2/3 那三拍 lb_q* 正是 col0/1/2 的数据（同步读 1 拍）
            // ------------------------------------------------------------
            if (pf_go) begin
                pf_stage <= 3'd1;                    // 下一拍把 col0 的地址放上去
                pf_kx    <= 2'd0;
            end else if (pf_stage != 3'd0) begin
                // 同步读 1 拍延迟 ⇒ stage 2/3/4 才捕获 col0/1/2（cap_k = stage-2）
                // 写"另一组"（buf_sel 取反那一组）；pf_kx 是列号
                if (pf_stage >= 3'd2) begin
                    if (buf_sel) begin
                        tapw_a[0*3 + cap_k] <= tq0;
                        tapw_a[1*3 + cap_k] <= tq1;
                        tapw_a[2*3 + cap_k] <= tq2;
                        tapv_a[0*3 + cap_k] <= r0_ok && pc_ok_k(cap_k);
                        tapv_a[1*3 + cap_k] <= r1_ok && pc_ok_k(cap_k);
                        tapv_a[2*3 + cap_k] <= r2_ok && pc_ok_k(cap_k);
                    end else begin
                        tapw_b[0*3 + cap_k] <= tq0;
                        tapw_b[1*3 + cap_k] <= tq1;
                        tapw_b[2*3 + cap_k] <= tq2;
                        tapv_b[0*3 + cap_k] <= r0_ok && pc_ok_k(cap_k);
                        tapv_b[1*3 + cap_k] <= r1_ok && pc_ok_k(cap_k);
                        tapv_b[2*3 + cap_k] <= r2_ok && pc_ok_k(cap_k);
                    end
                end
                if (pf_stage == 3'd4) begin
                    pf_stage <= 3'd0;
                    if (!cur_ok) begin
                        cur_ok  <= 1'b1;              // 预热（第一个组）
                        buf_sel <= ~buf_sel;          // cur 指向刚装好的那组
                    end else begin
                        next_ok <= 1'b1;              // 下一组已就绪
                    end
                end else begin
                    pf_stage <= pf_stage + 3'd1;
                    if (pf_stage <= 3'd2) pf_kx <= pf_kx + 2'd1;
                end
            end

            // 权重 RAM 读地址：**提前一个 ic**（BRAM 同步读 1 拍）
            //   t    : 发出 ic(t+1) 的地址
            //   t+1  : w_row_q = ic(t+1) 的权重，同时 b_r 也走到 ic(t+1) ⇒ 对齐
            if (issue_en) w_rd_ic <= ic_nxt;         // 见上面 §13 的相位推导

            // ------------------------------------------------------------
            // 11.4 权重装载（PAD12 → 权重 RAM，单字节写）
            // ------------------------------------------------------------
            w_ld_we <= 1'b0;
            case (wl_state)
                W_IDLE: begin
                    // ⚠ BUG#17（安全）：oc 不存在的 lane **绝不能发 AXI 事务**。
                    //   权重容器按真实 cout 打包（cout*cin*12 字节），而本引擎按
                    //   补齐后的 ⌈cout/8⌉*8 个 oc 记账 ⇒ cout 非 8 倍数时，
                    //   尾 pass 的无效 lane 会读到 wgt_bytes 之外
                    //   （cout=12 时偏移 = 12*cin*12 = 0x900，恰好越过 w+2304）。
                    //   仿真里 TB 返回 0 看不出问题，**上板会 DECERR 或读进别的从设备数据**。
                    if ((state == ST_WLOAD) && (wl_lane < LANES[3:0]) && !wl_done) begin
                        if (({27'd0, p_r} * LANES[3:0] + wl_lane) >= {24'd0, cout_r}) begin
                            // 该 lane 的 oc 不存在 ⇒ 跳过（不读 DDR）；有效 lane 的
                            // 数据照旧，输出侧由 lok=0 把无效 oc 写成 0
                            if ((wl_lane + 4'd1) >= LANES[3:0]) wl_done <= 1'b1;
                            else begin
                                wl_lane <= wl_lane + 4'd1;
                                wl_addr <= wl_addr + cin_x12_r;   // 跳过也要前进一个容器
                            end
                        end else begin
                        // ⚠ 时序：原来这里是 `wgt_base + (p*8+lane)*cin_x12`，
                        //   一拍里"组号×8 + 乘法 + 32 位加"⇒ 关键路径 112 MHz（Slack -2.265ns）。
                        //   `wl_addr` 已改成**累加器**（容器在 DDR 里连续，相邻 lane 正好差
                        //   cin_x12）⇒ 这里不再算地址，直接用寄存器值。
                        wl_beat_left <= wgt_lane_beats();
                        wl_burst_left<= (wgt_lane_beats() > 16'd256)
                                        ? 16'd256 : wgt_lane_beats();
                        wl_ic        <= 8'd0;
                        wl_rem       <= 4'd0;
                        wl_bi        <= 3'd0;
                        wl_state     <= W_AR;
                        end
                    end
                end
                W_AR: begin
                    if (m_axi_w_arvalid && m_axi_w_arready) wl_state <= W_R;
                end
                W_R: begin
                    if (m_axi_w_rvalid && m_axi_w_rready) begin
                        wl_beat      <= m_axi_w_rdata;
                        wl_addr      <= wl_addr + 32'd8;
                        wl_bi         <= 3'd0;
                        wl_beat_left  <= wl_beat_left  - 16'd1;
                        wl_burst_left <= wl_burst_left - 16'd1;
                        wl_state      <= W_BYTE;
                    end
                end
                W_BYTE: begin
                    // 一拍 8 字节：每个字节推进 (ic, rem)；rem<9 才是有效抽头
                    if (wl_rem < 4'd9) begin
                        w_ld_we   <= 1'b1;
                        w_wr_ic   <= wl_ic;
                        // ⚠ 必须用完整的 4 位 rem：写成 wl_rem[2:0] 会把 rem=8
                        //   截成 0（tap8 的数据写进 tap0 的字节）——自查抓到的 bug
                        w_ld_byte <= {1'b0, ({2'd0, wl_lane} * 7'd9)} + {4'd0, wl_rem};
                        w_ld_data <= wl_beat[{wl_bi, 3'b000} +: 8];
                    end
                    if (wl_rem == 4'd11) begin
                        wl_rem <= 4'd0;
                        wl_ic  <= wl_ic + 8'd1;
                    end else begin
                        wl_rem <= wl_rem + 4'd1;
                    end
                    if (wl_bi == 3'd7) begin
                        wl_bi <= 3'd0;
                        if (wl_beat_left == 16'd0) begin
                            // 本 lane 装完
                            if ((wl_lane + 4'd1) >= LANES[3:0]) begin
                                wl_done  <= 1'b1;
                                wl_state <= W_IDLE;
                            end else begin
                                // ⚠ 不要在这里加 cin_x12：装这个 lane 的每拍已经 +8，
                                //   读完正好等于 +cin_x12（ngrp*12 拍 × 8 = cin*12）⇒ 已就位
                                wl_lane  <= wl_lane + 4'd1;
                                wl_state <= W_IDLE;
                            end
                        end else if (wl_burst_left == 16'd0) begin
                            // 本笔 burst 收完、本 lane 还没完 ⇒ 再发一笔（≤256）
                            wl_burst_left <= (wl_beat_left > 16'd256)
                                             ? 16'd256 : wl_beat_left;
                            wl_state      <= W_AR;
                        end else begin
                            wl_state <= W_R;      // 继续收下一拍
                        end
                    end else begin
                        wl_bi <= wl_bi + 3'd1;
                    end
                end
                default: wl_state <= W_IDLE;
            endcase

            // ------------------------------------------------------------
            // 11.4b 每 pass 锁存 8 个 lane 的 bias（ST_WLOAD 期间，8 拍读完）
            //      ⚠ 变量下标写寄存器组 ⇒ 8 路 demux，不是 RAM
            // ------------------------------------------------------------
            if ((state == ST_WLOAD) && (bl_lane < LANES[3:0])) begin
                bias_lane[bl_lane[2:0]] <= bias_mem[bias_oc];
                bl_lane <= bl_lane + 4'd1;
            end
            if (start_pulse || (state == ST_NEXT)) bl_lane <= 4'd0;

            // ------------------------------------------------------------
            // 11.5 bias 装载
            // ------------------------------------------------------------
            bi_we <= 1'b0;
            case (bl_state)
                B_IDLE: begin
                    if ((state == ST_BIAS) && !bl_done) begin
                        bl_bi    <= 8'd0;
                        bl_half  <= 1'b0;
                        bl_nb    <= bias_nbeat_r;
                        bl_state <= B_AR;
                    end
                end
                B_AR: begin
                    if (m_axi_b_arvalid && m_axi_b_arready) bl_state <= B_R;
                end
                B_R: begin
                    if (m_axi_b_rvalid && m_axi_b_rready) begin
                        bl_beat  <= m_axi_b_rdata;
                        bl_half  <= 1'b0;
                        bl_state <= B_BYTE;
                    end
                end
                B_BYTE: begin
                    bi_we    <= 1'b1;
                    bi_waddr <= {bl_bi[6:0], 1'b0} + {7'd0, bl_half};
                    bi_wdata <= bl_half ? bl_beat[63:32] : bl_beat[31:0];
                    if (bl_half) begin
                        bl_half <= 1'b0;
                        if (bl_bi >= (bl_nb - 8'd1)) begin
                            bl_done  <= 1'b1;
                            bl_state <= B_IDLE;
                        end else begin
                            bl_bi    <= bl_bi + 8'd1;
                            bl_state <= B_R;
                        end
                    end else begin
                        bl_half <= 1'b1;
                    end
                end
                default: bl_state <= B_IDLE;
            endcase

            // ------------------------------------------------------------
            // 11.6 主 FSM
            // ------------------------------------------------------------
            case (state)
                ST_IDLE: begin
                    if (start_pulse) begin
                        in_base_r    <= ctrl_in_addr;
                        wgt_base_r   <= ctrl_wgt_addr;
                        wl_addr      <= ctrl_wgt_addr;   // 权重地址累加器的起点
                        wl_lane      <= 4'd0;
                        bias_base_r  <= ctrl_bias_addr;
                        out_base_r   <= ctrl_out_addr;
                        ngrp_r       <= ctrl_cin[NGW+2:3];
                        w_r          <= ctrl_w[AW-1:0];
                        h_r          <= ctrl_h[AH-1:0];
                        wout_r       <= wout_c[AW-1:0];
                        hout_r       <= hout_c[AH-1:0];
                        p_cnt_r      <= (p_cnt_c[PW-1:0] == {PW{1'b0}})
                                        ? {{(PW-1){1'b0}}, 1'b1}   // cout=0 防御（配置检查会拦）
                                        : p_cnt_c[PW-1:0];
                        shift_r      <= ctrl_shift[4:0];
                        relu_r       <= ctrl_relu;
                        stride_r     <= ctrl_stride[1:0];
                        dil_r        <= ctrl_dil[1:0];
                        mode_pp      <= (ctrl_dil[1:0] != 2'd1);
                        // ⚠ 下面这些"乘法派生量"**不在这里算**（2026-10-27 时序）：
                        //   它们原来的输入是跨模块端口 ctrl_h/ctrl_w/ctrl_cin，
                        //   乘法起点就在包装层寄存器上，走线 + DSP 建立时间吃掉 −2.2ns。
                        //   挪到 ST_CFG 用本地寄存器（h_r/w_r/hout_r/wout_r/ngrp_r）算 ——
                        //   ST_CFG 与首次使用（ST_PROLOG）之间还隔着 ST_BIAS/ST_WLOAD 几十拍 ⇒ 天然够。
                        w_n_r        <= ctrl_w[AW-1:0];
                        bias_nbeat_r <= bias_nb;
                        cout_r       <= ctrl_cout[7:0];
                        pass_off_r   <= 32'd0;
                        row_off_r    <= 32'd0;
                        orow_off_r   <= 32'd0;
                        // ow8_r/ohw8_r 也在 ST_CFG 里算（见上）
                        p_r          <= '0;
                        y_r          <= '0;
                        x_r          <= '0;
                        sy_r         <= 11'd0;
                        col_x_q      <= 11'd0;
                        icg_r        <= '0;
                        b_r          <= 3'd0;
                        ts0          <= 3'd2;
                        ts1          <= 3'd0;
                        ts2          <= 3'd1;
                        cur_ok       <= 1'b0;
                        next_ok      <= 1'b0;
                        buf_sel      <= 1'b0;
                        pf_stage     <= 3'd0;
                        pf_go        <= 1'b0;
                        pf_kx        <= 2'd0;
                        pf_cx        <= 11'd0;
                        pg_off       <= '0;
                        f_state      <= F_IDLE;
                        f_njob       <= 3'd0;
                        f_job_i      <= 3'd0;
                        f_param_ok   <= 1'b0;
                        f_rd_grp     <= '0;
                        f_wr_grp     <= '0;
                        f_rd_col     <= '0;
                        f_wr_col     <= '0;
                        f_grp_off    <= 32'd0;
                        f_wr_goff    <= 32'd0;
                        f_cnt        <= 5'd0;
                        f_wp         <= 4'd0;
                        f_rp         <= 4'd0;
                        wl_state     <= W_IDLE;
                        wl_lane      <= 4'd0;
                        wl_done      <= 1'b0;
                        bl_state     <= B_IDLE;
                        bl_done      <= 1'b0;
                        state        <= ST_CFG;
                    end
                end

                ST_CFG: begin
                    // 步长派生：**输入全部是本地寄存器**，输出也是本地寄存器
                    //   H,W ≤ 320(9b) ⇒ H·W ≤ 18b；Hout,Wout ≤ 320(10b) ⇒ ≤ 20b
                    hw8_r       <= {11'd0, hw_prod, 3'b000};      // 18b+3b=21b，补 11 位 = 32 ✓
                    w8_r        <= {20'd0, w_r[AW-1:0]} << 3;
                    ow8_r       <= {20'd0, wout_r[AW-1:0]} << 3;
                    ohw8_r      <= {9'd0, ohw_prod, 3'b000};      // 20b+3b=23b，补 9 位 = 32 ✓
                    ngrp_cols_r <= {5'd0, ngrp_r} * {4'd0, w_r[AW-1:0]};   // ≤ 24×320 = 13b
                    cin_x12_r   <= {5'd0, ngrp_r} * 32'd96;                // cin*12 = (cin/8)*96
                    state       <= ST_BIAS;
                end

                ST_BIAS: if (bl_done) state <= ST_WLOAD;

                ST_WLOAD: begin
                    // 前导作业数恒为 2：滚动(d=1) 装行 0,1；乒乓装行 0,d
                    //（原来给 d+1，d=2/3 时会多排"行 2d/3d"，那两个槽不存在）
                    f_njob  <= 3'd2;
                    f_job_i <= 3'd0;
                    if (wl_done) state <= ST_PROLOG;
                end

                // 前导：滚动 行 i→槽 i；乒乓 行0→槽1、行D→槽2
                ST_PROLOG: begin
                    if (fill_idle && (f_njob != 3'd0)
                        && (f_job_i >= f_njob)) begin
                        y_r     <= '0;
                        x_r     <= '0;
                        sy_r    <= 11'd0;
                        col_x_q <= 11'd0;
                        // 槽号初值：滚动 {2,0,1}（y=0 的 -1/0/+1 号槽），乒乓 {0,1,2}
                        ts0     <= mode_pp ? 3'd0 : 3'd2;
                        ts1     <= mode_pp ? 3'd1 : 3'd0;
                        ts2     <= mode_pp ? 3'd2 : 3'd1;
                        icg_r   <= '0;
                        b_r     <= 3'd0;
                        f_njob  <= mode_pp ? 3'd3 : {1'b0, stride_r};
                        f_job_i <= 3'd0;
                        state   <= ST_ROW;
                    end
                end

                ST_ROW: begin
                    // ⚠ 用 issue_en（内含 next_ok 边界条件）：边界没就绪就整拍停住，
                    //   计数器不动、不会重复发射同一个 ic
                    if (issue_en) begin
                        if (b_r == 3'd7) begin
                            b_r <= 3'd0;
                            if (icg_r == ngrp_m1) begin
                                icg_r <= '0;
                                if (x_r == (wout_r - 1'b1)) begin
                                    state <= ST_ROWEND;
                                end else begin
                                    x_r     <= x_r + 1'b1;
                                    col_x_q <= col_x_q + {9'd0, stride_r};
                                end
                            end else begin
                                icg_r   <= icg_r + 1'b1;
                            end
                            next_ok <= 1'b0;          // 组边界：刚装好的那组变成 cur
                            buf_sel <= ~buf_sel;
                        end else begin
                            b_r <= b_r + 3'd1;
                        end
                    end
                end

                ST_ROWEND: begin
                    if ((f_job_i >= f_njob) && fill_idle) begin
                        if (mode_pp) begin
                            // ⚠ 三条必须用**同一个判据**（ts0 的组别）同时翻转，
                            //   且判据是 `>= 3` 而不是 bit2（槽 3 的 bit2 = 0）。
                            //   （2026-10-26：写成 ts_x[2] 后 ts0 卡在 3、ts1/ts2 翻 →
                            //     三元组变成 (3,1,2)，乒乓两组混在一起，d=2 早一行响应）
                            ts0 <= (ts0 >= 3'd3) ? 3'd0 : 3'd3;
                            ts1 <= (ts0 >= 3'd3) ? 3'd1 : 3'd4;
                            ts2 <= (ts0 >= 3'd3) ? 3'd2 : 3'd5;
                        end else begin
                            ts0 <= rot3(ts0, stride_r);
                            ts1 <= rot3(ts1, stride_r);
                            ts2 <= rot3(ts2, stride_r);
                        end
`ifdef K3_DUMP
                        $display("[K3-ROW] y=%0d done | ts=%0d,%0d,%0d sy=%0d mode_pp=%b f_njob=%0d",
                                 y_r, ts0, ts1, ts2, sy_r, mode_pp, f_njob);
`endif
                        sy_r    <= sy_r + {9'd0, stride_r};
                        x_r     <= '0;
                        col_x_q <= 11'd0;
                        y_r     <= y_r + 1'b1;
                        row_off_r  <= row_off_r + w8_r;      // 输入行步长（填充用）
                        orow_off_r <= orow_off_r + ow8_r;    // 输出行步长（s≠1 时二者不同）
                        // 换行后抽头缓冲失效：下一行第 0 组的 9 个字要重新装
                        //（最后一像素预取的是"下一像素"，对下一行来说是垃圾）
                        cur_ok  <= 1'b0;
                        next_ok <= 1'b0;
                        // ⚠ 最后一行不要再排"下一行的填充作业"：否则 f_job_i(0) !=
                        //   f_njob(S/3) 永远成立不了 ⇒ fill_idle 永假 ⇒ ST_DRAIN 卡死
                        //   （2026-10-26 自查抓到的同族死锁 #2）
                        if (y_r == (hout_r - 1'b1)) begin
                            f_njob  <= 3'd0;
                            f_job_i <= 3'd0;
                        end else begin
                            f_njob  <= mode_pp ? 3'd3 : {1'b0, stride_r};
                            f_job_i <= 3'd0;
                        end
                        state   <= (y_r == (hout_r - 1'b1)) ? ST_NEXT : ST_ROW;
                    end
                end

                ST_NEXT: begin
                    if (p_r == (p_cnt_r - 1'b1)) begin
                        state <= ST_DRAIN;
                    end else if (!e_pipe_empty_r) begin
                        // 等最后一个 beat 走完 E1/E2（它还要读 bias_lane）再重装权重/bias
                    end else begin
                        p_r        <= p_r + 1'b1;
                        y_r        <= '0;
                        x_r        <= '0;
                        sy_r       <= 11'd0;
                        col_x_q    <= 11'd0;
                        row_off_r  <= 32'd0;
                        orow_off_r <= 32'd0;                 // ⚠ BUG#15：换 pass 必须清
                        pass_off_r <= pass_off_r + ohw8_r;   // 输出整图步长，不是输入
                        cur_ok     <= 1'b0;
                        next_ok    <= 1'b0;
                        // 前导作业数恒为 2（滚动 d=1 装行 0,1；乒乓装行 0,d）。
                        // 原来写成 dil+1（d≥2 时会多排"行 2d/3d"两个不存在的槽），
                        // 与 ST_WLOAD 里那处是同一条（BUG#10 的换 pass 版本）
                        f_njob  <= 3'd2;
                        f_job_i <= 3'd0;
                        ts0     <= mode_pp ? 3'd0 : 3'd2;
                        ts1     <= mode_pp ? 3'd1 : 3'd0;
                        ts2     <= mode_pp ? 3'd2 : 3'd1;
                        wl_lane <= 4'd0;
                        wl_done <= 1'b0;
                        wl_state<= W_IDLE;
                        state   <= ST_WLOAD;
                    end
                end

                ST_DRAIN: begin
                    if (fill_idle && (ob_cnt == 3'd0) && (b_cnt == 8'd0) && e_pipe_empty_r)
                        state <= ST_DONE;
                end

                ST_DONE: begin
                    ctrl_done <= 1'b1;
                    state     <= ST_IDLE;
                end

                default: state <= ST_IDLE;
            endcase

            // ------------------------------------------------------------
            // 11.6b 填充作业参数（行内并发）：在 ST_ROW / ST_ROWEND 逐作业给
            //      (行号, 目标槽)。滚动=行 S·y+D+1+i →槽 ts_i；乒乓=行(y+1)-D+i·D →槽另一组
            //      ⚠ 这是"填充与计算重叠"的关键：作业在**行开始时**就排队，
            //        写指针由 f_gate_ok 按列跟随读游标（滚动模式）
            // ------------------------------------------------------------
            if (f_param_load_ok && (f_job_i < f_njob)
                && ((state == ST_PROLOG) || (state == ST_ROW)
                    || (state == ST_ROWEND))) begin
`ifdef K3_DUMP
                $display("[K3-FILL] st=%0d job=%0d row=%0d slot=%0d | ts=%0d,%0d,%0d sy=%0d ppb=%0d",
                         state, f_job_i, $signed(job_row(f_job_i)), job_slot(f_job_i),
                         ts0, ts1, ts2, sy_r, $signed(pp_base()));
`endif
                if (jr_wait) begin
                    // 第 2 拍：只做 9 位比较（端点不会回到 f_job_i 的组合环）
                    jr_wait <= 1'b0;
                    if (job_row_q >= h_r) begin
                        f_job_i <= f_job_i + 3'd1;      // 越界：跳过（不读 DDR）
                    end else begin
                        f_row      <= job_row_q;        // 直接用寄存好的行号，省一次乘法
                        f_slot     <= job_slot(f_job_i);
                        f_row_wait <= 1'b1;
                    end
                end else if (f_row_wait) begin
                    // 第 3 拍：两个操作数都是寄存器 ⇒ 只剩 9×9 乘法
                    f_row_w_r  <= {9'd0, f_row} * w_n_r;
                    f_row_wait <= 1'b0;
                    f_param_ok <= 1'b1;
                end else begin
                    // 第 1 拍：只把行号算进寄存器（i·d 加法链的端点=寄存器）
                    job_row_q  <= job_row(f_job_i);
                    jr_wait    <= 1'b1;
                end
            end

            // ------------------------------------------------------------
            // 11.7 预取启动：cur_ok=0 时预热第一组；否则预取下一组
            // ------------------------------------------------------------
            pf_go <= 1'b0;
            if ((state == ST_ROW) && (pf_stage == 3'd0) && !next_ok) begin
                pf_go <= 1'b1;
                if (!cur_ok) begin
                    pf_cx  <= col_x_q;               // 预热：本像素第 0 组
                    pg_off <= '0;
                end else if (icg_r == ngrp_m1) begin
                    // 预取永远是"比当前正在装的那一组再往后一组"（1 级前瞻），
                    // 所以这一组会跨到**下一像素** ⇒ 列号要 +stride。
                    // 时序核对（关键，别再来回改）：
                    //   · ngrp=1：像素内唯一的边界就是像素边界，进这里时 col_x_q
                    //     已经是本像素列（边界那拍刚加过）⇒ +stride 正好是"再下一像素"✓
                    //   · ngrp=2：本分支在 g0 的边界触发（此时像素还没结束、
                    //     col_x_q 未变）⇒ +stride 正好是"下一像素的 g0" ✓
                    //   （2026-10-26：我曾把它改成 `col_x_q`，"不跳"⇒ 每行只有 x=0 对）
                    pf_cx  <= col_x_q + {9'd0, stride_r};
                    pg_off <= '0;                    // 换像素 ⇒ 组偏移归零
                end else begin
                    // BUG#11：原来是 `pg_off <= pg_off + w_r`（累加）。
                    //   累加只在"像素内 group 递增"时成立；一旦跨过像素边界
                    //   （icg_r 已回 0、但 pg_off 还停在旧值），就会继续 +W ⇒
                    //   第 2 个像素起读到 group 2/3… 的越界地址 ⇒ 抽头全 0 ⇒ 输出全 0。
                    //   改成**无状态**直接算"被预取那一组"的偏移 = (icg_r+1)*W。
                    pf_cx  <= col_x_q;
                    pg_off <= ({3'd0, icg_r} + 3'd1) * w_r;
                end
            end
        end
    end

    // ========================================================================
    // 11b. 诊断计数器（只增不清，层与层之间靠 start 清零）
    //   stall_fill：行末等填充灌完（= DDR 供数不足，裁决 3 的判据）
    //   stall_mac ：抽头预取没就绪 / 输出 FIFO 满（内层掉 II，应≈0）
    // ========================================================================
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            dbg_stall_fill <= 32'd0;
            dbg_stall_mac  <= 32'd0;
        end else if (start_pulse) begin
            dbg_stall_fill <= 32'd0;
            dbg_stall_mac  <= 32'd0;
        end else begin
            if ((state == ST_ROWEND) && !fill_idle)
                dbg_stall_fill <= dbg_stall_fill + 32'd1;
            if (((state == ST_ROW) && !cur_ok) ||
                (push_vld && (ob_cnt >= OB_D[2:0])))
                dbg_stall_mac <= dbg_stall_mac + 32'd1;
        end
    end

    // ========================================================================
    // 12. AXI4 读三路
    // ========================================================================
    assign m_axi_in_arvalid = (f_state == F_AR);
    assign m_axi_in_araddr  = f_raddr;
    assign m_axi_in_arlen   = f_chunk[7:0] - 8'd1;
    assign m_axi_in_rready  = (f_state == F_R) && (f_cnt < 5'd16);

    assign m_axi_w_arvalid  = (wl_state == W_AR);
    assign m_axi_w_araddr   = wl_addr;
    assign m_axi_w_arlen    = (wl_burst_left > 16'd256) ? 8'd255
                                                        : (wl_burst_left[7:0] - 8'd1);
    assign m_axi_w_rready   = (wl_state == W_R);

    assign m_axi_b_arvalid  = (bl_state == B_AR);
    assign m_axi_b_araddr   = bias_base_r;
    assign m_axi_b_arlen    = bias_nbeat_r - 8'd1;
    assign m_axi_b_rready   = (bl_state == B_R);

    // ========================================================================
    // 13. MAC 阵列（stage 2）：8 lane × 9 抽头 = 72 DSP，II=1
    //
    //  相位（与 tools/model_k3_cycles.py 的 PHASES 一致）：
    //    t   : pb[9]（由 tapw_cur 按 b_r 选字节）+ w_row_q（72 字节）
    //    t+1 : prod[lane][tap] 打拍
    //    t+1 : 9→1 加法树（组合，20 位）
    //    t+2 : acc_e/acc_o 累加（奇偶双链）；last_b 那拍 sum27 快照并清零
    // ========================================================================
    logic [7:0]  pb [0:8];                   // 9 个抽头字节（pad ⇒ 0，组合）
    logic [7:0]  pb_r [0:8];                 // 打一拍：与 w_row_q 同相
    logic        v_a, v_b, v_c;              // valid 链（issue 延 1/2/3 拍）
    logic        par_a, par_b, par_c;        // ic[0]（奇偶链选择）
    logic        last_a, last_b, last_c;     // 本像素最后一个 ic
    logic [PW-1:0] p_a, p_b, p_c;            // pass 号流水（供收尾写地址）
    logic [AW-1:0] x_a, x_b, x_c;            // 列号流水
    logic [AH-1:0] y_a, y_b, y_c;            // 行号流水
    logic [31:0]   oad_a, oad_b, oad_c;      // 输出字节地址流水（issue 时算好）
    wire [31:0]    oad_now = out_base_r + pass_off_r + orow_off_r
                             + ({20'd0, x_r} << 3);

    // 9 个抽头字节：pad/越界位置强制 0（内联零填充）
    always_comb begin
        for (int t = 0; t < 9; t = t + 1) begin
            tw[t] = buf_sel ? tapw_b[t] : tapw_a[t];
            tv[t] = buf_sel ? tapv_b[t] : tapv_a[t];
            pb[t] = tv[t] ? tw[t][{b_r, 3'b000} +: 8] : 8'd0;
        end
    end

    // 流水控制（寄存器，3 级）
    //
    //  拍 t   : issue（pb 组合出 9 个字节 + 算好 oad）→ 打拍进 pb_r
    //  拍 t+1 : pb_r 与 w_row_q **同相**（w_rd_ic 给 ic_nxt）
    //           → 组合乘 mulw(t+1) = issue(t) 的乘积
    //  拍 t+2 : prod(=issue(t)) → 9→1 树（组合）→ **tsum_r 打拍**
    //  拍 t+3 : tsum_r(=issue(t) 的树) + v_c/last_c/par_c（也与 issue(t) 同相）
    //           ⇒ acc 在 t+3 的边沿累加、last 那拍快照 sum27  ← 时序修复 #3
    //  拍 t+4 : sum27 可读 → E1 v32 = sum27 + bias
    //  拍 t+5 : E2 sat8 → push
    //
    //  ⚠ 门控级必须**与数据的寄存器级数一致**：
    //    · 只有 prod 一级寄存时，树与累加串在一拍（关键路径 -0.777ns）⇒ 门控用 b 级；
    //    · 插入 tsum_r 后数据多了一级 ⇒ 门控必须用 c 级。
    //    2026-10-26 的教训（两次都踩在这一点上）：
    //      - 用 b 级 + prod 一级 ⇒ 差一拍（k3_tap00 的 (3,2)=100）
    //      - 用 c 级 + 无 tsum_r ⇒ 也差一拍（这次被 k3_1x1 的"全通道同值"巧合掩盖，8/8 通过）
    //    **判据：数一遍数据从 issue 到累加经过几个寄存器，门控就延几级。**
    //
    //  ⚠ 为什么不是"地址提前 N 个 ic"：BRAM 同步读的相位下，
    //    要 w_row_q[c] = weights(X(c)) 就得 w_rd_ic[c-1] = X(c)，
    //    而寄存器只能在 c-1 的边沿装载 —— 第一拍永远差一格。
    //    k1 的解法（也是本文件的解法）：**把像素字节也打一拍**，
    //    地址取"当前 ic"，两个寄存输出天然同相，从第一拍就对。
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            v_a <= 1'b0; v_b <= 1'b0; v_c <= 1'b0;
            par_a <= 1'b0; par_b <= 1'b0; par_c <= 1'b0;
            last_a <= 1'b0; last_b <= 1'b0; last_c <= 1'b0;
            p_a <= '0; p_b <= '0; p_c <= '0;
            x_a <= '0; x_b <= '0; x_c <= '0;
            y_a <= '0; y_b <= '0; y_c <= '0;
            oad_a <= 32'd0; oad_b <= 32'd0; oad_c <= 32'd0;
            for (int t = 0; t < 9; t = t + 1) pb_r[t] <= 8'd0;
        end else if (start_pulse) begin
            v_a <= 1'b0; v_b <= 1'b0; v_c <= 1'b0;
            par_a <= 1'b0; par_b <= 1'b0; par_c <= 1'b0;
            last_a <= 1'b0; last_b <= 1'b0; last_c <= 1'b0;
            p_a <= '0; p_b <= '0; p_c <= '0;
            x_a <= '0; x_b <= '0; x_c <= '0;
            y_a <= '0; y_b <= '0; y_c <= '0;
            oad_a <= 32'd0; oad_b <= 32'd0; oad_c <= 32'd0;
            for (int t = 0; t < 9; t = t + 1) pb_r[t] <= 8'd0;
        end else begin
            v_a  <= issue_en;
            if (issue_en) begin
                for (int t = 0; t < 9; t = t + 1) pb_r[t] <= pb[t];
                par_a  <= b_r[0];
                last_a <= (b_r == 3'd7) && (icg_r == ngrp_m1);
                p_a    <= p_r;
                x_a    <= x_r;
                y_a    <= y_r;
                oad_a  <= oad_now;
            end else begin
                // 停顿期间必须清 last（否则下一块的第一个乘积会被当成最后拍，
                // 这是 k1 §5 踩过的同一个坑）
                last_a <= 1'b0;
            end
            v_b    <= v_a;
            par_b  <= par_a;
            last_b <= last_a;
            p_b    <= p_a;
            x_b    <= x_a;
            y_b    <= y_a;
            oad_b  <= oad_a;
            v_c    <= v_b;
            par_c  <= par_b;
            last_c <= last_b;
            p_c    <= p_b;
            x_c    <= x_b;
            y_c    <= y_b;
            oad_c  <= oad_b;
        end
    end

    // 每 lane：乘 → 打拍 → 树 → 累加
    logic signed [31:0] sum27 [0:LANES-1];
    logic               sum27_vld;
    assign sum27_vld = v_c && last_c;

    genvar gl, gt;
    generate
        for (gl = 0; gl < LANES; gl = gl + 1) begin : g_lane
            // 本 lane 的 9 个权重字节：常量切片（零 mux）
            wire signed [7:0] wbv [0:8];
            for (gt = 0; gt < 9; gt = gt + 1) begin : g_wb
                assign wbv[gt] = $signed(w_row_q[(9*gl + gt)*8 +: 8]);
            end

            // 8×8 乘：use_dsp 强制进 DSP48（否则 Vivado 判"太小"推给 LUT）
            (* use_dsp = "yes" *) wire signed [15:0] mulw [0:8];
            for (gt = 0; gt < 9; gt = gt + 1) begin : g_mul
                assign mulw[gt] = $signed(pb_r[gt]) * wbv[gt];   // pb_r 与 w_row_q 同相
            end

            // t+1：乘积打拍
            logic signed [15:0] prod [0:8];
            // 9→1 加法树（组合，20 位；9×16384 = 147456 < 2^18 ⇒ 20 位足够）
            logic signed [19:0] tsum;      // 9→1 树的组合输出
            logic signed [19:0] tsum_r;    // 树输出打一拍（时序修复 #3：砍掉树+累加串在一拍）
            logic signed [25:0] acc_e, acc_o;

            wire signed [19:0] ts0 = {{4{prod[0][15]}}, prod[0]};
            wire signed [19:0] ts1 = {{4{prod[1][15]}}, prod[1]};
            wire signed [19:0] ts2 = {{4{prod[2][15]}}, prod[2]};
            wire signed [19:0] ts3 = {{4{prod[3][15]}}, prod[3]};
            wire signed [19:0] ts4 = {{4{prod[4][15]}}, prod[4]};
            wire signed [19:0] ts5 = {{4{prod[5][15]}}, prod[5]};
            wire signed [19:0] ts6 = {{4{prod[6][15]}}, prod[6]};
            wire signed [19:0] ts7 = {{4{prod[7][15]}}, prod[7]};
            wire signed [19:0] ts8 = {{4{prod[8][15]}}, prod[8]};
            assign tsum = ((ts0 + ts1) + (ts2 + ts3))
                        + ((ts4 + ts5) + (ts6 + ts7)) + ts8;

            always_ff @(posedge ap_clk or negedge ap_rst_n) begin
                if (!ap_rst_n) begin
                    for (int t = 0; t < 9; t = t + 1) prod[t] <= 16'sd0;
                    tsum_r <= 20'sd0;
                    acc_e <= 26'sd0;
                    acc_o <= 26'sd0;
                    sum27[gl] <= 32'sd0;
                end else if (start_pulse) begin
                    for (int t = 0; t < 9; t = t + 1) prod[t] <= 16'sd0;
                    tsum_r <= 20'sd0;
                    acc_e <= 26'sd0;
                    acc_o <= 26'sd0;
                    sum27[gl] <= 32'sd0;
                end else begin
                    for (int t = 0; t < 9; t = t + 1) prod[t] <= mulw[t];
                    tsum_r <= tsum;                    // 第 1 级：9→1 树
                    if (v_c) begin                     // 第 2 级：累加（门控随之延一级）
`ifdef K3_DUMP
                        // 每次"最后一拍"打一行：acc_e/acc_o 是历史，tsum 是当拍 9 抽头和。
                        // 若 (3,2) 那行 acc_e/acc_o 全 0 而 tsum=100 ⇒ 当拍误命中；
                        // 若 acc_o=100 而 tsum=0 ⇒ 历史漏清/串入。
                        if (last_c)
                            $display("[K3-SUM] p=%0d x=%0d y=%0d lane=%0d acc_e=%0d acc_o=%0d tsum=%0d",
                                     p_c, x_c, y_c, gl, acc_e, acc_o, tsum_r);
`endif
                        if (last_c) begin
                            // 两条奇偶链 + 最后一个乘积一次加完（省 2×8×26 FF）
                            sum27[gl] <= $signed({{6{acc_e[25]}}, acc_e})
                                       + $signed({{6{acc_o[25]}}, acc_o})
                                       + $signed({{12{tsum_r[19]}}, tsum_r});
                            acc_e <= 26'sd0;
                            acc_o <= 26'sd0;
                        end else if (par_c) begin
                            acc_o <= acc_o + {{6{tsum_r[19]}}, tsum_r};
                        end else begin
                            acc_e <= acc_e + {{6{tsum_r[19]}}, tsum_r};
                        end
                    end
                end
            end
        end
    endgenerate

    // ========================================================================
    // 14. 收尾：+bias → >>>shift → ReLU → sat8 → 拼 64-bit beat（2 拍流水）
    //
    //  E1(t+3) : v32 = sext(sum27) + bias_lane   （bias 是 pass 级寄存器）
    //  E2(t+4) : shf = v32 >>> shift；relu；饱和；lane_ok；拼 beat
    //  与下一像素的 MAC 完全重叠 ⇒ 稳态每像素额外拍数 0
    // ========================================================================
    logic              e1_vld, e2_vld;
    logic signed [31:0] v32 [0:LANES-1];
    logic [31:0]        ep_addr;          // 本 beat 的写地址（sum27 拍捕获）
    logic [7:0]         sat_b [0:LANES-1];
    logic [63:0]        out_word;

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            e1_vld  <= 1'b0;
            e2_vld  <= 1'b0;
            ep_addr <= 32'd0;
            ob_inf  <= 2'd0;
            e_pipe_empty_r <= 1'b0;
            for (int l = 0; l < LANES; l = l + 1) v32[l] <= 32'sd0;
        end else if (start_pulse) begin
            e1_vld  <= 1'b0;
            e2_vld  <= 1'b0;
            ep_addr <= 32'd0;
            ob_inf  <= 2'd0;
            e_pipe_empty_r <= 1'b0;
            for (int l = 0; l < LANES; l = l + 1) v32[l] <= 32'sd0;
        end else begin
            ob_inf  <= ob_inf + {1'b0, sum27_vld} - {1'b0, push_vld};
            // ⚠ 空判据必须覆盖 beat 走过的**每一级**：
            //   最后那个 ic 发射后，它还要在 v_a/v_b/v_c 里走 3 拍才到 sum27_vld，
            //   这 3 拍里 ob_inf 仍是 0 ⇒ 只用 ob_inf 会"假空"
            //   （2026-10-26 实测：门控从未生效、拍数与错误字节完全不变）。
            //   判据与"数寄存器级数"是同一条规律的另一面：**门控跟着数据延几级，
            //   "空"的判据就要覆盖数据经过的每一级**。
            e_pipe_empty_r <= !v_a && !v_b && !v_c
                              && (ob_inf == 2'd0) && !e1_vld && !push_vld;
            e1_vld <= sum27_vld;
            e2_vld <= e1_vld;
            if (sum27_vld) ep_addr <= oad_c;    // sum27 在 t+3，地址也走 3 级
            if (e1_vld) begin
                for (int l = 0; l < LANES; l = l + 1)
                    v32[l] <= sum27[l] + bias_lane[l];
            end
        end
    end

    // E2：移位 / ReLU / 饱和 / lane_ok（一个纯函数，便于逐条对照参考实现）
    function automatic [7:0] sat8_of(input [7:0] oc, input signed [31:0] acc_v);
        reg signed [31:0] shf;
        reg               lok;
        begin
            shf = acc_v >>> shift_r;
            lok = (oc < cout_r);
            if (!lok)                   sat8_of = 8'd0;        // cout 非 8 倍数：尾组写 0
            else if (relu_r && shf[31]) sat8_of = 8'd0;        // 运行期 ReLU
            else if (shf > 32'sd127)    sat8_of = 8'd127;
            else if (shf < -32'sd128)   sat8_of = 8'd128;      // -128
            else                        sat8_of = shf[7:0];
        end
    endfunction

    always_comb begin
        for (int l = 0; l < LANES; l = l + 1)
            sat_b[l] = sat8_of({p_c, 3'b000} + l[2:0], v32[l]);
    end

    // C8 拼包：第 j 个 lane 就是第 j 个字节（8 lane = 1 个 8-oc 组 = 1 整 beat）
    always_comb begin
        out_word = 64'd0;
        for (int l = 0; l < LANES; l = l + 1) out_word[l*8 +: 8] = sat_b[l];
    end

    // synopsys translate_off
    // FIFO 不溢出是"每 cin 拍一个 beat vs 1/拍 抽走"的直接推论；这里兜底抓意外
    always @(posedge ap_clk) begin
        if (ap_rst_n && push_vld && (ob_cnt >= 3'd4))
            $display("[K3] ERROR: 输出 beat FIFO 满时 push（ob_cnt=%0d）⇒ 丢 beat", ob_cnt);
        if (ap_rst_n && (f_cnt > 5'd16))
            $display("[K3] ERROR: 填充 FIFO 溢出（f_cnt=%0d）", f_cnt);
    end
    // synopsys translate_on

    // ========================================================================
    // 15. 输出 beat FIFO（4 深移位式）+ AXI4 写
    //
    //  为什么用移位式而不是"指针 + RAM"：k1 在 §9.3 踩过"寄存的 pop 晚一拍"
    //  导致旧地址配新数据。移位式让 ob_mem[0] 永远是队头（组合、常数下标），
    //  只有"整队下移"一个动作 ⇒ 不存在地址/数据错拍的可能。
    //  AWLEN=0（每 beat 一笔）：全网写 6.9M 笔 / 259M 拍 = 每 37 拍一笔，
    //  带宽 32 MB/s，完全不是瓶颈（见设计文档 §9.4；burst 版留作 v2）。
    // ========================================================================
    logic [95:0] ob_mem [0:OB_D-1];       // {addr[31:0], data[63:0]}
    logic        wd_aw_done, wd_w_done;

    assign push_vld = e2_vld;

    wire [95:0] ob_head = ob_mem[0];
    wire        ob_have = (ob_cnt != 3'd0);
    wire        aw_hs   = m_axi_out_awvalid && m_axi_out_awready;
    wire        w_hs    = m_axi_out_wvalid  && m_axi_out_wready;
    wire        beat_done = ob_have && (wd_aw_done || aw_hs) && (wd_w_done || w_hs);

    assign m_axi_out_awvalid = ob_have && !wd_aw_done;
    assign m_axi_out_awaddr  = ob_head[95:64];
    assign m_axi_out_awlen   = 8'd0;
    assign m_axi_out_wvalid  = ob_have && !wd_w_done;
    assign m_axi_out_wdata   = ob_head[63:0];
    assign m_axi_out_wstrb   = 8'hFF;
    assign m_axi_out_wlast   = 1'b1;
    assign m_axi_out_bready  = 1'b1;

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            ob_cnt     <= 3'd0;
            wd_aw_done <= 1'b0;
            wd_w_done  <= 1'b0;
            b_cnt      <= 8'd0;
            for (int i = 0; i < OB_D; i = i + 1) ob_mem[i] <= 96'd0;
        end else if (start_pulse) begin
            ob_cnt     <= 3'd0;
            wd_aw_done <= 1'b0;
            wd_w_done  <= 1'b0;
            b_cnt      <= 8'd0;
        end else begin
            // ---- B 计数 ----
            b_cnt <= b_cnt + {7'd0, aw_hs}
                            - {7'd0, (m_axi_out_bvalid && m_axi_out_bready)};
            // ---- 队头 beat 的 AW/W 半程握手 ----
            if (beat_done) begin
                wd_aw_done <= 1'b0;
                wd_w_done  <= 1'b0;
                for (int i = 0; i < OB_D-1; i = i + 1) ob_mem[i] <= ob_mem[i+1];
                if (push_vld && (ob_cnt < OB_D[2:0])) begin
                    ob_mem[ob_cnt - 3'd1] <= {ep_addr, out_word};
                    ob_cnt <= ob_cnt;              // 弹一个进一个
                end else begin
                    ob_cnt <= ob_cnt - 3'd1;
                end
            end else if (ob_have) begin
                if (aw_hs) wd_aw_done <= 1'b1;
                if (w_hs)  wd_w_done  <= 1'b1;
                if (push_vld && (ob_cnt < OB_D[2:0])) begin
                    ob_mem[ob_cnt] <= {ep_addr, out_word};
                    ob_cnt <= ob_cnt + 3'd1;
                end
            end else if (push_vld) begin
                ob_mem[3'd0] <= {ep_addr, out_word};
                ob_cnt <= 3'd1;
            end
        end
    end

    // ========================================================================
    // 14b. 仿真期状态转储（k5 同款；编译加 -d K3_SM_DUMP 才生效）
    //  状态迁移 + 每 20k 拍一行摘要 + 一段"卡住检测"（同一状态超过 200k 拍就报）
    // ========================================================================
    // synopsys translate_off
`ifdef K3_SM_DUMP
    reg [3:0] dbg_state_d = ST_IDLE;
    integer   dbg_same    = 0;

    always @(posedge ap_clk) begin
        dbg_cyc <= dbg_cyc + 1;
        if (!ap_rst_n) begin
            dbg_state_d <= ST_IDLE;
            dbg_same    <= 0;
        end else begin
            if (state != dbg_state_d) begin
                $display("[K3-SM] %0d -> %0d @cyc=%0d p=%0d y=%0d x=%0d g=%0d b=%0d cur_ok=%b pf=%0d bufsel=%b",
                         dbg_state_d, state, dbg_cyc, p_r, y_r, x_r, icg_r, b_r,
                         cur_ok, pf_stage, buf_sel);
                dbg_state_d <= state;
                dbg_same    <= 0;
            end else begin
                dbg_same <= dbg_same + 1;
                if (dbg_same == 200000)
                    $display("[K3-SM] STUCK 200k cycles in state=%0d p=%0d y=%0d x=%0d g=%0d b=%0d cur_ok=%b next_ok=%b",
                             state, p_r, y_r, x_r, icg_r, b_r, cur_ok, next_ok);
            end
            if ((dbg_cyc % 20000) == 0)
                $display("[K3-SM] t=%0d state=%0d p=%0d y=%0d/%0d x=%0d/%0d g=%0d/%0d b=%0d | fill st=%0d cnt=%0d job=%0d/%0d pok=%b gate=%b | RD grp=%0d col=%0d | WR grp=%0d col=%0d rp=%0d | mac cur_ok=%b pf=%0d next=%b | stall fill=%0d mac=%0d",
                         dbg_cyc, state, p_r, y_r, hout_r, x_r, wout_r, icg_r, ngrp_r, b_r,
                         f_state, f_cnt, f_job_i, f_njob, f_param_ok, f_gate_ok,
                         f_rd_grp, f_rd_col, f_wr_grp, f_wr_col, f_rp,
                         cur_ok, pf_stage, next_ok, dbg_stall_fill, dbg_stall_mac);
        end
    end
`endif
    // synopsys translate_on

    // ========================================================================
    // 14c. 定点转储（`-d K3_DUMP`）：三样一次打全，用来区分
    //      "权重装载错" / "抽头/字节选错" / "多推了一个 beat"
    // ========================================================================
    // synopsys translate_off
`ifdef K3_DUMP
    integer dw_i, dw_c;
    integer dw_wgt_done = 0;
    integer dw_mac_cnt   = 0;

    // (a) 权重装载完成后：lane0 每 ic 的 9 个字节（可直接与 weight.bin 对照）
    always @(posedge ap_clk) begin
        if (ap_rst_n && wl_done && (state == ST_WLOAD) && (dw_wgt_done == 0)) begin
            dw_wgt_done = 1;
            for (dw_i = 0; dw_i < 8; dw_i = dw_i + 1) begin
                $write("[K3-WGT] ic=%0d lane0:", dw_i);
                for (dw_c = 0; dw_c < 9; dw_c = dw_c + 1)
                    $write(" %0d", $signed(wgt_mem[dw_i][dw_c*8 +: 8]));
                $write("\n");
            end
        end
    end

    // (b) 前 32 个 issue 拍：当前 ic、tap4 的像素字节、lane0/tap4 的权重
    //     若 pb4 全 7 而 wb4 不对 ⇒ 权重装载；若 wb4 对而 pb4 不对 ⇒ 抽头/字节选
    always @(posedge ap_clk) begin
        if (ap_rst_n && issue_en && (dw_mac_cnt < 32)) begin
            dw_mac_cnt = dw_mac_cnt + 1;
            $display("[K3-MAC] ic=%0d b=%0d w_rd_ic=%0d | pb4=%0d pb_r4=%0d wb4=%0d pb0=%0d wb0=%0d",
                     {icg_r, b_r}, b_r, w_rd_ic,
                     $signed(pb[4]), $signed(pb_r[4]),
                     $signed(w_row_q[4*8 +: 8]),
                     $signed(pb[0]), $signed(w_row_q[0*8 +: 8]));
        end
    end

    // (b2) 输入口越界探针：只有越界的那个 AR 才打，输出很短
    wire [31:0] dbg_in_bytes = hw8_r * {27'd0, ngrp_r};
    always @(posedge ap_clk) begin
        if (ap_rst_n && m_axi_in_arvalid && m_axi_in_arready) begin
            if (m_axi_in_araddr >= (in_base_r + dbg_in_bytes))
                $display("[K3-OOB] IN addr=0x%08h len=%0d | in_base=0x%08h in_bytes=%0d | st=%0d job=%0d/%0d row=%0d goff=%0d wcol=%0d rgrp=%0d rcol=%0d chunk=%0d",
                         m_axi_in_araddr, m_axi_in_arlen,
                         in_base_r, dbg_in_bytes,
                         state, f_job_i, f_njob, f_row, f_grp_off,
                         f_wr_col, f_rd_grp, f_rd_col, f_chunk);
        end
    end

    // (b3) 权重/偏置口越界探针（sticky：每条越界 AR 打一行）
    wire [31:0] dbg_wgt_bytes = {4'd0, cout_r} * cin_x12_r;        // cout*cin*12
    wire [31:0] dbg_bia_bytes = {4'd0, cout_r} * 32'd4;            // cout*4（int32）
    always @(posedge ap_clk) begin
        if (ap_rst_n && m_axi_w_arvalid && m_axi_w_arready
            && (m_axi_w_araddr >= (wgt_base_r + dbg_wgt_bytes)))
            $display("[K3-OOB] WGT addr=0x%08h len=%0d | base=0x%08h bytes=%0d | p=%0d lane=%0d oc=%0d cout=%0d",
                     m_axi_w_araddr, m_axi_w_arlen, wgt_base_r, dbg_wgt_bytes,
                     p_r, wl_lane, ({1'b0,p_r}*4'd8 + wl_lane), cout_r);
        if (ap_rst_n && m_axi_b_arvalid && m_axi_b_arready
            && (m_axi_b_araddr >= (bias_base_r + dbg_bia_bytes)))
            $display("[K3-OOB] BIA addr=0x%08h len=%0d | base=0x%08h bytes=%0d",
                     m_axi_b_araddr, m_axi_b_arlen, bias_base_r, dbg_bia_bytes);
    end

    // (c) 每个输出 beat：地址 + 数据（抓"同一地址推两次"或多推一拍）
    always @(posedge ap_clk) begin
        if (ap_rst_n && push_vld)
            $display("[K3-PUSH] addr=0x%08h p=%0d x=%0d y=%0d data=0x%016h",
                     ep_addr, p_c, x_c, y_c, out_word);
    end
`endif
    // synopsys translate_on

    // ========================================================================
    // 14. 仿真期断言（综合忽略）
    // ========================================================================
    // synopsys translate_off
    always @(posedge ap_clk) begin
        if (ap_rst_n && start_pulse) begin
            if (ctrl_cin[2:0] != 3'd0 || ctrl_cin < 32'd8 || ctrl_cin > CIN_MAX)
                $display("[K3] ERROR: cin=%0d 不满足 8 的倍数 / 8..%0d", ctrl_cin, CIN_MAX);
            if (ctrl_cout > COUT_MAX)
                $display("[K3] ERROR: cout=%0d > COUT_MAX=%0d", ctrl_cout, COUT_MAX);
            if (ctrl_stride != 4'd1 && ctrl_stride != 4'd2)
                $display("[K3] ERROR: stride=%0d 不支持（只 1/2）", ctrl_stride);
            if (ctrl_dil < 4'd1 || ctrl_dil > 4'd3)
                $display("[K3] ERROR: dilation=%0d 不支持（只 1/2/3）", ctrl_dil);
            if (ctrl_w > W_MAX || ctrl_h > H_MAX)
                $display("[K3] ERROR: %0dx%0d 超过 W_MAX/H_MAX=%0d/%0d",
                         ctrl_w, ctrl_h, W_MAX, H_MAX);
        end
    end
    // synopsys translate_on

endmodule
