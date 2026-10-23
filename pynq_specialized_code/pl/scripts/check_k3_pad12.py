#!/usr/bin/env python3
"""k=3 权重"12 字节容器"的**跨语言**对拍（Python 打包 ↔ C 引擎 ↔ numpy 金标准）。

为什么需要这个脚本：`fuzz_shapes.py` / `fuzz_c8_shapes.py` 喂给引擎和
`conv_ref_rt` 的是**同一串随机字节**，所以"两边一起错"是测不出来的
（把 12 字节容器写成 9 字节紧排、或把字内字节序搞反，两个 C 文件照样一致）。
本脚本用**Python 侧的打包函数**（`ref_numpy.k3_to_pad12`，导出侧 `export_network.py`
用的同一个）生成权重容器，再用 **numpy 金标准**（`conv_ref_numpy`，输入原始的
(cout,cin,3,3)）算期望输出，与 **C 引擎**（tb_engine_c8）的输出逐位比对：

    Python 打包 → C 引擎读数 → 结果 == Python 用 (cout,cin,3,3) 算的结果

任何一侧写错（字内字节序、pad 位置、oc/ic 先后）都会立刻红。

为什么需要这个脚本：`fuzz_shapes.py` / `fuzz_c8_shapes.py` 喂给引擎和
`conv_ref_rt` 的是**同一串随机字节**，所以"两边一起错"是测不出来的
（把 [ky][kx][oc][ic] 写成 [kx][ky][oc][ic]，两个 C 文件照样逐位一致）。
本脚本用 **Python 侧的打包函数**（`ref_numpy.k3_to_plane`，也是导出侧
`export_network.py` 用的那一个）生成权重容器，再用 **numpy 金标准**
（`conv_ref_numpy`，输入原始的 (cout,cin,3,3) 数组）算期望输出，与
**C 引擎**（tb_engine_c8）的输出逐位比对：

    Python 打包 → C 引擎读数 → 结果 == Python 用 (cout,cin,3,3) 算的结果

任何一侧的布局公式写错（ky/kx 换位、oc/ic 换位、平面号偏移）都会立刻红。

覆盖：cin%4==0（宽读路径）/ cin%4!=0（逐字节回退）/ cout%8!=0（尾部补零块）/
s=2 / d=2 / 640 宽多列块。

用法（pynq_specialized_code/pl 下）：
    /data/work/venv-demoir/bin/python scripts/check_k3_plane_layout.py
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
PL = os.path.join(REPO, "pynq_specialized_code", "pl")
WORK = os.path.join(ROOT, "build", "k3plane_data")
TB_C8 = os.path.join(ROOT, "build", "tb_engine_c8.exe")
TB_CHW = os.path.join(ROOT, "build", "tb_engine_c8_chw.exe")

sys.path.insert(0, os.path.join(REPO, "pynq_specialized_code", "ps", "pynq"))
sys.path.insert(0, os.path.join(REPO, "pynq_specialized_code", "ps", "c8"))
from ref_numpy import conv_ref_numpy, k3_to_pad12   # noqa: E402
import c8lib                                        # noqa: E402

# (cin, cout, k, s, d, H, W)
CASES = [
    (16, 16, 3, 1, 1, 9, 9),      # 网络里最常见的形状（108 层）；cin%4==0
    (112, 64, 3, 1, 1, 7, 7),     # decoder.preconv_1 的通道数
    (12, 12, 3, 1, 1, 5, 6),      # cin%4==0 但 cout%8!=0
    (3, 12, 3, 1, 1, 5, 5),       # cin%4!=0 → 逐字节回退路径
    (351, 95, 3, 1, 1, 4, 6),     # cin%4!=0 且接近上界
    (96, 192, 3, 2, 1, 8, 8),     # 下采样 + 大 cout
    (24, 32, 3, 1, 2, 7, 7),      # d=2（span=5）
    (16, 16, 3, 1, 1, 8, 640),    # 640 宽：10 个列块
]


def write_case(ddir, cin, cout, k, s, dil, pad, h, w, shift, seed):
    os.makedirs(ddir, exist_ok=True)
    rng = random.Random(seed)
    chw = np.array([rng.randint(-128, 127) for _ in range(cin * h * w)],
                   dtype=np.int8).reshape(cin, h, w)
    r = c8lib.to_c8(chw, C8=8, pad=True)
    c8, info = r if isinstance(r, tuple) else (r, {"padded_c": cin})
    wgt = np.array([rng.randint(-128, 127) for _ in range(cout * cin * k * k)],
                   dtype=np.int8).reshape(cout, cin, k, k)
    w_hw = k3_to_pad12(wgt) if k == 3 else wgt   # 12 字节/(oc,ic) 容器
    bias = np.array([rng.randint(-100000, 100000) for _ in range(cout)], np.int32)
    np.ascontiguousarray(chw).tofile(os.path.join(ddir, "input_chw.bin"))
    np.ascontiguousarray(c8).tofile(os.path.join(ddir, "input_c8.bin"))
    np.ascontiguousarray(w_hw).tofile(os.path.join(ddir, "weight.bin"))
    bias.tofile(os.path.join(ddir, "bias.bin"))
    with open(os.path.join(ddir, "meta.txt"), "w") as f:
        f.write("%d\n%d\n%d\n" % (h, w, shift))
    return chw, wgt, bias


def main():
    for exe in (TB_C8, TB_CHW):
        if not os.path.exists(exe):
            print("找不到 %s，先按 README 的 g++ 命令编译" % os.path.basename(exe))
            return 2
    rng = random.Random(20261023)
    ok = n = 0
    for i, (cin, cout, k, s, d, h, w) in enumerate(CASES):
        pad = (k - 1) * d // 2
        if (k - 1) * d != 2 * pad:
            print("[SKIP] #%02d 不满足 same padding" % i)
            continue
        n += 1
        ddir = os.path.join(WORK, "c%02d" % i)
        shift = rng.randint(1, 10)
        chw, wgt, bias = write_case(ddir, cin, cout, k, s, d, pad, h, w,
                                    shift, 30000 + i)
        exp = conv_ref_numpy(chw, wgt, bias, k, s, d, pad, shift, relu=True)
        case_ok = True
        for exe, c8flag in ((TB_C8, 1), (TB_CHW, 0)):
            dump = os.path.join(ddir, "out_%s.bin" % os.path.basename(exe))
            env = os.environ.copy()
            env["TB_DUMP_OUT"] = dump
            relu_en = 0b1101 if c8flag else 1
            r = subprocess.run([exe, ddir, str(cin), str(cout), str(k), str(s),
                                str(d), str(pad), str(relu_en)],
                               capture_output=True, text=True, timeout=600,
                               env=env, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                print("      %s rc=%d: %s" % (os.path.basename(exe), r.returncode,
                                              (r.stdout or r.stderr).strip()[-200:]))
                case_ok = False
                continue
            got = np.fromfile(dump, np.int8).reshape(exp.shape)
            bad = int((got != exp).sum())
            if bad:
                print("      %s 与 Python 打包/numpy 金标准不一致 %d/%d"
                      % (os.path.basename(exe), bad, exp.size))
                case_ok = False
        ok += case_ok
        print("[%s] #%02d cin=%d cout=%d k=%d s=%d d=%d p=%d %dx%d"
              % ("PASS" if case_ok else "FAIL", i, cin, cout, k, s, d, pad, h, w))

    print("==== k=3 权重容器跨语言对拍 %d/%d PASS ====" % (ok, n))
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(main())
