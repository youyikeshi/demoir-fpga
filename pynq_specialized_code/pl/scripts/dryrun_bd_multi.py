#!/usr/bin/env python3
# ============================================================================
#  dryrun_bd_multi.py —— 不跑 Vivado 的 BD 改造「干跑」校验
#
#  干什么：
#    读 proj_multi 现有的 design_1.bd（纯 JSON），把 bd_multi_bin.tcl 要做的事
#    在内存里推演一遍，然后把「必然会被 Vivado 判死」的东西提前判死：
#      · 地址冲突 / 重叠 / 没按 0x20000 对齐 / 基址 A[16]=1（驱动会 ValueErr）
#      · 基址掉出 Zynq M_AXI_GP0 的 0x4000_0000..0x7FFF_FFFF aperture
#      · axi_ctrl 空闲 master 口够不够
#      · AXI-HP 口够不够（conv_uni 占 HP1 时 dedicated 最多 3 个 bin_unit）
#      · 共享拓扑下 ID 位宽账（N×4bit 展宽后必须 ≤ HP 的 6 bit）
#      · bin_unit_0 已占的端口/地址段是否与新规划打架
#
#  用法：python3 dryrun_bd_multi.py            # 跑 NBIN=2,3,4 三种
#        python3 dryrun_bd_multi.py 3          # 只跑某一种
#
#  这是 bd_multi_bin.tcl 的**辅证**，不能替代它自己的 .hwh 自检。
# ============================================================================
from __future__ import annotations
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PL = os.path.dirname(HERE)
PROJ = os.path.join(PL, "vivado", "proj_multi")
BD = os.path.join(PROJ, "proj_multi.srcs", "sources_1", "bd", "design_1", "design_1.bd")

CTRL_BASE0 = 0x4006_0000
CTRL_STRIDE = 0x20000
CTRL_RANGE = 0x20000
GP0_LO, GP0_HI = 0x4000_0000, 0x7FFF_FFFF

# bin_unit 的 m_axi 是 AXI4 / ID_WIDTH=4 / DATA=64；Zynq HP 是 AXI3 / ID=6 / DATA=64
BIN_MID_W = 4
HP_ID_W = 6
HP_DATA_W = 64
HP_FREQ_MHZ = 150.0


def load_bd():
    with open(BD) as f:
        return json.load(f)["design"]


def free_ctrl_masters(d):
    """axi_ctrl 的 Mxx_AXI 里哪些没被接口网占用。"""
    used = set()
    for net, v in d.get("interface_nets", {}).items():
        for p in v.get("interface_ports", []):
            if p.startswith("axi_ctrl/"):
                used.add(p.split("/", 1)[1])
    mi = d["components"]["axi_ctrl"].get("parameters", {}).get("NUM_MI", {})
    nmi = int(mi.get("value", 0))
    allm = ["M%02d_AXI" % i for i in range(nmi)]
    return [m for m in allm if m not in used], sorted(used), nmi


def hp_state(d):
    """ps7 已使能 / 被占用的 HP 口。"""
    ports = [k for k in d["components"]["ps7"].get("interface_ports", {})
             if k.startswith("S_AXI_HP")]
    used = set()
    for net, v in d.get("interface_nets", {}).items():
        for p in v.get("interface_ports", []):
            if p.startswith("ps7/S_AXI_HP"):
                used.add(p.split("/", 1)[1])
    return sorted(ports), sorted(used)


def addr_segs(d):
    """{instance: (offset, range)} 从 addressing 段路径反推。"""
    out = {}
    for master, mv in d.get("addressing", {}).items():
        for sp, sv in mv.get("address_spaces", {}).items():
            for name, seg in sv.get("segments", {}).items():
                ab = seg["address_block"]          # /bin_unit_0/s_axi/reg0
                inst = ab.strip("/").split("/")[0]
                out[inst] = (int(seg["offset"], 16), int(seg["range"].replace("K", "*1024")
                               .replace("M", "*1024*1024"), 0)
                             if False else _parse_range(seg["range"]))
    return out


def _parse_range(s):
    s = s.strip().upper()
    if s.endswith("K"):
        return int(s[:-1]) * 1024
    if s.endswith("M"):
        return int(s[:-1]) * 1024 * 1024
    if s.endswith("G"):
        return int(s[:-1]) * 1024 * 1024 * 1024
    return int(s, 0)


def check(nbin, topo, drop_conv, d):
    errs, warns, infos = [], [], []
    comps = d["components"]

    # ---- 1. 控制侧 ----
    free, used, nmi = free_ctrl_masters(d)
    infos.append("axi_ctrl NUM_MI=%d，已占 %s，空闲 %s" % (nmi, used, free))
    need = max(0, nbin - 1)          # bin_unit_0 已占 M03
    if len(free) < need:
        errs.append("axi_ctrl 空闲 master 只有 %d 个，需要 %d 个" % (len(free), need))

    # ---- 2. 数据侧 ----
    enabled, occupied = hp_state(d)
    infos.append("已使能 HP=%s，已占用=%s（HP2/HP3 需由脚本 PCW_USE_S_AXI_HP2/3 打开）"
                 % (enabled, occupied))
    # 模拟 DROP_CONV_UNI 之后的状态：conv_uni 让出 HP1
    if drop_conv and "conv_uni_0" in comps:
        occupied = [p for p in occupied if p != "S_AXI_HP1"]
        enabled = [p for p in enabled if p != "S_AXI_HP1"] + ["S_AXI_HP1(释放)"]
    n_new_hp = max(0, nbin - 1)
    free_hp = [p for p in ("S_AXI_HP0", "S_AXI_HP1", "S_AXI_HP2", "S_AXI_HP3")
               if p not in occupied]
    if topo == "dedicated":
        if len(free_hp) < n_new_hp:
            errs.append("dedicated 需要 %d 个空闲 HP 口，实际只有 %s" % (n_new_hp, free_hp))
        else:
            infos.append("dedicated: 新引擎用 %s（其余保持不动）" % free_hp[:n_new_hp])
    else:
        sid = BIN_MID_W + (nbin - 1).bit_length()   # 4 + ceil(log2 N)
        infos.append("shared: %d 个 SI × ID %d bit → MI ID %d bit（HP 上限 %d）"
                     % (nbin, BIN_MID_W, sid, HP_ID_W))
        if sid > HP_ID_W:
            errs.append("shared 拓扑 ID 展宽到 %d bit > HP 的 %d bit ⇒ DRC 报错"
                        % (sid, HP_ID_W))

    # ---- 3. 地址 ----
    segs = addr_segs(d)
    infos.append("现有寄存器段: %s" % {k: hex(v[0]) for k, v in sorted(segs.items())})
    plan = {}
    for i in range(nbin):
        plan["bin_unit_%d" % i] = CTRL_BASE0 + i * CTRL_STRIDE
    if not drop_conv and "conv_uni_0" in segs:
        plan["conv_uni_0"] = segs["conv_uni_0"][0]

    # 3a 对齐 / 步长
    if CTRL_STRIDE < CTRL_RANGE:
        errs.append("步长 0x%X < 窗口 0x%X ⇒ 必定重叠" % (CTRL_STRIDE, CTRL_RANGE))
    for name, base in plan.items():
        if base % CTRL_STRIDE:
            errs.append("%s 基址 0x%08X 没按 0x%X 对齐" % (name, base, CTRL_STRIDE))
        # 3b 驱动硬断言：A[16] 必须是 0，否则 +0x10000 翻到保留页
        if (base >> 16) & 1:
            errs.append("%s 基址 0x%08X 的 A[16]=1 ⇒ bin_engine.py 会 ValueErr"
                        % (name, base))
        if not (GP0_LO <= base and base + CTRL_RANGE - 1 <= GP0_HI):
            errs.append("%s 窗口 0x%08X..0x%08X 掉出 GP0 aperture 0x%08X..0x%08X"
                        % (name, base, base + CTRL_RANGE - 1, GP0_LO, GP0_HI))
    # 3c 重叠
    items = sorted(plan.items(), key=lambda kv: kv[1])
    for (n1, b1), (n2, b2) in zip(items, items[1:]):
        if b1 + CTRL_RANGE > b2:
            errs.append("%s [0x%08X..0x%08X] 与 %s [0x%08X..] 重叠"
                        % (n1, b1, b1 + CTRL_RANGE - 1, n2, b2))

    # ---- 4. 报告 ----
    print("=" * 78)
    print(" NBIN=%d  TOPO=%s  DROP_CONV_UNI=%d" % (nbin, topo, int(drop_conv)))
    print("=" * 78)
    for i in infos:
        print("  [info ] " + i)
    print("  地址表（Vivado 会把 offset 原样写进 .hwh 的 BASEVALUE）：")
    for name in sorted(plan):
        b = plan[name]
        print("    %-12s %s .. %s" % (name, hex(b).replace("0x", "0x"),
                                      "0x%08X" % (b + CTRL_RANGE - 1)))
    for w in warns:
        print("  [warn ] " + w)
    for e in errs:
        print("  [ERROR] " + e)
    print("  → %s" % ("FAIL" if errs else "PASS"))
    return not errs


def main():
    if not os.path.exists(BD):
        sys.exit("找不到 %s（先 cp -r proj_engine proj_multi）" % BD)
    d = load_bd()
    print("BD: %s" % BD)
    print("device: %s   validated=%s" % (d["design_info"]["device"],
                                         d["design_info"]["validated"]))
    cases = []
    if len(sys.argv) > 1:
        cases = [(int(sys.argv[1]), "dedicated", int(sys.argv[1]) == 4)]
    else:
        cases = [(2, "dedicated", 0), (3, "dedicated", 0),
                 (4, "shared", 0), (4, "dedicated", 1)]
    ok = True
    for nbin, topo, drop in cases:
        ok &= check(nbin, topo, drop, d)
        print()
    print("总结: %s" % ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
