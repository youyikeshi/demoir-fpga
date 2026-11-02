// ============================================================================
//  tb_bin_unit_axi_wrap.v —— bin_unit_axi_wrap（AXI 包装层）的验收 TB
//
//  历史：本文件 2026-10-28 写的端口是旧的 `m_axi_rd_*` / `m_axi_wr_*`，
//        而 DUT 在 2026-10-29 换成了 AXI3 风格的 `m_axi_ar* / m_axi_r* /
//        m_axi_aw* / m_axi_w* / m_axi_b*`（arlen/awlen 4 bit、新增 wid）。
//        ⇒ 这个 tb **从来没能 xelab 过**，wrapper 的正确性只靠上板试。
//        2026-10-31 重写：端口逐条对齐 DUT，并补了 AXI3 从模型（含 burst
//        与随机延迟）—— 读侧重写（arlen=7）落地时它就是现成的安全网。
//
//  DUT 端口核对表（modules/bin_unit_axi_wrap.v:230-320）：
//    AXI4-Lite 从 : s_axi_aclk / s_axi_aresetn / s_axi_aw* / s_axi_w* /
//                   s_axi_b* / s_axi_ar* / s_axi_r*
//    读主口(AXI3) : m_axi_arvalid/arready/araddr/arlen[3:0]/arid[AXI_ID_W-1:0]
//                   m_axi_rvalid/rready/rdata[63:0]/rlast/rid[AXI_ID_W-1:0]
//    写主口(AXI3) : m_axi_awvalid/awready/awaddr/awlen[3:0]
//                   m_axi_wid[AXI_ID_W-1:0]
//                   m_axi_wvalid/wready/wdata[63:0]/wstrb[7:0]/wlast
//                   m_axi_bvalid/bready
//    ★ 没有 ap_clk/ap_rst_n/ap_start/ap_done：这个 IP 是**纯寄存器控制**
//      （配置页 A[16]=1，start 写 AP_CTRL.bit0，done 读 AP_RETURN.bit0）。
//      ⇒ tb 只需要 AXI4-Lite 主机 + 两个 AXI3 从机，不需要 ap_ctrl 握手。
//    ★ 没有 XPM / BUFGCE / 任何 Xilinx 原语（门控是同步 CE，ap_ce 进单元），
//      所以 xelab 不需要 -L unisims_ver / glbl；`-L xpm` 加着也无害。
//
//  覆盖：
//    T0  **AXI3 从模型自检**（不经 DUT）：直接驱动从机模型，验证
//        arlen=0/1/3/7/15 的 burst 逐拍数据、rid、rlast 位置，
//        以及"同 ID 多笔必须按 AR 顺序返回"。读侧重写要用 arlen=7，
//        这条自检保证"模型本身是可信的"，将来出问题能分清是谁的错。
//    T1  AXI4-Lite 寄存器读写回环（64 位基址高/低字、只读 done 位、AP_CTRL 自清）
//    T2  契约 §7.3-②：C%8 != 0 ⇒ AP_RETURN[15:8]=4(ERR_LAYOUT)，且引擎没启动
//    T3  契约 §7.3-①：需要 64-bit 读的基址非 8 字节对齐 ⇒ 5(ERR_ALIGN)
//    T4  基址为 0 ⇒ 1(ERR_NULL)
//    T5  功能跑：装黄金向量 → 写寄存器 → start → 等 done → **逐字节**比对
//        expected_c8.bin（覆盖读侧 1 拍延迟 / 多笔在飞 / 乱序 RID 落槽，
//        写侧字节合并 / burst 攒包 / 尾部排空）
//    T6  不变量：读侧最大在飞 ≥ 2、AXI 协议错误 0、**逐拍读数据对拍** 0 错、
//        DBG_FLAG 的 misalign/rid_bad 位为 0、写侧"DMA 一致性"统计
//
//  随机性（这是抓"靠时序巧合才对"的 bug 的关键）：
//    · arready 随机拉低 0..3 拍（+ABP=0 关掉）
//    · 读数据返回延迟 1..20 拍（+RLMIN= +RLMAX= 可调），同 ID 保序、
//      跨 ID 乱序返回（+ROOO=1，默认 1）
//    · awready / wready / bvalid 也各有随机反压（+WBP=0 关掉）
//    · 伪随机用本文件内的 LCG（读/写各一条），+SEED= 可复现；不用 $urandom，
//      避免不同仿真器/版本给出不同序列（回归要可复现）。
//
//  编译/运行（每个 UNIT 各编一次；RD_WIN_WORDS 等参数用 DUT 默认值）：
//      xvlog --sv -L xpm -d TB_UNIT=0 \
//            modules/bin_conv_core.v modules/bin_conv_core8.v \
//            modules/bin_rprelu.v modules/bin_sam.v \
//            modules/bin_interp.v modules/bin_unit_axi_wrap.v \
//            tb/tb_bin_unit_axi_wrap.v
//      ★ 2026-10-31 00:29 起 bin_conv_core.v 只是**薄壳**（例化 bin_conv_core8），
//        少编 bin_conv_core8.v 会 xelab 报 `Module <bin_conv_core8> not found`；
//        若在"壳已换、包装层未换"的中间窗口构建，会得到**能跑但全错**的结果
//        （8-bit 写数据接进 64-bit 端口）—— 这就是一次"bytes=14/4096"的来源。
//      xelab -debug typical --relax -L xpm tb_bin_unit_axi_wrap -s snap
//      xsim snap -R -testplusarg "GOLDEN=golden/bin_1x64_16x16" \
//                    -testplusarg "SHAPE_NAME=bin_1x64_16x16"
//  一键（四个单元 12 个形状）：  ./tools/verify_all.sh wrap
//      -d TB_UNIT=0/1/2/3 选内嵌单元（0=conv_core 1=rprelu 2=sam 3=interp）
//
//  ★ 读侧重写（arlen=7 突发）落地后**不用改本文件**：
//    从机模型按收到的 arlen 回 arlen+1 拍 INCR（ARADDR+8*k），RLAST 只在末拍，
//    RID 原样回送；TB 不写死"必须单拍"。跑完看这两个数就知道读侧换没换：
//        统计: ... 最大arlen=0   → 还是单拍
//        统计: ... 最大arlen=7   → 已经是 8 拍突发
//    失败的判据优先级：T0（模型自检）→ T6(rd_bad) → 逐字节 → T6(协议错误)。
//
//  plusarg：
//    +GOLDEN=<dir>      黄金向量目录（必需）
//    +SHAPE_NAME=<n>    形状名（必需；见 set_shape 的表）
//    +CIN= +COUT= +K= +S= +D= +PAD= +H= +W= +SHIFT= +HO= +WO=
//                       可选覆盖（verify_all 会从 golden/shape.env 传，
//                       防止表与 golden 漂移）
//    +RLMIN= +RLMAX=    读延迟区间（默认 1..20）
//    +RBURST_EXTRA=n    arlen>0 的 burst 额外延迟（默认 0；校准建议 7 = 8 拍串行）
//    +MAXOUTSTANDING=n  读通道在飞笔数上限（默认 16=不限；模拟 DDR 并发能力）
//    +ABP= +WBP=        随机反压开关（默认 1）
//    +ROOO=             跨 ID 乱序返回（默认 1 = 优先回最新的）
//    +SEED=             LCG 种子（默认 32'h1234_5678）
//    +SELFTEST=         是否跑 T0（默认 1）
//    +MAXCYC=           done 轮询上限（默认 20_000_000 拍）
//    +WBM=              期望的 WR_BURST_MAX（默认 8，只用于 AWLEN 上限检查）
//    +NOSTOP=1          失败时也跑完（默认失败即 $fatal 结束）
//    +DUMPFILE=<f>      把 DUT 写出的 C8 输出区逐字节落盘（每行 1 个 %02x 字节），
//                       供"改前 vs 改后"落盘对拷（T-38 加；默认不开，/dev/null 语义）
//
//  ⚠ 本 TB 只判"功能/协议"，不判性能；性能看 DBG_RD/DBG_WR（本 TB 会打印）。
// ============================================================================
`timescale 1ns / 1ps

`ifndef TB_UNIT
 `define TB_UNIT 0
`endif
// ★ v94：被测 wrapper 的 VLANES（8 = v39 等价控制组；16 = v50 被测）
`ifndef V94_VLANES
 `define V94_VLANES 16
`endif

module tb_bin_unit_axi_wrap;

    localparam int UNIT_ = `TB_UNIT;
    localparam int AXIW  = 4;            // = DUT.AXI_ID_W

    // =========================================================================
    // 内存模型（4 MiB 字节数组，与各单元 TB 的 mem 语义完全一致）
    //   ★ 2026-10-31：1 MiB → 4 MiB。64x64 的 golden 输出是 128~256 KB，
    //     原来的 B_OUT=0xD0000 / B_EXP=0xF0000 会让两者**重叠**、并且 expected
    //     后半段越出 1 MiB ⇒ 读出来是 X（现象：bytes=65536/131072、exp=x）。
    //     现在 B_OUT/B_EXP 都上移，留 1 MB 给输出。
    // =========================================================================
    localparam int MAXM = 1 << 22;
    logic [7:0] mem [0:MAXM-1];

    // 各区域基址（都非 0 且 8 字节对齐 —— 0 是 DUT 的"非法基址"哨兵）
    localparam int B_IN    = 32'h0001_0000;
    localparam int B_IN2   = 32'h0002_0000;
    localparam int B_IN3   = 32'h0003_0000;
    localparam int B_IN4   = 32'h0004_0000;
    localparam int B_WGT   = 32'h0005_0000;
    localparam int B_W2    = 32'h0006_0000;
    localparam int B_W3    = 32'h0007_0000;
    localparam int B_BIAS  = 32'h0008_0000;
    localparam int B_ZETA  = 32'h0009_0000;
    localparam int B_GAMMA = 32'h000A_0000;
    localparam int B_SLOPE = 32'h000B_0000;
    localparam int B_ALPHA = 32'h000C_0000;
    localparam int B_OUT   = 32'h0020_0000;   // 输出区（留 1 MB）
    localparam int B_EXP   = 32'h0030_0000;   // expected 区（最大 256 KB）
    localparam int B_SCR   = 32'h0000_8000;   // T0 自检的擦写区（DUT 不会访问）

    // 配置页偏移（A[16]=1，见 DUT 文件头 §1）
    localparam [15:0] R_AP_CTRL = 16'h00, R_AP_RET = 16'h10;
    localparam [15:0] R_C  = 16'h18, R_H  = 16'h20, R_W  = 16'h28;
    localparam [15:0] R_HO = 16'h30, R_WO = 16'h38;
    localparam [15:0] R_K  = 16'h40, R_S  = 16'h48, R_D  = 16'h50, R_PAD = 16'h58;
    localparam [15:0] R_SHIFT = 16'h60, R_ASH = 16'h68, R_SSH = 16'h6C, R_COUT = 16'h70;
    localparam [15:0] B_IN_  = 16'h80, B_IN2_ = 16'h88, B_IN3_ = 16'h90, B_IN4_ = 16'h98;
    localparam [15:0] B_WGT_ = 16'hA0, B_W2_  = 16'hA8, B_W3_  = 16'hAC;
    localparam [15:0] B_BIAS_ = 16'hB0, B_ZETA_ = 16'hB8, B_GAMMA_ = 16'hBC;
    localparam [15:0] B_SLOPE_ = 16'hC0, B_OUT_ = 16'hD0, B_ALPHA_ = 16'hD8;
    localparam [15:0] R_DBG_RD = 16'hE0, R_DBG_WR = 16'hE8, R_DBG_FLG = 16'hF0;
    // ★ 0xF8 = 读侧顺序预取开关（rdwin_pf patch 引入；未打 patch 的实现会忽略这次写）
    localparam [15:0] R_PF_EN  = 16'hF8;

    // =========================================================================
    // 时钟 / 复位
    // =========================================================================
    logic clk = 1'b0;
    logic rst_n = 1'b0;
    always #5 clk = ~clk;

    // =========================================================================
    // DUT 端口
    // =========================================================================
    // AXI4-Lite 从
    logic [31:0] l_awaddr;  logic l_awvalid, l_awready;
    logic [31:0] l_wdata;   logic [3:0] l_wstrb; logic l_wvalid, l_wready;
    logic [1:0]  l_bresp;   logic l_bvalid, l_bready;
    logic [31:0] l_araddr;  logic l_arvalid, l_arready;
    logic [31:0] l_rdata;   logic [1:0] l_rresp; logic l_rvalid, l_rready;

    // AXI3 读主口（DUT 输出 AR / 输入 R）
    logic               dut_arvalid, dut_arready;
    logic [31:0]        dut_araddr;
    logic [3:0]         dut_arlen;
    logic [AXIW-1:0]    dut_arid;
    logic               dut_rvalid, dut_rready;
    logic [63:0]        dut_rdata;
    logic               dut_rlast;
    logic [AXIW-1:0]    dut_rid;

    // AXI3 写主口（DUT 输出 AW/W / 输入 B）
    logic               dut_awvalid, dut_awready;
    logic [31:0]        dut_awaddr;
    logic [3:0]         dut_awlen;
    logic [AXIW-1:0]    dut_wid;
    logic               dut_wvalid, dut_wready;
    logic [63:0]        dut_wdata;
    logic [7:0]         dut_wstrb;
    logic               dut_wlast;
    logic               dut_bvalid, dut_bready;

    bin_unit_axi_wrap_v50 #(
        .UNIT    (UNIT_),
        .AXI_ID_W(AXIW),
        .VLANES  (`V94_VLANES)          // ★ v94：8 = v39 等价（控制组）；16 = v50 被测
    ) dut (
        .s_axi_aclk      (clk),
        .s_axi_aresetn   (rst_n),
        .s_axi_awaddr    (l_awaddr),  .s_axi_awvalid (l_awvalid), .s_axi_awready (l_awready),
        .s_axi_wdata     (l_wdata),   .s_axi_wstrb   (l_wstrb),
        .s_axi_wvalid    (l_wvalid),  .s_axi_wready  (l_wready),
        .s_axi_bresp     (l_bresp),   .s_axi_bvalid  (l_bvalid),  .s_axi_bready  (l_bready),
        .s_axi_araddr    (l_araddr),  .s_axi_arvalid (l_arvalid), .s_axi_arready (l_arready),
        .s_axi_rdata     (l_rdata),   .s_axi_rresp   (l_rresp),
        .s_axi_rvalid    (l_rvalid),  .s_axi_rready  (l_rready),
        // ---- 读主口（AXI3）----
        .m_axi_arvalid   (dut_arvalid), .m_axi_arready (dut_arready),
        .m_axi_araddr    (dut_araddr),  .m_axi_arlen   (dut_arlen),
        .m_axi_arid      (dut_arid),
        .m_axi_rvalid    (dut_rvalid),  .m_axi_rready  (dut_rready),
        .m_axi_rdata     (dut_rdata),   .m_axi_rlast   (dut_rlast),
        .m_axi_rid       (dut_rid),
        // ---- 写主口（AXI3）----
        .m_axi_awvalid   (dut_awvalid), .m_axi_awready (dut_awready),
        .m_axi_awaddr    (dut_awaddr),  .m_axi_awlen   (dut_awlen),
        .m_axi_wid       (dut_wid),
        .m_axi_wvalid    (dut_wvalid),  .m_axi_wready  (dut_wready),
        .m_axi_wdata     (dut_wdata),   .m_axi_wstrb   (dut_wstrb),
        .m_axi_wlast     (dut_wlast),
        .m_axi_bvalid    (dut_bvalid),  .m_axi_bready  (dut_bready)
    );



`ifdef V97_BF
    // ★ v97：WR_BUF_BEATS（写 beat FIFO 深度）编译期覆盖 —— 量"加深到多深才不停核"
    defparam dut.WR_BUF_BEATS = `V97_BF;
`endif

    // ★ v94：写侧串行化器探针（层次化引用；xelab --relax 下可用）
    int m_go, m_cen, m_beat, m_core_wr, m_ucelow, m_press;
    int m_fd; int m_n;
    initial begin
        m_go=0; m_cen=0; m_beat=0; m_core_wr=0; m_ucelow=0; m_press=0;
        m_fd=0; m_n=0;
    end
    always @(posedge clk) if (rst_n) begin
        if (dut.u_ce && dut.g_conv.c_wr_en)  m_go      = m_go + 1;
        if (dut.g_conv.cw_beat_en)           m_cen     = m_cen + 1;
        if (dut.g_conv.c_wr_en)              m_core_wr = m_core_wr + 1;
        if (dut.g_conv.c_wr_en && !dut.u_ce) m_ucelow  = m_ucelow + 1;
        if (dut.wr_fifo_press)               m_press   = m_press + 1;
        if (dut.push_now && m_fd != 0)
            $fwrite(m_fd, "PUSH word=%08x pwv=%0b bfc=%0d dfc=%0d bu_len=%0d\n",
                    dut.push_word_x, dut.pw_valid,
                    dut.bf_cnt, dut.df_cnt, dut.bu_len);
    end
    // 只 VLANES>8 才有 g_wr_split2（generate 未选中的分支不参与 elaborate）
`ifdef V94_P2PROBE
    generate if (`V94_VLANES > 8) begin : g_p2probe
        always @(posedge clk) if (rst_n) begin
            if (dut.g_conv.g_wr_split2.p1_v) m_beat = m_beat + 1;
            if (dut.u_ce && dut.g_conv.c_wr_en) begin
                if (m_fd != 0)
                    $fwrite(m_fd, "%0d core=%08x beat=%08x pix=%0d pbase=%08x\n",
                            m_n, dut.g_conv.c_wr_addr,
                            dut.g_conv.g_wr_split2.beat_addr,
                            dut.g_conv.g_wr_split2.pix_q,
                            dut.g_conv.g_wr_split2.pbase_q);
                m_n = m_n + 1;
            end
        end
    end endgenerate
`endif


    // =========================================================================
    // 统计 / 判定
    // =========================================================================
    int n_pass = 0, n_fail = 0;
    int axi_errors = 0;
    int n_ar = 0, n_rbeat = 0, n_aw = 0, n_wbeat = 0;
    int max_rq = 0;                  // 实测最大在飞读笔数
    int max_arlen = 0;               // 实测最大 arlen（读侧重写后应变成 7）
    int n_rburst = 0;                // arlen>0 的 AR 笔数
    int n_rd_bad = 0;                // 逐拍读数据对拍错
    int n_arbp = 0, n_awbp = 0;      // 实测发生的反压次数（覆盖统计）
    // ---- 只统计 **DUT 侧** 的流量（T0 自检的 AR/R 不计入，否则会污染覆盖结论）----
    int d_ar = 0, d_rbeat = 0, d_rburst = 0, d_maxalen = 0;

    task automatic chk(input string name, input [31:0] got, input [31:0] exp);
        begin
            if (got === exp) begin
                $display("[TB] PASS %0s = 0x%08h", name, got);
                n_pass = n_pass + 1;
            end else begin
                $display("[TB] FAIL %0s : got 0x%08h exp 0x%08h", name, got, exp);
                n_fail = n_fail + 1;
            end
        end
    endtask

    task automatic chk_ge(input string name, input int got, input int lim);
        begin
            if (got >= lim) begin
                $display("[TB] PASS %0s = %0d (>= %0d)", name, got, lim);
                n_pass = n_pass + 1;
            end else begin
                $display("[TB] FAIL %0s = %0d (< %0d)", name, got, lim);
                n_fail = n_fail + 1;
            end
        end
    endtask

    // mem[a .. a+7] 组合成 64 位字（小端：a+0 是最低字节）
    function automatic logic [63:0] memword(input [31:0] a);
        memword = {mem[a+7], mem[a+6], mem[a+5], mem[a+4],
                   mem[a+3], mem[a+2], mem[a+1], mem[a+0]};
    endfunction

    // ---- 两条独立 LCG（读/写从机各一条，避免两个 always 块抢同一变量）----
    logic [31:0] lfsr_r, lfsr_w;

    function automatic int ur_r(input int lo, input int hi);
        int span, off;
        begin
            lfsr_r = lfsr_r * 32'd1664525 + 32'd1013904223;
            span = hi - lo + 1;
            off  = (lfsr_r >> 8) % span;
            ur_r = lo + off;
        end
    endfunction

    function automatic int ur_w(input int lo, input int hi);
        int span, off;
        begin
            lfsr_w = lfsr_w * 32'd1664525 + 32'd1013904223;
            span = hi - lo + 1;
            off  = (lfsr_w >> 8) % span;
            ur_w = lo + off;
        end
    endfunction

    // =========================================================================
    // AXI4-Lite 主机
    // =========================================================================
    task automatic lite_wr(input [31:0] addr, input [31:0] data);
        begin
            @(posedge clk);
            l_awaddr <= addr; l_awvalid <= 1'b1;
            l_wdata  <= data; l_wstrb <= 4'hF; l_wvalid <= 1'b1;
            l_bready <= 1'b1;
            fork
                begin
                    @(posedge clk);
                    while (!l_awready) @(posedge clk);
                    l_awvalid <= 1'b0;
                end
                begin
                    @(posedge clk);
                    while (!l_wready) @(posedge clk);
                    l_wvalid <= 1'b0;
                end
            join
            while (!l_bvalid) @(posedge clk);
            @(posedge clk);
            l_bready <= 1'b0;
        end
    endtask

    task automatic lite_rd(input [31:0] addr, output [31:0] data);
        begin
            @(posedge clk);
            l_araddr <= addr; l_arvalid <= 1'b1; l_rready <= 1'b1;
            @(posedge clk);
            while (!l_arready) @(posedge clk);
            l_arvalid <= 1'b0;
            while (!l_rvalid) @(posedge clk);
            data = l_rdata;
            @(posedge clk);
            l_rready <= 1'b0;
        end
    endtask

    task automatic w_cfg(input [15:0] off, input [31:0] d);
        lite_wr({15'd0, 1'b1, off}, d);       // A[16]=1 ⇒ 配置页
    endtask
    task automatic r_cfg(input [15:0] off, output [31:0] d);
        lite_rd({15'd0, 1'b1, off}, d);
    endtask

    // =========================================================================
    // AXI3 读从机模型：**支持 burst（arlen 0..15）+ 随机 arready 反压 +
    // 随机返回延迟 + 跨 ID 乱序 + 同 ID 保序**
    //
    //  · 请求表 RQ 项，每项记 addr/id/len/已回拍数/序号/剩余延迟
    //  · 一旦开始回一笔 burst 就**连续回完**（不做拍级交织）——
    //    AXI3 允许交织、AXI4 不允许，连续回完在两者下都合法，也最简单
    //  · 保序判据用 **AR 序号**而不是表项号（表项会复用，复用后低号可能是
    //    后发的 AR —— conv 侧 TB 就在这里踩过坑）
    //  · 自检模式下从机由 TB 直接驱动（st_*），功能模式下由 DUT 驱动
    // =========================================================================
    localparam int RQ = 64;
    logic [31:0]     rq_addr [0:RQ-1];
    logic [AXIW-1:0] rq_id   [0:RQ-1];
    logic [3:0]      rq_len  [0:RQ-1];
    logic [3:0]      rq_beat [0:RQ-1];
    logic [31:0]     rq_seq  [0:RQ-1];
    int              rq_dly  [0:RQ-1];
    logic            rq_v    [0:RQ-1];

    // 从机端口（内部）与 DUT 端口的 mux（自检模式 = TB 直接驱动从机）
    logic             selftest;         // 1 = 从机由 TB 的 st_* 驱动
    logic             s_arvalid, s_arready;
    logic [31:0]      s_araddr;
    logic [3:0]       s_arlen;
    logic [AXIW-1:0]  s_arid;
    logic             s_rvalid, s_rready;
    logic [63:0]      s_rdata;
    logic             s_rlast;
    logic [AXIW-1:0]  s_rid;

    logic             st_arvalid;
    logic [31:0]      st_araddr;
    logic [3:0]       st_arlen;
    logic [AXIW-1:0]  st_arid;

    assign s_arvalid  = selftest ? st_arvalid : dut_arvalid;
    assign s_araddr   = selftest ? st_araddr  : dut_araddr;
    assign s_arlen    = selftest ? st_arlen   : dut_arlen;
    assign s_arid     = selftest ? st_arid    : dut_arid;
    assign s_rready   = selftest ? 1'b1       : dut_rready;

    // 给 DUT 的读响应：自检期间喂 0（DUT 没启动，也不该收到数据）
    assign dut_arready = selftest ? 1'b0    : s_arready;
    assign dut_rvalid  = selftest ? 1'b0    : s_rvalid;
    assign dut_rdata   = selftest ? 64'd0   : s_rdata;
    assign dut_rlast   = selftest ? 1'b0    : s_rlast;
    assign dut_rid     = selftest ? {AXIW{1'b0}} : s_rid;

    // ---- 自检记分板：每次 R 握手记一笔 (burst 首地址, 该拍数据) ----
    logic [31:0] hist_addr [0:255];
    logic [63:0] hist_data [0:255];
    int          hist_n = 0;

    int   rq_free_cnt;
    integer i_free;
    always @* begin
        rq_free_cnt = 0;
        for (i_free = 0; i_free < RQ; i_free = i_free + 1)
            if (!rq_v[i_free]) rq_free_cnt = rq_free_cnt + 1;
    end

    int  ar_stall;                  // 随机反压剩余拍数
    int  r_sel;                     // 当前正在回哪一项
    logic r_busy;                   // 有 burst 正在回（禁止交织）
    int  rlmin, rlmax, abp_en, rooo;
    // ★ 板端校准用的两个旋钮（默认值 = 不改变原来的功能仿真行为）
    //   maxout : 读通道最多几笔同时在飞（模拟 DDR 的并发服务能力）。默认 16 = 不限
    //            （RD_WIN_WORDS=8 时天然只有 8 笔，所以默认等于不限）。
    //   rbext  : "一笔 arlen>0 的 burst 比单拍多花多少拍"（模拟 8 拍串行回数）。
    //            默认 0 = 与单拍同延迟；校准模式用 +RBURST_EXTRA=7（8 拍串行）。
    int  maxout, rbext;
    int  pfen;                  // 1 = 打开读侧顺序预取（0xF8 bit0）
    int  slot_f, sel_r, j_ord;
    integer i_ar, i_dl, i_sel;
    logic   legal;

    // 在飞笔数上限：DDR 端最多同时服务几笔（板端并发能力的建模旋钮）
    assign s_arready = (rq_free_cnt > 0) && (ar_stall == 0) &&
                       (((RQ - rq_free_cnt) < maxout) || (maxout >= RQ));

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s_rvalid <= 1'b0;
            s_rdata  <= 64'd0;
            s_rlast  <= 1'b0;
            s_rid    <= {AXIW{1'b0}};
            r_busy   <= 1'b0;
            r_sel    = 0;
            ar_stall <= 0;
            for (i_ar = 0; i_ar < RQ; i_ar = i_ar + 1) begin
                rq_v[i_ar]    <= 1'b0;
                rq_dly[i_ar]  <= 0;
                rq_len[i_ar]  <= 4'd0;
                rq_beat[i_ar] <= 4'd0;
                rq_seq[i_ar]  <= 32'd0;
            end
        end else begin
            // ---------------- 1) 收 AR ----------------
            if (s_arvalid && s_arready) begin
                slot_f = -1;
                for (i_ar = 0; i_ar < RQ; i_ar = i_ar + 1)
                    if (!rq_v[i_ar] && (slot_f < 0)) slot_f = i_ar;
                if (slot_f < 0) begin
                    $display("[TB-AXI] ERROR: AR 到达但请求表已满（不应发生）");
                    axi_errors = axi_errors + 1;
                end else begin
                    rq_v[slot_f]    <= 1'b1;
                    rq_addr[slot_f] <= s_araddr;
                    rq_id[slot_f]   <= s_arid;
                    rq_len[slot_f]  <= s_arlen;
                    rq_beat[slot_f] <= 4'd0;
                    rq_seq[slot_f]  <= n_ar;
                    // 初延迟 1..20 拍（模拟 DDR 往返）；同一项只等一次
                    rq_dly[slot_f]  <= ur_r(rlmin, rlmax) +
                                       (((s_arlen != 4'd0) && (rbext > 0)) ? rbext : 0);
                    n_ar = n_ar + 1;
                    if (s_arlen > max_arlen) max_arlen = s_arlen;
                    if (s_arlen != 4'd0) n_rburst = n_rburst + 1;
                    if (!selftest) begin
                        d_ar = d_ar + 1;
                        if (s_arlen > d_maxalen) d_maxalen = s_arlen;
                        if (s_arlen != 4'd0) d_rburst = d_rburst + 1;
                    end
                    // 本拍新收一笔 ⇒ 在飞数 = (RQ - 空闲) + 1
                    if ((RQ - rq_free_cnt + 1) > max_rq) max_rq = RQ - rq_free_cnt + 1;
                    // ---- AXI 协议检查 ----
                    if (s_araddr[2:0] !== 3'd0) begin
                        $display("[TB-AXI] ERROR: ARADDR 未按 8 字节对齐 0x%08h", s_araddr);
                        axi_errors = axi_errors + 1;
                    end
                    // 一拍 8 字节 ⇒ 一笔 burst 的字节数 = (arlen+1)*8，不得跨 4KB
                    if (({20'd0, s_araddr[11:0]} + ((s_arlen + 4'd1) * 8)) > 13'd4096) begin
                        $display("[TB-AXI] ERROR: 读 burst 跨 4 KB addr=0x%08h arlen=%0d",
                                 s_araddr, s_arlen);
                        axi_errors = axi_errors + 1;
                    end
                end
            end

            // ---------------- 2) 随机 arready 反压（0~3 拍）----------------
            if (ar_stall != 0) begin
                ar_stall <= ar_stall - 1;
            end else if (abp_en && (ur_r(0, 3) == 0)) begin
                ar_stall <= ur_r(1, 3);
                n_arbp   = n_arbp + 1;
            end

            // ---------------- 3) 延迟递减 ----------------
            for (i_dl = 0; i_dl < RQ; i_dl = i_dl + 1)
                if (rq_v[i_dl] && (rq_dly[i_dl] > 0)) rq_dly[i_dl] <= rq_dly[i_dl] - 1;

            // ---------------- 4) R 通道 ----------------
            if (s_rvalid && s_rready) begin
                n_rbeat = n_rbeat + 1;
                if (!selftest) d_rbeat = d_rbeat + 1;
                if (hist_n < 256) begin
                    hist_addr[hist_n] = rq_addr[r_sel];
                    hist_data[hist_n] = s_rdata;
                    hist_n = hist_n + 1;
                end
                if (rq_beat[r_sel] == rq_len[r_sel]) begin
                    // 末拍：整笔完成，表项释放
                    s_rvalid     <= 1'b0;
                    s_rlast      <= 1'b0;
                    rq_v[r_sel]  <= 1'b0;
                    r_busy       <= 1'b0;
                end else begin
                    rq_beat[r_sel] <= rq_beat[r_sel] + 4'd1;
                    s_rdata  <= memword(rq_addr[r_sel] + (rq_beat[r_sel] * 8) + 8);
                    s_rlast  <= ((rq_beat[r_sel] + 4'd1) == rq_len[r_sel]);
                end
            end else if (!s_rvalid) begin
                if (!r_busy) begin
                    // 挑一笔"延迟已到、且没有同 ID 的更早请求没回"的
                    sel_r = -1;
                    if (rooo) begin
                        // 优先回"最新"的那笔 ⇒ 天然压出跨 ID 乱序
                        for (i_sel = RQ - 1; i_sel >= 0; i_sel = i_sel - 1) begin
                            if (rq_v[i_sel] && (rq_dly[i_sel] <= 0)) begin
                                legal = 1'b1;
                                for (j_ord = 0; j_ord < RQ; j_ord = j_ord + 1)
                                    if (rq_v[j_ord] && (rq_id[j_ord] == rq_id[i_sel]) &&
                                        (rq_seq[j_ord] < rq_seq[i_sel])) legal = 1'b0;
                                if (legal) sel_r = i_sel;
                            end
                        end
                    end else begin
                        for (i_sel = 0; i_sel < RQ; i_sel = i_sel + 1) begin
                            if (rq_v[i_sel] && (rq_dly[i_sel] <= 0) && (sel_r < 0)) begin
                                legal = 1'b1;
                                for (j_ord = 0; j_ord < RQ; j_ord = j_ord + 1)
                                    if (rq_v[j_ord] && (rq_id[j_ord] == rq_id[i_sel]) &&
                                        (rq_seq[j_ord] < rq_seq[i_sel])) legal = 1'b0;
                                if (legal) sel_r = i_sel;
                            end
                        end
                    end
                    if (sel_r >= 0) begin
                        r_busy   <= 1'b1;
                        r_sel    = sel_r;
                        s_rvalid <= 1'b1;
                        s_rid    <= rq_id[sel_r];
                        s_rdata  <= memword(rq_addr[sel_r]);
                        s_rlast  <= (rq_len[sel_r] == 4'd0);
                    end
                end
            end
        end
    end


    // ★ v97：写压力 / FIFO 占用探针（**只读**，不驱动任何信号）
    int   v97_bf_peak, v97_df_peak, v97_press_cyc, v97_press_blk, v97_split_blk, v97_run_cyc;
    int   v97_done_early;
    always @(posedge clk) if (rst_n && dut.run_active) begin
        v97_run_cyc = v97_run_cyc + 1;
        if (dut.bf_cnt > v97_bf_peak) v97_bf_peak = dut.bf_cnt;
        if (dut.df_cnt > v97_df_peak) v97_df_peak = dut.df_cnt;
        if (dut.wr_fifo_press) v97_press_cyc = v97_press_cyc + 1;
        if (dut.wr_take && dut.wr_fifo_press) v97_press_blk = v97_press_blk + 1;
        if (dut.wr_split_busy) v97_split_blk = v97_split_blk + 1;
        // ★ v97：done 早报窗口 —— unit_done_q && wr_all_idle 成立，但串行化缓冲里还有拍
        if (dut.unit_done_q && dut.wr_all_idle && dut.wr_split_pend_i)
            v97_done_early = v97_done_early + 1;
    end

    // =========================================================================
    // AXI3 写从机模型：AW → (awlen+1) 拍 W → B，按 WSTRB 落字节；
    // awready / wready / bvalid 各有随机反压（+WBP=0 关掉）
    // =========================================================================
    localparam [1:0] WS_IDLE = 2'd0, WS_DATA = 2'd1, WS_RESP = 2'd2;
    logic [1:0]  wsl;
    logic [31:0] ws_addr;
    logic [3:0]  ws_len, ws_cnt;
    logic        s_awready, s_wready, s_bvalid;
    int          wa_stall, wd_stall, wb_dly;
    // ★ v97：+WSTALLN=<n> 把 wready 反压加大到 n 拍（板上 DDR 争用/互连延迟的粗模型）
    //   n=0 ⇒ 与原 v94 行为逐位相同（默认）。
    int          wstalln;
    int          wbm = 8;                 // = DUT.WR_BURST_MAX（用于 AWLEN 上限检查）
    int          wbp_en;
    integer      bi, i_ws;
    // ---- ★ v94：逐笔 (addr,data,sel) 记录（+T94LOG=<file>）----
    int          t94fd;
    int          t94_n;
    string       t94file;

    assign s_awready = (wsl == WS_IDLE) && (wa_stall == 0) && !s_bvalid;
    assign s_wready  = (wsl == WS_DATA) && (wd_stall == 0);
    assign dut_awready = s_awready;
    assign dut_wready  = s_wready;
    assign dut_bvalid  = s_bvalid;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wsl       <= WS_IDLE;
            s_bvalid  <= 1'b0;
            wa_stall  <= 0;
            wd_stall  <= 0;
            wb_dly    = 0;
            ws_addr   <= 32'd0;
            ws_len    <= 4'd0;
            ws_cnt    <= 4'd0;
        end else begin
            // ---- 随机反压 ----
            if (wa_stall != 0) begin
                wa_stall <= wa_stall - 1;
            end else if (wbp_en && (ur_w(0, 3) == 0)) begin
                wa_stall <= ur_w(1, 3);
                n_awbp   = n_awbp + 1;
            end
            if (wd_stall != 0) begin
                wd_stall <= wd_stall - 1;
            end else if ((wsl == WS_DATA) && wbp_en && (ur_w(0, 7) == 0)) begin
                wd_stall <= (wstalln > 0) ? wstalln : ur_w(1, 2);
            end

            case (wsl)
            WS_IDLE: begin
                if (dut_awvalid && s_awready) begin
                    ws_addr  <= dut_awaddr;
                    ws_len   <= dut_awlen;
                    ws_cnt   <= 4'd0;
                    wsl      <= WS_DATA;
                    n_aw     = n_aw + 1;
                    // ---- AXI3 写 burst 合法性 ----
                    if (dut_awaddr[2:0] !== 3'd0) begin
                        $display("[TB-AXI] ERROR: AWADDR 未按 8 字节对齐 0x%08h", dut_awaddr);
                        axi_errors = axi_errors + 1;
                    end
                    if (dut_wid !== {AXIW{1'b0}}) begin
                        $display("[TB-AXI] ERROR: WID 应为 0（单一写源），实际 %0d", dut_wid);
                        axi_errors = axi_errors + 1;
                    end
                    if (dut_awlen >= wbm[3:0]) begin
                        $display("[TB-AXI] ERROR: AWLEN=%0d 超过 WR_BURST_MAX-1=%0d",
                                 dut_awlen, wbm-1);
                        axi_errors = axi_errors + 1;
                    end
                    if (({20'd0, dut_awaddr[11:0]} + ((dut_awlen + 4'd1) * 8)) > 13'd4096) begin
                        $display("[TB-AXI] ERROR: 写 burst 跨 4 KB awaddr=0x%08h awlen=%0d",
                                 dut_awaddr, dut_awlen);
                        axi_errors = axi_errors + 1;
                    end
                    if ((dut_awaddr < B_OUT) || (dut_awaddr >= B_EXP)) begin
                        $display("[TB-AXI] ERROR: 写越界 awaddr=0x%08h（应落在 0x%08h..0x%08h）",
                                 dut_awaddr, B_OUT, B_EXP);
                        axi_errors = axi_errors + 1;
                    end
                end
            end
            WS_DATA: begin
                if (dut_wvalid && s_wready) begin
                    for (bi = 0; bi < 8; bi = bi + 1)
                        if (dut_wstrb[bi])
                            mem[ws_addr + (ws_cnt * 8) + bi] = dut_wdata[bi*8 +: 8];
                    // ★ v94：逐笔记录（AXI 语义下的真实 (addr,data,sel)）
                    if (t94fd != 0)
                        $fwrite(t94fd, "%0d %08x %016x %02x\n",
                                t94_n, ws_addr + (ws_cnt * 8), dut_wdata, dut_wstrb);
                    t94_n = t94_n + 1;
                    n_wbeat = n_wbeat + 1;
                    if (dut_wstrb === 8'd0) begin
                        $display("[TB-AXI] ERROR: WSTRB=0 的空拍 addr=0x%08h", ws_addr);
                        axi_errors = axi_errors + 1;
                    end
                    if (dut_wlast !== (ws_cnt == ws_len)) begin
                        $display("[TB-AXI] ERROR: WLAST 位置错 cnt=%0d len=%0d wlast=%0d",
                                 ws_cnt, ws_len, dut_wlast);
                        axi_errors = axi_errors + 1;
                    end
                    if (ws_cnt == ws_len) begin
                        wsl      <= WS_RESP;
                        wb_dly    = ur_w(0, 2);
                        s_bvalid <= 1'b0;
                    end else begin
                        ws_cnt <= ws_cnt + 4'd1;
                    end
                end
            end
            WS_RESP: begin
                if (wb_dly != 0) begin
                    wb_dly <= wb_dly - 1;
                end else begin
                    s_bvalid <= 1'b1;
                end
                if (s_bvalid && dut_bready) begin
                    s_bvalid <= 1'b0;
                    wsl      <= WS_IDLE;
                end
            end
            default: wsl <= WS_IDLE;
            endcase
        end
    end

    // =========================================================================
    // 逐拍读数据对拍（包装层最硬的 ground truth）
    //   单元 t 拍发 rd_en/addr，t+1 拍取 rd_data。这里把"被放行的那一拍"的
    //   地址记下来，下一拍拿 mem[addr..addr+7] 与 dut.u_rd_data 逐字节比。
    //   缓存/槽号/对齐/CAM 任何错都会在这里露，而且比"最终输出错"好定位。
    // =========================================================================
    logic [31:0] mon_addr_q;
    logic        mon_hit_q;
    logic [63:0] mon_exp;
    always @(posedge clk) begin
        mon_addr_q <= dut.u_rd_addr;
        mon_hit_q  <= rst_n && dut.u_ce && dut.u_rd_en && dut.rd_hit;
    end
    always @(negedge clk) begin
        if (mon_hit_q) begin
            mon_exp = memword(mon_addr_q);
            if (dut.u_rd_data !== mon_exp) begin
                if (n_rd_bad < 8)
                    $display("[TB-RD] ERROR: 逐拍读数据不符 addr=0x%08h got=%016h exp=%016h",
                             mon_addr_q, dut.u_rd_data, mon_exp);
                n_rd_bad = n_rd_bad + 1;
            end
        end
    end

    // =========================================================================
    // T0：AXI3 从模型自检（不经 DUT —— 保证"模型本身可信"）
    // =========================================================================
    int st_err = 0;

    task automatic st_put_ar(input [31:0] a, input [AXIW-1:0] id, input [3:0] len);
        int guard;
        begin
            // ⚠ 三种驱动方式在这里有讲究：
            //   ① st_* 用**阻塞**赋值、且只在 negedge 改（半周期，没有触发器在采样）；
            //   ② 当前时刻就在 negedge ⇒ 此刻的 s_arready 就是"下一个 posedge
            //      从机会看到的值"（ar_stall 只在时钟沿变），所以可以在驱动完
            //      立刻判 handshake，**不能**再等一个 negedge（那样 AR 可能已经
            //      在上一拍被收走了，后面会白等一个永远不会来的握手）。
            st_araddr  = a;
            st_arid    = id;
            st_arlen   = len;
            st_arvalid = 1'b1;
            guard = 0;
            while (!s_arready && (guard < 500)) begin
                @(negedge clk);
                guard = guard + 1;
            end
            if (!s_arready) begin
                $display("[TB-T0] ERROR: AR 一直不被接收 addr=0x%08h", a);
                st_err = st_err + 1;
            end
            @(posedge clk);            // 握手拍
            st_arvalid <= 1'b0;        // 沿后用 NBA 撤 ⇒ 不会二次握手
            @(negedge clk);
        end
    endtask

    // 收 len+1 拍并与 mem 对拍
    task automatic st_get_beats(input [31:0] a, input [AXIW-1:0] id, input [3:0] len);
        int beat, guard;
        logic [63:0] exp64;
        begin
            beat  = 0;
            guard = 0;
            while ((beat <= len) && (guard < 5000)) begin
                @(negedge clk);
                guard = guard + 1;
                if (s_rvalid) begin
                    exp64 = memword(a + (beat * 8));
                    if (s_rid !== id) begin
                        $display("[TB-T0] ERROR: burst RID 错 addr=0x%08h beat=%0d got=%0d exp=%0d",
                                 a, beat, s_rid, id);
                        st_err = st_err + 1;
                    end
                    if (s_rlast !== (beat == len)) begin
                        $display("[TB-T0] ERROR: RLAST 位置错 addr=0x%08h beat=%0d len=%0d rlast=%0d",
                                 a, beat, len, s_rlast);
                        st_err = st_err + 1;
                    end
                    if (s_rdata !== exp64) begin
                        $display("[TB-T0] ERROR: burst 数据错 addr=0x%08h beat=%0d got=%016h exp=%016h",
                                 a, beat, s_rdata, exp64);
                        st_err = st_err + 1;
                    end
                    beat = beat + 1;
                end
            end
            if (beat != (len + 1)) begin
                $display("[TB-T0] ERROR: burst 只回了 %0d/%0d 拍 addr=0x%08h",
                         beat, len + 1, a);
                st_err = st_err + 1;
            end
        end
    endtask

    // 等到从机彻底空闲（请求表空 + 没有挂着的 R 拍）。
    //   必要性：st_get_beats 在"看到末拍"的 negedge 就返回了，而那拍要到**下一个
    //   posedge** 才成交 ⇒ 紧接着开始下一笔会把上一笔的末拍算进新用例里。
    task automatic st_wait_idle;
        int g;
        begin
            g = 0;
            while (((rq_free_cnt != RQ) || s_rvalid) && (g < 2000)) begin
                @(negedge clk);
                g = g + 1;
            end
            if (g >= 2000) begin
                $display("[TB-T0] ERROR: 从机未能回到空闲（rq 剩 %0d, rvalid=%b）",
                         RQ - rq_free_cnt, s_rvalid);
                st_err = st_err + 1;
            end
        end
    endtask

    task automatic st_burst(input [31:0] a, input [AXIW-1:0] id, input [3:0] len);
        begin
            st_wait_idle();
            st_put_ar(a, id, len);
            st_get_beats(a, id, len);
        end
    endtask

    task automatic axi_model_selftest;
        int k, beat, guard;
        logic [63:0] exp64;
        begin
            $display("[TB] ---- T0 AXI3 从模型自检（arlen=0/1/3/7/15 + 同 ID 保序）----");
            @(negedge clk);
            selftest   = 1'b1;
            st_arvalid = 1'b0;
            @(negedge clk);

            // 擦写区填可区分的图案（全 0 的 mem 抓不到地址混淆）
            for (k = 0; k < 4096; k = k + 1)
                mem[B_SCR + k] = (k * 37 + 11) % 256;

            // ① 各种长度的 burst
            st_burst(B_SCR + 32'h000, 4'd0, 4'd0);
            st_burst(B_SCR + 32'h100, 4'd1, 4'd1);
            st_burst(B_SCR + 32'h200, 4'd2, 4'd3);
            st_burst(B_SCR + 32'h300, 4'd3, 4'd7);
            st_burst(B_SCR + 32'h400, 4'd4, 4'd15);
            st_burst(B_SCR + 32'h500, 4'd5, 4'd7);

            // ② 同 ID 多笔必须按 AR 顺序返回（每笔单拍）
            //    ⚠ 不能"边发边收"：最早发的那笔可能在后面几笔还在握手时就已经
            //      回数并成交（selftest 下 rready 恒 1）⇒ 会漏掉第一拍。
            //      改用从机侧记分板 hist_*：握手时把 (burst 首地址, 数据) 记下来，
            //      发完 8 笔再一次性比对顺序。
            st_wait_idle();
            // ★ 等所有在途 burst 彻底回完再清记分板，否则上一批的末拍会
            //   在 hist_n=0 之后才落进 hist_*，导致同 ID 保序比对整体错一位。
            guard = 0;
            while (((RQ - rq_free_cnt) != 0) && (guard < 5000)) begin
                @(negedge clk);
                guard = guard + 1;
            end
            repeat (2) @(negedge clk);
            hist_n = 0;
            for (k = 0; k < 8; k = k + 1)
                st_put_ar(B_SCR + 32'h600 + (k * 16), 4'd7, 4'd0);
            guard = 0;
            while ((hist_n < 8) && (guard < 5000)) begin
                @(negedge clk);
                guard = guard + 1;
            end
            if (hist_n != 8) begin
                $display("[TB-T0] ERROR: 同 ID 8 笔只回了 %0d 笔", hist_n);
                st_err = st_err + 1;
            end
            for (k = 0; k < 8; k = k + 1) begin
                exp64 = memword(B_SCR + 32'h600 + (k * 16));
                if (hist_addr[k] !== (B_SCR + 32'h600 + (k * 16))) begin
                    $display("[TB-T0] ERROR: 同 ID 保序失败 第 %0d 笔地址 got=0x%08h exp=0x%08h",
                             k, hist_addr[k], B_SCR + 32'h600 + (k * 16));
                    st_err = st_err + 1;
                end
                if (hist_data[k] !== exp64) begin
                    $display("[TB-T0] ERROR: 同 ID 保序失败 第 %0d 笔数据 got=%016h exp=%016h",
                             k, hist_data[k], exp64);
                    st_err = st_err + 1;
                end
            end

            st_arvalid <= 1'b0;
            repeat (4) @(negedge clk);
            selftest    = 1'b0;
            repeat (2) @(negedge clk);

            if (st_err == 0) begin
                $display("[TB] PASS T0 AXI3 从模型自检（burst/rid/rlast/同 ID 保序 全对）");
                n_pass = n_pass + 1;
            end else begin
                $display("[TB] FAIL T0 AXI3 从模型自检：%0d 处错（模型不可信，后续结果无意义）",
                         st_err);
                n_fail = n_fail + 1;
            end
        end
    endtask

    // =========================================================================
    // 黄金向量装载
    // =========================================================================
    string gdir;
    int    fh, nb_exp;

    task automatic load_file(input string fn, input int base, output int nbytes);
        int c;
        begin
            fh = $fopen({gdir, "/", fn}, "rb");
            if (fh == 0) $fatal(1, "[TB] FAIL: 打不开 %s/%s（先跑 tools/gen_bin_golden.py --all）",
                                gdir, fn);
            nbytes = 0;
            c = $fgetc(fh);
            while (c >= 0) begin
                mem[base + nbytes] = c[7:0];
                nbytes = nbytes + 1;
                c = $fgetc(fh);
            end
            $fclose(fh);
            $display("[TB] 装载 %-18s -> 0x%07h  %0d 字节", fn, base, nbytes);
        end
    endtask

    // =========================================================================
    // 形状（+SHAPE_NAME 选；+CIN 等可选覆盖，防止表与 golden 漂移）
    // =========================================================================
    int C, H, W, K, S, D, PAD, COUT, HO, WO, SHIFT;
    int NCH, HOUT, WOUT;            // 输出通道数 / 输出高 / 输出宽（按 UNIT 归一）
    string shape_name;
    int t_i;

    task automatic set_shape(input string s);
        begin
            C = 0; H = 0; W = 0; K = 1; S = 1; D = 1; PAD = 0;
            COUT = 0; HO = 0; WO = 0; SHIFT = 1;
            // ---------- UNIT 0：bin_conv_core ----------
            if      (s == "bin_1x32_32_8x8")  begin C=32; COUT=32; K=1; H=8; W=8; PAD=0; SHIFT=7; end
            else if (s == "bin_1x64_16x16")   begin C=64; COUT=64; K=1; H=8; W=8; PAD=0; SHIFT=7; end
            else if (s == "bin_3x32_48_8x8")  begin C=32; COUT=48; K=3; H=8; W=8; PAD=1; SHIFT=7; end
            else if (s == "bin_3x32_48_d2")   begin C=32; COUT=32; K=3; H=8; W=8; PAD=2; D=2; SHIFT=7; end
            else if (s == "bin_3x96_64_8x8")  begin C=96; COUT=64; K=3; H=8; W=8; PAD=1; SHIFT=7; end
            // ---- 64×64 golden（WIN16=128B 线的读侧才测得出差异；以前不在表里所以从没被 verify_all 跑到）
            else if (s == "bin_1x64_64_64x64") begin C=64; COUT=64; K=1; H=64; W=64; PAD=0; SHIFT=7; end
            else if (s == "bin_3x48_32_64x64") begin C=48; COUT=32; K=3; H=64; W=64; PAD=1; SHIFT=7; end
            else if (s == "bin_3x64_64_64x64") begin C=64; COUT=64; K=3; H=64; W=64; PAD=1; SHIFT=7; end
            else if (s == "bin_3x96_32_64x64") begin C=96; COUT=32; K=3; H=64; W=64; PAD=1; SHIFT=7; end
            // ---------- UNIT 1：bin_rprelu ----------
            else if (s == "rprelu_64_8x8")    begin C=64; H=8; W=8; SHIFT=6; end
            else if (s == "rprelu_96_8x8")    begin C=96; H=8; W=8; SHIFT=6; end
            else if (s == "rprelu_48_4x16")   begin C=48; H=4; W=16; SHIFT=6; end
            // ---------- UNIT 2：bin_sam ----------
            else if (s == "sam_32_8x8")       begin C=32; H=8; W=8; SHIFT=6; end
            else if (s == "sam_64_8x8")       begin C=64; H=8; W=8; SHIFT=6; end
            // ---------- UNIT 3：bin_interp ----------
            else if (s == "interp_16_8x8_to_16x16")  begin C=16; H=8;  W=8;  HO=16; WO=16; end
            else if (s == "interp_16_16x16_to_8x8")  begin C=16; H=16; W=16; HO=8;  WO=8;  end
            // 不在内置表里 ⇒ 参数**全部**由 plusarg 给（verify_all/bench_lat 一定会传全套）。
            // 这样新增 golden 不需要改本文件（64x64 的三个大形状就是这么跑的）。
            else begin
                $display("[TB] NOTE: 形状 %0s 不在内置表里，参数将全部取自 plusarg", s);
                C = 0; H = 0; W = 0; COUT = 0;
            end
            // 可选覆盖（verify_all 从 golden/shape.env 传过来）
            if ($value$plusargs("CIN=%d",   t_i)) C    = t_i;
            if ($value$plusargs("COUT=%d",  t_i)) COUT = t_i;
            if ($value$plusargs("K=%d",     t_i)) K    = t_i;
            if ($value$plusargs("S=%d",     t_i)) S    = t_i;
            if ($value$plusargs("D=%d",     t_i)) D    = t_i;
            if ($value$plusargs("PAD=%d",   t_i)) PAD  = t_i;
            if ($value$plusargs("H=%d",     t_i)) H    = t_i;
            if ($value$plusargs("W=%d",     t_i)) W    = t_i;
            if ($value$plusargs("SHIFT=%d", t_i)) SHIFT= t_i;
            if ($value$plusargs("HO=%d",    t_i)) HO   = t_i;
            if ($value$plusargs("WO=%d",    t_i)) WO   = t_i;
            if ($value$plusargs("H0=%d",    t_i)) HO   = t_i;   // 别名
            if ($value$plusargs("W0=%d",    t_i)) WO   = t_i;
            if (S <= 0) S = 1;
            if (D <= 0) D = 1;
            if ((C <= 0) || (H <= 0) || (W <= 0) || ((UNIT_ == 0) && (COUT <= 0)))
                $fatal(1, "[TB] FAIL: 形状参数不全（C=%0d H=%0d W=%0d COUT=%0d）—— 请传 +CIN/+COUT/+H/+W",
                       C, H, W, COUT);
            // ---- 归一成 C8 输出布局的参数 ----
            if (UNIT_ == 0) begin
                HOUT = (H - 1) / S + 1;
                WOUT = (W - 1) / S + 1;
                NCH  = COUT;
            end else if (UNIT_ == 3) begin
                HOUT = (HO > 0) ? HO : (H - 1) / S + 1;
                WOUT = (WO > 0) ? WO : (W - 1) / S + 1;
                NCH  = C;
            end else begin
                HOUT = (H - 1) / S + 1;
                WOUT = (W - 1) / S + 1;
                NCH  = C;
            end
        end
    endtask

    // =========================================================================
    // 写配置寄存器（按 UNIT 选）
    // =========================================================================
    task automatic cfg_apply(input int unit_sel);
        begin
            w_cfg(R_C,    C[31:0]);
            w_cfg(R_H,    H[31:0]);
            w_cfg(R_W,    W[31:0]);
            if (unit_sel == 0) begin
                w_cfg(R_COUT, COUT[31:0]);
                w_cfg(R_K,    K[31:0]);
                w_cfg(R_S,    S[31:0]);
                w_cfg(R_D,    D[31:0]);
                w_cfg(R_PAD,  PAD[31:0]);
                w_cfg(B_IN_,    B_IN);
                w_cfg(B_WGT_,   B_WGT);
                w_cfg(B_ALPHA_, B_ALPHA);
                w_cfg(B_BIAS_,  B_BIAS);
                w_cfg(B_ZETA_,  B_ZETA);
                w_cfg(B_GAMMA_, B_GAMMA);
                w_cfg(B_SLOPE_, B_SLOPE);
            end else if (unit_sel == 1) begin
                w_cfg(B_IN_,    B_IN);
                w_cfg(B_ZETA_,  B_ZETA);
                w_cfg(B_GAMMA_, B_GAMMA);
                w_cfg(B_SLOPE_, B_SLOPE);
            end else if (unit_sel == 2) begin
                w_cfg(B_IN_,  B_IN);
                w_cfg(B_IN2_, B_IN2);
                w_cfg(B_IN3_, B_IN3);
                w_cfg(B_IN4_, B_IN4);
                w_cfg(B_WGT_, B_WGT);
                w_cfg(B_W2_,  B_W2);
                w_cfg(B_W3_,  B_W3);
            end else begin
                w_cfg(R_HO, HO[31:0]);
                w_cfg(R_WO, WO[31:0]);
                w_cfg(B_IN_, B_IN);
            end
            w_cfg(R_PF_EN, pfen[31:0]);     // 预取开关（旧实现忽略）
            w_cfg(R_SHIFT, SHIFT[31:0]);
            w_cfg(R_ASH,   32'd9);          // 契约 §1 的固定值
            w_cfg(R_SSH,   32'd8);
            w_cfg(B_OUT_,  B_OUT);
        end
    endtask

    // =========================================================================
    // start + 等 done（done 直接看 dut.done_sticky，省掉 MMIO 轮询开销）
    // =========================================================================
    task automatic run_and_wait(output [31:0] ret, output int ncyc, input int max_cyc);
        logic seen;
        begin
            w_cfg(R_AP_CTRL, 32'd1);        // start（bit0 写 1）
            ncyc = 0;
            seen = 1'b0;
            while ((ncyc < max_cyc) && !seen) begin
                @(posedge clk);
                ncyc = ncyc + 1;
                if (dut.done_sticky === 1'b1) seen = 1'b1;
            end
            r_cfg(R_AP_RET, ret);
            if (!seen) begin
                $display("[TB] 超时：%0d 拍内没等到 done，AP_RETURN=0x%08h", max_cyc, ret);
                // 只探测两版 RTL 都存在的稳定信号（读侧内部信号随重构会改名，
                // 不要把 tb 绑死在某一个实现的内部结构上）
                $display("[TB]   dump: run_active=%b u_ce=%b rd_want=%b rd_hit=%b u_rd_en=%b",
                         dut.run_active, dut.u_ce, dut.rd_want, dut.rd_hit, dut.u_rd_en);
                $display("[TB]   dump: u_rd_addr=0x%08h done_sticky=%b u_done=%b u_busy=%b",
                         dut.u_rd_addr, dut.done_sticky, dut.u_done, dut.u_busy);
                $display("[TB]   写侧: wst=%0d be_open=%b bu_len=%0d bf=%0d df=%0d axst=%0d",
                         dut.wst, dut.be_open, dut.bu_len, dut.bf_cnt, dut.df_cnt, dut.axst);
                n_fail = n_fail + 1;
            end else begin
                $display("[TB] done 在 %0d 拍后到达（AP_RETURN=0x%08h）", ncyc, ret);
            end
        end
    endtask

    // =========================================================================
    // 主流程
    // =========================================================================
    int cyc, total, nbad, n_x, n_over;
    int t94fill;            // ★ v94：1 = 预铺 0xA5（区分"写过/没写"）
    int oobwin;             // ★ v94：尾部越界检查窗口（默认 8192 B）
    int o, a, mi, y, x, lane, oc;
    int bad_per_oc [0:255];
    logic [7:0] gb, eb;
    logic [31:0] v, ret;
    int max_cycle;
    int nb_dummy;
    int seed;
    int selftest_en;
    int nostop;
    int cbx, shown;

    initial begin
        l_awvalid = 0; l_wvalid = 0; l_bready = 0; l_arvalid = 0; l_rready = 0;
        l_awaddr = 0; l_wdata = 0; l_wstrb = 0; l_araddr = 0;
        selftest = 1'b0;
        st_arvalid = 1'b0; st_araddr = 32'd0; st_arlen = 4'd0; st_arid = {AXIW{1'b0}};

        for (mi = 0; mi < MAXM; mi = mi + 1) mem[mi] = 8'h00;
        for (mi = 0; mi < 256; mi = mi + 1) bad_per_oc[mi] = 0;

        if (!$value$plusargs("GOLDEN=%s", gdir))
            $fatal(1, "[TB] 缺少 +GOLDEN=<dir>");
        if (!$value$plusargs("SHAPE_NAME=%s", shape_name))
            $fatal(1, "[TB] 缺少 +SHAPE_NAME=<name>");
        if (!$value$plusargs("RLMIN=%d", rlmin)) rlmin = 1;
        if (!$value$plusargs("RLMAX=%d", rlmax)) rlmax = 20;
        if (!$value$plusargs("ABP=%d",   abp_en)) abp_en = 1;
        if (!$value$plusargs("WBP=%d",   wbp_en)) wbp_en = 1;
        if (!$value$plusargs("WSTALLN=%d", wstalln)) wstalln = 0;
        if (!$value$plusargs("ROOO=%d",  rooo))   rooo = 1;
        if (!$value$plusargs("SEED=%d",  seed))   seed = 32'h1234_5678;
        // ⚠ 别名不能写成两个独立的 if：第二个的 else 分支会把第一个解析到的值覆盖掉
        //   （这里踩过一次：+MAXOUTSTANDING=1 被后面的 default 复位成 16）。
        if ($value$plusargs("MAXOUTSTANDING=%d", maxout)) begin end
        else if (!$value$plusargs("MAXOUT=%d", maxout)) maxout = 16;
        if (!$value$plusargs("RBURST_EXTRA=%d", rbext)) rbext = 0;
        if (!$value$plusargs("PFEN=%d", pfen)) pfen = 0;
        if (maxout < 1) maxout = 1;
        if (!$value$plusargs("SELFTEST=%d", selftest_en)) selftest_en = 1;
        if (!$value$plusargs("NOSTOP=%d", nostop)) nostop = 0;
        if (!$value$plusargs("MAXCYC=%d", max_cycle)) max_cycle = 20_000_000;
        if (!$value$plusargs("WBM=%d",   wbm)) wbm = 8;
        // ★ v94
        t94_n = 0; t94fd = 0;
        if ($value$plusargs("T94LOG=%s", t94file)) begin
            t94fd = $fopen(t94file, "w");
            m_fd  = $fopen({t94file, ".cap"}, "w");
        end
        if (!$value$plusargs("T94FILL=%d", t94fill)) t94fill = 0;
        if (!$value$plusargs("OOBWIN=%d",  oobwin))  oobwin = 8192;

        lfsr_r = seed;
        lfsr_w = seed ^ 32'hA5A5_5A5A;

        set_shape(shape_name);
        $display("[TB] =========================================================");
        $display("[TB] unit=%0d shape=%0s C=%0d COUT=%0d K=%0d S=%0d D=%0d PAD=%0d H=%0d W=%0d SHIFT=%0d",
                 UNIT_, shape_name, C, COUT, K, S, D, PAD, H, W, SHIFT);
        $display("[TB] 输出: NCH=%0d HOUT=%0d WOUT=%0d  => %0d 字节（C8 交错布局）",
                 NCH, HOUT, WOUT, NCH*HOUT*WOUT);
        if (UNIT_ == 0) begin
            cbx = (C + 63) / 64;
            $display("[TB] conv: C_B=%0d 每像素 term 数 K=C_B*k^2=%0d，写口是 1 字节/term 的模式(b)",
                     cbx, cbx*K*K);
        end
        $display("[TB] 读侧预取 R_PF_EN=%0d（0xF8；旧实现忽略该寄存器）", pfen);
        $display("[TB] AXI3 从模型: 读延迟 %0d..%0d 拍、burst 额外 %0d 拍、在飞上限 %0d、arready 反压=%0d、写反压=%0d、乱序=%0d、seed=0x%08h",
                 rlmin, rlmax, rbext, maxout, abp_en, wbp_en, rooo, seed);
        $display("[TB] DUT 参数: RD_WIN_WORDS=%0d AXI_ID_W=%0d WR_BUF_BEATS=%0d WR_BURST_MAX=%0d",
                 dut.RD_WIN_WORDS, dut.AXI_ID_W, dut.WR_BUF_BEATS, dut.WR_BURST_MAX);
        $display("[TB] =========================================================");

        // ------------------------------------------------------------------
        // 装黄金向量（T0 只读擦写区，互不影响）
        // ------------------------------------------------------------------
        $display("[TB] ---- 装载黄金向量（%0s）", gdir);
        begin
            if (UNIT_ == 0) begin
                load_file("act_pack.bin",    B_IN,    nb_dummy);
                load_file("wgt_pack.bin",    B_WGT,   nb_dummy);
                load_file("alpha.bin",       B_ALPHA, nb_dummy);
                load_file("bias.bin",        B_BIAS,  nb_dummy);
                load_file("zeta.bin",        B_ZETA,  nb_dummy);
                load_file("gamma.bin",       B_GAMMA, nb_dummy);
                load_file("slope.bin",       B_SLOPE, nb_dummy);
            end else if (UNIT_ == 1) begin
                load_file("in_c8.bin", B_IN,    nb_dummy);
                load_file("zeta.bin",  B_ZETA,  nb_dummy);
                load_file("gamma.bin", B_GAMMA, nb_dummy);
                load_file("slope.bin", B_SLOPE, nb_dummy);
            end else if (UNIT_ == 2) begin
                load_file("x_c8.bin",  B_IN,  nb_dummy);
                load_file("y0_c8.bin", B_IN2, nb_dummy);
                load_file("y2_c8.bin", B_IN3, nb_dummy);
                load_file("y4_c8.bin", B_IN4, nb_dummy);
                load_file("w0.bin",    B_WGT, nb_dummy);
                load_file("w2.bin",    B_W2,  nb_dummy);
                load_file("w4.bin",    B_W3,  nb_dummy);
            end else begin
                load_file("in_c8.bin", B_IN, nb_dummy);
            end
            load_file("expected_c8.bin", B_EXP, nb_exp);
        end
        total = NCH * HOUT * WOUT;
        if (nb_exp != total) begin
            $display("[TB] FAIL golden 与形状不符: expected_c8.bin=%0d 字节，形状要求 %0d 字节",
                     nb_exp, total);
            n_fail = n_fail + 1;
        end

        #100 rst_n = 1'b1;
        repeat (8) @(posedge clk);

        // ⚠ 先清调试计数器（DUT 的 cnt_burst/cnt_wbeat **不在 aresetn 复位链里**：
        //   它们只被 "+1" 和 "写 0xE8 的清零脉冲" 驱动 ⇒ 仿真起点是 X，X+1 还是 X。
        //   板上上电是 0，所以这不是功能 bug，但仿真里不先清就会读到 X。
        //   顺手把这条"写 0 清零"的路径也覆盖掉。
        w_cfg(R_DBG_RD, 32'd0);
        w_cfg(R_DBG_WR, 32'd0);
        w_cfg(R_DBG_FLG, 32'd0);

        // ------------------------------------------------------------------
        // T0 从模型自检
        // ------------------------------------------------------------------
        if (selftest_en) axi_model_selftest();

        // ------------------------------------------------------------------
        // T1 Lite 寄存器读写回环
        // ------------------------------------------------------------------
        $display("[TB] ---- T1 AXI4-Lite 寄存器回环 ----");
        cfg_apply(UNIT_);
        r_cfg(R_C, v);     chk("T1 R_C   readback", v, C[31:0]);
        r_cfg(R_H, v);     chk("T1 R_H   readback", v, H[31:0]);
        r_cfg(R_W, v);     chk("T1 R_W   readback", v, W[31:0]);
        r_cfg(R_SHIFT, v); chk("T1 R_SHIFT readback", v, SHIFT[31:0]);
        r_cfg(R_ASH, v);   chk("T1 alpha_shift reset=9", v, 32'd9);
        r_cfg(R_SSH, v);   chk("T1 slope_shift reset=8", v, 32'd8);
        r_cfg(B_IN_, v);   chk("T1 B_IN readback", v, B_IN);
        r_cfg(R_AP_CTRL, v); chk("T1 AP_CTRL.bit0 selfclr", v & 32'h1, 32'd0);
        r_cfg(R_AP_RET, v);  chk("T1 done=0 before start", v & 32'h1, 32'd0);

        // ------------------------------------------------------------------
        // T2 契约 §7.3-②：C % 8 != 0 ⇒ ERR_LAYOUT(4)，且引擎不启动
        // ------------------------------------------------------------------
        $display("[TB] ---- T2 契约 §7.3-②：C%%8!=0 ⇒ ERR_LAYOUT(4)，引擎不许启动 ----");
        w_cfg(R_C, 32'd68);
        w_cfg(R_AP_CTRL, 32'd1);
        repeat (40) @(posedge clk);
        r_cfg(R_AP_RET, v);
        chk("T2 C%8!=0 -> ERR_LAYOUT(4)+done", v, 32'h0000_0401);
        if ((dut.u_rd_en === 1'b0) && (dut.u_wr_en === 1'b0)) begin
            $display("[TB] PASS T2 校验失败时单元未被启动");
            n_pass = n_pass + 1;
        end else begin
            $display("[TB] FAIL T2 校验失败但单元动了 (rd=%b wr=%b)", dut.u_rd_en, dut.u_wr_en);
            n_fail = n_fail + 1;
        end
        cfg_apply(UNIT_);                       // 复原

        // ------------------------------------------------------------------
        // T3 契约 §7.3-①：需要 64-bit 读的基址非 8 字节对齐 ⇒ ERR_ALIGN(5)
        //    UNIT=2 时这条正是"三组权重基址"本体
        // ------------------------------------------------------------------
        $display("[TB] ---- T3 契约 §7.3-①：基址非 8 字节对齐 ⇒ ERR_ALIGN(5) ----");
        if (UNIT_ == 2) w_cfg(B_W2_, B_W2 + 32'h2);
        else            w_cfg(B_IN_, B_IN + 32'h2);
        w_cfg(R_AP_CTRL, 32'd1);
        repeat (40) @(posedge clk);
        r_cfg(R_AP_RET, v);
        chk("T3 base not 8B aligned -> ERR_ALIGN(5)+done", v, 32'h0000_0501);
        cfg_apply(UNIT_);                       // 复原

        // ------------------------------------------------------------------
        // T4 基址为 0 ⇒ ERR_NULL(1)
        // ------------------------------------------------------------------
        $display("[TB] ---- T4 基址为 0 ⇒ ERR_NULL(1) ----");
        w_cfg(B_OUT_, 32'd0);
        w_cfg(R_AP_CTRL, 32'd1);
        repeat (40) @(posedge clk);
        r_cfg(R_AP_RET, v);
        chk("T4 base==0 -> ERR_NULL(1)+done", v, 32'h0000_0101);
        cfg_apply(UNIT_);                       // 复原

        // ------------------------------------------------------------------
        // T5 功能跑：start → 等 done → 逐字节比对
        // ------------------------------------------------------------------
        $display("[TB] ---- T5 功能跑：start → done → 逐字节比对 expected_c8.bin ----");
        cfg_apply(UNIT_);
        // ★ v94：用 0xA5 预铺 OUT 区（+ total + 8 KB），模拟板上探针
        //   ⇒ "写过的字节" vs "没写的字节" 可分；越界窗口扩到 8 KB（板上实测到 8184）
        if (t94fill) begin
            for (a = 0; a < total + 8192; a = a + 1) mem[B_OUT + a] = 8'hA5;
            $display("[TB] v94: OUT 区预铺 0xA5（0..%0d）", total + 8192);
        end
        run_and_wait(ret, cyc, max_cycle);
        chk("T5 AP_RETURN(done=1,err=0)", ret, 32'h0000_0001);

        // ---- 逐字节比对（C8 交错布局；按 (oc,y,x) 遍历，方便定位）----
        nbad = 0; n_x = 0;
        for (o = 0; o < NCH; o = o + 1)
            for (y = 0; y < HOUT; y = y + 1)
                for (x = 0; x < WOUT; x = x + 1) begin
                    a = (o / 8) * HOUT * WOUT * 8 + y * WOUT * 8 + x * 8 + (o % 8);
                    gb = mem[B_OUT + a];
                    eb = mem[B_EXP + a];
                    if ((^gb) === 1'bx) n_x = n_x + 1;
                    if (gb !== eb) begin
                        if (nbad < 8)
                            $display("    差异 addr=0x%08h oc=%0d y=%0d x=%0d (pixel=%0d, block=%0d lane=%0d): got=%0d exp=%0d",
                                     B_OUT + a, o, y, x, y*WOUT + x, o/8, o%8,
                                     $signed(gb), $signed(eb));
                        nbad = nbad + 1;
                        if (o < 256) bad_per_oc[o] = bad_per_oc[o] + 1;
                    end
                end
        $display("[TB] 逐字节一致 : %0d / %0d 字节（X/Z=%0d）", total - nbad, total, n_x);
        if (nbad == 0 && n_x == 0) begin
            n_pass = n_pass + 1;
        end else begin
            // ---- 分组诊断：哪些 oc 挂了、挂了几个像素 ----
            $display("[TB] FAIL 逐字节不一致 %0d 字节；逐 oc 分布（最多列 8 个）：", nbad);
            begin
                shown = 0;
                for (o = 0; o < NCH && shown < 8; o = o + 1)
                    if (bad_per_oc[o] != 0) begin
                        $display("      oc=%0d 挂了 %0d/%0d 个像素", o, bad_per_oc[o], HOUT*WOUT);
                        shown = shown + 1;
                    end
            end
            $display("[TB] 提示：整块/整线错 ⇒ 先查读线槽号/tag；零星错 ⇒ 先查写侧 lane/WSTRB");
            n_fail = n_fail + 1;
        end

        // ---- 输出区尾部越界检查（写口把范围写飞时会在这里露）----
        //   ★ v94：窗口 256 → oobwin（默认 8192，与板上探针一致）；
        //           t94fill=1 时"未写"的哨兵是 0xA5 而不是 0。
        n_over = 0;
        for (a = total; a < total + oobwin; a = a + 1)
            if (mem[B_OUT + a] !== ((t94fill) ? 8'hA5 : 8'h00)) begin
                if (n_over < 6)
                    $display("[TB] ERROR: 输出区越界写：off=%0d (0x%08h) = %0d",
                             a, B_OUT + a, mem[B_OUT + a]);
                n_over = n_over + 1;
            end
        if (n_over != 0) begin
            $display("[TB] FAIL 输出区尾部（%0d..%0d）被写过 %0d 字节（窗口 %0d B）",
                     total, total+oobwin-1, n_over, oobwin);
            n_fail = n_fail + 1;
        end

        // ---- 输出区落盘（+DUMPFILE=<f>）----------------------------------
        //  T-38 回灌（2026-11-02）：做"改前 vs 改后"的**逐字节对拷**时，仓内 golden
        //  只覆盖 12 个形状，探针形状没有权威参考 ⇒ 只能 ref 落盘 / new 落盘 互拷。
        //  默认**不开**（不给 +DUMPFILE 就完全不执行），所以对既有 T0~T6 判据与
        //  拍数零影响（T-38 §2.1 的 24 组 0 差就是这么得到的）。
        //  落盘格式：每行一个字节（`%02x`），顺序 = mem[B_OUT .. B_OUT+total-1]。
        begin : t38_dump_blk
            string dfile_; int fd_; int dd_;
            if ($value$plusargs("DUMPFILE=%s", dfile_)) begin
                fd_ = $fopen(dfile_, "w");
                if (fd_ == 0) $display("[TB] WARN DUMPFILE 打不开 %0s", dfile_);
                else begin
                    for (dd_ = 0; dd_ < total; dd_ = dd_ + 1)
                        $fwrite(fd_, "%02x\n", mem[B_OUT + dd_]);
                    $fclose(fd_);
                    $display("[TB] DUMP 落盘 %0s bytes=%0d", dfile_, total);
                end
            end
        end

        // ------------------------------------------------------------------
        // T6 不变量 / 覆盖统计
        // ------------------------------------------------------------------
        repeat (20) @(posedge clk);
        chk("T6 AXI protocol errors", axi_errors, 0);
        chk("T6 per-cycle read-data errors", n_rd_bad, 0);
        // 在飞笔数是**实现相关**的，不作为判据：
        //   arlen=0 单线窗口：一条线 = 8 笔单拍 AR 同时挂上 ⇒ max_rq = 8
        //   arlen=7 + 多线 CAM：一次未命中只有 1 笔需求能触发 ⇒ max_rq = 1
        // 两者都正确。真正的性能判据是 DBG_RD 的「填线/需求读」（越大越好）。
        chk_ge("T6 max outstanding reads >= 1", max_rq, 1);
        // DBG_FLAG: [0]=高字写过非 0 [1]=读到非 8 字节对齐读 [2]=RID 越界 [3]=busy
        r_cfg(R_DBG_FLG, v);
        $display("[TB] DBG_FLAG=0x%08h (hi_bad=%0d misalign=%0d rid_bad=%0d busy=%0d)",
                 v, v[0], v[1], v[2], v[3]);
        chk("T6 flg_rd_misalign=0", v[1], 32'd0);
        chk("T6 flg_rid_bad=0",      v[2], 32'd0);
        if (rq_free_cnt != RQ) begin
            $display("[TB] FAIL 读请求表泄漏: 还剩 %0d 项未回收", RQ - rq_free_cnt);
            n_fail = n_fail + 1;
        end

        $display("[TB] 统计: AR=%0d(%0d 为 burst) R拍=%0d AW=%0d W拍=%0d 最大在飞=%0d 最大arlen=%0d",
                 d_ar, d_rburst, d_rbeat, n_aw, n_wbeat, max_rq, d_maxalen);
        if (d_maxalen == 0)
            $display("[TB] 读侧形态: 全部单拍 AR（arlen=0）—— 与当前 master 的读侧一致");
        else
            $display("[TB] 读侧形态: 出现了 %0d 笔 burst，最大 arlen=%0d（%0d 拍/笔）",
                     d_rburst, d_maxalen, d_maxalen + 1);
        $display("[TB] 覆盖: arready 反压 %0d 次 / awready 反压 %0d 次 / 读延迟 %0d..%0d 拍 / 拍数=%0d",
                 n_arbp, n_awbp, rlmin, rlmax, cyc);
        r_cfg(R_DBG_RD, v);
        $display("[TB] DBG_RD=0x%08h (需求读=%0d 填线=%0d)", v, v[15:0], v[31:16]);
        r_cfg(R_DBG_WR, v);
        $display("[TB] DBG_WR=0x%08h (AW笔数=%0d W拍数=%0d)  => 平均 %0d.%02d 拍/AW",
                 v, v[15:0], v[31:16],
                 (v[15:0] == 0) ? 0 : (v[31:16] / v[15:0]),
                 (v[15:0] == 0) ? 0 : ((v[31:16] * 100 / v[15:0]) % 100));
        if (n_aw != 0)
            $display("[TB] 写侧: 从机侧 AW=%0d / W拍=%0d => %0d.%02d 拍/AW（1.00 = 攒包退化成 1 字节/笔）",
                     n_aw, n_wbeat, n_wbeat / n_aw, ((n_wbeat * 100 / n_aw) % 100));

        $display("=========================================================");
        $display("[TB-v97] FIFO: peak_bf=%0d peak_df=%0d / press 拍=%0d / 写被压力挡=%0d 拍 / wr_split_busy 停核=%0d 拍 / run_active 拍=%0d / done-早报窗口=%0d 拍",
                 v97_bf_peak, v97_df_peak, v97_press_cyc, v97_press_blk, v97_split_blk, v97_run_cyc,
                 v97_done_early);
        $display("[TB-v94] 串行化探针: core_wr=%0d (c_wr_en 拍) / uce=1 捕获=%0d / cw_beat_en 拍=%0d / p1_v 拍=%0d / c_wr_en&!u_ce=%0d / press 拍=%0d",
                 m_core_wr, m_go, m_cen, m_beat, m_ucelow, m_press);
        $display("[TB-SUMMARY] module=bin_unit_axi_wrap unit=%0d shape=%0s bytes=%0d/%0d cases=%0d/%0d cycles=%0d max_rq=%0d max_arlen=%0d axi_errors=%0d rd_bad=%0d unknown=%0d status=%0s",
                 UNIT_, shape_name, total - nbad, total, n_pass, n_pass + n_fail, cyc,
                 max_rq, d_maxalen, axi_errors, n_rd_bad, n_x,
                 ((nbad == 0) && (n_fail == 0) && (axi_errors == 0) && (n_rd_bad == 0)) ? "PASS" : "FAIL");
        $display("[TB] PASS=%0d FAIL=%0d", n_pass, n_fail);
        if ((nbad != 0) || (n_fail != 0) || (axi_errors != 0) || (n_rd_bad != 0)) begin
            if (!nostop) $fatal(1, "[TB] FAIL");
        end
        $display("=========================================================");
        if (t94fd != 0) begin
            $fclose(t94fd);
            t94fd = 0;
            $display("[TB] v94: 逐笔写记录已落盘 %0s（%0d 拍）", t94file, t94_n);
            if (m_fd != 0) begin $fclose(m_fd); m_fd = 0; end
        end
        $finish;
    end

    // 看门狗（防死锁挂死主机）
    initial begin
        #2_000_000_000;
        $display("[TB] TIMEOUT（%0d ns）", $time);
        $fatal(1, "[TB] TIMEOUT");
    end

endmodule
