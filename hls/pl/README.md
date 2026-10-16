# pl/ —— PL 侧：要综合成硬件的 C/C++

> 这个目录里的代码最终会变成 FPGA 上的电路。所以它同时要满足两件经常互相冲突的事：
>
> 1. **能在 g++ 下编译运行**，跟 `../ps/ops_ref.py` 逐算子对拍（秒级、可调试）；
> 2. **能被 Vitis HLS 综合**（循环结构、接口 pragma、不能用 `new`/`vector`/递归/浮点库函数）。
>
> 做法是**同一份源码走两条编译路径**，而不是维护两份实现 ——
> 如果为综合另写一份，等价性证明就作废了。所以综合出来的东西
> 与对拍过的那个东西**是同一个**。

## 两条编译路径

```bash
make csim     # g++ 建 build/op_runner（对拍用）。#pragma HLS 被 -Wno-unknown-pragmas 忽略
make csynth   # Vitis HLS 综合。先 source /home/zheyang/AMD/2025.2/Vitis/settings64.sh
```

| | csim | csynth |
|---|---|---|
| 谁编译 | `g++ -O2 -std=c++11` | Vitis HLS 2025.2（内部 clang-16-csynth） |
| 干什么 | 跑出结果，跟 NumPy 比 | 出 RTL + 资源/时序报告 |
| 耗时 | 秒 | 十几秒（当前规模） |
| 判据 | 逐算子误差 < 2e-5 | 无 ERROR；资源与时序能接受 |

> `vitis_hls` 这个可执行文件在 2025.2 里**已经没有了** —— HLS 并进了统一 Vitis，
> 只能用 `vitis-run --mode hls --tcl <脚本>`。`make csynth` 已经封好。

## 文件

| 文件 | 干什么 | 由谁解释 |
|---|---|---|
| `include/demoir_ops.h` | 全部算子接口 + 三条贯穿全文件的约定（NHWC 布局 / `demoir_t` / OIHW 权重） | [`../docs/切分契约.md`](../docs/切分契约.md) §3 |
| `include/npy_io.h` | 极简 `.npy` 读写。**只给测试台用，不参与综合** | 文件头注释 |
| `src/conv2d.cpp` | NHWC 直接卷积 + 可选 ReLU。占全图 99% 的 MAC | [`../docs/切分契约.md`](../docs/切分契约.md) §2.3 |
| `src/rearrange.cpp` | `pixel_unshuffle/shuffle`、`channel_shuffle/unshuffle`、`concat_c` | 同上 §2.4（concat 为什么是最大优化目标） |
| `src/resize.cpp` | 双线性缩放。**采样步长是 `1/scale_factor` 不是 `in/out`** | [`../docs/切分契约.md`](../docs/切分契约.md) §7 K1 |
| `src/channel_ops.cpp` | ReLU / sigmoid / add / 广播乘 / 全局平均 / 通道轴 1D 卷积 / ECA 融合 | 同上 §7 K4 |
| `src/demoir_top.cpp` | **唯一的 HLS 综合入口**：`m_axi` + `s_axilite` 接口 pragma 都在这里 | 同上 §4；pragma 怎么变成 AXI 端口见 [`../docs/说明_从HLS到上板.md`](../docs/说明_从HLS到上板.md) §1 |
| `tb/op_runner.cpp` | 测试台：读清单 → 执行算子 → 写 `.npy` | [`../docs/流程_等价性验证.md`](../docs/流程_等价性验证.md) §2 |
| `run_hls.tcl` | `vitis-run --mode hls` 的脚本 | [`../docs/流程_等价性验证.md`](../docs/流程_等价性验证.md) §2 ③ |

## 当前的综合结果（2026-10-08）

```text
器件   xc7z015-clg485-1      时钟约束 10 ns
资源   34 DSP (21%)  ·  9258 LUT (20%)  ·  10518 FF (11%)  ·  6 BRAM_18K (3%)
时序   估算 Fmax 136.99 MHz（余量充足）
```

⚠️ 综合的是一个**通用卷积器**（维度全是运行期标量），所以报告的 Latency 是 `?`。
它证明的是"工具链能用 + 算子写法能被综合"，**不证明整网能跑**。
整网有 247 个卷积，不可能各实例化一个；真正的做法是 M2 把通道数变成编译期常量、
一个实例按层复用。

⚠️ **`make csynth` 也不是这条链的终点。** `run_hls.tcl` 只到 `csynth_design`，
后面还有 `export_design` → block design 连线 → `.bit`/`.hwh` → PYNQ 调用，
所以**现在还不能直接上板实测**。逐环说明见
[`../docs/说明_从HLS到上板.md`](../docs/说明_从HLS到上板.md)。

## 加一个算子时

看 [`../docs/流程_等价性验证.md`](../docs/流程_等价性验证.md) §4 的 11 步清单。
要点：**接口头和实现分开改，改完立刻 `make csim` + `run_equiv.py`**，
不要攒着一起验。
