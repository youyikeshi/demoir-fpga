// ============================================================================
//  wrap/bin_conv_core64.v —— 只给"包装层 64-bit 写口"联调用的薄壳
//
//  模块名仍然叫 bin_conv_core（包装层按这个名字例化），但写口是
//  mem_wr_data[63:0] + mem_wr_sel[7:0] —— 配合 patch/bin_unit_axi_wrap_wr64.patch。
//  正式落地时**不要**用这个文件：直接把 bin_conv_core8.v 当二进制核、
//  把 bin_conv_core.v（端口兼容版）留给"包装层不能动"的退路。
// ============================================================================
`timescale 1ns/1ps
module bin_conv_core #(
    parameter int LANES    = 64,
    parameter int COUT_MAX = 192,
    // v60：透传数值补丁开关（默认 = v60 语义；=0 时逐字节回到 v58）
    parameter bit R1_ROUND = 1'b1,
    parameter bit OOB_ZERO = 1'b1
)(
    input  logic        ap_clk,
    input  logic        ap_rst_n,
    input  logic        ap_ce,
    input  logic        ctrl_start,
    output logic        ctrl_done,
    output logic        ctrl_busy,
    input  logic [15:0] cfg_cin, cfg_cout, cfg_k, cfg_s, cfg_d, cfg_pad,
    input  logic [15:0] cfg_h, cfg_w,
    input  logic [5:0]  cfg_alpha_shift, cfg_slope_shift, cfg_shift,
    input  logic [31:0] base_act, base_wgt, base_alpha, base_bias,
    input  logic [31:0] base_zeta, base_gamma, base_slope, base_out,
    output logic        mem_rd_en,
    output logic [31:0] mem_rd_addr,
    input  logic [63:0] mem_rd_data,
    output logic        mem_wr_en,
    output logic [31:0] mem_wr_addr,
    output logic [63:0] mem_wr_data,
    output logic [7:0]  mem_wr_sel
);
    bin_conv_core8 #(.LANES(LANES), .COUT_MAX(COUT_MAX),
                     .R1_ROUND(R1_ROUND), .OOB_ZERO(OOB_ZERO)) u_eng (
        .ap_clk(ap_clk), .ap_rst_n(ap_rst_n), .ap_ce(ap_ce),
        .ctrl_start(ctrl_start), .ctrl_done(ctrl_done), .ctrl_busy(ctrl_busy),
        .cfg_cin(cfg_cin), .cfg_cout(cfg_cout), .cfg_k(cfg_k), .cfg_s(cfg_s),
        .cfg_d(cfg_d), .cfg_pad(cfg_pad), .cfg_h(cfg_h), .cfg_w(cfg_w),
        .cfg_alpha_shift(cfg_alpha_shift), .cfg_slope_shift(cfg_slope_shift),
        .cfg_shift(cfg_shift),
        .base_act(base_act), .base_wgt(base_wgt), .base_alpha(base_alpha),
        .base_bias(base_bias), .base_zeta(base_zeta), .base_gamma(base_gamma),
        .base_slope(base_slope), .base_out(base_out),
        .mem_rd_en(mem_rd_en), .mem_rd_addr(mem_rd_addr), .mem_rd_data(mem_rd_data),
        .mem_wr_en(mem_wr_en), .mem_wr_addr(mem_wr_addr),
        .mem_wr_data(mem_wr_data), .mem_wr_sel(mem_wr_sel)
    );
endmodule
