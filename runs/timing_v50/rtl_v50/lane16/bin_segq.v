// ============================================================================
//  bin_segq.v —— v50 段描述符链队列（R4）
//  owner: IMP-B   契约：docs/V50_ARCHITECTURE.md §2 D3、§3 契约 B/D、§5 ⑤⑪
// ----------------------------------------------------------------------------
//  1. 干什么
//     v39 的 kick 是"启动一次命令"；v50 的 kick 是"**启动整条链**"。本模块：
//        读 CHAIN_HEAD → 取 10 字描述符 → 校验 → 装 band → 起单元 → 排空 band
//        → progress++ → 按 next_pa 取下一条 … → next_pa==0 时置 done（等
//        wr_all_idle，契约 ⑤）。
//     出错（地址越界 / 4 KB 未对齐 / 超时 / 金丝雀）⇒ err 置位、**停止推进**、
//     band 与 mux 被 band_abort 推回可重启状态（契约 ⑪）。
//
//  2. 时间轴（和四个单元、和包装层读引擎**共用一套**时钟使能语义）
//     `en` 就是包装层的 u_ce。所有状态更新都挂在 en 上；一次请求（*_req）在
//     第 t 个 en 拍发出，数据在第 t+1 个 en 拍回来。这样"读未命中就停 en"的
//     行为对 segq 与对单元是**同一件事**，不需要 ready/valid 握手。
//
//  3. 描述符格式（契约 B：10 字 = 40 B，4 KB 对齐；字 0..7 沿用 v39，8=next_pa，
//     9=nband|op_mask）。下面是**本文件的字段划分**：
//         字0  in_pa            输入/激活基址（DDR，本带）
//         字1  out_pa           输出基址（DDR，本带）
//         字2  {16'h0, cin}
//         字3  {h[31:16], w[15:0]}
//         字4  {16'h0, cout}
//         字5  {pad[31:24], d[23:16], s[15:8], k[7:0]}
//         字6  band_y0          本带起始**绝对行号**（R1 修订，2026-11-02）
//         字7  flags            bit0=末带 bit1=需物化 bit2=含 shuffle bit3=输出 f32
//                               **未定义位一律忽略**（R1 明确要求，不报错）
//         字8  next_pa          （0 = 链尾）
//         字9  {op_mask[31:16], nband[15:0]}
//     ⚠ 权重/PRM 基址与 shift 组**不在描述符里**（字6/7 已被 R1 定死）：
//       它们由 PS 在**段边界**写一次寄存器（0x60/0x68/0x6C/0xA0/B8/BC/C0/D8），
//       描述符只带几何 + 地址 + flags ⇒ 一条链一次提交。
//     ⚠ PRM 帧宽 = 2×VLANES 个字，由核的 PRM_W/PB_* 参数决定；**本模块不参与**
//       （它不碰参数页，只搬 band 数据），所以不存在"硬编码 16"的问题。
//
//  4. 四条状态机路径（B3 验收）
//       (a) 推进：IDLE→HDR→CHK→LOAD→RUN→DRAIN→NEXT→(HDR)…；progress 每完成
//           一条描述符 +1（= 已完成描述符数，PS 轮询 0x204）
//       (b) 链尾：NEXT 发现 next_pa==0 ⇒ TAIL；TAIL 里**必须等 wr_all_idle**
//           才置 done（契约 ⑤），否则 PS 看到 done=1 时内存还没一致
//       (c) 出错即停：任何判据失败 ⇒ ERR；ERR 里 band_abort 拉高一拍、锁
//           err_code、**不再发任何请求**；只能被新的 kick 清（不会自己爬出来）
//       (d) 重启：kick 在任意状态都有效 ⇒ 计数器/band 指针清零、err/done 清 0、
//           从 chain_head 重来（链头为 0 时直接进 TAIL = 空链）
//
//  5. 超时
//     每个非空闲状态一个 TMO_CYC 计数器（en 拍），超了 ⇒ ERR_TIMEOUT。
//     用途：AXI 挂死 / 单元不返回 done / DMA 数据不来 —— 这三种死等都会让 PS
//     的轮询永远退不出来（k1 的老教训："校验失败也要置 done"）。
// ============================================================================
`timescale 1ns / 1ps

module bin_segq #(
    parameter int     DESC_WORDS  = 10,
    parameter int     HDR_BEATS   = (DESC_WORDS * 4 + 7) / 8,  // 40 B = 5 个 64-bit 拍
    parameter longint ARENA_LO    = 64'h0000_0000,             // 已注册 arena 下界（默认）
    parameter longint ARENA_HI    = 64'hFFFF_FFFF,             // 已注册 arena 上界（默认）
    parameter int     TMO_CYC     = 1 << 20,                   // 单状态超时（en 拍）
    parameter bit     CHK_ALIGN4K = 1'b1,
    parameter [31:0]  OP_MASK_SUP = 32'h0000_00FF              // 本版支持的小算子掩码
)(
    input  logic        clk,
    input  logic        rst_n,
    input  logic        en,              // 单元时钟使能（= u_ce）

    // ---- 启动 / 配置 ----
    input  logic        kick,            // 提交链首（AP_CTRL.start 的有效沿）
    input  logic [31:0] chain_head,      // 0x200 CHAIN_HEAD
    input  logic [31:0] arena_lo,        // arena 下界（运行时）
    input  logic [31:0] arena_hi,        // arena 上界
    input  logic [31:0] band_words,      // 一个带 = 多少个 64-bit 字
    input  logic        canary_bad,      // band_buf 的 canary_err | cap_err

    // ---- 描述符取指（走包装层统一读引擎；1 拍延迟）----
    output logic        hdr_req,
    output logic [31:0] hdr_addr,
    input  logic        hdr_data,        // 下一 en 拍有效
    input  logic [63:0] hdr_q,

    // ---- band 装载（DDR → band）----
    output logic        ld_req,
    output logic [31:0] ld_addr,
    input  logic        ld_data,
    input  logic [63:0] ld_q,
    output logic        ld_we,
    output logic [31:0] ld_wa,           // band 内字节地址（带相对）
    output logic [63:0] ld_wd,

    // ---- band 排空（band → DDR）----
    output logic        dr_req,
    output logic [31:0] dr_addr,         // band 内字节地址（带相对）
    input  logic        dr_data,
    input  logic [63:0] dr_q,
    output logic        st_we,
    output logic [31:0] st_wa,           // DDR 字节地址
    output logic [63:0] st_wd,
    output logic [7:0]  st_ws,

    // ---- 单元 / 小算子 ----
    output logic        band_start,      // 本带启动（1 个 en 拍）
    input  logic        band_done,       // 本带完成
    output logic        band_abort,      // 出错：把 band/mux 推回可重启态
    output logic [15:0] band_opmask,     // 本带要做哪些小算子（给融合控制）

    // ---- 状态（PS 可见）----
    output logic [31:0] progress,        // 0x204：已完成描述符数
    output logic        done,            // 链尾完成（已等 wr_all_idle）
    input  logic        wr_all_idle,     // 写侧全静默（来自包装层）
    output logic        err,
    output logic [7:0]  err_code,
    output logic        busy,
    output logic [3:0]  state_o,         // 状态（给包装层做 band 带选择/观测）

    // ---- 当前描述符字段（给包装层做 cfg mux）----
    output logic [31:0] d_in_pa,
    output logic [31:0] d_out_pa,
    output logic [15:0] d_band_y0,       // 字6：本带起始**绝对行号**（R1 修订）
    output logic [31:0] d_flags,         // 字7：bit0=末带 bit1=需物化 bit2=shuffle bit3=输出 f32
    output logic [15:0] d_cin,
    output logic [15:0] d_cout,
    output logic [15:0] d_h,
    output logic [15:0] d_w,
    output logic [7:0]  d_k,
    output logic [7:0]  d_s,
    output logic [7:0]  d_d,
    output logic [7:0]  d_pad,
    output logic [31:0] d_next_pa,
    output logic [15:0] d_nband,
    output logic [15:0] d_op_mask
);

    // ========================================================================
    // 0. 状态编码 / 错误码
    // ========================================================================
    localparam [3:0] S_IDLE  = 4'd0,
                     S_HDR   = 4'd1,
                     S_CHK   = 4'd2,
                     S_LOAD  = 4'd3,
                     S_RUN   = 4'd4,
                     S_DRAIN = 4'd5,
                     S_NEXT  = 4'd6,
                     S_TAIL  = 4'd7,
                     S_ERR   = 4'd8;

    localparam [7:0] E_ALIGN   = 8'd1,   // 描述符地址非 4 KB 对齐
                     E_RANGE   = 8'd2,   // 描述符/数据越出 arena
                     E_TIMEOUT = 8'd3,   // 状态超时（AXI 挂死 / 单元不返回）
                     E_CANARY  = 8'd4,   // band 金丝雀被踩 / 容量错
                     E_SIZE    = 8'd5,   // nband==0 / band_words==0
                     E_OPMASK  = 8'd6,   // op_mask 里有本版不支持的位
                     E_NULL    = 8'd7;   // 必需基址为 0

    localparam [31:0] TMO_C = TMO_CYC;

    // ========================================================================
    // 1. 寄存器
    // ========================================================================
    logic [3:0]  st;
    logic [31:0] cur_pa;
    logic [31:0] prog_q;
    logic        done_q, err_q;
    logic [7:0]  err_q_code;
    logic [31:0] tmo_q;
    logic [2:0]  hdr_beat;
    logic        req_pend;
    logic [31:0] cnt_q;
    logic        start_q, abort_q;
    logic [63:0] dw [0:HDR_BEATS-1];

    logic [31:0] f_in, f_out, f_next, f_flags;
    logic [15:0] f_cin, f_cout, f_h, f_w, f_nband, f_opmask, f_y0;
    logic [7:0]  f_k, f_s, f_d, f_pad;

    logic [31:0] ld_base, st_base;

    assign progress     = prog_q;
    assign done         = done_q;
    assign err          = err_q;
    assign err_code     = err_q_code;
    assign busy         = (st != S_IDLE) && (st != S_TAIL) && (st != S_ERR);
    assign state_o      = st;
    assign band_start   = start_q;
    assign band_abort   = abort_q;
    assign band_opmask  = f_opmask;
    assign d_in_pa      = f_in;
    assign d_out_pa     = f_out;
    assign d_band_y0    = f_y0;
    assign d_flags      = f_flags;
    assign d_cin        = f_cin;
    assign d_cout       = f_cout;
    assign d_h          = f_h;
    assign d_w          = f_w;
    assign d_k          = f_k;
    assign d_s          = f_s;
    assign d_d          = f_d;
    assign d_pad        = f_pad;
    assign d_next_pa    = f_next;
    assign d_nband      = f_nband;
    assign d_op_mask    = f_opmask;

    // ---- 请求（一次 1 拍；数据下一 en 拍回来）----
    assign hdr_req = (st == S_HDR)   && !req_pend;
    assign hdr_addr= cur_pa + {26'd0, hdr_beat, 3'd0};          // + beat*8
    assign ld_req  = (st == S_LOAD)  && !req_pend;
    assign ld_addr = ld_base + {cnt_q[28:0], 3'd0};             // + word*8
    assign dr_req  = (st == S_DRAIN) && !req_pend;
    assign dr_addr = {cnt_q[28:0], 3'd0};

    // ========================================================================
    // 2. 判据（组合；S_CHK / S_NEXT 用）
    // ========================================================================
    function automatic logic is_align4k(input logic [31:0] pa);
        is_align4k = CHK_ALIGN4K ? (pa[11:0] == 12'd0) : 1'b1;
    endfunction

    // arena 包含性：pa..pa+len-1 全在 [arena_lo, arena_hi) 内
    function automatic logic in_arena(input logic [63:0] pa, input logic [63:0] len);
        logic [63:0] lo, hi, endp;
        lo   = {32'd0, arena_lo};
        hi   = {32'd0, arena_hi};
        endp = pa + len;
        in_arena = (pa >= lo) && (endp <= hi) && (endp >= pa);
    endfunction

    // 描述符校验（S_CHK）：单条优先级链，失败即 ERR + 错误码
    logic       chk_ok;
    logic [7:0] chk_err;
    always_comb begin
        chk_ok  = 1'b1;
        chk_err = 8'd0;
        if (!is_align4k(cur_pa)) begin
            chk_ok = 1'b0; chk_err = E_ALIGN;
        end else if (!in_arena({32'd0, cur_pa}, 64'd40)) begin
            chk_ok = 1'b0; chk_err = E_RANGE;
        end else if ((dw[4][47:32] == 16'd0) || (band_words == 32'd0)) begin
            chk_ok = 1'b0; chk_err = E_SIZE;
        end else if ((dw[4][63:48] & ~OP_MASK_SUP[15:0]) != 16'd0) begin
            chk_ok = 1'b0; chk_err = E_OPMASK;
        // ★ T-89：必需基址只有两个 —— 字0 in_pa、字1 out_pa。
        //   （旧版写成 dw[1][31:0] = 字2 = cin，并且要求 dw[3][63:32]
        //     = 字7 = flags 非 0 ⇒ cin=0 / flags=0 被误判 E_NULL）
        end else if ((dw[0][31:0] == 32'd0) || (dw[0][63:32] == 32'd0)) begin
            chk_ok = 1'b0; chk_err = E_NULL;
        end else if (!in_arena({32'd0, dw[0][31:0]}, {32'd0, band_words} << 3)
                     || !in_arena({32'd0, dw[0][63:32]}, {32'd0, band_words} << 3)) begin
            chk_ok = 1'b0; chk_err = E_RANGE;
        end
    end

    // ========================================================================
    // 3. 顺序逻辑（一个 always_ff 管状态 + 字段；另有一个管 band/AXI 动作）
    // ========================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            st <= S_IDLE; cur_pa <= 32'd0; prog_q <= 32'd0;
            done_q <= 1'b0; err_q <= 1'b0; err_q_code <= 8'd0;
            tmo_q <= 32'd0; hdr_beat <= 3'd0; req_pend <= 1'b0; cnt_q <= 32'd0;
            start_q <= 1'b0; abort_q <= 1'b0;
            ld_base <= 32'd0; st_base <= 32'd0;
            f_in <= 32'd0; f_out <= 32'd0; f_next <= 32'd0; f_flags <= 32'd0;
            f_cin <= 16'd0; f_cout <= 16'd0; f_h <= 16'd0; f_w <= 16'd0;
            f_nband <= 16'd0; f_opmask <= 16'd0; f_y0 <= 16'd0;
            f_k <= 8'd0; f_s <= 8'd0; f_d <= 8'd0; f_pad <= 8'd0;
            for (int i = 0; i < HDR_BEATS; i++) dw[i] <= 64'd0;
        end else if (en) begin
            start_q <= 1'b0;
            abort_q <= 1'b0;

            // ---- (d) 重启：kick 在任意状态都有效（先清干净，再从链头来）----
            if (kick) begin
                err_q      <= 1'b0;
                err_q_code <= 8'd0;
                done_q     <= 1'b0;
                prog_q     <= 32'd0;
                tmo_q      <= 32'd0;
                hdr_beat   <= 3'd0;
                req_pend   <= 1'b0;
                cnt_q      <= 32'd0;
                cur_pa     <= chain_head;
                ld_base    <= 32'd0;
                st_base    <= 32'd0;
                st         <= (chain_head == 32'd0) ? S_TAIL : S_HDR;
            end else begin
                // ---- 超时（只在"真的在干活"的五个状态里计）----
                if ((st == S_HDR) || (st == S_CHK) || (st == S_LOAD) ||
                    (st == S_RUN) || (st == S_DRAIN)) begin
                    if (tmo_q >= TMO_C) begin
                        st         <= S_ERR;
                        err_q_code <= E_TIMEOUT;
                    end else begin
                        tmo_q <= tmo_q + 32'd1;
                    end
                end else begin
                    tmo_q <= 32'd0;
                end

                case (st)
                // --------------------------------------------------------
                // (a) 推进 · 取指：10 字 = 5 个 64-bit 拍
                // --------------------------------------------------------
                S_HDR: begin
                    if (!req_pend) begin
                        req_pend <= 1'b1;                    // 发请求
                    end else if (hdr_data) begin
                        dw[hdr_beat] <= hdr_q;               // 收一拍
                        req_pend     <= 1'b0;
                        if (hdr_beat == HDR_BEATS[2:0]-3'd1) begin
                            hdr_beat <= 3'd0;
                            st       <= S_CHK;
                        end else begin
                            hdr_beat <= hdr_beat + 3'd1;
                        end
                    end
                end
                // --------------------------------------------------------
                // 校验 + 字段锁存：失败 ⇒ 直接 ERR（一个请求都不发）
                // --------------------------------------------------------
                S_CHK: begin
                    // ★ T-89 / §14 权威表：dw[i] 是 **64-bit 拍**，不是"字"
                    //   字0 = dw[0][31:0]   字1 = dw[0][63:32]   ← 本文件曾把字1 漏掉
                    //   字2 = dw[1][31:0]   字3 = dw[1][63:32]
                    //   字4 = dw[2][31:0]   字5 = dw[2][63:32]
                    //   字6 = dw[3][31:0]   字7 = dw[3][63:32]
                    //   字8 = dw[4][31:0]   字9 = dw[4][63:32]
                    //   字0=in_pa(装载源) 字1=out_pa(排空基址) 字2={16'h0,cin}
                    //   字3={h,w} 字4={16'h0,cout} 字5={pad,d,s,k}
                    //   字6=band_y0(R1) 字7=flags(R1) 字8=next_pa 字9={op_mask,nband}
                    f_cin    <= dw[1][15:0];
                    f_h      <= dw[1][63:48];
                    f_w      <= dw[1][47:32];
                    f_cout   <= dw[2][15:0];
                    f_k      <= dw[2][39:32];
                    f_s      <= dw[2][47:40];
                    f_d      <= dw[2][55:48];
                    f_pad    <= dw[2][63:56];
                    f_y0     <= dw[3][15:0];                 // 字6 = band_y0（绝对行号）
                    f_flags  <= dw[3][63:32];                // 字7 = flags（未定义位**忽略**）
                    f_next   <= dw[4][31:0];
                    f_nband  <= dw[4][47:32];
                    f_opmask <= dw[4][63:48];
                    f_in     <= dw[0][31:0];
                    f_out    <= dw[0][63:32];                // 字1 = out_pa

                    if (!chk_ok) begin
                        st         <= S_ERR;
                        err_q_code <= chk_err;
                    end else begin
                        ld_base <= dw[0][31:0];
                        st_base <= dw[0][63:32];         // 字1 = out_pa
                        cnt_q   <= 32'd0;
                        tmo_q   <= 32'd0;
                        st      <= S_LOAD;
                    end
                end
                // --------------------------------------------------------
                // (a) 推进 · 装 band（DDR→band）：band_words 个 64-bit 字
                // --------------------------------------------------------
                S_LOAD: begin
                    if (!req_pend) begin
                        req_pend <= 1'b1;
                    end else if (ld_data) begin
                        req_pend <= 1'b0;
                        if (cnt_q + 32'd1 >= band_words) begin
                            cnt_q <= 32'd0;
                            tmo_q <= 32'd0;
                            st    <= S_RUN;
                        end else begin
                            cnt_q <= cnt_q + 32'd1;
                        end
                    end
                end
                // --------------------------------------------------------
                // (a) 推进 · 起单元（一条 band_start 脉冲），等 band_done
                // --------------------------------------------------------
                S_RUN: begin
                    if (tmo_q == 32'd1) start_q <= 1'b1;   // 进状态后的第一个 en 拍
                    if (band_done) begin
                        tmo_q <= 32'd0;
                        st    <= S_DRAIN;
                    end
                end
                // --------------------------------------------------------
                // (a) 推进 · 排空 band（band→DDR）
                // --------------------------------------------------------
                S_DRAIN: begin
                    if (!req_pend) begin
                        req_pend <= 1'b1;
                    end else if (dr_data) begin
                        req_pend <= 1'b0;
                        if (cnt_q + 32'd1 >= band_words) begin
                            cnt_q <= 32'd0;
                            tmo_q <= 32'd0;
                            st    <= S_NEXT;
                        end else begin
                            cnt_q <= cnt_q + 32'd1;
                        end
                    end
                end
                // --------------------------------------------------------
                // (a) 推进 / (b) 链尾
                // --------------------------------------------------------
                S_NEXT: begin
                    prog_q <= prog_q + 32'd1;              // 已完成描述符数
                    hdr_beat <= 3'd0;
                    req_pend <= 1'b0;
                    if (f_next == 32'd0) begin
                        // (b) 链尾：这里**不**置 done，交给 TAIL 等 wr_all_idle
                        st <= S_TAIL;
                    end else if (!is_align4k(f_next)) begin
                        st         <= S_ERR;
                        err_q_code <= E_ALIGN;
                    end else if (!in_arena({32'd0, f_next}, 64'd40)) begin
                        st         <= S_ERR;
                        err_q_code <= E_RANGE;
                    end else begin
                        cur_pa <= f_next;
                        st     <= S_HDR;
                    end
                end
                S_TAIL: begin
                    // (b) 链尾 done：写侧全静默才允许上报（契约 ⑤）
                    if (wr_all_idle) done_q <= 1'b1;
                end
                // --------------------------------------------------------
                // (c) 出错即停：锁码 + 拉一拍 band_abort + 不再发请求
                //     done 也置 1（让 PS 的轮询能退出），但 err 同时为 1
                // --------------------------------------------------------
                S_ERR: begin
                    err_q   <= 1'b1;
                    abort_q <= 1'b1;
                    done_q  <= 1'b1;
                    req_pend<= 1'b0;
                    if (err_q_code == 8'd0) err_q_code <= E_TIMEOUT;
                end
                default: st <= S_IDLE;
                endcase

                // ---- 金丝雀 / 容量错：任何状态立刻停（优先级最高）----
                if (canary_bad && (st != S_ERR)) begin
                    st         <= S_ERR;
                    err_q_code <= E_CANARY;
                end
            end
        end
    end

    // ========================================================================
    // 4. 数据落地：请求后一拍的数据 → band 写口 / AXI 写口
    // ========================================================================
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ld_we <= 1'b0; ld_wa <= 32'd0; ld_wd <= 64'd0;
            st_we <= 1'b0; st_wa <= 32'd0; st_wd <= 64'd0; st_ws <= 8'd0;
        end else if (en) begin
            ld_we <= 1'b0;
            st_we <= 1'b0;
            if ((st == S_LOAD) && req_pend && ld_data) begin
                ld_we <= 1'b1;
                ld_wa <= {cnt_q[28:0], 3'd0};                 // band 内相对字节地址
                ld_wd <= ld_q;
            end
            if ((st == S_DRAIN) && req_pend && dr_data) begin
                st_we <= 1'b1;
                st_wa <= st_base + {cnt_q[28:0], 3'd0};       // DDR 字节地址
                st_wd <= dr_q;
                st_ws <= 8'hFF;
            end
        end
    end

    // ========================================================================
    // 5. 仿真期不变量
    // ========================================================================
    // synthesis translate_off
    always @(posedge clk) begin
        if (rst_n && en) begin
            if (err_q && (st != S_ERR) && !kick)
                $error("bin_segq: err 置位但状态已离开 S_ERR（出错即停被破坏）");
            if (kick && (st == S_ERR))
                $display("bin_segq: kick 重启 err 状态 @%0t", $time);
        end
    end
    // synthesis translate_on

endmodule
