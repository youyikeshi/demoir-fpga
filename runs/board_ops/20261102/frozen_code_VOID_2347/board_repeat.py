#!/usr/bin/env python3
"""同一个进程内重复跑整网 N 次（长程验证的标准测量工具）。

★ 口径（2026-11-02 更正）：旧版 docstring 写"跨进程比较没有意义"，同一文件第 62 行又写
  "冻结基线 8.096 s 的口径是『同进程 5 次里最好』"——**后者是错的**，两句也自相矛盾，
  现统一为：
    冻结基线 **8.096 s** 的口径是「**跨 2 个进程的 5 个热态样本取最小**」：
      · P1 `runs/board_v39/v39_verify.log:78,83`   ⇒ 8.281 / 8.198（该进程第 4/5 次）
      · P2 `runs/board_v39/v39_warm.log:82,88,94`  ⇒ 8.113 / 8.096 / 8.136（第 5/6/7 次）
      ⇒ 中位 8.136、均值 8.165、极差 0.185
  本工具给出的是**单进程内**统计（第 2 次起的最小/中位/均值/最大）。
  **两者口径不同 ⇒ 只允许比相对差**（同工具、同进程、同运行点的 A 臂 vs B 臂）；
  不许把本工具的绝对值直接当成 8.096 的同口径复现值。

★ 一次性成本：C 内核 `load()` + fast 自检在**计时外**单独计一次并打印；
  首个 run 若含它，则**同时**报 口径 a（含一次性成本 = 用户真实首帧）与
  口径 b（不含 = 技术对比）。不许只报好看的那个。

用法（板端）：
    sudo -S -p '' env XILINX_XRT=/usr ... python3 -u board_repeat.py \
         --bit /home/xilinx/demoir/design_1_150v39.bit --slots 24 --hw 256 --n 5 \
         --bilinear-c 1

输出：每次的总时间/PL/PS/各分项/sha1 + 第 2 次起的最小/中位/均值/最大 + 两种口径。
"""
import argparse, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
# 本工具既可从仓库 tools/ 跑（主机侧语法检查），也可放在板端 ps/pynq/ 同目录跑；
# 旧版只 insert HERE ⇒ 放在 tools/ 下必然 import 失败。
for _p in (HERE,
           os.path.join(HERE, "..", "pynq_specialized_code", "ps", "pynq"),
           "/home/xilinx/demoir/ps/pynq"):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np
import board_v39_verify as V   # 复用已验证的 load/地址核对/run_net


def stats(xs):
    s = sorted(float(x) for x in xs)
    n = len(s)
    med = s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])
    return dict(n=n, min=s[0], med=med, mean=sum(s) / n, max=s[-1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v39.bit")
    ap.add_argument("--fclk", type=float, default=150.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--hw", type=int, default=256)
    ap.add_argument("--n", type=int, default=5, help="同进程内跑几次")
    ap.add_argument("--json", default="/home/xilinx/demoir/repeat.json")
    ap.add_argument("--download", action="store_true", help="需要一次真正的 overlay 下载")
    ap.add_argument("--bilinear-c", type=int, choices=(0, 1), default=1,
                    help="1=interp 走 C 内核（基线运行点；opt-in 开关必须显式给）；"
                         "0=numpy 回落臂")
    ap.add_argument("--seed", type=int, default=1,
                    help="输入 rng=default_rng(seed)。默认 1 = 冻结基线输入")
    a = ap.parse_args()

    V._BC["want"] = str(a.bilinear_c)
    os.environ["BIN_BILINEAR_C"] = V._BC["want"]   # opt-in：显式落环境变量

    print("=" * 74)
    print("  同进程重复测量  hw=%d  槽=%d  n=%d  fclk=%.1f  seed=%d  BIN_BILINEAR_C=%s"
          % (a.hw, a.slots, a.n, a.fclk, a.seed, V._BC["want"]))
    print("  bit=%s" % os.path.basename(a.bit))
    print("=" * 74)

    ov = V.step_load(a.bit, download=a.download)
    V.step_addr(ov)
    ident = V.step_identity(ov, a.fclk)
    if not ident.get("ok"):
        print("!! 指纹不对 ⇒ 停手")
        return 3
    V.step_bilinear()                      # ④：一次性成本单独计时（在任何 run 之前）
    rp = V.runpoint(a.bit, a.fclk, a.slots,
                    switches=V._switch_values(a.slots, a.bilinear_c))
    print("  运行点：bit_md5=%s  BIN_BILINEAR_C=%s"
          % (rp.get("bit_md5"), os.environ.get("BIN_BILINEAR_C")))
    print("  代码 md5：%s" % json.dumps(rp.get("code_md5"), ensure_ascii=False))
    arena, need = V.make_arena(a.hw, a.slots, "rep_arena_%d" % a.hw)

    rows = []
    for i in range(a.n):
        print("-" * 74)
        print("  第 %d/%d 次" % (i + 1, a.n))
        row = V.run_net(ov, a.hw, arena, a.slots,
                        tag="rep%d_hw%d" % (i + 1, a.hw), seed=a.seed)
        rows.append(row or {})
        sys.stdout.flush()

    tot = [float(r["total_s"]) for r in rows if r.get("total_s")]
    warm_st = None
    print("=" * 74)
    if tot:
        warm = tot[1:] if len(tot) > 1 else tot
        warm_st = stats(warm)
        print("  全部 %d 次：%s" % (len(tot), "  ".join("%.3f" % t for t in tot)))
        print("  **第 2 次起**（单进程热态 n=%d）：最小 %.3f  中位 %.3f  均值 %.3f  最大 %.3f"
              % (warm_st["n"], warm_st["min"], warm_st["med"],
                 warm_st["mean"], warm_st["max"]))
        print("  口径：本工具 = **单进程内**统计（第 2 次起）；")
        print("        冻结基线 8.096 s = **跨 2 进程 5 个热态样本的最小值**（中位 8.136、均值 8.165）")
        print("        ⇒ 两者口径不同，**只允许比相对差**，不许拿本工具绝对值当 8.096 的复现")
        first = rows[0] if rows else {}
        if first.get("onetime_s"):
            print("  口径 a) 含一次性成本 %.3f s 的首帧 = %.3f s（第 1 次，用户真实体验）"
                  % (first["onetime_s"], first["total_incl_onetime_s"]))
            print("  口径 b) 不含它的稳态 = %.3f s（第 1 次；技术对比）" % first["total_s"])
        else:
            print("  口径 a = 口径 b（本次 C 内核未启用 ⇒ 无一次性成本）")
    else:
        print("  ✗ 没有拿到任何 total_s，不做统计（不把「没测」写成「通过」）")
    bc = ((V.R.get("runpoint") or {}).get("bilinear_c") or {})
    if bc:
        print("  C 内核一次性成本（load+fast 自检，计时外）= %.3f s  loaded=%s  why=%s"
              % (bc.get("seconds") or 0, bc.get("loaded"), bc.get("why")))
    print("=" * 74)

    V.R["repeat"] = dict(hw=a.hw, slots=a.slots, n=a.n, seed=a.seed,
                         bilinear_c=V._BC["want"],
                         totals=tot, warm=warm_st,
                         rows=rows)
    V.dump(a.json)
    print("  结果已写 %s" % a.json)


if __name__ == "__main__":
    main()
