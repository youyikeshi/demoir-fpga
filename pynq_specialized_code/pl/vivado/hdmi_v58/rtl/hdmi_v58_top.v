// ============================================================================
//  hdmi_v58_top.v —— v58 合并位流的 HDMI 结构外壳（module reference，进 design_1）
//
//  血缘：与 pl/vivado/hdmi_p1/rtl/hdmi_p1_top.v（md5 0b669bc9…）**同构**，只改一处：
//        MMCM 的输入时钟从 PS FCLK0（100 MHz）换成 **板上 50 MHz 单端晶振（Y14）**，
//        M 从 11.875 改成 **23.75**（VCO 仍然精确等于 1187.5 MHz）。
//
//  ★ 为什么要换（docs/camera/07_合并方案_去摩尔纹+HDMI.md §1.2）：
//    本 BD 的 IO PLL = 1800 MHz、FCLK0 分频 4×3（BD 初始 150 MHz，运行点由 PS 写
//    Clocks.fclk0_mhz 改变），而 PYNQ 的 Clocks 报数与真值有 1.5× 偏差 ⇒
//    **任何"按 PS 时钟标称值算 MMCM"的做法都会让像素时钟错**。
//    改用板上 50 MHz 晶振后，像素时钟 = 50 × 23.75 / DIV，与 PS 完全解耦。
//
//  板上 50 MHz 的出处（厂商手册原文，2026-11-03 核实）：
//    "AC7015B 核心板上提供了单端 50MHz 的 PL 系统时钟源，3.3V 供电。晶振输出连接到
//     FPGA BANK13 的全局时钟(MRCC)" / 引脚表 "PL_GCLK = **Y14**"
//    来源：ALINX AX7015B_UserManual（官方仓库 HTML 文档）
//    https://github.com/alinxalinx/AX7015B_2023.1/blob/master/HTMLdocxs/
//           AX7015B_UserManual_CN/AX7015B_UserManual.html
//
//  时钟树（**本外壳只负责 MMCM，不再加任何 BUFG/BUFGMUX**）：
//      clk_50m (Y14, 20.000 ns) ─┐
//                                └─ MMCME2_ADV  M=23.75  D=1  VCO=1187.5 MHz
//                                     ├ CLKFBOUT ── BUFG u_bufg_clkfb ── CLKFBIN
//                                     ├ CLKOUT0 ÷8  = 148.4375 MHz → pix_clk_a ┐
//                                     ├ CLKOUT1 ÷16 =  74.21875 MHz → pix_clk_b ┤ 送进
//                                     └ CLKOUT2 ÷32 =  37.109375 MHz → pix_clk_c ┘ 冻结 top
//
//  ★★ 重要（与"外壳里再写一遍两级 BUFGMUX"的说法不同，有据，别改）：
//    三级时钟选择（3×BUFG + **两级 BUFGMUX 串联**）与 I2C 的 IOBUF **已经在冻结的
//    rtl_hand/modules/hdmi_out_top.v 内部**（md5 27cda94d…，第 128~156 行）：
//        BUFG u_bufg_a/u_bufg_b/u_bufg_c → BUFGMUX u_bufgmux → BUFGMUX u_bufgmux_c
//    ⇒ 外壳若再加 BUFG，就成 BUFG→BUFG 级联（DRC 直接报错），且 BUFGCTRL 站点数与
//       p1 实测的 6 个（1 clkfb + 3 pix + 2 mux）对不上。
//    ⇒ 外壳的判据是：**MMCM 输出端一个 buffer 都不加**（与 p1 外壳第 142~143 行同一条）。
//
//  ★ 复位分工（契约 §8.1 / §8.5，事故驱动，逐字沿用 p1）：
//    AXI 域（互联 / 寄存器文件 / I2C 状态机）：时钟 = s_axi_aclk（PS FCLK0），
//        复位 = aresetn（= rst_0/peripheral_aresetn，其 dcm_locked 在 BD 里恒接常量 1）
//        ⇒ 这条链里**没有** MMCM.locked 的任何参与 ⇒ "不亮而已，不变砖"。
//    像素域：复位由冻结 top 内部自己用 aresetn 在 pix 域 3FF 同步 + pwr_good 生成。
//    MMCM.locked **只**送进 u_hdmi_out_top.mmcm_locked（0x30[1] 状态上报），
//        不进复位、不进使能、不进 hdmi_rst_n。BD 里本模块的 mmcm_locked 输出故意悬空。
// ============================================================================
`timescale 1ns / 1ps

module hdmi_v58_top #(
    parameter integer MODE_DEFAULT = 3,            // 3 = 720p30（37.109375 MHz）
    parameter integer AXI_HZ       = 150_000_000   // = BD 里 FCLK0 的声明值（150 MHz）
) (
    // ---- 板上 50 MHz 晶振（Y14, BANK13 MRCC, LVCMOS33）----
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME clk_50m, FREQ_HZ 50000000, PHASE 0.000" *)
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_50m CLK" *)
    input  wire        clk_50m,

    // ---- AXI 时钟/复位（BD 接口属性；FREQ_HZ 必须等于 PS7 FCLK0）----
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME s_axi_aclk, ASSOCIATED_BUSIF s_axi:m_axi, FREQ_HZ 150000000" *)
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 s_axi_aclk CLK" *)
    input  wire        s_axi_aclk,
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME aresetn, POLARITY ACTIVE_LOW" *)
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn RST" *)
    input  wire        aresetn,

    // ---- AXI4-Lite 配置页（→ axi_ctrl/M00_AXI，地址 0x400A0000 / 64 KB）----
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME s_axi, PROTOCOL AXI4LITE, ADDR_WIDTH 32, DATA_WIDTH 32, FREQ_HZ 150000000, HAS_BURST 0, HAS_CACHE 0, HAS_QOS 0, HAS_REGION 0, HAS_LOCK 0, HAS_PROT 0, HAS_WSTRB 1, NUM_READ_OUTSTANDING 1, NUM_WRITE_OUTSTANDING 1" *)
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWADDR" *)
    input  wire [31:0] s_axi_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWVALID" *)
    input  wire        s_axi_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWREADY" *)
    output wire        s_axi_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WDATA" *)
    input  wire [31:0] s_axi_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WSTRB" *)
    input  wire [3:0]  s_axi_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WVALID" *)
    input  wire        s_axi_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WREADY" *)
    output wire        s_axi_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BRESP" *)
    output wire [1:0]  s_axi_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BVALID" *)
    output wire        s_axi_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BREADY" *)
    input  wire        s_axi_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARADDR" *)
    input  wire [31:0] s_axi_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARVALID" *)
    input  wire        s_axi_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARREADY" *)
    output wire        s_axi_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RDATA" *)
    output wire [31:0] s_axi_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RRESP" *)
    output wire [1:0]  s_axi_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RVALID" *)
    output wire        s_axi_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RREADY" *)
    input  wire        s_axi_rready,

    // ---- MMCM 锁定：**只做观测/断言锚点**（BD 里故意悬空）----
    output wire        mmcm_locked,

    // ---- 视频输出（→ 厂商 XDC 的引脚；pix_clk = H1）----
    output wire        pix_clk,       // H1
    output wire        hdmi_de,       // G2
    output wire        hdmi_hs,       // E4
    output wire        hdmi_vs,       // E3
    output wire [23:0] hdmi_d,        // G3/H3/H4/G7/G8/G1/H5/H6/G4/F4/F5/E5/G6/F6/E7/F7/D3/C3/C4/D5/C5/C6/E8/D8

    // ---- SIL9134 控制（IOBUF 在冻结 top 内部）----
    output wire        hdmi_rst_n,    // L4，低有效
    inout  wire        hdmi_scl,      // J8，开漏 + 外部上拉
    inout  wire        hdmi_sda,      // K8，开漏 + 外部上拉

    // ---- AXI4 主口（P2 扫描器读 DDR；只读 ⇒ READ_WRITE_MODE READ_ONLY）----
    //   ★ ID：冻结 top 本身不产生 ID，这里显式引出 4 bit 并**恒 0**。
    //     为什么必须有：BD 侧接 `pc_conv_hdmi`（axi_protocol_converter，AXI4→AXI3）
    //     的 SI 口，它的 ID_WIDTH=4（默认，与 v57 的 pc_conv_bin/pc_conv_uni 一致）
    //     ⇒ 主口不给 ID 就会在 BD 里被绑 0（CRITICAL WARNING BD 41-759，
    //       按契约 §8.6 属门禁级）。给一根常 0 的 ID 让"意图"显式化。
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME m_axi, PROTOCOL AXI4, READ_WRITE_MODE READ_ONLY, ADDR_WIDTH 32, DATA_WIDTH 64, ID_WIDTH 4, FREQ_HZ 150000000, HAS_BURST 1, HAS_CACHE 0, HAS_LOCK 0, HAS_PROT 0, HAS_QOS 0, HAS_REGION 0, HAS_WSTRB 0, HAS_AXLEN 1, NUM_READ_OUTSTANDING 1, NUM_WRITE_OUTSTANDING 0, SUPPORTS_NARROW_BURST 0, MAX_BURST_LENGTH 256" *)
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi ARADDR" *)  output wire [31:0] m_axi_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi ARLEN"  *)  output wire [7:0]  m_axi_arlen,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi ARSIZE" *)  output wire [2:0]  m_axi_arsize,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi ARBURST" *) output wire [1:0]  m_axi_arburst,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi ARID"    *) output wire [3:0]  m_axi_arid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi ARVALID" *) output wire        m_axi_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi ARREADY" *) input  wire        m_axi_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi RDATA" *)   input  wire [63:0] m_axi_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi RRESP" *)   input  wire [1:0]  m_axi_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi RLAST" *)   input  wire        m_axi_rlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi RID"    *)  input  wire [3:0]  m_axi_rid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi RVALID" *)  input  wire        m_axi_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 m_axi RREADY" *)  output wire        m_axi_rready
);

    // ================================================================ ① MMCM
    // 单 VCO 1187.5 MHz = 50 MHz × 23.75 / 1
    //   CLKOUT0 = 1187.5 / 8  = 148.4375 MHz  （6.7368 ns，1080p60，mode 1）
    //   CLKOUT1 = 1187.5 / 16 =  74.21875 MHz （13.4737 ns，720p60/1080p30，mode 0/2）
    //   CLKOUT2 = 1187.5 / 32 =  37.109375 MHz（26.9474 ns，720p30，**默认 mode 3**）
    //  全部 −421 ppm（因为真实 VCO 是 1187.500 MHz 而不是 1188.000）
    wire clk_pix_a, clk_pix_b, clk_pix_c;
    wire clkfb_out, clkfb_in;
    wire mmcm_locked_int;
    wire mmcm_rst = ~aresetn;      // 只由 PS 域复位决定（不含任何 locked 反馈）

    assign mmcm_locked = mmcm_locked_int;

    MMCME2_ADV #(
        .BANDWIDTH            ("OPTIMIZED"),
        .COMPENSATION         ("ZHOLD"),
        .STARTUP_WAIT         ("FALSE"),
        .CLKOUT4_CASCADE      ("FALSE"),
        .DIVCLK_DIVIDE        (1),
        .CLKFBOUT_MULT_F      (23.750),        // ★ 50 MHz × 23.75 = 1187.5 MHz
        .CLKFBOUT_PHASE       (0.000),
        .CLKFBOUT_USE_FINE_PS ("FALSE"),
        .CLKOUT0_DIVIDE_F     (8.000),         // 148.4375 MHz
        .CLKOUT0_PHASE        (0.000),
        .CLKOUT0_DUTY_CYCLE   (0.500),
        .CLKOUT0_USE_FINE_PS  ("FALSE"),
        .CLKOUT1_DIVIDE       (16),            //  74.21875 MHz
        .CLKOUT1_PHASE        (0.000),
        .CLKOUT1_DUTY_CYCLE   (0.500),
        .CLKOUT1_USE_FINE_PS  ("FALSE"),
        .CLKOUT2_DIVIDE       (32),            //  37.109375 MHz
        .CLKOUT2_PHASE        (0.000),
        .CLKOUT2_DUTY_CYCLE   (0.500),
        .CLKOUT2_USE_FINE_PS  ("FALSE"),
        .CLKIN1_PERIOD        (20.000),        // ★ 50 MHz（Y14）
        .CLKIN2_PERIOD        (20.000),
        .REF_JITTER1          (0.010),
        .REF_JITTER2          (0.010)
    ) u_mmcm (
        .CLKOUT0   (clk_pix_a),
        .CLKOUT0B  (),
        .CLKOUT1   (clk_pix_b),
        .CLKOUT1B  (),
        .CLKOUT2   (clk_pix_c),
        .CLKOUT2B  (),
        .CLKOUT3   (),
        .CLKOUT3B  (),
        .CLKOUT4   (),
        .CLKOUT5   (),
        .CLKOUT6   (),
        .CLKFBOUT  (clkfb_out),
        .CLKFBOUTB (),
        .CLKFBIN   (clkfb_in),
        .CLKIN1    (clk_50m),                  // ★ 板上 50 MHz（Y14），不经 BUFG
        .CLKIN2    (1'b0),
        .CLKINSEL  (1'b1),
        .DADDR     (7'd0),
        .DCLK      (1'b0),
        .DEN       (1'b0),
        .DI        (16'd0),
        .DO        (),
        .DRDY      (),
        .DWE       (1'b0),
        .LOCKED    (mmcm_locked_int),
        .PSCLK     (1'b0),
        .PSEN      (1'b0),
        .PSINCDEC  (1'b0),
        .PSDONE    (),
        .PWRDWN    (1'b0),
        .RST       (mmcm_rst)
    );

    // MMCM 反馈必须是全局时钟（这是 p1 已验证的 6 个 BUFGCTRL 之一）
    BUFG u_bufg_clkfb (.I (clkfb_out), .O (clkfb_in));

    // ================================================================ ② 冻结 RTL
    //   pix_clk_a/b/c 直接进 top（top 内部 3×BUFG + 2×BUFGMUX，外壳不得再加 buffer）
    //   m_axi 的 ID 恒 0（见端口注释）
    //   irq_vsync/init_done/cfg_upd/fid_upd 本版不外露（BD 里用不上，由 0x04/0x30 读）
    wire        irq_vsync_w, init_done_w, cfg_upd_w, fid_upd_w;

    assign m_axi_arid = 4'd0;

    hdmi_out_top #(
        .MODE_DEFAULT (MODE_DEFAULT),
        .AXI_HZ       (AXI_HZ)
    ) u_hdmi_out_top (
        .s_axi_aclk     (s_axi_aclk),
        .aresetn        (aresetn),            // ★ 只有 PS 域复位：AXI 绝不被 MMCM 门控
        .pix_clk_a      (clk_pix_a),
        .pix_clk_b      (clk_pix_b),
        .pix_clk_c      (clk_pix_c),
        .mmcm_locked    (mmcm_locked_int),    // ★ 全设计只接这一处（0x30[1] 上报）

        .s_axi_awaddr   (s_axi_awaddr),
        .s_axi_awvalid  (s_axi_awvalid),
        .s_axi_awready  (s_axi_awready),
        .s_axi_wdata    (s_axi_wdata),
        .s_axi_wstrb    (s_axi_wstrb),
        .s_axi_wvalid   (s_axi_wvalid),
        .s_axi_wready   (s_axi_wready),
        .s_axi_bresp    (s_axi_bresp),
        .s_axi_bvalid   (s_axi_bvalid),
        .s_axi_bready   (s_axi_bready),
        .s_axi_araddr   (s_axi_araddr),
        .s_axi_arvalid  (s_axi_arvalid),
        .s_axi_arready  (s_axi_arready),
        .s_axi_rdata    (s_axi_rdata),
        .s_axi_rresp    (s_axi_rresp),
        .s_axi_rvalid   (s_axi_rvalid),
        .s_axi_rready   (s_axi_rready),

        .m_axi_araddr   (m_axi_araddr),
        .m_axi_arlen    (m_axi_arlen),
        .m_axi_arsize   (m_axi_arsize),
        .m_axi_arburst  (m_axi_arburst),
        .m_axi_arvalid  (m_axi_arvalid),
        .m_axi_arready  (m_axi_arready),
        .m_axi_rdata    (m_axi_rdata),
        .m_axi_rresp    (m_axi_rresp),
        .m_axi_rlast    (m_axi_rlast),
        .m_axi_rvalid   (m_axi_rvalid),
        .m_axi_rready   (m_axi_rready),

        .pix_clk        (pix_clk),
        .hdmi_de        (hdmi_de),
        .hdmi_hs        (hdmi_hs),
        .hdmi_vs        (hdmi_vs),
        .hdmi_d         (hdmi_d),

        .hdmi_scl       (hdmi_scl),           // 内部 IOBUF
        .hdmi_sda       (hdmi_sda),
        .hdmi_rst_n     (hdmi_rst_n),

        .irq_vsync      (irq_vsync_w),
        .init_done      (init_done_w),
        .cfg_upd        (cfg_upd_w),
        .fid_upd        (fid_upd_w)
    );

    // m_axi_rid 由 pc_conv_hdmi 回送（只读主口不使用 ID 做路由），保留端口以免悬空警告
    wire _unused_rid = |m_axi_rid;

endmodule
