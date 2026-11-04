#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台 `exp` 变体 canary（T-171 / 阶段 A）。

同一份脚本在两平台各跑一次：
  主机 x86_64 / num py 2.2.6 ;  板端 armv7l / numpy 1.21.5

输入**确定性公式**生成（无随机、不读文件）：
  dsA  x = (arange(N) - N/2) * 0.0137            # 题目指定的原始公式（|x| 最大 ~1370）
  dsB  x = ((arange(N) % 2000) - 1000) * 0.0137  # 同公式折叠到 |x| <= 13.7，模拟 ECA logit 量级

用法：
  python3 canary_exp.py compute <outdir>            # 本机计算 + 落 npy + 打印 sha1
  python3 canary_exp.py compare <A_dir> <B_dir>     # 逐元素比较两平台落盘结果
"""
import hashlib
import json
import os
import sys

import numpy as np

N = 200000


def datasets():
    i = np.arange(N, dtype=np.float64)
    dsA = (i - N / 2.0) * 0.0137
    dsB = ((np.arange(N) % 2000).astype(np.float64) - 1000.0) * 0.0137
    return [("dsA", dsA), ("dsB", dsB)]


def sha1_of(a):
    a = np.ascontiguousarray(a)
    return hashlib.sha1(a.tobytes()).hexdigest(), a.dtype.str, tuple(a.shape)


# ---------------------------------------------------------------- 变体
def v1(x):                      # 现状：float32 上直接 exp
    x32 = x.astype(np.float32)
    return np.exp(x32)


def v2(x):                      # f32 -> f64 -> exp -> f32
    x32 = x.astype(np.float32)
    return np.float32(np.exp(x32.astype(np.float64)))


def v3(x):                      # f64 输入 -> exp(f64) -> f32（x 本身是 f64）
    return np.float32(np.exp(x.astype(np.float64)))


def v4(x):                      # ufunc dtype 参数
    x32 = x.astype(np.float32)
    try:
        return np.exp(x32, dtype=np.float32)
    except TypeError as e:      # numpy 太老 / 该 ufunc 不支持
        return ("UNSUPPORTED: %s" % e)


# v5：自实现定点项数 exp（只用 IEEE 正确舍入的 + - * / 与精确 ldexp / rint）
LN2_HI = 6.93147180369123816490e-01
LN2_LO = 1.90821492927058770002e-10
INV_LN2 = 1.44269504088896338700e+00
_CP = []
_f = 1.0
for _k in range(0, 14):
    if _k:
        _f *= _k
    _CP.append(1.0 / _f)


def v5(x):
    """exp(x) = 2^k * P(r),  k = rint(x/ln2), r = x - k*ln2（双精度分段多项式）。

    只含 + - * / 、np.rint（精确）、np.ldexp（精确指数缩放），
    这些在 IEEE-754 双精度上都是**逐位确定**的 ⇒ 两平台同输入必同输出。
    """
    xd = np.asarray(x, np.float64)
    t = xd * INV_LN2
    k = np.rint(t)
    r = (xd - k * LN2_HI) - k * LN2_LO
    p = np.full_like(r, _CP[13])
    for c in _CP[12::-1]:
        p = p * r + c
    out = np.ldexp(p, k.astype(np.int32))
    return out.astype(np.float32)


VARIANTS = [
    ("v1", "np.exp(x32)                        [现状]", v1),
    ("v2", "float32(np.exp(x32.astype(f64)))", v2),
    ("v3", "float32(np.exp(x.astype(f64)))", v3),
    ("v4", "np.exp(x32, dtype=f32)", v4),
    ("v5", "self exp: ldexp(poly, rint(x/ln2))", v5),
]


def compute(outdir):
    os.makedirs(outdir, exist_ok=True)
    meta = {"N": N, "numpy": np.__version__, "python": sys.version.split()[0],
            "sha1_in": {}, "variants": {}, "extra": {}}
    print("# platform: numpy %s / python %s" % (np.__version__, sys.version.split()[0]))
    for dname, x in datasets():
        s_in, dt, sh = sha1_of(x)
        meta["sha1_in"][dname] = s_in
        print("\n=== %s  input  sha1=%s  dtype=%s shape=%s  min=%.6g max=%.6g"
              % (dname, s_in[:12], dt, sh, float(x.min()), float(x.max())))
        np.save(os.path.join(outdir, "%s__input_f64.npy" % dname), x)
        meta["variants"].setdefault(dname, {})
        for tag, desc, fn in VARIANTS:
            v = fn(x)
            if isinstance(v, str) or (isinstance(v, tuple) and v and isinstance(v[0], str)):
                print("  %s  %-42s  %s" % (tag, desc, v if isinstance(v, str) else v[0]))
                meta["variants"][dname][tag] = {"status": "unsupported",
                                                "detail": str(v)}
                continue
            s, dt, sh = sha1_of(v)
            np.save(os.path.join(outdir, "%s__%s.npy" % (dname, tag)), v)
            fin = np.isfinite(v)
            extra = ""
            if v.dtype == np.float32:
                extra = " max|finite|=%g" % (float(np.abs(v[fin]).max()) if fin.any() else -1)
            print("  %s  %-42s  sha1=%s  dtype=%s shape=%s n_nonfinite=%d%s"
                  % (tag, desc, s[:12], dt, sh, int((~fin).sum()), extra))
            meta["variants"][dname][tag] = {"sha1": s, "dtype": dt, "shape": list(sh),
                                            "n_nonfinite": int((~fin).sum())}
        # 额外的“纯 double exp”探针：双精度上两平台是否已经不同？
        if dname in ("dsA", "dsB"):
            e64 = np.exp(x)
            s64, dt64, sh64 = sha1_of(e64)
            np.save(os.path.join(outdir, "%s__d_exp64.npy" % dname), e64)
            print("  d   %-42s  sha1=%s  dtype=%s shape=%s"
                  % ("np.exp(x_f64)  [纯双精度探针]", s64[:12], dt64, sh64))
            meta["extra"]["%s__d_exp64" % dname] = {"sha1": s64, "dtype": dt64,
                                                    "shape": list(sh64)}
            e32d = np.exp(x.astype(np.float32).astype(np.float64))
            s32d, dt32d, sh32d = sha1_of(e32d)
            np.save(os.path.join(outdir, "%s__d_exp64_of_x32.npy" % dname), e32d)
            print("  d   %-42s  sha1=%s  dtype=%s shape=%s"
                  % ("np.exp(f64(x32))  [纯双精度探针2]", s32d[:12], dt32d, sh32d))
            meta["extra"]["%s__d_exp64_of_x32" % dname] = {"sha1": s32d, "dtype": dt32d,
                                                           "shape": list(sh32d)}
    with open(os.path.join(outdir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1, sort_keys=True)
    print("\n[meta] %s" % os.path.join(outdir, "meta.json"))


def compare(adir, bdir):
    """A = 参考（主机），B = 对照（板端）。给 max|Δ| 与不同元素数。"""
    rows = []
    names = sorted(f for f in os.listdir(adir) if f.endswith(".npy"))
    for fn in names:
        pB = os.path.join(bdir, fn)
        if not os.path.exists(pB):
            continue
        a = np.load(os.path.join(adir, fn))
        b = np.load(pB)
        sa, s1, s2 = sha1_of(a)
        sb = hashlib.sha1(np.ascontiguousarray(b).tobytes()).hexdigest()
        same = sa == sb
        if a.shape != b.shape or a.dtype != b.dtype:
            rows.append((fn, a.dtype.str, b.dtype.str, "SHAPE/DTYPE MISMATCH", -1, -1))
            continue
        left = a.astype(np.float64) - b.astype(np.float64)   # B - A（板 - 主机）
        fin = np.isfinite(left)
        ndiff = int(np.count_nonzero(np.ascontiguousarray(a) != np.ascontiguousarray(b)))
        nnanmismatch = int(np.count_nonzero(np.isnan(a) != np.isnan(b)))
        if fin.any():
            mx = float(np.abs(left[fin]).max())
        else:
            mx = float("nan")
        nfin_mismatch = int(np.count_nonzero(np.isfinite(a) != np.isfinite(b)))
        rows.append((fn, a.dtype.str, b.dtype.str, ("SAME" if same else "DIFF"),
                     mx, ndiff, nnanmismatch, nfin_mismatch,
                     int(fin.sum()), sa[:12], sb[:12]))
    print("%-34s %-8s %-8s %-4s %-12s %-8s %-6s %-6s %s"
          % ("file", "dtA", "dtB", "cmp", "max|B-A|", "n_diff", "n_nan", "n_inf", "sha1(A/B)"))
    for r in rows:
        if len(r) == 6:
            print("%-34s %-8s %-8s %s" % (r[0], r[1], r[2], r[3]))
        else:
            print("%-34s %-8s %-8s %-4s %-12.6g %-8d %-6d %-6d %s/%s"
                  % (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[9], r[10]))
    with open(os.path.join(os.path.dirname(os.path.abspath(adir)) or ".",
                           "compare_exp.txt"), "w") as f:
        for r in rows:
            f.write(repr(r) + "\n")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "compute":
        compute(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "compare":
        compare(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(2)
