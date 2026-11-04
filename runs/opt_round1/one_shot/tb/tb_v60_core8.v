// ============================================================================
//  tb_bin_conv_core8.v —— 8-lane 二值卷积核（bin_conv_core8）验收 TB
//
//  与官方的 tb_bin_conv_core.v 的区别只有一处：**写口是 64-bit + 8 位字节使能**
//  （8 个 lane 一拍吐 8 个相邻字节 = C8 布局的一个整字）。其余（golden 文件、
//  判据、输出格式）完全沿用，保证"逐字节"口径一致。
//
//  用法（由 tools/run_lane8.sh 调用）：
//      xsim snap -R +GOLDEN=<golden 目录> +SHAPE_NAME=<形状名>
//  环境：golden 目录里的 act_pack.bin / wgt_pack.bin / alpha.bin / bias.bin /
//        zeta.bin / gamma.bin / slope.bin / expected_c8.bin
// ============================================================================
`timescale 1ns/1ps
// ★ v60 派生：`tb_bin_conv_core_lane8.v` + 两处改动
//   (1) 新增 +SLOPESHIFT=<n>（默认 12）：cfg_slope_shift 不再写死 8
//   (2) DUT 的 R1_ROUND / OOB_ZERO 由编译宏控制（`-d R1_OFF` / `-d OOB_OFF`），
//       供 S4 变异测试：同一份 golden（v60 语义）跑"关掉补丁"的 RTL 必须变红。
module tb_v60_core8;

    localparam int MAXM  = 1 << 20;
    localparam int BASE_ACT   = 32'h0000_0000;
    localparam int BASE_WGT   = 32'h0002_0000;
    localparam int BASE_ALPHA = 32'h0004_0000;
    localparam int BASE_BIAS  = 32'h0004_1000;
    localparam int BASE_ZETA  = 32'h0004_2000;
    localparam int BASE_GAMMA = 32'h0004_3000;
    localparam int BASE_SLOPE = 32'h0004_4000;
    localparam int BASE_OUT   = 32'h0006_0000;
    localparam int BASE_EXP   = 32'h000E_0000;
    localparam int MAX_CYCLES = 20_000_000;
    localparam int    TX_CIN = `TB_CIN;
    localparam int    TX_COUT= `TB_COUT;
    localparam int    TX_K   = `TB_K;
    localparam int    TX_S   = `TB_S;
    localparam int    TX_D   = `TB_D;
    localparam int    TX_PAD = `TB_PAD;
    localparam int    TX_H   = `TB_H;
    localparam int    TX_W   = `TB_W;
    localparam int    TX_SH  = `TB_SHIFT;

    logic [7:0] mem [0:MAXM-1];

    logic        ap_clk = 1'b0;
    logic        ap_rst_n;
    logic        ctrl_start, ctrl_done, ctrl_busy;
    logic [15:0] cfg_cin, cfg_cout, cfg_k, cfg_s, cfg_d, cfg_pad, cfg_h, cfg_w;
    logic [5:0]  cfg_alpha_shift, cfg_slope_shift, cfg_shift;
    logic [31:0] base_act, base_wgt, base_alpha, base_bias;
    logic [31:0] base_zeta, base_gamma, base_slope, base_out;
    logic        mem_rd_en;
    logic [31:0] mem_rd_addr;
    logic [63:0] mem_rd_data;
    logic        mem_wr_en;
    logic [31:0] mem_wr_addr;
    logic [63:0] mem_wr_data;
    logic [7:0]  mem_wr_sel;

    always #5 ap_clk = ~ap_clk;

    int n_wr = 0, n_rd = 0, n_x = 0;
    int n_wrbyte = 0;
    // ---- 内存模型：读 1 拍延迟；写当拍生效（按字节使能）----
    always @(posedge ap_clk) begin
        if (mem_rd_en) begin
            n_rd <= n_rd + 1;
            mem_rd_data <= {mem[mem_rd_addr+7], mem[mem_rd_addr+6],
                            mem[mem_rd_addr+5], mem[mem_rd_addr+4],
                            mem[mem_rd_addr+3], mem[mem_rd_addr+2],
                            mem[mem_rd_addr+1], mem[mem_rd_addr+0]};
        end
        if (mem_wr_en) begin
            n_wr <= n_wr + 1;
            for (int b = 0; b < 8; b++)
                if (mem_wr_sel[b]) begin
                    mem[mem_wr_addr + b] <= mem_wr_data[8*b +: 8];
                    n_wrbyte = n_wrbyte + 1;
                end
        end
    end

`ifdef R1_OFF
    localparam bit TB_R1  = 1'b0;
`else
    localparam bit TB_R1  = 1'b1;
`endif
`ifdef OOB_OFF
    localparam bit TB_OOB = 1'b0;
`else
    localparam bit TB_OOB = 1'b1;
`endif

    bin_conv_core #(.R1_ROUND(TB_R1), .OOB_ZERO(TB_OOB)) dut (
        .ap_clk(ap_clk), .ap_ce(1'b1), .ap_rst_n(ap_rst_n),
        .ctrl_start(ctrl_start), .ctrl_done(ctrl_done), .ctrl_busy(ctrl_busy),
        .cfg_cin(cfg_cin), .cfg_cout(cfg_cout), .cfg_k(cfg_k),
        .cfg_s(cfg_s), .cfg_d(cfg_d), .cfg_pad(cfg_pad),
        .cfg_h(cfg_h), .cfg_w(cfg_w),
        .cfg_alpha_shift(cfg_alpha_shift), .cfg_slope_shift(cfg_slope_shift),
        .cfg_shift(cfg_shift),
        .base_act(base_act), .base_wgt(base_wgt),
        .base_alpha(base_alpha), .base_bias(base_bias),
        .base_zeta(base_zeta), .base_gamma(base_gamma),
        .base_slope(base_slope), .base_out(base_out),
        .mem_rd_en(mem_rd_en), .mem_rd_addr(mem_rd_addr), .mem_rd_data(mem_rd_data),
        .mem_wr_en(mem_wr_en), .mem_wr_addr(mem_wr_addr),
        .mem_wr_data(mem_wr_data), .mem_wr_sel(mem_wr_sel)
    );

    // ---- 写地址不变量：64-bit 整字写必须 8 字节对齐 ----
    int n_wr_misaligned = 0;
    always @(posedge ap_clk) begin
        if (mem_wr_en && (mem_wr_addr[2:0] != 3'd0)) n_wr_misaligned <= n_wr_misaligned + 1;
    end

    int SLOPESHIFT = 12;
    string gdir;
    int fh, nb, i, cyc, nbad, total;

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
        int Hout, Wout, nb_a, nb_w, nb_al, nb_bi, nb_ze, nb_ga, nb_sl, nb_ex;
        void'($value$plusargs("SLOPESHIFT=%d", SLOPESHIFT));
        if (!$value$plusargs("GOLDEN=%s", gdir))
            $fatal(1, "[TB] 缺少 +GOLDEN=<dir>（用 tools/run_lane8.sh 会自动带上）");
        Hout = (TX_H - 1) / TX_S + 1;
        Wout = (TX_W - 1) / TX_S + 1;

        for (i = 0; i < MAXM; i++) mem[i] = 8'h00;
        load_file("act_pack.bin",   BASE_ACT,   nb_a);
        load_file("wgt_pack.bin",   BASE_WGT,   nb_w);
        load_file("alpha.bin",      BASE_ALPHA, nb_al);
        load_file("bias.bin",       BASE_BIAS,  nb_bi);
        load_file("zeta.bin",       BASE_ZETA,  nb_ze);
        load_file("gamma.bin",      BASE_GAMMA, nb_ga);
        load_file("slope.bin",      BASE_SLOPE, nb_sl);
        load_file("expected_c8.bin",BASE_EXP,   nb_ex);

        cfg_cin <= TX_CIN[15:0]; cfg_cout <= TX_COUT[15:0]; cfg_k <= TX_K[15:0];
        cfg_s <= TX_S[15:0]; cfg_d <= TX_D[15:0]; cfg_pad <= TX_PAD[15:0];
        cfg_h <= TX_H[15:0]; cfg_w <= TX_W[15:0];
        cfg_alpha_shift <= 6'd9;
        cfg_slope_shift <= SLOPESHIFT[5:0];
        cfg_shift <= TX_SH[5:0];
        base_act <= BASE_ACT; base_wgt <= BASE_WGT;
        base_alpha <= BASE_ALPHA; base_bias <= BASE_BIAS;
        base_zeta <= BASE_ZETA; base_gamma <= BASE_GAMMA;
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

        total = TX_COUT * Hout * Wout;
        nbad  = 0;
        for (int o = 0; o < TX_COUT; o++)
            for (int y = 0; y < Hout; y++)
                for (int x = 0; x < Wout; x++) begin
                    int a = (o / 8) * Hout * Wout * 8 + y * Wout * 8 + x * 8 + (o % 8);
                    int got = mem[BASE_OUT + a];
                    int exp = mem[BASE_EXP + a];
                    if ((got === 8'hx) || (got === 8'hz)) n_x = n_x + 1;
                    if (got !== exp) begin
                        if (nbad < 8)
                            $display("    差异 o=%0d y=%0d x=%0d: got=%0d exp=%0d",
                                     o, y, x, $signed(got), $signed(exp));
                        nbad++;
                    end
                end

        $display("");
        $display("============================================================");
        $display(" [BIN_ENGINE-8LANE] 验收报告");
        $display("============================================================");
        $display(" golden 目录 : %s", gdir);
        $display(" 形状        : cin=%0d cout=%0d k=%0d s=%0d d=%0d p=%0d H=%0d W=%0d",
                 TX_CIN, TX_COUT, TX_K, TX_S, TX_D, TX_PAD, TX_H, TX_W);
        $display(" 逐字节一致  : %0d / %0d 字节", total - nbad, total);
        $display(" 拍数        : %0d", cyc);
        $display(" 参考拍数(1lane)=cout*Hout*Wout*K : %0d   ⇒ 加速 %0.2fx",
                 TX_COUT*Hout*Wout*((TX_CIN+63)/64)*TX_K*TX_K,
                 (1.0*TX_COUT*Hout*Wout*((TX_CIN+63)/64)*TX_K*TX_K)/cyc);
        $display(" 读次数 %0d / 写拍数 %0d / 写字节 %0d（期望 %0d）",
                 n_rd, n_wr, n_wrbyte, total);
        $display(" 未对齐写 %0d 拍", n_wr_misaligned);
        $display(" 结论        : %s", (nbad == 0) ? "PASS" : "FAIL");
        if (nbad != 0) $fatal(1, "[TB] FAIL：%0d 字节不一致", nbad);
        $display("[TB-BIN8-SUMMARY] shape=%s bytes=%0d/%0d nbad=%0d cycles=%0d status=PASS",
                 gdir, total - nbad, total, nbad, cyc);
        $finish;
    end
endmodule
