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
//
// ============================================================================
//  ★★ T-36（2026-11-02）：两个**互相独立、各自可单独开关**的改进
// ----------------------------------------------------------------------------
//  都被上游取证判为"无论哪个融合假设成立都对"：不依赖板端探针判决即可上车。
//
//  ① E1E2_SKIP_EMPTY_LANE —— 空 lane 提前跳 DRAIN
//     现状：lane 0→7 **无条件全扫**（ST_DRAIN 里 `lane==7` 才去 FLUSH），`en_q`
//           只掩 bcmp 的 bit、**不省读** ⇒ 每个字组为 8−lanes_real 条空通道白读
//           8·HW 拍。
//     改法：ST_CHK 里算出本组真实 lane 数
//             nlanes = clamp(ceil((cfg_c − 64·grp0)/8), 0, 8)
//           扫到 lane == nlanes−1 就在 ST_DRAIN 直接去 ST_FLUSH；nlanes==0 时
//           连一次读都不发（ST_LINI 直接跳 ST_FLUSH）。
//     ⚠ 必须同时做的一件事：**空 lane 的输出字节必须强制为 0**。
//       旧行为里空 lane 也被扫过，acc_b[k] 被写成全 0；不扫之后 acc_b[k] 留的是
//       上一 tile 的残值（仿真里甚至是 X）。所以 §4 的写数据通路加了
//       `nl_ok[k] ? acc_b[k][..] : 8'd0`（8×8 位与门，~16 LUT）。
//       **漏了这一步 = 输出字节错**，这是本改动唯一的真实风险点。
//     签名：输出**逐字节不变**；读拍数下降 Σ(8−lanes_real)·HW（= 15.412 MB，
//           与 T-35 `runs/t35_pack/REPORT_pack_bytes.md` 的 C 类逐位相同）。
//
//  ② E1E2_GRP_LOOP —— 一条命令自增遍历 cb 个字组（698 → 227 条命令）
//     现状：一条命令只产出一个 64 位字组，而一层的二值张量有 cb=ceil(cin/64) 个
//           字组 ⇒ 每层要发 cb 条命令，471/698 条（67%）是"同一层重复遍历"。
//     依据（T-35 §3.2 逐层断言过）：相邻命令之间
//             E_DESC += 64（= 8 字 × 8 B）、E_OUT += HW·8、E_GROUP += 1
//           **严格等差**，C/H/W 完全相同 ⇒ 三个自增完全可以在 RTL 里做。
//     改法：ST_DONE 里如果还有组要跑就**不抬 done**，当场把
//             desc_q += 64、out_q += hw<<3、grp_q += 1、ngrp_q −= 1
//           再回 ST_CHK 做**逐组重校验**。done 只在最后一组抬一次。
//     ★ 拍数：**工作拍数逐拍不变**——每组仍然走 CHK(1)+DESC(16)+…+DONE(1)，
//       描述符每组的 16 拍重读也照旧（E_DESC 每组确实是 8 个新字），只是省掉了
//       "PS 重新发一条命令"的外部周转（模块内 1 个 IDLE 拍；包装层还要 512 拍
//       tag 冲刷 + 若干次 MMIO）。⇒ 判据是"读/写拍数逐位相等 + busy 拍数逐位
//       相等"，不是"差不多"。
//     ⚠ 中途出错必须**立刻**抬 done：ST_CHK 是**每组**都过的，错误（组号越界 /
//       输出区间越过 32 位）在那一拍就进 ST_DONE ⇒ 下一拍 done、err_code 有效，
//       **绝不会把剩下 7 组跑完**（否则会踩脏 7 段输出内存）。
//     ⚠ 与 PS 的契约（见 §1 的 cfg_group 位域）：ngrp 的**默认值等价于 1 组**，
//       旧驱动（cmds.bin 里 cfg_group ∈ 0..8）写不写它行为逐位相同。
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
    // ★ T-36 ②：cfg_group 这一个 16 位字段被切成两段复用（**不新增寄存器地址**，
    //   ⇒ 包装层 `bin_unit_axi_wrap.v` 一行都不用改；PS 侧只改 E_GROUP 的写法）：
    //       cfg_group[3:0]  = grp0   起始字组号（0..8，**旧驱动写的就是这个值**）
    //       cfg_group[7:4]  = ngrp   本次命令连续遍历的组数（1..9；**0 定义成 1**）
    //       cfg_group[15:8] = 保留，必须 0（非 0 ⇒ ERR_UNIT）
    //   为什么这样切而不是新开 R_E_NGRP（0x218）：本轮的硬约束是"只许改
    //   bin_e1e2.v"。0x218 在白名单之外（包装层 decode 里没有它），而
    //   `r_egrp[15:0]` 已经整字送到本模块 ⇒ 用高半字节即可，且 cmds.bin 的
    //   cfg_group 本来就是 u8，`(ngrp<<4)|grp0` 恰好塞满一个字节。
    //   **向后兼容的证明**：旧驱动的 cfg_group ∈ 0..8 ⇒ [7:4]==0 ⇒ ngrp=1 ⇒
    //   走的还是"一条命令一个组"的老路；且 ST_CHK 对 0..8 之外的接受集合
    //   与改动前**完全相同**（见 ST_CHK 的注释）。
    //   ⚠ 若将来要换成 0x218 的独立寄存器：只需把下面 §1 的 ngrp_c/grp0_c 改成
    //     新端口，本模块其余部分一行不用动（自增逻辑与字段编码解耦）。
    input  logic [15:0] cfg_group,      // {ngrp[3:0], grp0[3:0]}，见上
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
    // ★ T-36：两个改进的**独立开关**（P6 用四组组合 00/10/01/11 证明互不干扰）。
    //   交付默认值都是 1（= 打开）。关掉时该路径被常量折叠掉，行为逐位回到
    //   改动前（这一点由 P1「关掉 = 与 HEAD 版逐字节 0 差」直接证明）。
    //   A/B 用法（不改本文件）：
    //     sed -i "s/localparam bit E1E2_GRP_LOOP *= 1'b1;/…= 1'b0;/" modules/bin_e1e2.v
    // ========================================================================
    localparam bit E1E2_SKIP_EMPTY_LANE = 1'b1;   // ① 空 lane 提前跳 DRAIN
    localparam bit E1E2_GRP_LOOP       = 1'b1;    // ② 单命令自增遍历 cb 个字组

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

    // ========================================================================
    // ★ T-36 §1b：两个改进的状态（4 个 FF 级 + 组合辅助）
    // ------------------------------------------------------------------------
    //  ① 空 lane：
    //     nlanes_q   本组真实 lane 数（0..8），ST_CHK 每组装一次（grp_q 变才变）
    //     nl_ok      逐 lane 有效位 = (k < nlanes_q)，**只喂写数据 mux**（不进读锥）
    //     last_lane  = nlanes_q − 1（3 位，nlanes=8 ⇒ 7，与旧 `lane==7` 逐位相同）
    //     d17_c      = max(cfg_c − 64·grp0, 0)（17 位：c ≤ C_MAX=4096）
    //  ② 组循环：
    //     ngrp_q     剩余组数（含当前组，1..9）；ST_CHK 每组 −1 之前先校验
    //     cb_c       = ceil(cfg_c/64)，用来挡住"ngrp 比这一层真正的组数还大"
    //                  （否则会把后面的 buffer 当成输出写烂）
    //     hw8_c      = hw << 3 = 每组 E_OUT 的步长（= T-35 逐层断言过的 HW·8）
    //     out_end    = out_q + hw8_c（35 位），每组进 ST_DESC 前查一次溢出
    // ========================================================================
    logic [3:0]  ngrp_q;
    logic [3:0]  nlanes_q;
    logic [7:0]  nl_ok;
    logic [2:0]  last_lane_c;
    logic [16:0] c17_c, gb17_c, d17_c;
    logic [3:0]  nlanes_c;
    logic [3:0]  grp0_c, ngrp_c;
    logic [15:0] cb_c;
    logic [34:0] hw8_c;
    logic        ovf_c;

    // ★ 时序刀 7（v40，2026-10-31）：SWEEP 期的读使能改成**寄存的倒计数器**
    // ------------------------------------------------------------------------
    //  改前（v39 及以前）：ST_SWEEP 里 `issue_en = (p < run_len)`。
    //  布线后实测（150 MHz 口径，run_len∈[1,256]）：
    //     · 起点 `run_len_reg[3]_replica` 一个寄存器 = **7,124 条违例端点（34.4%）**
    //     · 最差路径 −5.331 ns：
    //         run_len → 16 位比较 issue_en0_carry（4×CARRY4，1.46 ns）
    //         → issue_en/mem_rd_en → 包装层 rd_want → u_ce
    //         → 引擎内部 CE（2 个千级扇出网）→ DSP CEA2
    //  为什么这是结构性浪费：run_len 每个 tile 只变一次，却出现在**逐拍**读使能的
    //  组合锥里 ⇒ 整条 u_ce 使能锥都挂在它上面（CE 端点占全部违例的 51.6%）。
    //
    //  改法（**等价改写，不是删比较**）：
    //     · `rem`：SWEEP 期剩余读拍数（9 位够：run_len ≤ 256 = 9'h100）
    //     · 装载只发生在**边界**：ST_THR（每 lane 一次、进 SWEEP 前），值 = run_len
    //     · 递减只发生在 ST_SWEEP（p 递增的同一拍）⇒ rem ≡ run_len − p
    //     · 退出条件 `rem == 9'd1`  ⇔  原 `p == run_len − 16'd1`（逐位等价）
    //     · `sw_act_q`：SWEEP 期读使能，在进 SWEEP 的那一条边置 1、退出时清 0
    //  等价性证明（三段，全部可验）：
    //     (1) 进入 SWEEP 时 p=0、rem=run_len（ST_LINI 与 ST_THR 都清 p；
    //         rem 在 ST_THR 装载，且 ST_THR 里 run_len 已是本 tile 的终值：
    //         tile0 由 ST_DESC(ph=15) 装载、后续 tile 由 ST_LAST 装载，都在 ST_LINI 之前）
    //     (2) SWEEP 每拍 p+1、rem−1 ⇒ 第 k 拍 rem = run_len − k，不变量保持
    //     (3) run_len ∈ [1,256]（ST_CHK 保证 h,w≠0 ⇒ hw≥1；且 run_len=min(hw,256)）
    //         ⇒ p 只取 0..run_len−1 ⇒ 改前的 `p < run_len` 在 SWEEP 期**恒为 1**
    //         ⇒ `issue_en = sw_act_q` 与改前逐拍相同；退出拍也相同（p=run_len−1）
    //  拍数：状态机转移表一字未改（进入/退出 SWEEP 的条件等价）⇒ 总拍数不变。
    //  ⚠ 依然**不许**"直接把比较删掉"（前代理"时序刀 6"那样做被叫停）：
    //    删掉后 issue_en 与状态机失去耦合，任何让 p 越界的 bug 都会静默多发读。
    //    这里保留 rem 计数 + §5 的 translate_off 断言（p<run_len、rem==run_len−p）。
    logic [8:0]  rem;              // SWEEP 期剩余读拍（1..256），装载于 ST_THR
    logic        sw_act_q;         // SWEEP 期读使能（寄存；= 原 `p < run_len`）

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

    // ========================================================================
    // ★ T-36 §2a：本组真实 lane 数 / 组数 / 输出区间（全部由**寄存器**喂，组合）
    // ------------------------------------------------------------------------
    //  nlanes = clamp(ceil((c - 64·grp0)/8), 0, 8)
    //     · d17_c ≤ 0 ⇒ 0（本组一个真实通道都没有；旧设计会白扫 8 条空 lane）
    //     · d17_c > 64 ⇒ 8（剩下 ≥8 条满 lane，与旧设计逐位相同）
    //     · 否则 (d+7)>>3 ∈ 1..8
    //  等价性（与旧 `en_q` 掩码的对照）：旧 en_q[j] = (c - 64·grp0 - 8·lane > j)，
    //  所以"lane 至少有一个通道" ⟺ c - 64·grp0 - 8·lane ≥ 1 ⟺ lane < ceil(d/8)
    //  ⇒ nlanes 就是"en_q 非全 0 的 lane 个数"，两种写法在同一条整数轴上。
    // ========================================================================
    assign c17_c  = {1'b0, c_q};
    assign gb17_c = {11'd0, grp_q[5:0]} << 6;                    // 64·grp0 ≤ 64·8 = 512
    assign d17_c  = (c17_c > gb17_c) ? (c17_c - gb17_c) : 17'd0;

    always_comb begin
        if (d17_c == 17'd0)       nlanes_c = 4'd0;
        else if (d17_c > 17'd64)  nlanes_c = 4'd8;
        else                      nlanes_c = 4'((d17_c + 17'd7) >> 3);
    end

    assign last_lane_c = nlanes_q[2:0] - 3'd1;   // nlanes_q==8 ⇒ 7（与旧 `lane==3'd7` 相同）

    always_comb begin
        for (int k = 0; k < 8; k++) nl_ok[k] = (k < nlanes_q);
    end

    // ---- ② 组循环：字段解码 + 上界 + 步长 + 溢出
    assign grp0_c = cfg_group[3:0];
    assign ngrp_c = (cfg_group[7:4] == 4'd0) ? 4'd1 : cfg_group[7:4];   // 0 ⇒ 1 组（向后兼容）
    assign cb_c   = (c_q + 16'd63) >> 6;                               // ceil(c/64) ≤ 64
    assign hw8_c  = {3'd0, hw_q} << 3;                                  // = HW·8（E_OUT 步长）
    // 本组的输出区间 [out_q, out_q + HW·8) 必须整个落在 32 位地址空间里
    assign ovf_c  = (({3'd0, out_q} + hw8_c) > 35'h1_0000_0000);

    // ========================================================================
    // ★ v60 R2：阈值加 k 位小数（k = 6 ⇒ 1/64 LSB），语义仍是严格 `> 0`
    // ------------------------------------------------------------------------
    //   旧：bit = ( sext(src_byte) + thr_q        > 0 )     阈值分辨率 1 LSB
    //   新：bit = ( sext(src_byte)·2^6 + thr_k   > 0 )     分辨率 1/64 LSB
    //   为什么要它：40 304 个 (层,通道) 里 30.0% 的阈值落在半个 LSB 内被舍成 0，
    //   对占 20% 的 ReLU 精确 0 激活判成 −1（模型判 +1）⇒ 每层中位 9.11% 符号翻错。
    //   位宽（RTL_SPEC §R2 已证）：|x·2^6| ≤ 128·64 = 8192、|thr_k| ≤ 5249
    //   ⇒ 和 ≤ 13441 < 32767 ⇒ **16 位有符号加法器不用加宽**；`<<< 6` 是常量
    //   移位（纯布线，0 LUT）；thr 表大小不变（同槽同大小，只是打包端换算）。
    //   x = 0（ReLU 精确零）：bit = (thr_k > 0) = (t < 0) ✔ 与 sign(0+t) 一致。
    //   容器侧：thr_k = round(t · 2^f · 2^6)（ps/tools/pack_bin_weights.py）。
    // ========================================================================
    localparam int THR_K = 6;
    logic signed [15:0] xs_c [0:7];
    always_comb begin
        for (int j = 0; j < 8; j++)
            xs_c[j] = $signed({{8{mem_rd_data[j*8+7]}}, mem_rd_data[j*8 +: 8]})
                      <<< THR_K;
    end
    // bit_j = en_j && (src_byte_j·2^K + THR_j > 0)
    always_comb begin
        for (int j = 0; j < 8; j++) begin
            bcmp[j] = en_q[j] && ((xs_c[j] + $signed(thr_q[j])) > 0);
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
            //
            // ★ v40 时序刀 7：比较**不再删**，而是等价改写成寄存使能 `sw_act_q`
            //   （边界装载；证明见 §1 的 rem/sw_act_q 注释块）。
            //   SWEEP 期 sw_act_q 恒 1 ⇔ 原 `p < run_len` 恒 1 ⇒ 逐拍等价。
            if (sw_act_q) begin
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
            ngrp_q <= 4'd1;  nlanes_q <= 4'd8;
            desc_q <= '0; out_q <= '0; hw_q <= '0; tile_q <= '0;
            lane <= '0; ph <= '0; p <= '0; run_len <= '0;
            rem <= '0;  sw_act_q <= 1'b0;
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
                // ★ T-36 ②：字段解码见端口表。旧驱动（0..8）⇒ grp0=原值、ngrp=1，
                //   与改动前逐位相同。
                grp_q <= {12'd0, cfg_group[3:0]};
                ngrp_q <= (E1E2_GRP_LOOP && (cfg_group[7:4] != 4'd0))
                          ? cfg_group[7:4] : 4'd1;
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
                // ------------------------------------------------------------------
                // ★ T-36 ②：下面两条**只在 ngrp>1 时可能为真**。
                //   旧驱动 ngrp≡1 ⇒ 两条都是 0 ⇒ 接受/拒绝的集合与改动前**逐位相同**
                //   （包括 err_code 的取值与优先级顺序：新检查排在老的 5 条之后）。
                //   ⚠ 这一段是本改动唯一的"会写坏内存"风险，所以必须在**第一组进
                //     ST_DESC 之前**就挡住，而不是靠事后发现。
                // ------------------------------------------------------------------
                end else if (E1E2_GRP_LOOP && (ngrp_q > 4'd1) &&
                             (({12'd0, grp_q[3:0]} + {12'd0, ngrp_q}) > {12'd0, cb_c})) begin
                    // grp0 + ngrp 越过本层真正的组数 ceil(c/64) ⇒ 后面那段不是本层
                    // 的输出（是别的 buffer）⇒ 一个字节都不许写。
                    err_code <= ERR_UNIT;  state <= ST_DONE;
                end else if (E1E2_GRP_LOOP && (ngrp_q > 4'd1) && ovf_c) begin
                    // 本组输出区间越过 32 位地址空间（只可能出现在畸形 base_out/H/W）。
                    err_code <= ERR_SIZE;  state <= ST_DONE;
                end else begin
                    lane  <= '0;  ph <= '0;
                    // ★ T-36 ①：本组真实 lane 数（关掉改进时恒 8 = 旧行为）
                    nlanes_q <= E1E2_SKIP_EMPTY_LANE ? nlanes_c : 4'd8;
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
                // ★ T-36 ①：本组一条真实 lane 都没有（nlanes==0）⇒ 一次读都不发，
                //   直接去 FLUSH 把这一组（"全空 lane"组）的**全零**输出写出去。
                //   旧设计在这里会把 8 条空 lane 扫一遍（8·run_len 次白读）再写同样
                //   的全零；输出逐字节相同，只有读拍数不同。
                //   nlanes_q==0 只可能来自畸形命令（grp0 ≥ ceil(c/64)），正常驱动
                //   不会发；但它必须被正确实现，否则 acc_b 的残值会被写出去。
                if (nlanes_q == 4'd0) begin
                    wp_q  <= '0;
                    state <= ST_FLUSH;
                end else begin
                    state <= ST_THR;
                end
            end
            // ---------------------------------------------------------------
            ST_THR: begin
                // 第 2 刀：扫地址在这里算好；ST_SWEEP 只做 +8 递增
                // ★ 时序刀 4：改由 src_base_q 相加（寄存器+寄存器），不再从 desc_ram 输出起算
                rd_addr_q <= src_base_q + (tile_q << 3);
                // ★ v40 时序刀 7：**边界装载倒计数器**。ST_THR 是本 lane 进 SWEEP 前的
                //   最后 4 拍，此处的 run_len 已是本 tile 的终值（tile0 由 ST_DESC(ph=15)、
                //   后续 tile 由 ST_LAST 装载，两者都在 ST_LINI 之前）⇒ 这里装载
                //   rem = run_len 后，SWEEP 每拍 −1 就恒等于 run_len − p。
                //   每 lane 装一次（ST_THR 4 拍写同值，无害）；换 lane 时 run_len 不变，
                //   所以 lane 1..7 也走同一条装载路径 —— **没有第二处装载点**。
                rem <= run_len[8:0];
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
                        sw_act_q <= 1'b1;               // 进 SWEEP 的这条边就当拍置位
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
                // ★ v40 时序刀 7：退出条件从 `p == run_len-1`（16 位减法+比较）
                //   改成 `rem == 9'd1`（9 位常数比较）。两者逐位等价，见 §1 证明 (1)~(3)。
                rem <= rem - 9'd1;
                if (rem == 9'd1) begin
                    p        <= '0;
                    sw_act_q <= 1'b0;               // 退出这条边清使能（下一拍 ST_DRAIN）
                    state    <= ST_DRAIN;
                end else begin
                    p <= p + 16'd1;
                end
            end
            // ---------------------------------------------------------------
            ST_DRAIN: begin
                // 收最后一拍（数据写进 acc_b 在独立的无复位 always_ff 里）
                // ★ T-36 ①：退出条件从常数 `lane==7` 改成 `lane==nlanes−1`。
                //   nlanes==8 时 last_lane_c 逐位等于 3'd7 ⇒ 关掉改进 = 旧代码。
                if (lane == last_lane_c) begin
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
            // ---------------------------------------------------------------
            // ★ T-36 ②：本组收尾。若还有组要跑 ⇒ **不抬 done**，当场自增后回 ST_CHK。
            //   为什么自增放在这里而不是新增一个 ST_GADV 状态：
            //   旧流程"跑完一组"的 busy 拍序列是 ……FLUSH → LAST(1) → DONE(1) → [IDLE(1)]
            //   → CHK(1) → DESC(16)，IDLE 那一拍是 **PS/包装层的外部周转**，不属于
            //   单元的 busy 拍。放在 ST_DONE 里自增 ⇒ busy 拍序列变成
            //   ……LAST(1) → DONE(1) → CHK(1) → DESC(16)：**每组的工作拍数与旧流程
            //   逐拍相同**（这正是 P2 判据"总拍数不变"要的），而省掉的 IDLE 那一拍
            //   本来就属于"重新发一条命令"的外部开销。
            //   自增的 3 个字段是 T-35 逐层断言过的严格等差：+64 / +HW·8 / +1。
            // ---------------------------------------------------------------
            ST_DONE: begin
                // ⚠ **错误必须当场收工**：`err_code != 0` 就是"这一组已经判死了"，
                //   绝不能再自增到下一组。首版漏了这一条（只判 ngrp_q），实测被
                //   err_ngrp_ovf 用例抓到：ngrp=9 且越界时，它把 8 个组的 ST_CHK
                //   全跑完、还真的把最后一组写进了输出（71 拍 / 35 笔写），
                //   正是任务书点名禁止的"等跑完 8 组"。
                if (E1E2_GRP_LOOP && (err_code == ERR_OK) && (ngrp_q > 4'd1)) begin
                    ngrp_q <= ngrp_q - 4'd1;
                    desc_q <= desc_q + 32'd64;            // E_DESC += 64
                    out_q  <= out_q  + hw8_c[31:0];       // E_OUT  += HW·8
                    grp_q  <= grp_q  + 16'd1;             // E_GROUP += 1
                    // lane/ph/tile_q/run_len 由下面的 ST_CHK→ST_DESC(ph=15) 重新装载，
                    // 与"新来一条命令"走的是同一条装载路径（没有第二处初始化点）。
                    state  <= ST_CHK;
                end else begin
                    ctrl_done <= 1'b1;
                    state     <= ST_IDLE;
                end
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
                //
                // ★ T-36 ①：**空 lane 的字节必须强制 0**。
                //   旧设计把空 lane 也扫了一遍、bcmp 恒 0 写进 acc_b[k]，所以直接
                //   拼 acc_b 就是对的；现在空 lane 不再被扫 ⇒ acc_b[k] 里是上一 tile
                //   的残值（仿真里甚至是 X）⇒ 必须在这里按 nl_ok 掩掉。
                //   nl_ok 来自寄存器 nlanes_q（本组内恒定），所以这是"寄存器 → 8 个
                //   与门 → 寄存器"，0 级加法/比较、不进任何读写使能锥。
                //   关掉改进时 nl_ok ≡ 8'hFF，综合器把 8 个与门折叠成直连。
                wr_data_c[7:0]   <= nl_ok[0] ? acc_b[0][pend_p[ACC_AW-1:0]] : 8'd0;
                wr_data_c[15:8]  <= nl_ok[1] ? acc_b[1][pend_p[ACC_AW-1:0]] : 8'd0;
                wr_data_c[23:16] <= nl_ok[2] ? acc_b[2][pend_p[ACC_AW-1:0]] : 8'd0;
                wr_data_c[31:24] <= nl_ok[3] ? acc_b[3][pend_p[ACC_AW-1:0]] : 8'd0;
                wr_data_c[39:32] <= nl_ok[4] ? acc_b[4][pend_p[ACC_AW-1:0]] : 8'd0;
                wr_data_c[47:40] <= nl_ok[5] ? acc_b[5][pend_p[ACC_AW-1:0]] : 8'd0;
                wr_data_c[55:48] <= nl_ok[6] ? acc_b[6][pend_p[ACC_AW-1:0]] : 8'd0;
                wr_data_c[63:56] <= nl_ok[7] ? acc_b[7][pend_p[ACC_AW-1:0]] : 8'd0;
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

    // ========================================================================
    // 5. 断言（**只仿真**）：守住 rem 倒计数器 / sw_act_q 与原 `p < run_len` 的等价性
    // ------------------------------------------------------------------------
    //   为什么要它：时序刀 7 把"每拍比较"换成了"边界装载的计数器 + 寄存使能"。
    //   这类改写最怕的是**在某个边角状态**下不再是同一个函数（多发/少发读）。
    //   逐字节 A/B（106 命令 / 24 层）抓的是"值错了"，抓不到"多读了一拍、又恰好
    //   被后写覆盖"这种。所以这里把不变量逐拍钉死：
    //       (A) SWEEP 期 p < run_len                 ← 原比较的左半（这是"恒真"的依据）
    //       (B) rem ≡ run_len − p                    ← 倒计数器与 p 同步
    //       (C) SWEEP 期 sw_act_q ≡ 1                ← 原比较的取值（恒真）
    //       (D) 非 SWEEP 期 sw_act_q ≡ 0
    //       (E) 进 SWEEP 前 rem 已装载且非 0、run_len 非 0
    //   `synthesis translate_off` ⇒ 综合完全看不到（不占面积、不进时序、不进 8-4767）。
    // ========================================================================
    // synthesis translate_off
    // ---- T-36 断言的影子寄存器（**不受 ap_ce 门控**：包装层停 CE 时状态不变，
    //      影子跟着冻结，才不会把"CE 冻了 3 拍"误判成"自增错"）----
    state_t      s_state;
    logic [15:0] s_grp_q, s_ngrp_q;
    logic [31:0] s_desc_q, s_out_q, s_hw_q;
    always_ff @(posedge ap_clk) begin
        s_state  <= state;
        s_grp_q  <= grp_q;
        s_ngrp_q <= {12'd0, ngrp_q};
        s_desc_q <= desc_q;
        s_out_q  <= out_q;
        s_hw_q   <= hw_q;
    end

    always_ff @(posedge ap_clk) begin
        if (ap_rst_n && ap_ce) begin
            if (state == ST_SWEEP) begin
                if (!(p < run_len)) begin
                    $error("[E1E2-ASRT] SWEEP 期 p=%0d 不满足 p<run_len=%0d（(A) 不变量破）",
                           p, run_len);
                end
                if (rem !== (run_len[8:0] - p[8:0])) begin
                    $error("[E1E2-ASRT] rem=%0d != run_len-p=%0d（(B) 倒计数器失步）",
                           rem, run_len - p);
                end
                if (sw_act_q !== 1'b1) begin
                    $error("[E1E2-ASRT] SWEEP 期 sw_act_q=0（(C) 读使能丢了，会少发读）");
                end
                if (rem == 9'd0) begin
                    $error("[E1E2-ASRT] SWEEP 期 rem=0（(E) 计数器已空，会多发读）");
                end
            end else begin
                if (sw_act_q !== 1'b0) begin
                    $error("[E1E2-ASRT] 非 SWEEP 状态(state=%0d) sw_act_q 仍为 1（(D) 会多发读）",
                           state);
                end
            end
            if ((state == ST_THR) && ph[0] && (ph == 4'd3)) begin
                if (rem == 9'd0) begin
                    $error("[E1E2-ASRT] 即将进 SWEEP 但 rem=0（(E) run_len 未装载 / >256）");
                end
                if (run_len == 16'd0) begin
                    $error("[E1E2-ASRT] 即将进 SWEEP 但 run_len=0（(E) 会空转）");
                end
            end

            // ================================================================
            // ★ T-36 的不变量（只仿真）。为什么需要：①"少读"和"少写"是两种不同
            //   的错误，逐字节 A/B 抓不到"读少了但输出恰好还对"；②"组自增走错"
            //   会安静地把下一层的数据写进上一层。所以把这两条钉成逐拍断言。
            // ================================================================
            if (E1E2_SKIP_EMPTY_LANE) begin
                // (F) 只许扫真实 lane：SWEEP/THR 期 lane 必须 < nlanes_q
                if (((state == ST_SWEEP) || (state == ST_THR)) && (lane >= nlanes_q)) begin
                    $error("[E1E2-ASRT] T36① state=%0d 扫到 lane=%0d ≥ nlanes=%0d（多读了空 lane）",
                           state, lane, nlanes_q);
                end
                // (G) DRAIN 只能停在最后一条真实 lane 上
                if ((state == ST_DRAIN) && (lane >= nlanes_q)) begin
                    $error("[E1E2-ASRT] T36① DRAIN 的 lane=%0d ≥ nlanes=%0d", lane, nlanes_q);
                end
                // (H) **写出的空 lane 字节必须全 0**（这是"跳读"能成立的前提：
                //     acc_b[k] 里留着上一 tile 的残值/仿真 X）
                if (mem_wr_en) begin
                    for (int k = 0; k < 8; k++) begin
                        if (!nl_ok[k] && (mem_wr_data[k*8 +: 8] !== 8'd0)) begin
                            $error("[E1E2-ASRT] T36① 空 lane k=%0d 写出了非 0 字节 %02h（acc 残值泄漏）",
                                   k, mem_wr_data[k*8 +: 8]);
                        end
                    end
                end
            end
            // (I) 越过 ceil(c/64) 的组**只能**是"一个真实通道都没有"的零组
            //    （那种组不读不写真实通道，只写全零；P1 的 en_q 全 0 边界就是它）。
            //    若越界组还有真实 lane，说明"写到别人 buffer 上"了 ⇒ 必须报。
            //    ⚠ 首版这条写成了"无条件 grp<cb"，把 en_q 全 0 的合法边界用例打成
            //      误报（c64_h5w7_g5 用例 16 次）⇒ 2026-11-02 放宽为下面这条。
            //    ⚠ 用 nlanes_c（= 真实 lane 数）而不是 nlanes_q：后者在
            //      E1E2_SKIP_EMPTY_LANE=0 时被强制成 8（"全扫"的语义），
            //      拿它判越界会对合法用例误报（c64_h5w7_g5 用例上翻过车）。
            if ((state == ST_DESC) && (grp_q >= cb_c) && (nlanes_c != 4'd0)) begin
                $error("[E1E2-ASRT] T36② grp=%0d >= cb=ceil(c/64)=%0d 且本组有 %0d 条真实 lane（输出区间不属于本层）", grp_q, cb_c, nlanes_c);
            end
            // (J) 组自增的三条等差（T-35 §3.2 的 +64 / +HW·8 / +1），在**进入
            //     ST_CHK 的那一拍**逐条核对；只有"上一拍是 ST_DONE"才查
            if (E1E2_GRP_LOOP && (state == ST_CHK) && (s_state == ST_DONE)) begin
                if (grp_q !== (s_grp_q + 16'd1))
                    $error("[E1E2-ASRT] T36② grp 自增错：%0d → %0d", s_grp_q, grp_q);
                if (ngrp_q !== (s_ngrp_q - 4'd1))
                    $error("[E1E2-ASRT] T36② ngrp 自减错：%0d → %0d", s_ngrp_q, ngrp_q);
                if (desc_q !== (s_desc_q + 32'd64))
                    $error("[E1E2-ASRT] T36② E_DESC 步长不是 64：%08h → %08h", s_desc_q, desc_q);
                if (out_q !== (s_out_q + (s_hw_q << 3)))
                    $error("[E1E2-ASRT] T36② E_OUT 步长不是 HW*8：%08h → %08h (hw=%0d)",
                           s_out_q, out_q, s_hw_q);
                if (ngrp_q == 4'd0)
                    $error("[E1E2-ASRT] T36② ngrp 下溢");
            end
        end
    end
    // synthesis translate_on

    // h_q/w_q 只参与 ST_CHK 的合法性判断（保留寄存器便于调试观察）
    logic unused_ok;
    assign unused_ok = &{1'b0, h_q[0], w_q[0], 1'b0};

endmodule
