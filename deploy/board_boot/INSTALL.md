# 板端开机自启（demo-player）

把"载位流 → 校 FCLK0 → 起播放器（监听 START）"做成系统的开机自启。
**这三个文件是板上实际运行的版本**（2026-11-03 冷启动验收通过，89 s 就绪）。

| 文件 | 板上位置 | 说明 |
|---|---|---|
| `demo-boot.sh` | `/usr/local/sbin/demo-boot.sh` | 启动脚本：等 fpga_manager → 载 `.bin` → 跑 `fix_fclk.py` → `exec` 播放器 |
| `fix_fclk.py` | `/home/xilinx/demo/fix_fclk.py` | 幂等：`Overlay(download=False)` 登记 + 把 FCLK0 校到真值 66.67 MHz |
| `demo-player.service` | `/etc/systemd/system/demo-player.service` | `User=root`、`WantedBy=multi-user.target`、`Restart=no` |

## 前置（板子上必须已有）

```
/lib/firmware/design_1_v58.bin          # byte-swapped bin（不是 .bit！）
/home/xilinx/demo/design_1_v58.bit/.hwh # 给 PYNQ 解析硬件
/home/xilinx/demo/demo_player.py        # 播放器（薄壳，只调 minimal_proof）
/home/xilinx/demo/minimal_proof.py      # 能出图的 bringup/show + PL 硬门禁
/home/xilinx/demo/pairs100/             # 100 对（manifest.json + pairs/）
```

## 安装（两行）

```sh
sudo install -m 0755 demo-boot.sh      /usr/local/sbin/demo-boot.sh
sudo install -m 0755 fix_fclk.py       /home/xilinx/demo/fix_fclk.py
sudo install -m 0644 demo-player.service /etc/systemd/system/demo-player.service
sudo systemctl daemon-reload && sudo systemctl enable --now demo-player
```

## 验证（四条判据）

```sh
systemctl is-enabled demo-player      # enabled
systemctl is-active  demo-player      # active
cat /sys/class/fpga_manager/fpga0/state          # operating
ss -ltn | grep 9999                              # 0.0.0.0:9999 LISTEN
tail -40 /home/xilinx/demo/boot.log              # 看到 "就绪：等 START"
```

FCLK0 真值必须是 66.67（boot.log 里 `fix_fclk` 那段会打印 `FPGA0_CLK_CTRL / DIV0+1 / 真值`）：
报数 100 = 真值 66.67；报数 75 = 真值 50（会黑屏，每行都读空）。

**冷启动实测时间线**：位流 ≈15 s → `fix_fclk` ≈45 s → `bringup` + 监听 ≈**89 s**
（暖启动 / 手动 `systemctl start` ≈35 s；冷启动慢在 100 对预载 23.6 s）

## 换图对（不改代码）

```sh
sudo systemctl edit demo-player
# [Service]
# Environment=PAIRS=/home/xilinx/demo/pairsXXX
```
`LOOPS`（0=无限等待触发）、`DELAY`（START 后延迟）、`PERIOD`（帧间隔）同理。

## 卸载

```sh
sudo systemctl disable --now demo-player
sudo rm -f /etc/systemd/system/demo-player.service \
           /usr/local/sbin/demo-boot.sh \
           /home/xilinx/demo/fix_fclk.py
sudo systemctl daemon-reload
```

## 黑屏急救（按顺序，别反复重启）

1. `sudo systemctl restart demo-player` —— 会**重载位流**，唯一能修"扫描 FSM 死"的办法
2. 还黑：`tail -40 /home/xilinx/demo/boot.log` 看卡在哪一步
3. 仍不行：`sudo reboot`（一次性），等 ~90 s

> 判别口令：**"黑屏但 `0x2C`=0 且 `nonblack` 高" ⇒ 扫描 FSM 死了 ⇒ 只能重载位流。**
> 详见 `runs/demo_pairs/RECORD_RUNBOOK.md` §6/§7。
