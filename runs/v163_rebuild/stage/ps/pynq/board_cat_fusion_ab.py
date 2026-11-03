#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""board_cat_fusion_ab.py —— S2 板端整网 A/B：`BIN_CAT_FUSION=0/1`（**不下载比特流**）。

接管已加载的 v33（`Overlay(..., download=False)`），同一输入、同一 PL、同一 fclk，
只改环境变量：

    BIN_PACK   BIN_CAT_FUSION   路径
    off        -                今天基线（f32 cat → PS quant_pack）
    e1e2       0                S1 单段直读；cat 仍 np.concatenate（S2 前的行为）
    e1e2       1                **S2**：cat 不物化，下一层 E1E2 按多段读
    e1e2       1 (+strict)      保守口径：含 f32 段的层整层回落（性能对照）

判据：sha1 == 冻结基线；报告 总 / PL / prologue / epilogue / 非卷积 / cat 桶，
以及按 cat 类别分开的 PS 时间：
    i8      = 60 个全 int8 叶子 cat（融合后完全免费）
    mixed   = 50 个含 f32 叶子 cat（融合后仍要逐段量化）
    blocked = 6 个喂 eca 的 cat（必须物化）

用法（板上）：
  /usr/local/share/pynq-venv/bin/python3 -u board_cat_fusion_ab.py --hw 256 --fclk 100
  ... --hw 64 --only e1e2/cf0,e1e2/cf1 --json /home/xilinx/demoir/cat_ab64.json
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


def set_env(pack_on, fusion, strict, qcache=False, c8impl="", q8fused=True):
    os.environ.pop("BIN_PACK", None)
    os.environ.pop("BIN_LAZY_DEQ", None)
    os.environ.pop("CATPACK", None)
    os.environ.pop("BIN_CAT_FUSION", None)
    os.environ.pop("BIN_CAT_STRICT", None)
    os.environ.pop("Q8CACHE", None)
    os.environ.pop("Q8CACHE_MB", None)
    os.environ.pop("Q8_C8_IMPL", None)
    os.environ.pop("BIN_Q8_FUSED", None)
    if pack_on:
        os.environ["BIN_PACK"] = "e1e2"
    os.environ["BIN_LAZY_DEQ"] = "1"
    os.environ["BIN_CAT_FUSION"] = "1" if fusion else "0"
    os.environ["Q8CACHE"] = "1" if qcache else "0"
    if c8impl:
        os.environ["Q8_C8_IMPL"] = str(c8impl)
    os.environ["BIN_Q8_FUSED"] = "1" if q8fused else "0"
    if strict:
        os.environ["BIN_CAT_STRICT"] = "1"


def _rss():
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1])
    except Exception:                                   # noqa: BLE001
        pass
    return 0


def run_one(ov, hw, pack_on, fusion, strict, slots, arena, x0, log,
            qcache=False, c8impl="", q8fused=True):
    set_env(pack_on, fusion, strict, qcache=qcache, c8impl=c8impl,
            q8fused=q8fused)
    arena.reset()                       # 同一块 arena 复用（region() 是 append 式）
    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    eng.load_weights()
    net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
    tag = "%-8s cf=%s%s%s%s" % ("e1e2" if pack_on else "off",
                                "1" if fusion else "0",
                                "+qc" if qcache else "",
                                "+c8:%s" % c8impl if c8impl else "",
                                "+strict" if strict else "")
    log("  ── %s ──" % tag)
    t0 = time.time()
    outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
    dur = time.time() - t0
    sh = {k: sha1(outs[k]) for k in ("out1", "out2", "out3")}
    ok = (FROZEN[hw] == (sh["out1"], sh["out2"], sh["out3"]))
    prof = st.get("prof") or {}
    row = dict(tag=tag, pack_on=bool(pack_on), fusion=bool(fusion),
               strict=bool(strict), qcache=bool(qcache), c8impl=c8impl,
               q8fused=bool(q8fused),
               total_s=st["total"], wall_s=dur, pl_s=st.get("pl"),
               ps_s=st.get("ps"), prologue=st.get("ps_prologue"),
               epilogue=st.get("ps_epilogue"), nonconv=st.get("ps_nonconv"),
               overlap=st.get("overlap"), wait=st.get("wait"),
               mmio=st.get("mmio_writes"),
               prof={k: round(float(v), 4) for k, v in prof.items()},
               cat_bucket_s=float(prof.get("cat", 0.0)),
               cat_stats=st.get("cat"),
               cat_pack_s=(st.get("cat") or {}).get("cat_pack_s"),
               cat_pack_fused_s=(st.get("cat") or {}).get("cat_pack_fused_s"),
               qcache_stats=st.get("qcache"),
               lazy_stats=st.get("lazy"), lazy_deq=st.get("lazy_deq"),
               sha1=sh, sha_ok=bool(ok), rss_kb=_rss())
    log("     总 %.3f s（墙钟 %.3f） PL %.3f | PS %.3f = prologue %.3f + "
        "epilogue %.3f + 非卷积 %.3f | overlap %.3f wait %.3f"
        % (row["total_s"], dur, row["pl_s"] or 0, row["ps_s"] or 0,
           row["prologue"] or 0, row["epilogue"] or 0, row["nonconv"] or 0,
           row["overlap"] or 0, row["wait"] or 0))
    log("     cat 桶 %.3f s | 非卷积分项 %s"
        % (row["cat_bucket_s"], json.dumps(row["prof"], ensure_ascii=False)))
    cs = row["cat_stats"] or {}
    if cs.get("ops_by_class"):
        log("     cat 分类：ops %s | cat_s %s | pack_s %s"
            % (cs["ops_by_class"],
               {k: round(v, 3) for k, v in (cs.get("cat_s") or {}).items()},
               {k: round(v, 3) for k, v in (cs.get("pack_s") or {}).items()}))
        log("     cat_s+pack_s（按类）%s | 可融合合计 %.3f s"
            % ({k: round(v, 3) for k, v in (cs.get("cat_pack_s") or {}).items()},
               cs.get("cat_pack_fused_s", 0.0)))
        _bsp = (row["pl_s"] or 0) + (row["ps_s"] or 0)
        _rec = _bsp - (row["overlap"] or 0) + (row["wait"] or 0)
        log("     口径：PL+PS 桶和 %.3f（重叠 %.3f、wait %.3f）⇒ 桶和-重叠+wait "
            "%.3f vs 总 %.3f（差 %+.3f）"
            % (_bsp, row["overlap"] or 0, row["wait"] or 0, _rec,
               row["total_s"], row["total_s"] - _rec))
        row["bucket_sum_s"] = _bsp
        row["bucket_recon_s"] = _rec
        log("     融合 %d / 回落 %d（%s）"
            % (cs.get("fused", 0), cs.get("fallback", 0),
               cs.get("fallback_reason")))
    log("     sha1 %s  %s" % (sh["out1"][:12], "OK" if ok else "**≠冻结基线**"))
    del net, eng, outs
    gc.collect()
    return row


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hw", type=int, default=256)
    ap.add_argument("--fclk", type=float, default=100.0)
    ap.add_argument("--slots", type=int, default=16)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--reps", type=int, default=1,
                    help="每档重复次数（同进程内；汇总取 min，抑制 5~6%% 抖动）")
    ap.add_argument("--bit", default=BIT)
    ap.add_argument("--json", default="")
    ap.add_argument("--only", default="",
                    help="只跑某些组合：off, e1e2/cf0, e1e2/cf1, e1e2/cf1s")
    a = ap.parse_args(argv)
    if not a.json:
        a.json = "/home/xilinx/demoir/cat_fusion_ab_%d.json" % a.hw
    lines = []

    def log(s):
        print(s, flush=True)
        lines.append(s)

    t_start = time.strftime("%Y-%m-%d %H:%M:%S")
    log("== board_cat_fusion_ab：S2 BIN_CAT_FUSION A/B（hw=%d，fclk=%.0f，"
        "不下载比特流）==" % (a.hw, a.fclk))
    ov = Overlay(a.bit, download=False)
    Clocks.fclk0_mhz = a.fclk
    time.sleep(0.4)
    log("  PL 已接管（download=False），fclk0 读回 = %g MHz" % Clocks.fclk0_mhz)
    probe = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    probe.enable_e1e2()
    log("  E1E2 probe：R_RUN_UNIT 写 4 读 4 / 写 0 读 0 通过")
    probe.disable_e1e2()
    del probe
    gc.collect()

    net0 = BN.BinNet(PACK, engine=None, H=a.hw, W=a.hw, verbose=False)
    g, _outs, _tin = net0.build_graph(np.zeros((3, a.hw, a.hw), np.float32))
    conv = [o for o in g.ops if o.kind == "conv"]
    max_in = max(o.layer.act_bytes for o in conv)
    max_out = max(o.layer.out_bytes for o in conv)
    need = (max_in + max_out + 4096) * max(2, a.slots) + (1 << 16)
    arena = BE.BinArena(need, name="cat_ab_arena")
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

    combos = [("off", False, False, False, False, ""),
              ("e1e2/cf0", True, False, False, False, ""),
              ("e1e2/cf1", True, True, False, False, ""),
              ("e1e2/cf1qc", True, True, False, True, ""),
              ("e1e2/cf1q8off", True, True, False, False, "padded"),
              ("e1e2/cf1s", True, True, True, False, "")]
    want = set(x.strip() for x in a.only.split(",") if x.strip()) or None
    rows = []
    for key, pack_on, fusion, strict, qc, c8 in combos:
        if want is not None and key not in want:
            log("  ── %s ──（跳过）" % key)
            continue
        for _rep in range(max(1, int(a.reps))):
            try:
                r = run_one(ov, a.hw, pack_on, fusion, strict, a.slots,
                            arena, x0, log, qcache=qc, c8impl=c8,
                            q8fused=("q8off" not in key))
                r["rep"] = _rep
                rows.append(r)
            except Exception as ex:                      # noqa: BLE001
                import traceback
                traceback.print_exc()
                rows.append(dict(tag=key, error=repr(ex)[:500], rep=_rep))
            gc.collect()

    ok = bool(rows) and all(r.get("sha_ok") for r in rows)
    # 每档取 total_s 最小的那一轮（同进程同输入，抖动 ~5~6%）
    best = {}
    for r in rows:
        if "error" in r:
            continue
        t = r["tag"]
        if t not in best or r["total_s"] < best[t]["total_s"]:
            best[t] = r
    log("")
    log("== 汇总（hw=%d，每档取 min of %d 轮）==" % (a.hw, max(1, int(a.reps))))
    for r in best.values():
        if "error" in r:
            log("  %-18s ERROR %s" % (r.get("tag"), r["error"]))
            continue
        cs = r.get("cat_stats") or {}
        log("  %-22s 总 %7.3f  PL+PS %7.3f  PL %5.3f  prologue %7.3f  epilogue %5.3f  "
            "非卷积 %5.3f  cat %6.3f  cat+pack %6.3f  融合 %3d/回落 %d  sha1 %s"
            % (r["tag"], r["total_s"], r.get("bucket_sum_s", 0.0),
               r["pl_s"] or 0, r["prologue"] or 0,
               r["epilogue"] or 0, r["nonconv"] or 0, r["cat_bucket_s"],
               r.get("cat_pack_fused_s", 0.0),
               cs.get("fused", 0), cs.get("fallback", 0),
               "OK" if r["sha_ok"] else "**≠**"))
    res = dict(started=t_start, finished=time.strftime("%Y-%m-%d %H:%M:%S"),
               hw=a.hw, fclk=a.fclk, slots=a.slots, seed=a.seed, reps=a.reps,
               x0_sha1=sha1(x0), frozen=FROZEN[a.hw], rows=rows,
               best=list(best.values()), ok=ok)
    json.dump(res, open(a.json, "w"), indent=1, ensure_ascii=False)
    with open(os.path.splitext(a.json)[0] + ".log", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log("→ %s / %s   %s" % (a.json, os.path.splitext(a.json)[0] + ".log",
                            "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
