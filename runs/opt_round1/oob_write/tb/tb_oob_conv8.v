// ============================================================================
//  tb_oob_conv8.v ——「输出写口是否写超 out_bytes」自包含探针（单元级）
//
//  目的（任务：引擎越界写 8 字节）：
//    只回答一个问题 —— **bin_conv_core8 的写口（mem_wr_en/addr/sel）实际写到
//    的最大字节地址**，与本层"按 PS 公式应写"的 out_bytes =
//    ceil(cout/8) * ceil(H/s) * ceil(W/s) * 8 相比，是否超、超多少。
//
//  为什么单元级就够（写地址与数据无关）：
//    · 写地址链 = oaddr_q（CMP 期每 term_last 累加 VLANES）→ s1_oa_q → osnap
//      → a1..a7 → wr_addr_q。**全部由计数器/常量推导，不吃数据**。
//    · 写数据（sat8）不影响地址、也不影响状态机（状态机只看 l/b/ky/kx/ox/oy）。
//    ⇒ 读数据用确定性伪随机（addr 混入），不需要任何 golden 向量。
//
//  读侧时序契约（与 bin_unit_axi_wrap 一致）：
//    t 拍 mem_rd_en/addr 有效 ⇒ t+1 拍 mem_rd_data 有效（BRAM 语义）。
//
//  判据（每个形状打印一行 OOBREP）：
//    exp      = ceil(cout/8)*Hout*Wout*8        （PS 的 BinLayer.out_bytes）
//    maxend   = max over 写拍 (addr + 最高置位字节位)   ← 实际写到的最后 1 个字节
//    oob      = maxend + 1 - exp                （>0 即越界）
//    beats    = 写拍数；bytes = 置位字节位计数
//    misalign = 非 8 字节对齐的写拍数（C8 布局下应为 0）
//
//  plusarg：
//    +SWEEP=1            跑内建形状表（默认）
//    +COUT +CIN +K +S +PAD +H +W +SHIFT +BASE   单形状模式（SWEEP=0）
//    +TIMEOUT=<cyc>      单形状拍数上限（默认按形状自动算）
//    +VERBOSE=1          每拍打印写口
// ============================================================================
`timescale 1ns/1ps

module tb_oob_conv8;

    // ---------------- 时钟 / 复位 ----------------
    logic clk = 1'b0;
    logic rstn = 1'b0;
    always #5 clk = ~clk;             // 100 MHz（时序无关，只为快）
    logic ce = 1'b1;

    // ---------------- DUT 端口 ----------------
    logic        start, done, busy;
    logic [15:0] cfg_cin, cfg_cout, cfg_k, cfg_s, cfg_d, cfg_pad, cfg_h, cfg_w;
    logic [5:0]  ash, ssh, shift;
    logic [31:0] b_act, b_wgt, b_alpha, b_bias, b_zeta, b_gamma, b_slope, b_out;
    logic        rd_en;
    logic [31:0] rd_addr;
    logic [63:0] rd_data;
    logic        wr_en;
    logic [31:0] wr_addr;
    logic [63:0] wr_data;
    logic [7:0]  wr_sel;

    int n_valid   = 0;
    int n_oob     = 0;
    int failing   = 0;

    bin_conv_core8 dut (
        .ap_clk(clk), .ap_rst_n(rstn), .ap_ce(ce),
        .ctrl_start(start), .ctrl_done(done), .ctrl_busy(busy),
        .cfg_cin(cfg_cin), .cfg_cout(cfg_cout), .cfg_k(cfg_k), .cfg_s(cfg_s),
        .cfg_d(cfg_d), .cfg_pad(cfg_pad), .cfg_h(cfg_h), .cfg_w(cfg_w),
        .cfg_alpha_shift(ash), .cfg_slope_shift(ssh), .cfg_shift(shift),
        .base_act(b_act), .base_wgt(b_wgt), .base_alpha(b_alpha),
        .base_bias(b_bias), .base_zeta(b_zeta), .base_gamma(b_gamma),
        .base_slope(b_slope), .base_out(b_out),
        .mem_rd_en(rd_en), .mem_rd_addr(rd_addr), .mem_rd_data(rd_data),
        .mem_wr_en(wr_en), .mem_wr_addr(wr_addr),
        .mem_wr_data(wr_data), .mem_wr_sel(wr_sel)
    );

    // ---------------- 行为读模型（1 拍延迟，内容对地址无影响）----------------
    always_ff @(posedge clk) begin
        if (ce)
            rd_data <= rd_en ? ({32'h9E37_79B9 ^ rd_addr, 32'h7F4A_7C15 + rd_addr})
                             : rd_data;
    end

    // ---------------- 写口监视（越界探测的唯一判据）----------------
    //  last_byte : 实际被写到的最大字节地址（含），-1 表示还没写
    //  exp       : 本形状"应写"的字节数
    longint last_byte;
    longint exp_bytes;
    longint wr_bytes;
    longint wr_beats;
    longint misalign;
    longint wid_max;
    int     verbose = 0;

    task automatic mon_reset(input longint e);
        last_byte = -1;
        exp_bytes = e;
        wr_bytes  = 0;
        wr_beats  = 0;
        misalign  = 0;
        wid_max   = 0;
    endtask

    always @(posedge clk) begin
        int hi;
        if (rstn && wr_en) begin
            hi = -1;
            // 手写字节计数（xsim 的 $countones 在 8bit→longint 赋值上会给错值）
            for (int b = 0; b < 8; b++) if (wr_sel[b]) begin hi = b; wr_bytes = wr_bytes + 1; end;
            if (hi >= 0) begin
                wr_beats = wr_beats + 1;
                if (wr_addr[2:0] != 3'd0 && hi > 0) misalign = misalign + 1;
                if (longint'(wr_addr) + hi > last_byte)
                    last_byte = longint'(wr_addr) + hi;
                if (longint'(wr_addr) > wid_max) wid_max = longint'(wr_addr);
                if (verbose)
                    $display("    [WR] addr=%0d sel=%02x data=%016x", wr_addr, wr_sel, wr_data);
            end
        end
    end

    // ---------------- 调试迹：每 5000 拍打一次状态（VERBOSE=1 才开）----------------
    longint dbg_cyc = 0;
    always @(posedge clk) begin
        dbg_cyc = dbg_cyc + 1;
        if (verbose && (dbg_cyc % 500) == 0)
            $display("  [DBG t=%0d] state=%0d busy=%b done=%b rd_en=%b wr_en=%b prm_i=%0d wl_l=%0d wl_lane=%0d cmp_init=%b pix_last=%b oaddr=%h",
                     dbg_cyc, dut.state, busy, done, rd_en, wr_en, dut.prm_i, dut.wl_l,
                     dut.wl_lane, dut.cmp_init, dut.pix_last, dut.oaddr_q);
    end

    // ---------------- 单形状执行 ----------------
    task automatic run_shape(input int cout_, cin_, k_, s_, d_, pad_, h_, w_, sh_,
                             input longint base_, input int tagi);
        longint hout, wout, cg, exp_;
        longint cyc, t0;
        longint oob;
        begin
            // 期望 out_bytes（= bin_engine.BinLayer.out_bytes）
            hout = (h_ - 1) / s_ + 1;
            wout = (w_ - 1) / s_ + 1;
            cg   = (cout_ + 7) / 8;
            exp_ = cg * hout * wout * 8;

            // 复位 + 配置
            //  ⚠ 全部用非阻塞赋值：TB 里 @(posedge clk) 之后紧跟阻塞赋值会与 DUT 的
            //    always_ff **同拍竞争**（DUT 可能采样到"已改回去"的值）⇒ 曾经表现为
            //    start 脉冲被吃掉、DUT 永远 IDLE。这是 TB 的坑，不是 DUT 的。
            rstn <= 1'b0;
            start <= 1'b0;
            repeat (4) @(posedge clk);
            cfg_cin <= cin_[15:0]; cfg_cout <= cout_[15:0]; cfg_k <= k_[15:0];
            cfg_s   <= s_[15:0];   cfg_d <= d_[15:0]; cfg_pad <= pad_[15:0];
            cfg_h   <= h_[15:0];   cfg_w <= w_[15:0];
            ash <= 6'd9; ssh <= 6'd8; shift <= sh_[5:0];
            b_act <= 32'h0010_0000; b_wgt <= 32'h0020_0000; b_alpha <= 32'h0030_0000;
            b_bias<= 32'h0031_0000; b_zeta<= 32'h0032_0000; b_gamma <= 32'h0033_0000;
            b_slope<=32'h0034_0000; b_out <= base_[31:0];
            rstn <= 1'b1;
            mon_reset(exp_);
            repeat (2) @(posedge clk);

            start <= 1'b1;
            @(posedge clk);
            start <= 1'b0;
            if (verbose) begin
                $display("  [DBG start] start=%b start_q=%b start_pulse=%b rstn=%b ce=%b state=%0d",
                         start, dut.start_q, dut.start_pulse, rstn, ce, dut.state);
                repeat (4) @(posedge clk);
                $display("  [DBG after4] state=%0d prm_i=%0d rd_en=%b busy=%b",
                         dut.state, dut.prm_i, rd_en, busy);
            end

            // 拍数上限：cout*(20 + 2K + Hout*Wout*K) 的 4 倍 + 余量
            cyc = longint'(cout_) * (20 + 2*longint'(k_)*k_ +
                                      hout*wout*longint'(k_)*k_);
            cyc = cyc * 4 + 2000;
            t0  = 0;
            while (!done && t0 < cyc) begin
                @(posedge clk);
                t0 = t0 + 1;
            end
            repeat (20) @(posedge clk);      // 让写口尾部排空

            oob = last_byte + 1 - (base_ + exp_);   // 相对本层基址的越界字节数
            n_valid = n_valid + 1;
            if (oob != 0) begin
                n_oob = n_oob + 1;
                if (oob > 0) failing = failing + 1;
            end
            $display("OOBREP tag=sw%0d cout=%0d cin=%0d k=%0d s=%0d d=%0d pad=%0d H=%0d W=%0d sh=%0d",
                     tagi, cout_, cin_, k_, s_, d_, pad_, h_, w_, sh_);
            $display("OOBREP2 base=%08x Hout=%0d Wout=%0d cg=%0d exp=%0d lastbyte=%0d oob=%0d beats=%0d wrbytes=%0d misalign=%0d timeout=%0d",
                     base_[31:0], hout, wout, cg, exp_, last_byte, oob,
                     wr_beats, wr_bytes, misalign, (t0 >= cyc));
        end
    endtask

    // ---------------- 形状表 ----------------
    //  {cout, cin, k, s, d, pad, H, W, shift}
    //  覆盖：k1/k3/k5、s1/s2、pad 0..3、cout ∈ {8,16,32,48,64,96,192}、
    //        奇数 H/W（行尾余数）、H/W=1（退化）、cin%64 != 0（补零通道）
    int sweeps [0:63][0:8];
    int nsweep;

    // 形状表填充：直接逐条赋值（避免在循环里声明自动数组，xsim 兼容性最稳）
    task automatic add_shape(input int co, cin, kk, ss, dd, pp, hh, ww, sh);
        sweeps[nsweep][0] = co; sweeps[nsweep][1] = cin; sweeps[nsweep][2] = kk;
        sweeps[nsweep][3] = ss; sweeps[nsweep][4] = dd;  sweeps[nsweep][5] = pp;
        sweeps[nsweep][6] = hh; sweeps[nsweep][7] = ww;  sweeps[nsweep][8] = sh;
        nsweep = nsweep + 1;
    endtask

    task automatic build_sweep();
        nsweep = 0;
        // ---- 组 A：cout 扫描（k=1,pad=0；奇数行列）----
        add_shape(  8, 64, 1, 1, 1, 0,  3,  3, 8);
        add_shape( 16, 64, 1, 1, 1, 0,  5,  5, 8);
        add_shape( 32, 64, 1, 1, 1, 0,  7,  7, 8);
        add_shape( 48, 64, 1, 1, 1, 0,  8,  8, 8);
        add_shape( 64, 64, 1, 1, 1, 0,  9,  9, 8);
        add_shape(192, 64, 1, 1, 1, 0, 16, 16, 8);
        // ---- 组 B：k=3 素数行列（tile/行尾余数）----
        add_shape( 32, 64, 3, 1, 1, 1,  1,  1, 8);
        add_shape( 32, 64, 3, 1, 1, 1,  2,  2, 8);
        add_shape( 32, 64, 3, 1, 1, 1,  3,  3, 8);
        add_shape( 32, 64, 3, 1, 1, 1,  5,  5, 8);
        add_shape( 32, 64, 3, 1, 1, 1,  7,  7, 8);
        add_shape( 32, 64, 3, 1, 1, 1, 11, 11, 8);
        add_shape( 32, 64, 3, 1, 1, 1, 13, 13, 8);
        // ---- 组 C：k=3 + s=2（Wout=ceil(W/2)）----
        add_shape( 32, 64, 3, 2, 1, 1,  2,  2, 8);
        add_shape( 32, 64, 3, 2, 1, 1,  3,  3, 8);
        add_shape( 32, 64, 3, 2, 1, 1,  5,  5, 8);
        add_shape( 32, 64, 3, 2, 1, 1,  6,  6, 8);
        add_shape( 32, 64, 3, 2, 1, 1,  9,  9, 8);
        add_shape( 32, 64, 3, 2, 1, 1, 10, 10, 8);
        // ---- 组 D：k=1 + s=2 ----
        add_shape( 48, 64, 1, 2, 1, 0,  1,  1, 8);
        add_shape( 48, 64, 1, 2, 1, 0,  2,  2, 8);
        add_shape( 48, 64, 1, 2, 1, 0,  3,  3, 8);
        add_shape( 48, 64, 1, 2, 1, 0,  5,  5, 8);
        // ---- 组 E：pad 2/3、k=5、cin%64!=0、大 cout ----
        add_shape( 64, 64, 3, 1, 1, 2,  5,  7, 8);
        add_shape( 64, 64, 3, 1, 1, 3,  4,  6, 8);
        add_shape( 32, 96, 5, 1, 1, 2,  5,  5, 8);
        add_shape( 32, 96, 3, 1, 1, 1,  7,  9, 8);
        add_shape(192,128, 3, 1, 1, 1,  4,  5, 8);
        add_shape( 96,256, 3, 2, 1, 1,  6,  7, 8);
        // ---- 组 F：退化形状 ----
        add_shape( 32, 64, 3, 1, 1, 1,  1, 16, 8);
        add_shape( 32, 64, 3, 1, 1, 1, 16,  1, 8);
        add_shape( 32, 64, 1, 1, 1, 0,  2,  2, 0);
        // ---- 组 H：本网真实最大层的形状（out_bytes = 6291456 @256²）----
        add_shape( 96, 64, 1, 1, 1, 0, 256, 256, 8);   // cg=12, K=1：696k 拍
        add_shape( 96, 64, 3, 1, 1, 1,  64,  64, 8);   // cg=12, K=9：443k 拍
        // ---- 组 G：shift 边界 ----
        add_shape( 32, 64, 1, 1, 1, 0,  5,  5, 0);
        add_shape( 32, 64, 1, 1, 1, 0,  5,  5, 63);
    endtask

    // ========================================================================
    //  ★ 本轮（3 层金丝雀审计）新增形状表。旧表 build_sweep() 保持原样，
    //    用 +SHAPESET 选表 ⇒ 上一轮的 36 个形状仍可逐字节复跑（不影响旧结论）。
    //
    //  SHAPESET=1：板上**唯一**能看见 8 字节越界的 3 层
    //    依据 runs/board_150v22/board_out/layer_probe_256.json：227 层里
    //    out_bytes 的最大值 = 1572864（= PS 的 max_out），而 OUT 槽长恒为
    //    max_out+4096、金丝雀起点 = 槽尾-4096 = base_out+max_out
    //    （bin_net.py:808-810 `arena.region(nout + 4096 ...)`；
    //      bin_engine.py:209-211 `start = len(buf)-CANARY_BYTES`）
    //    ⇒ out_bytes < max_out 的层，多写的 8 字节落在"松量"里，金丝雀**看不见**。
    // ========================================================================
    task automatic build_3l();
        nsweep = 0;
        // {cout, cin, k, s, d, pad, H, W, shift}
        //  idx190 cin=64  k3 s1 d1 pad1 128² shift6  cb=1 K=9   out_bytes=1572864
        add_shape( 96,  64, 3, 1, 1, 1, 128, 128, 6);
        //  idx191 cin=160 k3 s1 d2 pad2 128² shift7  cb=3 K=27（全网唯一 d=2 的大层）
        add_shape( 96, 160, 3, 1, 2, 2, 128, 128, 7);
        //  idx192 cin=256 k3 s1 d1 pad1 128² shift6  cb=4 K=36
        add_shape( 96, 256, 3, 1, 1, 1, 128, 128, 6);
    endtask

    //  SHAPESET=2：cout×Hout 变体网格（专找"多写一拍 = 8 字节"）
    //    cout ∈ {88,92,96,100}：88%8=0；92/100 ⇒ cg=12/13 且末组只有 4 lane
    //      ⇒ wr_sel=0x0F ≠ 8'hFF，包装层改走"逐字节攒字"路径（u_wr_wide_w=0）
    //    Hout ∈ {124,128,132}：128 是板上真值；124/132 探行数奇偶与 tile 余数
    task automatic build_var();
        nsweep = 0;
        add_shape( 88, 64, 3, 1, 1, 1, 124, 124, 6);
        add_shape( 88, 64, 3, 1, 1, 1, 128, 128, 6);
        add_shape( 88, 64, 3, 1, 1, 1, 132, 132, 6);
        add_shape( 92, 64, 3, 1, 1, 1, 124, 124, 6);
        add_shape( 92, 64, 3, 1, 1, 1, 128, 128, 6);
        add_shape( 92, 64, 3, 1, 1, 1, 132, 132, 6);
        add_shape( 96, 64, 3, 1, 1, 1, 124, 124, 6);
        add_shape( 96, 64, 3, 1, 1, 1, 128, 128, 6);
        add_shape( 96, 64, 3, 1, 1, 1, 132, 132, 6);
        add_shape(100, 64, 3, 1, 1, 1, 124, 124, 6);
        add_shape(100, 64, 3, 1, 1, 1, 128, 128, 6);
        add_shape(100, 64, 3, 1, 1, 1, 132, 132, 6);
    endtask

    //    SHAPESET=3：贴金丝雀的"末拍形态"专测 —— cout=96/Hout=Wout=128 的三种
    //    kernel 与 dilation 组合 + 仅 1 行/1 像素的退化（把"最后 8 字节来自哪一拍"
    //    压到最小可观测规模）
    task automatic build_edge();
        nsweep = 0;
        add_shape( 96,  64, 1, 1, 1, 0, 128, 128, 6);   // k=1：每像素 1 笔
        add_shape( 96,  64, 3, 1, 2, 2, 128, 128, 7);   // d=2
        add_shape( 96,  64, 3, 1, 1, 1, 128, 128, 6);   // k=3 基准
        add_shape( 96,  64, 3, 1, 1, 1,   1, 128, 6);   // 单行：行尾=层尾
        add_shape( 96,  64, 3, 1, 1, 1, 128,   1, 6);   // 单列
        add_shape( 96,  64, 3, 1, 1, 1,   1,   1, 6);   // 单像素
    endtask

    // ---------------- 主流程 ----------------
    int    p_cout, p_cin, p_k, p_s, p_d, p_pad, p_h, p_w, p_shift;
    int    p_sweep, p_nshape, p_shapeset;
    longint p_base;
    int    p_tagi;
    longint bases [0:2];

    initial begin
        void'($value$plusargs("VERBOSE=%d", verbose));
        void'($value$plusargs("SWEEP=%d", p_sweep));
        if (!$value$plusargs("SWEEP=%d", p_sweep)) p_sweep = 1;
        if (!$value$plusargs("NSHAPE=%d", p_nshape)) p_nshape = 999;
        if (!$value$plusargs("SHAPESET=%d", p_shapeset)) p_shapeset = 0;

        $display("=== tb_oob_conv8 start  sweep=%0d shapeset=%0d ===", p_sweep, p_shapeset);
        bases[0] = 64'h0000_0000_0010_0000;   // 1 MiB，8 对齐
        bases[1] = 64'h0000_0000_0010_0008;   // +8
        bases[2] = 64'h0000_0000_2A5A_5000;

        if (p_sweep == 1) begin
            if      (p_shapeset == 1) build_3l();
            else if (p_shapeset == 2) build_var();
            else if (p_shapeset == 3) build_edge();
            else                      build_sweep();
            for (int i = 0; i < nsweep && i < p_nshape; i++) begin
                p_tagi = i;
                run_shape(sweeps[i][0], sweeps[i][1], sweeps[i][2], sweeps[i][3],
                          sweeps[i][4], sweeps[i][5], sweeps[i][6], sweeps[i][7],
                          sweeps[i][8], bases[i % 2], p_tagi);
            end
        end else begin
            void'($value$plusargs("COUT=%d",  p_cout));
            void'($value$plusargs("CIN=%d",   p_cin));
            void'($value$plusargs("K=%d",     p_k));
            void'($value$plusargs("S=%d",     p_s));
            void'($value$plusargs("D=%d",     p_d));
            void'($value$plusargs("PAD=%d",   p_pad));
            void'($value$plusargs("H=%d",     p_h));
            void'($value$plusargs("W=%d",     p_w));
            void'($value$plusargs("SHIFT=%d", p_shift));
            if (!$value$plusargs("BASEH=%h", p_base)) p_base = bases[0];
            p_tagi = 999;
            run_shape(p_cout, p_cin, p_k, p_s, p_d, p_pad, p_h, p_w, p_shift,
                      p_base, p_tagi);
        end

        $display("=== SUMMARY shapes=%0d oob_nonzero=%0d oob_over=%0d ===",
                 n_valid, n_oob, failing);
        if (failing > 0) $display("=== RESULT FAIL(越界) ===");
        else             $display("=== RESULT PASS(无越界) ===");
        $finish;
    end

    // 兜底超时（防止形状表把仿真挂死）
    initial begin
        #( `ifdef GLOBAL_CYC  `GLOBAL_CYC `else 2_000_000_000 `endif );  // 400ms -> 2s（3 层/变体表要 ~50M 拍）
        $display("=== RESULT GLOBAL-TIMEOUT ===");
        $finish;
    end

endmodule
