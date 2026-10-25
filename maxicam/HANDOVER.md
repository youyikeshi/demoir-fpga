# 交接文档：MaixCAM Pro 视频采集/推流（AI 相机端）

> 写于 2026-10-25。本目录是「去摩尔纹」项目的**采集端**（MaixCAM Pro → 电脑/Zynq）。
> 与之配套的算法训练与 Zynq 部署见仓库根目录 `交接文档_去摩尔纹训练部署.md`。

---

## 1. 这套东西是干什么的

用 **MaixCAM Pro**（算能 SG2002）当图像采集器：

- 板子触屏上选 **分辨率 / 编码格式 / 码率**，通过 **RTSP** 把实时画面推给电脑（或 Zynq）
- 按板子上的 **USER 键**可以**截图**，截图**绕开视频编码器**、走独立通道发到电脑，
  存成**无损 PNG** —— 这是重点，因为摩尔纹是高频信息，被 H.264/JPEG 一压就没了
- 电脑端一个窗口实时看画面，左上角有 HUD（帧率/分辨率/编码/双通道状态）

一句话数据流：

```
MaixCAM Pro ──┬─ 摄像头 NV21 ──> H.264/H.265 硬编 ──> RTSP :8554 ──> 电脑窗口（有损，看画面用）
              │                                                         ↑
              ├─ 显示通道 RGB888 ─────────────────────────────────────┘（640x480 预览）
              │        └── 按键取一帧 ──> 原始像素 TCP :8556 ──> 无损 PNG（截图用）
              └─ 控制通道 TCP :8555（JSON：状态/命令/截图通知）
```

---

## 2. 当前状态（能用的 / 不能用的）

### ✅ 已实测跑通

| 能力 | 证据 |
|---|---|
| 设备端触屏 UI（分辨率/编码/码率/开始停止） | 界面截图 `docs/assets/ui_new.jpg` |
| RTSP 推流被电脑接收并显示 | 30.0 fps，`docs/assets/pc_window2.png` |
| 分辨率/编码/码率切换（320x240 ~ 1920x1080，H.264/H.265） | 设备状态事件回读 |
| USER 键 / 触屏 / 电脑空格 三种方式触发截图 | 设备日志 `原始像素已发送 #N` |
| **无损 RGB 截图**（不经 H.264） | 640x480 / 921600 字节 / 0.18 s 送达；`docs/assets/shot_compare.png` |
| 优雅退出（PC 远程 `quit`，不硬杀进程） | 设备日志 `退出中… / 已清理` + `bye` |
| 链路吞吐实测 | iperf3 双向 ≈ **170 Mbps**（USB2.0 gadget，非理论 480） |

### ⚠️ 已知限制（重要，别踩）

1. **截图分辨率固定 640×480**
   原因见 §5「VI 只有 2 路通道」。若推流也选 640×480（默认），截图即完整原生帧。
2. **1080p 只能 30fps**，拿不到 60（>1280×720 时 fps 参数失效）。
3. **只能在设备菜单里点开应用**，不能从 SSH 起进程抢屏幕（见 §5 第 2 条）。
4. 设备 Linux 侧**只有 128MB 内存**（规格书写 256MB），高分辨率+高码率要克制。

### ❌ 还没做

- 截图分辨率跟随推流分辨率（需要改显示通道尺寸，代价见 §5 末）
- 音频（`rtsp.Rtsp` 支持 `bind_audio_recorder`，未启用）
- 电脑端录制视频（现在只显示 + 截图）

---

## 3. 代码地图

```
maxicam/
├── apps/
│   ├── maixcam/main.py       ★ 设备端应用（触屏 UI + 推流 + 截图 + 控制通道）
│   ├── maixcam/app.yaml        装进设备应用菜单用的元数据
│   ├── pc/receiver.py        ★ 电脑端（RTSP 接收 + 窗口 + 无损截图落盘）
│   ├── pc/run.sh               电脑端启动脚本（自动带 venv PYTHONPATH）
│   ├── pc/send_cmd.py          给设备发命令（含急救用的 quit）
│   ├── pc/maixcam-receiver.desktop  电脑端桌面快捷方式
│   ├── deploy.py               部署/安装/停止设备端应用
│   └── README.md               完整使用说明 + 10 条实测坑
├── scripts/
│   ├── test_raw_shot.py        验证「原始像素通道」端到端（含视频帧对照）
│   ├── measure_link.py         用 socket 测链路吞吐
│   ├── probe_device.py         探测设备能力（字体/网络/按键）
│   └── xwd2png.py              X11 抓屏转 PNG（无 ImageMagick 时的取证工具）
├── docs/
│   ├── maixcam-to-zynq-video.md  ★ 为什么要这么传、带宽实测、方案对比
│   ├── reference/               官方 USB 模式切换脚本、AX7015B 手册正文
│   └── assets/                  界面/窗口/截图对比 三张图
├── ENVIRONMENT.md            ★ 开发环境配置清单（复现用）
└── HANDOVER.md               本文
```

### 设备端 `main.py` 关键结构

| 部分 | 作用 |
|---|---|
| `init_font()` | 注册中文字体（**默认字体画中文是方块**，必须 `image.load_font(name,path,size)` 后 `font=name`） |
| `Streamer.render_panel()` | 配置界面（分辨率/编码/码率/开始/退出），触摸命中区在 `_hits` |
| `Streamer.render_overlay()` | 推流中底部信息条 `[停止][截图][退出]` |
| `Streamer.handle_touch()` | 触摸分发；**底部信息条的判定要加 `H-OVERLAY_H` 偏移**（踩过坑） |
| `Streamer.start_stream()` | 建摄像头 + 显示通道 + RTSP；**强制距上次停止 ≥1.5s** |
| `Streamer.do_shot()` | 只登记请求；真正发送在主循环里做 |
| `Streamer._handle_pending_shot()` | 主循环中、**画 HUD 之前**把整帧 RGB 发走 |
| `ControlServer` | `:8555` 行分隔 JSON，双向（设备推状态/截图事件，PC 发命令） |
| `ShotServer` | `:8556` 二进制原始像素，帧头 `magic "MSHT" + seq/w/h/len` |
| `_setup_key()` | `key.rm_default_listener()` 摘掉"按 USER 键就退出"，改成长按退出 |
| `_setup_signals()` | 接管 SIGTERM/SIGINT → 优雅退出（**防止 MMF 资源泄漏**） |

---

## 4. 快速开始

### 4.1 设备端（一次性）

```bash
cd maxicam/apps
./deploy.py install        # 装进设备应用菜单（只做文件操作）
# 重启设备一次（launcher 只在启动时读 app.info），菜单里出现「视频推流」
```

之后每次：**板子菜单点「视频推流」→ 选参数 → 点「开始推流」**。

### 4.2 电脑端

```bash
cd maxicam/apps
./pc/run.sh --host 10.13.148.1
```

窗口里：**空格**=请设备抓原图、**q** 退出、**f** 全屏。
截图落在 `<工作区>/shots/maixcam_<时间>_no<序号>_<WxH>_RGB888.png`。

也可以点桌面/应用菜单里的「MaixCAM 视频接收」（`--host` 已写死在快捷方式里，换 IP 要改）。

### 4.3 常用运维

```bash
./deploy.py status     # 设备端在不在跑（只看 /proc/net/tcp 的 LISTEN 端口）
./deploy.py logs       # 设备端日志
./pc/send_cmd.py quit  # ★ 界面点不动时的救命招：让应用优雅退出
```

---

## 5. 必须知道的坑（都是踩出来的，按严重程度排）

### 5.1 绝不能硬杀设备端进程 ⚠️⚠️

应用握着 MMF（摄像头 VI / VPSS / 硬件编码器）。`kill -9` / 不经清理的 `pkill` 会导致：

- 之后任何 `camera.Camera()` 都报 `RuntimeError: camera open failed`（伴随 `vi get frame timeout 0xc006800e`）
- 严重时整个 MMF 驱动卡死：**板子 ping 得通、端口还在 LISTEN，但 sshd 连 SSH banner 都发不出来**
  —— 只能断电 / 按 RST 恢复（本项目已因此挂死过一次）

已做的防护：
- 应用接管 `SIGTERM/SIGINT` → `pkill -TERM` 会变成优雅退出（先 `stop_stream()` 释放资源）
- `deploy.py` 先通过控制通道发 `quit` 等它自己退，超时才发 SIGTERM，**绝不 `kill -9`**

### 5.2 设备开机后 launcher 占着屏幕，SSH 起进程会永久阻塞

此时 `display.Display()` **不是报错而是卡死**（不是网络/驱动问题）。
只有两种方式拿到屏幕：**从菜单点开应用**，或连 MaixVision（它会让 launcher 退出）。

⚠️ 也别去杀 launcher：这台设备上 `ps` / `killall` / `pidof` / 遍历 `/proc/*/cmdline` 都会**挂死 SSH 通道**
（`busybox ps` 反而是可用的）。判断进程只能读 `/proc/net/tcp` 的 LISTEN(`0A`) 端口。

**拉起应用只能从菜单点**：`app.switch_app()` 从 SSH 调用无效（launcher 只在应用退出后读
`/tmp/run_app.txt`，空闲在菜单时不轮询）。

### 5.3 停止后必须等 ≥1.5 秒才能再开始

Camera 关闭后 VI/VPSS 不是立刻释放，紧接着重开必然 `camera open failed`。
`start_stream()` 内部已强制等待，并且**不做激进重试循环** —— 因为 `camera.Camera()`
在异常状态下会长时间阻塞并持有 GIL，把整个应用（连控制通道）一起卡死。

### 5.4 摄像头只有 2 路 VI 通道（截图方案就是被它决定的）

MaixCDK `sophgo_middleware.c`：`#define MMF_VI_MAX_CHN 2`，而且 `Camera.add_channel()`
**不是子通道**，是又 new 一个独立 Camera 实例去抢通道：

```
主 NV21（bind 给 RTSP） = 第 1 路
add_channel(W,H) 显示   = 第 2 路
add_channel(w,h) 截图   = 第 3 路 ⇒ RuntimeError: mmf get vi channel failed
```

⇒ 截图只能**复用显示通道**（它本来就是 RGB888、不过编码器），并让**主循环单线程读**、
在画 HUD 之前发走（否则截图会带界面信息条）。

### 5.5 中文字体必须显式注册

MaixPy 默认 hershey 矢量字体**画中文是方块**：

```python
image.load_font("cjk0", "/maixapp/share/font/SourceHanSansCN-Regular.otf", 22)
img.draw_string(x, y, "分辨率", color, 1.1, font="cjk0")   # 注意是「字体名」不是路径
```

### 5.6 版面行高

中文字体实际行高比 `8*scale` 大不少，按后者排版会**文字重叠**。
实测行高：scale 1.0→24px、1.25→30px、1.5→36px。

### 5.7 RTSP 必须 NV21

设备端摄像头必须是 `image.Format.FMT_YVU420SP`，否则 RTSP 出不来流。

### 5.8 触摸命中区必须和绘制位置一致

底部信息条画在 `H-OVERLAY_H`，判定却写成 `6..50` 会出现"点了没反应"。
（已修，见 `handle_touch`，并且抽出了 `OVERLAY_H` 常量。）

---

## 6. 协议速查

| 端口 | 方向 | 格式 |
|---|---|---|
| 8554 | PC→设备（拉流） | RTSP，`rtsp://<设备IP>:8554/live` |
| 8555 | 双向 | 行分隔 JSON |
| 8556 | PC 连设备，设备推送 | 二进制：`"MSHT" + seq:u32 + w:u32 + h:u32 + len:u32` + RGB888 |

设备 → PC 事件：

```json
{"t":"state","running":true,"width":640,"height":480,"codec":"H.264",
 "bitrate":2000000,"urls":["rtsp://10.13.149.1:8554/live"],"shots":3}
{"t":"shot","seq":3,"width":640,"height":480,"codec":"H.264","bitrate":2000000,
 "raw":true,"img_width":640,"img_height":480}
{"t":"bye"}
```

PC → 设备命令：`shot` / `start` / `stop` / `quit` / `set {res,codec,bitrate}`。

> 注意：`set` 在**推流中不会自动重启**，只记参数并提示「停止后再开始」—— 这是故意的，
> 因为快速 stop→start 会触发 §5.3 的摄像头打不开。

---

## 7. 接手后建议的下一步

1. **跑一遍验证**：`./deploy.py install` → 重启 → 点应用 → `./pc/run.sh` → 按 USER 键 → 看 `shots/` 里有没有无损 PNG
2. **确认截图分辨率需求**：640×480 够不够看摩尔纹？不够就按 §5.4 末改成跟随推流分辨率
3. **接 Zynq**：见 `docs/maixcam-to-zynq-video.md`，方案 A（MaixCAM 的 USB 网卡直插 AX7015B 的 USB HOST）最省事，一根线
4. 采集端与算法端的**分辨率一致性**要提前对齐（摩尔纹频率和采集分辨率绑定，降采样会混叠）

---

## 8. 关键实测数字（方便回查）

| 指标 | 数值 |
|---|---|
| 链路吞吐（iperf3，USB2.0 gadget） | 上行 ~170 Mbps / 下行 ~179 Mbps |
| RTSP 解码帧率（640x480 H.264 2Mbps） | 30.0 fps |
| 原始像素单帧传输 | 640x480 = 921600 B，0.18 s |
| 无损 PNG vs H.264+JPEG q95 | 340 KB vs 78 KB（差 4.3 倍） |
| 设备可用内存 | 128 MB total，~80 MB available |
| 1080p30 H.264 码率需求 | 2~8 Mbps（170 Mbps 余量 20 倍以上） |
