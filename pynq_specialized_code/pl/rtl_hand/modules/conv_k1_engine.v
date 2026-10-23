// ============================================================================
//  conv_k1_engine.v —— 手写 1×1 卷积加速器 IP（k=1, stride=1, pad=0, dil=1）
//
//  契约：docs/rtl_hand/00_SPEC_接口契约.md §3（端口名/方向严格一致）
//  结构来源：docs/rtl_map/04_mac_array.md（k=1 HLS 现状：16 车道 + 21 拍收尾）
//            docs/rtl_map/03_weight_path.md（重装 = 每 (y,列块,ocb) 一次）
//
//  设计目标（针对 132 个 k=1 层里占比最大的 64×64 形状）：
//    1. 32 个 oc 车道并行（8×8 乘法器 ×32，映射 DSP ≤ 60 的红线内）；
//    2. 收尾完全并行、3 拍流水：acc0+acc1 → +bias → >>>shift/ReLU/sat8；
//    3. 权重整层驻留 + 双 bank（ping-pong）：每层只从 DDR 装一次
//       （64×64 = 4 KB），彻底消掉 HLS 的 237 MB 重装；
//    4. 输入按 C8 布局整 8 字节读，双/四缓冲预取把 DDR 延迟藏进计算；
//    5. 每个 (pixel, 32-oc) 块 = cin 拍（II=1），收尾与下一块重叠，
//       稳态每像素每 oc 块恰好 cin 拍。
//
//  关键不变量（仿真/综合都必须成立）：
//    · 计算内核每拍消费 1 个 ic（issue_en=1 时），32 个 8×8 乘 + 32 个 26 位加
//      同拍发生；bank 号 = generate 常量，权重地址 = {bank, ic>>3}；
//    · 累加器 26 位有符号，奇偶双链（每条链 2 拍循环依赖）；
//    · 收尾 E1: ep_acc_e+ep_acc_o；E2: +bias；E3: >>>shift/ReLU/sat8 → 写队列；
//    · 权重/偏置各用独立 AXI4 读口，装载与计算重叠（bank1 在 pass0 期间装）；
//    · 输出按 C8 分组（每 oc 组 8 字节）单拍写，经 4 块深写队列排空。
//
//  参数：
//    CIN/COUT/H/W/PAD/STRIDE/DIL/RELU 与契约 §3 同名；第一版**运行时形状**
//    由 ctrl_* 给出（≤ 下面的 CIN_MAX/COUT_MAX），编译期参数是上界/默认值。
//    固定形状（cin=64,cout=64,H=W=320）直接用即可；H/W 走 ctrl 是为了能用
//    黄金向量 k1_64x64（H=2,W=320，来自 /data/work/tb_c8_k1_64x64）。
//
//  已知限制（详见 docs/rtl_hand/conv_k1_engine.md）：
//    · 要求 cin % 8 == 0（权重按 8 字节整字装；非 8 倍数的 cin 未实现）；
//    · cin ≥ 8（收尾 3 拍与下一块的流水重叠需要块长 > 3）；
//    · cout ≤ COUT_MAX = 64（两个 32-lane bank）；RELU 是编译期参数；
//    · 不支持 stride≠1 / pad≠0 / dil≠1（k=1 不需要）。
// ============================================================================
`timescale 1ns / 1ps

module conv_k1_engine #(
    parameter int CIN      = 64,
    parameter int COUT     = 64,
    parameter int W        = 320,
    parameter int H        = 320,
    parameter int PAD      = 0,
    parameter int STRIDE   = 1,
    parameter int DIL      = 1,
    parameter bit RELU     = 1'b1,

    // ---- 实现参数（本版固定）----
    parameter int LANES    = 32,   // oc 车道数（= 每块 oc 数）
    parameter int CIN_MAX  = 64,   // 运行期 cin 上界；权重每 lane 深度 = 2*CIN_MAX/8
    parameter int COUT_MAX = 64,   // 运行期 cout 上界；bank 数 = COUT_MAX/LANES
    parameter int NBUF     = 4,    // 输入像素缓冲数（预取深度）
    parameter int OBUF_N   = 4     // 输出块队列深度（每块 4 个 oc 组）
)(
    input  logic        ap_clk,
    input  logic        ap_rst_n,

    // ---- AXI4 master 读：输入（C8，64-bit 数据宽）----
    output logic        m_axi_in_arvalid,  input logic m_axi_in_arready,
    output logic [31:0] m_axi_in_araddr,   output logic [7:0]  m_axi_in_arlen,
    input  logic        m_axi_in_rvalid,   output logic m_axi_in_rready,
    input  logic [63:0] m_axi_in_rdata,    input logic m_axi_in_rlast,

    // ---- AXI4 master 读：权重 ----
    output logic        m_axi_w_arvalid,   input logic m_axi_w_arready,
    output logic [31:0] m_axi_w_araddr,    output logic [7:0]  m_axi_w_arlen,
    input  logic        m_axi_w_rvalid,    output logic m_axi_w_rready,
    input  logic [63:0] m_axi_w_rdata,     input logic m_axi_w_rlast,

    // ---- AXI4 master 读：偏置 ----
    output logic        m_axi_b_arvalid,   input logic m_axi_b_arready,
    output logic [31:0] m_axi_b_araddr,    output logic [7:0]  m_axi_b_arlen,
    input  logic        m_axi_b_rvalid,    output logic m_axi_b_rready,
    input  logic [63:0] m_axi_b_rdata,     input logic m_axi_b_rlast,

    // ---- AXI4 master 写：输出（C8，64-bit 数据宽）----
    output logic        m_axi_out_awvalid, input logic m_axi_out_awready,
    output logic [31:0] m_axi_out_awaddr,  output logic [7:0]  m_axi_out_awlen,
    output logic        m_axi_out_wvalid,  input logic m_axi_out_wready,
    output logic [63:0] m_axi_out_wdata,   output logic [7:0]  m_axi_out_wstrb,
    output logic        m_axi_out_wlast,
    input  logic        m_axi_out_bvalid,  output logic m_axi_out_bready,

    // ---- 控制寄存器（AXI-Lite 侧由 BD 分配，这里只列语义）----
    input  logic [31:0] ctrl_in_addr,
    input  logic [31:0] ctrl_wgt_addr,
    input  logic [31:0] ctrl_bias_addr,
    input  logic [31:0] ctrl_out_addr,
    input  logic [31:0] ctrl_shift,
    input  logic [31:0] ctrl_cin,       // 运行时通道数
    input  logic [31:0] ctrl_cout,
    input  logic [31:0] ctrl_h,
    input  logic [31:0] ctrl_w,
    input  logic        ctrl_start,
    output logic        ctrl_done,
    output logic        ctrl_busy
);

    // ========================================================================
    // 0. 常量
    // ========================================================================
    localparam int C8      = 8;
    localparam int GMAX    = CIN_MAX / C8;              // 每 lane 每组 8 通道 → 组数
    localparam int WDEPTH  = 2 * GMAX;                  // ping-pong bank × 组
    localparam int BLKMAX  = (COUT_MAX + LANES - 1) / LANES;
    localparam int NCGMAX  = (COUT_MAX + 7) / 8;        // 输出 8 字节组数
    localparam int NBUFW   = $clog2(NBUF);
    localparam int OBUFW   = $clog2(OBUF_N);

    // ========================================================================
    // 1. 配置锁存 / 派生
    // ========================================================================
    logic [31:0] in_base_r, wgt_base_r, bias_base_r, out_base_r;
    logic [6:0]  cin_r, cout_r;
    logic [4:0]  shift_r;
    logic [31:0] pix_total_r, hw8_r;
    logic [3:0]  g_count_r;          // cin/8，1..8
    logic [1:0]  blk_cnt_r;          // ceil(cout/32)
    logic [3:0]  ncg_r;              // ceil(cout/8)
    logic [1:0]  blk_r;              // 当前 oc 块

    wire [31:0] hw_mul = {16'd0, ctrl_h[15:0]} * {16'd0, ctrl_w[15:0]};

    // ========================================================================
    // 2. 主 FSM
    // ========================================================================
    localparam logic [2:0] ST_IDLE  = 3'd0,
                           ST_LOAD  = 3'd1,
                           ST_PASS  = 3'd2,
                           ST_NEXT  = 3'd3,
                           ST_DRAIN = 3'd4,
                           ST_DONE  = 3'd5;

    logic [2:0] state;
    logic       start_q, start_pulse;
    logic [1:0] bank_ready;
    logic       bias_ready;

    // 前向声明（主 FSM / 取数 / 收尾 / 写队列互有引用）
    logic         f_busy;
    logic [31:0]  f_pix, f_next, f_araddr;
    logic [3:0]   f_ar, f_rd;
    logic [7:0]   b_cnt;
    logic [2:0]   ob_cnt;
    logic         push_vld;
    logic [511:0] px_sh [0:NBUF-1];

    assign ctrl_busy   = (state != ST_IDLE);
    assign start_pulse = ctrl_start & ~start_q;

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) start_q <= 1'b0;
        else           start_q <= ctrl_start;
    end

    // 计算/取数核心与收尾的状态
    logic [31:0] pix_cur, pix_off;
    logic [6:0]  ic_cur;
    logic        pass_done;
    logic [2:0]  pend;               // 已发射但收尾还没入队的块数
    logic [1:0]  cur_buf;

    assign cur_buf = pix_cur[1:0];

    logic pass_go;
    assign pass_go = ((state == ST_LOAD) && bank_ready[0] && bias_ready) ||
                     ((state == ST_NEXT) && bank_ready[blk_r] && bias_ready);

    // 输入缓冲就绪 / 输出队列余量
    logic [NBUF-1:0] pix_ready;
    wire blk_room = (ob_cnt <= (OBUF_N - 2));

    logic issue_en;
    assign issue_en = (state == ST_PASS) && !pass_done &&
                      ((ic_cur != 7'd0) ||
                       (pix_ready[cur_buf] && blk_room));

    // 块开始 / 收尾入队脉冲
    wire block_start = issue_en && (ic_cur == 7'd0);
    wire push_ok     = push_vld && (ob_cnt < OBUF_N);

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            state       <= ST_IDLE;
            ctrl_done   <= 1'b0;
            blk_r       <= 2'd0;
            in_base_r   <= 32'd0;
            wgt_base_r  <= 32'd0;
            bias_base_r <= 32'd0;
            out_base_r  <= 32'd0;
            cin_r       <= 7'd0;
            cout_r      <= 7'd0;
            shift_r     <= 5'd0;
            pix_total_r <= 32'd0;
            hw8_r       <= 32'd0;
            g_count_r   <= 4'd0;
            blk_cnt_r   <= 2'd1;
            ncg_r       <= 4'd1;
        end else begin
            ctrl_done <= 1'b0;
            case (state)
                ST_IDLE: begin
                    if (start_pulse) begin
                        in_base_r   <= ctrl_in_addr;
                        wgt_base_r  <= ctrl_wgt_addr;
                        bias_base_r <= ctrl_bias_addr;
                        out_base_r  <= ctrl_out_addr;
                        cin_r       <= ctrl_cin[6:0];
                        cout_r      <= ctrl_cout[6:0];
                        shift_r     <= ctrl_shift[4:0];
                        pix_total_r <= hw_mul;
                        hw8_r       <= hw_mul << 3;
                        g_count_r   <= ctrl_cin[6:3];
                        blk_cnt_r   <= (ctrl_cout[6:0] + 7'd31) >> 5;
                        ncg_r       <= (ctrl_cout[6:0] + 7'd7) >> 3;
                        blk_r       <= 2'd0;
                        state       <= ST_LOAD;
                    end
                end
                ST_LOAD: if (pass_go) state <= ST_PASS;
                ST_PASS: begin
                    if (pass_done && (pend == 3'd0) && !f_busy &&
                        (f_next == pix_total_r)) begin
                        if ((blk_r + 2'd1) < blk_cnt_r) begin
                            blk_r <= blk_r + 2'd1;
                            state <= ST_NEXT;
                        end else begin
                            state <= ST_DRAIN;
                        end
                    end
                end
                ST_NEXT: if (pass_go) state <= ST_PASS;
                ST_DRAIN: if ((ob_cnt == 3'd0) && (b_cnt == 8'd0)) state <= ST_DONE;
                ST_DONE: begin
                    ctrl_done <= 1'b1;
                    state     <= ST_IDLE;
                end
                default: state <= ST_IDLE;
            endcase
        end
    end

    // ---- 计算像素/ic 指针 ----
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            pix_cur   <= 32'd0;
            pix_off   <= 32'd0;
            ic_cur    <= 7'd0;
            pass_done <= 1'b0;
        end else if (start_pulse || pass_go) begin
            pix_cur   <= 32'd0;
            pix_off   <= 32'd0;
            ic_cur    <= 7'd0;
            pass_done <= 1'b0;
        end else if (issue_en) begin
            if (ic_cur == (cin_r - 7'd1)) begin
                pix_ready[cur_buf] <= 1'b0;          // 本像素数据已消费完
                ic_cur             <= 7'd0;
                if (pix_cur == (pix_total_r - 32'd1)) begin
                    pass_done <= 1'b1;
                end else begin
                    pix_cur <= pix_cur + 32'd1;
                    pix_off <= pix_off + 32'd8;
                end
            end else begin
                ic_cur <= ic_cur + 7'd1;
            end
        end
    end

    // ---- 在飞块计数 ----
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n)                          pend <= 3'd0;
        else if (start_pulse || pass_go)        pend <= 3'd0;
        else                                    pend <= pend + block_start - push_ok;
    end

    // ========================================================================
    // 3. AXI4 读：输入像素预取（每像素 g_count 笔 1-beat，C8 组步长 HW*8）
    // ========================================================================
    wire         in_ar_hs = m_axi_in_arvalid && m_axi_in_arready;
    wire         in_r_hs  = m_axi_in_rvalid  && m_axi_in_rready;

    assign m_axi_in_arvalid = f_busy && (f_ar < g_count_r);
    assign m_axi_in_araddr  = f_araddr;
    assign m_axi_in_arlen   = 8'd0;
    assign m_axi_in_rready  = f_busy;

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            f_busy   <= 1'b0;
            f_pix    <= 32'd0;
            f_next   <= 32'd0;
            f_araddr <= 32'd0;
            f_ar     <= 4'd0;
            f_rd     <= 4'd0;
            pix_ready <= '0;
        end else if (start_pulse || pass_go) begin
            f_busy    <= 1'b0;
            f_next    <= 32'd0;
            f_ar      <= 4'd0;
            f_rd      <= 4'd0;
            pix_ready <= '0;
        end else if (!f_busy) begin
            if ((state == ST_PASS) && (f_next < pix_total_r) &&
                ((f_next - pix_cur) < NBUF[31:0])) begin
                f_busy   <= 1'b1;
                f_pix    <= f_next;
                f_ar     <= 4'd0;
                f_rd     <= 4'd0;
                f_araddr <= in_base_r + (f_next << 3);
            end
        end else begin
            if (in_ar_hs) begin
                f_ar     <= f_ar + 4'd1;
                f_araddr <= f_araddr + hw8_r;
            end
            if (in_r_hs) begin
                case (f_pix[1:0])
                    2'd0: px_sh[0][{f_rd, 6'b000000} +: 64] <= m_axi_in_rdata;
                    2'd1: px_sh[1][{f_rd, 6'b000000} +: 64] <= m_axi_in_rdata;
                    2'd2: px_sh[2][{f_rd, 6'b000000} +: 64] <= m_axi_in_rdata;
                    default: px_sh[3][{f_rd, 6'b000000} +: 64] <= m_axi_in_rdata;
                endcase
                f_rd <= f_rd + 4'd1;
            end
            if (((f_ar + {3'd0, in_ar_hs}) == g_count_r) &&
                ((f_rd + {3'd0, in_r_hs}) == g_count_r)) begin
                f_busy                <= 1'b0;
                pix_ready[f_pix[1:0]] <= 1'b1;
                f_next                <= f_pix + 32'd1;
            end
        end
    end

    // ========================================================================
    // 4. 输入像素缓冲（每像素 8 组 × 64 bit = 512 bit 移位寄存器）
    //    计算时每拍右移 8 bit，最低字节即当前 ic 的激活值（C8：组内 8 通道连续）
    // ========================================================================
    logic [7:0] p_byte;
    always_comb begin
        case (cur_buf)
            2'd0: p_byte = px_sh[0][7:0];
            2'd1: p_byte = px_sh[1][7:0];
            2'd2: p_byte = px_sh[2][7:0];
            default: p_byte = px_sh[3][7:0];
        endcase
    end

    always_ff @(posedge ap_clk) begin
        if (issue_en && (cur_buf == 2'd0)) px_sh[0] <= {8'h00, px_sh[0][511:8]};
        if (issue_en && (cur_buf == 2'd1)) px_sh[1] <= {8'h00, px_sh[1][511:8]};
        if (issue_en && (cur_buf == 2'd2)) px_sh[2] <= {8'h00, px_sh[2][511:8]};
        if (issue_en && (cur_buf == 2'd3)) px_sh[3] <= {8'h00, px_sh[3][511:8]};
    end

    // ========================================================================
    // 5. 计算流水（3 级，II=1）
    //    A: 取 p / 读权重 word（BRAM/LUTRAM 同步读）/ 记 parity、last、pix
    //    B: 8×8 乘（32 路同拍），按 sel_a 从 64-bit word 里选字节
    //    C: 26 位奇偶双链累加；last_b 时快照进收尾寄存器并清 0
    // ========================================================================
    logic signed [7:0] p_a;
    logic [2:0]        sel_a;
    logic              par_a, par_b;
    logic              last_a, last_b;
    logic              v_a, v_b;
    logic [31:0]       pixoff_a, pixoff_b;
    logic [1:0]        blk_a, blk_b;

    logic [63:0]       w_word_q [0:LANES-1];
    logic signed [15:0] prod_b  [0:LANES-1];
    logic signed [25:0] acc_e   [0:LANES-1];
    logic signed [25:0] acc_o   [0:LANES-1];
    logic signed [25:0] ep_e    [0:LANES-1];
    logic signed [25:0] ep_o    [0:LANES-1];
    logic signed [26:0] sum27   [0:LANES-1];
    logic signed [31:0] v32     [0:LANES-1];
    logic [7:0]         sat_b   [0:LANES-1];
    logic signed [31:0] bias0   [0:LANES-1];
    logic signed [31:0] bias1   [0:LANES-1];

    // ---- stage A/B metadata ----
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            p_a <= 8'sd0; sel_a <= 3'd0;
            par_a <= 1'b0; par_b <= 1'b0;
            last_a <= 1'b0; last_b <= 1'b0;
            v_a <= 1'b0; v_b <= 1'b0;
            pixoff_a <= 32'd0; pixoff_b <= 32'd0;
            blk_a <= 2'd0; blk_b <= 2'd0;
        end else if (start_pulse || pass_go) begin
            p_a <= 8'sd0; sel_a <= 3'd0;
            par_a <= 1'b0; par_b <= 1'b0;
            last_a <= 1'b0; last_b <= 1'b0;
            v_a <= 1'b0; v_b <= 1'b0;
        end else begin
            v_a <= issue_en;
            if (issue_en) begin
                p_a      <= p_byte;
                sel_a    <= ic_cur[2:0];
                par_a    <= ic_cur[0];
                last_a   <= (ic_cur == (cin_r - 7'd1));
                pixoff_a <= pix_off;
                blk_a    <= blk_r;
            end else begin
                // 关键：块边界若输入没就绪（issue 停顿），last_a 必须清 0；
                // 否则 last_b 会在停顿期间保持 1，下一块第一个乘积会被误判成
                // “最后拍”而触发一次假快照/清 0。
                last_a   <= 1'b0;
            end
            v_b      <= v_a;
            par_b    <= par_a;
            last_b   <= last_a;
            pixoff_b <= pixoff_a;
            blk_b    <= blk_a;
        end
    end

    // ========================================================================
    // 6. 权重 bank（每 lane 一个 16×64bit ram：{bank, ic>>3}）+ 累加器
    //    bank 号 = generate 常量 ⇒ 32 个独立 RAM，无运行期 bank 选择 mux
    // ========================================================================
    logic        wl_we;
    logic [63:0] wl_din;          // 权重 beat 是 64 位（每 oc 的 8 个 ic 一组）
    logic [4:0]  wl_we_lane;
    logic [3:0]  wl_we_word;
    logic [1:0]  wl_we_blk;

    logic        bl_weA, bl_weB;
    logic [6:0]  bl_idxA, bl_idxB;
    logic signed [31:0] bl_dataA, bl_dataB;

    logic e1_vld, e2_vld, e3_vld;
    logic [1:0]  ep_blk_q;
    logic [31:0] ep_gbase;

    genvar gi;
    generate
        for (gi = 0; gi < LANES; gi = gi + 1) begin : g_lane
            (* ram_style = "distributed" *) reg [63:0] wmem [0:WDEPTH-1];

            always_ff @(posedge ap_clk) begin
                // 装载写口（bank 号常量 = gi）
                if (wl_we && (wl_we_lane == gi[4:0]))
                    wmem[{wl_we_blk, wl_we_word[2:0]}] <= wl_din;
                // 计算读口：{当前 oc bank, ic>>3}
                if (issue_en)
                    w_word_q[gi] <= wmem[{blk_r, ic_cur[5:3]}];
            end

            // 偏置（每 lane 两个 bank 的寄存器，无读地址 mux）
            always_ff @(posedge ap_clk or negedge ap_rst_n) begin
                if (!ap_rst_n) begin
                    bias0[gi] <= 32'sd0;
                    bias1[gi] <= 32'sd0;
                end else begin
                    if (bl_weA && (bl_idxA == gi[6:0]))    bias0[gi] <= bl_dataA;
                    if (bl_weB && (bl_idxB == gi[6:0]))    bias0[gi] <= bl_dataB;
                    if (bl_weA && (bl_idxA == (gi + 32)))  bias1[gi] <= bl_dataA;
                    if (bl_weB && (bl_idxB == (gi + 32)))  bias1[gi] <= bl_dataB;
                end
            end

            // B/C 级：乘 + 奇偶双链累加 + 收尾快照
            always_ff @(posedge ap_clk or negedge ap_rst_n) begin
                if (!ap_rst_n) begin
                    prod_b[gi] <= 16'sd0;
                    acc_e[gi]  <= 26'sd0;
                    acc_o[gi]  <= 26'sd0;
                    ep_e[gi]   <= 26'sd0;
                    ep_o[gi]   <= 26'sd0;
                end else if (start_pulse || pass_go) begin
                    prod_b[gi] <= 16'sd0;
                    acc_e[gi]  <= 26'sd0;
                    acc_o[gi]  <= 26'sd0;
                end else begin
                    prod_b[gi] <= p_a * $signed(w_word_q[gi][{sel_a, 3'b000} +: 8]);
                    if (v_b) begin
                        if (last_b) begin
                            if (par_b) begin
                                ep_e[gi] <= acc_e[gi];
                                ep_o[gi] <= acc_o[gi] + {{10{prod_b[gi][15]}}, prod_b[gi]};
                            end else begin
                                ep_e[gi] <= acc_e[gi] + {{10{prod_b[gi][15]}}, prod_b[gi]};
                                ep_o[gi] <= acc_o[gi];
                            end
                            acc_e[gi] <= 26'sd0;
                            acc_o[gi] <= 26'sd0;
                        end else if (par_b) begin
                            acc_o[gi] <= acc_o[gi] + {{10{prod_b[gi][15]}}, prod_b[gi]};
                        end else begin
                            acc_e[gi] <= acc_e[gi] + {{10{prod_b[gi][15]}}, prod_b[gi]};
                        end
                    end
                end
            end

            // E1/E2：合并双链 → 加 bias
            always_ff @(posedge ap_clk or negedge ap_rst_n) begin
                if (!ap_rst_n) begin
                    sum27[gi] <= 27'sd0;
                    v32[gi]   <= 32'sd0;
                end else if (start_pulse || pass_go) begin
                    sum27[gi] <= 27'sd0;
                    v32[gi]   <= 32'sd0;
                end else begin
                    if (e1_vld)
                        sum27[gi] <= ep_e[gi] + ep_o[gi];
                    if (e2_vld)
                        v32[gi] <= $signed({{5{sum27[gi][26]}}, sum27[gi]}) +
                                   (ep_blk_q ? bias1[gi] : bias0[gi]);
                end
            end

            // E3：>>>shift / ReLU / sat8（组合），lane 越界写 0
            wire signed [31:0] shf = v32[gi] >>> shift_r;
            wire [6:0] lane_abs = gi[6:0] + (ep_blk_q ? 7'd32 : 7'd0);
            wire       lane_ok  = (lane_abs < cout_r);
            always_comb begin
                if (!lane_ok)                   sat_b[gi] = 8'd0;
                else if (RELU && shf[31])       sat_b[gi] = 8'd0;
                else if (shf > 32'sd127)        sat_b[gi] = 8'd127;
                else if (shf < -32'sd128)       sat_b[gi] = 8'd128; // -128
                else                            sat_b[gi] = shf[7:0];
            end
        end
    endgenerate

    // ========================================================================
    // 7. 收尾流水控制（E1→E2→E3，3 拍）+ 输出版本拼字
    // ========================================================================
    logic [63:0] out_word [0:3];
    always_comb begin
        for (int j = 0; j < 4; j++)
            for (int b = 0; b < 8; b++)
                out_word[j][b*8 +: 8] = sat_b[j*8 + b];
    end

    logic [2:0]  push_n;
    logic [31:0] push_addr [0:3];
    logic [63:0] push_data [0:3];

    logic [2:0] push_n_comb;
    always_comb begin
        push_n_comb = 3'd0;
        for (int jc = 0; jc < 4; jc = jc + 1)
            if (({1'b0, ep_blk_q, 2'b00} + jc[2:0]) < ncg_r)
                push_n_comb = push_n_comb + 3'd1;
    end

    integer jj;
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            e1_vld <= 1'b0; e2_vld <= 1'b0; e3_vld <= 1'b0;
            push_vld <= 1'b0; push_n <= 3'd0;
            ep_blk_q <= 2'd0; ep_gbase <= 32'd0;
        end else if (start_pulse || pass_go) begin
            e1_vld <= 1'b0; e2_vld <= 1'b0; e3_vld <= 1'b0;
            push_vld <= 1'b0; push_n <= 3'd0;
        end else begin
            e1_vld <= v_b && last_b;
            e2_vld <= e1_vld;
            e3_vld <= e2_vld;
            push_vld <= e3_vld;

            if (v_b && last_b) begin
                ep_blk_q <= blk_b;
                ep_gbase <= out_base_r + pixoff_b + (blk_b ? (hw8_r << 2) : 32'd0);
            end

            if (e3_vld) begin
                push_n <= push_n_comb;
                for (jj = 0; jj < 4; jj = jj + 1) begin
                    if (({1'b0, ep_blk_q, 2'b00} + jj[2:0]) < ncg_r) begin
                        push_addr[jj] <= ep_gbase + jj * hw8_r;
                        push_data[jj] <= out_word[jj];
                    end
                end
            end
        end
    end

    // ========================================================================
    // 8. 输出块队列（OBUF_N 块 × 4 组）与 AXI4 写排空
    // ========================================================================
    logic [1:0]  ob_wr, ob_rd;
    logic [2:0]  ob_n [0:OBUF_N-1];
    logic [31:0] ob_addr [0:OBUF_N-1][0:3];
    logic [63:0] ob_data [0:OBUF_N-1][0:3];
    logic        wr_pop;

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            ob_wr <= 2'd0; ob_rd <= 2'd0; ob_cnt <= 3'd0;
        end else if (start_pulse) begin
            ob_wr <= 2'd0; ob_rd <= 2'd0; ob_cnt <= 3'd0;
        end else begin
            if (push_ok) begin
                for (jj = 0; jj < 4; jj = jj + 1) begin
                    ob_addr[ob_wr][jj] <= push_addr[jj];
                    ob_data[ob_wr][jj] <= push_data[jj];
                end
                ob_n[ob_wr] <= push_n;
                ob_wr       <= ob_wr + 2'd1;
            end
            if (wr_pop) ob_rd <= ob_rd + 2'd1;
            ob_cnt <= ob_cnt + {2'd0, push_ok} - {2'd0, wr_pop};
        end
    end

    logic [1:0] wd_j;
    logic       wd_aw, wd_w;
    wire        wr_ent = (ob_cnt != 3'd0) && ({1'b0, wd_j} < ob_n[ob_rd]);

    assign m_axi_out_awvalid = wr_ent && !wd_aw;
    assign m_axi_out_awaddr  = ob_addr[ob_rd][wd_j];
    assign m_axi_out_awlen   = 8'd0;
    assign m_axi_out_wvalid  = wr_ent && !wd_w;
    assign m_axi_out_wdata   = ob_data[ob_rd][wd_j];
    assign m_axi_out_wstrb   = 8'hFF;
    assign m_axi_out_wlast   = 1'b1;
    assign m_axi_out_bready  = 1'b1;

    wire out_aw_hs = m_axi_out_awvalid && m_axi_out_awready;
    wire out_w_hs  = m_axi_out_wvalid  && m_axi_out_wready;

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            wd_j <= 2'd0; wd_aw <= 1'b0; wd_w <= 1'b0; wr_pop <= 1'b0;
        end else if (start_pulse) begin
            wd_j <= 2'd0; wd_aw <= 1'b0; wd_w <= 1'b0; wr_pop <= 1'b0;
        end else begin
            wr_pop <= 1'b0;
            if (!wr_ent) begin
                wd_j <= 2'd0; wd_aw <= 1'b0; wd_w <= 1'b0;
            end else begin
                if (out_aw_hs) wd_aw <= 1'b1;
                if (out_w_hs)  wd_w  <= 1'b1;
                if ((wd_aw || out_aw_hs) && (wd_w || out_w_hs)) begin
                    if (({1'b0, wd_j} + 3'd1) == ob_n[ob_rd]) begin
                        wd_j <= 2'd0; wd_aw <= 1'b0; wd_w <= 1'b0;
                        wr_pop <= 1'b1;
                    end else begin
                        wd_j <= wd_j + 2'd1;
                        wd_aw <= 1'b0; wd_w <= 1'b0;
                    end
                end
            end
        end
    end

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n)                 b_cnt <= 8'd0;
        else if (start_pulse)          b_cnt <= 8'd0;
        else                           b_cnt <= b_cnt + {7'd0, out_aw_hs}
                                                 - {7'd0, (m_axi_out_bvalid && m_axi_out_bready)};
    end

    // ========================================================================
    // 9. 权重装载（每层一次；bank0 装完即开始计算，bank1 在 pass0 期间装好）
    //    权重布局 k=1：[oc][ic] 1 字节 → 每 oc 的 cin 字节 = cin/8 个 64-bit 字
    // ========================================================================
    localparam logic [1:0] WL_IDLE = 2'd0, WL_AR = 2'd1, WL_R = 2'd2;

    logic [1:0]  wl_state;
    logic [31:0] wl_addr;
    logic [4:0]  wl_lane;
    logic [3:0]  wl_word;
    logic [1:0]  wl_blk;
    logic [6:0]  wl_lane_lim;            // 本 bank 实际要装的 oc 数（min(32, 余量)）
    wire  [3:0]  wpl = cin_r[6:3];        // 每 oc 的 64-bit 字数

    assign m_axi_w_arvalid = (wl_state == WL_AR);
    assign m_axi_w_araddr  = wl_addr;
    assign m_axi_w_arlen   = (wpl == 4'd0) ? 8'd0 : (wpl - 4'd1);
    assign m_axi_w_rready  = (wl_state == WL_R);

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            wl_state <= WL_IDLE;
            wl_addr  <= 32'd0;
            wl_lane  <= 5'd0;
            wl_word  <= 4'd0;
            wl_blk   <= 2'd0;
            wl_we    <= 1'b0;
            wl_din   <= 64'd0;
            wl_we_lane <= 5'd0;
            wl_we_word <= 4'd0;
            wl_we_blk  <= 2'd0;
            wl_lane_lim <= 7'd32;
            bank_ready <= 2'd0;
        end else if (start_pulse) begin
            wl_state   <= WL_AR;
            wl_addr    <= ctrl_wgt_addr;
            wl_lane    <= 5'd0;
            wl_word    <= 4'd0;
            wl_blk     <= 2'd0;
            wl_we      <= 1'b0;
            wl_lane_lim <= (ctrl_cout[6:0] >= 7'd32) ? 7'd32 : ctrl_cout[6:0];
            bank_ready <= 2'd0;
        end else begin
            wl_we <= 1'b0;
            case (wl_state)
                WL_AR: begin
                    if (m_axi_w_arvalid && m_axi_w_arready)
                        wl_state <= WL_R;
                end
                WL_R: begin
                    if (m_axi_w_rvalid && m_axi_w_rready) begin
                        wl_we      <= 1'b1;
                        wl_din     <= m_axi_w_rdata;
                        wl_we_lane <= wl_lane;
                        wl_we_word <= wl_word;
                        wl_we_blk  <= wl_blk;
                        wl_addr    <= wl_addr + 32'd8;
                        if ((wl_word + 4'd1) == wpl) begin
                            wl_word <= 4'd0;
                            if (({1'b0, wl_lane} + 7'd1) == wl_lane_lim) begin
                                bank_ready[wl_blk] <= 1'b1;
                                if ((wl_blk + 2'd1) < blk_cnt_r) begin
                                    wl_blk      <= wl_blk + 2'd1;
                                    wl_lane     <= 5'd0;
                                    wl_lane_lim <= cout_r - 7'd32; // 本 bank 剩余 oc 数
                                    wl_state    <= WL_AR;
                                end else begin
                                    wl_state <= WL_IDLE;
                                end
                            end else begin
                                wl_lane  <= wl_lane + 5'd1;
                                wl_state <= WL_AR;
                            end
                        end else begin
                            wl_word <= wl_word + 4'd1;
                        end
                    end
                end
                default: wl_state <= WL_IDLE;
            endcase
        end
    end

    // ========================================================================
    // 10. 偏置装载（cout × int32，每 64-bit 拍 2 个；bank0=oc0..31，bank1=oc32..63）
    // ========================================================================
    localparam logic [1:0] BI_IDLE = 2'd0, BI_AR = 2'd1, BI_R = 2'd2;

    logic [1:0] bi_state;
    logic [5:0] bi_beat, bi_total;
    wire  [5:0] bi_n = (ctrl_cout[6:0] + 7'd1) >> 1;

    assign m_axi_b_arvalid = (bi_state == BI_AR);
    assign m_axi_b_araddr  = ctrl_bias_addr;
    assign m_axi_b_arlen   = bi_n - 6'd1;
    assign m_axi_b_rready  = (bi_state == BI_R);

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            bi_state  <= BI_IDLE;
            bi_beat   <= 6'd0;
            bi_total  <= 6'd0;
            bl_weA    <= 1'b0;
            bl_weB    <= 1'b0;
            bl_idxA   <= 7'd0;
            bl_idxB   <= 7'd0;
            bl_dataA  <= 32'sd0;
            bl_dataB  <= 32'sd0;
            bias_ready <= 1'b0;
        end else if (start_pulse) begin
            bi_state  <= BI_AR;
            bi_beat   <= 6'd0;
            bi_total  <= bi_n;
            bl_weA    <= 1'b0;
            bl_weB    <= 1'b0;
            bias_ready <= 1'b0;
        end else begin
            bl_weA <= 1'b0;
            bl_weB <= 1'b0;
            case (bi_state)
                BI_AR: begin
                    if (m_axi_b_arvalid && m_axi_b_arready)
                        bi_state <= BI_R;
                end
                BI_R: begin
                    if (m_axi_b_rvalid && m_axi_b_rready) begin
                        bl_weA   <= 1'b1;
                        bl_weB   <= 1'b1;
                        bl_idxA  <= {bi_beat, 1'b0};
                        bl_idxB  <= {bi_beat, 1'b0} + 7'd1;
                        bl_dataA <= m_axi_b_rdata[31:0];
                        bl_dataB <= m_axi_b_rdata[63:32];
                        if ((bi_beat + 6'd1) == bi_total) begin
                            bi_state   <= BI_IDLE;
                            bias_ready <= 1'b1;
                        end else begin
                            bi_beat <= bi_beat + 6'd1;
                        end
                    end
                end
                default: bi_state <= BI_IDLE;
            endcase
        end
    end

    // ========================================================================
    // 11. 仿真期一致性断言（综合忽略）
    // ========================================================================
    // synopsys translate_off
    always @(posedge ap_clk) begin
        if (ap_rst_n && start_pulse && ((ctrl_cin[2:0] != 3'd0) ||
                                        (ctrl_cin < 32'd8)))
            $display("[K1] WARNING: cin=%0d 不是 8 的倍数或 <8，本版不支持",
                     ctrl_cin);
        if (ap_rst_n && push_vld && (ob_cnt >= OBUF_N))
            $display("[K1] ERROR: 输出块队列溢出（ob_cnt=%0d）", ob_cnt);
    end
    // synopsys translate_on

endmodule
