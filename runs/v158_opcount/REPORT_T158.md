# T-158 ★ 按「命令次数」重算融合（band 链）：**0.26 s（保守）/ 0.53 s（乐观）**，不如 `convfp`

> 纯主机侧、只读、纯 CPU 计算。不上板 / 不起 Vivado / 不改任何 RTL 与 PS。
> 输入全部是已实测产物：`runs/board_t151/t151a_141402_141440.json`（227 层实测分桶）、
> `runs/board_t156/REPORT_T156.md`（MMIO 事务普查）、`runs/board_t154/REPORT_T154.md`（每命令五格 + `DBG_E_CYC`）、
> `runs/v90_stage2/verify/v4f/fuse1.log`（223 段段计划）、`docs/V50_ARCHITECTURE.md` §14/§15。
> 复算脚本 `opcount.py`，结果 `opcount.json`。

---

## 0. 先纠正一处口径（**这会改变结论的方向**）

$$\Rightarrow\ \textbf{"698"不是 conv 命令，是 E1E2 pack 命令}：\ 698=\sum_{227\ \text{层}}\lceil \mathtt{cin}/64\rceil$$
$$\quad\ \text{已在实测层表上复算：}\ \Sigma=698\ ✓\（107\ \text{层}\ cb{=}1、\text{最多}\ 9）$$
$$\Rightarrow\ \text{conv 侧是}\ \textbf{227 次 kick}（\text{每层 1 次}）；\text{合计}\ \mathbf{925}=\mathtt{698\ pack}+\mathtt{227\ conv}\ (\texttt{V50\_ARCHITECTURE.md:60})$$
$$\Rightarrow\ \text{168 个 PS op 一条 PL 命令都不发}（\text{interp/sam/add/eca 走 numpy，cat 惰性 110/116}）$$
$$\Rightarrow\ \Rightarrow\ \textbf{"698 条命令能不能合"= "pack 这步能不能消失"}，\text{而 pack 恰恰是 band 链里唯一能被硬件内化的那一级}$$

---

## 1. 命令分类表（类型 → 条数 → 每条固定开销，全部实测）

| 命令类型 | 条数 | 每条的固定开销（实测） | 拆项 | 命令消失时能否省 |
|---|---|---|---|---|
| **E1E2 pack（`run_one`，`bin_engine.py:3847` 的 `for g in range(cb)`）** | **698** | **0.542 ms** | `run_one` Python 0.295 + 5 个配置写 0.247（`T-156` 普查 3494 写/698） | **能** |
| ├ 其中"等硬件"部分（不许算进收益） | 698 | 0.755 ms | 9.4 次轮询读 0.330 + poll 内 sleep 0.422 | **不能**（等于 pack 的 PL 硬件 0.317 s） |
| conv kick（`start`） | 227 | 60.8 µs | — | 不省（223 段仍各 1 次） |
| conv 配置写（precomp 直写，绕过钩子） | 2433 | 45 µs | 10.7 次/层 | 不省（每段仍要写 cfg） |
| `_wait_idle` / epi 读 | 227 / 406 | 47.6 / 47.3 µs | — | 不省 |
| PS op（168） | 168 | — | 无 MMIO | — |

$$\Rightarrow\ \text{pack 命令的实测总成本随尺寸走：}\ \mathtt{H}{=}8\ 0.76\ \text{ms/cmd}\to\mathtt{H}{=}128\ 4.75\ \text{ms/cmd}\ (\text{均值}\ 1.61)$$
$$\quad\ \text{把它按}\ V3(\mathtt{DBG\_E\_CYC})\ \text{每命令硬件拍放大：}\ \Sigma\ \text{硬件}=0.322\ \text{s}\ \text{vs}\ \text{T-154 实测}\ 0.317\ \text{s}\ ✓\Rightarrow\ \text{模型自洽}$$
$$\Rightarrow\ \Rightarrow\ \textbf{pack 桶 1.126 s = 硬件 0.317（真活）+ PS 纯开销 0.379（698×0.542）+ 轮询/等待 0.43（与硬件重叠）}$$

---

## 2. band 链能压缩到几条（**核心**）

### 2.1 链的结构上限：**3 级、定序、不能串 conv**

$$\Rightarrow\ \mathtt{bin\_band\_seq.v:109-114}\ \text{的}\ \mathtt{nxt()}\ \text{是常量折叠：}\ \mathtt{SC\_NONE}\to\mathtt{CONV}\to\mathtt{PACK}\to\mathtt{SAM}\to\mathtt{NONE}$$
$$\Rightarrow\ \textbf{一个描述符最多驱动 3 级算子，且顺序写死}；\text{链长由}\ \mathtt{op\_mask}\ \text{的位组合决定，}\textbf{不由 band 容量决定}$$
$$\quad\ \text{band 容量只限}\ \mathtt{BAND\_ROWS}{=}8\ \text{行 + halo 行（D4：}2\times8\times W\times C\le270\ \text{KB}）；\text{依赖只体现在 FENCE（换带前写静默，}:203\text{-}217）$$
$$\Rightarrow\ \textbf{没有 "conv→conv" 这条边}：\ \mathtt{nxt(SC\_CONV)}\ \text{只会去 PACK/SAM} \Rightarrow \text{连续两个 conv 层必须两条描述符}$$
$$\Rightarrow\ \text{整网真实链长：}395\ \text{op}/223\ \text{段}=\mathbf{1.77}\ \text{op/段}；\text{最长段 4 op（}\mathtt{conv/cat/conv/interp}\text{，12 段）}\textbf{超出序列器能力}$$
$$\Rightarrow\ \text{每段描述符数}=\lceil \mathtt{h\_out}/8\rceil（\mathtt{bin\_net.py:1627,1681}）\Rightarrow\ \textbf{整网 1354 条描述符（256²）}$$

### 2.2 但描述符不是命令：**链内不需要 PS 写任何寄存器**

$$\Rightarrow\ \text{PS 只写：链首}\ \mathtt{0x300}+\mathtt{n\_desc}+\text{arena 窗口（}\S 15.2\ \text{的 5 个写）+ 每段的}\ \mathtt{cout/k/s/d/pad}\ \text{5 个边界寄存器}$$
$$\Rightarrow\ \text{描述符 40 B/条由 segq 自己从 DDR 取（5 拍）}\Rightarrow 1354\times40\ \text{B}=54\ \text{KB/帧}\approx0.08\ \text{ms}$$
$$\Rightarrow\ \Rightarrow\ \textbf{固定开销}\times\textbf{链数}，\text{而链数}=\text{段数}=223\ (\text{不是描述符数 1354})$$

### 2.3 现状：**v50 已落地的融合对命令数是 0 收益**

$$\Rightarrow\ \text{段计划 223 段 / 融合 90 段（纯单级 conv）/ 140 条描述符，其余 133 段逐 op（}\S 15.4）$$
$$\Rightarrow\ \text{融合段的输入必须是 PL 可读的 C8/int8 位流 blob（}\mathtt{bin\_net.py:2777\text{-}2781}\text{）}\Rightarrow \textbf{pack 仍由 PS 先发}$$
$$\Rightarrow\ \text{且这 90 段恰好是}\ cin{=}48/112/176/240/304\ \text{的"压到 32 通道"层，}\Sigma cb=\mathbf{369}\ (53\%\ \text{的 698})$$
$$\Rightarrow\ \Rightarrow\ \textbf{现有 261 行序列器的收益=0 条命令（与 T-148 的 0 字节一致）}$$

### 2.4 三档 N

| 档 | 做法 | 命令数 | 需要什么 |
|---|---|---|---|
| 现状 | v50 单级 conv 融合 | **925 → 925** | 已有（缺带级 TB） |
| **保守** | 同层 `cb` 组并成一条链（`chain_descs`，`bin_engine.py:3169` **PS 已写好**） | **925 → 454**（698→227） | 只缺能跑的 v50 位流 |
| **乐观** | 带内 PACK 吃掉整条 pack 命令（698→0），223 段各 1 次 submit | **925 → 223**（−76%） | 新 RTL：多分支带内拼装 |

$$\Rightarrow\ \text{乐观档的拦路虎不是性能而是}\textbf{结构}：698\ \text{条命令恰恰集中在下游}\ \mathtt{cat}\ \text{消费层}（cin{=}112/176/240/304\Rightarrow cb{=}2\text{-}5）$$
$$\quad\ \text{本网 84 个}\ \mathtt{conv/cat}\ \text{段，每个 cat 平均拼 5.9 个分支，}\textbf{通道不对齐 64}\Rightarrow \text{要按通道偏移 OR 进位流字 = 读改写}$$
$$\quad\ \mathtt{bin\_band\_seq.v}\ \textbf{没有通道偏移、没有多分支累加、没有带内 RMW}（261 行里只有级序 + 双带 ping-pong）$$
$$\Rightarrow\ \Rightarrow\ \textbf{"698→0" 不是移植这 261 行，而是重做一版带内拼装数据通路}$$

---

## 3. 折算收益

$$\text{能省的绝对秒数}=\text{消失的命令数}\times0.542\ \text{ms}$$
$$\Rightarrow\ \textbf{保守}\ 471\times0.542=\mathbf{0.255\ s}\ (5.33\to5.07,\ -4.8\%)$$
$$\Rightarrow\ \textbf{乐观}\ (698\times0.542)+\underbrace{0.15}_{\text{轮询/交接揉合，估算}}=\mathbf{0.53\ s}\ (5.33\to4.80,\ -10\%)$$
$$\Rightarrow\ \textbf{不能省的下限}=\mathbf{3.56\ s}\ (5.33\ \text{的}\ 67\%)：\text{pack 硬件 }0.317+\mathtt{q8cache}\ 0.502+\mathtt{epi}\ 0.240+\text{段表 }0.186+\mathtt{cfg}\ 0.117+\text{槽 }0.099+\text{conv 轮询 }0.119+\mathtt{convfp}\ 1.246+\text{小算子 }0.696+\mathtt{cat}\ 0.037$$
$$\Rightarrow\ \text{其中}\ \mathtt{q8cache/epi/段表/cfg/槽}\ \text{都是"每段"成本，而}\ \textbf{段数 223 ≈ 层数 227} \Rightarrow \textbf{融合几乎不动它们}$$

---

## 4. 结论

$$\Rightarrow\ \textbf{按命令次数算，融合值 0.26 s（保守）/ 0.53 s（乐观）}$$
$$\Rightarrow\ \text{与}\ \mathtt{convfp}\ \textbf{1.256 s（4 个 op）比：总量小 2.4\text{-}4.8 倍}；\text{与它"可拿的"}\ -0.45\ \text{s}（现成 C 内核）\textbf{相当}$$
$$\quad\ \text{但}\ \mathtt{convfp}\ \textbf{不碰 RTL/位流/带级 TB}，\text{融合要}\ \textbf{新位流 + 带级 TB + 复冻三尺寸 sha1}$$
$$\Rightarrow\ \Rightarrow\ \textbf{按命令次数单独算：不值得当主杠杆。}\ \text{它能赢得过}\ \mathtt{convfp}\ \text{的唯一方式是}\textbf{把小算子也算进来}：$$
$$\quad\ \text{interp/sam/add/eca 共}\ \mathbf{0.696\ s}\ \text{的 PS 时间搬进 PL（D5）}\Rightarrow \text{融合全口径}\ 0.53+0.70\approx\mathbf{1.2\ s}\ (\text{最大单点})，\text{但要 4\text{-}6 周 RTL}$$

### 若"值得"：最小可行范围

$$\Rightarrow\ \textbf{#1 只做保守档（698→227）：E1E2 描述符链}。\text{PS 侧代码已在（}\mathtt{chain\_descs}\ \text{/}\ \mathtt{run\_one(chain=)}\text{），}$$
$$\quad\ \text{它}\textbf{没有 band、没有 halo、没有级联} \Rightarrow \text{不需要 }\S 15.6\ \text{的 ②序列器、也不需要 ③}k\ge3\ \text{的裁定}$$
$$\quad\ \text{而且它是 v50 位流本来就要交付的东西} \Rightarrow \textbf{不额外立项}，\text{先拿}\ -0.26\ \text{s}$$
$$\quad\ ⚠\ \text{第一条待验：}\mathtt{run\_one}\ \text{的 chain 分支填}\ \mathtt{op\_mask}{=}1{\ll}\mathtt{V50\_OP\_BITS["conv"]}\ (\mathtt{bin\_engine.py:3117\text{-}3126},\ \text{op\_mask 在 }3125)，$$
$$\quad\ \quad\ \text{而这是 pack 命令}\Rightarrow \text{与 segq 的 }\mathtt{OP\_MASK\_SUP}\ \text{校验语义要先对齐（}\S 14.9）$$
$$\Rightarrow\ \textbf{#2 若只想要收益最大的一段}：\text{先融}\ \mathtt{H}\ge64\ \text{的 86 层（249 条命令、pack 桶 }0.707\ \text{s 的 63\%）}，$$
$$\quad\ \text{它们每命令 1.79\text{-}4.75 ms、离硬件最远} \Rightarrow \text{同样 698→227 的机制，命中率 36\%}$$

---

## 5. 附：`convfp` 为什么 4 个 op 要 1.256 s

$$\Rightarrow 0.769\ \text{GFLOP}/1.256\ \text{s}=\mathbf{0.61\ GFLOP/s}；\text{A9 单核 NEON sgemm 现实 }1.5\text{-}2.5\Rightarrow\textbf{算力受限，不是带宽}$$
$$\Rightarrow\ \text{数据只有 }10.5\ \text{MB（等效 }8.4\ \text{MB/s，远低于 }655\ \text{MB/s 的 CMA）}\Rightarrow \text{慢在 }\mathtt{im2col}+\mathtt{numpy}\ \text{sgemm}\ \text{本身}$$
$$\Rightarrow\ \text{最小的 }32^2\ \text{层只有 }0.23\ \text{GFLOP/s}\Rightarrow \text{固定开销占比大} \Rightarrow \textbf{换 C 内核（树里已有、默认关）预期 }-0.45\ \text{s}$$

---

## 6. 假设与"实测/估算"分界

| 数 | 性质 |
|---|---|
| 698 = Σ⌈cin/64⌉、227 kick、925、cb 直方图、H 分桶 | **实测**（`t151a` 层表逐层复算） |
| 0.542 ms/命令 = 0.295 Python + 0.247 写 | **实测**（`T-154` 206 ms/698；`T-156` 172.1 ms/698） |
| pack 硬件 0.317 s | **实测**（`DBG_E_CYC`），并用 V3 拍数独立复算得 0.322 s |
| 223 段 / 90 融合 / 140 描述符 / 84 conv-cat | **实测**（`v90_stage2` FakePL 段计划，**64² 那张网的图结构**；段数只依赖图，与 H 无关） |
| 698→0 需要多分支带内拼装 | **读码判定**（`bin_band_seq.v` 无通道偏移/RMW） |
| +0.15 s 轮询揉合、223 次 submit 的净开销 | **估算** |

## 7. 产物

| 文件 | 内容 |
|---|---|
| `opcount.py` | 全部复算（可重跑，只读上游 JSON/日志） |
| `opcount.json` | 机器可读结果（命令数、直方图、两档秒数、不可省下限） |
