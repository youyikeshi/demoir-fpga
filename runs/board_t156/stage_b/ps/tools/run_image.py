#!/usr/bin/env python3
"""任意图片 → 板端去摩尔纹 → 输出 PNG（一条命令跑完）。

设计取向：**这个脚本在主机上跑，通过 SSH 驱动板子**。因为你平时的工作流是
"改主机代码 → scp 到板子 → ssh 跑 → 取回结果"，这里把这条链子固化成一条命令。
（`--host` 可以完全不碰板子，用 numpy 参考实现跑同一张图，用来做对照。）

画质口径（与部署包、`check_pack_ref.py`、`prep_input.py` 完全一致，不要改）
    输入量化：  x_q = clip(rint(x * 127), 0, 127)            x ∈ [0,1]，CHW int8
    输出反量化：x̂   = clip(y * scales[io.out1], 0, 1)        y 为 int8
    s_in  = 0.007874015748031496 = 1/127  （所以量化是"除以 127"而不是"除以 0.0078…"）

注意两条上游铁律（写在 docs/训练/说明_交付_20260910.md）：
  1. **不要在部署前 resize** —— 摩尔纹频率与采集分辨率绑定，下采样会折叠峰值频率；
  2. 画幅必须是 32 的倍数（网络有 2 次 pixel_unshuffle + 4 级下采样）。
因此默认只做**原生像素裁剪**；图比画幅小时直接报错，除非显式给 `--allow-resize`。

用法
    # 板端（默认）：裁 640×640 → 传到板子 → 跑 → 取回 → 出图
    python3 tools/run_image.py --image 照片.jpg --out-dir out/

    # 主机对照（不碰板子，用 numpy 参考实现）
    python3 tools/run_image.py --image 照片.jpg --out-dir out/ --host

    # 只想看裁下来的区域是否合适
    python3 tools/run_image.py --image 照片.jpg --out-dir out/ --dry-run
"""
import argparse
import json
import math
import os
import shlex
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from img_io import load_image_np            # noqa: E402

PS_ROOT = os.path.abspath(os.path.join(HERE, ".."))
DEFAULT_MANIFEST = os.path.join(PS_ROOT, "manifest_640.json")
DEFAULT_BOARD = "xilinx@192.168.7.2"
DEFAULT_BOARD_DIR = "/home/xilinx/demoir_new"


# ---------------------------------------------------------------- 量化/反量化
def quantize(x01):
    """(H,W,3) 浮点 [0,1] → (3,H,W) int8。与 prep_input.py / export_network.py 同源。"""
    return np.clip(np.rint(np.asarray(x01, dtype=np.float64) * 127.0),
                   0, 127).astype(np.int8).transpose(2, 0, 1)


def dequantize(y_q, s_out):
    """(3,H,W) int8 → (H,W,3) 浮点 [0,1]。"""
    y = np.asarray(y_q, dtype=np.float64) * float(s_out)
    return np.clip(y, 0.0, 1.0).transpose(1, 2, 0)


def psnr_db(a, b, peak=1.0):
    mse = float(np.mean((np.asarray(a, np.float64) - np.asarray(b, np.float64)) ** 2))
    return 99.0 if mse <= 0 else 10.0 * math.log10(peak * peak / mse)


# ---------------------------------------------------------------- 出图
def save_png(rgb01, path):
    from PIL import Image
    arr = np.clip(np.asarray(rgb01, np.float64), 0, 1)
    Image.fromarray((arr * 255.0 + 0.5).astype(np.uint8)).save(path)


def save_side_by_side(a, b, path, gap=8):
    from PIL import Image
    ia = Image.fromarray((np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8))
    ib = Image.fromarray((np.clip(b, 0, 1) * 255 + 0.5).astype(np.uint8))
    w, h = ia.size
    sheet = Image.new("RGB", (w * 2 + gap, h), (255, 255, 255))
    sheet.paste(ia, (0, 0))
    sheet.paste(ib, (w + gap, 0))
    sheet.save(path)


# ---------------------------------------------------------------- 板端执行
def _sh(cmd, check=True, **kw):
    """统一的子进程封装。**不要用 check=True 把 stderr 吞掉**——
    2026-10-23 就因为它在板端失败时只抛了一句 "exit status 1"，
    把真正的原因（板端 traceback）丢掉了，排查白白多花一小时。"""
    r = subprocess.run(cmd, shell=isinstance(cmd, str),
                       capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        raise SystemExit("命令失败（%d）：%s\n--- stdout ---\n%s\n--- stderr ---\n%s"
                         % (r.returncode,
                            cmd if isinstance(cmd, str) else " ".join(cmd),
                            r.stdout[-3000:], r.stderr[-3000:]))
    return r


def preflight_board(args):
    """开跑前的板端自检。

    2026-10-23 两次把板子跑挂，事后看都是"上一次的 run_network_on_board 还活着"——
    它握着 overlay/DMA，新的一次再来抢就会把 PS 卡死（表现为 ping 得通但 SSH 无路由，
    只能断电）。所以这里先查、再问、必要时强杀，杀不掉就直接拒绝开跑，
    而不是硬着头皮把板子再挂一次。
    """
    print("[0/4] 板端自检 %s …" % args.board)
    r = _sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", args.board,
             "printf 'xilinx\\n' | sudo -S -p '' bash -lc "
             "'pgrep -af run_network_on_board | grep -v pgrep | head -5'"],
            check=False)
    if r.returncode != 0 and not r.stdout.strip():
        raise SystemExit("板子连不上（%s）。确认已上电/重启后再试。\n%s"
                         % (args.board, r.stderr[-500:]))
    stale = [ln for ln in r.stdout.splitlines() if "run_network_on_board" in ln]
    if stale:
        print("      发现 %d 个残留进程，先清理（否则会抢 overlay 导致板子卡死）：" % len(stale))
        for ln in stale:
            print("        " + ln.strip()[:110])
        _sh(["ssh", "-o", "BatchMode=yes", args.board,
             "printf 'xilinx\\n' | sudo -S -p '' bash -lc "
             "'pkill -f run_network_on_board; sleep 2; pgrep -c run_network_on_board || true'"],
            check=False)
        chk = _sh(["ssh", "-o", "BatchMode=yes", args.board,
                   "printf 'xilinx\\n' | sudo -S -p '' bash -lc "
                   "'pgrep -c run_network_on_board || echo 0'"], check=False)
        if chk.stdout.strip() not in ("0", ""):
            raise SystemExit("残留进程杀不掉，拒绝开跑（避免把板子挂死）：\n" + chk.stdout[:500])
        print("      已清理")


def run_on_board(inp_q, args, h, w):
    """把 input_q 传到板子、跑整网、把输出取回来。返回 (3,H,W) int8。"""
    preflight_board(args)
    bd = args.board_dir
    remote_in = "%s/_run_in_q.bin" % bd
    remote_out = "/tmp/_run_out.bin"

    print("[1/4] 上传输入 %d 字节 → %s" % (inp_q.size, args.board))
    local_tmp = os.path.join(args.out_dir, "_input_q.bin")
    inp_q.tofile(local_tmp)
    _sh(["scp", "-q", local_tmp, "%s:%s" % (args.board, remote_in)])

    cmd = ("cd %s && printf 'xilinx\\n' | sudo -S -p '' bash -lc %s"
           % (bd, shlex.quote(
               "python3 -u run_network_on_board.py "
               "--pp-ops %s --bit %s --manifest %s --pack %s --input %s "
               "--fclk %d --dump-out %s"
               % (args.pp_ops, args.bit, args.manifest_name, args.pack,
                  remote_in, args.fclk, remote_out))))
    print("[2/4] 板端推理（约 3 分钟 @140MHz）…")
    r = _sh(["ssh", "-o", "BatchMode=yes", args.board, cmd], check=False)
    tail = [ln for ln in r.stdout.splitlines()
            if any(k in ln for k in ("整网完成", "不一致", "Traceback", "Error"))]
    for ln in tail[-3:]:
        print("      " + ln.strip())
    if "整网完成" not in r.stdout:
        raise SystemExit("板端没有跑到完成（rc=%d）。下面是板端最后的输出：\n"
                         "--- stdout ---\n%s\n--- stderr ---\n%s"
                         % (r.returncode, r.stdout[-3000:], r.stderr[-3000:]))

    print("[3/4] 取回输出")
    local_out = os.path.join(args.out_dir, "_out_q.bin")
    _sh(["scp", "-q", "%s:%s" % (args.board, remote_out), local_out])
    y = np.fromfile(local_out, dtype=np.int8)
    if y.size != 3 * h * w:
        raise SystemExit("输出大小 %d ≠ 3*%d*%d，manifest 与 bit 的画幅不一致" % (y.size, h, w))
    _sh(["ssh", "-o", "BatchMode=yes", args.board, "rm -f %s %s" % (remote_in, remote_out)])
    return y.reshape(3, h, w)


def run_on_host(inp_q, args, h, w):
    """不碰板子：用 numpy 参考实现（RefEngine）跑同一条网络。

    这条路径的存在意义：① 没板子也能验证"裁剪→量化→推理→反量化→出图"整条管线；
    ② 板端结果可以拿它做对照。数值上与板上不完全逐位相同（板端走 pp 的 Q16 舍入，
    这里可以选 PPSim 复现，或完全走 numpy 得到"逐位语义"的答案）。
    """
    sys.path.insert(0, PS_ROOT)
    sys.path.insert(0, os.path.join(PS_ROOT, "pynq"))
    from runner import Runner, RefEngine, NumpyArena, load_pack   # noqa: E402

    pack_dir = args.pack
    if not os.path.isdir(pack_dir):
        for cand in (os.path.join(PS_ROOT, "deploy", args.pack),
                     os.path.join(PS_ROOT, args.pack)):
            if os.path.isdir(cand):
                pack_dir = cand
                break
        else:
            raise SystemExit("主机模式找不到权重包：试过 %s 与 deploy/%s" % (args.pack, args.pack))
    with open(args.manifest, "r", encoding="utf-8") as f:
        m = json.load(f)
    arena = NumpyArena(m)
    index, blob = load_pack(pack_dir)
    eng = RefEngine(index, blob, arena)
    print("[2/2] 主机推理（RefEngine，numpy 参考实现）…")
    r = Runner(args.manifest, pack_dir, engine=eng, arena=arena)
    if args.host_pp:
        # 用 PPSim 复现板端 pp 的定点语义（Q16 系数），结果与板上同一路
        from pp_engine import PPSim                             # noqa: E402
        r.pp = PPSim()
        r.pp_ops_en = set(x.strip() for x in args.host_pp.split(",") if x.strip())
        print("      使用 PPSim 复现 pp 语义：%s" % sorted(r.pp_ops_en))
    b = m["buffers"][m["io"]["t_in"]]
    if (b["c"], b["h"], b["w"]) != (inp_q.shape[0], inp_q.shape[1], inp_q.shape[2]):
        raise SystemExit("输入张量形状 %s 与 manifest 的 %s 不符"
                         % (inp_q.shape, (b["c"], b["h"], b["w"])))
    r.run(np.asarray(inp_q, dtype=np.int8))
    out = np.array(arena.view(m["io"]["out1"]), dtype=np.int8).copy()
    return out


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="任意图片 → 去摩尔纹 → PNG")
    ap.add_argument("--image", required=True, help="输入照片（任意尺寸）")
    ap.add_argument("--out-dir", default="run_out", help="输出目录（PNG 与中间文件都放这里）")
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST,
                    help="决定画幅的清单（默认 manifest_640.json，画幅 640×640）")
    ap.add_argument("--pack", default="weights_pack",
                    help="板上权重包目录名（相对板端工作目录）")
    ap.add_argument("--board", default=DEFAULT_BOARD, help="板子的 SSH 目标")
    ap.add_argument("--board-dir", default=DEFAULT_BOARD_DIR, help="板端工作目录")
    ap.add_argument("--bit", default="design_1.bit", help="板端 bit 文件名（hwh 必须同名同目录）")
    ap.add_argument("--fclk", type=int, default=140, help="板端 FCLK0 频率（交付 bit 用 140）")
    ap.add_argument("--pp-ops", default="sam_sum,eca",
                    help="走 pp 的算子（交付 bit 为 sam_sum,eca；留空则全走 numpy，慢但逐位）")
    ap.add_argument("--crop", choices=["center", "topleft"], default="center")
    ap.add_argument("--allow-resize", action="store_true",
                    help="图比画幅小时允许缩放（默认禁止，见文件头两条铁律）")
    ap.add_argument("--host", action="store_true",
                    help="不碰板子：用 numpy 参考实现（RefEngine）跑同一条网络")
    ap.add_argument("--host-pp", default=None,
                    help="主机模式下用 PPSim 复现 pp 语义的算子，如 sam_sum,eca；"
                         "留空 = 完全走 numpy（逐位语义）")
    ap.add_argument("--dry-run", action="store_true", help="只做裁剪与量化，不推理")
    a = ap.parse_args()

    if not os.path.isfile(a.image):
        raise SystemExit("找不到图片: %s" % a.image)
    os.makedirs(a.out_dir, exist_ok=True)
    a.manifest_name = os.path.basename(a.manifest)

    with open(a.manifest, "r", encoding="utf-8") as f:
        m = json.load(f)
    h, w = m["input_hw"]
    if h % 32 or w % 32:
        raise SystemExit("清单画幅 %dx%d 不是 32 的倍数，网络跑不了" % (w, h))
    print("画幅: %d×%d（来自 %s）" % (w, h, a.manifest_name))

    # ---- 1) 裁 + 量化
    x01 = load_image_np(a.image, h, w, a.crop, a.allow_resize)
    inp_q = quantize(x01)
    save_png(x01, os.path.join(a.out_dir, "1_input.png"))
    print("输入已量化: %s  dtype=%s 范围=[%d,%d]"
          % (inp_q.shape, inp_q.dtype, inp_q.min(), inp_q.max()))

    if a.dry_run:
        print("--dry-run：只输出 1_input.png，不做推理")
        return

    # ---- 2) 推理
    if a.host:
        y_q = run_on_host(inp_q, a, h, w)
    else:
        y_q = run_on_board(inp_q, a, h, w)

    # ---- 3) 反量化 + 出图
    pack_index = None
    for cand in (os.path.join(a.out_dir, "index.json"),
                 os.path.join(PS_ROOT, "deploy", a.pack, "index.json"),
                 os.path.join(a.pack, "index.json")):
        if os.path.isfile(cand):
            pack_index = cand
            break
    if pack_index is None:
        if a.host:
            raise SystemExit("主机模式找不到 index.json（应在 %s/index.json）" % a.pack)
        print("[4/4] 本地没有 index.json，从板子取一份")
        pack_index = os.path.join(a.out_dir, "index.json")
        _sh(["scp", "-q", "%s:%s/%s/index.json" % (a.board, a.board_dir, a.pack), pack_index])
    with open(pack_index, "r", encoding="utf-8") as f:
        scales = json.load(f)["scales"]
    out_name = m["io"]["out1"]
    s_out = float(scales[out_name])
    y01 = dequantize(y_q, s_out)
    save_png(y01, os.path.join(a.out_dir, "2_output.png"))
    save_side_by_side(x01, y01, os.path.join(a.out_dir, "3_compare.png"))

    print("[4/4] 完成")
    print("  输出张量 %s，标度 s_out = %.12g" % (out_name, s_out))
    print("  输入范围 [%.3f, %.3f] → 输出范围 [%.3f, %.3f]"
          % (x01.min(), x01.max(), y01.min(), y01.max()))
    print("  文件：1_input.png / 2_output.png / 3_compare.png  （在 %s）" % a.out_dir)


if __name__ == "__main__":
    main()
