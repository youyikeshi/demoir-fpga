#!/usr/bin/env python3
"""只做一件事：mmap /dev/mem 到 0x400A0000，写【一个】偏移，回读一次，打印，退出。
用法: wr.py <offset> <value>
纪律：一次进程只写一个寄存器，必须被 timeout 包住（2026-11-03 挂死事故）。
"""
import sys, os, mmap, struct, time

BASE = 0x400A0000
PAGE = 0x1000

off = int(sys.argv[1], 0)
val = int(sys.argv[2], 0) & 0xFFFFFFFF
assert 0 <= off <= PAGE - 4, "offset 越界"
assert BASE % PAGE == 0, "base 未页对齐"

fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
try:
    m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                  offset=BASE)
    before = struct.unpack('<I', m[off:off + 4])[0]
    m[off:off + 4] = struct.pack('<I', val)
    after = struct.unpack('<I', m[off:off + 4])[0]
    print("WR off=0x%02X write=0x%08X  before=0x%08X readback=0x%08X %s"
          % (off, val, before, after, "OK" if after == val else "MISMATCH"),
          flush=True)
finally:
    os.close(fd)
sys.exit(0)
