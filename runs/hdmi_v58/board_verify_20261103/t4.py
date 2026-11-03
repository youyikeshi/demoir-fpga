# v58: ① 默认模式改 720p60(mode0) 并保持 20 s；② 第 4 档真画面(左原图/右处理图)；③ SLCR 表
import ctypes, json, mmap, os, sys, time, subprocess
import numpy as np
from pynq import Overlay, Clocks, allocate

BIT = "/tmp/probe_v58_v58a/design_1_v58.bit"
OUT = "/tmp/probe_v58_v58a/t4.json"
R = {"t_wall_start": time.strftime("%F %T")}
ov = Overlay(BIT, download=False)
fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
regm = mmap.mmap(fd, 0x10000, mmap.MAP_SHARED, mmap.PROT_READ|mmap.PROT_WRITE, offset=0x400A0000)
slcr = mmap.mmap(fd, 0x1000,  mmap.MAP_SHARED, mmap.PROT_READ|mmap.PROT_WRITE, offset=0xF8000000)
def rd(o):   return ctypes.c_uint32.from_buffer(regm, o).value
def wr(o,v): ctypes.c_uint32.from_buffer(regm, o).value = v & 0xFFFFFFFF
def srd(o):  return ctypes.c_uint32.from_buffer(slcr, o).value
def meminfo():
    d={}
    for ln in open("/proc/meminfo"):
        k,v = ln.split(":",1)
        if k in ("CmaTotal","CmaFree","MemAvailable"): d[k]=int(v.split()[0])
    return d
def uptime(): return float(open("/proc/uptime").read().split()[0])
def hb(window):
    t0=time.perf_counter(); last=rd(0x30)&1; e=0;s=0;lock0=0
    while True:
        v=rd(0x30); s+=1
        if not (v>>1)&1: lock0+=1
        b=v&1
        if b!=last: e+=1; last=b
        if time.perf_counter()-t0>=window: break
    d=time.perf_counter()-t0
    return {"window_s":round(d,6),"edges":e,"samples":s,"f_pix_hz":round(e*65536/d,1),
            "half_ms":round(d/e*1000,4) if e else None,"lock_absent":lock0}

# ---------------- ① 720p60 默认 + 20 s 观察 -------------
M={}
wr(0x00,0x00); time.sleep(0.05); wr(0x08,0); time.sleep(0.20); wr(0x00,0x15)
M["switch_wall"]=time.strftime("%H:%M:%S"); M["vmode_rb"]="0x%08X"%rd(0x08); M["ctrl_rb"]="0x%08X"%rd(0x00)
t0=time.perf_counter(); nm=2.0; marks=[]
while True:
    el=time.perf_counter()-t0
    if el>=20.0: break
    st=rd(0x04)
    if el>=nm:
        marks.append({"t_s":round(el,3),"wall":time.strftime("%H:%M:%S"),"STATUS":"0x%08X"%st,
                      "init_done":st&1,"UFCNT":"0x%08X"%rd(0x2C),"PIX_HB":"0x%08X"%rd(0x30)})
        nm+=2.0
    time.sleep(0.002)
M["hold_s"]=round(time.perf_counter()-t0,3); M["marks"]=marks
M["hb_2s"]=hb(2.0); M["hb_2s"]["dev_pct"]=round((M["hb_2s"]["f_pix_hz"]/1e6-74.21875)/74.21875*100,3)
M["wall_end"]=time.strftime("%H:%M:%S")
R["stage1_mode0"]=M

# ---------------- ② 第 4 档：真画面 -------------
D={}
D["uptime_before_alloc_s"]=round(uptime(),1)
D["meminfo_before_alloc"]=meminfo()
sys.path.insert(0,"/tmp/probe_v58_v58a")
import display_demo as dd
pipe=None
try:
    pipe = dd.DisplayPipeline(mode=0)
    alive, mhz = pipe.pix_alive(100)
    D["pix_alive_before_setup"]={"alive":bool(alive),"f_pix_mhz":round(float(mhz),4)}
    t_setup=time.perf_counter(); pipe.setup(); D["setup_s"]=round(time.perf_counter()-t_setup,3)
    D["fb_addr"]=hex(pipe.fb_addr); D["block_bytes"]=pipe.block_bytes
    D["meminfo_after_alloc"]=meminfo()
    D["uptime_after_alloc_s"]=round(uptime(),1)
    D["alloc_delta_cmafree_kb"]=D["meminfo_before_alloc"]["CmaFree"]-D["meminfo_after_alloc"]["CmaFree"]

    def make_orig(i):
        yy,xx = np.mgrid[0:256,0:256]
        img=np.zeros((256,256,3),np.uint8)
        img[...,0]=(xx & 0xFF).astype(np.uint8)
        img[...,1]=(yy & 0xFF).astype(np.uint8)
        img[...,2]=(((xx+yy)//2) & 0xFF).astype(np.uint8)
        x0=(i*70)%224
        img[:, x0:x0+32, :]=255
        return img
    def make_proc(orig):
        k=9; pad=k//2; f=orig.astype(np.float32); acc=np.zeros_like(f)
        for dy in range(-pad,pad+1):
            for dx in range(-pad,pad+1):
                acc += np.roll(np.roll(f,dy,axis=0),dx,axis=1)
        return (acc/(k*k)).astype(np.uint8)

    frames=[]
    for i in (1,2,3):
        o=make_orig(i); p=make_proc(o)
        fid=pipe.show(o,p)
        ok=pipe.wait(fid,1.0)
        frames.append({"i":i,"fid_written":fid,"wait_ok":bool(ok),
                       "0x24_latched":"0x%08X"%rd(0x24),"0x20_frame_id":"0x%08X"%rd(0x20),
                       "latched_matches": rd(0x24)==fid,
                       "STATUS":"0x%08X"%rd(0x04),"UFCNT":"0x%08X"%rd(0x2C),
                       "PIX_HB":"0x%08X"%rd(0x30),"wall":time.strftime("%H:%M:%S")})
        time.sleep(5.0)
    D["frames"]=frames
    # 稳态：隔 2 帧再读两次 0x2C
    a=rd(0x2C); time.sleep(2.0); b=rd(0x2C)
    D["uf_steady"]={"read1":a,"read2_after_2s":b,"grew":b-a}
    D["pix_alive_after"]=dict(zip(("alive","f_pix_mhz"),
                            (lambda r:(bool(r[0]),round(float(r[1]),4)))(pipe.pix_alive(200))))
    st=getattr(pipe,"stats",None)
    if callable(st):
        try: D["pipe_stats"]=st()
        except Exception as e: D["pipe_stats"]=repr(e)
    D["final_regs"]={"CTRL":"0x%08X"%rd(0x00),"STATUS":"0x%08X"%rd(0x04),
                     "VIDEO_MODE":"0x%08X"%rd(0x08),"FB_ADDR":"0x%08X"%rd(0x0C),
                     "FB_STRIDE":"0x%08X"%rd(0x14),"FB_W":"0x%08X"%rd(0x18),
                     "FB_H":"0x%08X"%rd(0x1C),"FRAME_ID":"0x%08X"%rd(0x20),
                     "LATCHED":"0x%08X"%rd(0x24),"UFCNT":"0x%08X"%rd(0x2C),
                     "PIX_HB":"0x%08X"%rd(0x30)}
except Exception as e:
    import traceback
    D["error"]="%s: %s" % (type(e).__name__, str(e)[:300])
    D["traceback"]=traceback.format_exc()[-1200:]
    D["meminfo_at_error"]=meminfo()
R["stage2_tier4"]=D

# ---------------- ③ SLCR 表 ----------------
U={}
for off,nm in ((0x100,"FPLL_CTRL"),(0x104,"FPLL_CFG"),(0x108,"IO_PLL_CTRL"),
               (0x10C,"IO_PLL_CFG"),(0x170,"FCLK0_CTRL")):
    U["raw_0x%03X_%s"%(off,nm)]="0x%08X"%srd(off)
def dec(v):
    d0=(v>>8)&0x3F; d1a=(v>>14)&0x3F; d1b=(v>>20)&0x3F
    prod=(d0*d1a) or (d0*d1b)
    return {"div0_13_8":d0,"div1_19_14":d1a,"div2_25_20":d1b,
            "product_used":prod,
            "f_if_io1800":round(1800/prod,3) if prod else None,
            "f_if_io1000":round(1000/prod,3) if prod else None,
            "f_if_pynq1500":round(1500/prod,3) if prod else None}
U["decode_at_entry"]=dec(srd(0x170))
rows=[]
for want in (125,150,75,62.5,100):
    r={"requested":want}
    try: Clocks.fclk0_mhz=want; r["err"]=None
    except Exception as e: r["err"]="%s: %s"%(type(e).__name__,str(e)[:100])
    time.sleep(0.05)
    try: r["reported"]=Clocks.fclk0_mhz
    except Exception as e: r["reported"]=repr(e)
    v=srd(0x170); r["FCLK0_CTRL"]="0x%08X"%v; r["dec"]=dec(v); rows.append(r)
U["table"]=rows
try: Clocks.fclk0_mhz=125; U["restore_err"]=None
except Exception as e: U["restore_err"]=repr(e)
time.sleep(0.05)
U["restored"]={"written":125,"reported":Clocks.fclk0_mhz,"dec":dec(srd(0x170))}
R["stage3_slcr"]=U
try: R["dmesg_tail20"]=subprocess.run(["dmesg"],capture_output=True,text=True,timeout=10).stdout.splitlines()[-20:]
except Exception as e: R["dmesg_tail20"]=repr(e)
R["t_wall_end"]=time.strftime("%F %T")
json.dump(R,open(OUT,"w"),indent=1,ensure_ascii=False)

print("### ① mode0(720p60) 20 s ###")
print(" switch %s vmode_rb=%s ctrl_rb=%s hold=%.1fs" % (M["switch_wall"],M["vmode_rb"],M["ctrl_rb"],M["hold_s"]))
for m in M["marks"]:
    print("   t=%5.1fs %s STATUS=%s init=%d UFCNT=%s HB=%s" % (m["t_s"],m["wall"],m["STATUS"],m["init_done"],m["UFCNT"],m["PIX_HB"]))
print(" hb2s: edges=%d f=%.3f MHz dev=%+.3f%% half=%.4f ms lock_absent=%d" %
      (M["hb_2s"]["edges"],M["hb_2s"]["f_pix_hz"]/1e6,M["hb_2s"]["dev_pct"],M["hb_2s"]["half_ms"],M["hb_2s"]["lock_absent"]))
print("### ② tier4 ###")
print(" uptime=%.1fs CmaFree before=%s after=%s delta=%s kB" % (D.get("uptime_before_alloc_s",-1),
      D.get("meminfo_before_alloc",{}).get("CmaFree"), D.get("meminfo_after_alloc",{}).get("CmaFree"),
      D.get("alloc_delta_cmafree_kb")))
print(" pix_alive_before_setup:", D.get("pix_alive_before_setup"), " setup_s:", D.get("setup_s"),
      " fb_addr:", D.get("fb_addr"), " block_bytes:", D.get("block_bytes"))
if "error" in D:
    print(" ERROR:", D["error"]); print(D["traceback"])
for f in D.get("frames",[]):
    print("   frame%d %s fid=%d latched=%s match=%s wait_ok=%s STATUS=%s UFCNT=%s HB=%s" %
          (f["i"],f["wall"],f["fid_written"],f["0x24_latched"],f["latched_matches"],f["wait_ok"],
           f["STATUS"],f["UFCNT"],f["PIX_HB"]))
print(" uf_steady:", D.get("uf_steady"), " pix_alive_after:", D.get("pix_alive_after"))
print(" final regs:", D.get("final_regs"))
print("### ③ SLCR ###")
for k,v in U.items():
    if k.startswith("raw_"): print("  ",k,v)
print("  decode@entry:",U["decode_at_entry"])
for r in U["table"]:
    d=r["dec"]
    print("   want=%-6s reported=%-6s reg=%s div0=%d div1=%d div2=%d prod=%d f(1800)=%s f(1000)=%s f(1500)=%s" %
          (r["requested"],r["reported"],r["FCLK0_CTRL"],d["div0_13_8"],d["div1_19_14"],
           d["div2_25_20"],d["product_used"],d["f_if_io1800"],d["f_if_io1000"],d["f_if_pynq1500"]))
print("  restored:",U["restored"])
print("JSON ->",OUT)
