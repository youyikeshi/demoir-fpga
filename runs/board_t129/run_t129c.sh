#!/usr/bin/env bash
# ============================================================================
#  runs/board_t129/run_t129c.sh —— T-129 第 3 轮：**最高压力重叠 + 压力重复**
#
#  唯一用法（必须先删陈旧 load 令牌）：
#      rm -f /tmp/dsh_board.loadtoken
#      BOARD_ALLOW_LOAD=1 BOARD_TAG=T129C tools/boardlock.sh bash runs/board_t129/run_t129c.sh
#
#  ★ 一个租约 = 一次 session = **一次 load**（只有 v53）：
#      probe_t129c.py 一个进程内跑 S / P1 / P2 / P2nc / P2ncan / P2lazy（各 2 跑）
#      + F（写回落地时延）+ G（P2 连跑 N 次判非确定）
#
#  租约内共 2 条 ssh + 1 条 scp（去）+ 1 条 scp（回）：
#      ① 体检/杀旧/CMA  ② scp  ③ 部署+门禁+跑  ④ 拉回产物
#  ★ 铁律：只 pkill -x / 按 pid kill -TERM；禁 pkill -f；禁 kill -9；
#          不收尾 poweroff（只 sync; sync）；不许 ssh 重试风暴；ulimit -c 0。
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOARD="${BOARD_HOST:-xilinx@192.168.7.2}"
SUDO_PASS="${BOARD_SUDO_PASS:-xilinx}"
DEST=/tmp/probe_T129C
OUT="${OUT:-/home/xilinx/demoir/t129c_out}"
LOCAL_OUT="${LOCAL_OUT:-$REPO/runs/board_t129}"

BIT53_DIR="$REPO/runs/v53_archive"; BIT53=design_1_150v53
MD5_BIT53=e19de0326fe657fc8de9ddd241ecc729
MD5_HWH53=5235764602921d03351e121a81e35613

MANIFEST="$REPO/runs/board_ops/20261102/deps_closure.txt"
MD5_MANIFEST=$(md5sum "$MANIFEST" | cut -d" " -f1)
PROBE="$LOCAL_OUT/probe_t129c.py"
PROBE_MD5=$(md5sum "$PROBE" | cut -d" " -f1)
HW="${HW:-64}"
REPS="${REPS:-2}"
STRESS="${STRESS:-100}"
ONLY="${ONLY:-}"
ONLYARG=""
[ -n "$ONLY" ] && ONLYARG="--only $ONLY"
FREQ="${FREQ:-50}"

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no
     -o ServerAliveInterval=20 -o ServerAliveCountMax=120 "$BOARD")
SCP=(scp -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)

mkdir -p "$LOCAL_OUT"
echo "[t129] repo = $REPO"
echo "[t129] board = $BOARD"
echo "[t129] 探针 md5 = $PROBE_MD5"
echo "[t129] 清单 md5 = $MD5_MANIFEST"
echo "[t129] 特征图 H=W=$HW（64=基分辨率 128²）@ $FREQ MHz；每臂 $REPS 跑；G 臂 $STRESS 跑；只跑=${ONLY:-全部}"

# ---- ⓪ 主机侧：冻结资产硬校验 + 打包 ps ----
[ "$(md5sum "$BIT53_DIR/$BIT53.bit" | cut -d' ' -f1)" = "$MD5_BIT53" ] \
  || { echo "[t129] ✗ v53 bit md5 不符冻结值"; exit 15; }
[ "$(md5sum "$BIT53_DIR/$BIT53.hwh" | cut -d' ' -f1)" = "$MD5_HWH53" ] \
  || { echo "[t129] ✗ v53 hwh md5 不符冻结值"; exit 15; }
echo "[t129] ⓪ v53 bit/hwh md5 = 冻结值 ✓（成对）"

cd "$REPO/pynq_specialized_code" || exit 10
find ps -type f ! -path "*__pycache__*" ! -name "*.so" | LC_ALL=C sort \
    > "$LOCAL_OUT/ps_t129c_filelist.txt"
NF=$(wc -l < "$LOCAL_OUT/ps_t129c_filelist.txt")
tar czf "$LOCAL_OUT/ps_t129c_deploy.tgz" -T "$LOCAL_OUT/ps_t129c_filelist.txt" \
    || { echo "[t129] ✗ 打包失败"; exit 10; }
xargs -a "$LOCAL_OUT/ps_t129c_filelist.txt" md5sum > "$LOCAL_OUT/ps_md5_host.txt" \
    || { echo "[t129] ✗ md5 清单失败"; exit 10; }
echo "[t129] ⓪ 打包 $NF 个文件（$(du -h "$LOCAL_OUT/ps_t129c_deploy.tgz" | cut -f1)）"

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
echo "--- PACK 目录 + e1e2_desc（E1E2 要它）"
ls -d /home/xilinx/demoir/deploy/bin_weights_new /home/xilinx/demoir/deploy/e1e2_desc 2>/dev/null || true
md5sum /home/xilinx/demoir/deploy/bin_weights_new/index.json 2>/dev/null | head -1 || true
ls /home/xilinx/demoir/deploy/e1e2_desc 2>/dev/null | tr '\n' ' '; echo
echo "--- 真 python"
ls -l /usr/local/share/pynq-venv/bin/python3 2>&1 | head -2
echo "--- 腾 CMA：drop_caches 前"
grep -E '^CmaFree' /proc/meminfo
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null && echo "  ✓ drop_caches" || echo "  ⚠ drop_caches 失败"
sleep 1
echo "--- 腾 CMA：drop_caches 后"
grep -E '^CmaFree' /proc/meminfo
echo "[t129] ① done"
EOS1
if [ $? -ne 0 ]; then echo "[t129] ✗ 板端体检失败（不重试风暴）"; exit 11; fi

# ---- ② scp ----
"${SCP[@]}" "$LOCAL_OUT/ps_t129c_deploy.tgz" "$LOCAL_OUT/ps_md5_host.txt" \
    "$PROBE" "$MANIFEST" \
    "$BIT53_DIR/$BIT53.bit" "$BIT53_DIR/$BIT53.hwh" \
    "$BOARD:$DEST/" \
  || { echo "[t129] ✗ scp 失败"; exit 12; }
echo "[t129] ② scp done（ps 包 + md5 + 探针 + 清单 + v53 bit/hwh）"

# ---- ③ 部署 + 门禁 + 跑 ----
"${SSH[@]}" "bash -s" <<EOS
set -u
ulimit -c 0
cd /home/xilinx/demoir || exit 20

echo "=== ③-a 位流落位 + md5 ==="
cp -f "$DEST/$BIT53.bit" /home/xilinx/demoir/$BIT53.bit || exit 21
cp -f "$DEST/$BIT53.hwh" /home/xilinx/demoir/$BIT53.hwh || exit 21
for pair in "$BIT53.bit:$MD5_BIT53" "$BIT53.hwh:$MD5_HWH53"; do
    f=\${pair%%:*}; want=\${pair##*:}
    got=\$(md5sum "/home/xilinx/demoir/\$f" | awk '{print \$1}')
    echo "  \$f = \$got （期望 \$want）"
    [ "\$got" = "\$want" ] || { echo "  ✗ \$f md5 不符 ⇒ 停"; exit 21; }
done
echo "  ✓ v53 bit/hwh = 冻结值，成对"

echo "=== ③-b 解包 ps 源码 + 逐文件 md5 ==="
tar xzf "$DEST/ps_t129c_deploy.tgz" || { echo "  ✗ 解包失败"; exit 21; }
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

echo "=== ③-d 清 PYNQ 残留状态 + 释放页缓存（load 之前）==="
[ -n "\$PLD" ] && printf '%s\n' "$SUDO_PASS" | sudo -S -p '' rm -f "\$PLD/global_pl_state_.json" "\$PLD/_current_metadata.pkl" 2>/dev/null || true
grep -E '^(CmaTotal|CmaFree)' /proc/meminfo
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null && echo "  ✓ drop_caches" || echo "  ⚠ drop_caches 失败（不致命）"
sleep 2
grep -E '^(CmaTotal|CmaFree|MemFree|MemAvailable)' /proc/meminfo

echo "=== ③-e 探针：3 个 op 的真实流水排程 S/P1/P2（★ 唯一 load）==="
t0=\$(date +%s)
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' env XILINX_XRT=/usr \\
    "\$PY0" -u "$DEST/probe_t129c.py" \\
        --bit /home/xilinx/demoir/$BIT53.bit \\
        --pack /home/xilinx/demoir/deploy/bin_weights_new \\
        --freq $FREQ --H $HW --reps $REPS --stress $STRESS $ONLYARG \\
        --outdir "$OUT" 2>&1 | tee "$OUT/t129c_run.log"
RC=\${PIPESTATUS[1]}
t1=\$(date +%s)
echo "  [t129] 探针 rc=\$RC  用时 \$((t1-t0)) s"
md5sum /home/xilinx/demoir/$BIT53.bit | tee "$OUT/t129c_bit_after.md5"
case "\$RC" in
  0|4) : ;;   # 0=各臂全对；4=有臂出错（这是结果，不是故障）
  *) echo "  ✗✗ 探针 rc=\$RC（异常/超时/信号）⇒ 停、报"
     grep -E '^(Cma|Mem)' /proc/meminfo; sync; sync; exit 30 ;;
esac

echo "=== ③-f 跑后凭据 ==="
grep -E '^(CmaTotal|CmaFree|MemFree)' /proc/meminfo
ls -la "$OUT" | head -40
du -sh "$OUT" 2>/dev/null || true
echo "  [t129] 收尾只 sync（不 poweroff）"
sync; sync
echo "  [t129] 仍开机：uptime=\$(uptime | sed 's/^ *//')"
echo "  [t129] rc=\$RC"
exit 0
EOS
RC=$?
echo "[t129] ③ 返回 rc=$RC"

# ---- ④ 无论成败都把产物拉回 ----
"${SCP[@]}" -r "$BOARD:$OUT/*" "$LOCAL_OUT/" \
  && echo "[t129] ④ 产物已拉回 $LOCAL_OUT" \
  || echo "[t129] ⚠ 产物拉回失败（板端 $OUT）"

echo "[t129] DONE rc=$RC"
exit "$RC"
