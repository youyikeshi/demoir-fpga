// ============================================================================
//  tb_v90_range.v —— T-90（独立验收）V2：越界 out_pa 必须报 E_RANGE
// ----------------------------------------------------------------------------
//  被测：runs/timing_v50/rtl_v50/lane16/bin_segq.v（T-89 的 4 行修正版）
//  判据：
//    A  out_pa out of arena ⇒ err=1, err_code=E_RANGE(2)，且一个写都不许发
//    B  in_pa  out of arena ⇒ 同上（对称）
//    C  合法 out_pa      ⇒ 不报错；**第一条 st_wa 必须 = out_pa**（证明排空基址
//       真的来自字1，旧版这里是 cin ⇒ 这条就是"静默写低地址"的回归判据）
//        C 同时把 cin=0 / flags=0 放进去：修正后它们**不再**被判 E_NULL
//    D  ARENA 默认值回落窗口（0x3000_0000..0x3FFF_FFFF，wrap:1789-1791）下：
//       窗口外 out_pa ⇒ E_RANGE
//    E  同一回落窗口内 out_pa ⇒ 正常跑完，st_wa 基址 = out_pa
//  内存模型：可切换基址 MEM_OFF（每个用例把描述符放在 MEM_OFF+0 的 5 个拍里）
// ============================================================================
`timescale 1ns/1ps

module tb_v90_range;
    logic        clk = 1'b0;
    logic        rst_n = 1'b0;
    logic        en = 1'b1;

    logic        kick = 1'b0;
    logic [31:0] chain_head = 32'd0;
    logic [31:0] arena_lo = 32'h0000_2000;
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

    // ---- 内存模型：MEM_OFF 起 512 个 64-bit 字；越界读回 0 ----
    logic [63:0] mem [0:511];
    logic [31:0] mem_off = 32'h0000_2000;
    logic [63:0] hdr_qa, ld_qa;
    logic        hdr_v, ld_v, dr_v;

    always_ff @(posedge clk) begin
        hdr_v <= hdr_req; if (hdr_req) hdr_qa <= (hdr_addr - mem_off) >> 3;
        ld_v  <= ld_req;  if (ld_req)  ld_qa  <= (ld_addr  - mem_off) >> 3;
        dr_v  <= dr_req;
    end
    assign hdr_data = hdr_v;
    assign hdr_q    = (hdr_qa[31:9] == 23'd0) ? mem[hdr_qa[8:0]] : 64'd0;
    assign ld_data  = ld_v;
    assign ld_q     = (ld_qa[31:9] == 23'd0) ? mem[ld_qa[8:0]] : 64'hDEAD_BEEF_0000_0000;
    assign dr_data  = dr_v;

    always #5 clk = ~clk;

    integer errors = 0;
    task automatic chk(input string nm, input logic cond);
        if (!cond) begin errors = errors + 1; $display("  [FAIL] %s @%0t", nm, $time); end
        else $display("  [ ok ] %s", nm);
    endtask

    // 描述符：字0 in_pa / 字1 out_pa / 字2 cin / 字3 {h_in,cfg_h} / 字4 W
    //         字5 let / 字6 y0 / 字7 flags / 字8 next / 字9 {omask,nband}
    task automatic wr_desc(input [31:0] in_pa, input [31:0] out_pa,
                           input [15:0] cin, input [15:0] flags,
                           input [15:0] omask);
        begin
            mem[0] = {out_pa, in_pa};
            mem[1] = {16'd4, 16'd1, 16'd0, cin};   // 字3={h_in=4,cfg_h=1}, 字2={0,cin}
            mem[2] = {16'd0, 16'd0, 16'd0, 16'd8}; // 字5=0, 字4=W=8
            mem[3] = {{16'd0, 16'd0}, {16'd0, flags}}; // 字7=flags, 字6=y0=0
            mem[4] = {omask, 16'd1, 32'd0};        // 字9={omask,nband=1}, 字8=next=0
        end
    endtask

    task automatic do_kick(input [31:0] head);
        @(posedge clk); kick <= 1'b1; chain_head <= head;
        @(posedge clk); kick <= 1'b0;
    endtask

    // 排空写口观测
    integer n_wr = 0;
    logic [31:0] first_st_wa = 32'hDEAD_DEAD;
    logic [31:0] last_st_wa  = 32'hDEAD_DEAD;
    always_ff @(posedge clk) begin
        if (st_we) begin
            if (n_wr == 0) first_st_wa <= st_wa;
            last_st_wa <= st_wa;
            n_wr <= n_wr + 1;
        end
    end

    int unsigned bd_cnt = 0;
    always_ff @(posedge clk) begin
        if (band_start) bd_cnt <= 3;
        else if (bd_cnt != 0) bd_cnt <= bd_cnt - 1;
        band_done <= (bd_cnt == 1);
    end

    task automatic wait_done(input int maxcyc);
        int i;
        for (i = 0; i < maxcyc; i++) begin @(posedge clk); if (done) i = maxcyc; end
    endtask

    task automatic clear_run();
        begin
            n_wr = 0; first_st_wa = 32'hDEAD_DEAD; last_st_wa = 32'hDEAD_DEAD;
            do_kick(32'd0);                      // 空链 ⇒ 清 err/done/progress
            repeat (5) @(posedge clk);
        end
    endtask

    initial begin
        for (int i = 0; i < 512; i++) mem[i] = 64'd0;
        repeat (4) @(posedge clk);
        rst_n <= 1'b1;
        repeat (2) @(posedge clk);

        // ================= A：out_pa 越界 =================
        $display("\n=== A out_pa=0x8000 out of arena[0x2000,0x4000) ===");
        mem_off = 32'h0000_2000;
        wr_desc(32'h0000_2100, 32'h0000_8000, 16'd8, 16'd1, 16'h1);
        do_kick(32'h0000_2000);
        repeat (200) @(posedge clk);
        chk("A err == 1", err == 1'b1);
        chk("A err_code == E_RANGE(2)", err_code == 8'd2);
        chk("A no write issued on range error (n_wr==0)", n_wr == 0);
        chk("A done == 1 (PS polling can exit)", done == 1'b1);

        // ================= B：in_pa 越界 =================
        $display("\n=== B in_pa=0x8000 out of arena ===");
        clear_run();
        wr_desc(32'h0000_8000, 32'h0000_2200, 16'd8, 16'd1, 16'h1);
        do_kick(32'h0000_2000);
        repeat (200) @(posedge clk);
        chk("B err == 1", err == 1'b1);
        chk("B err_code == E_RANGE(2)", err_code == 8'd2);
        chk("B no write issued on range error (n_wr==0)", n_wr == 0);

        // ========= C：合法 + cin=0 + flags=0（不再误判 E_NULL）=========
        $display("\n=== C legal out_pa=0x3000 / cin=0 / flags=0 ===");
        clear_run();
        wr_desc(32'h0000_2100, 32'h0000_3000, 16'd0, 16'd0, 16'h1);
        do_kick(32'h0000_2000);
        wait_done(3000); repeat (3) @(posedge clk);
        chk("C err == 0 (cin=0/flags=0 no longer E_NULL)", err == 1'b0);
        chk("C progress == 1", progress == 32'd1);
        chk("C done == 1", done == 1'b1);
        chk("C drained 4 words", n_wr == 4);
        chk("C first st_wa == out_pa(0x3000)", first_st_wa == 32'h0000_3000);
        chk("C last st_wa == 0x3018", last_st_wa == 32'h0000_3018);

        // ========= D：回落窗口 0x3000_0000..0x3FFF_FFFF 外的 out_pa =========
        $display("\n=== D fallback window desc + out-of-window out_pa=0x2000_0000 ===");
        clear_run();
        arena_lo <= 32'h3000_0000; arena_hi <= 32'h3FFF_FFFF;
        mem_off  <= 32'h3000_1000;
        wr_desc(32'h3000_1000, 32'h2000_0000, 16'd8, 16'd1, 16'h1);
        do_kick(32'h3000_1000);
        repeat (200) @(posedge clk);
        chk("D err == 1", err == 1'b1);
        chk("D err_code == E_RANGE(2)", err_code == 8'd2);
        chk("D no write issued on range error (n_wr==0)", n_wr == 0);

        // ========= E：回落窗口内的 out_pa =========
        $display("\n=== E inside fallback window out_pa=0x3002_0000 ===");
        clear_run();
        wr_desc(32'h3000_1000, 32'h3002_0000, 16'd8, 16'd1, 16'h1);
        do_kick(32'h3000_1000);
        wait_done(3000); repeat (3) @(posedge clk);
        chk("E err == 0", err == 1'b0);
        chk("E progress == 1", progress == 32'd1);
        chk("E done == 1", done == 1'b1);
        chk("E first st_wa == out_pa(0x3002_0000)",
            first_st_wa == 32'h3002_0000);

        $display("\n===== V2 result: %0d errors =====", errors);
        if (errors == 0) $display("V2 PASS (out-of-range out_pa/in_pa always raise E_RANGE)");
        else             $display("V2 FAIL");
        $finish;
    end

    initial begin
        #2000000; $display("TIMEOUT"); $finish;
    end
endmodule
