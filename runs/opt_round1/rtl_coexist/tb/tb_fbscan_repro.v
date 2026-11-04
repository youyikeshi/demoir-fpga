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

module tb_fbscan_repro;

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

    // ------------------------------------------------------------- 视频时序
    localparam integer H_TOTAL  = 1650;
    localparam integer H_ACTIVE = 1280;
    localparam integer V_TOTAL  = 750;
    localparam integer V_ACTIVE = 720;
    localparam integer V_FP     = 5;
    localparam integer V_SYNC   = 5;
    localparam integer V_START  = V_ACTIVE + V_FP;   // 725：vsync 起始行
    localparam [31:0]  FB_ADDR  = 32'h1000_0000;

    // ------------------------------------------------------------- 时钟/复位
    reg aclk = 1'b0;
    reg pclk = 1'b0;
    always #(AXI_HALF_PS) aclk = ~aclk;
    always #(PIX_HALF_PS) pclk = ~pclk;

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

    always @(posedge aclk or negedge aresetn) begin
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
    wire [11:0] p_wl     = dut.wl_bin;         // PIX 侧看到的行内写进度
    wire [11:0] p_aline  = dut.a_line;         // AXI 侧受理的行号
    wire [11:0] p_rqline = dut.rq_line;        // PIX 侧请求的行号
    wire [2:0]  p_fst    = dut.fst;
    wire [11:0] p_lcur   = dut.line_cur;
    wire [31:0] p_lbase  = dut.line_base;
    wire [5:0]  p_room   = dut.room;
    wire [5:0]  p_out    = dut.outstanding;
    wire        p_ufnow  = dut.uf_now;
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
    integer line_pix [0:1023];
    integer line_inv [0:1023];
    integer line_fu  [0:1023];
    integer n_trace        = 0;
    integer sum_wl_vde     = 0;
    integer n_wl_vde       = 0;
    integer n_req_issued   = 0;
    integer n_req_accept   = 0;
    integer n_line_start   = 0;
    integer n_ar           = 0;
    integer n_beats        = 0;
    integer n_jump         = 0;
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
                    $display("[TRC] t=%0d vde_begin vy=%0d hdr=%0d wl=%0d pops=%0d fst=%0d aline=%0d beats=%0d out=%0d room=%0d",
                             $time/1000, in_vy, p_hdr, p_wl, p_pops, p_fst, p_aline, p_beats, p_out, p_room);
                end
            end
            vde_d <= in_vde;

            // ---- 输出像素流统计（以 DUT 的 pix_de 为准，坐标 = 输出坐标）
            if (pix_de) begin
                n_pix_de <= n_pix_de + 1;
                line_pix[pix_y] <= line_pix[pix_y] + 1;
                if (pix_valid) begin
                    n_pix_valid <= n_pix_valid + 1;
                    if (pix_rgb !== exp_pix(pix_x, pix_y)) begin
                        n_pix_bad <= n_pix_bad + 1;
                        if (n_pix_bad < 3)
                            $display("[TRC] t=%0d DATA_MISMATCH x=%0d y=%0d got=%06x exp=%06x",
                                     $time/1000, pix_x, pix_y, pix_rgb, exp_pix(pix_x, pix_y));
                    end
                end else begin
                    line_inv[pix_y] <= line_inv[pix_y] + 1;
                    if (line_fu[pix_y] < 0) begin
                        line_fu[pix_y] <= pix_x;
                        if (frame_idx == TRACE_FRAME && n_trace <= TRACE_N + 1)
                            $display("[TRC] t=%0d FIRST_UF vy=%0d x=%0d hdr=%0d wl=%0d ufnow=%0d",
                                     $time/1000, pix_y, pix_x, p_hdr, p_wl, p_ufnow);
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
                    $display("[TRC] t=%0d pix_end vy=%0d npix=%0d inv=%0d first_uf=%0d hdr=%0d wl=%0d fst=%0d",
                             $time/1000, pix_y_d, line_pix[pix_y_d], line_inv[pix_y_d],
                             line_fu[pix_y_d], p_hdr, p_wl, p_fst);
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

    // ---- AXI 域：请求受理 / 行起始 / 突发 / 收拍 ------------------------
    always @(posedge aclk or negedge aresetn) begin
        if (!aresetn) begin
            n_req_accept <= 0; n_line_start <= 0; n_ar <= 0; n_beats <= 0;
            reqack_d <= 1'b0; prev_aline <= -1; prev_ok <= 0; n_jump <= 0;
            pops_full_d <= 1'b0; fst_d <= 3'd0;
        end else begin
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

    // =========================================================================
    // 主流程
    // =========================================================================
    real    f_axi_mhz, f_pix_mhz, f_axi_min, aclk_per_line;
    integer n_lines_total;
    integer guard;
    reg     vplus;

    initial begin
        vplus = $value$plusargs("AXI_HALF_PS=%d", AXI_HALF_PS);
        vplus = $value$plusargs("PIX_HALF_PS=%d", PIX_HALF_PS);
        vplus = $value$plusargs("R_LAT=%d",       R_LAT);
        vplus = $value$plusargs("R_LAT_MODE=%d",  R_LAT_MODE);
        vplus = $value$plusargs("NLINES=%d",      NLINES);
        vplus = $value$plusargs("CFG_W=%d",       CFG_W);
        vplus = $value$plusargs("CFG_H=%d",       CFG_H);
        vplus = $value$plusargs("TRACE_N=%d",     TRACE_N);
        vplus = $value$plusargs("TRACE_FRAME=%d", TRACE_FRAME);
        vplus = $value$plusargs("TAG=%s",         TAG);

        cfg_stride = CFG_W * 3;
        cfg_w      = CFG_W[15:0];
        cfg_h      = CFG_H[15:0];

        for (ii = 0; ii < 1024; ii = ii + 1) begin
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
        @(negedge pclk);
        gen_run = 1'b1;

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
        $display("[SUM] pixels_data_mismatch = %0d", n_pix_bad);
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
            $display("[SUM] avg_wl_at_vde_begin  = %0.1f / %0d (%0.2f%%)",
                     sum_wl_vde / 1.0 / n_wl_vde, CFG_W,
                     100.0 * (sum_wl_vde / 1.0 / n_wl_vde) / CFG_W);
        if (fill_n != 0)
            $display("[SUM] line_fill_time       = %0d ns (avg over %0d lines)  line_period = %0d ns  ratio = %0.3f",
                     fill_sum / fill_n / 1000, fill_n,
                     (H_TOTAL * 2 * PIX_HALF_PS) / 1000,
                     (fill_sum / 1.0 / fill_n) / (H_TOTAL * 2.0 * PIX_HALF_PS));
        $display("[SUM] STAT st_underflow=%0d st_vsync_seen=%0d fid_latched=%0d",
                 st_uf, st_vs, fid_lat);
        $display("[SUM] VERDICT lost_rate=%0.4f underflow_per_line=%0.4f",
                 100.0 * n_dirty / ((n_lines_total == 0) ? 1 : n_lines_total),
                 uf_cnt / 1.0 / ((n_lines_total == 0) ? 1 : n_lines_total));
        $display("[SUM] ================= END %0s =================", TAG);
        $finish(0);
    end

endmodule
