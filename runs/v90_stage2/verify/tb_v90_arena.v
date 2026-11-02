// ============================================================================
//  tb_v90_arena.v —— T-90 独立验收：ARENA 默认值 ⇒ **拒绝启动**（u_err=E_RANGE）
// ----------------------------------------------------------------------------
//  被测：bin_unit_axi_wrap_bd_v50（VLANES=8），RTL = runs/v90_stage2/verify/snapB
//  判据：
//    A1  复位后 0x310 == 0
//    A2  配置齐全（vcode=0）但**不写 0x318/0x31C** ⇒ start 后 900 拍内
//        m_axi_arvalid **恒 0**（kick 被门控），AP_RETURN[15:8] == 2 (E_RANGE)
//    B   复位后写 arena=0x3000_0000..0x4000_0000 + 同一套配置 ⇒
//        m_axi_arvalid 拉起，AP_RETURN[15:8] == 0（证明同一路径 kick 没被误杀）
//  m_axi 永不 ready ⇒ 只验证"有没有发起"，不模拟 DDR。
// ============================================================================
`timescale 1ns/1ps

module tb_v90_arena;
    localparam [31:0] CFG_PAGE = 32'h0001_0000;
    localparam [9:0]  A_CTRL  = 10'h000;
    localparam [9:0]  A_RET   = 10'h010;
    localparam [9:0]  A_C     = 10'h018;
    localparam [9:0]  A_H     = 10'h020;
    localparam [9:0]  A_W     = 10'h028;
    localparam [9:0]  A_K     = 10'h040;
    localparam [9:0]  A_S     = 10'h048;
    localparam [9:0]  A_D     = 10'h050;
    localparam [9:0]  A_PAD   = 10'h058;
    localparam [9:0]  A_SH    = 10'h060;
    localparam [9:0]  A_ASH   = 10'h068;
    localparam [9:0]  A_SSH   = 10'h06C;
    localparam [9:0]  A_COUT  = 10'h070;
    localparam [9:0]  A_IN    = 10'h080;
    localparam [9:0]  A_WGT   = 10'h0A0;
    localparam [9:0]  A_BIAS  = 10'h0B0;
    localparam [9:0]  A_ZETA  = 10'h0B8;
    localparam [9:0]  A_GAM   = 10'h0BC;
    localparam [9:0]  A_SLP   = 10'h0C0;
    localparam [9:0]  A_OUT   = 10'h0D0;
    localparam [9:0]  A_ALP   = 10'h0D8;
    localparam [9:0]  A_CHAIN = 10'h300;
    localparam [9:0]  A_BROWS = 10'h308;
    localparam [9:0]  A_FUSE  = 10'h30C;
    localparam [9:0]  A_STAT  = 10'h310;
    localparam [9:0]  A_ALO   = 10'h318;
    localparam [9:0]  A_AHI   = 10'h31C;

    integer n_pass = 0, n_fail = 0;
    reg clk = 1'b0;
    reg rstn = 1'b0;
    always #5 clk = ~clk;

    reg  [31:0] s_awaddr=0; reg s_awvalid=0; wire s_awready;
    reg  [31:0] s_wdata=0;  reg [3:0] s_wstrb=0; reg s_wvalid=0; wire s_wready;
    wire [1:0] s_bresp; wire s_bvalid; reg s_bready=1'b1;
    reg  [31:0] s_araddr=0; reg s_arvalid=0; wire s_arready;
    wire [31:0] s_rdata; wire [1:0] s_rresp; wire s_rvalid; reg s_rready=1'b1;

    wire m_arvalid; wire [31:0] m_araddr_w; wire [3:0] m_arlen, m_arid;
    wire m_rready, m_awvalid, m_wvalid, m_wlast, m_bready;
    wire [31:0] m_awaddr; wire [3:0] m_awlen, m_wid;
    wire [63:0] m_wdata; wire [7:0] m_wstrb;

    bin_unit_axi_wrap_bd_v50 #(.VLANES(8)) u_dut (
        .s_axi_aclk(clk), .s_axi_aresetn(rstn),
        .s_axi_awaddr(s_awaddr), .s_axi_awvalid(s_awvalid), .s_axi_awready(s_awready),
        .s_axi_wdata(s_wdata), .s_axi_wstrb(s_wstrb), .s_axi_wvalid(s_wvalid),
        .s_axi_wready(s_wready), .s_axi_bresp(s_bresp), .s_axi_bvalid(s_bvalid),
        .s_axi_bready(s_bready), .s_axi_araddr(s_araddr), .s_axi_arvalid(s_arvalid),
        .s_axi_arready(s_arready), .s_axi_rdata(s_rdata), .s_axi_rresp(s_rresp),
        .s_axi_rvalid(s_rvalid), .s_axi_rready(s_rready),
        .m_axi_arvalid(m_arvalid), .m_axi_arready(1'b0),
        .m_axi_araddr(m_araddr_w), .m_axi_arlen(m_arlen), .m_axi_arid(m_arid),
        .m_axi_rvalid(1'b0), .m_axi_rready(m_rready), .m_axi_rdata(64'd0),
        .m_axi_rlast(1'b0), .m_axi_rid(4'd0),
        .m_axi_awvalid(m_awvalid), .m_axi_awready(1'b0), .m_axi_awaddr(m_awaddr),
        .m_axi_awlen(m_awlen), .m_axi_wid(m_wid), .m_axi_wvalid(m_wvalid),
        .m_axi_wready(1'b0), .m_axi_wdata(m_wdata), .m_axi_wstrb(m_wstrb),
        .m_axi_wlast(m_wlast), .m_axi_bvalid(1'b0), .m_axi_bready(m_bready)
    );

    task axi_wr(input [31:0] a, input [31:0] d);
        begin
            @(negedge clk);
            s_awaddr=a; s_awvalid=1'b1; s_wdata=d; s_wstrb=4'hF; s_wvalid=1'b1;
            fork
                begin @(posedge clk); while (!s_awready) @(posedge clk); end
                begin @(posedge clk); while (!s_wready)  @(posedge clk); end
            join
            @(negedge clk); s_awvalid=1'b0; s_wvalid=1'b0;
            @(posedge clk); while (!s_bvalid) @(posedge clk);
            @(negedge clk);
        end
    endtask

    task axi_rd(input [31:0] a, output [31:0] d);
        begin
            @(negedge clk); s_araddr=a; s_arvalid=1'b1;
            @(posedge clk); while (!s_arready) @(posedge clk);
            @(negedge clk); s_arvalid=1'b0;
            @(posedge clk); while (!s_rvalid) @(posedge clk);
            d = s_rdata;
            @(negedge clk);
        end
    endtask

    task chk(input string nm, input [31:0] got, input [31:0] exp);
        begin
            if (got === exp) begin n_pass=n_pass+1; $display("  PASS  %-54s got=0x%08h", nm, got); end
            else begin n_fail=n_fail+1; $display("  FAIL  %-54s got=0x%08h exp=0x%08h", nm, got, exp); end
        end
    endtask

    // 一套能过 vcode 的 conv 配置（FUSE 路径仍要 v39 的配置页合法）
    task cfg_valid();
        begin
            axi_wr(CFG_PAGE + A_C,    32'd48);
            axi_wr(CFG_PAGE + A_H,    32'd32);
            axi_wr(CFG_PAGE + A_W,    32'd32);
            axi_wr(CFG_PAGE + A_K,    32'd1);
            axi_wr(CFG_PAGE + A_S,    32'd1);
            axi_wr(CFG_PAGE + A_D,    32'd1);
            axi_wr(CFG_PAGE + A_PAD,  32'd0);
            axi_wr(CFG_PAGE + A_SH,   32'd0);
            axi_wr(CFG_PAGE + A_ASH,  32'd9);
            axi_wr(CFG_PAGE + A_SSH,  32'd8);
            axi_wr(CFG_PAGE + A_COUT, 32'd32);
            axi_wr(CFG_PAGE + A_IN,   32'h3000_2000);
            axi_wr(CFG_PAGE + A_WGT,  32'h3000_3000);
            axi_wr(CFG_PAGE + A_ALP,  32'h3000_4000);
            axi_wr(CFG_PAGE + A_BIAS, 32'h3000_5000);
            axi_wr(CFG_PAGE + A_ZETA, 32'h3000_6000);
            axi_wr(CFG_PAGE + A_GAM,  32'h3000_7000);
            axi_wr(CFG_PAGE + A_SLP,  32'h3000_8000);
            axi_wr(CFG_PAGE + A_OUT,  32'h3000_9000);
            axi_wr(CFG_PAGE + A_BROWS,32'd8);
            axi_wr(CFG_PAGE + A_FUSE, 32'd1);
        end
    endtask

    task do_reset();
        begin
            @(negedge clk); rstn = 1'b0;
            repeat (12) @(posedge clk);
            @(negedge clk); rstn = 1'b1;
            repeat (5) @(posedge clk);
        end
    endtask

    reg [31:0] v;
    integer i; reg ar_seen;

    initial begin
        $display("== T-90 V2b: ARENA default => refuse start (E_RANGE) / set => kick ==");
        do_reset();
        axi_rd(CFG_PAGE + A_STAT, v);
        chk("A1 0x310 CHAIN_STATUS == 0 after reset", v, 32'h0);

        // ---------------- A：不写 arena ----------------
        cfg_valid();
        axi_wr(CFG_PAGE + A_CHAIN, 32'h0000_2000);
        ar_seen = 1'b0;
        fork
            begin @(negedge clk); axi_wr(CFG_PAGE + A_CTRL, 32'd1); end
            begin : watch_ar
                for (i = 0; i < 1200; i++) begin
                    @(posedge clk);
                    if (m_arvalid) ar_seen = 1'b1;
                end
            end
        join
        chk("A2 A2 arena-default: m_axi_arvalid stays 0 (kick gated)", {31'd0, ar_seen}, 32'd0);
        axi_rd(CFG_PAGE + A_RET, v);
        $display("  ---- A AP_RETURN = 0x%08h ----", v);
        chk("A3 AP_RETURN[15:8] == E_RANGE(2)  [RTL claim]", (v >> 8) & 32'hFF, 32'd2);

        // ---------------- B：复位 + 写 arena ----------------
        do_reset();
        cfg_valid();
        axi_wr(CFG_PAGE + A_ALO, 32'h3000_0000);
        axi_wr(CFG_PAGE + A_AHI, 32'h4000_0000);
        axi_wr(CFG_PAGE + A_CHAIN, 32'h3000_1000);
        axi_wr(CFG_PAGE + A_CTRL, 32'd1);
        ar_seen = 1'b0;
        for (i = 0; i < 1200; i++) begin
            @(posedge clk);
            if (m_arvalid) ar_seen = 1'b1;
        end
        chk("B1 B1 arena written: m_axi_arvalid rises", {31'd0, ar_seen}, 32'd1);
        $display("  ---- B last ARADDR = 0x%08h ----", m_araddr_w);
        chk("B2 ARADDR inside arena ([31:28]==0x3)", m_araddr_w[31:28], 32'h3);
        axi_rd(CFG_PAGE + A_RET, v);
        $display("  ---- B AP_RETURN = 0x%08h ----", v);
        chk("B3 AP_RETURN[15:8] == 0", (v >> 8) & 32'hFF, 32'd0);

        $display("\n===== V2b result: %0d pass / %0d fail =====", n_pass, n_fail);
        if (n_fail == 0) $display("V2b: A2/B1/B2/B3 pass; A3 is the finding");
        else             $display("V2b FAIL");
        $finish;
    end

    initial begin #4000000; $display("TIMEOUT"); $finish; end
endmodule
