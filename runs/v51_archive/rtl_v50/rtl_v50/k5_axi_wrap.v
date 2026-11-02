// ============================================================================
//  k5_axi_wrap.v —— conv_k5_engine 的 AXI4-Lite 控制包装
//
//  寄存器映射**与 k3_axi_wrap 完全一致**（PS 侧 `k5_engine.py` 已按这张表写好，
//  k3 又是照 k1 的表做的 ⇒ 三兄弟同一套 PS 代码，只换基地址）：
//
//    ctrl 页（A[16]=1，本 IP 的 PS 基地址 0x4008_0000 → ctrl 页 0x4009_0000）
//      0x00 AP_CTRL    W: bit0=start（自清）  R: [0]=0,[1]=done,[2]=idle,[3]=ready
//      0x10 AP_RETURN  R: 0=成功；1..9=k5_cfg_check 的错误码；0xA=地址窗口越界
//      0x18 H    0x20 W    0x28 HOUT  0x30 WOUT  0x38 CIN  0x40 COUT  0x48 KSIZE
//      0x50 STRIDE  0x58 DIL  0x60 PAD  0x68 SHIFT  0x70 RELU
//      0x94 STATUS     R: [0]done [1]busy [2]err [7:4]err_code [8]idle
//                         [9]sticky_oob（只读，粘性：越界读被钳制过就置位，仅复位清）
//      0x98 WIN_LO    RW: 允许访问的地址窗口下界（默认 0）
//      0x9C WIN_HI    RW: 允许访问的地址窗口上界（默认 0xFFFF_FFFF）
//      0xA0 STALL_FILL R: 读口被反压的拍数（见下"与 k3 的差异"）
//      0xA4 STALL_MAC  R: 写口被反压的拍数（同上）
//      0xA8 自检     R: {26'd0, win_ok[5], ok[4], err[3:0]}
//      0xAC 四区窗口 R: {28'd0, ok_ou[3], ok_bi[2], ok_wg[1], ok_in[0]}
//      0xB0 bytes_in R: ⌈cin/8⌉·8·H·W·8（本版 13,107,200 = 0x00C8_0000）
//    offs 页（A[16]=0，PS 基地址 0x4008_0000）
//      0x10 IN_ADDR(lo) 0x14 IN_ADDR(hi)
//      0x1C WGT_ADDR    0x20 WGT_ADDR(hi)
//      0x28 BIAS_ADDR   0x2C BIAS_ADDR(hi)
//      0x34 OUT_ADDR    0x38 OUT_ADDR(hi)
//
//  ★ 本层被当作**安全边界**（不是胶水），三条硬约束（与 k3 同构）：
//    1) 配置合法性：启动前跑 k5_cfg_check；非法 ⇒ 写 ERR + **自打 done**
//       （PS 的"启动→等 done"流程不会挂死），且**不拉 ctrl_start**。
//    2) 地址窗口：四个区 [addr, addr+bytes) 必须整体落在 [WIN_LO, WIN_HI] 内，
//       否则同 1) 拒绝启动并给错误码 0xA。
//    3) 越界兜底 + sticky：即使参数全合法，合并读口也把 AR 地址**钳**在所属区的
//       [base, base+bytes) 内（读最后一个合法 8 字节），并置 sticky_oob。
//       TB 断言 sticky_oob 恒 0；上板时 STATUS[9] 就是一盏"地址算错了"的灯。
//
//  AXI4 master 只有 2 个（k5 引擎本体有 3 读 1 写）：
//    in/wgt/bias 三个读引擎在**本层**合并成 1 个读口（2-bit ARID tag：
//    0=in / 1=wgt / 2=bias），R 通道按锁存的 tag 回路由；授权从 AR 握手保持到
//    该 burst 的 RLAST 握手，burst 内绝不换人。输出口原样透传（一像素一 beat，
//    AWLEN≤255，WSTRB=0xFF 由引擎给）。
//
//  ────────────────────────────────────────────────────────────────────────
//  与 k3_axi_wrap 的差异（每一条都是"k5 引擎端口不同"逼出来的，不是口味）：
//   1) 引擎没有 `ctrl_dil` / `ctrl_relu` / `ctrl_pad` / `ctrl_ksize` 端口：
//      k5 的 dilation/ReLU 是**编译期常量**（d=1、ReLU 硬连线）。这些寄存器
//      照样存在、照样能读写回显，但只喂给 k5_cfg_check 做拒绝判定 —— 因为
//      "PS 写了 d=2 而硬件按 d=1 算"必须报错，不能静默算错。
//   2) 引擎没有 `dbg_stall_fill/dbg_stall_mac` 端口：k5 引擎 FSM 是顺序执行、
//      没有 k3 那种填充 FIFO / 抽头仲裁，取不到"等填充/等抽头"的分类。本层
//      改在**总线边界**统计（见 §5）：
//         STALL_FILL = (读 AR 有效但未被接受) 或 (R 通道该给数却没给) 的拍数
//         STALL_MAC  = (写 AW 有效但未被接受) 或 (W 有效但未被接受) 的拍数
//      语义是"互连/DDR 反压拍数"，与 k3 的"引擎内部 stall 分类"**不可直接对比**，
//      但同样能一眼判出"DDR 供不上"还是"逻辑慢"（前者非零且随时间线性增长）。
//   3) 引擎没有 `m_axi_*_rlast` 的**消费**（只按自己的 beat 计数收 burst）⇒
//      三路 rlast 输入恒接 1'b0（与 k3 一致）；合并读口的结束判据仍用从机的
//      RLAST（见 §4，与 k3 逐行相同）。
//   4) 参数不同：k3 是 (LANES, CAP_B)，k5 是 (CIN, COUT, W, H, PAD, OC_PER_PASS)
//      —— 这 6 个必须**原样**传给 conv_k5_engine，同时原样传给 k5_cfg_check
//      作为"合法集合"。本层不另设一份常量，避免两处不一致。
//   5) HOUT/WOUT 由 H/W 直接派生（stride 被 cfg_check 固定为 1 ⇒ Hout=H、Wout=W）。
//      k3 在这里放了一个 (X-1)>>(S-1)+1 的移位器，k5 不需要，也就避开了
//      "移位量写成 stride[0]"那个已经踩过的坑（k3 §18.2c 修复 #4）。
//
//  时钟/复位：s_axi_aclk / s_axi_aresetn（低有效）同时驱动引擎的 ap_clk/ap_rst_n。
// ============================================================================
`timescale 1ns / 1ps

module k5_axi_wrap #(
    // ↓ 这 6 个参数**必须**与 conv_k5_engine 的实例参数逐字相同（本层原样下传）
    parameter int CIN         = 12,
    parameter int COUT        = 48,
    parameter int W           = 128,
    parameter int H           = 128,
    parameter int PAD         = 2,
    parameter int OC_PER_PASS = 1,
    // k5_cfg_check 的"槽容量保守上界"（见 k5_cfg_check.v 文件头 4'h8）
    parameter int CAP_B       = 4608
)(
    // ---- AXI4-Lite slave ----
    input  logic        s_axi_aclk,
    input  logic        s_axi_aresetn,
    input  logic [31:0] s_axi_awaddr,
    input  logic        s_axi_awvalid,
    output logic        s_axi_awready,
    input  logic [31:0] s_axi_wdata,
    input  logic [3:0]  s_axi_wstrb,
    input  logic        s_axi_wvalid,
    output logic        s_axi_wready,
    output logic [1:0]  s_axi_bresp,
    output logic        s_axi_bvalid,
    input  logic        s_axi_bready,
    input  logic [31:0] s_axi_araddr,
    input  logic        s_axi_arvalid,
    output logic        s_axi_arready,
    output logic [31:0] s_axi_rdata,
    output logic [1:0]  s_axi_rresp,
    output logic        s_axi_rvalid,
    input  logic        s_axi_rready,

    // ---- AXI4 master：合并后的读（in/wgt/bias；2-bit tag）----
    output logic        m_axi_rd_arvalid,  input logic m_axi_rd_arready,
    output logic [31:0] m_axi_rd_araddr,   output logic [7:0] m_axi_rd_arlen,
    output logic [1:0]  m_axi_rd_arid,
    input  logic        m_axi_rd_rvalid,   output logic m_axi_rd_rready,
    input  logic [63:0] m_axi_rd_rdata,    input logic m_axi_rd_rlast,
    input  logic [1:0]  m_axi_rd_rid,

    // ---- AXI4 master：输出 ----
    output logic        m_axi_out_awvalid, input logic m_axi_out_awready,
    output logic [31:0] m_axi_out_awaddr,  output logic [7:0]  m_axi_out_awlen,
    output logic        m_axi_out_wvalid,  input logic m_axi_out_wready,
    output logic [63:0] m_axi_out_wdata,   output logic [7:0]  m_axi_out_wstrb,
    output logic        m_axi_out_wlast,
    input  logic        m_axi_out_bvalid,  output logic m_axi_out_bready
);

    // ========================================================================
    // 0. 时钟/复位
    // ========================================================================
    logic clk, rst_n;
    assign clk   = s_axi_aclk;
    assign rst_n = s_axi_aresetn;

    // ========================================================================
    // 1. AXI4-Lite 从机：单拍握手，寄存器文件
    // ========================================================================
    logic [31:0] cfg_h, cfg_w, cfg_hout, cfg_wout;
    logic [31:0] cfg_cin, cfg_cout, cfg_ksize, cfg_stride_d, cfg_dil_d, cfg_pad;
    logic [31:0] cfg_shift, cfg_relu_d;
    logic [31:0] addr_in, addr_wgt, addr_bias, addr_out;   // 32 位（高字必须为 0）
    logic [31:0] win_lo, win_hi;

    logic        eng_done, eng_busy;    // 引擎握手（提前声明：§2 的拒绝逻辑要用）
    // ⚠ 时序关键（k3 上 BD 综合 WNS −43ns / 101 级的根因，2026-10-27）：
    //   不能把 `.ctrl_start(do_start && cfg_ok && cfg_win_ok)` 直接接引擎 ——
    //   那等于把"k5_cfg_check 的 9 条规则 + 4 区 33 位窗口比较"（约 70 级组合）
    //   串到引擎内部的时钟使能上。配置检查只需"启动前稳定"，不需要"同拍到达"。
    //   ⇒ 打成两级：start_pend（挂起）→ 等 cfg_settle≥8 → eng_start（单拍脉冲）。
    //   代价：PS 看到 start 晚 2 拍（13 ns），PS 本来就在轮询 done ⇒ 无影响。
    logic        eng_start;
    logic        start_pend;         // 启动请求挂起（等配置派生量稳定）
    logic [3:0]  cfg_settle;         // 配置写后 ≥8 拍才认为派生量(bytes)已稳定
    // 提前声明：0xA8/0xAC/0xB0 调试寄存器要读它们（赋值在 §2）
    wire         cfg_ok, cfg_win_ok;
    wire [3:0]   cfg_err;
    logic [3:0]  cfg_err_r;          // cfg_err 打一拍：截断"cfg_check → 寄存器 D"的链
    // ⚠ **一致性要求**：Lite 读回(0xA8)与"启动决策"必须用**同一份寄存值**，
    //   否则会出现"自检说合法、实际被拒"（或反之），上板极难排查（k3 踩过）。
    logic        cfg_ok_r, cfg_win_ok_r;
    wire         ok_in, ok_wg, ok_bi, ok_ou;
    logic [31:0] bytes_in, bytes_wg, bytes_bi, bytes_ou;   // 由 always_ff 驱动（流水），必须是 logic
    logic [31:0] dbg_fill, dbg_mac;     // §5：总线反压拍数（只读）
    logic        reg_start;      // 自清的启动脉冲寄存器（由 AP_CTRL 写入置位）
    logic        reg_done;       // 完成
    logic [3:0]  reg_err;        // 错误码（0=成功）
    logic        sticky_oob;

    // AXI-Lite 写通道：AW 与 W **分别锁存**，两者都到手才执行
    //   （PS 的 axi_gpio/自研驱动经常把 AW 和 W 分两拍给，不能要求同拍）
    logic        aw_have, w_have, bvalid_r;
    wire wr_go = aw_have && w_have;        // 本拍执行写入
    logic [31:0] aw_addr_l, w_data_l;
    logic [3:0]  w_strb_l;

    assign s_axi_awready = !aw_have && !bvalid_r;
    assign s_axi_wready  = !w_have  && !bvalid_r;
    assign s_axi_bvalid  = bvalid_r;
    assign s_axi_bresp   = 2'b00;

    wire aw_hs = s_axi_awvalid && s_axi_awready;
    wire w_hs  = s_axi_wvalid  && s_axi_wready;

    wire page_ctrl = aw_addr_l[16];        // 写页（用锁存后的地址）
    wire [11:0] woff = aw_addr_l[13:2];    // 写偏移（字对齐）
    wire         cfg_written = wr_go && !(page_ctrl && (woff == 12'h00)); // 除 AP_CTRL 外的写

    wire page_ctrl_r = s_axi_araddr[16];   // 读页
    wire [11:0] roff = s_axi_araddr[13:2];

    // 启动脉冲：写 AP_CTRL bit0
    logic start_q;
    wire  do_start = reg_start && !start_q;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) start_q <= 1'b0;
        else        start_q <= reg_start;
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cfg_h <= 0; cfg_w <= 0; cfg_hout <= 0; cfg_wout <= 0;
            cfg_cin <= 0; cfg_cout <= 0; cfg_ksize <= 5;
            // 复位默认值 = 本次综合的常量（PS 少写一项时不会"看起来合法"）
            cfg_stride_d <= 1; cfg_dil_d <= 1; cfg_pad <= 2;
            cfg_shift <= 0; cfg_relu_d <= 32'h1;
            addr_in <= 0; addr_wgt <= 0; addr_bias <= 0; addr_out <= 0;
            win_lo  <= 32'd0;  win_hi <= 32'hFFFF_FFFF;
            reg_start <= 1'b0;
            bvalid_r  <= 1'b0;
            aw_have   <= 1'b0;
            w_have    <= 1'b0;
            aw_addr_l <= 32'd0;
            w_data_l  <= 32'd0;
            w_strb_l  <= 4'd0;
        end else begin
            if (aw_hs) begin aw_have <= 1'b1; aw_addr_l <= s_axi_awaddr; end
            if (w_hs)  begin w_have  <= 1'b1; w_data_l  <= s_axi_wdata;
                                     w_strb_l  <= s_axi_wstrb; end
            if (wr_go) begin
                aw_have  <= 1'b0;              // 消费掉，可接下一笔
                w_have   <= 1'b0;
                bvalid_r <= 1'b1;
                if (page_ctrl) begin
                    case (woff)
                        12'h06: cfg_h      <= w_data_l;      // 0x18 >> 2
                        12'h08: cfg_w      <= w_data_l;      // 0x20
                        12'h0A: cfg_hout   <= w_data_l;      // 0x28（只回显，见文件头 5)）
                        12'h0C: cfg_wout   <= w_data_l;      // 0x30（只回显）
                        12'h0E: cfg_cin    <= w_data_l;      // 0x38
                        12'h10: cfg_cout   <= w_data_l;      // 0x40
                        12'h12: cfg_ksize  <= w_data_l;      // 0x48
                        12'h14: cfg_stride_d <= w_data_l;    // 0x50
                        12'h16: cfg_dil_d  <= w_data_l;      // 0x58
                        12'h18: cfg_pad    <= w_data_l;      // 0x60
                        12'h1A: cfg_shift  <= w_data_l;      // 0x68
                        12'h1C: cfg_relu_d <= w_data_l;      // 0x70（只回显）
                        12'h26: win_lo     <= w_data_l;      // 0x98
                        12'h27: win_hi     <= w_data_l;      // 0x9C
                        default: ;
                    endcase
                end else begin
                    case (woff)
                        12'h04: addr_in   <= w_data_l;       // 0x10
                        12'h05: ;                            // 0x14 hi（丢弃，必须 0）
                        12'h07: addr_wgt  <= w_data_l;       // 0x1C
                        12'h08: ;                            // 0x20 hi
                        12'h0A: addr_bias <= w_data_l;       // 0x28
                        12'h0B: ;                            // 0x2C hi
                        12'h0D: addr_out  <= w_data_l;       // 0x34
                        12'h0E: ;                            // 0x38 hi
                        default: ;
                    endcase
                end
            end
            // AP_CTRL bit0：写入置位，do_start（上升沿）后**自清** ⇒ 下次写 1 还能再启动
            if (wr_go && page_ctrl && (woff == 12'h00)) reg_start <= w_data_l[0];
            else if (do_start)                                  reg_start <= 1'b0;
            if (bvalid_r && s_axi_bready) bvalid_r <= 1'b0;
        end
    end

    // STATUS（0x94）：[0]done [1]busy [2]err [7:4]err_code [8]idle [9]sticky_oob
    wire [31:0] status_w  = {23'd0, sticky_oob, !eng_busy,   // [9]=sticky [8]=idle
                             (reg_err != 4'd0), reg_err,     // [7:4]=err_code [2]=err
                             eng_busy,                       // [1]=busy
                             reg_done};                      // [0]=done
    // AP_CTRL: [0]=0 [1]=done [2]=idle [3]=ready
    wire [31:0] ap_ctrl_w = {28'd0, 1'b1, !eng_busy, reg_done, 1'b0};

    // AXI-Lite 读通道（组合给数 + 1 拍有效）
    logic rvalid_r;
    assign s_axi_arready = !rvalid_r;
    assign s_axi_rvalid  = rvalid_r;
    assign s_axi_rresp   = 2'b00;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s_axi_rdata <= 32'd0;
            rvalid_r    <= 1'b0;
        end else begin
            if (s_axi_arvalid && s_axi_arready) begin
                rvalid_r <= 1'b1;
                if (page_ctrl_r) begin
                    case (roff)
                        12'h00: s_axi_rdata <= ap_ctrl_w;
                        12'h04: s_axi_rdata <= {28'd0, reg_err};          // 0x10 AP_RETURN
                        12'h06: s_axi_rdata <= cfg_h;
                        12'h08: s_axi_rdata <= cfg_w;
                        12'h0A: s_axi_rdata <= cfg_hout;
                        12'h0C: s_axi_rdata <= cfg_wout;
                        12'h0E: s_axi_rdata <= cfg_cin;
                        12'h10: s_axi_rdata <= cfg_cout;
                        12'h12: s_axi_rdata <= cfg_ksize;
                        12'h14: s_axi_rdata <= cfg_stride_d;
                        12'h16: s_axi_rdata <= cfg_dil_d;
                        12'h18: s_axi_rdata <= cfg_pad;
                        12'h1A: s_axi_rdata <= cfg_shift;
                        12'h1C: s_axi_rdata <= cfg_relu_d;
                        12'h25: s_axi_rdata <= status_w;                  // 0x94 STATUS（含 sticky）
                        12'h2A: s_axi_rdata <= {26'd0, cfg_win_ok_r, cfg_ok_r, cfg_err_r}; // 0xA8 自检
                        12'h2B: s_axi_rdata <= {28'd0, ok_ou, ok_bi, ok_wg, ok_in};    // 0xAC 四区标志
                        12'h2C: s_axi_rdata <= bytes_in;                              // 0xB0 bytes_in
                        12'h28: s_axi_rdata <= dbg_fill;              // 0xA0 stall_fill
                        12'h29: s_axi_rdata <= dbg_mac;               // 0xA4 stall_mac
                        12'h26: s_axi_rdata <= win_lo;
                        12'h27: s_axi_rdata <= win_hi;
                        default: s_axi_rdata <= 32'd0;
                    endcase
                end else begin
                    case (roff)
                        12'h04: s_axi_rdata <= addr_in;
                        12'h07: s_axi_rdata <= addr_wgt;
                        12'h0A: s_axi_rdata <= addr_bias;
                        12'h0D: s_axi_rdata <= addr_out;
                        default: s_axi_rdata <= 32'd0;
                    endcase
                end
            end else if (rvalid_r && s_axi_rready) begin
                rvalid_r <= 1'b0;
            end
        end
    end

    // ========================================================================
    // 2. 配置派生 + 三个安全边界
    // ========================================================================
    wire [3:0] cfg_stride = cfg_stride_d[3:0];
    wire [3:0] cfg_dil    = cfg_dil_d[3:0];

    // ------------------------------------------------------------------
    // 2.1 bytes_*：三级流水 + 收窄操作数 + 收窄位宽
    //
    //  ⚠ 三条硬规则（k3 上花了一整天才收敛的，别"优化"回去）：
    //   (a) `*` 的两个操作数必须**按物理上界显式收窄**，注释写上界值；
    //   (b) 乘积必须**先赋给显式宽度的 wire/reg**，再拿去拼接（
    //       `{11'd0, (a*b), 3'b000}` 里的乘积会被截成 max(width(a),width(b)) 位）；
    //   (c) 配置期的大计算**不能**用组合 assign 实时算（那是 32 位乘法器串联，
    //       每级 ~30 级逻辑、三级 ≈ 90 级），必须打成级寄存流水，
    //       再由 cfg_settle[3] 保证"启动前已稳定"。
    //
    //  上界（按 9 位切片的最坏值给，跟"合法值"无关，避免任何截断）：
    //    ⌈cin/8⌉·8  ≤ 504（9b，{ngrp[5:0],3'b000}）
    //    H/W/pad 后 ≤ 511（9b，cfg_*[8:0]）
    //    cout·cin ≤ 511×511 = 261,121（18b）
    //    → pA ≤ 504×511 = 257,544（18b）；pB ≤ 257,544×511 = 131,604,984（27b）
    //  ------------------------------------------------------------------
    //  本版真实值（cin=12/H=W=320/cout=48）：
    //    bytes_in  = 16·320·320·8 = 13,107,200
    //    bytes_wgt = 48·12·28     = 16,128
    //    bytes_bias= 48·4         = 192
    //    bytes_out = 48·320·320·8 = 39,321,600
    //  ⚠ 三个"×8"的因子是 k1/k3/k5 共同的记账口径（PS 侧 buffer_bytes() 同式），
    //    所以窗口/钳制用的上界比真实张量大 8 倍 —— **必须与 PS 一致**，别"修正"它。
    wire [8:0]  ngrp_i   = (cfg_cin[8:0]  + 9'd7) >> 3;             // ⌈cin/8⌉（cin=12 → 2）
    wire [8:0]  ngrp_o   = (cfg_cout[8:0] + 9'd7) >> 3;             // ⌈cout/8⌉（48 → 6）
    wire [8:0]  ngrp_i8  = {ngrp_i[5:0], 3'b000};                   // ×8 用拼接（≤504）
    wire [8:0]  ngrp_o8  = {ngrp_o[5:0], 3'b000};                   // ×8 用拼接（≤504）

    // 三级流水：
    //   stage0 寄存窄位宽配置 → stage1 两两相乘 → stage2 再乘 → stage3 拼字节数
    logic [8:0]  h_n_r, w_n_r;                 // H/W 窄位宽寄存版（≤511，9b）
    logic [8:0]  cout_n_r, cin_n_r, ngrpi8_r, ngrpo8_r;
    logic [17:0] pA_in, pA_ou;                 // 9×9 ≤ 257,544（18b）
    logic [17:0] pB_wg;                        // cout·cin ≤ 261,121（18b）
    logic [26:0] pB_in, pB_ou;                 // 18×9 ≤ 131,604,984（27b）
    logic [22:0] pB_wg2;                       // ×28 ≤ 7,311,388（23b）

    // bytes_wgt 的 ×28：28 = 32 − 4 ⇒ 两条移位 + 一次减法，不占 DSP/乘法器。
    //   {5'd0, pB_wg} 是 23 位（18+5）；×32 ≤ 8,355,872 < 2^23 ⇒ 23 位够装。
    wire [22:0] pB_wg_x32 = {5'd0, pB_wg} << 5;    // ≤ 8,355,872（< 2^23）
    wire [22:0] pB_wg_x4  = {5'd0, pB_wg} << 2;    // ≤ 1,044,484（< 2^21）

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            h_n_r <= 9'd0; w_n_r <= 9'd0;
            cout_n_r <= 9'd0; cin_n_r <= 9'd0;
            ngrpi8_r <= 9'd0; ngrpo8_r <= 9'd0;
            pA_in <= 18'd0; pA_ou <= 18'd0; pB_wg <= 18'd0;
            pB_in <= 27'd0; pB_ou <= 27'd0; pB_wg2 <= 23'd0;
            bytes_in <= 32'd0; bytes_ou <= 32'd0;
            bytes_wg <= 32'd0; bytes_bi <= 32'd0;
        end else begin
            // stage0：先寄存配置窄位宽（截断跨模块走线，乘法两端都是本地寄存器）
            h_n_r    <= cfg_h[8:0];
            w_n_r    <= cfg_w[8:0];
            cout_n_r <= cfg_cout[8:0];
            cin_n_r  <= cfg_cin[8:0];
            ngrpi8_r <= ngrp_i8;
            ngrpo8_r <= ngrp_o8;
            // stage1：9×9
            pA_in <= ngrpi8_r * h_n_r;
            pA_ou <= ngrpo8_r * h_n_r;         // stride 固定 1 ⇒ Hout=H（见文件头 5)）
            pB_wg <= cout_n_r * cin_n_r;
            // stage2：18×9
            pB_in <= pA_in * w_n_r;
            pB_ou <= pA_ou * w_n_r;
            pB_wg2 <= pB_wg_x32 - pB_wg_x4;    // ×28
            // stage3：×8 用拼接（不占资源）；bytes_bi 的 ×4 也用拼接
            bytes_in <= {pB_in, 3'b000};
            bytes_ou <= {pB_ou, 3'b000};
            bytes_wg <= {9'd0, pB_wg2};                       // 9+23 = 32
            bytes_bi <= {21'd0, cout_n_r, 2'b00};             // 21+9+2 = 32
        end
    end

    // ------------------------------------------------------------------
    // 2.2 (边界 1) 配置合法性：k5_cfg_check，合法集合 = 本次综合的参数
    // ------------------------------------------------------------------
    k5_cfg_check #(
        .CIN_EXP    (CIN),
        .COUT_EXP   (COUT),
        .H_EXP      (H),
        .W_EXP      (W),
        .PAD_EXP    (PAD),
        .KSIZE_EXP  (5),
        .STRIDE_EXP (1),
        .DIL_EXP    (1),
        .CAP_B      (CAP_B)
    ) u_chk (
        .cfg_cin    (cfg_cin),
        .cfg_cout   (cfg_cout),
        .cfg_h      (cfg_h),
        .cfg_w      (cfg_w),
        .cfg_ksize  (cfg_ksize),
        .cfg_pad    (cfg_pad),
        .cfg_shift  (cfg_shift),
        .cfg_stride (cfg_stride),
        .cfg_dil    (cfg_dil),
        .err        (cfg_err),
        .ok         (cfg_ok)
    );

    // cfg_err / cfg_ok / cfg_win_ok 各打一拍（反正已经等 cfg_settle 8 拍，多 1 拍零成本）
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cfg_err_r    <= 4'd0;
            cfg_ok_r     <= 1'b0;
            cfg_win_ok_r <= 1'b0;
        end else begin
            cfg_err_r    <= cfg_err;
            cfg_ok_r     <= cfg_ok;
            cfg_win_ok_r <= cfg_win_ok;
        end
    end

    // ------------------------------------------------------------------
    // 2.3 (边界 2) 地址窗口：每区 [addr, addr+bytes) ⊆ [WIN_LO, WIN_HI]
    //     展开成 33 位比较（原来用一个 automatic function 写，仿真里结果与算术
    //     不符 —— 展开后可直接观测）
    // ------------------------------------------------------------------
    wire [32:0] e_in = {1'b0, addr_in}   + {1'b0, bytes_in};
    wire [32:0] e_wg = {1'b0, addr_wgt}  + {1'b0, bytes_wg};
    wire [32:0] e_bi = {1'b0, addr_bias} + {1'b0, bytes_bi};
    wire [32:0] e_ou = {1'b0, addr_out}  + {1'b0, bytes_ou};

    assign ok_in = ({1'b0, addr_in}   >= {1'b0, win_lo}) && (e_in <= {1'b0, win_hi});
    assign ok_wg = ({1'b0, addr_wgt}  >= {1'b0, win_lo}) && (e_wg <= {1'b0, win_hi});
    assign ok_bi = ({1'b0, addr_bias} >= {1'b0, win_lo}) && (e_bi <= {1'b0, win_hi});
    assign ok_ou = ({1'b0, addr_out}  >= {1'b0, win_lo}) && (e_ou <= {1'b0, win_hi});

    assign cfg_win_ok = ok_in && ok_wg && ok_bi && ok_ou;

    // ------------------------------------------------------------------
    // 2.4 启动流程（两级）：
    //   do_start（Lite 写脉冲）→ start_pend 挂起 → 等 cfg_settle≥8（bytes 三级流水算完）
    //   → 统一"接受/拒绝"：合法则打出一拍 eng_start，非法则写 reg_err + 自打 done
    //   ⇒ 决策时用的是**寄存后的 bytes**，且组合逻辑只到寄存器 D 端
    // ------------------------------------------------------------------
    logic done_stub;            // 被拒绝时的一拍 done

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) cfg_settle <= 4'd0;
        else if (cfg_written) cfg_settle <= 4'd0;              // 任何配置写都重新计时
        else if (cfg_settle != 4'd15) cfg_settle <= cfg_settle + 4'd1;
    end

    wire cfg_stable = cfg_settle[3];        // ≥8 拍 ⇒ bytes 必然已稳定
    wire start_go   = start_pend && cfg_stable;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            reg_err     <= 4'd0;
            reg_done    <= 1'b0;
            done_stub   <= 1'b0;
            eng_start   <= 1'b0;
            start_pend  <= 1'b0;
            // ⚠ sticky_oob 只在 §4（合并/钳制块）里驱动 —— 同一个 reg 不能有两个
            //   always_ff 驱动（xvlog 不报、xelab 报 VRFC 10-3818，k3 踩到过）
        end else begin
            done_stub <= 1'b0;
            eng_start <= 1'b0;
            if (do_start) start_pend <= 1'b1;
            if (start_go) begin
                start_pend <= 1'b0;
                if (!cfg_ok_r) begin
                    reg_err <= cfg_err_r;                // 4'h1..4'h9（寄存版，链更短）
                    done_stub <= 1'b1;
                end else if (!cfg_win_ok_r) begin
                    reg_err <= 4'hA;                     // 地址窗口越界
                    done_stub <= 1'b1;
                end else begin
                    reg_err   <= 4'd0;
                    eng_start <= 1'b1;                   // 合法：单拍启动
                    reg_done  <= 1'b0;                   // 启动时清 done
                end
            end
            // done 必须是"保持到下一次启动"（HLS ap_done 语义），否则 PS 轮询读不到。
            // 本行写在接受分支之后 ⇒ 若同拍既有 eng_done 又有新启动，done 会被置 1
            // （下一拍新启动的接受分支再清 0），行为安全。
            if (eng_done || done_stub) reg_done <= 1'b1;
        end
    end

    // ========================================================================
    // 3. 引擎例化（一个端口都没改）
    // ========================================================================
    logic        e_in_arvalid, e_in_arready;
    logic [31:0] e_in_araddr;
    logic [7:0]  e_in_arlen;
    logic        e_in_rvalid,  e_in_rready;
    logic [63:0] e_in_rdata;
    logic        e_w_arvalid,  e_w_arready;
    logic [31:0] e_w_araddr;
    logic [7:0]  e_w_arlen;
    logic        e_w_rvalid,   e_w_rready;
    logic [63:0] e_w_rdata;
    logic        e_b_arvalid,  e_b_arready;
    logic [31:0] e_b_araddr;
    logic [7:0]  e_b_arlen;
    logic        e_b_rvalid,   e_b_rready;
    logic [63:0] e_b_rdata;

    conv_k5_engine #(
        .CIN         (CIN),
        .COUT        (COUT),
        .W           (W),
        .H           (H),
        .PAD         (PAD),
        .OC_PER_PASS (OC_PER_PASS)
    ) u_eng (
        .ap_clk (clk),
        .ap_rst_n (rst_n),

        .m_axi_in_arvalid (e_in_arvalid), .m_axi_in_arready (e_in_arready),
        .m_axi_in_araddr  (e_in_araddr),  .m_axi_in_arlen   (e_in_arlen),
        .m_axi_in_rvalid  (e_in_rvalid),  .m_axi_in_rready  (e_in_rready),
        .m_axi_in_rdata   (e_in_rdata),   .m_axi_in_rlast   (1'b0),

        .m_axi_w_arvalid  (e_w_arvalid),  .m_axi_w_arready  (e_w_arready),
        .m_axi_w_araddr   (e_w_araddr),   .m_axi_w_arlen    (e_w_arlen),
        .m_axi_w_rvalid   (e_w_rvalid),   .m_axi_w_rready   (e_w_rready),
        .m_axi_w_rdata    (e_w_rdata),    .m_axi_w_rlast    (1'b0),

        .m_axi_b_arvalid  (e_b_arvalid),  .m_axi_b_arready  (e_b_arready),
        .m_axi_b_araddr   (e_b_araddr),   .m_axi_b_arlen    (e_b_arlen),
        .m_axi_b_rvalid   (e_b_rvalid),   .m_axi_b_rready   (e_b_rready),
        .m_axi_b_rdata    (e_b_rdata),    .m_axi_b_rlast    (1'b0),

        .m_axi_out_awvalid(m_axi_out_awvalid), .m_axi_out_awready(m_axi_out_awready),
        .m_axi_out_awaddr (m_axi_out_awaddr),  .m_axi_out_awlen  (m_axi_out_awlen),
        .m_axi_out_wvalid (m_axi_out_wvalid),  .m_axi_out_wready (m_axi_out_wready),
        .m_axi_out_wdata  (m_axi_out_wdata),   .m_axi_out_wstrb  (m_axi_out_wstrb),
        .m_axi_out_wlast  (m_axi_out_wlast),
        .m_axi_out_bvalid (m_axi_out_bvalid),  .m_axi_out_bready (m_axi_out_bready),

        .ctrl_in_addr (addr_in),  .ctrl_wgt_addr (addr_wgt),
        .ctrl_bias_addr (addr_bias), .ctrl_out_addr (addr_out),
        .ctrl_shift (cfg_shift),          // k5 的 ctrl_shift 是 32 位（>> 用寄存器值）
        .ctrl_cin (cfg_cin), .ctrl_cout (cfg_cout),
        .ctrl_h (cfg_h), .ctrl_w (cfg_w),
        .ctrl_start (eng_start),          // ← 已寄存两拍，切断配置检查到引擎 CE 的长链
        .ctrl_done (eng_done), .ctrl_busy (eng_busy)
        // ⚠ k5 引擎**没有** ctrl_dil / ctrl_relu / ctrl_pad / ctrl_ksize 端口，
        //   也没有 dbg_stall_* 端口 —— 见文件头"与 k3 的差异" 1)/2)
    );

    // ========================================================================
    // 4. (边界 3) 三读合并 + 越界钳制 + sticky
    // ========================================================================
    logic        rd_busy;
    logic [1:0]  rd_tag;
    logic [1:0]  rd_sel;

    wire r_last_hs = m_axi_rd_rvalid && m_axi_rd_rready && m_axi_rd_rlast;

    assign rd_sel = e_in_arvalid ? 2'd0 :
                    e_w_arvalid  ? 2'd1 : 2'd2;

    wire any_req  = e_in_arvalid || e_w_arvalid || e_b_arvalid;
    wire ar_hs    = m_axi_rd_arvalid && m_axi_rd_arready;

    // 各区的合法上界（最后一个 8 字节字的地址）
    wire [31:0] end_in = addr_in   + bytes_in   - 32'd8;
    wire [31:0] end_wg = addr_wgt  + bytes_wg   - 32'd8;
    wire [31:0] end_bi = addr_bias + bytes_bi   - 32'd8;

    // 钳制：越界就读该区最后一个合法字（不读穿总线）
    wire [31:0] a_in_raw = e_in_araddr;
    wire [31:0] a_wg_raw = e_w_araddr;
    wire [31:0] a_bi_raw = e_b_araddr;

    wire oob_in = (a_in_raw < addr_in)   || (a_in_raw > end_in);
    wire oob_wg = (a_wg_raw < addr_wgt)  || (a_wg_raw > end_wg);
    wire oob_bi = (a_bi_raw < addr_bias) || (a_bi_raw > end_bi);

    wire [31:0] a_in = oob_in ? end_in : a_in_raw;
    wire [31:0] a_wg = oob_wg ? end_wg : a_wg_raw;
    wire [31:0] a_bi = oob_bi ? end_bi : a_bi_raw;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rd_busy <= 1'b0;
            rd_tag  <= 2'd0;
            sticky_oob <= 1'b0;
        end else begin
            if (!rd_busy) begin
                if (ar_hs) begin
                    rd_busy <= 1'b1;
                    rd_tag  <= rd_sel;
                    if ((rd_sel == 2'd0 && oob_in) ||
                        (rd_sel == 2'd1 && oob_wg) ||
                        (rd_sel == 2'd2 && oob_bi)) sticky_oob <= 1'b1;
                end
            end else if (r_last_hs) begin
                rd_busy <= 1'b0;
            end
        end
    end

    assign m_axi_rd_arvalid = !rd_busy && any_req;
    assign m_axi_rd_araddr  = (rd_sel == 2'd0) ? a_in :
                              (rd_sel == 2'd1) ? a_wg : a_bi;
    assign m_axi_rd_arlen   = (rd_sel == 2'd0) ? e_in_arlen :
                              (rd_sel == 2'd1) ? e_w_arlen : e_b_arlen;
    assign m_axi_rd_arid    = rd_sel;

    // 各引擎的 arready：只有"被选中且合并口握手"才回 1
    assign e_in_arready = !rd_busy && any_req && (rd_sel == 2'd0) && m_axi_rd_arready;
    assign e_w_arready  = !rd_busy && any_req && (rd_sel == 2'd1) && m_axi_rd_arready;
    assign e_b_arready  = !rd_busy && any_req && (rd_sel == 2'd2) && m_axi_rd_arready;

    // R 通道按锁存 tag 回路由
    assign m_axi_rd_rready = (rd_tag == 2'd0) ? e_in_rready :
                             (rd_tag == 2'd1) ? e_w_rready  : e_b_rready;
    assign e_in_rvalid = m_axi_rd_rvalid && (rd_tag == 2'd0);
    assign e_w_rvalid  = m_axi_rd_rvalid && (rd_tag == 2'd1);
    assign e_b_rvalid  = m_axi_rd_rvalid && (rd_tag == 2'd2);
    assign e_in_rdata  = m_axi_rd_rdata;
    assign e_w_rdata   = m_axi_rd_rdata;
    assign e_b_rdata   = m_axi_rd_rdata;

    // ========================================================================
    // 5. STALL_FILL / STALL_MAC（0xA0 / 0xA4）：总线反压拍数
    //
    //  为什么不是 k3 那两个计数器：k5 引擎 FSM 顺序执行、没有填充 FIFO/抽头仲裁，
    //  没有可导出的"等填充 / 等抽头"分类。本层改在**总线边界**统计，
    //  语义是"互连/DDR 没接住这次握手"的拍数：
    //    STALL_FILL：读 AR 有效但未被接受，或 R 通道该给数却没给
    //    STALL_MAC ：写 AW 有效但未被接受，或 W 有效但未被接受
    //  判读方式不变：① 两者都≈0 而拍数偏高 ⇒ 逻辑/参数问题；
    //                ② STALL_FILL 与时间线性增长 ⇒ DDR/互连供数不足。
    //  ⚠ 底噪：每个读 burst 都有 1~3 拍固有延迟（AR→首拍 R），它们会被算进
    //     STALL_FILL。真实层的 burst 数 ≈ 权重 16128B/2048B ≈ 8 个 + bias 1 个
    //     + 每个输入行 (W×2 块)=640 beat ≈ 3 个 × 320 行 ≈ 960 个 ⇒ 底噪约
    //     1~3 千拍，相对 59 M 拍 < 0.01%，不影响判读；但**不要**期望它恰好为 0。
    //  每次 eng_start 清零（PS 在 done 之后读到的是本层的累计值）。
    // ========================================================================
    wire rd_backpressure = (m_axi_rd_arvalid && !m_axi_rd_arready) ||
                           (rd_busy && m_axi_rd_rready && !m_axi_rd_rvalid);
    wire wr_backpressure = (m_axi_out_awvalid && !m_axi_out_awready) ||
                           (m_axi_out_wvalid  && !m_axi_out_wready);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            dbg_fill <= 32'd0;
            dbg_mac  <= 32'd0;
        end else if (eng_start) begin
            dbg_fill <= 32'd0;
            dbg_mac  <= 32'd0;
        end else begin
            if (rd_backpressure) dbg_fill <= dbg_fill + 32'd1;
            if (wr_backpressure) dbg_mac  <= dbg_mac  + 32'd1;
        end
    end

    // ========================================================================
    // 6. 调试（仅在 -d K5_WRAP_DBG 时存在；放在**最后**，只引用上面已声明的信号）
    // ========================================================================
    // synopsys translate_off
`ifdef K5_WRAP_DBG
    initial begin
        $display("[K5-WRAP] CIN=%0d COUT=%0d W=%0d H=%0d PAD=%0d OC_PER_PASS=%0d",
                 CIN, COUT, W, H, PAD, OC_PER_PASS);
    end
`endif
    // synopsys translate_on

endmodule
