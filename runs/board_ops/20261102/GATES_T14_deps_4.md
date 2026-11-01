# T-14-deps（第二棒）④ fail-fast + 性能门槛 + **唯一一次板端验证**

**执行者**：机制加固工程师（第二棒）
**日期**：2026-11-02（板端时钟 `2025-05-08`，板子 NTP 常年偏；以主机时间为准）
**作用域**：只改 PS 侧 Python（`ps/pynq/*.py`、`ps/tools/deps_closure.py`）；**未碰 RTL、未起 vivado、未动 `runs/board_ops/20261102/frozen_code/`**
**板端**：v39 `602725b8`，`fclk 150` 读回 150，24 槽，`armv7l + pynq 3.1.0 + numpy 1.21.5`；**一次 session 一次 load**，收工 `sync; sync; poweroff`（ping 已不通 = 关机确认）

---

## 0. ★ 这类事故为什么门禁看不见（**本节最值钱，放最前面**）

$$\Rightarrow\ \textbf{内核回落是「数值等价」的}\ \Rightarrow\ \text{逐字节 / sha1 / 拍数 / 资源四条门禁}\ \textbf{全部通过}$$

2026-11-02 那场事故（`runs/board_ops/20261102/REPORT_rootcause_missing_deps.md`）里，
冻结点少拷了 `q8.py/.c`、`c8deq.py/.c`、`bpack.py/.c` 六个文件，
`_q8()/_deq_kernel()/_bpack()` 三个回落点**全部静默**换成 numpy 纯 PS 实现。今天实测出来的形态是：

| 门禁 | 回落时会发生什么 | 能不能抓到 |
|---|---|---|
| **逐字节（bit-exact）** | numpy 版与 C 版**输出逐位相同**（C 内核自带的逐位自检覆盖全部 256 个 int8 取值 × fs 0..12 × 平局/±inf/NaN/次正规数；今天两臂 sha1 也是一模一样） | **抓不到**（它就是要等价） |
| **sha1 三件套** | 256² 两臂都是 `9b1940c8955b / f86a7035621b / 025757a83c39`（今天实测，**3/3**） | **抓不到**（这正是"缺 q8 时整网 sha1 完全相同"那条发现） |
| **拍数 parity** | 回落只改 PS 侧实现，PL 侧命令/拍数**一个字节不变** | **抓不到** |
| **资源表 / 时序** | 与 PS 侧 Python 无关 | **抓不到** |
| `total` 这一格 | 只从 6.49 s 变成 11.69 s。**没有历史值对照时，11.69 s 看起来就是一个正常数字** | **抓不到**（除非有人恰好在同一运行点有基线） |
| **`[DEPS]` 这一行** | `q8=fallback-numpy` ⇒ `deps_closure_ok=False` | **只有它抓到** |

推论（写进机制，而不是写进纪律）：

1. **"结果对"不能推出"跑的是应跑的那条路"**。加速内核与参考实现**天生等价**，
   所以任何"比结果"的门禁在结构上对这类事故是**失明的**。
2. 唯一能看见它的是**旁证**：`①` 依赖在不在（`[DEPS]` / `deps_closure_ok`）、
   `②` 该快的桶快不快（`prologue.q8` / `epilogue` 对热态基线）。
   这两条今天都做成了机械化判据，**默认关、零行为影响**。
3. 所以本次的判据是**"跑点必须自己声明内核状态"**：`run_valid=False` 的跑点
   **不许进基线表**——把"数字能不能用"从人的记忆搬到 JSON 字段上。

> 一句话：**门禁比的是"结果对不对"，而这类事故坏的是"路径快不快"。**
> 想抓它只有两条路：**声明依赖**（`[DEPS]`）和**声明性能下限**（`PERF_GATE`）。
> 二者都不是"再测一次结果"能替代的。

---

## 1. 交付物与文件 md5（副本 `worktrees/T-14-deps`）

`git diff HEAD --stat`（相对基线 commit `825cbb9`，**合并视角**；`deps_closure.py` / `board_repeat.py` 是新增文件，
第一棒已 `git add` 过，所以 `git diff`（对索引）只显示其后的小改动 ⇒ 以 `git diff HEAD` 为准）：

```
 pynq_specialized_code/ps/pynq/bin_engine.py       | 353 ++++++++++++++++++++++
 pynq_specialized_code/ps/pynq/bin_net.py          |  65 ++-
 pynq_specialized_code/ps/pynq/board_v39_verify.py | 108 ++++++-
 pynq_specialized_code/ps/tools/deps_closure.py    | 481 ++++++++++++++++++++++
 tools/board_repeat.py                             | 154 +++++++
 5 files changed, 1159 insertions(+), 2 deletions(-)
```

| 文件 | md5（终版） | 谁改的 |
|---|---|---|
| `ps/pynq/bin_engine.py` | `1967aa8a57dabd40057a8a462785b132` | 第一棒 §6.4 + 第一棒 §6.5(④) + **第二棒 §6.5 `deps_warm/load_kernels`** |
| `ps/pynq/bin_net.py` | `2e077940df90e5ac59583ae65af10b54` | 第一棒 §② + 第一棒 ④接线 + **第二棒 热态判定** |
| `ps/pynq/board_v39_verify.py` | `903aa8bfb8b0e33a8fb6314a36e8eaf1` | 第一棒 §②/④ + **第二棒 `step_kernels()` + `e1e2_prof` 留证** |
| `ps/tools/deps_closure.py` | `b6b85a4d64deda02c39e353b4e3a712c` | 第一棒 + **第二棒 `_strip_stamp` 修「源根」误判** |
| `runs/board_ops/20261102/deps_closure.txt` | `ffd2b678a169d327e10596b7e39b0a3d` | 生成物（`--gen`，11 模块 / 15 文件） |
| `drv/deps_board_run.py`（板端驱动，归档） | `b4f673178c74f98c6a058534ed020daf` | 第二棒新建 |

证据目录：`runs/board_ops/20261102/t14_board/`
（`board_run1_cold_1213.log` = **修复前**首跑那次；`board_run2_hot_1218.log` = 终版；
`t14_deps_board2*.json`、`t14_deps_board3_prof.json`、`board_closure/` = 上板那份逐字节副本。）

---

## 2. ④ 复核：四条要求逐条（**不是我写的，我复核 + 补缺**）

第一棒在我接手后被"批准消息"重新唤醒，先写了 §6.5（`BIN_REQUIRE_KERNELS` / `PERF_GATE` /
`perf_gate()` / `deps_require_error()`）与接线。按主代理指令，**我不重写，逐条复核 + 补缺**。

| # | 要求 | 现状 | 复核结论 |
|---|---|---|---|
| a | `BIN_REQUIRE_KERNELS=1` ⇒ 任一内核未装上就 `raise`，**默认关** | `bin_engine.report_deps()` 打完诊断后 `raise deps_require_error(d)`；`require_kernels()` 认 `1/on/true/yes`，`""/0/off/false/no/n/none` 都算关 | ✅ **实测通过**（§3 触发①） |
| b | 错误信息含：缺哪个 / 期望在哪 / `sys.path` 前 6 项 | 抛出体含：缺的模块名+用途+期望文件名(+`.c`)/现在解析到哪/`.so` 是否存在+md5/原因/期望目录=模块所在目录/`sys.path` **前 8 项**（比要求的 6 项多 2 项）/3 条修法/清单路径 | ✅ **通过（6→8 项是超集）** |
| c | `prologue.q8≈0.61`、`epilogue≈0.26`（256² 热态）各 **1.5×** ⇒ 醒目告警 + `run_valid=False`，**不自动修正** | `PERF_GATE{ratio 1.5, hw 256, q8_s 0.61, epilogue_s 0.26}`；`perf_gate()` 只算不改；`bin_net.run()` 打 `[PERF] ⚠⚠` 并写 `stats["perf_gate"]`、`run_valid=False` | ⚠️ **原实现有缺陷，已补**（见 §2.1）；补后 ✅ |
| d | 默认关时除 `[DEPS]` 一行外输出**逐字**不变 | 见 §4 | ✅ **通过** |

### 2.1 我做的两处**最小增量修正**（都写清为什么）

**修正 1（关键，板端跑出来才暴露）：性能门槛必须只说"热态"。**

板端实测（同一 boot、同一闭包，`runs/board_ops/20261102/t14_board/board_run1_cold_1213.log`）：

| 桶 | 同一进程**首跑** | 第二次跑（热态） | 差 |
|---|---|---|---|
| `prologue` | **3.572 s** | 1.613 s | ×2.2 |
| `epilogue` | **1.632 s** | 0.257 s | **×6.4** |

原因不是回落（`[DEPS]` 两跑都是全 `fast`），而是 `q8/c8deq/bpack` 的 `load()`**本身**
（`.so` 已编好也要载入 + 逐位自检）：板端实测 `q8 0.222 s / c8deq 1.375 s / bpack 1.780 s = 3.377 s`，
而这段成本落在**首个整网的 prologue/epilogue 桶里**。
⇒ 原实现会对**每次 session 的首跑**判 `run_valid=False`（那一次实测确实在 profile 跑上打出
`[PERF] ⚠⚠ epilogue = 1.640 s > 门槛 0.390 s`）。**这是 100% 误报**，报警一旦误报就没人看了。

最小修正（3 处，都不改数值路径）：

- `bin_engine.deps_warm()`：返回"三个 native 内核本进程是否已经真的试过 `load()`"；
- `bin_engine.load_kernels()`：显式装载三个内核并**单独计时**（零数值影响）；
- `bin_net.run()`：在计时之前取一次 `_deps_warm`；**只有热态才判门槛**，
  非热态打印一行 `[PERF] 本跑含一次性内核装载（口径 a）⇒ 门槛按热态语义不判`，
  并在 stats 里写 `perf_warm=False / perf_note=...`；
- `board_v39_verify.step_kernels()`：与 `step_bilinear()` 完全同构——把一次性装载
  **挪出计时**并写进 `runpoint["kernel_load"]`，主流程在 `step_net(256)` 之前调用。

**修正 2（板端判据会永远 FAIL）：`deps_closure.py --check` 不该把「源根」算进一致性。**

`--check` ① 是"内存里重渲染一份，与磁盘清单比（忽略时间戳）"。但 `# 源根:` 这行记的是
**生成时的相对路径**，而任务书要求的用法正是"把 15 文件闭包整体拷到
`/home/xilinx/demoir/ps/pynq_deps_t14/` 后在**那个目录**里跑 `--check --src . --dir .`"——
此时源根必然不同 ⇒ **永远 FAIL**（我实测复现：`✗ 清单与源码树不一致`）。

最小修正：`_strip_stamp()` 把 `# 源根:` 也归为"环境相关元信息"（真正判据是**模块图** + **逐文件 md5**，都与路径无关）。
**没有放宽任何判据**，用四个负例证明（§3.3）。

---

## 3. 两次"故意触发"的实测（原文）

### 3.1 触发①：`BIN_REQUIRE_KERNELS=1` + `sys.path` 缺 `q8` ⇒ **必须 raise**

被测目录 `/tmp/t14b/noq8` = 完整闭包**只少** `q8.py` / `q8.c`（其余 10 模块 + 3 个 `.c` 都在），
`env -u PYTHONPATH` 保证不会悄悄 import 到真 `ps/pynq`。

```
[DEPS] q8=fallback-numpy c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast  ⇒ deps_closure_ok=False
[DEPS] ⚠ 依赖闭包不完整：这一跑跑的不是快路径，**数字不可当基线**
[DEPS]   ✗ q8       fallback-numpy f32→int8 量化（prologue；缺 ⇒ prologue ×3.69）
[DEPS]             原因：模块不在 sys.path（缺 q8.py）
[DEPS]   修法：把 ps/pynq/{q8,c8deq,bpack}.{py,c} 与 e1e2_sim.py / catpack.py / board_safe.py 放进同一个目录 …
[DEPS]   sys.path 前 6 项：['/tmp/t14b/noq8', '/tmp/t14b/noq8', '/usr/lib/python310.zip', ...]

[t] ★ raise RuntimeError
----------------------------------------------------------------------
BIN_REQUIRE_KERNELS=1：1/6 个内核没装上 ⇒ 拒绝在 numpy 回落路径上跑

  ✗ q8       state=fallback-numpy
      用途    ：f32→int8 量化（prologue；缺 ⇒ prologue ×3.69）
      期望模块：q8.py（+ q8.c，.so 由 cc 现场编）
      现在解析：<未解析到 —— 不在 sys.path 上>
      .so     ：/tmp/t14b/noq8/q8_x86_64.so（不存在 ⇒ 首次使用要现场 cc 编译）
      原因    ：模块不在 sys.path（缺 q8.py）

  期望位置（本模块所在目录，依赖必须在这里闭包内）：
    /tmp/t14b/noq8
  /usr/bin/python3 现在的 sys.path 前 8 项：
    [0] /tmp/t14b/noq8   [1] /tmp/t14b/noq8   [2] /usr/lib/python310.zip …
  修法（任选其一，都别改代码）：① 拷文件 ② 加 PYTHONPATH ③ deps_closure.py --check
  注意：只拷 .py 不够 —— q8/c8deq/bpack/bilinear_c 还要各自的 .c 才编得出 .so。
----------------------------------------------------------------------
```

**整网入口也 raise**（不是只在探针里）：`bin_net.py --dry-run --hw 64` + 同一个开关 ⇒ `rc=1`，
`RuntimeError: BIN_REQUIRE_KERNELS=1：1/6 个内核没装上…`，**在任何计时发生之前**。

| 场景 | rc | 行为 |
|---|---|---|
| 缺 `q8` + 开关**开** | **1** | `raise`（上面原文） |
| 缺 `q8` + 开关**关**（默认） | 0 | 只报 `[DEPS] … deps_closure_ok=False`，照常跑（**不 raise**） |
| 完整闭包 + 开关**开** | 0 | 正常跑，`sha1 = 104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd`（64²，与冻结一致） |

### 3.2 触发②：把 `prologue.q8`「人为放大」⇒ **必须告警 + `run_valid=False`**

**先说一条被实测推翻的假设（重要）**：任务书建议"临时用 `CC=/bin/false` 制造回落"。
在 **x86 上这样做了，门槛不会告警**——因为 x86 的 numpy 比 C 内核**还快**（终版代码实测，两次独立复现）：

| 256² 主机 FakePL（终版代码、`BIN_PROFILE=1`） | q8 桶 | epilogue 桶 | `deps_closure_ok` | `run_valid` |
|---|---|---|---|---|
| 内核正常（`.so` 在位，`CC` 默认） | **0.073 s** | 0.009 s | True | True |
| `CC=/bin/false`（真回落） | **0.084 s** | 0.011 s | **False**（q8/c8deq/bpack 全部 `fast→fallback-numpy` 漂移） | **False**（由 `[DEPS]` 判的，**不是**门槛） |

⇒ `CC=/bin/false` 在 x86 上**只**被 `[DEPS]` 抓到（`q8/c8deq/bpack fast→fallback-numpy`，
`deps_closure_ok=False`，`run_valid=False`）；**性能门槛是板端口径，在 x86 上本来就不该判**。
这不是缺陷，而是"两条机制互补"的直接证据（§0）。

于是触发②分两路做，两路都实测：

**路 A（端到端接线，唯一改动 = 门槛常数）**：把 `PERF_GATE["q8_s"]` 由 0.61 临时压到 0.02
（= 门槛从 0.915 s 变成 0.030 s），其余一律不动：

```
  真实门槛：q8 0.61 s × 1.50 = 0.915 s / epilogue 0.26 × 1.50 = 0.390 s
  [PERF] ⚠⚠ prologue.q8 = 0.077 s > 门槛 0.030 s（历史 0.020 s × 1.50）
         ⇒ 疑似 numpy 回落（q8 未装上） ⇒ run_valid=False，本跑点不许进基线表
  实测：q8 桶 0.077 s / epilogue 0.015 s
  stats['perf_gate'] = [{"bucket":"q8","got_s":0.0773,"base_s":0.02,"limit_s":0.03,"ratio":1.5,"hw":256}]
  stats['perf_warm'] = True
  stats['run_valid'] = False
  sha1 = ('392d1ae02761','7f68fb00d2b5','aa03751a2d1c')      ← **数据一字未变**
  判据：门槛压低后必须 alert + run_valid=False ⇒ ✓
```

⇒ 满足"告警 + `run_valid=False` + **不自动修正**"（sha1 不变就是"没动数据"的证据）。

**路 B（判据值，不动代码；用板端实测数字）**：

```
  快路径（板端热态 0.61 / 0.26）          ⇒ 无告警   ✓
  numpy 回落（3.60 / 1.19）               ⇒ 告警 q8,epilogue   ✓
  边界 0.9149（< 0.61×1.5）               ⇒ 无告警   ✓
  边界 0.9151（> 0.61×1.5）               ⇒ 告警 q8   ✓
  epilogue 边界 0.3899                    ⇒ 无告警   ✓
  epilogue 边界 0.3901                    ⇒ 告警 epilogue   ✓
  hw=128（无历史值 ⇒ 分辨率保护）          ⇒ 无告警   ✓
```

**并且板端实测把这两个常数校准回自身**（§5.4）：`q8 = 0.605 s`（基线 0.61，**−0.8%**）、
`epilogue = 0.268 s`（基线 0.26，+3%）。⇒ 门槛不是拍的，是**可复现的**。

### 3.3 修正 2 的四个负例（证明没有放宽判据）

| 负例 | 期望 | 实测 |
|---|---|---|
| 新增本地模块 `zzz` 并被 `import` | FAIL | `✗ 闭包里有 DECL 未声明的模块：['zzz']`（rc=1） |
| **只**加一条"已声明模块之间的依赖边"（清单没重生成） | FAIL | `✗ 清单与源码树不一致（依赖图或文件内容变了）`（rc=1） |
| 删 `q8.py`/`q8.c`（事故形态） | FAIL | `md5sum -c rc=1` + `✗ DECL 声明了但扫描不到依赖：['q8']` |
| 改 `c8deq.py` 一个字节 | FAIL | `✗ md5 不符 c8deq.py：清单 af33f47f… / 实际 944bd5fb…` |

正例（两处都 rc=0）：源码树 `--src . --dir .`；**板端** `/home/xilinx/demoir/ps/pynq_deps_t14` 同上。

---

## 4. 判据 d：默认关时**零影响**（逐字口径）

命令（同参数、同输入、`--pack deploy/bin_weights_new`）：

```
python3 bin_net.py --dry-run --hw 64
base = git HEAD（基线）    ×2 次        new = 改后终版 ×2 次
把浮点数一律归一化成 T 之后 diff：
  base1 vs base2  ⇒ IDENTICAL          （同一份代码，噪声归一化后逐字相同）
  new1  vs new2   ⇒ IDENTICAL
  base1 vs new1   ⇒ 只多 1 行：
    > [DEPS] q8=fast c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast  ⇒ deps_closure_ok=True
  base1 vs new2   ⇒ 同上（1 行）
```

**推论链**（为什么说"与第一棒版本逐字相同"）：第一棒版本相对 HEAD 也只多这 1 行（其 §4 实测），
而终版相对 HEAD 也只多这 1 行 ⇒ 终版相对第一棒版本**输出逐字相同**。
`④` 新增的输出全部**只在被触发时**出现（`[PERF]` 仅在 `BIN_PROFILE=1` 且冷/超门槛时打；`raise` 仅开关开时）。

另：`404` 之外没有任何默认行为变化 —— `run_valid / perf_gate / perf_warm / kernel_deps`
只是 `stats` 里的字段，`print_stats()` 不打印它们。

---

## 5. 板端验证（**唯一一次 load**）

会话：`00:50:22` 取锁签发 load 令牌 → `00:58:28` 关锁关机；全程 `tools/boardlock.sh`；
主机侧先清 `/tmp/dsh_board.loadtoken`、`/tmp/dsh_board.ssrate`；`BOARD_ALLOW_LOAD=1` **仅一次**。

### 5.1 四项体检 + 位流

```
df -h /   : 28G 用 7.3G 剩 19G (28%)
df -i /   : 6%
sudo true : SUDO_RC=0
pynq      : PYNQ_OK 3.1.0 1.21.5 armv7l
bit       : 602725b86941864eeeeab3be005e1e81  /home/xilinx/demoir/design_1_150v39.bit   ← v39，非 v40/v41
uptime    : 1:55（fresh boot），load average 0.23
```

加载与身份：`Overlay(download=True)` **1.572 s**（本 session 唯一一次真 download，令牌放行）；
`conv_uni_0=0x40040000` / `bin_unit_0=0x40060000` ✓；`Clocks.fclk0_mhz` 目标 150 → **读回 150**；
复位指纹 `alpha_shift=9 slope_shift=8`、`ResvPage[0x18]=0x0` ✓（金丝雀由 `engine.run()` 内建强制，跑完未报警）。

### 5.2 闭包落点与 `[DEPS]` 原文（板端 `armv7l`）

15 文件闭包整体放在 **`/home/xilinx/demoir/ps/pynq_deps_t14/`**（`ps/pynq_deps` 已被上一位占用，故新建 `_t14`），
驱动与 `deps_closure.py` 放 `drv/`。

```
[2] 闭包落点（find_spec().origin 必须都在 /home/xilinx/demoir/ps/pynq_deps_t14 内）
    bin_net        ✓  /home/xilinx/demoir/ps/pynq_deps_t14/bin_net.py
    bin_engine     ✓  …/bin_engine.py        bin_ref_numpy ✓  …/bin_ref_numpy.py
    qcache ✓  board_safe ✓  catpack ✓  e1e2_sim ✓  bpack ✓  c8deq ✓  q8 ✓  bilinear_c ✓
    全部落在闭包内 = ✓

[3] [DEPS]（跑前 = 零副作用预测；跑后由 dump 覆盖为实测权威）
[DEPS] q8=fast c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast  ⇒ deps_closure_ok=True
    sys.path 前 6 项 = ['/home/xilinx/demoir/ps/pynq_deps_t14', '…/drv', '…/drv',
                       '/usr/lib/python310.zip', '/usr/lib/python3.10', '/usr/lib/python3.10/lib-dynload']
```

**板端两条判据**（在板端目录里跑）：

```
$ cd /home/xilinx/demoir/ps/pynq_deps_t14 && md5sum -c deps_closure.txt      → rc=0（15/15 OK）
$ python3 drv/deps_closure.py --check --src . --dir . --file deps_closure.txt
  ✓ deps_closure --check PASS：11 模块 / 15 文件；目录 = /home/xilinx/demoir/ps/pynq_deps_t14
```

⇒ 闭包落点、`find_spec`、两条判据在**真实板端**全部成立。

### 5.3 一次性成本（显式计出去，不进稳态）

```
步骤 0b：q8/c8deq/bpack 一次性装载（单独计时，不进稳态）
  q8     load=0.222 s  loaded=True  why=ok
  c8deq  load=1.375 s  loaded=True  why=ok
  bpack  load=1.780 s  loaded=True  why=ok
  **一次性装载合计 = 3.377 s**
```

### 5.4 两臂实测（同一 boot、同一 arena `@0x30100000` 66.22 MB、同一输入）

输入自证：`seed=1 (3,256,256) sha1 = cfc75bc3f15d306b` = 冻结基线输入 ✓

| 臂 | total | PL | PS | prologue | epilogue | interp | sha1（out1/out2/out3） | `run_valid` | `perf_warm` |
|---|---|---|---|---|---|---|---|---|---|
| numpy（`BIN_BILINEAR_C=0`） | **8.480 s** | 3.233 | 5.705 | 1.672 | 0.265 | **1.960** | `9b1940c8955b / f86a7035621b / 025757a83c39` **3/3** | True | True |
| C（`BIN_BILINEAR_C=1`） | **6.489 s** | 2.222 | 3.819 | 1.595 | 0.256 | **0.260** | 同上 **3/3** | True | True |

$$\Rightarrow\ \textbf{8.480 / 6.489}\ \text{vs 历史 } \textbf{8.261 / 6.497} \Rightarrow \textbf{numpy} +2.65\%,\ \textbf{C} -0.12\%$$

**"基线正式可复现"这句话今天落地了**：依赖闭包补齐后，同一 boot 内
numpy 臂 8.480 s、C 臂 6.489 s，两者差距（1.99 s）与 `interp` 桶差（1.96 s − 0.26 s = 1.70 s）+ 相关税一致；
两臂 sha1 与冻结基线**逐位相同**（`9b1940c8955b/f86a7035621b/025757a83c39`）。

**性能门槛在板端的自校准**（`BIN_PROFILE=1`、C 臂、热态）：

```
e1e2_prof   = {'views': 0.119, 'descviews': 0.163, 'q8': 0.605, 'q8b': 133988352.0, 'q8n': 122.0,
               'f32': 0.615, 'desc': 0.087}
perf_gate   = []     实测 q8 = 0.605 s < 0.915 s ，epilogue = 0.268 s < 0.390 s
run_valid   = True   perf_warm = True      ← **无告警（不是漏判：判过了，在门槛内）**
```

⇒ 门槛常数 **0.61 / 0.26 就是板端热态真值**（误差 −0.8% / +3%），1.5× 留量 0.915 / 0.390，
对 numpy 回落（q8≈3.6 / epi≈1.19）有 4~6× 的区分度，**不误报也不漏报**。

### 5.5 跑动前后 15 文件 md5（板端逐一）

跑前/跑后**完全一致**（`md5_same=True`），且**板端 15 个 md5 与主机副本逐一对齐**（见 §7）。
收工：`sync; sync; poweroff`，`ping` 100% 丢包 = 已关机。

---

## 6. 本次新增的三条发现（都不是"顺带"，都是判据逼出来的）

1. **一次性内核装载成本被算进了"性能桶"**：`.so` 已编好，`load()`+逐位自检仍要
   `q8 0.222 / c8deq 1.375 / bpack 1.780 s`，**落在首个整网的 prologue/epilogue 里**
   （首跑 epi 1.632 s vs 热态 0.257 s，×6.4）。⇒ 门槛必须只判热态（§2.1 修正 1 的来源）。
   **误报的代价**：如果不修，`board_v39_verify` 主流程的**首行 256² 永远 `run_valid=False`**。
2. **性能门槛在 x86 上抓不到回落**（x86 numpy 比 C 内核还快：0.097 s vs 0.159 s）。
   ⇒ 两个机制**互补**而不是冗余：x86/CI 靠 `[DEPS]` 漂移，板端两个都灵。
   这条直接支持 §0 的结论：**没有任何"单点测量"能同时覆盖两类事故**。
3. **闭包校验器在"换目录跑"时会自伤**（`# 源根:` 参与一致性比较）⇒ 板端 `--check` 永远 FAIL。
   任务书要求的"板端先跑一次 `--check`"在原实现下**根本做不到**。已修（§2.1 修正 2）+ 四个负例守住。

---

## 7. 跑点对齐表（报告要求项）

| 项 | 值 |
|---|---|
| 位流 | `design_1_150v39.bit`，md5 **`602725b86941864eeeeab3be005e1e81`**（v39；**v40 `04a62581` 禁用、v41 不可上板**） |
| `deps_closure.txt` md5 | **`ffd2b678a169d327e10596b7e39b0a3d`**（11 模块 / 15 文件 = 11 `.py` + 4 `.c`） |
| 板端闭包目录 | `/home/xilinx/demoir/ps/pynq_deps_t14/`（`drv/` 放驱动与校验器） |
| 板端 `md5sum -c` | rc=0（15/15 OK），**跑前、跑后、收工前**各一次 |
| 板端 `--check` | PASS（11 模块 / 15 文件；目录 = 该目录） |
| 15 文件 host↔board | **15/15 逐一对齐**（下表） |
| 运行点 | `fclk 150`（读回 150）、24 槽、`BIN_BILINEAR_C` 显式臂、`BIN_PROFILE` 按需、`BIN_REQUIRE_KERNELS` 未开 |
| 输入 | `seed=1 (3,256,256)` sha1 `cfc75bc3f15d306b` = 冻结基线 |
| sha1 | 两臂均 `9b1940c8955b / f86a7035621b / 025757a83c39`（3/3 冻结） |

15 文件逐一（左=主机副本 `worktrees/T-14-deps/ps/pynq`，右=板端，**全部相同**）：

| 文件 | md5 | host=board |
|---|---|---|
| `bin_net.py` | `2e077940df90e5ac59583ae65af10b54` | ✓ |
| `bin_engine.py` | `1967aa8a57dabd40057a8a462785b132` | ✓ |
| `bin_ref_numpy.py` | `a19ba76c56df56f517c8640a62eb6051` | ✓ |
| `qcache.py` | `4157ba3d7e35b8514d71eef29f0ee6a5` | ✓ |
| `board_safe.py` | `d4d62fd06f74c963b643cec59566bf54` | ✓ |
| `catpack.py` | `30149d936c9eea6cd510b200d1cd6d5c` | ✓ |
| `e1e2_sim.py` | `2c4a6180fb74f88abb5e13b8e641b489` | ✓ |
| `bpack.py` / `bpack.c` | `63d6a936…` / `2a8e6c55…` | ✓ |
| `c8deq.py` / `c8deq.c` | `af33f47f…` / `5019fab0…` | ✓ |
| `q8.py` / `q8.c` | `e794a43d…` / `c744794c…` | ✓ |
| `bilinear_c.py` / `bilinear_c.c` | `cd43b1d6…` / `1cd97282…` | ✓ |

---

## 8. 未覆盖 / 风险（不许当做过）

1. **`bilinear_c` 仍不在 `[DEPS]` 行里**（opt-in，默认关）：它的**文件齐不齐**由闭包清单管，
   **装没装上**由 `step_bilinear()` + `runpoint["bilinear_c"]` 管。今天 C 臂实测 `kernel_loaded=True why=ok`。
2. **`perf_gate` 只对 256² 判**（其它分辨率没有历史值）。这是设计，不是遗漏；
   若以后要判 64²，必须先有 64² 热态基线（今天没测）。
3. **串行臂（`pipeline=False`）没有性能桶**，因此也没有门槛（只带 `kernel_deps/deps_closure_ok/run_valid`）。
4. **`ab_arms.py` 仍只有补丁**（该文件只存在于 `runs/board_ops/20261102/frozen_code/`，红线不许动）。
   即使不打补丁，`bin_net.run()` 也会为它的每一臂打 `[DEPS]` 行。
5. **`CC=/bin/false` 的端到端"门槛告警"在 x86 上做不到**（§3.2）；板端可以做到，但本次只批准
   两臂各 1 次，**没有在板端故意制造回落**（会把一个已验过的跑点弄脏）。触发②用
   "受控门槛 + 板端真值"两路替代，判据等价。
6. 板端 `ps/pynq_deps/`（上一位的目录）**未动**；我的闭包在 `ps/pynq_deps_t14/`。

---

## 9. 给主代理（合并用）

- **副本**：`worktrees/T-14-deps`（只此一份，第二棒是唯一属主；第一棒已被中断）
- **改的文件**：`ps/pynq/bin_engine.py`、`ps/pynq/bin_net.py`、`ps/pynq/board_v39_verify.py`、
  `ps/tools/deps_closure.py`、`tools/board_repeat.py`（第一棒补丁，已在本副本）
- **生成物**：`runs/board_ops/20261102/deps_closure.txt`（`ffd2b678…`，**合入后必须再 `--gen` 一次**，
  因为合入会改 `bin_engine.py` 的内容 ⇒ md5 变）
- **合并后必须重跑的两条判据**：
  ```
  cd pynq_specialized_code/ps/pynq && md5sum -c ../../../runs/board_ops/20261102/deps_closure.txt
  python3 ../tools/deps_closure.py --check --src . --dir .
  ```
- **登记表建议行**（`docs/PLAN_TASKS.md`）：

| 编号 | 计划 | 负责 | 判据 | 交付物 | 状态 | 依赖 |
|---|---|---|---|---|---|---|
| T-14 | 依赖闭包可见性 + fail-fast + 性能门槛 | `T-14-deps`（第二棒） | `md5sum -c` rc=0 **且** `--check` rc=0；`[DEPS]` 全 `fast`；`BIN_REQUIRE_KERNELS=1` 缺内核必 raise；门槛 0.915/0.390 s 超则 `run_valid=False`；板端两臂 sha1 3/3 | 副本 diff + `deps_closure.txt` + `GATES_T14_deps.md`(①②) + 本文件(④+板端) | **待复核**（合并由主代理做） | ①② |
