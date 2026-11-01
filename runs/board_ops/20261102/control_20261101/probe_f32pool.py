#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_f32pool.py —— 定向实验：`BIN_F32POOL` 能否解释 prologue/epilogue 的 4× 差。

## 动机（只讲证据）

同一块板、同一次开机、同一位流（`602725b8`）、同一频率读回（150）、同一驱动
（HEAD `board_v39_verify.run_net`），把代码换成**2026-11-01 跑点那两份**后：

| 桶 | 2026-11-01 报的 | 今天实测 | 比 |
|---|---|---|---|
| `interp`（numpy 臂） | 1.916–1.957 | **1.932** | **1.00×（一模一样）** |
| `convfp` | 1.18–1.26 | **1.25** | 1.00× |
| `cat` | 0.13–0.16 | **0.15** | 1.00× |
| `prologue` | 1.57–1.62 | **5.90–6.01** | **≈3.7×** |
| `epilogue` | 0.25–0.26 | **1.18–1.19** | **≈4.6×** |
| PL 忙 | 2.26–3.05 | **3.09–4.05** | ≈1.3× |
| 总 | 8.06 | **13.62** | +5.5 s |

⇒ 与算法/内核相关的桶**逐项不变**（所以不是 CPU 变慢、也不是算法变了），
  但 prologue/epilogue **正好是 chip 侧 cache 维护与反量化涉及的阶段**。
  `bin_net.py` 自己的注释写着：`BIN_F32POOL` 默认关，池关时"每层整块 cache 维护量
  ×5.5，会把 epilogue 的收益吃回去" —— 4.6× 与 5.5× 同量级，且这个开关
  **从未进过任何开关表**（20261101 的 6 项、第一棒的 11 项都没有它）。

## 本实验（严格对照，同进程、同 arena、同输入）

    run 0：BIN_F32POOL 未设（= 今天的冻结点口径）  → 期望复现 ≈13.4 s
    run 1：BIN_F32POOL=1                            → 看 prologue/epilogue/总 是否掉到 ≈8.1 s

`bet`：`bin_engine.f32pool_plan()` 是纯函数，本探针直接打印它的 (nslots, slot_bytes)，
      以及 CmaFree（池是**独立** CMA 块，可能根本建不起来 ⇒ 会静默回落，必须记下来）。

用法（板端；复用已加载 overlay，不 download）：
    echo xilinx | sudo -S -p '' env XILINX_XRT=/usr \\
      /usr/local/share/pynq-venv/bin/python3 -u \\
      /home/xilinx/demoir/ps/probe_f32pool.py --no-download
"""
from __future__ import annotations

import argparse
import json
import os
import sys

FROZEN = "/home/xilinx/demoir/ps/pynq_frozen"
sys.path.insert(0, FROZEN)

import numpy as np                      # noqa: E402


def cmafree_mb():
    try:
        for ln in open("/proc/meminfo"):
            if ln.startswith("CmaFree"):
                return round(int(ln.split()[1]) / 1024.0, 2)
    except Exception:                   # noqa: BLE001
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v39.bit")
    ap.add_argument("--fclk", type=float, default=150.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--json", default="/home/xilinx/demoir/probe_f32pool.json")
    ap.add_argument("--no-download", action="store_true")
    a = ap.parse_args()

    import board_v39_verify as V
    import bin_engine as BE
    import bin_net as BN

    print("=" * 78)
    print("  定向实验：BIN_F32POOL 是否解释 prologue/epilogue 的 4× 差")
    print("=" * 78)
    print("  frozen dir = %s（HEAD b58964e）" % FROZEN)
    print("  CmaFree 开工前 = %s MB" % cmafree_mb())

    ov = V.step_load(a.bit, download=not a.no_download)
    V.step_addr(ov)
    ident = V.step_identity(ov, a.fclk)
    if not ident.get("ok"):
        return 3
    arena, need = V.make_arena(256, a.slots, "probe_pool_arena")
    print("  CmaFree 建 arena 后 = %s MB" % cmafree_mb())

    # f32 池计划（纯函数，不改行为）
    n0 = BN.BinNet(V.PACK, engine=None, H=256, W=256, verbose=False)
    g, _o, _t = n0.build_graph(np.zeros((3, 256, 256), np.float32))
    conv = [o for o in g.ops if o.kind == "conv"]
    del n0, g
    plan = BE.f32pool_plan(conv)
    print("  f32pool_plan(conv ops=%d) = (nslots=%s, slot_bytes=%s) ⇒ 需 %.2f MB"
          % (len(conv), plan[0], plan[1], plan[0] * plan[1] / 1e6))
    print("  CmaFree 余量 = %s MB" % cmafree_mb())

    rows = []
    for tag, pool in (("pool_off", ""), ("pool_on", "1")):
        if pool:
            os.environ["BIN_F32POOL"] = pool
        else:
            os.environ.pop("BIN_F32POOL", None)
        print("\n  >>> %s（BIN_F32POOL=%r） CmaFree=%s MB"
              % (tag, os.environ.get("BIN_F32POOL"), cmafree_mb()))
        row = V.run_net(ov, 256, arena, a.slots, tag=tag)
        row["pool_env"] = os.environ.get("BIN_F32POOL")
        row["cmafree_mb_after"] = cmafree_mb()
        row["f32pool_plan"] = list(plan)
        rows.append(row)
    os.environ.pop("BIN_F32POOL", None)

    out = dict(buckets=["total", "pl", "ps", "prologue", "epilogue", "nonconv",
                        "wait", "interp"], rows=rows)
    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print("\n  → %s" % a.json)
    for r in rows:
        print("  %-9s total=%.3f pro=%.3f epi=%.3f pl=%.3f wait=%.3f interp=%.3f"
              % (r["tag"], r["total_s"], r["prologue"], r["epilogue"],
                 r["pl_s"], r["wait"], (r.get("prof") or {}).get("interp", -1)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
