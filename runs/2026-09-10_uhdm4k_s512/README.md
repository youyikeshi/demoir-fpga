# run: `2026-09-10_uhdm4k_s512`

首个可用的 student 就是在这里训出来的。

## 这次训练用了什么

| 项 | 值 |
|---|---|
| 配置 | `src/configs/uhdm_4k_s.yaml`（快照见本目录 `config.yaml`） |
| 数据 | `datasets/uhdm/{train,eval}` —— UHDM 官方 test 集按**图**划分成 400 / 100 |
| 裁剪 | `LOADER: crop`，原生 3840×2160 上随机裁 **512**，不做任何重采样 |
| 模型 | student `my_model(48,32,64,32,1)` = 1.562 M 参数 |
| teacher | `weights/teacher/ghost_34large_UHDM_ep150.tar`（论文作者产物，冻结） |
| 步数 | 30 epoch × 200 步 = **6000 步** |

## 训练过程（不是一次跑完的）

| 阶段 | epoch | 用了哪份配置 |
|---|---|---|
| 第一次 | 1~7 | `uhdm_4k_s` |
| 中断 | —— | 进程级崩溃（`0xC0000374` 堆损坏，与本项目代码无关），停在 epoch 8 |
| 续训 | 8~30 | `uhdm_4k_s_resume`（`LOAD_EPOCH: 7` → 续到 30） |

续训前的操作是把 `checkpoint_latest.tar` 复制成 `checkpoint_000007.tar`，
详见 `docs/交付说明_20260910.md` 4.3。**现在这条已由 `src/train.py` 的快照机制覆盖**，
但上游仍按 `checkpoint_%06d.tar` 找断点，所以那个复制动作依然要做。

## 结果

同一批 60 张评估图、crop 512 的公平对照：

| 模型 | 步数 | 输入 | 输出 | Δ | SSIM |
|---|---|---|---|---|---|
| student | 1,400 | 16.83 | 17.51 | +0.67 | 0.6428 |
| **student（本目录）** | **6,000** | 16.83 | **19.33** | **+2.50** | **0.7123** |
| teacher（上界参考） | — | 16.83 | 22.86 | +6.03 | 0.7760 |

**仍是欠训练状态**：论文是 150 epoch × 2250 步 ≈ 33.7 万步，我们只跑到 1.8%。
结果是"代码正确、能收敛、方向对、有真实提升"，**不代表论文指标可复现**。

## 目录内容

| 路径 | 说明 | 进 git 吗 |
|---|---|---|
| `config.yaml` | 全绝对路径的配置快照（上游实际读的就是这份） | ✅ |
| `run_info.yaml` | 可读的本次运行记录（数据集/权重/epochs/主机） | ✅ |
| `net_checkpoints/` | `checkpoint_000007/000010/000020/000030/latest.tar`，各 18.3 MB | ❌ 走移动硬盘 |
| `logs/` | tensorboard 事件 | ✅ |
| `train_visual/` | 训练过程中的可视化对比图 | ✅ |
| `train.log` | 训练日志 | ✅ |

## 怎么用它

```bash
# 看效果（自己导入的照片）
python src/infer.py --student uhdm4k_s512_ep30

# 定量评估（UHDM 评估集，有真值）
python src/eval.py --role student --limit 60

# 接着训
python src/train.py --config uhdm_4k_s_resume
```

`uhdm4k_s512_ep30` 是 `weights/student/` 下由 `checkpoint_000030.tar` 导出的发布版，
便于 `infer.py` / `eval.py` 直接引用；两者权重完全一致。
