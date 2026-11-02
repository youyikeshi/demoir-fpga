// ============================================================================
// tb_hdmi_top_cfg.v —— hdmi_out_top 的 AXI-Lite + 双时钟切换 + 彩条判据
//
// 判据（契约 §3.3 + §7.1）：
//   ① AXI-Lite 各寄存器读写正确（RO 位写不坏、STATUS.vsync 读清、0x2C=0）
//   ② 越界地址不挂总线（bvalid/rvalid 必须回）
//   ③ test_pattern=1 输出与 hdmi_color_bar 逐像素一致
//   ④ enable=0 时 de 恒 0
//   ⑤ VIDEO_MODE 切两种像素时钟：切换期间 de 无高电平；切后 hs/de 参数正确
//   ⑥ cfg_upd / fid_upd 各打 1 拍
//   ⑦ 0x30 PIX_HB（事故 2026-11-03_0018 / 契约 §8.2 的"体检"寄存器）：
//      C-A 像素时钟正常时，连续两次读 0x30[0]（间隔 >= 2 ms）必须不同
//      C-B 把像素时钟停掉后，0x30[0] 必须冻结，且 0x00/0x04/0x08 仍可正常读写
//          （这条是本改动的核心：像素域死 != 总线死）
//      C-C mmcm_locked=0 -> 0x30[1]=0；置 1 后（>=2 aclk）-> 0x30[1]=1
//      C-D 0x30 只读：写 0xFFFFFFFF / 0x00000000 后回读值不被破坏（[0] 仍按原节奏翻转）
//      C-E enable=0 -> DE 落低的像素时钟拍数 de_settle <= 8（四种模式各测一次）；
//          de_bad 的 2 帧统计从 DE 真的落低之后才开始 ⇒ 既有覆盖、也没有盲区
//      C-F soft_reset 后 hb_cnt 不被清零（直接监视像素域计数器：每拍必须恰好 +1）
//
// 定格手段：pix_stop=1 时两路像素时钟都停止翻转（BUFG/BUFGMUX 输入不变 ⇒ pix_clk
// 恒电平），这正是"MMCM 没锁 / 像素时钟没跑"在位流里的样子。
//
// 仿真用 MODE_DEFAULT=0（720p60）以缩短时间；综合默认仍是 MODE_DEFAULT=1。
// 两路像素时钟由 TB 直接给（BD 里由 MMCM 产生）：
//   pix_clk_a ≈ 148.4375 MHz, pix_clk_b ≈ 74.21875 MHz
// ============================================================================
`timescale 1ns / 1ps

module tb_hdmi_top_cfg;

    // ------------------------------------------------------------------ 时钟
    reg aclk  = 0;
    reg pix_a = 0;
    reg pix_b = 0;

    reg pix_stop = 0;                  // 1 = 两路像素时钟停止翻转（C-B 用）
    always #5.000  aclk  = ~aclk;      // 100 MHz（AXI 域，永不停）
    always #3.368  if (!pix_stop) pix_a = ~pix_a;   // ~148.44 MHz
    always #6.737  if (!pix_stop) pix_b = ~pix_b;   // ~74.22 MHz

    reg aresetn = 0;

    // BD 里由 MMCM.locked 驱动；C-C 会把它拉到 0 再拉回 1
    reg mmcm_locked = 1;

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
    wire        pix_clk;
    wire        hdmi_de, hdmi_hs, hdmi_vs;
    wire [23:0] hdmi_d;
    wire        hdmi_rst_n;
    wire        irq_vsync;
    wire        init_done;
    wire        cfg_upd, fid_upd;

    hdmi_out_top #(
        .MODE_DEFAULT (0),
        .AXI_HZ       (100_000_000)
    ) dut (
        .s_axi_aclk   (aclk),
        .aresetn      (aresetn),
        .pix_clk_a    (pix_a),
        .pix_clk_b    (pix_b),
        .mmcm_locked  (mmcm_locked),
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

    // ------------------------------------------------------------------ 参考彩条（720p）
    wire [23:0] ref720;
    hdmi_color_bar #(
        .H_ACTIVE (1280),
        .V_ACTIVE (720),
        .LINE_W   (4)
    ) u_ref720 (
        .de        (dut.u_timing_720.de),
        .x         (dut.u_timing_720.x),
        .y         (dut.u_timing_720.y),
        .line_x    (dut.line_x),
        .motion_en (dut.cfg_motion),
        .rgb       (ref720)
    );

    // ------------------------------------------------------------------ 计数/比较
    integer pcyc = 0;
    always @(posedge pix_clk) pcyc <= pcyc + 1;

    reg     cmp_en = 0;
    integer cmp_pixels = 0;
    integer cmp_errs = 0;
    integer de_errs = 0;
    integer border_errs = 0;

    reg     zero_en = 0;
    integer zero_de_errs = 0;
    integer zero_d_errs = 0;

    reg     guard_en = 0;
    integer de_bad = 0;

    always @(negedge pix_clk) begin
        if (cmp_en) begin
            if (dut.u_timing_720.de) begin
                cmp_pixels = cmp_pixels + 1;
                if (hdmi_d !== ref720) begin
                    cmp_errs = cmp_errs + 1;
                    if (cmp_errs < 5)
                        $display("  [cmp] MISMATCH x=%0d y=%0d dut=%06h ref=%06h",
                                 dut.u_timing_720.x, dut.u_timing_720.y, hdmi_d, ref720);
                end
                if (hdmi_de !== 1'b1) de_errs = de_errs + 1;
                if ((dut.u_timing_720.x == 0) || (dut.u_timing_720.x == 1279) ||
                    (dut.u_timing_720.y == 0) || (dut.u_timing_720.y == 719)) begin
                    if (hdmi_d !== 24'hFF_FF_FF) border_errs = border_errs + 1;
                end
            end
        end

        if (zero_en) begin
            if (hdmi_de !== 1'b0) zero_de_errs = zero_de_errs + 1;
            if (hdmi_d  !== 24'h00_00_00) zero_d_errs = zero_d_errs + 1;
        end

        if (guard_en && hdmi_de !== 1'b0) de_bad = de_bad + 1;
    end

    // 任务会用到的模块级变量（必须在使用前声明）
    integer    fails = 0;
    reg [31:0] rd;
    reg [15:0] lx1, lx2;
    real       mhz;
    integer    hs_n, de_w;

    // cfg_upd / fid_upd 脉冲计数
    integer cfg_pulses = 0;
    integer fid_pulses = 0;
    always @(posedge aclk) begin
        if (cfg_upd) cfg_pulses <= cfg_pulses + 1;
        if (fid_upd) fid_pulses <= fid_pulses + 1;
    end

    // pix_clk 在"被停掉"期间的沿数（C-B 的直接证据：必须为 0）
    integer pstop_edges = 0;
    always @(posedge pix_clk) if (pix_stop) pstop_edges = pstop_edges + 1;

    // ------------------------------------------------------------------ 0x30 判据用
    reg        hb_1, hb_2, hb_3, hb_4;
    time       t_1, t_2;
    reg [31:0] ctrl_bak;

    // -------- C-E：enable=0 -> DE 落低的像素时钟拍数（门禁 <= 8，四种模式都测）--------
    // 参考点 = DUT 内部 wr_commit（写真的提交的那一拍，aclk 域）；
    // 终点   = hdmi_de 首次为 0；计数 = 自由运行的像素时钟沿计数器。
    // 两端都在 aclk 域采样 ⇒ 量化误差 ±1 拍（判据 8 拍、实测 2~3 拍，余量足够）。
    // 为什么要有这条：de_bad 的统计必须从 DE 真的落低之后才开始（否则把 2FF 固有延迟
    // 算成缺陷，结果取决于写落点在哪一拍）；但"移出统计"必须同时把这段变成**被测量的量**，
    // 否则等于制造盲区。de_settle > 8 ⇒ 使能/CDC 通路真有问题。
    integer pix_edge_n = 0;
    always @(posedge pix_clk) pix_edge_n = pix_edge_n + 1;

    reg     settle_armed = 1'b0;
    reg     settle_done  = 1'b1;
    integer settle_t0    = 0;
    integer de_settle    = 0;
    integer settle_max   = 0;
    integer lk_n         = 0;

    always @(posedge aclk) begin
        if (!settle_armed &&
            dut.wr_commit && (dut.awaddr_r[7:2] == 6'h00) &&
            dut.wstrb_r[0] && (dut.wdata_r[0] === 1'b0)) begin   // 写 CTRL.enable=0
            settle_armed = 1'b1;
            settle_done  = 1'b0;
            settle_t0    = pix_edge_n;
            de_settle    = 0;
        end
        if (settle_armed && !settle_done && (hdmi_de === 1'b0)) begin
            de_settle   = pix_edge_n - settle_t0;
            settle_done = 1'b1;
        end
    end

    // -------- C-F：soft_reset 期间 hb_cnt 不许被清零 ----------
    // 只看 0x30[0] "还在跳" 不足以证明没被清：清空后照样会继续跳，只是相位跳了一下。
    // 所以直接监视像素域计数器本身：每一拍必须恰好 +1，出现任何非 +1 跳变即判失败。
    reg        hb_mon   = 1'b0;
    integer    hb_jumps = 0;
    reg [16:0] hb_prev  = 17'd0;

    // 注意：监视器是在像素沿上比"上一沿之后的值 +1"，所以武装时必须把基准设成
    // hb_cnt-1（此刻 hb_cnt 还没有被下一个沿推进），否则第一个沿会误报一次跳变。
    // 17 bit 加法天然按 17 位截断 ⇒ 0x1FFFF->0x00000 的回绕不会被误判。
    always @(posedge pix_clk) begin
        if (hb_mon && (dut.hb_cnt !== (hb_prev + 17'd1))) hb_jumps = hb_jumps + 1;
        hb_prev = dut.hb_cnt;
    end

    // C-A：两次连续读 0x30[0]，间隔取 hb 半周期的**奇数倍**（5×2^16 个像素时钟）
    //   —— 为什么必须是奇数倍：pix_hb = hb_cnt[16] 的半周期 = 2^16 个像素时钟。
    //      间隔 Δ 内跨过的沿数 k 在 {floor(Δ/h), ceil(Δ/h)} 之间；只有 k 严格为奇数
    //      才能保证"两次读一定不同"。Δ/h 取奇数整数 ⇒ k 恒为该奇数，判据可复现，
    //      不靠相位运气（pix_clk_a: 5h = 2.207 ms；pix_clk_b: 5h = 4.415 ms，均 >= 2 ms）。
    task hb_check_alive;
        input [8*8-1:0] tag;
        reg   v1, v2;
        time  u1, u2;
        begin
            axi_read(32'h30, rd);
            v1 = rd[0]; u1 = $time;
            if (rd[31:2] != 30'd0) begin
                fails=fails+1; $display("  FAIL C-A[%0s] 保留位非 0: 0x%08h", tag, rd);
            end
            repeat (5*65536) @(posedge pix_clk);
            axi_read(32'h30, rd);
            v2 = rd[0]; u2 = $time;
            $display("  C-A[%0s] 0x30[0]: %b --(%0.4f ms)--> %b   [full 0x30=0x%08h]",
                     tag, v1, $itor(u2-u1)/1000000.0, v2, rd);
            if ((u2 - u1) < 2_000_000) begin
                fails=fails+1; $display("  FAIL C-A[%0s] 两次读间隔 < 2 ms", tag);
            end
            if (v1 === v2) begin
                fails=fails+1; $display("  FAIL C-A[%0s] 像素时钟在跑但 0x30[0] 没变", tag);
            end
        end
    endtask

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

    // ------------------------------------------------------------------ 测量
    task measure_pix_mhz;
        input  integer htotal;
        output real    mhz;
        time t1, t2;
        real per;
        begin
            @(posedge hdmi_hs); t1 = $time;
            @(posedge hdmi_hs); t2 = $time;
            per = $itor(t2 - t1) / htotal;
            mhz = 1000.0 / per;
        end
    endtask

    task measure_hs_clocks;
        output integer n;
        integer c1, c2;
        begin
            @(posedge hdmi_hs); c1 = pcyc;
            @(posedge hdmi_hs); c2 = pcyc;
            n = c2 - c1;
        end
    endtask

    task measure_de_width;
        output integer w;
        begin
            while (hdmi_de !== 1'b1) @(posedge pix_clk);
            w = 0;
            while (hdmi_de === 1'b1) begin
                w = w + 1;
                @(posedge pix_clk);
            end
        end
    endtask

    // ------------------------------------------------------------------ 帧同步
    task frame_start;
        begin
            wait (hdmi_vs === 1'b1);
            wait (hdmi_vs === 1'b0);
        end
    endtask

    task frame_end;
        begin
            wait (hdmi_vs === 1'b1);
        end
    endtask

    // ------------------------------------------------------------------ 模式切换
    task switch_and_measure;
        input [1:0]  mode;
        input integer exp_h;
        input integer exp_htotal;
        input real    exp_mhz;
        begin
            de_bad  = 0;
            guard_en = 0;
            // C-E 要从 enable=1 出发（④ 段结束时是 0），而且让 enable=0 落在**有效行内**
            // （DE 为高的最坏情况）才测得到 2FF 的真实传播拍数；落在消隐期会量到 0 拍、
            // 等于没测。所以先保证 enable=1，再等 DE 起来。
            if (!dut.ctrl_enable) axi_write(32'h00, 32'h0000_0015);
            @(posedge pix_clk);
            wait (hdmi_de === 1'b1);
            settle_armed = 1'b0;                  // 重新武装 C-E 测量
            settle_done  = 1'b1;
            de_settle    = 0;
            axi_write(32'h00, 32'h0000_0014);     // enable=0
            // C-E：enable=0 经 2FF 进像素域要几拍？把这段时间**量出来**（不是移出统计）。
            // 等最多 64 个像素时钟让 DE 落低；落不下来或 > 8 拍 ⇒ 判 FAIL。
            repeat (64) @(posedge pix_clk);
            if (!settle_done) begin
                fails = fails + 1;
                $display("  FAIL C-E mode %0d: enable=0 后 64 个像素时钟内 DE 仍为高", mode);
            end else begin
                if (de_settle > settle_max) settle_max = de_settle;
                $display("  C-E mode %0d: de_settle = %0d pix clk (门禁 <= 8)", mode, de_settle);
                if (de_settle > 8) begin
                    fails = fails + 1;
                    $display("  FAIL C-E mode %0d: de_settle=%0d > 8 拍", mode, de_settle);
                end
            end
            // 从这里开始 2 帧的 de_bad 统计（DE 已经落低 ⇒ 无盲区、无相位运气）
            de_bad   = 0;
            guard_en = 1;
            axi_write(32'h08, {30'd0, mode});
            // 等 2 个完整场（切换后至少 2 帧）
            wait (hdmi_vs === 1'b0);
            wait (hdmi_vs === 1'b1);
            wait (hdmi_vs === 1'b0);
            wait (hdmi_vs === 1'b1);
            guard_en = 0;
            axi_write(32'h00, 32'h0000_0015);     // enable=1

            measure_pix_mhz(exp_htotal, mhz);
            measure_hs_clocks(hs_n);
            measure_de_width(de_w);

            $display("  mode %0d: pix=%0.4f MHz (exp %0.4f) | hs=%0d clk (exp %0d) | de_width=%0d (exp %0d) | de_bad(disabled)=%0d",
                     mode, mhz, exp_mhz, hs_n, exp_htotal, de_w, exp_h, de_bad);
            if ((mhz < (exp_mhz * 0.995)) || (mhz > (exp_mhz * 1.005))) begin
                fails = fails + 1; $display("  FAIL mode %0d pix clock", mode);
            end
            if (hs_n != exp_htotal) begin fails = fails + 1; $display("  FAIL mode %0d hs", mode); end
            if (de_w != exp_h)      begin fails = fails + 1; $display("  FAIL mode %0d de", mode); end
            if (de_bad != 0)        begin fails = fails + 1; $display("  FAIL mode %0d de during enable=0", mode); end
        end
    endtask

    // ------------------------------------------------------------------ 主流程
    initial begin
        $display("=== tb_hdmi_top_cfg (dual pix clk, MODE_DEFAULT=0) ===");
        aresetn = 0;
        #200;
        aresetn = 1;

        wait (init_done === 1'b1);
        $display("  init_done asserted at %0d ns", $time);

        // ---------------------------------------------------------- ① 寄存器
        axi_read(32'h04, rd);
        if (!rd[0]) begin fails=fails+1; $display("  FAIL STATUS.init_done"); end
        $display("  STATUS = 0x%08h", rd);

        axi_read(32'h00, rd);
        if ((rd & 32'h1F) != 32'h15) begin fails=fails+1; $display("  FAIL CTRL reset=0x%08h", rd); end
        axi_write(32'h00, 32'h0000_0011); axi_read(32'h00, rd);
        if ((rd & 32'h1F) != 32'h11) begin fails=fails+1; $display("  FAIL CTRL 0x11 -> 0x%08h", rd); end
        axi_write(32'h00, 32'h0000_0015);

        axi_write(32'h04, 32'hFFFF_FFFF);
        axi_read (32'h04, rd);
        if ((rd & 32'hFFFF_FFF8) != 0 || rd[2]) begin
            fails=fails+1; $display("  FAIL STATUS RO: 0x%08h", rd);
        end
        axi_write(32'h24, 32'hDEAD_BEEF); axi_read(32'h24, rd);
        if (rd != 0) begin fails=fails+1; $display("  FAIL FRAME_ID_LATCHED RO"); end

        axi_read(32'h08, rd);
        if ((rd & 3) != 0) begin fails=fails+1; $display("  FAIL VIDEO_MODE reset=%0d", rd); end
        axi_write(32'h08, 32'd2); axi_read(32'h08, rd);
        if ((rd & 3) != 2) begin fails=fails+1; $display("  FAIL VIDEO_MODE write 2 -> %0d", rd); end
        axi_write(32'h08, 32'd1); axi_read(32'h08, rd);
        if ((rd & 3) != 1) begin fails=fails+1; $display("  FAIL VIDEO_MODE write 1 -> %0d", rd); end
        axi_write(32'h08, 32'd0);
        #200000;

        axi_write(32'h0C, 32'h1234_5000); axi_read(32'h0C, rd);
        if (rd != 32'h1234_5000) begin fails=fails+1; $display("  FAIL FB_ADDR_LO"); end
        axi_write(32'h10, 32'h0); axi_read(32'h10, rd);
        if (rd != 0) begin fails=fails+1; $display("  FAIL FB_ADDR_HI"); end
        cfg_pulses = 0;
        axi_write(32'h14, 32'd1920); axi_read(32'h14, rd);
        if (rd != 1920) begin fails=fails+1; $display("  FAIL FB_STRIDE"); end
        if (cfg_pulses < 1) begin fails=fails+1; $display("  FAIL cfg_upd not pulsed"); end
        axi_write(32'h18, 32'd1280); axi_read(32'h18, rd);
        if (rd != 1280) begin fails=fails+1; $display("  FAIL FB_W"); end
        axi_write(32'h1C, 32'd720); axi_read(32'h1C, rd);
        if (rd != 720) begin fails=fails+1; $display("  FAIL FB_H"); end
        fid_pulses = 0;
        axi_write(32'h20, 32'd7); axi_read(32'h20, rd);
        if (rd != 7) begin fails=fails+1; $display("  FAIL FRAME_ID"); end
        if (fid_pulses < 1) begin fails=fails+1; $display("  FAIL fid_upd not pulsed"); end
        axi_write(32'h28, 32'd0); axi_read(32'h28, rd);
        if (rd != 0) begin fails=fails+1; $display("  FAIL PIXEL_FMT"); end
        axi_read(32'h2C, rd);
        if (rd != 0) begin fails=fails+1; $display("  FAIL UNDERFLOW_CNT != 0 (P1)"); end

        // ---------------------------------------------------------- ② 越界
        axi_write(32'h40, 32'hA5A5_5A5A); axi_read(32'h40, rd);
        if (rd != 0) begin fails=fails+1; $display("  FAIL out-of-range 0x40"); end
        axi_write(32'hFFC, 32'h1234_5678); axi_read(32'hFFC, rd);
        if (rd != 0) begin fails=fails+1; $display("  FAIL out-of-range 0xFFC"); end
        $display("  out-of-range writes/reads completed");

        // ---------------------------------------------------------- 模式 0 基线
        axi_write(32'h00, 32'h0000_0015);
        measure_pix_mhz(1650, mhz);
        measure_hs_clocks(hs_n);
        measure_de_width(de_w);
        $display("  mode 0 base: pix=%0.4f MHz (exp 74.21875) | hs=%0d | de=%0d", mhz, hs_n, de_w);
        if ((mhz < 73.8) || (mhz > 74.6)) begin fails=fails+1; $display("  FAIL base pix clock"); end
        if (hs_n != 1650 || de_w != 1280) begin fails=fails+1; $display("  FAIL base timing"); end

        // ---------------------------------------------------------- ③ 彩条逐像素
        cmp_pixels = 0; cmp_errs = 0; de_errs = 0; border_errs = 0;
        frame_start;
        lx1 = dut.line_x;
        cmp_en = 1;
        frame_end;
        cmp_en = 0;
        #100000;
        lx2 = dut.line_x;
        $display("  bar compare: pixels=%0d (exp 921600) mismatches=%0d de_errs=%0d border_errs=%0d",
                 cmp_pixels, cmp_errs, de_errs, border_errs);
        $display("  moving line: line_x %0d -> %0d (exp +8)", lx1, lx2);
        if (cmp_pixels != 921600) begin fails=fails+1; $display("  FAIL pixel count"); end
        if (cmp_errs   != 0)      begin fails=fails+1; $display("  FAIL bar mismatches"); end
        if (de_errs    != 0)      begin fails=fails+1; $display("  FAIL de_errs"); end
        if (border_errs!= 0)      begin fails=fails+1; $display("  FAIL border"); end
        if (lx2 !== (lx1 + 16'd8)) begin fails=fails+1; $display("  FAIL line_x step"); end

        // ---------------------------------------------------------- ④ enable=0
        axi_write(32'h00, 32'h0000_0014);
        zero_de_errs = 0; zero_d_errs = 0;
        frame_start;
        zero_en = 1;
        frame_end;
        zero_en = 0;
        $display("  enable=0 frame: de_high=%0d d_nonzero=%0d", zero_de_errs, zero_d_errs);
        if (zero_de_errs != 0) begin fails=fails+1; $display("  FAIL de when enable=0"); end
        if (zero_d_errs  != 0) begin fails=fails+1; $display("  FAIL d when enable=0"); end

        // ---------------------------------------------------------- ⑤ 双时钟切换
        // C-E 的 de_settle 也在这 4 次切换里各测一次（mode 1/3 走 pix_clk_a，mode 2/0 走 pix_clk_b）
        switch_and_measure(2'd1, 1920, 2200, 148.4375);
        hb_check_alive("pix_a");          // C-A @148.44 MHz（mode 1 刚切好，enable=1）
        switch_and_measure(2'd3, 1920, 2200, 148.4375);   // mode 3 = 1080p60 等价（双时钟位流）
        switch_and_measure(2'd2, 1920, 2200, 74.21875);
        switch_and_measure(2'd0, 1280, 1650, 74.21875);
        hb_check_alive("pix_b");          // C-A @74.22 MHz（mode 0）

        // ---------------------------------------------------------- motion=0 对比
        axi_write(32'h00, 32'h0000_0005);   // enable=1 test=1 motion=0
        cmp_pixels = 0; cmp_errs = 0;
        frame_start;
        cmp_en = 1;
        frame_end;
        cmp_en = 0;
        $display("  motion=0 compare: pixels=%0d mismatches=%0d", cmp_pixels, cmp_errs);
        if (cmp_pixels != 921600 || cmp_errs != 0) begin
            fails=fails+1; $display("  FAIL motion=0 compare");
        end

        // ---------------------------------------------------------- STATUS 读清
        axi_write(32'h00, 32'h0000_0015);
        #100000;
        axi_read(32'h04, rd);
        $display("  STATUS after frames = 0x%08h (vsync=%b)", rd, rd[1]);
        if (!rd[1]) begin fails=fails+1; $display("  FAIL vsync not latched"); end
        axi_read(32'h04, rd);
        if (rd[1]) begin fails=fails+1; $display("  FAIL vsync not read-cleared"); end

        // ---------------------------------------------------------- soft reset
        axi_write(32'h00, 32'h0000_0017);
        axi_read (32'h00, rd);
        if (rd[1]) begin fails=fails+1; $display("  FAIL soft_reset not self-cleared"); end
        $display("  CTRL after soft_reset = 0x%08h", rd);

        // ================================================================
        // ⑦ 0x30 PIX_HB —— "像素时钟活没活"的体检寄存器（事故 2026-11-03_0018）
        //    此时：mode 0（pix_clk_b 在用）、两路像素时钟都在跑、enable=1。
        // ================================================================
        // 上一节的 soft_reset 会重启 SIL9134 的 I2C 初始化（init_done 掉 0 约 1.7 ms），
        // 先等它回来，否则下面 STATUS.init_done 的检查测的是"刚软复位"而不是"像素域停摆"。
        wait (init_done === 1'b1);
        repeat (4) @(posedge aclk);

        axi_read(32'h30, rd);
        $display("  0x30 PIX_HB = 0x%08h (pix_hb=%b mmcm_locked=%b)", rd, rd[0], rd[1]);
        if (rd[31:2] != 0) begin
            fails=fails+1; $display("  FAIL PIX_HB [31:2] 非 0: 0x%08h", rd);
        end

        // ------------------------------------------------ C-B：像素域死 != 总线死
        pstop_edges = 0;
        pix_stop = 1;                         // 两路像素时钟停止翻转
        repeat (8) @(posedge aclk);           // 2FF 同步链稳定
        axi_read(32'h30, rd);
        hb_1 = rd[0]; t_1 = $time;
        $display("  C-B pix_clk STOPPED. 0x30 = 0x%08h (pix_hb=%b mmcm_locked=%b) at %0d ns",
                 rd, rd[0], rd[1], $time);

        // ① AXI 侧 0x00 仍可读写
        axi_read(32'h00, rd); ctrl_bak = rd;
        axi_write(32'h00, 32'h0000_0011);
        axi_read(32'h00, rd);
        if ((rd & 32'h1F) != 32'h11) begin
            fails=fails+1; $display("  FAIL C-B CTRL(0x00) 读写失效: 0x%08h", rd);
        end
        axi_write(32'h00, ctrl_bak);
        $display("  C-B 0x00 RW ok: 0x%08h -> 0x%08h -> restored 0x%08h", ctrl_bak, 32'h11, ctrl_bak);

        // ② AXI 侧 0x04 仍可读、只读位写不坏
        axi_read(32'h04, rd);
        $display("  C-B STATUS = 0x%08h (init_done=%b vsync=%b)", rd, rd[0], rd[1]);
        if (!rd[0]) begin fails=fails+1; $display("  FAIL C-B STATUS.init_done 掉了"); end
        if (rd[31:3] != 0) begin
            fails=fails+1; $display("  FAIL C-B STATUS 保留位非 0: 0x%08h", rd);
        end
        axi_write(32'h04, 32'hFFFF_FFFF);
        axi_read (32'h04, rd);
        if (rd[31:2] != 0 || rd[1]) begin
            fails=fails+1; $display("  FAIL C-B STATUS 被写坏: 0x%08h", rd);
        end

        // ③ AXI 侧 0x08 仍可读写
        axi_write(32'h08, 32'h0000_0000);
        axi_read (32'h08, rd);
        if ((rd & 32'hFFFF_FFFC) != 0 || (rd & 3) != 0) begin
            fails=fails+1; $display("  FAIL C-B VIDEO_MODE(0x08) 读写失效: 0x%08h", rd);
        end

        // ④ 0x30[0] 必须冻结（3 ms 远大于最慢一路 1.766 ms 的全翻转周期）
        #3000000;
        axi_read(32'h30, rd); hb_2 = rd[0]; t_2 = $time;
        $display("  C-B 0x30[0]: %b --(%0.4f ms)--> %b | pix_clk edges while stopped = %0d",
                 hb_1, $itor(t_2-t_1)/1000000.0, hb_2, pstop_edges);
        if (pstop_edges != 0) begin
            fails=fails+1; $display("  FAIL C-B 像素时钟没真停（停摆期仍有 %0d 个沿）",
                                    pstop_edges);
        end
        if (hb_1 !== hb_2) begin
            fails=fails+1; $display("  FAIL C-B 像素时钟停摆后 0x30[0] 仍在跳");
        end

        // ⑤ 恢复像素时钟：0x30[0] 必须重新翻转（排除"卡成恒定值"冒充合格）
        pix_stop = 0;
        repeat (5*65536) @(posedge pix_clk);
        axi_read(32'h30, rd); hb_3 = rd[0];
        repeat (5*65536) @(posedge pix_clk);
        axi_read(32'h30, rd); hb_4 = rd[0];
        $display("  C-B pix_clk RESTORED. 0x30[0]: %b -> %b", hb_3, hb_4);
        if (hb_3 === hb_4) begin
            fails=fails+1; $display("  FAIL C-B 恢复像素时钟后 0x30[0] 不翻转");
        end

        // ------------------------------------------------ C-C：mmcm_locked 的 2FF 传播
        mmcm_locked = 0;
        repeat (4) @(posedge aclk);
        axi_read(32'h30, rd);
        $display("  C-C mmcm_locked=0 -> 0x30 = 0x%08h (bit1=%b)", rd, rd[1]);
        if (rd[1] !== 1'b0) begin
            fails=fails+1; $display("  FAIL C-C mmcm_locked=0 时 0x30[1] 不为 0");
        end
        if (rd[31:2] != 0) begin
            fails=fails+1; $display("  FAIL C-C 保留位非 0: 0x%08h", rd);
        end

        mmcm_locked = 1;
        lk_n = 0;
        while ((dut.lk_sync !== 1'b1) && (lk_n < 8)) begin
            @(posedge aclk); #1;          // #1 让 NBA 结算后再读
            lk_n = lk_n + 1;
        end
        $display("  C-C mmcm_locked 0->1 : lk_sync 经 %0d 个 aclk 变 1 (要求 <= 2)", lk_n);
        if (lk_n > 2) begin
            fails=fails+1; $display("  FAIL C-C 2FF 传播用了 %0d 个 aclk（应 <= 2）", lk_n);
        end
        repeat (2) @(posedge aclk);
        axi_read(32'h30, rd);
        $display("  C-C 置 1 后 0x30 = 0x%08h (bit1=%b)", rd, rd[1]);
        if (rd[1] !== 1'b1) begin
            fails=fails+1; $display("  FAIL C-C mmcm_locked=1 后 0x30[1] 不为 1");
        end

        // ------------------------------------------------ C-D：0x30 只读
        axi_write(32'h30, 32'hFFFF_FFFF);
        axi_read (32'h30, rd);
        $display("  C-D write 0xFFFFFFFF -> read 0x%08h (bit1=%b 保留位=%b)",
                 rd, rd[1], rd[31:2]);
        if (rd[31:2] != 0) begin
            fails=fails+1; $display("  FAIL C-D 保留位被写坏: 0x%08h", rd);
        end
        if (rd[1] !== 1'b1) begin
            fails=fails+1; $display("  FAIL C-D 写全 1 把 0x30[1] 覆盖了");
        end
        // [0] 是活的：写全 1 之后它仍须按原节奏翻转（不是被强制成 1）
        hb_1 = rd[0];
        repeat (5*65536) @(posedge pix_clk);
        axi_read (32'h30, rd); hb_2 = rd[0];
        $display("  C-D write 0xFFFFFFFF 后 0x30[0]: %b -> %b（仍按原节奏翻转）", hb_1, hb_2);
        if (hb_1 === hb_2) begin
            fails=fails+1; $display("  FAIL C-D 写全 1 后 0x30[0] 被冻住了");
        end
        // 再写全 0：[1] 必须仍为 1（没被写 0 覆盖）
        axi_write(32'h30, 32'h0000_0000);
        axi_read (32'h30, rd);
        $display("  C-D write 0x00000000 -> read 0x%08h (bit1=%b)", rd, rd[1]);
        if (rd[1] !== 1'b1 || rd[31:2] != 0) begin
            fails=fails+1; $display("  FAIL C-D 写全 0 破坏了 0x30: 0x%08h", rd);
        end

        // ------------------------------------------------ C-F：soft_reset 不清 hb_cnt
        // 只看"0x30[0] 还在跳"不够：计数器被清零后照样跳，只是相位跳一下。
        // 所以同时监视像素域计数器本身（每一拍必须恰好 +1）。
        hb_jumps = 0;
        hb_prev  = dut.hb_cnt - 17'd1;
        hb_mon   = 1'b1;
        axi_read(32'h30, rd); hb_1 = rd[0];
        $display("  C-F before soft_reset: 0x30[0]=%b  hb_cnt=0x%05h", hb_1, dut.hb_cnt);
        axi_write(32'h00, 32'h0000_0017);     // enable=1 + soft_reset=1
        repeat (8) @(posedge aclk);           // soft_reset 在 aclk 域生效（soft_cnt=64）
        axi_read(32'h30, rd); hb_2 = rd[0];   // 复位后"立刻"读
        $display("  C-F right after soft_reset: 0x30[0]=%b  hb_cnt=0x%05h", hb_2, dut.hb_cnt);
        repeat (5*65536) @(posedge pix_clk);
        axi_read(32'h30, rd); hb_3 = rd[0];
        $display("  C-F after 5*2^16 pix clk:  0x30[0]=%b  hb_cnt=0x%05h  (hb_cnt 非 +1 跳变 = %0d 次)",
                 hb_3, dut.hb_cnt, hb_jumps);
        hb_mon = 1'b0;
        if (hb_jumps != 0) begin
            fails=fails+1;
            $display("  FAIL C-F soft_reset 期间 hb_cnt 被改动了 %0d 次（计数器被清）", hb_jumps);
        end
        if (hb_2 === hb_3) begin
            fails=fails+1; $display("  FAIL C-F soft_reset 后 0x30[0] 不翻转（心跳假死）");
        end

        $display("--------------------------------------------------------------");
        $display("  C-E 汇总：4 次模式切换 de_settle 最大值 = %0d pix clk (门禁 <= 8)",
                 settle_max);
        $display("  sim end at %0.3f ms", $itor($time)/1000000.0);
        if (fails == 0) $display("RESULT: PASS  (all top-level criteria met, errors = 0)");
        else            $display("RESULT: FAIL  (errors = %0d)", fails);
        $display("--------------------------------------------------------------");
        $finish;
    end

    // ------------------------------------------------------------------ 超时
    // 加了 0x30 PIX_HB 判据（C-A 两组 + C-B 停/启两个窗口）后总时长约 +15 ms，
    // 因此把硬超时从 300 ms 放宽到 500 ms（只是兜底看门狗，不影响任何判据）。
    integer to_cnt = 0;
    always @(posedge aclk) begin
        to_cnt <= to_cnt + 1;
        if (to_cnt == 50000000) begin   // 500 ms
            $display("RESULT: TIMEOUT");
            $finish;
        end
    end

endmodule
