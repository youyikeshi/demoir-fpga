# 演示握手：PC 放一张图 → MaixCAM → 板子（v1）

> 协议权威：同事的 `10_演示握手协议_v1.md`（2026-11-03，TCP 9999 + `START`）。
> 本文只讲**相机侧怎么实现、怎么部署、怎么验**。
> 代码：`apps/maixcam/signal_service.py`、`apps/maixcam/board_notify.py`、
> `apps/pc/show_and_signal.py`、`apps/pc/board_relay.py`。

---

## 1. 链路

```
PC 轮播图片
  │  TCP 8555，一行 JSON（show idx=N）
  ▼
MaixCAM  signal_service.py
  │  TCP 9999，一行 ASCII（HELLO 1 / START / STOP / PING）
  ▼
AX7015B  板端演示服务：收到 START 后 1 s 一帧循环轮播对比图对
```

相机侧只做一件事：**把 PC 的 `show` 翻译成板端的 `START`**（协议里 START 幂等，
重复发不会重置进度）。`play stop` → `STOP`；`ping` → `PING`。

### 1.1 相机怎么够到板子（三种，按现场选）

| 方式 | 相机填的地址 | 前提 | 现状 |
|---|---|---|---|
| **A. 相机 USB 插板子** | `10.13.148.100:9999` | 板端 USB host 镜像（usb1 = 10.13.148.100） | 推荐，最终演示形态 |
| **B. 电脑转发**（相机 WiFi，板子只连电脑网口） | `<电脑 WiFi IP>:9999` | 电脑跑 `board_relay.py` | **现在就能用** |
| C. 相机直连板子网口 | `192.168.7.2:9999` | 相机和板子同网段 | 当前不成立（相机在 WiFi 上） |

方式 B 的拓扑：

```
相机 10.135.70.66 --WiFi--> PC 10.135.70.200:9999 --网口--> 板子 192.168.7.2:9999
                            （board_relay.py 原样转发，协议不变）
```

> 板子现在从这台电脑都 ping 不通（`192.168.7.2` 100% 丢包），说明网口/板子没就绪；
> 所以本次实测用的是 PC 上的**假板子**（见 §5）。

---

## 2. 协议（相机 → 板端，逐条对齐同事文档）

| 命令（整行） | 板端应答 | 相机什么时候发 |
|---|---|---|
| `HELLO 1` | `ACK HELLO 1` | 每条连接先发（可选，失败也继续） |
| `START` | `ACK START <n>` | 每次 `show`；`play start` |
| `STOP` | `ACK STOP` | `play stop` |
| `PING` | `PONG <ms>` | PC 的 `ping` |
| 其他 | `ERR BADCMD <原文>` | — |

- `\n` 结尾（`\r\n` 也接受）；大小写敏感。
- 板端 10 s 无数据会关连接 ⇒ 相机**每条事件一条短连接**，天然规避。
- 板端 `EVT FIRST`（首帧上屏）是可选上报；相机**不等它**（等了会拖慢轮播），
  只以 `ACK START` 作为"已开始播放"的判据（协议 §7 也是这么建议的）。

### 2.1 PC → 相机（8555）

```json
{"t":"cmd","cmd":"show","idx":3,"tag":"src_0003","file":"0003.png","ts":1699000000123}
{"t":"cmd","cmd":"play","state":"start"}
{"t":"cmd","cmd":"ping"}
```

### 2.2 相机 → PC（同一条连接回）

```json
{"t":"show_ack","idx":3,"tag":"src_0003","ok":true,
 "board":"10.13.148.100:9999","rtt_ms":24,"n":12,
 "reply":"ACK START 12","err":""}
```

`ok:false` 时 `err` 是 `ConnectionRefusedError: ...` / `no_ack` / `bad_reply: ERR BADCMD ...`。
**PC 不因此中断轮播**，只打印 FAIL。

---

## 3. 部署（WiFi 即可，不需要先插电脑）

```bash
cd maxicam/apps

# 方式 A（USB 直连板子）
./deploy.py signal --host 10.135.70.66 --board-host 10.13.148.100

# 方式 B（经电脑转发；先在电脑上起 relay）
./deploy.py signal --host 10.135.70.66 --board-host 10.135.70.200

./deploy.py signal-status --host 10.135.70.66
./deploy.py signal-logs   --host 10.135.70.66
./deploy.py signal-stop   --host 10.135.70.66
```

`deploy.py` 走 SSH（root/root），**WiFi 通就能传能跑**，不依赖 USB。
设备上落在 `/root/maxicam_signal/`，日志/pid 同目录。

> 为什么不用 `cam_shot.py` 顺带做这件事：`cam_shot.py` 要开 2K 相机、占 MMF、
> 要 11 MB 连续内存。真机实测它开相机时 MMF 报 `No buffer space available` 然后
> **SIGSEGV**，进程在 8555 起来之前就死了 —— 演示会直接黑掉。
> 所以信号单独做成 `signal_service.py`（不 import maix、不碰相机）。

---

## 4. 电脑上放图

```bash
pip install opencv-python numpy        # 只装一次

# 全屏轮播一个目录，每张 3 秒；上屏那一瞬间发信号
python3 apps/pc/show_and_signal.py --src ./src_pages --host 10.135.70.66 --interval 3

# 联调：只发一条
python3 apps/pc/send_show.py --host 10.135.70.66 --idx 1
python3 apps/pc/send_show.py --host 10.135.70.66 --ping
python3 apps/pc/send_show.py --host 10.135.70.66 --stop
```

"一瞬间"的保证：`imshow()` → `waitKey(1)`（真正上屏）→ 下一行才发 `show`。

方式 B 还要在电脑上起转发：

```bash
python3 apps/pc/board_relay.py --board-host 192.168.7.2 --board-port 9999
# 相机侧 --board-host 填这台电脑的 WiFi IP
```

---

## 5. 自测（不需要真相机/真板子）

```bash
python3 apps/pc/test_demo_handshake.py -v
# 23 项：HELLO/START/STOP/PING/ERR、START 幂等、掉线 ok:false、
#        恢复后自动接上、board_relay 转发、本地留痕
```

假板子（严格按协议 v1，含"只服务最新连接 / 断连不停播 / 10 s 空闲超时 / EVT FIRST"）：

```bash
python3 apps/pc/fake_board_9999.py --port 19999
printf 'START\n' | nc 127.0.0.1 19999
```

### 真机实测（2026-11-03，相机 `10.135.70.66`，PC 假板子 `10.135.70.200:19999`）

```
show  idx=1  -> ACK START 0   rtt=27ms
ping         -> PONG 5339     rtt=24ms
play stop    -> ACK STOP      rtt=22ms
```

相机侧日志（`deploy.py signal-logs`）：

```
[signal] 就绪：PC -> :8555 -> 板子 10.135.70.200:19999
[ctrl] 监听 0.0.0.0:8555（PC 显示信号入口）
[sig] show idx=1 tag='realtest' -> 10.135.70.200:19999 ACK START 0 rtt=27ms
```

> WiFi 省电会把延迟从 ~20 ms 拉到几百 ms，演示前建议关掉：
> `ssh root@<相机IP> 'iw dev wlan0 set power_save off'`（本次已关）。

---

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| `ok:false err=ConnectionRefused` | 板端服务没起/地址错：先 `nc -vz <板IP> 9999`；走 B 方案确认 relay 在跑 |
| `ok:true` 但板子没反应 | 看板端日志有没有收到 `START`；`n` 是否在涨（`ACK START <n>`） |
| 8555 起不来 | 被 `main.py`/`cam_shot.py` 占着；先停掉它们（它们各自也带中继） |
| PC 连 8555 超时 | `--host` 用了 `10.13.148.1`（USB 口）；改 WiFi IP |
| 延迟大/抖 | 关 WiFi 省电；换 5G；避开拥塞信道 |
| 相机开相机就崩 | 与信号服务无关；是 MMF/内存问题（先停 launcher、重启后再试 `cam_shot`） |

---

## 7. 改动清单

| 文件 | 作用 |
|---|---|
| `apps/maixcam/board_notify.py` | 握手 v1 客户端 + 8555 服务 + show/play/ping 映射 |
| `apps/maixcam/signal_service.py` | 新增。独立轻服务（不碰相机），演示用 |
| `apps/maixcam/cam_shot.py` | 基于设备上 1.1.0-preflight 版，加 `--ctrl-* / --board-*` 中继 |
| `apps/maixcam/main.py` | 8555 的 `_on_cmd` 也接同一套中继 |
| `apps/pc/show_and_signal.py` | 全屏轮播 + 逐张发信号 |
| `apps/pc/send_show.py` | 命令行发一条 show/ping/stop |
| `apps/pc/fake_board_9999.py` | 假板子（协议 v1 全行为） |
| `apps/pc/board_relay.py` | 跨网段转发（相机 WiFi → 电脑 → 板子网口） |
| `apps/pc/test_demo_handshake.py` | 23 项自测 |
| `apps/deploy.py` | 新增 `signal / signal-stop / signal-status / signal-logs` |

---

## 8. 电脑端放图程序（跟随板端节奏 + START）

`apps/pc/play_demo.py`：全屏放图，第一张上屏前先发 `{"t":"cmd","cmd":"play","state":"start"}`，
**结束不发 STOP**（板端 STOP 走 `0x15`，随后再 START 算新会话，容易黑屏；停板子用 SIGTERM）。

### 8.1 节奏：2026-11-04 起板端改成 **6.2 s/张**

同事在 restructure1 把 `demo_player.py` 的默认节奏定为 `--cadence real`：
`--start-delay 6.2 --period 6.2`（= v57 @75 MHz 真实算法吞吐，256² 墙钟 6.198~6.234 s）。
**电脑这边必须同步改成 6.2 s/张**，否则板子出到第 3 对时电脑已经翻到第 18 张。

`play_demo.py` 默认就按这个来（`--cadence real`），并且默认是 **mirror** 模式：
每 0.25 s 发一次幂等 START、读回板端 `<n>`，`n` 变了才换图、显示 `files[(n-1) % N]`。
所以板端无论是 6.2 s/张、首帧 13.5 s（`--first-period 13.5`）、跳帧还是回绕，电脑都自动跟上，
**不用手改 `--interval`**。

```bash
# 正式：全屏，跟随板端 6.2 s/张（默认参数即可）
python3 apps/pc/play_demo.py --src ../../../shots/uhdm100/src \
    --list ../../../shots/uhdm100/playlist.txt --host 10.135.70.66

# 1 fps 对焦/试片：板端也要 --cadence fast，电脑加同样的参数
python3 apps/pc/play_demo.py --src ... --cadence fast --host 10.135.70.66

# 板端没起 / 只想看电脑节奏
python3 apps/pc/play_demo.py --src ... --no-mirror --host 10.135.70.66
python3 apps/pc/play_demo.py --src ... --dry-run
python3 apps/pc/play_demo.py --src ... --no-gui --limit 5 --host 10.135.70.66
```

- **`<n>` 是已播帧计数**（协议 v1 §5）：`n=3` 时板子屏上是第 2 号 ⇒ 电脑索引 = `n-1`
  （`--n-offset 0` 可退回旧的 `n`）
- 每 0.25 s 一个 START 是**幂等**的：板端播放中收到只回同一个 `<n>`，不碰 CTRL、不重置进度
- `--log-csv sync.csv` 逐行记 `(时刻, n, 显示的图)`，录像核对同步用
- grid 模式（`--no-mirror`）才用绝对网格：第 i 张 = t0 + lead + i×interval，实测漂移 **<1 ms**
- `--loop 0` 无限循环（默认 1 = 跟完 100 张就退出）；`q` / `ESC` 退出，空格暂停

板端地址按 restructure1 演示支线定稿：**`10.13.148.100:9999`**（相机 `10.13.148.1`）。
