# 显示构图自检报告（纯 PS）

- 结论：**PASS** —— 4 种显示模式 × 3 组输入 = 12 组，全部判据逐条通过，黑边高度与缩放比与规范 §1.1 完全一致。

- 规范：`docs/camera/03_显示构图规范_纯PS.md`（唯一需求来源）
- 被测模块：`ps/pynq/display_layout.py`
- 自检脚本：`tools/display_selftest.py`
- 复现命令：`python3 tools/display_selftest.py --out /media/felix/Data/coding/demoir-fpga/runs/display_layout`

## 输入来源

| 组 | 来源 |
|---|---|
| `color` | synthetic:纯色 左红(255,0,0) / 右蓝(0,0,255) |
| `checker` | synthetic:8x8 棋盘，每格 32 px |
| `photo` | real:runs/2026-09-10_uhdm4k_s512/train_visual/visual_x0100_00001.jpg, runs/2026-09-10_uhdm4k_s512/train_visual/visual_x0100_00002.jpg |

## 布局参数（规范 §1.1 期望 vs 实测计划）

| 显示模式 | 缩放 s | 内容区 | 上/下黑边 | 期望上/下 | 一致 |
|---|---:|---|---:|---:|:--:|
| 1280×720 | 2.5 | 1280×640 | 40/40 | 40/40 | ✅ |
| 1920×1080 | 3.75 | 1920×960 | 60/60 | 60/60 | ✅ |
| 1024×768 | 2.0 | 1024×512 | 128/128 | 128/128 | ✅ |
| 2560×1440 | 5.0 | 2560×1280 | 80/80 | 80/80 | ✅ |

## 判据表（逐条 PASS/FAIL）

`n/a` = 该条判据对当前输入不适用（例如「纯色无第三色」只在纯色输入上有意义）。

| 判据 | 说明 | 1280×720<br>`color` | 1280×720<br>`checker` | 1280×720<br>`photo` | 1920×1080<br>`color` | 1920×1080<br>`checker` | 1920×1080<br>`photo` | 1024×768<br>`color` | 1024×768<br>`checker` | 1024×768<br>`photo` | 2560×1440<br>`color` | 2560×1440<br>`checker` | 2560×1440<br>`photo` |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| C1 | 输出形状 (H,W,3)/uint8/C 连续 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| C2 | 内容左上=orig[0,0]；中线右侧第一列=proc[0,0] | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| C3 | 第 0 行与第 H-1 行全黑 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| C4 | 黑边高度=公式值（上下差<=1） | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| C5 | 纯色输入：全图仅 {红,蓝,黑}，无第三种来源 | ✅ | n/a | n/a | ✅ | n/a | n/a | ✅ | n/a | n/a | ✅ | n/a | n/a |
| C6 | 同输入两次调用逐字节相同 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| C7 | 内容区与独立实现(np.repeat 绘制法)逐字节一致 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| C8 | 缩放比实测 == W/512 == 2.5/3.75/2.0/5.0 | n/a | ✅ | n/a | n/a | ✅ | n/a | n/a | ✅ | n/a | n/a | ✅ | n/a |
| C9 | 横纵等比：32 源像素横向=纵向=32*s（无拉伸） | n/a | ✅ | n/a | n/a | ✅ | n/a | n/a | ✅ | n/a | n/a | ✅ | n/a |
| C10 | 镜像敏感性（meta，证明判据对方向敏感） | n/a | ✅ | ✅ | n/a | ✅ | ✅ | n/a | ✅ | ✅ | n/a | ✅ | ✅ |

## 判据自检（破坏注入：证明 PASS 不是空的）

把**正确的输出**按各种典型方式弄坏，每种都必须被至少一条判据抓到；否则判据就是摆设。

| 注入的破坏 | 是否被抓到 | 触发的判据 |
|---|:--:|---|
| 左右镜像 | ✅ | ['C2', 'C7'] |
| orig/proc 互换 | ✅ | ['C2', 'C7'] |
| 整幅下移 2px（黑边错位） | ✅ | ['C2', 'C4', 'C7', 'C9'] |
| 均值模糊（模拟浮点插值） | ✅ | ['C2', 'C4', 'C7', 'C8', 'C9'] |
| 黑边写成 (1,1,1) | ✅ | ['C3', 'C4', 'C7'] |
| 输出非 C 连续（Fortran 序） | ✅ | ['C1'] |
| 拉伸变形（纵向撑满，sx≠sy） | ✅ | ['C3', 'C4', 'C7', 'C9'] |
| 索引四舍五入代替 floor（差 1px） | ✅ | ['C7', 'C8', 'C9'] |

> 对照组：未注入破坏的正确输出，触发判据 `无` ⇒ ✅ 无失败（判据不误报）。


## 黑边高度与缩放比（机械实测）

| 显示模式 | 上黑边 | 下黑边 | 上下差 | 缩放比 | 每格(32 源px)显示宽 |
|---|---:|---:|---:|---:|---:|
| 1280×720 | 40 | 40 | 0 | 2.5 | 80 px |
| 1920×1080 | 60 | 60 | 0 | 3.75 | 120 px |
| 1024×768 | 128 | 128 | 0 | 2.0 | 64 px |
| 2560×1440 | 80 | 80 | 0 | 5.0 | 160 px |

> 判据 C8/C9 不是读内部索引表，而是从输出 PNG 的棋盘格游程反推：左半 `W/2` 列承载 256 个源像素 ⇒ 实测比例 `(W/2)/256 = W/512`；一格 32 源像素在横/纵两个方向都必须正好是 `32*s` 显示像素 ⇒ 等比、无拉伸。

## 产物 PNG（相对 `runs/display_layout/`）

代表 4 张（每个显示模式一张，`color` 纯色输入，机械可验「左红右蓝 + 纯黑黑边」）：

- `display_layout/1280x720_color.png` —— 1280×720，黑边上 40 / 下 40，s=2.5
- `display_layout/1920x1080_color.png` —— 1920×1080，黑边上 60 / 下 60，s=3.75
- `display_layout/1024x768_color.png` —— 1024×768，黑边上 128 / 下 128，s=2.0
- `display_layout/2560x1440_color.png` —— 2560×1440，黑边上 80 / 下 80，s=5.0

全部 12 张（`raw_md5` = 整幅原始像素的 md5，**与 PNG 编码器无关**，板端比对请用这个）：

| 显示模式 | 输入 | PNG | raw_md5 |
|---|---|---|---|
| 1280×720 | `color` | `display_layout/1280x720_color.png` | `bb92e6ecd0d67304f9721888949e4d3d` |
| 1280×720 | `checker` | `display_layout/1280x720_checker.png` | `feff9378a96cf20069c7bb72a71ec286` |
| 1280×720 | `photo` | `display_layout/1280x720_photo.png` | `c20c3cccf0967a6e26af2d8a2898e613` |
| 1920×1080 | `color` | `display_layout/1920x1080_color.png` | `858d4f5a55ecadd04c6de9641e16a63b` |
| 1920×1080 | `checker` | `display_layout/1920x1080_checker.png` | `e5bc3064c4c972bad0540d25a4b93c66` |
| 1920×1080 | `photo` | `display_layout/1920x1080_photo.png` | `13322038b9548bc8a0a41c1aaf53173c` |
| 1024×768 | `color` | `display_layout/1024x768_color.png` | `2a17576ec2438a39b9787bb302de5552` |
| 1024×768 | `checker` | `display_layout/1024x768_checker.png` | `94059fb8fe869feda5d678641598282c` |
| 1024×768 | `photo` | `display_layout/1024x768_photo.png` | `398ec60c1026bcd994693b5a75ec2d6e` |
| 2560×1440 | `color` | `display_layout/2560x1440_color.png` | `714a9936f7a6fcfb0926ddfd8c7930ee` |
| 2560×1440 | `checker` | `display_layout/2560x1440_checker.png` | `4738492d7fe889a248ff2772fdf8945b` |
| 2560×1440 | `photo` | `display_layout/2560x1440_photo.png` | `41383877ee2eb47e79c71349966c41f1` |

> `checker` 用于肉眼验缩放锯齿/变形，`photo` 用于肉眼验左原图/右处理图的观感。PNG 只落在 `runs/` 下，**不进 git**。

## 上板复现（跨机逐字节）

主机与板端跑同一份代码，**输入必须逐字节相同**，否则比 md5 没意义。两种做法：

1. 主机 `python3 tools/display_selftest.py --dump-inputs` 会把用到的 6 张 256² 输入落盘到 `runs/display_layout/inputs/`；把这 6 个 PNG 一起同步到板端，板端跑 `--from-inputs <该目录>`。
2. 比对**整幅原始像素的 md5**（上表 `raw_md5` 列，控制台也会打印），不要比 PNG 文件 md5 —— PNG 会随 zlib/PIL 版本变化。

```bash
# 主机（本机）
python3 tools/display_selftest.py --out runs/display_layout --dump-inputs
# 板端（等机器空闲；本阶段不需要 overlay/位流）
rsync -a runs/display_layout/inputs/ xilinx@192.168.7.2:/home/xilinx/demoir/inputs/
cd /home/xilinx/demoir && python3 tools/display_selftest.py \
  --out /home/xilinx/runs/display_layout --from-inputs /home/xilinx/demoir/inputs
```

## 结论

画面构图正确：左原图 / 右处理图（不镜像），先撑满宽度等比放大（s = W/512），剩余高度上下对称填黑，黑边为纯黑 `(0,0,0)`；四种模式的黑边高度与缩放比与规范 §1.1 完全一致，同输入两次调用逐字节相同 ⇒ 可进入下一步（板端复跑比对 md5）。
