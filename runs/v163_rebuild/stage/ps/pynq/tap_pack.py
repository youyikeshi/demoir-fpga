#!/usr/bin/env python3
"""tap_pack.py —— 权重容器步长 TAP 的**唯一**打包/解包实现（纯 numpy，不要 torch）

容器地址（与 RTL `conv_int8_uni.v` / `docs/binary/04_定制int8引擎规格.md` §3.3 同一定义）：

    addr(o, c, j) = base_wgt + ((o*cin + c)*TAP + j),   j = ky*k + kx,  只用 j < k²

TAP 的两套取值（**只差补齐洞宽度，j < k² 的字节逐字节相同**）：

    ======  =============  =============
    k       TAP_LEGACY     TAP_NEW
             = k²+3         = 8·ceil(k²/8)
    ======  =============  =============
    1       4              8
    3       12             16
    5       28             32
    ======  =============  =============

2026-10-30 起 RTL 走**新** TAP（`conv_uni_axi_wrap.v` 的 `tap_expect = {1:8,3:16,5:32}`、
`conv_int8_uni.v` 的 `wgtblk = cin<<5/6/7`）。送错值时引擎启动校验直接返回
`ERR_TAP(2)`（AP_RETURN=0x0201），**不会静默算错**。

真相源（三处必须一致，漂移就会被 `ps/tools/test_tap_restride.py` T1 抓出来）：

    ps/pynq/uni_engine.py                     TAP_OF_K / TAP_OF_K_LEGACY
    pl/rtl_hand/tools/gen_uni_golden.py       TAP_OF_K / tap_stride()
    ps/pynq/tap_pack.py（本文件）              直接 import 前者的表，不另抄一份

用法：
    from tap_pack import tap_of_k, pack_layer, unpack_layer   # ps/pynq 在 sys.path 里
    w = pack_layer(w_q, k=3, tap=tap_of_k(3))     # (cout,cin,3,3) → (cout,cin,16)
    w2 = unpack_layer(w, k=3)                     # 反向，并校验补齐洞全 0
"""
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:                 # 允许 `python3 pynq/tap_pack.py` 直接跑
    sys.path.insert(0, _HERE)

import uni_engine as _U                                  # noqa: E402

# ---- TAP 表（唯一来源 = ps/pynq/uni_engine.py） ----
TAP_OF_K = dict(_U.TAP_OF_K)                             # {1: 8, 3: 16, 5: 32}
TAP_OF_K_LEGACY = dict(_U.TAP_OF_K_LEGACY)               # {1: 4, 3: 12, 5: 28}


def tap_of_k(k, padded=True):
    """TAP(k)。padded=True（默认）→ 8·ceil(k²/8)；False → k²+3。"""
    return _U.tap_of_k(int(k), padded)


def tap_of_k_choices(k):
    """(新 TAP, 旧 TAP)。"""
    return _U.tap_of_k_choices(int(k))


def is_new_tap(tap):
    """这个 TAP 值是不是新格式（8 的整数倍）。注意 4 不是、8 是。"""
    return int(tap) % 8 == 0 and int(tap) >= 8


def k_of_tap(tap):
    """TAP → k（用"能装下 k² 个抽头"反推；4/8→1, 12/16→3, 28/32→5）。"""
    tap = int(tap)
    for k in (1, 3, 5):
        if tap in tap_of_k_choices(k):
            return k
    raise ValueError("tap=%d 不是合法 TAP（合法：%r）"
                     % (tap, sorted(set(TAP_OF_K.values())
                                    | set(TAP_OF_K_LEGACY.values()))))


def check_tap(k, tap):
    """校验 (k, tap) 组合合法，返回 (is_new)。"""
    new, old = tap_of_k_choices(k)
    if int(tap) == new:
        return True
    if int(tap) == old:
        return False
    raise ValueError("tap=%r 与 k=%r 不匹配（合法值：%d 新 / %d 旧）"
                     % (tap, k, new, old))


def containers_bytes(cout, cin, k, tap):
    """一层权重的容器字节数 = cout*cin*TAP —— 这就是"TAP 正确性"的直接判据。"""
    return int(cout) * int(cin) * int(tap)


# ---------------------------------------------------------------- 打包/解包
def pack_layer(w, k=None, tap=None, padded=True):
    """(cout,cin,k,k) → (cout,cin,TAP) int8 容器；第 k² 字节之后补 0。

    k 可从 w 的形状推断；tap 缺省按 padded 取新/旧格式。
    """
    w = np.ascontiguousarray(w, dtype=np.int8)
    if w.ndim != 4 or w.shape[2] != w.shape[3]:
        raise ValueError("pack_layer 期望 (cout,cin,k,k)，收到 %r" % (w.shape,))
    kk = int(w.shape[2]) if k is None else int(k)
    if int(w.shape[2]) != kk:
        raise ValueError("pack_layer: w 的核尺寸 %d ≠ k=%d" % (w.shape[2], kk))
    tap = tap_of_k(kk, padded) if tap is None else int(tap)
    check_tap(kk, tap)
    cout, cin = int(w.shape[0]), int(w.shape[1])
    out = np.zeros((cout, cin, tap), np.int8)
    out[:, :, :kk * kk] = w.reshape(cout, cin, kk * kk)
    return out


def unpack_layer(w, k=None, tap=None, cout=None, cin=None, strict=True):
    """(cout,cin,TAP) int8 容器 → (cout,cin,k,k)。

    strict=True 时校验补齐洞**必须全 0**（抓"打包时忘了清零 / TAP 张冠李戴"）。
    `w` 可以是扁平 buffer 的视图，此时要给 cout/cin。
    """
    w = np.asarray(w, dtype=np.int8)
    if w.ndim == 1:
        if cout is None or cin is None or tap is None:
            raise ValueError("unpack_layer: 扁平输入必须给 cout/cin/tap")
        w = w.reshape(int(cout), int(cin), int(tap))
    if w.ndim != 3:
        raise ValueError("unpack_layer 期望 (cout,cin,TAP)，收到 %r" % (w.shape,))
    cout, cin, ttap = (int(w.shape[0]), int(w.shape[1]), int(w.shape[2]))
    tap = ttap if tap is None else int(tap)
    if tap != ttap:
        raise ValueError("unpack_layer: 记录的 tap=%d 与 w_shape 末维 %d 不一致"
                         % (tap, ttap))
    kk = k_of_tap(tap) if k is None else int(k)
    check_tap(kk, tap)          # (k, TAP) 必须是合法组合之一：k=3 配 12/16，不许配 8
    if kk * kk > tap:
        raise ValueError("unpack_layer: k²=%d > TAP=%d，容器装不下" % (kk * kk, tap))
    if strict and np.any(w[:, :, kk * kk:]):
        n = int(np.count_nonzero(w[:, :, kk * kk:]))
        raise ValueError("TAP=%d 容器里 %d 个补齐字节非 0（k=%d 只用前 %d 字节）"
                         "：权重包坏了或 TAP 张冠李戴" % (tap, n, kk, kk * kk))
    return np.ascontiguousarray(w[:, :, :kk * kk].reshape(cout, cin, kk, kk))


# ---------------------------------------------------------------- 自检
def _selftest():
    rng = np.random.default_rng(0)
    bad = []
    for k in (1, 3, 5):
        new, old = tap_of_k_choices(k)
        assert tap_of_k(k, True) == new and tap_of_k(k, False) == old
        assert new == 8 * ((k * k + 7) // 8) and old == k * k + 3
        for cout, cin in ((1, 1), (8, 12), (48, 12), (16, 192), (64, 224)):
            w = rng.integers(-128, 128, (cout, cin, k, k), dtype=np.int8)
            for tap in (new, old):
                c = pack_layer(w, k, tap)
                assert c.shape == (cout, cin, tap), (c.shape, tap)
                assert containers_bytes(cout, cin, k, tap) == c.size
                back = unpack_layer(c, k)
                if not np.array_equal(back, w):
                    bad.append((k, tap, cout, cin, "roundtrip"))
                # 地址公式逐字节回读：addr = (o*cin+c)*TAP + j
                for o in range(min(cout, 2)):
                    for ci in range(min(cin, 2)):
                        for ky in range(k):
                            for kx in range(k):
                                j = ky * k + kx
                                got = c.reshape(-1)[(o * cin + ci) * tap + j]
                                if got != w[o, ci, ky, kx]:
                                    bad.append((k, tap, o, ci, j))
                # 洞全 0
                assert not np.any(c[:, :, k * k:])
    # 张冠李戴必须炸
    c = pack_layer(rng.integers(-128, 128, (2, 2, 3, 3), dtype=np.int8), 3, 16)
    try:
        unpack_layer(c, k=1)          # 用 k=1 读 TAP=16 容器 → 洞里有非零/或 k²>tap
        bad.append(("tap-mismatch-not-caught",))
    except ValueError:
        pass
    return bad


if __name__ == "__main__":
    errs = _selftest()
    print("TAP 表：新 %r / 旧 %r" % (TAP_OF_K, TAP_OF_K_LEGACY))
    for k in (1, 3, 5):
        n, o = tap_of_k_choices(k)
        print("  k=%d: TAP %2d（旧）→ %2d（新）；每 (o,c) 容器 %d→%d 字节，"
              "k²=%d，补齐洞 %d→%d 字节"
              % (k, o, n, o, n, k * k, o - k * k, n - k * k))
    print("自检：%s" % ("PASS（千级断言）" if not errs else "FAIL %r" % (errs[:5],)))
    sys.exit(0 if not errs else 1)
