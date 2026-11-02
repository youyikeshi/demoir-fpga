// ============================================================================
//  bin_interp_v50.v —— bin_interp 的 v50 适配壳（R6）
//  owner: IMP-B   契约：docs/V50_ARCHITECTURE.md §2 D5、§4 R6、§5 ⑧
// ----------------------------------------------------------------------------
//  改动**只有两种**：
//    ① 旧端口一个不改（FUSE_MODE=0 时逐位等于 v39）；
//    ② 新增 band 原地通路（op_en=1）：把原模块的 mem_* 直接接到 band 口。
//       band 与 DDR 在地址语义上完全同构（1 字节 = 1 通道、C8 交错），所以
//       **不需要重写任何算术**，原模块就是"在 band 上原地做"的那台机器。
//
//  为什么这么做（而不是把算子重写成流式）：
//    v39 的 bin_interp 已经过 xsim 逐字节验证；重写一遍算术 = 重新引入 4 类
//    requant/边界的错。融合要的是"中间张量不落 DDR"，不是"把算子重写"。
//    把 mem_* 换成 band 口，正好只动 D1 要动的那一处。
//
//  op_en=0 / op_done：bin_interp 是纯 combinational-address 的流式单元，op_en=1 时
//  它的 ctrl_done 直接就是 op_done。
// ============================================================================
`timescale 1ns/1ps

module bin_interp_v50 (
    input  logic        ap_clk,
    input  logic        ap_rst_n,
    input  logic        ap_ce,
    input  logic        ctrl_start,
    output logic        ctrl_done,
    output logic        ctrl_busy,
    input  logic [15:0] cfg_c, cfg_h, cfg_w, cfg_ho, cfg_wo,
    input  logic [31:0] base_in, base_out,
    output logic        mem_rd_en,
    output logic [31:0] mem_rd_addr,
    input  logic [63:0] mem_rd_data,
    output logic        mem_wr_en,
    output logic [31:0] mem_wr_addr,
    output logic [7:0]  mem_wr_data,
    // ---- ★ v50 新增：band 原地通路 ----
    input  logic        op_en,            // 1 = 走 band 原地通路（0 = v39 老路径）
    output logic        band_rd_en,
    output logic [31:0] band_rd_addr,
    input  logic [63:0] band_rd_data,
    output logic        band_wr_en,
    output logic [31:0] band_wr_addr,
    output logic [63:0] band_wr_data,
    output logic [7:0]  band_wr_sel
);
    // ------------------------------------------------------------------
    // 原模块的 mem_* 接内部线；外露的 mem_* / band_* 由 op_en 决定
    // ------------------------------------------------------------------
    logic        u_rd_en;
    logic [31:0] u_rd_addr;
    logic [63:0] u_rd_data;
    logic        u_wr_en;
    logic [31:0] u_wr_addr;
    logic [7:0]  u_wr_data;
    bin_interp u_legacy (
        .ap_clk(ap_clk), .ap_rst_n(ap_rst_n), .ap_ce(ap_ce),
        .ctrl_start(ctrl_start), .ctrl_done(ctrl_done), .ctrl_busy(ctrl_busy),
        .cfg_c(cfg_c), .cfg_h(cfg_h), .cfg_w(cfg_w), .cfg_ho(cfg_ho), .cfg_wo(cfg_wo),
        .base_in(base_in), .base_out(base_out),
        .mem_rd_en(u_rd_en), .mem_rd_addr(u_rd_addr), .mem_rd_data(u_rd_data),
        .mem_wr_en(u_wr_en), .mem_wr_addr(u_wr_addr), .mem_wr_data(u_wr_data)
    );

    // ---- 旧口：op_en=0 时原样外露（逐位 = v39）----
    assign mem_rd_en   = op_en ? 1'b0 : u_rd_en;
    assign mem_rd_addr = u_rd_addr;
    assign mem_wr_en   = op_en ? 1'b0 : u_wr_en;
    assign mem_wr_addr = u_wr_addr;
    assign mem_wr_data = u_wr_data;

    // ---- 读数据：op_en=1 从 band 取 ----
    assign u_rd_data   = op_en ? band_rd_data : mem_rd_data;

    // ---- band 口 ----
    assign band_rd_en   = op_en ? u_rd_en : 1'b0;
    assign band_rd_addr = u_rd_addr;
    assign band_wr_en   = op_en ? u_wr_en : 1'b0;
    assign band_wr_addr = u_wr_addr;
    // 单字节写口 → band 的字节车道
    assign band_wr_data = {8{u_wr_data}};
    assign band_wr_sel  = op_en ? (8'h01 << u_wr_addr[2:0]) : 8'h00;
endmodule
