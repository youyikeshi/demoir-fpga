#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""health_t137.py —— T-137 收尾：**事故取证 + 把板子复位到已知good状态**。

背景：T-137 租约 C 在 93.75 MHz 上第一次 PL 操作就没完成（`E1E2 等 idle 超时 30.0s`），
随后探针进程被 SIGSEGV 杀掉（rc=139）。按铁律"段错误 ⇒ 停、报"，此后不再试 100 MHz。
本脚本做两件事（**只读 + 一次 load**）：

  ① 复位**之前**直接用 `/dev/mem` 读两个 IP 的 `AP_RETURN` 与 `0x310`（不需要 Overlay 对象），
     看 PL 是不是还停在"busy"（93.75 那一跑可能一直没退出，正在访存）；
  ② load v54（本次唯一一次 download）+ 设 50 MHz → 复核 magic/0x310/AP busy
     ⇒ 把板子留在"v54 @50 MHz、PL 空闲"的已知good状态（与 T-136 收尾同态）。

只读、不写任何 PL 寄存器；收尾只 `sync; sync`（不 poweroff）。
"""
from __future__ import annotations

import argparse
import json
import mmap
import os
import struct
import sys
import time

BIT_DEFAULT = "/home/xilinx/demoir/design_1_150v54.bit"
# 绝对地址（T-137 实测基址：bin_unit_0=0x40060000 / conv_uni_0=0x40040000）
ADDR = {
    "bin_0x310_CHAIN_STATUS": 0x4006_0310,
    "bin_AP_RETURN(0x10010)": 0x4007_0010,
    "conv_AP_RETURN(0x10010)": 0x4005_0010,
}
MAGIC_ABS = 0x4007_0314
MAGIC_WANT = 0x5030_0001
OUT = {}


def log(s=""):
    s = str(s)
    print(s, flush=True)


def devmem_read(addr):
    """读 /dev/mem 的 32 位寄存器（只读；页对齐后 mmap 再取偏移）。"""
    page = int(addr) & ~0xFFF
    off = int(addr) - page
    with open("/dev/mem", "rb") as f:
        mm = mmap.mmap(f.fileno(), 0x1000, mmap.MAP_SHARED,
                       mmap.PROT_READ, offset=page)
        try:
            return struct.unpack("<I", mm[off:off + 4])[0]
        finally:
            mm.close()


def decode_ap(raw):
    if raw is None:
        return dict(raw=None, done=None, busy=None, err=None)
    raw = int(raw) & 0xFFFFFFFF
    return dict(raw="%#010x" % raw, done=int(raw & 1), busy=int((raw >> 1) & 1),
                err=int((raw >> 8) & 0xFF))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default=BIT_DEFAULT)
    ap.add_argument("--freq", type=float, default=50.0)
    ap.add_argument("--out", default="/home/xilinx/demoir/t137d_out")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    tag = time.strftime("%H%M%S")
    log("[health] bit = %s ；目标 fclk = %g MHz" % (a.bit, a.freq))

    # ---------- ① 复位前：/dev/mem 只读 ----------
    log("")
    log("① 复位前（不 new Overlay，直接 /dev/mem 只读）")
    pre = {}
    for name, addr in list(ADDR.items()) + [("MAGIC(0x314)", MAGIC_ABS)]:
        try:
            v = devmem_read(addr)
            pre[name] = "%#010x" % v
            log("   %#010x  %-24s = %#010x" % (addr, name, v))
        except Exception as ex:                                      # noqa: BLE001
            pre[name] = None
            log("   %#010x  %-24s 读失败：%r" % (addr, name, ex))
    OUT["pre_load_devmem"] = pre
    try:
        b = decode_ap(int(pre["bin_AP_RETURN(0x10010)"], 16))
        c = decode_ap(int(pre["conv_AP_RETURN(0x10010)"], 16))
        s = int(pre["bin_0x310_CHAIN_STATUS"], 16)
        log("   ⇒ bin AP busy=%s done=%s err=%s ；conv AP busy=%s done=%s err=%s ；0x310=%s"
            % (b["busy"], b["done"], b["err"], c["busy"], c["done"], c["err"],
               pre["bin_0x310_CHAIN_STATUS"]))
        OUT["pre_idle"] = dict(bin_busy=b["busy"], conv_busy=c["busy"],
                               st310_zero=(s == 0),
                               stuck=bool(b["busy"] or c["busy"] or s != 0))
    except Exception as ex:                                          # noqa: BLE001
        OUT["pre_idle"] = dict(error=repr(ex)[:200])

    # ---------- ② load + 设 50 ----------
    try:
        import pynq.pl_server.embedded_device as _emb      # noqa: F401
    except Exception:                                                # noqa: BLE001
        pass
    from pynq import Overlay, Clocks
    log("")
    log("② load v54（本次唯一一次 download=True）+ 设 %g MHz" % a.freq)
    t0 = time.time()
    ov = Overlay(a.bit, download=True)
    log("   Overlay 用时 %.3f s ；IP=%s" % (time.time() - t0,
                                            sorted(ov.ip_dict.keys())))
    Clocks.fclk0_mhz = float(a.freq)
    time.sleep(0.5)
    rb = float(Clocks.fclk0_mhz)
    base = int(ov.bin_unit_0.mmio.base_addr)
    # 复用 ps/ 的偏移常量（只读）
    sys.path.insert(0, "/home/xilinx/demoir/ps/pynq")
    import bin_engine as BE                                        # noqa: E402
    magic = int(ov.bin_unit_0.mmio.read(int(BE.CFG + BE.V50_CHAIN_REG["CHAIN_ID"]))) & 0xFFFFFFFF
    st310 = int(ov.bin_unit_0.mmio.read(int(BE.V50_CHAIN_REG["CHAIN_STATUS"]))) & 0xFFFFFFFF
    aps = {}
    for ip in ("bin_unit_0", "conv_uni_0"):
        aps[ip] = decode_ap(int(getattr(ov, ip).mmio.read(int(BE.CFG + BE.BIN_REG["AP_RET"]))))
    log("   fclk0 读回 = %g MHz ；base = %#010x" % (rb, base))
    log("   magic(0x314) = %#010x（期望 %#010x）%s"
        % (magic, MAGIC_WANT, "✓" if magic == MAGIC_WANT else "✗✗"))
    log("   0x310 = %#010x ⇒ %s" % (st310, "空闲 ✓" if st310 == 0 else "✗✗ 非零"))
    for ip, d in aps.items():
        log("   %-11s AP_RETURN=%s ⇒ busy=%s err=%s" % (ip, d["raw"], d["busy"], d["err"]))
    ok = bool(magic == MAGIC_WANT and st310 == 0
              and all((d["busy"] == 0 and d["err"] == 0) for d in aps.values()))
    OUT["after_load"] = dict(fclk_readback=rb, base="%#010x" % base,
                             magic="%#010x" % magic, st310="%#010x" % st310,
                             ap={k: v for k, v in aps.items()}, ok=ok)
    log("")
    log("★★ 复位结论：%s" % ("**板子已在已知good状态（v54 @ %g MHz、PL 空闲）**" % rb
                            if ok else "**✗✗ 仍未回到good状态，需要人工处理**"))
    OUT["ok"] = ok
    OUT["seconds"] = round(time.time() - t0, 3)
    if os.environ.get("NO_SYNC") != "1":
        os.sync()
        os.sync()
    p = os.path.join(a.out, "health_%s.json" % tag)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(OUT, f, indent=1, ensure_ascii=False)
    log("[health] json = %s" % p)
    return 0 if ok else 6


if __name__ == "__main__":
    sys.exit(main())
