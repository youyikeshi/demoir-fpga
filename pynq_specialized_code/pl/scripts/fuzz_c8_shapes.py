#!/usr/bin/env python3
"""编译期 C8 布局的形状回归（g++ 版 tb_engine_c8）。

与 `fuzz_shapes.py` 的关系：形状/随机数据完全同构，**23 个形状**逐一跑。
每个用例生成两份输入（CHW 与 C8，用软件侧已归档的 `ps/c8/c8lib.py` 的
`to_c8(..., pad=True)` 生成；C8 输入按 ceil(C/8)*8 通道补零），输出统一转
CHW 与 `conv_ref_rt` 逐位比对：

    build/tb_engine_c8.exe      ← -DC8_LAYOUT=1：只跑 C8（布局编译期定死）
    build/tb_engine_c8_chw.exe  ← -DC8_LAYOUT=0：只跑 CHW

上一版是运行期 bit2/bit3 选路的四种组合（00/11/01/10）；本版布局在编译期
定死，组合只剩一种，判据变成"两个**独立构建**逐字节相同"：

  ① C8 构建的输出 == conv_ref_rt（引擎内部 C8 地址公式 == c8lib 的布局）；
  ② C8 构建的输出 == CHW 构建的输出（逐字节；含 cout 非 8 倍数时补零通道
     没被写脏、cin 非 8 倍数时补零区不被乘加"读进来"）；
  ③ CHW 构建忽略 bit2/bit3（传 0b1101 与传 0b0001 同结果）；
  ④ C8 构建对"声明位"缺失报 ERR_LAYOUT（由测试台内的负向测试断言）。

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
TB_CHW = os.path.join(ROOT, "build", "tb_engine_c8_chw.exe")
WORK = os.path.join(ROOT, "build", "fuzz_c8_data")
C8LIB = os.path.join(REPO, "pynq_specialized_code", "ps", "c8")

# 每个 (oc,ic) 的权重容器字节数（与 conv2d_engine.h 的 K3_PAD12 / K5_PAD28 对应）
WC = {1: 1, 3: 12, 5: 28}

sys.path.insert(0, C8LIB)
import c8lib  # noqa: E402  （仓库里已归档的软件实现，逐位验证过 517 算子）

# 编译期布局：C8 构建只有一种组合（bit2/bit3 是声明位，由测试台无条件置位）
COMBOS = [(1, 1)]


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
    with open(os.path.join(d, "input_chw.bin"), "wb") as f:
        f.write(chw.tobytes(order="C"))
    with open(os.path.join(d, "input_c8.bin"), "wb") as f:
        f.write(c8.tobytes(order="C"))
    with open(os.path.join(d, "weight.bin"), "wb") as f:
        # k=3 / k=5 的权重容器是 12 / 28 字节每 (oc,ic)（K3_PAD12 / K5_PAD28=1）
        n_w = cout * cin * WC[k]
        f.write(bytes(bytearray(rng.randint(-128, 127) & 0xFF
                                for _ in range(n_w))))
    bias = [rng.randint(-100000, 100000) for _ in range(cout)]
    with open(os.path.join(d, "bias.bin"), "wb") as f:
        f.write(struct.pack("<%di" % cout, *bias))
    with open(os.path.join(d, "meta.txt"), "w") as f:
        f.write("%d\n%d\n%d\n" % (h, w, shift))
    return info


def run_tb(exe, datadir, cin, cout, k, s, dil, pad, relu_en, dump=None):
    env = os.environ.copy()
    if dump is not None:
        env["TB_DUMP_OUT"] = dump
    else:
        env.pop("TB_DUMP_OUT", None)
    r = subprocess.run([exe, datadir, str(cin), str(cout), str(k), str(s),
                        str(dil), str(pad), str(relu_en)],
                       capture_output=True, text=True, timeout=600, env=env,
                       encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    if not os.path.exists(TB):
        print("找不到 tb_engine_c8.exe，先编译：")
        print("   g++ -std=c++14 -O2 -DC8_LAYOUT=1 -I src src/conv2d_engine.cpp "
              "src/conv_ref_rt.cpp tb/tb_engine_c8.cpp -o build/tb_engine_c8.exe")
        print("   g++ -std=c++14 -O2 -DC8_LAYOUT=0 -I src src/conv2d_engine.cpp "
              "src/conv_ref_rt.cpp tb/tb_engine_c8.cpp -o build/tb_engine_c8_chw.exe")
        sys.exit(2)
    # 二进制必须比源码新（防"旧 exe 假绿"，2026-10-17 真踩过）
    srcs = [os.path.join(ROOT, "src", n) for n in
            ("conv2d_engine.cpp", "conv2d_engine.h", "conv_ref_rt.cpp",
             "conv2d_accel.h")]
    srcs.append(os.path.join(ROOT, "tb", "tb_engine_c8.cpp"))
    newest = max(os.path.getmtime(p) for p in srcs if os.path.exists(p))
    for exe in (TB, TB_CHW):
        if os.path.exists(exe) and os.path.getmtime(exe) < newest:
            print("!! %s 比源码旧，拒绝运行。先重编" % os.path.basename(exe))
            sys.exit(2)

    have_chw = os.path.exists(TB_CHW)
    if not have_chw:
        print("!! 没有 build/tb_engine_c8_chw.exe：跳过「与旧 CHW 构建逐字节比对」这一项")

    rng = random.Random(20261023)
    cases = [
        # cin / cout 都不是 8 的倍数（尾部补零块）
        (12, 12, 3, 1, 1, 5, 6),
        (3, 12, 3, 1, 1, 5, 5),
        (1, 1, 1, 1, 1, 3, 4),
        (2, 95, 3, 2, 1, 7, 7),
        (113, 191, 1, 1, 1, 3, 5),
        (351, 95, 3, 1, 1, 4, 6),
        (12, 48, 5, 1, 1, 5, 5),       # conv_first 形状：cin CHW / cout C8
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
    ok_combo = {c: 0 for c in COMBOS}
    chw_same = 0
    chw_n = 0
    for i, (cin, cout, k, s, dil, h, w) in enumerate(cases):
        pad = (k - 1) * dil // 2
        if (k - 1) * dil != 2 * pad:
            print("[SKIP] #%02d k=%d d=%d 不满足 same padding" % (i, k, dil))
            continue
        n += 1
        ddir = os.path.join(WORK, "c%03d" % i)
        shift = rng.randint(1, 12)
        info = write_case(ddir, cin, cout, k, h, w, shift, 20000 + i)
        flags = []
        case_ok = True
        outs = {}
        for (c8_in, c8_out) in COMBOS:
            relu_en = 1 | (c8_in << 2) | (c8_out << 3)   # relu 恒开
            dump = os.path.join(ddir, "out_%d%d.bin" % (c8_in, c8_out))
            rc, out = run_tb(TB, ddir, cin, cout, k, s, dil, pad, relu_en, dump)
            good = (rc == 0 and "全部一致" in out)
            case_ok = case_ok and good
            ok_combo[(c8_in, c8_out)] += good
            if good:
                with open(dump, "rb") as f:
                    outs[(c8_in, c8_out)] = f.read()
            flags.append("%d%d:%s" % (c8_in, c8_out, "P" if good else "F"))
            if not good:
                print("      [%d%d] %s" % (c8_in, c8_out,
                                           out.strip().splitlines()[-1] if out else "无输出"))
        # 编译期布局：只有一种组合，组合间一致性退化为"存在即一致"
        same = len(set(outs.values())) <= 1
        if have_chw:
            # 与 C8_LAYOUT=0 的**另一个构建**逐字节比对
            dump_ref = os.path.join(ddir, "out_chwbuild.bin")
            rc, out = run_tb(TB_CHW, ddir, cin, cout, k, s, dil, pad, 1, dump_ref)
            with open(dump_ref, "rb") as f:
                ref = f.read()
            # C8_LAYOUT=0 必须忽略 bit2/bit3：传 0b1101 结果应相同
            dump_ref2 = os.path.join(ddir, "out_chwbuild_bits.bin")
            rc2, out2 = run_tb(TB_CHW, ddir, cin, cout, k, s, dil, pad,
                               1 | (1 << 2) | (1 << 3), dump_ref2)
            with open(dump_ref2, "rb") as f:
                ref2 = f.read()
            chw_n += 1
            if (rc == 0 and rc2 == 0 and ref == ref2
                    and (1, 1) in outs and outs[(1, 1)] == ref):
                chw_same += 1
            else:
                print("      !! #%02d C8 构建与 C8_LAYOUT=0 构建不一致"
                      "（rc=%d/%d，bit 忽略=%s）" % (i, rc, rc2, ref == ref2))
        ok += case_ok and same
        print("[%s] #%02d cin=%d(->%d) cout=%d(->%d) k=%d s=%d d=%d p=%d %dx%d  %s%s"
              % ("PASS" if (case_ok and same) else "FAIL", i, cin, info["padded_c"],
                 cout, ((cout + 7) // 8) * 8, k, s, dil, pad, h, w,
                 " ".join(flags), "" if same else "  组合间不一致"))

    print("==== 编译期 C8 布局回归 %d/%d 用例 PASS ====" % (ok, n))
    print("     分组合计：" + "  ".join("%d%d=%d/%d" % (a, b, ok_combo[(a, b)], n)
                                       for (a, b) in COMBOS))
    if have_chw:
        print("     C8 构建与 C8_LAYOUT=0 构建逐字节一致：%d/%d"
              "（含 bit2/bit3 被忽略的验证）" % (chw_same, chw_n))
    sys.exit(0 if ok == n and n >= 20 and (not have_chw or chw_same == chw_n) else 1)


if __name__ == "__main__":
    main()
