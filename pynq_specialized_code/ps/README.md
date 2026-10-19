# ps —— PS 侧：调度、层间算子、量化导出与上板脚本

> 跑在 Zynq 的 **ARM（Cortex-A9）** 上。首版用 **PYNQ 的 Python + numpy**（不需要 PyTorch）；
> 另有一条 **JTAG 裸机 C** 自检路线。PS 不做卷积乘加，只做"发任务、搬数据、做层间算子、对拍"。

---

## 1. 文件清单

### `pynq/` —— 板上运行库（拷到板子上就能跑）

| 文件 | 作用 |
|---|---|
| `runner.py` | **整网调度器**：读清单，按 `needed` 过滤出 517 个算子；卷积交给引擎，其余交给 `ops_numpy`；含 `plan_pool` 内存池规划、`NumpyArena`（本机）与 `PYNQArena`（板上连续内存） |
| `ops_numpy.py` | 层间算子（int8 语义）：`pixel_unshuffle/shuffle`、`interp`（双线性，align_corners=False）、`slice`、`cat`、`add`、`eca_attn`、`sam_sum` |
| `conv_engine.py` | **PL 驱动**：从 `.hwh` 发现两个 AXI-Lite 基地址；写 4 个 DDR 地址 + 11 个参数；启动并轮询 done；返回码检查；超时按工作量估算；两条 RTL 上界前置拦截 |
| `ref_numpy.py` | 卷积的 numpy 金标准（与 `pl/src/conv_ref_rt.cpp` 逐位一致） |
| `test_on_board.py` | 板上第 1 步：5 个单算子用例（含 352×192 最坏形状、`relu_en=0`），与金标准逐元素对拍 |
| `run_network_on_board.py` | 板上第 2 步：整网推理 + 与部署包 `ref_out.bin` 逐位对拍；`--timeout`、每 25 个卷积的进度 |

### `tools/` —— 开发机侧（需要 Python；部分需要 PyTorch）

| 文件 | 作用 | 需要 torch |
|---|---|---|
| `build_manifest.py` | 按画幅生成执行清单（算子顺序/形状/缓冲/依赖）；内置引擎上界检查 | 否 |
| `export_network.py` | 浮点前向 + 多图标定 → 权重包（`weights.bin/index.json`）+ 部署参考；交叉验证；写 `build_info.json`；`--clip-pct` 分位裁剪（实测默认 100 别改） | **是** |
| `prep_input.py` | 照片 → 原生裁剪 → `input_q.bin`（**只裁不缩**；画幅须 32 倍数） | 否（读图在 `img_io.py`） |
| `img_io.py` | 读图/裁剪（numpy + Pillow；被 `prep_input` 与 `export_network` 共用） | 否 |
| `tiled_infer.py` | 大图分块推理 + 羽化拼接（tile 32 倍数、overlap 默认 256；带自检） | 否 |
| `check_pack_ref.py` | **部署包自检**：用包重算整网，与 `ref_out.bin` 逐位对拍（640/512 均 0 差异） | 否 |
| `check_consistency.py` | 清单/包的静态一致性（张量、标度、偏移、对齐、池规划） | 否 |
| `check_numpy_ref.py` + `dump_ref.cpp` | numpy 金标准 vs C 参考逐位对拍 | 否 |
| `diag_scale.py` / `eval_generalize.py` | 逐算子标度对账 / 换图泛化评估 | **是** |
| `outlier_report.py` | 激活离群体检 + `--shift-diff`（解释"改标度为什么反而更差"） | **是** |
| `render_compare.py` | 量化对比图 | 否 |
| `make_board_bundle.py` | 一键拼上板包（bit/hwh + 脚本 + 清单 + 部署包 + 说明） | 否 |
| `make_mock_pack.py` | 造测试用假权重包（`test_runner_local.py` 用） | 否 |
| `test_runner_local.py` / `test_pool_plan.py` / `test_ops_vs_torch.py` / `test_driver_discovery.py` | 本机回归（整网调度 / 内存池 / 算子对拍 / 地址发现） | 后者需要 torch |
| `_diag_pool.py` | 内存池调试（打印每个张量的生命周期与地址） | 否 |

### 其它

| 路径 | 作用 |
|---|---|
| `baremetal/main.c` | **JTAG 裸机自检**：5 个用例（含 352×192）、CPU 参考对拍、报告写 `0x3000_0000` |
| `manifest_64.json` | 64×64 快速回归 |
| `manifest_640.json` | **主用**：640×640（517 算子、522 张量、池 84.8 MB） |
| `manifest_512.json` | 512 原生画幅（池 54.3 MB） |
| `manifest_608x416.json` | 600×400 需求的 32 倍数画幅（池 52.4 MB；**尚无对应标定包**） |

---

## 2. 一次整网在板上怎么走

```
读 input_q.bin (3,H,W int8)
  → Overlay(design_1.bit)            # 加载 PL，PYNQ 按 .hwh 配 FCLK0=150MHz
  → PYNQArena: 一块连续 DDR 池（640 图 84.8 MB）+ 偏移视图
  → PYNQEngine: 再把 weights.bin 装进另一块池（~1.6 MB）并 flush
  → for op in manifest.ops（517 条）:
        conv       → 写 4 个地址 + 11 个参数 → start → 等 done → 查返回码
        unshuffle/shuffle/interp/slice/cat/add/eca/sam_sum → numpy
  → 取 out_1 与 ref_out.bin 逐位比较
```

**每层卷积都复用同一块电路**；层间算子不进 PL（当前实现）。

---

## 3. 与 PL 的契约（PS 这边必须守的）

- **寄存器**：见 `../pl/README.md` §4（18 项，`../pl/scripts/check_regmap.py` 自动对表）。
- **内存**：四个缓冲区必须是**连续物理内存**（`pynq.allocate`），地址对齐 64 B；
  写地址时低 32 位之后要写 0 到 `+4`（64 位寄存器）。
- **缓存**：每次卷积前 `flush()`、后 `invalidate()`（当前整池同步，正确优先）；
  权重池初始化时 flush 一次即可。
- **数值**：输入/权重 int8、偏置 int32、输出 int8；标度在 `index.json` 的 `scales`；
  层间汇合点（`cat`/`add`/`sam_sum`）先按目标标度重定标（`_req`，`rint` + clip）。
- **超时**：驱动默认按工作量估算（最贵一层约 700 s 上限）；不要传 5 秒。

---

## 4. 怎么产出部署包（只在开发机上做）

```bash
# 1) 选画幅生成清单
python ps/tools/build_manifest.py --h 640 --w 640 --out ps/manifest_640.json

# 2) 标定 + 导出（需要 PyTorch；≥4 张标定图，第一张兼作部署参考）
python ps/tools/export_network.py --ckpt <student.tar> \
    --manifest ps/manifest_640.json \
    --image <图1> --image <图2> --image <图3> --image <图4> \
    --out <pack 目录>

# 3) 自检（0 差异才算包是好的）
python ps/tools/check_pack_ref.py --manifest ps/manifest_640.json --pack <pack 目录>
```

本工程已实测的包：**640 → PSNR 31.52 dB（0/1,228,800 差异）**、**512 → 31.73 dB（0/786,432）**。

---

## 5. 上板脚本怎么用

```bash
# 第 1 步：5 个单算子
sudo python3 test_on_board.py
#   预期：FCLK0 = 150.0 MHz，5/5 用例通过

# 第 2 步：整网（640 约 5–6 分钟；保守上界 7–9 分钟，别当卡死）
sudo python3 run_network_on_board.py --manifest manifest_640.json \
    --pack weights_pack --input weights_pack/input_q.bin
#   预期：0/1228800 不一致；失败时先跑 check_pack_ref.py 证明包没问题

# 换自己的照片（PC 上做，不缩放）
python ps/tools/prep_input.py --image 照片.jpg --manifest ps/manifest_608x416.json --out input_q.bin
```

---

## 6. 本机回归（不需要板子）

```bash
cd ps/tools
python test_runner_local.py          # 517 算子，两次逐位一致
python test_pool_plan.py             # 2/2（对齐/覆盖/无重叠）
python check_consistency.py --manifest ../manifest_640.json --pack <pack>
python tiled_infer.py --selftest     # 分块/羽化自检
python check_pack_ref.py --manifest ../manifest_640.json --pack <pack>
```

2026-10-18 实测：以上全过；`test_ops_vs_torch.py` 在无 torch 环境显式 `SKIP(77)`。
