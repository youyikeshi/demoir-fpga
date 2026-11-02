#!/usr/bin/env bash
# ============================================================================
#  runs/board_t86/t86_board.sh —— T-86 板端执行体（由 run_on_board.sh scp 后执行）
#
#  环境（run_on_board.sh 在 ssh 命令行上给出）：
#      SUDO_PASS / DEST / OUT / MD5_BIT / MD5_HWH / MD5_MANIFEST / PROBE_MD5 / FCLK
#
#  流程：解包 ps → 逐文件 md5 对齐 → 落依赖闭包清单(两处) → md5sum -c 门禁
#        → 空闲检查 → 成对位流 md5 门禁 → drop_caches → 跑探针（一次 load + 三段）
#        → 取证 → sync（**不 poweroff**）
# ============================================================================
set -u
ulimit -c 0
cd /home/xilinx/demoir || exit 20

PY0=/usr/local/share/pynq-venv/bin/python3
mkdir -p "$OUT" "$DEST" || exit 21

echo "=== 板端 python ==="
[ -x "$PY0" ] || { echo "  ✗ 找不到 $PY0 ⇒ 停"; exit 26; }
"$PY0" -c "import pynq, sys; print('  pynq', pynq.__version__, '| py', sys.version.split()[0])" || exit 26
echo "  python = $PY0"

echo "=== ③-a 解包 ps 源码 ==="
tar xzf "$DEST/ps_t86_deploy.tgz" || { echo "  ✗ 解包失败"; exit 21; }
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' rm -rf ps/pynq/__pycache__ ps/tools/__pycache__ 2>/dev/null || true
echo "  解包完成（$(find ps -type f ! -path '*__pycache__*' | wc -l) 个文件）"

echo "=== ③-b 主机↔板端逐文件 md5 对齐 ==="
if md5sum -c "$DEST/ps_md5_host.txt" > "$OUT/ps_md5_board_check.txt" 2>&1; then
    echo "  ✓ md5 -c 全过：$(grep -c ': OK' "$OUT/ps_md5_board_check.txt") 个文件"
else
    echo "  ✗ md5 -c 有失败："
    grep ': FAILED' "$OUT/ps_md5_board_check.txt" | head -20
    exit 22
fi
md5sum ps/pynq/fconv.py | tee "$OUT/t86_fconv_md5.txt"
[ "$(md5sum ps/pynq/fconv.py | awk '{print $1}')" = "67ea5cbb5c41c2e478d7c033800fde57" ] \
    || { echo "  ✗ fconv.py md5 不是冻结值 ⇒ 停"; exit 22; }
[ "$(md5sum $DEST/t86_probe.py | awk '{print $1}')" = "$PROBE_MD5" ] \
    || { echo "  ✗ 探针传输 md5 不符 ⇒ 停"; exit 26; }
echo "  ✓ fconv.py / 探针 md5 均为冻结值"

echo "=== ③-b2 依赖闭包清单（与 ps/ 同时同步，T-53 的坑）==="
MROOT=/home/xilinx
MPRIM=$MROOT/runs/board_ops/20261102/deps_closure.txt
mkdir -p "$MROOT/runs/board_ops/20261102" runs/board_ops/20261102
cp -f "$DEST/deps_closure.txt" "$MPRIM"
cp -f "$DEST/deps_closure.txt" runs/board_ops/20261102/deps_closure.txt
M=$(md5sum "$MPRIM" | awk '{print $1}')
M2=$(md5sum runs/board_ops/20261102/deps_closure.txt | awk '{print $1}')
echo "  板端主清单 $MPRIM md5 = $M"
echo "  板端镜像 runs/board_ops/20261102/deps_closure.txt md5 = $M2"
[ "$M" = "$MD5_MANIFEST" ] && [ "$M2" = "$MD5_MANIFEST" ] || {
    echo "  ✗ 清单 md5 不符（期望 $MD5_MANIFEST）⇒ 停"; exit 25; }
( cd ps/pynq && LC_ALL=C md5sum -c "$MPRIM" > "$OUT/ps_deps_md5sum_c.txt" 2>&1 ) || {
    echo "  ✗ md5sum -c 失败："; tail -20 "$OUT/ps_deps_md5sum_c.txt"; exit 25; }
NOK=$(grep -c ': OK$' "$OUT/ps_deps_md5sum_c.txt")
echo "    ✓ 依赖闭包 md5sum -c rc=0（一致文件数 = $NOK，期望 20）"
[ "$NOK" = "20" ] || { echo "  ✗ 一致文件数 $NOK ≠ 20 ⇒ 停"; exit 25; }

echo "=== ③-d 板子空闲检查 ==="
BUSY=$(ps -eo pid,etime,cmd | grep -E "[p]ython3? .*(t[3-9][0-9]|probe_|bin_net|board_|run_e|one_load)" | head -5)
if [ -n "$BUSY" ]; then
    echo "  ✗ 板上还有别的 python 在跑 ⇒ 不启动："; echo "$BUSY"; exit 24
fi
echo "  ✓ 空闲"

echo "=== ③-e 位流成对 md5 门禁（v45 .bit 与它自己的 .hwh）==="
B1=$(md5sum "$DEST/design_1_150v45.bit" | awk '{print $1}')
B2=$(md5sum "$DEST/design_1_150v45.hwh" | awk '{print $1}')
echo "  v45 .bit=$B1 .hwh=$B2"
[ "$B1" = "$MD5_BIT" ] && [ "$B2" = "$MD5_HWH" ] || { echo "  ✗ v45 成对 md5 不符 ⇒ 停"; exit 27; }
ls -la "$DEST"/design_1_150v45.* >> "$OUT/t86_bits_pair.txt" 2>&1
echo "  ✓ 成对合格（期望 .bit=$MD5_BIT .hwh=$MD5_HWH）"

echo "=== ③-f 释放页缓存 ==="
grep -E '^CmaFree' /proc/meminfo
sync
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null \
    && echo "  ✓ drop_caches 完成" || echo "  ⚠ drop_caches 失败（不致命）"
grep -E '^CmaFree' /proc/meminfo

echo "=== ③-g 跑探针（一次 load + 三段）==="
t0=$(date +%s)
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' env XILINX_XRT=/usr \
    PYTHONPATH=/home/xilinx/demoir/ps/pynq \
    "$PY0" -u "$DEST/t86_probe.py" \
        --bit "$DEST/design_1_150v45.bit" --out "$OUT" --fclk "$FCLK" 2>&1 \
    | tee "$OUT/t86_probe.log"
RC=${PIPESTATUS[0]}
t1=$(date +%s)
echo "  [t86] 探针 rc=$RC  用时 $((t1-t0)) s"

echo "=== ③-h 跑后取证 ==="
echo "--- pl_server 当前位流 / 时钟状态"
cat /usr/local/share/pynq-venv/lib/python3*/site-packages/pynq/pl_server/global_pl_state_.json 2>/dev/null | head -c 400; echo
grep -E '^(MemTotal|MemFree|MemAvailable|CmaTotal|CmaFree)' /proc/meminfo | tee "$OUT/t86_mem_after.txt"
echo "--- 板端历史位流（只记录，不删）"
md5sum design_1_150v39.bit design_1_150v39.hwh 2>&1 | tee "$OUT/t86_bits_after.md5"
echo "--- core 文件（应为 0）"
ls -la /home/xilinx/core* 2>/dev/null | head -3 || echo "  无 core"
ls -la "$OUT"
echo "  [t86] 收尾 sync（不 poweroff）"
sync; sync
echo "  [t86] rc=$RC"
exit "$RC"
