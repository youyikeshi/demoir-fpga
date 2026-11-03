#!/bin/bash
# C8 软件侧验证一条龙（单进程，约 4 分钟；不碰 RTL / Vivado / 板子）
set -e
cd "$(dirname "$0")"
PY=/data/work/venv-demoir/bin/python
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1

echo "===== 第1步 布局工具：正确性 + 性能 ====="
$PY bench_layout.py

echo; echo "===== 第2步 算子适配层自测 ====="
$PY ops_c8.py

echo; echo "===== 第3步 端到端：CHW 基线 ====="
$PY run_baseline.py

echo; echo "===== 第3步 端到端：C8 路径 ====="
$PY c8_runner.py

echo; echo "===== 第3步 逐位对拍 ====="
$PY compare.py

echo; echo "===== 第4步 事务数模型 ====="
$PY txn_model.py --json txn_model_640.json

echo; echo "===== 第5步 边界例外审计 ====="
$PY exceptions_audit.py

echo; echo "===== PS 侧纯净计时（PS 开销）====="
$PY run_nohash.py base
$PY run_nohash.py c8
