# datasets/ —— 所有数据集

> ⚠️ 整个目录**不进 git**（GB 级）。走移动硬盘搬运，见 `docs/训练/流程_双机协作.md`。

## 约定布局

```text
datasets/
└─ <数据集名字>/
   ├─ raw/          原始压缩包（下载来的 tar.gz，解压后可以删）
   ├─ pairs/        解压出来、还没划分的完整配对图
   ├─ train/        ★ 训练集（配置里 DATASET 指向的就是这里）
   └─ eval/         ★ 评估集
```

配置里只写**数据集名字**，路径由 `src/vendor_boot.py` 解析：

```yaml
DATA:
  DATASET: uhdm      # -> datasets/uhdm/{train,eval}
```

换数据集 = 改这一行。必须存在的只有 `train/` 和 `eval/`，`raw/` 和 `pairs/` 是给
划分脚本用的中间层，可以没有。

## 现有数据集

| 名字 | 来源 | 规模 | 说明 |
|---|---|---|---|
| `uhdm` | UHDM 官方 | 500 对 3840×2160 | 由官方 `test.tar.gz` 解压后**按图**划分成 400 / 100 |
| `uhdm_full` | UHDM 官方 | **4500 对 4032×3024** | 官方 **train 划分**，按 `pair_NN`/`pair_mi_NN` 分 28 个子目录 |

`raw/` 里保留的是 `test.tar.gz`（5.5 GB）。要论文级复现需要官方的 4500 对训练集，
下载方式见 `docs/训练/方案_数据集获取.md`。

### `uhdm_full` 怎么拿（国内可直连，不必挂 VPN）

官方只在 Google Drive 提供（`train.tar.gz` ≈72 GB），另有 OpenDataLab 的 80 GB 单文件整包。
两条路都不轻。**更省的做法**是走 hf-mirror 上的完整包，用本仓库的
`tools/fetch_uhdm_from_hf.py` 做**远程随机抽取**：只读 ZIP 中央目录，再对需要的条目发
精确 Range 请求，**只下训练集那 35 GB，省掉整包的 9.5 GB 流量**，且逐文件校验 CRC32。

```bash
# 看包里有什么
python tools/fetch_uhdm_from_hf.py --list

# 抽取官方训练集 4500 对（保留 pair_NN 子目录结构），约 2 小时 / 5.3 MB/s
python tools/fetch_uhdm_from_hf.py --extract-subset \
    --path-filter "train/train/" --out datasets/uhdm_full/train --workers 12

# 抽取测试集 500 对（摊平成 datasets/uhdm/pairs/，再跑 make_split.py 划分）
python tools/fetch_uhdm_from_hf.py --extract-test --path-filter "test/test/"
```

> ⚠️ `--path-filter` 必须写 `train/train/` 或 `test/test/`。写 `train` 会同时命中
> `test_origin/`，两者文件名相同会互相覆盖。
> 实测记录（含两个数据集的核对结果）见 `docs/训练/说明_本机重训环境搭建.md` 2.2。

## 怎么新增一个数据集

```bash
# 1. 建目录
mkdir datasets/mydata

# 2. 放数据：train/ 与 eval/ 两个子目录，内部布局要符合该数据集的契约
#    （UHDM 扁平配对 / AIM 要 clear+moire / TIP 要 source+target）
#    契约清单见 src/configs/README.md

# 3. 训练时改名即可
python src/train.py --config uhdm_4k_s --dataset mydata
```

**布局放错的典型症状**：

| 症状 | 原因 |
|---|---|
| `FileNotFoundError` 在 `os.listdir(...)` | 缺 `train/` 或 `eval/`，或缺少内部分目录（如 AIM 的 `clear/`） |
| 训练正常开始但 `iters` 极少 / 报 `drop_last` 丢空批次 | 目录在但没找到图 → 命名不符合契约（UHDM 必须是 `NNNN_gt.jpg` + `NNNN_moire.jpg`） |
| `image ... is WxH, smaller than CROP_SIZE` | 图比 `CROP_SIZE` 小。把小图放大会引入混叠，应改小 `--crop-size` |

## 划分训练/评估：`tools/make_split.py`

官方 tar 解压出来往往只有一组图，必须自己划分。**必须按整张图划分** ——
同一张图的裁块不能同时出现在训练和评估集里，否则是数据泄漏，评估分会虚高。

```bash
python tools/make_split.py                                  # uhdm: pairs/ -> train/ + eval/ (400/100)
python tools/make_split.py --n-train 300 --force            # 换比例，重建
```

用**硬链接**而非复制（500 对约 5.7 GB，复制会翻倍）。跨盘或不支持硬链接的文件系统会自动退回复制。

> 📌 因为 `train/` 与 `eval/` 是硬链接，**搬硬盘时不必搬它们**，在目标机器上重跑
> `tools/make_split.py` 即可重建，省一半体积。

## 关于分辨率的硬约束

模型的输入边长**必须是 32 的倍数**（网络有 2 次 `pixel_unshuffle` + 4 级下采样），
理论下限约 224。裁剪尺寸取 384~512 收益最大且饱和（见 `experiments/context_study.py`）。

⚠️ **不要把大图 resize 后再训练/评估**。摩尔纹的空间频率与采集分辨率强绑定，
重采样会把它混叠折叠成现实中不存在的低频纹理（见 `docs/训练/说明_交付_20260910.md` 3.4）。
正确做法是在**原生分辨率**上裁块。

## 实际数据位置（2026-10-23 补记）

> 本机（Linux 开发机）的 `datasets/` 曾一度是空的 —— 数据没删，在 **CP2 移动硬盘**上。

| 项 | 路径 |
|---|---|
| 原始数据 | `/media/felix/CP2 2TB A/ai/demoir-fpga/demoir-fpga-main/uhdm_data/Train/pair_*/` |
| 规模 | **4500 对** `<id>_gt.jpg` + `<id>_moire.jpg`，4032×3024 RGB，38 GB |
| 本仓库软链接 | `datasets/uhdm/eval/`（100 对）、`datasets/uhdm/train/`（200 对） |

**为什么用软链接**：38 GB 不必复制；`src/eval.py` 的 `list_pairs()` 要求**扁平目录**且
文件名**前 4 位**是唯一 id（`0000_gt.jpg` ↔ `0000_moire.jpg`），而原始数据的 id 在
每个 `pair_XX/` 子目录里会重复，所以软链接做了**重编号**（跨 pair 随机抽样后编号 0000 起）。

重建命令见 `docs/部署/记录_20261023_数据集恢复与int4实验.md`；挂载点变化时只需改软链接。
