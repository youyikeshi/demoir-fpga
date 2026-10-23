# conv2d_engine 顶层控制与相位时序（纯静态分析）

对象：`/data/work/hls_c8_csynth/r9_k1_l16/solution1/syn/verilog/conv2d_engine.v`（17,588 行）
配套子模块：同目录 `conv2d_engine_conv2d_engine_Pipeline_VITIS_LOOP_*.v`（19 个）
C++ 源：`pynq_specialized_code/pl/src/conv2d_engine.cpp`
csynth：`syn/report/conv2d_engine_csynth.rpt`

> 本文所有结论都标了行号。行号格式：顶层文件写 `L<行>`，子模块写 `<模块名>:L<行>`。
> 不确定的地方标「存疑」并给出验证方法。**没有**编译/仿真/上板，全部是文本核对。

---

## 0. 结论摘要（先看这 6 条）

1. **116 个乘法器的 5.3% 利用率，主因不是填充带宽，而是「每 (pixel, ocb) 块的收尾（epilogue）被串行化了 16 拍」。**
   k=1 收尾 `LOOP_730_36` 的 RTL 是一条 **II=1、16 次迭代**的流水（不是展开的并行 16 路）：
   循环条件 `icmp_ln730 == 5'd16`（`...LOOP_730_36.v:1009`），累加器用 **16:1 动态下标 mux**
   （`sparsemux_33_4_26`，`sel = trunc_ln730 = i[3:0]`，同文件 L450–L460 / L513–L520）。csynth 报该实例 latency **min=max=21**
   （`conv2d_engine_csynth.rpt:54`），即 16 拍迭代 + 5 拍流水。
2. **一个 (pixel, 16-oc) 块的耗时 = cin + 23 拍**，其中只有 cin 拍在乘加，23 拍是固定开销
   （4 拍 FSM + 19 拍收尾）。cin=64 时 68+19=87 拍，与 cosim 实测「68+~19」逐字吻合
   （`docs/设计/记录_20261023_k1十六车道与两处加宽证伪.md:104`）。
   ⇒ cin=8 的层，每块只有 8 拍干活、23 拍空转，效率 26%。
3. **填充/计算/写回不能重叠，是同一个 167 态 one-hot FSM 的串行结果，不是协议限制。**
   证据：`ap_start_reg` 的置位状态互斥——填 `LOOP_364_13` 只在 S70（L7910）、计算 `LOOP_684_33` 只在 S138
   （L7970）、写 `LOOP_945_50` 只在 S152（L8054）；且每一段的 `ap_done` 都被单独一个状态阻塞
   （`ap_ST_fsm_state71_blk` L9524、`ap_ST_fsm_state141_blk` L9236、`ap_ST_fsm_state153_blk` L9292）。
   读口 gmem0 与写口 gmem1 是**两个独立的 m_axi**（L7540/L7586），硬件上本来可以并行。
4. **FSM 里硬编码了 41 个「纯延时」状态**（只在 `case (ap_CS_fsm)` 里出现、不驱动任何信号）：
   19,24–26,56–58,63–69,79–84,96–102,112–117,154–157,162–165。它们是 HLS 对「发 AR→收 R」和
   「发 W→收 B」固定延迟的补偿。单个权重字节的读取就要吃掉 7 个这样的状态。
5. **写回路径上一版「每字节一笔事务」的问题已经修掉**：现在 AW 由 FSM 在 S151 一次发出，
   `AWLEN = sext_ln279_reg_8655 = xn`（元素数，8 B/元素）⇒ xn=64 时 64 beat = 512 B，AXI `AWLEN=63`。
   证据：`gmem1_0_AWLEN` mux 在 L9784–L9795；写回流水自己**不发 AW**（`...LOOP_945_50.v:519/533`
   `m_axi_gmem1_0_AWLEN=0; AWVALID=0`）。同理填充的行读：`ARLEN = p_cast151`（≈ lb×2 个 32-bit 元素）
   由 FSM 在 S61 一次发出（L9694–L9701），填流水自己**不发 AR**（`...LOOP_364_13.v:775` ARVALID=0）。
6. **可删的 HLS 冗余**：41 个纯延时态、3 态一组的 3 级乘法器等待、pad 循环在 `la=0` 时仍按 cin 次空转
   （S50–S54，见 §5.4）、16 车道收尾的 16 拍串行。手写 FSM 可以压到 **12 个状态**（§7）。

---

## 1. 坐标系

| 项 | 值 | 位置 |
|---|---|---|
| FSM 寄存器 | `reg [166:0] ap_CS_fsm`，one-hot，167 个 `ap_ST_fsm_stateN` 参数 | `L144`–`L311` |
| 状态译码 | `assign ap_CS_fsm_stateN = ap_CS_fsm[32'dN-1];` | `L16290`–`L16390` |
| 次态逻辑 | `always @ (*) begin case (ap_CS_fsm) ... endcase` | **`L15318`–`L16115`** |
| 状态阻塞（stall）| `ap_ST_fsm_stateN_blk`，合并进 `ap_NS_fsm` 的 `_blk` 使能 | `L9055`–`L9352` |
| 阻塞源定义 | `ap_block_state*` | `L16544`–`L16588` |
| 子模块 `ap_start_reg` | 置位状态 = 「哪一拍启动哪个流水」 | `L7845`–`L8070` |
| 时序数据通路 | 167 态 × one-hot 的寄存器更新 | `L8080`–`L9053` |
| 组合数据通路（1526 处 `1'b1 == ap_CS_fsm_stateN` 守卫）| 各 RAM 的 ce/we/addr、AXI 信号 mux | `L9061`–`L15317` |
| AXI 读 mux（gmem0）| ARADDR/ARLEN/ARVALID/RREADY 按状态选源 | `L9686`–`L9760` |
| AXI 写 mux（gmem1）| AWADDR/AWLEN/AWVALID/BREADY/WDATA | `L9778`–`L9840` |
| `ap_done/ap_ready` | 只在 S167 | `L16109`–`L16113`；`L8062` 区 |

**一个必须先纠正的前提**：167 个状态**全部可达**（不存在「只用 82 个」）。
每个状态在 `case` 里都有分支，且 one-hot 位宽就是 167（`L144` 起 167 行参数）。
`docs/rtl_map/00_fsm_digest.md` 说「实际用到 82 个状态」是把「有组合赋值的状态」当成了全部，
漏掉了 41 个纯延时态和 44 个只做寄存器算术的状态。

状态选择器（贯穿全文）：

- `empty_reg_7546` = ksize 的运行期编码（`3'd1` / `3'd3` / 其他=5），在 `L16826` 区按 `==3'd1`、`==3'd3` 比较；
- `empty_103_reg_7953` = 「16 车道通道组」选择位（0/1），决定收尾走 16 lane 还是 8 lane；
- `empty_99_reg_7897` = `cin & 3`；`icmp_ln474 = (empty_99==0)` = `cin%4==0`（`L16826`）；
- `icmp_ln437/439` = 权重装载的 4/8 字节对齐判定（`L16820`/`L16822`）。

---

## 2. 167 个状态的相位分类表

列的读法：
- **拍数** = 该状态常规停留拍数。`1` 表示 `ap_ST_fsm_stateN_blk` 恒 0（L9055–L9352 里是 `assign ... = 1'b0`），
  即**固定烧掉 1 拍**；`≥` 表示该状态被 `*_blk` 或 `ap_done` 阻塞，拍数取决于信号。
- 「等什么」一栏来自 `ap_ST_fsm_stateN_blk` 的表达式（L9055–L9352）与 `ap_block_state*`（L16544–L16588）。

### 2.1 P0 —— AXI-Lite 参数锁存 + C++ 入参合法性检查（S1–S22，每次内核调用一次）

| 状态 | 干什么 | 等什么 | 拍数 |
|---:|---|---|---:|
| 1 | 锁存全部 s_axilite 参数（H/W/Hout/Wout/cin/cout/ksize/…/指针） `L8398`–`L8430` | `ap_start`（`L9362`） | ≥1 |
| 2 | 检查组 1：`(Hout-1)*stride>H-1` 等；错误写 `retval_3` | — | 1 |
| 3–5 | 乘法器 `grp_fu_4232/4236`（3 级流水的 3 拍） | — | 3 |
| 6 | 同上检查，失败 → S167（返回 ERR） | — | 1 |
| 7–9 | W 侧同类检查 + `icmp_ln134/135` | — | 3 |
| 10 | `empty_103` / `or_ln137` 判定 | — | 1 |
| 11 | `add_ln140`（shift 范围准备） | — | 1 |
| 12–14 | 乘法器 3 拍 | — | 3 |
| 15 | `icmp_ln141`（shift 合法性） | — | 1 |
| 16 | `icmp_ln159` + `span = 2*pad+1` | — | 1 |
| 17 | `icmp_ln168`（relu_en 的 C8 声明位） | — | 1 |
| 18 | 选 `bw = wide1?448:(stride==2?32:64)` 的输入 | — | 1 |
| 19 | **纯延时** | — | 1 |
| 20 | `mul_ln169 = bw*bwcol` 等 | — | 1 |
| 21 | 写 `bwcol_reg_8016` | — | 1 |
| 22 | `icmp_ln170 = (bwcol>76)` 检查 + `x0=0,gc0=-pad` + **启动 `LOOP_257_2`(rdoff 初始化)** | — | 1 |

> 这一整段（22 拍）只在每次 kernel 调用出现一次，与 35 s 的量级无关，但它解释了为什么
> 「小层」的固定成本高——每层至少 22 + 4（bias）+ 6（x0 头）拍。

### 2.2 P1 —— bias 装载与循环不变式（S23–S32）

| 状态 | 干什么 | 等什么 | 拍数 |
|---:|---|---|---:|
| 23 | **发 bias 的 AR**：`ARADDR=sext_ln244_1`，`ARLEN=zext_ln244`（=cout 个元素）`L9698`/`L9712` | `gmem0_0_ARREADY` 且 `LOOP_257_2_ap_done`（`L9376`） | ≥1 |
| 24–26 | **纯延时**（等 R 数据，3 拍） | — | 3 |
| 27–29 | 乘法器/地址预计算（`dgc`、`empty_107/108`） | — | 3 |
| 30 | 读 `rdoff`、算 `shl_ln268` | — | 1 |
| 31 | **启动 `LOOP_244_1`（bias 拷贝流水）** | — | 1 |
| 32 | 等 bias 拷贝完成；同时算 `cpadded/select_ln268_6/dgc` 等 | `LOOP_244_1_ap_done`（`L9402`） | ≥4 |

> `LOOP_257_2` = C++ 的 `rdoff[t]=t*dil`（cpp:L256–L259），csynth latency 7 拍（rpt:41）。
> `LOOP_244_1` = C++ 的 `bias_buf[i]=bias[i]`（cpp:L244），latency 4+ 拍。

### 2.3 P2 —— 列块（x0）外层（S33–S40）

| 状态 | 干什么 | 等什么 | 拍数 |
|---:|---|---|---:|
| 33 | **x0 循环头**：`icmp_ln268_6 = (x0 < Wout)`，假 → S167（正常结束） | — | 1 |
| 34 | `la = (gc0<0)?-gc0:0`、`xn = min(bw, Wout-x0)` | — | 1 |
| 35 | `la_1`、`tmp_43`、`trunc_ln272/274` | — | 1 |
| 36 | `lb = clamp(W-gc0-la, 0, bwcol-la)` | — | 1 |
| 37 | `nwide = lb<<3`（=本行要搬的字节数）、`icmp_ln319` | — | 1 |
| 38 | `add_ln279_1`（=本行 AR 的元素数） | — | 1 |
| 39 | `y=0`、`row_buf_valid=0`、`icmp_ln657=(xn>0)`、`p_cast151`（AR 长度） | — | 1 |
| 40 | **y 循环头**：`y_1=y+1`、`need0`、`and_ln284=(row_buf_valid&icmp_ln284)`、`icmp_ln279_2=(y==Hout)` | — | 1 |

### 2.4 P3 —— 行缓冲下移（keep-shift，S41–S44）

| 状态 | 干什么 | 等什么 | 拍数 |
|---:|---|---|---:|
| 41–42 | 乘法器 3 拍（`mul_ln280`） | — | 3 |
| 43 | 循环头：`icmp_ln285==1`（keep==0）或 `and_ln284==0` → 跳过；否则 | — | 1 |
| 44 | **`LOOP_286_6_VITIS_LOOP_287_7`**：`line_buf[ic][r][j]=line_buf[ic][r+stride][j]`（cpp:L285–L291） | `LOOP_286_6_ap_done`（`L9434`） | ≥7+keep·cin·bwcol |

### 2.5 P4 —— 行填充（fill，S45–S71）★ 41% 时间的那一段

| 状态 | 干什么 | 等什么 | 拍数 |
|---:|---|---|---:|
| 45 | **r 循环头**：`icmp_ln304 = (r<span)`，假 → S72（填充结束）；同时 `r=0`、`ocb=0`、`row_ok=(0<=iy<H)` | — | 1 |
| 46–48 | 乘法器 3 拍（pad / ic 边界） | — | 3 |
| 49 | `ic_1=0`、`phi_mul29`（ic 循环初始化） | — | 1 |
| 50 | **ic 循环头**：`icmp_ln314=(ic==cin)`；`cinb=0` | — | 1 |
| 51 | **启动 `LOOP_315_10`（左补零 `for j<la`）** | — | 1 |
| 52 | 等左补零；`icmp_ln319=(j<bwcol)` 决定是否要做右补零 | `LOOP_315_10_ap_done`（`L9458`） | ≥la+2 |
| 53 | **启动 `LOOP_319_11`（右补零 `for j=la+lb..bwcol`）** | — | 1 |
| 54 | 等右补零 | `icmp_ln319 & LOOP_319_11_ap_done`（`L16573`） | ≥(bwcol-la-lb)+2 |
| 55 | **cinb 循环头**：`icmp_ln324=(cinb==bound)`，假 → S56；真 → S45（下一 r） | — | 1 |
| 56–58 | **纯延时**（3 拍，地址算术等待） | — | 3 |
| 59 | `mul_ln324` | — | 1 |
| 60 | `add_ln324_1`（本行 io 地址） | — | 1 |
| 61 | **发本行 AR**：`ARADDR=sext_ln364_fu_6185_p1`，`ARLEN=p_cast151`，`L9694`–`L9701`/`L9732`；`row_ok=1` 才发 | `gmem0_0_ARREADY`（`L9490`，`ap_block_state61_io`） | ≥1 |
| 62 | 若 `row_ok==0`：**启动 `LOOP_372_14`（整行补零）** | `LOOP_372_14_ap_done`（`L9498`） | ≥nwide/4 |
| 63–69 | **纯延时 7 拍**（AR→R 的固定延迟补偿） | — | 7 |
| 70 | **启动 `LOOP_364_13`（DDR→line_buf 流式搬运）** | — | 1 |
| 71 | 等搬运完成；写 `line_buf` 的 ce/we/d1（`L9524` 区） | `row_ok & LOOP_364_13_ap_done`（`L16581`） | ≥nwide/4+5 |

### 2.6 P5 —— ocb 循环与权重装载（S72–S127）

| 状态 | 干什么 | 等什么 | 拍数 |
|---:|---|---|---:|
| 72 | **ocb 循环 latch**：`icmp_ln436=(indvar1858==cpadded)`，真 → S40（下一 y）；假 → S73。同时 `row_buf_valid=1`、`off_ocb/off_ocb12/off_ocb28` 递推、`y=y_1` | — | 1 |
| 73–74 | 乘法器 3 拍 | — | 3 |
| 75 | `icmp_ln437/439`（4/8 字节对齐判定）+ `trunc_ln437` | — | 1 |
| 76 | `ocbl/ocbl1/off/off_1/and_ln451` + **按 ksize & cin%4 派发**：k=1 且 `cin%4==0`→S106，否则 k=1→S92，k≠1→S77，`and_ln451`→S166 | — | 1 |
| 77 | k=3/k=5 权重地址递推 + `icmp_ln517/566` | — | 1 |
| 78 | **发 wgt 的 AR**（`ARLEN=empty_114`，一小段） | `gmem0_0_ARREADY`（`L9544`） | ≥1 |
| 79–84 | **纯延时 6 拍** | — | 6 |
| 85 | `ic_3++` | — | 1 |
| 86 | 循环条件 `icmp_ln521=(ic<cin)`；假 → S77（下一 oc） | — | 1 |
| 87–89 | 等 R：`RREADY=1`（`L9756`），88 拍里写 `ap_predicate_*`，89 拍把 9 个 tap 分发到 `wgt3_buf_8/17/26/35/44/53/62/71` | `gmem0_0_RVALID`（`L9570`–`L9586`） | 3 |
| 90 | 写 `wgt3_buf`（9 个常量 bank） | — | 1 |
| 91 | `ic_3++` → 回 S86 | — | 1 |
| 92 | k=1 **慢速**权重循环头 `icmp_ln488=(i<ocbl1)`，假 → S110 | — | 1 |
| 93 | `icmp_ln490=(ic<cin)` | — | 1 |
| 94 | 发 AR（`ARLEN=64'd1`，1 个字节） | — | 1 |
| 95 | 等 ARREADY | `gmem0_0_ARREADY`（`L9551`） | ≥1 |
| 96–102 | **纯延时 7 拍** | — | 7 |
| 103 | 等 R | `gmem0_0_RVALID`（`L9062`） | ≥1 |
| 104 | 写 `wgt1_buf`（16 个 bank 并行） | — | 1 |
| 105 | `ic_3++` → S93 | — | 1 |
| 106 | k=1 **快速**路径地址预计算（`cin%4==0`） | — | 1 |
| 107–109 | 计算 + **启动 `LOOP_475_16`（32-bit 宽读权重）**，`L7934` | — | 3 |
| 110 | 等权重宽读完成；写 `wgt1_buf_*`（`L9084`） | `op1405 & LOOP_475_16_ap_done`（`L16545`） | ≥ocbl1·cin+14 |
| 111 | k=5 发 AR（`ARLEN=empty_116`） | `gmem0_0_ARREADY`（`L9092`） | ≥1 |
| 112–117 | **纯延时 6 拍** | — | 6 |
| 118 | `ic_2++` | — | 1 |
| 119 | `icmp_ln570=(ic<cin)` | — | 1 |
| 120–126 | 等 R + 分批写 25 个 `wgt_buf` bank（每拍一组） | `gmem0_0_RVALID`（`L9118`–`L9166`） | 7 |
| 127 | `ic_2++` → S119 | — | 1 |

> **k=3 的权重装载是当前最大的隐藏开销之一**：每个 (oc, ic) 走 S78→S79..84→S86→S87..89→S90→S91 ≈ **13–14 拍搬 9 字节**。
> ocbl=8、cin=12 时一块要 96 组 ≈ 1300 拍，而 k=3 的计算只有 cin+23=35 拍/像素。

### 2.7 P6 —— x2（像素）循环（S128–S150）

| 状态 | 干什么 | 等什么 | 拍数 |
|---:|---|---|---:|
| 128 | **x2 循环头 + 尾部补零派发**：`x2=0`；`icmp_ln657=(xn>0)` 且尾部已补完 → S133（进像素循环）；`xn==0` → S158（跳过）；否则 S129/S131（补零） | — | 1（但见下注） |
| 129 | 启动 `LOOP_649_30`（8 车道尾部清零，`icmp_ln647=(i<ocbl)`） | — | 1 |
| 130 | 等清零 | `icmp_ln647==0 & LOOP_649_30_ap_done`（`L16549`） | ≥xn+2 |
| 131 | 启动 `LOOP_639_28`（16 车道尾部清零，`icmp_ln637=(i<ocbl1)`） | — | 1 |
| 132 | 等清零 | `icmp_ln637==0 & LOOP_639_28_ap_done`（`L16553`） | ≥xn+2 |
| 133 | **x2 条件**：`icmp_ln657_1=(x2==xn)`，真 → S151（写回）；`add_ln657`、`mul_ln658` | — | 1 |
| 134 | **ksize 派发**（k=1→S138，k=3→S135，其他→S142）+ `wbuf_*_addr_reg` | — | 1 |
| 135 | **`LOOP_764_38`（k=3 计算，40 DSP）** | `ap_done`（`L9204`） | ≥cin+? |
| 136 | 启动 `LOOP_819_41`（k=3 收尾，8 lane） | — | 1 |
| 137 | 等 k=3 收尾 | `empty==3 & LOOP_819_41_ap_done`（`L16557`） | ≥13 |
| 138 | 启动 `LOOP_684_33`（k=1 计算，8 DSP + 8 LUT-DSP，16 lane） | — | 1 |
| 139 | 等 k=1 计算 | `LOOP_684_33_ap_done`（`L9232`） | ≥cin（记录按 cin+4 计，含 4 拍流水，见 §8#2） |
| 140 | 启动 `LOOP_730_36`（k=1 收尾，16 lane 串行） | — | 1 |
| 141 | 等 k=1 收尾；`x2++`（`L8384`） | `empty==1 & LOOP_730_36_ap_done`（`L16561`） | **≥19（实测）** |
| 142 | generic 派发（k=1→S145 `LOOP_849_43`，k=3→S143 `LOOP_865_44`，k=5→S146 `LOOP_892_47`）+ `acc_even/odd` 重装 | — | 1 |
| 143 | `LOOP_865_44`（generic k=3 计算，5 DSP） | `ap_done`（`L9246`） | ≥12 |
| 144 | 收尾：`acc+bias`（每 lane 一次） | — | 1 |
| 145 | `LOOP_849_43`（generic k=1 计算） | `ap_done`（`L9256`） | ≥6 |
| 146 | `LOOP_892_47`（generic k=5 计算，20 DSP） | `ap_done`（`L9264`） | ≥26 |
| 147 | `v = acc >> shift` | — | 1 |
| 148 | ReLU + `saturate_int8` | — | 1 |
| 149 | 写 `wbuf`（8 bank） | — | 1 |
| 150 | `i_6++`；`icmp_ln838=(i<8)` → 回 S142 | — | 1 |

> **注（实测结构，容易漏）**：`i_4`/`i_5` 的「尾部补零」`for i<16`（或 `i<8`）循环**总是跑满**：
> `i_4` 在 S110 清 0（`L8131`），在 S132 自增（`L8133`），退出条件是 `i_4==16`（`L16840`）。
> 所以即使 `ocbl1==16`（本网络 cout 全是 16 的倍数，一个尾通道都没有），每个 (y,ocb) 块仍要走
> **16 轮 × (S128+S132) = 32 拍**。这一段没有任何数据搬运，是纯 FSM 空转。

> **generic 路径（k=5）的收尾在 FSM 里逐 lane 做**：S144/S147/S148/S149/S150 = 每 lane 5 拍 × 8 lane = 40 拍/像素。
> 这是 249 层里 k=5 那些层慢得多的直接原因。

### 2.8 P7 —— 写回（S151–S166）

| 状态 | 干什么 | 等什么 | 拍数 |
|---:|---|---|---:|
| 151 | **发第一个 8-oc 块的 AW**：`AWADDR=sext_ln945`，`AWLEN=sext_ln279_reg_8655 = xn`（8 B/元素）`L9784`–`L9795` | `gmem1_0_AWREADY`（`L9282`） | ≥1 |
| 152 | 启动 `LOOP_945_50`（`wbuf[0..7]` → 64-bit 拼接 → DDR 流式写，`...LOOP_945_50.v:485` `icmp_ln945=(px==xn)`） | — | 1 |
| 153 | 等 W 流完成；`wbuf_*_ce0`（`L9292`） | `LOOP_945_50_ap_done` | ≥xn+3 |
| 154–157 | **纯延时 4 拍**（等 B） | — | 4 |
| 158 | 等第一个块的 B；`and_ln963`（是否还有第二个 8-oc 块）→ S166 或 S159 | `gmem1_0_BVALID`（`L9308`，`ap_block_state158`） | ≥1 |
| 159 | 发第二个块 AW（k=1 16 车道，`AWLEN=zext_ln279_4`） | `gmem1_0_AWREADY`（`L9316`） | ≥1 |
| 160 | 启动 `LOOP_967_51` | — | 1 |
| 161 | 等第二个 W 流 | `LOOP_967_51_ap_done`（`L9328`） | ≥xn+3 |
| 162–165 | **纯延时 4 拍** | — | 4 |
| 166 | 等第二个块的 B；`ocb += 8`、`indvar1858 += 8`；→ S72 | `ap_predicate_op1744 & gmem1_0_BVALID`（`L9344`） | ≥1 |
| 167 | `ap_done/ap_ready`，→ S1 | `ap_start` | ≥1 |

---

## 3. 块级时序图

### 3.1 三个嵌套循环（由归纳变量更新点反推，全部有行号）

```
S22  x0=0,gc0=-pad, select_ln169=bw            L8375, L8107
└─ S33  while (x0 < Wout)                      L16780 / L15460
   ├─ S34..S39  la/lb/xn/nwide, y=0, row_buf_valid=0      L8330–L8395
   ├─ S40  y_1=y+1; if (y==Hout) -> S33         L16792 / L8548
   │  └─ while (y < Hout)  [S41 行向下移 / S45..S71 填充]
   │     ├─ S43/S44  LOOP_286_6        (keep 行下移)      L15498
   │     ├─ S45  while (r < span)      (fill 行)          L16804
   │     │   ├─ S50..S54  pad 零填充（每个 ic 一次）
   │     │   └─ S55  while (cinb < cin)  → S56..S71 (AR+搬运)
   │     └─ S72  if (indvar1858 == cpadded) -> S40 (下一 y)       L16818
   │        └─ while (ocb < cpadded)  [S73..S76 权重装载 → S128..S166]
   │           ├─ S76..S127   权重装载（k=1/k=3/k=5 三条路）
   │           ├─ S128  if (x2 == xn) -> S151 ; else S133       L16848
   │           │  └─ while (x2 < xn) [S134..S141 计算+收尾]
   │           └─ S151..S166  写回 (AW/W/B)
   │              S166  ocb += 8 -> S72
   └─ S40 -> S33 (下一列块)
```

### 3.2 一次 `(x0, y, ocb)` 块的时间线（k=1 快路径，`cin`、`xn` 为变量）

```
时刻(拍)  状态        事件                                                      累计
  0      S72         ocb latch（indvar1858==cpadded?）/ row_buf_valid=1          1
  1      S73,74,75   3 级乘法器：off_ocb / off_12 / off_28 递推                   3
  4      S76         算 ocbl, ocbl1, 派发                                        1
  5      S106,107    地址预计算                                                 2
  7      S108        写 ap_start_reg(LOOP_475_16)                                 1
  8      S109,110    ── 权重宽读 ocbl1·cin 拍（+14 拍流水）──                     ocbl1·cin+14
  .      S128×16    尾部补零 i 循环：本网络无尾通道，但仍走满 16 轮 × 2 态            32
  .      S128        x2 循环头（icmp_ln636=1 或对齐位=0 ⇒ 直通 S133）                  1
  .      ┌─────── xn 次 ───────────────────────────────────────────────────────┐
  .      │ S133    x2 条件 (add_ln657)                                          1
  .      │ S134    ksize 派发 + wbuf 地址                                        1
  .      │ S138    写 ap_start_reg(LOOP_684_33)                                 1
  .      │ S139    ── 计算 cin 拍（流水 5 级，尾部 4 拍摊到邻状态）──             cin
  .      │ S140    写 ap_start_reg(LOOP_730_36)                                 1
  .      │ S141    ── 收尾 16 lane 串行 ──                                       19
  .      └───────────────────────────────────────────────────────────────────────┘
  .      S151        发 AW (AWLEN=xn)                                             1
  .      S152        写 ap_start_reg(LOOP_945_50)                                 1
  .      S153        ── W 流 xn+3 拍 ──                                           xn+3
  .      S154–157    纯延时等 B                                                  4
  .      S158        等 B / 判是否第二块                                          1
  (k=1 且 cout 跨 2 个 8-oc 块时)
  .      S159        发第二块 AW                                                 1
  .      S160,161    ── 第二块 W 流 xn+3 拍 ──                                    xn+3
  .      S162–165    纯延时                                                      4
  .      S166        等 B + ocb += 8                                             1
```

**每 (pixel, 16-oc) 块 = S133..S141 = `1 + 1 + 1 + cin + 1 + 19 = cin + 23` 拍。**
（4 个 1 拍状态 = S133/S134/S138/S140；cin 是 S139 里 `LOOP_684_33` 的 trip，流水 5 级多出的 4 拍摊到相邻状态上，见 §8 存疑 #2。）
与 cosim 记录的 `68（cin+4）+ ~19`（`记录_20261023_k1十六车道与两处加宽证伪.md:104`，cin=64 → 87 拍）
**逐字一致** ⇒ 该模型可直接用于成本估算：

```
块周期数 = 4 + (L_wgt = ocbl1·cin + 14) + 32(尾部 i 循环) + xn·(cin + 23)
         + (L_wb ≈ xn + 12 [+ xn+12 if 16lane])
MAC 数   = 16 · cin · xn
效率     = 16·cin·xn / [xn·(cin+23) + 小项] ≤ 16·cin/(cin+23)
```

`cin=8` 时上界 16×8/31 = **4.1 MAC/拍/16 车道**（理论 16）⇒ 26%；
`cin=64` 时 16×64/87 = **11.8 MAC/拍** ⇒ 74%。全网实测 6.2 MAC/拍（0.93 GMAC/s @150 MHz）
正好落在这个区间内，说明**收尾串行是主因，DDR 是次因**。

### 3.3 k=3 与 generic(k=5) 的差异

```
k=3:  S133(1) S134(1) S135(cin+?) S136(1) S137(13) ...              → 每像素 ≈ cin + 16
generic k=1: S142(1) S145(6) S144(1) S147(1) S148(1) S149(1) S150(1) ×8 lane → 每像素 ≈ 8×6 + 12 = 60 拍
generic k=5: 上面 + S146(26) → 每像素 ≈ 8×6 + 26 + 12 ≈ 86 拍
```

> k=1/k=3 快路径的收尾是「流水线里 16/8 次迭代」，generic 路径的收尾是「FSM 里 8 次迭代 × 5 态」。
> 两条都比理想慢一个数量级。

---

## 4. 为什么填充 / 计算 / 写回不能重叠？

**答：不是被握手卡住，是 FSM 串行 + 单缓冲（没有 ping-pong）造成的结构串行。** 四条独立证据：

1. **启动时刻互斥。** `ap_start_reg` 的置位状态（L7845–L8070）：
   | 流水 | 置位状态 | 行号 |
   |---|---|---|
   | `LOOP_364_13`（填充） | S70 | `L7910` |
   | `LOOP_475_16`（权重） | S108 | `L7934` |
   | `LOOP_684_33`（k=1 计算） | S138 | `L7970` |
   | `LOOP_730_36`（k=1 收尾） | S140 | `L7982` |
   | `LOOP_945_50`（写回） | S152 | `L8054` |
   任一时刻只有一个 `*_ap_start_reg` 会被置 1（one-hot 状态），且每个模块的 `ap_ready` 会立刻清 0，
   所以**同一时刻至多一个子流水在跑**。

2. **每一段都被一个「等 done」状态封口。**
   `ap_ST_fsm_state71_blk = ... LOOP_364_13_ap_done==0`（L9524）、
   `ap_ST_fsm_state139_blk = ... LOOP_684_33_ap_done==0`（L9232）、
   `ap_ST_fsm_state153_blk = ... LOOP_945_50_ap_done==0`（L9292）。
   FSM 必须等前一段 `ap_done` 才会进下一段，中间没有「启动下一段同时等上一段」的并行分支。

3. **缓冲区只有一份，没有双缓冲。**
   `line_buf[CIN_MAX][SPAN_MAX][BWCOL_MAX]`（cpp:L210）被填充写、被计算读；
   `wbuf[OC_BLOCK*K1_BLK][SPAN_MAX*64]`（cpp:L239）被收尾写、被写回读。
   没有 ping-pong ⇒ 即使 FSM 允许，数据依赖也不允许重叠。

4. **两个 AXI 口本来就是分开的，硬件上完全可以并行。**
   `gmem0_m_axi`（读，32-bit，`MAX_READ_BURST_LENGTH=256`，`NUM_READ_OUTSTANDING=16`，L7500–L7585）
   与 `gmem1_m_axi`（写，64-bit，`MAX_WRITE_BURST_LENGTH=256`，L7586–L7615）是两个独立 adapter。
   读口内部按状态 mux（L9686–L9760），写口单独 mux（L9778–L9840），互不影响。
   ⇒ **「填充下一行」与「写回上一行」在引脚层面没有任何冲突，纯粹是控制逻辑没做。**

**唯一算得上「协议性」的依赖**只有两处：
- 填充必须在计算之前（同一 `line_buf`）；
- 写回必须在收尾之后（同一 `wbuf`），且 `S158/S166` 要等 `BVALID`（`L16565`/`L16569`）。

而这两条都可以用「双缓冲 + 一个块的延迟」消掉：填充第 n+1 行时计算第 n 行，写回第 n-1 行。
代价是 line_buf 复制一份（当前 BRAM_18K 已用 175/190，见 rpt:107，`Utilization 92%`——这是唯一硬约束，见 §8）。

---

## 5. 所有空等点（完整清单）

### 5.1 顶层 stall 信号 → 状态

`ap_ST_fsm_stateN_blk` 的非零项只有 42 个状态（其余 125 个 `= 1'b0`，L9055–L9352）：

| 状态 | 阻塞表达式（原文缩写） | 在等 | 行号 |
|---:|---|---|---:|
| 1 | `ap_start == 0` | AXI-Lite 启动 | L9362 |
| 23 | `ARREADY==0 \| LOOP_257_2_ap_done==0` | **AR** + 子流水 | L9376 |
| 32 | `LOOP_244_1_ap_done==0` | 子流水 | L9402 |
| 44 | `LOOP_286_6_ap_done==0` | 子流水 | L9434 |
| 52 | `LOOP_315_10_ap_done==0` | 子流水 | L9458 |
| 54 | `ap_block_state54_on_subcall_done` | 子流水 | L9468 |
| 61 | `ap_block_state61_io` = `ARREADY==0 & row_ok` | **AR** | L9490 |
| 62 | `LOOP_372_14_ap_done==0` | 子流水 | L9498 |
| 71 | `ap_block_state71_on_subcall_done` | 子流水 | L9524 |
| 78 | `ARREADY==0` | **AR** | L9544 |
| 87/88/89 | `RVALID==0` | **R** | L9570/L9578/L9586 |
| 95 | `ARREADY==0` | **AR** | L9551 |
| 103 | `RVALID==0` | **R** | L9062 |
| 110 | `ap_block_state110_on_subcall_done` | 子流水 | L9084 |
| 111 | `ARREADY==0` | **AR** | L9092 |
| 120–126 | `RVALID==0`（7 个状态各一组） | **R** | L9118–L9166 |
| 130/132 | `ap_block_state130/132_on_subcall_done` | 子流水 | L9182/L9192 |
| 135 | `LOOP_764_38_ap_done==0` | 子流水 | L9204 |
| 137 | `ap_block_state137_on_subcall_done` | 子流水 | L9214 |
| 139 | `LOOP_684_33_ap_done==0` | 子流水 | L9232 |
| 141 | `ap_block_state141_on_subcall_done` | 子流水 | L9236 |
| 143/145/146 | `LOOP_865_44/849_43/892_47_ap_done==0` | 子流水 | L9246/L9256/L9264 |
| 151 | `gmem1_0_AWREADY==0` | **AW** | L9282 |
| 153 | `LOOP_945_50_ap_done==0` | 子流水 | L9292 |
| 158 | `ap_block_state158` = `BVALID==0 & xn>0` | **B** | L9308 |
| 159 | `gmem1_0_AWREADY==0` | **AW** | L9316 |
| 161 | `LOOP_967_51_ap_done==0` | 子流水 | L9328 |
| 166 | `ap_block_state166` | **B** | L9344 |

### 5.2 各类等待的绝对拍数（可优化空间）

| 等待 | 出现位置 | 每块/每行拍数 | 能否并行化 |
|---|---|---|---|
| **AR→R（权重，慢路 1 字节）** | S94→S95→S96–102→S103 | **~11 拍/字节**，k=3 每 (oc,ic) 一组 13–14 拍 | ✅ 可以：一条 512-bit 的权重预取队列 + 双缓冲 `wgt*_buf` |
| **AR→R（权重，快路 32-bit）** | S108→S110 | `ocbl1·cin+14`，II=1 | ⚠️ 已是最优形态，但仍在关键路径上（应与上一块的收尾重叠） |
| **AR→R（填充行）** | S61→S63–69→S70/S71 | 7 拍硬延时 + `nwide/4` 拍流 | ✅ 7 拍纯浪费；且可与计算重叠（不同 buffer bank） |
| **AR→R（bias）** | S23→S24–26→S31/S32 | 3 拍纯浪费 + cout 拍 | ✅ 一次性，可忽略 |
| **W→B（写回）** | S153→S154–157→S158 | 4 拍硬延时 + B 往返 | ✅ **最大的可并行点**：B 可以等到下一块的 S151 之前再收 |
| **子流水 ap_done** | 见上表 | 由流水决定 | ⚠️ 收尾 `LOOP_730_36` 的 19 拍是最大头（见 §6） |
| **空转的 pad 循环** | S50–S54，每 (r, ic) 一次 | 每 r 约 `3·cin` 拍，`la=0` 时全空转 | ✅ 加一个 `if (la||rb) skip` 直接省掉 |
| **尾部补零的 i 循环** | S128/S132（16 lane）或 S128/S130（8 lane） | **32 拍/块**（本网络一个尾通道都没有，纯空转） | ✅ 加一个「`ocbl1>=16` 直接跳过」即可 |

### 5.3 「发完 AR 就去算别的」目前为什么做不到

- 读口只有一个 AR 通道（`gmem0_0_ARADDR` 由状态 mux，L9687–L9705），但**没有任何在途请求记录**：
  FSM 发完 AR 后必须原地等 R（S87/S103/S120）。
- `gmem0_m_axi` 内部虽然有 `CH0_NUM_READ_OUTSTANDING(16)` 的读 FIFO（L7596 区），
  但顶层只用了 `I_CH0_RVALID/RDATA`，没把「已发出的 AR 数」纳入调度。

### 5.4 pad 循环的浪费（具体数字）

`S50→S51→S52→S53→S54→S50`，每个 ic 一次。`wide1`（k=1, s=1）时 `pad=0` ⇒ `la=0`，
cpp:L318 的 `if (!wide1)` 让右补零也为空，但 **S50 的 `ic` 循环仍然跑 cin 次**。
cin=64、每 y 一行 ⇒ 每行白烧 `~3×64 = 192` 拍；一个 64×64 的层 = 4096 行 ⇒ **78 万拍 ≈ 5.2 ms/层**。

---

## 6. 19 拍/块的逐拍分解

数据来源：`S141` 的 stall 由 `LOOP_730_36_ap_done` 决定（L16561），该实例 csynth latency **min=max=21**
（rpt:54），而 cosim 反推的固定开销是 19（记录:104）。差值 2 拍来自 `ap_start_reg` 与 `ap_done`
在 S140/S141 两端的重叠。**按 19 拍分：**

| 拍 | 在干什么 | 证据 |
|---:|---|---|
| 1 | 取 `acc0[i]`：`sparsemux_33_4_26` 16:1 mux，4-bit 运行期选择子 `sel=trunc_ln730`（=`i[3:0]`） | `LOOP_730_36.v:450`–`L460`、`:745` |
| 2 | 取 `acc1[i]`：第二个 16:1 mux | `LOOP_730_36.v:513`–`L520`、`:1017`/`:1021` |
| 3 | `acc = acc0+acc1`；`v = acc + bias_buf[i]`（bias 是 BRAM 读，`bias_buf_address0=zext_ln733`） | `:1039` `v_fu_968_p2` |
| 4 | `v >>= shift`（桶形移位，`shift` 是运行期值） | `:1035` `v_3_fu_974_p2` |
| 5 | ReLU：`if (relu_on && v<0) v=0`（运行期 `empty` 位） | `:1037` `v_4_fu_991_p3` |
| 6 | `saturate_int8`：两个比较 + 选择（`icmp_ln19/ln20`、`v8_fu_1036_p3`） | `:1033` `v8_fu_1036_p3` |
| 7 | 写 `wbuf[i][x2]`：`i` 决定 16 个 bank 里的哪一个（**bank 号是运行期值**） | `wbuf_*_we1` |
| 8–19 | **重复 1–7 两次以上**：`i` 从 0 数到 15，`icmp_ln730` 拿 `ap_sig_allocacmp_i_2` 和 `5'd16` 比较，**每拍只推进一个 oc** | `:1009` `icmp_ln730_fu_729_p2` |

也就是说：**这 19 拍里没有一拍在做乘加，全是在为 16 个输出通道做「取数→移位→饱和→写缓冲」的串行收尾。**
`#pragma HLS UNROLL`（cpp:L729）没有生效——RTL 里保留了循环变量 `i_2` 和 35:1 mux，
这正是「HLS 把 acc0/acc1 当成可按运行期下标访问的寄存器堆」的典型症状。

> 对比：如果展开成 16 路并行，收尾应该只要 2–3 拍（加法 + 移位 + 饱和都是组合逻辑，wbuf 的 16 个 bank 可以同时写）。
> 单这一项就能把 cin=8 的块从 31 拍压到 15 拍（2 倍），cin=64 的块从 87 压到 70 拍（1.24 倍）。
> 全网 132 个 k=1 层受益最大。

**同源问题**（同样没展开，只是规模小）：
- `LOOP_819_41`（k=3 收尾，8 lane）：csynth latency `min=max=13`（rpt:52）= 8 + 5；
- `LOOP_849_43`（generic k=1）：`6`（rpt:56）；`LOOP_892_47`（generic k=5）：`26`（rpt:57）。

---

## 7. 手写替换的边界与 FSM 草案

### 7.1 哪些是「必须保留的协议」

| 必须 | 说明 | 参考 |
|---|---|---|
| s_axilite 寄存器映射 | H/W/Hout/Wout/cin/cout/ksize/stride/dil/pad/shift/relu_en/4 个指针 + `ap_return` | `ctrl_s_axi.v`、`L16590` `assign ap_return = retval_3_reg_3626` |
| `ap_ctrl_hs` 握手 | `ap_start/ap_done/ap_ready/ap_idle`；`ap_done` 必须在最后一次写回 **B 之后**才能拉高 | `L16109`–`L16113`、`L8062` |
| AXI4 读（gmem0） | AR 的地址/长度（**字节长度**语义，见 `gmem0_m_axi.v:418` `out_AXI_ARLEN = tmp_len; // Byte length`）、R 的 `RLAST` | L9686–L9760 |
| AXI4 写（gmem1） | AW 的长度是**元素数（8 B/元素）**；W 的 `WSTRB` 必须逐字节正确（尾部通道补零） | `gmem1_m_axi.v:480`；cpp:L631–L654 |
| 读/写各自独立的 outstanding 记账 | 否则 `BVALID` 会丢 | `ap_block_state158/166` |
| `line_buf` / `wbuf` 的真实 BRAM 结构 | 不能改成运行期 bank 号（会把 BRAM 复制成 336 块，见 cpp:L212–L227 的历史记录） | — |

### 7.2 哪些是 HLS 冗余（可以删）

| 冗余 | 规模 | 证据 |
|---|---|---|
| 41 个纯延时态 | 41 拍/块级 | §5.2 列表 |
| 3 态一组的 3 级乘法器等待（S3-5, S7-9, S12-14, S27-29, S41-42, S46-48, S73-74） | ~18 拍/行 | `grp_fu_4219/4232/4236` 是 3 级流水（`mul_29ns_32s_60_3_1`） |
| 每 (r, ic) 一次的 pad 循环（`la=0` 时全空） | `3·cin` 拍/行 | §5.4 |
| 尾部补零的 16 次 i 循环 | 32 拍/块 | `i_4` 清 0 在 `L8131`、自增在 `L8133`、退出 `L16840` |
| 收尾的 16 次 i 循环（**最大项**） | 19 拍/像素 | §6 |
| generic 路径逐 lane 的 5 态收尾 | 60–86 拍/像素 | S142–S150 |
| 权重慢路的 1 字节 AR | 11 拍/字节 | S92–S105 |
| S154–157 / S162–165 的 4 拍等 B | 8 拍/块 | L9308/L9344 |
| `icmp_ln474`/`icmp_ln437/439` 的运行期对齐分支 | 会让两条路都进综合 | S76/S75 |

### 7.3 建议的手写 FSM（12 态 + 4 组计数器）

设计目标：**每拍 16 个 MAC 不停**，填充/计算/写回用 ping-pong 缓冲重叠。

```
状态        行为                                              转移
IDLE      : ap_idle=1, 等 ap_start                          -> CFG
CFG       : 锁存 s_axilite, 算 span/bwcol/bw/la/lb/nwide     -> BIA
BIA       : 发 bias AR (1 拍), 同时把权重 AR 也发出去        -> RUN     ← 一次性
RUN       : 唯一的常驻状态（见下），每拍推进 pipeliner        RUN / BWAIT
BWAIT     : 等最后一次 BVALID, 然后 ap_done                  -> IDLE
```

`RUN` 里用 4 个独立的小控制器（不是一个大 FSM）：

| 单元 | 状态数 | 职责 | 与谁并行 |
|---|---:|---|---|
| **FILL** | 4 | `F_ROW`: 发下一行的 AR（`ARLEN=lb*2`）→ `F_STREAM`: 消费 RVALID 写 `line_buf[bank^1]` | 与 MAC/WB 完全并行（不同 bank） |
| **WGT** | 4 | 每个 ocb 边界：发 1 条长 AR（16·cin 字节）→ 流式写 `wgt1_buf` | 与 FILL 共用读口，需仲裁；与 MAC 并行 |
| **MAC** | 4 | `M_LOAD`(取 lane 权重) → `M_RUN`(cin 拍, II=1, **16 lane 全展开**) → `M_EPI`(**1 拍**，16 lane 并行 shift/relu/sat) | 收尾 1 拍是核心改动 |
| **WB** | 5 | `W_AW`(发 AW, AWLEN=xn) → `W_STREAM`(xn 拍 64-bit) → `W_B`(收 B) | 与 MAC 并行（不同 wbuf bank） |

**关键改动（按收益排序）：**

1. **把 16 路收尾真正展开**（消掉 §6 的 19 拍）→ 每像素从 `cin+23` 变 `cin+2`。
2. **`line_buf` 双缓冲**（fill 写 bank1、MAC 读 bank0）+ **`wbuf` 双缓冲**（MAC 写 bank1、WB 读 bank0）
   → 填充、计算、写回三段重叠。代价：`line_buf`（BRAM 163→~326）放不下 ⇒ 见 §8 的替代方案。
3. **权重装载改成「1 条 AR 覆盖整块」**（16·cin 字节，`ARLEN=16·cin`），删掉逐字节状态链。
4. **删掉 41 个纯延时态**，改成「请求队列 + 在途计数」：发 AR 时 `outstanding++`，收 R 时 `outstanding--`。
5. **pad 循环加 `if (la==0 && rb==0) skip`**。

**状态转移表草案（RUN 内的仲裁优先级）：**

| 当前 | 条件 | 下一 | 说明 |
|---|---|---|---|
| F_ROW | `buf_ready[bank^1]==0 && ar_free` | F_STREAM | 发 AR，记 outstanding |
| F_STREAM | `RVALID && !rlast` | F_STREAM | 1 beat/拍写 line_buf |
| F_STREAM | `rlast` | F_ROW | 换 bank，`buf_ready++` |
| M_LOAD | `buf_ready[bank]>0 && wgt_ready` | M_RUN | 锁存 acc=0 |
| M_RUN | `ic < cin` | M_RUN | 16 lane × 1 MAC/拍 |
| M_RUN | `ic == cin` | M_EPI | 1 拍并行收尾 |
| M_EPI | `wbuf_ready[bank^1]==0` | M_LOAD | 切 wbuf bank，`wb_pending++` |
| W_AW | `wb_pending>0 && aw_free` | W_STREAM | AWLEN=xn |
| W_STREAM | `px < xn` | W_STREAM | 64-bit/拍 |
| W_STREAM | `px == xn` | W_B | — |
| W_B | `BVALID && b_free` | W_AW | `b_free--` |

预期吞吐：`16 MAC/拍` 常数（只要 `cin ≥ 2` 且行缓冲能跟上），即 **2.4 GMAC/s @150 MHz**
（相对现在 0.93 GMAC/s 约 2.6×）。

---

## 8. 存疑清单（以及怎么验）

| # | 存疑点 | 影响 | 验证方法 |
|---:|---|---|---|
| 1 | S141 到底停 19 还是 21 拍 | 每像素 ±2 拍 | cosim 里对 `ap_CS_fsm_state141` 计数；或看 `LOOP_730_36_ap_done` 相对 `ap_start` 的延迟 |
| 2 | `LOOP_684_33`（k=1 计算）的精确 latency（csynth 报 `?`） | 每像素 ±2 拍 | 数该模块 `ap_enable_reg_pp0_iter0..4`（深度 5）+ `icmp_ln684` 的 trip=cin（`:1850`） |
| 3 | `empty_103_reg_7953` 的确切语义（我按「16 车道组选择位」处理） | 只影响 k=1 尾块分支 | 看 `L7953` 附近的赋值来源（S10） |
| 4 | `and_ln451` 的语义（我按「强制走 generic 路径」处理） | 影响 S76 的派发 | 追 `icmp_ln451_fu_6339_p2` 的两个操作数 |
| 5 | 填充 41% 这个历史数字是否还适用于本构建 | §3.3 的效率结论 | 需要一次 cosim 的分段计数（fill/compute/wb 各多少拍） |
| 6 | 手写 FSM 的 line_buf 双缓冲放不下（BRAM_18K 175/190 = 92%，rpt:107） | 决定第 2 条改动能否落地 | 双缓冲前先确认 `SPAN_MAX=7`、`BWCOL_MAX` 是否能降到 64 列（k=1 不需要 448 列的宽行） |

---

## 附：一条命令重放本文的关键证据

```bash
V=/data/work/hls_c8_csynth/r9_k1_l16/solution1/syn/verilog
# 167 个状态参数
sed -n '144,311p' $V/conv2d_engine.v
# 次态表
sed -n '15318,16115p' $V/conv2d_engine.v
# 42 个 stall 状态
sed -n '9055,9352p' $V/conv2d_engine.v
# 握手源
sed -n '16544,16590p' $V/conv2d_engine.v
# 哪个状态启动哪个流水
grep -n "_ap_start_reg <= 1'b1" $V/conv2d_engine.v
# 41 个纯延时态（只在 case 里出现的状态号）
python3 - <<'EOF'
import re,collections
L=open('/data/work/hls_c8_csynth/r9_k1_l16/solution1/syn/verilog/conv2d_engine.v').read().split('\n')
u=collections.defaultdict(list)
for i,l in enumerate(L):
    for m in re.finditer(r'ap_CS_fsm_state(\d+)\b',l): u[int(m.group(1))].append(i+1)
print([n for n in range(1,168) if not [p for p in u.get(n,[]) if not 15318<=p<=16116]])
EOF
# 16 车道收尾其实是 16 次迭代
grep -n "icmp_ln730_fu_729_p2\|tmp_s_fu_755_p35" $V/conv2d_engine_conv2d_engine_Pipeline_VITIS_LOOP_730_36.v
# 写回流水自己不发 AW；填流水自己不发 AR
grep -n "m_axi_gmem1_0_AWVALID = \|m_axi_gmem1_0_AWLEN = " $V/conv2d_engine_conv2d_engine_Pipeline_VITIS_LOOP_945_50.v
grep -n "m_axi_gmem0_0_ARVALID = " $V/conv2d_engine_conv2d_engine_Pipeline_VITIS_LOOP_364_13.v
```
