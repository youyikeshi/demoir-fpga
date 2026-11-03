#!/usr/bin/env bash
# ============================================================================
#  board_v163.sh —— **板端**执行体（T-159：v57 上板验收）
#  由 run_v163.sh 用 scp 送到板上再执行；也可单独调试。
#  必需环境（host 侧用 ssh 传）：TAG DEST OUT BIT_FILE HWH_FILE MD5_BIT MD5_HWH
#                                MD5_BIN_NET MD5_BIN_ENGINE SUDO_PASS
#                                FREQ_WRITE EXPECT_FCLK N_MAIN REPS_EXTRA EXTRA_HW CALIB
#                                PS_TGZ PS_MD5_PROBE PROBE_BASE DEPLOY_ROOT
#  铁律：只按 pid kill -TERM；禁 pkill -f；ulimit -c 0；收尾只 sync; sync。
# ============================================================================
set -uo pipefail
ulimit -c 0 2>/dev/null || true
: "${TAG:=v163}"
: "${DEST:?}" ; : "${OUT:?}"
: "${BIT_FILE:?}" ; : "${HWH_FILE:?}"
: "${MD5_BIT:?}" ; : "${MD5_HWH:?}"
: "${SUDO_PASS:=xilinx}"
: "${FREQ_WRITE:=150.0}" ; : "${EXPECT_FCLK:=}"
: "${N_MAIN:=6}" ; : "${REPS_EXTRA:=3}" ; : "${EXTRA_HW:=128,64}"
: "${CALIB:=150,100,66.6667}"
: "${PS_TGZ:?}" ; : "${PS_MD5_PROBE:?}" ; : "${PROBE_BASE:?}"
: "${MD5_BIN_NET:=}" ; : "${MD5_BIN_ENGINE:=}"
: "${DUMP_OUT:=}" ; : "${CONTINUE:=0}"

say() { echo "[$TAG] $*"; }
die() { echo "[$TAG] ✗✗ $*"; exit "${2:-90}"; }

mkdir -p "$OUT" || die "建 $OUT 失败" 91
cd /home/xilinx/demoir || die "没有 /home/xilinx/demoir" 91

say "=== ① 板端体检 ==="
uptime
df -h / | tail -1 ; df -i / | tail -1
grep -E '^(MemTotal|MemFree|MemAvailable|CmaTotal|CmaFree|SwapTotal|SwapFree)' /proc/meminfo

say "--- 按 pid 杀旧探针（禁 pkill -f）---"
CUR=$$ ; PAR=$PPID
for p in $(pgrep -x python3 2>/dev/null || true); do
    [ "$p" = "$CUR" ] && continue
    [ "$p" = "$PAR" ] && continue
    c=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null || true)
    case "$c" in
      *jupyter*|*redirect_server*|*networkd*|*unattended*|*'pynq.pl_server'*) continue;;
    esac
    case "$c" in
      *probe_*|*bin_net*|*board_*|*t1[0-9][0-9]_*)
        echo "  kill -TERM $p : $c"; kill -TERM "$p" 2>/dev/null || true;;
    esac
done
sleep 2
say "--- 杀后残留 ---"
pgrep -a -x python3 2>/dev/null | grep -E 'probe_|bin_net|board_' || echo "  (无)"

say "--- 现有位流 + PACK ---"
ls -l /home/xilinx/demoir/design_1_150v*.bit 2>/dev/null | tail -6 || true
[ -d /home/xilinx/demoir/deploy/bin_weights_new ] || die "缺 PACK 目录" 92
md5sum /home/xilinx/demoir/deploy/bin_weights_new/index.json 2>/dev/null | head -1 || true

PY0=""
for p in /usr/local/share/pynq-venv/bin/python3 /usr/share/pynq-venv/bin/python3; do
    [ -x "$p" ] && { PY0="$p"; break; }
done
[ -n "$PY0" ] || die "找不到真 python（pynq venv）" 93
say "真 python = $PY0"

say "=== ② 位流落位 + md5（成对）==="
cp -f "$DEST/$BIT_FILE" /home/xilinx/demoir/${BIT_ONBOARD:-design_1_150v163}.bit || die "拷 bit 失败" 94
cp -f "$DEST/$HWH_FILE" /home/xilinx/demoir/${BIT_ONBOARD:-design_1_150v163}.hwh || die "拷 hwh 失败" 94
for pair in "${BIT_ONBOARD:-design_1_150v163}.bit:$MD5_BIT" "${BIT_ONBOARD:-design_1_150v163}.hwh:$MD5_HWH"; do
    f=${pair%%:*}; want=${pair##*:}
    got=$(md5sum "/home/xilinx/demoir/$f" | awk '{print $1}')
    echo "  $f = $got （期望 $want）"
    [ "$got" = "$want" ] || die "$f md5 不符 ⇒ 停" 94
done
say "✓ bit/hwh = 冻结值"

say "=== ③ ps 源码部署 + 逐文件 md5 ==="
tar xzf "$DEST/$PS_TGZ" || die "解包失败" 95
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' rm -rf ps/pynq/__pycache__ ps/tools/__pycache__ 2>/dev/null || true
NF=$(find ps -type f ! -path '*__pycache__*' | wc -l)
say "解包完成（$NF 个文件）"
if md5sum -c "$DEST/$PS_MD5_PROBE" > "$OUT/ps_md5_board_check.txt" 2>&1; then
    say "✓ md5 -c 全过：$(grep -c ': OK' "$OUT/ps_md5_board_check.txt") 个文件"
else
    grep ': FAILED' "$OUT/ps_md5_board_check.txt" | head -20
    die "md5 -c 失败" 95
fi
for pair in "ps/pynq/bin_net.py:$MD5_BIN_NET" "ps/pynq/bin_engine.py:$MD5_BIN_ENGINE"; do
    f=${pair%%:*}; want=${pair##*:}
    [ -n "$want" ] || continue
    got=$(md5sum "$f" | awk '{print $1}')
    echo "  单变量锚 $f = $got （期望 $want）"
    [ "$got" = "$want" ] || die "$f md5 不符 ⇒ 不许拿错部署做实验" 96
done

say "=== ④ 依赖闭包（ps/ 与清单一起同步）==="
MPRIM=/home/xilinx/runs/board_ops/20261102/deps_closure.txt
mkdir -p /home/xilinx/runs/board_ops/20261102 runs/board_ops/20261102
cp -f "$DEST/deps_closure.txt" "$MPRIM" || die "拷清单失败" 97
cp -f "$DEST/deps_closure.txt" runs/board_ops/20261102/deps_closure.txt
M=$(md5sum "$MPRIM" | awk '{print $1}')
M2=$(md5sum runs/board_ops/20261102/deps_closure.txt | awk '{print $1}')
echo "  主清单 md5=$M ；镜像 md5=$M2"
[ "$M" = "$M2" ] || die "清单两份 md5 不一致" 97
( cd ps/pynq && md5sum -c "$MPRIM" > "$OUT/ps_deps_md5sum_c.txt" 2>&1 ) \
    || { tail -20 "$OUT/ps_deps_md5sum_c.txt"; die "cd ps/pynq && md5sum -c 失败" 97; }
say "  判据① md5sum -c rc=0（一致 $(grep -c ': OK' "$OUT/ps_deps_md5sum_c.txt") 个）"
"$PY0" ps/tools/deps_closure.py --check || die "判据② deps_closure --check 失败" 97
say "  判据② --check rc=0"

say "=== ⑤ 测量前 ps/ 指纹（第 140 条：板子是共享的）==="
( cd ps && find pynq -maxdepth 1 -name '*.py' | LC_ALL=C sort | xargs md5sum ) \
    > "$OUT/ps_md5_before.txt" 2>&1
md5sum "$OUT/ps_md5_before.txt" | tee "$OUT/ps_md5_before_SUM.txt"
( cd ps && find pynq -maxdepth 1 -name '*.py' | LC_ALL=C sort | xargs md5sum ) \
    > "$OUT/ps_md5_before2.txt" 2>&1
say "  再取一次用于事后差异比较（$(wc -l < "$OUT/ps_md5_before.txt") 个文件）"

say "=== ⑤-b 空跑门（**不加载位流**）：验计入/边界 ==="
"$PY0" "$DEST/$PROBE_BASE" --bit /dev/null --self-test || die "self-test 失败 ⇒ 不许 load" 98
say "  ✓ self-test PASS"

say "=== ⑥ 清 PYNQ 残留 + drop_caches ==="
PLD=$(ls -d /usr/local/share/pynq-venv/lib/python3*/site-packages/pynq/pl_server 2>/dev/null | head -1)
say "  pl_server = $PLD"
[ -n "$PLD" ] && printf '%s\n' "$SUDO_PASS" | sudo -S -p '' rm -f "$PLD/global_pl_state_.json" "$PLD/_current_metadata.pkl" 2>/dev/null || true
grep -E '^CmaFree' /proc/meminfo
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null \
    && say "  ✓ drop_caches" || say "  ⚠ drop_caches 失败（不致命）"
sleep 2
grep -E '^CmaFree' /proc/meminfo

say "==================================================================="
say "  单进程 一次 load：$(basename "$BIT_FILE")  写频 $FREQ_WRITE  期望读回 ${EXPECT_FCLK:-n/a}"
say "  主尺寸 ${HW:-256}² × $N_MAIN（丢首跑）；附加 $EXTRA_HW × $REPS_EXTRA；零护栏"
say "==================================================================="
EPOCH=$(date +%s.%N 2>/dev/null); case "$EPOCH" in *N*) EPOCH=$(date +%s);; esac
say "  epoch=$EPOCH"
t0=$(date +%s)
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' env XILINX_XRT=/usr \
    "$PY0" -u "$DEST/$PROBE_BASE" \
        --bit /home/xilinx/demoir/${BIT_ONBOARD:-design_1_150v163}.bit \
        --pack /home/xilinx/demoir/deploy/bin_weights_new \
        --freq-write "$FREQ_WRITE" \
        ${EXPECT_FCLK:+--expect-fclk "$EXPECT_FCLK"} \
        --calib "$CALIB" \
        --slots 24 --hw "${HW:-256}" \
        --n-main "$N_MAIN" --extra-hw "$EXTRA_HW" --reps-extra "$REPS_EXTRA" \
        --drop-first 1 --epoch "$EPOCH" --tag "$TAG" \
        ${DUMP_OUT:+--dump-out "$DUMP_OUT"} \
        ${CONTINUE_FLAG:-} \
        --wd 300 --tmax 1800 --outdir "$OUT" 2>&1 | tee "$OUT/${TAG}_run.log"
RC=${PIPESTATUS[1]}
PRC=$RC
t1=$(date +%s)
say "探针 rc=$RC  用时 $((t1-t0)) s"
case "$RC" in
  0) say "✓ 三尺寸 sha1 全中参照" ;;
  4) say "△ 有 sha1 未命中（见汇总表）" ;;
  31) say "✗✗ 频率读回不符 ⇒ 按父代理口径停手" ;;
  32) say "✗✗ load 门禁不过 ⇒ 停手" ;;
  *)  say "✗✗ 探针异常 rc=$RC ⇒ 停、报";;
esac

say "=== ⑦ 测量后取证 ==="
md5sum /home/xilinx/demoir/${BIT_ONBOARD:-design_1_150v163}.bit | tee "$OUT/${TAG}_bit_after.md5"
( cd ps && find pynq -maxdepth 1 -name '*.py' | LC_ALL=C sort | xargs md5sum ) \
    > "$OUT/ps_md5_after.txt" 2>&1
if diff -q "$OUT/ps_md5_before.txt" "$OUT/ps_md5_after.txt" >/dev/null 2>&1; then
    say "  ✓ 测量前后板端 ps/ 逐文件 md5 **完全一致**（第 140 条）"
else
    say "  ✗✗ 测量前后 ps/ 变了（别人动过板子）—— 见 ps_md5_diff.txt"
    diff "$OUT/ps_md5_before.txt" "$OUT/ps_md5_after.txt" > "$OUT/ps_md5_diff.txt" 2>&1 || true
    head -20 "$OUT/ps_md5_diff.txt"
fi
grep -E '^(CmaTotal|CmaFree|MemFree)' /proc/meminfo
ls -la "$OUT" | head -30
say "收尾只 sync（绝不 poweroff）"
sync; sync
say "仍开机：uptime=$(uptime | sed 's/^ *//')"
exit ${PRC:-0}
