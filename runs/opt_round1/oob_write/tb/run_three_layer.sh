#!/usr/bin/env bash
# ============================================================================
#  run_three_layer.sh —— 本轮「3 层金丝雀」批量驱动（一次 xelab，多次 xsim）
#
#  为什么单独写：run_unit.sh / run_wrap.sh 每次都要重编译重精化（~1.5 min），
#  本轮要跑 3 个反压档 + 变体表，重编译的固定开销会吃掉三分之一时间盒。
#  本脚本**不改动**那两个脚本（它们仍可一键复跑，上一轮 36+12 形状不受影响），
#  只是把"编译一次、跑多次"的做法固化下来。
#
#  用法（必须经 memgate 持锁）：
#    cd pynq_specialized_code
#    bash tools/memgate.sh 4 -- bash ../runs/opt_round1/oob_write/tb/run_three_layer.sh [phase...]
#        phase ∈ unit3l wrap3l-fast wrap3l-wslow wrap3l-rslow unitvar wrapvar unit256
#        默认全跑
# ============================================================================
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK_W="${WORK_W:-$HERE/../sim/work_wrap}"
WORK_U="${WORK_U:-$HERE/../sim/work}"
LOG="${LOG:-$HERE/../sim/logs}"
mkdir -p "$WORK_W" "$WORK_U" "$LOG"

set +u
export PATH=/data/xilinx/2025.2/2025.2.1/Vivado/bin:$PATH
source /data/xilinx/2025.2/2025.2.1/Vivado/settings64.sh >/dev/null 2>&1 || true
set -u

PHASES=("$@")
if [ ${#PHASES[@]} -eq 0 ]; then
    PHASES=(unit3l wrap3l-fast wrap3l-wslow wrap3l-rslow unitvar wrapvar unit256)
fi

want() { for p in "${PHASES[@]}"; do [ "$p" = "$1" ] && return 0; done; return 1; }

# ---------------- 包装层：编译一次 ----------------
if want unit3l || want unitvar || want unit256; then :; fi
if want wrap3l-fast || want wrap3l-wslow || want wrap3l-rslow || want wrapvar || want lasthold; then
    echo "=== [wrap] xvlog/xelab ==="
    cd "$WORK_W" || exit 1
    xvlog --sv \
      "$HERE/bin_conv_core8.v" "$HERE/bin_conv_core.v" \
      "$HERE/bin_e1e2.v" "$HERE/bin_catpack.v" \
      "$HERE/bin_rprelu.v" "$HERE/bin_sam.v" "$HERE/bin_interp.v" \
      "$HERE/bin_unit_axi_wrap.v" "$HERE/tb_oob_wrap.v" \
      > "$LOG/three_layer_wrap_xvlog.log" 2>&1 \
      || { echo "xvlog 失败"; tail -25 "$LOG/three_layer_wrap_xvlog.log"; exit 1; }
    xelab --mt 4 --timescale 1ns/1ps -debug off --relax tb_oob_wrap -s snap \
      > "$LOG/three_layer_wrap_xelab.log" 2>&1 \
      || { echo "xelab 失败"; tail -25 "$LOG/three_layer_wrap_xelab.log"; exit 1; }

    # 3 层：cin=256 那层在板上实测 ~10.7M 拍 ⇒ MAXCYC/WATCH 都要放大
    BIG="-testplusarg MAXCYC=60000000 -testplusarg WATCH=60000000"
    if want wrap3l-fast; then
        echo "=== [wrap] 3 层 / 快从机 ==="
        xsim snap -R $BIG -testplusarg "SWEEP=4" \
          > "$LOG/three_layer_wrap_3l_fast.log" 2>&1
        grep -E "OOBW|WRAPSUMMARY|WRAPRESULT" "$LOG/three_layer_wrap_3l_fast.log"
    fi
    if want wrap3l-wslow; then
        echo "=== [wrap] 3 层 / 慢 DDR 写反压 (WSLOW) ==="
        xsim snap -R $BIG -testplusarg "SWEEP=4" -testplusarg "WSLOW=1" \
          > "$LOG/three_layer_wrap_3l_wslow.log" 2>&1
        grep -E "OOBW|WRAPSUMMARY|WRAPRESULT" "$LOG/three_layer_wrap_3l_wslow.log"
    fi
    if want wrap3l-rslow; then
        echo "=== [wrap] 3 层 / 慢 DDR 读反压 (RSLOW) ==="
        xsim snap -R $BIG -testplusarg "SWEEP=4" -testplusarg "RSLOW=1" \
          > "$LOG/three_layer_wrap_3l_rslow.log" 2>&1
        grep -E "OOBW|WRAPSUMMARY|WRAPRESULT" "$LOG/three_layer_wrap_3l_rslow.log"
    fi
    if want lasthold; then
        echo "=== [wrap] 3 层 / 最后一拍定点冻结 +LASTHOLD=64 ==="
        xsim snap -R $BIG -testplusarg "SWEEP=6" \
          > "$LOG/three_layer_wrap_3l_lasthold.log" 2>&1
        grep -E "OOBW|WRAPSUMMARY|WRAPRESULT" "$LOG/three_layer_wrap_3l_lasthold.log"
    fi
    if want wrapvar; then
        echo "=== [wrap] cout×Hout 变体网格 / 快从机 ==="
        xsim snap -R $BIG -testplusarg "SWEEP=5" \
          > "$LOG/three_layer_wrap_var_fast.log" 2>&1
        grep -E "OOBW|WRAPSUMMARY|WRAPRESULT" "$LOG/three_layer_wrap_var_fast.log"
    fi
fi

# ---------------- 单元层：编译一次 ----------------
if want unit3l || want unitvar || want unit256; then
    echo "=== [unit] xvlog/xelab ==="
    cd "$WORK_U" || exit 1
    xvlog --sv "$HERE/bin_conv_core8.v" "$HERE/tb_oob_conv8.v" \
      > "$LOG/three_layer_unit_xvlog.log" 2>&1 \
      || { echo "xvlog 失败"; tail -20 "$LOG/three_layer_unit_xvlog.log"; exit 1; }
    xelab --mt 4 --timescale 1ns/1ps -debug off --relax tb_oob_conv8 -s snap \
      > "$LOG/three_layer_unit_xelab.log" 2>&1 \
      || { echo "xelab 失败"; tail -20 "$LOG/three_layer_unit_xelab.log"; exit 1; }
    if want unit3l; then
        echo "=== [unit] 3 层 ==="
        xsim snap -R -testplusarg "SWEEP=1" -testplusarg "SHAPESET=1" \
          > "$LOG/three_layer_unit_3l.log" 2>&1
        grep -E "OOBREP|SUMMARY|RESULT" "$LOG/three_layer_unit_3l.log"
    fi
    if want unitvar; then
        echo "=== [unit] cout×Hout 变体网格 ==="
        xsim snap -R -testplusarg "SWEEP=1" -testplusarg "SHAPESET=2" \
          > "$LOG/three_layer_unit_var.log" 2>&1
        grep -E "OOBREP|SUMMARY|RESULT" "$LOG/three_layer_unit_var.log"
    fi
    if want unit256; then
        echo "=== [unit] 同权重 256² 对照（out_bytes=6291456） ==="
        xsim snap -R -testplusarg "SWEEP=0" -testplusarg "COUT=96" -testplusarg "CIN=64" \
          -testplusarg "K=3" -testplusarg "S=1" -testplusarg "D=1" -testplusarg "PAD=1" \
          -testplusarg "H=256" -testplusarg "W=256" -testplusarg "SHIFT=6" \
          > "$LOG/three_layer_unit_256.log" 2>&1
        grep -E "OOBREP|SUMMARY|RESULT" "$LOG/three_layer_unit_256.log"
    fi
fi
echo "=== run_three_layer.sh 全部完成 ==="
