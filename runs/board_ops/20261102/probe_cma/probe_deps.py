#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_deps.py —— 决定性 A/B：prologue/epilogue 的 4× 是不是"缺 q8/c8deq/bpack"造成的。

## 假设（H2，由 H1 的证伪逼出来）

冻结点 `pynq_frozen/` **只有 7 个 .py**，缺 `q8.py/q8.c`、`c8deq.py/c8deq.c`、
`bpack.py/bpack.c` —— 这三个模块**专门**用来把"往非缓存 DMA 缓冲里写/读"从
numpy 的逐字节散访问换成一趟 burst。`bin_engine` 对它们全部**静默回落**：
    `_q8()`          → prologue 的量化+C8 转置（`_pf["q8"]`）
    `_deq_kernel()`  → epilogue 的 C8→f32 反量化
所以：**代码字节没变，但依赖缺了 ⇒ prologue/epilogue 数量级上升，算法桶 1.00×。**

## 实验（同进程、同 arena、同输入、同开关；只有 sys.path 不同）

    arm A  only frozen            ×2   ← 今天所有 13.4 s 的跑法
    arm B  frozen + /tmp/probe_cma/deps ×2   ← deps = 从 ps/pynq/ **只读**拷出的 6 个文件

判据（三条全过才算 H2 成立）：
    ① arm B 的 prologue 明显回落（目标量级 ~1.6 s）
    ② arm B 的 epilogue 明显回落（目标量级 ~0.26 s）
    ③ **两臂 sha1 三件套都 = V.FROZEN[256]**（⇒ 内核是逐字节等价的，只是快）

纪律：不改 `ps/pynq/` 一个字节（只 `shutil.copy2` 读出）；.so 在 `/tmp/probe_cma/deps/` 里编。
"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import sys
import time

FROZEN = "/home/xilinx/demoir/ps/pynq_frozen"
LIVE = "/home/xilinx/demoir/ps/pynq"
DEPS = "/tmp/probe_cma/deps"
OUTDIR = "/tmp/probe_cma"

sys.path.insert(0, FROZEN)
os.environ.setdefault("BIN_PROFILE", "1")

import numpy as np                             # noqa: E402

DEPSRC = ("q8.py", "q8.c", "c8deq.py", "c8deq.c", "bpack.py", "bpack.c")
R = {"stamp": time.strftime("%Y-%m-%d %H:%M:%S")}


def say(s=""):
    print(s, flush=True)


def hdr(s):
    say("")
    say("=" * 78)
    say("  " + s)
    say("=" * 78)


def md5_of(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


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
    ap.add_argument("--json", default=os.path.join(OUTDIR, "probe_deps.json"))
    a = ap.parse_args()

    # ------------------------------------------------ 0 依赖清单（这是核心证据）
    hdr("0 冻结点 vs ps/pynq：三个 C 内核模块在不在")
    inv = {}
    for d in (FROZEN, LIVE):
        ent = {}
        for f in DEPSRC:
            p = os.path.join(d, f)
            ent[f] = md5_of(p) if os.path.exists(p) else None
        inv[d] = ent
        say("  %-40s %s" % (d, json.dumps(
            {k: (v[:8] if v else "缺失") for k, v in ent.items()}, ensure_ascii=False)))
    R["inventory"] = inv
    R["frozen_ls"] = sorted(os.listdir(FROZEN))
    R["live_has"] = sorted(f for f in os.listdir(LIVE)
                           if f.split(".")[0] in ("q8", "c8deq", "bpack"))
    say("  冻结点全部文件：%s" % R["frozen_ls"])
    say("  ps/pynq 里相关文件：%s" % R["live_has"])

    os.makedirs(DEPS, exist_ok=True)
    copied = {}
    for f in DEPSRC:
        s = os.path.join(LIVE, f)
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(DEPS, f))
            copied[f] = md5_of(os.path.join(DEPS, f))
    R["copied"] = copied
    say("  已只读拷出 %d 个文件到 %s（ps/pynq 未改动）" % (len(copied), DEPS))

    import board_v39_verify as V
    import bin_engine as BE
    import bin_net as BN

    def kern_state(tag):
        st = {}
        try:
            st["_q8"] = "OK" if BE._q8() is not None else "None(回落 numpy)"
        except Exception as ex:                # noqa: BLE001
            st["_q8"] = "EXC:%r" % (ex,)
        try:
            st["_deq_kernel"] = "OK" if BE._deq_kernel() is not None else "None(回落 numpy)"
        except Exception as ex:                # noqa: BLE001
            st["_deq_kernel"] = "EXC:%r" % (ex,)
        try:
            st["_bpack"] = "OK" if BE._bpack() is not None else "None(回落 numpy)"
        except Exception as ex:                # noqa: BLE001
            st["_bpack"] = "EXC:%r" % (ex,)
        say("  [%s] C 内核状态：%s" % (tag, json.dumps(st, ensure_ascii=False)))
        return st

    R["kern_frozen_only"] = kern_state("only frozen")
    try:
        import q8 as _q8m
        R["q8_where"] = getattr(_q8m, "__file__", "?")
    except Exception:                          # noqa: BLE001
        R["q8_where"] = "ImportError（冻结点路径下根本没有 q8.py）"
    say("  import q8 → %s" % R["q8_where"])

    # ------------------------------------------------ 1 load + arena
    hdr("1 load（本 session 唯一一次）+ arena 24 槽")
    say("  CmaFree 开工前 = %s MB" % cma_mb())
    ov = V.step_load(a.bit, download=bool(a.download))
    V.step_addr(ov)
    ident = V.step_identity(ov, a.fclk)
    R["identity"] = ident
    if not ident.get("ok"):
        say("  ✗✗ 复位指纹不对 ⇒ 停手")
        return 3
    arena, need = V.make_arena(256, a.slots, "deps_arena_256")
    say("  arena %.2f MB @%#x   CmaFree = %s MB"
        % (need / 1e6, int(arena.buf.physical_address), cma_mb()))

    # 非缓存缓冲的原始代价（1 MB fill：arena vs heap）
    hb = {"size_mb": 1}
    dst_a = np.frombuffer(arena.buf, np.uint8)[:1 << 20]
    dst_h = np.empty(1 << 20, np.uint8)

    def fill_bench(dst, n=5):
        ts = []
        for _ in range(n):
            t0 = time.perf_counter()
            dst[:] = 7
            ts.append(time.perf_counter() - t0)
        return round(min(ts) * 1e3, 3)
    hb["arena_uncached_fill_ms"] = fill_bench(dst_a)
    hb["heap_cached_fill_ms"] = fill_bench(dst_h)
    hb["ratio"] = round(hb["arena_uncached_fill_ms"] / hb["heap_cached_fill_ms"], 2)
    R["fill_bench_1MB"] = hb
    say("  1 MB 标量填充：arena(非缓存) %.3f ms  vs  heap(缓存) %.3f ms  ⇒ **%.1f×**"
        % (hb["arena_uncached_fill_ms"], hb["heap_cached_fill_ms"], hb["ratio"]))
    del dst_a, dst_h
    gc.collect()

    # ------------------------------------------------ 2 整网
    def my_run(tag):
        env = V.cfg(a.slots)
        os.environ["BV_FREE"] = "1"
        arena.reset()
        eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=V.PACK, verbose=False)
        eng.load_weights()
        try:
            eng._prof = None
        except Exception:                      # noqa: BLE001
            pass
        net = BN.BinNet(V.PACK, engine=eng, H=256, W=256, verbose=False)
        rng = np.random.default_rng(1)
        x0 = rng.random((3, 256, 256), dtype=np.float32)
        xs = sha1_of(x0)
        t0 = time.time()
        outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
        dur = time.time() - t0
        sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
        ok = (V.FROZEN[256] == (sh["out1"], sh["out2"], sh["out3"]))
        prof = st.get("prof") or {}
        pf = dict(getattr(eng, "_prof", None) or {})
        row = dict(tag=tag, env=env, total_s=st.get("total"), wall_s=round(dur, 4),
                   pl_s=st.get("pl"), pack_pl=st.get("pack_pl"), ps_s=st.get("ps"),
                   prologue=st.get("ps_prologue"), epilogue=st.get("ps_epilogue"),
                   nonconv=st.get("ps_nonconv"), overlap=st.get("overlap"),
                   wait=st.get("wait"),
                   prof={k: round(float(v), 4) for k, v in prof.items()},
                   e1e2_ms={k: round(float(v) * 1e3, 1) for k, v in pf.items()},
                   lazy=st.get("lazy"), acq=st.get("acq"),
                   x0_sha1=xs, sha1=sh, sha_ok=bool(ok), cmafree_mb=cma_mb())
        say("  ── %-18s ──" % tag)
        say("     总 %.3f s  PL %.3f | PS %.3f = pro %.3f + epi %.3f + 非卷积 %.3f"
            " | ovl %.3f wait %.3f  CmaFree %.2f"
            % (row["total_s"] or 0, row["pl_s"] or 0, row["ps_s"] or 0,
               row["prologue"] or 0, row["epilogue"] or 0, row["nonconv"] or 0,
               row["overlap"] or 0, row["wait"] or 0, row["cmafree_mb"]))
        say("     E1E2 亚项(ms)：%s" % json.dumps(row["e1e2_ms"], ensure_ascii=False))
        say("     分量：%s" % json.dumps(row["prof"], ensure_ascii=False))
        say("     out1=%s %s" % (sh["out1"][:12], "OK 3/3" if ok else "**✗ ≠冻结基线**"))
        del net, eng, outs
        gc.collect()
        return row

    hdr("2 arm A：只有冻结点在路径上（= 今天 13.4 s 的跑法）×%d" % a.n)
    rows_a = [my_run("A%d_frozenonly" % (i + 1)) for i in range(a.n)]

    hdr("3 arm B：把 deps/ 插到 sys.path 最前 + 清掉内核缓存 ×%d" % a.n)
    sys.path.insert(0, DEPS)
    BE._Q8 = None
    BE._c8deq = None
    BE._BPACK_STATE["tried"] = False
    BE._BPACK_STATE["mod"] = None
    R["kern_with_deps"] = kern_state("frozen+deps")
    try:
        import q8 as _q8m2
        R["q8_where2"] = getattr(_q8m2, "__file__", "?")
        say("  import q8 → %s" % R["q8_where2"])
        R["q8_selftest"] = bool(_q8m2.selftest()) if hasattr(_q8m2, "selftest") else None
        say("  q8.selftest() = %s" % R["q8_selftest"])
    except Exception as ex:                    # noqa: BLE001
        R["q8_where2"] = "EXC:%r" % (ex,)
    try:
        import c8deq as _c8m
        R["c8deq_where"] = getattr(_c8m, "__file__", "?")
        if hasattr(_c8m, "selftest"):
            R["c8deq_selftest"] = bool(_c8m.selftest())
            say("  c8deq.selftest() = %s" % R["c8deq_selftest"])
    except Exception as ex:                    # noqa: BLE001
        R["c8deq_where"] = "EXC:%r" % (ex,)
    rows_b = [my_run("B%d_withdeps" % (i + 1)) for i in range(a.n)]

    hdr("4 判据")
    R["runs"] = dict(A=rows_a, B=rows_b)
    pa = min(float(r["prologue"]) for r in rows_a)
    pb = min(float(r["prologue"]) for r in rows_b)
    ea = min(float(r["epilogue"]) for r in rows_a)
    eb = min(float(r["epilogue"]) for r in rows_b)
    ta = min(float(r["total_s"]) for r in rows_a)
    tb = min(float(r["total_s"]) for r in rows_b)
    sha_ok = all(r["sha_ok"] for r in rows_a + rows_b)
    R["verdict"] = dict(
        prologue_a=round(pa, 4), prologue_b=round(pb, 4), prologue_ratio=round(pa / pb, 3),
        epilogue_a=round(ea, 4), epilogue_b=round(eb, 4), epilogue_ratio=round(ea / eb, 3),
        total_a=round(ta, 4), total_b=round(tb, 4),
        all_sha_ok=bool(sha_ok),
        prologue_collapsed=bool(pa / pb >= 1.5),
        epilogue_collapsed=bool(ea / eb >= 1.5))
    say("  prologue : A %.3f → B %.3f  = **%.2f×**" % (pa, pb, pa / pb))
    say("  epilogue : A %.3f → B %.3f  = **%.2f×**" % (ea, eb, ea / eb))
    say("  total    : A %.3f → B %.3f  （差 %.3f s）" % (ta, tb, ta - tb))
    say("  两臂 sha1 三件套全中 = %s" % sha_ok)
    say("  ⇒ H2（缺 q8/c8deq 的静默回落）%s"
        % ("**成立**" if (pa / pb >= 1.5 and ea / eb >= 1.5 and sha_ok) else "不成立/未定"))

    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False)
    say("  → %s" % a.json)
    del arena
    gc.collect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
