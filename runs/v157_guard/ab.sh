#!/bin/bash
# T-157：PF_MAX 闸门补丁的 A/B 功能等价性测试
#   用法： ./ab.sh <A|B> <8|16> <shape...>
#     A = 补丁前（rtlA，wrap md5=17164dfc...）
#     B = 补丁后（rtlB，wrap md5=f376f47d...）
#   每个 (side,V) 一个独立 xsim 目录：ab_<side>_v<V>
set -u
VIV=/data/xilinx/2025.2/2025.2.1/Vivado/bin
REPO=/media/felix/Data/coding/demoir-fpga
HERE=$(cd "$(dirname "$0")" && pwd)
GOLD=$REPO/runs/v94_wrpath/golden
TB=$REPO/runs/v94_wrpath/tb_v94_wrap.v

SIDE=${1:?A|B}; V=${2:?8|16}; shift 2
[ "${1:-}" != "" ] || { echo "需要一个 shape"; exit 64; }

case "$SIDE" in
  A) RTL=$HERE/rtlA ;;
  B) RTL=$HERE/rtlB ;;
  *) echo "side 必须是 A 或 B"; exit 64 ;;
esac

WD=$HERE/ab_${SIDE}_v${V}
mkdir -p "$WD"; cd "$WD" || exit 2

# ---------- 编译（每个 (side,V) 只做一次）----------
if [ ! -f xelab.ok ]; then
  rm -rf xsim.dir xvlog.pb xelab.pb xelab.ok
  "$VIV/xvlog" --sv -L xpm -d TB_UNIT=0 -d V94_VLANES=$V -d V94_P2PROBE \
      "$RTL"/*.v "$RTL"/lane16/*.v "$TB" > xvlog.log 2>&1
  rc=$?
  echo "[$SIDE v$V] xvlog rc=$rc ERROR=$(grep -c ERROR xvlog.log)"
  [ $rc -eq 0 ] || { tail -25 xvlog.log; exit $rc; }
  "$VIV/xelab" -debug off --relax -L xpm tb_bin_unit_axi_wrap -s snap > xelab.log 2>&1
  rc=$?
  echo "[$SIDE v$V] xelab rc=$rc"
  [ $rc -eq 0 ] || { grep ERROR xelab.log | head -15; exit $rc; }
  touch xelab.ok
else
  echo "[$SIDE v$V] 复用已有 snap"
fi

# ---------- 逐形状跑 ----------
for SH in "$@"; do
  tag="${SIDE}v${V}"
  [ -d "$GOLD/$SH" ] || { echo "缺 golden: $SH"; exit 2; }
  P=()
  for kv in CIN COUT KK SS DD PAD HH WW SHIFT HO WO; do
    v=$(grep -oE "\b$kv=[0-9]+" "$GOLD/$SH/shape.env" 2>/dev/null | head -1 | cut -d= -f2)
    [ -z "${v:-}" ] && continue
    case "$kv" in
      HH) P+=(-testplusarg "H=$v") ;;
      WW) P+=(-testplusarg "W=$v") ;;
      KK) P+=(-testplusarg "K=$v") ;;
      SS) P+=(-testplusarg "S=$v") ;;
      DD) P+=(-testplusarg "D=$v") ;;
      *)  P+=(-testplusarg "$kv=$v") ;;
    esac
  done
  "$VIV/xsim" snap -R -log "xsim_${SH}_${tag}.log" \
    -testplusarg "GOLDEN=$GOLD/$SH" -testplusarg "SHAPE_NAME=$SH" \
    -testplusarg NOSTOP=1 -testplusarg T94FILL=1 \
    -testplusarg "T94LOG=wr_${SH}_${tag}.txt" \
    "${P[@]}" > "run_${SH}_${tag}.log" 2>&1
  echo "[$SIDE v$V] $SH xsim rc=$? $(grep -oE 'status=[A-Z]+' "run_${SH}_${tag}.log" | tail -1) $(grep -oE 'cycles=[0-9]+' "run_${SH}_${tag}.log" | tail -1)"
done
