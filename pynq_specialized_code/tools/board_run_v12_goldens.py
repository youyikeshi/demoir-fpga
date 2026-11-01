#!/usr/bin/env python3
"""第三次（最后一次）load：把 stage5 的 5 个 golden 全跑完 + RLAT 尺寸扫描。

前两次 load 的教训：
  stage4 不符 1862/2048、stage5 第一个 case（1x32_32_8x8）不符 1591/2048 之后
  fail() 直接 raise ⇒ 剩下 4 个 golden 一条遥测都没打印。

本脚本做两件事：
  1. 把 bin_test_staged.fail 换成**软失败**（只打印、不抛）⇒ 5 个 golden 全部
     走完，每个都打印 cyc / cyc.term / fill.rd / cyc.fill。
  2. stage5 之后再跑一遍「同一个 k=1 cin=64 cout=64 层，尺寸 16→256」的扫描：
        RLAT ≈ Δcyc / Δfill
     把固定开销（per-start）从每线延迟里差分掉 —— 这是判「整网 1 秒」的关键数。

仍然只 load 一次。
"""
import os
import sys
import time

BASE = "/home/xilinx/demoir/ps/pynq"
BIT = "/home/xilinx/demoir/design_1.bit"
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
LOG = "/home/xilinx/v12_goldens.log"
FCLK = 125.0
POOL_MB = 30


class Tee:
    def __init__(self, path):
        self.f = open(path, "a", buffering=1)
        self.real = sys.stdout

    def write(self, s):
        try:
            self.real.write(s)
            self.real.flush()
        except Exception:
            pass
        try:
            self.f.write(s)
            self.f.flush()
            os.fsync(self.f.fileno())
        except Exception:
            pass
        return len(s)

    def flush(self):
        try:
            self.real.flush()
        except Exception:
            pass

    def isatty(self):
        return False


def main():
    os.chdir(BASE)
    sys.path.insert(0, BASE)
    sys.stdout = Tee(LOG)
    sys.stderr = sys.stdout

    print("=" * 78)
    print(" design_1_150v12 第三次 load：全 golden + RLAT 尺寸扫描  "
          + time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 78, flush=True)

    import numpy as np
    import pynq
    import pynq.pl_server.embedded_device  # noqa: F401
    from pynq import Clocks
    from pynq import Overlay as _Overlay

    print("\n[LOAD] Overlay(%s)" % BIT, flush=True)
    t0 = time.time()
    ov = _Overlay(BIT)
    print("[LOAD] 完成 %.3f s" % (time.time() - t0), flush=True)
    Clocks.fclk0_mhz = FCLK
    time.sleep(0.3)
    print("[CLK] fclk0_mhz = %s" % Clocks.fclk0_mhz, flush=True)
    try:
        ip = getattr(ov, "bin_unit_0")
        print("[FP] alpha_shift=%d slope_shift=%d" % (ip.read(0x10068), ip.read(0x1006c)),
              flush=True)
    except Exception as ex:
        print("[FP] %r" % (ex,), flush=True)
    pynq.Overlay = lambda *a, **k: ov

    import bin_test_staged as B

    # ---- 1) 软失败：不让 stage5 在第一个不符处中断 ----
    def soft_fail(stage, msg):
        print("\n  [软失败·已记录，继续跑] 阶段 %d：%s" % (stage, msg), flush=True)

    B.fail = soft_fail

    # ---- 2) stage5 之后追加尺寸扫描 ----
    def sweep(e, pool, out_buf, out_pa, args, wdt):
        print("\n" + "=" * 74)
        print("  RLAT 尺寸扫描：同一层（k=1 cin=64 cout=64 s=1 d=1 pad=0），只改 H=W")
        print("  目的：cyc/fill 在 8x8 上被 per-start 固定开销主导，"
              "必须靠差分拿到真正的每线延迟")
        print("=" * 74, flush=True)
        blob, Ld, act0, exp0, meta = B.load_golden(args.golden, "bin_1x64_16x16")
        Ld = dict(Ld)
        print("  层：%r  blob=%d B" % (Ld.get("name", "?"), len(blob)), flush=True)
        print("  %5s %12s %10s %8s %10s %10s %10s"
              % ("H=W", "cyc", "rd_words", "fill", "cyc.term", "fill.rd", "cyc.fill"),
              flush=True)
        rows = []
        rng = np.random.default_rng(7)
        for H in (16, 32, 64, 128, 256):
            L = B.BinLayer(dict(Ld, H=H, W=H, Hout=H, Wout=H), H, H)
            cb = (L.cin + 63) // 64
            n_act = cb * H * H * 8
            need = ((L.cout + 7) // 8) * H * H * 8
            if need + B.CANARY_BYTES > len(out_buf):
                ob, opa = pool.region(need + B.CANARY_BYTES, name="sw_out")
                e.add_region(ob, "sw_out")
            else:
                ob, opa = out_buf, out_pa
            gb, gpa = pool.region(len(blob) + 64, name="sw_blob")
            e.add_region(gb, "sw_blob")
            ab, apa = pool.region(n_act + 64, name="sw_act")
            e.add_region(ab, "sw_act")
            B.u8(gb)[:len(blob)] = np.frombuffer(blob, np.uint8)
            B.u8(ab)[:n_act] = rng.integers(0, 256, n_act, dtype=np.uint8)
            gb.flush()
            ab.flush()
            e.blob, e.blob_pa = gb, gpa
            est = B.estimate_cycles(L, args.fclk)
            rs = e.run(ab, ob, L=L, wdt=wdt, timeout=max(10.0, est * 4 + 5.0))
            cyc = rs.seconds * args.fclk * 1e6
            rd = rs.dbg.get("rd_words", 0)
            fl = rs.dbg.get("fill_win", 0)
            terms = L.cout * H * H * cb * L.k * L.k
            print("  %5d %12d %10d %8d %10.3f %10.3f %10.1f"
                  % (H, int(cyc + 0.5), rd, fl,
                     cyc / max(1, terms), fl / max(1, rd), cyc / max(1, fl)),
                  flush=True)
            rows.append((H, cyc, rd, fl))
        print("\n  差分（相邻尺寸）⇒ 每线的真实延迟与每线词数：", flush=True)
        for (H1, c1, r1, f1), (H2, c2, r2, f2) in zip(rows, rows[1:]):
            df = f2 - f1
            dc = c2 - c1
            if df:
                print("   %3d→%3d : Δcyc=%10d  Δfill=%6d  Δfill/Δcyc_term-free "
                      "Δcyc/Δfill=%7.1f  Δrd/Δfill=%7.3f  每 fill 覆盖 %d 像素"
                      % (H1, H2, int(dc), df, dc / df, (r2 - r1) / df,
                         (H2 * H2) // max(1, df)), flush=True)
        print("=" * 74, flush=True)

    orig_stage5 = B.stage5

    def stage5_plus(e, bl, pool, out_buf, out_pa, args, wdt):
        orig_stage5(e, bl, pool, out_buf, out_pa, args, wdt)
        try:
            sweep(e, pool, out_buf, out_pa, args, wdt)
        except BaseException as ex:
            import traceback
            traceback.print_exc()
            print("[sweep] 失败 %r" % (ex,), flush=True)

    B.stage5 = stage5_plus

    try:
        B.main(["--stage", "6", "--bit", BIT, "--pack", PACK,
                "--pool-mb", str(POOL_MB), "--fclk", str(FCLK)])
    except BaseException as ex:
        import traceback
        traceback.print_exc()
        print("[X] %r" % (ex,), flush=True)

    print("\n[END] " + time.strftime("%H:%M:%S"), flush=True)


if __name__ == "__main__":
    main()
