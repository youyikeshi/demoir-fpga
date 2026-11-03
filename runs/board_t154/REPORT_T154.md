# T-154 ★ E1E2 pack 1.118 s：**诊断做完，三种改法全部否证**（净收益 −0.00 s）

> 日期 2026-11-03 12:12（主机）｜板端本地时间 14:45｜一个租约 = 一个进程 = **一次 load**
> 位流 v39 `602725b86941864eeeeab3be005e1e81`（跑前/跑后 md5 均校验）｜@**150 MHz**（读回）
> hw = 256²、slots = 24、**零护栏**｜工作点 = `e1e2`+`LAZY_DEQ=1`+`CAT_FUSION=1`+`BILINEAR_C`
> +`PRECOMP=1`+`Q8CACHE=1`+`F32DIRECT=1`+`CAT_ECA_FUSE=1`+`EPI_NOINV=1`+`PROFILE=1`
> **部署来源 = `git archive HEAD` 快照**（不是工作树：工作树里 `bin_net.py` 有 T-153 未提交的 +169 行）
> 判据：**256² 12/12 跑、128² 2/2、64² 2/2 三 sha1 全中**（`9b1940c8`/`f86a7035`/`025757a8`；
> `27e25cf6`/`b82b83f3`/`6db4e9bb`；`bfa246d3`/`226bd39e`/`762d84b9`）

---

## 0. 一句话结论

$$\Rightarrow\ \textbf{“空搬 1.118 s”不存在可回收的 PS 部分。}$$
$$\quad \text{pack 桶 } = \underbrace{0.32\ \text{s}}_{\text{PL 硬件（DBG\_E\_CYC 口径）}} + \underbrace{0.86\ \text{s}}_{\text{PS/总线握手}}$$
$$\quad \textbf{但后者不是 Python、也不是轮询次数}：\text{把 6 个字配置写搬进 C，实测 } \mathbf{0.109\ ms/cmd\ vs\ Python\ 0.108\ ms/cmd}$$
$$\quad \Rightarrow \textbf{纯 AXI-Lite 总线 turnaround}（引擎访存时一次读 30~42 µs、一次写 17 µs）。$$
$$\Rightarrow\ \textbf{三个候选改法实测判定：(a) 零拷贝不可行、(b) 重叠不可行、(c) 握手降不下来。}$$
$$\Rightarrow\ \text{剩下的唯一杠杆是 RTL（v50 的“整层一条描述符链”正是干这件事的）}；\textbf{v39 上没有。}$$

---

## 1. 依据：pack 桶的真实构成（`t154d` 诊断跑，带钩子，256²、227 层 / **698 条命令**）

| 桶（独占） | ms | 占比 | 次数 | 每次 |
|---|---|---|---|---|
| `wait`（轮询里的睡眠段） | 294.6 | 20.9% | 698 | 0.42 ms |
| `mmio_rd`（MMIO 读） | **265.5** | 18.9% | 6314 | **42 µs** |
| `run_one` 自身 Python | 206.0 | 14.6% | 698 | 0.30 ms |
| `wr_seq`（6 个字分两批） | 156.7 | 11.1% | 1396 | 112 µs |
| numpy 写 thr/desc（残留） | 146.9 | 10.4% | — | 0.65 ms/层 |
| `flush`（刷 1 MB pack arena） | 100.9 | 7.2% | 454 | **222 µs** |
| `mmio_wr` | 82.9 | 5.9% | 1623 | 51 µs |
| `invalidate` | 47.9 | 3.4% | 227 | 211 µs |
| `select` / `start` / `chain_on` / `wait_idle` | 105.0 | 7.5% | — | 46~77 µs |
| **Σ** | **1406** | 100% | | |

$$\Rightarrow\ \text{线性拟合 } \mathtt{pack\_ms} = \mathbf{3.36} + 2.7\times10^{-4}\cdot(cb\!\cdot\!hw)\ (R^2=0.921)
\Rightarrow \text{常数项 } \Sigma = 0.762\ \text{s},\ \text{比例项 } \Sigma = 0.645\ \text{s}$$
$$\Rightarrow\ \text{MMIO 原语：} \mathtt{pynq.MMIO.read}=14.5\ \mu s,\ \mathtt{write}=17.2\ \mu s,\ \mathtt{memoryview}=3.5\ \mu s\ (\text{空闲})$$
$$\quad \text{但**引擎访存时**读一次 } 42\ \mu s \Rightarrow \textbf{轮询读的开销 ≈ 硬件时间本身}$$

**单命令五格**（同一 job 原地复跑，25 次/格；V3 = `DBG_E_CYC` 硬件拍数）

| 形状 | V0 现状 | V1 影子全跳 | V2 +select | **V3 硬件** | V4 C 批量 | PS 占 V0 |
|---|---|---|---|---|---|---|
| 128² cin=48 cb=1 | 5.735 ms | 4.013 | 4.141 | **2.221** | 3.528 | 61% |
| 64² cin=48 cb=1 | 2.923 | 1.269 | 1.360 | **0.556** | 1.019 | 81% |
| 32² cin=48 cb=1 | 2.226 | 0.576 | 0.668 | **0.140** | 0.396 | 94% |
| 16² cin=96 cb=2 | 2.849 | 0.411 | 0.495 | **0.041** | 0.244 | 99% |

$$\Rightarrow\ \Sigma\ \text{硬件} \approx \mathbf{0.32\ s}\ (\text{全 698 条命令})；\text{其余 } 0.86\ \text{s 全在 PS/总线}$$

---

## 2. 三个候选改法：全部否证（各有硬证据）

### (a) 零拷贝 ⇒ **不可行**（布局不等价，不是恒等复制）
$$\Rightarrow\ \text{pack 进 } \mathbf{152.54\ MB} \Rightarrow \text{出 } \mathbf{19.07\ MB}，\text{比值 } \mathbf{8.00:1}\ (\text{恒等复制应为 } 1{:}1)$$
$$\Rightarrow\ \text{E1E2 干的是 } \mathtt{sat8(rint(x\!\cdot\!2^{fs}))+t_q>0}\ \text{的二值化 + 位打包（64 int8 → 8 B = 1 bit/通道，逐通道阈值）}$$
$$\Rightarrow\ \mathtt{n\_f32}=0\ \text{只说明“不需要 f32 量化”，}\textbf{不等于“源与目标逐字节相同”}（216/227 层 n\_f32=0）$$
$$\Rightarrow\ \text{conv 的 IN 是位流；源槽是 int8 C8} \Rightarrow \text{描述符改指源 pa 会把 int8 当位流喂给 conv}$$

### (b) 与 conv 重叠 ⇒ **不可行**（严格依赖，pack:conv=1.00 是结构性的）
$$\Rightarrow\ \mathtt{pack}(L)\ \text{读的是 } \mathtt{conv}(L{-}1)\ \text{刚写的 OUT 槽} \Rightarrow \text{必须等 } L{-}1 \text{ done}$$
$$\Rightarrow\ \text{除非 RTL 支持“按 band 流式消费”（v50 链有 band，v39 没有）}$$

### (c) 降低每层握手 ⇒ **实测降不下来**（两次 A/B，都逐位不变、都没有收益）
| 尝试 | 256² pack（3 跑交替，median） | 墙钟 median | 结论 |
|---|---|---|---|
| **A 基线**（原自适应轮询） | **1.189 s** | **6.050 s** | — |
| 2a C 批量下发（`mmio_batch`：写+start+紧轮询一次调用） | 1.229 s | 6.203 s | **+0.04 s（更差）** |
| 2a' C + 自校准预测睡 | **1.769 s** | 6.712 s | **+0.58 s（更差）** |
| 2b 仅预测睡（先睡到 0.92×2.2×解析拍数，再读 1~2 次） | 1.202 s | 6.192 s | **+0.013 s（等于 0）** |

$$\Rightarrow\ \textbf{决定性证据}：\text{同样 6 个配置寄存器，} \mathtt{\_wr\_seq}\ \text{(Python)} = \mathbf{0.108\ ms}，\mathtt{mmio\_batch::write\_pairs}\ \text{(C)} = \mathbf{0.109\ ms}$$
$$\quad \Rightarrow \textbf{“Python 慢”是伪命题}：\text{这 6 个写的钱全花在 AXI-Lite 上，换语言一分不省}$$
$$\Rightarrow\ \text{而“先睡再读”之所以无效：}\mathtt{time.sleep()}\ \text{在这块板上过冲 } 50\sim300\ \mu s，\text{睡省下的读又被过冲吃掉}$$
$$\Rightarrow\ \text{每次测量前后板端 } \mathtt{ps/}\ \text{md5 一致；} \mathbf{12/12}+2/2+2/2\ \text{跑三 sha1 全中} \Rightarrow \text{这些改动**数值上是安全的，只是没用**}$$

---

## 3. 剩余可拿的只有“零头”（都已实测，合计 ≈ −0.10 s，不建议单独上板）

| 项 | 现状 | 可改 | 收益 |
|---|---|---|---|
| `flush` 每层两次（都刷同一块 1 MB arena） | 454 次 / 100.9 ms | **每层刷一次**（实测 227 次 / 61.9 ms，sha1 不变） | **−40 ms** |
| `invalidate`（打包结果 CPU 从不读） | 227 次 / 47.9 ms | LAZY 路径可跳过（需改 `bin_net` kick 传入） | −48 ms |
| `chain_on()`（每层 2 次，现读环境变量） | 17.4 ms | 进程内缓存（实测本次未生效，需与 `_v50_env_probe` 一起看） | −17 ms |
| thr/desc 的 numpy 写法 | 146.9 ms | `tobytes()` + `frombuffer` 一次赋值 | −30 ms? |

$$\Rightarrow\ \text{这四项加起来 } <0.1\ s，\text{而 5.959 s 要 } \le 5.1\ \text{需 } 0.96\ s \Rightarrow \textbf{不够，且有风险}$$

---

## 4. 给主代理的判断

$$\Rightarrow\ \textbf{“pack 1.118 s 是空搬”应撤回}：\text{它搬的是 } 152.5\ \text{MB int8} \to 19.1\ \text{MB 位流（8:1 二值化+位打包）}，\textbf{语义上必须做}$$
$$\Rightarrow\ \text{它的 } 0.86\ \text{s PS 部分由 } \mathbf{698\ 次命令握手} \text{构成}：\text{每次 } \sim12\ \text{写} + \sim9\ \text{读}，\text{一次读写 } 17\sim42\ \mu s$$
$$\Rightarrow\ \text{PS 侧**只剩**一条路：}\textbf{减少命令数}（= 把 } cb \text{ 组并成 1 次）\Rightarrow \text{这正是 v50 描述符链；需要 RTL}$$
$$\Rightarrow\ \text{次优：把 conv 的 } \mathtt{reap}\ \text{与 pack 的轮询合成同一个等待}（\text{同一种病，跨算子，量级 } \sim0.6\ s）\text{——但那不是“pack 桶”的活}$$

---

## 5. 纪律与门禁

| 门禁 | 结果 |
|---|---|
| 部署来源 | **`git archive HEAD` 快照**（`runs/board_t154/stage/`），**不用工作树**（工作树 `bin_net.py` = T-153 未提交改动） |
| `ps/` 与 `deps_closure.txt` 一起同步 | ✓ 每次重新部署；`--gen` 重生成后 `md5sum -c` **20/20**、`deps_closure --check` rc=0 |
| 位流 md5 | ✓ `602725b8` / hwh `c1686bd8`，跑前 + 跑后 |
| **单 net 规矩** | ✓ 256² A/B 全程同一个 net/engine（父代理纪律①）；128²/64² **各用全新 engine+arena+net** |
| 板上 ps/ md5 前后 | ✓ 一致（每次会话重新部署 + `md5sum -c`） |
| sha1 | **12/12**（256²）+ 2/2（128²）+ 2/2（64²）全中，含 4 个插桩跑 |
| 第 131 条 idle 门 | ✓ 每跑前后（`0x310=0` 且两 IP busy=0） |
| 旧进程 | 只按 pid `kill -TERM`；未用 `pkill -f`；`ulimit -c 0` |
| 主线改动 | **无**（`pynq_specialized_code/` 已 `git checkout` 复原；两个候选补丁留档在 `runs/board_t154/`） |
| 收尾 | 只 `sync; sync`，未 poweroff |

## 6. 产物

| 文件 | 内容 |
|---|---|
| `t154d_probe.log/.json` | 第 1 步诊断：桶细分、单命令五格、MMIO 原语、三尺寸 sha1 |
| `t154ab_probe.log/.json` | A/B 第 1 次（C 批量下发）⇒ **放弃** |
| `t154ab2_probe.log/.json` | A/B 第 2 次（C + 自校准睡）⇒ **放弃** |
| `t154s_probe.log/.json` | A/B 第 3 次（仅预测睡）⇒ **中性** |
| `patch_2a_cpath.diff` / `patch_2b_sleep_first.diff` | 两个候选补丁（都已否证，**不许合并**） |
| md5 | `patch_2a_cpath.py` = `464e3f1a75140cc4d75d480f5ed17d7e`；`patch_2b_sleep_first.py` = `db8d40dd3bd24e4aba4cd94cba1662de` |

## 7. 板子现状

$$\Rightarrow\ \text{仍开机：} \mathtt{uptime}=10{:}29,\ \text{1 user},\ \text{load } 1.49/1.28/1.05$$
$$\Rightarrow\ \text{CmaTotal } 262144\ \text{kB}；\text{CmaFree } 228544\ \text{kB}；\text{MemFree } 956660\ \text{kB}；\text{无残留探针进程}$$
$$\Rightarrow\ \text{位流跑后 md5 仍 } = 602725b8；\text{租约正常释放（rc=0）}$$
