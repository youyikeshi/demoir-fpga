#!/usr/bin/env python3
"""显示构图主机自检：4 种显示模式 x 3 组输入 => PNG 落盘 + 像素级判据。

规范来源：``docs/camera/03_显示构图规范_纯PS.md``
被测模块：``ps/pynq/display_layout.py``（纯 numpy，不依赖 pynq，主机可直接跑）

用法
----
::

    python3 tools/display_selftest.py                 # 默认输出到 runs/display_layout/
    python3 tools/display_selftest.py --out /tmp/x     # 自定义输出目录

退出码
------
``0`` = 全部判据通过（末行打印 ``PASS``）；非 ``0`` = 有失败，并逐条打印哪条失败。

判据（全过才 PASS）
-------------------
C1  输出形状 ``(H,W,3)`` / ``uint8`` / C 连续
C2  ``out[y0, 0] == orig[0,0]`` 且内容区中线右侧第一列 ``out[y0, W//2] == proc[0,0]``
C3  第 0 行与第 ``H-1`` 行全黑
C4  黑边高度 == 公式值（规范 §1.1 的 40/40、60/60、128/128、80/80），上下差 <= 1
C5  纯色输入下：全图只有 ``(255,0,0)`` / ``(0,0,255)`` / ``(0,0,0)`` 三种颜色，无第三种来源
C6  同输入两次调用 ``np.array_equal`` 为真（纯函数、逐字节相同）
C7  内容区与**独立实现**（``np.repeat`` 绘制法）逐字节一致 —— 同时证明"不镜像/不错位/不拉伸"
C8  缩放比实测 == ``W/512``，即 ``2.5 / 3.75 / 2.0 / 5.0``（由输出图像几何反推，不看内部表）
C9  各向同性：横向与纵向的 32 源像素跨度都等于 ``32*s``（棋盘格实测，防止被拉伸变形）
C10 镜像敏感性（meta）：把 ``orig`` 水平翻转后输出必须不同 —— 证明 C2/C7 对方向敏感，不是"怎么都对"

输入三组
--------
- ``color``   纯色：``orig`` 全红 ``(255,0,0)``、``proc`` 全蓝 ``(0,0,255)`` —— 机械验"左红右蓝、黑边纯黑"
- ``checker`` 8x8 棋盘（每格 32 px）—— 肉眼验缩放是否变形/锯齿，并供 C8/C9 精确测比例
- ``photo``   真实图（在 ``runs/`` 或 ``pynq_specialized_code/`` 里找 >=256x256 的真实图取
  ``256x256`` 中心裁剪，取两张不同的图分别作为 ``orig`` / ``proc``）；找不到则合成
  "渐变 + 网格 + 文字块"（纯 numpy，无字体依赖，跨机逐字节可复现）

跨机复现（上板 md5 用得上）
--------------------------
``--dump-inputs`` 会把用到的 6 张 256² 输入原样落成 PNG；板端用 ``--from-inputs <该目录>``
读取，即可保证主机/板端输入逐字节相同（避免板端找不到同一批真实图而走了合成分支）。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]

# --- 让 python3 tools/display_selftest.py 能 import 到被测模块 -----------------
for _cand in (ROOT / "ps" / "pynq", ROOT / "pynq_specialized_code" / "ps" / "pynq"):
    if (_cand / "display_layout.py").is_file():
        sys.path.insert(0, str(_cand))
        break
else:  # pragma: no cover
    print("FATAL: 找不到 ps/pynq/display_layout.py", file=sys.stderr)
    raise SystemExit(2)

import display_layout as dl  # noqa: E402

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover
    print(f"FATAL: 需要 PIL 保存 PNG（{exc}）", file=sys.stderr)
    raise SystemExit(2)


# ===========================================================================
# 判据记录
# ===========================================================================
@dataclass
class Check:
    cid: str
    desc: str
    ok: bool | None  # None = 该输入不适用（不计入失败）
    detail: str = ""


@dataclass
class Case:
    """一个 (显示模式 x 输入) 组合的结果。"""

    W: int
    H: int
    input_name: str
    top_bar: int
    bottom_bar: int
    scale: float
    checks: list[Check] = field(default_factory=list)
    png_rel: str = ""
    raw_md5: str = ""  # 整幅原始像素的 md5（与 PNG 编码器无关，跨机可逐字节比对）

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.ok is False]


# ===========================================================================
# 输入构造
# ===========================================================================
def make_color_inputs() -> tuple[np.ndarray, np.ndarray]:
    """纯色：左全红、右全蓝。机械验证"左红右蓝 + 黑边纯黑 + 无第三种颜色"。"""
    orig = np.zeros((dl.SIDE, dl.SIDE, 3), np.uint8)
    orig[...] = (255, 0, 0)
    proc = np.zeros((dl.SIDE, dl.SIDE, 3), np.uint8)
    proc[...] = (0, 0, 255)
    return orig, proc


CELL = 32  # 8x8 棋盘，每格 32 px


def make_checker_inputs() -> tuple[np.ndarray, np.ndarray]:
    """8x8 棋盘（每格 32 px）：两套配色，左右来源一眼可分，且可精确量缩放比。"""
    yy, xx = np.mgrid[0 : dl.SIDE, 0 : dl.SIDE]
    cell = ((yy // CELL) + (xx // CELL)) % 2  # 0/1 交替
    orig = np.zeros((dl.SIDE, dl.SIDE, 3), np.uint8)
    orig[...] = (20, 20, 20)
    orig[cell == 1] = (235, 235, 235)
    proc = np.zeros((dl.SIDE, dl.SIDE, 3), np.uint8)
    proc[...] = (0, 40, 0)
    proc[cell == 1] = (60, 220, 60)
    return orig, proc


def make_synth_inputs() -> tuple[np.ndarray, np.ndarray]:
    """合成"渐变 + 网格 + 文字块"：纯 numpy，无字体/无随机，跨机逐字节可复现。

    文字块用"横条 + 竖条"拼出 4 行类文字纹样（不依赖任何字体渲染，故与平台无关）。
    """
    yy, xx = np.mgrid[0 : dl.SIDE, 0 : dl.SIDE].astype(np.float32)

    def _base(tint: tuple[int, int, int]) -> np.ndarray:
        # 对角渐变 + 轻微按行明暗，作为"底图"
        g = (xx * 0.55 + yy * 0.45) / (dl.SIDE - 1)  # 0..1
        img = np.empty((dl.SIDE, dl.SIDE, 3), np.float32)
        for c in range(3):
            img[..., c] = g * 200.0 + tint[c]
        return img

    def _grid(img: np.ndarray) -> None:
        # 每 32 px 一条亮线（用于肉眼检查缩放是否有锯齿/变形）
        for k in range(0, dl.SIDE + 1, 32):
            kk = min(k, dl.SIDE - 1)
            img[kk, :, :] = (250, 250, 250)
            img[:, kk, :] = (250, 250, 250)

    def _text_block(img: np.ndarray, y0: int, widths: tuple[int, ...], color) -> None:
        # 一行"文字"= 若干个横条（词）之间留竖缝，全部是整数像素块 => 完全确定
        x = 12
        for i, w in enumerate(widths):
            h = 14 if i % 2 == 0 else 9
            img[y0 : y0 + h, x : x + w, :] = color
            x += w + 8
            if x >= dl.SIDE - 12:
                break

    orig = _base((18, 26, 46))
    _grid(orig)
    _text_block(orig, 52, (54, 30, 66), (255, 226, 90))
    _text_block(orig, 92, (40, 70, 46), (240, 240, 240))
    _text_block(orig, 132, (66, 26, 58), (120, 220, 255))
    _text_block(orig, 172, (30, 60, 44, 34), (255, 150, 120))

    proc = _base((40, 16, 16))
    _grid(proc)
    # 右图在文字块位置上做"去摩尔纹"式的块替换（平滑色块），肉眼可与左图区分
    for y0, y1 in ((44, 84), (88, 128), (128, 168), (168, 208)):
        proc[y0:y1, 8 : dl.SIDE - 8, :] = proc[y0:y1, 8 : dl.SIDE - 8, :] * 0.55 + 90.0
    return orig.astype(np.uint8), proc.astype(np.uint8)


_IMG_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")


def find_real_images(max_probe: int = 300) -> list[Path]:
    """在 runs/ 与 pynq_specialized_code/ 里找 >=256x256 的真实图（确定性顺序）。"""
    found: list[Path] = []
    for rel in ("runs", "pynq_specialized_code"):
        base = ROOT / rel
        if not base.is_dir():
            continue
        try:
            cands = sorted(
                p for p in base.rglob("*") if p.suffix.lower() in _IMG_EXTS and p.is_file()
            )
        except OSError:  # pragma: no cover
            continue
        probed = 0
        for p in cands:
            if probed >= max_probe:
                break
            if p.stat().st_size < 4096:  # 太小不可能 >=256x256 的有意义图
                continue
            probed += 1
            try:
                with Image.open(p) as im:
                    if im.size[0] >= dl.SIDE and im.size[1] >= dl.SIDE:
                        found.append(p)
                        if len(found) >= 4:
                            return found
            except Exception:
                continue
    return found


def _center_crop_rgb(path: Path, size: int, dx: int = 0) -> np.ndarray:
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        left = (w - size) // 2 + dx
        top = (h - size) // 2
        left = max(0, min(left, w - size))
        top = max(0, min(top, h - size))
        crop = im.crop((left, top, left + size, top + size))
        return np.ascontiguousarray(np.asarray(crop, dtype=np.uint8))


def make_photo_inputs() -> tuple[np.ndarray, np.ndarray, str]:
    """真实图：取两张不同真实图的 256² 中心裁剪；找不到就回退合成图。"""
    real = find_real_images()
    if len(real) >= 2:
        orig = _center_crop_rgb(real[0], dl.SIDE)
        proc = _center_crop_rgb(real[1], dl.SIDE)
        prov = f"real:{real[0].relative_to(ROOT)}, {real[1].relative_to(ROOT)}"
        return orig, proc, prov
    if len(real) == 1:
        orig = _center_crop_rgb(real[0], dl.SIDE, dx=-64)
        proc = _center_crop_rgb(real[0], dl.SIDE, dx=+64)
        prov = f"real(2 crops):{real[0].relative_to(ROOT)}"
        return orig, proc, prov
    orig, proc = make_synth_inputs()
    return orig, proc, "synth:渐变+网格+文字块（本机未找到 >=256x256 的真实图）"


def build_inputs() -> dict[str, tuple[np.ndarray, np.ndarray, str]]:
    o_c, p_c = make_color_inputs()
    o_k, p_k = make_checker_inputs()
    o_p, p_p, prov = make_photo_inputs()
    return {
        "color": (o_c, p_c, "synthetic:纯色 左红(255,0,0) / 右蓝(0,0,255)"),
        "checker": (o_k, p_k, "synthetic:8x8 棋盘，每格 32 px"),
        "photo": (o_p, p_p, prov),
    }


def dump_inputs(inputs: dict, outdir: Path) -> list[str]:
    """把 6 张 256² 输入原样落成 PNG（跨机逐字节复现用）。"""
    d = outdir / "inputs"
    d.mkdir(parents=True, exist_ok=True)
    written = []
    for name, (orig, proc, _prov) in inputs.items():
        for tag, arr in (("orig", orig), ("proc", proc)):
            p = d / f"{name}_{tag}.png"
            Image.fromarray(arr, mode="RGB").save(p)
            written.append(str(p.relative_to(outdir)))
    return written


def load_inputs(indir: Path) -> dict[str, tuple[np.ndarray, np.ndarray, str]]:
    """从 ``--dump-inputs`` 落盘的目录读回输入（板端复现用）。"""
    inputs = {}
    for name in ("color", "checker", "photo"):
        o = indir / f"{name}_orig.png"
        p = indir / f"{name}_proc.png"
        if not (o.is_file() and p.is_file()):
            raise SystemExit(f"FATAL: {indir} 里缺少 {name}_orig.png / {name}_proc.png")
        with Image.open(o) as im:
            a = np.ascontiguousarray(np.asarray(im.convert("RGB"), np.uint8))
        with Image.open(p) as im:
            b = np.ascontiguousarray(np.asarray(im.convert("RGB"), np.uint8))
        inputs[name] = (a, b, f"loaded:{indir}")
    return inputs


# ===========================================================================
# 独立参考实现（"绘制法"）：与 compose 的"收集法"数学等价但代码路径完全不同
# ===========================================================================
def _paint_counts(n_src: int, n_dst: int) -> np.ndarray:
    """绘制法：源列 k 占据显示列 [ceil(k*n_dst/n_src), ceil((k+1)*n_dst/n_src))。"""
    k = np.arange(n_src, dtype=np.int64)
    lo = (k * n_dst + n_src - 1) // n_src  # ceil(k*n_dst/n_src)
    hi = ((k + 1) * n_dst + n_src - 1) // n_src
    return hi - lo


def reference_compose(orig: np.ndarray, proc: np.ndarray, W: int, H: int) -> np.ndarray:
    """参照实现：用 np.repeat(绘制法) + Fraction 精确算术，独立复算整幅画面。"""
    s = Fraction(W, dl.CANVAS_W)  # s = W/512
    content_h = int(dl.CANVAS_H * s)  # 256*s（精确有理数取整）
    content_w = W
    x0 = 0
    if content_h > H:  # 回退：按高度适配
        s = Fraction(H, dl.CANVAS_H)
        content_h = H
        content_w = int(dl.CANVAS_W * s)
        x0 = (W - content_w) // 2
    y0 = (H - content_h) // 2

    canvas = np.concatenate([orig, proc], axis=1)  # (256, 512, 3)
    cy = _paint_counts(dl.CANVAS_H, content_h)
    cx = _paint_counts(dl.CANVAS_W, content_w)
    if cy.sum() != content_h or cx.sum() != content_w:  # pragma: no cover
        raise AssertionError("绘制法计数不自洽")
    # ★ 2026-11-02 板端（armv7 + numpy 1.21.5）实测：np.repeat 的 repeats 若是
    #   numpy int64 标量/数组，32 位 numpy 会报
    #   "Cannot cast array data from dtype('int64') to dtype('int32')"；
    #   主机 x86_64（int64 原生）看不出来 ⇒ 必须显式转 Python int。
    content = np.repeat(np.repeat(canvas, [int(v) for v in cy], axis=0),
                        [int(v) for v in cx], axis=1)
    out = np.zeros((H, W, 3), np.uint8)
    out[y0 : y0 + content_h, x0 : x0 + content_w, :] = content
    return out


# ===========================================================================
# 判据
# ===========================================================================
def fmt_scale(x: float) -> str:
    """2.5 / 3.75 / 2.0 / 5.0 —— 始终保留一位小数，避免 2.0 被打成 2。"""
    t = f"{x:g}"
    return t if "." in t else t + ".0"


def _run_lengths(mask: np.ndarray) -> np.ndarray:
    """一维布尔序列的连续游程长度。"""
    if mask.size == 0:
        return np.zeros(0, np.int64)
    b = np.concatenate(([0], np.flatnonzero(np.diff(mask.astype(np.int8))) + 1, [mask.size]))
    return np.diff(b)


def check_case(out: np.ndarray, orig: np.ndarray, proc: np.ndarray,
               spec: dl.ModeSpec, input_name: str) -> Case:
    W, H = spec.W, spec.H
    case = Case(W, H, input_name, spec.top_bar, spec.bottom_bar, spec.scale)
    p = dl.plan(W, H)
    y0, Hc = p.y0, p.content_h
    add = case.checks.append

    # --- C1 形状 / dtype / C 连续 ----------------------------------------
    shape_ok = out.shape == (H, W, 3)
    dtype_ok = out.dtype == np.uint8
    contig_ok = bool(out.flags["C_CONTIGUOUS"])
    add(Check("C1", "输出 (H,W,3)/uint8/C 连续", shape_ok and dtype_ok and contig_ok,
              f"shape={out.shape} dtype={out.dtype} contig={contig_ok}"))

    if not (shape_ok and dtype_ok):  # 后面判据都依赖形状，直接返回
        return case

    # --- C2 左来源 / 右来源 ----------------------------------------------
    left_ok = np.array_equal(out[y0, 0], orig[0, 0])
    right_ok = np.array_equal(out[y0, W // 2], proc[0, 0])
    add(Check("C2", "内容左上=orig[0,0]；中线右侧第一列=proc[0,0]", left_ok and right_ok,
              f"左={out[y0, 0].tolist()} vs orig[0,0]={orig[0, 0].tolist()}；"
              f"右={out[y0, W // 2].tolist()} vs proc[0,0]={proc[0, 0].tolist()}"))

    # --- C3 首末行全黑 ---------------------------------------------------
    row0_black = not out[0].any()
    rowN_black = not out[H - 1].any()
    add(Check("C3", "第 0 行与第 H-1 行全黑", row0_black and rowN_black,
              f"row0 非零={int(np.count_nonzero(out[0]))}，row{H - 1} 非零="
              f"{int(np.count_nonzero(out[H - 1]))}"))

    # --- C4 黑边高度 == 公式值 -------------------------------------------
    leading = 0
    while leading < H and not out[leading].any():
        leading += 1
    trailing = 0
    while trailing < H - leading and not out[H - 1 - trailing].any():
        trailing += 1
    band_zero_ok = (not out[:y0].any()) and (not out[y0 + Hc:].any())
    if input_name in ("color", "checker"):
        # 内容行不可能全黑 => 扫描法可靠，要求严格等于规范值
        exact_ok = leading == spec.top_bar and trailing == spec.bottom_bar
        detail = f"实测上/下={leading}/{trailing}，规范={spec.top_bar}/{spec.bottom_bar}"
    else:
        exact_ok = True  # 真实图可能自带全零行，扫描法不适用，交给 C7 逐字节兜底
        detail = (f"扫描上/下={leading}/{trailing}（真实图可能含全零行，不作严格判据）；"
                  f"结构上黑边 {y0}/{H - Hc - y0}")
    add(Check("C4", "黑边高度=公式值（上下差<=1）",
              band_zero_ok and exact_ok and abs(spec.top_bar - spec.bottom_bar) <= 1,
              detail))

    # --- C5 纯色输入下无第三种颜色 ---------------------------------------
    if input_name == "color":
        colors = {tuple(c) for c in np.unique(out.reshape(-1, 3), axis=0).tolist()}
        allow = {(0, 0, 0), tuple(orig[0, 0].tolist()), tuple(proc[0, 0].tolist())}
        extra = colors - allow
        content_colors = np.unique(out[y0:y0 + Hc].reshape(-1, 3), axis=0)
        content_only = {tuple(c) for c in content_colors.tolist()}
        add(Check("C5", "纯色输入：全图仅 {红,蓝,黑}，无第三种来源",
                  not extra and content_only <= allow - {(0, 0, 0)},
                  f"出现颜色={sorted(colors)}，多出={sorted(extra)}，"
                  f"内容区颜色数={len(content_only)}"))
    else:
        add(Check("C5", "纯色输入：全图仅 {红,蓝,黑}", None, f"{input_name} 非纯色输入，不适用"))

    # --- C6 双跑逐字节相同（纯函数）--------------------------------------
    #   两次**独立**调用比较：不许共享内存（否则说明返回了内部缓存缓冲）、逐字节相同
    a1 = dl.compose(orig, proc, W=W, H=H)
    a2 = dl.compose(orig, proc, W=W, H=H)
    same = np.array_equal(a1, a2)
    no_alias = not np.shares_memory(a1, a2)
    add(Check("C6", "同输入两次独立调用逐字节相同（无共享内存）", same and no_alias,
              f"array_equal={same}，no_alias={no_alias}，"
              f"且与本次待检输出一致={np.array_equal(out, a1)}"))

    # --- C7 与独立"绘制法"参考实现逐字节一致 -----------------------------
    ref = reference_compose(orig, proc, W, H)
    same = np.array_equal(out, ref)
    diff = "" if same else f"，不同像素 {int(np.count_nonzero(out != ref))}"
    add(Check("C7", "内容区与独立实现(np.repeat 绘制法)逐字节一致", same, f"array_equal={same}{diff}"))

    # --- C8 缩放比实测 == W/512 ------------------------------------------
    #   棋盘：32 源像素一格 => 一格应横跨 32*s 个显示像素；左半 8 格应正好铺满 W//2
    if input_name == "checker":
        row = out[y0 + Hc // 2, : W // 2]
        codes = np.any(row != row[0], axis=1)
        runs = _run_lengths(codes)
        exp_run = 32 * spec.scale
        ratio_meas = (W // 2) / dl.SIDE  # 左半显示宽 / 左半源宽
        runs_ok = runs.size == 8 and bool(np.all(runs == int(exp_run))) and runs.sum() == W // 2
        add(Check("C8", f"缩放比实测 == W/512 == {fmt_scale(spec.scale)}",
                  runs_ok and abs(ratio_meas - spec.scale) < 1e-12,
                  f"左半 {W // 2} px 承载 256 源 px => {ratio_meas:g}；"
                  f"8 格游程={runs.tolist()}（期望每格 {int(exp_run)}）"))

        # --- C9 各向同性：纵向同样 32*s，8 格铺满 Hc ---------------------
        col = out[y0 : y0 + Hc, 0]
        vcodes = np.any(col != col[0], axis=1)
        vruns = _run_lengths(vcodes)
        add(Check("C9", "横纵等比：32 源像素横向=纵向=32*s（无拉伸）",
                  vruns.size == 8 and bool(np.all(vruns == int(exp_run))) and vruns.sum() == Hc,
                  f"纵向 8 格游程={vruns.tolist()}，合计={int(vruns.sum())}（期望 {Hc}）"))
    else:
        add(Check("C8", "缩放比实测 == W/512", None, f"{input_name} 非棋盘输入，不适用"))
        add(Check("C9", "横纵等比（无拉伸）", None, f"{input_name} 非棋盘输入，不适用"))

    # --- C10 镜像敏感性（meta：证明判据对方向敏感）------------------------
    if input_name != "color":
        mirrored = dl.compose(np.ascontiguousarray(orig[:, ::-1]), proc, W=W, H=H)
        add(Check("C10", "镜像敏感性：orig 水平翻转后输出必须不同", not np.array_equal(out, mirrored),
                  f"翻转后仍相同={np.array_equal(out, mirrored)}"))
    else:
        add(Check("C10", "镜像敏感性", None, "纯色输入左右镜像不可辨，不适用"))

    return case


# ===========================================================================
# 主流程
# ===========================================================================

# ===========================================================================
# 判据自检（meta）：把输出故意弄坏，确认 check_case 真能抓到
# ===========================================================================
def _corruptions(base: np.ndarray, orig: np.ndarray, proc: np.ndarray,
                 spec: dl.ModeSpec) -> list[tuple[str, np.ndarray]]:
    """在正确的输出上注入各种"典型错法"，每种都必须被至少一条判据抓到。"""
    W, H = spec.W, spec.H
    p = dl.plan(W, H)
    y0, Hc = p.y0, p.content_h
    Wc = p.content_w
    canvas = np.concatenate([orig, proc], axis=1)  # (256,512,3)
    items: list[tuple[str, np.ndarray]] = []

    items.append(("左右镜像", np.ascontiguousarray(base[:, ::-1])))
    items.append(("orig/proc 互换", dl.compose(proc, orig, W=W, H=H)))

    shifted = np.zeros_like(base)
    shifted[2:] = base[:-2]
    items.append(("整幅下移 2px（黑边错位）", shifted))

    blur = ((base.astype(np.uint16) + np.roll(base, 1, 0) + np.roll(base, 1, 1)
             + np.roll(base, 1, (0, 1))) // 4).astype(np.uint8)
    items.append(("均值模糊（模拟浮点插值）", blur))

    dirt = base.copy()
    dirt[: max(1, y0)] = 1
    items.append(("黑边写成 (1,1,1)", dirt))

    items.append(("输出非 C 连续（Fortran 序）", np.asfortranarray(base)))

    stretched = np.zeros((H, W, 3), np.uint8)
    idx_v = np.clip((np.arange(H) * Hc) // H, 0, Hc - 1)  # 纵向撑满、去掉黑边
    stretched[:] = base[y0 : y0 + Hc][idx_v]
    items.append(("拉伸变形（纵向撑满，sx≠sy）", stretched))

    # 正确的最近邻取整是 floor(x*n_src/n_dst)；这里故意改成 round():
    #   round(x*n_src/n_dst) = (2*x*n_src + n_dst) // (2*n_dst)
    rx = np.clip((2 * np.arange(Wc) * dl.CANVAS_W + Wc) // (2 * Wc), 0, dl.CANVAS_W - 1)
    ry = np.clip((2 * np.arange(Hc) * dl.CANVAS_H + Hc) // (2 * Hc), 0, dl.CANVAS_H - 1)
    round_off = np.zeros((H, W, 3), np.uint8)
    round_off[y0 : y0 + Hc, :Wc] = canvas[np.ix_(ry, rx)]
    items.append(("索引四舍五入代替 floor（差 1px）", round_off))

    return items


def _identical(a: np.ndarray, b: np.ndarray) -> bool:
    """逐项比较"判据能看到的一切"：形状、dtype、内存布局、像素。"""
    return (a.shape == b.shape
            and a.dtype == b.dtype
            and a.flags["C_CONTIGUOUS"] == b.flags["C_CONTIGUOUS"]
            and a.tobytes() == b.tobytes())


def judge_selfcheck() -> list[Check]:
    """判据自检：正确输出必须 0 失败；每种注入破坏必须至少被抓到 1 条。"""
    spec = dl.SPEC_MODES[0]
    orig, proc = make_checker_inputs()
    base = dl.compose(orig, proc, spec.W, spec.H)

    def fired(out: np.ndarray) -> list[str]:
        return sorted(
            c.cid for c in check_case(out, orig, proc, spec, "checker").checks if c.ok is False
        )

    checks: list[Check] = []
    f0 = fired(base)
    checks.append(Check("M0", "正确输出不应触发任何判据", not f0, f"触发判据={f0 or '无'}"))
    for label, arr in _corruptions(base, orig, proc, spec):
        if _identical(arr, base):
            # 注入本身与原图逐字节相同 => 这条 meta 判据无从判定（是自检脚本的问题，不是判据的问题）
            checks.append(
                Check(f"M:{label}", f"破坏注入「{label}」必须被抓到", False,
                      "注入与原输出逐字节相同（破坏方式在数学上等价），自检脚本需换一种破坏")
            )
            continue
        f = fired(arr)
        checks.append(
            Check(f"M:{label}", f"破坏注入「{label}」必须被抓到", bool(f),
                  f"触发判据={f if f else '无 —— 判据失效！'}")
        )
    return checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="显示构图主机自检（4 模式 x 3 输入）")
    ap.add_argument("--out", default=str(ROOT / "runs" / "display_layout"),
                    help="PNG/REPORT 输出目录（默认 runs/display_layout）")
    ap.add_argument("--dump-inputs", action="store_true",
                    help="把 6 张 256² 输入落成 PNG（跨机逐字节复现用）")
    ap.add_argument("--from-inputs", default=None, metavar="DIR",
                    help="从 --dump-inputs 目录读回输入（板端复现用）")
    ap.add_argument("--no-report", action="store_true", help="不写 REPORT.md")
    args = ap.parse_args(argv)

    outdir = Path(args.out).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    print("=" * 100)
    print("显示构图自检（纯 PS） —— 规范: docs/camera/03_显示构图规范_纯PS.md")
    print(f"被测模块: {Path(dl.__file__).resolve()}")
    print(f"输出目录: {outdir}")
    print("=" * 100)

    inputs = load_inputs(Path(args.from_inputs)) if args.from_inputs else build_inputs()
    for name, (_o, _p, prov) in inputs.items():
        print(f"  输入 {name:8s}: {prov}")

    cases: list[Case] = []

    # 先打印用户最关心的两个数：黑边高度 与 缩放比
    print("\n--- 布局参数（规范 §1.1 期望值 vs 实测计划值）---")
    hdr = f"{'显示模式':>12} | {'s':>5} | {'内容区':>11} | {'上/下黑边':>10} | {'期望上/下':>9} | 一致"
    print(hdr)
    print("-" * len(hdr))
    layout_ok = True
    for spec in dl.SPEC_MODES:
        p = dl.plan(spec.W, spec.H)
        ok = (p.top_bar == spec.top_bar and p.bottom_bar == spec.bottom_bar
              and abs(p.scale - spec.scale) < 1e-12 and p.content_h == spec.content_h)
        layout_ok &= ok
        print(f"{spec.W}x{spec.H:>7} | {p.scale:>5g} | "
              f"{p.content_w:>5}x{p.content_h:<5} | "
              f"{p.top_bar:>4}/{p.bottom_bar:<5} | {spec.top_bar:>4}/{spec.bottom_bar:<5} | "
              f"{'OK' if ok else 'MISMATCH'}")
    print()

    for spec in dl.SPEC_MODES:
        for name in ("color", "checker", "photo"):
            orig, proc, _prov = inputs[name]
            out = dl.compose(orig, proc, W=spec.W, H=spec.H)
            case = check_case(out, orig, proc, spec, name)
            png = outdir / f"{spec.W}x{spec.H}_{name}.png"
            Image.fromarray(out, mode="RGB").save(png, optimize=False)
            case.png_rel = png.name
            case.raw_md5 = hashlib.md5(np.ascontiguousarray(out).tobytes()).hexdigest()
            cases.append(case)

    # --- 逐条打印判据表 ---------------------------------------------------
    print("--- 像素级判据（4 模式 x 3 输入 = 12 组）---")
    total = 0
    failed: list[tuple[Case, Check]] = []
    for case in cases:
        print(f"\n[{case.W}x{case.H} / {case.input_name}]  "
              f"黑边 上{case.top_bar}/下{case.bottom_bar}  s={fmt_scale(case.scale)}  -> {case.png_rel}")
        for c in case.checks:
            total += 1
            mark = "PASS" if c.ok else ("n/a " if c.ok is None else "FAIL")
            print(f"    {mark}  {c.cid:<4} {c.desc}")
            if c.detail:
                print(f"          {c.detail}")
            if c.ok is False:
                failed.append((case, c))

    print("--- 整幅原始像素 md5（与 PNG 编码器无关，板端比对用这个）---")
    for case in cases:
        print(f"    {case.W}x{case.H:<5} {case.input_name:<8} {case.raw_md5}")

    # --- 判据自检（meta）：故意弄坏，确认判据有牙 -------------------------
    print("\n--- 判据自检（破坏注入：PASS 不能是空的）---")
    meta_checks = judge_selfcheck()
    meta_failed = [c for c in meta_checks if c.ok is False]
    for c in meta_checks:
        mark = "PASS" if c.ok else "FAIL"
        print(f"    {mark}  {c.desc}")
        if c.detail:
            print(f"          {c.detail}")

    # --- 汇总 -------------------------------------------------------------
    n_checked = sum(1 for case in cases for c in case.checks if c.ok is not None)
    print("\n" + "=" * 100)
    if args.dump_inputs:
        written = dump_inputs(inputs, outdir)
        print(f"输入已落盘（跨机复现用）: {outdir / 'inputs'} -> {len(written)} 个 PNG")

    if not args.no_report:
        report = write_report(outdir, inputs, cases, layout_ok, failed, meta_checks)
        print(f"报告: {report}")

    groups_pass = len([c for c in cases if not c.failed])
    print(f"判据统计: {n_checked} 条已判定（{total} 条含 n/a），通过 {n_checked - len(failed)} 条，"
          f"失败 {len(failed)} 条；组合通过 {groups_pass}/{len(cases)}；"
          f"判据自检 {len(meta_checks) - len(meta_failed)}/{len(meta_checks)} 通过")
    for case, c in failed:
        print(f"  FAIL: [{case.W}x{case.H}/{case.input_name}] {c.cid} {c.desc} :: {c.detail}")
    for c in meta_failed:
        print(f"  FAIL(判据自检): {c.desc} :: {c.detail}")

    if layout_ok and not failed and not meta_failed and groups_pass == len(cases):
        print("\nPASS —— 4 种显示模式 x 3 组输入，全部像素级判据通过")
        return 0
    print("\nFAIL —— 见上方 FAIL 行")
    return 1


def write_report(outdir: Path, inputs: dict, cases: list[Case],
                 layout_ok: bool, failed: list[tuple[Case, Check]],
                 meta_checks: list[Check] | None = None) -> Path:
    """把判据表 + PNG 相对路径 + 结论写成 REPORT.md。"""
    all_ids = ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10"]
    names = {
        "C1": "输出形状 (H,W,3)/uint8/C 连续",
        "C2": "内容左上=orig[0,0]；中线右侧第一列=proc[0,0]",
        "C3": "第 0 行与第 H-1 行全黑",
        "C4": "黑边高度=公式值（上下差<=1）",
        "C5": "纯色输入：全图仅 {红,蓝,黑}，无第三种来源",
        "C6": "同输入两次调用逐字节相同",
        "C7": "内容区与独立实现(np.repeat 绘制法)逐字节一致",
        "C8": "缩放比实测 == W/512 == 2.5/3.75/2.0/5.0",
        "C9": "横纵等比：32 源像素横向=纵向=32*s（无拉伸）",
        "C10": "镜像敏感性（meta，证明判据对方向敏感）",
    }
    meta_checks = meta_checks or []
    meta_failed = [c for c in meta_checks if c.ok is False]
    ok = (layout_ok and not failed and all(not c.failed for c in cases) and not meta_failed)
    verdict = "PASS" if ok else "FAIL"

    L: list[str] = []
    L.append("# 显示构图自检报告（纯 PS）\n")
    L.append(f"- 结论：**{verdict}** —— 4 种显示模式 × 3 组输入 = 12 组，"
             f"全部判据逐条通过，黑边高度与缩放比与规范 §1.1 完全一致。\n"
             if ok else
             f"- 结论：**{verdict}** —— 见下方失败条目。\n")
    L.append("- 规范：`docs/camera/03_显示构图规范_纯PS.md`（唯一需求来源）")
    L.append("- 被测模块：`ps/pynq/display_layout.py`")
    L.append("- 自检脚本：`tools/display_selftest.py`")
    L.append(f"- 复现命令：`python3 tools/display_selftest.py --out {outdir}`\n")

    # --- 输入来源 ---------------------------------------------------------
    L.append("## 输入来源\n")
    L.append("| 组 | 来源 |")
    L.append("|---|---|")
    for name, (_o, _p, prov) in inputs.items():
        L.append(f"| `{name}` | {prov} |")
    L.append("")

    # --- 布局参数 ---------------------------------------------------------
    L.append("## 布局参数（规范 §1.1 期望 vs 实测计划）\n")
    L.append("| 显示模式 | 缩放 s | 内容区 | 上/下黑边 | 期望上/下 | 一致 |")
    L.append("|---|---:|---|---:|---:|:--:|")
    for spec in dl.SPEC_MODES:
        p = dl.plan(spec.W, spec.H)
        good = (p.top_bar == spec.top_bar and p.bottom_bar == spec.bottom_bar
                and abs(p.scale - spec.scale) < 1e-12 and p.content_h == spec.content_h)
        L.append(f"| {spec.W}×{spec.H} | {fmt_scale(p.scale)} | {p.content_w}×{p.content_h} | "
                 f"{p.top_bar}/{p.bottom_bar} | {spec.top_bar}/{spec.bottom_bar} | "
                 f"{'✅' if good else '❌'} |")
    L.append("")

    # --- 判据总表 ---------------------------------------------------------
    L.append("## 判据表（逐条 PASS/FAIL）\n")
    L.append("`n/a` = 该条判据对当前输入不适用（例如「纯色无第三色」只在纯色输入上有意义）。\n")
    L.append("| 判据 | 说明 | " + " | ".join(f"{c.W}×{c.H}<br>`{c.input_name}`" for c in cases) + " |")
    L.append("|---|---|" + "---|" * len(cases))
    by_id: dict[str, dict[str, Check]] = {}
    for case in cases:
        key = f"{case.W}x{case.H}/{case.input_name}"
        for c in case.checks:
            by_id.setdefault(c.cid, {})[key] = c
    for cid in all_ids:
        row = []
        for case in cases:
            key = f"{case.W}x{case.H}/{case.input_name}"
            c = by_id.get(cid, {}).get(key)
            row.append("—" if c is None else ("✅" if c.ok else ("n/a" if c.ok is None else "❌")))
        L.append(f"| {cid} | {names[cid]} | " + " | ".join(row) + " |")
    L.append("")

    # --- 判据自检 ---------------------------------------------------------
    if meta_checks:
        L.append("## 判据自检（破坏注入：证明 PASS 不是空的）\n")
        L.append("把**正确的输出**按各种典型方式弄坏，每种都必须被至少一条判据抓到；"
                 "否则判据就是摆设。\n")
        L.append("| 注入的破坏 | 是否被抓到 | 触发的判据 |")
        L.append("|---|:--:|---|")
        for c in meta_checks:
            if c.cid == "M0":
                continue
            label = c.cid[2:] if c.cid.startswith("M:") else c.desc
            trig = c.detail.replace("触发判据=", "")
            L.append(f"| {label} | {'✅' if c.ok else '❌'} | {trig} |")
        m0 = next((c for c in meta_checks if c.cid == "M0"), None)
        if m0 is not None:
            L.append("")
            L.append(f"> 对照组：未注入破坏的正确输出，触发判据 `{m0.detail.replace('触发判据=', '')}`"
                     f" ⇒ {'✅ 无失败（判据不误报）' if m0.ok else '❌ 误报！'}。\n")
        L.append("")

    # --- 黑边与缩放实测 ---------------------------------------------------
    L.append("## 黑边高度与缩放比（机械实测）\n")
    L.append("| 显示模式 | 上黑边 | 下黑边 | 上下差 | 缩放比 | 每格(32 源px)显示宽 |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for spec in dl.SPEC_MODES:
        p = dl.plan(spec.W, spec.H)
        L.append(f"| {spec.W}×{spec.H} | {p.top_bar} | {p.bottom_bar} | "
                 f"{abs(p.top_bar - p.bottom_bar)} | {fmt_scale(p.scale)} | {int(32 * p.scale)} px |")
    L.append("")
    L.append("> 判据 C8/C9 不是读内部索引表，而是从输出 PNG 的棋盘格游程反推："
             "左半 `W/2` 列承载 256 个源像素 ⇒ 实测比例 `(W/2)/256 = W/512`；"
             "一格 32 源像素在横/纵两个方向都必须正好是 `32*s` 显示像素 ⇒ 等比、无拉伸。\n")

    # --- PNG --------------------------------------------------------------
    L.append("## 产物 PNG（相对 `runs/display_layout/`）\n")
    L.append("代表 4 张（每个显示模式一张，`color` 纯色输入，机械可验「左红右蓝 + 纯黑黑边」）：\n")
    for case in cases:
        if case.input_name == "color":
            L.append(f"- `{outdir.name}/{case.png_rel}` —— {case.W}×{case.H}，"
                     f"黑边上 {case.top_bar} / 下 {case.bottom_bar}，s={fmt_scale(case.scale)}")
    L.append("")
    L.append("全部 12 张（`raw_md5` = 整幅原始像素的 md5，**与 PNG 编码器无关**，"
             "板端比对请用这个）：\n")
    L.append("| 显示模式 | 输入 | PNG | raw_md5 |")
    L.append("|---|---|---|---|")
    for case in cases:
        L.append(f"| {case.W}×{case.H} | `{case.input_name}` | `{outdir.name}/{case.png_rel}` | "
                 f"`{case.raw_md5}` |")
    L.append("")
    L.append("> `checker` 用于肉眼验缩放锯齿/变形，`photo` 用于肉眼验左原图/右处理图的观感。"
             "PNG 只落在 `runs/` 下，**不进 git**。\n")

    # --- 上板复现 ---------------------------------------------------------
    L.append("## 上板复现（跨机逐字节）\n")
    L.append("主机与板端跑同一份代码，**输入必须逐字节相同**，否则比 md5 没意义。两种做法：\n")
    L.append("1. 主机 `python3 tools/display_selftest.py --dump-inputs` 会把用到的 6 张 256² 输入"
             "落盘到 `runs/display_layout/inputs/`；把这 6 个 PNG 一起同步到板端，"
             "板端跑 `--from-inputs <该目录>`。")
    L.append("2. 比对**整幅原始像素的 md5**（上表 `raw_md5` 列，控制台也会打印），"
             "不要比 PNG 文件 md5 —— PNG 会随 zlib/PIL 版本变化。\n")
    L.append("```bash")
    L.append("# 主机（本机）")
    L.append("python3 tools/display_selftest.py --out runs/display_layout --dump-inputs")
    L.append("# 板端（等机器空闲；本阶段不需要 overlay/位流）")
    L.append("rsync -a runs/display_layout/inputs/ xilinx@192.168.7.2:/home/xilinx/demoir/inputs/")
    L.append("cd /home/xilinx/demoir && python3 tools/display_selftest.py \\")
    L.append("  --out /home/xilinx/runs/display_layout --from-inputs /home/xilinx/demoir/inputs")
    L.append("```\n")

    # --- 失败明细 ---------------------------------------------------------
    if failed:
        L.append("## 失败明细\n")
        for case, c in failed:
            L.append(f"- `[{case.W}×{case.H}/{case.input_name}]` **{c.cid}** {c.desc} :: {c.detail}")
        L.append("")

    L.append("## 结论\n")
    if ok:
        L.append("画面构图正确：左原图 / 右处理图（不镜像），先撑满宽度等比放大（s = W/512），"
                 "剩余高度上下对称填黑，黑边为纯黑 `(0,0,0)`；四种模式的黑边高度与缩放比与规范 "
                 "§1.1 完全一致，同输入两次调用逐字节相同 ⇒ 可进入下一步（板端复跑比对 md5）。")
    else:
        L.append("存在失败判据，不能进入下一步。详见上方失败明细。")
    L.append("")

    path = outdir / "REPORT.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
