# 演示全流程：电脑放图 → MaixCAM 触发 → 板子轮播

> 适用范围：演示视频里「电脑屏幕按 1 秒 1 张翻页，相机对着屏幕拍，板子同步轮播左右对比图对」。
> 本文把**电脑、相机、板子**三端的准备、启动、自检、收工、排障写成可照抄的步骤。
>
> 关键前提（先读）：**相机只负责发触发信号，不把照片传给板子**。板子播的是提前生成好的
> `pairs/*.bin` 对比图对（v57 处理一帧约 6 s，做不到实时）。所以“相机拍照”在流程里的角色是
> **被拍摄对象**，不是板子的数据源。

---

## 1. 拓扑与地址

```
┌──────────────┐   WiFi（同一热点/路由器）   ┌──────────────────┐
│  电脑         │  8555 JSON（开始/停止）     │  MaixCAM Pro      │
│ play_demo.py │ ─────────────────────────> │  wlan0 192.168.x  │
│ 全屏 1 秒 1 张│                            │  usb1 10.13.148.1 │
└──────────────┘                            └────────┬─────────┘
                                                     │ USB-C（NCM 网卡 + 供电）
                                                     │ TCP 9999 文本行（START/STOP/PING）
                                            ┌────────▼─────────┐
                                            │  AX7015B          │
                                            │  usb1 10.13.148.100│
                                            │  demo_player.py   │
                                            │  HDMI 轮播图对     │
                                            └──────────────────┘
```

| 端 | 地址 | 用途 |
|---|---|---|
| 电脑 ↔ 相机 | 相机 `wlan0`（热点分配，如 `10.135.70.66`）: **8555** | PC 发 `play start/stop` |
| 相机 ↔ 板子 | 板子 **`10.13.148.100:9999`**；相机自己 `10.13.148.1` | 相机发 `START/STOP/PING` |
| 电脑 ↔ 板子 | 板子 `192.168.7.2`（eth0，仅调试/烧录用） | SSH、看播放器日志 |

> 电脑和板子**不需要**互相可达；两条链路是分开的。

---

## 2. 准备清单

### 2.1 硬件

- MaixCAM Pro + Type-C 线（插板子的 USB-A 口，一根线同时供电和组网）
- AX7015B + 已烧好 v58 位流/镜像的 SD 卡 + HDMI 显示器
- 电脑（跑播放器）+ 一个手机热点或路由器（电脑和相机都要连）
- 可选：带供电的 USB Hub（相机 WiFi 峰值电流大时防欠压重启）

### 2.2 软件/文件

| 位置 | 需要的东西 | 谁准备 |
|---|---|---|
| 板子 | `/home/xilinx/demo/demo_player.py`、`pairs/*.bin` | 板子同事（restructure1 演示支线） |
| 相机 | `signal_service.py` + `board_notify.py`（本仓库 `apps/maixcam/`） | 本仓库部署 |
| 电脑 | `apps/pc/play_demo.py`、100 张全屏源图 | 本仓库 |
| 电脑 | Python + OpenCV（`opencv-python`，或工作区 `.pylibs`） | 一次性 |

---

## 3. 一次性准备（只做一次）

### 3.1 板子侧（同事负责，这里只列验收点）

```bash
ssh xilinx@192.168.7.2
cd /home/xilinx/demo
# 启动播放器（常驻等 START；--loops 0 = 无限）
echo xilinx | sudo -S -p '' env XILINX_XRT=/usr \
  /usr/local/share/pynq-venv/bin/python3 -u demo_player.py \
  --pairs /home/xilinx/demo/pairs --loops 0 --period 1.0 --start-delay 1.0 \
  --log /home/xilinx/demo/player.log --pidfile /tmp/demo_player.pid
```

启动约 **12 s**。就绪判据：日志出现 `camera should connect to 10.13.148.100:9999`，
且 **9999 端口打开**（`ss -ltn | grep 9999` 或从相机侧 `nc`）。

> 板端 `--period 1.0 --start-delay 1.0` 是本文所有时序假设的来源，改参数要同步改电脑侧 `--lead`。

### 3.2 图对（决定“电脑第 N 张”和“板子第 N 对”是不是同一张）

板子播的是预置图对，不是现场拍的。要保证内容一一对应，就用**同一批图**生成：

```bash
# 在板子同事的仓库里（restructure1 演示支线）
python3 tools/prepare_demo_pairs.py --mode external --src <同一批图目录> --out runs/demo_pairs/pairs
# 或按现成帧
python3 tools/prepare_demo_pairs.py --mode frames ...
```

生成后按他们的流程拷到板子 `/home/xilinx/demo/pairs`。

> 如果电脑放 100 张、板子只有 8 对，那只能保证“节奏都是 1 秒”，**内容对不上**。

### 3.3 相机侧部署（一次性，之后每次开机要启动）

```bash
cd demoir-fpga/maxicam/apps
# 只发信号、不碰相机（推荐演示用；最稳）
./deploy.py signal --host <相机 WiFi IP> --board-host 10.13.148.100

# 需要相机同时出 256 图时，才用这个（会开相机、吃内存）
./deploy.py shot --host <相机 WiFi IP> --board-host 10.13.148.100
```

部署内容：`/root/maxicam_signal/signal_service.py`、`board_notify.py`。
开机**不会自启**，每次重启后要再执行一次（或自己加进启动脚本）。

### 3.4 电脑侧

```bash
pip install opencv-python          # 只需一次
# 或者用工作区里的 .pylibs（play_demo.py 会自动加进去）
```

---

## 4. 每次演示的启动顺序（重要：板子 → 相机 → 电脑）

### 4.1 三十秒版

```bash
# ① 板子：起播放器，等 9999 就绪（约 12 s）
# ② 相机 USB 插板子，确认 10.13.148.100 通
# ③ 相机：起 signal_service（WiFi SSH）
# ④ 电脑：play_demo.py 正式放
```

### 4.2 详细步骤

**第 1 步：板子上电、起播放器**

按 §3.1 的命令起，等日志/端口就绪。

**第 2 步：相机 USB 插板子**

插上后等 5~10 s，在板子上确认：

```bash
ip -br addr show usb1        # 期望 10.13.148.100/24 UP
```

在相机上确认（WiFi SSH）：

```bash
ssh root@<相机 WiFi IP>
ip -br addr show usb1        # 期望 10.13.148.1/24 UP
ping -c2 10.13.148.100       # 期望 0% 丢包，RTT ~2 ms
python3 -c "import socket;s=socket.create_connection(('10.13.148.100',9999),3);s.sendall(b'PING\n');print(s.recv(64))"
# 期望 b'PONG <ms>\n'
```

**第 3 步：起相机侧服务**

```bash
cd demoir-fpga/maxicam/apps
./deploy.py signal --host <相机 WiFi IP> --board-host 10.13.148.100
./deploy.py signal-status --host <相机 WiFi IP>
# 期望：运行中 (8555)
```

启动日志应包含：

```
[signal] 就绪：PC -> :8555 -> 板子 10.13.148.100:9999
[ctrl] 监听 0.0.0.0:8555（PC 显示信号入口）
```

**第 4 步：电脑播放**

```bash
cd demoir-fpga/maxicam
# 先空跑确认节奏（不发信号）
python3 apps/pc/play_demo.py --src /home/zheyang/qiansaiFPGA/shots/uhdm100/src --dry-run --limit 5

# 正式
python3 apps/pc/play_demo.py --src /home/zheyang/qiansaiFPGA/shots/uhdm100/src \
    --host <相机 WiFi IP> --loop 0
```

---

## 5. 联调自检（逐条过，别跳）

| # | 在哪 | 命令/动作 | 期望 |
|---|---|---|---|
| 1 | 板子 | `ss -ltn \| grep 9999` | 有监听 |
| 2 | 相机 | `ping -c2 10.13.148.100` | 0% 丢包 |
| 3 | 相机 | 上面的 `PING` 一行 | `PONG <ms>` |
| 4 | 电脑 | `nc -vz <相机IP> 8555` | succeeded |
| 5 | 电脑 | `./apps/pc/send_show.py --host <相机IP> --start` | `play_ack`，`ok:true`（板子在线时） |
| 6 | 板子 | `tail -f player.log` | 收到 START 后 1 s 开始出图 |
| 7 | 相机 | `./deploy.py signal-logs --host <IP>` | `[sig] play -> 10.13.148.100:9999 ACK START <n>` |

第 5 步若 `ok:false` 且 `err` 是 `TimeoutError`/`ConnectionRefused`：相机到板子的 9999 不通，
先解决链路，不要继续放图。

---

## 6. 正式播放与录制

```bash
python3 apps/pc/play_demo.py --src <图片目录> --host <相机IP> --loop 0 --lead 1.0
```

- 程序会在第一张**上屏前**发 `START`，等 `--lead` 秒后按 1 秒网格开始翻页；
- 每张打印 `drift`（对目标时刻的偏差，正常 < 几 ms）；
- 回包异步打印 `[ack] {"t":"play_ack","ok":true,...}`；
- `q` / `ESC` 退出（会发 `STOP`），空格暂停。

---

## 7. 时序模型（为什么“间隔稳定、绝对有偏”）

两边各自按**绝对时刻网格**跑：

```
电脑第 i 张：  t_pc(i)  = T0 + lead + i × 1.000 s
板子第 i 对：  t_brd(i) = T_start + 1.0 s + i × 1.000 s     （收到 START 后延迟 1 s）
```

- **间隔稳定**：两边都是 `i×1.000 s`，不是 sleep 累加。实测电脑 100 张累计漂移 **+0.7 ms 级**；
  板端实测 40 s 累计 15 µs。
- **绝对偏差** = `(T0+lead) - (T_start+1.0)` + 网络/调度抖动。`lead=1.0` 与 `start-delay=1.0`
  对消后，只剩 WiFi 往返（20~50 ms）+ 板端调度 + HDMI 刷新相位，**几十毫秒级、不随张数累积**。
- 两边晶振有 ppm 级差异，100 s 内几毫秒，演示可忽略；要帧锁得做 genlock，这套没有。

> 想微调“电脑先/板子先”，只改电脑 `--lead`：`1.05` = 电脑晚 50 ms，`0.95` = 电脑早 50 ms。

---

## 8. 收工（务必按顺序）

```bash
# ① 电脑：Ctrl-C / q（会发 STOP）
# ② 相机：停服务（只杀 pid，不 pkill）
./deploy.py signal-stop --host <相机IP>

# ③ 板子：SIGTERM，让播放器回彩条、释放帧缓冲
ssh xilinx@192.168.7.2 'echo xilinx | sudo -S -p "" kill -TERM $(cat /tmp/demo_player.pid)'
```

相机侧**不要 `kill -9`**（会弄脏 MMF）；板子侧**不要 `pkill`**（会杀到自己的 shell）。

---

## 9. 故障排查

| 现象 | 最可能原因 | 处理 |
|---|---|---|
| 电脑 `nc 8555` 不通 | 相机服务没起 / IP 变化 | `./deploy.py signal-status`；重新 `signal` |
| `play_ack ok:false err=Timeout` | 相机到板子 9999 不通 | 查两边 `usb1` IP、USB 线、板端 9999 |
| 板子收到 START 后不动 | 位流/播放器异常 | 看 `player.log`；`--still 0` 定住试片 |
| 板子出图但和电脑不同步 | 图对不是同一批 / lead 不对 | 重新生成 pairs；微调 `--lead` |
| 板子播放几轮后停 | `--loops` 不是 0 | 改 `--loops 0` |
| 相机 WiFi 掉线 | USB 供电不足 | 换带供电 Hub / PMOD 补 5V |
| 相机开相机就崩（MMF SIGSEGV） | 128 MB 内存 + 2K 连续内存 | 演示用 `signal_service`（不开相机）；要采集先重启设备、停 launcher |
| `deploy.py` 连不上相机 | 相机没连 WiFi | 看屏幕设置里的 IP；换 2.4G 热点 |

---

## 10. 已知限制（提前和评审说清楚）

1. **相机不参与画面传输**：只发 START；板子播预置图对。
2. **不是硬同步**：两边自由跑 1 Hz，偏差几十毫秒、不累积；没有 genlock。
3. **板端启动 12 s**：启动期间是彩条，9999 开了才算就绪。
4. **相机服务不自启**：每次开机要手动起（或自己加 init 脚本）。
5. **2K 全幅回传慢**：11 MB/张，约 2.7 s；256 只要 1.2 s。演示链路不传图，不受影响。

---

## 11. 命令速查

```bash
# 电脑
python3 apps/pc/play_demo.py --src <DIR> --host <CAM_IP> --loop 0
python3 apps/pc/play_demo.py --src <DIR> --dry-run --limit 5
python3 apps/pc/send_show.py --host <CAM_IP> --start|--stop|--ping

# 相机
cd demoir-fpga/maxicam/apps
./deploy.py signal --host <CAM_IP> --board-host 10.13.148.100
./deploy.py signal-status|signal-logs|signal-stop --host <CAM_IP>

# 板子
ssh xilinx@192.168.7.2
ss -ltn | grep 9999
tail -f /home/xilinx/demo/player.log

# 协议速查
PC  -> 相机 8555 : {"t":"cmd","cmd":"play","state":"start"|"stop"}
相机 -> 板子 9999: HELLO 1 / START / STOP / PING  （一行一个，\n 结尾）
板子 -> 相机     : ACK HELLO 1 / ACK START <n> / ACK STOP / PONG <ms> / EVT FIRST
```
