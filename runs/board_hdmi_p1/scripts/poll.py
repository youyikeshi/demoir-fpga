#!/usr/bin/env python3
"""只读 0x30 PIX_HB，在一段**有界**的短窗口内密集采样，用时间戳估算心跳翻转周期
⇒ 反推像素时钟频率（hb_cnt[16] 的半周期 = 2^16 个像素时钟）。

为什么允许多次访问：仍然是**同一个**"体检寄存器"，仍然是独立短进程 + timeout 包住；
目的不是"多读几个寄存器"，而是在**同一次采样**里拿到翻转的**时间精度**，
否则单靠两次读只能证明"在动"，无法区分 148.4375 MHz 与 222.66 MHz。
用法: poll.py [秒数，默认 0.30]
"""
import sys, os, mmap, struct, time, statistics

BASE = 0x400A0000
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 0.30

fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
              offset=BASE)

edges = []          # pix_hb 翻转时刻（ns）
last_b = None
last_lk = None
n = 0
vals = set()
t_end = time.monotonic() + DUR
t0 = time.monotonic()
while time.monotonic() < t_end:
    v = struct.unpack('<I', m[0x30:0x34])[0]
    n += 1
    b = v & 1
    lk = (v >> 1) & 1
    vals.add(v)
    if last_b is not None and b != last_b:
        edges.append(time.monotonic_ns())
    last_b = b
    last_lk = lk
elapsed = time.monotonic() - t0
os.close(fd)

print("POLL dur=%.3f s samples=%d distinct_vals=%s last_bit1=%s bit0=%d bit1=%d"
      % (elapsed, n, [hex(x) for x in sorted(vals)], last_lk, last_b, last_lk),
      flush=True)
print("POLL edges=%d" % len(edges), flush=True)
if len(edges) >= 3:
    d = [(edges[i + 1] - edges[i]) / 1e6 for i in range(len(edges) - 1)]
    med = statistics.median(d)
    span_ms = (edges[-1] - edges[0]) / 1e6     # 首末沿跨度（不受窗口边界影响）
    span_period = span_ms / (len(edges) - 1)
    for tag, half in (("median", med / 1000.0), ("span", span_period / 1000.0)):
        f = (1 << 16) / half if half > 0 else 0.0
        print("POLL[%s] half_period_ms=%.6f implied_pix_clk_MHz=%.4f"
              % (tag, half * 1000.0, f / 1e6), flush=True)
    print("POLL half_period_ms med=%.6f min=%.6f max=%.6f span_ms=%.3f edges=%d"
          % (med, min(d), max(d), span_ms, len(edges)), flush=True)
else:
    print("POLL too_few_edges=%d (窗口太短或心跳不动)" % len(edges), flush=True)
sys.exit(0)
