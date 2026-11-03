#!/usr/bin/env bash
# ============================================================================
#  runs/board_t156/run_t156.sh —— T-156：MMIO 事务普查 + 轮询策略 A/B（ABBA）
#  一个租约 = 一个进程 = 一次 load。位流 = v39 冻结值 602725b8（md5 校验），
#  频率 = 150 MHz（读回），hw = 256²，slots = 24，**零护栏**。
#  跑：256² ×5（A B B A + 最后一跑开计时钩子做普查）→ 128²/64² 验 sha1
#
#  唯一用法（必须先删陈旧 load 令牌）：
#      rm -f /tmp/dsh_board.loadtoken
#      BOARD_ALLOW_LOAD=1 BOARD_TAG=T156 tools/boardlock.sh bash runs/board_t156/run_t156.sh
#
#  ★ 铁律：只按 pid `kill -TERM`；禁 `pkill -f`；不收尾 poweroff（只 sync; sync）；
#          ulimit -c 0；ssh ≤6/60s；ps/ 与 deps_closure.txt 一起同步 + md5sum -c + --check。
#  ★ RTL/PS 主线一字节不动（本目录只有探针 + 本脚本）。
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOARD="${BOARD_HOST:-xilinx@192.168.7.2}"
SUDO_PASS="${BOARD_SUDO_PASS:-xilinx}"
TAG="${TAG:-t156a}"
DEST="${DEST:-/tmp/probe_T156d}"
OUT="${OUT:-/home/xilinx/demoir/t156_out}"
LOCAL_OUT="${LOCAL_OUT:-$REPO/runs/board_t156}"
# ★ T-156：部署树 = `git archive HEAD` 快照 + **唯一一个** T-153 的 bin_net.py（0990948d）。
#   为什么：HEAD 里没有 T-153 那 169 行（未提交），而 5.464 s 的工作点**必须**含它。
#   已验证 144/144 与 T-153 实测部署逐文件 md5 相同（只差 .bak_* 本地备份）。
#   ⚠ 不要用 runs/board_t154/stage：那份被 T-154 的实验补丁污染过（bin_engine db8d40dd）。
PS_ROOT="${PS_ROOT:-$REPO/runs/board_t156/stage}"

BIT_DIR="${BIT_DIR:-$REPO/pynq_specialized_code/board/overlay_engine_150mhz}"
BIT="${BIT:-design_1_150v39}"
MD5_BIT="${MD5_BIT:-602725b86941864eeeeab3be005e1e81}"
MD5_HWH="${MD5_HWH:-c1686bd8e080c1c7f46181d980543202}"
# ★ 单变量锚：部署树里这两份必须是预期的
MD5_BIN_NET="${MD5_BIN_NET:-0990948de490eb6ba6d4aaefbcafa355}"
MD5_BIN_ENGINE="${MD5_BIN_ENGINE:-48548b8d588ece6d20de43a6901d284e}"

MANIFEST="$PS_ROOT/deps_closure.txt"
MD5_MANIFEST=$(md5sum "$MANIFEST" | cut -d" " -f1)
PROBE="${PROBE:-$REPO/runs/board_t156/probe_t156.py}"
PROBE_MD5=$(md5sum "$PROBE" | cut -d" " -f1)
PROBE_BASE=$(basename "$PROBE")

FREQ="${FREQ:-150.0}"
SLOTS="${SLOTS:-24}"
HW="${HW:-256}"
TMAX="${TMAX:-420}"
WD="${WD:-240}"

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no
     -o ServerAliveInterval=20 -o ServerAliveCountMax=180 "$BOARD")
SCP=(scp -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)

mkdir -p "$LOCAL_OUT"
echo "[$TAG] 位流 = $BIT_DIR/$BIT.bit md5=$MD5_BIT ；hwh=$MD5_HWH"
echo "[$TAG] 探针 md5 = $PROBE_MD5 ；清单 md5 = $MD5_MANIFEST"
echo "[$TAG] hw=$HW slots=$SLOTS fclk=$FREQ MHz 零护栏"

# ---- ⓪ 主机侧：冻结资产硬校验 + py_compile + 打包 ps ----
[ -f "$BIT_DIR/$BIT.bit" ] || { echo "[$TAG] ✗ 没有 $BIT_DIR/$BIT.bit"; exit 15; }
[ "$(md5sum "$BIT_DIR/$BIT.bit" | cut -d' ' -f1)" = "$MD5_BIT" ] \
  || { echo "[$TAG] ✗ $BIT.bit md5 不符冻结值"; exit 15; }
[ "$(md5sum "$BIT_DIR/$BIT.hwh" | cut -d' ' -f1)" = "$MD5_HWH" ] \
  || { echo "[$TAG] ✗ $BIT.hwh md5 不符冻结值"; exit 15; }
echo "[$TAG] ⓪ bit/hwh = 冻结值 ✓"

python3 -m py_compile "$PROBE" || { echo "[$TAG] ✗ 探针 py_compile 失败 ⇒ 不许上板"; exit 15; }
echo "[$TAG] ⓪ 探针 py_compile ✓"

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

# ---- ① 板端体检 + 按 pid 杀旧程序 ----
"${SSH[@]}" "bash -s" <<EOS1
set -u
ulimit -c 0
mkdir -p '$DEST' '$OUT' || exit 1
echo "--- uptime"; uptime
echo "--- disk"; df -h / | tail -1; df -i / | tail -1
echo "--- mem/cma"
grep -E '^(MemTotal|MemFree|MemAvailable|CmaTotal|CmaFree|SwapTotal|SwapFree)' /proc/meminfo
echo "--- 现存 python（pgrep -x python3）"
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
ls -l /home/xilinx/demoir/design_1_150v*.bit 2>/dev/null | tail -6 || true
echo "--- PACK 目录"
ls -d /home/xilinx/demoir/deploy/bin_weights_new 2>/dev/null || echo "  ✗ 缺 PACK"
md5sum /home/xilinx/demoir/deploy/bin_weights_new/index.json 2>/dev/null | head -1 || true
echo "--- 真 python"
ls -l /usr/local/share/pynq-venv/bin/python3 2>&1 | head -2
echo "--- 腾 CMA：drop_caches 前后"
grep -E '^CmaFree' /proc/meminfo
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null && echo "  ✓ drop_caches" || echo "  ⚠ drop_caches 失败"
sleep 1
grep -E '^CmaFree' /proc/meminfo
echo "[$TAG] ① done"
EOS1
if [ $? -ne 0 ]; then echo "[$TAG] ✗ 板端体检失败（不重试风暴）"; exit 11; fi

# ---- ② scp ----
SCPFILES=("$PROBE" "$MANIFEST" "$BIT_DIR/$BIT.bit" "$BIT_DIR/$BIT.hwh"
          "$LOCAL_OUT/ps_${TAG}_deploy.tgz" "$LOCAL_OUT/ps_md5_host.txt")
"${SCP[@]}" "${SCPFILES[@]}" "$BOARD:$DEST/" \
  || { echo "[$TAG] ✗ scp 失败"; exit 12; }
echo "[$TAG] ② scp done（${#SCPFILES[@]} 个文件）"

# ---- ③ 部署 + 门禁 + 探针（单进程、单次 load）----
"${SSH[@]}" "bash -s" <<EOS
set -u
ulimit -c 0
cd /home/xilinx/demoir || exit 20

echo "=== ③-a 位流落位 + md5 ==="
cp -f "$DEST/$BIT.bit" /home/xilinx/demoir/$BIT.bit || exit 21
cp -f "$DEST/$BIT.hwh" /home/xilinx/demoir/$BIT.hwh || exit 21
for pair in "$BIT.bit:$MD5_BIT" "$BIT.hwh:$MD5_HWH"; do
    f=\${pair%%:*}; want=\${pair##*:}
    got=\$(md5sum "/home/xilinx/demoir/\$f" | awk '{print \$1}')
    echo "  \$f = \$got （期望 \$want）"
    [ "\$got" = "\$want" ] || { echo "  ✗ \$f md5 不符 ⇒ 停"; exit 21; }
done
echo "  ✓ bit/hwh = 冻结值，成对"

echo "=== ③-b ps 源码（逐文件 md5）==="
tar xzf "$DEST/ps_${TAG}_deploy.tgz" || { echo "  ✗ 解包失败"; exit 21; }
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' rm -rf ps/pynq/__pycache__ ps/tools/__pycache__ 2>/dev/null || true
echo "  解包完成（\$(find ps -type f ! -path '*__pycache__*' | wc -l) 个文件）"
if md5sum -c "$DEST/ps_md5_host.txt" > "$OUT/ps_md5_board_check.txt" 2>&1; then
    echo "  ✓ md5 -c 全过：\$(grep -c ': OK' "$OUT/ps_md5_board_check.txt") 个文件"
else
    echo "  ✗ md5 -c 失败"; grep ': FAILED' "$OUT/ps_md5_board_check.txt" | head -20; exit 22
fi

echo "=== ③-b2 单变量锚：关键文件 md5（HEAD+T-153 bin_net）==="
for pair in "ps/pynq/bin_net.py:$MD5_BIN_NET" \
            "ps/pynq/bin_engine.py:$MD5_BIN_ENGINE"; do
    f=\${pair%%:*}; want=\${pair##*:}
    got=\$(md5sum "\$f" | awk '{print \$1}')
    echo "  \$f = \$got （期望 \$want）"
    [ "\$got" = "\$want" ] || { echo "  ✗ \$f md5 不符 ⇒ 停（不许拿错部署做实验）"; exit 24; }
done

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
echo "  单进程 一次 load：v39 @ $FREQ MHz ；hw=$HW ；零护栏；LAZY=1 + 四刀"
echo "  臂：256² A B B A（A=自适应/B=tight）+ 智查跑 → 128²/64² sha1"
echo "==================================================================="
EPOCH=\$(date +%s.%N 2>/dev/null); case "\$EPOCH" in *N*) EPOCH=\$(date +%s);; esac; echo "  epoch=\$EPOCH"
t0=\$(date +%s)
printf '%s\n' "$SUDO_PASS" | sudo -S -p '' env XILINX_XRT=/usr \\
    "\$PY0" -u "$DEST/$PROBE_BASE" \\
        --bit /home/xilinx/demoir/$BIT.bit \\
        --pack /home/xilinx/demoir/deploy/bin_weights_new \\
        --freq $FREQ --slots $SLOTS --hw $HW --epoch "\$EPOCH" \\
        --wd $WD --tmax $TMAX --tag $TAG --outdir "$OUT" \\
        ${EXTRA_ARGS:-} 2>&1 | tee "$OUT/${TAG}_run.log"
RC=\${PIPESTATUS[1]}
PRC=\$RC
t1=\$(date +%s)
echo "  [$TAG] 探针 rc=\$RC  用时 \$((t1-t0)) s"
case "\$RC" in
  0) echo "  ✓ 各跑三 sha1 全中参照" ;;
  4) echo "  △ 有未命中项（见汇总表）" ;;
  *) echo "  ✗✗ 探针 rc=\$RC（异常/超时/信号）⇒ 停、报";
     grep -E '^(Cma|Mem)' /proc/meminfo; sync; sync; exit 30 ;;
esac

md5sum /home/xilinx/demoir/$BIT.bit | tee "$OUT/${TAG}_bit_after.md5"

echo "=== ③-h 跑后凭据 ==="
grep -E '^(CmaTotal|CmaFree|MemFree)' /proc/meminfo
ls -la "$OUT" | head -30
echo "  [$TAG] 收尾只 sync（不 poweroff）"
sync; sync
echo "  [$TAG] 仍开机：uptime=\$(uptime | sed 's/^ *//')"
exit \${PRC:-0}
EOS
RC=$?
echo "[$TAG] ③ 返回 rc=$RC"

# ---- ④ 无论成败都把产物拉回 ----
"${SCP[@]}" -r "$BOARD:$OUT/*" "$LOCAL_OUT/" \
  && echo "[$TAG] ④ 产物已拉回 $LOCAL_OUT" \
  || echo "[$TAG] ⚠ 产物拉回失败（板端 $OUT）"

echo "[$TAG] DONE rc=$RC"
exit "$RC"
