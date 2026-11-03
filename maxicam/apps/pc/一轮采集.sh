#!/usr/bin/env bash
# 双击/终端启动：向导式跑一轮采集
#   检查设备 → 显示本轮计划 → 问距离/角度 → 采集 → 汇总
set -u

WS="/media/felix/892G/ai/maxicam"
PC="$WS/apps/pc"
BASE="${BASE:-$WS/runs}"
SRC_ALL="${SRC_ALL:-$WS/_push/all_gt}"
HOST="${HOST:-10.13.148.1}"
cd "$PC" || exit 1

line() { printf '%s\n' "───────────────────────────────────────────────────────"; }

clear 2>/dev/null
echo "═══════════════════════════════════════════════════════"
echo "              摩尔纹采集 · 一轮 20 对"
echo "═══════════════════════════════════════════════════════"

# ---- 1) 设备检查 ----
DEV_OK=1
for port in 8555 8556; do
  timeout 2 bash -c "echo >/dev/tcp/$HOST/$port" 2>/dev/null || DEV_OK=0
done
if [ "$DEV_OK" = "1" ]; then
  echo " 设备 $HOST   ✓ 在线"
else
  echo " 设备 $HOST   ✗ 离线"
  line
  echo " 请在板子上点开「视频推流」，等它显示出画面后再运行本脚本。"
  echo
  read -r -p " 按回车重新检测（Ctrl+C 退出）…" _
  for port in 8555 8556; do
    timeout 2 bash -c "echo >/dev/tcp/$HOST/$port" 2>/dev/null || { echo " 仍然连不上，退出。"; read -r -p "按回车关闭…" _; exit 1; }
  done
  echo " 设备已上线 ✓"
fi

# ---- 2) 上一轮信息 + 本轮计划 ----
INFO=$(python3 - "$BASE" <<'PY'
import json,sys,glob,os,re
base=sys.argv[1]
rounds=[]
for d in glob.glob(os.path.join(base,"round*")):
    m=re.search(r"round(\d+)$", d)
    if m: rounds.append((int(m.group(1)), d))
rounds.sort()
last=rounds[-1] if rounds else None
nxt=(last[0]+1) if last else 1
ldist=langle=0
lnote=""
if last:
    try:
        rj=json.load(open(os.path.join(last[1],"round.json")))
        g=rj.get("geometry",{})
        ldist=g.get("distance_cm",0); langle=g.get("angle_deg",0)
        lnote=rj.get("note","")
    except Exception: pass
print(nxt, last[0] if last else 0, ldist, langle)
PY
)
NEXT=$(echo "$INFO" | awk '{print $1}')
LASTN=$(echo "$INFO" | awk '{print $2}')
LDIST=$(echo "$INFO" | awk '{print $3}')
LANGLE=$(echo "$INFO" | awk '{print $4}')

START=$(( (NEXT - 1) * 2 ))
IMGS=$(ls "$SRC_ALL" 2>/dev/null | sort | sed -n "$((START+1)),$((START+4))p" | tr '\n' ' ')

if [ "$LASTN" != "0" ]; then
  echo " 上一轮 第 $LASTN 轮   距离 ${LDIST}cm   角度 ${LANGLE}°"
fi
line
echo " 本轮  第 $NEXT 轮   源图 $IMGS"
echo "       每张 5 个随机不重复位置 ⇒ 共 20 对"
line
echo
echo " 动手改过相机位置的话，把新的距离/角度填一下（直接回车=沿用上次）"
echo
read -r -p " 距离(cm) [${LDIST}]: " DIST;  DIST="${DIST:-$LDIST}"
read -r -p " 角度(°)  [${LANGLE}]: " ANGLE; ANGLE="${ANGLE:-$LANGLE}"
read -r -p " 备注      []: " NOTE
echo
line
echo " 即将开始：第 $NEXT 轮，距离 ${DIST}cm，角度 ${ANGLE}°${NOTE:+，备注 $NOTE}"
echo " 流程：重新标定位置 → 锁曝光 → 颜色标定 → 采集 20 对 → 生成 GT"
echo " ⚠️ 采集过程中 ESC/q 会被忽略一次，需连按两次才会中止"
line
read -r -p " 回车开始（Ctrl+C 取消）…" _ || { echo; echo " 已取消，未采集。"; exit 0; }

# ---- 3) 跑 ----
./run_round.sh "$NEXT" "$DIST" "$ANGLE" "$NOTE"
RC=$?

echo
line
if [ $RC -eq 0 ]; then
  echo " ✅ 完成。数据在：$BASE/round$(printf '%02d' "$NEXT")"
  echo
  echo " 下一步（换个距离/角度让摩尔纹频率变掉，再跑一次本脚本）："
  echo "   ./一轮采集.sh"
else
  echo " ❌ 失败（返回码 $RC），看上面日志。设备屏若黑掉，重开一次「视频推流」。"
fi
line
read -r -p " 按回车关闭窗口…" _
