#!/bin/sh
# run_v58.sh —— 收尾：回 v58 演示态 + 播放态验收（必须整段以 root 运行）
set -e

SP=/usr/local/share/pynq-venv/lib/python3.10/site-packages/pynq/pl_server
PYB=/usr/local/share/pynq-venv/bin/python3
OUT=/tmp/probe_t166/out

echo "=== [0] f32 canary（纯 PS，不碰 PL；与主机同脚本比 sha1）"
$PYB -u /tmp/probe_t166/canary_f32.py || true

echo "=== [1] PYNQ 状态文件（Overlay(download=False) 会删它；恢复以便下次开跑）"
[ -f "$SP/global_pl_state_.json" ] || cp -p "$SP/global_pl_state_.json.bak_rec" "$SP/global_pl_state_.json"
ls -l "$SP/global_pl_state_.json"

echo "=== [2] 停播放器（此刻已停，幂等）"
systemctl stop demo-player || true
sleep 1
echo -n "    active? "; systemctl is-active demo-player || true

echo "=== [3] 载流前 state"
cat /sys/class/fpga_manager/fpga0/state

echo "=== [4] 载 v58（demo-boot 用的同一份 .bin）"
md5sum /lib/firmware/design_1_v58.bin
echo design_1_v58.bin > /sys/class/fpga_manager/fpga0/firmware
sleep 1
ST=$(cat /sys/class/fpga_manager/fpga0/state)
echo "    state=$ST"
[ "$ST" = "operating" ] || { echo "✗ state != operating ⇒ 中止"; exit 2; }

echo "=== [5] start 播放器（它的 bringup 是这块 PL 的第一次武装）"
systemctl start demo-player
for i in $(seq 1 90); do
    if (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -q ':9999'; then
        echo "    9999 已监听（等待 ${i}s）"; break
    fi
    sleep 1
done
(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep ':9999' || echo "    !! 9999 未监听"

echo "=== [6] 寄存器基线（空闲态 = 彩条 CTRL=0x15，不读 FB）"
/usr/bin/python3 /tmp/probe_t166/regs.py || true

echo "=== [7] SLCR 记账（FCLK0 报数应 100 ⇒ 真值 66.67；FCLK1 分频域）"
/usr/bin/python3 - <<'PY'
import ctypes, mmap, os
fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0xF8000000)
for nm, off in (("FCLK0", 0x170), ("FCLK1", 0x174)):
    raw = ctypes.c_uint32.from_buffer(m, off).value
    div = ((raw >> 8) & 0x3F) + 1
    print("[clk] %s SLCR 0x%03X = 0x%08X  DIV0+1=%d  ⇒ 真值 %.2f MHz"
          % (nm, off, raw, div, (3200.0 / 3.0) / div))
m.close(); os.close(fd)
PY
$PYB -c "
from pynq import Overlay, Clocks
Overlay('/home/xilinx/demo/design_1_v58.bit', download=False)
print('[clk] PYNQ 报数 FCLK0=%g FCLK1=%g' % (float(Clocks.fclk0_mhz), float(Clocks.fclk1_mhz)))
" || true

echo "=== [8] demo-boot.sh 摘要（载流路径核对）"
grep -n "firmware\|design_1_v58\|fix_fclk" /usr/local/sbin/demo-boot.sh 2>/dev/null | head -12 || true

echo "=== [9] sync"
sync
echo "    rc=$?"
echo "=== [10] 结束 state"
cat /sys/class/fpga_manager/fpga0/state
