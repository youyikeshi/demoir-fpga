# 多 bin_unit 并行 BD 改造方案（W1-F 交付）

> 生成时间：2026-10-30
> 范围：**只读** `vivado/proj_engine`，全部改动落在副本 `vivado/proj_multi`
> 状态：脚本已写完 + 干跑通过 + **未跑 Vivado**（按铁律，等 W0-A/W2-H 完成后统一构建）

---

## 0. 结论先行（三条与任务书不符的硬事实）

| # | 任务书说 | 实测/核算 | 影响 |
|---|---|---|---|
| **①** | 「当前 LUT 12,570 / 46,200」 | **全设计 38,360 / 46,200 = 83.03%**（`proj_engine/verify_synth_util.rpt`，2026-10-30 09:07）。12,570 是 `board/WORKING_v1_20261030` 那份**旧冻结版**的数字 | 资源账完全变了，见 §8.1 |
| **②** | 「一个 bin_unit ≈ 7,500 LUT；4×7500 + 5000(conv) = 35,000 = 76%」 | **`conv_uni_0` 单实例 OOC 就吃 25,694 LUT（55.61%）**，不是 5,000 | 4 实例**物理上不可能**（除非砍掉 conv_uni） |
| **③** | 「bin_unit 的 m_axi → `S_AXI_HP1`」 | 实测 `.bd`：**bin_unit_0 → `pc_conv_bin` → `ps7/S_AXI_HP0`**；conv_uni_0 → `pc_conv_uni` → `S_AXI_HP1` | 端口规划和任务书反了，见 §5 |

额外两条好消息（省掉了任务书里预判的两个难点）：

- **`axi_ctrl` 不用扩容**。它已经是 `axi_interconnect 2.1 / NUM_MI=5`，其中 `M00/M01/M04` **本来就是空闲的**（`proj_engine` 已验证通过）。挂 3 个新 slave 不需要动互联。
- **不需要 4→1 汇聚**。Zynq-7000 有 `S_AXI_HP0..HP3` 四个口，`PCW_USE_S_AXI_HP2/HP3` 当前为 0，直接打开即可。**每个引擎独占一个 HP 口 ⇒ ARID 不经互联、零重映射风险**，比任务书设想的汇聚方案安全得多。

---

## 1. 现状核对（照抄实测，不是从文档抄的）

```
ps7 (xc7z015clg485-2, FCLK_CLK0 = 150 MHz)
├── M_AXI_GP0 → axi_ctrl (axi_interconnect 2.1, NUM_SI=1, NUM_MI=5)
│                 s00_couplers/auto_pc : AXI3 → AXI4LITE  （整条控制通路是 AXI4-Lite 32bit）
│                 M02 → conv_uni_0/s_axi       @0x40040000
│                 M03 → bin_unit_0/s_axi       @0x40060000
│                 M00 / M01 / M04              ← **空闲**
├── S_AXI_HP0 ← pc_conv_bin (AXI4 64bit → AXI3 64bit, ID 4bit) ← bin_unit_0/m_axi
└── S_AXI_HP1 ← pc_conv_uni (同上)                             ← conv_uni_0/m_axi
```

`bin_unit_0` 解析出来的接口形状（从 `.hwh` 逐引脚数出来的，这是**唯一可信的事实**）：

| 接口 | 引脚数 | 关键点 |
|---|---:|---|
| `s_axi` | **19** | 只有 `awaddr/awvalid/awready/wdata/wstrb/...`，**没有** `awlen/awburst/awprot/awid` ⇒ **AXI4-Lite 32bit** |
| `m_axi` | **21** | 有 `arlen/awlen/arid/rid/rlast/wlast`，**没有 `awid`/`prot`/`cache`** ⇒ **AXI4 64bit, ID=4** |

生成网表 `design_1.v` 佐证：`wire [7:0] bin_unit_0_m_axi1_ARLEN; wire [3:0] ..._ARID; wire [63:0] ..._RDATA;`（无 AWID）。

> **新增实例必须同时满足 19 / 21 这两个数。** 脚本在 `create_bd_cell` 之后、`assign_bd_address` 之前、
> 以及最终 `.hwh` 上各查一遍（§7 有 3 道闸）。

---

## 2. 并行策略：按通道 vs 按空间

用 `runs/perf114x/data/net_per_layer.csv` 的 227 层真实形状（`cin/cout/k/H/W`），换算到 256×256（H、W 各 ×4，面积 ×16）逐层累加。`s≡1`。

### 2.1 理想流量（零放大，按 AXI 字节算）

| 方案 | N=1 | N=2 | N=3 | N=4 |
|---|---:|---:|---:|---:|
| **A 按 cout 分**（读放大 ×N） | 62.9 MB | 82.0 MB | 101.1 MB | **120.1 MB** |
| **B 按行带分**（halo 只加在 H） | 63.1 MB | 63.2 MB | 63.3 MB | **63.5 MB** |
| B ÷ A 的总流量 | 1.00 | 0.77 | 0.63 | **0.53** |

只看**读**流量更刺眼：A@4 = 76.3 MB，B@4 = 19.6 MB ⇒ **A 是 B 的 3.9 倍**。

> 说明：任务书给的「555 MB / 帧」不是理想流量，它= `Σ rd拍数×8B`（513 MB）+ 输出（43 MB）。
> 而 513 MB 相对理想输入 19.1 MB 是 **27× 读放大** —— 这正是 W2-H 要修的东西（"消 8× 读放大"）。
> 放大倍数对 A/B 是**同一个乘子**，所以两者的相对比较不受影响。

### 2.2 带当前读放大的流量（这才是上板会遇到的）

| 方案 | 总流量 | 0.77 s 内需带宽 | 占**单个** HP 口（1200 MB/s）| 判定 |
|---|---:|---:|---:|---|
| B@3（保底） | ≈ 560 MB | 545 MB/s | 45% | ✅ 舒服 |
| **B@4** | ≈ 570 MB | **740 MB/s** | **62%** | ⚠️ 能过，但峰值余量薄 |
| A@4 | ≈ 2096 MB | **2722 MB/s** | 227% | ❌ 单口必崩；4 口也顶到 DDR 上限 |

⇒ **任务书里「带宽 28% 不是瓶颈」这个结论只在 N=1、且只算理想流量时成立。**
N=4 时真实需求约 740 MB/s（DDR 侧还有 27× 读放大顶着），必须靠 W2-H 把读放大压下来才安全。
**W2-H 是 4 实例的前置条件，不是可选项。**

### 2.3 halo 开销（只算行方向，逐层真实 H 加权）

行带切分时列方向**不需要 halo**：引擎读 `ix = ox*s + kx - pad`，`in_bounds` 对 `ix<0 || ix>=W` 直接零填（`bin_conv_core.v:347`），永远不越界访存。所以

$$\text{读放大} = \frac{H/N + (k-1)}{H/N} = 1 + \frac{(k-1)N}{H}$$

| H（256² 尺度）| 层数 | k=1 时 | k=3, N=2 | k=3, N=3 | k=3, N=4 |
|---|---:|---:|---:|---:|---:|
| 8 | 22 | 1.000 | 1.50 | 1.75 | **2.00** |
| 16 | 44 | 1.000 | 1.25 | 1.375 | **1.50** |
| 32 | 75 | 1.000 | 1.125 | 1.1875 | **1.25** |
| 64 | 54 | 1.000 | 1.0625 | 1.083 | **1.125** ← 任务书的 "12%" |
| 128 | 32 | 1.000 | 1.031 | 1.042 | **1.0625** |

**字节加权后 B@4 只多 2.6% 读流量**（19.6 vs 19.1 MB）——因为大层（H=64/128）的 halo 只有 6%~12%，
而 H=8 的 75 个层虽然 halo 100%，但它们的像素少。

### 2.4 小层处理（这一条任务书担心过头了）

按 `bin_conv_core` 的官方拍数式 `cyc = cout × (20 + 2K + Ho·Wo·K)`，空间切分后变
`cout × (20 + 2K + (Ho/N)·Wo·K)`；逐层取 `argmin_N [ cyc(N)/150MHz + N×5µs ]`（5 µs = 每引擎 22 次 MMIO + start + poll 的保守估计）：

| 层数 | 选 N=1 | N=2 | N=3 | N=4 |
|---|---:|---:|---:|---:|
| 227 | 0 | 1 | 2 | **224** |

- 逐层自适应：**0.80 s**
- 一律 N=4：**0.80 s**
- 一律 N=3：1.07 s
- 一律 N=2：1.60 s
- 串行 N=1：3.19 s

⇒ **只有 3 个 H=8 小层值得退到 2/3 引擎，收益 ≈ 0。所以不需要复杂调度器：固定"用满可用引擎"即可**，
  留一个 per-layer 覆盖开关给那 3 个层就够。这也说明「小层效率低」在这个网络上不是真问题——
  256×256 把即使最小的层也撑到 ~10⁶ 拍量级。

### 2.5 还有一种"半方案"值得知道

**A 的变体：按 cout 分，但只分 2 份。** 读放大 2×（38 MB 理想），换来**零 halo、零 pad 改造、驱动改动最小**
（每个引擎的 H/W/Hout 完全相同，只有 `B_OUT` 和 `cout` 切片不同）。如果 §8.1 的资源结论逼着我们只能上 2 实例，
A-2 的软件代价比 B-2 低得多。**但 B-2 的流量仍然更低，所以默认还是 B。**

---

## 3. 推荐方案

> **推荐：方案 B（按行带 / 空间分块），dedicated HP 口，先 3 实例保底、4 实例冲刺。**

理由（按权重排序）：

1. **流量**：读流量 19.6 MB vs 76.3 MB，差 3.9×；且当前 RTL 有 27× 读放大，A 会把它乘到 108×，实测必崩。
2. **端口安全**：dedicated 拓扑下每个引擎独占一个 HP 口，`AXI_ID_W=4` 的 ARID **不经任何互联**。
   这条不是洁癖——`bin_unit_axi_wrap.v:1067` 用 `rid` 当数组下标（`win_data_q[rid_slot]`），
   项目历史上被 SmartConnect 重映射 ID 坑过一次（见 `integrate_new_engines.tcl` §5 的注释）。
3. **软件代价可控**：B 需要驱动侧把输入布局改成"每带预置 halo"，见 §3.1；这是一次性的、可流水隐藏的工作。
4. **A 的唯一优势（零 halo、零 pad 复杂度）不值得用 3.9× 读流量去换。**

### 3.1 分块契约（**这是 B 方案能落地的关键，请 RTL/驱动两侧都确认**）

RTL 的寻址是 `addr = act_b + (iy·W + ix)·8`，其中 `iy = oy·s + ky·d - pad`，越界零填。
`cfg_pad` 是**一个标量、上下左右对称**，无法表达"只在图像最上面补 1 行"。

⇒ 所以**不能让引擎自己处理 pad**。契约如下（**零 RTL 改动**）：

```
PS 侧把每层输入激活写进一块 (H+2P) × (W+2P) 的"已 pad 好"的 DDR 缓冲（P = 该层 pad）
—— 更好的做法是 binarize/quant 那一步直接产出这个布局，零额外拷贝

引擎 i（i = 0..N-1），B = Hout/N（最后一带吃掉余数）：
    R_PAD = 0                      ← 所有引擎一律 0
    R_W   = W + 2P                 ← 含左右各 P 列
    R_H   = B*s + k - 1            ← 含上下 halo（s=1,k=3 时 = B+2）
    B_IN  = pad_buf + (i*B*s) * (W+2P) * cb * 8       /* 行偏移 */
    B_OUT = out_buf + (i*B) * Wout * ceil(cout/8) * 8 /* 行偏移 */
    Hout_i = (R_H - k)/s + 1 = B*s  ✔ 恰好等于本带输出行数
```

- 每一带的 halo 行从已 pad 的缓冲里**真实读出来**（不是零填），所以带边界不会出现假 pad。
- 最后一带的底部 pad 由 pad 缓冲提供。
- `pad=0` 时这个契约退化成"直接按行偏移切一块"，无额外成本。

**另一种做法（不推荐，但要说明）**：不改布局，靠"带 0 用 pad=P，其余带用 pad=0 且 `B_IN` 前移 P 行"。
它省掉了 pad 缓冲，但最后一带的底部 pad 表达不出来，会出现"末尾 P 行输出丢失 / 读到图外内存"，
必须靠相邻带重叠 + 丢弃来补，边界条件极难测。**预置 pad 是一次性把钱付清的做法。**

---

## 4. 调度策略

```
对每一层 L：
    N_use = min(可用引擎数, max(1, 使 wall 时间最小的 N))     # §2.4，实测 224/227 层选满
    B     = ceil(L.Hout / N_use)
    1) 准备"已 pad"的输入缓冲（PS 侧，可与上一层的 PL 计算重叠）
    2) 一次性写完 N_use 个引擎的配置页（22 个寄存器 × N_use）
    3) 同时 start（背靠背写 AP_CTRL），然后一起 poll 完成
    4) 只有全部 done 且 err_code==0 才继续；任何一个报错 ⇒ 记下 engine id 再抛
```

要点：
- **并行度只按层内算**；跨层流水是 W1-D 的事，这里不重叠。
- 权重每层只需装一次到 blob，N 个引擎**共用同一份 blob**（`B_WGT` 指向同一地址），
  因为空间切分不切 cout ⇒ 权重完全相同。**这省掉了 N 份权重装载时间。**
- 所有引擎共享 `ALPHA/BIAS/ZETA/GAMMA/SLOPE` 基址，同理。

---

## 5. BD 结构（推荐拓扑：`TOPO=dedicated`）

```mermaid
flowchart LR
  subgraph PS
    GP0[ps7 M_AXI_GP0<br/>AXI3 32b ID12]
    HP0[ps7 S_AXI_HP0<br/>AXI3 64b ID6]
    HP1[ps7 S_AXI_HP1]
    HP2[ps7 S_AXI_HP2<br/>新开]
    HP3[ps7 S_AXI_HP3<br/>新开]
  end

  GP0 --> AC[axi_ctrl<br/>axi_interconnect 2.1<br/>NUM_SI=1 NUM_MI=5<br/>s00 auto_pc: AXI3→AXI4LITE]

  AC -- M03 --> B0[bin_unit_0<br/>0x40060000]
  AC -- M00 --> B1[bin_unit_1<br/>0x40080000]
  AC -- M01 --> B2[bin_unit_2<br/>0x400A0000]
  AC -- M04 -. M04 仅 NBIN=4 .-> B3[bin_unit_3<br/>0x400C0000]
  AC -- M02 --> U0[conv_uni_0<br/>0x40040000]

  B0 -- m_axi AXI4 64b ID4 --> P0[pc_conv_bin<br/>AXI4→AXI3] --> HP0
  B1 -- m_axi --> P1[pc_bin_1] --> HP2
  B2 -- m_axi --> P2[pc_bin_2] --> HP3
  B3 -- m_axi -. NBIN=4 且 DROP_CONV_UNI=1 .-> P3[pc_bin_3] -.-> HP1
  U0 -- m_axi --> PU[pc_conv_uni] --> HP1
```

### 5.1 端口分配表

| 引擎 | 控制口 | 数据口（NBIN=3） | 数据口（NBIN=4 + DROP_CONV_UNI=1） |
|---|---|---|---|
| `bin_unit_0` | `axi_ctrl/M03_AXI`（不动） | `pc_conv_bin` → **HP0**（不动） | 同左 |
| `bin_unit_1` | `axi_ctrl/M00_AXI` | `pc_bin_1` → **HP2** | 同左 |
| `bin_unit_2` | `axi_ctrl/M01_AXI` | `pc_bin_2` → **HP3** | 同左 |
| `bin_unit_3` | `axi_ctrl/M04_AXI` | —（NBIN=3 不建） | `pc_bin_3` → **HP1**（conv_uni 已摘） |
| `conv_uni_0` | `axi_ctrl/M02_AXI`（不动） | `pc_conv_uni` → **HP1**（不动） | **删除**（连同 `pc_conv_uni`） |

为什么 NBIN=4 必须 `DROP_CONV_UNI=1`（或改 `TOPO=shared`）：HP0/HP1 已被占，只剩 HP2/HP3 两个口，
3 个新引擎不够分。脚本在参数阶段就 `error` 掉，不会跑到一半才失败。

### 5.2 `TOPO=shared` 备选（保留 conv_uni + 4 实例）

`bin_unit_0..3 /m_axi` → `axi_bin`（`axi_interconnect`，NUM_SI=4、NUM_MI=1）→ `pc_conv_bin` → HP0。

ID 位宽账：4 个 SI × `ID 4bit` → MI ID = 4 + ⌈log₂4⌉ = **6 bit**，正好等于 Zynq HP 的 6 bit（`PCW_S_AXI_HP0_ID_WIDTH=6`），不多不少。互联在返回路径剥掉高位，引擎看到的 `rid` 仍是自己的 4 bit。
**协议上成立，但会引入 ID 重映射 + 4 master 仲裁，属于"能不用就不用"。**

---

## 6. 地址分配

| 实例 | 基址 | 高端 | 窗口 | A[16] | 状态 |
|---|---|---:|---:|---:|---|
| `conv_uni_0` | `0x4004_0000` | `0x4005_FFFF` | 128 KB | 0 | 不动 |
| `bin_unit_0` | `0x4006_0000` | `0x4007_FFFF` | 128 KB | 0 | 不动（**已验证上板**）|
| `bin_unit_1` | `0x4008_0000` | `0x4009_FFFF` | 128 KB | 0 | 新增 |
| `bin_unit_2` | `0x400A_0000` | `0x400B_FFFF` | 128 KB | 0 | 新增 |
| `bin_unit_3` | `0x400C_0000` | `0x400D_FFFF` | 128 KB | 0 | 新增 |

校验（`dryrun_bd_multi.py` 自动跑，全 PASS）：

- **间隔 0x20000 = 128 KB = 窗口大小** ⇒ 首尾相接、零重叠、零空隙。`0x40060000 + 3×0x20000 = 0x400C0000` ✔
- **全部在 GP0 aperture `0x4000_0000..0x7FFF_FFFF` 内** ✔（`0x4000_0000..0x3FFF_FFFF` 是 DDR，不能碰）
- **每个基址的 A[16] 都是 0**（`0x4006>>0 &1 = 0`、`0x4008&1=0`、`0x400A&1=0`、`0x400C&1=0`）。
  这条不是形式主义：`bin_engine.py:455` 有硬断言，A[16]=1 会直接 `ValueError`（`+0x10000` 会翻到保留页，寄存器全读 0）。
- 控制侧不涉及 `assign_bd_address` 的 master 段：`bin_unit_*/m_axi` 地址空间是默认 4G END_POINT，
  由 Vivado 在 `generate_target` 时自动填，不需要人工分配。

---

## 7. 交付脚本

| 文件 | 作用 |
|---|---|
| `pl/scripts/bd_multi_bin.tcl` | **主脚本**：改 BD + 分配地址 + validate + generate_target + `.hwh` 自检 |
| `pl/scripts/build_multi_150p.tcl` | 从 `proj_multi` 构建比特流（IP 清单**自动扫描**，不硬编码） |
| `pl/scripts/dryrun_bd_multi.py` | 不跑 Vivado 的地址/端口/重叠校验（纯 JSON 推演） |
| `pl/scripts/mock_vivado.tcl` | 最小 Vivado BD API 模拟器，用来在无 Vivado 环境把 tcl 真跑一遍 |

### 7.1 用法

```bash
cd pl/scripts

# ① 先干跑，看地址表对不对（不需要 Vivado）
python3 dryrun_bd_multi.py

# ② 演算模式：改内存里的 BD、不存盘（需要 Vivado，但不动文件）
DRY=1 vivado -mode batch -source bd_multi_bin.tcl

# ③ 真跑（默认 3 实例，最保守）
vivado -mode batch -source bd_multi_bin.tcl

# ④ 4 实例（摘掉 conv_uni，顺带省 25.7k LUT + 空出 HP1）
NBIN=4 DROP_CONV_UNI=1 vivado -mode batch -source bd_multi_bin.tcl

# ⑤ 构建比特流
NBIN=3 vivado -mode batch -source build_multi_150p.tcl
```

参数（环境变量）：`NBIN=2|3|4`、`TOPO=dedicated|shared`、`DROP_CONV_UNI=0|1`、`RUN_OOC=0|1`、`DRY=0|1`。

### 7.2 脚本里的 6 道自检（针对"BD 地址错挂过 3 次板子"）

1. **前置硬检**：BD 路径含 `proj_engine` 就 `error`（物理上拒绝写只读工程）。
2. **同构闸（创建时）**：新实例的 VLNV、接口集 `{m_axi s_axi}`、`s_axi_*` 引脚数 **必须 = 19**，
   且不许出现 `*_arlen/awlen/wid/burst/prot/cache/qos`（出现即 = proj_v2 那种 v0 包装层）。
3. **地址闸（分配时）**：`assign_bd_address` 之后立刻回读 `OFFSET`，不等于规划值就 `error`。
4. **validate 闸**：失败就不存盘（`proj_multi` 保持上一次状态）。
5. **`.hwh` 闸（硬性要求）**：解析 `hw_handoff/design_1.hwh`
   - 每个 `bin_unit_i` 的 `BASEVALUE`/`HIGHVALUE` 必须精确等于表 §6（大小写都归一化比较）
   - 每个 `bin_unit_i` 的 **端口集合**必须与 `bin_unit_0` **逐个相等**（s_axi 19 / m_axi 21）
   - `s_axi_awvalid` / `s_axi_arvalid` 必须真有 `CONNECTION`（防"引擎被常量传播剪掉"）
   - 生成的 `design_1.v` 里必须有 `bin_unit_i` 的连线
6. **收尾闸**：比对 `proj_engine/…/design_1.bd` 的 mtime+size，证明只读工程没被碰。

---

## 8. 干跑结果

### 8.1 `dryrun_bd_multi.py`（纯 Python，无 Vivado）

```
$ python3 dryrun_bd_multi.py
BD: .../proj_multi/proj_multi.srcs/sources_1/bd/design_1/design_1.bd
device: xc7z015clg485-2   validated=true
[NBIN=2 dedicated]  axi_ctrl 空闲 M00/M01/M04；新引擎用 S_AXI_HP2          → PASS
[NBIN=3 dedicated]  新引擎用 S_AXI_HP2, S_AXI_HP3                          → PASS
[NBIN=4 shared]     4×ID4 → MI ID 6bit ≤ HP 6bit                          → PASS
[NBIN=4 dedicated+drop_conv]  新引擎用 HP1,HP2,HP3                         → PASS
总结: 全部通过
```

### 8.2 `mock_vivado.tcl`（把 tcl 真跑一遍）

用真实 `design_1.bd` 的内容做后端，模拟 22 个 Vivado BD API。四种配置**全部跑到 `.hwh` 自检通过**：

```
$ NBIN=3 HWH=... NETLIST=... tclsh mock_vivado.tcl bd_multi_bin.tcl
INFO: bin_unit_0 接口 = m_axi s_axi  (期望 {m_axi s_axi})
INFO:     bin_unit_1 接口=s_axi m_axi  s_axi引脚=19 ✔ 同构
INFO:     bin_unit_2 接口=s_axi m_axi  s_axi引脚=19 ✔ 同构
INFO:   axi_ctrl/M00_AXI  ->  bin_unit_1/s_axi
INFO:   axi_ctrl/M01_AXI  ->  bin_unit_2/s_axi
INFO:   使能 ps7 CONFIG.PCW_USE_S_AXI_HP2 / HP3
INFO:   bin_unit_1/m_axi -> pc_bin_1(AXI4→AXI3) -> ps7/S_AXI_HP2
INFO:   /bin_unit_0/s_axi/reg0  <- 0x40060000  range=0x00020000
INFO:   /bin_unit_1/s_axi/reg0  <- 0x40080000  range=0x00020000
INFO:   /bin_unit_2/s_axi/reg0  <- 0x400A0000  range=0x00020000
INFO: validate 通过
INFO:   ✔ bin_unit_0  BASE=0x40060000  HIGH=0x4007ffff
INFO:   ✔ bin_unit_1  BASE=0x40080000  HIGH=0x4009ffff
INFO:   ✔ bin_unit_2  BASE=0x400a0000  HIGH=0x400bffff
INFO:   ✔ bin_unit_1 端口与 bin_unit_0 同构（s_axi=19 m_axi=21）
INFO:   ✔ bin_unit_2 端口与 bin_unit_0 同构（s_axi=19 m_axi=21）
 ✅ 自检全部通过
INFO: proj_engine 的 BD 未被触碰（mtime/size 不变）✔
```

**干跑抓出来的真实 bug（已修）**：
1. `$pcc(AXI4→AXI3)` —— Tcl 把它当数组引用，运行期炸；已改成 `${pcc}(...)`。
2. `get_property NAME [多元素列表]` —— 真 Vivado 常直接报 wrong # args；已改成逐个取 + `file tail`。
3. `get_property NAME` 对 BD 引脚返回**全路径**（`bin_unit_0/m_axi`），直接比 `"m_axi s_axi"` 永远不等；已加 `file tail`。

### 8.3 `xvlog` 语法检查

```
$ xvlog -sv bin_unit_axi_wrap.v bin_unit_axi_wrap_bd.v bin_conv_core.v \
        bin_rprelu.v bin_sam.v bin_interp.v
INFO: analyzing module bin_unit_axi_wrap / bin_unit_axi_wrap_bd / bin_conv_core
      / bin_rprelu / bin_sam / bin_interp
（0 error, 0 warning）
```
⇒ 要被例化 4 次的那套 RTL 本身是干净可编译的；BD 的四份实例是四个独立层次。

---

## 9. 驱动侧改动清单（**只报告，未动手**）

> ⚠ **行号会漂**：`bin_engine.py` / `bin_net.py` 正在被 W0-C / W1-D 改（我写这段时
> `bin_net.py` 在 23:49 刚被改过，`_conv()` 从 399 行挪到了 615 行，`self.engine()` 的签名也
> 从 `engine(x_q, BL)` 改成了 `engine(x_f, BL)`）。下表行号是 **2026-10-30 23:52 的快照**，
> 动手前请用 `grep -n` 重新定位符号，不要直接跳到行号上改。

| # | 符号（当前行号） | 现状 | 要改成 |
|---|---|---|---|
| 1 | `_resolve_ip` @ **485** | 找 `bin_unit_<unit_id>`，找不到就 `cands[min(unit_id,len-1)]` **静默退回** | ① 加 `list_units(overlay)` 返回排序后的全部 `bin_unit_*`；② 找不到目标就**报错**，不要退回（退回会让 4 实例跑成"1 实例×4 并存"，静默错数据）|
| 2 | `BinConvEngine.__init__` @ **433** | `unit_id` 单值 | 保留单引擎语义，另加 `MultiBinEngine(overlay, n=None)` 持有 `engines: list[BinConvEngine]` |
| 3 | `config()` @ **664** | 写 `L.regs()` | 加 `config_band(L, i, n, *, H_in, in_pa, out_pa)`：按 §3.1 算 `R_PAD=0 / R_W=W+2P / R_H=B*s+k-1`，其余寄存器照抄 |
| 4 | `set_bases()` @ **697** | 所有基址来自同一个 blob | `B_WGT/ALPHA/BIAS/ZETA/GAMMA/SLOPE` **全部相同**（空间切分不切 cout，权重可共享，省掉 N 份装载），只把 `B_IN`/`B_OUT` 按带偏移 |
| 5 | `run()` @ **776** | 单引擎串行 | 加 `run_all(in_buf, out_buf, L, n)`：**先全部 config，再背靠背 start，最后一起 wait**；任一 `err_code≠0` 要报出是哪个 engine |
| 6 | `BinLayer` @ **222** | `pad` 从 index.json 原样取；`Hout/Wout` 由 `out_hw()` 算 | 加 `split_bands(L, n)` 产出子层：`pad=0`、`H=R_H`、`W=W+2P`、`Hout=B`；`act_bytes`/`out_bytes` 要按带尺寸重算 |
| 7 | `estimate_cycles` @ **306** | 单引擎拍数 | 加 `estimate_cycles_band(L,n)`：`cout×(20+2K+(Ho/n)·Wo·K)`，用于 §4 的 N 选择 |
| 8 | **`__call__` @ 977** / `pack_input` | `xf` → `pack_input(xf, L)` → `quant_pack` | **pad 就插在这里**：`xf` 先补成 `(cin, H+2P, W+2P)` 再进 `pack_input`。`quant_pack`（@1057）**不用改**——它按 `x.reshape(cin,hw).T` 走，pad 后行 stride 自动变成 `W+2P` ✔ |
| 9 | `quant_pack` @ **1057** | 产出 `(cb,H,W)`，行 stride = W | 一行都不用改；只要输入是 pad 过的，stride 天然正确（这点很关键，省掉一处 RTL 改动）|
| 10 | A[16]/range 自检 @ **455-464** | 单实例 | 对每个 engine 跑一遍（4 个基址都要过 A[16]=0 + range≥0x10080）|
| 11 | `bin_net.py:_conv` @ **615** | `y, _rs = self.engine(x_f, BL)`（引擎内部自己量化+打包） | 改调 `self.engine.run_multi(x_f, BL)`；注意**入参已经是 float**，pad 在里面做 |
| 12 | `FakeUnit`（`bin_engine.py` 尾部）| 只造 `bin_unit_0` | 造 N 个，供本机自检多实例调度逻辑 |

**建议的落地顺序**：先做 #1（`list_units` + 不再静默退回）和 #6/#3/#4（band 化），
这两步做完就能"用 N=1 走通 band 路径"（N=1 时 `B=Hout`、`R_H=Hout*s+k-1`），
**先在单实例比特流上验证分块契约正确，再上多实例比特流** —— 这样万一结果不对，能立刻分清是分块错还是 BD 错。

---

## 10. 风险清单（按严重度）

| 级 | 风险 | 依据 | 缓解 |
|---|---|---|---|
| **P0** | **4 实例装不进 xc7z015** | 现有 1 实例已 **38,360/46,200 = 83.03%** LUT、48,352/92,400 = 52% FF；`conv_uni_0` 单独 25,694 LUT；余量仅 **7,840 LUT** | ① 先 `open_checkpoint proj_multi/proj_multi.runs/design_1_bin_unit_0_9_synth_1/design_1_bin_unit_0_9.dcp; report_utilization` **实测单实例 LUT/FF**（我算不出，OOC 没落 util rpt）；② **`DROP_CONV_UNI=1`**：`bin_net.py` 根本不用 conv_uni，摘掉立省 25.7k LUT ⇒ 4 实例约 74%；③ 否则只能 N=2 |
| **P0** | `bin_unit_axi_wrap_bd.v:19` 的 `X_INTERFACE_PARAMETER` **声明错**：`PROTOCOL AXI3, DATA_WIDTH 64, HAS_BURST 1, HAS_PROT 1`，但端口是 **AXI4-Lite 32bit**（无 len/burst/prot） | 这就是 v0/v9 差异的**真正根因**：Vivado 有时信属性（→AXI3 64b，不插协议转换器 → MMIO 读非 0 偏移挂死）、有时信端口（→Lite 32b，正常）。`proj_v2` 抽到前者，`proj_engine` 抽到后者 | **本轮不许改 RTL**，所以：脚本第 2/5 道闸把它变成"必然失败"而不是"随机挂板"。**强烈建议 W0-* 顺手把第 19 行改成 `PROTOCOL AXI4LITE, ADDR_WIDTH 32, DATA_WIDTH 32, HAS_BURST 0, HAS_PROT 0`，第 54 行改成 `PROTOCOL AXI4`** —— 这是 3 次挂板的根治 |
| **P1** | 读放大 27×（`Σrd = 513 MB` vs 理想 19 MB）。B@4 需 740 MB/s，单 HP 口 1200 MB/s 的 **62%** | `net_per_layer.csv` 的 `rd` 列 + PLAN §1.3 | **W2-H 是 4 实例的前置条件**。3 实例时 45%，可以先上 |
| **P1** | `pc_bin_1/2/3` 是新 `create_bd_cell`，参数靠 propagation（`SI_PROTOCOL=AXI4/MI_PROTOCOL=AXI3` 我显式写了，宽度自动） | `pc_conv_bin.xci` 里 `ADDR_WIDTH=32/DATA_WIDTH=64/ID_WIDTH=4` 都是 `value_src=propagated` | validate + `.hwh` 端口集比对兜底；若 propagate 出 6bit ID（shared 拓扑）也合法 |
| **P2** | `conv_uni_0` 删除后，`build_final_150p.tcl` 的 `DSP≥78` 护栏会把正确设计判死；反之"引擎被剪掉"又恰好是没 DSP 的 bin_unit，老护栏只能放行 | `build_final_150p.tcl` 的 `if {$ndsp2 < 60} {error}` | `build_multi_150p.tcl` 已改成：conv_uni 在→`DSP≥28`，不在→`DSP≤4`；另加"逐实例层次存在性"检查（综合后 + 实现后各一次）|
| **P2** | `assign_bd_address` 幂等性 | 重跑脚本时旧段残留会抢窗口 | 脚本 §4 按路径名清理 `/bin_unit_<n>/` 段；`-force` 覆盖 |
| **P2** | 陈旧生成产物（项目 4 次被咬） | `docs/binary/06_集成状态_20261030.md` §下一步 | `generate_target all` + `RUN_OOC=1`（`synth_ip -force`）。若仍异常：先 `rm -rf proj_multi.gen proj_multi.runs proj_multi.cache` 再跑 |
| **P3** | `S_AXI_HP2/HP3` 打开后 PS 的 DDR 仲裁行为变化 | 未知 | 首次上板只跑单层 + `perf_probe`，别直接跑整网 |

---

## 11. 我不确定的地方（请实测确认，别当结论用）

1. **单实例 bin_unit 的确切 LUT / FF**。OOC 跑只落了 `.dcp` 没落 `utilization.rpt`，我不能跑 Vivado 去量。
   用这条命令量（30 秒）：
   ```
   vivado -mode batch -notrace -source <(echo 'open_checkpoint .../design_1_bin_unit_0_9.dcp; report_utilization')
   ```
   这个数直接决定 N=3 还是 N=4，是**当前最该先拿到的数字**。
2. **`create_bd_cell -type ip -vlnv` 能否 100% 复现 v9 的协议推断**。我能证明的是：
   `proj_engine` 与 `proj_v2` 的 `.bd` 里 `bin_unit_0` 的 `interface_ports` **逐字节相同**，
   差异只在各自生成网表时的推断结果 ⇒ 推断**不由 `.bd` 决定**，而由当时的 `ip_repo/component.xml` 决定。
   当前 `component.xml` 里**没有** `spirit:protocol`/`DATA_WIDTH`（我 grep 过），所以应从端口推断（→Lite 32b）。
   但这只是"应该"，**没有实跑验证**。兜底 = 脚本第 5 道闸，它会 `error` 而不是让坏 BD 往下走。
3. **`Hout = (R_H - k)/s + 1` 在 `s>1` 时的整除边界**。本网络 227 层全是 `s=1`，我只验证了 `s=1`；
   若以后出现 `s=2`，`R_H = B*s + k - 1` 的推导需要重推（我在 §3.1 里按通用式写了，但没跑数）。
4. **每引擎固定开销的真实值**。§2.4 用了 5 µs（22 次 MMIO × ~0.2 µs + start + poll）。若实际是 20~50 µs
   （PYNQ MMIO 在 `mmio` 未缓存时可能更慢），小层的 N 选择会变，但 224/227 层选 N=4 的结论很稳
   （它们单引擎都在 ms 量级）。
5. **`mock_vivado.tcl` 的保真度**。它是我为这次干跑写的最小实现，语义按 Vivado 文档 + 项目里踩坑注释对齐
   （例如 `get_property NAME` 返回全路径、`get_bd_intf_pins -of_objects {}` 返回全设计、`connect_bd_net`
   会并入已存在的网）。**它能证明 tcl 逻辑自洽，不能证明 Vivado 的 DRC/推断行为。**
6. **HP0/HP1/HP2/HP3 四口同时工作的实际聚合带宽**。理论 4×1200 = 4800 MB/s，但 DDR 控制器只有一路；
   `PLAN_1second.md` 自己写的上限是 2000 MB/s。B@4 需要 740 MB/s ⇒ 37%，看起来安全，**但没有实测**。

---

## 12. 下一步（给 W3-I 的操作顺序）

```bash
cd pl/scripts

# 0. 量单实例资源（决定 N=3 还是 N=4）      ← 最优先
#    open_checkpoint .../design_1_bin_unit_0_9.dcp ; report_utilization

# 1. N=1 走通分块路径（用现有比特流验证 §3.1 契约，不换 bit）
#    → 改驱动 #1/#6/#3/#4，逐层与 conv_rtl 对拍

# 2. 3 实例保底
python3 dryrun_bd_multi.py
DRY=1 vivado -mode batch -source bd_multi_bin.tcl
vivado       -mode batch -source bd_multi_bin.tcl
NBIN=3 vivado -mode batch -source build_multi_150p.tcl
#    → 上板：先读 ip_dict 确认 4 个 phys_addr，再单层对拍，再整网

# 3. 4 实例（需要 DROP_CONV_UNI=1 腾出 LUT 和 HP 口）
NBIN=4 DROP_CONV_UNI=1 vivado -mode batch -source bd_multi_bin.tcl
NBIN=4 vivado -mode batch -source build_multi_150p.tcl
```

**上板前必查**（对应 3 次挂板事故）：
1. `overlay.ip_dict` 里 `bin_unit_0..N-1` 的 `phys_addr` 是否 = `0x40060000/0x40080000/0x400A0000/0x400C0000`。
2. 每个引擎 `config_readback()` 与期望逐寄存器一致（`config_check()` 返回 ok）。
3. 先只跑最小层，看 `done` 与 `err_code`，再跑整网。
