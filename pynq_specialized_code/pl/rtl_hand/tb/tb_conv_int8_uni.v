// ============================================================================
//  tb_conv_int8_uni.v —— 定制 int8 卷积引擎的验收 TB（自包含）
//
//  规格：docs/binary/04_定制int8引擎规格.md
//  设计要点：**k / s / d / H / W 全部运行时配置**，没有编译期特殊化
//  （老 k5 把 W 焊死导致换分辨率就得重新综合，这里不允许重犯）。
//
//  内存映射（字节地址，相对 base_*）：
//    输入   base_in  + addr_c8(c,y,x)         int8，C8 交错，通道补齐到 8
//    权重   base_wgt + ((o*cin + c)*TAP + j)  TAP = 4/12/28（按 k）
//    偏置   base_bias+ o*4                    int32
//    输出   base_out + addr_c8(o,y,x)         int8，C8 交错
//  其中 addr_c8(c,y,x) = (c/8)*H*W*8 + y*W*8 + x*8 + (c%8)
// ============================================================================
`timescale 1ns/1ps
module tb_conv_int8_uni;
    // ⚠ MAXM 必须 > B_EXP + 最大输出（B_EXP=0x280000=2,621,440），
    //   原来写 1<<21=2,097,152 ⇒ 期望区落在数组外 ⇒ 全部判不等。
    localparam int MAXM = 1 << 22;
    localparam int B_IN  = 32'h0000_0000;
    localparam int B_WGT = 32'h0010_0000;
    localparam int B_BIAS= 32'h0018_0000;
    localparam int B_OUT = 32'h001C_0000;
    localparam int B_EXP = 32'h0028_0000;
    localparam int MAX_CYCLES = 30_000_000;

    localparam int T_CIN = `TB_CIN;
    localparam int T_COUT= `TB_COUT;
    localparam int T_K   = `TB_K;
    localparam int T_S   = `TB_S;
    localparam int T_D   = `TB_D;
    localparam int T_PAD = `TB_PAD;
    localparam int T_H   = `TB_H;
    localparam int T_W   = `TB_W;
    localparam int T_SH  = `TB_SHIFT;
    localparam int T_RELU= `TB_RELU;
    localparam int T_TAP = `TB_TAPSTRIDE;

    logic [7:0] mem [0:MAXM-1];
    logic ap_clk = 1'b0, ap_rst_n;
    logic ctrl_start, ctrl_done, ctrl_busy;
    logic [15:0] cfg_cin, cfg_cout, cfg_k, cfg_s, cfg_d, cfg_pad, cfg_h, cfg_w;
    logic [7:0]  cfg_tap_stride;
    logic [5:0]  cfg_shift;
    logic        cfg_relu;
    logic [31:0] base_in, base_wgt, base_bias, base_out;
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

    conv_int8_uni dut (
        .ap_clk(ap_clk), .ap_ce(1'b1), .ap_rst_n(ap_rst_n),
        .ctrl_start(ctrl_start), .ctrl_done(ctrl_done), .ctrl_busy(ctrl_busy),
        .cfg_cin(cfg_cin), .cfg_cout(cfg_cout), .cfg_k(cfg_k),
        .cfg_s(cfg_s), .cfg_d(cfg_d), .cfg_pad(cfg_pad),
        .cfg_h(cfg_h), .cfg_w(cfg_w),
        .cfg_tap_stride(cfg_tap_stride), .cfg_shift(cfg_shift),
        .cfg_relu(cfg_relu),
        .base_in(base_in), .base_wgt(base_wgt), .base_bias(base_bias),
        .base_out(base_out),
        .mem_rd_en(mem_rd_en), .mem_rd_addr(mem_rd_addr), .mem_rd_data(mem_rd_data),
        .mem_wr_en(mem_wr_en), .mem_wr_addr(mem_wr_addr), .mem_wr_data(mem_wr_data)
    );

    string gdir;
    int fh, i, cyc, nbad, total;
    task automatic load_file(input string fn, input int base, output int nbytes);
        fh = $fopen({gdir, "/", fn}, "rb");
        if (fh == 0) $fatal(1, "[TB] FAIL: 打不开 %s/%s（先跑 tools/gen_uni_golden.py --all）", gdir, fn);
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
        int nb1, nb2, nb3, nb4, Hout, Wout, co8;
        if (!$value$plusargs("GOLDEN=%s", gdir)) $fatal(1, "[TB] 缺少 +GOLDEN=");
        Hout = (T_H - 1) / T_S + 1;
        Wout = (T_W - 1) / T_S + 1;
        co8  = ((T_COUT + 7) / 8) * 8;
        for (i = 0; i < MAXM; i++) mem[i] = 8'h00;
        load_file("input_c8.bin",    B_IN,   nb1);
        load_file("weight.bin",      B_WGT,  nb2);
        load_file("bias.bin",        B_BIAS, nb3);
        load_file("expected_c8.bin", B_EXP,  nb4);

        cfg_cin <= T_CIN[15:0]; cfg_cout <= T_COUT[15:0];
        cfg_k <= T_K[15:0]; cfg_s <= T_S[15:0]; cfg_d <= T_D[15:0];
        cfg_pad <= T_PAD[15:0]; cfg_h <= T_H[15:0]; cfg_w <= T_W[15:0];
        cfg_tap_stride <= T_TAP[7:0]; cfg_shift <= T_SH[5:0];
        cfg_relu <= (T_RELU != 0);
        base_in <= B_IN; base_wgt <= B_WGT; base_bias <= B_BIAS; base_out <= B_OUT;

        ap_rst_n <= 1'b0; ctrl_start <= 1'b0;
        repeat (8) @(posedge ap_clk);
        ap_rst_n <= 1'b1;
        repeat (4) @(posedge ap_clk);
        ctrl_start <= 1'b1; @(posedge ap_clk); ctrl_start <= 1'b0;
        cyc = 0;
        while (!ctrl_done && cyc < MAX_CYCLES) begin @(negedge ap_clk); cyc++; end
        if (!ctrl_done) $fatal(1, "[TB] FAIL: ctrl_done 超时（%0d 拍）", cyc);
        repeat (4) @(posedge ap_clk);

        total = co8 * Hout * Wout;
        nbad = 0;
        for (int a = 0; a < total; a++)
            if (mem[B_OUT + a] !== mem[B_EXP + a]) begin
                if (nbad < 5)
                    $display("    差异 idx=%0d (oc=%0d y=%0d x=%0d): got=%0d exp=%0d",
                             a, (a/(Hout*Wout))/8*8 + a%8,
                             (a/8)%Hout, (a/(8*Hout))%Wout,
                             $signed(mem[B_OUT + a]), $signed(mem[B_EXP + a]));
                nbad++;
            end
        $display("");
        $display("============================================================");
        $display(" [INT8_UNI] 验收报告");
        $display("============================================================");
        $display(" golden : %s", gdir);
        $display(" 形状   : cin=%0d cout=%0d k=%0d s=%0d d=%0d pad=%0d H=%0d W=%0d tap=%0d",
                 T_CIN, T_COUT, T_K, T_S, T_D, T_PAD, T_H, T_W, T_TAP);
        $display(" 逐字节一致 : %0d / %0d 字节", total - nbad, total);
        $display(" 拍数   : %0d   （读 %0d / 写 %0d）", cyc, n_rd, n_wr);
        $display(" 结论   : %s", (nbad == 0) ? "PASS" : "FAIL");
        if (nbad != 0) $fatal(1, "[TB] FAIL：%0d 字节不一致", nbad);
        $display("[TB-BIN-SUMMARY] shape=%s bytes=%0d/%0d nbad=%0d cycles=%0d status=PASS",
                 gdir, total - nbad, total, nbad, cyc);
        $finish;
    end
endmodule
