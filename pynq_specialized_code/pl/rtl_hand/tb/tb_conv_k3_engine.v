// ============================================================================
//  tb_conv_k3_engine.v —— conv_k3_engine 的统一验收 testbench
//
//  按共享设施模板编写（同 tb_conv_k1_engine.v）：时钟/复位/三路 AXI4 读从机/
//  一路 AXI4 写从机/向量加载/逐字节比对/[TB-SUMMARY]/看门狗全部由
//  `tb/tb_common_tasks.vh` 提供；本文件只做 `define + 三个额外 ctrl 端口 + 例化。
//
//  运行（由 tools/run_module.sh 驱动）：
//    cd rtl_hand
//    ./tools/run_module.sh conv_k3_engine --golden /tmp/k3_dbg/k3_min
//    ./tools/run_module.sh conv_k3_engine --fuzz-manifest /tmp/k3_dbg/manifest.txt
//    ./tools/run_module.sh conv_k3_engine --fuzz-manifest /tmp/k3_real/manifest.txt
//    ./tools/run_module.sh conv_k3_engine --compile-only
//
//  形状全部来自 golden/<case>/meta.json（契约 §4）：
//    cin/cout/k/s/d/pad/H/W/relu → 共享 TB 读进 tb_cin..tb_relu，
//    所以 **一个 tb 覆盖全部 114 层的形状**，不需要按形状重新编译。
//  本模块额外需要两个运行期参数（主控 2026-10-26 批准的契约外新增）：
//    ctrl_stride ← meta.json 的 s （1/2）
//    ctrl_dil    ← meta.json 的 d （1/2/3）
//
//  拍数口径：ctrl_start 拉高后的第一个 posedge 起，到采样 ctrl_done 止
//  （共享 TB 的 drive_start()）。目标值由 tools/model_k3_cycles.py 的
//  `--layer` 输出给出，用 --target N 传入。
//
//  诊断计数器（DUT 输出，仅打印）：dbg_stall_fill / dbg_stall_mac
//    · stall_fill 占主导 ⇒ 填充（DDR 供数）受限，见设计文档 §18.3；
//    · stall_mac  应恒为 0（内层 II=1 的判据）。
// ============================================================================
`timescale 1ns / 1ps

module tb_conv_k3_engine;
    `define TB_MODULE_NAME "conv_k3_engine"
    `include "tb_common_tasks.vh"

    // ---- 契约外新增的三个运行期控制（relu 同 k1；stride/dil 见设计文档 §15）----
    logic        ctrl_relu;
    logic [3:0]  ctrl_stride;
    logic [3:0]  ctrl_dil;

    assign ctrl_relu   = (tb_relu != 0);
    assign ctrl_stride = tb_s[3:0];
    assign ctrl_dil    = tb_d[3:0];

    logic [31:0] dbg_stall_fill;
    logic [31:0] dbg_stall_mac;

    conv_k3_engine #(
        // 2026-10-28：同步瘦身后的参数
        .LANES(8), .CIN_MAX(192), .COUT_MAX(32),
        .W_MAX(320), .H_MAX(320), .CAP_B(4608)
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

        .ctrl_in_addr(ctrl_in_addr),     .ctrl_wgt_addr(ctrl_wgt_addr),
        .ctrl_bias_addr(ctrl_bias_addr), .ctrl_out_addr(ctrl_out_addr),
        .ctrl_shift(ctrl_shift),         .ctrl_cin(ctrl_cin),
        .ctrl_cout(ctrl_cout),           .ctrl_h(ctrl_h), .ctrl_w(ctrl_w),
        .ctrl_stride(ctrl_stride),       .ctrl_dil(ctrl_dil),
        .ctrl_relu(ctrl_relu),           .ctrl_start(ctrl_start),
        .ctrl_done(ctrl_done),           .ctrl_busy(ctrl_busy),

        .dbg_stall_fill(dbg_stall_fill), .dbg_stall_mac(dbg_stall_mac)
    );

    // 每个 case 结束时打印诊断（不算进 [TB-SUMMARY]，只做人工/脚本观察）
    always @(posedge ctrl_done) begin
        $display("[K3-DIAG] stall_fill=%0d stall_mac=%0d cycles=%0d（cin=%0d cout=%0d %0dx%0d s=%0d d=%0d pad=%0d）",
                 dbg_stall_fill, dbg_stall_mac, tb_cycles,
                 tb_cin, tb_cout, tb_h, tb_w, tb_s, tb_d, tb_pad);
    end

    // 内层 II=1 的硬判据：stall_mac 必须为 0（否则说明抽头/输出 FIFO 掉了拍）
    // synopsys translate_off
    always @(negedge ctrl_done) begin
        if (dbg_stall_mac != 0)
            $display("[K3-DIAG] WARNING: stall_mac=%0d != 0 ⇒ 内层没做到 II=1（查抽头预取时序）",
                     dbg_stall_mac);
    end
    // synopsys translate_on

endmodule
