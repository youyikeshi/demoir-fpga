// ============================================================================
// hdmi_out_top.v —— HDMI P1 顶层（视频时序 + 彩条 + SIL9134 I2C + AXI-Lite 配置页）
//
// 需求来源：docs/camera/05_HDMI输出_PS_PL接口契约.md §2 / §3.1
//
// 关键设计决定：
//   1) P1 默认模式 = 1080p60（MODE_DEFAULT=1）。理由：厂商 lut_sil9134.v 的两条
//      寄存器写是在 1080p60 例程里验证的，表里没有"视频模式"寄存器 ⇒ 换成
//      720p/1080p30/640x480 等于赌芯片自适应。四种模式的参数都保留，但
//      **仅 1080p60 经厂商验证**；其它模式上板前需按数据手册补视频模式寄存器。
//   2) 像素时钟不从 MMCM 出：pix_clk 是顶层输入（P3 由 PS FCLK1 提供 148.5 MHz）。
//      FCLK1 若分不出 148.5 MHz，回退 MMCM：M=11.875 / DIVCLK=1 / CLKOUT0=8
//      ⇒ 148.4375 MHz（-421 ppm），报告里必须写实际值。
//   3) 跨时钟域：
//        s_axi_aclk -> pix_clk : enable / test_pattern / motion_en
//                                每位 1 个 2FF 同步器（共 3 个，6 个 FF）
//        pix_clk -> s_axi_aclk : vsync 用"翻转电平 + 2FF + 边沿检测"
//                                （单拍脉冲不直接同步）
//        soft_reset / pwr_good : 电平信号，各 1 个 2FF
//        VIDEO_MODE / FB_*     : P1 只在 AXI 域读写，不参与 pix 逻辑 ⇒ 无跨域；
//                                P2 要动态换模式时改为"写使能+双拍握手"再引入。
//   4) AXI-Lite 越界地址（偏移 > 0x28）也回 bvalid/rvalid（OKAY + 0 数据），
//      不让 PS 的 mmap 访问挂总线。
//   5) I2C 输出是开漏语义（t=1 释放/上拉，t=0 驱动 0）。IOBUF 例化在 BD 顶层
//      （P3）：IOBUF .IO(hdmi_scl) .I(1'b0) .O(scl_i) .T(i2c_scl_t)；
//      本模块只按契约给出 o/t/i 三组端口。
//
// 寄存器图（32-bit，4 字节对齐）：
//   0x00 CTRL          RW  [0]enable [1]soft_reset(写1自清) [2]test_pattern
//                          [3]frame_swap_req [4]motion_en(彩条移动线)
//                          复位值 = 1_1101b? 见下：enable=1,test=1,motion=1
//   0x04 STATUS        RO  [0]init_done [1]vsync（读清）[2]scan_underflow(P2=0)
//   0x08 VIDEO_MODE    RW  [1:0]，复位=1(1080p60)；P1 只读回，不改变 pix 逻辑
//   0x0C FB_ADDR_LO    RW
//   0x10 FB_ADDR_HI    RW
//   0x14 FB_STRIDE     RW
//   0x18 FB_W          RW
//   0x1C FB_H          RW
//   0x20 FRAME_ID      RW
//   0x24 FRAME_ID_LATCHED RO（P1 恒 0）
//   0x28 PIXEL_FMT     RW
// ============================================================================
`timescale 1ns / 1ps

module hdmi_out_top #(
    parameter integer MODE_DEFAULT = 1,          // 0=720p60 1=1080p60 2=1080p30 3=640x480p60
    parameter integer AXI_HZ       = 100_000_000
) (
    // ---- 时钟/复位 ----
    input  wire        s_axi_aclk,
    input  wire        aresetn,
    input  wire        pix_clk,          // P3 由 PS FCLK1 = 148.5 MHz 提供

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

    // ---- 视频输出（直连 SIL9134）----
    output wire        hdmi_clk,         // = pix_clk（板上有 H1）
    output wire        hdmi_de,  output wire hdmi_hs, output wire hdmi_vs,
    output wire [23:0] hdmi_d,

    // ---- SIL9134 控制 ----
    output wire        hdmi_rst_n,
    output wire        i2c_scl_o, output wire i2c_scl_t,
    input  wire        i2c_sda_i, output wire i2c_sda_o, output wire i2c_sda_t,

    // ---- 状态 ----
    output wire        irq_vsync,
    output wire        init_done
);

    // ---------------------------------------------------------------- 模式参数
    // 与契约 §1 完全一致；厂商 color_bar.v 的 1080p 参数已核对
    localparam integer H_ACTIVE = (MODE_DEFAULT == 0) ? 1280 :
                                  (MODE_DEFAULT == 3) ?  640 : 1920;
    localparam integer H_TOTAL  = (MODE_DEFAULT == 0) ? 1650 :
                                  (MODE_DEFAULT == 3) ?  800 : 2200;
    localparam integer H_FP     = (MODE_DEFAULT == 0) ? 110  :
                                  (MODE_DEFAULT == 3) ?  16  : 88;
    localparam integer H_SYNC   = (MODE_DEFAULT == 0) ? 40   :
                                  (MODE_DEFAULT == 3) ?  96  : 44;
    localparam integer H_BP     = (MODE_DEFAULT == 0) ? 220  :
                                  (MODE_DEFAULT == 3) ?  48  : 148;
    localparam integer V_ACTIVE = (MODE_DEFAULT == 0) ? 720  :
                                  (MODE_DEFAULT == 3) ? 480  : 1080;
    localparam integer V_TOTAL  = (MODE_DEFAULT == 0) ? 750  :
                                  (MODE_DEFAULT == 3) ? 525  : 1125;
    localparam integer V_FP     = (MODE_DEFAULT == 0) ? 5    :
                                  (MODE_DEFAULT == 3) ? 10   : 4;
    localparam integer V_SYNC   = (MODE_DEFAULT == 0) ? 5    :
                                  (MODE_DEFAULT == 3) ? 2    : 5;
    localparam integer V_BP     = (MODE_DEFAULT == 0) ? 20   :
                                  (MODE_DEFAULT == 3) ? 33   : 36;

    localparam integer PWR_CYCLES = AXI_HZ / 1000;   // 1 ms 上电等待（与 hdmi_rst_n 合并）

    // ---------------------------------------------------------------- AXI 寄存器
    reg        ctrl_enable;
    reg        ctrl_test;
    reg        ctrl_fswap;
    reg        ctrl_motion;
    reg [1:0]  video_mode;
    reg [31:0] fb_addr_lo, fb_addr_hi, fb_stride, fb_w, fb_h;
    reg [31:0] frame_id;
    reg [31:0] pixel_fmt;
    reg        status_vsync;

    // AXI 写/读握手
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
    assign m_axi_arsize  = 3'b011;   // 8 bytes
    assign m_axi_arburst = 2'b01;    // INCR
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
                default: rd_mux = 32'd0;             // 越界地址：回 0，不挂总线
            endcase
        end
    endfunction

    // ---------------------------------------------------------------- AXI 写
    always @(posedge s_axi_aclk) begin
        if (!aresetn) begin
            aw_pending <= 1'b0;
            w_pending  <= 1'b0;
            bvalid     <= 1'b0;
            awaddr_r   <= 32'd0;
            wdata_r    <= 32'd0;
            wstrb_r    <= 4'd0;

            ctrl_enable <= 1'b1;     // 上电即出彩条，便于"点亮"判据
            ctrl_test   <= 1'b1;
            ctrl_fswap  <= 1'b0;
            ctrl_motion <= 1'b1;
            video_mode  <= (MODE_DEFAULT & 3);
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
                    6'h02: if (wstrb_r[0]) video_mode  <= wdata_r[1:0];
                    6'h03: fb_addr_lo  <= wdata_r;
                    6'h04: fb_addr_hi  <= wdata_r;
                    6'h05: fb_stride   <= wdata_r;
                    6'h06: fb_w        <= wdata_r;
                    6'h07: fb_h        <= wdata_r;
                    6'h08: frame_id    <= wdata_r;
                    6'h0A: pixel_fmt   <= wdata_r;
                    default: ;                         // 越界写：丢掉，但照常回 bvalid
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

    // soft_reset：写 CTRL[1]=1 产生 64 个 aclk 的复位电平（自清，读回 0）
    reg [6:0] soft_cnt;
    wire      soft_rst_axi = (soft_cnt != 7'd0);

    always @(posedge s_axi_aclk) begin
        if (!aresetn) soft_cnt <= 7'd0;
        else if (wr_commit && (awaddr_r[7:2] == 6'h00) && wstrb_r[0] && wdata_r[1])
            soft_cnt <= 7'd64;
        else if (soft_rst_axi)
            soft_cnt <= soft_cnt - 7'd1;
    end

    // ---------------------------------------------------------------- CDC
    // s_axi_aclk -> pix_clk：enable / test_pattern / motion_en，每位 2FF
    reg [2:0] cfg_meta, cfg_sync;
    // pix_clk -> s_axi_aclk：vsync 翻转电平 + 2FF + 边沿检测
    reg       vs_tog;
    reg       vt_meta, vt_sync, vt_sync_d;
    // 复位电平跨域（pwr_good / soft_rst_axi）
    reg       pg_meta, pg_sync;
    reg       sr_meta, sr_sync;

    always @(posedge pix_clk or negedge aresetn) begin
        if (!aresetn) begin
            pg_meta <= 1'b0; pg_sync <= 1'b0;
            sr_meta <= 1'b0; sr_sync <= 1'b0;
        end else begin
            pg_meta <= pwr_good;    pg_sync <= pg_meta;
            sr_meta <= soft_rst_axi; sr_sync <= sr_meta;
        end
    end

    wire pix_rst = ~pg_sync | sr_sync;

    always @(posedge pix_clk) begin
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

    // ---------------------------------------------------------------- I2C 初始化
    sil9134_init #(
        .CLK_HZ (AXI_HZ),
        .SCL_HZ (100_000),
        .POR_US (200)            // 上电 1 ms 由 hdmi_rst_n 合并，这里再等 0.2 ms
    ) u_sil9134_init (
        .clk       (s_axi_aclk),
        .rst       (~pwr_good | soft_rst_axi),
        .init_done (init_done),
        .busy      (),
        .error     (),
        .i2c_scl_o (i2c_scl_o),
        .i2c_scl_t (i2c_scl_t),
        .i2c_sda_i (i2c_sda_i),
        .i2c_sda_o (i2c_sda_o),
        .i2c_sda_t (i2c_sda_t)
    );

    // ---------------------------------------------------------------- 视频时序
    wire        v_hs, v_vs, v_de;
    wire [15:0] v_x, v_y;

    hdmi_timing_gen #(
        .H_TOTAL (H_TOTAL), .H_ACTIVE(H_ACTIVE), .H_FP(H_FP), .H_SYNC(H_SYNC), .H_BP(H_BP),
        .V_TOTAL (V_TOTAL), .V_ACTIVE(V_ACTIVE), .V_FP(V_FP), .V_SYNC(V_SYNC), .V_BP(V_BP),
        .HS_POL  (1), .VS_POL(1)
    ) u_timing (
        .pix_clk (pix_clk),
        .rst     (pix_rst),
        .hs      (v_hs),
        .vs      (v_vs),
        .de      (v_de),
        .x       (v_x),
        .y       (v_y)
    );

    // ---------------------------------------------------------------- 移动竖线
    reg        vs_d_pix;
    reg [15:0] line_x;

    always @(posedge pix_clk) begin
        if (pix_rst) begin
            vs_d_pix <= 1'b0;
            line_x   <= 16'd0;
        end else begin
            vs_d_pix <= v_vs;
            if (v_vs && !vs_d_pix) begin
                if ((line_x + 16'd8) >= H_ACTIVE) line_x <= 16'd0;
                else                               line_x <= line_x + 16'd8;
            end
        end
    end

    // ---------------------------------------------------------------- 彩条
    wire [23:0] bar_rgb;

    hdmi_color_bar #(
        .H_ACTIVE (H_ACTIVE),
        .V_ACTIVE (V_ACTIVE),
        .LINE_W   (4)
    ) u_color_bar (
        .de        (v_de),
        .x         (v_x),
        .y         (v_y),
        .line_x    (line_x),
        .motion_en (cfg_motion),
        .rgb       (bar_rgb)
    );

    // ---------------------------------------------------------------- 输出
    assign hdmi_clk = pix_clk;
    assign hdmi_hs  = v_hs;
    assign hdmi_vs  = v_vs;
    assign hdmi_de  = v_de && cfg_enable;
    assign hdmi_d   = (v_de && cfg_enable && cfg_test) ? bar_rgb : 24'h00_00_00;

    assign irq_vsync = v_vs;

    // ---------------------------------------------------------------- vsync 回读
    // pix 域：每帧翻转一次；aclk 域 2FF + 边沿检测 -> STATUS[1]，读清
    always @(posedge pix_clk) begin
        if (pix_rst) begin
            vs_tog <= 1'b0;
        end else if (v_vs && !vs_d_pix) begin
            vs_tog <= ~vs_tog;
        end
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
            if (ar_hs && (s_axi_araddr[7:2] == 6'h01)) status_vsync <= 1'b0;  // 读清
        end
    end

endmodule
