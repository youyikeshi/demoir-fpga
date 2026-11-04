#!/bin/sh
# run_v60.sh —— 切 v60 + 跑 p0067 逐层 sha1 探针（T-166）。必须整段以 root 运行。
set -e

SP=/usr/local/share/pynq-venv/lib/python3.10/site-packages/pynq/pl_server
OUT=/tmp/probe_t166/out
mkdir -p "$OUT"

echo "=== [1] PYNQ global_pl_state_.json（缺失 ⇒ 第一句 RuntimeError: Overlay is not downloaded）"
if [ ! -f "$SP/global_pl_state_.json" ]; then
    cp -p "$SP/global_pl_state_.json.bak_rec" "$SP/global_pl_state_.json"
    echo "    已从 .bak_rec 恢复"
else
    echo "    已存在，未动"
fi
ls -l "$SP/global_pl_state_.json"

echo "=== [2] 停止演示播放器（SIGTERM 写 0x15 + 释放缓冲）"
systemctl stop demo-player || true
sleep 1
echo -n "    demo-player active? "; systemctl is-active demo-player || true

echo "=== [3] 载流前 state"
cat /sys/class/fpga_manager/fpga0/state

echo "=== [4] 校验 /lib/firmware 里的 v60 .bin 与 deploy 源一致（必须 37a163f7d32e1b1ff661e36b70561142）"
md5sum /lib/firmware/design_1_v60.bit.bin
if cmp -s /lib/firmware/design_1_v60.bit.bin \
          /home/xilinx/demoir/deploy/v60/design_1_v60.bit.bin; then
    echo "    与 deploy 逐字节相同（未覆盖）"
else
    echo "    ⚠ 不一致 ⇒ 从 deploy 覆盖一份"
    cp -f /home/xilinx/demoir/deploy/v60/design_1_v60.bit.bin /lib/firmware/
    md5sum /lib/firmware/design_1_v60.bit.bin
fi

echo "=== [5] sysfs 载 v60（用 .bit.bin，不是 .bit）"
echo design_1_v60.bit.bin > /sys/class/fpga_manager/fpga0/firmware
sleep 1
ST=$(cat /sys/class/fpga_manager/fpga0/state)
echo "    state=$ST"
[ "$ST" = "operating" ] || { echo "✗ state != operating ⇒ 中止"; exit 2; }

echo "=== [6] 探针（一次前向，只读钩子）"
export XILINX_XRT=/usr
export PYTHONPATH=/home/xilinx/demoir/deploy/v60/ps:/home/xilinx/demoir/ps/pynq
export BIN_PACK=ps BIN_RTL_ROUND=1 BIN_CONV_OOB0=1 BV_FREE=1
export BIN_THR_F32=1 BIN_LAZY_DEQ=1 BIN_CAT_FUSION=1 BIN_BILINEAR_C=1
export BIN_PRECOMP=1 Q8CACHE=1 BIN_F32DIRECT=1 BIN_CAT_ECA_FUSE=1
export BIN_EPI_NOINV=1 BIN_LAZY_SLOTS=24
export PYTHONIOENCODING=utf-8
cd /tmp/probe_t166
/usr/local/share/pynq-venv/bin/python3 -u probe12.py \
    --input /home/xilinx/demo/frozen256/p0067_crop256.npy \
    --pack  /home/xilinx/demoir/deploy/v60/pack_clean \
    --bit   /home/xilinx/demoir/deploy/v60/design_1_v60.bit \
    --outdir "$OUT" 2>&1 | tee "$OUT/probe.log"

echo "=== [7] 结束 state"
cat /sys/class/fpga_manager/fpga0/state
