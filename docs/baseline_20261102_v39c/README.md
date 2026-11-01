# 基线 v3 冻结（2026-11-02 01:50）

> 技能 §2.1/§2.7：**任何时刻都能用一行说清"当前基线是什么"**。这一行就是：

$$\textbf{v3} = \text{v39 位流 } \texttt{602725b8} + \text{T-14 依赖闭包} + \text{interp C 内核；150 MHz / 24 槽；}\mathbf{6.489\ s}\ (\text{numpy 臂 } 8.480\ s)\ \text{@256}^2$$

## 1. 运行点（所有数字只在此运行点下可比）

| 项 | 值 |
|---|---|
| 位流（**唯一可用**） | `pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v39.bit` md5 **`602725b86941864eeeeab3be005e1e81`** |
| HWH | `design_1_150v39.hwh` md5 `c1686bd8e080c1c7f46181d980543202` |
| 频率 / 槽数 | 150 MHz / 24 槽（32 槽算术上越界，见 PLAN §64）|
| 开关 | `BIN_PACK=e1e2 BIN_LAZY_DEQ=1 BIN_LAZY_SLOTS=24 BIN_CAT_FUSION=1 BIN_BILINEAR_C=1 Q8CACHE=0` |
| 输入 | seed=1 `(3,256,256)` float32，`x0_sha1 = cfc75bc3f15d306b` |
| 测量协议 | **同一进程内重复 N 次，取第 2 次起的中位/最好**（首跑含一次性装载，永远偏大）|

## 2. 代码 md5（= 本目录 `MD5SUMS` 的前 7 行）

| 文件 | md5 |
|---|---|
| `ps/pynq/bin_engine.py` | `eec568b2a507768f93d169b29c95c838` |
| `ps/pynq/bin_net.py` | `17d072ab2f378d056292f57488aed52c` |
| `ps/pynq/board_v39_verify.py` | `ff41b80b1c67f5102c56e9a26b8867f2` |
| `ps/pynq/bin_ref_numpy.py` | `5a3bbd8b36a5a248001943b78ab695b0` |
| `ps/pynq/qcache.py` | `d2dc2517f1bad49ab6c61f0c3ae96944` |
| `ps/pynq/bilinear_c.py` | `48134b7b0056ea71be77a965f0adf633` |
| `ps/tools/deps_closure.py` | `b6b85a4d64deda02c39e353b4e3a712c` |

⇒ 与 **v2+（`70dcfa1`）的差别只有 T-14**：`bin_engine` 由 `ddfe6b6f` → `eec568b2`、`bin_net` 由 `3f154b75` → `17d072ab`、`board_v39_verify` 由 `e9877b79` → `ff41b80b`，外加新文件 `deps_closure.py`。
⇒ 并**清掉了工作树里停放的 T-05 psdesc 改动**（`BIN_DESC_CACHE` 等 5 个 token 在 `ps/**/*.py` 上全为 0）。

## 3. 判据（本目录内可复算，全部 rc=0）

| 判据 | 命令 | 结果 |
|---|---|---|
| ① 闭包逐字节 | `cd pynq_specialized_code/ps/pynq && md5sum -c ../../../docs/baseline_20261102_v39c/deps_closure.txt` | **15/15 成功**，rc=0 |
| ② 闭包结构 | `python3 pynq_specialized_code/ps/tools/deps_closure.py --check` | **PASS 11 模块 / 15 文件**，rc=0 |
| ③ 数值不变 | `python3 pynq_specialized_code/ps/tools/verify_lazy_net.py --hw 64` | A=B=C 逐位相同，且 = 冻结 `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd` |
| ④ 运行点自校准 | 板端 `q8 ≈ 0.605 s`（基线 0.61）、`epilogue ≈ 0.268 s`（基线 0.26）| `run_valid=True` |

⚠ **③ 的跑点口径（T-15 发现的真问题）**：冻结 sha1 **与 BLAS 线程数耦合**。

| 口径 | out1/out2/out3 |
|---|---|
| 默认 BLAS 线程（冻结常量的原始跑点）| `104710f69c4d` / `2ac6efec2325` / `8ab595fcf7cd` |
| `OMP/OPENBLAS/MKL_NUM_THREADS=1` | `24222554c880` / `2fd745d93159` / `8ab595fcf7cd` |

⇒ **纪律（新增第 72 条）：sha1 只在同一 BLAS 线程口径内可比；跨口径比较无意义。**
⇒ 判据只要求"**同口径内 A=B=C**"，两种口径下都已验证成立（脚本自判 FAIL 是它内置的常量只对应默认口径，不是数值错）。

## 4. 板端复现证据（真板，2026-11-02 01:00）

| 臂 | total | prologue | epilogue | interp | sha1 |
|---|---:|---:|---:|---:|---|
| numpy | 8.480 | 1.672 | 0.265 | 1.960 | 3/3 |
| C 内核 | **6.489** | 1.595 | 0.256 | 0.260 | 3/3 |

`[DEPS] q8=fast c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast ⇒ deps_closure_ok=True`
（完整拆项见 `docs/PLAN_1second.md` §70.1）

## 5. 本基线的**已知缺口**（下一个基线之前必须补）

1. **板端运行点自记录机器没带进来**：T-14 的板端证据是用"停放版 `board_v39_verify.py`（含 runpoint/code_md5/_ONETIME）"跑出来的；
   HEAD 原生版没有这套（439 行 vs 644 行）。⇒ 新基线的板端 JSON **不会**再自记录 `code_md5`/`runpoint`。
   **计划 T-30**：按证据需求**重新实现**（不是把停放版搬回来），判据 = runpoint 键齐全 + 数值 sha1 不变。
2. `bin_engine.py` 里保留 6 行 `_e1p`（读 HEAD 原生 `_e1e2._prof`）：不带它则 T-14 §④ 的 `perf_gate` 永远是空列表 = 死代码。
3. 位流/`.hwh` 原本被 `.gitignore` 的 `pynq_specialized_code/board/` 整体挡住，本基线**强制纳入 git**（3.4 MB，不可重建，只有这一份）。

## 6. 回滚
```
git checkout baseline-v3        # 位流不在 git 里时：从本目录 MD5SUMS 校验磁盘上的 .bit
cd pynq_specialized_code/ps/pynq && md5sum -c ../../../docs/baseline_20261102_v39c/deps_closure.txt
```
