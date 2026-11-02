#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runs/board_t86/t86_flag_probe.py —— T-86 §3 的**追加判读**（不是第四个实验段）

问题：4 KB 恒等冒烟数据全对（md5 相等、0 字节差、4 个 DBG 计数非 0），
      但 `DBG_FLAG = 0x4`（bit2「写 burst 跨 4 KB」sticky）不满足判据。

RTL 取证（`fconv_axi_wrap.v:777-779`）：
    wire w_b4k = w_push && (wr_wr_addr[31:12] != wstart[31:12]);
    wire w_close = (wlen != 0) && (… || w_b4k || …) && wd_room;
  `wstart` 复位值 = 0；首个 beat 时 `wlen == 0 ⇒ wstart 还没更新`，于是
  `w_b4k = (页 ≠ 0) = 1` ⇒ **首次 push 必然把 flg[2] 置起**（假阳性）；
  而 `w_close` 要求 `wlen != 0`，所以那一下**不会**多切一笔 burst。

验证方法（只读，不 reload 位流）：同一组缓冲连跑两次——
  run1：wstart=0（刚复位）⇒ 预期 flag bit2 = 1
  run2：wstart 已被 run1 的最后一段写成 y 的页 ⇒ 预期 flag bit2 = 0
  两次都应 nAW == 32（512 拍 / WR_BURST_MAX=16）＝没有因跨页多切的 burst。

用法（板端，PL 已由 t86_probe.py load 好；本脚本 `download=False`，**不重载**）：
    python3 t86_flag_probe.py --bit /tmp/probe_T86/design_1_150v45.bit --out /home/xilinx/demoir/t86_out
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, "/home/xilinx/demoir/ps/pynq")
sys.path.insert(0, "/tmp/probe_T86")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    import numpy as np
    import pynq
    import t86_probe as P
    from pynq import Overlay, Clocks

    res = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "runs": []}

    def log(m=""):
        print(m, flush=True)

    # ★ download=False：位流已经是 v45，本脚本只复用（不再签发 load）
    ov = Overlay(args.bit, download=False)
    log("Overlay(download=False) ok（PL 未被重载）")
    Clocks.fclk0_mhz = 25.0
    time.sleep(0.2)
    rb = float(Clocks.fclk0_mhz)
    log("Clocks.fclk0_mhz 读回 = %s" % rb)
    res["fclk0_mhz"] = rb
    res["overlay"] = {"fconv_0": int(ov.ip_dict["fconv_0"]["phys_addr"]),
                      "bin_unit_0": int(ov.ip_dict["bin_unit_0"]["phys_addr"]),
                      "conv_uni_0": int(ov.ip_dict["conv_uni_0"]["phys_addr"])}

    x = pynq.allocate(4096, dtype=np.uint8)
    w = pynq.allocate(4096, dtype=np.uint8)
    y = pynq.allocate(8192, dtype=np.uint8)
    # 第二组：y2 落在**另一个 4 KB 页**（CMA 顺序分配）⇒ 用来做决定性负对照：
    #   预测量：wstart 此刻 = 上一轮最后一段的页 ⇒ y2 页 ≠ wstart 页 ⇒ 首个 beat
    #   必然再置 flg[2]，而 nAW 仍 32（没有真的跨页切 burst）；同一块 y2 连跑第二次
    #   ⇒ wstart 页 = y2 页 ⇒ flg[2] 不再置位。
    y2 = pynq.allocate(8192, dtype=np.uint8)
    for b in (x, w, y, y2):
        np.frombuffer(b, dtype=np.uint8)[:] = 0
    np.frombuffer(y, dtype=np.uint8)[4096:] = 0xA5
    np.frombuffer(y2, dtype=np.uint8)[4096:] = 0xA5
    for b in (x, w, y, y2):
        b.flush()
    log("缓冲：x=%#x w=%#x y=%#x（y 页 = %#x）  y2=%#x（y2 页 = %#x）"
        % (int(x.physical_address), int(w.physical_address), int(y.physical_address),
           int(y.physical_address) >> 12 << 12, int(y2.physical_address),
           int(y2.physical_address) >> 12 << 12))
    res["y_pa"] = int(y.physical_address)
    res["y2_pa"] = int(y2.physical_address)
    res["same_page"] = bool((int(y.physical_address) >> 12) == (int(y2.physical_address) >> 12))

    plan = [("A1", y), ("A2", y), ("B1", y2), ("B2", y2)]
    for tag, buf in plan:
        r = P.run_child(lambda: P.hp2_smoke_body(x, w, buf),
                        os.path.join(args.out, "t86_flag_child_%s.json" % tag), 120.0,
                        "flag-%s" % tag)
        ch = (r.get("child") or {})
        if not ch.get("ok"):
            log("  %s ✗ %r" % (tag, ch.get("error") or r))
            res["runs"].append({"tag": tag, "ok": False, "raw": r})
            break
        h = ch["result"]
        d = h["dbg"]
        row = {"tag": tag, "ok": True, "y_pa": int(buf.physical_address),
               "md5_x": h["md5_x"], "md5_y": h["md5_y"],
               "equal_md5": h["equal_md5"], "nAW": d["nAW"], "nW": d["nW"],
               "nAR": d["nAR"], "nR": d["nR"], "flag": d["flag"],
               "flag_low3": h["dbg_flag_low3"], "canary_ok": h["canary_ok"],
               "status": h["status"], "seconds": h["seconds"]}
        res["runs"].append(row)
        log("  %s: y=%#x md5 相等=%s  nW=%d nAW=%d nAR=%d nR=%d  DBG_FLAG=%#x  "
            "canary_ok=%s  STATUS=%#010x  %.4fs"
            % (tag, row["y_pa"], row["equal_md5"], d["nW"], d["nAW"], d["nAR"], d["nR"],
               d["flag"], row["canary_ok"], h["status"], h["seconds"]))

    out_json = os.path.join(args.out, "t86_flag_result.json")

    def _dump():
        with open(out_json, "w") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())

    _dump()
    log("")
    if len(res["runs"]) == 4 and all(r.get("ok") for r in res["runs"]):
        A1, A2, B1, B2 = res["runs"]
        fA1, fA2 = A1["flag"] & 0x7, A2["flag"] & 0x7
        fB1, fB2 = B1["flag"] & 0x7, B2["flag"] & 0x7
        log("判读：A1 flag_low3=%d A2=%d   B1(新页) flag_low3=%d B2=%d"
            % (fA1, fA2, fB1, fB2))
        log("      nAW = %d/%d/%d/%d（恒期望 32 = 512 拍 / WR_BURST_MAX16）"
            % (A1["nAW"], A2["nAW"], B1["nAW"], B2["nAW"]))
        ok = (fA1 == 0 and fA2 == 0 and fB1 == 4 and fB2 == 0
              and {A1["nAW"], A2["nAW"], B1["nAW"], B2["nAW"]} == {32}
              and all(r["equal_md5"] and r["canary_ok"] for r in res["runs"]))
        res["verdict_ok"] = bool(ok)
        res["verdict"] = (
            "DBG_FLAG bit2 是**假阳性**：它只在『首拍地址的页 ≠ 上一段 burst 起始页』"
            "时置位（复位后 wstart=0 / 换到新 y 页的首跑），4 次运行 nAW 恒为 32"
            "＝写通路从未真的跨 4 KB 切 burst，数据逐字节正确、金丝雀完好。"
            if ok else
            "与假设不符 ⇒ 需人工复核（贴 A1/A2/B1/B2 四个 flag 与 nAW）")
        _dump()
        log("⇒ " + res["verdict"])
        return 0 if ok else 1
    log("⇒ 证据不足（见上）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
