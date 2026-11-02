# v50 目标架构（整体规格）— 2026-11-02 15:20

> 原则：**架构变动从整体考虑，一次性写全所有必须修改的点**，不按收益渐进。
> 本文件是 4 个实现代理的**共同契约**。接口不改就不许改实现。

---

## 1. 目标

$$\Rightarrow\ \text{整网 } 5.83\ \text{s} \;\to\; \mathbf{1.5\sim2.0\ s}\quad(\text{v50 一轮达到})$$
$$\Rightarrow\ \text{手段三条同时上}：\textbf{① 16-lane 算力}\quad \textbf{② 分段流式融合}\quad \textbf{③ convfp 进 PL}$$

$$\Rightarrow\ \text{预期分项（v5 实测 → v50 目标）}$$
| 项 | v5 | v50 目标 | 依据 |
|---|---:|---:|---|
| `conv` | 1.12 | **0.56** | 延迟受限，lane 翻倍 ⇒ 组遍历减半（T-66）|
| `pack` | 1.08 | **0.35** | 与 conv 融合，中间张量不落 DDR |
| `prologue` | 1.14 | **0.30** | 量化搬 PL + 描述符进程级预计算 |
| `convfp` | 1.15 | **0.81** | fconv 4-lane 进 PL（v44 已建）|
| `interp/sam/eca/add/epi` | 0.63 | **0.35** | 融进流式通路 |
| `wait` / `overlap` | 0.6 | **0.1** | 一次提交整段，不再逐 op 轮询 |
| **合计** | **5.83** | **≈2.5** | 见 §8 |

---

## 2. 核心架构决定（**已定，实现不许改**）

### D1 融合发生在 `bin_unit` 内部，**不新增第三个 HP 主口**
$$\Rightarrow\ \text{conv 的输出写进}\textbf{片上 band 缓冲}，\text{下一层从 band 缓冲读} \Rightarrow \text{不落 DDR}$$
$$\Rightarrow\ \text{pack（E1E2）也从 band 缓冲读} \Rightarrow \text{一次 DDR 都不写中间张量}$$
$$\Rightarrow\ \Rightarrow\ \textbf{仍只用 HP0}\ (\texttt{bin\_unit})\ +\ \textbf{HP2}\ (\texttt{fconv}) \Rightarrow \textbf{不需要 HP3}$$
$$\Rightarrow\ \text{这是 v50 最大的简化：}\text{BD 只需 = v44 的 BD（fconv + HP2）}$$

### D2 16-lane 是 **LANES 参数**，共享读语义不许变
$$\Rightarrow\ \texttt{LANES=8}\ \text{时必须与 } \texttt{rtl\_v39\_exact}\ \text{功能等价（回归保底）}$$
$$\Rightarrow\ \text{lane } k = \text{输出通道 } o_0+k;\ \text{一个 64-bit 激活字喂全部 lane（}\textbf{不许改成独立读）}$$
$$\Rightarrow\ \texttt{p\_acc[15{:}0]}\ \text{位宽与深度}\textbf{一律不许动}（\text{字节一致性的根）}$$
$$\Rightarrow\ \text{C16 交错布局}：\text{16 lane 覆盖 128-bit}（\text{两个 64-bit 字}）\Rightarrow \text{C8 布局是 C16 的特例}$$

### D3 段描述符 = **链式**，一次 `kick` 跑完一段
$$\Rightarrow\ \text{段描述符头 } 8\ \text{个字（沿用 v39 描述符）+ 2 个字扩展：}$$
$$\quad\ \texttt{next\_pa}（\text{下一个描述符的物理地址，0=链尾}）,\ \ \texttt{nband}（\text{本段行带数}）$$
$$\Rightarrow\ \texttt{kick}\ \text{语义改为"启动整条链"};\ \texttt{done}\ \text{在链尾置位};\ \texttt{progress}\ \text{寄存器给出已完成描述符数}$$
$$\Rightarrow\ \text{出错（地址越界/超时/金丝雀）}\Rightarrow \texttt{err}\ \text{置位并}\textbf{停止推进}，\text{不再取下一个描述符}$$

### D4 band 缓冲规格
$$\Rightarrow\ \text{每带高度 } \texttt{BAND\_ROWS}\ \text{行（参数，默认 }8）；\text{双缓冲（计算带 + 输出带）}$$
$$\Rightarrow\ \text{缓冲字节} = 2 \times \texttt{BAND\_ROWS} \times W \times C \times 1\ \text{B}$$
$$\quad\ \text{预算：BRAM 剩 } 60 \times 36\ \text{Kb} \approx 270\ \text{KB}$$
$$\quad\ \Rightarrow\ \text{256² 下 } C=48\ \text{时：} 2 \times 8 \times 256 \times 48 = 196\ \text{KB}\ \Rightarrow \textbf{能装}$$
$$\Rightarrow\ \text{段内 halo}：\text{级联 } k\times k \Rightarrow \text{额外 } \sum(k_i-1)\ \text{行}$$

### D5 小算子**留在 PL**，挂到流式通路上
$$\Rightarrow\ \texttt{sam/interp/eca/add}\ \text{已是 } \texttt{bin\_unit}\ \text{的子模块} \Rightarrow \text{在 band 通路上原地做}$$
$$\Rightarrow\ \text{PS 不再为它们发命令};\ \text{段描述符里用 op 码指明该带要做哪些小算子}$$

### D6 PS 侧：**一次算好整网段计划，一次提交**
$$\Rightarrow\ \texttt{build\_graph()}\ \text{之后立刻产出 } \texttt{SegmentPlan}（\text{段列表 + 每段的描述符链 + 缓冲需求}）$$
$$\Rightarrow\ \text{执行期只剩：提交首描述符} + \text{轮询 } \texttt{progress}/\texttt{done} + \text{处理错误}$$
$$\Rightarrow\ \textbf{消灭 925 条 MMIO 命令}（\text{现在 } 698\ \text{pack} + 227\ \text{conv}）$$

---

## 3. 接口契约（**四份契约，不许单方面改**）

### 契约 A：寄存器映射（`bin_unit`，AXI-Lite）
$$\Rightarrow\ \text{保留 } \texttt{0x000..0x1FF}\ \text{全部既有寄存器语义（v39 原样）}$$
$$\Rightarrow\ \text{新增：}\texttt{0x200 CHAIN\_HEAD}（\text{描述符链首地址}）、\texttt{0x204 PROGRESS}（\text{已完成描述符数}）、\texttt{0x208 BAND\_ROWS}、\texttt{0x20C FUSE\_MODE}$$
$$\Rightarrow\ \texttt{0x250..0x25C}\ \textbf{不许再放任何东西}（\text{v42d 证明观测器会改时序}）；\text{诊断走 ILA}$$

### 契约 B：描述符格式（10 个字 = 40 B，4 KB 对齐）
$$\Rightarrow\ \text{字 } 0..7\ \text{沿用 v39 描述符（}\texttt{desc/out/cfg\_c/cfg\_h/cfg\_w/cfg\_group/\dots}\text{）}$$
$$\Rightarrow\ \text{字 } 8 = \texttt{next\_pa};\quad \text{字 } 9 = \texttt{nband} \,\|\, \texttt{op\_mask}（\text{低 }16\ \text{位行带数，高 }16\ \text{位小算子掩码}）$$
$$\Rightarrow\ \texttt{next\_pa}=0\ \Rightarrow \text{链尾};\ \text{地址必须 }4\ \text{KB 对齐、落在已注册 arena 内}$$

### 契约 C：band 缓冲布局
$$\Rightarrow\ \text{线性、行主序、通道最内：}\texttt{band[r][c][x]}\ \text{其中 } x=0..W-1$$
$$\Rightarrow\ \text{元素宽度 } = \text{int8（1 B）};\ \text{与 C8/C16 布局一致（1 字节 = 1 通道）}$$
$$\Rightarrow\ \text{双缓冲由 RTL 内部管理，PS 不感知}$$

### 契约 D：段计划（PS → 描述符链）
$$\Rightarrow\ \text{每段} = \{\texttt{ops[], h\_in, w\_in, c\_in, h\_out, w\_out, c\_out, halo\_rows, bands}\}$$
$$\Rightarrow\ \text{每段的描述符数} = \texttt{bands};\ \text{描述符里 }\texttt{nband}\ \text{恒 }1（\text{一条描述符 = 一个行带}）$$
$$\Rightarrow\ \text{段间通过 } \texttt{next\_pa}\ \text{串起来} \Rightarrow \text{整网一条链}$$

---

## 4. 必须修改的点（**完整清单，共 19 项**）

### RTL（7 项）
| # | 文件 | 改动 |
|---|---|---|
| **R1** | `bin_conv_core8.v` → `bin_conv_core.v`（参数化）| `LANES` 参数；`LANES=16` 时 pop 树 ×16；`p_acc` 宽度/深度**不变**；C8/C16 布局统一 |
| **R2** | `bin_unit_axi_wrap.v` | `LANES` 参数；权重 BRAM 8 块→16 块；`ST_WLD` 扩展；地址链/`win_q`/AR/预取**一律不动** |
| **R3** | 新增 `bin_band_buf.v` | band 缓冲（双缓冲 + halo 行 + 读写口仲裁）|
| **R4** | 新增 `bin_segq.v` | 描述符链队列 + `progress` + 出错即停 + `mux` 复位语义 |
| **R5** | `bin_unit_axi_wrap.v`（融合控制）| 把 conv/pack/小算子按 `op_mask` 串到 band 通路上；`FUSE_MODE=0` 时退化为 v39 行为 |
| **R6** | `bin_sam.v`/`bin_interp.v`/`bin_rprelu.v`/`bin_catpack.v` | 加 band 通路接口（原地读写），**`FUSE_MODE=0` 时保持原接口** |
| **R7** | `bin_e1e2.v` | **不动**（正交）；只在其入口加 band 通路选择 |

### BD / 构建（3 项）
| # | 文件 | 改动 |
|---|---|---|
| **B1** | `bd_v50.tcl` | `bin_unit` 换 16-lane 版 + **加 fconv + 开 HP2**（= v44 的 BD，v44 验通后直接派生）|
| **B2** | 地址图 | `conv_uni 0x40040000`、`bin_unit 0x40060000`、**`fconv 0x40080000`**、各 128K；**既有项 0 变化** |
| **B3** | `build_final_150v50.tcl` + `verify_rtl_root.sh` | T-65 已在做（manifest 断言机制化）|

### PS（6 项）
| # | 文件/函数 | 改动 |
|---|---|---|
| **P1** | `bin_net.py: build_graph()` 之后 | 新增 `make_segment_plan()` → `SegmentPlan`（段/描述符/缓冲需求）|
| **P2** | `bin_engine.py: prepare_segments()` | 改成**生成描述符链**（10 字/条，链式），不再生成逐 op 命令序列 |
| **P3** | `bin_engine.py: run_one()/kick()` | 语义改为"提交链首 + 等整链 done"；`progress` 轮询；错误即停 |
| **P4** | `bin_net.py: _exec_ps()` | 段内 op 不再逐个驱动；只处理"段边界"（fan-out 处物化）|
| **P5** | `bin_net.py: _fp_conv()` | 接 `FconvEngine`（`BIN_CONVPF_PL`），失败静默回落 CPU |
| **P6** | `q8.py`/`precomp`/`qcache` | 与段计划对齐；量化搬 PL 后删除 CPU 量化路径 |

### 验证（3 项，由仿真代理负责）
| # | 内容 |
|---|---|
| **V1** | xsim：`LANES=8` 回归（≡ v39）+ `LANES=16` 等价性（同输入 8-lane×2 ≡ 16-lane×1，逐字节 0 diff）|
| **V2** | xsim：`bin_segq` 语义（链推进/`progress`/出错即停/mux 复位）|
| **V3** | xsim：band 通路逐字节（conv→band→pack 一次走完 ≡ 原"落 DDR 再读")|

---

## 5. 字节一致性不变量（**任何实现都不许破**，来自 T-66 的 8 条 + 我的 3 条）
$$\Rightarrow\ \text{① } \texttt{p\_acc[15{:}0]}\ \text{位宽与深度不变};\quad \text{② } \texttt{cout}\%\ 8\ \text{不许收紧成 } \%\ 16$$
$$\Rightarrow\ \text{③ 末块补零}\textbf{双侧}消隐};\quad \text{④ lane 8-15 用}\textbf{自己的}参数切片}（\text{不许复用 lane }0\text{-}7）$$
$$\Rightarrow\ \text{⑤ } \texttt{done}\ \text{必须等 } \texttt{wr\_all\_idle};\quad \text{⑥ } \texttt{nvalid}\ \text{掩码按实际 lane 数算}$$
$$\Rightarrow\ \text{⑦ C16 交错布局必须与 C8 兼容（C8 是 C16 的子集）}$$
$$\Rightarrow\ \text{⑧ } \texttt{FUSE\_MODE=0}\ \text{时必须逐字节等于 v39 行为（回归保底）}$$
$$\Rightarrow\ \text{⑨ 段间 halo 不许少一行、不许重复累加}$$
$$\Rightarrow\ \text{⑩ 双缓冲切换时不许出现半写带被读}$$
$$\Rightarrow\ \text{⑪ 出错即停后，band 缓冲与 mux 必须回到}\textbf{可重新启动}状态$$

---

## 6. 并行分工（4 个实现代理 + 1 审查 + 1 仿真）

| 角色 | 拥有文件 | 交付 |
|---|---|---|
| **IMP-A（RTL 算力）** | `bin_conv_core*.v`、`bin_unit_axi_wrap.v` 的 lane/权重部分 | R1、R2 |
| **IMP-B（RTL 数据流）** | `bin_band_buf.v`、`bin_segq.v`、融合控制、小算子接口 | R3、R4、R5、R6、R7 |
| **IMP-C（PS）** | `bin_net.py`、`bin_engine.py`、`fconv.py` 接线、`q8/precomp/qcache` | P1~P6 |
| **IMP-D（BD/构建）** | `bd_v50.tcl`、`build_final_150v50.tcl`、`verify_rtl_root.sh` | B1~B3 |
| **REVIEW（1）** | 只读 + 合并 | 审契约符合性、字节不变量、接口一致；**发现问题退回对应 IMP** |
| **SIM（1）** | `pl/rtl_hand/_worktrees/v50_sim/` | V1~V3；找错误**与优化点**，结果退回对应 IMP |

$$\Rightarrow\ \textbf{冲突规则}：\text{同一文件只能一个 IMP 改};\ \text{跨文件的接口改动必须先在 }\texttt{V50\_ARCHITECTURE.md}\ \text{里改契约}$$
$$\Rightarrow\ \textbf{合并规则}：\text{REVIEW 用 } \texttt{ps/tools/merge\_gate.sh} + \text{逐文件 md5} + \text{契约检查；不合格退回，不许"我先合进去再修"}$$

---

## 7. 验收（v50 整体）
$$\Rightarrow\ \textbf{G1 功能}：\text{64²/256² 整网 } \texttt{sha1} = \text{冻结三件套}，\textbf{两次连跑逐位相同}$$
$$\Rightarrow\ \textbf{G2 回归}：\texttt{FUSE\_MODE=0} + \texttt{LANES=8}\ \text{时逐字节等于 v39}$$
$$\Rightarrow\ \textbf{G3 金丝雀}：\text{227/227 全绿}$$
$$\Rightarrow\ \textbf{G4 性能}：\text{256² 整网 } \le 2.5\ \text{s}（\text{同 session vs v5 } 5.83）$$
$$\Rightarrow\ \textbf{G5 资源}：\text{LUT } \le 42{,}000（91\%）,\ \text{BRAM } \le 90,\ \text{DSP } \le 150$$
$$\Rightarrow\ \textbf{G6 时序}：\text{按 R8 先不设门槛，}\text{但要记录最差路径与"建议功能验证时钟"}$$

---

## 8. 时间预算（诚实）
$$\Rightarrow\ \text{4 个 IMP 并行写（RTL 两天、PS 两天）}\Rightarrow \text{首轮可用约 }\mathbf{2\sim3\ 天}$$
$$\Rightarrow\ \text{REVIEW + SIM 迭代 }2\sim3\ \text{轮}\Rightarrow \mathbf{+2\ 天}$$
$$\Rightarrow\ \text{上板调通（按 R8 低频验证）}\Rightarrow \mathbf{+2\sim3\ 天}$$
$$\Rightarrow\ \textbf{总计 } 6\sim8\ \text{天到 } 2.0\sim2.5\ \text{s}$$
$$\Rightarrow\ \text{再往 }1\ \text{s 需要：}\text{200 MHz 收时序} + \text{剩余 PS 桶全搬 PL} \Rightarrow \mathbf{+5\sim8\ 天}$$

---

## 9. 命名契约（2026-11-02 15:35 补充，**解决 IMP-B 与 BD 的接不上**）

$$\Rightarrow\ \text{v39 有}\textbf{两层}：\mathtt{bin\_unit\_axi\_wrap.v}（\text{单元}）\ \text{与}\ \mathtt{bin\_unit\_axi\_wrap\_bd.v}（\text{BD 外壳，module-ref 指向它}）$$
$$\Rightarrow\ \text{v50 也必须给两层，且}\textbf{名字不能与旧文件重名}（\text{T-65 的 } \mathtt{bd\_v50.tcl}\ \text{有重名断言）}$$

| 层 | v50 文件名 | 模块名 | 拥有者 |
|---|---|---|---|
| 单元 | `bin_unit_axi_wrap_v50.v` | `bin_unit_axi_wrap_v50` | IMP-B |
| **BD 外壳** | `bin_unit_axi_wrap_bd_v50.v` | **`bin_unit_axi_wrap_bd_v50`** | **IMP-B**（外壳 `FREQ_HZ 150000000`、端口不窄于旧外壳）|
| conv 核 | `bin_conv_core_v50.v` | `bin_conv_core_v50`（`LANES`/`VLANES` 参数）| IMP-A |
| （基线薄壳，**不动**）| `bin_conv_core.v` | `bin_conv_core`（v39 1-lane）| 冻结 |
| band 缓冲 | `bin_band_buf.v` | `bin_band_buf` | IMP-B |
| 描述符链 | `bin_segq.v` | `bin_segq` | IMP-B |

$$\Rightarrow\ \mathtt{bd\_v50.tcl}\ \text{的 module-ref 必须指向 } \mathbf{\mathtt{bin\_unit\_axi\_wrap\_bd\_v50}}$$
$$\Rightarrow\ \text{所有新模块名}\textbf{不得与 } \mathtt{rtl\_v50/}\ \text{里既有的 25 个模块重名}$$
$$\Rightarrow\ \textbf{2026-11-02 T-82/D4 落实：lane16/ 的核改名 } \mathtt{bin\_conv\_core\_v50}\text{（文件 } \mathtt{bin\_conv\_core\_v50.v}\text{）}$$
$$\Rightarrow\ \text{理由：基线 } \mathtt{bin\_conv\_core.v}\ \text{（薄壳）已定义 } \mathtt{module\ bin\_conv\_core}\text{，lane16 再定义同名模块会让 } \mathtt{build\_final\_150v50.tcl}\ \text{的重名断言硬 error；改名后 25 个基线模块名保持冻结}$$

---

## 10. 契约修订 R1（2026-11-02 15:30）——**寄存器块搬家 + 描述符 w6/w7 定死**

### 10.1 撞车：`0x200` 段已被 E1E2 占用
$$\Rightarrow\ \text{IMP-C 实测：}\mathtt{E1E2\_REG}\ \text{的 } \mathtt{RUN\_UNIT/E\_GROUP/E\_DESC/E\_DESC\_HI}\ \text{正是 } \mathtt{0x200/0x204/0x208/0x20C}$$
$$\Rightarrow\ \text{我原定的 } \mathtt{CHAIN\_HEAD/PROGRESS/BAND\_ROWS/FUSE\_MODE}\ \text{与它}\textbf{逐字冲突}$$

$$\Rightarrow\ \textbf{修订：v50 新寄存器整体搬到 } \mathbf{\mathtt{0x300}} \text{ 段，E1E2 的 } \mathtt{0x200}\ \text{段}\textbf{一个字节都不动}$$

| 地址 | v50 寄存器 | 说明 |
|---|---|---|
| `0x300` | `CHAIN_HEAD` | 描述符链首物理地址（4 KB 对齐）|
| `0x304` | `PROGRESS` | 已完成描述符数（只读）|
| `0x308` | `BAND_ROWS` | 行带高度 |
| `0x30C` | `FUSE_MODE` | 0=旧路径（默认），1=段融合 |
| `0x310` | `CHAIN_STATUS` | bit0 done / bit1 err / bit2 busy / bit8~15 err_code |
| `0x314` | `CHAIN_ID` | **只读 magic = `0x5030_0001`**（"V50" + 版本 1）。PS 用它判定"链引擎在位"：该地址写忽略、读回常量，`BIN_V50_CHAIN_RTL=1` 的探针因此不可能被"寄存器回声"骗过（D9）。`FUSE_MODE=0` 时同样可读 |

$$\Rightarrow\ \mathtt{0x000..0x1FF}\ \text{与 } \mathtt{0x200..0x20C}\ \textbf{全部保持原样}$$
$$\Rightarrow\ \mathtt{0x250..0x25C}\ \text{仍}\textbf{禁止}（\text{v42d 教训}）$$
$$\Rightarrow\ \mathtt{0x314}\ \text{原为留白，2026-11-02 T-82/D9 定死为只读 } \mathtt{CHAIN\_ID}\text{；}\mathtt{0x310}\ \text{与 } \mathtt{0x314}\ \text{之间不插别的寄存器}$$

### 10.2 契约 B 的字 6/字 7（原留白，现定死）
$$\Rightarrow\ \text{字 } 6 = \mathtt{band\_y0}\ (\text{本带起始行号，绝对行号，不是带内偏移}）$$
$$\Rightarrow\ \text{字 } 7 = \mathtt{flags}：\text{bit0=末带、bit1=本带需物化、bit2=含 shuffle、bit3=输出 f32}$$
$$\Rightarrow\ \text{其余位保留为 } 0;\ \text{RTL 必须忽略未定义位}（\text{不报错}）$$

### 10.3 PRM 帧宽（IMP-C 已实现，登记入契约）
$$\Rightarrow\ \text{帧宽 }= 2\times\mathtt{VLANES}\ \text{字} \Rightarrow \mathtt{VLANES=8}\ \text{时 } 16\ \text{字 / 偏移 } 0,2,6,10,14;\ \mathtt{VLANES=16}\ \text{时 } 32\ \text{字 / } 0,4,12,20,28$$
$$\Rightarrow\ \text{打包点在 } \mathtt{ps/tools/pack\_bin\_weights.py:388\text{-}395}；\text{新增 } \mathtt{bin\_engine.prm\_layout()}\ \text{与 } \mathtt{check\_prm\_capacity()}$$
$$\Rightarrow\ \text{前提已核：227 层的 } \mathtt{cout}\in\{32,48,64,96,192\}\ \text{全是 } 16\ \text{的倍数} \Rightarrow \text{容器不需 padding}$$

### 10.4 合并时必须补的一行（否则基线门禁假失败）
$$\Rightarrow\ \mathtt{bin\_net}\ \text{新增懒加载 } \mathtt{import\ fconv} \Rightarrow \mathtt{deps\_closure.py}\ \text{必须加 } \mathtt{DECL["fconv"]}$$
$$\Rightarrow\ \text{这是第 41/44 条纪律的}\textbf{第三次}（前两次是 } \mathtt{convpf\_c}、\mathtt{mmio\_batch}\text{）\Rightarrow \text{合并代理必须跑 } \mathtt{merge\_gate.sh}\ \text{三关}$$

---

## 11. 契约修订 R2：**halo / pad / cfg_h 的段边界语义**（2026-11-02 15:45）

> 背景：IMP-B 报"band 模式下 `cfg_h`=带内行数(含 halo)，段边界 pad 与 halo 行怎么叠要与 IMP-A/IMP-C 一起定、未与 PS 对齐验证"。这是**必须由契约裁定**的缺口。

### 11.1 三个量的定义（**互不重叠**）
$$\Rightarrow\ \mathtt{y0}（\text{描述符字 }6）: \text{本段}\textbf{第一个有效输出行}（\text{绝对行号}）$$
$$\Rightarrow\ \mathtt{nband}（\text{描述符字 }9\text{ 低 }16\text{ 位}）: \text{本段}\textbf{有效输出行数}；\text{最后一带可以 } < \mathtt{BAND\_ROWS}$$
$$\Rightarrow\ \mathtt{HALO}（\text{寄存器 } \mathtt{0x308}\ \text{按 } k\ \text{自动推}): \text{输入侧需要}\textbf{上下各多少行}$$

### 11.2 band 缓冲里装什么
$$\Rightarrow\ \text{band 缓冲装的是}\textbf{输入}：\text{行号范围 } [\mathtt{y0}-\mathtt{HALO},\ \mathtt{y0}+\mathtt{nband}+\mathtt{HALO})$$
$$\Rightarrow\ \text{行数} = \mathtt{nband} + 2\cdot\mathtt{HALO}$$
$$\Rightarrow\ \text{输出只写 } [\mathtt{y0},\ \mathtt{y0}+\mathtt{nband})\ \Rightarrow \textbf{halo 行只读不写}$$

### 11.3 pad 由**填充路径（E 口）**做，不由 conv 做
$$\Rightarrow\ \text{E 口按行号 } \mathtt{yr}\ \text{填充时：}\text{若 } \mathtt{yr}<0\ \text{或}\ \mathtt{yr}\ge H_{\text{in}}\ \Rightarrow \textbf{整行写 0}（\text{不发起 DMA}）$$
$$\Rightarrow\ \Rightarrow\ \text{conv 读侧}\textbf{永远不需要判边界}（\text{它读到的 pad 行已经是 0}）$$
$$\Rightarrow\ \text{好处：} k\in\{1,3,5\}\ \text{共用同一填充器，conv 内部零分支}$$

### 11.4 段内级联的 halo 叠加
$$\Rightarrow\ \text{若一段内有 } n\ \text{个 conv 级联，}\mathtt{HALO} = \sum_i (k_i-1)/2$$
$$\Rightarrow\ \text{T-67 实测：当前网络级联 halo 只有 } \{0,2\}\ \text{行} \Rightarrow \text{预算无压力}$$
$$\Rightarrow\ \text{但 }\textbf{填充器只填"段输入的原始行"}，\text{中间层输出留在 band 缓冲里} \Rightarrow \text{级联 halo 由 RTL 内部管理，PS 只给 }\mathtt{HALO}_{\text{段}}$$

### 11.5 `cfg_h` 的最终语义（**IMP-B 的实现据此对齐**）
$$\Rightarrow\ \text{conv 的 } \mathtt{cfg\_h} = \mathtt{nband}\ (\textbf{不含 halo}）$$
$$\Rightarrow\ \text{band 缓冲的行数} = \mathtt{nband} + 2\cdot\mathtt{HALO}\ \text{由硬件按寄存器自动算，}\textbf{PS 不传缓冲行数}$$
$$\Rightarrow\ \mathclap{\text{}} \text{若 IMP-B 当前把 } \mathtt{cfg\_h}\ \text{当成"含 halo 的行数"}\Rightarrow \textbf{必须改成不含}$$

### 11.6 权重/PRM 基址走寄存器（IMP-B 的做法，**采纳**）
$$\Rightarrow\ \text{融合模式下 }\mathtt{r\_wgt/r\_shift/r\_*\_shift}\ \text{在}\textbf{段边界由 PS 写一次}（\text{不再逐描述符带}）$$
$$\Rightarrow\ \text{理由：描述符只有 10 字，塞不下 5 个指针};\ \text{且段内各层的权重是连续排布的}$$
$$\Rightarrow\ \Rightarrow\ \textbf{IMP-C 必须配合}：\text{段边界写权重基址，}\text{且}\textbf{写序必须在 } \mathtt{CHAIN\_HEAD}\ \text{之前}（\text{否则第一描述符就用错基址}$$
$$\Rightarrow\ \text{描述符里的 } \mathtt{desc\_pa}\ \text{仍按 v39 语义（该层的激活/权重描述），}\text{基址寄存器提供}\textbf{段级起点}$$

### 11.7 未完成项（IMP-B 自报，必须有人接）
$$\Rightarrow\ \text{② } \mathtt{op\_mask}\ \text{只实现 bit0（conv）};\ \textbf{conv}\to\textbf{pack}\to\textbf{sam}\ \text{的}\textbf{带内串联序列未做}$$
$$\Rightarrow\ \text{④ } \mathtt{catpack}\ \text{融合模式不支持（} \mathtt{fuse\_cfg\_bad}\to \mathtt{ERR\_UNIT}）$$
$$\Rightarrow\ \text{⑤ 层例化依赖 IMP-A 的核文件存在（无核时 } \mathtt{xvlog}\ \text{过、} \mathtt{xelab}\ \text{不过）}$$

---

## 12. 契约修订 R3：三处不一致的裁定（2026-11-02 16:00）

### 12.1 magic 值不一致（**PS 与 RTL 各说一个**）
$$\Rightarrow\ \text{RTL（T-82/T-83 已仿真验证）：}\mathtt{0x314}\ \text{只读恒 } \mathbf{\mathtt{0x5030\_0001}}$$
$$\Rightarrow\ \text{PS（T-81）：期望 } \mathbf{\mathtt{0x7635\_5000}}$$
$$\Rightarrow\ \Rightarrow\ \textbf{裁定：以 RTL 为准 = } \mathtt{0x5030\_0001}$$（\text{它已在 } \mathtt{xsim}\ \text{里逐项验过，PS 只是常量}）
$$\Rightarrow\ \text{PS 侧改常量，}\mathtt{BIN\_V50\_CHAIN\_MAGIC}\ \text{仍可覆盖};\ \textbf{但必须保留"读不到就不走链"}$$

### 12.2 `0x310` 空闲态误报 err（T-83 发现）
$$\Rightarrow\ \text{复位后 } \mathtt{0x310}\ \text{读 } \mathbf{\mathtt{0x0403}}（\mathtt{done}=1,\ \mathtt{err}=1,\ \mathtt{code}=4=\mathtt{E\_CANARY}）$$
$$\Rightarrow\ \text{机理：}\mathtt{band\_buf.shape\_bad}（\mathtt{cfg\_c}=0/\mathtt{cfg\_w}=0\ \text{在复位态恒真）}\to \mathtt{cap\_err}\ \text{粘滞}\to \mathtt{band\_bad}\to \mathtt{segq}\to \mathtt{S\_ERR}$$
$$\Rightarrow\ \text{kick 会清零，所以 kick 后正常；但}\textbf{PS 若在 kick 前读 } \mathtt{0x310}\ \text{判 err 会误判}$$
$$\Rightarrow\ \textbf{裁定（两边都改）}：$$
$$\quad\ \text{① RTL：}\mathtt{cap\_err}\ \textbf{在第一次 kick 之前不许置位}（\text{加 } \mathtt{cfg\_valid}\ \text{门控}）$$
$$\quad\ \text{② PS：}\textbf{kick 之前不读 } \mathtt{err}，\text{只看 busy/done}；\mathtt{err}\ \text{只在 kick 之后有意义}$$

### 12.3 描述符字 3 高 16 = `H_in`（IMP-B2 的 C5 缺口）
$$\Rightarrow\ \text{R2 里我写了"band 装输入 } [\mathtt{y0}-\mathtt{HALO},\ \mathtt{y0}+\mathtt{nband}+\mathtt{HALO})"\text{，但}\textbf{没说 } H_{\text{in}}\ \text{从哪来}$$
$$\Rightarrow\ \text{IMP-B2 的实现落到"}\textbf{字 3 高 16 改解释成 } \mathtt{H\_in}\text{"} \Rightarrow \textbf{采纳，写进契约 B}$$
$$\Rightarrow\ \textbf{契约 B 修订}：\text{字 3 = } \mathtt{cfg\_h}\ (\text{低 }16) \parallel \mathtt{H\_in}\ (\text{高 }16)$$
$$\quad\ \text{填充器用 } \mathtt{H\_in}\ \text{判 } \mathtt{yr}\ge H_{\text{in}}\Rightarrow \text{整行写 0}；\text{PS 必须两个都填}$$
$$\Rightarrow\ \text{若 PS 只填低 16（旧行为）}\Rightarrow \mathtt{H\_in}=0 \Rightarrow \textbf{整带全 0} \Rightarrow \text{会在 sha1 上立刻暴露（好事）}$$

### 12.4 SAM 的垂直抽头与 `cfg_h=nband` **互斥**（IMP-B2 的 C5）
$$\Rightarrow\ \text{网络里的 } \mathtt{sam}\ \text{用 } y_2/y_4\ \textbf{垂直抽头}（\text{T-67 也说 }\mathtt{sam}\ \text{结构性必须两趟）$$
$$\Rightarrow\ \text{而 } \mathtt{cfg\_h}=\mathtt{nband}\ \text{的段内行带是"输出行"}\Rightarrow \text{抽头需要}\textbf{多行输入同时在带内}$$
$$\Rightarrow\ \textbf{裁定：}\mathtt{SAM}\ \textbf{不进融合段}（\text{本阶段}）$$
$$\quad\ \Rightarrow \mathtt{op\_mask}\ \text{的 bit2 保留但不许 PS 下发}；\mathtt{pack}\to\mathtt{sam}\ \text{的数据语义}\textbf{当前不成立}$$
$$\quad\ \Rightarrow\ \text{这也印证 T-67：}\mathtt{sam/tor}\ 0.245\ \text{s}\ \text{那一块}\textbf{先不动}$$
$$\Rightarrow\ \text{本阶段的融合只做 }\mathbf{CONV}\to\mathbf{PACK}\ \text{（}\mathtt{op\_mask}=0x3\text{）}$$

### 12.5 `in_pa`（字 0）在 band 模式下的语义（T-81 指出待裁）
$$\Rightarrow\ \text{旧语义：段输入张量基址（DDR）}$$
$$\Rightarrow\ \text{band 模式：输入来自 band 缓冲} \Rightarrow \text{字 0 改用为}\textbf{段输入在 DDR 的源基址}（\text{填充器用}）$$
$$\Rightarrow\ \Rightarrow\ \textbf{裁定：字 0 在 } \mathtt{FUSE\_MODE=1}\ \text{时解释为"段输入 DDR 基址"}，\text{band 偏移由 } \mathtt{band\_y0}\ \text{与填行号算}$$

---

## 13. 契约 B **最终定稿**（2026-11-02 17:05，解决 w3 归属冲突）

> T-87 报：IMP-B2 的填充器把 **w3 低 16 当 `W`**，而 §12.3 + PS 把低 16 当 `cfg_h`、`W` 在 w4。
> **裁定：以本文为准，填充器改。**

| 字 | 字段 | 说明 |
|---|---|---|
| **w0** | `src_pa` | `FUSE_MODE=1`：段输入在 DDR 的**源基址**；`=0`：旧语义（段输入张量基址）|
| **w1** | `out_pa` | 输出基址（**不带**带偏移；偏移只由 w6 给）|
| **w2** | `cfg_c` | 组通道数（旧语义）|
| **w3** | `cfg_h`(低 16) ‖ `H_in`(高 16) | `cfg_h` = 本带**有效输出行数**（不含 halo）；`H_in` = 输入张量高度 |
| **w4** | `cfg_w` | **行宽 `W`** |
| **w5** | `cfg_group` | 组数（旧语义）|
| **w6** | `band_y0` | 本带首个有效输出行的**绝对行号** |
| **w7** | `flags` | bit0 末带 / bit1 物化 / bit2 shuffle / bit3 f32；未定义位**忽略** |
| **w8** | `next_pa` | 下一描述符物理地址；**0 = 链尾** |
| **w9** | `nband`(低 16) ‖ `op_mask`(高 16) | `op_mask`：bit0 conv / bit1 pack / bit2 sam（**sam 本阶段禁用**）|

$$\Rightarrow\ \textbf{填充器必须}：\text{行宽取 } \mathtt{w4}，\text{带内有效行数取 } \mathtt{w3}\ \text{低 16}，\text{输入高度取 } \mathtt{w3}\ \text{高 16}$$
$$\Rightarrow\ \textbf{不许}：\text{把 } \mathtt{w3}\ \text{低 16 当 } W（\text{IMP-B2 的写法，必须改}）$$

---

## 14. 描述符字段**权威表**（2026-11-02 T-89，**以 RTL 实测解码为准**）

> ## ⚠ **本节以 `bin_segq.v` 的实际解码为唯一真相源：§3、§12.3、§13 全部作废。**
> 三版字段表（§3 / §12.3 / §13）**每一版都与 RTL 对不上**，且对不上的**不是命名，是位**。
> 本节保留即生效；§13 的表格只作历史记录，**不许再据此接线、填描述符、写填充器**。
> 依据：T-88 的逐字节实测 + T-89 通读 `lane16/bin_segq.v`（483 行，md5 `191bda10…`）、
> `bin_band_fill_v50.v`（`1b0407bd…`）、`bin_unit_axi_wrap_v50.v`（`0229c186…`）。
> 完整逐行证据表：`runs/v89_desc/AUTHORITATIVE_DESC_FIELDS.md`、`runs/v89_desc/desc_field_map.csv`。

### 14.1 第一性事实：`dw[i]` 是「64-bit 拍」，不是「字」

$$\Rightarrow\ \mathtt{DESC\_WORDS}=10,\ \mathtt{HDR\_BEATS}=(10\times4+7)/8=\mathbf{5}\ (\text{segq:55-56})$$
$$\Rightarrow\ \mathtt{dw[hdr\_beat]}\le\mathtt{hdr\_q}\ (\text{segq:304}),\quad \mathtt{hdr\_addr}=\mathtt{cur\_pa}+\mathtt{beat}\times8\ (\text{segq:204})$$

| 拍 | 字节 | 覆盖的字 |
|---|---|---|
| `dw[0]` | 0..7 | 字 0 ‖ 字 1 |
| `dw[1]` | 8..15 | **字 2** ‖ 字 3 |
| `dw[2]` | 16..23 | 字 4 ‖ 字 5 |
| `dw[3]` | 24..31 | 字 6 ‖ 字 7 |
| `dw[4]` | 32..39 | 字 8 ‖ 字 9 |

$$\Rightarrow\ \textbf{凡把 } \mathtt{dw[1]}\ \text{当"字 1"的写法都错位了一个字：}\mathtt{dw[1][31{:}0]}\ \text{是字 2，不是字 1}$$

### 14.2 权威字段表（bit 段 → 语义 → 行号 → 消费者）

行号：RTL = `bin_segq.v`；消费方 = `bin_unit_axi_wrap_v50.v`。

| 拍/bit | 字节 | =字 | 语义（RTL 实际行为） | segq 行 | 消费者 |
|---|---|---|---|---|---|
| `dw[0][31:0]` | 0..3 | w0 | `in_pa`（装载源 + arena 检查） | 339, 346, 240, 243 | 填充器 `.in_pa`(1809)、S_LOAD 地址(206) |
| `dw[0][63:32]` | 4..7 | **w1** | **无人读取（悬空）** | — | 无 —— 本应在此的 `out_pa` 被丢弃 |
| `dw[1][15:0]` | 8..9 | w2 | `cin`(=`cfg_c`) | 326 | conv `.cfg_cin`(1372) |
| `dw[1][31:0]` | 8..11 | w2 | **被当 `out_pa`/排空基址** ⇒ 实为 cin | 340, 347, 240, 244 | 排空 DDR 地址(462) |
| `dw[1][47:32]` | 12..13 | w3 低 | `cfg_h`=本带有效输出行数（RTL 名叫 `d_w`） | 328 | 填充器 `.cfg_h`(1811)、`fill_rows`(1348)、`band_words`(1660) |
| `dw[1][63:48]` | 14..15 | w3 高 | `H_in`（RTL 名叫 `d_h`） | 327 | 填充器 `.h_in`(1812) |
| `dw[2][15:0]` | 16..17 | w4 低 | `cfg_w`=行宽 `W`（RTL 名叫 `d_cout`） | 329 | conv `.cfg_w`(1381)、填充器 `.w_row`(1813) |
| `dw[2][31:16]` | 18..19 | w4 高 | 未读 | — | — |
| `dw[2][39:32]` | 20 | w5[7:0] | RTL 名 `d_k`（PS 填的是 `cfg_group`） | 330 | conv `.cfg_k`(1374) |
| `dw[2][47:40]` | 21 | w5[15:8] | RTL 名 `d_s` | 331 | conv `.cfg_s`(1375) |
| `dw[2][55:48]` | 22 | w5[23:16] | RTL 名 `d_d` | 332 | conv `.cfg_d`(1376) |
| `dw[2][63:56]` | 23 | w5[31:24] | RTL 名 `d_pad` | 333 | conv `.cfg_pad`(1377) |
| `dw[3][15:0]` | 24..25 | w6 低 | `band_y0`（绝对行号） | 334 | 填充器 `.y0`(1810) |
| `dw[3][31:16]` | 26..27 | w6 高 | 未读 | — | — |
| `dw[3][63:32]` | 28..31 | w7 | `flags`；**且被 E_NULL 要求非 0** | 335, 241 | 无消费者（`seg_flags` 接了不用） |
| `dw[4][31:0]` | 32..35 | w8 | `next_pa`（0=链尾） | 336, 404, 407, 410 | segq 内部 |
| `dw[4][47:32]` | 36..37 | w9 低 | `nband`；**E_SIZE 要求非 0** | 337, 236 | conv `.cfg_h`(1379)、填充器 `.nband`(1814)、`band_words`(1658) |
| `dw[4][63:48]` | 38..39 | w9 高 | `op_mask`；**OP_MASK_SUP 检查** | 338, 238 | `fill_conv_en`(1346)、`band_opmask`(1755) |

### 14.3 `dw[0]` 与 `dw[1]` 低 32 位的真相（本次要澄清的那一条）

$$\mathtt{dw[0][31{:}0]}=\text{字 0}=\mathtt{in\_pa}\ (\textbf{地址})\quad✔$$
$$\mathtt{dw[1][31{:}0]}=\text{字 2}=\mathtt{cfg\_c}=\mathtt{cin}\ (\textbf{不是地址})\quad✘$$

四条独立证据：
$$\Rightarrow\ \text{① } \mathtt{dw[1][15{:}0]}\ \text{已被单独锁成 } \mathtt{f\_cin}(326)\text{，同一 32 位段不可能既是 cin 又是地址}$$
$$\Rightarrow\ \text{② 该当 } \mathtt{out\_pa}\ \text{的 } \mathtt{dw[0][63{:}32]}\ \text{在整份文件里}\textbf{零读取}\text{（"必须存在的字段没人读"只有一个解释）}$$
$$\Rightarrow\ \text{③ segq:318-325 的注释自述"字2=dw[1][31:0]、字3=dw[1][63:32]"，只有 240/244/340/347 四行违反同一份注释}$$
$$\Rightarrow\ \text{④ 模块头注释(20-21)写"字0 in_pa / 字1 out_pa"，与代码在"字 1"上分叉}$$

$$\Rightarrow\ \textbf{判定：这是 } \mathtt{dw}\ \text{把"字"当"拍"的 off-by-one（4 处），不是设计取向}$$
与 v39 布局（`in_pa/out_pa/cin/{h,w}/cout/{pad,d,s,k}`）完全一致，只错在索引。

### 14.4 后果（四条，全部**静默**）

| # | 后果 | 证据 |
|---|---|---|
| ① | **排空写到 DDR 低地址**：`st_wa = cin + cnt*8`，PS 的 `out_pa` 被整体丢弃 | segq:347, 462 |
| ② | `cin=0` 的描述符被判 E_NULL | segq:240 |
| ③ | `in_arena` 把 **cin 当缓冲区**查包含性 ⇒ PS 一旦写 `0x318/0x31C`，第一条描述符就 E_RANGE | segq:244 |
| ④ | `flags=0`（非末带、不需物化）被判 E_NULL，违反 §10.2"未定义位忽略" | segq:241 |

$$\Rightarrow\ \text{why 至今没炸：}\mathtt{ARENA\_LO/HI}\ \text{复位 } \mathtt{0/0xFFFF\_FFFF}\ (\text{wrap:891})\text{，PS 从不写 } \mathtt{0x318/0x31C}$$
$$\Rightarrow\ \Rightarrow\ \texttt{in\_arena}\ \text{恒真、E\_NULL 只卡 cin=0 ⇒ \textbf{segq 一条 err 都不报，输出静默写到地址 cin}}$$
同 `docs/板端事故_内存踩踏_真相.md` 的失效类型（PL 写到不该写的地方）。

### 14.5 PS 填 ↔ RTL 读 逐条对照：**6 处对不上**

$$\Rightarrow\ \text{位置错位 }1\ (\mathtt{out\_pa}\to\text{w1 悬空，RTL 读 w2})\ +\ \text{字段不存在 }5\ (\mathtt{cout,k,s,d,pad})$$
$$\Rightarrow\ \text{另有 }1\ \text{处校验误伤（}\mathtt{flags}=0\Rightarrow\mathtt{E\_NULL}\text{）、}1\ \text{处接了不消费（}\mathtt{flags}\text{）}$$

| 字 | PS 填 | RTL 读 | 判定 |
|---|---|---|---|
| w0 | `in_pa` | `dw[0][31:0]` | ✅ |
| w1 | `out_pa` | **无** | ❌ 错位 |
| w2 | `cfg_c` | `dw[1][31:0]`(当 out_pa) + `dw[1][15:0]`(cin) | ⚠ 一错一对 |
| w3 低/高 | `cfg_h` / `H_in` | `d_w` / `d_h` | ✅ 值对名错（T-88 已按名改接） |
| w4 | `cfg_w`=W | `d_cout` | ✅ 值对名错（T-88 已按名改接） |
| w5 | `cfg_group`=0 | `d_k/d_s/d_d/d_pad` | ❌ conv 拿到 k=s=d=pad=0 |
| w6 / w7 / w8 / w9 | `band_y0` / `flags` / `next_pa` / `nband\|op_mask` | 同 | ✅（w7 无人消费 + E_NULL 误伤） |

$$\Rightarrow\ \textbf{结构性缺口}：\mathtt{cout}\ \text{在 10 个字里}\textbf{没有位置}（\text{w4 被 } W \text{ 占、w5 被 } \mathtt{cfg\_group}\ \text{占}）$$
$$\Rightarrow\ \text{现在融合模式下 conv 的 } \mathtt{cfg\_cout} = \mathtt{w4} = W（\text{行宽，例如 224}），\textbf{不是通道数}$$

### 14.6 必须改 `bin_segq.v` 的 4 行（T-89 提案 —— **已批准并已应用**，见 §14.10）

```diff
- 240: (dw[0][31:0]==0) || (dw[1][31:0]==0) || (dw[3][63:32]==0)
+ 240: (dw[0][31:0]==0) || (dw[0][63:32]==0)
- 244: in_arena({32'd0, dw[1][31:0]}, ...)
+ 244: in_arena({32'd0, dw[0][63:32]}, ...)
- 340: f_out    <= dw[1][31:0] & 32'hFFFF_FFFF;
+ 340: f_out    <= dw[0][63:32];
- 347: st_base <= dw[1][31:0];
+ 347: st_base <= dw[0][63:32];
```
$$\Rightarrow\ \text{不改状态机、不改时间轴、不改端口、不改 } \mathtt{DESC\_WORDS}\text{；四条已验证路径语义不变}$$

### 14.7 新发现（需裁定）：conv 核的 `cfg_h` **双用途**

$$\mathtt{hout\_c}=((\mathtt{cfg\_h}-1)>>s)+1\ (\text{conv:156})\quad\text{vs}\quad \mathtt{hw8\_c}=\mathtt{cfg\_h}\cdot\mathtt{cfg\_w}\cdot8\ (\text{conv:163})$$
$$\Rightarrow\ \mathtt{cfg\_h}\ \text{同时是"输出行数"和"64-通道块步长乘数"（}\mathtt{cp1}=(\mathtt{cb}-1)\cdot\mathtt{hw8}\text{，conv:805）}$$
$$\Rightarrow\ \text{band 布局要求块步长}=\mathtt{rows\_fill}\cdot W\cdot8\text{，输出行数要求}=\mathtt{nband}$$
$$\Rightarrow\ \mathtt{halo}\neq0\ \text{时 } \mathtt{rows\_fill}=\mathtt{nband}+2\mathtt{HALO}\neq\mathtt{nband}\ \Rightarrow\ \textbf{无法同时满足（} \mathtt{cin}>64 \text{ 时必错）}$$
$$\Rightarrow\ \text{T-88 的 } \mathtt{c\_cfg\_h}=\mathtt{nband}\ \text{对"输出行数"对、对"块步长"错；缺口④ 的 } \mathtt{base\_act}=\mathtt{HALO}\cdot W\cdot8\ \text{修不了它}$$

**建议**：本阶段融合只跑 `k=1`（`halo=0` ⇒ 两用途自洽），`k=3/5` 段继续走 v39 路径，
不碰 `bin_conv_core_v50.v`。（三条备选方案见 `runs/v89_desc/AUTHORITATIVE_DESC_FIELDS.md` §6。）

### 14.8 悬空字段清单（供后续清理）

$$\Rightarrow\ \mathtt{dw[0][63{:}32]}\ (\text{w1})、\mathtt{dw[2][31{:}16]}\ (\text{w4 高})、\mathtt{dw[3][31{:}16]}\ (\text{w6 高})\ \textbf{零读取}$$
$$\Rightarrow\ \mathtt{d\_out\_pa}、\mathtt{d\_flags}\ \text{在包装层}\textbf{接了不消费}$$

### 14.9 附带登记：`.OP_MASK_SUP` 会把 `conv→cat` 融合段全拒

$$\Rightarrow\ \text{包装层传 } \mathtt{.OP\_MASK\_SUP(16'h0001)}\ (\text{segq 默认 } \mathtt{32'h00FF})，\text{而 PS 的 } \mathtt{V50\_OP\_BITS}=\{\mathtt{conv}{:}0,\mathtt{cat}{:}1,\dots\}$$
$$\Rightarrow\ \text{融合段 } \mathtt{op\_mask}=0x3\Rightarrow \mathtt{segq:238}\ \text{判 } \mathtt{E\_OPMASK}=6\Rightarrow \textbf{第一条描述符就被拒}$$
$$\Rightarrow\ \text{修法：} \mathtt{.OP\_MASK\_SUP(16'h0003)}，\text{并核 cat 在带模式下的读口（E1E2 已接 band C 口）}$$

### 14.10 状态（2026-11-02 17:5x，T-89 第二阶段已落地）

$$\Rightarrow\ \text{§14.6 的 4 行补丁}\textbf{已应用}\text{（segq md5 } \mathtt{266454b8\dots}\text{），四条 FSM 路径 + 3 条新判据仿真 }\mathbf{28/0}$$
$$\Rightarrow\ \mathtt{.OP\_MASK\_SUP}\ \textbf{已改 } \mathtt{16'h0003}\text{；但带内没有级联序列器 ⇒ PS 只融合单级 conv 段（见 §15）}$$

---

## 15. T-89 第二阶段实施记录（2026-11-02，四缺口 + arena 窗口 + 门禁）

> 本节记录"§14 权威表落到代码"的那一步：改了什么、判据是什么、还剩几条阻断。
> 逐条判据的原始日志在 `runs/v89_desc/`。

### 15.1 代价最高的一个更正：排空字数 = `ceil(cout/8)`，**与 lane 数无关**

$$\Rightarrow\ \text{band 存 int8，}\textbf{一个 64-bit 字 = 8 个通道}（\text{C8 布局}）\Rightarrow \text{排空字数/像素} = \lceil \mathtt{cout}/8 \rceil$$
$$\Rightarrow\ \text{曾写成 } \lceil \mathtt{cout}/\mathtt{VLANES} \rceil \Rightarrow \mathtt{VLANES}=16\ \text{时}\textbf{只搬一半}（\text{8-lane 恰好等价，只在 16-lane 暴露}）$$
$$\Rightarrow\ \text{两个"每字多少通道"是不同的数据类型，不许混：}$$
$$\quad \text{装} = \lceil \mathtt{cin}/64 \rceil\ (\text{act 是二值位流，1 bit/通道 ⇒ }64\text{ 通道/字，与 conv:158 的 }\mathtt{cb\_c}\text{ 同口径})$$
$$\quad \text{排} = \lceil \mathtt{cout}/8 \rceil\ (\text{输出是 int8，8 bit/通道 ⇒ }8\text{ 通道/字})$$
$$\Rightarrow\ \text{两者都与 } \mathtt{VLANES}\ \text{无关；lane 数只决定 PRM 帧宽（}2\times\mathtt{VLANES}\text{ 字）}$$
$$\Rightarrow\ \text{本项目 } \mathtt{cout}\in\{32,48,64,96,192\}\ (\text{全是 }16\text{ 的倍数}) \Rightarrow \text{旧口径在 8-lane 下数值恰好相同，}\textbf{只有 16-lane 会错一半}$$

### 15.2 arena 窗口：默认值 ⇒ **拒绝启动**（不再 fallback）

$$\Rightarrow\ \text{PS 侧：} \mathtt{v50\_submit\_chain(..., arena=(lo,hi))}\ \text{是}\textbf{必填}，\text{不给直接 } \mathtt{ValueError}$$
$$\quad \text{写序：} \mathtt{0x318/0x31C}\to\mathtt{0x308}\to\mathtt{0x30C}\to\mathtt{0x300}\ (\text{全部在 } \mathtt{CHAIN\_HEAD}\ \text{之前})$$
$$\Rightarrow\ \text{RTL 侧：} \mathtt{ARENA\_LO}{=}0\ \&\&\ \mathtt{ARENA\_HI}{=}\mathtt{0xFFFF\_FFFF}\Rightarrow \mathtt{kick}\ \text{被门控掉}，\mathtt{u\_err}\ \text{报 } \mathtt{E\_RANGE}{=}2$$
$$\Rightarrow\ \textbf{判据（已实测）}：\text{越界 } \mathtt{out\_pa}{=}\mathtt{0x8000}\Rightarrow \text{segq 报 } \mathtt{E\_RANGE}(2)；\text{旧 RTL 上这条}\textbf{必须失败}（\text{它连字1 都没读}）$$

### 15.3 四条缺口落地情况

| # | 缺口 | 修法 | 状态 |
|---|---|---|---|
| ① | `cout/k/s/d/pad` 取 segq 的 v39 解码 | 描述符里**没有这些位** ⇒ 走**段边界寄存器** `0x70/0x40/0x48/0x50/0x58`，PS 在 `CHAIN_HEAD` 之前写 | ✅ |
| ② | `band_words` 装/排共用一个数 | 相位化：`LOAD=⌈cin/64⌉·rows·W`、`DRAIN=⌈cout/8⌉·nband·W`、`S_CHK=max(两者)` | ✅ |
| ③ | `S_CHK` 当拍读到上一条描述符 | 包装层 `sh_dw[0:4]+sh_beat`（S_HDR 5 拍自存一份，同 IMP-B2）；`band_words`/填充器/conv 几何全吃影子 | ✅ |
| ④ | conv 读带行偏移 `base_act=0` | **不接 HALO 偏移**：`k=1 ⇒ halo=0`，`base_act=0` 本来就对 | ✅（靠 k=1 锁） |

### 15.4 本阶段**只有单级 conv 段能融合**（这条必须记住）

$$\Rightarrow\ \text{lane16 的包装层}\textbf{没有带内级联序列器}（\text{对比 IMP-B2 的 } \mathtt{seq\_stage}/\mathtt{bin\_band\_seq.v}）$$
$$\Rightarrow\ \mathtt{c\_start}=\mathtt{seg\_band\_start}\ \text{只起 conv}；\mathtt{e\_start}/\mathtt{k\_start}\ \text{仍只走 v39 的地址选择}$$
$$\Rightarrow\ \text{含 }\ge2\ \text{级算子的段若判成融合} \Rightarrow \textbf{后面的级被静默跳过}（\text{错数，不报错}）$$
$$\Rightarrow\ \textbf{PS 侧加门禁}：\mathtt{len(ops)}{=}{=}1\ \wedge\ \mathtt{ops[0].kind}{=}{=}\mathtt{conv}\ \text{才融合}$$
$$\Rightarrow\ \text{实测（FakePL 整网 223 段）}：\textbf{融合 90 段 / 140 条描述符}，\text{其余 133 段走 v39 逐 op}$$

### 15.5 门禁实测（全部通过，日志在 `runs/v89_desc/`）

| 判据 | 结果 |
|---|---|
| `bin_segq` 四条 FSM 路径 + 3 条新判据（out_pa 落地 / flags=0 不误判 / 越界报 E_RANGE） | **28 ok / 0 fail**（旧 RTL 对照：22 ok / **6 fail**） |
| `xvlog -sv` 全 15 个文件 | rc=0，**0 error / 0 warning** |
| `xelab bin_unit_axi_wrap_v50 VLANES=8 / 16` | **Built**（两份快照） |
| `FUSE=0` 结构不变 | 全部 `sel_fuse_q ? … : v39` 的**else 分支逐字节未动**；唯一改动的 `u_err` 只在 fuse 分支加 `arena_err` |
| `verify_lazy_net --hw 64`（taskset −c 6-7） | sha1 **`104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd`** = 冻结值；改前/改后归一化 stdout **0 差异** |
| `FUSE=1`（FakePL 无链引擎 ⇒ 自动回落 v5） | sha1 与 FUSE=0 **逐位相同**（回落是字节等价的） |
| `verify_rtl_root` | **MATCH=35 DIFF=0 EXTRA=0 MISSING=0 PASS**（manifest 已刷新） |

### 15.6 还剩几条阻断 → 能不能出位流

$$\Rightarrow\ \textbf{位流可以出}：\text{结构上是 v39 基线 + 新增链通路，}\mathtt{FUSE=0}\ \text{路径逐位不变（已证）}$$
$$\Rightarrow\ \textbf{但 } \mathtt{FUSE=1}\ \text{现在还不能上板}，\text{还差三条：}$$
$$\quad \text{① } \textbf{带级联缺集成仿真}：\text{segq 单模块 TB 过了，但 } \mathtt{fill\to conv\to band\to drain}\ \text{的带级联}\textbf{没有 TB}$$
$$\quad \text{② } \textbf{级联序列器缺失}：\text{要吃到 } \mathtt{conv\to cat}\ \text{必须移植 IMP-B2 的 } \mathtt{bin\_band\_seq.v}\ (45\ \text{条段})$$
$$\quad \text{③ } k\ge3\ \text{不可融合}：\mathtt{cfg\_h}\ \text{双用途（§14.7），要改核或改契约}$$
$$\Rightarrow\ \text{建议门禁：}\mathtt{FUSE=1}\ \text{上板前必须先有 } \text{① 的逐个带级 TB 证据（否则又是"仿真等价、硬件崩"}$$
