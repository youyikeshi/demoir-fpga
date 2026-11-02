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
| conv 核 | `bin_conv_core.v` | `bin_conv_core`（`LANES` 参数）| IMP-A |
| band 缓冲 | `bin_band_buf.v` | `bin_band_buf` | IMP-B |
| 描述符链 | `bin_segq.v` | `bin_segq` | IMP-B |

$$\Rightarrow\ \mathtt{bd\_v50.tcl}\ \text{的 module-ref 必须指向 } \mathbf{\mathtt{bin\_unit\_axi\_wrap\_bd\_v50}}$$
$$\Rightarrow\ \text{所有新模块名}\textbf{不得与 } \mathtt{rtl\_v50/}\ \text{里既有的 25 个模块重名}$$

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

$$\Rightarrow\ \mathtt{0x000..0x1FF}\ \text{与 } \mathtt{0x200..0x20C}\ \textbf{全部保持原样}$$
$$\Rightarrow\ \mathtt{0x250..0x25C}\ \text{仍}\textbf{禁止}（\text{v42d 教训}）$$

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
