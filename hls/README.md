# hls/ —— 去摩尔纹网络：PS 控制 / PL 算子切分与 HLS 实现

> 这条线回答一个问题：**`vendor/PDNet/model/nets_student.py` 那个 PyTorch 网络，
> 哪一部分留在 ARM 上用 Python 跑，哪一部分要写成能综合的 C/C++ 丢进 PL。**
>
> 现状（2026-10-08）：切分已定、算子已写完、**三段等价性全部通过**、
> `vitis-run --mode hls` 综合跑通（34 DSP / 21%，估算 Fmax 137 MHz）。
> 还没做的：量化（M1）、真正的加速器架构（行缓冲/并行度，M2）、板上实测。

```text
训练线                          部署线
nets_student.py  ──权重──▶  weights/student/*.tar
      │                            │
      │  逐算子/整网对照             │
      ▼                            ▼
  PyTorch  ══①══▶  ps/ops_ref.py (NumPy)  ══②══▶  pl/src/*.cpp (HLS C++)
   golden           PS 侧基准 + 板上兜底          PL 侧算子
                          │                            │
                          └───── ③ 等价性对拍 ─────────┘
                                     ▲
                                  你在这一层

  三段证据链的跑法与判据：docs/流程_等价性验证.md
  切分到底怎么切的、为什么：docs/切分契约.md
```

---

## 1. 你在什么处境？读哪份？

| 你的处境 | 去哪 | 为什么 |
|---|---|---|
| **第一次进来，想知道这里在干什么** | 本文件的第 3、4 节 | 目录结构 + 现在到哪一步了 |
| **要理解"为什么这样切"** | [`docs/切分契约.md`](docs/切分契约.md) | PS/PL 边界、张量契约、任务接口、缓冲生命周期 |
| **要理解"原来怎么跑、现在变成什么流程"** | [`docs/说明_程序流对比.md`](docs/说明_程序流对比.md) | 原始调用树 → 三段摊平（结构/内存/执行）→ PS/PL 两域时序 |
| **要跑一遍验证看看是不是真的对** | [`docs/流程_等价性验证.md`](docs/流程_等价性验证.md) | 三段证据链，每段一条命令 |
| **要动手改一个算子**（加/换实现） | [`docs/流程_等价性验证.md`](docs/流程_等价性验证.md) 第 4 节 | 加算子必须同步改哪几个文件 |
| **要开始 HLS 优化 / 定并行度** | [`docs/算子清单.md`](docs/算子清单.md) 第 4、6 节 | 每层的形状、MAC、读写量都在这 |
| **问"这堆代码能不能直接上板跑 / AXI 谁负责"** | [`docs/说明_从HLS到上板.md`](docs/说明_从HLS到上板.md) | pragma → 综合出 AXI 端口 → block design → bitstream → PYNQ，逐环说清谁做什么、现在缺哪个 |
| **要看板子那边（PYNQ 镜像/上板）现在什么状态** | [`../docs/部署/README.md`](../docs/部署/README.md) | 那是另一条线：板卡本身 |

---

## 2. 五分钟跑通

```bash
cd demoir-fpga

# 没有 torch 的机器（板上、或只想确认 C++ 与 NumPy 一致）
make -C hls check-quick SIZE=256

# 看 PS 侧到底要给 PL 发什么、要多少 DDR（不需要板子，也不需要 torch）
make -C hls plan

# 有 torch 的机器：连"NumPy 是否等于 PyTorch"一起验
make -C hls venv       # 第一次才需要，建 .venv-hls 并装 torch-cpu
make -C hls check SIZE=256

# 有 Vitis 的机器：综合出资源报告
source /home/zheyang/AMD/2025.2/Vitis/settings64.sh
make -C hls csynth
```

`SIZE` 是测试用的输入边长（会等比缩放到 SIZE×SIZE 再补边）。**它只影响测试速度，
不影响结论**：正式推理不做缩放（理由见 `src/infer.py` 文件头）。

---

## 3. 目录结构

**每个子目录有自己的 README**，说明里面的文件各干什么、由哪份文档解释 ——
这里只给地图，不重复内容（两份清单必然有一份先过期）。

```text
hls/
├─ README.md                  ← 你在看的这个，本线唯一入口
├─ Makefile                   全部命令在这里（venv / weights / pl / check / csynth）
│
├─ ps/        ──▶ ps/README.md       ★ PS 侧：PYNQ 的 ARM 上跑的 Python（只依赖 NumPy）
├─ pl/        ──▶ pl/README.md       ★ PL 侧：要综合成硬件的 C/C++（g++ 与 HLS 同一份源码）
├─ tools/     ──▶ tools/README.md    ★ 只有开发机用（需要 torch）：权重导出、算子清单
├─ tests/     ──▶ tests/README.md    ★ 三段等价性验证
│
├─ docs/
│  ├─ 切分契约.md              ★ 为什么这样切：判据、契约、归属、踩过的坑
│  ├─ 说明_程序流对比.md        ★ 原来怎么跑 → 现在 PS/PL 各跑什么（含完整时序）
│  ├─ 说明_从HLS到上板.md       ★ 离上板还差什么：AXI 谁例化、PYNQ 在哪一环
│  ├─ 流程_等价性验证.md        ★ 三段证据链怎么跑、判据是什么、证明不了什么
│  └─ 算子清单.md              **生成物**（tools/op_inventory.py），不要手改
│
└─ build/                     构建产物（gitignore）
   ├─ weights/student.npz     .tar 摊平后的权重 + manifest.json
   ├─ equiv/                  C++ 对拍用的 .npy 用例 + report.md
   └─ …                       HLS 工程在 pl/build/hls/
```

| 你想干的事 | 去哪个 README |
|---|---|
| 改 PS 侧的图/参考算子 | [`ps/README.md`](ps/README.md) |
| 改一个 PL 算子、或跑综合 | [`pl/README.md`](pl/README.md) |
| 换权重、重算预算 | [`tools/README.md`](tools/README.md) |
| 某条对拍用例挂了 | [`tests/README.md`](tests/README.md) → [`docs/流程_等价性验证.md`](docs/流程_等价性验证.md) §3 |

---

## 4. 现在到哪一步了（都是实测数字，不是估计）

| 环节 | 状态 | 数字 |
|---|---|---|
| 切分（哪些算子进 PL） | ✅ 已定 | 12 类算子**全部**进 PL；PS 侧只剩调度/补边/量化/I/O |
| PS 侧执行计划 | ✅ 已生成并自检 | 577 条任务 / 578 块缓冲；缓冲复用 9.9×（2069 MB → **210 MB**）+ 权重 5.9 MB，合计 **216 MB**，1 GB DDR 装得下 |
| ① NumPy vs PyTorch（逐算子） | ✅ 通过 | 39 项覆盖全部参数组合，最大误差 ≤ 2.5e-5（fp32 累加顺序差） |
| ① NumPy vs PyTorch（整网） | ✅ 通过 | 256×256：`max|Δ| = 1.3e-6`（PSNR 132 dB）；**全分辨率 1728×1280 也是 2.0e-6**（135 dB），含补边/裁回 |
| ① 端到端（补边→整网→裁回） | ✅ 通过 | 249×243 → 补边 (3,6,4,7)：`1.6e-6`；全分辨率 1706×1279 → 补边 (11,0,11,1)：`2.0e-6` |
| ② C++ vs NumPy（逐算子） | ✅ 通过 | 64 条用例 / 12 类算子，`conv2d` 最大 2.4e-6，其余多为 0 |
| ③ HLS 综合 | ✅ 能跑通 | `xc7z015-clg485-1`，34 DSP(21%) / 9258 LUT(20%) / 10518 FF(11%) / 6 BRAM(3%)，估算 Fmax **136.99 MHz** |
| 算子清单 / 预算 | ✅ 已实测 | 512×512 单图 **21.28 G MAC**，权重 5.90 MB fp32，张量峰值驻留 131 MB |
| 量化（M1） | ⬜ 未开始 | 算子清单第 5 节给了 INT8 后的量级 |
| 真正的加速器架构（M2） | ⬜ 未开始 | 当前 kernel 是"可综合但未优化"的通用卷积器 |
| ③ 之后的链路：导出 IP | ⬜ 未开始 | `run_hls.tcl` 只到 `csynth_design`，还没跑 `export_design`，**寄存器地址映射表还不存在** |
| ③ 之后的链路：block design + bitstream | ⬜ 未开始 | 没有 PL 连线、没有 `.bit`/`.hwh`（`deploy/xsa/` 那份 tcl 是 M0 修 PS7 外设用的，不含 PL） |
| ③ 之后的链路：PYNQ 调用代码 | ⬜ 未开始 | `schedule.py` 算出了 578 块缓冲，但还没翻译成 `allocate` + 寄存器写入 |
| 板上实测 | ⬜ 未开始 | 板子已能启动 PYNQ，见 `../docs/部署/README.md` |

> 上面「③ 之后的链路」三行合起来的意思是：**现在还不能直接上板实测**。
> 逐环拆解见 [`docs/说明_从HLS到上板.md`](docs/说明_从HLS到上板.md)。

**关于那个 34 DSP / 137 MHz 要泼的冷水**：它综合的是**一个**通用卷积器实例，
维度全是运行期标量，所以 HLS 报的 Latency 是 `?`（无法静态展开）。这证明了
"工具链能用、算子能综合"，**不证明**能跑得动整网 —— 整网有 247 个卷积，
不可能各实例化一个。真正的做法是 M2 把通道数变成编译期常量、一个实例按层复用。

---

## 5. 切分的结论（一句话版）

**PL 只做"每个输出像素需要 k×k×C_in 次乘加或一次结构化搬运"的算子；
PS 只做"和像素数无关、或必须串行看整张图"的事。**

于是：卷积、双线性缩放、通道重排、逐元素、全局平均、ECA 全进 PL；
读图 / 补边 / 量化 / 发任务 / 等中断 / 拼结果全留 PS。

PS 侧因此交付两样东西，都不碰硬件：

* `ps/run_ref.py` —— 算法基准（也是板上兜底）：照图算一遍，验证结果；
* `ps/schedule.py` —— **要发给 PL 的东西**：静态任务表 + DDR 缓冲分配表。

**细节、判据、以及为什么 `concat` 反而最该被融合掉，见
[`docs/切分契约.md`](docs/切分契约.md)。**

---

## 6. 下一步

1. **M1 量化**：先量化 `ops_ref.py` 的 NumPy 参考（改 `demoir_t` 一处），
   用 `docs/算子清单.md` 第 5 节的张量量级定累加器位宽。
2. **M2 第一个真算子**：挑 `dec.preconv_1`（4.23 G MAC，全图最贵，
   3×3/112→64/256×256）做行缓冲 + 通道分块，同一个 `op_runner` 当测试台。
3. **接口定稿**：`pl/src/demoir_top.cpp` 现在是"一层一次调用"，
   要按 `docs/切分契约.md` §4 的任务描述符扩展。
4. **补上板链路**：`export_design` → block design（PS7 + 互连 + 复位 + 时钟
   + `demoir_conv2d_top` + 地址分配）→ `.bit`/`.hwh` → PYNQ 调用代码。
   每一环要产出什么、为什么 PYNQ 替不了这几步，见
   [`docs/说明_从HLS到上板.md`](docs/说明_从HLS到上板.md) 第 2、6 节。
   首版只需要 `demoir_conv2d_top` 一个 top 就能跑通 M2 的"单层闭环"。
