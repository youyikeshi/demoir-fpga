# 28 s/帧 里那 18 s 是什么 —— 纯主机代码分析（2026-11-04）

**只读代码与已有日志**；没碰板子、没改 PS 生产代码。所有数字都带出处，推算式标了"推算"。

---

## 0 一句话答案

$$
18\ s\ \approx\ \underbrace{5\sim7\ s}_{\text{一次性：3 个 C 内核 load+自检、Overlay 下载}}\ +\ \underbrace{10\sim12\ s}_{\textbf{BV\_FREE=0}：726\ MB\ 中间张量常驻\Rightarrow超出\ 880\ MB\Rightarrow\text{SD 卡 swap}}\ +\ \underbrace{0.5\sim0.9\ s}_{\text{每帧 Python 重组装（build\_graph/plan）}}
$$

**根因不是"每帧重打包权重"**（这条我否掉了，见 §1.3），而是 **①口径不同**（28.4 s 是冷启动单跑，
10.3 s 是同进程第 2 跑）+ **②`BV_FREE` 没开**（一行环境变量，`bin_net.py:3189`）。

---

## 1 逐条回答

### 1.1 `net.run()` 里哪些是**每帧重复**的？（`bin_net.py:3069` 起）

| # | 每帧都做的事 | 行号 | 每帧成本 | 备注 |
|---|---|---|---|---|
| 1 | `BE.report_deps()` + `BE.deps_warm()` | `bin_net.py:3071-3072` | µs 级 | 只打印/查标志 |
| 2 | **`build_graph(t)`**：整张图重建（396 个 op 对象、layer 包装、张量对象） | `bin_net.py:3077` | **0.13~0.53 s** | `runs/t16_prologue/REPORT_attribution.md:121` 量到"534 ms 是 Python 组装" |
| 3 | `_analyze_lazy(g, outs)`（惰性槽分析） | `bin_net.py:3171` | 十几 ms | 纯 Python 遍历 |
| 4 | `BinArena(need)` **仅当 `arena=None`** | `bin_net.py:3211` | 0（外部传入时） | run100 / `dump_board_int8` / `demo_pipeline` 都传了 `arena=`，`dump_board_int8.py:274` 有注释："不传 arena 时第 2 张就被 Killed" |
| 5 | **`engine.add_region(arena.buf,"net")`** | `bin_net.py:3212` | µs | ⚠ **`_regions` 每帧 append 一条**（`bin_engine.py:1263`），100 帧 = 100 条；只影响护栏扫描，不是性能问题，但是"只该做一次"的东西 |
| 6 | `_PlBackend(...)` 重建 | `bin_net.py:3215` | ms | 内含 lane plan / prof 字典 |
| 7 | `enable_f32_pool()/disable_f32_pool()`（**仅 `BIN_F32POOL=1` 时**） | `bin_net.py:3218-3224` | 0（默认关） | 若开了：**每次调用新建 nslots 块 CMA**（`bin_engine.py:1038`），100 帧 = 600 块 6.29 MB 的分配 ⇒ 必炸。**默认关，不建议开** |
| 8 | 227 × `pack_input()`（`BIN_PACK=ps`） | `bin_engine.py:2111` | **2.5~2.6 s** | 11.3 ms/层（A53 实测，`docs/PLAN_1second.md` §36.3）× 227 |
| 9 | 227 层的 cfg/描述符/视图组装 | `bin_net.py` `_PlBackend.prepare` | **0.5~0.6 s** | `t16_prologue` 归因：30.37 µs/层 × 2861 写 + 534 ms Python |
| 10 | IN band 写入 + 每层 MMIO（3194 次/帧） | `bin_engine.py` push/start | 1.4 s（推算≈P 余量） | 由 ① prologue 4.64 s − ②2.56 − ③0.62 反推 |

### 1.2 权重装载是每帧一次还是一次性？

**一次性**（这点是好消息）：
* `load_weights()` 只在该族脚本里显式调用一次：`bin_net.py:3615`（`__main__` 的 CLI）、
  `board_verify_numfix.py` 建 net 之前一次、`demo_pipeline` 建 net 时一次。
* `run()` 内部**没有** `load_weights`（§1.1 表里没有它）。
* 容器解析（`Blob`：`np.fromfile` 1.3 MB + `json.load`）也在 `BinNet.__init__`（一次）。
  ⇒ **"每帧重打包/重载权重"这个怀疑，在代码上是假的。**

### 1.3 那 18 s 的真正来源：**两条口径混了**

| 口径 | 怎么跑 | 稳态值 | 出处 |
|---|---|---|---|
| **10.3 s** | `dump_board_int8.py --limit 1 --repeat 2 --time-only`，**同进程第 2 跑**（首跑丢弃），`BV_FREE=1` | 10.33 s | `runs/opt_round1/perf/segment_breakdown.md:3`；`Q1_report.md:5`("`BV_FREE=1`（不开则第二跑必 OOM）") |
| **28.4~29.1 s** | `board_verify_numfix.py`（V-D 备用路径），**"每张冷启动单跑"**，`BIN_PACK=ps`，**没有设 `BV_FREE`** | 28.4~29.1 s | `runs/opt_round1/ps_numeric/v60_vd_results.md:3`（原文："每张冷启动单跑"） |

两者差的三块：

**(a) 一次性启动成本（每张冷启动都付，10.3 s 口径被"丢首跑"排除了）**

| 项 | 秒 | 出处 |
|---|---|---|
| 3 个 C 内核 `q8/c8deq/bpack` 的 `load()` + 逐位自检 | **4.2~5.7**（1.4~1.9 s/个） | `bin_net.py:3073`（"首跑会把 `q8/c8deq/bpack` 的 load()+逐位自检（1.4~1.9 s/个）吃进…"）；同处与 `bin_net.py:3406` 均实测"epilogue 首跑 1.640 s vs 热态 0.257 s" |
| Overlay 下载位流 + 首次 page fault | 1~2（推算） | `board_verify_numfix.py` 的 `Overlay(a.bit, download=...)` |

⇒ 这一块 **5~7 s** 是"跑一次就没了"的成本，**不属于每帧开销**。

**(b) `BV_FREE=0`：726 MB 中间张量常驻（这是最大的一块，也是 100 帧跑崩的原因）**

```
bin_net.py:3184-3189
  # ★ BV_FREE=1：所有消费者都跑完的张量立刻释放（纯内存优化，不碰数值）。
  #   为什么需要：本实现默认把所有中间张量留到最后（release() 只减依赖计数），
  #   256×256 时 396 个张量累计 726 MB > 板子 880 MB 可用内存 ⇒ 换页/OOM，
  #   测出来的时间就不是"算"的时间。
  _free_on = os.environ.get("BV_FREE", "0") not in ("", "0", "false", "False")
```
* `board_verify_numfix.py` 里 **`BV_FREE` 出现 0 次**（`grep -c` = 0）⇒ 读到默认 `"0"` ⇒ **不开**。
* 726 MB（中间张量）+ 66 MB（arena，run100 日志"[init] arena 66.2 MB"）+ ~1.3 MB（容器）
  + Python/numpy 常驻 ≈ **900 MB+** > 板子可用 **880 MB**；板子 `SwapTotal = 512 MB`（SD 卡，
  `docs/PLAN_1second.md` §68.6 实测"`SwapTotal = 512 MB` 而非 0"）⇒ **换页走 SD 卡**。
* 量级核对（**推算**）：板端自己量过"165 MB 的 mmap/缺页 ≈ 0.35 s"（`bin_engine.py:488-490` 引
  `runs/epi256/REPORT.md` §2.3）⇒ 726 MB 纯 RAM 也要 ~1.5 s；剩下 ~10 s 只能是 **SD 卡 swap
  I/O**（512 MB swap，按 20~30 MB/s 折算 ≈ 400~500 MB 换出/换入，与"726−880 的缺口 400 MB 级"吻合）。

**(c) 每帧 Python 重组装**：`build_graph` 0.13~0.53 s + `_analyze_lazy` 十几 ms（§1.1 第 2/3 行）。

### 1.4 `BIN_PACK=ps` 时 prologue 具体做什么、按 227 层估多少

`ps` 档 prologue = ①`pack_input` 量化+打包（`bin_engine.py:2111`，走 `quant_pack`/`bpack` C 内核）
+ ② 每层 cfg/描述符/视图组装 + ③ IN band 状写入 + ④ 每层 MMIO。

| 段 | 227 层合计 | 依据 |
|---|---|---|
| ① `pack_input`（bpack） | **2.56 s** | 11.3 ms/层 × 227（`PLAN_1second.md` §36.3，A53 实测） |
| ② cfg/desc 组装 | **0.62 s** | `t16_prologue/REPORT_attribution.md:121`（534 ms Python + 86.9 ms 硬底） |
| ③ IN 写入 + ④ MMIO | **1.4 s**（推算） | ① prologue 实测 4.64 s（`segment_breakdown.md`）− 上面两项 |
| **合计** | **4.64 s** | 与板端实测**吻合**（这就是那 45.8%） |

⇒ **ps 打包本身只占 2.56 s**，且它已经在 10.3 s 的 4.64 s 里（`dump_board_int8` 没设 `BIN_PACK`，
`pack_mode()` 默认就是 `ps`，`bin_engine.py:4069-4081`）。所以"换成 ps 档导致 +18 s"**不成立**。

**与 18 s 的差额在哪**：prologue 4.64 + nonconv 3.46 + PL 2.87 + epi 1.03 − overlap 1.99 ≈ 10.0 s
（`segment_breakdown.md` 四段分解）⇒ **18 s 不在计算路径里**，只在 (a) 一次性 + (b) 换页。

> ⚠ 顺手排除一条：**interp/pool/cat 没有"每帧重算两次"**。`nonconv 3.46 s` 是**一次前向里**
> 的非卷积算子（`_exec_ps`，`cat` 是纯拷贝），不是每帧额外再来一遍。

---

## 2 可以"只做一次"的项 + 缓存方案

| 项 | 与输入无关？ | 现在每帧都做？ | 缓存到哪 | 预期省 |
|---|---|---|---|---|
| **容器 blob 解析**（1.3 MB + index.json） | ✅ | ❌ 已在 `__init__` 一次 | — | 0（已经最优） |
| **`load_weights()` → `blob_pa`** | ✅ | ❌ 一次 | — | 0（已经最优） |
| **`build_graph()` 的图结构**（op 拓扑、layer 几何） | ✅（只依赖 H,W） | ✅ **每帧重建** | 进程内缓存 `{(H,W,nbuf): graph}`，帧内只重绑输入 `tin.val` | **0.13~0.53 s/帧** |
| **每层 cfg/描述符/视图**（`_PlBackend.prepare` 的组装） | ✅（只依赖 `(layer, in_pa, out_pa, thr_pa, vdesc_pa)`） | ✅ 每帧 | **预计算成表**，键含 5 个 pa（地址陈旧性！见 `t16` 报告的"前车之鉴"） | **0.5 s/帧** |
| **`_adj_cache`（每层二值化阈值）** | ✅ | ❌ 已有缓存（`bin_engine.py:2125`） | — | 0 |
| **`_regions` 白名单** | ✅ | ✅ 每帧 append（`bin_net.py:3212`） | 登记一次即可（pa 不变时） | 0（只是内存整洁） |
| **fp_layers 权重** | ✅ | ❌ 已有缓存（`Blob.fp_layer`） | — | 0 |
| **lane plan / 段计划**（`BIN_V50_FUSE`） | ✅ | ✅ 每帧 | 同 graph 缓存 | 十几 ms |

**风险（惰性槽架构）**：`BIN_LAZY_DEQ=1` + `BIN_LAZY_SLOTS=24`（`bin_net.py:3163-3175`）会把 OUT 槽
**用 C8Tensor 句柄钉住**，槽不够时"自动物化最老的句柄"（`bin_net.py:3174` 的注释、实现见 `bin_net.py:819`）。
⇒ **缓存 graph 会跨越两帧持有旧张量/句柄**，必须：
1. 帧尾把非保护张量 `val=None` 并 `_f32_release()`（`bin_net.py:3282` 已有幂等归还）；
2. 缓存键里**放 pa**（arena 复用则 pa 稳定；一旦重建 arena 就整表失效）；
3. 先在 `--repeat 2` 下用 sha1 对照（冻结三件套）证明"缓存命中 ≠ 数值变化"。
**在没做到这三点之前，缓存 graph 是"自减速"高危改动**（历史坑：多 net 共用槽会互相挤掉）。

---

## 3 内存增长：100 帧为什么把板子压满

| 点 | 行号 | 机制 | 改法（不改数值） |
|---|---|---|---|
| **① 中间张量整帧常驻 726 MB** | `bin_net.py:3184-3189`，释放点 `:3271-3282` | `BV_FREE=0` ⇒ 396 个张量留到帧末 | **`BV_FREE=1`**（环境变量，零代码）；正确性已由 `board_verify.py:670-677`（256² 强制 `BV_FREE=1`）+ sha1 对照验证 |
| **② arena 每帧新建 66 MB CMA** | `bin_net.py:3211` | `arena=None` 时每帧 `BinArena(need)` | 传外部 arena（`dump_board_int8.py:274-286` 已这么做；run100 也传了） |
| **③ f32 池每帧重建**（仅 `BIN_F32POOL=1`） | `bin_net.py:3218` → `bin_engine.py:1038` | 每次 `enable_f32_pool` 新建 `nslots` 块 6.29 MB CMA | **保持默认关**；要开就必须"建一次、跨帧复用" |
| **④ `_regions` 每帧 +1 条** | `bin_engine.py:1263` | list append，永不清理 | 幂等登记（同 pa 只登一次） |
| **⑤ 句柄/槽被跨帧钉住** | `bin_net.py:1048, 819` | 惰性句柄未及时 `release()` ⇒ 槽耗光 ⇒ 每层物化（慢） | 帧尾显式 `release`；`BIN_LAZY_SLOTS` 别乱加（24 已接近槽上限） |
| **⑥ 100 帧输入一次性 `np.load`** | run100 的 `x0_all`（板端脚本） | 100×3×256×256×4 B = **78 MB** | 改成逐帧读/环形缓冲（省 78 MB + 首帧 100 次 IO） |

**"sshd 被打挂"的解释**：① + ② 让单进程 RSS 顶到 880 MB 上限 ⇒ 内核在 512 MB SD swap 上循环换页，
整机可用内存趋零 ⇒ `sshd` 的 fork/内存分配失败（这不是"我们进程泄漏到几十 GB"，是**系统级内存耗尽**）。

---

## 4 排序的三项优化建议（按 省时间/风险 比）

| # | 改动 | 省 | 风险 | 怎么做 |
|---|---|---:|---|---|
| **1** | **`BV_FREE=1`**（环境变量，零代码） | **10~12 s/帧** | **低**：数值中性，已被 `board_verify.py` 在 256² 上强制使用并用 sha1 对照验证（`board_verify.py:670-677`） | 所有上板脚本/cron 里加 `export BV_FREE=1`；`board_verify_numfix.py`/`demo_pipeline.py` 的 `RUNPOINT` 里补一条 |
| **2** | **图/描述符表缓存**（`build_graph` + `prepare` 组装） | **0.6~1.1 s/帧** | **中高**：与惰性槽/pa 陈旧性耦合，必须带 pa 键 + 帧尾释放 + sha1 门 | 见 §2 的风险三条；先做"只缓存 graph 结构、不缓存张量值"的最小版 |
| **3** | **prologue 批量化**（把 227 层的 IN band 写/MMIO 合并；`pack_input` 用 C 内核已是 11.3 ms/层） | **0.5~1.4 s/帧** | 中：`t16` 报告说"只做 desc/views 上限只有 0.1 s，必须连 cfg 一起做才有 0.5 s" | 先做 `t16` 的"预计算表"，收益封顶 0.5 s；IN band 合并是 1.4 s 的那块，风险更高 |

> 口径提醒：以上"省"都是**相对 28.4 s 冷启动值**；稳态基线是 **10.33 s**（`BV_FREE=1`）。
> **别拿 28.4 s 当基线去算 3× 加速** —— 约 15 s 是口径差，不是可优化空间。

---

## 5 板上 3 条命令（30 秒，验证本文的归因；不跑 100 帧）

```bash
# ① 同一进程两次跑：第 2 跑应回到 ~10.3 s（证明"18 s 主要是一次性 + 内存"）
BV_FREE=1 python3 ps/tools/dump_board_int8.py --limit 1 --repeat 2 --time-only

# ② 同一进程、只关 BV_FREE：第 2 跑立刻变慢（证明 BV_FREE 是主因）
BV_FREE=0 python3 ps/tools/dump_board_int8.py --limit 1 --repeat 2 --time-only

# ③ 冷启动一次（复现 28 s 口径）：应 ≈ 10.3 + 5~7（内核 load/Overlay）+ 内存项
python3 ps/pynq/board_verify_numfix.py --pack deploy/bin_weights_new \
  --input demo/frozen256/p0000_crop256.npy \
  --golden demo/frozen256/golden/p0000_out1_fp.npy --bit design_1_v60.bit
```

判读：①≈10.3、②≈28、③≈28 ⇒ **归因成立**；若 ① 也是 28，则我的 ①/② 结论要推翻，
靶子转到 prologue/PL（那时再查 v60 位流的 PL 段）。

---

## 6 结论（可直接抄进日报）

* **"每帧重打包权重"不成立**：`load_weights` 只在建 net 时一次（`bin_net.py:3615` 的 CLI / 脚本各一次），
  `run()` 里没有它。
* **28 s 与 10.3 s 不是同一口径**：前者是**冷启动单跑**（`v60_vd_results.md:3`），后者是**同进程第 2 跑 + `BV_FREE=1`**。
* **18 s = 5~7 s 一次性 + 10~12 s `BV_FREE=0` 的 726 MB 常驻/swap + 0.5~0.9 s Python 重组装**。
* **最划算的一步**：所有上板脚本加 `export BV_FREE=1`（零代码、数值中性、已有 sha1 验证）。
* **100 帧压满板子**：726 MB 中间张量 + 66 MB arena 顶到 880 MB ⇒ 512 MB SD swap 换页，
  系统级内存耗尽把 `sshd` 打挂；不是"我们的进程泄漏几十 GB"。
