#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_np.py —— 闭合 8.096 s 那一档：**依赖闭包 + numpy 臂（BIN_BILINEAR_C=0）**

probe_deps 已证明 C 臂 6.497 s（历史 6.485）。本探针只补一个格子：
把 interp 也切回 numpy（BIN_BILINEAR_C=0），依赖闭包保持完整 ⇒ 应当落在 8.0–8.2 s，
即复现 2026-10-31 v39 冻结基线的 **8.096 s**。

判据：sha1 三件套 = V.FROZEN[256]（numpy interp 与 C interp 逐位等价 ⇒ 输出必须相同）。
用法：--no-download 复用已加载位流（同一 boot，不二次 load）。
"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import sys
import time

FROZEN = "/home/xilinx/demoir/ps/pynq_frozen"
DEPS = "/tmp/probe_cma/deps"
sys.path.insert(0, FROZEN)
sys.path.insert(0, DEPS)                    # ★ 依赖闭包优先（q8/c8deq/bpack）
os.environ["BIN_PROFILE"] = "1"
os.environ["BIN_BILINEAR_C"] = "0"          # ★ 这一格就是 numpy 臂

import numpy as np                          # noqa: E402


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
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v39.bit")
    ap.add_argument("--fclk", type=float, default=150.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--json", default="/tmp/probe_cma/probe_np.json")
    a = ap.parse_args()

    import board_v39_verify as V
    import bin_engine as BE
    import bin_net as BN
    import bin_ref_numpy as RN

    R = {"stamp": time.strftime("%Y-%m-%d %H:%M:%S"), "arm": "numpy (BIN_BILINEAR_C=0)"}
    say("  sys.path[0] = %s（依赖闭包）" % sys.path[0])
    say("  q8=%s c8deq=%s bpack=%s"
        % ("OK" if BE._q8() else "None", "OK" if BE._deq_kernel() else "None",
           "OK" if BE._bpack() else "None"))
    say("  bilinear C 内核 = %r（None ⇒ 真的是 numpy 臂）" % (RN._bilinear_c_kernel(),))
    R["kernel_loaded"] = bool(RN._bilinear_c_kernel() is not None)
    R["deps"] = dict(q8=bool(BE._q8()), c8deq=bool(BE._deq_kernel()),
                     bpack=bool(BE._bpack()))
    say("  CmaFree 开工前 = %s MB" % cma_mb())

    ov = V.step_load(a.bit, download=bool(a.download))
    V.step_addr(ov)
    ident = V.step_identity(ov, a.fclk)
    if not ident.get("ok"):
        say("  ✗✗ 复位指纹不对（位流没在板上？需要 --download）")
        return 3
    arena, need = V.make_arena(256, a.slots, "np_arena_256")
    say("  arena %.2f MB @%#x  CmaFree %s MB" % (need / 1e6, int(arena.buf.physical_address), cma_mb()))

    rows = []
    for i in range(a.n):
        env = V.cfg(a.slots)
        os.environ["BIN_BILINEAR_C"] = "0"          # cfg 不管这个键，但显式再写一次
        os.environ["BV_FREE"] = "1"
        arena.reset()
        eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=V.PACK, verbose=False)
        eng.load_weights()
        try:
            eng._prof = None
        except Exception:                        # noqa: BLE001
            pass
        net = BN.BinNet(V.PACK, engine=eng, H=256, W=256, verbose=False)
        rng = np.random.default_rng(1)
        x0 = rng.random((3, 256, 256), dtype=np.float32)
        t0 = time.time()
        outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
        dur = time.time() - t0
        sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
        ok = (V.FROZEN[256] == (sh["out1"], sh["out2"], sh["out3"]))
        prof = st.get("prof") or {}
        row = dict(tag="NP%d" % (i + 1), env=env, total_s=st.get("total"),
                   wall_s=round(dur, 4), pl_s=st.get("pl"), ps_s=st.get("ps"),
                   prologue=st.get("ps_prologue"), epilogue=st.get("ps_epilogue"),
                   nonconv=st.get("ps_nonconv"), overlap=st.get("overlap"),
                   wait=st.get("wait"),
                   prof={k: round(float(v), 4) for k, v in prof.items()},
                   sha1=sh, sha_ok=bool(ok), cmafree_mb=cma_mb(),
                   kernel_loaded=bool(RN._bilinear_c_kernel() is not None))
        rows.append(row)
        say("  ── %s ── 总 %.3f s（墙钟 %.3f） PL %.3f | PS %.3f = pro %.3f + epi %.3f + 非卷积 %.3f"
            " | ovl %.3f wait %.3f  interp %.3f  %s"
            % (row["tag"], row["total_s"] or 0, dur, row["pl_s"] or 0, row["ps_s"] or 0,
               row["prologue"] or 0, row["epilogue"] or 0, row["nonconv"] or 0,
               row["overlap"] or 0, row["wait"] or 0, row["prof"].get("interp", -1),
               "sha1 OK 3/3" if ok else "**sha1 ✗**"))
        del net, eng, outs
        gc.collect()
    R["rows"] = rows
    R["best_total"] = round(min(float(r["total_s"]) for r in rows), 4)
    R["all_sha_ok"] = all(r["sha_ok"] for r in rows)
    say("  最好 %.3f s；历史 8.096 s ⇒ 差 %+.2f%%"
        % (R["best_total"], 100.0 * (R["best_total"] / 8.096 - 1)))
    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False)
    say("  → %s" % a.json)
    del arena
    gc.collect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
