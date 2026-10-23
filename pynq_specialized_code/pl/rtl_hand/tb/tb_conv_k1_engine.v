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
//  编译期参数只是上界：CIN_MAX=64、COUT_MAX=64、LANES=32。
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

    conv_k1_engine #(
        .CIN(64), .COUT(64), .W(320), .H(320),
        .PAD(0), .STRIDE(1), .DIL(1), .RELU(1'b1),
        .LANES(32), .CIN_MAX(64), .COUT_MAX(64)
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
        .ctrl_start(ctrl_start),       .ctrl_done(ctrl_done), .ctrl_busy(ctrl_busy)
    );
endmodule
