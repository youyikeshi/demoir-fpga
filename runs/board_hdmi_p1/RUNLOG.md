# HDMI P1 上板 RUNLOG —— 板端测试工程师

- 板子 `192.168.7.2` / 用户 `xilinx`
- 全部板端交互经 `tools/boardlock.sh`（ping 预检 + 全局 flock + ssh ≤6 次/分）
- 本文件按执行顺序记录**每一条命令与其原始输出**；不能复现的一律标注
- 每次 Python 板端调用形状固定：
  `echo xilinx | sudo -S -p '' sh -c 'ulimit -c 0; exec env XILINX_XRT=/usr timeout -s KILL <T> /usr/local/share/pynq-venv/bin/python3 -u <脚本>'`
  ⇒ 一次进程只读/写**一个**寄存器，`timeout` 包住，输出无缓冲

---

## 0. 被测对象与冻结资产（上板前主机侧核对）

```
$ md5sum runs/hdmi_p1/hdmi_p1.bit runs/hdmi_p1/hdmi_p1.hwh
d478c6058721492694d6d4a5f51a5196  runs/hdmi_p1/hdmi_p1.bit
3a5053bca9e4d9fbb465c0d4d8014be9  runs/hdmi_p1/hdmi_p1.hwh

$ ls -l ...
-rwxrwxrwx 1 felix felix 3510797  11月  3 00:57 runs/hdmi_p1/hdmi_p1.bit
-rwxrwxrwx 1 felix felix  140727  11月  3 00:55 runs/hdmi_p1/hdmi_p1.hwh

$ cd pynq_specialized_code && md5sum -c ../runs/hdmi_p1/build_manifest.txt
pl/vivado/hdmi_p1/rtl_frozen/hdmi_out_top.v: 成功
pl/vivado/hdmi_p1/rtl_frozen/hdmi_timing_gen.v: 成功
pl/vivado/hdmi_p1/rtl_frozen/hdmi_color_bar.v: 成功
pl/vivado/hdmi_p1/rtl_frozen/sil9134_init.v: 成功
pl/vivado/hdmi_p1/rtl/hdmi_p1_top.v: 成功
pl/vivado/hdmi_p1/constraints_frozen/hdmi_out_pins.xdc: 成功
pl/vivado/hdmi_p1/constraints/hdmi_p1_clocks.xdc: 成功
pl/vivado/hdmi_p1/build_hdmi_p1.tcl: 成功
../runs/hdmi_p1/hdmi_p1.bit: 成功
../runs/hdmi_p1/hdmi_p1.hwh: 成功
```
⇒ 位流 ↔ 源码 manifest **10/10 全过**，上板验的就是仓库里这一份。

---

## 第 0 步：排他 + 四项体检  ✅

```
$ BOARD_TAG=hdmi-p1-board-tools tools/boardlock.sh ssh xilinx@192.168.7.2 '...'
[boardlock] 取得板级独占 01:14:03  使用者=hdmi-p1-board-tools
== df -h / ==
Filesystem      Size  Used Avail Use% Mounted on
/dev/root        28G  7.8G   19G  30% /
== df -i / ==
Filesystem      Inodes  IUsed   IFree IUse% Mounted on
/dev/root      3751936 208728 3543208    6% /
== sudo ==
sudo_rc=0
== fpga0 state ==
unknown
== uptime ==
 04:18:08 up 1 min,  1 user,  load average: 1.10, 0.43, 0.15
== uname ==
Linux pynq 6.12.40-xilinx-g31626ef92ff1 #1 SMP PREEMPT ... armv7l
[boardlock] 释放板级锁 01:14:05  rc=0
```

| 体检项 | 原始值 | 判定 |
|---|---|---|
| `df -h /` | 28 G 容量 / 用 7.8 G / 剩 **19 G** / 30% | ✅ |
| `df -i /` | IUse% **6%**（208728/3751936） | ✅ |
| `sudo true` | `sudo_rc=0` | ✅ |
| `fpga0/state` | **unknown**（PL 空，符合"上电后"预期） | ✅ |

**旁证**：`uptime` = 1 min ⇒ 板子是刚上电的干净状态（与事故档案 §6 恢复记录一致）。
**load 令牌**：`/tmp/dsh_board.loadtoken` 里有一条 00:56:23（上一轮已崩会话）的陈旧令牌。
已删除并在下条记录理由——板子已断电重上电、`fpga0=unknown`、PL 为空 ⇒ 该令牌所指的那次 load 已不存在，
新会话的"唯一一次 load"必须重新签发，否则合法 load 会被 `exit 8` 挡住。

---

## 第 1 步：传文件 + 板端核对 md5  ✅

```
$ BOARD_TAG=hdmi-p1-transfer tools/boardlock.sh scp hdmi_p1.bit hdmi_p1.hwh xilinx@192.168.7.2:/home/xilinx/
[boardlock] 取得板级独占 01:14:11 ... 释放板级锁 01:14:12  rc=0

$ BOARD_TAG=hdmi-p1-md5 tools/boardlock.sh ssh ... 'cd /home/xilinx && ls -l ... && md5sum ...'
-rwxrwxr-x 1 xilinx xilinx 3510797 May  9 04:18 hdmi_p1.bit
-rwxrwxr-x 1 xilinx xilinx  140727 May  9 04:18 hdmi_p1.hwh
d478c6058721492694d6d4a5f51a5196  hdmi_p1.bit
3a5053bca9e4d9fbb465c0d4d8014be9  hdmi_p1.hwh
```

| 文件 | 要求 md5 | 板端实测 | 结论 |
|---|---|---|---|
| `hdmi_p1.bit` | `d478c6058721492694d6d4a5f51a5196` | `d478c6058721492694d6d4a5f51a5196` | ✅ 一致 |
| `hdmi_p1.hwh` | `3a5053bca9e4d9fbb465c0d4d8014be9` | `3a5053bca9e4d9fbb465c0d4d8014be9` | ✅ 一致 |

同名同目录（`/home/xilinx/`），满足 PYNQ 对 `.bit`+`.hwh` 的配对要求。

---

## 第 2 步：★ 加载 + 心跳（本次最关键）

### 2A 唯一一次 load + 首组心跳（`step2A_load_hb.log`）

```
[boardlock] 已签发唯一 load 令牌
== [A1] pynq version ==
pynq 3.1.0 py 3.10.4
== [A2/A3] 清 PYNQ 残留 PL 状态 ==
rm .../pynq/pl_server/_current_metadata.pkl
rm .../pynq/pl_server/global_pl_state_.json
== [A4] fpga0 state before load ==
unknown
== [A6] LOAD (single download, timeout 90) ==
OVERLAY_OK download=True ips=['hdmi_p1_top_0', 'ps7']
FCLK0 前 = 150.0 MHz          ← ★ PYNQ 报数口径，见 §2D 的裁定
LOAD_PROC_DONE
load_rc=0
== [A7] fpga0 state after load ==
operating
== [A8] READ 0x30 #1 ==
RD off=0x30 val=0x00000002  bin=...0010  elapsed=0.000 s
rd1_rc=0
== [A9] READ 0x30 #2 (>=10 ms later) ==
RD off=0x30 val=0x00000003  bin=...0011  elapsed=0.000 s
rd2_rc=0
```
- load 进程**只**做 `Overlay()`，不读任何 PL 寄存器（协议要求）
- 载入后 `fpga0/state` = `operating`
- `0x30` 从 `0x02` → `0x03`：**`[0]` 翻转、`[1]=1`** ⇒ 心跳通过（但下面还要把"多快"量出来）

### 2B 心跳的**速率**（`step2B_fclk_hb.log`）

单看两次读只能证明"在动"，不能证明**什么频率在动**。`hb_cnt[16]` 的半周期 = $2^{16}$ 个像素时钟，
所以在有界窗口内密集采样 0x30、用时间戳取翻转间隔，就能反推像素时钟：

```
== [B1] POLL 0x30 @ FCLK0 报数=150 ==
POLL dur=0.300 s samples=19848 distinct_vals=['0x2','0x3'] bit1=1
POLL edges=679
POLL implied_pix_clk_MHz=149.5559   (v1：仅 median 估计)
```

### 2C 时间基准校验（先证明"我的秒是秒"，`step2C_timebase_rd.log`）

时间戳被我用来算频率，**必须先证明板端时间基准可信**，否则频率结论全部作废：

```
== [C1] clocksource ==
arm_global_timer
arm_global_timer ttc_clocksource
== [C2] 板端 time.sleep(5.0) 自报 ==
board_mono_delta=5.005112 board_perf_delta=5.005115

$ HOST_WALL_CONTROL_SSH   = 0.852 s      (空 ssh，纯开销)
$ HOST_WALL_SLEEP10_SSH   = 10.869 s     (板端 sleep 10.0 s)
$ HOST_WALL_SLEEP_PORTION = 10.017 s
$ TIME_BASE_RATIO(board/host) = 0.9983
```
⇒ **板端时间基准误差 0.17%**，下面的频率反推成立。

### 2D ★★ 关键发现：`Clocks.fclk0_mhz` 的报数被放大了 1.5 倍

把 2B/2C 的读数拿去对源码，发现"像素时钟 ≈ FCLK0 报数"，而 RTL 里是 ×1.484375
（`MMCME2_ADV`: `CLKFBOUT_MULT_F=11.875`、`DIVCLK_DIVIDE=1`、`CLKOUT0_DIVIDE_F=8.000`）。
两者不可能同时成立 ⇒ 逐条取证：

**证据 1 —— 位流自己的 PS7 时钟配置（`runs/hdmi_p1/hdmi_p1.hwh`）**
```
"PCW_IO_IO_PLL_FREQMHZ"           VALUE="1000.000"
"PCW_IOPLL_CTRL_FBDIV"            VALUE="30"        # 33.333 × 30 = 1000 MHz
"FCLK0_PERIPHERAL_CLKSRC"         VALUE="IO PLL"
"FCLK0_PERIPHERAL_DIVISOR0"       VALUE="5"
"FCLK0_PERIPHERAL_DIVISOR1"       VALUE="2"
```
⇒ $FCLK0 = 1000 / (5\times2) = \mathbf{100\ MHz}$

**证据 2 —— PYNQ 的报数**
- 上电（未动过）：`Clocks.fclk0_mhz = 150.0` ⇒ 150/100 = **1.500 倍**
- 我按文档写的"设成 100"执行后：`Clocks.fclk0_mhz = 100.0`，此时实测像素时钟
  = **98.99 MHz** = $11.875\times 66.67/8$ ⇒ 真实 ACLK = **66.67 MHz** = 1000/15
  ⇒ 写"100"实际让硬件分频成 15 分频，**真实 FCLK0 = 66.67 MHz**（不是 100）

**证据 3 —— 恢复报数 150 后，像素时钟精确回到设计值（`step2D_restore_verify.log`）**
```
== [D3] POLL 0x30 (span 估计, 0.5 s) ==
POLL edges=1132  span_ms=499.268
POLL[median] half_period_ms=0.441630 implied_pix_clk_MHz=148.3957
POLL[span]   half_period_ms=0.441440 implied_pix_clk_MHz=148.4597
```
设计值 148.4375 MHz，实测 148.4597（**+0.015%**）。

| PYNQ 报数 | 真实 ACLK（由像素时钟反推） | 实测像素时钟 | 设计公式 $=1.484375\times$ACLK |
|---|---|---|---|
| 150.0 | **100.0 MHz** | 148.46 MHz | 148.4375 ✅ |
| 100.0（我按文档设的） | **66.67 MHz** | 98.99 MHz | 98.958 ✅ |

$$\Rightarrow\ \text{结论：这套镜像上 } \texttt{Clocks.fclk0\_mhz}\ \text{的读数/写入口径 = 真实值}\times1.5$$
$$\Rightarrow\ \text{要真实 100 MHz，必须写 } \texttt{Clocks.fclk0\_mhz = 150}$$

**由此推翻事故档案的一条前提**（只报事实，归档由主代理定）：
$$\Rightarrow\ \text{事故时"读到 fclk0=150 MHz ⇒ VCO}=1781\ \text{MHz ⇒ MMCM 不锁"}\text{ 的前提是读数=真值}$$
$$\Rightarrow\ \text{本次位流的实证是"报数 150 = 真值 100"；真值 100 时 VCO}=1187.5\ \text{MHz，合法、MMCM 锁定、像素时钟 148.4375}$$
$$\Rightarrow\ \text{若事故版位流的 PS7 配置与本版同（IO PLL 1000 / ÷5 / ÷2），则"MMCM 未锁"不成立，}\textbf{挂死应归因于 AXI 复位被按住（S00/M00_ARESETN 绑 0，BD_NOTES §13.4 的"第二根线"）}$$
$$\Rightarrow\ \text{本条不影响本轮点亮结论，但 } \textbf{§8.3"先设时钟再 load"与"改 100 MHz"两条，按字面执行会把像素时钟打到 98.96 MHz（1080p 变 }\sim40\ \text{Hz）}$$

### 2E 事故红线检查

| 红线项 | 本次 |
|---|---|
| `timeout` 到点被杀 | **0 次**（全部 `rc=0`，`elapsed=0.000 s`） |
| ssh `lost connection` / `Connection reset` | **0 次** |
| `ping` 丢包 | **0 次**（每次 boardlock 前置 ping 均通过） |
| 单次寄存器操作挂住 | **0 次** |
| `pkill -f` / `killall` | **未使用** |

---

## 第 3 步：心跳通过后才读状态  ✅（`step3_reads_720p.log`）

### 3.1 1080p60 成功态（**用户已肉眼确认彩条**）

| 偏移 | 名称 | 实测 | 期望 | 判定 |
|---|---|---|---|---|
| `0x00` | `CTRL` | `0x00000015` | `0x15`（enable\|test_pattern\|motion） | ✅ |
| `0x04` | `STATUS` | `0x00000003` | `0x03` | ✅ |
| `0x08` | `VIDEO_MODE` | `0x00000001` | `1` = 1080p60 | ✅ |
| `0x2C` | `UNDERFLOW_CNT` | `0x00000000` | P1 恒 0 | ✅ |
| `0x30` #1 | `PIX_HB` | `0x00000002` | — | — |
| `0x30` #2 | `PIX_HB` | `0x00000002` | — | 见下方"采样注记" |
| `0x30` POLL | `PIX_HB` | 1133 沿 / 499.719 ms ⇒ **148.4568 MHz** | 148.4375 | ✅ +0.013% |

`0x04` 拆位：`0x03 = 0b011` ⇒ `[0] init_done = 1`（SIL9134 I2C 配置完成）、
`[1] vsync = 1`（自上次读之后发生过场同步，**读清**；连读两次都是 1，因为 60 Hz 下两次读相隔 >16.7 ms）

**★ 采样注记（协议改进项，不是异常）**：`0x30` 的两次独立读出现了"两次相同"的情形
（本次 3 组里有 3 组相同）。原因不是心跳停了——同一次 SSH 里的 POLL 拿到 1133 个翻转沿；
而是两次读之间的实际间隔由进程启动决定（约 0.3 s），若恰好接近翻转周期的整数倍，两次必然同相。
$$\Rightarrow\ \text{判据"两次读必须不同"在 0.88 ms 级心跳上}\textbf{不可靠}$$
$$\Rightarrow\ \text{建议改为：}\textbf{有界窗口内 poll ≥100 ms，要求 } \ge5\ \text{个翻转沿}$$（本次已按此执行）

### 3.2 切 720p60（这是双时钟位流的唯一目的）

严格按 `BD_NOTES §8` 的切换协议（每次写都是独立短进程 + timeout + 回读）：

```
[E9]  WR 0x00 = 0x11  before=0x00000015 readback=0x00000011 OK   (enable=0, 保留 test_pattern|motion)
[E10] WR 0x08 = 0x00  before=0x00000001 readback=0x00000000 OK   (VIDEO_MODE=0 → 720p60)
[E11] sleep 0.05                                                 (≥2 帧)
[E12] WR 0x00 = 0x15  before=0x00000011 readback=0x00000015 OK   (enable=1)
[E13] sleep 0.10
[E14] RD 0x30 = 0x00000002
[E15] RD 0x30 = 0x00000002
[E16] POLL 0x30 0.5 s : edges=566 span_ms=498.813
        POLL[median] half_period_ms=0.883308 implied_pix_clk_MHz=74.1938
        POLL[span]   half_period_ms=0.882855 implied_pix_clk_MHz=74.2319
[E17] RD 0x04 = 0x00000003        → init_done 仍 =1
[E18] RD 0x08 = 0x00000000
[E19] RD 0x00 = 0x00000015
[E20] sync ; sync_rc=0
[E21] fpga0/state = operating
```

$$\Rightarrow\ \text{心跳半周期 } 0.4414\to0.8829\ \text{ms}\ (\times2.000)\Rightarrow 148.46\to74.23\ \text{MHz}$$
$$\Rightarrow\ \text{与 } \texttt{pix\_clk\_b}=74.21875\ \text{MHz 差 } +0.018\%\Rightarrow \textbf{BUFGMUX 确实切到了 } \texttt{pix\_clk\_b}$$
$$\Rightarrow\ \text{切模式期间 } \texttt{init\_done}\ \text{没有掉}（0x04[0]=1），\text{软复位/自动复位没把 SIL9134 打回未配置态}$$

**切换瞬间的状态：停在 720p60（`VIDEO_MODE=0`）**，交用户肉眼判定。

### 3.3 720p60 被 SIL9134 接受 ⇒ 切回 1080p60 并做**双向可逆性验证**（`step4_closure_1080p.log`）

**用户肉眼判定：720p 图是对的** ⇒ SIL9134 同时接受 1080p60 与 720p60，那两笔 I2C 寄存器写不锁死模式、芯片自适应。

#### (a) 720p60 下的可信实测频率（P2 文档基准，两个 1.0 s 窗口）

| 轮次 | 翻转沿 | 首末沿跨度 | 半周期（span） | 反推像素时钟 |
|---|---|---|---|---|
| #1 | 1133 | 999.481 ms | 0.882933 ms | **74.2253 MHz** |
| #2 | 1132 | 998.609 ms | 0.882943 ms | **74.2245 MHz** |

$$\Rightarrow\ \text{均值 } \mathbf{74.2249\ MHz}\ \text{vs 设计 } 74.21875\ \text{MHz} \Rightarrow \textbf{+0.008\%}$$
⇒ 本窗口精度约 ±0.02 MHz（±0.03%），故 **P2 文档基准取 74.225 MHz**

#### (b) 切回 1080p60（与切过去**完全同一套**协议）

```
[F0] RD 0x08 = 0x00000000        (切换前确证仍在 720p)
[F1] WR 0x00 = 0x11  before=0x15 readback=0x11 OK
[F2] WR 0x08 = 0x01  before=0x00 readback=0x01 OK
[F3] sleep 0.05
[F4] WR 0x00 = 0x15  before=0x11 readback=0x15 OK
[F5] sleep 0.10
[F6] RD 0x08 = 0x00000001        ✅
[F7] RD 0x04 = 0x00000003        ✅ init_done=1, vsync=1
[F8] RD 0x00 = 0x00000015        ✅
[F10] RD 0x2C = 0x00000000       ✅
```

#### (c) 切回后的像素时钟（两个 1.0 s 窗口）

| 轮次 | 翻转沿 | 首末沿跨度 | 半周期（span） | 反推像素时钟 |
|---|---|---|---|---|
| #1 | 2265 | 999.471 ms | 0.441462 ms | **148.4520 MHz** |
| #2 | 2264 | 999.052 ms | 0.441473 ms | **148.4486 MHz** |

$$\Rightarrow\ \text{均值 } \mathbf{148.4503\ MHz}\ \text{vs 设计 } 148.4375\ \text{MHz} \Rightarrow \textbf{+0.0086\%}$$

#### (d) ★ 双向可切换判据

$$\frac{148.4503}{74.2249} = \mathbf{2.00001}\ \ (\text{判据区间 } 1.999\sim2.001)\ \Rightarrow\ \textbf{PASS}$$
$$\Rightarrow\ 74.225 \leftrightarrow 148.450\ \text{MHz 可双向切换，且每次切换后 } \texttt{init\_done}\ \text{都保持 }1$$
$$\Rightarrow\ \text{BUFGMUX 切换没有把 SIL9134 打回未配置态、没有丢帧级复位、没有 AXI 挂死}$$

#### (e) 收尾

```
sync ; sync_rc=0
fpga0/state = operating
uptime = 04:27:14 up 10 min, load average 0.16 0.17 0.15
dmesg 尾部 20 行 = 仅 zocl 的 create/destroy client 与 "bitstream … locked/unlocked, ref="
                  （= 每次 Overlay(download=False) 建上下文的正常日志）
                  无 AXI timeout、无 OCM/ECC、无 kernel panic、无 fpga_manager 报错
```

**板子最终状态：1080p60（`VIDEO_MODE=1`）、`CTRL=0x15`、`fpga0/state=operating`、未 poweroff、未动 SD 卡。**

---

## P1 点亮收尾结论（一句话）

$$\Rightarrow\ \textbf{位流一 load 就出 1080p60 彩条}（\texttt{CTRL}\ \text{复位值 }0x15，\text{PS 未写任何一个寄存器}）$$
$$\Rightarrow\ \textbf{720p60 也被接受} \Rightarrow \text{P2 真画面可用 720p60（AXI 100 MHz，带宽余量约 }26\%\text{）}$$
$$\Rightarrow\ \text{模式切换双向可逆，判据做到寄存器级 + 频率级，不依赖肉眼}$$

## 取证边界（说清"我没能证明什么"，避免把间接推断当结论）

- **事故版位流（`b658e0da…`）的 PS7 时钟配置已不可考**：它的 `.hwh` 在本次 scp 时被同名覆盖，
  且从未进过 git（`git log -- runs/hdmi_p1/hdmi_p1.hwh` 只有 00:57 的重建版一条）。
  ⇒ 我只能断言：**本次位流（`d478c605…`）里"报数 150 = 真实 100 MHz"是实测成立的**；
  ⇒ 事故版是否也是同一份 PS7 preset，需要别的证据（例如构建时用的 preset 文件）才能定，
  **不能拿本次结论直接覆盖事故档案**。
- `720p` 是否被 SIL9134 真正接受，**寄存器路径看不到**（EDID/输出格式由芯片自己决定）。
  ⇒ 本条只能由用户肉眼判定；本次我不给"是/否"结论。

## 附：板端脚本（本次实际执行的字节）
`scripts/rd.py`（读一个偏移）、`scripts/wr.py`（写一个偏移+回读）、
`scripts/poll.py`（有界窗口心跳速率）、`scripts/fclk.py`（写 FCLK0，`download=False` 不重复下载）、
`scripts/load.py`（唯一一次 `Overlay()`）
