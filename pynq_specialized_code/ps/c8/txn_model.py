#!/usr/bin/env python3
"""事务数（burst）解析模型：CHW vs C8 交错。

模型来源：直接照 pl/src/conv2d_engine.cpp 的 DDR 访问循环复刻
（不改 RTL，只读它的循环结构）。

  读侧（"填充"）：列块外层 → 输出行 → 需要装载的输入行 r → 通道 ic → 连续 lb 字节
      一次 `in_fm[io + j]`（j=0..lb-1，常量连续地址）在 CHW 下 = 同 (ic, iy) 行内
      连续 lb 字节 ⇒ **1 笔 burst / (通道, 行, 列块)**。
      行缓冲复用（span>stride 时只在 BRAM 内下移，DDR 只装 stride 行）要照抄，
      否则会多算 2~3 倍。补零行（iy 越界）不读 DDR，也要照抄。
  写侧：`out_fm[((ocb+i)*Hout + y)*Wout + x0 + x2]`，x2 连续 ⇒
      **1 笔 burst / (输出通道, 行, 列块)**。

C8 下同 (行, 列块) 的 8 个通道在内存里连续 ⇒ 每 8 个通道合成 1 笔：
  bursts_C8 = ceil(cin/8)（最后一个不完整块也算 1 笔）
  通道数不是 8 的倍数（8 个边界例外）→ 该张量仍 CHW，笔数不变。

标定（板端实测）：10.3 µs/事务，64.7 B/事务。
"""
import argparse
import json
import math
import os

PS = "/media/felix/Data/coding/demoir-fpga/pynq_specialized_code/ps"
SPAN_MAX = 7
K1_WIDE_COLS = 1          # conv2d_engine.h
US_PER_TXN = 10.3         # 实测
BYTES_PER_TXN_MEAS = 64.7  # 实测


def read_bursts(H, W, Hout, Wout, cin, k, s, d, pad, c8=0,
                wide1_en=K1_WIDE_COLS, reuse=True):
    """复刻 RTL 的填充循环。c8=0 -> CHW；c8=8 -> 按 C8 合并。

    返回 (bursts, bytes_read)。
    """
    span = 2 * pad + 1
    wide1 = bool(wide1_en) and (k == 1) and (s == 1)
    bw = (SPAN_MAX * 64) if wide1 else (32 if s == 2 else 64)
    bwcol = (bw - 1) * s + span + 2 * pad
    keep = (span - s) if span > s else 0
    groups = (cin + c8 - 1) // c8 if c8 else cin
    n = 0
    nbytes = 0
    gc0 = -pad
    for x0 in range(0, Wout, bw):
        la = min(max(-gc0, 0), bwcol)
        lb = min(max(W - gc0 - la, 0), bwcol - la)
        row_buf_valid = False
        for y in range(Hout):
            need0 = y * s - pad
            r_begin = (keep if (row_buf_valid and keep > 0) else 0) if reuse else 0
            for r in range(r_begin, span):
                iy = need0 + r
                if 0 <= iy < H and lb > 0:
                    n += groups
                    nbytes += cin * lb
            row_buf_valid = True
        gc0 += bw * s
    return n, nbytes


def write_bursts(Hout, Wout, cout, s, c8=0):
    """输出写回：1 笔 / (输出通道块, 行, 列块)。"""
    bw = 32 if s == 2 else 64
    blocks = (Wout + bw - 1) // bw
    groups = (cout + c8 - 1) // c8 if c8 else cout
    return groups * Hout * blocks, cout * Hout * Wout


CONFIGS = {
    # 名称: (wide1 是否启用, 是否行缓冲复用)
    # 板端标定口径：8.76 M 笔 / 90 s —— 与 "wide1=0 + 行复用" 的模型吻合到 0.5%
    "calib": (False, True),
    # 当前 bit（conv2d_engine.h: K1_WIDE_COLS=1）
    "current": (True, True),
}


def collect(manifest, wide1, reuse):
    """返回 (读笔数_old, 读笔数_new, 写笔数_old, 写笔数_new, 读字节数, 明细)。"""
    bufs = manifest["buffers"]
    ops = [o for o in manifest["ops"] if o["needed"] and o["op"] == "conv"]
    rb_old = rb_new = wb_old = wb_new = rbytes = 0
    rows = []
    for op in ops:
        o0 = op["out"][0]
        H, W = op["h"], op["w"]                     # 输入 H/W
        Hout, Wout = bufs[o0]["h"], bufs[o0]["w"]   # 输出 H/W
        cin, cout = op["cin"], op["cout"]
        c8_in = 8 if cin % 8 == 0 else 0             # 例外张量（12ch）保持 CHW
        c8_out = 8 if cout % 8 == 0 else 0
        n0, b0 = read_bursts(H, W, Hout, Wout, cin, op["k"], op["s"],
                             op["d"], op["pad"], 0, wide1, reuse)
        n1, _ = read_bursts(H, W, Hout, Wout, cin, op["k"], op["s"],
                            op["d"], op["pad"], c8_in, wide1, reuse)
        m0, _ = write_bursts(Hout, Wout, cout, op["s"], 0)
        m1, _ = write_bursts(Hout, Wout, cout, op["s"], c8_out)
        rb_old += n0; rb_new += n1; wb_old += m0; wb_new += m1; rbytes += b0
        rows.append((op["name"], cin, cout, n0, n1, m0, m1))
    return rb_old, rb_new, wb_old, wb_new, rbytes, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=os.path.join(PS, "manifest_640.json"))
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    m = json.load(open(a.manifest))
    out = {}
    for name, (wide1, reuse) in CONFIGS.items():
        rb_old, rb_new, wb_old, wb_new, rbytes, rows = collect(m, wide1, reuse)
        out[name] = dict(read_old=rb_old, read_new=rb_new,
                         write_old=wb_old, write_new=wb_new,
                         bytes_read=rbytes, rows=rows)

    print("manifest: %s   卷积 %d 个   标定 10.3 us/事务\n"
          % (os.path.basename(a.manifest),
             sum(1 for o in m["ops"] if o["needed"] and o["op"] == "conv")))

    for name in ("calib", "current"):
        d = out[name]
        ro, rn, wo, wn = d["read_old"], d["read_new"], d["write_old"], d["write_new"]
        to, tn = ro + wo, rn + wn
        tag = {"calib": "标定口径（wide1=0 + 行复用，与板端 8.76 M 吻合）",
               "current": "当前 bit（K1_WIDE_COLS=1 + 行复用）"}[name]
        print("=" * 78)
        print(tag)
        print("  读（填充）: CHW %8.3f M 笔 (%.1f B/笔)  ->  C8 %7.3f M 笔 (%.1f B/笔)   ÷%.2f"
              % (ro / 1e6, d["bytes_read"] / ro, rn / 1e6, d["bytes_read"] / rn, ro / rn))
        print("  写        : CHW %8.3f M 笔                ->  C8 %7.3f M 笔                ÷%.2f"
              % (wo / 1e6, wn / 1e6, wo / wn))
        print("  读写合计  : CHW %8.3f M 笔                ->  C8 %7.3f M 笔                ÷%.2f"
              % (to / 1e6, tn / 1e6, to / tn))
        print("  填充耗时  : CHW %6.1f s  ->  C8 %5.1f s      （10.3 us/笔，仅读侧）"
              % (ro * US_PER_TXN / 1e6, rn * US_PER_TXN / 1e6))
        print("  整网事务  : CHW %6.1f s  ->  C8 %5.1f s      （读写都算）"
              % (to * US_PER_TXN / 1e6, tn * US_PER_TXN / 1e6))
        print()

    d = out["calib"]
    print("口径校验（calib 配置）: 板端实测填充 8.76 M 笔 / 90 s / 64.7 B/笔")
    print("  模型 %.3f M 笔（差 %+.1f%%），%.1f B/笔（差 %+.1f%%），%.0f MB"
          % (d["read_old"] / 1e6, 100 * (d["read_old"] / 8.76e6 - 1),
             d["bytes_read"] / d["read_old"],
             100 * (d["bytes_read"] / d["read_old"] / 64.7 - 1),
             d["bytes_read"] / 1e6))
    print()

    rows = sorted(d["rows"], key=lambda r: -(r[3] - r[4]))
    print("读侧笔数降幅最大的 8 层:")
    for name, cin, cout, n0, n1, m0, m1 in rows[:8]:
        print("  %-52s cin=%-3d %7d -> %5d 笔 (%.1fx)"
              % (name, cin, n0, n1, n0 / n1 if n1 else 0))
    print("\n未获益的层（cin 不是 8 的倍数 -> 保持 CHW）:")
    hit = False
    for name, cin, cout, n0, n1, m0, m1 in d["rows"]:
        if n0 == n1 and cin % 8:
            hit = True
            print("  %-52s cin=%-3d %7d 笔" % (name, cin, n0))
    if not hit:
        print("  （无）")
    print("\n写侧未获益的层（cout 不是 8 的倍数）:")
    for name, cin, cout, n0, n1, m0, m1 in d["rows"]:
        if m0 == m1 and cout % 8:
            print("  %-52s cout=%-3d %6d 笔" % (name, cout, m0))

    if a.json:
        slim = {k: {kk: vv for kk, vv in v.items() if kk != "rows"}
                for k, v in out.items()}
        slim["rows_calib"] = out["calib"]["rows"]
        json.dump(slim, open(a.json, "w"), indent=1)
        print("\n已写 %s" % a.json)


if __name__ == "__main__":
    main()
