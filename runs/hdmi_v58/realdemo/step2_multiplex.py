# -*- coding: utf-8 -*-
"""v58 第二步（时分复用版）：算在 50 MHz，显示在 66.67 MHz。

为什么必须分两段（实测，见 README_STEP2_BLOCKER.md）：
  真值 50.00 MHz：去摩尔纹 = golden，但扫描器 43,168 行/s 全读空
  真值 66.67 MHz：扫描器 0 行/6 s，但去摩尔纹**静默错数**
  ⇒ 两个独立功能域限制，交集为空 ⇒ 时间上分开：算 → 显示

阶段 A  FCLK0 写 75（真值 50.00）：跑**真图**整网（≈20 s），结果落 DDR/npy
阶段 B  FCLK0 写 100（真值 66.67）：软复位清零 0x2C → setup → 显式写 CTRL=0x01
        → 推帧：噪声 ×2（有 golden 背书）→ 真图 ×3，每帧 5 s，每 5 s 读 0x2C
阶段 C  CTRL=0x15 彩条 + 释放缓冲 + sync

映射定死：u8 = round(255 * clip(x,0,1))（同 demo_pipeline.to_uint8），不做自适应拉伸。
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
OUT = os.path.join(PROBE, "step2_multiplex.json")
HDMI_BASE = 0x400A0000
SLCR_BASE = 0xF8000000
N_FCLK = 75.0        # 写 75 ⇒ 真值 50.00
D_FCLK = 100.0       # 写 100 ⇒ 真值 66.67
DWELL = 5.0

R = {"stage": "realdemo_step2_multiplex", "t_wall_start": time.strftime("%F %T"),
     "phaseA": {}, "phaseB": {}, "frames": []}
PIPE = {"pipe": None, "sampler": None}

fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
hm = mmap.mmap(fd, 0x10000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=HDMI_BASE)
sm = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=SLCR_BASE)


def rd(mm, o):
    return ctypes.c_uint32.from_buffer(mm, o).value


def rd32(o):
    return rd(hm, o)


def wr32(o, v):
    ctypes.c_uint32.from_buffer(hm, o).value = int(v) & 0xFFFFFFFF


def sha1_bytes(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def regs():
    return {"CTRL": "0x%08X" % rd32(0x00), "STATUS": "0x%08X" % rd32(0x04),
            "VMODE": "0x%08X" % rd32(0x08), "FB_ADDR": "0x%08X" % rd32(0x0C),
            "STRIDE": "0x%08X" % rd32(0x14), "FB_W": "0x%08X" % rd32(0x18),
            "FB_H": "0x%08X" % rd32(0x1C), "FRAME_ID": "0x%08X" % rd32(0x20),
            "LATCHED": "0x%08X" % rd32(0x24), "UFCNT_0x2C": rd32(0x2C),
            "PIX_HB": "0x%08X" % rd32(0x30)}


def fclk(write_mhz):
    """写 Clocks.fclk0_mhz，返回 (写值, 报告值, ctrl, DIV0, DIV1, 真值 MHz)。"""
    from pynq import Clocks
    Clocks.fclk0_mhz = float(write_mhz)
    time.sleep(0.25)
    v = rd(sm, 0x170)
    d0, d1 = (v >> 8) & 0x3F, (v >> 20) & 0x3F
    return {"write": float(write_mhz), "reported": Clocks.fclk0_mhz,
            "ctrl": "0x%08X" % v, "div0": d0, "div1": d1,
            "true_mhz": round(1000.0 / (d0 * d1), 4)}


def to_uint8(out):
    a = np.asarray(out, dtype=np.float32)
    if a.ndim != 3 or a.shape[0] != 3:
        raise ValueError("引擎输出必须是 (3,H,W)，得到 %s" % (a.shape,))
    return (np.clip(a, 0.0, 1.0).transpose(1, 2, 0) * np.float32(255.0)).round().astype(np.uint8)


def to_float_tensor(img):
    a = np.asarray(img)
    if a.shape != (256, 256, 3) or a.dtype != np.uint8:
        raise ValueError("必须是 (256,256,3) uint8，得到 %s/%s" % (a.shape, a.dtype))
    return np.ascontiguousarray(a.transpose(2, 0, 1), dtype=np.float32) / np.float32(255.0)


def stats8(a):
    a = np.asarray(a)
    q = np.percentile(a.astype(np.float32), [1, 50, 99])
    return {"shape": list(a.shape), "dtype": str(a.dtype), "sha1": sha1_bytes(a),
            "min": int(a.min()), "max": int(a.max()), "mean": round(float(a.mean()), 4),
            "p1": round(float(q[0]), 3), "p50": round(float(q[1]), 3),
            "p99": round(float(q[2]), 3)}


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.phase = "init"
        self.samples = []
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                self.samples.append({"t": time.strftime("%H:%M:%S"), "phase": self.phase,
                                     "UFCNT": rd32(0x2C), "CTRL": "0x%08X" % rd32(0x00),
                                     "LATCHED": "0x%08X" % rd32(0x24),
                                     "STATUS": "0x%08X" % rd32(0x04)})
            except Exception:                      # noqa: BLE001
                pass
            time.sleep(1.0)


def main():
    import pynq.pl_server.embedded_device  # noqa: F401
    from pynq import Overlay
    import display_demo as dd
    import display_layout as dl

    ov = Overlay(BIT, download=False)          # ★ 不二次 load
    R["regs_start"] = regs()

    # ================= 阶段 A：真图整网 @ 真值 50.00 MHz =================
    R["phaseA"]["clk"] = fclk(N_FCLK)
    print("[A/clk] %s" % R["phaseA"]["clk"], flush=True)
    wr32(0x00, 0x15)                            # 确保扫描器关（算的时候不扫）
    time.sleep(0.2)
    R["phaseA"]["regs_idle"] = regs()

    from bin_engine import BinConvEngine
    from bin_net import BinNet
    eng = BinConvEngine(ov, unit_id=0, pack_dir=PACK)
    eng.load_weights()
    net = BinNet(PACK, engine=eng, H=256, W=256)
    real = np.load(os.path.join(PROBE, "real_in_0000_256.npy"))
    x_in = to_float_tensor(real)
    R["phaseA"]["input"] = stats8(real)
    R["phaseA"]["x_in_sha1"] = sha1_bytes(x_in)
    print("[A] 真图输入 sha1=%s" % R["phaseA"]["x_in_sha1"], flush=True)

    t0 = time.time()
    outs, st = net.run(x_in, pipeline=True, nbuf=2)
    R["phaseA"]["run_s"] = round(time.time() - t0, 2)
    R["phaseA"]["stats"] = {k: st.get(k) for k in
                            ("total", "pl", "ps", "ps_prologue", "ps_epilogue",
                             "ps_nonconv", "overlap", "wait", "convs")}
    real_out1 = outs["out1"]
    R["phaseA"]["out1"] = {"shape": list(real_out1.shape),
                           "min": round(float(real_out1.min()), 5),
                           "max": round(float(real_out1.max()), 5),
                           "mean": round(float(real_out1.mean()), 5),
                           "sha1": sha1_bytes(real_out1)}
    np.save(os.path.join(PROBE, "real_out1_256.npy"),
            np.ascontiguousarray(real_out1, np.float32))
    print("[A] 真图 out1 min=%.4f max=%.4f mean=%.4f sha1=%s"
          % (real_out1.min(), real_out1.max(), real_out1.mean(),
             R["phaseA"]["out1"]["sha1"]), flush=True)
    del net, eng, outs
    gc.collect()
    R["phaseA"]["done"] = True

    # ================= 阶段 B：显示 @ 真值 66.67 MHz =================
    R["phaseB"]["clk"] = fclk(D_FCLK)
    print("[B/clk] %s" % R["phaseB"]["clk"], flush=True)

    pipe = dd.DisplayPipeline(mode=0)
    PIPE["pipe"] = pipe
    alive, mhz = pipe.pix_alive(200)
    R["phaseB"]["pix_alive"] = {"alive": bool(alive), "mhz": round(mhz, 4)}
    print("[B/pix] alive=%s %.3f MHz" % (alive, mhz), flush=True)
    if not alive:
        raise SystemExit("像素时钟没跑")

    pipe.setup()
    R["phaseB"]["fb_addr"] = "0x%08X" % pipe.fb_addr
    R["phaseB"]["block_bytes"] = pipe.block_bytes
    R["phaseB"]["regs_after_setup"] = regs()

    # —— 软复位：CTRL[1] 写 1（自清），把 RO 的 0x2C 清零 ——
    uf_before = rd32(0x2C)
    wr32(0x00, 0x02)
    time.sleep(0.35)
    R["phaseB"]["ufcnt_before_softreset"] = uf_before
    R["phaseB"]["regs_after_softreset"] = regs()
    R["phaseB"]["ufcnt_after_softreset"] = rd32(0x2C)
    print("[B/reset] 0x2C: %d -> %d ; CTRL=%s FB_ADDR=%s"
          % (uf_before, rd32(0x2C), regs()["CTRL"], regs()["FB_ADDR"]), flush=True)

    # 软复位可能把配置页清掉 ⇒ 重新 setup（缓冲尺寸没变 ⇒ 复用同一块）
    pipe.setup()
    R["phaseB"]["regs_after_reset_setup"] = regs()
    wr32(0x00, 0x01)                 # ★ 显式写：enable=1 / test_pattern=0
    time.sleep(0.35)
    R["phaseB"]["regs_scan_on"] = regs()
    if rd32(0x00) & 0x04:
        raise SystemExit("test_pattern 仍为 1")
    print("[B/scan] %s" % R["phaseB"]["regs_scan_on"], flush=True)

    sampler = Sampler()
    sampler.phase = "B_display"
    sampler.start()
    PIPE["sampler"] = sampler

    # 两张图（噪声：有 golden 背书；真图：看效果）
    x0 = np.load(os.path.join(PROBE, "x0_256_seed1.npy"))
    o1 = np.load(os.path.join(PROBE, "out1_256_seed1.npy"))
    noise_orig, noise_proc = to_uint8(x0), to_uint8(o1)
    real_proc = to_uint8(real_out1)
    R["phaseB"]["noise_orig_u8"] = stats8(noise_orig)
    R["phaseB"]["noise_proc_u8"] = stats8(noise_proc)
    R["phaseB"]["real_orig_u8"] = stats8(real)
    R["phaseB"]["real_proc_u8"] = stats8(real_proc)
    R["phaseB"]["mapping"] = "u8 = round(255 * clip(x,0,1)); no auto-contrast"
    R["phaseB"]["real_mean_abs_diff_f32"] = round(float(np.abs(real_out1 - x_in).mean()), 5)
    R["phaseB"]["real_mean_abs_diff_u8"] = round(float(np.abs(
        real_proc.astype(np.int16) - real.astype(np.int16)).mean()), 3)
    R["phaseB"]["real_frac_pix_changed_gt2"] = round(float(
        (np.abs(real_proc.astype(np.int16) - real.astype(np.int16)).max(axis=2) > 2).mean()), 4)
    np.save(os.path.join(PROBE, "real_proc_u8_256.npy"), real_proc)
    print("[map] noise orig=%s proc=%s" % (R["phaseB"]["noise_orig_u8"]["sha1"],
                                           R["phaseB"]["noise_proc_u8"]["sha1"]), flush=True)
    print("[map] real  orig=%s proc=%s  mean|out-in| f32=%.4f u8=%.2f  changed>2=%.1f%%"
          % (R["phaseB"]["real_orig_u8"]["sha1"], R["phaseB"]["real_proc_u8"]["sha1"],
             R["phaseB"]["real_mean_abs_diff_f32"], R["phaseB"]["real_mean_abs_diff_u8"],
             100 * R["phaseB"]["real_frac_pix_changed_gt2"]), flush=True)

    plan = [("noise", noise_orig, noise_proc, 2), ("real", real, real_proc, 3)]
    for tag, o, p, n in plan:
        for i in range(1, n + 1):
            t_push = time.time()
            fid = pipe.show(o, p)
            ok = pipe.wait(fid, 1.0)
            latched = rd32(0x24)
            rec = {"tag": tag, "i": i, "fid": fid, "latched": "0x%08X" % latched,
                   "match_0x24_eq_fid": bool(latched == fid), "wait_ok": bool(ok),
                   "call_s": round(time.time() - t_push, 3),
                   "t": time.strftime("%H:%M:%S"), "UFCNT": rd32(0x2C),
                   "STATUS": "0x%08X" % rd32(0x04), "PIX_HB": "0x%08X" % rd32(0x30),
                   "CTRL": "0x%08X" % rd32(0x00), "fclk_true_mhz": R["phaseB"]["clk"]["true_mhz"]}
            R["frames"].append(rec)
            print("[frame] %s #%d fid=%d latched=%s match=%s wait=%s UFCNT=%d"
                  % (tag, i, fid, rec["latched"], rec["match_0x24_eq_fid"], ok,
                     rec["UFCNT"]), flush=True)
            time.sleep(DWELL)

    # 显示期保持 10 s，每 2.5 s 读 0x2C（判据：显示期间恒 0）
    hold = []
    for k in range(5):
        hold.append({"k": k, "t": time.strftime("%H:%M:%S"), "UFCNT": rd32(0x2C),
                     "LATCHED": "0x%08X" % rd32(0x24), "STATUS": "0x%08X" % rd32(0x04)})
        if k < 4:
            time.sleep(2.5)
    R["phaseB"]["hold_display"] = hold
    R["phaseB"]["hold_all_zero"] = bool(all(h["UFCNT"] == 0 for h in hold))
    R["phaseB"]["compose_sha1_noise"] = sha1_bytes(dl.compose(noise_orig, noise_proc, 1280, 720))
    R["phaseB"]["compose_sha1_real"] = sha1_bytes(dl.compose(real, real_proc, 1280, 720))
    print("[hold] 显示期 0x2C = %s ⇒ all_zero=%s"
          % ([h["UFCNT"] for h in hold], R["phaseB"]["hold_all_zero"]), flush=True)
    R["phaseB"]["done"] = True
    return 0


def cleanup():
    sampler = PIPE.get("sampler")
    if sampler is not None:
        sampler._stop = True
    if sampler is not None and sampler.samples:
        R["samples"] = list(sampler.samples)
        R["samples_n"] = len(R["samples"])
        R["samples_all_zero"] = bool(all(s["UFCNT"] == 0 for s in R["samples"]))
        R["samples_max"] = max(s["UFCNT"] for s in R["samples"])
    try:
        wr32(0x00, 0x15)                     # 阶段 C：彩条
        time.sleep(0.2)
        R["regs_restored"] = regs()
    except Exception:                        # noqa: BLE001
        traceback.print_exc()
    try:
        if PIPE.get("pipe") is not None:
            PIPE["pipe"].close()
        R["buffers_freed"] = True
    except Exception:                        # noqa: BLE001
        R["buffers_freed"] = False
        traceback.print_exc()
    try:
        from pynq import Clocks
        Clocks.fclk0_mhz = N_FCLK          # 交回 50.00（去摩尔纹的运行点）
        time.sleep(0.2)
        R["clk_restored"] = fclk(N_FCLK)
    except Exception:                        # noqa: BLE001
        traceback.print_exc()
    R["sync_rc"] = subprocess.run(["sync"]).returncode
    try:
        R["fpga0_state"] = open("/sys/class/fpga_manager/fpga0/state").read().strip()
    except Exception:                        # noqa: BLE001
        R["fpga0_state"] = "?"


if __name__ == "__main__":
    rc = 0
    try:
        rc = main()
    except SystemExit as e:
        print("[exit] %s" % (e,), flush=True)
        rc = 3
    except Exception:
        R["traceback"] = traceback.format_exc()[-4000:]
        print(R["traceback"], flush=True)
        rc = 90
    finally:
        try:
            cleanup()
        except Exception:                    # noqa: BLE001
            traceback.print_exc()
        R["rc"] = rc
        R["t_wall_end"] = time.strftime("%F %T")
        try:
            json.dump(R, open(OUT, "w"), indent=1, ensure_ascii=False)
            print("[json] -> %s rc=%d" % (OUT, rc), flush=True)
        except Exception:                    # noqa: BLE001
            traceback.print_exc()
    sys.exit(rc)
