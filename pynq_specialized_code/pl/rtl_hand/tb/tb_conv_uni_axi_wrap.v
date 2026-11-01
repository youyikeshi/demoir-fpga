// ============================================================================
//  tb_conv_uni_axi_wrap.v —— conv_uni_axi_wrap 的自测 TB
//
//  为什么必须单独有这个 TB：conv_int8_uni 自己的 TB（tb_conv_int8_uni.v）
//  把内存接成"组合地址的 1 拍 BRAM"，**完全覆盖不到**包装层承担的五件事：
//    ① 把引擎的 1 拍延迟 BRAM 口翻译成几十拍往返的 AXI（延迟不匹配 ⇒ 数据错）
//    ② 读侧多笔在飞 + ARID 分槽（乱序返回时必须靠 RID 落槽）
//    ③ **4 字节对齐的权重读要横跨两个 8 字节字**（本层与 bin_unit 的唯一实质差异）
//    ④ 写侧 8-bit → 64-bit beat → INCR burst（地址/lane/4 KB 边界/尾部排空）
//    ⑤ 启动前校验（ERR_RANGE/TAP/WCAP/ALIGN/ADDR）拒绝启动且置 done
//
//  覆盖：
//    T1  AXI4-Lite 寄存器读写回环（两页式基址、AP_CTRL 自清、AP_RETURN done=0）
//    T2  七类非法配置 ⇒ AP_RETURN[15:8] 对应错误码、引擎未被启动、done 直接置 1
//    T3  功能跑：装黄金向量 → 写寄存器 → start → 等 done → **逐字节**比对
//        expected_c8.bin（覆盖 1 拍延迟、多笔在飞、乱序 RID 落槽、跨字读、写 burst）
//    T4  统计与不变量：AR 永远 8 字节对齐、arlen=0、AW 对齐、burst 不跨 4 KB、
//        WLAST 位置、最大在飞笔数 ≥ 2、**确实发生过 4 字节对齐需求读**（跨字覆盖）
//
//  编译/运行（形状全部用 plusarg 给，不需要 -D 宏；每个 golden 目录跑一次）：
//      xvlog --sv -L xpm modules/conv_int8_uni.v modules/conv_uni_axi_wrap.v \
//            tb/tb_conv_uni_axi_wrap.v
//      xelab -debug typical tb_conv_uni_axi_wrap -s snap
//      xsim snap -R +GOLDEN=golden/uni_k3_64x12_8x8 +CIN=64 +COUT=12 +K=3 +S=1 \
//                +D=1 +PAD=1 +H=8 +W=8 +SHIFT=7 +RELU=1 +TAP=12
//  或（iverilog 自检）：
//      iverilog -g2012 -o tb.vvp conv_int8_uni.v conv_uni_axi_wrap.v tb_conv_uni_axi_wrap.v
//      vvp tb.vvp +GOLDEN=... +CIN=... ...
//
//  plusarg：
//    +GOLDEN=<dir>  +CIN= +COUT= +K= +S= +D= +PAD= +H= +W= +SHIFT= +RELU= +TAP=
//    +RLAT=<n>      AXI 读从机基础延迟（默认 4，实际 3..7 抖动）
//    +ROOO=1        R 通道"后发先回"（默认 0 = 近似按序），用来压 RID 分槽逻辑
//    +MAXCYC=<n>    看门狗拍数（默认 40_000_000）
//  ⚠ 本 TB 只判"功能/协议"，不判拍数：包装层拍数取决于访存局部性（每条 64 B
//    线一次往返），要评估性能请读 DBG_RD/DBG_AR 两个调试寄存器（本 TB 会打印）。
// ============================================================================
`timescale 1ns / 1ps

// DUT 参数从 TB 参数传下去：换 RD_WIN_WORDS / 写缓冲深度时只要重编一次 TB
// （iverilog: -Ptb_conv_uni_axi_wrap.WINW=4；xelab: --generic_top "WINW=4"）
module tb_conv_uni_axi_wrap #(
    parameter int WINW  = 8,            // = DUT.RD_WIN_WORDS
    parameter int NLINE = 64,           // = DUT.NLINES
    parameter int AXIW  = 6,            // = DUT.AXI_ID_W
    parameter int PFMAX = 32,           // = DUT.PF_MAX
    parameter int WBF   = 16,           // = DUT.WR_BUF_BEATS
    parameter int WBM   = 16            // = DUT.WR_BURST_MAX
);

    // ---- 内存模型（4 MB 字节数组；各区域都 8 字节对齐）----
    localparam int MAXM = 1 << 22;
    localparam int NSLOT = WINW + 1;
    logic [7:0] mem [0:MAXM-1];

    localparam int B_IN   = 32'h0001_0000;
    localparam int B_WGT  = 32'h0010_0000;
    localparam int B_BIAS = 32'h0018_0000;
    localparam int B_OUT  = 32'h001C_0000;
    localparam int B_EXP  = 32'h0028_0000;

    // ---- 配置页偏移（A[16]=1）----
    localparam [15:0] R_AP_CTRL = 16'h00, R_AP_RET = 16'h10;
    localparam [15:0] R_H  = 16'h18, R_W = 16'h20;
    localparam [15:0] R_CIN = 16'h38, R_COUT = 16'h40;
    localparam [15:0] R_K  = 16'h48, R_S = 16'h50, R_D = 16'h58, R_PAD = 16'h60;
    localparam [15:0] R_SHIFT = 16'h68, R_RELU = 16'h70, R_TAP = 16'h74;
    localparam [15:0] R_STATUS = 16'h94;
    localparam [15:0] R_DBG_RD = 16'hE0, R_DBG_AR = 16'hE4, R_DBG_WR = 16'hE8;
    // ---- 基址页偏移（A[16]=0，与 k1_axi_wrap / HLS 默认 bundle 一致）----
    localparam [15:0] B_IN_ = 16'h10, B_WGT_ = 16'h1C, B_BIAS_ = 16'h28, B_OUT_ = 16'h34;

    // =========================================================================
    // 时钟 / 复位
    // =========================================================================
    logic clk = 1'b0;
    logic rst_n = 1'b0;
    always #5 clk = ~clk;

    // =========================================================================
    // DUT 端口
    // =========================================================================
    logic [31:0] awaddr;   logic awvalid, awready;
    logic [31:0] wdata;    logic [3:0] wstrb; logic wvalid, wready;
    logic [1:0]  bresp;    logic bvalid, bready;
    logic [31:0] araddr;   logic arvalid, arready;
    logic [31:0] rdata;    logic [1:0] rresp; logic rvalid, rready;

    logic        rd_arvalid, rd_arready;
    logic [31:0] rd_araddr;
    logic [3:0]  rd_arlen;   // AXI3: 4 bit
    logic [AXIW-1:0] rd_arid;
    logic        rd_rvalid, rd_rready;
    logic [63:0] rd_rdata;
    logic        rd_rlast;
    logic [AXIW-1:0] rd_rid;

    logic        wr_awvalid, wr_awready;
    logic [31:0] wr_awaddr;
    logic [3:0]  wr_awlen;   // AXI3: 4 bit
    logic        wr_wvalid, wr_wready;
    logic [63:0] wr_wdata;
    logic [7:0]  wr_wstrb;
    logic        wr_wlast;
    logic        wr_bvalid, wr_bready;

    conv_uni_axi_wrap #(
        .RD_WIN_WORDS (WINW),
        .NLINES       (NLINE),
        .AXI_ID_W     (AXIW),
        .PF_MAX       (PFMAX),
        .WR_BUF_BEATS (WBF),
        .WR_BURST_MAX (WBM),
        // ★ 仿真固定用 latch+AND 门控时钟（USE_BUFGCE=0）：
        //   BUFGCE 是 Xilinx 原语，xelab 需要 glbl/unisims_ver 才能展开，
        //   而两种门控方式**功能完全等价**（都是 u_ce 有效时引擎才推进），
        //   所以仿真侧没必要为它引入原语依赖。综合侧用默认值（=1，BUFGCE）。
        .USE_BUFGCE   (1'b0)
    ) dut (
        .s_axi_aclk      (clk),
        .s_axi_aresetn   (rst_n),
        .s_axi_awaddr    (awaddr),  .s_axi_awvalid (awvalid), .s_axi_awready (awready),
        .s_axi_wdata     (wdata),   .s_axi_wstrb   (wstrb),
        .s_axi_wvalid    (wvalid),  .s_axi_wready  (wready),
        .s_axi_bresp     (bresp),   .s_axi_bvalid  (bvalid),  .s_axi_bready  (bready),
        .s_axi_araddr    (araddr),  .s_axi_arvalid (arvalid), .s_axi_arready (arready),
        .s_axi_rdata     (rdata),   .s_axi_rresp   (rresp),
        .s_axi_rvalid    (rvalid),  .s_axi_rready  (rready),
        .m_axi_arvalid(rd_arvalid), .m_axi_arready(rd_arready),
        .m_axi_araddr (rd_araddr),  .m_axi_arlen  (rd_arlen),
        .m_axi_arid   (rd_arid),
        .m_axi_rvalid (rd_rvalid),  .m_axi_rready (rd_rready),
        .m_axi_rdata  (rd_rdata),   .m_axi_rlast  (rd_rlast),
        .m_axi_rid    (rd_rid),
        .m_axi_awvalid(wr_awvalid), .m_axi_awready(wr_awready),
        .m_axi_awaddr (wr_awaddr),  .m_axi_awlen  (wr_awlen),
        .m_axi_wvalid (wr_wvalid),  .m_axi_wready (wr_wready),
        .m_axi_wdata  (wr_wdata),   .m_axi_wstrb  (wr_wstrb),
        .m_axi_wlast  (wr_wlast),
        .m_axi_bvalid (wr_bvalid),  .m_axi_bready (wr_bready)
    );

    // =========================================================================
    // 黄金向量目录 / 统计 / 判定
    // =========================================================================
    string gdir;
    int n_pass = 0, n_fail = 0;
    int axi_errors = 0;
    int n_ar = 0, n_rbeat = 0, n_aw = 0, n_wbeat = 0;
    int max_rq = 0;                 // 实测最大在飞读笔数
    int n_misaln = 0;               // "4 字节对齐的需求读"次数（跨字路径覆盖）
    int n_misaln_word = 0;          // 其中落在线的最后一个槽（必须靠前瞻槽）

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

    // =========================================================================
    // AXI4-Lite 主机
    // =========================================================================
    task automatic lite_wr(input [31:0] addr, input [31:0] data);
        begin
            @(posedge clk);
            awaddr <= addr; awvalid <= 1'b1;
            wdata  <= data; wstrb <= 4'hF; wvalid <= 1'b1;
            bready <= 1'b1;
            fork
                begin
                    @(posedge clk);
                    while (!awready) @(posedge clk);
                    awvalid <= 1'b0;
                end
                begin
                    @(posedge clk);
                    while (!wready) @(posedge clk);
                    wvalid <= 1'b0;
                end
            join
            while (!bvalid) @(posedge clk);
            @(posedge clk);
            bready <= 1'b0;
        end
    endtask

    task automatic lite_rd(input [31:0] addr, output [31:0] data);
        begin
            @(posedge clk);
            araddr <= addr; arvalid <= 1'b1; rready <= 1'b1;
            @(posedge clk);
            while (!arready) @(posedge clk);
            arvalid <= 1'b0;
            while (!rvalid) @(posedge clk);
            data = rdata;
            @(posedge clk);
            rready <= 1'b0;
        end
    endtask

    task automatic w_cfg(input [15:0] off, input [31:0] d);
        lite_wr({15'd0, 1'b1, off}, d);
    endtask
    task automatic r_cfg(input [15:0] off, output [31:0] d);
        lite_rd({15'd0, 1'b1, off}, d);
    endtask
    task automatic w_base(input [15:0] off, input [31:0] d);
        lite_wr({16'd0, off}, d);
    endtask
    task automatic r_base(input [15:0] off, output [31:0] d);
        lite_rd({16'd0, off}, d);
    endtask

    // =========================================================================
    // AXI4 读从机：**多笔在飞 + 跨 ID 乱序返回**
    //   · arready 在表未满时恒高；arlen 必须是 0（包装层只发单拍）
    //   · 每条请求延迟 rlat-1 .. rlat+3 拍，谁先到点谁先回
    //   · 同一 ID 的多笔必须保序（AXI 要求）—— 选择器里显式检查
    //   · +ROOO=1 时优先返回"最新的" ⇒ 强制乱序，压 RID 分槽
    // =========================================================================
    localparam int RQ = 32;
    logic [31:0] rq_addr [0:RQ-1];
    logic [AXIW-1:0] rq_id   [0:RQ-1];
    // ⚠ 同 ID 保序必须按 **AR 序号** 判，不能按槽号判：槽位会复用，复用后低槽号
    //   可能是后发的 AR（本 TB 自测时正是这里出了错 —— 会让从机把同一个 ID 的
    //   两个 AR 的顺序倒过来，而 AXI 明确要求同 ID 按 AR 顺序返回）。
    logic [31:0]     rq_seq  [0:RQ-1];
    logic [31:0]     seq_cnt;
    int          rq_dly  [0:RQ-1];
    logic        rq_v    [0:RQ-1];
    int          rq_used = 0;
    int          rq_free_cnt;
    int          rlat = 4;
    int          rooo = 0;

    // ⚠ 每个并发进程用**独立**的循环变量：共用一个 integer 会让 always @* 与
    //   always @(posedge) 的 for 循环互相打断（本 TB 自测时就踩过这个坑）。
    integer ri_c;                 // always @*（空闲槽计数）
    integer ri_a, rslot;          // AR 接收
    integer ri_d;                 // 延迟递减
    integer ri_s, rj_s, sel;      // 返回选择
    logic   legal;

    always @* begin
        rq_free_cnt = 0;
        for (ri_c = 0; ri_c < RQ; ri_c = ri_c + 1)
            if (!rq_v[ri_c]) rq_free_cnt = rq_free_cnt + 1;
    end

    assign rd_arready = (rq_free_cnt > 0);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rd_rvalid <= 1'b0;
            rd_rdata  <= 64'd0;
            rd_rlast  <= 1'b0;
            rd_rid    <= {AXIW{1'b0}};
            rq_used   = 0;
            seq_cnt   = 0;
            for (ri_a = 0; ri_a < RQ; ri_a = ri_a + 1) begin
                rq_v[ri_a]   <= 1'b0;
                rq_dly[ri_a] <= 0;
                rq_seq[ri_a] <= 32'd0;
            end
        end else begin
            // ---- 1) 收 AR ----
            if (rd_arvalid && rd_arready) begin
                rslot = -1;
                for (ri_a = 0; ri_a < RQ; ri_a = ri_a + 1)
                    if (!rq_v[ri_a] && (rslot < 0)) rslot = ri_a;
                if (rslot >= 0) begin
                    rq_v[rslot]    <= 1'b1;
                    rq_addr[rslot] <= rd_araddr;
                    rq_id[rslot]   <= rd_arid;
                    rq_seq[rslot]  <= seq_cnt;
                    seq_cnt        = seq_cnt + 1;
                    rq_dly[rslot]  <= rlat + ((rslot * 5 + rq_used) % 5) - 1;
                    rq_used        = rq_used + 1;
                    n_ar           = n_ar + 1;
                    if (rd_araddr[2:0] !== 3'd0) begin
                        $display("[TB-AXI] ERROR: ARADDR 未按 8 字节对齐 0x%08h", rd_araddr);
                        axi_errors = axi_errors + 1;
                    end
                    if (rd_arlen !== 4'd0) begin
                        $display("[TB-AXI] ERROR: 读应只发单拍 (arlen=%0d)", rd_arlen);
                        axi_errors = axi_errors + 1;
                    end
                    if (rd_arid >= NLINE) begin
                        $display("[TB-AXI] ERROR: ARID=%0d 超出缓存条目号范围(0..%0d)",
                                 rd_arid, NLINE-1);
                        axi_errors = axi_errors + 1;
                    end
                    if (rq_used + 1 > max_rq) max_rq = rq_used + 1;
                end
            end

            // ---- 2) 延迟递减 ----
            for (ri_d = 0; ri_d < RQ; ri_d = ri_d + 1)
                if (rq_v[ri_d] && (rq_dly[ri_d] > 0)) rq_dly[ri_d] <= rq_dly[ri_d] - 1;

            // ---- 3) 挑一条返回 ----
            sel = -1;
            if (rooo) begin
                for (ri_s = RQ - 1; ri_s >= 0; ri_s = ri_s - 1) begin
                    if ((sel < 0) && rq_v[ri_s] && (rq_dly[ri_s] <= 0)) begin
                        legal = 1'b1;
                        for (rj_s = 0; rj_s < RQ; rj_s = rj_s + 1)
                            if (rq_v[rj_s] && (rq_id[rj_s] == rq_id[ri_s]) &&
                                (rq_seq[rj_s] < rq_seq[ri_s])) legal = 1'b0;
                        if (legal) sel = ri_s;
                    end
                end
            end else begin
                for (ri_s = 0; ri_s < RQ; ri_s = ri_s + 1) begin
                    if ((sel < 0) && rq_v[ri_s] && (rq_dly[ri_s] <= 0)) begin
                        legal = 1'b1;
                        for (rj_s = 0; rj_s < RQ; rj_s = rj_s + 1)
                            if (rq_v[rj_s] && (rq_id[rj_s] == rq_id[ri_s]) &&
                                (rq_seq[rj_s] < rq_seq[ri_s])) legal = 1'b0;
                        if (legal) sel = ri_s;
                    end
                end
            end

            if ((sel >= 0) && (!rd_rvalid || rd_rready)) begin
                rd_rvalid <= 1'b1;
                rd_rid    <= rq_id[sel];
                rd_rlast  <= 1'b1;                       // arlen=0 ⇒ 每拍都是末拍
                rd_rdata  <= {mem[rq_addr[sel] + 7], mem[rq_addr[sel] + 6],
                              mem[rq_addr[sel] + 5], mem[rq_addr[sel] + 4],
                              mem[rq_addr[sel] + 3], mem[rq_addr[sel] + 2],
                              mem[rq_addr[sel] + 1], mem[rq_addr[sel] + 0]};
                rq_v[sel] <= 1'b0;
                rq_used   = rq_used - 1;
            end else if (rd_rvalid && rd_rready) begin
                rd_rvalid <= 1'b0;
            end
            if (rd_rvalid && rd_rready) n_rbeat = n_rbeat + 1;
        end
    end

    // =========================================================================
    // AXI4 写从机：一次一笔 burst（AW → W 拍 → B），按 WSTRB 落字节
    // =========================================================================
    localparam [1:0] WS_IDLE = 2'd0, WS_DATA = 2'd1, WS_RESP = 2'd2;
    logic [1:0]  wsl;
    logic [31:0] ws_addr;
    logic [7:0]  ws_len, ws_cnt;
    integer      bi;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wsl        <= WS_IDLE;
            wr_awready <= 1'b1;
            wr_wready  <= 1'b0;
            wr_bvalid  <= 1'b0;
            ws_addr    <= 32'd0;
            ws_len     <= 8'd0;
            ws_cnt     <= 8'd0;
        end else begin
            case (wsl)
            WS_IDLE: begin
                if (wr_awvalid && wr_awready) begin
                    ws_addr    <= wr_awaddr;
                    ws_len     <= wr_awlen;
                    ws_cnt     <= 8'd0;
                    wr_awready <= 1'b0;
                    wr_wready  <= 1'b1;
                    wsl        <= WS_DATA;
                    n_aw       = n_aw + 1;
                    // ---- 协议检查 ----
                    if (wr_awaddr[2:0] !== 3'd0) begin
                        $display("[TB-AXI] ERROR: AWADDR 未按 8 字节对齐 0x%08h", wr_awaddr);
                        axi_errors = axi_errors + 1;
                    end
                    if (wr_awlen >= WBM[7:0]) begin
                        $display("[TB-AXI] ERROR: AWLEN=%0d 超过 WR_BURST_MAX-1=%0d",
                                 wr_awlen, WBM-1);
                        axi_errors = axi_errors + 1;
                    end
                    if (({20'd0, wr_awaddr[11:0]} + ({3'b000, wr_awlen} * 8)) > 12'd4095) begin
                        $display("[TB-AXI] ERROR: 写 burst 跨 4 KB addr=0x%08h len=%0d",
                                 wr_awaddr, wr_awlen);
                        axi_errors = axi_errors + 1;
                    end
                    if (wr_awaddr < B_OUT || wr_awaddr >= B_EXP) begin
                        $display("[TB-AXI] ERROR: 写越界 addr=0x%08h（应在 B_OUT..B_EXP 之间）",
                                 wr_awaddr);
                        axi_errors = axi_errors + 1;
                    end
                end
            end
            WS_DATA: begin
                if (wr_wvalid && wr_wready) begin
                    for (bi = 0; bi < 8; bi = bi + 1)
                        if (wr_wstrb[bi])
                            mem[ws_addr + (ws_cnt * 8) + bi] = wr_wdata[bi*8 +: 8];
                    n_wbeat = n_wbeat + 1;
                    if (wr_wlast !== (ws_cnt == ws_len)) begin
                        $display("[TB-AXI] ERROR: WLAST 位置错 cnt=%0d len=%0d wlast=%0d",
                                 ws_cnt, ws_len, wr_wlast);
                        axi_errors = axi_errors + 1;
                    end
                    ws_cnt <= ws_cnt + 8'd1;
                    if (wr_wlast) begin
                        wr_wready <= 1'b0;
                        wr_bvalid <= 1'b1;
                        wsl       <= WS_RESP;
                    end
                end
            end
            WS_RESP: begin
                if (wr_bvalid && wr_bready) begin
                    wr_bvalid  <= 1'b0;
                    wr_awready <= 1'b1;
                    wsl        <= WS_IDLE;
                end
            end
            default: wsl <= WS_IDLE;
            endcase
        end
    end

    // =========================================================================
    // 读数据对拍：引擎 t 拍发 rd_en/addr、t+1 拍取 rd_data。这里把"被放行的那一拍"
    // 的地址记下来，下一拍拿 mem[addr..addr+7] 与 dut.u_rd_data 逐字节比 ——
    // 这是包装层最硬的 ground truth（缓存/对齐/CAM 任何错都会在这里露）。
    // =========================================================================
    logic [31:0] mon_addr_q;
    logic        mon_ce_q;
    int          n_rd_bad = 0;
    logic [63:0] mon_exp;

    always @(posedge clk) begin
        mon_addr_q <= dut.u_rd_addr;
        mon_ce_q   <= rst_n && dut.u_ce && dut.u_rd_en && dut.rd_hit;
    end

    always @(negedge clk) begin
        if (mon_ce_q) begin
            mon_exp = {mem[mon_addr_q + 7], mem[mon_addr_q + 6],
                       mem[mon_addr_q + 5], mem[mon_addr_q + 4],
                       mem[mon_addr_q + 3], mem[mon_addr_q + 2],
                       mem[mon_addr_q + 1], mem[mon_addr_q + 0]};
            if (dut.u_rd_data !== mon_exp) begin
                if (n_rd_bad < 8)
                    $display("[TB-RD] ERROR: 读数据不符 addr=0x%08h got=%016h exp=%016h",
                             mon_addr_q, dut.u_rd_data, mon_exp);
                n_rd_bad = n_rd_bad + 1;
            end
        end
    end

    // =========================================================================
    // 覆盖监视：引擎发来的"4 字节对齐需求读"（TAP=4/12/28 的权重装载必然出现）
    // =========================================================================
    logic [28:0] rd_word_d;
    always @(posedge clk) begin
        if (rst_n && dut.u_rd_en && (dut.u_rd_addr[2:0] != 3'd0)) begin
            n_misaln = n_misaln + 1;
            rd_word_d = dut.u_rd_addr[31:3];
            if (dut.u_rd_addr[2:0] == 3'd4 &&
                (rd_word_d[2:0] == (WINW - 1)))
                n_misaln_word = n_misaln_word + 1;      // 落在线的最后一槽 ⇒ 靠前瞻槽
        end
    end

    // =========================================================================
    // 黄金向量装载（不用 break / $feof：iverilog 11 不支持 break）
    // =========================================================================
    integer fh;
    int     nb_in, nb_wgt, nb_bias, nb_exp;

    task automatic load_file(input string fn, input int base, output int nbytes);
        integer c;
        begin
            fh = $fopen({gdir, "/", fn}, "rb");
            if (fh == 0) $fatal(1, "[TB] FAIL: 打不开 %s/%s（先跑 tools/gen_uni_golden.py --all）",
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
    // 形状 / 配置
    // =========================================================================
    int CIN, COUT, KK, SS, DD, PADD, HH, WW, SH, RELU, TAP;
    int Hout, Wout, co8, total;
    int max_cycle;

    task automatic cfg_apply;
        begin
            w_cfg(R_H,     HH);
            w_cfg(R_W,     WW);
            w_cfg(R_CIN,   CIN);
            w_cfg(R_COUT,  COUT);
            w_cfg(R_K,     KK);
            w_cfg(R_S,     SS);
            w_cfg(R_D,     DD);
            w_cfg(R_PAD,   PADD);
            w_cfg(R_SHIFT, SH);
            w_cfg(R_RELU,  RELU);
            w_cfg(R_TAP,   TAP);
            // 两页式基址：低字写在偏移处，高字写在 +4（本 TB 全 0）
            w_base(B_IN_,    B_IN);    w_base(B_IN_+4,   32'd0);
            w_base(B_WGT_,   B_WGT);   w_base(B_WGT_+4,  32'd0);
            w_base(B_BIAS_,  B_BIAS);  w_base(B_BIAS_+4, 32'd0);
            w_base(B_OUT_,   B_OUT);   w_base(B_OUT_+4,  32'd0);
        end
    endtask

    task automatic start_and_wait(output [31:0] ret, output int ncyc);
        logic seen;
        begin
            w_cfg(R_AP_CTRL, 32'd1);
            ncyc = 0;
            seen = 1'b0;
            while ((ncyc < max_cycle) && !seen) begin
                repeat (64) begin
                    @(posedge clk);
                    ncyc = ncyc + 1;
                end
                r_cfg(R_AP_RET, ret);
                if (ret[0] === 1'b1) seen = 1'b1;
            end
            if (!seen) begin
                $display("[TB] FAIL: %0d 拍内没等到 done，AP_RETURN=0x%08h", max_cycle, ret);
                n_fail = n_fail + 1;
            end else begin
                $display("[TB] done 在 ~%0d 拍后到达（AP_RETURN=0x%08h）", ncyc, ret);
            end
        end
    endtask

    // =========================================================================
    // 主流程
    // =========================================================================
    int mi, o, nbad, n_x, cyc;
    int got_i, exp_i;
    int arr_pm;
    logic [31:0] v, ret;

    initial begin
        awvalid = 0; wvalid = 0; bready = 0; arvalid = 0; rready = 0;
        awaddr = 0; wdata = 0; wstrb = 0; araddr = 0;

        for (mi = 0; mi < MAXM; mi = mi + 1) mem[mi] = 8'h00;

        if (!$value$plusargs("GOLDEN=%s", gdir))
            $fatal(1, "[TB] 缺少 +GOLDEN=<dir>");
        if (!$value$plusargs("CIN=%d", CIN))   $fatal(1, "[TB] 缺少 +CIN=");
        if (!$value$plusargs("COUT=%d", COUT)) $fatal(1, "[TB] 缺少 +COUT=");
        if (!$value$plusargs("K=%d", KK))      $fatal(1, "[TB] 缺少 +K=");
        if (!$value$plusargs("S=%d", SS))      SS = 1;
        if (!$value$plusargs("D=%d", DD))      DD = 1;
        if (!$value$plusargs("PAD=%d", PADD))  PADD = 0;
        if (!$value$plusargs("H=%d", HH))      $fatal(1, "[TB] 缺少 +H=");
        if (!$value$plusargs("W=%d", WW))      $fatal(1, "[TB] 缺少 +W=");
        if (!$value$plusargs("SHIFT=%d", SH))  $fatal(1, "[TB] 缺少 +SHIFT=");
        if (!$value$plusargs("RELU=%d", RELU)) RELU = 1;
        // TAP 合法值已从 {4,12,28} 改成 8·ceil(k²/8) = {8,16,32}（2026-10-30）
        if (!$value$plusargs("TAP=%d", TAP))   TAP = (KK == 1) ? 8 : ((KK == 3) ? 16 : 32);
        if (!$value$plusargs("RLAT=%d", rlat)) rlat = 4;
        if (!$value$plusargs("ROOO=%d", rooo)) rooo = 0;
        if (!$value$plusargs("MAXCYC=%d", max_cycle)) max_cycle = 40_000_000;

        Hout = (HH - 1) / SS + 1;
        Wout = (WW - 1) / SS + 1;
        co8  = ((COUT + 7) / 8) * 8;
        total = co8 * Hout * Wout;
        $display("[TB] shape=%0s cin=%0d cout=%0d k=%0d s=%0d d=%0d pad=%0d H=%0d W=%0d shift=%0d relu=%0d tap=%0d",
                 gdir, CIN, COUT, KK, SS, DD, PADD, HH, WW, SH, RELU, TAP);
        $display("[TB] Hout=%0d Wout=%0d co8=%0d 期望输出 %0d 字节（rlat=%0d rooo=%0d）",
                 Hout, Wout, co8, total, rlat, rooo);
        $display("[TB] 参数: WINW=%0d NLINE=%0d AXIW=%0d PFMAX=%0d WBF=%0d WBM=%0d",
                 WINW, NLINE, AXIW, PFMAX, WBF, WBM);
        $display("[TB] DUT : RD_WIN_WORDS=%0d NLINES=%0d AXI_ID_W=%0d PF_MAX=%0d",
                 dut.RD_WIN_WORDS, dut.NLINES, dut.AXI_ID_W, dut.PF_MAX);

        #100 rst_n = 1'b1;
        repeat (8) @(posedge clk);

        // ------------------------------------------------------------------
        // T1 Lite 寄存器读写回环
        // ------------------------------------------------------------------
        cfg_apply;
        r_cfg(R_CIN, v);   chk("T1 R_CIN 回读",   v, CIN);
        r_cfg(R_COUT, v);  chk("T1 R_COUT 回读",  v, COUT);
        r_cfg(R_K, v);     chk("T1 R_K 回读",     v, KK);
        r_cfg(R_S, v);     chk("T1 R_S 回读",     v, SS);
        r_cfg(R_D, v);     chk("T1 R_D 回读",     v, DD);
        r_cfg(R_PAD, v);   chk("T1 R_PAD 回读",   v, PADD);
        r_cfg(R_H, v);     chk("T1 R_H 回读",     v, HH);
        r_cfg(R_W, v);     chk("T1 R_W 回读",     v, WW);
        r_cfg(R_SHIFT, v); chk("T1 R_SHIFT 回读", v, SH);
        r_cfg(R_TAP, v);   chk("T1 R_TAP 回读",   v, TAP);
        r_base(B_IN_, v);  chk("T1 B_IN 回读",   v, B_IN);
        r_base(B_WGT_, v); chk("T1 B_WGT 回读",  v, B_WGT);
        r_base(B_BIAS_, v);chk("T1 B_BIAS 回读", v, B_BIAS);
        r_base(B_OUT_, v); chk("T1 B_OUT 回读",  v, B_OUT);
        r_cfg(R_AP_CTRL, v); chk("T1 AP_CTRL.bit0 自清", v & 32'h1, 32'd0);
        r_cfg(R_AP_RET, v);  chk("T1 未启动时 done=0",   v & 32'h1, 32'd0);

        // ------------------------------------------------------------------
        // T2 七类非法配置 ⇒ 错误码 + done，且引擎不启动
        //    期望值 = (err<<8) | 0x01
        // ------------------------------------------------------------------
        // T2a cin 超限 ⇒ ERR_RANGE(1)
        w_cfg(R_CIN, 32'd193);
        w_cfg(R_AP_CTRL, 32'd1);
        repeat (40) @(posedge clk);
        r_cfg(R_AP_RET, v);
        chk("T2a cin=193 > ERR_RANGE", v, 32'h0000_0101);
        if (dut.run_active === 1'b0 && dut.u_rd_en === 1'b0) begin
            $display("[TB] PASS T2a 校验失败时引擎未被启动"); n_pass = n_pass + 1;
        end else begin
            $display("[TB] FAIL T2a 校验失败但引擎启动/读内存了"); n_fail = n_fail + 1;
        end
        cfg_apply;

        // T2b cout 超限 ⇒ ERR_RANGE
        w_cfg(R_COUT, 32'd65);
        w_cfg(R_AP_CTRL, 32'd1);
        repeat (40) @(posedge clk);
        r_cfg(R_AP_RET, v);
        chk("T2b cout=65 > ERR_RANGE", v, 32'h0000_0101);
        cfg_apply;

        // T2c k=2 ⇒ ERR_TAP(2)
        w_cfg(R_K, 32'd2);
        w_cfg(R_AP_CTRL, 32'd1);
        repeat (40) @(posedge clk);
        r_cfg(R_AP_RET, v);
        chk("T2c k=2 > ERR_TAP", v, 32'h0000_0201);
        cfg_apply;

        // T2d tap_stride 与 k 不匹配 ⇒ ERR_TAP
        // TAP 合法值表已从 {4,12,28} 改成 {8,16,32}：
        //   这里要送"另一个 k 的**合法** TAP"才能构成"合法但不匹配"，
        //   旧值 12/4 在新表里对任何 k 都不合法（会变成"野值"），语义不同。
        w_cfg(R_TAP, (KK == 1) ? 32'd16 : 32'd8);
        w_cfg(R_AP_CTRL, 32'd1);
        repeat (40) @(posedge clk);
        r_cfg(R_AP_RET, v);
        chk("T2d tap_stride 与 k 不匹配 > ERR_TAP", v, 32'h0000_0201);
        cfg_apply;

        // T2e cin=192,k=5 ⇒ ceil(192/8)*25 = 600 > 512 ⇒ ERR_WCAP(3)
        //   ⚠ TAP 已改成 8·ceil(k²/8)，k=5 时合法值是 32（不是旧的 28）。
        //     用 28 会先撞 ERR_TAP 而不是 ERR_WCAP，这个用例会误报。
        w_cfg(R_CIN, 32'd192);
        w_cfg(R_K,   32'd5);
        w_cfg(R_TAP, 32'd32);
        w_cfg(R_AP_CTRL, 32'd1);
        repeat (40) @(posedge clk);
        r_cfg(R_AP_RET, v);
        chk("T2e cin=192,k=5 > ERR_WCAP", v, 32'h0000_0301);
        cfg_apply;

        // T2f 基址非 8 字节对齐 ⇒ ERR_ALIGN(4)
        w_base(B_IN_, B_IN + 32'd4);
        w_cfg(R_AP_CTRL, 32'd1);
        repeat (40) @(posedge clk);
        r_cfg(R_AP_RET, v);
        chk("T2f B_IN+4 > ERR_ALIGN", v, 32'h0000_0401);
        cfg_apply;

        // T2g 基址高 32 位非 0 ⇒ ERR_ADDR(5)
        w_base(B_WGT_+4, 32'd1);
        w_cfg(R_AP_CTRL, 32'd1);
        repeat (40) @(posedge clk);
        r_cfg(R_AP_RET, v);
        chk("T2g B_WGT 高字非 0 > ERR_ADDR", v, 32'h0000_0501);
        w_base(B_WGT_+4, 32'd0);
        cfg_apply;

        // ------------------------------------------------------------------
        // T3 功能跑：装黄金向量 → start → 等 done → 逐字节比对
        // ------------------------------------------------------------------
        $display("[TB] ---- 装载黄金向量（%0s）", gdir);
        load_file("input_c8.bin",    B_IN,   nb_in);
        load_file("weight.bin",      B_WGT,  nb_wgt);
        load_file("bias.bin",        B_BIAS, nb_bias);
        load_file("expected_c8.bin", B_EXP,  nb_exp);

        cfg_apply;
        start_and_wait(ret, cyc);
        chk("T3 AP_RETURN(done=1, err=0)", ret, 32'h0000_0001);

        // ---- 逐字节比对 ----
        nbad = 0; n_x = 0;
        for (o = 0; o < total; o = o + 1) begin
            got_i = mem[B_OUT + o];
            exp_i = mem[B_EXP + o];
            if (got_i > 255) n_x = n_x + 1;          // X/Z 读成 int 时不是 0..255
            if (got_i !== exp_i) begin
                if (nbad < 8)
                    $display("    差异 off=0x%06h (oc=%0d y=%0d x=%0d): got=%0d exp=%0d",
                             o, (o / (Hout*Wout)) * 8 + (o % 8),
                             (o / 8) % Hout, (o / (8*Hout)) % Wout,
                             $signed(got_i[7:0]), $signed(exp_i[7:0]));
                nbad = nbad + 1;
            end
        end
        $display("[TB] 逐字节一致 : %0d / %0d 字节（X/Z=%0d）", total - nbad, total, n_x);
        if (nbad == 0 && n_x == 0) n_pass = n_pass + 1;
        else begin
            $display("[TB] FAIL 逐字节不一致 %0d 字节", nbad);
            n_fail = n_fail + 1;
        end

        // ------------------------------------------------------------------
        // T4 不变量 / 统计
        // ------------------------------------------------------------------
        repeat (20) @(posedge clk);
        if (max_rq >= 2) begin
            $display("[TB] PASS T4 读侧最大在飞笔数 = %0d (>=2)", max_rq); n_pass = n_pass + 1;
        end else begin
            $display("[TB] FAIL T4 读侧最大在飞笔数 = %0d（应 >=2）", max_rq); n_fail = n_fail + 1;
        end
        // ⚠ 新 TAP = 8·ceil(k²/8) ⇒ 权重/偏置/输入读地址全部 ≡ 0 (mod 8)
        //   ⇒ 包装层的"跨字读 + 桶形移位 + 前瞻槽"是**数学上不可达的死代码**，
        //   所以"必须发生 4 字节对齐读"这条硬断言在新布局下无法满足。
        //   保留严格口径：TAP 是 8 的倍数时视为通过（并打印原因），
        //   否则（未来若有 TAP%8!=0 的布局）仍然严格失败 —— 跨字通路那时才用得上。
        if (n_misaln > 0) begin
            $display("[TB] PASS T4 发生过 %0d 次 4 字节对齐需求读（其中 %0d 次落在线的最后一槽，靠前瞻槽）",
                     n_misaln, n_misaln_word); n_pass = n_pass + 1;
        end else if ((TAP % 8) != 0) begin
            $display("[TB] FAIL T4 TAP=%0d 不是 8 的倍数，却没有触发非对齐读（跨字路径未被覆盖）",
                     TAP); n_fail = n_fail + 1;
        end else begin
            $display("[TB] PASS T4 TAP=%0d 是 8 的倍数 ⇒ 读地址恒 8 字节对齐，跨字通路不可达（0 次非对齐读，符合预期）",
                     TAP); n_pass = n_pass + 1;
        end
        chk("T4 AXI 协议错误数", axi_errors, 0);
        chk("T4 读数据逐拍对拍错误数", n_rd_bad, 0);

        r_cfg(R_DBG_RD, v);
        $display("[TB] DBG_RD=0x%08h (需求读=%0d 填线=%0d)", v, v[15:0], v[31:16]);
        r_cfg(R_DBG_AR, v);
        arr_pm = (dut.cnt_rdreq == 0) ? 0 : ((dut.cnt_ar * 1000) / dut.cnt_rdreq);
        $display("[TB] AR 笔数=%0d  R 拍数=%0d  DBG_RD.填线=%0d  => AR/需求读 = %0d.%03d",
                 v[15:0], v[31:16], dut.cnt_fill, arr_pm / 1000, arr_pm % 1000);
        r_cfg(R_DBG_WR, v);
        $display("[TB] DBG_WR=0x%08h (burst=%0d W拍=%0d)", v, v[31:16], v[15:0]);
        $display("[TB] 统计: AR=%0d R拍=%0d AW=%0d W拍=%0d 最大在飞=%0d 拍数=%0d",
                 n_ar, n_rbeat, n_aw, n_wbeat, max_rq, cyc);

        $display("=========================================================");
        $display("[TB-SUMMARY] module=conv_uni_axi_wrap shape=%0s bytes=%0d/%0d cases=%0d/%0d cycles=%0d max_rq=%0d misaln=%0d axi_errors=%0d status=%0s",
                 gdir, total - nbad, total, n_pass, n_pass + n_fail, cyc, max_rq,
                 n_misaln, axi_errors + n_rd_bad,
                 ((nbad == 0 && n_fail == 0 && axi_errors == 0) ? "PASS" : "FAIL"));
        $display("[TB] PASS=%0d FAIL=%0d", n_pass, n_fail);
        if (nbad != 0 || n_fail != 0 || axi_errors != 0 || n_rd_bad != 0)
            $fatal(1, "[TB] FAIL");
        $display("=========================================================");
        $finish;
    end

    // 看门狗（防止死锁时永远挂着）
    initial begin
        #1_000_000_000;
        $display("[TB] TIMEOUT（%0d ns）", $time);
        $fatal(1, "[TB] TIMEOUT");
    end

endmodule
