#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 probe_t120.py 的冻结/已验证骨架改造成 probe_t121.py（T-121，PF_EN=0 位流）。"""
import io
import sys

P = "/media/felix/Data/coding/demoir-fpga/runs/board_t121/probe_t121.py"
src = io.open(P, encoding="utf-8").read()


def rep(old, new, cnt=1):
    global src
    n = src.count(old)
    assert n == cnt, "count=%d (want %d) for %r" % (n, cnt, old[:70])
    src = src.replace(old, new)


# ---------- A. 文件头 docstring（第 3~19 行）----------
lines = src.split("\n")
assert lines[2].startswith('"""probe_t120.py'), lines[2]
assert lines[18].strip() == '"""', lines[18]
HEADER = '''"""probe_t121.py —— T-121：**PF_EN=0 位流（v118）** 的机制判决

判定的唯一问题：v53（PF_EN=1）在 128²/256² 上的错，是不是**预取 fill 路径**造成的？
    主判据：v118 在 128² 上两次 sha1 (a) 两次相同 且 (b) = v39 参照三 sha1
    旁证：256² 是否 = v39 冻结三 sha1；64² 是否仍 = 冻结三 sha1

用法（★ 本进程**只 load 一次**，三尺寸同一 session：64 → 128×2 → 256）：
    probe_t121.py --role v118 --bit /home/xilinx/demoir/design_1_150v118.bit \\
                  --runs-64 1 --runs-128 2 --runs-256 1 --freq 50 --slots 24

★ 顺序：64² 放最前（先在"已证明正确"的尺寸上确认新位流没弄坏正常路径），
        128² 主判据居中，256² 放最后（避免它污染后续块）。
★ 频率只许 50 MHz（该位流约束 10 ns / WNS=-0.103，跑 20 ns 有约 36% 余量）。
★ 纪律：不改任何 RTL/PS；运行点全走环境变量；ulimit -c 0 由外层脚本给；
        收尾只 sync 不 poweroff；看门狗只用 SIGALRM。
"""'''
lines[2:19] = HEADER.split("\n")
src = "\n".join(lines)

# ---------- B. 默认 role ----------
rep('    return "v53"', '    return "v118"')

# ---------- C. 参照三 sha1 + 输入 sha1 ----------
rep('''# 冻结 64² 三 sha1 + 输入 sha1（T-115/T-117 口径；128² 无冻结值，只做 A/B）
FROZEN64 = ("bfa246d34681e748311bbcd2f1b87317a56923fe",
            "226bd39e522aa373b8fc183acae5280931f8b63f",
            "762d84b9618eab8922edd6da4f07ba06de865628")
X0_REF64 = "bbae82e46e363a776d8fce8d9004e94502456bdc"''',
'''# ---- 参照三 sha1：全部来自 v39（已多次逐位复现）----
FROZEN64 = ("bfa246d34681e748311bbcd2f1b87317a56923fe",
            "226bd39e522aa373b8fc183acae5280931f8b63f",
            "762d84b9618eab8922edd6da4f07ba06de865628")
REF128_V39 = ("27e25cf67fdad22d95bfd488cc73ccc39cbe8134",
              "b82b83f3d7eb1e0dc92e0c22a3c9656a2b963b2e",
              "6db4e9bb19e0e744ef4f41a2c5a2eb6e003ad301")
REF256_V39 = ("9b1940c8955be53ccffd7905e333deffcea31f0a",
              "f86a7035621bed133374a1bef42a27db204e7019",
              "025757a83c3993c8f592e31219b6a97906020363")
REF = {64: FROZEN64, 128: REF128_V39, 256: REF256_V39}
X0_REF = {64: "bbae82e46e363a776d8fce8d9004e94502456bdc",
          128: "26c9796e18f0e6883680693b805ce8bb7ad51486",
          256: "cfc75bc3f15d306b07787325fdc9ae715cfadd0a"}''')

# ---------- D. role 判定：v53 → v118 ----------
rep('ROLE == "v53"', 'ROLE == "v118"', 5)

# ---------- E. run_net：参照 / 输入 sha1 ----------
rep('    frozen = FROZEN64 if hw == 64 else None',
    '    frozen = REF.get(hw)')
rep('''    if hw == 64:
        log("  输入 x0 sha1 = %s（冻结口径 %s）%s"
            % (xs, X0_REF64, "✓" if xs == X0_REF64 else "✗✗ 与冻结口径不同"))
    else:
        log("  输入 x0 sha1 = %s（无冻结口径；两版位流必须同值才可比）" % xs)''',
'''    xref = X0_REF.get(hw)
    if xref:
        log("  输入 x0 sha1 = %s（冻结口径 %s）%s"
            % (xs, xref, "✓" if xs == xref else "✗✗ 与冻结口径不同 ⇒ 不可比"))
    else:
        log("  输入 x0 sha1 = %s（无冻结口径）" % xs)''')
rep('x0_ok=(bool(xs == X0_REF64) if hw == 64 else None)',
    'x0_ok=(bool(xs == X0_REF.get(hw)) if X0_REF.get(hw) else None)')

# ---------- F. argparse ----------
rep('ap.add_argument("--role", choices=("v53", "v39"), required=True)',
    'ap.add_argument("--role", choices=("v118",), required=True)')
rep('''    ap.add_argument("--runs-64", type=int, default=0)
    ap.add_argument("--wd128", type=float, default=420.0)
    ap.add_argument("--wd64", type=float, default=300.0)
    ap.add_argument("--tag", default="t120")''',
'''    ap.add_argument("--runs-64", type=int, default=0)
    ap.add_argument("--runs-256", type=int, default=0)
    ap.add_argument("--wd128", type=float, default=600.0)
    ap.add_argument("--wd64", type=float, default=420.0)
    ap.add_argument("--wd256", type=float, default=1500.0)
    ap.add_argument("--tag", default="t121")''')

# ---------- G. main 的第 2/3 步 → 64 → 128 → 256 ----------
old_blocks_start = '    # ---------------- 第 2 步：128² ×N ----------------'
old_blocks_end = '    R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")'
i0 = src.index(old_blocks_start)
i1 = src.index(old_blocks_end)
NEW = '''    # ---------------- 第 2 步：64² ×N（先做：确认正常路径没被弄坏） ----------------
    row64 = None
    if a.runs_64 > 0:
        try:
            ar64 = make_arena(64, a.slots, "t121_arena64")
            row64 = run_net(ov, 64, ar64, f, "net64", a.runs_64, a.wd64)
            del ar64["arena"]
            gc.collect()
        except BaseException as ex:                               # noqa: BLE001
            log("✗✗ 64² 异常：%r" % (ex,))
            traceback.print_exc()
            row64 = dict(tag="net64", hw=64, error=repr(ex)[:500])
        R["net_64"] = row64
        dump()

    # ---------------- 第 3 步：128² ×N（★ 主判据） ----------------
    row128 = None
    try:
        ar128 = make_arena(128, a.slots, "t121_arena128")
        row128 = run_net(ov, 128, ar128, f, "net128", a.runs_128, a.wd128)
        del ar128["arena"]
        gc.collect()
    except BaseException as ex:                                   # noqa: BLE001
        log("✗✗ 128² 异常：%r" % (ex,))
        traceback.print_exc()
        row128 = dict(tag="net128", hw=128, error=repr(ex)[:500])
    R["net_128"] = row128
    dump()

    # ---------------- 第 4 步：256² ×N（旁证；放最后） ----------------
    row256 = None
    if a.runs_256 > 0:
        try:
            ar256 = make_arena(256, a.slots, "t121_arena256")
            row256 = run_net(ov, 256, ar256, f, "net256", a.runs_256, a.wd256)
            del ar256["arena"]
            gc.collect()
        except BaseException as ex:                               # noqa: BLE001
            log("✗✗ 256² 异常：%r" % (ex,))
            traceback.print_exc()
            row256 = dict(tag="net256", hw=256, error=repr(ex)[:500])
        R["net_256"] = row256
        dump()

    # ---------------- 小结 + 判据 ----------------
    def _blk(row, name):
        if not row or "runs" not in row or not row["runs"]:
            log("  %s：**没有有效 run**（%s）" % (name, (row or {}).get("error")))
            return None, None, None
        for r in row["runs"]:
            s = r.get("sha1") or {}
            log("  %s run%d sha1 = %s / %s / %s"
                % (name, r.get("run"), s.get("out1"), s.get("out2"), s.get("out3")))
        s = row["runs"][0].get("sha1") or {}
        return ((s.get("out1"), s.get("out2"), s.get("out3")),
                row.get("same_across_runs"), bool(row.get("sha_ok_all")))

    hdr("小结（role=%s，PF_EN=0）" % ROLE)
    t64, same64, ok64 = _blk(row64, "64²")
    t128, same128, ok128 = _blk(row128, "128²")
    t256, same256, ok256 = _blk(row256, "256²")
    exits = 0
    log("")
    log("  ---- 判据 ----")
    if t64 is not None:
        log("  ① 64² 仍 = 冻结三 sha1：%s（%s）"
            % ("**✓**" if ok64 else "**✗**",
               "正常路径没弄坏" if ok64 else "正常路径被弄坏 ✗✗"))
        if not ok64:
            exits = 8
    else:
        log("  ① 64² 无结果 ⇒ 正常路径未确认")
    if t128 is not None:
        log("  ② 128² 两次相同 = %s（共 %d 次）"
            % (same128, len(row128["runs"])))
        log("  ③ 128² = v39 参照三 sha1：%s" % ("**✓ 命中**" if ok128 else "✗ 不命中"))
        log("     【主判据】%s" % (
            "两次相同 ✓ 且 = v39 ✓ ⇒ **预取路径是真凶（定罪）**"
            if (same128 and ok128) else
            ("两次相同但 ≠ v39 ⇒ 预取去掉后变确定但仍错 ⇒ **另有确定性问题**"
             if same128 else
             ("两次仍不同 ⇒ **预取无罪**，转查别的"
              if len(row128["runs"]) >= 2 else "样本不足"))))
        if len(row128["runs"]) < 2:
            exits = max(exits, 8)
    else:
        exits = max(exits, 8)
    if t256 is not None:
        log("  ④ 256² = v39 冻结三 sha1：%s" % ("**✓**" if ok256 else "✗ 不中"))
    verdict = ("prefetch_guilty" if (t128 is not None and same128 and ok128)
               else ("deterministic_other_bug" if (t128 is not None and same128)
                     else ("prefetch_innocent" if t128 is not None else "no_data")))
    R["verdict_128"] = verdict
    R["criteria"] = dict(c64_frozen=(ok64 if t64 is not None else None),
                         c128_same_two=same128, c128_eq_v39=ok128,
                         c256_eq_v39=(ok256 if t256 is not None else None),
                         c64_triple=t64, c128_triple=t128, c256_triple=t256)
    log("  verdict_128 = **%s**（exits=%d）" % (verdict, exits))
'''
src = src[:i0] + NEW + src[i1:]

# ---------- H. 收尾 return ----------
rep('''    R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    dump()
    if not ok128:
        return EXIT_BAD
    if row64 is not None and not ok64:
        return EXIT_BAD
    return EXIT_OK''',
'''    R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    dump()
    return int(exits)''')

# ---------- I. 日志标签/默认路径 ----------
src = src.replace("[t120]", "[t121]")
src = src.replace('"/home/xilinx/demoir/t120_out"', '"/home/xilinx/demoir/t121_out"')
src = src.replace('"t120.json"', '"t121.json"')

io.open(P, "w", encoding="utf-8").write(src)
print("patched OK, lines =", src.count("\n") + 1)
