#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""board_lazy_ab.py —— 板端整网 A/B：E1E2 × LAZY_DEQ 四种组合（**不下载比特流**）。

接管已加载的 v33（`Overlay(..., download=False)`），同一输入、同一 PL、同一 fclk，
只改环境变量：

    E1E2   LAZY_DEQ   路径
    off    off        今天的基线（f32 反量化 → PS quant_pack）
    off    on         惰性开关被忽略（E1E2 没挂）⇒ 应与基线逐字节/同时间
    on     off        E1E2 老路径：f32 物化 + quantize_int8(140 ms/层) ⇒ ~87 s
    on     on         **新路径**：E1E2 直接从 conv 的 int8 C8 OUT 读

判据：四组 sha1 必须等于冻结基线；报告 总/PL/prologue/epilogue/非卷积。

用法（板上）：
  /usr/local/share/pynq-venv/bin/python3 -u board_lazy_ab.py --hw 256 --fclk 100
"""
import argparse
import gc
import hashlib
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import pynq.pl_server.embedded_device as _emb       # noqa: E402,F401
except Exception:                                      # noqa: BLE001
    pass
from pynq import Overlay, Clocks                        # noqa: E402

import bin_engine as BE                                 # noqa: E402
import bin_net as BN                                    # noqa: E402

PACK = os.environ.get("PACK", "/home/xilinx/demoir/deploy/bin_weights_new")
BIT = os.environ.get("BIT", "/home/xilinx/demoir/design_1_150v33.bit")
FROZEN = {
    64: ("bfa246d34681e748311bbcd2f1b87317a56923fe",
         "226bd39e522aa373b8fc183acae5280931f8b63f",
         "762d84b9618eab8922edd6da4f07ba06de865628"),
    256: ("9b1940c8955be53ccffd7905e333deffcea31f0a",
          "f86a7035621bed133374a1bef42a27db204e7019",
          "025757a83c3993c8f592e31219b6a97906020363"),
}


def sha1(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def set_env(e1e2, lazy, slots):
    os.environ.pop("BIN_PACK", None)
    os.environ.pop("BIN_LAZY_DEQ", None)
    os.environ.pop("CATPACK", None)
    if e1e2:
        os.environ["BIN_PACK"] = "e1e2"
    os.environ["BIN_LAZY_DEQ"] = "1" if lazy else "0"
    os.environ["BIN_LAZY_SLOTS"] = str(int(slots))


def run_one(ov, hw, e1e2, lazy, slots, arena, x0, seed, log):
    set_env(e1e2, lazy, slots)
    arena.reset()                       # 同一块 arena 复用（region() 是 append 式）
    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    eng.load_weights()
    net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
    tag = "E1E2=%-3s LAZY=%-3s" % ("on" if e1e2 else "off",
                                   "on" if lazy else "off")
    log("  ── %s ──" % tag)
    t0 = time.time()
    outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
    dur = time.time() - t0
    sh = {k: sha1(outs[k]) for k in ("out1", "out2", "out3")}
    ok = (FROZEN[hw] == (sh["out1"], sh["out2"], sh["out3"]))
    row = dict(tag=tag, e1e2=bool(e1e2), lazy=bool(lazy),
               total_s=st["total"], wall_s=dur, pl_s=st.get("pl"),
               ps_s=st.get("ps"), prologue=st.get("ps_prologue"),
               epilogue=st.get("ps_epilogue"), nonconv=st.get("ps_nonconv"),
               overlap=st.get("overlap"), wait=st.get("wait"),
               mmio=st.get("mmio_writes"), lazy_stats=st.get("lazy"),
               pack_pl=st.get("pack_pl"),
               lazy_deq=st.get("lazy_deq"), sha1=sh, sha_ok=bool(ok),
               rss_kb=_rss())
    log("     总 %.3f s（墙钟 %.3f） PL %.3f（其中 E1E2 %.3f）| PS %.3f = prologue %.3f + "
        "epilogue %.3f + 非卷积 %.3f | overlap %.3f wait %.3f"
        % (row["total_s"], dur, row["pl_s"] or 0, row["pack_pl"] or 0,
           row["ps_s"] or 0, row["prologue"] or 0, row["epilogue"] or 0,
           row["nonconv"] or 0, row["overlap"] or 0, row["wait"] or 0))
    _buckets = (row["pl_s"] or 0) + (row["ps_s"] or 0) - (row["overlap"] or 0)
    log("     口径：总=墙钟；桶和 PL+PS-overlap+wait = %.3f（PL 与 PS 重叠部分"
        "只算一次）" % (_buckets + (row["wait"] or 0)))
    log("     sha1 %s  %s  | C8直读 %s"
        % (sh["out1"][:12], "OK" if ok else "**≠冻结基线**",
           json.dumps(row["lazy_stats"] or {}, ensure_ascii=False)))
    del net, eng, outs
    gc.collect()
    return row


def _rss():
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1])
    except Exception:                                   # noqa: BLE001
        pass
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hw", type=int, default=256)
    ap.add_argument("--fclk", type=float, default=100.0)
    ap.add_argument("--slots", type=int, default=16)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--bit", default=BIT)
    ap.add_argument("--json", default="/home/xilinx/demoir/lazy_ab.json")
    ap.add_argument("--only", default="",
                    help="只跑某些组合，如 'on/on,off/off'")
    ap.add_argument("--warm-repeat", type=int, default=1,
                    help="末尾再跑一遍 off/off（热内核）⇒ 与冷启动基线对照")
    a = ap.parse_args(argv)
    lines = []

    def log(s):
        print(s, flush=True)
        lines.append(s)

    t_start = time.strftime("%Y-%m-%d %H:%M:%S")
    log("== board_lazy_ab：E1E2 × LAZY_DEQ（hw=%d，fclk=%.0f，不下载比特流）=="
        % (a.hw, a.fclk))
    ov = Overlay(a.bit, download=False)
    Clocks.fclk0_mhz = a.fclk
    time.sleep(0.4)
    log("  PL 已接管（download=False），fclk0 读回 = %g MHz" % Clocks.fclk0_mhz)
    # 探针：确认这份比特流同时有 E1E2 与 conv
    probe = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    ee = probe.enable_e1e2()
    log("  E1E2 probe：R_RUN_UNIT 写 4 读 4 / 写 0 读 0 通过")
    probe.disable_e1e2()
    del probe, ee
    gc.collect()

    # 共享 arena：尺寸按 BIN_LAZY_SLOTS 的最大配置算（配置间不重复分配 CMA）
    net0 = BN.BinNet(PACK, engine=None, H=a.hw, W=a.hw, verbose=False)
    g, _outs, _tin = net0.build_graph(np.zeros((3, a.hw, a.hw), np.float32))
    conv = [o for o in g.ops if o.kind == "conv"]
    max_in = max(o.layer.act_bytes for o in conv)
    max_out = max(o.layer.out_bytes for o in conv)
    need = (max_in + max_out + 4096) * max(2, a.slots) + (1 << 16)
    arena = BE.BinArena(need, name="ab_arena")
    log("  IN/OUT arena：%.2f MB（%d 槽 × (in %.2f + out %.2f MB)）@%#x"
        % (need / 1e6, max(2, a.slots), max_in / 1e6, max_out / 1e6,
           int(arena.buf.physical_address)))
    del net0, g
    gc.collect()

    rng = np.random.default_rng(a.seed)
    x0 = rng.random((3, a.hw, a.hw), dtype=np.float32)
    log("  输入 (3,%d,%d) seed=%d sha1=%s"
        % (a.hw, a.hw, a.seed, sha1(x0)[:16]))

    os.environ["BV_FREE"] = "1" if a.hw >= 256 else "0"
    want = None
    if a.only:
        want = set(a.only.replace(" ", "").split(","))
    combos = [(False, False), (False, True), (True, False), (True, True)]
    rows = []
    for e1e2, lazy in combos:
        key = ("on" if e1e2 else "off") + "/" + ("on" if lazy else "off")
        if want is not None and key not in want:
            log("  ── %s ──（跳过）" % key)
            continue
        try:
            rows.append(run_one(ov, a.hw, e1e2, lazy, a.slots, arena, x0,
                                a.seed, log))
        except Exception as ex:                          # noqa: BLE001
            import traceback
            traceback.print_exc()
            rows.append(dict(tag=key, error=repr(ex)[:400]))
        gc.collect()

    if a.warm_repeat and want is None:
        log("")
        log("  ── 复核：off/off 热内核（排除一次性 .so 加载/自检） ──")
        try:
            rows.append(run_one(ov, a.hw, False, False, a.slots, arena, x0,
                                a.seed, log))
        except Exception as ex:                          # noqa: BLE001
            import traceback
            traceback.print_exc()
            rows.append(dict(tag="off/off (warm)", error=repr(ex)[:400]))

    ok = all(r.get("sha_ok") for r in rows) and len(rows) > 0
    log("")
    log("== 汇总（hw=%d）==" % a.hw)
    for r in rows:
        if "error" in r:
            log("  %-16s ERROR %s" % (r.get("tag"), r["error"]))
            continue
        log("  %-16s 总 %7.3f  PL %5.3f  prologue %7.3f  epilogue %5.3f  "
            "非卷积 %5.3f  sha1 %s"
            % (r["tag"], r["total_s"], r["pl_s"] or 0, r["prologue"] or 0,
               r["epilogue"] or 0, r["nonconv"] or 0,
               "OK" if r["sha_ok"] else "**≠**"))
    res = dict(started=t_start, finished=time.strftime("%Y-%m-%d %H:%M:%S"),
               hw=a.hw, fclk=a.fclk, slots=a.slots, seed=a.seed,
               x0_sha1=sha1(x0), frozen=FROZEN[a.hw], rows=rows, ok=bool(ok))
    json.dump(res, open(a.json, "w"), indent=1, ensure_ascii=False)
    with open(os.path.splitext(a.json)[0] + ".log", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log("→ %s / %s   %s" % (a.json, os.path.splitext(a.json)[0] + ".log",
                            "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
