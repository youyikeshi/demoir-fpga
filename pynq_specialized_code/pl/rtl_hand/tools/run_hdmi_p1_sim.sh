#!/usr/bin/env bash
# ============================================================================
# run_hdmi_p1_sim.sh —— HDMI P1 的 xsim 运行器
#   用法: ./tools/run_hdmi_p1_sim.sh timing|i2c|topcfg|all
#   规则: 每次运行前 rm -rf xsim.dir；xelab 用 -debug off（不落 GB 级 wdb）
#   顶层含 BUFG/BUFGMUX/IOBUF（unisim）⇒ xelab 加 -L unisims_ver 并链接 glbl
# ============================================================================
set -uo pipefail

MODE="${1:-timing}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VIV="${XILINX_VIVADO:-/data/xilinx/2025.2/2025.2.1/Vivado}"
WORK="${WORK:-/tmp/probe_hdmi_p1/xsim_$MODE}"

if ! command -v xvlog >/dev/null 2>&1; then
  if [[ -f "$VIV/settings64.sh" ]]; then
    set +u
    # shellcheck disable=SC1090
    source "$VIV/settings64.sh" >/dev/null 2>&1
    set -u
  fi
fi
command -v xvlog >/dev/null || { echo "xvlog not found"; exit 3; }

GLBL="$(cd "$(dirname "$(command -v xvlog)")/.." && pwd)/data/verilog/src/glbl.v"
[[ -f "$GLBL" ]] || GLBL="$VIV/data/verilog/src/glbl.v"
[[ -f "$GLBL" ]] || { echo "glbl.v not found"; exit 3; }

run_one() {
  local name="$1"; shift
  local top="$1"; shift
  local files=("$@")
  local w="$WORK/$name"
  rm -rf "$w"; mkdir -p "$w"; cd "$w" || exit 4
  rm -rf xsim.dir
  echo "=== [$name] xvlog ==="
  if ! xvlog --nolog -L unisims_ver "${files[@]}" > xvlog.log 2>&1; then
    echo "xvlog FAILED"; tail -40 xvlog.log; return 1
  fi
  echo "=== [$name] xelab ==="
  if ! xelab --nolog -debug off --relax -L unisims_ver -s "sim_$name" $top > xelab.log 2>&1; then
    echo "xelab FAILED"; tail -40 xelab.log; return 1
  fi
  echo "=== [$name] xsim ==="
  xsim --nolog -R "sim_$name" 2>&1 | tee xsim.log
  local rc=${PIPESTATUS[0]}
  if grep -q "RESULT: PASS" xsim.log && ! grep -q "RESULT: FAIL" xsim.log && ! grep -q "RESULT: TIMEOUT" xsim.log; then
    echo "[$name] PASS"
    return 0
  fi
  echo "[$name] FAIL (rc=$rc, log=$w/xsim.log)"
  return 1
}

RC=0
case "$MODE" in
  timing)
    run_one timing tb_hdmi_timing \
      "$ROOT/modules/hdmi_timing_gen.v" "$ROOT/tb/tb_hdmi_timing.v" || RC=1
    ;;
  i2c)
    run_one i2c tb_sil9134_i2c \
      "$ROOT/modules/sil9134_init.v" "$ROOT/tb/tb_sil9134_i2c.v" || RC=1
    ;;
  topcfg)
    run_one topcfg "tb_hdmi_top_cfg glbl" \
      "$ROOT/modules/hdmi_timing_gen.v" "$ROOT/modules/hdmi_color_bar.v" \
      "$ROOT/modules/sil9134_init.v" "$ROOT/modules/hdmi_out_top.v" \
      "$GLBL" "$ROOT/tb/tb_hdmi_top_cfg.v" || RC=1
    ;;
  all)
    run_one timing tb_hdmi_timing \
      "$ROOT/modules/hdmi_timing_gen.v" "$ROOT/tb/tb_hdmi_timing.v" || RC=1
    run_one i2c tb_sil9134_i2c \
      "$ROOT/modules/sil9134_init.v" "$ROOT/tb/tb_sil9134_i2c.v" || RC=1
    run_one topcfg "tb_hdmi_top_cfg glbl" \
      "$ROOT/modules/hdmi_timing_gen.v" "$ROOT/modules/hdmi_color_bar.v" \
      "$ROOT/modules/sil9134_init.v" "$ROOT/modules/hdmi_out_top.v" \
      "$GLBL" "$ROOT/tb/tb_hdmi_top_cfg.v" || RC=1
    ;;
  *)
    echo "usage: $0 timing|i2c|topcfg|all"; exit 2
    ;;
esac

exit $RC
