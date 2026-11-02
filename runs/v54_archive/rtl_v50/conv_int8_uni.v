// ============================================================================
//  conv_int8_uni.v —— 为二值网定制的统一 int8 卷积引擎（32 lane / 单主内存口）
//
//  规格：docs/binary/04_定制int8引擎规格.md（权威）
//  端口、内存映射、定点语义以该文档 + tb/tb_conv_int8_uni.v 头部注释 +
//  tools/gen_uni_golden.py 为准。**端口一个字都没改**（原文件是永不 done 的桩）。
//
//  取代 conv_k1_engine + conv_k3_engine + conv_k5_engine。三个"编译期焊死"的
//  老毛病在这里全部消除：
//    · k ∈ {1,3,5}   —— 运行时 cfg_k，只影响循环次数/(ky,kx) 递增，数据通路不变；
//    · s ∈ {1,2}     —— 运行时 cfg_s，只影响 iy/ix 的基准增量；
//    · d ∈ {1,2,3}   —— 运行时 cfg_d，只影响 ky/kx 的步长；
//    · H / W ≤ 256   —— 运行时 cfg_h / cfg_w，**没有任何按 W 综合的常量**
//                       （老 k5 把 LB_DEPTH=W+2*PAD 焊死，换分辨率就要重综合）。
//  本引擎连行缓冲都不需要（下面 §3 解释为什么），所以连 LB_DEPTH 都不存在。
//
//  ============================================================================
//  1. 计算语义（与 gen_uni_golden.py 逐位一致）
//  ============================================================================
//      acc[oc] = bias[oc] + Σ_ic Σ_ky Σ_kx in[ic][oy*s+ky*d-pad][ox*s+kx*d-pad]
//                                       * w[oc][ic][ky*k+kx]
//      out     = sat8(relu ? max(0, acc >> shift) : (acc >> shift))
//
//    · 输入 / 权重都是 int8（$signed），乘积 int32 累加；中间量全为有符号；
//    · '>>' 是**算术右移**（负数向 -∞），RTL 用 '>>>'，与 Python 的 '>>' 同义；
//    · 越界（iy<0 或 iy>=H 或 ix<0 或 ix>=W）的抽头贡献 0：本引擎"不发读"，
//      数据通路用 0 代替（等价于 golden 里的 'if iy<0 or iy>=H: continue'）。
//      iy/ix 一律按**有符号**比较，否则 pad>0 时 iy=-1 会被当成 4G 误读内存。
//    · 输入通道补齐：输入按 C8 布局存 cp8=ceil8(cin) 个通道，cin..cp8-1 全是 0
//      （golden 生成器写的）；但权重容器里没有这些行 ⇒ 权重 bank 对应 lane 是
//      未写过的 X。4 态仿真里 0*X=X 会污染整个累加器（实机 0*任意=0 没问题），
//      所以数据通路对 "cg*8+ch >= cin" 的车道**显式乘 0**（见 §13），
//      与 golden 的 'if c >= cin: continue' 逐位等价。
//    · 输出通道补齐：cout 补齐到 co8 的字节在 golden 里是 0，所以引擎对 o>=cout
//      **不写**（写队列的 mask，见 §10）即逐字节一致。
//    · 权重容器沿用现有格式：TAP = cfg_tap_stride ∈ {4,12,28}，
//      地址 = base_wgt + ((o*cin + c)*TAP + j)，j = ky*k+kx（只用 j<k²）。
//
//  ============================================================================
//  2. 状态机（6 态）
//  ============================================================================
//    ST_IDLE  等 ctrl_start 上升沿；锁存全部 cfg_*/base_*，组合算出派生常量
//             （Hout/Wout/k²/NCG/NOG/W*8/s*W*8/d*W*8/pad*8/pad*W*8/4*cin*TAP
//              等），把 og 指针复位。**不读内存**。两个"真"乘法（H*W*8、
//              Hout*Wout*8）挪到 ST_BIA 用已锁存副本算（各 1 个 DSP）。
//    ST_BIA   装当前 oc 组的 4 个 int32 偏置（2 次 8 字节对齐读，共 4 拍），
//             顺便算 hw8/p8（BIA 的 4 拍空转足够掩盖 1 个 DSP 的延迟）。
//    ST_WLD   装当前 oc 组的权重进 4 个 bank（每拍 1 字节，见 §4）。
//             总拍数 = 4*cin*k²（= 本组有效权重字节数），与 TAP 的补齐字节无关。
//    ST_CMP   计算主体：**每拍一个 term**，term = (cg, j)，cg = 8 通道组号
//             (0..NCG-1)，j = 0..k²-1。一拍 = 32 个 MAC（4 oc × 8 ch）。
//             每拍发一次 8 字节输入读（C8 一个整字 = 8 个通道），下一拍数据回来，
//             与内部权重 bank 的读数据同拍对齐，先寄存一级（§11b 的 S1a），
//             再进 4×8 = 32 个乘加（S1b）。
//    ST_DRN   等写入队列 + 收尾流水排空（固定 13 拍；现在最深是 6 拍收尾
//             （拍A..拍E）+ 4 拍写队列 = 11 拍，检查落在第 13 拍，余量 1 拍），
//             然后换下一个 oc 组（NOG = ceil(cout/4) 组）。拍数核算见 §18。
//    ST_DONE  ctrl_done 单拍脉冲，回 IDLE。
//
//  ============================================================================
//  3. 数据流 / 流水线（为什么不需要行缓冲）
//  ============================================================================
//  本引擎是"权重驻留 + 输入流式"结构：
//    · 权重：一个 oc 组（4 个 oc）的*全部*权重（cin*k² 字节/oc）装进 4 个 bank，
//      轮流被 4 个 oc 车道使用；换组才重装。
//    · 输入：**每个 term 读一次 8 字节的 C8 整字**（= 8 个通道 × 1 个像素），
//      这 8 个字节正好喂满 8 条通道车道，一个字节都不浪费。
//      term 数/像素 = NCG*k²，MAC 数/像素 = cin*k²*4，所以
//      吞吐 = 32 MAC/拍 且外部读 = 1 字/拍 —— 读口带宽与 MAC 带宽**恰好匹配**。
//      既然读口没有富余，行缓冲（把输入先搬进片上再复用）对本结构没有任何
//      收益，只会多花 3 块 RAMB36 和一堆控制，所以刻意不做。
//
//  流水线（8 级，逐拍推进，只有写回拥塞时才反压）：
//    S0 发读拍：计数器给出 term(cg,j) → 发 mem_rd_en/addr（输入 C8 字）+
//              同时给出权重 bank 读地址 e = cg*k² + j。
//    S1a 输入寄存拍：输入字 / 权重字同拍到齐 → **寄存进 DSP 输入侧的那一级
//              寄存器（§11b 的 xr_q/wr_q，每车道 2×8 bit）**。
//              ★ 2026-10-30 新增：BRAM 输出 → DSP 的 NET 1.579 ns（两块硬核
//              在物理上隔得很远）只能靠"插寄存器把一条路径切成两条"来治，
//              逻辑已经没有可拆的东西了（那一锥只有 2 级 LUT）。
//    S1b 乘加拍：xr_q/wr_q（已打一拍）→ 32 个 8bit×8bit → 累加到 32 个 int32
//              累加器（每车道一个 = 1 个 DSP48 的 25×18 乘 + P 回环加）。
//              清零改成"**本像素末拍**同步清零"（旧版是"下一像素首拍"），于是
//              清零走 DSP 的 RSTP、不在加法回路上，Vivado 才能把 32 条 MAC 全收
//              进 DSP（旧版三层门 + 异步复位 ⇒ 一条都没进，全在 LUT 里）。
//    S2a 归约拍A：末拍的乘加**写进 acc 之后**，把 4 个 bank 各自 8 条车道拆成
//              两个"4 lane 半和"（2 项加法 = 2 级）寄存进 smhalf。取的是**已寄存的
//              acc**，乘法器输出没有组合扇出，DSP 推断才干净。
//    S2b 归约拍B：sm0..sm3 = smhalf[2i] + smhalf[2i+1]（1 级），再减 s2tprev
//              得到本像素和（再 1 级）⇒ 组合路径 4 级进位链降到 2 级。见 §14。
//    S3a 收尾拍①：q = sum + bias[oc]            （32 位加，见 §15）
//    S3b 收尾拍②：v = q >>> shift               （32 位算术桶形 6 级，见 §15）
//    S3c 收尾拍③：relu + 上下饱和 → 4 个 int8   （**组合**，只喂 rb0..rb3 的 D 端）
//              ★ 2026-10-30：原来这三件事压在一拍（sat_sh），实测 24 级、
//                slack −1.656 ns（s2sum_reg → rb0_reg）。拆成 3 级后每级只剩
//                "一次 32 位加 / 一次桶形 / 一次比较饱和"。
//    WB 写回：4 个字节按 C8 连续地址（同一 oc 组内 (o%8) 连续）用 8bit 写口
//              每拍 1 字节吐出，**与后续计算完全重叠**（写口是独立端口）。
//              只有当"每像素拍数 < 4"（例如 cin≤7,k=1 的 SGRA 层）时才会反压。
//
//  每拍 MAC 数：稳态恒为 32（4 oc × 8 ch），与 k / s / d / W 无关。
//
//  ============================================================================
//  4. 权重缓冲（4 × RAMB36，共 128 Kbit）
//  ============================================================================
//    4 个 bank，bank b ↔ 本组第 b 个 oc（b=0..3）。每个 bank 是
//        DEPTH=512 × 64bit，一个字 = 8 个连续通道（通道 c 占字节 c%8）。
//    条目号： e = (c/8)*k² + j         （c = 通道号，j = tap 号）
//    约束：   ceil(cin/8)*k² <= 512 —— 本仓库 6 个验收形状最大 72，二值网实际层
//             最大 50（conv_first: cin=12,k=5）与 72（decoder: cin=64,k=3）。
//    资源：  512×72 模式的 RAMB36 正好 4 块（每 bank 一块），远低于规格 §4 的
//             权重 4–6 块预算；行缓冲 0 块。
//
//    装载（ST_WLD）：按容器**字节流**顺序搬，每拍写 1 个字节（字节使能写）。
//      ⚠ 写口必须是 UG901 的 byte-write-enable 模板：64 bit 写数据 + 8 个
//      **静态**字节切片 + 由 wl_c[2:0] 译码出的 1 bit 写使能。左侧动态切片
//      'bank[a][8*c +: 8]' 只能推成 1 bit 宽 RAM（byte width=1 不是 8 的倍数）：
//      实测 4 块 RAMB36 → 64 个 tile + 2048 LUT 的 LUTRAM（见 §17 的注释）。
//      读地址 = 块基址 + ((b*cin + c)*TAP + j0)，j0 ∈ {0,8,16} 且 j0<k²，
//      每次读回 8 字节 = 该行的 j0..j0+7；只搬 j<k² 的字节（补齐字节不占拍）。
//      所以装载拍数 = 4*cin*k²，正好等于本组有效权重字节数，不随 TAP 的
//      3 字节补齐膨胀（k=1 时 TAP=4，若按容器字节流走会多花 4 倍）。
//
//  ============================================================================
//  5. k / s / d / W 是怎么"统一"的
//  ============================================================================
//    统一 tap 发生器：term 序号 j = ky*k + kx，j 从 0 数到 k²-1，
//      ky = j/k，kx = j%k —— RTL 里不除，用 kx/ky 两个计数器：kx 每次 +1，
//      kx 到 k-1 归零并让 ky+1；j 到 k²-1 归零并让 cg+1。
//    坐标（iy/ix，只用于越界比较）用**增量**维护，全部 17 bit 二进制补码：
//      iy = oy*s + ky*d - pad      ⇒ ybase = oy*s-pad, iy = ybase + ky*d
//      ix = ox*s + kx*d - pad      ⇒ xbase = ox*s-pad, ix = xbase + kx*d
//      增量：kx++ → ix += d；  kx 回绕 → ky++(iy += d), ix = xbase
//            j 回绕 → cg++，且 ky/kx 归零, iy = ybase, ix = xbase
//            像素 ox++ → xbase += s, ix = xbase+s；ox 回绕 → oy++(ybase += s), ix = -pad
//      k 只改"j 数到几"，s 只改"像素基准增量"，d 只改"tap 步长"，
//      W 只改"行字节数"——三者都不进数据通路，所以一份比特流通吃全部形状。
//    **读地址**是另一套寄存器（§6b）：三个相位各有一个累加器/指针，每拍只做
//      1 次加法，且 mem_rd_addr 直连寄存器输出（0 级组合）。
//
//  ============================================================================
//  6. 读延迟（BRAM 语义，TB 与实机一致）
//  ============================================================================
//    mem_rd_en 在 T 拍拉高 → T+1 拍 mem_rd_data 才有效。所以：
//      · 计算主体：T 拍发读、T+1 拍做 32 个乘加；r1_* 元数据随 T 拍登记，
//        与 T+1 拍的数据严格对齐；
//      · 偏置 / 权重装载：显式"发读拍 / 收数拍"两相，不依赖隐含对齐。
//    内部权重 bank 也是同步读（1 拍延迟），地址与输入读**同一拍**给出，所以
//    权重数据与输入数据同拍到 S1，不需要额外的对齐寄存器。
//
//  ============================================================================
//  6b. 读地址是**寄存器输出**（150 MHz 关键路径修复，2026-10-30）
//  ============================================================================
//  综合后 150 MHz WNS = -11.2 ns（周期 6.667 ns），逐 cell 拆解最差路径的
//  35 级里有 16 级（6.28 ns，38%）落在 mem_rd_addr 的组合逻辑上：
//      kk_q →(7 级) wl_left → wl_nb → wl_tend → wl_rowend → wl_rbn/wl_j0n
//           →(4 级) 32 bit 加法 + 3:1 读源 mux → 包装层 26 bit tag 比较 ×32
//      （★ P6：wl_left 这条减法锥已经删掉，wl_nb 改成 jglast ? rem : 8，见 §8a；
//        wl_rbn/wl_j0n 现在只剩仿真自检用，硬件上不再有这条路径。）
//  根因是"**地址每拍从计数器重算**"：wl 分支是 wbase+rb+j0 的三级串联加法。
//
//  读口契约只要求一件事（§6 + 包装层 rd_live = u_ce & u_rd_en & rd_hit）：
//    **mem_rd_en=1 的那一拍，mem_rd_addr 必须是这次读的地址**；
//    mem_rd_en=0 的拍地址是 don't care（BRAM 读数据寄存器的 CE 被 rd_live 挡住）。
//  ⇒ 地址完全可以**提前一拍**算好放进 rd_addr_q：
//      · mem_rd_addr = rd_addr_q          （0 级组合，包装层 tag 比较从 FF 出发）
//      · rd_addr_q 的 D 端 = 1 次 32 bit 加法（每个分支一个预存步长）
//  ⚠ 因此 mem_rd_en **保持组合**、不跟着打拍：rd_addr_q 在 t-1 沿装的是 t 拍那个
//    term 的地址，t 拍的使能与它指同一次读。使能若再打一拍，就会错位到上一个
//    地址（这正是"发读拍地址必须同拍"的那条约束，只是方向相反）。
//
//  三个相位各自增量维护（不再用 wbase+rb+j0 每次重算）：
//    · BIA（2 次读/组）：c1 收数拍装 bbase+8（c2 读）；c3 装 bbase+16（下一组 c0 读）。
//    · WLD（每 8 个消费拍 1 次读）：**固定步长 +8**（★ P5 之后）。
//        旧版用指针 wl_rem 记"本通道还剩几个 j0 组"、步长
//        = (最后一组 ? TAP-j0_last : 8)；在 TAP' ∈ {8,16,32} 下
//        TAP'-j0_last ≡ 8 对 k=1/3/5 恒成立（见 §2），两支相等 ⇒ 常数。
//        ⚠ 容器是 c*TAP+j（TAP≠k²，每通道有补齐洞），所以通道边界步长**不是 1**；
//          +8 之所以对，是因为 TAP' 恰好是 8 的倍数且 j0_last = k²-1 是最后一组起点
//          （旧 TAP{4,12,28} 下该步长是 4，写 8 会整段读错 —— 回退时必须一起改）。
//    · CMP：kx++ → +d*8；ky++ → +d*W*8-(k-1)*d*8；cg++ → +H*W*8-(k-1)*d*W*8-(k-1)*d*8；
//        像素推进 → pix_base+s*8（ox++）/ rowy_nxt-pad*8（oy++ 回行首）。
//        后两个寄存器保证像素推进也只有 1 次加法。
//  两个步长（step_ky / step_cg）都是**启动常量**，在 ST_IDLE/ST_BIA
//  用锁存副本算一次（cfg_* 端口不直接进加法链），此后每拍只做 1 次加法。
//  （★ P5：第三个步长 wl_dlast 因为恒等于 8 已经消失，见上。）
//  §9b 末尾还有一段 // synthesis translate_off 的逐拍地址等价性自检：用三个
//  **独立的**表达式（wbase+rb+j0 / in_b+iy*W*8+ix*8+cg*hw8 / bbase+bia_i*8）
//  对比 rd_addr_q，任何 off-by-one 都会在仿真里直接打印 MISMATCH，不会静默读错。
//
//  ============================================================================
//  7. 实测拍数（官方 TB 的 cyc 计数值；参考 = cout*Hout*Wout*cin*k²/32）
//  ============================================================================
//    每组拍数 = 1(WLD 进入拍) + 4(bias) + 4*cin*k²(装载) 或 ceil(cin/8)... 见下
//             + Hout*Wout*NCG*k²(计算) + 12(排空)
//    形状                         参考      实测     比值
//    uni_k1_64x12_8x8            1536     2360    1.54
//    uni_k3_64x12_8x8           13824    20792    1.50
//    uni_k5_12x48_8x8           28800    53018    1.84
//    uni_k3_d3_32x16            36864    41546    1.13
//    uni_k1_s2_192x64            6144    18722    3.05   ← 见下
//    uni_k3_nopad_16x8           2160     3350    1.55
//    比值可解析写成 1 + 32/(Hout*Wout)（"装载 1 字节/拍"这一项），再乘上
//    「cin 补到 8 的倍数」的浪费（只有 cin=12 的那个 k5 形状有，×1.33）。
//    ⇒ 这 6 个形状是 8×8 / 16×16 的小图，Hout*Wout 只有 16~256；二值网真实层
//      （conv_first 128² = 16384、decoder 160²/80²/40²）的 Hout*Wout 大 1~3 个
//      数量级，装载项 < 0.2%，实际比值 ≈ 1.0~1.33。
//    uni_k1_s2_192x64 的 3.05× 是"TAP=4 的 3 字节补齐 × 4×4 小输出图"叠加：
//    12288 个有效权重按 1 字节/拍装 = 12288 拍，正好是计算拍数 6144 的两倍。
//    该形状要压到 1.5× 必须把装载提速到 >=2 字节/拍（读 8 字节里只 2 个有效
//    权重 → 一次读搬 2 个通道到同一 bank 字的相邻 lane 字节），属于下一版优化；
//    本版优先正确性，不做这块。
// ============================================================================
`timescale 1ns/1ps

module conv_int8_uni (
    input  logic        ap_clk,
    input  logic        ap_rst_n,
    input  logic        ctrl_start,
    output logic        ctrl_done,
    output logic        ctrl_busy,
    input  logic [15:0] cfg_cin, cfg_cout, cfg_k, cfg_s, cfg_d, cfg_pad,
    input  logic [15:0] cfg_h, cfg_w,
    input  logic [7:0]  cfg_tap_stride,
    input  logic [5:0]  cfg_shift,
    input  logic        cfg_relu,
    input  logic [31:0] base_in, base_wgt, base_bias, base_out,
    output logic        mem_rd_en,
    output logic [31:0] mem_rd_addr,
    input  logic [63:0] mem_rd_data,
    output logic        mem_wr_en,
    output logic [31:0] mem_wr_addr,
    output logic [7:0]  mem_wr_data
);

    // ========================================================================
    // 0. 常量
    // ========================================================================
    // LANES = 32 固定为 4 个 oc × 8 个通道：8 个通道恰好是一个 C8 输入整字，
    // 4 个 oc 恰好是 4 个权重 bank —— 这是本引擎唯一"结构上"的取舍。
    localparam int LANES = 32;
    localparam int NBO   = 4;       // 每组的 oc 数（= bank 数 = 权重读口数）
    localparam int NCH   = 8;       // 每组的通道数（= 一个 C8 字的字节数）
    localparam int DEPTH = 512;     // 每个 bank 的条目数（512×72 模式的 RAMB36）

    localparam logic [2:0] ST_IDLE = 3'd0,
                           ST_BIA  = 3'd1,
                           ST_WLD  = 3'd2,
                           ST_CMP  = 3'd3,
                           ST_DRN  = 3'd4,
                           ST_DONE = 3'd5;

    // ========================================================================
    // 1. 组合函数
    // ========================================================================
    // ⚠ 这里原来有一个 16 位恢复余数除法器 div16u（16 级减法链，实测 43 级
    //   CARRY4，单条路径 57 ns）。它已经**没有任何调用点**（Hout/Wout 改成移位，
    //   见下面 §2），所以整段删掉，防止有人再把它接回 cfg 路径 ——
    //   它的结果一旦进 DSP 的乘法 B 口，150 MHz 直接 WNS = -51.5 ns。
    //   如果以后真要支持 s ∉ {1,2} 的除法，请写成流水线寄存器输出，不要放组合路径。

    // ------------------------------------------------------------------------
    // 收尾三级（2026-10-30：一拍拆三拍，见 §15）
    //
    // 旧版 sat_sh 把 "32 位加 + 32 位算术桶形(6 级 mux) + 3 次 32 位比较" 全部压在
    // **一拍**里，实测 s2sum_reg[0][1]/C → rb0_reg/D 共 **24 级**、slack −1.656 ns
    // （conv 侧最差、全芯片第二差）。现按"每拍只做一件事"拆成三拍：
    //     拍D  s3q_q[i] <= s2sum[i] + bias_q[i];      // ① 32 位加（1 级进位链）
    //     拍E  s4v_q[i] <= s3q_q[i] >>> shift_q;      // ② 32 位桶形（6 级 mux）
    //     拍F  sb*_c    = sat8f(s4v_q[i], relu_q);    // ③ relu + 上下饱和（组合）
    //
    // **逐位等价**：下面 sat8f 就是旧 sat_sh 去掉前两行（加、移位）之后的后半段，
    //   恒等式 sat_sh(sm,bi,sh,rl) ≡ sat8f((sm+bi) >>> sh, rl)。（旧 sat_sh 原文：
    //       q = sm + bi;  v = q >>> sh;
    //       if (rl && v < 0) v = 0;  if (v > 127) v = 127;  if (v < -128) v = -128;
    //       sat_sh = v[7:0]; ）
    // ⚠ 迟 1~2 拍再采 bias_q / shift_q / relu_q 与在拍C 采**完全等价**，因为它们在
    //   这段时间内是常量：shift_q/relu_q 在 ST_IDLE 一次锁存；bias_q 只在 ST_BIA
    //   改写，而换组必须先等 ST_DRN 把全部 valid 排空（§18）⇒ 不会有新组的值提前
    //   打进本组还在流水线上的结果。
    // ------------------------------------------------------------------------
    // ③ 饱和级：relu + 上下饱和。有符号 32 位，与 golden 的 sat8 一致。
    function automatic [7:0] sat8f(input logic signed [31:0] v,   // = (sum+bias)>>>shift
                                   input logic              rl);
        logic signed [31:0] u;
        begin
            u = v;
            if (rl && (u < 32'sd0)) u = 32'sd0;
            if (u > 32'sd127)  u = 32'sd127;
            if (u < -32'sd128) u = -32'sd128;
            sat8f = u[7:0];
        end
    endfunction

    // ========================================================================
    // 2. 启动时由 cfg_* 组合算出的派生常量（ST_IDLE 一次性锁存）
    // ========================================================================
    wire [15:0] s_safe  = (cfg_s == 16'd0) ? 16'd1 : cfg_s;
    // ⚠ **不要用 div16u**：组合 16 位除法器要走 16 次迭代减法（实测 43 级 CARRY4），
    //   而它的结果又直接进 DSP 的乘法 B 口 ⇒ 单条路径 57 ns，150 MHz（6.667 ns）
    //   下 WNS = -51.5 ns，整个设计跑不起来，上板还会因亚稳态乱写 DDR 把板子搞死。
    //   既然 s ∈ {1,2}（已由范围检查保证），除法就等价于移位：
    //       Hout = ((H-1) >> (s-1)) + 1
    wire [15:0] s_sh    = (s_safe == 16'd2) ? 16'd1 : 16'd0;
    wire [15:0] hout_c  = ((cfg_h - 16'd1) >> s_sh) + 16'd1;
    wire [15:0] wout_c  = ((cfg_w - 16'd1) >> s_sh) + 16'd1;

    wire [31:0] cin32_c = {16'd0, cfg_cin};
    // ★ 平凡乘法全部改移位/加法：旧版这 7 个 32 bit×32 bit 乘法被 Vivado 全收进
    //   DSP（"operator kk_c/dw8_c/sw8_c/padw8_c/hw8_c0/wgtblk_c0/p8_c0 is absorbed
    //   into DSP"），把本文件仅有的 DSP 预算吃干，而 32 条 lane MAC 反而没 DSP。
    //   k ≤ 5 ⇒ k² 用 4×4 位乘（~20 LUT，不会进 DSP）；s/d/pad/TAP 都是小常数，
    //   用移位/加法精确展开。
    (* use_dsp = "no" *) wire [7:0] kk_c = cfg_k[3:0] * cfg_k[3:0];     // k²（4×4，锁死不用 DSP）
    wire [31:0] ncg_c   = (cin32_c + 32'd7) >> 3;                       // ceil(cin/8)
    wire [31:0] nog_c   = ({16'd0, cfg_cout} + 32'd3) >> 2;             // ceil(cout/4)
    wire [31:0] w8_c    = {16'd0, cfg_w} << 3;                          // W*8
    wire [31:0] s8_c    = {16'd0, cfg_s} << 3;                          // s*8
    wire [31:0] d8_c    = {16'd0, cfg_d} << 3;                          // d*8
    // s ∈ {1,2}
    wire [31:0] sw8_c   = (cfg_s == 16'd2) ? (w8_c << 1) : w8_c;        // s*W*8
    // d ∈ {1,2,3}
    wire [31:0] dw8_c   = (cfg_d == 16'd1) ? w8_c :
                          (cfg_d == 16'd2) ? (w8_c << 1) : (w8_c + (w8_c << 1)); // d*W*8
    wire [31:0] pad8_c  = {16'd0, cfg_pad} << 3;                        // pad*8
    // pad ∈ [0,8]（conv_uni_axi_wrap 的范围检查）：按 pad 的低 4 bit 移位相加，
    // 对 0..15 都**精确**（不改语义），且不需要乘法器。
    wire [31:0] padw8_c = ({32{cfg_pad[0]}} &  w8_c)
                        + ({32{cfg_pad[1]}} & (w8_c << 1))
                        + ({32{cfg_pad[2]}} & (w8_c << 2))
                        + ({32{cfg_pad[3]}} & (w8_c << 3));             // pad*W*8
    // 一个 oc 组的权重容器块 = 4 个 oc × cin 行 × TAP 字节。
    //   ⚠ TAP 已从 {4,12,28} 改为 8·ceil(k²/8) = {8,16,32}（2026-10-30 时序优化）。
    //   于是 4·TAP = {32,64,128} = 2^5/2^6/2^7 ⇒ 三个分支全是**纯常数移位**（零加法器、
    //   零进位链），比旧式 `(cin<<5)+(cin<<4)` / `(cin<<7)-(cin<<4)` 各少一条 32 bit 加法链。
    //   注意别写成 cin·TAP（= <<3/<<4/<<5）——那漏了 4 个 oc 的因子，会让 wbase_q 每组
    //   只前进 1/4 块，第 2 组起读到上一组权重（逐字节全错且不报错）。
    wire [31:0] wgtblk_c = (cfg_tap_stride == 8'd8)  ? (cin32_c << 5) :
                           (cfg_tap_stride == 8'd16) ? (cin32_c << 6) :
                                                       (cin32_c << 7);
    // ---- §6b 的三个读地址步长（同样是移位相加，锁死不用 DSP）----
    //  (k-1)*d*8 / (k-1)*d*W*8：用 k-1 的低 3 bit 移位相加（对 k-1 ≤ 7，即 k ≤ 8 精确；
    //  实际用 k ≤ 5，k ≥ 9 早就超出 DEPTH=512 的条目预算，见 §4）。
    wire [15:0] km1_c    = cfg_k - 16'd1;                               // k-1 ∈ {0,2,4}
    wire [31:0] dx_c     = ({32{km1_c[0]}} &  d8_c)
                         + ({32{km1_c[1]}} & (d8_c << 1))
                         + ({32{km1_c[2]}} & (d8_c << 2));              // (k-1)*d*8
    wire [31:0] dy_c     = ({32{km1_c[0]}} &  dw8_c)
                         + ({32{km1_c[1]}} & (dw8_c << 1))
                         + ({32{km1_c[2]}} & (dw8_c << 2));             // (k-1)*d*W*8
    //  权重装载：每个通道要读 j0 = 0,8,... 共 ceil(k²/8) 组。
    //  ⚠ 恒等式（TAP' = 8n 时）：通道边界步长 TAP' - j0_last = 8n - 8(n-1) ≡ 8，
    //    对 k=1/3/5 恒成立（k=1: 8-0；k=3: 16-8；k=5: 32-24）。
    //    ⇒ 每一次权重读都恰好 +8，与 k、与 cfg_tap_stride 都无关。
    //  ★ P5：因此 wl_ngrp_c / wl_dlast_c 这两条 wire **已删除**（连同它们喂的
    //    三个寄存器 wl_ngrp_q/wl_dlast_q/wl_rem_q）。§9b 里直接写 `+ 32'd8`。
    //     旧注释保留如下，供以后回退到 TAP{4,12,28} 时参考：
    //       wl_ngrp_c  = (kk_c + 7) >> 3              // ceil(k²/8)
    //       wl_dlast_c = cfg_tap_stride - j0_last     // 旧 TAP 下恒为 4
    // 还剩两个"真"乘法：H*W*8 与 Hout*Wout*8。它们**不在这里**算 —— cfg_* 是
    // 模块输入端口，直接乘等于"输入端口 → DSP"，还会和上面这些常量抢 DSP。
    // 改到 ST_BIA 用已锁存的 h_q/w_q/hout_q/wout_q 算（输入是 FF，各 1 个 DSP，
    // BIA 有 4 拍空转可以掩盖；hw8/p8 最早也要 WLD/CMP/DRN 才用到）。

    // ========================================================================
    // 3. 锁存的配置 / 派生常量
    // ========================================================================
    logic [15:0] cin_q, cout_q, k_q, tap_q, h_q, w_q, hout_q, wout_q;
    logic [10:0] kk_q, ncg_q, nog_q;           // ≤ 25 / ≤ 24 / ≤ 16（见 §7 的范围）
    logic [5:0]  shift_q;
    logic        relu_q;
    logic [31:0] w8_q, s8_q, d8_q, sw8_q, dw8_q, pad8_q, padw8_q;
    logic [31:0] hw8_q, p8_q;                  // 在 ST_BIA 用锁存副本算（各 1 DSP）
    logic [31:0] wgtblk_q;
    logic signed [16:0] padS_q, sS_q, dS_q;    // pad / s / d（值域 ±256，17 bit 够）
    logic [31:0] in_b, wgt_b, bias_b, out_b;
    // ---- 地址步长常量（启动时算一次，此后只在 §9b 的 1 次加法里用）----
    logic [31:0] dx_q, dy_q;                   // (k-1)*d*8 / (k-1)*d*W*8
    logic [31:0] step_ky_q, step_cg_q;         // ky++ / cg++ 的读地址步长
    // ★ P5（零风险）：wl_ngrp_q / wl_dlast_q / wl_rem_q 三个寄存器**已删除**。
    //   因为 wl_dlast_c ≡ 8（TAP'=8n 的恒等式，见 §2），§9b 里 wl_step_c 的
    //   两个分支（wl_dlast_q / 32'd8）恒等于 8 ⇒ 步长写成常数 +8，
    //   这三个寄存器（16+32+16 = 64 FF + 一条 16 bit 比较 + 一条 16 bit 减法）
    //   变成纯死码。删掉它们不影响任何读地址（逐字节等价，见 §9b 注释）。

    // ---- ★ P0：6 个"末值"寄存器（150 MHz 时序，2026-10-30）----
    //   旧版 j_last/kx_last/ox_last/oy_last/cg_last/wl_done 全部写成
    //       (cnt == (xxx_q - 1))  ← 当拍现算 6 条减法器（11/11/16/16/16/16 bit）
    //   减法器的结果**只**给比较器用，等于在每条"计数器 → 比较 → 计数器使能"
    //   的回路上白插一级 CARRY 链。这些值在一次 ctrl_start 内是常量，
    //   所以和 kk_q/ncg_q 一起在 start_pulse 那一沿锁存，比较器直接对 FF。
    //   代价 ~86 FF，收益：6 条减法锥离开所有计数器的使能路径。
    logic [10:0] kk_m1_q, ncg_m1_q;
    logic [15:0] k_m1_q, wout_m1_q, hout_m1_q, cin_m1_q;

    // ========================================================================
    // 4. 状态 / 握手 / og 组指针
    // ========================================================================
    logic [2:0]  state;
    logic        start_q, done_q;
    logic [31:0] og_q;        // 当前 oc 组（每组 4 个 oc）
    logic [31:0] wbase_q;     // 本组权重块基址 = base_wgt + og*4*cin*TAP
    logic [31:0] bbase_q;     // 本组偏置基址   = base_bias + og*4*4
    logic [31:0] obase_q;     // 本组输出基址   = base_out + (og/2)*P8 + (og%2)*4
    logic [1:0]  bia_i;       // 偏置装载：字序号 0/1
    logic        bia_ph;      // 0 = 发读拍，1 = 收数拍（读数据下一拍才有效）
    logic [3:0]  dr_q;        // 排空计数

    wire start_pulse = ctrl_start & ~start_q;

    // ========================================================================
    // 4b. ★ P1（2026-10-30，150 MHz 关键路径，373 条违例路径的根因）
    //     "组初始化"从 **WLD 末拍** 挪到 **BIA 首拍**。
    //
    //   旧版:  wire cmp_init = (state == ST_WLD) && wl_done;
    //      wl_done 的 10 级锥（kk_q → wl_left 16bit 减 → wl_nb → wl_tend →
    //      wl_rowend → wl_c==cin_m1）实测到达时间 7.089 ns > 6.667 ns 周期，
    //      却被直接接到 13 个计数器的 D 端选择器 + 32 个 acc 的 RSTP +
    //      s2tprev + s2tprev valid ⇒ 综合成 1 个 LUT2、扇出 116。
    //      （★ P6：这条锥本身也在 §8a 被拆掉了 —— 16 bit 减法/比较换成了
    //        两个装载开始时锁存的比较对象，wl_done 现在只剩 3~4 级 LUT，
    //        所以它留在 rd_wld / cmp_init 上也不再是违例源。）
    //
    //   为什么可以挪：cmp_init 的唯一职责是"保证**第一个 CMP 拍**里计数器/acc/
    //   s2tprev 已就绪"。BIA 的 4 拍里**没有任何人读这些寄存器**：
    //     · CMP 之外唯一的读口 eaddr §12 被 `state == ST_CMP` 挡住；
    //     · 其余消费者（in_bounds/term_last/pix_last/oaddr）全部只在
    //       state==ST_CMP 的分支里被采样。
    //   而"每个组都必经 BIA"（IDLE→BIA、DRN→BIA，见 §18）⇒ 提前到 BIA 完全等价。
    //
    //   ⚠ 相位：cmp_load 是**寄存后**的脉冲，所以它在 BIA 的**第 2 拍（c1，收数拍）**
    //     为 1，各寄存器在 c1→c2 沿被初始化。c1 的 rd_bia = 0（不发读），
    //     而 c0 那次偏置读的数据在 c1 沿只写 bias_q —— 与 cmp_load 的消费者
    //     完全不重叠。到 CMP 首拍还有 c2/c3 + 全部 WLD 拍（≥ 8 拍）的余量。
    // ========================================================================
    logic cmp_load;
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) cmp_load <= 1'b0;
        // (bia_ph==0 && bia_i==0) 在一次 BIA 里**只出现一次**（c0，发第 1 次偏置读）：
        //   c0: ph=0,i=0 → 发读     c1: ph=1,i=0 → 收数, i→1
        //   c2: ph=0,i=1 → 发读     c3: ph=1,i=1 → 收数, i→0, → WLD
        else           cmp_load <= (state == ST_BIA) && (bia_ph == 1'b0) && (bia_i == 2'd0);
    end

    assign ctrl_busy = (state != ST_IDLE);
    assign ctrl_done = done_q;

    // 写队列非空 → 计算流水线整体冻结（声明提前：xvlog 要求先用后声明会报错）
    wire freeze;
    // S1 末拍标记 / S2 收尾流水（§14 使用，写队列 §10 提前引用）
    // ★ 归约树拆成两拍 + 收尾再拆两级（见 §14/§15），收尾流水现在是 **5 级**：
    //     s1_vld  → 拍A：acc 已是完整 T(n)，把 8 个"4 lane 半和"寄存进 smhalf
    //     s1b_vld → 拍B：半和已寄存，两两合并 + 减 s2tprev → s2sum
    //     s2_vld  → 拍C：s2sum 已寄存
    //     s3_vld  → 拍D：+bias 已寄存（原 sat_sh 的第①段）
    //     s4_vld  → 拍E：>>>shift 已寄存（原 sat_sh 的第②段）；
    //                    下一拍由拍F 的饱和（组合，sat8f）推进写队列
    logic        s1_vld;          // 末 term 的乘加已写进 acc（下一拍 acc 才是完整和）
    logic        s1b_vld;         // ★ 新增：smhalf 已寄存，等拍B 合并
    logic [31:0] s1addr;          // 拍A 用的输出地址（末拍锁存）
    logic [31:0] s1baddr;         // ★ 与 s1addr 同拍打进拍B：地址随 valid 一起多打一拍
    logic        s2_vld;
    logic signed [31:0] s2sum [0:3];
    logic signed [31:0] s2tprev [0:3];   // 上一像素的累积和（acc 连续累加，取差分）
    logic signed [31:0] smhalf [0:7];    // ★ 拍A：8 个半和（每 4 条 lane 一组）
    // ★ 2026-10-30 收尾再拆两级（拆 sat_sh 的 24 级，见 §1/§15）：
    //   这两个 valid 必须进 freeze（§10）和 ST_DRN 的排空判据（§18），
    //   否则队列快满时在途结果会被丢 / 排空提前切组。地址同样要跟着打两拍。
    logic        s3_vld;                 // 拍D：+bias 已寄存
    logic        s4_vld;                 // 拍E：>>>shift 已寄存（= 推写队列前最后一拍）
    logic signed [31:0] s3q_q [0:3];     // 拍D 的值 = s2sum + bias
    logic signed [31:0] s4v_q [0:3];     // 拍E 的值 = (s2sum + bias) >>> shift
    logic [31:0] s3addr, s4addr;         // 与 valid 逐拍对齐的输出地址（同 s1baddr 的做法）
    logic [31:0] s2addr;

    // ========================================================================
    // 5. 偏置（每 oc 组 4 个 int32 = 16 字节 = 2 个对齐 8 字节字）
    // ========================================================================
    logic signed [31:0] bias_q [0:3];

    // ========================================================================
    // 6. 权重 bank：4 个 512×64 的同步读 RAM（每 bank 对应一个 oc）
    //    bank b 的字 = 8 个连续通道（字节 c%8 = 通道 cg*8+c%8）
    //    条目 e = (c/8)*k² + j
    // ========================================================================
    (* ram_style = "block" *) logic [63:0] wbank0 [0:DEPTH-1];
    (* ram_style = "block" *) logic [63:0] wbank1 [0:DEPTH-1];
    (* ram_style = "block" *) logic [63:0] wbank2 [0:DEPTH-1];
    (* ram_style = "block" *) logic [63:0] wbank3 [0:DEPTH-1];

    logic [63:0] wqr [0:3];      // 同步读出的 4 个 bank 字（与输入数据同拍）

    // ---- 权重装载计数器（字节流，1 字节/拍） ----
    logic [1:0]  wl_b;           // 组内 oc（= bank 号）
    logic [15:0] wl_c;           // 通道 c
    logic [15:0] wl_j0;          // 本行内的 j 组起点（0/8/16/24）
    logic [3:0]  wl_t;           // 本次 8 字节读里的字节序号
    logic        wl_ph;          // 0 = 发第一次读，1 = 消费阶段
    logic [31:0] wl_rb;          // (b*cin + c)*TAP
    logic [31:0] wl_eo;          // (c/8)*k²

    // ========================================================================
    // 7. 计算主体计数器（每拍推进一个 term）
    //    位宽按**实际值域**收窄（都远小于旧版的 16/32 bit）：
    //      cg ≤ ceil(cin/8)-1 ≤ 23，j ≤ k²-1 ≤ 24，kx/ky ≤ 4 ⇒ 10 bit 足够；
    //      ox/oy ≤ 255 ⇒ 9 bit；iy/ix/ybase/xbase ∈ [-8, 256*s+d*(k-1)] ⇒ 17 bit 有符号。
    //    收窄只影响 FF / 进位链长度，比较与算术语义不变（全部非负或补码一致）。
    // ========================================================================
    logic [9:0]  cg_q;           // 8 通道组号 0..NCG-1
    logic [9:0]  j_q;            // tap 号 0..k²-1
    logic [9:0]  kx_q;           // ★ P5：ky_q 已删（无人读，见 §16）
    logic [8:0]  ox_q, oy_q;
    logic signed [16:0] iy_q, ix_q;          // 当前 term 的输入坐标（可越界）
    logic signed [16:0] ybase_q, xbase_q;    // oy*s-pad, ox*s-pad
    // ---- 读地址（§6b：全部是寄存器，mem_rd_addr 直连 rd_addr_q）----
    logic [31:0] rd_addr_q;                  // ★ 本次/下一次读的地址（0 级组合输出）
    logic [31:0] pix_base_q;                 // 当前像素首 term 地址（cg=ky=kx=0）
    logic [31:0] rowy_nxt_q;                 // 下一个 oy 行的行首 = in_b+rowbase+sw8
    // ★ 2026-10-30 时序：cm_pix_c 的两个 32 位加法各预存一个寄存器副本，
    //   把"ox_last → 32 位加法器 → rd_addr_q"这条 7.355 ns 的最差路径砍掉。
    //       pix_nx_q ≡ pix_base_q + s8_q        （ox++ 后的像素首 term 地址）
    //       row0_q   ≡ rowy_nxt_q - pad8_q      （oy++ 后的行首像素地址）
    //   不变式由 §16 的更新条件保证（与 pix_base_q / rowy_nxt_q 逐拍同步）。
    logic [31:0] pix_nx_q;
    logic [31:0] row0_q;
    // ★ 三个"相对推进"候选地址的预存副本（≡ rd_addr_q + 各自的步长）
    logic [31:0] kx_addr_q, ky_addr_q, cg_addr_q;
    // ★ P5：eoff_q（32 bit，= cg*k²）→ eaddr_q（9 bit 累加器，= cg*k² + j）。
    //   旧版 §12 每个 CMP 拍都要做 `eoff_q[15:0] + j_q` 的 16 bit 加法，
    //   结果直接当 4 个权重 bank 的读地址 ⇒ 加法器整个落在 BANK 地址脚上。
    //   改成累加器后这一级加法被 j 的增量(+1) 和 cg 回绕(+k²) 吸收到
    //   **一个 9 bit 寄存器的 D 端**，读地址变成 0 级组合。
    //   值域：max = NCG*k² - 1 ≤ 511（§4 的 DEPTH=512 约束）⇒ 9 bit 足够。
    logic [8:0]  eaddr_q;                    // 权重条目号 = cg*k² + j
    logic [31:0] oaddr_q;                    // 当前像素的输出字节地址

    // ★ P0：比较对象全部换成**寄存器**（旧版这里 6 条 wire 是当拍现算的减法器）。
    //   截位安全性（逐个核对值域，见 §7 的范围表）：
    //     kk_m1_q[9:0]  : kk ≤ 25      ⇒ kk-1 ≤ 24    ⊂ 10 bit（= j_q 位宽）
    //     ncg_m1_q[5:0] : ncg ≤ 24     ⇒ ncg-1 ≤ 23   ⊂ 6 bit
    //     k_m1_q[3:0]   : k ≤ 5        ⇒ k-1 ≤ 4      ⊂ 4 bit
    //     wout_m1_q[8:0]: Wout ≤ 256   ⇒ Wout-1 ≤ 255 ⊂ 9 bit（= ox_q 位宽）
    //     hout_m1_q[8:0]: Hout ≤ 256   ⇒ Hout-1 ≤ 255 ⊂ 9 bit（= oy_q 位宽）
    //   左操作数非负 ⇒ 右侧截位值零扩展回原宽度，与旧版全宽比较**逐位等价**。
    //   （ox/oy 这里取 9 bit 而不是 8 bit：与 ox_q/oy_q 等宽，综合代价完全相同，
    //     但不再依赖 "W ≤ 256" 这个假设，只依赖 "ox_q/oy_q 是 9 bit" 这个事实。）
    // ★ 2026-10-30 时序（第三刀）：5 个"末拍"标志的扇出很大（实测 j_last=77、
    //   term_last=117 个负载），单条网线被拉出 1.0~1.1 ns，而它们又直接当
    //   §9b 地址米克斯的选择信号 ⇒ 那条 −0.240 ns 的路是"比较 → j_last(1.06ns)
    //   → term_last(1.02ns) → mux → rd_addr_q[7]"。加 max_fanout 让综合器
    //   复制驱动逻辑（每个副本就近服务它的那撮负载），把这两条网线压短。
    //   ⚠ 这些是**纯综合属性**：逻辑逐位不变，仿真完全不受影响。
    (* max_fanout = 24 *) wire j_last   = (j_q  == kk_m1_q[9:0]);
    (* max_fanout = 24 *) wire cg_last  = (cg_q == ncg_m1_q[5:0]);
    (* max_fanout = 24 *) wire kx_last  = (kx_q == k_m1_q[3:0]);
    (* max_fanout = 24 *) wire ox_last  = (ox_q == wout_m1_q[8:0]);
    wire oy_last  = (oy_q == hout_m1_q[8:0]);
    (* max_fanout = 24 *) wire term_last = j_last && cg_last;   // 本像素最后一个 term
    wire pix_last  = term_last && ox_last && oy_last;   // 本组最后一个像素

    // 越界判定：必须按**有符号**比较（iy/ix 是 17 bit 补码，-1 不能当 64K）
    wire in_bounds = (iy_q >= 17'sd0) && (ix_q >= 17'sd0) &&
                     (iy_q < $signed({1'b0, h_q})) &&
                     (ix_q < $signed({1'b0, w_q}));

    // ---- S0 侧 8 bit 通道掩码（cbase = cg*8）----
    // 旧代码在 S1 里对 **32 条 lane** 各算一次 '(r1_cbase+gc) < cin'，现在只算
    // 一次 8 bit 掩码、随 r1_* 登记，4 个 oc 车道共用：
    //     ch_ok_c[gc] = (cg*8 + gc < cin)      ⇔  旧 u_ok（同一个谓词）
    // 用 "cin-cbase" 的余数生成掩码：(1<<rem)-1，rem>=8 时全 1。
    wire [15:0] cbase_c  = {3'd0, cg_q, 3'b000};                       // cg*8
    wire [15:0] ch_rem_c = ({16'd0, cin_q} > cbase_c)
                         ? ({16'd0, cin_q} - cbase_c) : 16'd0;         // cin - cg*8
    wire [7:0]  ch_ok_c  = (ch_rem_c >= 16'd8) ? 8'hFF
                                              : ((8'h01 << ch_rem_c[3:0]) - 8'h01);

    // ========================================================================
    // 8. 权重装载的两相时序（避免"每行一次气泡"：在本次最后一个字节那一拍
    //    就把下一次读发出去，于是数据在下一拍正好可用，1 字节/拍不停顿）
    // ========================================================================
    // 进入 ST_WLD 的第一拍只做计数器清零、不发读（下一拍 wl_ph==0 才发第一次读），
    // 这样 wl_* 的复位/初始化全部落在装载器那个 always_ff 里 —— 不会和 FSM 双驱动。
    // （声明放在这里是因为 rd_wld 要用 wl_enter，xvlog 不接受先用后声明。）
    logic [2:0] st_prev;
    wire wl_enter = (state == ST_WLD) && (st_prev != ST_WLD);

    // ------------------------------------------------------------------------
    // 8a. ★ P6（2026-10-30）：wl_done 的 10 级锥 → 3~4 级（只换比较对象）
    //
    //  旧版（实测 7.089 ns > 6.667 ns，是 conv 侧剩余违例的公共前缀）：
    //      wl_left   = kk_q - wl_j0                 // 16 bit 减法
    //      wl_nb     = min(wl_left, 8)              // 16 bit 比较 + 4 bit mux
    //      wl_tend   = wl_ph && (wl_t+1 == wl_nb)
    //      wl_jglast = (wl_j0 + 16'd8 >= kk_q)      // 又一次 16 bit 加法 + 比较
    //      wl_rowend = wl_tend && wl_jglast
    //      wl_done   = wl_rowend && (wl_c == cin_m1_q) && (wl_b == 2'd3)
    //  这条锥同时挂在 rd_wld（→ 包装层 u_ce → p_addr_reg/p_data_reg/cnt_rdreq_reg
    //  /rd_need_q_reg/p_valid_reg/FSM_onehot_wst_reg 的 CE）与 FSM 的转移条件上，
    //  所以它是"一个条件、90 条路径"。
    //
    //  关键观察：**一行的字节流只可能是 "8,8,…,8,rem"，rem ∈ [1,8]**
    //    j0 从 0 每次 +8；行的最后一组 j0_last = 8*floor((k²-1)/8)，
    //    该组有效字节数 rem = k² - j0_last（由 §2 的 TAP'=8·ceil(k²/8) 直接得到；
    //    也等价于 TAP-j0_last）。于是：
    //      (a) wl_j0 + 8 >= kk_q  ⟺  wl_j0 == j0_last
    //          证：j0 ∈ {0,8,…,j0_last}。j0 < j0_last ⇒ j0 ≤ j0_last-8
    //              ⇒ j0+8 ≤ j0_last < kk（因为 kk ≥ j0_last+1）；j0 = j0_last
    //              ⇒ j0+8 = j0_last+8 ≥ kk（因为 kk ≤ j0_last+8）。∎
    //      (b) min(kk_q - j0, 8) == rem ⟺ jglast
    //          证：jglast 时 kk-j0 = rem ∈ [1,8]；非 jglast 时
    //              kk-j0 ≥ kk-(j0_last-8) = rem+8 ≥ 9 ⇒ min 取 8。∎
    //  ⇒ 两个"比较对象"变成装载开始时锁存的常量，表达式一个字不改：
    //      wl_jglast = (wl_j0 == wl_j0last_q)      ← FF == FF（不再是加法+比较）
    //      wl_nb     = wl_jglast ? wl_lastnb_q : 8 ← 4 bit mux（不再是 16 bit 减法）
    //  这比"倒计数器"更稳：不需要 4*cin*k² 这个乘法（那是 1 个 DSP48 或 ~70 LUT），
    //  不需要新状态（无 load/first-decrement 相位问题、无 wl_enter 那拍的残留值），
    //  也不依赖"装载总拍数"这个与 TAP 方案耦合的恒等式；改的只是**操作数来源**，
    //  所以 wl_tend/wl_rowend/wl_done 的布尔表达式逐字保留。
    //
    //  代价：+2 个寄存器（16 + 4 FF；j0last 的高 5 bit 由 kk_q[10:8]=0 可证为 0）
    //        ＋ 一条 16 bit 比较 ＋ 一个 4 bit mux，
    //        16 bit 减法/比较/加法各少 2 条；wl_done 最深路径 ≈ 3~4 级 LUT
    //        （jglast 2 级 ⊂ LUT6 树 → rowend AND → 3 个操作数 AND）。
    //  ⚠ 前提与 P5 相同：j0 的推进步长恒为 +8（TAP 是 8 的倍数）。回退到旧
    //    TAP{4,12,28} 时 §9b 的 +8 与这里的 j0_last 都要一起改（见 §6b）。
    // ------------------------------------------------------------------------
    logic [15:0] wl_j0last_q;     // 本行最后一个 j0 组 = 8*floor((k²-1)/8)（wl_enter 锁存）
    logic [3:0]  wl_lastnb_q;     // 本行最后一组的有效字节数 = k²-j0_last ∈ [1,8]
    // ------------------------------------------------------------------------
    // 8a'. ★ P7（2026-10-30，最后一轮 WNS 收口）：把两个**边界判定**寄存一拍
    //
    //   P6 之后 wl_done 的表达式已经是 5 个 FF + AND，但实测 WNS 仍是
    //       wl_j0_reg[6] → wl_jglast(16 bit 等值) → wl_nb → wl_tend → wl_rowend
    //       → wl_done → rd_wld → mem_rd_en → rd_need_q_reg/D      （11 级，−1.834）
    //   这条锥的**头两级**就是 wl_jglast 的 16 bit 等值比较（LUT6+CARRY4 ≈ 1.3 ns）。
    //
    //   观察：wl_j0 只在「一组的最后一个字节」那一沿推进（+8 或回 0），组内恒定；
    //   同理 wl_c 只在「一行（rowend）结束」那一沿推进，行内恒定。所以
    //       wl_jglast(t) ≡ (wl_j0(t)   == wl_j0last_q)
    //       wl_clast (t) ≡ (wl_c(t)    == cin_m1_q)
    //   都可以用**寄存器**精确跟踪 —— 只要在"值会变"的那一沿装入**下一拍**的值：
    //     wl_jglast_q(t+1) = (wl_j0(t+1) == j0last)
    //                      = wl_rowend(t) ? (0 == j0last) : (j0+8 == j0last)
    //     wl_clast_q(t+1)  = (wl_c(t+1)  == cin_m1)          （wl_c 只在 rowend 变）
    //                      = (c==cin_m1) ? (0==cin_m1) : (c+1 == cin_m1)
    //   逐拍归纳可得 **wl_jglast_q(t) ≡ (wl_j0(t)==j0last)**、wl_clast_q 同理，
    //   所以 wl_nb/wl_tend/wl_rowend/wl_done 的布尔表达式**一个字不用改**，
    //   只是把操作数从"现算比较"换成"寄存器输出"，逐拍逐位等价。
    //
    //   ⚠ 相位核对（这是唯一有陷阱的地方，已逐拍推导 + 模型穷举，见报告）：
    //     wl_j0 在 wl_enter 沿清 0，同一沿 wl_j0last_q 装入 j0last_c ⇒ 进入拍
    //     之后 j0=0 与 j0last 同时生效，jglast 在**第一个消费拍之前**就正确；
    //     此后 j0 只在 tend 沿变，而 jglast 也在同一沿按 +8/回 0 更新 ⇒ 不存在
    //     "寄存慢一拍"的错位。wl_c/clast 同理（都在 rowend 沿）。
    //
    //   收益：把 WNS 锥的头 2 级（16 bit 等值）移出时序路径；
    //         wl_done 改成 1 个 LUT 直接从 wl_tend 取 ⇒ 再省 1 级。
    //   代价：+2 FF（另 +2 个"下一拍比较"的组合，只喂各自的 D，不进关键路径）。
    // ------------------------------------------------------------------------
    logic        wl_jglast_q;     // 寄存器版：本行最后一个 j0 组
    logic        wl_clast_q;      // 寄存器版：本行是最后一行（c == cin-1）

    // 两个常量由 kk_q 直接推（**只依赖 k**，与 TAP 无关），在 wl_enter 与
    // wl_j0/wl_t/wl_c 同一个沿、同一个复位窗口锁存 ⇒ 装载期间恒定。
    //   j0_last = 8*floor((k²-1)/8)    rem = ((k²-1) mod 8)+1 ∈ [1,8]
    wire [15:0] kk_q16      = {5'd0, kk_q};
    wire [15:0] wl_j0last_c = (kk_q16 - 16'd1) & 16'hFFF8;
    wire [3:0]  wl_lastnb_c = ((kk_q16 - 16'd1) & 16'd7) + 4'd1;

    wire        wl_jglast = wl_jglast_q;                            // 本行最后一个 j 组
    wire [3:0]  wl_nb     = wl_jglast ? wl_lastnb_q : 4'd8;            // 本次读的有效字节数
    wire        wl_tend   = wl_ph && ((wl_t + 4'd1) == wl_nb);      // 本次读的最后一个字节
    wire        wl_rowend = wl_tend && wl_jglast;                   // 本行（通道 c）结束
    // ★ 用寄存后的 clast，且**不从 wl_rowend 再串一级**（平铺给综合器最浅形式：
    //   wl_tend & (jglast & clast & b==3)，后 3 项全是 FF ⇒ wl_done 只比 wl_tend 晚 1 级）
    wire        wl_lastgrp = wl_jglast_q && wl_clast_q && (wl_b == 2'd3);  // 最后一行最后一组
    wire        wl_done   = wl_tend && wl_lastgrp;
    wire        wl_c_eq   = (wl_c == cin_m1_q);                     // 仅用于 c / clast 的推进
    // cmp_init：权重装载的最后一个字节那一拍（下一拍就进 CMP）。
    // ⚠ P1 之后它**只剩一个消费者**：§9b 的 rd_addr_q（首地址必须在第一个 CMP
    //   拍之前装好，所以它必须仍然挂在"最后一个 WLD 拍"上，不能挪）。
    //   其余全部消费者（§16 的 13 个计数器 / §13 的 32 个 acc / §14 的
    //   s1/s2 流水与 s2tprev）已全部改挂 cmp_load（见 §4b），
    //   于是这条 7.089 ns 的锥只剩 32 bit 的 rd_addr_q 装载扇出。
    wire        cmp_init = (state == ST_WLD) && wl_done;
    // ⚠ 下面 4 条 wire 已经**不驱动硬件**（§9b 的读地址改成累加器了），只用于
    //   §9b 末尾 translate_off 里的"逐拍地址等价性自检"。综合时整段剥离、零面积。
    wire [31:0] wl_rbn = wl_rowend ? (wl_rb + {16'd0, tap_q}) : wl_rb;
    wire [15:0] wl_j0n = wl_rowend ? 16'd0 : (wl_j0 + 16'd8);
    wire [31:0] wl_addr_cur = wbase_q + wl_rb + {16'd0, wl_j0};
    wire [31:0] wl_addr_nxt = wbase_q + wl_rbn + {16'd0, wl_j0n};

    // ========================================================================
    // 9. 主读口（唯一）：偏置 / 权重 / 输入 三选一，互斥
    // ========================================================================
    wire rd_bia = (state == ST_BIA) && (bia_ph == 1'b0);
    wire rd_wld = (state == ST_WLD) && !wl_enter &&
                  ((wl_ph == 1'b0) || (wl_tend && !wl_done));
    wire rd_cmp = (state == ST_CMP) && in_bounds && !freeze;

    assign mem_rd_en   = rd_bia | rd_wld | rd_cmp;

    // ========================================================================
    // 9b. 读地址：**寄存器输出**（§6b）
    //     mem_rd_addr = rd_addr_q  ⇒ 到包装层的 26 bit tag 比较是 0 级组合
    //     rd_addr_q 的 D 端每分支只有 **1 次 32 bit 加法**（步长全部预存成寄存器）
    //     ⚠ rd_addr_q 在 t-1 沿装的是 **t 拍那个 term** 的地址 ⇒ t 拍的组合使能
    //       （rd_bia/rd_wld/rd_cmp）与它指同一次读，相位不变。
    // ========================================================================
    // ---- WLD：每次读之间的地址步长 ----
    //   旧版:  wl_step_c = (wl_rem_q == 16'd1) ? wl_dlast_q : 32'd8;
    //   但 wl_dlast_q ≡ wl_dlast_c ≡ 8（TAP' = 8·ceil(k²/8) 的恒等式，见 §2：
    //   k=1: 8-0, k=3: 16-8, k=5: 32-24 全部 = 8）⇒ 两个分支恒等 ⇒ 常数 +8。
    //   ★ P5：于是 wl_rem_q / wl_dlast_q / wl_ngrp_q 三个寄存器（64 FF + 一条
    //     16 bit 比较 + 一条 16 bit 减法 + 一个 2:1 步长 mux）全是死码，已删除。
    //   语义不变："每次发读（每 8 个消费拍一次）+8"，跨通道时 +8 正好从
    //   j0_last 组跨到下一通道的 j0=0（因为 TAP - j0_last ≡ 8）。
    //   ⚠ 回退到旧 TAP{4,12,28} 时必须把 wl_step_c 与 wl_rem_q/wl_dlast_q 一起恢复。

    // ---- CMP：每个 term 的下一拍地址（四选一）----
    // ★ 2026-10-30 时序（第二刀）：三个"相对推进"的候选值**预存成寄存器**
    //   kx_addr_q / ky_addr_q / cg_addr_q ≡ rd_addr_q + {d8_q, step_ky_q, step_cg_q}。
    //   旧写法把 3 个 32 位加法器并联后接 4:1 mux，综合器会把米克斯**折进加法器
    //   的进位输入 LUT**（省面积），于是选择信号 j_last/kx_last（由 kk_m1_q 比较
    //   出来，网线扇出 77、单条 1.23 ns）直接串进了进位链首级：
    //       kk_m1_q → 比较 → j_last(1.23ns) → 加法器 6×CARRY4 → mux → rd_addr_q
    //   实测这就是 WNS −0.847（12 级 / 7.20 ns）。预存之后 rd_addr_q 的 D 端
    //   只剩**纯米克斯**；加法器搬到"寄存器→寄存器"的短路上（见 §9b 的更新）。
    wire [31:0] cm_pix_c = ox_last ? row0_q                        // ox 回绕 → oy++，x 回 pad 起点
                                   : pix_nx_q;                     // 像素 ox++：xbase += s
    wire [31:0] cm_nxt_c = term_last ? cm_pix_c :
                           j_last    ? cg_addr_q :
                           kx_last   ? ky_addr_q : kx_addr_q;

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            rd_addr_q <= 32'd0;
            kx_addr_q <= 32'd0; ky_addr_q <= 32'd0; cg_addr_q <= 32'd0;
        end else if (cmp_init) begin
            // 下一拍就是 ST_CMP 的第一个 term（cg=ky=kx=0），地址 = in_b - pad*W*8 - pad*8。
            // ★ 这里直接取 pix_base_q，不再重算那条 2 级 32 位减法：
            //   pix_base_q 在 cmp_load（BIA c1）被赋成**同一个表达式**，
            //   而 WLD 期间它只在 term_last 分支更新（WLD 不产生 term）⇒ 此处值恒等。
            //   于是 cmp_init / wl_done 对**数据通路**的消费者清零（只剩 FSM 那 3 个 FF），
            //   那条 7.089 ns 的 10 级锥彻底离开数据路径。
            rd_addr_q <= pix_base_q;
            // ★ 三个候选值在进 CMP 前一次性重新播种（它们只在 ST_CMP 里被消费，
            //   而 CMP 的入口唯一 = cmp_init ⇒ 其它状态装 rd_addr_q 时不必同步）。
            kx_addr_q <= pix_base_q + d8_q;
            ky_addr_q <= pix_base_q + step_ky_q;
            cg_addr_q <= pix_base_q + step_cg_q;
        end else if (state == ST_IDLE) begin
            if (start_pulse) rd_addr_q <= base_bias;            // 本组 BIA 首个读拍
        end else if (state == ST_BIA) begin
            // c0/c2 是发读拍（地址保持）；c1/c3 是收数拍，装下一次读的地址：
            //   c1 → bbase+8（c2 读）；c3 → bbase+16（下一组的 c0 读）
            if (bia_ph) rd_addr_q <= bbase_q + (bia_i ? 32'd16 : 32'd8);
        end else if (state == ST_WLD) begin
            if (wl_enter) begin
                // 装载器清零那一拍预置本组权重块的首地址（rb=0, j0=0）
                rd_addr_q <= wbase_q;
            end else if (rd_wld) begin
                // 只有真正的发读拍才推进（每 8 个消费拍一次），一次读 +8
                rd_addr_q <= rd_addr_q + 32'd8;
            end
        end else if (state == ST_CMP) begin
            // 与 §16 的计数器**同一条件**推进：freeze（写口反压）时一起冻结，
            // 所以解冻后重发的是同一个 term 的地址。
            if (!freeze && !pix_last) begin
                rd_addr_q <= cm_nxt_c;
                // ★ 与 rd_addr_q **同一条使能条件**推进（freeze/pix_last 时一起冻结），
                //   保证不变式 kx_addr_q ≡ rd_addr_q + d8_q 等逐拍成立。
                kx_addr_q <= cm_nxt_c + d8_q;
                ky_addr_q <= cm_nxt_c + step_ky_q;
                cg_addr_q <= cm_nxt_c + step_cg_q;
            end
        end else if (state == ST_DRN) begin
            // ★ 换 oc 组的回环：CMP 结束后 rd_addr_q 还停在上一个像素的末 term 上，
            //   而下一组 ST_BIA 的 c0 立刻要读 bbase(new)。DRN 里每拍无条件装
            //   bbase+16（bbase_q 只在 DRN→BIA 那一沿才 +16，所以这里装的正是新组
            //   的基址），这样不管 FSM 在 DRN 停几拍（dr_q 重试）都装得对。
            rd_addr_q <= bbase_q + 32'd16;
        end
    end

    assign mem_rd_addr = rd_addr_q;   // 0 级组合逻辑

// synthesis translate_off
    // ========================================================================
    // 9c. 读地址等价性自检（**只存在于仿真**；综合时整段被剥离，零面积/零延迟）
    //     用三个**独立于 §9b 实现**的表达式逐拍对比 rd_addr_q：
    //       bia : bbase + bia_i*8
    //       wld : wbase + wl_rb + wl_j0（旧的三级加法写法；tend 那拍取"下一次"的值）
    //       cmp : in_b + iy*W*8 + ix*8 + cg*H*W*8（由坐标/通道直接算，与增量无关）
    //     任何 off-by-one / 相位错位都会在这里打印 MISMATCH，而不是静默读错数据。
    //     注意：这里用 !== 以同时抓 X；只报前 8 次，避免刷屏。
    // ========================================================================
    wire [31:0] chk_bia = bbase_q + (bia_i ? 32'd8 : 32'd0);
    wire [31:0] chk_wld = wl_ph ? wl_addr_nxt : wl_addr_cur;
    wire signed [63:0] chk_cmp64 = $signed({32'd0, in_b})
                                 + $signed({54'd0, cg_q}) * $signed({32'd0, hw8_q})
                                 + $signed({{47{iy_q[16]}}, iy_q}) * $signed({48'd0, w_q}) * 64'sd8
                                 + $signed({{47{ix_q[16]}}, ix_q}) * 64'sd8;
    wire [31:0] chk_cmp = chk_cmp64[31:0];

    // ---- ★ P5 新增自检（sim only，零综合影响）----
    //   eaddr_q 是本次改动里唯一的"状态化"量：把旧版组合的 eoff_q[15:0]+j_q
    //   改成 9 bit 累加器。三条 tap 分支漏掉任何一处 +1，就会静默读错权重
    //   （不报错、只是数据全错）。这里用**独立的** cg*k² + j 表达式逐拍对账。
    wire [17:0] chk_eaddr = {8'd0, cg_q} * {10'd0, kk_q} + {8'd0, j_q};

    // ---- ★ P1 新增自检（sim only，零综合影响）----
    //   cmp_load 若对某个 oc 组漏发一拍，13 个计数器就会带着**上一组**的末值
    //   进 CMP（旧版在 WLD 末拍初始化，所以这种错在旧版不可能出现）。
    //   这里在"每一组进入 CMP 的第一拍"核对 7 个计数器的初值。
    wire cmp_enter = (state == ST_CMP) && (st_prev != ST_CMP);

    integer chk_n_bia, chk_n_wld, chk_n_cmp, chk_n_eaddr, chk_n_init;
    initial begin
        chk_n_bia = 0; chk_n_wld = 0; chk_n_cmp = 0;
        chk_n_eaddr = 0; chk_n_init = 0;
    end
    always @(posedge ap_clk) begin
        if (ap_rst_n) begin
            if (rd_bia && (rd_addr_q !== chk_bia) && (chk_n_bia < 8)) begin
                $display("[conv_int8_uni] BIA ADDR MISMATCH t=%0t got=%h ref=%h i=%0d",
                         $time, rd_addr_q, chk_bia, bia_i);
                chk_n_bia = chk_n_bia + 1;
            end
            if (rd_wld && (rd_addr_q !== chk_wld) && (chk_n_wld < 8)) begin
                $display("[conv_int8_uni] WLD ADDR MISMATCH t=%0t got=%h ref=%h b=%0d c=%0d j0=%0d ph=%0d",
                         $time, rd_addr_q, chk_wld, wl_b, wl_c, wl_j0, wl_ph);
                chk_n_wld = chk_n_wld + 1;
            end
            if ((state == ST_CMP) && !freeze && (rd_addr_q !== chk_cmp) && (chk_n_cmp < 8)) begin
                // ★ P5：ky_q 已删（无人读，只在这里出现过）⇒ 打印字段也去掉 ky=%0d。
                //   自检本身（ADDR MISMATCH）一个字都没动。
                $display("[conv_int8_uni] CMP ADDR MISMATCH t=%0t got=%h ref=%h cg=%0d j=%0d kx=%0d iy=%0d ix=%0d",
                         $time, rd_addr_q, chk_cmp, cg_q, j_q, kx_q, iy_q, ix_q);
                chk_n_cmp = chk_n_cmp + 1;
            end
            if ((state == ST_CMP) && !freeze && (eaddr_q !== chk_eaddr[8:0]) &&
                (chk_n_eaddr < 8)) begin
                $display("[conv_int8_uni] EADDR MISMATCH t=%0t got=%0d ref=%0d cg=%0d j=%0d kk=%0d",
                         $time, eaddr_q, chk_eaddr[8:0], cg_q, j_q, kk_q);
                chk_n_eaddr = chk_n_eaddr + 1;
            end
            if (cmp_enter && (chk_n_init < 8) &&
                ((cg_q !== 10'd0) || (j_q !== 10'd0) || (kx_q !== 10'd0) ||
                 (ox_q !== 9'd0) || (oy_q !== 9'd0) ||
                 (iy_q !== -padS_q) || (ix_q !== -padS_q))) begin
                $display("[conv_int8_uni] GROUP INIT MISMATCH t=%0t og=%0d cg=%0d j=%0d kx=%0d ox=%0d oy=%0d iy=%0d ix=%0d pad=%0d",
                         $time, og_q, cg_q, j_q, kx_q, ox_q, oy_q, iy_q, ix_q, padS_q);
                chk_n_init = chk_n_init + 1;
            end
        end
    end
// synthesis translate_on

    // ========================================================================
    // 10. 写回队列（4 字节/像素，8bit 写口逐拍吐出，与计算并行）
    // ========================================================================
    logic [7:0]  rb0, rb1, rb2, rb3;
    logic [31:0] raddr_q;
    logic [3:0]  rmask_q;       // 只写 o < cout 的字节（co8 补齐字节保持 0）
    logic [1:0]  rcnt_q;
    logic        rvld_q;

    // 写队列拥塞 → 计算流水线整体冻结。
    // 判据是"**下一拍就要推入**且队列还没排空"（(s4_vld|s3_vld|s2_vld|s1b_vld) && rvld_q），
    // 而不是整个排出期都冻：排出的 4 拍与后续像素的计算是重叠的，只有真的撞上才停。
    // 正常形状（每像素 >= 8 拍）永不触发，只有 cin<=7,k=1 这类每像素 1~2 拍的
    // 极小层才会在本接口处反压写口。
    // ★ 归约树拆成两拍后，s2_vld 之前多了一级 s1b_vld（半和已寄存、等拍B 合并）：
    //    s1b_vld=1 的那一拍之后紧跟就是 s2_vld=1，所以"下一拍要推入且队列没空"
    //    的判据要把 s1b_vld 一起算进来，否则会漏判一拍（那一拍放开 freeze 就等于
    //    让结果再多挤一级，写口反压时数据被后续像素覆盖）。
    // ★ 2026-10-30 收尾再拆两级（拍D/拍E，§15）后同理，而且**更必须**加进来：
    //    推入点从 s2_vld 移到了 s4_vld，在途结果从 1 个变成 3 个
    //    （s2sum 已算完 / 已加 bias / 已移位）。若只判 s2_vld，
    //    队列未空的那几拍里 s3_vld/s4_vld 仍在往前挤 ⇒ 第 2、3 个结果被
    //    后续像素覆盖（丢像素）。四条一起判是**保守但正确**的写法：
    //    最小正确集合其实是 {s4_vld}（因为推入本身要求 rvld_q==0），
    //    多判上游只是把 freeze 提前 1~2 拍、让在途结果停在原地，
    //    代价仅是极小层每像素多停 1~2 拍（那些层本来就被写口带宽卡住）。
    //    注：freeze 自身的延迟是安全的 —— s1b_vld..s4_vld 在 freeze 期间保持，
    //    而 rvld_q 不受 freeze 门控（4 拍必排空），所以不会死锁。
    //    （深度：rvld_q && (4 个 valid 相或) = 5 输入，仍合进 1 个 LUT6 ⇒ freeze
    //      的组合深度与旧版 3 输入时**相同**，不会加深 §9 的 mem_rd_en 锥。）
    assign freeze = rvld_q && (s4_vld || s3_vld || s2_vld || s1b_vld);

    assign mem_wr_en   = rvld_q && rmask_q[rcnt_q];
    assign mem_wr_addr = raddr_q + {30'd0, rcnt_q};
    assign mem_wr_data = (rcnt_q == 2'd0) ? rb0 :
                         (rcnt_q == 2'd1) ? rb1 :
                         (rcnt_q == 2'd2) ? rb2 : rb3;

    // 单一时钟块里既"排出"又"拍F 推入"，避免多驱动（push 只在 rvld_q==0 时发生）
    logic [7:0]  b0_c, b1_c, b2_c, b3_c;     // 拍F 饱和的组合结果（下面 §15 赋值）
    logic [3:0]  mask_c;

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            rvld_q  <= 1'b0;
            rcnt_q  <= 2'd0;
            raddr_q <= 32'd0;
            rmask_q <= 4'd0;
            rb0 <= 8'd0; rb1 <= 8'd0; rb2 <= 8'd0; rb3 <= 8'd0;
        end else begin
            // ---- 排出：每拍 1 个字节（rmask 为 0 的槽不发写，但照样占 1 拍）----
            if (rvld_q) begin
                if (rcnt_q == 2'd3) rvld_q <= 1'b0;
                else                rcnt_q <= rcnt_q + 2'd1;
            end
            // ---- 拍F 推入：freeze ⊇ rvld_q，所以推入时队列必为空
            //      （s4_vld = 移位已完成，组合饱和后正好落在 rb0..rb3 的 D 端）----
            if (s4_vld && !freeze) begin
                rb0     <= b0_c;
                rb1     <= b1_c;
                rb2     <= b2_c;
                rb3     <= b3_c;
                raddr_q <= s4addr;
                rmask_q <= mask_c;
                rcnt_q  <= 2'd0;
                rvld_q  <= 1'b1;
            end
        end
    end

    // ========================================================================
    // 11. 流水线寄存器
    // ========================================================================
    logic        r1_pend;        // r1 里有一个尚未被 S1a 消费的 term
    logic        r1_rd;          // 该 term 的输入是否在界内（否则乘 0）
    logic        r1_last;        // 该 term 是本像素的最后一个
    logic [7:0]  r1_ok;          // 该 term 的 8 个通道里哪些 < cin（补齐通道乘 0）
    logic [31:0] r1_oaddr;

    wire r1_load = (state == ST_CMP) && !freeze;   // S0 → S1a
    wire mac_fire = r1_pend && !freeze;            // S1a 生效（装载 DSP 输入寄存器）

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            r1_pend <= 1'b0;
            r1_rd   <= 1'b0;
            r1_last <= 1'b0;
            r1_ok   <= 8'd0;
            r1_oaddr <= 32'd0;
        end else if (!freeze) begin
            if (r1_load) begin
                r1_pend  <= 1'b1;
                r1_rd    <= in_bounds;
                r1_last  <= term_last;
                r1_ok    <= ch_ok_c;
                r1_oaddr <= oaddr_q;
            end else begin
                r1_pend  <= 1'b0;
            end
        end
    end

    // ========================================================================
    // 11b. ★ S1b：DSP 输入侧的寄存器（2026-10-30，150 MHz 的最后一项）
    //
    //   实测最差路径（合成，960 条同源）：
    //       SRC  cache_b_reg/CLKARDCLK          ← 包装层 BRAM 的输出寄存器
    //       DST  G_BANK[0].G_CH[4].acc_reg[4]/A[10]   ← DSP48E1 的 A 口
    //       LOGIC 2.355 ns（= BRAM Tcko + 1 级 rd_comb mux + 1 级 &{8{r1_rd}}）
    //       NET   1.579 ns（BRAM 在存储侧、DSP 在算术侧，横跨大半个 die）
    //   ⇒ 这一锥里**我们自己的组合逻辑只有 2 级**，其余全是不可压缩的
    //     BRAM Tcko 和长线。继续拆逻辑（P1/P5/P6/P7 那一套）在这里已经没有
    //     东西可拆了 ⇒ 唯一的办法是**把这一条路径切成两条**。
    //
    //   做法：在乘法器 A/B 口之前插一级寄存器（每车道 2×8 bit = 512 FF）。
    //   四个写法约束（缺一个就不是"可摆位的流水级"，而是白加延迟）：
    //     ① **单级、无复位**：DSP48E1 的 A/B 寄存器没有 reset；而且这里
    //        也不需要复位 —— 它只在 mac_fire 拍被写，而 mac_fire ⇒ 上一次
    //        读已经返回有效数据。带 negedge ap_rst_n 会白白多一条复位网络。
    //     ② **无组合反馈**：Q 只进乘法器，不进任何别的逻辑（见 §13），
    //        于是它就是纯粹的流水边界，placer 可以自由摆位。
    //     ③ **使能明确**：写使能 = mac_fire（S1a 那一拍），冻结期间自动保持
    //        （mac_fire 已含 !freeze），与 r1_pend 同拍进退。
    //     ④ ★ **门控（r1_rd / r1_ok）放在寄存器之后**，这一条是刻意的：
    //        如果把 `& {8{r1_rd}}` 写在寄存器之前（`xr_q <= xb`），Vivado 会
    //        把这 512 个 FF 吸收成 DSP 的 AREG/BREG（免费、零 fabric FF），
    //        但寄存器就被**钉死在 DSP 那一端** —— BRAM→DSP 的 1.579 ns 长线
    //        一个字节都没被切开，恰好在我们要治的地方无效（甚至更糟：以为
    //        "已经加了寄存器"而实际路径长度不变）。
    //        门控放在寄存器之后，A/B 口前面就多了一级 LUT，AREG/BREG 无法
    //        吸收（AREG 要求 A 口直接来自寄存器输出），于是这一级**必然**落在
    //        fabric 里、可被摆到长线中段 ⇒ 长线被切成两段 ~0.8 ns。
    //        代价：+9 FF（r1b_rd 1 + r1b_ok 8，跟着 valid 一起打一拍）。
    //        ⚠ 这不是风格问题：把门控挪回寄存器之前 ⇒ 时序收益消失。
    //
    //   与既有同类拆分同构（valid / last / 地址"三件套"一起打拍）：
    //     · S2 归约：s1_vld → s1b_vld，s1addr → s1baddr（§14）
    //     · 收尾：  s3_vld/s3addr → s4_vld/s4addr（§15）
    //   本级的产物同样是三件套：r1b_vld / r1b_last / r1b_oaddr + 两个掩码。
    //
    //   ⚠ 相位（与 §11 的 S1a 差一拍，别混）：
    //       t   : S0 发 term(t)（rd_addr_q = term(t) 的地址，rd_cmp 发读）
    //       t+1 : 数据/权重到齐 ⇒ **S1a**（r1_* 有效）
    //             · mac_fire=1：xr_q/wr_q 在 t+2 沿锁存 term(t)
    //       t+2 : **S1b**（r1b_* 有效，xr_q/wr_q = term(t)）
    //             · mac_fire_b=1：acc 在 t+3 沿加上 p(t)
    //     见 §14 的逐拍表（那里整体比旧版晚 1 拍）。
    // ========================================================================
    logic        r1b_vld;        // S1b 有效（= mac_fire 打一拍）
    logic        r1b_last;       // 该 term 是本像素最后一个（r1_last 打一拍）
    logic        r1b_rd;         // 输入是否在界内（r1_rd 打一拍）
    logic [7:0]  r1b_ok;         // 补齐通道掩码（r1_ok 打一拍）
    logic [31:0] r1b_oaddr;      // 输出地址（r1_oaddr 打一拍）
    wire mac_fire_b = r1b_vld && !freeze;      // S1b 生效 = 真正的乘加那一拍

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            r1b_vld   <= 1'b0;
            r1b_last  <= 1'b0;
            r1b_rd    <= 1'b0;
            r1b_ok    <= 8'd0;
            r1b_oaddr <= 32'd0;
        end else if (!freeze) begin
            // ★ 冻结时**整体保持**（与 §14/§15 各级 valid 同一个门控）。
            //   mac_fire 在冻结期间是 0，所以这里装进去的就是"没有新 term"；
            //   而在途的那个 term 停在 r1b_vld=1 上不动 —— 解冻后它正好再走
            //   一拍进 acc。为什么不会重复累加，见下面 §11c 的逐拍推演。
            r1b_vld   <= mac_fire;
            r1b_last  <= r1_last;
            r1b_rd    <= r1_rd;
            r1b_ok    <= r1_ok;
            r1b_oaddr <= r1_oaddr;
        end
    end

    // ========================================================================
    // 11c. ★ 冻结（freeze）与"在途数据"的竞争 —— 加这一级**唯一**的风险点
    //
    //   freeze = rvld_q && (s4_vld|s3_vld|s2_vld|s1b_vld)（§10），语义是：
    //   写队列还没排空、流水里已经有结果要推 ⇒ 整条流水**原地不动**（各级
    //   valid 与数据寄存器全部关在 `else if (!freeze)` 里）。
    //
    //   加 S1b 之后，冻结那一拍 DSP 前面**可能压着一个在途 term**（就在 xr_q
    //   里）。它必须同时满足：(a) 冻结期间不被覆盖，(b) 解冻后**只累加一次**。
    //
    //   ---- 相位约定（全节统一，与 §11b 末尾一致）----
    //     拍 c：S1a 里是 term(c-1)（总线 bus(c) 就是它的数据），
    //           S1b 里是 term(c-2)（xr_q(c) = bus(c-1)），r1b_* 也是 term(c-2)；
    //           acc 在 (c+1) 沿加上 p(term(c-2))，条件 = r1b_vld(c) && !freeze(c)。
    //
    //   ---- 逐拍推演（f = 第一个冻结拍，g = 第一个解冻拍）----
    //     t=f-1（未冻）：r1_pend=1 → S1a = term(f-2)；r1b_vld=1 → S1b = term(f-3)
    //                    mac_fire   = 1 ⇒ f 沿 xr_q ← term(f-2)（f-1 拍总线上是它）
    //                    mac_fire_b = 1 ⇒ f 沿 acc += p(term(f-3))
    //                    ⚠ 同一个沿：一个读 xr_q 的旧值、一个写 xr_q（非阻塞）
    //                      ⇒ 互不干扰，顺序天然正确
    //     t=f （冻结） ：mac_fire   = 0 ⇒ xr_q/wr_q **保持** term(f-2)，不被覆盖
    //                    r1b_vld 的块整块不执行 ⇒ **保持 1**
    //                    mac_fire_b = 0 ⇒ acc **不动**
    //                    ⇒ term(f-2) 原封不动卡在 S1b：既没进 acc、也没被冲掉
    //     t=f+1..g-1   ：同上，全部原地站住（r1_pend 也保持 1，S1a = term(f-1)）
    //     t=g （解冻） ：mac_fire_b = r1b_vld(g) && !freeze = 1
    //                    ⇒ g+1 沿 acc += p(term(f-2))，**就这一次**
    //                    mac_fire = 1 ⇒ g+1 沿 xr_q ← bus(g) = data(term(f-1))
    //                    （冻结前最后发的那一读，包装层冻着、没有丢）
    //                    r1b_vld ← 1 ⇒ 下一拍接着累加 term(f-1)
    //     ⇒ 冻结期间"标志不前进、数据不消费"；解冻后每个未冻沿恰好消费一份、
    //       并装上下一份。**不存在重复累加，也不存在丢失。**
    //
    //   ---- 关键写法：acc 的使能必须带 !freeze ----
    //     写成 mac_fire_b（= r1b_vld && !freeze）就自带。若图省事写成
    //     `else if (r1b_vld)`，冻结的**每一个**周期都会再累加一次 p(term f-2)：
    //     一次冻结多算 2~4 个 term，而且只在小层（写口反压）出现，很容易漏测
    //     —— 这是本任务最隐蔽的一个 bug，已规避。
    //
    //   ---- freeze 的**判据本身**要不要扩展（把 r1b_vld 也算进去）？----
    //     不需要。判据与"保持"是两件事：
    //       · 判据只回答"现在有没有结果推不出去" ⇒ 看推入点 s4_vld（以及为了
    //         提前 1~2 拍停住而保守多算的 s3/s2/s1b/s1），它们**全在 S1b 下游**；
    //       · r1b_vld 在 s1_vld 的上游，它=1 而 s1_vld=0 的那一拍意味着"结果还
    //         没成形、离推入点还有 5 拍"，此时怎么走都不会丢数；
    //       · "冻结"是靠各级的 !freeze 使能实现的，r1b_vld 已被同一个 !freeze
    //         罩住 ⇒ 判据里不列它，它也照样原地不动。
    //     若把 r1b_vld 加进判据：正确性不变，但极小层每像素多停 1 拍，无收益。
    //
    //   ---- ⚠ 解冻边界上"读数据总线"的行为：与旧版逐拍同构，未改 ----
    //     冻结期间包装层的数据寄存器 CE=0（rd_live=0，见 conv_uni_axi_wrap
    //     §5.3b 的注释）⇒ bus 保持 f(A_{t-1})；rd_addr_q 也冻着不推进 ⇒ 解冻
    //     那一拍重发的是**同一个地址**，bus 不变。于是 S1a 在解冻后会连续两拍
    //     看到同一个 term 的 (data, flags) 对 —— 这一点**旧版就存在**（旧版是
    //     mac_fire 在解冻拍及下一拍各消费一次），本改动只是把它整体平移 1 拍，
    //     **没有引入任何新行为**。附注：该现象值得一次定向仿真（cin≤7,k=1 且
    //     强制反压）确认，见交付报告"不确定的地方"一节。
    // ========================================================================

    // ========================================================================
    // 12. 权重 bank 同步读：地址与输入读同一拍给（下一拍 S1a 用）
    // ========================================================================
    // ★ P5：条目号 eaddr_q 是**寄存器**（9 bit 累加器，见 §3/§16）。
    //   旧版这里写着 `wire [15:0] eaddr_c = eoff_q[15:0] + j_q;` —— 一条 16 bit
    //   加法器直连 4 个 bank 的地址脚，每个 CMP 拍都要走一遍。现在 0 级组合。
    always_ff @(posedge ap_clk) begin
        if (state == ST_CMP && !freeze) begin
            wqr[0] <= wbank0[eaddr_q];
            wqr[1] <= wbank1[eaddr_q];
            wqr[2] <= wbank2[eaddr_q];
            wqr[3] <= wbank3[eaddr_q];
        end
    end


    // ========================================================================
    // 13. S1b：32 个 8bit×8bit 乘加（4 bank × 8 通道 = 32 lane）
    //     ★ 三层门（r1b_rd / 补齐通道 / 像素边界）全部搬到乘法器**输入侧**和
    //       累加器**同步清零**，RTL 结构就是 UG901 的 "P <= A*B + P" + RSTP：
    //         · u_x = xr_q & {8{r1b_rd}}             （越界 ⇒ 0）
    //         · u_w = wr_q & {8{r1b_ok[gc]}}         （补齐通道 ⇒ 0）
    //         · acc <= 0                当 cmp_load（每个 oc 组开头，同步复位 = RSTP）
    //         · acc <= acc + u_x*u_w    其余（加在 DSP 内部 P 回环里）
    //     ★ 2026-10-30（§11b）：u_x/u_w 现在来自 **xr_q/wr_q**（打了一拍），
    //       门控因此用 r1b_rd/r1b_ok（同拍打一拍的副本）。乘法器与 P 回环的
    //       形状一个字没改 ⇒ DSP 推断结构不变。
    //       ⇒ 每条 lane 1 个 DSP48E1（旧版：u_p 清零 mux + r1_t0 条件装载 mux +
    //         异步复位，Vivado 直接放弃推断，32 条 8×8 乘法全在 LUT 里）。
    //
    //     ★ 为什么像素边界**不**清零 acc（与最初方案不同的地方，很关键）：
    //       DSP48E1 的乘法结果 M 不是端口，只有"寄存器化的后加器输出 P"能出芯片。
    //       如果像素和取自组合的 acc+u_p，乘积 u_p 就多了一个 LUT 扇出（加法树），
    //       Vivado 只能把乘法留在 LUT 里（或在 LUT/DSP 里各做一份）⇒ MAC 又进不了 DSP。
    //       所以求和必须取**已寄存的 acc**；而 acc 一旦在末拍清零，下一拍读到的就是 0。
    //       ⇒ 改成"acc 在整个 oc 组内**连续累加**、不清零"，像素和用**差分**得到：
    //            T(n) = Σ_lane acc_lane   （第 n 个像素末拍之后的累积和）
    //            sum(n) = T(n) - T(n-1)   （n-1 的 T 由 s2tprev 存着，见 §14）
    //          32 bit 模加下差分是**精确**的：每个像素和都落在 int32 内，减法不会溢出。
    // 逐位等价：整数加法在 mod 2^32 下可结合/可交换 ⇒ 差分结果与旧版逐位相同；
    //       旧版"首拍清零 + 累加"得到的正是同一个整数（见 §14 的逐拍论证）。
    // ========================================================================
    logic signed [31:0] acc [0:LANES-1];

    genvar gb, gc;
    generate
        for (gb = 0; gb < NBO; gb = gb + 1) begin : G_BANK
            for (gc = 0; gc < NCH; gc = gc + 1) begin : G_CH
                // ★★ 2026-10-30 新增的一级（§11b）：DSP 的 A/B 输入寄存器。
                //   取的输入是**未门控**的原始字节；r1_rd / r1_ok 的门控移到
                //   寄存器**之后**（理由见 §11b ④：门控在前 ⇒ 被吸收成 AREG，
                //   寄存器钉死在 DSP 端，长线切不开，白加一级）。
                //   ⚠ 不能有异步复位：DSP48E1 的 A/B 寄存器没有 reset，而且这一级
                //     也不在复位必须覆盖的集合里（只在 mac_fire 拍写，mac_fire ⇒
                //     上一次读已经返回有效数据；acc 本身由 cmp_load 同步清零）。
                logic [7:0] xr_q, wr_q;
                always_ff @(posedge ap_clk) begin
                    if (mac_fire) begin
                        xr_q <= mem_rd_data[8*gc +: 8];
                        wr_q <= wqr[gb][8*gc +: 8];
                    end
                end

                wire [7:0] xb = xr_q & {8{r1b_rd}};
                wire [7:0] wb = wr_q & {8{r1b_ok[gc]}};
                wire signed [7:0] u_x = $signed(xb);
                wire signed [7:0] u_w = $signed(wb);
                // 无异步复位（DSP48E1 只有同步复位）：只有 cmp_load 一个同步清零
                // ★ P1：清零源由 cmp_init（7.089 ns 的 WLD 末拍锥）改成 cmp_load
                //   （BIA 第 2 拍的寄存脉冲）⇒ 32 个 DSP 的 RSTP 由 FF 直驱。
                // ★ 2026-10-30：累加使能由 mac_fire 改成 mac_fire_b（S1b，晚一拍）。
                //   ⚠ **if/else-if 的层级一个字都不能动**：`if (cmp_load) ... else if
                //   (mac_fire_b)` 就是 UG901 的 "P <= P + A*B，RSTP=cmp_load" 模板；
                //   把 cmp_load 挪进 else 分支、或改成异步复位，32 个 DSP 立刻退回
                //   LUT 乘法器。mac_fire_b 只出现在 CE 位置，不改变 P 回环的形状。
                always_ff @(posedge ap_clk) begin
                    if (cmp_load) begin
                        acc[gb*NCH + gc] <= 32'sd0;
                    end else if (mac_fire_b) begin
                        acc[gb*NCH + gc] <= acc[gb*NCH + gc] + (u_x * u_w);
                    end
                end
            end
        end
    endgenerate

    // 每个 oc 的 8 路车道求和**拆成两拍**（拍A 的半和已在 §14 拍A 寄存进 smhalf）：
    //   拍A（s1_vld 拍）：smhalf[k] <= 4 条 lane 的和，写成 2 项加法 → 2 级
    //   拍B（s1b_vld 拍）：sm_i = smhalf[2i] + smhalf[2i+1] → 1 级
    // 两项取的都是**已寄存**的值 ⇒ 乘法器输出只喂 DSP，没有 LUT 组合扇出；
    // 组合路径从 "acc → 3 级加 → 1 级减 → s2sum" 降到 "smhalf → 1 级加 → 1 级减"。
    wire signed [31:0] sm0 = smhalf[0] + smhalf[1];
    wire signed [31:0] sm1 = smhalf[2] + smhalf[3];
    wire signed [31:0] sm2 = smhalf[4] + smhalf[5];
    wire signed [31:0] sm3 = smhalf[6] + smhalf[7];

    // ========================================================================
    // 14. S2：本像素最后一个 term 的乘加完成后，锁存 4 个 oc 的像素和
    //
    //  ★ 归约树拆成两拍（原来是 "8 项连加 + 1 级差分" = 4 级 32 bit 进位链，
    //    150 MHz 下 WNS = −4.734 ns，全芯片最差路径）：
    //      拍A：smhalf[k] = (acc[a]+acc[a+1]) + (acc[a+2]+acc[a+3])   → 2 级
    //      拍B：sm_i = smhalf[2i] + smhalf[2i+1]  →  −s2tprev[i] → s2sum → 2 级
    //    组合路径由 4 级降到 2 级；求和顺序改变**不影响数值**（整数 mod 2^32
    //    加法可结合，且 s2sum 取的是差分 T(n)−T(n−1)），与旧版逐位相同。
    //
    //  逐拍论证（求和项、求和顺序、数值都不变，只多延迟；★ 2026-10-30 又整体
    //  往后挪 1 拍 —— §11b 在 S1 与乘法器之间插了 DSP 输入寄存器）：
    //    t 拍      : S0 给出 term(t)（本像素最后一个），r1_* 在 t+1 沿登记；
    //    t+1 拍    : mac_fire 把 term(t) 的数据装进 xr_q/wr_q（**t+2 沿**）；
    //    t+2 拍    : mac_fire_b 消费 xr_q/wr_q，**t+3 沿** acc 写入
    //                「累积到本像素」的 T(n)；同一沿 r1b_vld/r1b_last 登记；
    //    t+3 拍    : s1_vld=1，acc 就是 T(n) ⇒ **t+4 沿**把 8 个半和写进 smhalf；
    //    t+4 拍    : s1b_vld=1，sm0..sm3 = 各 oc 的 T(n)，
    //                s2sum <= T(n) - s2tprev(n-1) = 本像素和；s2tprev <= T(n)；
    //    t+5 拍    : s2_vld=1，s2sum 已寄存（收尾三级在 §15 展开）：
    //                **t+5 沿** s3q_q <= s2sum + bias
    //    t+6 拍    : s3_vld=1，**t+6 沿** s4v_q <= s3q_q >>> shift
    //    t+7 拍    : s4_vld=1，拍F 组合饱和 → **t+7 沿**推进写队列。
    //  ⇒ 旧版的 accn = acc+u_p 与这里的 T(n)-T(n-1) 是**同一个整数**：
    //     旧版每个像素首拍把 acc 置为 p0（清零），末拍 accn = Σ 本像素全部 p；
    //     T(n) 是同一组 p 的累加，减去 T(n-1) 后剩下的正是这些 p。
    //     模 2^32 加法差值与加法顺序无关，所以逐位相同。
    //    · 单 term 像素（k=1,cin≤8）：每个 term 都是末拍，T(n)-T(n-1)=该 term 的 p；
    //        s1_vld / s1b_vld / s2_vld / s3_vld / s4_vld 会连续每拍为 1，
    //        流水线仍 1 结果/拍（该层本来就受 4 字节写口带宽限制）；
    //    · 首个像素：cmp_init 把 acc 与 s2tprev 同时清 0 ⇒ sum = T(0) = 本像素全和。
    //  多出的 1 拍（拍A/拍B 拆分）、2 拍（拍D/拍E，§15）与 1 拍（S1a/S1b，§11b）
    //  都由 ST_DRN 的固定排空吸收；核算见 §18 的排空判据（余量仍 1 拍）。
    // ========================================================================
    // ---- 拍A：8 个半和（每 2 项加法 = 2 级），顺手把 valid 链推一格 ----
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            s1_vld  <= 1'b0;
            s1b_vld <= 1'b0;
            s1addr  <= 32'd0;
            s1baddr <= 32'd0;
            for (int i = 0; i < 8; i = i + 1) smhalf[i] <= 32'sd0;
        end else if (cmp_load) begin
            // 换组：上一组的收尾流水已被 ST_DRN 排空，这里只是把半和与拍A/B 的
            // valid 一起清干净（s2sum 基准 s2tprev 由 §14 拍B 的块清）
            // ★ P1：改挂 cmp_load 后这三条清零**在功能上是空操作**
            //   （进 BIA 之前 ST_DRN 的排空判据已保证 s1_vld/s1b_vld 均为 0，
            //    见 §18；smhalf 只在 s1_vld 拍被读）。保留是为了不改变复位语义，
            //   同时把 cmp_init 的扇出从这里挪走（原来这 3 条 = 258 FF 的负载）。
            s1_vld  <= 1'b0;
            s1b_vld <= 1'b0;
            for (int i = 0; i < 8; i = i + 1) smhalf[i] <= 32'sd0;
        end else if (!freeze) begin
            // ★ 2026-10-30（§11b 加了 S1b 之后）：拍A 的触发条件由 S1a 的
            //   `mac_fire && r1_last` 换成 S1b 的 `mac_fire_b && r1b_last`。
            //   必须换 —— acc 现在晚一拍才完整（term(t) 在 t+3 沿才进 acc），
            //   而 smhalf 取的是**已寄存的 acc**：s1_vld 早一拍就会采到
            //   T(n)-p(t)（漏掉本像素最后一个 term）。
            //   r1b_last / r1b_oaddr 与 r1b_vld 同拍（都在 §11b 里跟着 !freeze
            //   打一拍），所以"valid / last / 地址"三件套仍然严格对齐。
            s1_vld  <= mac_fire_b && r1b_last;
            s1b_vld <= s1_vld;                       // ★ 拍A → 拍B 的 valid
            if (mac_fire_b && r1b_last) s1addr <= r1b_oaddr;
            // ★ 地址也要跟着 valid 多打一拍：拍B 采样时 s1addr 可能已被
            //   **下一个像素**的末拍改写（单 term 像素时是背靠背的 1 拍 1 个）。
            if (s1_vld) s1baddr <= s1addr;
            if (s1_vld) begin
                // 写成 2 项加法（不是 4 项连加），综合才是 2 级进位链
                smhalf[0] <= (acc[0]  + acc[1])  + (acc[2]  + acc[3]);
                smhalf[1] <= (acc[4]  + acc[5])  + (acc[6]  + acc[7]);
                smhalf[2] <= (acc[8]  + acc[9])  + (acc[10] + acc[11]);
                smhalf[3] <= (acc[12] + acc[13]) + (acc[14] + acc[15]);
                smhalf[4] <= (acc[16] + acc[17]) + (acc[18] + acc[19]);
                smhalf[5] <= (acc[20] + acc[21]) + (acc[22] + acc[23]);
                smhalf[6] <= (acc[24] + acc[25]) + (acc[26] + acc[27]);
                smhalf[7] <= (acc[28] + acc[29]) + (acc[30] + acc[31]);
            end
        end
    end

    // ---- 拍B：两两合并 → 减 s2tprev → s2sum ----
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            s2_vld  <= 1'b0;
            s2addr  <= 32'd0;
            for (int i = 0; i < 4; i = i + 1) begin
                s2sum[i]   <= 32'sd0;
                s2tprev[i] <= 32'sd0;
            end
        end else if (cmp_load) begin
            // acc 被同步清零 ⇒ 差分基准同时清零（本组第一个像素 T(-1) = 0）
            // ★ P1：跟着 acc 一起改挂 cmp_load（必须与 acc 的清零**同沿**，
            //   否则差分基准会比 acc 晚/早一拍，首像素的和就错）。
            s2_vld <= 1'b0;
            for (int i = 0; i < 4; i = i + 1) s2tprev[i] <= 32'sd0;
        end else if (!freeze) begin
            s2_vld <= s1b_vld;                       // ★ 拍B → 拍C 的 valid
            if (s1b_vld) begin
                s2sum[0] <= sm0 - s2tprev[0];
                s2sum[1] <= sm1 - s2tprev[1];
                s2sum[2] <= sm2 - s2tprev[2];
                s2sum[3] <= sm3 - s2tprev[3];
                s2tprev[0] <= sm0;
                s2tprev[1] <= sm1;
                s2tprev[2] <= sm2;
                s2tprev[3] <= sm3;
                s2addr   <= s1baddr;  // ★ 用随 valid 打了一拍的副本
            end
        end
    end

    // ========================================================================
    // 15. 收尾三级：拍D(+bias) / 拍E(>>>shift) / 拍F(relu+饱和) → 4 个 int8
    //
    //  2026-10-30：原来只有一级组合（sat_sh：加 + 桶形 + 3 次比较），实测
    //    s2sum_reg[0][1]/C → rb0_reg/D = 24 级、slack −1.656 ns，是 rb0..rb3
    //    那 4×8 = 32 个端点的公共源。现按"每拍一件事"拆成 3 级（详见 §1 的注释）。
    //
    //  逐拍（T = 本像素最后一个 term 在 S0 发出的那一拍，与 §14 的记号一致）：
    //    T+5  s2_vld=1   → 本块拍D：T+5 沿 s3q_q <= s2sum + bias
    //    T+6  s3_vld=1   → 本块拍E：T+6 沿 s4v_q <= s3q_q >>> shift
    //    T+7  s4_vld=1   → 拍F 组合饱和 → T+7 沿推进写队列（rb0..rb3 / raddr_q）
    //    T+8..T+11       → 写队列 4 拍排出；T+12 起 rvld_q=0
    //  （★ 2026-10-30：整表比加 §11b 之前晚 1 拍；T+3 是 s1_vld、T+4 是 s1b_vld。）
    //  地址/掩码链：s1addr(T+3) → s1baddr(T+4) → s2addr(T+5) → s3addr(T+6)
    //              → s4addr(T+7) 推入 raddr_q。逐级都由各自 valid 使能，
    //              所以**不会**被下一个像素的末拍覆盖（单 term 像素时像素间零间隙）。
    //  掩码：mask_c = mask_w 只依赖 og_q/cout_q —— 在一整个 oc 组内是常量，
    //        换组要等 ST_DRN 排空（那时 s4_vld 已为 0），所以**不需要**跟着打拍。
    // ========================================================================
    // ---- 拍D：q = sum + bias（32 位加，1 级进位链）----
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            s3_vld  <= 1'b0;
            s3addr  <= 32'd0;
            for (int i = 0; i < 4; i = i + 1) s3q_q[i] <= 32'sd0;
        end else if (cmp_load) begin
            // 换组清 valid（与 §14 拍A/拍B 的清零同沿；数值不关心，valid=0 即无效）
            s3_vld <= 1'b0;
        end else if (!freeze) begin
            s3_vld <= s2_vld;                            // 拍C → 拍D 的 valid
            if (s2_vld) begin
                s3q_q[0] <= s2sum[0] + bias_q[0];
                s3q_q[1] <= s2sum[1] + bias_q[1];
                s3q_q[2] <= s2sum[2] + bias_q[2];
                s3q_q[3] <= s2sum[3] + bias_q[3];
                s3addr   <= s2addr;                      // 地址随 valid 再打一拍
            end
        end
    end

    // ---- 拍E：v = q >>> shift（32 位算术桶形移位，6 级 mux）----
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            s4_vld  <= 1'b0;
            s4addr  <= 32'd0;
            for (int i = 0; i < 4; i = i + 1) s4v_q[i] <= 32'sd0;
        end else if (cmp_load) begin
            s4_vld <= 1'b0;
        end else if (!freeze) begin
            s4_vld <= s3_vld;                            // 拍D → 拍E 的 valid
            if (s3_vld) begin
                s4v_q[0] <= s3q_q[0] >>> shift_q;        // '>>>' = 算术右移（向 -∞）
                s4v_q[1] <= s3q_q[1] >>> shift_q;
                s4v_q[2] <= s3q_q[2] >>> shift_q;
                s4v_q[3] <= s3q_q[3] >>> shift_q;
                s4addr   <= s3addr;
            end
        end
    end

    // ---- 拍F：relu + 上下饱和（组合；只喂 rb0..rb3 的 D 端，不在任何回路上）----
    wire [7:0] sb0_c = sat8f(s4v_q[0], relu_q);
    wire [7:0] sb1_c = sat8f(s4v_q[1], relu_q);
    wire [7:0] sb2_c = sat8f(s4v_q[2], relu_q);
    wire [7:0] sb3_c = sat8f(s4v_q[3], relu_q);

    // 本组第 b 个 oc = og*4+b；只有 o < cout 的字节才写
    wire [31:0] og4_c = {og_q[29:0], 2'b00};       // og*4
    wire [3:0]  mask_w = { ((og4_c + 32'd3) < {16'd0, cout_q}),
                           ((og4_c + 32'd2) < {16'd0, cout_q}),
                           ((og4_c + 32'd1) < {16'd0, cout_q}),
                           ( og4_c          < {16'd0, cout_q}) };

    assign b0_c   = sb0_c;
    assign b1_c   = sb1_c;
    assign b2_c   = sb2_c;
    assign b3_c   = sb3_c;
    assign mask_c = mask_w;

    // ========================================================================
    // 16. 计算计数器（进入 CMP 时初始化，之后每拍推进一个 term）
    //     ★ 坐标 iy/ix（只给 in_bounds 用）仍按增量维护，不做每拍乘法，负数用
    //       17 bit 补码自然回绕；**读地址**搬到 §9b 的 rd_addr_q（见 §6b），
    //       这里只额外维护它的两个"锚点"：
    //         pix_base_q  : 当前像素首 term 地址  ⇒ ox++ 时 +s*8 即得新像素首地址
    //         rowy_nxt_q  : 下一 oy 行的行首      ⇒ oy++ 时 -pad*8 即得新行首像素
    //       于是"像素推进"这个最麻烦的地址跳变也只有 1 次加法（旧版要
    //       in_b+rowbase+… 三项串联）。
    // ========================================================================
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            cg_q <= 10'd0; j_q <= 10'd0; kx_q <= 10'd0;
            ox_q <= 9'd0;  oy_q <= 9'd0;
            iy_q <= 17'sd0; ix_q <= 17'sd0;
            ybase_q <= 17'sd0; xbase_q <= 17'sd0;
            pix_base_q <= 32'd0; rowy_nxt_q <= 32'd0;
            pix_nx_q   <= 32'd0; row0_q     <= 32'd0;
            eaddr_q <= 9'd0; oaddr_q <= 32'd0;
        end else if (cmp_load) begin
            // ★ P1：初始化条件由 cmp_init（WLD 末拍）改成 cmp_load（BIA 第 2 拍）。
            //   等价性：BIA/WLD 期间**没有任何消费者**读这 13 个寄存器 ——
            //   唯一的组合读口 eaddr（§12 权重 bank 读地址）被 state==ST_CMP 挡住，
            //   其余消费者（term_last / pix_last / in_bounds / oaddr→r1_oaddr）
            //   全部只在 ST_CMP（或 CMP 的下游流水）里被采样。见 §4b。
            //   obase_q / in_b / padw8_q / pad8_q / sw8_q / padS_q 在 BIA 首拍时
            //   都已是本组的值（DRN→BIA 那一沿全部更新完），与旧版相同。
            // ---- 第一个像素 (oy=0, ox=0)，ky=kx=0，cg=0 ----
            cg_q   <= 10'd0;
            j_q    <= 10'd0;
            kx_q   <= 10'd0;
            ox_q   <= 9'd0;
            oy_q   <= 9'd0;
            ybase_q   <= -padS_q;
            xbase_q   <= -padS_q;
            iy_q      <= -padS_q;
            ix_q      <= -padS_q;
            // 首像素首 term = in_b - pad*W*8 - pad*8
            pix_base_q  <= in_b - padw8_q - pad8_q;
            rowy_nxt_q  <= in_b - padw8_q + sw8_q;   // 下一行行首 = in_b-(pad-s)*W*8
            // ★ 两个预存副本与上面**同一条表达式**、同一沿装载（同一个 cfg 采样
            //   窗口：s8_q / pad8_q / padw8_q / sw8_q 在 ST_LOAD 就已锁存，到
            //   cmp_load（BIA c1）早稳定 ⇒ 与 pix_base_q/rowy_nxt_q 逐位自洽）：
            pix_nx_q    <= (in_b - padw8_q - pad8_q) + s8_q;
            row0_q      <= (in_b - padw8_q + sw8_q) - pad8_q;
            eaddr_q   <= 9'd0;
            oaddr_q   <= obase_q;
        end else if ((state == ST_CMP) && !freeze && !pix_last) begin
            if (term_last) begin
                // ---- 本像素算完：推进到 raster 序的下一个像素 ----
                cg_q    <= 10'd0;
                j_q     <= 10'd0;
                kx_q    <= 10'd0;
                eaddr_q <= 9'd0;
                oaddr_q <= oaddr_q + 32'd8;     // C8：相邻像素恒 +8
                pix_base_q <= cm_pix_c;         // 下一个像素的首 term 地址
                // ★ 预存副本同步推进：pix_base_q 的 D 端就是 cm_pix_c，所以
                //   pix_nx_q 必须装 (cm_pix_c + s8_q) 才能保持
                //   pix_nx_q ≡ pix_base_q + s8_q（s8_q 在 CMP 期间恒定）。
                pix_nx_q   <= cm_pix_c + s8_q;
                if (ox_last) begin
                    ox_q      <= 9'd0;
                    oy_q      <= oy_q + 9'd1;
                    ybase_q   <= ybase_q + sS_q;
                    iy_q      <= ybase_q + sS_q;
                    xbase_q   <= -padS_q;
                    ix_q      <= -padS_q;
                    rowy_nxt_q <= rowy_nxt_q + sw8_q;   // 再下一行
                    // ★ 同一沿：row0_q ≡ rowy_nxt_q - pad8_q（pad8_q 恒定）
                    //   ⇒ 新值 = (rowy_nxt_q + sw8_q) - pad8_q = row0_q + sw8_q。
                    row0_q     <= row0_q + sw8_q;
                end else begin
                    ox_q      <= ox_q + 9'd1;
                    xbase_q   <= xbase_q + sS_q;
                    ix_q      <= xbase_q + sS_q;
                    iy_q      <= ybase_q;
                end
            end else if (j_last) begin
                // ---- 换下一个 8 通道组（同一像素）----
                j_q     <= 10'd0;
                cg_q    <= cg_q + 10'd1;
                // ★ P5：条目号 = cg*k² + j，改成 **9 bit 累加器 eaddr_q**
                //   （旧版是 32 bit eoff_q，再由 §12 做 16 bit `eoff_q[15:0]+j_q`）。
                //   ⚠ 这里**也是 +1**，不是 +k²：走进这个分支时 j_q 已经等于 k²-1
                //     （j_last），所以
                //        新值 = (cg+1)*k² + 0 = (cg*k² + k²-1) + 1 = 旧值 + 1
                //     （旧版 eoff_q 不含 j，才写成 eoff_q + kk_q）。
                //   于是三条 tap 分支的增量**完全一致**（都是 +1），综合成
                //   一个 9 bit 增 1 加法器 + 一个 term_last 的清零 mux。
                eaddr_q <= eaddr_q + 9'd1;
                kx_q    <= 10'd0;
                iy_q    <= ybase_q;
                ix_q    <= xbase_q;
            end else if (kx_last) begin
                // ---- 换到下一行 tap（ky++）----
                // ★ P5：ky_q 已删（无人读；旧版只出现在 §9c 的 $display 里）。
                j_q     <= j_q + 10'd1;
                kx_q    <= 10'd0;
                eaddr_q <= eaddr_q + 9'd1;
                iy_q    <= iy_q + dS_q;
                ix_q    <= xbase_q;
            end else begin
                // ---- 同一行 tap 右移（kx++）----
                j_q     <= j_q + 10'd1;
                kx_q    <= kx_q + 10'd1;
                eaddr_q <= eaddr_q + 9'd1;
                ix_q    <= ix_q + dS_q;
            end
        end
    end

    // ========================================================================
    // 17. 权重装载（ST_WLD）：容器字节流 → 4 个 bank，1 字节/拍
    //     只搬 j < k² 的字节（TAP 的 3 个补齐字节不占拍）
    // ========================================================================
    wire [7:0] wl_byte = mem_rd_data[8*wl_t +: 8];
    wire [15:0] wl_entry = wl_eo[15:0] + wl_j0 + {12'd0, wl_t};

    // ========================================================================
    // 6b. 权重 bank 的**写口**：必须是**无复位**的时序块
    //
    //   硬件约束（踩过）：只要 RAM 数组出现在 'always_ff @(posedge clk or
    //   negedge rst_n)' 的复位分支里，Vivado 就判定 "RAM is sensitive to
    //   asynchronous reset. this RTL style is not supported" ⇒ 拒绝推断
    //   BRAM/DRAM ⇒ "dissolved into registers"。
    //   4 个 512×64 bank 溶解后 = 131072 个触发器，配合读口的巨型 mux 树，
    //   实测 84479 LUT（器件仅 46200）、BRAM 0、无法布线。
    //
    //   所以：RAM 写口单独成一个**没有复位**的 always_ff。RAM 本来也不需
    //   要复位（首次使用前一定会被权重装载写满需要的条目）。
    //
    //   第二个约束（实测 Synth 8-6841 / 8-6849）：**左侧不能用动态字节切片**。
    //   'wbank0[a][8*wl_c[2:0] +: 8]' 会被 Vivado 推成 1 bit 宽写的 RAM
    //   （"byte width (1) is not a multiple of 8"）⇒ wbank2/3 各拆成 64 个
    //   RAMB18（共 64 tile），wbank0/1 直接被判 "Infeasible attribute
    //   ram_style=block ... trying to implement using LUTRAM"（2048 LUT）。
    //   改成 UG901 的 byte-write-enable 模板：读/写口都是 64 bit，
    //   8 个**静态**字节切片，各配 1 bit 写使能（wsel 由 wl_c[2:0] 译码）。
    //   ⇒ 4 个 bank 各 1 块 RAMB36（4 tile），LUTRAM 那 2048 LUT 消失。
    // ========================================================================
    wire wbank_we = (state == ST_WLD) && wl_ph && (wl_entry <= 16'd511);
    wire [7:0] wsel   = wbank_we ? (8'b0000_0001 << wl_c[2:0]) : 8'd0;
    wire [8:0] wentry = wl_entry[8:0];

    always_ff @(posedge ap_clk) begin
        case (wl_b)
            2'd0: begin
                if (wsel[0]) wbank0[wentry][ 7: 0] <= wl_byte;
                if (wsel[1]) wbank0[wentry][15: 8] <= wl_byte;
                if (wsel[2]) wbank0[wentry][23:16] <= wl_byte;
                if (wsel[3]) wbank0[wentry][31:24] <= wl_byte;
                if (wsel[4]) wbank0[wentry][39:32] <= wl_byte;
                if (wsel[5]) wbank0[wentry][47:40] <= wl_byte;
                if (wsel[6]) wbank0[wentry][55:48] <= wl_byte;
                if (wsel[7]) wbank0[wentry][63:56] <= wl_byte;
            end
            2'd1: begin
                if (wsel[0]) wbank1[wentry][ 7: 0] <= wl_byte;
                if (wsel[1]) wbank1[wentry][15: 8] <= wl_byte;
                if (wsel[2]) wbank1[wentry][23:16] <= wl_byte;
                if (wsel[3]) wbank1[wentry][31:24] <= wl_byte;
                if (wsel[4]) wbank1[wentry][39:32] <= wl_byte;
                if (wsel[5]) wbank1[wentry][47:40] <= wl_byte;
                if (wsel[6]) wbank1[wentry][55:48] <= wl_byte;
                if (wsel[7]) wbank1[wentry][63:56] <= wl_byte;
            end
            2'd2: begin
                if (wsel[0]) wbank2[wentry][ 7: 0] <= wl_byte;
                if (wsel[1]) wbank2[wentry][15: 8] <= wl_byte;
                if (wsel[2]) wbank2[wentry][23:16] <= wl_byte;
                if (wsel[3]) wbank2[wentry][31:24] <= wl_byte;
                if (wsel[4]) wbank2[wentry][39:32] <= wl_byte;
                if (wsel[5]) wbank2[wentry][47:40] <= wl_byte;
                if (wsel[6]) wbank2[wentry][55:48] <= wl_byte;
                if (wsel[7]) wbank2[wentry][63:56] <= wl_byte;
            end
            default: begin
                if (wsel[0]) wbank3[wentry][ 7: 0] <= wl_byte;
                if (wsel[1]) wbank3[wentry][15: 8] <= wl_byte;
                if (wsel[2]) wbank3[wentry][23:16] <= wl_byte;
                if (wsel[3]) wbank3[wentry][31:24] <= wl_byte;
                if (wsel[4]) wbank3[wentry][39:32] <= wl_byte;
                if (wsel[5]) wbank3[wentry][47:40] <= wl_byte;
                if (wsel[6]) wbank3[wentry][55:48] <= wl_byte;
                if (wsel[7]) wbank3[wentry][63:56] <= wl_byte;
            end
        endcase
    end

    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            wl_b  <= 2'd0;
            wl_c  <= 16'd0;
            wl_j0 <= 16'd0;
            wl_t  <= 4'd0;
            wl_ph <= 1'b0;
            wl_rb <= 32'd0;
            wl_eo <= 32'd0;
            wl_j0last_q <= 16'd0;                       // ★ P6
            wl_lastnb_q <= 4'd0;                        // ★ P6
            wl_jglast_q <= 1'b0;                        // ★ P7
            wl_clast_q  <= 1'b0;                        // ★ P7
            st_prev <= ST_IDLE;
        end else begin
            st_prev <= state;                       // 每拍都记，供 wl_enter 判沿
            if (wl_enter) begin
                wl_b  <= 2'd0;
                wl_c  <= 16'd0;
                wl_j0 <= 16'd0;
                wl_t  <= 4'd0;
                wl_ph <= 1'b0;
                wl_rb <= 32'd0;
                wl_eo <= 32'd0;
                // ★ P6（§8a）：两个"行末组"常量与本拍清零点**同一个沿**锁存
                //   （kk_q 在上一次 start_pulse 就已稳定，不关心它是否同拍变）。
                wl_j0last_q <= wl_j0last_c;
                wl_lastnb_q <= wl_lastnb_c;
                // ★ P7：边界判定寄存器与 j0/c 的清零同一个沿、按清零后的值装入
                //   （j0<=0 且 j0last_q<=c ⇒ (0==c)；c<=0 ⇒ (0==cin_m1)）
                wl_jglast_q <= (16'd0 == wl_j0last_c);
                wl_clast_q  <= (16'd0 == cin_m1_q);
            end else if (state == ST_WLD) begin
                // ⚠ 权重 bank 的写口**不在这里**：见下面单独的 always_ff。
                //   把 RAM 数组写在带异步复位的时序块里，Vivado 会判
                //   "RAM is sensitive to asynchronous reset" ⇒ 无法推断 BRAM
                //   ⇒ 溶解成 131072 个触发器 + 巨型 mux 树。
                //   实测代价：conv_int8_uni 单 IP 84479 LUT、BRAM 0（器件只有 46200）。
                // ---- 推进 ----
                if (wl_ph == 1'b0) begin
                    wl_ph <= 1'b1;                     // 第一次读已发出
                end else if (!wl_tend) begin
                    wl_t <= wl_t + 4'd1;
                end else begin
                    wl_t <= 4'd0;
                    // ★ P7：在"本组最后一个字节"这一沿，wl_j0 / wl_c 即将下一次改变，
                    //   两个边界判定寄存器用**下一拍的值**同步更新（见 §8a' 的归纳证明）。
                    //   非 tend 拍两者都保持 —— 与 j0/c 的保持语义一致。
                    wl_jglast_q <= wl_rowend ? (16'd0 == wl_j0last_q)
                                             : ((wl_j0 + 16'd8) == wl_j0last_q);
                    if (wl_rowend) begin
                        // 本行（通道 wl_c）结束
                        wl_j0 <= 16'd0;
                        wl_rb <= wl_rb + {16'd0, tap_q};
                        // wl_c 的下一拍值 = c==cin_m1 ? 0 : c+1 ⇒ 同步更新 clast
                        wl_clast_q <= wl_c_eq ? (16'd0 == cin_m1_q)
                                              : ((wl_c + 16'd1) == cin_m1_q);
                        if (wl_c == cin_m1_q) begin
                            wl_c  <= 16'd0;
                            wl_eo <= 32'd0;
                            if (wl_b == 2'd3) ;        // 最后一行的最后一字节：转 CMP
                            else wl_b <= wl_b + 2'd1;
                        end else begin
                            wl_c <= wl_c + 16'd1;
                            if (wl_c[2:0] == 3'd7) wl_eo <= wl_eo + kk_q;
                        end
                    end else begin
                        wl_j0 <= wl_j0 + 16'd8;
                    end
                end
            end
        end
    end

    // ========================================================================
    // 18. 主 FSM
    // ========================================================================
    always_ff @(posedge ap_clk or negedge ap_rst_n) begin
        if (!ap_rst_n) begin
            state   <= ST_IDLE;
            done_q  <= 1'b0;
            start_q <= 1'b0;
            og_q    <= 32'd0;
            wbase_q <= 32'd0;
            bbase_q <= 32'd0;
            obase_q <= 32'd0;
            bia_i   <= 2'd0;
            bia_ph  <= 1'b0;
            dr_q    <= 4'd0;
            cin_q   <= 16'd0; cout_q <= 16'd0; k_q <= 16'd0; tap_q <= 16'd0;
            h_q     <= 16'd0; w_q <= 16'd0; hout_q <= 16'd0; wout_q <= 16'd0;
            kk_q    <= 11'd0; ncg_q <= 11'd0; nog_q <= 11'd0;
            kk_m1_q <= 11'd0; ncg_m1_q <= 11'd0;                    // ★ P0
            k_m1_q  <= 16'd0; wout_m1_q <= 16'd0;
            hout_m1_q <= 16'd0; cin_m1_q <= 16'd0;
            shift_q <= 6'd0;  relu_q <= 1'b0;
            hw8_q   <= 32'd0; w8_q <= 32'd0; s8_q <= 32'd0; d8_q <= 32'd0;
            sw8_q   <= 32'd0; dw8_q <= 32'd0; pad8_q <= 32'd0; p8_q <= 32'd0;
            wgtblk_q <= 32'd0;
            dx_q <= 32'd0; dy_q <= 32'd0;
            step_ky_q <= 32'd0; step_cg_q <= 32'd0;
            padS_q  <= 17'sd0; sS_q <= 17'sd0; dS_q <= 17'sd0;
            in_b    <= 32'd0; wgt_b <= 32'd0; bias_b <= 32'd0; out_b <= 32'd0;
            for (int i = 0; i < 4; i = i + 1) bias_q[i] <= 32'sd0;
        end else begin
            start_q <= ctrl_start;
            done_q  <= 1'b0;

            case (state)
            // ---------------- 等启动 ----------------
            ST_IDLE: begin
                if (start_pulse) begin
                    cin_q    <= cfg_cin;
                    cout_q   <= cfg_cout;
                    k_q      <= cfg_k;
                    tap_q    <= {8'd0, cfg_tap_stride};
                    h_q      <= cfg_h;
                    w_q      <= cfg_w;
                    shift_q  <= cfg_shift;
                    relu_q   <= cfg_relu;
                    kk_q     <= kk_c;
                    ncg_q    <= ncg_c;
                    nog_q    <= nog_c;
                    hout_q   <= hout_c;
                    wout_q   <= wout_c;
                    // ★ P0：末值寄存器与上面 kk_q/ncg_q/... **同一沿**锁存
                    //   （同一个 cfg_* 采样窗口，语义与旧版"用 kk_q-1 当拍现算"相同）
                    kk_m1_q   <= kk_c   - 11'd1;
                    ncg_m1_q  <= ncg_c  - 11'd1;
                    k_m1_q    <= cfg_k  - 16'd1;
                    wout_m1_q <= wout_c - 16'd1;
                    hout_m1_q <= hout_c - 16'd1;
                    cin_m1_q  <= cfg_cin - 16'd1;
                    w8_q     <= w8_c;
                    s8_q     <= s8_c;
                    d8_q     <= d8_c;
                    sw8_q    <= sw8_c;
                    dw8_q    <= dw8_c;
                    pad8_q   <= pad8_c;
                    padw8_q  <= padw8_c;
                    wgtblk_q <= wgtblk_c;
                    // §6b 的读地址步长（纯移位相加，一次算好，之后每拍只做 1 次加法）
                    dx_q       <= dx_c;          // (k-1)*d*8
                    dy_q       <= dy_c;          // (k-1)*d*W*8
                    // ★ P5：wl_ngrp_q / wl_dlast_q 已删（恒等式 ⇒ 死码，见 §2/§9b）
                    padS_q   <= $signed({16'd0, cfg_pad});
                    sS_q     <= $signed({16'd0, cfg_s});
                    dS_q     <= $signed({16'd0, cfg_d});
                    in_b     <= base_in;
                    wgt_b    <= base_wgt;
                    bias_b   <= base_bias;
                    out_b    <= base_out;
                    og_q     <= 32'd0;
                    wbase_q  <= base_wgt;      // og=0 ⇒ 块偏移 0
                    bbase_q  <= base_bias;
                    obase_q  <= base_out;      // og=0 ⇒ (0/2)*P8 + 0
                    bia_i    <= 2'd0;
                    bia_ph   <= 1'b0;
                    dr_q     <= 4'd0;
                    state    <= ST_BIA;
                end
            end
            // ---------------- 偏置：4 拍（2 次 发读+收数）----------------
            ST_BIA: begin
                // ★ 两个"真"乘法在这里算：输入是**已锁存**的 h_q/w_q/hout_q/wout_q
                //   （FF → DSP → FF，各 1 个 DSP48），不再是 cfg_* 输入端口 → DSP。
                //   BIA 有 4 拍空转，hw8/p8 最早也要 WLD/CMP/DRN 才用，覆盖得住。
                hw8_q <= {16'd0, h_q} * {16'd0, w_q} << 3;        // H*W*8
                p8_q  <= {16'd0, hout_q} * {16'd0, wout_q} << 3;  // Hout*Wout*8
                // ★ §6b 的两个 CMP 步长（hw8_q 从本状态第 2 拍起才有效，所以放在
                //   BIA 里每拍无条件算一次，到 cmp_init 早已稳定）：
                //     ky++ : 换下一行 tap、kx 回 0 ⇒ +d*W*8-(k-1)*d*8
                //     cg++ : 换下个通道组、ky/kx 归零 ⇒ +H*W*8-(k-1)*d*W*8-(k-1)*d*8
                step_ky_q <= dw8_q - dx_q;
                step_cg_q <= hw8_q - dy_q - dx_q;
                if (bia_ph == 1'b0) begin
                    bia_ph <= 1'b1;
                end else if (bia_i == 2'd0) begin
                    bias_q[0] <= $signed(mem_rd_data[31:0]);
                    bias_q[1] <= $signed(mem_rd_data[63:32]);
                    bia_i  <= 2'd1;
                    bia_ph <= 1'b0;
                end else begin
                    bias_q[2] <= $signed(mem_rd_data[31:0]);
                    bias_q[3] <= $signed(mem_rd_data[63:32]);
                    bia_i  <= 2'd0;
                    bia_ph <= 1'b0;
                    state  <= ST_WLD;
                end
            end
            // ---------------- 权重装载：4*cin*k² 拍 ----------------
            ST_WLD: begin
                if (wl_done) state <= ST_CMP;
            end
            // ---------------- 计算主体 ----------------
            ST_CMP: begin
                if (pix_last && !freeze) begin
                    dr_q  <= 4'd0;
                    state <= ST_DRN;
                end
            end
            // ---------------- 排空（写队列 + 5 级收尾流水）----------------
            ST_DRN: begin
                dr_q <= dr_q + 4'd1;
                // ★ 原来是 'dr_q == 4'd11'：不满足就走到 12/13/14/15 再回绕，
                //   重试粒度变成 16 拍。改成 >= 后每拍都重试（每个 oc 组最多多耗
                //   十几拍，相对几千拍的计算体 < 0.5%，可忽略）。13 拍的最短排空
                //   拍数不变（dr_q 从 0 数到 12 才第一次检查）。
                // ★ 2026-10-30：11 → 12。§11b 在 S1 里插了一拍，收尾流水整体
                //   往后挪一拍（rvld_q 由 T+7..T+10 变成 T+8..T+11），第一次检查
                //   必须跟着推到 **T+13**，否则余量从 1 拍掉成 0 拍。
                if (dr_q >= 4'd12) begin
                    // ★ 排空判据必须覆盖**收尾流水的全部 valid**（漏一个就会在流水没排空时
                    //   切 oc 组 ⇒ 丢掉本组最后一个像素，或其 s2sum 被下一组的
                    //   cmp_init 冲掉）：
                    //     r1_pend(S1a 待发) → r1b_vld(S1b 待累加) → s1_vld(拍A)
                    //     → s1b_vld(拍B) → s2_vld(拍C) → s3_vld(拍D) → s4_vld(拍E)
                    //     → rvld_q(写队列 4 拍)
                    //   归约树拆两拍后新增 s1b_vld；收尾三级（§15）又新增
                    //   s3_vld/s4_vld；§11b 的 DSP 输入寄存器又新增 r1b_vld
                    //   （它是"xr_q 里那一份还没进 acc"的标志，漏了它就会在
                    //   最后一个 term 还没进 acc 时切组 ⇒ 丢整个末 term）。
                    //   s1_vld 也一并加上（更保守：s1_vld=1 时 s1b_vld 下一拍必为 1）。
                    //   ⚠ 拍数核算（T = CMP 最后一个 term 发出的那一拍，
                    //     见 §15 的逐拍表）：本判据第一次生效是 **T+13**，而
                    //     最后一个非零 valid 是 rvld_q（T+8..T+11 为 1，T+12 为 0）
                    //     ⇒ 余量 **1 拍**（与加这一级之前相同）。再拆一级（rvld_q
                    //     推到 T+12）就必须把这一行改成 >= 4'd13。
                    //     ⚠ 这个判据是**每拍重试**的，所以"余量不够"只会多等，
                    //     不会丢数/死锁 —— 真正保证正确性的是条件本身，不是 13 拍。
                    if (!rvld_q && !s4_vld && !s3_vld && !s2_vld && !s1b_vld &&
                        !s1_vld && !r1b_vld && !r1_pend) begin
                        dr_q <= 4'd0;
                        if (og_q == (nog_q - 32'd1)) begin
                            state <= ST_DONE;
                        end else begin
                            og_q    <= og_q + 32'd1;
                            wbase_q <= wbase_q + wgtblk_q;
                            bbase_q <= bbase_q + 32'd16;
                            // obase(og) = base_out + (og/2)*P8 + (og%2)*4
                            obase_q <= obase_q + (og_q[0] ? (p8_q - 32'd4) : 32'd4);
                            bia_i   <= 2'd0;
                            bia_ph  <= 1'b0;
                            state   <= ST_BIA;
                        end
                    end
                end
            end
            // ---------------- 完成脉冲 ----------------
            ST_DONE: begin
                done_q <= 1'b1;
                state  <= ST_IDLE;
            end
            default: state <= ST_IDLE;
            endcase
        end
    end

endmodule
