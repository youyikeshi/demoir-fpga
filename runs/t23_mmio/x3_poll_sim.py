# -*- coding: utf-8 -*-
# ★ T-43 追加（合并 T-23 时用）：本文件 = T-23 的 `/tmp/probe_T23/sim_poll.py`（未改模型）
#   + ① 默认 PS 指向 main 的 `pynq_specialized_code/ps`（合并后代码）
#   + ② 把"首次观测到 done − 硬件置 done"的**分布**打出来（min/p50/max、=0 的次数、负的次数）
#   + ③ 多一档 bus=33.5 µs（T-39 板端"引擎忙时单次读"的实测值）
# 用法：python3 runs/t23_mmio/x3_poll_sim.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-23：轮询策略的**定量投影**（不是板端实测；参数化到"忙时单次访问 µs"）。

背景（父代理 2026-11-02 决定）：
  真正能动的软件杠杆不是"把每次访问变便宜"，而是**把读次数降下来**——
  净网路径每命令 4.22 写 + 2~7 读，其中 done 轮询占大头。

模型（刻意做得很笨，因为要的是**趋势**与**边界**，不是精度）：
  · 设备 = 一个 dict + 每次访问 `bus_us` 的**墙钟**（busy-wait）；done 在
    `t_start + T` 之后才置位（T = 解析拍数模型 × mult，用来模拟"真实比模型慢"）。
  · 驱动 = 仓库里**真的** `E1E2Engine.run_one`，只换寄存器后端。
  · 于是"每命令读几次""每命令墙钟"就是真驱动的行为，只是物理换成模型参数。

输出（每组参数）：adaptive / tight / est 的
  · 读次数/命令、写次数/命令
  · ms/命令（= T + 多花的读)
  · `dt − T` 的分布（多等了多久）与 `min(dt − T)`（必须 ≥0，否则就是"没跑完就当完成"）
"""
import json
import os
import sys
import time

PS = os.environ.get("T23_PS") or "/media/felix/Data/coding/demoir-fpga/pynq_specialized_code/ps"
sys.path.insert(0, os.path.join(PS, "pynq"))
sys.path.insert(0, os.path.join(PS, "tools"))

import numpy as np                       # noqa: E402
import bin_engine as BE                  # noqa: E402
from verify_e1e2_path import FakeE1E2Unit, P_PACK      # noqa: E402

CFG = BE.CFG


class LatDev(FakeE1E2Unit):
    """每次访问吃掉 bus_us；done 在 t_start+T 之后才置位。"""

    def __init__(self, bus_us, mult, fclk=150.0):
        FakeE1E2Unit.__init__(self)
        self.bus = bus_us * 1e-6
        self.mult = mult
        self.fclk = fclk
        self.done_at = 0.0
        self.busy = 0
        self.reg = {}
        self.nw = self.nr = 0
        self.t_start = 0.0
        self.dur = 0.0
        self.t_obs = None          # 首次观测到 done 的时刻

    def _spin(self):
        if self.bus <= 0:
            return
        t_end = time.perf_counter() + self.bus
        while time.perf_counter() < t_end:
            pass

    def _model_s(self):
        hw = int(self.reg.get(0x20, 0)) * int(self.reg.get(0x28, 0))
        cyc, left = 17, hw
        while left > 0:
            run = min(BE.E1E2_ACC_DEPTH, left)
            cyc += 9 * run + 50
            left -= run
        return cyc / (self.fclk * 1e6) * self.mult

    def write(self, off, v):
        self.nw += 1
        self._spin()
        off, v = int(off), int(v) & 0xFFFFFFFF
        if off == CFG and (v & 1):
            self.dur = self._model_s()
            self.t_start = time.perf_counter()
            self.done_at = self.t_start + self.dur
            self.done = 0
            self.busy = 1
            self.t_obs = None
            return
        self.reg[(off - CFG) & 0x3FF] = v

    def read(self, off):
        self.nr += 1
        self._spin()
        off = int(off)
        if off == CFG + 0x10:
            if self.busy and time.perf_counter() >= self.done_at:
                self.busy = 0
                self.done = 1
                self.t_obs = time.perf_counter()
            return ((self.err & 0xFF) << 8) | ((self.busy & 1) << 1) | (self.done & 1)
        return self.reg.get((off - CFG) & 0x3FF, 0)

    def _start(self):
        pass


def run(mode, bus_us, mult, fclk=150.0, ncmd=None):
    prog = BE.E1E2Program(verbose=False)
    dev = LatDev(bus_us, mult, fclk=fclk)
    core = BE.BinConvEngine(dev, pack_dir=P_PACK, verbose=False)
    core._poll_mode = "" if mode == "adaptive" else mode
    ee = BE.E1E2Engine(core, verbose=False, fclk_mhz=fclk)
    arena = BE.MemBuf(prog.size, prog.base)
    ee.load_program(buf=arena)
    rows = []
    orig = ee.run_one

    def run_one(*a, **kw):
        dev.nr = dev.nw = 0
        kw["count"] = False
        r = orig(*a, **kw)
        rows.append(dict(reads=dev.nr, writes=dev.nw, dt=r["seconds"],
                         T=dev.dur, t_obs=dev.t_obs, t_start=dev.t_start,
                         done_at=dev.done_at,
                         hw=int(dev.reg.get(0x20, 0)) * int(dev.reg.get(0x28, 0))))
        return r

    ee.run_one = run_one
    t0 = time.perf_counter()
    ee.run_all(count=False)
    wall = time.perf_counter() - t0
    n = len(rows)
    # 判据：首次观测到 done 的时刻 − 硬件真正完成的时刻（必须 ≥0）
    over = [(r["t_obs"] - r["done_at"]) for r in rows
            if r["T"] and r["t_obs"] is not None]
    # ★ T-43 X3：把"多等了多久"的分布也算出来（有多少次是 0）
    over_us = sorted(x * 1e6 for x in over)
    nzero = sum(1 for x in over_us if x <= 1e-6)
    neg = sum(1 for x in over_us if x < 0.0)
    return dict(mode=mode, bus_us=bus_us, mult=mult, n=n, wall_s=wall,
                ms_per_cmd=wall / n * 1e3,
                reads_pc=sum(r["reads"] for r in rows) / n,
                writes_pc=sum(r["writes"] for r in rows) / n,
                over_ms_p50=sorted(over)[len(over) // 2] * 1e3 if over else 0.0,
                over_ms_max=max(over) * 1e3 if over else 0.0,
                over_ms_min=min(over) * 1e3 if over else 0.0,
                over_n=len(over), over_zero=nzero, over_neg=neg,
                over_us_min=over_us[0] if over_us else 0.0,
                over_us_max=over_us[-1] if over_us else 0.0,
                over_us_p50=over_us[len(over_us)//2] if over_us else 0.0,
                pred_k=core._pred_k)


def main():
    out = []
    print("=" * 130)
    print("T-43 X3：轮询策略的 done 时刻分布（模型；bus_us = 忙时单次 MMIO 延迟，mult = 真实时长/解析模型）")
    print("口径：多等 = 首次观测到 done 的时刻 − 硬件真正置 done 的时刻，必须 >= 0")
    print("=" * 130)
    for bus_us in (25.0, 33.5, 100.0):
        for mult in (1.0, 3.0):
            print("\n-- bus=%.1f µs/次, 真实时长 = %.1f × 解析模型 --" % (bus_us, mult))
            print("%-9s %8s %8s %10s | %9s %9s %10s %10s %10s"
                  % ("mode", "写/命令", "读/命令", "ms/命令",
                     "多等min", "多等p50", "多等max", "=0次数", "负次数"))
            for mode in ("adaptive", "tight", "est"):
                r = run(mode, bus_us, mult)
                out.append(r)
                print("%-9s %8.2f %8.2f %10.3f | %9.2f %9.2f %10.2f %7d/%-4d %8d"
                      % (mode, r["writes_pc"], r["reads_pc"], r["ms_per_cmd"],
                         r["over_us_min"], r["over_us_p50"], r["over_us_max"],
                         r["over_zero"], r["over_n"], r["over_neg"]))
    print("\n" + "=" * 130)
    bad = [r for r in out if r["over_neg"] > 0]
    print("判据（不许放宽）：每一组的 `多等min` 必须 >= 0 ⇒ 没有任何一次"
          "'PL 还没跑完就判 done'。")
    print("  ⇒ %s" % ("PASS（全部 >=0）" if not bad else "FAIL: %r" % bad))
    print("\n读次数与墙钟（698 条命令）：")
    for bus_us in (25.0, 33.5, 100.0):
        for mult in (1.0, 3.0):
            a = [r for r in out if r["bus_us"] == bus_us and r["mult"] == mult and r["mode"] == "adaptive"][0]
            e = [r for r in out if r["bus_us"] == bus_us and r["mult"] == mult and r["mode"] == "est"][0]
            print("  bus=%5.1f mult=%.1f：读 %.2f → %.2f /命令，墙钟 %.3f → %.3f ms/命令"
                  "（差 %+0.3f ms ⇒ 698 条 %+0.3f s）"
                  % (bus_us, mult, a["reads_pc"], e["reads_pc"], a["ms_per_cmd"],
                     e["ms_per_cmd"], e["ms_per_cmd"] - a["ms_per_cmd"],
                     (e["ms_per_cmd"] - a["ms_per_cmd"]) * 698 / 1e3))
    with open("/tmp/T43/sim_poll_x3.json", "w") as f:
        json.dump(out, f, indent=1)
    print("\n落盘 /tmp/T43/sim_poll_x3.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
