#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe2011_control.py —— **对照实验**：用 2026-11-01 跑点的两份文件复跑 256²。

## 为什么需要这个对照

冻结跑点（git HEAD `b58964e`）实测 256²：
    numpy 13.44 s / C 11.66 s
而 2026-11-01 跑点（`runs/board_ops/20261101/`）报的是：
    numpy 8.06 s / C 6.49 s

两者差 **≈5.4 s**，且差**全部**落在 `prologue` 桶（5.78 s vs 1.57 s），`interp` 桶
的 C 收益（≈1.65 s）两边一模一样。⇒ 怀疑差异来自 `bin_engine.py` / `bin_net.py`。

## 控制变量（这就是"机制"而不是"猜想"）

| 变量 | 冻结点 | 本对照 |
|---|---|---|
| 板子 / 本次开机 / 位流 | 同 | **同（同一 boot，--no-download 复用已加载 overlay）** |
| fclk | 150 | 150 |
| 槽数 / 开关 | 24 / 同 | 24 / 同（跑 `V.cfg()`，同一个函数） |
| 驱动 | HEAD `board_v39_verify.run_net` | **同一个文件（HEAD e9877b79）** |
| `bin_engine.py` | `ddfe6b6f` | **`f848a29c`** |
| `bin_net.py` | `3f154b75` | **`cdf4b9a9`** |
| `bin_ref_numpy / bilinear_c / qcache` | HEAD | HEAD（完全相同） |

⇒ 只有 2 个文件不同。若本对照复现 ≈8.06 s，则"13.44 s"100% 归因于
  `bin_engine`/`bin_net` 的代码差异，与板子状态、位流、频率、开关、驱动**无关**。

**根因定位（静态）**：`ddfe6b6f → f848a29c` 共 380 行差异、`3f154b75 → cdf4b9a9`
共 96 行差异；prologue 是"PS 侧准备（q8 量化 + f32 打包 + 描述符）"桶。

用法（板端，走 tools/boardlock.sh；**复用已加载的 overlay，不再 download**）：
    echo xilinx | sudo -S -p '' env XILINX_XRT=/usr \\
      /usr/local/share/pynq-venv/bin/python3 -u \\
      /home/xilinx/demoir/ps/pynq_20261101/probe2011_control.py --no-download
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
sys.path.insert(0, HERE)

import numpy as np

FILES = ("bin_net.py", "bin_engine.py", "bin_ref_numpy.py",
         "bilinear_c.py", "bilinear_c.c", "qcache.py", "board_v39_verify.py")
# 2026-11-01 跑点在 `runs/board_ops/20261101/REPORT.md` 里写下的五个 md5
EXPECT_2021 = {"bin_net.py": "cdf4b9a9711ab4112439c43ba6ed1f12",
               "bin_engine.py": "f848a29c9f51d5580428684e232473bd"}
# 冻结点（本次基线）md5，用来证明"本目录确实不是冻结点"
FROZEN_HEAD = {"bin_net.py": "3f154b75f6526a096f78001aaa1e785b",
               "bin_engine.py": "ddfe6b6ffdfea33e76959748825d7af3"}


def md5_of(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def code_md5():
    return {f: md5_of(os.path.join(HERE, f)) for f in FILES}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v39.bit")
    ap.add_argument("--fclk", type=float, default=150.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--json", default="/home/xilinx/demoir/probe2011_control.json")
    ap.add_argument("--no-download", action="store_true")
    a = ap.parse_args()

    import board_v39_verify as V
    import bin_engine as BE
    import bin_net as BN
    import bin_ref_numpy as RN
    import bilinear_c as BC

    md5_before = code_md5()
    print("=" * 78)
    print("  对照实验：2026-11-01 跑点复跑（只换 bin_net/bin_engine）")
    print("=" * 78)
    for f, m in md5_before.items():
        want = EXPECT_2021.get(f)
        tag = ("  ← 2026-11-01 版本 %s" % ("✓" if m == want else "✗ 不符!")
               if want else "")
        print("    %-24s %s%s" % (f, m, tag))
    ok_engine = all(md5_before[f] == EXPECT_2021[f] for f in EXPECT_2021)
    ok_notfrozen = any(md5_before[f] != FROZEN_HEAD[f] for f in FROZEN_HEAD)
    print("  与 20261101 REPORT 声明的 md5 一致 = %s" % ("✓" if ok_engine else "✗"))
    print("  与冻结点 HEAD 不同 = %s" % ("✓" if ok_notfrozen else "✗"))

    ov = V.step_load(a.bit, download=not a.no_download)
    V.step_addr(ov)
    ident = V.step_identity(ov, a.fclk)
    if not ident.get("ok"):
        print("  ✗✗ 指纹不对 ⇒ 停手")
        return 3

    def set_arm(use_c):
        os.environ["BIN_BILINEAR_C"] = "1" if use_c else "0"
        RN._C_KERNEL = {"tried": False, "fn": None}
        BC._STATE = {"tried": False, "lib": None, "why": "reset", "checked": False}
        t0 = time.perf_counter()
        fn = RN._bilinear_c_kernel()
        return (fn is not None), str(BC.why()), round(time.perf_counter() - t0, 4)

    # 一次性成本（计时外）
    ok, why, dt = set_arm(True)
    print("  C 内核一次性成本 = %.3f s（kernel_loaded=%s why=%s）" % (dt, ok, why))
    if not ok:
        return 4

    arena, need = V.make_arena(256, a.slots, "probe2011_arena")
    rows = []
    for use_c in (False, True):
        ok, why, _ = set_arm(use_c)
        if use_c and not ok:
            return 5
        row = V.run_net(ov, 256, arena, a.slots,
                        tag="control_%s" % ("C" if use_c else "numpy"))
        row["kernel_loaded"] = bool(ok)
        row["kernel_why"] = why
        rows.append(row)
        print("  >>> %-5s total=%.3f pl=%.3f ps=%.3f pro=%.3f epi=%.3f nonconv=%.3f "
              "interp=%.3f sha_ok=%s"
              % (row["arm"] if "arm" in row else ("C" if use_c else "numpy"),
                 row["total_s"], row["pl_s"] or 0, row["ps_s"] or 0,
                 row["prologue"] or 0, row["epilogue"] or 0, row["nonconv"] or 0,
                 (row.get("prof") or {}).get("interp", -1), row["sha_ok"]))
    md5_after = code_md5()
    out = dict(md5_before=md5_before, md5_after=md5_after,
               md5_same=bool(md5_before == md5_after),
               matches_2021_report=bool(ok_engine),
               onetime_s=dt,
               identity={k: ident[k] for k in ("fclk_readback", "alpha_shift",
                                              "slope_shift", "ok")},
               rows=rows)
    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print("  → %s" % a.json)
    print("  md5 前后一致 = %s" % ("✓" if out["md5_same"] else "✗✗ 变了"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
