# 执行文档：BinaryDemoire 二值化重训（另一台机器）

> 生成：2026-10-27；对象：`demoir-fpga` 仓库；目标论文 arXiv 2602.03176
> 代码分支：**`feat/binary-demoire-train`**（模型、配置、自检脚本、本机接线都在这个分支上，`main` / `camera` 不受影响）
> 论文原文：[`docs/论文/2026_BinaryDemoire.pdf`](../论文/2026_BinaryDemoire.pdf) + [补充材料](../论文/2026_BinaryDemoire_补充材料.pdf)；怎么读见 [`docs/论文/README.md`](../论文/README.md)
> 配套代码：`vendor/PDNet/model/binary_esdnet.py`（本仓库新增，单文件）
> 状态：**代码只做过 CPU 上的形状/梯度/数学自检，没跑过完整训练**。第 7 节的冒烟测试是上机第一件事。

---

## 0. 这份文档的边界

**是什么**：把 ESDNet 二值化成 BinaryDemoire（MABG + SGRA）、按论文协议在 UHDM 上重训、并用一条五级阶梯判断"我复现对了吗"的完整操作步骤。

**不是什么**：

- 不是官方复现脚本。**官方代码至今没放出来**（`zhengchen1999/BinaryDemoire` 只有 README + 图，TODO 里 `Release code and pretrained models` 未勾）。
- 不是逐字节等价实现。论文有 8 处细节没写死，本实现都做了选择，全部列在 **附录 B**，能校准的给了校准方法。

**因此复现的成功标准不是"数字完全一致"，而是**：

1. 每一级落在论文数字的 ±0.3 dB 内；
2. 组件增益的方向和量级对得上（MABG 约 +0.8、SGRA 约 +1.3、合并约 +1.6）；
3. 参数量的口径对得上（Base 0.27M / 完整 0.31M）。

第 3 条是唯一可以"秒级验证"的，先做它。

---

## 1. 先定 backbone 尺度（这一步错了，后面全白跑）

论文 Table 2 里的 `ESDNet` 是 **5.93M 参数 / 17.59 G OPs**。这个数可以在 ESDNet 原文里核实（本机资料 `.research/esdnet.txt`，ESDNet 原 PDF 在 `文献_去摩尔纹/2022_ESDNet_UHD-demoire.pdf`）：其表格写着 `Params 5.934M`、`PSNR 22.119`、`SSIM 0.7956`、`LPIPS 0.2551` —— 与 BinaryDemoire Table 2 的 ESDNet 那一列逐位一致。**可以确认论文用的是 ESDNet 完整版（Model E）。**

你现在仓库里的 student 是 **1.562M**（`my_model(48,32,64,32,1)`），因为 PDNet 把 ESDNet 的 RDB 稠密块换成了 GhostModule。

**用小模型去对 20.97 是错的口径。** 二值化的绝对 PSNR 强依赖 FP 上界：FP 骨架本身从 5.93M 缩到 1.56M，上界就变了，二值化后的 20.97 没有可比性。

好消息：RDB 版本就在 `vendor/PDNet/model/nets_student.py` 里，只是被注释掉了（`#self.rdb = RDB(...)` 紧跟着 `self.rdb = GhostModule(...)`）。本补丁用 `DEMOIR_RDB=1` 走 RDB。

**关键一步：先把 FP 参数量调回 5.93M。** 直接换回 RDB 也只有 **2.15M**，因为 PDNet 的 config 用的是 (48, 32, 64, 32) 这套收缩过的通道数，不是 ESDNet 原文的。用 `param_report` 扫五分钟就能对上（下表是实测值）：

| EN_FEATURE | EN_INTER | DE_FEATURE | DE_INTER | FP 总数 | 二值等价 | 判定 |
|---|---|---|---|---|---|---|
| 48 | 32 | 64 | 32 | 2.15 M | 0.149 M | 你现有 student 的尺度，**不够** |
| 48 | 64 | 64 | 64 | 4.46 M | 0.246 M | 偏小 |
| 48 | 96 | 64 | 32 | 5.17 M | 0.268 M | 偏小 |
| **48** | **64** | **64** | **96** | **5.96 M** | **0.305 M** | ✅ 对上了 |
| 48 | 96 | 64 | 64 | 6.20 M | 0.312 M | 偏大 |

最后那组是**两个独立数字同时命中**：FP 5.96M ↔ 论文 5.93M，二值等价 0.305M ↔ 论文 0.31M。基本可以认定 `(48, 64, 64, 96)` 就是（或极接近）论文那台 ESDNet 的尺度。

**所以 yaml 里请用 `EN_INTER_NUM: 64` / `DE_INTER_NUM: 96`**；默认的 (32, 32) 只适合先跑通流程，不适合对数字。

自查命令（CPU，5 秒，不用 GPU）：

```bash
cd demoir-fpga/vendor/PDNet
DEMOIR_RDB=1 DEMOIR_MODE=full python model/binary_esdnet.py    # 看"参数/二值/等价"三个数
DEMOIR_RDB=0 DEMOIR_MODE=fp   python model/binary_esdnet.py    # 应 ≈1.565M（对现有 student 的复刻校验）
```

`RDB=0 / MODE=fp` 必须 ≈ **1.565M**（你 README 记的 1.562M，差 0.2% 属正常）。这一项对不上，说明我重写的骨架和 `nets_student.py` 有出入，**先解决它再往下走**。

### 五级阶梯

| 级 | 配置 | 论文 PSNR / SSIM | 参数 | 作用 |
|---|---|---|---|---|
| L0 | `MODE=fp` | 22.12 / 0.7956 | 5.93 M | 验数据 + 评估 + 训练流程 |
| L1 | `MODE=base` | 19.36 / 0.7407 | 0.27 M | 验二值卷积本身 |
| L2 | `MODE=mabg` | 20.18 / 0.7562 | 0.27 M | 验 DWT + 门控 |
| L3 | `MODE=sgra` | 20.68 / 0.7820 | 0.31 M | 验捷径对齐 |
| L4 | `MODE=full` | **20.97 / 0.7856** | 0.31 M | 目标 |

> L0 不必真跑 150 epoch。它的作用是"证明评估链路能出论文量级的数"。省时间的做法见 §8.1。

---

## 2. 机器准备

### 2.1 依赖

```bash
python -m venv .venv-bin
# Windows: .venv-bin\Scripts\activate
# Linux:   source .venv-bin/bin/activate

pip install torch==2.7.0 torchvision==0.22.0 --index-url https://download.pytorch.org/whl/cu118
pip install -r tools/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

`cu118` 是 RTX 40 系（sm_89）可用的组合，与你现有环境一致。换卡就换 index-url，别用 `environment.yaml`（锁了 cudatoolkit 11.3，跑不了 40 系）。

依赖里三样别漏：`tensorboardX`（SummaryWriter）、`lpips`（评估）、`opencv-python`（数据读图）。

### 2.2 离线/内网机器要预下的权重

- `torchvision.models.vgg19(pretrained=True)` —— `multi_VGGPerceptualLoss` 要用，第一次运行会去下 ~550MB。
  提前在有网的机器上下好，拷到 `~/.cache/torch/hub/checkpoints/vgg19-dcbb9e9d.pth`（Windows：`C:\Users\<you>\.cache\torch\hub\checkpoints\`）。
- `lpips` 的 `alex` 权重（评估用，同目录）。

不预下的话，训练会在第 0 步卡在下载上，且在无网机器上直接崩。

### 2.3 显存与 crop

论文用 **768×768, batch 2**。但你的记录里，**1.562M** 的 student 在 640/bs2 上就占了 9557 MiB，而 768/bs2 实测 11830/12282 MiB（96%）并把吞吐从 2.6 it/s 打到 0.04 it/s。

二值化**不减显存**（PyTorch 里二值卷积仍是 float 计算，特征图也还是 FP）。所以：

**默认用 640，不要用 768。** 按 640→9.5GB 线性推算 768 ≈ 13.7GB，12GB 卡必然 OOM 或颠簸。这是与论文的一处**明确偏离**，写进 run README。

如果你的卡 ≥24GB，则用 768（论文口径），并在报告里注明 —— 这样数字可以和论文直接比。

```bash
python src/train.py --config uhdm_bin_ep150 --dry-run     # 先校验配置和路径
# 正式跑之前，手动计时 50 步，记录 it/s 与 torch.cuda.max_memory_allocated()
```

> ⚠️ 640 与 768 的差距通常在 0.1–0.3 dB 量级，属于"可解释偏离"，但必须写清楚，不能当成复现成功。

---

## 3. 迁移清单：要带走什么

| 带走 | 具体 | 备注 |
|---|---|---|
| 代码 | `demoir-fpga/` 的 `feat/binary-demoire-train` 分支：`git clone -b feat/binary-demoire-train git@github.com:youyikeshi/demoir-fpga.git` | 模型、配置、自检脚本都在分支里，不用手工拷文件 |
| 训练集 | `datasets/uhdm_full/train`（4500 对，28 个子目录） | 最大的一块，先 `du -sh` 量 |
| 评估集 | `datasets/uhdm/eval`（100 张） | 必须与训练集无重叠 |
| VGG/LPIPS 权重 | §2.2 | 小但会卡住训练 |

**不需要**：`weights/teacher/`（本复现**不做蒸馏**，论文也没用 teacher）、`datasets/tip/`、历史 `runs/`。

打包与校验：

```bash
# 源机器
tar -cf uhdm_full.tar datasets/uhdm_full && sha256sum uhdm_full.tar > uhdm_full.tar.sha256
# 目标机器
sha256sum -c uhdm_full.tar.sha256 && tar -xf uhdm_full.tar
```

目录结构不能变（`pair_NN` / `pair_mi_NN` 的 28 个子目录），dataset loader 按这个约定枚举。传完先用 `python src/train.py --config uhdm_bin_ep150 --dry-run` 让 `check_dataset` 验一遍。

---

## 4. 代码接线（已全部提交，不用手改）

### 4.1 三个新增文件

| 文件 | 作用 |
|---|---|
| `vendor/PDNet/model/binary_esdnet.py` | 模型本体。单文件自包含（不 import `nets_student`），所以只拷这一个文件就能跑 |
| `src/configs/uhdm_bin_ep150.yaml` | 训练配置。宽度按 §1 标定、关蒸馏、640 crop |
| `tools/smoke_binary.py` | 上机自检：参数量 / 前向形状 / STE 梯度。20 秒，不需要 GPU 与数据 |

### 4.2 模型怎么被选中（`DEMOIR_BIN`）

vendor 的两个入口各改了一处 import，做成环境变量开关：

```python
# vendor/PDNet/train.py 与 test.py
if os.environ.get('DEMOIR_BIN'):
    from model.binary_esdnet import my_model
else:
    from model.nets_student import my_model
```

不设 `DEMOIR_BIN` 时行为与上游完全一致，所以这个分支**不会污染原来的实验**；只有 `DEMOIR_BIN=1` 才走二值化模型。

> ⚠️ 忘了设 `DEMOIR_BIN=1` 是本分支最容易犯的错。训练照跑、日志照出、loss 照降，只是训成了一个普通的 5.96M ESDNet（也就是阶梯里的 L0），而你看到的数字会好得不像二值网络。

### 4.3 配置

`src/configs/uhdm_bin_ep150.yaml` 与 `uhdm_4k_s_ep150.yaml` 只差三处：

| 差异 | 值 | 原因 |
|---|---|---|
| `MODEL` 宽度 | 48 / **64** / 64 / **96** | 默认的 `EN_INTER_NUM=32` 只给出 2.15M，对不上论文的 5.93M，见 §1 |
| 蒸馏 | `USE_TEACHER: False` | BinaryDemoire 不用 teacher，论文也没用 |
| 运行目录 | 未锁定 `GENERAL.RUN` | 五级阶梯要跑出五个 `runs/` 目录，用 `--name` 指定，见 §6.1 |

模型档位**不**写在这个 yaml 里（vendor 的 config 解析不接受嵌套新键，而模型在 import 期就要知道档位），走环境变量，见 §6。

### 4.4 自检（上机第一步，20 秒，不需要 GPU 与数据）

```bash
cd demoir-fpga
DEMOIR_BIN=1 DEMOIR_MODE=full DEMOIR_RDB=1 python tools/smoke_binary.py --config uhdm_bin_ep150
```

预期输出（2026-10-28 在纯 CPU 上实测，4.4 秒）：

```
宽度      : (48, 64, 64, 96)  ← src/configs/uhdm_bin_ep150.yaml
档位      : MODE=full RDB=1 SGRA=1 MABG_SCOPE=boundary

[1] 参数量
    FP 参数        : 5.961 M
    二值参数       : 5.839 M
    二值等价计数量 : 0.305 M   （论文 0.31M）
    二值卷积个数   : 227
    MABG 层 5 / SGRA 层 5
    ✓ 与论文 5.93M 相符（+0.5%）

[2] 前向形状
    输入 (2, 3, 128, 128) -> 输出 [(2, 3, 128, 128), (2, 3, 64, 64), (2, 3, 32, 32)]
    ✓ 三级金字塔形状正确

[3] 反向梯度（STE 通路）
    BinaryConv2d 227 个；有梯度 227 个；梯度非零 227 个
    ✓ 梯度回到了全精度影子权重
```

三项分别对应三个会让 40 小时白跑的错误：宽度抄错（FP 不在 5.93M ±5% 内，脚本直接以退出码 1 结束）、输出层级不对、STE 断链（梯度回不到未量化的影子权重，训练不报错但不收敛）。

模型自带一份更细的单元自检——前向数值等于 ±1 内积、`alpha` 公式、门控行为、Haar 子带、SGRA 交错下标。怀疑数学实现时再跑：

```bash
cd vendor/PDNet && DEMOIR_RDB=1 DEMOIR_MODE=full python model/binary_esdnet.py
```

它用的是配置默认宽度 `(48, 32, 64, 32)`，所以打出来的参数是 2.154M 而不是 5.96M，别误判成配错了。

---

## 5. MABG 加在哪：本复现最大的不确定性 ⚑

论文正文说"约三分之一的卷积层"，补充材料说"上/下采样、编解码通道调整这些关键操作，dense block 和 channel attention 里不加"。

问题是：按字面照做，在 ESDNet 里只有 5 层符合（也就是本实现的默认值），远不到 1/3。这个矛盾没法从论文里消解，只能校准。

**本实现的选择**：`MABG_SCOPE=boundary`，只加那 5 层。

**校准手段**（按顺序试，每步用 30 epoch 代理跑做 A/B，不要用 150）：

| 方案 | 加在哪 | 代价 |
|---|---|---|
| A（默认） | 2 个 down + 3 个 preconv | 最省，最贴论文文字 |
| B | A + 3 个 decoder 输出卷积 | +3 层，仍便宜 |
| C | A + 各 GhostModule/RDB 的 1×1 融合卷积（12 层） | 中等 |
| D | 所有 DenseConv | 最贵，且与补充材料冲突 |

**判据**：先跑 A，若 L2 明显低于 20.18（比如只有 19.7），说明门控覆盖不足，按 B→C 扩。若 L2 已经 20.2+，说明 A 就是对的，收工。

⚠️ 参数量**不能**用来校准 MABG 位置（5 维→1 维的共享 FC 总共只有 6 个参数，看不出差别）。能校准的是 PSNR，以及 §4.4 自检打印的"MABG 层数"。

反过来，**参数量可以用来校准 SGRA**，而且这里已经出现了一个可量化的疑点：论文 Base 0.27M → 完整 0.31M，SGRA 贡献 **+40k**；本实现只在 5 个不匹配边界放 SGRA，实测只贡献 **+1k**（0.148 → 0.149M）。**差了 40 倍。**如果论文的 +40k 是真数，意味着它的 SGRA 出现在**远多于 5 处**的位置（很可能每个二值卷积块都带一条捷径，形状匹配时退化成 identity 但算符仍在）。这是附录 B 里的待定项——如果 L3 对不上而 L2 对得上，这是第一个要改的地方。

---

## 6. 训练执行

### 6.1 命令

```bash
cd demoir-fpga

# L4（目标档位）
DEMOIR_BIN=1 DEMOIR_MODE=full DEMOIR_RDB=1 DEMOIR_MABG_SCOPE=boundary DEMOIR_SGRA=1 \
  python src/train.py --config uhdm_bin_ep150 --name bin_full
```

Windows（cmd）：

```bat
set DEMOIR_BIN=1
set DEMOIR_MODE=full
set DEMOIR_RDB=1
python src\train.py --config uhdm_bin_ep150 --name bin_full
```

各档位只替换 `DEMOIR_MODE`：`fp` / `base` / `mabg` / `sgra` / `full`。

> ⚠️ 换档位必须换 `--name`，否则五档会写进同一个 `runs/` 目录，checkpoint 互相覆盖。约定 `--name bin_{mode}` → `runs/bin_full`、`runs/bin_fp` …
> 配置里刻意没锁 `GENERAL.RUN`，就是为了让 `--name` 说了算；续训时才需要把它指向原目录。

### 6.2 后台跑 + 断点

```bash
nohup env DEMOIR_BIN=1 DEMOIR_MODE=full DEMOIR_RDB=1 \
  python src/train.py --config uhdm_bin_ep150 --name bin_full \
  > /dev/null 2>&1 &
tail -f runs/bin_full/train.log
```

`SAVE_EPOCH: 5` 会存 `checkpoint_%06d.tar`，中断后加 `--load-epoch <epoch> --name bin_full` 续训（optimizer 和 lr 状态一起恢复）。

### 6.3 一次只跑一个

同一张卡上并行两个训练会让 it/s 抖动、显存碎片化，最终 wall-clock 更慢。阶梯是串行的。

---

## 7. 冒烟测试（上机第一小时，按顺序）

三段，任何一段不过就不要启动 150 epoch。

### 7.1 参数量 + 形状 + 梯度（20 秒）

```bash
python tools/smoke_binary.py --config uhdm_bin_ep150
```

跑法与预期输出见 §4.4。它同时替代了原来的两件事：宽度标定核对（FP 必须 5.93M ±5%）和 STE 梯度是否连通。

### 7.2 单图过拟合（10 分钟，最关键）

拿 1 张训练图反复训 200 步，loss 应该单调掉到接近 0、输出 PSNR 冲到 40+。

```python
# tools/gradcheck_overfit.py（自己写 10 行）：固定一张 256² 的 in/label，
# batch 里重复它，跑 200 步，打印 loss 和 PSNR。
```

**这一步能抓 90% 的二值化 bug**：
- STE 没接上（`sign` 的梯度被 detach 掉了）→ loss 平着不动；
- `alpha` 没 detach 或公式反了 → loss 震荡/发散；
- `t` 没初始化成 0 或形状对不上 → 第一步就 nan；
- SGRA 分区写错 → 能收敛但上限很低（PSNR 卡在 25 左右）。

### 7.3 一小时真跑（看曲线形状）

跑 1000 步就够。健康的表现：loss 从 ~5.9 降到 ~4.5 附近，PSNR 从输入的 17.9 涨到 19+。
**不健康**：loss 在前 100 步内急剧下降然后卡住（说明只有最后几层在学），或 lr 重启点（epoch 50）之前就发散。

---

## 8. 验收阶梯与判定

### 8.1 L0 的省时做法

L0 的目的是"证明评估链路能出论文量级的数"，不是刷分。三个选项，从省到贵：

1. **拿官方 ESDNet 权重直接评估**（最省，几十分钟）。UHDM 官方 repo 有 ESDNet 的 checkpoint，能拿到就用它跑 `test.py`，对 22.12。
2. **只训 20–30 epoch**，看趋势到 21 附近就停，认定链路通。
3. 完整 150 epoch（最贵，只有在 1、2 都不可行时才做）。

拿不到官方权重、又不想花 3 天 → 直接跳过 L0，从 L1 开始，但要在报告里注明"基线未独立验证"。

### 8.2 判定表

| 级 | 期望 | 命中区间 | 不达标的动作 |
|---|---|---|---|
| 0（秒级） | FP 5.93M → 二值等价 0.31M | ±3% | 调 `EN_INTER_NUM` / `DE_INTER_NUM`，见 §1 表 |
| L0 | 22.12 / 0.7956 | ±0.3 | 查评估口径（见 §8.3）、数据对齐、crop |
| L1 | 19.36 / 0.7407 | ±0.3 | 查 STE、`t` 初始化、首尾层是否被误二值化 |
| L2 | 20.18 / 0.7562 | ±0.3 | 扩 MABG 位置（§5 的 B→C） |
| L3 | 20.68 / 0.7820 | ±0.3 | 查 SGRA 的 gcd/交错索引；试"每个二值块都带捷径" |
| L4 | 20.97 / 0.7856 | ±0.3 | 先确认 L2、L3 各自达标 |

±0.3 dB 的意思是"同一个方法在合理重实现下的正常抖动"。**低于 L1 的 19.36 是本复现失败**，不是抖动。

### 8.3 评估口径必须统一

你现在 `runs/2026-09-14_*` 里的 22.59/0.7735 是**自建协议**（100 张 × 4 位置 = 400 块，512² 裁块）。论文 Table 2 是**官方 UHDM test** 全分辨率评估。

这两个数**不可直接比较**。要跟论文对表，必须用 `vendor/UHDM` 的官方评估代码、官方 test 列表、全分辨率（或论文同款 padding 策略）。任何"我们的模型比 BinaryDemoire 高 1.6 dB"的结论，只有在同一口径下才成立。

### 8.4 顺带能白拿的两个消融

L2 与 L3 之间已经覆盖了 Table 1(a) 的四行。再花两次 30-epoch 代理就能补上 Table 1(b) 的三行（freq only / stats only / learnable），这是论文里唯一没给实现细节的消融，复现它成本极低、说服力很高：

- freq only：`MABG(c, mode='freq')` —— 只用 `[r_hf, s]` 两维
- stats only：只用 `[μ, σ, m]`
- learnable：把 FC 输入换成一个可学习常量向量（输入无关）

---

## 9. 故障排查表

| 症状 | 最可能的原因 | 动作 |
|---|---|---|
| 第 1 步就 `nan` | RPReLU 的 slope 或 `beta_ste` 初值不对；lr 太大 | 查 `RPReLU.slope` 初始 0.25、`beta_ste` 初始 1.0；临时把 lr 降到 1e-4 试 |
| loss 完全不动，PSNR = 输入 | STE 断了：`sign` 用了 `torch.sign` 而不是自定义 `Function` | 查 `_ActSign` 是否真的被调用（在 forward 里打点） |
| 收敛，但 PSNR 只有 18–19 | 大概率是 L1 水平：MABG/SGRA 没生效 | 打 `param_report` 和 `mabg_layers`，确认层数非 0 |
| PSNR 20.2 卡住上不去 | SGRA 没接上（参数量没涨到 0.31M） | 看 §4.4 的 SGRA 层数 |
| loss 在 epoch 51 反弹 | **正常**，`CosineAnnealingWarmRestarts(T_0=50)` 的 lr 重启 | 不要动，这是论文协议 |
| OOM | 768/bs2 太大 | 降 640，记录偏离 |
| 训练中后期突变发散 | `beta_ste` 漂到 clamp 上限（10） | 加日志监控 `beta_ste`；必要时收紧 clamp |
| 评估 PSNR 比训练时低很多 | 评估没做 32 倍数 padding，或裁边不对 | 对齐 `model_fn_decorator` 里的 pad/crop 逻辑 |
| Windows 上 dataloader 卡死 | `WORKER` 过大 | 降到 8 或 0，先确认不是数据路径问题 |

---

## 10. 预算

**时间**（150 epoch = 2250 步/epoch = 337,500 步）：

| it/s | 小时 |
|---|---|
| 2.0 | 47 |
| 1.5 | 62 |
| 1.0 | 94 |
| 0.5 | 187 |

PyTorch 里二值卷积**不会**真的快 32 倍（还是 float 卷积 + sign），所以别指望比 FP 模型快。二值省的是显存和权重体积，不是这个框架下的算力。

上表按 2.15M 配置估。换成 §1 推荐的 5.96M 配置，it/s 大约再低 30–50%（多出来的算力在稠密块的通道数上）。**先测 50 步再排计划**，别照表硬算。

**阶梯总成本**：L1+L2+L3+L4 各 150 epoch ≈ 4 × 2–4 天 ≈ **8–16 天**。这就是为什么 §5/§8.4 的决策必须用 30 epoch 代理（约 1/5 成本）来做，只有最终确认的数字才值得跑满 150。

**磁盘**：checkpoint 存的是全精度参数 + Adam 两份动量，约 3×4 字节/参数 —— 2.15M 配置 ≈26MB/个，5.96M 配置 ≈72MB/个。30 个 checkpoint 就是 0.8GB / 2.2GB 每 run。加上数据 20–40GB，留 60GB 余量。

---

## 11. 产出与记录

每个 run 的 `runs/<RUN>/README.md` 至少记这 6 项（论文没给的，只有你能提供）：

1. `DEMOIR_*` 全部环境变量的值
2. 实际 crop / batch / it/s / 峰值显存
3. 最终 PSNR / SSIM（以及用的是哪个评估口径）
4. `param_report` 的完整输出（总参数、二值部分、等价参数、二值卷积数）
5. MABG / SGRA 的层名列表（`mabg_layers()` 的输出）
6. 与论文数字的差值，以及你归因到的原因

第 6 项是这份复现最有价值的部分——**"差 0.2 dB 且我知道为什么"比"差 0 dB 但不知道为什么"值钱得多**。

---

## 附录 A：论文数字对照表

**UHDM（Table 2）**

| 方法 | Params | OPs | PSNR | SSIM | LPIPS |
|---|---|---|---|---|---|
| ESDNet (FP) | 5.93 M | 17.59 G | 22.12 | 0.7956 | 0.2551 |
| ReActNet | 0.31 M | 0.86 G | 19.42 | 0.7274 | 0.3353 |
| BBCU | 0.31 M | 0.86 G | 19.36 | 0.7251 | 0.3428 |
| BiSRNet | 0.33 M | 0.86 G | 19.70 | 0.7278 | 0.3305 |
| Biper | 0.31 M | 0.86 G | 19.46 | 0.7319 | 0.3308 |
| BiMaCoSR | 0.31 M | 0.85 G | 20.50 | 0.7782 | 0.2896 |
| **BinaryDemoire** | **0.31 M** | **0.78 G** | **20.97** | **0.7856** | **0.2845** |

**其他数据集（BinaryDemoire）**：FHDMi 22.47 / 0.8091，TIP2018 27.33 / 0.8795，LCDMoiré 39.88 / 0.9914。

**Table 1(a) 拆解（UHDM）**：Base 19.36 → +MABG 20.18 → +SGRA 20.68 → 两者 20.97。

⚠️ Table 1(a) 的 Params/Ops 列有笔误（Base 与 +MABG 都写 0.27M/0.66G，与 1(b) 的 0.31M/0.78G 冲突）。以 1(b)/1(c) 为准。

---

## 附录 B：本实现与论文的 8 处拍板

| # | 论文怎么说 | 本实现 | 风险 | 怎么校准 |
|---|---|---|---|---|
| 1 | 激活 STE 用 `tanh(βx)` 平滑替代，β 可学 | 前向存 `tanh(βx)`，反向同时给 x 和 β 传梯度，β `clamp(0.5,10)` | 中 | 监控 β 漂移；L1 对不上先看这里 |
| 2 | MABG 加在"约 1/3 层" | 默认 5 个边界卷积 | **高** | §5 的 A→D 扫描 |
| 3 | 共享 FC：`5 → 1` 还是 `5 → r → 1` 未写 | 默认 `5 → 1`（`hidden=0`） | 低 | 参数差 6 vs 6+5r，影响可忽略 |
| 4 | DWT 未说小波类型 | Haar，单级，stride 2，系数 0.5 | 低 | 只有 LL/HH 等能量比值进入 gate，尺度无关 |
| 5 | SGRA 的 g = gcd(Cin,Cout) | 分组卷积实现 block-diagonal，再 transpose 交错 | 低 | 自检 (f) 已覆盖 |
| 6 | SGRA 出现在哪些层 | 只在不匹配的 5 个边界 | **中** | L3 对不上就改成"每块都带" |
| 7 | 首尾两层保持全精度 | `conv_first` + 3 个 decoder 输出卷积 | 低 | 参数量反推 |
| 8 | 二值块是否自带残差（Fig.3） | 稠密块内 = 二值卷积 + RPReLU，无残差；边界 = 带残差 | 中 | L1 的 19.36 是主要判据 |

第 2、6、8 项是"看数字才能定"的，其余靠自检就能锁死。
