# v58 第 0/1 档：Overlay 加载 + 心跳（有界窗口 poll）
import ctypes, json, mmap, os, struct, sys, time, subprocess

BASE = 0x400A0000
OUT  = "/tmp/probe_v58_v58a/t01.json"
R = {"stage": "t01", "t_wall": time.strftime("%F %T")}

def meminfo():
    d = {}
    for ln in open("/proc/meminfo"):
        k, v = ln.split(":", 1)
        if k in ("CmaTotal", "CmaFree", "MemAvailable", "MemTotal"):
            d[k] = int(v.split()[0])
    return d

R["meminfo_pre_load"] = meminfo()
t0 = time.time()
from pynq import Overlay, Clocks
DL = (len(sys.argv) > 1 and sys.argv[1] == "download")
ov = Overlay("/tmp/probe_v58_v58a/design_1_v58.bit", download=DL)
R["download_arg"] = DL
R["load_s"] = round(time.time() - t0, 2)
def _ip(v):
    try:
        return {"physical_address": hex(v["physical_address"]),
                "addr_range": v["addr_range"]}
    except Exception:
        return str(v)[:80]
R["ip_dict"] = {k: _ip(v) for k, v in ov.ip_dict.items()}
R["fclk0_mhz_reported"] = Clocks.fclk0_mhz
R["fclk1_mhz_reported"] = Clocks.fclk1_mhz
R["meminfo_post_load"] = meminfo()

fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
mm = mmap.mmap(fd, 0x10000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=BASE)
def rd32(o): return ctypes.c_uint32.from_buffer(mm, o).value
def wr32(o, v): ctypes.c_uint32.from_buffer(mm, o).value = v

def snap():
    return {n: "0x%08X" % rd32(o) for n, o in
            (("CTRL",0x00),("STATUS",0x04),("VMODE",0x08),("FID_LAT",0x24),
             ("UFCNT",0x2C),("PIX_HB",0x30))}

def hb(window_s):
    """有界窗口 poll 0x30[0]，返回翻转沿数与实测窗口。"""
    t0 = time.perf_counter()
    last = rd32(0x30) & 1
    edges = 0; samples = 0; seen = set(); lockz = 0
    while True:
        v = rd32(0x30)
        b = v & 1
        seen.add(v)
        if not (v >> 1) & 1: lockz += 1
        if b != last:
            edges += 1; last = b
        samples += 1
        if (time.perf_counter() - t0) >= window_s:
            break
    dur = time.perf_counter() - t0
    return {"window_s": round(dur, 6), "edges": edges, "samples": samples,
            "f_pix_hz": round(edges * 65536 / dur, 1),
            "half_period_ms": round(dur / edges * 1000, 4) if edges else None,
            "lock_absent_samples": lockz,
            "distinct_raw": ["0x%08X" % x for x in sorted(seen)][:8]}

def set_mode(m, settle=0.05):
    wr32(0x08, m); time.sleep(settle)

R["boot_snapshot"] = snap()
R["tier0_status_bit0_init_done"] = rd32(0x04) & 1

# ---- 第 1 档：默认 mode=3（720p30, 期望 37.109375 MHz）
set_mode(3, 0.10)
R["hb_mode3_100ms"] = hb(0.100)
R["hb_mode3_2s"]    = hb(2.000)
# ---- 720p60 对照
set_mode(0, 0.10)
R["hb_mode0_100ms"] = hb(0.100)
R["hb_mode0_2s"]    = hb(2.000)
# ---- 回到默认 mode 3
set_mode(3, 0.10)
R["hb_mode3_back_100ms"] = hb(0.100)
R["final_snapshot"] = snap()

# 比值（用 2 s 窗口）
try:
    a = R["hb_mode3_2s"]["f_pix_hz"]; b = R["hb_mode0_2s"]["f_pix_hz"]
    R["ratio_720p60_over_720p30"] = round(b / a, 4)
    R["dev_pct_mode3_vs_37.109375MHz"] = round((a / 37.109375e6 - 1) * 100, 3)
    R["dev_pct_mode0_vs_74.218750MHz"] = round((b / 74.21875e6 - 1) * 100, 3)
except Exception as e:
    R["ratio_err"] = repr(e)

try:
    R["dmesg_tail30"] = subprocess.run(["dmesg"], capture_output=True, text=True,
                                       timeout=10).stdout.splitlines()[-30:]
except Exception as e:
    R["dmesg_tail30"] = repr(e)
R["fpga_state"] = open("/sys/class/fpga_manager/fpga0/state").read().strip()
json.dump(R, open(OUT, "w"), indent=1, ensure_ascii=False)

print("=== tier0 ===")
print("load_s =", R["load_s"], " fclk0_mhz_reported =", R["fclk0_mhz_reported"])
print("STATUS =", R["boot_snapshot"]["STATUS"], " init_done =", R["tier0_status_bit0_init_done"])
print("boot regs:", R["boot_snapshot"])
print("ip base:", {k: v["physical_address"] for k, v in R["ip_dict"].items()})
print("Cma pre/post (kB):", R["meminfo_pre_load"]["CmaFree"], "/", R["meminfo_post_load"]["CmaFree"])
print("=== tier1 heartbeat ===")
for k in ("hb_mode3_100ms","hb_mode3_2s","hb_mode0_100ms","hb_mode0_2s","hb_mode3_back_100ms"):
    d = R[k]
    print("%-20s edges=%4d dur=%.4fs f_pix=%12.1f Hz half=%.4f ms lock_absent=%d" %
          (k, d["edges"], d["window_s"], d["f_pix_hz"],
           d["half_period_ms"] or -1, d["lock_absent_samples"]))
print("ratio 60/30 =", R.get("ratio_720p60_over_720p30"),
      " dev%: mode3=", R.get("dev_pct_mode3_vs_37.109375MHz"),
      " mode0=", R.get("dev_pct_mode0_vs_74.218750MHz"))
print("final regs:", R["final_snapshot"])
print("JSON ->", OUT)
