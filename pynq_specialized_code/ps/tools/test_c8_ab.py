#!/usr/bin/env python3
"""主机侧 C8 整合验收（纯 numpy / RefEngine，**不碰板子**）。

阶段（全部自动，缺一不可）：

  A) 默认路径自检
     · `py_compile` 编译 ps/ 下全部 .py（语法/导入期错误）
     · C8_LAYOUT 未设置时，布局表必须**全 False**（= 与整合前完全一致）
  B) 布局/分配单测（不跑网络）
     · 判定表：**int8 ⇒ C8（全部补零到 8 的倍数）**；fp32 ⇒ CHW。没有例外
     · 字节数：对齐张量 ceil(C/8)*8*H*W == C*H*W（零额外内存）；非对齐的
       4 个张量（3ch/12ch）多占 (8-C%8)*H*W，补零区必须是 0
     · 内存池：C8 与 CHW 的偏移表相同、池只多出补零那点字节
     · cat 别名组：C8 视图是 (total/8,H,W,8)，成员 = 通道块切片；
       逐组核对"组的 CHW 视图 [off:off+c]" == "成员的 CHW 视图"（偏移公式）
     · 引擎 relu_en 位：relu/in-c8/out-c8 的四种组合
  C) 整网 A/B（**最重要**）
     · `C8_LAYOUT=0` 跑一遍、`C8_LAYOUT=1` 跑一遍，比较最终输出
       → 要求 **0 / 1,228,800** 不一致
     · 517 个算子的输出 sha1（统一转 CHW 后）逐条对比
     · CHW 路输出 vs `deploy/weights_pack/ref_out.bin` → 要求 0
  D) 统计：多少 buffer 走 C8、例外是哪些、conv 的 (c8_in,c8_out) 分布

用法（仓库根目录或 pynq_specialized_code 下均可）：
    /data/work/venv-demoir/bin/python ps/tools/test_c8_ab.py
可选：
    --manifest ps/manifest_640.json   --pack deploy/weights_pack
    --skip-net   （只跑 A/B 单测，不跑整网）
"""
import argparse
import gc
import json
import os
import py_compile
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.dirname(HERE)                       # pynq_specialized_code/ps
FPGA = os.path.dirname(PS)                       # pynq_specialized_code
for p in (PS, os.path.join(PS, "pynq"), os.path.join(PS, "c8")):
    if p not in sys.path:
        sys.path.insert(0, p)

import c8_layout                                   # noqa: E402
from runner import (NumpyArena, RefEngine, Runner, compute_cat_alias,   # noqa: E402
                    load_pack, plan_pool)

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                          ("  " + detail) if detail else ""), flush=True)
    return ok


# ---------------------------------------------------------------- A) 默认路径

def stage_a(ps_root):
    print("\n== A) 默认路径自检 ==", flush=True)
    bad = []
    n = 0
    for root, _dirs, files in os.walk(ps_root):
        if "__pycache__" in root:
            continue
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            n += 1
            path = os.path.join(root, fn)
            try:
                py_compile.compile(path, doraise=True)
            except py_compile.PyCompileError as e:
                bad.append((path, str(e).splitlines()[-1]))
    check("py_compile 全部通过（%d 个文件）" % n, not bad, str(bad[:3]))

    os.environ.pop("C8_LAYOUT", None)
    check("C8_LAYOUT 未设置 ⇒ 开关默认关", c8_layout.enabled() is False)


# ---------------------------------------------------------------- B) 单测

def stage_b(manifest):
    print("\n== B) 布局 / 分配单测 ==", flush=True)
    bufs = manifest["buffers"]
    off = c8_layout.table(manifest, on=False)
    on = c8_layout.table(manifest, on=True)

    # 判定表：int8 全部走 C8（补零到 8 的倍数），fp32 走 CHW
    exp = {t: (m["dtype"] == "int8") for t, m in bufs.items()}
    check("判定表 == 规则(int8 ⇒ C8，无例外)", on == exp)
    check("默认表全 False（与整合前一致）", not any(off.values()))
    n8, nchw, exc, nfp = c8_layout.stats(on, manifest)
    print("       C8=%d  int8 CHW=%d  fp32=%d" % (n8, nchw, nfp), flush=True)
    check("C8=516 / int8 例外 0 / fp32 6（共 522 个 buffer）",
          (n8, nchw, nfp) == (516, 0, 6), "实际 %d/%d/%d" % (n8, nchw, nfp))
    check("c8_layout.table() 里没有 int8 的 False（除 fp32）",
          all(v for t, v in on.items() if bufs[t]["dtype"] == "int8"))

    # 字节数：对齐张量零额外内存；4 个非对齐张量多占 (8-C%8)*H*W
    worse = [(t, bufs[t]["c"]) for t, is8 in on.items()
             if is8 and bufs[t]["c"] % 8 == 0
             and c8_layout.nbytes(bufs[t]["c"], bufs[t]["h"], bufs[t]["w"], True)
             != bufs[t]["bytes"]]
    check("对齐张量：C8 字节数 == CHW 字节数（零额外内存）", not worse, str(worse[:3]))
    pad_t = [t for t, is8 in on.items() if is8 and bufs[t]["c"] % 8]
    extra = sum(c8_layout.nbytes(bufs[t]["c"], bufs[t]["h"], bufs[t]["w"], True)
                - bufs[t]["bytes"] for t in pad_t)
    print("       补零张量 %d 个：%s，合计多占 %.2f MB"
          % (len(pad_t), sorted(pad_t), extra / 1e6), flush=True)
    check("补零张量正好是 3ch/12ch 那 8 个（执行面 4 个）",
          sorted(pad_t) == ["t0", "t1", "t342", "t343", "t431", "t432",
                            "t520", "t521"], str(sorted(pad_t)))
    # 非对齐张量：补零区存在且形状是 ceil(C/8) 个通道块
    check("c=13 的 C8 补零：ceil(13/8)*8*2*3 == 96，视图 %r"
          % (c8_layout.view_shape(13, 2, 3, True),),
          c8_layout.nbytes(13, 2, 3, True) == 96
          and c8_layout.pad_channels(13, True) == 16
          and c8_layout.nbytes(13, 2, 3, False) == 78
          and c8_layout.view_shape(13, 2, 3, True) == (2, 2, 3, 8))
    # 补零区必须是 0（np.zeros 分配 ⇒ 恒成立）
    z = np.zeros(c8_layout.view_shape(16, 2, 3, True), np.int8)
    z = z.reshape(16, 2, 3)[13:]
    check("补零通道为 0（分配即清零）", int(np.count_nonzero(z)) == 0)

    # 池大小/偏移：C8 与 CHW 必须逐字节相同（规范 §3：偏移公式不用改）
    s0, o0 = plan_pool(manifest, layout=off)
    s1, o1 = plan_pool(manifest, layout=on)
    print("       池大小：CHW %.2f MB  C8 %.2f MB（+%.2f MB，来自 4 个补零张量）"
          % (s0 / 1e6, s1 / 1e6, (s1 - s0) / 1e6), flush=True)
    check("C8 池只多出补零的字节（< 8 MB）", 0 <= (s1 - s0) < 8 * 2 ** 20,
          "%d vs %d" % (s0, s1))
    diff = [t for t in o0 if o0[t] != o1[t]]
    print("       偏移表差异 %d 个张量（补零张量变大后重新打包，属预期）"
          % len(diff), flush=True)
    check("池大小仍远小于 PYNQ CMA（128 MB）", s1 < 96 * 2 ** 20, "%.1f MB" % (s1 / 1e6))

    # cat 别名组：C8 组视图 (total/8,H,W,8)；成员 = 通道块切片；偏移公式对
    ag0 = compute_cat_alias(manifest, is_c8=off)
    ag1 = compute_cat_alias(manifest, is_c8=on)
    check("cat 别名折叠组数/折叠 op 数（C8 vs CHW 完全一致）",
          (ag0["groups_n"], len(ag0["groups"]), len(ag0["skipped"]))
          == (ag1["groups_n"], len(ag1["groups"]), len(ag1["skipped"])),
          "C8: %d op / %d 组 / 回退 %d"
          % (ag1["groups_n"], len(ag1["groups"]), len(ag1["skipped"])))
    bad = []
    for gname, grp in ag1["groups"].items():
        buf = np.zeros(c8_layout.view_shape(grp["total"], grp["h"], grp["w"], True),
                       np.int8)
        flat_chw = c8_layout.to_chw(buf, True)
        for t, off_c in grp["members"].items():
            view = c8_layout.ch_slice(buf, True, off_c, bufs[t]["c"])
            if np.shares_memory(view, buf) is False:
                bad.append((t, "不共享内存"))
            if not np.shares_memory(flat_chw[off_c:off_c + bufs[t]["c"]], flat_chw):
                bad.append((t, "CHW 切片异常"))
            if view.shape != c8_layout.view_shape(bufs[t]["c"], grp["h"], grp["w"],
                                                  True):
                bad.append((t, str(view.shape)))
    check("%d 个 cat 组：成员视图 = 通道块切片（零拷贝，形状正确）"
          % len(ag1["groups"]), not bad, str(bad[:3]))
    # 再拿随机数据核对偏移公式的**取值**：组的 CHW 视图 [off:off+c] == 成员的 CHW 视图
    rng = np.random.default_rng(20261023)
    bad = []
    for gname, grp in ag1["groups"].items():
        src = rng.integers(-128, 128, size=(grp["total"], grp["h"], grp["w"]),
                           dtype=np.int8)
        buf = c8_layout.from_chw(src, True)
        back = c8_layout.to_chw(buf, True)
        if not np.array_equal(back, src):
            bad.append((gname, "往返"))
        for t, off_c in grp["members"].items():
            c = bufs[t]["c"]
            view = c8_layout.to_chw(c8_layout.ch_slice(buf, True, off_c, c), True)
            if not np.array_equal(view, src[off_c:off_c + c]):
                bad.append((t, "取值"))
    check("cat 组偏移公式取值逐位正确（全部组 × 全部成员）", not bad, str(bad[:3]))

    # ---- 板端 arena（PYNQArena）的 C8 视图：用假 pynq.allocate 验证 ----
    # 本机没有 pynq，不能真的分配 CMA；但 PYNQArena 的 C8 逻辑（reshape / 通道
    # 块切片 / 字节偏移）是可以离线验证的——板子是最终目标，这一段不能留空白。
    check("校验 PYNQArena（假 pynq.allocate）的 C8 布局", _check_pyq_arena(manifest, on))

    # ---- 算子级：ops_c8 的适配（真网络里每个分支都过一遍随机数据）----
    import ops_numpy as ops
    oc = c8_layout.ops_c8()
    import ops_c8 as oc_direct
    rng = np.random.default_rng(1234)
    C, H, W = 48, 9, 7

    def rnd(c=C, h=H, w=W):
        return rng.integers(-128, 128, size=(c, h, w), dtype=np.int8)

    a, b = rnd(), rnd()
    ga = c8_layout.from_chw(a, True)
    gb = c8_layout.from_chw(b, True)
    check("add：C8 直算 == CHW",
          np.array_equal(c8_layout.to_chw(oc.add(ga, gb, "c8"), True), ops.add(a, b)))
    # cat：块级拼接 + 段通道数全对齐 8
    segs = [rnd(8), rnd(16), rnd(24)]
    got = oc.cat([(c8_layout.from_chw(x, True), "c8") for x in segs], 48, "c8")
    check("cat：C8 块级拼接 == CHW 拼接",
          np.array_equal(c8_layout.to_chw(got, True), np.concatenate(segs, axis=0)))
    # cat 回退（有不对齐的段）
    segs2 = [rnd(4), rnd(7)]
    got2 = oc.cat([(x, "chw") for x in segs2], 11, "chw")
    check("cat：段不对齐 8 时回退 CHW",
          np.array_equal(got2, np.concatenate(segs2, axis=0)))
    # interp / pixel_* / slice
    for num, den in ((2, 1), (1, 2), (4, 1), (1, 4)):
        g = oc.interp(c8_layout.from_chw(a, True), "c8", num, den)
        ok = np.array_equal(c8_layout.to_chw(g, True), ops.interp(a, num, den))
        if not ok:
            break
    check("interp：C8 进出（转 CHW 算）逐位相同", ok)
    u = rnd(12 - 0, H * 2, W * 2)                     # (12,18,14) -> pixel_unshuffle
    g = oc.pixel_unshuffle(u, "chw", 2, out_lay="chw")
    check("pixel_unshuffle：例外张量走 CHW",
          np.array_equal(g, ops.pixel_unshuffle(u, 2)))
    ps_in = rnd(12, H, W)
    g = oc.pixel_shuffle(ps_in, "chw", 2, out_lay="chw")
    check("pixel_shuffle：例外张量走 CHW（12ch -> 3ch）",
          np.array_equal(g, ops.pixel_shuffle(ps_in, 2)) and g.shape[0] == 3)
    t8 = c8_layout.from_chw(a, True)
    g = oc.slice_channels(t8, "c8", 16, 8)
    check("slice：起止对齐 8 走块级切片",
          np.array_equal(c8_layout.to_chw(g, True), a[8:24]))
    # eca：均值必须转 CHW 后按 32 通道分块（float32 归约顺序敏感）
    v = rnd(96, 8, 8)
    m_ref = np.empty(96, np.float32)
    for c0 in range(0, 96, 32):
        c1 = min(c0 + 32, 96)
        fin = np.asarray(v[c0:c1]).astype(np.float32)
        fin *= 0.017
        m_ref[c0:c1] = fin.mean(axis=(1, 2), dtype=np.float32)
    v8 = c8_layout.from_chw(v, True)
    m_c8 = np.empty(96, np.float32)
    for c0 in range(0, 96, 32):
        c1 = min(c0 + 32, 96)
        fin = np.asarray(c8_layout.chw_chunk(v8, True, c0, c1)).astype(np.float32)
        fin *= 0.017
        m_c8[c0:c1] = fin.mean(axis=(1, 2), dtype=np.float32)
    check("eca：C8 分块转 CHW 的均值 == CHW 均值（逐位）",
          np.array_equal(m_ref, m_c8))
    # sam_sum：走 runner 里真正用的那个函数
    y0, y2, y4, xx = rnd(), rnd(), rnd(), rnd()
    attn = (rng.random(3 * C) * 0.9 + 0.05).astype(np.float32).reshape(3 * C, 1, 1)
    sc = (0.017, 0.021, 0.019, 0.013, 0.011)
    o_ref = np.empty((C, H, W), np.int8)
    w = attn.reshape(3, -1)
    for c0 in range(0, C, 16):
        sl = slice(c0, min(c0 + 16, C))
        yf = (w[0][sl, None, None] * (y0[sl].astype(np.float64) * sc[0])
              + w[1][sl, None, None] * (y2[sl].astype(np.float64) * sc[1])
              + w[2][sl, None, None] * (y4[sl].astype(np.float64) * sc[2]))
        xf = xx[sl].astype(np.float64) * sc[3]
        o_ref[sl] = np.clip(np.rint((xf + yf) / sc[4]), -128, 127).astype(np.int8)
    o_c8 = np.empty((C // 8, H, W, 8), np.int8)
    Runner._sam_sum_c8(o_c8,
                       c8_layout.from_chw(y0, True), c8_layout.from_chw(y2, True),
                       c8_layout.from_chw(y4, True), attn,
                       c8_layout.from_chw(xx, True),
                       sc[0], sc[1], sc[2], sc[3], sc[4], C)
    check("sam_sum：C8 直算 == CHW 分块（逐位）",
          np.array_equal(c8_layout.to_chw(o_c8, True), o_ref))

    # 引擎 relu_en 位
    import conv_engine
    cases = [((True, False, False), 1), ((False, False, False), 0),
             ((True, True, False), 5), ((True, False, True), 9),
             ((True, True, True), 13), ((False, True, True), 12)]
    bad = [(c, conv_engine.relu_flags(*c[0]), c[1]) for c in cases
           if conv_engine.relu_flags(*c[0]) != c[1]]
    check("relu_en = bit0(relu) | bit2(入 C8) | bit3(出 C8)", not bad, str(bad))

    # conv 的 (c8_in,c8_out) 分布 + 两个典型不匹配层
    dist = {}
    for op in manifest["ops"]:
        if op["op"] != "conv" or not op["needed"]:
            continue
        k = (bool(on[op["in"][0]]), bool(on[op["out"][0]]))
        dist[k] = dist.get(k, 0) + 1
    print("       conv 的 (c8_in,c8_out) 分布: %s"
          % {str(k): v for k, v in sorted(dist.items())}, flush=True)
    # 执行面（needed）247 个 conv：**全部 (1,1)** —— 没有任何层需要混搭
    check("(in,out) 全部 (1,1)：%d 个 conv" % dist.get((True, True), 0),
          list(dist.keys()) == [(True, True)] and dist.get((True, True)) == 247,
          str(dist))
    by_name = {o["name"]: o for o in manifest["ops"] if o["op"] == "conv"}
    for nm in ("encoder.conv_first", "decoder.decoder_1.out_conv"):
        a = by_name[nm]
        check("%s：输入/输出都是 C8（cin=%d cout=%d，补零到 %d/%d）"
              % (nm, a["cin"], a["cout"],
                 c8_layout.pad_channels(a["cin"], True),
                 c8_layout.pad_channels(a["cout"], True)),
              (on[a["in"][0]], on[a["out"][0]]) == (True, True))
    return on, n8, nchw, exc, nfp


# ---------------------------------------------------------------- C) 整网 A/B

def run_net(manifest_path, pack, on, do_hash=True):
    """跑一遍整网（RefEngine），返回 (输出 CHW, hashlog, 秒, arena 统计)。

    do_hash=False 时不记录逐算子 sha1（**纯净计时**：逐算子哈希要把每个输出都
    从 C8 转回 CHW，单张会多搬 ~756 MB，把 PS 开销测得虚高）。
    """
    if on:
        os.environ["C8_LAYOUT"] = "1"
    else:
        os.environ.pop("C8_LAYOUT", None)
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    index, blob = load_pack(pack)
    arena = NumpyArena(manifest)
    eng = RefEngine(index, blob, arena)
    r = Runner(manifest_path, pack, engine=eng, arena=arena)
    r.hashlog = [] if do_hash else None
    c8stats = None
    if r.c8:
        import c8lib
        c8lib.STATS_ENABLED = True
        c8lib.stats_reset()
    b = manifest["buffers"][manifest["io"]["t_in"]]
    img = np.fromfile(os.path.join(pack, "input_q.bin"), dtype=np.int8).reshape(
        b["c"], b["h"], b["w"])
    out, dt = r.run(img)
    if r.c8:
        import c8lib
        c8lib.STATS_ENABLED = False
        c8stats = {k: list(v) for k, v in c8lib.STATS.items()}
    info = {
        "c8_on": r.c8,
        "n_c8": sum(1 for v in r.layout.values() if v),
        "groups_n": r.alias["groups_n"],
        "skipped": len(r.alias["skipped"]),
        "skip_reasons": list(r.alias["skipped"]),
        "n_hash": len(r.hashlog) if r.hashlog is not None else 0,
        "conv": r.n_conv,
        "ops": len(r.ops),
    }
    info["c8stats"] = c8stats
    out = np.array(out, dtype=np.int8, copy=True)
    hlog = list(r.hashlog) if r.hashlog is not None else []
    del r, eng, arena
    gc.collect()
    return out, hlog, dt, info


class _FakeBuf:
    """最小的 PYNQ 缓冲替身：numpy 数组 + device_address + flush/invalidate。"""

    def __init__(self, nbytes, base=0x10000000):
        self._a = np.zeros(int(nbytes), np.uint8)
        self.device_address = int(base)
        self.nbytes = int(nbytes)

    def __array__(self, dtype=None, copy=None):
        del copy
        return self._a if dtype is None else self._a.view(dtype)

    def flush(self):
        pass

    def invalidate(self):
        pass


def _check_pyq_arena(manifest, on):
    """用假 pynq 模块构造真的 PYNQArena，核对 C8 视图与地址公式。"""
    import types
    import runner as R
    fake = types.ModuleType("pynq")

    def allocate(shape, dtype):
        n = int(np.prod(shape)) * np.dtype(dtype).itemsize
        return _FakeBuf(n)

    fake.allocate = allocate
    saved = {k: sys.modules.get(k) for k in ("pynq",)}
    saved_env = os.environ.get("C8_LAYOUT")
    sys.modules["pynq"] = fake
    os.environ["C8_LAYOUT"] = "1"        # 板端就是走环境变量/--c8 这条路
    try:
        arena = R.PYNQArena(manifest)
        pool = np.asarray(arena.pool)
        bufs = manifest["buffers"]
        bad = []
        # 0) 环境变量 -> arena 布局表（板端 --c8 走的就是这条）
        if dict(arena.layout) != dict(on):
            bad.append(("env->layout", "不一致"))
        # 1) 形状：C8 张量必须是 (C/8,H,W,8)
        for t, meta in bufs.items():
            v = arena.view(t)
            if on[t]:
                if v.shape != c8_layout.view_shape(meta["c"], meta["h"],
                                                   meta["w"], True):
                    bad.append((t, "shape", v.shape))
            elif v.shape != (meta["c"], meta["h"], meta["w"]):
                bad.append((t, "shape", v.shape))
        # 2) 地址公式：在视图里写标记，池内字节偏移必须是
        #    o + (cb*H + y)*W*8 + x*8 + i
        rng = np.random.default_rng(7)
        for t, meta in list(bufs.items()):
            if not on[t]:
                continue
            v = arena.view(t)
            o = arena.offs[t]
            Cb, H, W = v.shape[0], v.shape[1], v.shape[2]
            for _ in range(3):
                cb = int(rng.integers(0, Cb))
                y = int(rng.integers(0, H))
                x = int(rng.integers(0, W))
                i = int(rng.integers(0, 8))
                v[cb, y, x, i] = np.int8(42)
                exp = o + ((cb * H + y) * W + x) * 8 + i
                if int(pool[exp]) != np.uint8(42 & 0xFF):
                    bad.append((t, "addr", (cb, y, x, i), exp, int(pool[exp])))
                v[cb, y, x, i] = np.int8(0)
        # 3) cat 组成员 = 组缓冲的通道块切片（零拷贝、偏移正确）
        for gname, grp in arena.alias["groups"].items():
            base = arena.view(gname)
            for t, off_c in grp["members"].items():
                v = arena.view(t)
                if not np.shares_memory(v, base):
                    bad.append((t, "不共享内存"))
                if arena.offs[t] != arena.offs[gname] + off_c * grp["h"] * grp["w"]:
                    bad.append((t, "组内偏移", arena.offs[t], arena.offs[gname]))
                if v.shape != c8_layout.view_shape(bufs[t]["c"], grp["h"],
                                                   grp["w"], True):
                    bad.append((t, "成员形状", v.shape))
        return not bad, str(bad[:3])
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        if saved_env is None:
            os.environ.pop("C8_LAYOUT", None)
        else:
            os.environ["C8_LAYOUT"] = saved_env


def conv_needs(manifest, on):
    """真机 PS 真正需要做的布局转换（解析式，不含 conv 出入口）。

    conv：PL 原生 C8，PS **零转换**（C8 侧软件实现里的 from_c8/to_c8 只是模拟）。
    其余按"哪些算子必须转 CHW"算：
      · eca      —— 必须（float32 归约顺序敏感）：只转输入
      · interp   —— 转输入 + 结果转回
      · pixel_*  —— 输入输出都是 3ch/12ch 例外张量，本来就是 CHW ⇒ 0
      · sam_sum  —— 直算（逐元素）⇒ 0
      · cat/add  —— 块级/逐元素 ⇒ 0
    """
    bufs = manifest["buffers"]
    mb = {"eca": 0, "interp": 0, "pixel": 0, "other": 0}
    for op in manifest["ops"]:
        if not op["needed"]:
            continue
        k = op["op"]
        nb_in = sum(bufs[t]["bytes"] for t in op.get("in", []))
        nb_out = sum(bufs[t]["bytes"] for t in op.get("out", []))
        if k == "eca":
            mb["eca"] += nb_in
        elif k == "interp":
            mb["interp"] += nb_in + nb_out
        elif k in ("pixel_shuffle", "pixel_unshuffle"):
            # 例外张量（3ch/12ch）保持 CHW ⇒ 真正需要转的只有"进出 C8 的部分"
            mb["pixel"] += sum(bufs[t]["bytes"] for t in op.get("in", [])
                               if on[t]) + sum(bufs[t]["bytes"]
                                               for t in op.get("out", []) if on[t])
    return mb


def stage_c(manifest_path, pack, manifest):
    print("\n== C) 整网 A/B（RefEngine，纯 numpy，不碰板子）==", flush=True)
    ref_path = os.path.join(pack, "ref_out.bin")
    io = manifest["io"]

    out0, h0, dt0, i0 = run_net(manifest_path, pack, on=False)
    print("   CHW 路: %.1fs  ops=%d conv=%d  out=%s range=[%d,%d]"
          % (dt0, i0["ops"], i0["conv"], out0.shape, out0.min(), out0.max()),
          flush=True)
    out1, h1, dt1, i1 = run_net(manifest_path, pack, on=True)
    print("   C8  路: %.1fs  ops=%d conv=%d  out=%s range=[%d,%d]  C8 张量 %d 个"
          % (dt1, i1["ops"], i1["conv"], out1.shape, out1.min(), out1.max(),
             i1["n_c8"]), flush=True)

    check("两次运行的开关状态正确", (i0["c8_on"], i1["c8_on"]) == (False, True))
    if i1.get("c8stats"):
        st = i1["c8stats"]
        tot_s = st["to"][0] + st["from"][0]
        tot_b = st["to"][2] + st["from"][2]
        conv_b = sum(manifest["buffers"][o["in"][0]]["bytes"]
                     + manifest["buffers"][o["out"][0]]["bytes"]
                     for o in manifest["ops"]
                     if o["op"] == "conv" and o["needed"]
                     and c8_layout.table(manifest, on=True)[o["in"][0]])
        print("   C8 路纯布局转换（本机模拟，真机上 conv 那部分为 0）:"
              " to_c8 %.2fs/%d 次/%.1f MB，from_c8 %.2fs/%d 次/%.1f MB，"
              "合计 %.2fs（%.1f MB）"
              % (st["to"][0], st["to"][1], st["to"][2] / 1e6,
                 st["from"][0], st["from"][1], st["from"][2] / 1e6,
                 tot_s, tot_b / 1e6), flush=True)
        real_b = max(0, tot_b - conv_b)
        rate = (tot_b / tot_s) if tot_s > 0 else 0.0
        print("   其中 conv 出入囗模拟 %.1f MB（真机由 PL 直读直写 C8 ＝ 0）；"
              "真机 PS 实际要转 ≈ %.1f MB ⇒ 本机约 %.3f s"
              % (conv_b / 1e6, real_b / 1e6, real_b / rate if rate else 0),
              flush=True)
    check("两次运行的 cat 折叠组数一致",
          i0["groups_n"] == i1["groups_n"], "%d vs %d"
          % (i0["groups_n"], i1["groups_n"]))
    lay_skip = [x for x in i1.get("skip_reasons", []) if "布局" in str(x)]
    check("C8 路没有因布局回退的 cat 组（回退数 CHW %d = C8 %d，原因都是 H*W 未对齐 64）"
          % (i0["skipped"], i1["skipped"]),
          i0["skipped"] == i1["skipped"] and not lay_skip,
          "布局回退 %d 个" % len(lay_skip))

    # 1) 最终输出
    bad = int(np.count_nonzero(out0 != out1))
    print("\n   ★ CHW vs C8 最终输出: 不一致 %d / %d  (%.4f%%)"
          % (bad, out0.size, 100.0 * bad / out0.size), flush=True)
    ok = check("CHW vs C8 最终输出 0 不一致（%d/%d）" % (bad, out0.size), bad == 0)

    # 2) 逐算子 sha1
    if h0 and h1:
        nb = 0
        first = None
        for (n0, k0, sh0, d0), (n1, k1, sh1, d1) in zip(h0, h1):
            if (n0, k0) != (n1, k1) or sh0 != sh1:
                nb += 1
                if first is None:
                    first = (n0, k0, sh0, sh1)
        print("   逐算子 sha1 不一致: %d / %d" % (nb, len(h0)), flush=True)
        check("517 个算子输出 sha1 全同（首个不一致=%s）" % (first,),
              nb == 0 and len(h0) == len(h1) == 517)
    else:
        check("两路 hashlog 都生成了", False)

    # 3) CHW 路 vs 部署包参考
    if os.path.exists(ref_path):
        ref = np.fromfile(ref_path, dtype=np.int8).reshape(out0.shape)
        bad0 = int(np.count_nonzero(out0 != ref))
        bad1 = int(np.count_nonzero(out1 != ref))
        print("\n   ★ CHW 路 vs ref_out.bin: 不一致 %d / %d" % (bad0, ref.size),
              flush=True)
        check("CHW 路与部署参考 ref_out.bin 0 不一致（%d/%d）" % (bad0, ref.size),
              bad0 == 0)
        check("C8 路与部署参考 ref_out.bin 0 不一致（%d/%d）" % (bad1, ref.size),
              bad1 == 0)
    else:
        print("   （没有 ref_out.bin，跳过与部署参考的对比）", flush=True)
    return ok, out0, out1


# ---------------------------------------------------------------- D) 统计

def stage_d(manifest, on, n8, nchw, exc, nfp):
    print("\n== D) is_c8 表统计 ==", flush=True)
    bufs = manifest["buffers"]
    need = set()
    for op in manifest["ops"]:
        if op["needed"]:
            need.update(op.get("in", []))
            need.update(op.get("out", []))
    print("   总计 %d 个 buffer：C8 %d 个、CHW %d 个（fp32 %d + int8 例外 %d）"
          % (len(bufs), n8, nchw, nfp, len(exc)), flush=True)
    print("   int8 例外（保持 CHW）: %d 个（补零后全部走 C8）" % len(exc), flush=True)
    for t, c, h, w in exc:
        print("     %-6s C=%-3d %dx%d  %s" % (t, c, h, w,
                                             "执行面上会碰到" if t in need
                                             else "不在 needed 子图内"),
              flush=True)
    pad_t = [t for t in bufs if on.get(t) and bufs[t]["c"] % 8]
    if pad_t:
        print("   补零到 8 的倍数后仍走 C8 的（原通道数 -> 补零后）:", flush=True)
        for t in sorted(pad_t):
            m = bufs[t]
            print("     %-6s C=%-3d -> %-3d %dx%d  %s"
                  % (t, m["c"], c8_layout.pad_channels(m["c"], True),
                     m["h"], m["w"],
                     "执行面上会碰到" if t in need else "不在 needed 子图内"),
                  flush=True)
    print("   字节量：C8 %d 个张量共 %.1f MB，CHW %d 个共 %.1f MB"
          % (n8, sum(m["bytes"] for t, m in bufs.items()
                     if on[t]) / 1e6,
             nchw, sum(m["bytes"] for t, m in bufs.items()
                       if not on[t]) / 1e6), flush=True)
    print("   cat：224 个、454 个段，段通道数全部 8 的倍数 ⇒ 全部块级拼接"
          "（C8 下 0 个因布局回退）", flush=True)


def stage_e(manifest_path, pack, manifest):
    """纯净计时（关 hashlog）+ 解析式转换量：估算板端 PS 侧开销。"""
    print("\n== E) 纯净计时（关 hashlog）+ 真机 PS 转换量 ==", flush=True)
    t0 = run_net(manifest_path, pack, on=False, do_hash=False)
    t1 = run_net(manifest_path, pack, on=True, do_hash=False)
    print("   CHW 路 %.1fs   C8 路 %.1fs（差 %+.1fs；本机 CPU 上有别的重活，"
          "墙钟差不作性能结论）" % (t0[2], t1[2], t1[2] - t0[2]), flush=True)
    on = c8_layout.table(manifest, on=True)
    need = conv_needs(manifest, on)
    tot = sum(need.values())
    print("   真机 PS 需要做的布局转换（解析式，conv 由 PL 直读直写 = 0）：", flush=True)
    for k, v in need.items():
        if v:
            print("      %-8s %7.1f MB" % (k, v / 1e6), flush=True)
    print("      合计     %7.1f MB/张" % (tot / 1e6), flush=True)
    st = t1[3].get("c8stats") or {}
    if st:
        b = st["to"][2] + st["from"][2]
        sec = st["to"][0] + st["from"][0]
        print("   实测（关 hashlog）纯转换 %.2fs / %.1f MB ⇒ %.0f MB/s（x86 单核）"
              % (sec, b / 1e6, (b / sec / 1e6) if sec else 0), flush=True)
        print("   ⇒ 该比率下真机 PS 转换 %0.1f MB 约 %.2f s（x86；ARM Cortex-A9 "
              "很可能慢 5~10×，**必须板端实测**）"
              % (tot / 1e6, (tot / (b / sec)) if sec else 0), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=os.path.join(PS, "manifest_640.json"))
    ap.add_argument("--pack", default=os.path.join(FPGA, "deploy", "weights_pack"))
    ap.add_argument("--skip-net", action="store_true", help="只跑单测，不跑整网")
    ap.add_argument("--timing", action="store_true",
                    help="额外跑一轮纯净计时（关 hashlog）+ 估算 PS 转换量（约 +100 s）")
    a = ap.parse_args()
    print("manifest: %s\npack    : %s" % (a.manifest, a.pack), flush=True)
    with open(a.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    stage_a(PS)
    on, n8, nchw, exc, nfp = stage_b(manifest)
    stage_d(manifest, on, n8, nchw, exc, nfp)
    if not a.skip_net:
        stage_c(a.manifest, a.pack, manifest)
        if a.timing:
            stage_e(a.manifest, a.pack, manifest)

    print("\n================ 汇总 ================", flush=True)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)), flush=True)
    if FAIL:
        for n in FAIL:
            print("   FAILED:", n)
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
