# 演示录制操作单（板端 + 相机侧）

> 运行点：板 `192.168.7.2`（eth0）/ 相机侧 `10.13.148.100:9999`（usb1, cdc_ncm）｜
> 位流 `design_1_v58.bit` **已在 PL 里，绝不重载**｜`VIDEO_MODE=0` 720p60｜
> 帧缓冲双块 2,764,800 B｜1 帧/秒，绝对时刻网格。

---

## 0. 三十秒版

```bash
# ① 板上起播放器（常驻，等 START）
# ② 相机发一行 START
# ③ 录
# ④ 收工：SIGTERM（回彩条、释放缓冲）
```

---

## 1. 板端命令

```bash
ssh xilinx@192.168.7.2
cd /home/xilinx/demo

# 起（常驻；日志每帧一行；--loops 0 = 无限循环）
echo xilinx | sudo -S -p '' env XILINX_XRT=/usr \
  /usr/local/share/pynq-venv/bin/python3 -u demo_player.py \
  --pairs /home/xilinx/demo/pairs \
  --loops 0 --period 1.0 --start-delay 1.0 \
  --log /home/xilinx/demo/player.log \
  --pidfile /tmp/demo_player.pid
```

- **启动约 12 s**（Overlay 1.4 s + 心跳 0.2 s + 预载 0.44 s + 网络探测 ~3 s + bringup ~9 s）。
  期间屏幕是彩条；**端口 `9999` 打开 = 已经完全就绪**。
- 启动日志必看三行：
  - `camera should connect to 10.13.148.100:9999`
  - `ping 10.13.148.1 ×3 → rc=0`（相机在线）
  - `setup 完成：CTRL=0x01｜0x2C 基线=…｜未做任何软复位`
- **收工**（务必用这个，别 `pkill`）：
  ```bash
  echo xilinx | sudo -S -p '' kill -TERM $(cat /tmp/demo_player.pid)
  ```
  它会：`CTRL=0x15`（彩条）→ 释放帧缓冲 → 打印 `fpga0=operating` → `rc=0`。

**试片（不用相机）**：
```bash
# 定住第一对，便于对焦/构图
... demo_player.py --still 0 --log /home/xilinx/demo/player_still.log
# 自己开播 3 轮（24 帧 ≈ 24 s）
... demo_player.py --auto --start-delay 1.0 --loops 3 --once
```

## 2. 相机侧命令（MaixCAM）

板端地址 **`10.13.148.100`**，端口 **`9999`**（相机自己是 `10.13.148.1`）。

```python
# 在相机上跑：发 START，等板端确认
import socket
s = socket.create_connection(("10.13.148.100", 9999), timeout=3)
s.sendall(b"HELLO 1\n"); print(s.recv(64))     # b'ACK HELLO 1\n'
s.sendall(b"START\n");   print(s.recv(64))     # b'ACK START <n>\n'  ← 收到即"已开播"
s.settimeout(5)
try:    print(s.recv(64))                      # b'EVT FIRST\n'（第一帧真正上屏）
except socket.timeout: pass
s.close()                                      # 断连不影响播放
```

```python
# 停止（回彩条）
import socket
s = socket.create_connection(("10.13.148.100", 9999), timeout=3)
s.sendall(b"STOP\n"); print(s.recv(64))        # b'ACK STOP\n'
s.close()
```

- `PING` → `PONG <ms>`（保活；**10 s 无数据连接会被板端关闭**，重连即可，播放不停）
- 非法命令 → `ERR BADCMD <原文>`（**不会中断播放**）
- 只服务**最新一个连接**；新连接会把旧连接关掉

## 3. 拍摄建议

| 项 | 建议 |
|---|---|
| 显示器 | 1280×720@60（板端唯一可用模式；30 Hz 会全黑） |
| 手机机位 | 正对屏幕、三脚架、**画面四边贴齐取景框**（满屏无边框） |
| 曝光/白平衡 | 手动锁定（否则 1 fps 的明暗变化会让白纸忽亮忽暗） |
| 房间 | **关灯**，避免屏幕反光；用遮光布挡窗 |
| 手机长焦 | 用 2×/3× 减少**手机自己拍屏产生的摩尔纹**（别把手机的摩尔纹当成板子的效果） |
| 时长 | 每对 1.000 s；录 30 s 就能覆盖 3 轮全 8 对 |
| 起录 | 收到 `EVT FIRST` 后再等 1 s 起录（避开第一帧的 vsync 抖动） |

## 4. 故障快查表

| 现象 | 先做什么 | 说明 |
|---|---|---|
| **全黑** | 跑 `python3 minimal_proof.py 60` | 它是"能出图"的基线。**有图** ⇒ 播放器路径问题；**也黑** ⇒ PL/HDMI 输出级，别改代码，查 RTL |
| 黑但 `0x24` 正常 | `cat player.log \| grep nonblack` | `nonblack ≥ 80%` ⇒ 数据在 DDR 正确 ⇒ 问题在扫描/输出侧 |
| 彩条也没有 | 读 `CTRL` 是否 `0x15`；`0x30` 的 `mmcm_locked` | `mmcm_locked=0` 或像素时钟死 ⇒ 别碰 AXI |
| 相机连不上 | `ip -4 addr` 看 `usb1` 有没有 `10.13.148.100/24`；`ping 10.13.148.1` | 服务 `usb-cam-net.service` 管这个；**不要手动 `ip addr add`** |
| 端口被占 | 启动日志会打印 `端口被占用 …` 并退出 | 先 `ps -eo pid,args \| grep [d]emo_player` 杀干净 |
| 有僵尸播放器 | `bash /tmp/kp.sh`（按 pid 杀，含 `T` 状态） | **别用 `pkill -f`**：会匹配到自己的命令行 |
| FCLK0 不对 | 日志 `FCLK0 只读 … 不在 60~75` ⇒ 程序会退出 | 板端把 FCLK0 设成写 100（真值 66.67），再启动 |
| 帧节奏 | `grep '\[frame' player.log` | 判据：`max\|T-1.000\| ≤ 20 ms`；实测 0.4 ms |

## 5. 判据（录制前自检）

```
0x24 采纳率       = 100%       （日志/报告 latch_ok / (latch_ok+latch_bad)）
0x2C 增量         = 0          （相对启动基线的 uf 增量恒 0）
nonblack          ≥ 80%        （实测 88.8% = 640/720 内容区）
START→首帧        = 延迟 + <10 ms
帧间隔            = 1.000000 ± 0.001 s
收尾              = CTRL=0x15 / 缓冲释放 / operating / sync rc=0
```
