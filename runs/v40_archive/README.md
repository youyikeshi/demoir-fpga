# v40 构建存档（2026-10-31 20:44）

## 内容
| 文件 | 说明 |
|---|---|
| `design_1_150v40.bit` | 比特流 |
| `design_1_150v40_routed.dcp` | 布线后 checkpoint（可离线复算时序/资源，不必重建） |
| `util_v40.rpt` | 资源（**Latch 必须 0**） |
| `timing_v40.rpt` | 时序（WNS/TNS/WHS/端点） |
| `build_v40_manifest.txt` | **bit ↔ 源码 md5 对齐清单**（纪律第 42 条，上板前必须 `md5sum -c` 全过） |
| `design_1_150v40.hwh` | PYNQ 地址映射（BD 自 v2 未变，与 v33/v39 同内容） |
| `MD5SUMS` | 本目录全部文件的校验和 |

## 上板前
```
cd pynq_specialized_code/pl && md5sum -c ../../runs/v40_archive/build_v40_manifest.txt
```
不一致 ⇒ **不许上板**（验的是一个不存在的设计）。

## 为什么要单独归档
2026-10-31 v40 第一次构建在**报告阶段**报错中止（`get_timing_paths -min_pulse_width` 非法选项），
而 `write_checkpoint`/`write_bitstream` 排在其后 ⇒ **已布线的整个实现被丢掉，白烧 22 分钟**。
修法：① `write_checkpoint` 提前到所有报告之前；② 每个报告项包 `catch`；
③ 产物一落盘就用本脚本归档（纪律第 51 条）。
