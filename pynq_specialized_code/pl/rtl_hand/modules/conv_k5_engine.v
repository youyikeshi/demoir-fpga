// ============================================================================
//  conv_k5_engine.v —— 手写 5×5 卷积加速器（k=5 专用，替换 HLS 的 LOOP_892_47）
//
//  目标层：encoder.conv_first
//      H=320 W=320  cin=12(逻辑) -> 物理 16  cout=48  k=5  pad=2  stride=1  ReLU
//
//  结构（详见 docs/rtl_hand/conv_k5_engine.md）：
//    1. AXI4 master（64-bit）把输入行 / 权重 / bias 读进来，把输出行写回去。
//       读请求用「字节地址 + 字节长度」；写请求用「字节地址 + 64-bit 字数」。
//    2. 5 条行缓冲（每条 = 2 个 64-bit bank，对应 C8 的两个 8 通道块）。
//       每拍只读 1 个地址 => 一列 5 行同时吐出，彻底消掉 HLS 版 II=3 的列口瓶颈。
//    3. 5×5×16B 窗口移位寄存器：每输出一个像素只移入 1 列（5 行）。
//    4. 25 抽头 × OC_PER_PASS 个 oc = 25*OC_PER_PASS 个 DSP 累加器：
//          每个 DSP 负责一个 (oc, tap)，在 12 个 ic 上做 P += a*b。
//          12 拍后 25 个抽头和再求和 => 每像素每 pass 12 拍。
//    5. 权重整层预装进 25*OC_PER_PASS 个分布式 RAM
//          （每 (oc_lane,tap) 一个 NGRP*16 深 × 8 位），内层 0 次 DDR 权重读。
//    6. 输出按 C8 布局打包：每个 pass 写 OC_PER_PASS 个字节（byte strobe），
//       一个 8-oc 块的 8 个字节拼满一个 64-bit beat 后再整行写回。
//
//  OC 并行度参数（DSP vs 时间，详见 docs/rtl_hand/conv_k5_engine.md §5.5）：
//    OC_PER_PASS=4：100 DSP，整层计算 14,745,600 拍（≈98 ms  @150MHz）
//    OC_PER_PASS=2： 50 DSP，整层计算 29,491,200 拍（≈197 ms）
//    OC_PER_PASS=1： 25 DSP，整层计算 58,982,400 拍（≈393 ms）  <-- 默认
//    整机 DSP 预算 160：HLS 92 + k1 33 = 125，只剩 35 个；k5 必须 ≤40 DSP。
//    k=5 全网只有 1 层（约 4.5% 的 MAC），用 100 DSP 极不划算，故默认 1。
//
//  II 说明（默认 OC_PER_PASS=1）：
//    每 (pixel, pass) = CIN 个 ic 拍，pass 与 pass 背靠背，II=1 不变；
//    每像素 = (COUT/OC_PER_PASS) 个 pass = 48×12 = 576 拍（原来是 12×12=144）。
//    计算拍数 = H*W*(COUT/OC_PER_PASS)*CIN = 320*320*48*12 = 58,982,400 拍
//    @150MHz = 393 ms；加上权重装载/行填充/写回，整层约 0.40 s。
//    2×320 条带（golden k5_12x48）约 392 k 拍，是旧 115,939 拍的 ~3.4 倍，
//    相对 HLS 的 3.39 s 仍快约 8.5 倍。
//
//  本文件不例化 DSP48E1 原语，而是写「3 级流水 + use_dsp」的可综合行为：
//      stage1: a_r/b_r（DSP 的 A/B 寄存器）
//      stage2: m_r = a_r*b_r（DSP 的 M 寄存器）
//      stage3: p_acc += m_r（DSP 的 P 寄存器）
//    综合会把每个 (oc,tap) 映射成 1 个 DSP48E1；仿真不依赖 UNISIM。
// ============================================================================
`timescale 1ns / 1ps

module conv_k5_engine #(
    parameter int CIN  = 12,          // 逻辑输入通道（物理 CPAD = 补齐到 8 的倍数）
    parameter int COUT = 48,
    parameter int W    = 320,
    parameter int H    = 320,
    parameter int PAD  = 2,
    // 每个 pass 并行计算的 oc 数。必须整除 8（输出按 8-oc beat 拼包）。
    // 1 => 25 DSP（默认，满足整机 ≤40 DSP 预算）；2/4 仅用于权衡实验。
    parameter int OC_PER_PASS = 1
)(
    input  logic         ap_clk,
    input  logic         ap_rst_n,

    // ------------------------------------------------------------------
    // 读口 0：输入张量（C8 布局，64-bit beat）
    // ------------------------------------------------------------------
    output logic         m_axi_in_arvalid,
    input  logic         m_axi_in_arready,
    output logic [31:0]  m_axi_in_araddr,
    output logic [7:0]   m_axi_in_arlen,
    input  logic         m_axi_in_rvalid,
    output logic         m_axi_in_rready,
    input  logic [63:0]  m_axi_in_rdata,
    input  logic         m_axi_in_rlast,

    // ------------------------------------------------------------------
    // 读口 1：权重（PAD28 容器，64-bit beat）
    // ------------------------------------------------------------------
    output logic         m_axi_w_arvalid,
    input  logic         m_axi_w_arready,
    output logic [31:0]  m_axi_w_araddr,
    output logic [7:0]   m_axi_w_arlen,
    input  logic         m_axi_w_rvalid,
    output logic         m_axi_w_rready,
    input  logic [63:0]  m_axi_w_rdata,
    input  logic         m_axi_w_rlast,

    // ------------------------------------------------------------------
    // 读口 2：bias（int32 × cout，64-bit beat）
    // ------------------------------------------------------------------
    output logic         m_axi_b_arvalid,
    input  logic         m_axi_b_arready,
    output logic [31:0]  m_axi_b_araddr,
    output logic [7:0]   m_axi_b_arlen,
    input  logic         m_axi_b_rvalid,
    output logic         m_axi_b_rready,
    input  logic [63:0]  m_axi_b_rdata,
    input  logic         m_axi_b_rlast,

    // ------------------------------------------------------------------
    // 写口：输出张量（C8 布局，64-bit beat）
    // ------------------------------------------------------------------
    output logic         m_axi_out_awvalid,
    input  logic         m_axi_out_awready,
    output logic [31:0]  m_axi_out_awaddr,
    output logic [7:0]   m_axi_out_awlen,
    output logic         m_axi_out_wvalid,
    input  logic         m_axi_out_wready,
    output logic [63:0]  m_axi_out_wdata,
    output logic [7:0]   m_axi_out_wstrb,
    output logic         m_axi_out_wlast,
    input  logic         m_axi_out_bvalid,
    output logic         m_axi_out_bready,

    // ------------------------------------------------------------------
    // 控制寄存器
    // ------------------------------------------------------------------
    input  logic [31:0]  ctrl_in_addr,    // 输入张量基址（C8 布局）
    input  logic [31:0]  ctrl_wgt_addr,   // 权重基址（PAD28 容器）
    input  logic [31:0]  ctrl_bias_addr,
    input  logic [31:0]  ctrl_out_addr,
    input  logic [31:0]  ctrl_shift,      // 输出右移位数
    input  logic [31:0]  ctrl_cin,        // 运行期形状（本模块编译期参数已固定，仅作契约保留）
    input  logic [31:0]  ctrl_cout,
    input  logic [31:0]  ctrl_h,
    input  logic [31:0]  ctrl_w,
    input  logic         ctrl_start,
    output logic         ctrl_done,
    output logic         ctrl_busy
);

    // ==================================================================
    // 0. 派生常量
    // ==================================================================
    localparam int CPAD     = ((CIN + 7) / 8) * 8;   // 物理通道数：12 -> 16
    localparam int CBLK     = CPAD / 8;              // 输入 8 通道块数：16/8 = 2
    localparam int NTAP     = 25;                    // 5×5
    localparam int NOC      = OC_PER_PASS;           // 每个 pass 并行的 oc 数
    localparam int NG       = 8 / NOC;               // 拼满一个 64-bit beat 需 NG 个 pass
    localparam int NGRP     = (COUT + NOC - 1) / NOC;// pass 总数 = ceil(COUT/NOC)
    localparam int NBLK     = (COUT + 7) / 8;        // 8-oc 输出块数（写回 burst 数/像素）
    localparam int NWB      = NOC * NTAP;            // 权重 bank 数 = NOC×25
    localparam int WB_DEPTH = NGRP * CPAD;           // 每 bank 深度 = NGRP×16（1 oc 时 768）
    localparam int WB_AW    = $clog2(WB_DEPTH);      // 权重地址位宽（768 -> 10）
    localparam int LB_DEPTH = W + 2 * PAD;           // 324 = 320 + 左右各 2
    localparam int WGT_BYTES  = COUT * CIN * 28;     // PAD28 容器
    localparam int BIAS_BYTES = COUT * 4;
    localparam int ROW_BYTES  = W * 8;               // 一行一个 8 通道块
    localparam int BLK_BYTES  = H * W * 8;           // 一个输出 8 通道块的字节数
    localparam int TW         = 26;                  // 抽头和位宽（25×12×127² 需 23 bit）
    localparam int IC_LAST    = CIN - 1;
    localparam int X_LAST     = W - 1;
    localparam int Y_LAST     = H - 1;
    localparam int NBLK_LAST  = NBLK - 1;
    localparam int NG_LAST    = NG - 1;

    // 组号 / 块号 / 窗口行号
    logic [5:0]  grp;        // 0..NGRP-1：pass 号 = b_cnt*NG + s_cnt
    logic [2:0]  b_cnt;      // 0..NBLK-1：当前 8-oc 输出块
    logic [2:0]  s_cnt;      // 0..NG-1：块内第几个 pass（每 pass = NOC 个 oc）
    logic [3:0]  ic_cnt;     // 0..CIN-1
    logic [9:0]  x_cnt;      // 0..W-1
    logic [8:0]  y_cnt;      // 0..H-1
    assign grp = b_cnt * NG + s_cnt;

    typedef enum logic [5:0] {
        S_IDLE,
        S_WGT_REQ, S_WGT_DATA,
        S_BIAS_REQ, S_BIAS_DATA,
        S_PRE_REQ,
        S_FILL_REQ, S_FILL_DATA,
        S_ROW_BEGIN,
        S_BLOCK_INIT, S_BLOCK_PRE, S_PRELOAD,
        S_COMPUTE, S_DRAIN, S_FLUSH,
        S_WR_REQ, S_WR_WAIT,
        S_DONE
    } state_t;
    state_t state;

    // ==================================================================
    // 1. AXI4 读引擎
    //    请求：rd_req_addr (字节) + rd_req_bytes (字节，必须 8 的倍数)
    //    数据：rd_data/rd_valid/rd_ready（消费者可反压）
    // ==================================================================
    logic        rd_req_valid, rd_req_ready;
    logic [31:0] rd_req_addr, rd_req_bytes;
    logic [63:0] rd_data;
    logic        rd_valid, rd_ready;
    logic        rd_done;
    logic        rd_arvalid, rd_arready;
    logic [31:0] rd_araddr;
    logic [7:0]  rd_arlen;
    logic        rd_rvalid, rd_rready;
    logic [63:0] rd_rdata;
    logic        rd_rlast;
    logic [1:0]  rd_sel;          // 0=in 1=w 2=b

    typedef enum logic [2:0] {
        RD_IDLE, RD_AR, RD_R
    } rd_state_t;
    rd_state_t   rd_state;
    logic [31:0] rd_addr_q;
    logic [31:0] rd_left_q;
    logic [15:0] rd_burst_beats_q;   // 本 burst 的 beat 数 (<=256)
    logic [15:0] rd_beat_q;

    wire [31:0] rd_bytes_left_beats = rd_left_q >> 3;
    wire [15:0] rd_next_burst = (rd_bytes_left_beats > 256) ? 16'd256
                                                             : rd_bytes_left_beats[15:0];

    assign rd_req_ready = (rd_state == RD_IDLE);
    assign rd_valid  = rd_rvalid;
    assign rd_data   = rd_rdata;
    assign rd_rready = (rd_state == RD_R) && rd_ready;

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            rd_state        <= RD_IDLE;
            rd_addr_q       <= 0;
            rd_left_q       <= 0;
            rd_burst_beats_q<= 0;
            rd_beat_q       <= 0;
            rd_done         <= 1'b0;
            rd_arvalid      <= 1'b0;
            rd_araddr       <= 0;
            rd_arlen        <= 0;
        end else begin
            rd_done <= 1'b0;
            case (rd_state)
                RD_IDLE: begin
                    rd_arvalid <= 1'b0;
                    if (rd_req_valid) begin
                        rd_addr_q  <= rd_req_addr;
                        rd_left_q  <= rd_req_bytes;
                        rd_state   <= RD_AR;
                    end
                end
                RD_AR: begin
                    rd_arvalid       <= 1'b1;
                    rd_araddr        <= rd_addr_q;
                    rd_arlen         <= rd_next_burst[7:0] - 8'd1;
                    rd_burst_beats_q <= rd_next_burst;
                    rd_beat_q        <= 0;
                    if (rd_arvalid && rd_arready) begin
                        rd_arvalid <= 1'b0;
                        rd_state      <= RD_R;
                    end
                end
                RD_R: begin
                    if (rd_rvalid && rd_ready) begin
                        if (rd_beat_q == rd_burst_beats_q - 16'd1) begin
                            rd_addr_q <= rd_addr_q + (rd_burst_beats_q << 3);
                            rd_left_q <= rd_left_q - (rd_burst_beats_q << 3);
                            if (rd_left_q == (rd_burst_beats_q << 3)) begin
                                rd_done  <= 1'b1;
                                rd_state <= RD_IDLE;
                            end else begin
                                rd_state <= RD_AR;
                            end
                        end else begin
                            rd_beat_q <= rd_beat_q + 16'd1;
                        end
                    end
                end
                default: rd_state <= RD_IDLE;
            endcase
        end
    end


    // ------------------------------------------------------------------
    // 1b. 三路读口 mux：同一时刻只有一个读请求在飞
    //     rd_sel: 0=输入 1=权重 2=bias（由主 FSM 状态决定）
    // ------------------------------------------------------------------
    always_comb begin
        rd_sel = 2'd0;
        case (state)
            S_WGT_REQ, S_WGT_DATA:   rd_sel = 2'd1;
            S_BIAS_REQ, S_BIAS_DATA: rd_sel = 2'd2;
            default:                 rd_sel = 2'd0;
        endcase
    end

    always_comb begin
        // 默认全部空闲
        m_axi_in_arvalid = 1'b0;
        m_axi_in_araddr  = 32'd0;
        m_axi_in_arlen   = 8'd0;
        m_axi_in_rready  = 1'b0;
        m_axi_w_arvalid  = 1'b0;
        m_axi_w_araddr   = 32'd0;
        m_axi_w_arlen    = 8'd0;
        m_axi_w_rready   = 1'b0;
        m_axi_b_arvalid  = 1'b0;
        m_axi_b_araddr   = 32'd0;
        m_axi_b_arlen    = 8'd0;
        m_axi_b_rready   = 1'b0;

        case (rd_sel)
            2'd0: begin
                m_axi_in_arvalid = rd_arvalid;
                m_axi_in_araddr  = rd_araddr;
                m_axi_in_arlen   = rd_arlen;
                m_axi_in_rready  = rd_rready;
                rd_arready       = m_axi_in_arready;
                rd_rvalid        = m_axi_in_rvalid;
                rd_rdata         = m_axi_in_rdata;
                rd_rlast         = m_axi_in_rlast;
            end
            2'd1: begin
                m_axi_w_arvalid  = rd_arvalid;
                m_axi_w_araddr   = rd_araddr;
                m_axi_w_arlen    = rd_arlen;
                m_axi_w_rready   = rd_rready;
                rd_arready       = m_axi_w_arready;
                rd_rvalid        = m_axi_w_rvalid;
                rd_rdata         = m_axi_w_rdata;
                rd_rlast         = m_axi_w_rlast;
            end
            default: begin
                m_axi_b_arvalid  = rd_arvalid;
                m_axi_b_araddr   = rd_araddr;
                m_axi_b_arlen    = rd_arlen;
                m_axi_b_rready   = rd_rready;
                rd_arready       = m_axi_b_arready;
                rd_rvalid        = m_axi_b_rvalid;
                rd_rdata         = m_axi_b_rdata;
                rd_rlast         = m_axi_b_rlast;
            end
        endcase
    end

    // ==================================================================
    // 2. 行缓冲（5 条 × 2 个 64-bit bank）
    //    每条行缓冲 324 深：物理列 0..1 = 左 pad，2..W+1 = 真实输入，
    //    W+2..W+3 = 右 pad。pad 列靠读侧 col_ok 强制 0，不写。
    // ==================================================================
    (* ram_style = "block" *) logic [63:0] lb_lo [0:4][0:LB_DEPTH-1];
    (* ram_style = "block" *) logic [63:0] lb_hi [0:4][0:LB_DEPTH-1];
    logic [4:0] lb_valid;              // 每条行缓冲当前装的是有效行？
    logic [2:0] wr_line;               // 当前要填充的行缓冲号 (r % 5)
    logic [2:0] sel [0:4];             // 窗口第 r 行 -> 行缓冲号

    // 行缓冲读端口
    logic [9:0]   lb_raddr;
    logic [9:0]   lb_raddr_d1;
    logic [127:0] lb_rd [0:4];

    always_ff @(posedge ap_clk) begin
        lb_raddr_d1 <= lb_raddr;
        for (int i = 0; i < 5; i++) begin
            lb_rd[i] <= {lb_hi[i][lb_raddr], lb_lo[i][lb_raddr]};
        end
    end

    // 行缓冲写端口（填充时每拍 1 个 64-bit beat）
    logic        fill_we;
    logic [2:0]  fill_line;
    logic        fill_blk;
    logic [9:0]  fill_col;             // 0..W-1
    logic [63:0] fill_data;

    always_ff @(posedge ap_clk) begin
        if (fill_we) begin
            if (fill_blk == 1'b0) lb_lo[fill_line][fill_col + PAD] <= fill_data;
            else                  lb_hi[fill_line][fill_col + PAD] <= fill_data;
        end
    end

    // ==================================================================
    // 3. 5×5 窗口移位寄存器
    //    win[r][c] = 当前输出像素窗口的第 r 行、第 c 列（128 bit = 16 通道）
    // ==================================================================
    logic [127:0] win [0:4][0:4];
    logic         shift_en;
    logic [127:0] shift_pix [0:4];

    wire col_ok = (lb_raddr_d1 >= PAD) && (lb_raddr_d1 < PAD + W);

    always_comb begin
        for (int r = 0; r < 5; r++) begin
            shift_pix[r] = (col_ok && lb_valid[sel[r]]) ? lb_rd[sel[r]] : 128'd0;
        end
    end

    always_ff @(posedge ap_clk) begin
        if (shift_en) begin
            for (int r = 0; r < 5; r++) begin
                win[r][0] <= win[r][1];
                win[r][1] <= win[r][2];
                win[r][2] <= win[r][3];
                win[r][3] <= win[r][4];
                win[r][4] <= shift_pix[r];
            end
        end
    end

    // 当前 ic 的 25 个像素字节（每个窗口位置取第 ic 个通道）
    logic [7:0] pix_byte [0:24];
    always_comb begin
        for (int r = 0; r < 5; r++) begin
            for (int c = 0; c < 5; c++) begin
                pix_byte[r*5+c] = win[r][c][{ic_cnt, 3'b000} +: 8];
            end
        end
    end

    // ==================================================================
    // 4. 权重 bank：NWB = NOC*25 个 WB_DEPTH×8 分布式 RAM
    //    bank 下标 j = oc_lane*25 + tap（oc_lane = 0..NOC-1）
    //    地址 a = grp*CPAD + ic（grp = pass 号，0..NGRP-1）
    //    整层预装：NOC=1 时 25 个 768×8（仍共 153,600 bit，与 100 个 192×8 等量）
    // ==================================================================
    logic [7:0] wgt [0:NWB-1];
    logic [WB_AW-1:0] wgt_raddr;    // = grp*CPAD + ic
    logic       wgt_we;
    logic [WB_AW-1:0] wgt_waddr;
    logic [6:0] wgt_widx;           // = (oc%NOC)*25 + tap
    logic [7:0] wgt_wdata;

    assign wgt_raddr = grp * CPAD + ic_cnt;

    genvar gj;
    generate
        for (gj = 0; gj < NWB; gj++) begin : g_wbank
            k5_wbank #(.DEPTH(WB_DEPTH)) u_wbank (
                .clk   (ap_clk),
                .we    (wgt_we && (wgt_widx == gj[6:0])),
                .waddr (wgt_waddr),
                .wdata (wgt_wdata),
                .raddr (wgt_raddr),
                .rdata (wgt[gj])
            );
        end
    endgenerate

    // ==================================================================
    // 5. bias：COUT×32 分布式 RAM（单读口，每 pass 分 NOC 拍读出 NOC 个 lane）
    // ==================================================================
    logic [31:0] bias_rdata;
    logic [5:0]  bias_raddr;
    logic        bias_we;
    logic [5:0]  bias_waddr;
    logic [31:0] bias_wdata;

    k5_bias_ram #(.DEPTH(64)) u_bias (
        .clk   (ap_clk),
        .we    (bias_we),
        .waddr (bias_waddr),
        .wdata (bias_wdata),
        .raddr (bias_raddr),
        .rdata (bias_rdata)
    );

    // ==================================================================
    // 6. MAC 阵列：25 tap × NOC 个 oc（DSP 数 = 25*NOC）
    // ==================================================================
    logic signed [7:0]  a_r  [0:24];          // 像素（DSP A 寄存器）
    logic signed [7:0]  b_r  [0:NOC-1][0:24]; // 权重（DSP B 寄存器）
    logic signed [15:0] m_r  [0:NOC-1][0:24]; // 乘积（DSP M 寄存器）
    (* use_dsp = "yes" *) logic signed [47:0] p_acc[0:NOC-1][0:24]; // 累加（DSP P 寄存器）

    logic        pipe_en;                   // MAC 流水使能
    logic        mac_first;                 // 当前 ic==0
    logic        mac_last;                  // 当前 ic==CIN-1
    logic        first_d1, first_d2, last_d3;
    logic [5:0]  grp_d1, grp_d2, grp_d3;
    logic [9:0]  x_d1, x_d2, x_d3;
    logic [2:0]  s_d1, s_d2, s_d3;

    // 流水输入（非计算状态下强制 0，避免 drain 期间引入垃圾）
    logic signed [7:0] mac_pix [0:24];
    logic signed [7:0] mac_wgt [0:NOC-1][0:24];
    always_comb begin
        for (int j = 0; j < 25; j++) begin
            mac_pix[j] = (state == S_COMPUTE) ? $signed(pix_byte[j]) : 8'sd0;
        end
        for (int l = 0; l < NOC; l++) begin
            for (int j = 0; j < 25; j++) begin
                mac_wgt[l][j] = (state == S_COMPUTE) ? $signed(wgt[l*25+j]) : 8'sd0;
            end
        end
    end

    (* use_dsp = "yes" *) always_ff @(posedge ap_clk) begin
        if (pipe_en) begin
            for (int j = 0; j < 25; j++) a_r[j] <= mac_pix[j];
            for (int l = 0; l < NOC; l++)
                for (int j = 0; j < 25; j++) b_r[l][j] <= mac_wgt[l][j];

            first_d1 <= mac_first;
            grp_d1   <= grp;
            x_d1     <= x_cnt;
            s_d1     <= s_cnt;

            for (int l = 0; l < NOC; l++)
                for (int j = 0; j < 25; j++)
                    m_r[l][j] <= a_r[j] * b_r[l][j];

            first_d2 <= first_d1;
            grp_d2   <= grp_d1;
            x_d2     <= x_d1;
            s_d2     <= s_d1;

            for (int l = 0; l < NOC; l++) begin
                for (int j = 0; j < 25; j++) begin
                    if (first_d2) p_acc[l][j] <= $signed(m_r[l][j]);
                    else          p_acc[l][j] <= p_acc[l][j] + $signed(m_r[l][j]);
                end
            end

            grp_d3  <= grp_d2;
            x_d3    <= x_d2;
            s_d3    <= s_d2;
        end
    end

    // last 的 3 级延迟（与 grp_d3/x_d3/s_d3 对齐）
    logic last_d1, last_d2;
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            last_d1 <= 1'b0;
            last_d2 <= 1'b0;
            last_d3 <= 1'b0;
        end else if (pipe_en) begin
            last_d1 <= mac_last;
            last_d2 <= last_d1;
            last_d3 <= last_d2;
        end
    end

    // ==================================================================
    // 7. 抽头和树：capture 时把 NOC 个 P 值拍进 26-bit，5 级流水求和
    // ==================================================================
    logic        capture;          // 正常组边界
    logic        drain_capture;    // 块结束的最后一次 capture

    logic signed [TW-1:0] ts0 [0:NOC-1][0:24];
    logic signed [TW-1:0] ts1 [0:NOC-1][0:12];
    logic signed [TW-1:0] ts2 [0:NOC-1][0:6];
    logic signed [TW-1:0] ts3 [0:NOC-1][0:3];
    logic signed [TW-1:0] ts4 [0:NOC-1][0:1];
    logic signed [TW-1:0] tsum [0:NOC-1];

    always_ff @(posedge ap_clk) begin
        if (capture) begin
            for (int l = 0; l < NOC; l++)
                for (int j = 0; j < 25; j++)
                    ts0[l][j] <= p_acc[l][j][TW-1:0];
        end

        // 5 级加法树（每拍一级；capture 间隔 12 拍，远大于 5）
        for (int l = 0; l < NOC; l++) begin
            for (int j = 0; j < 12; j++) ts1[l][j] <= ts0[l][2*j] + ts0[l][2*j+1];
            ts1[l][12] <= ts0[l][24];
            for (int j = 0; j < 6; j++)  ts2[l][j] <= ts1[l][2*j] + ts1[l][2*j+1];
            ts2[l][6] <= ts1[l][12];
            for (int j = 0; j < 3; j++)  ts3[l][j] <= ts2[l][2*j] + ts2[l][2*j+1];
            ts3[l][3] <= ts2[l][6];
            for (int j = 0; j < 2; j++)  ts4[l][j] <= ts3[l][2*j] + ts3[l][2*j+1];
            tsum[l] <= ts4[l][0] + ts4[l][1];
        end
    end

    // capture 有效延迟 6 拍（ts0 寄存器 + 5 级加法树）-> tsum 有效
    logic [5:0] cap_d;
    logic [5:0] cap_grp_d [0:5];
    logic [2:0] cap_s_d   [0:5];
    logic [9:0] cap_x_d   [0:5];
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            cap_d     <= 6'd0;
            for (int i = 0; i < 6; i++) begin
                cap_grp_d[i] <= 6'd0;
                cap_s_d[i]   <= 3'd0;
                cap_x_d[i]   <= 10'd0;
            end
        end else begin
        cap_d[0] <= capture;
        for (int i = 1; i < 6; i++) cap_d[i] <= cap_d[i-1];
        if (capture) begin
            cap_grp_d[0] <= grp_d3;
            cap_s_d[0]   <= s_d3;
            cap_x_d[0]   <= x_d3;
        end
        for (int i = 1; i < 6; i++) begin
            cap_grp_d[i] <= cap_grp_d[i-1];
            cap_s_d[i]   <= cap_s_d[i-1];
            cap_x_d[i]   <= cap_x_d[i-1];
        end
        end
    end

    // ==================================================================
    // 8. bias 读取（capture 后 NOC 拍读出 NOC 个 lane 的 bias）
    // ==================================================================
    logic [2:0]  bias_rd_cnt;
    logic        bias_rd_active;
    logic [5:0]  bias_rd_grp;
    logic [31:0] bias_lane [0:NOC-1];

    // bias RAM 是同步读（地址打 1 拍）。capture 拍先给 lane0，
    // 之后每拍给 lane(cnt+1)，这样 C+1..C+NOC 依次采到 lane0..lane{NOC-1}。
    assign bias_raddr = capture ? (grp_d3 * NOC)
                                : (bias_rd_grp * NOC + bias_rd_cnt + 6'd1);

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            bias_rd_grp    <= 6'd0;
            bias_rd_cnt    <= 3'd0;
            bias_rd_active <= 1'b0;
            for (int l = 0; l < NOC; l++) bias_lane[l] <= 32'd0;
        end else if (capture) begin
            bias_rd_grp    <= grp_d3;
            bias_rd_cnt    <= 3'd0;
            bias_rd_active <= 1'b1;
        end else if (bias_rd_active) begin
            bias_lane[bias_rd_cnt] <= bias_rdata;
            if (bias_rd_cnt == NOC - 1) bias_rd_active <= 1'b0;
            else                        bias_rd_cnt <= bias_rd_cnt + 3'd1;
        end
    end

    // ==================================================================
    // 9. 量化 + 输出打包 + 输出行缓冲
    //    NOC=1 时每个 pass 只产出 1 个字节，按 byte strobe 写进 8 字节 beat；
    //    一个 8-oc 块的 NG 个 pass 都写完后，整行（W 个 64-bit beat）才写回。
    // ==================================================================
    logic [31:0] shift_q;
    logic signed [31:0] q_v [0:NOC-1];
    logic [7:0]  q_byte [0:NOC-1];
    logic        q_valid;
    logic [5:0]  q_grp;
    logic [2:0]  q_s;
    logic [9:0]  q_x;

    always_comb begin
        for (int l = 0; l < NOC; l++) begin
            q_v[l] = $signed(tsum[l]) + $signed(bias_lane[l]);
            q_v[l] = q_v[l] >>> shift_q;
        end
    end
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            q_valid <= 1'b0;
            q_s     <= 3'd0;
            q_x     <= 10'd0;
            q_grp   <= 6'd0;
            for (int l = 0; l < NOC; l++) q_byte[l] <= 8'd0;
        end else if (cap_d[5]) begin
            for (int l = 0; l < NOC; l++) begin
                if (q_v[l] < 0)        q_byte[l] <= 8'd0;
                else if (q_v[l] > 127) q_byte[l] <= 8'd127;
                else                   q_byte[l] <= q_v[l][7:0];
            end
            q_valid <= 1'b1;
            q_grp   <= cap_grp_d[5];
            q_s     <= cap_s_d[5];
            q_x     <= cap_x_d[5];
        end else begin
            q_valid <= 1'b0;
        end
    end

    // 输出行缓冲（W × 64-bit，C8 布局的一个 8-oc 块的一行）
    (* ram_style = "block" *) logic [63:0] obuf [0:W-1];
    logic        obuf_we;
    logic [9:0]  obuf_waddr;
    logic [63:0] obuf_wdata;
    logic [7:0]  obuf_wstrb;
    logic [9:0]  obuf_raddr;
    logic [63:0] obuf_rdata;

    // 本 pass 的 NOC 个字节放到 64-bit beat 的 [q_s*NOC .. q_s*NOC+NOC-1] 字节位置
    always_comb begin
        obuf_wdata = 64'd0;
        obuf_wstrb = 8'd0;
        for (int n = 0; n < NOC; n++) begin
            obuf_wdata[(q_s * NOC + n) * 8 +: 8] = q_byte[n];
            obuf_wstrb[q_s * NOC + n]            = 1'b1;
        end
    end
    assign obuf_we    = q_valid;
    assign obuf_waddr = q_x;

    always_ff @(posedge ap_clk) begin
        if (obuf_we) begin
            for (int k = 0; k < 8; k++) begin
                if (obuf_wstrb[k]) obuf[obuf_waddr][k*8 +: 8] <= obuf_wdata[k*8 +: 8];
            end
        end
        obuf_rdata <= obuf[obuf_raddr];
    end

    // ==================================================================
    // 10. AXI4 写引擎（输出行 burst）
    //     请求：wr_req_addr + wr_req_beats（单位：64-bit 字）
    //     数据：从 obuf 顺序读 W 个 beat
    // ==================================================================
    logic        wr_req_valid, wr_req_ready;
    logic [31:0] wr_req_addr;
    logic [15:0] wr_req_beats;
    logic        wr_done;

    typedef enum logic [2:0] {
        WR_IDLE, WR_AR, WR_FILL, WR_W, WR_B
    } wr_state_t;
    wr_state_t   wr_state;
    logic [31:0] wr_addr_q;
    logic [15:0] wr_total_q;
    logic [15:0] wr_left_q;
    logic [15:0] wr_burst_q;
    logic [15:0] wr_beat_q;
    logic [2:0]  wr_fill_cnt;
    logic [63:0] wr_data_q;

    assign wr_req_ready = (wr_state == WR_IDLE);

    // 说明：obuf 是同步读（地址打 1 拍）。为了做到 1 beat/cycle，
    // 数据通路是 2 级预取：
    //   obuf_raddr = n+2 时，obuf_rdata = beat(n+1)，wr_data_q = beat(n)
    //   发送 beat(n) 的同一拍，把 beat(n+1) 装进 wr_data_q / m_axi_out_wdata。
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            wr_state      <= WR_IDLE;
            wr_addr_q     <= 0;
            wr_total_q    <= 0;
            wr_left_q     <= 0;
            wr_burst_q    <= 0;
            wr_beat_q     <= 0;
            wr_fill_cnt   <= 0;
            wr_data_q     <= 0;
            wr_done       <= 1'b0;
            m_axi_out_awvalid <= 1'b0;
            m_axi_out_wvalid  <= 1'b0;
            m_axi_out_wlast   <= 1'b0;
            m_axi_out_bready  <= 1'b0;
            m_axi_out_awaddr  <= 0;
            m_axi_out_awlen   <= 0;
            m_axi_out_wdata   <= 0;
            obuf_raddr    <= 0;
        end else begin
            wr_done <= 1'b0;
            case (wr_state)
                WR_IDLE: begin
                    m_axi_out_awvalid <= 1'b0;
                    m_axi_out_wvalid  <= 1'b0;
                    m_axi_out_bready  <= 1'b0;
                    if (wr_req_valid) begin
                        wr_addr_q   <= wr_req_addr;
                        wr_total_q  <= wr_req_beats;
                        wr_left_q   <= wr_req_beats;
                        wr_beat_q   <= 0;
                        wr_fill_cnt <= 0;
                        wr_state    <= WR_AR;
                    end
                end

                WR_AR: begin
                    m_axi_out_awvalid <= 1'b1;
                    m_axi_out_awaddr  <= wr_addr_q;
                    wr_burst_q    <= (wr_left_q > 256) ? 16'd256 : wr_left_q;
                    m_axi_out_awlen   <= ((wr_left_q > 256) ? 16'd256 : wr_left_q) - 16'd1;
                    // 本 burst 从「已经发过的 beat 数」开始，支持 256+64 切分
                    obuf_raddr    <= (wr_total_q - wr_left_q);
                    if (m_axi_out_awvalid && m_axi_out_awready) begin
                        m_axi_out_awvalid <= 1'b0;
                        m_axi_out_wvalid  <= 1'b0;
                        wr_fill_cnt   <= 3'd0;
                        wr_state      <= WR_FILL;
                    end
                end

                WR_FILL: begin
                    case (wr_fill_cnt)
                        3'd0: begin
                            obuf_raddr  <= obuf_raddr + 10'd1;  // offset -> offset+1
                            wr_fill_cnt <= 3'd1;
                        end
                        3'd1: begin
                            wr_data_q   <= obuf_rdata;  // beat0
                            wr_fill_cnt <= 3'd2;
                        end
                        default: begin
                            // 此刻 obuf_rdata = beat1，wr_data_q = beat0
                            m_axi_out_wdata  <= wr_data_q;
                            m_axi_out_wlast  <= (wr_burst_q == 16'd1);
                            m_axi_out_wvalid <= 1'b1;
                            obuf_raddr   <= obuf_raddr + 10'd1;  // offset+1 -> offset+2
                            wr_beat_q    <= 16'd0;
                            wr_state     <= WR_W;
                        end
                    endcase
                end

                WR_W: begin
                    if (m_axi_out_wready) begin
                        if (wr_beat_q == wr_burst_q - 16'd1) begin
                            m_axi_out_wvalid <= 1'b0;
                            m_axi_out_wlast  <= 1'b0;
                            m_axi_out_bready <= 1'b1;
                            wr_state     <= WR_B;
                        end else begin
                            m_axi_out_wdata <= obuf_rdata;          // beat(n+1)
                            m_axi_out_wlast <= (wr_beat_q + 16'd2 == wr_burst_q);
                            wr_data_q   <= obuf_rdata;
                            obuf_raddr  <= obuf_raddr + 10'd1;  // -> beat(n+2)
                            wr_beat_q   <= wr_beat_q + 16'd1;
                        end
                    end
                end

                WR_B: begin
                    if (m_axi_out_bvalid) begin
                        m_axi_out_bready <= 1'b0;
                        if (wr_left_q == wr_burst_q) begin
                            wr_done  <= 1'b1;
                            wr_state <= WR_IDLE;
                        end else begin
                            wr_addr_q <= wr_addr_q + (wr_burst_q << 3);
                            wr_left_q <= wr_left_q - wr_burst_q;
                            wr_state  <= WR_AR;
                        end
                    end
                end

                default: wr_state <= WR_IDLE;
            endcase
        end
    end

    assign m_axi_out_wstrb   = 8'hFF;

    // busy 覆盖从 start 被锁存到 done 的整个区间
    assign ctrl_busy = (state != S_IDLE);

    // ==================================================================
    // 11. 主状态机
    // ==================================================================
    // 控制寄存器锁存
    logic [31:0] in_base, wgt_base, bias_base, out_base;

    // 权重装载计数
    logic [5:0]  w_oc;
    logic [3:0]  w_ic;
    logic [4:0]  w_b;
    logic [2:0]  w_lane;
    logic        w_word_valid;
    logic [63:0] w_word;
    logic [15:0] w_byte_cnt;

    // bias 装载计数
    logic [5:0]  b_idx;
    logic        b_lane;
    logic        b_word_valid;
    logic [63:0] b_word;

    // 填充
    logic        fill_mode;        // 0=preload row0/1, 1=主循环
    logic [1:0]  pre_row;
    logic [8:0]  fill_row;
    logic        fill_is_pad;

    // 计算计数器（b_cnt/s_cnt/ic_cnt/x_cnt/y_cnt 已在顶部声明）
    logic [2:0]  pre_cnt;
    logic [2:0]  drain_cnt;
    logic [3:0]  flush_cnt;

    // 当前 capture 的触发
    assign mac_first = (state == S_COMPUTE) && (ic_cnt == 4'd0);
    assign mac_last  = (state == S_COMPUTE) && (ic_cnt == IC_LAST[3:0]);
    assign drain_capture = (state == S_DRAIN) && (drain_cnt == 3'd2);
    assign capture   = (first_d2 && last_d3) || drain_capture;

    // 流水使能：计算 + drain 3 拍
    assign pipe_en = (state == S_COMPUTE) || (state == S_DRAIN);

    // 窗口移位：只在「一个像素的最后一个 pass」且不是本行最后一个像素时移入新列。
    // 1 oc 时 s_cnt 要数满 0..7，像素之间窗口仍然只移 1 列。
    assign shift_en = ((state == S_PRELOAD) && (pre_cnt >= 3'd1) && (pre_cnt <= 3'd5))
                   || ((state == S_COMPUTE) && (ic_cnt == IC_LAST[3:0]) &&
                       (s_cnt == NG_LAST[2:0]) && (x_cnt != X_LAST[9:0]));

    // 行缓冲读地址
    always_comb begin
        if (state == S_PRELOAD) begin
            lb_raddr = (pre_cnt <= 3'd4) ? {7'd0, pre_cnt} : 10'd0;
        end else if (state == S_COMPUTE) begin
            lb_raddr = (x_cnt == X_LAST[9:0]) ? 10'd0 : (x_cnt + 10'd5);
        end else begin
            lb_raddr = 10'd0;
        end
    end

    // 读请求 mux
    always_comb begin
        rd_req_valid = 1'b0;
        rd_req_addr  = 32'd0;
        rd_req_bytes = 32'd0;
        case (state)
            S_WGT_REQ: begin
                rd_req_valid = 1'b1;
                rd_req_addr  = wgt_base;
                rd_req_bytes = WGT_BYTES[31:0];
            end
            S_BIAS_REQ: begin
                rd_req_valid = 1'b1;
                rd_req_addr  = bias_base;
                rd_req_bytes = BIAS_BYTES[31:0];
            end
            S_FILL_REQ: begin
                rd_req_valid = !fill_is_pad;
                rd_req_addr  = in_base + (fill_blk ? BLK_BYTES[31:0] : 32'd0)
                              + fill_row * ROW_BYTES[31:0];
                rd_req_bytes = ROW_BYTES[31:0];
            end
            default: ;
        endcase
    end

    // 读数据消费者 ready
    always_comb begin
        rd_ready = 1'b0;
        case (state)
            S_WGT_DATA:  rd_ready = !w_word_valid;
            S_BIAS_DATA: rd_ready = !b_word_valid;
            S_FILL_DATA: rd_ready = 1'b1;
            default:     rd_ready = 1'b0;
        endcase
    end

    // 写请求 mux
    always_comb begin
        wr_req_valid = (state == S_WR_REQ);
        wr_req_addr  = out_base + (b_cnt * BLK_BYTES[31:0]) + (y_cnt * ROW_BYTES[31:0]);
        wr_req_beats = W[15:0];
    end

    // ------------------------------------------------------------------
    // 主状态机
    // ------------------------------------------------------------------
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            state       <= S_IDLE;
            ctrl_done   <= 1'b0;
            in_base     <= 0;
            wgt_base    <= 0;
            bias_base   <= 0;
            out_base    <= 0;
            shift_q     <= 0;
            y_cnt       <= 0;
            b_cnt       <= 0;
            x_cnt       <= 0;
            s_cnt       <= 3'd0;
            ic_cnt      <= 0;
            pre_cnt     <= 0;
            drain_cnt   <= 0;
            flush_cnt   <= 0;
            wr_line     <= 0;
            lb_valid    <= 0;
            fill_mode   <= 0;
            pre_row     <= 0;
            fill_row    <= 0;
            fill_blk    <= 1'b0;
            fill_col    <= 0;
            fill_is_pad <= 1'b0;
            w_oc        <= 0;
            w_ic        <= 0;
            w_b         <= 0;
            w_lane      <= 0;
            w_word_valid<= 0;
            w_byte_cnt  <= 0;
            wgt_we      <= 0;
            b_idx       <= 0;
            b_lane      <= 1'b0;
            b_word_valid<= 0;
            bias_we     <= 0;
            sel[0]      <= 3'd3;
            sel[1]      <= 3'd4;
            sel[2]      <= 3'd0;
            sel[3]      <= 3'd1;
            sel[4]      <= 3'd2;
        end else begin
            ctrl_done <= 1'b0;
            wgt_we    <= 1'b0;
            bias_we   <= 1'b0;

            case (state)
                // ------------------------------------------------------
                S_IDLE: begin
                    if (ctrl_start) begin
                        in_base   <= ctrl_in_addr;
                        wgt_base  <= ctrl_wgt_addr;
                        bias_base <= ctrl_bias_addr;
                        out_base  <= ctrl_out_addr;
                        shift_q   <= ctrl_shift;
                        y_cnt     <= 0;
                        b_cnt     <= 0;
                        wr_line   <= 0;
                        lb_valid  <= 0;
                        pre_row   <= 0;
                        fill_mode <= 1'b0;
                        // 多 case 复用（不经过 ap_rst_n）时也要回到干净状态
                        sel[0]    <= 3'd3;
                        sel[1]    <= 3'd4;
                        sel[2]    <= 3'd0;
                        sel[3]    <= 3'd1;
                        sel[4]    <= 3'd2;
                        w_oc      <= 0;
                        w_ic      <= 0;
                        w_b       <= 0;
                        w_lane    <= 0;
                        w_word_valid <= 1'b0;
                        w_byte_cnt   <= 0;
                        b_idx     <= 0;
                        b_lane    <= 1'b0;
                        b_word_valid <= 1'b0;
                        pre_cnt   <= 0;
                        drain_cnt <= 0;
                        flush_cnt <= 0;
                        ic_cnt    <= 0;
                        s_cnt     <= 3'd0;
                        x_cnt     <= 0;
                        state     <= S_WGT_REQ;
                    end
                end

                // ------------------------------------------------------
                // 权重装载：PAD28 容器逐字节流式写入 NWB 个 bank
                //   oc 全局下标 -> bank = (oc%NOC)*25 + tap，地址 = (oc/NOC)*CPAD + ic
                // ------------------------------------------------------
                S_WGT_REQ: begin
                    if (rd_req_ready) state <= S_WGT_DATA;
                end
                S_WGT_DATA: begin
                    if (rd_valid && rd_ready) begin
                        w_word       <= rd_data;
                        w_word_valid <= 1'b1;
                        w_lane       <= 0;
                    end
                    if (w_word_valid) begin
                        // 写当前字节（只有 b<25 是有效抽头）
                        if (w_b < 5'd25) begin
                            wgt_we    <= 1'b1;
                            wgt_widx  <= (w_oc % NOC) * 7'd25 + w_b[4:0];
                            wgt_waddr <= (w_oc / NOC) * CPAD + w_ic;
                            wgt_wdata <= w_word[{w_lane, 3'b000} +: 8];
                        end
                        // 推进字节计数器
                        w_byte_cnt <= w_byte_cnt + 16'd1;
                        w_lane     <= w_lane + 3'd1;
                        if (w_lane == 3'd7) w_word_valid <= 1'b0;
                        if (w_b == 5'd27) begin
                            w_b <= 5'd0;
                            if (w_ic == IC_LAST[3:0]) begin
                                w_ic <= 4'd0;
                                w_oc <= w_oc + 6'd1;
                            end else begin
                                w_ic <= w_ic + 4'd1;
                            end
                        end else begin
                            w_b <= w_b + 5'd1;
                        end
                        if (w_byte_cnt == WGT_BYTES[15:0] - 16'd1) begin
                            w_word_valid <= 1'b0;
                            state        <= S_BIAS_REQ;
                        end
                    end
                end

                // ------------------------------------------------------
                // bias 装载：24 个 64-bit beat -> 48 个 32-bit word
                // ------------------------------------------------------
                S_BIAS_REQ: begin
                    if (rd_req_ready) state <= S_BIAS_DATA;
                end
                S_BIAS_DATA: begin
                    if (rd_valid && rd_ready) begin
                        b_word       <= rd_data;
                        b_word_valid <= 1'b1;
                        b_lane       <= 1'b0;
                    end
                    if (b_word_valid) begin
                        bias_we    <= 1'b1;
                        bias_waddr <= b_idx;
                        bias_wdata <= b_lane ? b_word[63:32] : b_word[31:0];
                        if (b_lane) begin
                            b_lane       <= 1'b0;
                            b_word_valid <= 1'b0;
                            if (b_idx == COUT[5:0] - 6'd1) begin
                                state <= S_PRE_REQ;
                            end else begin
                                b_idx <= b_idx + 6'd1;
                            end
                        end else begin
                            // 低 32-bit 字：地址 b_idx，然后字地址 +1；
                            // 高 32-bit 字在下一拍用 b_idx+1，写完后才判终值 47。
                            b_idx  <= b_idx + 6'd1;
                            b_lane <= 1'b1;
                        end
                    end
                end

                // ------------------------------------------------------
                // 预载 row0 / row1
                // ------------------------------------------------------
                S_PRE_REQ: begin
                    if ((pre_row < 2'd2) && (pre_row < H)) begin
                        fill_row  <= pre_row;
                        fill_blk  <= 1'b0;
                        fill_mode <= 1'b0;
                        state     <= S_FILL_REQ;
                    end else begin
                        lb_valid[wr_line] <= 1'b0;
                        wr_line <= (wr_line == 3'd4) ? 3'd0 : wr_line + 3'd1;
                        pre_row <= pre_row + 2'd1;
                        if (pre_row == 2'd1) state <= S_ROW_BEGIN;
                    end
                end

                // ------------------------------------------------------
                // 行填充：一个 8 通道块 W 个 beat，两个块
                // ------------------------------------------------------
                S_FILL_REQ: begin
                    if (rd_req_ready) begin
                        fill_col <= 0;
                        state    <= S_FILL_DATA;
                    end
                end
                S_FILL_DATA: begin
                    if (rd_valid && rd_ready) begin
                        if (fill_col == W[9:0] - 10'd1) begin
                            if (fill_blk == 1'b0) begin
                                fill_blk <= 1'b1;
                                state    <= S_FILL_REQ;
                            end else begin
                                lb_valid[wr_line] <= 1'b1;
                                wr_line <= (wr_line == 3'd4) ? 3'd0 : wr_line + 3'd1;
                                fill_blk <= 1'b0;
                                if (fill_mode == 1'b0) begin
                                    pre_row <= pre_row + 2'd1;
                                    if (pre_row == 2'd0) state <= S_PRE_REQ;
                                    else                 state <= S_ROW_BEGIN;
                                end else begin
                                    state <= S_BLOCK_INIT;
                                end
                            end
                        end else begin
                            fill_col <= fill_col + 10'd1;
                        end
                    end
                end

                // ------------------------------------------------------
                // 主循环：每行
                // ------------------------------------------------------
                S_ROW_BEGIN: begin
                    if (y_cnt == Y_LAST[8:0] + 9'd1) begin
                        state <= S_DONE;
                    end else if ((y_cnt + 9'd2) < H) begin
                        fill_row  <= y_cnt + 9'd2;
                        fill_blk  <= 1'b0;
                        fill_mode <= 1'b1;
                        state     <= S_FILL_REQ;
                    end else begin
                        // 行越界：清 valid，不读 DDR
                        lb_valid[wr_line] <= 1'b0;
                        wr_line <= (wr_line == 3'd4) ? 3'd0 : wr_line + 3'd1;
                        state   <= S_BLOCK_INIT;
                    end
                end

                // ------------------------------------------------------
                // 每个 8-oc 块：预载窗口 -> 计算 -> drain -> flush -> 写回
                // ------------------------------------------------------
                S_BLOCK_INIT: begin
                    b_cnt  <= 0;
                    pre_cnt<= 0;
                    state  <= S_BLOCK_PRE;
                end
                S_BLOCK_PRE: begin
                    pre_cnt  <= 0;
                    state    <= S_PRELOAD;
                end
                S_PRELOAD: begin
                    if (pre_cnt == 3'd5) begin
                        ic_cnt <= 0;
                        s_cnt  <= 3'd0;
                        x_cnt  <= 0;
                        state  <= S_COMPUTE;
                    end else begin
                        pre_cnt <= pre_cnt + 3'd1;
                    end
                end

                // 循环顺序：for x { for s=0..NG-1 { for ic=0..CIN-1 { MAC } } shift }
                // pass 与 pass 背靠背，中间不插气泡 => II 仍为 1。
                S_COMPUTE: begin
                    if (ic_cnt == IC_LAST[3:0]) begin
                        ic_cnt <= 4'd0;
                        if (s_cnt == NG_LAST[2:0]) begin
                            s_cnt <= 3'd0;
                            if (x_cnt == X_LAST[9:0]) begin
                                drain_cnt <= 3'd0;
                                state     <= S_DRAIN;
                            end else begin
                                x_cnt <= x_cnt + 10'd1;
                            end
                        end else begin
                            s_cnt <= s_cnt + 3'd1;
                        end
                    end else begin
                        ic_cnt <= ic_cnt + 4'd1;
                    end
                end

                S_DRAIN: begin
                    if (drain_cnt == 3'd2) begin
                        drain_cnt <= 3'd0;
                        flush_cnt <= 4'd0;
                        state     <= S_FLUSH;
                    end else begin
                        drain_cnt <= drain_cnt + 3'd1;
                    end
                end

                S_FLUSH: begin
                    if (flush_cnt == 4'd9) begin
                        flush_cnt <= 4'd0;
                        state     <= S_WR_REQ;
                    end else begin
                        flush_cnt <= flush_cnt + 4'd1;
                    end
                end

                S_WR_REQ: begin
                    if (wr_req_ready) state <= S_WR_WAIT;
                end
                S_WR_WAIT: begin
                    if (wr_done) begin
                        if (b_cnt == NBLK_LAST[2:0]) begin
                            // 一行完成：旋转 sel，进入下一行
                            sel[0] <= sel[1];
                            sel[1] <= sel[2];
                            sel[2] <= sel[3];
                            sel[3] <= sel[4];
                            sel[4] <= sel[0];
                            y_cnt  <= y_cnt + 9'd1;
                            state  <= S_ROW_BEGIN;
                        end else begin
                            b_cnt <= b_cnt + 3'd1;
                            state <= S_BLOCK_PRE;
                        end
                    end
                end

                S_DONE: begin
                    ctrl_done <= 1'b1;
                    state     <= S_IDLE;
                end

                default: state <= S_IDLE;
            endcase
        end
    end

    // 填充写端口连接
    assign fill_we    = (state == S_FILL_DATA) && rd_valid && rd_ready;
    assign fill_line  = wr_line;
    assign fill_data  = rd_data;

    // 写引擎输出缓冲读地址由写引擎内部驱动（obuf_raddr 已在写引擎 always_ff 中）

    // ==================================================================
    // 12. 仿真可观测性（综合时由 translate_off 去掉）
    //     [K5-CFG] 每次启动打一行配置；[K5-SM] 状态转储默认关闭，
    //     需要时编译加宏：xvlog -d K5_SM_DUMP（run_module.sh 用 --define）。
    // ==================================================================
    // synopsys translate_off
    initial begin
        $display("[K5-CFG] CIN=%0d COUT=%0d W=%0d H=%0d PAD=%0d IC_LAST=%0d X_LAST=%0d Y_LAST=%0d NOC=%0d NG=%0d NGRP=%0d NBLK=%0d WB_DEPTH=%0d",
                 CIN, COUT, W, H, PAD, IC_LAST, X_LAST, Y_LAST, NOC, NG, NGRP, NBLK, WB_DEPTH);
    end

`ifdef K5_SM_DUMP
    state_t  dbg_state_d;
    integer  dbg_cyc;
    integer  dbg_obuf_wr;

    initial begin
        dbg_state_d = S_IDLE;
        dbg_cyc     = 0;
        dbg_obuf_wr = 0;
    end

    always @(posedge ap_clk) begin
        dbg_cyc <= dbg_cyc + 1;
        if (obuf_we) dbg_obuf_wr <= dbg_obuf_wr + 1;

        if (!ap_rst_n) begin
            dbg_state_d <= S_IDLE;
        end else if (state != dbg_state_d) begin
            $display("[K5-SM] %0d -> %0d @cyc=%0d y=%0d b=%0d x=%0d s=%0d ic=%0d pre=%0d pre_row=%0d wr_line=%0d sel=%0d,%0d,%0d,%0d,%0d lbv=%b%b%b%b%b wcnt=%0d bidx=%0d rd=%0d wr=%0d",
                     dbg_state_d, state, dbg_cyc,
                     y_cnt, b_cnt, x_cnt, s_cnt, ic_cnt, pre_cnt, pre_row, wr_line,
                     sel[0], sel[1], sel[2], sel[3], sel[4],
                     lb_valid[4], lb_valid[3], lb_valid[2], lb_valid[1], lb_valid[0],
                     w_byte_cnt, b_idx, rd_state, wr_state);
            dbg_state_d <= state;
        end

        if (dbg_cyc == 8000000)
            $display("[K5-SM] WATCHDOG 8M cycles: state=%0d y=%0d b=%0d x=%0d s=%0d ic=%0d rd=%0d wr=%0d wcnt=%0d bidx=%0d fill_col=%0d",
                     state, y_cnt, b_cnt, x_cnt, s_cnt, ic_cnt, rd_state, wr_state,
                     w_byte_cnt, b_idx, fill_col);

        // 20k 一拍摘要：比 8M 更早暴露卡点；正常 2×320 条带 ~392k 拍（1 oc）
        if ((dbg_cyc % 20000) == 0) begin
            $display("[K5-SM] t=%0d state=%0d y=%0d b=%0d x=%0d s=%0d ic=%0d pre=%0d wr_line=%0d lbv=%b%b%b%b%b fill_col=%0d fill_blk=%b wr_beat=%0d wr_left=%0d wr_burst=%0d obuf_wr=%0d",
                     dbg_cyc, state, y_cnt, b_cnt, x_cnt, s_cnt, ic_cnt, pre_cnt, wr_line,
                     lb_valid[4], lb_valid[3], lb_valid[2], lb_valid[1], lb_valid[0],
                     fill_col, fill_blk, wr_beat_q, wr_left_q, wr_burst_q, dbg_obuf_wr);
            $display("[K5-BIAS] b_idx=%0d b_lane=%b b_word_valid=%b bias_we=%b bias_waddr=%0d",
                     b_idx, b_lane, b_word_valid, bias_we, bias_waddr);
            $display("[K5-AXI] in: arv=%b arr=%b rv=%b rr=%b | w: arv=%b arr=%b rv=%b rr=%b | b: arv=%b arr=%b rv=%b rr=%b | out: awv=%b awr=%b wv=%b wr=%b bv=%b br=%b",
                     m_axi_in_arvalid, m_axi_in_arready, m_axi_in_rvalid, m_axi_in_rready,
                     m_axi_w_arvalid, m_axi_w_arready, m_axi_w_rvalid, m_axi_w_rready,
                     m_axi_b_arvalid, m_axi_b_arready, m_axi_b_rvalid, m_axi_b_rready,
                     m_axi_out_awvalid, m_axi_out_awready, m_axi_out_wvalid, m_axi_out_wready,
                     m_axi_out_bvalid, m_axi_out_bready);
        end
    end
`endif
    // synopsys translate_on

endmodule

// ============================================================================
//  权重 bank 的单 bank：DEPTH×8 分布式 RAM（1 写口 + 1 读口，组合读）
//  OC_PER_PASS=1 时 DEPTH=768；=4 时 DEPTH=192（与原设计一致）
// ============================================================================
module k5_wbank #(
    parameter int DEPTH = 192
)(
    input  logic               clk,
    input  logic               we,
    input  logic [$clog2(DEPTH)-1:0] waddr,
    input  logic [7:0]         wdata,
    input  logic [$clog2(DEPTH)-1:0] raddr,
    output logic [7:0]         rdata
);
    (* ram_style = "distributed" *) logic [7:0] mem [0:DEPTH-1];
    always_ff @(posedge clk) begin
        if (we) mem[waddr] <= wdata;
    end
    // 组合读：权重输出的 ic 必须与同一拍的 pix_byte 的 ic 对齐。
    // DEPTH:1 的分布式 RAM 读是一条 mux 链，与像素 mux 并联进 DSP 的
    // A/B 寄存器。DEPTH=768 比 192 多约 2 级 LUT6（见 docs 时序风险）。
    assign rdata = mem[raddr];
endmodule

// ============================================================================
//  bias RAM：64×32 分布式 RAM
// ============================================================================
module k5_bias_ram #(
    parameter int DEPTH = 64
)(
    input  logic        clk,
    input  logic        we,
    input  logic [5:0]  waddr,
    input  logic [31:0] wdata,
    input  logic [5:0]  raddr,
    output logic [31:0] rdata
);
    (* ram_style = "distributed" *) logic [31:0] mem [0:DEPTH-1];
    always_ff @(posedge clk) begin
        if (we) mem[waddr] <= wdata;
        rdata <= mem[raddr];
    end
endmodule
