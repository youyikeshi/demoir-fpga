#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""比较 e2e_ref.py 产出的两份 npz：判据 np.array_equal（逐字节）。"""
import argparse
import hashlib
import sys

import numpy as np


def sha(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    a = ap.parse_args()
    b = np.load(a.before)
    c = np.load(a.after)
    ok = True
    print("键     形状            逐字节相同  sha1(before)     sha1(after)")
    for k in b.files:
        assert k in c.files, "缺键 %s" % k
        eq = bool(np.array_equal(b[k], c[k]))
        ok = ok and eq
        print("  %-5s %-14s %-9s %s  %s"
              % (k, "x".join(map(str, b[k].shape)),
                 "YES" if eq else "NO", sha(b[k])[:16], sha(c[k])[:16]))
    print("\n端到端等价: %s" % ("PASS（np.array_equal 全真）" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
