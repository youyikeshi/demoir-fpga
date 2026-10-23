#!/usr/bin/env python3
"""cosim 的 AXI `depth=` 与测试数据集对账。

**为什么需要这个脚本**：`src_cosim/conv2d_engine.cpp` 里的 `depth=` 是编译期常量，
必须等于 cosim 所用数据集的张量元素个数。对不上时，HLS 的 C 测试台会少载/少写
一部分数据，报出来的却是

    ERROR: [COSIM 212-359] C/RTL co-simulation FAIL
    不一致 4094 / 10240

——**看起来像"电路写错了"**。2026-10-21 晚因此把 3×3 重排的正确改写冤杀了三次
（每次都是同一个 4094，因为错的是测试台），代价约 2 小时。

本脚本从数据集目录的 `meta.txt` + `TB_S/TB_D/TB_P` 算出四个期望值，
与 `src_cosim` 里的实际值逐项对账：

    python check_cosim_dims.py --data /data/work/tb_k3_32x16          # 只检查
    python check_cosim_dims.py --data /data/work/tb_k3_32x16 --write  # 就地改写

`hls_cosim_engine.tcl` 在 `add_files` 之前会调用本脚本，不一致直接中止。

数据尺寸口径（与 tb_engine.cpp 一致）：
    in  = cin * H * W
    # k=3 的权重容器是 12 字节/(oc,ic)（9 个抽头 + 3 个零，见 conv2d_engine.h
    # 的 K3_PAD12）；k=1/k=5 仍是 k*k。
    wgt = cout * cin * (12 if k == 3 else k * k)
    bias= cout
    out = cout * Hout * Wout,  Hout = (H-1)//s + 1
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PORTS = ["in_fm", "wgt", "bias", "out_fm"]


def parse_meta(data_dir):
    """读数据集形状。

    meta.txt 有两种格式：
      · `CIN=.. COUT=.. K=.. H=.. W=.. shift=..`（新）
      · 每行一个数：H、W、shift（gen_testdata.py 当前的写法）
    后者的 cin/cout/k 只在 `TB_CIN/TB_COUT/TB_K` 环境变量里（tb_engine 就是从这读的），
    所以这里一并回退到环境变量。
    """
    path = os.path.join(data_dir, "meta.txt")
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read()
    kv = {k.lower(): int(v) for k, v in re.findall(r"([A-Za-z_]+)\s*=\s*(\d+)", txt)}
    if "cin" in kv:
        return kv
    nums = [int(x) for x in re.findall(r"^\s*(\d+)\s*$", txt, re.M)]
    meta = {}
    if len(nums) >= 3:                       # H / W / shift
        meta.update({"h": nums[0], "w": nums[1], "shift": nums[2]})
    for key, env in (("cin", "TB_CIN"), ("cout", "TB_COUT"), ("k", "TB_K")):
        if env in os.environ:
            meta[key] = int(os.environ[env])
    return meta


def expected_dims(meta, s):
    cin, cout, k = meta["cin"], meta["cout"], meta["k"]
    h, w = meta["h"], meta["w"]
    hout = (h - 1) // s + 1
    wout = (w - 1) // s + 1
    return {"in_fm": cin * h * w,
            "wgt": cout * cin * (12 if k == 3 else k * k),
            "bias": cout, "out_fm": cout * hout * wout}


def read_actual(cpp):
    with open(cpp, "r", encoding="utf-8") as f:
        src = f.read()
    got = {}
    for port in PORTS:
        m = re.search(r"port=%s\b[^)]*?depth\s*=\s*(\d+)" % port, src)
        got[port] = int(m.group(1)) if m else None
    return src, got


def rewrite(src, want):
    out = src
    for port in PORTS:
        if want[port] is None:
            continue
        out = re.sub(r"(port=%s\b[^)]*?depth\s*=\s*)\d+" % port,
                     lambda m: m.group(1) + str(want[port]), out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("TB_DATA"),
                    help="cosim 数据集目录（含 meta.txt），默认取 TB_DATA")
    ap.add_argument("--cpp", default=os.path.join(HERE, "..", "src_cosim",
                                                  "conv2d_engine.cpp"))
    ap.add_argument("--s", type=int, default=int(os.environ.get("TB_S", 1)))
    ap.add_argument("--write", action="store_true", help="把实际值改成期望值")
    a = ap.parse_args()
    if not a.data:
        print("SKIP: 没有 --data / TB_DATA，跳过 depth 对账")
        return 0
    meta = parse_meta(a.data)
    miss = [x for x in ("cin", "cout", "k", "h", "w") if x not in meta]
    if miss:
        print("SKIP: 缺少 %s（meta.txt 只有 H/W/shift，其余请用 TB_CIN/TB_COUT/TB_K 环境变量给）"
              % ",".join(miss))
        return 0
    want = expected_dims(meta, a.s)
    src, got = read_actual(a.cpp)
    print("数据集: cin=%d cout=%d k=%d H=%d W=%d s=%d  →  期望 in=%d wgt=%d bias=%d out=%d"
          % (meta["cin"], meta["cout"], meta["k"], meta["h"], meta["w"], a.s,
             want["in_fm"], want["wgt"], want["bias"], want["out_fm"]))
    bad = []
    for port in PORTS:
        if got[port] != want[port]:
            bad.append((port, got[port], want[port]))
    if not bad:
        print("[OK] src_cosim 的 four depth= 与数据集一致")
        return 0
    print("[FAIL] depth 与数据集不一致（cosim 会伪装成\"RTL 功能错误\"）：")
    for port, g, w in bad:
        print("   %-7s 现在 %-7s 应为 %d" % (port, g, w))
    if a.write:
        with open(a.cpp, "w", encoding="utf-8") as f:
            f.write(rewrite(src, want))
        print("[WROTE] 已改写 %s（记得别提交这份针对临时数据集的取值）" % a.cpp)
        return 0
    print("修复：加 --write，或手工改成上面的\"应为\"值。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
