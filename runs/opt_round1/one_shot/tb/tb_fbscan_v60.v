// ============================================================================
//  tb_fbscan_repro.v —— hdmi_fb_scan 板端「AXI < PIX ⇒ 每行读空」现象复现台
//
//  里程碑 1 / 自包含 TB（不依赖其它 RTL，只例化 hdmi_fb_scan.v）
//  ------------------------------------------------------------------------
//  板端硬事实（作为 golden 输入，不在本 TB 里质疑）：
//    · 像素时钟 74.21875 MHz（720p60，H_TOTAL=1650 / H_ACTIVE=1280 /
//      V_TOTAL=750 / V_ACTIVE=720，与 hdmi_timing_gen.v MODE0 完全一致）
//    · s_axi_aclk 三档：37.109375 / 50.000 / 66.666667 MHz
//    · 板端：50 MHz ⇒ 0x2C(UNDERFLOW_CNT) 以 ~45000 行/s 增长、屏幕全黑；
//            66.67 MHz ⇒ 增量 0；37.09 MHz 未测。
//
//  TB 结构（三块，边界清楚）
//  ------------------------
//  (1) 视频时序发生器：与 hdmi_timing_gen.v 的 MODE0 逐拍等价（同样的寄存
//      输出口径、同样的 vsync 位置）；in_vsync 按 hdmi_out_top 的做法取
//      v_vs 的上升沿拍（1 个 pix_clk 宽）。
//  (2) AXI4 读从机行为模型：无限带宽、无随机等待、突发内背靠背、
//      **首拍延迟 R_LAT 可参数化**（0 / 20 / 60 ...），数据按地址可复算。
//      ⇒ 若这个理想从机下仍然读空，瓶颈就不在总线延迟/带宽。
//  (3) 统计与追踪：以 DUT 输出流（pix_de/pix_valid）判「行是否读空」，
//      并在失败档打印 toggle 请求/应答、行计数、写指针、pix_valid 的时间线。
//
//  运行（plusargs）
//  ---------------
//    +AXI_HALF_PS=10000 +PIX_HALF_PS=6737 +R_LAT=0 +NLINES=1200
//    +CFG_W=1280 +CFG_H=720 +TRACE_N=8 +TRACE_FRAME=1 +TAG=50.00MHz
//
//  timescale 1ps/1ps ⇒ 所有 #延迟 都以 ps 为单位
// ============================================================================
`timescale 1ps/1ps

module tb_fbscan_v60;

    // ------------------------------------------------------------- 可调参数
    integer AXI_HALF_PS = 10000;      // aclk 半周期(ps)：10000 ⇒ 50.000 MHz
    integer PIX_HALF_PS = 6737;       // pclk 半周期(ps)：6737  ⇒ 74.217  MHz
    integer R_LAT       = 0;          // AXI 读首拍延迟（aclk 拍）
    integer R_LAT_MODE  = 1;          // 1=流水式（空闲后只付一次 L，贴近 HP 口）
                                      // 0=每个突发都付 L（极端保守）
    integer NLINES      = 1200;       // 至少跑多少条有效行
    integer CFG_W       = 1280;
    integer CFG_H       = 720;
    integer TRACE_N     = 8;          // 追踪帧里打印前 N 行
    integer TRACE_FRAME = 1;          // 在第几帧追踪
    reg [8*32-1:0] TAG  = "unnamed";
    // 重新武装用例（enable → 3 帧 → enable=0 保持 N 拍 → enable=1 → 3 帧）
    integer REARM      = 0;
    integer REARM_OFF  = 64;
    integer REARM_FRAMES = 3;
    integer DBG          = 0;      // 1 = 打印行缓冲四个 bank 的读出值（定位错行/错 bank）
    integer REARM_MID    = 0;      // 1 = 在"行内"（in_vde 且 in_vx==REARM_MID_X）关 enable
    integer REARM_MID_X  = 400;
    integer REARM_SR     = 0;      // 1 = 追加 soft_reset 与 enable 同时变的用例
    // ★ v60 S3：跨域用例——一串快速 cfg / FRAME_ID 写（间隔跨过 fs_busy 窗口），
    //   判据：终值必达（s_en/p_en/fid_latched）+ 最后 3 帧 cov=1.0
    //        + underflow_cnt == TB 自己数出的"有欠载的行数"（计数不丢）
    integer CDCWAVE      = 0;
    integer FID_LAST     = 0;
    integer tb_uf_rows   = 0;
    integer cdc_k = 0, cdc_gap = 0, cdc_F1 = 0;
    reg     row_uf_hit   = 1'b0;
    integer settle;

    // ------------------------------------------------------------- 视频时序
    // 时序常数：默认 720p60，可用 +H_TOTAL/+H_ACTIVE/+V_TOTAL/+V_ACTIVE/+V_FP/+V_SYNC 覆盖
    integer H_TOTAL  = 1650;
    integer H_ACTIVE = 1280;
    integer V_TOTAL  = 750;
    integer V_ACTIVE = 720;
    integer V_FP     = 5;
    integer V_SYNC   = 5;
    integer V_START  = 725;          // = V_ACTIVE + V_FP
    localparam [31:0]  FB_ADDR  = 32'h1000_0000;

    // ------------------------------------------------------------- 时钟/复位
    reg aclk = 1'b0;
    reg pclk = 1'b0;
    // ★ v60：扫描域独立时钟（scan_clk）。默认与 aclk 完全相同（复现 v3 的
    //   三档结论）；给 +SCAN_HALF_PS 就变成独立域（S3 的跨域用例，支持
    //   非整数频率比 + 相位差）。
    integer SCAN_HALF_PS = 0;         // 0 ⇒ 跟随 AXI_HALF_PS
    integer SCAN_PHASE_PS = 0;        // scan_clk 的起始相位（ps）
    reg sclk = 1'b0;
    always #(AXI_HALF_PS) aclk = ~aclk;
    always #(PIX_HALF_PS) pclk = ~pclk;
    initial begin
        #(SCAN_PHASE_PS);
        forever #((SCAN_HALF_PS == 0) ? AXI_HALF_PS : SCAN_HALF_PS) sclk = ~sclk;
    end

    reg aresetn  = 1'b0;
    reg preseten = 1'b0;

    // ------------------------------------------------------------- DUT 端口
    reg  [31:0] cfg_addr   = FB_ADDR;
    reg  [31:0] cfg_stride = 32'd3840;
    reg  [15:0] cfg_w      = 16'd1280;
    reg  [15:0] cfg_h      = 16'd720;
    reg  [31:0] cfg_fid    = 32'd0;
    reg         cfg_en     = 1'b1;
    reg         cfg_srst   = 1'b0;
    reg         cfg_upd    = 1'b0;
    reg         fid_upd    = 1'b0;
    reg         st_clr     = 1'b0;

    wire [31:0] fid_lat, uf_cnt;
    wire        st_uf, st_vs;

    wire [31:0] ar_addr;  wire [7:0] ar_len;  wire [2:0] ar_size;
    wire [1:0]  ar_burst; wire       ar_valid;
    wire        ar_ready;
    wire [63:0] r_data;   wire [1:0] r_resp;  wire      r_last;
    wire        r_valid;  wire       r_ready;

    reg         in_vde;
    reg  [11:0] in_vx, in_vy;
    reg         in_vsync;

    wire        pix_de, pix_valid;
    wire [11:0] pix_x, pix_y;
    wire [23:0] pix_rgb;

    // =========================================================================
    // (1) 720p60 视频时序发生器（口径 = hdmi_timing_gen.v MODE0，寄存输出）
    // =========================================================================
    reg [15:0] hcnt, vcnt;
    reg        gen_run = 1'b0;

    always @(posedge pclk or negedge preseten) begin
        if (!preseten) begin
            hcnt <= 16'd0;
            vcnt <= V_START[15:0];
        end else if (!gen_run) begin
            // 预置在 vsync 行：启动后第一拍就有 vsync 上升沿（不必等 725 行）
            hcnt <= 16'd0;
            vcnt <= V_START[15:0];
        end else if (hcnt >= H_TOTAL - 1) begin
            hcnt <= 16'd0;
            vcnt <= (vcnt >= V_TOTAL - 1) ? 16'd0 : (vcnt + 16'd1);
        end else begin
            hcnt <= hcnt + 16'd1;
        end
    end

    reg de_r, vs_r, vs_d;
    reg [11:0] vx_r, vy_r;

    always @(posedge pclk or negedge preseten) begin
        if (!preseten) begin
            de_r <= 1'b0; vs_r <= 1'b0; vs_d <= 1'b0;
            vx_r <= 12'd0; vy_r <= 12'd0;
        end else begin
            de_r <= (hcnt < H_ACTIVE) && (vcnt < V_ACTIVE);
            vs_r <= gen_run && (vcnt >= V_START) && (vcnt < V_START + V_SYNC);
            vs_d <= vs_r;
            vx_r <= hcnt[11:0];
            vy_r <= vcnt[11:0];
        end
    end

    always @(*) begin
        in_vde   = de_r;
        in_vx    = vx_r;
        in_vy    = vy_r;
        in_vsync = vs_r & ~vs_d;     // 1 个 pix_clk 宽（= hdmi_out_top 的 vs_rise）
    end

    // =========================================================================
    // (2) AXI4 读从机行为模型
    //      AR：接受即入队（arready 常 1）
    //      R ：一次一个突发、突发内背靠背；首拍延迟 = R_LAT 个 aclk
    // =========================================================================
    localparam integer QD = 16;
    reg [31:0] q_addr [0:QD-1];
    reg [7:0]  q_len  [0:QD-1];
    integer    q_wr = 0, q_rd = 0;

    assign ar_ready = ((q_wr - q_rd) < QD);

    reg        b_valid = 1'b0;
    reg [31:0] b_addr  = 32'd0;
    integer    b_left  = 0;
    integer    b_delay = 0;

    // 内存内容：按字节地址可复算（便于反查数据通路搬字节是否正确）
    function [7:0] membyte(input [31:0] a);
        membyte = a[7:0] ^ a[15:8] ^ 8'h5A;
    endfunction

    function [63:0] beat_data(input [31:0] a);
        integer k;
        begin
            for (k = 0; k < 8; k = k + 1)
                beat_data[k*8 +: 8] = membyte(a + k);
        end
    endfunction

    assign r_valid = b_valid && (b_delay == 0);
    assign r_last  = (b_left == 1);
    assign r_resp  = 2'b00;
    assign r_data  = beat_data(b_addr);

    // ★ v60：AXI 读从机模型必须跟**主机的时钟**（scan_clk）跑 —— 扫描器的主口
    //   现在在扫描域（板上 FCLK1），它的 ARVALID/RREADY 握手只认 sclk。
    //   （踩过：留在 aclk 域时，1 个 sclk 宽的 ARVALID 会整个落在两个 aclk 沿之间
    //     ⇒ 突发丢失、outstanding 永远回不到 0、FSM 卡在 F_FIN。）
    always @(posedge sclk or negedge aresetn) begin
        if (!aresetn) begin
            q_wr <= 0; q_rd <= 0;
            b_valid <= 1'b0; b_addr <= 32'd0; b_left <= 0; b_delay <= 0;
        end else begin
            if (ar_valid && ar_ready) begin
                q_addr[q_wr % QD] <= ar_addr;
                q_len [q_wr % QD] <= ar_len;
                q_wr <= q_wr + 1;
            end
            if (!b_valid && (q_wr != q_rd)) begin
                b_valid <= 1'b1;
                b_addr  <= q_addr[q_rd % QD];
                b_left  <= q_len[q_rd % QD] + 1;
                // 流水式：只有"上一突发已流完、队列空"时才付首拍延迟；
                // 队列里还有描述符 ⇒ 数据流是背靠背的（真实 HP 口 + 多 outstanding）
                if ((R_LAT_MODE != 0) && (q_wr != q_rd)) b_delay <= 0;
                else                                     b_delay <= R_LAT;
                q_rd    <= q_rd + 1;
            end else if (b_valid) begin
                if (b_delay != 0) begin
                    b_delay <= b_delay - 1;
                end else if (r_ready) begin
                    b_addr <= b_addr + 32'd8;
                    b_left <= b_left - 1;
                    if (b_left == 1) b_valid <= 1'b0;
                end
            end
        end
    end

    // =========================================================================
    // DUT
    // =========================================================================
    hdmi_fb_scan #(
        .LINE_AW (11),
        .PIX_W   (12)
    ) dut (
        .s_axi_aclk      (aclk),
        .aresetn         (aresetn),
        .scan_clk        (sclk),
        .cfg_fb_addr_lo  (cfg_addr),
        .cfg_fb_addr_hi  (32'd0),
        .cfg_fb_stride   (cfg_stride),
        .cfg_fb_w        (cfg_w),
        .cfg_fb_h        (cfg_h),
        .cfg_frame_id    (cfg_fid),
        .fid_upd         (fid_upd),
        .cfg_upd         (cfg_upd),
        .cfg_enable      (cfg_en),
        .cfg_soft_reset  (cfg_srst),
        .frame_id_latched(fid_lat),
        .st_underflow    (st_uf),
        .st_vsync_seen   (st_vs),
        .underflow_cnt   (uf_cnt),
        .st_clr          (st_clr),
        .m_axi_araddr    (ar_addr),
        .m_axi_arlen     (ar_len),
        .m_axi_arsize    (ar_size),
        .m_axi_arburst   (ar_burst),
        .m_axi_arvalid   (ar_valid),
        .m_axi_arready   (ar_ready),
        .m_axi_rdata     (r_data),
        .m_axi_rresp     (r_resp),
        .m_axi_rlast     (r_last),
        .m_axi_rvalid    (r_valid),
        .m_axi_rready    (r_ready),
        .pix_clk         (pclk),
        .pix_aresetn     (preseten),
        .in_vde          (in_vde),
        .in_vx           (in_vx),
        .in_vy           (in_vy),
        .in_vsync        (in_vsync),
        .pix_de          (pix_de),
        .pix_x           (pix_x),
        .pix_y           (pix_y),
        .pix_rgb         (pix_rgb),
        .pix_valid       (pix_valid)
    );

    // =========================================================================
    // 内部探针（只读，纯日志用；不参与激励）
    // =========================================================================
    wire [11:0] p_pops   = dut.pops_done;      // 本行已弹（=已写 BRAM）像素数
    wire [11:0] p_beats  = dut.beats_done;     // 本行已收拍数
    wire [11:0] p_hdr    = dut.hdr_line;       // PIX 侧看到的"正在取的行号"
    wire [11:0] p_wl     = dut.hdr_line;       // v2：PIX 侧"已完成行号"（= hdr_line）
    wire [11:0] p_aline  = dut.a_line;         // AXI 侧受理的行号
    wire [11:0] p_rqline = dut.rq_line;        // PIX 侧请求的行号
    wire [2:0]  p_fst    = dut.fst;
    wire [11:0] p_lcur   = dut.line_cur;
    wire [31:0] p_lbase  = dut.line_base;
    wire [5:0]  p_room   = dut.room;
    wire [5:0]  p_out    = dut.outstanding;
    wire        p_ufnow  = dut.uf_now;

    // ★ v60：TB 侧的"有欠载的行数"独立计数（行 = pix_de 连续段；与 DUT 的
    //   uf_line_hit 同一语义，但完全从输出流 pix_de/pix_valid 观察）
    //   只在 dut.uf_en=1（第一次 vsync 之后）才开始计，与 DUT 的使能点对齐。
    always @(posedge pclk) begin
        if (preseten && dut.uf_en) begin
            if (!pix_de)                 row_uf_hit <= 1'b0;
            else if (!pix_valid && !row_uf_hit) begin
                row_uf_hit <= 1'b1;
                tb_uf_rows <= tb_uf_rows + 1;
            end
        end else begin
            row_uf_hit <= 1'b0;
            tb_uf_rows <= 0;
        end
    end
    wire        p_rqtog  = dut.rq_tog;
    wire        p_reqack = dut.req_ack;
    wire        p_pubtog = dut.pub_tog;
    wire        p_pend   = dut.pub_pend;
    wire        p_pen    = dut.p_en;
    wire        p_lstart = dut.line_start;
    wire [11:0] p_sbeats = dut.s_line_beats;

    // =========================================================================
    // (3a) 统计寄存器（PIX 域）
    // =========================================================================
    integer frame_idx      = 0;
    integer n_active_lines = 0;
    integer n_de_done      = 0;
    integer n_clean        = 0;
    integer n_dirty        = 0;
    integer n_pix_de       = 0;
    integer n_pix_valid    = 0;
    integer n_pix_bad      = 0;
    integer n_pix_short    = 0;
    integer mism_shown     = 0;
    integer line_pix [0:2047];
    integer line_inv [0:2047];
    integer line_fu  [0:2047];
    integer n_trace        = 0;
    integer sum_wl_vde     = 0;
    integer n_wl_vde       = 0;
    integer n_req_issued   = 0;
    integer n_req_accept   = 0;
    integer n_line_start   = 0;
    integer n_ar           = 0;
    integer n_beats        = 0;
    integer n_jump         = 0;
    integer n_disp_lines   = 0;   // 输出流的行首个数
    integer n_alias        = 0;   // V3：写口落在"正在显示"的 buffer 上的拍数（应为 0）
    integer n_line_order_bad = 0; // V3：帧内行号必须逐行 +1（719 后回 0）
    integer n_x_bad        = 0;   // V2：行内 pix_x 必须 0..W-1 逐拍 +1
    reg  [11:0] last_py    = 12'd0;
    reg     last_py_ok     = 1'b0;
    integer exp_x_i        = 0;   // 行内期望的 pix_x（阻塞赋值，避免与 NB 抢同一拍）
    integer prev_aline     = -1;
    integer prev_ok        = 0;
    reg     trace_win      = 1'b0;
    reg     vde_d          = 1'b0;
    reg     pix_de_d       = 1'b0;
    reg [11:0] pix_y_d     = 12'd0;
    reg     rqtog_d        = 1'b0;
    reg     reqack_d       = 1'b0;
    reg     pops_full_d    = 1'b0;
    time    t_lstart       = 0;
    time    fill_sum       = 0;
    integer fill_n         = 0;
    reg [2:0] fst_d        = 3'd0;
    integer ii;
    integer snap [0:47];        // 4 个快照 × 10 个指标
    integer F0;

    function [23:0] exp_pix(input [11:0] x, input [11:0] y);
        reg [31:0] a;
        begin
            a = FB_ADDR + y * cfg_stride + x * 3;
            exp_pix = {membyte(a), membyte(a + 1), membyte(a + 2)};
        end
    endfunction

    // ---- PIX 域：行/像素统计 + 时序追踪 --------------------------------
    always @(posedge pclk or negedge preseten) begin
        if (!preseten) begin
            frame_idx <= 0; n_active_lines <= 0; n_de_done <= 0;
            n_clean <= 0; n_dirty <= 0;
            n_pix_de <= 0; n_pix_valid <= 0; n_pix_bad <= 0; n_pix_short <= 0;
            n_trace <= 0; sum_wl_vde <= 0; n_wl_vde <= 0;
            n_req_issued <= 0;
            trace_win <= 1'b0;
            vde_d <= 1'b0; pix_de_d <= 1'b0; pix_y_d <= 12'd0;
            rqtog_d <= 1'b0;
        end else if (gen_run) begin

            // ---- 场边界
            if (in_vsync) begin
                frame_idx <= frame_idx + 1;
                n_trace   <= 0;
            end

            // ---- 行开始（显示域）：采样取数侧此刻的进度
            if (in_vde && !vde_d) begin
                n_active_lines <= n_active_lines + 1;
                line_pix[in_vy] <= 0;
                line_inv[in_vy] <= 0;
                line_fu [in_vy] <= -1;
                sum_wl_vde <= sum_wl_vde + p_wl;
                n_wl_vde   <= n_wl_vde + 1;
                n_trace    <= n_trace + 1;
                if (frame_idx == TRACE_FRAME && n_trace <= TRACE_N) begin
                    trace_win <= 1'b1;
                    $display("[TRC] t=%0d vde_begin vy=%0d done=%0d done_lat=%0d pops=%0d fst=%0d aline=%0d beats=%0d out=%0d room=%0d",
                             $time/1000, in_vy, p_wl, p_hdr, p_pops, p_fst, p_aline, p_beats, p_out, p_room);
                end
            end
            vde_d <= in_vde;

            // ---- 调试：逐 bank 读出值（v1 没有分 bank，编译基线时关掉）
`ifndef RTL_V1
            if (DBG != 0 && pix_de && (pix_y < 2) && (pix_x < 4))
                $display("[BUF] f=%0d y=%0d x=%0d got=%06x exp=%06x | e0=%06x o0=%06x e1=%06x o1=%06x rdbuf=%0d half=%0d wbuf=%0d pops=%0d aline=%0d",
                         frame_idx, pix_y, pix_x, pix_rgb, exp_pix(pix_x, pix_y),
                         dut.rd0_e, dut.rd0_o, dut.rd1_e, dut.rd1_o,
                         dut.rd_buf_q, dut.rd_half_q, dut.bram_wbuf, p_pops, p_aline);
`endif

            // ---- V3/V2：行号单调、行内 x 连续（ping-pong 错行/半新半旧的直接判据）
            if (pix_de && !pix_de_d) begin
                n_disp_lines <= n_disp_lines + 1;
                exp_x_i = 0;
                if (last_py_ok && (pix_y !== (((last_py + 1'b1) >= V_ACTIVE) ? 12'd0 : (last_py + 1'b1))))
                    n_line_order_bad <= n_line_order_bad + 1;
                last_py    <= pix_y;
                last_py_ok <= 1'b1;
            end

            // ---- 输出像素流统计（以 DUT 的 pix_de 为准，坐标 = 输出坐标）
            if (pix_de) begin
                if (pix_x !== exp_x_i[11:0]) n_x_bad <= n_x_bad + 1;
                exp_x_i = exp_x_i + 1;
                n_pix_de <= n_pix_de + 1;
                line_pix[pix_y] <= line_pix[pix_y] + 1;
                if (pix_valid) begin
                    n_pix_valid <= n_pix_valid + 1;
                    if (pix_rgb !== exp_pix(pix_x, pix_y)) begin
                        n_pix_bad <= n_pix_bad + 1;
                        // 每个相位都给出前 3 条（相位切换时把窗口重新打开）
                        if (mism_shown < 3) begin
                            mism_shown <= mism_shown + 1;
`ifdef RTL_V1
                            $display("[TRC] t=%0d DATA_MISMATCH f=%0d x=%0d y=%0d got=%06x exp=%06x done=%0d aline=%0d",
                                     $time/1000, frame_idx, pix_x, pix_y, pix_rgb, exp_pix(pix_x, pix_y),
                                     p_wl, p_aline);
`else
                            $display("[TRC] t=%0d DATA_MISMATCH f=%0d x=%0d y=%0d got=%06x exp=%06x done=%0d aline=%0d wbuf=%0d rdbuf=%0d",
                                     $time/1000, frame_idx, pix_x, pix_y, pix_rgb, exp_pix(pix_x, pix_y),
                                     p_wl, p_aline, dut.bram_wbuf, dut.rd_buf_q);
`endif
                        end
                        if (n_pix_bad < 3)
                            $display("[TRC] t=%0d DATA_MISMATCH x=%0d y=%0d got=%06x exp=%06x",
                                     $time/1000, pix_x, pix_y, pix_rgb, exp_pix(pix_x, pix_y));
                    end
                end else begin
                    line_inv[pix_y] <= line_inv[pix_y] + 1;
                    if (line_fu[pix_y] < 0) begin
                        line_fu[pix_y] <= pix_x;
                        if (frame_idx == TRACE_FRAME && n_trace <= TRACE_N + 1)
                            $display("[TRC] t=%0d FIRST_UF vy=%0d x=%0d done=%0d ufnow=%0d",
                                     $time/1000, pix_y, pix_x, p_wl, p_ufnow);
                    end
                end
            end

            // ---- 行结束（pix_de 下降沿 ⇒ 该行像素已全部输出）
            if (!pix_de && pix_de_d) begin
                n_de_done <= n_de_done + 1;
                if (line_inv[pix_y_d] == 0 && line_pix[pix_y_d] == cfg_w)
                    n_clean <= n_clean + 1;
                else
                    n_dirty <= n_dirty + 1;
                if (line_pix[pix_y_d] != cfg_w) n_pix_short <= n_pix_short + 1;
                if (frame_idx == TRACE_FRAME && n_trace <= TRACE_N + 1)
                    $display("[TRC] t=%0d pix_end vy=%0d npix=%0d inv=%0d first_uf=%0d done=%0d fst=%0d",
                             $time/1000, pix_y_d, line_pix[pix_y_d], line_inv[pix_y_d],
                             line_fu[pix_y_d], p_wl, p_fst);
            end
            pix_de_d <= pix_de;
            pix_y_d  <= pix_y;

            // ---- PIX 侧行请求（rq_tog 边沿 ⇒ 发了一次请求）
            if (p_rqtog != rqtog_d) begin
                n_req_issued <= n_req_issued + 1;
                if (trace_win && (frame_idx == TRACE_FRAME) && (n_trace <= TRACE_N + 1))
                    $display("[TRC] t=%0d REQ_ISSUE next_line=%0d tog=%0d ack=%0d free_before=%0d",
                             $time/1000, p_rqline, p_rqtog, p_reqack, (p_reqack == rqtog_d));
            end
            rqtog_d <= p_rqtog;
        end
    end

    // ---- 扫描域：请求受理 / 行起始 / 突发 / 收拍（v60 起统计在 sclk 上采）----
    always @(posedge sclk or negedge aresetn) begin
        if (!aresetn) begin
            n_req_accept <= 0; n_line_start <= 0; n_ar <= 0; n_beats <= 0;
            reqack_d <= 1'b0; prev_aline <= -1; prev_ok <= 0; n_jump <= 0;
            pops_full_d <= 1'b0; fst_d <= 3'd0;
        end else begin
            // ping-pong 不变量：正在写 buffer 的奇偶必须 != 正在显示行的奇偶
            // （写到正在显示的那个 buffer 上 = 半新半旧/错行；正常必须恒 0）
`ifdef RTL_V1
            // v1 是单缓冲，没有 bram_wbuf：写永远落在"正在显示"的那一块上，
            // 这一项对 v1 恒为"无意义"，故不统计（用 -d RTL_V1 编译基线时走这里）
`else
            if (dut.bram_we && in_vde && (dut.bram_wbuf == in_vy[0])) n_alias <= n_alias + 1;
`endif

            if (p_reqack != reqack_d) begin
                n_req_accept <= n_req_accept + 1;
                if (trace_win)
                    $display("[TRC] t=%0d AXI_ACCEPT line=%0d (ack_prev=%0d)",
                             $time/1000, p_rqline, reqack_d);
            end
            reqack_d <= p_reqack;

            if (p_lstart) begin
                n_line_start <= n_line_start + 1;
                t_lstart <= $time;
                // 帧边界 719→0 是新一场重新请求第 0 行，属正常，不算"跳行"
                if (prev_ok && (p_aline != 0) && (p_aline != prev_aline + 1))
                    n_jump <= n_jump + 1;
                prev_aline <= p_aline;
                prev_ok    <= 1;
                if (trace_win)
                    $display("[TRC] t=%0d LINE_START line=%0d base=%08x beats=%0d",
                             $time/1000, p_aline, p_lbase, p_sbeats);
            end

            if (ar_valid && ar_ready) begin
                n_ar <= n_ar + 1;
                if (trace_win && (p_beats == 0))
                    $display("[TRC] t=%0d FIRST_AR line=%0d addr=%08x len=%0d",
                             $time/1000, p_aline, ar_addr, ar_len);
            end
            if (r_valid && r_ready) n_beats <= n_beats + 1;

            // 本行写完（pops_done 第一次达到 W）那一拍 —— 填充耗时统计
            if ((p_pops != 0) && (p_pops == cfg_w[11:0])) begin
                if (pops_full_d != 1'b1) begin
                    pops_full_d <= 1'b1;
                    fill_sum <= fill_sum + ($time - t_lstart);
                    fill_n   <= fill_n + 1;
                    if (trace_win)
                        $display("[TRC] t=%0d POPS_FULL line=%0d pops=%0d out=%0d fill_ns=%0d",
                                 $time/1000, p_aline, p_pops, p_out, ($time - t_lstart)/1000);
                end
            end else begin
                pops_full_d <= 1'b0;
            end

            if (trace_win && (p_fst == 3'd0) && (fst_d == 3'd3))
                $display("[TRC] t=%0d FIN line=%0d", $time/1000, p_aline);
            fst_d <= p_fst;
        end
    end

    // ---------------------------------------------------------------- 快照任务
    task snapit;
        input integer ph;
        begin
            snap[ph*10+0] = n_clean;
            snap[ph*10+1] = n_dirty;
            snap[ph*10+2] = n_pix_bad;
            snap[ph*10+3] = n_alias;
            snap[ph*10+4] = n_pix_de;
            snap[ph*10+5] = n_pix_valid;
            snap[ph*10+6] = n_line_order_bad;
            snap[ph*10+7] = uf_cnt;
            mism_shown = 0;                 // 每个相位重新打开"前 3 条失配"窗口
        end
    endtask

    task show_phase;
        input integer ph;
        input [8*2-1:0] nm;
        integer lc, ld, lb, la, lde, lv, lo, lu;
        begin
            lc  = snap[ph*10+0] - snap[(ph-1)*10+0];
            ld  = snap[ph*10+1] - snap[(ph-1)*10+1];
            lb  = snap[ph*10+2] - snap[(ph-1)*10+2];
            la  = snap[ph*10+3] - snap[(ph-1)*10+3];
            lde = snap[ph*10+4] - snap[(ph-1)*10+4];
            lv  = snap[ph*10+5] - snap[(ph-1)*10+5];
            lo  = snap[ph*10+6] - snap[(ph-1)*10+6];
            lu  = snap[ph*10+7] - snap[(ph-1)*10+7];
            $display("[REARM] %0s lines=%0d clean=%0d lost=%0d lost_rate=%0.4f%% bad=%0d alias=%0d pix_de=%0d pix_valid=%0d cov=%0.6f lineorder_bad=%0d uf=%0d",
                     nm, lc+ld, lc, ld,
                     (lc+ld == 0) ? 0.0 : 100.0*ld/(lc+ld),
                     lb, la, lde, lv,
                     (lde == 0) ? 0.0 : (lv*1.0)/lde, lo, lu);
        end
    endtask

    // =========================================================================
    // 主流程
    // =========================================================================
    real    f_axi_mhz, f_pix_mhz, f_axi_min, aclk_per_line;
    integer n_lines_total;
    integer guard;
    reg     vplus;

    initial begin
        vplus = $value$plusargs("AXI_HALF_PS=%d", AXI_HALF_PS);
        vplus = $value$plusargs("SCAN_HALF_PS=%d", SCAN_HALF_PS);
        vplus = $value$plusargs("SCAN_PHASE_PS=%d", SCAN_PHASE_PS);
        vplus = $value$plusargs("PIX_HALF_PS=%d", PIX_HALF_PS);
        vplus = $value$plusargs("R_LAT=%d",       R_LAT);
        vplus = $value$plusargs("R_LAT_MODE=%d",  R_LAT_MODE);
        vplus = $value$plusargs("NLINES=%d",      NLINES);
        vplus = $value$plusargs("CFG_W=%d",       CFG_W);
        vplus = $value$plusargs("CFG_H=%d",       CFG_H);
        vplus = $value$plusargs("TRACE_N=%d",     TRACE_N);
        vplus = $value$plusargs("TRACE_FRAME=%d", TRACE_FRAME);
        vplus = $value$plusargs("TAG=%s",         TAG);
        vplus = $value$plusargs("REARM=%d",       REARM);
        vplus = $value$plusargs("REARM_OFF=%d",   REARM_OFF);
        vplus = $value$plusargs("REARM_FRAMES=%d",REARM_FRAMES);
        vplus = $value$plusargs("REARM_MID=%d",   REARM_MID);
        vplus = $value$plusargs("REARM_MID_X=%d", REARM_MID_X);
        vplus = $value$plusargs("REARM_SR=%d",    REARM_SR);
        vplus = $value$plusargs("CDCWAVE=%d",     CDCWAVE);
        vplus = $value$plusargs("DBG=%d",         DBG);
        vplus = $value$plusargs("H_TOTAL=%d",  H_TOTAL);
        vplus = $value$plusargs("H_ACTIVE=%d", H_ACTIVE);
        vplus = $value$plusargs("V_TOTAL=%d",  V_TOTAL);
        vplus = $value$plusargs("V_ACTIVE=%d", V_ACTIVE);
        vplus = $value$plusargs("V_FP=%d",     V_FP);
        vplus = $value$plusargs("V_SYNC=%d",   V_SYNC);
        V_START = V_ACTIVE + V_FP;

        cfg_stride = CFG_W * 3;
        cfg_w      = CFG_W[15:0];
        cfg_h      = CFG_H[15:0];

        for (ii = 0; ii < 2048; ii = ii + 1) begin
            line_pix[ii] = 0; line_inv[ii] = 0; line_fu[ii] = -1;
        end

        f_axi_mhz     = 1000000.0 / (2.0 * AXI_HALF_PS);
        f_pix_mhz     = 1000000.0 / (2.0 * PIX_HALF_PS);
        aclk_per_line = (H_TOTAL * 2.0 * PIX_HALF_PS) / (2.0 * AXI_HALF_PS);
        // 吞吐硬下限：每行 W 个像素必须在 1 个行周期(H_TOTAL/f_pix)内写完
        f_axi_min     = f_pix_mhz * CFG_W / H_TOTAL;

        $display("[SUM] ================ TAG=%0s ================", TAG);
        $display("[SUM] CFG  W=%0d H=%0d stride=%0d fb=0x%08x", CFG_W, CFG_H, CFG_W*3, FB_ADDR);
        $display("[SUM] CLK  f_axi=%0.6f MHz f_pix=%0.6f MHz ratio=%0.4f R_LAT=%0d",
                 f_axi_mhz, f_pix_mhz, f_axi_mhz / f_pix_mhz, R_LAT);
        $display("[SUM] AXI 从机模型：主机带宽无限、突发内背靠背、R_LAT_MODE=%0d（1=流水式首拍延迟）", R_LAT_MODE);
        $display("[SUM] BUDGET aclk_per_line=%0.2f  pops_per_line=%0d  fill_need=%0.3f aclk/pop",
                 aclk_per_line, CFG_W, aclk_per_line / CFG_W);
        $display("[SUM] THEORY f_axi_min = f_pix*W/H_TOTAL = %0.6f MHz", f_axi_min);

        #200000;                       // 200 ns
        aresetn  = 1'b1;
        preseten = 1'b1;

        // ---- 写配置（cfg_upd 单拍脉冲；模块内部要累加 H 次算 FB_SIZE）
        @(negedge aclk);
        cfg_upd = 1'b1;
        @(negedge aclk);
        cfg_upd = 1'b0;
        repeat (CFG_H + 40) @(posedge aclk);
        $display("[SUM] after cfg: s_w=%0d s_h=%0d s_stride=%0d s_fsize=%0d s_beats=%0d cfg_tog=%0d",
                 dut.s_w, dut.s_h, dut.s_stride, dut.s_fsize, dut.s_line_beats, dut.cfg_tog);

        // ---- 写 FRAME_ID=0（走一遍 fid toggle 通路）
        cfg_fid = 32'd0;
        @(negedge aclk);
        fid_upd = 1'b1;
        @(negedge aclk);
        fid_upd = 1'b0;
        repeat (20) @(posedge aclk);

        // ---- 放行视频
        //   ★ v60 修：用 **p_en**（跨域之后的真实生效点）当放行门，而不是数固定
        //     的 aclk 拍。为什么：v60 起配置提交跑在**扫描域**（独立自由时钟），
        //     数 aclk 拍在"扫描域更慢"的档位会提前放行 ⇒ 首场那个 vsync 到来时
        //     p_en 还是 0 ⇒ 行 0 从未被请求 ⇒ 缓冲空 ⇒ 首帧 1 行 xxx（错字节 1280）。
        //     （踩过；v58 单域时两者同频所以看不出来。）
        //   注：时序发生器的相位在 !gen_run 时被钉在 vsync 行，放行即产生一个
        //   in_vsync 脉冲 ⇒ 上电后第一个 vsync 一定会请求第 0 行。
        guard = 0;
        while ((!dut.p_en) && (guard < 400000)) begin @(posedge pclk); guard = guard + 1; end
        if (dut.p_en !== 1'b1) $fatal(1, "[TB] cfg 一直没到像素域（p_en=0，跨域通路坏了）");
        $display("[SUM] cfg 已跨域生效（p_en=1，等了 %0d pix 拍）@ t=%0d ns", guard, $time/1000);
        @(negedge pclk);
        gen_run = 1'b1;

        // ---- 重新武装用例（v2 新增）：enable 掉 0 → 64 拍 → 回 1
        if (REARM != 0) begin
            while (frame_idx < 2) @(posedge pclk);
            F0 = frame_idx;
            snapit(0);
            while (frame_idx < F0 + REARM_FRAMES) @(posedge pclk);
            snapit(1);
            $display("[REARM] ---- phase A done (frame_idx=%0d), 关 enable @ t=%0d ns", frame_idx, $time/1000);
            if (REARM_MID != 0) begin
                // 等一条有效行的中间（不是行边界）再关：验证"行内关闭"也能恢复
                while (!(in_vde && (in_vx == REARM_MID_X[11:0]))) @(posedge pclk);
                $display("[REARM] MID: 行内关闭点 vy=%0d in_vx=%0d t=%0d ns", in_vy, in_vx, $time/1000);
            end
            // 模拟 PS 写 CTRL：cfg_enable 变 0 + cfg_upd 单拍脉冲（与 hdmi_out_top 一致）
            @(negedge aclk); cfg_en = 1'b0; cfg_upd = 1'b1;
            @(negedge aclk); cfg_upd = 1'b0;
            repeat (REARM_OFF) @(negedge aclk);
            $display("[REARM] ---- 保持 %0d 拍后端重新置 1 @ t=%0d ns", REARM_OFF, $time/1000);
            @(negedge aclk); cfg_en = 1'b1; cfg_upd = 1'b1;
            @(negedge aclk); cfg_upd = 1'b0;
            settle = 1;
            if (REARM_SR != 0) begin
                // ---- soft_reset 与 enable 同时变的用例：提交跑到一半时来一个 soft_reset
                repeat (200) @(negedge aclk);          // 让第 2 次提交进行到一半（H 个 aclk 之内）
                cfg_srst = 1'b1; @(negedge aclk); cfg_srst = 1'b0;
                while (frame_idx < F0 + REARM_FRAMES + 1) @(posedge pclk);
`ifdef RTL_V1
                $display("[REARM] SR-cancel: 取消后 s_en=%0d p_en=%0d", dut.s_en, dut.p_en);
`else
                $display("[REARM] SR-cancel: 取消后 s_en=%0d p_en=%0d cfg_req=%0d  (预期 s_en=0：文档语义=soft_reset 取消挂起提交)",
                         dut.s_en, dut.p_en, dut.cfg_req);
`endif
                // PS 侧规矩：soft_reset 之后必须重写一次 CTRL
                @(negedge aclk); cfg_en = 1'b1; cfg_upd = 1'b1;
                @(negedge aclk); cfg_upd = 1'b0;
                settle = 2;
            end
            // 留够帧让 cfg 提交完成（提交本身要 H 个 aclk），再测 3 帧
            while (frame_idx < F0 + REARM_FRAMES + settle) @(posedge pclk);
            snapit(2);
            // 死态取证：AXI 侧是否把 enable 提交下去了（s_en/p_en=0 且 uf=0 ⇒ p_en 没回来）
            $display("[REARM] state@B: s_en=%0d l_en=%0d p_en=%0d fs_busy=%0d cfg_pend=%0d cfg_tog=%0d cfg_upd=%0d",
                     dut.s_en, dut.l_en, dut.p_en, dut.fs_busy, dut.cfg_pend, dut.cfg_tog, cfg_upd);
            while (frame_idx < F0 + REARM_FRAMES + settle + REARM_FRAMES) @(posedge pclk);
            snapit(3);
            show_phase(1, "A");
            show_phase(3, "B");
            if ((snap[10+1]-snap[1]) == 0 && (snap[30+1]-snap[21]) == 0 &&
                (snap[30+2]-snap[22]) == 0 && (snap[30+4]-snap[24]) != 0 &&
                (snap[30+5]-snap[25]) == (snap[30+4]-snap[24]))
                $display("[REARM] VERDICT REARM_PASS  (两段完全一致：0 丢行 / 0 错字节 / 100%% pix_valid)");
            else
                $display("[REARM] VERDICT REARM_FAIL  (A/B 不一致 —— 重新武装后扫描器没有恢复到原状态)");
        end

        // ---- ★ v60 S3：跨域写入必达 + 计数不丢
        if (CDCWAVE != 0) begin
            $display("[CDCWAVE] ---- 开始：8 次快速 CTRL + 8 次 FRAME_ID 写（间隔跨过 fs_busy）");
            for (cdc_k = 0; cdc_k < 8; cdc_k = cdc_k + 1) begin
                @(negedge aclk);
                cfg_en  = cdc_k[0] ? 1'b0 : 1'b1;   // 交替 1/0
                cfg_upd = 1'b1;
                @(negedge aclk);
                cfg_upd = 1'b0;
                cdc_gap = 3 + (cdc_k * 7) % 40;   // 3..42 拍：既踩 fs_busy 也踩空闲
                repeat (cdc_gap) @(negedge aclk);
                cfg_fid = 32'hA500_0000 + cdc_k;
                fid_upd = 1'b1;
                @(negedge aclk);
                fid_upd = 1'b0;
                repeat (2 + (cdc_k * 5) % 13) @(negedge aclk);
            end
            // 终值：enable=1、FRAME_ID=最后值
            @(negedge aclk); cfg_en = 1'b1; cfg_upd = 1'b1;
            @(negedge aclk); cfg_upd = 1'b0;
            @(negedge aclk); cfg_fid = 32'hA500_00FF; fid_upd = 1'b1;
            @(negedge aclk); fid_upd = 1'b0;
            FID_LAST = 32'hA500_00FF;
            repeat (CFG_H + 60) @(posedge aclk);      // 等提交 + 跨域握手完成

            // 再跑 3 帧测覆盖率
            // 等 2 场：FRAME_ID 只在 vsync 边界采纳，采纳后还要跨回 aclk
            cdc_F1 = frame_idx;
            while (frame_idx < cdc_F1 + 2) @(posedge pclk);
            snapit(1);                       // 相位 1 = 基线
            while (frame_idx < cdc_F1 + 5) @(posedge pclk);
            snapit(2);                       // 相位 2 = 后 3 帧
            $display("[CDCWAVE] cfg 终值检查：s_en=%0d p_en=%0d (期望 1/1)  fid_latched=%08h (期望 %08h)  cfg_req=%0d",
                     dut.s_en, dut.p_en, dut.frame_id_latched, FID_LAST[31:0], dut.cfg_req);
            $display("[CDCWAVE] 后 3 帧：pix_de=%0d pix_valid=%0d cov=%0.6f",
                     snap[24] - snap[14], snap[25] - snap[15],
                     (snap[25] - snap[15]) * 1.0 / ((snap[24] - snap[14]) ? (snap[24] - snap[14]) : 1));
            $display("[CDCWAVE] underflow: DUT=%0d  TB独立计数=%0d (期望相等)",
                     dut.underflow_cnt, tb_uf_rows);
            if ((dut.s_en === 1'b1) && (dut.p_en === 1'b1) &&
                (dut.frame_id_latched === FID_LAST[31:0]) &&
                ((snap[24] - snap[14]) > 0) &&
                ((snap[24] - snap[14]) == (snap[25] - snap[15])) &&
                (dut.underflow_cnt == tb_uf_rows))
                $display("[CDCWAVE] VERDICT CDC_PASS");
            else
                $display("[CDCWAVE] VERDICT CDC_FAIL");
        end

        // ---- 跑够 NLINES 条有效行
        guard = 0;
        while ((n_active_lines < NLINES) && (guard < NLINES * H_TOTAL * 4)) begin
            @(posedge pclk);
            guard = guard + 1;
        end
        if (n_active_lines < NLINES)
            $display("[SUM] *** TIMEOUT *** n_active_lines=%0d", n_active_lines);

        repeat (4) @(posedge pclk);

        // ---- 汇总
        n_lines_total = n_clean + n_dirty;
        $display("[SUM] ---------------- 统计 ----------------");
        $display("[SUM] frames_seen          = %0d", frame_idx);
        $display("[SUM] lines_expected       = %0d  (active=%0d finished=%0d)",
                 NLINES, n_active_lines, n_de_done);
        $display("[SUM] req_issued(pix)      = %0d", n_req_issued);
        $display("[SUM] req_accepted(axi)    = %0d", n_req_accept);
        $display("[SUM] line_start(axi)      = %0d", n_line_start);
        $display("[SUM] line_skipped(jump>1) = %0d", n_jump);
        $display("[SUM] ar_bursts=%0d  r_beats=%0d", n_ar, n_beats);
        $display("[SUM] pixels_de            = %0d  (expect %0d)",
                 n_pix_de, n_lines_total * CFG_W);
        $display("[SUM] pixels_valid         = %0d  (%0.4f%%)", n_pix_valid,
                 100.0 * n_pix_valid / ((n_pix_de == 0) ? 1 : n_pix_de));
        $display("[SUM] pixels_data_mismatch = %0d   (V2: 显示缓冲逐像素 == 测试图案)", n_pix_bad);
        $display("[SUM] line_order_bad       = %0d   (V3: 帧内行号非 +1 的次数)", n_line_order_bad);
        $display("[SUM] x_sequence_bad       = %0d   (V2: 行内 pix_x 非 0,1,2.. 的次数)", n_x_bad);
        $display("[SUM] alias_write_cycles   = %0d   (V3: 写到正在显示的 buffer 的拍数)", n_alias);
        $display("[SUM] disp_lines           = %0d", n_disp_lines);
        $display("[SUM] lines_short          = %0d", n_pix_short);
        $display("[SUM] lines_valid(clean)   = %0d", n_clean);
        $display("[SUM] lines_lost(dirty)    = %0d", n_dirty);
        if (n_lines_total != 0)
            $display("[SUM] LOST_RATE            = %0.4f%%  (%0d / %0d)",
                     100.0 * n_dirty / n_lines_total, n_dirty, n_lines_total);
        else
            $display("[SUM] LOST_RATE            = n/a");
        $display("[SUM] underflow_cnt(0x2C)  = %0d  (per frame=%0.2f per line=%0.4f)",
                 uf_cnt, uf_cnt / 1.0 / ((frame_idx == 0) ? 1 : frame_idx),
                 uf_cnt / 1.0 / ((n_lines_total == 0) ? 1 : n_lines_total));
        if (n_wl_vde != 0)
            $display("[SUM] avg_done_line_at_vde = %0.1f (line#, W=%0d, %0.2f%%)",
                     sum_wl_vde / 1.0 / n_wl_vde, CFG_W,
                     100.0 * (sum_wl_vde / 1.0 / n_wl_vde) / CFG_W);
        if (fill_n != 0)
            $display("[SUM] line_fill_time       = %0d ns (avg over %0d lines)  line_period = %0d ns  ratio = %0.3f",
                     fill_sum / fill_n / 1000, fill_n,
                     (H_TOTAL * 2 * PIX_HALF_PS) / 1000,
                     (fill_sum / 1.0 / fill_n) / (H_TOTAL * 2.0 * PIX_HALF_PS));
        $display("[SUM] STAT st_underflow=%0d st_vsync_seen=%0d fid_latched=%0d",
                 st_uf, st_vs, fid_lat);
        $display("[SUM] VERDICT V1_lost_rate=%0.4f V2_databad=%0d V3_lineorder_bad=%0d V3_alias=%0d V6_uf_cnt=%0d",
                 100.0 * n_dirty / ((n_lines_total == 0) ? 1 : n_lines_total), n_pix_bad, n_line_order_bad, n_alias, uf_cnt);
        $display("[SUM] VERDICT lost_rate=%0.4f underflow_per_line=%0.4f",
                 100.0 * n_dirty / ((n_lines_total == 0) ? 1 : n_lines_total),
                 uf_cnt / 1.0 / ((n_lines_total == 0) ? 1 : n_lines_total));
        $display("[SUM] ================= END %0s =================", TAG);
        $finish(0);
    end

endmodule
