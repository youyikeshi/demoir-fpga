#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t103.py —— T-103：**v52 在 100 MHz 下结果是否正确**（"100 MHz 达标即止"的最终验收）

判据（父代理任务书，本脚本只做机械比较，不放宽任何容差）：

  第 2 步 逐层正确性（最敏感两层：cin112 k3 d2 / cin512 k1）
      H=W=32、输出后 64 KB 垫子全铺 0xA5、与 numpy golden 逐字节
      ⇒ **0 B 越界 + 0/65536 与 0/196608 字节不符**
  第 3 步 确定性（关键）
      同一层**连跑 2 次**，两次输出**逐字节互比**
      ⇒ **两次完全相同**（v51 在 100 MHz 两次差 3286 B）
  第 4 步 64² 整网（冻结运行点，无任何 BIN_V50_*）
      ⇒ 三个 sha1 = 冻结值，并且**连跑 2 次 sha1 相同**
  第 5 步 100 不达标 ⇒ 依次 95 → 90 MHz 重跑（第 4 步每档 1 次）

纪律：
  · 一个进程**只 download 一次**；ulimit -c 0 由外层脚本设；收尾只 sync 不 poweroff
  · 不改任何仓库代码：本文件只读调用 bin_engine / bin_net / bin_test_staged
  · 两层缓冲**每个 case 只分配一次**（跨频率/跨两次 run 物理地址恒定）⇒ 地址不是自变量
  · 每次 run 前输出区重铺 0x5A、尾部 64 KB 重铺 0xA5、flush；读前 invalidate
  · 金丝雀（64 KB 尾巴）判据 = 0 B 被写；任何报警/段错误/超时 ⇒ 停、报
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import signal
import sys
import time
import traceback

# ============================================================================
# ★ 0. 冻结运行点必须在 import bin_engine / bin_net 之前写进环境（T-54 §6）
# ============================================================================
RUNPOINT = [
    ("BIN_PACK", "e1e2"),
    ("BIN_LAZY_DEQ", "1"),
    ("BIN_LAZY_SLOTS", "24"),
    ("BIN_CAT_FUSION", "1"),
    ("BIN_BILINEAR_C", "1"),
    ("BIN_PRECOMP", "1"),
    ("Q8CACHE", "1"),
    ("BIN_F32DIRECT", "1"),
    ("BIN_CAT_ECA_FUSE", "1"),
    ("BIN_EPI_NOINV", "1"),
    ("BIN_PROFILE", "1"),
]
FORBID = ("BIN_V50_CHAIN_MAGIC", "BIN_SAM_TILE", "BIN_SAM_SLAB_KB",
          "BIN_DEQ_TILE", "BIN_MMIO_POLL", "BIN_MMIO_FAST", "BIN_MMIO_C",
          "BIN_CONVPF_C", "BIN_F32POOL", "BIN_CAT_STRICT", "BIN_VLANES")


def setup_env():
    for k in list(os.environ):
        if k in FORBID or k.startswith("BIN_V50_"):
            os.environ.pop(k, None)
    for k, v in RUNPOINT:
        os.environ[k] = v
    keys = [x for x, _ in RUNPOINT] + ["BIN_V50_FUSE", "BIN_VLANES"]
    return {k: os.environ.get(k, "") for k in keys}


ENV_AT_START = setup_env()

import numpy as np                                                   # noqa: E402

PS_DIR = os.environ.get("T103_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# 64² 冻结 sha1（runs/board_t96 控制组同口径复现过）
FROZEN64 = ("bfa246d34681e748311bbcd2f1b87317a56923fe",
            "226bd39e522aa373b8fc183acae5280931f8b63f",
            "762d84b9618eab8922edd6da4f07ba06de865628")
MAGIC_WANT = 0x5030_0001
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
PAD = 64 * 1024
MARK = 0xA5
EXIT_OK, EXIT_FP, EXIT_LOWER, EXIT_BAD, EXIT_ERR, EXIT_HANG = 0, 2, 3, 4, 6, 5

R = {"env_at_start": ENV_AT_START, "steps": {}}
LOGPATH = None
_fh = None
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
OPREC = []


def log(s=""):
    s = str(s)
    print(s, flush=True)
    if _fh is not None:
        _fh.write(s + "\n")
        _fh.flush()
        try:
            os.fsync(_fh.fileno())
        except Exception:                                        # noqa: BLE001
            pass


def hdr(s):
    log("")
    log("=" * 78)
    log("  " + s)
    log("=" * 78)


def sha1_of(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def dump():
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t103.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(R, f, indent=1, ensure_ascii=False, default=str)
        log("[json] 已落盘 %s" % path)
    except Exception as ex:                                      # noqa: BLE001
        log("[json] 落盘失败：%r" % (ex,))


# ---- 看门狗：整网 run 没有内建 timeout，用 SIGALRM 兜住"挂死" -------------
def _on_alarm(_sig, _frm):
    raise TimeoutError("SIGALRM 看门狗到期（PL 未 done）")


signal.signal(signal.SIGALRM, _on_alarm)


def with_alarm(sec, fn, *a, **kw):
    signal.alarm(int(max(1, sec)))
    try:
        return fn(*a, **kw)
    finally:
        signal.alarm(0)


try:                                                   # PYNQ 3.1 不自动注册 Device
    import pynq.pl_server.embedded_device as _emb      # noqa: F401
except Exception:                                      # noqa: BLE001
    pass
from pynq import Overlay, Clocks                       # noqa: E402

import bin_engine as BE                                # noqa: E402
import bin_net as BN                                   # noqa: E402
import bin_test_staged as BS                           # noqa: E402

log("[t103] python=%s numpy=%s mach=%s" % (sys.version.split()[0], np.__version__,
                                           platform.machine()))
log("[t103] vlanes_now()=%d prm_layout=%s BIN_V50_* 残留=%s"
    % (BE.vlanes_now(), BE.prm_layout(),
       [k for k in os.environ if k.startswith("BIN_V50_")]))
log("[t103] cat_eca_fuse=%s epi_noinv=%s"
    % (BN._cat_eca_fuse(), BE._epi_noinv()))

# ---- per-op PL 周期采集（只读钩子；只用于"op 数/是否挂死"）----------------
_orig_reap = BE.BinConvEngine.reap


def _reap_hook(self, t0, timeout, out_buf=None, L=None, verify_canary=True,
               t_done=None):
    rs = _orig_reap(self, t0, timeout, out_buf=out_buf, L=L,
                    verify_canary=verify_canary, t_done=t_done)
    try:
        if L is not None:
            OPREC.append(dict(name=str(getattr(L, "name", "?")), cin=int(L.cin),
                              cout=int(L.cout), k=int(L.k), H=int(L.H), W=int(L.W),
                              Hout=int(L.Hout), Wout=int(L.Wout), cb=int(L.cb),
                              K=int(L.K), sec=float(rs.seconds)))
    except Exception:                                            # noqa: BLE001
        pass
    return rs


BE.BinConvEngine.reap = _reap_hook


def u8(b):
    return np.asarray(b).view(np.uint8).reshape(-1)


def deps_line():
    try:
        d = BE.kernel_deps()
        ok = bool(BE.deps_closure_ok(d))
    except Exception as ex:                                      # noqa: BLE001
        return "[DEPS] <读不到：%r>" % (ex,)
    order = ("q8", "c8deq", "bpack", "e1e2", "catpack", "safe")
    return ("[DEPS] " + " ".join("%s=%s" % (k, d[k]["state"]) for k in order
                                 if k in d) + "  ⇒ deps_closure_ok=%s" % ok)


# ============================================================================
# 第 1 步：load + 指纹（本进程唯一一次 download=True）
# ============================================================================
def step1_load(bit):
    hdr("第 1 步：load overlay + 指纹（本进程唯一一次 download=True）")
    t0 = time.time()
    ov = Overlay(bit, download=True)
    dt = time.time() - t0
    ips = sorted(ov.ip_dict.keys())
    log("  Overlay(%s) 用时 %.3f s" % (os.path.basename(bit), dt))
    log("  IP 清单：%s" % ips)
    fconv = [x for x in ips if "fconv" in x.lower()]
    got, ok_all = {}, True
    for name, exp in BASE_WANT.items():
        ba = int(getattr(ov, name).mmio.base_addr)
        ln = int(getattr(ov, name).mmio.length)
        ok = (ba == exp)
        ok_all &= ok
        got[name] = dict(base="%#010x" % ba, length=ln, ok=bool(ok))
        log("  %-11s base=%#010x length=%#x （期望 %#010x）%s"
            % (name, ba, ln, exp, "✓" if ok else "✗✗"))
    base = BASE_WANT["bin_unit_0"]
    magic = {}
    for tag, off in (("CFG+0x314", BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"]),
                     ("base+0x314", BE.V50_CHAIN_REG["CHAIN_ID"]),
                     ("0x310 CHAIN_STATUS", BE.V50_CHAIN_REG["CHAIN_STATUS"]),
                     ("0x308 BAND_ROWS", BE.V50_CHAIN_REG["BAND_ROWS"])):
        v = int(ov.bin_unit_0.mmio.read(int(off))) & 0xFFFFFFFF
        magic[tag] = dict(abs_addr="%#010x" % (base + int(off)), hex="%#010x" % v)
        log("  %#010x = %#010x   (%s)" % (base + int(off), v, tag))
    m = int(magic["CFG+0x314"]["hex"], 16)
    magic_ok = (m == MAGIC_WANT)
    log("  **magic(0x314) = %#010x 期望 %#010x ⇒ %s**"
        % (m, MAGIC_WANT, "v52 寄存器块在位 ✓" if magic_ok else "✗✗ ⇒ 停"))
    fclk0_now = float(Clocks.fclk0_mhz)
    log("  load 后 fclk0 读回 = %g MHz（下一步按 100 MHz 设并再读回）" % fclk0_now)
    R["steps"]["load"] = dict(bit=bit, bit_md5=None, seconds=round(dt, 4), ips=ips,
                              has_fconv=bool(fconv), addr=got, magic=magic,
                              magic_ok=bool(magic_ok), fclk0_at_load=fclk0_now,
                              downloads=1)
    ok = bool(ok_all and magic_ok and not fconv)
    R["steps"]["load"]["fp_ok"] = ok
    if not ok:
        if fconv:
            log("  ✗✗ overlay 里出现了 fconv_*：%s" % fconv)
        if not magic_ok:
            log("  ✗✗ magic 不对 ⇒ 按任务书**停，报父代理**")
    return ov, ok


def set_fclk(mhz):
    Clocks.fclk0_mhz = float(mhz)
    time.sleep(0.4)
    return float(Clocks.fclk0_mhz)


# ============================================================================
# 第 2--3 步：单层准备 + 每次 run
# ============================================================================
CASES = [
    ("cin112_k3_d2", lambda L: L["cin"] == 112 and L["cout"] == 64
     and L["k"] == 3 and L["d"] == 2),
    ("cin512_k1", lambda L: L["cin"] == 512 and L["cout"] == 192 and L["k"] == 1),
]


def find_layer(bl, pred):
    for L in bl.layers:
        try:
            if pred(L):
                return L
        except Exception:                                        # noqa: BLE001
            continue
    return None


def prepare_case(e, bl, pool, pred, H, W, seed, timeout):
    L = find_layer(bl, pred)
    if L is None:
        raise RuntimeError("权重包里找不到目标层")
    LL = BE.BinLayer(L, H, W)
    P = bl.params(L)
    rng = np.random.default_rng(seed)
    x = rng.integers(-128, 128, (LL.cin, H, W), dtype=np.int16).astype(np.int8)
    A = BS.binarize_pack(x, P["t_q"])
    ref, _ = BS.conv_rtl(LL.cin, LL.cout, LL.k, LL.s, LL.dil, LL.pad, LL.shift, A, P)
    blob, Ld = BS.synth_layer_blob("real:" + L["name"], L, P["words"],
                                   P["alpha_q"], P["bias_q"], P["zeta_q"],
                                   P["gamma_q"], P["slope_q"])
    Ld = dict(Ld, H=H, W=W, Hout=LL.Hout, Wout=LL.Wout)
    Lr = BE.BinLayer(Ld, H, W)
    act = BS.act_bytes(A)
    exp = np.frombuffer(BS.to_c8(ref), np.int8).copy()
    need = ((Lr.cout + 7) // 8) * Lr.Hout * Lr.Wout * 8
    ob, opa = pool.region(need + PAD, name="gout")
    gb, gpa = pool.region(len(blob) + 64, name="gblob")
    ab, apa = pool.region(len(act) + 64, name="gact")
    e.add_region(gb, "gblob")
    e.add_region(ab, "gact")
    e.add_region(ob, "gout")
    u8(gb)[:len(blob)] = np.frombuffer(blob, np.uint8)
    u8(ab)[:len(act)] = np.frombuffer(act, np.uint8)
    gb.flush()
    ab.flush()
    e.blob, e.blob_pa = gb, gpa
    C = dict(name=L["name"], cin=Lr.cin, cout=Lr.cout, k=Lr.k, K=Lr.K,
             Hout=Lr.Hout, Wout=Lr.Wout, need=need, exp=exp, Lr=Lr,
             ob=ob, ob_pa=int(opa), ab=ab, ab_pa=int(apa),
             gb=gb, gb_pa=int(gpa), act_n=len(act), blob_n=len(blob),
             timeout=timeout, act_sha1=sha1_of(act), exp_sha1=sha1_of(exp))
    log("  准备 %-14s cin=%d cout=%d k=%d %dx%d→%dx%d K=%d  OUT=%d B  act=%d B"
        "  ob=%#x  exp_sha1=%s"
        % (C["name"][:14], C["cin"], C["cout"], C["k"], H, W, C["Hout"], C["Wout"],
           C["K"], need, len(act), int(opa), C["exp_sha1"][:12]))
    return C


def run_once(e, C, fclk, label):
    """一次 PL 运行；返回原始输出字节（copy）+ 越界计数 + 周期 + 不符字节数。"""
    ob, need = C["ob"], C["need"]
    # ★ 多层共用一个 engine ⇒ 每次 run 前必须把 blob 指回本 case
    e.blob, e.blob_pa = C["gb"], C["gb_pa"]
    v = u8(ob)
    v[:] = 0x5A                        # 输出区先铺非 MARK 的底
    v[need:] = MARK                    # 尾部 64 KB 垫子
    ob.flush()
    t0 = time.time()
    rs = with_alarm(C["timeout"] + 30.0, e.run, C["ab"], ob, L=C["Lr"],
                    timeout=C["timeout"], verify_canary=False)
    wall = time.time() - t0
    try:
        st310 = int(e._rd(BE.CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
    except Exception:                                            # noqa: BLE001
        st310 = None
    ob.invalidate()
    gg = u8(ob)
    out = np.array(gg[:need], copy=True)
    tail = gg[need:]
    obad = np.nonzero(tail != MARK)[0]
    got = np.frombuffer(out.tobytes(), np.int8)
    mbad = np.nonzero(got != C["exp"])[0]
    d = dict(label=label, fclk=fclk, wall=round(wall, 5),
             cyc=int(rs.seconds * fclk * 1e6 + 0.5),
             oob_bytes=int(len(obad)), mismatch=int(len(mbad)), out_n=int(len(got)),
             st310=("%#010x" % st310) if st310 is not None else None,
             sha1=sha1_of(out),
             first_diffs=[dict(i=int(i), got=int(got[i]), want=int(C["exp"][i]))
                          for i in mbad[:8]])
    d["_out"] = out
    d["_got"] = got
    return d


# ============================================================================
# 第 4 步：64² 整网
# ============================================================================
def make_arena(hw, slots, name):
    n0 = BN.BinNet(PACK, engine=None, H=hw, W=hw, verbose=False)
    g, _o, _t = n0.build_graph(np.zeros((3, hw, hw), np.float32))
    conv = [o for o in g.ops if o.kind == "conv"]
    max_in = max(o.layer.act_bytes for o in conv)
    max_out = max(o.layer.out_bytes for o in conv)
    need = (max_in + max_out + 4096) * max(2, int(slots)) + (1 << 16)
    del n0, g
    gc.collect()
    arena = BE.BinArena(need, name=name)
    log("  arena %-10s %.2f MB（%d 槽 × (in %.2f + out %.2f MB)）@%#x"
        % (name, need / 1e6, max(2, int(slots)), max_in / 1e6, max_out / 1e6,
           int(arena.buf.physical_address)))
    return arena


def run_net(ov, hw, arena, slots, fclk, tag, nrun, watchdog):
    hdr("第 4 步：64² 整网（冻结运行点）fclk 读回 %g MHz，连跑 %d 次" % (fclk, nrun))
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    log("  输入 x0 sha1 = %s    %s" % (xs, deps_line()))
    runs = []
    for i in range(nrun):
        # ★ 每次 run 都用**全新的 engine/net**（与 T-96 同款口径：整网跑两次 =
        #   两个互不继承 PS 侧缓存的独立跑），arena 复用同一块物理内存
        arena.reset()
        eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
        eng.load_weights()
        net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
        OPREC.clear()
        t0 = time.time()
        outs, st = with_alarm(watchdog, net.run, x0, pipeline=True, nbuf=2,
                              arena=arena)
        dur = time.time() - t0
        sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
        trip = (sh["out1"], sh["out2"], sh["out3"])
        rec = dict(run=i + 1, wall_s=round(dur, 3), total_s=st.get("total"),
                   pl_s=st.get("pl"), ps_s=st.get("ps"),
                   prologue=st.get("ps_prologue"), epilogue=st.get("ps_epilogue"),
                   nonconv=st.get("ps_nonconv"),
                   deps_closure_ok=st.get("deps_closure_ok"),
                   kernel_deps={k: v.get("state") for k, v in
                                (st.get("kernel_deps") or {}).items()},
                   n_conv_ops=len(OPREC), sha1=dict(sh), sha_ok=bool(trip == FROZEN64),
                   conv_cyc=int(sum(r["sec"] for r in OPREC) * fclk * 1e6 + 0.5))
        runs.append(rec)
        log("  run%d: 总 %.3f s（墙钟 %.3f）| PL %.3f | PS %.3f | conv op %d 个"
            % (i + 1, rec["total_s"] or 0, dur, rec["pl_s"] or 0, rec["ps_s"] or 0,
               rec["n_conv_ops"]))
        log("        sha1 out1=%s out2=%s out3=%s  %s"
            % (sh["out1"][:16], sh["out2"][:16], sh["out3"][:16],
               "OK = 冻结值 ✓" if rec["sha_ok"] else "**≠ 冻结值 ✗✗**"))
        if not rec["sha_ok"]:
            for j, e in enumerate(FROZEN64):
                if trip[j] != e:
                    log("        冻结 out%d=%s" % (j + 1, e))
        del outs, net, eng
        gc.collect()
    same = all(r["sha1"] == runs[0]["sha1"] for r in runs)
    log("  %d 次 sha1 互比：%s" % (nrun, "**完全相同 ✓**" if same
                                 else "**不同 ✗✗（非确定性）**"))
    row = dict(tag=tag, hw=hw, slots=int(slots), fclk=fclk, x0_sha1=xs,
               nrun=nrun, runs=runs, frozen=list(FROZEN64),
               sha_ok_all=bool(all(r["sha_ok"] for r in runs)),
               same_across_runs=bool(same))
    return row


# ============================================================================
# 主流程
# ============================================================================
def main():
    global LOGPATH, _fh, PACK
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v52.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--hw", type=int, default=32)
    ap.add_argument("--timeout", type=float, default=4.0)
    ap.add_argument("--seed", type=int, default=20261031)
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t103_out")
    ap.add_argument("--net-watchdog", type=float, default=180.0)
    ap.add_argument("--layer-freqs", default="100,95,90,75",
                    help="逐层要测的 fclk 目标（MHz，逗号分隔；读回按 PYNQ 就近值）")
    ap.add_argument("--net-freqs", default="100,95,90,75",
                    help="整网要测的 fclk 目标（MHz，逗号分隔）")
    ap.add_argument("--net-runs-first", type=int, default=3,
                    help="第一个整网频率连跑几次（100 MHz 判据要 >=2）")
    ap.add_argument("--net-runs-other", type=int, default=1)
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "t103_%s.log" % time.strftime("%H%M%S"))
    _fh = open(LOGPATH, "a", buffering=1, encoding="utf-8")
    PACK = a.pack
    R["args"] = vars(a)
    R["runpoint"] = ENV_AT_START
    log("[t103] 日志 = %s" % LOGPATH)
    log("[t103] bit = %s" % a.bit)
    log("[t103] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))

    # ---------- 第 1 步 ----------
    ov, fp_ok = step1_load(a.bit)
    if not fp_ok:
        log("  ⇒ **停**：指纹/magic 不合格（按任务书报父代理，不继续）")
        dump()
        return EXIT_FP

    try:
        kl = BE.load_kernels()
        log("  内核装载：%s" % {k: (round(float(v.get("seconds") or 0), 3),
                                  v.get("loaded")) for k, v in kl.items()})
    except Exception as ex:                                      # noqa: BLE001
        log("  内核装载失败（逐层路径不需要，不致命）：%r" % (ex,))
    log("  %s" % deps_line())
    R["steps"]["deps"] = dict(line=deps_line(),
                              closure_ok=bool(BE.deps_closure_ok()))

    # ---------- 两层缓冲一次性分配 ----------
    hdr("第 2 步：逐层缓冲准备（每个 case 只分配一次 ⇒ 跨频率/跨 run 地址恒定）")
    bl = BS.Blob(PACK)
    pool = BE.BinArena(32 << 20, name="t103_layer_pool")
    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    e.add_region(pool.buf, "pool")
    cases = []
    try:
        for key, pred in CASES:
            C = prepare_case(e, bl, pool, pred, a.hw, a.hw, a.seed, a.timeout)
            C["key"] = key
            cases.append(C)
    except Exception as ex:                                      # noqa: BLE001
        traceback.print_exc()
        log("  ✗ 准备失败：%r" % (ex,))
        R["steps"]["prepare"] = dict(error=repr(ex)[:500])
        dump()
        return EXIT_ERR

    # ---------- 64² 整网 arena（24 槽，冻结运行点）----------
    hdr("第 4 步：64² 整网 arena（24 槽）")
    try:
        arena64 = make_arena(64, 24, "t103_arena64")
    except Exception as ex:                                      # noqa: BLE001
        traceback.print_exc()
        log("  ✗ arena 分配失败：%r" % (ex,))
        dump()
        return EXIT_ERR

    # ---------- 阶段 A：逐层（★ 顺序纪律：先把**所有频率的逐层**跑完，再跑整网）
    #   为什么：整网跑完会把 PL 留在 busy（0x310 = 0x403）；紧接着改频去跑逐层，
    #   reap 会读到"早到的 done"（本代理第一次跑时实测 cyc=19491，而这层至少
    #   需要 ~166k 拍 ⇒ 该次逐层数据无效）。T-96/T-98 也是逐层在前、整网在后。
    blocks = []
    clean_at = None
    freq_layer = [float(x) for x in a.layer_freqs.split(",") if x.strip()]
    for f in freq_layer:
        btag = "%g" % f
        rb = set_fclk(f)
        log("")
        log("################ [%s MHz] 目标 %g MHz → fclk0 读回 **%g MHz**"
            % (btag, f, rb))
        R.setdefault("fclk", {})[btag] = dict(target=f, readback=rb)
        B = dict(block=btag, target=f, fclk=rb, layers=[], net=None)
        layer_ok = True
        for C in cases:
            try:
                r1 = run_once(e, C, rb, btag + "#1")
                r2 = run_once(e, C, rb, btag + "#2")
            except Exception as ex:                              # noqa: BLE001
                traceback.print_exc()
                log("  ✗✗ %s @ %g MHz 异常：%r" % (C["key"], rb, ex))
                B["layers"].append(dict(key=C["key"], error=repr(ex)[:400]))
                B["error"] = repr(ex)[:300]
                blocks.append(B)
                R["steps"]["blocks"] = blocks
                dump()
                if isinstance(ex, TimeoutError) or "超时" in str(ex):
                    return EXIT_HANG
                return EXIT_ERR
            d12 = int(np.count_nonzero(r1["_got"] != r2["_got"]))
            rec = dict(key=C["key"], cin=C["cin"], cout=C["cout"], k=C["k"],
                       out_n=r1["out_n"], exp_sha1=C["exp_sha1"],
                       run1={k: v for k, v in r1.items() if not k.startswith("_")},
                       run2={k: v for k, v in r2.items() if not k.startswith("_")},
                       diff_bytes_1v2=d12, same_1v2=bool(d12 == 0),
                       ok=bool(r1["oob_bytes"] == 0 and r2["oob_bytes"] == 0
                               and r1["mismatch"] == 0 and r2["mismatch"] == 0
                               and d12 == 0))
            layer_ok &= rec["ok"]
            B["layers"].append(rec)
            log("  ---- %s (cin=%d cout=%d k=%d out_n=%d) @ %g MHz"
                % (C["key"], C["cin"], C["cout"], C["k"], r1["out_n"], rb))
            log("   run1: 越界 **%5d B** | 不符 **%6d/%d** | cyc=%9d | 0x310=%s | sha1=%s"
                % (r1["oob_bytes"], r1["mismatch"], r1["out_n"], r1["cyc"],
                   r1.get("st310"), r1["sha1"][:12]))
            log("   run2: 越界 **%5d B** | 不符 **%6d/%d** | cyc=%9d | 0x310=%s | sha1=%s"
                % (r2["oob_bytes"], r2["mismatch"], r2["out_n"], r2["cyc"],
                   r2.get("st310"), r2["sha1"][:12]))
            log("   两次互比：**不同 %d B** ⇒ %s | 与 golden 逐字节：%s | 判据：%s"
                % (d12, "逐字节相同 ✓" if d12 == 0 else "**不同 ✗✗**",
                   "相同 ✓" if (r1["mismatch"] == 0 and r2["mismatch"] == 0)
                   else "不同 ✗✗", "✓ 过" if rec["ok"] else "✗✗ 不过"))
            if r1["mismatch"]:
                log("         run1 前几笔差异：%s" % (r1["first_diffs"] or []))
        B["layers_ok"] = bool(layer_ok)
        log("  [%s MHz] 逐层小结：%s" % (btag, "两层 0 越界 / 0 不符 / 两次相同 ✓"
                                        if layer_ok else "✗✗ 不达标"))
        blocks.append(B)
        R["steps"]["blocks"] = blocks
        dump()

    # ---------- 阶段 B：64² 整网（一律放在逐层之后）----------
    net_freqs = [float(x) for x in a.net_freqs.split(",") if x.strip()]
    for i, f in enumerate(net_freqs):
        btag = "%g" % f
        B = next((x for x in blocks if x["block"] == btag), None)
        if B is None:
            log("  (整网：没有 %g MHz 的逐层块，跳过)" % f)
            continue
        rb = set_fclk(f)
        nrun = a.net_runs_first if i == 0 else a.net_runs_other
        log("")
        log("################ [%s MHz] 64² 整网（连跑 %d 次）→ fclk0 读回 **%g MHz**"
            % (btag, nrun, rb))
        R.setdefault("fclk", {})[btag + "_net"] = dict(target=f, readback=rb)
        try:
            B["net"] = run_net(ov, 64, arena64, 24, rb, "net64_" + btag, nrun,
                               a.net_watchdog)
        except Exception as ex:                                  # noqa: BLE001
            traceback.print_exc()
            log("  ✗✗ 整网 64² @ %g MHz 异常：%r" % (rb, ex))
            B["net"] = dict(error=repr(ex)[:600])
            R["steps"]["blocks"] = blocks
            dump()
            if isinstance(ex, TimeoutError):
                return EXIT_HANG
            continue
        net = B["net"]
        net_ok = bool(net["sha_ok_all"] and net["same_across_runs"])
        B["clean"] = bool(B.get("layers_ok") and net_ok)
        log("  [%s MHz] 小结：逐层 %s；64² sha1 %s；%d 次相同 %s ⇒ **本频率 %s**"
            % (btag, "✓" if B.get("layers_ok") else "✗✗",
               "= 冻结值 ✓" if net["sha_ok_all"] else "≠ 冻结值 ✗✗", nrun,
               "✓" if net["same_across_runs"] else "✗✗",
               "干净 ✓" if B["clean"] else "不干净 ✗✗"))
        R["steps"]["blocks"] = blocks
        dump()

    # 第一个干净的频率（整网只跑一次的低频档也算：sha1=冻结 即算干净）
    for B in blocks:
        if B.get("clean"):
            clean_at = B["fclk"]
            break

    # ---------- 汇总 ----------
    hdr("汇总：频率 × 是否达标")
    log("  %-8s %-10s %-34s %-12s %-10s %s"
        % ("频率", "fclk读回", "两层（越界B / 不符B / 两次差B）", "64² sha1",
           "两次相同", "达标"))
    for B in blocks:
        if B.get("error"):
            log("  %-8s %-10s %-34s %-12s %-10s %s"
                % (B["block"], B["fclk"], "异常：" + B["error"][:28], "-", "-",
                   "✗✗"))
            continue
        ls = " | ".join("%d/%d/%d" % (r["run1"]["oob_bytes"],
                                      r["run1"]["mismatch"], r["diff_bytes_1v2"])
                        for r in B["layers"] if "run1" in r)
        if B["layers"] and all("run1" in r for r in B["layers"]):
            allzero = all(r["run1"]["oob_bytes"] == 0 and r["run2"]["oob_bytes"] == 0
                          and r["run1"]["mismatch"] == 0 and r["run2"]["mismatch"] == 0
                          and r["diff_bytes_1v2"] == 0 for r in B["layers"])
            ls += " ⇒ %s" % ("全 0 ✓" if allzero else "非 0 ✗✗")
        nt = B.get("net") or {}
        log("  %-8s %-10s %-34s %-12s %-10s %s"
            % (B["block"], B["fclk"], ls,
               ("冻结 ✓" if nt.get("sha_ok_all") else
                ("不冻结 ✗✗" if nt else "-")),
               ("✓" if nt.get("same_across_runs") else
                ("✗✗" if nt else "-")),
               "**达标 ✓**" if B.get("clean") else "不达标"))

    # ---------- 判定 ----------
    hdr("判定（判据由父代理任务书给定）")
    b100 = next((x for x in blocks if x["block"] == "100"), None)
    ok100 = bool(b100 and b100.get("clean"))
    if ok100:
        verdict = "100 MHz 达标（两层 0 越界/0 不符/两次逐字节相同；64² 三个 sha1 = 冻结值且两次相同）"
        rc = EXIT_OK
    elif clean_at is not None:
        verdict = "100 MHz **不达标**；从 **%g MHz** 开始干净" % clean_at
        rc = EXIT_LOWER
    else:
        verdict = "所测频率（%s MHz）全部不达标" % a.layer_freqs
        rc = EXIT_BAD
    log("  ⇒ **%s**" % verdict)
    R["verdict"] = dict(ok100=ok100, clean_at=clean_at, text=verdict)

    m = int(ov.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"])) & 0xFFFFFFFF
    s = int(ov.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
    log("  跑后 0x314 = %#010x（%s）；0x310 = %#010x；fclk0 = %g MHz"
        % (m, "仍是 magic ✓" if m == MAGIC_WANT else "✗✗ magic 变了", s,
           float(Clocks.fclk0_mhz)))
    R["steps"]["after"] = dict(magic="%#010x" % m, status="%#010x" % s,
                               fclk_now=float(Clocks.fclk0_mhz))
    log("[t103] DONE")
    dump()
    return rc


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:                                            # noqa: BLE001
        traceback.print_exc()
        try:
            R["fatal"] = traceback.format_exc()
            dump()
        except Exception:                                        # noqa: BLE001
            pass
        rc = EXIT_ERR
    sys.exit(rc)
