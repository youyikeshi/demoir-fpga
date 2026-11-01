#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""board_v39_verify.py —— v39 单次 load 真机验证**驱动**。

本文件只做编排，不碰算法：
    load 一次 → 地址/身份 → 整网 256² → 整网 64² → 单层 RLAT → 频率曲线
  整网：bin_net.BinNet（原样调用，含 BIN_PACK/BIN_LAZY_DEQ/BIN_CAT_FUSION 语义）
  单层：bin_test_staged.stage5（原样调用，只为拿 cyc/rd/fill ⇒ RLAT）

判据（冻结基线，与 board_cat_fusion_ab.FROZEN 同源）：
    256: 9b1940c8955b / f86a7035621b / 025757a83c39
     64: bfa246d34681 / 226bd39e522a / 762d84b9618e
配置（任务书指定）：BIN_LAZY_DEQ=1  BIN_LAZY_SLOTS=24  Q8CACHE=0
    + 基线同款 BIN_PACK=e1e2  BIN_CAT_FUSION=1  （否则拿不到 8.3 s 那一档）

用法（板上）：
  source /etc/profile.d/xrt_setup.sh
  /usr/local/share/pynq-venv/bin/python3 -u board_v39_verify.py \
      --bit /home/xilinx/demoir/design_1_150v39.bit --fclk 100 \
      --freqs 125,150 --slots 24 --json /home/xilinx/demoir/v39_verify.json
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
from types import SimpleNamespace

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, "pynq")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

try:                                                   # PYNQ 3.1 不自动注册 Device
    import pynq.pl_server.embedded_device as _emb      # noqa: F401
except Exception:                                      # noqa: BLE001
    pass
from pynq import Overlay, Clocks                       # noqa: E402

import bin_engine as BE                                # noqa: E402
import bin_net as BN                                   # noqa: E402

PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
GOLDEN = "/home/xilinx/demoir/pl/rtl_hand/golden"
FROZEN = {
    64: ("bfa246d34681e748311bbcd2f1b87317a56923fe",
         "226bd39e522aa373b8fc183acae5280931f8b63f",
         "762d84b9618eab8922edd6da4f07ba06de865628"),
    256: ("9b1940c8955be53ccffd7905e333deffcea31f0a",
          "f86a7035621bed133374a1bef42a27db204e7019",
          "025757a83c3993c8f592e31219b6a97906020363"),
}
# 基线输入（seed=1，(3,hw,hw) float32 均匀分布）的 sha1，用来证明"同一输入"
X0_SHA1 = {256: "cfc75bc3f15d306b"}                    # 只记了 256；64 现场打印

LOGPATH = "/home/xilinx/demoir/v39_verify.log"
_fh = None
R = {"steps": {}}


def log(s=""):
    global _fh
    s = str(s)
    print(s, flush=True)
    if _fh is None:
        _fh = open(LOGPATH, "a", buffering=1, encoding="utf-8")
    _fh.write(s + "\n")
    _fh.flush()
    try:
        os.fsync(_fh.fileno())
    except Exception:                                  # noqa: BLE001
        pass


def hdr(s):
    log("")
    log("=" * 74)
    log("  " + s)
    log("=" * 74)


class _Tee:
    """把 stage5 的 print 同时写进本脚本的日志（只为留证，不改其逻辑）。"""

    def __init__(self, fh, real):
        self.fh = fh
        self.real = real

    def write(self, s):
        self.real.write(s)
        if self.fh is not None:
            self.fh.write(s)
        return len(s)

    def flush(self):
        self.real.flush()
        if self.fh is not None:
            self.fh.flush()


def sha1_of(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def dump(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False)


# ---------------------------------------------------------------- 配置开关
_KEYS = ("BIN_PACK", "BIN_LAZY_DEQ", "BIN_LAZY_SLOTS", "CATPACK",
         "BIN_CAT_FUSION", "BIN_CAT_STRICT", "Q8CACHE", "Q8CACHE_MB",
         "Q8_C8_IMPL", "BIN_Q8_FUSED")


def cfg(slots):
    for k in _KEYS:
        os.environ.pop(k, None)
    os.environ["BIN_PACK"] = "e1e2"          # 基线同款：E1E2 硬打包
    os.environ["BIN_LAZY_DEQ"] = "1"         # 任务书指定
    os.environ["BIN_LAZY_SLOTS"] = str(int(slots))   # 任务书指定 24
    os.environ["BIN_CAT_FUSION"] = "1"       # S2 融合（8.3 s 那一档）
    os.environ["Q8CACHE"] = "0"              # 任务书指定
    os.environ["BIN_Q8_FUSED"] = "1"
    return {k: os.environ.get(k, "") for k in _KEYS}


# ---------------------------------------------------------------- 步骤 1：load
def step_load(bit, download=True):
    hdr("步骤 1：加载 overlay（本 session 唯一一次真正的 download）")
    t0 = time.time()
    ov = Overlay(bit, download=download)
    dt = time.time() - t0
    log("  Overlay(%s, download=%s) 用时 %.3f s" % (bit, download, dt))
    R["load"] = dict(bit=bit, download=bool(download), seconds=round(dt, 4))
    return ov


# ---------------------------------------------------------------- 步骤 2：地址
def step_addr(ov):
    hdr("步骤 2：地址核对（从**已加载的** overlay 实测）")
    want = {"conv_uni_0": 0x40040000, "bin_unit_0": 0x40060000}
    got = {}
    for name, exp in want.items():
        try:
            ip = getattr(ov, name)
            ba = int(ip.mmio.base_addr)
            ln = int(ip.mmio.length)
            got[name] = dict(base_addr=ba, base_addr_hex="%#010x" % ba,
                             length=ln, expected_hex="%#010x" % exp,
                             ok=bool(ba == exp))
            log("  %-12s mmio.base_addr = %#010x  length = %#x  （期望 %#010x）  %s"
                % (name, ba, ln, exp, "✓" if ba == exp else "✗✗"))
        except Exception as ex:                        # noqa: BLE001
            got[name] = dict(error=repr(ex)[:300])
            log("  %-12s ✗ 读不到：%r" % (name, ex))
    try:
        log("  overlay 里的 IP 清单：%s" % sorted(ov.ip_dict.keys()))
    except Exception:                                  # noqa: BLE001
        pass
    R["addr"] = got
    return got


# ---------------------------------------------------------------- 步骤 3：身份
def step_identity(ov, fclk):
    hdr("步骤 3：时钟读回 + 复位指纹")
    Clocks.fclk0_mhz = fclk
    time.sleep(0.4)
    rb = Clocks.fclk0_mhz
    log("  Clocks.fclk0_mhz 目标 %g → **读回 = %g**  %s"
        % (fclk, rb, "✓" if abs(rb - fclk) < 0.5 else "✗ 没设上"))
    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    ash = e._rd_reg("ALPHA_SHIFT")
    ssh = e._rd_reg("SLOPE_SHIFT")
    ctl = e._rd_reg("AP_CTRL")
    ap = e._rd_reg("AP_RET")
    dfl = e._rd_reg("DBG_FLAG")
    resv = e._rd(0x18)
    log("  复位指纹：alpha_shift=%d slope_shift=%d （期望 9 / 8）" % (ash, ssh))
    log("  AP_CTRL=%#010x AP_RETURN=%#010x DBG_FLAG=%#010x" % (ctl, ap, dfl))
    log("  保留页 ResvPage[0x18] = %#x （期望 0）" % resv)
    log("  引擎认到的 IP=%s base=%#x range=%#x"
        % (e.ip_name, e.base_pa, e.range_size))
    ok = (ash, ssh) == (9, 8) and resv == 0
    d = dict(alpha_shift=ash, slope_shift=ssh, ap_ctrl=ctl, ap_ret=ap,
             dbg_flag=dfl, resv_0x18=resv, fclk_readback=rb, fclk_target=fclk,
             ip=e.ip_name, base=e.base_pa, range=e.range_size, ok=bool(ok))
    R["identity"] = d
    del e
    gc.collect()
    if not ok:
        log("  ✗✗ 指纹不对 ⇒ bit 或地址错，停止")
    return d


# ---------------------------------------------------------------- 整网一次
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
    log("  arena %-14s %.2f MB（%d 槽 × (in %.2f + out %.2f MB)）@%#x"
        % (name, need / 1e6, max(2, int(slots)), max_in / 1e6, max_out / 1e6,
           int(arena.buf.physical_address)))
    return arena, need


def run_net(ov, hw, arena, slots, tag=""):
    env = cfg(slots)
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    arena.reset()
    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    eng.load_weights()
    net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
    rng = np.random.default_rng(1)
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    t0 = time.time()
    outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
    dur = time.time() - t0
    sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
    ok = (FROZEN[hw] == (sh["out1"], sh["out2"], sh["out3"]))
    prof = st.get("prof") or {}
    row = dict(tag=tag, hw=hw, slots=int(slots), env=env, x0_sha1=xs,
               total_s=st.get("total"), wall_s=round(dur, 4),
               pl_s=st.get("pl"), pack_pl=st.get("pack_pl"),
               ps_s=st.get("ps"), prologue=st.get("ps_prologue"),
               epilogue=st.get("ps_epilogue"), nonconv=st.get("ps_nonconv"),
               overlap=st.get("overlap"), wait=st.get("wait"),
               mmio=st.get("mmio_writes"),
               prof={k: round(float(v), 4) for k, v in prof.items()},
               lazy=st.get("lazy"), cat=st.get("cat"),
               sha1=sh, expect=FROZEN[hw], sha_ok=bool(ok),
               n_sha_ok=int(ok) * 3)
    log("  ── %s hw=%d ──" % (tag or "net", hw))
    log("     总 %.3f s（墙钟 %.3f） PL %.3f（其中 E1E2 %.3f）| PS %.3f = "
        "prologue %.3f + epilogue %.3f + 非卷积 %.3f | overlap %.3f wait %.3f"
        % (row["total_s"] or 0, dur, row["pl_s"] or 0, row["pack_pl"] or 0,
           row["ps_s"] or 0, row["prologue"] or 0, row["epilogue"] or 0,
           row["nonconv"] or 0, row["overlap"] or 0, row["wait"] or 0))
    log("     非卷积分项 %s" % json.dumps(row["prof"], ensure_ascii=False))
    log("     输入 sha1=%s %s" % (xs[:16],
                                 "(与冻结基线一致)" if hw != 256 or
                                 xs.startswith(X0_SHA1[256]) else "(⚠ 与基线不同)"))
    log("     sha1 out1=%s out2=%s out3=%s  %s"
        % (sh["out1"][:12], sh["out2"][:12], sh["out3"][:12],
           "OK/3" if ok else "**≠冻结基线**"))
    del net, eng, outs
    gc.collect()
    return row


# ---------------------------------------------------------------- 步骤 4/5
def step_net(ov, hw, slots, fclk):
    hdr("步骤 4：整网 %d² 功能判据（fclk 读回 %g MHz）" % (hw, fclk))
    arena, need = make_arena(hw, slots, "v39_arena_%d" % hw)
    row = run_net(ov, hw, arena, slots, tag="net%d" % hw)
    R["net_%d" % hw] = row
    del arena
    gc.collect()
    return row


# ---------------------------------------------------------------- 步骤 6：RLAT
def step_rlat(ov, fclk):
    hdr("步骤 6：单层 RLAT（复用 bin_test_staged.stage5，只为拿 cyc/rd/fill）")
    import bin_test_staged as BS
    pool = BE.BinArena(8 << 20, name="rlat_pool")
    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    e.add_region(pool.buf, "pool")
    bl = BS.Blob(PACK)          # Blob 由 bin_ref_numpy 提供，bin_test_staged 已导入
    minL, cyc = BS.pick_min_layer(bl)
    L0 = BE.BinLayer(minL, 8, 8)
    out_buf, out_pa = pool.region(L0.out_bytes, name="OUT", keep_canary=True)
    a = SimpleNamespace(golden=GOLDEN, seed=20261031, fclk=fclk)
    real = sys.stdout
    sys.stdout = _Tee(_fh, real)
    try:
        BS.stage5(e, bl, pool, out_buf, out_pa, a, None)   # wdt 要对象不要秒数
        R["rlat"] = dict(status="ok", note="明细见下方日志 cyc/rd/fill 行")
    except Exception as ex:                            # noqa: BLE001
        traceback.print_exc()
        R["rlat"] = dict(status="error", error=repr(ex)[:500])
    finally:
        sys.stdout = real
        log("  （stage5 结束）")
    del pool, e, bl
    gc.collect()


# ---------------------------------------------------------------- 步骤 7：曲线
def step_sweep(ov, hw, slots, freqs, reps, fclk0):
    hdr("步骤 7：频率曲线（同 bit / 同 PS 代码，只切 Clocks.fclk0_mhz）")
    arena, need = make_arena(hw, slots, "v39_sweep_%d" % hw)
    rows = []
    for f in freqs:
        try:
            Clocks.fclk0_mhz = f
        except Exception as ex:                        # noqa: BLE001
            log("  设 %g MHz 失败：%r" % (f, ex))
            continue
        time.sleep(0.4)
        got = Clocks.fclk0_mhz
        best = None
        for r in range(max(1, reps)):
            try:
                row = run_net(ov, hw, arena, slots, tag="sweep@%g rep%d" % (f, r))
            except Exception as ex:                    # noqa: BLE001
                log("  %g MHz rep%d 异常：%r" % (f, r, ex))
                row = dict(tag="sweep@%g" % f, error=repr(ex)[:400])
            if "error" in row:
                best = best or row
                continue
            if best is None or "error" in best or row["total_s"] < best["total_s"]:
                best = row
        if best is not None and "error" not in best:
            best["req_mhz"] = f
            best["got_mhz"] = got
            best["reps"] = reps
            rows.append(best)
            log("  >>> 请求 %g MHz（读回 %g）→ min 总 %.3f s / PL %.3f s  sha1 %s"
                % (f, got, best["total_s"], best["pl_s"] or 0,
                   "OK" if best["sha_ok"] else "**≠**"))
            R["sweep"] = rows
            dump(R["_json"])
        else:
            rows.append(dict(req_mhz=f, got_mhz=got, error=str(best)[:300]))
            R["sweep"] = rows
            dump(R["_json"])
    del arena
    gc.collect()
    try:
        Clocks.fclk0_mhz = fclk0
        log("  时钟已恢复到 %g MHz（读回 %g）" % (fclk0, Clocks.fclk0_mhz))
        R["fclk_restored"] = Clocks.fclk0_mhz
    except Exception as ex:                            # noqa: BLE001
        log("  ⚠ 恢复时钟失败：%r" % (ex,))
    return rows


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v39.bit")
    ap.add_argument("--fclk", type=float, default=100.0)
    ap.add_argument("--freqs", default="125,150",
                    help="曲线频率（100 已在功能判据里测过；175 需自行判断余量）")
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--json", default="/home/xilinx/demoir/v39_verify.json")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--skip", default="", help="rlat,sweep,net64")
    a = ap.parse_args(argv)
    skip = set(x.strip() for x in a.skip.split(",") if x.strip())
    R["_json"] = a.json
    R["args"] = vars(a)
    R["started"] = time.strftime("%Y-%m-%d %H:%M:%S")
    R["env_at_start"] = {k: os.environ.get(k, "") for k in _KEYS}

    ov = step_load(a.bit, download=not a.no_download)
    step_addr(ov)
    ident = step_identity(ov, a.fclk)
    dump(a.json)
    if not ident.get("ok"):
        log("!! 指纹不对，停止后续测量")
        R["fatal"] = "fingerprint"
        R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        dump(a.json)
        return 3

    # 256²（最重要）
    r256 = step_net(ov, 256, a.slots, Clocks.fclk0_mhz)
    dump(a.json)
    if not r256.get("sha_ok"):
        log("!! 256² sha1 ≠ 冻结基线 ⇒ 真问题，停在这里（不做 64/曲线）")
        R["fatal"] = "net256 sha1 mismatch"
        R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        dump(a.json)
        return 4

    # 64²
    if "net64" not in skip:
        r64 = step_net(ov, 64, a.slots, Clocks.fclk0_mhz)
        dump(a.json)
        if not r64.get("sha_ok"):
            log("!! 64² sha1 ≠ 冻结基线 ⇒ 真问题")
            R["fatal"] = "net64 sha1 mismatch"

    if "rlat" not in skip:
        try:
            step_rlat(ov, Clocks.fclk0_mhz)
        except Exception:                              # noqa: BLE001
            traceback.print_exc()
            R["rlat"] = dict(status="crash")
        dump(a.json)

    if "sweep" not in skip:
        freqs = [float(x) for x in a.freqs.split(",") if x.strip()]
        step_sweep(ov, 256, a.slots, freqs, a.reps, a.fclk)

    # ---- 汇总
    hdr("汇总")
    R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    dump(a.json)
    for key in ("net_256", "net_64"):
        r = R.get(key)
        if not r:
            continue
        log("  %-8s 总 %7.3f  PL %6.3f  prologue %6.3f  epilogue %5.3f  "
            "非卷积 %5.3f  sha1 %s"
            % (key, r["total_s"] or 0, r["pl_s"] or 0, r["prologue"] or 0,
               r["epilogue"] or 0, r["nonconv"] or 0,
               "3/3 OK" if r["sha_ok"] else "**≠**"))
    for r in R.get("sweep") or []:
        if "error" in r:
            log("  @%g MHz ERROR %s" % (r["req_mhz"], r["error"]))
            continue
        log("  @%-6g MHz（读回 %g）总 %7.3f  PL %6.3f  sha1 %s"
            % (r["req_mhz"], r["got_mhz"], r["total_s"], r["pl_s"] or 0,
               "3/3 OK" if r["sha_ok"] else "**≠**"))
    log("  → %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
