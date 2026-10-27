// ============================================================================
//  tb_k5_axi_wrap.v —— k5_axi_wrap 的自测 TB（AXI4-Lite + 两个 AXI4 从机模型）
//
//  为什么单独一个 TB（与 k3 同一理由）：
//    包装层是 PS 唯一的接口，而引擎 TB 完全覆盖不到它。它承担的是
//    "让 IP 自己拒绝错参数"的安全边界（k5 引擎是**编译期固定形状**，
//      写错 cin/cout/H/W/pad/ksize 时引擎不会报错、只会算错 ⇒ 这一层是唯一的防线）。
//
//  覆盖（与 tb_k3_axi_wrap.v 的 T1..T5 同构）：
//    T1  Lite 寄存器读写回环（形状寄存器 + 4 组 64 位地址 + 窗口）
//    T2  拒绝非法配置：cin≠8 → 1、H≠2 → 5、stride≠1 → 3、shift>31 → 7
//        （每种都要 done 自打、引擎**不启动**）
//    T3  拒绝地址窗口越界 ⇒ AP_RETURN = 4'hA，引擎不启动
//    T4  正常启动（H=2 / W=8 / cin=8 / cout=8，引擎真的跑完）
//        + 校验 0xA8 自检 = 0x30、0xAC 四区 = 0xF、0xB0 bytes_in = 2048
//    T5  运行期不变量：STATUS[9]（sticky_oob）必须恒 0（TB 断言）
//
//  ★ 本 TB 用**小形状**例化 DUT（CIN=12/COUT=8/W=8/H=2）而不是真实层的
//    12/48/320/320：k5 引擎的形状是编译期参数，真实层整层要 5,900 万拍，
//    仿真跑不动。小形状走的是同一套包装逻辑（这才是本 TB 的被测对象），
//    而且 H=2 与真实 golden 条带 k5_12x48 的行数一致（pad 行也走到）。
//    CIN 必须保持 12（引擎的行填充写死两个 8 通道块，见 T_CIN 处的说明）；
//    cout=8 + OC_PER_PASS=1 ⇒ 8 个 pass 刚好拼满一个 64-bit beat，
//    输出写回路径也被覆盖。
//
//  从机模型：
//    · AXI4 读从机（合并口）：AR 握手后按 arlen 回 (arlen+1) 拍，末拍 rlast=1，
//      数据全 0（引擎只需要能跑，不校验数值）。
//    · AXI4 写从机（输出口）：收 AW/W，wlast 后回 B。
//  运行：xvlog --sv modules/*.v tb/tb_k5_axi_wrap.v && xelab/xsim tb_k5_axi_wrap
//        （本文件只做 xvlog 语法检查；xelab/xsim 由父 agent 统一跑）
// ============================================================================
`timescale 1ns / 1ps

module tb_k5_axi_wrap;

    // ---- 被测形状（必须与 DUT 的实例参数、以及 TB 写的寄存器值三者一致）----
    //  ★ T_CIN **不能**取 8：conv_k5_engine 的行填充写死了"两个 8 通道块"
    //    （CIN=12 ⇒ CPAD=16 = 2 块），CIN=8 会让它去读第二个不存在的块 ⇒
    //    包装层钳制 + sticky_oob，T5 必失败。真实层就是 12，这里也用 12。
    localparam integer T_CIN  = 12;
    localparam integer T_COUT = 8;
    localparam integer T_W    = 8;
    localparam integer T_H    = 2;
    localparam integer T_PAD  = 2;
    localparam integer T_OC   = 1;

    // ---- 时钟/复位 ----
    logic clk = 1'b0;
    logic rst_n = 1'b0;
    always #5 clk = ~clk;

    // ---- AXI4-Lite ----
    logic [31:0] awaddr;  logic awvalid; logic awready;
    logic [31:0] wdata;   logic [3:0] wstrb; logic wvalid; logic wready;
    logic [1:0]  bresp;   logic bvalid;  logic bready;
    logic [31:0] araddr;  logic arvalid; logic arready;
    logic [31:0] rdata;   logic [1:0] rresp; logic rvalid; logic rready;

    // ---- 合并读口 ----
    logic        rd_arvalid; logic rd_arready;
    logic [31:0] rd_araddr;  logic [7:0] rd_arlen; logic [1:0] rd_arid;
    logic        rd_rvalid;  logic rd_rready;
    logic [63:0] rd_rdata;   logic rd_rlast;
    logic [1:0]  rd_rid = 2'd0;              // 引擎不用 RID，给个常量免得悬空

    // ---- 输出写口 ----
    logic        out_awvalid; logic out_awready;
    logic [31:0] out_awaddr;  logic [7:0] out_awlen;
    logic        out_wvalid; logic out_wready;
    logic [63:0] out_wdata;   logic [7:0] out_wstrb; logic out_wlast;
    logic        out_bvalid; logic out_bready;

    // ---- DUT ----
    k5_axi_wrap #(
        .CIN         (T_CIN),
        .COUT        (T_COUT),
        .W           (T_W),
        .H           (T_H),
        .PAD         (T_PAD),
        .OC_PER_PASS (T_OC)
    ) dut (
        .s_axi_aclk(clk), .s_axi_aresetn(rst_n),
        .s_axi_awaddr(awaddr), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(wstrb), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_araddr(araddr), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(rready),
        .m_axi_rd_arvalid(rd_arvalid), .m_axi_rd_arready(rd_arready),
        .m_axi_rd_araddr(rd_araddr), .m_axi_rd_arlen(rd_arlen), .m_axi_rd_arid(rd_arid),
        .m_axi_rd_rvalid(rd_rvalid), .m_axi_rd_rready(rd_rready),
        .m_axi_rd_rdata(rd_rdata), .m_axi_rd_rlast(rd_rlast), .m_axi_rd_rid(rd_rid),
        .m_axi_out_awvalid(out_awvalid), .m_axi_out_awready(out_awready),
        .m_axi_out_awaddr(out_awaddr), .m_axi_out_awlen(out_awlen),
        .m_axi_out_wvalid(out_wvalid), .m_axi_out_wready(out_wready),
        .m_axi_out_wdata(out_wdata), .m_axi_out_wstrb(out_wstrb), .m_axi_out_wlast(out_wlast),
        .m_axi_out_bvalid(out_bvalid), .m_axi_out_bready(out_bready)
    );

    // ========================================================================
    // 观测：引擎是否被启动过 / 观测到几次 AR / 几次写 beat
    // （层次化引用 dut.u_eng 是**只读观测**，不改 DUT 的任何行为）
    // ========================================================================
    integer n_start = 0, n_rd_ar = 0, n_wr_beat = 0;
    integer pass_cnt = 0, fail_cnt = 0;
    always @(posedge clk) begin
        if (rst_n && dut.u_eng.ctrl_start) n_start <= n_start + 1;
        if (rst_n && rd_arvalid && rd_arready) n_rd_ar <= n_rd_ar + 1;
        if (rst_n && out_wvalid && out_wready) n_wr_beat <= n_wr_beat + 1;
    end

    // ========================================================================
    // AXI4-Lite master tasks
    // ========================================================================
    task automatic lite_wr(input [31:0] addr, input [31:0] data);
        begin
            @(posedge clk);
            awaddr <= addr; awvalid <= 1'b1;
            wdata  <= data; wstrb <= 4'hF; wvalid <= 1'b1;
            bready <= 1'b1;
            // 等 AW/W 各自被接收
            fork
                begin : aw_wait
                    @(posedge clk);
                    while (!awready) @(posedge clk);
                    awvalid <= 1'b0;
                end
                begin : w_wait
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

    // 便捷：按字节偏移写 ctrl 页 / offs 页
    task automatic w_ctrl(input [31:0] off, input [31:0] d); lite_wr({15'd0, 1'b1, off[15:0]}, d); endtask
    task automatic w_offs(input [31:0] off, input [31:0] d); lite_wr({15'd0, 1'b0, off[15:0]}, d); endtask

    // ========================================================================
    // AXI4 读从机（合并口）：全 0 数据，按 arlen 回 (arlen+1) 拍
    //
    // ⚠⚠ RLAST 必须是**组合**的，而且要跟"本拍正在给出的那个 beat"对齐。
    //
    //   原来（照抄 tb_k3_axi_wrap.v，而那个 TB 从未跑过）写成
    //       rd_rlast <= (rs_cnt == rs_len);        // 寄存器，比数据晚一拍
    //   ⇒ 数据 beat k 在 `rs_cnt==k` 的那拍给出，而 rlast 用的是**上一拍的** rs_cnt
    //     ⇒ RLAST 恒落在"最后一个 beat 之后的那一拍"，而那拍 rd_rvalid 已经被清 0
    //     ⇒ **RLAST 从不与 RVALID 同时为 1**。
    //
    //   后果（2026-10-27 实跑）：k5_axi_wrap 的 `rd_busy` 靠 `rvalid && rready && rlast`
    //   清零，于是第一个 burst（权重 2688B = 256+80）走完 256 拍后 rd_busy 永远为 1
    //   ⇒ 后续所有 AR 都被挡住 ⇒ 引擎卡在 S_WGT_DATA，表现为 busy=1 / done=0 /
    //   输出口零写 beat（T4 的三条 FAIL 全由这一个 TB bug 引起，DUT 无辜）。
    //   引擎自己按 beat 计数收 burst，所以它"看起来收到了数据"，只有包装层死等 RLAST。
    // ========================================================================
    logic [7:0]  rs_len  = 8'd0;
    logic        rs_busy = 1'b0;
    logic [7:0]  rs_cnt  = 8'd0;

    // 本拍给出的 beat 序号就是 rs_cnt；末拍（rs_cnt==rs_len）拉 RLAST
    assign rd_rlast = rs_busy && (rs_cnt == rs_len);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rd_arready <= 1'b1; rd_rvalid <= 1'b0; rd_rdata <= 64'd0;
            rs_busy <= 1'b0;  rs_cnt <= 8'd0;
        end else begin
            if (!rs_busy) begin
                rd_rvalid <= 1'b0;
                if (rd_arvalid && rd_arready) begin
                    rs_len  <= rd_arlen;
                    rs_cnt  <= 8'd0;
                    rs_busy <= 1'b1;
                    rd_arready <= 1'b0;          // 一个突发内不再接 AR
                end
            end else begin
                rd_rvalid <= 1'b1;
                rd_rdata  <= {8{8'h00}};
                if (rd_rvalid && rd_rready) begin
                    if (rs_cnt == rs_len) begin
                        rs_busy <= 1'b0; rd_rvalid <= 1'b0;
                        rd_arready <= 1'b1;
                    end else begin
                        rs_cnt <= rs_cnt + 8'd1;
                    end
                end
            end
        end
    end

    // ========================================================================
    // AXI4 写从机（输出口）
    // ========================================================================
    logic ws_aw = 1'b0, ws_b = 1'b0;
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            out_awready <= 1'b1; out_wready <= 1'b0; out_bvalid <= 1'b0;
            ws_aw <= 1'b0; ws_b <= 1'b0;
        end else begin
            if (!ws_aw) begin
                if (out_awvalid && out_awready) begin
                    out_awready <= 1'b0; out_wready <= 1'b1; ws_aw <= 1'b1;
                end
            end else begin
                if (out_wvalid && out_wready && out_wlast) begin
                    out_wready <= 1'b0; out_bvalid <= 1'b1; ws_b <= 1'b1;
                end
            end
            if (out_bvalid && out_bready) begin
                out_bvalid <= 1'b0; out_awready <= 1'b1; ws_aw <= 1'b0; ws_b <= 1'b0;
            end
        end
    end

    // ========================================================================
    // 检查助手
    // ========================================================================
    task automatic chk(input [255:0] name, input [31:0] got, input [31:0] exp);
        begin
            if (got === exp) begin
                $display("[TB] PASS %0s : 0x%08h", name, got);
                pass_cnt = pass_cnt + 1;
            end else begin
                $display("[TB] FAIL %0s : got 0x%08h exp 0x%08h", name, got, exp);
                fail_cnt = fail_cnt + 1;
            end
        end
    endtask

    // ========================================================================
    // 主流程
    // ========================================================================
    logic [31:0] v;

    // 写一套"合法"的形状寄存器（T4 之前调用；T2/T3 在其上做单点破坏）
    task automatic cfg_legal;
        begin
            w_ctrl(32'h18, T_H);      // H
            w_ctrl(32'h20, T_W);      // W
            w_ctrl(32'h28, T_H);      // HOUT（只回显）
            w_ctrl(32'h30, T_W);      // WOUT（只回显）
            w_ctrl(32'h38, T_CIN);    // CIN
            w_ctrl(32'h40, T_COUT);   // COUT
            w_ctrl(32'h48, 32'd5);    // KSIZE
            w_ctrl(32'h50, 32'd1);    // STRIDE
            w_ctrl(32'h58, 32'd1);    // DIL
            w_ctrl(32'h60, 32'd2);    // PAD
            w_ctrl(32'h68, 32'd0);    // SHIFT
            w_ctrl(32'h70, 32'hD);    // RELU | C8_IN | C8_OUT（只回显）
        end
    endtask

    initial begin
        awvalid = 0; wvalid = 0; bready = 0; arvalid = 0; rready = 0;
        awaddr = 0; wdata = 0; wstrb = 0; araddr = 0;
        #100 rst_n = 1'b1;
        repeat (5) @(posedge clk);

        // ------------------------------------------------------------------
        // T1 Lite 寄存器读写回环
        // ------------------------------------------------------------------
        cfg_legal;
        lite_rd({15'd0, 1'b1, 16'h18}, v); chk("REG H",      v, T_H);
        lite_rd({15'd0, 1'b1, 16'h20}, v); chk("REG W",      v, T_W);
        lite_rd({15'd0, 1'b1, 16'h38}, v); chk("REG CIN",    v, T_CIN);
        lite_rd({15'd0, 1'b1, 16'h40}, v); chk("REG COUT",   v, T_COUT);
        lite_rd({15'd0, 1'b1, 16'h48}, v); chk("REG KSIZE",  v, 32'd5);
        lite_rd({15'd0, 1'b1, 16'h50}, v); chk("REG STRIDE", v, 32'd1);
        lite_rd({15'd0, 1'b1, 16'h58}, v); chk("REG DIL",    v, 32'd1);
        lite_rd({15'd0, 1'b1, 16'h60}, v); chk("REG PAD",    v, 32'd2);
        lite_rd({15'd0, 1'b1, 16'h68}, v); chk("REG SHIFT",  v, 32'd0);
        lite_rd({15'd0, 1'b1, 16'h70}, v); chk("REG RELU",   v, 32'hD);

        w_offs(32'h10, 32'h1000_0000);      // in
        w_offs(32'h1C, 32'h1002_0000);      // wgt
        w_offs(32'h28, 32'h1004_0000);      // bias
        w_offs(32'h34, 32'h1006_0000);      // out
        lite_rd({15'd0, 1'b0, 16'h10}, v); chk("ADDR IN",  v, 32'h1000_0000);
        lite_rd({15'd0, 1'b0, 16'h1C}, v); chk("ADDR WGT", v, 32'h1002_0000);
        lite_rd({15'd0, 1'b0, 16'h28}, v); chk("ADDR BIAS",v, 32'h1004_0000);
        lite_rd({15'd0, 1'b0, 16'h34}, v); chk("ADDR OUT", v, 32'h1006_0000);

        w_ctrl(32'h98, 32'h1000_0000);      // WIN_LO
        w_ctrl(32'h9C, 32'h1010_0000);      // WIN_HI
        lite_rd({15'd0, 1'b1, 16'h98}, v); chk("WIN_LO", v, 32'h1000_0000);
        lite_rd({15'd0, 1'b1, 16'h9C}, v); chk("WIN_HI", v, 32'h1010_0000);

        // ------------------------------------------------------------------
        // T2 拒绝非法配置（k5 引擎是编译期固定形状 ⇒ 这些必须被本层拦住）
        //   注意：每次都要把上一次改坏的那一项改回来，否则后面的 case 会命中前一条规则
        // ------------------------------------------------------------------
        n_start = 0;

        w_ctrl(32'h38, 32'd16);             // cin=16 ≠ 12 ⇒ 4'h1
        w_ctrl(32'h00, 32'd1);
        repeat (20) @(posedge clk);
        lite_rd({15'd0, 1'b1, 16'h10}, v); chk("REJ cin AP_RETURN", v, 32'h1);
        lite_rd({15'd0, 1'b1, 16'h94}, v); chk("REJ cin done bit",  v & 32'h1, 32'h1);
        lite_rd({15'd0, 1'b1, 16'hA8}, v); chk("REJ cin selftest",  v, 32'h21); // win_ok[5]=1,ok[4]=0,err=1
        w_ctrl(32'h38, T_CIN);

        w_ctrl(32'h18, 32'd1);              // H=1 ≠ 2  ⇒ 4'h5（形状常量桶）
        w_ctrl(32'h00, 32'd1);
        repeat (20) @(posedge clk);
        lite_rd({15'd0, 1'b1, 16'h10}, v); chk("REJ H AP_RETURN", v, 32'h5);
        w_ctrl(32'h18, T_H);

        w_ctrl(32'h50, 32'd2);              // stride=2 ⇒ 4'h3
        w_ctrl(32'h00, 32'd1);
        repeat (20) @(posedge clk);
        lite_rd({15'd0, 1'b1, 16'h10}, v); chk("REJ stride AP_RETURN", v, 32'h3);
        w_ctrl(32'h50, 32'd1);

        w_ctrl(32'h68, 32'd32);             // shift=32 > 31 ⇒ 4'h7
        w_ctrl(32'h00, 32'd1);
        repeat (20) @(posedge clk);
        lite_rd({15'd0, 1'b1, 16'h10}, v); chk("REJ shift AP_RETURN", v, 32'h7);
        w_ctrl(32'h68, 32'd0);

        chk("REJ engine_start", n_start, 0);
        // 0xA8/0xAC 在合法配置下的形状（T4 前先核对一次）
        repeat (4) @(posedge clk);
        lite_rd({15'd0, 1'b1, 16'hA8}, v); chk("SELFTEST ok", v, 32'h30);   // win_ok|ok
        lite_rd({15'd0, 1'b1, 16'hAC}, v); chk("WIN4 all ok",  v, 32'hF);

        // ------------------------------------------------------------------
        // T3 拒绝地址窗口越界（WIN_HI 只到 in 区中间）
        //   bytes_in = ⌈12/8⌉·8·H·W·8 = 16·2·8·8 = 2048
        // ------------------------------------------------------------------
        // 抬高 WIN_LO 到 in 区中间 ⇒ **只有 in 区**越界（其余三区仍合法）
        //   这样 0xAC 的四区位图能精确验到：ok_ou|ok_bi|ok_wg = 3'b111、ok_in=0 ⇒ 0xE
        w_ctrl(32'h98, 32'h1000_0200);
        n_start = 0;
        repeat (4) @(posedge clk);
        lite_rd({15'd0, 1'b1, 16'hB0}, v); chk("T3 bytes_in", v, 32'd2048);
        lite_rd({15'd0, 1'b1, 16'hAC}, v); chk("T3 WIN4 in=0", v, 32'hE);   // 只有 in 越界
        w_ctrl(32'h00, 32'd1);
        repeat (20) @(posedge clk);
        lite_rd({15'd0, 1'b1, 16'h10}, v); chk("REJA AP_RETURN", v, 32'hA);
        chk("REJA engine_start", n_start, 0);

        // ------------------------------------------------------------------
        // T4 正常启动（H=2 / W=8 / cin=12 / cout=8）—— 从机全回 0，只要跑完即可
        // ------------------------------------------------------------------
        w_ctrl(32'h98, 32'h1000_0000);      // 恢复 WIN_LO（T3 抬高过）
        w_ctrl(32'h9C, 32'h1010_0000);      // 恢复 WIN_HI
        n_start = 0; n_rd_ar = 0; n_wr_beat = 0;
        repeat (4) @(posedge clk);
        w_ctrl(32'h00, 32'd1);
        repeat (60000) @(posedge clk);      // 小形状完整流程（权重装载+2 行+8 pass/像素）
        lite_rd({15'd0, 1'b1, 16'h10}, v); chk("RUN AP_RETURN", v, 32'h0);
        lite_rd({15'd0, 1'b1, 16'h94}, v); chk("RUN done bit",  v & 32'h1, 32'h1);
        chk("RUN busy  bit", v & 32'h2, 32'h0);
        if (n_start == 1) begin $display("[TB] PASS RUN engine_start 恰好 1 次"); pass_cnt = pass_cnt + 1; end
        else begin $display("[TB] FAIL RUN engine_start = %0d (期望 1)", n_start); fail_cnt = fail_cnt + 1; end
        if (n_rd_ar > 0) begin $display("[TB] PASS RUN 合并读口发出 %0d 个 AR", n_rd_ar); pass_cnt = pass_cnt + 1; end
        else begin $display("[TB] FAIL RUN 合并读口没有 AR"); fail_cnt = fail_cnt + 1; end
        if (n_wr_beat > 0) begin $display("[TB] PASS RUN 输出口发出 %0d 个写 beat", n_wr_beat); pass_cnt = pass_cnt + 1; end
        else begin $display("[TB] FAIL RUN 输出口没有写 beat（OC_PER_PASS=1 时 8 个 pass 才拼满一个 beat）"); fail_cnt = fail_cnt + 1; end
        lite_rd({15'd0, 1'b1, 16'hB0}, v);
        $display("[TB] 0xB0 bytes_in = %0d（期望 2048）/ bytes_wgt 期望 2688 / bytes_bias 期望 32 / bytes_out 期望 1024", v);

        // ------------------------------------------------------------------
        // T5 运行期不变量：sticky_oob 必须恒 0
        // ------------------------------------------------------------------
        lite_rd({15'd0, 1'b1, 16'h94}, v);
        chk("INV sticky_oob==0", {31'd0, v[9]}, 32'd0);

        // ------------------------------------------------------------------
        $display("=========================================================");
        $display("[TB-SUMMARY] PASS %0d / FAIL %0d", pass_cnt, fail_cnt);
        if (fail_cnt == 0) $display("[TB-SUMMARY] ALL PASS");
        else               $display("[TB-SUMMARY] HAS FAILURES");
        if (fail_cnt == 0) $display("RESULT: PASS");
        else               $display("RESULT: FAIL (%0d)", fail_cnt);
        $display("=========================================================");
        $finish;
    end

    // ========================================================================
    // 调试打印（仅在 -d K5_WRAP_DBG 时存在）
    // ⚠ 必须能带宏编译通过 ⇒ 只引用**上面已经声明**的信号，且不放在任何
    //   声明之前；调试块绝不出现在可综合路径里（本文件是 TB，无综合问题）。
    // ========================================================================
`ifdef K5_WRAP_DBG
    // 引擎状态名（S_IDLE..S_DONE，与 conv_k5_engine.v 的 state_t 顺序一致）
    function automatic string st_name(input int s);
        case (s)
            0:  return "S_IDLE";
            1:  return "S_WGT_REQ";
            2:  return "S_WGT_DATA";
            3:  return "S_BIAS_REQ";
            4:  return "S_BIAS_DATA";
            5:  return "S_PRE_REQ";
            6:  return "S_FILL_REQ";
            7:  return "S_FILL_DATA";
            8:  return "S_ROW_BEGIN";
            9:  return "S_BLOCK_INIT";
            10: return "S_BLOCK_PRE";
            11: return "S_PRELOAD";
            12: return "S_COMPUTE";
            13: return "S_DRAIN";
            14: return "S_FLUSH";
            15: return "S_WR_REQ";
            16: return "S_WR_WAIT";
            17: return "S_DONE";
            default: return "?";
        endcase
    endfunction

    // 状态追踪：**每次变化打一行**（父 agent 定位死锁用）
    // ⚠ 格式串里的 %b 个数必须与参数个数一一对应：少一个就会整体错位，
    //   打出来的 y/b/x 全是别人的值（第一版就这样错过一次，已按"逐 token 对齐"重写）。
    logic [5:0] dbg_st_q     = 6'd0;
    integer     dbg_cyc      = 0;
    integer     dbg_idle_cyc = 0;    // 连续"没有任何握手"的拍数

    wire dbg_prog = (rd_arvalid && rd_arready) || (rd_rvalid && rd_rready) ||
                    (out_awvalid && out_awready) || (out_wvalid && out_wready) ||
                    (out_bvalid && out_bready) || dut.eng_start;

    always @(posedge clk) begin
        if (rst_n) begin
            dbg_cyc = dbg_cyc + 1;
            if (dbg_prog) dbg_idle_cyc = 0;
            else          dbg_idle_cyc = dbg_idle_cyc + 1;

            if (dut.u_eng.state !== dbg_st_q) begin
                $display("[K5-SM] cyc=%0d %0s->%0s | busy=%b done=%b start=%b || ARv=%b ARr=%b len=%0d | Rv=%b Rr=%b Rlast=%b | Wv=%b Wr=%b | rd_busy=%b tag=%0d || y=%0d b=%0d x=%0d s=%0d ic=%0d wr=%0d lbv=%b",
                         dbg_cyc, st_name(dbg_st_q), st_name(dut.u_eng.state),
                         dut.u_eng.ctrl_busy, dut.u_eng.ctrl_done, dut.eng_start,
                         rd_arvalid, rd_arready, rd_arlen,
                         rd_rvalid, rd_rready, rd_rlast,
                         out_wvalid, out_wready,
                         dut.rd_busy, dut.rd_tag,
                         dut.u_eng.y_cnt, dut.u_eng.b_cnt, dut.u_eng.x_cnt,
                         dut.u_eng.s_cnt, dut.u_eng.ic_cnt, dut.u_eng.wr_line,
                         dut.u_eng.lb_valid);
                dbg_st_q     = dut.u_eng.state;
                dbg_idle_cyc = 0;
            end else if (dut.u_eng.ctrl_busy && (dbg_idle_cyc > 0) && ((dbg_idle_cyc % 2000) == 0)) begin
                $display("[K5-SM] ⚠ 2000 拍无任何握手：state=%0s rd_busy=%b AR=%b/%b R=%b/%b/%b W=%b/%b",
                         st_name(dut.u_eng.state), dut.rd_busy,
                         rd_arvalid, rd_arready, rd_rvalid, rd_rready, rd_rlast,
                         out_wvalid, out_wready);
            end
        end
    end

    always @(posedge clk) begin
        if (rst_n && rd_arvalid && rd_arready)
            $display("[TB-DBG] AR id=%0d addr=0x%08h len=%0d @%0t", rd_arid, rd_araddr, rd_arlen, $time);
        if (rst_n && out_awvalid && out_awready)
            $display("[TB-DBG] AW addr=0x%08h len=%0d @%0t", out_awaddr, out_awlen, $time);
        if (rst_n && dut.eng_start)
            $display("[TB-DBG] eng_start @%0t（cfg_ok=%b win_ok=%b err=%0h，引擎 state=%0s）",
                     $time, dut.cfg_ok_r, dut.cfg_win_ok_r, dut.cfg_err_r, st_name(dut.u_eng.state));
    end
`endif

    // 看门狗
    initial begin
        #20_000_000;
        $display("[TB] TIMEOUT");
        $display("RESULT: FAIL (timeout)");
        $finish;
    end

endmodule
