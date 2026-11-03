#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-17 门禁：convfp 的**逐字节**对拍（4 层全覆盖，不采样）。

三条判据全在这里：
  A) **改动 1/2/3 的逐字节性**：同一进程内把 `rn.CONVPF_NOPAD/FUSE/GEMM_POOL`
     逐个（以及全部组合）打开，与"全关"基准比 `array_equal` + `tobytes()`。
  B) **J1（内核 vs 串行参考）**：`BIN_CONVPF_C` 的 C 内核输出 vs
     `serial_ref()`（纯 Python 逐元素、K 方向严格串行标量乘加，含 ±0 语义）。
     对象是串行参考，**不是**主机 BLAS —— 主机 BLAS 的累加序与串行不同，
     所以"与主机 BLAS 的差异"单独量化（判据 C）。
  C) **量化 C 内核 vs 主机 BLAS 的差异**（不计入判据，只为报告）。

用法：
  python3 convpf_ab.py            # 全部跑（约 1~2 分钟）
  python3 convpf_ab.py --only ab  # 只跑 A
  python3 convpf_ab.py --only j1  # 只跑 J1
"""
import argparse
import hashlib
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PSCODE = os.path.realpath(os.path.join(HERE, *[os.pardir] * 2))
sys.path.insert(0, os.path.join(PSCODE, "ps", "pynq"))

# ★ 主机门禁/计时脚本：本机不是 ARM ⇒ 加载器默认**禁用** NEON 内核（见
#   `convpf_c.py` 的平台闸门）。本脚本的判据/计时对象就是那个内核，必须强制。
os.environ.setdefault("BIN_CONVPF_C_FORCE", "1")

import bin_ref_numpy as rn                                  # noqa: E402

PACK = os.path.join(PSCODE, "deploy", "bin_weights_new")
LAYERS = [("encoder.conv_first.0", 2), ("decoder.decoder_3.conv", 1),
          ("decoder.decoder_2.conv", 1), ("decoder.decoder_1.conv", 1)]
# 256² 运行点下各层输入分辨率（= 图里的真实形状）
HWS = {"encoder.conv_first.0": (128, 128), "decoder.decoder_3.conv": (32, 32),
       "decoder.decoder_2.conv": (64, 64), "decoder.decoder_1.conv": (128, 128)}


def sha(a):
    return hashlib.sha1(np.ascontiguousarray(a, np.float32).tobytes()).hexdigest()[:12]


def neq(a, b):
    """返回 (是否逐字节相同, 首个不同下标, 不同元素个数)。"""
    A = np.ascontiguousarray(a, np.float32)
    B = np.ascontiguousarray(b, np.float32)
    if A.shape != B.shape:
        return False, -2, A.size
    if A.tobytes() == B.tobytes():
        return True, -1, 0
    d = np.flatnonzero(A.ravel() != B.ravel())
    return False, int(d[0]) if d.size else -1, int(d.size)


def set_flags(nopad=False, fuse=False, pool=False, c=False):
    rn.CONVPF_NOPAD = bool(nopad)
    rn.CONVPF_FUSE = bool(fuse)
    rn.GEMM_POOL = bool(pool)
    rn.CONVPF_C = bool(c)


def make_inputs(scale=1.0):
    rng = np.random.default_rng(20261102)
    blob = rn.Blob(PACK)
    out = []
    for name, pad in LAYERS:
        w, bias = blob.fp_layer(name)
        H, W = HWS[name]
        x = (rng.standard_normal((w.shape[1], H, W)) * scale).astype(np.float32)
        out.append((name, pad, x, w, bias))
    return out


def part_a(inputs):
    print("\n== A) 改动 1/2/3 的逐字节性（4 层全覆盖）==")
    set_flags()
    base = [rn.conv2d_f32(x, w, b, pad=p) for (_n, p, x, w, b) in inputs]
    print("  基准（全关）sha1：" + " ".join(sha(y) for y in base))
    combos = [(n, f, pl)
              for n in (False, True) for f in (False, True) for pl in (False, True)]
    ok = True
    for (n, f, pl) in combos[1:]:
        set_flags(n, f, pl)
        tag = "NOPAD=%d FUSE=%d POOL=%d" % (n, f, pl)
        same, i0, nd = True, -1, 0
        shas = []
        for k, (name, p, x, w, b) in enumerate(inputs):
            y = rn.conv2d_f32(x, w, b, pad=p)
            shas.append(sha(y))
            s, a0, an = neq(base[k], y)
            if not s:
                same = False
                i0, nd = a0, an
                print("    ✗ %-26s %s 首差@%d n=%d" % (name, tag, a0, an))
        print("    %s %-28s sha 一致=%s"
              % ("✓" if same else "✗", tag,
                 shas == [sha(y) for y in base]))
        ok = ok and same
    set_flags()
    print("  => A %s" % ("PASS" if ok else "FAIL"))
    return ok


def serial_ref(x, w, b, pad):
    """**J1 的判据对象**：每个输出元素沿 K 严格串行标量乘加（无 FMA、无重排）。

    K 次序 = (ci, ky, kx) 字典序（= `w.reshape(cout,cin*k*k)` 的行内次序）；
    acc 从 +0.0 起，累计**全部** K 项（越界项取 +0.0，与 np.pad 一致）；
    最后再 `acc + bias`（与 `y = dot; y + bias` 同序）。
    """
    x = np.asarray(x, np.float32)
    w = np.asarray(w, np.float32)
    cin, H, W = x.shape
    cout, _c, k, _k = w.shape
    Ho, Wo = H + 2 * pad - k + 1, W + 2 * pad - k + 1
    xp = np.pad(x, ((0, 0), (pad, pad), (pad, pad)))
    w2 = w.reshape(cout, cin * k * k)
    out = np.empty((cout, Ho, Wo), np.float32)
    for co in range(cout):
        for ho in range(Ho):
            for wo in range(Wo):
                acc = np.float32(0.0)
                q = 0
                for ci in range(cin):
                    for ky in range(k):
                        row = xp[ci, ho + ky]
                        for kx in range(k):
                            acc = np.float32(acc + np.float32(
                                w2[co, q] * row[wo + kx]))
                            q += 1
                out[co, ho, wo] = (acc if b is None
                                   else np.float32(acc + np.float32(b[co])))
    return out


def part_j1():
    """J1：C 内核 vs 串行参考，**4 层全覆盖**（用真实权重 + 真实形状，但只在
    小块上跑串行参考，因为纯 Python 串行是 O(cout*Ho*Wo*K) 次标量运算）。
    做法：同一层、同一权重，取真实形状的一个**子块**（H,W 缩小）——
    形状变了仍覆盖 k=3/k=5、pad=1/2、cin=12/64、cout=12/48 的全部组合。
    """
    print("\n== J1) C 内核 vs 串行参考（逐字节 0 差；k/pad/cin/cout 全覆盖）==")
    st = rn.convpf_c_status()
    print("  convpf_c 状态：", st)
    if not st.get("ok"):
        print("  ✗ C 内核不可用 ⇒ J1 无法判定（见上面的 why）")
        return False, None
    blob = rn.Blob(PACK)
    ok = True
    rows = []
    for name, pad in LAYERS:
        w, bias = blob.fp_layer(name)
        cout, cin, k, _ = w.shape
        H = max(8, k + 2 * pad + 4)
        W = max(8, k + 2 * pad + 5)
        rng = np.random.default_rng(hash(name) & 0xffff)
        # 真实权重、随机激活，量级贴近真实（真实激活 |x|<~3）
        x = (rng.standard_normal((cin, H, W)) * 1.5).astype(np.float32)
        ref = serial_ref(x, w, bias, pad)
        set_flags()
        rn.CONVPF_C = True
        got = rn.conv2d_f32(x, w, bias, pad=pad)
        rn.CONVPF_C = False
        same, i0, nd = neq(ref, got)
        rows.append(dict(name=name, shape=list(ref.shape), sha_ref=sha(ref),
                         sha_c=sha(got), equal=same, first=i0, ndiff=nd))
        print("    %s %-26s %-14s k=%d pad=%d cin=%d cout=%d sha_ref=%s sha_c=%s%s"
              % ("✓" if same else "✗", name, ref.shape, k, pad, cin, cout,
                 sha(ref), sha(got), "" if same else " 首差@%d n=%d" % (i0, nd)))
        ok = ok and same
    set_flags()

    # C：量化"内核 vs 主机 BLAS"的差异（不计入判据）
    print("\n== C) 量化：C 内核 vs 主机 BLAS（numpy `@`）—— 仅供参考，不是判据 ==")
    import bin_ref_numpy as _rn
    for name, pad in LAYERS:
        w, bias = blob.fp_layer(name)
        H, W = HWS[name]
        rng = np.random.default_rng(7)
        x = (rng.standard_normal((w.shape[1], H, W)) * 1.5).astype(np.float32)
        yb = _rn.conv2d_f32(x, w, bias, pad=pad)          # BLAS 路径
        _rn.CONVPF_C = True
        yc = _rn.conv2d_f32(x, w, bias, pad=pad)
        _rn.CONVPF_C = False
        same, i0, nd = neq(yb, yc)
        ulp = np.abs(yb.astype(np.float64) - yc.astype(np.float64))
        rel = float((ulp / np.maximum(np.abs(yb), 1e-30)).max())
        print("    %-26s equal=%-5s n_diff=%-7d/%d  最大绝对差=%.3e  最大相对差=%.2e"
              % (name, same, nd, yb.size, float(ulp.max()), rel))
    print("  => J1 %s" % ("PASS" if ok else "FAIL"))
    return ok, rows


def part_j1b():
    """J1b：**全画幅**（256² 运行点的真实形状、逐像素）`convpf_direct` vs
    `convpf_direct_s`。J1 用小块对的是 Python 串行参考（慢），这里用 C 标量
    把整幅图跑满 —— 于是"边界 tile / 行尾尾块 / 每一条取数路径"全都覆盖到。
    证据链：Python 串行参考 ==(J1,小块)== C 标量 ==(J1b,全幅)== C 向量。
    """
    print("\n== J1b) 全画幅（256² 真实形状、逐像素）：C 向量 vs C 标量 ==")
    import convpf_c
    st = convpf_c.state()
    if not st.get("ok"):
        print("  ✗ C 内核不可用")
        return False
    blob = rn.Blob(PACK)
    ok = True
    for name, pad in LAYERS:
        w, bias = blob.fp_layer(name)
        cout, cin, k, _ = w.shape
        H, W = HWS[name]
        Ho, Wo = H + 2 * pad - k + 1, W + 2 * pad - k + 1
        rng = np.random.default_rng(99)
        x = (rng.standard_normal((cin, H, W)) * 1.5).astype(np.float32)
        b = np.ascontiguousarray(bias, np.float32)
        yv = convpf_c._run(st["lib"] if "lib" in st else convpf_c._init()["lib"],
                           "convpf_direct", x, w, b, pad, Ho, Wo)
        ys = convpf_c._run(convpf_c._init()["lib"], "convpf_direct_s",
                           x, w, b, pad, Ho, Wo)
        same, i0, nd = neq(ys, yv)
        print("    %s %-26s %-14s 像素=%d  sha=%s"
              % ("✓" if same else "✗", name, yv.shape, yv.size, sha(yv)))
        if not same:
            print("        首差@%d n=%d" % (i0, nd))
        ok = ok and same
    print("  => J1b %s" % ("PASS" if ok else "FAIL"))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="all", choices=("all", "ab", "j1"))
    a = ap.parse_args()
    inputs = make_inputs()
    res = {}
    oka = okj = okjb = None
    if a.only in ("all", "ab"):
        oka = part_a(inputs)
    if a.only in ("all", "j1"):
        okj, rows = part_j1()
        res["j1"] = rows
        okjb = part_j1b()
    res.update(a=oka, j1=okj, j1b=okjb)
    good = all(v is not False for v in (oka, okj, okjb))
    out = os.path.join(HERE, "_t17_gate.json")
    json.dump(res, open(out, "w"), indent=1)
    def fmt(v):
        return "N/A" if v is None else ("PASS" if v else "FAIL")
    print("\n== 总判：A=%s J1=%s J1b=%s（明细 %s）=="
          % (fmt(oka), fmt(okj), fmt(okjb), out))
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
