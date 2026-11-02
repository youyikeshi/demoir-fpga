// ============================================================================
//  tb_bin_conv_core_v50.v —— v50 IMP-A 交付物（bin_conv_core.v，VLANES 参数化）
//                            的 xsim 回归 + 等价性 TB        （SIM / v50_simA）
//
//  结构照抄 tb/tb_bin_conv_core_lane8.v（字节 RAM 模型 + 1 拍读延迟 + 字节使能写），
//  但把"golden 文件"换成"同一块 RAM 喂所有 DUT"，因为本 TB 要证明的是
//  **DUT 之间逐 bit 相同**，不是和软件 golden 相同。
//
//  4 个 DUT 实例，共用一块 byte RAM（输入区只读、输出平面按实例分开）：
//    [0] u_org : runs/v42e_archive/rtl_v39_exact/bin_conv_core8.v   （v39 原版，8 lane 写死）
//    [1] u_a   : bin_conv_core_v50 #(.VLANES(8))   —— S1 的被测；S2 里当第 p 趟 8-lane
//    [2] u_b   : bin_conv_core_v50 #(.VLANES(8))   —— 备用（第 2 趟并行，当前按顺序复用 u_a）
//    [3] u_c   : bin_conv_core_v50 #(.VLANES(16))  —— S2/S5 的被测（128 bit 写口）
//
//  S1：org 与 new(VLANES=8) 同 cfg 同输入 ⇒ 写事务（地址/数据/字节使能）逐 bit 比。
//  S2：new(VLANES=16, cout=C) 一次  vs  ceil(C/8) 趟 new(VLANES=8, cout<=8)，
//      第 p 趟的参数基址/权重基址按 8p lane 平移（= 包装层的 o0, o0+8 串行化）。
//      逐 lane 比数据与字节使能；另比地址（暴露 group 平面步长问题）。
//
//  用法：  xsim simA -runall                       # 24 组（20 随机 + 4 边界）
//          xsim simA -runall -testplusarg NCASE=1   # 只跑 1 组
//          xsim simA -runall -testplusarg DUMP      # 加 $dumpvars（波形取证）
//  判据：全 0 diff ⇒ "PASS"，任一 diff ⇒ 打印 (case, tx#, lane, exp, act) 并置 fail
// ============================================================================
`timescale 1ns/1ps
module tb_bin_conv_core_v50;

    // ---- 内存布局（互不重叠；输出平面每个 64 KB）----
    localparam int MAXM      = 1 << 22;
    localparam int BASE_ACT   = 32'h0001_0000;   // 激活区（pad 负偏移也在这儿之上）
    localparam int BASE_WGT   = 32'h0004_0000;   // 权重区（线性 oc*K*8）
    localparam int BASE_ALPHA = 32'h0010_0000;
    localparam int BASE_BIAS  = 32'h0010_1000;
    localparam int BASE_ZETA  = 32'h0010_2000;
    localparam int BASE_GAMMA = 32'h0010_3000;
    localparam int BASE_SLOPE = 32'h0010_4000;
    localparam int OUT0       = 32'h0020_0000;   // u_org
    localparam int OUT1       = 32'h0021_0000;   // u_a（S1 用）；S2 里第 p 趟用 OUT1 + p*64K
    localparam int OUT2       = 32'h0022_0000;   // u_b
    localparam int OUT3       = 32'h0023_0000;   // u_c
    localparam int IN_LO      = 0;
    localparam int IN_HI      = 32'h0004_FFFF;
    localparam int PM_LO      = 32'h0010_0000;
    localparam int PM_HI      = 32'h0010_4FFF;
    localparam int OT_LO      = 32'h0020_0000;
    localparam int OT_HI      = 32'h0024_FFFF;

    localparam int MAXTX      = 2048;

    logic [7:0] mem [0:MAXM-1];

    logic clk = 1'b0;
    always #5 clk = ~clk;
    logic rst_n = 1'b0;

    // ---------------- 每 DUT 的激励 ----------------
    logic [15:0] c_cin [0:3], c_cout[0:3], c_k[0:3], c_s[0:3], c_d[0:3],
                 c_pad [0:3], c_h[0:3], c_w[0:3];
    logic [5:0]  c_ash [0:3], c_ssh[0:3], c_sh[0:3];
    logic [31:0] b_act [0:3], b_wgt[0:3], b_al[0:3], b_bi[0:3],
                 b_ze  [0:3], b_ga[0:3], b_sl[0:3], b_out[0:3];
    logic        start [0:3];
    logic        done  [0:3], busy[0:3];
    logic        rden  [0:3];
    logic [31:0] raddr [0:3];
    logic [63:0] rdata [0:3];
    logic        wren  [0:3];
    logic [31:0] waddr [0:3];
    logic [63:0] wdata64[0:3];
    logic [127:0] wdata128[0:0];
    logic [7:0]  wsel8 [0:3];
    logic [15:0] wsel16[0:0];

    // ---------------- 实例 ----------------
    bin_conv_core8 u_org (
        .ap_clk(clk), .ap_ce(1'b1), .ap_rst_n(rst_n),
        .ctrl_start(start[0]), .ctrl_done(done[0]), .ctrl_busy(busy[0]),
        .cfg_cin(c_cin[0]), .cfg_cout(c_cout[0]), .cfg_k(c_k[0]),
        .cfg_s(c_s[0]), .cfg_d(c_d[0]), .cfg_pad(c_pad[0]),
        .cfg_h(c_h[0]), .cfg_w(c_w[0]),
        .cfg_alpha_shift(c_ash[0]), .cfg_slope_shift(c_ssh[0]), .cfg_shift(c_sh[0]),
        .base_act(b_act[0]), .base_wgt(b_wgt[0]),
        .base_alpha(b_al[0]), .base_bias(b_bi[0]),
        .base_zeta(b_ze[0]), .base_gamma(b_ga[0]),
        .base_slope(b_sl[0]), .base_out(b_out[0]),
        .mem_rd_en(rden[0]), .mem_rd_addr(raddr[0]), .mem_rd_data(rdata[0]),
        .mem_wr_en(wren[0]), .mem_wr_addr(waddr[0]),
        .mem_wr_data(wdata64[0]), .mem_wr_sel(wsel8[0])
    );

    bin_conv_core_v50 #(.VLANES(8)) u_a (
        .ap_clk(clk), .ap_ce(1'b1), .ap_rst_n(rst_n),
        .ctrl_start(start[1]), .ctrl_done(done[1]), .ctrl_busy(busy[1]),
        .cfg_cin(c_cin[1]), .cfg_cout(c_cout[1]), .cfg_k(c_k[1]),
        .cfg_s(c_s[1]), .cfg_d(c_d[1]), .cfg_pad(c_pad[1]),
        .cfg_h(c_h[1]), .cfg_w(c_w[1]),
        .cfg_alpha_shift(c_ash[1]), .cfg_slope_shift(c_ssh[1]), .cfg_shift(c_sh[1]),
        .base_act(b_act[1]), .base_wgt(b_wgt[1]),
        .base_alpha(b_al[1]), .base_bias(b_bi[1]),
        .base_zeta(b_ze[1]), .base_gamma(b_ga[1]),
        .base_slope(b_sl[1]), .base_out(b_out[1]),
        .mem_rd_en(rden[1]), .mem_rd_addr(raddr[1]), .mem_rd_data(rdata[1]),
        .mem_wr_en(wren[1]), .mem_wr_addr(waddr[1]),
        .mem_wr_data(wdata64[1]), .mem_wr_sel(wsel8[1])
    );

    bin_conv_core_v50 #(.VLANES(8)) u_b (
        .ap_clk(clk), .ap_ce(1'b1), .ap_rst_n(rst_n),
        .ctrl_start(start[2]), .ctrl_done(done[2]), .ctrl_busy(busy[2]),
        .cfg_cin(c_cin[2]), .cfg_cout(c_cout[2]), .cfg_k(c_k[2]),
        .cfg_s(c_s[2]), .cfg_d(c_d[2]), .cfg_pad(c_pad[2]),
        .cfg_h(c_h[2]), .cfg_w(c_w[2]),
        .cfg_alpha_shift(c_ash[2]), .cfg_slope_shift(c_ssh[2]), .cfg_shift(c_sh[2]),
        .base_act(b_act[2]), .base_wgt(b_wgt[2]),
        .base_alpha(b_al[2]), .base_bias(b_bi[2]),
        .base_zeta(b_ze[2]), .base_gamma(b_ga[2]),
        .base_slope(b_sl[2]), .base_out(b_out[2]),
        .mem_rd_en(rden[2]), .mem_rd_addr(raddr[2]), .mem_rd_data(rdata[2]),
        .mem_wr_en(wren[2]), .mem_wr_addr(waddr[2]),
        .mem_wr_data(wdata64[2]), .mem_wr_sel(wsel8[2])
    );

    bin_conv_core_v50 #(.VLANES(16)) u_c (
        .ap_clk(clk), .ap_ce(1'b1), .ap_rst_n(rst_n),
        .ctrl_start(start[3]), .ctrl_done(done[3]), .ctrl_busy(busy[3]),
        .cfg_cin(c_cin[3]), .cfg_cout(c_cout[3]), .cfg_k(c_k[3]),
        .cfg_s(c_s[3]), .cfg_d(c_d[3]), .cfg_pad(c_pad[3]),
        .cfg_h(c_h[3]), .cfg_w(c_w[3]),
        .cfg_alpha_shift(c_ash[3]), .cfg_slope_shift(c_ssh[3]), .cfg_shift(c_sh[3]),
        .base_act(b_act[3]), .base_wgt(b_wgt[3]),
        .base_alpha(b_al[3]), .base_bias(b_bi[3]),
        .base_zeta(b_ze[3]), .base_gamma(b_ga[3]),
        .base_slope(b_sl[3]), .base_out(b_out[3]),
        .mem_rd_en(rden[3]), .mem_rd_addr(raddr[3]), .mem_rd_data(rdata[3]),
        .mem_wr_en(wren[3]), .mem_wr_addr(waddr[3]),
        .mem_wr_data(wdata128[0]), .mem_wr_sel(wsel16[0])
    );

    // ---------------- 内存模型：读 1 拍延迟；写当拍生效（字节使能）----------------
    always @(posedge clk) begin
        if (rden[0]) rdata[0] <= {mem[raddr[0]+7], mem[raddr[0]+6], mem[raddr[0]+5], mem[raddr[0]+4],
                                  mem[raddr[0]+3], mem[raddr[0]+2], mem[raddr[0]+1], mem[raddr[0]+0]};
        if (rden[1]) rdata[1] <= {mem[raddr[1]+7], mem[raddr[1]+6], mem[raddr[1]+5], mem[raddr[1]+4],
                                  mem[raddr[1]+3], mem[raddr[1]+2], mem[raddr[1]+1], mem[raddr[1]+0]};
        if (rden[2]) rdata[2] <= {mem[raddr[2]+7], mem[raddr[2]+6], mem[raddr[2]+5], mem[raddr[2]+4],
                                  mem[raddr[2]+3], mem[raddr[2]+2], mem[raddr[2]+1], mem[raddr[2]+0]};
        if (rden[3]) rdata[3] <= {mem[raddr[3]+7], mem[raddr[3]+6], mem[raddr[3]+5], mem[raddr[3]+4],
                                  mem[raddr[3]+3], mem[raddr[3]+2], mem[raddr[3]+1], mem[raddr[3]+0]};
    end
    always @(posedge clk) if (wren[0]) for (int b=0;b<8;b++)  if (wsel8[0][b])  mem[waddr[0]+b] <= wdata64[0][8*b +: 8];
    always @(posedge clk) if (wren[1]) for (int b=0;b<8;b++)  if (wsel8[1][b])  mem[waddr[1]+b] <= wdata64[1][8*b +: 8];
    always @(posedge clk) if (wren[2]) for (int b=0;b<8;b++)  if (wsel8[2][b])  mem[waddr[2]+b] <= wdata64[2][8*b +: 8];
    always @(posedge clk) if (wren[3]) for (int b=0;b<16;b++) if (wsel16[0][b]) mem[waddr[3]+b] <= wdata128[0][8*b +: 8];

    // ---------------- 写事务日志 ----------------
    int          tx_n  [0:3];
    logic [31:0] tx_off [0:3][0:MAXTX-1];   // 相对本次运行 base_out 的偏移
    logic [127:0] tx_dat[0:3][0:MAXTX-1];
    logic [15:0] tx_sel [0:3][0:MAXTX-1];
    logic [31:0] run_out[0:3];              // 本次运行的 base_out（比偏移用）
    localparam int MAXPASS = 4;
    logic [127:0] p_da [0:MAXPASS-1][0:MAXTX-1];
    logic [15:0]  p_se [0:MAXPASS-1][0:MAXTX-1];
    logic [31:0]  p_of [0:MAXPASS-1][0:MAXTX-1];
    int           p_n  [0:MAXPASS-1];
    int          n_x   [0:3];               // 写口出现 X
    int          n_mis [0:3];               // 写地址未按 VLANES 对齐

    always @(posedge clk) if (wren[0]) begin
        if ($isunknown({waddr[0], wdata64[0], wsel8[0]})) n_x[0]++;
        if (waddr[0][2:0] != 3'd0) n_mis[0]++;
        if (tx_n[0] < MAXTX) begin
            tx_off[0][tx_n[0]] = waddr[0] - run_out[0];
            tx_dat[0][tx_n[0]] = {64'd0, wdata64[0]};
            tx_sel[0][tx_n[0]] = {8'd0, wsel8[0]}; tx_n[0]++;
        end
    end
    always @(posedge clk) if (wren[1]) begin
        if ($isunknown({waddr[1], wdata64[1], wsel8[1]})) n_x[1]++;
        if (waddr[1][2:0] != 3'd0) n_mis[1]++;
        if (tx_n[1] < MAXTX) begin
            tx_off[1][tx_n[1]] = waddr[1] - run_out[1];
            tx_dat[1][tx_n[1]] = {64'd0, wdata64[1]};
            tx_sel[1][tx_n[1]] = {8'd0, wsel8[1]}; tx_n[1]++;
        end
    end
    always @(posedge clk) if (wren[2]) begin
        if ($isunknown({waddr[2], wdata64[2], wsel8[2]})) n_x[2]++;
        if (waddr[2][2:0] != 3'd0) n_mis[2]++;
        if (tx_n[2] < MAXTX) begin
            tx_off[2][tx_n[2]] = waddr[2] - run_out[2];
            tx_dat[2][tx_n[2]] = {64'd0, wdata64[2]};
            tx_sel[2][tx_n[2]] = {8'd0, wsel8[2]}; tx_n[2]++;
        end
    end
    always @(posedge clk) if (wren[3]) begin
        if ($isunknown({waddr[3], wdata128[0], wsel16[0]})) n_x[3]++;
        if (waddr[3][3:0] != 4'd0) n_mis[3]++;
        if (tx_n[3] < MAXTX) begin
            tx_off[3][tx_n[3]] = waddr[3] - run_out[3];
            tx_dat[3][tx_n[3]] = wdata128[0];
            tx_sel[3][tx_n[3]] = wsel16[0]; tx_n[3]++;
        end
    end

    // ---------------- 状态驻留计数（流水线利用率取证）----------------
    int s_prm[0:3], s_wld[0:3], s_cmp[0:3], s_drn[0:3], s_bub[0:3];
    always @(posedge clk) if (rst_n) begin
        case (u_a.state)
            3'd1: s_prm[1]++;
            3'd2: s_wld[1]++;
            3'd3: begin s_cmp[1]++; if (!rden[1]) s_bub[1]++; end
            3'd4: s_drn[1]++;
        endcase
        case (u_org.state)
            3'd1: s_prm[0]++;
            3'd2: s_wld[0]++;
            3'd3: begin s_cmp[0]++; if (!rden[0]) s_bub[0]++; end
            3'd4: s_drn[0]++;
        endcase
        case (u_c.state)
            3'd1: s_prm[3]++;
            3'd2: s_wld[3]++;
            3'd3: begin s_cmp[3]++; if (!rden[3]) s_bub[3]++; end
            3'd4: s_drn[3]++;
        endcase
    end

    // ---------------- 调试：状态跳变跟踪 ----------------
    logic [2:0] dbg_st0 = 3'd7, dbg_st1 = 3'd7, dbg_st3 = 3'd7;
    always @(posedge clk) if ($test$plusargs("DBG")) begin
        if (u_org.state !== dbg_st0) begin
            $display("  DBG t=%0t org.state %0d->%0d prm_i=%0d wl_l=%0d wl_lane=%0d b=%0d l=%0d o=%0d",
                     $time, dbg_st0, u_org.state, u_org.prm_i, u_org.wl_l, u_org.wl_lane,
                     u_org.b_q, u_org.l_q, u_org.o_q);
            dbg_st0 <= u_org.state;
        end
        if (u_a.state !== dbg_st1) begin
            $display("  DBG t=%0t n8.state %0d->%0d prm_i=%0d wl_l=%0d wl_lane=%0d b=%0d l=%0d o=%0d",
                     $time, dbg_st1, u_a.state, u_a.prm_i, u_a.wl_l, u_a.wl_lane,
                     u_a.b_q, u_a.l_q, u_a.o_q);
            dbg_st1 <= u_a.state;
        end
        if (u_c.state !== dbg_st3) begin
            $display("  DBG t=%0t n16.state %0d->%0d prm_i=%0d wl_l=%0d wl_lane=%0d b=%0d l=%0d o=%0d",
                     $time, dbg_st3, u_c.state, u_c.prm_i, u_c.wl_l, u_c.wl_lane,
                     u_c.b_q, u_c.l_q, u_c.o_q);
            dbg_st3 <= u_c.state;
        end
    end

    // ---------------- 补零双侧消隐结构检查（E3）+ 读端口利用计数 ----------------
    int n_actpad_bad = 0;   // 激活字补零位未强制 1
    int n_wgtpad_bad = 0;   // 权重字补零位未强制 0
    int n_rdx       = 0;    // 读地址出现 X
    int s_rdp [0:3], s_rdw[0:3], s_rdc[0:3];
    always @(posedge clk) if (rst_n) begin
        if (rden[0]) begin
            case (u_org.state) 3'd1: s_rdp[0]++; 3'd2: s_rdw[0]++; 3'd3: s_rdc[0]++; endcase
        end
        if (rden[1]) begin
            case (u_a.state) 3'd1: s_rdp[1]++; 3'd2: s_rdw[1]++; 3'd3: s_rdc[1]++; endcase
            if ($isunknown(raddr[1])) n_rdx++;
        end
        if (rden[3]) begin
            case (u_c.state) 3'd1: s_rdp[3]++; 3'd2: s_rdw[3]++; 3'd3: s_rdc[3]++; endcase
            if ($isunknown(raddr[3])) n_rdx++;
        end
    end
    // 末块（b == C_B-1）时：激活补零位必须为 1，权重补零位必须为 0
    always @(posedge clk) if (rst_n) begin : g_padchk
        logic [5:0]  cm;
        logic [63:0] msk;
        cm  = c_cin[3][5:0];
        msk = (cm == 6'd0) ? {64{1'b1}} : ((64'd1 << cm) - 64'd1);
        if (u_c.r1_vld && u_c.r1_bm1 && (cm != 6'd0)) begin
            if ((u_c.act_c & ~msk) !== ~msk) n_actpad_bad++;
        end
        if ((u_c.state == 3'd2) && u_c.wl_ph && (u_c.wl_b == u_c.cb_m1_q) && (cm != 6'd0)) begin
            if ((u_c.wdata_c & ~msk) !== 64'd0) n_wgtpad_bad++;
        end
    end

    // ---------------- 统计 ----------------
    int FAIL_TOTAL = 0;
    int CASES_OK   = 0;
    int S1_TX_TOT  = 0;
    int S2_LANE_TOT= 0;
    int S2_ADDR_BAD= 0;
    int MASK_ORG   = 0;      // v39 原版的末组掩码错误数（绝对值）
    int MASK_NEW   = 0;      // 新版 8-lane 的同项
    int MASK_N16   = 0;      // 新版 16-lane
    int OVERRUN    = 0;      // 16-lane 写出 oc>=cout 的非法字节
    int S1_BAD     = 0;      // S1 逐 bit 回归失败的 case 数
    int S2_BAD     = 0;      // S2 逐 lane 等价失败的 case 数

    // ---------------- 工具任务 ----------------
    int unsigned rnd_state;

    task automatic fill_pat(input int pat);
        // pat: 0=随机 1=全0 2=全1 3=单 bit
        rnd_state = rnd_state * 1103515245 + 12345;
        for (int i = IN_LO; i <= IN_HI; i++) begin
            rnd_state = rnd_state * 1103515245 + 12345;
            mem[i] = (pat == 1) ? 8'h00 : (pat == 2) ? 8'hFF :
                     (pat == 3) ? 8'h00 : rnd_state[7:0];
        end
        for (int i = PM_LO; i <= PM_HI; i++) begin
            rnd_state = rnd_state * 1103515245 + 12345;
            mem[i] = (pat == 1) ? 8'h00 : (pat == 2) ? 8'hFF :
                     (pat == 3) ? 8'h00 : rnd_state[7:0];
        end
        for (int i = OT_LO; i <= OT_HI; i++) mem[i] = 8'h5A;   // 输出平面标记
        if (pat == 3) mem[BASE_ACT + 32'h123] = 8'h01;         // 单个激活 bit
    endtask

    task automatic cfg_one(input int i, input int cin, input int cout, input int k,
                           input int s, input int d, input int pad, input int h,
                           input int w, input int sh, input int o0,
                           input int wofs_bytes, input int obase);
        c_cin[i]  = cin[15:0];  c_cout[i] = cout[15:0]; c_k[i]   = k[15:0];
        c_s[i]    = s[15:0];    c_d[i]    = d[15:0];    c_pad[i] = pad[15:0];
        c_h[i]    = h[15:0];    c_w[i]    = w[15:0];
        c_ash[i]  = 6'd9;       c_ssh[i]  = 6'd8;       c_sh[i]  = sh[5:0];
        b_act[i]  = BASE_ACT;   b_wgt[i]  = BASE_WGT + wofs_bytes;
        // 参数基址按 o0 lane 平移：alpha/slope int16(2B)，bias/zeta/gamma int32(4B)
        b_al[i]   = BASE_ALPHA + o0*2; b_sl[i] = BASE_SLOPE + o0*2;
        b_bi[i]   = BASE_BIAS  + o0*4; b_ze[i] = BASE_ZETA  + o0*4;
        b_ga[i]   = BASE_GAMMA + o0*4;
        b_out[i]  = obase;
        run_out[i]= obase;
    endtask

    task automatic clear_tx(input int i);
        tx_n[i] = 0; n_x[i] = 0; n_mis[i] = 0;
        s_prm[i] = 0; s_wld[i] = 0; s_cmp[i] = 0; s_drn[i] = 0; s_bub[i] = 0;
        s_rdp[i] = 0; s_rdw[i] = 0; s_rdc[i] = 0;
    endtask

    // done 是 1 拍脉冲：用粘滞标志接住，避免"顺序等两个 DUT"漏掉先到的脉冲
    logic done_seen [0:3];
    always @(posedge clk) begin
        for (int i = 0; i < 4; i++) if (done[i]) done_seen[i] <= 1'b1;
    end

    task automatic run_start(input int mask);
        @(negedge clk);
        for (int i = 0; i < 4; i++) if (mask[i]) begin done_seen[i] = 1'b0; start[i] = 1'b1; end
        @(negedge clk);
        for (int i = 0; i < 4; i++) if (mask[i]) start[i] = 1'b0;
        for (int i = 0; i < 4; i++) if (mask[i]) begin
            while (!done_seen[i]) @(negedge clk);
            @(negedge clk);
        end
    endtask

    // ---------------- S1 比较：org(0) vs new8(1) ----------------
    task automatic cmp_s1(input int cid, input int cout, input int npix, output int bad);
        int g, expm;
        bad = 0;
        if (tx_n[0] != tx_n[1]) begin
            $display("  [S1 cid=%0d] TX COUNT MISMATCH org=%0d new=%0d", cid, tx_n[0], tx_n[1]);
            bad = 1;
        end
        for (int i = 0; i < tx_n[0] && i < tx_n[1]; i++) begin
            if (tx_off[0][i] !== tx_off[1][i]) begin
                if (bad < 8) $display("  [S1 cid=%0d] tx%0d ADDR exp=%0h act=%0h",
                                      cid, i, tx_off[0][i], tx_off[1][i]);
                bad = 1;
            end
            if (tx_dat[0][i][63:0] !== tx_dat[1][i][63:0]) begin
                if (bad < 8) $display("  [S1 cid=%0d] tx%0d DATA exp=%016h act=%016h",
                                      cid, i, tx_dat[0][i][63:0], tx_dat[1][i][63:0]);
                bad = 1;
            end
            if (tx_sel[0][i][7:0] !== tx_sel[1][i][7:0]) begin
                if (bad < 8) $display("  [S1 cid=%0d] tx%0d SEL exp=%02h act=%02h",
                                      cid, i, tx_sel[0][i][7:0], tx_sel[1][i][7:0]);
                bad = 1;
            end
        end
        // 绝对值检查（与基准无关）：最后一组的字节掩码应当 = (1<<(cout-g*8))-1
        for (int i = 0; i < tx_n[0] && i < tx_n[1]; i++) begin
            g = i / npix;
            if (cout - g*8 >= 8) expm = 8'hFF;
            else expm = (1 << (cout - g*8)) - 1;
            if (tx_sel[0][i][7:0] !== expm[7:0]) begin
                if (MASK_ORG <= 3)
                    $display("  [S1 cid=%0d] MASK(v39) tx%0d g=%0d got=%02h exp=%02h",
                             cid, i, g, tx_sel[0][i][7:0], expm[7:0]);
                MASK_ORG++;
            end
            if (tx_sel[1][i][7:0] !== expm[7:0]) begin
                if (MASK_NEW <= 3)
                    $display("  [S1 cid=%0d] MASK(new8) tx%0d g=%0d got=%02h exp=%02h",
                             cid, i, g, tx_sel[1][i][7:0], expm[7:0]);
                MASK_NEW++;
            end
        end
        S1_TX_TOT += tx_n[0];
        if (n_x[0] || n_x[1]) begin
            $display("  [S1 cid=%0d] X ON WRITE PORT org=%0d new=%0d", cid, n_x[0], n_x[1]);
            bad = 1;
        end
        if (n_mis[0] || n_mis[1])
            $display("  [S1 cid=%0d] WARN misaligned wr addr org=%0d new=%0d", cid, n_mis[0], n_mis[1]);
    endtask

    // ---------------- S2 比较：16-lane(3) vs passes(每趟 8-lane, 存 pass 数组) ----------------
    task automatic cmp_s2(input int cid, input int cout, input int npix, output int bad);
        int expm16;
        int npass, ngroup, g, pix, lane, oc, p_, l_, lane_bad, addr_bad;
        bad = 0; lane_bad = 0; addr_bad = 0;
        npass  = (cout + 7) / 8;
        ngroup = (cout + 15) / 16;
        // ① 每组像素数 / 事务数
        if (tx_n[3] != ngroup * npix) begin
            $display("  [S2 cid=%0d] TX COUNT exp=%0d act=%0d", cid, ngroup*npix, tx_n[3]);
            bad = 1;
        end
        // ② 逐 lane 数据 + 使能（按 tx 序：g = t/npix, pixel = t%npix）
        for (int t = 0; t < tx_n[3]; t++) begin
            g   = t / npix;
            pix = t % npix;
            for (lane = 0; lane < 16; lane++) begin
                oc = 16*g + lane;
                if (oc < cout) begin
                    p_  = oc / 8;  l_ = oc % 8;
                    if (p_ >= npass) continue;
                    if (pix >= p_n[p_]) continue;
                    if (tx_dat[3][t][8*lane +: 8] !== p_da[p_][pix][8*l_ +: 8]) begin
                        if (lane_bad < 6)
                            $display("  [S2 cid=%0d] t=%0d g=%0d pix=%0d lane=%0d (oc=%0d) DATA exp=%02h act=%02h",
                                     cid, t, g, pix, lane, oc,
                                     p_da[p_][pix][8*l_ +: 8], tx_dat[3][t][8*lane +: 8]);
                        lane_bad++;
                    end
                    if (tx_sel[3][t][lane] !== p_se[p_][pix][l_]) begin
                        if (lane_bad < 6)
                            $display("  [S2 cid=%0d] t=%0d g=%0d pix=%0d lane=%0d (oc=%0d) SEL exp=%0b act=%0b",
                                     cid, t, g, pix, lane, oc, p_se[p_][pix][l_], tx_sel[3][t][lane]);
                        lane_bad++;
                    end
                    S2_LANE_TOT++;
                end else if (tx_sel[3][t][lane] !== 1'b0) begin
                    if (OVERRUN < 6)
                        $display("  [S2 cid=%0d] OVERRUN t=%0d g=%0d pix=%0d lane=%0d (oc=%0d >= cout=%0d) 写了非法字节 byte=%02h",
                                 cid, t, g, pix, lane, oc, cout, tx_dat[3][t][8*lane +: 8]);
                    OVERRUN++;
                end
            end
            // ③ 16-lane 末组掩码绝对值检查
            if (cout - g*16 >= 16) begin
                if (tx_sel[3][t] !== 16'hFFFF) MASK_N16++;
            end else if (tx_sel[3][t] !== ((16'd1 << (cout - g*16)) - 16'd1)) MASK_N16++;
            // ④ 地址：16-lane 的平面步长应为 npix*16 字节
            if (tx_off[3][t] !== g*npix*16 + pix*16) begin
                if (addr_bad < 4)
                    $display("  [S2 cid=%0d] t=%0d ADDR exp=%0h act=%0h (group=%0d pix=%0d)",
                             cid, t, g*npix*16 + pix*16, tx_off[3][t], g, pix);
                addr_bad++;
            end
        end
        if (lane_bad) begin
            $display("  [S2 cid=%0d] LANE DIFF = %0d", cid, lane_bad); bad = 1;
        end
        if (addr_bad) begin
            $display("  [S2 cid=%0d] ADDR DIFF = %0d / %0d  (组平面步长错，见报告 F1)",
                     cid, addr_bad, tx_n[3]);
            S2_ADDR_BAD += addr_bad;
        end
        if (n_x[3]) begin
            $display("  [S2 cid=%0d] X ON WRITE PORT n16=%0d", cid, n_x[3]); bad = 1;
        end
        if (n_mis[3])
            $display("  [S2 cid=%0d] WARN misaligned wr addr n16=%0d", cid, n_mis[3]);
    endtask

    // ---------------- 单 case：S1 一趟 + S2 多趟 ----------------
    task automatic one_case(input int cid, input int cin, input int cout, input int k,
                            input int s, input int d, input int pad, input int h,
                            input int w, input int sh, input int pat);
        int hout, wout, npix, cb, K, npass, p, b1, b2, c8, wofs, ob;
        int s1_tx, s1_bub, s1_cmp, s1_wld, s1_prm;
        hout = (h - 1) / s + 1;
        wout = (w - 1) / s + 1;
        npix = hout * wout;
        cb   = (cin + 63) / 64;
        K    = cb * k * k;
        fill_pat(pat);

        // ===== S1：org vs new(VLANES=8) =====
        for (int i = 0; i < 4; i++) clear_tx(i);
        cfg_one(0, cin, cout, k, s, d, pad, h, w, sh, 0, 0, OUT0);
        cfg_one(1, cin, cout, k, s, d, pad, h, w, sh, 0, 0, OUT1);
        run_start(4'b0011);
        cmp_s1(cid, cout, npix, b1);
        s1_tx  = tx_n[0];
        s1_bub = s_bub[0];
        s1_cmp = s_cmp[0];
        s1_wld = s_wld[0];
        s1_prm = s_prm[0];

        // ===== S2：new(VLANES=16) 一次 vs ceil(cout/8) 趟 new(VLANES=8) =====
        npass = (cout + 7) / 8;
        for (p = 0; p < npass; p++) begin
            c8   = (cout - 8*p > 8) ? 8 : (cout - 8*p);
            wofs = 8 * p * K * 8;
            ob   = OUT1 + p * 32'h0001_0000;
            clear_tx(1);
            cfg_one(1, cin, c8, k, s, d, pad, h, w, sh, 8*p, wofs, ob);
            run_start(4'b0010);
            for (int t = 0; t < tx_n[1]; t++) begin
                p_da[p][t] = tx_dat[1][t]; p_se[p][t] = tx_sel[1][t]; p_of[p][t] = tx_off[1][t];
            end
            p_n[p] = tx_n[1];
        end
        clear_tx(3);
        cfg_one(3, cin, cout, k, s, d, pad, h, w, sh, 0, 0, OUT3);
        run_start(4'b1000);
        cmp_s2(cid, cout, npix, b2);

        if (!b1) CASES_OK++; else S1_BAD++;
        if (b2) S2_BAD++;
        $display("CASE %2d  cin=%3d cout=%3d k=%0d s=%0d d=%0d pad=%0d h=%0d w=%0d sh=%0d pat=%0d | S1=%s(%0d tx, bub=%0d) S2=%s(%0d tx, %0d pass, bub=%0d)",
                 cid, cin, cout, k, s, d, pad, h, w, sh, pat,
                 b1 ? "FAIL" : "ok", s1_tx, s1_bub,
                 b2 ? "FAIL" : "ok", tx_n[3], npass, s_bub[3]);
        $display("        STAT8  prm=%0d(rd%0d) wld=%0d(rd%0d) cmp=%0d(rd%0d) drn=%0d oob=%0d | STAT16 prm=%0d(rd%0d) wld=%0d(rd%0d) cmp=%0d(rd%0d) drn=%0d oob=%0d",
                 s_prm[0], s_rdp[0], s_wld[0], s_rdw[0], s_cmp[0], s_rdc[0], s_drn[0], s_bub[0],
                 s_prm[3], s_rdp[3], s_wld[3], s_rdw[3], s_cmp[3], s_rdc[3], s_drn[3], s_bub[3]);
    endtask

    // ---------------- 主流程 ----------------
    int ci, cin, cout, k, s, d, pad, h, w, sh, ncase, cid;
    int cout_tab [0:7] = '{8, 16, 13, 32, 3, 17, 24, 5};

    initial begin
        #200_000_000;
        $display("TIMEOUT: sim > 200 ms");
        $finish;
    end

    initial begin
        if ($test$plusargs("DUMP")) begin
            $dumpfile("v50_simA.wdb");
            $dumpvars(0, u_org);
            $dumpvars(0, u_a);
            $dumpvars(0, u_b);
            $dumpvars(0, u_c);
            $dumpvars(0, clk, rst_n, start[0], done[0], start[1], done[1],
                      start[3], done[3], wren[0], waddr[0], wdata64[0], wsel8[0],
                      wren[3], waddr[3], wdata128[0], wsel16[0]);
        end
        ncase = 24;
        if ($value$plusargs("NCASE=%d", ncase)) ;
        rnd_state = 32'h1234_5678;
        for (int i = 0; i < 4; i++) begin
            start[i] = 1'b0; clear_tx(i);
            c_ash[i] = 6'd9; c_ssh[i] = 6'd8;
        end
        rst_n = 1'b0;
        repeat (6) @(negedge clk);
        rst_n = 1'b1;
        repeat (3) @(negedge clk);

        $display("=== v50_simA: IMP-A bin_conv_core.v 回归/等价性 (xsim) ===");
        cid = 0;
        // 20 组随机
        for (ci = 0; ci < 20 && cid < ncase; ci++) begin
            rnd_state = rnd_state * 1103515245 + 12345;
            cin  = 64 * (1 + (rnd_state[7:0] % 3));
            if (rnd_state[8]) cin = cin - 1 - (rnd_state[11:9] % 9);   // 非 64 倍数 ⇒ 末块掩码
            rnd_state = rnd_state * 1103515245 + 12345;
            k    = rnd_state[0] ? 3 : 1;
            s    = 1 + rnd_state[1];
            d    = 1 + rnd_state[2];
            pad  = rnd_state[3];
            h    = 4 + (rnd_state[10:8] % 5);
            w    = 4 + (rnd_state[13:11] % 5);
            sh   = rnd_state[16:14];
            cout = cout_tab[rnd_state[19:17] % 8];
            one_case(cid, cin, cout, k, s, d, pad, h, w, sh, 0);
            cid++;
        end
        // 4 组边界：全 0 / 全 1 / 单 bit / cout 非 8 倍数
        if (cid < ncase) begin one_case(cid, 64, 16, 3, 1, 1, 1, 6, 6, 3, 1); cid++; end
        if (cid < ncase) begin one_case(cid, 64, 16, 3, 1, 1, 1, 6, 6, 3, 2); cid++; end
        if (cid < ncase) begin one_case(cid, 64, 16, 3, 1, 1, 1, 6, 6, 3, 3); cid++; end
        if (cid < ncase) begin one_case(cid, 100, 13, 3, 1, 1, 1, 7, 5, 4, 0); cid++; end

        $display("");
        $display("=== SUMMARY ===");
        $display("cases                  : %0d", cid);
        $display("S1 org vs new8         : %0d/%0d cases 0-diff (addr+data+sel), %0d write-tx",
                 cid - S1_BAD, cid, S1_TX_TOT);
        $display("S2 n16 vs ceil(c/8)x8  : %0d/%0d cases lane-0-diff, %0d lane compared",
                 cid - S2_BAD, cid, S2_LANE_TOT);
        $display("F1 plane-stride (addr) : %0d addr-diff", S2_ADDR_BAD);
        $display("F2 last-group mask     : v39=%0d  new8=%0d  n16=%0d (mask errors)",
                 MASK_ORG, MASK_NEW, MASK_N16);
        $display("F2 overrun bytes (n16) : %0d", OVERRUN);
        $display("E3 padding mask        : act-pad-bad=%0d  wgt-pad-bad=%0d  rd-addr-X=%0d",
                 n_actpad_bad, n_wgtpad_bad, n_rdx);
        $display("VERDICT                : %s",
                 (S1_BAD == 0 && S2_BAD == 0) ? "S1/S2 EQUIV PASS" : "EQUIV FAIL");
        $finish;
    end

endmodule
