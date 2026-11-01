# -*- coding: utf-8 -*-
"""T-16 第一阶段：prologue 归因探针（离线，FakePL，hw=256，板端 C 臂同环境）。
只在 /tmp 里跑；被 import 的 bin_net 是 bin_net_instr.py（仅加计时行，逐行 diff 已存）。
"""
import os, sys, json, time, hashlib
import numpy as np

COPY = "/media/felix/Data/coding/demoir-fpga/worktrees/T-16-prologue/pynq_specialized_code"
P_PYNQ = COPY + "/ps/pynq"; TOOLS = COPY + "/ps/tools"; PACK = COPY + "/deploy/bin_weights_new"
HW = int(os.environ.get("PROBE_HW", "256"))
NSLOT = os.environ.get("PROBE_SLOTS", "24")

# ---- 板端 C 臂环境（t14_deps_board2.json runpoint/rows）----
os.environ.update(dict(
    BIN_PACK="e1e2", BIN_LAZY_DEQ="1", BIN_LAZY_SLOTS=NSLOT,
    BIN_CAT_FUSION="1", Q8CACHE="0", BIN_Q8_FUSED="1", BIN_BILINEAR_C="1",
    BIN_PROFILE="1", BIN_LAZY_DEBUG="1", BV_FREE="1",
))
os.environ.pop("CATPACK", None); os.environ.pop("BIN_CAT_STRICT", None)

sys.path.insert(0, P_PYNQ); sys.path.insert(0, TOOLS); sys.path.insert(0, "/tmp/probe_T16")
import bin_engine as BE
import bin_net_instr as BN
from bin_ref_numpy import Blob
from verify_e1e2_path import FakeE1E2Unit
from verify_e1e2_direct import SubAlloc

sys.path.insert(0, TOOLS)
from verify_lazy_net import FakePLUnit          # conv(run_unit=0) 用 numpy 参考

BASE = 0x3000_0000
PHASE = ["top"]

class Acc:
    def __init__(self):
        self.t = {}; self.n = {}; self.b = {}; self.w = {}; self.extra = {}
    def add(self, k, dt=0.0, n=0, b=0, w=0):
        if dt: self.t[k] = self.t.get(k, 0.0) + dt
        if n: self.n[k] = self.n.get(k, 0) + n
        if b: self.b[k] = self.b.get(k, 0) + b
        if w: self.w[k] = self.w.get(k, 0) + w

ACC = Acc()

class phase:
    def __init__(self, name): self.name = name
    def __enter__(self):
        self.old = PHASE[0]; PHASE[0] = self.name; self.t0 = time.perf_counter()
    def __exit__(self, *a):
        PHASE[0] = self.old
        ACC.add(self.name, dt=time.perf_counter() - self.t0)

def wrap(obj, name, label, getb=None, getn=None, getw=None, static=False):
    orig = getattr(obj, name)
    def f(*a, **kw):
        b0 = getb(*a, **kw) if getb else 0
        w0 = getw() if getw else 0
        with phase(label):
            r = orig(*a, **kw)
        w1 = getw() if getw else 0
        if getn: ACC.add(label, n=getn(r, *a, **kw))
        if getw: ACC.add(label, w=w1 - w0)
        return r
    if static:
        f = staticmethod(f)
    setattr(obj, name, f)
    return orig

# ---------------- 主流程 ----------------
def main():
    os.makedirs("/tmp/probe_T16/out", exist_ok=True)
    blob = Blob(PACK)
    flat = BE.MemBuf(512 << 20, BASE)
    unit = FakePLUnit(blob)
    unit.map_mem(BASE, flat)
    old_alloc = BE._pynq_allocate
    BE._pynq_allocate = SubAlloc(flat, BASE, start=0)
    eng = BE.BinConvEngine(unit, pack_dir=PACK, verbose=False)
    eng.load_weights()
    _orig_start = BE.BinConvEngine.start
    def start(self):
        unit.cur_layer = self.cfg
        return _orig_start(self)
    BE.BinConvEngine.start = start

    W = lambda: eng._writes
    # --- 叶子桶包装（顺序重要：先内层后外层）---
    wrap(BE.BinGuard, "fill_canary", "p_canary")
    wrap(BE.BinConvEngine, "push_input", "p_push",
         getn=lambda r, *a, **k: 1)
    wrap(BE.BinConvEngine, "arm", "p_arm", getw=W)
    wrap(BE.E1E2Engine, "prepare_segments", "p_job")
    wrap(BE.E1E2Engine, "run_prepared", "p_packjob", getw=W)
    wrap(BE.BinConvEngine, "pack_input", "p_packin", getw=W)
    wrap(BE.BinConvEngine, "kick", "p_kick", getw=W)
    wrap(BE.BinConvEngine, "reap", "p_reap")
    wrap(BE.BinConvEngine, "read_out", "p_readout")
    wrap(BE.C8Tensor, "materialize", "p_c8mat")
    wrap(BN.BinNet, "_q8_segments", "p_q8segs")
    wrap(BN.BinNet, "_materialize", "p_netmat")
    wrap(BN._PlBackend, "_segments", "p_segs", static=True)
    wrap(BN._PlBackend, "_acquire_slot", "p_acq")
    wrap(BN._PlBackend, "prepare", "p_prep_total", getw=W)
    wrap(BN.BinNet, "_exec_ps", "p_psop")

    rng = np.random.default_rng(1)
    x0 = rng.random((3, HW, HW), dtype=np.float32)
    t0 = time.perf_counter()
    net = BN.BinNet(PACK, engine=eng, H=HW, W=HW, verbose=True)
    st_all = net.run(x0, pipeline=True, nbuf=2)
    outs, st = st_all
    wall = time.perf_counter() - t0

    def sha16(a):
        return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()[:12]
    sha = tuple(sha16(outs[k]) for k in ("out1", "out2", "out3"))

    res = dict(hw=HW, slots=NSLOT, wall_s=wall,
               host_sha=sha,
               stats=dict(total=st["total"], pl=st["pl"], ps=st["ps"],
                          prologue=st["ps_prologue"], epilogue=st["ps_epilogue"],
                          nonconv=st["ps_nonconv"], wait=st["wait"],
                          overlap=st["overlap"], mmio=st["mmio_writes"],
                          mmio_skipped=st["mmio_skipped"],
                          cfg_hits=st.get("cfg_hits"), cfg_miss=st.get("cfg_miss"),
                          prof=st["prof"], lazy=st.get("lazy"),
                          cat={k: v for k, v in (st.get("cat") or {}).items()
                               if k in ("ops","fused","fallback","leaves","channels",
                                        "bytes","pack_s","cat_s","ops_by_class",
                                        "q8_hits","f32_leaf_bytes_mixed")},
                          e1e2_prof=st.get("e1e2_prof"), pack_pl=st.get("pack_pl"),
                          f32pool=st.get("f32pool")),
               acc=dict(t=ACC.t, n=ACC.n, b=ACC.b, w=ACC.w),
               prep_prof=net._prep_prof,
               probe=BN.PROBE_OUT.get("pr"),
               unit_writes=unit and getattr(eng, "_writes", None),
               unit_reads=getattr(eng, "_reads", None),
               f32pool_stats=st.get("f32pool"))
    with open("/tmp/probe_T16/out/attrib_host%d.json" % HW, "w") as f:
        json.dump(res, f, indent=1, ensure_ascii=False, default=str)
    print("WALL %.1f s  sha=%s" % (wall, sha), flush=True)
    print("stats:", json.dumps(res["stats"], ensure_ascii=False, default=str)[:1200], flush=True)
    print("ACC:", json.dumps(res["acc"], ensure_ascii=False, indent=1)[:4000], flush=True)
    print("PROBE:", json.dumps(res["probe"], ensure_ascii=False)[:1500], flush=True)

main()
