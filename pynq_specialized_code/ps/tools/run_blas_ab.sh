#!/bin/sh
# 等 OpenBLAS 编译结束 -> 旧 BLAS 存参考 -> LD_PRELOAD 新 BLAS 对比
# 全部写到 /home/xilinx/blas_ab.log
set -x
LOG=/home/xilinx/blas_ab.log
: > $LOG

# 1) 等编译结束（没有 make 进程，且 .so 出现）
i=0
while [ $i -lt 360 ]; do
    if [ -f /home/xilinx/obl/libopenblas.so.0 ] && ! pgrep -f "make -j2 TARGET=ARMV7" >/dev/null 2>&1; then
        break
    fi
    sleep 20
    i=$((i+1))
done
sleep 5
echo "=== BUILD DONE check ===" >> $LOG
ls -la /home/xilinx/obl/libopenblas.so* >> $LOG 2>&1
tail -3 /home/xilinx/obl/build.log >> $LOG

cd /home/xilinx/eca_ab || exit 1

echo "=== 0) 空闲确认 ===" >> $LOG
uptime >> $LOG

echo "=== 1) 旧 BLAS (ATLAS) 基线 + 存参考 ===" >> $LOG
nice -n 5 python3 bench_blas_ab.py --save /home/xilinx/ref_old.npy >> $LOG 2>&1

echo "=== 2) 新 BLAS 单线程 (LD_PRELOAD) ===" >> $LOG
LD_PRELOAD=/home/xilinx/obl/libopenblas.so.0 nice -n 5 python3 bench_blas_ab.py \
    --compare /home/xilinx/ref_old.npy --threads 1 >> $LOG 2>&1

echo "=== 3) 新 BLAS 双线程 (OPENBLAS_NUM_THREADS=2) ===" >> $LOG
LD_PRELOAD=/home/xilinx/obl/libopenblas.so.0 nice -n 5 python3 bench_blas_ab.py \
    --compare /home/xilinx/ref_old.npy --threads 2 >> $LOG 2>&1

echo "=== 4) 与参考再比一次：确认 sgemm 真的换掉了 (LD_DEBUG) ===" >> $LOG
LD_PRELOAD=/home/xilinx/obl/libopenblas.so.0 LD_DEBUG=bindings python3 -c "
import numpy as np; a=np.ones((2,3),np.float32); b=np.ones((3,2),np.float32); print((a@b).sum())" \
    2>&1 | grep -i "sgemm" | head -5 >> $LOG

echo "=== ALL DONE ===" >> $LOG
