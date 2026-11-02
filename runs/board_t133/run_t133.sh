#!/usr/bin/env bash
# ============================================================================
#  runs/board_t133/run_t133.sh —— T-133：把"`BE._flush` 之后 sleep 3 ms"换成**便宜且确定
#  的 fence**，并证明等效。v53 / 50 MHz / 128² 全臂 + 最优臂下 256² 与 64²。
#
#  唯一用法（必须先删陈旧 load 令牌）：
#      rm -f /tmp/dsh_board.loadtoken
#      BOARD_ALLOW_LOAD=1 BOARD_TAG=T133 tools/boardlock.sh bash runs/board_t133/run_t133.sh
#
#  ★ 一个租约 = **一个进程 = 一次 load**（v53 位流）。进程内按 --plan 顺序跑 18 次 128²
#    （B0×2 / B1×2 / F1×2 / F2×2 / F3×2 / F3x×2 / F4a×2 / F4×2 / F5×2），
#    再由探针**自己**选最优臂（pick_winner），然后在**同一进程**里用最优臂跑
#      256² 一次 + 64² 一次。
#    ⇒ 全程只 download=True 一次，只一条 ssh 跑业务，产物一次拉回。
#
#  租约内共 2 条 ssh + 2 条 scp：
#      ① 体检/杀旧/CMA   ② scp   ③ 部署+双门禁+探针   ④ 拉回产物
#  ★ 铁律：只按 pid `kill -TERM`；**禁 `pkill -f`/killall**；不收尾 poweroff（只 `sync; sync`）；
#          不许 ssh 重试风暴（boardlock 内含 6/60s 闸门）；`ulimit -c 0`；频率 ≤ 50 MHz。
#  ★ ps/ 源码**一字节不改**（fence 全在探针的运行时钩子里），但**必须**与
#    `runs/board_ops/20261102/deps_closure.txt` 一起同步，并跑两条判据：
#        ① cd ps/pynq && md5sum -c deps_closure.txt     （rc 必须 0）
#        ② python3 ps/tools/deps_closure.py --check     （rc 必须 0）
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOARD="${BOARD_HOST:-xilinx@192.168.7.2}"
SUDO_PASS="${BOARD_SUDO_PASS:-xilinx}"
DEST=/tmp/probe_T133
OUT="${OUT:-/home/xilinx/demoir/t133_out}"
LOCAL_OUT="${LOCAL_OUT:-$REPO/runs/board_t133/stage1}"

BIT53_DIR="$REPO/runs/v53_archive"; BIT53=design_1_150v53
MD5_BIT53=e19de0326fe657fc8de9ddd241ecc729
MD5_HWH53=5235764602921d03351e121a81e35613

MANIFEST="$REPO/runs/board_ops/20261102/deps_closure.txt"
MD5_MANIFEST=$(md5sum "$MANIFEST" | cut -d" " -f1)
PROBE="$REPO/runs/board_t133/probe_t133.py"
PROBE_MD5=$(md5sum "$PROBE" | cut -d" " -f1)
PLAN="${PLAN:-sleep3,sleep3,none,none,readall,readall,readtail,readtail,readhead,readhead,touchonly,touchonly,dsb,dsb,dsb0p3,dsb0p3,devmem,devmem}"
HW256="${HW256:-1}"
HW64="${HW64:-1}"
FREQ="${FREQ:-50.0}"
SLOTS="${SLOTS:-24}"

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no
     -o ServerAliveInterval=20 -o ServerAliveCountMax=180 "$BOARD")
SCP=(scp -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)

mkdir -p "$LOCAL_OUT"
echo "[t133] repo = $REPO"
echo "[t133] board = $BOARD"
echo "[t133] 探针 md5 = $PROBE_MD5"
echo "[t133] 清单 md5 = $MD5_MANIFEST"
echo "[t133] plan = $PLAN"
echo "[t133] HW256=$HW256 HW64=$HW64  @ ${FREQ} MHz，${SLOTS} 槽（单进程、单次 load）"

# ---- ⓪ 主机侧：冻结资产硬校验 + 打包 ps ----
[ "$(md5sum "$BIT53_DIR/$BIT53.bit" | cut -d' ' -f1)" = "$MD5_BIT53" ] \
  || { echo "[t133] ✗ v53 bit md5 不符冻结值"; exit 15; }
[ "$(md5sum "$BIT53_DIR/$BIT53.hwh" | cut -d' ' -f1)" = "$MD5_HWH53" ] \
  || { echo "[t133] ✗ v53 hwh md5 不符冻结值"; exit 15; }
echo "[t133] ⓪ v53 bit/hwh = 冻结值 ✓"

cd "$REPO/pynq_specialized_code" || exit 10
find ps -type f ! -path "*__pycache__*" ! -name "*.so" | LC_ALL=C sort \
    > "$LOCAL_OUT/ps_t133_filelist.txt"
NF=$(wc -l < "$LOCAL_OUT/ps_t133_filelist.txt")
tar czf "$LOCAL_OUT/ps_t133_deploy.tgz" -T "$LOCAL_OUT/ps_t133_filelist.txt" \
    || { echo "[t133] ✗ 打包失败"; exit 10; }
xargs -a "$LOCAL_OUT/ps_t133_filelist.txt" md5sum > "$LOCAL_OUT/ps_md5_host.txt" \
    || { echo "[t133] ✗ md5 清单失败"; exit 10; }
echo "[t133] ⓪ 打包 $NF 个文件（$(du -h "$LOCAL_OUT/ps_t133_deploy.tgz" | cut -f1)）"

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
echo "--- /tmp/t133_fence 残留"
ls -l /tmp/t133_fence 2>/dev/null || echo "  (无)"
echo "--- cc 可用？（F4 的 dsb 助手要现场编）"
which cc && cc --version | head -1 || echo "  ⚠ 没有 cc"
echo "--- 腾 CMA：drop_caches 前"
grep -E '^CmaFree' /proc/meminfo
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null && echo "  ✓ drop_caches" || echo "  ⚠ drop_caches 失败"
sleep 1
echo "--- 腾 CMA：drop_caches 后"
grep -E '^CmaFree' /proc/meminfo
echo "[t133] ① done"
EOS1
if [ $? -ne 0 ]; then echo "[t133] ✗ 板端体检失败（不重试风暴）"; exit 11; fi

# ---- ② scp ----
"${SCP[@]}" "$LOCAL_OUT/ps_t133_deploy.tgz" "$LOCAL_OUT/ps_md5_host.txt" \
    "$PROBE" "$MANIFEST" \
    "$BIT53_DIR/$BIT53.bit" "$BIT53_DIR/$BIT53.hwh" \
    "$BOARD:$DEST/" \
  || { echo "[t133] ✗ scp 失败"; exit 12; }
echo "[t133] ② scp done（ps 包 + md5 + 探针 + 清单 + bit/hwh）"

# ---- ③ 部署 + 门禁 + 探针（单进程、单次 load）----
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
echo "  ✓ bit/hwh = 冻结值，成对"

echo "=== ③-b 解包 ps 源码 + 逐文件 md5 ==="
tar xzf "$DEST/ps_t133_deploy.tgz" || { echo "  ✗ 解包失败"; exit 21; }
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

echo "==================================================================="
echo "  单进程 一次 load：plan=$PLAN  HW256=$HW256 HW64=$HW64  @ $FREQ MHz"
echo "==================================================================="
t0=\$(date +%s)
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' env XILINX_XRT=/usr \\
    "\$PY0" -u "$DEST/probe_t133.py" \\
        --bit /home/xilinx/demoir/$BIT53.bit \\
        --pack /home/xilinx/demoir/deploy/bin_weights_new \\
        --freq $FREQ --slots $SLOTS --plan "$PLAN" \\
        --hw256 $HW256 --hw64 $HW64 \\
        --arm t133 --tag t133 --outdir "$OUT" 2>&1 | tee "$OUT/t133_run.log"
RC=\${PIPESTATUS[1]}
PRC=\$RC
t1=\$(date +%s)
echo "  [t133] 探针 rc=\$RC  用时 \$((t1-t0)) s"
case "\$RC" in
  0) echo "  ✓ 判据全中（B0 中 / B1 不中 / 最优臂 256²+64² 全中）" ;;
  4) echo "  △ 判据有未命中项（见上面汇总表）" ;;
  *) echo "  ✗✗ 探针 rc=\$RC（异常/超时/信号）⇒ 停、报";
     grep -E '^(Cma|Mem)' /proc/meminfo; sync; sync; exit 30 ;;
esac

md5sum /home/xilinx/demoir/$BIT53.bit | tee "$OUT/t133_bit_after.md5"

echo "=== ③-h 跑后凭据 ==="
grep -E '^(CmaTotal|CmaFree|MemFree)' /proc/meminfo
ls -la "$OUT" | head -50
du -sh "$OUT" 2>/dev/null || true
echo "  [t133] 收尾只 sync（不 poweroff）"
sync; sync
echo "  [t133] 仍开机：uptime=\$(uptime | sed 's/^ *//')"
echo "  [t133] rc=\$RC（探针判据 rc=\${PRC:-?}）"
exit \${PRC:-0}
EOS
RC=$?
echo "[t133] ③ 返回 rc=$RC"

# ---- ④ 无论成败都把产物拉回 ----
"${SCP[@]}" -r "$BOARD:$OUT/*" "$LOCAL_OUT/" \
  && echo "[t133] ④ 产物已拉回 $LOCAL_OUT" \
  || echo "[t133] ⚠ 产物拉回失败（板端 $OUT）"

echo "[t133] DONE rc=$RC"
exit "$RC"
