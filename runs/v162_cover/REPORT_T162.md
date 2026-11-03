# T-162：`TNS Total Endpoints` 47,805 → 41,273（−6,532）的归因（2026-11-03）

**口径**：只读。两个 routed DCP，同一台 Vivado 2025.2.1，**同一条脚本** `enum_ep.tcl`，
DCP 内建约束不动（两版都是 `create_clock -period 10.000`）。不上板、不改 RTL/网表。
产物目录 `runs/v162_cover/`；探针 `/tmp/probe_t162_cover/`。全程走 `memgate.sh`，开跑前 `pgrep -c vivado = 0`。

---

## 0. 一句话结论

$$\Rightarrow\ \textbf{不是口径差异，是真少了}；\text{少的不是逻辑，是}\textbf{被分析的端点}$$
$$\Rightarrow\ \text{那 }6{,}532\ \text{个端点} = \text{v39 里}\textbf{唯一被分析的到达路径全部起自 }\mathtt{run\_len}/\mathtt{u\_ce}\ \text{准静态使能锥}\text{的那一批}$$
$$\quad\ \text{（}\mathtt{run\_len\_reg[3]\_replica}\ 3{,}178+\mathtt{run\_unit\_q\_reg[2]}\ 2{,}549 = 87.4\%\text{）}$$
$$\Rightarrow\ \text{刀 7 把这整条锥删掉 ⇒ 它们只剩"本来就不被分析"的那一族路径 ⇒ 从时序图里消失（引脚仍在网表里）}$$

---

## 1. 先排除口径（最便宜的一步）

| 检查 | v39 | v57 | 判定 |
|---|---|---|---|
| 约束周期（DCP 内建） | `create_clock -period 10.000` | 同 | **一致** |
| 出报告的脚本 | `report_timing_summary -max_paths 20` | `-max_paths 5` | `-max_paths` 只影响明细条数，**不影响 Total Endpoints** |
| 同一脚本复跑 | `47805` | `41273` | **复现** |
| `check_timing` 12 类 | 全 0 | 全 0 | 一致（无未约束/无时钟端点） |
| 例外覆盖（`report_exceptions -coverage`） | FP10 24,492；FP11 13,742；FP12 4,838 | FP10 24,489；FP11 13,740；FP12 4,840 | **几乎相同（−3）** ⇒ 不是 false_path 覆盖面变了 |
| 复位/常量/多周期/disable | 0 | 0 | 一致 |

⇒ **口径完全相同，6532 是真差异。** 另：v39 的 47,805 与 6.667 ns 那次审计（47,805）也一致，周期不影响端点数。

## 2. 同口径端点集合枚举（决定性证据）

`get_timing_paths -max_paths 60000 -nworst 1 -delay_type max -slack_lesser_than 1000`（未截断）：

| | 枚举条数 | 汇总表 TNS Total Endpoints |
|---|---:|---:|
| v39 | **47,805** | 47,805 ✔ |
| v57 | **41,273** | 41,273 ✔ |

每个端点唯一，一网打尽。差集：**only_in_v39 = 6,551，only_in_v57 = 19**（净 −6,532）。
那 19 个新增全是刀 7 的新逻辑：`u_pack/rem_reg[0..8]/{D,CE}` + `sw_act_q_reg/D`。

### 2.1 分类表（`pin_class × 模块 × 数量`，见 `lost_endpoints_table.csv`）

| # | pin_class | 模块 | 数量 |
|---|---|---|---:|
| 1 | CE | `u_bin_unit` 顶层 | 2,262 |
| 2 | CE | `u_eng` | 2,232 |
| 3 | D | `u_bin_unit` 顶层 | 1,608 |
| 4 | **ADDRBWRADDR**（BRAM 端口B地址） | `u_bin_unit` 顶层 | 208 |
| 5 | CE | `u_pack` | 146 |
| 6 | ENBWREN | 顶层 16 / `u_pack` 9 | 25 |
| 7 | CEP（DSP P 寄存器使能） | `u_eng` | 16 |
| 8 | WEA | `u_pack` | 16 |
| 9 | ENARDEN / CEA / CEB | `u_eng` | 8 / 8 / 8 |
| 10 | CE | 其它 | 1 |
| | | **合计** | **6,551** |

按类汇总：**CE 4,641（70.8%）、D 1,620（24.7%）、BRAM 控制脚 258、DSP 使能 32**。

### 2.2 这些端点在 v39 里的最差起点（= 它们为什么被分析）

| 起点寄存器 | 条数 |
|---|---:|
| `u_pack/run_len_reg[3]_replica` | 3,178 |
| `u_bin_unit/run_unit_q_reg[2]` | 2,549 |
| `u_bin_unit/p_addr_reg[2]` | 373 |
| `u_bin_unit/FSM_onehot_wst_reg[1]` | 251 |
| `u_bin_unit/pw_word_reg[13]` | 69 |

⇒ **前两名（87.4%）就是 STAGE_A 判定的那条准静态使能锥（`run_len` → `u_ce` → 千级 CE/WEBWE/ADDR）**，也正是刀 7 删掉的那条锥（指纹 `issue_en0_carry` 15→0、`issue_en` 网 15→0）。

### 2.3 它们在 v57 里的状态：引脚还在，但**没有 slack**

- 6,551 个消失端点里，**6,526 个引脚在 v57 网表里照样存在**（25 个是 v39 的 `_replica` 复制体被合并，属正常）。
- 对它们做 `get_timing_paths -slack_lesser_than 1000` ⇒ **返回 0 条**；而定向查询 `get_timing_paths -to <pin>` 返回 1 条 **`SLACK = <>`（空）** 的路径：
  - `u_bin_unit/FSM_onehot_wst_reg[0]/D`：起点 `win_q_reg_2/CLKBWRCLK`，**21 级 / 21.445 ns**，slack 空
  - `u_eng/p_0_out__0/CEA2`：起点 `win_q_reg_2/CLKBWRCLK`，**26.824 ns**，slack 空
  - 两个时钟属性都仍是 `FCLK_CLK0`，`check_timing` 仍判 0 未约束端点 ⇒ 属于**被例外吃掉**的那一族，不是"未约束"。
- **关键自校验（证明这一族在 v39 里本来就没被分析）**：v39 同一条 `win_q → FSM_onehot_wst_reg[0]/D` 路径是 21 级；若它在 v39 里被分析，slack 应 ≈ 10−21.4 < 0 ≈ **−11.4 ns**，v39 的 WNS 就该是 −11.4 而不是 −1.998。
  ⇒ 这一族（穿 catpack、被 FP10 `-through` 覆盖的宽锥）**在 v39 就已经是 untimed**；
  v39 里这些端点**唯一**被分析的到达路径就是 `run_len`/`u_ce` 锥。
- ⇒ 刀 7 删掉那条锥后，这些端点**没有任何一条被分析的路径**，于是从 `TNS Total Endpoints` 里消失。
  **不是因为更快了而"满足"，而是因为"不再检查"。**

## 3. 对"v39 违例都是准静态 CE"这个结论的影响

v39（10 ns 口径）**7,109** 个违例端点与这 6,551 个消失端点求交：

| 量 | 数 | 占比 |
|---|---:|---:|
| 违例端点中 **v57 不再被分析** | **2,841** | **40.0%**（其中 CE 2,697、D 62、BRAM 58、DSP 32…） |
| 违例端点中 **v57 仍被分析且已满足** | **4,268** | 60.0%（最差 slack +0.206，路径 `win_q→s1_pc_q`） |

⇒ **"刀 7 消掉 7,109 条违例"要改写成：4,268 条真的被修好并满足；2,841 条（40%，几乎全是 CE）是分析对象消失。**
⇒ **v39 的 CE 结论本身不动**：违例确实集中在那条准静态使能锥上（起点 87.4% 是 `run_len_reg[3]_replica`/`run_unit_q_reg[2]`）。
⇒ 但 v57 的"0 违例 / WNS +0.206"是**在一个少了 13.7% 端点的分析集上**得到的，其中 40% 的原违例端点不再进入检查。这一点应写进 v57 的验收记录。

## 4. 能否解释 v57 在 100 MHz 上板失败

**不能。** 理由：
1. 消失的是 **CE 4,641 + BRAM 控制 258 + DSP 使能 32 = 4,931** 个**准静态使能类**端点 —— 正是 v39 已用 54/54 sha1、30+ 次跑证明"150 MHz 下也不致命"的那一类；
2. 差异的方向是**报告更乐观**（少检查 13.7%），它不会让硬件算出错数据；
3. 消失集合与 v57 的真实绑定路径（`win_q → s1_pc_q`，slack +0.206）**不相交**；
4. v57 板端失败需要一个"STA 看不见/不可信"的机制（T-160 §4 已论证），本次发现的"端点被漏检"只是把**同一结论的范围放大**：不止 catpack 那 59.3% 的洞，**还有这 6,526 个端点整条不在分析里**。

## 5. 建议（最小动作）

1. **v57 的验收记录必须改口径**：写"41,273 端点（比 v39 少 6,526，其中 2,841 个原违例端点不再被分析）"，不许写"0 违例"。
2. **刀 7 的收益应报 −1.998 → +0.206（+2.204 ns）但注明 40% 的原违例是"退出分析"而非"被修好"**；真被修好的 4,268 条证据仍在，刀 7 的结论不倒。
3. **要真正验证那 6,526 个端点**：只能去掉/收窄 catpack 三件套后重跑（本机无 `reset_path`，须新建一份不带 FP10/11/12 的约束从 routed DCP 重跑；或用 8.000 ns 重建送 P&R），否则这份 STA 对它们的覆盖是 0。

## 6. 产物清单

| 文件 | 内容 |
|---|---|
| `v39_endpoints.csv` / `v57_endpoints.csv` | 逐端点全枚举（47,805 / 41,273，含 slack+起点） |
| `lost_endpoints_table.csv` | `pin_class × 模块 × 数量`（6,551） |
| `v39_summ.rpt` / `v57_summ.rpt` / `*_util.rpt` | 同脚本汇总与资源对照 |
| `v39_checktiming.rpt` / `v57_checktiming.rpt` | 12 类全 0 的原始证据 |
| `v39_refcount.csv` / `v57_refcount.csv` | 按 REF_NAME 的 cell 普查（FF 18,963→18,961；DSP 101；RAMB36 31） |
| `/tmp/probe_t162_cover/*.tcl,*.log` | 探针脚本与日志（`@D@`/`@P3@`/`@P4@` 行） |
