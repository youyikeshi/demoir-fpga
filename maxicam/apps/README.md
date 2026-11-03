# MaixCAM → 电脑 视频传输应用

两端一体的视频推流/接收方案：

```
┌──────────────── MaixCAM Pro (设备端, MaixPy) ────────────────┐
│  apps/maixcam/main.py                                        │
│   ├─ display + touchscreen   640x480 触屏 UI                  │
│   │     · 选分辨率 320x240 / 640x480 / 1280x720 / 1920x1080   │
│   │     · 选编码格式 H.264 / H.265                            │
│   │     · 选码率 1 / 2 / 4 / 8 Mbps                           │
│   │     · 开始 / 停止推流                                     │
│   ├─ camera(NV21) ──> rtsp.Rtsp  →  rtsp://<设备IP>:8554/live │
│   ├─ camera RGB888 通道(与流同分辨率，不经编码器)              │
│   │     └─> 截图原始像素通道 TCP :8556（二进制帧）             │
│   ├─ camera 显示通道 ──> 屏幕实时预览（带 HUD 条）             │
│   └─ 控制通道 TCP :8555（按行 JSON，双向）                     │
│         USER 键：短按 = 截图，长按 2 秒 = 退出                 │
└──────────────────────────────────────────────────────────────┘
                            │  USB 虚拟网卡 / 网口 / WiFi
                            ▼
┌──────────────────── 这台电脑 (Ubuntu) ────────────────────────┐
│  apps/pc/receiver.py                                         │
│   ├─ cv2.VideoCapture(rtsp)  自带 ffmpeg 解码 → 窗口显示      │
│   ├─ cv2.imshow + 左上角 HUD（帧率/分辨率/编码/截图数/双通道状态）│
│   └─ 收到原始像素 → 存**无损 PNG**（不经过 H.264 / JPEG）      │
│          <工作区>/shots/maixcam_<时间>_no<序号>_<WxH>_RGB888.png
└──────────────────────────────────────────────────────────────┘
```

## 截图为什么是「原始 RGB」而不是从视频里截

视频链路（RTSP）已经被**硬件 H.264/H.265 编码器压过一次**，再从解码帧里截图等于二次压缩
（H.264 + JPEG），高频细节会被抹掉 —— 拍屏幕时的**摩尔纹**就是这么糊掉的。

所以截图走了一条**完全独立的、不经编码器**的通路：

```
摄像 ISP ──┬─> NV21 ──> H.264 编码器 ──> RTSP :8554      （看画面用，有损）
           └─> RGB888 ────────────────> TCP :8556       （截图用，无损原图）
```

- 帧格式（大端）：`magic "MSHT" | seq:u32 | w:u32 | h:u32 | len:u32` + `len` 字节 RGB888
- 分辨率 = 当前选的分辨率（选 1080p 就抓 1080p），不是被缩放到屏幕尺寸
- 电脑落盘用 **PNG**（无损、可直接查看）；加 `--save-raw-rgb` 还会额外存一份原始 `.rgb` 字节

按下 USER 键时设备才抓一帧，**不占常驻带宽**。电脑上按**空格**会请设备去抓一张（不是截屏窗口）。
只有在原始像素通道不可用时，才会退回「保存解码帧」的旧行为（此时日志会明确写出来）。

---

## 目录

```
apps/
├── maixcam/
│   ├── main.py          # 设备端应用（触屏 UI + 推流 + 按键）
│   ├── app.yaml         # 想装进设备应用菜单时用
│   ├── cam_shot.py      # ★ 精简采集服务（TCP 8660，按协议要图；不推流、不显示）
│   ├── cam_core.py      # ★ 采集协议内核（不 import maix，PC 上可测）
│   └── README_cam_shot.md   # ★ 上面两个的说明书（部署/协议字段表/上机清单）
├── pc/
│   ├── receiver.py      # 电脑端：接收 + 窗口显示 + 截图落盘
│   ├── run.sh           # 启动脚本（自动带上 venv 的 PYTHONPATH）
│   ├── send_cmd.py      # 给设备发命令（也能用来远程触发截图）
│   ├── fake_camera.py   # ★ 假相机（§3 协议全路径，不需要硬件）
│   ├── test_cam_proto.py    # ★ 门禁判据 59 条（假相机 + 内核 + 裁剪硬判据）
│   └── cam_shot_probe.py    # ★ 连发要图并逐张核对（报实测 t_cap_us）
├── deploy.py            # 部署/启动/停止设备端应用（含 shot / shot-check / shot-status）
└── README.md            # 本文件
```

> **要给 Zynq 板子按协议要图（不推流）** ⇒ 看 `maixcam/README_cam_shot.md`；
> 本文件讲的是旧的那套 RTSP 推流 + 电脑端预览。两套不能同时跑（抢相机）。

---

## 快速开始（推荐：装成设备应用）

```bash
cd <工作区>/apps

# 1) 装进设备菜单（只做文件操作，不碰桌面）
./deploy.py install

# 2) 重启一次设备（按 RST 或拔插 Type-C）
#    —— launcher 只在启动时读 app.info，重启后菜单里才会出现「视频推流」

# 3) 在设备屏幕上点开「视频推流」
#    —— 它会自动开始推流，屏幕显示实时预览 + RTSP 地址

# 4) 电脑上开窗口看画面
./pc/run.sh --host 10.13.148.1
```

第 4 步默认会**等最多 60 秒**，所以"先开电脑端、再点设备应用"也行。

### 为什么必须从设备菜单打开

设备开机后 **launcher（桌面）会一直占着屏幕和摄像头**。
这时候从 SSH 另起一个进程去 `display.Display()` 会**永久阻塞**（不是报错，是卡住）。

只有两种方式能拿到屏幕：
1. **从 launcher 菜单点开应用** ← 推荐，也是官方应用的正常路径
2. 连上 MaixVision（它会让 launcher 退出）

⚠️ **不要用 `pkill` 杀 launcher**：这台设备上 `ps`/`killall`/`pidof` 本身就会卡死，
而且杀桌面会导致下次开机行为异常。用菜单打开才是正路。

窗口里：**空格**截图、**q/ESC** 退出、**f** 全屏切换。
按板子上的 **USER 键**同样会截一张，存到 `<工作区>/shots/`。

其它常用：

```bash
./deploy.py status      # 看应用/推流状态、设备 IP
./deploy.py logs        # 看设备端日志
./deploy.py stop        # 优雅停止设备端应用
./pc/send_cmd.py set --res 1280x720 --codec H.265 --bitrate 4000000
./pc/run.sh --no-window --max-frames 100     # 无界面自检
./pc/run.sh --host <ip> --out /tmp/shots --scale 1.5
```

> ⚠️ **推流中改分辨率/编码不会立即生效**，参数会记下来并提示你「停止后再开始」。
> 这是故意的，原因见下面「必须知道的坑」第 2 条。

---

## 设备端 UI

**设置界面**（未推流时）

```
MaixCAM 视频推流                     PC: 已连接
分辨率    [320x240] [640x480] [1280x720] [1920x1080]
编码格式  [H.264] [H.265]
码率      [1 Mbps] [2 Mbps] [4 Mbps] [8 Mbps]
┌──────────── 开始推流 ────────────┐
状态：已停止
选好参数后点「开始推流」
已截图：0 张（存到电脑工作区）
USER 键：短按截图 / 长按2秒退出
```

**推流中**：全屏实时预览，底部有一条 HUD：

```
[停止] [截图] [退出]   截图: 3     rtsp://10.13.149.1:8554/live
```

- 点 `[停止]` 回到配置界面，点 `[截图]` 同 USER 键短按，点 `[退出]` 直接退出应用
- 按 **USER 键长按 2 秒** 也能退出（短按是截图）

### 三种退出方式（都在，任选）

| 方式 | 位置 |
|---|---|
| 点 **[退出]** | 配置界面右上角 / 推流中底部信息条 |
| **长按 USER 键 2 秒** | 板子上的功能键（短按=截图） |
| **PC 端远程** | `./pc/send_cmd.py quit` ← 界面点不动时的救命手段 |

> 💡 如果应用像是卡住了、屏幕点不动，**在电脑上执行 `./pc/send_cmd.py quit`**，
> 它会通过控制通道让应用自己优雅退出（不会硬杀进程、不会弄脏 MMF 驱动）。
> 只要 `8555` 端口还在监听这一招就有效。

---

## 电脑端参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `10.13.148.1` | MaixCAM 的 IP（USB 网卡一般是 `10.13.148.1` / `10.13.149.1`） |
| `--rtsp-port` | 8554 | RTSP 端口 |
| `--ctrl-port` | 8555 | 控制通道端口 |
| `--url` | — | 直接给完整 RTSP URL，覆盖上面两个 |
| `--out` | `<工作区>/shots` | 截图保存目录 |
| `--no-window` | 关 | 不显示窗口（自检/无人值守） |
| `--max-frames` | 0 | 读满 N 帧后退出 |
| `--dump-display` | — | 把「窗口里显示的画面（含 HUD）」存一份，用于取证 |
| `--save-frame` | — | 退出前把最后一帧存到指定路径 |
| `--scale` | 1.0 | 窗口缩放 |
| `--no-ctrl` | 关 | 只收视频，不连控制通道（不显示 HUD 里的设备信息，也不能远程触发截图） |
| `--shot-port` | 8556 | 原始像素通道端口（截图走这里，无损 RGB888） |
| `--no-raw` | 关 | 不连原始像素通道；截图退回「保存解码帧」（有损 JPEG） |
| `--save-raw-rgb` | 关 | 除无损 PNG 外，再额外存一份原始 `.rgb` 字节文件（= w×h×3 字节） |

---

## 控制通道协议

设备端在 `:8555` 监听（文本 JSON），原始像素在 `:8556`（二进制，见上文）。

**设备 → PC**（按行 JSON）

```json
{"t":"state","running":true,"width":640,"height":480,"codec":"H.264",
 "bitrate":2000000,"urls":["rtsp://10.13.149.1:8554/live"],"shots":3}
{"t":"shot","seq":3,"ts":123456,"width":640,"height":480,"codec":"H.264",
 "bitrate":2000000,"raw":true,"img_width":640,"img_height":480}
{"t":"bye"}
```

`shot.raw=true` 表示**原始像素已经从 :8556 发出**（电脑存无损 PNG）；
`raw=false` 时电脑才退回保存解码帧。

**原始像素帧（:8556，二进制，大端）**

```
magic "MSHT" (4B) | seq u32 | width u32 | height u32 | len u32 |  RGB888 像素 len 字节
```

**PC → 设备**

```json
{"t":"cmd","cmd":"shot"}
{"t":"cmd","cmd":"start"}
{"t":"cmd","cmd":"stop"}
{"t":"cmd","cmd":"quit"}
{"t":"cmd","cmd":"set","res":"1280x720","codec":"H.265","bitrate":4000000}
```

用 `./pc/send_cmd.py` 就能发，也可以用 `nc`。

---

## 必须知道的坑（都是实测踩出来的）

### 1. 绝对不要硬杀设备端应用 ⚠️ 最重要

应用握着 **MMF 资源**（摄像头 VI / VPSS / 硬件编码器）。用 `kill -9` 或不经清理地 `pkill`
会把这些资源留在占用状态，之后：

- 任何 `camera.Camera()` 都报 `RuntimeError: camera open failed`（伴随 `vi get frame timeout 0xc006800e`）
- 严重时整个 MMF 驱动卡死，表现为**板子 ping 得通、端口还在 LISTEN，
  但 sshd 连 SSH banner 都发不出来** —— 只能给设备断电或按 RST 恢复

所以：
- 应用自己接管了 `SIGTERM/SIGINT`，`pkill -TERM` 会变成一次**优雅退出**（先释放资源）
- `deploy.py` 会**先通过控制通道发 `quit`** 等它自己退，超时才发 SIGTERM，绝不 `kill -9`
- 要停止请用 `./deploy.py stop`，别手工 `pkill -9`

### 2. 停止到再次开始必须间隔 ≥ 1.5 秒

Camera 关闭后 VI/VPSS **不是立刻释放**的。紧接着重开必然 `camera open failed`。
所以 `start_stream()` 内部会强制等待，并且**不做激进重试循环** ——
因为 `camera.Camera()` 在异常状态下会长时间阻塞并持有 GIL，把整个应用
（连控制通道）一起卡死。失败就提示用户再点一次，比循环重试安全得多。

推流中改参数也因此**不会自动重启**，需要你显式「停止 → 开始」。

### 3. 这台设备上这些命令会卡死

`ps aux`、`netstat`、`pgrep`、`pidof`、遍历 `/proc/*/cmdline` ——
都会挂住整个 SSH 通道。判断进程只能读 `/proc/net/tcp` 的 LISTEN(`0A`) 端口。
`deploy.py` 已经全部避开了这些命令。

### 4. 中文字体必须显式注册

MaixPy 默认字体是 hershey 矢量字体，**画中文会变成方块**。
而且 `draw_string(font=...)` 要的是**注册过的字体名**，不是路径：

```python
image.load_font("cjk0", "/maixapp/share/font/SourceHanSansCN-Regular.otf", 22)
img.draw_string(x, y, "分辨率", color, 1.1, font="cjk0")
```

`main.py` 的 `init_font()` 已处理，找不到中文字体时会明确告警。

### 5. 版面行高

中文字体实际行高比 `8 * scale` 大不少，按后者排版会**文字重叠**。
`main.py` 里用的是实测行高（scale 1.0 → 24px，1.25 → 30px，1.5 → 36px）。

### 6. RTSP 必须 NV21

设备端摄像头必须是 `image.Format.FMT_YVU420SP`，否则 RTSP 出不来流。

### 7. 设备内存只有 128MB

`free -m` 实测 Linux 侧只有 128MB（规格书写 256MB）。高分辨率 + 高码率容易紧张，
建议默认 `640x480`；上 1080p/2K 时注意。

### 8. 分辨率与帧率绑定

- 分辨率 > 1280×720 时 `fps` 参数失效，**强制锁 30fps**（所以拿不到 1080p60）
- GC4653 的官方帧率表里没有 1080p 档，1080p 是 ISP 缩放出来的

### 9. 带宽不是瓶颈

实测 USB2.0 链路 iperf3 双向约 **170 Mbps**（不是理论 480）。
但 1080p30 H.264 只要 2~8 Mbps，余量 20 倍以上，所以分辨率/编码随便选。
详见 `docs/maixcam-to-zynq-video.md` §4.1。

### 10. 摄像头 VI 只有 2 路通道 ⚠️（截图方案就是被这个限制决定的）

MaixCDK `components/3rd_party/sophgo-middleware/.../sophgo_middleware.c`：

```c
#define MMF_VI_MAX_CHN 2        // manually limit the max channel number of vi
```

而且 **`Camera.add_channel()` 不是"主通道的子通道"**，它是**又 new 一个独立的
Camera 实例**去抢 VI 通道（见 `maix_camera_mmf.cpp` 的 `Camera::add_channel`）。
所以：

```
主 NV21 (bind 给 RTSP)          = 第 1 路
add_channel(W,H) 显示通道       = 第 2 路
add_channel(w,h) 截图通道       = 第 3 路  ⇒  RuntimeError: mmf get vi channel failed
```

结论：**截图只能复用显示通道那一帧**（它本来就是 RGB888、不经过编码器，天然无损）。
为了让"只有一个线程读通道"，截图请求由主循环在 `chan.read()` 之后、**画 HUD 之前**
统一处理 —— 这样存下来的原图也不会带上界面信息条。

代价：截图分辨率 = 显示通道尺寸 = **640×480**。若哪天要更高分辨率的截图，
得把这一路通道按目标分辨率建（`disp.show` 会缩放显示），代价是主循环每帧多搬内存、
预览可能掉帧。

---

## 故障排查

| 现象 | 处理 |
|---|---|
| PC 报 `无法打开 RTSP 流` | ① 设备上点了「开始推流」吗（`./deploy.py status` 看 8554）② `--host` 对不对（`ip -4 addr` 看 `enx*` 网卡）③ 能 ping 通吗 |
| **屏幕点了没反应 / 退不出去** | 在电脑上执行 `./pc/send_cmd.py quit`，让应用优雅退出。若已完全无响应，再按 RST |
| 窗口打不开 | `--no-window` 先确认能收流；检查 `DISPLAY`。opencv-python 自带 Qt5 后端，`libqxcb.so` 依赖已确认齐全 |
| 设备屏幕没显示 UI | 应用没起来（看 `./deploy.py logs`），或 launcher 占着屏幕 |
| 板子 ping 通但 SSH 不上 | 大概率是 MMF 卡死（见坑 1），**按 RST 重启设备** |
| 截图没存下来 | PC 要能连上控制通道（HUD 里 `ctrl=OK`）；`--no-ctrl` 时不行 |
| 设备端起来但立刻退出 | 看日志；应用接管了 SIGTERM，正常退出会打印「退出中…」「已清理」 |

### 急救速查

```bash
./deploy.py status            # 设备端到底在不在跑（看 8555/8554 端口）
./pc/send_cmd.py quit         # 让设备端应用优雅退出（界面点不动时的救命招）
./deploy.py stop              # 同上，但走 deploy 的优雅停止流程
# 以上都不行 -> 按板子 RST 键重启
```

---

## 依赖

电脑端依赖已装在 `<工作区>/env/receiver/lib/python3.10/site-packages`：

```bash
python3 -m venv --without-pip <工作区>/env/receiver
python3 -m pip install --target <工作区>/env/receiver/lib/python3.10/site-packages \
        opencv-python numpy -i https://pypi.tuna.tsinghua.edu.cn/simple
```

设备端不需要额外依赖（MaixPy 自带 `rtsp` / `display` / `touchscreen` / `key` / `camera`）。
