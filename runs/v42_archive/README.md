**⇒ 不可上板（按既定门槛）** —— 旧口径 `N2+IP_OTHER = 14,971 + 63 = 15,034 ≠ 0`；新口径 G2（地址/写使能通路）**256 条违例 ≠ 0**。但 v42 在 G1 上逐桶**全面优于 v39**（N2 −585、N0 −1,501、TNS +2,754、WNS +0.541），唯一恶化点与修法见 `REPORT_v42.md` §4.1 与 §9。

# v42 构建存档（2026-11-02 08:19）

> 运行点：`xc7z015clg485-2`、Vivado 2025.2.1、**构建约束 10.000 ns（复刻 v39 配方）/ 审计口径 6.667 ns（150 MHz）**
> RTL：`git HEAD = 7b1069f`（T-38 的 `bin_e1e2.v` + `bin_unit_axi_wrap.v`）

## 交付物

| 文件 | 说明 |
|---|---|
| `design_1_150v42.bit` | 位流，3,510,798 B，md5 **`f1b5dd2af4a1f37dd4b6c08c9ef18f1b`** |
| `design_1_150v42.hwh` | 硬件描述；md5 `c1686bd8…` **= v39/v40/v41 同一份 ⇒ 没有动 BD** |
| `design_1_150v42_routed.dcp` | routed checkpoint（审计对象），37,514,349 B |
| `util_v42.rpt` | 资源报告（`Register as Latch = 0`） |
| `timing_v42.rpt` | 构建约束（10.000 ns）下的时序摘要 |
| `endpoints_classified_v42.csv` | **18,618** 条违例端点逐条分类（三桶 + reason） |
| `audit_v42_summary_by_bucket.rpt` | 三桶汇总 + 按(桶\|引脚\|模块)聚合 + N0 逐 IP |
| `audit_v42_G2_address_paths.rpt` | **G2 专项：地址/突发/写使能通路**（真安全判据） |
| `audit_v42_top20.rpt` / `audit_v42_top10_gate.rpt` | 最差 20 / 门槛桶最差 10 |
| `audit_v42_summary.rpt` / `audit_v42_util.rpt` | 6.667 ns 口径的 timing summary / util |
| `G1_G2_analysis.rpt` | **v39 vs v42 逐桶对比 + 新增端点 + G2 双列对比**（可复算） |
| `build_v42.log` | 构建全日志（12 分 12 秒） |
| `build_v42_manifest.txt` | bit ↔ 源码 md5 清单（16 个文件） |
| `build_final_150v42.tcl` / `audit_v42_endpoints.tcl` | 构建脚本 / 审计脚本（可复跑） |
| `rtl/` | RTL + BD 网表快照（与 manifest 对应） |
| `launch_v42.log` / `watch_v42.log` / `memwatch_v42.log` | 闸门、内存采样记录 |
| `MD5SUMS` | 本目录全文件校验（相对路径） |

## 校验（三条都跑过）

```bash
cd /media/felix/Data/coding/demoir-fpga/runs/v42_archive && md5sum -c MD5SUMS
# 基准必须是 pynq_specialized_code/：清单里 15 条是绝对路径，第 16 条（构建脚本自身）
# 来自 info script，记成了 pl/scripts/build_final_150v42.tcl
cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code && \
    md5sum -c board/overlay_engine_150mhz/build_v42_manifest.txt        # 16/16 OK
```

## 关键数字（详见 `REPORT_v42.md`）

| 项 | v39 | **v42** | Δ |
|---|---:|---:|---:|
| WNS @6.667 | −5.331 | **−4.790** | **+0.541** |
| TNS @6.667 | −47,312 | **−44,558** | **+2,754** |
| 违例端点 | 20,706 | **18,618** | −2,088 |
| N2 Fatal | 15,556 | **14,971** | −585 |
| N1 Tolerable | 936 | 936 | 0 |
| N0 Inherited | 4,149 | **2,648** | −1,501 |
| IP_OTHER | 65 | 63 | −2 |
| **G2 地址/写使能违例** | 263 | **256** | −7（**但我方起点最差 −1.605→−1.990**） |
| Slice LUTs | 20,552 | **19,503** | **−1,049** |
| Slice Registers | 18,963 | 19,326 | +363 |
| Block RAM Tile | 35 | 31 | −4 |
| DSPs | 101 | 101 | 0 |
| Register as Latch | 0 | **0** | 0 |

## 构建开销

- 总 **12 分 12 秒**（v41 同配方 18 分 31 秒）；综合 **7:29**（v41 9:05，**更快**）、布线 5:03（v41 2:38，更慢）
- Vivado 内部峰值 **4,187.7 MB**（OS 侧 7 进程 RSS 之和 6,464 MB，含共享库重复计）
- DRC **0 Errors / 21 Warnings**；综合/终检 DSP 均 = **101**

## ⚠ 使用前必读

1. 本存档**只覆盖 PL 侧证据**。金丝雀、三态 memtest、与 v39 的 sha1 A/B **都没做**（板断电、任务禁止上板）。
2. G2 的 4 组模式是人工按 RTL 行号挑的，**不是穷举**；"未命中"不等于"安全"。
3. `audit_v42.log` 里 3 条观测路径是 `NO_PATH`，**不可当作"路径已被切干净"的证据**（也可能是模式没匹配到）。
