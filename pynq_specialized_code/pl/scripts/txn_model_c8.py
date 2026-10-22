#!/usr/bin/env python3
"""C8 布局的事务数（burst）对照：把 RTL 这次的实现口径算清楚。

直接复用软件侧已归档的 `ps/c8/txn_model.py` 的 `read_bursts` / `write_bursts`
（它照抄了 `pl/src/conv2d_engine.cpp` 的 DDR 访问循环），只是把**四种配置**都跑一遍：

    chw_nowide    CHW + 行复用 + K1_WIDE_COLS=0   ← 板端 8.76 M 笔的标定口径
    chw_wide1     CHW + 行复用 + K1_WIDE_COLS=1   ← 仓库当前源码的默认配置
    c8            C8  + 行复用 + K1_WIDE_COLS=0   ← **本次 RTL（C8_LAYOUT=1）**
    c8_noreuse    C8  + 不用行复用                ← 若把 line_buf 砍到 8 通道的代价

另外给一条"64 拍上限"修正：wide1 下每笔逻辑 burst 是 320–448 B = 80–112 拍，
超过 `max_read_burst_length=64`（C8 才提到 256），会被拆成 2 笔 —— 模型按逻辑笔数
算，会低估 wide1 的实拍数。

用法：
    /data/work/venv-demoir/bin/python pynq_specialized_code/pl/scripts/txn_model_c8.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PL = os.path.abspath(os.path.join(HERE, ".."))
C8DIR = os.path.abspath(os.path.join(PL, "..", "ps", "c8"))
sys.path.insert(0, C8DIR)
import txn_model as M  # noqa: E402

MANIFEST = os.path.join(PL, "..", "ps", "manifest_640.json")
CFGS = {
    "chw_nowide": (False, True, 0),
    "chw_wide1": (True, True, 0),
    "c8": (False, True, 8),
    "c8_noreuse": (False, False, 8),
}


def main():
    man = json.load(open(MANIFEST, encoding="utf-8"))
    bufs = man["buffers"]
    ops = [o for o in man["ops"] if o["needed"] and o["op"] == "conv"]
    tot = {k: [0, 0] for k in CFGS}
    per_k = {}
    for o in ops:
        H, W = o["h"], o["w"]
        Hout, Wout = bufs[o["out"][0]]["h"], bufs[o["out"][0]]["w"]
        cin, cout = o["cin"], o["cout"]
        c8_in = 8 if cin % 8 == 0 else 0      # cin 不是 8 的倍数 -> 该张量保持 CHW
        c8_out = 8 if cout % 8 == 0 else 0
        for name, (wide1, reuse, _) in CFGS.items():
            gg = c8_in if name.startswith("c8") else 0
            n, _ = M.read_bursts(H, W, Hout, Wout, cin, o["k"], o["s"], o["d"],
                                 o["pad"], gg, wide1, reuse)
            m, _ = M.write_bursts(Hout, Wout, cout, o["s"],
                                  c8_out if name.startswith("c8") else 0)
            tot[name][0] += n
            tot[name][1] += m
            key = (name, o["k"], o["s"])
            v = per_k.setdefault(key, [0, 0])
            v[0] += n
            v[1] += 1

    print("manifest: %s   卷积 %d 个   标定 10.3 us/事务\n"
          % (os.path.basename(MANIFEST), len(ops)))
    print("%-12s %10s %10s %12s %12s" % ("配置", "读(笔)", "写(笔)", "读写合计(笔)", "填充@10.3us"))
    for name in ("chw_nowide", "chw_wide1", "c8", "c8_noreuse"):
        r, w = tot[name]
        print("%-12s %9.3fM %9.3fM %11.3fM %10.1f s"
              % (name, r / 1e6, w / 1e6, (r + w) / 1e6, r * M.US_PER_TXN / 1e6))

    r0, r1 = tot["chw_nowide"][0], tot["c8"][0]
    print("\n---- C8 构建（本次 RTL）的降幅 ----")
    print("  读 vs 板端标定 8.76 M 笔      : ÷%.2f  (模型 chw_nowide=%.3f M, 差 %+.1f%%)"
          % (8.76e6 / r1, r0 / 1e6, 100 * (r0 / 8.76e6 - 1)))
    print("  读 vs 仓库当前源码 wide1=1    : ÷%.2f  (模型 %.3f M)"
          % (tot["chw_wide1"][0] / r1, tot["chw_wide1"][0] / 1e6))
    w0, w1 = tot["chw_nowide"][1], tot["c8"][1]
    print("  写 vs CHW                     : ÷%.2f  (%.3f M -> %.3f M)"
          % (w0 / w1, w0 / 1e6, w1 / 1e6))
    print("  读写合计 vs 标定口径          : ÷%.2f"
          % ((tot["chw_nowide"][0] + w0) / (r1 + w1)))
    print("  若 line_buf 砍到 8 通道（丢跨行复用）: 读 %.3f M，比本实现差 %.2f×（÷%.2f vs 8.76 M）"
          % (tot["c8_noreuse"][0] / 1e6, tot["c8_noreuse"][0] / r1,
             8.76e6 / tot["c8_noreuse"][0]))

    # wide1 的 64 拍上限修正
    print("\n---- wide1 的突发长度修正（重要）----")
    wide1_k1 = per_k.get(("chw_wide1", 1, 1), [0, 0])
    print("  k=1,s=1 层（%d 个）wide1 逻辑笔数 %.3f M；每笔 320–448 B = 80–112 拍，"
          % (wide1_k1[1], wide1_k1[0] / 1e6))
    print("  超过 CHW 构建的 max_read_burst_length=64 ⇒ 实际约 ×2 = %.3f M" % (2 * wide1_k1[0] / 1e6))
    corr = tot["chw_wide1"][0] + wide1_k1[0]
    print("  修正后 wide1 读 ≈ %.3f M（C8 构建 1.118 M ⇒ ÷%.2f）" % (corr / 1e6, corr / r1))
    print("  注：C8 构建把 max_read_burst_length 提到 256，8*lb ≤ 608 B = 152 拍 < 256 ⇒ 一笔。")

    print("\n---- 按 (k,s) 拆分（读笔数，M）----")
    print("%-10s %12s %12s %10s %8s" % ("(k,s)", "chw_wide1", "chw_nowide", "c8", "层数"))
    for (k, s) in sorted({(k, s) for (_, k, s) in per_k}):
        a = per_k.get(("chw_wide1", k, s), [0, 0])[0]
        b = per_k.get(("chw_nowide", k, s), [0, 0])[0]
        c = per_k.get(("c8", k, s), [0, 0])[0]
        n = per_k.get(("c8", k, s), [0, 0])[1]
        print("k=%d s=%d  %12.3f %12.3f %10.3f %8d" % (k, s, a / 1e6, b / 1e6, c / 1e6, n))


if __name__ == "__main__":
    main()
