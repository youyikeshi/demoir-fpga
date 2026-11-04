#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""canary2.py —— 定点候选 f32 算子逐位对拍（输入全部"精确可表示"，两侧同一公式生成）。

测四个 ECA/SAM 用到的 f32 原语：
  exp    np.exp                     （ECA 注意力权重）
  gemv   A @ v  / np.dot            （ECA 的滑窗点积底层是 BLAS）
  ecadot 滑窗 np.dot ×100           （`_windowed_dot` 的真实形态）
  bilin  bilinear_f32 ×2 上采样      （板端 ARM C 内核 vs 主机 numpy 回落）
sha1 相同 ⇒ 该算子两侧逐位同实现；不同 ⇒ "亚 LSB 差"的来源之一。
"""
import hashlib
import sys

import numpy as np


def sha1(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def main():
    print("[canary2] numpy=%s py=%s" % (np.__version__, sys.version.split()[0]))

    # ---- exp：输入 i/4096-8，i∈[0,65536) 全部精确可表示
    i = np.arange(1 << 16, dtype=np.float64)
    x = ((i - 32768.0) / 4096.0).astype(np.float32)
    y = np.exp(x)
    print("[exp  ] in=%s out=%s  y[1000]=%.9g y[50000]=%.9g"
          % (sha1(x), sha1(y), float(y[1000]), float(y[50000])))

    # ---- gemv
    A = ((np.arange(64 * 64, dtype=np.float64).reshape(64, 64) - 2048.0) / 256.0).astype(np.float32)
    v = ((np.arange(64, dtype=np.float64) - 32.0) / 128.0).astype(np.float32)
    g = (A @ v).astype(np.float32)
    print("[gemv ] A=%s v=%s out=%s  g[5]=%.9g" % (sha1(A), sha1(v), sha1(g), float(g[5])))

    # ---- ECA 滑窗 dot（np.dot(w, v[i:i+64])，n=100）
    vm = ((np.arange(4096, dtype=np.float64) - 2048.0) / 512.0).astype(np.float32)
    w = ((np.arange(64, dtype=np.float64) - 32.0) / 64.0).astype(np.float32)
    out = np.empty(100, np.float32)
    for k in range(100):
        out[k] = np.float32(np.dot(w, vm[k:k + 64]))
    print("[ecadot] v=%s w=%s out=%s  out[7]=%.9g"
          % (sha1(vm), sha1(w), sha1(out), float(out[7])))

    # ---- bilinear ×2（板端 ARM C 内核 / 主机 numpy）
    try:
        import bin_ref_numpy as BR
        z = ((np.arange(4 * 37 * 53, dtype=np.float64) - 3000.0) / 1024.0) \
            .astype(np.float32).reshape(4, 37, 53)
        b = BR.bilinear_f32(z, scale=2)
        c = BR._bilinear_f32_numpy(z, scale=2)
        print("[bilin] in=%s out=%s  numpy_vs_c_same=%s  b[0,10,20]=%.9g"
              % (sha1(z), sha1(b), sha1(b) == sha1(c), float(b[0, 10, 20])))
        print("[bilin] C 内核是否在用 = %s" % (BR._bilinear_c_kernel() is not None,))
    except Exception as ex:                              # noqa: BLE001
        print("[bilin] 跳过：%r" % (ex,))
    return 0


if __name__ == "__main__":
    sys.exit(main())
