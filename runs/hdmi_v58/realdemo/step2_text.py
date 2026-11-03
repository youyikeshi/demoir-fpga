# -*- coding: utf-8 -*-
"""v58 第二步 · 文字图版：两张文字底图，算在 50 MHz、显示在 66.67 MHz。

时序（parent 指定）：
  第 1 张：3 帧 × 2.7 s ≈ 8 s  →  黑 1 s  →  第 2 张：3 帧 × 2.7 s ≈ 8 s  →  彩条
映射：
  A 版（默认推上屏）= u8 = round(255 * clip(x,0,1))            ← 父代理定死的固定映射
  B 版（另存，不推）  = u8 = round(255 * clip((x-p1)/(p99-p1),0,1))  ← 用户嫌灰时备用
"""
import ctypes
import gc
import hashlib
import json
import mmap
import os
import subprocess
import sys
import threading
import time
import traceback

import numpy as np

PROBE = os.path.dirname(os.path.abspath(__file__))
BIT = "/tmp/probe_v58_v58a/design_1_v58.bit"
PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
OUT = os.path.join(PROBE, "step2_text.json")
HDMI_BASE = 0x400A0000
SLCR_BASE = 0xF8000000
N_FCLK, D_FCLK = 75.0, 100.0        # 真值 50.00 / 66.67
DWELL = 2.7
IMAGES = ("0094", "0049")

R = {"stage": "realdemo_step2_text", "t_wall_start": time.strftime("%F %T"),
     "images": {}, "frames": []}
PIPE = {"pipe": None, "sampler": None}

fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
hm = mmap.mmap(fd, 0x10000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=HDMI_BASE)
sm = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=SLCR_BASE)
rd32 = lambda o: ctypes.c_uint32.from_buffer(hm, o).value


def wr32(o, v):
    ctypes.c_uint32.from_buffer(hm, o).value = int(v) & 0xFFFFFFFF


def sha1_bytes(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def regs():
    return {"CTRL": "0x%08X" % rd32(0x00), "STATUS": "0x%08X" % rd32(0x04),
            "VMODE": "0x%08X" % rd32(0x08), "FB_ADDR": "0x%08X" % rd32(0x0C),
            "FRAME_ID": "0x%08X" % rd32(0x20), "LATCHED": "0x%08X" % rd32(0x24),
            "UFCNT_0x2C": rd32(0x2C), "PIX_HB": "0x%08X" % rd32(0x30)}


def fclk(w):
    from pynq import Clocks
    Clocks.fclk0_mhz = float(w)
    time.sleep(0.25)
    v = ctypes.c_uint32.from_buffer(sm, 0x170).value
    d0, d1 = (v >> 8) & 0x3F, (v >> 20) & 0x3F
    return {"write": float(w), "reported": Clocks.fclk0_mhz, "ctrl": "0x%08X" % v,
            "true_mhz": round(1000.0 / (d0 * d1), 4)}


def to_f32(img):
    return np.ascontiguousarray(np.asarray(img).transpose(2, 0, 1), dtype=np.float32) / np.float32(255.0)


def map_fixed(x):
    a = np.asarray(x, dtype=np.float32)
    return (np.clip(a, 0.0, 1.0).transpose(1, 2, 0) * np.float32(255.0)).round().astype(np.uint8)


def map_stretch(x):
    a = np.asarray(x, dtype=np.float32)
    lo, hi = np.percentile(a, 1.0), np.percentile(a, 99.0)
    if hi - lo < 1e-6:
        return map_fixed(a)
    return (np.clip((a - lo) / (hi - lo), 0.0, 1.0).transpose(1, 2, 0) * np.float32(255.0)).round().astype(np.uint8)


def stats8(a):
    a = np.asarray(a)
    q = np.percentile(a.astype(np.float32), [1, 50, 99])
    return {"shape": list(a.shape), "sha1": sha1_bytes(a), "min": int(a.min()),
            "max": int(a.max()), "mean": round(float(a.mean()), 3),
            "p1": round(float(q[0]), 2), "p50": round(float(q[1]), 2), "p99": round(float(q[2]), 2)}


def psnr(a, b):
    a = np.asarray(a, np.float32); b = np.asarray(b, np.float32)
    mse = float(((a - b) ** 2).mean())
    return round(10 * np.log10(255.0 * 255.0 / mse), 3) if mse > 0 else 99.0


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.samples, self._stop, self.phase = [], False, "init"

    def run(self):
        while not self._stop:
            try:
                self.samples.append({"t": time.strftime("%H:%M:%S"), "phase": self.phase,
                                     "UFCNT": rd32(0x2C), "CTRL": "0x%08X" % rd32(0x00),
                                     "LATCHED": "0x%08X" % rd32(0x24)})
            except Exception:                 # noqa: BLE001
                pass
            time.sleep(1.0)


def main():
    import pynq.pl_server.embedded_device  # noqa: F401
    from pynq import Overlay
    import display_demo as dd
    import display_layout as dl

    ov = Overlay(BIT, download=False)
    R["regs_start"] = regs()

    # ---------- 阶段 A：两张图各跑一次整网（真值 50 MHz，扫描器关） ----------
    R["clkA"] = fclk(N_FCLK)
    print("[A/clk] %s" % R["clkA"], flush=True)
    wr32(0x00, 0x15)
    from bin_engine import BinConvEngine
    from bin_net import BinNet
    eng = BinConvEngine(ov, unit_id=0, pack_dir=PACK)
    eng.load_weights()

    disp = {}
    for name in IMAGES:
        moire = np.load(os.path.join(PROBE, "text_%s_moire.npy" % name))
        gt = np.load(os.path.join(PROBE, "text_%s_gt.npy" % name))
        net = BinNet(PACK, engine=eng, H=256, W=256)
        t0 = time.time()
        outs, st = net.run(to_f32(moire), pipeline=True, nbuf=2)
        dt = round(time.time() - t0, 2)
        o1 = outs["out1"]
        np.save(os.path.join(PROBE, "text_%s_out1.npy" % name), np.ascontiguousarray(o1, np.float32))
        fixed, stretch = map_fixed(o1), map_stretch(o1)
        np.save(os.path.join(PROBE, "text_%s_proc_u8_fixed.npy" % name), fixed)
        np.save(os.path.join(PROBE, "text_%s_proc_u8_stretch.npy" % name), stretch)
        info = {"run_s": dt, "pl": st.get("pl"),
                "out1": {"min": round(float(o1.min()), 5), "max": round(float(o1.max()), 5),
                         "mean": round(float(o1.mean()), 5), "std": round(float(o1.std()), 5),
                         "sha1": sha1_bytes(o1)},
                "moire_u8": stats8(moire), "gt_u8": stats8(gt),
                "proc_fixed_u8": stats8(fixed), "proc_stretch_u8": stats8(stretch),
                "mean_abs_diff_vs_input": round(float(np.abs(
                    fixed.astype(np.int16) - moire.astype(np.int16)).mean()), 3),
                "psnr_moire_vs_gt": psnr(moire, gt),
                "psnr_proc_vs_gt": psnr(fixed, gt),
                "psnr_procstretch_vs_gt": psnr(stretch, gt),
                "mean_abs_diff_proc_vs_gt": round(float(np.abs(
                    fixed.astype(np.int16) - gt.astype(np.int16)).mean()), 3)}
        R["images"][name] = info
        disp[name] = fixed
        print("[A] %s run=%.1fs out1[min %.4f max %.4f mean %.4f std %.4f] "
              "PSNR in/out vs GT = %.2f / %.2f dB  mean|out-in|=%.1f"
              % (name, dt, o1.min(), o1.max(), o1.mean(), o1.std(),
                 info["psnr_moire_vs_gt"], info["psnr_proc_vs_gt"],
                 info["mean_abs_diff_vs_input"]), flush=True)
        del net, outs
        gc.collect()
    del eng
    gc.collect()
    R["phaseA_done"] = True

    # ---------- 阶段 B：显示（真值 66.67 MHz） ----------
    R["clkB"] = fclk(D_FCLK)
    print("[B/clk] %s" % R["clkB"], flush=True)
    pipe = dd.DisplayPipeline(mode=0)
    PIPE["pipe"] = pipe
    alive, mhz = pipe.pix_alive(200)
    R["pix_alive"] = {"alive": bool(alive), "mhz": round(mhz, 4)}
    pipe.setup()
    R["fb_addr"] = "0x%08X" % pipe.fb_addr
    uf_before = rd32(0x2C)
    wr32(0x00, 0x02)                      # 软复位：清 RO 的 0x2C
    time.sleep(0.35)
    R["ufcnt_before_softreset"] = uf_before
    R["ufcnt_after_softreset"] = rd32(0x2C)
    pipe.setup()                          # 复位可能清配置页 ⇒ 重新配置（复用同一块缓冲）
    wr32(0x00, 0x01)                      # ★ 显式写：enable=1 / test_pattern=0
    time.sleep(0.35)
    R["regs_scan_on"] = regs()
    if rd32(0x00) & 0x04:
        raise SystemExit("test_pattern 仍为 1")
    print("[B/reset] 0x2C %d -> %d ; scan_on=%s" % (uf_before, rd32(0x2C), R["regs_scan_on"]),
          flush=True)

    sampler = Sampler(); sampler.phase = "B_display"; sampler.start()
    PIPE["sampler"] = sampler

    black = np.zeros((256, 256, 3), np.uint8)

    def push(tag, orig, proc, n):
        for i in range(1, n + 1):
            fid = pipe.show(orig, proc)
            ok = pipe.wait(fid, 1.0)
            latched = rd32(0x24)
            rec = {"tag": tag, "i": i, "fid": fid, "latched": "0x%08X" % latched,
                   "match": bool(latched == fid), "wait_ok": bool(ok),
                   "t": time.strftime("%H:%M:%S"), "UFCNT": rd32(0x2C),
                   "STATUS": "0x%08X" % rd32(0x04), "CTRL": "0x%08X" % rd32(0x00)}
            R["frames"].append(rec)
            print("[frame] %-10s #%d fid=%d latched=%s match=%s UFCNT=%d"
                  % (tag, i, fid, rec["latched"], rec["match"], rec["UFCNT"]), flush=True)
            time.sleep(DWELL)

    # 第 1 张：moire vs PL 输出
    m1 = np.load(os.path.join(PROBE, "text_%s_moire.npy" % IMAGES[0]))
    push("img1_" + IMAGES[0], m1, disp[IMAGES[0]], 3)
    # 黑 1 s
    fid = pipe.show(black, black); pipe.wait(fid, 1.0)
    R["frames"].append({"tag": "black", "fid": fid, "latched": "0x%08X" % rd32(0x24),
                        "match": bool(rd32(0x24) == fid), "t": time.strftime("%H:%M:%S")})
    print("[frame] black fid=%d" % fid, flush=True)
    time.sleep(1.0)
    # 第 2 张
    m2 = np.load(os.path.join(PROBE, "text_%s_moire.npy" % IMAGES[1]))
    push("img2_" + IMAGES[1], m2, disp[IMAGES[1]], 3)

    hold = []
    for k in range(5):
        hold.append({"k": k, "t": time.strftime("%H:%M:%S"), "UFCNT": rd32(0x2C)})
        if k < 4:
            time.sleep(2.5)
    R["hold_display"] = hold
    R["hold_all_zero"] = bool(all(h["UFCNT"] == 0 for h in hold))
    R["compose_sha1"] = {n: sha1_bytes(dl.compose(np.load(os.path.join(PROBE, "text_%s_moire.npy" % n)),
                                                 disp[n], 1280, 720)) for n in IMAGES}
    print("[hold] 0x2C=%s all_zero=%s" % ([h["UFCNT"] for h in hold], R["hold_all_zero"]),
          flush=True)
    return 0


def cleanup():
    s = PIPE.get("sampler")
    if s is not None:
        s._stop = True
        if s.samples:
            R["samples"] = list(s.samples)
            R["samples_n"] = len(s.samples)
            R["samples_all_zero"] = bool(all(x["UFCNT"] == 0 for x in s.samples))
            R["samples_max"] = max(x["UFCNT"] for x in s.samples)
    try:
        wr32(0x00, 0x15)
        time.sleep(0.2)
        R["regs_restored"] = regs()
    except Exception:                       # noqa: BLE001
        traceback.print_exc()
    try:
        if PIPE.get("pipe") is not None:
            PIPE["pipe"].close()
        R["buffers_freed"] = True
    except Exception:                       # noqa: BLE001
        R["buffers_freed"] = False
    try:
        R["clk_restored"] = fclk(N_FCLK)
    except Exception:                       # noqa: BLE001
        traceback.print_exc()
    R["sync_rc"] = subprocess.run(["sync"]).returncode
    try:
        R["fpga0_state"] = open("/sys/class/fpga_manager/fpga0/state").read().strip()
    except Exception:                       # noqa: BLE001
        R["fpga0_state"] = "?"


if __name__ == "__main__":
    rc = 0
    try:
        rc = main()
    except SystemExit as e:
        print("[exit] %s" % (e,), flush=True); rc = 3
    except Exception:
        R["traceback"] = traceback.format_exc()[-4000:]
        print(R["traceback"], flush=True); rc = 90
    finally:
        try:
            cleanup()
        except Exception:                   # noqa: BLE001
            traceback.print_exc()
        R["rc"] = rc
        R["t_wall_end"] = time.strftime("%F %T")
        try:
            json.dump(R, open(OUT, "w"), indent=1, ensure_ascii=False)
            print("[json] -> %s rc=%d" % (OUT, rc), flush=True)
        except Exception:                   # noqa: BLE001
            traceback.print_exc()
    sys.exit(rc)
