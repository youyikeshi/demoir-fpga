// ============================================================================
//  k3_axi_wrap.v —— conv_k3_engine 的 AXI4-Lite 控制包装
//
//  与 k1_axi_wrap 的**寄存器映射完全一致**（PS 侧 conv_engine.py 零改动，只换基地址）：
//    ctrl 页（A[16]=1，PS 基地址 0x4001_0000）
//      0x00 AP_CTRL    W: bit0=start（自清）   R: [0]=0,[1]=done,[2]=idle,[3]=ready
//      0x10 AP_RETURN  R: 0=成功；1..9=k3_cfg_check 的错误码；0xA=地址窗口越界
//      0x18 H    0x20 W    0x28 HOUT  0x30 WOUT  0x38 CIN  0x40 COUT  0x48 KSIZE
//      0x50 STRIDE  0x58 DIL  0x60 PAD  0x68 SHIFT  0x70 RELU
//      0x94 STATUS     R: [0]done [1]busy [2]err [7:4]err_code [8]idle
//                         [9]sticky_oob（只读，粘性：越界读被钳制过就置位，仅复位清）
//      0xA0 STALL_FILL R: 行末等填充满的拍数（只读，判 DDR 供数）
//      0xA4 STALL_MAC  R: 抽头/输出未就绪的拍数（只读，应≈0）
//      0x98 WIN_LO    RW: 允许访问的地址窗口下界（默认 0）
//      0x9C WIN_HI    RW: 允许访问的地址窗口上界（默认 0xFFFF_FFFF）
//    offs 页（A[16]=0，PS 基地址 0x4000_0000）
//      0x10 IN_ADDR(lo) 0x14 IN_ADDR(hi)
//      0x1C WGT_ADDR    0x20 WGT_ADDR(hi)
//      0x28 BIAS_ADDR   0x2C BIAS_ADDR(hi)
//      0x34 OUT_ADDR    0x38 OUT_ADDR(hi)
//
//  ★ 本层被当作**安全边界**（不是胶水），三条硬约束：
//    1) 配置合法性：启动前跑 k3_cfg_check；非法 ⇒ 写 ERR 寄存器 + **自打 done**
//       （PS 的"启动→等 done"流程不会挂死），且**不拉 ctrl_start**。
//    2) 地址窗口：四个区 [addr, addr+bytes) 必须整体落在 [WIN_LO, WIN_HI) 内，
//       否则同 1) 拒绝启动并给错误码 0xA。
//    3) 越界兜底 + sticky：即使参数全合法，合并读口也把 AR 地址**钳**在所属区的
//       [base, base+bytes) 内（读最后一个合法 8 字节），并置 sticky_oob。
//       TB 断言 sticky_oob 恒 0；上板时 STATUS[9] 就是一盏"地址算错了"的灯。
//
//  AXI4 master 从 4 个减到 2 个：in/wgt/bias 三个读引擎在内部合并成 1 个读口
//  （2-bit ARID tag：0=in / 1=wgt / 2=bias），R 通道按锁存的 tag 回路由；
//  授权从 AR 握手保持到该 burst 的 RLAST 握手，burst 内绝不换人。
//  输出口原样透传（AWLEN=0，一像素一 beat）。
//
//  时钟/复位：s_axi_aclk / s_axi_aresetn（低有效）同时驱动引擎的 ap_clk/ap_rst_n。
// ============================================================================
`timescale 1ns / 1ps

module k3_axi_wrap #(
    parameter int LANES  = 8,
    parameter int CAP_B  = 4608
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
    // ⚠ 时序关键（2026-10-27 BD 综合 WNS -43ns / 101 级逻辑的根因）：
    //   原来 `.ctrl_start(do_start && cfg_ok && cfg_win_ok)` 把
    //   **k3_cfg_check 的 9 条规则 + 4 区 33 位窗口比较（约 70 级组合）**
    //   直接串到了引擎内部的时钟使能上。配置检查只需要"启动前稳定"，不需要"同拍到达"，
    //   所以打成两拍：start_ok_r（配置许可）→ eng_start（启动脉冲）。
    //   代价：PS 看到 start 晚 2 拍（13 ns），PS 本来就在轮询 done ⇒ 无影响。
    logic        eng_start;
    logic        start_pend;         // 启动请求挂起（等配置派生量稳定）
    logic [3:0]  cfg_settle;         // 配置写后 ≥8 拍才认为派生量(bytes)已稳定
    // 提前声明：0xA8/0xAC/0xB0 调试寄存器要读它们（赋值在 §2）
    wire         cfg_ok, cfg_win_ok;
    wire [3:0]   cfg_err;
    logic [3:0]  cfg_err_r;          // cfg_err 打一拍：截断"cfg_check → 寄存器 D"的链
    // ⚠ **一致性要求**：Lite 读回(0xA8)与"启动决策"必须用**同一份寄存值**，
    //   否则会出现"自检说合法、实际被拒"（或反之），上板极难排查（2026-10-27）。
    logic        cfg_ok_r, cfg_win_ok_r;
    wire         ok_in, ok_wg, ok_bi, ok_ou;
    logic [31:0] bytes_in, bytes_wg, bytes_bi, bytes_ou;   // 由 always_ff 驱动（流水），必须是 logic
    logic [31:0] dbg_fill, dbg_mac;     // §18.3：stall 分类计数（只读）
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
            cfg_cin <= 0; cfg_cout <= 0; cfg_ksize <= 3;
            cfg_stride_d <= 1; cfg_dil_d <= 1; cfg_pad <= 1;
            cfg_shift <= 0; cfg_relu_d <= 1;
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
                        12'h0A: cfg_hout   <= w_data_l;      // 0x28
                        12'h0C: cfg_wout   <= w_data_l;      // 0x30
                        12'h0E: cfg_cin    <= w_data_l;      // 0x38
                        12'h10: cfg_cout   <= w_data_l;      // 0x40
                        12'h12: cfg_ksize  <= w_data_l;      // 0x48
                        12'h14: cfg_stride_d <= w_data_l;    // 0x50
                        12'h16: cfg_dil_d  <= w_data_l;      // 0x58
                        12'h18: cfg_pad    <= w_data_l;      // 0x60
                        12'h1A: cfg_shift  <= w_data_l;      // 0x68
                        12'h1C: cfg_relu_d <= w_data_l;      // 0x70
                        12'h26: win_lo     <= w_data_l;      // 0x98
                        12'h27: win_hi     <= w_data_l;      // 0x9C
                        default: ;
                    endcase
                end else begin
                    case (woff)
                        12'h04: addr_in   <= w_data_l;       // 0x10
                        12'h05: ;                            // 0x14 hi（必须 0）
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
    // 位布局（与 HLS 的 AP_CTRL/STATUS 约定一致）：
    //   [0]done [1]busy [2]err [7:4]err_code [8]idle [9]sticky_oob
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
    wire       cfg_relu   = cfg_relu_d[0];

    // 各区字节数（与 k3_cfg_check / 引擎内部的记账口径逐条对齐）
    // ⚠ 时序（2026-10-27 BD：WNS −37.5ns / 100 级，Source=cfg_w, Dest=done_stub）：
    //   原来这三行是 **32 位乘法器串联**（`ngrp*8*H*W*8` 是三个 32×32），
    //   每级 ~30 级逻辑 ⇒ 三级 ≈ 90 级，正是那 100 级的来源。
    //   这些量**只在启动前**需要正确 ⇒ 改成三级流水 + 收窄操作数（真实位宽：
    //   ⌈cin/8⌉·8 ≤ 192(8b)、H/W ≤ 320(9b)、cout/cin ≤ 192(8b)）。
    wire [8:0]  ngrp_o   = (cfg_cout[8:0] + 9'd7) >> 3;             // ⌈cout/8⌉
    wire [8:0]  ngrp_i   = (cfg_cin[8:0]  + 9'd7) >> 3;             // ⌈cin/8⌉（cin 必为 8 的倍数）
    wire [8:0]  ngrp_x8  = {ngrp_i[5:0], 3'b000};                    // ×8 用拼接
    wire [8:0]  cout_n   = cfg_cout[8:0];
    wire [8:0]  cin_n    = cfg_cin[8:0];
    wire [8:0]  h_n      = cfg_h[8:0];
    wire [8:0]  w_n      = cfg_w[8:0];
    // Hout/Wout 由 (X-1)/S+1 派生（与引擎内部同式；S∈{1,2} ⇒ 用移位）
    wire [9:0]  hout_d   = ((cfg_h[9:0] - 10'd1) >> (cfg_stride[3:0] - 4'd1)) + 10'd1;
    wire [9:0]  wout_d   = ((cfg_w[9:0] - 10'd1) >> (cfg_stride[3:0] - 4'd1)) + 10'd1;
    wire [8:0]  ngrp_ox8 = {ngrp_o[5:0], 3'b000};

    // 三级流水：第1级两两相乘 → 第2级再乘 → 第3级移位成字节数
    logic [8:0]  h_n_r, w_n_r;            // 配置窄位宽的寄存版（乘法两端都成本地寄存器）
    logic [9:0]  hout_r10, wout_r10;      // 同上（派生 Hout/Wout）
    logic [8:0]  cout_n_r, cin_n_r, ngrpi_x8_r, ngrpo_x8_r;
    logic [16:0] pA_in, pA_ou, pB_wg;
    logic [25:0] pB_in, pB_ou;
    logic [18:0] pB_wg2;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            pA_in <= 17'd0; pA_ou <= 17'd0; pB_wg <= 17'd0;
            pB_in <= 26'd0; pB_ou <= 26'd0; pB_wg2 <= 19'd0;
            bytes_in <= 32'd0; bytes_ou <= 32'd0;
            bytes_wg <= 32'd0; bytes_bi <= 32'd0;
        end else begin
            // 第 0 级：先寄存配置窄位宽（截断跨模块走线）
            h_n_r   <= cfg_h[8:0];
            w_n_r   <= cfg_w[8:0];
            hout_r10 <= hout_d;
            wout_r10 <= wout_d;
            cout_n_r <= cfg_cout[8:0];
            cin_n_r  <= cfg_cin[8:0];
            ngrpi_x8_r <= ngrp_x8;
            ngrpo_x8_r <= ngrp_ox8;
            // 第 1 级：用寄存后的操作数
            pA_in <= ngrpi_x8_r * h_n_r;    // 9×9
            pA_ou <= ngrpo_x8_r * {1'b0, hout_r10[8:0]};
            pB_wg <= cout_n_r * cin_n_r;              // 9×9
            pB_in <= pA_in    * w_n_r;                // 17×9
            pB_ou <= pA_ou    * {1'b0, wout_r10[8:0]};
            pB_wg2 <= pB_wg   * 19'd12;
            bytes_in <= {pB_in, 3'b000};    // ×8 用拼接，不占资源
            bytes_ou <= {pB_ou, 3'b000};
            bytes_wg <= {13'd0, pB_wg2};
            bytes_bi <= {21'd0, cout_n_r, 2'b00}; // cout*4 用拼接（21+9+2 = 32）
        end
    end

    // (1) 配置合法性
    k3_cfg_check u_chk (
        .cfg_cin    (cfg_cin),
        .cfg_cout   (cfg_cout),
        .cfg_h      (cfg_h),
        .cfg_w      (cfg_w),
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

    // (2) 地址窗口：每区 [addr, addr+bytes) ⊆ [WIN_LO, WIN_HI]，**展开成 33 位比较**
    //     （原来用一个 automatic function 写，仿真里结果与算术不符 —— 展开后可直接观测）
    wire [32:0] e_in = {1'b0, addr_in}   + {1'b0, bytes_in};
    wire [32:0] e_wg = {1'b0, addr_wgt}  + {1'b0, bytes_wg};
    wire [32:0] e_bi = {1'b0, addr_bias} + {1'b0, bytes_bi};
    wire [32:0] e_ou = {1'b0, addr_out}  + {1'b0, bytes_ou};

    assign ok_in = ({1'b0, addr_in}   >= {1'b0, win_lo}) && (e_in <= {1'b0, win_hi});
    assign ok_wg = ({1'b0, addr_wgt}  >= {1'b0, win_lo}) && (e_wg <= {1'b0, win_hi});
    assign ok_bi = ({1'b0, addr_bias} >= {1'b0, win_lo}) && (e_bi <= {1'b0, win_hi});
    assign ok_ou = ({1'b0, addr_out}  >= {1'b0, win_lo}) && (e_ou <= {1'b0, win_hi});

    assign cfg_win_ok = ok_in && ok_wg && ok_bi && ok_ou;

    // 拒绝启动：非法配置 / 窗口越界 ⇒ 写 ERR + 自打 done，不拉 ctrl_start
    // （原来的组合 `start_ok = cfg_ok && cfg_win_ok` 已删除：启动决策改在 start_go 那一拍
    //   用**寄存版** cfg_ok_r/cfg_win_ok_r，与 Lite 读口 0xA8 用同一份值 ⇒ 一致性有保证）
    logic done_stub;            // 被拒绝时的一拍 done

    // 启动流程（两级）：
    //   do_start（Lite 写脉冲）→ start_pend 挂起 → 等 cfg_settle≥8（bytes 三级流水算完）
    //   → 统一"接受/拒绝"：合法则打出一拍 eng_start，非法则写 reg_err + 自打 done
    //   ⇒ 决策时用的是**寄存后的 bytes**，且组合逻辑只到寄存器 D 端
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
            //   always_ff 驱动（xvlog 不报、xelab 报 VRFC 10-3818，2026-10-26 踩到）
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
            // 本行写在接受分支之后 ⇒ 若同拍既有 eng_done 又有新启动，done 会被置 1（下一拍
            // 新启动的接受分支再清 0），行为安全。
            if (eng_done || done_stub) reg_done <= 1'b1;
        end
    end

    // ========================================================================
    // 3. 引擎例化
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
    conv_k3_engine #(
        .LANES (LANES),
        .CAP_B (CAP_B)
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
        .ctrl_shift (cfg_shift[4:0]),
        .ctrl_cin (cfg_cin), .ctrl_cout (cfg_cout),
        .ctrl_h (cfg_h), .ctrl_w (cfg_w),
        .ctrl_stride (cfg_stride), .ctrl_dil (cfg_dil), .ctrl_relu (cfg_relu),
        .ctrl_start (eng_start),          // ← 已寄存两拍，切断配置检查到引擎 CE 的长链
        .ctrl_done (eng_done), .ctrl_busy (eng_busy),

        .dbg_stall_fill (dbg_fill), .dbg_stall_mac (dbg_mac)
    );

    // ========================================================================
    // 4. 三读合并 + 越界钳制 + sticky
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

endmodule
