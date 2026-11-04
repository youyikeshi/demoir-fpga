#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diverge_probe.py —— p0067「板端 vs 主机 int8」差异定位（**纯主机**，不 ssh / 不碰板 / 不跑 Vivado）

做的事
------
1. `--mode ab`   ：在主机上跑 2×2×2 = 8 个 **单变量 A/B** 组合，把每个组合的 `out1_u8`
   与**板端** `p0067_out1_u8.npy`（`(256,256,3)` HWC）逐像素比，报
   `不同像素数 / max|Δ| / 带符号均值 / mean|Δ|`；同时报与 float golden 的同口径指标。
   逐层（conv 层）记录 int8 `y_q` 的 sha1 + 统计，用于**定位组合之间第一个发散的层**。
2. `--mode dump`：把某一组合的**逐层 out** 落盘（conv ⇒ int8 反量化前 `y_q`；
   非 conv（cat/eca/sam/interp/shuffle/convfp）⇒ f32），供板端逐层 npy 逐字节对拍。

三个开关在**主机 engine=None** 下的确切含义（见 REPORT §1.1）
--------------------------------------------------------------
容器 `pack_v60` 带 `off_thr32`（f32 阈值），`_RefBackend.prepare()` 无条件优先用它
（`bin_net.py:1128-1133`）⇒

    「BIN_PACK」轴：`ps` = 用 f32 阈值（`x + t > 0`）；
                   `e1e2` = **只能**用 int16 `t_q`（`x_q + t_q > 0`，int8 网格）。
    但 `BIN_PACK=e1e2` 只在 `self.engine is not None` 时才生效
    （`bin_net.py:3116-3124`），主机 engine=None ⇒ **该环境变量是纯 no-op**。
    ⇒ 本脚本用 `BIN_THR_F32=0`（`bin_ref_numpy.py:237`）**等价模拟** e1e2 的阈值域，
      并用一次 `--mode pack-ab` 实测证明 `BIN_PACK=ps` 与 `=e1e2` 在主机上逐字节相同。

用法
----
    python3 diverge_probe.py --mode ab     --tag p0067          # ≈9 次整网
    python3 diverge_probe.py --mode dump   --tag p0067
    python3 diverge_probe.py --mode pack-ab --tag p0067
    python3 diverge_probe.py --mode golden --tag p0067          # 粗查：8 组合各自 vs 板端
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
PN = os.path.abspath(os.path.join(HERE, os.pardir))           # runs/opt_round1/ps_numeric
ROOT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir, os.pardir, os.pardir))
PSP = os.path.join(ROOT, "pynq_specialized_code", "ps", "pynq")
if PSP not in sys.path:
    sys.path.insert(0, PSP)

PACK = os.path.join(PN, "pack_v60")
FROZEN = os.path.join(ROOT, "runs", "frozen256")
BOARD = "/tmp/t165_logs/board_out"
HOSTREF = os.path.join(PN, "host_int8_ref")

#: 板端 run100 的完整 RUNPOINT（与 host_int8_ref/make_host_ref.py 逐字一致）
ENV_FULL = {
    "BIN_PACK": "ps", "BIN_RTL_ROUND": "1", "BIN_CONV_OOB0": "1", "BV_FREE": "1",
    "BIN_THR_F32": "1", "BIN_LAZY_DEQ": "1", "BIN_CAT_FUSION": "1",
    "BIN_BILINEAR_C": "1", "BIN_PRECOMP": "1", "Q8CACHE": "1", "BIN_F32DIRECT": "1",
    "BIN_CAT_ECA_FUSE": "1", "BIN_EPI_NOINV": "1", "BIN_LAZY_SLOTS": "24",
}
#: 三个单变量轴（其余全 = ENV_FULL）
AXES = {
    "pack": ("BIN_THR_F32", {"ps": "1", "e1e2": "0"}),   # e1e2 ⇔ int16 t_q 域
    "round": ("BIN_RTL_ROUND", {"1": "1", "0": "0"}),
    "oob0": ("BIN_CONV_OOB0", {"1": "1", "0": "0"}),
}
PURGE = ("BIN_PACK", "BIN_RTL_ROUND", "BIN_CONV_OOB0", "BIN_THR_F32", "BV_FREE")


# ---------------------------------------------------------------- 基础口径
def sha1b(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def u8(x):
    """与 board_verify_numfix.u8() 逐字一致。"""
    return np.rint(np.clip(np.asarray(x, np.float64), 0.0, 1.0) * 255.0).astype(np.uint8)


def load_input(path):
    a = np.load(path)
    if a.ndim == 2:
        a = np.stack([a, a, a])
    if a.shape[0] in (1, 3, 4):
        a = a[:3]
    else:
        a = a.transpose(2, 0, 1)[:3]
    a = np.asarray(a, np.float64)
    if a.max() > 1.5:
        a = a / 255.0
    return np.ascontiguousarray(a, np.float32)


def load_board(tag):
    """板端 u8：HWC `(256,256,3)` uint8 ⇒ 转成 (3,256,256) 再与主机同形比较。"""
    p = os.path.join(BOARD, "%s_out1_u8.npy" % tag)
    b = np.load(p)
    assert b.ndim == 3
    if b.shape[0] in (3, 4) and b.shape[2] not in (3, 4):
        b = b[:3]
    else:
        b = np.ascontiguousarray(b.transpose(2, 0, 1))
    return b.astype(np.uint8)


def load_board_f32(tag):
    return np.load(os.path.join(BOARD, "%s_out1_f32.npy" % tag)).astype(np.float64)


def m_board(out1, board_u8):
    """主机 out1 → u8 与板端 u8 逐像素比（口径与题面表格一致）。"""
    a = u8(out1).astype(np.int16)
    b = board_u8.astype(np.int16)
    d = a - b
    return dict(ndiff=int((d != 0).sum()), ntot=int(d.size),
                frac=float((d != 0).mean()),
                max_abs=int(np.abs(d).max()),
                mean_signed=float(d.mean()), mean_abs=float(np.abs(d).mean()),
                sha1_u8=sha1b(u8(out1)))


def m_golden(out1, golden):
    ui, ug = u8(out1), u8(golden)
    d = ui.astype(np.int16) - ug.astype(np.int16)
    of, gf = np.asarray(out1, np.float64), np.asarray(golden, np.float64)
    mse = float(np.mean(d.astype(np.float64) ** 2))
    return dict(mean_abs=float(np.abs(d).mean()), max_abs=int(np.abs(d).max()),
                psnr=float(10 * np.log10(255.0 ** 2 / max(mse, 1e-12))),
                corr=float(np.corrcoef(of.ravel(), gf.ravel())[0, 1]),
                dc=float(of.mean() - gf.mean()),
                sigma=float(of.std() / max(gf.std(), 1e-12)))


# ---------------------------------------------------------------- 逐层探针
STATE = {"name": None, "rec": {}, "keep": False}


def install_probes():
    """monkeypatch：conv ⇒ 记 int8 y_q（反量化前）；其他 op ⇒ 记 f32 out。"""
    import bin_net as BN

    orig_finish = BN._RefBackend.finish
    orig_conv = BN.conv_rtl
    orig_ps = BN.BinNet._exec_ps

    def finish(self, task):
        STATE["name"] = task.op.name
        STATE["fs"] = int(task.layer.d.get("act_fs_out", -1))
        return orig_finish(self, task)

    def conv(*a, **kw):
        y_q, r = orig_conv(*a, **kw)
        nm = STATE["name"]
        if nm is not None:
            yq = np.ascontiguousarray(y_q, np.int8)
            rec = STATE["rec"].setdefault(nm, {})
            rec.update(kind="conv", i8=yq, shape=tuple(yq.shape),
                       sha1=sha1b(yq), min=int(yq.min()), max=int(yq.max()),
                       mean=float(yq.mean()), std=float(yq.std()),
                       n_lo=int((yq == -128).sum()), n_hi=int((yq == 127).sum()),
                       n_zero=int((yq == 0).sum()),
                       fs_out=int(STATE.get("fs", -1)))
            sh = int(kw.get("shift") if "shift" in kw else a[6])
            pre = r >> sh
            rec["n_clip"] = int(((pre > 127) | (pre < -128)).sum())
            rec["max_pre"] = int(pre.max())
            rec["min_pre"] = int(pre.min())
        return y_q, r

    def exec_ps(self, op):
        y = orig_ps(self, op)
        if isinstance(y, np.ndarray) and y.dtype != object:
            rec = STATE["rec"].setdefault(op.name, {})
            arr = np.ascontiguousarray(y)
            rec.update(kind=op.kind, shape=tuple(arr.shape),
                       sha1=sha1b(arr), min=float(arr.min()), max=float(arr.max()),
                       mean=float(arr.mean()), std=float(arr.std()))
        return y

    BN._RefBackend.finish = finish
    BN.conv_rtl = conv
    BN.BinNet._exec_ps = exec_ps
    return BN


def set_env(over):
    for k in PURGE:
        os.environ.pop(k, None)
    os.environ.update(ENV_FULL)
    os.environ.update(over)


def run_cfg(img, env_over, tag):
    set_env(env_over)
    import bin_net as BN
    STATE["name"] = None
    STATE["rec"] = {}
    x = np.asarray(img, np.float32)
    m = BN.BinNet(PACK, engine=None, H=int(x.shape[1]), W=int(x.shape[2]),
                  verbose=False)
    t0 = time.time()
    outs, st = m.run(x, pipeline=True, nbuf=2)
    dt = time.time() - t0
    o1 = np.ascontiguousarray(outs["out1"], np.float32)
    return dict(tag=tag, env={k: os.environ.get(k, "") for k in PURGE},
                out1=o1, layers=STATE["rec"], sec=dt)


# ---------------------------------------------------------------- 主流程
def mode_ab(a, img, board, golden):
    combos = []
    for pk in ("ps", "e1e2"):
        for rd in ("1", "0"):
            for oo in ("1", "0"):
                combos.append(dict(pack=pk, round=rd, oob0=oo))
    rows = []
    for c in combos:
        over = {}
        for ax, key in (("pack", c["pack"]), ("round", c["round"]), ("oob0", c["oob0"])):
            var, table = AXES[ax]
            over[var] = table[key]
        tag = "pk%s_rd%s_ob%s" % (c["pack"], c["round"], c["oob0"])
        t0 = time.time()
        r = run_cfg(img, over, tag)
        mb = m_board(r["out1"], board)
        mg = m_golden(r["out1"], golden)
        row = dict(tag=tag, combo=c, env=over, sec=r["sec"],
                   board=mb, golden=mg,
                   nlayers=len(r["layers"]),
                   layer_sha1={k: v["sha1"] for k, v in r["layers"].items()},
                   layer_stats={k: {kk: vv for kk, vv in v.items()
                                    if kk not in ("i8", "sha1", "kind", "shape")}
                                for k, v in r["layers"].items()})
        rows.append(row)
        np.save(os.path.join(HERE, "ab", "%s_out1_f32.npy" % tag), r["out1"])
        np.save(os.path.join(HERE, "ab", "%s_out1_u8.npy" % tag), u8(r["out1"]))
        print("[ab] %-16s %5.1fs | vs板端 不同=%6d max=%2d 均值=%+0.4f mean|Δ|=%0.4f "
              "| vsGolden mean|Δu8|=%5.2f PSNR=%5.2f corr=%+0.3f"
              % (tag, r["sec"], mb["ndiff"], mb["max_abs"], mb["mean_signed"],
                 mb["mean_abs"], mg["mean_abs"], mg["psnr"], mg["corr"]))

    # 第一个"同一轴两根柱子之间"发散的层（其余两轴固定）
    div = {}
    order = [k for k in rows[0]["layer_sha1"]]
    for ax, (var, table) in AXES.items():
        keys = [("pk", "rd", "ob")]  # 占位
        axis_of = {"pack": ("pk", 0), "round": ("rd", 1), "oob0": ("ob", 2)}
        tagk, idx = axis_of[ax]
        pairs = []
        for r in rows:
            parts = r["tag"][2:].split("_")
            pairs.append((parts, r))
        # 固定其余两轴，找该轴的两个值
        seen = {}
        for parts, r in pairs:
            rest = tuple(p for i, p in enumerate(parts) if i != idx)
            seen.setdefault(rest, {})[parts[idx]] = r
        ax_rows = []
        for rest, d in seen.items():
            if len(d) < 2:
                continue
            ra, rb = list(d.values())
            first = None
            for nm in order:
                if ra["layer_sha1"].get(nm) != rb["layer_sha1"].get(nm):
                    first = nm
                    break
            ax_rows.append(dict(rest=rest, a=ra["tag"], b=rb["tag"], first=first))
        div[ax] = ax_rows

    res = dict(tag=a.tag, input=a.input or os.path.join(FROZEN, "%s_crop256.npy" % a.tag),
               env_full=ENV_FULL, combo_rows=rows, divergence=div,
               pack_note=("主机 engine=None 时 BIN_PACK 不生效（bin_net.py:3116），"
                          "e1e2 轴用 BIN_THR_F32=0 模拟 int16 阈值域（bin_ref_numpy.py:237）"))
    p = os.path.join(HERE, "ab", "ab_%s.json" % a.tag)
    json.dump(res, open(p, "w"), indent=1, ensure_ascii=False)
    print("[ab] → %s" % p)
    return res


def _layer_fname(i, nm):
    return "%03d__%s.npy" % (i, nm.replace("/", "_"))


def mode_dump(a, img, board):
    over = {}
    if a.thr is not None:
        over["BIN_THR_F32"] = a.thr
    if a.round is not None:
        over["BIN_RTL_ROUND"] = a.round
    if a.oob0 is not None:
        over["BIN_CONV_OOB0"] = a.oob0
    outdir = a.outdir or os.path.join(HERE, "host")
    os.makedirs(outdir, exist_ok=True)
    r = run_cfg(img, over, a.tag)
    idx = {}
    tot = 0
    for i, (nm, v) in enumerate(r["layers"].items()):
        fn = _layer_fname(i, nm)
        arr = v.get("i8")
        if arr is None:
            continue       # 非 conv 的 f32 层单独存
        np.save(os.path.join(outdir, fn), arr)
        tot += int(arr.nbytes)
        idx[nm] = dict(i=i, kind=v["kind"], shape=list(v["shape"]),
                       dtype="int8", file=fn, sha1=v["sha1"],
                       fs_out=int(v.get("fs_out", -1)),
                       stats={k: v[k] for k in ("min", "max", "mean", "std",
                                                "n_lo", "n_hi", "n_zero", "n_clip",
                                                "max_pre", "min_pre")})
    mb = m_board(r["out1"], board)
    meta = dict(tag=a.tag, env={k: os.environ.get(k, "") for k in PURGE},
                ndump=len(idx), bytes=tot, out1=mb,
                sha1_out1_f32=sha1b(np.asarray(r["out1"], np.float32)),
                layers=idx)
    json.dump(meta, open(os.path.join(outdir, "layers_index.json"), "w"),
              indent=1, ensure_ascii=False)
    np.save(os.path.join(outdir, "out1_f32.npy"), np.asarray(r["out1"], np.float32))
    np.save(os.path.join(outdir, "out1_u8.npy"), u8(r["out1"]))
    print("[dump] %d 层 %0.1f MB → %s" % (len(idx), tot / 1e6, outdir))
    print("[dump] vs 板端 u8: 不同=%d max=%d mean=%+0.4f" %
          (mb["ndiff"], mb["max_abs"], mb["mean_signed"]))
    return meta


def mode_pack_ab(a, img, board):
    """实测 BIN_PACK=ps vs e1e2 在主机 engine=None 上是否逐字节相同。"""
    out = {}
    for pk in ("ps", "e1e2"):
        env = dict(ENV_FULL)
        env["BIN_PACK"] = pk
        r = run_cfg(img, env, "pack_" + pk)
        out[pk] = dict(sha1_out1_u8=sha1b(u8(r["out1"])),
                       sha1_out1_f32=sha1b(np.asarray(r["out1"], np.float32)),
                       board=m_board(r["out1"], board), sec=r["sec"])
        print("[pack-ab] BIN_PACK=%-5s sha1_u8=%s  不同(vs板端)=%d"
              % (pk, out[pk]["sha1_out1_u8"][:12], out[pk]["board"]["ndiff"]))
    out["identical"] = out["ps"]["sha1_out1_u8"] == out["e1e2"]["sha1_out1_u8"]
    print("[pack-ab] 两者逐字节相同 = %s ⇒ 主机上 BIN_PACK 是 no-op"
          % out["identical"])
    json.dump(out, open(os.path.join(HERE, "ab", "pack_ab_%s.json" % a.tag), "w"),
              indent=1)
    return out



# ---------------------------------------------------------------- ③ 敏感度实验
def mode_margin(a, img):
    """逐层"二值化判据余量"：|x·2^fs_in + t·2^fs_in| 的分布（单位 = int8 LSB）。

    余量 ≈ 0 的元素 = **一次 1-ULP 的上游差异就能翻 bit** 的位置；它把
    "某层有几千个判据贴边" 与 "整网对微小差异的放大"直接连起来。
    """
    import bin_net as BN
    import bin_ref_numpy as RB
    set_env({})
    orig = BN._RefBackend.prepare
    rows = {}

    def prepare(self, op):
        task = orig(self, op)
        dep = op.deps[0]
        x = np.asarray(dep.val, np.float32)
        L = op.layer
        fs = int(L.d["act_fs_in"])
        t32 = self.net.blob.thr_f32(L.d)
        rec = rows.setdefault(op.name, dict(kind="conv", shape=list(x.shape),
                                            fs_in=fs, n=int(x.size),
                                            t32_none=(t32 is None)))
        if t32 is not None:
            mg = np.abs(x + np.asarray(t32, np.float32).reshape(-1, 1, 1)) * (2.0 ** fs)
            rec.update(m_lt_1e6=int((mg < 1e-6).sum()), m_lt_1e3=int((mg < 1e-3).sum()),
                       m_lt_1e2=int((mg < 1e-2).sum()), m_lt_01=int((mg < 0.1).sum()),
                       m_min=float(mg.min()))
        rec.update(n_x0=int((x == 0).sum()), std=float(x.std()),
                   absmax=float(np.abs(x).max()))
        return task

    BN._RefBackend.prepare = prepare
    try:
        r = run_cfg(img, {}, a.tag)
    finally:
        BN._RefBackend.prepare = BN._RefBackend.prepare if False else orig
    out = dict(tag=a.tag, layers=rows, out1=r["out1"].tolist()[:0])
    json.dump(out, open(os.path.join(HERE, "ab", "margin_%s.json" % a.tag), "w"),
              indent=1, ensure_ascii=False)
    top = sorted(((v.get("m_lt_01", 0) / max(v["n"], 1), k, v) for k, v in rows.items()),
                 reverse=True)[:10]
    print("[margin] %d 层；贴边率(余量<0.1 LSB) 最高的 10 层：" % len(rows))
    for fr, k, v in top:
        print("   %-58s %6.2f%%  (x==0 %.1f%%, 余量<1e-3 LSB %d)"
              % (k[:58], 100 * fr, 100 * v["n_x0"] / max(v["n"], 1), v.get("m_lt_1e3", -1)))
    return out


def _layer_diff(sa, sb):
    """两个 sha1 表 → (第一处不同层, [(层名, 不同整形元素数?)])。不同层只报名字。"""
    first = None
    for nm in sa:
        if sa.get(nm) != sb.get(nm):
            first = nm
            break
    return first


def mode_probe_impl(a, img, board, golden):
    """④ 敏感度：把**浮点路径**换一个等价位实现（末位差异）/ 给输入加 1 ULP，
    看 p0067 的 out1 会不会崩到"和板端同一量级"。这是"级联放大"假设的直接检验。
    """
    import bin_net as BN
    import bin_ref_numpy as RB
    variants = [("base", None)]
    variants.append(("convpf_c", "rb.CONVPF_C=True"))
    variants.append(("interp_np", "bn.bilinear_f32=rb._bilinear_f32_numpy"))
    variants.append(("conv_ref", "bn.conv2d_f32_np=bn.conv2d_f32_ref"))
    variants.append(("ulp_in", "img_nextafter"))
    base = None
    res = {}
    for name, how in variants:
        rb_C = RB.CONVPF_C
        bn_bil = BN.bilinear_f32
        bn_conv = BN.conv2d_f32_np
        x = img
        try:
            if how == "rb.CONVPF_C=True":
                RB.CONVPF_C = True
            elif how == "bn.bilinear_f32=rb._bilinear_f32_numpy":
                BN.bilinear_f32 = RB._bilinear_f32_numpy
            elif how == "bn.conv2d_f32_np=bn.conv2d_f32_ref":
                BN.conv2d_f32_np = BN.conv2d_f32_ref
            elif how == "img_nextafter":
                x = np.nextafter(np.asarray(img, np.float32),
                                 np.float32(np.inf)).astype(np.float32)
            r = run_cfg(x, {}, name)
        finally:
            RB.CONVPF_C = rb_C
            BN.bilinear_f32 = bn_bil
            BN.conv2d_f32_np = bn_conv
        d = None
        if base is None:
            base = r
        else:
            du = u8(r["out1"]).astype(np.int16) - u8(base["out1"]).astype(np.int16)
            d = dict(ndiff=int((du != 0).sum()), max_abs=int(np.abs(du).max()),
                     mean_signed=float(du.mean()), mean_abs=float(np.abs(du).mean()))
        mb = m_board(r["out1"], board)
        mg = m_golden(r["out1"], golden)
        first = None if base is r else _layer_diff(
            {k: v["sha1"] for k, v in base["layers"].items()},
            {k: v["sha1"] for k, v in r["layers"].items()})
        res[name] = dict(how=how or "canonical", sec=r["sec"], board=mb, golden=mg,
                         diff_vs_base=d, first_div_layer=first)
        print("[impl] %-10s vsBase 不同=%s max=%s | vs板端 不同=%6d max=%2d 均值=%+0.4f "
              "| vsGolden mean|Δu8|=%5.2f | 首个发散层=%s"
              % (name, d and d["ndiff"], d and d["max_abs"], mb["ndiff"], mb["max_abs"],
                 mb["mean_signed"], mg["mean_abs"], first))
    json.dump(res, open(os.path.join(HERE, "ab", "impl_%s.json" % a.tag), "w"),
              indent=1, ensure_ascii=False)
    return res



def mode_flipchain(a, img, board, golden):
    """⑤ 级联剖面：给输入加 `--eps` 个 ULP，逐层统计 int8 翻转元素数。

    这是"**第一个发散的层**"在主机侧的对照物：它说明一次 1-ULP 的浮点差
    从哪一层开始变成 int8 位差、以及每层放大多少倍。板端逐层 npy 到手后，
    同一张表可以直接与 board vs host 的逐层翻转数并排。
    """
    eps = int(a.eps or 1)
    base = run_cfg(img, {}, a.tag + "_base")
    x = np.asarray(img, np.float32).copy()
    for _ in range(eps):
        x = np.nextafter(x, np.float32(np.inf))
    var = run_cfg(x.astype(np.float32), {}, a.tag + "_ulp%d" % eps)
    prof = []
    first = None
    for nm, v in base["layers"].items():
        w = var["layers"].get(nm)
        if w is None or "i8" not in v or "i8" not in w:
            continue
        if v["i8"].shape != w["i8"].shape:
            continue
        n = int((v["i8"] != w["i8"]).sum())
        prof.append(dict(i=len(prof), name=nm, shape=list(v["i8"].shape), flips=n,
                         n=int(v["i8"].size), frac=n / max(v["i8"].size, 1)))
        if n and first is None:
            first = nm
    du = u8(var["out1"]).astype(np.int16) - u8(base["out1"]).astype(np.int16)
    out = dict(tag=a.tag, eps_ulp=eps, first_flip_layer=first,
               ndiff_out1=int((du != 0).sum()), max_abs=int(np.abs(du).max()),
               mean_signed=float(du.mean()),
               board_ndiff=m_board(base["out1"], board)["ndiff"],
               n_layers=len(prof), profile=prof)
    json.dump(out, open(os.path.join(HERE, "ab", "flipchain_%s_ulp%d.json"
                                     % (a.tag, eps)), "w"), indent=1, ensure_ascii=False)
    print("[flipchain %s ulp=%d] out1 不同=%d max=%d | 第一个 int8 翻转层=%s"
          % (a.tag, eps, out["ndiff_out1"], out["max_abs"], first))
    for r in prof[:14]:
        print("   %3d %-58s %7d/%7d (%5.2f%%)"
              % (r["i"], r["name"][:58], r["flips"], r["n"], 100 * r["frac"]))
    return out



def mode_fpdump(a, img, board, golden):
    """⑥ 把 4 个 **f32 卷积层**的输出落盘（板端要判断"浮点路径是否已经不同"）。

    动机（见 REPORT §4）：p0067 的整网对 1 ULP 输入差就能放大成 6262 个 out1
    像素差（p0000 是 **0**），所以板端与主机的差异极可能起于**第一个二值化之前**
    的一次亚 LSB 浮点差。要证实/否证它，板端必须先把 `encoder.conv_first.0`
    的 f32 输出与主机比 —— 它是最上游、也是唯一一个"输入完全相同却仍可能不同"
    的 f32 张量（其后都是 int8 主导）。
    """
    import bin_net as BN
    outdir = a.outdir or os.path.join(HERE, "host")
    os.makedirs(outdir, exist_ok=True)
    orig = BN.BinNet._fp_conv
    rec = {}

    def fp_conv(self, x, name, relu=False, stride=1, pad=0):
        y = orig(self, x, name, relu=relu, stride=stride, pad=pad)
        rec[name] = np.ascontiguousarray(np.asarray(y, np.float32))
        return y

    BN.BinNet._fp_conv = fp_conv
    try:
        r = run_cfg(img, {}, a.tag)
    finally:
        BN.BinNet._fp_conv = orig
    meta = {}
    for nm, y in rec.items():
        fn = "fp__%s.npy" % nm.replace("/", "_")
        np.save(os.path.join(outdir, fn), y)
        meta[nm] = dict(shape=list(y.shape), dtype="float32", file=fn,
                        bytes=int(y.nbytes), sha1=sha1b(y), min=float(y.min()),
                        max=float(y.max()), mean=float(y.mean()), std=float(y.std()))
        print("[fpdump] %-28s %r %.1f MB sha1=%s" %
              (nm, y.shape, y.nbytes / 1e6, meta[nm]["sha1"][:12]))
    json.dump(dict(tag=a.tag, layers=meta),
              open(os.path.join(outdir, "fp_layers_index.json"), "w"),
              indent=1, ensure_ascii=False)
    print("[fpdump] vs 板端 u8: 不同=%d" % m_board(r["out1"], board)["ndiff"])
    return meta


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="ab",
                    choices=("ab", "dump", "pack-ab", "margin", "impl", "flipchain",
                             "fpdump"))
    ap.add_argument("--tag", default="p0067")
    ap.add_argument("--input", default=None)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--thr", default=None, help="BIN_THR_F32 (1=f32 阈值, 0=int16 t_q)")
    ap.add_argument("--round", default=None)
    ap.add_argument("--oob0", default=None)
    ap.add_argument("--eps", type=int, default=1, help="flipchain: 输入加几个 ULP")
    a = ap.parse_args(argv)

    img = load_input(a.input or os.path.join(FROZEN, "%s_crop256.npy" % a.tag))
    board = load_board(a.tag)
    golden = np.load(os.path.join(FROZEN, "golden", "%s_out1_fp.npy" % a.tag))
    print("[probe] 输入 %r range [%.3f, %.3f] mean %.4f | 板端 u8 %r"
          % (img.shape, img.min(), img.max(), img.mean(), board.shape))

    install_probes()
    if a.mode == "ab":
        mode_ab(a, img, board, golden)
    elif a.mode == "pack-ab":
        mode_pack_ab(a, img, board)
    elif a.mode == "margin":
        mode_margin(a, img)
    elif a.mode == "impl":
        mode_probe_impl(a, img, board, golden)
    elif a.mode == "flipchain":
        mode_flipchain(a, img, board, golden)
    elif a.mode == "fpdump":
        mode_fpdump(a, img, board, golden)
    else:
        mode_dump(a, img, board)
    return 0


if __name__ == "__main__":
    sys.exit(main())
