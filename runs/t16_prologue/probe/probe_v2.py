# -*- coding: utf-8 -*-
"""T-16 第二阶段探针 v2：细化 run() 外层 + 每层记录 + 字节计数。
同一被测量：bin_net_instr（只加计时行）。
"""
import os, sys, json, time, hashlib
import numpy as np
COPY = "/media/felix/Data/coding/demoir-fpga/worktrees/T-16-prologue/pynq_specialized_code"
P_PYNQ = COPY + "/ps/pynq"; TOOLS = COPY + "/ps/tools"; PACK = COPY + "/deploy/bin_weights_new"
HW = int(os.environ.get("PROBE_HW", "256")); NSLOT = os.environ.get("PROBE_SLOTS", "24")
os.environ.update(dict(BIN_PACK="e1e2", BIN_LAZY_DEQ="1", BIN_LAZY_SLOTS=NSLOT,
    BIN_CAT_FUSION="1", Q8CACHE="0", BIN_Q8_FUSED="1", BIN_BILINEAR_C="1",
    BIN_PROFILE="1", BIN_LAZY_DEBUG="1", BV_FREE="1"))
os.environ.pop("CATPACK", None); os.environ.pop("BIN_CAT_STRICT", None)
sys.path.insert(0, P_PYNQ); sys.path.insert(0, TOOLS); sys.path.insert(0, "/tmp/probe_T16")
import bin_engine as BE
import bin_net_instr as BN
from bin_ref_numpy import Blob
from verify_e1e2_path import FakeE1E2Unit
from verify_e1e2_direct import SubAlloc
from verify_lazy_net import FakePLUnit
BASE = 0x3000_0000
ACC = dict(t={}, n={}, b={}, w={})
def add(k, dt=0.0, n=0, b=0, w=0):
    if dt: ACC["t"][k] = ACC["t"].get(k, 0.0) + dt
    if n: ACC["n"][k] = ACC["n"].get(k, 0) + n
    if b: ACC["b"][k] = ACC["b"].get(k, 0) + b
    if w: ACC["w"][k] = ACC["w"].get(k, 0) + w
def wrap(obj, name, label, static=False, nfn=None, bfn=None, wfn=None):
    orig = getattr(obj, name)
    def f(*a, **kw):
        w0 = wfn() if wfn else 0
        t0 = time.perf_counter(); r = orig(*a, **kw); dt = time.perf_counter() - t0
        add(label, dt=dt, n=nfn(r, *a, **kw) if nfn else 0,
            b=bfn(r, *a, **kw) if bfn else 0, w=(wfn() - w0) if wfn else 0)
        return r
    setattr(obj, name, staticmethod(f) if static else f)

PER_LAYER = []
def main():
    os.makedirs("/tmp/probe_T16/out", exist_ok=True)
    blob = Blob(PACK)
    flat = BE.MemBuf(512 << 20, BASE)
    unit = FakePLUnit(blob); unit.map_mem(BASE, flat)
    BE._pynq_allocate = SubAlloc(flat, BASE, start=0)
    eng = BE.BinConvEngine(unit, pack_dir=PACK, verbose=False); eng.load_weights()
    _s = BE.BinConvEngine.start
    def start(self):
        unit.cur_layer = self.cfg; return _s(self)
    BE.BinConvEngine.start = start
    W = lambda: eng._writes
    # ---- 外层：图/装配 ----
    wrap(BN.BinNet, "build_graph", "o_build_graph")
    wrap(BN.BinNet, "_analyze_lazy", "o_analyze_lazy")
    wrap(BE.BinConvEngine, "enable_e1e2", "o_enable_e1e2")
    wrap(BE.BinArena, "__init__", "o_arena_alloc")
    wrap(BN._PlBackend, "__init__", "o_backend_ctor")
    wrap(BE, "f32pool_plan", "o_f32pool_plan", static=False)
    wrap(BE.BinConvEngine, "add_region", "o_add_region")
    wrap(BN._PlBackend, "poll", "o_poll", nfn=lambda r, *a, **k: 1)
    wrap(BN.BinNet, "_materialize", "o_netmat")
    # ---- prologue 内部 ----
    wrap(BE.BinGuard, "fill_canary", "p_canary")
    wrap(BE.BinConvEngine, "push_input", "p_push", nfn=lambda r, *a, **k: 1,
         bfn=lambda r, *a, **k: int(len(np.frombuffer(a[1], np.uint8))))
    wrap(BE.BinConvEngine, "arm", "p_arm", wfn=W)
    wrap(BE.E1E2Engine, "_pack_views", "p_views")
    wrap(BE.E1E2Engine, "prepare_segments", "p_job", wfn=W)
    wrap(BE.E1E2Engine, "run_prepared", "p_packjob", wfn=W)
    wrap(BE.BinConvEngine, "pack_input", "p_packin", wfn=W,
         bfn=lambda r, *a, **k: int(len(np.frombuffer(r, np.uint8))))
    wrap(BE.BinConvEngine, "kick", "p_kick", wfn=W)
    wrap(BE.BinConvEngine, "reap", "p_reap")
    wrap(BE.BinConvEngine, "read_out", "p_readout")
    wrap(BE.C8Tensor, "materialize", "p_c8mat")
    wrap(BN.BinNet, "_q8_segments", "p_q8segs")
    wrap(BN._PlBackend, "_segments", "p_segs", static=True)
    wrap(BN._PlBackend, "_acquire_slot", "p_acq")
    # 每层记录
    _prep = BN._PlBackend.prepare
    def prep(self, op):
        L = op.layer
        rec = dict(name=L.name, cin=int(L.cin), H=int(L.H), W=int(L.W))
        snap = {k: dict(v) for k, v in ACC.items()}
        w0 = eng._writes; t0 = time.perf_counter()
        r = _prep(self, op)
        rec["t_prep"] = time.perf_counter() - t0
        rec["w"] = eng._writes - w0
        for kk in ("p_job", "p_packin", "p_arm", "p_push", "p_canary", "p_segs",
                   "p_views", "p_acq", "p_packjob"):
            rec[kk] = ACC["t"].get(kk, 0.0) - snap["t"].get(kk, 0.0)
        rec["b_push"] = ACC["b"].get("p_push", 0) - snap["b"].get("p_push", 0)
        rec["b_packin"] = ACC["b"].get("p_packin", 0) - snap["b"].get("p_packin", 0)
        PER_LAYER.append(rec)
        return r
    BN._PlBackend.prepare = prep
    # _e1e2._prof 每次 prepare 后清空则看不到总量；保留
    rng = np.random.default_rng(1)
    x0 = rng.random((3, HW, HW), dtype=np.float32)
    t0 = time.perf_counter()
    net = BN.BinNet(PACK, engine=eng, H=HW, W=HW, verbose=False)
    outs, st = net.run(x0, pipeline=True, nbuf=2)
    wall = time.perf_counter() - t0
    sha = tuple(hashlib.sha1(np.ascontiguousarray(outs[k]).tobytes()).hexdigest()[:12]
                for k in ("out1", "out2", "out3"))
    res = dict(hw=HW, slots=NSLOT, wall=wall, sha=sha,
               stats={k: st.get(k) for k in ("total","pl","ps","ps_prologue","ps_epilogue",
                     "ps_nonconv","wait","overlap","mmio_writes","mmio_skipped",
                     "cfg_hits","cfg_miss","prof","lazy","pack_pl","e1e2_prof","f32pool")},
               cat={k: v for k, v in (st.get("cat") or {}).items()
                    if k in ("pack_s","cat_s","ops_by_class","q8_hits")},
               acc=ACC, per_layer=PER_LAYER, probe=BN.PROBE_OUT.get("pr"))
    json.dump(res, open("/tmp/probe_T16/out/attrib_v2_%d.json" % HW, "w"),
              indent=1, ensure_ascii=False, default=str)
    print("WALL %.1f sha=%s" % (wall, sha))
    print("marks", json.dumps(res["probe"]["mark"], indent=0))
    print("ACC.t", json.dumps({k: round(v, 4) for k, v in ACC["t"].items()}, indent=0))
    print("ACC.n", json.dumps(ACC["n"]), "ACC.b", json.dumps({k: int(v) for k, v in ACC["b"].items()}))
    print("ACC.w", json.dumps(ACC["w"]))
    print("e1e2_prof", json.dumps(st.get("e1e2_prof")))
    print("prep_prof", json.dumps({k: (round(v,4) if isinstance(v,float) else (len(v) if isinstance(v,list) else v)) for k,v in net._prep_prof.items()}))
main()
