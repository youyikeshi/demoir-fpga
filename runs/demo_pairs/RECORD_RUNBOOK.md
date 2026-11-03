# 演示录制操作单（板端 + 相机侧）

> 运行点：板 `192.168.7.2`（eth0）/ 相机侧 `10.13.148.100:9999`（usb1, cdc_ncm）｜
> 位流 `design_1_v58.bit` **已在 PL 里，绝不重载**｜`VIDEO_MODE=0` 720p60｜
> 帧缓冲双块 2,764,800 B｜1 帧/秒，绝对时刻网格。

---

## 0.0 ★ 上电/重启后**必须**先做这两件事（顺序固定）

```sh
# ① 位流在不在？——不是 operating 就 sysfs 喂 **.bin**（不是 .bit！）
cat /sys/class/fpga_manager/fpga0/state
#   unknown ⇒
#     sudo cp -f /home/xilinx/demo/design_1_v58.bit /lib/firmware/      # 备着
#     echo design_1_v58.bin | sudo tee /sys/class/fpga_manager/fpga0/firmware
#     cat /sys/class/fpga_manager/fpga0/state      # 必须 operating
#   ⚠️ .bit 直接喂会被拒：Invalid bitstream, could not find a sync word
#      （内核要 byte-swapped .bin；板上 /lib/firmware/design_1_v58.bin 已有）
#   ⚠️ state != operating 时**绝不**碰 0x400A0000（AXI 挂死，只能断电）

# ② 真值 66.67 MHz 对不对？——判别法：0x2C 在 2 s 内涨 ⇒ 频率错
python3 -c "
from pynq import Overlay, Clocks
Overlay('/home/xilinx/demo/design_1_v58.bit', download=False)
print('fclk0 报数 =', float(Clocks.fclk0_mhz))    # 必须是 100.0 ⇒ 真值 66.67
"
#   不是 100 ⇒ Clocks.fclk0_mhz = 100  （只写这一次；播放器里只读校验）
#   实测：报数 75 ⇒ 真值 50 MHz ⇒ 每行都读空 ⇒ 0x2C 疯涨（一次涨到 1,546,533）
#         报数 100 ⇒ SLCR 0x170=0x00100F00, DIV0+1=16 ⇒ 真值 66.67 MHz, 0x2C 增量 0
```

**这两条查完才允许跑 `minimal_proof.py` / 播放器。**
`minimal_proof.ensure_pl()` 已经把①做成了硬门禁（state≠operating 自动 sysfs 载 + 再校验），
但**②必须人工确认**（播放器只读校验，不对就 `SystemExit`）。

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

## 1.5 三种跑法（复制粘贴）

```bash
cd /home/xilinx/demo
SUDO="echo xilinx | sudo -S -p ''"
PYB=/usr/local/share/pynq-venv/bin/python3

# ① 对焦/构图：定住第一对，不切换
$SUDO env XILINX_XRT=/usr $PYB -u demo_player.py --pairs /home/xilinx/demo/pairs \
      --still 0 --log /home/xilinx/demo/player_still.log

# ② 试片（不用相机）：自己开播 3 轮 = 24 帧
$SUDO env XILINX_XRT=/usr $PYB -u demo_player.py --pairs /home/xilinx/demo/pairs \
      --auto --start-delay 20 --loops 3 --once --log /home/xilinx/demo/player_trial.log

# ③ 正式：常驻等相机 START，无限循环
$SUDO env XILINX_XRT=/usr $PYB -u demo_player.py --pairs /home/xilinx/demo/pairs \
      --loops 0 --period 1.0 --start-delay 1.0 \
      --log /home/xilinx/demo/player.log --pidfile /tmp/demo_player.pid
```

> 三种跑法**只差命令行参数**，代码一条不改。

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


## 2.5 ★ 不依赖相机的触发方式（今晚的备用主线）

相机那条 USB-NCM 链路不稳时（板子反复重启、相机枚举失败），**演示仍然可以照常录**：
播放器的 TCP 触发不挑来源，从主机走 `eth0` 发一行 `START` 同样有效。镜头里拍"板子 + 显示器"即可。

```bash
# 主机侧（不需要板子有相机）
printf 'START\n' | nc -q1 192.168.7.2 9999        # 最简
python3 tools/host_trigger.py start                # 推荐：会打印 ACK START 与 EVT FIRST
python3 tools/host_trigger.py ping                 # 保活（10 s 无数据连接会被板端关闭）
python3 tools/host_trigger.py stop                 # 回彩条
```

`host_trigger.py start` 的输出应该是：
```
  >>> HELLO 1    <<< ACK HELLO 1             0.8 ms
  >>> START      <<< ACK START 1             0.9 ms
  <<< EVT     EVT FIRST                      ← 第一帧真正上屏
```

> 断开连接**不会**停止播放（协议规则 2）；只有 `STOP` / `SIGTERM` 才回彩条（规则 3）。

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


## 6. 禁令（今晚付过学费的三条）

1. **不要写 `CTRL[1]` 软复位**（`CTRL=0x03` 保持 ~20 ms）
   ⇒ 会打到 `hdmi_fb_scan.cfg_soft_reset`，之后扫描 FSM 不再吐 `pix_valid`
   ⇒ **DE 有效但像素全 0 = 全黑**，而 `0x2C` 仍为 0（握手根本不发）。
   今晚为这一个动作排查了 4 轮。
2. **正确写法：空闲/停播 = `CTRL=0x15`（彩条），每次从彩条切扫描都必须重新写 `CTRL=0x01`**
   - `cfg_enable = ctrl_enable && !ctrl_test` ⇒ 写了 `0x15` 扫描就停（这是**想要**的待命态）
   - **实测背书（2026-11-03 晚）**：`STOP → 0x15 → START → 0x01` 连续成功 3 次（play#1/#2/#3），
     空闲态显示彩条、收到 START 后 1000 ms 出图
   - 所以"不能写 0x15"是**早期误判**（当时的真因是 `CTRL[1]` 软复位 + 陈旧 pickle），已撤销
   - **真正的禁令**：① 播放中不许写 CTRL；② 任何情况下不许写 `CTRL[1]`（软复位）
3. **不要改 `demo_player` 的寄存器调用**
   ⇒ 一切走 `minimal_proof.bringup() / show()`。
   今晚"照着最小脚本重写"的 4 个版本全黑，而打桩 diff 显示**寄存器写序列逐条相同** ——
   差异在进程环境/时序里，肉眼看不出来。等价性只能靠"同一份代码"。

### 收工信号怎么发
```bash
# ✅ 正确
echo xilinx | sudo -S -p '' kill -TERM $(cat /tmp/demo_player.pid)
# ❌ 禁止：kill -9 打到 sudo（今晚踩过，python 子进程变僵尸还占 CMA）
# ❌ 禁止：pkill -f demo_player（会匹配到自己的命令行）
```

## 7. ⛔ 绝对禁令：PL 没载位流时，**不许碰 `0x400A0000`**

**实测（2026-11-03 22:36 与 22:44，两次完全同样）**：
`/sys/class/fpga_manager/fpga0/state` 是 `unknown`（PL 空）时，
用 `/dev/mem` mmap 读 `0x400A0000`（HDMI 寄存器）⇒ **板子当场失联**（ping 100% 丢包、ARP INCOMPLETE），
两次都是"上一条命令正常、读到这个地址就没返回值"。

**原因**：PL 里没有那个 AXI 从设备，CPU 去读一个不存在的 AXI 地址 ⇒ 总线事务挂死。
**这不是软件 bug，是硬件行为。**

**硬规则**：
```sh
# 动手之前必须先看这一行
cat /sys/class/fpga_manager/fpga0/state      # 必须 == operating
```
- `unknown` ⇒ **只允许**用 `sysfs` 载位流，**绝对不要** `/dev/mem`、不要 `pynq.DisplayPipeline`、不要播放器
- `operating` ⇒ 才可以读/写 `0x400A0000`

**重启后的正确顺序**：
```sh
# ① 只读，不碰 AXI
cat /sys/class/fpga_manager/fpga0/state
# ② 载位流（sysfs 路线，不依赖 PYNQ 元数据）
sudo cp -f /home/xilinx/demo/design_1_v58.bit /lib/firmware/
echo design_1_v58.bit | sudo tee /sys/class/fpga_manager/fpga0/firmware
cat /sys/class/fpga_manager/fpga0/state       # 必须 == operating
# ③ 现在才允许读寄存器 / 跑 minimal_proof / 跑播放器
```
