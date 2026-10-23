# 02 · 行填充（DDR → line_buf）与 line_buf 的 RTL 静态分析

对象：`/data/work/hls_c8_csynth/r9_k1_l16/solution1/syn/verilog/conv2d_engine.v`（17,588 行）
构建：`-DC8_LAYOUT=1 -DUSE_HLS_TYPES`，Vitis HLS 2025.2.1，xc7z015，150 MHz（6.67 ns）
C++ 源头：`pynq_specialized_code/pl/src/conv2d_engine.cpp`（下述行号均指该文件当前版本）

> 本文只做静态读 RTL/报告，**没有**编译/仿真/上板。所有"实测"数字都标明出处（来自
> `docs/设计/记录_20261023_cin×cout二维网格与conv成本模型.md`）。
> 不确定处标「存疑」。

---

## 0. 六条结论速览

| 问题 | 结论 | 关键证据 |
|---|---|---|
| 数据通路 | `gmem0`(AXI4, 32-bit) → **RBUFF 4096×32 bit (16 KB)** → `R` → **32-bit 循环携带寄存器 `hold`** → 字节抽取 → line_buf 写口（8-bit/拍，II=1） | `gmem0_m_axi.v:334`；`LOOP_364_13.v:233,751,829` |
| 地址 | line_buf 地址 = **`c·76 + j`**（每 bank），bank = **r**；DDR 地址 = `in_fm + 8·(need0·W+gc0+la) + (cinb/8)·H·W·8`，ARLEN = `2·lb−1`（4B beat） | `LOOP_364_13.v:689,815,645`；`conv2d_engine.v:16204,17050,17500,16880` |
| 行轮转 | **没有物理轮转**：7 个 bank 固定对应"窗口内第 r 行"，y 前进时把 `bank r+stride → bank r` **搬一遍**（`LOOP_286_6`），新行写进 bank ≥ keep | `LOOP_286_6.v:299,457-470`；cpp:284-291 |
| bank 号 | **结构上 7 个硬 bank，功能上是运行期变量**：写=3-bit one-hot 译码，读=7:1 mux。不是常量 | `LOOP_364_13.v:821,523-624`；`LOOP_764_38.v:1308-1490` |
| 补零 | 左=`LOOP_315_10`，右=`LOOP_319_11`，上/下=`row_ok==0` 走 `LOOP_372_14`（同地址写 0）。**有写了永不读的地址**：通道补齐区恒浪费；k=1 的 `[lb,bwcol)` 列恒浪费 | `conv2d_engine.v:9694,9732,7921`；cpp:309-322 |
| 1.77 | `1.0`(写口) + 每 burst 固定 `15` 拍 + 暴露的 DDR 读延迟 + 每 `(r,ic)` 空转的 pad pass + 通道补齐 + 尾块右补零。**拆不出唯一归因**（1.769 是回归斜率），见 §5 | 见 §5 表 |

---

## 1. 填充数据通路

```
                 in_fm (DDR, C8 交错布局)
                        │
  ┌─────────────────────┴──────────────────────────────────────────────────────┐
  │ AR: ADDR = in_fm + 8·(need0·W+gc0+la) + (cinb/8)·H·W·8  (USER 侧按 32-bit 字) │
  │     LEN  = 2·lb − 1  (32-bit 字) = nwide/4 − 1 ,  nwide = 8·lb               │
  │     32-bit 数据宽 / MAX_READ_BURST_LENGTH=256 / 16 outstanding               │
  └─────────────────────┬───────────────────────────────────────────────────────┘
                        ▼
        ┌───────────────────────────────────────────┐
        │ gmem0_m_axi core                          │
        │  RBUFF_DEPTH = 16 × 256 = 4096 × 32 bit   │  ← 16 KB，另有 7 深请求队列
        │  I_CH0_RFIFONUM[12:0] (13 bit 计数)       │
        └───────────────────┬───────────────────────┘
                            │ R: RDATA[31:0] / RVALID / RREADY
                            ▼
  ┌──────────────── Pipeline_VITIS_LOOP_364_13 (II=1, 5 级, DSP×1) ───────────────┐
  │ t = 0 … nwide−1                                                              │
  │                                                                              │
  │  (t[1:0]==0) ──► hold[31:0] <= RDATA      ← 32-bit 循环携带寄存器（1 字/4 拍）│
  │  byte = hold >> (t[1:0]·8)  → d1[7:0]     ← 移位抽取，纯组合                  │
  │                                                                              │
  │  bank = trunc_ln304 = r[2:0]              ← 运行期 3-bit                      │
  │  addr = add_ln368_1 = c·76 + la + (t>>3)  ← c = (cinb/8)·8 + (t&7)            │
  └───────────────────────────┬──────────────────────────────────────────────────┘
                              │ 7 × { address1[14:0], ce1, we1, d1[7:0] }（ce 全开，we 一热）
                              ▼
   ┌──────────┬──────────┬──────────┬──────────┬──────────┬──────────┬──────────┐
   │line_buf_0│line_buf_1│line_buf_2│line_buf_3│line_buf_4│line_buf_5│line_buf_6│
   │26752×8b  │   …      │   …      │   …      │   …      │   …      │   …      │
   │16 BRAM18K│          │          │          │          │          │          │
   └──────────┴──────────┴──────────┴──────────┴──────────┴──────────┴──────────┘
        (共 7×16 = 112 BRAM18K；每 bank 端口 = 1 读(address0/q0) + 1 读或写(address1/ce1/we1/q1))
```

### 1.1 各级位宽/深度（都可在 RTL 里点到）

| 级 | 位宽/深度 | 位置 |
|---|---|---|
| AXI 数据 | 32 bit（`C_M_AXI_GMEM0_DATA_WIDTH`，端口 `m_axi_gmem0_RDATA[31:0]`） | `conv2d_engine.v:7513`；`gmem0_m_axi.v:13` |
| 读突发上限 | `MAX_READ_BURST_LENGTH=256` beat（用户侧 `RD_BURST_MAX=256`） | `conv2d_engine.v:7509`；`conv2d_engine.h:120` |
| 在途读 | `CH0_NUM_READ_OUTSTANDING=16`（但父 FSM **同一时刻只发一笔** AR，见 §5） | `conv2d_engine.v:7527` |
| 读缓冲 FIFO | `RBUFF_DEPTH = 16×256 = 4096` 项 × 32 bit = 16 KB；计数 `RFIFONUM[12:0]` | `gmem0_m_axi.v:334`；`conv2d_engine.v:7528` |
| 字→字节寄存器 | `hold_fu_120[31:0]`（循环携带），每 4 拍装一次 | `LOOP_364_13.v:233,751` |
| 写数据 | `trunc_ln369_reg_486[7:0]`（`lshr` 结果取低 8 位） | `LOOP_364_13.v:829` |
| line_buf | 每 bank `26752×8 bit`；`AddressRange=26752 = CIN_MAX(352) × BWCOL_MAX(76)` | `line_buf_RAM_2P_BRAM_1R1W.v:15-17,30`；csynth `conv2d_engine_csynth.rpt:160-166` |
| line_buf 地址 | 15 bit（26752 < 32768） | `conv2d_engine.v:1450` |
| 每 bank BRAM | 16 × BRAM18K（7 bank = 112；全设计 175） | `csynth.rpt:160-166` |

**要点**：DDR 侧每拍搬 4 字节，但 line_buf 侧**每拍只写 1 字节** —— 4 拍消耗 1 个 beat。
这不是 RTL 没优化，而是 cpp:342-359 的刻意取舍（拆通道 bank 会让 LUT +5.6k）。
结果就是"写口 1 字节/拍"是硬下界，`1.0 拍/字节`。

---

## 2. 地址公式还原

### 2.1 line_buf 侧（每 bank 内的字节地址）

C++：`line_buf[c][r][la + (t>>3)]`，数组 `[CIN_MAX][SPAN_MAX][BWCOL_MAX]`，**dim2 完全拆分**
（`conv2d_engine.cpp:216`），所以每个 bank 的地址空间是 `[CIN_MAX][BWCOL_MAX]`：

```
addr_bank(c, j) = c · 76 + j          76 = BWCOL_MAX（编译期常量）
```

RTL 逐项对上：

| 数学项 | RTL | 行号 |
|---|---|---|
| `c = cinb + (t&7)` | `or_ln1_fu_335_p3 = {cinb[8:3], t[2:0]}`（cinb 是 8 的倍数，故 `(cinb/8)*8 + (t&7) = cinb+(t&7)`） | `LOOP_364_13.v:815,823` |
| `c · 76` | `grp_fu_428` = `mac_muladd_9ns_7ns_15s_15_4_1`：`p0 = or_ln1`，`p1 = 15'd76`，`p2 = add_ln368_reg`；**用 1 个 DSP、4 级流水**（因为 76 不是 2 的幂，HLS 选了 DSP 而不是 shift-add） | `LOOP_364_13.v:286-301,689` |
| `j = la + (t>>3)` | `trunc_ln_fu_347_p4 = {t[17:3]}`（= t>>3 = 列号），`add_ln368 = trunc + empty_95`（`empty_95 = la`） | `LOOP_364_13.v:831,645` |
| 最终地址 | `add_ln368_1_reg_497 = c·76 + (t>>3) + la` → `address1 = zext_ln368_1` | `LOOP_364_13.v:835,695` |
| 循环上界 | `icmp_ln364 = (t < empty_94)`，`empty_94 = nwide = lb<<3` | `LOOP_364_13.v:691`；`conv2d_engine.v:16880,6125` |
| 装字条件 | `icmp_ln365 = (t[1:0]==0)` | `LOOP_364_13.v:693,825` |
| 字节抽取 | `shift = {t[1:0],3'd0}`；`byte = hold >> shift` | `LOOP_364_13.v:817,751` |
| bank 号 | `tmp_reg_449 = empty`（=`trunc_ln304_reg_8760 = r_2_reg_3300[2:0]`） | `LOOP_364_13.v:821`；`conv2d_engine.v:17204,6156` |

顶层三条连接：
```
.empty_94(nwide_reg_8624)      // 8·lb
.cinb(trunc_ln324_reg_8825)    // 通道块基址（0,8,16,…）
.empty_95(trunc_ln272_reg_8580)// = la（左侧补零列数）
.empty(trunc_ln304_reg_8760)   // = r[2:0]   ← bank
```
（`conv2d_engine.v:6125-6127,6156`；`trunc_ln272_fu_5659_p1 = la_1_fu_5654_p3[14:0]`
`conv2d_engine.v:17176`）

### 2.2 DDR 侧

```
byte_addr(y,r,cinb) = in_fm
                    + 8 · ( need0·W + gc0 + la + r·W )      // io_row = io_base + r*W
                    + (cinb/8) · H·W·8                     // hw8
need0 = y·stride − pad
```

RTL：

| 数学项 | RTL | 行号 |
|---|---|---|
| `need0` | `need0_fu_5925_p2 = phi_mul48 − pad` | `conv2d_engine.v:16878` |
| `io_row·8` | `add_ln304_1 = sub_ln304 + reg_4333`，`shl_ln4 = {add_ln304_1, 3'd0}` | `conv2d_engine.v:16188,17050` |
| `+ in_fm` | `add_ln324 = $signed(sext_ln304) + $signed(in_fm_read_reg_7541)` | `conv2d_engine.v:16204` |
| `(cinb/8)·hw8` | `mul_ln324 = indvar1837 × zext_ln268_reg_8323`，其中 `zext_ln268 = hw8`；`add_ln324_1 = add_ln324 + mul_ln324` | `conv2d_engine.v:16200,17500,8540` |
| 字地址 | `sext_ln364 = add_ln324_1[63:2]`（**右移 2 位**） | `conv2d_engine.v:17140,16994` |
| 突发长度 | `p_cast151 = (nwide>0) ? add_ln279_1 : 0`；`add_ln279_1 = (2·lb) + (~(2·lb)) …` → **2·lb − 1** | `conv2d_engine.v:16626,16174,16988,17470` |
| 发 AR 的时刻 | state 61，`ARADDR=sext_ln364`，`ARLEN=p_cast151`，`ARVALID = row_ok & state61` | `conv2d_engine.v:9694,9717,9732` |

> **为什么地址是 `>>2`**：`gmem0_m_axi` 的 `BYTE_ADDR_MODE=0`，用户侧地址/长度按 **32-bit 字**
> 计，核内 `rreq_addr_byte = rreq_addr << USER_ADDR_ALIGN(=2)`、`rreq_len_byte = rreq_len << 2`
> （`conv2d_engine_gmem0_m_axi.v:330,423-428`）再转成字节地址/字节长度。
> 所以 `ARLEN = 2·lb−1` 个 4 字节 beat = `8·lb` 字节，正好一条 burst 覆盖
> 一个 `(行, 通道块)` 的整段有效数据。`nwide ≤ 8×76 = 608 B = 152 beat < 256` ⇒ **不拆 burst**
> （这正是 `RD_BURST_MAX` 从 64 提到 256 的原因，`conv2d_engine.h:119-120`）。

### 2.3 哪些是常量折叠

| 量 | 值 | 形态 |
|---|---|---|
| `BWCOL_MAX` | **76** | 常量：DSP 的 `p1`、`line_buf` 深度 26752、`phi_mul29 += 76`、`phi_mul38 += 76` |
| `CIN_MAX` / `SPAN_MAX` / `C8_CH` | 352 / 7 / 8 | 常量：决定 bank 数、`AddressRange`、`(t>>3)/(t&7)` |
| `8·lb`（nwide） | 运行期 | `lb<<3`（`conv2d_engine.v:16880`），lb 运行期 |
| `c·76` | 运行期 | **DSP 乘常量**（未折叠成移位，因 76=64+8+4 但 HLS 选了 DSP） |
| `(cinb/8)·hw8` | 运行期 | DSP 乘（`hw8` 运行期） |
| `t>>3`、`t&7`、`t&3` | 纯布线 | 位切片，零成本 |
| bank 号 | 运行期 3-bit | one-hot 译码（7 个 LUT 比较器） |

搬移/拷贝路径（`LOOP_286_6`）里 `ic·76` **被折叠成移位加**：
`sub_ln288 = (ic<<6) + (ic<<4) − (ic<<2)`（`LOOP_286_6.v:853-863`）——同一常量在不同
pipeline 里 HLS 选了不同实现（填充用 DSP、拷贝用 LUT）。

---

## 3. line_buf 的 bank 与"行轮转"

### 3.1 谁写谁读

| 相位 | 模块 | 端口 | bank |
|---|---|---|---|
| 行复用拷贝 | `Pipeline_286_6` | 读 `address0/q0`，写 `address1/we1/d1` | 读 `r+stride`，写 `r` |
| 左补零 | `Pipeline_315_10` | 写 | `r` |
| 右补零 | `Pipeline_319_11` | 写 | `r` |
| DDR 填充 | `Pipeline_364_13` | 写 | `r` |
| 上/下补零 | `Pipeline_372_14` | 写 | `r` |
| 计算（9 抽头） | `Pipeline_764_38` | **读两个口**（`q0` 和 `q1`） | `rdoff[ky]`（运行期） |
| 写回（wbuf 路径） | `865_44 / 892_47` | 只读，`we1=0` | `rdoff` |

顶层 mux 证明谁在什么状态占着写口：`line_buf_address1/we1` 的 mux 顺序是
`892(146) > 865(143) > 764(135) > 364(state71,row_ok=1) > 372(62) > 319(54) > 315(52) > 286(44)`
（`conv2d_engine.v:10673-10690,10753-10763`）。
即：填充/补零/拷贝用**写口**，计算/写回只**读**（`we1` 恒 0）。

### 3.2 "轮转"其实是"搬移"

```
y → y+1 (stride=1, span=3, keep=2)：

  改前              改后
  bank0 ← row y-1   bank0 ← row y      ← 拷贝自 bank1（r+stride）
  bank1 ← row y     bank1 ← row y+1    ← 拷贝自 bank2
  bank2 ← row y+1   bank2 ← row y+2    ← 新行，DDR 填充
```

- 影子关系：`bank r` 永远装"当前窗口第 r 行"，`need0 = y·stride − pad`（`conv2d_engine.v:16878`）。
- 拷贝：`line_buf[ic][r][j] = line_buf[ic][r+stride][j]`（cpp:288）
  - 读 bank = **`add_ln288` 输入**，直接做 7:1 mux 的 `sel`：`sparsemux_15_3_8_1_1 ... .sel(add_ln288)`（`LOOP_286_6.v:290-299`）
  - 写 bank = 7 个 one-hot 谓词：`ap_predicate_pred278_state8 <= (r_read_reg_458 == 31'd0)` … `==31'd6`（`LOOP_286_6.v:457-470`）
  - 读地址 = `ic·76 + j`（`LOOP_286_6.v:737,867`），写地址同式（`LOOP_286_6.v:837`）
  - II=1，迭代延迟 7（`..._286_6_csynth.rpt` Loop 表）
- 新行：`r_begin = (row_buf_valid && keep>0) ? keep : 0`，只对 `r ∈ [r_begin, span)` 走 DDR
  （cpp:293、304）。RTL 侧 `row_buf_valid` 就是 `and_ln284_reg_8699`，state 43 用它决定是否进拷贝
  （`conv2d_engine.v` state43 分支 `icmp_ln285 || and_ln284==0 → state45`）。
- **第一行**（`row_buf_valid=0`）：`r_begin=0`，7 行全从 DDR 装。

### 3.3 bank 号是常量还是运行期？——**结构常量、功能运行期**

RTL 证据（三处独立）：

1. **填充写**：`tmp_reg_449 = empty`（3-bit 输入）→ `line_buf_k_we1 = (tmp_reg_449 == k)`；
   注意 bank0 的条件是 `(tmp==7) | (tmp==0)` —— 7 是 3-bit 全 1 的 don't-care 被并到 bank0
   （`LOOP_364_13.v:821,523-528,602-624`）。
2. **拷贝读写**：见 3.2，`r_read_reg_458 == 0..6` 的 7 个谓词 + `add_ln288` 做 mux sel。
3. **补零写**：`tmp_reg_289/296 = empty`（`LOOP_315_10.v:460`、`LOOP_319_11.v:471`、`LOOP_372_14.v:482`）。
4. **计算读**：`sparsemux_15_3_8_1_1`（7 选 1）的 `sel` 是 `empty_89/90/…`（=`rdoff[kx]`），
   7 个 bank 的 `address0` **接同一个地址**（`LOOP_764_38.v:1308-1490`，`3137/3293`）。

结论：**物理上是 7 个独立 RAM（编译期固定），但"这一拍访问哪个 bank"是运行期的 3-bit 号**
（写侧 one-hot 译码 ≈ 7 个 3-bit 比较器 × 7 bank；读侧 7:1 mux × 每抽头一份）。
这就是 cpp:218-226 记录的"28 bank 之后每个读写点都要复制 28 份地址+片选，LUT 35,361→42,920"
的机理 —— 常量化的只有 `wbuf` 的 8 个 oc bank（cpp:233-241），**不是 line_buf**。

---

## 4. 补零

| 方向 | 循环/模块 | 地址 | 数据 | 何时 |
|---|---|---|---|---|
| 左 | `Pipeline_315_10`（cpp:315） | `ic·76 + j`，`j∈[0,la)`，`phi_mul29=ic·76` | `d1=0` | 每个 `(y,r,ic)`，state 50→51→52→54 |
| 右 | `Pipeline_319_11`（cpp:319） | `ic·76 + (la+lb+j)` | `d1=0` | 每个 `(y,r,ic)`，state 53/54（`if(!wide1)`，见下） |
| 上/下 | `Pipeline_372_14`（cpp:372-375） | 与填充**同式**：`c·76+la+(t>>3)` | `d1=0`（`LOOP_372_14.v:542`） | `row_ok==0` 时替代 364，state 61→62 |
| 通道补齐 | 填充本身 | 见下 | 真数据（补零缓冲区的 0） | 每个 `(r,cinb)` |

RTL 细节：
- `315_10`：`add_ln316 = phi_mul29 + zext_ln316(j)`，`d1 = 8'd0`（`LOOP_315_10.v:382,456`）
- `319_11`：`add_ln320 = phi_mul29 + trunc_ln320`，`d1 = 8'd0`（`LOOP_319_11.v:389,463`）
- `372_14`：`add_ln373 = (t>>3) + empty_93(la)`，再 `×76`（DSP，`grp_fu_339_p1=15'd76`），`d1=0`
  （`LOOP_372_14.v:462,486,542`）—— 结构与 364 **同源**，只是没有 R 通道、数据恒 0
- `row_ok` 本身：`row_ok = (iy>=0) & (iy<H)`（`xor_ln306 & icmp_ln306`，`conv2d_engine.v:16920`）；
  state 61 分叉：`row_ok==1 → 63…70`(真填) / `row_ok==0 → 62`(写零)
  （`conv2d_engine.v` state61 分支；`7921` 行是 state 61 写 `row_ok=0` 的那条）

### 4.1 写了但永不读的地址

**(a) 通道补齐区（恒存在，前提 `cin % 8 != 0`）**
填充读满 `8·⌈cin/8⌉` 个通道（`nwide = 8·lb` 与 `cin` 无关，`c = cinb+(t&7)` 一定跑 0..7），
而乘加只跑 `ic < cin`（cpp:336-340 注释；RTL 计算 pipeline 的 ic 上界是 `cin`）。
⇒ 每行每列写进去的 `[cin, 8⌈cin/8⌉)` 个通道**永不读**。
浪费比例 = `8⌈cin/8⌉/cin − 1`：`cin=12 → +33.3%`，`cin=24/32/16 → 0`。

**(b) 列 `[lb, bwcol)`（只对 k=1 层恒浪费）**
cpp:309-322 的注释说"wide1 不需要右补零"，但 **C8 构建把 wide1 强制关掉了**：
`conv2d_engine.h:105-117` `#undef K1_WIDE_COLS` → `#define K1_WIDE_COLS 0`，
于是 `wide1 = false`（cpp:167），`if (!wide1)` **恒真** ⇒ 右补零循环每行都跑。
而 k=1 时计算只读 `cb = x2 < xn = lb`（cpp:687,851；`LOOP_764_38.v:3953/3955`
读地址 = `ic·76 + (cb + kx)`，k=1 时 `kx` 只有 0）⇒ 最后一块 `lb<bwcol` 时
`[lb, bwcol)` 的零**永不读**。典型：W=48 ⇒ 每行每通道白写 16 列（+33%）。
（k=3/5 时这些列大部分会被抽头读到，只有 `[lb+(k−1)·dil, bwcol)` 的一两列浪费。）

**(c) 零长度仍被"执行"的循环（不是写，是空转）**
`la=0` 时左补零 `315_10` 仍被 state 51/52 启动一次，`bwcol-la-lb=0` 时右补零同理；
每个 `(y,r,ic)` 至少 4~5 拍（state 50→51→52→54→50）。见 §5 第 4 项。

---

## 5. `1.769 拍/输入字节` 的分解

### 5.1 先固定 RTL 能证明的部分

单次 `(r,cinb)` 填充调用在父 FSM 里的完整路径（`conv2d_engine.v` 的 `ap_NS_fsm`）：

```
state 55 → 56 → 57 → 58 → 59 → 60 → 61(发 AR，等 ARREADY)
       → 63 → 64 → 65 → 66 → 67 → 68 → 69 → 70(启动 pipeline) → 71(等 ap_done)
```

- **固定 15 拍**（55…61 共 7 + 63…70 共 8，含 state 61 的 ARREADY 握手）
- 之后是 pipeline：`II=1`、迭代延迟 5 ⇒ `nwide + 4` 拍（`nwide = 8·lb`）
  （`..._364_13_csynth.rpt`：`VITIS_LOOP_364_13 | Iteration Latency 5 | II 1`）
- 关键：**同一时刻只有一笔 AR 在途**。state 61 发 AR → state 71 等 pipeline 跑完才回到 55 发下一笔。
  所以每个 burst 起始的 DDR 读延迟 `L_ddr`（AR 被接受到首个 R beat）**完全暴露**，
  4096-beat 的 RBUFF 只在"一笔 burst 内部"起作用，跨 burst 不重叠。
  首拍等待体现在 `ap_block_state2_pp0_stage0_iter1_grp1 = (t[1:0]==0) & (RVALID==0)`
  （`LOOP_364_13.v:672`），即 t 每走到 4 的倍数就整条流水线阻塞。

### 5.2 记账式分解（单位：拍 / 新输入字节）

```
T/B = 1                                            ① 写口 1 字节/拍
    + (15 + L_ddr) / (8·lb)                        ② 每 burst 固定 + 暴露读延迟
    + 5·cin / (8·lb·⌈cin/8⌉)                       ③ 每 (r,ic) 的 pad 空转
    + (8⌈cin/8⌉ − cin)/cin                         ④ 通道补齐（写了不读）
    + (bwcol − lb_needed)/lb_needed                ⑤ 尾块右补零（写了不读）
    + (span − stride)/stride                       ⑥ 行复用拷贝（k=3 s=1 时 +2.0）
```

| 项 | 公式 | 典型量级 | 性质 |
|---|---|---|---|
| ① | 1 | **1.000** | 硬下界（cpp:352-359 已论证） |
| ② | `(15+L)/(8lb)` | lb=64, L=30 → **0.088**；lb=16 → 0.35 | 可消除（多 AR 在途） |
| ③ | `5·cin/(8·lb·⌈cin/8⌉)` | cin=64, lb=64 → **0.078** | 可消除（pad 合进主流） |
| ④ | `8⌈cin/8⌉/cin − 1` | cin=12 → **+0.333** | 改了会动计算语义，只能 PS 侧补到 8 的倍数 |
| ⑤ | `bwcol/lb − 1` | W=48 尾块 → **+0.333** | 可消除（k=1 不写 `[lb,bwcol)`） |
| ⑥ | `span/stride − 1` | k=3,s=1 → **+2.0** | 已单列为"行缓冲下移 1.94 拍/字节" |

### 5.3 为什么"0.77 全是读停顿"这个说法只能算**存疑**

- 长 burst 实测 **1.08**（40 笔 burst 的 cosim，`记录_20261023_cin×cout…:262`），
  按 ② 反推 `(15+L)/608 ≈ 0.08` ⇒ `L ≈ 34 拍 ≈ 0.23 µs`，**这个量级是合理的**。
- 但 24 点网格的回归斜率是 **1.769**。若差额 0.69 全归给 ②，需要
  `L ≈ 390 拍（≈2.6 µs @150 MHz）` —— 对单笔 DDR 读偏高，**不太可能**。
- 更可能的解释：那个网格里混入了 ④/⑤ 形状（`cin=12` 的层、`W` 不是 64 倍数的尾块）
  和 ③（每 ic 空转），而它们是"填充"这一项里的常数项，不在"读 DDR"的物理带宽里。
- ⇒ **静态 RTL 无法唯一给出 0.77 的权重分配**（1.769 是拟合斜率，不是计数器）。
  能确定的：①②③⑤ 都在 RTL 里真实存在且可被手写引擎消掉；④ 只能靠 PS 侧补齐；
  ⑥ 是算法代价（跨行复用换来的 DDR 3× 节省），不应算进"填充损失"。

> 另注：`docs/…成本模型` 里 "1.769 = 1.0 + 0.77 读停顿" 的表述建议改成
> "1.769 = 1.0 + 0.77（burst 固定开销 + pad 空转 + 通道/列补齐）"，否则容易把
> 可以靠 RTL 结构消掉的项误判成"已到架构极限"。

---

## 6. 手写 `fill_engine` 规格

目标：替掉 `286_6 / 315_10 / 319_11 / 364_13 / 372_14` 五个 pipeline + 父 FSM 的
`55…71` 地址链，做到 **1 字节/拍、零空转、读延迟全隐藏**。

### 6.1 端口表

| 组 | 信号 | 位宽 | 说明 |
|---|---|---|---|
| 时钟复位 | `clk`, `rst_n` | 1/1 | 同步复位 |
| 参数（cfg，工作期间不变） | `cfg_in_fm_base` | 64 | `in_fm` 字节基址 |
| | `cfg_w`, `cfg_h` | 16/16 | 输入 W/H |
| | `cfg_cin` | 16 | 真实通道数（≤352） |
| | `cfg_hw8` | 32 | `H·W·8` |
| | `cfg_la`, `cfg_lb` | 8/8 | 本列块左补零 / 有效列数（≤76） |
| | `cfg_span`, `cfg_stride`, `cfg_pad` | 3/2/2 | 窗口高 / 步长 / padding |
| | `cfg_need0` | 16 | `y·stride − pad` |
| | `cfg_r_begin` | 3 | 本行要新装的行起点（0 或 keep） |
| | `cfg_wr_cols` | 8 | 要写多少列：k=1 填 `lb`，k≥3 填 `bwcol`（消除 ⑤） |
| | `cfg_zero_only` | 1 | 上/下补零（不读 DDR，只写 0） |
| 启动/状态 | `start`, `busy`, `done`, `err` | 1 | 单次调用 = 一行（`r_begin..span-1` 全部行 × 所有 cinb） |
| AXI4 读（master） | `m_arvalid/arready`, `m_araddr[63:0]`, `m_arlen[7:0]`, `m_arsize=3'd2`, `m_arburst=2'd1` | | 32-bit，burst ≤152 beat |
| | `m_rvalid/rready`, `m_rdata[31:0]`, `m_rlast` | | |
| line_buf 写 | `lb_we[6:0]`（one-hot）, `lb_addr[14:0]`, `lb_ce[6:0]`, `lb_d1[7:0]` | | 7 bank 共用同一地址；`lb_addr = c·76 + j` |
| 可选：行拷贝 | `cp_en`, `cp_src_bank[2:0]`, `cp_dst_bank[2:0]`, `cp_len` | | 若保留"搬移"而不是指针轮转，见 6.4 |

### 6.2 FSM（6 态）

```
IDLE ──start──► AR_ISSUE ──arready──► STREAM ──(burst done)──► NEXT
                   ▲                    │ 每拍：4 byte 写或 1 byte 写        │
                   │                    └──rvalid/rdata→hold 轮转──────────┘
                   └──────────── 还有 (cinb,r) ───────────────────────────┤
                                                                           ▼
                                                                         DONE
   错误分支：AR/RRESP 错 → ERR（只置标志，不重试）
```

- `AR_ISSUE`：算 `addr = base + 8·io_row + (cinb/8)·hw8`、`len = 2·lb−1`；
  **提前一拍发下一笔**（`NEXT` 里就发），保持 2 笔 AR 在途 ⇒ `L_ddr` 完全隐藏
  （gmem0 核支持 16 outstanding）。
- `STREAM`：`beat_cnt` 每 4 拍 +1；`(t&3)==0` 时 `hold<=rdata, rready=1`；
  `byte = hold>>((t&3)*8)`；`lb_we = 1<<bank`；`lb_addr = c*76 + la + (t>>3)`。
  1 字节/拍，**没有任何 `ap_start/ap_done` 子调用**。
- 每行/每 cinb 的循环计数在 `NEXT` 里推进；`cinb` 到 `8·⌈cin/8⌉` 为止。
- 状态数：**6**（含 ERR）；若把"行拷贝"也做进来再 +2（`CP`/`CP_WAIT`）。

### 6.3 预期拍/字节

| 场景 | 预期 | 依据 |
|---|---|---|
| 长 burst（lb≥64） | **1.00 ~ 1.02** | ②③ 都被消掉；只剩每 burst 2 拍的 `NEXT` 开销 /608 |
| lb=16 | ~1.06 | `2/(8·16)` |
| 上/下补零行 | **1.00**（纯写 0，无 DDR） | 同样 1 字节/拍 |
| cin=12 | 仍 1.00（按有效通道算）… 但物理写 16 通道 ⇒ 相对"逻辑输入字节" **1.33** | ④ 不变（除非 PS 侧补到 8 的倍数） |
| k=1 尾块 | 1.00（省掉 `[lb,bwcol)` 的零写，⑤ 消失） | `cfg_wr_cols = lb` |
| k=3 行复用 | 仍要 2 行搬移；若改成**指针轮转**（bank 号用 `y mod span` 而不是物理搬移）可把"行缓冲下移 1.94 拍/字节"整项消掉 | 需改所有读侧的 bank 选择（运行期 → `(r+phase) mod span`），代价是读侧多一次 3-bit 加法+取模 |

> 6.4 建议（仅当要做）：把"搬移"换成"相位指针"。现在 `bank r` 恒等于"窗口第 r 行"，
> 所以每前进一行必须搬 `span−stride` 行；若让 `bank = (r + y·stride) mod span`，
> 则无需搬移，代价是 **读侧 bank 号 = (rdoff[ky] + phase) mod 7**，这是运行期模 7，
> 7 不是 2 的幂（不能位切片）⇒ 用 8→7 的 one-hot 旋转寄存器（7 个 FF × 3 bit）可以做到零 LUT 增长。
> **本文不主张现在就做**：这会把读侧的 bank mux 从"3-bit 常量选择"变成"旋转"，与 §3.3
> 的 28-bank 教训同类，需先算 LUT。

---

## 7. 存疑 / 没做的事

1. **0.77 的权重分配**（§5.3）：1.769 是回归斜率，静态读 RTL 无法唯一归因。需要
   在 `state 71`/AR 通道上插计数器（cosim）才能定。
2. `L_ddr` 的实际值：没有仿真/上板数据；只能从 1.08 反推出 ~34 拍，从 1.769 反推出 ~390 拍，
   两者矛盾 ⇒ 说明 0.77 里混了非 DDR 项（见 5.3）。
3. `add_ln304_1 = sub_ln304 + reg_4333` 中 `(need0·W + gc0 + la)` 的逐项拆分只核到结构
   （`sub_ln304` 含 `−1` 修正，`reg_4333 = tmp4·W`，`conv2d_engine.v:16182-16190,17118`），
   没有逐位复算；不影响 `= io_base` 的结论（C++ 侧 `io_base = need0·W+gc0+la`，cpp:281）。
4. `nwide==0`（`lb=0`）时 `ARLEN` 被置 0 但 `ARVALID` 仍可能在 state 61 拉高
   （`conv2d_engine.v:9732` 只看 `row_ok`）⇒ 会多发 1 个 beat。只为边界情形，
   没在 RTL 里找到把它挡掉的谓词，**存疑**（可能是无害的越界读）。
5. `764_38` 的两个读口在同一拍读两个不同抽头时，bank 冲突（同 bank 两口）如何处理
   —— 属于计算相位，本文没展开。
6. 本文没有验证 `line_buf` 的 `q1` read-first 语义对拷贝路径的正确性影响
   （`line_buf_RAM_2P_BRAM_1R1W.v:41-50`，注释 "read first"）；拷贝读写不同 bank，理论上无关。

---

## 8. 复现命令（全部只读）

```bash
cd /data/work/hls_c8_csynth/r9_k1_l16/solution1/syn/verilog/

# 1) 7 个 bank 的端口/例化
grep -n 'line_buf.*_address1\|line_buf.*_we1' conv2d_engine.v | head -40
sed -n '5414,5425p' conv2d_engine.v

# 2) 填充 pipeline 的端口连接（谁给 nwide/la/bank）
sed -n '6120,6160p' conv2d_engine.v

# 3) 地址公式
sed -n '685,700p;810,840p' conv2d_engine_conv2d_engine_Pipeline_VITIS_LOOP_364_13.v
sed -n '16185,16210p;16875,16885p;17045,17055p' conv2d_engine.v

# 4) bank 是运行期变量
sed -n '455,472p' conv2d_engine_conv2d_engine_Pipeline_VITIS_LOOP_286_6_VITIS_LOOP_287_7.v
sed -n '515,625p' conv2d_engine_conv2d_engine_Pipeline_VITIS_LOOP_364_13.v
sed -n '1300,1330p' conv2d_engine_conv2d_engine_Pipeline_VITIS_LOOP_764_38.v

# 5) 每次填充调用的 15 拍固定开销（55…71）
awk '/ap_ST_fsm_state55 : begin/,/ap_ST_fsm_state72 : begin/' conv2d_engine.v

# 6) report
grep -n 'VITIS_LOOP_364_13\|Iteration' solution1/../report/conv2d_engine_Pipeline_VITIS_LOOP_364_13_csynth.rpt
sed -n '157,166p' report/conv2d_engine_csynth.rpt
```
