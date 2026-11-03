// ============================================================================
//  tb_bin_sam.v —— SAM 三分支加权单元验收 TB
//
//  语义（二值网 SAM 块，9 次/网络）：
//      acc = (x << shift) + w0*y0 + w2*y2 + w4*y4        （int32，w 是 Q1.14 逐通道有符号）
//      out = sat8(acc >> shift)
//  w=0 时 out==x，所以这个式子保留了"x 加三个加权分支"的结构。
//
//  DUT 内存映射（相对 base_*）：
//      x/y0/y2/y4 都在 base_* + addr_c8(c,y,x)，int8，C8 交错
//      w0/w2/w4  在 base_* + c*2，int16
//      输出       base_out + addr_c8(c,y,x)
// ============================================================================
`timescale 1ns/1ps
module tb_bin_sam;
    localparam int MAXM = 1 << 20;
    localparam int B_X   = 32'h0000_0000;
    localparam int B_Y0  = 32'h0001_0000;
    localparam int B_Y2  = 32'h0002_0000;
    localparam int B_Y4  = 32'h0003_0000;
    localparam int B_W0  = 32'h0004_0000;
    localparam int B_W2  = 32'h0004_8000;
    localparam int B_W4  = 32'h0005_0000;
    localparam int B_OUT = 32'h0006_0000;
    localparam int B_EXP = 32'h000E_0000;
    localparam int MAX_CYCLES = 20_000_000;
    localparam int TX_C = `TB_CC;
    localparam int TX_H = `TB_HH;
    localparam int TX_W = `TB_WW;
    localparam int TX_SH= `TB_SHIFT;

    logic [7:0] mem [0:MAXM-1];
    logic ap_clk = 1'b0, ap_rst_n;
    logic ctrl_start, ctrl_done, ctrl_busy;
    logic [15:0] cfg_c, cfg_h, cfg_w;
    logic [5:0]  cfg_shift;
    logic [31:0] base_x, base_y0, base_y2, base_y4;
    logic [31:0] base_w0, base_w2, base_w4, base_out;
    logic        mem_rd_en;
    logic [31:0] mem_rd_addr;
    logic [63:0] mem_rd_data;
    logic        mem_wr_en;
    logic [31:0] mem_wr_addr;
    logic [7:0]  mem_wr_data;
    int n_rd = 0, n_wr = 0;

    always #5 ap_clk = ~ap_clk;
    always @(posedge ap_clk) begin
        if (mem_wr_en) n_wr <= n_wr + 1;
        if (mem_rd_en) n_rd <= n_rd + 1;
        if (mem_rd_en)
            mem_rd_data <= {mem[mem_rd_addr+7], mem[mem_rd_addr+6],
                            mem[mem_rd_addr+5], mem[mem_rd_addr+4],
                            mem[mem_rd_addr+3], mem[mem_rd_addr+2],
                            mem[mem_rd_addr+1], mem[mem_rd_addr+0]};
        if (mem_wr_en) mem[mem_wr_addr] <= mem_wr_data;
    end

    bin_sam dut (
        .ap_clk(ap_clk), .ap_ce(1'b1), .ap_rst_n(ap_rst_n),
        .ctrl_start(ctrl_start), .ctrl_done(ctrl_done), .ctrl_busy(ctrl_busy),
        .cfg_c(cfg_c), .cfg_h(cfg_h), .cfg_w(cfg_w), .cfg_shift(cfg_shift),
        .base_x(base_x), .base_y0(base_y0), .base_y2(base_y2), .base_y4(base_y4),
        .base_w0(base_w0), .base_w2(base_w2), .base_w4(base_w4), .base_out(base_out),
        .mem_rd_en(mem_rd_en), .mem_rd_addr(mem_rd_addr), .mem_rd_data(mem_rd_data),
        .mem_wr_en(mem_wr_en), .mem_wr_addr(mem_wr_addr), .mem_wr_data(mem_wr_data)
    );

    string gdir;
    int fh, i, cyc, nbad, total;
    task automatic load_file(input string fn, input int base, output int nbytes);
        fh = $fopen({gdir, "/", fn}, "rb");
        if (fh == 0) $fatal(1, "[TB] FAIL: 打不开 %s/%s", gdir, fn);
        nbytes = 0;
        while (!$feof(fh)) begin
            int c = $fgetc(fh);
            if (c < 0) break;
            mem[base + nbytes] = c[7:0];
            nbytes++;
        end
        $fclose(fh);
    endtask

    initial begin
        int nb1, nb2, nb3, nb4, nb5, nb6, nb7, nb8, nb9;
        if (!$value$plusargs("GOLDEN=%s", gdir)) $fatal(1, "[TB] 缺少 +GOLDEN=");
        for (i = 0; i < MAXM; i++) mem[i] = 8'h00;
        load_file("x_c8.bin",  B_X,   nb1);
        load_file("y0_c8.bin", B_Y0,  nb2);
        load_file("y2_c8.bin", B_Y2,  nb3);
        load_file("y4_c8.bin", B_Y4,  nb4);
        load_file("w0.bin",    B_W0,  nb5);
        load_file("w2.bin",    B_W2,  nb6);
        load_file("w4.bin",    B_W4,  nb7);
        load_file("expected_c8.bin", B_EXP, nb8);

        cfg_c <= TX_C[15:0]; cfg_h <= TX_H[15:0]; cfg_w <= TX_W[15:0];
        cfg_shift <= TX_SH[5:0];
        base_x <= B_X; base_y0 <= B_Y0; base_y2 <= B_Y2; base_y4 <= B_Y4;
        base_w0 <= B_W0; base_w2 <= B_W2; base_w4 <= B_W4; base_out <= B_OUT;

        ap_rst_n <= 1'b0; ctrl_start <= 1'b0;
        repeat (8) @(posedge ap_clk);
        ap_rst_n <= 1'b1;
        repeat (4) @(posedge ap_clk);
        ctrl_start <= 1'b1; @(posedge ap_clk); ctrl_start <= 1'b0;
        cyc = 0;
        while (!ctrl_done && cyc < MAX_CYCLES) begin @(negedge ap_clk); cyc++; end
        if (!ctrl_done) $fatal(1, "[TB] FAIL: ctrl_done 超时（%0d 拍）", cyc);
        repeat (4) @(posedge ap_clk);

        total = TX_C * TX_H * TX_W;
        nbad = 0;
        for (int a = 0; a < total; a++)
            if (mem[B_OUT + a] !== mem[B_EXP + a]) begin
                if (nbad < 5)
                    $display("    差异 idx=%0d: got=%0d exp=%0d", a,
                             $signed(mem[B_OUT + a]), $signed(mem[B_EXP + a]));
                nbad++;
            end
        $display("");
        $display("============================================================");
        $display(" [BIN_ENGINE] sam 验收报告");
        $display("============================================================");
        $display(" golden 目录 : %s", gdir);
        $display(" 形状        : C=%0d H=%0d W=%0d shift=%0d", TX_C, TX_H, TX_W, TX_SH);
        $display(" 逐字节一致  : %0d / %0d 字节", total - nbad, total);
        $display(" 拍数        : %0d", cyc);
        $display(" 诊断        : 读 %0d 次 / 写 %0d 次", n_rd, n_wr);
        $display(" 结论        : %s", (nbad == 0) ? "PASS" : "FAIL");
        if (nbad != 0) $fatal(1, "[TB] FAIL：%0d 字节不一致", nbad);
        $display("[TB-BIN-SUMMARY] shape=%s bytes=%0d/%0d nbad=%0d cycles=%0d status=PASS",
                 gdir, total - nbad, total, nbad, cyc);
        $finish;
    end
endmodule
