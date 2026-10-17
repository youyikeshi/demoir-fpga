# 去摩尔纹模型的 PS–PL 架构说明

> 阅读场景：想了解 student 模型如何从开发机部署到 AX7015B，以及 PS、PL、DDR 和 PYNQ 的分工。
> 本文记录截至 2026-10-17 可在本地核对的架构与验证状态。具体命令见各目录的 README。
> 目标板卡为 AX7015B（XC7Z015，Zynq-7000）。首轮验收聚焦 SD 卡单图推理，相机接入留待后续。

---

## 1. 先区分两份工程现状

本文所在的 `restructure` 目录与本地 `fpga-deploy` 工作树进度不同。下文提到“已完成”时，须结合对应工作树理解。

| 范围 | 已有内容 | 当前边界 |
|---|---|---|
| `restructure`（本文所在目录） | `deploy/` 的 PYNQ 板卡环境已上板实测；`hls/` 已有 12 类算子的 HLS 源码、PS 调度计划和等价性验证 | `hls/` 尚未完成量化、模型 overlay 和 PYNQ 整网驱动 |
| 本地 `fpga-deploy` 工作树 | 新增 `fpga/`，包括 INT8 部署包、PS 调度与层间算子、单卷积引擎、Vivado 集成产物和本机回归 | 这些提交尚未合入 GitHub `restructure`；模型整网尚未在实板对拍 |

`fpga-deploy` 的工程入口是 `fpga/README.md`，交接要求对照在 `fpga/docs/交接要求对照.md`。
这两个文件位于另一份本地工作树。**当前 `restructure` 目录里还没有 `fpga/`**。
下文引用 `fpga/` 时，均指本地 `fpga-deploy` 的实现，供后续合流参考。

## 2. 一张图看完运行路线

```text
开发机：PyTorch student + 已训练权重 + 原始照片
          │ 导出执行清单、INT8 权重、标度、参考输出；照片裁剪为 input_q.bin
          ▼
板上 SD 卡：PYNQ 镜像 + 同次构建的 .bit/.hwh + 部署包 + input_q.bin
          │
          ▼
PS（ARM / Python）：读取 input_q.bin、加载 overlay、分配 DDR、按清单调度
          │                     │
          │ AXI4-Lite：地址、尺寸、卷积参数、启动与状态
          ▼                     │
PL：复用的卷积引擎 ──AXI4 master / PS HP 口──▶ DDR：输入、权重、中间特征、输出
          │                                                │
          └── 完成 / 错误状态 ──▶ PS：层间算子、下一层调度、结果对拍
```

PYNQ 是运行在 **PS 上的 Python 框架**，提供 overlay 加载、硬件缓冲区分配和 IP 控制接口。
PL 运行的是综合后形成的硬件电路。`nets_student.py` 定义网络结构，训练权重提供参数；两者都需要经过部署转换。
现有单引擎用 Vitis HLS C++ 描述，由工具生成 Verilog RTL 和比特流。
如果交付要求必须包含手写 Verilog，还需要另写 RTL，并与现有引擎逐层对拍。

### 2.1 开发机先生成什么

| 顺序 | 输入 | 生成物 | 谁负责生成 |
|---|---|---|---|
| 1 | `vendor/PDNet/model/nets_student.py` 与 student `.tar` | 浮点推理基准、逐层名称和权重来源 | `src/infer.py` 与 `fpga/ps/tools/export_network.py` |
| 2 | 确定的输入画幅 | `manifest_640.json` 等执行清单，记录算子顺序、张量形状和依赖 | `fpga/ps/tools/build_manifest.py` |
| 3 | 执行清单、权重和多张标定图 | `weights_pack/`，包含 INT8 权重、标度和参考输入输出 | `fpga/ps/tools/export_network.py` |
| 4 | PL 引擎源码与 AX7015B 硬件设计 | `design_1.bit`、同名 `.hwh` 和可复核的综合/实现报告 | Vitis HLS 与 Vivado |
| 5 | 上述产物与 PS 运行脚本 | 供拷贝到板子的 `board_bundle/` 目录 | `fpga/ps/tools/make_board_bundle.py` |

板卡 PYNQ 镜像由 [`deploy/`](../../deploy/) 构建；`board_bundle/` 收集的是模型 overlay 和运行文件。
`board_bundle/` 由打包脚本生成，当前 `restructure` 目录中没有它。

### 2.2 一张 640×640 图片在板上怎么走

上板脚本位于 `fpga/ps/pynq/run_network_on_board.py`。
目前它读取**已量化的** `input_q.bin`，尚未在板上解码 JPG，也未将结果保存为 PNG。
处理自己拍的照片时，先在开发机用 `prep_input.py` 原生裁剪并量化，再将生成的二进制文件交给上板脚本。

| 次序 | 运行位置 | 实际动作 |
|---|---|---|
| 1 | PS | 核对 `input_q.bin` 的字节数是否等于 `3×H×W`，再读成 `(3,H,W)` 的 INT8 张量 |
| 2 | PS | `Overlay` 加载 `.bit` 并解析同名 `.hwh`；读取清单与部署包 |
| 3 | PS / DDR | `PYNQArena` 分配中间张量池；`PYNQEngine` 另分配权重池，把 `weights.bin` 装入并刷新缓存 |
| 4 | PS | `Runner` 过滤清单中不参与最终输出的任务，然后逐条执行 |
| 5 | PS → PL → DDR | 遇到卷积时，PS 写地址和参数；PL 从 DDR 读取输入和权重、写回输出；PS 等待完成并使缓存失效 |
| 6 | PS | 遇到重排、插值、拼接、加法或 ECA，由 `ops_numpy.py` 处理，再继续下一条任务 |
| 7 | PS | 取最终 `out_1` 对应张量，与包内 `ref_out.bin` 做逐元素比较并打印不一致数量 |

640×640 清单共有 521 条任务，其中 517 条参与最终输出，包含 247 条卷积；清单还描述了 522 个张量。
第 0 条任务是 `pixel_unshuffle`，PS 将 `(3,640,640)` 转为 `(12,320,320)`。
第 1 条是 `encoder.conv_first`：PL 执行 5×5、输入 12 通道、输出 48 通道的卷积，得到 `(48,320,320)`。
此后 PS 和 PL 继续按清单协作，所有卷积都复用同一块电路。

## 3. 代码与产物各放在哪里

下表按仓库顶层目录说明职责。“板上需要”指运行时所需的文件或产物，无须把整目录原样复制过去。

| 顶层路径 | 谁使用 | 在架构中的职责 | 板上需要什么 |
|---|---|---|---|
| [`src/`](../../src/) | 开发机 | `infer.py` 建立浮点输入输出基准；`tiling.py` 定义大图分块与羽化拼接 | 板上 PS 应实现相同的预处理与分块规则；无需整套训练源码 |
| [`vendor/PDNet/model/nets_student.py`](../../vendor/PDNet/model/nets_student.py) | 开发机、PL 开发 | student 的网络结构与计算顺序依据 | 按网络图实现 PS 调度和 PL 算子；原 `.py` 不由 PL 执行 |
| [`weights/student/`](../../weights/student/) | 开发机 | 浮点训练权重来源 | 导出后的 INT8 权重、标度和清单；原 `.tar` 不直接供 PL 读取 |
| [`hls/ps/`](../../hls/ps/) | 开发机 | NumPy 算法参考、计算图与静态任务/缓冲计划 | 目前是参考实现，尚不能直接充当完整的上板应用 |
| [`hls/pl/`](../../hls/pl/) | PL 开发 | 可综合的 HLS 算子及测试台 | 综合、集成后生成的硬件，不是板上运行的 C++ 文件 |
| [`deploy/`](../../deploy/) | 镜像构建、板卡 | AX7015B 的 XSA、PetaLinux、PYNQ 镜像与验收脚本 | 与该板匹配的启动镜像和 PYNQ 环境 |
| `fpga/`（仅本地 `fpga-deploy`） | PS、PL 开发 | 已量化的部署实现；见下表 | 待实板验收后，按 `fpga/README.md` 制作上板包 |

`fpga-deploy` 将模型部署细分为以下部分。**这些路径属于另一份本地工作树，当前目录中尚不存在。**

| 路径 | 归属 | 具体内容 |
|---|---|---|
| `fpga/ps/tools/` | 主要在开发机 | `export_network.py` 导出并标定；`prep_input.py` 原生裁剪；`tiled_infer.py` 分块、羽化；`check_pack_ref.py` 复算部署参考 |
| `fpga/ps/manifest_*.json` | PS | 固定画幅的执行顺序、张量与缓冲描述；换画幅需要匹配的清单 |
| `fpga/ps/pynq/runner.py` | PS | 读取清单和部署包，规划 DDR 内存池，逐项执行 517 个有效任务 |
| `fpga/ps/pynq/ops_numpy.py` | PS | `pixel_shuffle/unshuffle`、插值、拼接、加法、ECA 等层间算子 |
| `fpga/ps/pynq/conv_engine.py` | PS 控制 PL | 借助 overlay 的 `.hwh` 元数据定位 IP，写入卷积参数、启动引擎并读取状态 |
| `fpga/pl/src/conv2d_engine.cpp/.h` | PL 源码 | INT8 单卷积引擎；卷积参数经 AXI4-Lite 配置，输入、权重和输出经 AXI4 master 访问 DDR |
| `fpga/board/overlay_engine_150mhz/` | PL 交付物 | 同次 Vivado 构建的 `design_1.bit` 与 `design_1.hwh` |
| `fpga/deploy/weights_pack/` | PS 加载、PL 读取 | 已导出的量化权重、标度、输入与参考输出；PS 放入可访问的 DDR 缓冲区 |

### 3.1 当前上板包的文件分工

`fpga-deploy` 的打包脚本把上板文件集中到 `fpga/board/board_bundle/`。
该目录由脚本生成，主要包含以下文件。

| 上板包内路径 | 运行时交给谁 | 含义 |
|---|---|---|
| `design_1.bit` + `design_1.hwh` | PYNQ / PL | 配置电路、解析 IP 与寄存器；两个文件须来自同一版设计 |
| `run_network_on_board.py` | PS | 单张 INT8 输入的整网执行与对拍入口 |
| `runner.py`、`ops_numpy.py`、`conv_engine.py` | PS | 任务调度、层间算子、卷积引擎寄存器驱动 |
| `manifest_640.json` | PS | 640×640 画幅的任务顺序、形状、张量依赖 |
| `weights_pack/weights.bin`、`index.json` | PS 加载，PL 读取卷积权重 | INT8 权重、INT32 偏置、偏移、标度、层参数 |
| `weights_pack/input_q.bin`、`ref_out.bin` | PS | 冻结的量化输入和期望输出，用于首次整网对拍 |
| `test_on_board.py`、`ref_numpy.py` | PS | 板上单卷积自检与 NumPy 参考 |
| `main.c` | Vitis 裸机自检 | JTAG 路线的 PS 测试程序；PYNQ 整网运行不用它 |

打包脚本还会附上格式检查工具、溯源文件和 `BOARD_README.txt`。
`samples/phone/` 只提供可选测试照片。训练数据、teacher、优化器和研究脚本不进入首轮板上推理包。

### 3.2 源码、构建产物与运行文件

`fpga/pl/src/conv2d_engine.cpp` 是硬件设计源码。Vitis HLS 将其转换成 RTL/IP，再由 Vivado 生成 `.bit/.hwh`。
修改这份 C++ 源码后，需要重新综合、实现并生成上板包，PL 电路才会更新。
`runner.py` 和 `ops_numpy.py` 则在 PS 上运行；修改它们不会改变 `.bit`。
两侧都依赖执行清单和权重格式。调整接口时，须一并核对相关文件。

## 4. PS、PL 与 DDR 的边界

### 4.1 PS 保持全网顺序

PS 通过 `Runner` 读取 manifest 中的 `ops` 和 `buffers`，只执行 `needed=true` 的任务。
卷积交给 `PYNQEngine`；`pixel_unshuffle/shuffle`、`interp`、`slice`、`cat`、`add`、`eca` 和 `sam_sum` 在当前实现中交给 `ops_numpy.py`。
层间算子还要按各张量的标度重新缩放输入，才能保持量化结果一致。

源网络定义了 249 次卷积调用。当前清单跳过只用于辅助输出 `out_2/out_3` 的两条卷积及其后续 shuffle，
同时保留下一层仍需使用的解码特征，因此最终执行 247 条卷积任务。
实际执行范围由清单中的 `needed` 字段确定。

### 4.2 PS 怎样启动一次 PL 卷积

`conv_engine.py` 使用两组 AXI4-Lite 寄存器：`s_axi_control` 接收四个 DDR 设备地址，
`s_axi_ctrl` 接收形状与卷积参数，并提供启动、完成及返回码。
PS 优先从 overlay 的 `.hwh` 元数据读取接口基地址。若元数据不完整，现有驱动会使用该版比特流的固定地址。
更换硬件设计后，应重新核对这些地址。

```text
PS：准备 input / weight / bias / output 的 DDR 区间，刷新 CPU 缓存
  │
  ├── 写四个 device_address（地址来自 pynq.allocate 的池基址 + 偏移）
  ├── 写 H、W、Hout、Wout、cin、cout、k、stride、dilation、pad、shift、relu_en
  ├── 写 ap_start；轮询 ap_done，超过驱动超时就报错
  └── 读返回码；使输出池的 CPU 缓存失效，再让后续 PS 算子读输出
```

当前 `PYNQEngine` 每次卷积都对整个中间池调用 `flush()` 和 `invalidate()`。
整池同步便于先核对数值，但会增加耗时。上板测量后，可考虑只同步相关张量所在的区间。
驱动对单次卷积的默认等待上限为 5 秒；该上限是否合适，还须在板上验证。

### 4.3 PL 与 DDR 怎么配合

`fpga-deploy` 用一块 INT8 卷积引擎处理网络中的各种卷积。
PS 在运行时设置通道数、核大小、步长、膨胀率、补边、右移量和 ReLU 开关，PL 按这些参数复用电路。
PL 用 AXI4 master 经 PS 的 HP 口从 DDR 读取输入、权重和偏置，并把输出写回 DDR。
PL 内部的行缓冲保存当前输出块需要的输入行；权重缓冲按 8 个输出通道一组轮转装载。

当前引擎的上限参数为 `CIN_MAX=352`、`COUT_MAX=192`、`OC_BLOCK=8`。
它支持 1×1、3×3、5×5 卷积核，步长为 1 或 2，膨胀率为 1～3。参数超出范围时，引擎返回尺寸错误。
这些数值约束属于当前实现。BRAM 只保存计算时使用的局部数据；整网权重、跳连特征、
SAM 多分支特征和其他中间张量仍放在 DDR 中。

### 4.4 两条实现线的接口差异

| 项目 | `restructure/hls/` 参考线 | 本地 `fpga-deploy/fpga/` 实现线 |
|---|---|---|
| PL 的目标算子 | 12 类算子，包含卷积、重排、插值和 ECA | 已集成为单卷积引擎；层间算子仍由 PS 的 NumPy 执行 |
| 数值类型 | 现有 `demoir_t` 为 float | 卷积输入/权重为 INT8，偏置为 INT32；ECA 等保留部分浮点处理 |
| 张量排列 | HLS 契约计划使用 NHWC | 单引擎与 `Runner` 使用 CHW；卷积权重按输出通道、输入通道、核行、核列排列 |
| 成熟度 | 算子等价性、综合通过；缺模型 overlay | 本机量化、单引擎 RTL 验证、比特流与 PS 驱动已有；板上整网待验证 |

合并两条实现线之前，需要统一张量排列、数值格式、标度、寄存器接口和任务顺序，并重新逐算子对拍。
首版可沿用 `fpga/` 的卷积引擎。是否将 `hls/` 的层间算子移入 PL，应根据板上耗时和 DDR 流量决定。

## 5. 输入、权重和结果的约定

### 5.1 画幅和输入文件

推理画幅的高和宽必须是 32 的倍数，且与 manifest 的 `input_hw` 一致。
处理真实照片时须保留原生像素频率。`prep_input.py` 默认只裁剪；图片尺寸不足时直接报错。
它把原始 RGB 像素 `p∈[0,255]` 按 `clip(rint(p/255×127),0,127)` 量化，
输出 `(3,H,W)` 的 INT8 原始字节。
640×640 输入因此占 `3×640×640 = 1,228,800` 字节。

大图分块和羽化规则见 [`说明_分块推理与拼接.md`](../训练/说明_分块推理与拼接.md)。
`fpga/ps/tools/tiled_infer.py` 已实现分块与羽化。当前上板整网脚本只接收单块 `input_q.bin`。
要在板上直接读取照片、处理大图并保存结果，还需要扩展应用入口。

### 5.2 manifest 与部署包怎么配套

| 文件 | 核心字段或内容 | 消费者 |
|---|---|---|
| `manifest_640.json` | `input_hw`、`ops`、`buffers`、`io`；每个卷积任务含输入输出张量、`weights_key`、形状与核参数 | `Runner` 和 `PYNQArena` |
| `weights_pack/index.json` | `layers` 的权重/偏置偏移和 `shift`，以及 `scales`、`eca` | `Runner` 和 `PYNQEngine` |
| `weights_pack/weights.bin` | 连续存放的 INT8 卷积权重与 INT32 偏置；偏移由 `index.json` 解释 | PS 装入 DDR，PL 按地址读取 |
| `weights_pack/input_q.bin` | 冻结的量化输入样例 | 板上首次整网对拍 |
| `weights_pack/ref_out.bin` | 与该部署包同口径的量化期望输出 | 板上逐位比较 |
| `weights_pack/build_info.json` | 权重来源、标定图数量、量化口径和各文件校验值 | 开发机核对版本 |

例如，清单中的 `encoder.conv_first` 会通过 `weights_key` 找到 `index.json` 中对应的权重条目。
该条目记录 `w_off`、`w_len`、`b_off`、`b_len` 和 `shift`。
PS 先用这些偏移算出 DDR 设备地址，再把地址与卷积参数写给 PL。
清单、`index.json`、`weights.bin` 和 `.bit/.hwh` 的版本必须相互匹配。

### 5.3 内存生命周期与输出判断

`PYNQArena` 根据每个张量最后一次被使用的任务，规划一个连续 DDR 池和 64 字节对齐的偏移。
640×640 清单在本机规划的池大小约为 84.8 MB。实板运行前还须确认连续内存（CMA）足够。
跳连与 SAM 分支会跨多条任务存活，不能在当前卷积结束时立即覆盖。

首次验收先比 PL 单卷积，再运行完整清单并与 `ref_out.bin` 逐位比较。
浮点 student 与量化输出之间的 PSNR 用来衡量画质；逐位比较则用于检查板上实现是否符合量化参考。
目前尚无板上整网比较结果。

上述字段的设计依据见 [`对接文档_去摩尔纹PS-PL部署.md`](对接文档_去摩尔纹PS-PL部署.md) 第 3～5 节。
`restructure` 的原始任务契约见 [`../../hls/docs/切分契约.md`](../../hls/docs/切分契约.md)。

## 6. 验证状态与下一步

| 层次 | 已有证据 | 还需完成 |
|---|---|---|
| 板卡 PYNQ 基础 | `restructure` 的镜像已在 AX7015B 启动，串口、网口、`import pynq` 等通过；见 [`部署/README.md`](README.md) | 用匹配的模型 overlay 复测分配、寄存器和数据通路 |
| `hls/` 参考线 | PyTorch、NumPy、C++ 三段等价性和算子综合已通过；见 [`../../hls/README.md`](../../hls/README.md) | 完成量化、IP 导出、Vivado 集成与上板调用 |
| 本地 `fpga-deploy` 实现线 | INT8 参考包、单引擎 csim/cosim、Vivado 比特流与本机对拍已有记录 | JTAG 单算子、PYNQ 单算子、板上整网逐位对拍仍待实测 |

合流时，应先核对两条线的权重标度和张量排列，再按 `fpga/docs/on_board_checklist.md` 的顺序测试。
量化离群值、608×416 画幅的完整标定包、SSIM，以及层间算子是否移入 PL，仍待处理。
逐项状态见 `fpga/docs/交接要求对照.md`。

### 6.1 首次实板验证顺序

1. 核对板卡 PYNQ 环境、PL 时钟、`.bit/.hwh` 版本和连续内存配置。
2. 用 JTAG 裸机或 PYNQ 单算子测试检查卷积 IP、DDR 地址、缓存和返回码。
3. 使用冻结的 `input_q.bin` 与 `ref_out.bin` 跑整网，要求不一致数量为 0。
4. 记录单次卷积、整网、缓存同步和层间 NumPy 算子的耗时，再判断是否值得把更多算子移到 PL。
5. 接入自己拍的照片与图片保存；画幅不一致时先在开发机生成匹配清单及标定包，再做分块应用。

板卡基础镜像和本地 `fpga/board/` 的 overlay 来自两条工程线。
该 overlay 的 PS7 工程主要配置 DDR、AXI 和时钟。将它加载到现有 PYNQ 镜像后，
须在实板核对时钟、连续内存、SD 卡和网口状态。

### 6.2 当前最容易误读的地方

`restructure` 的 12 类 HLS 算子已通过对拍和综合，尚未作为完整模型在 AX7015B 的 PL 上运行。
`fpga-deploy` 已生成比特流和驱动文件，但整网仍待实板验收。
`run_network_on_board.py` 目前读取 INT8 二进制输入并对拍结果；照片输入与 PNG 输出仍待接入。

## 7. 从哪里继续读

| 你的问题 | 去哪 | 原因 |
|---|---|---|
| 板卡镜像怎么构建、已经验收到哪 | [`部署/README.md`](README.md) | 部署线的状态与操作入口 |
| PS/PL 的算子边界为什么这样画 | [`../../hls/docs/切分契约.md`](../../hls/docs/切分契约.md) | 张量、任务和缓冲的原始契约 |
| HLS 源码离上板还差什么 | [`../../hls/docs/说明_从HLS到上板.md`](../../hls/docs/说明_从HLS到上板.md) | 从 IP 导出到 `.bit/.hwh` 的各环节 |
| 大图为什么要分块，怎么拼 | [`../训练/说明_分块推理与拼接.md`](../训练/说明_分块推理与拼接.md) | 原生画幅与羽化规则 |
| 接手模型上板时的阶段和验收物 | [`对接文档_去摩尔纹PS-PL部署.md`](对接文档_去摩尔纹PS-PL部署.md) | M−1 至 M5 的交付顺序 |
