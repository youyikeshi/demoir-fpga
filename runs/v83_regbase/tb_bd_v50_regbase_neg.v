// ============================================================================
//  tb_bd_v50_regbase_neg.v —— T-83 负对照（证明上面那份 TB 真的能抓到平移）
// ----------------------------------------------------------------------------
//  与 tb_bd_v50_regbase.v 逐字相同，**唯一差别**：显式把外壳参数覆盖回旧默认
//  V50_REG_BASE = 16'h02E0（= T-82 报的 bug 状态）。
//  期望（负对照成立的条件）：
//    0x314 读 0x00000000（PS 的"链引擎在位"判定失败）
//    0x2F4 读 0x50300001（CHAIN_ID 实际落在 0x2E0 段）
//    0x300..0x310 全读 0（整块寄存器被平移到 0x2E0..0x2F4）
// ============================================================================
`timescale 1ns/1ps

module tb_bd_v50_regbase_neg;

    localparam [31:0] CFG_PAGE = 32'h0001_0000;

    integer n_pass = 0, n_fail = 0;

    reg clk  = 1'b0;
    reg rstn = 1'b0;
    always #5 clk = ~clk;

    reg  [31:0] s_awaddr = 32'd0;
    reg         s_awvalid = 1'b0;
    wire        s_awready;
    reg  [31:0] s_wdata = 32'd0;
    reg  [3:0]  s_wstrb = 4'd0;
    reg         s_wvalid = 1'b0;
    wire        s_wready;
    wire [1:0]  s_bresp;
    wire        s_bvalid;
    reg         s_bready = 1'b1;
    reg  [31:0] s_araddr = 32'd0;
    reg         s_arvalid = 1'b0;
    wire        s_arready;
    wire [31:0] s_rdata;
    wire [1:0]  s_rresp;
    wire        s_rvalid;
    reg         s_rready = 1'b1;

    wire        m_arvalid, m_rready, m_awvalid, m_wvalid, m_wlast, m_bready;
    wire [31:0] m_araddr_w, m_awaddr, m_wdata;
    wire [3:0]  m_arlen, m_arid, m_awlen, m_wid;
    wire [7:0]  m_wstrb;

    // ★ 唯一差别：把参数覆盖回旧默认 0x02E0
    bin_unit_axi_wrap_bd_v50 #(.V50_REG_BASE(16'h02E0)) u_dut (
        .s_axi_aclk(clk), .s_axi_aresetn(rstn),
        .s_axi_awaddr(s_awaddr), .s_axi_awvalid(s_awvalid), .s_axi_awready(s_awready),
        .s_axi_wdata(s_wdata), .s_axi_wstrb(s_wstrb), .s_axi_wvalid(s_wvalid),
        .s_axi_wready(s_wready), .s_axi_bresp(s_bresp), .s_axi_bvalid(s_bvalid),
        .s_axi_bready(s_bready),
        .s_axi_araddr(s_araddr), .s_axi_arvalid(s_arvalid), .s_axi_arready(s_arready),
        .s_axi_rdata(s_rdata), .s_axi_rresp(s_rresp), .s_axi_rvalid(s_rvalid),
        .s_axi_rready(s_rready),
        .m_axi_arvalid(m_arvalid), .m_axi_arready(1'b0), .m_axi_araddr(m_araddr_w),
        .m_axi_arlen(m_arlen), .m_axi_arid(m_arid),
        .m_axi_rvalid(1'b0), .m_axi_rready(m_rready), .m_axi_rdata(64'd0),
        .m_axi_rlast(1'b0), .m_axi_rid(4'd0),
        .m_axi_awvalid(m_awvalid), .m_axi_awready(1'b0), .m_axi_awaddr(m_awaddr),
        .m_axi_awlen(m_awlen), .m_axi_wid(m_wid), .m_axi_wvalid(m_wvalid),
        .m_axi_wready(1'b0), .m_axi_wdata(m_wdata), .m_axi_wstrb(m_wstrb),
        .m_axi_wlast(m_wlast), .m_axi_bvalid(1'b0), .m_axi_bready(m_bready)
    );

    task axi_rd(input [31:0] a, output [31:0] d);
        begin
            @(negedge clk);
            s_araddr = a;  s_arvalid = 1'b1;
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
                $display("  PASS  %-44s got=0x%08h", nm, got);
            end else begin
                n_fail = n_fail + 1;
                $display("  FAIL  %-44s got=0x%08h exp=0x%08h", nm, got, exp);
            end
        end
    endtask

    reg [31:0] v;

    initial begin
        $display("================================================================");
        $display(" T-83 NEGATIVE CONTROL: V50_REG_BASE forced back to 0x02E0");
        $display(" (this reproduces the T-82 bug; it is NOT the shipped default)");
        $display("================================================================");
        #1;
        $display(" PROBE shell V50_REG_BASE = 16'h%04h", u_dut.V50_REG_BASE);
        $display(" PROBE unit  RW_CHAIN     = 10'h%03h", u_dut.u_bin_unit.RW_CHAIN);
        $display(" PROBE unit  RW_MAGIC     = 10'h%03h", u_dut.u_bin_unit.RW_MAGIC);

        repeat (20) @(posedge clk);
        rstn = 1'b1;
        repeat (5)  @(posedge clk);

        $display("[shifted window 0x2E0..0x2F4]");
        axi_rd(CFG_PAGE + 10'h2E0, v); $display("   0x2E0 CHAIN_HEAD  = 0x%08h", v);
        axi_rd(CFG_PAGE + 10'h2E4, v); $display("   0x2E4 PROGRESS    = 0x%08h", v);
        axi_rd(CFG_PAGE + 10'h2E8, v); $display("   0x2E8 BAND_ROWS   = 0x%08h", v);
        axi_rd(CFG_PAGE + 10'h2EC, v); $display("   0x2EC FUSE_MODE   = 0x%08h", v);
        axi_rd(CFG_PAGE + 10'h2F0, v); $display("   0x2F0 CHAIN_STATUS= 0x%08h", v);
        axi_rd(CFG_PAGE + 10'h2F4, v);
        $display("   0x2F4 CHAIN_ID    = 0x%08h  <= bug signature", v);
        chk("NEG: 0x2F4 carries CHAIN_ID magic", v, 32'h5030_0001);

        $display("[contract window 0x300..0x314]");
        axi_rd(CFG_PAGE + 10'h300, v); chk("NEG: 0x300 reads 0 (shifted away)", v, 32'h0);
        axi_rd(CFG_PAGE + 10'h304, v); chk("NEG: 0x304 reads 0 (shifted away)", v, 32'h0);
        axi_rd(CFG_PAGE + 10'h310, v); chk("NEG: 0x310 reads 0 (shifted away)", v, 32'h0);
        axi_rd(CFG_PAGE + 10'h314, v);
        $display("   0x314 CHAIN_ID    = 0x%08h  <= PS 'engine present' check fails", v);
        chk("NEG: 0x314 reads 0 (the T-82 symptom)", v, 32'h0);

        $display("================================================================");
        $display(" RESULT: PASS=%0d FAIL=%0d", n_pass, n_fail);
        if (n_fail == 0) $display(" RESULT: PASS (negative control reproduced the bug)");
        else             $display(" RESULT: FAIL (control did not reproduce)");
        $display("================================================================");
        $finish;
    end

    initial begin
        #200_000;
        $display(" RESULT: FAIL (timeout)");
        $finish;
    end

endmodule
