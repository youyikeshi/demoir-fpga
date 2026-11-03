#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""center256.py —— 相机端「裁中心 256 + 矫色」后回传，PC 存进新文件夹。

和 capture_uhdm.py 的分工
------------------------
* `capture_uhdm.py`：2K 全幅 → PC 端透视裁切/矫色（看整屏效果用）
* 本脚本：**相机端**裁中心 256 → **相机端**矫色 → 只回传 196,608 B 的 256×256
  （就是工程里 `capture_pairs.py --crop 256` 的采集口径，区别是矫色也放在设备上）

三步
----
    # 0) 设备上先跑「不矫色」的采集服务（见 docs/center256.md）
    # 1) 标定颜色模型（色卡铺进中心 256，拟合后写到 out/color256.json）
    python3 center256.py calib --out shots/uhdm100 --host 10.135.70.66

    # 2) 把 color256.json 传到设备，重启服务时加 --color-model（见 docs）
    # 3) 逐张显示 + 取 256，存进新文件夹
    python3 center256.py run --src shots/uhdm100/src \
        --out shots/uhdm100_center256 --host 10.135.70.66
"""

import argparse
import glob
import json
import os
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import capture_pairs as cp
import capture_uhdm as cu

CROP = 256
CROP_X0 = (cu.SW - CROP) // 2
CROP_Y0 = (cu.SH - CROP) // 2


def log(*a):
    print(*a, flush=True)


def load_H(out):
    s = json.load(open(os.path.join(out, "session.json"), encoding="utf-8"))
    return np.array(s["H"], np.float64), s


def chart_page(rect):
    """整屏灰底 + 中心 rect 里 24 色块（BGR，直接 imshow）。"""
    return cp.build_color_page(cu.SW, cu.SH, rect, gap=3, margin=8)


def cmd_calib(args):
    out = os.path.abspath(os.path.expanduser(args.out))
    _, session = load_H(out)
    scr = cu.Screen()

    # 1) 先在同一进程/同一窗口里重算 H —— 旧 H 可能来自另一个窗口，采样会偏进缝里
    log("[1/3] ArUco 重标（同一窗口，保证 H 与本次显示严格一致）…")
    page, refs = cp.build_calib_page(cu.SW, cu.SH, cols=4, rows=3, s=140)
    scr.show(page, settle=args.settle)
    cam2k, _ = cu.cam_shot(args.host, w=cu.SW, h=cu.SH, exp=args.exp)
    H, rms, det = cu.fit_homography(page, refs, cam2k)
    session["H"] = H.tolist()
    session["H_rms"] = rms
    with open(os.path.join(out, "session.json"), "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=1)

    # 2) 用新 H 反推中心 256 覆盖的屏幕区域，把色卡铺进去
    pts = np.float32([[CROP_X0, CROP_Y0], [CROP_X0 + CROP, CROP_Y0],
                      [CROP_X0 + CROP, CROP_Y0 + CROP],
                      [CROP_X0, CROP_Y0 + CROP]]).reshape(-1, 1, 2)
    q = cv2.perspectiveTransform(pts, np.linalg.inv(H)).reshape(-1, 2)
    x0, x1 = float(q[:, 0].min()), float(q[:, 0].max())
    y0, y1 = float(q[:, 1].min()), float(q[:, 1].max())
    dx, dy = (x1 - x0) * 0.10, (y1 - y0) * 0.10
    rect = (x0 + dx, y0 + dy, x1 - dx, y1 - dy)
    log("中心 256 在屏幕上占 x %.0f~%.0f, y %.0f~%.0f" % (x0, x1, y0, y1))

    log("[2/3] 色卡按新 H 铺进中心 256…")
    page, cells = chart_page(rect)
    scr.show(page, settle=args.settle)
    cam, hdr = cu.cam_shot(args.host, w=CROP, h=CROP, exp=args.exp)

    cam_rgb, ref_rgb, spread = [], [], []
    for (px, py, pw, ph), ref in zip(cells, cp.COLOR_PATCHES):
        p = cv2.perspectiveTransform(
            np.float32([[[px + pw / 2.0, py + ph / 2.0]]]), H).reshape(2)
        cx, cy = int(round(p[0])) - CROP_X0, int(round(p[1])) - CROP_Y0
        r = max(3, int(min(pw, ph) * 0.18))
        if cx - r < 0 or cy - r < 0 or cx + r >= CROP or cy + r >= CROP:
            continue
        box = cam[cy - r:cy + r + 1, cx - r:cx + r + 1].reshape(-1, 3)
        cam_rgb.append(box.mean(0)[::-1])
        ref_rgb.append(ref)
        spread.append(float(box.std(0).mean()))
    log("采样 %d/24 块，块内平均 std %.1f（>25 说明采样点偏出块）"
        % (len(cam_rgb), float(np.mean(spread)) if spread else -1))
    cv2.imwrite(os.path.join(out, "color256_cam.png"), cam)
    if len(cam_rgb) < 16:
        raise SystemExit("色块采样太少（%d），色卡没落进 256 框" % len(cam_rgb))

    cam_rgb = np.array(cam_rgb); ref_rgb = np.array(ref_rgb, np.float64)
    if cam_rgb.min() > 240 or np.median(cam_rgb) > 248:
        raise SystemExit("标定帧过曝（min=%.0f median=%.0f），降 --exp 再跑"
                         % (cam_rgb.min(), np.median(cam_rgb)))

    model = cp.fit_color_model(cam_rgb, ref_rgb)
    log("颜色模型 gamma=%.2f  mean_err=%.2f  max_err=%.2f"
        % (model["gamma"], model["err_mean"], model["err_max"]))

    # 3) 中性灰验收：灰屏经模型后三通道应接近
    log("[3/3] 中性灰验收…")
    gray = np.full((cu.SH, cu.SW, 3), 128, np.uint8)
    scr.show(gray, settle=args.settle)
    gcam, _ = cu.cam_shot(args.host, w=CROP, h=CROP, exp=args.exp)
    gf = cp.apply_color_model(gcam, model)
    gm = gf.reshape(-1, 3).mean(0)[::-1]
    log("灰屏 128 -> 矫色后 RGB %s（极差 %.0f）"
        % (np.round(gm, 1).tolist(), float(gm.max() - gm.min())))
    scr.close()

    fixed = cp.apply_color_model(cam, model)
    cv2.imwrite(os.path.join(out, "color256_fixed.png"), fixed)
    path = os.path.join(out, "color256.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=1)
    log("模型 -> %s" % path)


def cmd_run(args):
    src_dir = os.path.abspath(os.path.expanduser(args.src))
    out = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(out, exist_ok=True)
    names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(src_dir, "*.png")))
    if args.limit:
        names = names[:args.limit]
    names = [n for n in names if args.overwrite or not os.path.exists(os.path.join(out, n))]
    log("待拍 %d 张（相机端裁 %d + 矫色）" % (len(names), CROP))

    scr = cu.Screen()
    idx_path = os.path.join(out, "index.jsonl")
    t0 = time.time()
    for i, n in enumerate(names):
        t = time.time()
        src = cv2.imread(os.path.join(src_dir, n))
        if src is None:
            continue
        scr.show(src, settle=args.settle)
        img, hdr = cu.cam_shot(args.host, w=CROP, h=CROP, exp=args.exp, seq=i + 1)
        cv2.imwrite(os.path.join(out, n), img)
        rec = {"name": n, "w": hdr.get("w"), "h": hdr.get("h"),
               "t_cap_us": hdr.get("t_cap_us"), "exp_us": hdr.get("exp_us"),
               "gain": hdr.get("gain"), "wall_s": round(time.time() - t, 2)}
        with open(idx_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log("[%d/%d] %s  %.2fs" % (i + 1, len(names), n, rec["wall_s"]))
    scr.close()
    log("完成 %d 张，%.1f 分钟 -> %s" % (len(names), (time.time() - t0) / 60, out))

    # 拼一张 10×10 预览
    cols = 10
    rows = (len(names) + cols - 1) // cols
    sheet = np.zeros((rows * CROP, cols * CROP, 3), np.uint8)
    for i, n in enumerate(names):
        im = cv2.imread(os.path.join(out, n))
        if im is None:
            continue
        r, c = divmod(i, cols)
        sheet[r * CROP:(r + 1) * CROP, c * CROP:(c + 1) * CROP] = im
    cv2.imwrite(os.path.join(out, "_sheet.jpg"), sheet,
                [cv2.IMWRITE_JPEG_QUALITY, 92])


def main(argv=None):
    ap = argparse.ArgumentParser(description="相机端裁 256 + 矫色，回传 256×256")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("calib")
    p.add_argument("--out", required=True, help="含 session.json 的目录（capture_uhdm 的产物）")
    p.add_argument("--host", default=cu.DEF_HOST)
    p.add_argument("--exp", type=int, default=cu.DEF_EXP)
    p.add_argument("--settle", type=float, default=0.6)
    p.set_defaults(func=cmd_calib)

    p = sub.add_parser("run")
    p.add_argument("--src", required=True)
    p.add_argument("--out", required=True, help="新建的 256 输出文件夹")
    p.add_argument("--host", default=cu.DEF_HOST)
    p.add_argument("--exp", type=int, default=cu.DEF_EXP)
    p.add_argument("--settle", type=float, default=0.45)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=cmd_run)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
