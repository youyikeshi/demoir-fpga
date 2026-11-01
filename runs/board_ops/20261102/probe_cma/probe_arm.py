#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_arm.py —— 单臂、单进程：按"依赖闭包子集"跑一次 256²，用于逐模块归因。

    --deps DIR[,DIR...]   插到 sys.path 最前的依赖目录（每个进程只放一个子集 ⇒ 干净）
    --bilinear 1|0        C 臂 / numpy 臂
    --n K                 跑 K 次

判据：每次 sha1 三件套必须 = V.FROZEN[256]（三臂数值必须逐位相同，只有速度能不同）。
"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import sys
import time

FROZEN = "/home/xilinx/demoir/ps/pynq_frozen"
sys.path.insert(0, FROZEN)
os.environ.setdefault("BIN_PROFILE", "1")

import numpy as np                             # noqa: E402


def say(s=""):
    print(s, flush=True)


def sha1_of(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def cma_mb():
    for ln in open("/proc/meminfo"):
        if ln.startswith("CmaFree"):
            return round(int(ln.split()[1]) / 1024.0, 3)
    return None


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--deps", default="")
    ap.add_argument("--bilinear", type=int, default=1)
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v39.bit")
    ap.add_argument("--fclk", type=float, default=150.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--label", default="arm")
    ap.add_argument("--json", default="/tmp/probe_cma/probe_arm.json")
    a = ap.parse_args()

    for d in [x for x in a.deps.split(",") if x.strip()]:
        sys.path.insert(0, d.strip())
    os.environ["BIN_BILINEAR_C"] = str(int(a.bilinear))

    import board_v39_verify as V
    import bin_engine as BE
    import bin_net as BN
    import bin_ref_numpy as RN

    R = dict(label=a.label, deps=a.deps, bilinear=int(a.bilinear),
             stamp=time.strftime("%Y-%m-%d %H:%M:%S"))
    say("  [%s] sys.path 前部 = %s" % (a.label, sys.path[:3]))
    R["kern"] = dict(q8=bool(BE._q8()), c8deq=bool(BE._deq_kernel()),
                     bpack=bool(BE._bpack()),
                     bilinear_c=bool(RN._bilinear_c_kernel() is not None))
    say("  [%s] 内核：q8=%s c8deq=%s bpack=%s bilinear_c=%s  CmaFree %s MB"
        % (a.label, R["kern"]["q8"], R["kern"]["c8deq"], R["kern"]["bpack"],
           R["kern"]["bilinear_c"], cma_mb()))

    ov = V.step_load(a.bit, download=bool(a.download))
    V.step_addr(ov)
    ident = V.step_identity(ov, a.fclk)
    if not ident.get("ok"):
        say("  ✗✗ 复位指纹不对")
        return 3
    arena, need = V.make_arena(256, a.slots, "arm_arena_256")
    say("  [%s] arena %.2f MB @%#x  CmaFree %s MB"
        % (a.label, need / 1e6, int(arena.buf.physical_address), cma_mb()))

    rows = []
    for i in range(a.n):
        V.cfg(a.slots)
        os.environ["BIN_BILINEAR_C"] = str(int(a.bilinear))
        os.environ["BV_FREE"] = "1"
        arena.reset()
        eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=V.PACK, verbose=False)
        eng.load_weights()
        net = BN.BinNet(V.PACK, engine=eng, H=256, W=256, verbose=False)
        x0 = np.random.default_rng(1).random((3, 256, 256), dtype=np.float32)
        t0 = time.time()
        outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
        dur = time.time() - t0
        sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
        ok = (V.FROZEN[256] == (sh["out1"], sh["out2"], sh["out3"]))
        prof = st.get("prof") or {}
        row = dict(tag="%s%d" % (a.label, i + 1), total_s=st.get("total"),
                   wall_s=round(dur, 4), pl_s=st.get("pl"), ps_s=st.get("ps"),
                   prologue=st.get("ps_prologue"), epilogue=st.get("ps_epilogue"),
                   nonconv=st.get("ps_nonconv"), overlap=st.get("overlap"),
                   wait=st.get("wait"),
                   prof={k: round(float(v), 4) for k, v in prof.items()},
                   sha_ok=bool(ok), sha1=sh, cmafree_mb=cma_mb())
        rows.append(row)
        say("  [%s] %-6s 总 %.3f  PL %.3f | pro %.3f + epi %.3f + 非卷积 %.3f | ovl %.3f wait %.3f"
            " | interp %.3f | %s"
            % (a.label, row["tag"], row["total_s"] or 0, row["pl_s"] or 0,
               row["prologue"] or 0, row["epilogue"] or 0, row["nonconv"] or 0,
               row["overlap"] or 0, row["wait"] or 0, row["prof"].get("interp", -1),
               "OK" if ok else "**sha1 ✗**"))
        del net, eng, outs
        gc.collect()
    R["rows"] = rows
    R["best"] = dict(total=round(min(float(r["total_s"]) for r in rows), 4),
                     prologue=round(min(float(r["prologue"]) for r in rows), 4),
                     epilogue=round(min(float(r["epilogue"]) for r in rows), 4),
                     wait=round(min(float(r["wait"]) for r in rows), 4))
    R["all_sha_ok"] = all(r["sha_ok"] for r in rows)
    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False)
    say("  [%s] best total %.3f  pro %.3f  epi %.3f ⇒ %s"
        % (a.label, R["best"]["total"], R["best"]["prologue"],
           R["best"]["epilogue"], a.json))
    del arena
    gc.collect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
