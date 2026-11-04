#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""board_verify_numfix.py —— **板端**上板验收：一次整网 vs PC 浮点 golden，打印修订门

在板子上跑（PS 侧，不用相机、不用 HDMI）：

    cd /home/xilinx/demoir/ps/pynq
    python3 board_verify_numfix.py \
        --pack   /home/xilinx/demoir/deploy/bin_weights_new \
        --input  /home/xilinx/demo/frozen256/<NNNN>_crop256.npy \
        --golden /home/xilinx/demo/frozen256/<NNNN>_out1.npy \
        --bit    /home/xilinx/demoir/design_1_v58.bit

`--golden` 是 PC 上用 `ps/tools/bin_fp_ref.py --out` 生成的 float64 out1（3,256,256）。
不给 `--golden` 时只打印统计，不判门。

判据（父代理 2026-11-04 修订，`runs/opt_round1/ps_numeric/REPORT.md` §5.5b）：
    A1' mean|Δu8| ≤ 16   A2' PSNR ≥ 20 dB   A3' |Δmean| ≤ 0.05
    A4' corr ≥ 0.80（仅当 ref_std ≥ 0.06）  A5' σ(i8)/σ(ref) ∈ [0.60, 1.40]

★ 必须在 `import bin_net` **之前**设好 `RUNPOINT`（有开关在模块导入时读走）——
  所以这个脚本自己做这件事，不 import demo_pipeline 的副作用版。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

GATE = dict(mean_lsb=16.0, psnr=20.0, dc=0.05, corr=0.80,
            corr_min_refstd=0.06, sigma_lo=0.60, sigma_hi=1.40)

#: 与 `demo_pipeline.RUNPOINT` 同源；`BIN_PACK=ps` 是**修复档**（吃 f32 阈值）
RUNPOINT = {"BIN_PACK": "ps", "BIN_LAZY_DEQ": "1", "BIN_CAT_FUSION": "1",
            "BIN_BILINEAR_C": "1", "BIN_PRECOMP": "1", "Q8CACHE": "1",
            "BIN_F32DIRECT": "1", "BIN_CAT_ECA_FUSE": "1", "BIN_EPI_NOINV": "1",
            "BIN_LAZY_SLOTS": "24"}


def u8(x):
    return np.rint(np.clip(np.asarray(x, np.float64), 0.0, 1.0) * 255.0).astype(np.int16)


def load_input(path):
    a = np.load(path)
    if a.ndim == 2:
        a = np.stack([a, a, a])
    if a.shape[0] in (1, 3, 4):
        a = a[:3]
    else:
        a = a.transpose(2, 0, 1)[:3]          # (H,W,3) → (3,H,W)
    a = np.asarray(a, np.float64)
    if a.max() > 1.5:
        a = a / 255.0
    return np.ascontiguousarray(a, np.float32)


def gate(n):
    sigma = n["i8_std"] / max(n["ref_std"], 1e-12)
    chk = [("A1' mean|Δu8| ≤ %g" % GATE["mean_lsb"],
            n["mean_lsb"] <= GATE["mean_lsb"], "%.2f" % n["mean_lsb"]),
           ("A2' PSNR ≥ %g dB" % GATE["psnr"],
            n["psnr_u8"] >= GATE["psnr"], "%.2f" % n["psnr_u8"]),
           ("A3' |Δmean| ≤ %g" % GATE["dc"],
            abs(n["i8_mean"] - n["ref_mean"]) <= GATE["dc"],
            "%+.3f" % (n["i8_mean"] - n["ref_mean"])),
           ("A4' corr ≥ %g" % GATE["corr"],
            (n["ref_std"] < GATE["corr_min_refstd"]) or n["corr"] >= GATE["corr"],
            "%+.3f%s" % (n["corr"], " (跳过:平坦)"
                         if n["ref_std"] < GATE["corr_min_refstd"] else "")),
           ("A5' σ比 ∈ [%.2f,%.2f]" % (GATE["sigma_lo"], GATE["sigma_hi"]),
            GATE["sigma_lo"] <= sigma <= GATE["sigma_hi"], "%.3f" % sigma)]
    return all(c[1] for c in chk), chk


#: **V-D 定稿判据（2026-11-04）**：三条只判"容器/配对/结构有没有搞坏"，
#: 全部按**多样本中位**（板端跑 5 张 frozen256，再 `--vd-aggregate` 汇总）。
#:  ① 中位 `mean|Δu8|` ≤ 16   ② 中位 PSNR ≥ 20 dB   ③ corr ≥ 0.80（仅 ref_std≥0.06 的样本）
#: **DC（`|Δmean| ≤ 0.05`）单独列"未解项"**：它检的是 227 层整流累加的既有债（v58 档也有），
#: 不删、不放宽、一直显示为红，直到 `q 域 +N`（v61）修好。
VD = dict(mean_lsb=16.0, psnr=20.0, corr=0.80, corr_min_refstd=0.06)
DC_TARGET = 0.05
#: 主机实测的 5 张 frozen256（v60 档）中位，抄在这里当"参考线"，仅用于提醒，不参与判分
VD_REF_5 = dict(mean_lsb=15.08, psnr=23.02, corr=0.927, dc_med=0.057, n_pass="2/5")


def vd_checks(n):
    """单样本的三条 V-D 判据。"""
    return [("① mean|Δu8| ≤ %g" % VD["mean_lsb"], n["mean_lsb"] <= VD["mean_lsb"],
             "%.2f" % n["mean_lsb"]),
            ("② PSNR ≥ %g dB" % VD["psnr"], n["psnr_u8"] >= VD["psnr"],
             "%.2f" % n["psnr_u8"]),
            ("③ corr ≥ %.2f（ref_std≥%.2f）" % (VD["corr"], VD["corr_min_refstd"]),
             n["ref_std"] < VD["corr_min_refstd"] or n["corr"] >= VD["corr"],
             "%+.3f%s" % (n["corr"], " (跳过:平坦)"
                          if n["ref_std"] < VD["corr_min_refstd"] else ""))]


def vd_aggregate(paths):
    """把多张单样本 JSON 汇总成 **V-D 判定**（中位）+ DC 未解项。零网络。"""
    import json as _j
    import numpy as _np
    rows = []
    for p in paths:
        d = _j.load(open(p))
        m = d.get("metrics") or d
        # 兼容两种落盘形状：每文件一个 metrics dict（本脚本写的），或一个 list
        rows.extend(m if isinstance(m, list) else [m])
    if not rows:
        print("[numfix] --vd-aggregate: 没有可用 JSON")
        return 2
    med = lambda k: float(_np.median([r[k] for r in rows]))
    ml, ps = med("mean_lsb"), med("psnr_u8")
    st = [r for r in rows if r["ref_std"] >= VD["corr_min_refstd"]]
    cr = float(_np.median([r["corr"] for r in st])) if st else None
    dm = float(_np.median([abs(r["i8_mean"] - r["ref_mean"]) for r in rows]))
    ok = (ml <= VD["mean_lsb"] and ps >= VD["psnr"]
          and (cr is None or cr >= VD["corr"]))
    print("[numfix] == V-D 判定（%d 张，中位）==" % len(rows))
    print("   %-30s %-5s %s" % ("① 中位 mean|Δu8| ≤ %g" % VD["mean_lsb"],
                                "OK" if ml <= VD["mean_lsb"] else "FAIL", "%.2f" % ml))
    print("   %-30s %-5s %s" % ("② 中位 PSNR ≥ %g dB" % VD["psnr"],
                                "OK" if ps >= VD["psnr"] else "FAIL", "%.2f" % ps))
    print("   %-30s %-5s %s" % ("③ 中位 corr ≥ %.2f" % VD["corr"],
                                "OK" if (cr is None or cr >= VD["corr"]) else "FAIL",
                                "%+.3f（%d/%d 张参与）" % (cr if cr is not None else 0,
                                                          len(st), len(rows))))
    print("[numfix] == DC（A3'，**未解项**，目标 ≤%.2f，不参与 V-D 通过/失败）==" % DC_TARGET)
    print("   中位 |Δmean| = %.3f  %s   ← %s"
          % (dm, "达标" if dm <= DC_TARGET else "**未达标（已知债，排 v61 的 q 域 +N）**",
             "主机 5 张参考线 %.3f" % VD_REF_5["dc_med"]))
    print("[numfix] ⇒ V-D: %s ｜ DC: %s"
          % ("PASS" if ok else "FAIL", "PASS" if dm <= DC_TARGET else "FAIL(未解)"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--input", default=None,
                    help="输入 crop256 .npy（--vd-aggregate 模式下可省）")
    ap.add_argument("--golden", default=None)
    ap.add_argument("--bit", default=None, help="不给 ⇒ 接手已加载的 PL（PL_TAKEOVER）")
    ap.add_argument("--shape", type=int, default=256)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--json", default=None)
    ap.add_argument("--vd-aggregate", nargs="+", default=None, metavar="J",
                    help="把多张单样本 --json 汇总成 V-D 判定（中位三判据 + DC 未解项）；"
                         "不跑网、不碰 PL")
    ap.add_argument("--pack-mode", choices=("ps", "e1e2"), default=None,
                    help="激活打包走哪条路（默认 ps = 能对的那一档；e1e2 只用于"
                         "量代价的 A/B，它的符号翻错率 ~2.16%%）")
    a = ap.parse_args(argv)
    if a.vd_aggregate:
        return vd_aggregate(a.vd_aggregate)
    if not a.input:
        ap.error("需要 --input <crop256.npy>（或用 --vd-aggregate 汇总已有 JSON）")

    for k, v in RUNPOINT.items():
        os.environ.setdefault(k, v)
    if a.pack_mode:
        os.environ["BIN_PACK"] = a.pack_mode
    os.environ.setdefault("PL_TAKEOVER", "1" if a.bit is None else "0")

    # ★ 2026-11-04（v60 配对自检，**先于任何一次 conv**）：容器说自己是哪一档？
    #   R1 在容器还是在 RTL，只能有一份 ⇒ 容器与位流必须成对，否则整幅抬亮/发灰。
    import json as _json
    _idx = _json.load(open(os.path.join(a.pack, "index.json")))["layers"]
    _rq = sorted({int(l.get("round_q") or 0) for l in _idx})
    _ss = sorted({int(l.get("slope_shift") or 8) for l in _idx})
    _has32 = all(l.get("off_thr32") for l in _idx)
    # 三档定稿（2026-11-04）：与 `ps_numeric/check_pack_consistency.pairing_of()` 同一张表
    #   (>0, 8)  = v58：R1 在容器，配 R1 未打的旧位流
    #   (0 , 8)  = v60：**R1 在 RTL、slope 保持 Q8** ⇒ 唯一可部署档
    #   (* , 12) = 已废弃：s12 经 2×2 实验判净退步（2/5→1/5），RTL 已回退到 8
    if _ss == [12]:
        _pair = "**已废弃(s12)**"
    elif _ss == [8] and _rq == [0]:
        _pair = "v60"
    elif _ss == [8] and len(_rq) == 1 and _rq[0] > 0:
        _pair = "v58"
    else:
        _pair = "**不合法**"
    print("[numfix] 容器配对：%s（round_q=%r slope_shift=%r thr32=%s）"
          % (_pair, _rq, _ss, "有" if _has32 else "没有"))
    if _pair == "v60":
        print("[numfix] ⇒ v60 正式契约（R1 在 RTL、slope=Q8）：**必须**配 R1+R3 已打的 "
              "v60 位流；旧位流会每层 −0.5 LSB（发灰）。")
    elif _pair == "v58":
        print("[numfix] ⇒ v58 旧档（R1 在容器）：**只能**配 R1 未打的旧位流；"
              "上 v60 会双重补偿（Δmean +0.48、整幅抬亮）。")
    elif _pair == "**已废弃(s12)**":
        print("[numfix] ⚠ slope_shift=12 已废弃（2×2 实测净退步 2/5→1/5，RTL 回退到 8）"
              " ⇒ 请改用 pack_v60/（round_q=0 × slope_shift=8）")
    else:
        print("[numfix] ⚠ 容器自身配对不合法（round_q/slope_shift 组合见上）⇒ 先修容器")

    import bin_net as BN
    import bin_engine as BE
    eng = None
    if a.bit is not None:
        try:
            import pynq.pl_server.embedded_device  # noqa: F401
        except Exception:
            pass
        from pynq import Overlay
        from bin_engine import BinConvEngine
        ov = Overlay(a.bit, download=os.environ.get("PL_TAKEOVER", "0") not in
                     ("1", "on", "true", "yes"))
        eng = BinConvEngine(ov, unit_id=0, pack_dir=a.pack)
        try:
            eng.load_weights()
        except Exception as _ex:            # noqa: BLE001
            msg = str(_ex)
            print("[numfix] ✗ load_weights 失败：%s" % msg[:300])
            if "ERR_SHIFT" in msg or "移位" in msg:
                print("[numfix] ⇒ **ERR_SHIFT = 容器与位流配对错**：容器 slope_shift=%r，"
                      "位流只接受它自己那档（v58=8 / v60=12）。"
                      % (_ss[0] if len(_ss) == 1 else _ss))
            raise
    net = BN.BinNet(a.pack, engine=eng, H=a.shape, W=a.shape, verbose=False)
    x = load_input(a.input)
    print("[numfix] 输入 %r mean %.4f  pack=%s" % (x.shape, float(x.mean()),
                                                   BE.pack_mode()))
    best = None
    for i in range(max(1, a.repeat)):
        t0 = time.time()
        outs, st = net.run(x, pipeline=True, nbuf=2)
        o = np.asarray(outs["out1"], np.float64)
        sh = {k: hashlib.sha1(np.ascontiguousarray(outs[k]).tobytes()).hexdigest()[:12]
              for k in ("out1", "out2", "out3")}
        print("[numfix] 第 %d 次 out1 min %.4f max %.4f mean %.4f std %.4f  %.2f s"
              % (i + 1, o.min(), o.max(), o.mean(), o.std(), time.time() - t0))
        print("[numfix] sha1[:12] = ('%s', '%s', '%s')   ← 新基线冻结表用这行"
              % (sh["out1"], sh["out2"], sh["out3"]))
        best = o
    if a.golden:
        g = np.asarray(np.load(a.golden), np.float64)
        if g.shape != best.shape:
            g = g.reshape(best.shape)
        ui, ug = u8(best), u8(g)
        d = ui - ug
        n = dict(mean_lsb=float(np.abs(d).mean()), max_lsb=float(np.abs(d).max()),
                 psnr_u8=float(10 * np.log10(255.0 ** 2 / max(np.mean(d.astype(float) ** 2), 1e-12))),
                 corr=float(np.corrcoef(best.ravel(), g.ravel())[0, 1]),
                 i8_mean=float(best.mean()), ref_mean=float(g.mean()),
                 i8_std=float(best.std()), ref_std=float(g.std()),
                 rel_rms=float(np.sqrt(np.mean((best - g) ** 2))
                               / max(np.sqrt(np.mean(g * g)), 1e-12)))
        # ---- V-D（三条，定稿口径）：判"容器/配对/结构有没有搞坏" ----
        chk = vd_checks(n)
        ok = all(c[1] for c in chk)
        print("[numfix] == V-D 判据（单样本口径；正式判定用 5 张中位 + --vd-aggregate）==")
        for name, good, val in chk:
            print("   %-30s %-5s %s" % (name, "OK" if good else "FAIL", val))
        # ---- DC：**未解项**，单独显示、单独红，不影响 V-D ----
        dc = abs(n["i8_mean"] - n["ref_mean"])
        dc_ok = dc <= DC_TARGET
        print("[numfix] == DC（A3'，**未解项**，目标 ≤%.2f，不参与 V-D）==" % DC_TARGET)
        print("   %-30s %-5s %+.3f   ← 主机 5 张实测中位 %.3f（%s）"
              % ("|Δmean| ≤ %.2f" % DC_TARGET, "OK" if dc_ok else "FAIL(未解)",
                 n["i8_mean"] - n["ref_mean"], VD_REF_5["dc_med"],
                 "2/5 达标；q 域 +N 排 v61"))
        print("[numfix] rel_rms %.4f  max|Δu8| %.0f" % (n["rel_rms"], n["max_lsb"]))
        print("[numfix] ⇒ V-D: %s ｜ DC: %s"
              % ("PASS" if ok else "FAIL", "PASS" if dc_ok else "FAIL(未解)"))
        if n["ref_std"] < VD["corr_min_refstd"]:
            print("[numfix] ⚠ 本样本参考几乎全平（ref_std=%.4f）⇒ ③ corr 跳过，A1'/A2' 也只当参考"
                  % n["ref_std"])
        if a.json:
            import json
            with open(a.json, "w") as f:
                json.dump(dict(input=a.input, golden=a.golden, pack=a.pack,
                               runpoint=RUNPOINT, metrics=n,
                               vd=dict(ok=bool(ok),
                                       checks=[dict(name=c[0], ok=bool(c[1]), val=c[2])
                                               for c in chk]),
                               dc=dict(target=DC_TARGET, val=n["i8_mean"] - n["ref_mean"],
                                       ok=bool(dc_ok), unresolved=True),
                               # 兼容旧调用方
                               checks=[dict(name=c[0], ok=bool(c[1]), val=c[2])
                                       for c in chk], pass_=bool(ok)), f, indent=1)
            print("[numfix] → %s" % a.json)
        # 退出码只看 V-D；DC 未解**不阻塞**（但脚本会一直打 FAIL(未解)）
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
