// ============================================================================
//  tb_k3_axi_wrap.v —— k3_axi_wrap 的自测 TB（AXI4-Lite + 两个 AXI4 从机模型）
//
//  为什么单独一个 TB（主控 2026-10-26）：
//    包装层是 PS 唯一的接口，但**从未被仿真过**（k1 的也没有）。它承担的是
//    "让 IP 自己拒绝错参数"的安全边界，而引擎 TB 完全覆盖不到。
//
//  覆盖：
//    T1  Lite 寄存器读写回环（含 4 组 64 位地址的高低字）
//    T2  拒绝 stride≠1 && dil≠1  ⇒ AP_RETURN = 4'h9，done 自打，**引擎不启动**
//    T3  拒绝地址窗口越界        ⇒ AP_RETURN = 4'hA，done 自打，引擎不启动
//    T4  正常启动（1×1 / cin=8 / cout=8）⇒ AP_RETURN=0、done 正常、引擎真的跑完
//    T5  运行期不变量：STATUS[9]（sticky_oob）必须恒 0（TB 断言）
//
//  从机模型：
//    · AXI4 读从机（合并口）：AR 握手后按 arlen 回 (arlen+1) 拍，末拍 rlast=1，
//      数据全 0（引擎只需要能跑，不校验数值）。
//    · AXI4 写从机（输出口）：收 AW/W，wlast 后回 B。
//  运行：xvlog --sv modules/*.v tb/tb_k3_axi_wrap.v && xelab/xsim tb_k3_axi_wrap
// ============================================================================
`timescale 1ns / 1ps

module tb_k3_axi_wrap;

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
    logic        out_wvalid;  logic out_wready;
    logic [63:0] out_wdata;   logic [7:0] out_wstrb; logic out_wlast;
    logic        out_bvalid;  logic out_bready;

    // ---- DUT ----
    k3_axi_wrap #(.LANES(8), .CAP_B(4608)) dut (
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
    // 观测：引擎是否被启动过 / 观测到几次 AR
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
    // AXI4 读从机（合并口）：全 0 数据，按 arlen 回拍
    // ========================================================================
    logic [7:0]  rs_len  = 8'd0;
    logic        rs_busy = 1'b0;
    logic [7:0]  rs_cnt  = 8'd0;

    assign rd_rlast = rs_busy && rd_rvalid && (rs_cnt == rs_len);

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
    task automatic chk(input [127:0] name, input [31:0] got, input [31:0] exp);
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
    integer cyc, t0;
    logic [31:0] v;

    initial begin
        awvalid = 0; wvalid = 0; bready = 0; arvalid = 0; rready = 0;
        awaddr = 0; wdata = 0; wstrb = 0; araddr = 0;
        #100 rst_n = 1'b1;
        repeat (5) @(posedge clk);

        // ------------------------------------------------------------------
        // T1 Lite 寄存器读写回环
        // ------------------------------------------------------------------
        w_ctrl(32'h38, 32'd8);   w_ctrl(32'h40, 32'd8);
        w_ctrl(32'h18, 32'd1);   w_ctrl(32'h20, 32'd1);
        w_ctrl(32'h48, 32'd3);
        w_ctrl(32'h50, 32'd1);   w_ctrl(32'h58, 32'd1);
        w_ctrl(32'h60, 32'd1);   w_ctrl(32'h68, 32'd0);  w_ctrl(32'h70, 32'd1);
        lite_rd({15'd0, 1'b1, 16'h38}, v); chk("REG CIN",   v, 32'd8);
        lite_rd({15'd0, 1'b1, 16'h40}, v); chk("REG COUT",  v, 32'd8);
        lite_rd({15'd0, 1'b1, 16'h50}, v); chk("REG STRIDE",v, 32'd1);
        lite_rd({15'd0, 1'b1, 16'h58}, v); chk("REG DIL",   v, 32'd1);
        lite_rd({15'd0, 1'b1, 16'h68}, v); chk("REG SHIFT", v, 32'd0);

        w_offs(32'h10, 32'h1000_0000);
        w_offs(32'h1C, 32'h1001_0000);
        w_offs(32'h28, 32'h1002_0000);
        w_offs(32'h34, 32'h1003_0000);
        lite_rd({15'd0, 1'b0, 16'h10}, v); chk("ADDR IN",  v, 32'h1000_0000);
        lite_rd({15'd0, 1'b0, 16'h1C}, v); chk("ADDR WGT", v, 32'h1001_0000);
        lite_rd({15'd0, 1'b0, 16'h28}, v); chk("ADDR BIAS",v, 32'h1002_0000);
        lite_rd({15'd0, 1'b0, 16'h34}, v); chk("ADDR OUT", v, 32'h1003_0000);

        w_ctrl(32'h98, 32'h1000_0000);      // WIN_LO
        w_ctrl(32'h9C, 32'h1004_0000);      // WIN_HI
        lite_rd({15'd0, 1'b1, 16'h98}, v); chk("WIN_LO", v, 32'h1000_0000);
        lite_rd({15'd0, 1'b1, 16'h9C}, v); chk("WIN_HI", v, 32'h1004_0000);

        // ------------------------------------------------------------------
        // T2 拒绝 stride≠1 && dil≠1
        // ------------------------------------------------------------------
        n_start = 0;
        w_ctrl(32'h50, 32'd2);              // stride=2
        w_ctrl(32'h58, 32'd2);              // dil=2  ⇒ 4'h9
        w_ctrl(32'h00, 32'd1);              // start
        repeat (20) @(posedge clk);
        lite_rd({15'd0, 1'b1, 16'hA8}, v);
        $display("[TB-DBG] T2 0xA8 自检 = %b (bit17=win_ok bit16=ok bits3:0=err)", v);
        lite_rd({15'd0, 1'b1, 16'h10}, v); chk("REJ9 AP_RETURN", v, 32'h9);
        lite_rd({15'd0, 1'b1, 16'h94}, v);
        chk("REJ9 done bit", v & 32'h1, 32'h1);
        chk("REJ9 engine_start", n_start, 0);

        // ------------------------------------------------------------------
        // T3 拒绝地址窗口越界（WIN_HI 只到 in 区中间）
        // ------------------------------------------------------------------
        w_ctrl(32'h50, 32'd1); w_ctrl(32'h58, 32'd1);   // 合法
        w_ctrl(32'h9C, 32'h1000_0010);                  // 窗口太小 ⇒ 4'hA
        n_start = 0;
        repeat (4) @(posedge clk);
        lite_rd({15'd0, 1'b1, 16'h9C}, v);
        $display("[TB-DBG] T3 WIN_HI 读回 = 0x%08h (期望 0x10000010)", v);
        lite_rd({15'd0, 1'b1, 16'hA8}, v);
        $display("[TB-DBG] T3 0xA8 自检 = 0x%02h (bit5=win_ok bit4=ok bits3:0=err)", v);
        lite_rd({15'd0, 1'b1, 16'hAC}, v);
        $display("[TB-DBG] T3 0xAC 四区 = 0x%01h (bit3=ou bit2=bi bit1=wg bit0=in)", v);
        lite_rd({15'd0, 1'b1, 16'hB0}, v);
        $display("[TB-DBG] T3 0xB0 bytes_in = %0d (期望 64)", v);
        w_ctrl(32'h00, 32'd1);
        repeat (20) @(posedge clk);
        lite_rd({15'd0, 1'b1, 16'h10}, v); chk("REJA AP_RETURN", v, 32'hA);
        chk("REJA engine_start", n_start, 0);

        // ------------------------------------------------------------------
        // T4 正常启动（1×1, cin=8, cout=8）—— 从机全回 0，只要跑完即可
        // ------------------------------------------------------------------
        w_ctrl(32'h9C, 32'h1004_0000);      // 恢复窗口
        n_start = 0; n_rd_ar = 0; n_wr_beat = 0;
        w_ctrl(32'h00, 32'd1);
        repeat (3000) @(posedge clk);       // 1×1/8×8 的完整流程约 300 拍，给足余量
        lite_rd({15'd0, 1'b1, 16'h10}, v); chk("RUN AP_RETURN", v, 32'h0);
        lite_rd({15'd0, 1'b1, 16'h94}, v); chk("RUN done bit", v & 32'h1, 32'h1);
        if (n_start == 1) begin $display("[TB] PASS RUN engine_start 恰好 1 次"); pass_cnt = pass_cnt + 1; end
        else begin $display("[TB] FAIL RUN engine_start = %0d (期望 1)", n_start); fail_cnt = fail_cnt + 1; end
        if (n_rd_ar > 0) begin $display("[TB] PASS RUN 合并读口发出 %0d 个 AR", n_rd_ar); pass_cnt = pass_cnt + 1; end
        else begin $display("[TB] FAIL RUN 合并读口没有 AR"); fail_cnt = fail_cnt + 1; end

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
        $display("=========================================================");
        $finish;
    end

    // 看门狗
    initial begin
        #20_000_000;
        $display("[TB] TIMEOUT");
        $finish;
    end

endmodule
