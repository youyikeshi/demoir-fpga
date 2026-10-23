#!/usr/bin/env python3
"""用板上 247 层实测时间回归四个相位的单位成本。

数据：/home/xilinx/demoir_new/layers.json（C8 纯 layout bit, FCLK=150MHz,
      --profile-layers 逐层计时，conv 合计 128.445 s）
工作量：phase_model.phase_counts()（逐字照抄 conv2d_engine.cpp 的循环）

模型  cyc = a*fill_bytes + b*weight_bytes + c*write_bytes + d*iters + e*n_wload + f0
      a,b,c : 每字节拍数（含 AXI 传输）
      d     : ic 流水每次迭代的拍数（= II_k）
      e     : 每次权重装载的固定开销（AR 握手 + 往返的非带宽部分）
      f0    : 每层固定开销（启动/收尾）
"""
import json
import numpy as np

from phase_model import phase_counts

ROOT = "/media/felix/Data/coding/demoir-fpga"
FCLK = 150e6
MAN = json.load(open(f"{ROOT}/pynq_specialized_code/ps/manifest_640.json"))
PROF = json.load(open(f"{ROOT}/runs/prof20261023/layers.json"))

byshape = {}
for o in MAN["ops"]:
    if o.get("op") == "conv" and o.get("needed"):
        byshape[o["name"]] = o

rows = []
for L in PROF["layers"]:
    if L["op"] != "conv":
        continue
    o = byshape[L["name"]]
    p = phase_counts(o["h"], o["w"], o["cin"], o["cout"], o["k"], o["s"], o["d"], o["pad"])
    p.update(name=L["name"], sec=L["sec"], cyc=L["sec"] * FCLK,
             k=o["k"], s=o["s"], cin=o["cin"], cout=o["cout"], h=o["h"], w=o["w"])
    rows.append(p)

names = ["fill_bytes", "weight_bytes", "write_bytes", "iters", "n_wload"]
A = np.array([[r[n] for n in names] + [1.0] for r in rows])
y = np.array([r["cyc"] for r in rows])

# 尺度归一到 ~1e6，改善条件数
scale = np.array([1e6, 1e6, 1e6, 1e6, 1e3, 1.0])
A2 = A / scale
coef, *_ = np.linalg.lstsq(A2, y, rcond=None)
unit = coef / scale
pred = A @ unit
res = y - pred
print("=== 全 247 层回归（单位：拍）===")
for n, u in zip(names + ["const"], unit):
    print("  %-13s %12.4f %s" % (n, u, "拍/字节" if n.endswith("bytes") else
                                  ("拍/迭代" if n == "iters" else "")))
print("  R^2 = %.4f   RMS=%.1f 拍 (%.2f s)   max|res|=%.1f 拍 (%.2f s)"
      % (1 - res.var() / y.var(), res.std(), res.std() / FCLK,
         np.abs(res).max(), np.abs(res).max() / FCLK))
print("  总实测 %.1f s / 总预测 %.1f s" % (y.sum() / FCLK, pred.sum() / FCLK))

# 条件数
s = np.linalg.svd(A2, compute_uv=False)
print("  条件数 = %.1f（<1e3 说明可分）" % (s[0] / s[-1]))

print("\n=== 按 k 分组单独拟合（a,c,d 共用，b/e 分组）===")
for kk in (1, 3, 5):
    m = np.array([r["k"] == kk for r in rows])
    if m.sum() == 0:
        continue
    print("  k=%d: %3d 层  实测 %6.1f s  预测(全局系数) %6.1f s  平均残差 %+.1f%%"
          % (kk, m.sum(), y[m].sum() / FCLK, pred[m].sum() / FCLK,
             100 * (pred[m].sum() / y[m].sum() - 1)))

print("\n=== 最不像模型的 12 层 ===")
idx = np.argsort(-np.abs(res))[:12]
print("  %-52s %6s %6s %5s %5s %7s %7s" % ("name", "HxW", "cin/out", "k", "s", "实测s", "残差s"))
for i in idx:
    r = rows[i]
    print("  %-52s %4dx%-3d %4d/%-3d %5d %5d %7.3f %+7.3f"
          % (r["name"][-52:], r["h"], r["w"], r["cin"], r["cout"], r["k"], r["s"],
             r["sec"], res[i] / FCLK))

# ---- 用拟合系数做全网络拆解 ----
print("\n=== 全网络 conv 128.4 s 的相位拆解（按拟合系数）===")
tot = {n: np.array([r[n] for r in rows]).sum() for n in names}
parts = {n: tot[n] * u / FCLK for n, u in zip(names, unit)}
const = unit[-1] * len(rows) / FCLK
for n in names:
    print("  %-13s %12.3f M   -> %7.2f s  (%5.1f%%)"
          % (n, tot[n] / 1e6, parts[n], 100 * parts[n] / (y.sum() / FCLK)))
print("  %-13s %12s   -> %7.2f s" % ("per-layer 固定", "", const))
print("  %-13s %12s   -> %7.2f s" % ("合计", "", sum(parts.values()) + const))
print("  %-13s %12s   -> %7.2f s" % ("实测", "", y.sum() / FCLK))

np.save("/tmp/fit_unit.npy", unit)
json.dump({"unit": {n: float(u) for n, u in zip(names + ["const"], unit)},
           "totals": {n: float(tot[n]) for n in names}},
          open(f"{ROOT}/runs/prof20261023/fit_unit.json", "w"), indent=1)
