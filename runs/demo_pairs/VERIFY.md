# 演示播放器 · 上板实测证据（2026-11-03）

**运行点**：PTNQ 3.1.0 / 内核 6.12.40 / 位流 `design_1_v58.bit`（md5 `555c22cf29a1482f8d6050645190fe92`，
**PL 里已跑着，全程 `Overlay(download=False)` 未重载**）/ `VIDEO_MODE=0` 720p60 /
`CTRL=0x01` / FCLK0 写 100（真值 ≈66.67 MHz）/ 帧缓冲双块 `2764800 B`。
板端 Python `/usr/local/share/pynq-venv/bin/python3`，一律 `echo xilinx | sudo -S -p ''`。

交付物：`tools/prepare_demo_pairs.py`（主机配对）与
`pynq_specialized_code/ps/pynq/demo_player.py`（板端播放器，含 `docs/camera/10` 协议）。

---

## 1. 判据 1：8 对生成成功、manifest 完整、md5/大小对得上

`python3 tools/prepare_demo_pairs.py --mode pcref --format bin` → `runs/demo_pairs/`

| # | 名字 | 左 md5 | 右 md5 | 字节 | out1 mean/std | u8 mean/std |
|---|---|---|---|---|---|---|
| 0 | 0094 | `9d6ca2678b` | `bccc0dacb3` | 1228800 | 0.973/0.137 | 247/34.7 |
| 1 | 0049 | `8f3303ad4e` | `bb0976509c` | 1228800 | 0.950/0.161 | 242/40.9 |
| 2 | 0060 | `d69b53991c` | `c6db05df9f` | 1228800 | 0.914/0.229 | 232/58.0 |
| 3 | 0084 | `ae04288973` | `484666eee4` | 1228800 | 0.909/0.236 | 231/59.7 |
| 4 | 0062 | `636349d80f` | `1b0929982a` | 1228800 | 0.965/0.146 | 245/36.9 |
| 5 | 0089 | `8083fe423d` | `6fbbae9fb3` | 1228800 | 0.484/0.165 | 124/42.2 |
| 6 | 0097 | `db1321ccaa` | `2bc92a868d` | 1228800 | 0.977/0.131 | 247/33.0 |
| 7 | 0001 | `92c6ea2aeb` | `7c18d3a56d` | 1228800 | 0.438/0.235 | 112/59.9 |

- `manifest.json` 字段：`version=1, W=640, H=640, channels=3, dtype=uint8, pixel_format=RGB888,
  format=bin, count=8, pairs[{index,name,left,right,bytes,md5,right_bytes,right_md5,...}],
  display{...}, pipeline{...}, selection{...}, net{...}` —— 齐全。
- 单文件恒 `640*640*3 = 1,228,800 B`；8 对共 **19.66 MB**。
- `--verify` 复算：`bytes` + `md5`（左右都算）**逐对 PASS**；`version/W/H/count` 齐全。
- 选片：`score = 边缘密度 × 双峰性`（在 256² 中心裁块上算），`0094/0049` 强制前两张；
  另外 **滤掉 2 张网络输出几乎全白的图**（`0043` u8 std=1.4、`0003` u8 std=8.5），
  原因写进 `manifest.json` 的 `selection.skipped`。**这是选片，不是统计评测。**

## 2. 判据 2：尺寸 640×640×3、compose 后 1280×720、上下各 40 行全黑（numpy 断言）

`--verify` 对每对逐条断言（不靠目测）：

```
[PASS] left (640,640,3) uint8   / right (640,640,3) uint8
[PASS] blit frame 720x1280x3 C-contig
[PASS] blit 上 40 行全黑        / blit 下 40 行全黑
[PASS] compose shape 720x1280x3 uint8
[PASS] compose 上/下各 40 行全黑
[PASS] compose 左半 == left 的 256 入口最近邻放大
[PASS] compose 右半 == right 的 256 入口最近邻放大
```

预览：`compose_check_0000.png`（1280×720，左 0094 原图/右 PC 浮点参考）。

> **几何说明（重要）**：`display_layout.compose()` 的入口是 **256²**，而本包的图对是 **640²**
> （= 屏幕半边真实像素数）。板端默认路径因此是 **1:1 贴图**（零缩放、零细节损失），
> 几何与 `compose()` 完全一致；`--layout-compose` 提供走 `compose()` 的 A/B 路径，
> 两条路径的 1280×720 / 40 行黑边 / 左右来源都由上面的断言覆盖。

## 3. 判据 3：20 帧实测帧间隔（1 fps）

`report_final.json`（`/home/xilinx/demo/player_final.log`，START 由**主机 TCP** 发起）：

```
frames=21 intervals=20
  k=00 -> k=01 : 1.000097  (偏差 +0.000097)
  k=01 -> k=02 : 0.999931  (偏差 -0.000069)
  k=02 -> k=03 : 0.999996  (偏差 -0.000004)
  k=03 -> k=04 : 0.999996  (偏差 -0.000004)
  k=04 -> k=05 : 0.999996  (偏差 -0.000004)
  k=05 -> k=06 : 1.000002  (偏差 +0.000002)
  k=06 -> k=07 : 1.000012  (偏差 +0.000012)
  k=07 -> k=08 : 1.000003  (偏差 +0.000003)
  k=08 -> k=09 : 0.999998  (偏差 -0.000002)
  k=09 -> k=10 : 0.999993  (偏差 -0.000007)
  k=10 -> k=11 : 0.999995  (偏差 -0.000005)
  k=11 -> k=12 : 1.000005  (偏差 +0.000005)
  k=12 -> k=13 : 1.000005  (偏差 +0.000005)
  k=13 -> k=14 : 0.999983  (偏差 -0.000017)
  k=14 -> k=15 : 1.000019  (偏差 +0.000019)
  k=15 -> k=16 : 0.999989  (偏差 -0.000011)
  k=16 -> k=17 : 1.000009  (偏差 +0.000009)
  k=17 -> k=18 : 0.999993  (偏差 -0.000007)
  k=18 -> k=19 : 0.999998  (偏差 -0.000002)
  k=19 -> k=20 : 1.000008  (偏差 +0.000008)

mean = 1.000001 s     max|T-1.000| = 0.000097 s = **0.097 ms**（目标 ≤20 ms ⇒ 富余 206×）
min = 0.999931        max = 1.000097
```

- 时刻表锚点 = **第 0 帧真正提交的时刻**，`sleep(1.0)` 累加**没有用**（会漂移）。
- 每帧耗时：构图 `build ≈ 8.5 ms`（640² 贴 1280×720）、`0x24` 等待 `≈12.4 ms`（vsync 相位）。

## 4. 判据 4 / 11：三种触发 + START→第一帧延迟

| 触发 | 实测 | 证据 |
|---|---|---|
| **TCP**（主机 `192.168.7.1` → 板 `:9999`） | `ACK START 0` 3.1 ms；**端到端到 `EVT FIRST` 43.8 ms / 83.0 ms** | `player_final.log`、`report_proto.json` |
| **UDP**（主机 → `:9999/udp`） | `START` → `ACK START 0` **2.4 ms**；`STOP` → `ACK STOP` 1.1 ms | `player_uf.log` |
| **文件** `touch /home/xilinx/demo/START.flag` | 50 ms 内发现 → 开播；flag **改名**为 `START.flag.<时间戳>.done`（不删） | `player_uf.log` |

- **板内 `START→第 1 帧上屏`**：`--start-delay 0` 时 = **1.318 ms**（出图本身）；
  **默认 `--start-delay 1.0` 时 = 1000.4~1000.7 ms**（= 1.000 s 延迟 + 0.4~0.7 ms 出图），详见 §13。
- 端到端 `START→EVT FIRST`（含 vsync 相位 + 回包）43.8~83.0 ms。
- 为什么这么快：`Overlay`/心跳/双缓冲/第 0 帧预构图/网络探测**全在等触发之前**做完；
  端口在**全部就绪之后**才 `listen` ⇒ **端口一开 = 立刻能出图**。

> **修过的一个真坑（2026-11-03 20:39 复测）**：网络探测里的 `ping -c3`（相机不在线要 ~3 s）
> 原本夹在 `bind()` 与等待循环之间，让 START 事件排在它后面 ⇒ 首帧被推迟到 **2700 ms**。
> 现已把探测移到 `listen` 之前（顺序：`setup()` → 预构图 → 网络探测 → `bind().start()`），
> 复测首帧 = **2.4 ms**（`--max-frames 4` 冒烟：间隔 1.000034 / 0.999991 / 0.999989，
> 22/22 协议仿真回归同步通过）。

## 5. 判据 5：`0x24` 采纳校验

`21/21 = 100%`（`latch_pass_rate=1.000`，`latch_bad=0`）。
另两轮：`4/4`、`34/34`。`0x24` 与写入的 `FRAME_ID` 全部一致；一次 MISS 都没有。

## 6. 判据 6：`0x2C`（欠载计数）播放期间恒 0

```
uf_baseline_0x2c = 0        （软复位后读）
uf_edge_0x2c     = 0        （彩条→扫描切换瞬间）
uf_max           = 0        （21 个采样点全 0）
uf_all_zero      = True
```
> 口径：0x2C 是"自上次软复位以来被读空的行数"，所以播放器在**每轮第 0 帧上屏后软复位一次**，
> 统计窗口 = 稳态播放；切换瞬间的值单独记成 `uf_edge`。
> 早期一版（未在切换后清零）曾看到 `edge=2` 行，独立记录在 `report_proto.json` 的
> `play_1..4.uf_max=2`，**稳态 0** —— 已按上面的口径修好并重测。

## 7. 判据 7：收尾

```
[sig] 收到 SIGTERM ⇒ 优雅退出（回彩条、释放缓冲）
[fin] CTRL=0x15（彩条恢复）
[fin] 帧缓冲已释放（pipe.close）
[fin] fpga0=operating
[exit] rc=0                       ← 进程返回码 0
sync rc=0
```
- **不 poweroff、不 pkill -f**；SIGTERM 按 `--pidfile` 里的 python pid 精确发。
- 只有 `STOP` / `SIGTERM` 才回彩条（断连、非法命令都不回 —— 判据 9 规则 3）。

## 8. 判据 8：板端文件 md5 与主机一致

`/home/xilinx/demo/MD5SUMS` 46 项，`md5sum -c` → **46/46 OK**（含 8 对 16 个 `.bin`、
manifest、README、net_stats、16 个 `pcref_raw/*.npy`、3 个兄弟模块、位流/hwh、播放器本体）。
主机侧 `demo_player.py` md5 = `03fc955020a6bf121e606782646f3830`，与板端一致（46/46）。

## 9. 判据 9 / 10：`docs/camera/10` 协议 · 五条规则 + §8 自检表（板端实测）

`/tmp/board_proto_drive.py` 对**真板子**跑，逐条打印收发原文：

```
[PASS] §8-1  PING → PONG <ms>                    -- 'PONG 10056'（2.3 ms）
[PASS] §3    HELLO 1 → ACK HELLO 1               -- 'ACK HELLO 1'（0.8 ms）
[PASS] §8-2  START → ACK START <n>               -- 'ACK START 0'（ACK 2.4 ms）
[PASS] §4    START → EVT FIRST ≤ 1000 ms         -- 'EVT FIRST'，端到端 43.8 ms
[PASS] §8-3  乱码 → ERR BADCMD XYZ               -- 'ERR BADCMD XYZ'
[PASS] 大小写敏感：start → ERR                    -- 'ERR BADCMD start'
[PASS] §8-4  STOP → ACK STOP                     -- 'ACK STOP'（0.7 ms）
[PASS] §8-5  断连 → 播放继续                       -- 断开 2.2 s 后重连，n 25→32
[PASS] 规则1 START 幂等（不重置进度）              -- 'ACK START 23' → 2 s 后 'ACK START 25'
[PASS] 规则2 断连不停播                            -- 见 §8-5
[PASS] 规则3 STOP 冻住帧计数（1.5 s 内 0 新增）     -- 26 → 26
[PASS] 规则3 重新 START 恢复播放                    -- 31 → 33 → 34（连续涨）
[PASS] 规则4 新连接顶掉旧连接                       -- 旧连接 recv 得 <closed>
[PASS] 规则4 新连接可用                            -- 'PONG 35899'
[PASS] 规则5 非法输入不中断播放                     -- ERR 后 PING 正常、帧计数继续涨
[PASS] 规则5 10 s 无数据 ⇒ 板端关连接               -- 实测 2.0 s 档（--idle-timeout 2）：对端读到 <closed>
```

> 说明：`规则3 恢复播放` 那一条在自动化脚本里第一次报 FAIL，原因是**脚本没消费恢复时
> 主动发的那行 `EVT FIRST`**（按协议每次开播第一帧上屏都会发）。单独复测已通过：
> `START→31、1.4 s→33、再 1.4 s→34`。

**多连接/旧连接关闭的原始日志**：
```
[proto] 新连接 192.168.7.1:36292 ⇒ 关闭旧连接 192.168.7.1:34638（规则 4）
```

## 10. 网络现状（**与 `docs/camera/11` 的差异，需要拍板**）

本次实测（20:18 / 20:26 两次）：

```
ip -4 -o addr show   → 只有 lo + eth0 192.168.7.2/24
lsusb                → 只有 0424:2514 hub + 根 hub   ← **没有 359f:2120 MaixCAM**
/sys/class/net/      → eth0 lo sit0                  ← **没有 usb0 / usb1**
dmesg                → rndis_host/cdc_ncm **unregister**（1-1.2），即设备被拔掉
systemctl            → usb-cam-net.service: enabled / active（oneshot RemainAfterExit）
```

$$\Rightarrow\ \textbf{相机当前不在线}（USB 未枚举）\Rightarrow \texttt{10.13.148.100}\ \text{尚不存在}$$

播放器的处理（**只告警、不退出**，演示要能起来）：
```
[net] !! 找不到 cdc_ncm 接口（USB 网卡没枚举？）——继续跑，但相机可能连不上
[net] 全部 IPv4：eth0=192.168.7.2/24
[net] ping 10.13.148.1 ×3 → rc=2  (无输出)
[net] camera should connect to 10.13.148.100:9999
```
接口按**驱动 `cdc_ncm`** 找（不按 `usb0/usb1` 名字硬编码），拿到接口后核对
`10.13.148.100/24` 并 `ping -c 3 -W 1 10.13.148.1`，结果都写进日志。

> 相机插上后要复核的两点：① `usb1` 是否拿到 `10.13.148.100/24`（若被 DHCP 改掉要报）；
> ② `10.13.148.0/24` 与 `192.168.7.0/24` 不同网段 ⇒ 无路由冲突。
> 板端**没有** DHCP 服务（无 dnsmasq/udhcpd），`/etc/network/interfaces.d/` 只有 `eth0`；
> 板端也**没有** `sshpass`（要从板端 ssh 进相机得走 pty）。

## 11. 文件

| 文件 | 内容 |
|---|---|
| `tools/prepare_demo_pairs.py` | 主机配对（`--mode pcref|gt`、`--format bin|png`、`--verify`） |
| `pynq_specialized_code/ps/pynq/demo_player.py` | 板端播放器（协议 v1 + 1 fps + 双缓冲 + 优雅退出） |
| `runs/demo_pairs/` | 8 对 `pairs/*.bin` + `manifest.json` + `net_stats.json` + `compose_check_0000.png` + `pcref_raw/` |
| `runs/demo_pairs/board_evidence/` | 板端日志与 report JSON 原件（`player_final.log` / `report_final.json` / `player_uf.log` / `player_sig2.log` / `report_proto.json`） |

## 12. 复现

```bash
# 主机
python3 tools/prepare_demo_pairs.py --verify
# 板端（部署包 = /home/xilinx/demo，含 3 个兄弟模块 + 位流）
echo xilinx | sudo -S -p '' env XILINX_XRT=/usr \
  /usr/local/share/pynq-venv/bin/python3 -u /home/xilinx/demo/demo_player.py \
  --pairs /home/xilinx/demo/pairs --log /home/xilinx/demo/player.log --pidfile /tmp/demo_player.pid
# 交互自检
nc 192.168.7.2 9999     # PING / HELLO 1 / START / XYZ / STOP
# 收工
echo xilinx | sudo -S -p '' kill -TERM $(cat /tmp/demo_player.pid)
```


## 13. 追加（2026-11-03 20:44）：START 后延迟 1 s + 循环衔接（3 轮 / 5 轮）

`--start-delay`（默认 **1.0 s**）：收到 `START` 后**等 1.0 s 才出第一帧**，期间**不碰 CTRL**
（屏幕保持彩条，不闪）；`ACK START <n>` 由网络线程**立刻回**（不等延迟）；
`EVT FIRST` 在第一帧真正上屏时才发；网格原点 = `t_start + delay` ⇒ 第 k 帧 =
`t_start + 1.0 + k×1.000 s`（`monotonic` 绝对时刻）。

板端实测（`--auto --start-delay 1.0 --loops N --once`，1 fps）：

| 轮数 | 帧数 | START→第1帧 | 间隔 mean | max\|T−1.000\| | 首帧→末帧 | **START→末帧（理论）** |
|---|---|---|---|---|---|---|
| 3 轮（8 对） | 24 | **1000.411 ms** | 0.999983 s | **0.388 ms** | 22.999610 s | **24.000020 s（24.0）** |
| 5 轮（8 对） | 40 | **1000.722 ms** | 0.999982 s | **0.697 ms** | 38.999293 s | **40.000015 s（40.0）** |

- **40 s 累计偏差 15 µs**（0.4 ppm）⇒ 无漂移。
- `0x24`：**24/24** 与 **40/40**（`latch_bad=0`，全程无 MISS）；`0x2C`：两轮 **全 0**（24 / 40 个采样点）。
- **回绕（第 8→第 1 帧）无异常**：24 帧那次 2 次回绕 `dt=1.000003 / 1.000005`；
  40 帧那次 4 次回绕 `dt=0.999992 / 0.999987 / 1.000000 / 1.000003`（偏差 ≤13 µs）。
- 对序完整无缺：`012345670123456701234567`、`01234567`×5。
- 进程墙钟（含 ~12 s 启动：Overlay 1.4 s + 心跳 0.2 s + 预载 0.44 s + 网络探测 ≈3 s）：
  24 帧 = 38.9 s，40 帧 = 53.4 s；播放段本身 = 24.000 / 40.000 s。
- 收尾：`CTRL=0x15`、缓冲释放、`fpga0=operating`、`rc=0`、`sync rc=0`、板端 md5 46/46。

### 三种跑法（已写进 `runs/demo_pairs/README.md`）

```bash
# ① 对焦/构图
--still 0
# ② 试片（不用相机）
--auto --start-delay 1.0 --loops 3
# ③ 正式（等相机 START，无限）
--loops 0
```


## 14. 事故与修复（2026-11-03 21:00-21:15）：倒计时期间黑屏 + 判据加严

### 14.1 现象
用户按"45 s 倒计时"等屏幕，**看到的是一整段黑**；40 s 播放期间也报黑。
同期寄存器全绿：`0x24` 20/20、`0x2C=0`、`mmcm_locked=1`、像素时钟 74.4 MHz、
**帧缓冲回读 md5 与主机算的帧逐字节相同**（`00e3a305…`，见 §14.3）。

### 14.2 已定位的直接原因（我的 bug）
`_soft_reset_ufcnt()` 里的掩码写成了 `CTRL & CTRL_RUN`（只保留 bit0），
本意是"保留 enable + test_pattern 位"，实际**把 test_pattern 位清掉了**：

```
setup(): 写 CTRL=0x15（彩条）→ 立刻被 _soft_reset_ufcnt 改写成 0x01
         ⇒ 空闲态变成"扫描一块预填全黑的缓冲" ⇒ 倒计时 45 s 就是纯黑
```
**为什么"黑"而不是"彩条"**：`CTRL=0x15` 才是彩条（`scan_sel=1` 选 `bar_rgb`）；
被改写成 `0x01` 后选了扫描通路，而两块缓冲在 `setup()` 里被 `_prefill_blocks()` 预填成黑，
这一次没有提交过任何帧 ⇒ 黑。

**修复**：掩码改为 `CTRL_KEEP=0x15`；且**播放中一次都不碰 CTRL**（原来在第 0 帧提交后还做了一次
20 ms 软复位，等于在"有画面"时把扫描器复位 —— 已删除）；`setup()` 里只做一次软复位（此时无画面），
之后空闲写 `0x15`（彩条），`START` 时写 `0x01`（与 `display_demo` 的板验顺序同向）。

### 14.3 同时发现的现场问题（会把后续测试带偏）
两轮点播进程**没死**：它们是做帧缓冲回读时被 `kill -STOP` 冻结的，
后来 `kill -TERM` 打到了 `sudo` 包装而不是 `python` 子进程 ⇒ 一直处于状态 `T`，
占着 11 MB CMA（`CmaFree` 一度只剩 528 kB ⇒ 新的 `allocate` 会失败）。
现已 `kill -9` 清理，`CmaFree` 回到 6.6 MB。**以后按 `--pidfile` 里的 python pid 发信号。**

### 14.4 新增判据 7'（每帧 nonblack 回读）
每帧写完立刻回读**这块 DMA 缓冲**（`pipe._u8` 视图，就是 PL 扫的那块），统计非黑像素占比：

```
[frame 000] pair=00/0094 fid=1 latch=OK 6.3ms 0x24=0x1 uf=None nonblack=88.9%
nonblack 期望 = 1280*640 / (1280*720) = 88.9%（黑的只有上下各 40 行黑边）
< 80% ⇒ 日志打 [warn]；report 里给 nonblack_min
```
这条能抓住"数据写了但画面全黑"和"贴图几何错"，而 `0x24/0x2C` 抓不住。

### 14.5 ★ 根因确认（A/B 实测，2026-11-03 21:16）
用**最小脚本**（`runs/demo_pairs/minimal_proof.py`：只 `CTRL=0x15` → `DisplayPipeline(mode=0).setup()`
→ `CTRL=0x01` → `show(orig,proc)`，**没有软复位、没有 FCLK0 写入**）推一张合成图
⇒ **用户确认"有图"**。同一块帧缓冲、同一位流、同一时钟。

$$\Rightarrow\ 	extbf{元凶 = } 	exttt{CTRL[1]}\ 	ext{软复位}\ (	exttt{0x03}\ 	ext{保持}\ 20\ 	ext{ms}）$$
它打到 `hdmi_fb_scan.cfg_soft_reset`，之后扫描 FSM 不再吐 `pix_valid`
⇒ `rgb_out = scan_pix_valid ? scan_pix_rgb : 0` **恒 0**，而 `hdmi_de` 仍有效
⇒ **DE 有效 + 像素全 0 = 全黑**；`0x2C=0` 也自洽（握手根本不发，它统计不到）。
除此之外的一切（数据 md5、latched、时钟、stride、块号）都是对的。

### 14.6 最终形态（v3，`md5 7e0c28d2031c61e629dc552dfa3abc94`）
- **删掉** `_soft_reset_ufcnt()` 整个函数与两处调用；**删掉** `Clocks.fclk0_mhz` 写入。
- **直接写的寄存器只剩**：进 `setup()` 前 `CTRL=0x15`、`START` 后 `CTRL=0x01`、`STOP/SIGTERM` 时 `CTRL=0x15`。
  `VIDEO_MODE/FB_*/FRAME_ID/PIXEL_FMT/模式切换` 全部由 `DisplayPipeline` 负责。
- **FCLK0 只读**：报数 100 → 真值 66.67；不在 60~75 就报错退出（不在代码里改频率）。
- **0x2C 只报增量**：基线 = 进程启动首读，之后 `uf` 是相对基线的差。
- 每帧回读 `nonblack%`（只读，安全），`<80%` 打 `[warn]`。

> 教训（三次同源）：**"清零计数器"这种好意动作，把显示通路打死了。**
> 以后凡是要写 `CTRL`，先问一句"这会不会碰到 bit1"。


## 15. 最终结构与 3 轮实测（2026-11-03 21:31–21:34，已由用户肉眼确认）

### 15.1 结构：`demo_player` 是薄壳，`minimal_proof` 是唯一实现
```python
from minimal_proof import bringup as mp_bringup, show as mp_show, ctrl_now as mp_ctrl
pipe, ctx = mp_bringup()      # CTRL=0x15 → 1 s → DisplayPipeline(mode=0).setup() → CTRL=0x01
fid = mp_show(ctx, frame)     # 每帧只调这一句（= pipe.show_frame(frame)）
```
`demo_player` 里再无 `show_frame()` 直接调用；`_write_ctrl()` 只剩 STOP/SIGTERM 的 `0x15`
与"上一轮 STOP 留下彩条时才切 `0x01`"。

### 15.2 为什么改成 import（今晚最贵的一课）
前四次"照着最小脚本重写"的版本**全部黑屏**，而最小脚本本身一直能出图。
最后一次做了**机械打桩 diff**（给 `MmapRegIo.write32` 打桩 + `minimal_proof.WRITES` 合并成一条时间线）：

```
MIN  (13): 0x00=0x15, 0x00=0x14, 0x08=0x00, 0x0C=0x30100000, 0x10=0x00,
           0x14=0xF00, 0x18=0x500, 0x1C=0x2D0, 0x28=0x00, 0x08=0x00,
           0x00=0x15, 0x00=0x01, 0x20=0x01
DEMO (13): 完全同上            SEQUENCE_IDENTICAL = True
```

$$\Rightarrow\ 	ext{写序列一致也会黑} \Rightarrow 	ext{差异在}	extbf{进程环境/时序}（	exttt{bringup()} 	ext{之后的 } 	exttt{arm(0)} + 	exttt{ping} 	ext{ 的 3 s 空档）}$$
$$\Rightarrow\ 	extbf{结论：等价性要靠"同一份代码"，不靠"看起来一样"。}$$

### 15.3 3 轮实测（24 帧，`--auto --start-delay 20 --loops 3 --once`）
```
mean = 0.999981 s    max|T-1.000| = 0.433 ms    min/max = 0.999567 / 1.000069
首帧→末帧 = 22.999571 s      START→末帧 = 43.000022 s（= 20 s 延迟 + 23 s，理论 43.000）
0x24 = 24/24 = 100%          0x2C 增量 = 0（24 采样点全 0）
nonblack 最小值 = 0.8884（88.8%）   停因 = loops
回绕 7→0：dt = 0.999986 (-14 µs)、1.000003 (+3 µs)
收尾：CTRL=0x15 → 缓冲释放 → operating → rc=0 → sync rc=0
md5：demo_player 64c3c8f2… / minimal_proof 260a200e…
```

### 15.4 待办（正式录制前）
`bringup()` 之后还有 ~3 s 空档（`arm(0)` + `ping -c3`），录正式片时会让"START→首帧"多 3 s。
**保守方案**：把 `advertise_addresses()`（含 ping）挪到 `bringup()` **之前**，
改完**必须重跑 `--still 0` 让用户再确认一次**。
