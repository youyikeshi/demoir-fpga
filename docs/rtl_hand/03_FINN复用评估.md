# 03 · FINN RTL SWG 复用评估（用于 k=3 窗口/行缓冲）

> **评估对象**：AMD/Xilinx FINN 的 RTL ConvolutionInputGenerator（"SWG"）
> 本地快照：`/data/work/finn_ref/`（4 个 `.sv` + 生成器 Python，见 §1.1 校验）。**下文 `gen:LNNN` = FINN 的 Jinja 生成器行号**
> **评估范围**：只回答"能不能用、怎么用、值不值得"。**未写产品 RTL、未综合、未跑 xsim、未改任何已有模块。**
> **方法**：读源码（SV + FINN 的 Jinja 生成器）→ 推导参数语义 → 用 Python 逐拍复刻 `swg_controller` 做交叉验证（§2.4）
> 最后更新：2026-10-26

---

## 0. 结论速览

> 符号约定：`gen:LNNN` = FINN 生成器 `convolutioninputgenerator_rtl.py` 的行号；`L1-L2`（无前缀）= 紧邻上下文那个文件的 `swg_common.sv`；`04:` / `06:` = `docs/rtl_map/04_mac_array.md` / `06_k5_and_primitives.md`。

| # | 模块 | 判定 | 一句话理由 |
|---|---|---|---|
| 1 | `swg_pkg.sv`（6 个枚举） | **A** | 无依赖、≤10 行有效代码，可直接打进同一个 SV fileset；仅剩"BD 顶层不能是 SV"的既有约束（§5.2） |
| 2 | `swg_controller` | **B（技术）/ C（用途）** | 语法上可原样实例化、8 个增量是自由参数；但它是"**一拍吐一个元素**"的串行地址生成器，我们要的是"一拍吐 9 个抽头"。按我们的 C8 地址公式填完参数后，单环形缓冲需求 = **1.18 MB（1,178,896 B）**（§2.7） |
| 3 | `swg_reg_buffer` | **A** | 27 行、纯 FF 移位、`data_out` 是**常数 part-select（零 mux）**；正是"横向 3 抽头链"的原语。可直接抄（保留许可证头） |
| 4 | `swg_ram_buffer` | **B** | 44 行 1R1W 环形 RAM 延迟线，可抄；但**无复位**（靠丢掉前 DEPTH 个输出对齐，§6.2），要加复位或搬 priming |
| 5 | `swg_cyclic_buffer_addressable` | **C** | **只有 1 个读口**——它就是把我们 II 卡住的那个结构本身。用等于复现瓶颈（§3.3） |
| 6 | `swg_template_default` | **C** | **吞吐不吃亏**（每拍 8 通道，等价于理想抽头并行，§3.3bis），但：(a) 假设"通道块最内层"的 HWC 内存序，与 C8 相反 → 缓冲需求 **1.18 MB**（§2.6）；(b) 不含 padding（§4.3） |
| 7 | `swg_template_parallel` | **B** | **拓扑完全正确**（reg FIFO + RAM FIFO 级联、抽头全是接线），但生成器**硬性要求 SIMD == IFMChannels**（`gen:817`）、**不含 padding**（`gen:355`、`gen:534`）、**无通道组维度**（官方文档把 M>1 标为 *Currently unsupported*）。只能是"抄结构"，不能"当模块用"（§4） |
| 8 | 生成器 `convolutioninputgenerator_rtl.py` | **C（代码）/ A（文档）** | 我们不会用它的 Jinja 参数体系，但它是**唯一权威的参数语义手册**（8 个增量 + 5 个计数器 + 阈值的公式全在里面） |

### 用不用、怎么用（明确回答）

> **结论：不复用任何 FINN RTL 模块来搭我们的 k=3 窗口/行缓冲。**
>
> 1. **可以抄进 `vendor/` 的只有 2 个原语**（`swg_reg_buffer` 27 行 + `swg_ram_buffer` 44 行 = 71 行），**且不推荐真抄**——见下面第 2 条：结构我们已经有更好的了。
> 2. **我们已经有等价且更合适的实现**：`conv_k5_engine.v` §2/§3 里已经写好了"5 条 BRAM 行缓冲 + 5×5 窗口移位链 + 行/列**内联零填充**"（`conv_k5_engine.v:309-372`、`:354-359`），k=3 = 把它从 5 改成 3。FINN **不支持内联 padding**（要靠前置 FMPadding 节点），抄它等于**主动丢掉**我们已有的边界处理。
> 3. **真正值得保留的是 FINN 的"结论"和"参数语义"**：官方文档明说并行风格"avoids memory port limitations and exploding multiplexing logic"（§4.1），这为我们 04/06 两篇的"列口数才是 II 的根因"提供了**外部独立佐证**；生成器里 8 个增量的公式是"如果哪天真要写通用 SWG，该怎么算"的参考手册。
> 4. **对 k=5 的 II=3 也救不了**：FINN default 的吞吐其实与理想方案持平（它用"8 通道/拍"补回了"1 抽头/拍"，§3.3bis），所以"借它解决 II=3"这个动机**在原理上就不成立**——II=3 只能靠"每行 1 读口 + 横向移位链"解决，那是我们 06 §6.2 已经规划的事。而 FINN 的 parallel 风格虽然正是那个结构，却不能当模块用（§4）。

**估计净收益：负。** 见 §6.1：从零把 k=3 的窗口/行缓冲写成"k=5 的特例"约 **0.5 天**；而"借 FINN"要付 1.5–3 天适配 + 0.5 天合规，还丢掉内联 padding。

---

## 1. 事实核对（先纠正 5 处，避免在错前提上评估）

### 1.1 本地快照校验

| 文件 | 本地字节 | 上游 `Xilinx/finn@main` 字节 | 一致？ |
|---|---:|---:|---|
| `swg_common.sv` | 8,584 | 8,584（`finn-rtllib/swg/`） | ✅ |
| `swg_pkg.sv` | 1,932 | 1,932 | ✅ |
| `swg_template_default.sv` | 12,270 | 12,270 | ✅ |
| `swg_template_parallel.sv` | 8,781 | 8,781 | ✅ |

- 上游模板目录里**还有 4 个文件本地没有**：`swg_template_wrapper.v` / `swg_template_axilite.v` / `swg_template_default_dynamic.sv` / `swg_template_wrapper_dynamic.v`。它们是 AXI-Stream 外壳与"动态形状"变体，**不影响本次结论**（我们不复用外壳，见 §4.4）。
- 为了能引用参数公式，本次另存了生成器：`/data/work/finn_ref/convolutioninputgenerator_rtl.py`（45,681 B / 1,014 行，上游 `src/finn/custom_op/fpgadataflow/rtl/`）。**下文 `gen:LNNN` 全部指这个文件**，行号对应 2026-10-26 的 main 快照。
- 我的 `swg_controller` 逐拍复刻件存在 `/data/work/finn_ref/_swg_controller_model.py`（不是仿真，只是用来验证我对状态机的理解，§2.4）。

### 1.2 五处前提更正

| # | 任务书/常见说法 | 实测 | 证据 |
|---|---|---|---|
| 1 | "k=3 层共 114 层，cin/cout 最大 352/192" | 114 层 ✅；**k=3 的 cin 最大是 192**，352 是 k=1 的最大值；cout 最大 192 ✅ | `runs/prof20261023_grid/layers_model_final.json`：k=3 的 cin ∈ {16,48,64,96,112,160,192}，cout ∈ {12,16,64,96,192} |
| 2 | "k=3 的 II=3 是我们的根因" | **k=3 的 HLS 路径是 II=2**；II=3 是 k=5（`LOOP_892_47`） | `docs/rtl_map/04_mac_array.md` §0 表（`764_38` 2 个 stage）+ §3.2；`06_k5_and_primitives.md` §1.5 |
| 3 | "k=3 全部 stride=1" | **2 层是 stride=2**：`encoder.encoder_1.down`(48→96,320²) 与 `encoder.encoder_2.down`(96→192,160²)。这两层占了 k=3 总 MAC 的 34%（4.25e9×2 / 2.48e10） | 同上 json 的 `s` 字段；`04_mac_array.md` §6.5 也已点名 |
| 4 | "cin=160/cout=64/H=W=80/pad=1 这个真实层" | **网络里没有这一层**。最接近的两个：`decoder.preconv_3`（cin=**192**,cout=64,80×80）与 `decoder.preconv_2`（cin=**160**,cout=64,**160**×160）。§2.5 两张都算 | 同上 json |
| 5 | "`conv_k1_engine.v`（39000 行）" | **859 行 / 37,011 字节**（39K 应该是字节数） | `wc -l` |

> 第 2、3 条对评估很关键：**II=2 → 目标 II=1 的收益是 2×（不是 3×）**；**stride=2 的那两层无法用"1 读/行 + 3 深移位链"直接覆盖**（每拍要跨 2 列，抽头变成 `x[c], x[c-2], x[c-4]`，见 `04_mac_array.md:515-516`）。

### 1.3 k=3 层的关键规模（决定缓冲大小）

| 层 | cin | cout | H=W | s | ⌈cin/8⌉ 组 | MAC | 备注 |
|---|---:|---:|---|---:|---:|---:|---|
| `decoder.preconv_1` | 112 | 64 | 320 | 1 | 14 | 6.61e9 | 行缓冲最贵 |
| `encoder.encoder_1.down` | 48 | 96 | 320 | 2 | 6 | 4.25e9 | **stride=2** |
| `encoder.encoder_2.down` | 96 | 192 | 160 | 2 | 12 | 4.25e9 | **stride=2** |
| `decoder.preconv_2` | 160 | 64 | 160 | 1 | 20 | 2.36e9 | |
| `decoder.preconv_3` | 192 | 64 | 80 | 1 | 24 | 7.08e8 | |
| 其余 109 层 | ≤112 | ≤192 | ≤320 | 1 | ≤14 | 合计 ≈5.3e9 | 其中 108 层是 cin=16 的 ghost 3×3 |

---

## 2. `swg_controller` 能不能原样实例化？

### 2.1 接口与参数（`swg_common.sv:34-63`）

```systemverilog
module swg_controller import swg::*; #(
  LOOP_H/W/KH/KW/SIMD_ITERATIONS,   // 5 个嵌套计数器（L36-40）
  INCR_BITWIDTH,                    // 增量位宽（L42）
  IS_DEPTHWISE,                     // L44
  HEAD_INCR_SIMD/KW/KH/W/H,         // 5 个"层内"增量（L46-50）
  TAIL_INCR_W/H/LAST,               // 3 个"窗口尾"增量（L51-53）
  state_e INNERMOST_STATE           // L55：初始=最内层状态
)(clk, rst_n, advance, addr_incr, tail_incr);
```

- 依赖：只有 `swg_pkg.sv` 的 6 个枚举（`swg_pkg.sv:33-40`）。`import swg::*` 写在模块头里，所以**必须整个文件按 SystemVerilog 编译**。
- **无乘法器、无存储、无组合环**：纯计数器 + 大小比较 + 一个 5 选 1 `case`（`swg_common.sv:76-85`）+ 一个 3 选 1（`L89-93`）。触发器约 `Σ log2(trip)` ≈ 40–60 FF。
- 结论（语法层）：**可以原样实例化**，不需要改一行。

### 2.2 状态机：一拍正好推进一个"输入元素"

推导（对着 `swg_common.sv:96-144` 读）：

1. `advance` 每拉高一拍，计数器走一步（`L121`）；外层状态周期 `state_next = INNERMOST_STATE` 无条件（`L98`），**只停 1 拍**。
2. 最内层计数器 `< 0` 时选下一个外层（`L100-107`），并在**同一个周期**把该层计数器预置回 `LOOP_x_ITERATIONS` 并把外层计数器减 1（`L123-142`）。
3. 因此外层状态周期**不是空转**：它照样输出一个 `addr_incr`（`L79-83` 的 `case`），照样读一个元素——在我验证的 4 组配置里"周期总数 == 输出元素总数"（§2.4）。
4. `-2` 的来历（`gen:479-483`）：生成器先 `trip-1`（因为初始状态就是最内层，第一个元素不需要"进入"），再 `-2`（状态机本身占掉首次/末次的两拍）。**所以参数值 = trip − 2（最内层再额外 −1）**，是"计数器上界"而不是"循环次数"，别按循环次数传。

### 2.3 `addr_incr` 的语义

> **`addr_incr` 是"输出元素流上相邻两个元素的、输入流线性索引之差"，单位是**元素**（= `BIT_WIDTH×SIMD` 位），**不是字节**。

证据链：

| 事实 | 位置 |
|---|---|
| 用 `addr_incr` 递推的是**读地址**，且在**环形缓冲内回卷**（不是字节地址） | `swg_template_default.sv:212-216`（`ra = read_addr_reg + addr_incr`，再按 `BUF_ELEM_TOTAL` 修正） |
| 同一个增量也递推"已消费到第几个输入元素" | `swg_template_default.sv:230`（`Current_elem += addr_incr`）；并行风格同 `swg_template_parallel.sv:211` |
| 增量由生成器按 `channel_factor = IFMChannels/SIMD` 算 | `gen:365`（`channel_factor`）、`gen:380-400`（8 个公式）、`gen:500-507`（赋参） |

8 个增量的**完整公式**（`gen:379-394`，非 depthwise 分支）：

```python
addr_incr_end_simd        = 1                                              # → HEAD_INCR_SIMD
addr_incr_end_window_elem = (dilation_w - 1) * channel_factor + 1          # → HEAD_INCR_KW
addr_incr_end_window_row  = (w - kernel_width)*cf + (dilation_h-1)*w*cf + 1# → HEAD_INCR_KH
addr_incr_end_window      = -buffer_min_size + stride_w*cf + 1             # → HEAD_INCR_W
addr_incr_end_row         = -buffer_min_size + (skip_columns+kernel_width)*cf
                            + (stride_h-1)*w*cf + 1                        # → HEAD_INCR_H
tail_incr_w   = stride_w*cf            (gen:445)                           # → TAIL_INCR_W
tail_incr_h   = (skip_columns+kernel_width)*cf + (stride_h-1)*w*cf (gen:446)# → TAIL_INCR_H
tail_incr_last= buffer_min_size - 1    (gen:447)                           # → TAIL_INCR_LAST
buffer_min_size = ((k_h-1)*dil_h*w + (k_w-1)*dil_w + 1) * channel_factor   # gen:367
```

> 读法：`HEAD_*` 是"读到该层最后一个元素时，一次跳多远"；负值 = **环形缓冲回卷**（`-buffer_min_size + 前向偏移`）。所以 `HEAD_INCR_W` 的语义是"**窗口整体右移 stride 个像素**"。

**能否直接产生我们需要的"行缓冲地址 + 字节偏移"？**

- **字节偏移（`c mod 8`）它产生不了**：它只输出"元素索引差"，没有任何位域/掩码语义。C8 的"8 通道在一个 64-bit 字里"这层信息必须由下游（MAC 前的字节选择）自己处理——和我们现在 `conv_k1_engine.v:405` 的 `p_word[{ic_cur[2:0],3'b000} +: 8]` 一样。
- **行缓冲地址它能产生**（任何"固定增量的嵌套线性走查"它都能表达），但**只在"一次一个元素"这个前提下**（§2.7 会说明这恰好是我们不需要的）。

### 2.4 交叉验证（我复刻状态机，逐拍核对）

做法：把 `swg_common.sv:76-144` 逐行翻译成 Python（`/data/work/finn_ref/_swg_controller_model.py`），用生成器算出的参数跑，然后把**读地址序列**和**im2col 理论序列** `((oy+ky)*w + ox+kx)*cf + c`（`w` = IFMDim 宽，模 `buffer_min_size`）逐拍对比：

| 配置 | 周期数 | 理论元素数 | 地址序列 |
|---|---:|---:|---|
| cf=1, k=3, W=10, out 8×8 | 576 | 576 | **完全一致** |
| cf=2, k=3, W=10, out 8×8 | 1,152 | 1,152 | **完全一致** |
| cf=4, k=3, W=10, out 8×8 | 2,304 | 2,304 | **完全一致** |
| cf=2, k=5, W=7, out 3×3 | 450 | 450 | **完全一致** |

> 这保证了我下面"参数怎么映射到 C8"的推导不是猜的。**注意**：这是复刻件的等价性验证，**不代替 xsim**，也不知道 Vivado 的综合/时序行为。

**`tail_incr` 语义（任务书要求"读状态机推导"）**：

- 它只在**输出窗口回卷**的那一拍更新（`swg_template_default.sv:219-224`：`PositionZero` 时 `First_elem_next_window += tail_incr`）。
- 它表达的是"**下一个输出窗口的首元素**在输入流里的位置"的增量，用来做**写指针覆盖保护**（`swg_template_default.sv:139-145`：只有"最老元素已经不再需要"才允许写）。
- **实测发现（要标出来）**：`tail_incr` 按**当拍**的计数器值组合选择（`swg_common.sv:89-93`），而那个计数器要到**同一拍末尾**才递减（`swg_common.sv:127`、`:133`）——两者错开一拍 ⇒ "行末跳变"**提前一个窗口**生效。在我验证的 cf=1/k=3/W=10/out=8×8 配置里，tail 序列是 `1,1,1,1,1,1,3,1,…`，而真实窗口首地址差是 `1,1,1,1,1,1,1,3,…`。累计值在**行边界上重新对齐**，因此：**只影响缓冲需求偏大（保守），不影响读出数据正确性**。这一点我在 FINN 文档里没找到说明，属于从 RTL 推出来的行为，**若将来真用，必须以 xsim 为准**。

### 2.5 参数取值示例

#### (a) 把我们的数据"假装成 HWC 流"（FINN 的原生口径）

> 前提：`IFMChannels=C`、`SIMD=8`（= 一个 C8 字）、`IFMDim=82×82`（pad 已在外层物化）、out=80×80。
> **这个口径对我们的内存布局是错的**（§2.6 会说明），列出来只是为了拿到"生成器会算出什么"。

**`decoder.preconv_3`（cin=192, cout=64, 80×80, pad=1）**

| 参数 | 值 | 来源 |
|---|---:|---|
| `LOOP_H_ITERATIONS` / `LOOP_W_ITERATIONS` | 78 / 78 | = out−2 |
| `LOOP_KH_ITERATIONS` / `LOOP_KW_ITERATIONS` | 1 / 1 | = k−2 |
| `LOOP_SIMD_ITERATIONS` | 21 | = (cf−1)−2, cf=24 |
| `INNERMOST_STATE` | `STATE_LOOP_SIMD` | `gen:461-464` |
| `HEAD_INCR_SIMD` / `_KW` / `_KH` | 1 / 1 / **1897** | `(82−3)·24+1` |
| `HEAD_INCR_W` / `_H` | **−3983** / **−3935** | `−4008+24+1` / `−4008+72+1` |
| `TAIL_INCR_W` / `_H` / `_LAST` | 24 / 72 / 4007 | |
| `INCR_BITWIDTH` | 13 | `gen:487-498` |
| `BUF_ELEM_TOTAL` | 4008 字 = **32.1 KB** | `((2·82)+2+1)·24` |
| `ELEM_PER_WINDOW` | 216 | `k_h·k_w·cf` |
| `LAST_READ_ELEM` | 161,375 | `82²·24−1` |

**`decoder.preconv_2`（cin=160, 160×160, pad=1）**：`LOOP_{H,W}=158`，`LOOP_SIMD=17`（cf=20），`HEAD_INCR_KH=3181`，`HEAD_INCR_W=−6519`，`HEAD_INCR_H=−6479`，`TAIL={20,60,6539}`，`BUF_ELEM_TOTAL=6540 字 = 52.3 KB`，`ELEM_PER_WINDOW=180`。

#### (b) 按**我们真实的 C8 地址**映射（8 个增量重新算）

地址公式（`00_SPEC` §2.1）以 **64-bit 字**为单位：

$$\text{word}(g,y,x)=g\cdot HW + y\cdot W + x,\qquad g=\lfloor c/8\rfloor$$

5 个计数器只能这样放（`KW/KH/SIMD` 是固定的三层，`SIMD` 一定在最内）：

| 计数器 | 映射到 | 步长 |
|---|---|---:|
| `LOOP_SIMD` | 通道组 `g` | `HEAD_INCR_SIMD = HW` |
| `LOOP_KW` | 列抽头 `kx` | `HEAD_INCR_KW = 1` |
| `LOOP_KH` | 行抽头 `ky` | `HEAD_INCR_KH = W` |
| `LOOP_W` | 输出列 `x` | `HEAD_INCR_W = 1 − (N_g−1)·HW − 2W − 2` |
| `LOOP_H` | 输出行 `y` | `HEAD_INCR_H = W − (N_g−1)·HW − 2W − 2` |

**`decoder.preconv_3`（N_g=24, HW=6400, W=80）**：

| 参数 | 值 |
|---|---:|
| `LOOP_H` / `LOOP_W` / `LOOP_KH` / `LOOP_KW` / `LOOP_SIMD` | 78 / 78 / 1 / 1 / **21** |
| `HEAD_INCR_SIMD` / `_KW` / `_KH` | **6400** / 1 / **80** |
| `HEAD_INCR_W` / `_H` | **−147,361** / **−147,282** |
| `TAIL_INCR_W` / `_H` / `_LAST` | 1 / 80 / 4007 |
| `INCR_BITWIDTH` | **19** |
| **单环形缓冲至少要多大** | 窗口内地址跨度 `= (N_g−1)·HW + 2W + 2 = 147,362` 字 = **1.18 MB** |

`decoder.preconv_2`（N_g=20, HW=25,600, W=160）同理：`HEAD_INCR_{SIMD,KW,KH} = 25,600 / 1 / 160`，`HEAD_INCR_W = −486,721`，`INCR_BITWIDTH=20`，缓冲需求 **3.89 MB**。

### 2.6 为什么"假装 HWC"也不行

C8 的内存序是 **`g` 最外层**（`addr = g·HW·8 + y·W·8 + x·8 + b`），FINN 假设的是 **`c` 块最内层**（`y·W·C + x·C + c`，`gen:365` 的 `channel_factor` 就是"每像素 cf 个元素"）。

两者对同一个 3×3 窗口的"元素地址集合"：

| | 元素单位 | 一个窗口占的地址 | 缓冲 |
|---|---|---|---|
| FINN/HWC | 8 通道 = 1 字 | 连续一小段：`2W·cf + 3cf` 字 | 4,008 字（32 KB） |
| 我们的 C8 | 8 通道 = 1 字 | `N_g` 段、每段 3×3 字，段间隔 `HW` | **147,362 字（1.18 MB）** |

这就是**唯一的、也是致命的差异**：不是"参数难算"（算出来了，见 §2.5b），而是**环形缓冲要装下整整 24 个通道平面**。想缩到 3 行，就必须让"组"变成片上独立的延迟线物理实体——那已经是并行风格了，`swg_controller` 的地址生成在整个并行风格里**根本不用来读数据**（`swg_template_parallel.sv:211` 只把它用于 `Current_elem` 记账）。

### 2.7 判定：`swg_controller`

| 维度 | 结论 |
|---|---|
| 能否原样实例化 | **能**（零改动、只有 1 个 package 依赖） |
| 能否产生我们需要的地址 | **能算出来**（§2.5b 的 5 个增量），但对应缓冲 **1.18 MB**（preconv_3）/ 3.89 MB（preconv_2） |
| 对 k=3 的用途 | **无用**，但**不是因为慢**：它的吞吐与理想方案相同（§3.3bis）。无用是因为它要求"窗口的元素在环形缓冲里连续"⇒ 在 C8 下要 **1.18 MB**（preconv_3）/ 3.89 MB（preconv_2），而它产生的 5 个增量只能寻址那一个缓冲 |
| 判定 | **B（技术）/ C（用途）**——"能装上，装上也对，但缓冲放不下、padding 还得另做" |

---

## 3. 三种缓冲实现怎么选

### 3.1 逐个拆（资源 / 端口 / 访问模式）

| | `swg_reg_buffer`<br>`swg_common.sv:176-202` | `swg_ram_buffer`<br>`swg_common.sv:205-248` | `swg_cyclic_buffer_addressable`<br>`swg_common.sv:149-173` |
|---|---|---|---|
| 存储 | `WIDTH×DEPTH` 个 **FF**（`L189`） | 1R1W RAM（`L225`，`(*ram_style=...)` 可推断 BRAM/LUTRAM） | 1R1W RAM（`L165`） |
| 写 | `Data[DEPTH-1:1] <= Data[DEPTH-2:0]; Data[0] <= shift_in`（纯移位，`L198-199`） | 写指针 `Addr_w` 自增回卷（`L233-239`） | `Ram[write_addr] <= data_in`（绝对地址，`L169`） |
| 读 | **全并行**：`assign data_out[e*WIDTH +: WIDTH] = Data[e]`（`L193-194`），**纯线，零 mux** | **1 个**，读指针固定在写指针前 `DEPTH` 位：`Out_reg <= Ram[Addr_r]`（`L234`） | **1 个**，绝对地址，同步读（`L168`） |
| 延迟 | 0（组合输出当前内容） | 1 拍寄存（`L234`） | 1 拍（`L168`/`L171`） |
| 同址同拍 | —（无地址） | 读的是 `DEPTH` 拍前的数据，物理上不会同址 | **read-first**：同址同拍读到**旧值**（非阻塞 `L168` 在 `L169` 前）→ **无 bypass** |
| 复位 | **没有 `rst_n`**（`L182`）→ 内容未定义 | 有 `rst_n`，只复位**指针**（`L228-231`），内容未定义 | 没有 `rst_n` |
| 每拍带宽 | `DEPTH×WIDTH` 位全可用 | `WIDTH` 位 | `WIDTH` 位 |
| 典型用途 | 横向抽头链（kernel width） | 行延迟线（line buffer） | default 风格的整窗口环形缓冲 |

### 3.2 我们的场景该选哪个

我们需要的（C8、k=3、pad=1、**每拍 9 抽头**）：

- **横向 3 抽头不能用 RAM**：3 个不同列地址/拍 = 3 个读口。RAM 做不到（除非 3 倍复制）。→ 必须用 `swg_reg_buffer` 这类寄存器链，**每行一条 3 深链**。
- **纵向 2 行才用 RAM**：每拍每行只需读 1 个新列 → 1 个读口够。→ `swg_ram_buffer` 的拓扑（延迟线）正确。
- **`swg_cyclic_buffer_addressable` 完全不需要**：它是 default 风格的核心，正是"1 读口 + 可变地址"。

**但请注意**：`swg_reg_buffer` / `swg_ram_buffer` 只有 **71 行**（`:176-202` / `:205-248`），而且

> 我们已经有等价实现，且更适合我们：`conv_k5_engine.v:309-372`（5 条 BRAM 行缓冲 + 5 深窗口移位链 + 行/列内联零填充）。

对比一下我们的 k=5 实现与 FINN 并行风格：

| | FINN 并行风格 | 我们的 k=5 引擎 |
|---|---|---|
| 行缓冲 | `swg_ram_buffer`，深度 `W−k_w` | 5 条 BRAM，深度 `W+2·PAD+2`（`conv_k5_engine.v:314-315`, `LB_DEPTH`） |
| 横向链 | `swg_reg_buffer`，深度 `k_w` | `win[0..4][0..4]` 显式移位（`:362-372`），**深度 = k_w**，一致 |
| 数据怎么动 | **数据在链里流**：每拍每个 RAM FIFO 都要 **1 写 + 1 读**（`swg_ram_buffer:232-234`），taps 永远是固定的寄存器位置 | **数据不动、地址动**：每拍 5 条 BRAM 各读 1 个新列（1 读/拍，`:325-330`），只有填充时才写 |
| RAM 端口压力 | `k_h−1` 个 RAM FIFO × (1R+1W)/拍 | 5 条 BRAM × 1R/拍（填充时另加 1W） |
| **padding** | ❌ 不含（要前置 FMPadding 节点） | ✅ **内联**：`col_ok`（`:354`）+ `lb_valid`（`:358`）组合成 `shift_pix = … ? lb_rd : 0` |
| 通道进制 | 元素 = `SIMD×bitwidth`（要 SIMD==C 才能并行） | 128 bit = 16 通道（`cin=12` 物理 16），**天然吃 C8** |
| 复位 | 无（靠丢前 N 个输出） | 有（`ap_rst_n`） |

**所以"选哪个"的答案是：选我们已经有的那个。** FINN 的原语只是同一件事的一个更小、更不完整的版本。

### 3.3 能真正解决"列口数不足"吗？

**能，而且这是 FINN 这套东西里唯一有战略价值的结论。** 但要分清两层：

1. **原理层**：把横向抽头从"存储器读"搬到"寄存器"，列口瓶颈**从根上消失**（不是缓解）。FINN 官方文档原文：并行风格 *"Uses a fixed structure of registers and line buffers to enable parallel access to multiple window elements. **This avoids memory port limitations and exploding multiplexing logic**"*（<https://finn.readthedocs.io/en/latest/components/rtl-swg.html>，§Architecture Differences）。这**独立佐证**了我们 `06_k5_and_primitives.md` §1.5 的判断（"行是免费并行的，列才是瓶颈"）和 `04_mac_array.md` §6.5 的方案。
2. **工程层**：我们自己**已经实现了**（k=5 引擎），k=3 只是把它从 5 抽头缩到 3。**"解决"这件事不需要 FINN。**

具体到数字（k=3，取最宽的 cin=192 层）：

| | HLS 现况 | 目标（链式，抽头并行） | FINN default 风格（通道并行） |
|---|---:|---:|---:|
| 每拍推进 | 1 个"通道 byte" | 1 个"通道 byte" × 9 抽头同时用 | 1 个"元素"= 8 个通道 × 1 抽头 |
| II | 2（`04:196`） | **1** | 9（= k²） |
| 每像素拍数 | `2·cin + 13`（`04:525`） | `cin + 3` | `9·(cin/8)` + 铺满 |
| 每拍 MAC | 36（72 乘法器 / II2） | 72（= 9 抽头 × 8 车道） | 8·L（L = 车道数） |
| **每像素每 oc 拍数** | `2·k²·C/M` | `k²·C/M` | **`k²·C/M`（相同！）** |

### 3.3bis ⚠️ 一处我必须自我更正：FINN default 风格**不慢**

我最初的判断是"FINN 的 default 风格串行 9 抽头 ⇒ 慢 2–9 倍"。**这个判断是错的**，核对后更正如下（保留在此以免后人再踩）：

> **在乘法器数量 M 相同的前提下，"抽头并行"和"通道并行"的吞吐完全相等**：
> $$\text{每像素每 oc 拍数}=\frac{k^2\cdot C_{\text{phys}}}{M}\quad(\text{两族结构都一样})$$
> 因为 FINN default 每拍虽然只推进 1 个抽头，但它那个"元素"里**装了 8 个通道**（`BUF_IN_WIDTH = BIT_WIDTH·SIMD`，`swg_template_default.sv:54` / `swg_template_parallel.sv:82`），8 个通道是并行做的。抽头并行省了 9 倍拍数，通道并行省了 8 倍拍数，**9 ≈ 8，基本抵消**。

代入实数（`C_phys=192`，k=3）：

| 结构 | M | 每像素每 oc 拍数 | 说明 |
|---|---:|---:|---|
| HLS 现况 | 72（40 DSP + 32 LUT） | **48** | `9·192/72 × II(2)` |
| 同 72 个乘法器但 II=1 | 72 | 24 | 只是把 II 干掉 |
| FINN default（8 通道并行） | 96（= 8 ch × 12 oc） | **18** | `9·192/96` |
| 抽头并行 9 × 12 | 108 | **16** | `9·192/108` |

⇒ **两个"理想结构"相差 12.5%（= 9/8，因为 k² 比 8 稍大），而它们相对 HLS 的收益恰好是 `M` 的倍数 × `II`。** 换句话说：**"借 FINN 提速"这个动机不成立；能提速的是"把 II 从 2 降到 1" + "多放乘法器"这两件事，与 SWG 用哪种风格无关。**

（FINN default 的 216 拍/像素 = `k²·cf`，`gen:167-175`；它每个元素 8 通道，所以折成"每 oc 拍数"就追平了。同理 k=5：HLS `25·16/25×3 = 48` 拍/oc，FINN default `25·16/25 = 16`，手写 100 DSP `25·16/100 = 4`。）

**那 FINN default 真正的问题是什么？** 只剩两条，而且都在 §2.6 / §4.3 里：

1. **缓冲**：C8 布局下窗口跨 `N_g` 个通道平面 ⇒ 单个环形缓冲 1.18 MB（不可行）。
2. **padding**：`gen:355` / `gen:534` 硬写 `pad=[0,0,0,0]`，必须前置一个 FMPadding 节点。

### 3.3ter 那"列口数不足"到底解决了没有？

**解决了，但解决它的不是 FINN，而是"每行 1 个读口 + 横向移位链"这一个拓扑选择。**

- 现状：HLS 每拍要 3 个列地址、每行 1 个读口 ⇒ `II = ⌈3/2⌉ = 2`（`04:208-215`）。
- 链式：每拍每行只读 1 个新列 ⇒ `II = 1`。
- FINN 的 **parallel** 风格正是这个拓扑（官方文档："avoids memory port limitations"，§4.1），**但它不能被我们直接实例化**（§4.2 的 `SIMD==C` 断言）。
- 所以收益（II 2→1，即 2×）**是拓扑带来的，不是代码带来的**；而拓扑我们自己早在 `04_mac_array.md:502-516` 就写下来了，并且已经在 `conv_k5_engine.v:309-372` 落地。

### 3.4 资源估算（vs 现在 HLS 的 40 DSP + 32 LUT 乘法器）

只算**窗口/行缓冲**这一块（乘法器阵列与 k=1 同构，不在本次评估范围）：

**方案 A：FINN 并行拓扑（`(k_h−1)` 行延迟线 + `k_h` 条 `k_w` 深寄存器链），N_g 个通道组全并行**

存储 = `N_g · ((k_h−1)·W' + k_w)` 字，`W' = W + 2·PAD`：

| 层 | N_g | W' | 链深/组 | 总字 | bit | BRAM18（理想紧排） |
|---|---:|---:|---:|---:|---:|---:|
| `decoder.preconv_1` | 14 | 322 | 647 | 9,058 | 579,712 | **31.5** |
| `decoder.preconv_2` | 20 | 162 | 327 | 6,540 | 418,560 | **22.7** |
| `decoder.preconv_3` | 24 | 82 | 167 | 4,008 | 256,512 | **13.9** |
| `encoder.encoder_2.down` | 12 | 162 | 327 | 3,924 | 251,136 | **13.6** |
| 中位数（114 层） | — | — | — | — | 21,376 | **1.2** |

- 每个组要**独立读写口**（各 1R1W），不能拼成一个大 RAM → 实际按组打包：64 bit 宽 × 1024 深 = 每组 4 个 BRAM18（或 2 个 BRAM36）⇒ 最坏层 14 × 4 = **56 块 BRAM18**（理想紧排只要 31.5，差在"每组的 647 深只用到 1024 的 63%"）。**这是唯一一处需要决策的地方**：56 比 SPEC 的 40 上限高 40%，得靠"按层换形态 / 降组并行度"来压。
- 寄存器链部分：`k_h·k_w` 个字/组 × `N_g` → 最坏层 `3·3·14 = 126` 字 × 64 bit = **8,064 FF**（若照 FINN 的 `k_w=3` 深，就是 `3·3`；用 1 级 bypass 可降到 2 深）。

**方案 B：我们 k=5 引擎的拓扑（`k_h` 条整行 BRAM）**

存储 = `k_h · W' · N_g` 字：

| 层 | 字 | bit | BRAM18 |
|---|---:|---:|---:|
| `decoder.preconv_1` | 3·322·14 = 13,524 | 865,536 | **47.0** |
| `decoder.preconv_2` | 3·162·20 = 9,720 | 622,080 | **33.8** |
| `decoder.preconv_3` | 3·82·24 = 5,904 | 377,856 | **20.5** |

**对比 HLS 现况**：`line_buf` = 7 bank × 26752×8（`06:238`）→ **112 块 BRAM18**（`06:241` 对账）。那是为 k=5 的 7 行 + 352 通道一次做足。我们按层做 3 行 → 20–47 块，**省 58%–82%**；用 FINN 的链式拓扑再省 1/3（→ 14–31 块）。

- **乘法器侧没法省**：k=3 的 `40 DSP + 32 LUT 乘`（`04:47`）是"8 oc × 5 DSP + 4 LUT"，是 HLS 的宽度选择，与窗口实现无关。窗口改成链式后**同样的 72 个乘法器可以从 36 MAC/拍 提到 72 MAC/拍**（II 2→1），这才是收益所在。
- **一处新的资源压力（FINN 帮不上）**：k=1 引擎把权重钉在 LUTRAM（32 lane × 44 × 64 bit = 90 kbit，`conv_k1_engine.v:484`）。k=3 每 lane 要 **9 抽头 × cin 字节**，`cin=192` 时 12 lane × 1728 B = **166 kbit**，LUTRAM 装不下（≈2 万 LUT6）→ 必须进 BRAM 或改多 pass。**这是 k=3 引擎独立于窗口方案的待定项，与 FINN 无关，但会吃掉 §3.4 算出来的 BRAM 余量。**

---

## 4. `swg_template_parallel` 的"并行风格"能否照搬到 C8

### 4.1 `window_buffer` 到底是什么（读 59-216 行 + 生成器）

模板里 `window_buffer` 是**一个生成的实例**（`swg_template_parallel.sv:90-104`），内容由 4 段 Jinja 拼接（`:47-55`）。**真正的结构在生成器里**（`gen:648-694`，这是决定性证据）：

```
px_idx = 0
for ky in range(k_h):                     # ← 每行一条"寄存器 FIFO"
    reg_fifo = []
    for kx in range(k_w):
        for c in range(cf):
            if c < cf-1:
                if not (ky==0 and kx==0): reg_fifo.append(-1)   # 不输出（同像素后续 beat）
            else:
                reg_fifo.append(px_idx)                          # ← 这个位置就是一个抽头
        if kx < k_w-1: reg_fifo.extend([-1]*((dil_w-1)*cf))      # dilation 空位
    reg_fifos.append(reg_fifo)
    if ky < k_h-1:                        # ← 每行后面接一条"RAM FIFO"（= 行缓冲）
        line_buffer_len = ((w-kernel_width) + w*(dil_h-1))*cf
        bram_fifos_depth.append(line_buffer_len)
```

串起来（`gen:772-791`）：

```
data_in → [reg_fifo_0 (k_w)] → [bram_fifo_0 (W−k_w)] → [reg_fifo_1 (k_w)] → [bram_fifo_1] → … → [reg_fifo_{k_h−1}]
                 ↑9 个抽头全从这里按常数下标取
```

**回答：既不是"寄存器阵列"也不是"行缓冲 + mux"，而是一条"寄存器 FIFO / RAM FIFO 交替的级联延迟线"**，总共恰好 `buffer_min_size = (k_h−1)·W + k_w` 个元素（`gen:546`）。

**零 mux 的证据**：输出映射是**生成期算好的常数下标**（`gen:726-741`）：

```python
data_out[OUT_ELEM_WIDTH*{out_idx}+:OUT_ELEM_WIDTH]      # out_idx 是常量
  = reg_fifo_{fifo_id}[{access_idx}*{mmv}*OUT_ELEM_WIDTH + ... +: OUT_ELEM_WIDTH]
```
```systemverilog
assign data_out[e*WIDTH +: WIDTH] = Data[e];   // swg_reg_buffer, swg_common.sv:193-194
```

`access_idx`/`mmv_idx`/`out_idx` 全是 Python 整数常量 → **纯接线，0 个 LUT**。这正是我们 `04_mac_array.md:474-489` 第 2 条硬规则（"bank 索引必须是常量，否则长出 16:1 mux"）的寄存器版。

### 4.2 在 C8 下"窗口并行"意味着什么

| 问题 | 回答 |
|---|---|
| 是"并 8 字节组"还是"并像素"？ | **并像素**。8 个通道被**装在一个元素里**（1 个 64-bit 字 = 1 个 (y,x) 的 8 通道）。所以 `BIT_WIDTH=8, SIMD=8, MMV_IN=1`，窗口并行度 = `MMV_OUT = k_h·k_w = 9`，总线 `8×8×9 = 576 bit`。 |
| 和 FINN 的 `SIMD` 概念一致吗？ | **在"单通道组"口径下完全一致**。FINN 全并行配置是 `SIMD = C_in·K`（官方文档 §Folding "SIMD = MW = C_in * K"）；取 `C_in=8`（一个 C8 组）、`K=9` → 576 bit，和我们一模一样。 |
| 那 24 个通道组呢？ | **FINN 没有这个维度**。它的并行风格**硬性要求 `SIMD == IFMChannels`**（`gen:817`：`assert ifm_ch == simd, "Constraint violated: SIMD must be equal to IFMChannels"`），也就是"一个元素装完一个像素的所有通道"。我们 `cin=192` 时一个像素是 **24 个字**，永远满足不了。官网折叠表里 `M>1` 的那一行写的是 **"Currently unsupported"**（<https://finn.readthedocs.io/en/latest/components/rtl-swg.html>，"Parallelism configurations" 表最后一行）。 |
| `channel_factor>1` 的并行风格支持吗？ | 生成器里 `loop_kh_iterations = channel_factor`（`gen:567`）说明它**能**把"同像素的多个 beat"塞进 KH 计数器，但 `gen:817` 的断言在非 depthwise 分支把这条路封死了。depthwise 分支（`gen:428-446`）允许 `cf>1`，代价是输出是 depthwise 交错布局。**对我们的用途不可用（存疑：我没有跑 FINN 验证这一点，只是读断言）。** |

### 4.3 照搬到 C8 需要新增什么

| # | 新增 | 说明 | 量级 |
|---|---|---|---|
| 1 | **通道组维度**：`N_g` 条并行链 | 把 `gen:648-694` 的循环套一层 `for g in range(N_g)` | 生成器 10 行；RTL 由 genvar 展开 |
| 2 | **组轮转计数** | 每拍消费 `g = t mod N_g`，因为累加器按输出像素组织，不能按组分批（§4.4） | 1 个计数器 |
| 3 | **抽头选择** | 从 `N_g×9` 个寄存器里选当拍的 9 个。**这是唯一新增的组合逻辑** | 全并行：64 bit × 9 × `N_g`:1 ≈ **5 k LUT6**（`N_g=24`，24:1 约 9 LUT6/bit）。**可压到 0.6–1.5 k**：同一组的 9 个抽头在它那 8 个通道拍里是静止的，把「选一次、用 8 拍」时分复用即可（需双缓冲，**未验证**）|
| 4 | **内联 padding** | FINN 完全没有（`gen:355` / `gen:534`：`pad=[0,0,0,0]  # padding happens in separate padding node for now`）。我们必须加"行有效 + 列有效 → 强制 0" | 我们 k=5 已有：`conv_k5_engine.v:354-359` |
| 5 | **复位** | FINN 的链/行缓冲无复位，靠 `FIRST_WRITE_ELEM = buffer_min_size−1`（`gen:590`）丢掉前 N 个输出。我们要求 `ap_rst_n` 干净复位 | 每条链加复位或搬 priming 逻辑 |
| 6 | **接口** | FINN 是 AXI-Stream（`swg_template_parallel.sv:59-80`），我们是 AXI4 master + `ctrl_*`（`00_SPEC` §3） | 外壳全丢，只留内核 |

**能省掉多少 mux？** 相对"用 RAM 读 3 列"的方案：横向 3 抽头由 **0 个 mux** 提供（对比 HLS 的 20 个 `sparsemux_15_3_8`，`06:194`）。但相对**我们自己 k=5 引擎的写法**（也是显式移位寄存器），**省 0**——因为我们已经是 0 个 mux 了。真正的代价反而是**新增**了第 3 项那个 `N_g:1` 选择器。

### 4.4 一个必须说清的架构后果

"组"为什么不能像 FINN 那样当最外层循环、一组一组地跑？

因为**累加器按输出像素组织**：输出像素 `(y,x)` 的累加要跨 `(g, ky, kx)` 全部 216 个乘积（`= N_g·9·8`）。若 `g` 在最外层，就要么

- 把 `H·W·⌈cout/12⌉` 个 26-bit 部分和存起来（preconv_2：`160·160·6·26 bit = 4.0 Mbit`，**不可行**），
- 要么一趟一趟往 DDR 读改写部分和（`N_g` 趟，**DDR 流量 ×20**，我们本来就是 DDR-bound，`04:241` 记过"16.3 拍/字节"）。

⇒ **所有 `N_g` 个组必须同时在片**。这就是 §3.4 那 31.5–56 块 BRAM 的来历（V1 的 1.18 MB 是另一回事：那是「按内存序连续寻址」才需要的），也是 FINN 的「单元素装完一个像素所有通道」这一假设在我们这里必须被打破的原因。

---

## 5. C8 适配方案（如果真要做，长什么样）

### 5.1 三个候选

| | 视图 | 结构 | 窗口/行缓冲 | 缓冲读拍/像素 | 结论 |
|---|---|---|---|---|---|
| **V1** | 整张特征图作为一个 FINN 流 | default 风格 + C8 增量 | 单环形缓冲 **1.18 MB** | 216（每元素 = 8 通道 × 1 抽头） | ❌ 缓冲不可行（§2.6） |
| **V2a** | 一个通道组一趟（24 趟） | 并行风格，逐字复用 FINN | 167 字/组（**能跑**） | 9 × 24 = 216 | ❌ 要 24 趟部分和（§4.4） |
| **V2b** | `N_g` 条链全并行 + 组轮转 | 并行风格 × N_g | 4,008 字 = 257 kbit | **192**（每拍 1 通道 × 9 抽头） | ✅ 唯一可行（= 我们 k=5 引擎的拓扑 + FINN 的链式省 RAM） |

> **注意两件事**：(1) 三种视图的 **DDR 流量都是 1×**（每个输入元素只从 DDR 读一次、写进缓冲一次）；差别在**缓冲大小**和**缓冲读拍数**。(2) V1/V2a 每拍只出 1 个抽头（靠 8 通道并行补吞吐，§3.3bis），V2b 每拍出 9 个抽头（靠 9 抽头并行补）。在乘法器数相同时两者吞吐相差 ≤12.5%。

**推荐 V2b**，且它的落地形态就是（下图是 k=5 引擎那种「3 整行」写法的推广；若再借 FINN 的链式省 RAM，可把最老那一行换成 `(W−k_w)` 深的延迟线，省约 1/3 存储，见 §3.4 方案 A vs B）：

```
DDR ──AXI4──► 行缓冲（3 行 × (W+2PAD) × N_g 字，BRAM）
                    │  每拍 3 个读（3 行同列，1 口/行）
                    ▼
             3 × 3 深横向移位链（每行 3 个字 = 6 个 64-bit 寄存器）  ← swg_reg_buffer 的位置
                    │  9 个抽头，常数下标，零 mux
                    ▼
             组轮转：第 t 拍用 g = t mod N_g 的那 9 个字（N_g:1 选择，~5 k LUT；时分复用可压到 ~1 k，§4.3）
                    │
                    ▼
             9 抽头 × L 个 oc 车道 = 9L 个 8×8 乘法器（L=12 → 108 DSP ≤ 120）
```

每拍消耗 **1 个字节**（= 1 组 × 1 个通道 × 9 个抽头同时用）、做 `9L` 个 MAC：`L=12` 时 **108 MAC/拍**，对比 HLS 的 36（`04:524`）。以 `decoder.preconv_3` 为例：6400 像素 × 24 组 × 8 通道 = 1.23 M 拍/oc-pass，cout=64 → 6 个 pass → 7.4 M 拍 ≈ **49 ms @150 MHz**（输入只需 24 字/像素，即特征图读一遍）。

**我建议：先只做 `V2b` 的窗口单元（等价于把 `conv_k5_engine.v:309-372` 从 5 改 3 + 加组轮转），不要引入 FINN 代码。** 理由见 §6/§7。

### 5.2 与既有约束的对齐检查

| 约束 | 出处 | 本方案 |
|---|---|---|
| BD module reference 顶层**不能**是 SystemVerilog | `BD集成说明.md:93-98`、`:416`、`:571` | 适配层保持 Verilog-2001（沿用 `k1_axi_wrap_bd.v`）；内核若是 SV 可以 |
| `import swg::*` 需要 package | `swg_common.sv:35` | **若抄 FINN 就要多引一个 package 进 `sources_1`**；Vivado 能处理，但这是新增的编译依赖（**存疑**，需 10 分钟 xvlog 实测） |
| `automatic` 变量在 `always_ff` 内 | `swg_template_default.sv:181-182`、`:212-213` | 我们的 SV 代码里**没有这个用法**；Vivado 2025.2 大概率支持，但属于"没试过的东西" |
| 模块作用域 `for (genvar …)` | `swg_common.sv:193` | SV-2009 隐式 generate；我们的代码风格不用。**存疑** |

---

## 6. 工作量与风险

### 6.1 从零写要多久 / 借用能省多少

**基准事实**：我们**已经有 k=5 引擎**（1,349 行，25 抽头 × 4 oc = 100 DSP，5×5 窗口移位寄存器，II=1，`conv_k5_engine.md` §0）。k=3 的窗口/行缓冲 = **它去掉 2 行/2 列**。

| 工作项 | 从零/改 k=5 | 借 FINN | 差额 |
|---|---|---|---|
| 窗口/行缓冲本体 | **0.5 天**（改 `k_h`、`LB_DEPTH`、`sel[]` 三个常数 + 少 2 条 bank） | 1 天（抄 2 个原语，但 `ram_buffer` 无复位要加、要写 `N_g` 条 genvar 展开） | **−0.5 天** |
| 地址/流控（含 pad、stride、组轮转、尾帧） | 1 天（改 k=5 的状态机） | 1.5 天（先读懂 FINN 265 行密集 FSM，再把它改成 C8 + 内联 pad + 组轮转——改别人的 FSM 比改自己的贵） | **−0.5 天** |
| 9 抽头数据通路 + PAD12 权重 + 累加 | 4 天 | 4 天 | 0 |
| 集成 / BD / 上板 / 验收 | 5 天 | 5.5 天（多 SV+package 风险项） | −0.5 天 |
| 合规（NOTICE / vendor 目录 / 修改清单） | 0 | 0.5 天 | −0.5 天 |
| **合计** | **≈10.5 天** | **≈12.5 天** | **借 = 净亏 2 天** |

> 另一种口径：**纯"窗口/行缓冲"这一个模块**，从零 0.5–2 天（有 k=5 参考 → 0.5 天）。**借用省下的是这 0.5–2 天里的一小部分，却要付 1.5–3 天适配。**
>
> 反过来说：**如果我手上没有 k=5 引擎、也没有 04/06 两篇的列口分析**，从零写一个 pad=1/k=3/C8 的窗口单元我会估 **3–4 天**（含 xsim 边界用例），那时借 FINN 的拓扑能省 1–1.5 天——但前提是 FINN 得支持内联 padding，而它不支持。

### 6.2 我们踩过的 4 个坑 vs FINN 代码（逐条）

| 我们的坑（证据） | FINN 代码里有没有？ | 结论 |
|---|---|---|
| **多驱动 net**（`conv_k1_engine.md:358-367`：`px_sh`/`pix_ready` 两个 always 写同一 reg，xsim 全过、Vivado 报 `Synth 8-6859`） | **没有**。`swg_common.sv` 里 `Ram`/`Out`/`Data`/`Out_reg` 各自只有一个 `always_ff` 写点（`L167-170`、`L196-201`、`L227-247`）；`data_out` 由 genvar 常量下标赋值（`L193-194`） | ✅ 这块**比我们自己写得更规矩** |
| **RAM 推断失败**（`conv_k1_engine.md:374-381`：RAM 读寄存器被两个源写 → 退化成 16,384 FF + 256:1 mux） | **没有**。`swg_ram_buffer:234` `Out_reg <= Ram[Addr_r]` 是唯一赋值源；`swg_cyclic_buffer_addressable:168` 同理。都是标准 1R1W 模板 | ✅ 但注意 `swg_reg_buffer` 是**纯 FF 移位**，深度大了会被推断成 SRL（无复位），不是 RAM |
| **`[expr +: W]` 自决定位宽**（`conv_k1_engine.md:411-422`） | **没有**。所有 `+:` 的基址都是常量或 genvar（`swg_common.sv:194`、`gen:735`） | ✅ |
| **BD module reference 不支持 SV 顶层**（`BD集成说明.md:93-98`） | **是风险**：`.sv` + package + `automatic` + 隐式 genvar。只能作为内核放进 SV fileset，顶层仍是 Verilog-2001 适配层 | ⚠️ 新增 1 个编译依赖 |
| **无 bypass 的同步 RAM**（`conv_k1_engine.md:369-372`：cin=8 单 beat 时同拍写读拿到旧值） | **有**：`swg_cyclic_buffer_addressable:167-170` 是 read-first 无 bypass；`swg_ram_buffer:234` 读的永远是 `DEPTH` 拍前的数据 | ⚠️ 抄过来要自己补旁路，或者接受固定延迟对齐 |
| **无复位的链**（我们要求 `ap_rst_n` 干净复位） | **有**：`swg_reg_buffer` 没有 `rst_n`（`L182`）、`swg_ram_buffer` 只复位指针（`L228-231`）、`swg_cyclic_buffer_addressable` 没有复位。正确性靠 `FIRST_WRITE_ELEM`（`gen:590`）丢掉前 `buffer_min_size−1` 个输出 | ⚠️ **这是最大的集成摩擦点**：等于要在我们的状态机里复刻一套"预热丢弃"，或者给 BRAM 写一遍零（一次全缓冲写 = 浪费 ~4000 拍/层，可接受但要显式做） |

### 6.3 引入 FINN 的额外成本（不是技术风险，是流程风险）

1. **上游会变**：模板是 Jinja + `$占位符$`，生成器行号每月都可能漂（§1.1 我记录的是 2026-10-26 的 main）。抄进 repo 后要像 `vendor/README.md` 那样维护"我们改过的 N 处"。
2. **口径翻译成本**：`SIMD / MMV_IN / MMV_OUT / M / channel_factor / ELEM_PER_WINDOW / INNERMOST_STATE` 这一套词要映射到我们的 `g / ic / tap / lane`，两次翻译之间极易差一（我这次就靠逐拍复刻才把"外层状态周期也读一个元素"这件事确认下来，`§2.4`）。
3. **调试责任转移**：如果窗口单元里出 bug，我们得先证明不是 FINN 的语义没抄对，才能查自己的代码。我们 k=5 的窗口单元出问题只需看自己的 `sel[]`/`lb_valid`。

---

## 7. 合规（BSD-3-Clause）

### 7.1 许可证事实

- 仓库级：`Xilinx/finn` 的 `LICENSE.txt` = **BSD-3-Clause**（`Copyright (C) 2020-2022 Xilinx, Inc.` / `2022-2024 Advanced Micro Devices, Inc.`），第三条是 *"Neither the name of FINN nor the names of its contributors may be used to endorse…"*。
- 文件级：我们本地这 4 个 `.sv` **每个文件头都自带完整 BSD-3 文本**（`swg_common.sv:1-30`、`swg_pkg.sv:1-30`、`swg_template_parallel.sv:1-30`、`swg_template_default.sv:1-30`），措辞是标准 BSD-3 第三条（*"Neither the name of the copyright holder nor the names of its contributors…"*）。
- 差异说明：文件头没有 `SPDX-License-Identifier`，且 `finn-rtllib/swg/` 目录下**没有独立的 LICENSE 文件**（我列过上游目录）。**⇒ 以每个文件头的文本为准。**
- 结论：**可以商用**（BSD-3 允许闭源商用、允许改、允许只发二进制）。

### 7.2 三条义务 → 我们的落地做法

| 义务 | 触发条件 | 我们的做法 |
|---|---|---|
| ① 保留版权声明 + 条件 + 免责声明 | **分发源码**时 | 若把 `.sv` 放进本 repo：**原文件头一个字都不改**（`swg_common.sv:1-30` 整段保留），修改处另加注释 |
| ② 在文档/材料里复现声明 | **只发二进制**（bitstream / 产品）时 | 在交付文档里放一段"本设计包含源自 AMD/Xilinx FINN 的代码，BSD-3-Clause，全文如下…" |
| ③ 不得用作者/机构名字背书 | 任何宣传 | 只写"包含源自 FINN 的代码（BSD-3）"，**不写**"基于 Xilinx FINN""与 AMD 合作"这类暗示背书的措辞 |

**具体落地（沿用本仓库已有的 `vendor/` 惯例）**：

```
vendor/finn_swg/
  README.md        # 来源 URL + 快照日期 + 逐文件"我们改过的 N 处"表（照 vendor/README.md 的格式）
  swg_common.sv    # 原样，文件头保留
  LICENSE.txt      # FINN 的 LICENSE.txt 原文
```
- 理由：`vendor/README.md` 已经确立了"哪些是抄的、改过哪几处"的规范（`vendor/PDNet/LICENSE.md` 也是这么留的）。**不要**把抄来的 `.sv` 放进 `pynq_specialized_code/pl/rtl_hand/modules/`——那里是"我们自己的模块"（`00_SPEC` §1）。
- **在文档里引用结构/结论（不抄代码）不需要任何义务**，但我建议仍然在本文和 k=3 文档里写一句出处（学术诚信 + 将来追溯）。

### 7.3 存疑（需法务/主控确认）

1. **bitstream 算不算 "binary form"**：BSD-3 第二条针对 "binary form" 分发。综合后的 bitstream 是否落入该条，法律上无统一判例。**保守做法：产品文档里带上声明，成本几乎为零。**
2. **BSD-3 无专利授权条款**（对比 Apache-2.0）。若将来有人主张 FINN 相关专利，BSD-3 不提供明示授权。**这条对"抄 50 行 FIFO"风险可忽略，若大规模复用则需评估。**
3. 我们 repo 目前**没有 LICENSE / NOTICE 文件**。引入第三方代码前建议先定"repo 自己的许可证"，否则"保留声明"放在哪、以什么名义发，都是空的。

---

## 8. 最终建议

### 8.1 分档判定（逐模块，不含糊）

| 模块 | 档 | 依据 |
|---|---|---|
| `swg_pkg.sv` | **A** | 6 个枚举、零依赖（§0 表） |
| `swg_controller` | **B（技术可行性）/ C（对 k=3 的用途）** | 能实例化、参数能算（§2.5），但 C8 下缓冲要 1.18 MB（§2.6-2.7） |
| `swg_reg_buffer` | **A** | 27 行、零 mux、纯 FF（§3.1） |
| `swg_ram_buffer` | **B** | 可抄，但要加复位/bypass（§6.2） |
| `swg_cyclic_buffer_addressable` | **C** | 1 读口 = 瓶颈本体（§3.3） |
| `swg_template_default` | **C** | 与我们内存序相反（⇒ 1.18 MB）+ 无 padding；吞吐本身不吃亏（§2.6、§3.3bis） |
| `swg_template_parallel` | **B** | 拓扑对，但 `SIMD==C` 断言 + 无 padding + 无组维度（§4） |
| 生成器 `.py` | **C（不复用）/ A（当参考手册）** | §0 表 |

### 8.2 明确回答任务书的三个问题

1. **"能不能直接复用？"** —— **不能。** 不是"需要改造"这种模糊答案：`default` 风格在 C8 下需要 1.18 MB 缓冲（原理性不可行），`parallel` 风格要求"一个元素装完一个像素的全部通道"（`gen:817` 断言），我们 `cin=192` 时是 24 个字/像素，永远不满足。**能复用的只有 2 个 FIFO 原语（71 行），而这两个我们已经有等价实现且更适合。**

2. **"从零写 k=3 的窗口/行缓冲要多久？借用能省多少？"** ——
   - 有 k=5 引擎 + 04/06 文档在手：**0.5 天**（改 `k_h`/`LB_DEPTH`/`sel[]`）。
   - 什么都没有、从零设计：**3–4 天**（含 pad 边界 + xsim 用例）。
   - 借 FINN：本体省 0.5 天，但适配（组维度 + 内联 pad + 复位/priming + SV package）**净亏 1.5–3 天**。
   - **⇒ 省不到东西，反而更贵。**

3. **"值不值得用？"** —— **不值得。** 明确说不值得，理由三条：
   - (a) **能力缺口是原理性的**（内存序 + 单元素装全通道），不是参数没调对；
   - (b) **我们已经走得更远**：k=5 引擎有内联 padding、C8 128-bit 行缓冲、`ap_rst_n` 干净复位——抄 FINN 等于**倒退**；
   - (c) **唯一的战略价值（"列口数是 II 的根因"）已经吸收**：FINN 官方文档那句话现在是 `06_k5_and_primitives.md` §1.5 和 `04_mac_array.md` §6.5 结论的**外部佐证**，这比抄 71 行代码值钱。

### 8.3 建议的动作（按优先级）

| # | 动作 | 谁 | 成本 |
|---|---|---|---|
| 1 | **不引入 FINN RTL**。k=3 引擎的窗口/行缓冲按 `V2b` 做（k=5 的 3 行特例 + 组轮转） | k=3 owner | 0.5 天 |
| 2 | 把本文 §2.3（8 个增量的公式）、§3.3（官方文档的"avoids memory port limitations"）、§2.6（C8 vs HWC 的缓冲差 37 倍：147,362 vs 4,008 字）**回填到 04/06 两篇**，作为"为什么必须走移位链"的外部证据 | 主控 | 0.2 天 |
| 3 | 若将来真的要写"通用 SWG"（支持任意 k/stride/dilation/形状），**那时**再以 `gen:379-394` 的 8 个公式为蓝本自己写，并保留 `vendor/finn_swg/` + NOTICE 的姿势 | 未来 | — |
| 4 | **不要**在文档/宣传里写"基于 Xilinx FINN 加速"（§7.2 第三条） | 全员 | 0 |

### 8.4 本次评估的两处自我限制

1. **没有编译/综合/仿真**（任务书要求）。所以"FINN 在我们工具链上能不能过 xvlog / 能不能推断出 BRAM"**我没有实测**，§5.2 的 3 条标了「存疑」。
2. `swg_template_default.sv` 的 `stride>1` 行为（我们那 2 个 stride=2 层要用）**我没有完全读通**：并行风格下 `reg_fifo` 只插了 `channel_factor` 与 `dilation` 的占位（`gen:655-694`），**没有 stride 占位**；stride 是靠 `advance` 的三种触发方式（`swg_template_parallel.sv:152`）跳过某些拍的。要断定"stride=2 时抽头如何对齐"必须读那 4 个 handshake 信号的时序或跑 xsim ——**标「存疑」，不作为结论使用**。

---

## 附录 A · 关键行号速查

| 内容 | 位置 |
|---|---|
| `swg_controller` 参数表 | `swg_common.sv:34-63` |
| 5 个计数器（初值 = `LOOP_x`） | `swg_common.sv:69-73` |
| `addr_incr` 的 5 选 1（按 State） | `swg_common.sv:76-85` |
| `tail_incr` 的 3 选 1（按计数器） | `swg_common.sv:88-93` |
| 下状态逻辑（外层只停 1 拍） | `swg_common.sv:96-109` |
| 计数器递减（仅最内层） | `swg_common.sv:112-144` |
| 环形缓冲（1R1W、绝对地址、同步读） | `swg_common.sv:149-173` |
| 寄存器 FIFO（零 mux 并行输出） | `swg_common.sv:176-202`（输出映射 `:193-194`） |
| RAM FIFO（1R1W 延迟线） | `swg_common.sv:205-248`（读 `:234`） |
| default 模板：单缓冲实例 | `swg_template_default.sv:65-79` |
| default 模板：读地址 + 回卷 | `swg_template_default.sv:212-216` |
| default 模板：窗口位置回卷 | `swg_template_default.sv:219-221` |
| default 模板：`tail_incr` 生效点 | `swg_template_default.sv:224` |
| parallel 模板：生成器占位符 | `swg_template_parallel.sv:47-55` |
| parallel 模板：`addr_incr` 只用于记账 | `swg_template_parallel.sv:211` |
| parallel 模板：shift/advance | `swg_template_parallel.sv:152-156` |
| 生成器：8 个增量公式 | `gen:379-394` |
| 生成器：缓冲下界 / 步长 | `gen:367`, `gen:387-394` |
| 生成器：`LOOP_x = trip−2` | `gen:479-483`、`gen:606-610` |
| 生成器：`INNERMOST_STATE` 选择 | `gen:455-464`、`gen:573-579` |
| 生成器：断言 `abs(incr) > buffer` | `gen:406-412` |
| 生成器：**padding 不在 SWG 里** | `gen:355`、`gen:534` |
| 生成器：并行风格的缓冲分区（reg/ram FIFO） | `gen:648-694` |
| 生成器：输出映射（常数下标 = 零 mux） | `gen:726-741` |
| 生成器：`assert ifm_ch == simd` | `gen:812-818` |
| 生成器：每像素拍数 `exp_cycles` | `gen:124-176` |
| 我们的 k=5 窗口/行缓冲（现成实现） | `conv_k5_engine.v:309-372`（内联 pad `:354-359`） |
| 我们的列口分析 | `06_k5_and_primitives.md:106-129`；`04_mac_array.md:189-219`、`:502-516` |
| 我们踩过的 4 个坑 | `conv_k1_engine.md:358-381`、`:411-422`；`BD集成说明.md:93-98` |
| FINN 官方文档（并行风格"avoids memory port limitations"） | <https://finn.readthedocs.io/en/latest/components/rtl-swg.html> |
| 上游模板目录（另有 4 个本地没有的外壳文件） | <https://github.com/Xilinx/finn/tree/main/finn-rtllib/swg> |

## 附录 B · 复现方法

```bash
# 1) 参数语义：读生成器（本文所有 gen: 行号基于此文件）
#    /data/work/finn_ref/convolutioninputgenerator_rtl.py  (2026-10-26 main 快照, 1014 行)
#    关键函数：get_exp_cycles / prepare_codegen_default / prepare_codegen_parallel / select_impl_style

# 2) 状态机逐拍验证（我写的复刻件，纯 Python，不是仿真）
python3 /data/work/finn_ref/_swg_controller_model.py   # 内含 gen_default()/sim()，可与 im2col 序列比对

# 3) 层形状（本文 §1.3 全部来自这个文件）
#    runs/prof20261023_grid/layers_model_final.json  (247 层, 含 k/s/H/W/cin/cout)
```

> 复核提示：`gen:406-412` 那条断言（`abs(addr_incr_end_window) > buffer_actual_size → "try setting parallel_window=1"`）是 FINN 自己写的"缓冲装不下就换并行风格"的提示。**本文 §2.6 的结论等价于：在 C8 布局下这条断言挡不住我们（因为我们可以把 buffer 设成 1.18 MB 让它通过），但那个 buffer 谁也放不下。**
