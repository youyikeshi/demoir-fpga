// ============================================================================
//  tb_conv_k1_engine.v —— conv_k1_engine 的统一验收 testbench
//
//  按共享设施模板（tb/tb__selftest_ok.v）编写：时钟/复位/三路 AXI4 读从机/
//  一路 AXI4 写从机/向量加载/逐字节比对/[TB-SUMMARY]/MAX_CYCLES 看门狗全部由
//  `tb/tb_common_tasks.vh` 提供，本文件只做 `define + include + 例化 DUT。
//
//  运行（由 tools/run_module.sh 驱动，勿手工改 plusarg 名）：
//    cd rtl_hand && ./tools/run_module.sh conv_k1_engine k1_64x64
//    cd rtl_hand && ./tools/run_module.sh conv_k1_engine k1_64x64 --fuzz 200
//    cd rtl_hand && ./tools/run_module.sh conv_k1_engine k1_64x64 --compile-only
//
//  DUT 形状：运行期 cin/cout/H/W 走 ctrl_* 寄存器（来自 golden meta.json），
//  编译期参数只是上界：CIN_MAX=352、COUT_MAX=192、LANES=32。
//  因此 k1_64x64（cin=64, cout=64, H=2, W=320）直接可用。
//
//  拍数口径（详见 docs/rtl_hand/conv_k1_engine.md）：
//    本设计稳态每 (pixel, 32-oc 块) = cin 拍，收尾 3 拍与下一块重叠；
//    k1_64x64 理论值 = 640 pixel × 2 块 × 64 ic = 81,920 拍（+ 装载/排空）。
//    tools/targets.tsv 里 k1_64x64 的目标（531,437）是 HLS cosim 基线，
//    用来判 "≤1.2× 基线"。
// ============================================================================
`timescale 1ns / 1ps

module tb_conv_k1_engine;
    `define TB_MODULE_NAME "conv_k1_engine"
    `include "tb_common_tasks.vh"

    // 运行期 ReLU：由 golden 的 meta.json 的 relu 字段（共享 TB 读入 tb_relu）驱动。
    // 所以 relu=0 的用例只需要 meta.json 里 "relu":0，不需要新的 plusarg。
    logic ctrl_relu;
    assign ctrl_relu = (tb_relu != 0);

    conv_k1_engine #(
        .CIN(64), .COUT(64), .W(320), .H(320),
        .PAD(0), .STRIDE(1), .DIL(1), .RELU(1'b1),
        .LANES(32), .CIN_MAX(192), .COUT_MAX(96)
    ) dut (
        .ap_clk(ap_clk), .ap_rst_n(ap_rst_n),

        .m_axi_in_arvalid(m_axi_in_arvalid), .m_axi_in_arready(m_axi_in_arready),
        .m_axi_in_araddr(m_axi_in_araddr),   .m_axi_in_arlen(m_axi_in_arlen),
        .m_axi_in_rvalid(m_axi_in_rvalid),   .m_axi_in_rready(m_axi_in_rready),
        .m_axi_in_rdata(m_axi_in_rdata),     .m_axi_in_rlast(m_axi_in_rlast),

        .m_axi_w_arvalid(m_axi_w_arvalid),   .m_axi_w_arready(m_axi_w_arready),
        .m_axi_w_araddr(m_axi_w_araddr),     .m_axi_w_arlen(m_axi_w_arlen),
        .m_axi_w_rvalid(m_axi_w_rvalid),     .m_axi_w_rready(m_axi_w_rready),
        .m_axi_w_rdata(m_axi_w_rdata),       .m_axi_w_rlast(m_axi_w_rlast),

        .m_axi_b_arvalid(m_axi_b_arvalid),   .m_axi_b_arready(m_axi_b_arready),
        .m_axi_b_araddr(m_axi_b_araddr),     .m_axi_b_arlen(m_axi_b_arlen),
        .m_axi_b_rvalid(m_axi_b_rvalid),     .m_axi_b_rready(m_axi_b_rready),
        .m_axi_b_rdata(m_axi_b_rdata),       .m_axi_b_rlast(m_axi_b_rlast),

        .m_axi_out_awvalid(m_axi_out_awvalid), .m_axi_out_awready(m_axi_out_awready),
        .m_axi_out_awaddr(m_axi_out_awaddr),   .m_axi_out_awlen(m_axi_out_awlen),
        .m_axi_out_wvalid(m_axi_out_wvalid),   .m_axi_out_wready(m_axi_out_wready),
        .m_axi_out_wdata(m_axi_out_wdata),     .m_axi_out_wstrb(m_axi_out_wstrb),
        .m_axi_out_wlast(m_axi_out_wlast),
        .m_axi_out_bvalid(m_axi_out_bvalid),   .m_axi_out_bready(m_axi_out_bready),

        .ctrl_in_addr(ctrl_in_addr),   .ctrl_wgt_addr(ctrl_wgt_addr),
        .ctrl_bias_addr(ctrl_bias_addr), .ctrl_out_addr(ctrl_out_addr),
        .ctrl_shift(ctrl_shift),       .ctrl_cin(ctrl_cin),
        .ctrl_cout(ctrl_cout),         .ctrl_h(ctrl_h), .ctrl_w(ctrl_w),
        .ctrl_start(ctrl_start),       .ctrl_relu(ctrl_relu),
        .ctrl_done(ctrl_done),         .ctrl_busy(ctrl_busy)
    );

    // ================= 诊断：issue 占空比 + 状态机卡死检测（2026-10-28）=====
    //  ① 占空比：确认引擎自身是否 II=1（k1 上板慢 8x 时，这里显示 99%，
    //     直接证明问题不在引擎逻辑，而在总线侧）。
    //  ② 卡死检测：state 不变超过 THRESH 拍就一次打全所有相关寄存器并结束。
    //     教训：只靠"仿真超时"没有波形看不出卡在哪，白耗几轮。
    integer d_cyc = 0, d_iss = 0, d_pix = 0, d_blk = 0, d_oth = 0;
    logic [2:0] d_state_q = 3'd0;
    integer     d_same = 0;
    localparam int STUCK_THRESH = 200000;

    always @(posedge ap_clk) begin
        if (ap_rst_n) begin
            d_cyc = d_cyc + 1;
            if (dut.state == dut.ST_PASS) begin
                if (dut.issue_en) d_iss = d_iss + 1;
                else if (!dut.pix_ready[dut.cur_buf]) d_pix = d_pix + 1;
                else if (!dut.blk_room)               d_blk = d_blk + 1;
                else                                  d_oth = d_oth + 1;
            end
            if (dut.state !== d_state_q) begin d_state_q <= dut.state; d_same = 0; end
            else if (d_same < STUCK_THRESH) d_same = d_same + 1;
            else begin
                $display("[K1-STUCK] state=%0d 停 %0d 拍 | pix_cur=%0d pix_total=%0d pass_done=%b",
                         dut.state, d_same, dut.pix_cur, dut.pix_total_r, dut.pass_done);
                $display("           ar_pix=%0d rd_pix=%0d ar=%0d rd=%0d ready=%b cur_buf=%0d",
                         dut.f_ar_pix, dut.f_rd_pix, dut.f_ar, dut.f_rd,
                         dut.pix_ready, dut.cur_buf);
                $display("           ic_cur=%0d issue_en=%b ob_cnt=%0d blk_room=%b push=%b",
                         dut.ic_cur, dut.issue_en, dut.ob_cnt, dut.blk_room, dut.push_vld);
                $display("           wl_state=%0d bank_ready=%b bias_ready=%b cin_r=%0d cout_r=%0d",
                         dut.wl_state, dut.bank_ready, dut.bias_ready, dut.cin_r, dut.cout_r);
                $finish;
            end
        end
    end

    final begin
        $display("[K1-DUTY] 总周期=%0d ST_PASS_issue=%0d pixwait=%0d blkwait=%0d other=%0d",
                 d_cyc, d_iss, d_pix, d_blk, d_oth);
        $display("[K1-DUTY] 占空比=%0d%%", (d_iss*100)/((d_iss+d_pix+d_blk+d_oth)==0?1:(d_iss+d_pix+d_blk+d_oth)));
    end
endmodule
