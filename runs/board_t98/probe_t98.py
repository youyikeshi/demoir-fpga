#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t98.py —— T-98：**v51 的 ±1 误差是"时序余量不足"还是"硬件非确定性"**

一次 load，同一个 session 内做三件事（两层：cin112_k3_d2 最严重 / cin512_k1 对照）：

  E1（确定性，100 MHz）：同一层、同一输入、**连跑 2 次**，把两次 PL 输出**逐字节互比**
      · 两次不同  ⇒ **硬件非确定性**（不是单纯时序余量不足）
      · 两次相同但与 golden 不同 ⇒ **确定性错误**（报父代理）
  E3（75 MHz）：同数据再跑 2 次
  E2（50 MHz）：同数据再跑 2 次
      · 判据：错误数随频率单调 100 > 75 > 50，且 50 MHz 归零 ⇒ **时序**

纪律（T-98 任务书）：
  · 一个进程**只 download 一次**；ulimit -c 0 由外层脚本设；只 sync 不 poweroff
  · **不改任何仓库代码**：本文件只读调用 bin_engine / bin_test_staged
  · 缓冲**每个 case 只分配一次**（同地址跨频率、跨两次 run）⇒ 地址不成为自变量
  · 每次 run 前把输出区重铺 0x5A、尾部 64 KB 重铺 0xA5 ⇒ 越界计数与两次运行可比
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import sys
import time
import traceback

# ============================================================================
# ★ 0. 运行点必须在 import bin_engine 之前写进环境（T-54 §6；与 T-96 完全一致）
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
    ("Q8CACHE_MB", "32"),
]
FORBID = ("BIN_V50_FUSE", "BIN_V50_CHAIN", "BIN_V50_PLQ8", "BIN_V50_REPROBE",
          "BIN_V50_BAND_ROWS", "BIN_V50_CHAIN_MAGIC",
          "BIN_SAM_TILE", "BIN_SAM_SLAB_KB", "BIN_DEQ_TILE",
          "BIN_MMIO_POLL", "BIN_MMIO_FAST", "BIN_MMIO_C",
          "BIN_CONVPF_C", "BIN_F32POOL", "BIN_CAT_STRICT", "BIN_VLANES")


def setup_env():
    for k in list(os.environ):
        if k in FORBID or k.startswith("BIN_V50_"):
            os.environ.pop(k, None)
    for k, v in RUNPOINT:
        os.environ[k] = v
    keys = [x for x, _ in RUNPOINT] + ["BIN_VLANES"]
    return {k: os.environ.get(k, "") for k in keys}


ENV_AT_START = setup_env()

import numpy as np                                                   # noqa: E402

PS_DIR = os.environ.get("T98_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

MAGIC_WANT = 0x5030_0001
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
PAD = 64 * 1024
MARK = 0xA5
EXIT_OK, EXIT_FP, EXIT_ERR, EXIT_HANG = 0, 2, 4, 5

R = {"env_at_start": ENV_AT_START, "steps": {}}
LOGPATH = None
_fh = None
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"


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
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t98.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(R, f, indent=1, ensure_ascii=False, default=str)
        log("[json] 已落盘 %s" % path)
    except Exception as ex:                                      # noqa: BLE001
        log("[json] 落盘失败：%r" % (ex,))


try:                                                   # PYNQ 3.1 不自动注册 Device
    import pynq.pl_server.embedded_device as _emb      # noqa: F401
except Exception:                                      # noqa: BLE001
    pass
from pynq import Overlay, Clocks                       # noqa: E402

import bin_engine as BE                                # noqa: E402
import bin_test_staged as BS                           # noqa: E402

log("[t98] python=%s numpy=%s mach=%s" % (sys.version.split()[0], np.__version__,
                                          platform.machine()))
log("[t98] vlanes_now()=%d prm_layout=%s BIN_V50_* 残留=%s"
    % (BE.vlanes_now(), BE.prm_layout(),
       [k for k in os.environ if k.startswith("BIN_V50_")]))


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
# ① load + 指纹（本进程唯一一次 download=True）
# ============================================================================
def step1_load(bit):
    hdr("① load overlay + 指纹（本进程唯一一次 download=True）")
    t0 = time.time()
    ov = Overlay(bit, download=True)
    dt = time.time() - t0
    ips = sorted(ov.ip_dict.keys())
    log("  Overlay(%s) 用时 %.3f s" % (os.path.basename(bit), dt))
    log("  IP 清单：%s" % ips)
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
    st310 = int(magic["0x310 CHAIN_STATUS"]["hex"], 16)
    log("  **magic(0x314) = %#010x 期望 %#010x ⇒ %s**"
        % (m, MAGIC_WANT, "v51 寄存器块在位 ✓" if magic_ok else "✗✗ ⇒ 停"))
    R["steps"]["load"] = dict(bit=bit, seconds=round(dt, 4), ips=ips, addr=got,
                              magic=magic, magic_ok=bool(magic_ok),
                              status310=st310, downloads=1)
    return ov, bool(ok_all and magic_ok)


def set_fclk(mhz):
    Clocks.fclk0_mhz = float(mhz)
    time.sleep(0.4)
    return float(Clocks.fclk0_mhz)


# ============================================================================
# ② 单层准备（★ 每个 case 只分配一次：跨频率/跨两次 run 地址恒定）
# ============================================================================
CASES = [
    ("cin112_k3_d2", "cin112 k3 d2 (cb=2 K=18)",
     lambda L: L["cin"] == 112 and L["cout"] == 64 and L["k"] == 3 and L["d"] == 2),
    ("cin512_k1", "cin512 k1    (cb=8 K=8) ",
     lambda L: L["cin"] == 512 and L["cout"] == 192 and L["k"] == 1),
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
             timeout=timeout, x_sha1=sha1_of(x), act_sha1=sha1_of(act))
    log("  准备 %-14s %s  cin=%d cout=%d k=%d %dx%d→%dx%d K=%d"
        "  OUT=%d B  act=%d B  blob=%d B  地址 ob=%#x ab=%#x blob=%#x"
        % (C["name"][:14], "✓", C["cin"], C["cout"], C["k"], H, W,
           C["Hout"], C["Wout"], C["K"], need, len(act), len(blob),
           int(opa), int(apa), int(gpa)))
    return C


def run_once(e, C, fclk, label):
    """一次 PL 运行；返回原始输出字节（copy）+ 越界计数 + 周期。"""
    ob, need = C["ob"], C["need"]
    # ★ 关键：多层共用一个 engine ⇒ 每次 run 前必须把 blob 指回本 case（否则用错权重）
    e.blob, e.blob_pa = C["gb"], C["gb_pa"]
    v = u8(ob)
    v[:] = 0x5A                        # 输出区先铺非 MARK 的底
    v[need:] = MARK                    # 尾部 64 KB 垫子
    ob.flush()
    t0 = time.time()
    rs = e.run(C["ab"], ob, L=C["Lr"], timeout=C["timeout"], verify_canary=False)
    wall = time.time() - t0
    ob.invalidate()
    gg = u8(ob)
    out = np.array(gg[:need], copy=True)
    tail = gg[need:]
    obad = np.nonzero(tail != MARK)[0]
    got = np.frombuffer(out.tobytes(), np.int8)
    mbad = np.nonzero(got != C["exp"])[0]
    first = [dict(i=int(i), got=int(got[i]), want=int(C["exp"][i])) for i in mbad[:8]]
    pairs = {}
    for i in mbad[:200000]:
        k2 = (int(C["exp"][i]), int(got[i]))
        pairs[k2] = pairs.get(k2, 0) + 1
    top_pairs = sorted(([dict(want=a, got=b, n=c) for (a, b), c in pairs.items()]),
                       key=lambda d: -d["n"])[:6]
    d = dict(label=label, fclk=fclk, wall=round(wall, 5),
             cyc=int(rs.seconds * fclk * 1e6 + 0.5),
             oob_bytes=int(len(obad)), oob_lo=int(obad[0]) if len(obad) else 0,
             oob_hi=(int(obad[-1]) + 1) if len(obad) else 0,
             mismatch=int(len(mbad)), out_n=int(len(got)),
             sha1=sha1_of(out), first_diffs=first, top_pairs=top_pairs)
    d["_out"] = out
    d["_got"] = got
    return d


# ============================================================================
# ③ 主循环：100(E1) → 75(E3) → 50(E2) → 100 复跑（同一 session）
# ============================================================================
BLOCKS = [("100", 100.0, "E1 100 MHz（同层连跑 2 次，逐字节互比）"),
          ("75", 75.0, "E3 75 MHz（同数据再跑 2 次）"),
          ("50", 50.0, "E2 50 MHz（同数据再跑 2 次）"),
          ("100b", 100.0, "E1' 回到 100 MHz 再跑 2 次（查是否与首轮 100 逐字节相同）")]


def main():
    global LOGPATH, _fh, PACK
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v51.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--hw", type=int, default=32)
    ap.add_argument("--timeout", type=float, default=4.0)
    ap.add_argument("--seed", type=int, default=20261031)
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t98_out")
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "t98_%s.log" % time.strftime("%H%M%S"))
    _fh = open(LOGPATH, "a", buffering=1, encoding="utf-8")
    PACK = a.pack
    R["args"] = vars(a)
    R["runpoint"] = ENV_AT_START
    log("[t98] 日志 = %s" % LOGPATH)
    log("[t98] bit = %s" % a.bit)

    ov, fp_ok = step1_load(a.bit)
    if not fp_ok:
        log("  ⇒ **停**：指纹不合格")
        dump()
        return EXIT_FP

    try:
        kl = BE.load_kernels()
        log("  内核装载：%s" % {k: (round(float(v.get("seconds") or 0), 3),
                                  v.get("loaded")) for k, v in kl.items()})
    except Exception as ex:                                      # noqa: BLE001
        log("  内核装载失败（逐层路径不需要，不致命）：%r" % (ex,))
    log("  %s" % deps_line())

    hdr("② 逐层缓冲准备（每个 case 只分配一次 ⇒ 跨频率/跨 run 物理地址恒定）")
    bl = BS.Blob(PACK)
    pool = BE.BinArena(32 << 20, name="t98_layer_pool")
    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    e.add_region(pool.buf, "pool")
    cases = []
    try:
        for key, tag, pred in CASES:
            C = prepare_case(e, bl, pool, pred, a.hw, a.hw, a.seed, a.timeout)
            C["key"] = key
            C["tag"] = tag
            cases.append(C)
    except Exception as ex:                                      # noqa: BLE001
        traceback.print_exc()
        log("  ✗ 准备失败：%r" % (ex,))
        R["steps"]["prepare"] = dict(error=repr(ex)[:500])
        dump()
        return EXIT_ERR

    hdr("③ E1/E3/E2：同一 session、同一输入、每层每频率连跑 2 次")
    rows = []
    for btag, f, desc in BLOCKS:
        rb = set_fclk(f)
        log("")
        log("---- [%s] %s  → fclk0 读回 **%g MHz**" % (btag, desc, rb))
        R.setdefault("fclk", {})[btag] = dict(target=f, readback=rb)
        for C in cases:
            try:
                r1 = run_once(e, C, rb, btag + "#1")
                r2 = run_once(e, C, rb, btag + "#2")
            except Exception as ex:                              # noqa: BLE001
                traceback.print_exc()
                log("  ✗✗ %s @ %g MHz 异常：%r" % (C["key"], rb, ex))
                rows.append(dict(block=btag, fclk=rb, key=C["key"],
                                 error=repr(ex)[:300], ok=False))
                R["steps"]["runs"] = rows
                dump()
                if isinstance(ex, TimeoutError):
                    return EXIT_HANG
                continue
            d12 = int(np.count_nonzero(r1["_got"] != r2["_got"]))
            same12 = (d12 == 0)
            rec = dict(block=btag, fclk=rb, key=C["key"],
                       addr=dict(ob="%#x" % C["ob_pa"], ab="%#x" % C["ab_pa"],
                                 blob="%#x" % C["gb_pa"]),
                       run1={k: v for k, v in r1.items() if not k.startswith("_")},
                       run2={k: v for k, v in r2.items() if not k.startswith("_")},
                       diff_bytes_1v2=d12, same_1v2=bool(same12),
                       golden=r1["sha1"],  # placeholder, replaced below
                       exp_sha1=sha1_of(C["exp"]))
            rec.pop("golden")
            rows.append(rec)
            log("   %-28s run1: 不符 **%6d/%d** 越界 %d B cyc=%d sha1=%s"
                % (C["key"], r1["mismatch"], r1["out_n"], r1["oob_bytes"],
                   r1["cyc"], r1["sha1"][:12]))
            log("   %-28s run2: 不符 **%6d/%d** 越界 %d B cyc=%d sha1=%s"
                % ("", r2["mismatch"], r2["out_n"], r2["oob_bytes"],
                   r2["cyc"], r2["sha1"][:12]))
            log("   %-28s **两次互比：不同 %d B** ⇒ %s | 与 golden: run1 %s, run2 %s"
                % ("", d12, "逐字节相同 ✓" if same12 else "**不同 ✗✗（非确定性）**",
                   "相同" if r1["mismatch"] == 0 else "不同(%d)" % r1["mismatch"],
                   "相同" if r2["mismatch"] == 0 else "不同(%d)" % r2["mismatch"]))
            if r1["mismatch"]:
                log("         run1 want→got 最多：%s" % (r1["top_pairs"] or []))
            R["steps"]["runs"] = rows
            dump()

    # ---------------- 汇总 ----------------
    hdr("④ 汇总表（mismatch / out_n；diff_1v2 = 同一 block 内两次互比的不同字节）")

    def cell(block, key, field):
        for r in rows:
            if r.get("block") == block and r.get("key") == key and "run1" in r:
                return r["run1"][field]
        return None

    tab = []
    for key, tag, _p in CASES:
        line = dict(key=key)
        for btag, f, _d in BLOCKS:
            m1 = cell(btag, key, "mismatch")
            r = next((x for x in rows if x.get("block") == btag
                      and x.get("key") == key and "run1" in x), None)
            line[btag] = dict(fclk=r["fclk"] if r else None, n1=m1,
                              n2=(r["run2"]["mismatch"] if r else None),
                              diff12=(r["diff_bytes_1v2"] if r else None))
        tab.append(line)
        log("  %s：%s" % (key, {k: v for k, v in line.items() if k != "key"}))

    log("")
    log("  频率 → 错误数（run1/run2；out_n 见上）")
    log("  %-14s %14s %14s %14s %14s" % ("层", "100 MHz", "75 MHz", "50 MHz", "100 MHz(复跑)"))
    for line in tab:
        log("  %-14s %14s %14s %14s %14s"
            % (line["key"],
               "%s/%s" % (line["100"]["n1"], line["100"]["n2"]),
               "%s/%s" % (line["75"]["n1"], line["75"]["n2"]),
               "%s/%s" % (line["50"]["n1"], line["50"]["n2"]),
               "%s/%s" % (line["100b"]["n1"], line["100b"]["n2"])))

    # ---------------- 判定 ----------------
    hdr("⑤ 判定（判据由父代理任务书给定，本脚本只做机械比较）")
    e1_same = all(r["same_1v2"] for r in rows
                  if r.get("block") == "100" and "same_1v2" in r)
    e1_n = [r["run1"]["mismatch"] for r in rows
            if r.get("block") == "100" and "run1" in r]
    log("  E1（100 MHz，同层连跑两次逐字节）：**%s**（diff_1v2 = %s；错误数 %s）"
        % ("两次完全相同" if e1_same else "两次不同 ⇒ 非确定性",
           [r["diff_bytes_1v2"] for r in rows if r.get("block") == "100"
            and "diff_bytes_1v2" in r], e1_n))
    for btag, f, _d in BLOCKS[1:]:
        ns = [(r["run1"]["mismatch"], r["run2"]["mismatch"]) for r in rows
              if r.get("block") == btag and "run1" in r]
        log("  %s（%g MHz）：错误数 %s %s"
            % (btag, f, ns, "⇒ 归零 ✓" if all(a == 0 and b == 0 for a, b in ns)
               else ""))
    mono_ok = True
    for key, _t, _p in CASES:
        n100 = cell("100", key, "mismatch")
        n75 = cell("75", key, "mismatch")
        n50 = cell("50", key, "mismatch")
        if None in (n100, n75, n50):
            continue
        mono_ok &= (n100 >= n75 >= n50)
        log("  单调性 %-14s 100=%s ≥ 75=%s ≥ 50=%s : %s"
            % (key, n100, n75, n50, "✓" if (n100 >= n75 >= n50) else "✗"))
    e2_zero = all(cell("50", k, "mismatch") == 0 for k, _t, _p in CASES)
    if not e1_same:
        verdict = "硬件非确定性（两次不同 ⇒ 不是单纯时序余量不足）"
    elif e2_zero and mono_ok:
        verdict = "时序（两次相同 ⇒ 确定性；错误随频率单调下降并在 50 MHz 归零）"
    elif e1_same and not e2_zero:
        verdict = "确定性错误但降频不消失 ⇒ 不是时序余量（更像设计/映射错，需回炉）"
    else:
        verdict = "确定性错误、降频部分改善但未归零 ⇒ 时序+设计混合（需再分）"
    log("  ⇒ **结论：%s**" % verdict)
    R["verdict"] = dict(e1_same=bool(e1_same), e2_zero=bool(e2_zero),
                        mono=bool(mono_ok), text=verdict, table=tab,
                        e1_errs=e1_n)

    m = int(ov.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"])) & 0xFFFFFFFF
    s = int(ov.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
    log("  跑后 0x314 = %#010x（%s）；0x310 = %#010x"
        % (m, "仍是 magic ✓" if m == MAGIC_WANT else "✗✗ magic 变了", s))
    R["steps"]["after"] = dict(magic="%#010x" % m, status="%#010x" % s,
                               fclk_now=float(Clocks.fclk0_mhz))
    log("[t98] DONE")
    dump()
    return EXIT_OK


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
