#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t127.py —— T-127：**链式两 op**（消费者读生产者 PL 写出的 C8 输出）。

任务唯一：判定「层间 C8 交接」是不是 v53 在 128²/256² 出错的原因。
这是**所有单层测试结构上都没覆盖**的口径：
    T-122 的单层测试，输入是 PS/numpy 准备好的 act 字节（push_input）；
    整网里 op N 的输入是 **op N-1 由 PL 写出的 C8 输出**经 E1E2（PL, R_RUN_UNIT=4）
    打包得到的。本探针只补这一条边。

一个进程、一次 load、只跑单层/双 op（**不跑整网**）。

几何（基分辨率 128² ⇒ 特征图 64×64）：
    A（生产，= 整网 op15 的几何）：cin=112 cout=32 k=1 s=1 pad=0 64×64 → OUT 131 072 B
    B（消费，= 整网 op13 的几何）：cin=32  cout=32 k=3 s=1 pad=1 64×64 → OUT 131 072 B
    64² 对照（基分辨率 64² ⇒ 32×32）：同一对层，H=W=32

三个必测口径（每个几何都做）：
  ① CHAIN_D   ：A 跑完 → **直接从 A 的 OUT 槽**（物理地址）用 E1E2 打包 → 写进 B 的 IN 槽 → B
                 （= BIN_LAZY_DEQ=1 的 C8 直读路径；PS 一个字节都不碰激活）
  ② CHAIN_P   ：A 跑完 → read_out(f32) → PS 量化 → E1E2 打包 → push_input → B
                 （= 整网实际跑点 BIN_PACK=e1e2 / BIN_LAZY_DEQ=0 的那条路）
  ③ CTL1      ：B 的输入由 PS/numpy 直接准备（**不经 A**）⇒ 应全对（= T-122 已证的口径）
      CTL2    ：E1E2 从**PS 写进 DDR 的** A 参照 C8 打包 → B（隔离「源缓冲是谁写的」）
  每个几何 CHAIN 连跑 2 次 ⇒ 判确定性。

参照（主机侧 numpy，同一 C8 布局）：
  refA  = conv_rtl(cin112,cout32,k1) → to_c8
  交接  ：x_q = sat8(x·2^(fs_in_B - fs_out_A))（整数精确；d=0 时 x_q == refA）
  refB  = conv_rtl(cin32,cout32,k3) 以 x_q 为输入 → to_c8

判据：
  · 越界字节 = 0（金丝雀尾不被写）
  · A、B 输出逐字节 = 参照
  · 交接字节（B 的 IN 槽实际内容）逐字节 = 参照 act
  · 两次逐字节相同
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

import numpy as np

# ============================================================================
# ★ 0. 冻结运行点必须写在 import bin_engine 之前（T-54 §6）
#    与 T-124/T-125 逐项相同；BIN_PACK=e1e2 是整网真实跑点（E1E2 参与交接）
# ============================================================================
RUNPOINT = [
    ("BIN_PACK", "e1e2"),
    ("BIN_LAZY_DEQ", "0"),
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
          "BIN_CONVPF_C", "BIN_F32POOL", "BIN_CAT_STRICT", "BIN_VLANES",
          "BIN_LAZY_SLOTS_OVERRIDE")


def setup_env():
    for k in list(os.environ):
        if k in FORBID or k.startswith("BIN_V50_"):
            os.environ.pop(k, None)
    for k, v in RUNPOINT:
        os.environ[k] = v
    keys = [x for x, _ in RUNPOINT] + ["BIN_V50_FUSE", "BIN_VLANES"]
    return {k: os.environ.get(k, "") for k in keys}


ENV_AT_START = setup_env()

PS_DIR = os.environ.get("T127_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

MAGIC_WANT = 0x5030_0001
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
PAD = 64 * 1024
MARK = 0xA5
FILL = 0x5A
EXIT_OK, EXIT_FP, EXIT_LOWER, EXIT_BAD, EXIT_ERR, EXIT_HANG = 0, 2, 3, 4, 6, 5

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


def raw_bytes(a):
    """bytes / bytearray / memoryview / ndarray → 裸字节（**不要** np.asarray(bytes)：
    numpy≥1.20 会把它当成 S 标量，np.asarray(b'..', np.int8) 直接抛 ValueError）。"""
    if isinstance(a, (bytes, bytearray, memoryview)):
        return bytes(a)
    return np.ascontiguousarray(a).tobytes()


def sha1_of(a):
    return hashlib.sha1(raw_bytes(a)).hexdigest()


def dump():
    path = LOGPATH.replace(".log", ".json") if LOGPATH else "t127.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(R, f, indent=1, ensure_ascii=False, default=str)
        log("[json] 已落盘 %s" % path)
    except Exception as ex:                                      # noqa: BLE001
        log("[json] 落盘失败：%r" % (ex,))


def save(name, arr):
    """裸字节落盘（分析在主机侧做）。"""
    p = os.path.join(OUTDIR, name)
    try:
        with open(p, "wb") as f:
            f.write(raw_bytes(arr))
        return p
    except Exception as ex:                                      # noqa: BLE001
        log("    ⚠ 落盘 %s 失败：%r" % (name, ex))
        return None


# ---- 看门狗 ---------------------------------------------------------------
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
import bin_ref_numpy as BR                             # noqa: E402

log("[t127] python=%s numpy=%s mach=%s" % (sys.version.split()[0], np.__version__,
                                           platform.machine()))
log("[t127] vlanes_now()=%d prm_layout=%s BIN_V50_* 残留=%s"
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
# 第 1 步：load + 指纹（本进程唯一一次 download=True）
# ============================================================================
def step1_load(bit, magic_required):
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
        try:
            ba = int(getattr(ov, name).mmio.base_addr)
            ln = int(getattr(ov, name).mmio.length)
        except Exception as ex:                                  # noqa: BLE001
            log("  %-11s 找不到（%r）" % (name, ex))
            got[name] = dict(base=None, length=None, ok=False)
            ok_all = False
            continue
        ok = (ba == exp)
        ok_all &= ok
        got[name] = dict(base="%#010x" % ba, length=ln, ok=bool(ok))
        log("  %-11s base=%#010x length=%#x （期望 %#010x）%s"
            % (name, ba, ln, exp, "✓" if ok else "（与 v51 不同，记录）"))
    base = int(getattr(ov, "bin_unit_0").mmio.base_addr)
    magic = {}
    for tag, off in (("CFG+0x314", BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"]),
                     ("0x310 CHAIN_STATUS", BE.V50_CHAIN_REG["CHAIN_STATUS"]),
                     ("0x308 BAND_ROWS", BE.V50_CHAIN_REG["BAND_ROWS"])):
        v = int(ov.bin_unit_0.mmio.read(int(off))) & 0xFFFFFFFF
        magic[tag] = dict(abs_addr="%#010x" % (base + int(off)), hex="%#010x" % v)
        log("  %#010x = %#010x   (%s)" % (base + int(off), v, tag))
    m = int(magic["CFG+0x314"]["hex"], 16)
    magic_ok = (m == MAGIC_WANT)
    log("  **magic(0x314) = %#010x 期望 %#010x ⇒ %s**"
        % (m, MAGIC_WANT, "寄存器块在位 ✓" if magic_ok
           else ("✗✗ ⇒ 停" if magic_required else "（豁免）")))
    fclk0_now = float(Clocks.fclk0_mhz)
    log("  load 后 fclk0 读回 = %g MHz" % fclk0_now)
    R["steps"]["load"] = dict(bit=bit, seconds=round(dt, 4), ips=ips,
                              has_fconv=bool(fconv), addr=got, magic=magic,
                              magic_ok=bool(magic_ok), magic_required=bool(magic_required),
                              fclk0_at_load=fclk0_now, downloads=1)
    ok = bool(ok_all and (magic_ok or not magic_required) and not fconv)
    R["steps"]["load"]["fp_ok"] = ok
    return ov, ok


def set_fclk(mhz):
    Clocks.fclk0_mhz = float(mhz)
    time.sleep(0.4)
    return float(Clocks.fclk0_mhz)


def pl_state(ov):
    """读 magic / 0x310（第 119 条：改频前后必须重读）。"""
    base = int(ov.bin_unit_0.mmio.base_addr)
    m = int(ov.bin_unit_0.mmio.read(int(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"]))) & 0xFFFFFFFF
    s = int(ov.bin_unit_0.mmio.read(int(BE.V50_CHAIN_REG["CHAIN_STATUS"]))) & 0xFFFFFFFF
    return dict(base="%#010x" % base, magic="%#010x" % m, magic_ok=(m == MAGIC_WANT),
                st310="%#010x" % s, fclk=float(Clocks.fclk0_mhz))


# ============================================================================
# 比较工具
# ============================================================================
def cmp_bytes(got, ref):
    """逐字节比较 + "从第一个错到末尾都错"（连续尾巴）指纹。"""
    got = np.frombuffer(raw_bytes(got), np.int8)
    ref = np.frombuffer(raw_bytes(ref), np.int8)
    n = int(min(got.size, ref.size))
    bad = np.nonzero(got[:n] != ref[:n])[0]
    d = dict(n=int(n), nbad=int(bad.size),
             first=(int(bad[0]) if bad.size else None),
             last=(int(bad[-1]) if bad.size else None),
             sha1_got=sha1_of(got), sha1_ref=sha1_of(ref))
    if bad.size:
        # 连续尾巴判据：从 first 到末尾**全错** 且 first 之前**全对**
        d["tail_all_wrong"] = bool(bad.size == n - int(bad[0]))
        d["words_from_first"] = int(bad[0] // 8)
        d["nwords"] = int(n // 8)
        d["tail_frac"] = round(float(bad.size) / n, 6)
    else:
        d["tail_all_wrong"] = False
    return d


def check_oob(buf, need):
    """OUT 区后面的金丝雀尾有没有被写。"""
    tail = u8(buf)[need:]
    bad = np.nonzero(tail != MARK)[0]
    return int(bad.size), (int(bad[0]) if bad.size else None)


# ============================================================================
# 第 2 步：几何 + 参照（numpy）
# ============================================================================
def pick_layers(bl):
    """按整网 op15/op13 的几何取真实层（权重 = 整网权重，不切片）。

    A：cin=112 cout=32 k=1 s=1 d=1 pad=0
       · 首选 `b4.ghost_layers.1.primary_conv`（fs_out=5）⇒ 与 B 的 fs_in=5 **d=0**
         （纯布局交接，无重标定，信号最干净）
       · 备选 `b0.ghost_layers.1.primary_conv`（fs_out=4）⇒ d=1（含 ×2 重标定）
    B：cin=32 cout=32 k=3 s=1 d=1 pad=1 且 fs_in=5
       · `b0.ghost_layers.0.cheap_operation`（= 整网 op13 本体）
    """
    def find(pred, prefer=()):
        cands = [x for x in bl.layers if pred(x)]
        for p in prefer:
            for x in cands:
                if p in x["name"]:
                    return x
        return cands[0] if cands else None

    A0 = find(lambda x: (x["cin"], x["cout"], x["k"], x["s"], x["d"], x["pad"])
              == (112, 32, 1, 1, 1, 0) and int(x["act_fs_out"]) == 5,
              prefer=("b4.ghost_layers.1.primary_conv",))
    A1 = find(lambda x: (x["cin"], x["cout"], x["k"], x["s"], x["d"], x["pad"])
              == (112, 32, 1, 1, 1, 0) and int(x["act_fs_out"]) == 4,
              prefer=("b0.ghost_layers.1.primary_conv",))
    B = find(lambda x: (x["cin"], x["cout"], x["k"], x["s"], x["d"], x["pad"])
             == (32, 32, 3, 1, 1, 1) and int(x["act_fs_in"]) == 5,
             prefer=("b0.ghost_layers.0.cheap_operation",))
    return A0, A1, B


def golden_A(bl, rec, H, W, xA):
    """A 的 numpy 参照：(actA u64, refA_logical int8, refA_c8 bytes)。"""
    P = bl.params(rec)
    L = BE.BinLayer(rec, H, W)
    A = BR.binarize_pack(xA, P["t_q"])
    ref, _raw = BR.conv_rtl(L.cin, L.cout, L.k, L.s, L.dil, L.pad, L.shift, A, P)
    return A, np.asarray(ref, np.int8), BR.to_c8(ref), P


def handoff_xq(refA_log, fs_out, fs_in):
    """交接的 PS 参照：`sat8(x·2^(fs_in-fs_out))`（整数精确；d>=0 时 = clip(x<<d)）。

    E1E2 的等价判据：`sat8(rint(x·2^d)) + t_q > 0`（bin_engine._build_desc 注释）。
    同时用 float32 路径交叉复核，二者必须逐字节相同。
    """
    d = int(fs_in) - int(fs_out)
    x = np.asarray(refA_log, np.int64)
    if d >= 0:
        qi = np.clip(x << d, -128, 127).astype(np.int8)
    else:
        # 负数位移：按 quantize_int8 的定义（rint 后 clip）
        qi = np.clip(np.rint(x.astype(np.float64) * (2.0 ** d)),
                     -128, 127).astype(np.int8)
    qf = BE.quantize_int8(x.astype(np.float32) * np.float32(2.0 ** (-int(fs_out))),
                          int(fs_in))
    same = bool(np.array_equal(qi.astype(np.int8), np.asarray(qf, np.int8)))
    return np.asarray(qi, np.int8), d, same, np.asarray(qf, np.int8)


def golden_B(bl, rec, H, W, xq):
    """B 的 numpy 参照：输入是**交接后的 int8**。"""
    P = bl.params(rec)
    L = BE.BinLayer(rec, H, W)
    A = BR.binarize_pack(xq, P["t_q"])
    ref, _raw = BR.conv_rtl(L.cin, L.cout, L.k, L.s, L.dil, L.pad, L.shift, A, P)
    return A, np.asarray(ref, np.int8), BR.to_c8(ref), P


# ============================================================================
# 第 3 步：执行
# ============================================================================
def run_conv(e, inb, ob, L, need_out, timeout, blob_pa):
    """一次单层 conv：填 OUT 为 0x5A + 金丝雀，跑，回读（invalidate 后）判越界/内容。"""
    v = u8(ob)
    v[:] = FILL
    v[need_out:] = MARK
    ob.flush()
    e.blob_pa = blob_pa
    t0 = time.time()
    rs = with_alarm(timeout + 30.0, e.run, inb, ob, L=L, timeout=timeout,
                    verify_canary=False)
    wall = time.time() - t0
    try:
        st310 = int(e._rd(BE.CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
    except Exception:                                            # noqa: BLE001
        st310 = None
    ob.invalidate()
    obad, ofirst = check_oob(ob, need_out)
    got = np.array(u8(ob)[:need_out], copy=True)
    return dict(got=got, oob=int(obad), oob_first=ofirst, wall=round(wall, 5),
                cyc=int(rs.seconds * float(Clocks.fclk0_mhz) * 1e6 + 0.5),
                st310=("%#010x" % st310) if st310 is not None else None,
                sha1=sha1_of(got))


def read_region(buf, n):
    """invalidate 后读一块（cacheable 的 arena 必须靠 invalidate）。"""
    buf.invalidate()
    return np.array(u8(buf)[:int(n)], copy=True)


def pack_from_pa(ee, pa, C, H, W, fs, Ldst, t_q, out_buf=None):
    """E1E2：从物理地址 pa 的 C8 缓冲打包成 dst 层的 act 字节。

    out_buf 给 ⇒ E1E2 直接写进那块（= C8 直读路径，PS 不碰激活）；
    否则 ⇒ 返回 E1E2 内部 out 视图的只读拷贝（调用方再 push_input）。
    """
    segs = [dict(kind="i8", C=int(C), H=int(H), W=int(W), fs=int(fs), pa=int(pa))]
    data = ee.pack_segments(segs, Ldst, t_q=t_q, out_buf=out_buf)
    if data is None:
        return None
    return np.array(data, copy=True)


def main():
    global LOGPATH, _fh, PACK, OUTDIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v53.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--seed", type=int, default=20261103)
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t127_out")
    ap.add_argument("--freq", type=float, default=50.0)
    ap.add_argument("--magic-required", type=int, default=1)
    ap.add_argument("--geoms", default="64,32",
                    help="逗号分隔的特征图边长（64=基分辨率 128², 32=基分辨率 64²）")
    ap.add_argument("--no-e1e2", type=int, default=0, help="1=不挂 E1E2（只做 PS 口径）")
    ap.add_argument("--tag", default="t127")
    a = ap.parse_args()

    OUTDIR = a.outdir
    os.makedirs(a.outdir, exist_ok=True)
    LOGPATH = os.path.join(a.outdir, "t127_%s.log" % time.strftime("%H%M%S"))
    _fh = open(LOGPATH, "a", buffering=1, encoding="utf-8")
    PACK = a.pack
    R["args"] = vars(a)
    R["runpoint"] = ENV_AT_START
    log("[t127] 日志 = %s" % LOGPATH)
    log("[t127] bit = %s" % a.bit)
    log("[t127] 运行点 = %s" % json.dumps(ENV_AT_START, ensure_ascii=False))

    ov, fp_ok = step1_load(a.bit, bool(a.magic_required))
    if not fp_ok:
        log("  ⇒ **停**：指纹/magic 不合格（报父代理，不继续）")
        dump()
        return EXIT_FP

    bl = BR.Blob(PACK)
    log("  权重容器：%d 层，blob=%.2f MB" % (len(bl.layers), bl.size / 1e6))

    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    blob_buf, blob_pa = e.load_weights(path=os.path.join(PACK, "bin_weights.bin"))
    _nb = int(np.prod(getattr(blob_buf, "shape", (0,))))
    log("  权重已上板：pa=%#010x（%.2f MB）" % (blob_pa, _nb / 1e6))

    hdr("第 1 步（续）：fclk + 指纹 + 挂 E1E2")
    rb = set_fclk(a.freq)
    st = pl_state(ov)
    log("  目标 %g MHz → **fclk0 读回 %g MHz**；magic=%s 0x310=%s（第 119 条）"
        % (a.freq, rb, st["magic"], st["st310"]))
    R.setdefault("fclk", {})["layer"] = dict(target=a.freq, readback=rb, **st)
    log("  %s" % deps_line())
    R["steps"]["deps"] = dict(line=deps_line(), closure_ok=bool(BE.deps_closure_ok()))

    ee = None
    if not a.no_e1e2:
        try:
            ee = e.enable_e1e2()          # 内部已做 probe（写 4 读回、写 0 读回）
            log("  E1E2 已挂上（R_RUN_UNIT=4，probe 通过；clk=%g MHz）"
                % float(getattr(ee, "fclk_mhz", 0.0)))
            R["steps"]["e1e2"] = dict(ok=True)
        except Exception as ex:                                  # noqa: BLE001
            log("  ✗ E1E2 挂不上：%r ⇒ 只做 PS 口径（交接的 PL 侧无法测）" % (ex,))
            R["steps"]["e1e2"] = dict(ok=False, err=repr(ex)[:300])
            ee = None
    else:
        log("  （--no-e1e2 ⇒ 只做 PS 口径）")
        R["steps"]["e1e2"] = dict(ok=False, skipped=True)

    pool = None
    for _mb in (4, 2, 1):
        try:
            pool = BE.BinArena(int(_mb) << 20, name="t127_pool")
            log("  arena %d MB @%#010x" % (_mb, pool.pa))
            break
        except Exception as _ex:                                  # noqa: BLE001
            log("  ⚠ arena %d MB 分配失败（%r）⇒ 降档" % (_mb, _ex))
    if pool is None:
        log("  ✗ arena 连 1 MB 都分配不到（CMA 不足）⇒ 报，不继续")
        dump()
        return EXIT_ERR
    e.add_region(pool.buf, "pool")

    A0, A1, B = pick_layers(bl)
    if A0 is None or B is None:
        log("  ✗ 权重容器里找不到需要的层（A0=%r B=%r）" % (A0, B))
        dump()
        return EXIT_ERR
    log("  A （d=0 链）=%s" % A0["name"])
    log("  A'（d=1 链）=%s" % (A1["name"] if A1 else "<无>"))
    log("  B            =%s" % B["name"])
    R["steps"]["layers"] = dict(
        A=dict(name=A0["name"], idx=A0["idx"], cin=A0["cin"], cout=A0["cout"],
               k=A0["k"], s=A0["s"], pad=A0["pad"], shift=A0["shift"],
               fs_in=A0["act_fs_in"], fs_out=A0["act_fs_out"]),
        A1=(dict(name=A1["name"], idx=A1["idx"], fs_in=A1["act_fs_in"],
                 fs_out=A1["act_fs_out"]) if A1 else None),
        B=dict(name=B["name"], idx=B["idx"], cin=B["cin"], cout=B["cout"],
               k=B["k"], s=B["s"], pad=B["pad"], shift=B["shift"],
               fs_in=B["act_fs_in"], fs_out=B["act_fs_out"]))

    geoms = [int(x) for x in a.geoms.split(",") if x.strip()]
    hdr("第 2 步：链式 A→B（%s），fclk=%.1f MHz" % (geoms, rb))
    all_ok = True
    block = dict(geoms=[], ok=None)
    R["steps"]["chain"] = block

    for H in geoms:
        W = H
        block["geoms"].append(run_geometry(e, ee, pool, bl, A0, A1, B, H, W,
                                           a.timeout, rb, blob_pa))
        g = block["geoms"][-1]
        all_ok &= bool(g.get("all_ok"))
        dump()

    block["ok"] = bool(all_ok)
    R["verdict"] = dict(all_ok=bool(all_ok), stopped="chain_only_T127")
    log("")
    log("  [T-127] 链式口径总判据：%s" % ("✓ 全部达标" if all_ok else "✗ 有不达标项"))
    dump()
    del e, pool
    gc.collect()
    return EXIT_OK if all_ok else EXIT_BAD


def run_geometry(e, ee, pool, bl, A0, A1, B, H, W, timeout, fclk, blob_pa):
    """一个几何（H×W 特征图）的全部口径。"""
    log("")
    log("-" * 78)
    log("几何：特征图 %d×%d（npix=%d）" % (H, W, H * W))
    log("-" * 78)
    g = dict(H=H, W=W, npix=H * W, cases=[], all_ok=True)

    # ---------- 参照 ----------
    t0 = time.time()
    rng = np.random.default_rng(20261103 + H)
    LA = BE.BinLayer(A0, H, W)
    LB = BE.BinLayer(B, H, W)
    xA = rng.integers(-128, 128, (LA.cin, H, W), dtype=np.int16).astype(np.int8)
    actA, refA_log, refA_c8, PA = golden_A(bl, A0, H, W, xA)
    fs_in_B = int(LB.d["act_fs_in"])
    fs_out_A = int(LA.d["act_fs_out"])
    xq, d, xq_same, xq_f = handoff_xq(refA_log, fs_out_A, fs_in_B)
    actB_ref, refB_log, refB_c8, PB = golden_B(bl, B, H, W, xq)
    dt_ref = time.time() - t0
    log("  参照：A %d×%d→%d×%d  B %d×%d→%d×%d  交接 d=fs_in_B(%d)-fs_out_A(%d)=%d"
        " （int/float 两路一致=%s）  用时 %.2f s"
        % (LA.cin, LA.H, LA.cout, LA.Hout, LB.cin, LB.H, LB.cout, LB.Hout,
           fs_in_B, fs_out_A, d, xq_same, dt_ref))
    log("    refA_c8 sha1=%s  actB_ref sha1=%s  refB_c8 sha1=%s"
        % (sha1_of(refA_c8)[:16], sha1_of(actB_ref)[:16], sha1_of(refB_c8)[:16]))
    g["ref"] = dict(seed=int(20261103 + H), xA_sha1=sha1_of(xA),
                    refA_c8=sha1_of(refA_c8), actB_ref=sha1_of(actB_ref),
                    refB_c8=sha1_of(refB_c8), d=int(d),
                    fs_in_B=fs_in_B, fs_out_A=fs_out_A, xq_int_eq_float=bool(xq_same),
                    seconds=round(dt_ref, 3))
    needA = LA.out_bytes
    needB = LB.out_bytes
    need_inB = LB.act_bytes
    log("    needA=%d B  needB=%d B  B 的 act=%d B" % (needA, needB, need_inB))

    save("g%d_refA_c8.bin" % H, refA_c8)
    save("g%d_refB_c8.bin" % H, refB_c8)
    save("g%d_actB_ref.bin" % H, actB_ref)
    save("g%d_xq_from_refA.bin" % H, xq)

    # ---------- 缓冲 ----------
    gact, act_pa = pool.region(len(BR.act_bytes(actA)) + 64, name="act")
    ginB, inB_pa = pool.region(need_inB + 64, name="inB")
    goutA, outA_pa = pool.region(needA + PAD, name="outA")
    goutB, outB_pa = pool.region(needB + PAD, name="outB")
    gsrcA, srcA_pa = pool.region(len(refA_c8) + PAD, name="srcA")
    for b, nm in ((gact, "gact"), (ginB, "ginB"), (goutA, "goutA"),
                  (goutB, "goutB"), (gsrcA, "gsrcA")):
        e.add_region(b, nm)
    u8(gact)[:len(BR.act_bytes(actA))] = np.frombuffer(BR.act_bytes(actA), np.uint8)
    gact.flush()
    u8(gsrcA)[:len(refA_c8)] = np.frombuffer(refA_c8, np.uint8)
    gsrcA.flush()
    log("  缓冲：act@%#010x(%d) inB@%#010x(%d) outA@%#010x(%d) outB@%#010x(%d)"
        " srcA@%#010x"
        % (act_pa, len(gact), inB_pa, len(ginB), outA_pa, len(goutA),
           outB_pa, len(goutB), srcA_pa))

    def push_act(actbytes, b=ginB):
        e.push_input(actbytes, b)
        return read_region(b, len(actbytes))

    def rec_case(key, kind, gotB, oobB, act_got=None, extra=None):
        cm = cmp_bytes(gotB, refB_c8)
        c = dict(key=key, kind=kind, outB=cm, oobB=int(oobB),
                 sha1_B=sha1_of(gotB))
        if act_got is not None:
            c["act"] = cmp_bytes(act_got, actB_ref)
        if extra:
            c.update(extra)
        g["cases"].append(c)
        log("   ---- %-26s %s" % (key, kind))
        log("        B 输出：不符 %6d/%d  越界 %4d B  首错字节 %s  连续尾巴=%s"
            % (cm["nbad"], cm["n"], c["oobB"], cm["first"], cm["tail_all_wrong"]))
        if act_got is not None:
            ca = c["act"]
            log("        交接(B 的 IN)：不符 %6d/%d  首错字节 %s  连续尾巴=%s"
                % (ca["nbad"], ca["n"], ca["first"], ca["tail_all_wrong"]))
        return c

    # ---------- ① A 单层（PS 喂输入）----------
    log("")
    log("  ① A 单层（PS 喂输入）")
    ra = run_conv(e, gact, goutA, LA, needA, timeout, blob_pa)
    cmA = cmp_bytes(ra["got"], refA_c8)
    log("     A：不符 %6d/%d  越界 %4d B  sha1=%s  连续尾巴=%s"
        % (cmA["nbad"], cmA["n"], ra["oob"], ra["sha1"][:12], cmA["tail_all_wrong"]))
    g["A_single"] = dict(out=cmA, oob=int(ra["oob"]), wall=ra["wall"],
                         cyc=ra["cyc"], st310=ra["st310"], sha1=ra["sha1"])
    save("g%d_A_single_out.bin" % H, ra["got"])
    A_ok = (cmA["nbad"] == 0 and ra["oob"] == 0)

    # ---------- ③ CTL1：B 的输入由 PS/numpy 直喂（不经 A）----------
    log("")
    log("  ③ CTL1：B 的输入由 PS/numpy 直喂（不经 A）")
    push_act(actB_ref)
    act_got = read_region(ginB, need_inB)
    rb_ = run_conv(e, ginB, goutB, LB, needB, timeout, blob_pa)
    c_ctl1 = rec_case("CTL1_PSffed", "B 的输入由 PS 直喂（不经 A）；判据：应全对",
                      rb_["got"], rb_["oob"], act_got)
    save("g%d_CTL1_outB.bin" % H, rb_["got"])
    save("g%d_CTL1_act_got.bin" % H, act_got)
    ctl1_ok = (c_ctl1["outB"]["nbad"] == 0 and rb_["oob"] == 0
               and c_ctl1["act"]["nbad"] == 0)

    # ---------- ④ CTL2：E1E2 从 PS 写进 DDR 的 A 参照 C8 打包 ----------
    c_ctl2 = None
    ctl2_ok = None
    if ee is not None:
        log("")
        log("  ④ CTL2：E1E2 从 **PS 写入 DDR 的** refA_c8 打包 → B（隔离「源缓冲是谁写的」）")
        try:
            act_got2 = pack_from_pa(ee, srcA_pa, LA.cout, H, W, fs_out_A, LB,
                                    PB["t_q"], out_buf=None)
            if act_got2 is None:
                log("     ⚠ pack_segments 返回 None（E1E2 拒绝）⇒ 跳过")
                c_ctl2 = dict(key="CTL2_PSsrc", skipped="pack None")
            else:
                push_act(act_got2)
                act_got2 = read_region(ginB, need_inB)
                rb2 = run_conv(e, ginB, goutB, LB, needB, timeout, blob_pa)
                c_ctl2 = rec_case("CTL2_PSsrc",
                                  "E1E2 从 PS 写的 refA_c8 打包；判据：应全对",
                                  rb2["got"], rb2["oob"], act_got2)
                save("g%d_CTL2_outB.bin" % H, rb2["got"])
                save("g%d_CTL2_act_got.bin" % H, act_got2)
                ctl2_ok = (c_ctl2["outB"]["nbad"] == 0 and rb2["oob"] == 0
                           and c_ctl2["act"]["nbad"] == 0)
        except Exception as ex:                                  # noqa: BLE001
            traceback.print_exc()
            log("     ✗ CTL2 失败：%r" % (ex,))
            c_ctl2 = dict(key="CTL2_PSsrc", error=repr(ex)[:300])
    g["CTL1"] = c_ctl1
    g["CTL2"] = c_ctl2

    # ---------- ② CHAIN（两次）----------
    chain_runs = []
    for it in (1, 2):
        log("")
        log("  ② CHAIN 第 %d 次：A → 交接 → B" % it)
        # A 重跑（每次链都用新鲜的 A 输出）
        ra = run_conv(e, gact, goutA, LA, needA, timeout, blob_pa)
        cmA_i = cmp_bytes(ra["got"], refA_c8)
        log("     A：不符 %6d/%d 越界 %4d B sha1=%s"
            % (cmA_i["nbad"], cmA_i["n"], ra["oob"], ra["sha1"][:12]))
        hA = e.read_out(goutA, LA, lazy=True)
        paA = int(getattr(hA, "pa", 0) or 0)
        rec = dict(it=it, A=dict(out=cmA_i, oob=int(ra["oob"]), sha1=ra["sha1"],
                                 cyc=ra["cyc"], st310=ra["st310"], pa="%#010x" % paA))
        save("g%d_chain%d_outA.bin" % (H, it), ra["got"])

        # --- ②a CHAIN_D：E1E2 直接从 A 的 OUT 槽打包 → 直写 B 的 IN 槽 ---
        if ee is not None and paA:
            try:
                # B 的 IN 槽尾巴先打金丝雀（E1E2 直写越界会踩掉）
                u8(ginB)[need_inB:] = MARK
                ginB.flush()
                act_got = pack_from_pa(ee, paA, LA.cout, H, W, fs_out_A, LB,
                                       PB["t_q"], out_buf=ginB)
                if act_got is None:
                    log("     ⚠ CHAIN_D pack_segments=None ⇒ 跳过")
                    rec["D"] = dict(skipped="pack None")
                else:
                    oob_act, _of = check_oob(ginB, need_inB)
                    act_got = read_region(ginB, need_inB)
                    rbd = run_conv(e, ginB, goutB, LB, needB, timeout, blob_pa)
                    cd = rec_case("CHAIN_D_it%d" % it,
                                  "E1E2 直读 A 的 OUT 槽（PS 不碰激活）",
                                  rbd["got"], rbd["oob"], act_got)
                    cd["oob_act_B_in"] = int(oob_act)
                    rec["D"] = cd
                    backA = read_region(goutA, needA)
                    cd["A_slot_intact"] = bool(sha1_of(backA) == ra["sha1"])
                    save("g%d_chain%d_D_outB.bin" % (H, it), rbd["got"])
                    save("g%d_chain%d_D_act_got.bin" % (H, it), act_got)
            except Exception as ex:                              # noqa: BLE001
                traceback.print_exc()
                log("     ✗ CHAIN_D 失败：%r" % (ex,))
                rec["D"] = dict(error=repr(ex)[:300])

        # --- ②b CHAIN_P：整网实际跑点（read_out→PS 量化→E1E2→push_input）---
        try:
            xfA = e.read_out(goutA, LA, lazy=False)          # A 的实际输出反量化
            xqA = BE.quantize_int8(xfA, fs_in_B)
            rec["A_f32_xq_sha1"] = sha1_of(xqA)
            data = None
            if ee is not None:
                data = ee.pack_int8(xqA, PB["t_q"], H, W)
            if data is None:
                data = BR.act_bytes(BR.binarize_pack(xqA, PB["t_q"]))
                log("     （E1E2 不可用 ⇒ CHAIN_P 用 PS binarize_pack）")
            push_act(data)
            act_got = read_region(ginB, need_inB)
            rbp = run_conv(e, ginB, goutB, LB, needB, timeout, blob_pa)
            cp = rec_case("CHAIN_P_it%d" % it,
                          "read_out→PS 量化→E1E2→push_input（= 整网跑点）",
                          rbp["got"], rbp["oob"], act_got)
            rec["P"] = cp
            # A 的 OUT 槽被 E1E2 读过之后有没有被改动
            backA = read_region(goutA, needA)
            rec["A_slot_intact"] = bool(sha1_of(backA) == ra["sha1"])
            log("     A 的 OUT 槽被 E1E2 读过之后仍逐字节不变：%s"
                % rec["A_slot_intact"])
            save("g%d_chain%d_P_outB.bin" % (H, it), rbp["got"])
            save("g%d_chain%d_P_act_got.bin" % (H, it), act_got)
        except Exception as ex:                                  # noqa: BLE001
            traceback.print_exc()
            log("     ✗ CHAIN_P 失败：%r" % (ex,))
            rec["P"] = dict(error=repr(ex)[:300])
        chain_runs.append(rec)

    # ---------- 确定性 ----------
    def shas(path_):
        return [r[key]["sha1_B"] for r in chain_runs
                for key in (path_,) if isinstance(r.get(key), dict)
                and r[key].get("sha1_B")]

    log("")
    det = {}
    for tag in ("D", "P"):
        ss = shas(tag)
        det[tag] = dict(n=len(ss), distinct=len(set(ss)), shas=[s[:12] for s in ss],
                        deterministic=bool(len(ss) >= 2 and len(set(ss)) == 1))
        log("   确定性 %s：%d 次 → %d 个不同 sha1 %s ⇒ %s"
            % (tag, len(ss), len(set(ss)), det[tag]["shas"],
               "✓ 确定" if det[tag]["deterministic"] else
               ("（只有 1 次，无法判）" if len(ss) < 2 else "✗✗ 非确定")))
    g["chain"] = dict(runs=chain_runs, determinism=det)

    # ---------- 小结 ----------
    def okof(rec_, tag):
        v = rec_.get(tag) if isinstance(rec_, dict) else None
        if not isinstance(v, dict) or "outB" not in v:
            return None
        return bool(v["outB"]["nbad"] == 0 and v["oobB"] == 0
                    and (v.get("act") is None or v["act"]["nbad"] == 0))

    summary = dict(
        A_single_ok=bool(A_ok),
        CTL1_ok=(None if ctl1_ok is None else bool(ctl1_ok)),
        CTL2_ok=(None if ctl2_ok is None else bool(ctl2_ok)),
        CHAIN_D_ok=[okof(r, "D") for r in chain_runs],
        CHAIN_P_ok=[okof(r, "P") for r in chain_runs],
        det_D=det["D"]["deterministic"], det_P=det["P"]["deterministic"])
    g["summary"] = summary
    # 判据：A 单层对 + CTL1 对（基线成立）；链式达标 = 每轮 D/P 的 out 与 act 都 0
    base_ok = bool(A_ok and ctl1_ok)
    chain_ok = all(v is True for v in summary["CHAIN_D_ok"] + summary["CHAIN_P_ok"]) \
        if summary["CHAIN_D_ok"] + summary["CHAIN_P_ok"] else False
    g["all_ok"] = bool(base_ok and chain_ok)
    log("")
    log("  [%d×%d] 小结：A 单层 %s | CTL1 %s | CTL2 %s | CHAIN_D %s | CHAIN_P %s"
        % (H, W, "✓" if A_ok else "✗", summary["CTL1_ok"], summary["CTL2_ok"],
           summary["CHAIN_D_ok"], summary["CHAIN_P_ok"]))
    log("  [%d×%d] 链式全对 = %s（判据：A 单层对 + CTL1 对 + 每轮链式 out/act 0 错）"
        % (H, W, "✓" if g["all_ok"] else "✗✗"))
    return g


if __name__ == "__main__":
    OUTDIR = "/home/xilinx/demoir/t127_out"
    try:
        sys.exit(main())
    except Exception:                                            # noqa: BLE001
        traceback.print_exc()
        try:
            R["fatal"] = traceback.format_exc()[-2000:]
            dump()
        except Exception:                                        # noqa: BLE001
            pass
        sys.exit(EXIT_ERR)
