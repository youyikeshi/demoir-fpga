#!/usr/bin/env bash
# ============================================================================
#  runs/board_t120/run_t120.sh —— T-120：**128² 输入**，v53 vs v39 同尺寸 A/B
#
#  唯一用法（必须先删陈旧 load 令牌）：
#      rm -f /tmp/dsh_board.loadtoken
#      BOARD_ALLOW_LOAD=1 BOARD_TAG=T120 tools/boardlock.sh bash runs/board_t120/run_t120.sh
#
#  ★ 一个租约（一次 boardlock）= 一次 session = **两次 load**：
#      进程 ① probe_t120.py --role v53 → load v53（唯一一次 download）
#               128² ×2 @50 MHz；每跑前后 magic(0x314)=0x50300001 / 0x310=0
#      进程 ② probe_t120.py --role v39 → load v39（唯一一次 download）
#               128² ×2 @50 MHz（这就是 128² 的参照值）+ 64² ×2（确认环境没变）
#    两次 load 分两个进程做：每个进程只 download 一次（历史验证过的路径），
#    中间清 PYNQ 的 pl 状态文件，避免第二次 download 被旧 metadata 干扰。
#
#  租约内共 2 条 ssh + 2 条 scp：
#      ① 体检/杀旧/CMA     ② scp     ③ 部署+门禁+跑两次 load     ④ 拉回产物
#  ★ 铁律：只 pkill -x / 按 pid kill -TERM；禁 pkill -f；禁 kill -9；
#          不收尾 poweroff（只 sync; sync）；不许 ssh 重试风暴；ulimit -c 0。
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOARD="${BOARD_HOST:-xilinx@192.168.7.2}"
SUDO_PASS="${BOARD_SUDO_PASS:-xilinx}"
DEST=/tmp/probe_T120
OUT="${OUT:-/home/xilinx/demoir/t120_out}"
LOCAL_OUT="${LOCAL_OUT:-$REPO/runs/board_t120}"

BIT53_DIR="$REPO/runs/v53_archive";              BIT53=design_1_150v53
BIT39_DIR="$REPO/pynq_specialized_code/board/overlay_engine_150mhz"; BIT39=design_1_150v39
MD5_BIT53=e19de0326fe657fc8de9ddd241ecc729
MD5_HWH53=5235764602921d03351e121a81e35613
MD5_BIT39=602725b86941864eeeeab3be005e1e81
MD5_HWH39=c1686bd8e080c1c7f46181d980543202

MANIFEST="$REPO/runs/board_ops/20261102/deps_closure.txt"
MD5_MANIFEST=$(md5sum "$MANIFEST" | cut -d" " -f1)
PROBE="$LOCAL_OUT/probe_t120.py"
PROBE_MD5=$(md5sum "$PROBE" | cut -d" " -f1)
P128="${RUNS128:-2}"
P64="${RUNS64:-2}"

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no
     -o ServerAliveInterval=20 -o ServerAliveCountMax=120 "$BOARD")
SCP=(scp -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)

mkdir -p "$LOCAL_OUT"
echo "[t120] repo = $REPO"
echo "[t120] board = $BOARD"
echo "[t120] 探针 md5 = $PROBE_MD5"
echo "[t120] 清单 md5 = $MD5_MANIFEST"
echo "[t120] 128² ×$P128（v53 与 v39 各一次 load），64² ×$P64（仅 v39 对照）"

# ---- ⓪ 主机侧：冻结资产硬校验 + 打包 ps ----
[ "$(md5sum "$BIT53_DIR/$BIT53.bit" | cut -d' ' -f1)" = "$MD5_BIT53" ] \
  || { echo "[t120] ✗ v53 bit md5 不符冻结值"; exit 15; }
[ "$(md5sum "$BIT53_DIR/$BIT53.hwh" | cut -d' ' -f1)" = "$MD5_HWH53" ] \
  || { echo "[t120] ✗ v53 hwh md5 不符冻结值"; exit 15; }
[ "$(md5sum "$BIT39_DIR/$BIT39.bit" | cut -d' ' -f1)" = "$MD5_BIT39" ] \
  || { echo "[t120] ✗ v39 bit md5 不符冻结值"; exit 15; }
[ "$(md5sum "$BIT39_DIR/$BIT39.hwh" | cut -d' ' -f1)" = "$MD5_HWH39" ] \
  || { echo "[t120] ✗ v39 hwh md5 不符冻结值"; exit 15; }
echo "[t120] ⓪ 两版 bit/hwh md5 = 冻结值 ✓（各自成对）"

cd "$REPO/pynq_specialized_code" || exit 10
find ps -type f ! -path "*__pycache__*" ! -name "*.so" | LC_ALL=C sort \
    > "$LOCAL_OUT/ps_t120_filelist.txt"
NF=$(wc -l < "$LOCAL_OUT/ps_t120_filelist.txt")
tar czf "$LOCAL_OUT/ps_t120_deploy.tgz" -T "$LOCAL_OUT/ps_t120_filelist.txt" \
    || { echo "[t120] ✗ 打包失败"; exit 10; }
xargs -a "$LOCAL_OUT/ps_t120_filelist.txt" md5sum > "$LOCAL_OUT/ps_md5_host.txt" \
    || { echo "[t120] ✗ md5 清单失败"; exit 10; }
echo "[t120] ⓪ 打包 $NF 个文件（$(du -h "$LOCAL_OUT/ps_t120_deploy.tgz" | cut -f1)）"

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
echo "--- 现有位流"
ls -l /home/xilinx/demoir/design_1_150v*.bit 2>/dev/null | tail -8 || true
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
echo "[t120] ① done"
EOS1
if [ $? -ne 0 ]; then echo "[t120] ✗ 板端体检失败（不重试风暴）"; exit 11; fi

# ---- ② scp ----
"${SCP[@]}" "$LOCAL_OUT/ps_t120_deploy.tgz" "$LOCAL_OUT/ps_md5_host.txt" \
    "$PROBE" "$MANIFEST" \
    "$BIT53_DIR/$BIT53.bit" "$BIT53_DIR/$BIT53.hwh" \
    "$BIT39_DIR/$BIT39.bit" "$BIT39_DIR/$BIT39.hwh" \
    "$BOARD:$DEST/" \
  || { echo "[t120] ✗ scp 失败"; exit 12; }
echo "[t120] ② scp done（ps 包 + md5 + 探针 + 清单 + 两版 bit/hwh）"

# ---- ③ 部署 + 门禁 + 两次 load 各跑一遍 ----
"${SSH[@]}" "bash -s" <<EOS
set -u
ulimit -c 0
cd /home/xilinx/demoir || exit 20

echo "=== ③-a 位流落位 + md5（两版都要成对）==="
cp -f "$DEST/$BIT53.bit" /home/xilinx/demoir/$BIT53.bit || exit 21
cp -f "$DEST/$BIT53.hwh" /home/xilinx/demoir/$BIT53.hwh || exit 21
cp -f "$DEST/$BIT39.bit" /home/xilinx/demoir/$BIT39.bit || exit 21
cp -f "$DEST/$BIT39.hwh" /home/xilinx/demoir/$BIT39.hwh || exit 21
for pair in "$BIT53.bit:$MD5_BIT53" "$BIT53.hwh:$MD5_HWH53" "$BIT39.bit:$MD5_BIT39" "$BIT39.hwh:$MD5_HWH39"; do
    f=\${pair%%:*}; want=\${pair##*:}
    got=\$(md5sum "/home/xilinx/demoir/\$f" | awk '{print \$1}')
    echo "  \$f = \$got （期望 \$want）"
    [ "\$got" = "\$want" ] || { echo "  ✗ \$f md5 不符 ⇒ 停"; exit 21; }
done
echo "  ✓ 两版 bit/hwh = 冻结值，成对"

echo "=== ③-b 解包 ps 源码 + 逐文件 md5 ==="
tar xzf "$DEST/ps_t120_deploy.tgz" || { echo "  ✗ 解包失败"; exit 21; }
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

echo "=== ③-d 清 PYNQ 残留状态 + 释放页缓存（v53 之前）==="
[ -n "\$PLD" ] && printf '%s\n' "$SUDO_PASS" | sudo -S -p '' rm -f "\$PLD/global_pl_state_.json" "\$PLD/_current_metadata.pkl" 2>/dev/null || true
grep -E '^(CmaTotal|CmaFree)' /proc/meminfo
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null && echo "  ✓ drop_caches" || echo "  ⚠ drop_caches 失败（不致命）"
sleep 2
grep -E '^(CmaTotal|CmaFree|MemFree|MemAvailable)' /proc/meminfo

echo "=== ③-e 进程①：load v53 → 128² ×$P128（★ 唯一 load #1）==="
t0=\$(date +%s)
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' env XILINX_XRT=/usr \\
    "\$PY0" -u "$DEST/probe_t120.py" --role v53 \\
        --bit /home/xilinx/demoir/$BIT53.bit \\
        --pack /home/xilinx/demoir/deploy/bin_weights_new \\
        --freq 50 --slots 24 --runs-128 $P128 --runs-64 0 \\
        --outdir "$OUT" 2>&1 | tee "$OUT/t120_v53_run.log"
RC53=\${PIPESTATUS[1]}
t1=\$(date +%s)
echo "  [t120] v53 探针 rc=\$RC53  用时 \$((t1-t0)) s"
md5sum /home/xilinx/demoir/$BIT53.bit | tee "$OUT/t120_v53_bit_after.md5"
case "\$RC53" in
  0|4) : ;;   # 0=正常；4=128² 两次不同（这是结果，不是故障）
  *) echo "  ✗✗ v53 探针 rc=\$RC53（异常/超时/信号）⇒ **不再做第二次 load**，停、报"; 
     grep -E '^(Cma|Mem)' /proc/meminfo; sync; sync; exit 30 ;;
esac

echo "=== ③-f 清 PYNQ 残留状态（两次 load 之间）==="
[ -n "\$PLD" ] && printf '%s\n' "$SUDO_PASS" | sudo -S -p '' rm -f "\$PLD/global_pl_state_.json" "\$PLD/_current_metadata.pkl" 2>/dev/null || true
sleep 1
grep -E '^CmaFree' /proc/meminfo

echo "=== ③-g 进程②：load v39 → 128² ×$P128 + 64² ×$P64（★ 唯一 load #2）==="
t0=\$(date +%s)
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' env XILINX_XRT=/usr \\
    "\$PY0" -u "$DEST/probe_t120.py" --role v39 \\
        --bit /home/xilinx/demoir/$BIT39.bit \\
        --pack /home/xilinx/demoir/deploy/bin_weights_new \\
        --freq 50 --slots 24 --runs-128 $P128 --runs-64 $P64 \\
        --outdir "$OUT" 2>&1 | tee "$OUT/t120_v39_run.log"
RC39=\${PIPESTATUS[1]}
t1=\$(date +%s)
echo "  [t120] v39 探针 rc=\$RC39  用时 \$((t1-t0)) s"
md5sum /home/xilinx/demoir/$BIT39.bit | tee "$OUT/t120_v39_bit_after.md5"

echo "=== ③-h 跑后凭据 ==="
grep -E '^(CmaTotal|CmaFree|MemFree)' /proc/meminfo
ls -la "$OUT" | head -40
du -sh "$OUT" 2>/dev/null || true
echo "  [t120] 收尾只 sync（不 poweroff）"
sync; sync
echo "  [t120] 仍开机：uptime=\$(uptime | sed 's/^ *//')"
echo "  [t120] rc53=\$RC53 rc39=\$RC39"
[ "\$RC39" = "0" ] || exit 31
exit 0
EOS
RC=$?
echo "[t120] ③ 返回 rc=$RC"

# ---- ④ 无论成败都把产物拉回 ----
"${SCP[@]}" -r "$BOARD:$OUT/*" "$LOCAL_OUT/" \
  && echo "[t120] ④ 产物已拉回 $LOCAL_OUT" \
  || echo "[t120] ⚠ 产物拉回失败（板端 $OUT）"

echo "[t120] DONE rc=$RC"
exit "$RC"
