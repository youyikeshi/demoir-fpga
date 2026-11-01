# 冻结点 frozen_code —— 20261102 跑点用的**确切**代码

> **一句话**：这就是 20261102 跑点用过的确切代码。任何后续改动都**不得覆盖**本目录；
> 要改就新建 `frozen_code_<新日期>/`，让每个跑点都有自己的代码快照。

**冻结时刻**：2026-11-01 23:52–23:53（主机），源码来源 **git HEAD `b58964e`**。
**机制**（不是纪律）：`ab_arms.py` 第一行 `sys.path.insert(0, HERE)` ⇒ 板上从
`/home/xilinx/demoir/ps/pynq_frozen/` 跑时，import 到的一定是本目录的代码。
**禁止**再从 `ps/pynq/` 跑测量（那棵树随时被改）。

## 为什么是 git HEAD，而不是工作树

工作树在 2026-11-01 23:47–23:49 期间**正被两个子代理编辑**（只读 md5 连续采样取证）：

| 时刻 | `bin_net.py` | `bin_engine.py` |
|---|---|---|
| 23:46（接手时） | `cdf4b9a9` 105174 B | `cac7b74f` 179240 B |
| 23:47:29 | `c0855542` 106900 B | — |
| 23:48:38 | `9cbc5ddf` 110117 B | `17fc7f65` 179403 B |
| 23:49:05 | — | 仍在增长 |

把这份树冻成"基线" ⇒ 基线里会悄悄包含两个**未经逻辑验证**的优化（q8 加固 / 描述符缓存）。
⇒ 改用 git HEAD：任何机器 `git checkout b58964e` 都能逐字节复现，且不含半成品。

被作废的那份快照保留在 `../frozen_code_VOID_2347/`（含 `VOID.md` 说明为什么不成立）。

## 文件指纹（MD5SUMS 可 `md5sum -c` 复核）

| 文件 | md5 | 来源 |
|---|---|---|
| `bin_net.py` | `3f154b75f6526a096f78001aaa1e785b` | git HEAD |
| `bin_engine.py` | `ddfe6b6ffdfea33e76959748825d7af3` | git HEAD |
| `bin_ref_numpy.py` | `5a3bbd8b36a5a248001943b78ab695b0` | git HEAD |
| `bilinear_c.py` | `48134b7b0056ea71be77a965f0adf633` | git HEAD |
| `bilinear_c.c` | `1cd97282ee30a092425602c0d09ef7c1` | git HEAD |
| `qcache.py` | `d2dc2517f1bad49ab6c61f0c3ae96944` | git HEAD |
| `board_v39_verify.py` | `e9877b79f3f7403b4ddfa85a305f1068` | git HEAD |
| `ab_arms.py` | `f9c61f7e33b6faef4e68b4dfaeefcf89` | 本目录新写（只驱动，不改算法） |
| `bin_ref_numpy_16efab60.py` | `16efab6086c4a9cbb30501a67230504e` | ⑦ 考古找回（见下） |

## "能跑"门禁（冻结前已过，不是声称）

1. `python3 -m py_compile *.py` —— 全部通过。
2. 主机 dry-run：`python3 ab_arms.py --dry-run`
   - C 臂 `kernel_loaded=True, why=ok`（主机 cc 编译 `bilinear_c_x86_64.so`）
   - `selftest("full")` = **29 REAL + 7 EDGE = 36 形状**全过（0.13 s）
   - 同一 64² 整网跑两臂（`engine=None` 纯 numpy 参考路径）⇒ **输出逐位相同**
   - **注意**：dry-run 的 sha1 **不等于**板端 `FROZEN`（主机走参考 backend，不是 PL 卷积）。
     dry-run 只证明"能 import / 能切臂 / 两臂一致"，**不证明板端数值**。
3. 主机 dry-run 产生的 `bilinear_c_x86_64.so` 与 `__pycache__/` **已删除**，不随包上板
   （跨架构 + 陈旧字节码两道风险）。

## 与旧跑点的差异（★ 报告里必须写，否则数字会被误比）

### 对 2026-11-01 跑点（6.485 s / 6.455 s，"boardops"）

| 文件 | 本冻结点 | 20261101 跑点 |
|---|---|---|
| `bin_net.py` | `3f154b75` | `cdf4b9a9` |
| `bin_engine.py` | `ddfe6b6f` | `f848a29c` |
| `bin_ref_numpy.py` | `5a3bbd8b` | `a19ba76c` |
| `bilinear_c.py` | `48134b7b` | `cd43b1d6`（默认值已改 `"0"`） |
| `board_v39_verify.py` | `e9877b79` | `6beead81`（含 `runpoint()`/`set_arm()`） |

⇒ 本跑点比那一次**少了 acq 计数器等改动**，**总时间不可直接相减**。
唯一可以跨跑点比的是 `interp` 桶（同名 `prof["interp"]`，但也要注明跑点）。

### 对 8.096 s 冻结基线（v39，2026-10-31 18:34）

- `bit 602725b8` / `qcache d2dc2517` / `bin_net 3f154b75` / `bin_engine ddfe6b6f`
  **完全一致**（见 `docs/baseline_20261031_v39/README.md`）。
- 唯一差异：`bin_ref_numpy.py` = `5a3bbd8b` vs 基线 `16efab60`。
  两者 diff **只有** C 内核挂钩（`_C_KERNEL` / `_bilinear_c_kernel()` / 把原
  `bilinear_f32` 拆成"分派器 + `_bilinear_f32_numpy`"）+ docstring；
  **numpy 内核体逐字节相同** ⇒ numpy 臂的**数值**与基线可比，**绝对时间**不可跨跑点比。

## 已知偏离（不许隐藏；写进报告）

1. 本快照 `bilinear_c.py` 第 318 行默认值是 `"1"`，而第 7 行 docstring 写"默认关"——
   这正是第一棒修掉的矛盾。`ab_arms.py` **每臂显式**写 `BIN_BILINEAR_C=0/1`，
   且用 `set_arm()` 返回的 `kernel_loaded` 判定"这条臂到底是不是 C 臂"，绝不看默认值。
2. `board_v39_verify.py`（HEAD 版）**没有** `runpoint()` / `set_arm()` / 一次性成本外移。
   本目录**不改它**（改了就"不是 HEAD"了）：运行点记录与切臂都在 `ab_arms.py` 里做。
3. HEAD 版 `V.run_net` 把 `seed` 硬编码为 1，所以 `ab_arms.py` 里有一份**薄拷贝**
   `run_net_seed()`（只把 seed 变参数）。忠实性由两个自证保证：
   seed=1 时复现 `FROZEN` 三件套；协议自证组 P 用**原函数**再跑一次比对输入与三级输出。

## ⑦ 考古结论：`bin_ref_numpy.py 16efab60` **已找回**

- 位置：`/tmp/s2frozen/pynq/bin_ref_numpy.py`（mtime 2026-10-31 13:20:49，58673 B）
- md5 核对：`16efab6086c4a9cbb30501a67230504e` ✓ 与 `docs/baseline_20261031_v39/README.md`
  以及 `docs/PLAN_1second.md:2358` 记载的基线值**完全一致**
- 全盘唯一性：按该文件字节数 58673 扫 `/tmp`、`/home/felix`、仓库 ⇒ **只有这一份**
- 结构佐证：它**不含** `_C_KERNEL`/`bilinear_c` 字样（C 内核是此后才合入的）
- 已复制为本目录 `bin_ref_numpy_16efab60.py`，`py_compile` 通过
- ⇒ **8.096 s 那版 PS 代码现在是可逐字节复现的**（此前"仓库与 git 里都没有"的判断仍成立，
  但"永久丢失"不成立：它存在于 `/tmp` 快照里）

## 目录内容

```
frozen_code/
├── ab_arms.py                  # 双臂驱动（本跑点的唯一入口）
├── bin_net.py bin_engine.py bin_ref_numpy.py bilinear_c.py bilinear_c.c
├── qcache.py board_v39_verify.py
├── bin_ref_numpy_16efab60.py   # ⑦ 考古产物（不参与本次 import）
├── MD5SUMS                     # md5sum -c 可复核
└── README.md                   # 本文件
```

板上对应目录：`/home/xilinx/demoir/ps/pynq_frozen/`（传完在板端再 `md5sum -c` 一次）。
