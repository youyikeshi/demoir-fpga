#!/usr/bin/env python3
"""design_1_150v12 上板验证 —— **整个进程只 load 一次**。

安全背景
--------
历史上连续 4 次背靠背 Overlay() 会把板子硬挂（链路层无响应，需人工断电）。
所以这里：
  1. 只在开头调用一次 Overlay()，之后把 ``pynq.Overlay`` 打成返回**同一实例**的
     lambda ⇒ bin_test_staged.main() / bin_net.main() 内部的 Overlay() 不会二次
     重编程 PL。
  2. 显式把 fclk0 设成 125 MHz（BD 里写的是 150，设计只收敛到 125）。
  3. 每个 print 都 fsync 到 /home/xilinx/v12_one_load.log（硬挂时页缓存会丢）。

用法（板上，必须用 venv 的 python，/usr/bin/python3 没有 pydantic）::

    echo xilinx | sudo -S -p '' nohup \\
        /usr/local/share/pynq-venv/bin/python3 -u \\
        /home/xilinx/demoir/ps/pynq/board_run_v12_one_load.py \\
        >/tmp/v12_console.log 2>&1 &
"""
import os
import sys
import time

BASE = "/home/xilinx/demoir/ps/pynq"
BIT = "/home/xilinx/demoir/design_1.bit"
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
LOG = "/home/xilinx/v12_one_load.log"
FCLK = 125.0
POOL_MB = 30


class Tee:
    """stdout → 屏幕 + 日志文件，**每次写入后 fsync**（硬挂不丢日志）。"""

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


def section(t):
    print("\n" + "#" * 78)
    print("# " + t)
    print("#" * 78, flush=True)


def run(tag, fn, *a, **k):
    t0 = time.time()
    try:
        fn(*a, **k)
        print("[%s] 正常返回 (%.1f s)" % (tag, time.time() - t0), flush=True)
        return True
    except SystemExit as ex:
        print("[%s] SystemExit(%r) (%.1f s)" % (tag, ex.code, time.time() - t0),
              flush=True)
    except BaseException as ex:
        import traceback
        traceback.print_exc()
        print("[%s] 异常 %r (%.1f s)" % (tag, ex, time.time() - t0), flush=True)
    return False


def main():
    os.chdir(BASE)
    sys.path.insert(0, BASE)
    sys.stdout = Tee(LOG)
    sys.stderr = sys.stdout

    print("=" * 78)
    print(" design_1_150v12 上板验证（单次 load）  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 78, flush=True)

    import pynq
    import pynq.pl_server.embedded_device  # noqa: F401  ★ 注册 Device 子类，否则 No Devices Found
    from pynq import Clocks
    from pynq import Overlay as _Overlay

    # ---------- 唯一的 load ----------
    print("\n[LOAD] Overlay(%s)  ← 本进程唯一一次" % BIT, flush=True)
    t0 = time.time()
    ov = _Overlay(BIT)
    print("[LOAD] 完成，用时 %.3f s" % (time.time() - t0), flush=True)

    # ---------- 时钟 ----------
    Clocks.fclk0_mhz = FCLK
    time.sleep(0.3)
    print("[CLK] Clocks.fclk0_mhz 读回 = %s" % Clocks.fclk0_mhz, flush=True)

    print("[IPLIST] %s" % sorted(ov.ip_dict), flush=True)

    # ---------- 指纹（原始 MMIO 读，独立于 bin_engine 的寄存器名）----------
    try:
        ip = getattr(ov, "bin_unit_0")
        print("[FP] raw ip.read(0x10068) alpha_shift = %d  (期望 9)" % ip.read(0x10068),
              flush=True)
        print("[FP] raw ip.read(0x1006c) slope_shift = %d  (期望 8)" % ip.read(0x1006c),
              flush=True)
    except Exception as ex:
        print("[FP] 原始读失败（stage0 还会用 engine 再查一次）: %r" % (ex,), flush=True)

    # ---------- 之后就复用它 ----------
    pynq.Overlay = lambda *a, **k: ov
    print("[LOAD] pynq.Overlay 已打桩 ⇒ 后续不再重编程 PL", flush=True)

    import bin_test_staged as B
    import bin_net as N

    section("阶段 A：bin_test_staged --stage 6（0..5，含 5 个 golden 逐字节）")
    run("A", B.main, ["--stage", "6", "--bit", BIT, "--pack", PACK,
                      "--pool-mb", str(POOL_MB), "--fclk", str(FCLK)])

    section("阶段 B：整网 64x64")
    run("B", N.main, ["--board", "--bit", BIT, "--pack", PACK, "--hw", "64",
                      "--fclk", str(FCLK)])

    section("阶段 C：整网 256x256（目标分辨率）")
    run("C", N.main, ["--board", "--bit", BIT, "--pack", PACK, "--hw", "256",
                      "--fclk", str(FCLK)])

    print("\n[END] 所有阶段结束 " + time.strftime("%H:%M:%S"), flush=True)
    print("[END] 日志: " + LOG, flush=True)


if __name__ == "__main__":
    main()
