# 演示用图对包（`pcref` 模式 / `bin`）

**生成时间**：2026-11-03 20:11:13 +0800 ｜ **生成器**：`tools/prepare_demo_pairs.py` ｜ **对数**：8

## 这是什么

板端 HDMI 演示播放器（`pynq_specialized_code/ps/pynq/demo_player.py`）**只读**
`manifest.json`，按顺序把每一对贴成整幅 `1280x720`，**每秒 1 帧**轮播。

```
manifest.json            ← 板端唯一入口
pairs/NNNN_left.{ext}    ← 640x640 RGB888，屏幕左半（原图 · 摩尔纹）
pairs/NNNN_right.{ext}   ← 640x640 RGB888，屏幕右半（去摩尔纹结果）
```

屏幕布局（与 `display_layout.compose(W=1280,H=720)` **同一几何**，上下各 **40 行纯黑**）：

```
y=0                        上 40 行黑边
y=40..679    ┌──── 640 ────┬──── 640 ────┐   每半正好 640x640
             │  left       │  right      │   （= 存储尺寸 1:1，板端不缩放）
y=680..719                 下 40 行黑边
```

- 每张 **1228800 B**（`640*640*3`，RGB888 连续，行首无 padding）
- `format=bin`：板端 `np.fromfile(...).reshape(640,640,3)`，最快
- `format=png`：便于人眼检查（体积大，板端要用 PIL 解码）

## 图对怎么来的

- 左 = `datasets/uhdm/eval/NNNN_moire.jpg` 的**中心 256² 裁块**（原始像素）
- 右 = `bin_fp_ref.py` 对**同一裁块**的输出 `out1` → `round(255*clip(x,0,1))`（PC 浮点参考）
- 原始裁块与网络输出都留在 `pcref_raw/`（`.npy`），可复算

**放大方法**：`bilinear`，`256 → 640`（ratio 2.5）。**左右两侧用同一种放大**，
所以左右对比里不存在"一边更锐"的偏差。放大是纯显示操作，不做任何算法。

## 没有显示补偿

右半是 **PC 上的浮点参考**（`pynq_specialized_code/ps/tools/bin_fp_ref.py`，
不依赖 torch 的 numpy 实现；权重 `runs/bin_base/net_checkpoints/checkpoint_latest.tar`）。它直接 `clip(0,1)×255` 转 uint8，
**不做任何对比补偿、拉伸或水印式"美化"**。

> 板端 int8 输出发灰（端到端 `out = 0.148*in + 0.310`）这条路径**不参与演示**，
> 所以本包无需补偿，也就没有"补偿声明"。板端只负责把这 640x640 的两张图贴到屏幕上。

`net_stats.json` 里存了每张图 `out1` 的 `min/max/mean/std` 与裁剪比例 —— 将来要查
"PC 浮点(好) vs 板端 int8(灰)"的量化代价，直接拿它跟板端输出比，不用重跑。

## 选图

在实际显示的 **256² 中心裁块**上打分 `score = 边缘密度 × 双峰性`（文字/表格底分数高）。
用户看过的 `0094`、`0049` **强制排在前两张**，其余按分数取。

| # | 名字 | score | corr(left,right) | 强制 |
|---|---|---|---|---|
| 0 | 0094 | 0.632 | 0.519 | 是 |
| 1 | 0049 | 0.679 | 0.499 | 是 |
| 2 | 0060 | 0.624 | 0.677 |  |
| 3 | 0084 | 0.604 | 0.718 |  |
| 4 | 0062 | 0.555 | 0.573 |  |
| 5 | 0089 | 0.527 | 0.762 |  |
| 6 | 0097 | 0.521 | 0.593 |  |
| 7 | 0001 | 0.511 | 0.934 |  |

## 已知限制

1. 左右都是 **256² 中心裁块的 2.5× 放大**（不是全幅），所以右半的细节上限就是 256²。
   这是网络输入分辨率决定的，不是显示通路。
2. `gt` 模式下 `gt` 与 `moire` **不是像素级配准**的，只能当"同场景的干净参考"，
   不能据此算 PSNR。
3. 水印/标签是**烘进像素**的（板端不做文字渲染），因此也计入 md5。

## 重新生成

```bash
python3 tools/prepare_demo_pairs.py                          # 8 对（pcref），默认 bin
python3 tools/prepare_demo_pairs.py --format png             # 便于人眼检查
python3 tools/prepare_demo_pairs.py --verify                 # 复算 md5 + compose 几何断言
```

板端：

```bash
nohup sudo -S -p '' env XILINX_XRT=/usr /usr/local/share/pynq-venv/bin/python3 \
  /home/xilinx/demo/demo_player.py --pairs /home/xilinx/demo/pairs \
  --log /home/xilinx/demo/player.log >/dev/null 2>&1 &
echo START | nc 192.168.7.2 9999        # 或 nc -u / touch /home/xilinx/demo/START.flag
```
