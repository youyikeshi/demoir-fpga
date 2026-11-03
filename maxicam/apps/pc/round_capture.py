#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一轮采集：选 4 张源图 → 重新标定（位置+曝光+颜色）→ 采 20 对（每张 5 个不重复位置）。

为什么这样组织（针对「同位置会过拟合」）：
  每一轮对应**一套几何**（相机/屏幕动过之后就是另一轮）。同一轮内摩尔纹频率是固定的，
  变的是内容；**跨轮**才是真正的多样性来源。所以：
    * 一轮只采少量（默认 20 对），逼着你换几何
    * 源图窗口按 step 滑动（默认 2）⇒ ABCD → CDEF → EFGH，
      相邻两轮共享 2 张图：同一内容在不同几何下各拍一次，便于模型把「内容」和「摩尔纹」解耦
    * 每轮的元数据（distance/angle）写进 round.json + session.json，训练时按轮分组划分

用法：
    python round_capture.py --all D:\\shots\\src --base D:\\shots\\runs --round 1 --distance 62 --angle 0
    # 第二轮：你先把相机/屏幕挪一下，再
    python round_capture.py --all D:\\shots\\src --base D:\\shots\\runs --round 2 --distance 68 --angle 0
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}

HERE = Path(__file__).resolve().parent


_LOGF = [None]


def log(*a):
    line = " ".join(str(x) for x in a)
    print("[round]", line, flush=True)
    f = _LOGF[0]
    if f is not None:
        try:
            f.write("[round] " + line + "\n")
            f.flush()
        except Exception:
            pass


def send_cmd(host, port, obj, wait=1.0):
    """给设备发一条控制命令（不依赖 send_cmd.py，带重试）"""
    import socket
    for _ in range(3):
        try:
            s = socket.create_connection((host, port), timeout=5)
            s.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode())
            time.sleep(wait)
            s.close()
            return True
        except Exception:
            time.sleep(0.5)
    raise IOError("控制通道连不上 %s:%d" % (host, port))


def link_or_copy(src, dst):
    if dst.exists() or dst.is_symlink():
        try:
            dst.unlink()
        except Exception:
            pass
    try:
        os.link(src, dst)
        return "hardlink"
    except Exception:
        pass
    try:
        os.symlink(src, dst)
        return "symlink"
    except Exception:
        pass
    import shutil
    shutil.copy2(src, dst)
    return "copy"


def main():
    ap = argparse.ArgumentParser(description="一轮采集：4 张图 × 5 位置 = 20 对")
    ap.add_argument("--all", required=True, help="全部源图目录")
    ap.add_argument("--base", required=True, help="输出根目录（每轮一个 roundNN 子目录）")
    ap.add_argument("--round", type=int, required=True, help="第几轮（从 1 开始）")
    ap.add_argument("--window", type=int, default=4, help="每轮用几张源图（默认 4）")
    ap.add_argument("--step", type=int, default=2, help="相邻轮次源图窗口前进几张（默认 2）")
    ap.add_argument("--per-image", type=int, default=5, help="每张源图采几对（默认 5，位置不重复）")
    ap.add_argument("--crop", type=int, default=256, help="只取中心 N×N（默认 256）")
    ap.add_argument("--res", default="2560x1440",
                    help="采集分辨率（**必须保持 2560x1440**：摩尔纹是采样现象，"
                         "换分辨率等于换一种摩尔纹，混进训练集就没意义了）")
    ap.add_argument("--host", default="10.13.148.1")
    ap.add_argument("--distance", type=float, default=0.0, help="本轮相机到屏幕距离 cm（写进元数据）")
    ap.add_argument("--angle", type=float, default=0.0, help="本轮俯仰/偏转角（写进元数据）")
    ap.add_argument("--note", default="", help="本轮备注（例如 '换了 5cm 距离'）")
    a = ap.parse_args()

    # ---- 先确认设备在线（放最前面：不要先建目录、写 round.json 再失败）----
    import socket as _sk
    ok_ports = False
    for i in range(6):                       # 最多等 ~12 s，给刚点开的应用留时间
        ok_ports = True
        for port in (8555, 8556):
            try:
                _sk.create_connection((a.host, port), timeout=2).close()
            except Exception:
                ok_ports = False
                break
        if ok_ports:
            if i:
                log("设备已上线（等了 %d 秒）" % (i * 2))
            break
        if i == 0:
            log("设备 %s 还没响应，等待中…（请在板子上点开「视频推流」）" % a.host)
        time.sleep(2)
    if not ok_ports:
        raise SystemExit(
            "❌ 设备端应用没在跑（%s 的 8555/8556 连不上）。\n"
            "   → 请在板子上点开「视频推流」，等屏幕显示出画面后再重跑。\n"
            "   （刚才那次什么都没有写盘，重跑不会产生半成品目录。）" % a.host)

    all_dir = Path(a.all)
    files = sorted([p for p in all_dir.iterdir() if p.suffix.lower() in IMG_EXT])
    if not files:
        raise SystemExit("源图目录里没有图片: %s" % all_dir)

    start = (a.round - 1) * a.step
    sel = files[start:start + a.window]
    if len(sel) < 2:
        raise SystemExit("源图不够了：第 %d 轮需要第 %d~%d 张，但目录里只有 %d 张"
                         % (a.round, start + 1, start + a.window, len(files)))

    out = Path(a.base) / ("round%02d" % a.round)
    src = out / "src"
    src.mkdir(parents=True, exist_ok=True)

    plan = []
    for f in sel:
        how = link_or_copy(f.resolve(), src / f.name)
        plan.append({"src_id": f.stem, "file": f.name, "link": how})

    round_meta = {
        "round": a.round,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "geometry": {"distance_cm": a.distance, "angle_deg": a.angle},
        "note": a.note,
        "window": a.window, "step": a.step, "per_image": a.per_image,
        "crop": a.crop, "res": a.res,
        "src_start_index": start,
        "images": plan,
        "total_pairs": len(sel) * a.per_image,
    }
    out.mkdir(parents=True, exist_ok=True)
    try:
        import io
        _LOGF[0] = open(out / "run.log", "w", buffering=1, encoding="utf-8")
        log("日志: %s" % (out / "run.log"))
    except Exception as e:
        print("[round] 打不开 run.log: %r" % (e,))
    (out / "round.json").write_text(json.dumps(round_meta, ensure_ascii=False, indent=2),
                                    encoding="utf-8")

    log("第 %d 轮：源图 %s ~ %s（%d 张），每张 %d 对 ⇒ 共 %d 对"
        % (a.round, sel[0].name, sel[-1].name, len(sel), a.per_image, len(sel) * a.per_image))
    log("几何：距离 %s cm，角度 %s°%s"
        % (a.distance, a.angle, ("，" + a.note) if a.note else ""))
    log("输出：%s" % out)


    # 采集前先把预览停掉，并留出 VI/VPSS 释放时间。
    # 教训：预览正在推流时直接采集，曾把设备打到 SIGSEGV（mmf_vo_frame_push failed）。
    try:
        send_cmd(a.host, 8555, {"t": "cmd", "cmd": "stop"}, wait=0.5)
        # 设备端自己也会等推流启动满 3 秒才真正停（避免打断 VO 初始化），
        # 这里再多留 4 秒让 VI/VPSS 彻底释放。
        log("已停止设备端预览，等待资源释放 4 秒…")
        time.sleep(4.0)
    except Exception as e:
        log("停止预览失败(%r)：直接尝试采集" % (e,))

    cmd = [sys.executable, str(HERE / "capture_pairs.py"),
           "--src", str(src), "--out", str(out),
           "--res", a.res, "--crop", str(a.crop),
           "--scan", "--scan-limit", str(a.per_image),
           "--limit", str(len(sel) * a.per_image),
           "--auto-calib", "--auto-exp", "--auto-color",
           "--host", a.host,
           "--distance", str(a.distance), "--angle", str(a.angle)]
    log("执行：capture_pairs.py（重新标定位置 → 锁曝光 → 颜色标定 → 采集）")
    r = subprocess.run(cmd, cwd=str(HERE), stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, bufsize=1)
    for ln in (r.stdout or "").splitlines():
        print(ln, flush=True)
        if _LOGF[0] is not None:
            try:
                _LOGF[0].write(ln + "\n")
            except Exception:
                pass
    if r.returncode != 0:
        raise SystemExit("采集失败（返回码 %d）" % r.returncode)

    # 统计
    pairs = list((out / "pairs").glob("*_cam.png"))
    log("完成：%d 对，目录 %s" % (len(pairs), out))
    log("下一步：extract_pairs.py --run %s --color --color-damp 0.85" % out)

    # 本轮结束 → 把实时预览推回设备屏，方便你对着屏幕调焦/摆位
    try:
        send_cmd(a.host, 8555, {"t": "cmd", "cmd": "start"})
        log("已开启设备端实时预览（屏幕底部有「采集区」方框）—— 现在可以对焦/调整位置")
        log("调好后直接跑下一轮：--round %d" % (a.round + 1))
    except Exception as e:
        log("开启预览失败(%r)：可在设备上点「开始推流」" % (e,))
    return 0


if __name__ == "__main__":
    sys.exit(main())
