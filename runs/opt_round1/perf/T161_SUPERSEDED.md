# ⛔ T-161 旧方案已作废（SUPERSEDED）—— 2026-11-04

本目录下这四份 **T-161 金丝雀修复**产物**已作废，不要合并、不要照抄上板**：

| 文件 | 状态 | 原因 |
|---|---|---|
| `T161_canary.patch` | **作废** | 单窗实现（只把金丝雀移到 `out_bytes`），不满足验收判据 U3 |
| `T161_bin_engine.py` | **作废** | 同上；且回落路径 `check_canary` 抛 `ValueError`、**不打印"回落"**，不满足 U4 |
| `T161_bin_net.py` | **作废** | 仅 `fill_canary(outb, L.out_bytes)`；数值上没问题，但与之配套的 engine 作废 |
| `test_canary_t161.py` | **作废** | 它的"U3 对照"是用复刻的老逻辑单独做的，**没有验证新实现抓 `[len-8,len)` 的能力** |

（`T161_canary_result.md` 只是上一版的自述，同样只作历史记录，**不是合并依据**。）

## 为什么作废

1. **U2 与 U3 相距 1.5 MB，不可能落在同一个 4096 B 窗里**：
   `out_bytes=24576`、`len(slot)=1 576 960` 时，
   `[out_bytes, out_bytes+8)` 与 `[len-8, len)` 相距 1 552 376 B。
   旧方案只有"紧贴 `out_bytes`"一个窗 ⇒ **槽尾写者（desc/scratch/别的槽）在新实现下不可观测**，
   而这是旧行为**唯一**能抓到的一类写者。
2. 旧方案的回落路径在 `out_bytes+4096 > len` 时抛 `ValueError` 且无日志，
   与本任务书 U4（**不崩 + 日志含"回落" + 与旧路径等价**）不符。

## 正确版本（唯一合并候选）

* **`worktrees/T-161-canary/`**
  * `T161-final.patch` —— 以生产 `pynq_specialized_code/ps/pynq/bin_engine.py`
    (`f3fca8a6…`) / `bin_net.py` (`c95d8154…`) 为基准；`git apply --check` **exit 0**；
    patch md5 `06aa52945bf06fd99a353c61bb2c9eac`
  * `REPORT.md` —— 含 U1~U4、变异 M1/M2、端到端等价、**板端上板验证步骤与判读表**
  * `pynq_specialized_code/ps/pynq/{bin_engine,bin_net}.py` —— 修复后整文件
    （md5 `8f6717fe…` / `2c104221…`）

## 双窗设计（一句话）

紧贴窗 `[out_bytes, out_bytes+4096)` 抓"贴着本层数据边界"的越界（首偏移 0）；
槽尾窗 `[len-4096, len)` 抓"越过整个槽"的写者（首偏移 4088）；放不下时回落槽尾并打日志。

**结论：本目录的 T-161 产物仅作历史证据保留，请勿合并、请勿上板。**
