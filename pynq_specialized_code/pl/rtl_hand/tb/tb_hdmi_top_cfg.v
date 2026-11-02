// ============================================================================
// tb_hdmi_top_cfg.v —— hdmi_out_top 的 AXI-Lite + 彩条 + enable 判据
//
// 判据（契约 §3.3）：
//   ① AXI-Lite 各寄存器读写正确（含 RO 位写不坏、STATUS 读清）
//   ② 越界地址不挂总线（bvalid/rvalid 必须回）
//   ③ test_pattern=1 时输出与 hdmi_color_bar 逐像素一致
//   ④ enable=0 时 de 恒 0
//
// 仿真用 MODE_DEFAULT=3（640x480@60）以缩短时间；综合默认是 MODE_DEFAULT=1
// （1080p60，厂商验证过的模式）。pix_clk 用 25.175 MHz。
// ============================================================================
`timescale 1ns / 1ps

module tb_hdmi_top_cfg;

    // ------------------------------------------------------------------ 时钟
    reg aclk = 0;
    reg pix_clk = 0;
    always #5.000 aclk = ~aclk;          // 100 MHz
    always #19.861 pix_clk = ~pix_clk;   // 25.175 MHz (640x480)

    reg aresetn = 0;

    // ------------------------------------------------------------------ AXI
    reg  [31:0] awaddr = 0;
    reg         awvalid = 0;
    wire        awready;
    reg  [31:0] wdata = 0;
    reg  [3:0]  wstrb = 4'hF;
    reg         wvalid = 0;
    wire        wready;
    wire [1:0]  bresp;
    wire        bvalid;
    reg         bready = 0;
    reg  [31:0] araddr = 0;
    reg         arvalid = 0;
    wire        arready;
    wire [31:0] rdata;
    wire [1:0]  rresp;
    wire        rvalid;
    reg         rready = 0;

    // ------------------------------------------------------------------ I2C
    wire scl, sda;
    pullup (scl);
    pullup (sda);
    wire i2c_scl_o, i2c_scl_t, i2c_sda_o, i2c_sda_t;
    assign scl = i2c_scl_t ? 1'bz : i2c_scl_o;
    assign sda = i2c_sda_t ? 1'bz : i2c_sda_o;

    reg slave_low = 0;
    assign sda = slave_low ? 1'b0 : 1'bz;

    // 极简 I2C slave：所有字节 ACK，用于让 init_done 起来
    reg        i2c_active = 0;
    reg [3:0]  i2c_bit = 0;
    reg [7:0]  i2c_sh = 0;
    reg        scl_s = 0, scl_d = 0, sda_s = 0, sda_d = 0;

    always @(posedge aclk) begin
        scl_s <= scl; sda_s <= sda;
        scl_d <= scl_s; sda_d <= sda_s;

        if (sda_s != sda_d && scl_s) begin
            if (sda_s == 1'b0) begin
                i2c_active <= 1'b1;
                i2c_bit    <= 4'd0;
                i2c_sh     <= 8'd0;
                slave_low  <= 1'b0;
            end else begin
                i2c_active <= 1'b0;
                slave_low  <= 1'b0;
            end
        end

        if (i2c_active) begin
            if (scl_s && !scl_d) begin
                if (i2c_bit < 8) i2c_sh[7-i2c_bit] <= sda_s;
                i2c_bit <= i2c_bit + 4'd1;
            end
            if (!scl_s && scl_d) begin
                if (i2c_bit == 8) slave_low <= 1'b1;       // ACK
                else if (i2c_bit == 9) begin
                    slave_low <= 1'b0;
                    i2c_bit   <= 4'd0;
                end
            end
        end
    end

    // ------------------------------------------------------------------ DUT
    wire        hdmi_clk, hdmi_de, hdmi_hs, hdmi_vs;
    wire [23:0] hdmi_d;
    wire        hdmi_rst_n;
    wire        irq_vsync;
    wire        init_done;

    hdmi_out_top #(
        .MODE_DEFAULT (3),
        .AXI_HZ       (100_000_000)
    ) dut (
        .s_axi_aclk   (aclk),
        .aresetn      (aresetn),
        .pix_clk      (pix_clk),

        .s_axi_awaddr (awaddr), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata  (wdata),  .s_axi_wstrb(wstrb),
        .s_axi_wvalid (wvalid), .s_axi_wready(wready),
        .s_axi_bresp  (bresp),  .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_araddr (araddr), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rdata  (rdata),  .s_axi_rresp(rresp),
        .s_axi_rvalid (rvalid), .s_axi_rready(rready),

        .m_axi_araddr (),
        .m_axi_arlen  (),
        .m_axi_arsize (),
        .m_axi_arburst(),
        .m_axi_arvalid(),
        .m_axi_arready(1'b0),
        .m_axi_rdata  (64'd0),
        .m_axi_rresp  (2'd0),
        .m_axi_rlast  (1'b0),
        .m_axi_rvalid (1'b0),
        .m_axi_rready (),

        .hdmi_clk     (hdmi_clk),
        .hdmi_de      (hdmi_de),
        .hdmi_hs      (hdmi_hs),
        .hdmi_vs      (hdmi_vs),
        .hdmi_d       (hdmi_d),

        .hdmi_rst_n   (hdmi_rst_n),
        .i2c_scl_o    (i2c_scl_o),
        .i2c_scl_t    (i2c_scl_t),
        .i2c_sda_i    (sda),
        .i2c_sda_o    (i2c_sda_o),
        .i2c_sda_t    (i2c_sda_t),

        .irq_vsync    (irq_vsync),
        .init_done    (init_done)
    );

    // ------------------------------------------------------------------ 参考彩条
    wire [23:0] ref_rgb;
    hdmi_color_bar #(
        .H_ACTIVE (640),
        .V_ACTIVE (480),
        .LINE_W   (4)
    ) u_ref_bar (
        .de        (dut.u_timing.de),
        .x         (dut.u_timing.x),
        .y         (dut.u_timing.y),
        .line_x    (dut.line_x),
        .motion_en (dut.cfg_motion),
        .rgb       (ref_rgb)
    );

    // ------------------------------------------------------------------ 逐像素比较
    reg        cmp_en = 0;
    integer    cmp_pixels = 0;
    integer    cmp_errs = 0;
    integer    de_errs = 0;
    integer    border_errs = 0;
    reg        zero_en = 0;
    integer    zero_de_errs = 0;
    integer    zero_d_errs = 0;

    always @(negedge pix_clk) begin
        if (cmp_en) begin
            if (dut.u_timing.de) begin
                cmp_pixels = cmp_pixels + 1;
                if (hdmi_d !== ref_rgb) begin
                    cmp_errs = cmp_errs + 1;
                    if (cmp_errs < 5)
                        $display("  [cmp] MISMATCH x=%0d y=%0d dut=%06h ref=%06h",
                                 dut.u_timing.x, dut.u_timing.y, hdmi_d, ref_rgb);
                end
                if (hdmi_de !== 1'b1) de_errs = de_errs + 1;

                // 边框独立核对（不依赖参考实例）
                if ((dut.u_timing.x == 0) || (dut.u_timing.x == 639) ||
                    (dut.u_timing.y == 0) || (dut.u_timing.y == 479)) begin
                    if (hdmi_d !== 24'hFF_FF_FF) border_errs = border_errs + 1;
                end
            end
        end

        if (zero_en) begin
            if (hdmi_de !== 1'b0) zero_de_errs = zero_de_errs + 1;
            if (hdmi_d  !== 24'h00_00_00) zero_d_errs = zero_d_errs + 1;
        end
    end

    // ------------------------------------------------------------------ AXI BFM
    task axi_write;
        input [31:0] a;
        input [31:0] d;
        reg aw_done, w_done;
        begin
            @(posedge aclk);
            awaddr  <= a;
            awvalid <= 1'b1;
            wdata   <= d;
            wstrb   <= 4'hF;
            wvalid  <= 1'b1;
            aw_done = 1'b0;
            w_done  = 1'b0;
            while (!aw_done || !w_done) begin
                @(posedge aclk);
                if (awvalid && awready) begin awvalid <= 1'b0; aw_done = 1'b1; end
                if (wvalid  && wready)  begin wvalid  <= 1'b0; w_done  = 1'b1; end
            end
            wait (bvalid === 1'b1);
            @(posedge aclk);
            bready <= 1'b1;
            @(posedge aclk);
            bready <= 1'b0;
            wait (bvalid === 1'b0);
        end
    endtask

    task axi_read;
        input  [31:0] a;
        output [31:0] d;
        reg rd_done;
        begin
            @(posedge aclk);
            araddr  <= a;
            arvalid <= 1'b1;
            rd_done = 1'b0;
            while (!rd_done) begin
                @(posedge aclk);
                if (arvalid && arready) begin arvalid <= 1'b0; rd_done = 1'b1; end
            end
            wait (rvalid === 1'b1);
            d = rdata;
            @(posedge aclk);
            rready <= 1'b1;
            @(posedge aclk);
            rready <= 1'b0;
            wait (rvalid === 1'b0);
        end
    endtask

    // ------------------------------------------------------------------ 帧同步
    task frame_start;
        begin
            wait (dut.u_timing.vs === 1'b1);
            wait (dut.u_timing.vs === 1'b0);
        end
    endtask

    task frame_end;
        begin
            wait (dut.u_timing.vs === 1'b1);
        end
    endtask

    // ------------------------------------------------------------------ 主流程
    integer    k;
    integer    fails = 0;
    reg [31:0] rd;
    reg [15:0] lx1, lx2;
    time       t0, t_done;

    initial begin
        $display("=== tb_hdmi_top_cfg (MODE=3 640x480, aclk=100M, pix=25.175M) ===");
        aresetn = 0;
        #200;
        aresetn = 1;

        wait (init_done === 1'b1);
        t_done = $time;
        $display("  init_done asserted at %0d ns (%.3f ms)", t_done, t_done/1000000.0);

        // ---------------------------------------------------------- ① 寄存器
        axi_read(32'h04, rd);
        if (!rd[0]) begin fails=fails+1; $display("  FAIL STATUS.init_done=%b", rd[0]); end
        $display("  STATUS = 0x%08h (init_done=%b vsync=%b underflow=%b)", rd, rd[0], rd[1], rd[2]);

        axi_read(32'h00, rd);
        $display("  CTRL reset = 0x%08h (expect 0x15: enable=1 test=1 motion=1)", rd);
        if ((rd & 32'h1F) != 32'h15) begin fails=fails+1; $display("  FAIL CTRL reset value"); end

        axi_write(32'h00, 32'h0000_0011);   // enable=1 test=0 motion=1
        axi_read (32'h00, rd);
        if ((rd & 32'h1F) != 32'h11) begin fails=fails+1; $display("  FAIL CTRL write 0x11 -> 0x%08h", rd); end

        axi_write(32'h00, 32'h0000_0015);   // 恢复
        axi_read (32'h00, rd);
        if ((rd & 32'h1F) != 32'h15) begin fails=fails+1; $display("  FAIL CTRL write 0x15 -> 0x%08h", rd); end

        // RO 位写不坏：写 STATUS / FRAME_ID_LATCHED
        axi_write(32'h04, 32'hFFFF_FFFF);
        axi_read (32'h04, rd);
        if ((rd & 32'hFFFF_FFF8) != 0 || rd[2]) begin
            fails=fails+1; $display("  FAIL STATUS RO bits writable: 0x%08h", rd);
        end
        axi_write(32'h24, 32'hDEAD_BEEF);
        axi_read (32'h24, rd);
        if (rd != 0) begin fails=fails+1; $display("  FAIL FRAME_ID_LATCHED not RO: 0x%08h", rd); end

        // VIDEO_MODE
        axi_read (32'h08, rd);
        if ((rd & 3) != 3) begin fails=fails+1; $display("  FAIL VIDEO_MODE reset=%0d expect 3", rd); end
        axi_write(32'h08, 32'd0);
        axi_read (32'h08, rd);
        if ((rd & 3) != 0) begin fails=fails+1; $display("  FAIL VIDEO_MODE write 0 -> %0d", rd); end
        axi_write(32'h08, 32'd3);

        // FB 寄存器
        axi_write(32'h0C, 32'h1234_5000); axi_read(32'h0C, rd);
        if (rd != 32'h1234_5000) begin fails=fails+1; $display("  FAIL FB_ADDR_LO=0x%08h", rd); end
        axi_write(32'h10, 32'h0000_0000); axi_read(32'h10, rd);
        if (rd != 0) begin fails=fails+1; $display("  FAIL FB_ADDR_HI=0x%08h", rd); end
        axi_write(32'h14, 32'd1920); axi_read(32'h14, rd);
        if (rd != 1920) begin fails=fails+1; $display("  FAIL FB_STRIDE=%0d", rd); end
        axi_write(32'h18, 32'd640); axi_read(32'h18, rd);
        if (rd != 640) begin fails=fails+1; $display("  FAIL FB_W=%0d", rd); end
        axi_write(32'h1C, 32'd480); axi_read(32'h1C, rd);
        if (rd != 480) begin fails=fails+1; $display("  FAIL FB_H=%0d", rd); end
        axi_write(32'h20, 32'd7); axi_read(32'h20, rd);
        if (rd != 7) begin fails=fails+1; $display("  FAIL FRAME_ID=%0d", rd); end
        axi_write(32'h28, 32'd0); axi_read(32'h28, rd);
        if (rd != 0) begin fails=fails+1; $display("  FAIL PIXEL_FMT=%0d", rd); end

        // ---------------------------------------------------------- ② 越界地址
        axi_write(32'h40, 32'hA5A5_5A5A);   // 仍在 64 KB 块内，但超出寄存器范围
        axi_read (32'h40, rd);
        if (rd != 0) begin fails=fails+1; $display("  FAIL out-of-range read 0x40 = 0x%08h", rd); end
        axi_write(32'hFFC, 32'h1234_5678);
        axi_read (32'hFFC, rd);
        if (rd != 0) begin fails=fails+1; $display("  FAIL out-of-range read 0xFFC = 0x%08h", rd); end
        $display("  out-of-range writes/reads completed (bus not hung)");

        // ---------------------------------------------------------- ③ 彩条逐像素
        axi_write(32'h00, 32'h0000_0015);   // enable=1 test=1 motion=1
        cmp_pixels = 0; cmp_errs = 0; de_errs = 0; border_errs = 0;
        frame_start;
        lx1 = dut.line_x;
        cmp_en = 1;
        frame_end;
        cmp_en = 0;
        #100000;                 // 离开 vs 边沿 100 us，避免读状态与 vsync 撞在同一拍
        lx2 = dut.line_x;
        $display("  bar compare: pixels=%0d (expect 307200)  mismatches=%0d  de_errs=%0d  border_errs=%0d",
                 cmp_pixels, cmp_errs, de_errs, border_errs);
        $display("  moving line: line_x %0d -> %0d (expect +8 per frame)", lx1, lx2);
        if (lx2 !== (lx1 + 16'd8)) begin fails=fails+1; $display("  FAIL line_x step"); end
        if (cmp_pixels != 307200) begin fails=fails+1; $display("  FAIL pixel count"); end
        if (cmp_errs != 0)       begin fails=fails+1; $display("  FAIL bar mismatches"); end
        if (de_errs != 0)        begin fails=fails+1; $display("  FAIL hdmi_de != de"); end
        if (border_errs != 0)    begin fails=fails+1; $display("  FAIL border"); end

        // STATUS.vsync 读清（此时至少发生过 1 次 vsync）
        axi_read(32'h04, rd);
        $display("  STATUS after frame = 0x%08h (vsync=%b, expect 1)", rd, rd[1]);
        if (!rd[1]) begin fails=fails+1; $display("  FAIL STATUS.vsync did not latch"); end
        axi_read(32'h04, rd);
        if (rd[1]) begin fails=fails+1; $display("  FAIL STATUS.vsync not cleared by read"); end

        // ---------------------------------------------------------- ④ enable=0
        axi_write(32'h00, 32'h0000_0014);   // enable=0 test=1 motion=1
        zero_en = 1; zero_de_errs = 0; zero_d_errs = 0;
        frame_start;
        frame_end;
        zero_en = 0;
        $display("  enable=0 frame: de_high_samples=%0d  d_nonzero_samples=%0d",
                 zero_de_errs, zero_d_errs);
        if (zero_de_errs != 0) begin fails=fails+1; $display("  FAIL de not 0 when enable=0"); end
        if (zero_d_errs  != 0) begin fails=fails+1; $display("  FAIL d not 0 when enable=0"); end

        // ---------------------------------------------------------- motion=0 对比
        axi_write(32'h00, 32'h0000_0005);   // enable=1 test=1 motion=0
        cmp_pixels = 0; cmp_errs = 0; de_errs = 0; border_errs = 0;
        frame_start;
        cmp_en = 1;
        frame_end;
        cmp_en = 0;
        $display("  motion=0 compare: pixels=%0d mismatches=%0d", cmp_pixels, cmp_errs);
        if (cmp_pixels != 307200 || cmp_errs != 0) begin
            fails=fails+1; $display("  FAIL motion=0 bar compare");
        end

        // ---------------------------------------------------------- 恢复 + soft_reset
        axi_write(32'h00, 32'h0000_0015);
        axi_write(32'h00, 32'h0000_0017);   // soft_reset=1
        axi_read (32'h00, rd);
        if (rd[1]) begin fails=fails+1; $display("  FAIL CTRL.soft_reset not self-cleared"); end
        $display("  CTRL after soft_reset write = 0x%08h", rd);

        $display("--------------------------------------------------------------");
        if (fails == 0) $display("RESULT: PASS  (all top-level criteria met, errors = 0)");
        else            $display("RESULT: FAIL  (errors = %0d)", fails);
        $display("--------------------------------------------------------------");
        $finish;
    end

    // ------------------------------------------------------------------ 超时
    integer to_cnt = 0;
    always @(posedge aclk) begin
        to_cnt <= to_cnt + 1;
        if (to_cnt == 20000000) begin   // 200 ms
            $display("RESULT: TIMEOUT");
            $finish;
        end
    end

endmodule
