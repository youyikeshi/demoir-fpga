// ============================================================================
//  bin_e1e2.v —— E1/E2 融合级：**int8 激活 → 二值打包 64 位字**
//
//  ── 它替代 PS 上的哪两步（这是本文件存在的理由）────────────────────────────
//  bin_net.BinNet._conv 里，每层卷积之前 PS 要做两件事：
//      x_q  = sat8(rint(x_f * 2^fs_in))            ← ① "quant"（重定标）
//      A    = binarize_pack(x_q, t_q)              ← ② "binarize"（64 通道打包）
//  本单元把 ①② 合成**一趟**，并且全程整数（不再有任何浮点）。
//
//  ── 关键定理（analysis/thr_equiv.py 对容器里**全部 40304 个 (层,通道) 组合**
//     做过 256 点穷举：0 例非单调；闭式与穷举 100% 一致）──────────────────────
//      设 d = fs_in - fs_out，则复合函数
//          f(x) = [ sat8(rint(x·2^d)) + t_q > 0 ]
//      在 x ∈ [-128,127] 上**单调非降**，于是
//          ∃T ∈ [-129,127] 使 f(x) ≡ [ x + THR > 0 ]，  THR = -T ∈ [-127,129]
//  ⇒ **重定标完全消失**：本单元只要"逐通道一个 int16 阈值 + 一次比较"。
//    这就是"把 binarize 搬进 PL"能顺手把 quant 也一起消灭的原因。
//
//  ── 位序/布局（与 bin_conv_core 的输入口**逐位一致**）─────────────────────
//      output  addr = base_out + p*8              （p = y*W + x，raster）
//              bit i = 逻辑通道 (64*group + i) 的二值位
//      source  addr = lsrc[k] + p*8               （C8 交错：8 个 int8 = 8 个连续通道）
//      ⇒ lane k 负责 bit [8k +: 8]，源来自 lsrc[k]（**可以是任意一段张量**）
//
//  ── 为什么是"8 条 lane"────────────────────────────────────────────────────
//      一个 64 位字 = 64 通道 = 8 个 C8 字 = **8 次 64-bit 读**。做成 8 条独立
//      lane 同时得到两件东西：
//        ① 吞吐 8 拍/输出字（单口 8 B/拍的读带宽就是硬上限）；
//        ② **cat 免费**：lsrc[k] 可指向不同张量段 ⇒ `concatenate` 不需要在内存里
//           落地（全网 148 MB 的 cat 拷贝消失，这是本方案比"独立 binarize IP"多出来
//           的那部分收益）。
//
//  ── 转置缓冲（全局最大的一块 SRAM，必须算清楚）────────────────────────────
//      源是通道主序（每通道一趟扫全图），目标是像素主序（每像素一个字），
//      所以必须有转置缓冲。**不能**按整图开：128×128 ⇒ 128 KB，板上放不下。
//      本单元按 tile = 2^ACC_AW 像素分块（默认 256 像素 = 2 KB）：
//          for tile:   for lane 0..7:  for p in tile:  acc_b[k][p] <= byte（8 个字节阵列）
//                      flush: for p in tile: 写出整字
//      每条 lane 一趟连续读 2^ACC_AW 个字（4 KB 连续流），对 wrapper 的
//      "线缓存 + 预取"（一条线 8 个字）是最理想的访问形态。
//
//  ── 拍数 ─────────────────────────────────────────────────────────────────
//      一个 tile（K = 2^ACC_AW 像素 × 8 lane）：
//          8·K 读拍 + 2 排空 + K 写拍 + 4·8+16 状态机开销
//      ⇒ 每个 64 位输出字 ≈ 9 拍（读写不重叠；双缓冲可做到 8 拍）
//
//  ── 与 bin_conv_core 的三处刻意差异 ───────────────────────────────────────
//      1. 写口 **64 位 + 字节选通**：二值输出天然整字。契约里"写口 64-bit 整字化"
//         是既定方向（docs/binary/10 第 2 批 E 项），本文件先按 64 位做。
//      2. 阈值从**内存**读（cin 最大 544 ⇒ 1088 B），不开 544 个寄存器。
//      3. 描述符表在**内存**里（8 个字），不开 16 个 32 位基址寄存器。
//
//  ── 维护规则（与 bin_conv_core 一致）──────────────────────────────────────
//      · 每个 always_ff 都带 ap_ce（P3 同步时钟使能约定）。
//      · 转置缓冲 acc 的 always_ff **不带复位**（带复位的数组 Vivado 不推断 RAM，
//        会溶解成 2^ACC_AW 个 FF）。
// ============================================================================
`timescale 1ns/1ps

module bin_e1e2 #(
    parameter int LANES  = 64,      // 必须 64：一个 64 位字 = 64 通道
    parameter int ACC_AW = 8,       // 转置缓冲深度 = 2^ACC_AW 个像素
    parameter int C_MAX  = 4096
)(
    input  logic        ap_clk,
    input  logic        ap_rst_n,
    input  logic        ap_ce,

    input  logic        ctrl_start,
    output logic        ctrl_done,
    output logic        ctrl_busy,

    input  logic [15:0] cfg_c,          // 逻辑通道数（= 下一层的 cin）
    input  logic [15:0] cfg_h,
    input  logic [15:0] cfg_w,
    input  logic [15:0] cfg_group,      // 本命令产出第几个 64 位字组（0..8）
    input  logic [31:0] base_desc,      // 描述符表（8 个字，每字 {thr_base, src_base}）
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

    localparam logic [7:0] ERR_OK   = 8'd0;
    localparam logic [7:0] ERR_SIZE = 8'd1;
    localparam logic [7:0] ERR_NULL = 8'd2;
    localparam logic [7:0] ERR_ALIGN= 8'd5;
    localparam logic [7:0] ERR_UNIT = 8'd6;

    localparam int ADEPTH = 1 << ACC_AW;
    localparam logic [15:0] DEPTH16 = ADEPTH[15:0];

    // ========================================================================
    // 1. 状态
    // ========================================================================
    typedef enum logic [3:0] {
        ST_IDLE  = 4'd0,
        ST_CHK   = 4'd1,
        ST_DESC  = 4'd2,   // 8 × (发读 1 拍 + 收数 1 拍)，ph = 0..15
        ST_LINI  = 4'd3,   // 装 lane 的 en 掩码
        ST_THR   = 4'd4,   // 2 × (发读 1 拍 + 收数 1 拍)
        ST_SWEEP = 4'd5,   // 1 字/拍流水
        ST_DRAIN = 4'd6,
        ST_FLUSH = 4'd7,   // 1 整字/拍
        ST_LAST  = 4'd8,
        ST_DONE  = 4'd9
    } state_t;

    state_t state;

    logic [15:0] c_q, h_q, w_q, grp_q;
    logic [31:0] desc_q, out_q, hw_q, tile_q;

    // 描述符 / 阈值
    //
    //   ★ 2026-10-31 面积刀 1（按 v31 布线后 netlist 实测定的）：
    //     原来是 lsrc[0:7] + lthr[0:7] 两个 32-bit 阵列，代价 =
    //        · 512 个 FF（Synth 8-4767，两个阵列都没进 RAM）
    //        · 8:1 lane 查表 = **64 个 MUXF7**（netlist 里 32 个叫 lsrc_q_reg、
    //          32 个叫 lthr_q_reg）+ 129 个 LUT
    //        · 变址写 `lsrc[ph[3:1]] <= …` 的 demux ≈ 40 LUT
    //     现在合成一个 **8×64 的 desc_ram**（一个字 = {thr_base, src_base}，
    //     正好是 mem_rd_data 原样），写在无复位的 §3c 块里；读地址**提前一拍**
    //     （ST_DRAIN 给 lane+1、ST_DESC 末拍 / ST_LAST 给 0），输出在 ST_LINI
    //     那拍被 desc_rd_q 接住 ⇒ ST_THR 看到的 desc 与原来逐位相同，**拍数不变**。
    (* ram_style = "block" *) logic [63:0] desc_ram [0:7];
    logic [63:0] desc_rd_q;        // 描述符 RAM 读出级（= 原 lsrc_q/lthr_q 合并）
    logic [2:0]  desc_raddr;       // 提前一拍的读地址
    logic signed [15:0] thr_q [0:7];
    logic [7:0]  en_q;

    logic [2:0]  lane;
    logic [3:0]  ph;               // DESC/THR 的相位
    logic [15:0] p;                // tile 内像素号
    logic [15:0] run_len;

    // ★ 面积刀 2：本 lane 的通道区间。原来
    //       `for (j) en_q[j] <= (lchan + 32'(j) < {16'd0,c_q});`
    //   被展开成 **8 条独立的 32 位加法 + 32 位比较链**
    //   （netlist：p_0_out_inferred__11..19/i__carry* 共 8 族，en_q 的 D 锥实测 311 cell）。
    //   而结果只取决于低 13 位（c_q ≤ C_MAX = 4096，由 ST_CHK 保证）：
    //       en_q[j] = (chan0 + j < c_q) ≡ (j < c_q - chan0) ≡ (room_c > j)
    //   ⇒ 1 次 16 位减法 + 8 个「17 位 signed vs 常数」比较（每个 1~2 LUT）。
    //   等价性：chan0 ≤ 576、c_q ≤ 4096 ⇒ room_c ∈ [-576, 4096]，17 位 signed 不溢出；
    //           两边都是精确整数，无舍入/饱和。
    logic [15:0]        chan0_c;   // 本 lane 首通道号 = 64*group + 8*lane  (≤ 576)
    logic signed [16:0] room_c;    // c_q - chan0_c

    // 读流水（发读 / 响应 各占 1 拍，SWEEP 时每拍都发、都收）
    logic        resp_vld;
    logic [15:0] resp_p;

    // ★ 2026-10-31 时序刀：扫描地址在 ST_THR 载入，ST_SWEEP 每拍 +8
    logic [31:0] rd_addr_q;

    // ========================================================================
    // ★ 2026-10-31 时序刀 4（v38）：把描述符字段在 ST_LINI 接住
    // ------------------------------------------------------------------------
    //   布线后 netlist 实测（scripts/detail_path2.tcl / all_viol.tcl，v37 routed）：
    //     设计里**每一条违例路径（1200 条端点 = 全部）都从同一个引脚出发**——
    //         g_conv.u_pack/desc_ram_reg/CLKARDCLK
    //     最差一条逐级（21 级 / 14.184 ns / slack −4.466）：
    //         RAMB36 CLKARDCLK→DOBDO[6]      2.125   (desc_rd_q = BRAM 输出寄存器)
    //         net                            0.946
    //         32 位加法（issue_addr）        3.129   (9 个 CARRY4 摊在 9 个 SLICE)
    //         散列 + lc_tag(LUTRAM) 地址     0.836 + 0.853
    //         标签比较 → rd_hit              2.643
    //         rd_hit → 写侧 push 锥 → bf_data D   4.598
    //   ⇒ 前 6.6 ns 全在"desc_ram 输出 → mem_rd_addr"，而这一段**根本不需要组合**：
    //     · ST_THR 只读 thr_base + {0, 8} 两个字；
    //     · src_base 只被 ST_THR 用来算 rd_addr_q（本来就是寄存器）。
    //   做法：
    //     · ST_LINI 本来是一整拍空转（装 en / 清 p / 清 ph），在这拍把 desc_rd_q
    //       接进三个寄存器；§3c 的"读地址提前一拍"保证 desc_rd_q 在 ST_LINI 拍的
    //       **值就是本 lane 的描述符** ⇒ ST_THR 看到的东西与原设计逐位相同；
    //     · +8 的加法提前算进 thr_a1_q 的 D 端 ⇒ ST_THR 的 mem_rd_addr 退化成
    //       **纯 2:1 mux**（0 级加法），rd_addr_q 的加法变成"两个寄存器相加"。
    //   ⇒ 拍数不变、状态机不变、数据通路逐位不变；只把 6.6 ns 的组合锥换成寄存器。
    //   ⚠ 三个寄存器**不要**写成数组（数组会被推断成 RAM，又是 2 拍；这里要的就是 FF）。
    // ========================================================================
    logic [31:0] thr_a0_q;      // 本 lane 阈值基址  = desc[63:32]     （ST_THR ph=0 读它）
    logic [31:0] thr_a1_q;      // 同一个 + 8        = desc[63:32] + 8 （ST_THR ph=2 读它）
    logic [31:0] src_base_q;    // 本 lane 源基址    = desc[31:0]

    // 写口内部（组合/寄存）与输出级；输出级再打一拍，和 wrapper 写口解耦
    logic        wr_en_c;
    logic [63:0] wr_data_c;
    logic [7:0]  wr_sel_c;

    // 转置缓冲：**8 个字节宽阵列**，每 lane 一个。
    //   ★ 2026-10-31 修复（Synth 8-4767 / WNS −5.9 ns）：
    //   原来是一个 64-bit × 256 的 acc，写在**带异步复位的 always_ff** 里
    //   ⇒ 综合拒绝推断 RAM，溶成 16,384 个 FF；再加上
    //   `acc[p][lane*8 +: 8]` 的变量部分选择 + 256:1×64b 读 mux，时序直接崩。
    //   现在：
    //     · 写：8 个独立阵列，每个只有 1 个写口（lane==k 时写 acc_b[k]），
    //           写在**无复位**的 always_ff 里（与文件头 §维护规则一致）；
    //     · 读：8 个阵列同地址并行读，纯拼接 ⇒ 0 级 64-bit mux。
    //   资源：8 × (256×8) = 16 Kb ⇒ 8 块 RAMB18（9-bit 模式），不再吃 FF。
    logic [7:0] acc_b [0:7][0:ADEPTH-1];

    // 写流水
    logic        wv_q;      // 已不使用（保留以最小化 diff）
    logic [31:0] wa_q;
    logic [15:0] wp_q;

    // ========================================================================
    // 2. 组合
    // ========================================================================
    logic [31:0] issue_addr;
    logic        issue_en;
    logic [7:0]  bcmp;

    // 本 lane 首通道号 / 剩余通道数（面积刀 2，见 §1 的推导）
    assign chan0_c = ({10'd0, grp_q[5:0]} << 6) + {10'd0, lane, 3'd0};   // 64*group + 8*lane
    assign room_c  = $signed({1'b0, c_q}) - $signed({1'b0, chan0_c});    // c_q - chan0

    // bit_j = en_j && (src_byte_j + THR_j > 0)
    always_comb begin
        for (int j = 0; j < 8; j++) begin
            bcmp[j] = en_q[j] &&
                      (($signed({{8{mem_rd_data[j*8+7]}}, mem_rd_data[j*8 +: 8]})
                        + $signed(thr_q[j])) > 0);
        end
    end

    // ---- 读地址（各状态各自一套；组合使能 + 寄存器地址，与 bin_conv_core §9b 同约定）
    always_comb begin
        issue_en   = 1'b0;
        issue_addr = 32'd0;
        case (state)
        ST_DESC: begin
            if (ph[0] == 1'b0) begin
                issue_en   = 1'b1;
                issue_addr = desc_q + ({28'd0, ph[3:1]} << 3);
            end
        end
        ST_THR: begin
            if (ph[0] == 1'b0) begin
                issue_en   = 1'b1;
                // ★ 时序刀 4：纯 2:1 mux（两个操作数都是寄存器，路径上 0 级加法）。
                //   ph[3:1] 在这里只有 0（ph==0）和 1（ph==2）两种取值，
                //   所以 |ph[3:1] 就是那两个相位。
                issue_addr = (|ph[3:1]) ? thr_a1_q : thr_a0_q;
            end
        end
        ST_SWEEP: begin
            // ⚠ 2026-10-31：曾试过删掉 `p < run_len`（前代理叫"时序刀 6"）以砍掉
            //   v38 剩下的 −2.080 ns（p → issue_en → mem_rd_en → u_ce → DSP CE）。
            //   RTL 级实测（106 命令/24 层/6.3 MB 逐字节 0 差、总拍数 7,244,033 不变）
            //   支持"恒真"，但板上 227 层 sha1 目前无法复测（板端 CMA 阻塞）⇒
            //   **先回退，等有板上 sha1 判据再做**。备份：/tmp/bin_e1e2_knife6_91b287.v
            //   与 rtl_hand/.snapshots/v38_A_unverified/bin_e1e2.v。
            if (p < run_len) begin
                issue_en   = 1'b1;
                issue_addr = rd_addr_q;              // 已寄存，路径上无加法
            end
        end
        default: ;
        endcase
    end

    assign mem_rd_en   = issue_en;
    assign mem_rd_addr = issue_addr;

    // ---- 写地址由第 3 刀的输出现级驱动（见 §4）

    // 第一个 tile 之后还剩多少像素（ST_LAST 用）
    logic [31:0] remain;
    assign remain = hw_q - tile_q - {16'd0, run_len};

    // ========================================================================
    // 3. 主状态机
    // ========================================================================
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            state     <= ST_IDLE;
            c_q <= '0; h_q <= '0; w_q <= '0; grp_q <= '0;
            desc_q <= '0; out_q <= '0; hw_q <= '0; tile_q <= '0;
            lane <= '0; ph <= '0; p <= '0; run_len <= '0;
            resp_vld <= 1'b0; resp_p <= '0;
            wv_q <= 1'b0; wp_q <= '0;
            rd_addr_q <= '0;
            thr_a0_q <= '0; thr_a1_q <= '0; src_base_q <= '0;
            err_code <= ERR_OK;
            ctrl_done <= 1'b0;
        end else if (ap_ce) begin
            ctrl_done <= 1'b0;
            resp_vld  <= issue_en;        // 发读的下一拍就是响应拍
            if (issue_en) resp_p <= p;

            case (state)
            // ---------------------------------------------------------------
            ST_IDLE: if (ctrl_start) begin
                c_q   <= cfg_c;  h_q <= cfg_h;  w_q <= cfg_w;
                grp_q <= cfg_group;
                desc_q<= base_desc;  out_q <= base_out;
                hw_q  <= {16'd0, cfg_h} * {16'd0, cfg_w};
                err_code <= ERR_OK;
                state <= ST_CHK;
            end
            // ---------------------------------------------------------------
            ST_CHK: begin
                if ((c_q == 16'd0) || (h_q == 16'd0) || (w_q == 16'd0)) begin
                    err_code <= ERR_SIZE;  state <= ST_DONE;
                end else if (c_q > 16'(C_MAX)) begin
                    err_code <= ERR_SIZE;  state <= ST_DONE;
                end else if (grp_q > 16'd8) begin
                    // 上限 8 而不是 7：cin=544 的 9 层需要第 9 组（通道 512..575）。
                    // grp_base_c = grp_q<<6 = 512，地址算术天然支持，无需其它改动。
                    err_code <= ERR_UNIT;  state <= ST_DONE;
                end else if ((base_desc == 32'd0) || (base_out == 32'd0)) begin
                    err_code <= ERR_NULL;  state <= ST_DONE;
                end else if ((base_desc[2:0] != 3'd0) || (base_out[2:0] != 3'd0)) begin
                    err_code <= ERR_ALIGN; state <= ST_DONE;
                end else begin
                    lane  <= '0;  ph <= '0;
                    state <= ST_DESC;
                end
            end
            // ---------------------------------------------------------------
            // 8 个描述符：ph = 0..15，偶=发读 desc[ph/2]，奇=收数 desc[(ph-1)/2]
            ST_DESC: begin
                if (ph[0]) begin                        // 收数（写 desc_ram 在 §3c）
                    if (ph == 4'd15) begin
                        lane   <= '0;
                        ph     <= '0;
                        tile_q <= '0;
                        run_len<= (hw_q > {16'd0, DEPTH16}) ? DEPTH16 : hw_q[15:0];
                        state  <= ST_LINI;
                    end else begin
                        ph <= ph + 4'd1;                // → 下一个发读相
                    end
                end else begin
                    ph <= ph + 4'd1;
                end
            end
            // ---------------------------------------------------------------
            ST_LINI: begin
                // 本 lane 8 个通道的有效位 = (j < c_q - chan0)（面积刀 2）
                for (int j = 0; j < 8; j++)
                    en_q[j] <= (room_c > $signed(17'(j)));
                // ★ 时序刀 4：本拍把本 lane 的描述符从 desc_rd_q 接进三个寄存器。
                //   desc_rd_q 在本拍的**值就是本 lane 的 desc**（§3c：读地址提前一拍，
                //   ST_LINI 那拍接住），所以 ST_THR 用的值与改前逐位相同。
                thr_a0_q  <= desc_rd_q[63:32];
                thr_a1_q  <= desc_rd_q[63:32] + 32'd8;   // 加法留在寄存器 D 端，不在读路径上
                src_base_q<= desc_rd_q[31:0];
                p     <= '0;
                ph    <= '0;
                state <= ST_THR;
            end
            // ---------------------------------------------------------------
            ST_THR: begin
                // 第 2 刀：扫地址在这里算好；ST_SWEEP 只做 +8 递增
                // ★ 时序刀 4：改由 src_base_q 相加（寄存器+寄存器），不再从 desc_ram 输出起算
                rd_addr_q <= src_base_q + (tile_q << 3);
                if (ph[0]) begin                        // 收数
                    if (ph == 4'd1) begin
                        thr_q[0] <= $signed(mem_rd_data[15:0]);
                        thr_q[1] <= $signed(mem_rd_data[31:16]);
                        thr_q[2] <= $signed(mem_rd_data[47:32]);
                        thr_q[3] <= $signed(mem_rd_data[63:48]);
                        ph <= 4'd2;
                    end else begin                      // ph == 3
                        thr_q[4] <= $signed(mem_rd_data[15:0]);
                        thr_q[5] <= $signed(mem_rd_data[31:16]);
                        thr_q[6] <= $signed(mem_rd_data[47:32]);
                        thr_q[7] <= $signed(mem_rd_data[63:48]);
                        ph    <= '0;
                        p     <= '0;
                        state <= ST_SWEEP;
                    end
                end else begin
                    ph <= ph + 4'd1;
                end
            end
            // ---------------------------------------------------------------
            // 扫一趟：每拍发一个读，同时把**上一拍**的数据写进缓冲
            ST_SWEEP: begin
                rd_addr_q <= rd_addr_q + 32'd8;
                if (p == run_len - 16'd1) begin
                    p     <= '0;
                    state <= ST_DRAIN;
                end else begin
                    p <= p + 16'd1;
                end
            end
            // ---------------------------------------------------------------
            ST_DRAIN: begin
                // 收最后一拍（数据写进 acc_b 在独立的无复位 always_ff 里）
                if (lane == 3'd7) begin
                    wp_q  <= '0;
                    ph    <= '0;
                    state <= ST_FLUSH;
                end else begin
                    lane  <= lane + 3'd1;
                    ph    <= '0;
                    state <= ST_LINI;               // 换 lane（重新装 en 与阈值）
                end
            end
            // ---------------------------------------------------------------
            // 排空：1 整字/拍
            ST_FLUSH: begin
                if (wp_q == run_len - 16'd1) begin
                    state <= ST_LAST;
                end else begin
                    wp_q <= wp_q + 16'd1;
                end
            end
            ST_LAST: begin
                lane  <= '0;
                ph    <= '0;
                if (tile_q + {16'd0, run_len} >= hw_q) begin
                    state <= ST_DONE;
                end else begin
                    tile_q  <= tile_q + {16'd0, run_len};
                    run_len <= (remain > {16'd0, DEPTH16}) ? DEPTH16 : remain[15:0];
                    state   <= ST_LINI;
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

    // ========================================================================
    // 3b. 转置缓冲写口（**独立、无复位**的 always_ff）
    // ------------------------------------------------------------------------
    //   ⚠ 不要把这 8 行搬回上面的主状态机块：那个块带 `negedge ap_rst_n`，
    //     综合器会以"RAM is sensitive to asynchronous reset"拒绝推断 RAM，
    //     把 acc 溶成 256×64 = 16,384 个 FF（本项目已踩 3 次）。
    //   每个 acc_b[k] 只有 1 个写口（lane==k），地址 resp_p、数据 bcmp；
    //   8 个写使能是固定比较，不是变量部分选择。
    //   ap_ce 必须保留：wrapper 在读 miss 时停 CE，写口不能"多写"。
    // ========================================================================
    always_ff @(posedge ap_clk) begin
        if (ap_ce && resp_vld) begin
            if (lane == 3'd0) acc_b[0][resp_p[ACC_AW-1:0]] <= bcmp;
            if (lane == 3'd1) acc_b[1][resp_p[ACC_AW-1:0]] <= bcmp;
            if (lane == 3'd2) acc_b[2][resp_p[ACC_AW-1:0]] <= bcmp;
            if (lane == 3'd3) acc_b[3][resp_p[ACC_AW-1:0]] <= bcmp;
            if (lane == 3'd4) acc_b[4][resp_p[ACC_AW-1:0]] <= bcmp;
            if (lane == 3'd5) acc_b[5][resp_p[ACC_AW-1:0]] <= bcmp;
            if (lane == 3'd6) acc_b[6][resp_p[ACC_AW-1:0]] <= bcmp;
            if (lane == 3'd7) acc_b[7][resp_p[ACC_AW-1:0]] <= bcmp;
        end
    end

    // ========================================================================
    // 3c. 描述符 RAM（面积刀 1）——**两个 no-reset 块**，与 §3b 同规矩
    // ------------------------------------------------------------------------
    //   写：只在 ST_DESC 的收数相（ph 奇），地址 = ph[3:1]，数据 = mem_rd_data
    //       原样（一个字就是 {thr_base, src_base}）。
    //   读：地址**提前一拍**给（见 desc_raddr），desc_rd_q 就是 1 拍后的数据。
    //       · 若工具落 RAMB18（ram_style=block）：desc_rd_q 就是 BRAM 的输出寄存器，
    //         地址在 T 拍 → 数据在 T+1 拍；
    //       · 若回落到分布式 RAM：读是组合的，desc_rd_q 是 T 拍地址的寄存器采样。
    //       两种映射下 desc_rd_q 的时序**完全相同**（都是"地址 T 拍、数据 T+1 拍"），
    //       所以换 RAM 种类不会改变行为 —— 这正是这里敢吃 BRAM 的原因。
    //   ⚠ 读地址必须在 ST_LINI 的**前一拍**就摆好，否则 ST_THR 拿不到本 lane 的 desc：
    //       ST_DESC(ph==15) → 0      （下一拍 ST_LINI，lane 已被置 0）
    //       ST_DRAIN        → lane+1 （下一拍 ST_LINI，lane 已 +1）
    //       ST_LAST         → 0      （下一拍 ST_LINI，新 tile 的 lane 0）
    //       其它拍保持 lane（数据无人使用）
    // ========================================================================
    always_ff @(posedge ap_clk) begin
        if (ap_ce && (state == ST_DESC) && ph[0])
            desc_ram[ph[3:1]] <= mem_rd_data;
    end

    always_comb begin
        desc_raddr = lane;
        case (state)
        ST_DESC:  if (ph == 4'd15) desc_raddr = 3'd0;
        ST_DRAIN:                  desc_raddr = lane + 3'd1;
        ST_LAST:                   desc_raddr = 3'd0;
        default:                   desc_raddr = lane;
        endcase
    end

    always_ff @(posedge ap_clk) begin
        if (ap_ce) desc_rd_q <= desc_ram[desc_raddr];
    end

    // ========================================================================
    // 4. 写口（寄存输出；数据是 acc 的同步读）
    // ========================================================================
    //  ★ 写口的时序对齐（这里踩过坑，写清楚）：
    //    单元写口是"当拍生效"（mem_wr_en 与 mem_wr_addr/data 同拍），
    //    但本单元的写数据来自 acc 的**同步读**（地址给上，数据晚 1 拍）。
    //    所以 (en, addr, data) 三者必须由**同一份**两拍深度的流水派生：
    //       第 1 拍（state==ST_FLUSH）：pend_v<=1, pend_a<=地址, pend_p<=像素号
    //       第 2 拍：mem_wr_en<=pend_v；wa_q<=pend_a；mem_wr_data<={acc_b[7..0][pend_p]}
    //       第 3 拍：总线上的 (en,addr,data) 一起有效
    //    只延迟其中一部分的后果是"每个 tile 的首字被吃掉 + 尾字多写一次"，
    //    逐字节回归正是这样抓到的（words 0 / 256 恒为 0）。
    //    注意 pend_a 必须**在第 1 拍就算好**：tile_q 到第 2 拍已经被 ST_LAST 改了。
    logic        pend_v;
    logic [31:0] pend_a;
    logic [15:0] pend_p;
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            pend_v      <= 1'b0;
            pend_a      <= 32'd0;
            pend_p      <= 16'd0;
            wr_en_c     <= 1'b0;
            wa_q        <= 32'd0;
            wr_data_c   <= 64'd0;
            wr_sel_c    <= 8'd0;
        end else if (ap_ce) begin
            if (state == ST_FLUSH) begin
                pend_v <= 1'b1;
                pend_a <= out_q + ((tile_q + {16'd0, wp_q}) << 3);
                pend_p <= wp_q;
            end else begin
                pend_v <= 1'b0;
            end
            wr_en_c     <= pend_v;
            wr_sel_c    <= {8{pend_v}};
            if (pend_v) begin
                wa_q        <= pend_a;
                // 8 个字节阵列同地址并行读，纯拼接 ⇒ 0 级 64-bit mux
                wr_data_c[7:0]   <= acc_b[0][pend_p[ACC_AW-1:0]];
                wr_data_c[15:8]  <= acc_b[1][pend_p[ACC_AW-1:0]];
                wr_data_c[23:16] <= acc_b[2][pend_p[ACC_AW-1:0]];
                wr_data_c[31:24] <= acc_b[3][pend_p[ACC_AW-1:0]];
                wr_data_c[39:32] <= acc_b[4][pend_p[ACC_AW-1:0]];
                wr_data_c[47:40] <= acc_b[5][pend_p[ACC_AW-1:0]];
                wr_data_c[55:48] <= acc_b[6][pend_p[ACC_AW-1:0]];
                wr_data_c[63:56] <= acc_b[7][pend_p[ACC_AW-1:0]];
            end
        end
    end

    // 第 3 刀：写口输出再打一拍（写口无延迟契约；一次把 wrapper 写口的
    //         bf_data/pw_reg/df_addr/df_len/bf_strb 那一族从 u_pack 上解耦）。
    //         注意：必须同样受 ap_ce 控制，wrapper 读 miss/写压力停 CE 时
    //         输出级与内部级一起冻结，不会丢写/重写。
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            mem_wr_en   <= 1'b0;
            mem_wr_addr <= 32'd0;
            mem_wr_data <= 64'd0;
            mem_wr_strb <= 8'd0;
        end else if (ap_ce) begin
            mem_wr_en   <= wr_en_c;
            mem_wr_addr <= wa_q;
            mem_wr_data <= wr_data_c;
            mem_wr_strb <= wr_sel_c;
        end
    end

    assign ctrl_busy = (state != ST_IDLE);

    // h_q/w_q 只参与 ST_CHK 的合法性判断（保留寄存器便于调试观察）
    logic unused_ok;
    assign unused_ok = &{1'b0, h_q[0], w_q[0], 1'b0};

endmodule
