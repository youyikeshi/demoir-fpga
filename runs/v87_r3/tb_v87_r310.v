// ============================================================================
//  tb_v87_r310.v —— T-87 / 契约 R3 §12.1 + §12.2 的 xsim 证据
// ----------------------------------------------------------------------------
//  判据（全部打 PRINT，FAIL 计数 > 0 就绝对不能在报告里写"过"）：
//    P1  复位后 `0x310 CHAIN_STATUS` == 0x0000（契约 R3 §12.2 的硬判据：
//        done/err/busy/code 全 0；**err 位必须是 0**）
//    P2  复位后 `0x310` 的 err 位 == 0（单独判一次，便于报告引用）
//    P3  `0x314 CHAIN_ID` == 0x5030_0001（与 PS 常量一致，§12.1）
//    P4  写 BAND_ROWS/FUSE_MODE/ARENA/CHAIN_HEAD 之后（**还没 kick**）
//        `0x310` 仍 == 0x0000（写配置不等于开跑，不许因此报错）
//    P5  kick 之后连续 64 拍内 `0x310` 的 err 位恒 0 —— 这是 §12.2 修好后
//        最关键的"不误报"判据：修之前 `cap_err` 在 kick 后 1~2 拍就粘滞，
//        `band_bad` → segq S_ERR → err=1/code=4(E_CANARY)。
//        （本 TB 的 m_axi 读永不返回 ⇒ segq 会停在 S_HDR 里数超时，
//          超时之前 err 必须一直是 0。超时码是 E_TIMEOUT 不是 E_CANARY。）
//    P6  再拉一次复位后 `0x310` 回到 0x0000（可重启性，与契约 ⑪ 一致）
//
//  只读/写 s_axi 从口；m_axi 主口钉成"永不受理"，不驱动任何 DDR。
//  日志全 ASCII（xsim 打 UTF-8 会乱码）。
// ============================================================================
`timescale 1ns/1ps

module tb_v87_r310;

    localparam [31:0] PS_BASE  = 32'h4006_0000;
    localparam [31:0] CFG_PAGE = 32'h0001_0000;   // A[16]=1 ⇒ 配置页
    localparam [31:0] MAGIC    = 32'h5030_0001;   // RTL 的 V50_CHAIN_ID

    localparam [9:0] A_CTRL   = 10'h000;          // AP_CTRL.bit0 = start（自清）
    localparam [9:0] A_CHAIN  = 10'h300;
    localparam [9:0] A_PROG   = 10'h304;
    localparam [9:0] A_BROWS  = 10'h308;
    localparam [9:0] A_FUSE   = 10'h30C;
    localparam [9:0] A_STAT   = 10'h310;
    localparam [9:0] A_MAGIC  = 10'h314;
    localparam [9:0] A_ALO    = 10'h318;
    localparam [9:0] A_AHI    = 10'h31C;

    integer n_pass = 0, n_fail = 0;

    reg clk  = 1'b0;
    reg rstn = 1'b0;
    always #5 clk = ~clk;

    reg  [31:0] s_awaddr  = 32'd0;
    reg         s_awvalid = 1'b0;
    wire        s_awready;
    reg  [31:0] s_wdata   = 32'd0;
    reg  [3:0]  s_wstrb   = 4'd0;
    reg         s_wvalid  = 1'b0;
    wire        s_wready;
    wire [1:0]  s_bresp;
    wire        s_bvalid;
    reg         s_bready  = 1'b1;

    reg  [31:0] s_araddr  = 32'd0;
    reg         s_arvalid = 1'b0;
    wire        s_arready;
    wire [31:0] s_rdata;
    wire [1:0]  s_rresp;
    wire        s_rvalid;
    reg         s_rready  = 1'b1;

    wire        m_arvalid;
    wire [31:0] m_araddr_w;
    wire [3:0]  m_arlen;
    wire [3:0]  m_arid;
    wire        m_rready;
    wire        m_awvalid;
    wire [31:0] m_awaddr;
    wire [3:0]  m_awlen;
    wire [3:0]  m_wid;
    wire        m_wvalid;
    wire [63:0] m_wdata;
    wire [7:0]  m_wstrb;
    wire        m_wlast;
    wire        m_bready;

    bin_unit_axi_wrap_bd_v50 u_dut (
        .s_axi_aclk   (clk),
        .s_axi_aresetn(rstn),
        .s_axi_awaddr (s_awaddr),
        .s_axi_awvalid(s_awvalid),
        .s_axi_awready(s_awready),
        .s_axi_wdata  (s_wdata),
        .s_axi_wstrb  (s_wstrb),
        .s_axi_wvalid (s_wvalid),
        .s_axi_wready (s_wready),
        .s_axi_bresp  (s_bresp),
        .s_axi_bvalid (s_bvalid),
        .s_axi_bready (s_bready),
        .s_axi_araddr (s_araddr),
        .s_axi_arvalid(s_arvalid),
        .s_axi_arready(s_arready),
        .s_axi_rdata  (s_rdata),
        .s_axi_rresp  (s_rresp),
        .s_axi_rvalid (s_rvalid),
        .s_axi_rready (s_rready),

        .m_axi_arvalid(m_arvalid),
        .m_axi_arready(1'b0),          // 永不接受 ⇒ segq 停在 S_HDR 数超时
        .m_axi_araddr (m_araddr_w),
        .m_axi_arlen  (m_arlen),
        .m_axi_arid   (m_arid),
        .m_axi_rvalid (1'b0),
        .m_axi_rready (m_rready),
        .m_axi_rdata  (64'd0),
        .m_axi_rlast  (1'b0),
        .m_axi_rid    (4'd0),
        .m_axi_awvalid(m_awvalid),
        .m_axi_awready(1'b0),
        .m_axi_awaddr (m_awaddr),
        .m_axi_awlen  (m_awlen),
        .m_axi_wid    (m_wid),
        .m_axi_wvalid (m_wvalid),
        .m_axi_wready (1'b0),
        .m_axi_wdata  (m_wdata),
        .m_axi_wstrb  (m_wstrb),
        .m_axi_wlast  (m_wlast),
        .m_axi_bvalid (1'b0),
        .m_axi_bready (m_bready)
    );

    // ---------------------------------------------------------------- AXI-Lite
    task axi_wr(input [31:0] a, input [31:0] d);
        begin
            @(negedge clk);
            s_awaddr  = a;      s_awvalid = 1'b1;
            s_wdata   = d;      s_wstrb   = 4'hF;   s_wvalid = 1'b1;
            fork
                begin : aw_hs
                    @(posedge clk);
                    while (!s_awready) @(posedge clk);
                end
                begin : w_hs
                    @(posedge clk);
                    while (!s_wready) @(posedge clk);
                end
            join
            @(negedge clk);
            s_awvalid = 1'b0;   s_wvalid = 1'b0;
            @(posedge clk);
            while (!s_bvalid) @(posedge clk);
            @(negedge clk);
        end
    endtask

    task axi_rd(input [31:0] a, output [31:0] d);
        begin
            @(negedge clk);
            s_araddr  = a;      s_arvalid = 1'b1;
            @(posedge clk);
            while (!s_arready) @(posedge clk);
            @(negedge clk);
            s_arvalid = 1'b0;
            @(posedge clk);
            while (!s_rvalid) @(posedge clk);
            d = s_rdata;
            @(negedge clk);
        end
    endtask

    task chk(input string nm, input [31:0] got, input [31:0] exp);
        begin
            if (got === exp) begin
                n_pass = n_pass + 1;
                $display("  PASS  %-46s got=0x%08h", nm, got);
            end else begin
                n_fail = n_fail + 1;
                $display("  FAIL  %-46s got=0x%08h exp=0x%08h", nm, got, exp);
            end
        end
    endtask

    reg [31:0] v, v2;
    integer i, err_seen;

    initial begin
        $display("== T-87 R3: 0x310 idle must not report err / 0x314 magic ==");
        rstn = 1'b0;
        repeat (20) @(posedge clk);
        @(negedge clk);
        rstn = 1'b1;
        repeat (5) @(posedge clk);

        // ---------------- P1/P2: 复位后 0x310 ----------------
        axi_rd(CFG_PAGE + A_STAT, v);
        $display("  ---- reset 0x310 CHAIN_STATUS = 0x%08h ----", v);
        chk("P1 0x310 == 0x0000 after reset", v, 32'h0000_0000);
        chk("P2 0x310 err bit == 0 after reset", (v >> 1) & 1, 32'd0);
        chk("P2 0x310 err_code == 0 after reset", (v >> 8) & 32'hFF, 32'd0);

        // ---------------- P3: magic ----------------
        axi_rd(CFG_PAGE + A_MAGIC, v);
        chk("P3 0x314 CHAIN_ID == 0x50300001", v, MAGIC);
        // 只读：写它必须无效（否则 magic 就不是"骗不过的常量"）
        axi_wr(CFG_PAGE + A_MAGIC, 32'hDEAD_BEEF);
        axi_rd(CFG_PAGE + A_MAGIC, v2);
        chk("P3 0x314 unchanged by write", v2, MAGIC);

        // ---------------- P4: 写配置但**还没 kick** ----------------
        axi_wr(CFG_PAGE + A_BROWS, 32'd8);
        axi_wr(CFG_PAGE + A_ALO,   32'h3000_0000);
        axi_wr(CFG_PAGE + A_AHI,   32'h4000_0000);
        axi_wr(CFG_PAGE + A_CHAIN, 32'h3000_1000);   // 4 KB 对齐且在 arena 内
        axi_wr(CFG_PAGE + A_FUSE,  32'd1);           // 融合模式（还没 start）
        repeat (4) @(posedge clk);
        axi_rd(CFG_PAGE + A_STAT, v);
        $display("  ---- after cfg writes, before kick: 0x310 = 0x%08h ----", v);
        chk("P4 0x310 == 0x0000 before kick", v, 32'h0000_0000);

        // ---------------- P5: kick 后 64 拍内 err 必须恒 0 ----------------
        axi_wr(CFG_PAGE + A_CTRL, 32'd1);           // start（自清位）
        err_seen = 0;
        for (i = 0; i < 64; i = i + 1) begin
            axi_rd(CFG_PAGE + A_STAT, v);
            if (((v >> 1) & 1) !== 1'b0) err_seen = err_seen + 1;
            if (i == 0 || i == 1 || i == 2 || i == 7 || i == 31 || i == 63)
                $display("  ---- kick+%0d 0x310 = 0x%08h ----", i, v);
            repeat (1) @(posedge clk);
        end
        chk("P5 err bit 0 for 64 cycles after kick", err_seen, 0);
        chk("P5 no E_CANARY(4) after kick", (v >> 8) & 32'hFF, 32'd0);
        axi_rd(CFG_PAGE + A_PROG, v);
        chk("P5 PROGRESS == 0 (chain never advanced)", v, 32'd0);

        // ---------------- P6: 复位可重启 ----------------
        @(negedge clk);
        rstn = 1'b0;
        repeat (10) @(posedge clk);
        @(negedge clk);
        rstn = 1'b1;
        repeat (5) @(posedge clk);
        axi_rd(CFG_PAGE + A_STAT, v);
        chk("P6 0x310 == 0x0000 after re-reset", v, 32'h0000_0000);

        $display("");
        if (n_fail == 0) $display("== SUMMARY: PASS (%0d checks) ==", n_pass);
        else             $display("== SUMMARY: FAIL (%0d pass / %0d fail) ==",
                                  n_pass, n_fail);
        $finish;
    end

    initial begin
        #2_000_000;
        $display("== SUMMARY: FAIL (timeout) ==");
        $finish;
    end

endmodule
