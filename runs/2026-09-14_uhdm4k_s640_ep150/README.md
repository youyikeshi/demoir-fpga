# run: `2026-09-14_uhdm4k_s640_ep150`

**首个用官方全量训练集（4500 对）跑满 150 epoch 的 student。**
上一次（`2026-09-10_uhdm4k_s512`）只用了 400 张测试集划分，δ +2.50 dB 且严重欠训练；
这一次把数据量和步数都补齐，Δ 提升到 **+4.70 dB**，已逼近 teacher 的 +4.80 dB。

## 这次训练用了什么

| 项 | 值 |
|---|---|
| 配置 | `src/configs/uhdm_4k_s_ep150.yaml`（快照见本目录 `config.yaml`） |
| 数据（训练） | **官方 UHDM 训练集 4500 对**，原生 **4032×3024**，按 `pair_NN` / `pair_mi_NN` 分 28 个子目录 |
| 数据（评估） | `datasets/uhdm/eval`（100 张，来自官方 test 集划分，与训练集无重叠） |
| 裁剪 | `LOADER: crop`，原生分辨率上随机裁 **640**，不做任何重采样 |
| batch | 2 |
| 模型 | student `my_model(48,32,64,32,1)` = 1.562 M 参数 |
| teacher | `weights/teacher/ghost_34large_UHDM_ep150.tar`（论文作者产物，冻结） |
| 步数 | 150 epoch × 2250 步 = **337,500 步**（论文量级） |
| 硬件 | RTX 4070 SUPER 12GB / Windows / torch 2.7.0+cu118 / Python 3.11.9 |
| 耗时 | **约 39.7 小时**（实测稳态 2.24 it/s，显存 9557 MiB，GPU 利用率 98%） |

### 与上一次训练（6000 步）的差异

| | `2026-09-10_uhdm4k_s512` | **本目录** |
|---|---|---|
| 训练数据 | 400 对（测试集划分） | **4500 对（官方训练集）** |
| crop | 512 | 640 |
| 步数 | 6,000 | **337,500**（56 倍） |
| 最终 train loss | 4.36 | **2.39** |
| 评估 Δ（400 块） | +2.47（同口径） | **+4.70** |

> ⚠️ **crop 为什么是 640 而不是官方配置的 768**：768 + batch 2 在本机 12 GB 卡上实测占用
> 11830/12282 MiB（96%），PyTorch 分配器进入饥饿颠簸，吞吐从 2.6 it/s 崩到 **0.04 it/s**，
> 属**不可用**。640 与 512 的挂钟时间几乎相同（GPU 有余量、瓶颈不在算力），
> 且保持官方步数（2250 步/epoch）。详见 `docs/训练/说明_本机重训环境搭建.md` 5.2。

## 训练过程

一次跑完，**无中断**（上一次在 epoch 8 遇到 `0xC0000374` 进程级堆损坏，这次没有复现）。

lr 走 `CosineAnnealingWarmRestarts(T_0=50, T_mult=1, eta_min=1e-6)`，
在 epoch 51、101 处重启回 base_lr，loss 相应先反弹再下降 —— 这是官方协议的设计，不是异常。

loss 轨迹（完整逐 epoch 见 `train.log`）：

| epoch | 1 | 50 | 51（重启） | 100 | 101（重启） | 150 |
|---|---|---|---|---|---|---|
| Avg. Loss | 5.888 | 2.959 | 3.471 | 2.580 | — | **2.390** |

## 结果

**评估口径**：`datasets/uhdm/eval` 的 **100 张 × 4 位置 = 400 块**，原生 512² 裁块，
不做任何重采样。student 与 teacher 跑**同一批块**。

| 指标 | **student（1.562 M）** | teacher（4.396 M，上界） |
|---|---|---|
| 输入 PSNR 均值 / 中位 | 17.89 / 18.25 | 17.89 / 18.25（同一批） |
| 输出 PSNR 均值 / 中位 | **22.59** / 21.66 | 22.69 / 21.88 |
| Δ PSNR 均值 / 中位 | **+4.70** / +3.06 | +4.80 / +3.40 |
| SSIM 均值 / 中位 | **0.7735** / 0.8346 | 0.7777 / 0.8377 |
| 提升样本比例 | 316/400（79%） | 328/400（82%） |
| 推理耗时 | **0.058 s/块** | 0.086 s/块 |

**结论：用 36% 的参数量（1.562 M vs 4.396 M）达到 teacher 98% 的画质**
（PSNR 均值差 0.10 dB、SSIM 差 0.0042），且推理快 1.5 倍。
这与 PDNet 论文声称的「用 26.32% 参数达到可比性能」一致。

另有一组 60 张的子集对照（与上一次训练口径完全一致，便于直接比较）：
student 22.55（Δ **+5.72**，SSIM 0.7708）vs teacher 22.86（Δ +6.03，SSIM 0.7760）。

### 已知特性

在「输入本身已经很干净」的块上会**过平滑**：例如某张输入 26.37 dB 的图，
student 掉到 −10.46 dB 而 teacher 只掉 −5.09 dB。这是 `docs/训练/说明_交付_20260910.md` 3.5
早就记录的已知特性（「25% 的块反而变差」），不是本次训练引入的。

## 目录内容

| 路径 | 说明 | 进 git 吗 |
|---|---|---|
| `config.yaml` | 全绝对路径的配置快照 | ✅ |
| `run_info.yaml` | 可读的本次运行记录 | ✅ |
| `net_checkpoints/` | `checkpoint_000005/…/000150/latest.tar`，各 18.3 MB，共 31 个 | ❌ 走移动硬盘 |
| `logs/` | tensorboard 事件（loss / lr 曲线） | ✅ |
| `train_visual/` | 训练过程中的可视化对比图（67 张） | ✅ |
| `train.log` | 逐 epoch 摘要日志（原始 73 MB 的 tqdm 刷屏已裁掉） | ✅ |

发布版权重已导出到 `weights/student/uhdm4k_s640_ep150.tar`
（内容与 `net_checkpoints/checkpoint_000150.tar` 完全一致，便于 `infer.py` / `eval.py` 直接引用）。

## 怎么用它

```bash
# 看效果（自己导入的照片）
python src/infer.py --student uhdm4k_s640_ep150

# 定量评估（UHDM 评估集，有真值）
python src/eval.py --role student --limit 100 --crops-per-image 4

# 与 teacher 上界对照
python src/eval.py --role teacher --limit 100 --crops-per-image 4

# 复现这次训练（需要 datasets/uhdm_full/train 的 4500 对）
python src/train.py --config uhdm_4k_s_ep150

# 接着训（改 src/configs/uhdm_4k_s_ep150.yaml 的 LOAD_EPOCH 与 EPOCHS）
```
