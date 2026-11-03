# v58: 第2档(AXI/RO/软复位) + 四模式扫频(4x20s) + SLCR 真值表
import ctypes, json, mmap, os, time, subprocess
from pynq import Overlay, Clocks

BIT = "/tmp/probe_v58_v58a/design_1_v58.bit"
OUT = "/tmp/probe_v58_v58a/t3.json"
R = {"t_wall_start": time.strftime("%F %T")}
ov = Overlay(BIT, download=False)
fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
reg  = mmap.mmap(fd, 0x10000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0x400A0000)
slcr = mmap.mmap(fd, 0x1000,  mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0xF8000000)
def rd(o):   return ctypes.c_uint32.from_buffer(reg, o).value
def wr(o,v): ctypes.c_uint32.from_buffer(reg, o).value = v & 0xFFFFFFFF
def srd(o):  return ctypes.c_uint32.from_buffer(slcr, o).value
NAMES = [("CTRL",0x00),("STATUS",0x04),("VIDEO_MODE",0x08),("FB_ADDR_LO",0x0C),
         ("FB_STRIDE",0x14),("FB_W",0x18),("FRAME_ID",0x20),("FRAME_ID_LATCHED",0x24),
         ("PIXEL_FMT",0x28),("UNDERFLOW_CNT",0x2C),("PIX_HB",0x30)]
def snap(tag):
    d = {"t": time.strftime("%H:%M:%S"), "tag": tag}
    for n,o in NAMES: d[n] = "0x%08X" % rd(o)
    return d
def hb(window):
    t0 = time.perf_counter(); last = rd(0x30)&1; e=0;s=0;lock0=0
    while True:
        v = rd(0x30); s += 1
        if not (v>>1)&1: lock0 += 1
        b = v&1
        if b != last: e += 1; last = b
        if time.perf_counter()-t0 >= window: break
    d = time.perf_counter()-t0
    return {"window_s": round(d,6), "edges": e, "samples": s,
            "f_pix_hz": round(e*65536/d,1), "half_ms": round(d/e*1000,4) if e else None,
            "lock_absent": lock0}

# ---------------- 第 2 档 ----------------
S = R.setdefault("tier2", {})
S["before"] = snap("tier2_before")
S["read_six_raw"] = {n: "0x%08X" % rd(o) for n,o in
                     (("0x00",0x00),("0x04",0x04),("0x08",0x08),
                      ("0x24",0x24),("0x2C",0x2C),("0x30",0x30))}
wr(0x14, 3840); S["ctrl_write_control_FB_STRIDE"] = {"wrote":"0x%08X"%3840,
    "read_back":"0x%08X"%rd(0x14), "ok": rd(0x14)==3840}
wr(0x14, 0)
u_pre = rd(0x2C); wr(0x2C, 0xFFFFFFFF); time.sleep(0.01); u_post = rd(0x2C)
S["ro_test_0x2C"] = {"before":"0x%08X"%u_pre, "wrote":"0xFFFFFFFF",
                     "after":"0x%08X"%u_post, "unchanged": u_pre==u_post}
S["hb_before_reset"] = hb(0.100)
wr(0x00, 0x02); S["ctrl_rb_after_srst"] = "0x%08X" % rd(0x00)
time.sleep(0.05)
S["after_soft_reset"] = {"CTRL":"0x%08X"%rd(0x00), "STATUS":"0x%08X"%rd(0x04),
                         "UNDERFLOW_CNT":"0x%08X"%rd(0x2C), "PIX_HB":"0x%08X"%rd(0x30),
                         "uf_is_zero": rd(0x2C)==0}
S["hb_after_reset"] = hb(0.100)

# ---------------- 四模式扫频 ----------------
MODES = [(1,"1080p60",148.4375),(0,"720p60",74.21875),(2,"1080p30",74.21875),(3,"720p30",37.109375)]
SW = R.setdefault("sweep", [])
for m, name, exp in MODES:
    seg = {"mode": m, "name": name, "expect_mhz": exp,
           "wall_start": time.strftime("%H:%M:%S"),
           "vmode_before_write": "0x%08X" % rd(0x08)}
    wr(0x00, 0x00)                 # enable=0
    time.sleep(0.05)
    wr(0x08, m)                    # 切模式
    time.sleep(0.20)               # >=3 帧（最慢 30 fps 时 100 ms）
    seg["vmode_readback"] = "0x%08X" % rd(0x08)
    wr(0x00, 0x05)                 # enable=1 + test_pattern=1
    seg["ctrl_readback"] = "0x%08X" % rd(0x00)
    t0 = time.perf_counter(); nextm = 2.0
    last = rd(0x30)&1; edges = 0; samples = 0; init0 = 0; lock0 = 0; marks = []
    uf_first = rd(0x2C); fid0 = rd(0x20); fidl0 = rd(0x24)
    while True:
        el = time.perf_counter() - t0
        if el >= 20.0: break
        v = rd(0x30); st = rd(0x04); ufc = rd(0x2C)
        samples += 1
        if not (st & 1): init0 += 1
        if not (v >> 1) & 1: lock0 += 1
        b = v & 1
        if b != last: edges += 1; last = b
        if el >= nextm:
            marks.append({"t_s": round(el,3), "wall": time.strftime("%H:%M:%S"),
                          "STATUS":"0x%08X"%st, "init_done": st&1,
                          "uf_bit": (st>>2)&1, "UFCNT":"0x%08X"%ufc, "PIX_HB":"0x%08X"%v})
            nextm += 2.0
        time.sleep(0.0005)
    seg["dwell_s"] = round(time.perf_counter()-t0, 3)
    seg["samples"] = samples
    seg["init_done_zero_samples"] = init0
    seg["lock_absent_samples"] = lock0
    seg["uf_first"] = "0x%08X" % uf_first
    seg["uf_last"] = "0x%08X" % rd(0x2C)
    seg["uf_growth"] = rd(0x2C) - uf_first
    seg["frame_id_0x20"] = "0x%08X" % rd(0x20)
    seg["fid_latched_0x24"] = "0x%08X" % rd(0x24)
    seg["fid_advanced"] = (rd(0x20) != fid0) or (rd(0x24) != fidl0)
    seg["hb_2s"] = hb(2.0)
    seg["hb_2s"]["dev_pct_vs_expect"] = round((seg["hb_2s"]["f_pix_hz"]/1e6 - exp)/exp*100, 3)
    seg["marks_every_2s"] = marks
    seg["end_regs"] = snap("mode%d_end" % m)
    seg["wall_end"] = time.strftime("%H:%M:%S")
    SW.append(seg)

# ---------------- 恢复原状 ----------------
wr(0x00, 0x00); time.sleep(0.05)
wr(0x08, 3); time.sleep(0.20)
wr(0x00, 0x15)
R["restored"] = snap("restored_mode3_ctrl0x15")

# ---------------- SLCR 真值表 ----------------
U = R.setdefault("slcr", {})
for off, nm in ((0x100,"FPLL_CTRL"),(0x104,"FPLL_CFG"),(0x108,"IO_PLL_CTRL"),
                (0x10C,"IO_PLL_CFG"),(0x170,"FCLK0_CTRL"),(0x174,"FCLK1_CTRL")):
    U["raw_0x%03X_%s" % (off, nm)] = "0x%08X" % srd(off)
IO_PLL = 1800.0
def dec(v):
    d0 = (v>>8)&0x3F; d1a = (v>>14)&0x3F; d1b = v&0x3F
    return {"div0_13_8": d0, "div1_19_14": d1a, "div1_5_0_alt": d1b,
            "srcsel_5_4": (v>>4)&3, "clkact0": v&1,
            "f_real_if_div1_19_14": round(IO_PLL/(d0*d1a),3) if d0 and d1a else None,
            "f_real_if_div1_5_0":   round(IO_PLL/(d0*d1b),3) if d0 and d1b else None}
U["decode_at_entry"] = dec(srd(0x170))
rows = []
for want in (125, 150, 75, 62.5, 100):
    row = {"requested": want}
    try:
        Clocks.fclk0_mhz = want; row["write_error"] = None
    except Exception as e:
        row["write_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    time.sleep(0.05)
    try: row["reported_mhz"] = Clocks.fclk0_mhz
    except Exception as e: row["reported_mhz"] = repr(e)
    v = srd(0x170); row["FCLK0_CTRL"] = "0x%08X" % v; row["dec"] = dec(v)
    rows.append(row)
U["table"] = rows
try:
    Clocks.fclk0_mhz = 125; U["restore_error"] = None
except Exception as e:
    U["restore_error"] = repr(e)
time.sleep(0.05)
U["restored"] = {"written": 125, "reported": Clocks.fclk0_mhz,
                 "FCLK0_CTRL": "0x%08X" % srd(0x170), "dec": dec(srd(0x170))}
U["final_hdmi_regs"] = snap("after_all_clk_changes")
try:
    U["dmesg_tail20"] = subprocess.run(["dmesg"], capture_output=True, text=True,
                                       timeout=10).stdout.splitlines()[-20:]
except Exception as e:
    U["dmesg_tail20"] = repr(e)
R["t_wall_end"] = time.strftime("%F %T")
json.dump(R, open(OUT,"w"), indent=1, ensure_ascii=False)

print("### TIER2 ###")
print("six raw:", S["read_six_raw"])
print("ctrl-write control FB_STRIDE:", S["ctrl_write_control_FB_STRIDE"])
print("0x2C RO:", S["ro_test_0x2C"])
print("hb before reset:", S["hb_before_reset"])
print("CTRL rb after srst:", S["ctrl_rb_after_srst"], " after:", S["after_soft_reset"])
print("hb after reset:", S["hb_after_reset"])
print("### SWEEP (each 20 s) ###")
for s in SW:
    h = s["hb_2s"]
    print("mode %d %-8s vmode_rb=%s ctrl_rb=%s | hb2s edges=%d f=%.2f MHz dev=%+.3f%% half=%.4f ms | "
          "init_done==0 samples=%d lock==0 samples=%d | UFCNT %s->%s (+%d) | FID %s/%s advanced=%s | %s~%s" %
          (s["mode"], s["name"], s["vmode_readback"], s["ctrl_readback"], h["edges"],
           h["f_pix_hz"]/1e6, s["hb_2s"]["dev_pct_vs_expect"], h["half_ms"],
           s["init_done_zero_samples"], s["lock_absent_samples"],
           s["uf_first"], s["uf_last"], s["uf_growth"],
           s["frame_id_0x20"], s["fid_latched_0x24"], s["fid_advanced"],
           s["wall_start"], s["wall_end"]))
    for m in s["marks_every_2s"]:
        print("    t=%5.1fs %s STATUS=%s init=%d UFCNT=%s HB=%s" %
              (m["t_s"], m["wall"], m["STATUS"], m["init_done"], m["UFCNT"], m["PIX_HB"]))
print("restored:", {k:R["restored"][k] for k in ("CTRL","STATUS","VIDEO_MODE","UNDERFLOW_CNT","PIX_HB")})
print("### SLCR ###")
for k in ("raw_0x100_FPLL_CTRL","raw_0x104_FPLL_CFG","raw_0x108_IO_PLL_CTRL",
          "raw_0x10C_IO_PLL_CFG","raw_0x170_FCLK0_CTRL","raw_0x174_FCLK1_CTRL"):
    print(" ", k, U[k])
print(" decode@entry:", U["decode_at_entry"])
for r in U["table"]:
    d = r["dec"]
    print("  want=%-6s err=%s reported=%s reg=%s div0=%s div1[19:14]=%s div1[5:0]=%s "
          "f(1800/div0/div1_19_14)=%s f(1800/div0/div1_5_0)=%s" %
          (r["requested"], r["write_error"], r["reported_mhz"], r["FCLK0_CTRL"],
           d["div0_13_8"], d["div1_19_14"], d["div1_5_0_alt"],
           d["f_real_if_div1_19_14"], d["f_real_if_div1_5_0"]))
print(" restored:", U["restored"])
print(" final hdmi:", {k:U["final_hdmi_regs"][k] for k in ("CTRL","STATUS","VIDEO_MODE","UNDERFLOW_CNT","PIX_HB")})
print("JSON ->", OUT)
