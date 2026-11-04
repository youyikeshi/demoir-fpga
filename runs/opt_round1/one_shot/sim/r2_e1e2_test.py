#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""r2_e1e2_test.py —— bin_e1e2 的 R2（阈值 K=6 小数位）单元验证

构造一个**最小但完整**的 E1E2 命令（c=64/h=1/w=8/group=0 ⇒ 8 lane × 8 通道 ×
8 像素 = 512 个 (x, thr) 组合），用 `tb_bin_e1e2.v` 跑 RTL，再与"按 RTL_SPEC §R2
独立写的参考模型"逐位比对：

    bit = ( sext8(x) · 2^6 + thr_k > 0 )        严格 > 0

判据：
  ① 逐位相等（512 bit 全中）；
  ② 必须覆盖 RTL_SPEC §R2 点名的两类边界：
       · x = 0（ReLU 精确零，占 20%）配 **正**阈值 ⇒ 1、配 **负**阈值 ⇒ 0
       · 亚 LSB 阈值（|thr_k| < 64）配 |x| ≥ 1 ⇒ 老实现（不加 6 位）会判反
  ③ `--mutant` 模式：把 RTL 里的 `<<< THR_K` 改成 `<<< 0`（= 老行为），
     同一份数据必须**变红**（否则这条判据没有背书）。

用法：
  python3 r2_e1e2_test.py --rtl <bin_e1e2.v> --tb <tb_bin_e1e2.v> --work /tmp/...
"""
import argparse, os, struct, subprocess, sys, random

W_PIX = 8
C = 64
LANES = 8
CH = 8

DESC_BASE = 0x1000
THR_BASE  = 0x2000
SRC_BASE  = 0x3000
OUT_BASE  = 0x4000
MEM_BYTES = 0x4200


def build_case():
    """返回 (mem bytearray, thr[64], src[64][W_PIX] int8 列表)"""
    mem = bytearray(MEM_BYTES)
    thr = [0] * C
    src = [[0] * W_PIX for _ in range(C)]

    # ---- ① 定向边界向量（放在 lane0 = 通道 0..7）----
    #   (x 值, thr_k) → 期望 bit = (x*64 + thr_k > 0)
    directed = [
        (0,    37),    # ReLU 零 + 正阈值 ⇒ 1（老实现：0+37>0 也是 1，但容器里 thr 会是 0）
        (0,   -37),    # ReLU 零 + 负阈值 ⇒ 0
        (0,    1),     # 正最小阈值 ⇒ 1
        (0,     0),    # 阈值恰 0 ⇒ 0（严格 >0）
        (1,   -64),    # 恰好 −0.5 LSB ⇒ 0（tie 判 0，严格 >0）
        (-1,   64),    # 恰好 +0.5 LSB ⇒ 0（tie）
        (1,   -63),    # 略大于 +0.5 LSB ⇒ 1
        (-1,   63),    # 略小于 −0.5 LSB ⇒ 0
    ]
    for j, (x, t) in enumerate(directed):
        src[0][0] = src[0][0]          # 占位（下面按通道写）
        thr[j] = t
        src[j][0] = x

    # ---- ② 亚 LSB 阈值 × 小 |x|（老实现必错的形状）----
    rng = random.Random(20261104)
    for c in range(8, C):
        thr[c] = rng.choice([-1, 1, -19, 19, -32, 32, 63, -63])
        base_x = rng.choice([-2, -1, 0, 1, 2])
        for p in range(W_PIX):
            src[c][p] = max(-128, min(127, base_x + rng.choice([-1, 0, 1])))

    # ---- ③ 全量程边界（|thr_k| 最大 + x 取 ±127/−128）----
    for c in (8, 9, 10, 11, 12, 13):
        thr[c] = 5249 if (c % 2 == 0) else -5249
        for p in range(W_PIX):
            src[c][p] = [127, -128, 0, 1, -1, 126, -127, 64][p]

    # ---- 其余通道：随机全量程（覆盖一般情形）----
    for c in range(14, C):
        thr[c] = rng.randint(-5249, 5249)
        for p in range(W_PIX):
            src[c][p] = rng.randint(-128, 127)

    # ---- 落盘到内存 ----
    def put32(addr, v):
        struct.pack_into("<I", mem, addr, v & 0xFFFFFFFF)

    for k in range(LANES):
        t_addr = THR_BASE + 16 * k
        s_addr = SRC_BASE + W_PIX * 8 * k
        # desc word k = {thr_base, src_base}
        put32(DESC_BASE + 8 * k, s_addr)
        put32(DESC_BASE + 8 * k + 4, t_addr)
        for j in range(CH):
            struct.pack_into("<h", mem, t_addr + 2 * j, thr[k * CH + j])
        for p in range(W_PIX):
            for j in range(CH):
                mem[s_addr + p * 8 + j] = src[k * CH + j][p] & 0xFF
    return mem, thr, src


def expected_bytes(thr, src):
    """独立参考模型：bit = (x·2^6 + thr_k > 0)"""
    out = bytearray(W_PIX * 8)
    for p in range(W_PIX):
        for i in range(C):
            x = src[i][p]                      # int8
            v = x * 64 + thr[i]
            if v > 0:
                out[p * 8 + (i // 8)] |= (1 << (i % 8))
    return out, None


def write_memfile(path, mem):
    with open(path, "w") as f:
        for i in range(0, len(mem), 8):
            f.write("%016x\n" % struct.unpack_from("<Q", mem, i)[0])


def write_cmdfile(path):
    with open(path, "w") as f:
        f.write("%x %x %x %x %x %x\n" % (DESC_BASE, OUT_BASE, C, 1, W_PIX, 0))


def read_outfile(path):
    words = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                words.append(int(ln, 16))
    return b"".join(struct.pack("<Q", w) for w in words)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtl", required=True)
    ap.add_argument("--tb", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--tag", default="r2")
    ap.add_argument("--expect-red", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.work, exist_ok=True)
    mem, thr, src = build_case()
    exp, _ = expected_bytes(thr, src)

    memf = os.path.join(a.work, "mem.hex")
    cmdf = os.path.join(a.work, "cmd.txt")
    outf = os.path.join(a.work, "out.hex")
    write_memfile(memf, mem)
    write_cmdfile(cmdf)
    if os.path.exists(outf):
        os.remove(outf)

    env = dict(os.environ)
    env["PATH"] = "/data/xilinx/2025.2/2025.2.1/Vivado/bin:" + env.get("PATH", "")
    sh = lambda cmd: subprocess.run(cmd, cwd=a.work, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    r = sh(["xvlog", "--sv", "--relax", a.rtl, a.tb])
    if r.returncode:
        print(r.stdout[-3000:]); sys.exit(2)
    r = sh(["xelab", "-debug", "off", "--relax", "-s", "snap_e1e2", "tb_bin_e1e2"])
    if r.returncode:
        print(r.stdout[-3000:]); sys.exit(2)
    r = sh(["xsim", "snap_e1e2", "-R", "--nolog",
            "-testplusarg", "MEMFILE=mem.hex",
            "-testplusarg", "MEMWORDS=%d" % (MEM_BYTES // 8),
            "-testplusarg", "CMDFILE=cmd.txt",
            "-testplusarg", "NCMD=1",
            "-testplusarg", "OUTBASE=4000",
            "-testplusarg", "OUTWORDS=%d" % ((W_PIX * 8) // 8),
            "-testplusarg", "OUTFILE=out.hex",
            "-testplusarg", "TAG=" + a.tag])
    log = r.stdout
    open(os.path.join(a.work, "sim.log"), "w").write(log)
    sumline = [l for l in log.splitlines() if l.startswith("T36 SUM")]
    if not os.path.exists(outf):
        print(log[-3000:]); print("❌ 没有 out.hex"); sys.exit(3)
    got = read_outfile(outf)[:W_PIX * 8]

    nbad = 0
    first = []
    for p in range(W_PIX):
        for i in range(C):
            gb = (got[p * 8 + (i // 8)] >> (i % 8)) & 1 if (p * 8 + (i // 8)) < len(got) else -1
            eb = (exp[p * 8 + (i // 8)] >> (i % 8)) & 1
            if gb != eb:
                nbad += 1
                if len(first) < 10:
                    first.append("p=%d ch=%d x=%d thr=%d got=%d exp=%d"
                                 % (p, i, src[i][p], thr[i], gb, eb))
    print("\n".join(sumline))
    print("  [%s] 512 bit 比对：一致 %d / 512，不一致 %d" % (a.tag, 512 - nbad, nbad))
    for s in first:
        print("   ✗", s)
    red = nbad > 0
    if a.expect_red:
        print("  判定：期望变红 ⇒ %s" % ("✔ RED（判据有背书）" if red else "✗ 没变红（判据无效）"))
        sys.exit(0 if red else 1)
    print("  判定：期望全绿 ⇒ %s" % ("✔ GREEN" if not red else "✗ FAIL"))
    sys.exit(0 if not red else 1)


if __name__ == "__main__":
    main()
