// ============================================================================
//  tb_conv_k5_engine.v —— 统一验收设施的 k5 最小 testbench
//
//  只做三件事：形状参数 + include 共享骨架 + 例化 DUT。
//  时钟/复位/AXI 从机/向量加载/逐字节比对/看门狗/[TB-SUMMARY] 全部由
//  tb/tb_common_tasks.vh 提供（契约 §3/§4）。
//
//  运行（父 agent 统一跑）：
//    tools/run_module.sh conv_k5_engine k5_12x48
//    tools/run_module.sh conv_k5_engine k5_12x48 --fuzz 200
//
//  形状由 run_module.sh 从 golden/<shape>/meta.json 派生
//  （-d TB_CIN/TB_COUT/TB_H/TB_W/TB_PAD ...），下面的默认值只在手工
//  编译时兜底。注意：/data/work/tb_c8_k5_12x48 是 H=2, W=320 的条带，
//  不是 320×320 整层；整层吞吐的目标拍数见 docs/rtl_hand/conv_k5_engine.md。
// ============================================================================
`timescale 1ns / 1ps

`ifndef TB_CIN
  `define TB_CIN 12
`endif
`ifndef TB_COUT
  `define TB_COUT 48
`endif
`ifndef TB_W
  `define TB_W 320
`endif
`ifndef TB_H
  `define TB_H 2
`endif
`ifndef TB_PAD
  `define TB_PAD 2
`endif

// 全局看门狗阈值由 tb_common_tasks.vh 提供（64'd600000000000，600 s）。
// 不要在 tb 里用未带位宽的 60000000000 覆盖：它会被截成 32 位负数。

module tb_conv_k5_engine;
    `define TB_MODULE_NAME "conv_k5_engine"
    `include "tb_common_tasks.vh"

    conv_k5_engine #(
        .CIN  (`TB_CIN),
        .COUT (`TB_COUT),
        .W    (`TB_W),
        .H    (`TB_H),
        .PAD  (`TB_PAD)
    ) dut (
        .ap_clk   (ap_clk),
        .ap_rst_n (ap_rst_n),

        // 读口 0：输入
        .m_axi_in_arvalid (m_axi_in_arvalid),
        .m_axi_in_arready (m_axi_in_arready),
        .m_axi_in_araddr  (m_axi_in_araddr),
        .m_axi_in_arlen   (m_axi_in_arlen),
        .m_axi_in_rvalid  (m_axi_in_rvalid),
        .m_axi_in_rready  (m_axi_in_rready),
        .m_axi_in_rdata   (m_axi_in_rdata),
        .m_axi_in_rlast   (m_axi_in_rlast),

        // 读口 1：权重
        .m_axi_w_arvalid  (m_axi_w_arvalid),
        .m_axi_w_arready  (m_axi_w_arready),
        .m_axi_w_araddr   (m_axi_w_araddr),
        .m_axi_w_arlen    (m_axi_w_arlen),
        .m_axi_w_rvalid   (m_axi_w_rvalid),
        .m_axi_w_rready   (m_axi_w_rready),
        .m_axi_w_rdata    (m_axi_w_rdata),
        .m_axi_w_rlast    (m_axi_w_rlast),

        // 读口 2：bias
        .m_axi_b_arvalid  (m_axi_b_arvalid),
        .m_axi_b_arready  (m_axi_b_arready),
        .m_axi_b_araddr   (m_axi_b_araddr),
        .m_axi_b_arlen    (m_axi_b_arlen),
        .m_axi_b_rvalid   (m_axi_b_rvalid),
        .m_axi_b_rready   (m_axi_b_rready),
        .m_axi_b_rdata    (m_axi_b_rdata),
        .m_axi_b_rlast    (m_axi_b_rlast),

        // 写口：输出
        .m_axi_out_awvalid(m_axi_out_awvalid),
        .m_axi_out_awready(m_axi_out_awready),
        .m_axi_out_awaddr (m_axi_out_awaddr),
        .m_axi_out_awlen  (m_axi_out_awlen),
        .m_axi_out_wvalid (m_axi_out_wvalid),
        .m_axi_out_wready (m_axi_out_wready),
        .m_axi_out_wdata  (m_axi_out_wdata),
        .m_axi_out_wstrb  (m_axi_out_wstrb),
        .m_axi_out_wlast  (m_axi_out_wlast),
        .m_axi_out_bvalid (m_axi_out_bvalid),
        .m_axi_out_bready (m_axi_out_bready),

        // 控制
        .ctrl_in_addr   (ctrl_in_addr),
        .ctrl_wgt_addr  (ctrl_wgt_addr),
        .ctrl_bias_addr (ctrl_bias_addr),
        .ctrl_out_addr  (ctrl_out_addr),
        .ctrl_shift     (ctrl_shift),
        .ctrl_cin       (ctrl_cin),
        .ctrl_cout      (ctrl_cout),
        .ctrl_h         (ctrl_h),
        .ctrl_w         (ctrl_w),
        .ctrl_start     (ctrl_start),
        .ctrl_done      (ctrl_done),
        .ctrl_busy      (ctrl_busy)
    );
endmodule
