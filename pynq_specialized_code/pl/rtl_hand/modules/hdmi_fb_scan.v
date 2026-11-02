// ============================================================================
//  hdmi_fb_scan.v —— HDMI P2 真画面扫描器：DDR 帧缓冲 → 行缓冲 → 逐像素流
//
//  契约来源（唯一需求来源）
//  ------------------------
//  · docs/camera/05_HDMI输出_PS_PL接口契约.md      §2 寄存器图 / §4 P2 交付
//  · docs/camera/03_显示构图规范_纯PS.md           §2 帧缓冲布局契约
//  · ps/pynq/display_layout.py::compose()          帧内容的唯一权威（板端已验证）
//
//  一句话
//  ------
//  PS 已经把整幅画面（含上下黑边）按 **RGB888、行首无 padding（stride = W*3）**
//  烘进 DDR；本模块按视频时序把这块内存 **逐行搬进 BRAM 行缓冲**，再按
//  ``pix_de/pix_x/pix_y/pix_rgb`` 逐像素吐出，**字节序与 compose() 的
//  (y, x, r) 顺序逐字节相同**：
//
//      地址：      FB_ADDR + buf*FB_SIZE + y*FB_STRIDE + 3*x + {0,1,2}
//      字节含义：  {0,1,2} = {R, G, B}
//      输出端口：  pix_rgb[23:16] = R, [15:8] = G, [7:0] = B
//
//  ⇒ 模块内**不做任何颜色/通道重排、不做 alpha、不做缩放**。它只做搬运与对齐。
//
//  1. 地址与突发（契约 §2/§4：arsize=8B、arburst=INCR、arlen ≤ 15）
//  ------------------------------------------------------------------
//  · 每行只读 **有效像素区** ``[0, W*3)`` 字节：黑边已经烘进缓冲，PL 不需要
//    也不会去读行尾 padding 或有效区外的行（``y >= H`` 永不发起）。
//  · ``arlen = beats-1``，beats = min(8, 剩余拍数) ⇒ **每次突发 ≤ 8 拍（64 B）**，
//    既满足 ``arlen ≤ 15``，又在 stride 按 64 B 对齐（契约）时**天然不跨 4KB**。
//    另有一条 14 LUT 的页尾保护：地址落进页尾最后 64 B 时退化成 1 拍突发，
//    所以即使 stride 只有 8 B 对齐也不会违反 AXI 的 4KB 规则。
//  · 为什么不用 16 拍？16 拍（128 B）要额外算“到 4KB 边界的拍数”，约 30 LUT；
//    而 64 B 突发在 720p/1080p 下各为 60/90 个/行，行时间 ≫ 突发开销，带宽不受影响。
//  · 行首地址 = ``FB_ADDR + (FRAME_ID[0] ? FB_SIZE : 0) + y * FB_STRIDE``。
//  · **前提**：FB_ADDR 与 FB_STRIDE 都按 8 字节对齐（契约要求 64 B 对齐；
//    1280×3=3840、1920×3=5760、640×3=1920 全部满足）。
//
//  2. 行缓冲（24 bit × 深度 ≥ 2048 ⇒ 1~2 个 RAMB36）
//  ------------------------------------------------
//  64-bit 的 AXI 数据流与 24-bit 像素**不对齐**（8 B 与 3 B 互质），写侧必须做
//  字节重排。做法：3 个 64-bit beat = 一个 192-bit 的“24 字节组” = 恰好 8 个像素
//  （因为每行都从行首第 0 字节开始取，组边界恒定成立，与 FB_STRIDE 是否为 3 的
//  倍数无关）：
//
//      b0 ─┐
//      b1 ─┼─► {b2,b1,b0}[191:0] ─► 按 3 字节切片（组内像素号 P%8）─► BRAM
//      b2 ─┘
//
//  · 缓冲占用 ``room = 8B - 3P``（增量计数，5 bit）：
//      收拍准入 ``room <= 16``（槽 s 的上一任 chunk B-3 已被读完；
//                          B < 3 时槽还空 ⇒ 恒安全）
//      弹出条件 ``room >= 3``（该像素 3 个字节已到）且 ``P < W``
//    ⇒ "下一组"的拍可以与本组的弹出重叠，稳态吞吐 **1 像素/拍**
//      （若串行化，3 拍写 + 8 拍弹 = 0.73 像素/拍，720p 会掉队）
//  · 取数按 **拍** 记账（``beats_left``），不再维护 17-bit 字节计数（省 ~30 LUT）
//  ⇒ 稳态吞吐 = 1 像素/s_axi_aclk = 100 Mpx/s @100 MHz = **300 MB/s**
//
//  3. 吞吐与模式
//  -------------
//    720p60  : 74.25 Mpx/s × 3 B = 223 MB/s  ⇒ 100 MHz AXI 够（余量 ~26%）
//    1080p30 : 74.25 Mpx/s       = 223 MB/s  ⇒ 够
//    640×480 : 25.18 Mpx/s       =  76 MB/s  ⇒ 够
//    1080p60 : 148.5 Mpx/s × 3 B = 445 MB/s  ⇒ **100 MHz 不够**
//  ⇒ 模式 1（1080p60）必须把 s_axi_aclk 提到 ≥ 150 MHz（契约 §1 已允许改 AXI
//    时钟），否则行缓冲会被读空 ⇒ ``STATUS[2] scan_underflow`` 置 1（可诊断，
//    不是静默错误）。本模块与两域频率比例无关。
//
//  4. 换帧（防撕裂，契约 §4）
//  -------------------------
//  · PS 写 ``FRAME_ID``（0x20）→ 本模块在 **vsync 边界**采纳到 ``fid_latched``；
//  · 采纳后**后续所有行**都按新帧号选缓冲 ⇒ 一帧之内绝不混两帧；
//  · ``FRAME_ID_LATCHED``（0x24）回读 = 已采纳值（跨域 toggle 握手发布）。
//  · 缓冲选择：``buf = FRAME_ID[0]``，块大小 ``FB_SIZE = align64(FB_STRIDE*FB_H)``。
//    PS 侧 ``put_frame`` 必须把第 n 帧写进 ``buf = n & 1``（hdmi_out.py 正是如此），
//    双方靠“帧号最低位”约定对齐，**不需要新增寄存器**（0x00~0x28 冻结）。
//
//  5. 状态位
//  --------
//  · ``st_underflow`` = STATUS[2] scan_underflow：行缓冲被读空。判据在像素域：
//    取数器发布“正在取第几行 + 本行已写多少像素”（行号走 toggle 握手、进度走
//    gray），像素侧逐像素比：
//        hdr_line == 当前行 ⇒ 读空当且仅当 vx >= 本行已写像素数
//        hdr_line  < 当前行 ⇒ 取数落后一整行（含请求被跳过）⇒ 读空
//        hdr_line  > 当前行 ⇒ 取数已跑在前面 ⇒ 安全
//    **置 1 后保持**，直到 ``cfg_soft_reset``（CTRL[1]）拉高一个时钟。
//    行内计数 12 bit（1920 < 2048）即可，比“全域像素计数器”省 ~85 LUT。
//  · ``st_vsync_seen`` = STATUS[1]：vsync 发生过（同样由 soft_reset 清）。
//
//  6. 跨时钟域（s_axi_aclk ⇄ pix_clk）——每一位都交代清楚
//  ------------------------------------------------------
//    信号                      方向        手段
//    ------------------------- ---------- -------------------------------------
//    cfg 快照（addr/stride/     AXI→PIX    **bundled-data + toggle 握手**：数据先
//      w/h/en/fsize，多 bit）               稳定 ≥2 个 AXI 拍，再翻 toggle；PIX 侧
//                                          3FF 同步后取边沿，边沿处一次性采样整捆
//                                          （数据在整个握手期间被 AXI 侧冻结）
//    FRAME_ID（32 bit）         AXI→PIX    同上（数据 + toggle，vsync 处采样）
//    FRAME_ID_LATCHED（32 bit） PIX→AXI    同上（反向）
//    line 请求（行号 + 奇偶）   PIX→AXI    同上（数据 + toggle + ack 电平回程）
//    cfg_upd / fid_upd          —          单 bit，来自 AXI-Lite 寄存器文件的写脉冲
//                                          （只在本域用，不跨域）
//    行内写进度（12 bit）       AXI→PIX    **gray 码**，逐 bit 2FF 同步（单 bit 跳变
//                                          ⇒ 任何采样时刻都不会“半新半旧”）
//    行头（行号 + toggle）      AXI→PIX    同 1 号手法（数据 + toggle 握手）
//    soft_reset / 粘滞状态位    AXI⇄PIX    单 bit 电平 2FF（再用边沿检测）
//    行缓冲 BRAM 数据           AXI→PIX    **真双口（2 时钟）BRAM**：A 口写
//                                          (s_axi_aclk)、B 口读 (pix_clk)；读写指针
//                                          由上面的 gray 计数器保证“先写后读”，
//                                          数据通路本身不需要同步器
//    行缓冲读地址               PIX 内部   —（不回 AXI 域）
//
//  7. 复位
//  ------
//  · ``aresetn``（AXI 域，低有效）、``pix_aresetn``（pix 域，低有效）各自复位本域；
//  · ``cfg_soft_reset`` 是运行期复位：清粘滞状态位、清 AXI 侧取数 FSM 与计数器，
//    **不清配置寄存器**（PS 不需要重新 configure）。
//
//  8. 端口时序约定（与 hdmi_timing_gen.v 的接线）
//  ---------------------------------------------
//  · 输入 ``in_vde/in_vx/in_vy``：vde 高 = 有效像素；vx ∈ [0,W)、vy ∈ [0,H)，
//    **只在 vde 期间有意义**，vy 只在有效行内递增（0 = 第一有效行）。
//  · 输入 ``in_vsync``：1 个 pix_clk 宽的场起始脉冲（在有效区之前，不在有效区内）。
//  · 输出 ``pix_de/pix_x/pix_y/pix_rgb``：比输入**晚 1 拍**（BRAM 读 1 拍），
//    ``pix_x/pix_y/pix_de`` 与 ``pix_rgb`` **同一延迟** ⇒ 下游按 ``pix_de`` 采
//    ``pix_rgb`` 即可，不需要自己再对齐。
//  · ``pix_valid`` = pix_de && enable && 该像素确实已写入行缓冲（非 underflow）。
//    下游推荐：``de_out = pix_de``（时序不能抖），``rgb_out = pix_valid ? pix_rgb : 0``。
// ============================================================================
`timescale 1ns / 1ps

module hdmi_fb_scan #(
    // 行缓冲深度 = 2**LINE_AW 像素（11 ⇒ 2048，支持到 1920 宽；2560 宽需 12）
    parameter integer LINE_AW = 11,
    // 像素坐标位宽（决定 in_vx/in_vy/pix_x/pix_y 与行内写进度的位宽）
    parameter integer PIX_W   = 12
) (
    // ---------------------------------------------------------------- AXI 域
    input  wire                 s_axi_aclk,
    input  wire                 aresetn,

    // 配置（由 hdmi_out_top 的 AXI-Lite 寄存器直连；契约 §2 的 0x0C~0x1C）
    input  wire [31:0]          cfg_fb_addr_lo,   // 0x0C 帧缓冲物理地址 [31:0]
    input  wire [31:0]          cfg_fb_addr_hi,   // 0x10 本板 1 GB DDR ⇒ 恒 0（保留）
    input  wire [31:0]          cfg_fb_stride,    // 0x14 每行字节数
    input  wire [15:0]          cfg_fb_w,         // 0x18 有效像素宽度
    input  wire [15:0]          cfg_fb_h,         // 0x1C 有效行数
    input  wire [31:0]          cfg_frame_id,     // 0x20 PS 写入的新帧号
    input  wire                 fid_upd,          // 写 0x20 时 1 拍脉冲（由寄存器文件产生）
    input  wire                 cfg_upd,          // 写 0x00/0x08/0x0C~0x1C 时 1 拍脉冲
    input  wire                 cfg_enable,       // CTRL[0]
    input  wire                 cfg_soft_reset,   // CTRL[1]（1 个时钟的脉冲）

    // 状态回读（接 AXI-Lite 的 STATUS 0x04 / FRAME_ID_LATCHED 0x24）
    output wire [31:0]          frame_id_latched,
    output wire                 st_underflow,     // STATUS[2]
    output wire                 st_vsync_seen,    // STATUS[1]

    // ------------------------------------------------------------ AXI4 读主
    output wire [31:0]          m_axi_araddr,
    output wire [7:0]           m_axi_arlen,
    output wire [2:0]           m_axi_arsize,
    output wire [1:0]           m_axi_arburst,
    output wire                 m_axi_arvalid,
    input  wire                 m_axi_arready,
    input  wire [63:0]          m_axi_rdata,
    input  wire [1:0]           m_axi_rresp,    // 未使用（本设计不做错误恢复）
    input  wire                 m_axi_rlast,    // 未使用（按 outstanding 计数收拍）
    input  wire                 m_axi_rvalid,
    output wire                 m_axi_rready,

    // ------------------------------------------------------------- 像素域
    input  wire                 pix_clk,
    input  wire                 pix_aresetn,
    input  wire                 in_vde,
    input  wire [PIX_W-1:0]     in_vx,
    input  wire [PIX_W-1:0]     in_vy,
    input  wire                 in_vsync,

    output wire                 pix_de,
    output wire [PIX_W-1:0]     pix_x,
    output wire [PIX_W-1:0]     pix_y,
    output wire [23:0]          pix_rgb,
    output wire                 pix_valid
);

    localparam [2:0] ARSIZE_8B    = 3'b011;   // 8 字节/beat
    localparam [1:0] ARBURST_INCR = 2'b01;    // INCR

    // =====================================================================
    // 0. 配置快照（AXI 域）—— FB_SIZE 用“h 次累加”算，不用乘法器
    //    FB_SIZE = align64(FB_STRIDE * FB_H)
    // =====================================================================
    reg [31:0] s_addr, s_stride;
    reg [15:0] s_w, s_h;
    reg        s_en;
    reg [31:0] s_fsize;
    reg [11:0] s_line_beats;      // = ceil(W*3/8)，本行要取多少拍
    reg        cfg_tog, cfg_pend;

    reg        fs_busy;
    reg [15:0] fs_cnt;
    reg [31:0] fs_acc, fs_add;
    reg [31:0] l_addr, l_stride;
    reg [15:0] l_w, l_h;
    reg        l_en;


    always @(posedge s_axi_aclk or negedge aresetn) begin
        if (!aresetn) begin
            s_addr <= 32'd0; s_stride <= 32'd0; s_w <= 16'd0; s_h <= 16'd0;
            s_en <= 1'b0; s_fsize <= 32'd0; s_line_beats <= 12'd0;
            cfg_tog <= 1'b0; cfg_pend <= 1'b0;
            fs_busy <= 1'b0; fs_cnt <= 16'd0; fs_acc <= 32'd0; fs_add <= 32'd0;
            l_addr <= 32'd0; l_stride <= 32'd0; l_w <= 16'd0; l_h <= 16'd0; l_en <= 1'b0;
        end else if (cfg_soft_reset) begin
            // 取消进行中的配置提交（已生效的配置值不动）
            fs_busy  <= 1'b0;
            cfg_pend <= 1'b0;
        end else if (!fs_busy && !cfg_pend && cfg_upd) begin
            fs_busy  <= 1'b1;
            fs_cnt   <= 16'd1;
            fs_acc   <= cfg_fb_stride;
            fs_add   <= cfg_fb_stride;
            l_addr   <= cfg_fb_addr_lo;
            l_stride <= cfg_fb_stride;
            l_w      <= cfg_fb_w;
            l_h      <= cfg_fb_h;
            l_en     <= cfg_enable;
        end else if (fs_busy) begin
            if (fs_cnt >= l_h) begin
                fs_busy      <= 1'b0;
                s_addr       <= l_addr;
                s_stride     <= l_stride;
                s_w          <= l_w;
                s_h          <= l_h;
                s_en         <= l_en;
                s_fsize      <= (fs_acc + 32'd63) & ~32'h3F;       // align64
                // ceil(W*3/8)
                s_line_beats <= ({1'b0, l_w} + {1'b0, l_w, 1'b0} + 14'd7) >> 3;
                cfg_pend     <= 1'b1;
            end else begin
                fs_cnt <= fs_cnt + 16'd1;
                fs_acc <= fs_acc + fs_add;
            end
        end else if (cfg_pend) begin
            cfg_tog  <= ~cfg_tog;   // 数据已稳定 ≥1 拍，此刻才翻 toggle
            cfg_pend <= 1'b0;
        end
    end

    // ---- 像素域接收配置快照（bundled data + toggle）---------------------
    reg [2:0] ctg;
    always @(posedge pix_clk or negedge pix_aresetn) begin
        if (!pix_aresetn) ctg <= 3'd0;
        else              ctg <= {ctg[1:0], cfg_tog};
    end
    wire cfg_evt = ctg[2] ^ ctg[1];

    reg [15:0] p_h;
    reg        p_en;
    always @(posedge pix_clk or negedge pix_aresetn) begin
        if (!pix_aresetn) begin
            p_h <= 16'd0; p_en <= 1'b0;
        end else if (cfg_evt) begin
            // 此刻 AXI 侧的 s_* 已冻结 ≥2 个 AXI 拍 ⇒ 整捆采样安全
            p_h  <= s_h;
            p_en <= s_en;
        end
    end

    // =====================================================================
    // 1. FRAME_ID：AXI 域采集 → 像素域在 vsync 采纳 → 回读发布
    // =====================================================================
    reg [31:0] fid_val, fid_seen;
    reg        fid_tog, fid_pend;
    always @(posedge s_axi_aclk or negedge aresetn) begin
        if (!aresetn) begin
            fid_val <= 32'd0; fid_seen <= 32'd0; fid_tog <= 1'b0; fid_pend <= 1'b0;
        end else if (fid_upd) begin
            fid_seen <= cfg_frame_id;     // 第 1 拍：数据落盘
            fid_val  <= cfg_frame_id;
            fid_pend <= 1'b1;
        end else if (fid_pend) begin
            fid_tog  <= ~fid_tog;         // 第 2 拍：翻 toggle
            fid_pend <= 1'b0;
        end
    end

    reg [3:0]  ftg;
    reg [31:0] fid_latched;
    reg        fid_ack;
    always @(posedge pix_clk or negedge pix_aresetn) begin
        if (!pix_aresetn) begin
            ftg <= 4'd0; fid_latched <= 32'd0; fid_ack <= 1'b0;
        end else begin
            ftg <= {ftg[2:0], fid_tog};
            if (in_vsync && (ftg[2] != fid_ack)) begin
                fid_latched <= fid_val;   // **只在 vsync 边界采纳**（防撕裂）
                fid_ack     <= ftg[2];
            end
        end
    end

    // vsync 当拍“正要采纳”的帧号奇偶（同一拍 fid_latched 还是旧值）
    wire fid_eff_par = (in_vsync && (ftg[2] != fid_ack)) ? fid_val[0] : fid_latched[0];

    // ---- 发布回 AXI 域（数据先、toggle 后）-----------------------------
    reg [31:0] fl_val, fl_seen;
    reg        fl_tog, fl_pend;
    always @(posedge pix_clk or negedge pix_aresetn) begin
        if (!pix_aresetn) begin
            fl_val <= 32'd0; fl_tog <= 1'b0; fl_pend <= 1'b0; fl_seen <= 1'b0;
        end else if (fid_latched != fl_seen) begin
            fl_seen <= fid_latched;
            fl_val  <= fid_latched;
            fl_pend <= 1'b1;
        end else if (fl_pend) begin
            fl_tog  <= ~fl_tog;
            fl_pend <= 1'b0;
        end
    end

    reg [2:0]  fltg;
    reg [31:0] fid_latched_axi;
    always @(posedge s_axi_aclk or negedge aresetn) begin
        if (!aresetn) begin
            fltg <= 3'd0; fid_latched_axi <= 32'd0;
        end else begin
            fltg <= {fltg[1:0], fl_tog};
            if (fltg[2] ^ fltg[1]) fid_latched_axi <= fl_val;
        end
    end
    assign frame_id_latched = fid_latched_axi;

    // =====================================================================
    // 2. 取数 FSM 的请求握手：像素域（何时该取哪一行） → AXI 域（去取）
    // =====================================================================
    localparam [2:0] F_IDLE = 3'd0,
                     F_BASE = 3'd1,   // 行首地址（增量追赶，通常 1 拍）
                     F_AR   = 3'd2,   // 发突发
                     F_FIN  = 3'd3;   // 等在途拍收完 + 像素写完

    reg [2:0] fst;

    reg [PIX_W-1:0] rq_line;
    reg             rq_fpar, rq_tog;
    reg [2:0]       rtg;              // rq_tog 的 3FF 同步（边沿用 rtg[2]^rtg[1]）
    reg [PIX_W-1:0] a_line;
    reg             a_fpar, req_ack;  // req_ack 同时是“已受理”的回程电平

    // 只做同步，不驱动任何握手状态（避免与主 FSM 双驱动）
    always @(posedge s_axi_aclk or negedge aresetn) begin
        if (!aresetn) rtg <= 3'd0;
        else          rtg <= {rtg[1:0], rq_tog};
    end

    // ---- 像素域：何时请求、请求哪一行 ----------------------------------
    reg         in_vde_q;
    reg [PIX_W-1:0] in_vy_q;
    reg [1:0]   ack_s;
    always @(posedge pix_clk or negedge pix_aresetn) begin
        if (!pix_aresetn) begin
            in_vde_q <= 1'b0; in_vy_q <= {PIX_W{1'b0}}; ack_s <= 2'd0;
        end else begin
            in_vde_q <= in_vde;
            in_vy_q  <= in_vy;
            ack_s    <= {ack_s[0], req_ack};
        end
    end

    wire             de_fall   = in_vde_q & ~in_vde;                 // 本行结束（进 hblank）
    wire             rq_free   = (ack_s[1] == rq_tog);               // 上一次请求已被取走
    wire [PIX_W-1:0] line_nxt  = in_vy_q + 1'b1;
    wire             rq_want_de = p_en & de_fall & (line_nxt < p_h);

    always @(posedge pix_clk or negedge pix_aresetn) begin
        if (!pix_aresetn) begin
            rq_line <= {PIX_W{1'b0}}; rq_fpar <= 1'b0; rq_tog <= 1'b0;
        end else if (rq_free) begin
            if (in_vsync && p_en && (p_h != 16'd0)) begin
                // 新的一帧：请求第 0 行，奇偶 = 本场刚采纳的帧号
                rq_line <= {PIX_W{1'b0}};
                rq_fpar <= fid_eff_par;
                rq_tog  <= ~rq_tog;
            end else if (rq_want_de) begin
                rq_line <= line_nxt;
                rq_fpar <= fid_latched[0];
                rq_tog  <= ~rq_tog;
            end
        end
    end

    // =====================================================================
    // 3. 取数 FSM + 行缓冲写入（全在 s_axi_aclk 域，单 always 块单驱动）
    // =====================================================================
    reg [31:0]      line_base;      // 当前请求行的行首字节地址
    reg [PIX_W-1:0] line_cur;       // line_base 对应的行号（增量追赶用）
    reg             cur_fpar, cur_valid;
    reg [31:0]      faddr;
    reg [11:0]      beats_left;     // 本行还需发起的拍数
    reg [11:0]      outstanding;

    // ---- 拍数：每次突发固定 ≤ 8 拍（64 B）-----------------------------
    // 为什么不取 16 拍（128 B）？因为契约要求 stride 按 64 B 对齐 ⇒ 64 B 对齐的
    // 突发**永远不会跨 4KB 边界**，于是整套"到 4KB 边界的拍数"计算（~30 LUT）
    // 可以完全省掉；arlen=7 ≤ 15 仍满足契约。行宽 1920 px 时每行 90 个突发，
    // 行时间 2200 拍 ≫ 90×(8+2) 拍 ⇒ 带宽与突发开销都不是瓶颈。
    // 页保护：8 拍 = 64 B，从 8B 对齐地址出发只有在页尾最后 64 B 内才可能跨 4KB。
    // stride 按 64 B 对齐（契约）时永不触发；对任意 8B 对齐 stride 也能退化到
    // "1 拍一次"安全跨页（正常 64B 对齐时 0 代价）。
    wire [11:0] off8      = {1'b0, faddr[11:3]};                 // 页内偏移/8 (0..511)
    wire        page_edge = (off8 > 12'd504);
    wire [11:0] beats_cap = page_edge ? 12'd1 : 12'd8;
    wire [11:0] beats_min = (beats_left < beats_cap) ? beats_left : beats_cap;
    wire [3:0]  beats_nxt = beats_min[3:0];                      // 1..8
    wire [11:0] ar_beats12 = {8'd0, beats_nxt};

    wire ar_fire = m_axi_arvalid & m_axi_arready;
    wire r_fire  = m_axi_rvalid  & m_axi_rready;

    // ---- 字节重排（3×64bit = 8 像素）------------------------------------
    // 24 字节 = 3 个 64-bit 槽 ≡ 8 个像素。槽内字节布局固定：
    //   b0 = 本组字节 0..7, b1 = 8..15, b2 = 16..23 ⇒ {b2,b1,b0} 就是"组内扁平向量"
    reg [63:0]      b0, b1, b2;
    reg [1:0]       slot;          // 下一个待写槽（0/1/2）
    reg [11:0]      beats_done;    // 本行已收拍数 B
    reg [11:0]      pops_done;     // 本行已弹像素数 P
    reg [11:0]      wr_lp;         // 本行已写入像素数（gray 发布给像素域）
    reg [PIX_W-1:0] pub_line;      // 正在取的行号（bundled + toggle 发布）
    reg             pub_tog, pub_pend;

    wire [191:0] acc     = {b2, b1, b0};
    wire [10:0]  pop_off = ({8'd0, pops_done[2:0]} << 4) + ({8'd0, pops_done[2:0]} << 3);
    wire [23:0]  pop_pix = acc[pop_off +: 24];

    // 收拍准入（槽安全）：槽 s 的上一任是 chunk B-3，它被读完当且仅当 3P >= 8(B-2)
    //   B < 3  ⇒ 槽还是空的，恒安全
    // room = 已到字节 − 已弹字节 = 8B − 3P（"还在缓冲里没弹出去的字节数"）。
    // 用增量计数器维护（+8 收拍 / −3 弹像素），比每次做 8B−3P 省 ~30 LUT；
    // 上界由收拍准入保证（≤16 才接，接完 ≤24 ⇒ 5 bit 够）。
    reg  [5:0] room;
    // 槽 s 的上一任是 chunk B-3，它被读完 ⟺ 8B−3P ≤ 16（B<3 时槽是空的，恒安全）
    wire        beat_ok   = (beats_done < 12'd3) || (room <= 6'd16);
    wire        pop_en    = (room >= 6'd3) && ({4'd0, pops_done} < {4'd0, s_w});
    wire        beat_accept = r_fire;

    // F_BASE 里 line_cur == a_line 的那一拍 = 新行开始（清行内状态 + 装 faddr）
    wire line_start = (fst == F_BASE) && (line_cur == a_line);

    // 行头发布：新行开始那一拍先把行号落盘，下一拍再翻 toggle（数据先、toggle 后）
    wire pub_fire = pub_pend;

    always @(posedge s_axi_aclk or negedge aresetn) begin
        if (!aresetn) begin
            fst <= F_IDLE;
            line_base <= 32'd0; line_cur <= {PIX_W{1'b0}};
            cur_fpar <= 1'b0; cur_valid <= 1'b0;
            faddr <= 32'd0; beats_left <= 12'd0; outstanding <= 12'd0;
            a_line <= {PIX_W{1'b0}}; a_fpar <= 1'b0; req_ack <= 1'b0;
            slot <= 2'd0; beats_done <= 12'd0; pops_done <= 12'd0; room <= 6'd0;
            b0 <= 64'd0; b1 <= 64'd0; b2 <= 64'd0;
            wr_lp <= 12'd0; pub_line <= {PIX_W{1'b0}}; pub_tog <= 1'b0; pub_pend <= 1'b0;
        end else if (cfg_soft_reset) begin
            fst <= F_IDLE;
            line_base <= 32'd0; line_cur <= {PIX_W{1'b0}};
            cur_fpar <= 1'b0; cur_valid <= 1'b0;
            faddr <= 32'd0; beats_left <= 12'd0; outstanding <= 12'd0;
            slot <= 2'd0; beats_done <= 12'd0; pops_done <= 12'd0; room <= 6'd0;
            wr_lp <= 12'd0; pub_tog <= 1'b0; pub_pend <= 1'b0;
        end else begin

            // ---------------- 在途拍计数（收发同拍合并）-------------------
            outstanding <= outstanding + (ar_fire ? ar_beats12 : 12'd0)
                                       - (r_fire  ? 12'd1 : 12'd0);

            // ---------------- 行缓冲写（字节重排）-------------------------
            if (line_start) begin
                // 新行的第 0 字节：清行内状态 + 发布行头（数据先落盘）
                slot <= 2'd0; beats_done <= 12'd0; pops_done <= 12'd0;
                room <= 6'd0;
                wr_lp <= 12'd0; pub_line <= a_line; pub_pend <= 1'b1;
            end else begin
                if (pub_pend) pub_tog <= ~pub_tog;      // 行头已稳定 ≥1 拍
                if (beat_accept) begin
                    case (slot)
                        2'd0:    b0 <= m_axi_rdata;
                        2'd1:    b1 <= m_axi_rdata;
                        default: b2 <= m_axi_rdata;
                    endcase
                    slot       <= (slot == 2'd2) ? 2'd0 : (slot + 2'd1);
                    beats_done <= beats_done + 12'd1;
                end
                if (pop_en) begin
                    pops_done <= pops_done + 12'd1;
                    wr_lp     <= wr_lp + 12'd1;
                end
                room <= room + (beat_accept ? 6'd8 : 6'd0)
                             - (pop_en     ? 6'd3 : 6'd0);
                if (pub_pend) pub_pend <= 1'b0;
            end

            // ---------------- 取数 FSM ------------------------------------
            case (fst)
                F_IDLE: begin
                    if (rtg[2] != req_ack) begin
                        // 受理一次行请求：数据（rq_line/rq_fpar）与 toggle 是
                        // bundled-data，自 toggle 边沿起就稳定，整捆采样安全
                        req_ack <= rtg[2];
                        a_line  <= rq_line;
                        a_fpar  <= rq_fpar;
                        fst     <= F_BASE;
                        // 基准一旦失效（换帧 / 回退 / 乱序），从行 0 重新起算
                        if (!cur_valid || (cur_fpar != rq_fpar) || (rq_line < line_cur)) begin
                            cur_fpar  <= rq_fpar;
                            cur_valid <= 1'b1;
                            line_cur  <= {PIX_W{1'b0}};
                            line_base <= s_addr + (rq_fpar ? s_fsize : 32'd0);
                        end
                    end
                end

                F_BASE: begin
                    if (line_cur == a_line) begin
                        // 行首地址就绪（同拍 line_start 清行内状态）
                        faddr      <= line_base;
                        beats_left <= s_line_beats;
                        fst        <= F_AR;
                    end else begin
                        line_base <= line_base + s_stride;
                        line_cur  <= line_cur + 1'b1;
                    end
                end

                F_AR: begin
                    if (beats_left == 12'd0) begin
                        fst <= F_FIN;
                    end else if (ar_fire) begin
                        faddr      <= faddr + (ar_beats12 << 3);
                        beats_left <= beats_left - ar_beats12;
                    end
                end

                F_FIN: begin
                    if ((outstanding == 12'd0) && ({4'd0, pops_done} >= {4'd0, s_w})) fst <= F_IDLE;
                end

                default: fst <= F_IDLE;
            endcase
        end
    end

    // 行缓冲写口（AXI 域）
    wire             bram_we    = pop_en && !line_start;
    wire [PIX_W-1:0] bram_waddr = pops_done[PIX_W-1:0];
    // 内存字节序 = {R,G,B}（b0=R），而 pix_rgb 要 {R,G,B} = 高字节在前 ⇒ 这里只换线序
    wire [23:0]      bram_wdata = {pop_pix[7:0], pop_pix[15:8], pop_pix[23:16]};

    assign m_axi_araddr  = faddr;
    assign m_axi_arlen   = {3'd0, beats_nxt} - 8'd1;
    assign m_axi_arsize  = ARSIZE_8B;
    assign m_axi_arburst = ARBURST_INCR;
    assign m_axi_arvalid = (fst == F_AR) && (beats_left != 12'd0) &&
                           (outstanding <= 12'd16) && !cfg_soft_reset;
    assign m_axi_rready  = beat_ok && (outstanding != 12'd0) && !cfg_soft_reset;

    // ---- 行内写进度 gray 发布（AXI → PIX）------------------------------
    // 只需"本行写到第几个像素"，12 bit 足够（1920 < 2048），比 24 bit 全域计数器
    // 省一半 LUT；行号由 pub_line + toggle 单独发布，两者合起来足以判 underflow。
    wire [11:0] wr_lp_gray = wr_lp ^ (wr_lp >> 1);
    reg  [11:0] wr_gray_q;
    reg  [PIX_W-1:0] pub_line_q;
    reg         pub_tog_q;
    always @(posedge s_axi_aclk or negedge aresetn) begin
        if (!aresetn) begin
            wr_gray_q <= 12'd0; pub_line_q <= {PIX_W{1'b0}}; pub_tog_q <= 1'b0;
        end else begin
            wr_gray_q  <= wr_lp_gray;
            pub_line_q <= pub_line;
            pub_tog_q  <= pub_tog;
        end
    end

    // =====================================================================
    // 4. 像素域：行缓冲读 → 输出流水（延迟 1 拍，坐标同延迟）
    // =====================================================================
    (* ram_style = "block" *) reg [23:0] line_buf [0:(1<<LINE_AW)-1];

    // 写口：s_axi_aclk（A 口）
    always @(posedge s_axi_aclk) begin
        if (bram_we) line_buf[bram_waddr[LINE_AW-1:0]] <= bram_wdata;
    end

    // 读口：pix_clk（B 口）—— 真双口 BRAM，无需数据同步器
    wire [PIX_W-1:0] rd_addr = in_vx;
    reg  [23:0]      rd_data;
    always @(posedge pix_clk) begin
        rd_data <= line_buf[rd_addr[LINE_AW-1:0]];
    end

    // ---- 行内写进度 gray → binary（逐 bit 2FF，单跳变无撕裂）------------
    reg [11:0] wg_s1, wg_s2;
    always @(posedge pix_clk or negedge pix_aresetn) begin
        if (!pix_aresetn) begin wg_s1 <= 12'd0; wg_s2 <= 12'd0; end
        else              begin wg_s1 <= wr_gray_q; wg_s2 <= wg_s1; end
    end

    integer gi;
    reg [11:0] wl_bin;
    always @* begin
        wl_bin[11] = wg_s2[11];
        for (gi = 10; gi >= 0; gi = gi - 1)
            wl_bin[gi] = wl_bin[gi+1] ^ wg_s2[gi];
    end

    // ---- 行头（行号 + toggle）2FF 接收 ---------------------------------
    reg [2:0]       ptg;
    reg [PIX_W-1:0] hdr_line;
    always @(posedge pix_clk or negedge pix_aresetn) begin
        if (!pix_aresetn) begin ptg <= 3'd0; hdr_line <= {PIX_W{1'b0}}; end
        else begin
            ptg <= {ptg[1:0], pub_tog_q};
            if (ptg[2] ^ ptg[1]) hdr_line <= pub_line_q;   // 边沿处整捆采样
        end
    end

    // ---- underflow 判据 -------------------------------------------------
    //   hdr_line == 当前行 ⇒ 本行正在取，逐像素看进度（vx >= 已写数 即读空）
    //   hdr_line  < 当前行 ⇒ 取数落后一整行（含"请求被跳过"）⇒ 读空
    //   hdr_line  > 当前行 ⇒ 取数已跑在前面 ⇒ 安全
    reg  uf_en;                      // soft_reset 后到下一次 vsync 之间不判
    reg  uf_sticky, vs_sticky;
    reg  [1:0] sr_s;
    reg  sr_q;

    // 把"取数进度(hdr_line, wl_bin)"与"显示进度(in_vy, in_vx)"做一次字典序比较：
    // 两个分量都 < 2^12 且 W ≤ 4096 ⇒ 字典序 == 数值序，省掉两组 12-bit 比较器。
    wire        uf_now = in_vde && p_en && uf_en &&
                         !({hdr_line, wl_bin} > {in_vy, in_vx});

    always @(posedge pix_clk or negedge pix_aresetn) begin
        if (!pix_aresetn) begin
            uf_en <= 1'b0; uf_sticky <= 1'b0; vs_sticky <= 1'b0;
            sr_s <= 2'd0; sr_q <= 1'b0;
        end else begin
            sr_s <= {sr_s[0], cfg_soft_reset};
            sr_q <= sr_s[1];
            if (sr_s[1] & ~sr_q) begin           // soft_reset 上升沿
                uf_en     <= 1'b0;
                uf_sticky <= 1'b0;
                vs_sticky <= 1'b0;
            end else begin
                if (in_vsync) uf_en <= 1'b1;     // 下一场的第一个像素起才判
                if (uf_now)   uf_sticky <= 1'b1;
                if (in_vsync) vs_sticky <= 1'b1;
            end
        end
    end

    // 粘滞位 2FF → AXI 域（STATUS 读）
    reg [1:0] uf_s, vs_s;
    always @(posedge s_axi_aclk or negedge aresetn) begin
        if (!aresetn) begin uf_s <= 2'd0; vs_s <= 2'd0; end
        else begin uf_s <= {uf_s[0], uf_sticky}; vs_s <= {vs_s[0], vs_sticky}; end
    end
    assign st_underflow  = uf_s[1];
    assign st_vsync_seen = vs_s[1];

    // ---- 输出流水（1 拍）------------------------------------------------
    reg               pix_de_q, pix_valid_q;
    reg [PIX_W-1:0]   pix_x_q, pix_y_q;
    always @(posedge pix_clk or negedge pix_aresetn) begin
        if (!pix_aresetn) begin
            pix_de_q <= 1'b0; pix_valid_q <= 1'b0; pix_x_q <= {PIX_W{1'b0}};
            pix_y_q <= {PIX_W{1'b0}};
        end else begin
            pix_de_q    <= in_vde;
            pix_x_q     <= in_vx;
            pix_y_q     <= in_vy;
            pix_valid_q <= in_vde && p_en && !uf_now;
        end
    end

    assign pix_de    = pix_de_q;
    assign pix_x     = pix_x_q;
    assign pix_y     = pix_y_q;
    // BRAM 读天然是 1 拍：rd_data 与"延迟 1 拍的 de/x/y"同拍，直接输出即可。
    // enable=0 时本模块不写行缓冲、pix_valid 恒 0，rgb 无效值由顶层（彩条/使能
    // 复用器）丢弃 ⇒ 这里不再花 24 个 LUT 做掩码。
    assign pix_rgb   = rd_data;
    assign pix_valid = pix_valid_q;

endmodule
