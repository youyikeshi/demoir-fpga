// ============================================================================
//  conv_k1_engine.v —— 手写 1×1 卷积加速器 IP（k=1, stride=1, pad=0, dil=1）
//
//  契约：docs/rtl_hand/00_SPEC_接口契约.md §3（端口名/方向严格一致）
//  结构来源：docs/rtl_map/04_mac_array.md / 03_weight_path.md
//
//  两版能力：
//    v1（已验收）：cin≤64, cout≤64，只覆盖 16/132 层；
//    v2（本版）  ：CIN_MAX=352, COUT_MAX=192 —— 覆盖网络全部 132 个 k=1 层的
//                 通道范围。车道数仍固定 32（DSP 仍 32 个，不随 cout 涨）；
//                 cout 用**多 pass** 覆盖：每个 pass 算 32 个 oc，权重按 pass
//                 重装（每层总共 cin·cout 字节，而不是 HLS 的
//                 cin·cout·Hout·ceil(Wout/64)）。
//
//  设计目标：
//    1. 32 个 oc 车道并行（8×8 乘法器 ×32），II=1；
//    2. 收尾 3 拍完全并行：acc0+acc1 → +bias → >>>shift/ReLU/sat8，且与下一块重叠；
//    3. 稳态每 (pixel, 32-oc 块) = cin 拍；
//    4. 输入按 C8 整 8 字节读，4 像素缓冲预取；
//    5. 权重按 pass 常驻 32 lane × cin 字节（cin=352 时 90 kbit，分 32 个
//       44×64bit LUTRAM）；每个 pass 的装载量 = 32·cin 字节，
//       相对该 pass 的 H·W·cin 拍可忽略（见文档）。
//
//  Verilog 位宽铁律（本版踩过的坑，务必保留注释）：
//    **part-select 基址 [expr +: W] 里的 expr 是自决定表达式**，`a*b` 的结果位宽
//    取 max(操作数位宽)（不是和）。因此所有“乘法型索引”都必须显式拼接加宽：
//        {sel_a, 3'b000}   // ic 字节偏移 = sel_a*8
//        {f_rd, 6'b000000} // 组偏移 = f_rd*64
//    写成 sel_a*4'd8 / f_rd*7'd64 会被截断成 4/7 位，导致只读字节 0/1、只落 2 个组。
//
//  参数：
//    CIN/COUT/W/H/PAD/STRIDE/DIL/RELU 与契约 §3 同名；运行期形状由 ctrl_* 给出。
//    CIN_MAX/COUT_MAX 是编译期上界。
//
//  已知限制（详见 docs/rtl_hand/conv_k1_engine.md）：
//    · cin % 8 == 0、cin ≥ 8（全网络 132 个 k=1 层都满足）
//    · cout ≤ 192（6 个 pass）；RELU 是编译期参数
//    · 不支持 stride≠1 / pad≠0 / dil≠1（k=1 不需要）
//    · 输出每 oc 组 8 字节单拍写（AWLEN=0），不是 burst
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

    // ---- 实现参数 ----
    parameter int LANES    = 32,   // oc 车道数（固定，DSP = 32）
    parameter int CIN_MAX  = 352,  // 全网络 k=1 最大 cin
    parameter int COUT_MAX = 192,  // 全网络 k=1 最大 cout（6 个 pass）
    parameter int NBUF     = 4,    // 输入像素缓冲数
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

    // ---- 控制寄存器 ----
    input  logic [31:0] ctrl_in_addr,
    input  logic [31:0] ctrl_wgt_addr,
    input  logic [31:0] ctrl_bias_addr,
    input  logic [31:0] ctrl_out_addr,
    input  logic [31:0] ctrl_shift,
    input  logic [31:0] ctrl_cin,
    input  logic [31:0] ctrl_cout,
    input  logic [31:0] ctrl_h,
    input  logic [31:0] ctrl_w,
    input  logic        ctrl_start,
    input  logic        ctrl_relu,     // 运行期 ReLU：1=clamp(v>>shift,0,127)，0=clamp(...,-128,127)
    output logic        ctrl_done,
    output logic        ctrl_busy
);

    // ========================================================================
    // 0. 常量 / 位宽
    // ========================================================================
    localparam int GMAX    = CIN_MAX / 8;                    // 44 组
    localparam int BLKMAX  = (COUT_MAX + LANES - 1) / LANES; // 6 个 oc pass
    localparam int NCGMAX  = (COUT_MAX + 7) / 8;             // 24 个 oc 组
    localparam int ICW     = $clog2(CIN_MAX + 1);            // 9
    localparam int CW      = $clog2(COUT_MAX + 1);           // 8
    localparam int GW      = $clog2(GMAX + 1);               // 6
    localparam int BW      = $clog2(BLKMAX + 1);             // 3
    localparam int NGW     = $clog2(NCGMAX + 1);             // 5

    // ========================================================================
    // 1. 配置锁存 / 派生
    // ========================================================================
    logic [31:0] in_base_r, wgt_base_r, bias_base_r, out_base_r, pass_gbase;
    logic [ICW-1:0] cin_r;
    logic [CW-1:0]  cout_r;
    logic [4:0]     shift_r;
    logic [31:0]    pix_total_r, hw8_r;
    logic [GW-1:0]  g_count_r;
    logic [BW-1:0]  blk_cnt_r, blk_r;
    logic [NGW-1:0] ncg_r;

    // 16×16 在 assignment 上下文里本该是 32 位；显式拼 32 位，防止再踩自决定位宽
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
    logic       bank_ready, bias_ready;

    logic         f_busy;
    logic [31:0]  f_pix, f_next, f_araddr;
    logic [GW-1:0] f_ar, f_rd;
    logic [7:0]   b_cnt;
    logic [2:0]   ob_cnt;
    logic         push_vld;
    // 像素缓冲：{buffer[1:0], group[5:0]} 直接寻址的简单双口 LUTRAM（1 写 + 1 异步读）。
    // 读地址 = {cur_buf, ic_cur[8:3]}，组内按 ic_cur[2:0] 选字节。
    // 这样既不需要 2816-bit 移位（它的“移位值/新数据”二选一 mux 是上一版 31k LUT 的来源），
    // 也不需要环形指针；0 FF、~256 LUT6、0 BRAM、异步读无延迟。
    (* ram_style = "distributed" *) reg [63:0] px_mem [0:255];
    // RAM 推断模板的读寄存器：**只能**由 px_mem 读赋值（见 §4）。
    logic [63:0] p_ram_q;
    // 单 beat 旁路：把“正在写的 group0”寄存一拍，计算侧再做组合 mux。
    logic        bp_hit_q;
    logic [63:0] bp_data_q;

    assign ctrl_busy   = (state != ST_IDLE);
    assign start_pulse = ctrl_start & ~start_q;

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) start_q <= 1'b0;
        else           start_q <= ctrl_start;
    end

    logic [31:0] pix_cur, pix_off;
    logic [ICW-1:0] ic_cur;
    logic        pass_done;
    logic [2:0]  pend;
    logic [1:0]  cur_buf;

    assign cur_buf = pix_cur[1:0];

    logic pass_go;
    logic [NBUF-1:0] pix_ready;
    wire blk_room = (ob_cnt <= (OBUF_N - 2));

    logic issue_en;
    assign issue_en = (state == ST_PASS) && !pass_done &&
                      ((ic_cur != {ICW{1'b0}}) ||
                       (pix_ready[cur_buf] && blk_room));

    wire block_start = issue_en && (ic_cur == {ICW{1'b0}});
    wire push_ok     = push_vld && (ob_cnt < OBUF_N);

    // 权重装载请求（PASS→NEXT 时给 loader 一个 1 拍脉冲）
    logic          wl_req_pulse;
    logic [BW-1:0] wl_req_blk;
    logic [BW-1:0] wl_done_blk;   // 前向声明：pass_go 要用（避免先用后声明）

    assign pass_go = ((state == ST_LOAD) && bank_ready && (wl_done_blk == 3'd0) && bias_ready) ||
                     ((state == ST_NEXT) && bank_ready && (wl_done_blk == blk_r) && bias_ready);

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            state       <= ST_IDLE;
            ctrl_done   <= 1'b0;
            blk_r       <= 3'd0;
            blk_cnt_r   <= 3'd1;
            pass_gbase  <= 32'd0;
            wl_req_pulse<= 1'b0;
            wl_req_blk  <= 3'd0;
            in_base_r   <= 32'd0;
            wgt_base_r  <= 32'd0;
            bias_base_r <= 32'd0;
            out_base_r  <= 32'd0;
            cin_r       <= 9'd0;
            cout_r      <= 8'd0;
            shift_r     <= 5'd0;
            pix_total_r <= 32'd0;
            hw8_r       <= 32'd0;
            g_count_r   <= 6'd0;
            ncg_r       <= 5'd1;
        end else begin
            ctrl_done <= 1'b0;
            case (state)
                ST_IDLE: begin
                    if (start_pulse) begin
                        in_base_r   <= ctrl_in_addr;
                        wgt_base_r  <= ctrl_wgt_addr;
                        bias_base_r <= ctrl_bias_addr;
                        out_base_r  <= ctrl_out_addr;
                        pass_gbase  <= ctrl_out_addr;
                        cin_r       <= ctrl_cin[ICW-1:0];
                        cout_r      <= ctrl_cout[CW-1:0];
                        shift_r     <= ctrl_shift[4:0];
                        pix_total_r <= hw_mul;
                        hw8_r       <= hw_mul << 3;
                        g_count_r   <= ctrl_cin[ICW-1:3];
                        blk_cnt_r   <= (ctrl_cout[CW-1:0] + 8'd31) >> 5;
                        ncg_r       <= (ctrl_cout[CW-1:0] + 8'd7) >> 3;
                        blk_r       <= 3'd0;
                        wl_req_pulse<= 1'b0;
                        state       <= ST_LOAD;
                    end
                end
                ST_LOAD: if (pass_go) state <= ST_PASS;
                ST_PASS: begin
                    if (pass_done && (pend == 3'd0) && !f_busy &&
                        (f_next == pix_total_r)) begin
                        if ((blk_r + 3'd1) < blk_cnt_r) begin
                            blk_r        <= blk_r + 3'd1;
                            pass_gbase   <= pass_gbase + (hw8_r << 2);
                            wl_req_pulse <= 1'b1;
                            wl_req_blk   <= blk_r + 3'd1;
                            state        <= ST_NEXT;
                        end else begin
                            state <= ST_DRAIN;
                        end
                    end
                end
                ST_NEXT: begin
                    wl_req_pulse <= 1'b0;
                    if (pass_go) state <= ST_PASS;
                end
                ST_DRAIN: if ((ob_cnt == 3'd0) && (b_cnt == 8'd0)) state <= ST_DONE;
                ST_DONE: begin
                    ctrl_done <= 1'b1;
                    state     <= ST_IDLE;
                end
                default: state <= ST_IDLE;
            endcase
        end
    end

    // ---- 计算像素 / ic 指针 ----
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            pix_cur   <= 32'd0;
            pix_off   <= 32'd0;
            ic_cur    <= 9'd0;
            pass_done <= 1'b0;
        end else if (start_pulse || pass_go) begin
            pix_cur   <= 32'd0;
            pix_off   <= 32'd0;
            ic_cur    <= 9'd0;
            pass_done <= 1'b0;
        end else if (issue_en) begin
            if (ic_cur == (cin_r - 1'b1)) begin
                ic_cur <= {ICW{1'b0}};
                if (pix_cur == (pix_total_r - 32'd1)) begin
                    pass_done <= 1'b1;
                end else begin
                    pix_cur <= pix_cur + 32'd1;
                    pix_off <= pix_off + 32'd8;
                end
            end else begin
                ic_cur <= ic_cur + 1'b1;
            end
        end
    end

    // ---- 在飞块计数 ----
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n)                   pend <= 3'd0;
        else if (start_pulse || pass_go) pend <= 3'd0;
        else                             pend <= pend + block_start - push_ok;
    end

    // ========================================================================
    // 3. AXI4 读：输入像素预取（每像素 g_count 笔 1-beat，C8 组步长 HW*8）
    // ========================================================================
    wire in_ar_hs = m_axi_in_arvalid && m_axi_in_arready;
    wire in_r_hs  = m_axi_in_rvalid  && m_axi_in_rready;

    // 预取下一拍 issue 要用的 {buffer, group} 组字：
    //   · 正常 issue：ic 前进 1；当前是字节 7 时组 +1
    //   · 最后一块拍：下一拍是下一像素的 ic=0 → {下一 buffer, 0}
    //   · 停顿（issue_en=0）：ic 不前进，组不变
    wire last_issue = issue_en && (ic_cur == (cin_r - 1'b1));
    wire [1:0] nxt_buf = last_issue ? (cur_buf + 2'd1) : cur_buf;
    wire [GW-1:0] nxt_grp = last_issue ? {GW{1'b0}} :
                            (ic_cur[ICW-1:3] +
                             {{(GW-1){1'b0}}, (issue_en && (ic_cur[2:0] == 3'd7))});
    wire [7:0] nxt_addr = {nxt_buf, nxt_grp};

    assign m_axi_in_arvalid = f_busy && (f_ar < g_count_r);
    assign m_axi_in_araddr  = f_araddr;
    assign m_axi_in_arlen   = 8'd0;
    assign m_axi_in_rready  = f_busy;

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            f_busy    <= 1'b0;
            f_pix     <= 32'd0;
            f_next    <= 32'd0;
            f_araddr  <= 32'd0;
            f_ar      <= 6'd0;
            f_rd      <= 6'd0;
            pix_ready <= '0;
        end else if (start_pulse || pass_go) begin
            f_busy    <= 1'b0;
            f_next    <= 32'd0;
            f_ar      <= 6'd0;
            f_rd      <= 6'd0;
            pix_ready <= '0;
        end else begin
            // ---- 计算侧：消费完一个像素后清 ready（像素数据在 px_mem 里，不移动）----
            if (issue_en && (ic_cur == (cin_r - 1'b1)))
                pix_ready[cur_buf] <= 1'b0;

            // ---- fetch 侧 ----
            if (!f_busy) begin
                if ((state == ST_PASS) && (f_next < pix_total_r) &&
                    ((f_next - pix_cur) < NBUF[31:0])) begin
                    f_busy   <= 1'b1;
                    f_pix    <= f_next;
                    f_ar     <= 6'd0;
                    f_rd     <= 6'd0;
                    f_araddr <= in_base_r + (f_next << 3);
                end
            end else begin
                if (in_ar_hs) begin
                    f_ar     <= f_ar + 6'd1;
                    f_araddr <= f_araddr + hw8_r;
                end
                if (in_r_hs)
                    f_rd <= f_rd + 6'd1;
                if (((f_ar + {5'd0, in_ar_hs}) == g_count_r) &&
                    ((f_rd + {5'd0, in_r_hs}) == g_count_r)) begin
                    f_busy                <= 1'b0;
                    pix_ready[f_pix[1:0]] <= 1'b1;
                    f_next                <= f_pix + 32'd1;
                end
            end
        end
    end

    // ========================================================================
    // 4. 输入像素缓冲（4 × 44 × 64-bit，简单双口 RAM）
    // ========================================================================
    // !! RAM 推断模板（禁止改）:
    //   这个 always 里 p_ram_q **只能**由 px_mem 的读赋值；不要给它加 if/else、
    //   不要做“同拍写数据旁路”——Vivado 的 RAM 模板不接受一个寄存器两个源，
    //   会退化成 16,384 FF + 256:1 读 mux（第三轮已实测）。
    //   旁路放在下面的 bp_hit_q/bp_data_q 寄存器 + p_word 组合 mux。
    always_ff @(posedge ap_clk) begin
        if (in_r_hs)
            px_mem[{f_pix[1:0], f_rd}] <= m_axi_in_rdata;
        p_ram_q <= px_mem[nxt_addr];
    end

    // 单 beat（cin=8）旁路：把“正在写的 group0”寄存一拍。
    //   · beat 在 t，首次 issue 在 t+1；t+1 时 in_r_hs 已是 0，所以旁路必须寄存。
    //   · 多 beat 像素时 group0 早已写入 RAM，bp_hit_q 只在“等待/首次 issue”这一拍
    //     置 1，随后 p_ram_q 就是新值，行为不变。
    wire bypass_now = in_r_hs && (f_rd == 6'd0) &&
                      (f_pix[1:0] == nxt_buf) && (nxt_grp == {GW{1'b0}});
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            bp_hit_q  <= 1'b0;
            bp_data_q <= 64'd0;
        end else begin
            bp_hit_q  <= bypass_now;
            bp_data_q <= m_axi_in_rdata;
        end
    end

    wire [63:0] p_word = bp_hit_q ? bp_data_q : p_ram_q;
    wire [7:0]  p_byte = p_word[{ic_cur[2:0], 3'b000} +: 8];

    // ========================================================================
    // 5. 计算流水（3 级，II=1）
    // ========================================================================
    logic signed [7:0] p_a;
    logic [2:0]        sel_a;
    logic              par_a, par_b;
    logic              last_a, last_b;
    logic              v_a, v_b;
    logic [31:0]       pixoff_a, pixoff_b;
    logic [BW-1:0]     blk_a, blk_b;

    logic [63:0]        w_word_q [0:LANES-1];
    (* use_dsp = "yes" *) logic signed [15:0] prod_b [0:LANES-1];
    logic signed [25:0] acc_e    [0:LANES-1];
    logic signed [25:0] acc_o    [0:LANES-1];
    logic signed [26:0] sum27    [0:LANES-1];
    logic signed [31:0] v32      [0:LANES-1];
    logic [7:0]         sat_b    [0:LANES-1];
    logic signed [31:0] bias_lane [0:LANES-1][0:BLKMAX-1];

    // 收尾流水 valid / 块信息（供 generate 与 E1/E2/E3 共用）
    logic          e1_vld, e2_vld;
    logic [BW-1:0] ep_blk_q;
    logic [31:0]   ep_gbase;

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            p_a <= 8'sd0; sel_a <= 3'd0;
            par_a <= 1'b0; par_b <= 1'b0;
            last_a <= 1'b0; last_b <= 1'b0;
            v_a <= 1'b0; v_b <= 1'b0;
            pixoff_a <= 32'd0; pixoff_b <= 32'd0;
            blk_a <= 3'd0; blk_b <= 3'd0;
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
                last_a   <= (ic_cur == (cin_r - 1'b1));
                pixoff_a <= pix_off;
                blk_a    <= blk_r;
            end else begin
                // 块边界若 issue 停顿，last_a 必须清 0；否则 last_b 会在停顿期间
                // 保持 1，下一块第一个乘积会被误判成“最后拍”。
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
    // 6. 权重 bank（每 lane 一个 44×64bit ram，地址 = ic>>3）+ 累加器
    // ========================================================================
    logic        wl_we;
    logic [63:0] wl_din;
    logic [4:0]  wl_we_lane;
    logic [GW-1:0] wl_we_word;

    // 偏置装载写口（在 generate 之前声明，供每个 lane 的 bias 写解码使用）
    logic             bl_weA, bl_weB;
    logic [7:0]       bl_idxA, bl_idxB;
    logic signed [31:0] bl_dataA, bl_dataB;

    genvar gi;
    generate
        (* keep_hierarchy = "yes" *)
        for (gi = 0; gi < LANES; gi = gi + 1) begin : g_lane
            (* ram_style = "distributed" *) reg [63:0] wmem [0:GMAX-1];

            always_ff @(posedge ap_clk) begin
                if (wl_we && (wl_we_lane == gi[4:0]))
                    wmem[wl_we_word] <= wl_din;
                if (issue_en)
                    w_word_q[gi] <= wmem[ic_cur[ICW-1:3]];
            end

            // 偏置：BLKMAX 个 pass 各一份，收尾按 ep_blk_q 选（无读地址 mux）
            always_ff @(posedge ap_clk or negedge ap_rst_n) begin
                if (!ap_rst_n) begin
                    for (int kk = 0; kk < BLKMAX; kk++)
                        bias_lane[gi][kk] <= 32'sd0;
                end else begin
                    for (int kk = 0; kk < BLKMAX; kk++) begin
                        if (bl_weA && (bl_idxA == (gi + 32*kk)))
                            bias_lane[gi][kk] <= bl_dataA;
                        if (bl_weB && (bl_idxB == (gi + 32*kk)))
                            bias_lane[gi][kk] <= bl_dataB;
                    end
                end
            end

            // 乘法器：use_dsp 强制映射到 DSP48；**组合使用**（不启用 DSP 内部
            // A/B/M/P 寄存器），所以 prod_b 仍然只延迟 1 拍，像素/权重对齐不变。
            (* use_dsp = "yes" *) wire signed [15:0] mul_prod =
                p_a * $signed(w_word_q[gi][{sel_a, 3'b000} +: 8]);

            // B/C 级：乘 + 奇偶双链累加 + 收尾快照
            always_ff @(posedge ap_clk or negedge ap_rst_n) begin
                if (!ap_rst_n) begin
                    prod_b[gi] <= 16'sd0;
                    acc_e[gi]  <= 26'sd0;
                    acc_o[gi]  <= 26'sd0;
                    sum27[gi]  <= 27'sd0;
                end else if (start_pulse || pass_go) begin
                    prod_b[gi] <= 16'sd0;
                    acc_e[gi]  <= 26'sd0;
                    acc_o[gi]  <= 26'sd0;
                    sum27[gi]  <= 27'sd0;
                end else begin
                    prod_b[gi] <= mul_prod;
                    if (v_b) begin
                        if (last_b) begin
                            // 两条奇偶链 + 最后一个乘积一次加完（27 位有符号）。
                            // 原来先写 ep_e/ep_o（2×26 位）再下一拍相加；总数恒为
                            // acc_e+acc_o+prod_b，与 parity 无关，所以直接合并，
                            // 省 2×32×26 = 3,328 FF，并把收尾从 3 级压到 2 级。
                            sum27[gi] <= $signed({{1{acc_e[gi][25]}}, acc_e[gi]}) +
                                         $signed({{1{acc_o[gi][25]}}, acc_o[gi]}) +
                                         $signed({{11{prod_b[gi][15]}}, prod_b[gi]});
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

            // E2：sum27 + bias → v32（sum27 在 last_b 拍已经算好）
            always_ff @(posedge ap_clk or negedge ap_rst_n) begin
                if (!ap_rst_n) begin
                    v32[gi] <= 32'sd0;
                end else if (start_pulse || pass_go) begin
                    v32[gi] <= 32'sd0;
                end else if (e1_vld) begin
                    v32[gi] <= $signed({{5{sum27[gi][26]}}, sum27[gi]}) +
                               bias_lane[gi][ep_blk_q];
                end
            end

            // E3：>>>shift / ReLU / sat8
            wire signed [31:0] shf = v32[gi] >>> shift_r;
            wire [7:0] lane_abs = gi[7:0] + {ep_blk_q, 5'b00000};
            wire       lane_ok  = (lane_abs < cout_r);
            always_comb begin
                if (!lane_ok)                   sat_b[gi] = 8'd0;
                else if (ctrl_relu && shf[31])  sat_b[gi] = 8'd0;   // 仅 ReLU 时砍负值
                else if (shf > 32'sd127)        sat_b[gi] = 8'd127;
                else if (shf < -32'sd128)       sat_b[gi] = 8'd128; // -128
                else                            sat_b[gi] = shf[7:0];
            end
        end
    endgenerate

    // ========================================================================
    // 7. 收尾流水控制（sum27→+bias→>>>shift，2 拍）+ 输出拼字
    // ========================================================================
    logic [63:0] out_word [0:3];
    always_comb begin
        for (int j = 0; j < 4; j++)
            for (int b = 0; b < 8; b++)
                out_word[j][{b[2:0], 3'b000} +: 8] = sat_b[j*8 + b];
    end

    logic [2:0]  push_n;

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
            e1_vld <= 1'b0; e2_vld <= 1'b0;
            push_vld <= 1'b0; push_n <= 3'd0;
            ep_blk_q <= 3'd0; ep_gbase <= 32'd0;
        end else if (start_pulse || pass_go) begin
            e1_vld <= 1'b0; e2_vld <= 1'b0;
            push_vld <= 1'b0; push_n <= 3'd0;
        end else begin
            e1_vld <= v_b && last_b;
            e2_vld <= e1_vld;
            push_vld <= e2_vld;

            if (v_b && last_b) begin
                ep_blk_q <= blk_b;
                ep_gbase <= pass_gbase + pixoff_b;
            end

            if (e2_vld)
                push_n <= push_n_comb;
        end
    end

    // ========================================================================
    // 8. 输出块队列与 AXI4 写排空
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
                // ep_gbase / out_word 在 push 这一拍稳定，直接组合写入，省 384 FF
                for (jj = 0; jj < 4; jj = jj + 1) begin
                    ob_addr[ob_wr][jj] <= ep_gbase + jj * hw8_r;
                    ob_data[ob_wr][jj] <= out_word[jj];
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
    // 9. 权重装载（按 oc pass；每次装 32 lane × cin 字节，装完计算）
    // ========================================================================
    localparam logic [1:0] WL_IDLE = 2'd0, WL_AR = 2'd1, WL_R = 2'd2;

    logic [1:0]  wl_state;
    logic [31:0] wl_addr;
    logic [4:0]  wl_lane;
    logic [GW-1:0] wl_word;
    logic [5:0]  wl_lane_lim;
    logic [BW-1:0] wl_blk;
    wire  [GW-1:0] wpl = cin_r[ICW-1:3];
    wire  [7:0]    wl_rem = cout_r - {wl_req_blk, 5'b00000}; // 本 pass 剩余 oc 数

    assign m_axi_w_arvalid = (wl_state == WL_AR);
    assign m_axi_w_araddr  = wl_addr;
    assign m_axi_w_arlen   = (wpl == 6'd0) ? 8'd0 : (wpl - 6'd1);
    assign m_axi_w_rready  = (wl_state == WL_R);

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            wl_state    <= WL_IDLE;
            wl_addr     <= 32'd0;
            wl_lane     <= 5'd0;
            wl_word     <= 6'd0;
            wl_blk      <= 3'd0;
            wl_done_blk <= 3'd0;
            wl_we       <= 1'b0;
            wl_din      <= 64'd0;
            wl_we_lane  <= 5'd0;
            wl_we_word  <= 6'd0;
            wl_lane_lim <= 6'd32;
            bank_ready  <= 1'b0;
        end else if (start_pulse) begin
            wl_state    <= WL_AR;
            wl_addr     <= ctrl_wgt_addr;
            wl_lane     <= 5'd0;
            wl_word     <= 6'd0;
            wl_blk      <= 3'd0;
            wl_done_blk <= 3'd0;
            wl_we       <= 1'b0;
            wl_lane_lim <= (ctrl_cout[7:0] > 8'd32) ? 6'd32 : ctrl_cout[5:0];
            bank_ready  <= 1'b0;
        end else begin
            wl_we <= 1'b0;
            case (wl_state)
                WL_IDLE: begin
                    if (wl_req_pulse) begin
                        wl_blk      <= wl_req_blk;
                        wl_lane     <= 5'd0;
                        wl_word     <= 6'd0;
                        wl_lane_lim <= (wl_rem > 8'd32) ? 6'd32 : wl_rem[5:0];
                        bank_ready  <= 1'b0;
                        wl_state    <= WL_AR;
                    end
                end
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
                        wl_addr    <= wl_addr + 32'd8;
                        if ((wl_word + 6'd1) == wpl) begin
                            wl_word <= 6'd0;
                            if (({1'b0, wl_lane} + 6'd1) == wl_lane_lim) begin
                                bank_ready  <= 1'b1;
                                wl_done_blk <= wl_blk;
                                wl_state    <= WL_IDLE;
                            end else begin
                                wl_lane  <= wl_lane + 5'd1;
                                wl_state <= WL_AR;
                            end
                        end else begin
                            wl_word <= wl_word + 6'd1;
                        end
                    end
                end
                default: wl_state <= WL_IDLE;
            endcase
        end
    end

    // ========================================================================
    // 10. 偏置装载（所有 cout 一次装；每 64-bit 拍 2 个 int32；按 32 分 bank）
    // ========================================================================
    localparam logic [1:0] BI_IDLE = 2'd0, BI_AR = 2'd1, BI_R = 2'd2;

    logic [1:0] bi_state;
    logic [6:0] bi_beat, bi_total;
    wire  [6:0] bi_n = (ctrl_cout[7:0] + 8'd1) >> 1;

    assign m_axi_b_arvalid = (bi_state == BI_AR);
    assign m_axi_b_araddr  = ctrl_bias_addr;
    assign m_axi_b_arlen   = (bi_n == 7'd0) ? 8'd0 : (bi_n - 7'd1);
    assign m_axi_b_rready  = (bi_state == BI_R);

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            bi_state   <= BI_IDLE;
            bi_beat    <= 7'd0;
            bi_total   <= 7'd0;
            bl_weA     <= 1'b0;
            bl_weB     <= 1'b0;
            bl_idxA    <= 8'd0;
            bl_idxB    <= 8'd0;
            bl_dataA   <= 32'sd0;
            bl_dataB   <= 32'sd0;
            bias_ready <= 1'b0;
        end else if (start_pulse) begin
            bi_state   <= BI_AR;
            bi_beat    <= 7'd0;
            bi_total   <= bi_n;
            bl_weA     <= 1'b0;
            bl_weB     <= 1'b0;
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
                        bl_idxB  <= {bi_beat, 1'b0} + 8'd1;
                        bl_dataA <= m_axi_b_rdata[31:0];
                        bl_dataB <= m_axi_b_rdata[63:32];
                        if ((bi_beat + 7'd1) == bi_total) begin
                            bi_state   <= BI_IDLE;
                            bias_ready <= 1'b1;
                        end else begin
                            bi_beat <= bi_beat + 7'd1;
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
        if (ap_rst_n && start_pulse &&
            ((ctrl_cin[2:0] != 3'd0) || (ctrl_cin < 32'd8) || (ctrl_cin > CIN_MAX)))
            $display("[K1] WARNING: cin=%0d 不满足 8 的倍数 / 8..%0d，本版不支持",
                     ctrl_cin, CIN_MAX);
        if (ap_rst_n && (ctrl_cout > COUT_MAX))
            $display("[K1] WARNING: cout=%0d 超过 COUT_MAX=%0d", ctrl_cout, COUT_MAX);
        if (ap_rst_n && push_vld && (ob_cnt >= OBUF_N))
            $display("[K1] ERROR: 输出块队列溢出（ob_cnt=%0d）", ob_cnt);
    end
    // synopsys translate_on

endmodule
