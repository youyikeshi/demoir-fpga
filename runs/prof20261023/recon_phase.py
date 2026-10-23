#!/usr/bin/env python3
"""把 conv 128.4 s 拆成 填充 / 权重重装 / 计算 / 写回 —— 全部系数来自板上实测。

数据来源
  1. 逐层实测：/home/xilinx/demoir_new/layers.json（纯 C8 bit 9385dc82，FCLK=150MHz，
     --profile-layers，247 个卷积合计 128.445 s）
  2. 板上形状扫描（本轮新测）：runs/prof20261023/phase_split{,2,3}.json
     · main   ：320² 上 (cin,cout) 互换、面积扫描、k=3 真实形状
     · phase2 ：cout%8!=0，打破"weight 字节 ∝ 迭代数"的共线性
     · phase3 ：1×1 图，把填充/写回字节压到 0，单独量权重读

系数（全部由上表回归/差分得到，见各 print 的推导）
  aF  填充每字节拍数          10.5   （320² 扫描；fill_heavy 与 cin 扫描一致）
  aW  写回每字节拍数          14.0   （write_heavy 8→192）
  aT  k=1 权重读每字节拍数     10.5   （1×1 扫描：o1→o8 差分 12.9；与大 burst 一致）
  aT3 k=3 权重读每字节拍数     16.3   （由 k=3 组总时间 − cosim 计算项倒推；1×1 实测 20）
  II  ic 迭代拍数             k=1: 1.0 / k=3: 5.2（cosim 339,120 拍反推）/ k=5: 59
  const 每层启动              ~0.1 ms（PS 写寄存器 + 轮询，见 1×1 的 1.1 万拍）

用法：python3 recon_phase.py
"""
import json

FCLK = 150e6
ROOT = "/media/felix/Data/coding/demoir-fpga"
from phase_model import phase_counts  # noqa: E402

MAN = json.load(open(ROOT + "/pynq_specialized_code/ps/manifest_640.json"))
by = {o["name"]: o for o in MAN["ops"] if o.get("op") == "conv" and o.get("needed")}

A_F = 10.5      # 拍/字节（填充读）
A_W = 14.0      # 拍/字节（写回）
A_T1 = 10.5     # 拍/字节（k=1 权重读）
A_T3 = 16.3     # 拍/字节（k=3 权重读）
A_T5 = 16.3
II = {1: 1.00, 3: 5.2, 5: 59.0}
CONST = 0.12e-3 * FCLK     # 每层启动拍数（1×1 微基准实测约 1.1~1.7 万拍）

rows = []
for L in json.load(open(ROOT + "/runs/prof20261023/layers.json"))["layers"]:
    if L["op"] != "conv":
        continue
    o = by[L["name"]]
    p = phase_counts(o["h"], o["w"], o["cin"], o["cout"], o["k"], o["s"], o["d"], o["pad"])
    p.update(name=L["name"], sec=L["sec"], k=o["k"], cin=o["cin"], cout=o["cout"])
    rows.append(p)

tot = {n: sum(r[n] for r in rows) for n in
       ("fill_bytes", "weight_bytes", "write_bytes", "iters", "mac")}
wgt = {k: sum(r["weight_bytes"] for r in rows if r["k"] == k) for k in (1, 3, 5)}
it = {k: sum(r["iters"] for r in rows if r["k"] == k) for k in (1, 3, 5)}

parts = {
    "填充(读 DDR)": tot["fill_bytes"] * A_F,
    "权重重装(读 DDR)": wgt[1] * A_T1 + wgt[3] * A_T3 + wgt[5] * A_T5,
    "计算(MAC/累加)": sum(it[k] * II[k] for k in II),
    "写回(写 DDR)": tot["write_bytes"] * A_W,
    "每层启动": CONST * len(rows),
}
T = sum(r["sec"] for r in rows) * FCLK
print("conv 实测 %.2f s（%d 层，FCLK=150MHz，C8 bit 9385dc82）\n"
      % (T / FCLK, len(rows)))
print("%-20s %10s %10s %8s" % ("相位", "工作量", "秒", "占比"))
wl = {"填充(读 DDR)": "%.1f MB" % (tot["fill_bytes"] / 1e6),
      "权重重装(读 DDR)": "%.1f MB" % (tot["weight_bytes"] / 1e6),
      "写回(写 DDR)": "%.1f MB" % (tot["write_bytes"] / 1e6),
      "计算(MAC/累加)": "%.0f M 迭代" % (tot["iters"] / 1e6),
      "每层启动": "%d 层" % len(rows)}
for k, v in sorted(parts.items(), key=lambda x: -x[1]):
    print("%-20s %10s %10.2f %7.1f%%" % (k, wl[k], v / FCLK, 100 * v / T))
print("%-20s %10s %10.2f %7.1f%%" % ("合计", "", sum(parts.values()) / FCLK,
                                     100 * sum(parts.values()) / T))
mem = sum(parts[k] for k in parts if "DDR" in k)
print("\nDDR 流量合计 %.0f MB（%.2f 倍输入填充量）→ %.1f s = %.0f%% 的 conv 时间"
      % ((tot["fill_bytes"] + tot["weight_bytes"] + tot["write_bytes"]) / 1e6,
         (tot["fill_bytes"] + tot["weight_bytes"] + tot["write_bytes"]) / tot["fill_bytes"],
         mem / FCLK, 100 * mem / T))
print("有效带宽：读 %.1f MB/s、写 %.1f MB/s（HP0 32bit@150MHz 峰值 600 MB/s）"
      % (FCLK / A_F / 1e6, FCLK / A_W / 1e6))
cc = sum(it[k] * II[k] for k in II) / FCLK
print("MAC 总量 %.2f GMAC → 计算相位 %.1f s = %.2f GMAC/s"
      % (tot["mac"] / 1e9, cc, tot["mac"] / cc / 1e9))
print("  分解：k=1 %d 层 %.2f GMAC / %.1f s（%.1f MAC/拍）；k=3 %d 层 %.2f GMAC / %.1f s（%.1f MAC/拍）"
      % (sum(1 for r in rows if r["k"] == 1), sum(r["mac"] for r in rows if r["k"] == 1) / 1e9,
         it[1] * II[1] / FCLK, 8.0,
         sum(1 for r in rows if r["k"] == 3), sum(r["mac"] for r in rows if r["k"] == 3) / 1e9,
         it[3] * II[3] / FCLK, 72.0 / II[3]))

# ---- 如果 DDR 访问变宽（每笔 1 字节 → 4/8 字节），能省多少 ----
print("\n---- 反事实：把每笔 AXI 访问从 1 字节改成 W 字节（每笔成本不变）----")
for W in (4, 8):
    mem2 = mem / W
    save = (mem - mem2) / FCLK
    print("  W=%d 字节：DDR %.1f s → %.1f s；conv %.1f s → %.1f s；整网 144.9 s → %.0f s"
          % (W, mem / FCLK, mem2 / FCLK, T / FCLK, (T - (mem - mem2)) / FCLK,
             144.89 - save))
print("  （W=4 是保守值：32bit 口上一笔 4 字节仍是 1 个 beat，事务形态与现在完全一样；"
      "W=8 需要 2 beat，是上限）")
