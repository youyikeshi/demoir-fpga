#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线配对：用 session.json 里的单应 H，把源图映射到相机坐标，
按每张记录的 crop 矩形取出对应的 **GT 256×256**，和相机图配成训练对。

为什么要单独一步
----------------
采集时只保证「相机图 + 源图 + 粘贴偏移 + 裁剪矩形 + H」齐全；GT 的提取放在离线，
好处是：H 可以重算（换标定）、裁剪位置可以改、参数可以反复重跑，不用重拍。

坐标链（和 capture_pairs.py 的约定一致）
--------------------------------------
    源图像素 (u,v)
        ── 源图贴到屏幕画布： (canvas) = (u + paste_x - crop_x0, v + paste_y - crop_y0)
        ── 屏幕画布 → 相机：  (cam) = H · (canvas)
    所以 相机 → 源图：  M = T · H⁻¹
        T = [[1,0, crop_x0 - paste_x],
             [0,1, crop_y0 - paste_y],
             [0,0, 1]]

用法
----
    # 采集前先标定（写 <out>/session.json 的 H）
    python capture_pairs.py --out D:\\shots\\run01 --calib

    # 采一批（只要中心 256）
    python capture_pairs.py --src D:\\shots\\src --out D:\\shots\\run01 --crop 256

    # 生成 GT
    python extract_pairs.py --run D:\\shots\\run01

产物
----
    pairs/p000001_cam.png   相机 256×256 原始像素（无损）
    pairs/p000001_src.png   屏幕源图（硬链接，全幅）
    pairs/p000001_gt.png    ★ 与 cam 严格对应的 GT 256×256
    pairs/p000001.json      meta（追加 gt_file / gt_valid）

注意
----
* 插值默认 INTER_LINEAR：相机像素落在源图像素之间时必须有插值，NEAREST 反而会
  引入 0.5px 的量化偏差。要"最保守"可以加 --nearest。
* 只有 `valid`（映射后落在源图范围内的比例）足够高才写 GT，否则跳过并计入报告 ——
  黑色补边会让训练对变成"拍黑屏"，比缺样本更糟。
"""
import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(*a):
    print(*a, flush=True)


def _srgb_to_lin(v):
    v = np.asarray(v, np.float64) / 255.0
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def _lin_to_srgb(v):
    v = np.clip(np.asarray(v, np.float64), 0.0, 1.0)
    out = np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(v, 1 / 2.4) - 0.055)
    return np.clip(out * 255.0, 0, 255)


def apply_color(bgr, model, damp=1.0):
    """BGR uint8 → 颜色校正后的 BGR uint8（gamma + 二次多项式，与 capture_pairs 一致）"""
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float64)
    h, w = rgb.shape[:2]
    x = np.clip(rgb.reshape(-1, 3) / 255.0, 0, 1) ** float(model.get("gamma", 1.0))
    R, G, B = x[:, 0], x[:, 1], x[:, 2]
    F = np.stack([R, G, B, R * R, G * G, B * B, R * G, R * B, G * B,
                  np.ones_like(R)], 1)
    out = np.clip(F @ np.asarray(model["coef"]), 0, 1).reshape(h, w, 3) * 255.0
    # 阻尼：完全校正会放大噪点/过校正（实测 α=0.85 时 ΔE 最优）
    if damp < 1.0:
        out = damp * out + (1.0 - damp) * rgb
    return cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR)


def load_session(run_dir):
    p = run_dir / "session.json"
    if not p.exists():
        raise SystemExit("找不到 %s —— 先跑 capture_pairs.py --calib" % p)
    s = json.loads(p.read_text(encoding="utf-8"))
    H = s.get("H")
    if not H:
        raise SystemExit("session.json 里没有 H —— 先跑一次 capture_pairs.py --calib")
    return s, np.array(H, np.float64)


def source_to_camera(H, disp):
    """源图像素 → 相机像素 的 3×3 矩阵。

    ⚠️ 这里必须返回**正向**（源→相机），因为 `cv2.warpPerspective(src, M)` 内部
    按 dst(x) = src(M⁻¹·x) 采样；传错方向得到的 GT 会整块错位。

    推导：
        源图 → 画布：  canvas = src + (paste_x - crop_x0, paste_y - crop_y0)   = T⁻¹·src
        画布 → 相机：  cam    = H · canvas
        ⇒  src → cam：  S2C = H · T⁻¹
    """
    T = np.array([[1.0, 0.0, disp["crop_x0"] - disp["paste_x"]],
                  [0.0, 1.0, disp["crop_y0"] - disp["paste_y"]],
                  [0.0, 0.0, 1.0]], np.float64)
    return H @ np.linalg.inv(T)


def main():
    ap = argparse.ArgumentParser(description="用标定 H 把源图映射到相机坐标，生成 GT 256")
    ap.add_argument("--run", required=True, help="采集目录（含 session.json 和 pairs/）")
    ap.add_argument("--nearest", action="store_true", help="用 INTER_NEAREST（默认 LINEAR）")
    ap.add_argument("--min-valid", type=float, default=0.98,
                    help="GT 里落在源图范围内的最小比例（默认 0.98），低于则跳过")
    ap.add_argument("--overwrite", action="store_true", help="重算已存在的 _gt.png")
    ap.add_argument("--color-damp", type=float, default=1.0,
                    help="颜色校正强度 0~1（1=完全校正）。实测 0.85 在这个 setup 上 ΔE 最优")
    ap.add_argument("--color", action="store_true",
                    help="用 session.json 里的颜色矩阵校正相机图，额外输出 _cam_cc.png")

    ap.add_argument("--limit", type=int, default=0, help="最多处理多少张（0=全部）")
    args = ap.parse_args()

    run = Path(args.run)
    pairs = run / "pairs"
    if not pairs.is_dir():
        raise SystemExit("找不到 %s" % pairs)

    session, H = load_session(run)
    cam_w, cam_h = session.get("capture_res") or [0, 0]
    if not cam_w:
        raise SystemExit("session.json 缺 capture_res")
    interp = cv2.INTER_NEAREST if args.nearest else cv2.INTER_LINEAR
    calib = session.get("calib") or {}
    color = session.get("color") or {}
    if args.color and not color.get("coef"):
        raise SystemExit("session.json 里没有颜色矩阵 —— 先跑 capture_pairs.py --color-calib")
    log("session: %s  采集分辨率 %dx%d  H RMS %.2f px"
        % (run.name, cam_w, cam_h, calib.get("rms_px", -1)))

    metas = sorted(pairs.glob("p*.json"))
    if not metas:
        raise SystemExit("pairs/ 里没有 meta —— 采集还没跑？")

    ok = skip = done = 0
    worst = 1.0
    for i, mp in enumerate(metas):
        if args.limit and i >= args.limit:
            break
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception as e:
            log("跳过坏 meta %s: %r" % (mp.name, e))
            skip += 1
            continue

        pid = meta.get("pair_id") or mp.stem
        gt_path = pairs / ("%s_gt.png" % pid)
        if gt_path.exists() and not args.overwrite:
            done += 1
            continue

        crop = meta.get("crop")
        disp = meta.get("display") or {}
        if not disp:
            log("%s: meta 缺 display 段，跳过" % pid)
            skip += 1
            continue
        src_path = pairs / (meta.get("src_file") or ("%s_src.png" % pid))
        src = cv2.imread(str(src_path), cv2.IMREAD_COLOR)
        if src is None:
            log("%s: 读不到源图 %s，跳过" % (pid, src_path.name))
            skip += 1
            continue

        M = source_to_camera(H, disp)
        gt_full = cv2.warpPerspective(src, M, (cam_w, cam_h), flags=interp,
                                      borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        # 有效区掩码：源图范围内 = 255，黑色补边 = 0
        mask = cv2.warpPerspective(np.full(src.shape[:2], 255, np.uint8), M,
                                   (cam_w, cam_h), flags=cv2.INTER_NEAREST)

        if crop:
            cx, cy, cw, ch = [int(v) for v in crop]
            gt = gt_full[cy:cy + ch, cx:cx + cw]
            mk = mask[cy:cy + ch, cx:cx + cw]
        else:
            gt, mk = gt_full, mask

        valid = float((mk > 0).mean()) if mk.size else 0.0
        worst = min(worst, valid)
        if valid < args.min_valid:
            log("%s: GT 有效区仅 %.1f%%（< %.0f%%），跳过" % (pid, valid * 100, args.min_valid * 100))
            skip += 1
            continue

        if not cv2.imwrite(str(gt_path), gt):
            log("%s: 写 GT 失败" % pid)
            skip += 1
            continue

        if args.color:
            cam_path = pairs / ("%s_cam.png" % pid)
            cam = cv2.imread(str(cam_path), cv2.IMREAD_COLOR)
            if cam is not None:
                cc = apply_color(cam, color, args.color_damp)
                cc_path = pairs / ("%s_cam_cc.png" % pid)
                cv2.imwrite(str(cc_path), cc)
                meta["cam_cc_file"] = cc_path.name

        meta["gt_file"] = gt_path.name
        meta["gt_size"] = [int(gt.shape[1]), int(gt.shape[0])]
        meta["gt_valid"] = round(valid, 4)
        mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        ok += 1
        if ok <= 3 or ok % 50 == 0:
            log("  %s  cam %s  gt %dx%d  有效 %.1f%%"
                % (pid, meta.get("crop") or "全幅", gt.shape[1], gt.shape[0], valid * 100))

    log("-" * 60)
    log("生成 GT %d 张，跳过 %d 张，已有 %d 张" % (ok, skip, done))
    if worst < 1.0:
        log("最低有效区比例 %.1f%%（接近 100% 说明相机视野基本落在屏幕内）" % (worst * 100))
    if ok:
        log("下一步：cam/gt 成对图在 %s/pairs/，可直接按 pair_id 组训练集" % run)
    return 0 if ok or done else 1


if __name__ == "__main__":
    sys.exit(main())
