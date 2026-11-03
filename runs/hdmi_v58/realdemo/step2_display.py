# -*- coding: utf-8 -*-
"""v58 第二步：把 PL 真算出来的 256² 去摩尔纹结果推到 HDMI 右半屏。

阶段 A（先行，推完停在最后一帧等闸门文件）
  ① `DisplayPipeline(mode=0).setup()` → 写块 → **直接写 `CTRL=0x01`**（不用 `enable()`：它是读-改-写）
  ② 推**噪声版** 3 帧（`out1_256_seed1`：唯一有 golden 背书的数据），每帧停 5 s
  ③ 全程 1 s 连续采样 `0x2C` + 末尾 20 s 内每 5 s 一个显式点
  ④ 写 `phaseA.json` 后停在此处（画面保持最后一帧），轮询 `/tmp/probe_v58_real_r1/go_phaseB`

阶段 B（闸门文件出现后）
  ⑤ 跑**真图**（`0000_moire` 中心 256² 裁块）整网 → 3 帧，每帧停 5 s
  ⑥ 恢复 `CTRL=0x15`（彩条）+ 释放缓冲 + `sync`（异常路径也走）

不 load 位流（`Overlay(download=False)` 接手），不动 FCLK0（沿用真值 50 MHz）。

显示映射**定死**：`u8 = round(255 * clip(x,0,1))`，与 `demo_pipeline.py:121-135`
（`to_float_tensor`/`to_uint8`，板端已验）**同式**；**不做**自适应拉伸。
（不直接 import demo_pipeline：它在导入时 `apply_runpoint()` 会把 `BIN_PACK=e1e2`
塞进环境，而那需要 `deploy/e1e2_desc` 数据目录；本轮按第一步结论走无 e1e2 路径。）
"""
import ctypes
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
GATE = os.path.join(PROBE, "go_phaseB")
OUT = os.path.join(PROBE, "step2.json")
AOUT = os.path.join(PROBE, "phaseA.json")
HDMI_BASE = 0x400A0000
SLCR_BASE = 0xF8000000
PS_CLK_MHZ = 33.3333          # Zynq-7000 标准 PS_CLK（三个 PLL 的 FDIV 只有按它解才自洽）
FRAME_DWELL = 5.0
HOLD_S = 20.0
GATE_TIMEOUT = 2400.0

R = {"stage": "realdemo_step2", "t_wall_start": time.strftime("%F %T"),
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
            "STRIDE": "0x%08X" % rd32(0x14), "FRAME_ID": "0x%08X" % rd32(0x20),
            "LATCHED": "0x%08X" % rd32(0x24), "UFCNT_0x2C": rd32(0x2C),
            "PIX_HB": "0x%08X" % rd32(0x30)}


def to_uint8(out):
    """(3,H,W) f32 → (H,W,3) u8：clip(0,1)→transpose→×255→round（同 demo_pipeline）。"""
    a = np.asarray(out, dtype=np.float32)
    if a.ndim != 3 or a.shape[0] != 3:
        raise ValueError("引擎输出必须是 (3,H,W)，得到 %s" % (a.shape,))
    return (np.clip(a, 0.0, 1.0).transpose(1, 2, 0) * np.float32(255.0)).round().astype(np.uint8)


def to_float_tensor(img):
    """(256,256,3) u8 RGB → (3,256,256) f32 ∈[0,1]（同 demo_pipeline）。"""
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
    """1 Hz 连续读 0x2C（下溢计数）+ 0x24/0x04/0x30，全程覆盖（含整网跑的时候）。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.phase = "init"
        self.samples = []
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                self.samples.append({"t": time.strftime("%H:%M:%S"), "phase": self.phase,
                                     "UFCNT": rd32(0x2C),
                                     "LATCHED": "0x%08X" % rd32(0x24),
                                     "STATUS": "0x%08X" % rd32(0x04),
                                     "PIX_HB": "0x%08X" % rd32(0x30)})
            except Exception:                     # noqa: BLE001
                pass
            time.sleep(1.0)


def pll_decode():
    """从 SLCR 解 IO PLL 与 FCLK0 真值（PYNQ 的字段：PLL_FDIV = [18:12]）。"""
    d = {}
    for off, name in ((0x100, "ARM_PLL"), (0x104, "DDR_PLL"), (0x108, "IO_PLL")):
        v = rd(sm, off)
        fd_ = (v >> 12) & 0x7F
        d[name] = {"raw": "0x%08X" % v, "FDIV": fd_,
                   "mhz_if_psclk_33.333": round(fd_ * PS_CLK_MHZ, 3)}
    v = rd(sm, 0x170)
    div0, div1 = (v >> 8) & 0x3F, (v >> 20) & 0x3F
    io = d["IO_PLL"]["mhz_if_psclk_33.333"]
    d["FCLK0"] = {"raw": "0x%08X" % v, "DIV0": div0, "DIV1": div1,
                  "product": div0 * div1,
                  "true_mhz": round(io / (div0 * div1), 4),
                  "pynq_assumed_pll_mhz": 1500.0,
                  "pynq_reported_mhz": round(1500.0 / (div0 * div1), 4),
                  "ratio_pynq_vs_true": round(1500.0 / io, 3)}
    return d


def push_frames(pipe, orig, proc, tag, n=3):
    """推 n 帧，每帧停 FRAME_DWELL 秒；逐帧记 0x24 == FRAME_ID。"""
    out = []
    for i in range(1, n + 1):
        t0 = time.time()
        fid = pipe.show(orig, proc)
        ok = pipe.wait(fid, 1.0)
        latched = rd32(0x24)
        st = regs()
        rec = {"tag": tag, "i": i, "fid": fid, "latched": "0x%08X" % latched,
               "match_0x24_eq_fid": bool(latched == fid), "wait_ok": bool(ok),
               "call_s": round(time.time() - t0, 3), "t": time.strftime("%H:%M:%S"),
               "UFCNT": st["UFCNT_0x2C"], "STATUS": st["STATUS"], "PIX_HB": st["PIX_HB"],
               "CTRL": st["CTRL"], "VMODE": st["VMODE"]}
        R["frames"].append(rec)
        out.append(rec)
        print("[frame] %s #%d fid=%d latched=%s match=%s wait=%s UFCNT=%d CTRL=%s"
              % (tag, i, fid, rec["latched"], rec["match_0x24_eq_fid"], ok,
                 rec["UFCNT"], rec["CTRL"]), flush=True)
        time.sleep(FRAME_DWELL)
    return out


def main():
    import pynq.pl_server.embedded_device  # noqa: F401
    import display_demo as dd
    import display_layout as dl

    R["pll"] = pll_decode()
    print("[pll] %s" % json.dumps(R["pll"], ensure_ascii=False), flush=True)

    # 契约 §8.2：心跳先行（setup() 之前只读 0x30）
    pipe = dd.DisplayPipeline(mode=0)
    alive, mhz = pipe.pix_alive(200)
    R["pix_alive_before_setup"] = {"alive": bool(alive), "mhz": round(mhz, 4)}
    print("[pix] alive=%s %.3f MHz（期望 74.219）" % (alive, mhz), flush=True)
    if not alive:
        raise SystemExit("像素时钟没跑，不动 AXI")

    R["regs_before_setup"] = regs()
    pipe.setup()                      # 分配双缓冲 → 切模式 → enable=1（test_pattern 仍在）
    PIPE["pipe"] = pipe
    R["setup"] = {"fb_addr": "0x%08X" % pipe.fb_addr, "block_bytes": pipe.block_bytes}
    R["regs_after_setup"] = regs()    # 期望 CTRL=0x15（setup 的 enable() 只改 bit0）
    print("[setup] fb=0x%08X block=%d  CTRL(after)=%s"
          % (pipe.fb_addr, pipe.block_bytes, R["regs_after_setup"]["CTRL"]), flush=True)

    # ★ 直接写 CTRL=0x01：enable=1 / test_pattern=0 / motion=0
    wr32(0x00, 0x01)
    time.sleep(0.35)                  # 让 scan_sel 在 vsync 边界采纳 cfg_test=0
    R["regs_after_ctrl01"] = regs()
    print("[ctrl] 写 0x01 → %s" % R["regs_after_ctrl01"], flush=True)
    if rd32(0x00) & 0x04:
        raise SystemExit("test_pattern 仍为 1（0x%08X）：扫描器没开，停止" % rd32(0x00))

    sampler = Sampler()
    sampler.phase = "A_noise"
    sampler.start()
    PIPE["sampler"] = sampler

    # ---- ② 噪声版（有 golden 背书）----
    x0 = np.load(os.path.join(PROBE, "x0_256_seed1.npy"))
    o1 = np.load(os.path.join(PROBE, "out1_256_seed1.npy"))
    noise_orig = to_uint8(x0)
    noise_proc = to_uint8(o1)
    R["phaseA"]["x0_sha1"] = sha1_bytes(x0)
    R["phaseA"]["out1_sha1"] = sha1_bytes(o1)
    R["phaseA"]["orig_u8"] = stats8(noise_orig)
    R["phaseA"]["proc_u8"] = stats8(noise_proc)
    R["phaseA"]["mapping"] = "u8 = round(255 * clip(x,0,1))"
    print("[map] orig_u8 sha1=%s  proc_u8 sha1=%s"
          % (R["phaseA"]["orig_u8"]["sha1"], R["phaseA"]["proc_u8"]["sha1"]), flush=True)

    fr = push_frames(pipe, noise_orig, noise_proc, "noise")
    R["phaseA"]["frames"] = fr
    R["phaseA"]["compose_sha1_last"] = sha1_bytes(dl.compose(noise_orig, noise_proc, 1280, 720))

    # ---- ③ 20 s 保持期：每 5 s 一次显式 0x2C ----
    hold = []
    t_hold = time.time()
    for k in range(5):
        hold.append({"k": k, "t": time.strftime("%H:%M:%S"), "UFCNT": rd32(0x2C),
                     "LATCHED": "0x%08X" % rd32(0x24), "STATUS": "0x%08X" % rd32(0x04),
                     "PIX_HB": "0x%08X" % rd32(0x30)})
        if k < 4:
            time.sleep(5.0)
    R["phaseA"]["hold_20s"] = hold
    R["phaseA"]["hold_span_s"] = round(time.time() - t_hold, 2)
    uf = [h["UFCNT"] for h in hold]
    R["phaseA"]["hold_all_zero"] = bool(all(v == 0 for v in uf))
    R["phaseA"]["hold_max"] = max(uf)
    print("[hold] 20 s 采样 0x2C = %s ⇒ all_zero=%s" % (uf, R["phaseA"]["hold_all_zero"]),
          flush=True)
    R["phaseA"]["done"] = True
    R["phaseA"]["t_end"] = time.strftime("%H:%M:%S")
    json.dump(R, open(AOUT, "w"), indent=1, ensure_ascii=False)
    print("[A] DONE  json -> %s ; 画面停在噪声第 3 帧，等闸门 %s" % (AOUT, GATE), flush=True)

    # ---- 等闸门 ----
    sampler.phase = "wait_gate"
    t_wait = time.time()
    while not os.path.exists(GATE):
        if time.time() - t_wait > GATE_TIMEOUT:
            break
        time.sleep(0.5)
    gated = os.path.exists(GATE)
    R["gate"] = {"seen": bool(gated), "waited_s": round(time.time() - t_wait, 1)}
    print("[gate] seen=%s waited=%.1f s" % (gated, time.time() - t_wait), flush=True)
    if not gated:
        raise SystemExit("闸门超时（%.0f s）⇒ 不跑真图，恢复彩条" % GATE_TIMEOUT)

    # ---- ⑤ 真图 ----
    sampler.phase = "B_net"
    from pynq import Overlay, Clocks
    ov = Overlay(BIT, download=False)
    R["phaseB"]["fclk_reported"] = Clocks.fclk0_mhz
    from bin_engine import BinConvEngine
    from bin_net import BinNet

    eng = BinConvEngine(ov, unit_id=0, pack_dir=PACK)
    eng.load_weights()
    net = BinNet(PACK, engine=eng, H=256, W=256)
    real = np.load(os.path.join(PROBE, "real_in_0000_256.npy"))
    x_in = to_float_tensor(real)
    R["phaseB"]["input"] = stats8(real)
    R["phaseB"]["x_in_sha1"] = sha1_bytes(x_in)
    print("[real] 输入 (3,256,256) sha1=%s" % R["phaseB"]["x_in_sha1"], flush=True)

    t0 = time.time()
    outs, st = net.run(x_in, pipeline=True, nbuf=2)
    R["phaseB"]["run_s"] = round(time.time() - t0, 2)
    R["phaseB"]["stats"] = {k: st.get(k) for k in
                            ("total", "pl", "ps", "ps_prologue", "ps_epilogue",
                             "ps_nonconv", "overlap", "wait", "convs", "mmio_writes")}
    out1 = outs["out1"]
    proc = to_uint8(out1)
    R["phaseB"]["out1"] = {"shape": list(out1.shape),
                           "min": round(float(out1.min()), 5),
                           "max": round(float(out1.max()), 5),
                           "mean": round(float(out1.mean()), 5),
                           "sha1": sha1_bytes(out1)}
    R["phaseB"]["proc_u8"] = stats8(proc)
    R["phaseB"]["mean_abs_diff_f32"] = round(float(np.abs(out1 - x_in).mean()), 5)
    R["phaseB"]["mean_abs_diff_u8"] = round(float(np.abs(
        proc.astype(np.int16) - real.astype(np.int16)).mean()), 3)
    R["phaseB"]["frac_pix_changed_gt2"] = round(float(
        (np.abs(proc.astype(np.int16) - real.astype(np.int16)).max(axis=2) > 2).mean()), 4)
    np.save(os.path.join(PROBE, "real_out1_256.npy"), np.ascontiguousarray(out1, np.float32))
    np.save(os.path.join(PROBE, "real_proc_u8_256.npy"), proc)
    print("[real] out1 min=%.4f max=%.4f mean=%.4f sha1=%s"
          % (out1.min(), out1.max(), out1.mean(), R["phaseB"]["out1"]["sha1"]), flush=True)
    print("[real] proc_u8 sha1=%s  mean|out-in|(f32)=%.4f  u8=%.2f  changed>2: %.1f%%"
          % (R["phaseB"]["proc_u8"]["sha1"], R["phaseB"]["mean_abs_diff_f32"],
             R["phaseB"]["mean_abs_diff_u8"], 100 * R["phaseB"]["frac_pix_changed_gt2"]),
          flush=True)

    sampler.phase = "B_frames"
    fr2 = push_frames(pipe, real, proc, "real")
    R["phaseB"]["frames"] = fr2
    R["phaseB"]["compose_sha1_last"] = sha1_bytes(dl.compose(real, proc, 1280, 720))
    hold2 = []
    for k in range(3):
        hold2.append({"k": k, "t": time.strftime("%H:%M:%S"), "UFCNT": rd32(0x2C)})
        time.sleep(2.0)
    R["phaseB"]["hold"] = hold2
    R["phaseB"]["hold_all_zero"] = bool(all(h["UFCNT"] == 0 for h in hold2))
    print("[hold2] 0x2C = %s" % [h["UFCNT"] for h in hold2], flush=True)
    return 0


def cleanup():
    sampler = PIPE.get("sampler")
    if sampler is not None:
        sampler._stop = True
    R["samples"] = list(sampler.samples) if sampler is not None else []
    if R["samples"]:
        R["samples_all_zero"] = bool(all(s["UFCNT"] == 0 for s in R["samples"]))
        R["samples_max"] = max(s["UFCNT"] for s in R["samples"])
        R["samples_n"] = len(R["samples"])
    try:
        wr32(0x00, 0x15)
        time.sleep(0.2)
        R["regs_restored"] = regs()
    except Exception:                                  # noqa: BLE001
        traceback.print_exc()
    try:
        if PIPE.get("pipe") is not None:
            PIPE["pipe"].close()
        R["buffers_freed"] = True
    except Exception:                                  # noqa: BLE001
        R["buffers_freed"] = False
        traceback.print_exc()
    R["sync_rc"] = subprocess.run(["sync"]).returncode
    try:
        R["fpga0_state"] = open("/sys/class/fpga_manager/fpga0/state").read().strip()
    except Exception:                                  # noqa: BLE001
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
        except Exception:                              # noqa: BLE001
            traceback.print_exc()
        R["rc"] = rc
        R["t_wall_end"] = time.strftime("%F %T")
        try:
            json.dump(R, open(OUT, "w"), indent=1, ensure_ascii=False)
            print("[json] -> %s rc=%d" % (OUT, rc), flush=True)
        except Exception:                              # noqa: BLE001
            traceback.print_exc()
    sys.exit(rc)
