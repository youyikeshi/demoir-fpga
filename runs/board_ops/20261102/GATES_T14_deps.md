# T-14-deps 门禁实测 —— ①② 依赖闭包清单 + 启动可见性

**执行者**：机制加固工程师（T-14-deps）
**日期**：2026-11-02
**作用域**：只改 PS 侧 Python；**未上板、未碰 RTL、未起 vivado、未动 `runs/board_ops/20261102/frozen_code/`**
**红线遵守**：`pynq_specialized_code/ps/pynq/`（main）**一个字节未改**；`q8.c/c8deq.c/bpack.c` 未改；未改算法语义

---

## 0. 交付物

| 项 | 值 |
|---|---|
| 副本 | `/media/felix/Data/coding/demoir-fpga/worktrees/T-14-deps`（detached，基线 commit `825cbb9`） |
| 基线 commit | `825cbb9 sync: mirror main working tree (ps/pynq 6 files + tools/export_network) as T-14 baseline` |
| `git diff --stat` | 3 files `+275 / -1`：`bin_engine.py +202`、`bin_net.py +27/-1`、`board_v39_verify.py +47` |
| 清单 | `runs/board_ops/20261102/deps_closure.txt`（**生成物**，`ps/tools/deps_closure.py --gen`） |
| 校验器 | `ps/tools/deps_closure.py --check [--dir <冻结点>]` |
| 两个 untracked 驱动的补丁 | `runs/board_ops/20261102/deps_runpoint_patches/{ab_arms,board_repeat}_deps_runpoint.diff` |

### 0.1 每个文件的改后 md5（副本内）

| 文件 | 改后 md5 | 改前 md5（基线） |
|---|---|---|
| `ps/pynq/bin_engine.py` | `52a95847327e0e5301e3fc43c8eb9ef4` | `17fc7f654540484595af80e83bcb68e7` |
| `ps/pynq/bin_net.py` | `22fb3ca2442854b5f859cf274ca2cd2d` | `9cbc5ddf1404a08c7f1447f760639bab` |
| `ps/pynq/board_v39_verify.py` | `0fdf3e6a4d124cc759b414bbbc3444e7` | `4d6a2011f1f88735f772acbf9a0c38eb` |
| `ps/tools/deps_closure.py`（**新增**） | `2731a4d73e78c14801423b4bd3e3eedd` | — |
| `tools/board_repeat.py`（untracked，**补丁**） | `806e5f2ecc261423f7e789ebe87ddad5` | `tools/board_repeat.py` 原文件 + `deps_runpoint_patches/board_repeat_deps_runpoint.diff` |
| `runs/board_ops/20261102/deps_closure.txt` | `11f1ce0410070d276273c2cfdd9a418f` | —（覆盖了原来不存在的草稿位置；草稿在 `probe_cma/`） |

> ⚠ 副本里有三个 `*_x86_64.so`（`q8/c8deq/bpack` 主机现场编译产物，`.gitignore` 已挡，基线 `.gitignore` 还没带这条 ⇒ 显示为 untracked）与
> `runs/e1e2_direct/`（判据脚本自己的输出目录）。这两个都不该入库。

---

## 1. ① 依赖闭包清单：从静态扫描**推出来**，不是手挑

### 1.1 闭包 = 11 模块 / 15 文件（3 个根 + 8 个传递依赖）

```
根（任务书指定）：bin_net, bin_engine, bin_ref_numpy
传递依赖：        qcache, bilinear_c, board_safe, catpack, e1e2_sim, bpack, c8deq, q8
文件：            11 × .py + 4 × .c（q8.c, c8deq.c, bpack.c, bilinear_c.c）
系统库：          libc.so.6（bin_ref_numpy 的 malloc_trim；不随包、不入 md5）
```

扫描方式（`deps_closure.py`，**AST，不 import 被测代码**）：

- `ast.Import` / `ast.ImportFrom`（含函数内惰性 import，并记录 `scope` 与是否在 `try:` 里）；
- `ctypes.CDLL(` / `_fast_lib(` 加载点（§1.3）；
- `<mod>.c` 字面量（决定该模块是否需要 `.c`）。

**事故里漏掉的三个（`q8`/`c8deq`/`bpack`）全部由扫描自动发现**，不依赖任何人记得它们。
**§8.5 点名的三项埋雷**（`e1e2_sim` / `catpack` / `board_safe`）同样在闭包里，并在文件头上单独标为"已知埋雷（冷路径）"。

### 1.2 三项判据（都实测过）

| # | 判据 | 场景 | 结果 |
|---|---|---|---|
| ① | `md5sum -c deps_closure.txt` | 源码树 `ps/pynq/` | **rc=0**（15/15 成功） |
| ① | `md5sum -c deps_closure.txt` | 正确冻结点（15 文件全拷到 `/tmp/t14/frozen15`） | **rc=0** |
| ① | `md5sum -c deps_closure.txt` | **事故现场** `frozen_code/` | **rc=1**（9 个 MISSING：3 模块 × .py+.c） |
| ② | `deps_closure.py --check` | 源码树 | **PASS** |
| ② | `deps_closure.py --check --dir /tmp/t14/frozen15` | 正确冻结点 | **PASS** |
| ② | `deps_closure.py --check --dir frozen_code/` | **事故现场** | **FAIL 14 项** |

事故现场的失败清单（原文节选）：

```
   ⚠ 冻结点目录还有闭包外的 .py（3 个，不影响判据）：['ab_arms', 'bin_ref_numpy_16efab60', 'board_v39_verify']
✗ deps_closure --check FAIL（14 项）
   · md5 不符 bin_net.py：清单 22fb3ca2... / 实际 3f154b75...
   · md5 不符 bin_engine.py：清单 52a95847... / 实际 ddfe6b6f...
   · md5 不符 bin_ref_numpy.py：清单 a19ba76c... / 实际 5a3bbd8b...
   · md5 不符 qcache.py：清单 4157ba3d... / 实际 d2dc2517...
   · 缺文件 board_safe.py / catpack.py / e1e2_sim.py / bpack.py / bpack.c / c8deq.py / c8deq.c / q8.py / q8.c
   · md5 不符 bilinear_c.py：清单 cd43b1d6... / 实际 48134b7b...
```

解读（**不许误读成"冻结目录被污染"**）：

- 9 个"MISSING" = 本次事故的**直接根因**，工具一次就点出来了；
- 5 个"md5 不符"是**预期**：清单描述的是"今天的代码"（工作树），而 `frozen_code/` 是 `git HEAD b58964e`
  的快照 ⇒ `bin_net 3f154b75 / bin_engine ddfe6b6f / bin_ref_numpy 5a3bbd8b`（`ab_arms.py` 的 docstring 已自述
  它是 `5a3bbd8b`，不是 8.096 基线的 `16efab60`）；`bilinear_c.py 48134b7b` = HEAD 版默认值 `"1"`（今日工作树已改成 `"0"`）；
- 3 个"闭包外 .py"只是**警告、不判失败**（`ab_arms.py`/`board_v39_verify.py` 是驱动，`bin_ref_numpy_16efab60.py` 是历史副本）。
  **"多"不害人，"缺"才害人** —— 判据只对"缺 / 不一致 / 过期"判失败。

> **为什么还需要 ② 而不能只靠 ①**：`md5sum -c` 只对**清单里已经写了的行**负责。
> 事故现场只列了 7 个 `.py` 时，`md5sum -c` 对那 7 个全是"成功"——它**没有任何办法**告诉你少列了 6 个文件。
> ② 重新扫源码 AST：**依赖图变了而清单没重生成 ⇒ rc=1**。这是"清单本身完整不完整"的唯一机械判据。

### 1.3 `.so` 决策（写进清单头）

4 个 native 模块（`q8/c8deq/bpack/bilinear_c`）都是首次使用时 `cc -O3 -std=c99 -fPIC -shared` 现场编译
（x86 会先试 `-mfpu=neon -mfloat-abi=hard`，失败再退无 NEON 那套）成 `<mod>_$(uname -m).so`，编在模块自己目录。

⇒ **冻结点必须同时带 `.py` 和 `.c`**：只带 `.py` 编不出来（缺 `.c`）；只带 `.so` 跨架构失效。
清单头还登记了每个模块的加载点（`q8.py` 5 处、`c8deq.py` 5 处、`bpack.py` 5 处、`bilinear_c.py` 2 处）。

---

## 2. ② 启动可见性：一行打印 + 两个 stats 字段

### 2.1 打印（实测原文）

```
[DEPS] q8=fast c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast  ⇒ deps_closure_ok=True
```

- 位置：`BinNet.run()` 里、`build_graph` 之前（**装配完成后**）⇒ 任何 driver / 任何 `net.run()` 都会打；
- **每进程一次**（`_DEPS_STATE`）⇒ `board_repeat --n 5` 不会刷 5 行；
- 不健康时**多打**几行"缺哪个 / 为什么 / 怎么修 / `sys.path` 前 6 项"（见 §3）。

### 2.2 stats 字段（`net.run()` 返回的第二个值）

```python
stats["kernel_deps"]      # {q8: {state, why, source, module, so, so_md5, hint}, ... 6 项}
stats["deps_closure_ok"]  # bool
```

实测（正常闭包）：

```
kernel_deps={'q8': 'fast', 'c8deq': 'fast', 'bpack': 'fast', 'safe': 'fast', 'e1e2': 'fast', 'catpack': 'fast'}
deps_closure_ok=True
```

- 流水路径（`run()`）与串行路径（`_run_serial()`，A/B 的对照臂）**都写了**；
- `kernel_deps` 是**跑完后**读的（此时 `q8._STATE`/`c8deq._STATE`/`bpack._STATE` 已是实测结果 ⇒ **权威**），
  开头那一行是"装配前预测"。两者不一致时 `deps_drift()` 会给出差异，`run()` 补一行 ⚠ 并**强制** `deps_closure_ok=False`（见 §3.3）。

### 2.3 三处 driver 都进跑点

| driver | 落点 | 状态 |
|---|---|---|
| `ps/pynq/board_v39_verify.py` | `runpoint()` 写 `kernel_deps_probe` + `deps_closure_ok`；`dump()` 时刷新为**实测** `kernel_deps`（"kernel_deps = 跑完实测（权威）"）；主流程打一行 `[DEPS]`，为 False 时打"本次跑点不许进基线表" | ✅ 已改（副本内） |
| `tools/board_repeat.py` | 复用 `V.runpoint()`（已自动带这两个字段）+ 额外打一行 `V.deps_summary()` | ✅ 补丁（untracked 文件） |
| `frozen_code/ab_arms.py` | 落盘前把 `kernel_deps`/`deps_closure_ok` 写进 `R["runpoint"]` | ⚠ **只交补丁**（见 §6 说明） |

`board_v39_verify.py` 的字段实测（主机用 stub `pynq` 跑 `runpoint()`+`dump()`）：

```
deps_summary: [DEPS] q8=fast c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast  ⇒ deps_closure_ok=True
probe keys: ['bpack', 'c8deq', 'catpack', 'e1e2', 'q8', 'safe']
q8: fast | so: .../ps/pynq/q8_x86_64.so
dump 后 runpoint 键: [..., 'deps_closure_ok', 'kernel_deps', 'kernel_deps_note', 'kernel_deps_probe', ...]
dump 后 kernel_deps(q8): fast f6a918a283451e337b6e9f51453286c6      ← .so 的 md5 也留证
```

---

## 3. 门禁 ③：可见性有效性（**故意制造回落**）—— 必须做，已做

### 3.1 场景：`sys.path` 指向不含 `q8` 的目录

`/tmp/t14/frozen_noq8/` = 完整闭包**只少 `q8.py` / `q8.c`**（其余 10 个模块 + 3 个 `.c` 都在）。
用 `env -u PYTHONPATH python3 -c "sys.path.insert(0,D); os.chdir(D); import bin_engine"` 跑（`sys.path` 里没有真 `ps/pynq`）。

```
--- A) 只调 report_deps()：不跑网、不编译 ---
[DEPS] q8=fallback-numpy c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast  ⇒ deps_closure_ok=False
[DEPS] ⚠ 依赖闭包不完整：这一跑跑的不是快路径，**数字不可当基线**
[DEPS]   ✗ q8       fallback-numpy f32→int8 量化（prologue；缺 ⇒ prologue ×3.69）
[DEPS]             原因：模块不在 sys.path（缺 q8.py）
[DEPS]   修法：把 ps/pynq/{q8,c8deq,bpack}.{py,c} 与 e1e2_sim.py / catpack.py / board_safe.py 放进同一个目录（冻结点 = 依赖闭包，不是文件清单）；
[DEPS]         清单见 runs/board_ops/20261102/deps_closure.txt，校验 `python3 ps/tools/deps_closure.py --check --dir <冻结点目录>`。
[DEPS]   sys.path 前 6 项：['/tmp/t14/frozen_noq8', '/tmp/t14', '/usr/lib/python310.zip', ...]

--- B) 真的走一次量化 ---
q8() -> None  dtype= int8  sum= -206          ← 回落**确实发生了**（不是"打了个字"）
```

**真跑整网**（`BinNet.run()`，hw=64，ref 后端）：

| 目录 | `[DEPS]` 行 | `kernel_deps` | `deps_closure_ok` | sha1（uint32） |
|---|---|---|---|---|
| 完整闭包 | `... q8=fast ...` | 6×fast | **True** | `104710f69c4d,2ac6efec2325,8ab595fcf7cd` |
| 缺 `q8` | `q8=fallback-numpy ...` | q8=fallback-numpy | **False** | `104710f69c4d,2ac6efec2325,8ab595fcf7cd` |

⇒ 判据满足：**回落被打印、被写进 stats、被判定为无效跑点**；同时 **sha1 不变**（证明回落只是性能问题，不是数值问题——
这正好说明"只看 sha1 抓不到这类事故"，所以必须靠这一行）。

### 3.2 场景：`q8.py` 在、`.c` 在、但编不出来（`CC=/bin/false`）

```
--- 跑前（预测：.c 在位、cc 在位 ⇒ fast）---
[DEPS] q8=fast c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast  ⇒ deps_closure_ok=True

--- 跑后（实测）---
[DEPS] q8=fallback-numpy c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast  ⇒ deps_closure_ok=False
drift = [('q8', 'fast', 'fallback-numpy')]
```

⇒ **"预测 fast、首次使用时才现原形"这种最难查的形态也被抓住**（`deps_drift()`）；`run()` 会补一行 ⚠ 并判无效。

### 3.3 为什么探测**不能**直接调 `_q8()/_deq_kernel()/_bpack()`

那三行一旦被调用就会 `cc -O3` 现场编译 `.so`（板端 ~1.5 s）。在 `run()` 开头调它 = 把"一次性编译成本"
从 `prologue/epilogue` 里**挪走** ⇒ 计时口径被改（正是事故报告 §4.4 "首跑 vs 稳态"那条纪律）。
所以探测只用：`find_spec`（不 import）+ 模块自己的 `_STATE`（已试过则权威）+ `.c`/`.so`/`cc` 在位性。
**跑完再读一次** `_STATE` ⇒ 权威值。实测确认：正常路径下 `_q8()` 首次调用仍发生在 prologue 里，未被提前。

---

## 4. 门禁 ⑤：默认关时零影响（**逐字**口径）—— 已做

### 4.1 命令行（`bin_net.py --dry-run --hw 64`，同参数、同输入）

- `base1` / `base2` = 基线代码跑两次；`new` = 改后代码跑一次。

```
三份输出行数：base1=30  base2=30  new=31
```

原始 diff（`base1` vs `base2`，**同一份代码跑两次**）：

```
24c24   总耗时 1.24 s   →   总耗时 1.14 s
26c26   PS 时间 0.25 s   →   PS 时间 0.09 s
28,29c28,29  理论下界 0.25 s / op 分解 add=0.000s cat=0.004s ... → 0.09 s / cat=0.005s ...
差异行数: 8
```

⇒ 计时数字本来每次都不一样（**既有噪声**，与本次改动无关）。

把浮点数一律归一化成 `T` 之后：

```
base1 vs base2  ⇒ IDENTICAL                    （同一份代码，归一化后逐字相同）
base1 vs new    ⇒ 只多 1 行：
   19a20
   > [DEPS] q8=fast c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast  ⇒ deps_closure_ok=True
```

**判据满足：除这一行 `[DEPS]` 外，输出逐字相同**（且新增行是 1 行，不是 2 行 —— 每进程一次确实生效）。

---

## 5. 门禁 ① / ②：逐字节 + sha1 三件套 —— 已做

用**冻结判据脚本** `ps/tools/verify_lazy_net.py` 的 `run_case()`（FakePL = `conv_rtl` + `e1e2_sim`），
把三个输出按 **uint32 视图**求 sha1（float32 字节的 uint32 视图是双射），跑 A(ref) / B(e1e2_lazy) / C(e1e2_nolazy)：

| hw | 模式 | 冻结基线 | 改动前 | 改动后 |
|---|---|---|---|---|
| 64² | ref / lazy / nolazy | `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd` | 三模式全部相同 | 三模式全部相同 |
| 256² | ref / lazy / nolazy | `392d1ae02761 / 7f68fb00d2b5 / aa03751a2d1c` | 三模式全部相同 | 三模式全部相同 |

`x0`（seed=1）：64² `u32=69b6c57d43dd`、256² `u32=acdb772f481d`（前后一致）。

改动前（`base_dump.log`）vs 改动后（`final_dump.log`）的 **diff 只有 2 行**，都是新增的 `[DEPS]`（两个进程各一行）：

```
2a3
> [DEPS] q8=fast c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast  ⇒ deps_closure_ok=True
9a11
> [DEPS] q8=fast c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast  ⇒ deps_closure_ok=True
```

附加回归：`python3 bin_engine.py --selftest <pack>` ⇒ **自检全部通过 ✓**（含惰性句柄/错误码/逐位判据）。

---

## 6. 门禁 ④（fail-fast `BIN_REQUIRE_KERNELS=1`）—— **未做，等批**

按任务书"先交 ①②，我批后再做 ③④"：**本次没有实现 `BIN_REQUIRE_KERNELS`**，也没有任何默认行为之外的开关。
代码里已为它留好接口（`kernel_deps()` 给出 `state/why/module/so`，`report_deps()` 是唯一打印点），
实现时只需在 `report_deps()` 里加一个 `raise`，不动其它任何路径。

---

## 7. 边界与未覆盖（不许当做过）

1. **没有上板**。真板端 `[DEPS]` 输出未验（板端 `armv7l` + pynq 3.1 + numpy 1.21.5 的行为未在本机覆盖）。
   如果要验：一次最小验证（`tools/boardlock.sh` + 一次 load + 四项体检 + 金丝雀 + v39 `602725b8`）即可，
   **待批准后执行**（我不会自己上）。
2. **`ab_arms.py` 只能交补丁**：它只存在于 `runs/board_ops/20261102/frozen_code/`（**红线：不许动**）。
   补丁已 `patch --dry-run` 验证可干净应用。**即使不打这个补丁**，`bin_net.run()` 也会为它的每一臂往
   stdout 打 `[DEPS]` 行 ⇒ 日志里仍有判据，只是 JSON 缺字段。
3. **`bilinear_c` 不在 `[DEPS]` 行里**：它是 opt-in（`BIN_BILINEAR_C=1` 才加载 `.so`，默认关）。
   它的**文件齐不齐**由 `deps_closure.txt` 管；它的**装没装上**由 `board_v39_verify.step_bilinear()` 与
   `runpoint["bilinear_c"]` 管（已有机制）。若把它塞进这一行，默认跑点会永远 `deps_closure_ok=False`，反而是噪声。
4. **闭包扫描是静态的**：不覆盖 `__import__(变量)` 这种动态导入（本仓库没有），也不会判断"某个 import 只在
   极端分支上被走到"（热/冷路径是人写的 `DECL` 表，工具只负责"依赖关系不许漏"）。
5. **`deps_closure.txt` 的 md5 与仓库路径无关**（裸文件名，`cd <冻结点目录> && md5sum -c`），
   所以同一份文件可以同时校验 `ps/pynq/`、`/tmp/t14/frozen15/`、板端 `ps/pynq_frozen/`。
