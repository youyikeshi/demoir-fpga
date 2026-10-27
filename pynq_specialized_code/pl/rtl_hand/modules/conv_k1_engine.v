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
//    5. 权重按 pass 常驻 32 lane × cin 字节（cin=352 时 90 kbit，**4 块 RAMB36**）；
//       每个 pass 的装载量 = 32·cin 字节，相对该 pass 的 H·W·cin 拍可忽略（见文档）。
//
//  第三轮瘦身（2026-10-26，见 docs/rtl_hand/conv_k1_engine.md §16）：
//    A. 输出块队列 ob_*：4 槽×4 组×(32b+64b) 寄存器阵列（1,536 FF + 4,190 LUT cell）
//       → **16×96 简单双口 LUTRAM**（push 的 4 组串行 4 拍写，cin≥8 有余量）；
//    B. 偏置 bias_lane：32 lane × 6 pass × 32b FF 阵列 + 6:1 mux（6,144 FF + 3,288
//       LUT cell）→ **每 lane 一块 6×32 分布式 RAM**（1 写口 + 组合读口）。
//    两处都遵守“RAM 输出寄存器单一赋值源、无复位、无旁路”的推断铁律。
//    权重 BRAM（§6）与像素 LUTRAM（§4）**一个字没动**（上一轮已全套验证）。
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
    parameter int OBUF_N   = 4     // 输出块队列深度（每块 4 个 oc 组；LUTRAM 4 槽 × 4 条）
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

    // 权重 RAM 的“行”= 一个 ic：行内 32 lane 各 1 字节（与 C8 字节顺序一致）。
    // 行宽 = LANES*8 = 256 bit；深度 = CIN_MAX = 352。BRAM 推断见 §6。
    localparam int WW      = LANES * 8;                      // 256 bit/行
    // 权重 RAM 的物理分块（§6b 的 XPM）：每块 8 个 lane = 8 字节 = 64 bit。
    localparam int WBW     = 8 * 8;                          // 64 bit/块
    localparam int GPN     = LANES / 8;                      // 4 块（LANES 固定 32）
    localparam int WDW     = 1 << ICW;                       // 512 行（≥ CIN_MAX=352）
                                                             // = 2^ICW，与 ADDR_WIDTH 自洽

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
    // 当前 pass 的权重在 w_mem 里的行基址（多 pass 时用“行槽”错开，见 §6/§9）
    logic [ICW-1:0] row_base_r;

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
    // 像素缓冲：{buffer[1:0], group[5:0]} 直接寻址，256 × 64 bit。
    // 读地址 = {nxt_buf, nxt_grp}（组内按 ic_cur[2:0] 选字节）。
    // 这样既不需要 2816-bit 移位（它的“移位值/新数据”二选一 mux 是上一版 31k LUT 的来源），
    // 也不需要环形指针。
    //
    // ⚠⚠ 2026-10-27 第 2 轮：**LUTRAM → 显式 XPM（BRAM）**，−336 个 LUT。
    //   原文里"1 写 + 1 异步读 / 异步读无延迟 / 0 BRAM"那段注释是**历史残留**
    //   （第三轮把它改成推断模板时就变成同步读了），照它判断会得出错误结论。
    //   代码事实：`always_ff: p_ram_q <= px_mem[nxt_addr];` —— **同步读、1 拍、无复位、
    //   唯一来源**；写口在同一个 always 里；`bp_hit_q/bp_data_q` 旁路在 RAM **外面**。
    //   ⇒ 换 BRAM 后读延迟**仍是 1 拍**（XPM 的 READ_LATENCY_B=1 就是这个寄存器），
    //     相位零变化、消费者 `p_word` 一行都不用改。
    logic [63:0] px_dout;                   // XPM 的寄存输出（= 原来的 p_ram_q 值）
    // 读数据（纯连线，保持原变量名，消费者不用改）
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

    // 权重装载请求（pass 启动时预装下一 pass；行槽不够时退回 pass 末尾装）
    logic          wl_req_pulse;
    logic [BW-1:0] wl_req_blk;
    logic [ICW-1:0] wl_req_row;
    logic [BW-1:0] wl_done_blk;   // 前向声明：pass_go 要用（避免先用后声明）

    // ---- 权重“行槽”分配 --------------------------------------------------
    //  一个 pass 占 cin 行；下一 pass 装到 [row_base+cin, row_base+2*cin)，
    //  只要 2*cin 还塞得进 CIN_MAX，就能在**本 pass 计算期间后台装载**（不阻塞）。
    //  塞不下（cin>176，即 CIN_MAX/cin < 2）时 next_row_base 回 0：
    //  等本 pass 算完再装，退化成原来的“pass 末尾串行重装”。
    wire [ICW:0] row_base_d = {1'b0, row_base_r} + {1'b0, cin_r};
    wire         preload_ok = (row_base_d + {1'b0, cin_r}) <= CIN_MAX;
    wire [ICW-1:0] next_row_base = preload_ok ? row_base_d[ICW-1:0]
                                              : {ICW{1'b0}};

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
            wl_req_row  <= {ICW{1'b0}};
            row_base_r  <= {ICW{1'b0}};
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
            ctrl_done    <= 1'b0;
            wl_req_pulse <= 1'b0;      // 真 1 拍脉冲（loader 在 WL_IDLE 收）
            // ---- pass 一启动就预装下一 pass 的权重 ----
            // 行槽够（preload_ok）时 loader 在后台写下一槽，计算照跑；
            // 不够时这里不发请求，等 ST_PASS→ST_NEXT 再串行装（见下）。
            if (pass_go && ((blk_r + 3'd1) < blk_cnt_r) && preload_ok) begin
                wl_req_pulse <= 1'b1;
                wl_req_blk   <= blk_r + 3'd1;
                wl_req_row   <= next_row_base;
            end
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
                        row_base_r  <= {ICW{1'b0}};
                        wl_req_row  <= {ICW{1'b0}};
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
                            row_base_r   <= next_row_base;
                            // 没预装（行槽不够）→ 现在串行装下一 pass
                            if (!preload_ok) begin
                                wl_req_pulse <= 1'b1;
                                wl_req_blk   <= blk_r + 3'd1;
                                wl_req_row   <= next_row_base;
                            end
                            state        <= ST_NEXT;
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
    // 4. 输入像素缓冲：256 × 64 bit，**显式 XPM SDP RAM（1 块 RAMB36）**
    // ========================================================================
    //  ⚠ 2026-10-27 第 2 轮：原 `(* ram_style="distributed" *) reg [63:0]
    //    px_mem [0:255]`（RAM64M×84 = **336 个 LUT**）改成显式 XPM。
    //    读延迟**前后都是 1 拍**，证据：
    //      改前 437: always_ff @(posedge ap_clk) p_ram_q <= px_mem[nxt_addr];
    //                ⇒ 同步读 = 1 拍（写口同 always；RAM 无复位、p_ram_q 单源）
    //      改后 XPM .READ_LATENCY_B(1) ⇒ doutb 就是同一个寄存器位置，
    //                `assign p_ram_q = px_dout;` 纯连线
    //    ⇒ 消费者 `p_word = bp_hit_q ? bp_data_q : p_ram_q` 与相位完全不变。
    //    写口是**整字写**（64 bit 一次），所以 BYTE_WRITE_WIDTH_A=64 ⇒ wea 1 位，
    //    用 ena 做写使能，语义与原来 `if (in_r_hs) px_mem[..] <= rdata` 等价。
    xpm_memory_sdpram #(
        .MEMORY_SIZE            (256 * 64),     // 16,384 bit
        .MEMORY_PRIMITIVE       ("block"),      // ← 强制 BRAM
        .CLOCKING_MODE          ("common_clock"),
        .ECC_MODE               ("no_ecc"),
        .MEMORY_INIT_FILE       ("none"),
        .MEMORY_INIT_PARAM      ("0"),
        .USE_MEM_INIT           (0),
        .WAKEUP_TIME            ("disable_sleep"),
        .AUTO_SLEEP_TIME        (0),
        .MESSAGE_CONTROL        (0),
        .USE_EMBEDDED_CONSTRAINT(0),
        .MEMORY_OPTIMIZATION    ("true"),
        .CASCADE_HEIGHT         (0),
        .SIM_ASSERT_CHK         (0),
        .WRITE_PROTECT          (1),
        .WRITE_DATA_WIDTH_A     (64),
        .BYTE_WRITE_WIDTH_A     (64),           // 整字写 ⇒ wea 1 位
        .ADDR_WIDTH_A           (8),
        .RST_MODE_A             ("SYNC"),
        .READ_DATA_WIDTH_B      (64),
        .ADDR_WIDTH_B           (8),
        .READ_RESET_VALUE_B     ("0"),
        .READ_LATENCY_B         (1),            // ← 1 拍，与原来一致
        .WRITE_MODE_B           ("read_first"), // 同址同拍读旧值，与原来 nonblocking 一致
        .RST_MODE_B             ("SYNC")
    ) u_pxmem (
        .sleep          (1'b0),
        // 写口：地址 = {f_pix[1:0], f_rd}（= 原来的拼接，8 位）
        .clka           (ap_clk),
        .ena            (in_r_hs),
        .wea            (1'b1),
        .addra          ({f_pix[1:0], f_rd}),
        .dina           (m_axi_in_rdata),
        .injectsbiterra (1'b0),
        .injectdbiterra (1'b0),
        // 读口：原来是无条件读 ⇒ enb/regceb 恒 1
        .clkb           (ap_clk),
        .rstb           (1'b0),
        .enb            (1'b1),
        .regceb         (1'b1),
        .addrb          (nxt_addr),
        .doutb          (px_dout),
        .sbiterrb       (),
        .dbiterrb       ()
    );

    assign p_ram_q = px_dout;      // 纯连线（原来的 always_ff 已删除）

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
    logic              par_a, par_b;
    logic              last_a, last_b;
    logic              v_a, v_b;
    logic [31:0]       pixoff_a, pixoff_b;
    logic [BW-1:0]     blk_a, blk_b;

    (* use_dsp = "yes" *) logic signed [15:0] prod_b [0:LANES-1];
    logic signed [25:0] acc_e    [0:LANES-1];
    logic signed [25:0] acc_o    [0:LANES-1];
    logic signed [26:0] sum27    [0:LANES-1];
    logic signed [31:0] v32      [0:LANES-1];
    logic [7:0]         sat_b    [0:LANES-1];
    // 第三轮瘦身 B：bias 不再是“32 lane × 6 pass 的 32b FF 阵列 + 6:1 mux”
    // （网表 bias_lane 占 3,288 LUT cell / 6,144 FF），改成**每 lane 一块
    // BLKMAX×32 的分布式 RAM**，声明在下面 generate 的 g_lane 里（见 §10 注释）。

    // 收尾流水 valid / 块信息（供 generate 与 E1/E2/E3 共用）
    logic          e1_vld, e2_vld;
    logic [BW-1:0] ep_blk_q;
    logic [31:0]   ep_gbase;

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            p_a <= 8'sd0;
            par_a <= 1'b0; par_b <= 1'b0;
            last_a <= 1'b0; last_b <= 1'b0;
            v_a <= 1'b0; v_b <= 1'b0;
            pixoff_a <= 32'd0; pixoff_b <= 32'd0;
            blk_a <= 3'd0; blk_b <= 3'd0;
        end else if (start_pulse || pass_go) begin
            p_a <= 8'sd0;
            par_a <= 1'b0; par_b <= 1'b0;
            last_a <= 1'b0; last_b <= 1'b0;
            v_a <= 1'b0; v_b <= 1'b0;
        end else begin
            v_a <= issue_en;
            if (issue_en) begin
                p_a      <= p_byte;
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
    // 6. 权重 RAM：一块 WW(=256)×CIN_MAX 的 **BRAM**（字节写 + 同步读 1 拍）
    //    行 = 一个 ic；行内 [lane*8 +: 8] = 该 lane 在该 ic 上的 8-bit 权重。
    //    · 旧版是 32 个 44×64 LUTRAM（3.1k LUTRAM + 每 lane 一个 8:1 字节 mux）；
    //    · 新版把 32 lane 压进同一行的 32 个字节，容量 90,112 bit，
    //      Vivado 推断成 **4 × RAMB36**（512×72 的 SDP 4 块，36-bit 组各放 4 个
    //      8-bit lane，1 bit 空着），约 +4 Block RAM Tile。
    //    · 读延迟仍是 **1 拍**（和旧 LUTRAM 版一样，旧版 w_word_q 也是寄存器输出）：
    //        issue 拍：w_rd_addr = row_base_r + ic_cur 打进 BRAM
    //        下一拍 ：w_row_q 有效，与 p_a（同一个 issue 拍寄存的像素字节）对齐
    //      所以 II=1 不变、不需要额外补拍。
    //    · 写口每拍只写 1 字节（一个 lane）；8 字节/beat 要 8 拍。多 pass 时靠
    //      “行槽”把这段装载藏进上一个 pass 的计算里（见 §2/§9）。
    //    ⚠ RAM 模板铁律（踩过）：w_row_q **只能**由下面这一个 always 的
    //      `w_row_q <= w_mem[w_rd_addr]` 赋值——不要加复位、不要旁路、不要第二源，
    //      否则 Vivado 不推断 BRAM，会退化成 FF + 256:1 读 mux。
    // ========================================================================
    // 写口（由 §9 的装载 FSM 用组合信号驱动；one-hot 字节使能 = 1 个 lane）
    logic             w_ld_we;
    logic [LANES-1:0] w_ld_be;
    logic [ICW-1:0]   w_ld_addr;
    logic [7:0]       w_ld_data;
    // 读口
    logic [ICW-1:0]   w_rd_addr;
    logic [WW-1:0]    w_row_q;      // 32 lane × 8-bit 的一整行

    assign w_rd_addr = row_base_r + ic_cur;

    // ========================================================================
    // 6b. 权重 RAM 的物理实现：**显式 XPM SDP RAM × GPN（=4）**，不再靠推断
    //     ⚠⚠ 2026-10-27：原来是一个 `(* ram_style="block" *) reg [255:0]
    //        w_mem [0:351]`，综合被 Vivado 否决：
    //          WARNING: [Synth 8-6849] Infeasible attribute ram_style = "block"
    //                   set for RAM "design_1_k1_wrap_1/w_mem_reg"   ×4
    //        ⇒ 退回 LUTRAM，实测吃掉 **3,072 个 LUT**
    //          （RAM32X1D×256 + RAM64X1D×256 + RAM128X1D×512，占 k1 全部
    //           LUTRAM 4,248 的 72.3%，占 k1 LUT 15,764 的 19.5%）。
    //        与本轮 k3 `wgt_mem`（576 bit 宽）完全同一个病：**位宽远超
    //        RAMB36 的 72 bit，Vivado 的 BRAM 推断不沿位宽方向拆块**；
    //        而且在这个引擎里连"按 lane 拆成窄数组"也仍被否决（k3 实测）。
    //        ⇒ 结论同 k3：**直接上显式原语，把选择权收回**。
    //     拆法：256 bit = **4 块 × 64 bit**（每块 8 个 lane = 8 个字节）。
    //     读延迟**【不变，仍是 1 拍】**（见下面 READ_LATENCY_B 的推导）。
    // ========================================================================
    logic [GPN-1:0][WBW-1:0] w_q;   // 4 块 BRAM 的**寄存输出**（packed，gq=3 在高位）

    genvar gq;
    generate
        for (gq = 0; gq < GPN; gq = gq + 1) begin : g_wmem
            xpm_memory_sdpram #(
                .MEMORY_SIZE            (WDW * WBW),    // 512 行 × 64 bit
                .MEMORY_PRIMITIVE       ("block"),      // ← 强制 BRAM
                .CLOCKING_MODE          ("common_clock"),
                .ECC_MODE               ("no_ecc"),
                .MEMORY_INIT_FILE       ("none"),
                .MEMORY_INIT_PARAM      ("0"),
                .USE_MEM_INIT           (0),
                .WAKEUP_TIME            ("disable_sleep"),
                .AUTO_SLEEP_TIME        (0),
                .MESSAGE_CONTROL        (0),
                .USE_EMBEDDED_CONSTRAINT(0),
                .MEMORY_OPTIMIZATION    ("true"),
                .CASCADE_HEIGHT         (0),
                .SIM_ASSERT_CHK         (0),
                .WRITE_PROTECT          (1),
                .WRITE_DATA_WIDTH_A     (WBW),          // 64
                .BYTE_WRITE_WIDTH_A     (8),            // → wea 8 位 = 8 个 lane
                .ADDR_WIDTH_A           (ICW),          // 9（WDW=512=2^9）
                .RST_MODE_A             ("SYNC"),
                .READ_DATA_WIDTH_B      (WBW),          // 64
                .ADDR_WIDTH_B           (ICW),          // 9
                .READ_RESET_VALUE_B     ("0"),
                .READ_LATENCY_B         (1),            // ← 1 拍，与原来完全一致
                .WRITE_MODE_B           ("read_first"), // 与分布式 RAM 同语义
                .RST_MODE_B             ("SYNC")
            ) u_wmem (
                .sleep          (1'b0),
                // 写口：ena=装载有效；wea=这 8 个 lane 里哪一个是本拍的写目标
                .clka           (ap_clk),
                .ena            (w_ld_we),
                .wea            (w_ld_be[gq*8 +: 8]),
                .addra          (w_ld_addr),
                .dina           ({8{w_ld_data}}),       // 只有 wea 选中的字节真写
                .injectsbiterra (1'b0),
                .injectdbiterra (1'b0),
                // 读口：**读使能 = issue_en**（原来 `if (issue_en) w_row_q <= ...`）
                .clkb           (ap_clk),
                .rstb           (1'b0),                 // 不复位（原来就没有复位）
                .enb            (issue_en),
                .regceb         (1'b1),
                .addrb          (w_rd_addr),
                .doutb          (w_q[gq]),
                .sbiterrb       (),
                .dbiterrb       ()
            );
        end
    endgenerate

    // 纯拼线（**不是寄存器**！）：位布局与原来完全一致，lane gi 在 [gi*8 +: 8]，
    //   所以 §5 的 `w_byte = w_row_q[gi*8 +: 8]` 一个字都不用改。
    assign w_row_q = w_q;

    // 偏置装载写口（在 generate 之前声明，供每个 lane 的 bias 写解码使用）
    logic             bl_weA, bl_weB;
    logic [7:0]       bl_idxA, bl_idxB;
    logic signed [31:0] bl_dataA, bl_dataB;
    // 第三轮瘦身 B：一个 64-bit beat 载着两个 int32（ocA=2i、ocB=2i+1）。
    //   · oc 偶 ⇒ lane 偶，oc 奇 ⇒ lane 奇（lane=oc[4:0]）；
    //   · 所以 **A 路只写偶 lane、B 路只写奇 lane**，两者永不写同一个 lane；
    //   · 于是每个 lane 的 bias RAM 仍然只有 **一个写口**（偶数 lane 接 A、奇数 lane
    //     接 B，在 generate 里按 gi 的奇偶硬选择），既不用 2 写口，也不用合并丢数据。
    wire [4:0] bl_wlaneA = bl_idxA[4:0];   // = 2i mod 32（偶）
    wire [2:0] bl_wpassA = bl_idxA[7:5];   // = oc/32
    wire [4:0] bl_wlaneB = bl_idxB[4:0];   // = (2i+1) mod 32（奇）
    wire [2:0] bl_wpassB = bl_idxB[7:5];

    genvar gi;
    generate
        (* keep_hierarchy = "yes" *)
        for (gi = 0; gi < LANES; gi = gi + 1) begin : g_lane
            // 本 lane 在该 ic 上的权重字节（w_row_q 的常量切片，纯连线）
            wire [7:0] w_byte = w_row_q[gi*8 +: 8];

            // ---- 偏置：每个 lane 一块 BLKMAX×32 的**分布式 RAM**（第三轮瘦身 B）----
            //  旧版：bias_lane[gi][kk] 是 6 个 32b FF（全模块 6,144 FF），收尾时用
            //  6:1 mux 选 pass ⇒ 网表 bias_lane 占 3,288 个 LUT cell。
            //  新版：6 条 × 32 bit 的 LUTRAM（写 1 口 + 组合读 1 口），
            //    · 写口：偶数 lane 只接 A 路、奇数 lane 只接 B 路（见上面 `bl_wlaneA`/`bl_wlaneB` 的说明），
            //      所以这个数组**只有一条写语句** = 真正的单写口 RAM 模板；
            //    · 读口：E2 拍**组合读** `bias_mem[ep_blk_q]`（地址 ep_blk_q 在 E1 前
            //      就已稳定，组合读不增加延迟，也省掉 32×32 输出 FF）；
            //    · **无复位**（RAM 一旦带复位就不推断），未装载的尾部 lane 读到 X，
            //      但 E3 的 lane_ok 会把它强制成 0，绝不会进输出（见 §10）。
            (* ram_style = "distributed" *) reg signed [31:0] bias_mem [0:BLKMAX-1];

            if ((gi % 2) == 0) begin : g_bias_wre
                always_ff @(posedge ap_clk) begin
                    if (bl_weA && (bl_wlaneA == gi))
                        bias_mem[bl_wpassA] <= bl_dataA;
                end
            end else begin : g_bias_wro
                always_ff @(posedge ap_clk) begin
                    if (bl_weB && (bl_wlaneB == gi))
                        bias_mem[bl_wpassB] <= bl_dataB;
                end
            end

            wire signed [31:0] bias_rd;
            assign bias_rd = bias_mem[ep_blk_q];

            // 乘法器：use_dsp 强制映射到 DSP48；**组合使用**（不启用 DSP 内部
            // A/B/M/P 寄存器），所以 prod_b 仍然只延迟 1 拍，像素/权重对齐不变。
            (* use_dsp = "yes" *) wire signed [15:0] mul_prod =
                p_a * $signed(w_byte);

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
                    v32[gi] <= $signed({{5{sum27[gi][26]}}, sum27[gi]}) + bias_rd;
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
    // 8. 输出块队列：16 条 × 96 bit 的**简单双口分布式 RAM** + AXI4 写排空
    //
    //  旧版（第一/二轮）：4 槽 × 4 组 × (32b 地址 + 64b 数据) 的**寄存器阵列**
    //      = 1,536 FF，外加写侧 4 个写口的 mux + 读侧 16:1 读 mux；
    //      网表里 ob_* 占 **4,190 个 LUT cell**，是全模块最大的一刀（docs §15.3）。
    //  新版：16 条 × 96 bit = {addr[31:0], data[63:0]}，地址 = {槽[1:0], 组[1:0]}。
    //      · 容量 1,536 bit ⇒ LUTRAM 约 50–150 个 LUT（`ram_style="distributed"`，1,536 bit ÷ 16–32 bit/LUT）；
    //      · 只有 **1 个写口** ⇒ push 的 4 组必须**串行 4 拍**写入（余量见下）；
    //      · 只有 1 个读口 + **1 拍寄存器输出**，用 wd_rdy_q 保证“地址稳一拍”
    //        之后才允许发 AW/W（AXI 上 AWADDR/WDATA 必须与 valid 同拍有效）。
    //
    //  ⚠ RAM 推断铁律（和 px_mem 一模一样，这个模块已经踩过三次）：
    //     写口和读口在**同一个 always_ff** 里；读寄存器 ob_q **只有**
    //     `ob_q <= ob_mem[...]` 这一个赋值源：**不复位、不旁路、不加第二源**。
    //     写口用 `if (ob_we)` 作写使能，是 RAM 模板允许的形式。
    //     判据：综合后 `LUT as Memory` 应随这 1,536 bit 增长（~50–150），
    //           FF 应从 1,536 降到 ~400；若 FF 没降就是没推断成 RAM。
    //
    //  ⚠ 串行写入的余量（为什么不会反压计算）：
    //    · 一个 push = 一个输出块 = 4 条记录；写口 1 条/拍 ⇒ 占用 **4 拍**；
    //    · 同一 pass 内相邻块 start 间隔 = **cin 个 issue 拍**，push 也间隔 cin 拍
    //      （push 比块尾晚固定 3 拍，间隔不变）；
    //    · 契约保证 cin ≥ 8（cin%8==0），所以**最坏** cin=8 时写口占用率 = 4/8 = 50%，
    //      cin=64 时 6.25%，cin=352 时 1.1% —— 写口永远在下一块 push 前 4 拍空闲；
    //    · 队列 4 槽 = 16 条 ≥ 一块的 4 条，且 cin=8 时仍有 4 拍空档，所以
    //      ob_stg 不会被下一块覆盖（仿真期断言 `push_ok && ob_wr_busy` 会报错）；
    //    · pass 边界：push 拍就把 4 条**快照**进 ob_stg（4×96 FF），所以紧接着的
    //      pass_go 把 v32/out_word 清 0 也不影响这 4 拍写（这是必须快照的原因）；
    //      pass_go **不**清 ob_wr_busy/ob_wg/ob_wr/ob_rd——4 拍写完照常提交。
    // ========================================================================
    localparam int OB_W  = 96;           // 一条记录 = {addr[31:0], data[63:0]}
    localparam int OB_DP = OBUF_N * 4;   // 16 条 = 4 槽 × 4 组

    (* ram_style = "distributed" *) reg [OB_W-1:0] ob_mem [0:OB_DP-1];

    logic [1:0]        ob_wr, ob_rd;      // 槽指针
    logic [2:0]        ob_n [0:OBUF_N-1]; // 每槽有效组数（1..4）——普通 FF，4×3
    logic [OBUF_N-1:0] ob_rdy;            // 每槽 4 条都写完（提交标志），4 FF
    logic              ob_wr_busy;        // 4 拍串行写进行中
    logic [1:0]        ob_wg;             // 串行写的组号 0..3
    logic [OB_W-1:0]   ob_stg [0:3];      // push 拍快照（4×96 = 384 FF）
    logic [2:0]        ob_n_stg;
    logic              wr_pop;

    logic [1:0]        wd_j;
    logic              wd_aw, wd_w;
    logic              wd_rdy_q;          // 读数据对齐（地址稳 1 拍）

    wire [3:0]      ob_waddr = {ob_wr, ob_wg};
    wire [OB_W-1:0] ob_wdata = ob_stg[ob_wg];
    wire            ob_we    = ob_wr_busy;

    // ---- RAM 本体：写口 + 读寄存器（read-first 简单双口，唯一赋值源）----
    logic [OB_W-1:0] ob_q;
    always_ff @(posedge ap_clk) begin
        if (ob_we) ob_mem[ob_waddr] <= ob_wdata;      // 写口：1 条/拍
        ob_q <= ob_mem[{ob_rd, wd_j}];                // 读口：唯一源，无复位/旁路
    end

    // ---- 队列记账：push 拍快照 → 4 拍串行写 → 提交 ob_rdy ----
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            ob_wr <= 2'd0; ob_rd <= 2'd0; ob_cnt <= 3'd0;
            ob_rdy <= '0; ob_wr_busy <= 1'b0; ob_wg <= 2'd0; ob_n_stg <= 3'd0;
            for (jj = 0; jj < 4; jj = jj + 1) ob_stg[jj] <= {OB_W{1'b0}};
        end else if (start_pulse) begin
            ob_wr <= 2'd0; ob_rd <= 2'd0; ob_cnt <= 3'd0;
            ob_rdy <= '0; ob_wr_busy <= 1'b0; ob_wg <= 2'd0; ob_n_stg <= 3'd0;
        end else begin
            if (push_ok) begin
                // ep_gbase / out_word 在 push 这一拍稳定；README：这里**必须**快照，
                // 因为下一个 pass 的 pass_go 会把 v32 清 0，而 4 拍写还没写完。
                // ⚠ 改成**位选赋值**而不是拼接 `{ep_gbase + jj*hw8_r, out_word}`：
                //   拼接的操作数是自决定位宽，加法按 32 位求值（这里 3*hw8 ≤ 2.46M，
                //   恰好不会截断，属安全写法），但 tools/check_concat_width.py 会报。
                //   两条位选与原来逐位相同、综合结果不变，同时让门禁通过。
                for (jj = 0; jj < 4; jj = jj + 1) begin
                    ob_stg[jj][63:0]  <= out_word[jj];
                    ob_stg[jj][95:64] <= ep_gbase + jj * hw8_r;
                end
                ob_n_stg   <= push_n;
                ob_wr_busy <= 1'b1;
                ob_wg      <= 2'd0;
            end else if (ob_wr_busy) begin
                if (ob_wg == 2'd3) begin
                    // 第 4 条写完 ⇒ 本槽提交，指针前进
                    ob_wr_busy   <= 1'b0;
                    ob_wg        <= 2'd0;
                    ob_n[ob_wr]  <= ob_n_stg;
                    ob_rdy[ob_wr] <= 1'b1;
                    ob_wr        <= ob_wr + 2'd1;
                end else begin
                    ob_wg <= ob_wg + 2'd1;
                end
            end
            if (wr_pop) begin
                ob_rd           <= ob_rd + 2'd1;
                ob_rdy[ob_rd]   <= 1'b0;
            end
            ob_cnt <= ob_cnt + {2'd0, push_ok} - {2'd0, wr_pop};
        end
    end

    // ---- 排空：只读**已提交**的槽；地址稳一拍后（wd_rdy_q）才发 AW/W ----
    wire cur_blk = (ob_cnt != 3'd0) && ob_rdy[ob_rd];
    wire wr_ent  = cur_blk && wd_rdy_q;

    assign m_axi_out_awvalid = wr_ent && !wd_aw;
    assign m_axi_out_awaddr  = ob_q[95:64];
    assign m_axi_out_awlen   = 8'd0;
    assign m_axi_out_wvalid  = wr_ent && !wd_w;
    assign m_axi_out_wdata   = ob_q[63:0];
    assign m_axi_out_wstrb   = 8'hFF;
    assign m_axi_out_wlast   = 1'b1;
    assign m_axi_out_bready  = 1'b1;

    wire out_aw_hs = m_axi_out_awvalid && m_axi_out_awready;
    wire out_w_hs  = m_axi_out_wvalid  && m_axi_out_wready;
    // 本组 AW 与 W 都握手 ⇒ 前进到下一组（或弹出一个槽）
    wire grp_adv   = wr_ent && (wd_aw || out_aw_hs) && (wd_w || out_w_hs);

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            wd_j <= 2'd0; wd_aw <= 1'b0; wd_w <= 1'b0; wr_pop <= 1'b0;
            wd_rdy_q <= 1'b0;
        end else if (start_pulse) begin
            wd_j <= 2'd0; wd_aw <= 1'b0; wd_w <= 1'b0; wr_pop <= 1'b0;
            wd_rdy_q <= 1'b0;
        end else begin
            wr_pop   <= 1'b0;
            // 读地址（ob_rd/wd_j）变了 ⇒ 下一拍 ob_q 还不对，先不许发 AW/W。
            // ⚠ 必须同时盖住 **grp_adv 和 wr_pop 两拍**：wr_pop 是**寄存**的，
            //   换组（wd_j 变）在 grp_adv 那一拍，弹槽（ob_rd 变）却晚一拍。
            //   只盖 grp_adv 会在“弹槽后立刻有下一块已提交”时（cin=8、队列积 2+ 块）
            //   用旧槽的 ob_q 发出一拍**错地址**（数据对、地址旧）——2026-10-26 实测
            //   dbg_pix64/dbg_ones 就栽在这里（pixel 1 的数据被写到 pixel 0 的地址）。
            wd_rdy_q <= ~(grp_adv || wr_pop);
            if (!cur_blk) begin
                wd_j <= 2'd0; wd_aw <= 1'b0; wd_w <= 1'b0;
            end else if (wr_ent) begin
                if (out_aw_hs) wd_aw <= 1'b1;
                if (out_w_hs)  wd_w  <= 1'b1;
                if (grp_adv) begin
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
    // 9. 权重装载（按 oc pass；写入 §6 的 BRAM，字节粒度）
    //
    //  装载量不变：每 pass 32 lane × cin 字节；但写口 1 字节/拍 ⇒ 每 beat
    //  （8 字节）要 8 拍。为了不把这段时间加到关键路径：
    //    · pass 启动时（pass_go）就把下一 pass 的请求发给 loader，写进
    //      next_row_base 那个**行槽**（与当前 pass 的 [row_base,+cin) 不重叠）；
    //    · 计算期间 loader 在后台慢慢写，pass 结束时早就写完 → ST_NEXT 立刻过；
    //    · cin > 176（CIN_MAX/cin < 2）塞不下两个行槽时，退回“pass 末尾串行装”，
    //      这时才多花装载时间（只有黄金 k1_64x64 的 +2% 口径受影响，它 cin=64
    //      有 5 个行槽，是后台装的）。
    //
    //  与输入预取的带宽协调：后台装载只在“输入已领先 ≥2 像素”时才发 AR。
    //  合并读口上输入是最高优先级（见 k1_axi_wrap.v），权重 burst 只占 1/8 带宽；
    //  这样既不会把输入 4 缓冲饿死，也不会让权重 burst 挡住 pass 启动。
    // ========================================================================
    localparam logic [1:0] WL_IDLE = 2'd0, WL_AR = 2'd1,
                           WL_R    = 2'd2, WL_WR = 2'd3;

    logic [1:0]  wl_state;
    logic [31:0] wl_addr;
    logic [4:0]  wl_lane;
    logic [GW-1:0] wl_word;
    logic [5:0]  wl_lane_lim;
    logic [BW-1:0] wl_blk;
    logic [ICW-1:0] wl_row_base;
    logic [63:0] wl_beat_q;
    logic [2:0]  wl_bcnt;
    logic        wl_last_q;
    wire  [GW-1:0] wpl = cin_r[ICW-1:3];
    wire  [7:0]    wl_rem = cout_r - {wl_req_blk, 5'b00000}; // 本 pass 剩余 oc 数
    // 输入预取领先量（像素）。只有领先 ≥2 才发权重 AR；ST_PASS 以外（ST_LOAD /
    // ST_NEXT）没有计算，不设门槛。
    wire  [31:0]   in_lead = f_next - pix_cur;
    wire           wl_ar_ok = (state != ST_PASS) || (in_lead >= 32'd2);

    // 门控一旦放开就必须保持 ARVALID 到 AR 握手（AXI 要求 valid 不得中途撤销；
    // in_lead 会随计算推进变小，不加 sticky 会违反协议 / 丢事务）。
    logic           wl_ar_seen;
    assign m_axi_w_arvalid = (wl_state == WL_AR) && (wl_ar_seen || wl_ar_ok);
    assign m_axi_w_araddr  = wl_addr;
    assign m_axi_w_arlen   = (wpl == 6'd0) ? 8'd0 : (wpl - 6'd1);
    // 一个 beat 的 8 字节要写 8 拍；只在 WL_R（收 beat）和 WL_WR 最后一拍收下一拍
    assign m_axi_w_rready  = (wl_state == WL_R) ||
                             ((wl_state == WL_WR) && (wl_bcnt == 3'd7) && !wl_last_q);

    // ---- RAM 写口（组合；RAM 在时钟沿采样）----
    assign w_ld_we   = (wl_state == WL_WR);
    assign w_ld_data = wl_beat_q[{wl_bcnt, 3'b000} +: 8];   // 本拍写第 wl_bcnt 个字节
    assign w_ld_addr = wl_row_base + {wl_word, 3'b000}
                       + {{(ICW-3){1'b0}}, wl_bcnt};
    always_comb begin
        w_ld_be = {LANES{1'b0}};
        w_ld_be[wl_lane] = 1'b1;                            // one-hot：一次只写一个 lane
    end

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            wl_state    <= WL_IDLE;
            wl_addr     <= 32'd0;
            wl_lane     <= 5'd0;
            wl_word     <= 6'd0;
            wl_blk      <= 3'd0;
            wl_row_base <= {ICW{1'b0}};
            wl_beat_q   <= 64'd0;
            wl_bcnt     <= 3'd0;
            wl_last_q   <= 1'b0;
            wl_ar_seen  <= 1'b0;
            wl_done_blk <= 3'd0;
            wl_lane_lim <= 6'd32;
            bank_ready  <= 1'b0;
        end else if (start_pulse) begin
            wl_state    <= WL_AR;
            wl_addr     <= ctrl_wgt_addr;
            wl_lane     <= 5'd0;
            wl_word     <= 6'd0;
            wl_blk      <= 3'd0;
            wl_row_base <= {ICW{1'b0}};
            wl_bcnt     <= 3'd0;
            wl_last_q   <= 1'b0;
            wl_ar_seen  <= 1'b0;
            wl_done_blk <= 3'd0;
            wl_lane_lim <= (ctrl_cout[7:0] > 8'd32) ? 6'd32 : ctrl_cout[5:0];
            bank_ready  <= 1'b0;
        end else begin
            case (wl_state)
                WL_IDLE: begin
                    if (wl_req_pulse) begin
                        wl_blk      <= wl_req_blk;
                        wl_row_base <= wl_req_row;
                        wl_lane     <= 5'd0;
                        wl_word     <= 6'd0;
                        wl_bcnt     <= 3'd0;
                        wl_ar_seen  <= 1'b0;
                        wl_lane_lim <= (wl_rem > 8'd32) ? 6'd32 : wl_rem[5:0];
                        bank_ready  <= 1'b0;
                        wl_state    <= WL_AR;
                    end
                end
                WL_AR: begin
                    if (m_axi_w_arvalid && m_axi_w_arready) begin
                        wl_ar_seen <= 1'b0;
                        wl_state   <= WL_R;
                    end else if (wl_ar_ok) begin
                        wl_ar_seen <= 1'b1;   // 锁住 ARVALID
                    end
                end
                WL_R: begin
                    if (m_axi_w_rvalid && m_axi_w_rready) begin
                        wl_beat_q <= m_axi_w_rdata;
                        wl_addr   <= wl_addr + 32'd8;
                        wl_bcnt   <= 3'd0;
                        // 本 beat 是这条 lane 的最后一 beat？
                        wl_last_q <= (wl_word == (wpl - 6'd1));
                        wl_state  <= WL_WR;
                    end
                end
                WL_WR: begin
                    if (wl_bcnt == 3'd7) begin
                        wl_bcnt <= 3'd0;
                        if (wl_last_q) begin
                            // 这一 lane 的 8 个字节写完了
                            wl_word <= 6'd0;
                            if (({1'b0, wl_lane} + 6'd1) == wl_lane_lim) begin
                                bank_ready  <= 1'b1;
                                wl_done_blk <= wl_blk;
                                wl_state    <= WL_IDLE;
                            end else begin
                                wl_lane    <= wl_lane + 5'd1;
                                wl_ar_seen <= 1'b0;
                                wl_state   <= WL_AR;
                            end
                        end else if (m_axi_w_rvalid && m_axi_w_rready) begin
                            // 第 8 个字节写的同一拍收下一 beat ⇒ 8 拍/beat，不是 9
                            wl_beat_q <= m_axi_w_rdata;
                            wl_addr   <= wl_addr + 32'd8;
                            wl_word   <= wl_word + 6'd1;
                            wl_last_q <= ((wl_word + 6'd1) == (wpl - 6'd1));
                        end else begin
                            wl_word  <= wl_word + 6'd1;
                            wl_state <= WL_R;
                        end
                    end else begin
                        wl_bcnt <= wl_bcnt + 3'd1;
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
        // 第三轮瘦身的串行写前提：push 之间必须 ≥4 拍（cin≥8 保证）。
        // 真出现说明 ctrl_cin<8 或新的块调度打破了“一块=cin 拍”，队列会丢数据。
        if (ap_rst_n && push_ok && ob_wr_busy)
            $display("[K1] ERROR: 4 拍串行写未完成又来 push（cin=%0d？LUTRAM 写口丢块）",
                     cin_r);
        if (ap_rst_n && push_ok && (ob_rdy[ob_wr] != 1'b0))
            $display("[K1] ERROR: 写到未弹出的槽（ob_wr=%0d ob_rd=%0d）", ob_wr, ob_rd);
    end
    // synopsys translate_on

endmodule
