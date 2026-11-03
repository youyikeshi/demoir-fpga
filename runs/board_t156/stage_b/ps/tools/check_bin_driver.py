#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_bin_driver.py —— 二值卷积 PS 驱动的**本机**验证（不需要板子 / torch / vivado）

把"能在本机证明的"全部收成一条命令，交给 CI / 上板前自检：

  1. 容器自洽：index.json 与 bin_weights.bin 的偏移/尺寸/约束/sha256
  2. **参考实现 vs xsim 黄金向量**：`bin_ref_numpy.conv_rtl` 与 5 个
     `pl/rtl_hand/golden/bin_*`（RTL 仿真逐字节 PASS）逐字节比对
  3. **交叉实现**：与 `verify_bin_weights.int_forward`（另一份独立实现，
     已用于 227 层验证）在真实层 + 真实容器数据上逐字节比对
  4. 层间算子的 PS 回退：rprelu/sam/bilinear 三份 numpy 实现 vs xsim golden
  5. RPReLU 融合核对：容器里 227 层的 `.act.*` 是否就是该 conv 的 RPReLU
     （是 ⇒ 整网不需要单独调 bin_rprelu）
  6. 整网调度：`bin_net.NetPlan` 的 227 条 cin/cout 形状断言
  7. 驱动逻辑：`bin_engine --selftest`（寄存器级 RTL 模型，覆盖 6 个错误码 +
     金丝雀 + 5 个 golden 的整层执行）
  8. 分级测试流程：`bin_test_staged --fake`（6 个阶段全流程，不碰硬件）

用法：
    python3 pynq_specialized_code/ps/tools/check_bin_driver.py
退出码：0 全过；1 有失败。
"""
from __future__ import annotations

import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.dirname(HERE)
PYNQ = os.path.join(PS, "pynq")
ROOT = os.path.abspath(os.path.join(PS, os.pardir))
PACK = os.path.join(ROOT, "deploy", "bin_weights_new")
GOLD = os.path.join(ROOT, "pl", "rtl_hand", "golden")

for p in (PYNQ, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import bin_ref_numpy as R       # noqa: E402

FAIL = []


def sec(t):
    print("\n== %s ==" % t, flush=True)


def chk(cond, msg):
    print("   %s %s" % ("✓" if cond else "✗", msg), flush=True)
    if not cond:
        FAIL.append(msg)


def py(*args, **kw):
    """在本目录下跑一个子进程（保持 import 路径与本脚本一致）。"""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((PYNQ, HERE)))
    r = subprocess.run([sys.executable] + list(args), cwd=PYNQ, env=env,
                       capture_output=True, text=True, **kw)
    return r.returncode, r.stdout + r.stderr


# ----------------------------------------------------------------------------
def main():
    print("检查目标：%s" % PACK)

    sec("1) 容器自洽（偏移 / 尺寸 / 约束 / sha256）")
    problems, notes = R.audit(PACK, verbose=False)
    for n in notes:
        print("   ✓ " + n)
    for p in problems[:8]:
        print("   ✗ " + p)
    chk(not problems, "容器审计 %d 项检查全过" % (len(notes) + len(problems)))

    sec("2) 参考实现 vs xsim 黄金向量（逐字节）")
    bad = R.check_goldens(GOLD)
    chk(not bad, "5 个模块级黄金向量逐字节一致")

    sec("3) 交叉实现：与 verify_bin_weights.int_forward 对拍（真实层 + 真实容器）")
    try:
        from verify_bin_weights import int_forward, load_pack
        idx, _blob = load_pack(PACK)
        bl = R.Blob(PACK)
        rng = np.random.default_rng(7)
        picks = []
        for want in (lambda L: L["k"] == 1, lambda L: L["k"] == 3 and L["s"] == 2,
                     lambda L: L["k"] == 3 and L["d"] >= 3, lambda L: L["cout"] >= 160,
                     lambda L: L["cin"] >= 500):
            for L in bl.layers:
                if want(L) and L["idx"] not in {p["idx"] for p in picks}:
                    picks.append(L)
                    break
        n_ok = 0
        for L in picks:
            H, W = 7, 9                          # 非方形、非 2 次幂，逼出坐标/边界错
            x = rng.integers(-128, 128, (int(L["cin"]), H, W), dtype=np.int8)
            P = bl.params(L)
            exp, _d, _s = int_forward(P, L, x)
            got, _ = R.conv_rtl(int(L["cin"]), int(L["cout"]), int(L["k"]), int(L["s"]),
                                int(L["d"]), int(L["pad"]), int(L["shift"]),
                                R.binarize_pack(x, P["t_q"]), P)
            chk(int((got != exp).sum()) == 0,
                "%-46s cin=%3d cout=%3d k=%d s=%d d=%d cb=%d  %dx%d 逐字节一致"
                % (L["name"][-44:], L["cin"], L["cout"], L["k"], L["s"], L["d"],
                   L["cb"], H, W))
            n_ok += 1
        chk(n_ok >= 4, "交叉实现对拍 %d 层" % n_ok)
    except Exception as ex:                      # noqa: BLE001
        chk(False, "交叉实现不可用（%s）—— 跳过不算失败？不，算失败" % ex)

    sec("4) 层间算子的 PS 回退 vs xsim golden")
    import json
    for nm in ("rprelu_48_4x16", "rprelu_64_8x8", "rprelu_96_8x8"):
        d = os.path.join(GOLD, nm)
        m = json.load(open(os.path.join(d, "meta.json")))
        C, H, W = m["C"], m["H"], m["W"]
        ld = lambda f, dt: np.fromfile(os.path.join(d, f), dt)       # noqa: E731
        x = R.from_c8(ld("in_c8.bin", np.int8).tobytes(), C, H, W)
        y = R.rprelu_int8(x, ld("zeta.bin", "<i4"), ld("gamma.bin", "<i4"),
                          ld("slope.bin", "<i2"), m["shift"], m["slope_shift"])
        chk(np.array_equal(np.frombuffer(R.to_c8(y), np.int8), ld("expected_c8.bin", np.int8)),
            "rprelu_int8  %-16s C=%d %dx%d" % (nm, C, H, W))
    for nm in ("sam_32_8x8", "sam_64_8x8"):
        d = os.path.join(GOLD, nm)
        m = json.load(open(os.path.join(d, "meta.json")))
        C, H, W = m["C"], m["H"], m["W"]
        ld = lambda f, dt: np.fromfile(os.path.join(d, f), dt)       # noqa: E731
        f = lambda n: R.from_c8(ld(n, np.int8).tobytes(), C, H, W)   # noqa: E731
        y = R.sam_int8(f("x_c8.bin"), f("y0_c8.bin"), f("y2_c8.bin"), f("y4_c8.bin"),
                       ld("w0.bin", "<i2"), ld("w2.bin", "<i2"), ld("w4.bin", "<i2"),
                       m["shift"])
        chk(np.array_equal(np.frombuffer(R.to_c8(y), np.int8), ld("expected_c8.bin", np.int8)),
            "sam_int8     %-16s C=%d %dx%d" % (nm, C, H, W))
    d = os.path.join(GOLD, "interp_16_8x8_to_16x16")
    if os.path.isdir(d):
        m = json.load(open(os.path.join(d, "meta.json")))
        C = m.get("C", 16)
        ld = lambda f, dt: np.fromfile(os.path.join(d, f), dt)       # noqa: E731
        x = R.from_c8(ld("in_c8.bin", np.int8).tobytes(), C, 8, 8)
        y = R.bilinear_int8(x, 16, 16)
        chk(np.array_equal(np.frombuffer(R.to_c8(y), np.int8), ld("expected_c8.bin", np.int8)),
            "bilinear_int8 interp_16_8x8_to_16x16")

    sec("5) RPReLU 融合核对（决定整网要不要单独调 bin_rprelu）")
    bl = R.Blob(PACK)
    fused = 0
    for L in bl.layers:
        act = L.get("act")
        want = L["name"][:-len(".conv")] + ".act" if L["name"].endswith(".conv") else None
        if act and want and act == want:
            fused += 1
    chk(fused == len(bl.layers),
        "%d/%d 层的 `.act` 与 conv 同名配对 ⇒ RPReLU 已融进 conv 的收尾流水"
        % (fused, len(bl.layers)))

    sec("6) 整网调度：227 条形状断言")
    try:
        import bin_net as BN
        for hw in (16, 32, 64):
            plan = BN.NetPlan(PACK, hw, hw, verbose=False)
            nconv = sum(1 for o in plan.ops if o.kind == "conv")
            chk(nconv == len(bl.layers) and len(plan._used) == len(bl.layers),
                "--hw %d：%d 个 op、%d 个二值卷积全部对上容器 cin/cout"
                % (hw, len(plan.ops), nconv))
    except Exception as ex:                      # noqa: BLE001
        chk(False, "bin_net.NetPlan 失败：%s" % ex)

    sec("7) 驱动逻辑（寄存器级 RTL 模型）")
    rc, out = py("bin_engine.py", "--selftest")
    tail = [l for l in out.strip().splitlines() if l.strip()][-3:]
    for l in tail:
        print("   " + l)
    chk(rc == 0, "bin_engine --selftest 通过")

    sec("8) 分级测试流程（--fake：6 阶段全流程，不碰硬件）")
    rc, out = py("bin_test_staged.py", "--fake", "--pool-mb", "2")
    tail = [l for l in out.strip().splitlines() if l.strip()][-3:]
    for l in tail:
        print("   " + l)
    chk(rc == 0, "bin_test_staged --fake 通过")

    print("\n" + "=" * 70)
    if FAIL:
        print("有 %d 项失败：" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("全部通过 ✓（本机能证明的部分都在上面）")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
