# `experiments/out` —— 实验产物索引

> 这里只保留**被文档引用的**最终产物。中间结果都可由 `experiments/*.py` 与 `src/eval.py`
> 重跑得到，因此被取代的版本已清理。
>
> 布局：`demo/` 推理演示 · `eval/` 当前 `src/eval.py` 的输出 · 根目录放历史产物与训练日志。

> ⚠️ 训练产物（checkpoint / tensorboard / 可视化图）现在归 **`runs/<运行目录>/`**，
> 每个运行目录下有自己的 `README.md` 说明用了什么配置、结果如何。**不在本目录。**

## 推理演示

| 文件 | 说明 |
|---|---|
| `demo/demo_compare.png` | **可视化对比图**：4 行样本 × 4 列 = `摩尔纹输入 \| student \| teacher \| 真值`。样本取自评估集（student 训练时完全未见过），512² 原生裁块 |
| `demo/demo_<id>_{1moire,2student,3teacher,4gt}.png` | 单张结果，便于放大查看。样本 0416（典型）、0401（较好）、0433（最好）、0443（**最差，失败案例**） |

样本选取依据（来自 `eval/eval_student_c512_n60.csv` 的提升分布）：
均值 **+2.50 dB**、中位 **+2.18 dB**、**82%（49/60）的样本有提升**。

## 定量评估

`eval/` 是现在 `src/eval.py` 的输出；根目录下的 `eval_real_*` 是已退役的
`pdnet_smoke/eval_real.py` 的历史输出。两者口径完全相同（原生裁块、同一套 PSNR/SSIM 实现），
保留历史那份是因为 `docs/` 里引用的是它。

| 文件 | 对应文档 | 说明 |
|---|---|---|
| `eval/eval_teacher_c512_n100x4.log` | 交付说明 **3.5** | **teacher 主结果**：100 张图 × 4 位置 = 400 个块，512² 原生裁块。PSNR 中位数 **22.10 dB**（与 ESDNet 论文 22.12 吻合） |
| `eval_real_teacher_c512_n100x4.{csv,log,_grid.png}` | 交付说明 **3.5** | 同上，历史输出（含逐样本 csv 与对比图） |
| `eval_real_teacher_c512_n60.{csv,log}` | 交付说明 **3.5** | teacher 在 60 张评估图上的结果（**与 student 同一批数据**，用作公平上界。Δ **+6.03 dB**） |
| `eval/eval_student_c512_n60.{csv,log,_grid.png}` | 交付说明 **3.5** | **student 结果**（30 epoch / 6000 步）：Δ **+2.50 dB**，SSIM 0.7123 |
| `eval_real_student_c512_n60.{csv,log,_grid.png}` | 交付说明 **3.5** | 同上，历史输出。**这份是重构后的回归基准**：重跑应复现 Δ +2.50 dB |

`.csv` 列：`id, in_psnr, out_psnr, out_ssim, out_lpips, sec`（逐样本指标）；
`.png` 是 `摩尔纹 | 模型输出 | 真值` 三列对比图。

## 训练日志

| 文件 | 说明 |
|---|---|
| `real_resume.log` | 完整训练日志（epoch 8~30）。**首次训练日志（epoch 1~7）已删**，它在 epoch 8 因 `0xC0000374` 堆损坏崩溃，该事件记录在交付说明 5.3 |
| `real_continue.log` | 更早的一次训练日志 |

## 已清理的文件（可重跑复现）

| 已删文件 | 为什么删 | 怎么复现 |
|---|---|---|
| `eval_real_teacher_c{256,512,768}_n30x2_*` | 裁切尺寸对照，结论已被 `context_study.py` 的受控实验取代（更干净） | `python src/eval.py --role teacher --limit 30 --crops-per-image 2 --crop {256,512,768}` |
| `eval_real_teacher_c512_n20_*` | 样本量太小的早期探测 | `--limit 20` |
| `compare_grid_900iters.png`、`curve_900iters.csv` | 合成数据冒烟测试产物（12 张图过拟合，无使用价值） | `python experiments/train_curve.py --iters 900` |
| `real_train.log` | 被 `real_resume.log` 覆盖（崩在第 8 epoch） | `python src/train.py --config uhdm_4k_s` |

## 产出这些的脚本

| 脚本 | 产出 |
|---|---|
| `src/eval.py` | `eval/` |
| `src/infer.py` | `out/infer/`（预览图，不进 git） |
| `experiments/train_curve.py` | 合成数据训练曲线（写到 `out/curve_runs/`，不进 git） |
| `experiments/context_study.py` | 上下文影响（结论：384~512 最优） |
| `experiments/crop_size_study.py` | 裁切尺寸对照 |
| `experiments/teacher_domain_gap.py` | teacher 域错配（结论：下采样后 teacher 掉 2.3~2.9 dB） |
| `experiments/downsample_study.py` | 下采样对频率成分的影响 |
| `experiments/make_synth_data.py` | 合成摩尔纹数据（写入 `samples/synth/`） |
| `experiments/probe_vram.py` | 显存探测 |
