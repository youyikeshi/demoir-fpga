#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bin_test_staged.py —— `bin_conv_core` 上板的**分级测试**（6 阶段，失败即停）

仿 `board_test_staged.py` 的骨架，但对象换成二值卷积。原则：

  * 每个阶段只引入**一个新的风险**：先证明"能说话"，再证明"能访存"，最后才证明
    "算得对"。任一阶段失败 ⇒ 立即 sys.exit(非 0)，不往下做。
  * 所有会让 PL 访存的动作都 (a) 过 `bin_engine.BinGuard`（地址/尺寸/白名单）、
    (b) 在 `board_safe.Watchdog` 里跑、(c) 轮询 done 时喂狗。
  * 每个阶段都打印**够定位问题**的信息（读回值、错误码名字+排查建议、首个不符下标）。

阶段：
  0  overlay 加载 + IP/页/复位值身份检查（**不访存、不写寄存器**）
  1  配置页读写回环（写 14 个配置字段再读回，**不 start**）+ 保留页写忽略
  2  启动前校验（每次只错一个参数，确认 RTL 回对应 ERR_*，**不访存**）
  3  权重搬运（整份容器 → DDR，回读 sha256 + 逐层切片比对）
  4  单个最小层推理（index.json 里 workload 最小的一层，8x8）+ 激活打包独立复核
  5  golden 扫掠（5 个 xsim 模块级黄金向量；目录不在就退化成 4 个真实层）

用法（板子上，board_bundle 里）：
    sudo python3 bin_test_staged.py --bit design_1.bit --pack bin_weights_new --stage 6
    sudo python3 bin_test_staged.py --stage 2          # 只做到阶段 2（先验寄存器通路）
本机（无板、无 pynq，用寄存器级 RTL 模型跑同一套流程）：
    python3 bin_test_staged.py --fake
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from bin_ref_numpy import (                     # noqa: E402
    Blob, C8, LANES, GOLDEN_NAMES, out_hw, binarize_pack, act_bytes,
    conv_rtl, to_c8, load_golden, synth_layer_blob, check_goldens,
)
from bin_engine import (                        # noqa: E402
    BinConvEngine, BinLayer, BinRunError, CFG, CANARY_BYTES,
    decode_err, estimate_cycles,
)

STAGE_NAMES = {
    0: "overlay 加载 + 身份检查（不访存/不写）",
    1: "配置页读写回环（不启动）",
    2: "启动前校验：每次只错一个参数，确认 ERR_*",
    3: "权重搬运（整份容器 → DDR，回读校验）",
    4: "单个最小层推理 + 激活打包独立复核",
    5: "golden 扫掠（逐字节）",
}


def _apply_fclk(mhz):
    """★ 真的把 PL 时钟设成 mhz（--fclk 原本只是算拍数用的数字）。
    BD 里写 150 MHz，但设计可能只收敛到 125 MHz ⇒ 必须显式设，否则跑在错的频率上。"""
    if not mhz or mhz <= 0:
        return
    try:
        from pynq import Clocks
        cur = Clocks.fclk0_mhz
        if abs(cur - mhz) > 0.5:
            Clocks.fclk0_mhz = mhz
            import time as _t; _t.sleep(0.2)
            print("  [clk] fclk0 %g -> %g MHz（读回 %g）" % (cur, mhz, Clocks.fclk0_mhz), flush=True)
        else:
            print("  [clk] fclk0 = %g MHz（已是目标值）" % cur, flush=True)
    except Exception as e:
        print("  [clk] 设置失败（不影响运行）: %r" % (e,), flush=True)


def hdr(s):
    print("\n" + "=" * 74)
    print("  " + s)
    print("=" * 74, flush=True)


class StageFail(Exception):
    def __init__(self, code, msg):
        self.code = code
        super().__init__(msg)


def fail(stage, msg):
    print("\n  \u2717 阶段 %d 失败：%s" % (stage, msg), flush=True)
    print("  ⇒ 立即停止，不再进入下一阶段（分级测试的意义就在这里）", flush=True)
    raise StageFail(100 + stage, msg)


class NullWdt:
    armed = False
    seconds = 0

    def beat(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_wdt(seconds, enabled):
    """真板子用 board_safe.Watchdog；本机（--fake）/--no-wdt 用空实现。"""
    if not enabled:
        return NullWdt()
    try:
        from board_safe import Watchdog
    except Exception as ex:                      # noqa: BLE001
        print("[wdt] 拿不到 board_safe.Watchdog（%s）⇒ 本次无看门狗保护！" % ex)
        return NullWdt()
    return Watchdog(seconds)


def _first_exist(*cands):
    for c in cands:
        if c and os.path.exists(c):
            return c
    return cands[0]


def _default_pack():
    """权重容器目录。按顺序找，第一个存在的就是它（板上/本机同一份代码）：

      1. 脚本旁边          <ps>/bin_weights_new/
      2. 板上部署布局      <ps>/../deploy/bin_weights_new/   ← /home/xilinx/demoir/{ps,deploy}
      3. 本机源码布局      <ps>/../../deploy/bin_weights_new/ ← pynq_specialized_code/deploy/
    """
    return _first_exist(os.path.join(HERE, "bin_weights_new"),
                        os.path.join(HERE, os.pardir, "deploy", "bin_weights_new"),
                        os.path.join(HERE, os.pardir, os.pardir, "deploy",
                                     "bin_weights_new"))


def _default_bit():
    """比特流**不再硬编码** /home/xilinx/demoir_v2/design_1.bit（那是旧部署的旧 bit，
    误加载等于把 PL 换成过期设计）。顺序：$BIT > 脚本旁 design_1.bit > 上一级。
    都找不到就返回 None，由 main() 要求显式 --bit。
    """
    return _first_exist(os.environ.get("BIT"),
                        os.path.join(HERE, "design_1.bit"),
                        os.path.join(HERE, os.pardir, "design_1.bit"),
                        os.path.join(HERE, os.pardir, os.pardir, "design_1.bit")) or None


def _default_golden():
    return _first_exist(os.path.join(HERE, "golden"),
                        os.path.join(HERE, os.pardir, os.pardir, "pl",
                                     "rtl_hand", "golden"))


def sha256_of(mv):
    h = hashlib.sha256()
    h.update(np.asarray(mv).tobytes())
    return h.hexdigest()


def u8(buf):
    return np.asarray(buf).view(np.uint8).reshape(-1)


# ============================================================================
# 阶段 0：overlay + 身份
# ============================================================================
def stage0(ov, e, args):
    hdr("阶段 0：%s" % STAGE_NAMES[0])
    ipd = getattr(ov, "ip_dict", {})
    print("  IPLIST = %s" % (sorted(ipd) if ipd else "<FakeUnit 没有 ip_dict>"))
    if ipd and not any(k.startswith("bin_unit") for k in ipd):
        fail(0, "overlay 里没有 bin_unit*（现有：%s）" % sorted(ipd))
    s = e.summary()
    print("  IP          = %s" % s["ip"])
    print("  物理基址    = %#010x（窗口 %#x）" % (s["base_pa"], s["range"]))
    print("  配置页      = %#010x（= 基址 + 0x10000，A[16]=1）" % s["cfg_page"])
    if (s["base_pa"] >> 16) & 1:
        fail(0, "基址第 16 位是 1 ⇒ +0x10000 会翻到保留页")

    # ---- 身份检查：RTL 里**没有 ID/版本寄存器**，用复位值当指纹 ----
    # bin_unit_axi_wrap.v §2：复位把 r_alpha_shift 写死 9、r_slope_shift 写死 8；
    # A[16]=0 的保留页读回 0。两条一起看即可确认"这个 128 KB 窗口里确实是我们的
    # 那颗 bin_unit，且页解码正确"。这是本 IP 唯一能做的只读身份检查。
    ash = e._rd_reg("ALPHA_SHIFT")
    ssh = e._rd_reg("SLOPE_SHIFT")
    ap = e._rd_reg("AP_RET")
    ctl = e._rd_reg("AP_CTRL")
    resv = e._rd(0x18)                  # 保留页（A[16]=0）的 R_C
    print("  复位指纹：alpha_shift=%d slope_shift=%d  （期望 9 / 8）" % (ash, ssh))
    print("  AP_CTRL=%#010x  AP_RETURN=%#010x  DBG_FLAG=%#010x"
          % (ctl, ap, e._rd_reg("DBG_FLAG")))
    print("  保留页读 R_C = %#x（期望 0）" % resv)
    if (ash, ssh) != (9, 8):
        fail(0, "alpha_shift/slope_shift 复位值不是 9/8（读到 %d/%d）⇒ 这个窗口不是"
                " bin_unit（或比特流不是这份 RTL / 配置页偏移不对）" % (ash, ssh))
    if resv != 0:
        fail(0, "保留页读到 %#x（期望 0）⇒ A[16] 分页没生效，配置页偏移要复查" % resv)
    if ap & 0x1:
        print("  ⚠ AP_RETURN 已经是 done=1（上一跑留下的），config+start 时会清掉")
    print("  ✓ overlay / 页解码 / 复位指纹通过（本轮**没有**访存，也没写任何寄存器）",
          flush=True)


# ============================================================================
# 阶段 1：配置页读写回环
# ============================================================================
def stage1(e, L0, args):
    hdr("阶段 1：%s" % STAGE_NAMES[1])
    L = e.config(L0)
    print("  写配置：%r" % L)
    ok, bad = e.config_check(L)
    got = e.config_readback(L)
    print("  读回：" + "  ".join("%s=%d" % (k, v) for k, v in got.items()
                                if not k.startswith("B_")))
    if not ok:
        for b in bad:
            print("    ✗ " + b)
        fail(1, "配置页回读不一致（%d 项）⇒ AXI-Lite 写通路或寄存器偏移有问题"
             % len(bad))
    # 基址段单独验一次（0x80 段）；用完清零，别给后面阶段留坑
    e._wr_reg("B_IN", 0x2000_0000)
    if e._rd_reg("B_IN") != 0x2000_0000:
        fail(1, "B_IN(0x80) 写 %#x 读回 %#x" % (0x2000_0000, e._rd_reg("B_IN")))
    e._wr_reg("B_IN", 0)
    # 保留页写忽略（A[16]=0）：写进去应该读回 0
    e._wr(0x18, 0xDEAD_BEEF)
    if e._rd(0x18) != 0:
        fail(1, "保留页写进去了（读回 %#x）⇒ 页解码可疑" % e._rd(0x18))
    print("  ✓ 14 个配置字段 + B_IN + 保留页写忽略 全部符合预期"
          "（本轮**没有** start，PL 没有访存）", flush=True)


# ============================================================================
# 阶段 2：启动前校验
# ============================================================================
def stage2(e, L, sb_pa, in_pa, out_pa, args):
    hdr("阶段 2：%s" % STAGE_NAMES[2])
    print("  做法：先摆一组合法基址，然后**每次只错一个**参数，期望 done=1 且")
    print("        AP_RETURN[15:8] = 对应的 ERR_*。校验失败时 RTL 不启动、不访存。")
    print("  ⚠ 这些用例即便 RTL 没拦住也是**安全**的：基址都落在已登记的池里，")
    print("     输出缓冲按 ceil(cout/8)*Hout*Wout*8 + 4KB 金丝雀切好。")

    def good():
        e.set_base("IN", in_pa)
        e.set_base("WGT", sb_pa)
        e.set_base("ALPHA", sb_pa + 0x1000)
        e.set_base("BIAS", sb_pa + 0x2000)
        e.set_base("ZETA", sb_pa + 0x3000)
        e.set_base("GAMMA", sb_pa + 0x4000)
        e.set_base("SLOPE", sb_pa + 0x5000)
        e.set_base("OUT", out_pa)

    # (标签, 期望错误码, 布置函数)  —— 布置函数自己决定要不要写基址
    cases = [
        ("一个基址都不写（复位值 0）", 1, lambda: None),
        ("base_in = 合法地址 + 4（非 8 字节对齐）", 5,
         lambda: (good(), e._wr_reg("B_IN", in_pa + 4))),
        ("alpha_shift = 8（契约固定 9）", 3,
         lambda: (good(), e._wr_reg("ALPHA_SHIFT", 8))),
        ("cin = 52（非 8 的倍数，CHK_C8_ALL=1）", 4,
         lambda: (good(), e._wr_reg("C", 52))),
        ("base_in 高 32 位字 = 1（必须 0）", 2,
         lambda: (good(), e._wr(CFG + 0x84, 1))),
        ("cout = 200（> COUT_MAX=192）", 2,
         lambda: (good(), e._wr_reg("COUT", 200))),
    ]
    for tag, want, setup in cases:
        e.config(L)                       # 回到已知好配置（顺带清 done/err/hi_bad）
        e._wr(CFG + 0x84, 0)
        setup()
        t0 = time.time()
        try:
            e.start_and_check(timeout=2.0)
            got = 0
        except BinRunError as ex:
            got = ex.code
        dt = time.time() - t0
        _c, wname, wshort, _whint = decode_err(want)
        if got == want:
            print("   ✓ %-42s → %s(%d)  %.1f ms" % (tag, wname, want, dt * 1e3))
        else:
            _gc, gname, _gs, ghint = decode_err(got)
            print("   ✗ %-42s → 期望 %s(%d)，实得 %s(%d)"
                  % (tag, wname, want, gname, got))
            print("        排查建议：%s" % ghint)
            fail(2, "启动前校验与 RTL 不一致：%s" % tag)
        e._wr(CFG + 0x84, 0)
    print("  ✓ %d/%d 个错误码全部对上（错误码→中文说明的映射也逐条打印了）"
          % (len(cases), len(cases)), flush=True)
    e.config(L)


# ============================================================================
# 阶段 3：权重搬运
# ============================================================================
def stage3(e, blob_buf, blob_pa, args):
    hdr("阶段 3：%s" % STAGE_NAMES[3])
    path = os.path.join(e.pack_dir, "bin_weights.bin")
    size = os.path.getsize(path)
    t0 = time.time()
    buf, pa = e.load_weights(buf=blob_buf, offset=0, path=path)
    dt = time.time() - t0
    print("  搬运 %d B（%.2f MB）→ pa=%#x，%.2f ms（%.1f MB/s）"
          % (size, size / 1e6, pa, dt * 1e3, size / 1e6 / max(dt, 1e-9)))
    if pa != blob_pa:
        fail(3, "load_weights 返回的 pa=%#x != 预期 %#x" % (pa, blob_pa))
    # 缓存验证：同参数再调一次必须**不重搬**（返回同一个对象）
    t1 = time.time()
    buf2, pa2 = e.load_weights(buf=blob_buf, offset=0, path=path)
    dt2 = time.time() - t1
    if (buf2 is not buf) or pa2 != pa:
        fail(3, "第二次 load_weights 换了缓冲（缓存失效）")
    print("  缓存：第二次调用 %.4f ms（不重搬）%s"
          % (dt2 * 1e3, "✓" if dt2 < dt * 0.5 + 5e-3 else "⚠ 比首次还慢，检查缓存键"))

    mv = u8(buf)[:size]
    host = np.fromfile(path, np.uint8)
    got = sha256_of(mv)
    want = hashlib.sha256(host.tobytes()).hexdigest()
    idx_sha = json.load(open(os.path.join(e.pack_dir, "index.json")))["blob"].get("sha256")
    print("  板上 sha256 = %s" % got)
    print("  磁盘 sha256 = %s" % want)
    if idx_sha:
        print("  index.sha256= %s%s" % (idx_sha, "" if idx_sha == want
                                        else "  ⚠ 与磁盘不一致！"))
    if got != want:
        bad = np.nonzero(mv != host)[0][:8]
        fail(3, "DDR 里的容器与磁盘不一致（首个不符偏移 %s）" % bad.tolist())

    bl = Blob(e.pack_dir)
    for i in (0, len(bl.layers) // 2, len(bl.layers) - 1):
        Lw = bl.layer(i)
        a = mv[Lw["off_wgt"]:Lw["off_wgt"] + Lw["sz_wgt"]]
        b = host[Lw["off_wgt"]:Lw["off_wgt"] + Lw["sz_wgt"]]
        if not np.array_equal(a, b):
            fail(3, "第 %d 层（%s）wgt 区搬运不一致" % (i, Lw["name"]))
        print("   ✓ 第 %3d 层 %-44s wgt %5d B 一致"
              % (i, Lw["name"][-42:], len(a)))
    print("  ✓ 权重搬运可回读、索引切片正确（%.2f ms）" % (dt * 1e3), flush=True)
    return pa


# ============================================================================
# 阶段 4：单个最小层
# ============================================================================
def pick_min_layer(bl):
    """挑 workload 最小的一层（用 RTL 官方拍数公式，H=W=8 估）。"""
    best = None
    for L in bl.layers:
        if int(L["cin"]) % C8:
            continue
        try:
            cyc, _ = estimate_cycles(BinLayer(L, 8, 8))
        except Exception:                       # noqa: BLE001
            continue
        if best is None or cyc < best[0]:
            best = (cyc, L)
    return best[1], best[0]


def indep_pack_check(x_q, t_q, L, in_buf):
    """**独立**复核 PS 侧二值化+打包：np.packbits 重算，和 DDR 里的字节比。

    刻意不用 `bin_ref_numpy.binarize_pack`：否则"同一个函数两处都对"什么也证明不了。
    """
    cin, H, W, cb = L.cin, L.H, L.W, L.cb
    bits = ((x_q.astype(np.int64) + t_q.reshape(cin, 1, 1)) > 0).astype(np.uint8)
    padded = np.zeros((cb * LANES, H, W), np.uint8)
    padded[:cin] = bits
    b = np.packbits(padded.reshape(cb, LANES, H, W), axis=1, bitorder="little")
    b = b.transpose(0, 2, 3, 1)                       # (cb,H,W,8) 小端
    want = b.tobytes()
    got = u8(in_buf)[:len(want)].tobytes()
    return want == got, want, got


def stage4(e, bl, L, in_buf, in_pa, out_buf, out_pa, args, wdt):
    hdr("阶段 4：%s" % STAGE_NAMES[4])
    print("  层：%r" % L)
    cyc, est = estimate_cycles(L, args.fclk)
    print("  预计 %d 拍 ≈ %.3f ms @%.0f MHz（超时按 4× + 5 s）"
          % (cyc, est * 1e3, args.fclk))
    rng = np.random.default_rng(args.seed)
    x_q = rng.integers(-128, 128, (L.cin, L.H, L.W), dtype=np.int16).astype(np.int8)
    P = bl.params(L.d)
    t_q = P["t_q"]

    # ---- 1) PS 侧二值化 + 打包 ----
    data = act_bytes(binarize_pack(x_q, t_q))
    if len(data) > len(in_buf):
        fail(4, "IN 区太小：需要 %d B（cb=%d,H=%d,W=%d），只有 %d B"
             % (len(data), L.cb, L.H, L.W, len(in_buf)))
    u8(in_buf)[:len(data)] = np.frombuffer(data, np.uint8)
    in_buf.flush()
    ok, want, got = indep_pack_check(x_q, t_q, L, in_buf)
    if not ok:
        d = np.nonzero(np.frombuffer(want, np.uint8)
                       != np.frombuffer(got, np.uint8))[0][:8]
        fail(4, "激活打包与 np.packbits 独立实现不一致（首个不符 %s）⇒ PS 侧打包错"
             % d.tolist())
    print("  ✓ 激活打包：%d B 与 np.packbits 独立实现逐字节一致" % len(want))

    # ---- 2) 参考输出（conv_rtl 已用 5 个 xsim 向量逐字节验过）----
    A = binarize_pack(x_q, t_q)
    ref, _ = conv_rtl(L.cin, L.cout, L.k, L.s, L.dil, L.pad, L.shift, A, P)
    exp = np.frombuffer(to_c8(ref), np.int8)

    # ---- 3) 跑 ----
    e.clear_dbg()
    t0 = time.time()
    rs = e.run(in_buf, out_buf, L=L, wdt=wdt)
    wall = time.time() - t0
    out_buf.invalidate()
    g = out_buf.view(np.int8).reshape(-1)[:len(exp)]
    g = np.frombuffer(g.tobytes(), np.int8)
    nbad = int((g != exp).sum())
    print("  done t=%.3f ms（墙钟 %.3f ms，含轮询）  HoutxWout=%dx%d"
          % (rs.seconds * 1e3, wall * 1e3, rs.hout, rs.wout))
    print("  dbg = %s" % (rs.dbg,))
    if nbad:
        d = np.nonzero(g != exp)[0][:8].tolist()
        print("  首个不符下标 %s  期望 %s 实得 %s"
              % (d, exp[d].tolist(), g[d].tolist()))
        print("  诊断：输出非零 %d/%d（期望 %d/%d）；不一致 %d 字节"
              % (int((g != 0).sum()), g.size, int((exp != 0).sum()), exp.size, nbad))
        fail(4, "输出与参考不符 %d/%d 字节" % (nbad, exp.size))
    print("  ✓ %s 逐字节一致（%d B）" % (L.name, len(exp)), flush=True)
    return x_q


# ============================================================================
# 阶段 5：golden 扫掠
# ============================================================================
def build_cases(args, bl):
    """返回 [(tag, blob_bytes, layer_dict(含 H/W), act_bytes, expected_int8)]。"""
    cases = []
    if args.golden and all(os.path.isdir(os.path.join(args.golden, n))
                           for n in GOLDEN_NAMES):
        print("  用 xsim 模块级黄金向量（%s）。" % args.golden)
        print("  这些向量是 RTL 仿真逐字节 PASS 的 ⇒ “硬件 == golden”"
              "直接等价于“硬件 == RTL 语义”，不需要浮点参考。")
        print("  （先在本机把参考实现与 golden 对一遍，golden/路径错了要在这里拦住）")
        bad = check_goldens(args.golden, GOLDEN_NAMES, verbose=True)
        if bad:
            fail(5, "本机参考实现跑不过 golden（%s）⇒ 先修参考/路径，别怀疑板子" % bad)
        for nm in GOLDEN_NAMES:
            blob, Ld, act, exp, meta = load_golden(args.golden, nm)
            Ho, Wo = out_hw(meta["H"], meta["W"], meta["s"])
            Ld = dict(Ld, H=int(meta["H"]), W=int(meta["W"]), Hout=Ho, Wout=Wo)
            cases.append((nm, blob, Ld, act, exp))
    else:
        print("  ⚠ 没有 golden 目录（%s）⇒ 退化成 4 个**真实层**。" % args.golden)
        print("    golden 用本机参考 conv_rtl 现算；该实现已用 5 个 xsim 向量逐字节验过")
        print("    （`python3 bin_ref_numpy.py golden` 可复现），所以它不是「自说自话」。")
        picks, seen = [], set()

        def take(pred, label):
            for L in bl.layers:
                if len(picks) >= 4:
                    return
                if L["idx"] in seen or int(L["cin"]) % C8:
                    continue
                if pred(L):
                    seen.add(L["idx"])
                    picks.append((L, label))
                    return
        take(lambda L: L["k"] == 1 and L["cb"] >= 3, "k=1 且 cb>1")
        take(lambda L: L["k"] == 3 and L["s"] == 2, "k=3 s=2")
        take(lambda L: L["k"] == 3 and L["d"] >= 3, "k=3 d>=3")
        take(lambda L: L["cout"] >= 128, "cout>=128")
        rng = np.random.default_rng(args.seed + 1)
        for L, label in picks:
            H = W = 8
            LL = BinLayer(L, H, W)
            P = bl.params(L)
            x = rng.integers(-128, 128, (LL.cin, H, W), dtype=np.int16).astype(np.int8)
            A = binarize_pack(x, P["t_q"])
            ref, _ = conv_rtl(LL.cin, LL.cout, LL.k, LL.s, LL.dil, LL.pad,
                              LL.shift, A, P)
            blob, Ld = synth_layer_blob("real:" + L["name"], L, P["words"],
                                        P["alpha_q"], P["bias_q"], P["zeta_q"],
                                        P["gamma_q"], P["slope_q"])
            Ld = dict(Ld, H=H, W=W, Hout=LL.Hout, Wout=LL.Wout)
            cases.append(("%s [%s]" % (L["name"], label), blob, Ld, act_bytes(A),
                          np.frombuffer(to_c8(ref), np.int8)))
    return cases


def stage5(e, bl, pool, out_buf, out_pa, args, wdt):
    hdr("阶段 5：%s" % STAGE_NAMES[5])
    cases = build_cases(args, bl)
    n_ok = 0
    for tag, blob, Ld, act, exp in cases:
        L = BinLayer(Ld, Ld["H"], Ld["W"])
        need = ((L.cout + 7) // 8) * L.Hout * L.Wout * 8
        if need + CANARY_BYTES > len(out_buf):
            ob, opa = pool.region(need + CANARY_BYTES, name="gout")
            e.add_region(ob, "gout")
        else:
            ob = out_buf
        gb, gpa = pool.region(len(blob) + 64, name="gblob")
        ab, _ = pool.region(len(act) + 64, name="gact")
        e.add_region(gb, "gblob")
        e.add_region(ab, "gact")
        u8(gb)[:len(blob)] = np.frombuffer(blob, np.uint8)
        u8(ab)[:len(act)] = np.frombuffer(act, np.uint8)
        gb.flush()
        ab.flush()
        e.blob, e.blob_pa = gb, gpa
        cyc, est = estimate_cycles(L, args.fclk)
        rs = e.run(ab, ob, L=L, wdt=wdt, timeout=max(10.0, est * 4 + 5.0))
        ob.invalidate()
        g = ob.view(np.int8).reshape(-1)[:len(exp)]
        g = np.frombuffer(g.tobytes(), np.int8)
        nbad = int((g != exp).sum())
        # ★ 2026-10-31：把 DBG_RD 的 (需求读, 填线) 和 PL 拍数一起打印出来。
        #   上板反推 RLAT 只用这四个数：cycles / rd / fill / (cycles/fill)，
        #   不用再单独写一个脚本（stage5 本身已经是"一次 load 全跑"的入口）。
        #   cycles 由 rs.seconds（start→done 的 PL 时间，不含 PS 轮询）× fclk 得到。
        cyc_hw = rs.seconds * args.fclk * 1e6
        _rd = rs.dbg.get("rd_words", 0)
        _fl = rs.dbg.get("fill_win", 0)
        print("   %s %-34s cin=%3d cout=%3d k=%d s=%d cb=%d  %d/%d  %.3f ms  "
              "cyc=%d rd=%d fill=%d  cyc/term=%.4f fill/rd=%.3f cyc/fill=%.1f"
              % ("✓" if nbad == 0 else "✗", tag[-34:], L.cin, L.cout, L.k, L.s,
                 L.cb, len(exp) - nbad, len(exp), rs.seconds * 1e3,
                 int(cyc_hw + 0.5), _rd, _fl,
                 (cyc_hw / max(1, len(exp) * L.cb * L.k * L.k)),
                 (_fl / max(1, _rd)),
                 (cyc_hw / max(1, _fl))))
        if nbad:
            d = np.nonzero(g != exp)[0][:8].tolist()
            print("      首个不符下标 %s 期望 %s 实得 %s"
                  % (d, exp[d].tolist(), g[d].tolist()))
            fail(5, "%s 逐字节不符 %d/%d" % (tag, nbad, len(exp)))
        n_ok += 1
    print("  ✓ %d/%d 个用例逐字节一致" % (n_ok, len(cases)), flush=True)


# ============================================================================
# main
# ============================================================================
def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(description="bin_conv_core 上板分级测试")
    ap.add_argument("--stage", type=int, default=6, choices=range(0, 7),
                    help="跑到第几阶段（0..5；6=全跑，默认 6）")
    ap.add_argument("--bit", default=os.environ.get("BIT") or _default_bit(),
                    help="比特流路径；默认找脚本旁 design_1.bit，找不到必须显式给")
    ap.add_argument("--pack", default=os.environ.get("PACK", _default_pack()),
                    help="deploy/bin_weights_new（板上放在脚本旁边也行）")
    ap.add_argument("--golden", default=os.environ.get("GOLDEN", _default_golden()),
                    help="xsim 黄金向量目录（没有就退化成本机参考现算）")
    ap.add_argument("--unit", type=int, default=0, help="bin_unit_<id>")
    ap.add_argument("--pool-mb", type=float, default=8.0, help="单块 DDR 池大小")
    ap.add_argument("--fclk", type=float, default=150.0, help="仅用于超时估算/打印")
    ap.add_argument("--seed", type=int, default=20261030)
    ap.add_argument("--wdt", type=float, default=15.0)
    ap.add_argument("--no-wdt", action="store_true")
    ap.add_argument("--fake", action="store_true",
                    help="本机跑：用寄存器级 RTL 模型代替板子（不加载 overlay）")
    args = ap.parse_args(argv)

    if not args.fake:
        if not args.bit:
            sys.exit("  \u2717 没找到比特流：用 --bit <path> 或环境变量 BIT 指定。\n"
                     "    （本脚本不再默认 /home/xilinx/demoir_v2/design_1.bit —— 那是旧设计）")
        if not os.path.isfile(args.bit):
            sys.exit("  \u2717 比特流不存在：%s" % args.bit)
        print("  将加载比特流：%s" % os.path.abspath(args.bit))

    # ---- 准备：overlay + 单块池 ----
    hdr("准备：overlay 与内存池")
    if args.fake:
        from bin_engine import FakeUnit, FakeArena
        print("  --fake：FakeUnit（寄存器级 RTL 模型）+ FakeArena，不碰硬件")
        ov = FakeUnit(0x4006_0000)
        arena = FakeArena(int(args.pool_mb * (1 << 20)), base_pa=0x1800_0000, unit=ov)
    else:
        # ★ PYNQ 3.1 本镜像不自动注册 Device 子类 ⇒ 不导入就报 No Devices Found
        try:
            import pynq.pl_server.embedded_device as _pynq_emb  # noqa: F401
        except Exception:
            pass
        from pynq import Overlay
        from bin_engine import BinArena
        print("  Overlay(%s)" % args.bit)
        ov = Overlay(args.bit)
        _apply_fclk(args.fclk)
        arena = BinArena(int(args.pool_mb * (1 << 20)))
    print("  池：%d B @ pa=%#x（单块分配 + 子区划分：多处 allocate 会让地址不可控）"
          % (arena.size, arena.pa))
    e = BinConvEngine(ov, unit_id=args.unit, pack_dir=args.pack)
    e.add_region(arena.buf, "pool")
    bl = Blob(args.pack)

    # ---- 阶段 0 ----
    stage0(ov, e, args)
    if args.stage < 1:
        return 0

    # ---- 阶段 1（需要一层已知形状：用 workload 最小的那层，8x8）----
    minL, cyc = pick_min_layer(bl)
    L0 = BinLayer(minL, 8, 8)
    print("\n  [预备] workload 最小的一层：%r（%d 拍 @8x8）" % (L0, cyc))
    stage1(e, L0, args)
    if args.stage < 2:
        return 0

    # ---- 阶段 2 ----
    # 校验阶段用一个 64 KB 的"哑容器"区当 base_wgt/参数：只验错误码通路，
    # 真容器的搬运留到阶段 3（否则阶段 2 就得先搬 1.07 MB 才能验寄存器）。
    sb, sb_pa = arena.region(64 * 1024, name="scratch-blob")
    in_buf, in_pa = arena.region(L0.act_bytes + 4096, name="IN")
    out_buf, out_pa = arena.region(L0.out_bytes, name="OUT", keep_canary=True)
    for b, tag in ((sb, "scratch-blob"), (in_buf, "IN"), (out_buf, "OUT")):
        e.add_region(b, tag)
    e.blob, e.blob_pa = sb, sb_pa           # 只给"非 0、对齐"的基址用

    with make_wdt(args.wdt, not args.no_wdt and not args.fake) as wdt:
        stage2(e, L0, sb_pa, in_pa, out_pa, args)
        if args.stage < 3:
            return 0

        # ---- 阶段 3 ----
        blob_buf, blob_pa = arena.region(
            os.path.getsize(os.path.join(args.pack, "bin_weights.bin")) + 4096,
            name="blob")
        e.add_region(blob_buf, "blob")
        stage3(e, blob_buf, blob_pa, args)
        if args.stage < 4:
            return 0

        # ---- 阶段 4 ----
        e.blob, e.blob_pa = blob_buf, blob_pa
        stage4(e, bl, L0, in_buf, in_pa, out_buf, out_pa, args, wdt)
        if args.stage < 5:
            return 0

        # ---- 阶段 5 ----
        stage5(e, bl, arena, out_buf, out_pa, args, wdt)

    print("\n" + "=" * 74)
    print("  全部通过 ✓（跑到阶段 %d）" % (min(args.stage, 5),))
    print("=" * 74)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StageFail as ex:
        sys.exit(ex.code)
    except BinRunError as ex:
        print("\n!! 引擎报错（未捕获）：%s" % ex)
        sys.exit(90)
    except KeyboardInterrupt:
        print("\n中断")
        sys.exit(130)
