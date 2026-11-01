#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-17 / J4：**板端投影**（指令计数模型，不碰板子）。

这里没有任何实测 —— 板子断电。所有数字来自"内核源码结构 → 指令条数 × A9 的
发射率"，因此它是一个**模型**，必须连同假设一起读。

模型假设（全部写死在这里，改假设必须改这段）
--------------------------------------------
A1 Cortex-A9 的 128-bit NEON 浮点：一条 Q 寄存器 `vmul/vadd/vmla` = **2 cycle**
   （A9 的 NEON 数据通路 64-bit，128-bit 指令拆两拍）⇒ **0.5 条/cycle/核**。
   ⇒ 单核上限 = 4 lane / 2 cycle = **2 MAC/cycle**；双核 667 MHz 峰值
   **2.67 GMAC/s**（与 `runs/psops/REPORT.md` 里"理论 float32 峰值 ~4.8 GFLOP/s"一致）。
A2 双核按**输出行**切（`BIN_CONVPF_THREADS=2`，默认）⇒ 线性 2×。
A3 PS 时钟 **667 MHz**（Zynq-7020 常规值；本仓库没有实测记录 ⇒ 这是假设，
   投影与它线性相关）。
A4 每条向量装载也是 2 cycle；**权重的标量装载**按 1 cycle 计（`vldr` 走 LSU，
   A9 上可与 NEON 部分重叠 ⇒ 取保守的 1 cycle）。
A5 边界 tile 的"栈上 padded 行"：每个元素 2 cycle（load+store 各一拍）。
A6 内存流：板端 0.50 GB/s（T-17 归因里用板端 `np.add` 标定的同一个数）。
A7 模型给的是**发射饱和**下的下界；实际给一个 75% 工作效率的"现实"列。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PSCODE = os.path.realpath(os.path.join(HERE, *[os.pardir] * 2))
sys.path.insert(0, os.path.join(PSCODE, "ps", "pynq"))
import bin_ref_numpy as rn                                  # noqa: E402

F_MHZ = 667.0
QCYC = 2.0             # 一条 Q 指令 2 cycle
WCYC = 2.0             # 标量装载 2 cycle（保守）
NCORE = 2
MEM_GBS = 0.50
EFF = 0.75             # 现实效率

# 真实层（= 256² 运行点；与归因报告同一批形状）
LAYERS = [("encoder.conv_first.0", 2, (128, 128)),
          ("decoder.decoder_3.conv", 1, (32, 32)),
          ("decoder.decoder_2.conv", 1, (64, 64)),
          ("decoder.decoder_1.conv", 1, (128, 128))]


def model_layer(cout, cin, k, H, W, pad, NR=8):
    """返回该层的 (向量指令数, 标量装载数, scratch 元素数, 标量尾像素数, 字节数)。"""
    Ho, Wo = H + 2 * pad - k + 1, W + 2 * pad - k + 1
    L = NR + k - 1
    vecload = vmla = wload = 0
    scr_elem = 0
    tail_px = 0
    bytes_ = 0
    for ho in range(Ho):
        wb = 0
        while wb + NR <= Wo:
            base = wb - pad
            inside = (base >= 0) and (base + L <= W)
            for ci in range(cin):
                for ky in range(k):
                    hh = ho + ky - pad
                    rok = 0 <= hh < H
                    fast = inside and rok
                    if not fast:
                        scr_elem += L
                    for kx in range(k):
                        # 每个 co-chunk(4)：**2 个向量装载（8 像素）+ 8 个 vmla
                        # （4 个 co × 2 个 Q 寄存器）+ 4 个权重标量装载**
                        vecload += 2 * (cout // 4)
                        vmla += 8 * (cout // 4)          # variant=1：8 条 vmla
                        wload += 4 * (cout // 4)
            wb += NR
        tail_px += Wo - wb
    # 标量尾像素：按 A9 标量约 5 cycle/MAC 估
    mac_tail = tail_px * cout * cin * k * k * Ho
    scr_cyc = scr_elem * 2.0
    return dict(Ho=Ho, Wo=Wo, vecload=vecload, vmla=vmla, wload=wload,
                scr_elem=scr_elem, tail_px=tail_px, mac_tail=mac_tail,
                scr_cyc=scr_cyc,
                mac=cout * cin * k * k * Ho * Wo,
                bytes_=2 * (cin * H * W + cout * Ho * Wo) * 4 + cout * cin * k * k * 4)


def main():
    blob = rn.Blob(os.path.join(PSCODE, "deploy", "bin_weights_new"))
    rows = []
    print("== J4 板端投影（指令计数模型；假设见文件头 A1~A7）==")
    print("   A9 NEON 0.5 Q-op/cycle/核，%.0f MHz，%d 核；内存 %.2f GB/s"
          % (F_MHZ, NCORE, MEM_GBS))
    print("   %-24s %8s %8s %10s %10s %10s"
          % ("layer", "MAC(M)", "向量指令", "scratch元", "tail像素", "内存MB"))
    t_v1 = t_v0 = (0.0, 0.0)
    t_mem = t_scr = 0.0
    tot_mac = 0
    for (name, pad, (H, W)) in LAYERS:
        w, b = blob.fp_layer(name)
        cout, cin, k = w.shape[0], w.shape[1], w.shape[2]
        d = model_layer(cout, cin, k, H, W, pad)
        # 两个包络（见文件头 A4）：
        #   lo = 标量装载与 NEON 算术**完全重叠**（A9 双发射、LSU 独立）
        #   hi = 标量装载**完全串行**在 NEON 之后（最保守）
        # variant=1 每个 co-chunk：2 vld + 8 vmla + 4 标量权重装载
        # variant=0 每个 co-chunk：2 vld + 8 vmul + 8 vadd + 4 标量权重装载
        cyc_v1_lo = (d["vecload"] + d["vmla"]) * QCYC
        cyc_v0_lo = (d["vecload"] + 2 * d["vmla"]) * QCYC
        cyc_v1_hi = cyc_v1_lo + d["wload"] * WCYC
        cyc_v0_hi = cyc_v0_lo + d["wload"] * WCYC
        extra = (d["scr_cyc"] + d["mac_tail"] * 5.0) / NCORE
        cyc_v1 = (cyc_v1_lo / NCORE + extra, cyc_v1_hi / NCORE + extra)
        cyc_v0 = (cyc_v0_lo / NCORE + extra, cyc_v0_hi / NCORE + extra)
        ms_v1 = (cyc_v1[0] / (F_MHZ * 1e6) * 1e3, cyc_v1[1] / (F_MHZ * 1e6) * 1e3)
        ms_v0 = (cyc_v0[0] / (F_MHZ * 1e6) * 1e3, cyc_v0[1] / (F_MHZ * 1e6) * 1e3)
        ms_mem = d["bytes_"] / (MEM_GBS * 1e9) * 1e3
        ms_scr = d["scr_cyc"] / NCORE / (F_MHZ * 1e6) * 1e3
        t_v1 = (t_v1[0] + ms_v1[0], t_v1[1] + ms_v1[1])
        t_v0 = (t_v0[0] + ms_v0[0], t_v0[1] + ms_v0[1])
        t_mem += ms_mem
        t_scr += ms_scr
        tot_mac += d["mac"]
        print("   %-24s %8.1f %8.1fM %10d %10d %10.2f"
              % (name, d["mac"] / 1e6,
                 (d["vecload"] + d["vmla"]) / 1e6, d["scr_elem"],
                 d["tail_px"], d["bytes_"] / 1e6))
        rows.append(dict(name=name, mac=d["mac"], ms_v1=ms_v1, ms_v0=ms_v0,
                         ms_mem=ms_mem, ms_scr=ms_scr,
                         mac_per_qop=d["mac"] / max(1, d["vecload"] + d["vmla"])))
    print("\n   Σ MAC = %.2f MMAC = %.1f MFLOP" % (tot_mac / 1e6, tot_mac * 2 / 1e6))
    print("   Σ 计算 [lo=装载全重叠, hi=装载全串行]:")
    print("        variant=1 %7.1f ~ %7.1f ms   (再乘 1/%.2f 效率 ⇒ %.1f ~ %.1f ms)"
          % (t_v1[0], t_v1[1], EFF, t_v1[0] / EFF, t_v1[1] / EFF))
    print("        variant=0 %7.1f ~ %7.1f ms   (再乘 1/%.2f 效率 ⇒ %.1f ~ %.1f ms)"
          % (t_v0[0], t_v0[1], EFF, t_v0[0] / EFF, t_v0[1] / EFF))
    print("   Σ 内存流（%.2f GB/s）: %7.1f ms（其中 scratch %.1f ms）"
          % (MEM_GBS, t_mem, t_scr))
    py = 5.0
    peak = NCORE * 2.0 * F_MHZ * 1e6 / 1e9
    for tag, t in (("variant=1 (vmla)", t_v1), ("variant=0 (mul+add)", t_v0)):
        lo = t[0] + t_mem + py
        hi = t[1] / EFF + t_mem + py
        print("   ⇒ **%s 投影**：%.0f ~ %.0f ms  vs 判据 350 ms ⇒ %s"
              % (tag, lo, hi, "PASS" if hi <= 350 else
                 ("压线" if lo <= 350 else "FAIL")))
        print("      有效速率 %.2f ~ %.2f GMAC/s（A9 峰值 %.2f GMAC/s 的 %.0f%% ~ %.0f%%）"
              % (tot_mac / (hi / 1e3) / 1e9, tot_mac / (lo / 1e3) / 1e9, peak,
                 100 * tot_mac / (hi / 1e3) / 1e9 / peak,
                 100 * tot_mac / (lo / 1e3) / 1e9 / peak))
        print("      达到 350 ms 需要 ≥ %.2f GMAC/s = A9 峰值的 %.0f%%"
              % (tot_mac / ((350 - t_mem - py) / 1e3) / 1e9,
                 100 * tot_mac / ((350 - t_mem - py) / 1e3) / 1e9 / peak))
    print("   MAC / (向量装载+vmla) = %.2f 条/op（NR=8、MR=4 的结构值 3.20）"
          % (tot_mac / sum(r["mac"] / r["mac_per_qop"] for r in rows)))
    out = os.path.join(HERE, "_t17_projection.json")
    json.dump(dict(rows=rows, t_v1=list(t_v1), t_v0=list(t_v0), t_mem=t_mem, mac=tot_mac,
                   eff=EFF, f_mhz=F_MHZ, ncore=NCORE, mem_gbs=MEM_GBS),
              open(out, "w"), indent=1)
    print("   明细写入", out)


if __name__ == "__main__":
    main()
