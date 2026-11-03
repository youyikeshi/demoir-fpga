#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""capture_uhdm.py —— 屏幕翻拍采集：PC 全屏放图 → MaixCAM 2K 拍屏 → 裁切+矫色 → 落盘。

为什么单独一个脚本
------------------
`capture_pairs.py` 走的是旧 app（main.py 的 8555/8556），而板上现在跑的是
`cam_shot.py`（8660 一问一答）。本脚本复用它的**标定数学**（ArUco 单应 + 24 色卡
多项式矫色），把取图换成 8660，并且不裁 256、直接要**全幅 2K**：

    屏幕 2560×1440 ──拍摄──> 相机 2560×1440 ──H⁻¹ 透视变换──> 屏幕坐标 2560×1440
                                                        └─ 24 色卡矫色 ──> PNG

三步：

    # 1) 选 100 张 UHDM 干净图（各种类型，含文字页），生成全屏源图
    python3 capture_uhdm.py select --out ~/shots/uhdm100

    # 2) 标定（相机别动；动了要重标）
    python3 capture_uhdm.py calib --out ~/shots/uhdm100

    # 3) 逐张拍摄
    python3 capture_uhdm.py run --out ~/shots/uhdm100

    # 4) 生成对照图（src | cam）
    python3 capture_uhdm.py verify --out ~/shots/uhdm100

产物：

    uhdm100/
    ├── src/p000_xxx.png        屏幕要显示的内容（已按 16:9 letterbox）
    ├── cam/p000_xxx.png        ★ 相机拍到的 + 裁切 + 矫色（与 src 同名）
    ├── session.json            H / 曝光 / 色卡模型
    ├── calib_*.png             标定取证
    ├── index.jsonl             每张的耗时/md5/曝光
    └── verify/*.jpg            对照图
"""

import argparse
import glob
import hashlib
import json
import os
import random
import socket
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import capture_pairs as cp          # 复用：ArUco 标定页 / 色卡页 / 颜色模型

WIN = "capture_uhdm"
SW, SH = 2560, 1440                 # 屏幕（= 相机原生 2K）
DEF_EXP = 45                        # 白屏实测：不削顶，白场约 200（G）
DEF_HOST = "10.135.70.66"


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- #
# 取图（cam_shot 8660，全幅）
# --------------------------------------------------------------------------- #
def cam_shot(host, w=SW, h=SH, exp=DEF_EXP, timeout=90, seq=1):
    s = socket.create_connection((host, 8660), timeout=10)
    s.settimeout(timeout)
    try:
        req = {"v": 1, "cmd": "shot", "seq": seq, "w": w, "h": h,
               "crop": "center", "exp_us": exp, "gain": None,
               "timeout_ms": int(timeout * 1000)}
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            d = s.recv(4096)
            if not d:
                raise RuntimeError("设备提前断开（读头）")
            buf += d
        line, buf = buf.split(b"\n", 1)
        hdr = json.loads(line.decode())
        if not hdr.get("ok"):
            raise RuntimeError("设备回错：%r" % (hdr,))
        n = int(hdr["nbytes"])
        while len(buf) < n:
            d = s.recv(1 << 20)
            if not d:
                break
            buf += d
        if len(buf) < n:
            raise RuntimeError("载荷不完整：%d/%d" % (len(buf), n))
        data = buf[:n]
        if hashlib.md5(data).hexdigest() != hdr.get("md5"):
            raise RuntimeError("md5 不符")
        rgb = np.frombuffer(data, np.uint8).reshape(hdr["h"], hdr["w"], 3)
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), hdr
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# 全屏显示
# --------------------------------------------------------------------------- #
class Screen:
    def __init__(self):
        cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
        cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        self.first = True

    def show(self, bgr, settle=0.45):
        cv2.imshow(WIN, bgr)
        cv2.waitKey(1)
        time.sleep(settle)
        cv2.waitKey(1)

    def close(self):
        cv2.destroyAllWindows()


def letterbox(img, w=SW, h=SH):
    ih, iw = img.shape[:2]
    s = min(w / float(iw), h / float(ih))
    nw, nh = int(round(iw * s)), int(round(ih * s))
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
    r = cv2.resize(img, (nw, nh), interpolation=interp)
    canvas = np.zeros((h, w, 3), np.uint8)
    x, y = (w - nw) // 2, (h - nh) // 2
    canvas[y:y + nh, x:x + nw] = r
    return canvas, (x, y, nw, nh)


# --------------------------------------------------------------------------- #
# select：从 UHDM 干净图里挑 100 张
# --------------------------------------------------------------------------- #
def features(path):
    im = cv2.imread(path, cv2.IMREAD_REDUCED_COLOR_4)
    if im is None:
        return None
    im = cv2.resize(im, (320, 240), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 80, 180)
    edge = float((edges > 0).mean())
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    sat = float(hsv[..., 1].mean())
    val = float(hsv[..., 2].mean())
    lab = cv2.cvtColor(im, cv2.COLOR_BGR2LAB)
    colorful = float(lab[..., 1].std() + lab[..., 2].std())
    dark = float((gray < 40).mean())
    return np.array([edge, sat, val, colorful, dark], np.float32)


def pick_images(root, n, seed):
    files = sorted(glob.glob(os.path.join(root, "**", "*_gt.jpg"), recursive=True))
    if not files:
        raise SystemExit("在 %s 下没找到 *_gt.jpg" % root)
    log("扫描 %d 张 UHDM 干净图…" % len(files))
    feats, keep = [], []
    for i, f in enumerate(files):
        v = features(f)
        if v is not None:
            feats.append(v)
            keep.append(f)
        if (i + 1) % 500 == 0:
            log("  %d/%d" % (i + 1, len(files)))
    F = np.stack(feats)
    log("特征完成：%d 张" % len(keep))

    # 1) 文字页：边缘密度最高的一批（论文页/文档扫描）
    n_text = min(30, n // 3)
    idx_text = list(np.argsort(-F[:, 0])[:n_text])

    # 2) 其余用 k-means 在特征空间铺开，保证"各种类型"
    rest = [i for i in range(len(keep)) if i not in set(idx_text)]
    Z = F[rest].copy()
    Z = (Z - Z.mean(0)) / (Z.std(0) + 1e-6)
    k = max(1, n - n_text)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 60, 0.5)
    _, labels, centers = cv2.kmeans(Z, k, None, crit, 4, cv2.KMEANS_PP_CENTERS)
    picked = []
    for c in range(k):
        members = np.where(labels.ravel() == c)[0]
        if len(members) == 0:
            continue
        d = np.linalg.norm(Z[members] - centers[c], axis=1)
        picked.append(rest[int(members[int(np.argmin(d))])])
    chosen = list(dict.fromkeys(idx_text + picked))
    if len(chosen) < n:
        for i in rest:
            if i not in chosen:
                chosen.append(i)
            if len(chosen) >= n:
                break
    chosen = chosen[:n]
    random.Random(seed).shuffle(chosen)
    return [keep[i] for i in chosen]


def cmd_select(args):
    out = os.path.abspath(os.path.expanduser(args.out))
    src_dir = os.path.join(out, "src")
    os.makedirs(src_dir, exist_ok=True)
    files = pick_images(args.root, args.n, args.seed)

    manifest = []
    log("生成全屏源图（%dx%d letterbox）…" % (SW, SH))
    for i, f in enumerate(files):
        im = cv2.imread(f, cv2.IMREAD_COLOR)
        canvas, rect = letterbox(im)
        pair = os.path.basename(os.path.dirname(f))
        name = "p%03d_%s_%s.png" % (i, pair, os.path.splitext(os.path.basename(f))[0])
        cv2.imwrite(os.path.join(src_dir, name), canvas)
        manifest.append({"idx": i, "name": name, "source": f,
                         "src_rect": rect,
                         "src_size": [im.shape[1], im.shape[0]]})
        if (i + 1) % 20 == 0:
            log("  %d/%d" % (i + 1, len(files)))
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fp:
        json.dump({"screen": [SW, SH], "items": manifest}, fp, ensure_ascii=False, indent=1)

    # 预览拼图（10×10）
    cols, rows = 10, 10
    tw, th = SW // cols, SH // rows
    sheet = np.zeros((SH, SW, 3), np.uint8)
    for i, it in enumerate(manifest):
        im = cv2.imread(os.path.join(src_dir, it["name"]))
        im = cv2.resize(im, (tw, th), interpolation=cv2.INTER_AREA)
        sheet[(i // cols) * th:(i // cols + 1) * th,
              (i % cols) * tw:(i % cols + 1) * tw] = im
    cv2.imwrite(os.path.join(out, "src_sheet.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 90])

    log("完成：%d 张 -> %s" % (len(manifest), src_dir))
    log("预览：%s" % os.path.join(out, "src_sheet.jpg"))


# --------------------------------------------------------------------------- #
# calib：ArUco 单应 + 24 色卡颜色模型
# --------------------------------------------------------------------------- #
def fit_homography(page, refs, cam_bgr, log_fn=log):
    det = cp.detect_markers(cam_bgr)
    src, dst = [], []
    for mid, sc in refs:
        if mid not in det:
            continue
        for j in range(4):
            src.append(sc[j])
            dst.append(det[mid][j])
    log_fn("  检测到 %d/%d 个标记" % (len(src) // 4, len(refs)))
    if len(src) < 12:
        raise SystemExit("标记太少（%d 点），标定失败：相机是不是没对准屏幕/失焦？" % len(src))
    H, mask = cv2.findHomography(np.float32(src), np.float32(dst), cv2.RANSAC, 3.0)
    if H is None:
        raise SystemExit("单应求解失败")
    proj = cv2.perspectiveTransform(np.float32(src).reshape(-1, 1, 2), H).reshape(-1, 2)
    err = np.linalg.norm(proj - np.float32(dst), axis=1)
    inl = mask.ravel().astype(bool) if mask is not None else np.ones(len(err), bool)
    rms = float(np.sqrt((err[inl] ** 2).mean()))
    log_fn("  内点 %d/%d，RMS %.2f px，最大 %.2f px" % (inl.sum(), len(err), rms, err.max()))
    if rms > 3.0:
        log_fn("  ⚠️ RMS 偏大：相机可能失焦/屏幕反光，先看看 calib_vis.png")
    return H, rms, det


def cmd_calib(args):
    out = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(out, exist_ok=True)
    dev = args.host
    scr = Screen()

    log("[1/2] ArUco 标定（全屏 4×3 标记）…")
    page, refs = cp.build_calib_page(SW, SH, cols=4, rows=3, s=140)
    scr.show(page, settle=args.settle)
    cam, hdr = cam_shot(dev, exp=args.exp)
    H, rms, det = fit_homography(page, refs, cam)
    cv2.imwrite(os.path.join(out, "calib_page.png"), page)
    cv2.imwrite(os.path.join(out, "calib_cam.png"), cam)
    vis = cam.copy()
    cv2.aruco.drawDetectedMarkers(vis, [c.reshape(1, 4, 2) for c in det.values()])
    cv2.imwrite(os.path.join(out, "calib_vis.png"), vis)

    # 屏幕四角在相机画面里的位置（判断是否看全）
    scr_c = np.float32([[0, 0], [SW, 0], [SW, SH], [0, SH]]).reshape(-1, 1, 2)
    poly = cv2.perspectiveTransform(scr_c, H).reshape(-1, 2)
    log("  屏幕四角(相机坐标)：%s" % np.round(poly, 1).tolist())

    log("[2/2] 24 色卡矫色（曝光 %s，AWB 已锁）…" % args.exp)
    cpage, cells = cp.build_color_page(SW, SH, (0, 0, SW, SH), gap=36, margin=60)
    scr.show(cpage, settle=args.settle)
    ccam, hdr2 = cam_shot(dev, exp=args.exp)
    cv2.imwrite(os.path.join(out, "color_page.png"), cpage)
    cv2.imwrite(os.path.join(out, "color_cam.png"), ccam)

    cam_rgb, ref_rgb = [], []
    for (x, y, cw, ch), ref in zip(cells, cp.COLOR_PATCHES):
        sx, sy = x + cw / 2.0, y + ch / 2.0
        p = cv2.perspectiveTransform(np.float32([[[sx, sy]]]), H).reshape(2)
        cx, cy = int(round(p[0])), int(round(p[1]))
        r = max(2, int(min(cw, ch) * 0.10))
        cx = min(max(cx, r), SW - r - 1)
        cy = min(max(cy, r), SH - r - 1)
        patch = ccam[cy - r:cy + r + 1, cx - r:cx + r + 1].reshape(-1, 3).mean(0)
        cam_rgb.append(patch[::-1])        # BGR -> RGB
        ref_rgb.append(ref)
    model = cp.fit_color_model(np.array(cam_rgb), np.array(ref_rgb))
    log("  颜色模型：gamma=%.2f  平均误差 %.2f  最大 %.2f（0-255）"
        % (model["gamma"], model["err_mean"], model["err_max"]))

    # 矫色后把色卡再拍一次，验证
    fixed = cp.apply_color_model(ccam, model)
    cv2.imwrite(os.path.join(out, "color_fixed.png"), fixed)

    session = {"screen": [SW, SH], "host": dev, "exp_us": args.exp,
               "H": H.tolist(), "H_rms": rms, "color": model,
               "camera_native": hdr.get("native")}
    with open(os.path.join(out, "session.json"), "w", encoding="utf-8") as fp:
        json.dump(session, fp, ensure_ascii=False, indent=1)
    scr.close()
    log("标定完成 -> %s/session.json（相机现在别动）" % out)


# --------------------------------------------------------------------------- #
# run：逐张拍摄
# --------------------------------------------------------------------------- #
def load_session(out):
    p = os.path.join(out, "session.json")
    if not os.path.exists(p):
        raise SystemExit("没有 session.json —— 先跑 calib")
    s = json.load(open(p, encoding="utf-8"))
    return s, np.array(s["H"], np.float64)


def cmd_run(args):
    out = os.path.abspath(os.path.expanduser(args.out))
    session, H = load_session(out)
    Hinv = np.linalg.inv(H)
    model = session["color"]
    exp = args.exp or session["exp_us"]
    src_dir = os.path.join(out, "src")
    cam_dir = os.path.join(out, "cam")
    os.makedirs(cam_dir, exist_ok=True)
    names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(src_dir, "*.png")))
    if args.start:
        names = [n for n in names if n >= args.start]
    if args.limit:
        names = names[:args.limit]
    names = [n for n in names if args.overwrite or not os.path.exists(os.path.join(cam_dir, n))]
    log("待拍 %d 张，曝光 %s，H RMS %.2f px" % (len(names), exp, session.get("H_rms", -1)))

    scr = Screen()
    idx_path = os.path.join(out, "index.jsonl")
    t_all = time.time()
    for i, name in enumerate(names):
        t0 = time.time()
        src = cv2.imread(os.path.join(src_dir, name))
        if src is None:
            log("  跳过（读不了）：%s" % name)
            continue
        scr.show(src, settle=args.settle)
        cam, hdr = cam_shot(args.host, exp=exp, seq=i + 1)
        warped = cv2.warpPerspective(cam, Hinv, (SW, SH), flags=cv2.INTER_LINEAR)
        fixed = cp.apply_color_model(warped, model)
        cv2.imwrite(os.path.join(cam_dir, name), fixed)
        rec = {"name": name, "t_cap_us": hdr.get("t_cap_us"),
               "exp_us": hdr.get("exp_us"), "gain": hdr.get("gain"),
               "wall_s": round(time.time() - t0, 2)}
        with open(idx_path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log("[%d/%d] %s  %.1fs" % (i + 1, len(names), name, rec["wall_s"]))
    scr.close()
    log("完成 %d 张，共 %.1f 分钟 -> %s" % (len(names), (time.time() - t_all) / 60.0, cam_dir))


# --------------------------------------------------------------------------- #
# verify：src | cam 对照图
# --------------------------------------------------------------------------- #
def cmd_verify(args):
    out = os.path.abspath(os.path.expanduser(args.out))
    src_dir = os.path.join(out, "src")
    cam_dir = os.path.join(out, "cam")
    vdir = os.path.join(out, "verify")
    os.makedirs(vdir, exist_ok=True)
    names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(cam_dir, "*.png")))
    if not names:
        raise SystemExit("cam/ 里还没有图")
    # 逐张：上 src、下 cam，缩到 1280 宽
    tw = 1280
    th = int(tw * SH / SW)
    for n in names:
        s = cv2.imread(os.path.join(src_dir, n))
        c = cv2.imread(os.path.join(cam_dir, n))
        if s is None or c is None:
            continue
        s = cv2.resize(s, (tw, th), interpolation=cv2.INTER_AREA)
        c = cv2.resize(c, (tw, th), interpolation=cv2.INTER_AREA)
        cv2.imwrite(os.path.join(vdir, n.replace(".png", "_cmp.jpg")),
                    np.vstack([s, c]), [cv2.IMWRITE_JPEG_QUALITY, 90])

    # 一张大拼图：每格 src 在上、cam 在下
    cols = 10
    rows = (len(names) + cols - 1) // cols
    cw, chh = SW // cols, SH // cols
    sheet = np.zeros((rows * chh * 2, cols * cw, 3), np.uint8)
    for i, n in enumerate(names):
        s = cv2.resize(cv2.imread(os.path.join(src_dir, n)), (cw, chh))
        c = cv2.resize(cv2.imread(os.path.join(cam_dir, n)), (cw, chh))
        r, cc = divmod(i, cols)
        sheet[r * 2 * chh:(r * 2 + 1) * chh, cc * cw:(cc + 1) * cw] = s
        sheet[(r * 2 + 1) * chh:(r * 2 + 2) * chh, cc * cw:(cc + 1) * cw] = c
    cv2.imwrite(os.path.join(out, "verify_sheet.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
    log("对照图：%s（单张在 %s/）" % (os.path.join(out, "verify_sheet.jpg"), vdir))


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="UHDM 屏幕翻拍：选图 → 标定 → 2K 拍摄 → 裁切矫色")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("select", help="挑 100 张并生成全屏源图")
    p.add_argument("--out", required=True)
    p.add_argument("--root", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(HERE))),
        "datasets", "uhdm_data", "Train"))
    p.add_argument("--n", type=int, default=100)
    p.add_argument("--seed", type=int, default=20261103)
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("calib", help="ArUco 单应 + 24 色卡")
    p.add_argument("--out", required=True)
    p.add_argument("--host", default=DEF_HOST)
    p.add_argument("--exp", type=int, default=DEF_EXP)
    p.add_argument("--settle", type=float, default=0.6)
    p.set_defaults(func=cmd_calib)

    p = sub.add_parser("run", help="逐张拍摄")
    p.add_argument("--out", required=True)
    p.add_argument("--host", default=DEF_HOST)
    p.add_argument("--exp", type=int, default=None, help="覆盖 session 里的曝光")
    p.add_argument("--settle", type=float, default=0.35)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--start", default=None, help="从某个文件名开始（断点续采）")
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("verify", help="生成 src|cam 对照图")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
