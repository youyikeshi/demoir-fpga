# 从 HLS 源码到板子上跑：中间隔着什么

## 0. 一句话结论

**现在不能直接上板实测。** 缺的不是算法代码，是 bitstream 那条产线。

而且有一个常见的误解要先纠正：**PYNQ 的 `Overlay` 不负责产生 AXI 互连，它是 `.hwh` 的消费者。**
没有 Vivado block design 就没有 `.hwh`，`Overlay('xxx.bit')` 会直接抛异常，跟 PYNQ 装得好不好无关。

本文回答三个问题：为什么 C++ 里看不到 AXI 例化、PYNQ 到底替你做了什么、还差哪几步。

---

## 1. 为什么 C++ 里没有 AXI 例化

因为**那本来就不该在 C++ 里写**。`pragma` 是声明意图，例化发生在综合阶段。

`pl/src/demoir_top.cpp:27-30` 声明四个张量指针各走一条独立的 AXI4 master：

```c
#pragma HLS INTERFACE m_axi port = in  offset = slave bundle = gmem0 depth = 65536
#pragma HLS INTERFACE m_axi port = w   offset = slave bundle = gmem1 depth = 65536
#pragma HLS INTERFACE m_axi port = b   offset = slave bundle = gmem2 depth = 4096
#pragma HLS INTERFACE m_axi port = out offset = slave bundle = gmem3 depth = 65536
```

`:31-44` 把 13 个标量 + `return` 挂到 AXI-Lite 的 `control` bundle。写完这 18 行，HLS 侧的工作就结束了。

综合器据此**自动生成**了完整端口。跑过 `make csynth` 后在
`pl/build/hls/demoir_hls/solution1/impl/verilog/demoir_conv2d_top.v` 里能直接看到
（这是构建产物，删掉 `pl/build/` 后重新 `make csynth` 会再生）：

```
parameter C_M_AXI_GMEM0_ADDR_WIDTH = 64;      // 64 位物理地址
parameter C_M_AXI_GMEM0_DATA_WIDTH = 32;

output        m_axi_gmem0_AWVALID;            // 写地址通道
output [63:0] m_axi_gmem0_AWADDR;
output  [7:0] m_axi_gmem0_AWLEN;              // burst 长度
...
output        m_axi_gmem0_ARVALID;            // 读地址通道
input         m_axi_gmem0_RVALID;
...
input  [ 7:0] s_axi_control_AWADDR;           // AXI-Lite 从口：8 位地址 = 256 字节 = 64 个寄存器
input  [31:0] s_axi_control_WDATA;
```

`AW`/`W`/`B`/`AR`/`R` 五通道、burst 拆分 FSM、位宽转换、地址对齐处理，**没有一行是手写的**。

pragma 与生成物的对应关系：

| 你写的 | HLS 生成的 | 谁连它 |
|---|---|---|
| `m_axi port = in bundle = gmem0` | 一个完整的 AXI4 master 端口组 `m_axi_gmem0_*` | Vivado 里接到 PS 的 `S_AXI_HP0` |
| `s_axilite port = C_in bundle = control` | 一个 32 位寄存器 + 地址映射条目 | Vivado 里接到 PS 的 `M_AXI_GP0` |
| `s_axilite port = return` | `ap_start`/`ap_done`/`ap_idle`/`ap_ready` 控制寄存器 | PYNQ 写它启动、读它等完成 |

手写 AXI 时序是 RTL 时代的事。HLS 的 POSIX 风格函数签名 + pragma 就是为了让你不写那个——
这也正是 M2 验收标准里"csim 与 csynth 用同一份 src 实现，不需要维护第二份"能成立的原因。

---

## 2. 产线：从 `.cpp` 到 Python 的一次调用

```
pl/src/*.cpp + demoir_top.cpp 的 pragma
        │
        │  ① csynth_design                    ← 已完成（pl/run_hls.tcl:39）
        ▼
   RTL + 资源/时序报告                        （34 DSP / 9258 LUT / 137 MHz 估算）
        │
        │  ② export_design                    ← ❌ 没做
        ▼
   IP 包（IP-XACT + 寄存器地址映射表）
        │
        │  ③ Vivado block design 连线          ← ❌ 没做
        │     PS7 + AXI Interconnect + Processor System Reset + 时钟
        ▼
   block design（.bd）
        │
        │  ④ implementation + write_bitstream  ← ❌ 没做
        ▼
   demoir.bit + demoir.hwh
        │
        │  ⑤ PYNQ::Overlay('demoir.bit')      ← ❌ 没做
        ▼
   ol.demoir_conv2d_top_0.register_map  →  Python 写寄存器
```

**注意 ② 也没做。** `pl/run_hls.tcl` 到 `csynth_design` 就结束了（第 11-13 行注释写明
"首版只有 csynth，cosim / impl 要几十分钟，等 M2 定下并行度再做"）。
`export_design` 才是产生寄存器地址映射表的那一步——所以现在连"`C_in` 在 0x10 还是 0x18"
都还不存在，更谈不上填 `.hwh`。

四类缺口的现状：

| 环节 | 谁做 | 现状 | 证据 |
|---|---|---|---|
| 算子 HLS 实现 + 接口声明 | 本项目 | ✅ 12 类算子全部 csynth 通过 | `pl/src/`、`pl/run_hls.tcl` |
| IP 导出（含寄存器映射） | `export_design` | ❌ | `run_hls.tcl:39` 之后没有 |
| block design + bitstream | Vivado | ❌ | `deploy/xsa/ax7015b_ps7_complete.tcl` 只配 PS7 外设，一个 PL 单元都没有 |
| PYNQ 驱动 + 调用代码 | 本项目 | ❌ | `deploy/pynq/` 是 M0 的 rootfs 构建，不含 overlay |

`deploy/xsa/ax7015b_ps7_complete.tcl` 是 M0 阶段为了修 SD 卡/网口/QSPI 使能而写的
PS7 配置脚本（见 `deploy/xsa/README.md`），**不是**这个用途，别拿它当 block design。

---

## 3. PYNQ 的 `Overlay` 在哪一环

它是产线**末端**的消费者，做的事只有三件：

1. 把 `.bit` 通过 FPGA manager 灌进 PL；
2. 解析同名 `.hwh`，得到 IP 实例名（如 `demoir_conv2d_top_0`）和每个寄存器的**字节偏移**；
3. 据此建出 `register_map` 对象，让 `regs.C_in = 12` 这种写法能落到正确的地址上。

它**不会**凭空造出 AXI 互连。`.hwh` 是 Vivado 在 block design 里给 IP 分配了地址段之后
才导出的；没有 block design，`Overlay()` 找不到硬件描述，直接失败。

顺带一个容易踩的坑：`.bit` 和 `.hwh` 必须**基名一致**且来自**同一次** block design。
改了 PL 连线就要重新生成两个文件——只换 `.bit` 会让 PYNQ 按旧地址写寄存器，
现象是"能启动、结果全错"，比直接报错难查得多。

---

## 4. 两条独立的路径，以及"PL 是 master"

```
   ┌──────── PS（ARM / Python）────────┐
   │  Overlay / register_map           │
   └──┬───────────────────────┬────────┘
      │ 控制路径               │
      │ M_AXI_GP0（PS 是 master）│
      ▼                        │
   AXI Interconnect            │
      │                        │
      ▼                        │
   s_axi_control（IP 是从口）   │
   ┌────────────────────────┐  │
   │  demoir_conv2d_top      │  │
   │    m_axi_gmem0..3       │  │
   └──┬─────────────────────┘  │
      │ 数据路径                │
      │ IP 是 master ───────────┼──▶ S_AXI_HP0（PS 是从口）
      ▼                        │         │
   AXI Interconnect            │         ▼
      └────────────────────────┴───▶ DDR（PS 的 1 GB）
```

关键在于**数据路径上方向是反的**：`m_axi` 是 master，PS 的 `S_AXI_HP0` 是 slave。
**PL 主动去 DDR 读写，CPU 不参与搬数据。** 这正是 `pynq.allocate()` 能被 PL 直接使用的原因——
它返回的 buffer 的 `device_address` 就是物理地址，落在 HP 口能看到的 DDR 区间里。

也正因为两条路径独立，才有了缓存同步问题：

- CPU 写完输入、启动 PL 之前 → `buf.flush()`，把 cache 刷进 DDR，否则 PL 读到旧数据；
- PL 写完输出、CPU 读之前 → `buf.invalidate()`，丢弃 cache，否则 CPU 读到旧数据。

首版不用 AXI DMA、不用 AXI-Stream（`切分契约.md` §4 已定），全部走 `m_axi` 直访 DDR。
是否需要 DMA 留到 M4 实测后再定。

---

## 5. PS 侧一次调用长什么样

下面是**设计稿，不是可运行代码**——它依赖第 2 节里那三步还没产出的 `.hwh`。
写在这里是为了把"任务接口契约"（`切分契约.md` §4）落成具体形态：

```python
from pynq import Overlay, allocate

ol = Overlay('demoir.bit')                     # 读 demoir.hwh

# 缓冲区按 schedule.py 算出的 578 块布局分配（这里只演示一层）
in_buf  = allocate(shape=(256, 256, 12), dtype=np.float32)   # NHWC
out_buf = allocate(shape=(256, 256, 48), dtype=np.float32)
in_buf[:] = ...                                 # PS 侧预处理结果
in_buf.flush()                                  # ← CPU 写完必须 flush

regs = ol.demoir_conv2d_top_0.register_map
regs.in_1  = in_buf.device_address & 0xFFFFFFFF # 64 位地址被拆成两个 32 位寄存器
regs.in_2  = in_buf.device_address >> 32
regs.out_1 = out_buf.device_address & 0xFFFFFFFF
regs.out_2 = out_buf.device_address >> 32
regs.C_in  = 12;  regs.H = 256;  regs.W = 256;  regs.C_out = 48
regs.k = 5;  regs.stride = 1;  regs.dilation = 1;  regs.pad = 2;  regs.relu = 1

regs.CTRL.AP_START = 1
while regs.CTRL.AP_DONE == 0:                   # 首版轮询；中断留到有实测需求再加
    pass

out_buf.invalidate()                            # ← PL 写完必须 invalidate
```

两个只有真正上手才会发现的细节：

- **地址要拆两个寄存器。** `C_M_AXI_GMEM0_ADDR_WIDTH = 64`，但 AXI-Lite 是 32 位，
  所以一个指针占两个寄存器（`.hwh` 里会叫 `in_1`/`in_2` 之类）。只写低 32 位在
  DDR 低 4 GB 内**碰巧**能跑，一旦 `allocate` 拿到高地址就静默出错。
- **寄存器名不是我编的，是 `.hwh` 决定的。** 上面的 `in_1`、`CTRL.AP_START` 是
  HLS IP 的常规形态，最终以 M2 导出的 `.hwh` 为准。

---

## 6. 现在缺什么

按"要能在板上跑通一层 conv"的最小集排：

| # | 缺的东西 | 产出物 | 对应 M2 交付物 |
|---|---|---|---|
| 1 | `export_design` | IP 包 + 寄存器映射 | HLS/Vivado 工程 |
| 2 | block design（PS7 + 互连 + 复位 + 时钟 + IP + 地址分配） | `.bd` + tcl | HLS/Vivado 工程 |
| 3 | implementation + bitstream | `demoir.bit` + `demoir.hwh` | 匹配的 `.bit`/`.hwh` |
| 4 | PYNQ 驱动：把 `schedule.py` 的 578 块缓冲表翻译成 `allocate` + 寄存器写入 | `ps/` 下的上板运行器 | PYNQ 调用路径 |
| 5 | 板上逐元素对照 | 报告 | 板上逐元素对照 |

关于算子数量：**首版 M2 只需要 `demoir_conv2d_top` 一个 top，这就够跑通"单层闭环"了。**
另外 11 类算子要等到 M3 整网推理时才需要一个 dispatcher（或 11 个 top）。
先跑通链路，不要一次铺开。

---

## 7. 上板之前就该知道的两个风险

**风险一：577 次 dispatch 的开销还没测过。**

现在的设计是 PS 每个任务写十几个 AXI-Lite 寄存器 + 轮询 `ap_done`，一层一次往返，
577 层就是 577 次。PYNQ 的 Python 寄存器访问是微秒量级，577 次 × 每次几十 μs
就是**几十毫秒的纯调度开销**——相对 CPU 基线 5 秒不算什么，但它会把
"减少 PS 发任务次数"这件事提上 M4 的日程。

`对接文档_去摩尔纹PS-PL部署.md` §6 的 M4 已经明确把"**PYNQ 逐层调度开销**"列为
必须单独测的项，优化顺序里也写了"如果 Python 的逐层控制开销明显，先减少 PS 发任务的次数
或在 PL 加任务序列控制"。这不是遗漏，是刻意留到有实测数据再决定。

**风险二：当前 kernel 是"能综合但未优化"的。**

`demoir_top.cpp:17-19` 的注释写明了：维度全是运行期标量，所以综合出的是一份
**通用卷积器**，没有做通道分块和行缓冲，资源与 II 都不会好看。
这样做的目的是先把"能综合、能导出 IP"这条链路跑通，M2 再按实际网络的通道数
变成编译期常量并分块优化。

所以 136.99 MHz 的估算 Fmax 和 34 DSP / 9258 LUT 的数字，**是链路的可行性证据，
不是加速器的性能承诺**。真正能不能加速，要等 M2/M4 的板上测量。

---

## 8. 与其他文档的关系

| 你想知道 | 读哪份 |
|---|---|
| 为什么这么切 PS/PL、契约是什么 | `切分契约.md` |
| 原来的程序流和切分后的程序流对比 | `说明_程序流对比.md` |
| 怎么证明 C++ 和 PyTorch 算得一样 | `流程_等价性验证.md` |
| 要上板还差什么（本文） | 本文 |
| 板卡本身（PYNQ 镜像/启动）什么状态 | `../../docs/部署/README.md` |
| M2 的完整交付物与通过条件 | `../../docs/部署/对接文档_去摩尔纹PS-PL部署.md` §6 |
