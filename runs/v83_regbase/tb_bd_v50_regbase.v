// ============================================================================
//  tb_bd_v50_regbase.v —— T-83：BD 外壳路径下的 v50 寄存器块落点验证
// ----------------------------------------------------------------------------
//  目的（**只验地址，不验逻辑**）：
//    经 BD 外壳模块 bin_unit_axi_wrap_bd_v50 的 s_axi 从口，用**默认参数**
//    （bd_v50.tcl 不写 CONFIG.V50_REG_BASE ⇒ 综合期走的就是默认值）发 6 笔
//    读事务，确认 0x300/0x304/0x308/0x30C/0x310/0x314 逐项落对；
//    并确认 0x250..0x25C 仍为空、旧的 0x2E0..0x2F4 窗口现在为空。
//
//  地址语义（来自 bin_unit_axi_wrap_v50 的译码，不是本 TB 的假设）：
//    PS 绝对地址 = IP 基址(0x4006_0000) + 配置页(+0x1_0000, A[16]=1) + 偏移
//    单元译码只用 A[16]（选页）与 A[9:0]（偏移），基址低 16 位为 0
//    ⇒ 无论 interconnect 传"全地址"还是"窗口内偏移"，A[9:0] 都是同一组值。
//    本 TB 对 6 个寄存器各发**两种**地址写法（全地址 / 纯偏移），两者必须同值。
//
//  日志全部用 ASCII：xsim 打印 UTF-8 会乱码，注释中文不进日志。
// ============================================================================
`timescale 1ns/1ps

module tb_bd_v50_regbase;

    // ---- 契约地址（docs/V50_ARCHITECTURE.md §10.1）----
    localparam [31:0] PS_BASE  = 32'h4006_0000;   // bin_unit_0 的 BD 基址（128K）
    localparam [31:0] CFG_PAGE = 32'h0001_0000;   // A[16]=1 配置页
    localparam [31:0] MAGIC    = 32'h5030_0001;   // CHAIN_ID

    localparam [9:0] A_CHAIN  = 10'h300;
    localparam [9:0] A_PROG   = 10'h304;
    localparam [9:0] A_BROWS  = 10'h308;
    localparam [9:0] A_FUSE   = 10'h30C;
    localparam [9:0] A_STAT   = 10'h310;
    localparam [9:0] A_MAGIC  = 10'h314;

    integer n_pass = 0, n_fail = 0;

    reg clk  = 1'b0;
    reg rstn = 1'b0;
    always #5 clk = ~clk;          // 100 MHz，只做功能，不追时序

    // ---- AXI4-Lite 从口 ----
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

    // ---- AXI4 主口：本 TB 不启动链，全部钉成"永不接受/永不来数" ----
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

    // ========================================================================
    //  DUT：**不加任何参数覆盖** —— 用的就是 BD 综合期的默认值
    // ========================================================================
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
        .m_axi_arready(1'b0),
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

    // ========================================================================
    //  AXI4-Lite 单拍事务
    // ========================================================================
    task axi_wr(input [31:0] a, input [31:0] d);
        begin
            @(negedge clk);
            s_awaddr  = a;      s_awvalid = 1'b1;
            s_wdata   = d;      s_wstrb   = 4'hF;   s_wvalid = 1'b1;
            // 两个写通道各自握手（可能不同拍）
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

    // ========================================================================
    //  判定助手
    // ========================================================================
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

    // 只记录、不判定的观察值（用于本任务范围之外的既有行为）
    task note(input string nm, input [31:0] got);
        begin
            $display("  NOTE  %-44s got=0x%08h  (record only)", nm, got);
        end
    endtask

    // ========================================================================
    //  六笔读：全地址 vs 纯偏移，两者必须同值
    // ========================================================================
    reg [31:0] v_full, v_off, v_b;

    task rd6(input string tag);
        begin
            $display(" ---- 6 reads, full-addr vs offset form (%0s) ----", tag);
            axi_rd(PS_BASE + CFG_PAGE + A_CHAIN, v_full);
            axi_rd(CFG_PAGE + A_CHAIN,           v_off);
            chk("0x300 CHAIN_HEAD   full==off", v_off, v_full);
            axi_rd(PS_BASE + CFG_PAGE + A_PROG,  v_full);
            axi_rd(CFG_PAGE + A_PROG,            v_off);
            chk("0x304 PROGRESS     full==off", v_off, v_full);
            axi_rd(PS_BASE + CFG_PAGE + A_BROWS, v_full);
            axi_rd(CFG_PAGE + A_BROWS,           v_off);
            chk("0x308 BAND_ROWS    full==off", v_off, v_full);
            axi_rd(PS_BASE + CFG_PAGE + A_FUSE,  v_full);
            axi_rd(CFG_PAGE + A_FUSE,            v_off);
            chk("0x30C FUSE_MODE    full==off", v_off, v_full);
            axi_rd(PS_BASE + CFG_PAGE + A_STAT,  v_full);
            axi_rd(CFG_PAGE + A_STAT,            v_off);
            chk("0x310 CHAIN_STATUS full==off", v_off, v_full);
            axi_rd(PS_BASE + CFG_PAGE + A_MAGIC, v_full);
            axi_rd(CFG_PAGE + A_MAGIC,           v_off);
            chk("0x314 CHAIN_ID     full==off", v_off, v_full);
            $display("   0x314 readback = 0x%08h (expect 0x%08h)", v_full, MAGIC);
        end
    endtask

    // ========================================================================
    //  主流程
    // ========================================================================
    initial begin
        $display("================================================================");
        $display(" T-83  v50 register landing through BD shell (default params)");
        $display(" DUT = bin_unit_axi_wrap_bd_v50   (no CONFIG override)");
        $display(" PS addr = 0x4006_0000 + 0x1_0000 + off,  off = 0x300..0x314");
        $display("================================================================");
        #1;
        $display(" PROBE shell  u_dut.V50_REG_BASE      = 16'h%04h", u_dut.V50_REG_BASE);
        $display(" PROBE unit   u_bin_unit.V50_REG_BASE = 16'h%04h", u_dut.u_bin_unit.V50_REG_BASE);
        $display(" PROBE unit   RW_CHAIN / RW_PROG      = 10'h%03h / 10'h%03h",
                 u_dut.u_bin_unit.RW_CHAIN, u_dut.u_bin_unit.RW_PROG);
        $display(" PROBE unit   RW_BROWS / RW_FUSE      = 10'h%03h / 10'h%03h",
                 u_dut.u_bin_unit.RW_BROWS, u_dut.u_bin_unit.RW_FUSE);
        $display(" PROBE unit   RW_STAT  / RW_MAGIC     = 10'h%03h / 10'h%03h",
                 u_dut.u_bin_unit.RW_STAT,  u_dut.u_bin_unit.RW_MAGIC);

        repeat (20) @(posedge clk);
        rstn = 1'b1;
        repeat (5)  @(posedge clk);

        // ---------------------------------------------------------------
        // [1] 复位值
        // ---------------------------------------------------------------
        $display("[1] reset values");
        axi_rd(CFG_PAGE + A_CHAIN, v_b);  chk("0x300 CHAIN_HEAD    reset", v_b, 32'h0);
        axi_rd(CFG_PAGE + A_PROG,  v_b);  chk("0x304 PROGRESS      reset", v_b, 32'h0);
        axi_rd(CFG_PAGE + A_BROWS, v_b);  chk("0x308 BAND_ROWS     reset (=param 8)", v_b, 32'd8);
        axi_rd(CFG_PAGE + A_FUSE,  v_b);  chk("0x30C FUSE_MODE     reset", v_b, 32'h0);
        axi_rd(CFG_PAGE + A_STAT,  v_b);  note("0x310 CHAIN_STATUS  reset (see report)", v_b);
        axi_rd(CFG_PAGE + A_MAGIC, v_b);  chk("0x314 CHAIN_ID      reset = magic", v_b, MAGIC);

        rd6("after reset");

        // ---------------------------------------------------------------
        // [2] 可写寄存器
        // ---------------------------------------------------------------
        $display("[2] RW registers");
        axi_wr(CFG_PAGE + A_CHAIN, 32'hDEAD_BEEF);
        axi_rd(CFG_PAGE + A_CHAIN, v_b);  chk("0x300 CHAIN_HEAD    write->read", v_b, 32'hDEAD_BEEF);
        axi_wr(CFG_PAGE + A_BROWS, 32'h0000_0005);
        axi_rd(CFG_PAGE + A_BROWS, v_b);  chk("0x308 BAND_ROWS     write->read", v_b, 32'd5);
        axi_wr(CFG_PAGE + A_FUSE,  32'h0000_0001);
        axi_rd(CFG_PAGE + A_FUSE,  v_b);  chk("0x30C FUSE_MODE     write->read", v_b, 32'h1);

        // ---------------------------------------------------------------
        // [3] 只读寄存器：写必须无效（值不变）
        // ---------------------------------------------------------------
        $display("[3] RO registers (write ignored)");
        axi_rd(CFG_PAGE + A_PROG, v_b);
        axi_wr(CFG_PAGE + A_PROG, 32'hFFFF_FFFF);
        axi_rd(CFG_PAGE + A_PROG, v_full);
        chk("0x304 PROGRESS      unchanged by write", v_full, v_b);

        axi_rd(CFG_PAGE + A_STAT, v_b);
        axi_wr(CFG_PAGE + A_STAT, 32'hFFFF_FFFF);
        axi_rd(CFG_PAGE + A_STAT, v_full);
        chk("0x310 CHAIN_STATUS  unchanged by write", v_full, v_b);

        axi_rd(CFG_PAGE + A_MAGIC, v_b);
        chk("0x314 CHAIN_ID      pre-write = magic", v_b, MAGIC);
        axi_wr(CFG_PAGE + A_MAGIC, 32'h0000_0000);
        axi_rd(CFG_PAGE + A_MAGIC, v_b);
        chk("0x314 still magic after write 0", v_b, MAGIC);
        axi_wr(CFG_PAGE + A_MAGIC, 32'hFFFF_FFFF);
        axi_rd(CFG_PAGE + A_MAGIC, v_b);
        chk("0x314 still magic after write F", v_b, MAGIC);

        // ---------------------------------------------------------------
        // [4] 0x250..0x25C 必须仍为空（v42d 禁区）
        // ---------------------------------------------------------------
        $display("[4] 0x250..0x25C must stay empty");
        for (int off = 8'h50; off <= 8'h5C; off = off + 4) begin
            axi_rd(CFG_PAGE + {22'd0, off[7:0]}, v_b);
            chk($sformatf("0x2%02h read must be 0 (forbidden)", off[7:0]), v_b, 32'h0);
        end
        axi_wr(CFG_PAGE + 10'h250, 32'hA5A5_5A5A);
        axi_wr(CFG_PAGE + 10'h254, 32'hFFFF_FFFF);
        axi_wr(CFG_PAGE + 10'h258, 32'hFFFF_FFFF);
        axi_wr(CFG_PAGE + 10'h25C, 32'hFFFF_FFFF);
        for (int off = 8'h50; off <= 8'h5C; off = off + 4) begin
            axi_rd(CFG_PAGE + {22'd0, off[7:0]}, v_b);
            chk($sformatf("0x2%02h still 0 after write", off[7:0]), v_b, 32'h0);
        end
        axi_rd(CFG_PAGE + A_CHAIN, v_b);
        chk("0x300 undisturbed by forbidden write", v_b, 32'hDEAD_BEEF);

        // ---------------------------------------------------------------
        // [5] 旧 0x2E0..0x2F4 窗口必须为空（T-82 的平移已消失）
        // ---------------------------------------------------------------
        $display("[5] old 0x2E0..0x2F4 window must be empty");
        axi_rd(CFG_PAGE + 10'h2E0, v_b);  chk("0x2E0 read must be 0 (old base)", v_b, 32'h0);
        axi_rd(CFG_PAGE + 10'h2E4, v_b);  chk("0x2E4 read must be 0 (old base)", v_b, 32'h0);
        axi_rd(CFG_PAGE + 10'h2E8, v_b);  chk("0x2E8 read must be 0 (old base)", v_b, 32'h0);
        axi_rd(CFG_PAGE + 10'h2EC, v_b);  chk("0x2EC read must be 0 (old base)", v_b, 32'h0);
        axi_rd(CFG_PAGE + 10'h2F0, v_b);  chk("0x2F0 read must be 0 (old base)", v_b, 32'h0);
        axi_rd(CFG_PAGE + 10'h2F4, v_b);  chk("0x2F4 must be 0 (old CHAIN_ID slot)", v_b, 32'h0);

        // ---------------------------------------------------------------
        // [6] 最终读数表（贴报告）
        // ---------------------------------------------------------------
        $display("[6] final readback table");
        axi_rd(PS_BASE + CFG_PAGE + A_CHAIN, v_b); $display("   0x4007_0300 (0x300 CHAIN_HEAD  ) = 0x%08h", v_b);
        axi_rd(PS_BASE + CFG_PAGE + A_PROG,  v_b); $display("   0x4007_0304 (0x304 PROGRESS    ) = 0x%08h", v_b);
        axi_rd(PS_BASE + CFG_PAGE + A_BROWS, v_b); $display("   0x4007_0308 (0x308 BAND_ROWS   ) = 0x%08h", v_b);
        axi_rd(PS_BASE + CFG_PAGE + A_FUSE,  v_b); $display("   0x4007_030C (0x30C FUSE_MODE   ) = 0x%08h", v_b);
        axi_rd(PS_BASE + CFG_PAGE + A_STAT,  v_b); $display("   0x4007_0310 (0x310 CHAIN_STATUS) = 0x%08h", v_b);
        axi_rd(PS_BASE + CFG_PAGE + A_MAGIC, v_b); $display("   0x4007_0314 (0x314 CHAIN_ID    ) = 0x%08h  <= must be 0x50300001", v_b);
        axi_rd(PS_BASE + CFG_PAGE + 10'h2E0, v_b); $display("   0x4007_02E0 (0x2E0 old landing ) = 0x%08h  <= must be 0", v_b);

        $display("================================================================");
        $display(" RESULT: PASS=%0d FAIL=%0d", n_pass, n_fail);
        if (n_fail == 0) $display(" RESULT: PASS");
        else             $display(" RESULT: FAIL");
        $display("================================================================");
        $finish;
    end

    // 超时保护：不启动链，正常 < 5 us
    initial begin
        #200_000;
        $display(" RESULT: FAIL (timeout)");
        $finish;
    end

endmodule
