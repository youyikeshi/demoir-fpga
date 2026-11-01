# T-16 第二阶段：三个开关（A/B/C）的实现、判据与板端投影

> **结论先行（不许读成别的）**
> ① 三个开关**全部默认关、关掉时逐字节走原路径**（stdout 也不变）；打开后 **sha1 逐位不变**（hw=64 与 hw=256 各 15 个换序臂全过），
>    A 的四条**负例**（换 IN/OUT 地址、换段 pa、同地址换形状）全部证明"必 miss、不读陈旧表"。
> ② **板端投影没到 ≤0.55 s**：$\text{prologue}\ 1595\to\mathbf{\approx1005\ ms}$（区间 890~1115），**缺口 ≈455 ms**。
>    §4 逐桶写出这 455 ms 在哪，并说明为什么 A 拿不到 −500（A 实际 $-$221，天花板 $\approx-$240）。

- patch 基线：main **`3207a79`**（已含 T-17 convfp 合并）。`runs/t16_prologue/T16_prologue.patch`，
  `patch -p1`（在仓库根）已在"干净拷贝"上 dry-run + 实打验证，打完与 `worktrees/T-16-prologue/` 四个文件 md5 逐一相同。
- 副本：`worktrees/T-16-prologue/`。**没有碰** `_fp_conv` / `conv2d_f32` / `gemm_split_m` / `bin_ref_numpy.py`
  （patch 里 0 个相关 hunk；唯一出现 `bin_ref_numpy` 的地方是新增判据脚本里的 `from bin_ref_numpy import Blob`，与既有 `verify_lazy_net.py` 同款）。
- 主数据：`runs/t16_prologue/probe2/`（hw=64 换序全臂日志、hw=256 换序日志、分桶日志）。

## 0. 三个开关（全部 opt-in，默认关 ⇒ 一个字节的路径都不变）

| 开关 | 默认 | 作用 | 子开关 |
|---|---|---|---|
| `BIN_PRECOMP=1` | 关 | **A**：cfg / 描述符 / 视图 / 段表 的 `(层×槽)` 预计算表 | `BIN_PRECOMP_STATS=1` 打印统计 |
| `Q8CACHE=1` | 关 | **B**：f32 叶子量化缓存（T-16 加固版） | `Q8CACHE_MULTIFS`(1) / `Q8CACHE_FILL`(1) / `Q8CACHE_KEEP`(1) / `Q8CACHE_VERIFY`(0) / `Q8CACHE_STATS` |
| `BIN_F32DIRECT=1` | 关 | **C**：27 层回落路改走 f32 段直读 | `BIN_F32DIRECT_STATS=1` |

缓存统计**默认一行都不打**；`verify_lazy_net --hw 64` 在 main 与本 patch 上的 stdout 归一化后 **diff 为空**（K3）。

## 1. 判据（K1~K5 逐条）

| 判据 | 做法 | 结果 |
|---|---|---|
| **K1** sha1 逐位不变 | `ps/tools/test_t16_ab.py`，**单进程 15 臂换序**：`off,A,off,A,off,B,off,B,off,C,off,C,off,ABC,off` | **最终代码 hw=64：15/15 臂**全等于 `104710f69c4d/2ac6efec2325/8ab595fcf7cd`；**hw=256**：15 臂全过（`392d1ae02761/7f68fb00d2b5/aa03751a2d1c`），改完最后两处（`_SegList`/内联重放）后**又跑 6 臂复核全过** ⇒ **PASS** |
| **K2** 逐字节 + 换序 | 每个"开"的臂紧跟一个"关"的臂；每臂**新建 engine+BinNet**（同板端 `run_net`）；A/B/C 各自独立归因 | PASS；另外 **mmio 写次数在 A 开/关下都是 5 927（逐条相同）**，可作"寄存器序列没变"的独立证据 |
| **K3** 默认关 + 静默回落 | 全关跑 `verify_lazy_net --hw 64`，与 main 版对跑，数值/时间戳归一化后 diff | **空**；回落路径：`_precomp_fallback()` 捕获异常 + 打**一行**原因（每进程每处一次），例外场景见 §6 |
| **K4** 板端投影 | §4/§5 | **未达 ≤0.55 s**：≈1005 ms（890~1115） |
| **K5** 改动函数清单 | §2 表 | — |

**A 的负例（`--neg`，全部 OK，可复算）**

| # | 负例 | 断言 | 实测 |
|---|---|---|---|
| ① | 同层同形状、**只换 OUT 槽地址** | arm 必 miss 且写进去的是新地址 | miss 1→2；`B_OUT` 806498496→806633728 = 新地址 |
| ② | **只换 IN 槽地址** | 同上 | miss=1；`B_IN` = 新地址 |
| ③ | **只换段的物理地址**（i8 段 pa） | seg 必 miss，且描述符 src 跟着换 | seg_hit=0 / miss=2；desc[0] 的 src 与新 pa 逐位相等（旧 pa≠新 pa） |
| ④ | **同地址换形状**（H/W 变、地址不变） | 必 miss | seg_hit=0 / miss=1 |

⇒ 失效是**结构性**的（键里含喂给被替代函数的全部输入），不依赖"人记得 invalidate"。

## 2. K5：改动函数清单（合并冲突评估用）

| 文件 | 改动 | 性质 |
|---|---|---|
| `ps/pynq/bin_engine.py` | 新增 `precomp_on/clear/stats/reset_stats/print_stats`、`_precomp_fallback`、`_ON_VALS`、`_ARM_PLANS`、`_SEG_PLANS`、`_PVIEWS_SHARED` | 新增（全部在 6.5 节，独立成段） |
| | `BinConvEngine.arm()` **加 3 行**（`precomp_on()` ⇒ `_arm_cached`），原体保留为回落 | 入口 + 分支 |
| | 新增 `BinConvEngine._arm_key/arm_plan/_arm_cached` | 新增方法 |
| | `E1E2Engine.prepare_segments()` **加 5 行**（`precomp_on()` ⇒ `_prepare_segments_cached`），原体保留 | 入口 + 分支 |
| | 新增 `E1E2Engine._seg_sig/_prepare_segments_cached` | 新增方法 |
| | `E1E2Engine._pack_views()`：`_pbuf` 分配处加"进程级复用"分支（`precomp_on()` 才走） | 分支 |
| `ps/pynq/bin_net.py` | 新增 `_ON_VALS/_env_on`、类 `_SegList` | 新增 |
| | `_PlBackend._segments()`：返回 `_SegList` 并**顺手**填 `sig`（原先只返回 list） | 返回值类型（list 子类，向后兼容） |
| | `_PlBackend.prepare()`：新增一个 `elif`（C 的 f32 段直读），原三分支不动 | 分支 |
| | `BinNet.run()`：新增 `self._f32direct` / `self._q8_multifs` 两行；return 前加**三段 env 门控**打印 | 开关 + 门控打印 |
| | `_q8_segment()`：fs 冲突门禁改为"允许多条目"（`Q8CACHE_MULTIFS=0` 可关回） | 单点行为（仅在 `Q8CACHE=1` 下可达） |
| `ps/pynq/qcache.py` | `quantize_leaf_c8(..., out=)` 新增直写缓冲参数 | 签名向后兼容 |
| | `QuantLeafCache.__init__`：新增 `_fill_direct/_keep` 两个开关属性 | 新增 |
| | `get_c8()`：未命中路径改为"先切槽 → 直写 → 再 flush"，并给条目加**强引用** | 单函数内部 |
| `ps/tools/test_t16_ab.py` | 新增（换序 A/B + 负例，可复跑判据） | 新文件 |

hunk 总数 20；`bin_engine` 7 / `bin_net` 7 / `qcache` 6，均为"新增分支 + 新增方法"，**没有删除既有分支**（29 行删除全部是同一函数内的替换行）。

## 3. 主机实测（FakePL，同一条判定路径）

**hw=256（板端口径）**，`BIN_PROFILE=1`，换序（每臂新建 engine+BinNet）：

| 臂 | prologue（热态） | q8 | desc | views | mmio 写 | mmio 读 | sha |
|---|---:|---:|---:|---:|---:|---:|---|
| off（8 样本） | **0.1015~0.1137**（均值 0.114） | 0.058~0.062 | 0.0055 | 0.0020 | 5927 | 3128 | ✓ |
| A（暖，含 2 趟冷跑） | **0.0826**（−18.6%，`arm_hit 227/227`、`seg_hit 200/200`） | 未变 | 0.0045 | ± | **5927** | **1312** | ✓ |
| B（暖） | **0.089~0.103** | **桶消失**：112 段全走缓存（71 命中/41 未命中），量化搬进 `_q8_segments` | — | — | 5927 | 3128 | ✓ |
| C（暖） | 0.114~0.120 | 0.069 | 0.0059 | 0.0024 | **6115** | 3197 | ✓ |
| ABC（暖，第 2 趟） | **0.0704**（−30.6%，`seg_hit 227/227`） | — | — | — | 6115 | 1381 | ✓ |

> A/C/ABC 的百分比都是**同进程相邻臂**之比（换序），不是跨进程；`off` 8 个样本、`A` 4 个、`ABC` 2 个。
> 6 臂复核（最终代码）日志：`probe2/hw256_spot_final.log`。

**hw=64**（分桶细看，`BIN_LAZY_DEBUG=1`，三轮均值）：

| 臂 | prologue | cfg | job | views | q8 | desc |
|---|---:|---:|---:|---:|---:|---:|
| off（热，3 次） | 0.0365 | 0.0133 | 0.0193 | 0.0017 | 0.0062 | 0.0032 |
| A（热，3 次） | **0.0251（−31%）** | **0.0063（−53%）** | 0.0148 | 0.0011 | 0.0064（**未变**） | 0.0019（−41%） |

- `mmio 读` 从 **3128 → 1312**（A 开）：少 1816 次 = **227 层 × 8 次基址回读**
  （`set_bases()` 末尾的 `bases_now()`，返回值被 `arm()` 丢弃）。这是 A 的**附带**收益，板端 24.94 µs/读 ⇒ **45.3 ms**。
- `mmio 写` 在 A 开/关下**完全相同（5927）** ⇒ 重放序列与原路径逐条一致。
- C 让写次数 **+188**（27 层的 E1E2 job 从 `prepare` 内部挪到 `kick`，组命令写法不同）⇒ 板端 +5.7 ms。
- B 在 hw=64 上**是净亏**（0.0444→0.043~0.046）：64² 只有 7.2 MB f32 要量化，而 cache 的逐段查表开销与字节量无关 ⇒
  **B 只在 ≥256² 才划算**（板端就是 256²）。C 同理（64² 上 pack_input 本来就不慢）。

## 4. 板端投影（每刀单独；方法写在下面，查不到依据的一律标"待板端实测"）

**方法（两段式，避免"主机时间当板端时间"）**：板端桶大小取第一阶段报告的真板数（`1595.5`
= q8 604.0 / packin 257.5 / cfg 350.0 / desc 85.5 / acq 92.0 / views 61.3 / glue 81.8 / 入口 42.9 / 其它 20）；
**比例**取本次主机实测的**同一子桶**，且先把 MMIO 的物理成分从桶里剥掉（写 30.37 µs、读 24.94 µs 是板端实测量，
主机侧用 FakeMMIO 实测 0.48/0.52 µs 剥离），只让"纯 Python 部分"按比例缩放。

| 刀 | 组成（板端 ms） | 前 | 后 | **省 ms** | 依据 | 置信 |
|---|---|---:|---:|---:|---|---|
| **A** | cfg（$350=86.9$ 写 $+45.3$ 读 $+217.8$ Python） | 350 | 184.5 | **−165** | $\rho_{cfg}=(6.3-1.37)/(13.3-2.31)=0.448$；写的地板 86.9 不动；读 45.3 全免 | 中 |
| | desc | 85.5 | 50.8 | −35 | $\rho=1.9/3.2=0.59$ | 中 |
| | 入口（`t_q`+`_seg_layout`） | 42.9 | 28.3 | −15 | $\rho=(14.8-6.4-1.1-1.9)/(19.3-6.2-1.7-3.2)=0.66$ | 低-中 |
| | views（每调一次的 `_pack_views`；首调 698 命令扫描**仍在**，board 11.9 ms） | 61.3 | 55.3 | −6 | 只对"每调一次"那 49.4 ms 按 $\rho=0.87$ 计 | 低 |
| | **A 小计** | | | **−221**（区间 170~250） | 端到端旁证：hw=256 暖态 0.1015→0.0826（−18.6%） | 中 |
| **B** | q8（f32 段量化+C8，33.5 M elem） | 604 | ~292 | **−312**（区间 261~343） | 两项加固正是 T-08 投影的前提。host 256² 实测：112 个 f32 段**全部**改走缓存（71 命中/41 未命中），剩余工作 = 41 个"唯一张量"重算 + 71 次查表 ⇒ host 上 60 ms → 29 ms（$\rho=0.48$）⇒ 板端 604×0.48 | 中 |
| **C** | pack_input（27 层，31 MB f32 + 1.13 MB 位图） | 257.5 | 200.5 | **−57**（区间 −50~−120） | 桶内分解：PL 打包 61（位图/18.5 MB/s，**只是搬到 `pl_s`，不消失**）、push 8（省掉）、量化+转置 188.5（=165 MB/s）→ q8_c8 232 MB/s = 133.6，再 +188 次写 ×30.37 µs | 中 |
| | **合计** | 1595.5 | **≈1005** | **−590**（区间 −480~−710） | | |

$$\Rightarrow\ \text{prologue}\ 1595\to\mathbf{1005\ ms}\ (\text{区间 } 890\sim1115),\quad \textbf{目标 }550 \Rightarrow \text{缺口 }\approx 455\ \text{ms}$$
$$\Rightarrow\ \textbf{诚实结论：A+B+C 只拿到需要削减量（1045 ms）的 56\%，到不了 0.55 s。}$$
> 注意 ABC 的"交互开销"是真的：host 上 A 单独 −18.9 ms、B 单独 −38 ms（q8 桶消失），但 ABC 合计只 −31 ms
> ⇒ B 让每个 f32 段都要过一次 `get_c8`（122 段 × ~60 µs），且 `_q8_segments` 会丢掉 A 的 `_SegList.sig`
> （退回逐段建键）。**上表的 B 用的是"29/60"这个实测比值，已含这笔开销。**

### A 为什么拿不到 −500（这是本阶段最重要的负面结论）

- A 能把"每层重算"换成"每层查表＋22 条重放"，但**查表＋重放本身就是 ~0.4 ms/层**的纯 Python
  （`_arm_key` 21 项、`_seg_sig`（已改成"建段表时顺手算"）、22 次影子比较、12.6 次真写）。板端 cfg 因此只能 $350\to185$。
- **A 的硬地板 = 12.6 次 MMIO 写 × (30.37 µs 硬件 + ~15 µs Python) ≈ 0.57 ms/层 = 130 ms**。
  ⇒ A 的理论天花板 $\approx-(350-130)=-\mathbf{220}$ ms ⇒ 实测投影 −221，**A 已经贴着天花板**，
  再往下必须动 MMIO 的物理成本（E）或整帧计划（D）。
- 要突破这个地板只有两条路，都**超出本阶段授权**：
  **E.** 把"写配置页 + 22 条影子判断"整体做成 C 侧一次调用（`/dev/mem` 直写）⇒ 130 → ~5 ms；
  **D.** 帧级计划表（整帧一次校验 + 按序重放，避免每层建键）⇒ 再省 ~60 ms。

### 缺口的 520 ms 在哪（逐桶，必须显式写出）

| 项 | 残余 ms | 为什么 A/B/C 拿不掉 |
|---|---:|---|
| q8（B 之后） | ~292 | 33.5 M elem 里**唯一张量**那部分必须真量化（41/112 段） |
| cfg（A 之后） | ~185 | 87 是 MMIO 写硬底，98 是"查表+重放"的纯 Python（见上） |
| pack_input（C 之后） | ~200 | 31 MB f32 的量化+布局地板（232 MB/s ⇒ 134）+ 61 ms 的 PL 打包（搬进 `pl_s`，不消失） |
| desc + 入口 + views | ~134 | 每层仍要建键/写缓冲；`views` 的 698 命令扫描每帧一次（板端 11.9 ms，因为 `run_net` 每帧新建 engine） |
| acq（8 次强制物化） | 92 | 槽压力问题（D；要动 pin 淘汰策略/槽数，受 CMA 限制） |
| 段表 glue（`_segments`） | 82 | 每层一次叶子遍历；离线缓存需要"帧级"键（D） |
| canary/push/tail | 20 | — |
| **Σ** | **≈1005** | 与投影自洽 |

⇒ 到 0.55 s 必须再加 D（帧级计划 + `_segments`/`views` 下沉）与 E（MMIO C 侧），
以及"把 27 层的 f32 输入在上游就变成 int8"（图级改动，属 T-19/T-17 的地盘）。
**这三件都不在本阶段的三刀里，请主代理决定是否开 T-16b。**

## 5. 已知风险 / 未覆盖

1. **B 的命中率在板上没有测过**（Q8CACHE 在板上从未跑过）。投影用的是 T-08 的 $-$261…$-$316 + 本次两项加固，
   **板上还有一个未验证项**：`qcache` 每帧要新分配一块 32 MB CMA（`run_net` 每帧新建 engine）
   ⇒ 与 66 MB arena 同时在场约 98 MB。`CmaFree` 曾报 0.5 MB 而 66 MB 仍分配成功 ⇒ **容量待板端实测**。
2. **A 的跨帧命中依赖"pack arena 物理地址跨帧稳定"**。板端每帧新建 engine ⇒ 原逻辑每帧重新 CMA 分配。
   本 patch 在 `BIN_PRECOMP=1` 时把它做成**进程级复用**（键含分配器身份）⇒ 地址稳定由构造保证，不是靠运气。
   **但这条只在主机 SubAlloc 模型上验过**（hw=64 15 臂 + hw=256 6 臂，`arm_hit 227/227`、`seg_hit 200/200`、ABC 时 227/227 全命中）；
   板端第一个要看的数就是 `[PRECOMP] arm hit/miss / seg hit/miss`（`BIN_PRECOMP_STATS=1`）。
3. **C 的收益是板端特有的**（`to_c8_padded` 在 A53 上慢），主机上 64²/256² 都测不出收益 ⇒ 只投影、无主机旁证。
4. B 的 `Q8CACHE_KEEP=1` 会**多持有 ≤27 MB f32 源张量**（换来"地址不可能被复用"的结构性保证）；要省内存可 `=0`
   （此时正确性退回"靠 BV_FREE 打点失效"，与既有基线同级别）。
5. 板端 `perf_gate` 的 q8 门槛（0.605×1.5）在 C 开着时会被**抬高**（27 层的量化搬进 q8 桶）；
   门槛语义随开关变化，建议板端记录 `[PRECOMP]/[Q8CACHE]/[F32DIRECT]` 三行统计一起入库。

## 6. 复现

```bash
cd worktrees/T-16-prologue/pynq_specialized_code
# ① 逐位 + 换序 A/B + 负例（约 90 s）
python3 ps/tools/test_t16_ab.py --hw 64 --neg
# ② 256²（约 25 min，板端口径；分桶要 BIN_PROFILE=1，且必须在**启动前**设）
BIN_PROFILE=1 python3 ps/tools/test_t16_ab.py --hw 256
# ②' 最终代码的 256² 复核（6 臂，含 A 的冷/暖两趟与 ABC 两趟）
python3 ps/tools/test_t16_ab.py --hw 256 --arms off1,A2,A2,off2,ABC,ABC
# ③ 单臂连跑两次看"跨帧命中"（A 的收益是帧间的）
python3 ps/tools/test_t16_ab.py --hw 256 --arms off1,A2,A2,off2,ABC,ABC
# ④ 缓存统计（默认不输出）
BIN_PRECOMP=1 BIN_PRECOMP_STATS=1 Q8CACHE=1 Q8CACHE_STATS=1 BIN_F32DIRECT=1 \
  python3 ps/tools/verify_lazy_net.py --hw 64
# ⑤ 生成/校验 patch（在仓库根）
bash runs/t16_prologue/mk_patch.sh && patch -p1 --dry-run -i runs/t16_prologue/T16_prologue.patch
```

**第 72 条纪律**：以上所有 sha1 都在 **默认 BLAS 线程口径**下取（未设 `OMP/OPENBLAS/MKL_NUM_THREADS`），
与冻结常量同口径；判据是"同口径内 A=B=C"，不是跨口径比对。
