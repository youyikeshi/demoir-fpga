#!/bin/sh
# host_dryrun.sh —— T-163 run100.py 的**主机空跑**验证（不需要板子；不碰 PL/CMA/相机）
#
# 跑法：sh host_dryrun.sh     ⇒ 覆盖 logs/host_dryrun.log
# 五个场景（顺序固定，都是为了把某一类路径走通）：
#   S1 控制流：fake 引擎 + 真 DisplayPipeline(假寄存器) + 20 帧环形读 → JSON 字段/追加/内存曲线
#   S2 真引擎：BinNet(engine=None) numpy 参考 @64² ×20 帧 → run()/BV_FREE/materialize/释放路径
#   S3 对照：同上但 BV_FREE=0 → 证明"内存曲线平"是 BV_FREE=1 挣来的，不是环境噪声
#   S4 分辨率：@128² ×6 帧 → 中间张量 4× 时曲线是否仍平坦
#   S5 注入：看门狗（--test-leak-mb 3 --rss-growth-mb 1）与 0x2C（--test-0x2c-bump-at 3）
set -u
cd "$(dirname "$0")" || exit 1
PACK=../../runs/opt_round1/ps_numeric/pack_v60
IN=../../runs/frozen256
OUT=logs/_dryrun_work
LOG=logs/host_dryrun.log
rm -rf "$OUT"
mkdir -p "$OUT"

{
  echo "==============================================================="
  echo " T-163 run100.py 主机空跑   $(date '+%F %T')"
  echo " host=$(uname -sr)  python=$(python3 -V 2>&1)  cpu=$(nproc)"
  echo " BV_FREE 默认值 = 1（run100.py 内 os.environ.setdefault）"
  echo "==============================================================="

  echo
  echo "==============================================================="
  echo " S1  控制流 + JSON 字段 + 内存曲线（fake 引擎，20 帧，环形复用 5 张输入）"
  echo "==============================================================="
  python3 run100.py --dry-run --inputs "$IN" --frames 20 \
      --out "$OUT/s1_fake20" 2>&1
  echo "--- S1 frames.jsonl 第 1 行（字段清单）---"
  head -1 "$OUT/s1_fake20/frames.jsonl"
  echo "--- S1 行数（应 = 20）---"
  wc -l < "$OUT/s1_fake20/frames.jsonl"

  echo
  echo "==============================================================="
  echo " S2  真引擎路径：BinNet(engine=None) numpy 参考 @64² ×20 帧  BV_FREE=1"
  echo "==============================================================="
  python3 run100.py --dry-run --engine ref --shape 64 --frames 20 --warmup-frames 0 \
      --pack "$PACK" --inputs "$IN" --out "$OUT/s2_ref64" 2>&1

  echo
  echo "==============================================================="
  echo " S3  对照：同 S2 但 BV_FREE=0（--allow-bv-free0 显式放行）"
  echo "     预期：VmRSS 每帧上涨，20 帧后不再平坦 —— 这就是 726 MB 的形状"
  echo "==============================================================="
  BV_FREE=0 python3 run100.py --dry-run --allow-bv-free0 --engine ref --shape 64 \
      --frames 20 --warmup-frames 0 --rss-growth-mb 100000 \
      --pack "$PACK" --inputs "$IN" --out "$OUT/s3_ref64_bvfree0" 2>&1

  echo
  echo "==============================================================="
  echo " S4  分辨率：@128² ×6 帧（中间张量 4×）BV_FREE=1 ⇒ 是否仍平坦"
  echo "==============================================================="
  python3 run100.py --dry-run --engine ref --shape 128 --frames 6 --warmup-frames 1 \
      --pack "$PACK" --inputs "$IN" --out "$OUT/s4_ref128" 2>&1

  echo
  echo "==============================================================="
  echo " S5a 注入：看门狗（故意每帧漏 3 MB，阈值调到 1 MB）⇒ 必须写 aborted.json"
  echo "==============================================================="
  python3 run100.py --dry-run --inputs "$IN" --frames 8 --test-leak-mb 3 \
      --rss-growth-mb 1 --out "$OUT/s5a_leak" 2>&1
  echo "--- S5a aborted.json（reason/frame/last3）---"
  python3 -c "import json,sys;d=json.load(open('$OUT/s5a_leak/aborted.json'));print(json.dumps({k:d[k] for k in ('reason','frame','frames_done','thresholds','last3','reg_2c_seq')},ensure_ascii=False,indent=1))"

  echo
  echo "==============================================================="
  echo " S5b 注入：0x2C 增量 > 0（第 3 帧后 +1）⇒ 必须立即停并写 aborted.json"
  echo "==============================================================="
  python3 run100.py --dry-run --inputs "$IN" --frames 8 --test-0x2c-bump-at 3 \
      --out "$OUT/s5b_2c" 2>&1
  echo "--- S5b aborted.json ---"
  python3 -c "import json,sys;d=json.load(open('$OUT/s5b_2c/aborted.json'));print(json.dumps({k:d[k] for k in ('reason','frame','reg_2c_base','reg_2c_seq')},ensure_ascii=False,indent=1))"

  echo
  echo "==============================================================="
  echo " 汇总：各场景 VmRSS 曲线（首帧 → 末帧）"
  echo "==============================================================="
  for d in s1_fake20 s2_ref64 s3_ref64_bvfree0 s4_ref128 s5a_leak s5b_2c; do
    python3 - "$OUT/$d" "$d" <<'PY'
import json, os, sys
d, name = sys.argv[1], sys.argv[2]
p = os.path.join(d, "frames.jsonl")
rows = [json.loads(l) for l in open(p)]
rss = [r["rss_mb"] for r in rows]
ab = os.path.join(d, "aborted.json")
tag = "ABORTED" if os.path.exists(ab) else "done"
print("%-18s frames=%2d  rss %7.1f → %7.1f  (Δ %+6.1f MB, max %7.1f)  %s"
      % (name, len(rows), rss[0], rss[-1], rss[-1] - rss[0], max(rss), tag))
PY
  done
  echo
  echo "done $(date '+%F %T')"
} 2>&1 | tee "$LOG"
