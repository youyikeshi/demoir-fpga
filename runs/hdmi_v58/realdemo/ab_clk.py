import ctypes, mmap, os, time
fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
h = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ|mmap.PROT_WRITE, offset=0x400A0000)
s = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ|mmap.PROT_WRITE, offset=0xF8000000)
g = lambda mm,o: ctypes.c_uint32.from_buffer(mm,o).value
w = lambda mm,o,v: setattr(ctypes.c_uint32.from_buffer(mm,o),'value',v)
def setfclk(v, name):
    w(s,0x170,v); time.sleep(0.25)
    print("  FCLK0_CTRL=%#010x -> %s (true %.2f MHz)" % (g(s,0x170), name, 1000.0/((g(s,0x170)>>8)&0x3F)))
def probe(sec=5.0):
    time.sleep(0.6)
    u0=g(h,0x2C); t0=time.time(); time.sleep(sec); u1=g(h,0x2C); dt=time.time()-t0
    return u0,u1,dt,(u1-u0)/dt
print("base: CTRL=%#x UFCNT=%d FCLK0=%#x" % (g(h,0),g(h,0x2C),g(s,0x170)))
w(h,0,0x15); time.sleep(0.2)
for val,name in ((0x00101400,"50.00 MHz"),(0x00100C00,"83.33 MHz")):
    w(h,0,0x15); setfclk(val,name)
    w(h,0,0x01); time.sleep(0.35)
    u0,u1,dt,rate=probe(5.0)
    print("  [%s] CTRL=%#x UFCNT %d -> %d  delta=%d in %.2fs  (%.0f lines/s)  STATUS=%#x"
          % (name, g(h,0), u0, u1, u1-u0, dt, rate, g(h,4)))
# 安全收尾：彩条 + 50 MHz
w(h,0,0x15); setfclk(0x00101400,"50.00 MHz"); time.sleep(0.3)
print("final: CTRL=%#x UFCNT=%d STATUS=%#x PIX_HB=%#x" % (g(h,0),g(h,0x2C),g(h,4),g(h,0x30)))
