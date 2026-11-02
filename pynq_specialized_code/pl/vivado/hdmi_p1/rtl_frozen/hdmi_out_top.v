// ============================================================================
// hdmi_out_top.v —— HDMI P1 顶层（双像素时钟 + 彩条 + SIL9134 I2C + AXI-Lite）
//
// 需求来源：docs/camera/05_HDMI输出_PS_PL接口契约.md §2/§3/§7.1/§7.3
//
// 关键设计决定（2026-11-02 与主代理确认）：
//   1) 双像素时钟：MMCM 在 P3 的 BD 里产生，本模块只接收两路：
//        pix_clk_a = 148.4375 MHz（100 × 11.875 / 8，1080p60，-421 ppm）
//        pix_clk_b =  74.21875 MHz（100 × 11.875 / 16，720p60 / 1080p30，-421 ppm）
//      内部 BUFG + BUFGMUX，由 VIDEO_MODE[0] 选：
//        [0]=0 -> pix_clk_b -> mode 0 (720p60, 720p 参数) / mode 2 (1080p30, 1080p 参数)
//        [0]=1 -> pix_clk_a -> mode 1 (1080p60, 1080p 参数)
//      mode 3 (640x480) 需要 25.175 MHz，本双时钟位流不支持；运行到 3 等价于
//      1080p60（单独构建 640x480 时改 MODE_DEFAULT=3 并换像素时钟）。
//   2) 切换协议（写进 CTRL 文档）：先 CTRL.enable=0 -> 写 VIDEO_MODE ->
//      等 >= 2 帧 -> CTRL.enable=1。模块内部另加 64 个像素时钟的自动复位，
//      保证切换瞬间 DE 不出非法脉冲（BUFGMUX 本身 glitchless）。
//   3) IOBUF 放在本模块内：顶层端口为 inout hdmi_scl / hdmi_sda，
//      内部 IOBUF .I(1'b0) .O(pad_i) .T(t)；t=1 释放（上拉）、t=0 驱动 0。
//   4) CDC：
//        s_axi -> pix : enable / test_pattern / motion_en 每位 2FF（3 条链）；
//        s_axi -> pix : VIDEO_MODE 用"写使能 toggle + 目的域整捆捕获"
//                       （2 bit 数据在 toggle 到达前已稳定，不逐位同步）；
//        s_axi -> pix : pwr_good / soft_reset 电平各 2FF；
//        pix -> s_axi : vsync 翻转电平 + 2FF + 边沿检测（不直接同步单拍脉冲）。
//        pix -> s_axi : pix_hb（慢变电平）+ mmcm_locked（单 bit）各一条 2FF；
//                       二者都不进 pix_rst / soft_rst，也不清 hb_cnt。
//   5) 0x2C UNDERFLOW_CNT（RO，P1 恒 0；P3 由扫描器驱动）。
//   6) cfg_upd / fid_upd：AXI 写配置寄存器 / 写 FRAME_ID 的 1 拍脉冲，
//      与扫描器同在 s_axi_aclk 域，P3 直接接 hdmi_fb_scan 同名输入。
//   7) 0x30 PIX_HB（2026-11-03 事故驱动，契约 §8.2）：这是**唯一**能在"像素时钟
//      死掉"时仍然可读的"体检"寄存器 —— 它是 AXI 时钟域的，数据源只是两个单 bit
//      2FF 同步链，因此 pix_clk 停摆时它读回稳定值（不跳、不挂总线）。
//      上板协议：两次读 0x30[0] 必须变化 ⇒ 才允许读 0x04/0x08 等；
//      不变 ⇒ 像素时钟没跑 ⇒ 立刻停，不要再做任何 AXI 访问。
//      mmcm_locked 只做状态上报（2FF），**不**参与任何复位/门控：
//      像素域的复位仍只看 aresetn / pwr_good / soft_reset / 模式切换（契约 §8.1：
//      AXI 侧逻辑绝不许被 PL 侧 MMCM 锁定门控）。
//
// 寄存器图（32-bit，4 字节对齐）：
//   0x00 CTRL  RW [0]enable [1]soft_reset(写1自清) [2]test_pattern
//                 [3]frame_swap_req [4]motion_en
//   0x04 STATUS RO [0]init_done [1]vsync(读清) [2]scan_underflow(P1=0)
//   0x08 VIDEO_MODE RW [1:0]，复位=MODE_DEFAULT（默认 1=1080p60）
//   0x0C FB_ADDR_LO 0x10 FB_ADDR_HI 0x14 FB_STRIDE 0x18 FB_W 0x1C FB_H
//   0x20 FRAME_ID RW   0x24 FRAME_ID_LATCHED RO(P1=0)   0x28 PIXEL_FMT RW
//   0x2C UNDERFLOW_CNT RO (P1=0)
//   0x30 PIX_HB RO [0]pix_hb（像素域自由计数 bit16，2FF 到 aclk）
//                  [1]mmcm_locked（外部输入，2FF 到 aclk）
//                  [31:2] 保留，读 0
// ============================================================================
`timescale 1ns / 1ps

module hdmi_out_top #(
    parameter integer MODE_DEFAULT = 1,          // 0=720p60 1=1080p60 2=1080p30 3=640x480*
    parameter integer AXI_HZ       = 100_000_000
) (
    // ---- 时钟/复位 ----
    input  wire        s_axi_aclk,
    input  wire        aresetn,
    input  wire        pix_clk_a,        // 148.4375 MHz（BD MMCM CLKOUT0）
    input  wire        pix_clk_b,        //  74.21875 MHz（BD MMCM CLKOUT1）
    input  wire        mmcm_locked,      // BD MMCM.locked（只做 0x30[1] 状态上报）

    // ---- AXI4-Lite 配置页 ----
    input  wire [31:0] s_axi_awaddr,  input wire s_axi_awvalid, output wire s_axi_awready,
    input  wire [31:0] s_axi_wdata,   input wire [3:0] s_axi_wstrb,
    input  wire        s_axi_wvalid,  output wire s_axi_wready,
    output wire [1:0]  s_axi_bresp,   output wire s_axi_bvalid, input wire s_axi_bready,
    input  wire [31:0] s_axi_araddr,  input wire s_axi_arvalid, output wire s_axi_arready,
    output wire [31:0] s_axi_rdata,   output wire [1:0] s_axi_rresp,
    output wire        s_axi_rvalid,  input  wire s_axi_rready,

    // ---- AXI4 master 读（P2；P1 合法空转）----
    output wire [31:0] m_axi_araddr,  output wire [7:0] m_axi_arlen,
    output wire [2:0]  m_axi_arsize,  output wire [1:0] m_axi_arburst,
    output wire        m_axi_arvalid, input  wire       m_axi_arready,
    input  wire [63:0] m_axi_rdata,   input  wire [1:0] m_axi_rresp,
    input  wire        m_axi_rlast,   input  wire       m_axi_rvalid,
    output wire        m_axi_rready,

    // ---- 视频输出（直连 SIL9134；H1 = pix_clk）----
    output wire        pix_clk,
    output wire        hdmi_de,  output wire hdmi_hs, output wire hdmi_vs,
    output wire [23:0] hdmi_d,

    // ---- SIL9134 控制（I2C 三态在模块内）----
    inout  wire        hdmi_scl,
    inout  wire        hdmi_sda,
    output wire        hdmi_rst_n,

    // ---- 状态 / P2 预留 ----
    output wire        irq_vsync,
    output wire        init_done,
    output wire        cfg_upd,          // P3 接 hdmi_fb_scan.cfg_upd
    output wire        fid_upd           // P3 接 hdmi_fb_scan.fid_upd
);

    // ---------------------------------------------------------------- 双时钟选择
    wire clk_a_buf, clk_b_buf;
    BUFG u_bufg_a (.I(pix_clk_a), .O(clk_a_buf));
    BUFG u_bufg_b (.I(pix_clk_b), .O(clk_b_buf));

    wire pix_clk_i;
    wire sel_148;

    // I0=74.21875, I1=148.4375；S 由 pix 域同步后的 VIDEO_MODE[0] 驱动
    BUFGMUX u_bufgmux (
        .I0 (clk_b_buf),
        .I1 (clk_a_buf),
        .S  (sel_148),
        .O  (pix_clk_i)
    );

    assign pix_clk = pix_clk_i;

    // ---------------------------------------------------------------- 0x30 PIX_HB
    // 目的（契约 §8.2）：PS 在读任何状态寄存器之前，先用两次读 0x30 确认像素时钟活着。
    // 设计要求（缺一条这条判据就失效）：
    //   1) hb_cnt 是像素域里**唯一不被 soft_reset / pwr_good / 模式切换清零**的计数器；
    //      只有 aresetn（PS 域硬复位）清它。否则 soft_reset 后 1 ms 内读会读到"没在跑"。
    //   2) 取固定位 bit[16]（不按绝对时间，同一份 RTL 适配两路像素时钟）：
    //        pix_clk_a 148.4375 MHz -> 翻转周期 2*2^16/f = 0.883 ms
    //        pix_clk_b  74.21875 MHz -> 翻转周期 2*2^16/f = 1.766 ms
    //      两次读间隔 >= 2 ms 必然跨过至少一个翻转沿（最慢一路 1.766 ms 也够）。
    //   3) 跨域只用单 bit 2FF（慢变电平，天然安全）；像素时钟停摆时同步链保持最后值
    //      ⇒ 0x30[0] 冻结，而 AXI 时钟域寄存器读写完全不受影响。
    //   4) 0x30 只读：写路径的 case 里没有 6'h0C 分支 ⇒ 写被忽略（不影响回读值）。
    //   5) pix_hb 不锁存成"曾经翻转"粘滞位：要求的是"现在还在跑"，粘滞位会掩盖停摆。
    reg  [16:0] hb_cnt;
    wire        pix_hb_pix = hb_cnt[16];

    always @(posedge pix_clk_i or negedge aresetn) begin
        if (!aresetn) hb_cnt <= 17'd0;
        else          hb_cnt <= hb_cnt + 17'd1;   // 复位只认 aresetn
    end

    // pix -> aclk：pix_hb 与 mmcm_locked 各一条 2FF
    reg hb_meta, hb_sync;
    reg lk_meta, lk_sync;

    always @(posedge s_axi_aclk) begin
        if (!aresetn) begin
            hb_meta <= 1'b0;
            hb_sync <= 1'b0;
            lk_meta <= 1'b0;
            lk_sync <= 1'b0;
        end else begin
            hb_meta <= pix_hb_pix;
            hb_sync <= hb_meta;
            lk_meta <= mmcm_locked;
            lk_sync <= lk_meta;
        end
    end

    // ---------------------------------------------------------------- 模式参数
    localparam integer H_TOTAL_720  = 1650, H_ACTIVE_720  = 1280;
    localparam integer H_FP_720     = 110,  H_SYNC_720    = 40,  H_BP_720  = 220;
    localparam integer V_TOTAL_720  = 750,  V_ACTIVE_720  = 720;
    localparam integer V_FP_720     = 5,    V_SYNC_720    = 5,   V_BP_720  = 20;

    localparam integer H_TOTAL_1080 = 2200, H_ACTIVE_1080 = 1920;
    localparam integer H_FP_1080    = 88,   H_SYNC_1080   = 44,  H_BP_1080 = 148;
    localparam integer V_TOTAL_1080 = 1125, V_ACTIVE_1080 = 1080;
    localparam integer V_FP_1080    = 4,    V_SYNC_1080   = 5,   V_BP_1080 = 36;

    localparam integer PWR_CYCLES = AXI_HZ / 1000;   // 1 ms 上电等待

    // ---------------------------------------------------------------- AXI 寄存器
    reg        ctrl_enable;
    reg        ctrl_test;
    reg        ctrl_fswap;
    reg        ctrl_motion;
    reg [1:0]  video_mode;
    reg        mode_tog;
    reg [31:0] fb_addr_lo, fb_addr_hi, fb_stride, fb_w, fb_h;
    reg [31:0] frame_id;
    reg [31:0] pixel_fmt;
    reg        status_vsync;

    reg        aw_pending, w_pending, bvalid;
    reg [31:0] awaddr_r, wdata_r;
    reg [3:0]  wstrb_r;
    reg        rvalid;
    reg [31:0] rdata_r;

    wire wr_commit = aw_pending && w_pending;
    wire ar_hs     = s_axi_arvalid && s_axi_arready;

    assign s_axi_awready = ~aw_pending && ~bvalid;
    assign s_axi_wready  = ~w_pending  && ~bvalid;
    assign s_axi_bresp   = 2'b00;
    assign s_axi_bvalid  = bvalid;
    assign s_axi_arready = ~rvalid;
    assign s_axi_rresp   = 2'b00;
    assign s_axi_rdata   = rdata_r;
    assign s_axi_rvalid  = rvalid;

    assign m_axi_araddr  = 32'h0000_0000;
    assign m_axi_arlen   = 8'd0;
    assign m_axi_arsize  = 3'b011;
    assign m_axi_arburst = 2'b01;
    assign m_axi_arvalid = 1'b0;
    assign m_axi_rready  = 1'b1;

    // ---------------------------------------------------------------- 读数据 mux
    function [31:0] rd_mux;
        input [31:0] a;
        begin
            case (a[7:2])
                6'h00:   rd_mux = {27'd0, ctrl_motion, ctrl_fswap, ctrl_test, 1'b0, ctrl_enable};
                6'h01:   rd_mux = {29'd0, 1'b0, status_vsync, init_done};
                6'h02:   rd_mux = {30'd0, video_mode};
                6'h03:   rd_mux = fb_addr_lo;
                6'h04:   rd_mux = fb_addr_hi;
                6'h05:   rd_mux = fb_stride;
                6'h06:   rd_mux = fb_w;
                6'h07:   rd_mux = fb_h;
                6'h08:   rd_mux = frame_id;
                6'h09:   rd_mux = 32'd0;              // FRAME_ID_LATCHED（P2）
                6'h0A:   rd_mux = pixel_fmt;
                6'h0B:   rd_mux = 32'd0;              // UNDERFLOW_CNT（P3 扫描器驱动）
                6'h0C:   rd_mux = {30'd0, lk_sync, hb_sync};   // 0x30 PIX_HB（RO）
                default: rd_mux = 32'd0;              // 越界回 0，不挂总线
            endcase
        end
    endfunction

    // ---------------------------------------------------------------- P2 预留脉冲
    wire cfg_hit = (awaddr_r[7:2] == 6'h02) || (awaddr_r[7:2] == 6'h03) ||
                   (awaddr_r[7:2] == 6'h04) || (awaddr_r[7:2] == 6'h05) ||
                   (awaddr_r[7:2] == 6'h06) || (awaddr_r[7:2] == 6'h07) ||
                   (awaddr_r[7:2] == 6'h0A);

    reg cfg_upd_r, fid_upd_r;

    always @(posedge s_axi_aclk) begin
        if (!aresetn) begin
            cfg_upd_r <= 1'b0;
            fid_upd_r <= 1'b0;
        end else begin
            cfg_upd_r <= wr_commit && cfg_hit;
            fid_upd_r <= wr_commit && (awaddr_r[7:2] == 6'h08);
        end
    end

    assign cfg_upd = cfg_upd_r;
    assign fid_upd = fid_upd_r;

    // ---------------------------------------------------------------- AXI 写
    always @(posedge s_axi_aclk) begin
        if (!aresetn) begin
            aw_pending <= 1'b0;
            w_pending  <= 1'b0;
            bvalid     <= 1'b0;
            awaddr_r   <= 32'd0;
            wdata_r    <= 32'd0;
            wstrb_r    <= 4'd0;

            ctrl_enable <= 1'b1;
            ctrl_test   <= 1'b1;
            ctrl_fswap  <= 1'b0;
            ctrl_motion <= 1'b1;
            video_mode  <= (MODE_DEFAULT & 3);
            mode_tog    <= 1'b0;
            fb_addr_lo  <= 32'd0;
            fb_addr_hi  <= 32'd0;
            fb_stride   <= 32'd0;
            fb_w        <= 32'd0;
            fb_h        <= 32'd0;
            frame_id    <= 32'd0;
            pixel_fmt   <= 32'd0;
        end else begin
            if (s_axi_awvalid && s_axi_awready) begin
                aw_pending <= 1'b1;
                awaddr_r   <= s_axi_awaddr;
            end
            if (s_axi_wvalid && s_axi_wready) begin
                w_pending <= 1'b1;
                wdata_r   <= s_axi_wdata;
                wstrb_r   <= s_axi_wstrb;
            end

            if (wr_commit) begin
                aw_pending <= 1'b0;
                w_pending  <= 1'b0;
                bvalid     <= 1'b1;

                case (awaddr_r[7:2])
                    6'h00: begin
                        if (wstrb_r[0]) begin
                            ctrl_enable <= wdata_r[0];
                            ctrl_test   <= wdata_r[2];
                            ctrl_fswap  <= wdata_r[3];
                            ctrl_motion <= wdata_r[4];
                        end
                    end
                    6'h02: begin
                        if (wstrb_r[0]) begin
                            video_mode <= wdata_r[1:0];
                            mode_tog   <= ~mode_tog;   // 目的域整捆捕获
                        end
                    end
                    6'h03: fb_addr_lo  <= wdata_r;
                    6'h04: fb_addr_hi  <= wdata_r;
                    6'h05: fb_stride   <= wdata_r;
                    6'h06: fb_w        <= wdata_r;
                    6'h07: fb_h        <= wdata_r;
                    6'h08: frame_id    <= wdata_r;
                    6'h0A: pixel_fmt   <= wdata_r;
                    default: ;
                endcase
            end else if (bvalid && s_axi_bready) begin
                bvalid <= 1'b0;
            end
        end
    end

    // ---------------------------------------------------------------- AXI 读
    always @(posedge s_axi_aclk) begin
        if (!aresetn) begin
            rvalid  <= 1'b0;
            rdata_r <= 32'd0;
        end else begin
            if (ar_hs) begin
                rdata_r <= rd_mux(s_axi_araddr);
                rvalid  <= 1'b1;
            end else if (rvalid && s_axi_rready) begin
                rvalid <= 1'b0;
            end
        end
    end

    // ---------------------------------------------------------------- 上电/软复位
    reg [31:0] pwr_cnt;
    reg        pwr_good;

    always @(posedge s_axi_aclk) begin
        if (!aresetn) begin
            pwr_cnt  <= 32'd0;
            pwr_good <= 1'b0;
        end else if (!pwr_good) begin
            if (pwr_cnt == (PWR_CYCLES - 1)) pwr_good <= 1'b1;
            else pwr_cnt <= pwr_cnt + 32'd1;
        end
    end

    assign hdmi_rst_n = pwr_good;

    reg [6:0] soft_cnt;
    wire      soft_rst_axi = (soft_cnt != 7'd0);

    always @(posedge s_axi_aclk) begin
        if (!aresetn) soft_cnt <= 7'd0;
        else if (wr_commit && (awaddr_r[7:2] == 6'h00) && wstrb_r[0] && wdata_r[1])
            soft_cnt <= 7'd64;
        else if (soft_rst_axi)
            soft_cnt <= soft_cnt - 7'd1;
    end

    // ---------------------------------------------------------------- pix 域复位/模式 CDC
    reg [2:0] rst_sync;
    reg       pg_meta, pg_sync;
    reg       sr_meta, sr_sync;
    reg       mt_meta, mt_sync, mt_d;
    reg [1:0] mode_pix;
    reg [6:0] mode_rst_cnt;

    wire pix_rst = rst_sync[2] | ~pg_sync | sr_sync | (mode_rst_cnt != 7'd0);

    always @(posedge pix_clk_i or negedge aresetn) begin
        if (!aresetn) begin
            rst_sync     <= 3'b111;
            pg_meta      <= 1'b0; pg_sync <= 1'b0;
            sr_meta      <= 1'b0; sr_sync <= 1'b0;
            mt_meta      <= 1'b0; mt_sync <= 1'b0; mt_d <= 1'b0;
            mode_pix     <= (MODE_DEFAULT & 3);
            mode_rst_cnt <= 7'd0;
        end else begin
            rst_sync <= {rst_sync[1:0], 1'b0};

            pg_meta <= pwr_good;      pg_sync <= pg_meta;
            sr_meta <= soft_rst_axi;  sr_sync <= sr_meta;

            mt_meta <= mode_tog;
            mt_sync <= mt_meta;
            mt_d    <= mt_sync;

            if (mt_sync != mt_d) begin
                mode_pix     <= video_mode;   // 数据已稳定 >= 2 pix 周期
                mode_rst_cnt <= 7'd63;        // 切时钟后 64 拍复位
            end else if (mode_rst_cnt != 7'd0) begin
                mode_rst_cnt <= mode_rst_cnt - 7'd1;
            end
        end
    end

    assign sel_148 = mode_pix[0];
    wire   is_720  = (mode_pix == 2'd0);

    // ---------------------------------------------------------------- 配置位 2FF
    reg [2:0] cfg_meta, cfg_sync;

    always @(posedge pix_clk_i) begin
        if (pix_rst) begin
            cfg_meta <= 3'd0;
            cfg_sync <= 3'd0;
        end else begin
            cfg_meta <= {ctrl_motion, ctrl_test, ctrl_enable};
            cfg_sync <= cfg_meta;
        end
    end

    wire cfg_enable = cfg_sync[0];
    wire cfg_test   = cfg_sync[1];
    wire cfg_motion = cfg_sync[2];

    // ---------------------------------------------------------------- I2C（IOBUF 在模块内）
    wire sda_pad_i, scl_pad_i;
    wire i2c_scl_t, i2c_sda_t;

    IOBUF u_iobuf_scl (
        .IO (hdmi_scl),
        .I  (1'b0),
        .O  (scl_pad_i),
        .T  (i2c_scl_t)
    );

    IOBUF u_iobuf_sda (
        .IO (hdmi_sda),
        .I  (1'b0),
        .O  (sda_pad_i),
        .T  (i2c_sda_t)
    );

    sil9134_init #(
        .CLK_HZ (AXI_HZ),
        .SCL_HZ (100_000),
        .POR_US (200)
    ) u_sil9134_init (
        .clk       (s_axi_aclk),
        .rst       (~pwr_good | soft_rst_axi),
        .init_done (init_done),
        .busy      (),
        .error     (),
        .i2c_scl_o (),
        .i2c_scl_t (i2c_scl_t),
        .i2c_sda_i (sda_pad_i),
        .i2c_sda_o (),
        .i2c_sda_t (i2c_sda_t)
    );

    // ---------------------------------------------------------------- 双模式时序
    wire        hs720, vs720, de720, hs1080, vs1080, de1080;
    wire [15:0] x720, y720, x1080, y1080;

    hdmi_timing_gen #(
        .H_TOTAL(H_TOTAL_720), .H_ACTIVE(H_ACTIVE_720), .H_FP(H_FP_720),
        .H_SYNC(H_SYNC_720), .H_BP(H_BP_720),
        .V_TOTAL(V_TOTAL_720), .V_ACTIVE(V_ACTIVE_720), .V_FP(V_FP_720),
        .V_SYNC(V_SYNC_720), .V_BP(V_BP_720),
        .HS_POL(1), .VS_POL(1)
    ) u_timing_720 (
        .pix_clk(pix_clk_i), .rst(pix_rst),
        .hs(hs720), .vs(vs720), .de(de720), .x(x720), .y(y720)
    );

    hdmi_timing_gen #(
        .H_TOTAL(H_TOTAL_1080), .H_ACTIVE(H_ACTIVE_1080), .H_FP(H_FP_1080),
        .H_SYNC(H_SYNC_1080), .H_BP(H_BP_1080),
        .V_TOTAL(V_TOTAL_1080), .V_ACTIVE(V_ACTIVE_1080), .V_FP(V_FP_1080),
        .V_SYNC(V_SYNC_1080), .V_BP(V_BP_1080),
        .HS_POL(1), .VS_POL(1)
    ) u_timing_1080 (
        .pix_clk(pix_clk_i), .rst(pix_rst),
        .hs(hs1080), .vs(vs1080), .de(de1080), .x(x1080), .y(y1080)
    );

    wire        v_hs = is_720 ? hs720 : hs1080;
    wire        v_vs = is_720 ? vs720 : vs1080;
    wire        v_de = is_720 ? de720 : de1080;
    wire [15:0] v_x  = is_720 ? x720  : x1080;
    wire [15:0] v_y  = is_720 ? y720  : y1080;

    // ---------------------------------------------------------------- 移动竖线
    reg        vs_d_pix;
    reg [15:0] line_x;

    wire [15:0] line_max = is_720 ? 16'd1280 : 16'd1920;

    always @(posedge pix_clk_i) begin
        if (pix_rst) begin
            vs_d_pix <= 1'b0;
            line_x   <= 16'd0;
        end else begin
            vs_d_pix <= v_vs;
            if (v_vs && !vs_d_pix) begin
                if ((line_x + 16'd8) >= line_max) line_x <= 16'd0;
                else                              line_x <= line_x + 16'd8;
            end
        end
    end

    // ---------------------------------------------------------------- 彩条（两套参数）
    wire [23:0] bar720, bar1080;

    hdmi_color_bar #(
        .H_ACTIVE(H_ACTIVE_720), .V_ACTIVE(V_ACTIVE_720), .LINE_W(4)
    ) u_bar720 (
        .de(de720), .x(x720), .y(y720),
        .line_x(line_x), .motion_en(cfg_motion), .rgb(bar720)
    );

    hdmi_color_bar #(
        .H_ACTIVE(H_ACTIVE_1080), .V_ACTIVE(V_ACTIVE_1080), .LINE_W(4)
    ) u_bar1080 (
        .de(de1080), .x(x1080), .y(y1080),
        .line_x(line_x), .motion_en(cfg_motion), .rgb(bar1080)
    );

    wire [23:0] bar_rgb = is_720 ? bar720 : bar1080;

    // ---------------------------------------------------------------- 输出
    assign hdmi_hs = v_hs;
    assign hdmi_vs = v_vs;
    assign hdmi_de = v_de && cfg_enable;
    assign hdmi_d  = (v_de && cfg_enable && cfg_test) ? bar_rgb : 24'h00_00_00;

    assign irq_vsync = v_vs;

    // ---------------------------------------------------------------- vsync 回读
    reg vs_tog;
    reg vt_meta, vt_sync, vt_sync_d;

    always @(posedge pix_clk_i) begin
        if (pix_rst) vs_tog <= 1'b0;
        else if (v_vs && !vs_d_pix) vs_tog <= ~vs_tog;
    end

    always @(posedge s_axi_aclk) begin
        if (!aresetn) begin
            vt_meta   <= 1'b0;
            vt_sync   <= 1'b0;
            vt_sync_d <= 1'b0;
            status_vsync <= 1'b0;
        end else begin
            vt_meta   <= vs_tog;
            vt_sync   <= vt_meta;
            vt_sync_d <= vt_sync;

            if (vt_sync != vt_sync_d) status_vsync <= 1'b1;
            if (ar_hs && (s_axi_araddr[7:2] == 6'h01)) status_vsync <= 1'b0;
        end
    end

endmodule
