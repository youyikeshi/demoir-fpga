# T-151 ★ 当前工作点上重测 PS 分桶：**"PS 3.07~3.33 s" 是个漏账——5.96 s 里 PS 侧实花 5.76 s，其中 1.12 s 被记在"PL"名下**

> 日期 2026-11-03 14:14（板端本地时间）｜一个租约 = 一个进程 = 一次 load
> 位流 v39 `602725b86941864eeeeab3be005e1e81`（跑前/跑后 md5 均校验）｜@**150 MHz**（读回）
> hw = 256²、slots = 24、**零护栏**（一次不睡）｜工作点 = `e1e2`+`LAZY_DEQ=1`+`CAT_FUSION=1`
> +`BILINEAR_C`+`PRECOMP=1`+`Q8CACHE=1`+`F32DIRECT=1`+`CAT_ECA_FUSE=1`+`EPI_NOINV=1`+`PROFILE=1`
> **判据：4/4 跑三 sha1 = `9b1940c8`/`f86a7035`/`025757a8` 全中，互相同；残差 2.5~3.3%；零段错误/零金丝雀/零超时。**

---

## 0. 一句话结论

$$\Rightarrow\ \text{墙钟 }\mathbf{5.959\ s}\ \text{被「互不重叠」地拆成：}$$
$$\quad\ \underbrace{\mathbf{3.97}}_{\text{PS-CPU}}(\text{P1 }0.67 + \text{prep }1.04 + \text{非卷积 }1.99 + \text{epi }0.24 + \text{poll }0.03)
+ \underbrace{\mathbf{1.12}}_{\text{E1E2 pack}} + 0.05\,(\text{kick 余}) + \underbrace{0.62}_{\text{等 conv}}+ \underbrace{0.20}_{\text{循环}} = \mathbf{5.96}$$
$$\Rightarrow\ \textbf{旧口径错在三处}：$$
$$\quad\ \textbf{① } \text{“PL } 2.23\ \text{s” 里 } \mathbf{1.13\ s\ \text{是 } E1E2\ \text{打包（PS 发起、串行 blocking）}}；conv\_uni\ \text{真算只有 } \mathbf{1.10\ s}$$
$$\quad\ \textbf{② } \text{“PS 3.4 s” }\textbf{漏了 } \mathtt{_analyze\_lazy}\ \mathbf{0.55\ s}\ \text{和 poll }0.03\ (\text{都不在旧三桶里})$$
$$\quad\ \textbf{③ } \text{overlap }0.58\ \text{是**视图**}（\subset \text{prep+psop}）\Rightarrow \text{绝不能与三桶相加}（\text{§55 加不平的真因）}$$
$$\Rightarrow\ \Rightarrow\ \textbf{“PS 那 3.2 s 还能拿多少”的正确答案是：} \mathbf{1.0\sim1.3\ s}，\text{且前两点（} \mathtt{\_analyze\_lazy}\ \text{与 convfp）不碰 RTL。}$$

---

## 1. ★ 分桶表（run3 = 稳态，墙钟 **5.959 s**，Σ 误差 **0.0%**）

| 桶 | 秒 | 占墙钟 | 口径（从哪到哪） |
|---|---|---|---|
| **P1 循环前固定开销** | **0.667** | 11.2% | `run()` 起 → 第一个 op 的 `prepare()` 前；**每次 run 都付** |
| ├ `_analyze_lazy` | 0.547 | 9.2% | 395 op + 116 cat 的 `needs_f32` 定点迭代（纯 Python） |
| ├ `build_graph` | 0.084 | 1.4% | 建图 |
| └ 后端/槽视图/e1e2 等 | 0.036 | 0.6% | `_PlBackend` + 48 槽视图 + 金丝雀 |
| **prologue：prepare** | **1.044** | 17.5% | 每层 `_PlBackend.prepare`（**不含**等 PL） |
| ├ 量化缓存 `_q8_segments` | 0.502 | 8.4% | 50 次未命中，产出 17.59 MB int8（f32 读 70.3 MB）⇒ **175 MB/s** |
| ├ 段表 `prepare_segments` | 0.186 | 3.1% | 其中 desc 0.052 + f32 段量化 0.077 |
| ├ cfg 写配置页 + 金丝雀 | 0.117 | 2.0% | 227 层 × 27 次 MMIO 写 |
| ├ 槽获取 | 0.099 | 1.7% | `_acquire_slot` |
| └ 分类/惰性物化/其余 | 0.140 | 2.3% | `pack_input`=0、`push_input`=0（硬计数） |
| **kick（发 start）** | **1.169** | 19.6% | 每层 `kick`：**其中 E1E2 `run_prepared` = 1.118** |
| **非卷积算子（168 op）** | **1.994** | 33.5% | 按类型见 §2 |
| **epilogue** | 0.240 | 4.0% | 反量化/钉槽/read_out |
| **wait-for-PL** | 0.620 | 10.4% | 等 conv_uni done（reap） |
| **poll** | 0.029 | 0.5% | 循环顶 MMIO 读（**旧桶完全没有这一项**） |
| **其它（Python 循环）** | 0.195 | 3.3% | 差分残差 |
| **Σ** | **5.959** | **100.0%** | 校验：Σ − 墙钟 = **−0.000 s** |

$$\text{视图（不参与求和）}：\text{overlap } \mathbf{0.583}\ \text{s}\ (\text{与 conv 并行做掉的 PS 时间})；\ \text{PL 口径 } 2.229 = \mathbf{1.126\ (pack)} + \mathbf{1.103\ (conv)}$$

**同 session 其余三跑（复现性）**

| 跑 | 臂 | 墙钟 | P1 | prep | 非卷积 | epi | wait | kick | 残差 | sha1 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 全新 engine+net | 7.872 | 1.026 | 1.624 | 2.827 | 0.257 | 0.724 | 1.188 | 2.5% | ✓ |
| 2 | 复用同一 net | 6.096 | 0.671 | 1.152 | 2.010 | 0.245 | 0.620 | 1.176 | 3.1% | ✓ |
| 3 | 复用同一 net | **5.959** | 0.667 | 1.044 | 1.994 | 0.240 | 0.620 | 1.169 | 3.3% | ✓ |
| 4 | 再做一次全新 | 6.701 | 1.022 | 1.360 | 2.057 | 0.244 | 0.616 | 1.183 | 2.9% | ✓ |

---

## 2. 非卷积算子：168 个 op 里 62% 的时间在 **4 个 convfp** 上

| kind | 秒 | 占非卷积 | n | 单次均 | 最大 | in+out | 有效带宽 |
|---|---|---|---|---|---|---|---|
| **convfp** | **1.246** | **62.4%** | 4 | **311.5 ms** | 548.1 ms | 10.5 MB | **8.4 MB/s** |
| interp | 0.297 | 14.2% | 26 | 10.9 ms | 35.3 ms | 57.5 MB | 202 MB/s |
| sam | 0.251 | 12.6% | 6 | 41.8 ms | 85.0 ms | 55.1 MB | 219 MB/s |
| add | 0.083 | 4.2% | 6 | 13.8 ms | 33.3 ms | 33.0 MB | 398 MB/s |
| eca | 0.078 | 3.9% | 6 | 13.0 ms | 23.1 ms | 33.0 MB | 424 MB/s |
| **cat** | **0.037** | 1.9% | **116** | **0.32 ms** | 0.50 ms | 1003.6 MB | 27.5 GB/s（**惰性，物理不存在**） |
| shuffle | 0.014 | 0.7% | 3 | 4.8 ms | 10.9 ms | 2.1 MB | 143 MB/s |
| unshuffle | 0.005 | 0.2% | 1 | 4.7 ms | 4.7 ms | 1.6 MB | 335 MB/s |
| **合计** | **1.994** | 100% | **168** | — | — | — | — |

$$\Rightarrow\ \textbf{cat 已经被完全治好了}（116\ op\ \text{合计 }36\ \text{ms}，\text{惰性 110/116、回落 0、物化 6 个 blocked）\Rightarrow \text{别再往 cat 上投资}$$
$$\Rightarrow\ \text{convfp 4 层恰好 } \mathbf{0.769\ GFLOP}（\text{与 PLAN 记录一致}）\Rightarrow \mathbf{0.61\ GFLOP/s}$$

| convfp 层 | 形状（w 的 layout） | in/out | 时间 | GFLOP | GFLOP/s |
|---|---|---|---|---|---|
| `encoder.conv_first.0` | (48,12,5,5) @128² | 12→48ch | 0.488 s | 0.472 | 0.97 |
| `decoder.decoder_1.conv` | (12,64,3,3) @128² | 64→12ch | 0.572 s | 0.227 | 0.40 |
| `decoder.decoder_2.conv` | (12,64,3,3) @64² | 64→12ch | 0.148 s | 0.057 | 0.38 |
| `decoder.decoder_3.conv` | (12,64,3,3) @32² | 64→12ch | 0.062 s | 0.014 | 0.23 |

$$\Rightarrow\ \text{这是**纯算力受限**（10.5 MB 数据、8.4 MB/s）}；\text{瓶颈是这板上的 } \mathtt{numpy}\ \text{BLAS（sgemm）}+ \text{im2col}$$

---

## 3. ★ top3 可压缩项 + 各自上限

### #1 E1E2 pack：**1.118 s（18.8%）→ 上限 −1.0 s**，且 217/227 层是**空搬**
$$\Rightarrow\ \text{它是什么}：\text{每层 conv 之前，} \mathtt{E1E2}\ \text{单元把“本层输入”搬/排成 conv 的 IN 缓冲；} \mathtt{kick}()\ \text{里 \textbf{同步阻塞}}$$
$$\Rightarrow\ \textbf{为什么这么久（关键）}：\text{227 层共只搬 } \mathbf{19.1\ MB} \Rightarrow \mathbf{16.9\ MB/s}$$
$$\quad\ \text{而 CMA memcpy 实测 } 655.8\ \text{MB/s}、\mathtt{q8\_c8}\ \text{内核 } 232\ \text{MB/s} \Rightarrow \text{比 memcpy 慢 } \mathbf{39\times} \Rightarrow \textbf{不是带宽，是每层 } \mathbf{\sim5\ ms}\ \text{的固定握手}$$
$$\quad\ \textbf{而且 217/227 层 }\mathtt{n\_f32=0}\ (\text{纯 int8 段})：\text{源就是上一层 conv 的 OUT 槽、布局**逐字节已就位**} \Rightarrow \text{这一次搬**语义上是恒等复制**}$$
$$\quad\ \text{证据：}217\ \text{层 } 15.5\ \text{MB} / 0.978\ \text{s}；\text{而它们的 conv 合计 } 0.980\ \text{s} \Rightarrow \textbf{pack/conv} = 1.00\ (\text{一对一，纯串行})$$
$$\Rightarrow\ \textbf{理论下限 } \mathbf{\approx 0}：\text{描述符里已经带源 } \mathtt{pa} \Rightarrow \text{让 conv 的 in base 直接指源槽（零拷贝）}$$
$$\quad\ \text{次优（不碰 RTL）：与 conv 重叠——两个独立 AXI 主口，conv 那 } 1.10\ \text{s 足够盖住 } 1.12\ \text{s}$$
$$\Rightarrow\ \textbf{要改什么}：① PS：\mathtt{n\_f32==0} 且单段时跳过 \mathtt{run\_prepared}（并把 in base 指向源）；② 若要重叠：\text{解掉“} \mathtt{kick}\ \text{串行”与 done 位耦合}$$
$$\quad\ ⚠ \text{本任务不许动主线}；\text{这是**下一轮最大的单点**，且需要一次上板 A/B（sha1 必须逐位不变）}$$

### #2 convfp：**1.243 s（20.9%）→ 上限 −0.85 s（保守 −0.5 s）**
$$\Rightarrow\ \text{它是什么}：4\ \text{个 f32 卷积在 PS 上跑}（\mathtt{im2col} + \mathtt{numpy}\ \text{sgemm}），\text{不挂 PL 的 fconv}$$
$$\Rightarrow\ \text{为什么慢}：0.61\ \text{GFLOP/s}；\text{A9 NEON 单核 sgemm 现实可达 } 1.5\sim2.5\ \text{GFLOP/s}$$
$$\quad\ \text{最小那层 } 32^2\ (12.2\ \text{k}\times 3\ \text{ch})\ \text{只用 } 0.23\ \text{GFLOP/s} \Rightarrow \text{说明有相当一部分是每调用固定开销}$$
$$\Rightarrow\ \textbf{理论下限}：0.769\ \text{GFLOP} / 2\ \text{GFLOP/s} = \mathbf{0.38\ s} \Rightarrow \textbf{上限 } −0.85\ \text{s}$$
$$\Rightarrow\ \textbf{最便宜的一步}：\mathtt{BIN\_CONVPF\_C=1}（\text{树里已有、默认关的 NEON im2col-free 内核，同一份报告里对它自己的实测是 } 1.66\times）$$
$$\quad\ \Rightarrow\ \text{预期 } -0.4\sim-0.5\ \text{s}；\text{判据 = “内核 vs 严格串行参考 0 差”（它本来就是 K 向串行累加）}$$
$$\quad\ \textbf{⚠ 但这是一次\textbf{数值口径变更}，不是纯加速}：\text{现在走的 } \mathtt{numpy}\ \text{sgemm 不保证“K 向严格串行”，}$$
$$\quad\ \text{换成 C 内核后末位可能变 } \Rightarrow \textbf{冻结三 sha1 会变} \Rightarrow \text{要么主代理先定“换参照”并重冻 } 54/54，$$
$$\quad\ \text{要么走“BLAS 等价”的实现（保累加序 ⇒ 收益会小很多）}。\textbf{本项必须由主代理拍板，不能子代理自行放宽}。$$

### #3 `_analyze_lazy`：**0.547 s（9.2%）→ 上限 −0.52 s（零数值风险）**
$$\Rightarrow\ \text{它是什么}：\text{每次 } \mathtt{run()}\ \text{里重算“哪些张量必须留 f32”（} \mathtt{needs\_f32}\text{）与每个 int8 张量的消费计数}$$
$$\quad\ \text{实现是 } \mathtt{while\ changed:}\ \text{对 116 个 cat 反复扫 + 集合运算} \Rightarrow \mathbf{0.547\ s}\ \text{花在 395 个 op 的纯 Python 上}$$
$$\Rightarrow\ \textbf{理论下限}：\mathbf{0.01\sim0.03\ s}（\text{按逆拓扑**一趟**算完，或按 } (H,W,\text{plan})\ \text{缓存）$$
$$\Rightarrow\ \textbf{要改什么}：\text{把结果挂到 net 上按图签名缓存——图只依赖 } (H,W)\ \text{与容器，跨帧完全可复用} \Rightarrow -0.52\ \text{s}$$

### 其它（都不是前 3，但别再猜）
| 项 | 秒 | 界 | 判断 |
|---|---|---|---|
| q8cache（f32 叶子→int8） | 0.502 | 88 MB / 232 MB/s ≈ 0.38 ⇒ −0.12 | **已经贴着内核速率跑（175 MB/s），不是漏洞**；真要拿只能改内核或消灭 f32 叶子 |
| wait-for-PL | 0.620 | conv 1.10 s 的未重叠部分 | 要降只能让 conv 更快（RTL 路线）或把 PS 活更多塞进这段 |
| epilogue | 0.240 | 29 层必须物化 | 已由 `deq_skipped=198` 保住 |
| cfg+金丝雀 | 0.117 | 227×27 MMIO 写 | 已带影子缓存，**cfg 命中 0/227**（槽轮换 ⇒ 每次都真写），可做“按槽记住配置” |
| 槽获取 | 0.099 | — | 纯 Python，可向量化 |
| interp/sam/add/eca | 0.696 | 200~420 MB/s | 已接近 CMA 带宽，**不是杠杆** |

---

## 4. 每 op 固定开销 × 227 ⇒ **2.41 s = 墙钟的 40%**

$$\Rightarrow\ \text{每层稳态势：prep } 4.60\ \text{ms} + pack\ 4.96\ \text{ms} + epi\ 1.06\ \text{ms} = \mathbf{10.6\ ms}；\times 227 = \mathbf{2.41\ s}$$
$$\Rightarrow\ \text{而 conv 真算 } 1.10\ s（\text{每层 } 4.86\ \text{ms}）$$
$$\Rightarrow\ \textbf{最刺眼的证据：22 个 } 8\times8\ \text{层，每层 } 4.92\ \text{ms}\ \text{里 conv 只占 } \mathbf{0.67\ ms}\ (14\%)}；16\times16\ \text{的 } 44\ \text{层}：5.67\ \text{ms}\ \text{里 } 1.11\ \text{ms}\ (20\%)$$
$$\quad\ \text{（逐分辨率：}8^2\ \text{conv}\,0.67/\text{pack}\,2.52/\text{prep}\,1.73\ \text{ms}；\ 128^2\ \text{conv}\,15.1/\text{pack}\,13.1/\text{prep}\,14.7\ \text{ms）}$$
$$\Rightarrow\ \Rightarrow\ \text{“把大算子再优化 10\%”收益 } <0.1\ s；\text{“把每 op 固定开销砍半”收益 } \mathbf{\sim1.2\ s}$$
$$\Rightarrow\ \text{其中 pack（4.96 ms/层）与 prep 的 q8cache+cfg+槽（\sim3.3 ms/层）是两个可砍的对象}$$

---

## 5. prologue 第 1 次 vs 第 2/3 次：**常驻进程值 1.78~1.91 s/帧**

| | 墙钟 | P1 | prep | 非卷积 | kick | 说明 |
|---|---|---|---|---|---|---|
| run1（全新 engine+net） | **7.872** | 1.026 | 1.624 | 2.827 | 1.188 | 冷进程首次 |
| run2（同 net） | 6.096 | 0.671 | 1.152 | 2.010 | 1.176 | **−1.776 s（−22.6%）** |
| run3（同 net） | **5.959** | 0.667 | 1.044 | 1.994 | 1.169 | **−1.913 s（−24.3%）** |
| run4（再做一次全新） | 6.701 | 1.022 | 1.360 | 2.057 | 1.183 | −1.170 s（−14.9%） |

$$\Rightarrow\ \textbf{差的 1.9 s 落在哪}（run1→run3）：\text{非卷积 } -0.833（\text{几乎全是 } \mathtt{interp}\ 1.076\to0.297）;\ \text{P1 } -0.359（\mathtt{enable\_e1e2}\ 0.39\ \text{只付一次}）$$
$$\quad\ \text{prep } -0.580（\text{q8cache } 0.68\to0.50 + \text{cfg } 0.23\to0.12）;\ \text{其余 } -0.1$$
$$\Rightarrow\ \Rightarrow\ \textbf{部署结论}：\text{常驻进程（一次 load、多帧前向）第一帧后每帧 } \mathbf{5.96\ s}；\text{每帧重启进程则 } \mathbf{7.87\ s}$$
$$\Rightarrow\ \textbf{进程级一次性（P0，**不在 6 s 里**，但重启就要付）}：\text{解释器 } 1.02 + \text{import } 14.0 + \text{Overlay } 2.74 + \text{内核 .so } 6.37\ (\mathtt{c8deq}\ 4.37/\mathtt{bpack}\ 1.80/\mathtt{q8}\ 0.28) + \text{arena } 0.61 + \text{权重 } 0.14 + \text{plan } 0.14$$
$$\quad\ \Rightarrow\ \mathbf{\approx 25\ s}\ \text{每重启一次}（\text{已核对：} \text{首个 op 下发} = +23.7\ \text{s 之外的 } 0.3\ \text{s}）$$

---

## 6. 若要 256² ≤ 5 s：**能，而且不必碰 RTL / 不必重建位流**

$$\Rightarrow\ \text{先把题意纠正}：\text{“PL } 2.3\ \text{s 不变 ⇒ PS ≤2.7 s” 的分母错了——PL 那 } 2.229\ \text{s 里 } 1.126\ \text{是 pack（PS 发起）}$$
$$\Rightarrow\ \text{真实串行链}：\mathbf{PS\text{-}CPU } 3.16 + pack\ 1.12 + \text{等 conv } 0.62 + \text{循环 } 0.20 = 5.96$$
$$\Rightarrow\ \text{要 } \le 5.0\ \text{s} \Rightarrow \text{砍 } \mathbf{0.96\ s}$$

| 组合 | 省 | 总 | 性质 |
|---|---|---|---|
| `_analyze_lazy` 缓存 **+** convfp 换 C 内核 | −0.52 − 0.45 = **−0.97** | **4.99 s** | 纯 PS，不碰 RTL/位流 |
| 上面 **+** convfp 打满（2 GFLOP/s） | −0.52 − 0.85 = −1.37 | 4.59 s | 需重写 gemm（NEON sgemm / 分块） |
| 上面 **+** pack 重叠/零拷贝 | −0.52 − 0.85 − 1.00 | **3.7 s** | 需 PS 主线 + 一次上板 A/B |

$$\Rightarrow\ \textbf{回答“PS 能否 ≤2.7 s”}：\text{旧口径 } 3.415 \Rightarrow \text{目标 } 2.7\ \text{差 } 0.715$$
$$\quad\ \text{convfp 一项就够：}1.243 \to 0.50 \Rightarrow \text{旧口径 } =\mathbf{2.67}\ \checkmark$$
$$\quad\ \text{或 } \mathtt{\_analyze\_lazy}\ (-0.55) + \text{q8cache } (-0.12) = 2.74（\text{差一口气，因为 P1 本来就不在旧口径里}）$$
$$\Rightarrow\ \textbf{但“PS ≤2.7”不是正确的目标}：\text{旧口径漏掉的 } 0.70\ \text{s（P1）}+\text{pack } 1.12\ \text{不在其中}$$
$$\quad\ \Rightarrow\ \textbf{要盯的是墙钟}：\text{上表第一行就已 } \mathbf{≤5\ s}，\text{且完全在 PS 侧、零 RTL 风险}$$

---

## 7. 板子现状（收尾）

$$\Rightarrow\ \text{仍开机：} \mathtt{uptime} = 9{:}54,\ \text{1 user},\ \text{运行本 session 后 load } 1.11/0.78/0.36\ (\text{衰减中})$$
$$\Rightarrow\ \text{CmaTotal } 262144\ \text{kB}；\text{CmaFree } \mathbf{151268\ kB}\ (\text{跑前 } 87232\ \text{kB，跑后已回补})；\text{MemFree } 779824\ \text{kB}；\text{Swap } 524284/524284$$
$$\Rightarrow\ \text{磁盘 } /\ 28\ \text{G，用 } 34\%；\text{inode 充裕}；\text{无残留 probe 进程}；\text{位流跑后 md5 仍 } = 602725b8$$
$$\Rightarrow\ \text{收尾只 } \mathtt{sync;sync}（\text{未 poweroff}）；\text{租约正常释放（rc=0）}$$

---

## 8. 纪律与门禁执行

| 门禁 | 结果 |
|---|---|
| 冻结位流 md5（bit/hwh 成对） | ✓ `602725b8` / `c1686bd8`，跑前+跑后 |
| ps/ 与 `deps_closure.txt` **一起**同步 | ✓ 389 文件解包，`md5sum -c` 158/158 OK |
| `deps_closure.py --check` | ✓ rc=0（14 模块 / 20 文件） |
| 第 131 条 idle 门（0x310=0 **且**两 IP AP busy=0） | ✓ 4 跑 × 跑前跑后全过 |
| `0x314` magic（v39 无） | 读回 `0x0`，按 T-141 豁免 |
| 护栏/睡眠 | **睡 0 次、`push_input` 0 次**（硬计数） |
| sha1 | **4/4 = 冻结三件套**，互相同 |
| 分桶闭合 | Σ − 墙钟 = **−0.000 s**；各跑残差 2.5~3.3%（<5%） |
| 旧进程 | 只按 pid `kill -TERM`；未用 `pkill -f`；`ulimit -c 0` |
| 主线改动 | **0**（全部计时都在探针的运行时钩子里） |

---

## 9. 证据清单

| 文件 | 内容 |
|---|---|
| `probe_t151.py` | 探针（运行时钩子 + 分桶 + 时间线），md5 见 `MD5SUMS` |
| `run_t151.sh` | 唯一的运行脚本（租约/令牌/门禁/scp/收尾） |
| `t151a_run.log` | 本次完整 stdout（4 跑 + 7 个汇总表） |
| `t151a_*.json` | 609 KB 原始账本（每跑 227 层的 prep/pack/PL/epi、168 个 PS op、口径内所有桶） |
| `ps_md5_host.txt` / `ps_md5_board_check.txt` | 主机/板端逐文件 md5 对照（158 OK） |
| `ps_deps_md5sum_c.txt` | `deps_closure.txt` 的 `md5sum -c`（20 OK） |
| `t151a_bit_after.md5` | 跑后位流 md5 = 冻结值 |
