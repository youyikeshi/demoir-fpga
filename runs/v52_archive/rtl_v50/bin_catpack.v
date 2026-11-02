// ============================================================================
//  bin_catpack.v —— 多源 float32 激活 → 二值打包 64 位字（`cat` + `binarize_pack` 融合）
//
//  ── 它替代 PS 上的哪两步 ───────────────────────────────────────────────────
//  bin_net 里 110 处 `cat → conv` 的形状是：
//        parts = [T0, T1, ...]                     （各段 float32，CHW 连续）
//        x     = np.concatenate(parts, 0)          ← ① 2.44 s（1003.6 MB 往返）
//        A     = quant_pack(x, adj)                ← ② 468.7 MB 读 + 19 MB 写
//  本单元把 ①② 合成**一趟**：按段逐通道读源 float32，逐通道比较，直接写 64 位字。
//  ⇒ **DDR 里再也没有"拼接后的大张量"**（省 1003.6 MB），PS 侧也不再读一遍做比较。
//
//  ── 判据（与 PS 版**逐字节**相同，不是"数值近似"）────────────────────────────
//      bit (64g + 8i + k) of word (g, p)  =  (x[64g+8i+k, p] > adj[64g+8i+k])
//  其中 adj = bin_engine.pack_thresh(t_q, fs_in)（**float32 逐通道阈值**）。
//  本单元不做任何算术：读到的 32 位就是 PS 那份 float32 的**原样比特**，比较是
//  IEEE-754 的 `>`（NaN ⇒ 假，+0/-0 相等）。⇒ 输入逐位相同则输出逐位相同。
//  这也是为什么源用 float32 而不是 int8：**没有新的量化/舍入点**，
//  `quant_pack_parts` 那套"先量化成 int8 再比较"的等价性证明在这里不需要。
//
//  ── 与 bin_e1e2 的分工（两者并存，由 R_RUN_UNIT 运行期选择）────────────────
//      bin_e1e2   : 源是 **int8 C8**（conv 的 OUT 缓冲原地），全网读 153 MB ⇒ 0.19 s
//      bin_catpack: 源是 **float32 CHW**（PS 已反量化好的张量），全网读 469 MB ⇒ 0.6 s
//  真实数据流里 cat 的各段现在是 PS 上的 float32（read_out 的产物），
//  所以"今天就能接上"的是本单元；等 int8 张量池落地后 E1E2 更省（1/3 的读）。
//
//  ── 一条 64 位读口怎么喂饱 ─────────────────────────────────────────────────
//  源是通道主序（一个通道一整片 H*W），输出是像素主序（一个像素一个 64 位字），
//  所以必须转置。做法：**tile = 16 像素**（= 16×4 B = 64 B = wrapper 的一整条线，
//  见 bin_unit_axi_wrap.v 的 RD_WIN_WORDS=8 ⇒ 一条线恰好被用满，0 放大）。
//  一个 64 位字组（64 通道）按"通道字节"分 8 趟，每趟 8 个通道：
//      每个通道：读 8 拍（16 个 float32）→ 16 个比较位 → 拼进 mat 的固定切片
//      8 趟后 mat = 8 通道 × 16 像素 = 128 bit，**按位重排就是 16 个字节**
//      （纯连线，0 LUT）→ 写进 stage 的第 cb 个 128 bit 行
//      最后 flush：16 个字（纯连线拼字 + 16:1 米克斯），16 拍写完
//  每通道 9 拍（1 拍描述符 + 8 拍数据），每个 16 位输出字 ≈ 29 拍 @ 真实 cin
//  ⇒ 全网 2.38 M 个输出字约 0.69 s @100 MHz（见 REPORT.md 的拍数模型）。
//
//  ── 描述符（每个 64 位字组一张，64 项 × 8 B）────────────────────────────────
//      base_desc + (cfg_group*64 + i)*8  →  { adj[63:32], src_pa[31:0] }
//      i = 8*cb + j（cb = 通道字节 0..7，j = 字节内通道 0..7）
//      src_pa = 该通道第 0 个像素的地址（段内 = 段基址 + (c-c_off)*H*W*4）
//  ⇒ **逐通道一个源地址**：段边界落在任何地方都行（不要求 C%8，也不限制段数），
//     这是本单元比 E1E2 的"8 lane × 8 通道连续"更宽的地方（描述符更贵，但不挑形状）。
//  掩码通道（通道号 ≥ cin）**不发读**；尾部字组的补零位恒 0（mat 每趟清 0）。
//
//  ── 维护规则（踩过的坑，别改回去）──────────────────────────────────────────
//  1. stage（8×128 bit）与 mat（128 bit）**只做常量下标写**（case 译码成 8 个使能），
//     绝不写 `mem[base + idx]` 这类变址数组 ⇒ 不会撞 8-4767（RAM 落回寄存器）。
//     （E1E2 的 acc 就是栽在"带异步复位 + 变址写"上，溶成 16k FF，吃 6 ns。）
//  2. 写口一次只写**整字**（mem_wr_strb ≡ 8'hFF），没有运行期字节选择。
//  3. 读口契约与 bin_e1e2 完全一致：`mem_rd_en` 当拍发地址，**下一拍** `mem_rd_data`
//     有效；miss 时由 wrapper 停 ap_ce（本单元不感知）。
//  4. 一个通道**固定发 8 拍**（尾部 tile 用重复地址当 dummy），这样 mat 的移位
//     永远是 16 bit/通道，转置连线不随 run 变化 ⇒ 没有变长桶形移位。
//  5. ST_D0 用**当拍描述符数据**直接算第 0 拍地址（省 1 拍/通道）。若时序吃紧，
//     把 src_q 打一拍、第 0 拍晚 1 拍发即可（只慢 1 拍/通道）。
//  6. **写口输出级必须打拍**（= 打一拍，受 ap_ce 控制）。纯组合写口会直接压在 wrapper 写
//     FIFO 的 D/CE 上：v28 C1 实测 **-5.766 ns**（族第一 `bf_data` 2004 条），
//     与 E1E2 当初 -5.9 ns 同病。别为了省 105 个 FF 把这级去掉。
//     寄存器带 `(* keep = "true" *)`：v29/v30 实测 opt_design 的 boundary 优化会把它
//     **吸收进 wrapper 的 pw_/bf_ 寄存器**（路径起点又变回 nb_q），keep 才真正拦住。
//  7. **读/写地址都必须走寄存器**（`raddr_q` / `waddr_q`）：
//     `src + ((p0 + 2b) << 2)`、`out + ((p0 + wp) << 3)` 这两条 32 位加法链是
//     v29 那条 26 级 / 11 个 CARRY4 路径的一半来源。
// ============================================================================
`timescale 1ns/1ps

module bin_catpack #(
    parameter int CMAX = 4096,      // 逻辑通道数上限（本网最大 544）
    parameter int TILE = 16         // 像素/tile（16*4B = 64B = 一条读线）
)(
    input  logic        ap_clk,
    input  logic        ap_rst_n,
    input  logic        ap_ce,

    input  logic        ctrl_start,
    output logic        ctrl_done,
    output logic        ctrl_busy,

    input  logic [15:0] cfg_c,          // 拼接后的逻辑通道数 cin
    input  logic [15:0] cfg_h,
    input  logic [15:0] cfg_w,
    input  logic [15:0] cfg_group,      // 本次产出第几个 64 位字组（0..8）
    input  logic [31:0] base_desc,      // 该层描述符表（cin 项，grp 段从 grp*512 起）
    input  logic [31:0] base_out,       // 本字组输出基址（= OUT + group*HW*8）

    output logic        mem_rd_en,
    output logic [31:0] mem_rd_addr,
    input  logic [63:0] mem_rd_data,
    output logic        mem_wr_en,
    output logic [31:0] mem_wr_addr,
    output logic [63:0] mem_wr_data,
    output logic [7:0]  mem_wr_strb,

    output logic [7:0]  err_code
);

    localparam [15:0] TILE16 = TILE;
    localparam [31:0] TILE32 = TILE;

    localparam logic [7:0] ERR_OK   = 8'd0;
    localparam logic [7:0] ERR_SIZE = 8'd1;
    localparam logic [7:0] ERR_NULL = 8'd2;
    localparam logic [7:0] ERR_ALIGN= 8'd5;
    localparam logic [7:0] ERR_UNIT = 8'd6;

    // ========================================================================
    // 1. 状态
    // ========================================================================
    typedef enum logic [3:0] {
        ST_IDLE = 4'd0,
        ST_CHK  = 4'd1,   // 启动校验
        ST_TINI = 4'd2,   // 一个 tile：算 run/nb，清 mat/stage
        ST_PASS = 4'd3,   // 一趟开头：j=0；若整趟被掩码 ⇒ 直接写 0 行
        ST_D0   = 4'd4,   // 描述符数据到达（latch），发本通道第 0 拍
        ST_DATA = 4'd5,   // 8 拍数据：收上一拍，发下一拍（最后一拍发下个描述符）
        ST_ROW  = 4'd6,   // mat ⇒ bytes ⇒ stage 的第 cb 行
        ST_FLUSH= 4'd7,   // 16 整字写到 DDR
        ST_TEND = 4'd8,   // 下一个 tile / 结束
        ST_DONE = 4'd9
    } state_t;

    state_t state;

    // ---- 命令寄存器 ----
    logic [15:0] c_q, h_q, w_q, grp_q;
    logic [31:0] remain_q;             // 剩余像素数（每 tile 递推，避免 32 位减法进关键路径）
    logic [31:0] hw_q;                 // H*W
    logic [31:0] dbase_q;              // = base_desc + grp*512
    logic [31:0] out_q;
    logic [31:0] p0_q;                 // 当前 tile 的起始像素
    logic [15:0] run_q;                // 本 tile 的像素数（1..TILE）
    logic [15:0] nb_q;                 // 本 tile 的有效读拍数 = ceil(run/2)

    // ---- 循环计数 ----
    logic [2:0]  cb_i;                 // 通道字节 0..7
    logic [2:0]  j;                    // 通道字节内的通道 0..7
    logic [2:0]  b;                    // 读拍 0..7
    logic [3:0]  wp;                   // flush 像素 0..TILE-1
    logic [31:0] src_q;                // 当前通道的源基址
    logic [31:0] adj_q;                // 当前通道的 float32 阈值（原样比特）
    logic [15:0] mask_q;            // ★ H：本组不再 keep（见报告 §H）   // 本 tile 的像素有效掩码（bit p = 像素 p）
    logic [3:0]   nch_q;               // 本趟使能通道数（0..8）
    logic [31:0] waddr_q;           // ★ H：本组不再 keep  // 写地址递推寄存器
    // 写口输出级（**带 keep**：见头注释第 6 条；防止 opt_design 的 boundary 优化把它吸收）
    (* keep = "true" *) logic        w_en_q;
    (* keep = "true" *) logic [31:0] w_addr_q;
    (* keep = "true" *) logic [63:0] w_data_q;
    (* keep = "true" *) logic [7:0]  w_strb_q;

    // ---- 数据通路（全部寄存器 / 常量下标 ⇒ 不可能 RAM 推断）----
    logic [127:0]  mat;                // 8 通道 × 16 像素 的比较位
    logic [15:0]   chb;                // 当前通道的 16 个比较位（移位拼）
    logic [1023:0] stage;              // 8 个通道字节 × 16 像素（= 16 个字）
    logic [127:0]  bytes_c;            // mat ⇒ 本趟的 16 个字节（纯连线）
    logic [1023:0] fw_c;               // stage ⇒ 16 个整字（纯连线）
    logic [63:0]   wdata_c;

    logic [31:0] remain_c;             // 本 tile 开始时剩余像素数（已锁存）
    logic [31:0] remain_n_c;           // 本 tile 结束后剩余（32 位减法，单独一拍）
    logic [15:0] run_c, nb_c;

    // ========================================================================
    // 2. 组合：地址 / 比较 / 重排
    // ========================================================================
    // ---- IEEE-754 float32 `a > b`（NaN ⇒ 假；±0 相等）----
    //  ★ 2026-10-31 面积刀 D：b 侧（= 本通道的 adj_q）是**每通道恒定**的，
    //    原来每个 beat 都把 b 侧重算一遍：kb(32 个条件取反) + nan_b + b[30:0]==0 + b[31]。
    //    v31 netlist 实测 `chb` 的 D 锥 = 194 cell（两个完整 f32 比较器）。
    //    现在这 4 个量在 ST_D0 用同一个 mem_rd_data[63:32] 一次算好（**不加拍**，
    //    与 adj_q 同一条边），每 beat 只算 a 侧 ⇒ 省掉一半。
    //    逐位等价：ka/kb/nan_a/nan_b/z0 的表达式一个字没改，只是 b 侧改读寄存器。
    logic [31:0] kadj_q;        // kb = adj[31] ? ~adj : (adj ^ 0x8000_0000)
    logic        nanb_q;        // adj 是 NaN
    logic        badj_zero_q;   // adj[30:0] == 0
    logic        badj_sign_q;   // adj[31]
    function automatic logic f32_gt_adj(input logic [31:0] a);
        logic [31:0] ka;
        logic        nan_a, z0;
        ka = a[31] ? ~a : (a ^ 32'h8000_0000);
        nan_a = (&a[30:23]) && (|a[22:0]);
        z0 = (a[30:0] == 31'd0) && badj_zero_q && (a[31] != badj_sign_q);
        f32_gt_adj = (ka > kadj_q) && !nan_a && !nanb_q && !z0;
    endfunction

    // 当前通道号 / 是否有效（grp 最多 8 ⇒ 6 bit 够）
    logic [31:0] lchan_c, pass_first_c;
    logic        pass_dead_c, next_en_c;
    assign pass_first_c = ({26'd0, grp_q[5:0]} << 6) + ({29'd0, cb_i} << 3);
    assign lchan_c      = pass_first_c + {29'd0, j};

    assign pass_dead_c  = (pass_first_c >= {16'd0, c_q});
    // ★ 只在**本趟内**（j<7）才继续下一个通道；j==7 必须收趟（否则 j 回绕 ⇒ 死循环）
    //   ★ 并且用**本趟使能通道数 nch_q（4 bit）**判断，不用 32 位通道号比较（时序）
    assign next_en_c    = (j != 3'd7) && (({1'b0, j} + 4'd1) < nch_q);
    logic [15:0] nch_c16;
    assign nch_c16      = (c_q > pass_first_c[15:0]) ? (c_q - pass_first_c[15:0]) : 16'd0;
    wire   [3:0] nch_n_c = (nch_c16 > 16'd8) ? 4'd8 : nch_c16[3:0];

    // ---- 本拍两个像素在 tile 内是否有效：**每 tile 预算一次**的 16 位掩码 ----
    //   原写法 `(b < nb_q) && (pb < run_q)` 是 3 个 16 位比较器 → 11 个 CARRY4，
    //   而这条链正好落在写数据锥里（v29 实测 26 级 / 11 CARRY4，起点 nb_q）。
    //   现在 ST_TINI 一次算出 mask_q（bit p = 像素 p 有效），每拍只查表（2~3 LUT）。
    logic [15:0] pb_c;
    assign pb_c = {13'd0, b} << 1;

    // 比较结果（2 个像素/拍）
    logic b0_c, b1_c;
    assign b0_c = mask_q[pb_c[3:0]]         && f32_gt_adj(mem_rd_data[31:0]);
    assign b1_c = mask_q[pb_c[3:0] + 4'd1]  && f32_gt_adj(mem_rd_data[63:32]);

    // 本通道 16 位（含本拍）
    logic [15:0] chn_c;
    assign chn_c = {b1_c, b0_c, chb[15:2]};   // 右移插入 ⇒ 第 p 位 = 像素 p

    // mat ⇒ bytes：byte(p, i) 的 bit j = 通道 (64g+8i+j) 在像素 p 的比较位
    //   mat[(7-j)*16 + p]（见头注释）⇒ bytes[8p + (7-j)] = mat[16*(7-j) + p]
    always_comb begin
        for (int p = 0; p < TILE; p++)
            for (int j = 0; j < 8; j++)           // 字节内第 j 位 = 本趟第 j 个通道
                bytes_c[8*p + j] = mat[16*(7-j) + p];
    end

    // stage ⇒ 16 个整字（纯连线；小端：字节 i = 通道 8i..8i+7）
    always_comb begin
        for (int w = 0; w < TILE; w++)
            for (int i = 0; i < 8; i++)
                fw_c[64*w + 8*i +: 8] = stage[128*i + 8*w +: 8];
    end
    assign wdata_c = fw_c[64*wp +: 64];

    // ---- 读地址（★ 走寄存器 raddr_q，见头注释第 7 条）----
    //   ST_D0 里描述符刚回来，第 0 拍的地址只能组合算（这一步无法避免，且只覆盖 1 拍）；
    //   从第 1 拍起 issue_addr = raddr_q（寄存器），每拍 +8，尾部保持（dummy 重复读）。
    logic [15:0] b2_c;
    logic        radv_c;
    assign b2_c   = {13'd0, b} + 16'd2;          // 下一拍之后那一拍的拍号
    assign radv_c = (b2_c < nb_q);
    logic [31:0] raddr_q;           // ★ H：本组不再 keep

    logic        issue_en;
    logic [31:0] issue_addr;

    always_comb begin
        issue_en   = 1'b0;
        issue_addr = 32'd0;
        case (state)
        ST_PASS: begin
            if (!pass_dead_c) begin
                issue_en   = 1'b1;
                issue_addr = dbase_q + {23'd0, cb_i, 6'd0};          // + 64*cb
            end
        end
        ST_D0: begin
            // 描述符数据当拍有效 ⇒ 直接用它算第 0 拍（p0 对齐 16 ⇒ 32 B 对齐）
            issue_en   = 1'b1;
            issue_addr = mem_rd_data[31:0] + (p0_q << 2);
        end
        ST_DATA: begin
            if (b == 3'd7) begin
                // 本通道最后一拍：顺手把**下一个通道**的描述符读发出去（省 1 拍/通道）
                if (next_en_c) begin
                    issue_en   = 1'b1;
                    issue_addr = dbase_q + (({26'd0, cb_i, j} + 32'd1) << 3);
                end
            end else begin
                issue_en   = 1'b1;
                issue_addr = raddr_q;                 // ★ 寄存器输出，不再有 32 位加法
            end
        end
        default: ;
        endcase
    end

    assign mem_rd_en   = issue_en;
    assign mem_rd_addr = issue_addr;

    // ========================================================================
    // 3'. 写口输出级（**必须打一拍**，见头注释第 6 条）
    // ------------------------------------------------------------------------
    //  踩过的坑（v28 C1，-5.766 ns）：写口的 en/addr/data/strb 原来是**纯组合**——
    //    addr = out_q + ((p0_q + wp) << 3)      16 位加法 + 移位直接进地址
    //    data = stage(1024b 常量下标拼字) → 16:1 米克斯 → 64 位数据
    //  这两条直接压在 wrapper 写 FIFO 的 D/CE 上（族第一 `bf_data` 2004 条），
    //  与 E1E2 当初 -5.9 ns 是同一个病。这里补"输出打一拍"≡ E1E2 的第 3 刀：
    //    · **受 ap_ce 控制**：wrapper 因读 miss / 写压力停 CE 时，本级与内部级
    //      一起冻结 ⇒ 不会丢写、也不会重复写（写口契约是"当拍生效"，多 1 拍无害）。
    //    · 最后一个字在 ST_FLUSH 的 wp=15 拍被捕获，下一个周期（ST_TEND）上总线，
    //      仍早于 ctrl_done（ST_DONE 之后）⇒ 对 done 语义无影响。
    // ========================================================================
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            w_en_q   <= 1'b0;
            w_addr_q <= 32'd0;
            w_data_q <= 64'd0;
            w_strb_q <= 8'd0;
        end else if (ap_ce) begin
            w_en_q   <= (state == ST_FLUSH);
            w_addr_q <= waddr_q;                 // ★ 地址来自递推寄存器，不再有 32 位加法
            w_data_q <= wdata_c;
            w_strb_q <= {8{state == ST_FLUSH}};
        end
    end
    assign mem_wr_en   = w_en_q;
    assign mem_wr_addr = w_addr_q;
    assign mem_wr_data = w_data_q;
    assign mem_wr_strb = w_strb_q;

    assign ctrl_busy    = (state != ST_IDLE);

    // 本 tile 的 run / nb
    // ★ 时序：原来 `remain = hw_q - p0_q`（32 位减法）→ 比较 → 加法 → 移位 是一条
    //   5.9 ns 的链（13 级，9 个 CARRY4）。这里把"减法"挪到 ST_TEND（用 remain_q 递推），
    //   ST_TINI 只做 16 位比较 + 选择 ⇒ 两拍各 ~2~3 ns，150 MHz 也有余量。
    assign remain_c   = remain_q;
    assign remain_n_c = remain_q - {16'd0, run_q};
    assign run_c      = (remain_c > TILE32) ? TILE16 : remain_c[15:0];
    assign nb_c       = (run_c + 16'd1) >> 1;

    // ========================================================================
    // 3. 主状态机
    // ========================================================================
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            state     <= ST_IDLE;
            c_q <= '0; h_q <= '0; w_q <= '0; grp_q <= '0;
            hw_q <= '0; dbase_q <= '0; out_q <= '0;
            p0_q <= '0; run_q <= '0; nb_q <= '0; remain_q <= '0;
            cb_i <= '0; j <= '0; b <= '0; wp <= '0;
            src_q <= '0; adj_q <= '0;
            kadj_q <= 32'd0; nanb_q <= 1'b0; badj_zero_q <= 1'b0; badj_sign_q <= 1'b0;
            mat <= '0; chb <= '0; stage <= '0;
            mask_q <= 16'hFFFF; nch_q <= 4'd0; waddr_q <= 32'd0;
            ctrl_done <= 1'b0;
            err_code  <= ERR_OK;
        end else if (ap_ce) begin
            ctrl_done <= 1'b0;

            case (state)
            // ---------------------------------------------------------------
            ST_IDLE: if (ctrl_start) begin
                c_q   <= cfg_c;  h_q <= cfg_h;  w_q <= cfg_w;
                grp_q <= cfg_group;
                out_q <= base_out;
                hw_q  <= {16'd0, cfg_h} * {16'd0, cfg_w};
                dbase_q <= base_desc + ({16'd0, cfg_group} << 9);   // 64 项 × 8 B
                err_code <= ERR_OK;
                state <= ST_CHK;
            end
            // ---------------------------------------------------------------
            ST_CHK: begin
                if ((c_q | h_q | w_q) == 16'd0) begin   // ★ 三个零比较合成一个
                    err_code <= ERR_SIZE;  state <= ST_DONE;
                end else if (c_q > 16'(CMAX)) begin
                    err_code <= ERR_SIZE;  state <= ST_DONE;
                end else if (grp_q > 16'd8) begin
                    err_code <= ERR_UNIT;  state <= ST_DONE;
                end else if ((base_desc == 32'd0) || (base_out == 32'd0)) begin
                    err_code <= ERR_NULL;  state <= ST_DONE;
                end else if ((base_desc[2:0] != 3'd0) || (base_out[2:0] != 3'd0)) begin
                    err_code <= ERR_ALIGN; state <= ST_DONE;
                end else begin
                    p0_q <= '0;  cb_i <= '0;  wp <= '0;
                    remain_q <= hw_q;
                    state <= ST_TINI;
                end
            end
            // ---------------------------------------------------------------
            ST_TINI: begin
                run_q <= run_c;
                nb_q  <= nb_c;
                // 本 tile 的像素有效掩码：bit p = (p < run)。一次算，之后每拍只查表。
                // ★ 面积刀 F：原来 `(16'd1 << run_c[3:0]) - 16'd1` 是个 16 位桶形移位
                //   + 加法（v31 netlist：mask_q 的 D 锥 62 cell）。bit p = (p < run)
                //   直接 16 个「5 位 run_c vs 常数」比较即可，每个 1 个 LUT6，语义逐位等价。
                for (int q = 0; q < TILE; q++)
                    mask_q[q] <= (run_c > 16'(q));
                // ★ 写地址第 0 个字在这里预装（**不能**只在 ST_ROW 预装：尾组最后一次
                //   flush 是从 ST_PASS 的"整趟掩码"分支跳进去的，不经过 ST_ROW —— 踩过）
                waddr_q <= out_q + (p0_q << 3);
                mat   <= '0;
                chb   <= '0;
                stage <= '0;                 // 本 tile 没写到的通道字节必须是 0
                cb_i  <= '0;
                j     <= '0;
                state <= ST_PASS;
            end
            // ---------------------------------------------------------------
            ST_PASS: begin
                nch_q <= nch_n_c;
                if (pass_dead_c) begin
                    // 整趟都在 cin 之外：该行保持 0（ST_TINI 已清），直接下一趟
                    j     <= '0;
                    chb   <= '0;
                    mat   <= '0;
                    if (cb_i == 3'd7) begin
                        wp <= '0;  state <= ST_FLUSH;
                    end else begin
                        cb_i <= cb_i + 3'd1;  state <= ST_PASS;
                    end
                end else begin
                    j     <= '0;
                    chb   <= '0;
                    mat   <= '0;
                    state <= ST_D0;
                end
            end
            // ---------------------------------------------------------------
            ST_D0: begin
                adj_q <= mem_rd_data[63:32];
                src_q <= mem_rd_data[31:0];
                // ★ 面积刀 D：adj 侧的 IEEE 派生量每通道只算这一次
                kadj_q      <= mem_rd_data[63] ? ~mem_rd_data[63:32]
                                               : (mem_rd_data[63:32] ^ 32'h8000_0000);
                nanb_q      <= (&mem_rd_data[62:55]) && (|mem_rd_data[54:32]);
                badj_zero_q <= (mem_rd_data[62:32] == 31'd0);
                badj_sign_q <= mem_rd_data[63];
                // 第 0 拍地址（A(0)）当拍组合发出；这里把 A(1) 装进 raddr_q
                raddr_q <= mem_rd_data[31:0] + (p0_q << 2)
                           + ((nb_q > 16'd1) ? 32'd8 : 32'd0);
                b     <= 3'd0;
                state <= ST_DATA;
            end
            // ---------------------------------------------------------------
            ST_DATA: begin
                // 本通道的 16 位落在 mat 的哪个 16 位切片（7-j）：常量 case ⇒ 8 个使能
                unique case (j)
                3'd0: mat[112 +: 16] <= chn_c;
                3'd1: mat[96  +: 16] <= chn_c;
                3'd2: mat[80  +: 16] <= chn_c;
                3'd3: mat[64  +: 16] <= chn_c;
                3'd4: mat[48  +: 16] <= chn_c;
                3'd5: mat[32  +: 16] <= chn_c;
                3'd6: mat[16  +: 16] <= chn_c;
                3'd7: mat[0   +: 16] <= chn_c;
                endcase
                if (b == 3'd7) begin
                    if (next_en_c) begin
                        j     <= j + 3'd1;
                        chb   <= '0;
                        state <= ST_D0;
                    end else begin
                        chb   <= chn_c;
                        state <= ST_ROW;
                    end
                end else begin
                    chb   <= chn_c;
                    raddr_q <= radv_c ? (raddr_q + 32'd8) : raddr_q;
                    b     <= b + 3'd1;
                    state <= ST_DATA;
                end
            end
            // ---------------------------------------------------------------
            ST_ROW: begin
                // 本趟的 16 个字节写进 stage 的第 cb 个 128 bit 行
                unique case (cb_i)
                3'd0: stage[0   +: 128] <= bytes_c;
                3'd1: stage[128 +: 128] <= bytes_c;
                3'd2: stage[256 +: 128] <= bytes_c;
                3'd3: stage[384 +: 128] <= bytes_c;
                3'd4: stage[512 +: 128] <= bytes_c;
                3'd5: stage[640 +: 128] <= bytes_c;
                3'd6: stage[768 +: 128] <= bytes_c;
                3'd7: stage[896 +: 128] <= bytes_c;
                endcase
                j  <= '0;
                chb <= '0;
                mat <= '0;
                if (cb_i == 3'd7) begin
                    wp <= '0;
                    state <= ST_FLUSH;
                end else begin
                    cb_i  <= cb_i + 3'd1;
                    state <= ST_PASS;
                end
            end
            // ---------------------------------------------------------------
            ST_FLUSH: begin
                waddr_q <= waddr_q + 32'd8;             // 递推：下一个字
                if (wp == 4'd15) begin
                    state <= ST_TEND;
                end else begin
                    wp <= wp + 4'd1;
                end
            end
            // ---------------------------------------------------------------
            ST_TEND: begin
                if ({16'd0, run_q} >= remain_q) begin
                    state <= ST_DONE;
                end else begin
                    p0_q     <= p0_q + {16'd0, run_q};
                    remain_q <= remain_n_c;
                    state    <= ST_TINI;
                end
            end
            // ---------------------------------------------------------------
            ST_DONE: begin
                ctrl_done <= 1'b1;
                state     <= ST_IDLE;
            end
            default: state <= ST_IDLE;
            endcase
        end
    end

    // h_q/w_q 只参与 hw 计算与合法性判断（保留寄存器便于调试观察）
    logic unused_ok;
    assign unused_ok = &{1'b0, h_q[0], w_q[0], 1'b0};

endmodule
