# 文件清单与来源

| 路径 | 来自 | 说明 |
|---|---|---|
| `03_BITSTREAM/design_1_150v57.bit` | `runs/v57_archive/` | md5 `5d1cb789`，3,510,798 B |
| `03_BITSTREAM/design_1_150v57.hwh` | `runs/v57_archive/` | md5 `c1686bd8`，**与 v39 同一份**（BD 未重建）|
| `01_TIMING/timing_v57.rpt` | `runs/v57_archive/` | 10 ns 口径报告 |
| `01_TIMING/util_v57.rpt` | `runs/v57_archive/` | 资源利用率 |
| `01_TIMING/families_v39_at10ns.csv` | `runs/v152_timing/` | v39 在 10 ns 的真值分族表（作为对照）|
| `04_RTL/rtl_v57/` | `runs/v57_archive/rtl/` | 12 个构建输入快照；`bin_e1e2.v` = `bc28ead3` |
| `04_RTL/diff_v39_to_v57_bin_e1e2.diff` | `runs/v57_archive/` | **唯一的 RTL 差异** |
| `04_RTL/build_v57_manifest.txt` | `runs/v57_archive/` | 18 条 `md5sum -c` 全过 |
| `02_FUNCTION/eqv_v39/` | `runs/v152_timing/eqv/` | v39 的 12 shape 仿真日志与 golden |
| `02_FUNCTION/eqv_v152/` | `runs/v152_timing/eqv/` | v57 的 12 shape 仿真日志与 golden |
| `02_FUNCTION/wrap12_v39.log` / `wrap12_v152.log` | `runs/v152_timing/eqv/` | 两树的 12 shape 对照日志（`diff` 为空）|
| `05_PS/bin_net.py` | 提交 `58abc5b` | md5 `0990948d`（T-153）|
| `05_PS/bin_engine.py` | 提交 `58abc5b` | md5 `2fd3d099`（T-156）|
| `05_PS/DIFF_*.patch` | `runs/board_t153/`、`runs/board_t156/` | 两份改动的独立 diff |
| `06_EVIDENCE/` | `runs/v57_archive/evidence/`、`runs/v152_timing/` | 构建日志、审计输出、模块级 A/B |

## 不在包内但可以复算的

| 东西 | 在哪 |
|---|---|
| v39 位流 | `pynq_specialized_code/board/overlay_engine_150mhz/design_1_150v39.bit`（md5 `602725b8`）|
| 主线 RTL | `runs/timing_v50/rtl_v50/`（`verify_rtl_root.sh` MATCH=35）|
| 板端性能数据 | `runs/board_t145/`（LAZY=1 浸泡）、`runs/board_t153/`、`runs/board_t156/` |
| 融合线评估 | `runs/v148_cap/`（按字节算 ROI 0.8%）、待补按命令次数算 |

## 校验方式

```bash
cd runs/ACCEPT_v57 && md5sum -c MD5SUMS
```
