# 03 权重装载与片内权重 bank（纯静态分析）

> 分析对象：`/data/work/hls_c8_csynth/r9_k1_l16/solution1/syn/verilog/`（Vitis HLS 2025.2.1，target `xc7z015-clg485-2`，时钟约束 6.67 ns / 估算 5.782 ns）
> 源码：`pynq_specialized_code/pl/src/conv2d_engine.cpp`（996 行）、`conv2d_engine.h`
> 方法：只读 `grep -n` / `sed -n` 局部 + csynth 报告。**未编译、未仿真、未上板、未 commit。**
> 行号约定：`v:` = `conv2d_engine.v`，`cpp:` = `conv2d_engine.cpp`，`rpt:` = `conv2d_engine_csynth.rpt`，`axiv:` = `conv2d_engine_gmem0_m_axi.v`。

---

## 0. 结论速览

| 问题 | 结论 |
|---|---|
| 三种缓冲的形态 | wgt1_buf = 16×352×8 LUTRAM；wgt3_buf = 72×352×8 LUTRAM；wgt_buf = 25×2816×8 BRAM（**50 块 BRAM18K**，不是 51，见 §1.4） |
| 铁律 | **bank 号 = 编译期常量（被 `ARRAY_PARTITION complete` 拆掉的那几维）；地址 = 运行期（剩下的 `ic` 维）**。wgt_buf 因为 `oc` 没拆，`oc` 被塞进 12-bit 地址里（地址 = `oc*352 + ic`），代价是 8 个 oc 串行读 |
| 装载通路 | AXI gmem0 32-bit → m_axi 内部 R FIFO **4096×34 bit（≈8 BRAM18K）** → **没有中间 FIFO**，直接由 FSM 状态寄存器写 bank 写口（1 拍 1 beat） |
| 2.76 拍/字节 | **是 k=1 的拟合系数，不是 k=3/k=5 的实测量**（见 §3.1 的更正）。结构下界 1.0 拍/字节 + 短 burst 摊派 1.76 |
| 重装时机 | 每个 **(输出行 y, 列块 x0, OC 块 ocb)** 都重装一次（`ocb` 循环在 `y` 循环**内部**）⇒ 每层 `cin·cout·Hout·ceil(Wout/64)` 字节 |
| 重装总量 | 全网络 **830 MB**（k=1 237 / k=3 567 / k=5 26），≈ 2.29 G 拍 ≈ **15.3 s**（44.21 s 的 34.6%）；权重本体只有 1.545 MB ⇒ 平均**每个权重字节被重读 ~444 次** |
| 全驻留 | 全网络常驻 = 14.32 Mbit = **777 BRAM18K ⇒ 不可行**；单层常驻 k=1 最坏 29.3 块（可行）、k=3 最坏 96 块（不可行）。**正解是 ping-pong 双 bank + 预取**，不是常驻 |
| wgt_buf | 只服务 1 层（`encoder.conv_first`，cin=12,cout=48,k=5），那层权重 16.1 KB = 全网的 **0.90%**，却占 30.7% 的 BRAM。**可省**（缩深度到 LUTRAM 或直接删），但**不能改成计算期 DDR 流式** |
| PAD 对齐 | 12/28 字节容器保证 **4 字节**对齐、**只有一半 (oc,ic) 是 8 字节对齐** ⇒ 加宽到 8 字节必然半崩；且 `bundle=gmem0` 会把输入端口一起加宽 |

---

## 1. 三种权重缓冲的物理结构

### 1.1 声明与拆分方式（铁律的来源）

```cpp
 cpp:173   data_t wgt_buf [OC_BLOCK][CIN_MAX][KMAX][KMAX];   // [8][352][5][5]
 cpp:178   data_t wgt1_buf[OC_BLOCK * K1_BLK][CIN_MAX];      // [16][352]
 cpp:200   data_t wgt3_buf[OC_BLOCK][CIN_MAX][9];            // [8][352][9]
 cpp:201   HLS ARRAY_PARTITION variable=wgt_buf  complete dim=3
 cpp:202   HLS ARRAY_PARTITION variable=wgt_buf  complete dim=4
 cpp:203   HLS ARRAY_PARTITION variable=wgt1_buf complete dim=1
 cpp:204   HLS BIND_STORAGE   variable=wgt1_buf type=RAM_2P impl=LUTRAM
 cpp:205   HLS ARRAY_PARTITION variable=wgt3_buf complete dim=1
 cpp:206   HLS ARRAY_PARTITION variable=wgt3_buf complete dim=3
 cpp:207   HLS BIND_STORAGE   variable=wgt3_buf type=RAM_2P impl=LUTRAM
```

- `wgt1_buf` 拆 dim1（oc/车道）⇒ 16 个 bank，剩 dim2 = `ic` 当地址。
- `wgt3_buf` 拆 dim1（oc）× dim3（9 个抽头平面 `p=ky*3+kx`）⇒ 8×9 = **72 个 bank**，剩 dim2 = `ic` 当地址。
- `wgt_buf` 只拆 dim3/dim4（`(ky,kx)` 25 个抽头）⇒ **25 个 bank**；`oc` **没拆**，落进地址。
- 常数（`conv2d_engine.h`）：`CIN_MAX 352`(35)、`KMAX 5`(41)、`OC_BLOCK 8`(53)、`K1_LANES 16`(222)、`K1_BLK = K1_LANES/OC_BLOCK = 2`(cpp:42)、`K3_PAD12 1`(h:189)、`K5_PAD28 1`(h:209)。

### 1.2 结构表（RTL / 报告实测）

| 缓冲 | k | bank 数 | 每 bank 深度×位宽 | 单 bank 容量 | 总容量 | 实现 | 每 bank 资源 | 总资源 | 报告行 |
|---|---:|---:|---|---|---|---|---|---:|---:|---|
| `wgt1_buf` | 1 | **16** | 352 × 8 | 2,816 bit | 45,056 bit (5.5 KB) | **LUTRAM** (`distributed`) | 44 LUT + 16 FF | 704 LUT | rpt:184–199 |
| `wgt3_buf` | 3 | **72** | 352 × 8 | 2,816 bit | 202,752 bit (24.8 KB) | **LUTRAM** | 44 LUT + 16 FF | 3,168 LUT | rpt:200–271 |
| `wgt_buf` | 5 | **25** | 2816 × 8 | 22,528 bit | 563,200 bit (68.8 KB) | **BRAM** (`auto`→BRAM) | 2×BRAM18K | **50 BRAM18K** | rpt:272–296 |
| `wbuf`（写回行缓冲，不是权重） | — | 16 | 448 × 8 | 3,584 bit | 57,344 bit (7 KB) | LUTRAM | 56 LUT | 896 LUT | rpt:168–183 |

三个权重/行缓冲合计 **4,768 LUT**（rpt 内存表 Total LUT = 4,770），占设计 LUT 的一成多；`wgt_buf` 一个吃掉全设计 **163 块 BRAM18K 里的 50 块（30.7%）**（rpt:298 Total 行）。

RTL 实例化证据：

```text
v:3649..3996   25 个 conv2d_engine_wgt_buf_RAM_AUTO_1R1W #(.DataWidth(8),.AddressRange(2816),.AddressWidth(12))
v:3999..4255   16 个 conv2d_engine_wgt1_buf_RAM_2P_LUTRAM_1R1W  #(.DataWidth(8),.AddressRange(352),.AddressWidth(9))
v:4259..5395   72 个（wgt3_buf_U .. wgt3_buf_71_U），同一个 LUTRAM 模块、同样 352×9
```
模块本体：`conv2d_engine_wgt1_buf_RAM_2P_LUTRAM_1R1W.v:28` `(* ram_style="distributed" *) reg [7:0] ram[0:351];`、`conv2d_engine_wgt_buf_RAM_AUTO_1R1W.v:25,33` `(* ram_style="auto" *)` + read-first（同址写会先读旧值）。

### 1.3 哪个维度是常量、哪个是变量（铁律，含 grep 证据）

**读侧（计算）——bank 号是硬连线的，地址只有 `ic`：**

```text
Pipeline_..._LOOP_764_38.v:4723   assign wgt3_buf_9_address0   = zext_ln764_1_reg_3854;   // = ic
Pipeline_..._LOOP_764_38.v:4455   assign wgt3_buf_13_address0  = zext_ln764_1_fu_1887_p1;  // 同一根 ic
Pipeline_..._LOOP_684_33.v:2010   assign wgt1_buf_15_address0  = zext_ln684_fu_966_p1;     // = ic
```
72 / 16 个 bank 各自的 `address0` 全部由**同一根 `ic`** 驱动；每个 bank 的 `q0` 在流水里直接进各自的乘法器输入（`LOOP_764_38.v:1947 .din0(wgt3_buf_9_q0)`）。
=> 「8 个 oc × 9 抽头」这 72 路是**结构性并行**：bank 号在综合时定死，`ic` 只是地址。这就是"bank 索引必须编译期常量"的硬件形态。

**`wgt_buf`（k=5）是反例：**

```text
Pipeline_..._LOOP_892_47.v:3175   assign wgt_buf_1_address0 = zext_ln902_fu_1203_p1;
Pipeline_..._LOOP_892_47.v:2921   assign add_ln902_fu_1198_p2 = (phi_mul46 + trunc_ln902_fu_1194_p1);
```
`phi_mul46` 是 `oc*CIN_MAX` 的递推、`trunc_ln902` 是 `ic` ⇒ **地址 = `oc*352 + ic`（12 bit）**，也就是 `oc` 是运行期变量。
后果：一个 bank 一拍只出 1 字节，8 个 oc 必须**串行 8 拍**才凑齐同一个 (tap, ic) ⇒ k=5 路径每拍只能喂 1 个 MAC 车道。这就是"通用路径慢"的结构性原因，也解释了为什么只有 1 层 k=5 却要给它 25 个 bank 的 BRAM。

**写侧——bank 号同样是常量（`generate` 后的独立模块），写地址只有 `ic`：**

```text
v:17452  assign wgt3_buf_address1 = zext_ln521_1_fu_6498_p1;
v:17534  assign zext_ln521_1_fu_6498_p1 = ic_3_reg_3469;       // 9-bit = 352 深度
```
`wgt3_buf_0..71` 的 `address1/d1` 各自独立，只有 `ic` 共享 —— 一次 `ic` 迭代把 9 个抽头分别写进 9 个 bank（同一拍或相邻拍，见 §2.3）。

### 1.4 与「51 块 BRAM」的差异

csynth 报告：`wgt_buf` 25 个实例，每个 `BRAM_18K = 2`（22,528 bit / 18,432 bit = 1.22 → 2），合计 **50**；设计总计 163 = line_buf 7×16(112) + bias_buf 1 + wgt_buf 50。
若别处记到 51，那是 **impl（布线后）报告口径**或把 `bias_buf` 的 1 块算进去了 —— 综合口径下就是 50。**存疑（非结论性差异）。**

---

## 2. 装载通路

### 2.1 通路图

```
 DDR (HP0)
   │  AXI4 32-bit（C_M_AXI_GMEM0_DATA_WIDTH = 32，v:318）
   │  ↑ 这条端口被 in_fm / wgt / bias / out_lut / chsum 五个指针共用：bundle=gmem0（cpp:91-107）
   ▼
 conv2d_engine_gmem0_m_axi
   ├─ fifo_rreq  (AR 请求队列)  DEPTH = USER_MAXREQS = 7            axiv:394-398, v:7508
   ├─ burst_converter          MAX_READ_BURST_LENGTH = 256 拍      v:7509
   ├─ NUM_READ_OUTSTANDING = 16                                    v:7524
   └─ buff_rdata (R FIFO)      DATA_WIDTH = 34 (32+RLAST)
                               DEPTH = RBUFF_DEPTH = 16×256 = 4096 拍  axiv:334-335, 463-469
                               4096×34 = 139 kbit ≈ 8×BRAM18K（rpt 实例表 gmem0_m_axi_U BRAM=8）
   │  gmem0_0_RDATA[31:0] / gmem0_0_RVALID / gmem0_0_RREADY        v:554, 7584
   ▼
 【没有中间 FIFO】—— 直接进 FSM 的拍级消费者
   ├─ k=1：流水模块 Pipeline_VITIS_LOOP_475_16_VITIS_LOOP_479_17
   │        驱动 wgt1_buf_*_address1/ce1/we1/d1（64 个输出 = 16 bank × 4 线）
   └─ k=3 / k=5：顶层 one-hot FSM 的标量状态（无 PIPELINE）
            写使能 = gmem0_0_RVALID & state 89/90（k=3）           v:14447-14475
            写地址 = ic_3                                          v:17452/17534
            bank 写口 ← 直接来自 gmem0_0_RDATA 的字节切片/寄存
   ▼
 wgt3_buf[72] / wgt_buf[25] / wgt1_buf[16]
```

要点：
1. **R 数据是 32 bit/beat**，也就是说"一个 32-bit 字 = 1 拍"。
2. R FIFO 很深（4096 拍 = 16 KB），所以**通道本身有能力预取**；慢的不是带宽（全设计只用了峰值带宽的 ~5%），而是**消费者的取数节奏**（§3）。
3. 从 R FIFO 到 bank 写口之间**没有任何弹性缓冲**：一拍 RVALID 就写一拍 bank，`state` 转移被 `gmem0_0_RVALID` 直接门控（`v:14447`、`v:15709-15725`）。

### 2.2 一次 burst 有多长

| 路径 | 发 AR 的位置 | ARADDR | ARLEN（单位 = 32-bit 字） | 覆盖 |
|---|---|---|---|---|
| k=3 (PAD12) | FSM `state 78`（`v:9692`） | `sext_ln521` = 本 oc 的 `off12` | `empty_114 = (cin<<2) − cin = **3·cin**`（`v:16632`、`v:17072`、`v:16912`） | 12·cin 字节 = 8 oc 之外**一个 oc** 的全部 ic |
| k=5 (PAD28) | FSM `state 111`（`v:9711`） | `sext_ln570` | `empty_116 = (cin<<3) − cin = **7·cin**`（`v:16634`、`v:17070`、`v:16910`） | 28·cin 字节 / 一个 oc |
| k=1 | 流水内 `v:1262 assign m_axi_gmem0_0_ARLEN = p_cast_cast_reg_825 ← p_cast = empty_107`（`v:6280`） | 递推 `off1` | 每 oc `cin/4` 个字 | cin 字节 / 一个 oc |
| 填充（对照） | `state 61`（`v:9697/9717`） | `sext_ln364` | `p_cast151 = nwide-1`，`nwide = 8·lb` 字节（= 2·lb 个字） | 一整行块的 8 通道 = **≥512 字节连续** |

⇒ **权重 burst 的粒度是"一个 oc"（134 字节级），填充 burst 的粒度是"一行块"（≥512 字节级）**，这就是 §3 的关键差异。`3·cin` 超过 256 时由 m_axi 的 burst_converter 拆成多笔（`MAX_READ_BURST_LENGTH=256`）。

### 2.3 k=3 装载的逐拍分解（FSM 走位）

```
state 77  ├─ 取本 oc 的 ARADDR/ARLEN 寄存器（v:8650-8660，`ap_CS_fsm_state77` 块）
          └─ oc 循环判（icmp_ln517：i < ocbl）→ 完则 state 110
state 78  AR 握手（ARVALID & ARREADY 才走）                     v:15669-15676
state 79..85  固定 7 拍（AR→首拍延迟窗口）                        v:15677-15695
state 86  ic 循环判（icmp_ln521：ic < cin）→ 完则回 state 77      v:15696-15700
state 87  等 beat #0（RVALID）                                  v:15701-15706
state 88  等 beat #1（RVALID）                                  v:15707-15712
state 89  等 beat #2，并写 bank0..7（抽头 0..7 落在字节 0..7）    v:14440-14475
state 90  写 bank8（抽头 8 = 第 3 个字的第 0 字节）               v:14422（pred_..._state90）
state 91 → 86（下个 ic）
```
一个 `ic` = **6 个状态**（86,87,88,89,90,91）消费 **3 拍**、写 **9 字节** ⇒ 结构化下界 **6 拍 / 9 有用字节 = 0.67 拍/字节**（12 个容器字节 = 0.5 拍/字节）。
k=5 同形：`state 119`（ic 判）→ `state 111` 发 AR → 120..126 收 7 拍并锁字节 → 122..127 分 6 拍写 25 个 bank → `state 119`（`v:15839`）。
注意两侧的**写口形态不同**：k=3 是"3 拍收、2 拍写（89: 8 bank，90: 1 bank）"，k=5 是"7 拍收、6 拍写（8+4+4+4+4+1）"——后者的写被拆细是因为 25 个 bank 在同一拍写会撞 LUTRAM 的写端口/布线，HLS 自己摊到了多拍。

---

## 3. 「2.76 拍/字节」的分解（并与填充 1.77 对比）

### 3.1 先更正一件事：2.76 是 **k=1** 的拟合系数

来源是成本模型文档，不是 k=3/k=5 的分相实测：

```text
docs/设计/记录_20261023_cin×cout二维网格与conv成本模型.md:93   ⇒ c_zpad = 1.82 拍/字节，c_wgt = 2.76 拍/字节
同文件 :123  | 权重重装（k=1） | 2.76 | 拍/权重字节 | W 阶梯 + cout=8/64 差分（±15%） |
同文件 :20   | cin·cout 交叉项 | 0.211 拍/(cin·cout) ... 其中计算 0.1679、权重重装 2.76/64 |
```
`2.76/64` 的 64 是**列块宽度**：每个 `(y, x0块, ocb)` 重装 `cin·8` 字节，摊到 64 个输出像素上 ⇒ 每像素 `cin·cout/64` 字节。这反证了 §4 的"重装粒度 = (行, 列块, OC 块)"。

⚠️ **k=3 / k=5 没有独立的 c_wgt 实测值**（模型的 k=3 误差 −6.23%，k=5 −0.04%，是拟合残差不是分相测量）。把 2.76 当成"任意 k 的权重重装单价"用，**存疑**。

### 3.2 分解（k=1 路径，RTL 可核对的部分）

| 项 | 拍/字节 | 依据 |
|---|---:|---|
| 写侧下界 | **1.00** | `wgt1_buf` dim2 未拆 ⇒ 每个 bank 只有 **1 个写口**；装载循环 `II=1` 且一拍只写 1 字节（`Pipeline_..._475_16` 报告 `Iteration Latency 14 / II achieved 1 / II target 1`） |
| 读侧等数据摊派 | **+1.76** | 实测/拟合 2.76 − 1.00 |
| 合计 | **2.76** | 记录 :93（置信区间 2.4–3.1） |

读侧为什么不是 0：装载用"循环携带寄存器"写法——**每 4 个 `ic` 才发一次 32-bit 读**，其余 3 拍从 `hold1` 里切字节：

```cpp
cpp:474  if ((cin & 3) == 0) {
cpp:479      for (int ic = 0; ic < cin; ++ic) {
cpp:480          if ((ic & 3) == 0) hold1 = pw1[ic >> 2];      // ← 4 拍一次读
cpp:483          wgt1_buf[i][ic] = (data_t)(hold1 >> ((ic & 3) << 3));  // ← 4 拍四次写
```
折算：**2.76 拍/字节 × 4 字节 = 11.0 拍/32-bit 字**；而消费者的需要是 **1 字 / 4 拍**（因为写侧 1 拍 1 字节）⇒ 实际只有 36% 的读效率。

### 3.3 为什么填充是 1.77 而权重是 2.76

| | 填充（C8 宽列块） | 权重重装（k=1） |
|---|---|---|
| 循环形态 | `for t < nwide`，`II=1`，每 4 拍取 1 个 `w32`（cpp:364-370） | `for ic < cin`，`II=1`，每 4 拍取 1 个 `w32`（cpp:479-485） |
| 写侧下界 | 1 字节/拍（`line_buf` dim2 单写口，cpp:352-357 明确"必须保持每拍 1 字节"） | 1 字节/拍（同左） |
| **一笔 burst 覆盖** | `nwide = 8·lb` = **512 字节级连续** | `cin` = **132 字节级**，且**每 oc 重开一笔**（AR 在 oc 循环里，`v:9692`） |
| 实测 | **1.77**（记录 :35、:274） | **2.76** |
| 折算每 4 字节 | 7.1 拍 | 11.0 拍 |

同一套"1 写/拍 + 每 4 拍一读"的骨架，差别只在 **burst 覆盖率**：填充一笔 burst 摊到 512 字节，权重一笔摊到 132 字节，而且每个 oc 的 AR 要等上一 oc 的 beat 全部消费完才发（`state 86 → 77 → 78` 是同一条路径），**没有任何跨 oc 的预取重叠**。

⇒ **结论**：差额 1.76 拍/字节 = "每 oc 一次短 burst 的开销（含未重叠的 AR→首拍往返）+ 每 4 字节一次的读依赖"摊到 132 字节上。
⇒ **可动的杠杆**：让 AR 跨 oc 预取（双 bank ping-pong），2.76 可以压到接近 1.0–1.2。
⚠️ 这条差额里"往返 / 串行依赖"各占多少，静态分析无法定论（需要 cosim 波形或 ILA 计数）——**存疑**。

---

## 4. 重装机制与全驻留可行性

### 4.1 什么时候重装（RTL 证实：`ocb` 在 `y` 里面）

大括号深度扫描（`awk` 累加 `{}`）：

```text
cpp:279  for (int y = 0; ...)                     depth(after)=3
cpp:436      for (int ocb = 0; ...)               depth(after)=4     ← 在 y 循环体内
cpp:475/517/566      for (i…) / for (ic…)         depth=6..7
```
FSM 也逐字对上：`state 72` 是 ocb 循环判（`icmp_ln436`）——**不满足就回 `state 40`（y 层）**，满足就 `state 73→74→75→76→77` 进装载：

```text
v:15632-15638  ap_ST_fsm_state72 : icmp_ln436==1 → ap_ST_fsm_state40  (y 层)
                                  else           → ap_ST_fsm_state73  (本行下一个 ocb)
v:16102-16106  ap_ST_fsm_state166 → ap_ST_fsm_state72                  (一个 ocb 干完)
v:15654        ap_ST_fsm_state76 → ap_ST_fsm_state77                   (进装载)
```
⇒ **每个 (输出行 y, 列块 x0, OC 块 ocb) 都要把当前 8 个 oc 的权重从 DDR 重搬一次**。

### 4.2 为什么必须重装

- 缓冲只装 **8 个 oc**（k=1 是 16 = 8 oc × 2 车道）× `cin ≤ 352`；而 `cout` 最大 192 = **24 个 oc 块**（`wgt1_buf` 深度 352 就是 `CIN_MAX`，不是 cout）。
- `wgt3_buf` 同理：72 bank 全部被 `(oc, tap)` 占满，`ic` 只能当地址。
- 再加上"列块"：`x0` 循环在 `y` 外面（cpp:268），所以一个 `(y, x0)` 块**只算 64 个像素**就要把 8 个 oc 的权重全搬一遍。

### 4.3 重装量（全网 247 层，逐层算）

单层重装字节 = `cin · cout · Hout · ceil(Wout/64) · 容器膨胀`（k=1 膨胀 1，k=3 膨胀 **12/9=1.333**，k=5 膨胀 **28/25=1.12**）。
（用 `runs/prof20261023_grid/layers_model_final.json` 的 247 层参数逐层算，层 0 与文件里的 `g.wgt` 字段逐位相同 = 25,804,800 字节，交叉验证通过。）

| k | 层数 | Σ cin·cout | 重装字节 | 占重装量 | 该组实测总时间 |
|---|---:|---:|---:|---:|---:|
| 1 | 132 | 800,256 | 237.2 MB | 28.6% | 27.49 s (62.2%) |
| 3 | 114 | 81,152 | 567.3 MB | 68.3% | 13.33 s (30.2%) |
| 5 | 1 | 576 | 25.8 MB | 3.1% | 3.39 s (7.7%) |
| **Σ** | **247** | — | **830.4 MB** | 100% | **44.21 s** |

按 2.76 拍/字节折算：**2.29 G 拍 = 15.3 s @150 MHz ≈ 全网 44.2 s 的 34.6%**。
权重本体（逻辑字节）全网络只有 **1.545 MB**（打包容器后 1.790 MB）⇒ 重读倍数：**容器口径 830/1.790 ≈ 464 次**，**逻辑口径 686 MB/1.545 MB ≈ 444 次**。
k=3 之所以最贵：它的 `cin·cout` 只有 k=1 的 1/10，但容器膨胀 1.33 且层数多、画幅大。

### 4.4 能否全部驻留？

| 口径 | 需要 | BRAM18K | 结论 |
|---|---:|---:|---|
| 全网络常驻（打包容器 1.790 MB） | 14.32 Mbit | **777** | **不可行**（设计已用 163，器件量级差 4 倍） |
| 单层常驻，最坏 k=1（67,584 B） | 540,672 bit | **29.3** | 可行（+30 块） |
| 单层常驻，最坏 k=3（221,184 B 容器） | 1.77 Mbit | **96.0** | **不可行**（设计已 163 块 ≈90% 占用，再 +96 必爆；即便先删掉 wgt_buf 的 50 也只够一半） |
| 单层常驻，k=5（最坏 16,128 B） | 129,024 bit | 7.0 | 可行 |

- 最大 `cin·cout` = **67,584**：`encoder.encoder_3.sam_blocks.0.basic_block.conv_proj`（k=1, cin=352, cout=192）。
- 最大 `cin·cout·k²` = **165,888**，在另一层：最坏 k=3 层 `encoder.encoder_2.down`（cin=96, cout=192, 96·192·9）。
- 全网络 `Σcin·cout` = 881,984；`Σcin·cout·k²` = 1.545 MB。

**结论**：不要走"常驻"。真正的解法是 **ping-pong 双 bank + 计算期预取**：
重装 830 MB 摊到计算时间 22.7 s 上只需要 **≈37 MB/s** 的 DDR 带宽（峰值能力的个位数百分比），完全可以藏掉；而当前是**串行**的（`state 77-110` 装载，然后 `state 134-166` 计算），所以 1 拍都没藏住，白付 15.3 s。

---

## 5. PAD12 / PAD28 在 RTL 里的体现

### 5.1 容器定义（与 cpp 注释逐条对应）

| 容器 | 每 (oc,ic) 字节 | 32-bit 字数 | 抽头布局 | 起址对齐 |
|---|---:|---:|---|---|
| 无容器（k=1） | 1 | — | 1 个抽头 | 任意 |
| **PAD12**（k=3） | **12** | **3** | 抽头 `k=0..8` 落在第 `k>>2` 字的第 `k&3` 字节；字节 9..11 = 0 | `12·n ≡ 0 (mod 4)`；`≡0 (mod 8)` 仅当 n 偶 |
| **PAD28**（k=5） | **28** | **7** | 抽头 `k=0..24` 落在第 `k>>2` 字第 `k&3` 字节；字节 25..27 = 0 | `28·n ≡ 0 (mod 4)`；`≡0 (mod 8)` 仅当 n 偶 |

cpp 出处：`cpp:497-531`（PAD12 装载）、`cpp:552-584`（PAD28 装载）、`cpp:466-472`（对齐前提的论证）。

### 5.2 宽读几次、怎么落到 9/25 个 bank

- **k=3：每 (oc,ic) 读 3 次 32-bit**（3 拍），9 个抽头分两批落地（以 oc0 为例，其余 7 个 oc 同形）：
  - 前两个字（8 字节）⇒ `state 89` 一拍写 **8 个 bank**（oc0 的 bank0..7；全 8 个 oc 合计 64 个 bank 都在这一拍）。证据：v:14463–14475 是 bank0 的 `ce1/we1`、v:14446 是 `wgt3_buf_9_we1_local`，条件都是 `state89 & gmem0_0_RVALID`；
  - 第 3 个字只用第 0 字节 ⇒ `state 90` 写每个 oc 的**第 9 个抽头 bank**（oc0 = bank8，oc1 = bank17 … oc7 = bank71，共 8 个）。证据：`v:14422`、`ap_predicate_pred6662_state90`；`grep -B3 'wgt3_buf_8_we1_local' ` 得 `state90`，而 `wgt3_buf_17/26/35/44/53/62/71` 也都是 `pred_*_state90`。
  - "64 + 8 分两拍写"正是 12 字节容器（不是 9 字节）造成的：第 3 个字里 3 个字节是零，白搬。
- **k=5：每 (oc,ic) 读 7 次 32-bit**（7 拍，`state 120..126` 逐字锁进 25 个字节寄存器），写使能在 `state 122..127` 分 6 拍放：**122 拍写 8 个 bank（抽头 0–7，两个字）+ 123/124/125/126 各写 4 个 bank（抽头 8–23）+ 127 写最后 1 个（抽头 24）**。3 个零字节白搬。
  （逐 bank 实测：`grep -B2 "wgt_buf_[0-9]*_we0_local = 1'b1;" conv2d_engine.v` → state122: bank 0–7；state123: 8–11；state124: 12–15；state125: 16–19；state126: 20–23；state127: 24。）
- 每 oc 一笔 AR，长度 `3·cin`（k=3）/ `7·cin`（k=5）个字 —— 见 §2.2。

### 5.3 为什么 8 字节对齐会毁 burst

三个独立原因，缺一不可：

1. **容器只保证 4 字节对齐**：`12k mod 8` 与 `28k mod 8` 在 k 为奇数时都等于 **4** ⇒ 一半 `(oc,ic)` 的起点是 4 mod 8。用 `ap_uint<64>*` 读，HLS 产生的 64-bit beat 有一半会**跨元素**（前 4 字节属于上一个 (oc,ic)，后 4 字节属于下一个），要么插运行期对齐检查 + 回退到窄访问，要么切 burst。
2. **端口宽度是"整条 bundle 取最大"**：`wgt` 与 `in_fm`/`bias` 共用 `bundle=gmem0`（`cpp:91-96`）。把权重指针声明成 64-bit ⇒ **整条 gmem0 变成 64-bit 端口**，输入填充那些按 `w32_t` 写的连续读（`cpp:361-370`）跟着变宽，行块的 4 字节粒度被打断。
3. 实测代价：**AR 笔数 201 → 15,416**，净亏 **27%**（本轮的已知事实）。AR 笔数暴涨正是"对齐检查回退 / burst 被切成 1–2 拍"的表征。

⇒ 想加宽只能**整层同时改容器**（例如 PAD12 → PAD16、PAD28 → PAD32，让 stride 恒 8 对齐），代价是权重体积再涨 16/12 = 1.33 倍、28/32 = 1.14 倍；或者**给权重单独一条 bundle**，但那会多一个 AXI master 端口。

---

## 6. `wgt_buf`（k=5）的浪费与可省性

### 6.1 确认：只有 k=5 那条路径用它

```text
v:3649..3996                wgt_buf 25 个实例，只在 k=5 分支被写（cpp:551-601）
Pipeline_..._LOOP_849_43.v   wgt_buf_*_address0（1 个 bank，收尾展开）
Pipeline_..._LOOP_865_44.v   wgt_buf_*_address0（第 1 行/ky=0 的行扫描）
Pipeline_..._LOOP_892_47.v   wgt_buf_*_address0（25 个 bank，主循环）
```
k=1 走 `LOOP_684_33`（16 个 `wgt1_buf`），k=3 走 `LOOP_764_38`（72 个 `wgt3_buf`），两者都**完全不碰** `wgt_buf`。
网络里 k 分布（247 层）：**k=1 132 层 / k=3 114 层 / k=5 1 层**——唯一那层是 `encoder.conv_first`（320×320, cin=12, cout=48, k=5, s=1）。

### 6.2 它到底有多贵

| 指标 | 数值 |
|---|---|
| 该层权重（逻辑 25 抽头） | 576 × 25 = **14.4 KB** |
| 该层权重（PAD28 容器） | 576 × 28 = **16.1 KB** = 全网打包权重 1.790 MB 的 **0.90%** |
| `wgt_buf` 占用 | 25 × 2816 × 8 = 563,200 bit = 68.8 KB = **50 BRAM18K** = 设计 163 块的 **30.7%** |
| 产能冗余 | 68.8 KB 的 buffer 服务 16.1 KB 的权重 ⇒ **4.3× 过配**；深度按 `CIN_MAX=352` 定，而这层 `cin=12` ⇒ 深度**过配 29×** |
| 该层重装时间 | 25.8 MB × 2.76 拍/字节 = 71.2 M 拍 = **0.475 s**（该层 3.39 s 的 14%，全网的 1.1%）<br>⚠️ 2.76 是 k=1 的单价，k=5 没有独立实测 ⇒ 这里是**借用估算**，存疑 |

### 6.3 改成 DDR 流式读的代价（这就是不能这么干的原因）

| 方案 | 每层搬运量 | 拍数 @150 MHz | 时间 |
|---|---:|---:|---:|
| A. 当前（每 (y,x0,ocb) 重装进 bank） | 25.8 MB | 71.2 M | **0.475 s** |
| B. 计算期按像素流式读（每像素 12·48·25 = 14,400 B × 102,400 像素） | **1.47 GB** | 4.07 G | **27.1 s** ✗ |
| C. 按 (y,ocb) 流式（等价现状但去掉 bank） | 25.8 MB | 71.2 M | 0.475 s（无收益） |
| **D. 缩深度常驻（推荐）** | 首次 16.1 KB，之后 0 | ≈ 44 k（一次） | **≈0.0003 s** ✓ |

**推荐 D 的两种落地**：
- **D1（最小改动）**：把 `wgt_buf` 的深度从 `CIN_MAX` 改成 k=5 的实际上界（k=5 只有这 1 层，`cin=12` ⇒ 深度 `8×12=96` 就够；留余量取 `8×32=256`）。`25 × 256 × 8 = 51.2 kbit`（对比现在 563 kbit，**缩 11×**）：落 LUTRAM 约 **800 LUT**（每 bank 256×8 分布式 RAM ≈ 32 LUT）⇒ **省下全部 50 块 BRAM18K**；若坚持用 BRAM，25 个独立小 RAM 各占 1 块 = 25 块，仍省 25 块。
- **D2（彻底）**：k=5 也归到一条"25 抽头 × 8 oc"的 LUTRAM bank（与 `wgt3_buf` 同构：`[8][cin][25]`，`complete dim=1, dim=3` ⇒ 200 个 bank，深度 cin=12 ⇒ 200×12×8 = 19.2 kbit）。

⚠️ 注意：**"删掉 wgt_buf、k=5 改为每次计算时从 DDR 读权重"是绝对不行的**（方案 B = 27 s/层）。可省的是**容量**，不是**片内驻留**这件事本身。

---

## 7. 手写 `weight_loader` / `weight_bank` 的规格

### 7.1 设计目标（由上面的结论直接推出来）

1. bank 号必须**综合期常量**（Verilog 里 = `generate` 的 genvar）。
2. bank 地址可以是运行期（`ic`，以及"层内 oc 块号"折进高地址位）。
3. **必须有 ping-pong 双 bank + 跨 oc 预取**——这是把 15.3 s 藏掉的唯一途径（§4.4）。
4. 装载侧和计算侧**各用各的 bank**（ping 用于计算时，pong 在装），用一个 `bank_sel` 切换读口。
5. 每拍 1 字节/bank 的写口不变（LUTRAM 单写口），但**每拍可并行写 9/25 个 bank**（这正是现在的 k=3 已经在做的）。

### 7.2 端口清单

```verilog
module weight_loader #(
  parameter NB_MAX = 72,      // 最大 bank 数（k=3: 8 oc × 9 tap）
  parameter AW     = 9        // 每 bank 深度位宽（cin ≤ 352 时 9 bit）
)(
  input  wire         clk,
  input  wire         rst_n,

  // ---- 层配置（每层一次，来自 control_s_axi）----
  input  wire         start,          // 脉冲：开始装本层权重
  input  wire [1:0]   ksel,           // 0=k1, 1=k3, 2=k5
  input  wire [15:0]  cin,            // 1..352
  input  wire [15:0]  cout,           // 1..192
  input  wire [63:0]  wgt_base,       // DDR 基址（PS 侧按容器布局打包）

  // ---- 与填充共用的 AXI 读通道（外部 2:1 arbiter）----
  output wire         m_arvalid,
  output wire [63:0]  m_araddr,
  output wire [7:0]   m_arlen,        // 拍数-1，单笔 ≤256
  input  wire         m_arready,
  input  wire         m_rvalid,
  input  wire [31:0]  m_rdata,
  output wire         m_rready,

  // ---- bank 写口（打包成向量；每个 bank 独立地址/使能）----
  output reg  [NB_MAX*AW-1:0] bank_waddr,
  output reg  [NB_MAX-1:0]    bank_we,
  output reg  [NB_MAX*8-1:0]  bank_wdata,
  output reg  [1:0]           bank_sel,   // ping/pong
  output reg                  bank_valid,// 本组 bank 已装好，可切给计算

  // ---- 状态 ----
  output reg          busy,
  output reg          done
);
```
计算侧端口（`weight_bank`）：

```verilog
module weight_bank #(parameter NB=16, parameter AW=9)(
  input  wire             clk,
  input  wire [NB-1:0]    we,          // 写使能（每 bank 1 bit，常量绑定）
  input  wire [NB*AW-1:0] waddr,       // 全部 = ic（同一根）
  input  wire [NB*8-1:0]  wdata,
  input  wire [NB*AW-1:0] raddr,       // 计算侧：全部 = ic
  output wire [NB*8-1:0]  rdata        // 计算侧：NB 字节/拍，直接进 NB 个乘法器
);
```

### 7.3 「bank 常量化」的 Verilog 写法（关键）

**唯一正确的写法 = `generate` + genvar 索引的独立数组**（综合器看到的是 NB 个独立 RAM，bank 号在 elaborate 期定死）：

```verilog
  genvar g;
  generate
    for (g = 0; g < NB; g = g + 1) begin : lane
      (* ram_style = "distributed" *) reg [7:0] mem [0:(1<<AW)-1];

      always @(posedge clk) begin
        if (we[g]) mem[waddr[g*AW +: AW]] <= wdata[g*8 +: 8];
        rdata[g*8 +: 8] <= mem[raddr[g*AW +: AW]];   // read-first，同址写先读旧值
      end
    end
  endgenerate
```
要点：
- `lane[g].mem` 是 **NB 个不同的数组实例**；`g` 是 genvar（elaboration 常量），所以"bank 号"在硬件里是**硬连线**的，等价于 HLS 的 `ARRAY_PARTITION complete`。
- **绝对不要**写成 `reg [7:0] mem [0:NB*DEPTH-1];` 然后用运行期 `bank` 去拼地址 —— 那正是 cpp:184-196 记录的那次否决：9 抽头挤 1 bank ⇒ 9 读口 ⇒ LUT 82%→91%；或者 bank 变运行期 ⇒ 无流水、cosim +38%。
- 要在别处"点名叫某一个 bank"，只能用**编译期常量下标**：`generate` 里的 genvar、`localparam`、或层次化实例名。例如 k=3 的 72 个 bank：
```verilog
  genvar g;
  generate
    for (g = 0; g < 72; g = g + 1) begin : bnk          // g 是 genvar ⇒ 常量
      wire [7:0] q;
      bank_ram #(.AW(9)) u (.clk(clk), .we(we[g]), .waddr(ic),
                            .d(din[g]), .raddr(ic), .q(q));   // 层次名 bnk[13].u 就是"bank13"
      assign q_all[g*8 +: 8] = q;
    end
  endgenerate
  // oc1 的第 0 个抽头 = bank 9，消费侧只写常量切片，不写运行期下标
  assign tap_oc1_p0 = q_all[9*8 +: 8];
```
  反例（HLS 已否决的写法）：`mem[bank*DEPTH + ic]`，`bank` 是运行期信号 —— 综合出来是"一个深 RAM + 地址加法"，bank 之间的并行读口全部消失，或者被工具复制成一堆 mux 把 LUT 顶爆。
- 若必须"一个模块多组 bank"（ping/pong），把 `bank_sel` 做成**两组独立数组 + 输出二选一**，不要做成"一个数组两倍深度 + 高位地址"——后者读口会变成一整个 2× 深度的 RAM，MUX 反而不贵，但会丢掉"两组同时读写"的能力。

### 7.4 装载 FSM（k=3 为模板，k=1/k=5 只是 beats_per_ic 不同）

```
beats_per_ic:  k1 = 1 (每 ic 1 字节, 4 ic/word)  k3 = 3  k5 = 7
banks_per_ic:  k1 = 1                           k3 = 9  k5 = 25

IDLE:
  if (start) → NEXT_OC
NEXT_OC:                                  // oc = 0..ceil(cout/8)-1，**预取 2 个 oc**
  if (oc == nblk) → DRAIN
  else { 发 AR(base + oc*cin*BYTES_PER_IC, len = BEATS*cin-1);
         if (oc+1 还有) 立刻再发下一笔 AR   // ← 关键：跨 oc 预取，现在的 RTL 缺这一步
         → RECV }
RECV:                                     // 每拍消费 1 beat，写 banks_per_ic 个 bank
  beat = m_rdata;  bank_waddr = ic; bank_we = 本拍对应的 bank 掩码;
  ic++；每 BEATS 拍 ic 跨一格，直到 ic == cin → 等该 oc 最后一拍 → NEXT_OC
DRAIN:  bank_valid <= 1; bank_sel <= ~bank_sel;   // 切给计算
```
- **AR 必须提前发**：现状 `state 86 → 77 → 78` 是"上一 oc 收完才发下一 oc 的 AR"，一笔 132 字节的 burst 完全没有后继重叠，这就是 §3 那 1.76 拍/字节的主要来源（推断）。
- 期望收益：装载从 **2.76 → ~1.05 拍/字节**（写口下界 + 少量摊派），再叠加 ping-pong 隐藏，全网可省 **≈15 s / 44.2 s（34%）**。

---

## 8. 存疑清单

1. **2.76 只对 k=1 成立**（成本模型拟合值，±15%，2.4–3.1）。k=3/k=5 的权重重装单价**没有独立实测**；k=3 的容器膨胀 1.33 会不会让它更贵，静态分析答不了。
2. **2.76 的微观归因**：本文拆成"1.0 写口下界 + 1.76 短 burst 摊派"，其中"往返 vs 每 4 字节读依赖"的占比**未经波形验证**。
3. **BRAM 51 vs 50**：csynth 口径 = 50；若引用到 51，应是 impl 报告或含 `bias_buf`。
4. **k=3 装载的 beats/ic 是否严格 3**：从状态与 bank 写使能反推（`state 87/88/89` 三拍 + `state 90` 写第 9 个 bank）是自洽的，但未用波形确认"3 拍里每拍都是有效 beat"。
5. **列块宽度的口径**（已核，不算存疑，留档）：`K1_WIDE_COLS` 在本构建里被 `#undef` 掉了（`conv2d_engine.h:107-115`：`C8_LAYOUT=1` 时强制 `K1_WIDE_COLS 0`），所以 `bw = 64`（s=1）/ `32`（s=2），**不会**出现 cpp:168 那条 `SPAN_MAX*64 = 448` 的宽列块。本文 `ceil(Wout/64)`（s=2 层用 `ceil(Wout/32)`）逐层重算后仍是 **830.4 MB**，与 `/64` 口径一致。若哪天重新打开 `K1_WIDE_COLS=1`，k=1 的重装量会掉到约 1/7（237 MB → ~34 MB），全网 ≈ 627 MB ⇒ 11.5 s。

---

## 9. 证据索引（可复现）

```bash
V=/data/work/hls_c8_csynth/r9_k1_l16/solution1/syn/verilog
R=/data/work/hls_c8_csynth/r9_k1_l16/solution1/syn/report

# 三种缓冲的 bank 数 / 深度 / 位宽 / 实现
grep -n 'wgt1_buf\|wgt3_buf\|wgt_buf\|wbuf' $R/conv2d_engine_csynth.rpt | head -140
sed -n '155,300p' $R/conv2d_engine_csynth.rpt          # 内存表 + Total(163 BRAM18K)

# 实例化（25 / 16 / 72）
grep -n 'conv2d_engine_wgt_buf_RAM_AUTO_1R1W #('   $V/conv2d_engine.v | head -30
grep -n 'conv2d_engine_wgt1_buf_RAM_2P_LUTRAM_1R1W #(' $V/conv2d_engine.v | head -20

# 铁律：bank 常量 / 地址只有 ic
grep -n 'wgt3_buf_9_address0\|wgt3_buf_13_address0' $V/*Pipeline_LOOP_764_38.v
grep -n 'add_ln902_fu_1198_p2' $V/*Pipeline_LOOP_892_47.v          # wgt_buf 地址 = oc*352+ic
sed -n '17452p;17534p;17538p;17544p' $V/conv2d_engine.v

# 装载 FSM 逐拍
sed -n '15648,15730p' $V/conv2d_engine.v     # state 76..91
sed -n '14440,14490p' $V/conv2d_engine.v     # state 89/90 写使能
sed -n '16632,16634p;16910,16912p;17070,17072p' $V/conv2d_engine.v   # ARLEN = 3·cin / 7·cin

# AXI 参数
sed -n '318p' $V/conv2d_engine.v
sed -n '7505,7528p' $V/conv2d_engine.v
grep -n 'RBUFF_DEPTH\|fifo_rreq\|buff_rdata' $V/conv2d_engine_gmem0_m_axi.v

# 成本模型出处
grep -n '2.76\|1.77' docs/设计/记录_20261023_cin×cout二维网格与conv成本模型.md
```
