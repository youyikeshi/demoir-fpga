#!/usr/bin/env bash
# ============================================================================
#  selftest.sh —— 验证设施自测（不依赖任何真实卷积模块）
#
#  用 tools/selftest/fake_engine.v 写「全 0」输出，与 golden/_selftest_zero
#  的 expected_c8.bin（全 0）对拍，验证三条链路：
#    ① PASS 链路：fake 写全 0            → run_module.sh 退出 0，byte=N/N
#    ② FAIL 链路：fake 在第 12345 字节写 1 → run_module.sh 退出 1，
#                 并打印 @byte=12345 / 期望=0 / 实际=1
#    ③ fuzz 循环：同一个 golden 目录重复 5 次 → 一次 xsim 内跑 5 组，5/5 PASS
#
#  用法： ./tools/selftest.sh
#  退出码：0 = 自测全部符合预期；1 = 有断言不成立。
# ============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SELF="$HERE/selftest"
TS="$(date +%Y%m%d_%H%M%S)"
EVID="$ROOT/reports/selftest_${TS}.md"
TMP="$(mktemp -d /tmp/rtl_hand_selftest.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "❌ 自测失败：$*" >&2; exit 1; }

echo "=== 0/3 准备全 0 自测向量 ==="
if [[ ! -f "$ROOT/golden/_selftest_zero/expected_c8.bin" ]]; then
  timeout 120 python3 "$HERE/make_golden.py" --zeros --shape k5_12x48 --name _selftest_zero \
      > "$TMP/make_golden.log" 2>&1 || { cat "$TMP/make_golden.log"; fail "生成 _selftest_zero"; }
fi
ZERO="$(realpath "$ROOT/golden/_selftest_zero")"
for i in 1 2 3 4 5; do echo "$ZERO"; done > "$TMP/manifest.txt"

run_case() {  # run_case <tag> <tb> <extra args...>
  local tag="$1" tb="$2"; shift 2
  "$HERE/run_module.sh" "fake_${tag}" k5_12x48 \
      --dut "$SELF/fake_engine.v" --tb "$tb" --top tb_fake_engine \
      --golden "$ZERO" "$@" > "$TMP/${tag}.log" 2>&1
  echo $?
}

echo "=== 1/3 PASS 链路（fake 写全 0）==="
RC="$(run_case ok "$SELF/tb_fake_ok.v")"
[[ "$RC" -eq 0 ]] || { tail -30 "$TMP/ok.log"; fail "PASS 链路退出码 $RC（期望 0）"; }
grep -q '\[TB-SUMMARY\].*bytes=30720/30720.*status=PASS' "$TMP/ok.log" \
  || { tail -30 "$TMP/ok.log"; fail "PASS 链路 summary 不含 bytes=30720/30720 status=PASS"; }
echo "   ✓ 退出 0，30,720/30,720 字节一致"

echo "=== 2/3 FAIL 链路（fake 在第 12345 字节写 1）==="
RC="$(run_case bad1 "$SELF/tb_fake_bad1.v")"
[[ "$RC" -eq 1 ]] || { tail -30 "$TMP/bad1.log"; fail "FAIL 链路退出码 $RC（期望 1）"; }
grep -q '\[BYTE-CHECK\].*@byte=12345.*期望=0(0x00) 实际=1(0x01)' "$TMP/bad1.log" \
  || { tail -30 "$TMP/bad1.log"; fail "FAIL 链路没有打印第 12345 字节的期望/实际"; }
grep -q '\[TB-SUMMARY\].*bytes=30719/30720.*status=FAIL' "$TMP/bad1.log" \
  || { tail -30 "$TMP/bad1.log"; fail "FAIL 链路 summary 不含 bytes=30719/30720 status=FAIL"; }
echo "   ✓ 退出 1，精确报出 byte=12345 期望=0 实际=1"

echo "=== 3/3 fuzz 循环（5 组，一次 xsim）==="
RC="$("$HERE/run_module.sh" fake_fuzz k5_12x48 \
      --dut "$SELF/fake_engine.v" --tb "$SELF/tb_fake_ok.v" --top tb_fake_engine \
      --fuzz-manifest "$TMP/manifest.txt" > "$TMP/fuzz.log" 2>&1; echo $?)"
[[ "$RC" -eq 0 ]] || { tail -30 "$TMP/fuzz.log"; fail "fuzz 循环退出码 $RC（期望 0）"; }
grep -q '\[TB-SUMMARY\].*bytes=153600/153600.*cases=5/5.*status=PASS' "$TMP/fuzz.log" \
  || { tail -30 "$TMP/fuzz.log"; fail "fuzz 循环 summary 不含 153600/153600 cases=5/5"; }
grep -c '\[TB\] CASE .* PASS' "$TMP/fuzz.log" | grep -qx 5 \
  || { tail -30 "$TMP/fuzz.log"; fail "fuzz 循环没有恰好 5 条 CASE PASS"; }
echo "   ✓ 5/5 组通过（153,600/153,600 字节）"

{
  echo "# 验证设施自测证据"
  echo
  echo "- 时间：$(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "- 自测模块：\`tools/selftest/fake_engine.v\`（假模块，不实现卷积）"
  echo "- 自测 golden：\`golden/_selftest_zero\`（全 0 → expected_c8 全 0）"
  echo
  echo "## 断言结果"
  echo
  echo "| 用例 | 期望 | 实际 | 结论 |"
  echo "|---|---|---|---|"
  echo "| fake 写全 0 | 退出 0 / 30720/30720 / PASS | 相符 | ✓ |"
  echo "| fake 第 12345 字节写 1 | 退出 1 / 30719/30720 / 打印 byte=12345 | 相符 | ✓ |"
  echo "| fuzz 循环 5 组 | 退出 0 / 153600/153600 / cases=5/5 | 相符 | ✓ |"
  echo
  echo "## 失败用例的定位输出（节选）"
  echo
  echo '```'
  grep -E '\[BYTE-CHECK\]|\[TB-SUMMARY\]' "$TMP/bad1.log" | tail -5
  echo '```'
} > "$EVID"

echo
echo "✅ 自测全部通过"
echo "证据：$EVID"
exit 0
