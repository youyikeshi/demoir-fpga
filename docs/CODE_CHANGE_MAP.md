# 到 1 秒：每一步要改哪些代码（2026-11-02 15:00）

> 逐里程碑列出**文件 + 函数/模块 + 改什么 + 现状（已有/要写）**。
> 代码规模：RTL 25 个模块（真源 `runs/v42e_archive/rtl_v39_exact/`）；PS `bin_net.py` 2410 行、`bin_engine.py` 4698 行。

---

## M1 `convfp → PL`（−0.34 s）

| 层 | 文件 | 改什么 | 现状 |
|---|---|---|---|
| RTL | `_worktrees/fconvint/rtl/fconv_core.v` + `fconv_lane.v` + `fconv_{w,l,t}load.v` + `fconv_unpack.v` + `fconv_stream_wrap.v` + `fconv_axi_wrap.v` + `fconv_axi_wrap_bd.v` + `fp32_add_rne.v`（9 个）| 无（已验逐字节）| **已存在** |
| BD | `pl/scripts/bd_add_fconv_v44.tcl` | 加 `fconv_0`（`0x40080000`）+ **启用 `ps7/S_AXI_HP2`** + 4 项修正 | **写了一半** |
| BD | `pl/scripts/build_final_150v44.tcl` | 编译列表加 9 个 fconv RTL + `read_verilog xlconstant` 库 | **本次在做** |
| PS 驱动 | `ps/pynq/fconv.py`（`FconvEngine`、`_Guard`、`pack_weights`）| 无（寄存器映射已一一对应）| **已存在** |
| PS 接线 | **`ps/pynq/bin_net.py:1522 _fp_conv`** | 加 `BIN_CONVPF_PL` 分支：调 `FconvEngine` 代替 `conv2d_f32`；处理 in/out 缓冲与 arena、失败静默回落 CPU | **要写（~80 行）** |
| PS | `ps/pynq/bin_engine.py` | 暴露 arena 物理地址给 fconv（复用现有 `_arena`/`prepare_segments` 的 buf 描述）| 小改 |
| 判据 | — | 逐层 `np.array_equal(y_pl.view(np.uint32), conv2d_f32(...).view(np.uint32))`（K 串行 golden，**不能用主机 numpy**）| — |

$$\Rightarrow\ \text{估算：BD 脚本 1 天（已在做）；PS 接线 0.5 天；上板验证 0.5 天}$$

---

## M2 `pack 698 → 227 条命令`（−0.20~0.37 s）

| 层 | 文件 | 改什么 | 现状 |
|---|---|---|---|
| RTL | **`bin_e1e2.v`** | 组自增循环（`E_DESC += 64` 等差、`NGRP` 计数器、`E_GROUP`）| **T-36 已写（+276/−14 行），但从未单独验过** |
| RTL | **`bin_unit_axi_wrap.v`** | 新增寄存器 `NGRP`（组数）+ 完成条件改为"组循环结束" | 部分在 T-36 版里 |
| PS | **`bin_engine.py:2753 prepare_segments`** | 每层只发**1 条**命令：设 `desc_pa/out_pa/group_base + NGRP=cb/groups`，不再循环发 `cb` 条 | **要写（~120 行）** |
| PS | **`bin_engine.py:2435 run_one`** | 支持"一次启动跑多层组"：完成判据从"每命令 done"变成"整层 done"，`kick` 的超时按层算 | **要写（~60 行）** |
| PS | `bin_engine.py:_pack_views` | 生成组基址表（现有 `desc` 等差可由 RTL 自增，省掉 PS 端展开）| 小改 |
| 前置 | — | 先出 `v42f = v39 + 只加 e1e2 组自增`（单变量）验 sha1 | **要做** |

$$\Rightarrow\ \text{估算：RTL 收口 0.5 天（代码已有，要审 + 补 NGRP 语义）；PS 0.5 天；位流+上板 0.5 天}$$
$$\Rightarrow\ \text{⚠ 风险：改了"一条命令内部的地址推进"}\Rightarrow \text{PS 若还按老契约发 } cb\ \text{条就会重复推进（v42 的失败模式之一）}$$

---

## M3 `prologue 1.14 → ~0.55`（−0.59 s）

| 层 | 文件 | 改什么 | 现状 |
|---|---|---|---|
| RTL | **`conv_uni_axi_wrap.v`** + `conv_k1/k3/k5_engine.v` | conv 输出**写侧直接产 E1E2 布局**（不用先写 f32 再读回量化）| **要写（+~150 LUT；64² 需 +8 RAMB36、128² 需 +32）** |
| RTL | `bin_e1e2.v` | 接受"已是 E1E2 布局"的输入（跳过重排）| 中改 |
| RTL | 新增小模块（量化）| int8 量化（`scale`/`zero_point`）搬进 PL 写路径 | **要写** |
| PS | **`bin_net.py` 的 q8 路径**（`q8.py`、`bpack.py` 调用点）| `q8` 量化（0.604 s）整段删掉，改为"PL 已产 int8" | 中改 |
| PS | `bin_net.py:_analyze_lazy` / `_needs_f32` | 物化决策要跟着变（不再需要 f32 中间张量）| 中改（**最容易出字节差**）|
| PS | `bin_engine.py:read_out` | 读回路径改成读 int8 布局 | 中改 |

$$\Rightarrow\ \text{估算：RTL 2 天（含 3 种 k 的写侧改造）；PS 1 天；验证 1 天}$$
$$\Rightarrow\ \text{⚠ 这是"字节账"最容易被破坏的一步，}\textbf{每一层都要与冻结 sha1 对齐}$$

---

## M4 `conv 残差 0.74 s`（机制未证）

| 层 | 文件 | 改什么 | 现状 |
|---|---|---|---|
| 观测 | `bin_unit_axi_wrap.v` | **安全**的只读计数器（`KEEP_DUPLICATE` 复制信号，或 ILA）| v42d 证明直接监听会改时序 |
| TB | `pl/rtl_hand/tb/`（新）| **乱序/背压 AXI 响应**的 memory model（现有 TB 是固定延迟）| **要写** |
| RTL | `bin_unit_axi_wrap.v` 的 linecache/prefetch | 按 TB 暴露的真因改（预取簿记 or 别的）| **需要先测才知道** |

$$\Rightarrow\ \text{估算：观测+TB 1.5 天（不含优化）；优化 1\~3 天，视机制}$$
$$\Rightarrow\ \text{⚠ 这一项}\textbf{不能承诺}，\text{先做完观测量再判断值不值}$$

---

## M5 `全融合`（−0.8~1.2 s）——1 秒的必经之路

| 层 | 文件 | 改什么 | 现状 |
|---|---|---|---|
| RTL | **新模块** `bin_desc_chain.v`（或扩 `bin_unit_axi_wrap.v`）| **描述符链**：一次启动跑完整层列表；含"出错即停/进度回读/mux 复位"三件语义 | **要写** |
| RTL | `bin_unit_axi_wrap.v` | 描述符常驻存储（BRAM）+ 自动取下一个描述符 + 并发 kick 的互斥 | **要写** |
| RTL | `bin_conv_core8.v` / `conv_uni_axi_wrap.v` | conv 与 pack **数据直通**（不落 DDR 再读）| **要写** |
| RTL | `bin_sam.v` / `bin_interp.v` / `bin_rprelu.v` / `bin_catpack.v` | 小算子挂到读写窗口上（现在它们是独立单元或 PS 调用）| 中改 |
| PS | **`bin_net.py` 的调度器**（`_exec_ps`、`build_graph`、`_analyze_lazy`）| 从"逐 op 驱动 MMIO"变成"提交描述符链 + 轮询一次进度" | **大改（~600 行）** |
| PS | **`bin_engine.py`** | `run_one`/`prepare_segments`/`_wr`/`_rd` 大半个要重写；`kick` 改成"提交并等待整链" | **大改（~800 行）** |
| PS | `precomp`/`qcache`/`lazy_deq` | 语义跟着变（帧间复用、缓存键、按需物化）| 中改 |

$$\Rightarrow\ \text{估算：RTL 5\~8 天；PS 3\~5 天；验证 2 天}$$
$$\Rightarrow\ \text{⚠ 这已经不是"优化"，是}\textbf{重写执行层}；\text{而且需要}\textbf{新的主口}（HP3）$$

---

## M6 `算力/时钟`（−0.5~0.9 s）

| 层 | 文件 | 改什么 | 现状 |
|---|---|---|---|
| RTL | **`bin_conv_core8.v`**（→ `bin_conv_core16.v`）| 8 lane → 16 lane，`VLANES`/`LANES` 参数化 + 累加器树加宽 | 改（+~8k LUT）|
| RTL | `bin_unit_axi_wrap.v` | `LANES` 参数、读口带宽（每拍要 2× 字节）| 改 |
| RTL | 全线 | **200 MHz 收时序**：流水线级数、`max_fanout`、跨域 | 大改（分布在整个设计）|
| PS | 无 | （吞吐提高后 `pack` 的相对占比会上升，要重新平衡）| — |
| BD | — | 提高 `FCLK_CLK0` 到 200 MHz（运行时 `Clocks.fclk0_mhz` 即可，**但必须先证明 200 MHz 能收敛**）| — |

$$\Rightarrow\ \text{估算：16 lane 2\~3 天；200 MHz 收时序 3\~5 天（这是最后的大战役）}$$

---

## 汇总：工作量与依赖

| 里程碑 | RTL 工作量 | PS 工作量 | 依赖 | 可并行 |
|---|---|---|---|---|
| M1 | 小（已存在）| 小（`_fp_conv` 80 行）| BD/HP2 | — |
| M2 | 中（已有代码要审）| 中（180 行）| M1 不冲突 | ✅ 与 M1 并行 |
| M3 | **大**（2 天）| 中 | M2（共用 e1e2 布局）| 与 M1/M2 部分并行 |
| M4 | 中（TB 为主）| 无 | 独立 | ✅ 可全程并行 |
| M5 | **很大** | **很大** | M2+M3 的布局语义 | M6 的 16 lane 可并行 |
| M6 | **大** | 无 | M5 定型后 | — |

$$\Rightarrow\ \textbf{关键路径}：\text{M1} \to \text{M3} \to \text{M5} \to \text{M6（200 MHz）}$$
$$\Rightarrow\ \textbf{可并行的支线}：\text{M2（与 M1 并行）}、\text{M4（独立）}、\text{M6 的 16 lane（与 M5 并行）}$$
$$\Rightarrow\ \text{按 2\sim3 个子代理并行，全部做完约 }\mathbf{3\sim4\ 周}；\text{到 } 2\sim3\ \text{s 约 }\mathbf{1.5\sim2\ 周}$$
