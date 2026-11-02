// ============================================================================
//  bin_unit_axi_wrap_bd.v —— bin_unit_axi_wrap 的 BD 引脚外壳（**自动生成**，勿手改）
//  生成器：tools/gen_bd_shell.py bin_unit_axi_wrap
//
//  为什么需要：BD 的 create_bd_cell -type module -reference 只吃 Verilog/VHDL，
//  而 bin_unit_axi_wrap 是 SystemVerilog；且端口需要 X_INTERFACE_INFO 属性，BD 才能
//  把它们聚成 AXI 接口。本文件内部例化的 bin_unit_axi_wrap 仍可以是 SV。
// ============================================================================
`timescale 1ns/1ps
module bin_unit_axi_wrap_bd #(
    parameter AXI_ID_W = 4
) (
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME s_axi_aclk, ASSOCIATED_BUSIF s_axi:m_axi, FREQ_HZ 40000000" *)
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 s_axi_aclk CLK" *)
    input        s_axi_aclk,
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME s_axi_aresetn, POLARITY ACTIVE_LOW" *)
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 s_axi_aresetn RST" *)
    input        s_axi_aresetn,
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME s_axi, PROTOCOL AXI3, ADDR_WIDTH 32, DATA_WIDTH 64, FREQ_HZ 40000000, HAS_BURST 1, HAS_CACHE 0, HAS_QOS 0, HAS_REGION 0, HAS_LOCK 0, HAS_PROT 1, HAS_WSTRB 1, MAX_BURST_LENGTH 16, NUM_READ_OUTSTANDING 16, NUM_WRITE_OUTSTANDING 16" *)
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWADDR" *)
    input  [31:0] s_axi_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWVALID" *)
    input        s_axi_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWREADY" *)
    output       s_axi_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WDATA" *)
    input  [31:0] s_axi_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WSTRB" *)
    input  [3:0] s_axi_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WVALID" *)
    input        s_axi_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WREADY" *)
    output       s_axi_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BRESP" *)
    output [1:0] s_axi_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BVALID" *)
    output       s_axi_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BREADY" *)
    input        s_axi_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARADDR" *)
    input  [31:0] s_axi_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARVALID" *)
    input        s_axi_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARREADY" *)
    output       s_axi_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RDATA" *)
    output [31:0] s_axi_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RRESP" *)
    output [1:0] s_axi_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RVALID" *)
    output       s_axi_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RREADY" *)
    input        s_axi_rready,
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME m_axi, PROTOCOL AXI3, ADDR_WIDTH 32, DATA_WIDTH 64, FREQ_HZ 40000000, HAS_BURST 1, HAS_CACHE 0, HAS_QOS 0, HAS_REGION 0, HAS_LOCK 0, HAS_PROT 1, HAS_WSTRB 1, MAX_BURST_LENGTH 16, NUM_READ_OUTSTANDING 16, NUM_WRITE_OUTSTANDING 16" *)
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi ARVALID" *)
    output       m_axi_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi ARREADY" *)
    input        m_axi_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi ARADDR" *)
    output [31:0] m_axi_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi ARLEN" *)
    output [3:0] m_axi_arlen,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi ARID" *)
    output [AXI_ID_W-1:0] m_axi_arid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi RVALID" *)
    input        m_axi_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi RREADY" *)
    output       m_axi_rready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi RDATA" *)
    input  [63:0] m_axi_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi RLAST" *)
    input        m_axi_rlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi RID" *)
    input  [AXI_ID_W-1:0] m_axi_rid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi AWVALID" *)
    output       m_axi_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi AWREADY" *)
    input        m_axi_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi AWADDR" *)
    output [31:0] m_axi_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi AWLEN" *)
    output [3:0] m_axi_awlen,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi WID" *)
    output [AXI_ID_W-1:0] m_axi_wid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi WVALID" *)
    output       m_axi_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi WREADY" *)
    input        m_axi_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi WDATA" *)
    output [63:0] m_axi_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi WSTRB" *)
    output [7:0] m_axi_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi WLAST" *)
    output       m_axi_wlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi BVALID" *)
    input        m_axi_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi BREADY" *)
    output       m_axi_bready
);
    bin_unit_axi_wrap u_bin_unit (
        .s_axi_aclk(s_axi_aclk),
        .s_axi_aresetn(s_axi_aresetn),
        .s_axi_awaddr(s_axi_awaddr),
        .s_axi_awvalid(s_axi_awvalid),
        .s_axi_awready(s_axi_awready),
        .s_axi_wdata(s_axi_wdata),
        .s_axi_wstrb(s_axi_wstrb),
        .s_axi_wvalid(s_axi_wvalid),
        .s_axi_wready(s_axi_wready),
        .s_axi_bresp(s_axi_bresp),
        .s_axi_bvalid(s_axi_bvalid),
        .s_axi_bready(s_axi_bready),
        .s_axi_araddr(s_axi_araddr),
        .s_axi_arvalid(s_axi_arvalid),
        .s_axi_arready(s_axi_arready),
        .s_axi_rdata(s_axi_rdata),
        .s_axi_rresp(s_axi_rresp),
        .s_axi_rvalid(s_axi_rvalid),
        .s_axi_rready(s_axi_rready),
        .m_axi_arvalid(m_axi_arvalid),
        .m_axi_arready(m_axi_arready),
        .m_axi_araddr(m_axi_araddr),
        .m_axi_arlen(m_axi_arlen),
        .m_axi_arid(m_axi_arid),
        .m_axi_rvalid(m_axi_rvalid),
        .m_axi_rready(m_axi_rready),
        .m_axi_rdata(m_axi_rdata),
        .m_axi_rlast(m_axi_rlast),
        .m_axi_rid(m_axi_rid),
        .m_axi_awvalid(m_axi_awvalid),
        .m_axi_awready(m_axi_awready),
        .m_axi_awaddr(m_axi_awaddr),
        .m_axi_awlen(m_axi_awlen),
        .m_axi_wid(m_axi_wid),
        .m_axi_wvalid(m_axi_wvalid),
        .m_axi_wready(m_axi_wready),
        .m_axi_wdata(m_axi_wdata),
        .m_axi_wstrb(m_axi_wstrb),
        .m_axi_wlast(m_axi_wlast),
        .m_axi_bvalid(m_axi_bvalid),
        .m_axi_bready(m_axi_bready)
    );
endmodule
