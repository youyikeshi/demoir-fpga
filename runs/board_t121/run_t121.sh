#!/usr/bin/env bash
# ============================================================================
#  runs/board_t121/run_t121.sh —— T-121：**PF_EN=0 位流（v118）** 的机制判决
#
#  唯一用法（必须先删陈旧 load 令牌）：
#      rm -f /tmp/dsh_board.loadtoken
#      BOARD_ALLOW_LOAD=1 BOARD_TAG=T121 tools/boardlock.sh bash runs/board_t121/run_t121.sh
#
#  ★ 一个租约（一次 boardlock）= 一次 session = **一次 load**：
#      probe_t121.py --role v118 → load v118（唯一一次 download）
#          64² ×1   （先做：确认新位流没弄坏已证明正确的正常路径）
#          128² ×2  （★ 主判据：两次是否相同 + 是否 = v39 参照三 sha1）
#          256² ×1  （旁证：是否 = v39 冻结三 sha1；放最后，避免污染后续）
#      运行点冻结：BIN_LAZY_DEQ=0、无任何 BIN_V50_*；频率 50 MHz（只许 50）。
#
#  租约内共 2 条 ssh + 2 条 scp：
#      ① 体检/杀旧/CMA   ② scp   ③ 部署+双门禁+跑（一次 load）   ④ 拉回产物
#  ★ 铁律：只按 pid kill -TERM；禁 pkill -f；禁 kill -9；
#          不收尾 poweroff（只 sync; sync）；不许 ssh 重试风暴；ulimit -c 0。
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOARD="${BOARD_HOST:-xilinx@192.168.7.2}"
SUDO_PASS="${BOARD_SUDO_PASS:-xilinx}"
DEST=/tmp/probe_T121
OUT="${OUT:-/home/xilinx/demoir/t121_out}"
LOCAL_OUT="${LOCAL_OUT:-$REPO/runs/board_t121}"

# ★ PF_EN=0 位流（T-118 副本产物）与 v53 同一 BD ⇒ .hwh 用 v53 那颗
BITDIR_SRC="$REPO/worktrees/T-118-pfoff/out"
BIT_SRC=design_1_150v118.bit
MD5_BIT=8deb14f379b0c45fac5449b1873457bd
HWH_SRC="$REPO/runs/v53_archive/design_1_150v53.hwh"
MD5_HWH=5235764602921d03351e121a81e35613
BIT=design_1_150v118

MANIFEST="$REPO/runs/board_ops/20261102/deps_closure.txt"
MD5_MANIFEST=$(md5sum "$MANIFEST" | cut -d" " -f1)
PROBE="$LOCAL_OUT/probe_t121.py"
PROBE_MD5=$(md5sum "$PROBE" | cut -d" " -f1)
R64="${RUNS64:-1}"
R128="${RUNS128:-2}"
R256="${RUNS256:-1}"

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no
     -o ServerAliveInterval=20 -o ServerAliveCountMax=120 "$BOARD")
SCP=(scp -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)

mkdir -p "$LOCAL_OUT"
echo "[t121] repo = $REPO"
echo "[t121] 探针 md5 = $PROBE_MD5"
echo "[t121] 清单 md5 = $MD5_MANIFEST"
echo "[t121] 尺寸：64² ×$R64 → 128² ×$R128（主判据）→ 256² ×$R256"

# ---- ⓪ 主机侧：位流硬校验 + 打包 ps ----
[ "$(md5sum "$BITDIR_SRC/$BIT_SRC" | cut -d' ' -f1)" = "$MD5_BIT" ] \
  || { echo "[t121] ✗ v118 bit md5 不符（期望 $MD5_BIT）⇒ 停"; exit 15; }
[ "$(md5sum "$HWH_SRC" | cut -d' ' -f1)" = "$MD5_HWH" ] \
  || { echo "[t121] ✗ v53 hwh md5 不符（期望 $MD5_HWH）⇒ 停"; exit 15; }
cp -f "$HWH_SRC" "$LOCAL_OUT/$BIT.hwh"
echo "[t121] ⓪ bit md5 = $MD5_BIT ✓ ；hwh md5 = $MD5_HWH ✓（与 v53 同一 BD，未重建）"

cd "$REPO/pynq_specialized_code" || exit 10
find ps -type f ! -path "*__pycache__*" ! -name "*.so" | LC_ALL=C sort \
    > "$LOCAL_OUT/ps_t121_filelist.txt"
NF=$(wc -l < "$LOCAL_OUT/ps_t121_filelist.txt")
tar czf "$LOCAL_OUT/ps_t121_deploy.tgz" -T "$LOCAL_OUT/ps_t121_filelist.txt" \
    || { echo "[t121] ✗ 打包失败"; exit 10; }
xargs -a "$LOCAL_OUT/ps_t121_filelist.txt" md5sum > "$LOCAL_OUT/ps_md5_host.txt" \
    || { echo "[t121] ✗ md5 清单失败"; exit 10; }
echo "[t121] ⓪ 打包 $NF 个 ps 文件（$(du -h "$LOCAL_OUT/ps_t121_deploy.tgz" | cut -f1)）"

# ---- ① 板端体检 + CMA 审计 + 按 pid 杀旧程序 ----
"${SSH[@]}" "bash -s" <<EOS1
set -u
ulimit -c 0
mkdir -p '$DEST' '$OUT' || exit 1
echo "--- uptime"; uptime
echo "--- disk"; df -h / | tail -1; df -i / | tail -1
echo "--- mem/cma"
grep -E '^(MemTotal|MemFree|MemAvailable|CmaTotal|CmaFree|SwapTotal|SwapFree)' /proc/meminfo
echo "--- 现存 python（pgrep -x python3）"
pgrep -a -x python3 2>/dev/null | head -10 || true
CUR=\$\$; PAR=\$PPID
for p in \$(pgrep -x python3 2>/dev/null || true); do
    [ "\$p" = "\$CUR" ] && continue
    [ "\$p" = "\$PAR" ] && continue
    c=\$(tr '\0' ' ' < /proc/\$p/cmdline 2>/dev/null || true)
    case "\$c" in
      *jupyter*|*redirect_server*|*networkd*|*unattended*|*'pynq.pl_server'*) continue;;
    esac
    case "\$c" in
      *probe_*|*bin_net*|*board_*|*t1[0-9][0-9]_*)
        echo "  kill -TERM \$p : \$c"; kill -TERM "\$p" 2>/dev/null || true;;
    esac
done
sleep 2
echo "--- 杀后残留"
pgrep -a -x python3 2>/dev/null | grep -E 'probe_|bin_net|board_' || echo "  (无)"
echo "--- 板上现有位流"
ls -l /home/xilinx/demoir/design_1_150v*.bit 2>/dev/null | tail -6 || true
echo "--- PACK 目录"
ls -d /home/xilinx/demoir/deploy/bin_weights_new 2>/dev/null || true
md5sum /home/xilinx/demoir/deploy/bin_weights_new/index.json 2>/dev/null | head -1 || true
echo "--- 真 python"
ls -l /usr/local/share/pynq-venv/bin/python3 2>&1 | head -2
echo "--- 腾 CMA：drop_caches 前"
grep -E '^CmaFree' /proc/meminfo
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null && echo "  ✓ drop_caches" || echo "  ⚠ drop_caches 失败"
sleep 1
echo "--- 腾 CMA：drop_caches 后"
grep -E '^CmaFree' /proc/meminfo
echo "[t121] ① done"
EOS1
if [ $? -ne 0 ]; then echo "[t121] ✗ 板端体检失败（不重试风暴）"; exit 11; fi

# ---- ② scp ----
"${SCP[@]}" "$LOCAL_OUT/ps_t121_deploy.tgz" "$LOCAL_OUT/ps_md5_host.txt" \
    "$PROBE" "$MANIFEST" \
    "$BITDIR_SRC/$BIT_SRC" "$LOCAL_OUT/$BIT.hwh" \
    "$BOARD:$DEST/" \
  || { echo "[t121] ✗ scp 失败"; exit 12; }
echo "[t121] ② scp done（ps 包 + md5 清单 + 探针 + 依赖清单 + v118 bit/hwh）"

# ---- ③ 部署 + 双门禁 + 一次 load 跑三尺寸 ----
"${SSH[@]}" "bash -s" <<EOS
set -u
ulimit -c 0
cd /home/xilinx/demoir || exit 20

echo "=== ③-a 位流落位 + md5（bit 与 hwh 必须成对）==="
cp -f "$DEST/$BIT_SRC" /home/xilinx/demoir/$BIT.bit || exit 21
cp -f "$DEST/$BIT.hwh" /home/xilinx/demoir/$BIT.hwh || exit 21
for pair in "$BIT.bit:$MD5_BIT" "$BIT.hwh:$MD5_HWH"; do
    f=\${pair%%:*}; want=\${pair##*:}
    got=\$(md5sum "/home/xilinx/demoir/\$f" | awk '{print \$1}')
    echo "  \$f = \$got （期望 \$want）"
    [ "\$got" = "\$want" ] || { echo "  ✗ \$f md5 不符 ⇒ 停"; exit 21; }
done
echo "  ✓ v118 bit/hwh = 冻结值，成对"

echo "=== ③-b 解包 ps 源码 + 逐文件 md5 ==="
tar xzf "$DEST/ps_t121_deploy.tgz" || { echo "  ✗ 解包失败"; exit 21; }
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' rm -rf ps/pynq/__pycache__ ps/tools/__pycache__ 2>/dev/null || true
echo "  解包完成（\$(find ps -type f ! -path '*__pycache__*' | wc -l) 个文件）"
if md5sum -c "$DEST/ps_md5_host.txt" > "$OUT/ps_md5_board_check.txt" 2>&1; then
    echo "  ✓ md5 -c 全过：\$(grep -c ': OK' "$OUT/ps_md5_board_check.txt") 个文件"
else
    echo "  ✗ md5 -c 失败"
    grep ': FAILED' "$OUT/ps_md5_board_check.txt" | head -20
    exit 22
fi

PY0=""
for p in /usr/local/share/pynq-venv/bin/python3 /usr/share/pynq-venv/bin/python3; do
    if [ -x "\$p" ]; then PY0="\$p"; break; fi
done
echo "  python = \$PY0"
[ -n "\$PY0" ] || { echo "  ✗ 找不到 pynq venv python3 ⇒ 停"; exit 26; }

echo "=== ③-c 依赖闭包清单（与 ps/ 一起同步）==="
MROOT=/home/xilinx
MPRIM=\$MROOT/runs/board_ops/20261102/deps_closure.txt
mkdir -p "\$MROOT/runs/board_ops/20261102" runs/board_ops/20261102
cp -f "$DEST/deps_closure.txt" "\$MPRIM"
cp -f "$DEST/deps_closure.txt" runs/board_ops/20261102/deps_closure.txt
M=\$(md5sum "\$MPRIM" | awk '{print \$1}')
M2=\$(md5sum runs/board_ops/20261102/deps_closure.txt | awk '{print \$1}')
echo "  主清单 md5 = \$M ；镜像 md5 = \$M2 （主机 $MD5_MANIFEST）"
[ "\$M" = "$MD5_MANIFEST" ] && [ "\$M2" = "$MD5_MANIFEST" ] || { echo "  ✗ 清单 md5 不符 ⇒ 停"; exit 25; }
echo "  判据①：cd ps/pynq && md5sum -c <主清单>"
( cd ps/pynq && md5sum -c "\$MPRIM" > "$OUT/ps_deps_md5sum_c.txt" 2>&1 ) || {
    echo "  ✗ md5sum -c 失败"; tail -20 "$OUT/ps_deps_md5sum_c.txt"; exit 25; }
echo "    ✓ rc=0（一致文件数 = \$(grep -c ': OK' "$OUT/ps_deps_md5sum_c.txt")）"
echo "  判据②：deps_closure --check"
"\$PY0" ps/tools/deps_closure.py --check; RC=\$?
echo "    rc=\$RC"
[ "\$RC" = "0" ] || { echo "  ✗ 依赖闭包不合格 ⇒ 停"; exit 23; }

PLD=\$(ls -d /usr/local/share/pynq-venv/lib/python3*/site-packages/pynq/pl_server 2>/dev/null | head -1)
echo "  pl_server = \$PLD"

echo "=== ③-d 清 PYNQ 残留状态 + 释放页缓存 ==="
[ -n "\$PLD" ] && printf '%s\n' "$SUDO_PASS" | sudo -S -p '' rm -f "\$PLD/global_pl_state_.json" "\$PLD/_current_metadata.pkl" 2>/dev/null || true
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null && echo "  ✓ drop_caches" || echo "  ⚠ drop_caches 失败（不致命）"
sleep 2
grep -E '^(CmaTotal|CmaFree|MemFree|MemAvailable)' /proc/meminfo

echo "=== ③-e 唯一 load：v118 → 64² ×$R64 / 128² ×$R128 / 256² ×$R256 ==="
T0=\$(date +%s)
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' env XILINX_XRT=/usr \\
    "\$PY0" -u "$DEST/probe_t121.py" --role v118 \\
        --bit /home/xilinx/demoir/$BIT.bit \\
        --pack /home/xilinx/demoir/deploy/bin_weights_new \\
        --freq 50 --slots 24 \\
        --runs-64 $R64 --runs-128 $R128 --runs-256 $R256 \\
        --outdir "$OUT" 2>&1 | tee "$OUT/t121_v118_run.log"
RCP=\${PIPESTATUS[1]}
T1=\$(date +%s)
echo "  [t121] 探针 rc=\$RCP  用时 \$((T1-T0)) s"
md5sum /home/xilinx/demoir/$BIT.bit | tee "$OUT/t121_v118_bit_after.md5"

echo "=== ③-f 跑后凭据 ==="
grep -E '^(CmaTotal|CmaFree|MemFree)' /proc/meminfo
echo "--- dmesg 尾部（找 segfault/OOM）"
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' dmesg 2>/dev/null | tail -12 || echo "  (dmesg 读不到)"
echo "--- 残留 python"
pgrep -a -x python3 2>/dev/null | head -5 || echo "  (无)"
ls -la "$OUT" | head -30
echo "  [t121] 收尾只 sync（不 poweroff）"
sync; sync
echo "  [t121] 仍开机：uptime=\$(uptime | sed 's/^ *//')"
echo "  [t121] 探针 rc=\$RCP"
exit 0
EOS
RC=$?
echo "[t121] ③ 返回 rc=$RC"

# ---- ④ 无论成败都把产物拉回 ----
"${SCP[@]}" -r "$BOARD:$OUT/*" "$LOCAL_OUT/" \
  && echo "[t121] ④ 产物已拉回 $LOCAL_OUT" \
  || echo "[t121] ⚠ 产物拉回失败（板端 $OUT）"

echo "[t121] DONE rc=$RC"
exit "$RC"
