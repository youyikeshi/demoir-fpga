# 第 4 档（修正版）：test_pattern=0 才有像素流；推 3 帧，每帧 5 s，末帧保持 40 s
import ctypes, json, mmap, os, sys, time, subprocess
import numpy as np
from pynq import Overlay
OUT="/tmp/probe_v58_v58a/t5.json"
R={"t_wall_start":time.strftime("%F %T")}
ov=Overlay("/tmp/probe_v58_v58a/design_1_v58.bit", download=False)
fd=os.open("/dev/mem", os.O_RDWR|os.O_SYNC)
m=mmap.mmap(fd,0x10000,mmap.MAP_SHARED,mmap.PROT_READ|mmap.PROT_WRITE,offset=0x400A0000)
def rd(o): return ctypes.c_uint32.from_buffer(m,o).value
def wr(o,v): ctypes.c_uint32.from_buffer(m,o).value=v&0xFFFFFFFF
def regs():
    return {"CTRL":"0x%08X"%rd(0x00),"STATUS":"0x%08X"%rd(0x04),"VMODE":"0x%08X"%rd(0x08),
            "FB_ADDR":"0x%08X"%rd(0x0C),"STRIDE":"0x%08X"%rd(0x14),"FB_W":"0x%08X"%rd(0x18),
            "FB_H":"0x%08X"%rd(0x1C),"FID":"0x%08X"%rd(0x20),"LATCHED":"0x%08X"%rd(0x24),
            "UFCNT":rd(0x2C),"PIX_HB":"0x%08X"%rd(0x30)}
sys.path.insert(0,"/tmp/probe_v58_v58a")
import display_demo as dd
R["cma_before_alloc"]=int([l for l in open("/proc/meminfo") if l.startswith("CmaFree")][0].split()[1])
R["uptime_s"]=round(float(open("/proc/uptime").read().split()[0]),1)
pipe=dd.DisplayPipeline(mode=0).setup()
R["fb_addr"]=hex(pipe.fb_addr); R["block_bytes"]=pipe.block_bytes
R["cma_after_alloc"]=int([l for l in open("/proc/meminfo") if l.startswith("CmaFree")][0].split()[1])

wr(0x00,0x01)            # enable=1, test_pattern=0, motion=0  ← 关键：打开扫描器像素流
time.sleep(0.30)         # 让 scan_sel 在 vsync 边界采纳 cfg_test=0
R["after_test_off"]=regs()

def make_orig(i):
    yy,xx=np.mgrid[0:256,0:256]
    img=np.zeros((256,256,3),np.uint8)
    img[...,0]=(xx&0xFF).astype(np.uint8)
    img[...,1]=(yy&0xFF).astype(np.uint8)
    img[...,2]=(((xx+yy)//2)&0xFF).astype(np.uint8)
    x0=(i*70)%224
    img[:,x0:x0+32,:]=255
    return img
def make_proc(o):
    k=9; pad=k//2; f=o.astype(np.float32); acc=np.zeros_like(f)
    for dy in range(-pad,pad+1):
        for dx in range(-pad,pad+1):
            acc+=np.roll(np.roll(f,dy,axis=0),dx,axis=1)
    return (acc/(k*k)).astype(np.uint8)

uf=[]
uf.append(("before_frame1", rd(0x2C), time.strftime("%H:%M:%S")))
frames=[]
for i in (1,2,3):
    o=make_orig(i); p=make_proc(o)
    fid=pipe.show(o,p); ok=pipe.wait(fid,1.0)
    uf.append(("after_frame%d"%i, rd(0x2C), time.strftime("%H:%M:%S")))
    frames.append({"i":i,"fid":fid,"latched":"0x%08X"%rd(0x24),"match":rd(0x24)==fid,
                   "wait_ok":bool(ok),"wall":time.strftime("%H:%M:%S"),"UFCNT":rd(0x2C),
                   "STATUS":"0x%08X"%rd(0x04),"PIX_HB":"0x%08X"%rd(0x30)})
    time.sleep(5.0)
# 末帧保持 40 s，期间每 5 s 读一次 0x2C（稳态判据）
hold=[]
for k in range(8):
    time.sleep(5.0)
    hold.append({"t":time.strftime("%H:%M:%S"),"UFCNT":rd(0x2C),
                 "LATCHED":"0x%08X"%rd(0x24),"STATUS":"0x%08X"%rd(0x04),
                 "PIX_HB":"0x%08X"%rd(0x30)})
R["frames"]=frames; R["uf_points"]=uf; R["hold"]=hold
R["regs_end"]=regs()
# 恢复彩条基准（enable+test+motion），让板子留在可人眼复核的状态
wr(0x00,0x15)
R["regs_restored_bars"]=regs()
R["t_wall_end"]=time.strftime("%F %T")
json.dump(R,open(OUT,"w"),indent=1,ensure_ascii=False)
print("CmaFree before/after alloc (kB):", R["cma_before_alloc"], "/", R["cma_after_alloc"],
      " uptime=%.0fs" % R["uptime_s"])
print("fb_addr=%s block_bytes=%d" % (R["fb_addr"], R["block_bytes"]))
print("after test_off:", R["after_test_off"])
for f in frames:
    print("frame%d %s fid=%d latched=%s match=%s wait_ok=%s UFCNT=%d STATUS=%s HB=%s" %
          (f["i"],f["wall"],f["fid"],f["latched"],f["match"],f["wait_ok"],f["UFCNT"],f["STATUS"],f["PIX_HB"]))
print("uf points:", uf)
for h in hold: print("  hold", h)
print("regs_end:", R["regs_end"])
print("restored bars:", R["regs_restored_bars"])
print("JSON ->", OUT)
