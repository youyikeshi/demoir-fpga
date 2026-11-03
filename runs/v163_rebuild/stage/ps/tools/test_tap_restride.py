#!/usr/bin/env python3
"""test_tap_restride.py —— 权重容器 TAP 从 ``k²+3`` 改成 ``8·ceil(k²/8)`` 的数值验证

不需要 RTL / 板子 / Vivado，纯 python3 + numpy。

背景（2026-10-30 时序优化）：``conv_int8_uni`` 的权重容器每 (o,c) 占 TAP 字节，

    地址 = base_wgt + ((o*cin + c)*TAP + j),   j = ky*k + kx,  只用 j < k²

    TAP = k²+3      ∈ {4, 12, 28}   （旧）
    TAP'= 8·ceil(k²/8) ∈ {8, 16, 32}（新；每块 8 字节整数倍 ⇒ 读地址纯 +8 累加）

检查项：
  T1  两处 Python 真相源（ps/pynq/uni_engine.py / pl/rtl_hand/tools/gen_uni_golden.py）
      的 TAP 表一致且等于公式；旧表仍是 k²+3
  T2  新布局寻址：addr(o,c,j) 能回读到正确权重；地址唯一、不越界、覆盖全
  T3  旧→新 repack **逐字节等价**（j < k² 的字节完全相同），新容器补齐洞全 0
  T4  8 对齐性质：(o,c) 块首地址低 3 位恒 0；块内是 TAP'/8 个 64-bit 字
  T5  字节总量：抽样 (k, cin, cout) 与二值网真实 9 层，旧 vs 新
  T6  gen_uni_golden.py 实跑（新/旧两个临时目录）：weight.bin 尺寸、payload 逐字节、
      expected_c8.bin 完全一致；CLI 的 --tap/--out 生效

用法：
    python3 ps/tools/test_tap_restride.py          # 全部检查，通过 exit 0
"""
import json
import os
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.dirname(HERE)                       # .../ps
ROOT = os.path.dirname(PS)                       # .../pynq_specialized_code
PYNQ = os.path.join(PS, "pynq")
TOOLS = os.path.join(ROOT, "pl", "rtl_hand", "tools")
sys.path.insert(0, PYNQ)
sys.path.insert(0, TOOLS)

import uni_engine as U        # noqa: E402  （PS 侧驱动：cfg_tap_stride 的真相源）
import gen_uni_golden as G    # noqa: E402  （golden/权重打包的真相源）

KS = (1, 3, 5)
CINS = (1, 8, 12, 16, 48, 192)
COUTS = (8, 12, 16, 48, 64)

FAILS = []
NCHECK = 0


def check(cond, msg):
    global NCHECK
    NCHECK += 1
    if not cond:
        FAILS.append(msg)
        print("  ✗ %s" % msg)
    return cond


def tap(k, padded):
    """用 golden 生成器（= 打包侧真相源）算 TAP；另外核对 PS 侧驱动给出同一个值。"""
    v = G.tap_stride(k, padded)
    assert U.tap_of_k(k, padded) == v, "TAP 表在 ps 与 tools 之间漂移了"
    return v


def pack(w3, k, padded):
    """(cout,cin,k*k) int8 → (cout,cin,TAP) 容器；补齐洞填 0。"""
    cout, cin, npix = w3.shape
    assert npix == k * k
    T = tap(k, padded)
    out = np.zeros((cout, cin, T), np.int8)
    out[:, :, :npix] = w3
    return out


def addrs(cin, cout, k, padded):
    """返回 (cout*cin, k²) 的地址矩阵：(o*cin+c)*TAP + j。"""
    T = tap(k, padded)
    oc = np.arange(cout * cin, dtype=np.int64)
    j = np.arange(k * k, dtype=np.int64)
    return oc[:, None] * T + j[None, :]


def random_w(cout, cin, k, seed):
    rng = np.random.default_rng(seed)
    return rng.integers(-128, 128, size=(cout, cin, k * k), dtype=np.int16).astype(np.int8)


# ---------------------------------------------------------------- T1 真相源
def t1_sources():
    print("[T1] TAP 表与公式")
    check(U.TAP_OF_K == G.TAP_OF_K, "TAP_OF_K 两边不一致: %r vs %r" % (U.TAP_OF_K, G.TAP_OF_K))
    check(U.TAP_OF_K_LEGACY == G.TAP_OF_K_LEGACY,
          "TAP_OF_K_LEGACY 两边不一致: %r vs %r" % (U.TAP_OF_K_LEGACY, G.TAP_OF_K_LEGACY))
    for k in KS:
        new, old = tap(k, True), tap(k, False)
        check(new == 8 * ((k * k + 7) // 8), "k=%d 新 TAP=%d ≠ 8·ceil(k²/8)" % (k, new))
        check(old == k * k + 3, "k=%d 旧 TAP=%d ≠ k²+3" % (k, old))
        check(new % 8 == 0, "k=%d 新 TAP=%d 不是 8 的倍数" % (k, new))
        check(new - k * k == 7, "k=%d 新容器补齐洞应为 7 字节（实际 %d）" % (k, new - k * k))
        check(old - k * k == 3, "k=%d 旧容器补齐洞应为 3 字节（实际 %d）"
              % (k, old - k * k))
        check(new >= k * k and new > old, "k=%d 新 TAP 必须 ≥ k² 且 > 旧值" % k)
        # RTL 侧的"通道边界步长"= TAP - j0_last（j0_last = 8·(ceil(k²/8)-1)）。
        # 旧 TAP 下它恰好是 4；新 TAP 下它**恒为 8** ⇒ wl_dlast 可退化成常数 8，
        # cfg_tap_stride 彻底离开那条三级串联加法链（本次优化的实质）。
        ngrp = (k * k + 7) // 8
        check(new - 8 * (ngrp - 1) == 8,
              "k=%d 最后一步 TAP'-j0_last 应恒为 8（实际 %d）" % (k, new - 8 * (ngrp - 1)))
        print("     k=%d  ceil(k²/8)=%d  最后一步 TAP'-j0_last=%d"
              % (k, ngrp, new - 8 * (ngrp - 1)))
        print("     k=%d  k²=%2d  旧 TAP=%2d (%2d 真+%d 补)  新 TAP'=%2d (%2d 真+%d 补)"
              % (k, k * k, old, k * k, old - k * k, new, k * k, new - k * k))
    for bad in (0, 2, 4, 7):
        try:
            G.tap_stride(bad)
            check(False, "k=%r 应被拒绝" % (bad,))
        except ValueError:
            check(True, "")
    check(sorted(U.TAP_OF_K.values()) == [8, 16, 32], "新 TAP 取值表应为 {8,16,32}")
    check(sorted(U.TAP_OF_K_LEGACY.values()) == [4, 12, 28], "旧 TAP 取值表应为 {4,12,28}")


# ---------------------------------------------------------------- T2/T3/T4 寻址
def t2_addressing():
    print("[T2] 新布局寻址回读（k×cin×cout 全组合）")
    n = 0
    for k in KS:
        for cin in CINS:
            for cout in COUTS:
                w = random_w(cout, cin, k, seed=1000 * k + 10 * cin + cout)
                buf = pack(w, k, True)
                a = addrs(cin, cout, k, True)
                flat = buf.reshape(-1)
                got = flat[a]                                     # (cout*cin, k²)
                ok = np.array_equal(got, w.reshape(cout * cin, k * k))
                n += 1
                if not check(ok, "k=%d cin=%d cout=%d 新布局回读不一致" % (k, cin, cout)):
                    return
                # 地址唯一 + 不越界 + 覆盖满 payload
                T = tap(k, True)
                check(len(np.unique(a)) == cout * cin * k * k,
                      "k=%d cin=%d cout=%d 地址有碰撞" % (k, cin, cout))
                check(int(a.max()) < cout * cin * T,
                      "k=%d cin=%d cout=%d 地址越界" % (k, cin, cout))
    print("     %d 个 (k,cin,cout) 组合全部回读正确、地址无碰撞" % n)


def t3_restride():
    print("[T3] 旧容器 → 新容器 repack 逐字节等价")
    n = 0
    for k in KS:
        for cin in CINS:
            for cout in COUTS:
                w = random_w(cout, cin, k, seed=7 + 1000 * k + 10 * cin + cout)
                old = pack(w, k, False)
                new = pack(w, k, True)
                ao, an = addrs(cin, cout, k, False), addrs(cin, cout, k, True)
                fo, fn = old.reshape(-1), new.reshape(-1)
                want = w.reshape(cout * cin, k * k)
                ok = (np.array_equal(fo[ao], fn[an])
                      and np.array_equal(fn[an], want))
                n += 1
                if not check(ok, "k=%d cin=%d cout=%d 新旧布局 payload 不等价" % (k, cin, cout)):
                    return
                # 模拟"不重新量化、只改步长"的 repack：按行抄 payload
                repack = np.zeros_like(new)
                npayload = k * k
                for o in range(cout):
                    for c in range(cin):
                        repack[o, c, :npayload] = old[o, c, :npayload]
                check(np.array_equal(repack, new),
                      "k=%d cin=%d cout=%d 行拷贝 repack ≠ 直接按新步长打包" % (k, cin, cout))
                # 新容器的补齐洞必须全 0（RTL 会读它）
                check(not new[:, :, k * k:].any(),
                      "k=%d cin=%d cout=%d 新容器补齐洞不是全 0" % (k, cin, cout))
    print("     %d 个组合：old_addr 读 == new_addr 读（j<k²），行拷贝 repack 逐字节相同" % n)


def t4_alignment():
    print("[T4] 8 字节对齐 / 64-bit 宽读性质")
    for k in KS:
        T = tap(k, True)
        for cin in CINS:
            for cout in COUTS:
                oc = np.arange(cout * cin, dtype=np.int64)
                base = oc * T
                check(bool(np.all(base % 8 == 0)),
                      "k=%d cin=%d cout=%d 块首地址不是 8 的倍数" % (k, cin, cout))
                # (o,c) → (o,c+1) 恒定步长 T'，且低 3 位不变
                if cin > 1:
                    step = np.diff(base[:cin])
                    check(bool(np.all(step == T)),
                          "k=%d cin=%d 同一 oc 内 c→c+1 步长不是恒 TAP" % (k, cin))
                # 64-bit 宽读：TAP'/8 个字正好覆盖一个 (o,c) 块
                w = random_w(cout, cin, k, seed=3 + 1000 * k + 10 * cin + cout)
                buf = pack(w, k, True)
                check(buf.size % 8 == 0, "k=%d cin=%d cout=%d 容器总字节不是 8 的倍数"
                      % (k, cin, cout))
                words = buf.reshape(-1).view(np.uint64)
                check(words.size == buf.size // 8, "64-bit 字拆分数量不对")
                # 反向：按 64-bit 读回再拼，必须还原 payload
                back = words.view(np.int8).reshape(buf.shape)
                check(np.array_equal(back, buf), "64-bit 宽读往返不一致")
    print("     所有 (o,c) 块首地址 ≡ 0 (mod 8)；块内 = TAP'/8 个 64-bit 字；宽读往返一致")


# ---------------------------------------------------------------- T5 字节总量
REAL_LAYERS = [
    # (名字, cin, cout, k, 份数)  ← docs/binary/04_定制int8引擎规格.md §1
    ("conv_first", 12, 48, 5, 1),
    ("decoder_1", 64, 12, 3, 1),
    ("decoder_2", 64, 12, 3, 1),
    ("decoder_3", 64, 12, 3, 1),
    ("SGRA ×5", 7, 4, 1, 5),
]


def t5_totals():
    print("[T5] 字节总量")
    print("     抽样（单份 cout·cin·TAP）：")
    print("     %-3s %-5s %-5s %10s %10s %8s" % ("k", "cin", "cout", "旧(T)", "新(T')", "比值"))
    for k in KS:
        for cin in (12, 64, 192):
            for cout in (12, 48):
                ob = cout * cin * tap(k, False)
                nb = cout * cin * tap(k, True)
                print("     %-3d %-5d %-5d %10d %10d %7.2fx"
                      % (k, cin, cout, ob, nb, nb / ob))
    print("\n     二值网真实 9 层（权重字节）：")
    print("     %-11s %5s %5s %2s %4s %12s %12s %10s %8s"
          % ("层", "cin", "cout", "k", "份数", "旧 (B)", "新 (B)", "增量 (B)", "比值"))
    tot_o = tot_n = 0
    rows = []
    for name, cin, cout, k, mult in REAL_LAYERS:
        ob = mult * cout * cin * tap(k, False)
        nb = mult * cout * cin * tap(k, True)
        tot_o += ob
        tot_n += nb
        rows.append((name, cin, cout, k, mult, ob, nb, nb - ob, nb / ob))
    for name, cin, cout, k, mult, ob, nb, d, r in rows:
        print("     %-11s %5d %5d %2d %4d %12d %12d %+10d %7.2fx"
              % (name, cin, cout, k, mult, ob, nb, d, r))
    print("     %-11s %5s %5s %2s %4s %12d %12d %+10d %7.2fx"
          % ("合计", "", "", "", "", tot_o, tot_n, tot_n - tot_o, tot_n / tot_o))
    check(tot_n == 56416, "真实 9 层新容器总字节应为 56,416（实测 %d）" % tot_n)
    # 手算：conv_first 12·48·32 = 18432
    #       decoder×3  64·12·16 = 12288 ×3 = 36864
    #       SGRA×5      7·4·8   =   224 ×5 =  1120
    #       合计 18,432 + 36,864 + 1,120 = 56,416
    print("\n     旧总 %d B，新总 %d B，多 %d B（+%.1f%%）"
          % (tot_o, tot_n, tot_n - tot_o, 100.0 * (tot_n - tot_o) / tot_o))


# ---------------------------------------------------------------- T6 golden 实跑
def t6_goldens():
    print("[T6] gen_uni_golden.py 实跑（新/旧两套布局对照）")
    tmp = tempfile.mkdtemp(prefix="tap_restride_")
    d_new = os.path.join(tmp, "gold_new")
    d_old = os.path.join(tmp, "gold_old")
    names = list(G.SHAPES)
    for n in names:
        G.gen(n, padded=True, gold_dir=d_new)
        G.gen(n, padded=False, gold_dir=d_old)
    print("  %-22s %3s %6s %10s %10s %10s" %
          ("形状", "k", "TAP", "旧 weight", "新 weight", "expected"))
    for n in names:
        sh = G.SHAPES[n]
        k, cin, cout = sh["k"], sh["cin"], sh["cout"]
        pn = os.path.join(d_new, n)
        po = os.path.join(d_old, n)
        wo = open(os.path.join(po, "weight.bin"), "rb").read()
        wn = open(os.path.join(pn, "weight.bin"), "rb").read()
        mn = json.load(open(os.path.join(pn, "meta.json")))
        mo = json.load(open(os.path.join(po, "meta.json")))
        check(len(wo) == cout * cin * tap(k, False), "%s 旧 weight.bin 尺寸错" % n)
        check(len(wn) == cout * cin * tap(k, True), "%s 新 weight.bin 尺寸错" % n)
        check(mn["tap_stride"] == tap(k, True), "%s 新 meta.tap_stride 错" % n)
        check(mo["tap_stride"] == tap(k, False), "%s 旧 meta.tap_stride 错" % n)
        check(mn["tap_padded"] == 1 and mo["tap_padded"] == 0,
              "%s meta.tap_padded 标记错" % n)
        env = open(os.path.join(pn, "shape.env")).read()
        check("TAPSTRIDE=%d" % tap(k, True) in env, "%s 新 shape.env TAPSTRIDE 错" % n)
        env = open(os.path.join(po, "shape.env")).read()
        check("TAPSTRIDE=%d" % tap(k, False) in env, "%s 旧 shape.env TAPSTRIDE 错" % n)
        # 逐行剥出 payload（j < k²）后，两套容器的权重必须逐字节相同
        npay = k * k
        po_b = np.frombuffer(wo, np.int8).reshape(cout, cin, -1)[:, :, :npay]
        pn_b = np.frombuffer(wn, np.int8).reshape(cout, cin, -1)[:, :, :npay]
        check(np.array_equal(po_b, pn_b), "%s 新旧容器 payload 逐字节不同" % n)
        # 容器补齐洞全 0
        check(not np.frombuffer(wn, np.int8).reshape(cout, cin, -1)[:, :, npay:].any(),
              "%s 新容器补齐洞不是全 0" % n)
        # 语义不变：卷积结果逐字节相同
        eo = open(os.path.join(po, "expected_c8.bin"), "rb").read()
        en = open(os.path.join(pn, "expected_c8.bin"), "rb").read()
        check(eo == en, "%s expected_c8.bin 变了（打包不应影响数值）" % n)
        print("  %-22s %3d %6d %10d %10d %10d %s"
              % (n, k, tap(k, True), len(wo), len(wn), len(en),
                 "✓" if eo == en else "✗"))
    # CLI 生效（PYTHONHASHSEED 固定，lcg 种子来自 hash(name)，否则两次权重不同）
    env = dict(os.environ, PYTHONHASHSEED="0")
    out = os.path.join(tmp, "cli")
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "gen_uni_golden.py"),
                        "--shape", "uni_k5_12x48_8x8", "--tap", "old", "--out", out],
                       env=env, capture_output=True, text=True)
    check(r.returncode == 0, "CLI --tap old 失败:\n%s" % r.stderr)
    if r.returncode == 0:
        envf = open(os.path.join(out, "uni_k5_12x48_8x8", "shape.env")).read()
        check("TAPSTRIDE=28" in envf, "CLI --tap old 没写出 TAPSTRIDE=28（实际: %s）" % envf.strip())
        check(os.path.getsize(os.path.join(out, "uni_k5_12x48_8x8", "weight.bin")) == 48 * 12 * 28,
              "CLI --tap old 的 weight.bin 尺寸不对")
        print("  CLI --tap old --out <tmp> → TAPSTRIDE=28、weight.bin 16,128 B ✓")
    print("  临时目录（可删）: %s" % tmp)


# ---------------------------------------------------------------- main
def main():
    print("=" * 78)
    print(" 权重容器 TAP 重定步长验证：%r → %r" % ({k: k * k + 3 for k in KS},
                                                  {k: 8 * ((k * k + 7) // 8) for k in KS}))
    print("=" * 78)
    t1_sources()
    t2_addressing()
    t3_restride()
    t4_alignment()
    t5_totals()
    t6_goldens()
    print("=" * 78)
    if FAILS:
        print(" 结论：✗ %d 项失败" % len(FAILS))
        for f in FAILS[:20]:
            print("   - %s" % f)
        return 1
    print(" 结论：✅ 全部 %d 项检查通过" % NCHECK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
