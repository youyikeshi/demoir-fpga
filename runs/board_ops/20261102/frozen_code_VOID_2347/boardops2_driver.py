#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批次 ② 板端 driver（**必须从冻结点跑**）：64²@150 双臂 + 256² seed=1/2 双臂 + full 自检。

冻结点纪律（2026-11-02）：
  · 本文件与 8 个代码文件一起冻结在 `ps/pynq_frozen/`，`sys.path[0]` 就是它自己所在目录
    ⇒ 保证 import 到的是**冻结点代码**，不是随时被改的 `ps/pynq/`。
  · 每条数字的 `row["env"]`、`row["arm_meta"]`、JSON 顶层的 `frozen_md5_before/after`
    一起回答"这一跑用的是哪份代码"。
  · 同进程内切臂（`V.set_arm()`）而不是开两个进程 ⇒ 避免"跨进程口径"污染；
    **一次 session 只 load 一次 overlay**。

组（每组都是逐次交替 numpy/C，同一轮同输入，便于算成对差）：
  ① net64_150     : 64²@150 MHz，numpy ≥5 + C ≥5（seed=1，sha1 三件套 = 冻结基线）
  ② net256_150_seed1 : 256²@150 MHz，numpy 3 + C 3（新跑点自洽基线，seed=1）
  ③ net256_150_seed2 : 256²@150 MHz，numpy 2 + C 2（seed=2：**不与冻结基线比**，
                        判据 = 两臂输出逐位相同）
  ④ selftest("full") 29 形状（C 臂）

用法（板端，走 tools/boardlock.sh）：
  echo xilinx | sudo -S -p '' env XILINX_XRT=/usr \
    /usr/local/share/pynq-venv/bin/python3 -u \
    /home/xilinx/demoir/ps/pynq_frozen/boardops2_driver.py --download
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                    # ★ 冻结点优先（本文件就在冻结点目录里）

import numpy as np                           # noqa: E402

import board_v39_verify as V                # noqa: E402
import bin_engine as BE                     # noqa: E402   (间接验证 import 链)
import bin_ref_numpy as RN                  # noqa: E402

LOGPATH = "/home/xilinx/demoir/v39_boardops2.log"
V.LOGPATH = LOGPATH

FROZEN_FILES = ("bin_net.py", "bin_engine.py", "bin_ref_numpy.py",
                "bilinear_c.py", "bilinear_c.c", "board_v39_verify.py",
                "board_safe.py", "board_repeat.py",
                os.path.basename(os.path.abspath(__file__)))


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def frozen_md5():
    out = {}
    for name in FROZEN_FILES:
        p = os.path.join(HERE, name)
        out[name] = md5_of(p) if os.path.exists(p) else "MISSING"
    return out


def stats(xs):
    s = sorted(float(x) for x in xs)
    n = len(s)
    if not n:
        return None
    med = s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])
    return dict(n=n, min=s[0], med=med, mean=sum(s) / n, max=s[-1])


def arm_stats(rows):
    tot = [r["total_s"] for r in rows if r.get("total_s")]
    it = [(r.get("prof") or {}).get("interp") for r in rows]
    it = [x for x in it if x is not None]
    per = [(r["sha1"]["out1"][:12], r["sha1"]["out2"][:12], r["sha1"]["out3"][:12],
            r.get("sha_ok")) for r in rows]
    return dict(n=len(tot), total=stats(tot), interp=stats(it),
                sha_per_run=per,
                sha_all_ok=(all(bool(r.get("sha_ok")) for r in rows)
                            if rows and rows[0].get("sha_ok") is not None else None),
                arm_effective=sorted({r.get("arm_effective") for r in rows}))


def run_group(ov, hw, slots, n, seed, label, out):
    hdr = "组 %s  hw=%d  seed=%d  numpy×%d + C×%d（逐次交替）" % (label, hw, seed, n, n)
    print("\n" + "=" * 78)
    print("  " + hdr)
    print("=" * 78)
    arena, need = V.make_arena(hw, slots, "b2_%s" % label)
    rows = {"numpy": [], "C": []}
    t_group = time.time()
    for i in range(n):
        for arm, use_c in (("numpy", False), ("C", True)):
            ok, why = V.set_arm(use_c)
            if use_c and not ok:
                print("  ✗✗ C 臂没装上（why=%s）⇒ 这一组不能当 C 臂" % why)
            bc = 1 if use_c else 0
            row = V.run_net(ov, hw, arena, slots,
                            tag="%s_%s_r%d" % (label, arm, i + 1), bc=bc, seed=seed)
            row["arm"] = arm
            row["arm_meta"] = dict(req_bc=bc, use_c=bool(use_c),
                                   kernel_loaded=bool(ok), why=why)
            row["arm_effective"] = ("C" if (use_c and ok) else
                                    ("numpy(fallback)" if use_c else "numpy"))
            row["interp_s"] = (row.get("prof") or {}).get("interp")
            rows[arm].append(row)
            print("    >> %-6s r%d  total=%.3f  interp=%.3f  sha1 3/3=%s  kernel=%s"
                  % (arm, i + 1, row["total_s"] or 0, row["interp_s"] or 0,
                     row.get("sha_ok"), ok))
            sys.stdout.flush()
    rec = dict(hw=hw, seed=seed, n_per_arm=n, arena_bytes=need,
               seconds=round(time.time() - t_group, 2), rows=rows,
               stats={k: arm_stats(v) for k, v in rows.items()})
    # 同轮成对差（同输入、交替 ⇒ 噪声最小）
    pair = []
    for i in range(min(len(rows["numpy"]), len(rows["C"]))):
        a, b = rows["numpy"][i], rows["C"][i]
        if a.get("total_s") and b.get("total_s"):
            pair.append(dict(round=i + 1, numpy=a["total_s"], C=b["total_s"],
                             d=b["total_s"] - a["total_s"],
                             rel=100.0 * (b["total_s"] - a["total_s"]) / a["total_s"],
                             d_interp=(b.get("interp_s") or 0) - (a.get("interp_s") or 0)))
    rec["pairwise"] = pair
    # 跨臂输出逐位一致（同输入 ⇒ 两臂 sha1 必须完全相同）
    same = []
    for i in range(min(len(rows["numpy"]), len(rows["C"]))):
        a, b = rows["numpy"][i]["sha1"], rows["C"][i]["sha1"]
        same.append(all(a[k] == b[k] for k in ("out1", "out2", "out3")))
    rec["cross_arm_bitwise_same"] = all(same) if same else None
    rec["cross_arm_x0_same"] = (rows["numpy"][0]["x0_sha1"] == rows["C"][0]["x0_sha1"]
                                if rows["numpy"] and rows["C"] else None)
    out["groups"][label] = rec
    print("  ── 组 %s 小结 ──" % label)
    for arm in ("numpy", "C"):
        s = rec["stats"][arm]
        if s and s["total"]:
            print("     %-5s total 最小 %.3f 中位 %.3f 均值 %.3f | interp 最小 %.3f 中位 %.3f"
                  " | sha1 %s | effective=%s"
                  % (arm, s["total"]["min"], s["total"]["med"], s["total"]["mean"],
                     (s["interp"] or {}).get("min", -1), (s["interp"] or {}).get("med", -1),
                     ("全 3/3" if s["sha_all_ok"] else
                      ("跳过(不适用)" if s["sha_all_ok"] is None else "**≠**")),
                     s["arm_effective"]))
    for p in pair:
        print("     第%d轮：numpy %.3f → C %.3f  Δ%+.3f s（%+.1f%%），interp Δ%+.3f s"
              % (p["round"], p["numpy"], p["C"], p["d"], p["rel"], p["d_interp"]))
    print("     跨臂输出逐位相同 = %s（x0 输入相同 = %s）"
          % (rec["cross_arm_bitwise_same"], rec["cross_arm_x0_same"]))
    del arena
    gc.collect()
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v39.bit")
    ap.add_argument("--fclk", type=float, default=150.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--n64", type=int, default=5, help="64² 每臂次数（≥5）")
    ap.add_argument("--n256s1", type=int, default=3, help="256² seed=1 每臂次数")
    ap.add_argument("--n256s2", type=int, default=2, help="256² seed=2 每臂次数")
    ap.add_argument("--json", default="/home/xilinx/demoir/boardops2_frozen.json")
    ap.add_argument("--download", action="store_true",
                    help="本 session 唯一一次真正的 overlay 下载")
    a = ap.parse_args()

    out = dict(driver=os.path.abspath(__file__), started=time.strftime("%Y-%m-%d %H:%M:%S"),
               args=vars(a), groups={}, frozen_dir=HERE,
               frozen_md5_before=frozen_md5())
    print("=" * 78)
    print("  批次② 冻结点实测  hw=64/256 槽=%d fclk=%g bit=%s"
          % (a.slots, a.fclk, os.path.basename(a.bit)))
    print("  冻结点目录：%s" % HERE)
    for k, v in out["frozen_md5_before"].items():
        print("    %-22s %s" % (k, v))
    print("=" * 78)

    ov = V.step_load(a.bit, download=a.download)
    V.step_addr(ov)
    ident = V.step_identity(ov, a.fclk)
    if not ident.get("ok"):
        print("!! 指纹不对 ⇒ 停手（不做任何测量）")
        out["fatal"] = "fingerprint"
        out["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        json.dump(out, open(a.json, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        return 3

    V.step_bilinear()                       # ④ 一次性成本（计时外，只付一次）
    rp = V.runpoint(a.bit, a.fclk, a.slots,
                    switches=V._switch_values(a.slots, 1))
    rp["frozen_md5"] = out["frozen_md5_before"]
    out["identity"] = V.R.get("identity")
    out["runpoint"] = rp
    json.dump(out, open(a.json, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    # ① 64²@150 双臂
    run_group(ov, 64, a.slots, a.n64, 1, "net64_150", out)
    json.dump(out, open(a.json, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    # ② 256²@150 seed=1 双臂（新跑点自洽基线）
    run_group(ov, 256, a.slots, a.n256s1, 1, "net256_150_seed1", out)
    json.dump(out, open(a.json, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    # ③ 256²@150 seed=2 双臂（不与冻结基线比）
    run_group(ov, 256, a.slots, a.n256s2, 2, "net256_150_seed2", out)
    json.dump(out, open(a.json, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    # ④ full 自检（29 形状），C 臂
    print("\n" + "=" * 78)
    print("  selftest(\"full\") 29 形状（C 臂）")
    print("=" * 78)
    ok, why = V.set_arm(True)
    import bilinear_c as BC
    t0 = time.time()
    full_ok = bool(BC.selftest("full", verbose=True)) if ok else False
    out["selftest_full"] = dict(kernel_loaded=bool(ok), why=why,
                                ok=full_ok, seconds=round(time.time() - t0, 3),
                                shapes=29)
    print("  → selftest full = %s（kernel=%s, %.3f s）" % (full_ok, ok, out["selftest_full"]["seconds"]))

    out["frozen_md5_after"] = frozen_md5()
    out["frozen_md5_same"] = bool(out["frozen_md5_after"] == out["frozen_md5_before"])
    out["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    json.dump(out, open(a.json, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    print("\n" + "=" * 78)
    print("  冻结点 md5 跑前/跑后：%s"
          % ("✓ 逐字节一致" if out["frozen_md5_same"] else "✗✗ 变了 ⇒ 立即停手"))
    print("  结果 → %s" % a.json)
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
