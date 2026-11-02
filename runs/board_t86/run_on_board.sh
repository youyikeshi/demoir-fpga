#!/usr/bin/env bash
# ============================================================================
#  runs/board_t86/run_on_board.sh —— T-86「v45：0x40080000 SIGBUS 是否消除 + HP2 冒烟」
#
#  唯一用法：
#      rm -f /tmp/dsh_board.loadtoken
#      BOARD_TAG=T86 BOARD_ALLOW_LOAD=1 tools/boardlock.sh bash runs/board_t86/run_on_board.sh
#
#  租约内：2 条 ssh + 2 条 scp（远低于 6 次/分闸门）——
#      ① ssh ：体检（uptime/磁盘/CMA/位流/pl_server/ps 关键 md5/空闲）+ 备份 ps
#      ② scp：ps 源码包 + 逐文件 md5 + 探针 + 板端执行体 + 依赖闭包清单
#              + **v45 的 .bit/.hwh 成对**（放 $DEST，不覆盖板端历史位流）
#      ③ ssh ：解包 → 两道门禁 → 空闲/成对 md5 → drop_caches → 跑探针（**一次 load**）
#              → 取证 → sync
#      ④ scp ：t86_out/ 整个拉回 runs/board_t86/
#
#  ★ load 令牌：`BOARD_ALLOW_LOAD=1` 在本租约内只签发一次；探针内**只有一次**
#    Overlay(download=True)（v45 的 .bit 与同目录自己的 .hwh）。
#  ★ 可能 SIGBUS 的访问全在 fork 出的子进程里 ⇒ 父进程能把判据完整落盘。
#
#  不做的事：不 poweroff（收尾只 sync）、不重试风暴、不起 Vivado、不删板端历史位流、
#            不改仓库代码、不上 v42*/v43base*/v43base2/v44。
# ============================================================================
set -uo pipefail

R="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOARD="${BOARD_HOST:-xilinx@192.168.7.2}"
SUDO_PASS="${BOARD_SUDO_PASS:-xilinx}"
DEST=/tmp/probe_T86
OUT=/home/xilinx/demoir/t86_out
LOCAL_OUT="$R/runs/board_t86"
FCLK="${T86_FCLK:-25}"
MD5_BIT=4e9853fbc69642b27943e34ba9481e1d
MD5_HWH=194562af193ecfa9553f2debc454daf9
MD5_MANIFEST=93155d7c28a62608b05a35ced6333775
MANIFEST="$R/runs/board_ops/20261102/deps_closure.txt"
BIT_A="$R/runs/v45_archive/design_1_150v45.bit"
HWH_A="$R/runs/v45_archive/design_1_150v45.hwh"
PROBE_MD5=$(md5sum "$R/runs/board_t86/t86_probe.py" | cut -d" " -f1)

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no
     -o ServerAliveInterval=20 -o ServerAliveCountMax=240 "$BOARD")
SCP=(scp -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)

mkdir -p "$LOCAL_OUT"
echo "[t86] repo  = $R"
echo "[t86] board = $BOARD    fclk 目标 = ${FCLK} MHz"
echo "[t86] 探针 md5 = $PROBE_MD5"
echo "[t86] v45 .bit md5 = $MD5_BIT   .hwh md5 = $MD5_HWH"

# ---- ⓪ 主机侧前置 md5 断言 + 打包 ps（排除 __pycache__ 与任何 .so）----
[ "$(md5sum "$BIT_A" | cut -d" " -f1)" = "$MD5_BIT" ] || { echo "[t86] ✗ v45 .bit md5 不符"; exit 15; }
[ "$(md5sum "$HWH_A" | cut -d" " -f1)" = "$MD5_HWH" ] || { echo "[t86] ✗ v45 .hwh md5 不符"; exit 15; }
[ "$(md5sum "$MANIFEST" | cut -d" " -f1)" = "$MD5_MANIFEST" ] || { echo "[t86] ✗ 依赖闭包清单 md5 不符"; exit 15; }
echo "[t86] ⓪ 主机侧三个 md5 断言全过"

cd "$R/pynq_specialized_code" || exit 10
find ps -type f ! -path "*__pycache__*" ! -name "*.so" | LC_ALL=C sort \
    > "$LOCAL_OUT/ps_t86_filelist.txt"
NF=$(wc -l < "$LOCAL_OUT/ps_t86_filelist.txt")
tar czf "$LOCAL_OUT/ps_t86_deploy.tgz" -T "$LOCAL_OUT/ps_t86_filelist.txt" \
    || { echo "[t86] ✗ 主机侧打包失败"; exit 10; }
xargs -a "$LOCAL_OUT/ps_t86_filelist.txt" md5sum > "$LOCAL_OUT/ps_md5_host.txt" \
    || { echo "[t86] ✗ 生成 md5 清单失败"; exit 10; }
echo "[t86] ⓪ 主机侧打包 $NF 个文件（$(du -h "$LOCAL_OUT/ps_t86_deploy.tgz" | cut -f1)）"

# ---- ① 板端体检 + 备份 ps ----
"${SSH[@]}" "mkdir -p '$DEST' '$OUT' && echo '--- uptime' && uptime \
  && echo '--- disk' && df -h / | tail -1 && df -i / | tail -1 \
  && echo '--- cma/mem' && grep -E '^(MemTotal|MemFree|MemAvailable|CmaTotal|CmaFree)' /proc/meminfo \
  && echo '--- 板端历史位流' && md5sum /home/xilinx/demoir/design_1_150v39.bit /home/xilinx/demoir/design_1_150v39.hwh 2>&1 \
  && echo '--- pl_server 状态（跑前）' && cat /usr/local/share/pynq-venv/lib/python3*/site-packages/pynq/pl_server/global_pl_state_.json 2>/dev/null | head -c 300; echo \
  && echo '--- 现有 ps 关键 md5' && cd /home/xilinx/demoir && md5sum ps/pynq/fconv.py ps/pynq/bin_engine.py ps/pynq/bin_net.py 2>&1 \
  && echo '--- 现有依赖闭包清单' && md5sum /home/xilinx/runs/board_ops/20261102/deps_closure.txt /home/xilinx/demoir/runs/board_ops/20261102/deps_closure.txt 2>&1 \
  && echo '--- busy check' && ps -eo pid,etime,cmd | grep -E '[p]ython3? .*(t4[0-9]|t5[0-9]|t8[0-9]|probe_|bin_net|board_|run_e)' | head -5 \
  && echo '--- 备份 ps' && ( [ -d ps ] && [ ! -d ps.bak_t86 ] && cp -a ps ps.bak_t86 && echo '  ✓ ps → ps.bak_t86' || echo '  (已存在或不存在，跳过)' ) \
  && echo '[t86] ① done'" \
  || { echo "[t86] ✗ 板端不可达/体检失败"; exit 11; }

# ---- ② 传（ps 包 + md5 清单 + 探针 + 板端执行体 + 依赖闭包清单 + v45 成对位流）----
"${SCP[@]}" "$LOCAL_OUT/ps_t86_deploy.tgz" "$LOCAL_OUT/ps_md5_host.txt" \
    "$R/runs/board_t86/t86_probe.py" "$R/runs/board_t86/t86_board.sh" \
    "$MANIFEST" "$BOARD:$DEST/" \
  || { echo "[t86] ✗ scp 小文件失败"; exit 12; }
"${SCP[@]}" "$BIT_A" "$HWH_A" "$BOARD:$DEST/" \
  || { echo "[t86] ✗ scp v45 成对位流失败"; exit 16; }
echo "[t86] ② scp done"

# ---- ③ 部署 + 门禁 + 跑（板端执行体；唯一一次 load）----
"${SSH[@]}" "env SUDO_PASS='$SUDO_PASS' DEST='$DEST' OUT='$OUT' \
    MD5_BIT='$MD5_BIT' MD5_HWH='$MD5_HWH' MD5_MANIFEST='$MD5_MANIFEST' \
    PROBE_MD5='$PROBE_MD5' FCLK='$FCLK' bash '$DEST/t86_board.sh'"
RC=$?
echo "[t86] ③ 返回 rc=$RC"

# ---- ④ 无论成败都把产物拉回（失败取证优先）----
"${SCP[@]}" -r "$BOARD:$OUT/*" "$LOCAL_OUT/" \
  && echo "[t86] ④ 产物已拉回 $LOCAL_OUT" \
  || echo "[t86] ⚠ 产物拉回失败（板端 $OUT）"

echo "[t86] DONE rc=$RC"
exit "$RC"
