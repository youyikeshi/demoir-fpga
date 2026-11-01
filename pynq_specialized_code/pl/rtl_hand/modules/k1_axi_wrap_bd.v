// ============================================================================
//  k1_axi_wrap_bd.v —— k1_axi_wrap 的 **BD 适配层**（IP Integrator 专用外壳）
//
//  ⚠ 本文件刻意写成 **Verilog-2001**（不是 SystemVerilog），原因很硬：
//     `create_bd_cell -type module -reference` 只接受 **Verilog / VHDL** 作为
//     "reference 的顶层文件"，顶层文件类型是 SystemVerilog 会直接报
//         [filemgmt 56-195] Reference 'xxx' contains top file 'xxx.v'
//         of type SystemVerilog. This type is not allowed as the top file.
//         [BD 41-1690] Unable to resolve module-source based on inputs
//     （2026-10-23 主控实跑踩到）。限制**只针对 reference 的顶层文件**：
//     本文件内部例化的 k1_axi_wrap / conv_k1_engine 仍然可以是 SystemVerilog。
//     所以这里没有任何 `logic`/`always_comb`/`parameter int`——只有 ANSI 端口、
//     assign 和例化，改成 Verilog-2001 是零语义损失。
//
//  为什么需要这一层（三条都是对着代码实测出来的，不是猜的）：
//   1) `k1_axi_wrap` 的端口上**没有** X_INTERFACE_INFO 属性 ⇒ 用
//      `create_bd_cell -type module -reference k1_axi_wrap` 加进 BD 后，
//      得到的是一堆**散引脚**（m_axi_rd_araddr 等），不是 interface pin；
//      散引脚**无法** connect_bd_intf_net 到 `smc/S02_AXI` 这种 interface pin。
//   2) `k1_axi_wrap` 的 master 是**精简 AXI4**：只有
//      AR{valid,ready,addr,len} + R{valid,ready,data,last} 和
//      AW{valid,ready,addr,len} + W{valid,ready,data,strb,last} + B{valid,ready}；
//      **缺** ARSIZE/ARBURST/ARCACHE/ARPROT/ARID/AW(...)/BRESP/RRESP/BID/RID。
//      SmartConnect 的 SI 口是完整 AXI4 ⇒ 少了这些信号连不上（宽度/成员不匹配）。
//   3) `k1_axi_wrap` **没有** ap_clk / ap_rst_n 端口：这两根是**内部**引擎的，
//      在 wrapper 里已被 s_axi_aclk / s_axi_aresetn 驱动（见 k1_axi_wrap.v:355）。
//      所以 BD 侧只能接 s_axi_aclk / s_axi_aresetn。
//
//  本文件做的事（全部是**引脚级**的，不含任何功能逻辑）：
//   · 把 2 个精简 AXI4 master（合并读 + 输出写）补成**完整 AXI4**
//     （AXI4 64-bit 数据 / 64-bit 地址；m_axi_rd 是 2-bit ID，m_axi_out 是 1-bit）：
//       资源瘦身后 in/w/b 已在 k1_axi_wrap 内部仲裁成 1 个 m_axi_rd，
//       所以本层只剩 m_axi_rd + m_axi_out，smc 的 SI 由 6 个降到 4 个。
//       - ARSIZE/AWSIZE = 3'b011（每拍 8 字节 = 64-bit）
//       - ARBURST/AWBURST = 2'b01（INCR；引擎给的是 arlen，语义就是 INCR）
//       - ARCACHE/AWCACHE = 4'b0011（Modifiable + Bufferable，PS HP 口常规值）
//       - ARPROT/AWPROT = 3'b000，ARQOS/AWQOS = 4'b0000，ARLOCK/AWLOCK = 1'b0
//       - m_axi_rd 的 ARID = 2-bit tag（0=in/1=w/2=b），RID 回到内部做检查；
//         m_axi_out 的 ARID/AWID 仍是 1'b0，RID/BID/RRESP/BRESP 不使用
//       - 32 位引擎地址**零扩展**成 64 位（高 32 位恒 0 ⇒ 只能访问 DDR 低 1GB，
//         与 HLS 引擎的 PS 侧分配一致）
//   · 两个 master 都声明成 READ_WRITE 并**补齐两条通道**：m_axi_rd 的写通道恒空闲
//     （awvalid=0, wvalid=0, bready=1），m_axi_out 的读通道恒空闲
//     （arvalid=0, rready=1）。这样 2 个 SI 的接口形态统一。
//   · 给 AXI4-Lite 从机补上 AWPROT/ARPROT（引擎的 s_axi_ctrl 有这两根，照抄）。
//   · 顶层加 X_INTERFACE_INFO / X_INTERFACE_PARAMETER 属性，让 BD **推断出**
//     interface pin：s_axi / m_axi_rd / m_axi_out。
//
//  本文件只做引脚级适配；仲裁/功能全在 k1_axi_wrap.v（SystemVerilog）。
//
//  BD 里的连法（见 scripts/bd_add_k1.tcl）：
//    m_axi_rd  → smc/S02_AXI     m_axi_out → smc/S03_AXI
//    s_axi     → axi_ctrl/M02_AXI（128KB 窗口，A[16] 分页）
//    s_axi_aclk     ← ps7/FCLK_CLK0（与 engine/ap_clk 同一根）
//    s_axi_aresetn  ← rst_0/peripheral_aresetn（与 engine/ap_rst_n 同一根）
// ============================================================================
`timescale 1ns / 1ps

module k1_axi_wrap_bd (
    // ---- 时钟 / 复位（标量引脚，和 engine/ap_clk 完全一样的接法）----
    input         s_axi_aclk,
    input         s_axi_aresetn,

    // ========================================================================
    // AXI4-Lite 从机（控制寄存器；128KB 窗口，A[16]=1 是 ctrl 页）
    // ========================================================================
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWADDR" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME s_axi, DATA_WIDTH 32, PROTOCOL AXI4LITE, ID_WIDTH 0, ADDR_WIDTH 32, AWUSER_WIDTH 0, ARUSER_WIDTH 0, WUSER_WIDTH 0, RUSER_WIDTH 0, BUSER_WIDTH 0, READ_WRITE_MODE READ_WRITE, HAS_BURST 0, HAS_LOCK 0, HAS_PROT 1, HAS_CACHE 0, HAS_QOS 0, HAS_REGION 0, HAS_WSTRB 1, HAS_BRESP 1, HAS_RRESP 1, SUPPORTS_NARROW_BURST 0, MAX_BURST_LENGTH 1, PHASE 0.0, INSERT_VIP 0" *)
    input  [31:0] s_axi_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWPROT" *)
    input  [2:0]  s_axi_awprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWVALID" *)
    input         s_axi_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWREADY" *)
    output        s_axi_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WDATA" *)
    input  [31:0] s_axi_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WSTRB" *)
    input  [3:0]  s_axi_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WVALID" *)
    input         s_axi_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WREADY" *)
    output        s_axi_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BRESP" *)
    output [1:0]  s_axi_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BVALID" *)
    output        s_axi_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BREADY" *)
    input         s_axi_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARADDR" *)
    input  [31:0] s_axi_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARPROT" *)
    input  [2:0]  s_axi_arprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARVALID" *)
    input         s_axi_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARREADY" *)
    output        s_axi_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RDATA" *)
    output [31:0] s_axi_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RRESP" *)
    output [1:0]  s_axi_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RVALID" *)
    output        s_axi_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RREADY" *)
    input         s_axi_rready,

    // ========================================================================
    // AXI4 master 0：合并后的读（in/w/b 三路仲裁；只读，写通道恒空闲）
    //   ID_WIDTH 2：ARID=0/1/2（in/w/b），R 通道按 burst 授权回路由
    // ========================================================================
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd AWADDR" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME m_axi_rd, DATA_WIDTH 64, PROTOCOL AXI4, ID_WIDTH 2, ADDR_WIDTH 64, AWUSER_WIDTH 0, ARUSER_WIDTH 0, WUSER_WIDTH 0, RUSER_WIDTH 0, BUSER_WIDTH 0, READ_WRITE_MODE READ_WRITE, HAS_BURST 1, HAS_LOCK 1, HAS_PROT 1, HAS_CACHE 1, HAS_QOS 1, HAS_REGION 0, HAS_WSTRB 1, HAS_BRESP 1, HAS_RRESP 1, SUPPORTS_NARROW_BURST 0, NUM_READ_OUTSTANDING 16, NUM_WRITE_OUTSTANDING 16, MAX_BURST_LENGTH 256, PHASE 0.0, INSERT_VIP 0" *)
    output [63:0] m_axi_rd_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd AWLEN" *)
    output [7:0]  m_axi_rd_awlen,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd AWSIZE" *)
    output [2:0]  m_axi_rd_awsize,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd AWBURST" *)
    output [1:0]  m_axi_rd_awburst,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd AWLOCK" *)
    output [0:0]  m_axi_rd_awlock,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd AWCACHE" *)
    output [3:0]  m_axi_rd_awcache,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd AWPROT" *)
    output [2:0]  m_axi_rd_awprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd AWQOS" *)
    output [3:0]  m_axi_rd_awqos,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd AWID" *)
    output [1:0]  m_axi_rd_awid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd AWVALID" *)
    output        m_axi_rd_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd AWREADY" *)
    input         m_axi_rd_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd WDATA" *)
    output [63:0] m_axi_rd_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd WSTRB" *)
    output [7:0]  m_axi_rd_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd WLAST" *)
    output        m_axi_rd_wlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd WVALID" *)
    output        m_axi_rd_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd WREADY" *)
    input         m_axi_rd_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd BID" *)
    input  [1:0]  m_axi_rd_bid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd BRESP" *)
    input  [1:0]  m_axi_rd_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd BVALID" *)
    input         m_axi_rd_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd BREADY" *)
    output        m_axi_rd_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd ARADDR" *)
    output [63:0] m_axi_rd_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd ARLEN" *)
    output [7:0]  m_axi_rd_arlen,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd ARSIZE" *)
    output [2:0]  m_axi_rd_arsize,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd ARBURST" *)
    output [1:0]  m_axi_rd_arburst,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd ARLOCK" *)
    output [0:0]  m_axi_rd_arlock,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd ARCACHE" *)
    output [3:0]  m_axi_rd_arcache,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd ARPROT" *)
    output [2:0]  m_axi_rd_arprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd ARQOS" *)
    output [3:0]  m_axi_rd_arqos,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd ARID" *)
    output [1:0]  m_axi_rd_arid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd ARVALID" *)
    output        m_axi_rd_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd ARREADY" *)
    input         m_axi_rd_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd RID" *)
    input  [1:0]  m_axi_rd_rid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd RDATA" *)
    input  [63:0] m_axi_rd_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd RRESP" *)
    input  [1:0]  m_axi_rd_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd RLAST" *)
    input         m_axi_rd_rlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd RVALID" *)
    input         m_axi_rd_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_rd RREADY" *)
    output        m_axi_rd_rready,

    // ========================================================================
    // AXI4 master 3：输出（只写；读通道恒空闲）
    // ========================================================================
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out AWADDR" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME m_axi_out, DATA_WIDTH 64, PROTOCOL AXI4, ID_WIDTH 1, ADDR_WIDTH 64, AWUSER_WIDTH 0, ARUSER_WIDTH 0, WUSER_WIDTH 0, RUSER_WIDTH 0, BUSER_WIDTH 0, READ_WRITE_MODE READ_WRITE, HAS_BURST 1, HAS_LOCK 1, HAS_PROT 1, HAS_CACHE 1, HAS_QOS 1, HAS_REGION 0, HAS_WSTRB 1, HAS_BRESP 1, HAS_RRESP 1, SUPPORTS_NARROW_BURST 0, NUM_READ_OUTSTANDING 16, NUM_WRITE_OUTSTANDING 16, MAX_BURST_LENGTH 256, PHASE 0.0, INSERT_VIP 0" *)
    output [63:0] m_axi_out_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out AWLEN" *)
    output [7:0]  m_axi_out_awlen,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out AWSIZE" *)
    output [2:0]  m_axi_out_awsize,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out AWBURST" *)
    output [1:0]  m_axi_out_awburst,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out AWLOCK" *)
    output [0:0]  m_axi_out_awlock,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out AWCACHE" *)
    output [3:0]  m_axi_out_awcache,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out AWPROT" *)
    output [2:0]  m_axi_out_awprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out AWQOS" *)
    output [3:0]  m_axi_out_awqos,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out AWID" *)
    output [0:0]  m_axi_out_awid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out AWVALID" *)
    output        m_axi_out_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out AWREADY" *)
    input         m_axi_out_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out WDATA" *)
    output [63:0] m_axi_out_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out WSTRB" *)
    output [7:0]  m_axi_out_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out WLAST" *)
    output        m_axi_out_wlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out WVALID" *)
    output        m_axi_out_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out WREADY" *)
    input         m_axi_out_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out BID" *)
    input  [0:0]  m_axi_out_bid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out BRESP" *)
    input  [1:0]  m_axi_out_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out BVALID" *)
    input         m_axi_out_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out BREADY" *)
    output        m_axi_out_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out ARADDR" *)
    output [63:0] m_axi_out_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out ARLEN" *)
    output [7:0]  m_axi_out_arlen,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out ARSIZE" *)
    output [2:0]  m_axi_out_arsize,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out ARBURST" *)
    output [1:0]  m_axi_out_arburst,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out ARLOCK" *)
    output [0:0]  m_axi_out_arlock,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out ARCACHE" *)
    output [3:0]  m_axi_out_arcache,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out ARPROT" *)
    output [2:0]  m_axi_out_arprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out ARQOS" *)
    output [3:0]  m_axi_out_arqos,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out ARID" *)
    output [0:0]  m_axi_out_arid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out ARVALID" *)
    output        m_axi_out_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out ARREADY" *)
    input         m_axi_out_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out RID" *)
    input  [0:0]  m_axi_out_rid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out RDATA" *)
    input  [63:0] m_axi_out_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out RRESP" *)
    input  [1:0]  m_axi_out_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out RLAST" *)
    input         m_axi_out_rlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out RVALID" *)
    input         m_axi_out_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi_out RREADY" *)
    output        m_axi_out_rready
);

    // ========================================================================
    // 常量（AXI4 必需但引擎不产生：按 64-bit 数据 / INCR burst 的常规值补齐）
    // ========================================================================
    localparam [2:0] AXI_SIZE  = 3'b011;   // 8 字节/拍（64-bit 数据总线）
    localparam [1:0] AXI_BURST = 2'b01;    // INCR（引擎给 arlen/awlen，语义即 INCR）
    localparam [3:0] AXI_CACHE = 4'b0011;  // Modifiable + Bufferable
    localparam [2:0] AXI_PROT  = 3'b000;   // Data / Secure / Unprivileged
    localparam [3:0] AXI_QOS   = 4'b0000;

    // 引擎的 32 位地址先落到内部线上，再零扩展成 64 位（不能直接驱动端口切片：
    // 那样同一根端口会被实例和 assign 同时驱动）。
    wire [31:0] k1_addr_rd, k1_addr_out;

    // ========================================================================
    // 被包装的引擎（原样例化，一个端口都没改）
    // ========================================================================
    k1_axi_wrap #(
        .CIN_MAX  (192),
        .COUT_MAX (96),   // 2026-10-28 瘦身；外壳参数覆盖 wrapper/引擎默认值
        .LANES    (32),
        .NBUF     (4),
        .OBUF_N   (4),
        .RELU     (1'b1)
    ) u_k1 (
        .s_axi_aclk    (s_axi_aclk),
        .s_axi_aresetn (s_axi_aresetn),

        .s_axi_awaddr  (s_axi_awaddr),
        .s_axi_awvalid (s_axi_awvalid),
        .s_axi_awready (s_axi_awready),
        .s_axi_wdata   (s_axi_wdata),
        .s_axi_wstrb   (s_axi_wstrb),
        .s_axi_wvalid  (s_axi_wvalid),
        .s_axi_wready  (s_axi_wready),
        .s_axi_bresp   (s_axi_bresp),
        .s_axi_bvalid  (s_axi_bvalid),
        .s_axi_bready  (s_axi_bready),
        .s_axi_araddr  (s_axi_araddr),
        .s_axi_arvalid (s_axi_arvalid),
        .s_axi_arready (s_axi_arready),
        .s_axi_rdata   (s_axi_rdata),
        .s_axi_rresp   (s_axi_rresp),
        .s_axi_rvalid  (s_axi_rvalid),
        .s_axi_rready  (s_axi_rready),

        // ---- 合并读：AR/R + 2-bit tag 直连；仲裁器在 k1_axi_wrap 内部 ----
        .m_axi_rd_arvalid (m_axi_rd_arvalid), .m_axi_rd_arready (m_axi_rd_arready),
        .m_axi_rd_araddr  (k1_addr_rd),
        .m_axi_rd_arlen   (m_axi_rd_arlen),
        .m_axi_rd_arid    (m_axi_rd_arid),
        .m_axi_rd_rvalid  (m_axi_rd_rvalid),  .m_axi_rd_rready  (m_axi_rd_rready),
        .m_axi_rd_rdata   (m_axi_rd_rdata),   .m_axi_rd_rlast   (m_axi_rd_rlast),
        .m_axi_rd_rid     (m_axi_rd_rid),

        // ---- 输出：AW/W/B 用真的 ----
        .m_axi_out_awvalid(m_axi_out_awvalid),.m_axi_out_awready(m_axi_out_awready),
        .m_axi_out_awaddr (k1_addr_out),
        .m_axi_out_awlen  (m_axi_out_awlen),
        .m_axi_out_wvalid (m_axi_out_wvalid), .m_axi_out_wready (m_axi_out_wready),
        .m_axi_out_wdata  (m_axi_out_wdata),  .m_axi_out_wstrb  (m_axi_out_wstrb),
        .m_axi_out_wlast  (m_axi_out_wlast),
        .m_axi_out_bvalid (m_axi_out_bvalid), .m_axi_out_bready (m_axi_out_bready)
    );

    // ========================================================================
    // 64-bit 地址零扩展 + 固定侧信号
    // 引擎内部只有 32 位地址；PS HP0 的 DDR 低 1GB 落在 0x0000_0000~0x3FFF_FFFF，
    // 所以高 32 位恒 0（与 k1_axi_wrap 里"高字必须为 0"的校验一致）。
    // ========================================================================

    // ---- rd：写通道恒空闲（本口只读；保留完整 AXI4 外形，SmartConnect 不必换配置）----
    assign m_axi_rd_awaddr  = 64'd0;
    assign m_axi_rd_awlen   = 8'd0;
    assign m_axi_rd_awsize  = AXI_SIZE;
    assign m_axi_rd_awburst = AXI_BURST;
    assign m_axi_rd_awlock  = 1'b0;
    assign m_axi_rd_awcache = AXI_CACHE;
    assign m_axi_rd_awprot  = AXI_PROT;
    assign m_axi_rd_awqos   = AXI_QOS;
    assign m_axi_rd_awid    = 2'd0;
    assign m_axi_rd_awvalid = 1'b0;
    assign m_axi_rd_wdata   = 64'd0;
    assign m_axi_rd_wstrb   = 8'd0;
    assign m_axi_rd_wlast   = 1'b0;
    assign m_axi_rd_wvalid  = 1'b0;
    assign m_axi_rd_bready  = 1'b1;

    // ---- rd：读通道（32 位引擎地址零扩展；ARID 就是 2-bit tag，直连）----
    assign m_axi_rd_araddr  = {32'd0, k1_addr_rd};
    assign m_axi_rd_arsize  = AXI_SIZE;
    assign m_axi_rd_arburst = AXI_BURST;
    assign m_axi_rd_arlock  = 1'b0;
    assign m_axi_rd_arcache = AXI_CACHE;
    assign m_axi_rd_arprot  = AXI_PROT;
    assign m_axi_rd_arqos   = AXI_QOS;

    // ---- out：读通道恒空闲 ----
    assign m_axi_out_araddr  = 64'd0;
    assign m_axi_out_arlen   = 8'd0;
    assign m_axi_out_arsize  = AXI_SIZE;
    assign m_axi_out_arburst = AXI_BURST;
    assign m_axi_out_arlock  = 1'b0;
    assign m_axi_out_arcache = AXI_CACHE;
    assign m_axi_out_arprot  = AXI_PROT;
    assign m_axi_out_arqos   = AXI_QOS;
    assign m_axi_out_arid    = 1'b0;
    assign m_axi_out_arvalid = 1'b0;
    assign m_axi_out_rready  = 1'b1;

    // ---- out：写通道（地址零扩展）----
    assign m_axi_out_awaddr  = {32'd0, k1_addr_out};
    assign m_axi_out_awsize  = AXI_SIZE;
    assign m_axi_out_awburst = AXI_BURST;
    assign m_axi_out_awlock  = 1'b0;
    assign m_axi_out_awcache = AXI_CACHE;
    assign m_axi_out_awprot  = AXI_PROT;
    assign m_axi_out_awqos   = AXI_QOS;
    assign m_axi_out_awid    = 1'b0;

endmodule
