#!/usr/bin/env python3
"""只跑 stage5（golden 扫掠）——**第二次也是最后一次 load**。

为什么需要它：
  第一次 load 时 stage4 逐字节不符（1862/2048），main() 在 stage4 就 fail() 了，
  stage5 根本没跑 ⇒ 拿不到最关键的 cyc/fill（RLAT）遥测。

stage5 用的是 xsim **逐字节黄金向量**（硬件 == RTL 语义的直接证据），
而且它自带 DBG_RD 遥测（cyc / cyc.term / fill.rd / cyc.fill）。
所以这里把 stage4 打桩成 no-op：0/1/2/3 照跑，stage5 拿到 5 个 golden 的完整遥测。

仍然是**一次 load**（同一个 Overlay 打桩技巧）。
"""
import os
import sys
import time

BASE = "/home/xilinx/demoir/ps/pynq"
BIT = "/home/xilinx/demoir/design_1.bit"
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
LOG = "/home/xilinx/v12_stage5.log"
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
    print(" design_1_150v12 第二次 load：只跑 stage5   " + time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 78, flush=True)

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

    def skip_stage4(e, bl, L, in_buf, in_pa, out_buf, out_pa, args, wdt):
        print("\n  [patch] 跳过 stage4（本版上它与 PS 参考不符 1862/2048；"
              "stage5 用 xsim golden，不依赖它）", flush=True)

    B.stage4 = skip_stage4

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
