**⇒ 不可上板（按既有硬门槛 `N2 + IP_OTHER = 3,310 + 58 = 3,368 ≠ 0`）** —— 但**本轮成败判据 Z3 通过**：`pf_tag` 违例族 **0 条**（v42 是 118 条 / 最差 −4.719），且 G2 各组**无任何新增 `pf_*` 端点**。与 v42 的差异：**只改了 `PF_AHEAD` 一个参数（1→2）**，`PF_DEPTH/PF_MAX/AR_SLOTS` 全部回到 v39 的 `4/4/4`。

# v42c 构建存档（2026-11-02 08:58）

> 运行点：`xc7z015clg485-2`、Vivado 2025.2.1、**构建约束 10.000 ns（v39 配方逐字复刻）/ 审计口径 6.667 ns（150 MHz）**
> RTL：`worktrees/T-46-v42c` = **v39 的 `bin_e1e2.v`**（`bc28ead3…`）+ `bin_unit_axi_wrap.v`（HEAD + 仅 `PF_AHEAD=2`，含 T-34 的 0x250..0x25C 只读计数器）

## 一、Z 判据逐条

| 编号 | 判据 | 结果 |
|---|---|---|
| **Z1** | 综合/实现过；Latch=0 双查；DRC 0 Errors；`.hwh` = `c1686bd8…` | **PASS**：synth 7:24 / place 1:14 / route 1:10 / bitstream 0:21；`Register as Latch = 0`（报告口径）；DRC **0 Errors / 42 Warnings**；`.hwh` md5 `c1686bd8e080c1c7f46181d980543202` |
| **Z2** | G1 逐桶对 v39 不许变差 | **PASS**：N2 **−12,246**、N1 **±0**、N0 **−490**、IP_OTHER **−7**；TNS **+41,148.38**；WNS **+2.244**（−5.331 → −3.087） |
| **Z3** | 不许出现 `pf_tag`/`pf_*` 相关的新违例端点 | **PASS**：`pf_tag_reg` 家族 **0 条**；G2-E 3 条**全部继承自 v39**、新增 **0**；网表口径 `pf_tag_reg` 行号 = `{0 1 2 3}` ⇒ `PF_DEPTH=4` 落实（`pf_tag[7]` 这一行物理上不存在） |
| **Z4** | 资源与 v39 对比 | 见 §三：LUT 20,407（v39 20,552）、FF 18,997（v39 18,963）、BRAM 35（=v39）、DSP 101（=v39） |
| **Z5** | 归档自校验 | `md5sum -c MD5SUMS` 全过；bit↔源码 manifest **16/16 OK** |

## 二、交付物

| 文件 | 说明 |
|---|---|
| `design_1_150v42c.bit` | 位流，3,510,798 B，md5 **`eecc6e8042bfc844c0c162b0ba3532f9`** |
| `design_1_150v42c.hwh` | 硬件描述；md5 `c1686bd8…` **= v39/v40/v41/v42 同一份 ⇒ 没有动 BD** |
| `design_1_150v42c_routed.dcp` | routed checkpoint（审计对象），42,909,102 B，md5 `6136a7c9929feb2409907c4613b8807f` |
| `util_v42c.rpt` | 资源报告（`Register as Latch = 0`） |
| `timing_v42c.rpt` | 构建约束（10.000 ns）下的时序摘要：**WNS +0.246 / TNS 0.000 / 违例端点 0** |
| `endpoints_classified_v42c.csv` | **7,963** 条违例端点逐条分类（三桶 + reason） |
| `audit_v42c_summary_by_bucket.rpt` | 三桶汇总 + 按(桶\|引脚\|模块)聚合 + N0 逐 IP |
| `audit_v42c_G2_address_paths.rpt` | G2 A–E 五组（地址/突发/WE/预取 tag） |
| `audit_v42c_G2E_pf_family.rpt` | **G2-E 专项**：`pf_tag_reg` 行号普查 + 该族违例 + `ph_reg→pf_tag` 直查 |
| `audit_v42c_top20.rpt` / `audit_v42c_top10_gate.rpt` | 最差 20 / 门槛桶最差 10 |
| `audit_v42c_summary.rpt` / `audit_v42c_util.rpt` | 6.667 ns 口径的 timing summary / util |
| `G1_G2_analysis.rpt` | **v39/v42/v42c 三列逐桶 + G2 五组 + 各组"新增 vs 继承" + `pf_*` 按寄存器展开** |
| `build_v42c.log` | 构建全日志（10 分 56 秒） |
| `build_v42c_manifest.txt` | bit ↔ 源码 md5 清单（16 个文件，全 OK） |
| `build_final_150v42c.tcl` / `audit_v42c_endpoints.tcl` / `audit_v39_reaudit.tcl` / `g1_g2_v42c.py` | 构建 / 审计 / v39 重审 / 对比脚本（全部可复跑） |
| `endpoints_classified_v39_reaudit.csv` | **v39 的逐端点分类（本次重跑）**——G1/G2 里 v39 那一列的原始数据，放在这里才能就地复算 |
| `rtl/` | RTL + BD 网表快照（**取自 worktree**，与 manifest 对应；`rtl/bin_e1e2.v` md5 = `bc28ead3…` = v39 原文） |
| `launch_v42c.log` / `memwatch_v42c.log` | 闸门记录、内存采样 |
| `MD5SUMS` | 本目录全文件校验（相对路径，40 项） |

## 三、关键数字

### G1：逐桶（6.667 ns 口径，同一分类器，v39 的 CSV 是本次**重跑**得到）

| 桶 | v39 | v42 | **v42c** | Δ(v42c−v39) |
|---|---:|---:|---:|---:|
| N2 Fatal | 15,556 | 14,971 | **3,310** | **−12,246** |
| N1 Tolerable | 936 | 936 | 936 | 0 |
| N0 Inherited | 4,149 | 2,648 | **3,659** | −490 |
| IP_OTHER | 65 | 63 | **58** | −7 |
| 合计 | 20,706 | 18,618 | **7,963** | −12,743 |
| TNS | −47,312.40 | −44,558.47 | **−6,164.01** | **+41,148.38** |
| WNS | −5.331 | −4.790 | **−3.087** | **+2.244** |

### G2：地址/突发/WE/预取 tag（门槛 = 全 0）

| 组 | v39 | v42 | **v42c** | v39 最差 | v42 最差 | **v42c 最差** |
|---|---:|---:|---:|---:|---:|---:|
| A 组自增(desc/out/grp)/D | 0 | 0 | **0** | — | — | — |
| B AXI AR/AW/W 载荷/D | 0 | 1 | **0** | — | −0.171 | — |
| C 线缓存 win_q 写地址 | 254 | 249 | **102** | −1.605 | −2.665 | **−2.198** |
| D fill 槽表/D | 9 | 6 | **7** | −0.375 | −0.190 | **−0.241** |
| **E 预取 tag / `pf_*`** | 136 | 235 | **3** | −5.251 | −4.719 | **−1.110** |
| 合计（端点去重） | 399 | 491 | **112** | | | |

- **E 组 3 条** = `pf_busy_reg[0..2]/D`（起点 `ps7/SAXIHP0ACLK`，lv=9），**v39 里同样这 3 条就在违例**（最差 −1.340）⇒ 新增 0。
- **C 组**：条数 254→102（−60%），但**最差从 −1.605 恶化到 −2.198**，且其中 **45 条是新增端点**（`win_q_reg_*/ADDRARDADDR[*]`，起点仍是 `ps7/SAXIHP0ACLK`）。这是本版**唯一**相对 v39 变差的单项，报告里不掩盖。
- **A 组（E1E2 组自增）在本版已归零**：`bin_e1e2.v` 回到 v39 原文（`bc28ead3…`），那三个加法器不存在 ⇒ 0 条端点，机械证据成立。

### 同一份 P&R 在**构建约束（10.000 ns）**下的干净程度（不改约束，只是把日志里的自报数并列）

| | v42 | **v42c** |
|---|---:|---:|
| 最终 WNS @10.000 | **−1.457** | **+0.246** |
| 违例端点数 | **≥2,000（取满上限）** | **0** |
| TNS @10.000 | 远负 | **0.000** |
| 最差路径 | lv=18，`desc_q_reg[3]` 出发（E1E2 组自增那条锥） | lv=8，`araddr_l_reg[4]/C → axi_ctrl/xbar/...` |

⇒ 摘掉 `pf_tag[7]` 那一行之后，**连 10 ns 约束下的 P&R 也从"大量违例"变成"全清"**。
（这不是"放宽约束"：约束位一个没动，6.667 的真相见 §三 G1 表。）

### 资源 / 开销

| 项 | v39 | v42 | **v42c** |
|---|---:|---:|---:|
| Slice LUTs | 20,552 | 19,503 | **20,407** |
| Slice Registers | 18,963 | 19,326 | **18,997** |
| Block RAM Tile | 35 | 31 | **35** |
| DSPs | 101 | 101 | **101** |
| Register as Latch | 0 | 0 | **0** |

- v39 的 LUT/FF/BRAM 取自 `runs/v42_archive/README.md` 的记账（v39 的 `util_v39.rpt` 未随构件归档）。
- 构建 **10 分 56 秒**（08:47:45 → 08:58:41；v42 是 12:12）：synth 7:24、opt 0:07、place 1:14、route 1:10、bitstream 0:21。
- Vivado 内部峰值 **4,148.3 MB**（v42 4,187.7 MB）。⚠ 本版 OS 侧 `memwatch` 因启动早于 vivado 而**空跑退出**（`memwatch_v42c.log` 只有一行"已退出"），所以这里只有 Vivado 自报峰值，没有独立采样。

## 四、怎么用

```bash
cd /media/felix/Data/coding/demoir-fpga/runs/v42c_archive && md5sum -c MD5SUMS
cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code && \
    md5sum -c board/overlay_engine_150mhz/build_v42c_manifest.txt   # 16/16 OK
```

## 五、⚠ 使用前必读

1. 本存档**只覆盖 PL 侧证据**。金丝雀、三态 memtest、与 v39 的 sha1 A/B **都没做**（任务禁止上板）。
2. `N2 = 3,310 ≠ 0`，按项目既有硬门槛仍是**不可上板**；但按 2026-11-02 改判后的口径（真正的失效模式只有"越界写"，看 G2），v42c 在 **A/B/D/E 四组上都 ≤ v39**，**只有 C 组最差路径比 v39 差 0.59 ns**（条数仍少 60%）。
3. `audit_v42c_G2E_pf_family.rpt` 的 ③（`ph_reg -> pf_tag_reg` 直查）返回了**空值**（不是 `NO_PATH`，也不是数字）⇒ 该行**不可当证据**，结论只依赖 ①（`pf_tag_reg` 行号 = {0,1,2,3}）与 ②（该族违例 0 条）。
4. 若上板，建议按梯度：先 64² ×1 看 sha1 是否回到冻结三件套，再 64² ×N（**v42 在这一步才暴露非确定性**：24 跑 24 种 sha1）。
5. G2 的分组模式是人工按 RTL 行号挑的，**不是穷举**；"未命中"不等于"安全"。
6. **v42b 已作废**（父代理 08:46 改判：锅在读通路预取，不在 E1E2 组自增）；它只跑到 `synth_design`，无位流，记录见 `runs/timing_v42b/NOTE_v42b_ABANDONED.md`。
7. `pf_addr_q` / `pf_plane_addr_q` 两组端点在 `conv_uni_0` 内部（该 IP 在 bin 整网里不被调用），它们的条数变化与本轮预取改动**无关**，不要误读。
