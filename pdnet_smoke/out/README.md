# `pdnet_smoke/out` —— 实验产物索引

> 这里只保留**被文档引用**的最终产物。所有中间结果都可由 `pdnet_smoke/*.py` 重跑得到，
> 因此已清理掉被取代的版本（省了 7.2 MB）。

## 保留的文件

### 推理演示（`demo_infer.py`）

| 文件 | 说明 |
|---|---|
| `demo/demo_compare.png` | **可视化对比图**：4 行样本 × 4 列 = `摩尔纹输入 \| student \| teacher \| 真值`。样本取自 `split_eval`（student 训练时完全未见过），512² 原生裁块 |
| `demo/demo_<id>_{1moire,2student,3teacher,4gt}.png` | 单张结果，便于放大查看。样本 0416（典型）、0401（较好）、0433（最好）、0443（**最差，失败案例**） |

样本选取依据（来自 `eval_real_student_c512_n60.csv` 的提升分布）：
均值 **+2.50 dB**、中位 **+2.18 dB**、**82%（49/60）的样本有提升**。

### 真实数据评估（`eval_real.py`）

| 文件 | 对应文档 | 说明 |
|---|---|---|
| `eval_real_teacher_c512_n100x4.csv/.log/.png` | 交付说明 **3.5** | **teacher 主结果**：100 张图 × 4 位置 = 400 个块，512² 原生裁块。中位数 **22.10 dB**（与 ESDNet 论文 22.12 吻合） |
| `eval_real_teacher_c512_n60.csv/.log` | 交付说明 **3.5** | teacher 在 60 张评估图上的结果（**与 student 同一批数据**，用作公平上界。Δ **+6.03 dB**） |
| `eval_real_student_c512_n60.csv/.log/.png` | 交付说明 **3.5** | **student 结果**（30 epoch / 6000 步）：Δ **+2.50 dB**，SSIM 0.7123 |

`.csv` 逐样本指标（id, in_psnr, out_psnr, out_ssim, out_lpips, sec）；
`.png` 是 `摩尔纹 | 模型输出 | 真值` 三列对比图。

### 训练日志

| 文件 | 说明 |
|---|---|
| `real_resume.log` | 完整训练日志（epoch 8~30）。**首次训练日志（epoch 1~7）已删**，它在 epoch 8 因 `0xC0000374` 堆损坏崩溃，该事件记录在交付说明 5.3 |

## 已清理的文件（可重跑复现）

| 已删文件 | 为什么删 | 怎么复现 |
|---|---|---|
| `eval_real_teacher_c{256,512,768}_n30x2_*` | 裁切尺寸对照，结论已被 `context_study.py` 的受控实验取代（更干净） | `python pdnet_smoke/eval_real.py --role teacher --crop {256,512,768}` |
| `eval_real_teacher_c512_n20_*` | 样本量太小的早期探测 | `--limit 20` |
| `compare_grid_900iters.png`、`curve_900iters.csv`、`train_curve.log` | 合成数据冒烟测试产物（12 张图过拟合，无使用价值） | `python pdnet_smoke/train_curve.py --iters 900` |
| `real_train.log` | 被 `real_resume.log` 覆盖（崩在第 8 epoch） | `python train.py --config ./config/real_config.yaml` |

## 相关脚本

| 脚本 | 产出 |
|---|---|
| `demo_infer.py` | `demo/` 下的可视化对比图与单张结果 |
| `eval_real.py` | 本目录的 `eval_real_*` |
| `train_curve.py` | 合成数据训练曲线 |
| `context_study.py` | 上下文影响（结论：384~512 最优） |
| `crop_size_study.py` | 裁切尺寸对照 |
| `teacher_domain_gap.py` | teacher 域错配（结论：下采样后 teacher 掉 2.3~2.9 dB） |
| `downsample_study.py` | 下采样对频率成分的影响 |
| `make_synth_data.py` | 合成摩尔纹数据（写入 `pdnet_smoke/synth_data/`） |
| `probe_vram.py` | 显存探测 |
