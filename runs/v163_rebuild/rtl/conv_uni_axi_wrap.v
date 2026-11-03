// ============================================================================
//  conv_uni_axi_wrap.v —— conv_int8_uni 的 AXI4 包装层
//                          （AXI4-Lite 从：配置/控制；AXI4 主：内存）
//
//  被测单元：modules/conv_int8_uni.v（874 行，6 个形状逐字节已验证，端口一个字没改）
//  规格依据：docs/binary/04_定制int8引擎规格.md
//  性能教训：docs/rtl_hand/上板性能优化_20261028.md
//            （k1 因"读仲裁一次只许一笔在飞"，每笔单拍读付 ≈56 拍往返，
//              整网慢 5.3×；以及"burst 不能跨 4 KB""不能用门控撤销 arvalid"）
//  结构范本：modules/bin_unit_axi_wrap.v（4/4 UNIT、17/17 用例通过）
//            modules/k1_axi_wrap.v（AXI4-Lite 两页式寄存器风格）
//
//  ============================================================================
//  0. 与 bin_unit_axi_wrap 的第一处实质差异：读地址不是 8 字节对齐
//  ============================================================================
//  conv_int8_uni 的权重读地址是 4 字节对齐（§3.3 / 引擎 §4）：
//
//       addr = base_wgt + ((o*cin + c)*TAP + j0)，  TAP ∈ {4,12,28}，j0 ∈ {0,8,16}
//
//  TAP 全是 4 的倍数、base_wgt 又被本层强制 8 字节对齐（ERR_ALIGN），所以
//  addr[2:0] ∈ {0,4}；但引擎要的是**从 addr 起的连续 8 个字节**（一次读喂 8 条
//  通道车道）。当 addr[2:0]=4 时，这 8 个字节**横跨两个 8 字节字**：
//
//      word(A)   = A[31:3]        （8 字节对齐字，含字节 A..A+3）
//      word(A)+1 = A[31:3] + 1    （含字节 A+4..A+7）
//      data = ( {word(A)+1, word(A)} >> 8*A[2:0] )[63:0]
//
//  所以本层**绝不向 AXI 发非对齐 AR**（ARADDR 永远 8 字节对齐、arlen=0，
//  AXI4 完全合法），而是在线缓存里多留 1 个槽（见 §1.2），由组合桶形移位器
//  拼出这 8 个字节。命中判据也从"1 个字有效"变成"该字 + 跨界的下 1 个字都有效"。
//
// ============================================================================
//  1. 读侧：NLINES 条全相联线缓存 + 2D 窗口预取 + 门控时钟
// ============================================================================
//  1.1 基本语义（正确性的根，与 bin_unit 一致）
//    引擎内存口是 **BRAM 语义**：t 拍给 mem_rd_en + mem_rd_addr，mem_rd_data 必须
//    在 **t+1 拍**有效，且**没有反压输入**（tb_conv_int8_uni.v:
//    'always @(posedge clk) if (mem_rd_en) mem_rd_data <= mem[addr..addr+7];'）。
//    所以命中时本层在 t 拍组合出 8 字节、t+1 前寄存（时间轴与直连 BRAM 逐拍
//    相同）；未命中时**停住引擎时钟**（§5），等数据。
//
//  ⚠ 单条 64B 线缓存在本引擎上**不成立**：引擎的计算主体每个 term 跳一次
//    （kx++ : +d*8 → ky++ : +d*W*8 → cg++ : +H*W*8），一个像素要摸
//    **NCG*k 条不同的线**（k 行 × NCG 个平面），而一条线要 8 次需求才摊得开
//    一次往返。实测单线结构比引擎直连慢 4~12×（rlat=4）/ 14~45×（rlat=56）。
//
//  1.2 线 = RD_WIN_WORDS（默认 8）个连续 64-bit 字 + **1 个前瞻槽**
//        slot 0 .. RD_WIN_WORDS-1 : 线本体（64 B）
//        slot RD_WIN_WORDS        : 线的下 1 个字（= 下一条线的第 0 字）
//      前瞻槽是为 §0 的跨字读服务的：4 字节对齐读落在线的**最后一个字**时，
//      需要"下一个字"，而那个字属于下一条线；存在本条目里，一条线内任意 4 字节
//      对齐读都能被单独一个条目服务（否则永远不命中）。代价 +12.5% 读流量。
//
//  1.3 全相联 + 两份存储（为什么）
//      · key = 行 tag = addr[31 : IDX_W+3]（默认 26 bit，即 64 B 行号）。
//      · NLINES 个条目（默认 32/64），每条目 NSLOT = RD_WIN_WORDS+1 = 9 个 64-bit 槽，
//        槽步长补成 2 的幂 SLOT_STRIDE = 16 ⇒ 每条目 16 槽、DEPTH = NLINES*16 = 512 字。
//      · **需求读一拍内要同时拿 slot 和 slot+1 两个字** ⇒ 存**两份同内容**的
//        cache_a / cache_b，各 1 个读口，两份写口同源（R 通道）。
//        2026-10-30：两块都已 ram_style="block"，各占 1 块 RAMB36（512×64）、同步读
//        1 拍（地址 t 拍、数据 t+1 拍）。改 BRAM 前它们被综合成寄存器阵列
//        （Synth 8-4767 "Trying to implement RAM ... in registers"），
//        实测吃掉 ~9k LUT + 36864 FF，是全芯片最大的 LUT 黑洞。
//      · 命中 = CAM 比对 tag 选中条目 且 该条目 ent_rdy（**整条线到齐**）。
//        不做逐槽 valid：NSLOT 拍全回来才算可用（最多多等 8 拍），换来 576 bit
//        的 valid 位图退化成 64 bit 的 ready 位图，逻辑与断言都简单得多。
//      · NLINES=64 的依据：工作集 = NCG*k 条线（cin=192,k=1 ⇒ 24；cin=64,k=3 ⇒ 24；
//        cin=12,k=5 ⇒ 10），而一次窗口预取又要压进 NCG*k-1 条 ⇒ 峰值
//        2*NCG*k-1 ≤ 47，取 64 = 2*24 + 裕量（见 §1.6-①）。
//
//  1.4 填充：**2D 窗口预取 + 需求注入通道**
//      · 需求未命中且该 tag 不在缓存里 ⇒ 分配一个条目，走**注入通道**（单描述符，
//        AR 仲裁最高优先级）；
//      · 同时把**整个像素窗口**（NCG*k-1 条：本平面下面 k-1 行 + 其余 NCG-1 个
//        平面各 k 行，地址 = 需求行基址 + p*H*W*8 + r*d*W*8）全部压进预取 FIFO
//        ⇒ 一个像素的整窗**一轮往返到齐**。k=1 时行维退化，这条规则自动变成
//        "平面维全铺"，正好是 k=1 唯一能用的并发来源。
//      · AR 侧每拍从"注入通道 > 预取队列"取一个槽发**单拍读**（arlen=0），
//        **ARID = 条目号**（NLINES=64 ⇒ 6 bit；Zynq-7000 S_AXI_HP 端口 ID 宽
//        6 bit ⇒ 正好够）。同一 ID 的 AR 按槽序发 ⇒ AXI 保证同 ID 保序返回 ⇒
//        每个条目只需一个"下个 R 落哪个槽"的计数器 ent_wslot。
//      · 地址/ID 都放在寄存器里、**只在 AR 握手那一拍切换** ⇒ 握手前天然稳定
//        （AXI 硬规则；绝不能用 u_ce 或"本拍已发"去撤销 arvalid）。
//      · R 通道按 RID 直接落到该条目当前槽，ent_out 归零那一拍置 ent_rdy。
//      · 预取地址必须落在输入缓冲区内（inend = base_in + cp8*H*W*8），绝不越界读。
//      · 替换策略：alloc_ptr 轮转（FIFO）。本引擎访问序是循环的，工作集 ≤47 条
//        < NLINES=64 ⇒ 轮转等价 LRU，不必实现真 LRU。
//      · 多笔在飞上限 = 注入通道 9 笔 + 预取 FIFO 里的 9*N 笔；实测 max_rq = 33。
//
//  1.5 实测（iverilog 11，6 个 golden，逐字节 PASS；AR/需求读 = 实际 AXI 读事务数
//      与引擎需求读拍数之比，理想 = 1/8）。
//      "引擎直连" = tb_conv_int8_uni 官方拍数（BRAM 1 拍延迟、无 AXI）。
//      A = 单线 64B 缓存（上一版）  B = 本版（NLINES=64 全相联 + 2D 窗口预取）
//
//    #  形状                     引擎直连   A@56    B@4    B@56   B/引擎  需求填线  AR/需求读
//   --- ------------------------ -------- ------- ------- ------- ------ -------- ---------
//    1  uni_k1_64x12_8x8           2360   106368   5056    9792   4.15x      75      0.946
//    2  uni_k3_64x12_8x8          20792   297280  25472   36224   1.74x     174      0.449
//    3  uni_k5_12x48_8x8          53018       -   55360   69120   1.30x     276      0.116
//    4  uni_k3_d3_32x16           41546       -   55360   79104   1.90x     308      0.877
//    5  uni_k1_s2_192x64          18722       -   46016   95744   5.11x     848      1.130
//    6  uni_k3_nopad_16x8          3350       -    3776    5184   1.55x      29      0.197
//
//    @4 / @56 = TB 的 +RLAT（AXI 读从机往返拍数）。**按 rlat=56 那一列看板端**。
//    #5 是 cin=192/k=1 的合成形状（真实 SGRA 是 cin≤7）：一笔 64B 线只服务 8 次
//    需求、每 8 次需求就要 9 笔 AR ⇒ 这一档天生受 AR 发币率限制，绝对量很小
//    （SGRA 全部 9.4 MMAC）。
//
//  1.6 关键教训（改这个文件的人先看这三条）
//    ① **NLINES 必须 ≥ 2*NCG*k**：一次窗口预取要压 NCG*k-1 条线，而当前窗口的
//       工作集本身就是 NCG*k 条。NLINES=32 时预取会把正在用的线挤掉 ⇒ 换入换出，
//       实测 cin=64/k=3 从 1.74x 退化到 3.11x。
//    ② **需求填充必须有优先通道**：一次预取压进 ~200 笔 AR，若需求排在队尾，
//       每换一条线都要先等它们发完（实测让 k=3 慢 2 倍）。本层用单描述符注入
//       通道给需求最高优先级。
//    ③ **同 ID 保序**是 R 落槽的唯一依据（ARID = 条目号，一笔填充的 9 个槽共用
//       同一 ID）。TB 侧若用"槽号序"判保序，会在槽复用后放行乱序 —— 本模块自测
//       时踩过，最后按 **AR 序号** 判才修对。
//
//  ============================================================================
//  2. 写侧：8-bit 写口 → 64-bit beat → INCR burst
//  ============================================================================
//  引擎写口 8-bit、写当拍生效。输出 C8 布局：
//      addr(o,y,x) = base_out + (o/8)*Hout*Wout*8 + y*Wout*8 + x*8 + (o%8)
//  引擎按 oc 组（4 个 oc）一趟：组内 4 个 oc 的字节落在**同一个 64-bit 字**的
//  相邻 4 个 lane（(og%2)*4 + 0..3），下一拍 rcnt+1、下一像素地址 +8：
//
//      字 W0: lane 0..3 (og 偶) 或 4..7 (og 奇)  ← 每字 4 字节
//      字 W1: 同 lane 偏移，地址 +8              ← 正好是 AXI INCR 的下一拍
//
//  所以攒包规则与 bin_unit_axi_wrap §3 完全一致，且天然吃满：
//    · 一个 **beat** = 一个「64-bit 字对齐地址」上的 64-bit 数据 + 8-bit WSTRB。
//      字节落在哪个 lane 由**地址低 3 位**决定 ⇒ AWADDR 永远是字对齐的
//      （AXI4 INCR 的拍地址必须按传输尺寸对齐，非对齐首地址语义未定义）。
//    · **攒 beat**：mem_wr_addr[31:3] 相同就继续合并（本引擎每字 4 字节）；
//      不同就先把上一个 beat 推进 beat FIFO、再开新 beat。整字凑满
//      （WSTRB==8'hFF）立即推进，不等下一拍。
//    · **攒 burst**：新 beat 的字地址 == 当前 burst 首地址 + 8*已收拍数 ⇒ 串进
//      同一笔（AWLEN 自增，上限 WR_BURST_MAX=16 拍 = 128 B）；否则收掉当前 burst
//      另起一笔。串的条件还包含**不跨 4 KB**（字地址 [28:9] 不变）。
//    · **排空（不满足成包条件时）**：写口空闲 / beat FIFO 将满（"有 beat 必有
//      描述符"，防死锁）/ 换字，都走 2 拍 W_DR1→W_DR2 排空。
//    · done 上报前要求 'wr_all_idle'，保证 PS 看到 done=1 时内存已经一致。
//
//  注意：本引擎每字只写 4 个字节（WSTRB=0x0F 或 0xF0），所以写带宽的 50% 是
//  浪费的；这是"一趟 oc 组只填半个字"的输出布局决定的（要根治得让引擎一次吐
//  8 个通道 = 改单元，本次硬约束不允许）。
//
//  ============================================================================
//  3. 寄存器映射（**A[16] 分页**，与 k1/k3/k5/bin_unit 一致）
//  ============================================================================
//  BD 里把本从机的地址范围设成 128 KB（如 0x4000_0000–0x4001_FFFF）：
//    A[16]=1 → 配置页（PS 基址 +0x10000），下表偏移**相对 +0x10000**
//    A[16]=0 → 基址页（PS 基址 +0x00000）
//  A[16]=0 页里没有的偏移：写忽略、读回 0。
//
//  配置页（A[16]=1）：
//  偏移  名字         属性  说明
//  ----  ------------ ----  ----------------------------------------------------
//  0x00  AP_CTRL      RW    [0] start（写 1 启动，读回 0）；[1] auto-restart
//  0x10  AP_RETURN    RO    [0] done  [1] busy  **[15:8] 错误码**（见 §4）
//  0x18  R_H          RW    输入高（≤ HW_MAX=256）
//  0x20  R_W          RW    输入宽（≤ HW_MAX=256）
//  0x28  （保留）      -    Hout —— 由引擎按 (H-1)/s+1 自算，写忽略、读回 0
//  0x30  （保留）      -    Wout —— 同上
//  0x38  R_CIN        RW    输入通道（1..CIN_MAX=192）
//  0x40  R_COUT       RW    输出通道（1..COUT_MAX=64）
//  0x48  R_K          RW    卷积核 1 / 3 / 5
//  0x50  R_S          RW    stride 1 / 2
//  0x58  R_D          RW    dilation 1 / 2 / 3
//  0x60  R_PAD        RW    补边（约定 = k/2，0..8）
//  0x68  R_SHIFT      RW    输出算术右移（0..63）
//  0x70  R_RELU       RW    [0] 输出 ReLU
//  0x74  R_TAP        RW    权重 tap_stride，必须 = {1:4, 3:12, 5:28}
//  0x80  （保留）      -    与 k1/xsim 脚本的 R_HOUT/R_WOUT 区间错开，写忽略
//  0x94  STATUS       RO    [0] done [1] busy [2] err [7:4] err_code [8] idle
//  0xE0  DBG_RD       RW    [15:0] 需求读拍数（被放行的 rd_en 拍数）
//                           [31:16] 填线次数（每条 64 B 线一次）；写 0 清零
//  0xE4  DBG_AR       RW    [15:0] AR 笔数（= 实际 AXI 读事务数）
//                           [31:16] R 拍数；写 0xE0 同时清本寄存器
//  0xE8  DBG_WR       RW    [15:0] AW 笔数 [31:16] W 拍数；写 0 清零
//  0xF0  DBG_FLAG     RW    [0] RID 越界（sticky）
//                           [1] 基址高 32 位写过非 0（sticky，start 时 ERR_ADDR）
//                           [2] 写 burst 跨 4 KB（sticky，本不该发生）
//                           [3] u_busy（只读位）
//                           写 bit0 = 1 清 [0]/[1]/[2]
//
//  基址页（A[16]=0）—— 偏移与 k1_axi_wrap 的 HLS 默认 bundle **完全一致**，
//  PS 侧 conv_engine.py 的基址常量不用改（64 位：低字在偏移、高字在 +4）：
//  0x10  B_IN   lo    0x14  B_IN   hi
//  0x1C  B_WGT  lo    0x20  B_WGT  hi
//  0x28  B_BIAS lo    0x2C  B_BIAS hi
//  0x34  B_OUT  lo    0x38  B_OUT  hi
//  （引擎只用 32 位地址；高字非 0 ⇒ start 时 ERR_ADDR，不启动）
//
//  ============================================================================
//  4. 启动前合法性检查（失败：AP_RETURN[15:8]=错误码，不启动，done 直接置 1）
//  ============================================================================
//  错误码（AP_RETURN[15:8]）：
//    0  ERR_NONE   通过，已启动
//    1  ERR_RANGE  cin ∉[1,192] / cout ∉[1,64] / H,W ∉[1,256] / s ∉{1,2} /
//                  d ∉{1,2,3} / pad ∉[0,8] / shift > 63
//    2  ERR_TAP    k ∉ {1,3,5}，或 R_TAP 与 k 不匹配（k=1→4, k=3→12, k=5→28）
//    3  ERR_WCAP   权重 bank 容量不足：ceil(cin/8)*k² > 512（引擎 4 个 512×64
//                  bank，条目 e = (c/8)*k² + j 必须 < 512）
//    4  ERR_ALIGN  B_IN/B_WGT/B_BIAS/B_OUT 任一不 8 字节对齐
//                  （AXI4 传输地址不齐 = 从机未定义行为；本层所有 AR 都是
//                    8 字节对齐单拍，所以基址不齐时"读窗口"必然非法）
//    5  ERR_ADDR   某个基址的高 32 位写过非 0（32 位引擎装不下 >4 GB 地址）
//
//  参数 AUTO_TAP=1 时 R_TAP 允许写 0（= 由 k 自动推导 4/12/28）；默认 0 严格按
//  规格"R_TAP 必须与 k 匹配"。不论哪种模式，送给引擎的 cfg_tap_stride 都是
//  **由 k 推导出的值**（合法时与 R_TAP 相等）。
//
//  ============================================================================
//  5. 时钟：为什么引擎必须用门控时钟
//  ============================================================================
//  引擎没有反压口，唯一能让它"等"的手段就是停它的时钟。所以
//    USE_BUFGCE=0（默认）: u_clk = s_axi_aclk & u_ce_lat（always_latch 只在低电平
//                          期跟随 u_ce，等价 BUFGCE；纯 RTL，不依赖原语）；
//    USE_BUFGCE=1        : BUFGCE u_bufgce(.I(s_axi_aclk), .CE(u_ce), .O(u_clk))
//                          （综合时要 -L unisims_ver；**纯功能仿真无需打开**）。
//  u_ce=0 的三个来源：读未命中（等 AXI 数据）/ 写 FIFO/描述符 FIFO 已达压力阈值
//  （bf_cnt≥BF_PRESS 或 df_cnt≥DF_PRESS，2026-10-30 起改为"到阈值就停引擎"，
//   原来那种"本拍这个字节是安全合并就放过去"的预测在写口加了 1 拍入口寄存器后
//   会差一拍、可能预测错，已删除，见 §7.0）/ 写排空状态机在跑而本拍又要写。
//  复位与空闲期 u_ce 恒 1（时钟常通），避免仿真起点 X 把时钟卡死。
//  ⚠ arvalid 一旦拉高必须保持到 arready 握手，**绝不能用 u_ce 去撤销它**
//    （性能文档补记二：撤销即丢事务）；本层 arvalid 只在握手后才可能撤。
//
//  ============================================================================
//  参数
//  ============================================================================
`timescale 1ns / 1ps

module conv_uni_axi_wrap #(
    // ---- 读侧缓存 ----
    parameter int  RD_WIN_WORDS = 8,      // 每条线的 64-bit 字数（2/4/8/16，2 的幂）
    parameter int  NLINES       = 32,     // 全相联条目数（2 的幂；≥ 2*NCG*k，见下）
    parameter int  AXI_ID_W     = 6,      // ARID/RID 宽度，必须 ≥ $clog2(NLINES)
    parameter int  PF_MAX       = 32,     // 一次窗口预取最多压入几条线（0 = 关预取）
    parameter int  NFQ          = 16,     // 待发 AR 的填充队列深度
    // ---- 写侧 ----
    parameter int  WR_BUF_BEATS = 16,     // beat FIFO 深度（= 描述符 FIFO 深度）
    parameter int  WR_BURST_MAX = 16,     // 单笔写 burst 拍数上限（1..256）
    // ---- 时钟门控 ----
    parameter bit  USE_BUFGCE   = 1'b1,   // 0 = latch+AND；1 = BUFGCE 原语
    parameter bit  AUTO_TAP     = 1'b0,   // 1 = 允许 R_TAP=0（由 k 推导）
    // ---- 校验上限（与规格 §2 一致）----
    parameter int  CIN_MAX      = 192,
    parameter int  COUT_MAX     = 64,
    parameter int  HW_MAX       = 256,
    parameter int  WBANK_DEPTH  = 512
)(
    // ---- AXI4-Lite slave（配置/控制）----
    input  logic        s_axi_aclk,
    input  logic        s_axi_aresetn,      // 低有效
    input  logic [31:0] s_axi_awaddr,
    input  logic        s_axi_awvalid,
    output logic        s_axi_awready,
    input  logic [31:0] s_axi_wdata,
    input  logic [3:0]  s_axi_wstrb,
    input  logic        s_axi_wvalid,
    output logic        s_axi_wready,
    output logic [1:0]  s_axi_bresp,
    output logic        s_axi_bvalid,
    input  logic        s_axi_bready,
    input  logic [31:0] s_axi_araddr,
    input  logic        s_axi_arvalid,
    output logic        s_axi_arready,
    output logic [31:0] s_axi_rdata,
    output logic [1:0]  s_axi_rresp,
    output logic        s_axi_rvalid,
    input  logic        s_axi_rready,

    // ---- AXI4 master：读（8 字节对齐单拍，多线并发，ARID = 缓存条目号）----
    output logic                     m_axi_arvalid,
    input  logic                     m_axi_arready,
    output logic [31:0]              m_axi_araddr,
    output logic [3:0]               m_axi_arlen,   // AXI3: 4 bit（原 AXI4 是 8 bit）
    output logic [AXI_ID_W-1:0]      m_axi_arid,
    input  logic                     m_axi_rvalid,
    output logic                     m_axi_rready,
    input  logic [63:0]              m_axi_rdata,
    input  logic                     m_axi_rlast,
    input  logic [AXI_ID_W-1:0]      m_axi_rid,

    // ---- AXI4 master：写（1..WR_BURST_MAX 拍 INCR，不跨 4 KB）----
    output logic                     m_axi_awvalid,
    input  logic                     m_axi_awready,
    output logic [31:0]              m_axi_awaddr,
    output logic [3:0]               m_axi_awlen,   // AXI3: 4 bit
    output logic [AXI_ID_W-1:0]      m_axi_wid,     // AXI3 写 ID（恒 0）
    output logic                     m_axi_wvalid,
    input  logic                     m_axi_wready,
    output logic [63:0]              m_axi_wdata,
    output logic [7:0]               m_axi_wstrb,
    output logic                     m_axi_wlast,
    input  logic                     m_axi_bvalid,
    output logic                     m_axi_bready
);

    // ========================================================================
    // 0. 派生常量 / 错误码 / 状态编码
    // ========================================================================
    localparam int IDX_W  = $clog2(RD_WIN_WORDS);          // 线内槽号位宽
    localparam int NSLOT  = RD_WIN_WORDS + 1;              // +1 = 跨字前瞻槽
    localparam int SLOT_W = $clog2(NSLOT);
    localparam int TAG_W  = 29 - IDX_W;                    // 字地址[28:IDX_W]
    localparam int ENT_W  = $clog2(NLINES);                // 条目号位宽
    // 槽步长补成 2 的幂（真实只用 9 个槽）：索引退化成纯拼接，没有 *9 的加法器；
    // DEPTH = 32*16 = 512 又正好是一个 RAMB36（512×64）的深度 ⇒ 线缓存能整块进 BRAM。
    localparam int SLOT_STRIDE = 1 << SLOT_W;               // = 16
    localparam int DEPTH  = NLINES * SLOT_STRIDE;           // = 512（原 NLINES*NSLOT = 288）
    localparam int CADW   = $clog2(DEPTH);                  // = ENT_W + SLOT_W（= 9）
    localparam int FQ_W   = (NFQ > 1) ? $clog2(NFQ) : 1;
    localparam int FQ_CW  = $clog2(NFQ + 1);
    localparam int BF     = WR_BUF_BEATS;
    localparam int BF_W   = (BF > 1) ? $clog2(BF) : 1;
    localparam int BF_CW  = $clog2(BF + 1);
    localparam int DF     = WR_BUF_BEATS;
    localparam int DF_W   = (DF > 1) ? $clog2(DF) : 1;
    localparam int DF_CW  = $clog2(DF + 1);

    localparam [8:0]        BU_MAX   = WR_BURST_MAX;       // burst 拍数上限（≤256）
    localparam [SLOT_W-1:0] NSLOT_V  = NSLOT;
    localparam [SLOT_W-1:0] SLOT_Z   = {SLOT_W{1'b0}};
    localparam [BF_W-1:0]   BF_LAST  = BF - 1;
    localparam [BF_CW-1:0]  BF_Z     = {(BF_CW){1'b0}};
    // 写侧压力阈值：BF-4 而不是 BF-3。
    //   写口入口寄存器（§7.0）把"吞字节"与"推 beat"分开 1 拍 ⇒ 同一个字节被吞下
    //   时的 bf_cnt 与它真正 push 时的 bf_cnt 差 1（bf_cnt 每拍最多 +1）。
    //   阈值必须跟着提前 1 拍，否则 FIFO 峰值会从 BF-2 涨到 BF-1（余量只剩 0，
    //   一旦哪天 AXIS 反压久一点就静默溢出）。核算见 §7.0 末尾。
    localparam [BF_CW-1:0]  BF_PRESS = (BF > 5) ? (BF - 4) : 1;
    localparam [DF_W-1:0]   DF_LAST  = DF - 1;
    localparam [DF_CW-1:0]  DF_Z     = {(DF_CW){1'b0}};
    localparam [DF_CW-1:0]  DF_PRESS = (DF > 5) ? (DF - 4) : 1;

    localparam [31:0] CIN_LIM  = CIN_MAX;
    localparam [31:0] COUT_LIM = COUT_MAX;
    localparam [31:0] HW_LIM   = HW_MAX;
    localparam [31:0] WB_LIM   = WBANK_DEPTH;

    // 错误码（AP_RETURN[15:8]）
    localparam [7:0] ERR_RANGE = 8'd1,
                     ERR_TAP   = 8'd2,
                     ERR_WCAP  = 8'd3,
                     ERR_ALIGN = 8'd4,
                     ERR_ADDR  = 8'd5;

    // 写侧小状态机编码
    localparam [1:0] W_IDLE = 2'd0, W_DR1 = 2'd1, W_DR2 = 2'd2;
    localparam [2:0] AX_IDLE = 3'd0, AX_AW = 3'd1, AX_W = 3'd2, AX_B = 3'd3;

    // 地址零扩展（避免"零宽复制"在不同工具上的差异）
    function automatic [AXI_ID_W-1:0] zx_id(input [ENT_W-1:0] v);
        zx_id = '0;
        zx_id[ENT_W-1:0] = v;
    endfunction

    // 槽号回绕（NSLOT 不是 2 的幂，用比较而不是位截断）
    function automatic [SLOT_W-1:0] slot_nxt(input [SLOT_W-1:0] s);
        slot_nxt = (s == NSLOT_V - 1'b1) ? SLOT_Z : (s + 1'b1);
    endfunction

    // ========================================================================
    // 1. 信号声明（集中在此，避免依赖声明顺序）
    // ========================================================================
    // ---- 配置寄存器堆 ----
    logic [31:0] r_ctrl;
    logic [31:0] r_h, r_w, r_cin, r_cout, r_k, r_s, r_d, r_pad, r_shift, r_relu, r_tap;
    logic [31:0] r_in, r_wgt, r_bias, r_out;
    logic [3:0]  r_hi_bad;

    // ---- 运行控制 ----
    logic        start_pend, start_req, start_fire, run_active;
    logic        unit_done_q, done_sticky;
    logic [7:0]  err_code;
    logic        u_done, u_busy;
    logic [7:0]  vcode;
    // ⚠ vcode 是一整条配置校验优先级链，而 start_req/start_fire/run_active
    //   的时钟使能挂在 `vcode == 0` 上 ⇒ 综合后出现 `r_k_reg → run_active_reg/D` 这类
    //   "与数据通路无关"的最差路径（二值单元那边实测 −5.338 ns）。
    //   两刀切断（2026-10-30，正文见 §3）：
    //     ① vcode_q <= vcode   —— 切断 vcode → 启动使能（本来就有）；
    //     ② vcode_lo_q         —— 把这 5 级链拆成 3 级（前缀 + ERR_TAP）+ 2 级（WCAP/ALIGN），
    //                             第二段变成 vcode = f(vcode_lo_q, r_*)。
    //   配置只经 AXI-Lite 串行写入（do_wr 要求 !bvalid ⇒ 相邻两笔写至少隔 4 拍；
    //   启动闸门还要等 rd_quiet，所以 start 那一拍读到的 vcode_q 一定反映最后一次
    //   配置写完的状态（逐拍推导见 §3 的注释）。
    logic [7:0]  vcode_q;
    // ---- vcode 两段化的中间寄存器（§3）----
    logic [7:0]  vcode_lo;      // 第一段：ADDR / RANGE / TAP
    logic [7:0]  vcode_lo_q;    // 第一段结果，每拍刷新（与 vcode→vcode_q 同类）
    // ---- §3 的叶子项：k² 也打一拍（否则 k²→ncg·k² 的两级乘法落在启动闸门上）----
    logic [5:0]  kk_q;

    // ---- 引擎内存口 ----
    logic        u_rd_en;
    logic [31:0] u_rd_addr;
    logic [63:0] u_rd_data;
    logic        u_wr_en;
    logic [31:0] u_wr_addr;
    logic [7:0]  u_wr_data;

    // ---- AXI4-Lite 从 ----
    logic        aw_done, w_done, bvalid;
    logic [31:0] awaddr_l, wdata_l;
    logic [3:0]  wstrb_l;
    logic [31:0] araddr_l;
    logic        rvalid;

    // ---- 时钟门控 ----
    logic        u_ce;
    logic        u_clk;

    // ---- 读侧：地址派生 / CAM ----
    logic [28:0] rd_word;
    logic [IDX_W-1:0] rd_slot;
    logic [IDX_W:0]   rd_slot_n;
    logic [TAG_W-1:0] rd_tag;
    logic [2:0]       rd_off;
    logic [31:0]      rd_line_baddr;
    logic             rd_want, rd_hit, rd_miss, rd_known;
    logic [NLINES-1:0] cam_rdy, cam_knw, cam_pf;
    logic [ENT_W-1:0] rd_ent, pf_ent;
    logic [63:0]      rd_w0, rd_w1;
    logic [127:0]     rd_pair, rd_pair_sh;
    logic [6:0]       rd_sh;
    logic [63:0]      rd_comb;
    logic [CADW-1:0]  rd_idx0, rd_idx1;
    logic             rd_live;                 // 本拍读地址"有效"（= 旧 u_rd_data 的写使能）
    logic [63:0]      cache_a_q, cache_b_q;    // BRAM 输出寄存器（t 给地址 / t+1 出数据）
    logic             rd_off2_q;                // 与数据同拍对齐的桶形移位选择位

    // ---- 读侧：缓存存储（两份 = 两个同步读口，各进一块 RAMB36 512×64）----
    //   2026-10-30: 原来是 ram_style="distributed" + 组合读，288:1 的 64 bit mux
    //   被综合成寄存器阵列（Synth 8-4767 "Trying to implement RAM ... in registers"）
    //   ⇒ 实测 ~9000~10000 LUT + 36864 FF。改成 block RAM + 同步读后这两块
    //   变成 2 × RAMB36，mux 消失。
    (* ram_style = "block" *) logic [63:0] cache_a [0:DEPTH-1];
    (* ram_style = "block" *) logic [63:0] cache_b [0:DEPTH-1];

    // ---- 读侧：条目状态 ----
    logic [TAG_W-1:0]  ent_tag   [0:NLINES-1];
    logic [NLINES-1:0] ent_on, ent_rdy;
    logic [SLOT_W-1:0] ent_wslot [0:NLINES-1];   // 下个 R 落哪个槽
    logic [SLOT_W-1:0] ent_out   [0:NLINES-1];   // 该条目未回拍数
    logic [ENT_W-1:0]  alloc_ptr;

    // ---- 读侧：填充队列 ----
    logic [TAG_W-1:0]  fq_tag  [0:NFQ-1];
    logic [ENT_W-1:0]  fq_ent  [0:NFQ-1];
    logic [SLOT_W-1:0] fq_slot [0:NFQ-1];
    logic [SLOT_W-1:0] fq_rem  [0:NFQ-1];
    logic [FQ_W-1:0]   fq_wp, fq_rp;
    logic [FQ_CW-1:0]  fq_cnt;

    // ---- 读侧：纵向下探预取 ----
    logic             pf_act;
    logic [3:0]       pf_p;                 // 当前预取到第几个平面
    logic [2:0]       pf_r;                 // 该平面内第几行（相对需求行）
    logic [6:0]       pf_left;              // 本窗口还剩几条要压
    logic [6:0]       pf_cap_q;             // 启动锁存：本窗要压几条（NCG*k-1，截到 PF_MAX）
    logic [6:0]       pf_cap_c;             // 组合：同上（只在 start 那一拍用）
    logic [31:0]      pf_addr_q;            // 当前预取行的字节地址
    logic [31:0]      pf_plane_addr_q;      // 当前平面的行基址
    logic [SLOT_W-1:0] pf_slot_q;
    logic [31:0]      pw8_q;        // 启动锁存：d*W*8（下一 tap 行的字节步长）
    logic [31:0]      plane_q;      // 启动锁存：H*W*8（平面步长）
    logic [31:0]      inend_q;      // 启动锁存：输入缓冲区末端（预取不得越界）
    logic [15:0]      k_q;          // 启动锁存：cfg_k
    logic [15:0]      ncg_q;        // 启动锁存：ceil(cin/8)（平面数）
    logic [31:0]      dw8_c, inend_c, plane_c;

    // ---- 读侧：仲裁/命中 ----
    logic [ENT_W-1:0] rid_ent;
    logic             rid_hi_bad;
    logic             alloc_ok, fq_room, dem_alloc, alloc_en;
    logic [TAG_W-1:0] alloc_tag;
    logic [SLOT_W-1:0] alloc_start;
    // ---- 分配流水（§6.0 拆拍）：观测 → 决策 → 落盘 ----
    logic              rd_need_w, rd_need_q;   // 需求"缺失且未在缓存"（打一拍的观察）
    logic [TAG_W-1:0]  rd_tag_q;               // 与 rd_need_q 同拍对齐的 tag
    logic [SLOT_W-1:0] rd_slot_q;              // 与 rd_need_q 同拍对齐的槽号
    logic              dem_acc_q;              // 本次 miss 的分配令牌已发（防重复分配）
    logic              al_v;                   // 落盘有效（= t-1 拍选中的那条）
    logic [ENT_W-1:0]  al_ptr;                 // 落盘条目号（决策拍锁存的 alloc_ptr 旧值）
    logic [TAG_W-1:0]  al_tag;                 // 落盘 tag
    logic [SLOT_W-1:0] al_slot;                // 落盘起始槽号
    logic              pend_dup_o, pend_dup_p; // 正在落盘的 tag 与需求/预取 tag 撞车
    logic             pf_in_range, pf_known;      // 拍A 的两个组合检查结果
    // ---- 预取三拍流水（2026-10-30 拆拍；预取纯投机 ⇒ 零延迟契约，见 §6.5a）----
    logic             pf_a_q;                     // 拍A：候选已接，等拍B 决策
    logic             pf_in_r_q;                  //   拍A 登记的越界检查（对候选是不变量）
    logic             pf_go_q;                    // 拍B：下拍发票（分配 + 入队 + 推进）
    logic             pf_drop_q;                  // 拍B：下拍只推进（跳过这条）
    logic             pf_act_now;                 // 拍C：本拍真的动 alloc_en/fq_*/指针
    logic             pf_adv_now;                 // 拍C：本拍推进窗口（发票 | 跳过）
    logic             pf_busy;                    // 流水非空 ⇒ 不许接新候选
    logic             pf_acc_w, pf_dec_ok, pf_dup_w;
    logic [31:0]      pf_addr_nx_q;               // 拍A 预登记的下一行地址
    logic [31:0]      pf_plane_nx_q;              // 拍A 预登记的下一平面基址
    logic [TAG_W-1:0] pf_tag;
    logic             ar_fire, r_fire, ar_hs, rr_same;
    logic             arvalid_r, ar_src_r;
    logic [31:0]      ar_addr_r;
    logic [AXI_ID_W-1:0] ar_id_r;
    logic [ENT_W-1:0] ar_ent_q;
    logic             inj_v;
    logic [TAG_W-1:0] inj_tag;
    logic [ENT_W-1:0] inj_ent;
    logic [SLOT_W-1:0] inj_slot, inj_rem;
    logic             any_out, rd_quiet;
    logic [TAG_W-1:0]  fq_tag_h;
    logic [ENT_W-1:0]  fq_ent_h;
    logic [SLOT_W-1:0] fq_slot_h, fq_rem_h;
    logic [CADW-1:0]   rd_wr_idx;

    // ---- 写侧 ----
    logic        be_open;
    logic [28:0] be_word;
    logic [63:0] be_data;
    logic [7:0]  be_strb;
    logic [63:0] bf_data [0:BF-1];
    logic [7:0]  bf_strb [0:BF-1];
    logic [BF_W-1:0] bf_wp, bf_rp;
    logic [BF_CW-1:0] bf_cnt;
    logic [31:0] df_addr [0:DF-1];
    logic [8:0]  df_len  [0:DF-1];
    logic [DF_W-1:0] df_wp, df_rp;
    logic [DF_CW-1:0] df_cnt;
    logic [28:0] bu_base;
    logic [8:0]  bu_len;
    logic [28:0] bu_next;        // = bu_base + bu_len 的**寄存器**副本（见 §7.0）
    logic [1:0]  wst;
    logic        wr_take, wr_byte, wr_fifo_press, drain_cond;
    logic        merge_done, merge_change, drain_pub, push_now, push_chain;
    logic        desc_push, bf_pop, desc_pop, ax_can_start;
    // ---- 写口入口寄存器（1 拍）：把"吞字节"和"合并/记账"切成两级流水 ----
    logic        p_valid;        // 入口寄存器里有一个已吞下、尚未合并的字节
    logic [31:0] p_addr;         // 该字节的字节地址（w_lane/w_word 由它派生）
    logic [63:0] p_data;         // 该字节的数据
    logic        p_merge;        // 本拍把入口寄存器里的字节并进 be_*
    logic [2:0]  w_lane;
    logic [28:0] w_word;
    logic [7:0]  be_strb_nx;
    logic        be_same_word;
    logic        wr_all_idle;

    // ---- 写引擎 ----
    logic [2:0]  axst;
    logic [31:0] ax_addr;
    logic [8:0]  ax_len, ax_sent;
    logic        ax_awvalid, ax_wvalid, ax_bready;

    // ---- 调试 ----
    logic [15:0] cnt_rdreq, cnt_fill, cnt_ar, cnt_rbeat, cnt_burst, cnt_wbeat;
    logic        flg_rid_bad, flg_4k;
    logic        clr_cnt_rd, clr_cnt_wr, clr_flg;

    // ========================================================================
    // 2. AXI4-Lite 写通道 + 寄存器堆（结构照抄 k1_axi_wrap）
    // ========================================================================
    wire aw_hs = s_axi_awvalid && s_axi_awready;
    wire w_hs  = s_axi_wvalid  && s_axi_wready;

    assign s_axi_awready = !aw_done && !bvalid;
    assign s_axi_wready  = !w_done  && !bvalid;

    // ★ P7（任务③）：配置写**只在两个通道都已锁存**的那一拍执行。
    //
    //   旧版 do_wr = (aw_done||aw_hs) && (w_done||w_hs) && !bvalid，
    //   在"AW 与 W 同拍到达"的那一拍会用**组合的** s_axi_awaddr/s_axi_wdata 做译码：
    //     s_axi_awaddr(来自 AXI 互连的 payload 寄存器) → wr_off(8 bit) → case 译码
    //     → do_wr → r_cin/r_w/r_bias 的 CE、r_hi_bad 的 D
    //   实测这条链 11~13 级（−0.766/−0.606/−0.296），而且它只是**一笔配置写**
    //   的首拍，完全不需要在当拍落盘。
    //
    //   改成只认 aw_done && w_done 之后：
    //     · wr_addr/wr_data/wr_strb 一律取**本模块自己的锁存寄存器**（FF 输出），
    //       组合输入 s_axi_awaddr/s_axi_wdata/s_axi_wstrb 只连到 awaddr_l/wdata_l/wstrb_l
    //       的 D 端 ⇒ 那条深锥被 awaddr_l 这级寄存器切断；
    //     · 代价：每笔 AXI-Lite 配置写多 1 拍（AW/W 同拍握手时由"当拍落盘"变成"次拍落盘"）。
    //       这是纯延迟增加，AXI-Lite 无延迟契约；`aw_done/w_done` 在 do_wr 之前保持 1，
    //       awready/wready 保持 0，不会重复接收。对启动序列只会**加大**余量
    //       （§3 的 W→start 窗口分析里两笔写之间多 1 拍）。
    wire do_wr = aw_done && w_done && !bvalid;

    wire [31:0] wr_addr = awaddr_l;
    wire [31:0] wr_data = wdata_l;
    wire [3:0]  wr_strb = wstrb_l;
    wire        wr_page = wr_addr[16];      // 1=配置页 0=基址页
    wire [7:0]  wr_off  = wr_addr[7:0];

    function automatic [31:0] bmerge(input [31:0] old, input [31:0] nw, input [3:0] stb);
        bmerge[7:0]   = stb[0] ? nw[7:0]   : old[7:0];
        bmerge[15:8]  = stb[1] ? nw[15:8]  : old[15:8];
        bmerge[23:16] = stb[2] ? nw[23:16] : old[23:16];
        bmerge[31:24] = stb[3] ? nw[31:24] : old[31:24];
    endfunction

    // 高字槽 → r_hi_bad 位（0 表示"这个偏移不是高字槽"）
    function automatic [3:0] hi_bit_of(input [7:0] off);
        case (off)
            8'h14: hi_bit_of = 4'h1;   // B_IN   hi
            8'h20: hi_bit_of = 4'h2;   // B_WGT  hi
            8'h2C: hi_bit_of = 4'h4;   // B_BIAS hi
            8'h38: hi_bit_of = 4'h8;   // B_OUT  hi
            default: hi_bit_of = 4'h0;
        endcase
    endfunction

    // ---- 启动锁存的派生量（只在 start 那一拍算一次）----
    wire [31:0] w8_c   = {16'd0, r_w[15:0]} << 3;                       // W*8
    // d*W*8，d ≤ 3（已校验）：用加/移位代替乘法
    wire [31:0] dw8_c_ = (r_d == 32'd1) ? w8_c :
                         (r_d == 32'd2) ? (w8_c << 1) :
                                          (w8_c + (w8_c << 1));
    // 输入缓冲区字节数 = ceil8(cin) * H*W*8 = ncg * H*W * 8
    //   ⚠ 原来写成 `<< 6`（×64）——那是 8 倍偏大。地址单位是**字节**（见 plane_q = H*W*8、
    //     pf_addr_nx = pf_addr_q + pw8_q），一个 8 通道组只占 H*W*8 字节，所以
    //     正确是 `<< 3`。偏大的后果是 `pf_in_range`（:1115）放行 8 倍的越界预取范围，
    //     即预取可能读到输入缓冲区之外（别的层的数据或未分配 DDR）。
    //     这正是 conv_int8_uni.v:29 注释"C8 布局 cp8=ceil8(cin) 个通道"的语义。
    wire [15:0] ncg_c16 = (r_cin[15:0] + 16'd7) >> 3;
    // ⚠ H,W ≤ 256（9 bit）⇒ H*W ≤ 65536（17 bit），原来写成 16×16 乘法（约 200 LUT）。
    //   收窄成 9×9（约 40 LUT）；inb = ncg(5b) × hw(17b) ≤ 2.02M（21 bit）。
    //   ×8（不是 ×64）：地址单位是**字节**，一个 8 通道组只占 H*W*8 字节。
    //
    // ⚠⚠ 2026-10-30（inend 三级化）：这两条乘法原来是**同一拍**里的两片级联 DSP：
    //      FDCE 0.397 + DSP48E1 3.367 (A[8]→P[16]) + DSP48E1 3.230 (A[16]→P[0])
    //      + LUT2 0.105 + CARRY4×7 1.213 ⇒ logic 8.312 / data 10.093，slack −3.534。
    //      两个 DSP 的 P→A 直连（中间没有任何寄存器）合计 6.6 ns，已经吃掉 6.667 ns
    //      周期的 99%（剩下的 FDCE + LUT2 + 7 级 CARRY4 根本无处安放）
    //      ⇒ 这条**必然**违例，靠布线/floorplan 救不回来。
    //   为什么可以白拿：inend_q / plane_q 只在 start_req **那一拍**锁存（见下方 `if (start_req)`），
    //      整跑不变；引擎启动前有完整 AXI-Lite 配置序列 + rd_quiet 等待（≥ 数拍），
    //      没有任何延迟契约 ⇒ 把两片 DSP 拆到相邻两拍，代价 0。
    //   拆法（每拍只有一级乘法，DSP 级联 P→A 消失）：
    //      拍1：hw_q  <= r_h[8:0] * r_w[8:0];              // DSP#1（9×9 → 17 bit）
    //      拍2：inb_q <= {11'd0,ncg_c16[4:0]} * hw_q;      // DSP#2（5×17 → 值 ≤21 bit，用寄存后的 hw_q）
    //      拍3：inend_q <= r_in + (inb_q << 3);            // 32 位加法 + 左移 3 = 纯布线
    //   ⚠ inb_q 存的是**未左移**的 ncg*H*W，`<<3` 留到拍3 与加法同拍做（移位只是改
    //     位宽/连线，0 个 LUT），这样中间那级既不用加宽位宽也不丢语义。
    logic [16:0] hw_q;      // 拍1：H*W（DSP#1 的输出寄存器）
    logic [21:0] inb_q;     // 拍2：ncg*H*W（DSP#2 的输出寄存器，未左移）
    // 这两个寄存器**故意无复位**：加异步复位后 Vivado 会把乘法的 P 寄存器赶出
    // DSP48（退化成 fabric FF + 复位网络）。语义上也不需要复位 —— 唯一的消费者
    // 是 start_req 那一拍锁存的 plane_q / inend_q（都有复位），而 start_req 最早
    // 发生在"复位后第 2 拍 + 一次完整 AXI-Lite 配置序列"之后：本模块的 Lite 写
    // FSM 是**单笔串行**的（do_wr 要求 !bvalid），所以最后一条 r_h/r_w/r_cin
    // 写入与 ctrl 写入之间 ≥2 拍，ctrl 写入到 start_req 再 ≥2 拍（start_pend 寄存，
    // 且还要等 rd_quiet）⇒ 窗口 ≥4 拍 > 2 拍流水深度。
    always_ff @(posedge s_axi_aclk) begin
        hw_q  <= r_h[8:0] * r_w[8:0];                    // 拍1（DSP#1）
        inb_q <= {11'd0, ncg_c16[4:0]} * hw_q;           // 拍2（DSP#2，输入是寄存后的 hw_q）
    end

    assign dw8_c   = dw8_c_;
    // ③ hw_c / inb_c 的消费者清单（逐一核对过，只有下面这些，全部同步改用寄存后的版本）：
    //      hw_c  →  inb_c、plane_c                      （2 个消费者）
    //      inb_c →  inend_c                             （1 个）
    //      plane_c → plane_q、inend_c → inend_q         （各 1 个，都只在 start_req 那一拍锁存）
    //    ⇒ 删掉 hw_c / inb_c 这两个组合信号是安全的：不存在"别的拍还要看组合版"的
    //      第三处消费者，也就没有"plane 走了 1 拍、inend 走了 2 拍"这种不一致。
    //      （plane 与 inend 必须同源：前者是平面步长、后者是缓冲区上界，两者都只
    //       由 H*W 决定，用同一个 hw_q 就保证了同一次锁存看到同一对 H/W。）
    assign plane_c = {hw_q, 3'b000};            // H*W*8（一个 C8 平面的字节数）= hw_q << 3
    assign inend_c = r_in + {inb_q, 3'b000};    // r_in + ncg*H*W*8（×8 不是 ×64）

    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) begin
            aw_done <= 1'b0; w_done <= 1'b0; bvalid <= 1'b0;
            awaddr_l <= 32'd0; wdata_l <= 32'd0; wstrb_l <= 4'd0;

            r_ctrl <= 32'd0;
            r_h <= 32'd0; r_w <= 32'd0;
            r_cin <= 32'd0; r_cout <= 32'd0;
            r_k <= 32'd0; r_s <= 32'd0; r_d <= 32'd0; r_pad <= 32'd0;
            r_shift <= 32'd0; r_relu <= 32'd0; r_tap <= 32'd0;
            r_in <= 32'd0; r_wgt <= 32'd0; r_bias <= 32'd0; r_out <= 32'd0;
            r_hi_bad <= 4'd0;

            start_req <= 1'b0; start_fire <= 1'b0;
            start_pend <= 1'b0; run_active <= 1'b0; vcode_q <= 8'd0;
            unit_done_q <= 1'b0; done_sticky <= 1'b0; err_code <= 8'd0;

            k_q <= 16'd0; ncg_q <= 16'd0;
            pw8_q <= 32'd0; plane_q <= 32'd0; inend_q <= 32'd0;
            pf_cap_q <= 7'd0;

            clr_cnt_rd <= 1'b0; clr_cnt_wr <= 1'b0; clr_flg <= 1'b0;
        end else begin
            start_req  <= 1'b0;             // start 只维持 1 拍
            start_fire <= 1'b0;
            clr_cnt_rd <= 1'b0;
            clr_cnt_wr <= 1'b0;
            clr_flg    <= 1'b0;

            if (aw_hs) begin awaddr_l <= s_axi_awaddr; aw_done <= 1'b1; end
            if (w_hs)  begin wdata_l <= s_axi_wdata; wstrb_l <= s_axi_wstrb; w_done <= 1'b1; end

            if (do_wr) begin
                aw_done <= 1'b0;
                w_done  <= 1'b0;
                bvalid  <= 1'b1;

                if (wr_page) begin
                    // ---------------- 配置页 ----------------
                    case (wr_off)
                    8'h00: begin
                        r_ctrl <= bmerge(r_ctrl, wr_data, wr_strb);
                        if (wr_strb[0] && wr_data[0] && !run_active) begin
                            // 只登记"要启动"：真正的 start_req 在读线静默后才发，
                            // 否则上一跑还在飞的预取数据会落到新一跑的条目里。
                            start_pend  <= 1'b1;
                            done_sticky <= 1'b0;
                            err_code    <= 8'd0;
                        end
                    end
                    8'h18: r_h     <= bmerge(r_h,     wr_data, wr_strb);
                    8'h20: r_w     <= bmerge(r_w,     wr_data, wr_strb);
                    8'h38: r_cin   <= bmerge(r_cin,   wr_data, wr_strb);
                    8'h40: r_cout  <= bmerge(r_cout,  wr_data, wr_strb);
                    8'h48: r_k     <= bmerge(r_k,     wr_data, wr_strb);
                    8'h50: r_s     <= bmerge(r_s,     wr_data, wr_strb);
                    8'h58: r_d     <= bmerge(r_d,     wr_data, wr_strb);
                    8'h60: r_pad   <= bmerge(r_pad,   wr_data, wr_strb);
                    8'h68: r_shift <= bmerge(r_shift, wr_data, wr_strb);
                    8'h70: r_relu  <= bmerge(r_relu,  wr_data, wr_strb);
                    8'h74: r_tap   <= bmerge(r_tap,   wr_data, wr_strb);
                    8'hE0: begin clr_cnt_rd <= 1'b1; clr_cnt_wr <= 1'b1; end
                    8'hE8: clr_cnt_wr <= 1'b1;
                    8'hF0: if (wr_strb[0]) clr_flg <= 1'b1;
                    default: ;
                    endcase
                end else begin
                    // ---------------- 基址页 ----------------
                    // ⚠ 高字登记只能放这一页（见文件头 §3 的警告）
                    if (hi_bit_of(wr_off) != 4'd0) begin
                        if (wr_data != 32'd0) r_hi_bad <= r_hi_bad |  hi_bit_of(wr_off);
                        else                  r_hi_bad <= r_hi_bad & ~hi_bit_of(wr_off);
                    end
                    case (wr_off)
                    8'h10: r_in   <= bmerge(r_in,   wr_data, wr_strb);
                    8'h1C: r_wgt  <= bmerge(r_wgt,  wr_data, wr_strb);
                    8'h28: r_bias <= bmerge(r_bias, wr_data, wr_strb);
                    8'h34: r_out  <= bmerge(r_out,  wr_data, wr_strb);
                    default: ;
                    endcase
                end
            end

            if (bvalid && s_axi_bready) bvalid <= 1'b0;

            // ---- 运行控制 ----
            if (u_done) unit_done_q <= 1'b1;
            vcode_q <= vcode;   // 切断 r_* → vcode → 启动使能 的长链

            // 启动闸门：等读线静默再发 start（保证新一跑不被迟到数据污染）
            if (start_pend && !run_active && rd_quiet) begin
                start_pend <= 1'b0;
                if (vcode_q == 8'd0) begin
                    start_req   <= 1'b1;
                    start_fire  <= 1'b1;
                    run_active  <= 1'b1;
                    unit_done_q <= 1'b0;
                    done_sticky <= 1'b0;
                    err_code    <= 8'd0;
                end else begin
                    done_sticky <= 1'b1;
                    err_code    <= vcode_q;
                end
            end

            // 启动那一拍锁存本跑要用的派生量（预取步长 / 缓冲区上界 / k）
            if (start_req) begin
                k_q     <= r_k[15:0];
                ncg_q   <= ncg_c16;
                pw8_q   <= dw8_c;
                plane_q <= plane_c;
                inend_q <= inend_c;
                pf_cap_q <= pf_cap_c;      // 本窗最多压几条（NCG*k-1，截到 PF_MAX）
            end

            if (run_active && unit_done_q && wr_all_idle) begin
                run_active  <= 1'b0;
                done_sticky <= 1'b1;
                if (r_ctrl[1]) begin                 // auto-restart
                    start_pend  <= 1'b1;
                    unit_done_q <= 1'b0;
                end
            end
        end
    end

    // ⚠ k1 当年漏驱动 s_axi_bvalid ⇒ 综合成常量 0 ⇒ PS 每笔 MMIO 写等不到 B
    //   响应 ⇒ 整机卡死。这里把所有 Lite 输出显式驱动。
    assign s_axi_bvalid = bvalid;
    assign s_axi_bresp  = 2'b00;

    // ========================================================================
    // 3. 启动前校验（顺序 = ADDR → RANGE → TAP → WCAP → ALIGN）
    // ------------------------------------------------------------------------
    //  ⚠ 2026-10-30 时序：这条 5 级 if/else-if 优先级链的项本身很深（TAP 要
    //    r_k→tap_expect 三级查表再比 r_tap；WCAP 要 ncg×k² 再比限值），而
    //    `vcode → vcode_q/D` 又是启动闸门的唯一判据 ⇒ 拆成两段，中间插 vcode_lo_q。
    //    **优先级顺序逐条不变**，只是切成两拍：
    //       3a  ADDR / RANGE      → vcode_lo → [vcode_lo_q]
    //       3b  TAP / WCAP / ALIGN → vcode    → [vcode_q] → 启动闸门
    //
    //    切点为什么在 RANGE 之后（而不是把 TAP 也放进第一段）：
    //      一条 if/else-if 链里，第 i 条的输出要穿过 i 个 2:1 mux 才到链尾
    //      ⇒ 某条的最大深度 ≈ 该项从 r_* 起的级数 + i。
    //        TAP 项 ≈ 6 级（r_k→tap_expect 3 + 32 位比较 2 + AND/OR 1）
    //        WCAP 项 ≈ 5 级（kk_q→乘 3 + 比较 2，k² 已打一拍）
    //        RANGE 项 ≈ 3 级
    //      若 TAP 放第一段（i=3）：6+3 = 9 级；放第二段（j=1）：6+1 = 7 级。
    //      而 WCAP 放第二段（j=2）：5+2 = 7 级、放第一段（i=4）：5+4 = 9 级。
    //      两种切法 WCAP 都是 7 或 9 ⇒ 最优是"RANGE 之后切"：两段都 ≤7 级
    //      （原来 5 级链最深是 WCAP 8+4 = 12 级，TAP 6+3 = 9 级）。
    //
    //  §3.1 迟到一拍为什么是安全的（与 REPORT_inendsplit.md §1、二值单元 §3 同一论证）
    //    W   = 最后一条被 vcode 读的配置写（r_k/r_tap/r_cin/... 的 do_wr 拍）；
    //    W+3 = 最早的下一条写 = ctrl 写 —— do_wr 要求 !bvalid（:572），bvalid 最早
    //          在 W+1 被 bready 清掉、aw_hs 在 W+2 才能成立、写才在 W+3 落盘
    //          ⇒ **相邻两笔写至少隔 3 拍**；
    //    ★ P7（任务③）把 do_wr 改成只认已锁存的 aw_done&&w_done ⇒ 每笔写再晚 1 拍落盘
    //      ⇒ **至少隔 4 拍**，下面所有"余量"结论只增不减（最坏余量从 1 拍变 2 拍）。
    //    W+4 = 闸门拍：ctrl 写在 W+3 落盘并置 start_pend（:701），闸门（:746）在同一个
    //          always_ff 里看不到同拍的 start_pend ⇒ 最早 W+4 取 vcode_q；
    //    而 vcode 在 W+1 就正确（vcode_lo_q 在 W+1 打，第二段组合在 W+1..W+2 算完，
    //    vcode_q 在 W+2 落盘、W+2..W+3 有效）⇒ **余量 1 拍**；实际 PS 侧每笔 MMIO 是
    //    AW+W+B 三次握手（≥4 拍）+ `rd_quiet`（:1289 要求预取队列空、无在飞读）
    //    ⇒ 实际余量 5~10 拍。
    // ========================================================================
    // ⚠ 原来是 'r_k * r_k'（32×32 乘法器，约 1000 LUT 或 4 个 DSP）再乘 ncg ——
    //   而且结果落在启动闸门的组合路径上，是一条"必挂"路径。
    //   k 只允许 {1,3,5} ⇒ k² ∈ {1,9,25}，直接查表（0 个乘法器、2 级 LUT）；
    //   cin ≤ 192 ⇒ ncg ≤ 24（5 bit）；wcap = ncg(5b) × kk(6b) ≤ 600（10 bit）。
    wire [5:0]  kk_c    = (r_k == 32'd1) ? 6'd1  :
                          (r_k == 32'd3) ? 6'd9  :
                          (r_k == 32'd5) ? 6'd25 : 6'd0;
    // ⚠ 叶子项：k²（3 路查表，r_k 起 3 级）→ ncg·k² 是**两级串联**。k² 只是 r_k 的
    //   纯函数 ⇒ 打一拍再进乘法，两级中间就有寄存器。它与 vcode_lo_q **同拍**，
    //   不额外增加启动前的配置陈旧度（k 在启动前早已稳定，同一前提）。
    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn)
        if (!s_axi_aresetn) kk_q <= 6'd0;
        else                kk_q <= kk_c;
    wire [4:0]  ncg_c5  = r_cin[7:0] >> 3;
    wire [9:0]  wcap_c  = ncg_c5 * kk_q[5:0];
    wire [31:0] ncg_c   = {27'd0, ncg_c5};

    // ⚠ TAP 已从 {4,12,28} 改为 8·ceil(k²/8) = {8,16,32}（2026-10-30 时序优化）。
    //   宿主侧（gen_uni_golden / uni_engine）已同步；这里不同步的话**所有合法配置都会
    //   在启动校验撞 ERR_TAP(2) 拒跑**（AP_RETURN=0x0201），7 个形状全 FAIL。
    //   旧值回退：16'd4 / 16'd12 / 16'd28。
    wire [15:0] tap_expect = (r_k == 32'd1) ? 16'd8  :
                             (r_k == 32'd3) ? 16'd16 :
                             (r_k == 32'd5) ? 16'd32 : 16'd0;

    wire tap_ok = (tap_expect != 16'd0) &&
                  (AUTO_TAP ? ((r_tap == 32'd0) || (r_tap == {16'd0, tap_expect}))
                            : (r_tap == {16'd0, tap_expect}));

    wire al_in   = (r_in[2:0]   == 3'd0);
    wire al_wgt  = (r_wgt[2:0]  == 3'd0);
    wire al_bias = (r_bias[2:0] == 3'd0);
    wire al_out  = (r_out[2:0]  == 3'd0);

    // ---- 3a. 第一段：ADDR / RANGE（原链第 1..2 条，一字未改）----
    always @* begin
        vcode_lo = 8'd0;
        if (r_hi_bad != 4'd0) begin
            vcode_lo = ERR_ADDR;
        end else if ((r_cin  == 32'd0) || (r_cin  > CIN_LIM)  ||
                     (r_cout == 32'd0) || (r_cout > COUT_LIM) ||
                     (r_h    == 32'd0) || (r_h    > HW_LIM)   ||
                     (r_w    == 32'd0) || (r_w    > HW_LIM)   ||
                     (r_s    == 32'd0) || (r_s    > 32'd2)    ||
                     (r_d    == 32'd0) || (r_d    > 32'd3)    ||
                     (r_pad  >  32'd8) ||
                     (r_shift > 32'd63)) begin
            vcode_lo = ERR_RANGE;
        end
    end

    // 第一段结果打一拍。**每拍刷新**：配置稳定时 vcode_lo 是常量 ⇒ vcode_lo_q 只比
    // 它晚 1 拍，对功能无影响（§3.1 的余量推导）。
    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn)
        if (!s_axi_aresetn) vcode_lo_q <= 8'd0;
        else                vcode_lo_q <= vcode_lo;

    // ---- 3b. 第二段：TAP / WCAP / ALIGN（原链第 3..5 条）----
    //   vcode 默认带上第一段的错码（第一段报错时原链就停在那个 else-if，TAP/WCAP/ALIGN
    //   根本不会再查 ⇒ 这里的 if (vcode_lo_q == 8'd0) 与之等价）；段内顺序与原链一致。
    always @* begin
        vcode = vcode_lo_q;
        if (vcode_lo_q == 8'd0) begin
            if (!tap_ok) begin
                vcode = ERR_TAP;
            end else if (wcap_c > WB_LIM) begin
                vcode = ERR_WCAP;
            end else if (!(al_in && al_wgt && al_bias && al_out)) begin
                vcode = ERR_ALIGN;
            end
        end
    end

    // ========================================================================
    // 4. 引擎例化（ap_clk 接门控时钟，内存口接 u_rd_*/u_wr_*）
    // ========================================================================
    conv_int8_uni u_unit (
        .ap_clk         (u_clk),
        .ap_rst_n       (s_axi_aresetn),
        .ctrl_start     (start_req),
        .ctrl_done      (u_done),
        .ctrl_busy      (u_busy),
        .cfg_cin        (r_cin[15:0]),
        .cfg_cout       (r_cout[15:0]),
        .cfg_k          (r_k[15:0]),
        .cfg_s          (r_s[15:0]),
        .cfg_d          (r_d[15:0]),
        .cfg_pad        (r_pad[15:0]),
        .cfg_h          (r_h[15:0]),
        .cfg_w          (r_w[15:0]),
        .cfg_tap_stride (tap_expect[7:0]),   // 合法时恒等于 R_TAP
        .cfg_shift      (r_shift[5:0]),
        .cfg_relu       (r_relu[0]),
        .base_in        (r_in),
        .base_wgt       (r_wgt),
        .base_bias      (r_bias),
        .base_out       (r_out),
        .mem_rd_en      (u_rd_en),
        .mem_rd_addr    (u_rd_addr),
        .mem_rd_data    (u_rd_data),
        .mem_wr_en      (u_wr_en),
        .mem_wr_addr    (u_wr_addr),
        .mem_wr_data    (u_wr_data)
    );

    // ========================================================================
    // 5. 读侧 §5.1 槽号 / 线号派生（全部算术派生 + 显式位宽，不手写位选）
    // ========================================================================
    //     word = addr >> 3            （29 位）
    //     slot = word[IDX_W-1:0]      （线内第几个 64-bit 字：0..RD_WIN_WORDS-1）
    //     tag  = word[28:IDX_W]       （第几条线 = 64 B 行号）
    //     off  = addr[2:0]            （8 字节窗口在字内的偏移；4 字节对齐读时 = 4）
    // ⚠ bin_unit_axi_wrap 2026-10-28 被 xsim 抓到的致命 bug 就是槽号写成
    //   addr[4:3]（少取最高位）⇒ 线内第 5..8 字别名到第 1..4 槽。本文件所有位宽
    //   都由 IDX_W/SLOT_W/ENT_W 决定，不出现手写常数位选。
    wire [28:0] rd_word_w    = {3'b000, u_rd_addr[31:3]};
    wire [IDX_W-1:0] rd_slot_w  = rd_word_w[IDX_W-1:0];
    wire [IDX_W:0]   rd_slot_nw = {1'b0, rd_slot_w} + 1'b1;   // ≤ RD_WIN_WORDS
    wire [TAG_W-1:0] rd_tag_w   = rd_word_w[28:IDX_W];
    wire [2:0]       rd_off_w   = u_rd_addr[2:0];
    wire [31:0]      rd_line_bw = {rd_tag_w, {(IDX_W+3){1'b0}}};

    assign rd_word       = rd_word_w;
    assign rd_slot       = rd_slot_w;
    assign rd_slot_n     = rd_slot_nw;
    assign rd_tag        = rd_tag_w;
    assign rd_off        = rd_off_w;
    assign rd_line_baddr = rd_line_bw;
    assign rd_want       = u_rd_en;

    // ---- 5.2 CAM：rdy（可服务）/ knw（已在缓存，含正在填）/ pf（预取去重）----
    integer ce, cj, ck, cr;   // 每个并发 always 块用**独立**的循环变量（共用一个会在仿真里打架）
    always @* begin
        for (ce = 0; ce < NLINES; ce = ce + 1) begin
            cam_rdy[ce] = ent_on[ce] && ent_rdy[ce] && (ent_tag[ce] == rd_tag_w);
            cam_knw[ce] = ent_on[ce]                 && (ent_tag[ce] == rd_tag_w);
            cam_pf[ce]  = ent_on[ce]                 && (ent_tag[ce] == pf_tag);
        end
    end
    assign rd_hit    = |cam_rdy;
    assign rd_known  = |cam_knw;
    assign pf_known  = |cam_pf;
    assign rd_miss   = rd_want && !rd_hit;

    // 命中条目号：**显式位或归约**，不是优先级链
    //   cam_rdy 是 one-hot（证明见 5.2b 的分配去重论证 + 下面的运行期断言），
    //   所以"把所有命中条目号按位或"== "取唯一那个条目号"。
    //   原来写成覆盖式（if (cam_rdy[cj]) rd_ent = cj;）时语义是"取最大条目号"，
    //   综合成 **32 级串联的 5 bit 2:1 mux 优先级链**（每级都要等上一级选定，
    //   位宽 5 ⇒ 每级吃 1 个 LUT 深度），直接压在这条路上：
    //       引擎地址 → tag 比较(26 bit ×32) → 优先级链(32 级) → {ent,slot} → BRAM ADDR
    //   位或则是**可交换归约**（与书写顺序无关），综合器会拆成 2 级 LUT6 树
    //   （每 bit 16 个 6 输入 OR）⇒ 地址路少 10~20 级。
    always @* begin
        rd_ent = {ENT_W{1'b0}};
        for (cj = 0; cj < NLINES; cj = cj + 1)
            if (cam_rdy[cj]) rd_ent = rd_ent | cj[ENT_W-1:0];   // 位或，可并行归约
    end

// synthesis translate_off
    // ---- 5.2b one-hot 断言（保护上面的位或归约，只影响仿真）------------------
    //   tag 唯一性的来源（结构论证，见 §6.0 / §6.4c）：分配器只有**一个写口**
    //   （ent_tag[al_ptr]/ent_on[al_ptr]，al_v 单路），且两条分配支路
    //   都先把"这个 tag 是否已在缓存 / 是否正在落盘"判过：
    //      需求：dem_alloc = rd_need_q && !dem_acc_q && alloc_ok && fq_room
    //                        (rd_need_q 来自 rd_want && !(rd_known || pend_dup_o))
    //      预取：pf_act_now = pf_go_q && !dem_alloc && !pend_dup_p && !start_fire
    //                        (pf_go_q 来自拍B：pf_a_q && !dem_alloc && !pf_dup_w
    //                         && alloc_ok && fq_room；pf_dup_w 含 cam_pf 同判据。
    //                         ⇒ 预取也只在"② 发票"那一拍动写口，与需求逐拍互斥)
    //   ent_tag 与 ent_on 在**同一条语句组、同一个沿**写入（不存在"tag 写了、on 没写"
    //   的中间拍），start_fire 冲刷只清 ent_on/ent_rdy、留 ent_tag 垃圾，但垃圾条目
    //   ent_on=0 ⇒ 不参与 cam_rdy。⇒ ent_on=1 的条目 tag 两两不同 ⇒ cam_rdy ≤ 1 路。
    //   多路命中只可能来自上两者以外的 bug，此时位或会把 rd_ent 指到"没命中的
    //   条目"上、读回静默错数据 ⇒ 必须当场报错，不能让它悄悄过去。
    //   ⚠ 2026-10-30 分配拆拍后，这里额外有"落盘去重"断言（§9 的 dup_al）守着同一
    //     件事：如果拆拍引入的同 tag 二次分配漏网，dup_al 会先报，不必等 cam_rdy 变两路。
    integer oh_i, oh_n;
    always @(posedge s_axi_aclk) begin
        if (s_axi_aresetn) begin
            oh_n = 0;
            for (oh_i = 0; oh_i < NLINES; oh_i = oh_i + 1)
                if (cam_rdy[oh_i]) oh_n = oh_n + 1;
            if (oh_n > 1)
                $display("[CONV-UNI-WRAP] ERROR: cam_rdy 非 one-hot（%0d 路同时命中）tag=%h rd_ent=%h —— rd_ent 位或归约失效",
                         oh_n, rd_tag_w, rd_ent);
        end
    end
// synthesis translate_on

    // ---- 5.3 两个同步读口：slot 与 slot+1（跨字读用）----
    //   槽步长 = SLOT_STRIDE = 16（2 的幂）⇒ 索引是纯拼接，没有 *9 的加法器；
    //   ⚠ rd_slot 只有 IDX_W 位（0..7），必须补 {(SLOT_W-IDX_W){1'b0}} 才能落到
    //     16 的步长上，否则 {rd_ent, rd_slot} 会退化成 8 的步长、和写口错位。
    // ---- 5.3b 线缓存的**无复位写口**（BRAM 必须这样写）----
    //   8-4767 的判据：RAM 只要碰异步复位就无法推断成 BRAM。
    always_ff @(posedge s_axi_aclk) begin
        if (r_fire) begin
            cache_a[rd_wr_idx] <= m_axi_rdata;
            cache_b[rd_wr_idx] <= m_axi_rdata;
        end
    end

    assign rd_idx0 = {rd_ent, {(SLOT_W-IDX_W){1'b0}}, rd_slot};
    assign rd_idx1 = {rd_ent, rd_slot_n};            // rd_slot_n 已是 SLOT_W 位（0..8）
    assign rd_w0   = cache_a_q;
    assign rd_w1   = cache_b_q;
    // BRAM 同步读（地址在本拍、数据在下拍）：输出寄存器必须带**与旧 u_rd_data
    // 逐位相同的写使能**，否则会破坏三种"地址动了但数据不该动"的场景：
    //   ① 引擎冻结（rd_want && !rd_hit）：地址仍是未命中的那个，rd_ent 兜底成 0，
    //      组合读会读到 0 号条目的垃圾；旧逻辑是"保持上一拍数据"。
    //   ② 偏置/权重装载的收数拍（bia_ph=1 / wl_ph=1）：mem_rd_en=0，mem_rd_addr
    //      已经切到"下一次读"甚至 compute 地址，而引擎正在用上一次读的数据。
    //   ③ 写口压力冻结（u_ce=0 但 rd_hit=1）。
    // ---- u_ce 的两个"非冻结"因子，单独取名 ⇒ rd_live 与 u_ce 共享（少一级）----
    //   u_ce = R || (P && W)   （P/W 的完整表达式见 §7 的 u_ce，这里把它拆出来）
    //     R = !s_axi_aresetn || !run_active        ← u_ce_free（复位期/空闲期恒 1）
    //     W = !(wr_take && wr_fifo_press) && !((wst != W_IDLE) && wr_take)  ← u_ce_wr_ok
    wire u_ce_free  = (!s_axi_aresetn) || (!run_active);
    wire u_ce_wr_ok = !(wr_take && wr_fifo_press)
                      && !((wst != W_IDLE) && wr_take);
    // ✅ 2026-10-30 落地 P4 等价化简（论证见下面 ⚠⚠ 段末 + 报告）：
    //     u_ce && rd_want && rd_hit  ≡  (rd_want && rd_hit) && (R || W)
    //   证明：① rd_want&&rd_hit=1 ⇒ P=(!rd_want||rd_hit)≡1 ⇒ R||(1&&W)=R||W，
    //            两边都等于 R||W；
    //         ② rd_want&&rd_hit=0 ⇒ 左边 = u_ce&&0 = 0，右边 = 0。
    //   逐值恒等（不是"仿真上碰巧"），所以不改变任何一拍的行为。
    //   深度：rd_hit→rd_live 从 4 级（P→&&W→||R→&&rd_hit…）降到 2 级
    //         （(&&rd_hit) → (&&(R||W))），且 rd_hit 对 CE 的两处扇出合成一次。
    assign rd_live = (rd_want && rd_hit) && (u_ce_free || u_ce_wr_ok);   // = 旧 u_rd_data 的写使能
    // ⚠⚠ 不要把上面这行改成 `1'b1`（2026-10-30 的 P1 提案）—— 已论证为**功能错误**，
    //   即使它能把这 192 条 CE 路径全部消掉（BRAM REGCE=1）。
    //   根因：引擎的**权重**走 u_clk（conv_int8_uni.v:518 wqr）、**输入**走本寄存器
    //   （s_axi_aclk，永不停），于是一旦 u_ce=0：
    //       u_clk 停振 ⇒ wqr 保持、acc 不更新（消费被推迟）；
    //       本寄存器若 CE=1 ⇒ 会在冻结期间持续装载 f(addr) = 那个**未命中**的地址。
    //   解除冻结的第一个 u_ce=1 的沿，acc 用的是"本拍"的 u_rd_data，而它在语义上
    //   必须是**冻结前最后一次 u_ce=1 那一拍地址**的数据 f(A_{t-1})（那一读早在
    //   t-1 拍发过，只差消费；消费沿被冻结压住了）。CE=1 会把 f(A_t)（未命中地址
    //   的垃圾/刚填进来的新线）灌进去 ⇒ u_x=f(A_t) 与 u_w=w(t-1) 跨拍错配；
    //   之后流水线又自动对齐 ⇒ **每个线边界静默算错一个 term**。
    //   而"命中冻结"正好发生在 ST_CMP 的**发读拍**（rd_cmp 每拍都发读），上一条
    //   读的消费沿被 u_ce 压住 ⇒ 一定存在待消费数据，这不是理论边角，是常态。
    //   要消掉这条路只有两条正路：P3（u_ce 改同步 CE、删掉 u_clk 那套网络，
    //   顺带回收 1.51 ns skew）或让引擎读口改成 valid/ready 握手（读延迟 2 拍）。
    //   唯一可证明等价的缩短写法（2026-10-30 **已落地**，见上面 rd_live 一带）：
    //       assign rd_live = (rd_want && rd_hit) && (R || W);
    //   R = !s_axi_aresetn || !run_active，W = !(wr_take&&wr_fifo_press)
    //                                              && !((wst!=W_IDLE)&&wr_take)
    //   （因为 rd_want&&rd_hit=1 时 u_ce 内那个 (!rd_want||rd_hit) 恒 1，可消去，
    //     只剩 R || W；能省 2 级、且把 rd_hit 对 CE 的两次扇出合成一次。）

    always_ff @(posedge s_axi_aclk) begin
        if (rd_live) begin
            cache_a_q <= cache_a[rd_idx0];           // t 拍给地址
            cache_b_q <= cache_b[rd_idx1];           // t+1 拍数据有效
        end
    end

    // rd_off[2]（= addr[2]）也必须跟数据一起打一拍：桶形移位是放在 BRAM 输出
    // **之后**的组合 mux 上，若用当前拍的 rd_off 去选上一拍的数据，跨字读会选错。
    // ⚠ rd_off2_q **必须有复位**：它是 rd_comb 的 mux 选择位，复位后到首次 live 读之间
    //   若为 X，'X ? a : b' 会把整条读数据通路染成 X（实测 512 字节里 383 字节错）。
    //   只复位这 1 个 FF 就够；cache_a_q/cache_b_q 保持无复位，以免把 BRAM 输出寄存器
    //   顶出 RAMB36（那会退化成 2 拍延迟并破坏时序契约）。
    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) rd_off2_q <= 1'b0;
        else if (rd_live)   rd_off2_q <= rd_off[2];
    end

    // ⚠ 原来这里是 128 bit × 8 粒度**通用桶形移位器**（约 190 LUT、3 级延迟）。
    //   但 rd_off 只能取 {0,4}（引擎只会 8 字节对齐读：
    //     bias 读 bbase+{0,8}；weight 读 wbase+wl_rb+wl_j0，TAP∈{4,12,28}、j0∈{0,8,16}；
    //     compute 偏移全是 8 的倍数 ⇒ u_rd_addr[2:0] ∈ {0,4}），
    //   所以只需要"移 0 或移 32 位"，即 64 个 2:1 mux（1 级、约 64 LUT）。
    //   128 bit 右移 32 后取低 64 位 = {rd_w1[31:0], rd_w0[63:32]}（逐字节核对过）。
    //   选择位用 rd_off2_q（与 cache_a_q/cache_b_q 同拍登记）。
    assign rd_comb = rd_off2_q ? {rd_w1[31:0], rd_w0[63:32]}
                               : rd_w0;

// synthesis translate_off
    // 把上面的不变量钉死：一旦以后有人改成非 8 字节对齐读，这里立刻报错。
    always @(posedge s_axi_aclk)
        // 合法 rd_off ∈ {0,4} ⇔ 低 2 位必须是 00（bit2 才是 0/1 的区分位）。
        // 原来写成 [2:1] 是错的：addr 末位为 4 时 [2:1]=2'b10 会误报。
        if (s_axi_aresetn && u_ce && u_rd_en && (u_rd_addr[2:0] != 3'b000))
            $display("[CONV-UNI-WRAP] ERROR: rd_off 只能是 {0,4}，实际 addr=%h", u_rd_addr);
// synthesis translate_on

    // 单元看到的读数据：不再有寄存器 —— BRAM 自带的输出寄存器（cache_a_q/cache_b_q）
    // 就是原来那个 u_rd_data，语义逐拍等价于
    //     always @(posedge clk) if (mem_rd_en && hit) mem_rd_data <= mem[mem_rd_addr..+7];
    assign u_rd_data = rd_comb;

    // ========================================================================
    // 6. 读侧 §5.4 分配 / 填充队列 / AR / R
    // ========================================================================
    // ========================================================================
    // 6.0 分配路径拆拍（2026-10-30：CAM 分配/替换是第二大违例家族）
    // ========================================================================
    //  违例实测（150 MHz，WNS −11.200）：ent_tag_reg[0..31] 每个 54 条（合计 1728）、
    //  ent_out_reg 128 条（= 32 条 × 4 bit），平均 28.6 / 31.0 级。
    //  其中上游 ~16 级"引擎读地址 → CAM 命中"已由 conv_int8_uni 的
    //  mem_rd_addr = rd_addr_q 修掉（tag 比较从 FF 出发，0 级组合）。剩下这 12~14
    //  级就是本节：**而且它是一条 ent_* → 自己的反馈锥**（pf_known 要读全部
    //  ent_tag、any_out 要读全部 ent_out，再经 pf_dup_w/pf_act_now/alloc_en 回到
    //  ent_tag/ent_out 的 CE）⇒ 单纯并行化没用，必须插寄存器切断。
    //
    //  ⚠ 拆拍的合法性根基（唯一需要证明的事）：
    //      u_ce = (...) && (!rd_want || rd_hit)            // §7 门控
    //    需求未命中 ⇒ rd_want=1 且 rd_hit=0 ⇒ u_ce=0 ⇒ u_clk 停 ⇒ 引擎的
    //    rd_addr_q（= mem_rd_addr 的驱动源，conv_int8_uni.v:530）**冻结**
    //    ⇒ 整个 miss 期间 rd_tag_w / rd_slot_w / rd_want **逐拍完全相同**。
    //    于是"这个 tag 缺失且不在缓存"这个观察打几拍都不会过期。
    //    ⇒ 对命中率的代价：**零**。miss 时引擎本来就在等 10~20 拍 AXI 往返，
    //      拆拍只把每次 miss 的第一笔 AR 推迟 1 拍（同一 miss 有 9 笔 AR、~9 拍
    //      R 返回），整网 AR/需求读 ≈ 0.1~1.1 的量级上完全测不出来。
    //      命中路径（rd_hit → u_ce → 引擎推进）**一个门都没多**。
    //
    //  三级结构（每级只把上一级的结果当输入）：
    //    ① 观测 t  ：rd_need_w = rd_want && !(rd_known || 正在落盘那条同 tag)
    //    ② 决策 t+1：dem_alloc = rd_need_q && !dem_acc_q && alloc_ok && fq_room
    //                （预取侧 pf_act_now 也在本级发票：它在 t-1 拍已用 CAM 树
    //                  决好 pf_go_q，这里只剩 !dem_alloc/!pend_dup_p 两个 1 bit
    //                  仲裁 —— 见 §6.5a）
    //    ③ 落盘 t+2：ent_tag/ent_on/ent_rdy/ent_wslot/ent_out 只由 al_* 驱动
    //                ⇒ 这 5 个数组的 D/CE 各只剩 1~2 级
    //    alloc_ptr 在 ② 那一拍"发票即前进"（al_ptr 同时锁存它的**旧值**），所以连拍
    //    分配（一次窗口预取可以每拍一条、几十条）不会把两条撞进同一条目。
    //    inj/fq 的 push 仍在 ② 那一拍做（用 alloc_ptr 的旧值 + rd_tag_q/rd_slot_q），
    //    与 al_ptr/al_tag/al_slot 同沿锁存 ⇒ 描述符里的条目号/槽号与真正落盘的
    //    那一条**逐位相同**（这是把 rd_tag_w 换成 rd_tag_q 的唯一原因，见 §6.4c）。
    //  去重靠三道互不重叠的闸门（论证见 6.4c 的逐拍表）：
    //     pend_dup_o  堵"已决策、未落盘"那 1 拍（否则同 tag 会被分配两次）
    //     dem_acc_q   堵"观察打拍后仍为 1"那一拍（否则落盘后 1 拍又会分配一次）
    //     pend_dup_p  堵预取撞上正在落盘的那条（H*W*8 == d*W*8 时窗口里同一行
    //                 会出现两次；老代码靠 cam_pf 现场挡住，拆拍后要补这 1 拍）
    //  这三道闸门都只是 1 bit / 1 个 26 bit 比较，位于**决策级**（已被寄存器隔开）。
    // ========================================================================
    // ---- 分配（需求优先，其次预取）；轮转替换 ----
    assign alloc_ok = (!ent_on[alloc_ptr]) || ent_rdy[alloc_ptr];
    assign fq_room  = (fq_cnt != NFQ[FQ_CW-1:0]);

    // "正在落盘的那条"与请求 tag 撞车 ⇒ 不能用"committed 的 ent_on"去判重，
    // 必须把这一拍的 al_tag 也算进去（这就是拆拍引入的唯一新状态）。
    assign pend_dup_o = al_v && (al_tag == rd_tag_w);   // 观测级：跟本拍看到的 tag 比
    assign pend_dup_p = al_v && (al_tag == pf_tag);     // 预取级：跟预取描述符比

    // ① 观测级（含 32 路 CAM 比较树，**到这里为止**）
    //    ⚠ 用 rd_want && !rd_known 而不是 rd_miss && !rd_known：
    //      cam_rdy = ent_on && ent_rdy && eq、cam_knw = ent_on && eq
    //      ⇒ |cam_rdy ⇒ |cam_knw ⇒ !rd_known ⇒ !rd_hit ⇒ rd_miss = rd_want。
    //      两者**逻辑恒等**，但后者不必等 cam_rdy 那棵树（省 2 级）。
    assign rd_need_w = rd_want && !(rd_known || pend_dup_o);

    // ② 决策级（输入全是寄存器：rd_need_q / rd_tag_q / alloc_ptr / ent_on / fq_cnt）
    assign dem_alloc = rd_need_q && !dem_acc_q && alloc_ok && fq_room;
    // 预取侧的发票用**寄存好的** pf_act_now（不再用最差 30 级组合的 pf_do）；
    // 落盘仍走 master 的 al_v/al_ptr/al_tag/al_slot 通道，预取**不新开落盘通道**。
    assign alloc_en  = dem_alloc || pf_act_now;
    // 落盘内容与 AR/描述符内容都用**打了一拍**的 (tag,slot)：两者永远同源，
    // 即使出现"观测后被解冻"的病态情形（见 6.4c 末段），条目 tag 与填进来的
    // 那条线也仍然是同一条 ⇒ 不可能出现"tag 是 A、内容是 B"的静默错数据。
    assign alloc_tag   = dem_alloc ? rd_tag_q : pf_tag;
    assign alloc_start = dem_alloc ? rd_slot_q : pf_slot_q;

    // ---- 2D 窗口预取走动：p = 平面号, r = 该平面内相对需求行的行号 -----------------
    //   p=0 只补 k-1 行（需求行那一行刚刚读过），p>=1 补满 k 行 ⇒ 合计 NCG*k-1 条。
    //   一条 64B 线只服务 8 次需求读，所以**必须一次把整个窗挂上总线**：否则每笔
    //   都付一次完整往返（k=1 时一笔只服务 8 次需求，最致命）。
    wire        pf_k1      = (k_q <= 16'd1);
    wire [3:0]  pf_p0      = pf_k1 ? 4'd1 : 4'd0;
    wire [2:0]  pf_r0      = 3'd0;
    wire [31:0] pf_a0      = pf_k1 ? (rd_line_baddr + plane_q)
                                   : (rd_line_baddr + pw8_q);
    wire [31:0] pf_pa0     = pf_k1 ? (rd_line_baddr + plane_q) : rd_line_baddr;
    wire        pf_r_last  = ((pf_r + 3'd1) >= k_q[2:0]);
    wire [3:0]  pf_p_nx    = pf_r_last ? (pf_p + 4'd1) : pf_p;
    wire [31:0] pf_addr_nx = pf_r_last ? (pf_plane_addr_q + plane_q)
                                       : (pf_addr_q + pw8_q);
    wire [31:0] pf_plane_nx = pf_r_last ? (pf_plane_addr_q + plane_q)
                                        : pf_plane_addr_q;
    // ⚠ Verilog 不允许对括号表达式取位 ⇒ 先落中间 wire
    // 预取深度：
    //    一次未命中就把**整个像素窗口**（NCG*k-1 条：本平面下面 k-1 行 + 其余
    //    NCG-1 个平面各 k 行）全部压进流水，一轮往返到齐。k=1 时行维退化，这条
    //    规则自动退化成"平面维全铺"，正好是 k=1 唯一能用的并发来源。
    // 2026-10-30：原来是 16×16 乘法（ncg_q * k_q）压在 pf_left/pf_act 上。
    //   ncg ≤ 24（5 bit）、k ∈ {1,3,5}（3 bit）⇒ 收窄成 5×3；且每跑一次不变，
    //   改成启动那拍锁进 pf_cap_q ⇒"重开窗口"只剩一次寄存器读。
    wire [4:0]  pf_ncg5  = ncg_c16[4:0];                      // NCG ≤ 24
    wire [2:0]  pf_k3    = r_k[2:0];                          // k 已校验 ∈ {1,3,5}
    wire [7:0]  pf_nk    = {3'd0, pf_ncg5} * {5'd0, pf_k3};    // NCG*k ≤ 120
    wire [7:0]  pf_want8 = pf_nk - 8'd1;                      // NCG*k-1 条
    assign pf_cap_c = (pf_want8 > PF_MAX[7:0]) ? PF_MAX[6:0] : pf_want8[6:0];

    // ---- 6.5a 预取三拍流水 ------------------------------------------------
    // 为什么拆：pf_do/pf_adv 原来是 30 级组合（32bit 越界比较 ×2 + 32 路 CAM 去重
    //   + alloc_ok + fq_room + !dem_alloc + (fq_wp==i) 译码），实测
    //   pf_plane_addr_q_reg 58 条 / fq_ent|tag|rem|slot_reg 34 条违例，最差 −9.957 ns。
    //   预取是**纯投机**的（需求走注入通道，不经过本流水），晚几拍出线零代价。
    // 拆法：
    //   拍A t0：接候选，把 32bit 越界比较单独登记（pf_in_r_q）
    //   拍B t1：CAM 去重 + alloc_ok/fq_room/pend_dup_p/!dem_alloc → pf_go_q/pf_drop_q
    //   拍C t2：pf_act_now 只由 flop + 当拍仲裁构成 ⇒ alloc_en/fq_*/指针写使能 1~2 级
    //   相邻地址在拍A 预登记进 pf_addr_nx_q/pf_plane_nx_q ⇒ pf_addr_q 的 D 端是纯寄存器。
    // 去重为什么在拍B 用**实时** cam 就够（E(t) 只由 alloc_en(t-2) 更新）：
    //   cam(E(t1)) 覆盖 alloc_en(≤ t-1)；alloc_en(t0) 由 pend_dup_p(t1) 覆盖
    //   （al_v/al_tag 就是 t0 那次发票）；alloc_en(t1)=dem_alloc(t1) 由 !dem_alloc 覆盖
    //   （pf_act_now(t1) 因 pf_go_q(t1)=0 恒 0）；alloc_en(t2) 由 pf_act_now 自带的
    //   !dem_alloc/!pend_dup_p 覆盖。⇒ 无窗口。
    // 状态稳定：拍A/拍B/拍C 都要求 !dem_alloc（dem_alloc 会重开窗口）
    //   ⇒ pf_addr_q 三拍不动，拍A 采的检查对拍C 仍然成立。
    assign pf_tag      = pf_addr_q[31:IDX_W+3];
    assign pf_in_range = (pf_addr_q >= r_in) && (pf_addr_q < inend_q);   // 拍A（慢）
    assign pf_busy     = pf_a_q || pf_go_q || pf_drop_q;
    // 拍A：接候选（dem_alloc 会重开窗口 ⇒ 必须挡住，否则三拍内地址会变）
    assign pf_acc_w    = pf_act && !pf_busy && !dem_alloc && !start_fire;
    // 拍B：决策。pf_dup_w = 这条线已经在缓存 / 正在落盘 / 越界 ⇒ 不占分配口，直接跳过
    assign pf_dup_w    = pf_known || !pf_in_r_q || pend_dup_p;
    assign pf_dec_ok   = pf_a_q && !dem_alloc && !start_fire;
    // 拍C：发票（= 原来的 pf_do，只是改由寄存器驱动，再和需求侧仲裁一次）
    assign pf_act_now  = pf_go_q && !dem_alloc && !pend_dup_p && !start_fire;
    assign pf_adv_now  = pf_act_now || pf_drop_q;

    // ---- 填充队列（**只放预取**）头 ----
    assign fq_tag_h  = fq_tag [fq_rp];
    assign fq_ent_h  = fq_ent [fq_rp];
    assign fq_slot_h = fq_slot[fq_rp];
    assign fq_rem_h  = fq_rem [fq_rp];

    // ---- 需求填充的"注入通道"（优先于预取队列）-------------------------------
    //  为什么需要：一次窗口预取会压进 ~NCG*k 条线、几百笔 AR。如果需求也被排在
    //  队尾，引擎每换一条线都要先等前面几百笔 AR 发完（实测这让 k=3 的形状
    //  反而慢了 2 倍）。所以需求填充不进 FIFO，直接走一个单描述符的注入通道，
    //  AR 仲裁给最高优先级；预取队列只在注入通道空的时候才发。
    wire [SLOT_W-1:0] NSLOT_W  = NSLOT_V;
    wire              inj_rem1 = (inj_rem == SLOT_W'(1));
    wire              fq_rem1  = (fq_rem_h == SLOT_W'(1));
    wire [SLOT_W-1:0] inj_slot_aft = (ar_src_r == 1'b0) ? slot_nxt(inj_slot) : inj_slot;
    wire [SLOT_W-1:0] q_slot_aft   = (ar_src_r == 1'b1) ? slot_nxt(fq_slot_h) : fq_slot_h;
    wire              inj_after = inj_v && !((ar_src_r == 1'b0) && inj_rem1);
    wire              q_after   = ((fq_cnt != {FQ_CW{1'b0}})) &&
                                  !((ar_src_r == 1'b1) && fq_rem1);
    wire              pop_q     = ar_hs && (ar_src_r == 1'b1) && fq_rem1;

    // AR 通道：地址/ID 来自寄存器，只在握手那一拍换 ⇒ 握手前绝对稳定
    assign m_axi_arvalid = arvalid_r;
    assign m_axi_araddr  = ar_addr_r;
    assign m_axi_arlen   = 4'd0;                       // 单拍：一笔一个 64-bit 字
    assign m_axi_arid    = ar_id_r;
    assign ar_hs            = arvalid_r && m_axi_arready;
    assign ar_fire          = ar_hs;

    // R 通道：按 RID 落条目，槽号由该条目自己的计数器给（同 ID 保序）
    assign rid_ent    = m_axi_rid[ENT_W-1:0];
    assign rid_hi_bad = (m_axi_rid != zx_id(rid_ent));
    // 写口索引：{条目号, 槽号} 纯拼接（槽步长 = SLOT_STRIDE；ent_wslot 已是 SLOT_W 位）
    assign rd_wr_idx  = {rid_ent, ent_wslot[rid_ent]};
    assign m_axi_rready = (ent_out[rid_ent] != SLOT_Z);
    assign r_fire     = m_axi_rvalid && m_axi_rready;
    assign rr_same    = r_fire && (rid_ent == ar_ent_q) && ar_hs;

    // 读线是否"完全静默"：注入通道空 + 预取队列空 + 没有任何未回拍
    always @* begin
        any_out = 1'b0;
        for (ck = 0; ck < NLINES; ck = ck + 1)
            if (ent_out[ck] != SLOT_Z) any_out = 1'b1;
    end
    assign rd_quiet = !inj_v && (fq_cnt == {FQ_CW{1'b0}}) && !any_out;

    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) begin
            alloc_ptr <= {ENT_W{1'b0}};
            ent_on    <= {NLINES{1'b0}};
            ent_rdy   <= {NLINES{1'b0}};
            fq_wp     <= {FQ_W{1'b0}};
            fq_rp     <= {FQ_W{1'b0}};
            // ⚠ fq_cnt 不在此驱动：它有自己的专用 always_ff（下方 §6.7）。
            pf_act    <= 1'b0;
            pf_p      <= 4'd0;
            pf_r      <= 3'd0;
            pf_left   <= 7'd0;
            pf_addr_q <= 32'd0;
            pf_plane_addr_q <= 32'd0;
            pf_slot_q <= {SLOT_W{1'b0}};
            // 预取三拍流水（§6.5a）
            pf_a_q        <= 1'b0;
            pf_in_r_q     <= 1'b0;
            pf_go_q       <= 1'b0;
            pf_drop_q     <= 1'b0;
            pf_addr_nx_q  <= 32'd0;
            pf_plane_nx_q <= 32'd0;
            arvalid_r <= 1'b0;
            ar_src_r  <= 1'b0;
            ar_addr_r <= 32'd0;
            ar_id_r   <= {AXI_ID_W{1'b0}};
            ar_ent_q  <= {ENT_W{1'b0}};
            inj_v     <= 1'b0;
            inj_tag   <= {TAG_W{1'b0}};
            inj_ent   <= {ENT_W{1'b0}};
            inj_slot  <= {SLOT_W{1'b0}};
            inj_rem   <= {SLOT_W{1'b0}};
            // ---- §6.0 分配流水（必须复位：rd_tag_q/al_tag 参与比较，
            //      留着 X 会让 pend_dup_* / alloc_tag 被 X 污染，仿真起点静默出错）
            rd_need_q <= 1'b0;
            rd_tag_q  <= {TAG_W{1'b0}};
            rd_slot_q <= {SLOT_W{1'b0}};
            dem_acc_q <= 1'b0;
            al_v      <= 1'b0;
            al_ptr    <= {ENT_W{1'b0}};
            al_tag    <= {TAG_W{1'b0}};
            al_slot   <= {SLOT_W{1'b0}};
            for (cr = 0; cr < NLINES; cr = cr + 1) begin
                ent_tag[cr]   <= {TAG_W{1'b0}};
                ent_wslot[cr] <= {SLOT_W{1'b0}};
                ent_out[cr]   <= {SLOT_W{1'b0}};
            end
        end else begin
            // ---------------- 6.1 R 通道落数据 ----------------
            if (r_fire) begin
                // ⚠ cache_a/cache_b 的写**不能**放在这个带 negedge 复位的块里！
                //   BRAM 不支持异步复位，Vivado 报
                //     8-4767 Reason: RAM is sensitive to asynchronous reset signal
                //     8-13159 dissolved into (32768) registers bits
                //   于是 512x64 缓存溶成 65536 个触发器 + 512:1 组合 mux，
                //   这正是 32 级逻辑 / -10.5 ns 的根因。写口已移到下面独立的无复位块。
                ent_wslot[rid_ent] <= slot_nxt(ent_wslot[rid_ent]);
            end

            // ---------------- 6.2 AR 仲裁：注入通道 > 预取队列 ----------------
            //   源选择只在"握手那一拍"或"AR 空闲"时切换 ⇒ 地址在握手前稳定。
            if (ar_hs) begin
                // 推进**本拍被服务**的那一路
                if (ar_src_r == 1'b0) begin
                    inj_slot <= slot_nxt(inj_slot);
                    inj_rem  <= inj_rem - 1'b1;
                    if (inj_rem1) inj_v <= 1'b0;
                end else begin
                    fq_slot[fq_rp] <= slot_nxt(fq_slot_h);
                    fq_rem[fq_rp]  <= fq_rem_h - 1'b1;
                    if (fq_rem1) fq_rp <= fq_rp + 1'b1;
                end
                // 下一笔（防环：inj_after / q_after 都已扣掉本拍消耗）
                if (inj_after) begin
                    arvalid_r <= 1'b1;
                    ar_src_r  <= 1'b0;
                    ar_addr_r <= {inj_tag, {(IDX_W+3){1'b0}}} + {inj_slot_aft, 3'b000};
                    ar_id_r   <= zx_id(inj_ent);
                    ar_ent_q  <= inj_ent;
                end else if (q_after) begin
                    arvalid_r <= 1'b1;
                    ar_src_r  <= 1'b1;
                    ar_addr_r <= {fq_tag_h, {(IDX_W+3){1'b0}}} + {q_slot_aft, 3'b000};
                    ar_id_r   <= zx_id(fq_ent_h);
                    ar_ent_q  <= fq_ent_h;
                end else begin
                    arvalid_r <= 1'b0;
                end
            end else if (!arvalid_r) begin
                if (inj_v) begin
                    arvalid_r <= 1'b1;
                    ar_src_r  <= 1'b0;
                    ar_addr_r <= {inj_tag, {(IDX_W+3){1'b0}}} + {inj_slot, 3'b000};
                    ar_id_r   <= zx_id(inj_ent);
                    ar_ent_q  <= inj_ent;
                end else if (fq_cnt != {FQ_CW{1'b0}}) begin
                    arvalid_r <= 1'b1;
                    ar_src_r  <= 1'b1;
                    ar_addr_r <= {fq_tag_h, {(IDX_W+3){1'b0}}} + {fq_slot_h, 3'b000};
                    ar_id_r   <= zx_id(fq_ent_h);
                    ar_ent_q  <= fq_ent_h;
                end
            end

            // ---------------- 6.3 未回拍计数（AR +1 / R -1，同条目同拍要合并）----
            if (rr_same) begin
                ent_out[rid_ent] <= ent_out[rid_ent];          // 净变化 0
            end else begin
                if (ar_hs)   ent_out[ar_ent_q] <= ent_out[ar_ent_q] + 1'b1;
                if (r_fire)  ent_out[rid_ent]  <= ent_out[rid_ent]  - 1'b1;
            end
            // 整条线到齐
            if (r_fire && !rr_same && (ent_out[rid_ent] == SLOT_W'(1)))
                ent_rdy[rid_ent] <= 1'b1;

            // ---------------- 6.4 分配条目（三级流水，见 §6.0）-------------------
            //  6.4a ①/② 的寄存器：观测打拍、决策打拍、发指针
            //   ⚠ al_ptr <= alloc_ptr 与 alloc_ptr <= alloc_ptr+1 都在这一拍，
            //     非阻塞 ⇒ al_ptr 拿到的是**旧值**（= 本拍发票出去的那一条），
            //     所以 ③ 落盘时用 al_ptr 与描述符里的 alloc_ptr 逐位相同。
            rd_need_q <= rd_need_w;
            rd_tag_q  <= rd_tag_w;
            rd_slot_q <= rd_slot_w;
            al_v      <= alloc_en;
            al_ptr    <= alloc_ptr;
            al_tag    <= alloc_tag;
            al_slot   <= alloc_start;
            if (alloc_en) alloc_ptr <= alloc_ptr + 1'b1;
            // 本次 miss 的分配令牌：发出后保持到"这个 tag 已经不在需求里"
            //  （即它已经落盘 ⇒ rd_known=1 ⇒ rd_need_w=0 ⇒ 下一拍 rd_need_q=0）
            if (dem_alloc)       dem_acc_q <= 1'b1;
            else if (!rd_need_q) dem_acc_q <= 1'b0;

            //  6.4b ③ 落盘：ent_* 只由 al_* 寄存器驱动 ⇒ D/CE 各 1~2 级
            //   ⚠ ent_tag 与 ent_on 在**同一条语句组、同一个沿**写入 ⇒ 不存在
            //     "ent_on=1 而 ent_tag 还是旧值"的中间拍（比拆拍前更强）。
            if (al_v) begin
                ent_tag  [al_ptr] <= al_tag;
                ent_on   [al_ptr] <= 1'b1;
                ent_rdy  [al_ptr] <= 1'b0;
                ent_wslot[al_ptr] <= al_slot;
                ent_out  [al_ptr] <= {SLOT_W{1'b0}};
            end

            //  6.4c 描述符（需求进注入通道 / 预取进 FIFO）——仍在 ② 那一拍
            if (dem_alloc && !inj_v) begin
                // 需求走注入通道（不进 FIFO）
                inj_v    <= 1'b1;
                inj_tag  <= rd_tag_q;      // ★ 与 al_tag 同源（见 §6.0 末段论证）
                inj_ent  <= alloc_ptr;     // ★ = 同沿锁存的 al_ptr
                inj_slot <= rd_slot_q;
                inj_rem  <= NSLOT_V;
            end else if (pf_act_now || (dem_alloc && inj_v)) begin
                // 预取（或注入通道已被占用时的需求）走 FIFO
                fq_tag[fq_wp]  <= dem_alloc ? rd_tag_q : pf_tag;
                fq_ent[fq_wp]  <= alloc_ptr;   // ★ = al_ptr
                fq_slot[fq_wp] <= dem_alloc ? rd_slot_q : pf_slot_q;
                fq_rem[fq_wp]  <= NSLOT_V;
                fq_wp <= fq_wp + 1'b1;
            end

            // ---------------- 6.5 2D 窗口预取序列推进（三拍流水，见 §6.5a）----
            // 拍A：接候选 + 预登记相邻地址（⇒ pf_addr_q/pf_plane_addr_q 的 D 端纯寄存器）
            pf_a_q        <= pf_acc_w;
            pf_in_r_q     <= pf_in_range;
            pf_addr_nx_q  <= pf_addr_nx;
            pf_plane_nx_q <= pf_plane_nx;
            // 拍B：决策（输入全是寄存器 + 一个 32 路 CAM 树）
            pf_go_q       <= pf_dec_ok && !pf_dup_w && alloc_ok && fq_room;
            pf_drop_q     <= pf_dec_ok && pf_dup_w;
            // 拍C：动状态（重开窗口优先级仍最高，与旧版同序）
            if (dem_alloc) begin
                pf_p            <= pf_p0;
                pf_r            <= pf_r0;
                pf_addr_q       <= pf_a0;
                pf_plane_addr_q <= pf_pa0;
                pf_slot_q       <= rd_slot_w;
                pf_left         <= pf_cap_q;
                pf_act          <= (pf_cap_q != 7'd0) && (pf_p0 < ncg_q) && (PF_MAX > 0);
            end else if (pf_adv_now) begin
                pf_left         <= pf_left - 7'd1;
                pf_act          <= (pf_left > 7'd1) && (pf_p_nx < ncg_q);
                pf_p            <= pf_p_nx;
                pf_r            <= pf_r_last ? 3'd0 : (pf_r + 3'd1);
                pf_addr_q       <= pf_addr_nx_q;
                pf_plane_addr_q <= pf_plane_nx_q;
            end

            // ---------------- 6.6 启动冲刷（放最后，优先级最高）----------------
            if (start_fire) begin
                ent_on    <= {NLINES{1'b0}};
                ent_rdy   <= {NLINES{1'b0}};
                fq_wp     <= {FQ_W{1'b0}};
                fq_rp     <= {FQ_W{1'b0}};
                // ⚠ fq_cnt 不在此驱动：它有自己的专用 always_ff（下方 §6.7）。
                alloc_ptr <= {ENT_W{1'b0}};
                pf_act    <= 1'b0;
                arvalid_r <= 1'b0;
                inj_v     <= 1'b0;
                // §6.0：流水线里还没落盘的那条必须丢掉（新一跑从零开始）。
                //   丢它是**安全**的：start_fire 同时清了 ent_on/ent_rdy/alloc_ptr，
                //   而引擎还没启动（run_active=0 ⇒ 没有 rd_want），下一次 miss 会
                //   重新走 ①→②→③ 拿一条新的；不会漏分配、也不会错分配。
                al_v      <= 1'b0;
                rd_need_q <= 1'b0;
                dem_acc_q <= 1'b0;
                // 预取流水一起清（否则旧一跑的候选会在新一跑里被发出去）
                pf_a_q    <= 1'b0;
                pf_go_q   <= 1'b0;
                pf_drop_q <= 1'b0;
            end
        end
    end

    // 预取队列计数（每拍一次加减，避免同拍丢更新）
    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) begin
            fq_cnt <= {FQ_CW{1'b0}};
        end else if (start_fire) begin
            fq_cnt <= {FQ_CW{1'b0}};
        end else begin
            fq_cnt <= fq_cnt + (((pf_act_now || (dem_alloc && inj_v))) ? 1'b1 : 0)
                            - (pop_q ? 1'b1 : 0);
        end
    end


    // 调试计数：需求读 / 窗口填充 / AR / R 拍
    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn)        begin cnt_rdreq <= 16'd0; cnt_fill <= 16'd0;
                                         cnt_ar <= 16'd0; cnt_rbeat <= 16'd0; end
        else if (clr_cnt_rd)       begin cnt_rdreq <= 16'd0; cnt_fill <= 16'd0;
                                         cnt_ar <= 16'd0; cnt_rbeat <= 16'd0; end
        else begin
            // ★ 2026-10-30 时序（逐值等价）：rd_want 从 CE 挪到数据端。
            //   旧: if (u_ce && rd_want) cnt_rdreq <= cnt_rdreq + 1;  （CE 多一级 AND）
            //   新: if (u_ce)            cnt_rdreq <= cnt_rdreq + {15'd0, rd_want};
            //   u_ce=0 ⇒ 两者都保持；u_ce=1 ⇒ 两者都 +rd_want ⇒ 逐拍恒等。
            if (u_ce)            cnt_rdreq <= cnt_rdreq + {15'd0, rd_want};
            if (dem_alloc)       cnt_fill  <= cnt_fill  + 16'd1;
            if (ar_fire)         cnt_ar    <= cnt_ar    + 16'd1;
            if (r_fire)          cnt_rbeat <= cnt_rbeat + 16'd1;
        end
    end
    // ========================================================================
    // 7. 写侧：字节 → beat → burst → AXI
    // ========================================================================
    // push_beat：把一个 beat 推进 beat FIFO；必要时收掉上一笔 burst 描述符。
    //   chain 条件 = 字地址正好是"上一拍 +8"、不跨 4 KB、未到拍数上限。
    //   只负责数据/指针/长度；计数器在下面统一加减。
    task automatic push_beat(input [28:0] t_word, input [63:0] t_data, input [7:0] t_strb);
        begin
            bf_data[bf_wp] <= t_data;
            bf_strb[bf_wp] <= t_strb;
            bf_wp <= (bf_wp == BF_LAST) ? {BF_W{1'b0}} : (bf_wp + 1'b1);
            if (push_chain) begin
                bu_len <= bu_len + 9'd1;                 // 串进当前 burst
            end else begin
                if (bu_len != 9'd0) begin                // 先收掉上一笔
                    df_addr[df_wp] <= {bu_base, 3'b000};
                    df_len [df_wp] <= bu_len;
                    df_wp  <= (df_wp == DF_LAST) ? {DF_W{1'b0}} : (df_wp + 1'b1);
                    cnt_burst <= cnt_burst + 16'd1;
                end
                bu_base <= t_word;
                bu_len  <= 9'd1;
            end
        end
    endtask

    // ------------------------------------------------------------------------
    // 7.0 写口入口寄存器：把写回路径切成"吞字节"和"合并/记账"两级
    // ------------------------------------------------------------------------
    // 时序问题（综合后 150 MHz WNS = −11.2 ns，写侧端点是全芯片最差的一族）：
    //   原来 wr_byte = wr_take && u_ce，而 u_ce 的上游是
    //   "引擎读地址 → CAM 命中 → rd_hit"那 16~20 级共享锥。于是
    //        u_ce → wr_byte → merge_* → push_now → bf_data CE / df_addr CE
    //   整条链全部挂在同一条 35 级的路上；写侧自己还额外贡献
    //   be_same_word（29 位比较）+ push_chain（29 位加法 + 29 位比较）十几级。
    //
    // 改动：写口**只多 1 拍延迟**（写侧无延迟契约，见文件头 §1 与规格 §4：
    //   内存写口是纯流式，字节顺序不变就等价），把 u_wr_* 先打进 p_* 寄存器：
    //
    //   第 0 级（u_clk 域，引擎输出，本来就是寄存器）
    //        u_wr_en / u_wr_addr / u_wr_data
    //        ⇒ wr_byte = u_wr_en && u_ce          ← 只剩 1 级 AND，交给 p_* 的 CE
    //   第 1 级（s_axi_aclk 域，新增 p_valid/p_addr/p_data）
    //        ⇒ w_lane/w_word/be_same_word/be_strb_nx/merge_*/push_now/push_chain
    //          全部从**本模块自己的寄存器**出发，和 u_ce 锥彻底解耦。
    //
    // 吞吐不变：字节流每拍进 1 个、每拍合并 1 个 —— 入口寄存器是"固定的 1 级
    //   流水"，不是 valid/ready 握手（合并逻辑同拍把 p_data 组合吃掉、同拍再写
    //   入下一个字节），所以连续字节流一个都不会丢。
    //
    // 三条必须一起改的配套逻辑（不改就是静默丢字节/静默溢出）：
    //   ① 不变式 p_valid ⇒ wst==W_IDLE。
    //      推导：吞字节要求 u_ce=1，而 u_ce 在 (wst!=W_IDLE && wr_take) 时恒 0；
    //      反向（wst 下一拍变成 W_DR1 而本拍刚吞了字节）由 drain_cond 里的
    //      !wr_byte 拦住 ⇒ 吞下的字节下一拍一定在 W_IDLE 被合并。
    //   ② drain_cond 必须等入口寄存器空（!p_valid && !wr_byte），否则排空状态机
    //      会把"还没并进 be_* 的字节"跨过去：be_* 被冲掉、字节留到下一轮，
    //      造成同一字被拆成两个 beat（功能仍对，但白多一笔 AXI 事务）。
    //      ★ 2026-10-30：drain_cond 里的 !wr_byte 已**代数消去**（见该 assign
    //      上方的逐值 proof），等价式 `!u_wr_en || (wr_fifo_press && !u_ce_free)`
    //      本身仍保证"drain=1 ⇒ wr_byte=0"，所以这条约束没有被放松。
    //   ③ wr_all_idle 必须带 !p_valid，否则"最后一个字节还在寄存器里"就报 done，
    //      PS 提前读内存 ⇒ 静默错数据。
    // ------------------------------------------------------------------------
    assign wr_take = u_wr_en;
    assign wr_byte = wr_take && u_ce;            // 本拍吞下的字节（进 p_*）
    assign w_lane  = p_addr[2:0];                // 以下全部用**入口寄存器**的字节
    assign w_word  = p_addr[31:3];
    assign be_same_word = be_open && (be_word == w_word);
    assign be_strb_nx   = be_strb | (8'd1 << w_lane);
    assign p_merge      = p_valid && (wst == W_IDLE);   // 本拍把入口字节并进 be_*
    assign wr_fifo_press = (bf_cnt >= BF_PRESS) || (df_cnt >= DF_PRESS);

    // u_ce = 引擎这一拍推进；三个冻结源见文件头 §5
    // 复位期/空闲期恒 1：避免仿真起点 u_ce=X 把门控时钟卡成 X
    //
    // ⚠ 压力项从 "wr_take && wr_fifo_press && !wr_safe_now" 改成
    //   "wr_take && wr_fifo_press"（去掉 wr_safe_now 这个"预测"）。
    //   原因：wr_safe_now 是在**吞字节那一拍**预测"这个字节会不会 push"，而改动后
    //   它要到下一拍才 push，预测基准整整差一拍（be_word/be_strb 可能已被上一个
    //   字节改过）⇒ 预测可以错。错了就会在 FIFO 高位继续 push ⇒ 溢出/静默错数据。
    //   现在改成"压力到就停引擎"：压力下不再有新字节进入 ⇒ 合并路径不可能再 push，
    //   bf_cnt 只能靠 AXI 弹出下降 ⇒ 上界严格可证（核算见本节末）。
    //   代价：压力期会提前把一个没填满的字当作一个 beat 冲出去（一次压力事件最多
    //   多 1 个 beat、1 个描述符），写侧流量本来只有读侧的百分之几，可忽略。
    //   顺带把 u_ce 上"29 位比较 + 8 位与树"整段拿掉。
    // ⚠ 2026-10-30：表达式**逐位未变**，只是把 R / W 两个因子提到 rd_live 那里命名，
    //   供 rd_live 共享（省 2 级，见那里）。这行与 rd_live 的化简必须**成对**看：
    //   rd_live 依赖 (R || W) 这个形式，若把 W 改回带 P 的形式，等价性就不成立了。
    assign u_ce = u_ce_free
                || ((!rd_want || rd_hit) && u_ce_wr_ok);

    // ---- 例化时钟门（参数化：latch+AND 或 BUFGCE）----
    generate
    if (USE_BUFGCE) begin : g_clk_bufgce
        BUFGCE u_bufgce (
            .I  (s_axi_aclk),
            .CE (u_ce),
            .O  (u_clk)
        );
    end else begin : g_clk_gate
        logic u_ce_lat;
        always_latch begin
            if (!s_axi_aclk) u_ce_lat = u_ce;   // 只在低电平期换值 ⇒ u_clk 无毛刺
        end
        assign u_clk = s_axi_aclk & u_ce_lat;
    end
    endgenerate

    // ------------------------------------------------------------------------
    // 排空触发：写口空闲 / FIFO 压力（后者保证"有 beat 必有描述符"，防死锁）
    //
    // ★ 2026-10-30（micro 轮，WNS 收口）：`!p_valid && !wr_byte` 化成**只含 FF 的
    //   等价式**，把 u_ce / rd_want / rd_hit 整条锥从排空转移条件上彻底摘掉。
    //
    //   为什么可以"只在一个分支里等价"：drain_cond 在本文件**只有一个消费者**
    //   （下方 `W_IDLE: if (drain_cond) wst <= W_DR1;`），W_DR1/W_DR2 的转移是
    //   无条件的。所以只需在 **wst==W_IDLE** 这一拍逐位相等；其它状态下它是
    //   don't-care。而在 wst==W_IDLE 时 u_ce_wr_ok 里的
    //   `!((wst != W_IDLE) && wr_take)` 恒为 1。
    //
    //   记号：E = wr_take = u_wr_en，F = wr_fifo_press，R = u_ce_free，
    //         B = be_open || (bu_len != 0)，PV = p_valid，P = !rd_want || rd_hit。
    //       原式  drain = B && !PV && !wr_byte && (!E || F)
    //             wr_byte = E && u_ce = E && ( R || (P && !(E && F)) )
    //   逐值展开（只分 E=0 / E=1 两支，没有任何"综合器应该能推出"的假设）：
    //       E=0 : wr_byte = 0                ⇒ drain = B && !PV
    //       E=1 : drain = B&&!PV&&F&&!wr_byte
    //                   = B&&!PV&&F&&!R&&(!P || F)
    //                   = B&&!PV&&F&&!R          ← F=1 使 (!P||F) 恒 1
    //   ⇒ 合并两支：  drain|wst=W_IDLE = B && !PV && ( !E || (F && !R) )
    //   注意 E=1 支里 **P（⇒ rd_want ⇒ mem_rd_en ⇒ rd_wld ⇒ wl_tend，以及 CAM 的
    //   rd_hit）整体抵消**：FIFO 到了压力线时 u_ce 已被 !(E&&F) 强制成 R，
    //   引擎本来就推不动，排空与否与"这次读命中没有"无关 —— 这正是物理直觉。
    //
    //   收益：排空转移条件不再依赖 rd_want/rd_hit（CAM 26 bit×32 比较）与 u_ce
    //   （门控时钟 u_clk 的 SCD≈1.95 ns，跨回 FCLK 域要罚 ≈1.51 ns）。
    //   实测（20:21 综合）：FSM_onehot_wst[0..2]/D 的 13 级 = CAM 比较 8 级 +
    //   u_ce 2 级 + wr_byte 1 级 + 排空 2 级；改后只剩
    //   `wr_fifo_press(计数器比较) / u_wr_en / p_valid / be_open|bu_len` 这几只 FF。
    //
    //   语义没有放松：新式仍然保证 drain=1 ⇒ wr_byte=0
    //     （E=1 支要求 F=1 ⇒ u_ce_wr_ok=0 ⇒ u_ce=R ⇒ E&&F&&!R 时 wr_byte=0；
    //       E=0 支 wr_byte 直接为 0），
    //   所以 §7.0 的两条不变式（"p_valid ⇒ wst==W_IDLE"、"排空不跨过还没并进
    //   be_* 的字节"）逐条照旧成立（translate_off 里也仍有对应断言）。
    // ------------------------------------------------------------------------
    assign drain_cond = ((be_open || (bu_len != 9'd0)) && !p_valid &&
                         (!u_wr_en || (wr_fifo_press && !u_ce_free)));

    // ------------------------------------------------------------------------
    // FIFO 记账全部放在**一处**：cnt 既会被"推入"（字节合并、排空）改，也会被
    // "弹出"（写引擎）改；若两处各写一次非阻塞赋值，同拍一推一弹就丢一次更新
    // ⇒ 计数器偏 1 ⇒ 描述符错位 ⇒ 静默错数据。所以先组合判 push/pop 再加减。
    // ------------------------------------------------------------------------
    assign merge_done   = p_merge && be_same_word &&
                          (be_strb_nx == 8'hFF);          // 整字凑满 ⇒ 落拍
    assign merge_change = p_merge && !be_same_word;
    assign drain_pub    = (wst == W_DR1) && be_open;
    // 本拍会推一个 beat 吗？（三种情况推的都是 open beat）
    assign push_now     = merge_done || (merge_change && be_open) || drain_pub;
    // 这个 beat 会不会串进当前 burst（不串 ⇒ 要先收掉上一笔描述符）
    //   原式 be_word == (bu_base + {20'd0, bu_len})：29 位加法器 + 29 位比较器，
    //   而且加法结果要等到 bu_len 更新后的**下一拍**才用得上 → 把加法器整个搬进
    //   寄存器 bu_next（**弱形式**：bu_len != 0 时才有 bu_next == bu_base+bu_len，见 §7.0 末），
    //   比较器只剩两排
    //   寄存器打进去的 29 位相等比较，深度从 ~13 级降到 ~5 级。
    assign push_chain   = (bu_len != 9'd0) &&
                          (be_word == bu_next) &&
                          (be_word[28:9] == bu_base[28:9]) &&
                          (bu_len < BU_MAX);
    // bu_len != 0 这一项不能少：push_beat 只在 bu_len != 0 时才真的写描述符
    assign desc_push    = (push_now && !push_chain && (bu_len != 9'd0)) ||
                          ((wst == W_DR2) && (bu_len != 9'd0));
    assign bf_pop       = (axst == AX_W) && m_axi_wready;
    assign ax_can_start = (axst == AX_IDLE) && (df_cnt != DF_Z) &&
                          ({1'b0, bf_cnt} >= {1'b0, df_len[df_rp]});
    assign desc_pop     = ax_can_start;

    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) begin
            be_open <= 1'b0; be_word <= 29'd0; be_data <= 64'd0; be_strb <= 8'd0;
            bu_base <= 29'd0; bu_len  <= 9'd0; bu_next <= 29'd0;
            p_valid <= 1'b0; p_addr <= 32'd0; p_data <= 64'd0;
            bf_wp <= {BF_W{1'b0}}; bf_rp <= {BF_W{1'b0}}; bf_cnt <= BF_Z;
            df_wp <= {DF_W{1'b0}}; df_rp <= {DF_W{1'b0}}; df_cnt <= DF_Z;
            wst   <= W_IDLE;
            axst  <= AX_IDLE; ax_addr <= 32'd0; ax_len <= 9'd0; ax_sent <= 9'd0;
            ax_awvalid <= 1'b0; ax_wvalid <= 1'b0; ax_bready <= 1'b0;
            cnt_burst <= 16'd0; cnt_wbeat <= 16'd0;
        end else begin
            // ---------------- FIFO 计数（每拍一次，加减同拍都对）----------------
            bf_cnt <= bf_cnt + (push_now ? 1'b1 : 1'b0) - (bf_pop ? 1'b1 : 1'b0);
            df_cnt <= df_cnt + (desc_push ? 1'b1 : 1'b0) - (desc_pop ? 1'b1 : 1'b0);

            // ---------------- 7.0a 写口入口寄存器（1 拍）----------------
            //   吞字节：wr_byte = u_wr_en && u_ce（现在只喂 p_valid 这一个 D 端）
            //   p_* 里的字节在下一拍被 7.1 合并；每拍进 1 个、每拍并 1 个，吞吐不变。
            //
            //   ★ 2026-10-30（finalopt 轮，WNS 收口）：p_addr / p_data 的 CE 从
            //     `wr_byte`（= u_wr_en && u_ce）改成 **只有 `u_ce`**，并且**不保持**
            //     （D 端直连 u_wr_addr / u_wr_data，既没有 u_wr_en 也没有 2:1 mux）。
            //     为什么不是"CE=u_ce + D 端 u_wr_en?new:hold"的 mux 版：那两式虽然
            //     逐值恒等，但综合器对 FDCE 的**使能推断**会把 `if (u_ce) if (u_wr_en)`
            //     重新合成 CE = u_ce && u_wr_en —— 那正是改动前的网表，一级也省不下。
            //     下面的写法让 CE 里**根本没有 wr_take 这一项**，工具无从折叠。
            //
            //   ---- 证明 1：被观测值逐值等价（p_valid=1 的每一拍）----
            //     p_valid 一个字没改：p_valid(t+1) = wr_byte(t) = u_wr_en(t) && u_ce(t)。
            //     于是 "p_valid(t) = 1" ⟹ u_wr_en(t-1)=1 且 u_ce(t-1)=1 ⟹
            //        旧：p_addr(t) = wr_byte(t-1) ? u_wr_addr(t-1) : …  = u_wr_addr(t-1)
            //        新：p_addr(t) = u_ce(t-1)    ? u_wr_addr(t-1) : …  = u_wr_addr(t-1)
            //     两者**逐位相同**（p_data 同理；复位后两式也都是 0）。
            //     更直白地说：p_valid 与 p_addr 永远在**同一个使能条件窗口**里滚动
            //     （p_valid 的"置 1"和 p_addr 的"装载"都要求 u_ce(t-1)=1，而 p_valid
            //      额外要求 u_wr_en(t-1)=1），所以只要 p_valid=1，p_addr 一定就是
            //      那个字节的地址，绝不会被"装错的那一拍"覆盖。
            //
            //   ---- 证明 2：p_valid=0 时的值是 don't-care（穷举全部消费者）----
            //     p_addr/p_data 只经 w_lane/w_word/be_same_word/be_strb_nx 与 1793 的
            //     `if (p_merge)` 块被消费，全文件再无第 7 处（grep w_lane|w_word|p_data
            //     可核）。逐条：
            //       · 1793 块内的 6 条语句（1795/1798/1799/1805/1809/1810）
            //         ⇒ 只在 p_merge = p_valid && (wst==W_IDLE) 为 1 时生效。
            //       · be_same_word(1618) / be_strb_nx(1619) 只被
            //             merge_done   = p_merge && be_same_word && (be_strb_nx==8'hFF)
            //             merge_change = p_merge && !be_same_word
            //         使用 ⇒ 都带 p_merge 因子；而
            //             push_now = merge_done || (merge_change && be_open) || drain_pub
            //         里不含 p_* 的那一支只有 drain_pub（纯 wst/be_open）
            //         ⇒ p_valid=0 时 push_now / push_chain / desc_push / be_* 的写使能
            //           与 w_word/w_lane/p_data 的取值**完全无关**。
            //       · 其余地方（drain_cond、wr_all_idle、断言）都不读 p_addr/p_data。
            //     ⇒ 没有任何一处会在 p_valid=0 时把 p_addr/p_data 传出去。
            //     （"p_valid=1 ⇒ wst==W_IDLE"这条不变式有 translate_off 断言守着，
            //      本轮未删未改。）
            //     ⚠ 由此得到一条**硬契约**：以后任何人若在 `if (p_merge)` 之外读
            //       p_addr/p_data，必须同时判 p_valid。写在注释里，别再踩。
            //
            //   ---- 净核算（CE 路 vs D 路）----
            //     CE 路：旧 CE = wr_byte = u_wr_en && u_ce，在 u_ce 之后**多 1 级 LUT**；
            //            新 CE = u_ce ⇒ **−1 级**。量级可查 micro 轮的逐 cell 分解
            //            （150 MHz，同一份 RTL）：u_ce 落 7.065 ns、wr_byte 落
            //            7.535 ns ⇒ 这一级 ≈ 0.47 ns。
            //     D  路：旧 D = u_wr_addr/u_wr_data；新 D **完全相同** ⇒ **+0 级**
            //            （比"CE=u_ce + D 端加 mux"的写法还少一级：那种写法 D 端要
            //             多吃一个 2:1 mux。u_wr_addr = raddr_q + rcnt_q 只有 1 次
            //             32 bit 加法，u_wr_data 是 4:1 mux，≈2 级，跨
            //             u_clk→s_axi_aclk 的预算 7.11 ns 下余量 >4 ns。）
            //     ⇒ 净 −1 级 LUT，且 CE 锥里不再有 u_wr_en 这一项。
            //   副作用：p_addr/p_data（97 个 FF）现在每个 u_ce 拍都翻新，而不是只在
            //     写字节那拍翻新 ⇒ 动态功耗略增（97 个 FF + 其 D 端网络），相对
            //     32 lane×30 个 acc 的翻转量可忽略，且不影响功能。
            //   ⚠ p_valid **不能**套用这个改法：它**每拍无条件刷新**（p_valid<=wr_byte，
            //     没有"保持"分支，D 端本身就是要算的那个 AND）。若给它加 CE=u_ce +
            //     保持 mux，"本拍没吞字节 ⇒ 清 0" 会退化成"保持旧值"，入口寄存器再也
            //     不空 ⇒ 排空/合并全乱（静默丢字节）。所以下面这一行必须留在 wr_byte。
            p_valid <= wr_byte;
            if (u_ce) begin
                p_addr <= u_wr_addr;     // 不带保持：p_valid=0 拍上的值无人读（证明 2）
                p_data <= u_wr_data;
            end

            // ---------------- 7.0b burst 记账：bu_next ≡ bu_base + bu_len ----------------
            //   推完一个 beat 之后，无论 chain 与否，都有
            //       新 bu_base + 新 bu_len = (该 beat 的字地址) + 1 = be_word + 1
            //     chain : bu_base 不变，bu_len' = bu_len+1，且 chain 成立时
            //             be_word == bu_base+bu_len ⇒ be_word+1 = bu_base + bu_len'
            //     不 chain: bu_base' = be_word，bu_len' = 1 ⇒ be_word+1 = bu_base' + 1
            //   ⇒ 只要 push 就写 be_word+1，否则保持。
            //   ⚠ 精确形式是**弱形式**：`bu_len != 0 ⇒ bu_next == bu_base + bu_len`。
            //     强形式（无条件相等）不成立——W_DR2 会清 bu_len 而不动 bu_base，那段
            //     窗口里不变式暂时失效；但 push_chain 的第一项就是 `bu_len != 0`，
            //     正好把失效窗口屏蔽掉，所以实现无害。（2026-10-30 由子代理指出并修正注释）
            //   于是 push_chain 的 29 位加法器变成一次"寄存器 +1"，加法器不再落在
            //   df_addr/df_len/bu_* 的写使能链上。
            if (push_now) bu_next <= be_word + 29'd1;

            // ---------------- 7.1 字节合并（处理入口寄存器里的那个字节）----------------
            if (p_merge) begin
                if (be_same_word) begin
                    be_data[w_lane*8 +: 8] <= p_data;
                    be_strb <= be_strb_nx;
                    if (be_strb_nx == 8'hFF) begin        // 整字凑满 ⇒ 立即落拍
                        push_beat(be_word, (be_data & ~(64'hFF << (w_lane*8)))
                                            | ({56'd0, p_data} << (w_lane*8)),
                                  be_strb_nx);
                        be_open <= 1'b0;
                        be_data <= 64'd0;
                        be_strb <= 8'd0;
                    end
                end else begin
                    if (be_open) push_beat(be_word, be_data, be_strb);  // 换字：先落上一拍
                    be_open <= 1'b1;
                    be_word <= w_word;
                    be_data <= ({56'd0, p_data} << (w_lane*8));
                    be_strb <= (8'd1 << w_lane);
                end
            end

            // ---------------- 排空状态机（2 拍）----------------
            case (wst)
            W_IDLE: begin
                if (drain_cond) wst <= W_DR1;
            end
            W_DR1: begin
                if (be_open) begin
                    push_beat(be_word, be_data, be_strb);   // 这一步可能又收一笔
                    be_open <= 1'b0;
                    be_data <= 64'd0;
                    be_strb <= 8'd0;
                end
                wst <= W_DR2;
            end
            W_DR2: begin
                if (bu_len != 9'd0) begin
                    df_addr[df_wp] <= {bu_base, 3'b000};
                    df_len [df_wp] <= bu_len;
                    df_wp  <= (df_wp == DF_LAST) ? {DF_W{1'b0}} : (df_wp + 1'b1);
                    bu_len <= 9'd0;
                    cnt_burst <= cnt_burst + 16'd1;
                end
                wst <= W_IDLE;
            end
            default: wst <= W_IDLE;
            endcase

            // ---------------- 写引擎 ----------------
            case (axst)
            AX_IDLE: begin
                // 描述符在手 且 它的拍已经全在 beat FIFO 里 才能开
                if (ax_can_start) begin
                    ax_addr <= df_addr[df_rp];
                    ax_len  <= df_len[df_rp];
                    ax_sent <= 9'd0;
                    df_rp   <= (df_rp == DF_LAST) ? {DF_W{1'b0}} : (df_rp + 1'b1);
                    ax_awvalid <= 1'b1;
                    axst    <= AX_AW;
                end
            end
            AX_AW: begin
                if (m_axi_awready) begin
                    ax_awvalid <= 1'b0;
                    ax_wvalid  <= 1'b1;
                    axst       <= AX_W;
                end
            end
            AX_W: begin
                if (m_axi_wready) begin
                    bf_rp  <= (bf_rp == BF_LAST) ? {BF_W{1'b0}} : (bf_rp + 1'b1);
                    cnt_wbeat <= cnt_wbeat + 16'd1;
                    if (m_axi_wlast) begin
                        ax_wvalid <= 1'b0;
                        ax_bready <= 1'b1;
                        axst      <= AX_B;
                    end else begin
                        ax_sent <= ax_sent + 9'd1;
                    end
                end
            end
            AX_B: begin
                if (m_axi_bvalid) begin
                    ax_bready <= 1'b0;
                    axst      <= AX_IDLE;
                end
            end
            default: axst <= AX_IDLE;
            endcase

            // 调试计数：清零放最后（优先于本拍的 +1）
            if (clr_cnt_wr) begin
                cnt_burst <= 16'd0;
                cnt_wbeat <= 16'd0;
            end
        end
    end

    assign m_axi_awvalid = ax_awvalid;
    assign m_axi_awaddr  = ax_addr;
    assign m_axi_awlen   = ax_len[3:0] - 4'd1;
    assign m_axi_wid     = {AXI_ID_W{1'b0}};   // 单一写源，写 ID 恒 0   // WR_BURST_MAX=16 ⇒ awlen≤15
    assign m_axi_wvalid  = ax_wvalid;
    assign m_axi_wdata   = bf_data[bf_rp];
    assign m_axi_wstrb   = bf_strb[bf_rp];
    assign m_axi_wlast   = (ax_sent == (ax_len - 9'd1));
    assign m_axi_bready  = ax_bready;

    // done 上报前必须成立：写侧全空（保证 PS 看到 done=1 时内存已一致）
    //   !p_valid：入口寄存器（§7.0）里可能还有一个刚吞下、还没并进 be_* 的字节。
    //   少了这一项，最后一个字节会在"已经报 done"之后才落进 FIFO ⇒ PS 提前读内存
    //   ⇒ 静默错数据（只在"最后一个字节正好落在 done 前 1 拍"的形状上偶发）。
    assign wr_all_idle = (wst == W_IDLE) && !be_open && (bu_len == 9'd0) &&
                         !p_valid &&
                         (bf_cnt == BF_Z) && (df_cnt == DF_Z) && (axst == AX_IDLE);

    // ------------------------------------------------------------------------
    // 7.9 bf_cnt / df_cnt 峰值核算（写口 +1 拍入口寄存器、阈值 −1 之后的上界）
    // ------------------------------------------------------------------------
    // 记号：BF = WR_BUF_BEATS = 16，BF_PRESS = BF-4 = 12。bf_cnt 每拍最多 +1
    // （一个 push）、最多 -1（一个 pop），push 源只有两个且互斥：
    //   · 合并推 beat（merge_done / merge_change&&be_open）：要求本拍有 p_merge，
    //     而 p_merge 的字节是上一拍吞下的 → 上一拍必须 !(wr_take&&wr_fifo_press)
    //     → 上一拍 bf_cnt ≤ BF_PRESS-1 = 11 且 df_cnt ≤ 11；
    //   · 排空推 beat（W_DR1 的 drain_pub）：要求 !p_valid && !wr_byte。
    // 逐拍推：
    //   t   : bf_cnt(t) ≤ 11，吞下字节                       （push 最早在 t+1）
    //   t+1 : bf_cnt ≤ 12（可能 +1），push 发生 ⇒ bf_cnt(t+2) ≤ 13
    //   t+2 : bf_cnt ≥ 12 ⇒ 进入压力区 ⇒ 不可能再吞字节 ⇒ 合并路径不再 push
    //         排空状态机在这之后还会 push 1 次（W_DR1），且之后 be_open/bu_len 都归 0
    //         ⇒ 再没有可推的 beat（要等新字节，而新字节被压力挡着）
    //   ⇒ **峰值 = 14 = BF-2**，和改动前（BF_PRESS = BF-3 时的 14）逐位相同。
    // 即：阈值 −1 正好抵消写口 +1 拍的“吞字节 / 推 beat 差一拍”，余量没有被吃掉。
    // df_cnt 同理（desc_push 只来自 push_now 或 W_DR2），峰值同样 = DF-2。
    // 死锁检查：压力把引擎冻住，但 AXI 写引擎跑在 s_axi_aclk 上（不受 u_ce 影响）；
    //   bf_cnt ≥ 12 时若 bu_len 还没收尾，drain_cond 会因为 wr_fifo_press 立刻成立
    //   把描述符落进 df FIFO ⇒ ax_can_start（bf_cnt ≥ df_len[df_rp]）可满足 ⇒ 一定会
    //   有 pop ⇒ 压力解除。不会出现"引擎冻住 + FIFO 有数据 + 没有描述符"的僵局。
    // ------------------------------------------------------------------------

    // ========================================================================
    // 8. 运行期不变量 / AP_RETURN / AXI4-Lite 读通道
    // ========================================================================
    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) begin
            flg_rid_bad <= 1'b0;
            flg_4k      <= 1'b0;
        end else if (clr_flg) begin
            flg_rid_bad <= 1'b0;
            flg_4k      <= 1'b0;
        end else begin
            if (r_fire && rid_hi_bad) flg_rid_bad <= 1'b1;
            if (m_axi_awvalid && m_axi_awready &&
                (({20'd0, m_axi_awaddr[11:0]} + ({4'b0000, m_axi_awlen} * 8)) > 12'd4095))
                flg_4k <= 1'b1;
        end
    end

    // bit1 busy：把"已登记 start、还没真启动"那两拍也算 busy
    wire        busy_w     = start_pend || (run_active && !done_sticky);
    wire [31:0] ap_return  = {16'd0, err_code, 6'd0, busy_w, done_sticky};
    wire        err_sticky = (err_code != 8'd0);

    assign s_axi_arready = !rvalid;

    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) begin
            araddr_l <= 32'd0;
            rvalid   <= 1'b0;
        end else begin
            if (s_axi_arvalid && s_axi_arready) begin
                araddr_l <= s_axi_araddr;
                rvalid   <= 1'b1;
            end else if (rvalid && s_axi_rready) begin
                rvalid   <= 1'b0;
            end
        end
    end

    // 状态字（用连续赋值而不是 always_comb：iverilog 11 对 always_comb 里的
    // 常量位选支持不全，连续赋值在 xvlog/iverilog 上语义完全一致）
    logic [31:0] status_w;
    assign status_w = {23'd0, (busy_w ? 1'b0 : 1'b1),   // [31:8] = 0, [8] idle
                       err_code[3:0],                   // [7:4] err_code
                       1'b0,                            // [3]   保留
                       err_sticky, busy_w, done_sticky}; // [2]err [1]busy [0]done

    logic [31:0] rd_mux;
    always @* begin
        rd_mux = 32'd0;
        if (araddr_l[16]) begin              // 配置页
            case (araddr_l[7:0])
            8'h00: rd_mux = {30'd0, r_ctrl[1], 1'b0};   // bit0 自清
            8'h10: rd_mux = ap_return;
            8'h18: rd_mux = r_h;
            8'h20: rd_mux = r_w;
            8'h38: rd_mux = r_cin;
            8'h40: rd_mux = r_cout;
            8'h48: rd_mux = r_k;
            8'h50: rd_mux = r_s;
            8'h58: rd_mux = r_d;
            8'h60: rd_mux = r_pad;
            8'h68: rd_mux = r_shift;
            8'h70: rd_mux = r_relu;
            8'h74: rd_mux = r_tap;
            8'h94: rd_mux = status_w;
            8'hE0: rd_mux = {cnt_fill, cnt_rdreq};
            8'hE4: rd_mux = {cnt_rbeat, cnt_ar};
            8'hE8: rd_mux = {cnt_wbeat, cnt_burst};
            8'hF0: rd_mux = {28'd0, u_busy, flg_4k, (r_hi_bad != 4'd0), flg_rid_bad};
            default: rd_mux = 32'd0;
            endcase
        end else begin                        // 基址页
            case (araddr_l[7:0])
            8'h10: rd_mux = r_in;
            8'h1C: rd_mux = r_wgt;
            8'h28: rd_mux = r_bias;
            8'h34: rd_mux = r_out;
            default: rd_mux = 32'd0;
            endcase
        end
    end

    assign s_axi_rdata  = rd_mux;
    assign s_axi_rresp  = 2'b00;
    assign s_axi_rvalid = rvalid;

    // ========================================================================
    // 9. 仿真期断言（translate_off，不综合）
    //    —— 只报"会让仿真白跑几小时 / 静默错数据"的那几类错
    // ========================================================================
    // synopsys translate_off
    initial begin
        if (RD_WIN_WORDS < 2 || RD_WIN_WORDS > 16 ||
            ((RD_WIN_WORDS & (RD_WIN_WORDS - 1)) != 0))
            $fatal(1, "[CONV-UNI-WRAP] RD_WIN_WORDS 必须是 2..16 的 2 的幂（当前 %0d）",
                   RD_WIN_WORDS);
        if (NLINES < 2 || ((NLINES & (NLINES - 1)) != 0))
            $fatal(1, "[CONV-UNI-WRAP] NLINES 必须是 >=2 的 2 的幂（当前 %0d）", NLINES);
        if (AXI_ID_W < ENT_W)
            $fatal(1, "[CONV-UNI-WRAP] AXI_ID_W(%0d) 必须 >= $clog2(NLINES)=%0d",
                   AXI_ID_W, ENT_W);
        if (PF_MAX > 127)
            $fatal(1, "[CONV-UNI-WRAP] PF_MAX 必须 <= 127（窗口最多 NCG*k-1 = 119 条）");
    end

    // ⚠ 用**真宏**隔离调试逻辑。原来只有一个 pragma 注释，Vivado 照样把
    //   'dbg_addr[0:287]'（288×32 = **9216 bit**，且带复位循环无法推断成 RAM）
    //   综合成 9216 个触发器，白吃掉 conv_uni_0 的一大块 FF。
    //   （槽步长补成 16 后这里是 dbg_addr[0:511]，512×32 = 16 Kbit，仅仿真占用。）
// synthesis translate_off
    // ---- 缓存内容自检：每个(条目,槽)记下"当初是为哪个字节地址取的数"，
    //      命中时必须完全一致。这条断言把"CAM/条目号/槽号/绕回写错"从静默数据
    //      错误变成一行 ERROR（bin_unit_axi_wrap 2026-10-28 的致命 bug 就是被
    //      同一手法抓出来的）。----
    // ⚠ 原来这里有一个 `dbg_addr[0:DEPTH-1]` 数组（512×32 = 16 Kbit）做"缓存内容自检"，
    //   综合日志显示它会被实现成寄存器阵列（Synth 8-4767 / 8-13159），白吃掉大量 FF/LUTRAM。
    //   已物理删除 —— TB 的逐字节比对是更强、更可靠的验收手段，不需要它。
    //   注意：`// synthesis translate_off` 只是注释，Vivado 对**声明**的裁剪并不保证，
    //   之前实测 4096 LUTRAM 就疑似来自这里，所以不能只靠 pragma。

    integer frozen_cnt;

    // §6.0 拆拍专项：落盘 tag 与"已有条目 tag"撞车的组合检查（仅仿真，不进综合）
    //   dup_al：落盘那一刻（③）al_tag 是否已经存在于某个 ent_on=1 的条目里
    //   dup_q ：决策那一刻（②）准备分配的那个 tag(rd_tag_q) 是否已经在缓存里
    integer dup_i, dup_j;
    logic   dup_al, dup_q;
    always @* begin
        dup_al = 1'b0;
        for (dup_i = 0; dup_i < NLINES; dup_i = dup_i + 1)
            if (ent_on[dup_i] && (ent_tag[dup_i] == al_tag)) dup_al = 1'b1;
    end
    always @* begin
        dup_q = 1'b0;
        for (dup_j = 0; dup_j < NLINES; dup_j = dup_j + 1)
            if (ent_on[dup_j] && (ent_tag[dup_j] == rd_tag_q)) dup_q = 1'b1;
    end

    always @(posedge s_axi_aclk) begin
        if (!s_axi_aresetn) begin
            frozen_cnt = 0;
        end else begin
            // (1) 时钟门控卡死报警
            if (run_active && !u_ce) frozen_cnt = frozen_cnt + 1;
            else                     frozen_cnt = 0;
            if (frozen_cnt == 200000)
                $display("[CONV-UNI-WRAP] ERROR: 引擎被冻住 200000 拍 rd_want=%0d rd_hit=%0d fq=%0d pf_act=%0d wst=%0d",
                         rd_want, rd_hit, fq_cnt, pf_act, wst);
            // (2) 条目/队列不变量
            //  ⚠ §6.0 拆拍后，"分配到还有未回拍的条目"要在**落盘那一拍**查：
            //    al_ptr 才是真正被写的条目号（alloc_ptr 在那时已经前进过）。
            if (al_v && (ent_out[al_ptr] != SLOT_Z))
                $display("[CONV-UNI-WRAP] ERROR: 分配到还有未回拍的条目 ent=%0d out=%0d",
                         al_ptr, ent_out[al_ptr]);
            //  ★ 拆拍专项断言（2026-10-30）：落盘那一刻，不允许任何 ent_on=1 的
            //    条目已经持有同一个 tag —— 这正是"同 tag 分配两次"的可观测形态。
            //    比起原来的 `dem_alloc && rd_known`（只看同一拍），这条能覆盖
            //    "已决策未落盘"那 1 拍和"观察打拍"那 1 拍的竞争。
            if (al_v && dup_al)
                $display("[CONV-UNI-WRAP] ERROR: 同一 tag 被分配进两个条目 tag=%h al_ptr=%0d（拆拍去重失效）",
                         al_tag, al_ptr);
            if (dem_alloc && dup_q)
                $display("[CONV-UNI-WRAP] ERROR: 需求条目已在缓存却重复分配 tag=%h（看的是 rd_tag_q）",
                         rd_tag_q);
            if (pf_act_now && pf_known)
                $display("[CONV-UNI-WRAP] ERROR: 预取条目已在缓存却重复分配 tag=%h", pf_tag);
            if (start_fire && any_out)
                $display("[CONV-UNI-WRAP] ERROR: 启动时仍有未回拍（启动闸门失效）");
            if (fq_cnt > NFQ)
                $display("[CONV-UNI-WRAP] ERROR: 填充队列溢出 fq_cnt=%0d", fq_cnt);
            // (3) FIFO 溢出
            if (bf_cnt > BF) $display("[CONV-UNI-WRAP] ERROR: beat FIFO 溢出 bf_cnt=%0d", bf_cnt);
            if (df_cnt > DF) $display("[CONV-UNI-WRAP] ERROR: 描述符 FIFO 溢出 df_cnt=%0d", df_cnt);
            // (3b) 写口入口寄存器不变式（§7.0）：p_valid 只允许在 wst==W_IDLE 时
            //      为 1。违反 ⇒ 那个字节在排空期既没被合并、又会在下一拍被
            //      p_valid<=wr_byte 冲掉 = 静默丢一个字节。这条判断只有 1 个 LUT。
            if (p_valid && (wst != W_IDLE))
                $display("[CONV-UNI-WRAP] ERROR: 入口字节跨排空拍（会丢字节）wst=%0d", wst);
            // (4) R 回来但没有在飞请求（AR/R 记账错）
            if (m_axi_rvalid && (ent_out[rid_ent] == SLOT_Z))
                $display("[CONV-UNI-WRAP] ERROR: R 数据回来但该条目无在飞 rid=%0d ent=%0d",
                         m_axi_rid, rid_ent);
            if (r_fire && rid_hi_bad)
                $display("[CONV-UNI-WRAP] ERROR: RID 超出条目范围 rid=%0d", m_axi_rid);
            // (5) 写 burst legality
            if (m_axi_awvalid && m_axi_awready &&
                (({20'd0, m_axi_awaddr[11:0]} + ({4'b0000, m_axi_awlen} * 8)) > 12'd4095))
                $display("[CONV-UNI-WRAP] ERROR: 写 burst 跨 4 KB awaddr=%h awlen=%0d",
                         m_axi_awaddr, m_axi_awlen);
            if (m_axi_awvalid && (m_axi_awlen >= BU_MAX[7:0]))
                $display("[CONV-UNI-WRAP] ERROR: 写 burst 超过 WR_BURST_MAX awlen=%0d",
                         m_axi_awlen);
            // (6) 本层 AR 必须永远 8 字节对齐（不变量）
            if (m_axi_arvalid && m_axi_arready && (m_axi_araddr[2:0] != 3'd0))
                $display("[CONV-UNI-WRAP] ERROR: ARADDR 非 8 字节对齐 addr=%h", m_axi_araddr);
        end
    end
    // synopsys translate_on

// synthesis translate_on
endmodule
