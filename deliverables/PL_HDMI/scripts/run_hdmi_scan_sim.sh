#!/usr/bin/env bash
# ============================================================================
#  run_hdmi_scan_sim.sh —— hdmi_fb_scan 的 xsim 一键跑（按任务书要求）
#
#  · 跑前 **rm -rf xsim.dir**（任务书硬要求）
#  · xelab **-debug off**（不落 wdb）
#  · 工作目录放 /tmp（不污染仓库）
#  · 不碰 vivado 批处理锁（xsim 不算综合作业），但用 nice 让路
#
#  用法： bash tools/run_hdmi_scan_sim.sh [case...]
#         默认 case 1 2 3
# ============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$ROOT/../../.." && pwd)"
WORK="${WORK:-/tmp/probe_hdmi_p2/xsim_hdmi}"
VECDIR="${VECDIR:-$REPO/runs/hdmi_p2}"
NCASE="${NCASE:-$(ls "$VECDIR"/case*_par.txt 2>/dev/null | wc -l)}"
[[ "${NCASE:-0}" -gt 0 ]] || { echo "没有 case*_par.txt，先跑生成脚本" >&2; exit 2; }

# ---- Vivado 环境 ----
if ! command -v xvlog >/dev/null 2>&1; then
  for cand in "${XILINX_VIVADO:-}/settings64.sh" \
              /data/xilinx/2025.2/2025.2.1/Vivado/settings64.sh \
              /data/xilinx/2025.2/Vivado/settings64.sh; do
    if [[ -n "$cand" && -f "$cand" ]]; then
      set +u; source "$cand"; set -u; break
    fi
  done
fi
command -v xvlog >/dev/null || { echo "找不到 xvlog（Vivado 环境没装上）" >&2; exit 3; }
command -v python3 >/dev/null || { echo "找不到 python3" >&2; exit 3; }

mkdir -p "$VECDIR"
echo "=== 0/4 生成黄金向量（compose() 作黄金）==="
python3 "$HERE/gen_hdmi_scan_golden.py" --out "$VECDIR" || exit 1

echo "=== 1/4 清工作目录（rm -rf xsim.dir 是硬要求）==="
rm -rf "$WORK"
mkdir -p "$WORK"
cd "$WORK"

echo "=== 2/4 xvlog ==="
nice -n 10 timeout -k 5 600 xvlog --sv -L xpm \
     "$ROOT/modules/hdmi_fb_scan.v" "$ROOT/tb/tb_hdmi_fb_scan.v" \
     > xvlog.log 2>&1
RC=$?
if [[ $RC -ne 0 ]]; then
  echo "❌ xvlog 失败（$WORK/xvlog.log）"; grep -E 'ERROR|CRITICAL' xvlog.log | head -20; exit 1
fi
grep -E 'ERROR|WARNING' xvlog.log | head -10 || true
echo "  xvlog ok（$(ls -la xsim.dir 2>/dev/null | head -1 | awk '{print $NF}')）"

echo "=== 3/4 xelab (-debug off) ==="
nice -n 10 timeout -k 5 900 xelab -debug off --relax -L xpm \
     -s sim_hdmi_scan tb_hdmi_fb_scan > xelab.log 2>&1
RC=$?
if [[ $RC -ne 0 ]]; then
  echo "❌ xelab 失败（$WORK/xelab.log）"; grep -E 'ERROR|CRITICAL' xelab.log | head -20; exit 1
fi
echo "  xelab ok"

echo "=== 4/4 xsim ==="
nice -n 10 timeout -k 10 900 xsim sim_hdmi_scan -R \
     -testplusarg "VECDIR=$VECDIR" -testplusarg "NCASES=$NCASE" > sim.log 2>&1
RC=$?
grep -E '^\[TB\]|===== |\[ERR\]' sim.log || tail -30 sim.log

if grep -q '===== PASS =====' sim.log && [[ $RC -eq 0 ]]; then
  echo
  echo "✅ tb_hdmi_fb_scan PASS（日志 $WORK/sim.log）"
  exit 0
fi
echo
echo "❌ tb_hdmi_fb_scan FAIL（日志 $WORK/sim.log）"
exit 1
