#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[T-14 ④] 板端验证驱动（**唯一一次 load**）—— 依赖闭包可见性 + 8.26/6.50 复查。

做的事（每一条都留证）：
  ① 环境身份：`platform.machine()` / python / numpy / pynq 版本（本次要证的正是
     `armv7l + pynq 3.1 + numpy 1.21.5` 这个本机 x86 覆盖不到的形态）；
  ② 闭包落点：11 个模块的 `find_spec().origin` **必须**全部落在本驱动所在闭包目录
     （否则验的是另一份代码）；
  ③ `[DEPS]` 原样一行（跑前 probe + 跑后权威）；
  ④ 15 个闭包文件**跑前/跑后 md5**（不许被跑动）；
  ⑤ `step_kernels()`：q8/c8deq/bpack 的一次性装载**移到计时之外**（否则首跑的
     prologue/epilogue 桶含装载成本，热态门槛必然误报——2026-11-02 板端实测
     epilogue 首跑 1.640 s vs 热态 0.257 s）；
  ⑥ 256² 两臂各 1 次：numpy 臂（`BIN_BILINEAR_C=0`）与 C 臂（`BIN_BILINEAR_C=1`），
     同一进程、同一 arena、同一输入（seed=1 ⇒ 输入 sha1 必须 = cfc75bc3f15d306b）；
  ⑦ 每行的 `deps_closure_ok / kernel_deps / perf_gate / perf_warm / run_valid`。

用法（板端，走 tools/boardlock.sh）：
    # 第一次：唯一一次 download（令牌由 boardlock 签发）
    echo xilinx | sudo -S -p '' env XILINX_XRT=/usr \\
      /usr/local/share/pynq-venv/bin/python3 -u drv/deps_board_run.py --json /home/xilinx/demoir/t14_deps_board.json
    # 第二次（复用已加载 overlay，不再 download）：取 q8/epilogue 真实桶
    echo xilinx | sudo -S -p '' env BIN_PROFILE=1 XILINX_XRT=/usr \\
      /usr/local/share/pynq-venv/bin/python3 -u drv/deps_board_run.py --no-download --only C \\
        --json /home/xilinx/demoir/t14_deps_board_prof.json
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import os
import platform
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CLOSURE = os.path.abspath(os.environ.get("T14_CLOSURE",
                                         os.path.join(HERE, os.pardir)))
# ★ 闭包目录必须在 sys.path **最前面**：否则 `ps/pynq`（板上的老目录）会被先解析到，
#   于是"验的不是这份代码"——这正是 2026-11-02 事故的同款错法。
sys.path.insert(0, HERE)
sys.path.insert(0, CLOSURE)

import numpy as np                        # noqa: E402

# 15 个闭包文件（顺序与 deps_closure.txt 一致：模块名序，.py 在 .c 前）
FILES = ("bin_net.py", "bin_engine.py", "bin_ref_numpy.py", "qcache.py",
         "board_safe.py", "catpack.py", "e1e2_sim.py",
         "bpack.py", "bpack.c", "c8deq.py", "c8deq.c", "q8.py", "q8.c",
         "bilinear_c.py", "bilinear_c.c")
MODULES = ("bin_net", "bin_engine", "bin_ref_numpy", "qcache", "board_safe",
           "catpack", "e1e2_sim", "bpack", "c8deq", "q8", "bilinear_c")

BIT = "/home/xilinx/demoir/design_1_150v39.bit"
X0_SHA1_256 = "cfc75bc3f15d306b"


def md5_of(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default=BIT)
    ap.add_argument("--fclk", type=float, default=150.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--json", default="/home/xilinx/demoir/t14_deps_board.json")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--only", default="both", choices=("both", "numpy", "C"),
                    help="只跑哪条臂（profile 那次只跑 C 臂）")
    a = ap.parse_args(argv)

    import board_v39_verify as V
    import bin_engine as BE

    print("=" * 78)
    print("  T-14 ④ 板端验证：依赖闭包可见性 + 256² numpy/C 两臂")
    print("  时间 %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 78)

    # ---------------- ① 环境身份 ----------------
    print("\n[1] 环境身份")
    print("    platform.machine() = %s" % platform.machine())
    print("    platform.platform()= %s" % platform.platform())
    print("    python  = %s" % sys.version.split()[0])
    print("    numpy   = %s" % np.__version__)
    try:
        import pynq
        print("    pynq    = %s（%s）" % (pynq.__version__,
                                         os.path.dirname(pynq.__file__)))
    except Exception as ex:                                   # noqa: BLE001
        print("    pynq    = <读不到：%r>" % (ex,))
    print("    BIN_PROFILE=%r  BIN_BILINEAR_C=%r  BIN_REQUIRE_KERNELS=%r"
          % (os.environ.get("BIN_PROFILE"), os.environ.get("BIN_BILINEAR_C"),
             os.environ.get("BIN_REQUIRE_KERNELS")))
    print("    cwd = %s" % os.getcwd())
    print("    闭包目录 T14_CLOSURE = %s" % CLOSURE)
    print("    解释器 sys.executable = %s" % sys.executable)

    # ---------------- ② 闭包落点 ----------------
    print("\n[2] 闭包落点（find_spec().origin 必须都在 %s 内）" % CLOSURE)
    import importlib.util as _iu
    origins = {}
    bad = []
    for m in MODULES:
        try:
            sp = _iu.find_spec(m)
            o = getattr(sp, "origin", None)
        except Exception as ex:                               # noqa: BLE001
            o = "ERROR:%r" % (ex,)
        origins[m] = o
        inside = bool(o) and str(o).startswith(CLOSURE + os.sep)
        if not inside:
            bad.append(m)
        print("    %-14s %s  %s" % (m, "✓" if inside else "✗✗", o))
    print("    全部落在闭包内 = %s" % ("✓" if not bad else "✗✗ 越界：%s" % bad))

    # ---------------- ③ [DEPS] 一行（跑前 probe）----------------
    print("\n[3] [DEPS]（跑前 = 零副作用预测；跑后由 dump 覆盖为实测权威）")
    BE.report_deps()
    print("    sys.path 前 6 项 = %s" % (sys.path[:6],))

    # ---------------- ④ 15 文件 md5（跑前）----------------
    print("\n[4] 15 个闭包文件 md5（跑前）")
    md5_before = {}
    for f in FILES:
        p = os.path.join(CLOSURE, f)
        md5_before[f] = md5_of(p) if os.path.exists(p) else "MISSING"
        print("    %-16s %s" % (f, md5_before[f]))

    rp = V.runpoint(a.bit, a.fclk, a.slots)
    print("\n    运行点：bit_md5=%s  fclk=%s  slots=%s"
          % (rp.get("bit_md5"), rp.get("fclk_target"), rp.get("slots")))
    print("    deps_closure_ok(probe) = %s" % rp.get("deps_closure_ok"))

    # ---------------- 唯一一次 load ----------------
    ov = V.step_load(a.bit, download=not a.no_download)
    V.step_addr(ov)
    ident = V.step_identity(ov, a.fclk)
    if not ident.get("ok"):
        print("  ✗✗ 复位指纹不对 ⇒ 停手（不测数）")
        V.dump(a.json)
        return 3

    V.step_bilinear()                     # 一次性成本：单独计时，不进稳态
    V.step_kernels()                      # ★ q8/c8deq/bpack 一次性装载（必须出计时）

    arena, need = V.make_arena(256, a.slots, "t14_deps_arena")
    rng = np.random.default_rng(1)
    x0 = rng.random((3, 256, 256), dtype=np.float32)
    x0_sha = V.sha1_of(x0)
    print("    [输入自证] seed=1 (3,256,256) sha1 = %s（期望 %s）  %s"
          % (x0_sha[:16], X0_SHA1_256,
             "✓" if x0_sha.startswith(X0_SHA1_256) else "✗✗ 输入不是冻结基线"))
    del x0, rng
    gc.collect()

    arms = {"both": (False, True), "numpy": (False,), "C": (True,)}[a.only]
    rows = []
    for use_c in arms:
        ok, why = V.set_arm(use_c)
        tag = "t14_%s" % ("C" if use_c else "numpy")
        print("\n    >>> 切臂 %s：kernel_loaded=%s why=%s" % (tag, ok, why))
        if use_c and not ok:
            print("    ✗✗ C 内核没装上 ⇒ C 臂无效，停")
            return 5
        row = V.run_net(ov, 256, arena, a.slots, tag=tag)
        row["kernel_loaded"] = bool(ok)
        row["kernel_why"] = why
        rows.append(row)
        print("    >>> %-6s total=%.3f pl=%.3f ps=%.3f pro=%.3f epi=%.3f "
              "nonconv=%.3f interp=%.3f sha_ok=%s run_valid=%s perf_warm=%s"
              % (tag, row["total_s"] or 0, row["pl_s"] or 0, row["ps_s"] or 0,
                 row["prologue"] or 0, row["epilogue"] or 0, row["nonconv"] or 0,
                 (row.get("prof") or {}).get("interp", -1), row["sha_ok"],
                 row.get("run_valid"), row.get("perf_warm")))
        print("    >>> e1e2_prof = %s" % {k: round(v, 3) for k, v in
                                         (row.get("e1e2_prof") or {}).items()
                                         if isinstance(v, float)})
        _g = BE.PERF_GATE
        print("    >>> perf_gate = %s  |  实测 q8=%s s < %.3f s ，epilogue=%.3f s < %.3f s"
              % ([a.get("bucket") for a in (row.get("perf_gate") or [])],
                 (round((row.get("e1e2_prof") or {}).get("q8"), 3)
                  if (row.get("e1e2_prof") or {}).get("q8") is not None else "N/A"),
                 _g["q8_s"] * _g["ratio"], row.get("epilogue") or 0,
                 _g["epilogue_s"] * _g["ratio"]))
        deps_line = V.deps_summary()
        print("    >>> %s" % deps_line)
        row["deps_line"] = deps_line

    # ---------------- ④ 15 文件 md5（跑后）----------------
    md5_after = {}
    for f in FILES:
        p = os.path.join(CLOSURE, f)
        md5_after[f] = md5_of(p) if os.path.exists(p) else "MISSING"
    same = (md5_before == md5_after)
    print("\n[5] 15 个闭包文件 md5（跑后）与跑前一致 = %s" % ("✓" if same else "✗✗ 变了"))
    if not same:
        for f in FILES:
            if md5_before[f] != md5_after[f]:
                print("    ✗ %-16s %s → %s" % (f, md5_before[f], md5_after[f]))

    if len(rows) == 2:
        s0, s1 = rows[0]["sha1"], rows[1]["sha1"]
        print("    两臂 sha1 逐位相同 = %s" % ("✓" if s0 == s1 else "✗✗"))
        print("      numpy: %s" % (s0,))
        print("      C    : %s" % (s1,))

    out = dict(schema="t14-deps-board-run/1",
               time=time.strftime("%Y-%m-%d %H:%M:%S"),
               machine=platform.machine(), platform=platform.platform(),
               python=sys.version.split()[0], numpy=np.__version__,
               closure_dir=CLOSURE, origins=origins, origins_all_inside=not bad,
               md5_before=md5_before, md5_after=md5_after, md5_same=bool(same),
               x0_sha1=x0_sha, x0_sha1_expected=X0_SHA1_256,
               runpoint=rp, rows=rows,
               profile=os.environ.get("BIN_PROFILE"),
               bin_bilinear_c=os.environ.get("BIN_BILINEAR_C"))
    V.R["t14_board_run"] = out
    V.dump(a.json)
    print("\n    → %s（含 runpoint + 两臂行）" % a.json)
    return 0 if (same and not bad) else 6


if __name__ == "__main__":
    sys.exit(main())
