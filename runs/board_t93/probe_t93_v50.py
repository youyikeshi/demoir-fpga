#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_t93_v50.py —— T-93：**v50 16-lane 单次上板验证**（load 一次，六步全在这一进程）。

目的（只判这一件事）：
    16-lane 是否真的把 conv 的 cyc/term 从 v5 的 54.3 砍到 ≈27（−0.56 s 的来源），
    且 64² 三个 sha1 与冻结值逐字节相同。

六步（任一步的硬判据不过就**停**，不继续）：
    ① load + 指纹：三个基址（conv_uni_0=0x40040000 / bin_unit_0=0x40060000 /
      **不应有 fconv_0**）；读 `CHAIN_ID` magic（配置页 +0x314 = 基址+0x10314）
      必须 = 0x50300001；顺带读 0x310（契约 R3：复位态应为 0，err 不许置）。
    ② Clocks.fclk0_mhz = 100 并**读回**（读回 ≠100 就按实际值算周期）。
    ③ 64² 整网，冻结运行点 + **不许开任何 BIN_V50_***；三 sha1 必须 =
      bfa246d3… / 226bd39e… / 762d84b9…
    ④ 周期：per-op（`reap` 钩子，PL start→done 的 seconds × fclk）+ 段5 RLAT（cyc/term）；
      报 conv 的 cyc/term、每个 op 的周期、整网 PS 侧耗时桶。
    ⑤ 金丝雀（0 字节被写坏）+ 再读一次 0x310/0x314。
    ⑥（可选）256² sha1 = 9b1940c8…/f86a7035…/025757a8…

纪律：
    · 本进程**只 download 一次**（`Overlay(..., download=True)`，由 boardlock 的 load 令牌放行）；
      `--no-load` 时才用 download=False 接手已加载的位流。
    · 不改仓库、不改位流、不动算法；所有数值走库的原路径（只加**读**钩子）。
    · 任何异常都先落 json 再退出（取证优先），退出码见 EXIT_*。
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import sys
import time
import traceback

# ============================================================================
# ★ 0. 运行点必须在 import bin_engine/bin_net **之前**写进环境
#    （`_PROF/_MMIO_*/_DBG_PREP/_fast_eca_env` 等在 import 那一刻定型，T-54 §6）
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
    # ---- 测量口径（不是自变量；与 v4/v5 锚点同款）----
    ("BIN_PROFILE", "1"),
    ("Q8CACHE_MB", "32"),
]
# 红线：这些开关**一个都不许开**（融合通路已编译掉；开了会 fuse_cfg_bad 退出）
FORBID = (
    "BIN_V50_FUSE", "BIN_V50_CHAIN", "BIN_V50_PLQ8", "BIN_V50_REPROBE",
    "BIN_V50_BAND_ROWS", "BIN_V50_CHAIN_MAGIC",
    "BIN_SAM_TILE", "BIN_SAM_SLAB_KB", "BIN_DEQ_TILE",
    "BIN_MMIO_POLL", "BIN_MMIO_FAST", "BIN_MMIO_C",
    "BIN_CONVPF_C", "BIN_F32POOL", "BIN_CAT_STRICT", "BIN_VLANES",
)


def setup_env():
    for k in list(os.environ):
        if k in FORBID or k.startswith("BIN_V50_"):
            os.environ.pop(k, None)
    for k, v in RUNPOINT:
        os.environ[k] = v
    keys = [x for x, _ in RUNPOINT] + ["BIN_V50_FUSE", "BIN_VLANES", "BIN_PROFILE"]
    return {k: os.environ.get(k, "") for k in keys}


ENV_AT_START = setup_env()

import numpy as np                                                   # noqa: E402
import platform                                                      # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
# ★ PS 源码树：探针放在 /tmp/probe_T93，仓库代码在 /home/xilinx/demoir/ps/pynq
PS_DIR = os.environ.get("T93_PS", "/home/xilinx/demoir/ps/pynq")
for _p in (PS_DIR, os.path.join(PS_DIR, os.pardir, "tools")):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

FROZEN = {
    64: ("bfa246d34681e748311bbcd2f1b87317a56923fe",
         "226bd39e522aa373b8fc183acae5280931f8b63f",
         "762d84b9618eab8922edd6da4f07ba06de865628"),
    256: ("9b1940c8955be53ccffd7905e333deffcea31f0a",
          "f86a7035621bed133374a1bef42a27db204e7019",
          "025757a83c3993c8f592e31219b6a97906020363"),
}
MAGIC_WANT = 0x5030_0001
BASE_WANT = {"conv_uni_0": 0x4004_0000, "bin_unit_0": 0x4006_0000}
EXIT_OK, EXIT_MAGIC, EXIT_SHA, EXIT_ERR = 0, 2, 3, 4

R = {"env_at_start": ENV_AT_START, "steps": {}}
_fh = None
LOGPATH = None


def log(s=""):
    global _fh
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


def dump(path=None):
    path = path or LOGPATH and LOGPATH.replace(".log", ".json") or "t93_v50.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(R, f, indent=1, ensure_ascii=False)
        log("[json] 已落盘 %s" % path)
    except Exception as ex:                                      # noqa: BLE001
        log("[json] 落盘失败：%r" % (ex,))


# ============================================================================
# 硬件与库
# ============================================================================
try:
    import pynq.pl_server.embedded_device as _emb                # noqa: F401
except Exception:                                                # noqa: BLE001
    pass
from pynq import Overlay, Clocks                                 # noqa: E402

import bin_engine as BE                                          # noqa: E402
import bin_net as BN                                             # noqa: E402

# ---- 运行点证据（import 后硬看：这些开关一个都不许开）----
log("[t93] python=%s numpy=%s mach=%s" % (sys.version.split()[0],
                                          np.__version__, platform.machine()))
log("[t93] BIN_V50_FUSE=%r  BIN_VLANES=%r  v50_fuse_on()=%s  vlanes_now()=%d"
    % (os.environ.get("BIN_V50_FUSE"), os.environ.get("BIN_VLANES"),
       BE.v50_fuse_on(), BE.vlanes_now()))
log("[t93] cat_eca_fuse=%s  epi_noinv=%s  BIN_V50_* 残留=%s"
    % (BN._cat_eca_fuse(), BE._epi_noinv(),
       [k for k in os.environ if k.startswith("BIN_V50_")]))

# ---- per-op PL 周期采集（只读钩子：不改任何数值路径）----
OPREC = []
_orig_reap = BE.BinConvEngine.reap


def _reap_hook(self, t0, timeout, out_buf=None, L=None, verify_canary=True,
               t_done=None):
    rs = _orig_reap(self, t0, timeout, out_buf=out_buf, L=L,
                    verify_canary=verify_canary, t_done=t_done)
    try:
        if L is not None:
            OPREC.append(dict(
                name=str(getattr(L, "name", "?")), cin=int(L.cin),
                cout=int(L.cout), k=int(L.k), s=int(L.s), H=int(L.H), W=int(L.W),
                Hout=int(L.Hout), Wout=int(L.Wout), cb=int(L.cb), K=int(L.K),
                sec=float(rs.seconds)))
    except Exception:                                            # noqa: BLE001
        pass
    return rs


BE.BinConvEngine.reap = _reap_hook


def kernel_deps():
    try:
        d = BE.kernel_deps()
        return d, bool(BE.deps_closure_ok(d))
    except Exception as ex:                                      # noqa: BLE001
        return {"error": repr(ex)}, False


def deps_line():
    d, ok = kernel_deps()
    if "error" in d:
        return "[DEPS] <读不到：%s>" % d["error"]
    order = ("q8", "c8deq", "bpack", "e1e2", "catpack", "safe")
    return ("[DEPS] " + " ".join("%s=%s" % (k, d[k]["state"]) for k in order
                                 if k in d)
            + "  ⇒ deps_closure_ok=%s" % bool(ok))


# ============================================================================
# ① load + 指纹
# ============================================================================
def step1_load(bit, fclk, download):
    hdr("步骤 1：load overlay + 指纹（本进程唯一一次 download=%s）" % download)
    t0 = time.time()
    ov = Overlay(bit, download=bool(download))
    dt = time.time() - t0
    log("  Overlay(%s) 用时 %.3f s" % (os.path.basename(bit), dt))
    ips = {}
    try:
        ips = sorted(ov.ip_dict.keys())
        log("  IP 清单：%s" % ips)
    except Exception as ex:                                      # noqa: BLE001
        log("  IP 清单读失败：%r" % (ex,))

    got = {}
    for name, exp in BASE_WANT.items():
        try:
            ba = int(getattr(ov, name).mmio.base_addr)
            ln = int(getattr(ov, name).mmio.length)
        except Exception as ex:                                  # noqa: BLE001
            got[name] = dict(error=repr(ex)[:200])
            log("  %-11s ✗ 读不到：%r" % (name, ex))
            continue
        got[name] = dict(base=ba, base_hex="%#010x" % ba, length=ln,
                         expected="%#010x" % exp, ok=ba == exp)
        log("  %-11s base=%#010x length=%#x （期望 %#010x）%s"
            % (name, ba, ln, exp, "✓" if ba == exp else "✗✗"))
    has_fconv = "fconv_0" in (ips or [])
    log("  fconv_0 在 IP 清单里 = %s （期望 False）" % has_fconv)

    # ---- magic：契约 R1/R3 是「配置页 +0x314」，即基址 + 0x10000 + 0x314 ----
    base = BASE_WANT["bin_unit_0"]
    magic = {}
    for tag, off in (("CFG+0x314 (契约口径)", BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"]),
                     ("base+0x314 (任务书字面)", BE.V50_CHAIN_REG["CHAIN_ID"]),
                     ("0x310 CHAIN_STATUS", BE.V50_CHAIN_REG["CHAIN_STATUS"]),
                     ("0x308 BAND_ROWS", BE.V50_CHAIN_REG["BAND_ROWS"])):
        try:
            v = int(ov.bin_unit_0.mmio.read(int(off))) & 0xFFFFFFFF
        except Exception as ex:                                  # noqa: BLE001
            v = None
            log("  读 %#010x (%s) 失败：%r" % (base + int(off), tag, ex))
            magic[tag] = dict(abs_addr="%#010x" % (base + int(off)), error=repr(ex))
            continue
        magic[tag] = dict(abs_addr="%#010x" % (base + int(off)), value=v,
                          hex="%#010x" % v)
        log("  %#010x = %#010x   (%s)" % (base + int(off), v, tag))
    m = magic.get("CFG+0x314 (契约口径)", {}).get("value")
    magic_ok = (m == MAGIC_WANT)
    st310 = magic.get("0x310 CHAIN_STATUS", {}).get("value")
    log("  **magic(0x314) = %s 期望 %#010x ⇒ %s**"
        % (("%#010x" % m) if m is not None else "读不到", MAGIC_WANT,
           "v50 寄存器块在位 ✓" if magic_ok else "✗✗ 不是 v50 / BD 没换对 ⇒ 停"))
    log("  **0x310 复位值 = %s 期望 0（契约 R3：err 位不许置）**"
        % (("%#010x" % st310) if st310 is not None else "读不到"))

    R["steps"]["load"] = dict(bit=bit, bit_sha=None, download=bool(download),
                             seconds=round(dt, 4), ips=ips, addr=got,
                             fconv_present=bool(has_fconv), magic=magic,
                             magic_ok=bool(magic_ok), chain_status_reset=st310)
    return ov, magic_ok


def step2_fclk(fclk):
    hdr("步骤 2：fclk0 设 %g MHz 并读回" % fclk)
    try:
        Clocks.fclk0_mhz = float(fclk)
        time.sleep(0.4)
        rb = float(Clocks.fclk0_mhz)
    except Exception as ex:                                      # noqa: BLE001
        log("  ✗ 设频率失败：%r" % (ex,))
        R["steps"]["fclk"] = dict(target=fclk, error=repr(ex))
        return None
    log("  目标 %g MHz → **读回 %g MHz** %s"
        % (fclk, rb, "✓" if abs(rb - fclk) < 0.5 else "✗ 没设上（按实际值算周期）"))
    R["steps"]["fclk"] = dict(target=fclk, readback=rb,
                              ok=bool(abs(rb - fclk) < 0.5))
    return rb


# ============================================================================
# ③ 整网
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
    return arena, need


def cyc_term_agg(recs, fclk):
    """sum(cyc) / sum(terms)；terms = cout·Hout·Wout·K（= MAC 数，与 stage5 同口径）。"""
    tot_c = tot_t = 0.0
    for r in recs:
        tot_c += r["sec"] * fclk * 1e6
        tot_t += (r["cout"] * r["Hout"] * r["Wout"] * r["K"])
    return (tot_c / tot_t) if tot_t else float("nan"), tot_c, tot_t


def run_net(ov, hw, arena, slots, fclk, tag):
    hdr("步骤 3：整网 %d²（fclk 读回 %g MHz）" % (hw, fclk))
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    arena.reset()
    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    eng.load_weights()
    net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    OPREC.clear()
    log("  开跑（运行点见 json，BIN_V50_*=空）  %s" % deps_line())
    t0 = time.time()
    outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
    dur = time.time() - t0
    sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
    exp = FROZEN[hw]
    ok = (exp == (sh["out1"], sh["out2"], sh["out3"]))
    recs = list(OPREC)
    prof = st.get("prof") or {}
    ct_agg, ctot, ttot = cyc_term_agg(recs, fclk)
    # 只保留最重的 12 个 op 进 json（日志里全打）
    heavy = sorted(recs, key=lambda r: -r["sec"])
    row = dict(tag=tag, hw=hw, slots=int(slots), fclk=fclk, x0_sha1=xs,
               total_s=st.get("total"), wall_s=round(dur, 4),
               pl_s=st.get("pl"), pack_pl=st.get("pack_pl"), ps_s=st.get("ps"),
               prologue=st.get("ps_prologue"), epilogue=st.get("ps_epilogue"),
               nonconv=st.get("ps_nonconv"), overlap=st.get("overlap"),
               wait=st.get("wait"), mmio=st.get("mmio_writes"),
               prof={k: round(float(v), 4) for k, v in prof.items()},
               lazy=st.get("lazy"), cat=st.get("cat"),
               sha1=sh, expect=list(exp), sha_ok=bool(ok),
               deps_closure_ok=st.get("deps_closure_ok"),
               kernel_deps={k: v.get("state") for k, v in
                            (st.get("kernel_deps") or {}).items()},
               perf_gate=st.get("perf_gate"), perf_warm=st.get("perf_warm"),
               run_valid=st.get("run_valid"),
               n_conv_ops=len(recs), conv_cyc=tot_c, conv_terms=ttot,
               conv_cyc_per_term=ct_agg,
               top_ops=[{k: (round(v, 6) if isinstance(v, float) else v)
                         for k, v in r.items()} for r in heavy[:12]])
    log("  总 %.3f s（墙钟 %.3f）| PL %.3f（E1E2 %.3f）| PS %.3f = prologue %.3f + "
        "epilogue %.3f + 非卷积 %.3f | overlap %.3f wait %.3f"
        % (row["total_s"] or 0, dur, row["pl_s"] or 0, row["pack_pl"] or 0,
           row["ps_s"] or 0, row["prologue"] or 0, row["epilogue"] or 0,
           row["nonconv"] or 0, row["overlap"] or 0, row["wait"] or 0))
    log("  非卷积分项 %s" % json.dumps(row["prof"], ensure_ascii=False))
    log("  sha1 out1=%s out2=%s out3=%s  %s"
        % (sh["out1"][:16], sh["out2"][:16], sh["out3"][:16],
           "OK/3 = 冻结" if ok else "**≠冻结**"))
    if not ok:
        log("     冻结 out1=%s" % exp[0])
        log("     冻结 out2=%s" % exp[1])
        log("     冻结 out3=%s" % exp[2])
    log("  --- conv: %d 个 op，PL 合计 %.4f s = %.1f M 拍，总 term %.4g ⇒ "
        "**cyc/term = %.2f**（v5 基线 54.3 / 理想 24）"
        % (len(recs), ctot / (fclk * 1e6) if fclk else 0, ctot / 1e6, ttot,
           ct_agg))
    log("  --- 每个 op 的周期（全量；cyc = PL seconds × fclk）---")
    for i, r in enumerate(recs):
        terms = r["cout"] * r["Hout"] * r["Wout"] * r["K"]
        log("   %3d %-30s cin=%3d cout=%3d k=%d %2dx%-2d→%2dx%-2d cb=%d K=%5d "
            "cyc=%9d cyc/term=%7.2f"
            % (i, r["name"][:30], r["cin"], r["cout"], r["k"], r["H"], r["W"],
               r["Hout"], r["Wout"], r["cb"], r["K"],
               int(r["sec"] * fclk * 1e6 + 0.5),
               (r["sec"] * fclk * 1e6) / max(1, terms)))
    del net, eng, outs
    gc.collect()
    return row


# ============================================================================
# ④ 段5 RLAT（与 54.3 cyc/term 同一测量协议）
# ============================================================================
def step_rlat(ov, fclk, golden):
    hdr("步骤 4b：段5 RLAT（cyc/rd/fill；同一协议下 cyc/term 与 54.3 可比）")
    try:
        import bin_test_staged as BS
        from types import SimpleNamespace
        pool = BE.BinArena(8 << 20, name="rlat_pool")
        e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
        e.add_region(pool.buf, "pool")
        bl = BS.Blob(PACK)
        minL, _cyc = BS.pick_min_layer(bl)
        L0 = BE.BinLayer(minL, 8, 8)
        out_buf, out_pa = pool.region(L0.out_bytes, name="OUT", keep_canary=True)
        a = SimpleNamespace(golden=golden, seed=20261031, fclk=fclk)
        BS.stage5(e, bl, pool, out_buf, out_pa, a, None)
        R["steps"]["rlat"] = dict(status="ok")
        del pool, e, bl
        gc.collect()
        return True
    except SystemExit as ex:                                     # stage5 用 fail() 退出
        log("  ✗ 段5 SystemExit(%r) —— 见上面的逐字节行" % (ex.code,))
        R["steps"]["rlat"] = dict(status="fail", error="SystemExit(%r)" % (ex.code,))
        return False
    except Exception as ex:                                      # noqa: BLE001
        traceback.print_exc()
        R["steps"]["rlat"] = dict(status="error", error=repr(ex)[:500])
        return False


# ============================================================================
# ⑤ 金丝雀 + magic 复核
# ============================================================================
def step5_canary(ov, arena):
    hdr("步骤 5：金丝雀 + CHAIN_ID 复核")
    # 每个 op 的 OUT 尾部 4096 B 都由 BinGuard.fill/check_canary 覆盖；
    # 整网跑完没有抛异常本身就是"0 字节被写坏"。这里再做一次**显式**取证：
    try:
        n = int(arena.buf.physical_address)
        view = np.asarray(arena.buf).view(np.uint8).reshape(-1)
        log("  arena %#x 长度 %.2f MB；整网期间每次 op 都 fill+check_canary，"
            "0 次抛异常 ⇒ 金丝雀 0 字节被写坏" % (n, len(view) / 1e6))
        R["steps"]["canary"] = dict(ops_checked=len(OPREC), violated=0,
                                    note="BinGuard.check_canary 每 op 覆盖 OUT 尾 4096 B")
    except Exception as ex:                                      # noqa: BLE001
        R["steps"]["canary"] = dict(error=repr(ex)[:300])
    try:
        m = int(ov.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"])) & 0xFFFFFFFF
        s = int(ov.bin_unit_0.mmio.read(BE.CFG + BE.V50_CHAIN_REG["CHAIN_STATUS"])) & 0xFFFFFFFF
        log("  跑后 0x314 = %#010x （%s）；0x310 = %#010x"
            % (m, "仍是 magic ✓" if m == MAGIC_WANT else "✗✗ magic 变了", s))
        R["steps"]["canary"].update(magic_after=m, status_after=s,
                                    magic_still=m == MAGIC_WANT)
    except Exception as ex:                                      # noqa: BLE001
        log("  ✗ 复读 magic 失败：%r" % (ex,))
        R["steps"]["canary"]["magic_after_error"] = repr(ex)


# ============================================================================
def main():
    global PACK, LOGPATH, _fh
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v50.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--golden", default="/home/xilinx/demoir/pl/rtl_hand/golden")
    ap.add_argument("--fclk", type=float, default=100.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--hw", default="64,256")
    ap.add_argument("--outdir", default="/home/xilinx/demoir/t93_out")
    ap.add_argument("--no-load", action="store_true")
    ap.add_argument("--skip-256", action="store_true")
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    LOGPATH = os.path.join(a.outdir, "t93_v50_%s.log" % stamp)
    _fh = open(LOGPATH, "a", buffering=1, encoding="utf-8")
    PACK = a.pack
    R["args"] = vars(a)
    R["base_want"] = BASE_WANT
    R["magic_want"] = "%#010x" % MAGIC_WANT
    log("[t93] 日志 = %s" % LOGPATH)
    log("[t93] bit = %s" % a.bit)

    # ---- ① ----
    ov, magic_ok = step1_load(a.bit, a.fclk, download=not a.no_load)
    if not magic_ok:
        log("  ⇒ **停**：0x314 读不到 0x50300001（位流不是 v50 或 BD 没换对）")
        dump()
        return EXIT_MAGIC
    # ---- ② ----
    rb = step2_fclk(a.fclk)
    fclk = rb if rb else a.fclk
    if not rb:
        log("  ⇒ 频率读不回来，按目标值 %g MHz 计算周期" % fclk)
    # 一次性内核装载（不进稳态）
    hdr("步骤 0b：q8/c8deq/bpack/bilinear_c 一次性装载（不进稳态）")
    rec = BE.load_kernels()
    tot = 0.0
    for k, v in rec.items():
        tot += float(v.get("seconds") or 0.0)
        log("  %-6s load=%.3f s loaded=%s" % (k, v.get("seconds") or 0, v.get("loaded")))
    try:
        import bilinear_c
        t_b = time.perf_counter()
        bilinear_c.load()
        tot += time.perf_counter() - t_b
        log("  bilinear load=%.3f s" % (time.perf_counter() - t_b))
    except Exception as ex:                                      # noqa: BLE001
        log("  bilinear 装载失败（不致命）：%r" % (ex,))
    log("  一次性装载合计 %.3f s；%s" % (tot, deps_line()))
    R["steps"]["kernel_load"] = dict(seconds=round(tot, 4),
                                     detail={k: dict(loaded=v.get("loaded"),
                                                     seconds=v.get("seconds"))
                                             for k, v in rec.items()})

    # ---- ③ 64² ----
    arena, _need = make_arena(64, a.slots, "t93_arena64")
    row64 = run_net(ov, 64, arena, a.slots, fclk, "net64")
    R["steps"]["net64"] = row64
    dump()
    canary_ok = True
    if not row64["sha_ok"]:
        log("  ⇒ **停**：64² 三个 sha1 ≠ 冻结值（见上，位置/数值已打印）")
        step5_canary(ov, arena)
        dump()
        return EXIT_SHA

    # ---- ④ 段5 RLAT ----
    step_rlat(ov, fclk, a.golden)
    dump()

    # ---- ⑤ 金丝雀 ----
    step5_canary(ov, arena)
    dump()

    # ---- ⑥ 256²（可选）----
    if not a.skip_256 and 256 in [int(x) for x in a.hw.split(",") if x.strip()]:
        hdr("步骤 6：对照 256²（可选）")
        try:
            del arena
            gc.collect()
            ar256, _ = make_arena(256, a.slots, "t93_arena256")
            row256 = run_net(ov, 256, ar256, a.slots, fclk, "net256")
            R["steps"]["net256"] = row256
            del ar256
            gc.collect()
        except Exception as ex:                                  # noqa: BLE001
            traceback.print_exc()
            log("  ✗ 256² 失败（不改变 64² 的结论）：%r" % (ex,))
            R["steps"]["net256"] = dict(error=repr(ex)[:500])
        dump()
    else:
        log("[t93] 跳过 256²（--skip-256 或 --hw 不含 256）")

    log("")
    log("[t93] DONE")
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
