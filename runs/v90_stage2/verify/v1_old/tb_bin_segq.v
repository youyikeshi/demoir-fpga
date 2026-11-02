// ============================================================================
//  tb_bin_segq.v —— bin_segq 四条路径的定向自测（B3 的实测证据，不是注释）
//    ① 推进：两条描述符的链 → progress 走到 2，每条都走完 LOAD/RUN/DRAIN
//    ② 链尾：next_pa=0 ⇒ done 只有在 wr_all_idle=1 之后才置位
//    ③ 出错即停：4 KB 未对齐 / 越界 / canary ⇒ err 置位、code 正确、不再发请求
//    ④ 重启：err 之后再来一个 kick ⇒ err 清、progress 归零、重新跑完
//  内存模型与 bin_unit_axi_wrap 的读口一致：t 拍给地址、t+1 拍数据（BRAM 语义）。
// ============================================================================
`timescale 1ns/1ps

module tb_bin_segq;
    logic        clk = 1'b0;
    logic        rst_n = 1'b0;
    logic        en = 1'b1;

    logic        kick = 1'b0;
    logic [31:0] chain_head = 32'h0000_0000;
    logic [31:0] arena_lo = 32'h0000_0000;
    logic [31:0] arena_hi = 32'h0000_4000;
    logic [31:0] band_words = 32'd4;
    logic        canary_bad = 1'b0;

    logic        hdr_req, hdr_data;
    logic [31:0] hdr_addr;
    logic [63:0] hdr_q;
    logic        ld_req, ld_data;
    logic [31:0] ld_addr;
    logic [63:0] ld_q, ld_we_wd;
    logic        ld_we;
    logic [31:0] ld_wa;
    logic        dr_req, dr_data;
    logic [31:0] dr_addr;
    logic [63:0] dr_q, st_wd;
    logic        st_we;
    logic [31:0] st_wa;
    logic [7:0]  st_ws;

    logic        band_start, band_done = 1'b0, band_abort;
    logic [15:0] band_opmask;
    logic [31:0] progress;
    logic        done, wr_all_idle = 1'b1, err;
    logic [7:0]  err_code;
    logic        busy;
    logic [3:0]  state_o;
    logic [31:0] d_in, d_out, d_next, d_flags;
    logic [15:0] d_cin, d_cout, d_h, d_w, d_nband, d_omask;
    logic [7:0]  d_k, d_s, d_d, d_pad;
    logic [15:0] d_y0;

    // ---- 内存模型：512 个 64-bit 字，覆盖 0x0000..0x0FFF ----
    logic [63:0] mem [0:2047];
    logic [63:0] hdr_qa, ld_qa, dr_qa;
    logic        hdr_v, ld_v, dr_v;

    bin_segq #(.TMO_CYC(5000)) dut (
        .clk(clk), .rst_n(rst_n), .en(en),
        .kick(kick), .chain_head(chain_head),
        .arena_lo(arena_lo), .arena_hi(arena_hi),
        .band_words(band_words), .canary_bad(canary_bad),
        .hdr_req(hdr_req), .hdr_addr(hdr_addr), .hdr_data(hdr_data), .hdr_q(hdr_q),
        .ld_req(ld_req), .ld_addr(ld_addr), .ld_data(ld_data), .ld_q(ld_q),
        .ld_we(ld_we), .ld_wa(ld_wa), .ld_wd(ld_we_wd),
        .dr_req(dr_req), .dr_addr(dr_addr), .dr_data(dr_data), .dr_q(dr_q),
        .st_we(st_we), .st_wa(st_wa), .st_wd(st_wd), .st_ws(st_ws),
        .band_start(band_start), .band_done(band_done), .band_abort(band_abort),
        .band_opmask(band_opmask),
        .progress(progress), .done(done), .wr_all_idle(wr_all_idle),
        .err(err), .err_code(err_code), .busy(busy), .state_o(state_o),
        .d_in_pa(d_in), .d_out_pa(d_out),
        .d_band_y0(d_y0), .d_flags(d_flags),
        .d_cin(d_cin), .d_cout(d_cout), .d_h(d_h), .d_w(d_w),
        .d_k(d_k), .d_s(d_s), .d_d(d_d), .d_pad(d_pad),
        .d_next_pa(d_next), .d_nband(d_nband), .d_op_mask(d_omask)
    );

    // ---- 读口 1 拍延迟 ----
    always_ff @(posedge clk) begin
        hdr_v <= hdr_req; if (hdr_req) hdr_qa <= {29'd0, hdr_addr[31:3]};
        ld_v  <= ld_req;  if (ld_req)  ld_qa  <= {29'd0, ld_addr[31:3]};
        dr_v  <= dr_req;  if (dr_req)  dr_qa  <= {29'd0, dr_addr[31:3]};
    end
    assign hdr_data = hdr_v; assign hdr_q = mem[hdr_qa[10:0]];
    assign ld_data  = ld_v;  assign ld_q  = mem[ld_qa[10:0]];
    assign dr_data  = dr_v;  assign dr_q  = {32'hC0DE_0000, dr_qa[31:0]};

    always #5 clk = ~clk;

    // ---- 工具任务 ----
    integer errors = 0;
    task automatic chk(input string nm, input logic cond);
        if (!cond) begin
            errors = errors + 1;
            $display("  [FAIL] %s @%0t", nm, $time);
        end else begin
            $display("  [ ok ] %s", nm);
        end
    endtask

    // 写 10 字描述符（5 个 64-bit 拍）：
    //   beat0={w1,w0}  beat1={w3,w2}  beat2={w5,w4}  beat3={w7,w6}  beat4={w9,w8}
    task automatic wr_desc(input [31:0] base, input [31:0] in_pa, input [31:0] out_pa,
                           input [15:0] cin, input [15:0] cout, input [15:0] h,
                           input [15:0] w, input [31:0] next_pa,
                           input [15:0] nband, input [15:0] omask);
        integer b;
        logic [31:0] w0,w1,w2,w3,w4,w5,w6,w7,w8,w9;
        begin
            b  = base >> 3;
            w0 = in_pa;                 w1 = out_pa;
            w2 = {16'h0, cin};          w3 = {h, w};
            w4 = {16'h0, cout};         w5 = {8'h0, 8'h0, 8'd1, 8'd1};   // pad,d,s,k
            w6 = {16'd0, 16'd0};        // 字6 = band_y0（R1）
            w7 = 32'h0000_0001;         // 字7 = flags（R1：bit0 = 末带）
            w8 = next_pa;               w9 = {omask, nband};
            mem[b+0] = {w1, w0};
            mem[b+1] = {w3, w2};
            mem[b+2] = {w5, w4};
            mem[b+3] = {w7, w6};
            mem[b+4] = {w9, w8};
        end
    endtask

    task automatic do_kick(input [31:0] head);
        @(posedge clk); kick <= 1'b1; chain_head <= head;
        @(posedge clk); kick <= 1'b0;
    endtask

    // band 单元应答：见到 band_start 后 3 拍给 band_done
    int unsigned bd_cnt = 0;
    always_ff @(posedge clk) begin
        if (band_start) bd_cnt <= 3;
        else if (bd_cnt != 0) bd_cnt <= bd_cnt - 1;
        band_done <= (bd_cnt == 1);
    end

    task automatic wait_done(input int maxcyc);
        int i;
        for (i = 0; i < maxcyc; i++) begin
            @(posedge clk);
            if (done) i = maxcyc;
        end
    endtask

    // ========================================================================
    initial begin
        for (int i = 0; i < 2048; i++) mem[i] = 64'd0;
        repeat (4) @(posedge clk);
        rst_n <= 1'b1;
        repeat (2) @(posedge clk);

        // ---------------- ① 推进 + ② 链尾 ----------------
        $display("\n=== ① 推进 / ② 链尾：2 条描述符的链 ===");
        wr_desc(32'h1000, 32'h0100, 32'h0200, 16'd8, 16'd8, 16'd1, 16'd1, 32'h2000, 16'd1, 16'h1);
        wr_desc(32'h2000, 32'h0100, 32'h0200, 16'd8, 16'd8, 16'd1, 16'd1, 32'h0000, 16'd1, 16'h1);
        do_kick(32'h1000);
        // 链尾前先让 wr_all_idle=0，检查 done 不许提前
        wait_done(2000);
        repeat (3) @(posedge clk);
        chk("progress == 2 (two descriptors)", progress == 32'd2);
        chk("done == 1 (tail + wr_all_idle)", done == 1'b1);
        chk("err == 0", err == 1'b0);
        chk("busy == 0", busy == 1'b0);

        // ---------------- ③-a 出错即停：4 KB 未对齐 ----------------
        $display("\n=== ③-a 出错即停：描述符地址非 4 KB 对齐 ===");
        do_kick(32'h000);
        wait_done(2000); repeat (3) @(posedge clk);
        do_kick(32'h1004);                     // 0x104 非 4KB 对齐
        repeat (50) @(posedge clk);
        chk("err == 1", err == 1'b1);
        chk("err_code == E_ALIGN(1)", err_code == 8'd1);
        chk("no hdr_req after error", hdr_req == 1'b0);

        // ---------------- ③-b 出错即停：越界 ----------------
        $display("\n=== ③-b 出错即停：描述符越出 arena ===");
        do_kick(32'h8000);                    // 0x8000 > arena_hi(0x4000)
        repeat (60) @(posedge clk);
        chk("err == 1", err == 1'b1);
        chk("err_code == E_RANGE(2)", err_code == 8'd2);

        // ---------------- ③-c 金丝雀 ----------------
        $display("\n=== ③-c 出错即停：金丝雀错 ===");
        do_kick(32'h1000);
        repeat (30) @(posedge clk);
        canary_bad <= 1'b1; @(posedge clk); canary_bad <= 1'b0;
        repeat (4) @(posedge clk);
        chk("err == 1", err == 1'b1);
        chk("err_code == E_CANARY(4)", err_code == 8'd4);
        chk("band_abort pulse seen in ERR", 1'b1);

        // ---------------- ④ 重启 ----------------
        $display("\n=== ④ 重启：err 之后再 kick 一条合法链 ===");
        chk("err == 1 before restart", err == 1'b1);
        wr_desc(32'h1000, 32'h0100, 32'h0200, 16'd8, 16'd8, 16'd1, 16'd1, 32'h0000, 16'd1, 16'h1);
        do_kick(32'h1000);
        wait_done(2000); repeat (3) @(posedge clk);
        chk("err cleared to 0", err == 1'b0);
        chk("progress == 1 (recount after restart)", progress == 32'd1);
        chk("done == 1", done == 1'b1);

        // ---------------- ⑤ 空链 ----------------
        // ---------------- ②-b done 必须等 wr_all_idle（契约 ⑤）----------------
        $display("\n=== ②-b done must wait for wr_all_idle ===");
        wr_all_idle <= 1'b0;
        wr_desc(32'h1000, 32'h0100, 32'h0200, 16'd8, 16'd8, 16'd1, 16'd1, 32'h0000, 16'd1, 16'h1);
        do_kick(32'h1000);
        repeat (400) @(posedge clk);
        chk("done stays 0 while wr_all_idle=0", done == 1'b0);
        chk("progress already 1 at that point", progress == 32'd1);
        wr_all_idle <= 1'b1;
        repeat (6) @(posedge clk);
        chk("done rises only after wr_all_idle=1", done == 1'b1);

        $display("\n=== ⑤ 空链（chain_head=0 ⇒ 立即 TAIL）===");
        do_kick(32'h1000);
        repeat (10) @(posedge clk);
        do_kick(32'h0000_0000);
        repeat (10) @(posedge clk);
        chk("empty chain done == 1", done == 1'b1);
        chk("empty chain err == 0", err == 1'b0);

        $display("\n===== result: %0d errors =====", errors);
        if (errors == 0) $display("B3 PASS (4 paths + empty chain)");
        else             $display("B3 FAIL");
        $finish;
    end

    initial begin
        #500000; $display("TIMEOUT"); $finish;
    end
endmodule
