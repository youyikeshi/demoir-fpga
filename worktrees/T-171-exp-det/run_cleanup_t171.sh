#!/bin/sh
# run_cleanup_t171.sh —— T-171 收尾前半段：回 v58 演示态 + 起播放器（root 运行）
set -e
SP=/usr/local/share/pynq-venv/lib/python3.10/site-packages/pynq/pl_server
echo "=== [1] PYNQ 状态文件"
[ -f "$SP/global_pl_state_.json" ] || { cp -p "$SP/global_pl_state_.json.bak_rec" "$SP/global_pl_state_.json"; echo "    已恢复"; }
echo "=== [2] 停播放器（幂等）"
systemctl stop demo-player || true
sleep 1
echo "=== [3] 载流前 state"; cat /sys/class/fpga_manager/fpga0/state
echo "=== [4] 载 v58（期望 md5 65b4f88463a00d76ba19319b8ebe5a3d）"
md5sum /lib/firmware/design_1_v58.bin
echo design_1_v58.bin > /sys/class/fpga_manager/fpga0/firmware
sleep 1
ST=$(cat /sys/class/fpga_manager/fpga0/state); echo "    state=$ST"
[ "$ST" = "operating" ] || { echo "✗ state != operating ⇒ 中止"; exit 2; }
echo "=== [5] start 播放器 + 等 9999"
systemctl start demo-player
for i in $(seq 1 100); do
    if (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -q ':9999'; then
        echo "    9999 已监听（等待 ${i}s）"; break
    fi
    sleep 1
done
(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep ':9999' || echo "    !! 9999 未监听"
echo "=== [6] 空闲态寄存器（CTRL 应为 0x15 彩条）"
/usr/bin/python3 /tmp/probe_t171/regs.py || true
echo "=== [7] 时钟（PYNQ 报数；SLCR 0x174 恒读 0，按报告口径不记账）"
/usr/local/share/pynq-venv/bin/python3 -c "
from pynq import Overlay, Clocks
Overlay('/home/xilinx/demo/design_1_v58.bit', download=False)
print('[clk] PYNQ 报数 FCLK0=%g FCLK1=%g' % (float(Clocks.fclk0_mhz), float(Clocks.fclk1_mhz)))
" 2>&1 | tail -3
echo "=== [8] fix_fclk 结果（boot.log 尾部）"; tail -6 /home/xilinx/demo/boot.log 2>/dev/null || true
echo "=== [9] sync"; sync; echo "    rc=$?"
echo "=== [10] state"; cat /sys/class/fpga_manager/fpga0/state
