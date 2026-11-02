// ============================================================================
//  hdmi_p1_top.v —— HDMI P1 独立 BD 的"结构外壳"（BD 顶层 module reference）
//
//  ★ 不改动任何冻结 RTL：只做 3 件结构性工作 ——
//      ① AXI4-Lite / 时钟 / 复位 的接口属性（BD 需要 X_INTERFACE_INFO 才能把
//         一组 s_axi_* 端口聚成 AXI 接口、才能 assign_bd_address、才能让
//         FREQ_HZ 与 PS7 FCLK0 对齐）
//      ② 单 VCO 1187.5 MHz 的 MMCM：100 MHz → 148.4375 / 74.21875 MHz 两路
//         （**不加输出 BUFG**：冻结 top 自己 u_bufg_a/u_bufg_b + BUFGMUX）
//      ③ 冻结 top 的 AXI4 master（P1 不用）合法 tie-off
//
//  血缘：内部例化的 hdmi_out_top / hdmi_timing_gen / hdmi_color_bar / sil9134_init
//        是 rtl_hand/modules 的**逐字节冻结副本**（rtl_frozen/），构建脚本对
//        md5 做硬断言；本外壳自己的 md5 也进 manifest。
//
//  ★ 与旧版 top 的关系（2026-11-02 23:45 作者更新，见 BD_NOTES.md §2/§6）：
//      旧版（md5 0e8ad168）= 单 pix_clk 输入 + I2C o/t/i 五端口 + 无内部 IOBUF
//      新版（md5 7189716…）= 双 pix_clk_a/b 输入 + inout hdmi_scl/sda +
//                            内部 BUFG×2 + BUFGMUX + IOBUF + cfg_upd/fid_upd
//     ⇒ 本外壳按**新版**写：MMCM 只产生两路时钟，BUFGMUX 在 top 内部（契约 §7.1）。
//
//  时钟树（与 BD_NOTES.md §3 一致）：
//      FCLK0 100 MHz ─ MMCM(M=11.875, D=1, VCO=1187.5 MHz, CLKIN1_PERIOD=10 ns)
//                       ├ CLKOUT0 ÷8  = 148.4375 MHz → pix_clk_a → top 内 BUFG ┐
//                       └ CLKOUT1 ÷16 =  74.21875 MHz → pix_clk_b → top 内 BUFG ┴ BUFGMUX
//                                                                  ↓ VIDEO_MODE[0]
//                                                        pix_clk（H1 转发给 SIL9134）
//      1080p60 (mode 1)：148.4375 MHz(59.94 Hz)    720p60 (mode 0)：74.21875 MHz(59.97 Hz)
//      1080p30 (mode 2)： 74.21875 MHz(29.97 Hz)   切模式必须先 CTRL.enable=0。
//
//  ★★ 2026-11-03 00:18 事故修复（契约 §8.1/§8.2，事故文档 §3）：★★
//     事故版把这根 MMCM locked **送到 proc_sys_reset.dcm_locked** ⇒ MMCM 不锁
//     ⇒ AXI 互联 + 寄存器文件全被按在复位 ⇒ PS 第一次读 0x400A0000 就永不完结
//     ⇒ 内核卡死、ARP 不应答。修复后的分工：
//       ① AXI 域：BD 里 dcm_locked **恒接 xlconstant 1'b1**，复位只由 PS 的
//          FCLK_RESET0_N 决定。MMCM 锁不锁，0x00/0x04/0x08/0x30 都必须可读。
//          ⇒ 本外壳的 mmcm_locked 输出在 BD 里**故意悬空**（只做观测/断言锚点），
//             绝不许接任何复位类端口（build_hdmi_p1.tcl §3.5 硬断言）。
//       ② 像素域：复位只作用于像素域（top 内部 pix_rst = aresetn 在 pix_clk 域的 3FF
//          同步 + pwr_good + soft_reset + 切模式），MMCM 没锁时像素时钟死 ⇒ 像素域停在
//          复位态 ⇒ **退化成"不亮"，不变砖**。MMCM.locked 原样接到 top 的 mmcm_locked
//          端口，**只**用于 0x30[1] 状态上报（RTL 作者明确要求：不进任何 reset/使能）。
//       ③ 心跳：0x30 PIX_HB —— [0] 像素域自由计数 bit16 翻转（2FF 到 aclk）、[1] locked。
//          ⇒ 上板第一步必须两次读 0x30：[0] 在变 且 [1]=1 才允许读其它寄存器。

// ============================================================================
`timescale 1ns / 1ps

module hdmi_p1_top #(
    parameter integer MODE_DEFAULT = 1,          // 1 = 1080p60（契约默认）
    parameter integer AXI_HZ       = 100_000_000 // 必须等于 FCLK0（build 脚本硬断言）
) (
    // ---- AXI 时钟/复位（BD 接口属性；FREQ_HZ 必须与 PS7 FCLK0 一致）----
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME s_axi_aclk, ASSOCIATED_BUSIF s_axi, FREQ_HZ 100000000" *)
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 s_axi_aclk CLK" *)
    input  wire        s_axi_aclk,
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME aresetn, POLARITY ACTIVE_LOW" *)
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn RST" *)
    input  wire        aresetn,

    // ---- AXI4-Lite 配置页（→ 0x400A0000，64 KB）----
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME s_axi, PROTOCOL AXI4LITE, ADDR_WIDTH 32, DATA_WIDTH 32, FREQ_HZ 100000000, HAS_BURST 0, HAS_CACHE 0, HAS_QOS 0, HAS_REGION 0, HAS_LOCK 0, HAS_PROT 0, HAS_WSTRB 1, NUM_READ_OUTSTANDING 1, NUM_WRITE_OUTSTANDING 1" *)
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

    // ---- MMCM 锁定：**只做观测/断言锚点**，BD 里必须悬空 ----
    //  ★ 绝不许接 proc_sys_reset.dcm_locked（事故根因）。BD 里 dcm_locked 接常量 1'b1。
    //  ★ 构建脚本 §3.5 会读回 BD：这个 pin 一旦连到任何 reset/dcm_locked 就 ERROR 退出。
    output wire        mmcm_locked,

    // ---- 视频输出（→ 厂商 XDC 的引脚；pix_clk = H1）----
    output wire        pix_clk,
    output wire        hdmi_de,         // G2
    output wire        hdmi_hs,         // E4
    output wire        hdmi_vs,         // E3
    output wire [23:0] hdmi_d,          // G3/H3/H4/G7/G8/G1/H5/H6/G4/F4/F5/E5/G6/F6/E7/F7/D3/C3/C4/D5/C5/C6/E8/D8

    // ---- SIL9134 控制（IOBUF 在冻结 top 内部）----
    output wire        hdmi_rst_n,      // L4，低有效
    inout  wire        hdmi_scl,        // J8，开漏 + 外部上拉
    inout  wire        hdmi_sda,        // K8，开漏 + 外部上拉

    // ---- 状态 / P2 预留（P1 全部悬空；BD 里不引出）----
    output wire        irq_vsync,
    output wire        init_done,
    output wire        cfg_upd,
    output wire        fid_upd
);

    // ================================================================ ② MMCM
    // 单 VCO 1187.5 MHz = 100 MHz × 11.875 / 1
    //   CLKOUT0 = 1187.5 / 8  = 148.4375 MHz（周期 6.7373 ns）
    //   CLKOUT1 = 1187.5 / 16 =  74.21875 MHz（周期 13.4737 ns）
    // ★ 输出**不加 BUFG**：冻结 top 内部 u_bufg_a/u_bufg_b 已经是每路一个 BUFG，
    //   再加就成 BUFG→BUFG（DRC 直接报错）。
    wire clk_pix_a, clk_pix_b;
    wire clkfb_out, clkfb_in;
    wire mmcm_locked_int;                    // MMCM LOCKED 原始锁定位（内部）
    wire mmcm_rst = ~aresetn;

    // 观测用（BD 里悬空）：只让构建脚本能把 MMCM 锁定位从 wrapper 上抓出来核对，
    // 绝不参与任何复位 —— 见文件头 ①。
    assign mmcm_locked = mmcm_locked_int;

    MMCME2_ADV #(
        .BANDWIDTH            ("OPTIMIZED"),
        .COMPENSATION         ("ZHOLD"),
        .STARTUP_WAIT         ("FALSE"),
        .CLKOUT4_CASCADE      ("FALSE"),
        .DIVCLK_DIVIDE        (1),
        .CLKFBOUT_MULT_F      (11.875),
        .CLKFBOUT_PHASE       (0.000),
        .CLKFBOUT_USE_FINE_PS ("FALSE"),
        .CLKOUT0_DIVIDE_F     (8.000),
        .CLKOUT0_PHASE        (0.000),
        .CLKOUT0_DUTY_CYCLE   (0.500),
        .CLKOUT0_USE_FINE_PS  ("FALSE"),
        .CLKOUT1_DIVIDE       (16),
        .CLKOUT1_PHASE        (0.000),
        .CLKOUT1_DUTY_CYCLE   (0.500),
        .CLKOUT1_USE_FINE_PS  ("FALSE"),
        .CLKIN1_PERIOD        (10.000),
        .CLKIN2_PERIOD        (10.000),
        .REF_JITTER1          (0.010),
        .REF_JITTER2          (0.010)
    ) u_mmcm (
        .CLKOUT0   (clk_pix_a),
        .CLKOUT0B  (),
        .CLKOUT1   (clk_pix_b),
        .CLKOUT1B  (),
        .CLKOUT2   (),
        .CLKOUT2B  (),
        .CLKOUT3   (),
        .CLKOUT3B  (),
        .CLKOUT4   (),
        .CLKOUT5   (),
        .CLKOUT6   (),
        .CLKFBOUT  (clkfb_out),
        .CLKFBOUTB (),
        .CLKFBIN   (clkfb_in),
        .CLKIN1    (s_axi_aclk),
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

    BUFG u_bufg_clkfb (.I (clkfb_out), .O (clkfb_in));

    // ============================================ ② 两域复位分工（事故修复的核心）
    //
    //  AXI 域（互联 / 寄存器文件 / I2C 状态机）：
    //     时钟 = s_axi_aclk（PS FCLK0），复位 = aresetn（= proc_sys_reset.peripheral_aresetn，
    //     其 dcm_locked 在 BD 里恒接常量 1'b1）⇒ **这条链里没有 MMCM.locked 的任何参与**。
    //
    //  像素域（时序发生器 / 彩条 / 视频输出）：
    //     时钟 = pix_clk_a/b（MMCM 派生）+ top 内 u_bufg_a/u_bufg_b + BUFGMUX；
    //     复位 = top 内部 pix_rst = rst_sync[2] | ~pg_sync | sr_sync | (mode_rst_cnt!=0)，
    //            其中 rst_sync 就是 aresetn 在 pix_clk 域的 3FF 同步（= "PS 侧复位同步后"），
    //            ⇒ 它**只**作用于像素域，AXI 域用的是 aresetn 原信号。
    //     ⇒ MMCM 没锁 ⇒ 像素时钟死 ⇒ 像素域全部触发器停在 init 值（= 复位态）、hb_cnt 不翻
    //        ⇒ 退化成"不亮"，**AXI 照常可读**。
    //
    //  ★ 为什么外壳**不**把 mmcm_locked 与复位相与（与契约 §8.1 的字面表述差一步，有据）：
    //    冻结 top（T-67a2 版，md5 b997aa8b…）明确要求 mmcm_locked 只做 0x30[1] 状态上报，
    //    不参与任何 reset/使能（见其文件头 26/27/36/37 行）：
    //      - 若把它与 pix_rst 相与 ⇒ 未锁时会清 hb_cnt ⇒ 0x30[0] 的"翻转"将不再是
    //        "像素时钟活着"的独立证据，心跳自证失效；
    //      - 外壳也**不许**把它与 hdmi_rst_n（SIL9134 复位）相与：sil9134_init 跑在
    //        s_axi_aclk 上（rst = ~pwr_good，POR 200 µs），MMCM 未锁时 I2C 初始化照样会跑完；
    //        此时若把芯片按在复位里，等 MMCM 锁上后 I2C 早已 S_DONE ⇒ **永久黑屏**。
    //     ⇒ 因此本外壳把 MMCM.locked **原样**送进 u_hdmi_out_top.mmcm_locked（且只送这一处）。


    // ================================================================ 冻结 RTL（双时钟版）
    hdmi_out_top #(
        .MODE_DEFAULT (MODE_DEFAULT),
        .AXI_HZ       (AXI_HZ)
    ) u_hdmi_out_top (
        .s_axi_aclk     (s_axi_aclk),
        .aresetn        (aresetn),           // ★ 只接 PS 域复位：AXI 域绝不被 MMCM 门控（①）
        .pix_clk_a      (clk_pix_a),         // 148.4375 MHz（top 内 BUFG + BUFGMUX）
        .pix_clk_b      (clk_pix_b),         //  74.21875 MHz

        // ③ MMCM.locked **只接这一处**：冻结 top 内只做 0x30[1] 状态上报（2FF 到 aclk）。
        //    不进复位、不做使能 —— 详见上面 ② 的说明。
        .mmcm_locked    (mmcm_locked_int),

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

        // ③ P1 的 AXI4 master 读通路恒空转（P3-b 才用）⇒ 合法 tie-off
        .m_axi_araddr   (),
        .m_axi_arlen    (),
        .m_axi_arsize   (),
        .m_axi_arburst  (),
        .m_axi_arvalid  (),
        .m_axi_arready  (1'b0),
        .m_axi_rdata    (64'd0),
        .m_axi_rresp    (2'd0),
        .m_axi_rlast    (1'b0),
        .m_axi_rvalid   (1'b0),
        .m_axi_rready   (),

        .pix_clk        (pix_clk),           // 内部 BUFGMUX 输出 → H1
        .hdmi_de        (hdmi_de),
        .hdmi_hs        (hdmi_hs),
        .hdmi_vs        (hdmi_vs),
        .hdmi_d         (hdmi_d),

        .hdmi_scl       (hdmi_scl),          // 内部 IOBUF
        .hdmi_sda       (hdmi_sda),
        .hdmi_rst_n     (hdmi_rst_n),

        .irq_vsync      (irq_vsync),
        .init_done      (init_done),
        .cfg_upd        (cfg_upd),           // P3 接 hdmi_fb_scan.cfg_upd（P1 悬空）
        .fid_upd        (fid_upd)
    );

endmodule
