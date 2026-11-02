#!/usr/bin/env python3
"""为 tb_hdmi_fb_scan 生成"逐字节一致"判据用的黄金向量。

唯一需求来源
------------
· ``docs/camera/05_HDMI输出_PS_PL接口契约.md`` §4 P2 判据
· ``docs/camera/03_显示构图规范_纯PS.md`` §2 帧缓冲布局契约
· ``ps/pynq/display_layout.py::compose()`` —— 帧内容的唯一权威（不改、只调用）

产出（放 ``runs/hdmi_p2/``，都是小文件，可进 git）
------------------------------------------------
::

    case<N>_par.txt     "W H STRIDE FB_ADDR"（十进制；tb 用 $fscanf 读，避免两边硬编码漂移）
    case<N>_mem.hex     DDR 镜像：每行 16 个十六进制字符 = 1 个 64-bit 小端字
    case<N>_gold.hex    期望像素流：每行 6 个十六进制字符 = 1 个像素 {R,G,B}

判据怎么来的
------------
* ``_mem.hex`` 把 ``compose()`` 的 (H,W,3) 字节**按 stride 铺进内存**：行首无 padding，
  行尾补 ``0xAA``（**毒值**：PL 若误读 padding，输出必然与黄金不符）。
* ``_gold.hex`` 是 ``compose()`` 的原始字节流按 (y,x) 顺序、每 3 字节一个像素
  ``{R,G,B}`` ⇒ TB 里 ``pix_rgb`` 与之逐字节比对，就等价于
  “PL 吐出的像素序列与 compose() 的布局逐字节一致”。

用法::

    python3 tools/gen_hdmi_scan_golden.py --out runs/hdmi_p2
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np

def _find_root(start: str) -> str:
    """向上找到含 ``pynq_specialized_code`` 的仓库根（仓库根没有 ps/ 目录）。"""
    d = start
    for _ in range(8):
        if os.path.isdir(os.path.join(d, "pynq_specialized_code", "ps", "pynq")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    raise SystemExit(f"找不到仓库根（从 {start} 向上找 pynq_specialized_code/ps/pynq）")


_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _find_root(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "pynq_specialized_code", "ps", "pynq"))

from display_layout import compose  # noqa: E402  （只调用，不改）

#: TB 的 AXI 从机内存模型基址（绝对地址 ≥ 此值才可访问）
MEM_BASE = 0x1000_0000

#: 用例表：必须与 tb_hdmi_fb_scan.v 的期望一致（但 tb 实际从 _par.txt 读，
#: 所以这里改了什么，tb 会自动跟上；本表只是“人类可读的登记”）
CASES = (
    # id, W,  H,  stride, fb_addr,            seed, 说明
    (1, 40, 30, 128, MEM_BASE + 0x0000_0000, 1, "stride≠W*3（120 → 128），宽度适配分支，上下黑边 5/5"),
    (2, 64, 20, 192, MEM_BASE + 0x0000_8000, 2, "stride==W*3（无 padding），高度适配回退分支（左右黑边）"),
    (3, 40, 30, 136, MEM_BASE + 0x0000_4000, 3, "stride=136（≠120 且 ≠128），偏移基址 + 非 64B 倍数行距"),
    (4, 640, 4, 1920, MEM_BASE + 0x0000_0FC0, 4,
     "真实行宽：640×3=1920 B = 15 个突发；基址 0x...0FC0 ⇒ 首个突发被 4KB 边界砍成 8 拍"),
    (5, 40, 30, 128, MEM_BASE + 0x0000_1000, 5,
     "判据④换帧：buf0/buf1 两张完全不同的图案（buf1 在 +align64(stride*H)），帧中途写 FRAME_ID=1"),
    (6, 40, 30, 128, MEM_BASE + 0x0000_0000, 6,
     "判据⑤欠载注入：单缓冲正常向量（TB 把 AXI 延迟拉到 400 拍/突发）"),
)

POISON = 0xAA  # 行尾 padding 的毒值（PL 若误读必然失配）


def _mk_inputs(seed: int) -> tuple[np.ndarray, np.ndarray]:
    """确定性输入图（同 seed 恒等），只要求字节分布“不平凡”。"""
    rng = np.random.default_rng(seed)
    orig = rng.integers(0, 256, size=(256, 256, 3), dtype=np.uint8)
    proc = (255 - orig).astype(np.uint8)  # 右半与左半互补，便于人眼/机器区分来源
    return orig, proc


def gen_case(case_id: int, W: int, H: int, stride: int, fb_addr: int, seed: int,
             out_dir: str, dual: bool = False) -> dict:
    if stride < W * 3:
        raise SystemExit(f"case{case_id}: stride={stride} < W*3={W*3}")
    if fb_addr % 8 or stride % 8:
        # AXI arsize=8B 要求地址 8 字节对齐；契约要求 64B 对齐，这里先放宽到 8B
        raise SystemExit(f"case{case_id}: fb_addr/stride 必须 8 字节对齐")

    orig, proc = _mk_inputs(seed)
    frame = compose(orig, proc, W, H)          # (H,W,3) uint8，C 连续
    assert frame.shape == (H, W, 3) and frame.flags["C_CONTIGUOUS"]

    # 双缓冲：第二块（buf1）用**完全不同的输入**，两帧像素绝不重合
    frame2 = None
    if dual:
        orig2 = np.ascontiguousarray(np.rot90(orig).copy())
        proc2 = np.ascontiguousarray(np.fliplr(proc).copy())
        frame2 = compose(orig2, proc2, W, H)

    # ---- DDR 镜像：按 stride 铺行，行尾毒值 ----
    def _lay(fr: np.ndarray) -> bytearray:
        b = bytearray(stride * H)
        for y in range(H):
            off = y * stride
            b[off:off + W * 3] = fr[y].tobytes()
            b[off + W * 3:off + stride] = bytes([POISON]) * (stride - W * 3)
        return b

    img = _lay(frame)
    fsize = ((stride * H) + 63) // 64 * 64      # 与 RTL 的 align64(FB_STRIDE*FB_H) 一致
    if dual:
        buf1 = _lay(frame2)
        img = img + bytes([POISON]) * (fsize - len(img)) + buf1
    if len(img) % 8:
        img += bytes([POISON]) * (8 - len(img) % 8)

    # ---- 64-bit 小端字 ----
    words = [int.from_bytes(img[i:i + 8], "little") for i in range(0, len(img), 8)]

    # ---- 期望像素流：每像素 {R,G,B} ----
    def _gold(fr: np.ndarray) -> np.ndarray:
        px = fr.reshape(-1, 3).astype(np.uint32)
        return (px[:, 0] << 16) | (px[:, 1] << 8) | px[:, 2]

    gold = _gold(frame)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"case{case_id}_par.txt"), "w") as f:
        f.write(f"{W} {H} {stride} {fb_addr}\n")
    with open(os.path.join(out_dir, f"case{case_id}_mem.hex"), "w") as f:
        f.write("".join(f"{w:016x}\n" for w in words))
    with open(os.path.join(out_dir, f"case{case_id}_gold.hex"), "w") as f:
        f.write("".join(f"{g:06x}\n" for g in gold))
    if dual:
        with open(os.path.join(out_dir, f"case{case_id}_gold2.hex"), "w") as f:
            f.write("".join(f"{g:06x}\n" for g in _gold(frame2)))

    return {
        "case": case_id,
        "W": W, "H": H, "stride": stride, "dual": dual, "fsize": fsize,
        "fb_addr": hex(fb_addr), "words": len(words), "pixels": int(gold.size),
        "img_bytes": len(img), "poison_bytes": stride - W * 3,
        "gold_md5": __import__("hashlib").md5(frame.tobytes()).hexdigest(),
        "gold2_md5": (__import__("hashlib").md5(frame2.tobytes()).hexdigest()
                      if dual else "-"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(_ROOT, "runs", "hdmi_p2"))
    ap.add_argument("--only", type=int, default=0, help="只生成某一个 case")
    a = ap.parse_args()

    rows = []
    for cid, W, H, stride, addr, seed, note in CASES:
        if a.only and cid != a.only:
            continue
        r = gen_case(cid, W, H, stride, addr, seed, a.out, dual=(cid == 5))
        r["note"] = note
        rows.append(r)
        print(f"case{r['case']}: W={r['W']} H={r['H']} stride={r['stride']} "
              f"fb={r['fb_addr']} words={r['words']} pixels={r['pixels']} "
              f"pad={r['poison_bytes']}B/row dual={r['dual']} fsize={r['fsize']} "
              f"gold_md5={r['gold_md5']} gold2_md5={r['gold2_md5']}")
        print(f"         {note}")
    print(f"输出目录: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
