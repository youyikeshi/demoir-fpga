#!/usr/bin/env bash
# 一键采集一轮：选 4 张源图 → 重新标定（位置+曝光+颜色）→ 采 20 对 → 生成 GT → 开预览
#
# 用法：
#   ./run_round.sh                 # 自动接续下一轮，距离/角度沿用上次
#   ./run_round.sh 2 68 0          # 第 2 轮，距离 68cm，角度 0°
#   ./run_round.sh 2 68 5 "换5cm远一点"
#   ./run_round.sh preview         # 只把实时预览推到设备屏（调焦用）
#   ./run_round.sh plan            # 只打印本轮计划，不采集
#
# 环境变量可覆盖：SRC_ALL（源图池） BASE（输出根目录） HOST（设备IP） DAMP（颜色校正强度）
set -u

WS="/media/felix/892G/ai/maxicam"
PC="$WS/apps/pc"
export PYTHONPATH="$WS/env/receiver/lib/python3.10/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"

SRC_ALL="${SRC_ALL:-$WS/_push/all_gt}"
BASE="${BASE:-$WS/runs}"
HOST="${HOST:-10.13.148.1}"
DAMP="${DAMP:-0.85}"
RES="${RES:-2560x1440}"   # 不要改！换分辨率=换一种摩尔纹

log() { printf '[一键] %s\n' "$*"; }

next_round() {
  local n=0 b m
  for d in "$BASE"/round*; do
    [ -d "$d" ] || continue
    b="$(basename "$d")"; m="${b#round}"
    case "$m" in ''|*[!0-9]*) continue;; esac
    [ "$((10#$m))" -gt "$n" ] && n=$((10#$m))
  done
  echo $((n + 1))
}

last_geometry() {
  # 从上一轮的 round.json 里读距离/角度，作为默认值
  local prev
  prev=$(ls -d "$BASE"/round* 2>/dev/null | sort -V | tail -1)
  if [ -n "${prev:-}" ] && [ -f "$prev/round.json" ]; then
    python3 - "$prev/round.json" <<'PY'
import json,sys
try:
    g=json.load(open(sys.argv[1])).get("geometry",{})
    print(g.get("distance_cm",0), g.get("angle_deg",0))
except Exception:
    print("0 0")
PY
  else
    echo "0 0"
  fi
}

# 前置模式标志：preview / plan，其余按位置参数解析
MODE="run"
case "${1:-}" in
  preview) MODE="preview"; shift ;;
  plan)    MODE="plan";    shift ;;
esac

if [ "$MODE" = "preview" ]; then
  log "把实时预览推到设备屏（调焦/摆位用）"
  python3 "$PC/send_cmd.py" start --host "$HOST" --seconds 3
  exit $?
fi

read -r LAST_D LAST_A <<EOF
$(last_geometry)
EOF

ROUND="${1:-$(next_round)}"
case "$ROUND" in ''|*[!0-9]*) log "轮次必须是数字（例如 2）；模式只有 preview / plan"; exit 2;; esac
DIST="${2:-${LAST_D:-0}}"
ANGLE="${3:-${LAST_A:-0}}"
NOTE="${4:-}"

OUT="$BASE/round$(printf '%02d' "$ROUND")"

log "第 $ROUND 轮"
log "源图池 : $SRC_ALL"
log "输出   : $OUT"
log "几何   : 距离 ${DIST}cm，角度 ${ANGLE}°${NOTE:+，备注：$NOTE}"

if [ "$MODE" = "plan" ]; then
  START=$(( (ROUND - 1) * 2 ))
  log "--- 计划（未采）---"
  log "源图窗口: 第 $((START+1)) ~ $((START+4)) 张"
  ls "$SRC_ALL" 2>/dev/null | sort | sed -n "$((START+1)),$((START+4))p" | sed 's/^/         /'
  log "每张 5 个随机不重复位置 ⇒ 20 对"
  exit 0
fi

if [ ! -d "$SRC_ALL" ]; then log "源图池不存在：$SRC_ALL"; exit 2; fi
mkdir -p "$BASE"

log "① 重新标定位置 → ② 锁曝光 → ③ 颜色标定 → ④ 采集 20 对"
python3 "$PC/round_capture.py" \
  --all "$SRC_ALL" --base "$BASE" --round "$ROUND" \
  --res "$RES" --distance "$DIST" --angle "$ANGLE" --note "$NOTE" --host "$HOST"
rc=$?
if [ $rc -ne 0 ]; then log "采集失败（返回码 $rc），未生成 GT"; exit $rc; fi

log "⑤ 生成 GT + 颜色校正（阻尼 $DAMP）"
python3 "$PC/extract_pairs.py" --run "$OUT" --color --color-damp "$DAMP"
rc=$?
if [ $rc -ne 0 ]; then log "GT 生成失败（返回码 $rc）"; exit $rc; fi

# 汇总
python3 - "$OUT" <<'PY'
import json,sys,glob,os
out=sys.argv[1]
pairs=sorted(glob.glob(os.path.join(out,"pairs","*_cam.png")))
valid=[]
for m in glob.glob(os.path.join(out,"pairs","*.json")):
    try: valid.append(json.load(open(m)).get("gt_valid",0))
    except Exception: pass
print("[一键] ===== 本轮完成 =====")
print("[一键] 目录      :", out)
print("[一键] 有效对    :", len(pairs))
print("[一键] GT 有效区 :", ("%.3f" % min(valid)) if valid else "n/a")
for f in ("session.json","round.json","index.jsonl"):
    p=os.path.join(out,f)
    print("[一键] %-12s: %s" % (f, "有" if os.path.exists(p) else "缺"))
PY
log "预览已推回设备屏，可继续调焦；调好后运行 ./run_round.sh 开下一轮"
