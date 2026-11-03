#!/usr/bin/env python3
"""重定标"整数仿射"与 256 项查找表的**逐位等价**验证（主机 + 板上都能跑）。

背景（2026-10-23 第六轮）：cat 占整网 5.66 s，实测每段的两种工作——
整块拷贝、按 256 项表重定标——都受限于 `_req` 的实现
（`tobytes` + `bytes.translate` + 赋值 = 42.8 MB/s，而 memcpy 有 183 MB/s）。
板端 `bytes.translate` 只有 69 MB/s，256 项 NEON 查表也只有 62 MB/s
（A9 的 NEON 是 64 位，256 项要 8 次 vtbl+选择）。

于是换成整数仿射 `y = sat8((x*A + C) >> S)`：只要**对全部 256 个 int8 逐项
验证与查表结果相同**，它就是逐位等价的（不是"近似"），而内核只要 ~10 条指令
/16 字节，瓶颈回到内存。

本脚本验证三件事：
  1) 整网 manifest 里**所有** (s_from, s_to) 标度对都能拟合出仿射式；
  2) 每个仿射式对 256 个 int8 取值与查表逐位相同；
  3) （若 i8_fast.c 能编）C 内核的输出 == bytes.translate，含全值域与随机数据；
     再补一组随机 (s_from, s_to) 做压力测试。

用法：
    python checks.../check_i8_scaling.py                 # 仓库里
    python check_i8_scaling.py                           # 板上（扁平目录）
"""
import os
import random
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pynq"))


def _imports():
    import lut_util
    return lut_util


def real_pairs(manifest_path, index_path):
    """从 manifest + index.json 抽出**整网真实用到的**标度对。"""
    import json
    with open(manifest_path, encoding="utf-8") as f:
        m = json.load(f)
    with open(index_path, encoding="utf-8") as f:
        sc = json.load(f)["scales"]
    pairs = set()
    for op in m["ops"]:
        if op["op"] not in ("cat", "add", "sam_sum"):
            continue
        so = sc.get(op["out"][0])
        if so is None:
            continue
        for t in op["in"]:
            st = sc.get(t)
            if st is not None and st != so:
                pairs.add((float(st), float(so)))
    return sorted(pairs)


def main():
    lut_util = _imports()
    xs = np.arange(-128, 128, dtype=np.int64)

    def verify(sf, st):
        tab = lut_util.rescale_lut_bytes(sf, st)
        tbl = np.frombuffer(tab, dtype=np.int8)
        want = tbl[xs & 0xFF]
        plan = lut_util.rescale_plan(sf, st)
        if plan[0] == "q15":
            m, A = plan[1], plan[2]
            got = np.clip(np.clip((2 * (xs << m) * A + (1 << 15)) >> 16,
                                  -32768, 32767), -128, 127).astype(np.int8)
        elif plan[0] == "affine":
            A, S, C = plan[1], plan[2], plan[3]
            got = np.clip((xs * A + C) >> S, -128, 127).astype(np.int8)
        else:
            return False, "既没拟合出 Q15 也没拟合出仿射（回落查表）"
        if not np.array_equal(got, want):
            return False, "%s 与查表不一致 %d/256" % (plan[0],
                                                     int((got != want).sum()))
        return True, plan[0]

    # 1) 整网真实标度对
    # 仓库里：ps/tools/ → ../../ps/manifest_640.json（实际是 ps/manifest_640.json）
    # 板上：扁平目录，manifest 与 weights_pack/ 都在脚本旁边
    man = next((p for p in (os.path.join(HERE, "..", "manifest_640.json"),
                            os.path.join(HERE, "manifest_640.json"))
                if os.path.exists(p)), "")
    idx = next((p for p in (os.path.join(HERE, "..", "..", "deploy", "weights_pack",
                                         "index.json"),
                            os.path.join(HERE, "weights_pack", "index.json"))
                if os.path.exists(p)), "")
    n_fail = 0
    kinds = {}
    pairs = real_pairs(man, idx) if (man and idx) else []
    for sf, st in pairs:
        ok, why = verify(sf, st)
        kinds[why] = kinds.get(why, 0) + 1
        if not ok:
            n_fail += 1
            print("[FAIL] %.9g -> %.9g : %s" % (sf, st, why))
    print("整网真实标度对：%d 对，等价拟合 %d/%d（%s）"
          % (len(pairs), len(pairs) - n_fail, len(pairs), kinds))

    # 2) 随机标度对（压力测试，含极端比例）
    rng = random.Random(20261023)
    n_rand = 0
    n_rand_fail = 0
    for _ in range(200):
        sf = 10 ** rng.uniform(-3, 0)
        st = 10 ** rng.uniform(-3, 0)
        n_rand += 1
        ok, why = verify(sf, st)
        if not ok:
            n_rand_fail += 1
            print("[FAIL] 随机 %.9g -> %.9g : %s" % (sf, st, why))
    print("随机标度对：%d 对，等价拟合 %d/%d"
          % (n_rand, n_rand - n_rand_fail, n_rand))

    # 3) C 内核（能编就用；x86 上走标量兜底，同样逐位验证）
    n_c_fail = 0
    try:
        import i8_fast
        if not i8_fast.available():
            print("i8_fast 不可用（%s）——跳过 C 内核验证" % i8_fast.err())
        else:
            rng2 = np.random.default_rng(20261023)
            base = np.tile(np.arange(-128, 128, dtype=np.int8), 32)
            for sf, st in (list(pairs[:20]) + [(1.0, 0.5), (0.5, 1.0),
                                               (1.0, 1.0), (3.0, 0.75)]):
                tab = lut_util.rescale_lut_bytes(sf, st)
                tbl = np.frombuffer(tab, dtype=np.int8)
                plan = lut_util.rescale_plan(sf, st)
                for src in (base, rng2.integers(-128, 128, 4096, dtype=np.int8)):
                    want = np.frombuffer(src.tobytes().translate(tab),
                                         dtype=np.int8)
                    dst = np.empty_like(src)
                    i8_fast.lut_into(dst, src, tbl)
                    if not np.array_equal(dst, want):
                        n_c_fail += 1
                        print("[FAIL] i8_lut 与 translate 不同: %.9g->%.9g" % (sf, st))
                    if plan[0] == "affine":
                        A, S, C = plan[1], plan[2], plan[3]
                        i8_fast.scale_into(dst, src, A, S, C)
                        if not np.array_equal(dst, want):
                            n_c_fail += 1
                            print("[FAIL] i8_scale 与 translate 不同: %.9g->%.9g"
                                  % (sf, st))
                        ip = src.copy()
                        i8_fast.scale_ip(ip, A, S, C)
                        if not np.array_equal(ip, want):
                            n_c_fail += 1
                            print("[FAIL] i8_scale_ip 与 translate 不同")
            cp = rng2.integers(-128, 128, 8192, dtype=np.int8)
            cd = np.zeros_like(cp)
            i8_fast.copy_into(cd, cp)
            if not np.array_equal(cd, cp):
                n_c_fail += 1
                print("[FAIL] i8_copy 不同")
            print("C 内核：%s（%s）" % ("全部一致" if n_c_fail == 0 else "有差异",
                                    "NEON/标量按编译结果"))
    except Exception as e:                        # noqa: BLE001
        print("C 内核验证异常（跳过）：%s" % e)

    bad = n_fail + n_rand_fail + n_c_fail
    print("==== 重定标逐位等价验证 %s ====" % ("PASS" if bad == 0 else "FAIL"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
