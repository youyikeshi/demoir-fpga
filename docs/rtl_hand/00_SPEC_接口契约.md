# 手写 RTL 模块 · 统一契约与验收规范

> 所有手写模块**必须**遵守本文件。任何偏离都要在模块文档里显式标出并说明理由。
> 最后更新：2026-10-23

---

## 0. 架构决策（为什么这么做）

HLS 输出的是一个**单体模块**（167 态 FSM + 19 条流水线 + 累加器 + 收尾，彼此耦合），
拆不出干净的部件边界。所以采用**"同板并存的新 IP"**策略：

$$\text{每类卷积核做一个独立 IP} \;\to\; \text{PS 侧按层路由} \;\to\; \text{HLS 引擎保底}$$

| 优点 | 说明 |
|---|---|
| 零风险 | HLS 引擎原封不动；新 IP 不可用时自动回退 |
| 可独立验收 | 逐层逐位比对，不需要整网、不需要上板 |
| 渐进上线 | k=5（1 层）→ k=1（132 层）→ k=3（114 层），每步可单独启用 |
| 不动 ABI | PS 侧只多一个"这类层交给谁"的分支 |

---

## 1. 模块清单与归属（文件互不重叠）

| 模块 | 文件（唯一所有者） | 覆盖层 | 目标 |
|---|---|---|---|
| `conv_k5_engine` | `modules/conv_k5_engine.v` | 1 层 | 3.39 s → ≤0.15 s |
| `conv_k1_engine` | `modules/conv_k1_engine.v` | 132 层 | 见任务书 |
| `conv_k3_engine` | `modules/conv_k3_engine.v` | 114 层 | 见任务书 |
| 验证设施 | `common/`、`tools/`、`tb/` | — | 见 §5 |

**规则**：每个 agent **只碰自己那一个 `.v` + 自己的 `tb_<模块>.v` + 自己的文档**。
共享设施（`common/`、`tools/`、本文件）由验证设施负责人维护，其他人只读。

---

## 2. 数据格式（三者必须完全一致）

### 2.1 激活：C8 交错 + 通道补齐到 8 的倍数

$$\text{addr}(c,y,x) = \left\lfloor \frac{c}{8} \right\rfloor \cdot HW \cdot 8 \;+\; y \cdot W \cdot 8 \;+\; x \cdot 8 \;+\; (c \bmod 8)$$

- int8 张量**通道维一律补齐到 8 的倍数**：cin=12 → 物理 16（后 4 个恒 0）；cin=3 → 物理 8
- **一个像素的 8 个通道是连续 8 字节** ⇒ 可一次 64-bit 读
- 输出同理：cout=48 → 48（已是 8 倍数）；cout=3 → 物理 8（后 5 个写 0 或忽略，**由 PS 侧保证不读**）

### 2.2 权重：PAD 容器（为了宽读 + bank 常量化）

| 核型 | 每 (oc,ic) 字节数 | 布局 |
|---|---:|---|
| k=1 | 1 | 1 抽头 |
| k=3 | **12** | 9 抽头 + 3 个零 = 3 个 32-bit 字（PAD12）|
| k=5 | **28** | 25 抽头 + 3 个零 = 7 个 32-bit 字（PAD28）|

**为什么补零**：补齐后每个 (oc,ic) 是整数个 32-bit 字，能一次宽读；
但注意 `12k mod 8 = 28k mod 8 = 4`（k 为奇数）⇒ **只有一半的 (oc,ic) 起点是 8 字节对齐**，
所以**不要试图 8 字节宽读权重**（实测 AR 笔数 201→15,416，净亏 27%）。

### 2.3 输出标度

$$\text{int8\_out} = \mathrm{clamp}\left( (\text{acc} + \text{bias}) \gg \text{shift},\ 0,\ 127 \right)\quad(\text{ReLU 层})$$

- 累加器 **26 位有符号**（k=3 最坏层定；k=5 + cin≳200 会绕回，当前网络不触发）
- `shift` 由寄存器传入，每个 conv 一份

---

## 3. 统一模块接口

```verilog
module conv_kX_engine #(
    parameter int CIN  = 12,
    parameter int COUT = 48,
    parameter int W    = 320,
    parameter int H    = 320,
    parameter int PAD  = 2,
    parameter int STRIDE = 1,
    parameter int DIL  = 1,
    parameter bit RELU = 1'b1
)(
    input  logic        ap_clk,
    input  logic        ap_rst_n,

    // ---- AXI4 master 读：输入 / 权重 / 偏置（64-bit 数据宽）----
    // 地址为**字节地址**；AWLEN/ARLEN 给的是**拍数-1**（读侧 8 字节/拍）
    output logic        m_axi_in_arvalid,  input logic m_axi_in_arready,
    output logic [31:0] m_axi_in_araddr,   output logic [7:0]  m_axi_in_arlen,
    input  logic        m_axi_in_rvalid,   output logic m_axi_in_rready,
    input  logic [63:0] m_axi_in_rdata,    input  logic m_axi_in_rlast,

    // 权重与偏置同理：m_axi_w_*, m_axi_b_*

    // ---- AXI4 master 写：输出（64-bit 数据宽）----
    output logic        m_axi_out_awvalid, input logic m_axi_out_awready,
    output logic [31:0] m_axi_out_awaddr,  output logic [7:0] m_axi_out_awlen,
    output logic        m_axi_out_wvalid,  input  logic m_axi_out_wready,
    output logic [63:0] m_axi_out_wdata,   output logic [7:0] m_axi_out_wstrb,
    output logic        m_axi_out_wlast,
    input  logic        m_axi_out_bvalid,  output logic m_axi_out_bready,

    // ---- 控制寄存器（AXI-Lite 侧由 BD 分配地址，这里只列寄存器语义）----
    input  logic [31:0] ctrl_in_addr,
    input  logic [31:0] ctrl_wgt_addr,
    input  logic [31:0] ctrl_bias_addr,
    input  logic [31:0] ctrl_out_addr,
    input  logic [31:0] ctrl_shift,
    input  logic [31:0] ctrl_cin,       // 运行时通道数（层间可变）
    input  logic [31:0] ctrl_cout,
    input  logic [31:0] ctrl_h,
    input  logic [31:0] ctrl_w,
    input  logic        ctrl_start,
    output logic        ctrl_done,
    output logic        ctrl_busy
);
```

**为了并行开发，第一版可以只实现"固定形状"**（参数写死成该类的典型层），
把 `ctrl_*` 的运行时字段留作常量即可——只要 testbench 能跑通、
且 ps 侧将来能按层选 IP。**但端口名与方向必须和上面一致**，否则合不起来。

### 3.1 资源红线（csynth 估计口径）

| 模块 | DSP | BRAM18K | LUT |
|---|---:|---:|---:|
| conv_k1_engine | ≤ 60 | ≤ 30 | ≤ 12,000 |
| conv_k3_engine | ≤ 120 | ≤ 40 | ≤ 18,000 |
| conv_k5_engine | ≤ 140 | ≤ 40 | ≤ 12,000 |

**合计不得超过 DSP 160 / BRAM 190 / LUT 46,200 减去 HLS 引擎占用**。
所以三个模块**不会同时上线**——由 PS 侧按层选，而且第一版可以**替换掉 HLS 引擎里的对应路径**
（HLS 侧删掉那条分支后，那部分资源就释放了）。

---

## 4. 黄金参考与向量格式（统一验收的基础）

**同一个目录、同一套文件，任何模块都用它**：

```
rtl_hand/golden/<shape>/
  meta.json         # {"cin":12,"cout":48,"k":5,"s":1,"d":1,"pad":2,"H":320,"W":320,"relu":1}
  input_c8.bin      # int8, C8 布局, 物理通道数 = ceil(cin/8)*8
  weight.bin        # PAD 容器（k=1:1B, k=3:12B, k=5:28B 每 (oc,ic)）
  bias.bin          # int32 × cout
  expected_c8.bin   # int8, C8 布局, 物理通道数 = ceil(cout/8)*8  ← 黄金输出
  shift.txt         # 一个十进制整数
```

**黄金输出从哪来**：用 `pl/src/conv_ref_rt.cpp`（与 RTL 逐位一致的 C 参考）生成，
并由现有 cosim 工程交叉验证。**不要**用 GPU 浮点模型当黄金。

---

## 5. 统一验收流程（每个模块都一样）

```
┌─ 1. 编译 ──────────────────────────────────────────┐
│  xvlog 模块 + tb + common/eq_checker.v             │
├─ 2. 灌向量 ────────────────────────────────────────┤
│  $readmemh 读 golden/<shape>/*.bin（tb 里统一写）   │
├─ 3. 跑 ────────────────────────────────────────────┤
│  驱动 ctrl_start，等 ctrl_done                     │
├─ 4. 比对 ──────────────────────────────────────────┤
│  逐字节比 expected_c8.bin（不是"看起来像"）         │
├─ 5. 吞吐 ──────────────────────────────────────────┤
│  统计实际拍数，与目标比值（≤1.2 倍）写成数字        │
└────────────────────────────────────────────────────┘
```

**验收报告模板**（写在 `docs/rtl_hand/<模块>.md` 末尾）：

| 项 | 值 |
|---|---|
| 逐字节一致 | `N / N`（必须全对）|
| 随机 fuzz | `M / M` 组（≥200，含全 0 / 全 127 / 全 -128 边界）|
| 每层拍数 | 实测 `X` 拍 vs 目标 `Y` 拍（`X/Y`）|
| 资源（csynth） | DSP `a` / BRAM `b` / LUT `c` / Fmax `d` MHz |
| 未达标的项 | 逐条列出 + 原因 + 数据 |

**任何一项不过，模块不得并入。**

---

## 6. 并行开发纪律

1. **只碰自己的文件**（见 §1）；
2. **共享设施只读**：`common/`、`tools/`、本文件、`golden/`；
3. **每 30 分钟落一次盘**：把已完成部分写进文档，不要攒到最后（会被限流打断）；
4. **不确定的标「存疑」**，不要用推断冒充事实；
5. **不要 git commit**——由主控统一提交；
6. 遇到本契约没覆盖的问题，**在文档里提出来**，由主控统一裁决，不要自己改契约。
