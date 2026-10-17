# pynq_specialized_code —— 去摩尔纹网络的 PS/PL 特化实现

> 目标板：**AX7015B（XC7Z015，Zynq-7000）**；工具链：Vitis HLS / Vivado / Vitis **2025.2**。
> 本目录只放**源码与配置**：`ps/` 是 ARM 侧（PYNQ Python 与裸机 C），`pl/` 是要综合成
> 电路的 HLS C++ 与测试台。**不含任何编译/构建产物**（比特流、XSA、部署包、HLS/Vivado
> 工程、上板包），生成方法见 §6。

---

## 1. 一句话架构

```
开发机（PyTorch）                        板子（AX7015B）
student 权重 + 标定图
   │ ps/tools/export_network.py
   ▼
weights.bin / index.json / 参考输入输出        ┌──────────────────────────────┐
   └──────────── 拷到 SD 卡 ─────────────────▶│ PS（ARM / PYNQ Python）       │
                                              │  读清单 → 逐算子调度          │
                                              │  卷积 → 写寄存器启动 PL        │
                                              │  层间算子 → numpy             │
                                              └───────┬──────────────▲───────┘
                                 AXI4-Lite（地址/参数/启动/状态）      │
                                              ┌───────▼──────────────┴───────┐
                                              │ PL：单卷积引擎（一块电路复用） │
                                              │  m_axi ──▶ DDR（输入/权重/输出）│
                                              └──────────────────────────────┘
```

**分工原则**：PL 只做"每个输出像素要 k×k×C_in 次乘加"的卷积；PS 做调度、层间算子
（重排/插值/拼接/加法/ECA 注意力）、标定与量化、上板对拍。整网 249 个卷积全部复用
**同一块电路**（运行期通过寄存器改形状参数）。

---

## 2. 两侧各装什么

| | `pl/`（PL 侧） | `ps/`（PS 侧） |
|---|---|---|
| 语言 | HLS C++（`ap_int` 位宽可综合） | Python（PYNQ，numpy）／ C（裸机） |
| 编译工具 | **Vitis HLS 2025.2** → IP；**Vivado 2025.2** → bit | 板上直接跑 Python；裸机用 **Vitis 2025.2** 编 ELF |
| 产物 | `design_1.bit` + `design_1.hwh`（+ 裸机路线用的 `.xsa`） | 无编译（Python）；裸机为 ELF |
| 管什么 | 卷积计算、行缓冲、权重装载、累加/右移/ReLU/饱和 | 执行清单、内存池、层间算子、寄存器驱动、对拍 |

详细清单与职责见 [`pl/README.md`](pl/README.md) 与 [`ps/README.md`](ps/README.md)。

---

## 3. 目录

```
pynq_specialized_code/
├─ README.md                 ← 本文件（总览）
├─ pl/
│  ├─ README.md              PL 侧说明：接口、资源、HLS 流程、测试清单
│  ├─ src/                   电路本体（conv2d_engine.cpp/.h）+ C 参考实现
│  ├─ src_cosim/             C/RTL cosim 专用副本（只差 m_axi depth=）
│  ├─ tb/                    测试台（引擎对拍、穷举、边界、膨胀手算…）
│  └─ scripts/               HLS 流程脚本 + 生成/检查工具 + golden 寄存器头
├─ ps/
│  ├─ README.md              PS 侧说明：每个文件干什么、寄存器契约、缓存/内存
│  ├─ pynq/                  板上运行库（调度器、层间算子、驱动、上板入口）
│  ├─ tools/                 开发机侧工具（导出/标定/检查/分块/输入准备）
│  ├─ baremetal/main.c       JTAG 裸机自检（5 用例，含 352×192 最坏形状）
│  └─ manifest_*.json        执行清单（64 / 640 / 512 / 608×416 画幅；放 ps/ 根便于工具默认读取）
└─ docs/
   ├─ pspl_interface_audit.md   PS–PL 接口审查（寄存器对表/超时/上界/性能实测）
   └─ on_board_checklist.md     上板操作清单（JTAG → PYNQ 单测 → 整网）
```

---

## 4. 构建链：谁编谁、产物去哪

| 步骤 | 在哪跑 | 命令（从仓库根） | 产物 |
|---|---|---|---|
| ① 电路综合 | 开发机 Vitis HLS | `vitis-run --mode hls --tcl pynq_specialized_code/pl/scripts/hls_run_engine.tcl` | `pl/proj/engine_v1/solution1/impl/ip/`（RTL + component.xml） |
| ② 硬件系统 | 开发机 Vivado | `vivado -mode batch -source <board>/build_bd_engine.tcl` → `build_bitstream_engine.tcl` | `design_1.bit` + `design_1.hwh` |
| ③ 裸机平台 | 开发机 Vivado | `export_xsa.tcl` | `design_1.xsa`（给 Vitis 建 platform） |
| ④ 裸机程序 | 开发机 Vitis | 新建 Platform（用 ③ 的 XSA，standalone，`ps7_cortexa9_0`）→ 把 `ps/baremetal/main.c` 加进 Application | ELF（JTAG 下载运行） |
| ⑤ 部署包 | 开发机（需 PyTorch） | `python ps/tools/export_network.py --ckpt <student.tar> --manifest ps/manifests/manifest_640.json --image <图1..4> --out <pack>` | `weights.bin / index.json / input_q.bin / ref_out.bin` |
| ⑥ 上板包 | 开发机 | `python ps/tools/make_board_bundle.py` | 一个目录（bit/hwh + 脚本 + 清单 + 部署包） |

> ②③ 的 Tcl 不在本目录（属硬件工程脚本），本仓库上游 `deploy/xsa/` 有板级 PS7 配置；
> 本工程用过的 `build_bd_engine.tcl`/`build_bitstream_engine.tcl`/`export_xsa.tcl`
> 与 `pl/scripts/hls_*.tcl` 同一套路径约定（见 `pl/README.md` §5）。

---

## 5. 上板：两条路线

**路线 A（第一次上板，推荐）：JTAG 裸机**

1. Vitis 用 XSA 建 Platform → 建 Empty Application（C）→ 加入 `ps/baremetal/main.c`；
2. 接 JTAG、上电，Debug Configuration 勾选 *Program FPGA*；
3. Run。程序跑 5 个用例（含 352×192 最坏形状、`relu_en=0` 线性用例），几秒完成；
4. 结果看 `g_report` 或内存 `0x3000_0000`（`[2]` 应为 5）。

**路线 B（最终形态）：PYNQ + Python**

1. 把上板包整个目录拷到板上（SD 卡 / scp / Samba 均可）；
2. `sudo python3 test_on_board.py` → 预期 `FCLK0 = 150.0 MHz`、`5/5 用例通过`；
3. `sudo python3 run_network_on_board.py --manifest manifest_640.json --pack weights_pack --input weights_pack/input_q.bin`
   → 预期 `与部署参考对比: 0/1228800 不一致`（整网按实测外推约 9–10 分钟，脚本每 25 个卷积打印进度）。

`.bit` 的装载方式：PYNQ 用 `Overlay("design_1.bit")`（运行时经 FPGA manager 配置 PL，掉电失效）；
JTAG 用 Hardware Manager 直接 Program Device；要开机自动加载则把 bit 打进 `BOOT.BIN`。

---

## 6. 本目录**不含**什么，以及怎么生成

| 不含 | 为什么 | 怎么生成 |
|---|---|---|
| `design_1.bit` / `.hwh` / `.xsa` | 构建产物（约 4 MB，随设计变） | §4 的 ①②③ |
| `weights_pack/`（weights.bin 等） | 导出产物（约 9 MB） | §4 的 ⑤（需 PyTorch + 标定图） |
| HLS 工程 `pl/proj/`、Vivado 工程 | 工作区，可重建（GB 级） | 跑 §4 的 ①② |
| 上板包目录 | 由上面两者拼装 | §4 的 ⑥ |
| 测试数据 `testdata_*` | 可固定种子重建 | `python pl/scripts/gen_testdata.py ...` |

---

## 7. 验证状态（2026-10-18 实测）

| 层 | 项 | 结果 |
|---|---|---|
| PL C 语义 | 形状回归（含 640 宽） | **30/30 PASS** |
| | 引擎穷举（relu=1/0 + 非法参数） | **2256 用例 PASS** |
| | 真实数据 5 场景 | 逐位一致（最大 1,572,864 输出） |
| | 4 个历史单测 | 全 PASS |
| HLS | csim | **PASS 3072 输出** |
| | csynth | 165 BRAM_18K / 56 DSP / 14,818 FF / 19,748 LUT |
| | C/RTL cosim | PASS 768 输出（352×192 k=1，2×2 图；4,142,592 拍） |
| 实现 | 时序 / 资源 | **WNS +0.087 ns @150MHz**；BRAM 82 块(86.3%)、DSP 59(36.9%)、LUT 11,699(25.3%) |
| PS | 整网调度 | 517 算子 / 247 卷积，两次运行逐位一致 |
| | 部署包自检 | 640：0/1,228,800 差异、31.52 dB；512：0/786,432、31.73 dB |
| 接口 | 寄存器对表 | 18 项全一致（`pl/scripts/check_regmap.py`） |
| | `src` ↔ `src_cosim` | 只差 `depth=`（`pl/scripts/check_src_sync.py`） |

---

## 8. 已知边界（上板前请知道）

- **性能**：按 cosim 实测外推，640 整网约 **9–10 分钟**（不是秒级）；1×1 快路径已加，
  下一刀是"权重装载从每行每块提出来"（整层装载需 ~67 KB ≈ 30 个 BRAM18K，当前 BRAM 已 86.3%）。
- **640 宽度的 RTL 级 cosim 尚未跑**（C 级已覆盖 640 宽；RTL 只跑过 2×2 / 8×8 等小形状）。
- **608×416** 只有清单，没有对应标定包；跑该画幅需按 §4-⑤ 重新导出。
- `ps/tools/test_ops_vs_torch.py` 需要 PyTorch；无 torch 的环境会显式 `SKIP(77)`。
- 板上整网尚未实测（这是下一步，按 `docs/on_board_checklist.md` 走）。
