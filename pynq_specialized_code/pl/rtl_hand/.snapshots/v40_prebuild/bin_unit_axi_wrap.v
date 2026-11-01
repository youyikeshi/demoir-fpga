// ============================================================================
//  bin_unit_axi_wrap.v —— 四个二值引擎单元的**通用 AXI 包装层**（唯一一份代码）
//
//  契约：docs/binary/03_新引擎接口契约与验收.md §7（本次实现的权威依据）
//  教训：docs/rtl_hand/上板性能优化_20261028.md
//        （k1 因"读仲裁一次只许一笔在飞"每笔单拍读付 ≈56 拍、整网慢 5.3×；
//          以及"burst 不能跨 4 KB""不能用门控撤销 arvalid"两条上板踩坑）
//  结构参考：modules/k1_axi_wrap.v（AXI4-Lite 从 + 两页式寄存器 + TAG 分流）
//
// ============================================================================
//  0. 内嵌单元怎么选（综合期参数 + generate if）
// ============================================================================
//      parameter int UNIT = 0 ;
//      UNIT = 0 → bin_conv_core （二值卷积 XNOR + popcount）
//      UNIT = 1 → bin_rprelu    （RPReLU 逐元素激活）
//      UNIT = 2 → bin_sam       （SAM 三分支加权）
//      UNIT = 3 → bin_interp    （双线性插值）
//  四个单元的单主内存口完全同构，所以**内存翻译逻辑只写一份**：
//      mem_rd_en/addr → AR 通道            mem_rd_data ← R 通道（§6）
//      mem_wr_en/addr/data → W 通道        （§7）
//  差异只在 cfg_*/base_* 的接线，全部收在 §4 的 generate 里。
//  用 generate-if 而不是 'ifdef：一个源文件即可综合出 4 个 IP，不需要维护
//  四套宏组合；未被选中的单元**不会被 elaborat**（所以库里只要有选中的那个）。
//
// ============================================================================
//  1. 寄存器映射（严格按契约 §7.1；**A[16]=1 为配置页**，与 k1/k3/k5 一致）
// ============================================================================
//  BD 里把本从机的地址范围设成 128 KB（如 0x4000_0000–0x4001_FFFF），
//  PS 侧用「配置页基址 +0x10000」访问下表，**偏移相对 +0x10000**。
//  A[16]=0 的那一页保留（写忽略、读回 0）。
//
//  偏移  名字              属性  说明
//  ----  ----------------  ----  ----------------------------------------------
//  0x00  AP_CTRL           RW    bit0 = start（写 1 启动，读回 0）；bit1 = auto-restart
//  0x10  AP_RETURN         RO    bit0 = done，bit1 = busy，**[15:8] = 错误码**
//  0x18  R_C               RW    通道数（conv 的 cin / rprelu·sam·interp 的 C）
//  0x1C  R_C_HI            RW    高 32 位（只允许 0；非 0 ⇒ ERR_SIZE）
//  0x20  R_H               RW    输入高（+0x24 高字，下同）
//  0x28  R_W               RW    输入宽
//  0x30  R_HO              RW    输出高（interp 用）
//  0x38  R_WO              RW    输出宽（interp 用）
//  0x40  R_K               RW    卷积核（conv 用）
//  0x48  R_S               RW    stride（conv 用）
//  0x50  R_D               RW    dilation（conv 用）
//  0x58  R_PAD             RW    pad（conv 用）
//  0x60  R_SHIFT           RW    输出右移（来自 meta.json）
//  0x68  R_ALPHA_SHIFT     RW    复位值 9 —— 契约 §1 的固定值
//  0x6C  R_SLOPE_SHIFT     RW    复位值 8 —— 契约 §1 的固定值
//  0x70  R_COUT            RW    cout（conv 用；**契约 §7.1 未列，本文件补**）
//  0x80  B_IN              RW    输入基址（+0x84 高字）
//  0x88  B_IN2             RW    第二输入（sam 的 y0；+0x8C 高字）
//  0x90  B_IN3             RW    第三输入（sam 的 y2；+0x94 高字）
//  0x98  B_IN4             RW    第四输入（sam 的 y4；+0x9C 高字）
//  0xA0  B_WGT             RW    权重基址 / sam 的 w0（+0xA4 高字）
//  0xA8  B_W2              RW    第二组权重（sam 的 w2，32 位，见 §1.1）
//  0xAC  B_W3              RW    第三组权重（sam 的 w4，32 位）
//  0xB0  B_BIAS            RW    偏置基址（+0xB4 高字）
//  0xB8  B_ZETA            RW    zeta 基址（32 位）
//  0xBC  B_GAMMA           RW    gamma 基址（32 位）
//  0xC0  B_SLOPE           RW    slope 基址（32 位）
//  0xD0  B_OUT             RW    输出基址（+0xD4 高字）
//  0xD8  B_ALPHA           RW    alpha_q 基址（**契约 §7.1 未列，本文件补**；
//                                conv 必需；+0xDC 高字）
//  0xE0  DBG_RD            RW    [15:0] 需求读拍数，[31:16] 填窗次数（写 0 清零）
//  0xE8  DBG_WR            RW    [15:0] AW 笔数，[31:16] W 拍数（写 0 清零）
//  0xF0  DBG_FLAG          RW    [0] 有 64 位基址的高字写过非 0
//                                [1] 单元发了非 8 字节对齐的读（硬件不变量）
//                                [2] RID 越界   [3] u_busy（只读位）
//                                写 bit0 清 sticky（[0]/[1]/[2]）
//
//  §1.1 两处"契约 §7.1 没写全"的地方（本文件的决定，均已在上面标注）：
//   · §7.1 没有 cout、没有 alpha_q 基址，而 bin_conv_core 两者都要 ⇒ 补在
//     0x70（契约里该区间为空）和 0xD8。
//   · 契约把 'B_W2 / B_W3' 挤在 0xA8 一行、'B_ZETA / B_GAMMA / B_SLOPE' 挤在
//     0xB8 一行。若每个 64 位基址占 8 字节槽，B_W3 会落到 0xB0 与 B_BIAS 撞车。
//     所以**这两行按 4 字节步进的 32 位寄存器**实现（0xA8/0xAC、0xB8/0xBC/0xC0），
//     其余基址仍是"低字在偏移、高字在 +4"的老规矩。四个单元的 base_* 全是
//     32 位，这样不损失任何能力。
//
// ============================================================================
//  2. 读侧：多笔在飞 + 一个绕不过去的"1 拍延迟"问题（**本节是设计核心**）
// ============================================================================
//  单元的内存口是 **BRAM 语义**：t 拍发 mem_rd_en + mem_rd_addr，
//  mem_rd_data 必须在 **t+1 拍**有效（四个单元的 TB 模型都是
//  'always @(posedge clk) if (mem_rd_en) mem_rd_data <= mem[addr];'）。
//  单元**没有**反压输入 ⇒ "数据来晚"就等于"算错"，不能靠等。
//
//  而一次 AXI 读的往返是几十拍（板上实测 ≈54 拍/term，见下）。两者不可能直接
//  对接，所以本 wrapper 用 **多线缓存 + 停单元时钟** 两件套：
//
//   ① **线（line）= RD_WIN_WORDS 个连续 64-bit 字**（默认 8 字 = 64 B，
//      2^k 字节对齐 ⇒ **永远不跨 4 KB**，见性能文档补记三；AXI4 禁止 burst 跨 4 KB）。
//      槽号 = word[IDX_W-1:0]（**算术派生、显式位宽**，位选写错一位会静默错数，见 §5）。
//   ② **命中**时 'rd_hit' 当拍成立 ⇒ mem_rd_data 仍按"1 拍延迟"给出，
//      单元**零气泡**、时间轴与直连 BRAM 完全一致。
//   ③ **未命中**时把单元的时钟停住（§5 的同步 CE），并填那条线；
//      整条到齐后再放行。单元只是被"拉长"，它的每一个自身周期都不变，
//      所以计算结果与直连 BRAM **逐位相同**（这是正确性的根）。
//
//  **2026-10-31 读侧优化（本文件这一轮的唯一改动目标）**
//  ------------------------------------------------------------------------
//  板上铁证（12 个只有 cin 不同的 k=1/32×32/cout=48 层，写工作量逐字节相同）：
//      cin  64 → K=1  → 3.24 ms → 8.2 cyc/term
//      cin 160 → K=3  → 64.32 ms → 54.5 cyc/term
//      cin 448 → K=7  → 149.58 ms → 54.3 cyc/term
//  时间比 1 : 1.329 : 1.995 : 2.326 == term 数比 1 : 1.333 : 2 : 2.333
//  ⇒ **PL 时间 ∝ term 数，cyc/term 恒为 54.3±0.2（cin>64）**。理想是 1 拍/term
//    ⇒ 53 拍全在等读。根因两条（都在这次改动之前的代码里）：
//    (a) `m_axi_arlen = 4'd0` 硬接 ⇒ 一条 64 B 线要发 8 笔单拍 AR；
//    (b) 只有**一条** 64 B 线窗口 ⇒ 相邻 term 的 b 跳 H·W·8（32×32 时 8 KB），
//        换线就得冲刷，**每个 term 必 miss ⇒ 每个 term 付一次完整往返**。
//  修法分两步（**分步落地、分步仿真、分步论证**；第 3 步 2D 预取本次不做）：
//    步骤① arlen 0 → RD_WIN_WORDS-1 = 一条线 1 笔 8 拍 INCR（省 AR 事务数）。
//    步骤② 单线 → **NLINES 条全相联线缓存**（吃掉"同一 (ky,kx,b)、相邻像素"
//           的重复访问，把"每 term 一次线填充"降到"每 8 个像素一次"）。
//
//  **步骤① 之后**（arlen=N-1）：
//    · 未命中 ⇒ 发**一笔 N 拍 burst**，地址恒为线基址（64 B 对齐），
//      8 拍 × 8 B = 64 B ⇒ **永不跨 4 KB**（AXI4 唯一的 burst 硬约束）。
//    · ARBURST/ARSIZE 不在端口上（BD 已接好，禁止加端口）：由 IP integrator 按
//      m_axi 的 DATA_WIDTH 钉成 INCR / 8 B。**已核对当前工程的 BD 展开网表**
//      （不是推测）：pl/vivado/proj_v2/engine.gen/sources_1/bd/design_1/synth/
//      design_1.v 里 `.S_AXI_HP1_ARBURST({1'b0,1'b1})`（=INCR）、
//      `.S_AXI_HP1_ARSIZE({1'b0,1'b1,1'b1})`（=8 B）、
//      `.S_AXI_HP1_ARLEN(bin_unit_0_m_axi_ARLEN)`（直连）、
//      `.S_AXI_HP1_ARID({1'b0,1'b0,bin_unit_0_m_axi_ARID})`（4→6 位零扩展）。
//      ⇒ 8 拍 burst 在板上是真 INCR。
//    · 槽号由**本模块的拍计数器**给：同一笔 burst 的 N 拍按 AXI 同 ID 保序返回
//      （单笔 burst 内本来就是顺序的）⇒ 第 k 拍必是线内第 k 个字。
//      `rlast` 与"第 N 拍"必须同拍（§9 有断言）。
//
//  **步骤② 之后**（NLINES 条全相联）：
//    · 每条目：ent_on（在用）+ ent_tag（线号）+ ent_rdy（**整条到齐**）+ ent_out
//      （未回拍数）+ ent_wr（下一个落哪个槽）+ win_q[{条目,槽}] 数据。
//    · **命中 = |(ent_on & ent_rdy & (ent_tag==rd_tag))**，同拍、纯组合（§6 有
//      为什么不能打拍的论证）。命中时 rd_ent 由位或归约给出（cam_rdy 是 one-hot）。
//    · 未命中且该线**不在缓存里** ⇒ 按 alloc_ptr 轮转选一个 **ent_out==0** 的条目
//      替换（在飞的条目绝不改标号），发一笔 burst，ARID = 条目号。
//    · 多条线可以同时在飞：R 拍按 m_axi_rid 落**各自的条目**，槽号用该条目自己的
//      计数器 ⇒ 两条线的数据不可能串（§6.5）。
//    · **只有整条线到齐才允许命中**（不做逐槽 valid）：rd_hit 的输入全是 FF，
//      组合锥只有"26 位比较 + 8 路或树"，与单线版同量级。conv 侧 NLINES=64 同款。
//    · 替换策略：轮转（FIFO）。本引擎的访问序是循环的、工作集固定 ⇒ 轮转等价 LRU。
//    · **NLINES 怎么定**：k=1 时每个像素摸 C_B=ceil(cin/64) 条线（b 跳 H·W·8），
//      一条线服务 8 个像素 ⇒ NLINES ≥ C_B 就把"每 term 一次填充"降到"每 8 像素
//      一次"。k=3 时工作集 = 3 行 × C_B，本网络最大 K=36（cin=256,k=3）⇒ C_B=4
//      ⇒ 工作集 12。AXI_ID_W=4 定死 NLINES ≤ 16 ⇒ **默认取满 16**
//      （= 2^4，ARID 刚好用满 4 位；8 条时 cin=256/k=3 会抖动，见参数表注释）。
//
//  **步骤③ 之后（2026-10-31 · 填充与计算重叠，本文件这一轮的唯一改动）**
//  ------------------------------------------------------------------------
//  基线 = binalloc 那一版的时序修复（al_v/al_ent/al_tag 决策级 + RD_WIN_WORDS=16
//  + cout%8 校验）。本步只动**读侧**，写侧/引擎/DSP 级联一个字没改。
//  步骤②的残余代价：**需求缺失**——未命中就把单元 CE 停住，等一整笔往返（≈65 拍）。
//  实测（64² golden，RLAT=55，逐拍 spy）：`u_ce=0` 的冻结拍占总拍数 34.9%（cin=48/k=3），
//  cin=96/CB=2 时占 53%。而这 1,064 笔填线**全是首次触碰的强制缺失**
//  （工作集 2~12 条 ≤ NLINES=16，命中率 92%）⇒ 能动的只剩"那 65 拍的冻结"。
//  ⇒ 做两件事，都不碰计算通路：
//    (a) **AR 队列 4 深**：分配不再被"上一笔 AR 还没握手"卡住（旧版 `!arvalid_r`），
//        连续几拍可以连发几笔；`arvalid` 只由"队列非空"驱动、队头只由握手弹出
//        ⇒ "挂住等握手时不许撤销/不许改地址"由结构保证（§9(3h) 还逐拍查）。
//        **ARID 仍是条目号 ⇒ AXI_ID_W=4 不用变、BD 不动**（16 个条目 ⇒ 至多 16 笔）。
//    (b) **按实际 term 序列预取**（`PF_AHEAD=1`）：引擎读地址在 ST_CMP 里每拍 +8，
//        一条线 16 字 ⇒ 一条线正好供 16 个连续像素，**下一条线就是当前线号+1**。
//        "任何一次被消费的读换了线号"就把它塞进预取队列，与需求缺失**非阻塞并行**
//        （`PF_MAX=4` 笔在飞）。预取也走 al_v 决策级，不另开长锥。
//        与 lane8 那个实测 −0.04% 的 `rdwin_pf` 的关键差别：旧版用
//        `ent_on_cnt+4 ≤ NLINES` 当预算门，而 `ent_on` 一旦置位永不落 0
//        ⇒ 工作集(12)一装满就**永久失效**（等于没开）；这里限的是**并发笔数**。
//  实测（RLAT=55，RD_WIN_WORDS=16，逐字节 PASS）：
//      bin_3x48_32_64x64  227,566 → **151,099** 拍（−33.6%）
//      bin_3x96_32_64x64  752,558 → **305,397** 拍（−59.4%）
//      bin_1x64_64_64x64  196,819 → **158,508** 拍（−19.5%）
//  前两个形状的改后拍数**低于同一形状在 RLAT=1 时的拍数**（170,036 / 422,020）
//  ⇒ 填线延迟被完全藏住。k=1 只藏住 ~47%：一条线只供 16 拍 < 往返 65 拍，
//  **领先量不足**（不是命中率不够：需求缺失已被预取压掉 97.3%，2096→56 笔）。
//  详见 _worktrees/mlp/REPORT_mlp_overlap.md。
//
//  ⇒ 步骤①+② 之后，k=1 的 cin>64 层从 **54.3 cyc/term** 降到
//    "每 8 个像素付 C_B 次往返" ⇒ ≈ (54+7)/8 ≈ **7.6 cyc/term**；
//    k=3/C_B=1 的层从 21 cyc/term 降到 ≈ 3 cyc/term（线填充次数按 1/24 摊）。
//    xsim 逐形状实测（含 RLAT 1..20 随机反压，8 个形状全部逐字节 PASS）见
//    REPORT_rdopt.md §4；填充次数/term 与这里推的 1/8 逐位吻合。
//    0 命中期也不可能比改前差（最坏退化成"每 term 一次填充 + 一样的一笔 burst"）。
//  ⚠ 已知代价（写在这里是为了不让人误判成 bug）：局部性差的单元会按线放大
//    读流量，放大上限 = RD_WIN_WORDS。典型是 bin_interp：一个输出像素要
//    4 个角点，通常分属 2 条线（同一行的 x0/x1 相邻，y0/y1 差一行）⇒ 2 条线
//    16 个字取 4 个字。**RD_WIN_WORDS 就是这个 trade-off 的旋钮**
//    （要省带宽就调小到 4，要吞吐就调到 16）。
//
// ============================================================================
//  3. 写侧：8-bit 写口怎么攒成 burst（"多少拍成包、不满足怎么排空"）
// ============================================================================
//  单元写口 8-bit、**写当拍生效**。C8 布局
//  addr(o,y,x) = (o/8)·Hout·Wout·8 + y·Wout·8 + x·8 + (o%8)，
//  实际只出现两种写模式：
//
//    (a) 同一 64-bit 字内 8 个连续字节（rprelu/sam/interp：一个像素组 = 8 通道
//        = addr+0..7）⇒ 8 拍写合成 **1 个 64-bit beat**（WSTRB = 8'hFF）。
//    (b) 跨像素同 lane（conv_core：一趟 oc 内 x 递增 ⇒ 地址恒 +8、lane=o%8 固定）
//        ⇒ 每字节各 1 拍（WSTRB = 1<<lane），但相邻像素的 beat 地址正好 +8
//        = AXI INCR 的下一个 beat，所以能串成**一笔最多 WR_BURST_MAX 拍的 burst**。
//
//  攒包规则（两种模式共用一套，自动都吃）：
//    · 一个 **beat** = 一个「64-bit 字对齐地址」上的 64-bit 数据 + 8-bit WSTRB。
//      字节落在哪个 lane 由**地址低 3 位**决定（B_OUT 只要求字节对齐，
//      不要求 8 字节对齐；也绝不把 AWADDR 摆成非对齐 —— AXI4 INCR 的拍地址
//      必须按传输尺寸对齐，非对齐首地址的语义会变成"首拍只写一部分字节"）。
//    · **攒 beat**：'mem_wr_addr[31:3]' 相同就继续合并（模式 a）；
//      不同就先把上一个 beat 推进 beat FIFO、再开新 beat（模式 b）。
//      整字凑满（WSTRB==8'hFF）立即推进，不等下一拍。
//    · **攒 burst**：新 beat 的字地址 == 当前 burst 首地址 + 8×已收拍数 ⇒ 串进
//      同一笔（AWLEN 自增）；否则收掉当前 burst、另起一笔。串的条件还包含
//      **不跨 4 KB**（字地址 [28:9] 不变 ⇔ 字节地址 [31:12] 不变）与
//      **拍数 < WR_BURST_MAX**（默认 16 拍 = 128 B，上限 256）。
//    · **排空**（2026-10-31 改写；旧规则是"1 字节 = 1 拍 = 1 笔 AW"的直接原因）：
//        旧规则 = "写口空闲就排空"，准确说是 `!u_wr_en && !p_valid` 同拍成立
//        （p_valid 是 wr_byte 打一拍 ⇒ 字节之间只空 1 拍时**不会**排空，空 2 拍
//        以上才会）。四个单元的实际写节奏：
//          – rprelu / sam / interp：ST_WR 里 8 拍连写 addr+0..+7（中间只有
//            wcnt 那一两拍的空隙）⇒ 旧规则基本不触发，**8 字节合并本来就是好的**；
//          – bin_conv_core：一个输出像素要算 K=C_B*k² 个 term 才写 1 个字节
//            （K=1..36+，即字节间隔 K 拍）⇒ 旧规则**每个字节都触发**，于是
//            "每个字节都把当前 burst 收笔"，1 笔 AW 只搬 1 个字节。
//        板上实测（218/218 个未溢出层，全是 conv）：cnt_wbeat == out_bytes、
//        且 203/218 层 cnt_burst == cnt_wbeat —— 就是这条规则造成的，
//        **与合并逻辑本身无关**（合并条件 merge_done 的写法是对的）。
//        新规则只有三个排空理由：
//        1) beat FIFO 到压力线（BF_PRESS）⇒ **强制**排空一笔，"有 beat 必有
//           描述符"，否则写引擎会因为没有描述符而空转、FIFO 永远排不出去（死锁）；
//        2) 单元已 done（unit_done_q 粘滞）⇒ 收尾，把最后一个字节/最后一笔落盘；
//        3) 写口连续 WR_GAP_TMO(=4095) 拍没有新字节 ⇒ 兜底（正常形状走不到）。
//        效果：模式 (b) 的连续字现在能一直串到 WR_BURST_MAX 拍（AW 笔数 ÷8）；
//        模式 (a) 不受影响（它本来就合并得上），只会少掉"半满字被空闲提前冲掉"。
//      排空用 2 拍小状态机：W_DR1 落 open beat（这一步本身可能又要收一笔，
//      因为不串），W_DR2 收 burst 描述符 —— 一拍卖不完两笔描述符。
//    · 写引擎 AX_IDLE→AW→W…→B 从两个 FIFO 取活；AWLEN 在描述符里定好，
//      wlast 由拍计数给出。done 上报前要求 'wr_all_idle'，保证 PS 看到
//      done=1 时内存已经一致（写响应也都收完了）。
//      ⚠ 这个引擎是**完全串行**的（一笔的 B 回来之前不发下一笔 AW）。
//        想再快就得做 AW 流水（2~4 笔在飞），方案与风险见 §7.10。
//
//  ⚠ 已知代价（改不掉的）：模式 (b) 是"1 字节 / 1 拍"，写带宽 8× 浪费。这是
//    bin_conv_core 的输出布局决定的 —— 一趟 oc 只填同一 lane，同一个 64-bit 字的
//    8 个 lane 分散在 **8 趟不同的 oc pass** 里（每趟隔一整个 Hout·Wout 光栅），
//    要凑成一拍必须缓存 Hout·Wout·8 字节，不可行；要根治得让单元改输出顺序
//    （= 改单元，本次硬约束不允许）。本文件能做的只有"把同 lane 的连续字串成
//    burst"，让 AW 笔数从"1 笔/字节"降到"1 笔/WR_BURST_MAX 字节"。
//    对照：同族的 conv_uni_axi_wrap/conv_int8_uni 是**单元**一拍吐一个字的 8 个
//    相邻字节（raddr_q+rcnt_q、rmask_q[rcnt_q]），所以它那边 8 字节真能攒成 1 拍；
//    bin 侧没有这个前提，别把两边的"写效率"直接对比。
//
// ============================================================================
//  4. 契约 §7.3 的两条强制约束（落成"启动前校验 + 拒绝启动 + 错误码"）
// ============================================================================
//   ① 权重基址必须 8 字节对齐（bin_sam 的三组 w0/w2/w4 按对齐 64-bit 字读 int16）。
//      本文件把它**推广到所有"要被 64-bit 读"的基址**：AXI4 要求传输地址按
//      传输尺寸对齐，基址不齐 ⇒ ARADDR[2:0]≠0 ⇒ 从机行为未定义。
//      conv: act/wgt/alpha/bias/zeta/gamma/slope；rprelu: in/zeta/gamma/slope；
//      sam: x/y0/y2/y4/w0/w2/w3；interp: in —— 全部要求 [2:0]==0，
//      否则 AP_RETURN[15:8] = **ERR_ALIGN(5)** 且不启动。
//   ② C 必须是 8 的倍数（rprelu/sam/interp 按 8 通道一组遍历 C8）。
//      不满足 ⇒ AP_RETURN[15:8] = **ERR_LAYOUT(4)** 且不启动。
//      （CHK_C8_ALL=1 时对 conv 的 cin 也一并要求 —— 契约 §7.3 就是这么写的；
//        严格说 conv 只要求 ceil(cin/64)·k² 装得进它的 256 字 wbuf，
//        想放宽就把 CHK_C8_ALL 置 0。）
//  顺手还会查（都是"不查就会悄悄算错"的项）：基址非 0（ERR_NULL）、
//  维度范围（ERR_SIZE）、alpha_shift/slope_shift 是否等于契约固定的 9/8
//  （ERR_SHIFT）、conv 的 wbuf 容量 ceil(cin/64)·k² ≤ 256（ERR_SIZE）、
//  以及 8 个 64 位基址的高 32 位是否为 0（非 0 ⇒ ERR_SIZE，与 k1 一致）。
//  **校验失败时置 done=1**，否则 PS 轮询 done 会永远等下去（k1 的经验）。
//
// ============================================================================
//  5. 时钟：单元用**同步时钟使能（CE）**，不再用门控时钟
// ============================================================================
//  ★ 2026-10-30（P3）：原来单元 ap_clk 接 'u_clk = s_axi_aclk & u_ce_lat'
//  （u_ce_lat 是"只在时钟低电平期跟随 u_ce"的锁存器，等价 BUFGCE）⇒ 第二套
//  时钟网络（LUT2+BUFG），跨域偏斜 −1.510 ns；那个 latch 自身 slack −1.875 ns
//  ⇒ 硅上 u_ce 可能比 RTL 晚一拍生效（仿真/硅不一致）。
//  现在改成：.ap_clk (s_axi_aclk) + .ap_ce (u_ce) —— 单一全局时钟 + 同步 CE。
//  u_ce=0 的那些沿，单元内部**一个 FF 都不更新**（与"时钟停振"逐拍等价）。
//  u_ce=0 的三个来源：
//    · 读侧未命中（等 AXI 数据回来）；
//    · 写侧 FIFO 已到压力阈值（bf_cnt ≥ BF_PRESS 或 df_cnt ≥ DF_PRESS）。
//      ⚠ 2026-10-30（时序优化 ③）：原来是"到阈值 **且** 本拍这个字节不是
//      '安全合并'（= 合并进一个已开着、合并后仍不完整的 beat）才放过去"的
//      **预测**写法。写口加了 1 拍入口寄存器（§7.0）后这个预测的基准整整差
//      一拍（预测时用的 be_*/be_strb 还没被上一个字节更新）⇒ 可能预测错 ⇒
//      在 FIFO 高位继续 push ⇒ 静默溢出。改成"到阈值就停单元"，并把
//      BF_PRESS/DF_PRESS 各提前 1 拍补偿 ⇒ 峰值仍是 BF-2（核算见 §7.9）。
//      代价：压力期会提前把一个没填满的字当 beat 冲出去（一次压力事件最多
//      多 1 个 beat + 1 个描述符）；写流量本来只有读侧的百分之几。
//    · 写侧排空状态机在跑 2 拍而单元这一拍又要写。
//  u_ce 只依赖单元自己的输出与本地寄存器 ⇒ 不会和 CE 成环。
//  **复位/空闲时 u_ce 恒 1**（单元照常推进），避免仿真起点 X 把 CE 卡成 X。
// ============================================================================
`timescale 1ns / 1ps

module bin_unit_axi_wrap #(
    // ---- 内嵌哪个单元（综合期常量）----
    parameter int  UNIT         = 0,      // 0=conv_core 1=rprelu 2=sam 3=interp
    // ---- 单元参数 ----
    parameter int  LANES        = 64,     // 仅 conv
    parameter int  COUT_MAX     = 192,    // 仅 conv
    parameter int  KMAX         = 7,      // 仅 conv（wbuf 容量另行校验）
    // ---- 通用校验上限 ----
    parameter int  C_MAX        = 4096,
    parameter int  HW_MAX       = 4096,
    parameter bit  CHK_C8_ALL   = 1'b1,   // 契约 §7.3-②：对 conv 的 cin 也查 %8
    // ---- 读窗口（多线全相联）----
    // ★ 2026-10-31：8 → 16（板端 binalloc 树同值）。AXI3 的 arlen 只有 4 bit（≤15）
    //   ⇒ **16 是硬上限**。一条线 16 字 = 128 B，把"每 8 个像素一次填充"减半。
    parameter int  RD_WIN_WORDS = 16,     // 每条线的字数（2/4/8/16，2 的幂）
    // ★ 2026-10-31：8 → 16（来自 lane8/patch/bin_unit_axi_wrap_rdwin_pf.patch，
    //   只取其中的 RD_WIN_WORDS 一项）。AXI3 的 arlen 只有 4 bit（≤15）⇒ 16 是硬上限。
    //   一条线 16 字 = 128 B，覆盖 16 个像素 ⇒ 线填充次数减半
    //   （实测 2.22 s → 1.03 s @RLAT=55）。代价：线缓存 16×16×8 = 2 KB BRAM。
    //   改回 8 只需改这一行。
    // ★ 步骤④（2026-10-31）：线缓存 **条目数**（直接映射，2 的幂：256/512/1024）
    //   全相联时代这个值被"ARID = 条目号、AXI_ID_W=4"卡死在 ≤16，而 256² 下
    //   工作集 = ceil(W/16) 条/行 × k 行 × cb 可以到 432 条 ⇒ 每个 term 重填。
    //   步骤④ 把 ARID 改成**填充槽号**（§6.5）后，条目数就只受 BRAM 限制了：
    //     一条线 = 16 字 × 8 B = 128 B ⇒ LC_N 条 = LC_N×128 B
    //     LC_N=256 → 32 KB = 7.1 BRAM36   LC_N=512 → 64 KB = 14.2
    //     LC_N=1024 → 128 KB = 28.4（板端现在只用 15/95 ⇒ 都装得下）
    //   默认 512 的理由（逐层统计见 REPORT_linecache.md §3）：256² 的 227 层里
    //   工作集最大的那一档是 216~256 条（cin=544,k=3,cb=9,W=128 一族）。512 条
    //   = 2× 工作集 ⇒ 直接映射的冲突概率足够低，而 1024 只把冲突再降一点、
    //   BRAM 却翻倍。**LC_N 是唯一的"覆盖率 vs BRAM"旋钮**。
    parameter int  LC_N         = 512,    // 线缓存条目数（2 的幂，≥ 2*RD_WIN_WORDS）
    // ★ 步骤④：填充槽数 = ARID 空间。一笔填充从"分配"到"最后一拍回来"独占一个槽，
    //   槽号就是 ARID（≤ 2^AXI_ID_W）。同时最多 AR_SLOTS(queue) + PF_MAX(在飞预取)
    //   + 1(需求) ≈ 9 笔 ⇒ 16 个槽有两倍余量；槽用完只会**暂停分配**（不会死锁，
    //   因为每笔填充最多 RD_WIN_WORDS 拍就归还）。
    parameter int  NSLOT        = 16,     // 填充槽数（必须 ≤ 2^AXI_ID_W）
    parameter int  AXI_ID_W     = 4,      // ARID/RID 宽度（BD 定死的端口宽度，不能加宽）
    // ---- ★ 2026-10-31 步骤③：多笔填充在飞 + 按实际 term 序列预取（填充/计算重叠）----
    //   ARID 仍 = **条目号**（AXI_ID_W=4 刚好用满 16 个条目），所以"N 笔在飞"
    //   不需要更宽的 ID：只要 N 笔各自打在不同的条目上即可（不变式：一笔 burst
    //   收完之前它的条目不会被重新分配）⇒ **AXI_ID_W 保持 4、BD/端口不动**。
    //   实测 BD：S_AXI_HP0_ID_WIDTH=6，而 S_AXI_HP0_ARID 是 4 位零扩展
    //   ⇒ 只有 ID ≥ 16 才会撞上扩展，我们永远用不到。
    parameter int  AR_SLOTS     = 4,      // AR 队列深度（= 已决定↔已握手之间最多几笔）
    parameter int  PF_EN        = 1,      // 预取总开关（0 = 退回纯需求缺失）
    parameter int  PF_AHEAD     = 1,      // 预取目标 = 当前观察到的线号 + PF_AHEAD
    parameter int  PF_DEPTH     = 4,      // 预取请求队列深度（提前排队的线号个数）
    parameter int  PF_MAX       = 4,      // 同时在飞的**预取**笔数上限（需求缺失不受限）
    // ---- 写侧 ----
    parameter int  WR_BUF_BEATS = 16,     // beat FIFO 深度（= 描述符 FIFO 深度）
    // ★ 2026-10-31（写回路径 ①②）：16 → **8**。
    //   §7.9 的逐拍核算给出 bf_cnt 峰值 = BF-2 = 14（BF=16、BF_PRESS=12）。
    //   旧值 16 > 14 ⇒ "16 拍描述符"**永远凑不出来**（要 ax_can_start 的
    //   bf_cnt >= 16，而 FIFO 最多只装得下 14）⇒ 一旦真凑出来就是死锁。
    //   降到 8 之后：8 <= 14 有 6 拍余量，且 8 拍描述符是**可证明可达**的
    //   （见 §7.9 "描述符可达性"一段），写侧不再有不可达/死锁状态。
    //   想换更大的 burst：必须同时把 WR_BUF_BEATS 提到 >= 2*WR_BURST_MAX
    //   （例：BURST_MAX=16 需要 BUF_BEATS=32 → 峰值 30 >= 16），别只改一个。
    parameter int  WR_BURST_MAX = 8       // 单笔写 burst 拍数上限（1..256，AXI3 上限 16）
)(
    // ---- AXI4-Lite slave ----
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

    // ---- AXI4 master：读（RD_WIN_WORDS 拍 INCR burst，ARID = 线缓存条目号）----
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
    // 0. 局部参数 / 错误码
    // ========================================================================
    localparam int IDX_W  = $clog2(RD_WIN_WORDS);          // 线内槽号位宽
    localparam int TAG_W  = 32 - 3 - IDX_W;                // 线号位宽
    localparam int LC_IW = $clog2(LC_N);                   // 表项号位宽（= 散列结果位宽）
    localparam int SLW   = $clog2(NSLOT);                  // 填充槽号位宽（= ARID 低位）
    localparam int DEPTH = LC_N * RD_WIN_WORDS;            // 线缓存总字数（一个平铺数组）
    localparam int DPW   = LC_IW + IDX_W;                  // = $clog2(DEPTH)
    localparam int BF     = WR_BUF_BEATS;                  // beat FIFO 深度
    localparam int BF_W   = $clog2(BF);
    localparam int BF_CW  = $clog2(BF + 1);
    localparam int DF     = WR_BUF_BEATS;                  // 描述符 FIFO 深度
    localparam int DF_W   = $clog2(DF);
    localparam int DF_CW  = $clog2(DF + 1);

    localparam [IDX_W:0]    PF_N      = RD_WIN_WORDS;      // 一条线的字数（= 一笔 burst 的拍数）
    localparam [IDX_W:0]    PF_Z      = {(IDX_W+1){1'b0}};
    localparam [IDX_W-1:0]  PF_LAST   = RD_WIN_WORDS - 1;
    // ★ 步骤①：一笔读 burst 的 ARLEN = 拍数-1（默认为 7 = 8 拍 = 一条 64 B 线）。
    //   AXI3 的 arlen 是 4 位、上限 16 拍（arlen=15）；RD_WIN_WORDS ≤ 16 时合法。
    localparam [3:0]        ARLEN_C   = RD_WIN_WORDS - 1;
    localparam [BF_W-1:0]   BF_LAST   = BF - 1;
    localparam [BF_CW-1:0]  BF_Z      = {(BF_CW){1'b0}};
    // 压力阈值：留 4 个空位（排空状态机还要塞 1~2 拍 + 入口寄存器差 1 拍）。
    //   2026-10-30（时序优化 ③）：原来是 BF-3。写口加了 1 拍入口寄存器（§7.0）
    //   之后，"吞字节那一拍"看到的 bf_cnt 与"真正 push 那一拍"的 bf_cnt 差 1
    //   （bf_cnt 每拍最多 +1）⇒ 阈值必须跟着提前 1 拍，否则 FIFO 峰值从 BF-2
    //   涨到 BF-1（余量只剩 0，反压久一点就静默溢出）。逐拍核算见 §7.9。
    localparam [BF_CW-1:0]  BF_PRESS   = (BF > 5) ? (BF - 4) : 1;
    localparam [DF_W-1:0]   DF_LAST   = DF - 1;
    localparam [DF_CW-1:0]  DF_Z      = {(DF_CW){1'b0}};
    localparam [DF_CW-1:0]  DF_PRESS   = (DF > 5) ? (DF - 4) : 1;
    localparam [8:0]        BU_MAX     = WR_BURST_MAX;     // burst 拍数上限（≤256）
    // 写口"耐心窗口"：连续多少拍没有新字节才允许**兜底排空**（§3 / §7.9）。
    //   必须大于"同一趟 oc 内相邻两个输出字节之间的最大拍距"，否则每个字节
    //   都会被兜底排空切成一笔（就是这个文件曾经的病）。
    //   单元写字节的拍距 ≈ K = C_B*k²（conv 每输出像素算 K 个 term 才写 1 字节）：
    //   本网络最大 K=36（cin=256,k=3）、片上实测 ~1800 拍以内；KMAX=7/CMAX=4096
    //   的极端形状 K 可达 3136 ⇒ 取 12 bit 饱和（4095）当上界，既覆盖极端形状，
    //   又保证"层结束/单元不再写"时最坏只多等 4095 拍（≈33 µs @125 MHz）。
    //   真正的收尾不靠它：u_done 会置 unit_done_q（粘滞），当拍/下一拍就排空。
    localparam int          WR_GAP_TMO = 4095;
    localparam [31:0]       C_LIM      = C_MAX;
    localparam [31:0]       HW_LIM     = HW_MAX;
    localparam [31:0]       COUT_LIM   = COUT_MAX;
    localparam [31:0]       K_LIM      = KMAX;

    // 错误码（AP_RETURN[15:8]）
    localparam [7:0] ERR_NULL   = 8'd1,   // 某必需基址为 0
                     ERR_SIZE   = 8'd2,   // 维度 / 容量越界
                     ERR_SHIFT  = 8'd3,   // 移位参数不是契约固定值
                     ERR_LAYOUT = 8'd4,   // C % 8 != 0              （契约 §7.3-②）
                     ERR_ALIGN  = 8'd5,   // 基址非 8 字节对齐         （契约 §7.3-①）
                     ERR_UNIT   = 8'd6;   // UNIT 参数不存在

    // 读侧 / 写侧小状态机编码
    //   ⚠ 读侧**没有状态机**：填充状态完全由"每条目一个 ent_out 计数器 + AR 寄存器"
    //     表达，"能不能开新线"就是 rd_quiet —— 与旧 RC_IDLE/RC_FILL 逐拍等价，
    //     但少了"状态机 vs 计数器"可能不一致的一类 bug（旧案：pf_abort 与
    //     rc_state 同拍更新时冲刷早/晚一拍的竞争）。
    localparam [1:0] W_IDLE  = 2'd0, W_DR1   = 2'd1, W_DR2 = 2'd2;
    localparam [2:0] AX_IDLE = 3'd0, AX_AW   = 3'd1, AX_W  = 3'd2, AX_B = 3'd3;

    // ========================================================================
    // 1. 全部信号声明（集中在此，避免依赖声明顺序）
    // ========================================================================
    // ---- 配置寄存器堆 ----
    logic [31:0] r_ctrl;
    logic [31:0] r_c, r_h, r_w, r_ho, r_wo;
    logic [31:0] r_k, r_s, r_d, r_pad;
    logic [31:0] r_cout, r_shift, r_alpha_shift, r_slope_shift;
    logic [31:0] r_in, r_in2, r_in3, r_in4;
    logic [31:0] r_wgt, r_w2, r_w3, r_bias;
    logic [31:0] r_zeta, r_gamma, r_slope_b, r_out, r_alpha;
    logic [7:0]  r_hi_bad;      // 每个 64 位基址的"高字写过非 0"位图

    // ---- ★ 2026-10-31：E1E2 运行期选择（R_RUN_UNIT，配置页 0x200）----
    //   UNIT 仍是**综合期**参数；本组寄存器只在 UNIT==0 的实例里真正起作用：
    //      0 = bin_conv_core（复位值，旧驱动逐位不变）
    //      4 = bin_e1e2     （= UNIT 编号，便于和 UNIT=4 的独立实例共用驱动）
    //   其它值 ⇒ ERR_UNIT。启动闸门把 run_unit 锁进 run_unit_q，运行中改寄存器
    //   不会切换 mux；start_fire 同时冲刷读线缓存，保证新单元看不到旧线的数据。
    logic [2:0]  run_unit;      // 配置寄存器（PS 写 0x200）
    // ════════════════════════════════════════════════════════════════════════
    //  ★ v40 时序刀 8（2026-10-31）：三个**准静态**控制信号的扇出锥 → max_fanout 复制
    // ------------------------------------------------------------------------
    //  150 MHz 实测（v39 routed DCP，20,706 条违例端点）的起点集中度：
    //      run_len_reg[3]_replica   7,124（34.4%） ← 已由 bin_e1e2 时序刀 7 消灭
    //      run_unit_q_reg[2]        2,375（11.5%） ← 本刀
    //      start_fire_reg           2,036（ 9.8%） ← 本刀
    //  后两个**都已经**是寄存器，而且只在"每次 launch"变一次：
    //      run_unit_q  在 start 那条边锁存（运行中改 R_RUN_UNIT 无效）；
    //      start_fire  是单拍脉冲。
    //  它们缺的不是"打拍"，是**复制**：一个 FF 驱动 3~5 千个 load 时，Vivado 只能
    //  搭多级 buffer 树，实测那条网每级 1.0~1.8 ns（见 v39_at150_top30.rpt 里
    //  fo=367 / fo=280 的两个网）。max_fanout=N 让综合器按 N 复制驱动器，
    //  每个副本就近服务一片 load。
    //  为什么**语义零变化**：复制品是被复制信号的同名副本，输入完全相同
    //  （寄存器复制 ⇒ 同一 D/C/CE/R；选择线复制 ⇒ 同一个 3 位比较），
    //  不引入任何状态、不改变任何拍。纯物理优化，只影响扇出网长度。
    //  值为什么取 32：这些信号各自几千个 load，32 个副本 ⇒ 每副本百来个 load，
    //  正好落在"一级 buffer 够"的量级；再小会开始浪费 FF 与布线资源。
    //  ⚠ 不许给"逐拍变化"的信号加这个属性（如 rd_hit / u_ce 的组合源）：
    //    那类信号复制后仍要跟每一拍的真实逻辑一致，复制不解决逻辑深度。
    //  ⚠ 实测教训（v40 第一次构建）：给 **start_fire**（单拍脉冲寄存器）加这个属性，
    //    综合器会生成 LDC 锁存器 `start_fire_reg_LDC` ⇒ 违反 Latch=0 门禁。
    //    ⇒ 该属性只留在 run_unit_q / sel_e1e2_q / sel_cp_q（三者实测不引 latch）。
    // ════════════════════════════════════════════════════════════════════════
    (* max_fanout = 32 *) logic [2:0]  run_unit_q;    // 启动时锁存的"本次运行单元"
    logic [31:0] r_egrp;        // 0x204 本命令的字组号（E1E2 cfg_group）
    logic [31:0] r_edesc;       // 0x208 描述符表基址（8 个 64-bit 字）
    logic [31:0] r_eout;        // 0x210 本字组输出基址
    logic [1:0]  r_e_hi_bad;    // 0/1 = B_E_DESC/B_E_OUT 的高字写过非 0
    (* max_fanout = 32 *) wire sel_e1e2_q;    // 本次运行的单元是不是 E1E2（锁存后）
    wire         sel_e1e2_c;    // 下一条 start 会选谁（配置值，给 vcode 用）

    // ---- E1E2 运行期 DBG：上板直接量"读吞吐 / 冻结占比"（0x230..0x240）----
    //   cnt_e_rd  : 真正被消费的 64-bit 读拍（= 单元读口吞吐，字节 = 8×它）
    //   cnt_e_cyc : E1E2 处于 run_active 的拍数
    //   cnt_e_rdst: 因读 miss 被冻结的拍数（u_ce=0 且单元在读）
    //   cnt_e_wrst: 因写 FIFO 压力被冻结的拍数（u_ce=0 且单元不在读）
    //   cnt_e_wr  : 真正被消费的 64-bit 写拍
    //   比值 cnt_e_rd/cnt_e_cyc 就是"每拍读几个字"；上板 ×8 B × f 即读带宽。
    logic [31:0] cnt_e_rd, cnt_e_cyc, cnt_e_rdst, cnt_e_wrst, cnt_e_wr;
    logic        clr_e_dbg;

    // ---- 运行控制 ----
    logic        start_pend;    // PS 已写 start，等读线静默后再真启动
    logic        start_req;     // 单拍脉冲 → 单元 ctrl_start
    // ⚠ v40 第一次构建实测（2026-10-31 19:41）：给 start_fire 加 (* max_fanout = 32 *)
    //   会让综合器生成一个 **LDC 锁存器** `start_fire_reg_LDC`（out_of_context 综合探针
    //   点名抓到），直接违反"Latch 必须 0"门禁 4 —— 因此这里**不加**该属性。
    //   代价核算：start_fire 起点只有 2,036 条违例端点、最差 slack −2.069 ns，
    //   本来就不是 binding 路径（最差的是 −5.331），复制它换不来 WNS；
    //   而一个 latch 会把门禁直接打红 ⇒ 收益远小于代价。
    //   （run_unit_q / sel_e1e2_q / sel_cp_q 的复制不会引 latch，保留。）
    logic        start_fire;    // 本拍发出 start_req（同时冲刷读线）
    logic        run_active;
    logic        unit_done_q;
    logic        done_sticky;
    logic [7:0]  err_code;
    logic        err_from_cfg;  // AP_RETURN[15:8] 的来源：1=启动校验，0=单元 err_code
    logic [7:0]  u_err;         // 选中单元的 err_code（仅 E1E2 有）
    logic        u_done, u_busy;
    logic [7:0]  vcode;
    logic [7:0]  vcode_conv, vcode_e, vcode_e_eff, vcode_cp_eff;

    // E1E2 选择线：配置值（给 vcode）和锁存值（给 start/CE/mem mux）。
    assign sel_e1e2_c = (UNIT == 4) || ((UNIT == 0) && (run_unit == 3'd4));
    assign sel_e1e2_q = (UNIT == 4) || ((UNIT == 0) && (run_unit_q == 3'd4));
    // ★ 2026-10-31（nonconv / cat 多源）：5 = bin_catpack（float32 多源 → 打包）。
    //   与 E1E2 复用同一组配置寄存器（0x18/0x20/0x28 + 0x204/0x208/0x210），
    //   只是描述符是"逐通道 64 项"而不是"8 lane"。
    wire         sel_cp_c;
    (* max_fanout = 32 *) wire sel_cp_q;
    assign sel_cp_c = (UNIT == 5) || ((UNIT == 0) && (run_unit == 3'd5));
    assign sel_cp_q = (UNIT == 5) || ((UNIT == 0) && (run_unit_q == 3'd5));
    // R_RUN_UNIT 合法值集合（链条里只留一次比较，别把它摊进优先级链）
    wire         run_unit_ok;
    assign run_unit_ok = (run_unit == 3'd0) || (run_unit == 3'd4) || (run_unit == 3'd5);
    // ⚠ vcode 是**一整条配置校验优先级链**（本文件 UNIT=0 时 13 级），而 run_active /
    //   start_req 的时钟使能挂在 `vcode == 0` 上 ⇒ 综合后最差路径是
    //   `r_k_reg[6]/C → vcode_q_reg[1]/D`（全芯片 150 MHz，WNS −5.338 ns，与数据通路
    //   无关）。两刀切断（2026-10-30，正文见 §3）：
    //     ① vcode_q <= vcode   —— 切断 vcode → run_active/start_req 的时钟使能（本来就有）；
    //     ② vcode_lo_q         —— 把 13 级链拆成 6 级（前缀）+ 7 级（各 UNIT）两段，
    //                             第二段变成 vcode = f(vcode_lo_q, r_*)。
    //   配置只在启动前通过 AXI-Lite 写入（串行事务：do_wr 要求 !bvalid ⇒ 相邻两笔写
    //   至少隔 3 拍），而启动闸门还要等 rd_quiet，所以 start 那一拍读到的 vcode_q
    //   一定反映"最后一次配置写完"的状态（逐拍推导见 §3.1）。
    logic [7:0]  vcode_q;
    // ---- vcode 两段化的中间寄存器（§3）----
    logic [7:0]  vcode_lo;      // 第一段：前缀 6 条（UNIT/高字/H,W/C/shift/C%8）
    logic [7:0]  vcode_lo_q;    // 第一段结果，每拍刷新（与 vcode→vcode_q 同类）
    // ---- §3 的叶子项：k² 也打一拍（否则 k² 与 cb·k² 是两级串联乘法）----
    logic [6:0]  kk_q;
    // ---- §3b 的叶子项再往前一步（第二轮，2026-10-30 晚）----------------------
    //   综合后全芯片 WNS = −2.360 ns，路径 `r_c_reg[8]/C → vcode_q_reg[?]/D`
    //   ⇒ 第二段（3b）里唯一还吃 r_c 的项就是 ktot：r_c → +63/>>6 的 13 bit
    //   进位链 → 7×7 乘法 → 14 bit 比较 → 6 级 mux → vcode_q。
    //   三刀（全部与 kk_q 同一手法，都是"纯派生量"，不改任何检查条件）：
    //     ① cb_q        —— 乘数也变成 FF（与 kk_q 配对，DSP 两边都从 FF 出发）
    //     ② ktot_bad_q  —— **整条判据**登记成一拍：FF → DSP → 比较 → FF，
    //                      3b 里只剩 1 bit 的 FF，r_c 到 vcode 的路彻底断开
    //     ③ sh_bad_q    —— 固定移位判据（第 7 条，链里最深的那条）同样登记
    //   三者都比 r_* 晚 1 拍（与 vcode_lo_q / kk_q 同相），§3.1 的"启动闸门在
    //   W+4 才取 vcode_q、而 vcode_q 从 W+3 起就正确"余量仍然 ≥1 拍。
    logic [6:0]  cb_q;          // ceil(cin/64) = C_B（ktot 的另一个乘数）
    logic        ktot_bad_q;    // (C_B*k² > 256)
    logic        sh_bad_q;      // (alpha_shift != 9) || (slope_shift != 8)

    // ---- 单元内存口 ----
    logic        u_rd_en;
    logic [31:0] u_rd_addr;
    logic [63:0] u_rd_data;     // 由 §6 驱动
    logic        u_wr_en;
    logic [31:0] u_wr_addr;
    logic [63:0] u_wr_data;      // ★ 64-bit：UNIT=0（8-lane 引擎）一拍吐 8 个相邻字节
    logic [7:0]  u_wr_sel;       // ★ 字节使能（UNIT!=0 的单元恒 8'hFF ⇒ 走原来的单字节路径）
    logic        u_wr_64;        // ★ 该单元是否有 64-bit 写口（只有 UNIT=0 有）
    logic        u_wr_wide_w;    // ★ 本拍是"整字"写（8 字节 + 全 1 WSTRB）
    logic        wide_pend;      // ★ 单元这一拍**摆着**一个整字写（不含 u_ce；时序刀 5）
    logic        wr_wide_b;      // ★ 真正被吞下的整字写（= wide_pend && u_ce）
    logic        wide_chain, push_chain_x;
    logic [28:0] push_word_x;

    // ---- AXI4-Lite 从 ----
    logic        aw_done, w_done, bvalid;
    logic [31:0] awaddr_l, wdata_l;
    logic [3:0]  wstrb_l;
    logic [31:0] araddr_l;
    logic        rvalid;

    // ---- 时钟门控 ----
    logic        u_ce;                 // ★ P3: 单元同步 CE（不再是门控时钟）


    // ---- 读线缓存 —— 步骤④：**直接映射** LC_N 条线，每条 RD_WIN_WORDS 个 64-bit 字 ----
    //   win_q 是一个平铺数组（下标 = {表项号, 槽号}）。1 个写口 + 1 个"带输出寄存器
    //   的读口"（u_rd_data）⇒ 正好是单时钟 BRAM 的推断模板。
    //   ★ ram_style 显式钉成 block：LC_N=512 时这是 512 Kb，一旦被推断成分布式 RAM
    //     就会溶成 8192 个 64-bit 宽的组合 mux（conv 侧踩过 8-4767 / −10.5 ns 那次）。
    (* ram_style = "block" *) logic [63:0] win_q [0:DEPTH-1];
    // ★ 2026-10-31 阶段②（面积）：`lc_tag` 由 "25 bit 线号" 改成 **{valid, 线号高位}**，
    //   并且**删掉 `lc_valid` / `lc_busy` 两个 512 位向量**（实测它们合计 1,120 LUT：
    //   608 + 512 —— 两个"双写口位向量"各带一套 512 路译码器，见 REPORT_wrapopt_stage1.md §2）。
    //   · 写口只剩**一个**源：alloc 时写 {1'b1, al_tag[高 21 位]} ⇒ 单写口 ⇒ 仍是 LUTRAM。
    //     （今天 `lc_valid` 之所以落成 FF，就是因为它有 alloc 清 0 / 到齐置 1 两个源。）
    //   · **"写了 tag"就等于"这条线已经在用的/in-flight"** ⇒ 命中必须再与
    //     `!fill_in_flight(rd_idx)` 相与（见 §6.2）。fill_in_flight 由填充槽表 `fs_*`
    //     给出，**不再需要 lc_busy 这个冗余向量**：
    //        lc_busy[i]  ≡  ∃s: fs_valid[s] & (fs_idx[s]==i)     （同拍置、同拍清）
    //   · 低 IDX_W 位（行内线号）**由条目号蕴含**（散列把 tag 低位原样放在 idx 低位，
    //     而条目一定写在 hash(tag) 上）⇒ 只存高 TAG_W-IDX_W = 21 位，比较也只比 21 位。
    //   · 跨次运行必须让旧条目失效 ⇒ 每次 start 前用 **512 拍**把整个阵列写 0（§5）
    //     （原来是在 start_fire 那一拍用 lc_valid<=0 一次清掉；单写口 LUTRAM 做不到一次清）。
    localparam int VLD = TAG_W - IDX_W;             // 条目内"有效位"的位置（最高位）
    localparam int TGW = TAG_W - IDX_W + 1;         // 条目宽度 = valid + tag 高位
    //   ⚠ 不要给这个阵列加 `ram_style`：钉成 "distributed" 时综合器会用另一种（更宽的）
    //     打包方式，实测 LUT 总数 15,044 vs 14,938（不钉属性更好）。宽度 22 的打包本身
    //     比旧的 25 位更费 primitive（720 vs 544 LUTRAM，见 REPORT_wrapopt_stage2.md §4），
    //     但逻辑侧省下的 1,271 LUT 远大于它。
    logic [TGW-1:0] lc_tag [0:LC_N-1];
    // ★ 直接映射特有的**新风险**（全相联时代没有）：一个新 miss 会覆盖一个可能
    //   正在回填的表项 ⇒ 迟到的 R 拍会写进"新线"的同号槽 = 静默错数据。
    //   闸门 = `!fill_in_flight(idx)`（§9(4a) 有逐拍断言），语义与旧 lc_busy 逐位相同。
    //
    // ---- 填充槽表：ARID = **槽号**（不是表项号），与 LC_N / 条目号彻底解耦（§6.5）----
    logic [NSLOT-1:0]        fs_valid;                 // 槽被一笔填充占用
    logic [LC_IW-1:0]        fs_idx  [0:NSLOT-1];      // 这笔填充的目标表项
    logic [IDX_W:0]          fs_out  [0:NSLOT-1];      // 该槽未回拍数（0..N）
    logic [IDX_W-1:0]        fs_wr   [0:NSLOT-1];      // 该槽下一拍落哪个字
    logic [NSLOT-1:0]        fs_pf;                    // 该槽这笔填充是预取
    logic [SLW-1:0]          fs_v;                     // 本拍可用的空槽号
    logic                    fs_ok, any_out;
    // ---- 命中 / 分配 ----
    logic [LC_IW-1:0]        rd_idx;                   // 需求读的表项号 = lc_hash(rd_tag)
    logic [SLW-1:0]          rid_slot;                 // R 拍落在哪个填充槽
    logic [LC_IW-1:0]        rid_idx;                  // 该槽的目标表项
    logic [IDX_W-1:0]        rid_wr;                   // 该槽下一拍落哪个字
    logic                    rid_last;
    logic                    rd_hit, rd_want, rd_miss;
    logic                    alloc_en, dec_en, do_alloc, arq_room;
    logic                    rid_hi_bad;
    // ★ 步骤④：决策级（"观察 → 决策 → 落盘"三级里的中间级）。
    //   全相联时代的锥是 ent_out[0..15] → 16 路优先扫描 → alloc_ok/vcand →
    //   ent_tag[*].CE（14 级 / 8.12 ns，11039 个端点违例）。直接映射下**没有扫描、
    //   没有轮转指针**，决策级只剩"锁存 idx/tag/槽号"，落盘级做 5 个写口。
    logic                    al_v;
    logic [LC_IW-1:0]        al_idx;
    logic [TAG_W-1:0]        al_tag;
    logic [SLW-1:0]          al_s;
    logic                    al_pf;

    // ---- ★ 步骤③（保留）：AR 队列（多笔在飞）+ 预取队列 ----
    //   arq_*  = 已决定、待/正在握手的 AR（{线基址, 填充槽号}）。表头直接驱动
    //            m_axi_ar*，只有 ar_fire 才弹出 ⇒ **arvalid 不可能在握手前撤销**。
    //   pf_*   = 要提前填的线号队列 + 记账。
    localparam int ARQ_W  = $clog2(AR_SLOTS + 1);
    localparam int ARP_W  = (AR_SLOTS <= 1) ? 1 : $clog2(AR_SLOTS);
    localparam int PFQW_W = $clog2(PF_DEPTH + 1);
    localparam int PFW_W  = (PF_DEPTH <= 1) ? 1 : $clog2(PF_DEPTH);
    localparam [ARQ_W-1:0]  ARQ_FULL = AR_SLOTS;
    localparam [PFQW_W-1:0] PFQ_FULL = PF_DEPTH;
    localparam [PFQW_W-1:0] PFQ_Z    = {PFQW_W{1'b0}};
    localparam [2:0]        PF_MAX3  = PF_MAX;
    localparam [TAG_W-1:0]  PF_A     = PF_AHEAD;
    logic [ARQ_W-1:0]        arq_cnt;
    logic [ARP_W-1:0]        arq_wp, arq_rp;
    logic [31:0]             arq_addr [0:AR_SLOTS-1];
    logic [AXI_ID_W-1:0]     arq_id   [0:AR_SLOTS-1];
    logic [TAG_W-1:0]        pf_tag   [0:PF_DEPTH-1];
    logic [PFQW_W-1:0]       pf_cnt;
    logic [PFW_W-1:0]        pf_wp, pf_rp;
    logic [2:0]              pf_busy;              // 在飞的预取笔数
    logic [TAG_W-1:0]        obs_tag_q;            // 上一次被消费的读的线号
    logic [15:0]             cnt_pf;               // 预取填充计数（只诊断，0xF4）
    logic                    pf_inq, pf_head_known;
    logic                    pf_push, pf_issue, pf_drop;
    logic [TAG_W-1:0]        pf_tgt, pf_head;
    logic [PFW_W-1:0]        pq_idx;
    logic [IDX_W-1:0]        rd_slot;
    logic [TAG_W-1:0]        rd_tag;
    // ---- ★ 阶段②：start 前把整个 tag 阵列写 0 的冲刷计数器（LC_N 拍）----
    //   单写口 LUTRAM 无法一拍清空，只能顺序写；start 闸门本来就等在 rd_quiet 上，
    //   所以这 512 拍只是让 start 晚 5.1 µs（@100 MHz），不影响任何数据通路。
    logic                    flush_busy;
    logic [LC_IW-1:0]        flush_ptr;
    logic                    ar_fire, r_fire;
    logic                    rd_quiet;
    integer cp3, ce;

    // ---- 写侧 ----
    logic        be_open;               // open beat 有效
    logic [28:0] be_word;               // open beat 的字地址
    logic [63:0] be_data;
    logic [7:0]  be_strb;
    logic [63:0] bf_data [0:BF-1];      // beat FIFO
    logic [7:0]  bf_strb [0:BF-1];
    logic [BF_W-1:0] bf_wp, bf_rp;
    logic [BF_CW-1:0] bf_cnt;
    logic [31:0] df_addr [0:DF-1];      // burst 描述符 FIFO
    logic [8:0]  df_len  [0:DF-1];
    logic [DF_W-1:0] df_wp, df_rp;
    logic [DF_CW-1:0] df_cnt;
    logic [28:0] bu_base;               // 当前 burst 首 beat 的字地址
    logic [8:0]  bu_len;                // 当前 burst 已收拍数
    logic [28:0] bu_next;               // = bu_base + bu_len 的**寄存器**副本（§7.0b）
    logic [1:0]  wst;
    logic        wr_take, wr_byte, wr_fifo_press, drain_cond;
    // ---- 写口"耐心窗口"计数器（2026-10-31，§3 / §7.9）----
    //   wr_byte=1 ⇒ 清 0；否则饱和 +1。到 WR_GAP_TMO ⇒ wr_gap_tmo=1（兜底排空）。
    //   全部是 FF（计数器 + 比较），不在 u_ce / rd_hit 那条组合锥上 —— 这也是
    //   把旧式 drain_cond 里的 `!u_wr_en` 换成它之后**时序只会更好**的原因。
    logic [11:0] wr_gap_cnt;
    logic        wr_gap_tmo;
    // ---- 写口入口寄存器（1 拍）：把"吞字节"与"合并/记账"切成两级流水 ----
    logic        p_valid;               // 入口寄存器里有一个已吞下、尚未合并的字节
    logic [31:0] p_addr;                // 该字节的字节地址（w_lane/w_word 由它派生）
    logic [63:0] p_data;                // 该字节的数据
    // ★ 2026-10-31 时序：8-lane **整字**写口也插入口寄存器（与 §7.0 字节路径同构）。
    //   原来那句 `push_beat(u_wr_addr[31:3], u_wr_data, 8'hFF, push_chain_x)` 把
    //   bf_data/bf_strb/df_addr/df_len 的 D 端直接接在"引擎读地址 → win_q(BRAM)
    //   → rd_hit → 合并"那条 14 级 / 8.57 ns 的锥上（端点 2,408 + 528 个）。
    //   写口无延迟契约 ⇒ 固定 1 级流水，吞吐仍是 1 beat/拍。
    // ★ 第 2 刀：禁止 opt_design 把这些入口寄存器吸收进下游组合锥
    logic        pw_valid;              // 入口寄存器里有一个已吞下的整字 beat
    logic [28:0] pw_word;               // 该 beat 的字地址
    logic [63:0] pw_data;               // 该 beat 的数据（WSTRB 恒 8'hFF）
    logic        p_merge;               // 本拍把入口寄存器里的字节并进 be_*
    logic        merge_done, merge_change, drain_pub, push_now, push_chain;
    logic        desc_push, bf_pop, desc_pop, ax_can_start;
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

    // ---- 调试（计数器/标志各自的驱动只在一个 always_ff 里；清零用脉冲）----
    logic [15:0] cnt_rdreq, cnt_fill, cnt_burst, cnt_wbeat;
    logic        flg_rd_misalign, flg_rid_bad;
    logic        clr_cnt_rd, clr_cnt_wr, clr_flg;   // 写 0xE0/0xE8/0xF0 产生的 1 拍脉冲

    // ========================================================================
    // 2. AXI4-Lite 写通道 + 寄存器堆（结构照抄 k1_axi_wrap，扩了寄存器表）
    // ========================================================================
    wire aw_hs = s_axi_awvalid && s_axi_awready;
    wire w_hs  = s_axi_wvalid  && s_axi_wready;

    assign s_axi_awready = !aw_done && !bvalid;
    assign s_axi_wready  = !w_done  && !bvalid;

    wire do_wr = (aw_done || aw_hs) && (w_done || w_hs) && !bvalid;

    wire [31:0] wr_addr = aw_done ? awaddr_l : s_axi_awaddr;
    wire [31:0] wr_data = w_done  ? wdata_l  : s_axi_wdata;
    wire [3:0]  wr_strb = w_done  ? wstrb_l  : s_axi_wstrb;
    wire        wr_page = wr_addr[16];      // 1=配置页 0=保留页
    wire [7:0]  wr_off  = wr_addr[7:0];

    function automatic [31:0] bmerge(input [31:0] old, input [31:0] nw, input [3:0] stb);
        bmerge[7:0]   = stb[0] ? nw[7:0]   : old[7:0];
        bmerge[15:8]  = stb[1] ? nw[15:8]  : old[15:8];
        bmerge[23:16] = stb[2] ? nw[23:16] : old[23:16];
        bmerge[31:24] = stb[3] ? nw[31:24] : old[31:24];
    endfunction

    // 高字槽 → r_hi_bad 位（0 表示"这个偏移不是高字槽"）
    function automatic [7:0] hi_bit_of(input [7:0] off);
        case (off)
            8'h84: hi_bit_of = 8'h01;   // B_IN   hi
            8'h8C: hi_bit_of = 8'h02;   // B_IN2  hi
            8'h94: hi_bit_of = 8'h04;   // B_IN3  hi
            8'h9C: hi_bit_of = 8'h08;   // B_IN4  hi
            8'hA4: hi_bit_of = 8'h10;   // B_WGT  hi
            8'hB4: hi_bit_of = 8'h20;   // B_BIAS hi
            8'hD4: hi_bit_of = 8'h40;   // B_OUT  hi
            8'hDC: hi_bit_of = 8'h80;   // B_ALPHA hi
            default: hi_bit_of = 8'h00;
        endcase
    endfunction

    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) begin
            aw_done <= 1'b0; w_done <= 1'b0; bvalid <= 1'b0;
            awaddr_l <= 32'd0; wdata_l <= 32'd0; wstrb_l <= 4'd0;

            r_ctrl <= 32'd0;
            r_c <= 32'd0; r_h <= 32'd0; r_w <= 32'd0; r_ho <= 32'd0; r_wo <= 32'd0;
            r_k <= 32'd0; r_s <= 32'd0; r_d <= 32'd0; r_pad <= 32'd0;
            r_cout <= 32'd0; r_shift <= 32'd0;
            // 契约 §1 把这两个移位固定成 9/8 ⇒ 复位就写死固定值，
            // 软件不写 0x68 也不会算错（写 0x68/0x6C 仍可覆盖，但 start 会校验）。
            r_alpha_shift <= 32'd9;
            r_slope_shift <= 32'd8;
            r_in <= 32'd0; r_in2 <= 32'd0; r_in3 <= 32'd0; r_in4 <= 32'd0;
            r_wgt <= 32'd0; r_w2 <= 32'd0; r_w3 <= 32'd0; r_bias <= 32'd0;
            r_zeta <= 32'd0; r_gamma <= 32'd0; r_slope_b <= 32'd0;
            r_out <= 32'd0; r_alpha <= 32'd0;
            r_hi_bad <= 8'd0;
            run_unit <= 3'd0; run_unit_q <= 3'd0;
            r_egrp <= 32'd0; r_edesc <= 32'd0; r_eout <= 32'd0;
            r_e_hi_bad <= 2'd0;

            start_req <= 1'b0; run_active <= 1'b0; unit_done_q <= 1'b0;
            done_sticky <= 1'b0; err_code <= 8'd0; vcode_q <= 8'd0;
            err_from_cfg <= 1'b0;
            flush_busy <= 1'b0; flush_ptr <= {LC_IW{1'b0}};

            // 调试计数器 / 标志（cnt_*、flg_*）**不在本块复位**：
            // 它们各自只在自己的 always_ff 里被驱动（多块驱动同一个信号
            // 不可综合，也会在仿真里"谁后写谁算"）。本块只驱动清零脉冲。
            clr_cnt_rd <= 1'b0; clr_cnt_wr <= 1'b0; clr_flg <= 1'b0;
        end else begin
            start_req <= 1'b0;              // start 只维持 1 拍
            start_fire <= 1'b0;
            clr_cnt_rd <= 1'b0;             // 三个清零脉冲都只维持 1 拍
            clr_cnt_wr <= 1'b0;
            clr_flg    <= 1'b0;
            clr_e_dbg  <= 1'b0;

            if (aw_hs) begin awaddr_l <= s_axi_awaddr; aw_done <= 1'b1; end
            if (w_hs)  begin wdata_l <= s_axi_wdata; wstrb_l <= s_axi_wstrb; w_done <= 1'b1; end

            if (do_wr) begin
                aw_done <= 1'b0;
                w_done  <= 1'b0;
                bvalid  <= 1'b1;

                if (wr_page) begin
                    // ---- 高字槽：写 0 清"坏"位、写非 0 置位（start 时 ERR_SIZE）----
                    if (hi_bit_of(wr_off) != 8'd0) begin
                        if (wr_data != 32'd0) r_hi_bad <= r_hi_bad |  hi_bit_of(wr_off);
                        else                  r_hi_bad <= r_hi_bad & ~hi_bit_of(wr_off);
                    end
                    // ★ 0x00..0xFF 是原有配置页；0x100..0x3FF 是新增扩展页
                    //   （R_RUN_UNIT / E1E2 描述符）。两级 case 保证原有偏移的
                    //   行为**逐位不变**（旧驱动一个字都不用改）。
                    if (wr_addr[9:8] == 2'b00) begin
                    case (wr_off)
                    // ---------------- 控制 / 状态 ----------------
                    8'h00: begin
                        r_ctrl <= bmerge(r_ctrl, wr_data, wr_strb);
                        if (wr_strb[0] && wr_data[0] && !run_active) begin
                            // 只登记"要启动"，真正的 start_req 由下面的运行控制
                            // 在读线静默（rd_quiet）后发出 —— 否则上一跑还在飞的
                            // 预取数据会落到新线的槽里（静默数据错）。
                            // 先把 done 清 0，免得 PS 把上一跑的 done 当成这一跑的。
                            start_pend  <= 1'b1;
                            done_sticky <= 1'b0;
                            err_code    <= 8'd0;
                            // 新命令还没选单元：先让 AP_RETURN 读 err_code（0），
                            // 避免残留上一跑的 E1E2 err_code 被当成新错误。
                            run_unit_q  <= 3'd0;
                            err_from_cfg <= 1'b0;
                        end
                    end
                    // ---------------- 维度 / 卷积参数 ----------------
                    8'h18: r_c    <= bmerge(r_c,    wr_data, wr_strb);
                    8'h20: r_h    <= bmerge(r_h,    wr_data, wr_strb);
                    8'h28: r_w    <= bmerge(r_w,    wr_data, wr_strb);
                    8'h30: r_ho   <= bmerge(r_ho,   wr_data, wr_strb);
                    8'h38: r_wo   <= bmerge(r_wo,   wr_data, wr_strb);
                    8'h40: r_k    <= bmerge(r_k,    wr_data, wr_strb);
                    8'h48: r_s    <= bmerge(r_s,    wr_data, wr_strb);
                    8'h50: r_d    <= bmerge(r_d,    wr_data, wr_strb);
                    8'h58: r_pad  <= bmerge(r_pad,  wr_data, wr_strb);
                    8'h60: r_shift <= bmerge(r_shift, wr_data, wr_strb);
                    8'h68: r_alpha_shift <= bmerge(r_alpha_shift, wr_data, wr_strb);
                    8'h6C: r_slope_shift <= bmerge(r_slope_shift, wr_data, wr_strb);
                    8'h70: r_cout <= bmerge(r_cout, wr_data, wr_strb);
                    // ---------------- 基址 ----------------
                    8'h80: r_in    <= bmerge(r_in,    wr_data, wr_strb);
                    8'h88: r_in2   <= bmerge(r_in2,   wr_data, wr_strb);
                    8'h90: r_in3   <= bmerge(r_in3,   wr_data, wr_strb);
                    8'h98: r_in4   <= bmerge(r_in4,   wr_data, wr_strb);
                    8'hA0: r_wgt   <= bmerge(r_wgt,   wr_data, wr_strb);
                    8'hA8: r_w2    <= bmerge(r_w2,    wr_data, wr_strb);
                    8'hAC: r_w3    <= bmerge(r_w3,    wr_data, wr_strb);
                    8'hB0: r_bias  <= bmerge(r_bias,  wr_data, wr_strb);
                    8'hB8: r_zeta  <= bmerge(r_zeta,  wr_data, wr_strb);
                    8'hBC: r_gamma <= bmerge(r_gamma, wr_data, wr_strb);
                    8'hC0: r_slope_b <= bmerge(r_slope_b, wr_data, wr_strb);
                    8'hD0: r_out   <= bmerge(r_out,   wr_data, wr_strb);
                    8'hD8: r_alpha <= bmerge(r_alpha, wr_data, wr_strb);
                    // ---------------- 调试（只发清零脉冲，不直接驱动计数器）----
                    8'hE0: clr_cnt_rd <= 1'b1;
                    8'hE8: clr_cnt_wr <= 1'b1;
                    8'hF0: if (wr_strb[0]) clr_flg <= 1'b1;
                    default: ;
                    endcase
                    end else begin
                        // ---------------- 0x200.. 扩展页 ----------------
                        case (wr_addr[9:0])
                        // R_RUN_UNIT：0=conv（复位值），4=E1E2；其它值启动时 ERR_UNIT
                        10'h200: run_unit <= bmerge({29'd0, run_unit}, wr_data, wr_strb);
                        // E1E2 命令参数（C/H/W 复用 0x18/0x20/0x28）
                        10'h204: r_egrp  <= bmerge(r_egrp,  wr_data, wr_strb);
                        10'h208: r_edesc <= bmerge(r_edesc, wr_data, wr_strb);
                        10'h210: r_eout  <= bmerge(r_eout,  wr_data, wr_strb);
                        // 高字槽：非 0 ⇒ 启动时 ERR_SIZE（与其它 64 位基址同约定）
                        10'h20C: r_e_hi_bad[0] <= (wr_data != 32'd0);
                        10'h214: r_e_hi_bad[1] <= (wr_data != 32'd0);
                        // E1E2 DBG 清零（写 0x230 把 5 个计数器一起清）
                        10'h230: clr_e_dbg <= 1'b1;
                        default: ;
                        endcase
                    end
                end
                // wr_page==0：保留页，写忽略
            end

            if (bvalid && s_axi_bready) bvalid <= 1'b0;

            // ---- 运行控制：done 只有在"单元跑完 && 写侧全排空"后才上报 ----
            if (u_done) unit_done_q <= 1'b1;
            vcode_q <= vcode;   // 每拍登记：切断 r_* → vcode → run_active/CE 的长链

            // 启动闸门：等读线静默（没有在飞 AR/R）再发 start，保证新一跑不会
            // 被上一跑的迟到预取数据污染；vcode 在这一刻复核。
            // ★ 阶段②（面积）：vcode 通过后**先进 LC_N 拍冲刷阶段**（把 tag 阵列
            //   顺序写 0，见 §6.11 的单写口约束），冲刷完才真发 start。
            //   为什么安全：这 512 拍里 run_active 仍为 0 ⇒ 单元一个沿都不推进
            //   （u_ce 由 !run_active 强制 1，但单元状态机停在 IDLE），
            //   rd_quiet 也仍成立（没有 AR/R 在飞）⇒ 不会读到"半清"的阵列。
            if (start_pend && !run_active && rd_quiet && !flush_busy) begin
                start_pend <= 1'b0;
                if (vcode_q == 8'd0) begin
                    flush_busy <= 1'b1;         // 先清 tag 阵列（LC_N 拍）
                    flush_ptr  <= {LC_IW{1'b0}};
                end else begin
                    // 校验失败：不启动，但置 done + 错误码，让 PS 的轮询能退出
                    done_sticky <= 1'b1;
                    err_code    <= vcode_q;
                    err_from_cfg <= 1'b1;
                end
            end

            // ---- tag 阵列冲刷阶段：flush_ptr = 0..LC_N-1 各写 0；最后一拍同沿发 start ----
            if (flush_busy) begin
                if (flush_ptr == LC_N - 1) begin
                    flush_busy  <= 1'b0;
                    start_req   <= 1'b1;
                    start_fire  <= 1'b1;        // 让读线把所有条目冲刷掉
                    run_active  <= 1'b1;
                    // ★ 锁存本次运行的单元：运行中改 R_RUN_UNIT 不影响 mux。
                    //   start_req 与 run_unit_q 同拍更新 ⇒ start 脉冲那一拍 mux 已选对。
                    run_unit_q  <= (UNIT == 4) ? 3'd4 : run_unit;
                    clr_e_dbg   <= 1'b1;        // 每次启动清 DBG 吞吐计数器
                    unit_done_q <= 1'b0;
                    done_sticky <= 1'b0;
                    err_code    <= 8'd0;
                    err_from_cfg <= 1'b0;
                end else begin
                    flush_ptr <= flush_ptr + 1'b1;
                end
            end

            if (run_active && unit_done_q && wr_all_idle) begin
                // 跑完：busy 落下、done 抬起；auto-restart 只是再挂一次 start_pend
                run_active  <= 1'b0;
                done_sticky <= 1'b1;
                if (r_ctrl[1]) begin                 // auto-restart（HLS 同义）
                    start_pend  <= 1'b1;
                    unit_done_q <= 1'b0;
                end
            end
        end
    end

    // ⚠ k1 当年在这里漏了驱动 s_axi_bvalid ⇒ 综合成常量 0 ⇒ PS 每笔 MMIO 写都
    //   等不到 B 响应 ⇒ 整机卡死（性能文档 ①）。这里把 Lite 的输出全部显式驱动。
    assign s_axi_bvalid = bvalid;
    assign s_axi_bresp  = 2'b00;

    // ========================================================================
    // 3. 启动前校验（§7.3 两条约束 = ERR_ALIGN / ERR_LAYOUT）
    // ------------------------------------------------------------------------
    //  ⚠ 2026-10-30 时序：这条校验原来是一条 13 级（UNIT=0）的 if/else-if 优先级链
    //    （6 条前缀 + 7 条单元内），综合后是全芯片最差路径
    //       Source      : bin_unit_0/inst/u_bin_unit/r_k_reg[6]/C
    //       Destination : bin_unit_0/inst/u_bin_unit/vcode_q_reg[1]/D
    //       WNS = −5.338 ns @150 MHz（6.667 ns 周期）
    //    现在拆成两段，中间插 vcode_lo_q。**优先级顺序逐条不变**，只是切成两拍：
    //       3a  前缀 6 条        → vcode_lo   → [vcode_lo_q]
    //       3b  各 UNIT 的 7 条   → vcode      → [vcode_q] → 启动闸门
    //    两段各约 7 级，原来是一条 13 级。
    //
    //  §3.1 迟到一拍为什么是安全的（逐拍推导）
    //    W   = 最后一条"会被 vcode 读"的配置写（r_c/r_k/r_h/... 的 do_wr 拍，
    //          r_* 在 W 这一拍的时钟沿更新）；
    //    W+3 = 最早的下一条写 = ctrl 写（0x00 bit0）—— 因为 do_wr 要求 !bvalid
    //          （:443），而 bvalid 最早在 W+1 被 bready 清掉、aw_hs 在 W+2 才能再次
    //          成立、write 在 W+3 落盘 ⇒ **相邻两笔写至少隔 3 拍**；
    //    W+4 = 闸门拍：ctrl 写在 W+3 落盘并置 start_pend（:529），同一 always_ff 里
    //          闸门（:580）下一拍才看得到 start_pend ⇒ 最早 W+4 取 vcode_q；
    //    需要 vcode 在 W+2 就正确（vcode_lo_q 在 W+1 打，第二段组合在 W+1..W+2 算完，
    //    vcode_q 在 W+2 落盘、W+2..W+3 有效）⇒ **余量 1 拍**，且这是"两笔 MMIO 零
    //    间隔背靠背"的理论极限；实际 PS 侧每笔 AXI-Lite 都是 AW+W+B 三次握手，
    //    再加上 `rd_quiet`（:982 要求预取队列空且没有在飞 AR）通常还要多等几拍
    //    ⇒ 实际余量 5~10 拍。这个"1 拍陈旧度"在 2026-10-30 的 inend 三级化
    //    （REPORT_inendsplit.md §1）里已经用过一次，同一类、同一量级。
    // ========================================================================
    wire [12:0] rc13   = r_c[12:0];
    wire [6:0]  cb_c   = (rc13 + 13'd63) >> 6;             // ceil(cin/64) ≤ 64
    wire [6:0]  kk_c   = r_k[6:0] * r_k[6:0];              // k² ≤ 49

    // ⚠ 叶子项：k² → cb·k² 是**两级串联乘法**（bin_conv_core.v:184-188 里同一条修正：
    //   两个 DSP 的 P→A 直连 ≈ 6.6 ns，周期只有 6.667 ns，靠布线救不回来）。
    //   k² 只是 r_k 的纯函数 ⇒ 打一拍再进第二级乘，两级中间就有寄存器。
    //   这个寄存器与 vcode_lo_q **同拍**，所以不额外增加启动前的配置陈旧度
    //   （k 在启动前早已稳定，和 §3.1 的推导同一前提）。
    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn)
        if (!s_axi_aresetn) kk_q <= 7'd0;
        else                kk_q <= kk_c;

    // ⚠ 第二轮（同一手法，同一拍）：3b 里还剩两处"项本身很深"的条件：
    //     第 6 条 ktot_c > 256 : cb_c 的 13 bit 进位链 + 7×7 乘法 + 14 bit 比较
    //     第 7 条 alpha/slope  : 两个 32 bit 等值比较再 OR
    //   两者都排在链的后半段（分别吃 5 / 6 级 mux），叠加起来仍会把
    //   `r_c_reg[8] → vcode_q_reg` 压在 −2.3 ns。这里把**整条判据**登记成 1 bit
    //   （不是把乘法换成查表：表达式与位宽一个字符都没改，等价性最直白）：
    //     cb_q      := cb_c            （C_B 也变成 FF ⇒ DSP 两个操作数都从 FF 出发）
    //     ktot_bad_q:= (cb_q*kk_q > 256)   ← 与原来的 ktot_c 比较逐位同义
    //     sh_bad_q  := (r_alpha_shift != 9) || (r_slope_shift != 8)
    //   三者都比 r_* 晚 1 拍（和 vcode_lo_q / kk_q 同相）⇒ 不额外增加配置陈旧度。
    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn)
        if (!s_axi_aresetn) cb_q <= 7'd0;
        else                cb_q <= cb_c;

    //   显式 14 位上下文（7b×7b ⇒ 14b，与改前的 `wire [13:0] ktot_c` 同宽同截断）
    wire [13:0] ktot_c = cb_q * kk_q;                      // C_B*k² ≤ 3136

    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn)
        if (!s_axi_aresetn) ktot_bad_q <= 1'b0;
        else                ktot_bad_q <= (ktot_c > 14'd256);   // C_B*k² > 256

    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn)
        if (!s_axi_aresetn) sh_bad_q <= 1'b0;
        else                sh_bad_q <= (r_alpha_shift != 32'd9) ||
                                        (r_slope_shift  != 32'd8);

    // 只在"该单元真的会用 64-bit 读"的基址上查对齐（写口是字节写，B_OUT 不要求）
    wire al_in   = (r_in[2:0]      == 3'd0);
    wire al_in2  = (r_in2[2:0]     == 3'd0);
    wire al_in3  = (r_in3[2:0]     == 3'd0);
    wire al_in4  = (r_in4[2:0]     == 3'd0);
    wire al_wgt  = (r_wgt[2:0]     == 3'd0);
    wire al_w2   = (r_w2[2:0]      == 3'd0);
    wire al_w3   = (r_w3[2:0]      == 3'd0);
    wire al_bias = (r_bias[2:0]    == 3'd0);
    wire al_zeta = (r_zeta[2:0]    == 3'd0);
    wire al_gam  = (r_gamma[2:0]   == 3'd0);
    wire al_slp  = (r_slope_b[2:0] == 3'd0);
    wire al_alp  = (r_alpha[2:0]   == 3'd0);

    // ---- 3a. 第一段：与 UNIT 无关的前缀 6 条（原链第 1..6 条，一字未改）----
    always_comb begin
        vcode_lo = 8'd0;
        if (UNIT > 4) begin
            vcode_lo = ERR_UNIT;
        // ★ R_RUN_UNIT 只允许 0（conv）或 4（E1E2）；其它值在启动前就拒掉。
        //   UNIT!=0 的实例（rprelu/sam/interp）忽略 R_RUN_UNIT，保持原行为。
        end else if ((UNIT == 0) && !run_unit_ok) begin
            vcode_lo = ERR_UNIT;
        end else if (r_hi_bad != 8'd0) begin
            vcode_lo = ERR_SIZE;                     // 64 位地址的高字非 0
        end else if ((r_h == 32'd0) || (r_h > HW_LIM) ||
                     (r_w == 32'd0) || (r_w > HW_LIM)) begin
            vcode_lo = ERR_SIZE;
        end else if ((r_c == 32'd0) || (r_c > C_LIM)) begin
            vcode_lo = ERR_SIZE;
        end else if (r_shift > 32'd63) begin
            vcode_lo = ERR_SHIFT;
        // ---------------- 契约 §7.3-②：C % 8 ----------------
        //   E1E2 的 `en` 掩码天然支持任意 cin（描述符生成器里 48/112/544 都跑过），
        //   所以 R_RUN_UNIT=4 时**不**要求 C%8==0；conv/rprelu/sam/interp 不变。
        end else if (((UNIT != 0) ||
                      ((UNIT == 0) && (run_unit != 3'd4) && (run_unit != 3'd5)
                       && CHK_C8_ALL)) &&
                     (r_c[2:0] != 3'd0)) begin
            vcode_lo = ERR_LAYOUT;
        // ---------------- ★ 2026-10-31：conv 的 cout % 8（同 patch 第 2 项）----
        //   8-lane 引擎的写口是 64-bit 整字（带字节使能，只写前几个 lane 是安全的），
        //   但**参数装载**（alpha/bias/slope/...）是按 8 个 oc 一组读的 ⇒ cout 不是
        //   8 的倍数时会越界读 8~28 字节（板上表现可能是 AXI decode error / 挂死）。
        //   契约 §7.3-② 本来就要求 C%8==0，这里把 cout 也一起卡住：宁可报
        //   ERR_LAYOUT 也不静默越界。
        end else if ((UNIT == 0) && (run_unit != 3'd4) && (run_unit != 3'd5)
                     && (r_cout[2:0] != 3'd0)) begin
            vcode_lo = ERR_LAYOUT;
        end
    end

    // 第一段结果打一拍。**每拍刷新**：配置寄存器稳定时 vcode_lo 是常量 ⇒
    // vcode_lo_q 只比它晚 1 拍，对功能无影响（§3.1 的余量推导）。
    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn)
        if (!s_axi_aresetn) vcode_lo_q <= 8'd0;
        else                vcode_lo_q <= vcode_lo;

    // ---- 3b. 第二段：各 UNIT 的检查（原链第 7..13 条）----
    //   vcode_conv 默认带上第一段的错码（第一段报错时原链就是在第一个 else-if 就停住，
    //   后面 case 里的检查**不再执行** ⇒ 这里的 if (vcode_lo_q == 8'd0) 与之等价）；
    //   只有第一段全过才继续往里查。段内每条的先后顺序与原链**完全一致**。
    //
    //   ★ 2026-10-31（E1E2 整合）：conv 链**一个字符没改**，只是换名为 vcode_conv；
    //     E1E2 的 4 条检查单独算 vcode_e；最后用 sel_e1e2_c 做一次 2:1 选择。
    //     这样 conv 的关键路径只多了一级末端 mux，而不是在 13 级链里插条件。
    always_comb begin
        vcode_conv = vcode_lo_q;
        if (vcode_lo_q == 8'd0) begin
            case (UNIT)
            // ===================== bin_conv_core =====================
            0: begin
                if ((r_in == 32'd0) || (r_wgt == 32'd0) || (r_alpha == 32'd0) ||
                    (r_bias == 32'd0) || (r_zeta == 32'd0) || (r_gamma == 32'd0) ||
                    (r_slope_b == 32'd0) || (r_out == 32'd0)) begin
                    vcode_conv = ERR_NULL;
                // 契约 §7.3-①：所有被 64-bit 读的基址必须 8 字节对齐
                end else if (!al_in || !al_wgt || !al_alp || !al_bias ||
                             !al_zeta || !al_gam || !al_slp) begin
                    vcode_conv = ERR_ALIGN;
                end else if ((r_cout == 32'd0) || (r_cout > COUT_LIM)) begin
                    vcode_conv = ERR_SIZE;
                end else if ((r_k == 32'd0) || (r_k > K_LIM)) begin
                    vcode_conv = ERR_SIZE;
                end else if ((r_s == 32'd0) || (r_d == 32'd0)) begin
                    vcode_conv = ERR_SIZE;
                end else if (ktot_bad_q) begin
                    vcode_conv = ERR_SIZE;        // conv 的 wbuf 只有 256 个 64-bit 字
                end else if (sh_bad_q) begin
                    vcode_conv = ERR_SHIFT;       // 契约 §1 的固定值
                end
            end
            // ===================== bin_rprelu =====================
            1: begin
                if ((r_in == 32'd0) || (r_zeta == 32'd0) || (r_gamma == 32'd0) ||
                    (r_slope_b == 32'd0) || (r_out == 32'd0)) begin
                    vcode_conv = ERR_NULL;
                end else if (!al_in || !al_zeta || !al_gam || !al_slp) begin
                    vcode_conv = ERR_ALIGN;
                end else if (r_slope_shift != 32'd8) begin
                    vcode_conv = ERR_SHIFT;
                end
            end
            // ===================== bin_sam =====================
            2: begin
                if ((r_in == 32'd0) || (r_in2 == 32'd0) || (r_in3 == 32'd0) ||
                    (r_in4 == 32'd0) || (r_wgt == 32'd0) || (r_w2 == 32'd0) ||
                    (r_w3 == 32'd0) || (r_out == 32'd0)) begin
                    vcode_conv = ERR_NULL;
                // §7.3-① 本体：三组权重基址 8 字节对齐（连三个输入一起查）
                end else if (!al_in || !al_in2 || !al_in3 || !al_in4 ||
                             !al_wgt || !al_w2 || !al_w3) begin
                    vcode_conv = ERR_ALIGN;
                end
            end
            // ===================== bin_interp =====================
            3: begin
                if ((r_in == 32'd0) || (r_out == 32'd0)) begin
                    vcode_conv = ERR_NULL;
                end else if (!al_in) begin
                    vcode_conv = ERR_ALIGN;
                end else if ((r_ho == 32'd0) || (r_ho > HW_LIM) ||
                             (r_wo == 32'd0) || (r_wo > HW_LIM)) begin
                    vcode_conv = ERR_SIZE;
                end
            end
            default: vcode_conv = ERR_UNIT;
            endcase
        end
    end

    // ---- 3b'. E1E2 的启动前检查（UNIT=4，或 UNIT=0 + R_RUN_UNIT=4）----
    //   与 bin_e1e2.v 的 ST_CHK 同义：
    //      维度（C/H/W）已在 3a 前缀里查过 ⇒ 这里只查描述符/输出基址与字组号。
    //      · B_E_DESC / B_E_OUT 非 0、8 字节对齐、高字为 0
    //      · cfg_c ≤ 576（9 个 64-bit 字组 = 8×64 + 最后 32 通道；再大 RTL 装不下）
    //      · cfg_group ≤ 8（grp=8 是 cin=544 的第 9 组）
    always_comb begin
        vcode_e = 8'd0;
        if ((r_edesc == 32'd0) || (r_eout == 32'd0)) begin
            vcode_e = ERR_NULL;
        end else if ((r_edesc[2:0] != 3'd0) || (r_eout[2:0] != 3'd0)) begin
            vcode_e = ERR_ALIGN;
        end else if (r_e_hi_bad != 2'd0) begin
            vcode_e = ERR_SIZE;              // 64 位基址的高字写过非 0
        end else if (r_c > 32'd576) begin
            vcode_e = ERR_SIZE;
        end else if (r_egrp > 32'd8) begin
            vcode_e = ERR_UNIT;
        end
    end

    // E1E2 路径也要带上前缀错误（3a 的 C/H/W/高字/移位）
    assign vcode_e_eff = (vcode_lo_q != 8'd0) ? vcode_lo_q : vcode_e;
    // catpack 的启动前检查与 E1E2 **同义**（描述符/输出基址非 0、8B 对齐、高字为 0、
    // c ≤ 576、group ≤ 8）⇒ 直接复用 vcode_e，不新增一条优先级链。
    assign vcode_cp_eff = vcode_e_eff;
    // UNIT=4/5 是"纯单元"；UNIT=0 时由 R_RUN_UNIT 选。其余 UNIT 不看 run_unit。
    assign vcode = sel_cp_c ? vcode_cp_eff : (sel_e1e2_c ? vcode_e_eff : vcode_conv);

    // ========================================================================
    // 4. 单元例化（generate if：只 elaborat 选中的那个）
    // ------------------------------------------------------------------------
    //  内存口统一接 u_rd_* / u_wr_*；cfg/base 按单元接线。
    //  ★ P3：ap_clk 接**真实时钟** s_axi_aclk，ap_ce 接 u_ce（同步 CE）；
    // ========================================================================
    generate
    if (UNIT == 0) begin : g_conv
        // ★ 2026-10-31：同一实例内**同时**例化 conv8 + E1E2，由 R_RUN_UNIT 选。
        //   · 两个核都有 64-bit 写口（conv8 带字节使能，E1E2 恒 FF）⇒ u_wr_64=1。
        //   · 只有被选中的核收 start；另一个停在 IDLE，mem_* 全 0。
        //   · 读数据 u_rd_data 两边共用；CE u_ce 也共用（未选中的核本来就不动）。
        //   · 复位 run_unit=0 ⇒ sel=0 ⇒ 旧驱动看到的行为与今天**逐位相同**。
        logic        c_done, c_busy, c_rd_en, c_wr_en;
        logic [31:0] c_rd_addr, c_wr_addr;
        logic [63:0] c_wr_data;
        logic [7:0]  c_wr_sel;
        logic        e_done, e_busy, e_rd_en, e_wr_en;
        logic [31:0] e_rd_addr, e_wr_addr;
        logic [63:0] e_wr_data;
        logic [7:0]  e_wr_sel, e_err;
        logic        k_done, k_busy, k_rd_en, k_wr_en;
        logic [31:0] k_rd_addr, k_wr_addr;
        logic [63:0] k_wr_data;
        logic [7:0]  k_wr_sel, k_err;
        wire         c_start = start_req && !sel_e1e2_q && !sel_cp_q;
        wire         e_start = start_req &&  sel_e1e2_q;
        wire         k_start = start_req &&  sel_cp_q;

        bin_conv_core #(
            .LANES    (LANES),
            .COUT_MAX (COUT_MAX)
        ) u_unit (
            .ap_clk          (s_axi_aclk),
            .ap_ce           (u_ce),
            .ap_rst_n        (s_axi_aresetn),
            .ctrl_start      (c_start),
            .ctrl_done       (c_done),
            .ctrl_busy       (c_busy),
            .cfg_cin         (r_c[15:0]),
            .cfg_cout        (r_cout[15:0]),
            .cfg_k           (r_k[15:0]),
            .cfg_s           (r_s[15:0]),
            .cfg_d           (r_d[15:0]),
            .cfg_pad         (r_pad[15:0]),
            .cfg_h           (r_h[15:0]),
            .cfg_w           (r_w[15:0]),
            .cfg_alpha_shift (r_alpha_shift[5:0]),
            .cfg_slope_shift (r_slope_shift[5:0]),
            .cfg_shift       (r_shift[5:0]),
            .base_act        (r_in),
            .base_wgt        (r_wgt),
            .base_alpha      (r_alpha),
            .base_bias       (r_bias),
            .base_zeta       (r_zeta),
            .base_gamma      (r_gamma),
            .base_slope      (r_slope_b),
            .base_out        (r_out),
            .mem_rd_en       (c_rd_en),
            .mem_rd_addr     (c_rd_addr),
            .mem_rd_data     (u_rd_data),
            .mem_wr_en       (c_wr_en),
            .mem_wr_addr     (c_wr_addr),
            .mem_wr_data     (c_wr_data),
            .mem_wr_sel      (c_wr_sel)
        );

        // E1E2：int8 激活 → 64-bit 二值打包（描述符在内存里，阈值也在内存里）
        bin_e1e2 #(
            .LANES    (64),
            .ACC_AW   (8),
            .C_MAX    (C_MAX)
        ) u_pack (
            .ap_clk          (s_axi_aclk),
            .ap_ce           (u_ce),
            .ap_rst_n        (s_axi_aresetn),
            .ctrl_start      (e_start),
            .ctrl_done       (e_done),
            .ctrl_busy       (e_busy),
            .cfg_c           (r_c[15:0]),
            .cfg_h           (r_h[15:0]),
            .cfg_w           (r_w[15:0]),
            .cfg_group       (r_egrp[15:0]),
            .base_desc       (r_edesc),
            .base_out        (r_eout),
            .mem_rd_en       (e_rd_en),
            .mem_rd_addr     (e_rd_addr),
            .mem_rd_data     (u_rd_data),
            .mem_wr_en       (e_wr_en),
            .mem_wr_addr     (e_wr_addr),
            .mem_wr_data     (e_wr_data),
            .mem_wr_strb     (e_wr_sel),
            .err_code        (e_err)
        );

        // catpack：float32 多源（cat 各段）→ 64-bit 二值字（cat + binarize 融合）
        bin_catpack #(
            .CMAX     (C_MAX),
            .TILE     (16)
        ) u_catpack (
            .ap_clk          (s_axi_aclk),
            .ap_rst_n        (s_axi_aresetn),
            .ap_ce           (u_ce),
            .ctrl_start      (k_start),
            .ctrl_done       (k_done),
            .ctrl_busy       (k_busy),
            .cfg_c           (r_c[15:0]),
            .cfg_h           (r_h[15:0]),
            .cfg_w           (r_w[15:0]),
            .cfg_group       (r_egrp[15:0]),
            .base_desc       (r_edesc),
            .base_out        (r_eout),
            .mem_rd_en       (k_rd_en),
            .mem_rd_addr     (k_rd_addr),
            .mem_rd_data     (u_rd_data),
            .mem_wr_en       (k_wr_en),
            .mem_wr_addr     (k_wr_addr),
            .mem_wr_data     (k_wr_data),
            .mem_wr_strb     (k_wr_sel),
            .err_code        (k_err)
        );

        // ---- 运行期 mux：用**锁存后**的 sel_*_q，运行中改 R_RUN_UNIT 无效 ----
        assign u_done    = sel_cp_q ? k_done    : (sel_e1e2_q ? e_done    : c_done);
        assign u_busy    = sel_cp_q ? k_busy    : (sel_e1e2_q ? e_busy    : c_busy);
        assign u_rd_en   = sel_cp_q ? k_rd_en   : (sel_e1e2_q ? e_rd_en   : c_rd_en);
        assign u_rd_addr = sel_cp_q ? k_rd_addr : (sel_e1e2_q ? e_rd_addr : c_rd_addr);
        assign u_wr_en   = sel_cp_q ? k_wr_en   : (sel_e1e2_q ? e_wr_en   : c_wr_en);
        assign u_wr_addr = sel_cp_q ? k_wr_addr : (sel_e1e2_q ? e_wr_addr : c_wr_addr);
        assign u_wr_data = sel_cp_q ? k_wr_data : (sel_e1e2_q ? e_wr_data : c_wr_data);
        assign u_wr_sel  = sel_cp_q ? k_wr_sel  : (sel_e1e2_q ? e_wr_sel  : c_wr_sel);
        assign u_wr_64   = 1'b1;
        // 单元 err_code 进 AP_RETURN[15:8]；conv 运行期没有 err_code（恒 0）
        assign u_err     = sel_cp_q ? k_err     : (sel_e1e2_q ? e_err     : 8'd0);

        // ---- E1E2 DBG：读吞吐 / 冻结占比（上板直接读 0x230..0x240）----
        always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
            if (!s_axi_aresetn) begin
                cnt_e_rd <= 32'd0; cnt_e_cyc <= 32'd0; cnt_e_rdst <= 32'd0;
                cnt_e_wrst <= 32'd0; cnt_e_wr <= 32'd0;
            end else if (clr_e_dbg) begin
                cnt_e_rd <= 32'd0; cnt_e_cyc <= 32'd0; cnt_e_rdst <= 32'd0;
                cnt_e_wrst <= 32'd0; cnt_e_wr <= 32'd0;
            end else if (run_active && sel_e1e2_q) begin
                cnt_e_cyc  <= cnt_e_cyc + 32'd1;
                if (e_rd_en && u_ce)  cnt_e_rd  <= cnt_e_rd + 32'd1;
                if (e_wr_en && u_ce)  cnt_e_wr  <= cnt_e_wr + 32'd1;
                if (!u_ce) begin
                    if (e_rd_en) cnt_e_rdst <= cnt_e_rdst + 32'd1;
                    else         cnt_e_wrst <= cnt_e_wrst + 32'd1;
                end
            end
        end
    end else if (UNIT == 5) begin : g_catpack
        // 独立的纯 catpack 实例（BD 里想要单独地址段/单独 cell 时用这个分支）
        logic [7:0] k_err;
        bin_catpack #(
            .CMAX     (C_MAX),
            .TILE     (16)
        ) u_unit (
            .ap_clk          (s_axi_aclk),
            .ap_rst_n        (s_axi_aresetn),
            .ap_ce           (u_ce),
            .ctrl_start      (start_req),
            .ctrl_done       (u_done),
            .ctrl_busy       (u_busy),
            .cfg_c           (r_c[15:0]),
            .cfg_h           (r_h[15:0]),
            .cfg_w           (r_w[15:0]),
            .cfg_group       (r_egrp[15:0]),
            .base_desc       (r_edesc),
            .base_out        (r_eout),
            .mem_rd_en       (u_rd_en),
            .mem_rd_addr     (u_rd_addr),
            .mem_rd_data     (u_rd_data),
            .mem_wr_en       (u_wr_en),
            .mem_wr_addr     (u_wr_addr),
            .mem_wr_data     (u_wr_data),
            .mem_wr_strb     (u_wr_sel),
            .err_code        (k_err)
        );
        assign u_err   = k_err;
        assign u_wr_64 = 1'b1;
    end else if (UNIT == 4) begin : g_e1e2
        // 独立的纯 E1E2 实例（如果将来想给它单独的地址段/BD cell，用这个分支）。
        // 端口/寄存器与 UNIT=0 + R_RUN_UNIT=4 完全一致，驱动可以复用。
        logic [7:0] e_err;
        bin_e1e2 #(
            .LANES    (64),
            .ACC_AW   (8),
            .C_MAX    (C_MAX)
        ) u_unit (
            .ap_clk          (s_axi_aclk),
            .ap_ce           (u_ce),
            .ap_rst_n        (s_axi_aresetn),
            .ctrl_start      (start_req),
            .ctrl_done       (u_done),
            .ctrl_busy       (u_busy),
            .cfg_c           (r_c[15:0]),
            .cfg_h           (r_h[15:0]),
            .cfg_w           (r_w[15:0]),
            .cfg_group       (r_egrp[15:0]),
            .base_desc       (r_edesc),
            .base_out        (r_eout),
            .mem_rd_en       (u_rd_en),
            .mem_rd_addr     (u_rd_addr),
            .mem_rd_data     (u_rd_data),
            .mem_wr_en       (u_wr_en),
            .mem_wr_addr     (u_wr_addr),
            .mem_wr_data     (u_wr_data),
            .mem_wr_strb     (u_wr_sel),
            .err_code        (e_err)
        );
        assign u_wr_64 = 1'b1;
        assign u_err   = e_err;

        always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
            if (!s_axi_aresetn) begin
                cnt_e_rd <= 32'd0; cnt_e_cyc <= 32'd0; cnt_e_rdst <= 32'd0;
                cnt_e_wrst <= 32'd0; cnt_e_wr <= 32'd0;
            end else if (clr_e_dbg) begin
                cnt_e_rd <= 32'd0; cnt_e_cyc <= 32'd0; cnt_e_rdst <= 32'd0;
                cnt_e_wrst <= 32'd0; cnt_e_wr <= 32'd0;
            end else if (run_active) begin
                cnt_e_cyc  <= cnt_e_cyc + 32'd1;
                if (u_rd_en && u_ce)  cnt_e_rd  <= cnt_e_rd + 32'd1;
                if (u_wr_en && u_ce)  cnt_e_wr  <= cnt_e_wr + 32'd1;
                if (!u_ce) begin
                    if (u_rd_en) cnt_e_rdst <= cnt_e_rdst + 32'd1;
                    else         cnt_e_wrst <= cnt_e_wrst + 32'd1;
                end
            end
        end
    end else if (UNIT == 1) begin : g_rprelu
        assign u_wr_sel  = 8'hFF;
        assign u_wr_64   = 1'b0;
        assign u_err     = 8'd0;
        assign cnt_e_rd = 32'd0; assign cnt_e_cyc = 32'd0;
        assign cnt_e_rdst = 32'd0; assign cnt_e_wrst = 32'd0;
        assign cnt_e_wr = 32'd0;
        bin_rprelu u_unit (
            .ap_clk          (s_axi_aclk),
            .ap_ce           (u_ce),
            .ap_rst_n        (s_axi_aresetn),
            .ctrl_start      (start_req),
            .ctrl_done       (u_done),
            .ctrl_busy       (u_busy),
            .cfg_c           (r_c[15:0]),
            .cfg_h           (r_h[15:0]),
            .cfg_w           (r_w[15:0]),
            .cfg_slope_shift (r_slope_shift[5:0]),
            .cfg_shift       (r_shift[5:0]),
            .base_in         (r_in),
            .base_zeta       (r_zeta),
            .base_gamma      (r_gamma),
            .base_slope      (r_slope_b),
            .base_out        (r_out),
            .mem_rd_en       (u_rd_en),
            .mem_rd_addr     (u_rd_addr),
            .mem_rd_data     (u_rd_data),
            .mem_wr_en       (u_wr_en),
            .mem_wr_addr     (u_wr_addr),
            .mem_wr_data     (u_wr_data[7:0])
        );
    end else if (UNIT == 2) begin : g_sam
        // ★ 这两个单元只有单字节写口：必须显式驱动，否则 u_wr_64=X
        //   ⇒ wr_byte=X ⇒ p_valid/bf_cnt/df_cnt 全 X ⇒ 仿真里 done 永远不来
        assign u_wr_sel  = 8'hFF;
        assign u_wr_64   = 1'b0;
        assign u_err     = 8'd0;
        assign cnt_e_rd = 32'd0; assign cnt_e_cyc = 32'd0;
        assign cnt_e_rdst = 32'd0; assign cnt_e_wrst = 32'd0;
        assign cnt_e_wr = 32'd0;
        bin_sam u_unit (
            .ap_clk          (s_axi_aclk),
            .ap_ce           (u_ce),
            .ap_rst_n        (s_axi_aresetn),
            .ctrl_start      (start_req),
            .ctrl_done       (u_done),
            .ctrl_busy       (u_busy),
            .cfg_c           (r_c[15:0]),
            .cfg_h           (r_h[15:0]),
            .cfg_w           (r_w[15:0]),
            .cfg_shift       (r_shift[5:0]),
            .base_x          (r_in),
            .base_y0         (r_in2),
            .base_y2         (r_in3),
            .base_y4         (r_in4),
            .base_w0         (r_wgt),
            .base_w2         (r_w2),
            .base_w4         (r_w3),
            .base_out        (r_out),
            .mem_rd_en       (u_rd_en),
            .mem_rd_addr     (u_rd_addr),
            .mem_rd_data     (u_rd_data),
            .mem_wr_en       (u_wr_en),
            .mem_wr_addr     (u_wr_addr),
            .mem_wr_data     (u_wr_data[7:0])
        );
    end else if (UNIT == 3) begin : g_interp
        // ★ 这两个单元只有单字节写口：必须显式驱动，否则 u_wr_64=X
        //   ⇒ wr_byte=X ⇒ p_valid/bf_cnt/df_cnt 全 X ⇒ 仿真里 done 永远不来
        assign u_wr_sel  = 8'hFF;
        assign u_wr_64   = 1'b0;
        assign u_err     = 8'd0;
        assign cnt_e_rd = 32'd0; assign cnt_e_cyc = 32'd0;
        assign cnt_e_rdst = 32'd0; assign cnt_e_wrst = 32'd0;
        assign cnt_e_wr = 32'd0;
        bin_interp u_unit (
            .ap_clk          (s_axi_aclk),
            .ap_ce           (u_ce),
            .ap_rst_n        (s_axi_aresetn),
            .ctrl_start      (start_req),
            .ctrl_done       (u_done),
            .ctrl_busy       (u_busy),
            .cfg_c           (r_c[15:0]),
            .cfg_h           (r_h[15:0]),
            .cfg_w           (r_w[15:0]),
            .cfg_ho          (r_ho[15:0]),
            .cfg_wo          (r_wo[15:0]),
            .base_in         (r_in),
            .base_out        (r_out),
            .mem_rd_en       (u_rd_en),
            .mem_rd_addr     (u_rd_addr),
            .mem_rd_data     (u_rd_data),
            .mem_wr_en       (u_wr_en),
            .mem_wr_addr     (u_wr_addr),
            .mem_wr_data     (u_wr_data[7:0])
        );
    end else begin : g_none
        // 非法 UNIT：不例化任何单元，输出钉死（避免"空 generate 块"歧义）
        assign u_done    = 1'b0;
        assign u_busy    = 1'b0;
        assign u_rd_en   = 1'b0;
        assign u_rd_addr = 32'd0;
        assign u_wr_en   = 1'b0;
        assign u_wr_addr = 32'd0;
        assign u_wr_data = 64'd0;
        assign u_wr_sel  = 8'hFF;
        assign u_wr_64   = 1'b0;
        assign u_err     = 8'd0;
        assign cnt_e_rd = 32'd0; assign cnt_e_cyc = 32'd0;
        assign cnt_e_rdst = 32'd0; assign cnt_e_wrst = 32'd0;
        assign cnt_e_wr = 32'd0;
    end
    endgenerate

    // ========================================================================
    // 5. 时钟门控 + u_ce（单元"能推进"的条件）
    // ========================================================================
    // 槽号 / 线号一律从**字地址**派生，且位宽显式（不再用容易写错一位的
    // "addr[2+IDX_W-1:3]" 这类位选）：
    //     word = addr >> 3
    //     slot = word[IDX_W-1:0]        （线内第几个 64-bit 字）
    //     tag  = word[28:IDX_W]         （第几条线）
    // ⚠ 2026-10-28 xsim 抓到的致命 bug 就在这里：槽号原来写成 '[2+IDX_W-1:3]'
    //   （= addr[4:3]，只有 2 位），少取最高一位后被零扩展 ⇒ **线内第 5..8 个字
    //   别名到第 1..4 个槽**，命中的是同一行前半段的旧数据。现象是"每条 64 B
    //   线的前 4 个字全对、后 4 个字全错，且错值像邻近像素"。
    //   现在写成算术派生 + 显式位宽，这类错误不可能再出现。
    logic [31:0] rd_word;                       // 字地址（低 29 位有效）
    assign rd_word = {3'b000, u_rd_addr[31:3]};
    assign rd_slot = rd_word[IDX_W-1:0];
    assign rd_tag  = rd_word[28:IDX_W];
    // 命中判据在 §6 的 CAM 里给（must be 同拍，论证见 §6）。
    assign rd_want = u_rd_en;

    assign wr_take   = u_wr_en;
    // ★ 8-lane 引擎的写口是"一个 64-bit 整字 + 8 位字节使能"，而且地址**恒 8 字节对齐**
    //   （C8 布局下 8 个输出通道正好是连续 8 字节）⇒ 不需要经过"逐字节攒 beat"，
    //   一拍直接落一个满 WSTRB 的 beat。其余三个单元 u_wr_wide 恒 0，行为完全不变。
    assign u_wr_wide_w = u_wr_64 && (u_wr_sel == 8'hFF) && (u_wr_addr[2:0] == 3'd0);
    // ★ 2026-10-31 时序刀 5（v38）：把 u_ce 从"合并/push 使能"这条锥上彻底拿掉。
    //   布线后实测（v37 / 10 ns）：设计里**全部 1200 条违例端点共用一个起点**
    //   （u_pack/desc_ram_reg/CLKARDCLK），最后 4.6 ns 就是
    //       rd_hit → u_ce → p_merge → push_now → bf_data_reg[*] 的 D-mux 选择
    //   而这条锥本来应该是"纯寄存器到寄存器"的（§7.0 的入口寄存器就是为此加的）。
    //   漏掉的那一项正是 p_merge 里的 `!wr_wide_b` —— wr_wide_b 里含 u_ce。
    //   改法：
    //     · 新增 wide_pend = wr_take && u_wr_wide_w（**不含 u_ce**，操作数全是单元输出寄存器）
    //     · p_merge 改用 !wide_pend（比 !wr_wide_b **更保守**：u_ce=0 时也挡住合并）
    //     · p_valid 从"下一拍必须并掉"改成"并不掉就等"（见 §7.0a 的 always_ff）
    //   等价性：
    //     · 新条件只在 (wide_pend && !u_ce) 时比旧条件多挡一拍；而那一拍
    //       wr_byte = wr_take && u_ce && !wide = 0（u_ce=0 且 wide）⇒ 不会有新字节把
    //       p_* 冲掉 ⇒ 旧代码"被挡就丢字节"，新代码"被挡就等下一拍"，**只会更安全**。
    //       （证明 p_valid 与 wr_byte 同拍为 1 时 wide_pend 必为 0：wr_byte=1 ⇒ !wide。）
    //     · p_merge 仍然要求 wst==W_IDLE ⇒ §9(2b) 的不变式 p_valid ⇒ wst==W_IDLE 不变。
    //     · push_beat 仍然每拍最多调用一次（!pw_valid 保留）⇒ FIFO 记账不变。
    assign wide_pend   = wr_take && u_wr_wide_w;
    assign wr_wide_b   = wide_pend && u_ce;
    assign wr_byte   = wr_take && u_ce && !u_wr_wide_w;  // 真正被吞下、进 p_* 的字节
    // ⚠ 以下全部改由**入口寄存器** p_addr 派生（§7.0）：合并逻辑不再从
    //   u_wr_addr/u_wr_data（= 单元输出，其上游挂着"读地址 → 命中"那条共享锥）
    //   出发。这是把 bf_data_reg / be_data_reg 的写使能从 17~19 级切到 2~3 级
    //   的关键一步（写侧对 1 拍延迟无契约：字节顺序不变即等价）。
    assign w_lane    = p_addr[2:0];
    assign w_word    = p_addr[31:3];
    assign be_same_word = be_open && (be_word == w_word);
    assign be_strb_nx   = be_strb | (8'd1 << w_lane);
    // 本拍把 p_* 里那个字节并进 be_*（只在 wst==W_IDLE 时可能，不变式见 §9）
    //   ⚠ 加上 !pw_valid：整字入口寄存器里有拍子未落盘时，字节路不许抢 push
    //     （push_beat 每拍只能调用一次，否则 bf_wp 只前进一次、丢一拍）。
    assign p_merge      = p_valid && (wst == W_IDLE) && !wide_pend && !pw_valid;
    assign wr_fifo_press = (bf_cnt >= BF_PRESS) || (df_cnt >= DF_PRESS);

    // u_ce = 单元这一拍推进；三个冻结源见文件头 §5
    // 复位期/空闲期恒 1：避免仿真起点 u_ce=X 把 CE 卡成 X（单元照常推进最安全）
    // ⚠ 压力项从 "wr_take && wr_fifo_press && !wr_safe_now" 改成
    //   "wr_take && wr_fifo_press"（去掉 wr_safe_now 这个**预测**）：
    //   入口寄存器把"吞字节"与"push beat"拆成两拍后，预测的基准差一拍 ⇒ 可能
    //   预测错（论证见文件头 §5 / §7.0，上界核算见 §7.9）。
    //   顺带把 u_ce 上"29 位比较 + 8 位与树"整段拿掉。
    assign u_ce = (!s_axi_aresetn) || (!run_active)
                || ((!rd_want || rd_hit)
                    && !(wr_take && wr_fifo_press)
                    && !((wst != W_IDLE) && wr_take));

    // ---- ★ P3：时钟门已删除（always_latch + AND / u_ce_lat / u_clk 全删）----
    //   u_ce 直接进 4 个单元例化的 .ap_ce（同步 CE），不再产生第二套时钟网络。
    //   ⚠ 不要"为了调试"把 u_clk 加回来：那会重新引入 −1.510 ns 的域间偏斜。

    // 单元看到的读数据寄存器：语义等价于
    //     always @(posedge clk) if (mem_rd_en) mem_rd_data <= mem[mem_rd_addr];
    // 即"t 拍给地址、t+1 拍数据有效"。单元的时间轴完全不变。
    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) u_rd_data <= 64'd0;
        else if (u_ce && rd_want && rd_hit) u_rd_data <= win_q[{rd_idx, rd_slot}];
    end

    // ========================================================================
    // ========================================================================
    // 6. 读侧：**直接映射**线缓存 + 异或散列（步骤④ —— 换掉步骤②/③ 的全相联 CAM）
    // ------------------------------------------------------------------------
    //  为什么必须换（板上铁证 → 结构推论）：
    //    整网 256² 的 PL = 11.28 s，而 64² 的每个形状在 xsim 里只要 ≈1.02 拍/term
    //    （bin_3x48_32_64x64：151,099 拍 / 147,456 term）⇒ 引擎本身没变慢，
    //    慢的是 256² 特有的**线缓存颠簸**。一行的工作集 = ceil(W/16) 条线，
    //    k=3 要 3 行，再乘 cb = ceil(cin/64) 个通道块：
    //        W=64  ⇒ 4×3×1  = 12  ≤ 16  ✓（**所有 golden 都是这个形状**）
    //        W=128 ⇒ 8×3×cb        （cin=544,cb=9 ⇒ 216）
    //        W=256 ⇒ 16×3×cb       （cin=256,cb=4 ⇒ 192；cin=544 ⇒ 432）
    //    装不下 ⇒ 反复重填同一条线 ⇒ 每 9 个 term 就付一次约 55 拍的往返
    //    （实测 11.28 s / 238.5 M term ≈ 7.1 拍/term @150 MHz，与 55/9 吻合）。
    //  为什么不能"把全相联条目数调大"：全相联的代价是 **N 个 TAG_W 位比较器 +
    //    N 路或树 + N 路优先扫描**，不是存储（板端 BRAM 只用了 15/95）。
    //  而 ARID = 条目号 又被 AXI_ID_W=4 卡死在 N ≤ 16。
    //  ⇒ 直接映射：**1 个比较器**、无优先扫描、无 alloc_ptr 轮转。
    //    LC_N=512 ⇒ 数据 512×16×8 B = 64 KB = 14.2 个 BRAM36（≪ 80 空闲）。
    //
    //  散列（idx = 表项号）—— **必须把 cb 折进来**：
    //    tag = cb·(H·W/16) + row·(W/16) + line，cb 只改 tag 的**高位**
    //    ⇒ 朴素的 `tag & (LC_N-1)` 会让所有 cb 组挤进同一批槽（灾难）。
    //  §6.1 的散列拆成两段：
    //     idx[IDX_W-1:0]     = tag[IDX_W-1:0]        （行内线号，**不做混淆**）
    //     idx[LC_IW-1:IDX_W] = XOR-fold(tag[TAG_W-1:IDX_W])（把 cb + 行高位折进来）
    //  ⇒ 对 W 是 16 的整数倍（本网络 W=256/128 都是）同一行的 16 条线**恰好一一
    //    占满低 4 位**、彼此永不冲突；冲突只可能发生在"不同 (cb,row)"之间，
    //    而那些只有 cb·k ≤ 27 组被折进 2^5=32 个值（LC_N=512）⇒ 冲突率很低。
    //
    //  ★ rd_hit **必须同拍**（本文件最容易写错的一条，绝不能打拍）：
    //    rd_hit 是 u_ce 的一部分。若打一拍，则"真 miss 的那一拍"rd_hit 还残留 1
    //    ⇒ u_ce=1、单元拿**旧地址**推进一格、u_rd_data 也被写成一拍前的槽数据
    //    ⇒ 那次 miss 重试被**永久跳过**，静默错一个 term（conv 侧有反例：
    //    行边界处每行一个 term 静默错）。这里 rd_hit 是纯组合：
    //    lc_valid[idx] && (lc_tag[idx]==rd_tag)，起点是 rd_tag（引擎的 rd_addr_q，
    //    纯寄存器输出），整条锥 = 散列 2 级 + 标签阵列 1~2 级 + 比较 2 级。
    // ========================================================================

    // ---- 6.1 散列：低位保持"行内线号"，把 cb/行高位异或折进高位 ----
    //   LC_N=512、TAG_W=25、IDX_W=4 ⇒ HIW=5、HFN=ceil(21/5)=5 段（末段左侧补 0）。
    //   ⚠ HIW 必须 > 0（LC_N ≥ 2·RD_WIN_WORDS），下面 §9 有 $fatal 检查。
    localparam int HIW = LC_IW - IDX_W;                     // 散列位数
    localparam int HFN = (TAG_W - IDX_W + HIW - 1) / HIW;   // 折几段
    function automatic [LC_IW-1:0] lc_hash(input [TAG_W-1:0] tg);
        logic [HFN*HIW-1:0] tx;
        integer hk;
        begin
            tx = {(HFN*HIW){1'b0}};
            tx[TAG_W-IDX_W-1:0] = tg[TAG_W-1:IDX_W];       // 显式零扩展，不靠位选越界
            lc_hash = {LC_IW{1'b0}};
            lc_hash[IDX_W-1:0] = tg[IDX_W-1:0];            // ★ 行内线号原样保留
            // ★ 2026-10-31 coreopt：折叠加**左旋**（第 hk 段左旋 1 位，hk 为奇数时）。
            //   动机（实测 128²/k3/d=2/cb=3 的线缓存颠簸）：
            //   `idx[3:0] = tag[3:0] = {iy[0], 行内线号}`，`iy[0]` 是唯一区分"行"的低位；
            //   d=2 的膨胀窗三个 ky 抽头**行奇偶相同** ⇒ 该位对三行全一样 ⇒ 9 条活线
            //   只能靠 idx[8:4] 的 5 位区分，而平面号 b（tag[10..12]）在对齐折叠时与
            //   iy[5:1]（tag[8:4]）落在同一批位上，a^b 互相抵消 ⇒ 9 条只占 7 个 set
            //   （d=1 是 9 个）⇒ 相邻 term 互踢 ⇒ 填线 = 理论最小值的 34 倍。
            //   把奇数段左旋 1 位 ⇒ b 的位挪到 h[1..3]，与 iy[5:1] 的 h[0..4] 错开
            //   ⇒ 焦点窗口（128²/d=2/cb=3，240 个采样）别名 180/240 → **0/240**，
            //   模型填线 718,848 → **36,864（= 理论最小）**。
            //   ★ **`idx[3:0] = tag[3:0]` 一个字没动** ⇒ "同一行的 16 条线永不冲突"
            //   与 wrapopt 那条 "低 4 位可由索引反推" 的不变量**都保留**。
            //   代价：纯走线（旋转移位不花 LUT）。
            for (hk = 0; hk < HFN; hk = hk + 1) begin
                if ((hk % 2) == 0)
                    lc_hash[LC_IW-1:IDX_W] = lc_hash[LC_IW-1:IDX_W] ^ tx[hk*HIW +: HIW];
                else
                    lc_hash[LC_IW-1:IDX_W] = lc_hash[LC_IW-1:IDX_W]
                        ^ {tx[hk*HIW +: HIW-1], tx[hk*HIW + HIW-1]};
            end
        end
    endfunction

    // ---- 6.2 命中 / 缺失（决策级输入，全部来自 FF；rd_hit 必须同拍）----
    //   ★ 阶段②：valid 是**同一个 LUTRAM 字**的最高位（比原来"另一个 512 深 LUTRAM"少一级），
    //     线号只比高 TAG_W-IDX_W 位（低 IDX_W 位由 rd_idx 蕴含，见 §6.1/§6.2 的推导）。
    //   ★ fill_in_flight：该条目正在被回填 ⇒ **绝不能命中**（win_q 里正被新数据覆盖）。
    //     它与旧的 lc_busy[rd_idx] 逐位等价，但只用 16 个 9-bit 比较 + 或树（≈50 LUT），
    //     而旧的 512 位向量带一整套 512 路写译码器（实测 512 LUT）。
    //   ⚠ 必须写成 always_comb 的**内联循环**，不能写成 "function + 连续赋值"：
    //     xsim 对"function 里读了实参之外的模块信号"的连续赋值只按**实参**建敏感表
    //     ⇒ fs_* 变了不重算 ⇒ 闸门恒 0 ⇒ 假命中 + 读到半条线（2026-10-31 实测踩到：
    //     命中槽地址 DEADBEEF / u_rd_data=X / 冻死 200000 拍）。always_comb 才把
    //     function/循环里读到的所有信号都算进敏感表。
    logic                    fill_rd;
    integer                  fk;
    always_comb begin
        fill_rd = 1'b0;
        for (fk = 0; fk < NSLOT; fk = fk + 1)
            fill_rd = fill_rd | (fs_valid[fk] && (fs_idx[fk] == rd_idx));
    end
    assign rd_idx  = lc_hash(rd_tag);
    //   整个条目只读一次（先存成 wire 再用）：可读性 + 保证"一个索引一个读口"。
    //   （实测：拆成两个位选读 vs 合成一次读，primitive 数一样是 192，所以这不是
    //     LUTRAM 变多的原因 —— 别为这个再改结构。）
    wire [TGW-1:0] tag_rd = lc_tag[rd_idx];
    assign rd_hit  = tag_rd[VLD] &&
                     (tag_rd[VLD-1:0] == rd_tag[TAG_W-1:IDX_W]) &&
                     !fill_rd;
    assign rd_miss = rd_want && !rd_hit;

    // ---- 6.3 空填充槽：优先编码最低位（16 路 1 位扫描 ⇒ 2~3 级 LUT，深度恒定）----
    integer sv;
    always_comb begin
        fs_v  = {SLW{1'b0}};
        fs_ok = 1'b0;
        for (sv = 0; sv < NSLOT; sv = sv + 1)
            if (!fs_ok && !fs_valid[sv]) begin
                fs_ok = 1'b1;
                fs_v  = sv[SLW-1:0];
            end
    end

    // ---- 6.4 分配（替换）：表项号 = 散列结果，**没有轮转指针、没有优先扫描** ----
    //   `!fill_rd` = "不许覆盖正在回填的表项"（直接映射的新风险，见 §6 开头）。
    //   不满足时 alloc_en=0 ⇒ 不分配 ⇒ rd_hit 保持 0 ⇒ 单元继续冻结，
    //   等那笔填充收完（最多 RD_WIN_WORDS 拍）⇒ 不会死锁。
    assign arq_room = (arq_cnt != ARQ_FULL);
    wire res_ok = fs_ok && arq_room && !start_fire && !al_v;
    assign alloc_en = rd_miss && !fill_rd && res_ok;

    // ---- 6.5 ★ ARID 位宽方案：ARID = **填充槽号**（不是表项号）----
    //  问题：idx 有 LC_IW=9 位，而 ARID 只有 AXI_ID_W=4 位（BD 定死，不能加宽）。
    //  三条候选与取舍：
    //    (a) 截断 idx[3:0] 当 ID ⇒ 两个在飞的填充可能同 ID。AXI 只保证**同 ID 保序**，
    //        同 ID 的两笔 burst 数据会交织回来 ⇒ 无法判定某一拍属于哪个表项
    //        ⇒ 必须再加"同 ID 不许并发"的约束（16 位掩码 + 命中即 stall）。
    //        可行但把"能不能分配"绑到了低 4 位的碰撞运气上。
    //    (b) 在飞槽表把 idx 映射到 2 位 ID ⇒ 最多 4 笔在飞，比 PF_MAX=4 还紧，
    //        需求缺失会顶掉预取预算。
    //    (c) **选这个**：ARID = 填充槽号 s（NSLOT=16 ⇒ 恰好 4 位，一个不浪费）。
    //        槽从"分配"那一刻独占到位、直到最后一拍回来才释放 ⇒
    //          · 同 ID 同一时刻最多 1 笔在飞 ⇒ **同 ID 保序 = 单笔 burst 内保序**，
    //            R 拍永远按 fs_wr[s] 递增落槽，不需要任何重排；
    //          · RID → 表项号 / 槽号 / 剩余拍数 全部由槽表查出（都是 FF），
    //            数据落点 = {fs_idx[RID], fs_wr[RID]}，**跨 ID 乱序也落不错**；
    //          · 表项号与 ID 彻底解耦 ⇒ LC_N 可以任意大，BD 一个字都不用改。
    //        代价：多一张 16 项的槽表（16×(9+5+4)+17 bit ≈ 350 bit）与一次 16 选 1
    //        读出（16 项 ⇒ 4 级 mux，起点是 m_axi_rid 这个输入，不在 u_ce 锥上）。
    //  ⇒ AXI_ID_W 保持 4、m_axi_arid 端口宽度不变、BD 不动（§9 有 NSLOT ≤ 2^AXI_ID_W 检查）。
    function automatic [AXI_ID_W-1:0] zx_slot(input [SLW-1:0] v);
        zx_slot = '0;
        zx_slot[SLW-1:0] = v;
    endfunction

    // ---- 6.6 ★ 步骤③ 保留：按**实际 term 序列**提前把下一条线填进来 ----
    //   "任何一次被消费的读换了线号"就把 (线号+PF_AHEAD) 塞进预取队列，与需求缺失
    //   非阻塞并行（PF_MAX 笔在飞）。预取只**多填**线、从不改变 rd_hit 的判据。
    //   ★ 预算门（步骤④ 改写）：在飞预取笔数 < PF_MAX **并且** 有空槽 **并且**
    //     队列非空 **并且** 目标槽没被占 —— 四条满足才发。
    //   "目标是否已在缓存里"的判据（CAM 时代是 NLINES 个 25 位比较器）现在退化成
    //   **1 bit**：看目标槽是否被占用（valid 或 busy）。为什么只看槽、不看 tag：
    //     · 槽有效且 tag 就是目标 ⇒ 已在缓存，跳过（本来就要跳过）；
    //     · 槽有效但 tag 不是目标 ⇒ 填目标会**挤掉一条正在用的线**，直接映射下
    //       这等于自己制造颠簸 ⇒ 跳过更划算（保守 = 安全）；
    //     · 槽正在填 ⇒ 现在不能分配（lc_busy 闸门），跳过。
    //   ⇒ 三种情况都该跳过，所以只读 1 bit 就够。
    wire rd_take  = u_ce && rd_want && rd_hit;        // 真正被消费的一拍
    wire new_line = rd_take && (rd_tag != obs_tag_q); // 进入一条新线
    assign pf_tgt  = rd_tag + PF_A;
    assign pf_head = pf_tag[pf_rp];
    wire [LC_IW-1:0] pf_hidx = lc_hash(pf_head);
    //   判据（与 CAM 版逐值等价，只是从"NLINES 路 25 位比较"变成"1 路"）：
    //     pf_head_known = 目标**真的**在缓存里（槽有效且 tag 相等）⇒ 放弃预取；
    //     pf_blocked    = 目标槽正在填 ⇒ 这一拍不能分配（lc_busy 闸门），等下一拍。
    //   ⚠ 不要退化成"槽被占用就算 known"：那样在"目标槽被另一条在用的线占着"
    //     （恰恰是颠簸时的常态）会**永不放行预取** ⇒ 实测 sam_64_8x8 反而 +16%
    //     拍数（见 REPORT_linecache.md §5）。精确比较只用 1 个 21-bit 比较器。
    //   ★ 阶段②：valid 从同一个 LUTRAM 字取；tag 只比高 21 位（低 4 位由索引蕴含）。
    wire [TGW-1:0] tag_pf = lc_tag[pf_hidx];
    wire pf_tag_eq = tag_pf[VLD] &&
                     (tag_pf[VLD-1:0] == pf_head[TAG_W-1:IDX_W]);
    //   预取口同样的闸门（这里也用 always_comb 内联循环，理由同上）
    logic                    fill_pf;
    integer                  fk2;
    always_comb begin
        fill_pf = 1'b0;
        for (fk2 = 0; fk2 < NSLOT; fk2 = fk2 + 1)
            fill_pf = fill_pf | (fs_valid[fk2] && (fs_idx[fk2] == pf_hidx));
    end
    wire pf_blocked = fill_pf;
    assign pf_head_known = pf_tag_eq;

    always_comb begin
        pf_inq = 1'b0;
        pq_idx = {PFW_W{1'b0}};
        for (cp3 = 0; cp3 < PF_DEPTH; cp3 = cp3 + 1) begin
            pq_idx = pf_rp + cp3[PFW_W-1:0];
            if ((cp3 < pf_cnt) && (pf_tag[pq_idx] == pf_tgt)) pf_inq = 1'b1;
        end
    end

    assign pf_push  = (PF_EN != 0) && (PF_A != {TAG_W{1'b0}})
                   && (new_line || alloc_en)
                   && !pf_inq && (pf_cnt != PFQ_FULL) && !start_fire;
    assign pf_issue = (PF_EN != 0) && (pf_cnt != PFQ_Z) && !pf_head_known && !pf_blocked
                   && (pf_busy != PF_MAX3) && res_ok && !alloc_en;
    assign pf_drop  = (PF_EN != 0) && (pf_cnt != PFQ_Z) && pf_head_known;

    assign dec_en   = alloc_en || pf_issue;          // 本拍"决策"一笔分配
    assign do_alloc = al_v && !start_fire;           // 本拍"落盘"（决策的下一拍）

    // ---- 6.7 AR 通道：地址/ID 全在**队列**里，表头驱动 m_axi ----
    //   AXI 硬规则：arvalid 拉高后必须保持到 arready 握手（"不能用门控撤销
    //   arvalid"，性能文档 §补记一踩过）。m_axi_arvalid = (arq_cnt != 0)，
    //   表头只在 ar_fire 那拍换 ⇒ arvalid 高期间地址/ID 恒定（§9(3h) 逐拍查）。
    assign m_axi_arvalid = (arq_cnt != {ARQ_W{1'b0}});
    // ★ 2026-10-31 第 1 刀：原 m_axi_araddr = arq_addr[arq_rp] 是队列表格多选中 mux
    //   （实测 8 级 LUT + MUXF7、9.09 ns），终点是互连 reg_slice。队头只在 ar_fire
    //   或入空队列时变 ⇒ 提前一拍选进寄存器，m_axi_araddr 变 0 级组合。
    logic [31:0]         ar_addr_sel_q;
    logic [AXI_ID_W-1:0] ar_id_sel_q;
    wire  [ARP_W-1:0]    arq_rp_nxt = arq_rp + 1'b1;
    //   ① cnt==0 入队 ⇒ 它就是队头（arvalid=0，改地址合法）
    //   ② cnt==1 且同拍 ar_fire ⇒ 一进一出后它是队头
    //   ⚠ 漏掉 ② 的 ar_fire 就会"arvalid 挂住期间改地址"（§9(3h) 断言会抓）。
    wire                 arq_head_new = do_alloc && ((arq_cnt == 0) || ((arq_cnt == 1) && ar_fire));
    assign m_axi_araddr  = ar_addr_sel_q;
    assign m_axi_arlen   = ARLEN_C;                    // ★ RD_WIN_WORDS 拍 INCR
    assign m_axi_arid    = ar_id_sel_q;                // ★ ARID = 填充槽号（预选中）

    assign ar_fire = m_axi_arvalid && m_axi_arready;

    // ---- 6.8 R 通道：RID → 槽 → 表项，槽号由该槽自己的计数器给 ----
    //   一笔 burst 内 AXI 保证顺序 ⇒ 第 k 拍必是该线第 k 个字。
    //   两条线同时在飞也不会混淆：落点由本模块的 fs_* 寄存器决定。
    assign rid_slot   = m_axi_rid[SLW-1:0];
    assign rid_hi_bad = (m_axi_rid != zx_slot(rid_slot));
    assign rid_idx    = fs_idx[rid_slot];
    assign rid_wr     = fs_wr[rid_slot];
    assign rid_last   = (fs_out[rid_slot] == {{IDX_W{1'b0}}, 1'b1});

    // 只在"这个槽确实还有拍没回"时收数据 ⇒ 多出来的 R 拍收不进来（停总线 +
    // §9(3) 报警），而不是静默写坏另一个表项的槽。
    //   ⚠ 空闲时**显式**给 0：不给的话 rready 会经 m_axi_rid 这个输入穿出去，
    //     仿真起点 rid=X 时输出也是 X。
    assign m_axi_rready = fs_valid[rid_slot] && (fs_out[rid_slot] != PF_Z);
    assign r_fire       = m_axi_rvalid && m_axi_rready;

    // ---- 6.9 "整条读通道静默" = 没有挂着的 AR + 任何槽都没有未回拍 ----
    assign any_out  = |fs_valid;
    assign rd_quiet = (arq_cnt == {ARQ_W{1'b0}}) && !any_out;

    // ---- 6.10 全部状态（除 win_q / lc_tag 两个阵列）----
    wire pf_done_ev  = r_fire && rid_last && fs_pf[rid_slot];
    wire pf_alloc_ev = do_alloc && al_pf;

    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) begin
            fs_valid  <= {NSLOT{1'b0}};
            fs_pf     <= {NSLOT{1'b0}};
            for (ce = 0; ce < NSLOT; ce = ce + 1) begin
                fs_idx[ce] <= {LC_IW{1'b0}};
                fs_out[ce] <= PF_Z;
                fs_wr [ce] <= {IDX_W{1'b0}};
            end
            al_v      <= 1'b0;
            al_idx    <= {LC_IW{1'b0}};
            al_tag    <= {TAG_W{1'b0}};
            al_s      <= {SLW{1'b0}};
            al_pf     <= 1'b0;
            arq_cnt   <= {ARQ_W{1'b0}};
            arq_wp    <= {ARP_W{1'b0}};
            arq_rp    <= {ARP_W{1'b0}};
            ar_addr_sel_q <= 32'd0;
            ar_id_sel_q   <= {AXI_ID_W{1'b0}};
            pf_cnt    <= {PFQW_W{1'b0}};
            pf_wp     <= {PFW_W{1'b0}};
            pf_rp     <= {PFW_W{1'b0}};
            pf_busy   <= 3'd0;
            obs_tag_q <= {TAG_W{1'b0}};
            cnt_pf    <= 16'd0;
            cnt_fill  <= 16'd0;
        end else begin
            // ---- 决策级：把（表项号, 线号, 填充槽, 是否预取）锁进 al_* --------
            //   这一级之后，落盘级所有输入都来自寄存器 ⇒ 决策级的锥
            //   （散列 + 标签阵列 + busy 读 + 槽优先编码）整周期可用。
            //   **预取也走这一级**，不给自己另开一条长锥。
            al_v   <= dec_en;
            al_idx <= alloc_en ? rd_idx : pf_hidx;
            al_s   <= fs_v;
            al_tag <= alloc_en ? rd_tag : pf_head;
            al_pf  <= !alloc_en;                  // dec_en=0 时 al_v=0，此值无用

            // ---- 落盘级：状态落盘 + 压入 AR 队列 --------------------------------
            //   ⚠ 必须用决策时锁存的 al_*，不许在这里重读 rd_idx/fs_v。
            if (do_alloc) begin
                arq_addr[arq_wp] <= {al_tag, {(IDX_W+3){1'b0}}};  // 线基址，线对齐
                arq_id  [arq_wp] <= zx_slot(al_s);
                arq_wp           <= arq_wp + 1'b1;
                // ★ 2026-10-31 修组合环：lc_tag 只能由 §6.11 的无复位专用块写
                //   （RAM 推断要它）。这里再写一遍 ⇒ 综合器合并两路驱动 ⇒
                //   该阵列又被同拍读 ⇒ [DRC LUTLP-1] 组合环 ⇒ place 失败。
                // ★ 阶段②：`lc_valid`/`lc_busy` 已删除（由 lc_tag 的 valid 位 +
                //   fill_in_flight 表达）；"这条表项现在在飞"由下面 fs_valid/fs_idx 记账。
                fs_valid[al_s]   <= 1'b1;
                fs_idx  [al_s]   <= al_idx;
                fs_out  [al_s]   <= PF_N;                  // 这笔 burst 要收 N 拍
                fs_wr   [al_s]   <= {IDX_W{1'b0}};
                fs_pf   [al_s]   <= al_pf;                 // 预取标记
                cnt_fill         <= cnt_fill + 16'd1;
            end
            if (pf_alloc_ev) cnt_pf <= cnt_pf + 16'd1;

            // ---- AR 队列计数/弹出（与压入可以在同一拍）----
            case ({do_alloc, ar_fire})
                2'b10:   arq_cnt <= arq_cnt + 1'b1;
                2'b01:   arq_cnt <= arq_cnt - 1'b1;
                default: ;                                   // 同拍一进一出：不变
            endcase
            if (ar_fire) arq_rp <= arq_rp + 1'b1;
            if (arq_head_new) begin
                ar_addr_sel_q <= {al_tag, {(IDX_W+3){1'b0}}};
                ar_id_sel_q   <= zx_slot(al_s);
            end else if (ar_fire) begin
                ar_addr_sel_q <= arq_addr[arq_rp_nxt];
                ar_id_sel_q   <= arq_id  [arq_rp_nxt];
            end

            // ---- 收数据：落槽 + 记账 + 整条到齐 ----
            //   ⚠ 落盘的表项必然 !fill_hits，而 r_fire 的表项必然 fill_hits
            //     ⇒ 两者**不可能**是同一个表项（§9(4d) 有断言）。
            //   ★ 阶段②："整条到齐"只需要清掉 fs_valid[槽] —— 命中的两个条件
            //     （tag 相等 + 不在填）分别由 lc_tag 阵列与 fs_* 给出，不再需要
            //     单独置一个 lc_valid 位。
            if (r_fire) begin
                fs_out[rid_slot] <= fs_out[rid_slot] - 1'b1;
                fs_wr [rid_slot] <= fs_wr[rid_slot] + 1'b1;
                if (rid_last) fs_valid[rid_slot] <= 1'b0;
            end

            // ---- 预取预算：+1（落盘一笔预取）/ −1（预取的那笔收完）----
            if (pf_alloc_ev && !pf_done_ev)      pf_busy <= pf_busy + 1'b1;
            else if (!pf_alloc_ev && pf_done_ev) pf_busy <= pf_busy - 1'b1;

            // ---- 新线检测（下一次 new_line 用）----
            if (rd_take) obs_tag_q <= rd_tag;

            // ---- 预取队列：压入 / 弹出（发出去 or 丢弃）----
            case ({pf_push, (pf_issue || pf_drop)})
                2'b10:   pf_cnt <= pf_cnt + 1'b1;
                2'b01:   pf_cnt <= pf_cnt - 1'b1;
                default: ;                                   // 同拍一进一出：不变
            endcase
            if (pf_push)             pf_wp <= pf_wp + 1'b1;
            if (pf_issue || pf_drop) pf_rp <= pf_rp + 1'b1;
            if (pf_push) pf_tag[pf_wp] <= pf_tgt;

            // ---- 新一跑：全冲刷（此时 rd_quiet 成立 ⇒ 没有任何迟到拍）----
            //   ★ 阶段②：lc_tag 阵列在 start 之前已经用 512 拍顺序写 0 了（§5 的 flush
            //     阶段，见 start 闸门），所以这里不再需要 lc_valid<=0 那一下；
            //     留在 6.11 的无复位块里会让阵列变成"两个写源" ⇒ 推断不成 LUTRAM。
            if (start_fire) begin
                fs_valid  <= {NSLOT{1'b0}};
                fs_pf     <= {NSLOT{1'b0}};
                for (ce = 0; ce < NSLOT; ce = ce + 1) begin
                    fs_out[ce] <= PF_Z;
                    fs_wr [ce] <= {IDX_W{1'b0}};
                end
                arq_cnt   <= {ARQ_W{1'b0}};
                arq_wp    <= {ARP_W{1'b0}};
                arq_rp    <= {ARP_W{1'b0}};
                pf_cnt    <= {PFQW_W{1'b0}};
                pf_wp     <= {PFW_W{1'b0}};
                pf_rp     <= {PFW_W{1'b0}};
                pf_busy   <= 3'd0;
            end

            // 调试计数：清零放最后（优先于本拍的 +1）
            if (start_fire || clr_cnt_rd) begin
                cnt_fill <= 16'd0;
                cnt_pf   <= 16'd0;
            end
        end
    end

    // ---- 6.11 两个阵列：**各自的 always_ff，不带复位** ----
    //   Vivado 对"敏感表里带异步复位的数组"拒推断 RAM（conv 侧踩过 8-4767 /
    //   8-13159：512×64 溶成 32768 个 FF + 512:1 组合 mux，是那版 −10.5 ns 的根因）。
    //   ⚠ u_rd_data 的读口在 §5（"t 拍给地址、t+1 拍数据"的 BRAM 语义，不能挪）。
    always_ff @(posedge s_axi_aclk) begin
        if (r_fire) win_q[{rid_idx, rid_wr}] <= m_axi_rdata;
    end
    // ★ 阶段②：tag 阵列现在**只有两个写源，且互斥**：
    //     · do_alloc   ：写 {valid=1, 线号高 21 位}（一次性把"在用/在填"标出来）
    //     · flush_busy ：start 前顺序写 0（LC_N 拍），让上一次运行的条目全部失效
    //   互斥性：flush 期间 run_active=0 ⇒ 单元不读 ⇒ rd_miss=0 ⇒ 没有 alloc
    //   （§9(4f) 有逐拍断言）。单一写口 ⇒ 仍是分布式 RAM（LUTRAM），不会溶成 FF。
    always_ff @(posedge s_axi_aclk) begin
        if (do_alloc)
            lc_tag[al_idx] <= {1'b1, al_tag[TAG_W-1:IDX_W]};
        else if (flush_busy)
            lc_tag[flush_ptr] <= {TGW{1'b0}};
    end



    // 需求读计数（DBG_RD[15:0]）
    // ★ 2026-10-30 时序（逐值等价）：rd_want 从 CE 挪到数据端，CE 只剩 u_ce。
    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn)  cnt_rdreq <= 16'd0;
        else if (clr_cnt_rd) cnt_rdreq <= 16'd0;
        else if (u_ce)       cnt_rdreq <= cnt_rdreq + {15'd0, rd_want};
    end

    // ========================================================================
    // 7. 写侧：字节 → beat → burst → AXI
    // ========================================================================
    // ------------------------------------------------------------------------
    // 7.0 写口入口寄存器：把写回路径切成"吞字节"和"合并/记账"两级（2026-10-30）
    // ------------------------------------------------------------------------
    // 时序问题（综合后 150 MHz 的实测）：本文件的违例端点里
    //     bf_data_reg[*]（~384 条 / 19 级 / 最差 −3.903）、
    //     be_data_reg（128 条 / 18 级 / −3.377）、
    //     u_rd_data_reg（64 条 / 17 级 / −2.919）
    // 全部落在**同一条共享锥**上：
    //     u_ce（含读地址 → rd_hit → ...）→ wr_byte → merge_* → push_now
    //     → bf_data/be_data/df_addr 的写使能，
    // 写侧自己还额外贡献 be_same_word（29 位比较）+ push_chain（29 位加法 + 比较）。
    // 改法（与 conv_uni_axi_wrap.v §7.0 同构，那边 7 个形状逐字节验证过）：
    //   第 0 级（引擎/单元输出域，本来就是寄存器）
    //       u_wr_en / u_wr_addr / u_wr_data ⇒ wr_byte = u_wr_en && u_ce（只剩 1 级 AND）
    //   第 1 级（s_axi_aclk 域，新增 p_valid/p_addr/p_data）
    //       ⇒ w_lane/w_word/be_same_word/be_strb_nx/merge_*/push_now/push_chain
    //         全部从**本模块自己的寄存器**出发，与 u_ce 锥彻底解耦。
    // 吞吐不变：字节流每拍进 1 个、每拍并 1 个 —— 入口寄存器是"固定的 1 级流水"，
    //   不是 valid/ready 握手（合并逻辑同拍把 p_data 组合吃掉，同拍再写入下一个
    //   字节），所以连续字节流一个都不会丢。
    // 三条必须一起改的配套逻辑（不改就是静默丢字节 / 静默溢出）：
    //   ① 不变式 p_valid ⇒ wst==W_IDLE。推导：吞字节要求 u_ce=1，而 u_ce 在
    //      (wst!=W_IDLE && wr_take) 时恒 0；反向（本拍刚吞字节、下一拍 wst 要变
    //      W_DR1）由 drain_cond 里的 !p_valid && !wr_byte 拦住 ⇒ 吞下的字节
    //      一定在 W_IDLE 被合并（**可能等 1~2 拍**，见下面的"时序刀 5"）。断言见 §9 (3b)。
    //   ★ 2026-10-31 时序刀 5（v38）修正了这一条的**这句**：原注释说"下一拍一定被合并"，
    //     而 `p_valid <= wr_byte` 的实现把"并不掉"变成"丢字节"。现在 p_merge 用
    //     u_ce-free 的 !wide_pend，p_valid 改成"并不掉就等"（wr_byte 装载 / p_merge 清 0），
    //     所以"下一拍"这句换成了"一定在 W_IDLE 被合并"。u_ce 从此**不再进 push 锥**。
    //   ② drain_cond 加 !p_valid && !wr_byte（否则排空跨过未合并的字节）。
    //   ③ wr_all_idle 加 !p_valid（否则"最后一个字节还在寄存器里"就报 done，
    //      PS 提前读内存 ⇒ 静默错数据）。
    // ------------------------------------------------------------------------
    // push_beat：把一个 beat 推进 beat FIFO；必要时收掉上一笔 burst 描述符。
    //   chain 条件 = 字地址正好是"上一拍 +8"、不跨 4 KB、未到拍数上限。
    // 只负责"数据/指针/长度"；**计数器在下面统一加减**（见 FIFO 记账那段注释）
    task automatic push_beat(input [28:0] t_word, input [63:0] t_data,
                             input [7:0] t_strb, input t_chain);
        begin
            bf_data[bf_wp] <= t_data;
            bf_strb[bf_wp] <= t_strb;
            bf_wp <= (bf_wp == BF_LAST) ? {BF_W{1'b0}} : (bf_wp + 1'b1);
            if (t_chain) begin
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
    // 排空触发（2026-10-31 改写 —— 这是"1 字节 = 1 beat = 1 笔 AW"的根因）
    // ------------------------------------------------------------------------
    // 旧式（错的）：`... && (!u_wr_en || wr_fifo_press)`
    //   只要出现 `!u_wr_en && !p_valid` 这一拍就排空（= 字节间空隙 >=2 拍）。
    //   bin_conv_core 每输出像素要算 K=C_B*k² 个 term 才写 1 个字节（K=1..36+），
    //   字节间空隙 = K 拍 ⇒ **每个字节都排空** ⇒ 每个字节都把当前 burst 收笔
    //   ⇒ cnt_burst == cnt_wbeat（板上实测 203/218 层）、1 笔 AW 只搬 1 字节，
    //   写引擎的 AW 往返延迟（由板上数据反推 <= ~106 拍/笔）完全暴露。
    //   （rprelu/sam/interp 是 8 拍连写，空隙只有 0~1 拍 ⇒ 旧规则基本不触发，
    //     所以那三个单元的 8 字节合并一直是好的 —— 本改动对它们无副作用。）
    // 新式：只有三个**真**排空理由
    //   ① wr_fifo_press : beat FIFO 到压力线。"有 beat 必有描述符"的防死锁通道，
    //                     与旧式完全一致（这是它唯一必须保留的旧语义）。
    //   ② unit_done_q   : 单元已 done（粘滞到下一次 start）。收尾：把最后一个
    //                     字节的 open beat 与最后一笔 burst 落盘，done 才可能成立。
    //   ③ wr_gap_tmo    : 写口连续 WR_GAP_TMO 拍没有新字节（饱和计数器，纯 FF）。
    //                     兜底，防"单元不再写但也没 done"（正常形状走不到）。
    // ⚠ 别把 `!u_wr_en` 加回来。真要恢复"空闲即排空"，那 ① 就白做了。
    //   （正确性不依赖排空时机：be_* 一直保持到被推进，字节顺序/数据/地址都不变，
    //     排空只决定"什么时候落 beat / 什么时候收笔"，收紧它只会让 burst 更长。）
    //   !p_valid && !wr_byte（§7.0 配套改动，少了就是"白多一笔 AXI 事务"）保留：
    //   入口寄存器里还有字节没并进 be_* 时不许排空，否则排空状态机会跨过它把
    //   be_* 冲掉、字节留到下一轮。
    assign wr_gap_tmo  = (wr_gap_cnt == WR_GAP_TMO);
    assign drain_cond = ((be_open || (bu_len != 9'd0)) && !p_valid && !wr_byte &&
                         !wr_wide_b && !pw_valid &&
                         (wr_fifo_press || unit_done_q || wr_gap_tmo));

    // ------------------------------------------------------------------------
    // FIFO 记账全部放在**一处**（重要）：
    //   bf_cnt / df_cnt 既会被"推入"（字节合并、排空）改，也会被"弹出"
    //   （写引擎）改。若两处各写一次同一条 always_ff 里的非阻塞赋值，
    //   同拍一推一弹就会丢掉一次更新（计数器偏 1 → 描述符错位 → 静默错数据）。
    //   所以这里先用**组合**逻辑判出 push/pop，再在 always_ff 里做一次
    //   'cnt <= cnt + push - pop'。
    // ------------------------------------------------------------------------
    assign merge_done   = p_merge && be_same_word &&
                          (be_strb_nx == 8'hFF);          // 整字凑满 ⇒ 落拍
    assign merge_change = p_merge && !be_same_word;
    assign drain_pub    = (wst == W_DR1) && be_open;
    // 本拍会推一个 beat 吗？（三种情况推的都是 open beat：be_word/be_data/be_strb）
    assign push_now     = merge_done || (merge_change && be_open) || drain_pub || pw_valid;
    // ★ 整字路径自己的"能不能串进当前 burst"判据：必须用**它自己的字地址**，
    //   不能用 be_word（那是逐字节路径里那个还没落拍的字）。
    //   ⚠ 全部改用**入口寄存器** pw_word（不再看 u_wr_addr）：链/字地址比较从
    //     本模块自己的 FF 出发，与引擎那条锥彻底解耦。
    assign wide_chain   = (bu_len != 9'd0) &&
                          (pw_word == bu_next) &&
                          (pw_word[28:9] == bu_base[28:9]) &&
                          (bu_len < BU_MAX);
    assign push_chain_x = pw_valid ? wide_chain : push_chain;
    assign push_word_x  = pw_valid ? pw_word : be_word;
    // 这个 beat 会不会串进当前 burst（不串 ⇒ 要先收掉上一笔描述符）
    //   ⚠ 2026-10-30（时序优化 ②）：原式 be_word == (bu_base + {20'd0, bu_len})
    //   是 29 位加法器 + 29 位比较器，而加法结果要等 bu_len 更新后的**下一拍**
    //   才用得上 ⇒ 把加法器整个搬进寄存器 bu_next（恒等于 bu_base + bu_len，
    //   归纳证明见 §7.0b）。这里只剩两排寄存器打进去的 29 位相等比较。
    assign push_chain   = (bu_len != 9'd0) &&
                          (be_word == bu_next) &&
                          (be_word[28:9] == bu_base[28:9]) &&
                          (bu_len < BU_MAX);
    //   （整字路径的等价物是下面的 wide_chain，见 §7.0c）
    // 注意 bu_len != 0 这一项：push_beat 只在 bu_len != 0 时才真的写描述符
    // （bu_len==0 表示这笔 burst 刚起头，还不需要收笔）。少了它 desc FIFO
    // 的计数就和实际描述符个数错位 → 写引擎会读到陈旧描述符。
    assign desc_push    = (push_now && !push_chain_x && (bu_len != 9'd0)) ||
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
            pw_valid <= 1'b0; pw_word <= 29'd0; pw_data <= 64'd0;
            bf_wp <= {BF_W{1'b0}}; bf_rp <= {BF_W{1'b0}}; bf_cnt <= BF_Z;
            df_wp <= {DF_W{1'b0}}; df_rp <= {DF_W{1'b0}}; df_cnt <= DF_Z;
            wst   <= W_IDLE;
            axst  <= AX_IDLE; ax_addr <= 32'd0; ax_len <= 9'd0; ax_sent <= 9'd0;
            ax_awvalid <= 1'b0; ax_wvalid <= 1'b0; ax_bready <= 1'b0;
            // ★ 2026-10-31（tb 侧发现）：这两个**诊断计数器**原来不在复位链里，
            //   只由 "+1" 和"写 0xE8 清 0"驱动 ⇒ 仿真起点是 X，而 X+1 恒为 X。
            //   板上上电是 0 所以不是功能 bug，但 tb 必须先显式清一次才能读。
            //   挂进 aresetn（纯调试路径，零功能风险）。
            cnt_burst <= 16'd0;
            cnt_wbeat <= 16'd0;
        end else begin
            // ---------------- FIFO 计数（每拍一次，加减同拍都对）----------------
            bf_cnt <= bf_cnt + (push_now ? 1'b1 : 1'b0) - (bf_pop ? 1'b1 : 1'b0);
            df_cnt <= df_cnt + (desc_push ? 1'b1 : 1'b0) - (desc_pop ? 1'b1 : 1'b0);

            // ---------------- 7.0a 写口入口寄存器（1 拍）----------------
            //   吞字节：wr_byte = u_wr_en && u_ce（只剩 1 级 AND）
            //   p_* 里的字节在**下一拍**被 7.1 合并；连续字节流时每拍进 1 个、每拍并 1 个，
            //   吞吐不变（固定的 1 级流水，不是握手）。
            //   ★ 2026-10-31 时序刀 5（v38）：原来是一句 `p_valid <= wr_byte;`
            //     = "下一拍必须并掉，并不掉就丢"。因为 p_merge 里带了 !wr_wide_b
            //     （含 u_ce），这条锥把 u_ce 一路拖到 bf_data 的 D-mux 选择上（−4.466 ns）。
            //     现在 p_merge 改用 u_ce-free 的 !wide_pend，被挡就**等**：
            //     p_valid 只在"并掉了"或"没有新字节"时清 0。
            //     等待是安全的：wide_pend / pw_valid 都只会持续 1~2 拍（都是本模块寄存器
            //     或单元输出），单元 done 之后 u_wr_en=0 ⇒ wide_pend=0 ⇒ 一定并得掉
            //     ⇒ 不会卡死；而 drain_cond / wr_all_idle 都带 !p_valid ⇒ 排空和 done
            //     也会等它，顺序不变。
            if (wr_byte) begin
                p_valid <= 1'b1;
                p_addr <= u_wr_addr;
                p_data <= {56'd0, u_wr_data[7:0]};
            end else if (p_merge) begin
                p_valid <= 1'b0;
            end

            // ---------------- 7.0c ★ 64-bit 整字直落一拍 beat ----------------
            //   单元一拍给出 8 个**连续且 8 字节对齐**的输出字节 ⇒ 一个满 WSTRB 的
            //   beat 完全成型，直接推 FIFO，不经过 be_*（逐字节攒）那一级。
            //   吞吐：1 beat/拍（旧路径是 1 字节/拍 ⇒ 8×）。
            //   反压：仍由 §5 的 u_ce（bf_cnt ≥ BF_PRESS 就停单元）负责，
            //         **不需要新握手**。
            //   ★ 2026-10-31：改成两级（吞字节 → 落盘），与 §7.0 字节路同构：
            //     第 0 级（单元输出域）：u_wr_addr / u_wr_data 只在 wr_wide_b 那拍进
            //        pw_*（**唯一**的消费者），所以 pw_* 的 D 端只挂 1 级 AND。
            //     第 1 级（本模块 FF）：push_beat 的全部输入（字地址、数据、链判据、
            //        FIFO 指针、描述符记账）都从寄存器出发。
            //   吞吐：连续整字流每拍 1 个 —— 上一拍的 beat 在 pw_valid 那拍落盘，
            //     同一拍 pw_* 再装下一个 ⇒ 1 beat/拍不变。
            //   收尾：最后那个 beat 停在 pw 里 1 拍，所以 wr_all_idle / drain_cond
            //     都加了 !pw_valid（否则 done 早报 ⇒ PS 提前读内存 ⇒ 静默错数据）。
            pw_valid <= wr_wide_b;
            if (wr_wide_b) begin
                pw_word <= u_wr_addr[31:3];
                pw_data <= u_wr_data;
            end

            // 落盘级：从入口寄存器推 beat
            if (pw_valid) begin
                be_open <= 1'b0;
                be_data <= 64'd0;
                be_strb <= 8'd0;
                push_beat(pw_word, pw_data, 8'hFF, push_chain_x);
            end

            // ---------------- 7.0b burst 记账：bu_next ≡ bu_base + bu_len ----------------
            //   推完一个 beat 之后，无论 chain 与否，都有
            //       新 bu_base + 新 bu_len = (该 beat 的字地址) + 1 = be_word + 1
            //     chain : bu_base 不变，bu_len' = bu_len+1，且 chain 成立时
            //             be_word == bu_base+bu_len ⇒ be_word+1 = bu_base + bu_len'
            //     不 chain: bu_base' = be_word，bu_len' = 1 ⇒ be_word+1 = bu_base' + 1
            //   ⇒ 只要 push 就写 be_word+1，否则保持。
            //   不变式的**精确形式**是 "bu_len != 0 ⇒ bu_next == bu_base + bu_len"
            //   （W_DR2 会把 bu_len 清 0 而不动 bu_base，那一拍之后该形式空洞成立；
            //    而 push_chain 的第一项就是 bu_len != 0 ⇒ 只在前者成立时用到 bu_next，
            //    下一次 push 时 bu_next 立刻恢复成 bu_base+bu_len）。断言见 §9 (2b)。
            if (push_now) bu_next <= push_word_x + 29'd1;

            // ---------------- 7.1 字节合并（处理入口寄存器里的那个字节）----------------
            if (p_merge) begin
                if (be_same_word) begin
                    be_data[w_lane*8 +: 8] <= p_data;
                    be_strb <= be_strb_nx;
                    if (be_strb_nx == 8'hFF) begin        // 整字凑满 ⇒ 立即落拍
                        push_beat(be_word, (be_data & ~(64'hFF << (w_lane*8)))
                                            | ({56'd0, p_data} << (w_lane*8)),
                                  be_strb_nx, push_chain);
                        be_open <= 1'b0;
                        be_data <= 64'd0;
                        be_strb <= 8'd0;
                    end
                end else begin
                    if (be_open) push_beat(be_word, be_data, be_strb, push_chain);  // 换字：先落上一拍
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
                    push_beat(be_word, be_data, be_strb, push_chain);   // 这一步可能又收一笔
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

            // ---------------- 7.6 写引擎 ----------------
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

    // ---- 写口"耐心窗口"计数器（§3 / §7.9）----
    //   逐值说明：wr_byte=1 那一拍清 0（刚来了新字节，窗口重新开始）；
    //   排空状态机在跑（wst != W_IDLE）时也清 0 —— 那两拍是"我们已经决定排空"
    //   的窗口，计数器不应在窗口里继续走（否则 W_DR2 出来的下一拍又立刻 tmo）。
    //   其余拍饱和 +1，到 WR_GAP_TMO 就保持。纯 FF + 1 个 12 bit 比较。
    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) begin
            wr_gap_cnt <= 12'd0;
        end else if (wr_byte || (wst != W_IDLE)) begin
            wr_gap_cnt <= 12'd0;
        end else if (!wr_gap_tmo) begin
            wr_gap_cnt <= wr_gap_cnt + 12'd1;
        end
    end

    assign m_axi_awvalid = ax_awvalid;
    assign m_axi_awaddr  = ax_addr;
    assign m_axi_awlen   = ax_len[3:0] - 4'd1;
    assign m_axi_wid     = {AXI_ID_W{1'b0}};   // 单一写源，写 ID 恒 0   // WR_BURST_MAX=8 ⇒ awlen≤7
    assign m_axi_wvalid  = ax_wvalid;
    assign m_axi_wdata   = bf_data[bf_rp];
    assign m_axi_wstrb   = bf_strb[bf_rp];
    assign m_axi_wlast   = (ax_sent == (ax_len - 9'd1));
    assign m_axi_bready  = ax_bready;

    // done 上报前必须成立：写侧全空（保证 PS 看到 done=1 时内存已一致）
    //   ⚠ !p_valid（§7.0 配套改动 ③，少了就是静默错数据）：入口寄存器里可能还有
    //   一个刚吞下、还没并进 be_* 的字节。少了这一项，最后一个字节会在"已经报
    //   done"之后才落进 FIFO ⇒ PS 提前读内存 ⇒ 静默错数据（只在"最后一个字节
    //   正好落在 done 前 1 拍"的形状上偶发，很难在随机激励里撞到）。
    assign wr_all_idle = (wst == W_IDLE) && !be_open && (bu_len == 9'd0) &&
                         !p_valid && !pw_valid &&
                         (bf_cnt == BF_Z) && (df_cnt == DF_Z) && (axst == AX_IDLE);

    // ------------------------------------------------------------------------
    // 7.9 bf_cnt / df_cnt 峰值核算 + 描述符可达性（2026-10-31 重新核算）
    // ------------------------------------------------------------------------
    // 记号：BF = WR_BUF_BEATS = 16，BF_PRESS = BF-4 = 12，DF = BF，DF_PRESS = 12，
    //       BU_MAX = WR_BURST_MAX = 8。bf_cnt 每拍最多 +1（一次 push）、最多 −1（一次 pop）。
    // push 源只有两个，且同拍互斥：
    //   (P1) 合并推 beat（merge_done / merge_change && be_open）：要求本拍 p_merge=1，
    //        而 p_merge 的字节是**上一拍**吞下的 ⇒ 上一拍必须 !(wr_take && wr_fifo_press)
    //        ⇒ 上一拍 bf_cnt ≤ BF_PRESS-1 = 11、df_cnt ≤ 11。
    //   (P2) 排空推 beat（W_DR1 的 drain_pub）：要求 !p_valid && !wr_byte，
    //        且 drain_cond 成立；一次排空事件最多推 **1** 个 beat（W_DR1 只进一次），
    //        推完 be_open=0 ⇒ 同一个 open beat 不会被推第二次。
    // 逐拍推（本拍看到的 bf_cnt 是"上一拍沿"的值）：
    //   t   : bf_cnt(t) ≤ 11，吞下字节                        （push 最早在 t+1）
    //   t+1 : bf_cnt ≤ 12（可能 +1），push 发生 ⇒ bf_cnt(t+2) ≤ 13
    //   t+2 : bf_cnt ≥ 12 ⇒ 进入压力区 ⇒ P1 不可能再成立（新字节被 u_ce 挡住）；
    //         排空在这之后还会推 1 次（W_DR1，P2）⇒ bf_cnt(t+3) ≤ 14
    //   t+3 : 既没有新字节（压力），也没有新的 open beat（刚被推掉）⇒ 不再 push
    //   ⇒ **峰值 = 14 = BF-2**（与 2026-10-30 那一版的结论逐位相同）。
    //   验证："若阈值不 −1（BF-3=13）"的反例仍然成立：t+1 那拍 bf_cnt=12 < 13 ⇒
    //   又会吞一个字节 ⇒ t+2 再 push 一次 ⇒ 峰值 BF-1（余量 0）。所以 BF_PRESS=BF-4 别动。
    // df_cnt 同理（desc_push 只来自 push_now 或 W_DR2，一次 push 最多 1 个描述符），
    //   峰值同样 = DF-2 = 14。余量：DF - 14 = 2。
    //
    // ★ 描述符可达性（就是旧参数 WR_BURST_MAX=16 的那个坑，本版修掉）：
    //   ax_can_start 要求 bf_cnt >= df_len[df_rp]。而 bf_cnt 峰值 = 14
    //   ⇒ **df_len=16 的描述符永远等不到 16 个 beat** ⇒ 一旦生成就是死锁。
    //   旧参数 WR_BURST_MAX = 16 = BF 时，这个状态在"数学上不可达"但**没有余量**：
    //     · bu_len 要长到 16 需要连续 16 次 push 而 bf_cnt 不越过 12 ⇒ 不可能
    //       （第 12 次 push 后单元就被压力冻住）⇒ 所以现场没死锁，只是"16 拍
    //       突发永久退化"（观测到最长 13 拍，正好卡在 14 下面）。
    //   本版把 BU_MAX 降到 8 ⇒ 8 <= 14，余量 6 拍。并且有**归纳保证**：
    //     一个长度为 L 的描述符被 push 的那一拍，它自己的 L 个 beat **一定都还在
    //     beat FIFO 里**（描述符是"收笔"时写的，而 L 个 beat 早已 push；能弹 beat
    //     的只有写引擎，写引擎要弹必须先有**本笔**的描述符 —— 还没有）
    //     ⇒ push 那一拍 bf_cnt ≥ L；此后 bf_cnt 只会被"更早的 burst"的 pop 减，
    //     本笔的 L 个 beat 只能被本笔的描述符弹 ⇒ ax_can_start 单调可信
    //     ⇒ **L ≤ BU_MAX ≤ 14 时不可能死锁**。取 8 是为了留够余量。
    //
    // 死锁检查（压力路径，与旧版一致）：压力把单元冻住，但 AXI 写引擎跑在
    //   s_axi_aclk 上（不受 u_ce 影响）；bf_cnt ≥ 12 时若 be_open/bu_len 还没收尾，
    //   drain_cond 会因为 wr_fifo_press 立刻成立 ⇒ 描述符落进 df FIFO ⇒
    //   ax_can_start 可满足（bf_cnt ≥ df_len，因为 df_len ≤ 8 ≤ 12）⇒ 一定有 pop
    //   ⇒ 压力解除。不会出现"单元冻住 + FIFO 有数据 + 无描述符"的僵局。
    //
    // ★ 2026-10-31 排空规则改动对峰值**没有影响**：新式把 `!u_wr_en` 换成了
    //   {wr_fifo_press, unit_done_q, wr_gap_tmo} 三个寄存器条件，P1/P2 的前提
    //   （一个来自上一拍吞字节、一个来自 W_DR1 且 !p_valid && !wr_byte）逐条没变；
    //   而 wr_gap_tmo / unit_done_q 只可能在"很久没有新字节"或"单元已 done"时成立，
    //   那时 bf_cnt 只会更低（长时间没有 push）。所以峰值仍是 14。
    // ------------------------------------------------------------------------

    // ------------------------------------------------------------------------
    // 7.10 【未实现】写引擎的 AW 流水（下一步，收益/风险都写在这里）
    // ------------------------------------------------------------------------
    // 现状：AX_IDLE →（等 can_start）→ AX_AW →（awready）→ AX_W →（wlast）→ AX_B
    //       →（bvalid）→ AX_IDLE。下一笔 AW 必须等上一笔的 B ⇒ 每笔付一次
    //       "AW + W + B" 的完整往返。
    // 目标（不改任何端口/协议语义）：
    //   · 新增一个 **长度队列** wq[0:1]（2 项）：AW 被接收时把 awlen 入队；
    //     W 通道从队头取长度、独立逐拍发 beat，wlast 用"剩余拍数==1"给出；
    //   · AW 通道独立：只要 !aw_pend && 队列不满 && df_cnt!=0 && bf_cnt>=df_len，
    //     就装载下一笔 AW。**判据与现在一字不差**（描述符的 beat 必须先到齐），
    //     所以"W 数据顺序 = AW 顺序"仍然成立（同一 ID、同一条 beat FIFO 顺序）；
    //   · B 通道：bready 恒 1，只做 b_cnt 记账（aw_fire +1 / bvalid -1），
    //     wr_all_idle 加 !aw_pend && wq_cnt==0 && !w_busy && b_cnt==0。
    //   · 这样 AW 的往返延迟就能被上一笔的 W 流盖住；AXI3 的
    //     NUM_WRITE_OUTSTANDING=16（BD 里就是这个值）允许在飞多笔。
    // 风险（为什么本轮不做）：
    //   ① 没有 xsim 回归（tb_bin_unit_axi_wrap.v 的端口还是旧的 m_axi_rd_*/m_axi_wr_*，
    //      与当前 AXI3 端口对不上；verify_all.sh bin 只跑 4 个核，不跑 wrapper）
    //      ⇒ 一旦 B 记账错一个，就是"done 早报 / 数据还没落地"的静默错。
    //   ② 收益在当前瓶颈下很小：板上 227 层数据表明 PL 时间 ≈ 55 拍/term，
    //      是**读侧单线窗口**（r/f≈1，每个 term 一次线填充）在主导；写侧只在
    //      K≤3 的层（合计 <3% 的 PL 时间）才是限制项。做完 ① ② 之后写侧已经
    //      不是瓶颈，③ 的边际收益 <5%，不值得冒这个险。
    //   ③ 真要做，先把 tb 的端口改成当前 AXI3 端口并补一个"B 紊乱/awready 随机
    //      反压"的随机回归，再上板。
    // ------------------------------------------------------------------------

    // ========================================================================
    // 8. 运行期不变量 / AP_RETURN / AXI4-Lite 读通道
    // ========================================================================
    always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) begin
            flg_rd_misalign <= 1'b0;
            flg_rid_bad     <= 1'b0;
        end else if (clr_flg) begin
            flg_rd_misalign <= 1'b0;
            flg_rid_bad     <= 1'b0;
        end else begin
            // 单元发来的读地址必须 8 字节对齐（否则 AXI 传输非法）。
            // 四个单元都是对齐的；不齐 = 换了形状或改了单元，这里报出来。
            if (u_rd_en && (u_rd_addr[2:0] != 3'd0)) flg_rd_misalign <= 1'b1;
            if (r_fire && rid_hi_bad)                flg_rid_bad     <= 1'b1;
        end
    end

    // bit1 busy：把"已登记 start、还没真启动"那一两拍也算 busy，
    // 免得 PS 用 busy 上升沿判断"已开始"时漏掉这一窗口。
    wire       busy_w     = start_pend || (run_active && !done_sticky);
    // 只有"本次运行确实是 E1E2 且启动校验通过"才把单元 err_code 报上去；
    // 否则报启动校验码（旧行为）。这样配置错误不会被 E1E2 的 0 掩盖。
    wire [7:0] err_show   = ((run_unit_q == 3'd4) && !err_from_cfg) ? u_err : err_code;
    wire [31:0] ap_return = {16'd0, err_show, 6'd0, busy_w, done_sticky};

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

    logic [31:0] rd_mux;
    always_comb begin
        rd_mux = 32'd0;
        if (araddr_l[16]) begin              // 配置页
            // 0x00..0xFF 原有配置页（逐位不变）；0x100..0x3FF 扩展页
            if (araddr_l[9:8] == 2'b00) begin
            case (araddr_l[7:0])
            8'h00: rd_mux = {30'd0, r_ctrl[1], 1'b0};   // bit0 自清
            8'h10: rd_mux = ap_return;
            8'h18: rd_mux = r_c;
            8'h20: rd_mux = r_h;
            8'h28: rd_mux = r_w;
            8'h30: rd_mux = r_ho;
            8'h38: rd_mux = r_wo;
            8'h40: rd_mux = r_k;
            8'h48: rd_mux = r_s;
            8'h50: rd_mux = r_d;
            8'h58: rd_mux = r_pad;
            8'h60: rd_mux = r_shift;
            8'h68: rd_mux = r_alpha_shift;
            8'h6C: rd_mux = r_slope_shift;
            8'h70: rd_mux = r_cout;
            8'h80: rd_mux = r_in;
            8'h88: rd_mux = r_in2;
            8'h90: rd_mux = r_in3;
            8'h98: rd_mux = r_in4;
            8'hA0: rd_mux = r_wgt;
            8'hA8: rd_mux = r_w2;
            8'hAC: rd_mux = r_w3;
            8'hB0: rd_mux = r_bias;
            8'hB8: rd_mux = r_zeta;
            8'hBC: rd_mux = r_gamma;
            8'hC0: rd_mux = r_slope_b;
            8'hD0: rd_mux = r_out;
            8'hD8: rd_mux = r_alpha;
            8'hE0: rd_mux = {cnt_fill, cnt_rdreq};
            8'hE8: rd_mux = {cnt_wbeat, cnt_burst};
            8'hF0: rd_mux = {28'd0, u_busy, flg_rid_bad, flg_rd_misalign,
                             (r_hi_bad != 8'd0)};         // 28+4 = 32 bit
            // ★ 步骤③：只读诊断（写忽略）——填充总数里有多少是**预取**发的。
            //   "需求填 = DBG_RD[31:16] − 0xF4"，两条加起来才是 AR 笔数。
            8'hF4: rd_mux = {16'd0, cnt_pf};
            default: rd_mux = 32'd0;
            endcase
            end else begin
                // ---------------- 0x200.. 扩展页 ----------------
                case (araddr_l[9:0])
                10'h200: rd_mux = {29'd0, run_unit};
                10'h204: rd_mux = r_egrp;
                10'h208: rd_mux = r_edesc;
                10'h20C: rd_mux = {31'd0, r_e_hi_bad[0]};
                10'h210: rd_mux = r_eout;
                10'h214: rd_mux = {31'd0, r_e_hi_bad[1]};
                // E1E2 吞吐 DBG（写 0x230 清零）
                10'h230: rd_mux = cnt_e_rd;
                10'h234: rd_mux = cnt_e_cyc;
                10'h238: rd_mux = cnt_e_rdst;
                10'h23C: rd_mux = cnt_e_wrst;
                10'h240: rd_mux = cnt_e_wr;
                default: rd_mux = 32'd0;
                endcase
            end
        end
        // 保留页（A[16]=0）：读 0
    end

    assign s_axi_rdata  = rd_mux;
    assign s_axi_rresp  = 2'b00;
    assign s_axi_rvalid = rvalid;

    // ========================================================================
    // 9. 仿真期断言（translate_off，不综合）
    //    —— 只报"会让仿真白跑几小时"的那几类错，并对齐性能文档的建议
    // ========================================================================
    // synopsys translate_off
    // ---- (0) 参数合法性：编译期常量，仿真起点就查，报错一行结束（不综合）----
    //   步骤④ 起这些参数互相有约束关系（LC_N 决定散列位宽、NSLOT 决定 ARID 空间），
    //   配错任何一个都会静默错数据或静默死锁，所以必须"配错就跑不起来"。
    initial begin
        if ((LC_N < 2 * RD_WIN_WORDS) || ((LC_N & (LC_N - 1)) != 0))
            $fatal(1, "[BIN-WRAP] LC_N(%0d) 必须是 >= 2*RD_WIN_WORDS(=%0d) 的 2 的幂",
                   LC_N, 2 * RD_WIN_WORDS);
        if ((NSLOT < 2) || ((NSLOT & (NSLOT - 1)) != 0))
            $fatal(1, "[BIN-WRAP] NSLOT(%0d) 必须是 >=2 的 2 的幂", NSLOT);
        if (NSLOT > (1 << AXI_ID_W))
            $fatal(1, "[BIN-WRAP] NSLOT(%0d) > 2^AXI_ID_W(=%0d)：ARID 装不下填充槽号",
                   NSLOT, 1 << AXI_ID_W);
        if ((1 << DPW) != DEPTH)
            $fatal(1, "[BIN-WRAP] DPW(%0d) 与 DEPTH(%0d) 不一致", DPW, DEPTH);
        if (RD_WIN_WORDS > 16)
            $fatal(1, "[BIN-WRAP] RD_WIN_WORDS(%0d) > 16：AXI3 arlen 只有 4 bit",
                   RD_WIN_WORDS);
        if (HIW <= 0)
            $fatal(1, "[BIN-WRAP] 散列高位宽 HIW=%0d 必须 > 0", HIW);
        $display("[BIN-WRAP] 步骤④ 直接映射线缓存：LC_N=%0d（%0d KB, %0d 位散列, 槽=%0d）",
                 LC_N, (LC_N * RD_WIN_WORDS * 8) / 1024, LC_IW, NSLOT);
    end
    // ---- 槽号自检：每个槽记下"当初是为哪个字节地址取的数"，命中时必须完全一致 ----
    //  这条断言就是为了让"槽号位宽/位选写错"从静默数据错变成一行 ERROR。
    //   步骤② 起下标是 {条目号, 槽号}：任何一个"命中"的槽，当初必须是为这个地址填进来的
    logic [31:0] dbg_slot_addr [0:DEPTH-1];
    logic [31:0] stuck_cnt;          // §9(8)：描述符卡死看门狗（只仿真）
    // ★ 步骤③：(3h) AR 握手期间稳定性检查用
    logic        ar_stall_q;
    logic [31:0] ar_chk_addr;
    logic [AXI_ID_W-1:0] ar_chk_id;
    integer dq;
    always @(posedge s_axi_aclk) begin
        if (!s_axi_aresetn) begin
            for (dq = 0; dq < DEPTH; dq = dq + 1) dbg_slot_addr[dq] <= 32'hDEAD_BEEF;
        end else begin
            // 步骤①：槽号由拍计数器给 ⇒ 这拍落的槽，地址必然是 "线基址 + 槽号*8"
            // 步骤④：落点 = {RID 查出的表项号, 该槽自己的字数计数} ⇒ 地址必然是
            //         "该表项 tag + 该槽号"。任何"槽号/表项号/计数器"错都会在这里露。
            //   ★ 阶段②：阵列里只剩 {valid, tag[24:4]}，tag[3:0] 由条目号低位蕴含
            //     （断言 (4g) 逐拍证明这个蕴含关系），所以这里用 rid_idx[3:0] 补回低位。
            if (r_fire)
                dbg_slot_addr[{rid_idx, rid_wr}] <=
                    {lc_tag[rid_idx][VLD-1:0], rid_idx[IDX_W-1:0], rid_wr, 3'b000};
            if (u_ce && rd_want && rd_hit && (dbg_slot_addr[{rd_idx, rd_slot}] !== u_rd_addr))
                $display("[BIN-WRAP] ERROR: 命中槽地址不符 addr=%h idx=%0d slot=%0d slot_addr=%h",
                         u_rd_addr, rd_idx, rd_slot, dbg_slot_addr[{rd_idx, rd_slot}]);
        end
    end

    integer frozen_cnt;
    always @(posedge s_axi_aclk) begin
        if (!s_axi_aresetn) begin
            frozen_cnt <= 0;
            stuck_cnt  <= 0;
            ar_stall_q <= 1'b0;
            ar_chk_addr <= 32'd0;
            ar_chk_id  <= {AXI_ID_W{1'b0}};
        end else begin
            // (1) 时钟门控卡死报警（性能文档补记二建议的诊断）
            if (run_active && !u_ce) frozen_cnt <= frozen_cnt + 1;
            else                     frozen_cnt <= 0;
            if (frozen_cnt == 200000)
                $display("[BIN-WRAP] ERROR: 单元被冻住 200000 拍 rd_want=%0d rd_hit=%0d wst=%0d bf=%0d df=%0d",
                         rd_want, rd_hit, wst, bf_cnt, df_cnt);
            // (2) FIFO 溢出（本不该发生）
            if (bf_cnt > BF) $display("[BIN-WRAP] ERROR: beat FIFO 溢出 bf_cnt=%0d", bf_cnt);
            if (df_cnt > DF) $display("[BIN-WRAP] ERROR: 描述符 FIFO 溢出 df_cnt=%0d", df_cnt);
            // (2b) 写口入口寄存器不变式（§7.0）：p_valid 只允许在 wst==W_IDLE 时为 1。
            //     违反 ⇒ 那个字节在排空期既没被合并、又会被新字节冲掉 = 静默丢一个字节。
            //     （时序刀 5 之后 p_valid 会"等"，所以这里更要盯死：wst 只能在
            //       p_valid=0 时离开 W_IDLE，否则就卡死在等待里。）判定只有 1 个 LUT。
            if (p_valid && (wst != W_IDLE))
                $display("[BIN-WRAP] ERROR: 入口字节跨排空拍（会丢字节）wst=%0d", wst);
            // (2c) bu_next 归纳不变式（§7.0b）：bu_len != 0 ⇒ bu_next == bu_base+bu_len。
            //     它是 push_chain 里"两排寄存器比较"能替代 29 位加法器的唯一依据。
            if ((bu_len != 9'd0) && (bu_next !== (bu_base + {20'd0, bu_len})))
                $display("[BIN-WRAP] ERROR: bu_next != bu_base+bu_len base=%h len=%0d next=%h",
                         bu_base, bu_len, bu_next);
            // (3) R 回来但没有在飞请求（AR/R 记账错）
            if (m_axi_rvalid && !fs_valid[rid_slot])
                $display("[BIN-WRAP] ERROR: R 数据回来但该槽无在飞请求 rid=%0d", m_axi_rid);
            // (3b) ★ 步骤①：rlast 必须**正好**落在该槽的第 N 拍（fs_out==1 那拍）。
            //       rlast 早了 ⇒ 后面还有拍要收；rlast 晚了 ⇒ arlen 与实际拍数不符
            //       （参数/BD 配错，比如 ARBURST 被钉成 FIXED 时 8 拍读同一个字）。
            if (r_fire && (m_axi_rlast !== rid_last))
                $display("[BIN-WRAP] ERROR: rlast 与 burst 拍数不符 rlast=%0d fs_out=%0d arlen=%0d rid=%0d",
                         m_axi_rlast, fs_out[rid_slot], m_axi_arlen, m_axi_rid);
            // ---- ★ 步骤④（直接映射）的四条不变式 ----
            // (4a) 落盘的表项必须**没有在飞填充** —— 直接映射下唯一能防止"迟到 R 拍
            //      写进新线"的不变式（§6 开头的新风险）。破坏它 = 静默错数据。
            //   ★ 阶段②：闸门从 lc_busy[idx] 换成 fill_in_flight(idx)（两者逐位等价）
            if (do_alloc) begin : ovl_chk
                integer ov;
                for (ov = 0; ov < NSLOT; ov = ov + 1)
                    if (fs_valid[ov] && (fs_idx[ov] == al_idx))
                        $display("[BIN-WRAP] ERROR: 覆盖了仍有在飞填充的表项 idx=%0d tag=%h",
                                 al_idx, al_tag);
            end
            // (4f) ★ 阶段②：冲刷期不许有 alloc（否则 tag 阵列"两个写源同拍" ⇒
            //      落盘单写口的前提被破坏，且有一条目清不干净 ⇒ 跨运行假命中）
            if (flush_busy && do_alloc)
                $display("[BIN-WRAP] ERROR: tag 冲刷期间发生 alloc idx=%0d", al_idx);
            // (4g) ★ 阶段②：**"低 IDX_W 位由条目号蕴含"这条不变式**逐拍验证。
            //      tag 只存高 21 位的唯一依据：任何一条被写进 idx 的线号，
            //      它的低 IDX_W 位必须正好等于 hash(该线号) 的低 IDX_W 位
            //      （散列函数把 tag 低位原样搬进 idx 低位 ⇒ 二者恒等）。
            //      这条一旦不成立，"只比高 21 位"就可能假命中 ⇒ 静默错数据。
            if (do_alloc && (al_tag[IDX_W-1:0] !== al_idx[IDX_W-1:0]))
                $display("[BIN-WRAP] ERROR: tag 低位与条目号低位不相等 tag=%h idx=%0d",
                         al_tag, al_idx);
            // (4h) ★ 阶段②：start_fire 那一拍，阵列必须**真的全 0**（逐条查，只仿真）。
            //      start_fire 是冲刷结束的下一拍（非阻塞写已经落地），所以这里能查全。
            if (start_fire) begin : flush_chk
                integer fq;
                for (fq = 0; fq < LC_N; fq = fq + 1)
                    if (lc_tag[fq] !== {TGW{1'b0}})
                        $display("[BIN-WRAP] ERROR: 冲刷后条目 idx=%0d 仍非 0 (%h)",
                                 fq, lc_tag[fq]);
            end
            // (4b) 落盘用的填充槽必须是空的（同拍不可能被别的分配抢走：一笔/拍）
            if (do_alloc && fs_valid[al_s])
                $display("[BIN-WRAP] ERROR: 用了非空的填充槽 s=%0d", al_s);
            // (4c) 预取不许填一条"已在缓存里/正在填"的线（否则白烧带宽 + 挤掉在用的线）
            if (pf_issue && (pf_tag_eq || pf_blocked))
                $display("[BIN-WRAP] ERROR: 预取了已在缓存/在填的线 tag=%h idx=%0d", pf_head, pf_hidx);
            // (4d) 同拍"落盘新填充"与"某填充收完"不许撞同一个表项
            //      （撞了 ⇒ lc_busy 的 1/0 同拍写、last-write-wins 会丢一笔在飞标记）
            if (do_alloc && r_fire && rid_last && (al_idx == rid_idx))
                $display("[BIN-WRAP] ERROR: 同拍落盘/收完撞同一表项 idx=%0d", al_idx);
            // (4e) 每个"有在飞拍"的槽必须 fs_out!=0（否则计数与有效位不一致）
            begin : fs_out_chk
                integer fo_i;
                for (fo_i = 0; fo_i < NSLOT; fo_i = fo_i + 1)
                    if (fs_valid[fo_i] && (fs_out[fo_i] == PF_Z))
                        $display("[BIN-WRAP] ERROR: 槽有效但未回拍数为 0 s=%0d", fo_i);
            end
            // (3g) 预取预算 / AR 队列 / 预取队列不许溢出
            if (pf_busy > PF_MAX3)
                $display("[BIN-WRAP] ERROR: 在飞预取笔数超上限 pf_busy=%0d", pf_busy);
            if (arq_cnt > ARQ_FULL)
                $display("[BIN-WRAP] ERROR: AR 队列计数溢出 arq_cnt=%0d", arq_cnt);
            if (pf_cnt > PFQ_FULL)
                $display("[BIN-WRAP] ERROR: 预取队列计数溢出 pf_cnt=%0d", pf_cnt);
            // (3h) ★ AXI 硬规则：arvalid 挂住等握手时，地址/ID 不许变、arvalid 不许撤。
            //      旧版靠"只有一个 arvalid_r 寄存器"天然满足；改成队列后必须显式查。
            if (ar_stall_q && (m_axi_arvalid !== 1'b1 ||
                               (m_axi_araddr !== ar_chk_addr) ||
                               (m_axi_arid   !== ar_chk_id)))
                $display("[BIN-WRAP] ERROR: arvalid 挂住期间被撤销/改地址 addr=%h->%h id=%0d->%0d",
                         ar_chk_addr, m_axi_araddr, ar_chk_id, m_axi_arid);
            ar_stall_q <= m_axi_arvalid && !m_axi_arready;
            ar_chk_addr <= m_axi_araddr;
            ar_chk_id   <= m_axi_arid;

            // (4) 写 burst legality：不跨 4 KB、不超拍数上限
            if (m_axi_awvalid && m_axi_awready &&
                (({20'd0, m_axi_awaddr[11:0]} + {4'b0000, m_axi_awlen} * 8) > 12'd4095))
                $display("[BIN-WRAP] ERROR: 写 burst 跨 4 KB awaddr=%h awlen=%0d",
                         m_axi_awaddr, m_axi_awlen);
            if (m_axi_awvalid && (m_axi_awlen >= (BU_MAX[7:0])))
                $display("[BIN-WRAP] ERROR: 写 burst 超过 WR_BURST_MAX awlen=%0d",
                         m_axi_awlen);
            // (5) 读地址必须对齐（同 flg_rd_misalign，但直接打到日志）
            if (u_rd_en && (u_rd_addr[2:0] != 3'd0))
                $display("[BIN-WRAP] ERROR: 单元读地址非 8 字节对齐 addr=%h", u_rd_addr);
            // (6) ★ 2026-10-31：描述符可达性（§7.9）的**运行期**证据。
            //     push 描述符那一拍，这笔描述符的 bu_len 个 beat 必须都还在
            //     beat FIFO 里（否则 ax_can_start 永远等不到 ⇒ 死锁）。
            //     这是 WR_BURST_MAX 参数配错时最先炸的一条。
            if (desc_push && (bf_cnt < bu_len))
                $display("[BIN-WRAP] ERROR: 描述符拍数 > FIFO 存量（会死锁）bu_len=%0d bf_cnt=%0d BU_MAX=%0d",
                         bu_len, bf_cnt, BU_MAX);
            // (7) ★ 2026-10-31：push 的 beat 必须至少有一个字节有效。
            //     be_strb==0 的 beat 是纯浪费（还会占用 AXI 带宽），
            //     只可能来自"排空时把空的 be_* 落盘"这类 bug。
            if (push_now && !pw_valid && (be_strb == 8'd0))
                $display("[BIN-WRAP] ERROR: push 了一个空 beat（WSTRB=0）word=%h", be_word);
            // (8) ★ 2026-10-31：描述符"卡死"看门狗（死锁的可观测化）。
            //     描述符在队头但 beat 永远不够 ⇒ 写引擎空转、单元被压力冻住。
            //     正常情况这个等待是 0 拍（描述符收笔时 beat 已经齐了），
            //     给 4096 拍余量（万一以后有人把 desc 提前 push）。
            if ((axst == AX_IDLE) && (df_cnt != DF_Z) &&
                ({1'b0, bf_cnt} < {1'b0, df_len[df_rp]})) begin
                if (stuck_cnt == 32'd4096)
                    $display("[BIN-WRAP] ERROR: 描述符卡死 4096 拍 df_len=%0d bf_cnt=%0d df_cnt=%0d",
                             df_len[df_rp], bf_cnt, df_cnt);
                stuck_cnt <= stuck_cnt + 32'd1;
            end else begin
                stuck_cnt <= 32'd0;
            end
        end
    end
    // synopsys translate_on

endmodule
