#!/usr/bin/env python3
"""
L1 等价性证明：把 bin_conv_core.v 里每一处参数化改动，按其"原文硬编码 8"版与
"VLANES=8 展开"版逐项数值比对；再顺带验证 VLANES=16 时槽位不别名。

只比对 RTL 里真正参与运算的量（pw 字号、字节偏移、读使能边界、写 sel 掩码），
不解析 Verilog，不做综合。
"""
FAIL = []


def chk(name, a, b):
    ok = a == b
    if not ok:
        FAIL.append(name)
    print(f"  [{'OK ' if ok else 'BAD'}] {name}")


# ---------------- 参数（RTL localparam 的同一个公式） ----------------
def derive(VLANES):
    NVA = VLANES // 4
    NVB = VLANES // 2
    PRM_W = 2 * NVA + 3 * NVB
    PRMW = (PRM_W).bit_length()  # $clog2(PRM_W+1)
    return dict(NVA=NVA, NVB=NVB, PRM_W=PRM_W, PRMW=PRMW,
                PB_A=0, PB_B=NVA, PB_Z=NVA + NVB,
                PB_G=NVA + 2 * NVB, PB_S=NVA + 3 * NVB)


# ---------------- 原文（硬编码 8） ----------------
def orig_lane_slots(k):
    return {
        "alpha": (k // 4, 16 * (k % 4)),
        "slope": (14 + k // 4, 16 * (k % 4)),
        "bias":  (2 + k // 2, 32 * (k % 2)),
        "zeta":  (6 + k // 2, 32 * (k % 2)),
        "gamma": (10 + k // 2, 32 * (k % 2)),
    }


def new_lane_slots(k, P):
    return {
        "alpha": (P["PB_A"] + k // 4, 16 * (k % 4)),
        "slope": (P["PB_S"] + k // 4, 16 * (k % 4)),
        "bias":  (P["PB_B"] + k // 2, 32 * (k % 2)),
        "zeta":  (P["PB_Z"] + k // 2, 32 * (k % 2)),
        "gamma": (P["PB_G"] + k // 2, 32 * (k % 2)),
    }


def orig_prm_addr(i, o0, B=(0x1000, 0x2000, 0x3000, 0x4000, 0x5000)):
    alpha_b, bias_b, zeta_b, gamma_b, slope_b = B
    if i in (0, 1):
        return alpha_b + (o0 << 1) + (i << 3)
    if i in (2, 3, 4, 5):
        return bias_b + (o0 << 2) + ((i - 2) << 3)
    if i in (6, 7, 8, 9):
        return zeta_b + (o0 << 2) + ((i - 6) << 3)
    if i in (10, 11, 12, 13):
        return gamma_b + (o0 << 2) + ((i - 10) << 3)
    if i in (14, 15):
        return slope_b + (o0 << 1) + ((i - 14) << 3)
    return alpha_b + 0                      # default


def new_prm_addr(i, o0, P, B=(0x1000, 0x2000, 0x3000, 0x4000, 0x5000)):
    alpha_b, bias_b, zeta_b, gamma_b, slope_b = B
    if i < P["PB_B"]:
        return alpha_b + (o0 << 1) + (i << 3)
    elif i < P["PB_Z"]:
        return bias_b + (o0 << 2) + ((i - P["PB_B"]) << 3)
    elif i < P["PB_G"]:
        return zeta_b + (o0 << 2) + ((i - P["PB_Z"]) << 3)
    elif i < P["PB_S"]:
        return gamma_b + (o0 << 2) + ((i - P["PB_G"]) << 3)
    elif i < P["PB_S"] + P["NVA"]:
        return slope_b + (o0 << 1) + ((i - P["PB_S"]) << 3)
    return alpha_b + 0                      # 越界 dummy 拍 = 原文 default


print("=" * 68)
print("0) VLANS=8 时派生常量是否退化成原文的字面量")
P8 = derive(8)
chk("LVW  = $clog2(8)          == 3", (8).bit_length() - 1, 3)
chk("NVA  == 2", P8["NVA"], 2)
chk("NVB  == 4", P8["NVB"], 4)
chk("PRM_W== 16 (原文 16 字)", P8["PRM_W"], 16)
chk("PRMW == 5  (原文 prm_i [4:0])", P8["PRMW"], 5)
chk("基址 0/2/6/10/14", (P8["PB_A"], P8["PB_B"], P8["PB_Z"], P8["PB_G"], P8["PB_S"]),
    (0, 2, 6, 10, 14))

print("1) lane k → (pw 字号, 字内 bit 偏移)：VLANES=8 新旧逐 lane 比对")
for k in range(8):
    chk(f"lane {k}", orig_lane_slots(k), new_lane_slots(k, P8))

print("2) prm_addr(i, o0)：VLANES=8 新旧逐 i / 逐 o0 比对（含越界 dummy 拍 16）")
for o0 in (0, 8, 16, 192, 4096, 0xFFFF):
    same = all(orig_prm_addr(i, o0) == new_prm_addr(i, o0, P8) for i in range(17))
    chk(f"o0={o0:<6} i=0..16", same, True)

print("3) 写字节使能 sel（VLANES=8）：(1<<nvalid[2:0])-1  ≡  (1<<nvalid[LVW-1:0])-1")
for nv in range(8):                        # else 分支只可能 nv<8（nv>=8 走全 1 保护）
    a = ((1 << (nv & 0x7)) - 1) & 0xFF
    b = ((1 << (nv & ((1 << 3) - 1))) - 1) & 0xFF
    chk(f"nvalid={nv}", a, b)

print("4) VLANES=16：PRM 帧 32 字，各参数槽位互不别名")
P16 = derive(16)
chk("NVA/NVB/PRM_W/PRMW", (P16["NVA"], P16["NVB"], P16["PRM_W"], P16["PRMW"]),
    (4, 8, 32, 6))
used = {}
for k in range(16):
    for typ, (w, off) in new_lane_slots(k, P16).items():
        used.setdefault(typ, []).append(w)
for typ in ("alpha", "slope", "bias", "zeta", "gamma"):
    ws = used[typ]
    chk(f"{typ}: 16 lane 覆盖 {len(set(ws))} 字，全部 < PRM_W",
        (len(set(ws)), max(ws) < P16["PRM_W"]), (P16["NVA"] if typ in ("alpha", "slope") else P16["NVB"], True))
chk("五段合计字数 == PRM_W",
    2 * P16["NVA"] + 3 * P16["NVB"], P16["PRM_W"])

print("5) VLANES=16：sel 掩码对 nvalid=0..15 单调且不溢出 16 位")
ok = all((((1 << (nv & 0xF)) - 1) & 0xFFFF) == (1 << nv) - 1 for nv in range(16))
chk("nvalid=0..15", ok, True)

print("=" * 68)
print("RESULT:", "ALL PASS" if not FAIL else f"FAILED {FAIL}")
raise SystemExit(1 if FAIL else 0)
