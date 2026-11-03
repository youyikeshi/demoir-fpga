#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""perf_probe.py —— 整网 114x 性能差距的定量探针（**只读测量，不碰 RTL**）

模式：
  --sweep   形状扫掠：每层跑 3 次，记 预测拍数 / done t / dbg / 周期分解
  --net     整网 227 层，逐层记录 (pred cycles, done t, 总时间, PS 开销, dbg)
  --mem     纯搬运基准（CPU memcpy 1 MB）+ AXI-Lite 寄存器读写开销

安全：清状态文件 → 看门狗 → 单块池 → 全部走 BinGuard。
日志：每行 fsync（硬挂时页缓存会丢）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# ---------------------------------------------------------------- 日志
_LOGF = None


def log(fmt, *a):
    global _LOGF
    s = fmt % a if a else fmt
    print(s, flush=True)
    if _LOGF is not None:
        _LOGF.write(s + "\n")
        _LOGF.flush()
        os.fsync(_LOGF.fileno())


# ---------------------------------------------------------------- 单层跑
def run_timed(e, L, in_pa, out_pa, reps=3, poll=0.0, timeout=90.0):
    """配 + set_bases + start + 紧轮询 done。返回 dict（取最快一次）。"""
    best = None
    for _ in range(reps):
        t_cfg0 = time.perf_counter()
        e.config(L)
        e.set_bases(L, in_pa=in_pa, out_pa=out_pa)
        t_cfg = time.perf_counter() - t_cfg0
        e.clear_dbg()
        t0 = time.perf_counter()
        e.start()
        n = 0
        t_wall = time.perf_counter()
        while True:
            st = e._rd_reg("AP_RET")
            n += 1
            if st & 1:
                break
            if time.perf_counter() - t_wall > timeout:
                log("[probe] !! TIMEOUT %.0fs 未 done（不发第二次 start）", timeout)
                st = e._rd_reg("AP_RET")
                break
            if poll:
                time.sleep(poll)
        dt = time.perf_counter() - t0
        if best is None or dt < best["done_s"]:
            best = dict(done_s=dt, cfg_s=t_cfg, polls=n, status=st,
                        err=(st >> 8) & 0xFF, dbg=e.dbg())
    return best


def main(argv=None):
    global _LOGF
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1.bit")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--mode", default="sweep")
    ap.add_argument("--fclk", type=float, default=125.0)
    ap.add_argument("--pool-mb", type=float, default=30.0)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--log", default="/home/xilinx/perf_probe.log")
    ap.add_argument("--fake", action="store_true", help="本机 FakeUnit 自检（不碰硬件）")
    a = ap.parse_args(argv)

    _LOGF = open(a.log, "a", encoding="utf-8")

    from bin_engine import (BinConvEngine, BinLayer, RunStats, estimate_cycles)
    if a.fake:
        from bin_engine import FakeUnit, FakeArena, BinArena
        ov = FakeUnit(0x4006_0000)
        arena = FakeArena(int(a.pool_mb * (1 << 20)), base_pa=0x1800_0000, unit=ov)
        e = BinConvEngine(ov, unit_id=0, addr_map=dict(phys_addr=0x4006_0000),
                          pack_dir=a.pack)
    else:
        try:
            import pynq.pl_server.embedded_device as _emb  # noqa: F401
        except Exception:
            pass
        from pynq import Overlay
        from bin_engine import BinArena
        ov = Overlay(a.bit)
        arena = BinArena(int(a.pool_mb * (1 << 20)))
        e = BinConvEngine(ov, unit_id=0, pack_dir=a.pack)
    from bin_ref_numpy import Blob, binarize_pack, act_bytes, sat8

    log("=" * 78)
    log("[probe] %s  mode=%s fclk=%.1f", time.strftime("%F %T"), a.mode, a.fclk)

    log("[probe] overlay ok (fake=%s)", a.fake)
    log("[probe] pool %d B @ pa=%#x", arena.size, arena.pa)
    e.add_region(arena.buf, "pool")
    log("[probe] %s base=%#x", e.ip_name, e.base_pa)

    bl = Blob(a.pack)
    nbytes = os.path.getsize(os.path.join(a.pack, "bin_weights.bin"))
    blob_buf, blob_pa = arena.region(nbytes + 4096, name="blob")
    e.load_weights(buf=blob_buf)
    e.add_region(blob_buf, "blob")
    log("[probe] weights %d B @ pa=%#x", nbytes, blob_pa)

    # ---------------------------------------------------------------- mem
    if a.mode == "mem":
        src = np.random.randint(0, 255, 1 << 20, dtype=np.uint8)
        dst = np.empty_like(src)
        for _ in range(3):
            t0 = time.perf_counter()
            np.copyto(dst, src)
            t = time.perf_counter() - t0
            log("[mem] CPU memcpy 1 MiB: %.3f ms -> %.1f MB/s", t * 1e3,
                1.048576 / t)
        assert (dst == src).all()

        # AXI-Lite 寄存器往返
        t0 = time.perf_counter()
        for _ in range(2000):
            e._rd_reg("AP_RET")
        t = time.perf_counter() - t0
        log("[mem] MMIO read x2000: %.3f ms -> %.2f us/read", t * 1e3, t / 2000 * 1e6)
        t0 = time.perf_counter()
        for _ in range(2000):
            e._wr_reg("DBG_FLAG", 1)
        t = time.perf_counter() - t0
        log("[mem] MMIO write x2000: %.3f ms -> %.2f us/write", t * 1e3, t / 2000 * 1e6)
        return 0

    # ---------------------------------------------------------------- sweep
    if a.mode == "sweep":
        plan = None
        cases = [
            # k=1 / k=3，cin 小→大，H 2→32，覆盖整网的全部访问形态
            ("encoder.encoder_1.sam_blocks.0.b4.ghost_layers.0.primary_conv.conv", 8, 8),
            ("encoder.encoder_1.sam_blocks.0.b4.ghost_layers.0.cheap_operation.conv", 8, 8),
            ("encoder.encoder_1.rdb.conv_layers.0.conv", 32, 32),
            ("encoder.encoder_1.rdb.conv_layers.2.conv", 32, 32),
            ("decoder.decoder_1.rdb.conv_layers.2.conv", 32, 32),
            ("decoder.decoder_1.rdb.conv_post.conv", 32, 32),
            ("decoder.decoder_1.sam_blocks.0.b0.conv.conv", 32, 32),
            ("encoder.encoder_1.sam_blocks.0.b0.ghost_layers.2.primary_conv.conv", 32, 32),
            ("encoder.encoder_3.sam_blocks.0.b4.ghost_layers.2.primary_conv.conv", 2, 2),
            ("encoder.encoder_2.rdb.conv_layers.2.conv", 16, 16),
        ]
        # 同一层换分辨率，看拍数/访存随 H*W 的缩放
        extra = ("encoder.encoder_1.rdb.conv_layers.0.conv", [8, 16, 32])

        in_buf, in_pa = arena.region(1 << 18, name="IN")
        out_buf, out_pa = arena.region(1 << 18, name="OUT", keep_canary=True)
        e.add_region(in_buf, "IN")
        e.add_region(out_buf, "OUT")

        rows = []
        rng = np.random.default_rng(7)
        for name, H, W in cases:
            L = BinLayer(bl.layer_by_name(name), H, W)
            x_q = rng.integers(-128, 128, (L.cin, H, W), dtype=np.int16).astype(np.int8)
            P = bl.params(L.d)
            t0 = time.perf_counter()
            A = binarize_pack(x_q, P["t_q"])
            t_pack = time.perf_counter() - t0
            data = act_bytes(A)
            np.frombuffer(in_buf, np.uint8)[:len(data)] = np.frombuffer(data, np.uint8)
            in_buf.flush()
            r = run_timed(e, L, in_pa, out_pa, reps=a.reps)
            cyc, _ = estimate_cycles(L, a.fclk)
            done_cyc = r["done_s"] * a.fclk * 1e6
            d = r["dbg"]
            rows.append(dict(name=name, cin=L.cin, cout=L.cout, k=L.k,
                             H=H, W=W, pred_cyc=cyc, done_ms=r["done_s"] * 1e3,
                             done_cyc=done_cyc, ratio=done_cyc / cyc,
                             rd=d["rd_words"], fill=d["fill_win"],
                             aw=d["aw"], wb=d["w_beats"], flags=d["flags"],
                             rd_per_fill=d["rd_words"] / max(1, d["fill_win"]),
                             cyc_per_fill=done_cyc / max(1, d["fill_win"]),
                             cyc_per_rd=done_cyc / max(1, d["rd_words"]),
                             pack_ms=t_pack * 1e3,
                             rd_bw=d["fill_win"] * 64.0 / r["done_s"] / 1e6,
                             b_byte=d["rd_words"] * 8.0 / r["done_s"] / 1e6,
                             err=r["err"]))
            log("[sweep] %-62s ct=%3d k=%d %2dx%-2d pred=%8d cyc=%9.0f "
                "x%5.1f rd=%6d fill=%5d r/f=%4.1f cyc/fill=%6.1f cyc/rd=%5.1f "
                "aw=%5d wb=%7d flags=%d pack=%.2fms",
                name, L.cout, L.k, H, W, cyc, done_cyc, done_cyc / cyc,
                d["rd_words"], d["fill_win"], d["rd_words"] / max(1, d["fill_win"]),
                done_cyc / max(1, d["fill_win"]), done_cyc / max(1, d["rd_words"]),
                d["aw"], d["w_beats"], d["flags"], t_pack * 1e3)

        # 分辨率缩放
        for H in extra[1]:
            name = extra[0]
            L = BinLayer(bl.layer_by_name(name), H, H)
            x_q = rng.integers(-128, 128, (L.cin, H, H), dtype=np.int16).astype(np.int8)
            P = bl.params(L.d)
            data = act_bytes(binarize_pack(x_q, P["t_q"]))
            np.frombuffer(in_buf, np.uint8)[:len(data)] = np.frombuffer(data, np.uint8)
            in_buf.flush()
            r = run_timed(e, L, in_pa, out_pa, reps=a.reps)
            cyc, _ = estimate_cycles(L, a.fclk)
            done_cyc = r["done_s"] * a.fclk * 1e6
            d = r["dbg"]
            log("[sweep-H] %-30s %2dx%-2d pred=%8d cyc=%9.0f x%5.1f rd=%6d "
                "fill=%5d r/f=%4.1f cyc/fill=%6.1f cyc/rd=%5.1f",
                name, H, H, cyc, done_cyc, done_cyc / cyc, d["rd_words"],
                d["fill_win"], d["rd_words"] / max(1, d["fill_win"]),
                done_cyc / max(1, d["fill_win"]), done_cyc / max(1, d["rd_words"]))

        log("[sweep-json] " + json.dumps(rows))
        return 0

    # ---------------------------------------------------------------- ps
    if a.mode == "ps":
        from bin_engine import _flush, _invalidate
        from bin_ref_numpy import sat8

        def tm(fn, n=3):
            best = 1e9
            for _ in range(n):
                t = time.perf_counter()
                r = fn()
                best = min(best, time.perf_counter() - t)
            return best, r

        # sleep 粒度（wait() 的 poll=2e-4 全靠它）
        t0 = time.perf_counter()
        for _ in range(50):
            time.sleep(2e-4)
        dt = time.perf_counter() - t0
        log("[ps] time.sleep(2e-4) x50: %.1f ms -> %.2f ms/次", dt * 1e3, dt / 50 * 1e3)
        t0 = time.perf_counter()
        for _ in range(50):
            time.sleep(0)
        log("[ps] time.sleep(0) x50: %.1f ms -> %.2f ms/次", (time.perf_counter() - t0) * 1e3,
            (time.perf_counter() - t0) / 50 * 1e3)

        in_buf, in_pa = arena.region(1 << 18, name="IN")
        out_buf, out_pa = arena.region(1 << 18, name="OUT", keep_canary=True)
        e.add_region(in_buf, "IN")
        e.add_region(out_buf, "OUT")
        rng = np.random.default_rng(3)

        for name, H, W in (("decoder.decoder_1.sam_blocks.0.b0.conv.conv", 32, 32),
                           ("encoder.encoder_1.sam_blocks.0.b4.ghost_layers.0.primary_conv.conv", 8, 8)):
            L = BinLayer(bl.layer_by_name(name), H, W)
            x_f = rng.random((L.cin, H, W), dtype=np.float32)
            fs = int(L.d["act_fs_in"])
            log("[ps] ---- %s ct=%d k=%d %dx%d  元素=%d", name, L.cout, L.k, H, W,
                L.cin * H * W)
            t_q = bl.params(L.d)["t_q"]

            t_qz, x_q = tm(lambda: sat8(np.rint(np.asarray(x_f, np.float64) * (1 << fs))))
            log("[ps]   量化 float64: %.2f ms", t_qz * 1e3)
            t_qz2, _ = tm(lambda: sat8(np.rint(x_f * np.float32(1 << fs))))
            log("[ps]   量化 float32: %.2f ms  (x%.1f)", t_qz2 * 1e3, t_qz / t_qz2)
            t_par, P = tm(lambda: bl.params(L.d))
            log("[ps]   Blob.params: %.2f ms", t_par * 1e3)
            t_bin, A = tm(lambda: binarize_pack(x_q, t_q))
            log("[ps]   binarize_pack: %.2f ms  (%.0f ns/元素)", t_bin * 1e3,
                t_bin / (L.cin * H * W) * 1e9)

            # 向量化替代（np.packbits，逐块）
            def fast_pack(xq, t, cin):
                cb = (cin + 63) // 64
                hw = xq.shape[1] * xq.shape[2]
                bits = ((xq.astype(np.int16) + np.asarray(t, np.int16).reshape(cin, 1, 1)) > 0)
                bb = np.packbits(bits.reshape(cin, hw), axis=0, bitorder="little")
                bbp = np.zeros((cb * 8, hw), np.uint8)
                bbp[: bb.shape[0]] = bb
                out = np.empty((cb, hw), np.uint64)
                for b in range(cb):
                    out[b] = np.ascontiguousarray(bbp[b * 8:(b + 1) * 8].T).view(np.uint64).ravel()
                return out.reshape(cb, xq.shape[1], xq.shape[2])
            t_fast, A2 = tm(lambda: fast_pack(x_q, t_q, L.cin))
            same = (np.ascontiguousarray(A).tobytes() == np.ascontiguousarray(A2).tobytes())
            log("[ps]   fast_pack: %.2f ms  (x%.1f) 逐字节一致=%s", t_fast * 1e3,
                t_bin / t_fast, same)
            t_ab, data = tm(lambda: act_bytes(A))
            log("[ps]   act_bytes: %.2f ms", t_ab * 1e3)

            np.frombuffer(in_buf, np.uint8)[:len(data)] = np.frombuffer(data, np.uint8)
            t_fl, _ = tm(lambda: in_buf.flush())
            log("[ps]   flush(IN %d B): %.2f ms", len(data), t_fl * 1e3)

            t_cfg, _ = tm(lambda: (e.config(L), e.set_bases(L, in_pa=in_pa, out_pa=out_pa)))
            log("[ps]   config+set_bases (%d 次 MMIO 写): %.2f ms",
                15, t_cfg * 1e3)
            e.clear_dbg()
            t_st = time.perf_counter()
            e.start()
            t_start = time.perf_counter() - t_st
            n = 0
            while not (e._rd_reg("AP_RET") & 1):
                n += 1
            t_done = time.perf_counter() - t_st
            log("[ps]   start=%.3f ms  done=%.2f ms  polls=%d", t_start * 1e3,
                t_done * 1e3, n)
            t_db, d = tm(lambda: e.dbg())
            log("[ps]   dbg(3 reads): %.2f ms  %s", t_db * 1e3, d)
            t_fc = time.perf_counter()
            out_buf.invalidate()
            log("[ps]   invalidate(OUT): %.2f ms", (time.perf_counter() - t_fc) * 1e3)
            # 空转：sleep 版 wait 的代价
            t0 = time.perf_counter()
            for _ in range(3):
                time.sleep(2e-4)
            log("[ps]   3x sleep(2e-4) = %.2f ms（wait() 每次轮询的最小代价）",
                (time.perf_counter() - t0) * 1e3)
        return 0

    # ---------------------------------------------------------------- net
    if a.mode == "net":
        from bin_net import BinNet

        _orig_run = BinConvEngine.run

        def run_hook(self, *aa, **kw):
            self.clear_dbg()
            rs = _orig_run(self, *aa, **kw)
            self._last_rs = rs
            return rs
        BinConvEngine.run = run_hook

        _orig_conv = BinNet._conv

        def conv_hook(self, x_f, layer):
            t0 = time.perf_counter()
            y = _orig_conv(self, x_f, layer)
            dt = time.perf_counter() - t0
            L = layer
            H, W = x_f.shape[1], x_f.shape[2]
            BL = BinLayer(L, H, W)
            cyc, _ = estimate_cycles(BL, a.fclk)
            rs = getattr(self.engine, "_last_rs", None)
            d = rs.dbg if rs is not None else {}
            r = dict(name=L["name"], cin=BL.cin, cout=BL.cout, k=BL.k,
                     H=H, W=W, pred_cyc=cyc,
                     total_ms=dt * 1e3,
                     done_ms=(rs.seconds * 1e3) if rs else -1,
                     rd=d.get("rd_words", -1), fill=d.get("fill_win", -1),
                     aw=d.get("aw", -1), wb=d.get("w_beats", -1),
                     flags=d.get("flags", -1))
            r["ps_ms"] = r["total_ms"] - r["done_ms"]
            r["out_bytes"] = ((BL.cout + 7) // 8) * BL.Hout * BL.Wout * 8
            r["act_bytes"] = BL.cb * BL.H * BL.W * 8
            rec.append(r)
            log("[net] %-58s ct=%3d k=%d %2dx%-2d pred=%7d done=%8.3fms "
                "x%6.1f total=%8.3f ps=%7.3f rd=%6d fill=%5d r/f=%4.1f",
                r["name"], r["cout"], r["k"], H, W, cyc, r["done_ms"],
                (r["done_ms"] * 1e-3 * a.fclk * 1e6) / cyc, r["total_ms"],
                r["ps_ms"], r["rd"], r["fill"],
                (r["rd"] / r["fill"]) if r["fill"] > 0 else -1)
            return y
        BinNet._conv = conv_hook

        rec = []
        net = BinNet(a.pack, engine=e, H=64, W=64, verbose=False)
        rng = np.random.default_rng(1)
        x0 = rng.random((3, 64, 64), dtype=np.float32)
        t0 = time.perf_counter()
        outs, st = net.run(x0)
        tot = time.perf_counter() - t0
        import hashlib
        for kk in ("out1", "out2", "out3"):
            o = outs[kk]
            log("[net] %s %s sha1=%s mean=%.4f", kk, o.shape,
                hashlib.sha1(np.ascontiguousarray(o).tobytes()).hexdigest()[:12],
                float(o.mean()))
        sum_pred = sum(r["pred_cyc"] for r in rec)
        sum_done = sum(r["done_ms"] for r in rec)
        sum_tot = sum(r["total_ms"] for r in rec)
        sum_ps = sum(r["ps_ms"] for r in rec)
        log("[net] ops=%d conv=%d total=%.2fs prof=%s", len(rec), len(rec), tot,
            st.get("prof"))
        log("[net] sum_pred=%d  sum_done=%.1fms  sum_total=%.1fms  sum_ps=%.1fms",
            sum_pred, sum_done, sum_tot, sum_ps)
        log("[net] implied PL cycles=%.0f  (%.2fs @%.0fMHz)  ratio_vs_pred=%.1f",
            sum_done * 1e-3 * a.fclk * 1e6, sum_done / 1e3, a.fclk,
            (sum_done * 1e-3 * a.fclk * 1e6) / sum_pred)
        log("[net-json] " + json.dumps(rec))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
