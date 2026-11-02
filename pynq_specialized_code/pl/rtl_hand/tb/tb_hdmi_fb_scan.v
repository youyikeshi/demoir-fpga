// ============================================================================
//  tb_hdmi_fb_scan.v —— hdmi_fb_scan 的 xsim 验收
//
//  判据（docs/camera/05_HDMI输出_PS_PL接口契约.md §4 / 任务书 ③）
//  ------------------------------------------------------------------
//   ① 输出像素与 compose() 布局**逐字节一致**（含 stride ≠ W*3 的情形）
//   ② de 之外不发起读（= 只读有效区 [0, W*3) 字节，y < H）
//   ③ arlen ≤ 15、地址单调递增（且 arsize=8B / INCR / 不跨 4KB）
//   ④ 两帧不同图案 ⇒ 切换点落在 vsync
//   ⑤ 行缓冲不足 ⇒ STATUS[2] 置位
//
//  本版（阶段一）实现并报告 ①②③ 的实测数字；④⑤ 的激励骨架已在位
//  （frame_id / underflow 用例），确认后补齐。
//
//  向量
//  ----
//  由 tools/gen_hdmi_scan_golden.py 生成（用 display_layout.compose() 作黄金）：
//      case<N>_par.txt   "W H STRIDE FB_ADDR"
//      case<N>_mem.hex   DDR 镜像（64-bit 小端字）
//      case<N>_gold.hex  期望像素流（6 hex/像素 = {R,G,B}）
//
//  运行（tools/run_hdmi_scan_sim.sh）：
//      xvlog -sv tb/tb_hdmi_fb_scan.v modules/hdmi_fb_scan.v
//      xelab -debug off tb_hdmi_fb_scan
//      xsim tb_hdmi_fb_scan -R +VECDIR=<abs 向量目录> +CASES=1,2,3
// ============================================================================
`timescale 1ns / 1ps

module tb_hdmi_fb_scan;

    // ---------------------------------------------------------------- 时钟
    localparam integer AXI_HALF = 5;    // 10 ns ⇒ 100 MHz（契约的 s_axi_aclk）
    localparam integer PIX_HALF = 7;    // 14 ns ⇒ 71.4 MHz（≈74.25；判据与频率无关）

    reg aclk = 1'b0;
    reg pclk = 1'b0;
    always #(AXI_HALF) aclk = ~aclk;
    always #(PIX_HALF) pclk = ~pclk;

    // ------------------------------------------------------------ 内存模型
    localparam integer MEM_WORDS = 1 << 16;          // 512 KB @8B
    localparam [31:0]  MEM_BASE  = 32'h1000_0000;

    reg [63:0] mem [0:MEM_WORDS-1];

    // ------------------------------------------------------------- DUT 端口
    reg         aresetn  = 1'b0;
    reg         preseten = 1'b0;

    reg  [31:0] cfg_addr   = 32'd0;
    reg  [31:0] cfg_stride = 32'd0;
    reg  [15:0] cfg_w      = 16'd0;
    reg  [15:0] cfg_h      = 16'd0;
    reg  [31:0] cfg_fid    = 32'd0;
    reg         cfg_en     = 1'b0;
    reg         cfg_srst   = 1'b0;
    reg         cfg_upd    = 1'b0;   // 写配置寄存器的 1 拍脉冲
    reg         fid_upd    = 1'b0;   // 写 FRAME_ID 的 1 拍脉冲

    wire [31:0] fid_lat;
    wire        st_uf, st_vs;

    wire [31:0] ar_addr;
    wire [7:0]  ar_len;
    wire [2:0]  ar_size;
    wire [1:0]  ar_burst;
    wire        ar_valid;
    wire        ar_ready;
    wire [63:0] r_data;
    wire [1:0]  r_resp;
    wire        r_last;
    wire        r_valid;
    wire        r_ready;

    wire        in_vde;
    wire [11:0] in_vx, in_vy;
    wire        in_vsync;
    wire        pix_de, pix_valid;
    wire [11:0] pix_x, pix_y;
    wire [23:0] pix_rgb;

    // ------------------------------------------------------------ 时序发生器
    //  一帧 = [VB 行垂直前肩][H 行有效][VF 行垂直后肩]，每行 = W + HB 拍
    //  vsync 脉冲在 (hcnt==0, vcnt==0)，即整帧第一个拍 —— 处于垂直前肩内，
    //  离第一个有效像素还有 VB*(W+HB) 拍，足够扫第 0 行。
    localparam integer HB = 64;
    localparam integer VB = 10;
    localparam integer VF = 2;

    integer hcnt = 0, vcnt = 0;
    reg     gen_run = 1'b0;
    integer htot, vtot;

    always @(*) begin
        htot = cfg_w + HB;
        vtot = VB + cfg_h + VF;
    end

    always @(posedge pclk or negedge preseten) begin
        if (!preseten) begin
            hcnt <= 0;
            vcnt <= 0;
        end else if (!gen_run) begin
            hcnt <= 0;
            vcnt <= 0;
        end else if (hcnt >= htot - 1) begin
            hcnt <= 0;
            vcnt <= (vcnt >= vtot - 1) ? 0 : vcnt + 1;
        end else begin
            hcnt <= hcnt + 1;
        end
    end

    assign in_vde   = (hcnt < cfg_w) && (vcnt >= VB) && (vcnt < VB + cfg_h);
    assign in_vx    = hcnt[11:0];
    assign in_vy    = (vcnt >= VB) ? vcnt[11:0] - VB[11:0] : 12'd0;
    assign in_vsync = gen_run && (hcnt == 0) && (vcnt == 0);

    // ------------------------------------------------------------- AXI 从机
    //  AR：入 8 深描述符队列（DUT 最多 2 个在途突发，够用）
    //  R ：逐拍返回，rvalid 组合；只有握手完成才推进地址
    localparam integer QD = 8;
    reg [31:0] q_addr [0:QD-1];
    reg [7:0]  q_len  [0:QD-1];
    integer    q_wr = 0, q_rd = 0;

    assign ar_ready = ((q_wr - q_rd) < QD);

    reg        b_valid = 1'b0;
    reg [31:0] b_addr  = 32'd0;
    integer    b_left  = 0;
    integer    b_delay = 0;
    localparam integer R_LAT = 8;      // 首个数据拍前的延迟（拍）

    assign r_valid = b_valid && (b_delay == 0);
    assign r_data  = mem[(b_addr - MEM_BASE) >> 3];
    assign r_last  = (b_left == 1);
    assign r_resp  = 2'b00;

    // ---- 统计 / 判据计数 -------------------------------------------------
    integer n_burst      = 0;
    integer n_arlen_bad  = 0;
    integer n_arsize_bad = 0;
    integer n_arburst_bad= 0;
    integer n_align_bad  = 0;
    integer n_4k_bad     = 0;
    integer n_outside    = 0;   // 读地址落在有效区之外（判据②）
    integer arlen_max    = 0;
    integer n_rbeat      = 0;
    integer n_line_rd    = 0;   // 出现过的不同行数
    integer line_expect = 0;
    integer line_bytes_min = 1 << 30;
    integer line_bytes_max = -1;
    integer line_bytes_sum = 0;
    reg [31:0] last_addr = 32'd0;
    integer    n_addr_back = 0;
    reg        first_ar = 1'b1;

    // 行内字节表（判据②：每行读了哪些字节）
    integer bytes_per_line [0:4095];
    integer mark_lo [0:4095];

    // 当前读地址属于哪一行（Verilog-2001 经典写法：函数体先声明返回类型）
    function integer cur_line_of_addr;
        input [31:0] a;
        begin
            if (cfg_stride == 0) cur_line_of_addr = 0;
            else                 cur_line_of_addr = (a - cfg_addr) / cfg_stride;
        end
    endfunction

    // ---- 描述符入队 + 检查 -----------------------------------------------
    integer di, line_i, off_i, alen_i;
    always @(posedge aclk or negedge aresetn) begin
        if (!aresetn) begin
            q_wr <= 0;
        end else if (ar_valid && ar_ready) begin
            q_addr[q_wr % QD] <= ar_addr;
            q_len [q_wr % QD] <= ar_len;
            q_wr <= q_wr + 1;

            alen_i = ar_len;
            n_burst = n_burst + 1;
            if (alen_i > 15) n_arlen_bad = n_arlen_bad + 1;
            if (alen_i > arlen_max) arlen_max = alen_i;
            if (ar_size !== 3'b011) n_arsize_bad = n_arsize_bad + 1;
            if (ar_burst !== 2'b01) n_arburst_bad = n_arburst_bad + 1;
            if (ar_addr[2:0] != 3'b000) n_align_bad = n_align_bad + 1;
            if (({1'b0, ar_addr[11:0]} + ((alen_i + 1) * 8)) > 4096) n_4k_bad = n_4k_bad + 1;

            // 判据②：每行只允许读 [0, W*3) 且 y < H
            line_i = cur_line_of_addr(ar_addr);
            off_i  = (ar_addr - cfg_addr) % cfg_stride;
            if ((off_i >= cfg_stride) || (line_i >= cfg_h) || (off_i < 0)) begin
                n_outside = n_outside + 1;
                $display("[TB][ERR] 读越界 addr=0x%08x line=%0d off=%0d", ar_addr, line_i, off_i);
            end else begin
                for (di = 0; di < (alen_i + 1) * 8; di = di + 1) begin
                    if ((off_i + di) < cfg_w * 3) begin
                        if (bytes_per_line[line_i] == 0) mark_lo[line_i] = off_i + di;
                        bytes_per_line[line_i] = bytes_per_line[line_i] + 1;
                    end
                end
                // 单调性：同一行内地址必须递增
                if (!first_ar && (line_i == cur_line_of_addr(last_addr)) && (ar_addr <= last_addr)) begin
                    n_addr_back = n_addr_back + 1;
                    $display("[TB][WRN] 同行回退 addr=0x%08x line=%0d off=%0d 上一拍=0x%08x",
                             ar_addr, line_i, off_i, last_addr);
                end
                if (bytes_per_line[line_i] > ((cfg_w * 3 + 7) / 8) * 8 * 3)
                    $display("[TB][WRN] t=%0t line%0d 超额: %0d 字节 addr=%h off=%0d",
                             $time, line_i, bytes_per_line[line_i], ar_addr, off_i);
            end
            last_addr = ar_addr;
            first_ar  = 1'b0;
        end
    end

    // ---- R 通道 -----------------------------------------------------------
    always @(posedge aclk or negedge aresetn) begin
        if (!aresetn) begin
            b_valid <= 1'b0; b_addr <= 32'd0; b_left <= 0; b_delay <= 0;
            q_wr <= 0; q_rd <= 0;
        end else begin
            if (!b_valid && (q_wr != q_rd)) begin
                b_valid <= 1'b1;
                b_addr  <= q_addr[q_rd % QD];
                b_left  <= q_len[q_rd % QD] + 1;
                b_delay <= R_LAT;
                q_rd    <= q_rd + 1;
            end else if (b_valid && (b_delay != 0)) begin
                b_delay <= b_delay - 1;
            end else if (b_valid && r_ready) begin
                n_rbeat = n_rbeat + 1;
                b_addr  <= b_addr + 8;
                b_left  <= b_left - 1;
                if (b_left == 1) b_valid <= 1'b0;
            end
        end
    end

    // ------------------------------------------------------------- 像素比对
    localparam integer MAX_PIX = 8192;
    reg [23:0] gold [0:MAX_PIX-1];
    integer    gold_n = 0;

    integer px_i     = 0;      // 已收到的有效像素数
    integer px_cmp   = 0;      // 参与比对的像素数
    integer px_bad   = 0;
    integer n_de     = 0;      // pix_de 拍数
    integer n_valid  = 0;
    integer n_xy_bad = 0;
    integer n_show   = 0;      // 已打印的失配条数

    integer e_idx, e_x, e_y;
    reg [23:0] e_pix;

    always @(posedge pclk or negedge preseten) begin
        if (!preseten) begin
            px_i = 0; n_de = 0; n_valid = 0;
        end else begin
            if (pix_de) n_de = n_de + 1;
            if (pix_valid) begin
                n_valid = n_valid + 1;
                e_idx = px_i % (cfg_w * cfg_h);
                e_x   = e_idx % cfg_w;
                e_y   = e_idx / cfg_w;
                e_pix = gold[px_i % gold_n];

                if (pix_x !== e_x[11:0] || pix_y !== e_y[11:0]) begin
                    n_xy_bad = n_xy_bad + 1;
                    if (n_show < 6) begin
                        $display("[TB][ERR] 坐标失配 #%0d: got (x=%0d,y=%0d) exp (x=%0d,y=%0d)",
                                 px_i, pix_x, pix_y, e_x, e_y);
                        n_show = n_show + 1;
                    end
                end
                if (pix_rgb !== e_pix) begin
                    px_bad = px_bad + 1;
                    if (n_show < 6) begin
                        $display("[TB][ERR] 像素失配 #%0d (x=%0d,y=%0d): got %02x%02x%02x exp %02x%02x%02x",
                                 px_i, pix_x, pix_y,
                                 pix_rgb[23:16], pix_rgb[15:8], pix_rgb[7:0],
                                 e_pix[23:16], e_pix[15:8], e_pix[7:0]);
                        n_show = n_show + 1;
                    end
                end
                px_i    = px_i + 1;
                px_cmp  = px_cmp + 1;
            end
        end
    end

    // ------------------------------------------------------ 帧计数（vsync）
    integer n_vsync = 0;
    always @(posedge pclk) begin
        if (preseten && in_vsync) n_vsync = n_vsync + 1;
    end


    // ======================================================================
    //  DUT
    // ======================================================================
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

    // ======================================================================
    //  主流程
    // ======================================================================
    reg [1023:0] vecdir;
    integer i, ci, ncase, cid, total_bad, total_cmp;
    integer fd, fcode;
    reg [31:0] p_w, p_h, p_stride, p_addr;

    integer case_ids [0:15];

    task automatic reset_all;
        begin
            aresetn  = 1'b0;
            preseten = 1'b0;
            cfg_en   = 1'b0;
            cfg_srst = 1'b0;
            gen_run  = 1'b0;
            repeat (8) @(posedge aclk);
        end
    endtask

    task automatic run_case(input integer cid_i);
        begin
            // ---- 读参数（与生成脚本共用同一份，避免两边硬编码漂移）----
            fd = $fopen($sformatf("%0s/case%0d_par.txt", vecdir, cid_i), "r");
            if (fd == 0) begin
                $display("[TB][ERR] 打不开 case%0d_par.txt（vecdir=%0s）", cid_i, vecdir);
                $fatal(1);
            end
            fcode = $fscanf(fd, "%d %d %d %d", p_w, p_h, p_stride, p_addr);
            $fclose(fd);

            // ---- 复位 + 装载向量 ----
            reset_all;
            cfg_w      = p_w[15:0];
            cfg_h      = p_h[15:0];
            cfg_stride = p_stride;
            cfg_addr   = p_addr;
            cfg_fid    = 32'd0;      // 单缓冲：帧号 0 ⇒ buf 0
            cfg_en     = 1'b1;
            cfg_upd    = 1'b0;
            fid_upd    = 1'b0;

            for (i = 0; i < MEM_WORDS; i = i + 1) mem[i] = 64'hDEADBEEFCAFEBABE;
            $readmemh($sformatf("%0s/case%0d_mem.hex", vecdir, cid_i), mem,
                      (p_addr - MEM_BASE) >> 3);
            for (i = 0; i < MAX_PIX; i = i + 1) gold[i] = 24'hxxxxxx;
            $readmemh($sformatf("%0s/case%0d_gold.hex", vecdir, cid_i), gold);
            gold_n = p_w * p_h;
            if (gold_n > MAX_PIX) begin
                $display("[TB][ERR] gold_n=%0d > MAX_PIX", gold_n);
                $fatal(1);
            end

            // ---- 清统计 ----
            n_burst = 0; n_arlen_bad = 0; n_arsize_bad = 0; n_arburst_bad = 0;
            n_align_bad = 0; n_4k_bad = 0; n_outside = 0; arlen_max = 0;
            n_rbeat = 0; n_addr_back = 0; last_addr = p_addr; first_ar = 1'b1;
            line_bytes_min = 1 << 30; line_bytes_max = -1; line_bytes_sum = 0;
            px_i = 0; px_cmp = 0; px_bad = 0; n_de = 0; n_valid = 0;
            n_xy_bad = 0; n_show = 0; n_vsync = 0;
            for (i = 0; i < 4096; i = i + 1) bytes_per_line[i] = 0;

            // ---- 放复位：先放 AXI（配置快照要几拍），再放像素侧 ----
            aresetn = 1'b1;
            repeat (4) @(posedge aclk);
            cfg_upd = 1'b1;                  // PS 写完 CTRL/VIDEO_MODE/FB_* ⇒ 打脉冲
            @(posedge aclk);
            cfg_upd = 1'b0;
            repeat (64) @(posedge aclk);     // 配置提交 + toggle 握手走完
            preseten = 1'b1;
            repeat (8) @(posedge pclk);
            @(negedge pclk);             // 在负沿开视频：避免与 posedge 同拍竞争
            gen_run = 1'b1;              // 配置已落定 ⇒ 再开视频（等价硬件顺序）

            // ---- 跑满 3 整帧就收手（最后一帧末尾还没到第 4 个 vsync，
            //      否则会多取一帧的第 0 行，把"每行字节数"判据带偏）----
            wait (px_i >= 3 * gold_n);
            repeat (20) @(posedge pclk);

            // ---- 判据 ----
            $display("");
            $display("[TB] ================= case%0d =================", cid_i);
            $display("[TB] W=%0d H=%0d stride=%0d (W*3=%0d, pad=%0dB) fb=0x%08x",
                     cfg_w, cfg_h, cfg_stride, cfg_w * 3, cfg_stride - cfg_w * 3, cfg_addr);
            $display("[TB] ① 逐字节: compared=%0d mismatches=%0d  (px_coord_bad=%0d)",
                     px_cmp, px_bad, n_xy_bad);
            $display("[TB]    pix_valid=%0d  pix_de=%0d  vsync=%0d  fid_latched=%0d",
                     n_valid, n_de, n_vsync, fid_lat);

            // 每行读了多少字节（判据②）
            line_bytes_min = 1 << 30; line_bytes_max = -1; line_bytes_sum = 0;
            n_line_rd = 0;
            for (i = 0; i < cfg_h; i = i + 1) begin
                if (bytes_per_line[i] != 0) begin
                    if (bytes_per_line[i] < line_bytes_min) line_bytes_min = bytes_per_line[i];
                    if (bytes_per_line[i] > line_bytes_max) line_bytes_max = bytes_per_line[i];
                    line_bytes_sum = line_bytes_sum + bytes_per_line[i];
                    n_line_rd = n_line_rd + 1;
                end
            end
            $display("[TB] ② 有效区读取: 越界突发=%0d  行数=%0d/%0d  每行累计字节 min/max=%0d/%0d (3 帧 × %0d)",
                     n_outside, n_line_rd, cfg_h, line_bytes_min, line_bytes_max,
                     ((cfg_w * 3 + 7) / 8) * 8);
            $display("[TB] ③ AXI: bursts=%0d rbeats=%0d arlen_max=%0d  (arlen>15:%0d arsize!=8B:%0d arb >INCR:%0d 未对齐:%0d 跨4KB:%0d 同行回退:%0d)",
                     n_burst, n_rbeat, arlen_max, n_arlen_bad, n_arsize_bad,
                     n_arburst_bad, n_align_bad, n_4k_bad, n_addr_back);
            $display("[TB]    行首地址递增: 首=0x%08x 末=0x%08x  (末-首)/stride=%0d",
                     p_addr, last_addr, (last_addr - p_addr) / cfg_stride);
            $display("[TB] ⑤ STATUS: underflow=%0d vsync_seen=%0d", st_uf, st_vs);

            total_cmp = total_cmp + px_cmp;
            if (px_bad != 0 || n_xy_bad != 0) total_bad = total_bad + 1;

            // 每行期望字节 = align8(W*3)；跑 3 帧 ⇒ 每行累计 3 份
            line_expect = 3 * (((cfg_w * 3 + 7) / 8) * 8);
            if ((px_bad == 0) && (n_xy_bad == 0) && (px_cmp == 3 * gold_n) &&
                (n_outside == 0) && (n_arlen_bad == 0) && (n_arsize_bad == 0) &&
                (n_arburst_bad == 0) && (n_align_bad == 0) && (n_4k_bad == 0) &&
                (n_addr_back == 0) && (st_uf == 0) &&
                (line_bytes_min == line_expect) && (line_bytes_max == line_expect) &&
                (n_line_rd == cfg_h))
                $display("[TB] ✅ case%0d PASS", cid_i);
            else
                $display("[TB] ❌ case%0d FAIL", cid_i);
        end
    endtask

    initial begin
        total_bad = 0;
        total_cmp = 0;
        vecdir = "runs/hdmi_p2";
        if (!$value$plusargs("VECDIR=%s", vecdir)) begin
            $display("[TB] 提示: 没有 +VECDIR，用默认 %0s", vecdir);
        end
        ncase = 3;
        if (!$value$plusargs("NCASES=%d", ncase)) ncase = 3;
        for (ci = 0; ci < ncase; ci = ci + 1) case_ids[ci] = ci + 1;
        if ($test$plusargs("CASES")) begin
            // 简化：+NCASES=n 或用默认 1..3
        end

        for (i = 0; i < MEM_WORDS; i = i + 1) mem[i] = 64'hDEADBEEFCAFEBABE;

        for (ci = 0; ci < ncase; ci = ci + 1) run_case(case_ids[ci]);

        $display("");
        $display("[TB] ============ 汇总 ============");
        $display("[TB] 比对像素总数 = %0d，失败用例 = %0d", total_cmp, total_bad);
        if (total_bad == 0) begin
            $display("===== PASS =====");
        end else begin
            $display("===== FAIL =====");
        end
        $finish;
    end

    // 全局超时
    initial begin
        #5_000_000;
        $display("[TB][ERR] 仿真超时");
        $display("===== FAIL =====");
        $finish;
    end

endmodule
