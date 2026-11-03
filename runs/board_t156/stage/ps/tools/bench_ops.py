#!/usr/bin/env python3
"""非卷积算子的**搬运量账本** + x86 基准（不需要板子）。

为什么要单独量"搬运量"而不是只看耗时：板端 Zynq XC7Z015 的 CMA 池是
**非缓存映射**，DDR 实测约 183 MB/s，非卷积部分 8.12 s 已经跑到
1354 MB / 183 MB/s ≈ 7.4 s 的 ~91%。在这个区间里"把循环写快"几乎没有
收益（每个字节只搬一遍了），**唯一的收益来自少搬**。所以先算清账，再改。

三个子命令
----------
``account``  按 manifest 算"每个非卷积算子搬了多少字节"（读 + 写 + 私有暂存），
             按总量排序；``--fuse`` 给出运行 SAM 块融合之后的同一张表。
             合计与实测量的 1354 MB 对得上（差 < 1%）。
``bench``    用合成 C8 数组测每个算子的耗时与等效带宽（x86）。
             ⚠️ x86 是缓存内存，**"直读池 vs 先搬进私有缓冲"这个决定不能
             用这里的数字判断**（板端实测差 4.9×，见 ops_numpy._io_bufs）。
``ab``       改动前后的**逐位对拍**：sam_sum 复用暂存 vs 原 5 处 copyto、
             eca 直方图通道和 vs chsum_c8(LUT(v))。

用法
----
    python3 ps/tools/bench_ops.py account                 # 当前实现（未融合）
    python3 ps/tools/bench_ops.py account --fuse          # SAM 块融合之后
    python3 ps/tools/bench_ops.py bench  --repeat 3
    python3 ps/tools/bench_ops.py ab
"""
import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.dirname(HERE)
PYNQ = os.path.join(PS, "pynq")
C8DIR = os.path.join(PS, "c8")
for _p in (PYNQ, C8DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 板端标定值（docs/设计/记录_20261023_pp整网错误根因与C8层间加速.md）
DDR_MBPS = 183.0        # np.copyto 实测（池 → 池，整块）
TRAFFIC_MEAS_MB = 1354.0  # 之前分析的实测量（本脚本应能复算出来）

PACK_CANDIDATES = (
    os.path.join(PS, os.pardir, "board", "board_bundle", "weights_pack"),
    os.path.join(PS, os.pardir, "deploy", "weights_pack"),
)


def find_pack(explicit=None):
    for p in ([explicit] if explicit else []) + list(PACK_CANDIDATES):
        if p and os.path.exists(os.path.join(p, "index.json")):
            return os.path.abspath(p)
    return None


# ------------------------------------------------------------------ 账本
def _load(manifest_path, pack):
    import c8_layout
    from runner import Runner, compute_cat_alias       # noqa: F401
    m = json.load(open(manifest_path, "r", encoding="utf-8"))
    layout = c8_layout.table(m, on=True)               # 板端是纯 C8（--c8）
    alias = compute_cat_alias(m, is_c8=layout)
    scales = {}
    if pack is not None:
        scales = json.load(open(os.path.join(pack, "index.json")))["scales"]
    return m, layout, alias, scales


def _plan_sam_cluster(m, alias):
    """复刻 runner._plan_sam_block 的判定（少一层包装，便于独立核算）。

    返回 (triples, skip, hand)：
        triples[eca_id] = (cat_id, sam_id, segs, t84)
        skip            = 被跳过的 cat op id
        hand            = 可改写成交接缓冲的 interp 输出张量名
    """
    ops = [o for o in m["ops"] if o.get("needed", True)]
    by_id = {o["id"]: o for o in ops}
    prod, cons = {}, defaultdict(list)
    for o in ops:
        for t in o["out"]:
            prod.setdefault(t, o)
        for t in o["in"]:
            cons[t].append(o)
    triples, skip, hand = {}, set(), {}
    for o in ops:
        if o["op"] != "eca":
            continue
        cat, ss = by_id.get(o["id"] - 1), by_id.get(o["id"] + 1)
        if cat is None or ss is None or cat["op"] != "cat" or ss["op"] != "sam_sum":
            continue
        t84, attn = cat["out"][0], o["out"][0]
        segs = list(cat["in"])
        if len(segs) != 3 or list(ss["in"])[:3] != segs or ss["in"][3] != attn:
            continue
        if len(cons.get(t84, [])) != 1 or len(cons.get(attn, [])) != 1:
            continue
        triples[o["id"]] = (cat["id"], ss["id"], segs, t84)
        skip.add(cat["id"])
    for _eid, (_c, _s, segs, _t84) in triples.items():
        for t in segs:
            cs = [c["id"] for c in cons.get(t, [])]
            p = prod.get(t)
            if p is not None and p["op"] == "interp" and len(cs) == 2:
                hand[t] = True
    return triples, skip, hand


def account(manifest_path, pack, fuse, fuse_conv=False):
    """按 manifest 复算每个非卷积算子的搬运量。

    单位是**字节**，分四栏记（板端两类内存速度差 1.4×，必须分开记）：
        pool_r / pool_w  —— 池（CMA 非缓存）上的读/写，实测 ~183 MB/s
        priv_r / priv_w  —— 私有缓冲（缓存）上的读/写，~260 MB/s（i8_scale 实测）
    合计 = 四栏之和，与"1354 MB"的实测口径一致（同一个 183 MB/s 折算）。
    """
    import c8_layout
    m, layout, alias, scales = _load(manifest_path, pack)
    bufs = m["buffers"]
    noop = alias["noop"]
    triples, skip_cats, hand = _plan_sam_cluster(m, alias)
    sam_by_id = {v[1]: (k, v) for k, v in triples.items()}

    def nb(t):
        meta = bufs[t]
        return c8_layout.nbytes(meta["c"], meta["h"], meta["w"],
                                bool(layout.get(t)))

    def s(t):
        return float(scales.get(t, 1.0))

    prod, cons = {}, defaultdict(list)
    for o in m["ops"]:
        for t in o.get("out", []):
            prod.setdefault(t, o)
        for t in o.get("in", []):
            cons[t].append(o)
    acc = defaultdict(Counter)
    notes = defaultdict(set)
    for op in m["ops"]:
        if not op.get("needed", True):
            continue
        k = op["op"]
        o0 = op["out"][0]
        if k == "conv":
            continue                                # FPGA 侧，不在本账
        if k == "cat":
            if fuse and op["id"] in skip_cats:
                notes["cat"].add("融合：t84 只喂 eca ⇒ 整块不物化")
                continue
            lay = noop.get(op["id"])
            acc["cat"]["n"] += 1
            if lay is None:                          # 非别名：整块读 + 整块写
                acc["cat"]["pool_r"] += sum(nb(t) for t in op["in"])
                acc["cat"]["pool_w"] += nb(o0)
                notes["cat"].add("非别名（H·W 不是 64 的倍数）")
                continue
            so = s(o0)
            for t, _off, mode in lay:
                b = nb(t)
                if fuse_conv and mode == "direct" and s(t) != so and \
                        prod.get(t, {}).get("op") == "conv" and \
                        len(cons.get(t, [])) == 1:
                    notes["cat"].add("conv→cat FUSE_OUT：生产者直接写目标标度")
                    continue
                if mode == "copy":                   # 重定标着拷进组缓冲
                    acc["cat"]["pool_r"] += b
                    acc["cat"]["pool_w"] += b
                    acc["cat"]["n_copy"] += 1
                elif s(t) != so:                     # 原地重定标
                    acc["cat"]["pool_r"] += b
                    acc["cat"]["pool_w"] += b
                    acc["cat"]["n_rescale"] += 1
                else:
                    acc["cat"]["n_noop"] += 1
        elif k == "add":
            b = nb(o0)
            so = s(o0)
            acc["add"]["n"] += 1
            acc["add"]["pool_w"] += b                # add_into 写 out
            for t in op["in"]:
                if s(t) == so:
                    acc["add"]["pool_r"] += b        # add_into 直读操作数
                else:                                # _req 造临时数组
                    acc["add"]["pool_r"] += b
                    acc["add"]["priv_w"] += b
                    acc["add"]["priv_r"] += b
        elif k == "sam_sum":
            b = nb(o0)                               # out 张量字节数
            n_y = sum(nb(t) for t in op["in"][:3])   # 3 路 y 合计
            b_x = nb(op["in"][4])
            acc["sam_sum"]["n"] += 1
            if fuse and op["id"] in sam_by_id:
                # 3 路 y 的暂存在 eca 阶段做过（这里 0 趟）；只剩 x + 内核 + 回写
                acc["sam_sum"]["pool_r"] += b_x
                acc["sam_sum"]["priv_w"] += b_x
                acc["sam_sum"]["priv_r"] += n_y + b_x + b
                acc["sam_sum"]["priv_w"] += b
                acc["sam_sum"]["priv_r"] += b
                acc["sam_sum"]["pool_w"] += b
                notes["sam_sum"].add("融合：3 处 copyto 归零（复用 eca 暂存）")
            else:
                acc["sam_sum"]["pool_r"] += n_y + b_x
                acc["sam_sum"]["priv_w"] += n_y + b_x
                acc["sam_sum"]["priv_r"] += n_y + b_x
                acc["sam_sum"]["priv_w"] += b
                acc["sam_sum"]["priv_r"] += b
                acc["sam_sum"]["pool_w"] += b
                notes["sam_sum"].add("5 处 copyto：池→私有(4) + 私有→池(1)")
        elif k == "interp":
            si, so_ = nb(op["in"][0]), nb(o0)
            acc["interp"]["n"] += 1
            acc["interp"]["pool_r"] += si            # 整块搬进私有
            acc["interp"]["priv_w"] += si
            acc["interp"]["priv_r"] += si            # 内核读 s
            acc["interp"]["priv_w"] += so_           # 内核写 d
            if fuse and o0 in hand:
                notes["interp"].add("交接缓冲：内核直接写出去，不落池")
            else:
                acc["interp"]["priv_r"] += so_       # d → dst
                acc["interp"]["pool_w"] += so_
        elif k == "eca":
            n = nb(op["in"][0])                      # == 3 段之和
            acc["eca"]["n"] += 1
            if fuse and op["id"] in triples:
                cid, sid, segs, _t84 = triples[op["id"]]
                for t in segs:
                    if t in hand:
                        acc["eca"]["priv_w"] += nb(t)   # 已在交接缓冲，不读池
                    else:
                        acc["eca"]["pool_r"] += nb(t)
                        acc["eca"]["priv_w"] += nb(t)
                acc["eca"]["priv_r"] += n            # LUT 查表读
                acc["eca"]["priv_w"] += n            # 映射结果写
                acc["eca"]["priv_r"] += n            # 沿像素轴整型累加
                notes["eca"].add("融合：按段求 LUT 通道和（t84 不物化）")
            else:
                acc["eca"]["pool_r"] += n            # 拷贝 r 进私有
                acc["eca"]["priv_w"] += n
                acc["eca"]["priv_r"] += n            # chsum 扫一遍
        elif k in ("pixel_unshuffle", "pixel_shuffle"):
            si, so_ = nb(op["in"][0]), nb(o0)
            acc["pixel_"]["n"] += 1
            # C8 → CHW → 重排 → 补零 → C8 → 写池：约 3 读 3 写（估算）
            acc["pixel_"]["pool_r"] += si
            acc["pixel_"]["priv_r"] += 3 * (si + so_)
            acc["pixel_"]["priv_w"] += 3 * (si + so_)
            acc["pixel_"]["pool_w"] += so_
            notes["pixel_"].add("C8↔CHW 来回转换（补零张量，2 个 op 占 1.5%）")
        else:
            raise SystemExit("未知算子 %r" % (k,))

    rows, tot = [], Counter()
    for k, c in acc.items():
        rows.append((k, c["n"], c["pool_r"], c["pool_w"], c["priv_r"], c["priv_w"]))
        for f in ("pool_r", "pool_w", "priv_r", "priv_w"):
            tot[f] += c[f]
    rows.sort(key=lambda r: -(sum(r[2:])))
    return rows, tot, notes


FOOTNOTE = ("池 = CMA 非缓存（实测 ~183 MB/s），私有 = 缓存缓冲（~260 MB/s）。"
            "合计一栏与 1354 MB 的实测口径一致（同一个 183 MB/s 折算），"
            "所以'占比'可以直接当'时间占比'看。")

PRIV_MBPS = 260.0       # i8_scale / i8_copy 实测（私有缓冲）


def _fmt_row(name, n, pr, pw, vr, vw, total):
    tb = pr + pw + vr + vw
    return ("%-10s %5d %8.1f %8s %8.1f %10.1f %6.1f%% %7.2f %7.2f"
            % (name, n, (pr + pw) / 1e6, "", (vr + vw) / 1e6, tb / 1e6,
               100.0 * tb / total, tb / (DDR_MBPS * 1e6),
               (pr + pw) / (DDR_MBPS * 1e6) + (vr + vw) / (PRIV_MBPS * 1e6)))


def _total(t):
    return sum(t[f] for f in ("pool_r", "pool_w", "priv_r", "priv_w"))


def cmd_account(a):
    pack = find_pack(a.pack)
    if pack is None:
        raise SystemExit("找不到 weights_pack/index.json（需要 scales 才能算重定标）")
    base, tot0, _ = account(a.manifest, pack, False, a.fuse_conv)
    fus, tot1, notes = account(a.manifest, pack, True, a.fuse_conv)
    n0, n1 = _total(tot0), _total(tot1)
    print("manifest: %s   带宽模型 %.0f MB/s（池/非缓存，np.copyto 实测）"
          " / %.0f MB/s（私有/缓存）"
          % (os.path.basename(a.manifest), DDR_MBPS, PRIV_MBPS))
    print("对照：之前分析的实测非卷积搬运量 %.0f MB = 8.12 s（@183 MB/s 折 7.4 s）"
          % TRAFFIC_MEAS_MB)
    hdr = ("%-10s %5s %8s %8s %8s %10s %7s %7s %7s"
           % ("算子", "次数", "池MB", "", "私有MB", "合计MB", "占比", "按183s", "按实速s"))
    for tag, rows, tt in (("① 当前实现（未融合）", base, tot0),
                          ("② SAM 块融合之后", fus, tot1)):
        print()
        print("=" * 82)
        print(tag)
        print(hdr)
        print("-" * 82)
        for k, n, pr, pw, vr, vw in rows:
            print(_fmt_row(k, n, pr, pw, vr, vw, _total(tt)))
        print("-" * 82)
        tb = _total(tt)
        pool = tt["pool_r"] + tt["pool_w"]
        priv = tt["priv_r"] + tt["priv_w"]
        print("%-10s %5s %8.1f %8s %8.1f %10.1f %6.1f%% %7.2f %7.2f"
              % ("合计", "", pool / 1e6, "", priv / 1e6, tb / 1e6, 100.0,
                 tb / (DDR_MBPS * 1e6),
                 pool / (DDR_MBPS * 1e6) + priv / (PRIV_MBPS * 1e6)))
        print("   对实测 %.0f MB 的偏差 %+.1f%%"
              % (TRAFFIC_MEAS_MB, 100.0 * (tb / 1e6 / TRAFFIC_MEAS_MB - 1)))
    print()
    p0, p1 = tot0["pool_r"] + tot0["pool_w"], tot1["pool_r"] + tot1["pool_w"]
    print("融合减少：合计 %.1f MB（%.1f%%）；**池（非缓存）那一档 %.1f → %.1f MB（−%.0f%%）**"
          % ((n0 - n1) / 1e6, 100.0 * (n0 - n1) / n0,
             p0 / 1e6, p1 / 1e6, 100.0 * (1 - p1 / p0)))
    t0 = p0 / (DDR_MBPS * 1e6) + (tot0["priv_r"] + tot0["priv_w"]) / (PRIV_MBPS * 1e6)
    t1 = p1 / (DDR_MBPS * 1e6) + (tot1["priv_r"] + tot1["priv_w"]) / (PRIV_MBPS * 1e6)
    print("按'池 183 + 私有 260'模型：池侧 %.2f → %.2f s；合计 %.2f → %.2f s（−%.2f s）"
          % (p0 / (DDR_MBPS * 1e6), p1 / (DDR_MBPS * 1e6), t0, t1, t0 - t1))
    print("注：" + FOOTNOTE)
    for k, v in sorted(notes.items()):
        print("  [%s] %s" % (k, "；".join(sorted(v))))


# ------------------------------------------------------------------ 基准
def _c8(shape_c_h_w, fill=None, rng=None):
    c, h, w = shape_c_h_w
    assert c % 8 == 0
    v = (rng.integers(-128, 128, (c // 8, h, w, 8), dtype=np.int8)
         if rng is not None else np.zeros((c // 8, h, w, 8), np.int8))
    if fill is not None:
        v[...] = fill
    return v


def _time(fn, repeat, warm=1):
    for _ in range(warm):
        fn()
    ts = []
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t)
    return min(ts)


def cmd_bench(a):
    import c8_layout
    import i8_fast
    import ops_numpy as ops
    rng = np.random.default_rng(20261027)
    rep = a.repeat
    print("x86 基准（缓存内存！）：每例 %d 次取最小；耗时单位 ms，带宽 = 模型搬运量/耗时"
          % rep)
    print("⚠️ '直读池 vs 私有暂存' 在本机**无法判据**：板端池是非缓存 CMA，"
          "同一 C 内核 3.65 s/op vs 0.75 s/op。")
    print()
    rows = []

    def run(name, mb, fn):
        try:
            dt = _time(fn, rep)
        except Exception as e:                       # noqa: BLE001
            rows.append((name, mb, None, str(e)[:48]))
            return
        rows.append((name, mb, dt * 1e3, None))

    MB = 1e6
    # 0) 纯 memcpy 基线
    big = _c8((48, 320, 320), rng=rng).reshape(-1)
    dst = np.empty_like(big)
    run("memcpy 4.9MB（基线）", 2 * big.size / MB,
        lambda: np.copyto(dst, big))
    # 1) cat 的原地重定标（i8_fast 亲和/查表 vs numpy 查表）
    lut = np.frombuffer(bytes(np.clip(np.rint(
        np.arange(-128, 128) * 1.7), -128, 127).astype(np.int8)), np.int8)
    if i8_fast.available():
        run("cat 重定标(原地,C)", 2 * big.size / MB,
            lambda: i8_fast.lut_ip(big, lut))
    def _np_lut():
        big[:] = lut[big.view(np.uint8)]
    run("cat 重定标(原地,np)", 2 * big.size / MB, _np_lut)
    # 2) interp 1/2 与 2/1
    src = _c8((48, 320, 320), rng=rng)
    dst_half = _c8((48, 160, 160))
    run("interp 1/2 (池→私→池)", 3 * src.size / MB,
        lambda: ops.interp_c8_into(dst_half, src, 1, 2))
    src_s = _c8((48, 160, 160), rng=rng)
    dst_full = _c8((48, 320, 320))
    run("interp 2/1 (池→私→池)", 3 * (src_s.size + dst_full.size) / MB,
        lambda: ops.interp_c8_into(dst_full, src_s, 2, 1))
    # 3) sam_sum：原 5 处 copyto vs 复用暂存
    y0, y2, y4, xx = (_c8((48, 320, 320), rng=rng) for _ in range(4))
    out = _c8((48, 320, 320))
    attn = rng.random((3 * 48, 1, 1)).astype(np.float32)
    sc = (0.0107458238, 0.00834746197, 0.00943494463, 0.0180516705, 0.013704585)
    ok_c = ops._sam_sum_c8_c() is not None
    if not ok_c:                                     # x86 默认不用 C 内核，这里强制
        os.environ["SAM_SUM_USE_C"] = "1"
        ops._C_LIB.update({"tried": False, "lib": None, "fn": None, "fn8": None,
                           "err": None, "warned": False, "fn_sum": None,
                           "fn_interp": None})
        ok_c = ops._sam_sum_c8_c() is not None
        if not ok_c:
            print("sam_sum C 内核不可用：%s" % ops._C_LIB.get("err"))
    if ok_c:
        run("sam_sum 原(5 次 copyto)", 15 * out.size / MB,
            lambda: ops.sam_sum_c8_into(out, y0, y2, y4, attn, xx, *sc))
        run("sam_sum 前(4 次 copyto)", 15 * out.size / MB,
            lambda: ops.sam_sum_c8_into(out, y0, y2, y4, attn, xx, *sc))
        tok = ops.sam_stage([(y0, False), (y2, False), (y4, False)])
        run("sam_sum 后(1 次 copyto+暂存复用)", 9 * out.size / MB,
            lambda: ops.sam_sum_c8_into(out, y0, y2, y4, attn, xx, *sc,
                                        pre_staged=tok))
        # 反面教材：把 5 处 copyto 全删掉、让 C 内核直读池。x86 上它最快，
        # 板端却慢 4.9×（CMA 非缓存 + 逐字节标量内核 = 每个字节一笔 AXI）。
        # 这一行专门用来对照 —— 别照它改代码。
        fi = ops._sam_sum_c8_c()
        bo = np.empty_like(out).reshape(-1)
        af = np.ascontiguousarray(attn, np.float32)
        run("sam_sum 全删拷贝(板端会慢 4.9×)", 5 * out.size / MB,
            lambda: fi(y0.ctypes.data, y2.ctypes.data, y4.ctypes.data,
                       xx.ctypes.data, af.ctypes.data, out.shape[0],
                       out.shape[1] * out.shape[2], *sc, bo.ctypes.data))
    # 4) eca：chsum_c8（拷贝 + 逐字节扫） vs chsum_lut（直方图，不物化）
    v = _c8((144, 320, 320), rng=rng)
    luts = [bytes(np.arange(-128, 128, dtype=np.int8))]
    if ops._C_LIB.get("fn_sum") is None:
        os.environ["SAM_SUM_USE_C"] = "1"
        ops._C_LIB.update({"tried": False, "lib": None, "fn": None, "fn8": None,
                           "err": None, "warned": False, "fn_sum": None,
                           "fn_interp": None})
        ops._ensure_lib()
    if ops._C_LIB.get("fn_sum") is not None:
        run("eca chsum_c8(拷贝+扫)", 3 * v.size / MB,
            lambda: ops.chsum_c8(v, 144))
    tok = ops.sam_stage([(v, False)])
    run("eca LUT映射+整型和(C)", 3 * v.size / MB,
        lambda: ops.chsum_lut(tok["arrays"], tok["shapes"],
                              [bytes(np.clip(np.rint(np.arange(-128, 128) * 1.4),
                                             -128, 127).astype(np.int8))], 144))
    # 5) add（饱和相加 C 内核）
    if i8_fast.available():
        a2, b2 = _c8((48, 320, 320), rng=rng), _c8((48, 320, 320), rng=rng)
        o2 = _c8((48, 320, 320))
        run("add(VQADD.S8)", 3 * o2.size / MB,
            lambda: i8_fast.add_into(o2, a2, b2))
    # 6) pixel_*（C8 ↔ CHW 来回转）
    import ops_c8 as oc8
    x = _c8((48, 320, 320), rng=rng)
    run("pixel_unshuffle C8(转CHW+补零)", 12 * x.size / MB,
        lambda: oc8.pixel_unshuffle(x, "c8", 2, out_lay="c8", c_in=48, c_out=192))

    print("%-32s %10s %10s %10s" % ("用例", "模型MB", "ms", "MB/s"))
    print("-" * 66)
    for name, mb, ms, err in rows:
        if err:
            print("%-32s %10.2f %10s  n/a: %s" % (name, mb, "-", err))
        else:
            print("%-32s %10.2f %10.3f %10.1f" % (name, mb, ms, mb / (ms / 1e3)))
    print("-" * 66)
    print("提示：x86 上'私有暂存'看着慢（多两趟拷贝），板端却快 4.9×；"
          "本表只用于看**算子本身的相对开销**与回归。")


# ------------------------------------------------------------------ 对拍
def cmd_ab(a):
    import ops_numpy as ops
    rng = np.random.default_rng(7)
    print("逐位对拍（合成数据，覆盖全 int8 值域）")
    print()

    # 1) sam_sum：pre_staged vs 5 处 copyto
    ok_c = False
    os.environ["SAM_SUM_USE_C"] = "1"
    ops._C_LIB.update({"tried": False, "lib": None, "fn": None, "fn8": None,
                       "err": None, "warned": False, "fn_sum": None,
                       "fn_interp": None})
    try:
        ops._sam_sum_c8_c()
        ok_c = True
    except Exception as e:                           # noqa: BLE001
        print("  sam_sum C 内核不可用（跳过该项）：%s" % e)
    if ok_c:
        for C, H, W in ((48, 40, 40), (8, 3, 5), (192, 7, 1)):
            y0, y2, y4, xx = (rng.integers(-128, 128, (C // 8, H, W, 8),
                                           dtype=np.int8) for _ in range(4))
            attn = (rng.integers(0, 4097, (3 * C, 1, 1)).astype(np.float32)
                    / np.float32(4096.0))
            sc = (0.0107, 0.0083, 0.0094, 0.0181, 0.0137)
            a = np.zeros_like(y0)
            b = np.zeros_like(y0)
            ops.sam_sum_c8_into(a, y0, y2, y4, attn, xx, *sc)
            tok = ops.sam_stage([(y0, False), (y2, False), (y4, False)])
            ops.sam_sum_c8_into(b, y0, y2, y4, attn, xx, *sc, pre_staged=tok)
            print("  sam_sum(5 拷贝) vs sam_sum(暂存复用)  C=%-3d H=%-3d W=%-3d  %s"
                  % (C, H, W, "逐字节相同" if np.array_equal(a, b)
                     else "!! 第 %d 处不同" % int(np.argmax((a != b).ravel()))))

    # 2) eca：chsum_lut vs chsum_c8(LUT(v)) —— 必须逐位相同（int64 精确）
    import lut_util
    for (s_from, s_to) in ((0.03655245616155294, 0.00708162797670168),
                           (0.008337359212514923, 0.031052887287),
                           (1.0, 1.0)):
        tab = lut_util.rescale_lut_bytes(s_from, s_to)
        lut_np = np.frombuffer(tab, np.int8)
        v = rng.integers(-128, 128, (6, 9, 13, 8), dtype=np.int8)
        # 参考：C8 视图里通道 c=b*8+cc 的元素在 (pixel*8+cc)，逐通道整型和
        want = np.concatenate([
            lut_np[v[b].view(np.uint8)].reshape(-1, 8).sum(axis=0, dtype=np.int64)
            for b in range(v.shape[0])])
        tok = ops.sam_stage([(v, False)])
        got = ops.chsum_lut(tok["arrays"], tok["shapes"], [tab], 48)
        same = np.array_equal(want, got)
        print("  eca LUT 通道和 vs chsum_c8(LUT(v))      s=%.6g→%.6g  %s"
              % (s_from, s_to, "逐位相同" if same else
                 "!! 差 %d 通道" % int(np.count_nonzero(want != got))))

    # 3) interp 交接缓冲 vs 写池
    ops._C_LIB.update({"tried": False, "lib": None, "fn": None, "fn8": None,
                       "err": None, "warned": False, "fn_sum": None,
                       "fn_interp": None})
    try:
        ops._ensure_lib()
        for num, den in ((1, 2), (2, 1), (1, 4)):
            C, H, W = 16, 20, 24
            src = rng.integers(-128, 128, (C // 8, H, W, 8), dtype=np.int8)
            h2, w2 = H * num // den, W * num // den
            d1 = np.zeros((C // 8, h2, w2, 8), np.int8)
            d2 = np.zeros_like(d1)
            ops.interp_c8_into(d1, src, num, den)
            hb = ops.handoff_buf("ab", d2.size)
            ops.interp_c8_into(d2, src, num, den, handoff=hb)
            got = hb.reshape(d2.shape)
            print("  interp %d/%d 写池 vs 交接缓冲  %dx%dx%d  %s"
                  % (num, den, C, h2, w2, "逐字节相同"
                     if np.array_equal(d1, got) else "!! 不同"))
    except Exception as e:                           # noqa: BLE001
        print("  interp 对拍跳过：%s" % e)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _common(p):
        p.add_argument("--manifest", default=os.path.join(PS, "manifest_640.json"))
        p.add_argument("--pack", default=None, help="weights_pack 目录（含 index.json）")

    p = sub.add_parser("account", help="搬运量排名表（manifest 驱动）")
    _common(p)
    p.add_argument("--fuse", action="store_true", help="按 SAM 块融合后的口径算")
    p.add_argument("--fuse-conv", action="store_true",
                   help="同时假设 conv→cat FUSE_OUT 已开（98 段直接写目标标度）")
    p.set_defaults(func=cmd_account)

    p = sub.add_parser("bench", help="合成数据测时（x86）")
    p.add_argument("--repeat", type=int, default=3)
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("ab", help="改动前后逐位对拍")
    p.set_defaults(func=cmd_ab)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
