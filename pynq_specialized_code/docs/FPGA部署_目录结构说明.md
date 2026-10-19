# 目录结构说明（demoir-pl-lab · FPGA 工程）

> 本文档记录 2026-10-16 的**结构落定**：把"去摩尔纹学生网 → FPGA 加速"
> 这段工程的目录、架构与来龙去脉写清楚。
> 增补（同日）：与上游库的对接定位（§1）、分块/行带形态与换画幅实测（§3.5）。
> 姊妹篇：上游 Python 侧（demoir-fpga 仓库）的《目录结构说明》。
> 日常入口：`pynq_specialized_code/README.md`（总览）与
> `pynq_specialized_code/ps/README.md`（操作手册）；
> **2026-10-18（分支 `restructure1`）**：本工程代码已按 PS/PL 两侧收进
> `pynq_specialized_code/`（`ps/`、`pl/`），**只含源码与配置**；
> 比特流 / XSA / 部署包 / HLS·Vivado 工程属构建产物，不收录，
> 生成方法见 `pynq_specialized_code/README.md` §6。
> 开发工作区里另有 `project_index.html`（目录导航）。
> **读本文先把 §2.1 的"入库后位置对照"看一眼**（正文路径是工作区旧布局）。

---

## 1. 这个工程是什么

上游 demoir-fpga 的学生网是 **Python（PyTorch）**；本工程把它变成
**"PL 电路 + PS 调度"**的 FPGA 部署，跑在 AX7015B（XC7Z015）上：

```
Python 学生网（249 次卷积）
   │  export_network.py：浮点前向 + 4 图标定 → int8 权重 + 逐张量标度
   ▼
weights_pack/ + manifest_*.json（执行清单：247 卷积 + 270 层间算子；
尺寸参数化：512 / 640 / 608×416 均已实测生成）
   │  runner.py 调度（本机 RefEngine / 板上 PYNQEngine 同一份代码）
   ▼
PL 卷积引擎（同一块电路被调用 249 次）  ⇄  PS 层间算子（重排/拼接/插值/ECA）
   ▼
板载整网推理 → 与参考输出逐位对拍
```

一句话：**"用一块 Verilog 电路，把 Python 网络里的卷积全部替换掉"**——
其余（数据搬运、融合、插值、注意力）留在 PS 上用 numpy 完成。

**与上游仓库的关系**：上游 demoir-fpga 的路线把工作分为三期，其中
**Phase 3 = 量化与 FPGA 部署**（2026-09 交接文档中标记"尚未开始"）——
**本工程就是 Phase 3 的实施**。上游给 Phase 3 定的接口条件与我们的对齐：

| 上游条件 | 本工程状态 |
|---|---|
| 权重约 1.5MB、BRAM 放不下 → 挂 DDR | ✅ 权重池走 DDR |
| 量化注意"摩尔纹激活有离群值" | ✅ 先以 4 图标定 PTQ 落地（31.52dB），离群值处理列为后续优化 |
| 部署形态 = 分块/行带流式 | ⚠️ 见 §3.5：现状为整图逐层 offload（已验证），流式为远期演进 |

## 2. 六个分区（+ 归档规则）

| 分区 | 谁的 | 进 git | 说明 |
|---|---|---|---|
| `01_hls/` | 电路源码与 HLS 工程 | 源码 ✅ / 工程 ❌ | `src/` 是电路唯一本体，改它＝改电路；`proj/` 是 HLS 工作区（1.5GB，可重建） |
| `02_vivado/` | 比特流与构建脚本 | 交付物 ✅ / 工程 ❌ | `overlay_engine_150mhz/` 是成品；`proj_*/` 是工作区 |
| `03_ps/` | PS 调度、量化、上板 | ✅（`logs/` 除外） | `pynq/`（运行库）`tools/`（导出与检查）`baremetal/`（JTAG 自检）`weights_pack/`（部署包） |
| `board_bundle/` | 上板包（8.2MB） | ✅ | 由 `make_board_bundle.py` 一键生成，不手工改 |
| `notes/` | 文档、图、报告 | ✅ | `figs/` 图、`reports/` 机器报告、其余为 .md 文档 |
| `docs/` | 早期设计文档 | ✅ | 只读存档 |
| `archive/`（各目录内） | 实验副本 | ❌ | 对比实验的副本用完进这里（或删），不占顶层 |

### 2.1 入库后的位置对照（读本文时按此换算）

2026-10-16 起，这套工程已按仓库 `src/` 原则整理目录；在 **`restructure1`** 分支上
落在 **`pynq_specialized_code/`**（下表的"位置"列即该目录内的相对位置）
（即上游仓库的 **Phase 3** 实施）。本文档正文沿用开发工作区的布局，
与仓库布局的对应关系：

| 本文档里的路径 | 仓库里的位置 |
|---|---|
| `01_hls/` | `pynq_specialized_code/pl/`（电路 `src/`、测试台 `tb/`、脚本 `scripts/`；工程 `proj/` 不入库） |
| `02_vivado/` | **本分支不收录**（比特流/XSA 是构建产物，按 README §4 生成） |
| `03_ps/` | `pynq_specialized_code/ps/`（`pynq/` 运行库、`tools/` 导出与检查、`baremetal/`、`manifest_*.json`） |
| `03_ps/weights_pack/` | **本分支不收录**（部署包按 `ps/tools/export_network.py` 生成） |
| `board_bundle/` | **本分支不收录**（`ps/tools/make_board_bundle.py` 一键生成） |
| `notes/*.md`、`notes/figs` | `pynq_specialized_code/docs/`（机器报告不入库） |

完整的目录说明与"clone 即用"命令见 **`pynq_specialized_code/README.md`**。

## 3. 关键机制

### 3.1 单引擎：249 个卷积共用一块电路（含一段架构演变）

开发早期按"六种卷积形态"做过**六个特化 IP**（`01_hls/proj/` 里
`c112_o64_*`、`c12_o48_*`、`c16_o16_*`、`c48_*` 六个工程就是那个世代的证据），
后来演进为**单引擎方案**：参数（cin/cout/k/s/d/pad/shift/relu）全部走寄存器，
形态差异用"换权重 + 换参数"表达——**一份电路覆盖全部 249 个卷积**。

v3.2（当前版）把存储上界扩到全网络最坏形状（**CIN 352 / COUT 192**），
并把两处装载地址改为常量递推（时序 WNS 从 +0.004ns 提到 **+0.107ns**）。
每次调用 = 写 4 个缓冲区地址 → 写 11 个参数 → 启动 → 等 done → 读回。

### 3.2 标度链：一条"链上合同"

int8 网络里每个张量都有自己的**标度**（1 LSB 代表多少浮点值），
由 `export_network.py` 用 **4 张图**标定后写入 `weights_pack/index.json`；
导出端（产生方）与 runner/PL（消费方）必须对同一个张量用同一个标度。
本工程曾修复两个系统性标度错误（输入标度键名、汇合点标度选择），
并留下对账工具 `diag_scale.py`——**以后任何量化改动都先跑它验收**。

### 3.3 三地分离：源码 / 工程产物 / 交付物

| 类别 | 例子 | 特点 |
|---|---|---|
| 源码 | `01_hls/src/`、`03_ps/pynq/`、脚本 | 小、可读、进 git |
| 工程产物 | `01_hls/proj/`、`02_vivado/proj_engine/` | 大、可重建、不进 git |
| 交付物 | `overlay_engine_150mhz/`（bit+hwh）、`xsa/`、`board_bundle/` | 上板要用，**哈希三方核验**（bit = XSA = bundle） |

### 3.4 验证金字塔（每层都有证据文件）

```
上板实测      JTAG 5 用例 / PYNQ 单测 / 整网对拍      ← 待做
RTL cosim    老形状 + 大形状（352×192）双双 PASS      logs/engine_v3*_cosim*.log
HLS csim     大形状逐元素一致                          logs/engine_v3*_run.log
g++ 回归     28 场景（含非法参数负例）                  scripts/fuzz_shapes.py
量化对账     算子 vs PyTorch 5/5 / 池回归 / 一致性      tools/test_*.py
```

### 3.5 部署形态对齐：分块/行带 与 换画幅（实测）

上游《分块推理_切割与拼接》给出的设计参数——**它们同时就是 FPGA 侧
带宽/缓冲设计的输入**：

| 参数 | 值 | 依据 |
|---|---|---|
| 网络有效感受野 | **约 512** | 受控实验（PSNR 在 512 处饱和） |
| `tile` 块边长 | **必须 32 的倍数** | 网络有 2 次 unshuffle + 4 级下采样 |
| `overlap` 重叠 | **≥ 256（感受野一半）**，默认 256 | 保证重叠区每像素被完整看到，无接缝 |
| 拼接口径 | 线性羽化 + 除以权重和归一化 | 纯色图往返偏差必须为 0（自检铁证） |
| 演进形态 | "块=行带、重叠=行缓冲上下文、羽化=片上累加器" | 上游第 9 节：这套逻辑将来原样搬到 PL 侧 |

**我们的现状与实测**：

- 现架构 = **整图逐层 offload**（全图上下文最完整，640 场景质量最好）；
- **换画幅已实测**：同一套权重，只改 manifest 尺寸即可——实测 **512×512
  原生**全流程（4 图标定导出）：交叉验证 max|diff|=7.7e-07、
  **PSNR 31.73dB**（与 640 的 31.52 持平）、驻留峰值 33MB；
  **608×416**（600×400 需求最近的 32 倍数）清单一并生成、引擎形状族全兼容
  （清单级验证；该尺寸的整网未跑）；
- 操作三步：`build_manifest.py --h H --w W` →（可选重导出）→ 上板跑同一
  runner——**引擎与调度对输入尺寸无假设**（尺寸只影响 manifest 与内存池规划）；
- **输入处理（已按上游铁律改）**：`load_image` 现在是**原生居中裁剪、不缩放**
  （图小于画幅直接报错，除非显式 `--allow-resize`）；`prep_input.py` 把照片裁成
  `input_q.bin`；大图/大画幅用 `tiled_infer.py`（tile 32 倍数、overlap 默认 256、
  羽化 + 归一化拼接）。

**大图（如 4K）的处理路线**（与上游对齐）：
① 稳妥版：PS 侧套用上游 tiling 参数——切块 → 逐块整网 → 羽化拼接
（画质按上游实测 ≈33dB @ 2048/512）；② 终极版：特征行带流式 PL——远期。

## 4. 结构落定对照表（2026-10-16 整理）

| 整理前 | 整理后 |
|---|---|
| 根目录 `p/`、`01_hls/01_hls/`（误跑的空壳工程） | 已删除（完整版在 `01_hls/proj/`） |
| `02_vivado/proj_check`、`proj_probe`（探测空壳） | 已删除 |
| `02_vivado/overlay`、`trial_*.tcl`、`probe_*.tcl` | `02_vivado/archive/` |
| `01_hls/var_f2`、`var_f4`、`var_nosplit`（实验副本） | `01_hls/archive/` |
| `notes/*.rpt`（52 个报告平铺） | `notes/reports/` |
| `03_ps/logs_*.log`、`02_vivado/*.log`（散落日志） | 各自 `logs/` 目录 |
| `03_ps/tmp_pynq_src/`（130MB 研究素材） | 已删除（可重下） |
| 构建脚本报告输出 `notes/` | `notes/reports/`（6 个 tcl 已同步修改） |

> 上游新文档学习（2026-10-16 增补）：上游 restructure 分支的 docs 与本工程
> 的对接要点已吸收进 §1 与 §3.5（Phase 3 定位、感受野 512、tiling 参数、
> "块=行带"形态）。

### 命令对照

| 世代 | 出比特流 | 上板 |
|---|---|---|
| 六 IP 时代（旧） | `build_bd.tcl` → `build_bitstream.tcl`（多 IP 手工搭） | 无 |
| 单引擎（当前） | `hls_run_engine.tcl` → `build_bd_engine.tcl` → `build_bitstream_engine.tcl` → `export_xsa.tcl` | JTAG：`baremetal/main.c`；PYNQ：`test_on_board.py` → `run_network_on_board.py` |
| 一键打包 | `python 03_ps/tools/make_board_bundle.py` → `board_bundle/` | 拷这一个目录即可 |

## 5. 没有改动的东西

- 上游 Python 库（`demoir-fpga` 及其本地副本）**只读取材**，一个字没写；
- `vendor/PDNet/` 的算法、模型、损失——保持原样；
- 网络**数值语义**：结构镜像与真实模型交叉验证 max|diff| = 7.15e-07；
  量化只影响精度（PSNR 31.52dB），不改结构。

## 6. 回滚

- 比特流历史版本：`02_vivado/overlay_engine_150mhz_rev1/2/3`（v1/v2/v3.0）；
- 六个特化工程完整保留在 `01_hls/proj/`（v1 世代证据，可回退参考）；
- `demoir-pl-lab` 已初始化本地 git（main 分支，首个提交为 2026-10-16 终检版）。

## 7. 仍待处理

| 项 | 说明 |
|---|---|
| **上板实测** | JTAG 5 用例（含 352×192）→ PYNQ 单测 → 整网对拍（清单见 `notes/on_board_checklist.md`） |
| 大图/异画幅输入 | ✅ 已落地：`load_image` 原生裁剪、`prep_input.py`、`tiled_infer.py`（32 倍数 / overlap 256 / 羽化）；**待做**只剩"板上直接读照片 + 分块应用入口" |
| 分块 → PL 流式演进 | 上游规划"块=行带、羽化=片上累加器"；本工程现为整图逐层 offload（功能已验证），流式化列为远期 |
| 归档清理 | `01_hls/proj/var_*` 与 16 个实验工程已清（2026-10-16，917MB，报告已归档）；`02_vivado/proj_ax7015b`（199MB 旧工程）仍待拍板 |
| 可选优化 | PYNQ 缓存细化（整池 flush → 区间）；层间算子微优化（上板画像后再定） |

## 8. 附：关键数字（2026-10-19，v3.4）

| 指标 | 值 |
|---|---|
| 卷积数 / 执行算子 | 249 / 517 |
| 单引擎参数上界 | CIN 352、COUT 192、K 5、OC_BLOCK 8 |
| 实现时序 / 资源 | WNS **+0.024ns**（6.40ns 过约束；等效 150MHz 约 +0.29ns），WHS +0.036ns；BRAM 86.3%、DSP 38.8%、LUT 27.2% |
| PL 吞吐（cosim 实测） | 宽画幅 1×1 **2.41 拍/单元**、3×3 **4.01 拍/单元**；2×2/352×192 从 4,142,592 → **2,385,312 拍**（−42%） |
| 整图预计用时 | 640×640：PL 约 4.6 分钟，含 PS/缓存 **5–6 分钟**（保守 7–9）；512/608×416：**3.5–4.5 分钟**（保守 5–6） |
| 量化精度 | PSNR 31.52dB（4 图标定；换图 25.8~33.6dB） |
| 换画幅实测（512 原生） | PSNR 31.73dB；交叉验证 7.7e-07；驻留峰值 33MB |
| 上游部署参数（引用） | 感受野 ≈512；tile 必须 32 倍数；overlap ≥256 |
| 内存池 | 84.8MB（活跃峰值 46.3MB） |
| 上板包 | 8.2MB / 13 文件（含一致性自检工具） |
