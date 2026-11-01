#!/usr/bin/env bash
# 一次 load 跑完全部实验（host 侧）：板锁 + 唯一 load 令牌 + 单进程
set -u
cd "$(dirname "$0")"
echo "[once] 部署探针到板子..."
scp -q /tmp/plog/bin_net.py /tmp/plog/bin_engine.py /tmp/plog/board_v39_verify.py /tmp/plog/one_load.py \
    xilinx@192.168.7.2:/tmp/plog/ || { echo "scp 失败（板子不可达？）"; exit 2; }
BOARD_ALLOW_LOAD=1 BOARD_TAG=one_load tools/boardlock.sh \
  ssh xilinx@192.168.7.2 "cd /tmp/plog && echo xilinx | sudo -S -E -p '' \
    env PYTHONPATH=/tmp/plog:/home/xilinx/demoir/ps/pynq \
    /usr/local/share/pynq-venv/bin/python3 -u one_load.py" || exit $?
scp -q xilinx@192.168.7.2:/tmp/plog/one_load.json /tmp/plog/ol_*.csv runs/board_v39/ 2>/dev/null || true
echo "[once] 结果已拉到 runs/board_v39/"
