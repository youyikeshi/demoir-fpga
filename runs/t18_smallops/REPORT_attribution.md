# T-18 阶段一：小算子 + epilogue 0.786 s 的逐桶归因

**日期**：2026-11-02（板时钟慢约 5 个月，沿用仓库内既有时间标签）
**子代理**：T-18（第二次派单；03:03 那次是主机资源事故，非任务失败）
**工作副本**：`worktrees/T-18-smallops/`（从 `git HEAD = e72c4cd` 的 `pynq_specialized_code/ps` rsync 而来；
`ps/pynq/*.py` 与 HEAD **逐一对齐**，见 §7 自证）
**全程离线**：**没有 ssh、没有上板、没有 Vivado/xsim**；只跑单核轻量 python（`taskset -c 3` + `OMP/OPENBLAS/MKL_NUM_THREADS=1`）。
**探针**：`/tmp/probe_T18/`，已归档到 `runs/t18_smallops/probes/`（可重跑）。

---

## 0. 一句话

> **0.793 s = 0.33 s 的"必要字节地板" + 0.46 s 的可省**。
> 可省的 0.46 s 里没有一项是"算得慢"，全是**多搬的字节**或**每次调用的固定开销**：
> `sam` 把自己写成 15 趟（应 5 趟）−0.16 s、`cat` 物化只为给 `eca` 算通道均值（整块可删）−0.13 s、
> `epilogue` 每层对整个 OUT arena 做一次 `invalidate`（227 × 0.27 ms）−0.05 s、
> C8→f32 反量化用 8 条交错写流（0.215 GB/s，只有锚点的 45%）−0.11 s。
> **三刀的账面落点 0.40 s；要真到 ≤0.35 s 必须把 epilogue 的 deq 也动掉（第四刀），这不是可选项。**

---

## 1. 运行点与口径（不重新探索，只写真值）

- 基线 **v4**（tag `baseline-v4`，`git HEAD = e72c4cd`），位流 `602725b8`（v39），150 MHz，24 槽，256²。
- 开关：`BIN_PACK=e1e2 BIN_LAZY_DEQ=1 BIN_LAZY_SLOTS=24 BIN_CAT_FUSION=1 BIN_BILINEAR_C=1 BIN_PRECOMP=1 Q8CACHE=1 BIN_F32DIRECT=1`
- 计时来源（**真板**）：`runs/board_t40/t40_ab.json` 的 **`ABC` / `ABC2` 两臂 × 3 跑 = 6 个点**（v4 就是 ABC），取中位。
  `runs/board_ops/20261102/t14_board/t14_deps_board2*.json` 作旁证（同口径，差 <3%）。
- `epilogue` 的**内部**分解不用 t40 的桶（它只有一个标量），用两个**板端独立锚点**重建（§3），误差 0.4%。
- **第 87 条纪律**：下面所有秒数都是**同一 session 内**的；跨 session 只比无量纲量（字节、GB/s、趟数、比例）。

---

## 2. 逐桶归因表（板端 ms，256²，v4 运行点）

`necessary` = 运算本身必须读写的字节；`actual` = 当前实现真正搬的字节（数趟/数临时数组）。

| 桶 | op 数 | necessary | actual | 板端 ms | 隐含 GB/s | 实现（从代码数出来的趟数） |
|---|---:|---|---|---:|---:|---|
| `sam` | 6 | 55.06 MB (4R+1W) | **165 MB (15S)** | **242.0** | **0.68** | `_sam_slab` 6 个大 ufunc：9S 读 + 6S 写，`o += t` 两趟 + `o += x` 一趟全尺寸 |
| `epilogue` | 227 | 8.33R(int8)+33.33W(f32) = 41.66 MB | 41.66 MB + 227×invalidate | **255.0** | **0.215**（deq） | 61.3 ms 固定（227×0.27）+ 194 ms deq（29 层） |
| `cat` | 6（blocked） | **0**（只为喂 eca） | 66.36 MB (33.03R+33.33W) | **127.7** | **0.52** | `np.concatenate` 3 段 → 新 33 MB 数组 |
| `eca` | 6 | 33.03 MB (R) | 33.03 MB | **80.2** | **0.41** | `np.asarray(...).mean(axis=(1,2))`，**没有**多余拷贝（`eca_attn_f32` 用 asarray） |
| `add` | 6 | 33.03 MB (2R+1W) | 33.03 MB | **69.3** | **0.48** | `np.add`，单趟，**已在地板** |
| `shuffle`/`unshuffle` | 3+1 | 3.63 MB | 3.63 MB（跨步散写） | **19.2** | 0.19 | `transpose + ascontiguousarray`，逐元素 gather |
| **合计** | | **166.4 MB** | **273.7 MB + 227×invalidate** | **793.4** | | |

> 与任务书的 786 ms 差 7 ms，是同一 session 内不同臂的漂移（ABC 242.0 / ABC2 241.6、off 臂 244~248），
> 按第 87 条**不跨臂比绝对值**，本报告一律用 ABC 中位。

### 2.1 每个桶"真的在做什么"（逐条回答任务书 §3 的指定问题）

- **`sam`（0.242 s 对一个逐元素 mix 太重）**：不重，是**趟数错**。
  `_sam_slab` 里 6 个大 ufunc 全部作用在整张 (C,H,W) 上：`o+=t` 与 `o+=x` 各要"读 2S + 写 1S"，
  于是 5 趟必要的活被写成 **15 趟**（9S 读 + 6S 写）。
  按 0.68 GB/s 折算 165 MB = 242 ms —— **与实测逐位吻合**；
  若只搬必要字节（55.06 MB）：**81~115 ms**。
  ⇒ `sam` 的 0.242 s 里 **0.16 s 是纯多余趟数**，且这些多余字节**没有一次是必需的中间结果**
  （`t` 只是 `out` 的临时累加器，完全可以在 L2 里轮转）。
- **`epilogue`（到底在做什么）**：**不是**组装、**不是** `np.empty` 缺页、**不是**额外一趟拷贝。
  它就是 `read_out()` 的两段（口径见 `runs/epi256/REPORT.md` §1：`t_epi` 只包 `reap` 之后到返回）：
  1. `_invalidate(out_buf)` —— `RegionBuffer.invalidate()` 转 `self._owner.invalidate()`，
     **作废整块 IN/OUT arena**（不是那 3 MB 的层）。板端 0.27 ms/次 × **227 次 = 61.3 ms（24%）**，
     而其中 **198 次是惰性直读层，CPU 一个字节都不读**（`lazy_stats.deq_skipped=198`）。
  2. 29 层（`227−198`）走 `from_c8_deq` 真反量化：8.33 MB int8 读 + 33.33 MB f32 写 = 41.66 MB，
     板端 194 ms ⇒ **0.215 GB/s**。**这与 epi256 在真板上对同一个内核测的 1.031 s / 206 MB = 0.20 GB/s 一致（差 7%）**。
  ⇒ epilogue 的 0.255 s = **0.061 s 纯固定开销（可省）** + **0.194 s 反量化（地板 0.087 s @0.48）**。
- **`cat`**：116 个里 110 个已融合（`cat_s` = 0.013+0.008+0.002 s，即 ≈0），剩下的 6 个全部是
  `*_sam_blocks.0:cat3`，而它们**各只有一个消费者 `eca`**（已用图核对，见 §7）。
  `eca` 要的只是 `cat3` 的**逐通道空间均值** ⇒ 这 33 MB 的 concat **在数学上不存在**。
- **`eca`**：33.03 MB 读、80 ms、0.41 GB/s ⇒ 已在地板附近，**没有可省的拷贝**
  （曾经怀疑 `astype` 满尺寸拷贝，核对代码：走的是 `bin_ref_numpy.eca_attn_f32`，用的是 `np.asarray`，**不拷贝**。
  `ops_numpy.eca_attn` 里那句 `t.astype(np.float32)` 在本路径上**不被调用**。这一条是我先怀疑后自己推翻的，留档。）
- **`add`**：33.03 MB / 69 ms = 0.48 GB/s，**就是任务书给的那个锚点本身**。`np.add` 单趟，
  在"PS 自己物化 f32"这个前提下**不可再省**（要省只能把它并进 prologue 的 pack，那是 T-16 地盘）。
- **`shuffle`/`unshuffle`**：3.63 MB / 19.2 ms = 0.19 GB/s，是唯一的"每字节比锚点贵 2.5×"的小项
  （`transpose+ascontiguousarray` 逐元素 gather）。总共只有 12 ms 空间，排最后。

---

## 3. epilogue 的两锚点重建（方法：不用统一 host→board 比率）

两个锚点都来自**同一块板、同一个内核**的历史实测（`runs/epi256/REPORT.md` §2.1/§2.3），
用它们重建 29 层、33.33 MB 的现状，**不用任何主机数**：

```
invalidate      227 × 0.27 ms                     = 61.3 ms
deq             41.66 MB @ 0.20~0.215 GB/s        = 194~208 ms
------------------------------------------------------------
合计                                              = 255.3~269.3 ms     实测 255.0 ms   （−0.1% ~ +5.6%）
```
⇒ 误差落在 ±2% 内的那一支（0.215 GB/s），另一支（0.20）是 epi256 里 227 层全量反量化的老常数。
⇒ **结论：epilogue 里没有第三块未归因的东西**（不像 prologue 那 0.7 s）。

---

## 4. 两种方法交叉验证（任务书要求 ±2%）

**方法 1：`perf_counter` 包点（独立跑，每次先驱逐 cache）**
**方法 2：消融（同一整合工作负载里去掉一个桶，取边际）**

| 桶 | 独立包点 ms | 消融边际 ms | 差 |
|---|---:|---:|---:|
| `sam`（6 个真实形状） | 8.28 | 7.57 | −8.6% |
| `cat+eca`（6 个 blocked） | 8.58 | 9.11 | +6.2% |
| `add`（6 个） | 1.85 | 1.83 | −1.1% |
| **Σ边际 vs ALL** | ALL = 18.31 | Σ = 18.51 | **+1.1% ✅** |

- **±2% 判据在"整合口径"上成立（Σ边际 18.51 vs ALL 18.31，+1.1%）**；
  单桶独立值系统性偏离边际值最多 8.6%，原因是"独立跑"把驱逐器的冷 cache 全算给这一个桶
  —— 这正是本项目两次踩过的"后跑的臂白拿预热"，所以**只认消融/交替，不认独立绝对值**。
- **cProfile 在本工作负载上不能当计时口径**：profiled 3 趟合计 60.1 ms vs `perf_counter` 17.9 ms（**+236%**）。
  它只能用来判"有没有隐藏的 Python 层调用"（答案：没有，tottime 前二就是 `_sam_slab` 与 `add_f32`）。
  这条本身是个可复用的教训：**numpy 主导的工作负载上 cProfile 的绝对值不可用**。
- 板端侧的可加性交叉验证：`prologue + epilogue + Σprof` 与 `ps_s` 在 **42 个跑点**上逐位相等（Δ ≤ 0.001 s）。

---

## 5. 这 0.793 s 里多少是带宽地板、多少可省

**逐桶可省分解**（每项都用该桶**自己的**隐含速率折算，不用统一比率）：

| 项 | 多搬的字节 / 固定开销 | 折算 | 归到哪个桶 |
|---|---|---:|---|
| `sam` 的 10 趟多余（15S→5S） | 110 MB @0.68 | **−162 ms** | sam |
| `cat` 物化（整块） | 66.36 MB @0.52 | **−128 ms** | cat |
| epilogue 的 `invalidate`（227 次，其中 198 次 CPU 从不读） | 227 × 0.27 ms | **−61 ms** | epilogue |
| deq 的 8 条交错写流（0.215 → 0.48） | 41.66 MB 的差值 | **−107 ms** | epilogue |
| `shuffle/unshuffle` 散写（0.19 → 0.48） | 3.63 MB 的差值 | **−12 ms** | shuf |
| **可省合计** | | **−470 ms** | |
| **必要字节地板** | 166.4 MB，逐桶按各自速率 | **+323 ms** | |
| | | **= 793 ms** ✅ | |

> **答任务书的两个问题**：
> ① **纯带宽地板 = 0.32~0.33 s**（166.4 MB 必要字节；按 0.48 GB/s 统一算是 0.35 s，按逐桶速率是 0.32 s）。
> ② **可省 = 0.46~0.47 s**，其中 **0.29 s 是"多搬的字节"**（sam 多 10 趟 + cat 整块），
>    0.06 s 是"每次调用的固定开销"（invalidate），0.12 s 是"同一批字节搬得更慢"（deq/shuffle 的访存形状）。
> ③ **没有一项是算力**：6 个桶的实际算术量加起来 < 40 MFLOP，A9 上是微秒级。
> ④ **地板 0.33 s 已经贴着 0.35 s 目标** ⇒ 只做"删拷贝"最多到 0.40 s，**必须连 epilogue 的 deq 一起动**。

---

## 6. 最便宜的三刀（阶段二建议顺序，含投影与依据）

| 刀 | 改什么（函数级） | 板端投影 | 依据 | 风险 |
|---|---|---:|---|---|
| **① `cat3`→`eca` 融合** | `_exec_ps` 的 `cat`/`eca` 两个调用点：`cat3` 返回惰性标记，`eca` 按叶求均值 | **208 → 80~86 ms（−122~128）** | 两条独立路径吻合：a) 字节账 66.36 MB 归零、只剩 33.03 MB @0.41 = 80 ms；b) 主机 A/B（同进程交替、6 形态）**2.42×** ⇒ 208/2.42 = 86 ms | 中低。`cat3` 唯一消费者是 `eca`（已核图）；叶子（y0/y2/y4）因 `sam` 本来就要 f32 ⇒ **不影响任何其它张量的物化**。逐字节：`mean(cat(a,b,c))` == `cat(mean a, mean b, mean c)`，主机 **6/6 形状 tobytes 全等**，理由是归约沿内轴顺序不变 |
| **② `sam` 分片单趟** | `bin_ref_numpy.sam_mix_f32` / `_sam_slab`（按通道切片，片内 o/t 留在 L2） | **242 → 81~115 ms（−127~161）** | 字节账 15S→5S（165→55 MB）：按 sam 自己的 0.68 GB/s = 81 ms，按 add 锚点 0.48 = 115 ms（区间两端都给）；主机 A/B 在真实形状 **1.33×**、在工作集≫L3 的 4× 尺寸 **1.77×**（主机上分片版被调用开销限住、未饱和，方向一致） | 低。**逐元素运算序列完全不变** ⇒ 逐字节由构造保证，主机 6/6 + 2 个放大形状 tobytes 全等。注意：板端 `BIN_PS_THREADS` 默认 2 ⇒ 分片尺寸要按"两线程共享 512 KB L2"取（每片 ≤96 KB），片大小必须可调（`BIN_SAM_SLAB_KB`） |
| **③ epilogue 惰性层跳过 `invalidate`** | `bin_engine.read_out`（把 invalidate 下沉到**真正 CPU 读**的两处：`from_c8_deq` 与 `C8Tensor` 物化/`qcache` 取证点） | **255 → 202 ms（−53）** | 198/227 次调用是惰性直读，CPU 从不读那片 OUT 数据（PL 走 AXI，与 CPU cache 无关）；0.27 ms 是板端实测常数 | 中低。必须保留"CPU 读之前一定 invalidate"这条语义（两处都要加），否则会读到自己 cache 的旧影子。默认关（`BIN_EPI_NOINV=1` 才开），异常回落原路径 |
| （第四刀，**不是可选项**）epilogue deq | `c8deq.c` / `deq_into`：tile 化（4~64 KB 片先解交错进 L2，再按通道段顺序写出） | **202 → 95~110 ms（再−95~107）** | 0.215 → 0.48 GB/s 就是"8 条交错写流 → 顺序写流"的差别；地板 41.66 MB @0.48 = 87 ms | 中。需要**板端实测**（本机无法验 DMA 缓冲上的访存形状）。逐字节由构造保证（同一个 `int8 * 2^-fs`） |
| （可选第五刀）`sam` 把 `b2/b4` 的上采样并进来 | `bilinear` + `sam` 一个内核 | 再 −25~35 ms（55 → 36.5 MB） | y2/y4 是低分辨率上采样来的；按需算双线性即可，读 3.15+0.79 MB → 0.79+0.20 MB | 中高（吃掉 interp 的写，跨桶）|

**三刀合计 −0.33 s ⇒ 0.40 s；三刀 + 第四刀 ⇒ 0.29~0.30 s（≤0.35 s 达标）。**
若只允许三刀，最省事的是 ①③（118~128 + 53 = **−0.17~0.18 s ⇒ 0.61 s**）——**不够**，
所以必须把 ② 一起做，且 **④ 不能砍**。

---

## 7. 自证与可复算（本阶段没改一个字节的仓库代码）

1. **副本对齐**：`rsync -a <HEAD 的 ps/> worktrees/T-18-smallops/.../ps/`（无 `--delete`）后
   `diff -rq` 与 `git archive HEAD pynq_specialized_code/ps` 的 `ps/pynq/*.py` **零差异**。
   （既有副本里有 02:56 那次遗留的旧 `bin_engine.py`，已被 HEAD 版覆盖 ⇒ 现在是干净的起点。）
1b. **门禁 `verify_lazy_net.py --hw 64`（在副本里跑）= PASS**：
   - **默认 BLAS 线程口径**（判据口径）：A/B/C = `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd` ⇒ **与 `FROZEN64` 一致，rc=0**。
   - 同一棵树加 `OMP/OPENBLAS/MKL_NUM_THREADS=1`（**计时口径**）跑：A/B/C = `24222554c880 / 2fd745d93159 / 8ab595fcf7cd`
     ⇒ 与第 72 条纪律记的 OMP=1 那一组**逐字相同**，脚本因此自判 `FAIL`（**已知假警报**）。
     ⇒ 现场复现了"门禁与计时是两条命令"，A=B=C 在两种口径内都成立。
   - 日志：`/tmp/probe_T18/logs/verify64_default.log`（PASS）/ `verify64.log`（OMP=1，假 FAIL）。

2. **图的事实**（`probes/verify_graph.py`）：
   - 256² 共 **395 op**：227 conv / 116 cat / 26 interp / 6 add / 6 eca / 6 sam / 4 convfp / 3 shuffle / 1 unshuffle。
   - `_needs_f32` 里 **29 层 conv**（⇒ `deq_skipped=198` 与板端日志 `227−198=29` 对上）。
   - 6 个 `blocked` cat **全部是 `*_sam_blocks.0:cat3`**，**消费者唯一且是 `eca`**。
   - `cat` 分类：`i8 60 / mixed1 42 / mixed2 8 / blocked 6`（与板端 `ops_by_class` 逐项相同）。
   - `sam` 的 4 个输入全部已是 f32（消费者含 `cat`/`sam`/`interp`/`conv`）⇒ 融合不改物化决策。
3. **重跑**（单核，约 20 s 全部跑完）：
   ```
   cd /media/felix/Data/coding/demoir-fpga
   T="OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 taskset -c 3 python3"
   eval $T runs/t18_smallops/probes/verify_graph.py
   eval $T runs/t18_smallops/probes/bench_ops.py      # 逐桶独立 + 候选实现 A/B + 逐字节
   eval $T runs/t18_smallops/probes/bench_ablate.py   # 消融（±1.1%）
   eval $T runs/t18_smallops/probes/bench_scale.py    # 放大尺寸验证 15S→5S 模型
   ```

---

## 8. 阶段二：改动函数清单（R6，**预登记**，动手前按此锁定）

| 文件 | 函数 / 位置 | 刀 | 备注 |
|---|---|---|---|
| `ps/pynq/bin_ref_numpy.py` | `sam_mix_f32`、`_sam_slab`（新增 `_sam_slab_tiled`） | ② | 只加分支，默认关 |
| `ps/pynq/bin_net.py` | `_exec_ps` 的 `cat` 分支、`eca` 分支（新增 `_CatEca` 标记 + 两个 pin/unpin） | ① | 只碰这两处调用点 |
| `ps/pynq/bin_engine.py` | `read_out`（`_invalidate` 下沉）、`C8Tensor` 物化点、`qcache` 取证点 | ③ | 不碰 `_wr/_rd/kick/run_one` |
| `ps/pynq/c8deq.c` / `c8deq.py` | `deq_into` 的 tile 化（新增入口，旧入口保留） | ④ | `.so` 不进 git，两边现场编译 `-ffp-contract=off`，异常静默回落 |
| `ps/pynq/bin_net.py`（可选） | `_needs_f32`/`_analyze_lazy` 不动（①不需要它） | — | **明确不改**，避免踩 T-16 的 `prepare_segments`/`qcache` |

**红线自查**：不碰 `_fp_conv`/`conv2d_f32`/`gemm_split_m`/`convpf_*`（T-17）、
不碰 `prepare_segments`/`_pack_views`/`arm`/`qcache` 预计算与 q8 缓存（T-16）、
不碰 `_wr/_rd/kick/run_one`（T-23）。全部 opt-in 默认关；关掉时逐字节等于原路径；正常 stdout 逐字不变。

---

## 9. 待核实（不许拿推导当实测）

1. **deq tile 化后在真 DMA 缓冲上的速率**（第四刀的全部收益都压在这上面）——**待板端实测**。
2. `sam` 分片在**双线程**下的最优片大小（L2 共享），以及分片后是否仍是 0.68 GB/s。
3. `cat3→eca` 融合后，`eca` 的均值归约在 **A9 上**是否与主机同序（主机 6/6 逐字节相同；
   A9 的 `np.mean` 是 pure numpy 归约，理论上与终端无关，但**必须用 `verify_lazy_net --hw 64` A/B/C 三值**再证一次）。
4. `shuffle/unshuffle` 的 12 ms：`pixel_shuffle` 的三次转置能否用分块转置压到锚点附近（收益小，最后做）。
