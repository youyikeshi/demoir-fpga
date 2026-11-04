#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe12.py —— 板端 p0067 逐层 sha1 探针（T-166，只读、不改生产代码）。

目的：把 `runs/opt_round1/ps_numeric/p0067_divergence/BOARD_REQUEST.md` §3 的 12 项
      （1 个 f32 + 11 个 int8 conv 层）在 p0067 **一次前向**里取出来只算 sha1；
      同时顺手把 **全 227 个 int8 conv 层** 与 **4 个 f32 层** 也各取一个 sha1，
      与主机 `host/layers_index.json` 的 227 个 sha1 逐层比（同一份容器、同一张图、
      同一条流水），这样"第一个发散层"就是**精确层号**而不是 12 项的区间上界。

数值路径零改动：只 monkeypatch 两个 **只读** 钩子
  · `bin_net._PlBackend.finish`（板端 conv 反量化后的 f32）→ `rint(y*2^fs_out)` 精确还原 int8
  · `bin_net.BinNet._fp_conv`（4 个 f32 卷积）
返回的 y / 张量一个字节都不改。

sha1 定义与主机**逐字相同**：`sha1(np.ascontiguousarray(a).tobytes())`
  · int8 层 == (C,H,W) C 序 int8（= conv 的 sat8 输出，反量化前语义）
  · f32 层 == (C,H,W) C 序 float32（= `_fp_conv` 的返回值，**含 relu**，与主机 fpdump 同一点）

用法（板端）见 run_probe.sh。
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

#: BOARD_REQUEST §3 的 11 个 int8 层（按 idx；名字在 host_index.json 里是权威）
WANT_I8_IDX = [0, 1, 2, 3, 5, 6, 37, 75, 113, 189, 226]
#: BOARD_REQUEST §3 的 1 个 f32 层
WANT_FP = ["encoder.conv_first.0"]

#: 已知的板端 out1 记账值（runs/opt_round1/perf/t165/board_out/，用来确认 runpoint 一致）
BOARD_OUT1_F32_SHA1 = "85d5e42bc9275c9ff77effc2038d3cf204991af4"
BOARD_OUT1_U8_HWC_SHA1 = "d709f02c0b1a09795daf967a75993fe428c87542"


def sha1b(a) -> str:
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def md5f(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _slcr_clk(off: int):
    """读 SLCR 时钟寄存器，返回 (raw, 真值 MHz)。只读。"""
    try:
        import ctypes
        import mmap
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        try:
            m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED,
                          mmap.PROT_READ | mmap.PROT_WRITE, offset=0xF8000000)
            raw = ctypes.c_uint32.from_buffer(m, off).value
            m.close()
        finally:
            os.close(fd)
        return raw, (3200.0 / 3.0) / (((raw >> 8) & 0x3F) + 1)
    except Exception:                                   # noqa: BLE001
        return None, None


def fpga_state() -> str:
    try:
        return open("/sys/class/fpga_manager/fpga0/state").read().strip()
    except OSError:
        return "?"


def meminfo():
    out = {}
    try:
        for ln in open("/proc/meminfo"):
            if ln.startswith(("MemAvailable:", "CmaFree:")):
                k, v = ln.split(":")[0], int(ln.split()[1])
                out[k] = round(v / 1024.0, 1)
    except OSError:
        pass
    return out


def u8_of(x):
    """与 board_verify_numfix.u8() / 主机 u8() 逐字相同的显示映射。"""
    return np.rint(np.clip(np.asarray(x, np.float64), 0.0, 1.0) * 255.0).astype(np.uint8)


# ===========================================================================
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="/home/xilinx/demo/frozen256/p0067_crop256.npy")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/v60/pack_clean")
    ap.add_argument("--bit", default="/home/xilinx/demoir/deploy/v60/design_1_v60.bit")
    ap.add_argument("--index", default=os.path.join(HERE, "host_index.json"))
    ap.add_argument("--outdir", default=os.path.join(HERE, "out"))
    ap.add_argument("--fclk0", type=float, default=75.0)
    ap.add_argument("--fclk1", type=float, default=150.0)
    ap.add_argument("--nosave", action="store_true", help="不落 npy（默认落 12 项 + 首个发散层）")
    a = ap.parse_args(argv)
    os.makedirs(a.outdir, exist_ok=True)
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                            # noqa: BLE001
            pass

    host = json.load(open(a.index))

    # ---------------------------------------------------------------- 前置
    print("=" * 100, flush=True)
    print("[pre] fpga_manager state = %s" % fpga_state(), flush=True)
    print("[pre] cwd=%s\n[pre] argv=%s" % (os.getcwd(), " ".join(sys.argv)), flush=True)
    keys = ("BIN_PACK", "BIN_RTL_ROUND", "BIN_CONV_OOB0", "BIN_THR_F32", "BIN_LAZY_DEQ",
            "BIN_CAT_FUSION", "BIN_BILINEAR_C", "BIN_PRECOMP", "Q8CACHE", "BIN_F32DIRECT",
            "BIN_CAT_ECA_FUSE", "BIN_EPI_NOINV", "BIN_LAZY_SLOTS", "BV_FREE", "PYTHONPATH")
    for k in keys:
        print("[env] %-16s = %s" % (k, os.environ.get(k, "<unset>")), flush=True)
    print("[pre] python=%s numpy=%s platform=%s"
          % (sys.version.split()[0], np.__version__, os.uname().machine), flush=True)
    print("[pre] mem %s" % (meminfo(),), flush=True)

    import bin_engine as BE
    import bin_net as BN
    print("[pre] bin_net=%s (%s)" % (BN.__file__, md5f(BN.__file__)), flush=True)
    print("[pre] bin_engine=%s" % (BE.__file__,), flush=True)

    # ---------------------------------------------------------------- 输入
    xi = np.load(a.input)
    print("[in] %s md5=%s shape=%s dtype=%s min=%d max=%d mean=%.3f"
          % (a.input, md5f(a.input), xi.shape, xi.dtype,
             int(xi.min()), int(xi.max()), float(xi.mean())), flush=True)
    x = np.asarray(xi, np.float64) / 255.0
    x = np.ascontiguousarray(x.transpose(2, 0, 1)[:3], np.float32)
    in_sha1 = sha1b(x)
    print("[in] f32 (3,256,256) CHW sha1=%s  (期望 c13eb436ed6f4709f16f4422b86aca6f4f4dec7a)"
          % in_sha1, flush=True)

    # ---------------------------------------------------------------- 建网（run100 同款）
    t0 = time.time()
    BE.load_kernels()
    n0 = BN.BinNet(a.pack, engine=None, H=256, W=256, verbose=False)
    g0, _o0, _t0 = n0.build_graph(np.zeros((3, 256, 256), np.float32))
    conv_ops = [o for o in g0.ops if o.kind == "conv"]
    need = (max(o.layer.act_bytes for o in conv_ops)
            + max(o.layer.out_bytes for o in conv_ops) + 4096) * 2 + (1 << 16)
    del n0, g0, _o0, _t0
    gc.collect()
    print("[net] 227-conv 图已建：conv=%d, arena need=%.2f MB"
          % (len(conv_ops), need / 1e6), flush=True)

    arena = BE.BinArena(need, name="probe_arena")

    from pynq import Overlay
    ov = Overlay(a.bit, download=False)          # ★ PL 必须已 operating，绝不重载
    print("[net] Overlay(download=False) ok", flush=True)

    from pynq import Clocks
    cur0 = float(Clocks.fclk0_mhz)
    print("[clk] FCLK0 报数(设前) = %g" % cur0, flush=True)
    if a.fclk0:
        Clocks.fclk0_mhz = float(a.fclk0)
    if a.fclk1:
        try:
            Clocks.fclk1_mhz = float(a.fclk1)
        except Exception as ex:                      # noqa: BLE001
            print("[clk] FCLK1 设置失败：%r" % (ex,), flush=True)
    time.sleep(0.4)
    print("[clk] 报数(设后) FCLK0=%g FCLK1=%g"
          % (float(Clocks.fclk0_mhz), float(Clocks.fclk1_mhz)), flush=True)
    r0, m0 = _slcr_clk(0x170)
    r1, m1 = _slcr_clk(0x174)
    print("[clk] SLCR 0xF8000170=%s ⇒ FCLK0 真值 %s MHz | 0xF8000174=%s ⇒ FCLK1 真值 %s MHz"
          % ("0x%08X" % r0 if r0 is not None else "N/A",
             "%.2f" % m0 if m0 is not None else "N/A",
             "0x%08X" % r1 if r1 is not None else "N/A",
             "%.2f" % m1 if m1 is not None else "N/A"), flush=True)
    print("[clk] 期望：FCLK0 报数 75 ⇒ 真值 50.79；FCLK1 报数 150 ⇒ 真值 96.97", flush=True)

    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=a.pack, verbose=False)
    eng.load_weights()
    net = BN.BinNet(a.pack, engine=eng, H=256, W=256, verbose=False)
    print("[net] load_weights ok；engine=%s" % (type(eng).__name__,), flush=True)
    print("[H3] engine._e1e2 = %r ; net._lazy_deq = %r ; net._cat_fusion = %r"
          % (getattr(eng, "_e1e2", None), net._lazy_deq,
             getattr(net, "_cat_fusion", None)), flush=True)

    # ---------------------------------------------------------------- 只读钩子
    cap_i8, cap_fp = {}, {}

    _o_fin = BN._PlBackend.finish

    def finish(self, task):
        y, rs, te = _o_fin(self, task)
        L = task.layer
        try:
            fs = int(L.d["act_fs_out"])
        except Exception:                            # noqa: BLE001
            fs = None
        if fs is not None:
            arr = y
            if not isinstance(arr, np.ndarray):
                arr = arr.materialize() if hasattr(arr, "materialize") else np.asarray(arr)
            q = np.rint(np.asarray(arr, np.float64) * (2.0 ** fs)).astype(np.int8)
            if q.shape != (L.cout, L.Hout, L.Wout):
                print("  ⚠ %s 形状 %s != (%d,%d,%d)"
                      % (L.name, q.shape, L.cout, L.Hout, L.Wout), flush=True)
            cap_i8[L.name] = (int(L.idx), np.ascontiguousarray(q), fs)
        return y, rs, te

    _o_fp = BN.BinNet._fp_conv

    def fp_conv(self, x_, name, relu=False, stride=1, pad=0):
        y = _o_fp(self, x_, name, relu=relu, stride=stride, pad=pad)
        if name in host["fp"] or name in WANT_FP:
            cap_fp[name] = np.ascontiguousarray(np.asarray(y, np.float32))
        return y

    BN._PlBackend.finish = finish
    BN.BinNet._fp_conv = fp_conv

    # ---------------------------------------------------------------- 一次前向
    print("=" * 100, flush=True)
    t_run = time.time()
    try:
        outs, st = net.run(x, pipeline=True, nbuf=2, arena=arena)
    finally:
        BN._PlBackend.finish = _o_fin
        BN.BinNet._fp_conv = _o_fp
    dt = time.time() - t_run
    print("[run] 整网一次前向 %.2f s（建网 %.1f s）；捕获 int8 层 %d / f32 层 %d"
          % (dt, t_run - t0, len(cap_i8), len(cap_fp)), flush=True)
    print("[run] stats: deps_closure_ok=%s kernel_deps=%s"
          % (st.get("deps_closure_ok"), json.dumps(st.get("kernel_deps"))[:200]),
          flush=True)
    print("[run] stats: total=%.2f s pl=%.2f ps=%.2f"
          % (float(st.get("total", -1)), float(st.get("pl", -1)),
             float(st.get("ps", -1))), flush=True)

    # ---------------------------------------------------------------- out1 记账
    o1 = np.asarray(outs["out1"], np.float32)
    u8 = u8_of(o1)
    o1_sha = sha1b(o1)
    u8_chw_sha = sha1b(u8)
    u8_hwc_sha = sha1b(np.ascontiguousarray(u8.transpose(1, 2, 0)))
    print("=" * 100, flush=True)
    print("[out1] f32 (3,256,256) sha1 = %s   %s"
          % (o1_sha, "== 板端记账" if o1_sha == BOARD_OUT1_F32_SHA1 else "!= 板端记账 %s" % BOARD_OUT1_F32_SHA1),
          flush=True)
    print("[out1] u8  HWC sha1 = %s   %s"
          % (u8_hwc_sha, "== 板端记账" if u8_hwc_sha == BOARD_OUT1_U8_HWC_SHA1
             else "!= 板端记账 %s" % BOARD_OUT1_U8_HWC_SHA1), flush=True)
    print("[out1] u8  CHW sha1 = %s ; min=%d max=%d mean=%.3f"
          % (u8_chw_sha, int(u8.min()), int(u8.max()), float(u8.mean())), flush=True)

    # ---------------------------------------------------------------- ① 12 项对照
    by_idx = {v[0]: (k, v[1], v[2]) for k, v in cap_i8.items()}
    rows12 = []
    print("=" * 100, flush=True)
    print("%-3s %-5s %-62s %-14s %-14s %s"
          % ("#", "类型", "层名", "期望 sha1", "实测 sha1", "一致"), flush=True)

    # #1 f32（卷积 conv_first，含 relu，= 主机 fpdump 同一点）
    nm = WANT_FP[0]
    exp = host["fp"].get(nm, {}).get("sha1", "")
    if nm in cap_fp:
        y = cap_fp[nm]
        got, ok = sha1b(y), (sha1b(y) == exp)
        rows12.append(dict(n=1, kind="f32", idx=None, name=nm, fs_out=None,
                           shape=list(y.shape), dtype=str(y.dtype),
                           bytes=int(y.nbytes), sha1=got, exp=exp, ok=ok,
                           stats=dict(min=float(y.min()), max=float(y.max()),
                                      mean=float(y.mean()), std=float(y.std()))))
        print("%-3d %-5s %-62s %-14s %-14s %s"
              % (1, "f32", nm, exp[:12], got[:12], "✓" if ok else "✗"), flush=True)
    else:
        rows12.append(dict(n=1, kind="f32", name=nm, exp=exp, ok=False,
                           note="MISSING"))
        print("%-3d %-5s %-62s %-14s %-14s %s"
              % (1, "f32", nm, exp[:12], "MISSING", "✗"), flush=True)

    # #2..#12 十一个 int8 conv 层（按 idx 取，名字以 host_index 为权威）
    idx2name = {v["i"]: k for k, v in host["i8"].items()}
    for n, idx in enumerate(WANT_I8_IDX, start=2):
        if idx not in by_idx:
            nmx = idx2name.get(idx, "?")
            rows12.append(dict(n=n, kind="int8", idx=idx, name=nmx, ok=False,
                               note="MISSING", exp=host["i8"].get(nmx, {}).get("sha1", "")))
            print("%-3d %-5s %-62s %-14s %-14s %s"
                  % (n, "int8", nmx, host["i8"].get(nmx, {}).get("sha1", "")[:12],
                     "MISSING", "✗"), flush=True)
            continue
        nmx, q, fs = by_idx[idx]
        got = sha1b(q)
        exp = host["i8"].get(nmx, {}).get("sha1", "")
        ok = (got == exp)
        rows12.append(dict(n=n, kind="int8", idx=idx, name=nmx, fs_out=fs,
                           shape=list(q.shape), dtype=str(q.dtype),
                           bytes=int(q.nbytes), sha1=got, exp=exp, ok=ok,
                           stats=dict(min=int(q.min()), max=int(q.max()),
                                      mean=float(q.mean()),
                                      n_zero=int((q == 0).sum()),
                                      n_lo=int((q == -128).sum()),
                                      n_hi=int((q == 127).sum()))))
        print("%-3d %-5s %-62s %-14s %-14s %s"
              % (n, "int8", nmx, exp[:12], got[:12], "✓" if ok else "✗"), flush=True)

    # ---------------------------------------------------------------- ② 全 227 层扫
    print("=" * 100, flush=True)
    miss = [k for k in host["i8"] if k not in cap_i8]
    extra = [k for k in cap_i8 if k not in host["i8"]]
    print("[scan] 主机 227 层中未捕获 %d 个%s；板端多出 %d 个%s"
          % (len(miss), ("（%s）" % ", ".join(miss[:5])) if miss else "",
             len(extra), ("（%s）" % ", ".join(extra[:5])) if extra else ""), flush=True)
    diffs, same = [], 0
    for nm, (idx, q, fs) in sorted(cap_i8.items(), key=lambda kv: kv[1][0]):
        exp = host["i8"].get(nm, {}).get("sha1")
        if exp is None:
            continue
        got = sha1b(q)
        if got == exp:
            same += 1
        else:
            h = host["i8"][nm]["stats"]
            diffs.append(dict(idx=idx, name=nm, fs_out=fs, sha1=got, exp=exp,
                              shape=list(q.shape),
                              b=dict(min=int(q.min()), max=int(q.max()),
                                     mean=float(q.mean()),
                                     n_zero=int((q == 0).sum())),
                              h=dict(min=h["min"], max=h["max"], mean=h["mean"],
                                     n_zero=h["n_zero"])))
    print("[scan] 板端 vs 主机：相同 %d / 227，不同 %d" % (same, len(diffs)), flush=True)
    if diffs:
        d0 = diffs[0]
        print("[scan] ★ 第一个发散的 int8 层 = idx %d  %s（fs_out=%s）"
              % (d0["idx"], d0["name"], d0["fs_out"]), flush=True)
        prev = [k for k, v in cap_i8.items() if v[0] == d0["idx"] - 1]
        if prev:
            pn = prev[0]
            print("[scan]   它的前一层 idx %d %s：%s"
                  % (d0["idx"] - 1, pn,
                     "相同" if sha1b(cap_i8[pn][1]) == host["i8"][pn]["sha1"] else "也不同"),
                  flush=True)
        print("[scan] 全部不同的层（idx / 名字 / 期望→实测 / 统计 b vs h）：", flush=True)
        for d in diffs:
            print("   %4d %-58s %s→%s  fs=%s  n_zero %d vs %d, min %d vs %d, "
                  "max %d vs %d, mean %.3f vs %.3f"
                  % (d["idx"], d["name"][-58:], d["exp"][:12], d["sha1"][:12],
                     d["fs_out"], d["b"]["n_zero"], d["h"]["n_zero"],
                     d["b"]["min"], d["h"]["min"], d["b"]["max"], d["h"]["max"],
                     d["b"]["mean"], d["h"]["mean"]), flush=True)

    # ---------------------------------------------------------------- ③ 4 个 f32 层
    print("=" * 100, flush=True)
    fpd = []
    for nm, v in host["fp"].items():
        got = sha1b(cap_fp[nm]) if nm in cap_fp else "MISSING"
        ok = (got == v["sha1"])
        fpd.append(dict(name=nm, sha1=got, exp=v["sha1"], ok=ok))
        print("[fp] %-30s %s  exp=%s  %s" % (nm, got[:14], v["sha1"][:14],
                                             "✓" if ok else "✗"), flush=True)
    first_fp_diff = next((d for d in fpd if not d["ok"]), None)

    # ---------------------------------------------------------------- 落盘
    saved = []
    if not a.nosave:
        d = os.path.join(a.outdir, "layers_p0067")
        os.makedirs(d, exist_ok=True)
        want = set()
        for r in rows12:
            if r["kind"] == "int8" and "sha1" in r:
                want.add(r["name"])
        for r in rows12:
            if r["kind"] == "f32" and "sha1" in r:
                want.add(r["name"])
        if diffs:
            d0 = diffs[0]
            want.add(d0["name"])
            prev = [k for k, v in cap_i8.items() if v[0] == d0["idx"] - 1]
            want.update(prev)
            if int(d0["idx"]) - 2 in [v[0] for v in cap_i8.values()]:
                want.update([k for k, v in cap_i8.items() if v[0] == d0["idx"] - 2])
        if first_fp_diff and first_fp_diff["name"] in cap_fp:
            want.add(first_fp_diff["name"])
        for nm in sorted(want):
            if nm in cap_i8:
                arr = cap_i8[nm][1]
            elif nm in cap_fp:
                arr = cap_fp[nm]
            else:
                continue
            fn = nm.replace("/", "_") + ".npy"
            np.save(os.path.join(d, fn), arr)
            saved.append(dict(name=nm, file=fn, bytes=int(arr.nbytes),
                              md5=md5f(os.path.join(d, fn)), sha1=sha1b(arr),
                              shape=list(arr.shape), dtype=str(arr.dtype)))
        np.save(os.path.join(d, "_out1_f32.npy"), o1)
        np.save(os.path.join(d, "_out1_u8_chw.npy"), u8)
        print("[save] %d 个 npy → %s（%.2f MB）"
              % (len(saved) + 2, d, (sum(s["bytes"] for s in saved)
                                     + o1.nbytes + u8.nbytes) / 1e6), flush=True)

    summ = dict(tag="p0067", when=time.strftime("%Y-%m-%dT%H:%M:%S"),
                input=dict(path=a.input, md5=md5f(a.input), in_sha1=in_sha1),
                env={k: os.environ.get(k) for k in keys},
                clocks=dict(fclk0_raw=("0x%08X" % r0) if r0 is not None else None,
                            fclk0_true_mhz=m0,
                            fclk1_raw=("0x%08X" % r1) if r1 is not None else None,
                            fclk1_true_mhz=m1),
                deps_closure_ok=st.get("deps_closure_ok"),
                kernel_deps=st.get("kernel_deps"),
                h3=dict(e1e2=repr(getattr(eng, "_e1e2", None)),
                        lazy_deq=bool(net._lazy_deq),
                        cat_fusion=bool(getattr(net, "_cat_fusion", False))),
                run_s=round(dt, 2),
                out1=dict(sha1_f32=o1_sha, sha1_u8_chw=u8_chw_sha,
                          sha1_u8_hwc=u8_hwc_sha,
                          f32_matches_board_record=(o1_sha == BOARD_OUT1_F32_SHA1),
                          u8_hwc_matches_board_record=(u8_hwc_sha == BOARD_OUT1_U8_HWC_SHA1)),
                items12=rows12, i8_n_same=same, i8_diffs=diffs, fp=fpd,
                saved=saved)
    json.dump(summ, open(os.path.join(a.outdir, "summary.json"), "w"), indent=1)
    print("=" * 100, flush=True)
    n12ok = sum(1 for r in rows12 if r.get("ok"))
    print("[SUMMARY] 12 项里一致 %d/12；227 个 int8 层一致 %d；f32 层一致 %d/4"
          % (n12ok, same, sum(1 for d in fpd if d["ok"])), flush=True)
    print("[SUMMARY] 第一个发散的 int8 层：%s"
          % (("idx %d %s" % (diffs[0]["idx"], diffs[0]["name"])) if diffs else "无（227 层全同）"),
          flush=True)
    print("[SUMMARY] summary.json → %s" % os.path.join(a.outdir, "summary.json"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
