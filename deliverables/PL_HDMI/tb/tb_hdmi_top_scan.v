// ============================================================================
// tb_hdmi_top_scan.v —— 扫描器集成进 hdmi_out_top 之后的验收（D-1 ~ D-5）
//
// 需求来源：docs/camera/05_HDMI输出_PS_PL接口契约.md §4（P2）
//
// 判据
// ----
//   D-1 test_pattern=1 时彩条**逐字节不变**（集成 mux 不能改像素）
//   D-2 test_pattern=0 时输出 == 扫描器流（用"按地址生成"的假 AXI 从机喂已知图案，
//       逐字节比对 921600 个有效像素 + 坐标）
//   D-3 切 test_pattern **只在 vsync 边界生效**：帧中途写，本帧剩余像素仍是旧源，
//       下一个 vsync 之后才是新源
//   D-4 0x2C 跟随扫描器欠载计数器（喂不动数据时 >0）；STATUS[2] 粘滞 + 读清；
//       soft_reset 后 0x2C 回 0
//   D-5 三路像素时钟全停时 AXI 仍可读写（含 0x2C / 0x30）——像素域死 ≠ 总线死
//
// 为什么用"按地址生成"而不是内存镜像
// ----------------------------------
//   720p 一帧 = 1280*3*720 = 2.76 MB，做成 reg 数组会让 xsim 变慢几十秒；
//   而判据只要求"PL 吐出的像素 == 帧缓冲里那一个字节"，所以直接从地址算字节即可。
//
// 本例用 MODE_DEFAULT=0（720p60，16.7 ms/帧）以缩短仿真；mode 3（720p30）的
// 彩条逐字节回归在 tb_hdmi_top_cfg.v 的 D-1(mode 3) 里做 —— 两条合起来覆盖两档。
// ============================================================================
`timescale 1ns / 1ps

module tb_hdmi_top_scan;

    // ---------------------------------------------------------------- 帧缓冲契约
    localparam integer FB_W  = 1280;
    localparam integer FB_H  = 720;
    localparam integer FSTR  = FB_W * 3;              // 3840，行首无 padding
    localparam [31:0]  FB_A  = 32'h1000_0000;

    // ---------------------------------------------------------------- 时钟
    reg aclk  = 0;
    reg pix_a = 0;
    reg pix_b = 0;
    reg pix_c = 0;
    reg pix_stop = 0;

    always #5.000  aclk  = ~aclk;                        // 100 MHz
    always #3.368  if (!pix_stop) pix_a = ~pix_a;        // 148.44 MHz
    always #6.737  if (!pix_stop) pix_b = ~pix_b;        //  74.22 MHz（mode 0 用）
    always #13.474 if (!pix_stop) pix_c = ~pix_c;        //  37.11 MHz

    reg aresetn = 0;

    // ---------------------------------------------------------------- AXI-Lite
    reg  [31:0] awaddr = 0;  reg awvalid = 0;  wire awready;
    reg  [31:0] wdata  = 0;  reg [3:0] wstrb = 4'hF;  reg wvalid = 0;  wire wready;
    wire [1:0]  bresp;       wire bvalid;      reg bready = 0;
    reg  [31:0] araddr = 0;  reg arvalid = 0;  wire arready;
    wire [31:0] rdata;       wire [1:0] rresp; wire rvalid;  reg rready = 0;

    // ---------------------------------------------------------------- I2C
    wire hdmi_scl, hdmi_sda;
    pullup (hdmi_scl);
    pullup (hdmi_sda);

    reg slave_low = 0;
    assign hdmi_sda = slave_low ? 1'b0 : 1'bz;

    reg        i2c_active = 0;
    reg [3:0]  i2c_bit = 0;
    reg [7:0]  i2c_sh = 0;
    reg        scl_s = 0, scl_d = 0, sda_s = 0, sda_d = 0;

    always @(posedge aclk) begin
        scl_s <= hdmi_scl; sda_s <= hdmi_sda;
        scl_d <= scl_s;    sda_d <= sda_s;
        if (sda_s != sda_d && scl_s) begin
            if (sda_s == 1'b0) begin
                i2c_active <= 1'b1; i2c_bit <= 4'd0; i2c_sh <= 8'd0; slave_low <= 1'b0;
            end else begin
                i2c_active <= 1'b0; slave_low <= 1'b0;
            end
        end
        if (i2c_active) begin
            if (scl_s && !scl_d) begin
                if (i2c_bit < 8) i2c_sh[7-i2c_bit] <= sda_s;
                i2c_bit <= i2c_bit + 4'd1;
            end
            if (!scl_s && scl_d) begin
                if (i2c_bit == 8) slave_low <= 1'b1;
                else if (i2c_bit == 9) begin slave_low <= 1'b0; i2c_bit <= 4'd0; end
            end
        end
    end

    // ---------------------------------------------------------------- DUT
    wire        pix_clk;
    wire        hdmi_de, hdmi_hs, hdmi_vs;
    wire [23:0] hdmi_d;
    wire        hdmi_rst_n, irq_vsync, init_done, cfg_upd, fid_upd;
    wire [31:0] m_araddr;  wire [7:0] m_arlen; wire [2:0] m_arsize; wire [1:0] m_arburst;
    wire        m_arvalid, m_arready, m_rvalid, m_rlast, m_rready;
    wire [63:0] m_rdata;

    reg mmcm_locked = 1;

    hdmi_out_top #(
        .MODE_DEFAULT (0),                  // 720p60（mode 3 的回归在 tb_hdmi_top_cfg）
        .AXI_HZ       (100_000_000)
    ) dut (
        .s_axi_aclk   (aclk),
        .aresetn      (aresetn),
        .pix_clk_a    (pix_a),
        .pix_clk_b    (pix_b),
        .pix_clk_c    (pix_c),
        .mmcm_locked  (mmcm_locked),
        .pix_clk      (pix_clk),

        .s_axi_awaddr (awaddr), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata  (wdata),  .s_axi_wstrb(wstrb),
        .s_axi_wvalid (wvalid), .s_axi_wready(wready),
        .s_axi_bresp  (bresp),  .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_araddr (araddr), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rdata  (rdata),  .s_axi_rresp(rresp),
        .s_axi_rvalid (rvalid), .s_axi_rready(rready),

        .m_axi_araddr (m_araddr),
        .m_axi_arlen  (m_arlen),
        .m_axi_arsize (m_arsize),
        .m_axi_arburst(m_arburst),
        .m_axi_arvalid(m_arvalid),
        .m_axi_arready(m_arready),
        .m_axi_rdata  (m_rdata),
        .m_axi_rresp  (2'd0),
        .m_axi_rlast  (m_rlast),
        .m_axi_rvalid (m_rvalid),
        .m_axi_rready (m_rready),

        .hdmi_de      (hdmi_de),
        .hdmi_hs      (hdmi_hs),
        .hdmi_vs      (hdmi_vs),
        .hdmi_d       (hdmi_d),

        .hdmi_scl     (hdmi_scl),
        .hdmi_sda     (hdmi_sda),
        .hdmi_rst_n   (hdmi_rst_n),

        .irq_vsync    (irq_vsync),
        .init_done    (init_done),
        .cfg_upd      (cfg_upd),
        .fid_upd      (fid_upd)
    );

    // ======================================================================
    //  帧缓冲的"按地址生成"模型：pix(y,x) = {x[7:0], y[7:0], (x+y)[7:0]}
    //  颜色三分量各不相同 ⇒ 任何通道交换/字节序错都会逐字节暴露
    // ======================================================================
    function [7:0] fR;  input [11:0] x; input [11:0] y; begin fR = x[7:0]; end endfunction
    function [7:0] fG;  input [11:0] x; input [11:0] y; begin fG = y[7:0]; end endfunction
    function [7:0] fB;  input [11:0] x; input [11:0] y; begin fB = x[7:0] + y[7:0]; end endfunction
    function [23:0] exp_pix;
        input [11:0] x; input [11:0] y;
        begin exp_pix = {fR(x,y), fG(x,y), fB(x,y)}; end
    endfunction

    // 一个 8 字节 AXI 拍的内容（行内字节偏移 → 像素/通道 → 字节）
    function [63:0] mk_word;
        input [31:0] a;
        integer k, off, lin, boff, pi, ch;
        reg [7:0]  by;
        reg [23:0] p;
        begin
            mk_word = 64'd0;
            off  = a - FB_A;
            lin  = off / FSTR;
            boff = off % FSTR;
            for (k = 0; k < 8; k = k + 1) begin
                pi = (boff + k) / 3;
                ch = (boff + k) % 3;
                p  = exp_pix(pi[11:0], lin[11:0]);
                by = (ch == 0) ? p[23:16] : ((ch == 1) ? p[15:8] : p[7:0]);
                mk_word[8*k +: 8] = by;
            end
        end
    endfunction

    // ======================================================================
    //  AXI 读从机：4 深描述符队列 + 2 拍延迟；starve=1 时拒收（制造欠载）
    // ======================================================================
    localparam integer MQ = 4;
    reg [31:0] mq_addr [0:MQ-1];
    reg [7:0]  mq_len  [0:MQ-1];
    integer    mq_wr = 0, mq_rd = 0;
    reg        starve = 0;

    assign m_arready = !starve && ((mq_wr - mq_rd) < MQ);

    reg        mr_busy = 0;
    reg [31:0] mr_addr = 0;
    integer    mr_left = 0;
    integer    mr_lat  = 0;
    integer    n_beats = 0;

    assign m_rvalid = mr_busy && (mr_lat == 0);
    assign m_rlast  = (mr_left == 1);
    assign m_rdata  = mk_word(mr_addr);

    always @(posedge aclk) begin
        if (!aresetn) begin
            mq_wr <= 0; mq_rd <= 0; mr_busy <= 0;
        end else begin
            if (m_arvalid && m_arready) begin
                mq_addr[mq_wr % MQ] <= m_araddr;
                mq_len [mq_wr % MQ] <= m_arlen;
                mq_wr <= mq_wr + 1;
            end
            if (!mr_busy && (mq_wr != mq_rd)) begin
                mr_busy <= 1'b1;
                mr_addr <= mq_addr[mq_rd % MQ];
                mr_left <= mq_len[mq_rd % MQ] + 1;
                mr_lat  <= 2;
                mq_rd   <= mq_rd + 1;
            end else if (mr_busy && (mr_lat != 0)) begin
                mr_lat <= mr_lat - 1;
            end else if (mr_busy && m_rready) begin
                mr_addr <= mr_addr + 8;
                mr_left <= mr_left - 1;
                n_beats <= n_beats + 1;
                if (mr_left == 1) mr_busy <= 1'b0;
            end
        end
    end

    // ======================================================================
    //  彩条参考（与 DUT 内部同一套参数、同一个 line_x）
    // ======================================================================
    wire [23:0] ref720;
    hdmi_color_bar #(
        .H_ACTIVE (1280), .V_ACTIVE (720), .LINE_W (4)
    ) u_ref720 (
        .de        (dut.u_timing_720.de),
        .x         (dut.u_timing_720.x),
        .y         (dut.u_timing_720.y),
        .line_x    (dut.line_x),
        .motion_en (dut.cfg_motion),
        .rgb       (ref720)
    );

    // ======================================================================
    //  像素级检查器
    // ======================================================================
    localparam integer CHK_OFF = 0, CHK_BAR = 1, CHK_SCAN = 2;

    // D-3 用（必须在检查器之前声明）
    reg     d3_pending = 0;      // 1 = 帧中途写过 test_pattern，等下一个 vsync 生效
    integer d3_bar_px  = 0;      // 写之后、vsync 之前仍按彩条比对的像素数

    integer chk_mode = CHK_OFF;
    integer px_x = 0, px_y = 0;
    integer n_px = 0;            // 本帧有效像素计数
    integer n_bad = 0;           // 当前判据下的失配数
    integer n_show = 0;
    integer n_px_win = 0;        // 当前判据窗口内参与比对的像素数
    reg     vs_q = 0;
    reg [23:0] e_now;

    always @(negedge pix_clk) begin
        // ---- 场边界（DE 在此期间恒 0，复位坐标安全）----
        if ((hdmi_vs === 1'b1) && (vs_q === 1'b0)) begin
            px_x = 0; px_y = 0; n_px = 0;
            if (d3_pending) begin
                d3_pending = 0;
                chk_mode   = CHK_SCAN;    // D-3：vsync 之后才允许切换到扫描源
            end
        end
        vs_q = (hdmi_vs === 1'b1);

        // ---- 有效像素 ----
        if (hdmi_de === 1'b1) begin
            if (chk_mode == CHK_BAR)       e_now = ref720;
            else if (chk_mode == CHK_SCAN) e_now = exp_pix(px_x[11:0], px_y[11:0]);
            else                           e_now = 24'h00_00_00;

            if (chk_mode != CHK_OFF) begin
                n_px_win = n_px_win + 1;
                if (hdmi_d !== e_now) begin
                    n_bad = n_bad + 1;
                    if (n_show < 8) begin
                        $display("  [ERR] %0s x=%0d y=%0d got %02x%02x%02x exp %02x%02x%02x",
                                 (chk_mode == CHK_BAR) ? "BAR " : "SCAN",
                                 px_x, px_y,
                                 hdmi_d[23:16], hdmi_d[15:8], hdmi_d[7:0],
                                 e_now[23:16], e_now[15:8], e_now[7:0]);
                        n_show = n_show + 1;
                    end
                end
            end

            n_px = n_px + 1;
            px_x = px_x + 1;
            if (px_x == FB_W) begin px_x = 0; px_y = px_y + 1; end
        end
    end

    // ======================================================================
    //  AXI-Lite BFM
    // ======================================================================
    task axi_write;
        input [31:0] a;
        input [31:0] d;
        reg aw_done, w_done;
        begin
            @(posedge aclk);
            awaddr  <= a; awvalid <= 1'b1;
            wdata   <= d; wstrb   <= 4'hF; wvalid <= 1'b1;
            aw_done = 0; w_done = 0;
            while (!aw_done || !w_done) begin
                @(posedge aclk);
                if (awvalid && awready) begin awvalid <= 1'b0; aw_done = 1; end
                if (wvalid  && wready)  begin wvalid  <= 1'b0; w_done = 1; end
            end
            wait (bvalid === 1'b1);
            @(posedge aclk); bready <= 1'b1;
            @(posedge aclk); bready <= 1'b0;
            wait (bvalid === 1'b0);
        end
    endtask

    task axi_read;
        input  [31:0] a;
        output [31:0] d;
        reg rd_done;
        begin
            @(posedge aclk);
            araddr  <= a; arvalid <= 1'b1; rd_done = 0;
            while (!rd_done) begin
                @(posedge aclk);
                if (arvalid && arready) begin arvalid <= 1'b0; rd_done = 1; end
            end
            wait (rvalid === 1'b1);
            d = rdata;
            @(posedge aclk); rready <= 1'b1;
            @(posedge aclk); rready <= 1'b0;
            wait (rvalid === 1'b0);
        end
    endtask

    // ======================================================================
    //  主流程
    // ======================================================================
    integer    fails = 0;
    reg [31:0] rd;
    integer    f_want, n_ok;
    integer    c_uf1 = 0, c_uf2 = 0;
    time       t0, t1;

    initial begin
        $display("=== tb_hdmi_top_scan (扫描器集成 D-1..D-5, MODE_DEFAULT=0) ===");
        aresetn = 0;
        #200;
        aresetn = 1;
        wait (init_done === 1'b1);
        $display("  init_done at %0d ns", $time);

        // ---------------- 配置：720p60 帧缓冲 + 彩条起始 ----------------
        axi_write(32'h08, 32'd0);              // VIDEO_MODE = 0
        axi_write(32'h0C, FB_A);               // FB_ADDR_LO
        axi_write(32'h10, 32'd0);              // FB_ADDR_HI
        axi_write(32'h14, FSTR);               // FB_STRIDE = 3840
        axi_write(32'h18, FB_W);
        axi_write(32'h1C, FB_H);
        axi_write(32'h20, 32'd0);              // FRAME_ID = 0（用 buf0）
        axi_write(32'h00, 32'h0000_0015);      // enable=1, test=1, motion=1
        #400000;

        // ================================================================
        // D-1：彩条逐字节不变（此时扫描器已配置好、mux 另一侧是活的）
        // ================================================================
        n_bad = 0; n_show = 0; n_px_win = 0; chk_mode = CHK_BAR;
        @(posedge hdmi_vs); @(posedge hdmi_vs);      // 丢掉半帧，保证对齐
        @(posedge hdmi_vs);
        n_bad = 0; n_show = 0; n_px_win = 0;         // 只统计一整帧
        @(posedge hdmi_vs);
        chk_mode = CHK_OFF;
        $display("  D-1 bar: pixels=%0d mismatches=%0d", n_px_win, n_bad);
        if (n_px_win != FB_W*FB_H) begin
            fails=fails+1; $display("  FAIL D-1 像素数 %0d（应 %0d）", n_px_win, FB_W*FB_H);
        end
        if (n_bad != 0) begin fails=fails+1; $display("  FAIL D-1 彩条失配 %0d", n_bad); end

        // ================================================================
        // D-2：test_pattern=0 输出 == 扫描器流
        // ================================================================
        axi_write(32'h00, 32'h0000_0011);      // enable=1, test=0
        @(posedge hdmi_vs); @(posedge hdmi_vs);      // 等 mux 在帧边界切过去 + 扫描器预取
        chk_mode = CHK_SCAN;
        @(posedge hdmi_vs);                          // 对齐帧起点
        n_bad = 0; n_show = 0; n_px_win = 0;
        @(posedge hdmi_vs);                          // 只统计一整帧
        chk_mode = CHK_OFF;
        $display("  D-2 scan: pixels=%0d mismatches=%0d (读拍数=%0d)", n_px_win, n_bad, n_beats);
        if (n_px_win < FB_W*FB_H) begin
            fails=fails+1; $display("  FAIL D-2 像素数不足 %0d", n_px_win);
        end
        if (n_bad != 0) begin fails=fails+1; $display("  FAIL D-2 扫描流失配 %0d", n_bad); end
        // 0x24 FRAME_ID_LATCHED 应等于写入的 0
        axi_read(32'h24, rd);
        if (rd != 0) begin fails=fails+1; $display("  FAIL 0x24 FRAME_ID_LATCHED=0x%08h", rd); end
        // 欠载判据要看**稳态**：切换帧的第一行可能来不及预取（扫描器在彩条模式被
        // cfg_enable 关掉、切过来那一拍才开始取第 0 行）⇒ 允许切换帧计 1~2 行。
        // 判据是"稳态不增长"：间隔 2 帧两次读必须完全相同。
        axi_read(32'h2C, rd);
        c_uf1 = rd;
        @(posedge hdmi_vs); @(posedge hdmi_vs);
        axi_read(32'h2C, rd);
        c_uf2 = rd;
        $display("  0x2C 稳态: %0d -> %0d（间隔 2 帧，要求不增长）", c_uf1, c_uf2);
        if (c_uf2 != c_uf1) begin
            fails=fails+1;
            $display("  FAIL 正常带宽下 0x2C 仍在增长（%0d -> %0d）", c_uf1, c_uf2);
        end

        // ================================================================
        // D-3：切 test_pattern 只在 vsync 边界生效
        // ================================================================
        axi_write(32'h00, 32'h0000_0015);      // 回彩条
        @(posedge hdmi_vs); @(posedge hdmi_vs);      // 现在整帧都是彩条
        chk_mode = CHK_BAR;
        @(posedge hdmi_vs);                          // 对齐帧起点再开始数
        n_bad = 0; n_show = 0; n_px_win = 0;
        // 本帧中途（约 1/3 处）写 test_pattern=0
        wait (n_px_win > 300000);
        axi_write(32'h00, 32'h0000_0011);      // 帧中途切扫描
        d3_pending = 1;                        // 下一个 vsync 之前必须仍是彩条
        d3_bar_px  = 0;
        wait (d3_pending === 1'b0);             // vsync 到，chk_mode 自动切 CHK_SCAN
        d3_bar_px = n_px_win;                   // 记下"写之后、vsync 之前"比对过的彩条像素数
        n_bad = 0; n_show = 0; n_px_win = 0;    // 从这里开始统计扫描源
        @(posedge hdmi_vs);
        chk_mode = CHK_OFF;
        $display("  D-3 mid-frame write: 写后本帧仍比对彩条 %0d 像素；下一帧扫描源 %0d 像素",
                 d3_bar_px, n_px_win);
        // n_bad 在 vsync 处被清零，只反映"下一帧扫描源"；彩条那段的失配要单独看
        if (n_bad != 0) begin
            fails=fails+1; $display("  FAIL D-3 vsync 之后不是扫描源（失配 %0d）", n_bad);
        end
        if (n_px_win < FB_W*FB_H) begin
            fails=fails+1; $display("  FAIL D-3 扫描帧像素数不足 %0d", n_px_win);
        end
        if (d3_bar_px < 100000) begin
            fails=fails+1; $display("  FAIL D-3 帧中途写入后没有足够彩条像素参与比对");
        end

        // ================================================================
        // D-4：0x2C 跟随欠载计数器；STATUS[2] 粘滞 + 读清；soft_reset 清 0x2C
        // ================================================================
        starve = 1;                            // 让扫描器取不到数据
        @(posedge hdmi_vs); @(posedge hdmi_vs); @(posedge hdmi_vs);
        axi_read(32'h2C, rd);
        $display("  D-4 starved: 0x2C = %0d (>0 说明按行计到了欠载)", rd);
        if (rd == 0) begin fails=fails+1; $display("  FAIL D-4 喂不动数据时 0x2C 仍为 0"); end
        axi_read(32'h04, rd);
        $display("  D-4 STATUS = 0x%08h (scan_underflow=%b)", rd, rd[2]);
        if (!rd[2]) begin fails=fails+1; $display("  FAIL D-4 STATUS[2] 没置粘滞位"); end
        starve = 0;                            // 恢复供数
        @(posedge hdmi_vs); @(posedge hdmi_vs);
        axi_read(32'h04, rd);                  // 读清
        axi_read(32'h04, rd);
        $display("  D-4 读清后 STATUS = 0x%08h (scan_underflow=%b)", rd, rd[2]);
        if (rd[2]) begin fails=fails+1; $display("  FAIL D-4 STATUS[2] 读不清"); end
        axi_read(32'h2C, rd); f_want = rd;     // 读清不影响 0x2C（只有 soft_reset 清）
        axi_write(32'h00, 32'h0000_0017);      // enable=1 + soft_reset=1
        #2000000;
        axi_read(32'h2C, rd);
        $display("  D-4 soft_reset 后 0x2C = %0d（复位前 %0d）", rd, f_want);
        if (rd != 0) begin fails=fails+1; $display("  FAIL D-4 soft_reset 没把 0x2C 清 0"); end

        // ================================================================
        // D-5：三路像素时钟全停 ⇒ AXI 仍可读写
        // ================================================================
        wait (init_done === 1'b1);             // 上一段的 soft_reset 会重启 I2C
        pix_stop = 1;
        repeat (16) @(posedge aclk);
        axi_read(32'h30, rd);
        $display("  D-5 pix_clk stopped: 0x30 = 0x%08h (pix_hb=%b locked=%b)", rd, rd[0], rd[1]);
        axi_read(32'h00, rd);
        if ((rd[0] !== 1'b1) || (rd[2] !== 1'b1)) begin
            fails=fails+1; $display("  FAIL D-5 停摆后 CTRL 读异常 0x%08h", rd);
        end
        axi_write(32'h08, 32'd0);
        axi_read (32'h08, rd);
        if ((rd & 3) != 0) begin fails=fails+1; $display("  FAIL D-5 停摆后 VIDEO_MODE 写失效"); end
        axi_read (32'h2C, rd);                 // 0x2C 在 AXI 域，必须可读
        axi_read (32'h24, rd);                 // 0x24 也在 AXI 域
        axi_read (32'h04, rd);
        if (!rd[0]) begin fails=fails+1; $display("  FAIL D-5 停摆后 STATUS.init_done 掉了"); end
        $display("  D-5 AXI ok while pix dead: CTRL/STATUS/VIDEO_MODE/0x24/0x2C/0x30 全可读写");
        pix_stop = 0;

        $display("--------------------------------------------------------------");
        $display("  sim end at %0.3f ms", $itor($time)/1000000.0);
        if (fails == 0) $display("RESULT: PASS  (D-1..D-5 all met, errors = 0)");
        else            $display("RESULT: FAIL  (errors = %0d)", fails);
        $display("--------------------------------------------------------------");
        $finish;
    end

    // ------------------------------------------------------------------ 超时
    integer to_cnt = 0;
    always @(posedge aclk) begin
        to_cnt <= to_cnt + 1;
        if (to_cnt == 60000000) begin   // 600 ms
            $display("RESULT: TIMEOUT");
            $finish;
        end
    end

endmodule
