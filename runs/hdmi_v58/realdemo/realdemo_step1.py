# -*- coding: utf-8 -*-
"""v58 第一步：去摩尔纹单独跑通（HDMI 停在彩条，不接显示链路）。

判据（本脚本只负责取证，判定在主机侧做）：
  ① 板端 PS 文件 md5（由 argv 传入清单，逐个算）
  ② 写 Clocks.fclk0_mhz=75 ⇒ 解 FCLK0_CTRL 得 DIV0×DIV1，真值必须 50.00 MHz
  ③ 256² 整网 out1/out2/out3 的 **完整 40 位 sha1**
  ④ 跑前/跑后各读一次 HDMI 0x2C（彩条模式，扫描器关 ⇒ 应恒 0，仅记录）

不 load 位流：Overlay(download=False) 接手 /tmp/probe_v58_v58a/design_1_v58.bit。
"""
import ctypes
import hashlib
import json
import mmap
import os
import platform
import sys
import time
import traceback

import numpy as np

PROBE = os.path.dirname(os.path.abspath(__file__))
BIT = "/tmp/probe_v58_v58a/design_1_v58.bit"
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
OUT = os.path.join(PROBE, "step1.json")
HW = 256
SEED = 1
FCLK_WRITE = 75.0          # PYNQ 口径写值；真值 = 1000/(DIV0*DIV1)
HDMI_BASE = 0x400A0000
SLCR_BASE = 0xF8000000

R = {"stage": "realdemo_step1", "probe_dir": PROBE,
     "t_wall_start": time.strftime("%F %T"), "hw": HW, "seed": SEED,
     "fclk_write": FCLK_WRITE, "bit": BIT, "pack": PACK}


def sha1_bytes(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def md5_file(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def meminfo():
    d = {}
    for ln in open("/proc/meminfo"):
        k, v = ln.split(":", 1)
        if k in ("CmaTotal", "CmaFree", "MemTotal", "MemAvailable"):
            d[k] = int(v.split()[0])
    return d


def main():
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    hm = mmap.mmap(fd, 0x10000, mmap.MAP_SHARED,
                   mmap.PROT_READ | mmap.PROT_WRITE, offset=HDMI_BASE)
    sm = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED,
                   mmap.PROT_READ | mmap.PROT_WRITE, offset=SLCR_BASE)

    def rd(mm, o):
        return ctypes.c_uint32.from_buffer(mm, o).value

    def hdmi():
        return {"CTRL": "0x%08X" % rd(hm, 0x00), "STATUS": "0x%08X" % rd(hm, 0x04),
                "VMODE": "0x%08X" % rd(hm, 0x08), "FB_ADDR": "0x%08X" % rd(hm, 0x0C),
                "FRAME_ID": "0x%08X" % rd(hm, 0x20), "LATCHED": "0x%08X" % rd(hm, 0x24),
                "UFCNT_0x2C": rd(hm, 0x2C), "PIX_HB": "0x%08X" % rd(hm, 0x30)}

    # ---------- 0. 板端环境 ----------
    R["uname"] = platform.uname()._asdict()
    R["uptime_s"] = round(float(open("/proc/uptime").read().split()[0]), 1)
    R["meminfo_before"] = meminfo()
    R["fpga0_state_before"] = open("/sys/class/fpga_manager/fpga0/state").read().strip()

    # ---------- ① 板端 PS 文件 md5 ----------
    files = [f for f in (sys.argv[1].split(",") if len(sys.argv) > 1 else []) if f]
    R["md5_onboard"] = {f: md5_file(os.path.join(PROBE, f)) for f in files}
    print("[md5] %d 个文件已算" % len(R["md5_onboard"]), flush=True)

    # ---------- ② overlay(接手) + 时钟 ----------
    import pynq.pl_server.embedded_device  # noqa: F401  （PYNQ 3.1 必须）
    from pynq import Overlay, Clocks
    t0 = time.time()
    ov = Overlay(BIT, download=False)          # ★ 不二次 load
    R["overlay_s"] = round(time.time() - t0, 2)
    R["ip_dict"] = {k: {"phys_addr": "0x%08X" % int(v["phys_addr"]),
                        "range": int(v["addr_range"])}
                    for k, v in ov.ip_dict.items() if int(v.get("addr_range", 0))}
    R["fclk_reported_before"] = Clocks.fclk0_mhz
    Clocks.fclk0_mhz = FCLK_WRITE               # ★ 写 75 ⇒ 真值 50
    time.sleep(0.2)
    R["fclk_reported_after"] = Clocks.fclk0_mhz
    v = rd(sm, 0x170)
    d0, d1 = (v >> 8) & 0x3F, (v >> 20) & 0x3F
    R["fclk0_ctrl"] = "0x%08X" % v
    R["div0"], R["div1"] = d0, d1
    R["div_product"] = d0 * d1
    R["fclk_true_mhz"] = round(1000.0 / (d0 * d1), 4)
    print("[clk] 报告 %.1f -> %.1f ; FCLK0_CTRL=%s DIV0=%d DIV1=%d prod=%d 真值=%.2f MHz"
          % (R["fclk_reported_before"], R["fclk_reported_after"], R["fclk0_ctrl"],
             d0, d1, d0 * d1, R["fclk_true_mhz"]), flush=True)

    R["hdmi_before_run"] = hdmi()
    R["ufcnt_before_run"] = R["hdmi_before_run"]["UFCNT_0x2C"]
    print("[hdmi] 跑前 %s" % R["hdmi_before_run"], flush=True)

    # ---------- ③ 整网 256² ----------
    from bin_engine import BinConvEngine
    from bin_net import BinNet

    eng = BinConvEngine(ov, unit_id=0, pack_dir=PACK)
    eng.load_weights()
    R["engine_fclk_mhz"] = getattr(eng, "fclk_mhz", None)
    net = BinNet(PACK, engine=eng, H=HW, W=HW)
    rng = np.random.default_rng(SEED)
    x0 = rng.random((3, HW, HW), dtype=np.float32)
    R["x0_sha1"] = sha1_bytes(x0)
    np.save(os.path.join(PROBE, "x0_256_seed1.npy"), x0)
    print("[net] 输入 (3,%d,%d) seed=%d sha1=%s" % (HW, HW, SEED, R["x0_sha1"]),
          flush=True)

    t0 = time.time()
    outs, st = net.run(x0, pipeline=True, nbuf=2)
    R["run_s"] = round(time.time() - t0, 2)
    R["stats"] = {k: st.get(k) for k in
                  ("total", "pl", "ps", "ps_prologue", "ps_epilogue", "ps_nonconv",
                   "overlap", "wait", "convs", "mmio_writes", "cfg_hits",
                   "lazy_deq", "bv_free")}
    R["stats"]["prof"] = {k: round(float(vv), 4) for k, vv in (st.get("prof") or {}).items()}

    R["outs"] = {}
    for k in ("out1", "out2", "out3"):
        o = outs[k]
        R["outs"][k] = {"shape": list(o.shape), "dtype": str(o.dtype),
                        "min": float(o.min()), "max": float(o.max()),
                        "mean": float(o.mean()), "sha1": sha1_bytes(o)}
        print("  %-5s %-14s min=%.4f max=%.4f mean=%.4f  sha1=%s"
              % (k, "x".join(map(str, o.shape)), o.min(), o.max(), o.mean(),
                 R["outs"][k]["sha1"]), flush=True)
        if k == "out1":
            np.save(os.path.join(PROBE, "out1_256_seed1.npy"),
                    np.ascontiguousarray(o, np.float32))

    # ---------- ④ 跑后：0x2C + 复位值 ----------
    R["hdmi_after_run"] = hdmi()
    R["ufcnt_after_run"] = R["hdmi_after_run"]["UFCNT_0x2C"]
    R["meminfo_after"] = meminfo()
    R["fpga0_state_after"] = open("/sys/class/fpga_manager/fpga0/state").read().strip()
    print("[hdmi] 跑后 %s" % R["hdmi_after_run"], flush=True)

    R["t_wall_end"] = time.strftime("%F %T")
    R["ok"] = True
    return 0


if __name__ == "__main__":
    rc = 0
    try:
        rc = main()
    except Exception:
        R["ok"] = False
        R["traceback"] = traceback.format_exc()[-4000:]
        print(R["traceback"], flush=True)
        rc = 90
    try:
        json.dump(R, open(OUT, "w"), indent=1, ensure_ascii=False)
        print("[json] -> %s  ok=%s" % (OUT, R.get("ok")), flush=True)
    except Exception:
        traceback.print_exc()
    sys.exit(rc)
