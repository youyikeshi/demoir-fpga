// ============================================================================
//  tb_bin_rprelu.v —— RPReLU 逐元素单元验收 TB
//
//  RPReLU 在二值网里出现 227 次、输出 92.8 M 元素，是"非卷积 7.4 s"的主体。
//  语义（契约 docs/binary/03 §1）：
//      dd = x - zeta
//      r  = dd > 0 ? dd + gamma : ((dd*slope) >> slope_shift) + gamma
//      y  = sat8(r >> shift)
//
//  DUT 内存映射（都相对 base_*）：
//      输入   base_in   + addr_c8(c,y,x)           int8，C8 交错
//      zeta   base_zeta + c*4                      int32
//      gamma  base_gamma+ c*4                      int32
//      slope  base_slope+ c*2                      int16
//      输出   base_out  + addr_c8(c,y,x)           int8，C8 交错
// ============================================================================
`timescale 1ns/1ps
module tb_bin_rprelu;

    localparam int MAXM = 1 << 20;
    localparam int BASE_IN   = 32'h0000_0000;
    localparam int BASE_ZETA = 32'h0004_0000;
    localparam int BASE_GAMMA= 32'h0004_8000;
    localparam int BASE_SLOPE= 32'h0005_0000;
    localparam int BASE_OUT  = 32'h0006_0000;
    localparam int BASE_EXP  = 32'h000E_0000;
    localparam int MAX_CYCLES= 20_000_000;

    localparam int TX_C  = `TB_CC;
    localparam int TX_H  = `TB_HH;
    localparam int TX_W  = `TB_WW;
    localparam int TX_SH = `TB_SHIFT;

    logic [7:0] mem [0:MAXM-1];

    logic ap_clk = 1'b0, ap_rst_n;
    logic ctrl_start, ctrl_done, ctrl_busy;
    logic [15:0] cfg_c, cfg_h, cfg_w;
    logic [5:0]  cfg_slope_shift, cfg_shift;
    logic [31:0] base_in, base_zeta, base_gamma, base_slope, base_out;
    logic        mem_rd_en;
    logic [31:0] mem_rd_addr;
    logic [63:0] mem_rd_data;
    logic        mem_wr_en;
    logic [31:0] mem_wr_addr;
    logic [7:0]  mem_wr_data;

    always #5 ap_clk = ~ap_clk;

    always @(posedge ap_clk) begin
        if (mem_rd_en)
            mem_rd_data <= {mem[mem_rd_addr+7], mem[mem_rd_addr+6],
                            mem[mem_rd_addr+5], mem[mem_rd_addr+4],
                            mem[mem_rd_addr+3], mem[mem_rd_addr+2],
                            mem[mem_rd_addr+1], mem[mem_rd_addr+0]};
        if (mem_wr_en) mem[mem_wr_addr] <= mem_wr_data;
    end

    bin_rprelu dut (
        .ap_clk(ap_clk), .ap_ce(1'b1), .ap_rst_n(ap_rst_n),
        .ctrl_start(ctrl_start), .ctrl_done(ctrl_done), .ctrl_busy(ctrl_busy),
        .cfg_c(cfg_c), .cfg_h(cfg_h), .cfg_w(cfg_w),
        .cfg_slope_shift(cfg_slope_shift), .cfg_shift(cfg_shift),
        .base_in(base_in), .base_zeta(base_zeta), .base_gamma(base_gamma),
        .base_slope(base_slope), .base_out(base_out),
        .mem_rd_en(mem_rd_en), .mem_rd_addr(mem_rd_addr), .mem_rd_data(mem_rd_data),
        .mem_wr_en(mem_wr_en), .mem_wr_addr(mem_wr_addr), .mem_wr_data(mem_wr_data)
    );

    string gdir;
    int fh, i, cyc, nbad, total;

    task automatic load_file(input string fn, input int base, output int nbytes);
        fh = $fopen({gdir, "/", fn}, "rb");
        if (fh == 0) $fatal(1, "[TB] FAIL: 打不开 %s/%s（先跑 tools/gen_bin_golden.py --unit rprelu --all）", gdir, fn);
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
        int nb_in, nb_ze, nb_ga, nb_sl, nb_ex;
        if (!$value$plusargs("GOLDEN=%s", gdir))
            $fatal(1, "[TB] 缺少 +GOLDEN=<dir>");
        for (i = 0; i < MAXM; i++) mem[i] = 8'h00;
        load_file("in_c8.bin",      BASE_IN,    nb_in);
        load_file("zeta.bin",       BASE_ZETA,  nb_ze);
        load_file("gamma.bin",      BASE_GAMMA, nb_ga);
        load_file("slope.bin",      BASE_SLOPE, nb_sl);
        load_file("expected_c8.bin",BASE_EXP,   nb_ex);

        cfg_c <= TX_C[15:0]; cfg_h <= TX_H[15:0]; cfg_w <= TX_W[15:0];
        cfg_slope_shift <= 6'd8; cfg_shift <= TX_SH[5:0];
        base_in <= BASE_IN; base_zeta <= BASE_ZETA; base_gamma <= BASE_GAMMA;
        base_slope <= BASE_SLOPE; base_out <= BASE_OUT;

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
        nbad  = 0;
        for (int a = 0; a < total; a++)
            if (mem[BASE_OUT + a] !== mem[BASE_EXP + a]) begin
                if (nbad < 5)
                    $display("    差异 c=%0d y=%0d x=%0d: got=%0d exp=%0d",
                             a / (TX_H * TX_W) / 8 * 8 + a % 8,
                             (a / 8) % TX_H, (a / (8 * TX_H)) % TX_W,
                             $signed(mem[BASE_OUT + a]), $signed(mem[BASE_EXP + a]));
                nbad++;
            end

        $display("");
        $display("============================================================");
        $display(" [BIN_ENGINE] rprelu 验收报告");
        $display("============================================================");
        $display(" golden 目录 : %s", gdir);
        $display(" 形状        : C=%0d H=%0d W=%0d shift=%0d", TX_C, TX_H, TX_W, TX_SH);
        $display(" 逐字节一致  : %0d / %0d 字节", total - nbad, total);
        $display(" 拍数        : %0d", cyc);
        $display(" 结论        : %s", (nbad == 0) ? "PASS" : "FAIL");
        if (nbad != 0) $fatal(1, "[TB] FAIL：%0d 字节不一致", nbad);
        $display("[TB-BIN-SUMMARY] shape=%s bytes=%0d/%0d nbad=%0d cycles=%0d status=PASS",
                 gdir, total - nbad, total, nbad, cyc);
        $finish;
    end
endmodule
