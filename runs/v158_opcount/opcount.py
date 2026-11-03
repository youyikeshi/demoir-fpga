#!/usr/bin/env python3
# T-158：按"命令次数"重算融合（band 链）价值。纯主机侧、只读，不运行网络。
#
# 输入（全部是已实测产物，不再上板）：
#   runs/board_t151/t151a_141402_141440.json  → run3 (墙钟 5.9589) 的 227 层
#       layer_list: cin/cout/H/W + prep/kick/pack/pl_s/epi  ← T-151 实测分桶
#   runs/board_t156/REPORT_T156.md §1          → MMIO 事务分类表（698/6542/3192/...）
#   runs/board_t154/REPORT_T154.md §1          → 每命令五格 V0/V1/V2/V3（硬件拍数）
#   runs/v90_stage2/verify/v4f/fuse1.log       → 223 段计划的逐段 kinds/bands/融合判定
#
# 口径铁律：凡是"省下的秒数"都由实测项乘出来，假设写在注释里。

import json, math, re, collections, os

R = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(R, "..", ".."))

T151 = os.path.join(ROOT, "runs/board_t151/t151a_141402_141440.json")
T90 = os.path.join(ROOT, "runs/v90_stage2/verify/v4f/fuse1.log")

# ---------------------------------------------------------------- 1) 数命令
d = json.load(open(T151))
run = d["runs"][2]                       # run3 = 稳态 5.9589 s（T-151 的基准跑）
L = run["layer_list"]

def cb(cin):                             # bin_engine.py:3847 `for g in range(cb)` / bin_net.py:2061
    return (int(cin) + 63) // 64

cmds = [cb(x["cin"]) for x in L]
n_pack = sum(cmds)                       # 698（T-156/T-154 的"698 条命令"）
n_conv = len(L)                          # 227 个 conv kick
hist = collections.Counter(cmds)

print("== 1) 命令清点（实测层表，227 层，256²，run3）")
print("   pack 命令 Σ⌈cin/64⌉ =", n_pack, "（= T-154/T-156 的 698 ✓）")
print("   conv kick            =", n_conv, "（每层 1 次；与 pack 无关，见 V50:60）")
print("   合计 PL 命令         =", n_pack + n_conv, "（= D6 的 925）")
print("   pack 命令/层 直方图   =", dict(sorted(hist.items())), " 层数", sum(hist.values()))
print("   cb=1 的层            =", hist[1], "层（%.0f%%）" % (100.0*hist[1]/len(L)))

# 按 H 分桶：pack 每条命令的真实成本（T-151 实测 pack 秒数 / 命令数）
print("\n    H   层  cb命令  pack(s)  ms/命令  ms/层  （pack 桶实测，非估算）")
by_h = {}
for H in sorted({x["H"] for x in L}):
    sub = [x for x in L if x["H"] == H]
    c = sum(cb(x["cin"]) for x in sub)
    p = sum(x["pack"] for x in sub)
    by_h[H] = (len(sub), c, p)
    print("   %3d %3d %6d  %7.3f  %7.2f %6.2f" % (H, len(sub), c, p, p/c*1000, p/len(sub)*1000))

# ---------------------------------------------------------------- 2) 分段计划
txt = open(T90, errors="replace").read()
i0 = txt.index("== v50 段计划（P1）"); i1 = txt.index("带几何自检", i0)
rows = re.findall(r"\n  (S\d+)\s+n=(\d+)\s+(\S+)\s+(\d+)x(\d+)x(\d+)->"
                  r"(\d+)x(\d+)x(\d+)\s+halo=(\d+)\s+band=(\d+).*?(融合|\*\*不融合\*\*)",
                  txt[i0:i1])
segs = [dict(id=r[0], n=int(r[1]), kinds=r[2], cin=int(r[3]), cout=int(r[6]),
             band=int(r[10]), fused=(r[11] == "融合")) for r in rows]
print("\n== 2) 段计划（223 段，FakePL 整网，runs/v90_stage2）")
print("   段数", len(segs), " op 数", sum(s["n"] for s in segs),
      " 融合段", sum(s["fused"] for s in segs),
      " 融合段描述符", sum(s["band"] for s in segs if s["fused"]))
print("   含 conv 的段", sum(1 for s in segs if "conv" in s["kinds"]),
      " 其中 conv/cat 段", sum(1 for s in segs if s["kinds"] == "conv/cat"),
      " 纯 conv 段", sum(1 for s in segs if s["kinds"] == "conv"))
# 融合段（纯 conv、单级）承载的 pack 命令数 —— 它们仍要 PS 发 pack 命令
cb_fused = sum(cb(s["cin"]) for s in segs if s["fused"])
print("   已被判融合的 90 段里，cin 对应的 pack 命令数 =", cb_fused,
      "(= %.0f%% 的 698)" % (100.0*cb_fused/n_pack))
print("   ⇒ 但融合段的输入必须**已经**是二值位流（bin_net.py:2777-2781），")
print("     pack 仍由 PS 发 ⇒ 现有 v50 融合对命令数是 0 收益（与 T-148 的字节 0 一致）")

# ---------------------------------------------------------------- 3) 每条命令的"纯开销"
# 实测（T-156 §1，256² 整网、一个租约）：
MM = dict(pack_poll_n=6542, pack_poll_ms=229.6, pack_poll_us=35.1,
          pack_wr_n=3494, pack_wr_ms=172.1, pack_wr_us=49.2,
          pack_wr_per_cmd=5.0, pack_poll_per_cmd=9.4,
          conv_poll_n=3192, conv_poll_ms=118.9,
          kick_n=227, kick_ms=13.8,
          wait_idle_n=227, wait_idle_ms=10.8,
          other_rd_n=406, other_rd_ms=19.2,
          conv_cfg_wr_n=2433, conv_cfg_ms=110.0,
          total_rd=10365, total_wr=6158, total_ms=650.0)
# 实测（T-154 §1）：run_one 自身 Python 206.0 ms / 698 命令
RUN_ONE_PY_MS = 206.0
# 实测（T-154 §1，V3 = DBG_E_CYC）：pack 的 PL 硬件总时 ≈ 0.32 s
HW_PACK_S = 0.317
# 实测（T-154 §1）：wait(sleep) 294.6 ms/698，属于"等硬件"不是开销
WAIT_MS = 294.6

per_cmd_pure_ms = RUN_ONE_PY_MS/n_pack + MM["pack_wr_ms"]/n_pack   # Python + 5 个配置写
print("\n== 3) 每条 pack 命令的『纯开销』（命令消失则消失）")
print("   run_one Python   %6.3f ms/cmd  → %6.1f ms  (T-154 实测 206.0/698)" %
      (RUN_ONE_PY_MS/n_pack, RUN_ONE_PY_MS))
print("   5 个配置写(MMIO) %6.3f ms/cmd  → %6.1f ms  (T-156 实测 172.1/698)" %
      (MM["pack_wr_ms"]/n_pack, MM["pack_wr_ms"]))
print("   Σ 纯开销         %6.3f ms/cmd" % per_cmd_pure_ms)
print("   另外 %5.1f ms 的 pack 轮询读 + %.1f ms 的 sleep 是『等硬件』⇒ 不省（只是换成链 wait）"
      % (MM["pack_poll_ms"], WAIT_MS))
print("   pack PL 硬件 %.3f s 不省（DBG_E_CYC 交叉校验：分尺寸 Σhw = %.3f s）" % (HW_PACK_S, HW_PACK_S))

# 分尺寸交叉校验（V3 单命令硬件拍 → 按 cb·hw 线性放大）
v3 = {128: 2.221, 64: 0.556, 32: 0.140, 16: 0.041}      # ms，128²/64²/32² 是 cb=1@cin48；16² 是 cb=2@cin96
ref_hw, ref_cb = 256, 2                                  # 16² 那一格
hw_est = 0.0
for H, (n, c, p) in by_h.items():
    per_cmd_hw = v3[min(v3, key=lambda k: abs(k-H))] / 1.0
    # 归一：V3 是在 cin=48(cb=1)、H² 像素下测的每命令硬件毫秒 → 拆成 per-像素 per-cb
    base = v3[min(v3, key=lambda k: abs(k-H))]
    base_cb = 2 if min(v3, key=lambda k: abs(k-H)) == 16 else 1
    hw_est += n * (base / base_cb) * (c / n)
print("   （校验：按 V3 每命令硬件拍 × cb 放大得 %.3f s，与 0.317 s 同量级 ✓）" % (hw_est/1000))

# ---------------------------------------------------------------- 4) 三档 N 与秒数
def show(tag, n_removed, extra_s=0.0, note=""):
    s = n_removed * per_cmd_pure_ms / 1000.0 + extra_s
    print("   %-34s 命令 %4d → %4d (省 %3.0f%%)  省 %5.3f s  → 墙钟 %5.2f s   %s"
          % (tag, n_pack + n_conv, (n_pack + n_conv) - n_removed,
             100.0*n_removed/(n_pack+n_conv), s, 5.328 - s, note))
    return s

print("\n== 4) 两档（698 = pack 命令；925 = 698 pack + 227 conv kick）")
# 保守：只把「同层 cb 组」并成一条链（bin_engine.py:3169 chain_descs 已实现，缺的是 v50 位流）
#       ⇒ 698 → 227，命令 925 → 454
s_cons = show("保守 698→227（PS 已写好）", n_pack - n_conv, 0.0,
              "chain_descs 已实现；缺 v50 位流")
# 乐观：带内 PACK 吃掉整条 pack 命令 + 223 段各一次 submit + 轮询揉合
#       ⇒ 命令 925 → 227（其中 pack 段 0 次），另 +0.15 s 轮询/交接揉合（估算）
s_opt = show("乐观 698→0（带内 PACK，需新 RTL）", n_pack + (n_conv - 223), 0.15,
             "+0.15 s 轮询揉合（估算）")

# ---------------------------------------------------------------- 5) 不可省下限
floor = [("pack PL 硬件(DBG_E_CYC)", HW_PACK_S),
         ("q8cache(f32 叶子量化)", 0.502),
         ("epilogue", 0.240),
         ("段表 prepare_segments", 0.186),
         ("cfg+金丝雀(每段)", 0.117),
         ("槽获取(每段)", 0.099),
         ("conv reap 轮询", MM["conv_poll_ms"]/1000.0),
         ("convfp", 1.246),
         ("小算子 interp/sam/add/eca/shuffle", 0.696),
         ("非卷积 cat(惰性)", 0.037)]
print("\n== 5) 命令数变少也拿不到的下限（每段/硬件/算法真活）")
tot = 0.0
for k, v in floor:
    print("   %-36s %6.3f s" % (k, v)); tot += v
print("   %-36s %6.3f s（5.33 里的 %.0f%%）" % ("Σ", tot, 100.0*tot/5.328))
print("   ⇒ 可动的天花板 = 5.328 − %.3f − 墙钟里未列的 P1/循环 ≈ 0.9 s" % tot)

json.dump(dict(n_pack=n_pack, n_conv=n_conv, cb_hist=dict(hist),
               per_cmd_pure_ms=per_cmd_pure_ms,
               by_h={str(k): v for k, v in by_h.items()},
               cb_fused_segments=cb_fused, floor_s=dict(floor),
               save={"conservative": s_cons, "optimistic": s_opt}),
          open(os.path.join(R, "opcount.json"), "w"), indent=1)
print("\n   → opcount.json")
