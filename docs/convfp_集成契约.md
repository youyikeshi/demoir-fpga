# convfp 集成契约 v1（确定位置、数据流与预算）

> 状态：**只写契约，不含 RTL 改动**。所有数字要么是 xsim 实测拍数（125 MHz 折算），
> 要么明确标「估」。已冻结的 core 逻辑（0 diff）不动。
> 上游文件：`FIX_core_line_v1.md`（功能定稿）、`PLAN_convfp_rest_v1.md`（BRAM/权重/golden 初稿）。

---

## 0. 一句话结论

$$\Rightarrow\ \textbf{convfp 现在的 DDR 形态不满足用户目标}：\text{它把 PS 的 } 1.15\ \text{s 换成 PL 的 } 0.46\ \text{s，}$$
$$\quad\ \text{但 }\textbf{DDR 流量一点没少}（\text{x 读 } 61.6\ \text{MB} + \text{y 写 } 4.2\ \text{MB/整网 4 层）}$$
$$\Rightarrow\ \textbf{要作为流水线一级}：\text{输入走「流式（模式 B）」、输出直接进下游 PL 单元}$$
$$\Rightarrow\ \text{好消息：}\textbf{convfp 的带宽需求很小}（\text{峰值 } 250\ \text{MB/s}、\text{平均 } \le 171\ \text{MB/s），$$
$$\quad\ \text{所以「吞掉 PS」在带宽上完全可行；}\textbf{真正的约束是 BRAM 和「每层一次 launch 的开销」}。$$

---

## 1. 实测基线（125 MHz，xc7z015）

| 层 | cin,cout,k | H×W | 输出 | MACs | 实测拍 | **时间** | x 读 | y 写 |
|---|---|---|---|---|---|---|---|---|
| conv_first | 12,48,5 | 128² | 786432 | 235.9 M | 31.38 M | **251.1 ms** | 26.5 MB | 3.1 MB |
| decoder_1 | 64,12,3 | 128² | 196608 | 113.2 M | 19.53 M | **156.2 ms** | 26.7 MB | 0.8 MB |
| decoder_2 | 64,12,3 | 64² | 49152 | 28.3 M | 4.92 M | **39.3 ms** | 6.7 MB | 0.2 MB |
| decoder_3 | 64,12,3 | 32² | 12288 | 7.1 M | 1.26 M | **10.1 ms** | 1.7 MB | 0.0 MB |
| **合计** | | | | **384.6 M** | **57.09 M** | **456.7 ms** | **61.6 MB** | **4.2 MB** |

$$\Rightarrow\ \text{有效吞吐 } \mathbf{0.84\ GMAC/s}（\text{上限 } 8\ \text{lane} \times 125\ \text{MHz} = 1.00）\Rightarrow \text{效率 } 84\%
$$\Rightarrow\ \text{与目标 } 0.45\ \text{s 吻合}（\text{本表 } 0.457\ \text{s}）\Rightarrow \text{PS } 1.15\ \text{s} \Rightarrow \text{省 } \sim 0.7\ \text{s} ✓$$

> 说明：`decoder_*` 的效率只有 58%，因为每层的 x 窗口是 `cin·k·L = 6528` 元素而 MAC 只有 `8·TW·K`，
> 装载与计算的比例被 `cin` 拉高。这是 §3 要解决的。

---

## 2. 取数模式：A / B / C（**推荐 B**）

| 模式 | 上游数据在哪 | 需要的硬件 | convfp 侧改动 | 结论 |
|---|---|---|---|---|
| **A. 全 DDR（现状）** | DRAM | 无 | 无 | ❌ 作为最终形态不可接受：每层一次 launch + 读回 DDR，DDR 流量没减少，只换了执行单元 |
| **B. 流式（推荐）** | 上游 PL 直出（AXI-Stream / 片上 FIFO） | 输入侧 2~4 KB FIFO + 反压 | 把 `tload` 的 DDR 读换成流式取数；`mem_rd_*` 口保留做旁路 | ✅ 可行性 = 本节的速率/反压论证 |
| **C. 片上缓冲（PL 乒乓）** | 上游写进 BRAM，convfp 读 BRAM | 大缓冲 | 多一套 buffer 管理 | ⚠ 只在「上游不能流式」时才用；BRAM 已经很紧（§3） |

### 2.1 模式 B 的速率需求（实测/推算）
$$\Rightarrow\ \text{core 消费 x 的方式：}\texttt{tload}\ \text{每 } 2\ \text{拍写 1 个 f32} \Rightarrow \textbf{峰值 } 2\ \text{B/拍} = \mathbf{250\ MB/s}\ (@125\ \text{MHz})$$
$$\Rightarrow\ \text{平均需求（\text{每层 x 读量} / \text{层时间}）：} 105.7\ \text{MB/s}\ (\text{conv\_first})、171.2\ (\text{dec\_1})、170.0\ (\text{dec\_2})、165.5\ (\text{dec\_3})$$
$$\Rightarrow\ \textbf{判据：上游只要能在「充数窗口」内持续给 } 2\ \text{B/拍}\ \text{即可}$$
$$\quad\ \text{充数窗口长度} = 2 \times \text{窗口元素数}： conv\_first\ 4320\ \text{拍},\ dec_*\ 13056\ \text{拍}$$
$$\Rightarrow\ \text{充数与计算的占空比：} conv\_first\ 4320/10215 = 42\%；\ dec_*\ 13056/19069 = 68\%$$

### 2.2 反压行为与缓冲深度（**这是 B 模式的真正约束**）
$$\Rightarrow\ \text{core 现在}\textbf{没有弹性缓冲}：\text{每 tile 的充数必须在 } \texttt{C\_TW}\ \text{处等完（} \texttt{tl\_busy}\ \text{落）}$$
$$\Rightarrow\ \text{上游抖动容忍度 = }\textbf{一个 tile 的计算时间}： conv\_first\ 10{,}215\ \text{拍} = \mathbf{82\ \mu s}；\ dec_*\ 19{,}069\ \text{拍} = \mathbf{153\ \mu s}$$
$$\Rightarrow\ \text{若上游停顿超过这个数} \Rightarrow \text{core 直接停（反压回上游）}$$

$$\Rightarrow\ \textbf{要给更大的抖动余量}：\text{每多 } 1\ \text{ms 需 } 250\ \text{MB/s} \times 1\ \text{ms} = \textbf{250 KB} \Rightarrow \text{BRAM 不可能}$$
$$\Rightarrow\ \textbf{所以结论是「率匹配」而不是「靠缓冲吃抖动」}：\text{加 } 2\sim4\ \text{KB FIFO（吸收 AXI 突发/仲裁间隙），}$$
$$\quad\ \text{上游必须承诺「在 } 13056\ \text{拍的窗口内平均 } \ge 2\ \text{B/拍」}；\text{做不到就按 §2.3 加行缓冲。}$$

### 2.3 若上游给不出 2 B/拍：需要多大缓冲
$$\Rightarrow\ \text{所需缓冲} \approx (\text{需求速率} - \text{上游速率}) \times \text{充数窗口}$$
$$\Rightarrow\ \text{例：上游只能给 } 1\ \text{B/拍} \Rightarrow \text{缺 } 1\ \text{B/拍} \times 13056 = 13\ \text{KB/tile}$$
$$\Rightarrow\ \text{即：}\textbf{tbuf 本身就是那个缓冲}（\text{它已经存了整窗 } 25.5\ \text{KB}）\Rightarrow \text{上游 } 1\ \text{B/拍 时 convfp 只慢 } 2\times$$
$$\Rightarrow\ \text{若上游更慢} \Rightarrow \text{按比例退化，不需要额外 BRAM（} \text{代价是时间}）。$$

---

## 3. W5 的精确 BRAM 账（**核心交付**）

### 3.1 现状（OOC 综合实测 24 BRAM36 = 108 KB）
| 缓冲 | 声明深度×宽度 | 位数 | BRAM36 | 备注 |
|---|---|---|---|---|
| `tbuf` | 6528 × 64 b（双半区，每半 3264 字） | 418 kbit | **12** | 按**最坏层**（cin=64,k=3）静态分配 |
| `wbank` | 8 × 1024 × 35 b | 287 kbit | **8** | 8 个 oc 的权重（解包态） |
| `obuf` | 256 × 32 b | 8 kbit | 1 | 一个 tile 行 |
| `line`/`wvec`/`bias` | 2×36×32 / 8×35 / 8×32 | 3 kbit | 0 | LUTRAM |
| **合计** | | | **24（108 KB）** | 与综合报告一致 |

**每层真实需要多少 tbuf**（这是省 BRAM 的全部空间来源）：

| 层 | 窗口元素 | 整窗 | 每半区字数 | 若按层定制 BRAM36 |
|---|---|---|---|---|
| conv_first | 2160 | 8.4 KB | 1080 | **4** |
| dec_1/2/3 | 6528 | 25.5 KB | 3264 | **12** |
| 最坏（cin=64,k=5，当前**不支持**） | 11520 | 45 KB | 5760 | 22 |

⇒ 静态分配只能按最坏 = 12。

### 3.2 三个可选方案（含代价）

| 方案 | tbuf | 合计 BRAM36 | x 流量 | 计算开销 | 改 RTL 面 |
|---|---|---|---|---|---|
| **现状** | 12 | **24（108 KB）** | 61.6 MB | — | — |
| **W5-a：k 行环形（只留 k 行，边流边算）** | 1 | **13（58 KB）** | **×4**（每个 sub-group 重扫一遍窗）⇒ 246 MB | 充数变连续，无充数窗口 | 大：tload/C_K 行推进重写 |
| **W5-b：按 ic 拆两趟（推荐）** | 6 | **18（81 KB）** | **不变** 61.6 MB | +1 次 pass 边界/tile（估 +1~2%） | 中：`tload` 加 pass 概念，`C_K` 的 gline 上界改两段 |
| **W5-c：TW 32→16** | 6 | **18（81 KB）** | **×1.11**（halo 20/16 vs 36/32）⇒ 68 MB | 每 tile 开销占比 +1.3% | 中：`sub` 降 1 bit、flush 128→64、`J`/`L` 跟着变 |

$$\Rightarrow\ \textbf{推荐 W5-b}：\text{把 } (\text{ic},\text{ky})\ \text{循环按 } \textbf{ic 前半/后半}\ \text{拆两趟，}$$
$$\quad\ \text{每趟只把一半的行装进 tbuf ⇒ 深度减半；}\textbf{累积器（lane 的 } \texttt{acc\_arr}\text{）跨趟保持} \Rightarrow \text{数值完全不变}$$
$$\Rightarrow\ \text{流量不变的原因：拆的是 } ic\ \text{，不是 } (\text{ic,ky})\ \text{混拆 —— 每个 ic 的 k 行窗口 } y\ \text{范围相同，}$$
$$\quad\ \text{所以两趟各读一半行、合计仍是整窗，}\textbf{没有重复读}$$
$$\Rightarrow\ \text{代价：每 tile 多一次 pass 边界（重装 } tbuf \text{ 前半 + 重新 prime 一次 line 双缓冲）≈ 300~500 拍}$$
$$\quad\ \text{占每 tile 10215 / 19069 拍的 } 3\%\ /\ 2\% \Rightarrow \textbf{总时间 +2~3\%}$$

$$\Rightarrow\ \text{若还差 3 个 BRAM36，可叠加 W5-b + W5-c？}\ \textbf{不建议}（两处都动行推进，风险叠加）。$$

### 3.3 W5-b 的改动面（估：4 处，中风险）
1. `tload`：`fidx` 的遍历上界从 `cin·k·L` 改成「本 pass 的 `ic` 区间 × k × L」；`half_base` 逻辑不变。
2. `fconv_core` 的 tile 循环：一个 tile 内加 `pass`（0/1）两趟，`C_TW` 处等第二趟充数；`gline` 上界分两段。
3. `lload`：`gline` 索引用 `(ic - ic_base)`（相对化）。**不动**数据格式。
4. lane/累加：**完全不动**（这是 0 diff 的硬资产）。

⚠ 风险点：`pass` 边界上 line 双缓冲的 `line_sel/ll_sel` 必须复位到已知态（现在每个 sub-group 本来就复位，
所以复用同一套 `C_PRE` 即可）。

---

## 4. 权重侧

### 4.1 容量包线（已加硬报错）
$$\Rightarrow\ \texttt{wbank}\ \text{深度 } 1024 \Rightarrow \textbf{硬约束 } cin\cdot k^2 \le 1024（\texttt{err\_code=3}，已落地）$$
$$\Rightarrow\ \text{真实四层：} conv\_first\ 300、dec_*\ 576 \Rightarrow \textbf{都在包线内} ✓$$
$$\Rightarrow\ \text{融合后若 PS 侧算子合并改通道数：只要 } k=3\ \text{时 } cin \le 113、k=5\ \text{时 } cin \le 40\ \text{即安全}$$
$$\quad\ （\text{本网最大 } cin=64\ \text{且只出现在 } k=3\ \text{层}）\Rightarrow \textbf{当前包线够用}$$

### 4.2 若将来超包线：分块重载的代价
$$\Rightarrow\ \text{把 } K\ \text{拆成 } \lceil K/1024 \rceil\ \text{块，每块重读一遍 x 窗口}$$
$$\Rightarrow\ \text{代价 = x 流量 } \times\ \text{块数}；\text{例 } K=1600 \Rightarrow 2\ \text{块} \Rightarrow x\ \text{翻倍}$$
$$\Rightarrow\ \text{每层多多少：}dec\_1\ \text{类层 } 26.7\ \text{MB} \Rightarrow 53.4\ \text{MB}，\text{按 } 171\ \text{MB/s} \Rightarrow \textbf{+78 ms/层}$$
$$\Rightarrow\ \textbf{所以不推荐靠分块救超大 } K；\text{真要支持就把 wbank 深度按层配置（} 8 \times 1600 \times 35\ \text{b} = 12\ \text{BRAM36）。}$$

### 4.3 权重的搬运量（很小，不是瓶颈）
$$\Rightarrow\ \text{每层 DMA} = (cout/8)\ \text{次} \times 8 \times K \times 4\ \text{B}$$
$$\Rightarrow\ conv\_first: 6 \times 9.6\ \text{KB} = 57.6\ \text{KB}；dec\_*: 2 \times 18.4\ \text{KB} = 36.9\ \text{KB}$$
$$\Rightarrow\ \text{平均带宽：} 0.23\sim3.65\ \text{MB/s}（\text{dec\_3 最短，故最高}）\Rightarrow \textbf{可忽略}$$

---

## 5. 峰值带宽预算（判据 ≤ 700 MB/s）

| 通路 | 峰值 | 平均（4 层合计） | 说明 |
|---|---|---|---|
| 读 x（tbuf 充数） | **2 B/拍 = 250 MB/s** | 61.6 MB / 0.457 s = **135 MB/s** | 只在充数窗口内爆发（42~68% 占空） |
| 读 w | 突发（每 group 9.6~18.4 KB） | < 4 MB/s | 可忽略 |
| 写 y | 8 B/拍 = 500 MB/s（flush 时） | 4.2 MB / 0.457 s = **9 MB/s** | flush 只占 ~1% 时间 |
| **convfp 合计峰值** | **≈ 250 MB/s**（读满时 y 基本不写） | **≈ 145 MB/s** | |
| 判据 700 MB/s | **占 36%** | 占 21% | ✅ 有余量 |

$$\Rightarrow\ \text{与二值链路叠加：}\text{两单元在当前调度里是}\textbf{层间串行}（\text{不重叠}）\Rightarrow \text{不叠加}$$
$$\Rightarrow\ \text{若将来做「层间流水」让两者同时跑} \Rightarrow \text{峰值 } 250 + \text{ bin}\ \text{的峰值}；\text{若 } >700\ \text{则必须：}$$
$$\quad\ \text{① 把 convfp 的充数速率限到 } 1\ \text{B/拍（代价 convfp 慢 2×）}；\text{或 ② 给 convfp 低优先级 + 内部 FIFO 吸收。}$$

---

## 6. 模式 B 的接口契约（给 BD/驱动用）

```
convfp_axi_wrap
  AXI4-Lite (PS 配置)       : 1 次写描述符（base_w/base_b/base_y + 形状 + relu/bias_en）
  AXI4-Stream slave  (x)    : 64-bit，tvalid/tready；tready 仅在 tbuf 充数窗口内拉高
                              ★ 顺序契约：ic 主序 → (oy 带) → ix；每行 L 个元素
  AXI4-Stream master (y)    : 64-bit（2 个 f32/beat），tlast = 每个 tile 行结束
  AXI4 master (w/b)         : 1 个只读通道，突发长度 ≥ 8（每 oc 连续 K 字）
```
$$\Rightarrow\ \textbf{x 流式顺序必须与 } \texttt{tload}\ \text{的遍历一致}（\texttt{ic} \to \texttt{ky} \to \texttt{col}），\text{否则要加一个换序缓冲}$$
$$\Rightarrow\ \text{若上游给的是「行带」顺序（}y \text{ 主序）} \Rightarrow \text{需要 } k\ \text{行 × }L\ \text{ 的转置缓冲（估 1 个 BRAM36）}$$

---

## 7. 给用户决策的三条

1. **数据流位置**：convfp 放在 **decoder_3 之后 / 上采样之前**（它是 4 个 convfp 里的最后一个），
   这样 y 直接进 interp/e1e2 的输入缓冲，不需要落 DDR ⇒ 省 4.2 MB 写 + 同量读。
   `⚠ 估`：多层串联（把 4 个 convfp 像 RDB 那样串起来）能进一步省 3 次 DDR 往返，但要改层调度 —— 属于 V2。
2. **BRAM**：当前 24 → **W5-b 后 18**（81 KB），把 271 KB 的余量从 27 KB 抬到 **~68 KB**，
   这是「装得下 PS 侧算子」的前提。**W5-b 与 V2 行带融合必须是同一批**（两者都动 tile/pass 调度）。
3. **带宽**：convfp 只吃 **250 MB/s 峰值**，远低于 700 的判据 ⇒ **不需要为带宽做取舍**，
   约束只有 BRAM（所以 W5-b 是硬前置）。


---

## 8. ★ 更正：W5-b（按 ic 拆两趟）**实测不可行**，真正可行的是 W5-c

### 8.1 为什么 W5-b 不成立（先证明，再实测）
$$\Rightarrow\ 	ext{每个 } 	extbf{sub-group}\ 	ext{都要把整窗（} cin\cdot k\ 	ext{ 行）走一遍}$$
$$\quad\ （	exttt{C\_DRAIN}\Rightarrow	exttt{C\_PRE}\ 	ext{把 } 	exttt{gline} 	ext{ 清 0、} 	exttt{ll\_start} 	ext{ 重来；4 个 sub-group 各走一遍）}$$
$$\Rightarrow\ 	ext{所以}	extbf{整窗必须常驻} \Rightarrow\ 	ext{拆成两个 pass 后，}	extbf{两个 pass 的数据必须同时对 4 个 sub 都可用}$$
$$\Rightarrow\ 	ext{但 pass1 的装载只能在 pass0 算完之后才开始（同一个半区被覆写）}$$
$$\Rightarrow\ 	ext{pass1 必须在}	extbf{单个 sub 的 pass0 计算}	ext{内装完：}$$

| 用例 | pass1 装载(拍) | 单个 sub 的 pass0 计算(拍) | 比值 |
|---|---|---|---|
| tiny | 408 | 144 | **2.83× 来不及** |
| c12o12 | 1224 | 432 | **2.83×** |
| decoder_1 | 6528 | 2304 | **2.83×** |
| conv_first | 2160 | 1200 | **1.80×** |

$$\Rightarrow\ 	extbf{实做验证}：	ext{按计划改完（tload 的 } ic 	ext{ 区间 / core 的 } pass+	ext{三缓冲预取链 / lload 相对化）后}$$
$$\quad\ 	ext{编译通过、}x\ 	ext{读次数不变（tiny } 13056	ext{、c12o12 } 78336	ext{，与理论完全一致）、拍数还略降（} 56882\ vs\ 57290	ext{）}$$
$$\quad\ 	extbf{但数据是错的}：776/5120、1896/12288\ 	ext{—— 正是 pass1 的 line 用了还没装好的 tbuf}$$
$$\Rightarrow\ 	ext{已回滚到 0 diff 的参考版本（复跑 } 0/5120、0/12288\ 	ext{，拍数一致）}$$

$$\Rightarrow\ 	extbf{结论：tbuf 必须 } \ge 1\ 	ext{整窗；tile 双缓冲又要 2 整窗} \Rightarrow 	extbf{拆 ic 省不了 BRAM}，$$
$$\quad\ 	ext{除非改「sub-group 循环」本身（见 §8.3）}$$

### 8.2 W5-c（TW 32→16）—— 唯一「改得动、真能到 18」的方案
| 项 | 现状 | W5-c |
|---|---|---|
| tbuf 窗口 | 2 × 3264 字 = 12 BRAM36 | 2 × 1728 字 = **6** |
| **合计** | 24（108 KB） | **18（81 KB）** |
| x 流量 | 61.6 MB | **68.5 MB（+11.1%）** |
| 拍数 | 57.09 M | **≈ +1.3%** |
| 改动面 | — | `sub` 2→1 bit、`cflush==127`→63、`cfg_w>>5`→`>>4`、`cfg_w[4:0]`→`[3:0]`、`DEPTHW_MAX` 1632→1728 |
| 碰 lane | — | **不碰** ✓ |

$$\Rightarrow\ 	ext{+11% 的来源是 halo：}(TW+k-1)/TW = 36/32 \Rightarrow 20/16\ 	ext{，比值 } 1.25/1.125 = 1.111$$
$$\Rightarrow\ 	ext{但这一条}	extbf{违反门禁②「x 流量必须精确不变」} \Rightarrow 	extbf{需要你拍板}$$
$$\Rightarrow\ 	ext{代价换算：4 层多 6.9 MB，峰值仍 } \le 250\ 	ext{MB/s}（700 的 36\%）\Rightarrow 	ext{带宽上无风险}$$

### 8.3 W5-d（真·不动流量的唯一路）—— 但必须动环
$$\Rightarrow\ 	ext{把 } 	extbf{sub-group 循环放到内层}：	ext{每个 gline 依次喂 4 个 sub（} k\cdot 8\ 	ext{ 拍/组，是 8 的倍数）}$$
$$\Rightarrow\ 	ext{则 tbuf 只需 } 	extbf{1 行}（L\ 	ext{元素}）\Rightarrow pprox 1\ 	ext{BRAM36}；	ext{流量不变；无装载气泡}$$
$$\Rightarrow\ 	extbf{代价}：	ext{每个 lane 要 } 4	imes 8 = 32\ 	ext{个累加槽（现在 8 个）}，	ext{且 8 拍环在 4 个 sub 之间交替} \Rightarrow 	extbf{必须动 lane}$$
$$\Rightarrow\ 	ext{这违反「lane 一点不动」的边界} \Rightarrow 	ext{属于下一轮的架构决定，本轮不做}$$

### 8.4 我的建议
$$\Rightarrow\ 	ext{选 	extbf{W5-c}：+11% 流量（6.9 MB/4 层）换 41 KB BRAM（27 KB 余量 → 68 KB），}$$
$$\quad\ 	ext{不碰 lane、改动面 6 处、0 diff 风险低}$$
$$\Rightarrow\ 	ext{若 }+11\%\ 	ext{不可接受} \Rightarrow 	ext{只能走 W5-d（动环、动 lane）} \Rightarrow 	ext{那是下一轮}


---

## 9. W5 全线作废（2026-10-31 19:40）

| 方案 | 裁定 | 依据 |
|---|---|---|
| W5-a（k 行环形、边流边算） | ❌ 不做 | x 流量 ×4（61.6 → 246 MB），融合后是硬成本 |
| W5-b（按 ic 拆两趟） | ❌ 不做 | **架构上不可能**：§8.1 的 2.83× 证明 + 实测 776/5120 |
| W5-c（TW 32→16） | ❌ 不做 | 能到 18 BRAM36，但 **+11.1% 流量换 6 块不需要的 BRAM** |
| W5-d（sub 循环放内层） | ❌ 不做 | 能到 ~13 BRAM36、流量不变，但**必须动 lane/环**，换的也是不需要的 BRAM |

**为什么不需要（BRAM 账重算）**
| 配置 | BRAM36 | /95 |
|---|---:|---:|
| v39 现状 | 35 | 37% |
| \+ floatconv（保持 24） | 59 | **62%** |
| \+ PS 吞并（+6~8） | 66~67 | **69~71%** |
| \+ V2 行带（净 +0.9） | 67~68 | **71~72%** |
⇒ **全部装完 71~72%** ⇒ BROM 不是墙；**LUT 已经 87~90%，那才是墙**。

**永久保留的判据（可直接复用）**
* `XREADS`（TB 里数 `mem_rd_en && tl_busy`）：**当前设计对每个窗元素恰好读一次/tile/group**。
  实测 tiny 13,056 = 32×1×408、c12o12 78,336 = 32×2×1,224，与理论逐位相符。
  ⇒ 任何改动只要让 `XREADS` 上涨 = 引入了重复读 ⇒ **拿它当门禁**。
* §8.1 的 2.83× 表格 = 「tbuf 不能小于 1 整窗」的证明。


---

## 10. lane 数可裁（N=4/8）：两档实测（2026-10-31 20:20）

### 10.1 参数化做法（N 默认 8，8-lane 行为逐位不变）
* `fconv_core` 原有 `parameter int N = 8` 但**全篇硬编码 8**；本轮回填为真正可裁：
  `LG=(N==4)?2:3`（drain 索引 `cdrain[LG+2:3]`、flush `cflush[LG+3:4]`）、
  `NACC=N*8`（acc_l 深度）、`NOB=N*TW`（obuf 深度）、`ngroups=(cout+N-1)>>LG`、`oc_base=(g+1)<<LG`、
  wbank/wvec 深度 `[0:N-1]`、generate 循环 `< N`。
* TB 用 `` `ifdef N4 `` 选 `N=4`；`run_queue.sh` 加 `n4:<case>` 规格（`xvlog -d N4`）。
* **8-lane 回归**：tiny 0/5120、c12o12 0/12288，**拍数与原版完全一致（57290 / 267602），XREADS 一致**
  ⇒ 参数化没有改变 8-lane 的任何行为 ✓

### 10.2 两档实测表
| 配置 | LUT | FF | BRAM36 | DSP | Latch | WNS@125MHz | 4 层时间 |
|---|---:|---:|---:|---:|---:|---:|---:|
| **8 lane** | 16173 | 12549 | 24 | 24 | 0 | −1.362 | **456.7 ms** |
| **4 lane** | **9887** | **7844** | **20** | **18** | **0** | **−1.101** | **807.5 ms** |
| 差 | **−6286（−38.9%）** | −4705 | −4 | −6 | 0 | +0.261 | **×1.77（+353 ms）** |

$$\Rightarrow\ 	ext{4 lane 的真实倍率是 } \mathbf{1.77	imes}\ 	ext{而不是 } 2	imes：	ext{因为 } cout=12\ 	ext{的层在 8 lane 下只用了 } 12/16=75\%\ 	ext{ lane}$$
$$\quad\ （8\ 	ext{组}= 8+4，	ext{第二组只用 4 个 lane），4\ lane 下 } 3\ 	ext{组全满} \Rightarrow 	ext{倍率 } 1.485\ 	ext{而不是 } 2$$
$$\Rightarrow\ cout=48\ 	ext{的 conv\_first 仍是 } 2	imes（48/8=6\ 	ext{组 vs } 48/4=12\ 	ext{组，都满）}$$

| 层 | 8 lane | 4 lane | 倍率 | 来源 |
|---|---:|---:|---:|---|
| conv_first (cout=48) | 251.1 ms | 502.1 ms | 2.000 | 外推（模型经 dec_2/dec_3 验证） |
| decoder_1 (cout=12) | 156.2 ms | 234.3 ms | 1.500 | 外推 |
| decoder_2 (cout=12) | 39.3 ms | **58.4 ms** | **1.485** | **实测 7,299,146 拍** |
| decoder_3 (cout=12) | 10.1 ms | **15.0 ms** | **1.485** | **实测 1,874,954 拍** |

### 10.3 取舍表（给架构层）
| 选项 | LUT | 整网 convfp 时间 | 相对 8lane 的整网代价 |
|---|---:|---:|---|
| 8 lane | 16173 | 0.457 s | 基线（PS 1.15 → PL 0.457，省 0.69 s） |
| **4 lane** | **9887** | **0.810 s** | 	ext{省 } 0.34\ 	ext{s（少省 } 0.35\ 	ext{s）} |
| 砍掉 convfp（全留 PS） | 0 | 1.15 s（PS） | 省 0 |

$$\Rightarrow\ 	ext{4 lane 用 } \mathbf{353\ ms}\ 	ext{换 } \mathbf{6286\ LUT}（	ext{LUT 预算的 } 13.6\%）$$
$$\Rightarrow\ 	ext{整网时间 }	imes 1.05（	ext{若整网 } 8.4\ s \Rightarrow 8.75\ s）$$
$$\Rightarrow\ 	ext{结论：LUT 吃紧时，4 lane 是}	extbf{便宜}的降级（+5\% 时间换 6.3k LUT），	ext{比砍掉 convfp 好得多（后者丢 0.69 s）}$$

### 10.4 注意
* 4 lane 下 `ngroups` 翻倍 ⇒ **x 流量翻倍**（dec_*：1.485×、conv_first：2×，整网 61.6 → ~108 MB）；
  峰值仍是 2 B/拍（250 MB/s），平均升到 ~270 MB/s ⇒ **仍在 700 判据内**，但融合时要计入。
* 4 lane 的 wload 仍按 8 个 oc 取数（只写前 4 个 bank）⇒ 权重搬运多一倍（每层 ~0.7 → 1.4 ms，可忽略）。


---

## 11. 流式包装（模式 B）落地状态：①②过，③还剩一个 bug

### 11.1 交付物
* `rtl/fconv_stream_wrap.v`：包装 core，`stream_mode` 一位切换
  * **模式 A**：DDR 端口全透传（与现状逐字节等价）
  * **模式 B**：x 从 `s_axis_x`（64-bit beat）流入片内 FIFO；y 从 `m_axis_y` 流出（含地址 sideband）
  * 反压 = **ce 门控**（core 无逐拍 ready）：FIFO 空/输出堵 → 拉低 ce，core 整体冻结
  * x FIFO `512×64b = 4 KB`；y FIFO `16×96b = 192 B`（地址+数据）
* `tb/tb_stream_wrap.v`：一个 case 跑 **A / B / C(上游减半) / D(人为冻 3 拍)** 四次，逐字节比

### 11.2 实测（tiny 32², cin=4 cout=5 k=3, ny=5120）
| 门禁 | 判据 | 结果 |
|---|---|---|
| ① 模式 B vs 模式 A | 逐字节 0 diff | **0 / 5120 ✅** |
| ② 吞吐 | 模式 B 拍数 = 模式 A | **57290 = 57290（ratio 1.000）✅** |
| ③ 上游减半（1 beat/4 拍） | 仍 0 diff，时间约 2× | **56 / 5120 ❌ 拍数只 1.018×** |
| ④ 面积 | 整包 LUT 增量 ≤ 800 | 未测（禁综合）→ 结构估 ~200 LUT + 1 BRAM36 |
| （附）ce 冻结是否透明 | 人为冻 3 拍仍 0 diff | **0 diff ✅**（证明反压机制本身安全）|

### 11.3 ★ ③ 的失败：**错误是按列段连续的**，指向"流的冷启动错位"
把限速做成可调周期后测三档：

| 上游节奏 | stall 拍数 | 错数 |
|---|---:|---:|
| 每 2 拍 1 beat | 0 | **0** |
| 每 4 拍 1 beat | 1,028 | 56 |
| 每 8 拍 1 beat | 49,828 | 846 |

错位的**空间形状**（period 8 那档）：
```
oy=0: ox 0..15  错
oy=1: ox 8..15  错
oy=2: ox 8..11  错
```
$$\Rightarrow\ 	ext{这不是"随机一个元素错"，而是}	extbf{一段连续列}（ox 8..15 = sub-group 1）	ext{错}$$
$$\Rightarrow\ 	ext{tload 的 } xidx\ 	ext{是按 } (ic,ky,col)\ 	ext{现算的（不是计数器），所以"丢/重一拍"只会错一个元素、不会整体平移}$$
$$\Rightarrow\ 	ext{现在错的是}	extbf{一整段列} \Rightarrow 	ext{只能是 } tbuf/line\ 	ext{里那一段元素同时是错的}$$
$$\Rightarrow\ 	extbf{最可能就是"流的冷启动"}：	ext{饥饿解除后头几拍被重复捕获或漏捕获，}$$
$$\quad\ 	ext{而且只影响流最开始的那一小段（正好落在第一个 tile 的前几行）}$$

### 11.3b 已排除的三件事
$$\Rightarrow\ 	ext{失败集中在}	extbf{每个 oc 的第一行前 16 像素}（	ext{即第一 tile 的第一批 flush beat}）$$
$$\Rightarrow\ 	ext{已排除：① core 的 ce 冻结不安全 —— GATE4 冻 3 拍仍 0 diff}；$$
$$\quad\ 	ext{② y 流错位 —— 流的 addr/data 与 core 的写口	extbf{逐拍完全相同}；}$$
$$\quad\ 	ext{③ 生产者顺序不确定 —— 位置推进与 throttle 无关（只影响相位）}$$
$$\Rightarrow\ 	ext{剩余嫌疑：包装里 } 	exttt{rd\_x\_q}/	exttt{xf\_pop} 	ext{ 的	extbf{握手在"冻结解除那一拍"}的时序}$$
$$\quad\ （	ext{读提交拍与被采样拍差 1 拍，冻结释放时可能重复/漏取一拍）}$$
$$\Rightarrow\ 	extbf{状态：未修完}。	ext{修法是把它做成"读提交 → 弹 FIFO → 数据寄存器"三段明确握手，}$$
$$\quad\ 	ext{或用 skid buffer 把 core 侧读口做成真正 1 拍延迟的 valid/data。}$$

### 11.4 带宽口径更正（重要）
$$\Rightarrow\ 	ext{core 的读口是 }\mathbf{64\ bit}	ext{：一次读拉 8 B，每 2 拍一次}$$
$$\Rightarrow\ 	ext{所以	extbf{总线峰值 } 4 B/拍 = 500 MB/s}（	ext{有效数据 } 2 B/拍 = 250 MB/s）$$
$$\Rightarrow\ 	ext{此前 §5 写的"峰值 250 MB/s"是}	extbf{有效数据}	ext{口径，总线口径应为 500 MB/s}$$
$$\quad\ \Rightarrow\ 	ext{融合时按 500 MB/s 的突发峰值做仲裁（平均仍是 } \le 170\ 	ext{MB/s）}$$


---

## 12. skid buffer 改造后的实测（③ 仍未过，但排除面又扩大一圈）

### 12.1 改造内容（按批准的三步）
* 读请求只在 `skid_valid==0 && fifo_cnt!=0` 时接单；接单当拍把 FIFO 头搬进 skid 并弹 FIFO（**与 `core_ce` 解耦**）
* 采样拍由 `skid_sample`（接单的下一拍）决定，不再用 `core_ce` 反推
* 删除旧的 `rd_is_x_q/rd_x_q` 路径

### 12.2 结果：三档错数**逐位不变**
| 档 | 改前 | 改后 |
|---|---:|---:|
| period 2 | 0 | **0** |
| period 4 | 56 | **56** |
| period 8 | 846 | **846** |
| ① B vs A / ② 拍数 | 0 / 57290 | **0 / 57290 ✅** |

### 12.3 三个新的硬事实
$$\Rightarrow\ 	extbf{FIFO 顺序完整性}：	ext{TB 影子队列逐拍比"弹出 vs 推入"}=\mathbf{0\ bad/129{,}200\ pops}$$
$$\quad\ \Rightarrow\ 	ext{core 收到的流 = 生产者推入的流，同序同值}$$
$$\Rightarrow\ 	extbf{冷启动专项}：	ext{reset 后空置 FIFO } N=1,2,4,8,60\ 	ext{拍}\Rightarrow \mathbf{全 0\ diff}$$
$$\quad\ （512	imes64b\ 	ext{的 FIFO 在 wload 阶段已填满，人为冷启动被吸收}）$$
$$\Rightarrow\ 	extbf{饥饿深度}：	ext{period4 } 480\ 	ext{事件/最长 } 70\ 	ext{拍}；	ext{period8 } 8{,}204\ 	ext{事件/最长 } \mathbf{210}\ 	ext{拍}$$
$$\quad\ \Rightarrow 210 \ll 512 \Rightarrow 	extbf{4 KB FIFO 不缺深}（	ext{饥饿来自上游速率}）$$

### 12.4 结论与下一步
$$\Rightarrow\ 	ext{同样的输入流 + 外部 } ce\ 	ext{长冻 0 diff + FIFO 不缺深，但 starvation 档仍错}$$
$$\Rightarrow\ \Rightarrow\ 	extbf{存在一个"只在 starvation 冻结下出现"的内部隐患}$$
$$\Rightarrow\ 	ext{两者唯一差别：starvation 冻结时 FIFO 仍在被填，且冻结边沿由 core 自己的读请求组合决定}$$
$$\Rightarrow\ 	ext{嫌疑：}	exttt{tbuf\_rdata <= tbuf[tbuf\_raddr]}\ 	ext{是}	extbf{未加 } ce 	ext{ 门控的读寄存器}	ext{（地址被冻结、但读动作每拍都发生）}$$
$$\Rightarrow\ 	extbf{下一步（一次就能分叉）}：period-2 与 period-8 两个 run 结束后 dump }	exttt{tbuf}\ 	ext{逐字比}$$
$$\quad\ 	ext{tbuf 不同} \Rightarrow 	ext{问题在 tload 写入侧；tbuf 相同} \Rightarrow 	ext{问题在 lload/lane 侧}$$


---

## 13. ★ ③ 的根因与修复：`tbuf_rdata` 是「未加 ce 门控的读寄存器」

### 13.1 根因
```verilog
// 修复前（fconv_core.v）
always_ff @(posedge clk) begin
    if (tl_we) ... tbuf[...] <= ...;
    tbuf_rdata <= tbuf[tbuf_raddr];      // ← 每拍都更新，只有地址被冻结
end
```
$$\Rightarrow\ 	exttt{tbuf_rdata}\ 	ext{在}	extbf{冻结期间仍每拍刷新}	ext{，而 } 	exttt{tbuf\_raddr}\ 	ext{来自被冻结的 lload}$$
$$\Rightarrow\ 	ext{但同一 }	exttt{always\_ff}\ 	ext{块里还做 tbuf 的写入} \Rightarrow 	ext{读值对"写生效顺序"敏感}$$
$$\quad\ \Rightarrow\ 	ext{冻结把}	extbf{读/写的相对时隙}	ext{打乱} \Rightarrow 	ext{采到的是"新写值"而不是"该拍该有的值"}$$
$$\Rightarrow\ 	extbf{为什么外部 } ce 	ext{ 长冻复现不出来}：	ext{外部冻结时 tload 也停、tbuf 内容不变，读值恒定 ⇒ 无害}$$
$$\Rightarrow\ 	extbf{为什么只有 starvation 冻结错}：	ext{冻结边沿由 core 自己的读请求组合决定，读写时隙被打乱}$$

### 13.2 修复（一行）
```verilog
if (ce) tbuf_rdata <= tbuf[tbuf_raddr];   // ★ 冻结期间读寄存器不更新
```
$$\Rightarrow\ 	extbf{拍数完全不变}（	ext{正常跑 } ce\equiv1，	ext{行为与原版逐拍一致）$$
$$\Rightarrow\ 	ext{8-lane 回归：tiny 0/5120、c12o12 0/12288，	extbf{拍数与原版逐拍相同（57290 / 267602）} ✓$$

### 13.3 修复后的完整矩阵（③ 全 0）
| 项 | 修复前 | 修复后 |
|---|---|---|
| ① 模式 B vs 模式 A（逐字节） | 0 | **0 ✅** |
| ② 拍数 B vs A | 57290 = 57290 | **57290 = 57290 ✅** |
| ③ period 2 / 4 / 8 | 0 / 56 / 846 | **0 / 0 / 0 ✅** |
| 冷启动 N=1,2,4,8,60 | 0 | **0 ✅** |
| FIFO 弹出=推入 | 0 bad | **0 bad / 129,200 ✅** |
| 外部 ce 长冻（A/B 各 60 拍） | 0 | **0 ✅** |
| 8-lane 功能回归 | 0 | **0（拍数不变）✅** |

### 13.4 常驻回归（防止将来改深度/加缓冲又撞）
`run_queue.sh` 新增 `stream:<case>` 规格：一个 case 自动跑
**A / B / period-2 / period-4 / period-8 / A+冻60 / B+冻60 / 冷启动×5**，一次打印全套判据。
