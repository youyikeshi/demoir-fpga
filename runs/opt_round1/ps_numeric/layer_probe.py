#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""layer_probe.py —— PS 侧整网数值对拍台架（浮点 golden vs 板端同款 int8 路径）

## 它做三件事

1. **逐层对拍**：在同一张 256² 输入上同时跑
     · 浮点参考 = `ps/tools/bin_fp_ref.py`（float64，语义照 vendor/PDNet/model/binary_esdnet.py）
     · int8 路径 = `ps/pynq/bin_net.py` 的 `BinNet(engine=None)` → `_RefBackend`
       （= 板端算法：阈值二值化 + `bin_ref_numpy.conv_rtl` 的 XNOR/popcount + 反量化；
        无 DMA。已在 256² 实测与板端 out1 **逐位相同**，见 REPORT §1）
   逐层算 rel_rms / Pearson 相关，报出**第一处**超过阈值的层。
2. **二值化符号翻错率**（本任务的根因指标）：对每一层，把"引擎判出的 bit"与
   "浮点参考的 `sign(x+t)`"逐元素比，算**翻错的抽头比例**。
3. **验收**：整网 out1 在**同一 u8 映射**（`round(255·clip(x,0,1))`）下的
   `max|Δ| / mean|Δ| / rel_rms / corr / PSNR`，可 `--json` 落盘。

## 用法

    # 基线（部署中的容器）= 旧的 t_q 阈值 + 截断右移
    python3 layer_probe.py --image <crop256.npy> --fp-ref <pNNN_out1.npy> \
        --pack ../../pynq_specialized_code/deploy/bin_weights_new --tag base

    # 修复后（容器带 off_thr32 = 未量化阈值 + gamma_q 带四舍五入偏移；OOB=0）
    BIN_CONV_OOB0=1 python3 layer_probe.py --image ... --pack pack_fix --tag fix

    # 一张图跑"前/后"对比 + 变异（关掉单点修复）
    python3 layer_probe.py --image ... --compare --mutate

`--compare` 用固定顺序跑 4 档：base(旧容器) / +thr+round(新容器) / +oob / 变异(新容器但
`BIN_THR_F32=0`)，并把结果打成一张表。

环境开关（都默认关 ⇒ 行为与改动前逐字节相同）：
    BIN_CONV_OOB0=1   空间越界抽头"不计贡献"（= PyTorch 补 0 语义；RTL 需同步打补丁）
    BIN_THR_F32=0     忽略容器的 off_thr32，退回 int16 t_q（变异测试用）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir, os.pardir))
PS = os.path.join(ROOT, "pynq_specialized_code", "ps")
for p in (os.path.join(PS, "pynq"), os.path.join(PS, "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import bin_fp_ref as FP                     # noqa: E402
import bin_net as BN                        # noqa: E402


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def rel_rms(a, b):
    a = np.asarray(a, np.float64).ravel()
    b = np.asarray(b, np.float64).ravel()
    return float(np.sqrt(np.mean((a - b) ** 2)) / max(np.sqrt(np.mean(b * b)), 1e-12))


def corr(a, b):
    a = np.asarray(a, np.float64).ravel()
    b = np.asarray(b, np.float64).ravel()
    a = a - a.mean()
    b = b - b.mean()
    d = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 0 else 0.0


def u8(x):
    """与 PC/板端**共用**的显示映射（tools/prepare_demo_pairs.py 的 to_uint8 同式）。"""
    return np.rint(np.clip(np.asarray(x, np.float64), 0.0, 1.0) * 255.0).astype(np.int16)


def psnr_u8(a, b):
    d = (np.asarray(a, np.float64) - np.asarray(b, np.float64))
    m = float(np.mean(d * d))
    return 99.0 if m <= 0 else float(10.0 * np.log10(255.0 * 255.0 / m))


# ---------------------------------------------------------------------------
# 1) 浮点 golden：抓逐层张量（原版 collect 只存 p99.9/max/rms）
# ---------------------------------------------------------------------------
class CaptureNet(FP.Net):
    """在 `_record()` 处留下整张张量；另把 fp 卷积/插值/注意力/SAM 的输出也对齐记下来。

    记录的 key **故意与 bin_net 的 op 名一致**（`<module>.conv` / `<p>.rdb:residual` /
    `<q>:weighted_sum` / `<p>:pixel_shuffle(2)` …），这样两侧可以直接按名字配对。
    """

    def __init__(self, sd):
        FP.Net.__init__(self, sd, collect={})      # collect 非 None 才会触发 _record
        self.tens = {}

    def _rec(self, name, x):
        self.tens.setdefault(name, {})["out"] = np.asarray(x, np.float64).copy()
        return x

    def _record(self, name, x, tag):
        self.tens.setdefault(name, {})[tag] = np.asarray(x, np.float64).copy()

    # ---- conv ----
    def conv_fp(self, x, name, stride=1, pad=0, dil=1):
        y = FP.Net.conv_fp(self, x, name, stride, pad, dil)
        return self._rec(name, y)

    # ---- 块（与 bin_fp_ref 同序，只多记录）----
    def rdb(self, x, p):
        y = FP.Net.rdb(self, x, p)
        return self._rec("%s.rdb:residual" % p, y)

    def eca(self, x, p):
        w = FP.Net.eca(self, x, p)
        return self._rec(p + ".fusion", w)

    def sam(self, x, p, inter):
        C, H, W = x.shape
        q = p + ".sam_blocks.0"
        x2 = FP.interpolate(x, scale=0.5)
        x4 = FP.interpolate(x, scale=0.25)
        self._rec(q + ":x2", x2)
        self._rec(q + ":x4", x4)
        y0 = self.ghost(x, q + ".b0", inter, residual=False)
        y2 = FP.interpolate(self.ghost(x2, q + ".b2", inter, residual=False), size=(H, W))
        y4 = FP.interpolate(self.ghost(x4, q + ".b4", inter, residual=False), size=(H, W))
        self._rec(q + ":b2_up", y2)
        self._rec(q + ":b4_up", y4)
        wgt = self.eca(np.concatenate([y0, y2, y4], 0), q + ".fusion")
        w0, w2, w4 = np.split(wgt, 3)
        y = x + (w0.reshape(-1, 1, 1) * y0 + w2.reshape(-1, 1, 1) * y2
                 + w4.reshape(-1, 1, 1) * y4)
        return self._rec(q + ":weighted_sum", y)

    def decoder_level(self, x, p, feat=True):
        x = self.rdb(x, p)
        x = self.sam(x, p, 96)
        out = FP.pixel_shuffle(self.conv_fp(x, p + ".conv", 1, 1), 2)
        self._rec(p + ":pixel_shuffle(2)", out)
        if feat:
            return out, self._rec(p + ":feat_up", FP.interpolate(x, scale=2.0))
        return out                      # 与 bin_fp_ref.decoder_level 一致（非 feat 只返回 out）

    def forward(self, x):
        x = FP.pixel_unshuffle(x, 2)
        self._rec("input:pixel_unshuffle(2)", x)
        y = FP.relu(self.conv_fp(x, "encoder.conv_first.0", 1, 2))
        self._rec("encoder.conv_first.0", y)
        y1, d1 = self._encoder_level(y, "encoder.encoder_1", 1)
        y2, d2 = self._encoder_level(d1, "encoder.encoder_2", 2)
        y3 = self._encoder_level(d2, "encoder.encoder_3", 3)[0]
        x3 = self.boundary(y3, "decoder.preconv_3")
        out3, f3 = self.decoder_level(x3, "decoder.decoder_3")
        x2 = self.boundary(np.concatenate([y2, f3], 0), "decoder.preconv_2")
        out2, f2 = self.decoder_level(x2, "decoder.decoder_2")
        x1 = self.boundary(np.concatenate([y1, f2], 0), "decoder.preconv_1")
        out1 = self.decoder_level(x1, "decoder.decoder_1", feat=False)
        return out1, out2, out3


def run_float(ckpt, img):
    from torchless_ckpt import load_ckpt
    sd = load_ckpt(ckpt)["state_dict"]
    net = CaptureNet(sd)
    t0 = time.time()
    o = net.forward(np.asarray(img, np.float64))
    dt = time.time() - t0
    for nm, v in zip(("out1", "out2", "out3"), o):
        net.tens.setdefault("NET:" + nm, {})["out"] = np.asarray(v, np.float64).copy()
    # 逐层阈值 t（浮点）+ t_q（容器）另算，见 flip_diag()
    net.sd = sd
    return net, dt


# ---------------------------------------------------------------------------
# 2) int8 路径：抓逐层张量（monkeypatch 后端，不动数值）
# ---------------------------------------------------------------------------
def run_int8(pack, img, verbose=False):
    rec = {}
    orig_prepare = BN._RefBackend.prepare
    orig_finish = BN._RefBackend.finish
    orig_ps = BN.BinNet._exec_ps

    def prepare(self, op):
        task = orig_prepare(self, op)
        dep = op.deps[0]
        if isinstance(dep.val, BN._CatLazy):
            dep.val = dep.materialize()
        rec.setdefault(op.name, {})["in"] = np.asarray(dep.val, np.float64).copy()
        return task

    def finish(self, task):
        y, rs, te = orig_finish(self, task)
        rec.setdefault(task.op.name, {})["out"] = np.asarray(y, np.float64).copy()
        return y, rs, te

    def exec_ps(self, op):
        y = orig_ps(self, op)
        if op.kind != "cat":                     # cat 是纯拷贝，不产生数值差
            rec.setdefault(op.name, {})["out"] = np.asarray(y, np.float64).copy()
        return y

    BN._RefBackend.prepare, BN._RefBackend.finish = prepare, finish
    BN.BinNet._exec_ps = exec_ps
    try:
        net = BN.BinNet(pack, engine=None, H=int(img.shape[1]), W=int(img.shape[2]),
                        verbose=verbose)
        t0 = time.time()
        outs, st = net.run(np.asarray(img, np.float32), pipeline=False, nbuf=2)
        dt = time.time() - t0
        for nm in ("out1", "out2", "out3"):
            rec.setdefault("NET:" + nm, {})["out"] = \
                np.asarray(outs[nm], np.float64).copy()
    finally:
        BN._RefBackend.prepare, BN._RefBackend.finish = orig_prepare, orig_finish
        BN.BinNet._exec_ps = orig_ps
    return rec, dt, st


# ---------------------------------------------------------------------------
# 3) 逐层对拍
# ---------------------------------------------------------------------------
def compare(frec, irec, order, thr=0.02):
    rows = []
    for nm in order:
        f, i = frec.get(nm, {}), irec.get(nm, {})
        if "out" not in f or "out" not in i:
            continue
        fo, io = f["out"], i["out"]
        if tuple(np.shape(fo)) != tuple(np.shape(io)):
            rows.append(dict(name=nm, err="shape %r vs %r"
                                       % (np.shape(fo), np.shape(io))))
            continue
        r = dict(name=nm, out_rel=rel_rms(io, fo), out_corr=corr(io, fo),
                 ref_std=float(fo.std()), i8_std=float(io.std()),
                 ref_absmax=float(np.abs(fo).max()))
        if "in" in f and "in" in i and np.shape(f["in"]) == np.shape(i["in"]):
            r["in_rel"] = rel_rms(i["in"], f["in"])
            r["in_corr"] = corr(i["in"], f["in"])
        rows.append(r)
    first = None
    for r in rows:
        v = r.get("out_rel")
        if v is not None and v > thr:
            first = r
            break
    return rows, first


# ---------------------------------------------------------------------------
# 4) 根因指标：二值化"符号翻错率"
# ---------------------------------------------------------------------------
def flip_diag(pack, frec, order, fs_of, t_of, tq_of, names=None):
    """对每一层，用**浮点参考自己的输入** x 比三种判据：

        golden : x + t > 0                     （模型的 sign(x+t)）
        old    : sat8(rint(x·2^fs)) + t_q > 0  （部署中的 t_q 阈值判据）
        new    : x + t > 0                     （off_thr32 的全精度阈值判据）

    返回 [(层名, golden与old翻错率, golden与new翻错率, t_q==0 但 t>0 的通道占比)]。
    这就是"整网崩掉"的根因数字：本来只有 ReLU 零（x=0）的位置靠 t 的符号决定，
    t_q=0 时这些位置整片翻成 −1。
    """
    out = []
    for nm in (names or order):
        L = fs_of.get(nm)
        if L is None:
            continue
        f = frec.get(nm, {})
        if "in" not in f or nm not in t_of:
            continue
        x = f["in"]
        t = t_of[nm].reshape(-1, 1, 1)
        tq = tq_of[nm].reshape(-1, 1, 1)
        fs = int(L)
        g = (x + t) > 0
        o = (np.clip(np.rint(x * (1 << fs)), -128, 127).astype(np.int64) + tq) > 0
        n = (x + t) > 0                    # new 用 f32 阈值 ⇒ 与 golden 同判据
        f_old = float((o != g).mean())
        f_new = float((n != g).mean())
        ch_bad = int(np.sum((tq.reshape(-1) == 0) & (t.reshape(-1) > 0)))
        out.append((nm, f_old, f_new, ch_bad, int(tq.size),
                    float((x == 0).mean())))
    return out


# ---------------------------------------------------------------------------
# 5) 主流程
# ---------------------------------------------------------------------------
def load_img(a):
    if a.image:
        return FP.load_image(a.image)
    if a.image_png:
        return FP.load_image(a.image_png)
    return FP.synth_input(a.synth or 64, seed=a.seed)


def eval_net(fo, io):
    uf, ui = u8(fo), u8(io)
    d = np.abs(ui - uf)
    return dict(rel_rms=rel_rms(io, fo), corr=corr(io, fo),
                max_lsb=float(d.max()), mean_lsb=float(d.mean()),
                rmse_lsb=float(np.sqrt(np.mean((ui - uf).astype(np.float64) ** 2))),
                psnr_u8=psnr_u8(ui, uf),
                ref_mean=float(fo.mean()), i8_mean=float(io.mean()),
                ref_std=float(fo.std()), i8_std=float(io.std()),
                ref_min=float(fo.min()), ref_max=float(fo.max()),
                i8_min=float(io.min()), i8_max=float(io.max()))


def one_run(pack, img, ckpt, thr, do_flip=True, verbose=False):
    net, dt_f = run_float(ckpt, img)
    frec = net.tens
    irec, dt_i, _st = run_int8(pack, img, verbose=verbose)
    import bin_ref_numpy as RB
    blob = RB.Blob(pack)
    order = [L["name"] for L in blob.layers]
    rows, first = compare(frec, irec, order, thr=thr)
    net_sum = {}
    for nm in ("out1", "out2", "out3"):
        k = "NET:" + nm
        if k in frec and k in irec:
            net_sum[nm] = eval_net(frec[k]["out"], irec[k]["out"])
    flips = []
    if do_flip:
        fs_of = {L["name"]: L["act_fs_in"] for L in blob.layers}
        t_of = {k[:-2]: np.asarray(v, np.float64).reshape(-1)
                for k, v in net.sd.items() if k.endswith(".t")}
        tq_of = {L["name"]: blob.t_q(L).astype(np.int64) for L in blob.layers}
        flips = flip_diag(pack, frec, order, fs_of, t_of, tq_of)
    out1 = irec["NET:out1"]["out"] if "NET:out1" in irec else None
    ref1 = frec["NET:out1"]["out"] if "NET:out1" in frec else None
    return dict(float_s=dt_f, int8_s=dt_i, net=net_sum, first_blast=first,
                layers=rows, flips=flips, pack=os.path.abspath(pack),
                out1=out1, ref1=ref1)


def _fmt_net(tag, s):
    return ("  %-22s rel_rms=%.4f corr=%+.4f  max|Δu8|=%3.0f  mean|Δu8|=%5.2f  "
            "PSNR=%5.2f dB   mean %.3f→%.3f  std %.3f→%.3f"
            % (tag, s["rel_rms"], s["corr"], s["max_lsb"], s["mean_lsb"],
               s["psnr_u8"], s["ref_mean"], s["i8_mean"], s["ref_std"], s["i8_std"]))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default=os.path.join(
        ROOT, "pynq_specialized_code", "deploy", "bin_weights_new"))
    ap.add_argument("--pack-fix", default=os.path.join(HERE, "pack_fix"))
    ap.add_argument("--ckpt", default=os.path.join(
        ROOT, "runs", "bin_base", "net_checkpoints", "checkpoint_latest.tar"))
    ap.add_argument("--image", default=None)
    ap.add_argument("--image-png", default=None)
    ap.add_argument("--synth", type=int, default=0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--fp-ref", default=None, help="冻结的浮点 out1 .npy（交叉核对用）")
    ap.add_argument("--thr", type=float, default=0.02,
                    help="判'第一处爆炸'的 rel_rms 阈值")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--compare", action="store_true",
                    help="跑 base / +thr+round / +oob / 变异 四档并打表")
    ap.add_argument("--no-flip", action="store_true", help="跳过二值化翻错率诊断")
    ap.add_argument("--json", default=None)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--outdir", default=None, help="把 out1.npy 存到哪（目视图用）")
    ap.add_argument("--name", default=None, help="输出文件名前缀（默认取输入文件名）")
    a = ap.parse_args(argv)

    img = load_img(a)
    print("[probe] 输入 %r  range [%.4f, %.4f] mean %.4f"
          % (img.shape, img.min(), img.max(), img.mean()))

    results = {}
    order = ["base", "fix:thr+rnd", "fix:+oob0", "mut:thr=off"]
    seq = order if a.compare else [a.tag]
    for tag in seq:
        oob = tag.endswith("oob0")
        thr_off = tag.startswith("mut")
        old = tag.startswith("base")
        pack = a.pack if old else a.pack_fix
        if oob:
            os.environ["BIN_CONV_OOB0"] = "1"
        else:
            os.environ.pop("BIN_CONV_OOB0", None)
        if thr_off:
            os.environ["BIN_THR_F32"] = "0"
        else:
            os.environ.pop("BIN_THR_F32", None)
        r = one_run(pack, img, a.ckpt, a.thr, do_flip=not a.no_flip)
        results[tag] = r
        o = r["net"].get("out1")
        if o:
            print(_fmt_net(tag, o))
            if a.outdir:
                os.makedirs(a.outdir, exist_ok=True)
                nm = a.name or os.path.basename(
                    (a.image or a.image_png or ("synth%d" % a.synth))
                ).split("_")[0].split(".")[0]
                np.save(os.path.join(a.outdir, "out1_%s_%s.npy"
                                     % (nm, tag.replace(":", "_").replace("+", "p"))),
                        r["out1"])
        if r["layers"]:
            bad = [x for x in r["layers"] if x.get("out_rel") is not None]
            bad.sort(key=lambda x: -x["out_rel"])
            print("     最差 5 层：" + "  ".join("%s=%.3f" % (x["name"].split(".")[-2][:16],
                                                           x["out_rel"]) for x in bad[:5]))
            if r["first_blast"]:
                fb = r["first_blast"]
                print("     第一处 rel_rms>%.2f：%s (%.4f)" % (a.thr, fb["name"], fb["out_rel"]))
    os.environ.pop("BIN_CONV_OOB0", None)
    os.environ.pop("BIN_THR_F32", None)

    # 交叉核对：本次浮点前向 vs 冻结的 golden（必须是 0 差，否则 golden 版本不对）
    if a.fp_ref:
        ref = np.asarray(np.load(a.fp_ref), np.float64)
        for tag, r in results.items():
            if r.get("ref1") is not None:
                print("[probe] %-12s 本次浮点 out1 vs 冻结 %s : rel_rms=%.3e corr=%.6f"
                      % (tag, os.path.basename(a.fp_ref), rel_rms(r["ref1"], ref),
                         corr(r["ref1"], ref)))
                break

    # 翻错率汇总
    for tag, r in results.items():
        fl = r.get("flips") or []
        if not fl:
            continue
        fo = np.array([x[1] for x in fl])
        fn = np.array([x[2] for x in fl])
        cb = sum(x[3] for x in fl)
        ct = sum(x[4] for x in fl)
        z = np.array([x[5] for x in fl])
        print("[flip] %-14s 翻错率: t_q 判据 中位 %.2f%% / 最大 %.2f%%  |  "
              "f32 阈值 中位 %.2f%% / 最大 %.2f%%  |  t_q=0 但 t>0 的通道 %d/%d"
              % (tag, 100 * np.median(fo), 100 * fo.max(),
                 100 * np.median(fn), 100 * fn.max(), cb, ct))

    if a.json:
        dump = {}
        for tag, r in results.items():
            dump[tag] = {k: v for k, v in r.items() if k not in ("out1", "ref1")}
            if r.get("flips"):
                fo = [x[1] for x in r["flips"]]
                fn = [x[2] for x in r["flips"]]
                dump[tag]["flip_summary"] = dict(
                    old_median=float(np.median(fo)), old_max=float(np.max(fo)),
                    new_median=float(np.median(fn)), new_max=float(np.max(fn)),
                    ch_tq0_tpos=int(sum(x[3] for x in r["flips"])),
                    ch_total=int(sum(x[4] for x in r["flips"])))
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(dict(input=a.image or a.image_png or ("synth%d" % a.synth),
                           thr=a.thr, order=seq, results=dump), f, indent=1)
        print("[probe] → %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
