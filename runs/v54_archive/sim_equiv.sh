#!/bin/bash
# ============================================================================
#  T-123：包装层写通路 **逐笔等价** 仿真（v53 RTL vs v54 RTL）
#  复用 runs/v94_wrpath/tb_v94_wrap.v（T-94/T-105 用过的同一份 TB），
#  两次编译只有 RTL 根不同（主线 v53 / worktree v54），比较 T94LOG 的逐笔
#  (addr,data,sel) 序列 —— 证明 T-123 的两处改动**没有改变任何一拍的行为**。
#
#  用法： ./sim_equiv.sh <VLANES> <shape> [shape...]
#  产物： worktrees/T-123-timing/sim/{v53,v54}/wr_<shape>_v<V>.txt
# ============================================================================
set -u
VIV=/data/xilinx/2025.2/2025.2.1/Vivado/bin
REPO=/media/felix/Data/coding/demoir-fpga
HERE=$REPO/worktrees/T-123-timing
TB=$REPO/runs/v94_wrpath/tb_v94_wrap.v
V=${1:?用法: sim_equiv.sh <VLANES> <shape>...}; shift
SHAPES=("$@")
[ ${#SHAPES[@]} -eq 0 ] && { echo "缺 shape"; exit 2; }

compile_one () {          # $1 = tag(v53|v54)  $2 = RTL 根
    local TAG=$1 RTL=$2 D=$HERE/sim/$1
    mkdir -p "$D"; cd "$D" || exit 2
    rm -rf xsim.dir xvlog.pb xelab.pb
    $VIV/xvlog --sv -L xpm -d TB_UNIT=0 -d V94_VLANES=$V -d V94_P2PROBE \
        "$RTL"/*.v "$RTL"/lane16/*.v "$TB" > xvlog.log 2>&1
    local rc=$?
    echo "[$TAG] xvlog rc=$rc ERROR=$(grep -c ERROR xvlog.log)"
    [ $rc -ne 0 ] && { tail -20 xvlog.log; return $rc; }
    $VIV/xelab -debug off --relax -L xpm tb_bin_unit_axi_wrap -s snap > xelab.log 2>&1
    rc=$?
    echo "[$TAG] xelab rc=$rc"
    [ $rc -ne 0 ] && { grep -E "ERROR" xelab.log | head -10; return $rc; }
    return 0
}

run_one () {              # $1 = tag  $2 = shape
    local TAG=$1 SH=$2 D=$HERE/sim/$1
    local GOLD=$REPO/runs/v94_wrpath/golden
    [ -d "$HERE/sim/golden/$SH" ] && GOLD=$HERE/sim/golden
    cd "$D" || return 2
    local P=()
    for kv in CIN COUT KK SS DD PAD HH WW SHIFT HO WO; do
        v=$(grep -oE "\b$kv=[0-9]+" "$GOLD/$SH/shape.env" 2>/dev/null | head -1 | cut -d= -f2)
        [ -z "${v:-}" ] && continue
        case "$kv" in
            HH) P+=(-testplusarg "H=$v") ;; WW) P+=(-testplusarg "W=$v") ;;
            KK) P+=(-testplusarg "K=$v") ;; SS) P+=(-testplusarg "S=$v") ;;
            DD) P+=(-testplusarg "D=$v") ;; *)  P+=(-testplusarg "$kv=$v") ;;
        esac
    done
    $VIV/xsim snap -R -log "xsim_${SH}_$TAG.log" \
        -testplusarg "GOLDEN=$GOLD/$SH" -testplusarg "SHAPE_NAME=$SH" \
        -testplusarg NOSTOP=1 -testplusarg T94FILL=1 \
        -testplusarg "T94LOG=wr_${SH}_$TAG.txt" "${P[@]}" > "run_${SH}_$TAG.log" 2>&1
    local rc=$?
    local n=0; [ -f "wr_${SH}_$TAG.txt" ] && n=$(wc -l < "wr_${SH}_$TAG.txt")
    echo "[$TAG] $SH rc=$rc beats=$n"
    return $rc
}

echo "==== 编译两份 RTL（只有 RTL 根不同）===="
compile_one v53 "$REPO/runs/timing_v50/rtl_v50" || exit 1
compile_one v54 "$HERE/rtl_v50"                 || exit 1

echo "==== 逐形状跑 + 逐笔比对 ===="
BAD=0
for SH in "${SHAPES[@]}"; do
    run_one v53 "$SH"; run_one v54 "$SH"
    A="$HERE/sim/v53/wr_${SH}_v53.txt"; B="$HERE/sim/v54/wr_${SH}_v54.txt"
    if [ ! -f "$A" ] || [ ! -f "$B" ]; then echo "[$SH] ★ 缺逐笔日志"; BAD=$((BAD+1)); continue; fi
    if diff -q "$A" "$B" > /dev/null; then
        echo "[$SH] ✓ 逐笔完全一致（$(wc -l < "$A") 笔）"
    else
        echo "[$SH] ✗ 有差异："; diff "$A" "$B" | head -10; BAD=$((BAD+1))
    fi
done
echo "==== 结论：差异形状数 = $BAD ===="
[ $BAD -eq 0 ] && echo "RESULT: PASS（T-123 改动逐笔等价）" || echo "RESULT: FAIL"
exit $BAD
