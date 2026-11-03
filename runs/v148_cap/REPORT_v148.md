# T-148 · 256² 行带融合：容量-收益模型

> 只读 + 纯 CPU。未上板、未 ssh、未起 Vivado/xsim、未改任何 RTL/PS。
> 全部数字由同目录 `cap_model.py` 生成（`python3 runs/v148_cap/cap_model.py`），原始输出见 `model_output.md`。
> 数据源：`runs/board_t104/op_inventory_64.txt`（395 op 真实形状）、`runs/m56_segments/segments.json`（223 段权威分段 + v5 真实字节）、
> `docs/PLAN_1second.md:3458/3474-3478/5754`、`runs/v140_fuse/REPORT_v140.md:285-287`、`worktrees/T-142-fuse/rtl_v50/lane16/bin_band_buf.v:126-134`。

---

## 0. 结论（先给答案）

$$\Rightarrow\ \textbf{按字节账判：256² 融合不值得做。}\text{全设计空间最大净省 } \mathbf{+5.7\ MB}=+0.025\ \text{s（母口径）/+0.022\ s（v5 真实口径）}$$
$$\quad\ \text{占 PL 3.061 s 的 } \mathbf{0.8\%}；\text{而扩组要把行数从 10 降到 5，halo 重读从 20\% 涨到 40\%}$$
$$\Rightarrow\ \textbf{唯一有量级的收益不在字节上}：\text{conv→cat 段（84 条、全 } k{=}3\text{）把 pack 的 DDR 读搬到片上，上限 } \mathbf{0.26\ s}$$
$$\quad\ \text{但那需要带内多级串联（}\mathtt{band\_seq}\text{，PLAN:6994 记 1.5~3 天 + TB 1~2 天），且这 0.26 s 是**上限估计、无板级证据**}$$
$$\Rightarrow\ \textbf{推荐设计点（若一定要做）}：\mathtt{band\_groups}{=}12,\ \mathtt{BAND\_ROWS}{=}5\text{，融合 } \mathtt{conv(单)}+\mathtt{conv{\to}cat}+\mathtt{多conv}\ \Rightarrow\ 215/227\ \text{层，48/52 块}$$
$$\Rightarrow\ \textbf{不推荐现在就做}：\text{先做 } \mathtt{mlp}\text{（0.74 s，0 BRAM，纯 RTL，见 PLAN:3356）——比融合的字节收益大 }\mathbf{29\times}$$

---

## 1. 数据与可复算口径

### 1.1 真实层清单（校验通过）

| 项 | 值 | 来源 |
|---|---|---|
| conv 层数 | **227** | `op_inventory_64.txt` 中 `kind=="conv"` 计数 |
| cout 分布 | 32:90 / 48:94 / 64:24 / 96:14 / **192:5** | 逐行读出，与任务书 §0 完全一致 |
| cin 最大 | **544** | 同上 |
| k 分布 | 1:114 / 3:113 | 同上 |
| 总 op | 395（conv 227、cat 116、interp 26、sam 6、eca 6、add 6、convfp 4、shuffle 3、unshuffle 1） | 同上 |
| 分段 | **223 段**（S1：`consumers==1` 才并链） | `segments.json` |

$$\Rightarrow\ \textbf{conv 打头段 200 条}\text{，分三类：}\mathtt{A}\ \text{单 op conv }\mathbf{92}\ ;\ \mathtt{B}\ \mathtt{conv{\to}cat}\ \mathbf{84}\ (\text{全 }k{=}3)\ ;\ \mathtt{C}\ \text{多 conv（}\ge2\text{）}\mathbf{24}$$

### 1.2 容量公式

$$\text{band 每行成本} = 2\times(\lceil c_{in}/64\rceil + G)\times(W+1)\times 8\ \text{B}$$
- $G$ = `band_groups`（输出组数，覆盖 $c_{out}\le 8G$）
- $\lceil c_{in}/64\rceil$：输入是**二值位流**，64 通道/64-bit 字（`V50_ARCHITECTURE.md:481-486`）
- $\lceil c_{out}/8\rceil\le G$：输出是 **int8**，8 通道/64-bit 字（同上）
- $\times2$ = 双 bank；$W{+}1=129$；$W=128$（256² 网最宽层是 128²，见 §1.3）
- 预算 $= (95-43)\times 4608 = \mathbf{239{,}616\ B}$

### 1.3 坐标换算（写清假设）

$$\Rightarrow\ \mathtt{op\_inventory\_64.txt}\ \text{是 64² 网}\Rightarrow\text{256² 网：}H,W\ \text{各}\times4\Rightarrow\text{像素}\times16\ ;\ c_{in}/c_{out}/k/s\ \text{不变}$$
$$\Rightarrow\ \textbf{最宽特征图}=128^2\ (\text{encoder\_1 / decoder\_1 级})\Rightarrow W_{BAND}=128\ \text{够用，不需要 256}$$
（v140 的 245,776 B 用的是 $W_{MAX}{=}256$，比实际需要大一倍，见 §2.4）

### 1.4 字节到秒的换算（三种口径，必须分开）

| 口径 | 读 | 写 | 227 层合计 |
|---|---|---|---|
| **P 母代理**（目标表示：1 bit/通道 packed） | $\lceil c_{in}/64\rceil\cdot 8\cdot HW$ | $c_{out}\cdot HW$ | **60.2 MB** |
| **M int8 全链** | $c_{in}\cdot HW$ | $c_{out}\cdot HW$ | **112.0 MB** |
| **R v5 真实**（`segments.json` 的 `in/out_bytes`，f32 段 $4CHW$） | 读出 | 读出 | **292.7 MB**（+ 非卷积 164 MB = 456.7 MB） |

$$\Rightarrow\ \text{全图一轮 } \mathbf{456.7\ MB}\ (\text{SEGMENT\_MAP §2})\ ;\ T_m=2.037\ \text{s}\ (\text{PLAN:3458}) \Rightarrow \text{达成率 } \mathbf{224\ MB/s}$$
$$\Rightarrow\ \text{而 480 MB/s 地板下 456.7 MB 只需 0.95 s} \Rightarrow \textbf{1.09 s 是"miss 冻结税"}（\text{利用率 }47\%\text{，与 PLAN:3480 的 46~50\% 吻合）$$
$$\Rightarrow\ \textbf{本报告用 } 224\ \text{MB/s}\ \text{折算保守值}，并列 } 413\ \text{MB/s（PLAN:3474 实测读带宽）}\text{作乐观界}$$

$$\Rightarrow\ \textbf{⚠ 口径矛盾（重要）}：\text{P 口径下全 227 层只有 60.2 MB；若 DDR 真是 packed 1-bit，}2.037\ \text{s 就只有 29 MB/s}$$
$$\quad\ \Rightarrow\ \textbf{今天的 DDR 里存的不是 packed 1-bit}（v5 是 int8/f32 eager），\text{P 口径描述的是**目标表示**，不是现状}$$
$$\quad\ \Rightarrow\ \text{所以"省字节"与"1 bit 打包"是**替代关系**：真把激活打包，2.04 s 的大部分自己就没了，融合几乎无事可做}$$

---

## 2. 容量表

### 2.1 二维表：组数 $G$ × 行数 $\mathtt{BAND\_ROWS}$（1..24）

每格 = `BRAM 块数 / 可覆盖层数`；`OK` = $\le52$ 块，`NO` = 超。块数按该 $G$ 下**最坏**那层算（设计必须统一满足它）。

| G | cout≤ | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | 21 | 22 | 23 | 24 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | 32 | 5OK/90 | 10OK/90 | 15OK/90 | 20OK/90 | 25OK/90 | 30OK/90 | 35OK/90 | 40OK/90 | 45OK/90 | **50OK/90** | 55NO/87 | 60NO/81 | 65NO/72 | 69NO/72 | 74NO/63 | 79NO/63 | 84NO/54 | 89NO/54 | 94NO/54 | 99NO/48 | 104NO/48 | 109NO/48 | 114NO/48 | 119NO/0 |
| 6 | 48 | 6OK/184 | 12OK/184 | 18OK/184 | 24OK/184 | 30OK/184 | 35OK/184 | 41OK/184 | **47OK/184** | 53NO/172 | 59NO/154 | 65NO/145 | 70NO/126 | 76NO/108 | 82NO/108 | 88NO/102 | 94NO/102 | 99NO/0 | 105NO/0 | 111NO/0 | 117NO/0 | 123NO/0 | 129NO/0 | 134NO/0 | 140NO/0 |
| 8 | 64 | 8OK/208 | 16OK/208 | 23OK/208 | 31OK/208 | 39OK/208 | **46OK/208** | 54NO/199 | 61NO/187 | 69NO/156 | 77NO/135 | 84NO/112 | 92NO/103 | 99NO/0 | 107NO/0 | 115NO/0 | 122NO/0 | 130NO/0 | 138NO/0 | 145NO/0 | 153NO/0 | 160NO/0 | 168NO/0 | 176NO/0 | 183NO/0 |
| 12 | 96 | 10OK/222 | 19OK/222 | 29OK/222 | 38OK/222 | **48OK/222** | 57NO/213 | 66NO/166 | 76NO/116 | 85NO/0 | 95NO/0 | 104NO/0 | 113NO/0 | 123NO/0 | 132NO/0 | 142NO/0 | 151NO/0 | 160NO/0 | 170NO/0 | 179NO/0 | 189NO/0 | 198NO/0 | 207NO/0 | 217NO/0 | 226NO/0 |
| 24 | 192 | 15OK/227 | 30OK/227 | **45OK/227** | 60NO/178 | 74NO/0 | 89NO/0 | 104NO/0 | 119NO/0 | 134NO/0 | 148NO/0 | 163NO/0 | 178NO/0 | 193NO/0 | 207NO/0 | 222NO/0 | 237NO/0 | 252NO/0 | 267NO/0 | 281NO/0 | 296NO/0 | 311NO/0 | 326NO/0 | 340NO/0 | 355NO/0 |

**容量前沿（加粗点）——组数与行数是硬对换：**

| G | 4 | 6 | 8 | 12 | 24 |
|---|---:|---:|---:|---:|---:|
| 最大行数 | **10** | **8** | **6** | **5** | **3** |
| 块数 | 50 | 47 | 46 | 48 | 45 |
| 覆盖层数 | 90 (39.6%) | 184 (81.1%) | 208 (91.6%) | **222 (97.8%)** | 227 (100%) |
| halo 重读 $2/BR$ | 20% | 25% | 33% | **40%** | 67% |

$$\Rightarrow\ \textbf{结论}：\text{组数每翻一倍，行数掉一档。}G{=}12\ \text{是"覆盖 }97.8\%\ \text{+ 还能留 5 行"的拐点}；G{=}24\ \text{用 67\% halo 换最后 5 层}$$

### 2.2 双 bank：真需要吗？

$$\Rightarrow\ \text{RTL 相位表（}\mathtt{bin\_band\_buf.v}\ \S5\text{）：RUN 期读 bank0 = 上一层输出、写 bank1 = 本层输出}$$
$$\quad\ \Rightarrow\ \textbf{两张张量同时活着}\text{，且 } c_{in}\ne c_{out}\Rightarrow\ \textbf{跨层融合必须双 bank}$$
$$\Rightarrow\ \text{单 bank 版本（load→compute→drain 串行）在结构上可行：}$$
$$\quad\ \text{代价按拍数估}：\text{以 }112{\to}32\ k{=}3\ W{=}128\ BR{=}10\ \text{为例，compute } 41.3\text{M MAC}/16\text{ lane}=2.58\text{M 拍，load+drain } \approx 12.8\text{k 拍} \Rightarrow \mathbf{0.5\%}$$
$$\quad\ \Rightarrow\ \textbf{单 bank 几乎免费，容量直接减半 ⇒ }G{=}12\ \text{可从 BR=5 提到 BR=10，halo 从 40\% 降到 20\%}$$
$$\quad\ \Rightarrow\ \textbf{但别现在改}：\text{现役 RTL 的 5 条相位不变量（}\S4\ \text{⑩⑪⑤}）\text{全部建立在双带上，改单带 = 重做相位证明，风险 >> 收益}$$

### 2.3 输入侧口径：白占多少？

| G | cin | 若 int8 占位（8ch/字） | 若 packed 1-bit（64ch/字） | 每行成本倍数 |
|---:|---:|---:|---:|---:|
| 12 | 544 | 68 组 | **9 组** | 3.81× |
| 8 | 448 | 56 组 | 7 组 | 4.27× |
| 12 | 256 | 32 组 | 4 组 | 2.75× |

$$\Rightarrow\ \text{通道数是 }8\times\text{（}64/8\text{），但"每行成本"含输出侧，故实为 }\mathbf{3.5\sim4.3\times}$$
$$\Rightarrow\ \textbf{结论：母代理的"4×"量级正确}\ ;\ \text{改对后，}c_{in}{=}544\ \text{的层每行从 } \mathbf{165{,}120\ B}\ \text{降到 } \mathbf{43{,}344\ B}\text{，行数上限从 } \mathbf{1}\ \text{提到 } \mathbf{5}$$
$$\Rightarrow\ \text{现役 RTL 正是 int8 占位：}\mathtt{NGRP}=\lceil C_{MAX}/8\rceil\ \text{对两个 bank 用同一个数} \Rightarrow \text{读侧白占 8×}$$

### 2.4 RTL 真实式交叉校验（`bin_band_buf.v:126-134`）

$$CAP = 2\cdot\lceil C_{MAX}/8\rceil\cdot(BR+2\cdot HALO)\cdot W_{MAX}\cdot 8$$

| C_MAX | BR | W_MAX | CAP (B) | 字节/4608 | 2 的幂深度估计 | 判定 |
|---:|---:|---:|---:|---:|---:|---|
| 48 | 8 | **256** | 245,760 | 54 | 57 | **✗ 超 52**（v140 算的 54 块，对上） |
| 48 | 8 | **128** | 122,880 | 27 | 29 | ✓ |
| 96 | 8 | 128 | 245,760 | 54 | 57 | ✗ |
| 192 | 4 | 128 | 294,912 | 64 | 114 | ✗ |
| 544 | 2 | 128 | 557,056 | 121 | 228 | ✗ |

$$\Rightarrow\ \textbf{现役 }C_{MAX}{=}48,W_{MAX}{=}256,BR{=}8\ \text{需 54 块 > 52 空闲} \Rightarrow \textbf{真 band 装不进（}41+54=95/95\text{ 正好顶死）}$$
$$\Rightarrow\ \text{把 }W_{MAX}\ \text{改成 128 就够（27 块）} \Rightarrow \textbf{这是最便宜的一刀，且不动任何逻辑}$$
$$\Rightarrow\ \textbf{另一个坑}：\text{深度}=2\cdot SLOT\ \text{不是 2 的幂} \Rightarrow Vivado\ \text{会按 2 的幂吃 RAMB36，实测口径比"字节/4608"高 }5\sim10\%$$
$$\quad\ (\text{T-142 的注释 }\mathtt{bin\_band\_buf.v:129-133}\ \text{已记这个坑})\Rightarrow\ \textbf{容量表要按"2 的幂列"再打一次折}$$

---

## 3. 收益表

### 3.1 逐层 DDR 流量（母代理 P 口径，前 15 重层）

| op | $c_{in}{\to}c_{out}$@H×W | k | 写 MB | 读 MB | 合计 MB |
|---:|---|---:|---:|---:|---:|
| 355 | 544→64@128×128 | 1 | 1.05 | 1.18 | 2.23 |
| 334 | 256→96@128×128 | 3 | 1.57 | 0.52 | 2.10 |
| 332 | 160→96@128×128 | 3 | 1.57 | 0.39 | 1.97 |
| 336 | 352→64@128×128 | 1 | 1.05 | 0.79 | 1.84 |
| 330 | 64→96@128×128 | 3 | 1.57 | 0.13 | 1.70 |
| 352 | 448→48@128×128 | 1 | 0.79 | 0.92 | 1.70 |
| 27 | 368→48@128×128 | 1 | 0.79 | 0.79 | 1.57 |
| 349 | 352→48@128×128 | 1 | 0.79 | 0.79 | 1.57 |
| 6 | 176→64@128×128 | 3 | 1.05 | 0.39 | 1.44 |
| 4 | 112→64@128×128 | 3 | 1.05 | 0.26 | 1.31 |
| 8 | 240→48@128×128 | 1 | 0.79 | 0.52 | 1.31 |
| 329 | 112→64@128×128 | 3 | 1.05 | 0.26 | 1.31 |
| 346 | 256→48@128×128 | 1 | 0.79 | 0.52 | 1.31 |
| 2 | 48→64@128×128 | 3 | 1.05 | 0.13 | 1.18 |
| 24 | 304→32@128×128 | 1 | 0.52 | 0.66 | 1.18 |
| **Σ227** | | | **41.2** | **18.9** | **60.2** |

### 3.2 融合子集的净省（$BR{=}8$，三种口径）

| 子集 | 段 | P 净 MB | P 净 s@224 | R 净 MB | R 净 s@224 |
|---|---:|---:|---:|---:|---:|
| **A 锁定：单 op conv** | 92 | **0.00** | **0.000** | **−1.2** | **−0.005** |
| B conv→cat | 84 | −0.8 | −0.003 | −8.6 | −0.038 |
| **C 多 conv（≥2）** | 24 | **+6.0** | **+0.027** | **+9.2** | **+0.041** |
| D = B+C | 108 | +5.3 | +0.023 | +0.5 | +0.002 |
| E = 全部 conv 打头 | 200 | +5.2 | +0.023 | −0.6 | −0.003 |

$$\Rightarrow\ \textbf{A 的净省恒为 0——这是数学恒等式，不是估算}：\text{单 conv 融合后仍要 "装输入 + 排输出"，一字不省}$$
$$\quad\ \text{唯一差别是输入多读 halo } \Rightarrow \text{净省 }= -\tfrac{k-1}{BR}\cdot(\text{输入读})\le 0$$
$$\quad\ \Rightarrow\ \textbf{REPORT\_v140:305 的"收益≈0"被独立复算证实}$$
$$\Rightarrow\ \textbf{B 也是 0 字节}\text{（cat 在 v5 是惰性的，SEGMENT_MAP §1：116 个 cat 记 0 字节），却要付 8.6 MB halo} \Rightarrow \textbf{纯字节看是负的}$$
$$\Rightarrow\ \textbf{只有 C（24 条多 conv 段）真省}：\text{段内中间张量不落 DDR} \Rightarrow \text{省"写 + 再读"} = c_{out}\cdot HW\cdot(1+\epsilon)$$

### 3.3 逐段收益（C 子集，$BR{=}5$，前 15）

| seg | 链 | conv 形状 | 省 MB(R) | halo MB | 净 MB(R) | 净 MB(P) |
|---|---|---|---:|---:|---:|---:|
| S201 | conv→cat→conv | 48@128→64@128 | 1.85 | 0.31 | **1.54** | 0.84 |
| S189 | conv→cat→conv→add | 96@128→64@128 | 4.00 | 2.94 | **1.07** | 1.58 |
| S15 | conv→cat→conv | 32@128→48@128 | 1.25 | 0.21 | **1.04** | 0.54 |
| S3 | conv→cat→conv→add | 64@128→48@128 | 2.73 | 2.10 | 0.63 | 1.03 |
| S163 | conv→cat→conv | 48@64→64@64 | 0.46 | 0.08 | 0.38 | 0.21 |
| S211 | conv→cat→conv→interp | 48@64→64@64 | 0.39 | 0.08 | 0.31 | 0.21 |
| S52 | conv→cat→conv | 32@64→96@64 | 0.35 | 0.05 | 0.30 | 0.14 |
| S151 | conv→cat→conv→add | 96@64→64@64 | 1.00 | 0.73 | 0.27 | 0.39 |
| S25 | conv→cat→conv→interp | 32@64→48@64 | 0.26 | 0.05 | 0.21 | 0.14 |
| S125 | conv→cat→conv | 48@32→64@32 | 0.12 | 0.02 | 0.10 | 0.05 |
| S89 | conv→cat→conv | 32@32→192@32 | 0.10 | 0.01 | 0.09 | 0.03 |
| S173 | conv→cat→conv→interp | 48@32→64@32 | 0.10 | 0.02 | 0.08 | 0.05 |
| S221 | conv→cat→conv→interp | 48@32→64@32 | 0.10 | 0.02 | 0.08 | 0.05 |
| S113 | conv→cat→conv→add | 96@32→64@32 | 0.25 | 0.18 | 0.07 | 0.10 |
| S35 | conv→cat→conv→interp | 32@32→48@32 | 0.07 | 0.01 | 0.05 | 0.03 |
| 其余 9 段 | | | | | −0.06 | +0.39 |

$$\Rightarrow\ \textbf{前三段占 C 子集净省的 } 40\%\text{，全在 128²（encoder\_1 / decoder\_1 的 rdb 块）}$$
$$\Rightarrow\ \textbf{注意 S189/S3 的 halo 税}：\text{省 4.00 MB 付 2.94 MB halo} \Rightarrow \text{因为段首是 256/64 通道的 } k{=}3\text{，重读贵}$$
$$\quad\ \Rightarrow\ \textbf{行数越大越赚}：BR{=}16\ \text{把 S189 的 halo 从 2.94 降到 0.92 MB（但 BR=16 装不进）}$$

### 3.4 为什么字节账这么小（关键判断）

$$\Rightarrow\ \text{可省的"中间张量"总量} = \text{24 个多 conv 段里第一级 conv 的输出之和} = \mathbf{6.0\ MB(P)\ /\ 14.2\ MB(R)}$$
$$\quad\ \text{而全图一轮 456.7 MB} \Rightarrow \text{只占 } \mathbf{1.3\%\ /\ 3.1\%}\text{ 的流量、}0.03\sim0.06\ \text{s} \Rightarrow\ \textbf{融合在 256² 上不是"省流量"的活}$$
$$\Rightarrow\ \text{真正贵的是 1.09 s 的 miss 冻结税（}456.7\text{ MB 本应 }0.95\ \text{s，实际 }2.037\ \text{s）}$$
$$\quad\ \Rightarrow\ \textbf{它只能靠"换成线性 DMA/合并访存"消灭，不能靠"少搬字节"}$$
$$\Rightarrow\ \textbf{而 miss 冻结的大头在 pack/E1E2 侧（PLAN:3480：46~50\%）} \Rightarrow\ \text{那正好是 B 子集（conv→cat，84 条全 }k{=}3\text{）}$$

### 3.5 pack 侧时间上限（字节账看不见的那部分）

$$\Rightarrow\ \text{v5 } \mathtt{pack\_s}=1.0911\ \text{s}\ (\text{segments.json})\ ;\ \text{227 个 conv 输出合计 } 66.2\ \text{MB}$$
$$\Rightarrow\ \text{B 子集（84 条）里 conv 输出 }=15.9\ \text{MB}=\mathbf{24\%} \Rightarrow\ \textbf{若这 24\% 改从 band 读，pack 上限省 } \mathbf{0.262\ s}$$
$$\Rightarrow\ \text{对照 PLAN:3481 的"读侧还有约 0.5 s 可吃（}48\%\times\mathtt{E1E2}\ \text{桶 }1.15\ \text{s）"}\Rightarrow \textbf{同量级，0.26 s 是它的子集}$$
$$\Rightarrow\ \textbf{这笔账需要板级证据}（5 个计数器重测 conv→cat 段的读 miss 冻结率），\text{本报告只有模型，没有实测}$$

---

## 4. 候选方案与推荐

$$\text{可行域}：(G,BR)\in\{(4,10),(6,8),(8,6),(12,5),(24,3)\}\ (\text{都用 45~50 块})$$

| 方案 | G | BR | 融合子集 | 段/层 | BRAM | 净省字节(P) | 净省字节(R) | pack 上限 |
|---|---:|---:|---|---:|---:|---:|---:|---:|
| **P1 字节最优** | 12 | 5 | A+C | 111 / **131** | 48 | **+5.67 MB / +0.025 s** | +4.92 MB / +0.022 s | 0 |
| **P2 推荐** | 12 | 5 | A+B+C | 195 / **215** | 48 | +4.42 MB / +0.020 s | −8.87 MB / −0.040 s | **≤0.26 s** |
| P3 通吃 | 24 | 3 | A+B+C | 200 / **224** | 45 | +3.22 MB / +0.014 s | −25.4 MB / −0.113 s | ≤0.26 s |
| （现状） | 4~6 | 8~10 | A | 90 / 90 | 47~50 | **0.00** | −0.6 MB | 0 |

$$\Rightarrow\ \textbf{推荐 P2}：\text{它是唯一"字节小亏但时间收益有量级"的点；}G{=}12\ \text{覆盖 97.8\%、48 块留 4 块余量}$$
$$\Rightarrow\ \textbf{放弃 P3 的代价}：\text{仅 9 层 + 那 5 个 192 层} \Rightarrow \text{值 } 0.004\sim0.03\ \text{s}，\text{却要 halo 从 40\% 涨到 67\%} \Rightarrow \textbf{不亏才怪}$$
$$\Rightarrow\ \textbf{放弃 P1（选 P2）的代价}：\text{字节上亏 } 0.045\ \text{s}（P 口径），换 84 条 conv→cat 段的 pack 通道}$$
$$\Rightarrow\ \textbf{从现状到 P2 要付}：\text{行数 }10{\to}5\text{（halo 20\%→40\%）} + \text{带内多级串联（}\mathtt{band\_seq}\text{ 移植 1.5~3 天）} + \text{一次 OOC 综合}$$
$$\Rightarrow\ \textbf{第一步该改什么}：\textbf{组数优先}（4/6→12）。\text{行数是"廉价旋钮"（每加一行只多 }2/BR\ \text{的 halo），组数是"覆盖旋钮"（}39.6\%{\to}97.8\%\text{）；}$$
$$\quad\ \text{反过来做（先加行）只会让已覆盖的 184 层更省一点，覆盖不动。}$$

---

## 5. 两个问题的直接回答

### a) 覆盖 $c_{out}\le96$（$G{=}12$）时 $\mathtt{BAND\_ROWS}$ 能到几？能覆盖 93\% 吗？

$$\Rightarrow\ \textbf{BR 上限 = 5}（48 块），此时覆盖 \mathbf{222/227 = 97.8\%}$$
$$\Rightarrow\ \text{受限于 }c_{in}{=}544\ \text{那几层（}2(9+12)\cdot129\cdot8=43{,}344\ \text{B/行，}239{,}616/43{,}344=5.5\text{）}$$
$$\Rightarrow\ \textbf{93\% 的要求：BR=6 就给 213 层（93.8\%），但块数 57 > 52 ✗}$$
$$\quad\ \Rightarrow\ \textbf{要么 BR=5 拿 97.8\%（推荐），要么 BR=6 就得先按 §2.4 把 }W_{MAX}\ \text{收紧到 128 且按 2 的幂口径重算}$$

| BR | 可覆盖层 | 占比 | 判定 |
|---:|---:|---:|---|
| 1~5 | 222 | **97.8%** | ✓ 全部 $c_{out}\le96$ |
| 6 | 213 | 93.8% | ✗ 57 块 |
| 8 | 116 | 51.1% | ✗ 76 块 |
| 10+ | 0 | 0% | ✗ |

$$\Rightarrow\ \textbf{所以：BR=5 就够，97.8\% 已超过 93\% 的目标；不需要为 93\% 去动行数}$$

### b) 那 5 个 $c_{out}{=}192$ 的层走老路，损失多少？

| op | 形状 | P 口径 MB | R 口径 MB | $G{=}24$ 行上限 |
|---:|---|---:|---:|---:|
| 129 | 96→192@32×32 k3 | 0.21 | 2.36 | 4 |
| 136 | 384→192@32×32 k1 | 0.25 | 1.77 | 3 |
| 155 | 512→192@32×32 k1 | 0.26 | 1.90 | 3 |
| 171 | 512→192@16×16 k1 | 0.07 | 0.33 | 3 |
| 187 | 512→192@8×8 k1 | 0.02 | 0.08 | 3 |
| **Σ** | | **0.80** | **6.44** | |

$$\Rightarrow\ \textbf{损失 = 0.0036 s（P）/ 0.029 s（R）——可以忽略}$$
$$\Rightarrow\ \textbf{更要紧的是}：\text{这 5 层全在 }32^2/16^2/8^2，\text{体积小到连 halo 都几乎不痛}$$
$$\Rightarrow\ \textbf{而且它们大多不是"单 conv 段"}：\text{S89 就是 }32{\to}192\ \text{的多 conv 段，}G{=}12\ \text{下装不进去} \Rightarrow \text{损失被高估了}$$
$$\Rightarrow\ \textbf{结论：不要为这 5 层把 }G\ \text{扩到 24}。\text{扩了要把行数砍到 3、halo 67\%，换 0.004~0.03 s，净亏}$$

---

## 6. 对任务书前提的 6 处更正（都带证据）

| # | 任务书说 | 实际 | 证据 |
|---|---|---|---|
| 1 | 现有 band 只有 **4 组 = 32 通道** | 现役 RTL 是 $C_{MAX}{=}48\Rightarrow$ **6 组 = 48 通道** | `bin_band_buf.v:60`；且这正好解释 **90/223**：92 个单 conv 段里 2 个 cout=96/192 被挡 ⇒ 92−2=90 ✓ |
| 2 | conv→cat 两级 = **45 条** | **84 条**（全部 $k{=}3$） | `segments.json` kinds 直方图。45 这个数在本项目里只对应"单 conv 段中 cout=32 的条数"（另一 45 是 cout=48），**猜测**是口径串了 |
| 3 | 单 op conv 段 = 90 | **92**（90 个 $k{=}1$ + 2 个 $k{=}3$） | 同上 |
| 4 | cin32/cout32 → 23 行 | 那是 $c_{in}{=}32$；**真实 $c_{in}$ 最大 448**（cout≤32 内）⇒ **10 行** | `op_inventory` 逐行 |
| 5 | 母公式按 1 bit/通道读 | 只描述**目标表示**；v5 真实是 int8/f32 ⇒ 差 8× / 总账 60 MB vs 457 MB | §1.4 |
| 6 | 真 band 54 块能放下 | 52 空闲 ⇒ **装不下**（$W_{MAX}{=}256$）；改 $W_{MAX}{=}128$ 后 27 块 | §2.4，与 REPORT_v140:285-287 一致 |

---

## 7. 复算方法

```bash
cd /media/felix/Data/coding/demoir-fpga
python3 runs/v148_cap/cap_model.py          # 全部表，输出到 stdout
python3 runs/v148_cap/cap_model.py > runs/v148_cap/model_output.md
```

- 容量：`cap_model.rows_max(G, cin)` / `bram_blocks(G, rows, cin)` / `cap_rtl(c_max, rows, w_max)`
- 收益：`cap_model.seg_stats(seg, ops, jops, model)`，`model ∈ {"P","M","R"}`
- 门控：`cap_model.feasible(G, br, st)`（段内每个 stage 的 $c_{out}$ 都要 $\le 8G$，且最坏 $c_{in}$ 的那行放得下 $BR$ 行）
- 改一个假设就重跑：`FREE_BYTES`（若多出几块）、`W_BAND`（若按层收紧）、`RATE`（若实测带宽不同）

**尚未验证、需要板级/综合确认的三件事**（本报告没有证据，不要当结论用）：
1. pack 路径那 0.26 s 的上限，需要 `board_wrst_probe` 在 conv→cat 段上重测读 miss 冻结率；
2. 真 band 的 BRAM 块数，需要一次 OOC 综合（2 的幂深度口径可能比字节口径高 5~10%）；
3. 单 bank 的 0.5% 串行化估计是拍数粗略推算，未做时序/相位证明。
