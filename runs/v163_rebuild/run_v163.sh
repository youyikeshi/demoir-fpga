#!/usr/bin/env bash
# ============================================================================
#  runs/board_v163/run_v163.sh —— **主机侧**驱动（T-159：v57 上板验收）
#
#  一次调用 = 一个租约 = 一个进程 = **一次 load**。
#  用法（必须先删陈旧 load 令牌）：
#      rm -f /tmp/dsh_board.loadtoken
#      BOARD_ALLOW_LOAD=1 BOARD_TAG=V163v57 tools/boardlock.sh \
#          env BIT_DIR=runs/v57_archive BIT=design_1_150v57 \
#              HWH=design_1_150v57 MD5_BIT=5d1cb78969f948d45f379c7845873717 \
#              EXPECT_FCLK=100 N_MAIN=6 REPS_EXTRA=3 TAG=v163v57 \
#              bash runs/board_v163/run_v163.sh
#  对照臂（v39 @150）把 BIT_DIR/BIT/MD5_BIT/EXPECT_FCLK/N_MAIN/REPS_EXTRA 换掉即可。
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOARD="${BOARD_HOST:-xilinx@192.168.7.2}"
SUDO_PASS="${BOARD_SUDO_PASS:-xilinx}"

BIT_DIR="${BIT_DIR:-$REPO/pynq_specialized_code/board/overlay_engine_150mhz}"
BIT="${BIT:-design_1_150v39}"
HWH="${HWH:-$BIT}"
MD5_BIT="${MD5_BIT:-602725b86941864eeeeab3be005e1e81}"
MD5_HWH="${MD5_HWH:-c1686bd8e080c1c7f46181d980543202}"

PS_ROOT="${PS_ROOT:-$REPO/runs/v163_rebuild/stage}"
PROBE="${PROBE:-$REPO/runs/v163_rebuild/probe_v163.py}"
MD5_BIN_NET="${MD5_BIN_NET:-0990948de490eb6ba6d4aaefbcafa355}"
MD5_BIN_ENGINE="${MD5_BIN_ENGINE:-2fd3d0990c47151fe3bc06544b8adb64}"

FREQ_WRITE="${FREQ_WRITE:-150.0}"
EXPECT_FCLK="${EXPECT_FCLK:-150}"
CALIB="${CALIB:-150,100,66.6667}"
N_MAIN="${N_MAIN:-6}"
REPS_EXTRA="${REPS_EXTRA:-3}"
EXTRA_HW="${EXTRA_HW:-128,64}"
HW="${HW:-256}"
TAG="${TAG:-v163}"

DEST="${DEST:-/tmp/probe_V163d}"
OUT="${OUT:-/home/xilinx/demoir/v163_out_$TAG}"
LOCAL_OUT="${LOCAL_OUT:-$REPO/runs/board_v163}"

case "$BIT_DIR" in /*) ;; *) BIT_DIR="$REPO/$BIT_DIR";; esac

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no
     -o ServerAliveInterval=20 -o ServerAliveCountMax=180 "$BOARD")
SCP=(scp -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)

mkdir -p "$LOCAL_OUT"
echo "[$TAG] 位流 = $BIT_DIR/$BIT.bit  md5=$MD5_BIT"
echo "[$TAG] hwh  = $BIT_DIR/$HWH.hwh  md5=$MD5_HWH"
echo "[$TAG] hw=$HW 写频=$FREQ_WRITE 期望读回=$EXPECT_FCLK 主跑=$N_MAIN 附加=$EXTRA_HW×$REPS_EXTRA"

# ---- ⓪ 主机侧硬校验 ----
[ -f "$BIT_DIR/$BIT.bit" ] || { echo "[$TAG] ✗ 没有 $BIT_DIR/$BIT.bit"; exit 15; }
[ -f "$BIT_DIR/$HWH.hwh" ] || { echo "[$TAG] ✗ 没有 $BIT_DIR/$HWH.hwh"; exit 15; }
[ "$(md5sum "$BIT_DIR/$BIT.bit" | cut -d' ' -f1)" = "$MD5_BIT" ] \
  || { echo "[$TAG] ✗ $BIT.bit md5 不符冻结值"; exit 15; }
[ "$(md5sum "$BIT_DIR/$HWH.hwh" | cut -d' ' -f1)" = "$MD5_HWH" ] \
  || { echo "[$TAG] ✗ $HWH.hwh md5 不符冻结值"; exit 15; }
echo "[$TAG] ⓪ bit/hwh = 冻结值 ✓"

python3 -m py_compile "$PROBE" || { echo "[$TAG] ✗ 探针 py_compile 失败 ⇒ 不许上板"; exit 15; }
bash -n "$REPO/runs/v163_rebuild/board_v163.sh" \
  || { echo "[$TAG] ✗ 板端脚本语法错 ⇒ 不许上板"; exit 15; }
echo "[$TAG] ⓪ 探针 py_compile ✓ ；板端脚本 bash -n ✓"

PROBE_MD5=$(md5sum "$PROBE" | cut -d' ' -f1)
PROBE_BASE=$(basename "$PROBE")
MANIFEST="$PS_ROOT/deps_closure.txt"
[ -f "$MANIFEST" ] || { echo "[$TAG] ✗ 缺 $MANIFEST"; exit 15; }
MD5_MANIFEST=$(md5sum "$MANIFEST" | cut -d' ' -f1)
echo "[$TAG] ⓪ 探针 md5=$PROBE_MD5 ；清单 md5=$MD5_MANIFEST"

cd "$PS_ROOT" || exit 10
find ps -type f ! -path "*__pycache__*" ! -name "*.so" | LC_ALL=C sort \
    > "$LOCAL_OUT/ps_${TAG}_filelist.txt"
NF=$(wc -l < "$LOCAL_OUT/ps_${TAG}_filelist.txt")
tar czf "$LOCAL_OUT/ps_${TAG}_deploy.tgz" -T "$LOCAL_OUT/ps_${TAG}_filelist.txt" \
    || { echo "[$TAG] ✗ 打包失败"; exit 10; }
xargs -a "$LOCAL_OUT/ps_${TAG}_filelist.txt" md5sum > "$LOCAL_OUT/ps_md5_host.txt" \
    || { echo "[$TAG] ✗ md5 清单失败"; exit 10; }
echo "[$TAG] ⓪ 打包 $NF 个文件（$(du -h "$LOCAL_OUT/ps_${TAG}_deploy.tgz" | cut -f1)）"
cd "$REPO" || exit 10

# ---- ① scp ----
"${SSH[@]}" "mkdir -p '$DEST' '$OUT'" || { echo "[$TAG] ✗ 建目录失败"; exit 11; }
"${SCP[@]}" "$PROBE" "$MANIFEST" "$BIT_DIR/$BIT.bit" "$BIT_DIR/$HWH.hwh" \
            "$LOCAL_OUT/ps_${TAG}_deploy.tgz" "$LOCAL_OUT/ps_md5_host.txt" \
            "$REPO/runs/v163_rebuild/board_v163.sh" \
            "$BOARD:$DEST/" || { echo "[$TAG] ✗ scp 失败"; exit 12; }
echo "[$TAG] ① scp done"

# ---- ② 板端执行（单连接，内部一次 load）----
"${SSH[@]}" "bash -s" <<EOS
set -u
ulimit -c 0
cd "$DEST" || exit 20
export TAG="$TAG" DEST="$DEST" OUT="$OUT"
export BIT_FILE="$BIT.bit" HWH_FILE="$HWH.hwh"
export MD5_BIT="$MD5_BIT" MD5_HWH="$MD5_HWH"
export MD5_BIN_NET="$MD5_BIN_NET" MD5_BIN_ENGINE="$MD5_BIN_ENGINE"
export SUDO_PASS="$SUDO_PASS"
export FREQ_WRITE="$FREQ_WRITE" EXPECT_FCLK="$EXPECT_FCLK"
export N_MAIN="$N_MAIN" REPS_EXTRA="$REPS_EXTRA" EXTRA_HW="$EXTRA_HW"
export CALIB="$CALIB" HW="$HW"
export DUMP_OUT="${DUMP_OUT:-}"
if [ "${CONTINUE:-0}" = "1" ]; then export CONTINUE_FLAG="--continue-on-miss";    else export CONTINUE_FLAG=""; fi
export PS_TGZ="ps_${TAG}_deploy.tgz" PS_MD5_PROBE="ps_md5_host.txt"
export PROBE_BASE="$PROBE_BASE"
bash "$DEST/board_v163.sh"
EOS
RC=$?
echo "[$TAG] ② 板端 rc=$RC"

# ---- ③ 产物拉回 ----
"${SCP[@]}" -r "$BOARD:$OUT/*" "$LOCAL_OUT/" \
  && echo "[$TAG] ③ 产物已拉回 $LOCAL_OUT" \
  || echo "[$TAG] ⚠ 产物拉回失败（板端 $OUT）"

echo "[$TAG] DONE rc=$RC"
exit "$RC"
