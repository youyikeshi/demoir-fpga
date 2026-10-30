# Windows 续训操作手册

> 场景：Linux 上二值训练跑到 **ep105**（ep150 的 70%）后需要切到 Windows 继续。
> 三个脚本都在 `tools/win/`，**双击即可**，全部是纯 ASCII（不含中文注释），避免 cmd 编码问题。

---

## 0. 顺序

```
00_check_env.bat   →   01_resume_train.bat   →   02_eval.bat
（自检，10 秒）        （续训 ep105→150）        （评估）
```

---

## 1. 三个脚本各自干什么

### `00_check_env.bat` — 环境自检（**先跑这个**）

检查四件事，任一 FAIL 就不要启动训练：

| 检查 | 失败时的处置 |
|---|---|
| Python + torch + CUDA | 装 `torch==2.7.0+cu118`（README 记录的 40 系组合）|
| `tensorboardX` / `lpips` | `pip install tensorboardX lpips` |
| **训练集路径** | 见下面第 2 节，Windows 上软链接失效 |
| 二值模型自检（参数量/形状/STE 梯度）| 应打印 `FP 5.961 M / 二值等价 0.305 M` |

### `01_resume_train.bat [起点] [终点]` — 续训

默认 **ep105 → ep150**。也可以手动指定：

```bat
tools\win\01_resume_train.bat 105 150
```

行为：
- `--load-epoch 105` 会**同时恢复 optimizer 与 lr**，所以不会打断 `T_0=50` 的 LR 周期；
- 每 5 epoch 存一次盘（`runs\bin_base\net_checkpoints\`），**随时 Ctrl+C 都不会丢太多**，重跑本脚本即可继续；
- 日志同时输出到屏幕和 `runs\bin_base\train_win.log`。

### `02_eval.bat [epoch] [标签]` — 评估

400 块（100 对 × 4 裁块、原生 512、不 resize、seed 0），口径与 Linux 完全一致。

```bat
tools\win\02_eval.bat 150 stage150
```

---

## 2. ⚠️ 唯一需要可能手改的地方：训练集路径

Linux 上 `datasets/uhdm_full/train` 是**指向 CP2 盘的软链接**：

```
datasets/uhdm_full/train  ->  /media/felix/CP2 2TB A/ai/demoir-fpga/demoir-fpga-main/uhdm_data/Train
```

**这个软链接在 Windows 上无效**。所以两个脚本都改成自动探测：

```bat
set "SUFFIX=ai\demoir-fpga\demoir-fpga-main\uhdm_data\Train"
for %%D in (E F G H I D C) do (
    if not defined TRAIN if exist "%%D:\%SUFFIX%\pair_00" set "TRAIN=%%D:\%SUFFIX%"
)
```

它会按 `E F G H I D C` 顺序找 `pair_00` 子目录。**如果 CP2 在别的盘符或路径不同**，在 `01_resume_train.bat` 里找到 `set "TRAIN="` 那一行，直接写死：

```bat
set "TRAIN=F:\ai\demoir-fpga\demoir-fpga-main\uhdm_data\Train"
```

判断标准：该目录下应有 **28 个 `pair_*` 子目录、4500 对 `*_gt.jpg` / `*_moire.jpg`**。

---

## 3. 当前训练进度（交接状态）

| 项 | 值 |
|---|---|
| 已跑到 | **ep105**（checkpoint `checkpoint_000105.tar`，70 MB，已校验 1609 张量）|
| 剩余 | ep105 → 150，**45 epoch ≈ 8 小时**（Windows 上速度可能不同）|
| 任务 | `base` 档（纯二值 W1A1，无 MABG/SGRA）|

### 三个 LR 周期底的实测成绩（400 块）

| epoch | lr | 中位 PSNR | SSIM 中位 | ΔPSNR vs dense |
|---|---|---:|---:|---:|
| 50 | 1e-6（周期底）| **17.7107** | 0.7257 | −1.48 |
| 100 | 1e-6（周期底）| **17.5229** | **0.7743** | −1.67 |
| **150** | 1e-6（周期底）| 待测 | 待测 | — |

对比口径（务必记住）：

- **dense 基线**（同一批 400 块）：均值 19.7995 / 中位 19.1906 / SSIM 中位 0.8144；
- **论文**：FP 22.12 → 二值 19.36，**掉幅 −2.76 dB**；
- **我们**：掉幅 −1.48（ep50）/ −1.67（ep100）⇒ **比论文小 40%**。

> ⚠️ 不要拿我们的 17.5 直接减论文的 19.36——两边评估集不同（我们的输入 PSNR 15.19，
> 项目早期 README 记录的那批块是 17.89）。**只能比"相对 dense 的掉幅"**。

### 曲线形状说明（别误判为发散）

训练中间点会大幅掉分，这是 `CosineAnnealingWarmRestarts(T_0=50)` 的设计：

| epoch | 50 | 60 | **75** | 90 | 100 |
|---|---:|---:|---:|---:|---:|
| 中位 PSNR | 17.71 | 17.47 | **15.51** | 16.78 | **17.52** |

$$\text{周期底才是好点}，\text{中间点（高 lr）掉 } 1\text{–}2\ \text{dB 属正常}$$

所以：**交付必须取周期底的 checkpoint，不能取最后一个**（ep150 结束后要扫一遍选最优）。

---

## 4. 训练结束后要做什么

1. `02_eval.bat 150 stage150` 拿最终数字；
2. **扫全部 checkpoint 选最优**（不一定是 ep150）：
   ```bat
   for %E in (110 115 120 125 130 135 140 145 150) do tools\win\02_eval.bat %E stage%E
   ```
3. 用最优 checkpoint 复核定点误差表（这一步决定硬件面积）：
   ```bat
   python tools\bin_ref_model.py --real --ckpt runs\bin_base\net_checkpoints\checkpoint_00150.tar
   ```
4. 把结果同步回 Linux（同一个仓库，`runs/` 目录会自动出现在两边）。

---

## 5. 已知与论文的偏离（写报告时必须列）

| 项 | 论文 | 我们 | 影响 |
|---|---|---|---|
| crop | 768 | **384** | 12 GB 显存放不下 768/bs2（实测 96% 显存后吞吐崩到 0.04 it/s）|
| 显存 | — | 9471/11600 MiB | 640 与 512 都 OOM，只有 384 能跑 |
| 其它 | batch 2、150 ep、Adam(0.9,0.999)、lr 2e-4、cosine warm restart | **完全一致** | — |
