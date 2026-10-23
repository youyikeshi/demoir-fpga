// 自测 tb：假模块写全 0，与 golden/_selftest_zero 的 expected 逐字节一致 → PASS
`timescale 1ns / 1ps

module tb__selftest_ok;
    `define TB_MODULE_NAME "_selftest_ok"
    `include "tb_common_tasks.vh"

    fake_engine #(.OUT_BYTES(30720), .INJECT_ERROR(1'b0)) dut (
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
