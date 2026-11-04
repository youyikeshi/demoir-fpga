#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""canary_f32.py —— 零传输的 f32 跨平台一致性探针（纯 PS，不碰 PL）。

两侧跑同一份脚本（输入由同一公式现场生成，不传文件），比三类 f32 结果的 sha1：
  exp  : np.exp（ECA 注意力用的就是它；ARM NEON 版 libm vs x86 SSE 版）
  dot  : np.dot  float32 向量点积（BLAS 求和序）
  sum  : np.sum  2D 沿轴归约（SIMD 归约顺序）
同一 sha1 ⇒ 该算子逐位同实现；不同 ⇒ 就是"亚 LSB 差"的一个来源。
"""
import hashlib
import sys

import numpy as np


def sha1(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def main():
    print("[canary] numpy=%s python=%s machine=%s"
          % (np.__version__, sys.version.split()[0], np.__file__))
    # 1) exp（ECA：w = exp(x) 归一化前）
    x = (np.arange(65536, dtype=np.float32) / 65536.0 * 8.0 - 4.0).astype(np.float32)
    y = np.exp(x)
    print("[exp ] in=%s out=%s  y[1,2048,40000]=%.9g %.9g %.9g"
          % (sha1(x), sha1(y), float(y[1]), float(y[2048]), float(y[40000])))
    # 2) dot（float32 BLAS）
    a = (np.arange(4096, dtype=np.float32) / 4096.0 - 0.5).astype(np.float32)
    b = np.cos(np.arange(4096, dtype=np.float32)).astype(np.float32)
    d = np.float32(np.dot(a, b))
    print("[dot ] a=%s b=%s dot=%.9g sha1=%s" % (sha1(a), sha1(b), float(d),
                                                 sha1(np.array([d], np.float32))))
    # 3) sum 归约（加权和 / 通道均值）
    m = (np.sin(np.arange(64 * 1024, dtype=np.float32) * 0.001)
         .astype(np.float32).reshape(64, 1024))
    s = m.sum(axis=1, dtype=np.float32)
    print("[sum ] in=%s out=%s  s[0]=%.9g s[63]=%.9g" % (sha1(m), sha1(s),
                                                         float(s[0]), float(s[63])))
    # 4) 乘加链（weighted_sum 的形态：y = sum_k w_k * v_k）
    w = np.linspace(0.0, 1.0, 48, dtype=np.float32)
    v = (np.arange(48 * 128 * 128, dtype=np.float32) / (48 * 128 * 128) - 0.5).astype(np.float32)
    v = v.reshape(48, 128, 128)
    y2 = np.zeros((128, 128), np.float32)
    for k in range(48):
        y2 += w[k] * v[k]
    print("[wsum] w=%s v=%s out=%s  min=%.9g max=%.9g"
          % (sha1(w), sha1(v), sha1(y2), float(y2.min()), float(y2.max())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
