#!/usr/bin/env python3
"""v3.2 引擎的随机形状回归（g++ 版 tb_engine）。

- 正例：随机 same-padding 形状（覆盖 cin/cout 边界 1/112/352/192、k∈{1,3,5}、
  s∈{1,2}、d∈{1,2,3}），要求与参考实现逐位一致；
- 负例：非法参数（cin/cout 越界、pad 不满足 same），要求引擎返回错误码。

用法（fpga/pl 下）：python scripts/fuzz_shapes.py
"""
import os
import random
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
TB = os.path.join(ROOT, "build", "tb_engine.exe")
WORK = os.path.join(ROOT, "build", "fuzz_data")


def write_case(d, cin, cout, k, h, w, shift, seed):
    os.makedirs(d, exist_ok=True)
    rng = random.Random(seed)
    n_in, n_w = cin * h * w, cout * cin * k * k
    with open(os.path.join(d, "input.bin"), "wb") as f:
        f.write(bytes(bytearray(rng.randint(-128, 127) & 0xFF
                                for _ in range(n_in))))
    with open(os.path.join(d, "weight.bin"), "wb") as f:
        f.write(bytes(bytearray(rng.randint(-128, 127) & 0xFF
                                for _ in range(n_w))))
    bias = [rng.randint(-100000, 100000) for _ in range(cout)]
    with open(os.path.join(d, "bias.bin"), "wb") as f:
        f.write(struct.pack("<%di" % cout, *bias))
    with open(os.path.join(d, "meta.txt"), "w") as f:
        f.write("%d\n%d\n%d\n" % (h, w, shift))


def run_tb(datadir, cin, cout, k, s, dil, pad, relu=1):
    r = subprocess.run([TB, datadir, str(cin), str(cout), str(k), str(s),
                        str(dil), str(pad), str(relu)],
                       capture_output=True, text=True, timeout=300,
                       encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    if not os.path.exists(TB):
        print("找不到 tb_engine.exe，先编译（见 README 的 g++ 命令）")
        sys.exit(2)
    # 二进制必须比源码新：否则会拿**旧 exe** 跑出"全过"，把源码编译错误盖住
    # （2026-10-17 真踩过：工作区源码被误贴了 markdown 表格，编译失败，
    #   但 fuzz 用旧 exe 仍然 30/30 PASS——假绿）
    srcs = [os.path.join(ROOT, "src", n) for n in
            ("conv2d_engine.cpp", "conv2d_engine.h", "conv_ref_rt.cpp",
             "conv2d_accel.h")]
    srcs.append(os.path.join(ROOT, "tb", "tb_engine.cpp"))
    newest = max(os.path.getmtime(p) for p in srcs if os.path.exists(p))
    if os.path.getmtime(TB) < newest:
        print("!! tb_engine.exe 比源码旧，拒绝运行（会拿旧二进制骗人）。先重编：")
        print("   g++ -std=c++14 -O2 -I src src/conv2d_engine.cpp "
              "src/conv_ref_rt.cpp tb/tb_engine.cpp -o build/tb_engine.exe")
        sys.exit(2)
    rng = random.Random(20261015)
    ok = 0
    n = 0

    # ---- 正例 ----
    cases = []
    for cin in (1, 2, 16, 112, 113, 144, 192, 351, 352):
        for cout in (1, 8, 16, 95, 96, 191, 192):
            if len(cases) >= 20:
                break
            k = rng.choice((1, 3, 5))
            d = rng.choice((1, 2, 3)) if k == 3 else 1
            pad = (k - 1) * d // 2
            s = rng.choice((1, 2))
            h = rng.randint(2, 9)
            w = rng.randint(2, 9)
            cases.append((cin, cout, k, s, d, pad, h, w))
    cases += [
        (352, 192, 1, 1, 1, 0, 5, 7),      # 全网络最坏形状的变体
        (192, 64, 3, 1, 1, 1, 6, 6),       # k=3 大 cin
        (96, 192, 3, 2, 1, 1, 8, 8),       # 下采样、大 cout
        (12, 48, 5, 1, 1, 2, 5, 5),        # conv_first 形状
        # ---- 部署画幅宽度（列分块 bwcol=76 的整条路径）----
        # 上面那些用例 W 都 ≤ 9，只压到一块；真实网络是 320/640 宽，
        # 一行要切 5~10 块，左补零/直拷/右补零三段边界都得走到。
        (16, 16, 3, 1, 1, 1, 8, 640),      # 640 宽 + 3×3：10 块/行，含右边界
        (352, 192, 1, 1, 1, 0, 4, 640),    # 最坏通道组合 + 640 宽（k=1 的宽度路径）
    ]
    for i, (cin, cout, k, s, d, pad, h, w) in enumerate(cases):
        n += 1
        ddir = os.path.join(WORK, "c%03d" % i)
        write_case(ddir, cin, cout, k, h, w, rng.randint(1, 12), 1000 + i)
        rc, out = run_tb(ddir, cin, cout, k, s, d, pad)
        good = (rc == 0 and "全部一致" in out)
        ok += good
        print("[%s] #%02d cin=%d cout=%d k=%d s=%d d=%d p=%d %dx%d"
              % ("PASS" if good else "FAIL", i, cin, cout, k, s, d, pad, h, w))
        if not good:
            print("      ", out.strip().splitlines()[-1] if out else "无输出")

    # ---- 负例：非法参数必须被拒绝 ----
    negs = [
        (353, 8, 1, 1, 1, 0),      # cin 越界
        (8, 193, 1, 1, 1, 0),      # cout 越界
        (8, 8, 3, 1, 1, 2),        # pad 不满足 same（应为 1）
        (8, 8, 5, 1, 2, 2),        # d 与 pad 不匹配（k=5 只有 d=1）
    ]
    for i, (cin, cout, k, s, d, pad) in enumerate(negs):
        n += 1
        ddir = os.path.join(WORK, "neg%d" % i)
        write_case(ddir, cin, cout, k, 4, 4, 7, 999 + i)
        rc, out = run_tb(ddir, cin, cout, k, s, d, pad)
        good = (rc != 0)
        ok += good
        print("[%s] 负例 #%d cin=%d cout=%d k=%d d=%d pad=%d -> rc=%d"
              % ("PASS" if good else "FAIL", i, cin, cout, k, d, pad, rc))

    print("==== v3.2 形状回归 %d/%d PASS ====" % (ok, n))
    sys.exit(0 if ok == n else 1)


if __name__ == "__main__":
    main()
