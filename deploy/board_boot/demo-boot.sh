#!/bin/bash
# 开机自启：载位流 → 校 FCLK0 → 起播放器（监听 START）
# 任何一步失败都以非 0 退出，日志留在 /home/xilinx/demo/boot.log
set -u
LOG=/home/xilinx/demo/boot.log
mkdir -p /home/xilinx/demo
exec >>"$LOG" 2>&1
echo "=== $(date -Is) demo-boot 开始 ==="
echo "boot 参数: $*"
PY=/usr/local/share/pynq-venv/bin/python3
export XILINX_XRT=/usr
export BOARD_SUDO_PW=xilinx

# 0) 等 fpga_manager 就绪（最多 30 s）
for i in $(seq 1 30); do
    [ -e /sys/class/fpga_manager/fpga0/state ] && break
    sleep 1
done
if [ ! -e /sys/class/fpga_manager/fpga0/state ]; then
    echo "FATAL: /sys/class/fpga_manager/fpga0/state 30 s 内没出现"
    exit 1
fi
echo "state0=$(cat /sys/class/fpga_manager/fpga0/state)"

# 1) 载位流：不是 operating 才载，必须喂 **.bin**（.bit 会被内核拒绝）
now=$(cat /sys/class/fpga_manager/fpga0/state)
if [ "$now" != "operating" ]; then
    if [ ! -e /lib/firmware/design_1_v58.bin ]; then
        echo "FATAL: /lib/firmware/design_1_v58.bin 不存在（需要 byte-swapped bin）"
        exit 1
    fi
    echo "载位流: echo design_1_v58.bin > /sys/class/fpga_manager/fpga0/firmware"
    if ! echo design_1_v58.bin > /sys/class/fpga_manager/fpga0/firmware; then
        echo "FATAL: 写 firmware 失败"
        exit 1
    fi
    sleep 1
fi
st=$(cat /sys/class/fpga_manager/fpga0/state)
echo "state1=$st"
if [ "$st" != "operating" ]; then
    echo "FATAL: 位流没载成功（state=$st）"
    exit 1
fi

# 2) PYNQ 登记 + FCLK0 校到真值 66.67（重载后会掉成 50/177，必须校）
ts=$(date +%s)
"$PY" /home/xilinx/demo/fix_fclk.py
rc=$?
echo "fix_fclk rc=$rc  用时 $(( $(date +%s) - ts )) s"
if [ $rc -ne 0 ]; then
    echo "FATAL: fix_fclk 失败（rc=$rc）"
    exit 1
fi

# 3) 起播放器（前台 exec，systemd 管进程；重启即重新载位流+校时钟）
cd /home/xilinx/demo || exit 1
echo "=== $(date -Is) 起播放器：pairs=$PAIRS loops=$LOOPS delay=$DELAY period=$PERIOD ==="
exec "$PY" -u demo_player.py \
    --pairs "${PAIRS:-/home/xilinx/demo/pairs100}" \
    --loops "${LOOPS:-0}" \
    --start-delay "${DELAY:-1.0}" \
    --period "${PERIOD:-1.0}" \
    --log /home/xilinx/demo/listen.log \
    --pidfile /tmp/demo-player.pid
