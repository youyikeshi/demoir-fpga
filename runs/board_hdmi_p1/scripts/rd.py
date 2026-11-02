#!/usr/bin/env python3
"""只做一件事：mmap /dev/mem 到 0x400A0000，读【一个】偏移，打印，退出。
用法: rd.py <offset, e.g. 0x30>
纪律：一次进程只读一个寄存器，必须被 timeout 包住（2026-11-03 挂死事故）。"""
import sys, os, mmap, struct

BASE = 0x400A0000
PAGE = 0x1000

off = int(sys.argv[1], 0)
assert 0 <= off <= PAGE - 4, "offset 越界"
assert BASE % PAGE == 0, "base 未页对齐"

t0 = __import__("time").time()
fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
try:
    m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                  offset=BASE)
    val = struct.unpack('<I', m[off:off + 4])[0]
    print("RD off=0x%02X val=0x%08X  bin=%s  elapsed=%.3f s"
          % (off, val, format(val, '032b'), __import__("time").time() - t0),
          flush=True)
finally:
    os.close(fd)
sys.exit(0)
