#!/usr/bin/env python3
"""C8 交错布局的形状回归（g++ 版 tb_engine_c8）。

与 `fuzz_shapes.py` 的关系：形状/随机数据完全同构，唯一区别是**输入按 C8 布局
编码**（用软件侧已归档的 `ps/c8/c8lib.py` 的 `to_c8(..., pad=True)` 生成），
引擎输出也按 C8 读回、再转成 CHW 与 `conv_ref_rt` 逐位比对。

所以它同时验证：
  ① 引擎内部的 C8 地址公式 == numpy 版 c8lib 的布局（读到错位就会算错）；
  ② 输出 C8 写回的位置正确（含 cout 不是 8 的倍数时不写越界）。

用法（pynq_specialized_code/pl 下）：
    /data/work/venv-demoir/bin/python scripts/fuzz_c8_shapes.py
"""
import os
import random
import struct
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
REPO = os.path.abspath(os.path.join(ROOT, "..", ".."))
TB = os.path.join(ROOT, "build", "tb_engine_c8.exe")
WORK = os.path.join(ROOT, "build", "fuzz_c8_data")
C8LIB = os.path.join(REPO, "pynq_specialized_code", "ps", "c8")

sys.path.insert(0, C8LIB)
import c8lib  # noqa: E402  （仓库里已归档的软件实现，逐位验证过 517 算子）


def write_case(d, cin, cout, k, h, w, shift, seed):
    os.makedirs(d, exist_ok=True)
    rng = random.Random(seed)
    chw = np.array([rng.randint(-128, 127) for _ in range(cin * h * w)],
                   dtype=np.int8).reshape(cin, h, w)
    r = c8lib.to_c8(chw, C8=8, pad=True)   # (Cb,H,W,8) C-contiguous；C%8 时返回 (out, info)
    c8, info = r if isinstance(r, tuple) else \
        (r, {"orig_c": cin, "padded_c": cin, "n_pad": 0, "C8": 8})
    assert info["padded_c"] == ((cin + 7) // 8) * 8
    assert np.array_equal(c8lib.from_c8(c8, orig_c=cin), chw)   # 往返无损（自检）
    with open(os.path.join(d, "input_c8.bin"), "wb") as f:
        f.write(c8.tobytes(order="C"))
    with open(os.path.join(d, "weight.bin"), "wb") as f:
        f.write(bytes(bytearray(rng.randint(-128, 127) & 0xFF
                                for _ in range(cout * cin * k * k))))
    bias = [rng.randint(-100000, 100000) for _ in range(cout)]
    with open(os.path.join(d, "bias.bin"), "wb") as f:
        f.write(struct.pack("<%di" % cout, *bias))
    with open(os.path.join(d, "meta.txt"), "w") as f:
        f.write("%d\n%d\n%d\n" % (h, w, shift))
    return info


def run_tb(datadir, cin, cout, k, s, dil, pad, relu=1):
    r = subprocess.run([TB, datadir, str(cin), str(cout), str(k), str(s),
                        str(dil), str(pad), str(relu)],
                       capture_output=True, text=True, timeout=600,
                       encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    if not os.path.exists(TB):
        print("找不到 tb_engine_c8.exe，先编译：")
        print("   g++ -std=c++14 -O2 -DC8_LAYOUT=1 -I src src/conv2d_engine.cpp "
              "src/conv_ref_rt.cpp tb/tb_engine_c8.cpp -o build/tb_engine_c8.exe")
        sys.exit(2)
    # 二进制必须比源码新（防"旧 exe 假绿"，2026-10-17 真踩过）
    srcs = [os.path.join(ROOT, "src", n) for n in
            ("conv2d_engine.cpp", "conv2d_engine.h", "conv_ref_rt.cpp",
             "conv2d_accel.h")]
    srcs.append(os.path.join(ROOT, "tb", "tb_engine_c8.cpp"))
    newest = max(os.path.getmtime(p) for p in srcs if os.path.exists(p))
    if os.path.getmtime(TB) < newest:
        print("!! tb_engine_c8.exe 比源码旧，拒绝运行。先重编（-DC8_LAYOUT=1）")
        sys.exit(2)

    rng = random.Random(20261023)
    cases = [
        # cin / cout 都不是 8 的倍数（尾部补零块）
        (12, 12, 3, 1, 1, 5, 6),
        (3, 12, 3, 1, 1, 5, 5),
        (1, 1, 1, 1, 1, 3, 4),
        (2, 95, 3, 2, 1, 7, 7),
        (113, 191, 1, 1, 1, 3, 5),
        (351, 95, 3, 1, 1, 4, 6),
        (12, 48, 5, 1, 1, 5, 5),       # conv_first 形状（k=5 d=1 span=5）
        (352, 192, 1, 1, 1, 4, 640),   # 最坏通道 + 640 宽（k=1 宽度路径）
        (16, 16, 3, 1, 1, 8, 640),     # 640 宽 + 3×3：10 块/行
        (96, 192, 3, 2, 1, 8, 8),      # 下采样、大 cout
        (8, 8, 5, 2, 1, 9, 9),
        (16, 8, 3, 2, 1, 9, 9),
        (24, 32, 3, 1, 2, 7, 7),       # d=2（k=3,pad=2,span=5）
        (24, 32, 3, 2, 2, 7, 7),
        (32, 8, 3, 1, 3, 5, 5),        # d=3（span=7，最大）
        (32, 16, 3, 2, 3, 5, 5),
        (40, 24, 5, 2, 1, 6, 7),
        (48, 40, 1, 1, 1, 2, 5),
        (56, 48, 1, 2, 1, 4, 33),      # 小图 + 多列块边界
        (144, 96, 3, 1, 1, 3, 3),
        (192, 64, 3, 1, 1, 6, 6),
        (17, 17, 5, 1, 1, 5, 5),       # cin/cout 都是 8k+1，k=5
        (7, 3, 1, 1, 1, 2, 2),         # 极小图（H/W < 块）
    ]
    ok = 0
    n = 0
    for i, (cin, cout, k, s, dil, h, w) in enumerate(cases):
        pad = (k - 1) * dil // 2
        if (k - 1) * dil != 2 * pad:
            print("[SKIP] #%02d k=%d d=%d 不满足 same padding" % (i, k, dil))
            continue
        n += 1
        ddir = os.path.join(WORK, "c%03d" % i)
        shift = rng.randint(1, 12)
        info = write_case(ddir, cin, cout, k, h, w, shift, 20000 + i)
        rc, out = run_tb(ddir, cin, cout, k, s, dil, pad)
        good = (rc == 0 and "全部一致" in out)
        ok += good
        print("[%s] #%02d cin=%d(->%d) cout=%d(->%d) k=%d s=%d d=%d p=%d %dx%d"
              % ("PASS" if good else "FAIL", i, cin, info["padded_c"], cout,
                 ((cout + 7) // 8) * 8, k, s, dil, pad, h, w))
        if not good:
            print("      ", out.strip().splitlines()[-1] if out else "无输出")

    print("==== C8 布局形状回归 %d/%d PASS ====" % (ok, n))
    sys.exit(0 if ok == n and n >= 20 else 1)


if __name__ == "__main__":
    main()
