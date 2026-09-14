# src/configs/ —— 训练配置

## 设计：配置里不写路径，只写名字

```yaml
DATA:
  DATASET: uhdm            # -> datasets/uhdm/{train,eval}
TEACHER_MODEL:
  TEACHER: ghost_34large   # -> weights/weights.yaml 里登记的权重
```

`src/train.py` 会把这些名字解析成**绝对路径**，写一份快照到
`runs/<run>/config.yaml`，再交给 `vendor/` 里的上游代码。

好处：**配置里没有任何 `../../` 相对路径**，所以从哪个目录运行都一样，
换数据集 / 换机器 / 换盘符都不用改配置。

## 现有配置

| 文件 | 用途 |
|---|---|
| `uhdm_4k_s.yaml` | **主力**：UHDM 4K 原生裁块 512，含 teacher 蒸馏 |
| `uhdm_4k_s_resume.yaml` | 续训模板（要改 `RUN` / `LOAD_EPOCH` / `EPOCHS` 三处） |
| `uhdm_4k_s_noteacher.yaml` | 不用 teacher，单独训 student |
| `smoke.yaml` | 冒烟测试，用合成数据，几分钟出结果 |
| `aim_1k_s.yaml` | AIM2019/LCDMoire（1024×1024）模板 |
| `tip_256_s.yaml` | TIP2018（256×256）模板 |

上游作者自带的 9 份配置（`uhdm_config.yaml`、`aim_large_config.yaml` 等）
留在 `vendor/PDNet/config/`，当参考用，不属于我们。

## 换一个数据集重训，怎么做

### 情况一：数据已按本项目约定放好

```bash
# 1. 把数据放到 datasets/<名字>/{train,eval}/
# 2. 复制一份配置
cp src/configs/uhdm_4k_s.yaml src/configs/mydata_4k_s.yaml
# 3. 改 DATASET 一行
#    DATA:
#      DATASET: mydata
# 4. 训练
python src/train.py --config mydata_4k_s
```

也可以完全不建配置文件，直接在命令行覆盖：

```bash
python src/train.py --config uhdm_4k_s --dataset mydata --name mydata_c512_s
```

### 情况二：数据在别处，不想搬

```bash
# --dataset-root 指向一个含 train/ 与 eval/ 的目录
python src/train.py --config uhdm_4k_s --dataset-root /mnt/disk2/mydata
```

### 情况三：数据的目录布局与约定不同

```bash
python src/train.py --config uhdm_4k_s \
    --train-dataset /path/to/train --eval-dataset /path/to/eval
```

## ⚠️ 各数据集的目录布局契约（换数据集最容易踩的坑）

布局要求**由上游 `dataset/load_data.py` 的四个 loader 决定**，不是我们定的。
放错位置的症状是 `os.listdir` 直接抛异常，或静默找到 0 张图。

| `DATA_TYPE` | 训练集布局 | 测试集布局 | 备注 |
|---|---|---|---|
| `UHDM` | 扁平放 `NNNN_gt.jpg` + 同目录 `NNNN_moire.jpg` | 同左 | **最宽松**：`os.walk` 递归找 `*_gt.jpg`，所以子目录随便分 |
| `AIM` | `moire/*.jpg` + `clear/*.jpg` | `moire/*.png` + `clear/*.png` | ⚠️ 测试集只认 `.png` |
| `TIP` | `source/*.png` + `target/*.png` | 同左 | 文件名须成对（`X_source.png` ↔ `X_target.png`） |
| `FHDMi` | `source/src_XX.png` + `target/XX.png` | 同左 | 上游项目未用到，仅备查 |

UHDM 的命名规则是**硬编码**的：`NNNN_gt.jpg` 是标签，同目录的 `NNNN_moire.jpg` 是输入，
`NNNN` 是前 4 个字符（见 `uhdm_data_loader.__getitem__`）。所以自定义数据要遵循这个命名。

## 这些键是什么

| 键 | 说明 |
|---|---|
| `DATA.DATA_TYPE` | 选哪个 loader：`UHDM` / `AIM` / `TIP` / `FHDMi` |
| `DATA.DATASET` | `datasets/` 下的名字（也可用 `TRAIN_DATASET`/`TEST_DATASET` 写死路径） |
| `TRAIN.LOADER` | `crop` 原生随机裁块 / `resize` 缩放 / `default` 整图。**UHDM 4K 一定要用 crop** |
| `TRAIN.CROP_SIZE` | 必须是 **32 的倍数**（网络有 2 次 pixel_unshuffle + 4 级下采样） |
| `TRAIN.SAVE_EPOCH` | 存盘间隔（我们加的，上游固定每 10 epoch）。调小可降低中断代价 |
| `TRAIN.LOAD_EPOCH` | 续训起点；配合 `GENERAL.RUN` 使用 |
| `GENERAL.RUN` | 运行目录名。留空则自动生成；**续训必须锁定原目录** |
| `USE_TEACHER` | 顶层键（不在任何 section 下）。`False` 则只做 L1 + VGG 感知 |
| `MODEL.*` | student 结构参数。必须与要加载的 checkpoint 严格一致 |
| `SOLVER.EPOCHS` | 总 epoch 数 |

## 命令行覆盖

`src/train.py` 支持用命令行覆盖配置里的任意常用键，不必建新文件：

```bash
python src/train.py --config uhdm_4k_s --epochs 100 --crop-size 384 --batch-size 4
python src/train.py --config uhdm_4k_s --no-teacher
python src/train.py --config uhdm_4k_s --teacher ghost_34large --name trial1
```

完整列表见 `python src/train.py --help`。
