#!/usr/bin/env python3
"""HDMI P2 主机自检：4 种模式 × 3 种输入 + 纯 Python“按行扫描”模型 + 寄存器握手。

判据（任务书 ③ / 契约 §4）
--------------------------
1. ``compose()`` 生成整幅画面 ⇒ 用**按行扫描的纯 Python 模型**（模拟 PL 取数：
   按行发起 8 拍突发事件、拼字节流、每 3 字节切一个像素、行首按 stride 对齐）
   读出“PL 会看到的像素序列” ⇒ 与 ``compose()`` **逐字节比对**；
2. 模拟 ``put_frame``/``wait_frame`` 的寄存器握手（假 reg_io）⇒ 断言
   ``frame_id`` 单调递增、且 ``FRAME_ID_LATCHED`` **只在 vsync 之后**才变化；
3. 双缓冲地址约定 ``buf = frame_id & 1``、``FB_SIZE = align64(stride*H)`` 与
   RTL 一致（同一份公式在这里独立重算一次）。

不 import pynq、不碰硬件；跑通打印 ``PASS``，返回 0。

用法::

    python3 tools/hdmi_selftest.py
    python3 tools/hdmi_selftest.py --dump-png runs/hdmi_p2/selftest   # 可选落图
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np

# ---------------------------------------------------------------------------
# 路径：仓库根向上找 pynq_specialized_code/ps/pynq（仓库根没有 ps/ 目录）
# ---------------------------------------------------------------------------
def _find_root(start: str) -> str:
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

from display_layout import compose                      # noqa: E402  只调用，不改
import hdmi_out as ho                                  # noqa: E402

FAILS: list[str] = []
CHECKS = [0]


def check(cond: bool, msg: str) -> bool:
    """记录一条判据；不通过就记进 FAILS。"""
    CHECKS[0] += 1
    if not cond:
        FAILS.append(msg)
        print(f"  ❌ {msg}")
    return bool(cond)


# ---------------------------------------------------------------------------
# 三种输入（纯 numpy 生成，主机/板端同源；与 tools/display_selftest.py 的三种同类）
# ---------------------------------------------------------------------------
def _input_color() -> tuple[np.ndarray, np.ndarray]:
    y, x = np.mgrid[0:256, 0:256]
    orig = np.stack([x, y, (x ^ y)], axis=-1).astype(np.uint8)
    proc = np.stack([255 - x, 255 - y, (x + y) & 0xFF], axis=-1).astype(np.uint8)
    return orig, proc


def _input_checker() -> tuple[np.ndarray, np.ndarray]:
    y, x = np.mgrid[0:256, 0:256]
    k = (((x // 32) + (y // 32)) & 1).astype(np.uint8)
    orig = np.stack([k * 255, k * 255, k * 255], axis=-1).astype(np.uint8)
    proc = np.stack([255 - k * 255, (x * 2) & 0xFF, (y * 2) & 0xFF], axis=-1).astype(np.uint8)
    return orig, proc


def _input_photo() -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(20261102)
    base = rng.integers(0, 256, size=(16, 16, 3), dtype=np.uint8)
    orig = np.repeat(np.repeat(base, 16, axis=0), 16, axis=1)          # 16×16 → 256²
    proc = np.clip(orig.astype(np.int16) // 2 + 64, 0, 255).astype(np.uint8)
    return orig, proc


INPUTS = (("color", _input_color), ("checker", _input_checker), ("photo", _input_photo))

# ---------------------------------------------------------------------------
# “PL 会看到的字节流” 纯 Python 模型
# ---------------------------------------------------------------------------
FB_BASE = 0x1000_0000


def ddr_image(frame: np.ndarray, stride: int, pad_byte: int = 0xAA) -> bytearray:
    """把 ``compose()`` 的 (H,W,3) 按 stride 铺进 DDR（行尾照 RTL 的“不读”原则填毒值）。"""
    h, w, _ = frame.shape
    img = bytearray(stride * h)
    for y in range(h):
        off = y * stride
        img[off:off + w * 3] = frame[y].tobytes()
        if stride > w * 3:
            img[off + w * 3:off + stride] = bytes([pad_byte]) * (stride - w * 3)
    return img


def pl_scan(img: bytearray, base: int, stride: int, w: int, h: int,
            buf: int, fsize: int, arlen_max: int = 7) -> bytes:
    """模拟 PL 的取数：**按行**发 8 拍突发事件（64 B），拼字节流后每 3 B 切一个像素。

    与 RTL 的对应关系：
      · 行首地址 = base + buf*fsize + y*stride  （buf 由 FRAME_ID[0] 选）
      · 每行只取 ceil(w*3/8) 拍（最后一拍可能不足 64 B）
      · 行尾 padding **一个字节都不碰**
    """
    beats_per_burst = arlen_max + 1
    out = bytearray()
    for y in range(h):
        line_start = base + buf * fsize + y * stride
        nbytes = w * 3
        beats = (nbytes + 7) // 8
        stream = bytearray()
        b = 0
        while b < beats:
            n = min(beats_per_burst, beats - b)
            a = line_start + b * 8
            stream += img[a - FB_BASE:a - FB_BASE + n * 8]
            b += n
        out += stream[:nbytes]
    return bytes(out)


def pixels_of(buf: bytes) -> np.ndarray:
    """字节流 → (N,3) uint8（不做任何重排：内存就是 R,G,B）。"""
    return np.frombuffer(buf, dtype=np.uint8).reshape(-1, 3)


# ---------------------------------------------------------------------------
# 用例 1：布局逐字节（4 模式 × 3 输入）
# ---------------------------------------------------------------------------
def test_layout(modes) -> None:
    print("\n[1] 布局逐字节（纯 Python 按行扫描模型 vs compose()）")
    for mode_code in modes:
        m = ho.MODES[mode_code]
        w, h = m.W, m.H
        stride = ho.align64(w * 3)          # 契约：64 B 对齐；1280*3/1920*3 本就对齐
        fsize = ho.align64(stride * h)
        for name, gen in INPUTS:
            orig, proc = gen()
            frame = compose(orig, proc, w, h)
            img = ddr_image(frame, stride)

            line_img = ddr_image(frame, stride)
            for buf in (0, 1):
                # 双缓冲：同一帧分别放到 buf0 / buf1 的物理位置，验证地址公式
                total = buf * fsize + len(line_img)
                img = bytearray(bytes([0xAA]) * total)
                img[buf * fsize:buf * fsize + len(line_img)] = line_img
                got = pl_scan(img, FB_BASE, stride, w, h, buf, fsize)
                ok = (got == frame.tobytes())
                if not ok:
                    bad = np.nonzero(np.frombuffer(got, np.uint8) !=
                                     frame.reshape(-1))[0]
                    check(False, f"{m.name}/{name}/buf{buf}: 首失配字节 {bad[:6].tolist()}"
                                 f"（共 {len(bad)}）")
                    continue
                # 再按“像素”比一次（等价，但让报告更像判据）
                px = pixels_of(got)
                check(np.array_equal(px, frame.reshape(-1, 3)),
                      f"{m.name}/{name}/buf{buf}: 像素序列不一致")
                CHECKS[0] += 1                    # 字节级 + 像素级各算一条
        print(f"  ✓ {m.name} ({w}x{h}, stride={stride}, fsize={fsize}) × 3 输入 × 2 块")


# ---------------------------------------------------------------------------
# 用例 2：寄存器图 / 双缓冲地址公式 与 RTL 对齐
# ---------------------------------------------------------------------------
def test_regmap() -> None:
    print("\n[2] 寄存器图与双缓冲地址公式")
    expect = {
        "CTRL": 0x00, "STATUS": 0x04, "VIDEO_MODE": 0x08,
        "FB_ADDR_LO": 0x0C, "FB_ADDR_HI": 0x10, "FB_STRIDE": 0x14,
        "FB_W": 0x18, "FB_H": 0x1C, "FRAME_ID": 0x20,
        "FRAME_ID_LATCHED": 0x24, "PIXEL_FMT": 0x28, "UNDERFLOW_CNT": 0x2C,
        "PIX_HB": 0x30,                      # §8.13 增补：像素域心跳
    }
    # 逐键比（含“REG 多出契约外条目”也算失败，故按并集遍历）；条数与旧版 12+1 相同
    for k in sorted(set(expect) | set(ho.REG)):
        check(ho.REG.get(k) == expect.get(k),
              f"REG[{k}] 应为 {expect.get(k)}，实际 {ho.REG.get(k)}（契约 §2/§8.13）")

    # 双缓冲公式：第 n 帧写 buf=n&1，PL 读 addr = base + (n&1)*fsize
    for mode_code in (0, 1, 2, 3):
        m = ho.MODES[mode_code]
        stride = ho.align64(m.W * 3)
        fsize = ho.align64(stride * m.H)
        io = ho.FakeRegIo()
        out = ho.HdmiOut(reg_io=io, fb_addr=FB_BASE, mode=mode_code)
        out.configure(FB_BASE, stride, m.W, m.H)
        check(out.fb_size == fsize, f"{m.name}: fb_size={out.fb_size} != {fsize}")
        check(io.mem.get(ho.REG["FB_STRIDE"]) == stride, f"{m.name}: FB_STRIDE 写错")
        check(io.mem.get(ho.REG["FB_ADDR_LO"]) == FB_BASE, f"{m.name}: FB_ADDR_LO 写错")
    print("  ✓ 13 个偏移 + 4 种模式的 fsize/stride 与 RTL 公式一致")


# ---------------------------------------------------------------------------
# 用例 3：put_frame / wait_frame 寄存器握手
# ---------------------------------------------------------------------------
def test_handshake() -> None:
    print("\n[3] put_frame / wait_frame 握手（假 reg_io）")
    m = ho.MODES[ho.MODE_720P60]
    stride = ho.align64(m.W * 3)
    io = ho.FakeRegIo()
    out = ho.HdmiOut(reg_io=io, fb_addr=FB_BASE, mode=ho.MODE_720P60)
    out.configure(FB_BASE, stride, m.W, m.H)
    io.mark_init_done()

    frame = np.zeros((m.H, m.W, 3), dtype=np.uint8)
    frame[..., 0] = 7

    prev = 0
    for n in range(1, 5):
        fid = out.put_frame(frame)
        check(fid == n, f"put_frame 第 {n} 次应返回 {n}，得到 {fid}")
        check(fid > prev, "frame_id 必须单调递增")
        prev = fid
        check(io.mem.get(ho.REG["FRAME_ID"]) == fid, "FRAME_ID 未写入")
        check(fid & 1 == (fid % 2), "双缓冲 buf = frame_id & 1")

        # 还没有 vsync ⇒ PL 不该采纳（wait_frame 必须超时返回 False）
        t0 = out.frame_latched()
        check(out.wait_frame(fid, timeout=0.02) is False,
              "vsync 之前 wait_frame 不该成功（说明 PL 提前采纳 = 会撕裂）")
        check(out.frame_latched() == t0, "vsync 之前 FRAME_ID_LATCHED 不该变化")

        # 来一个 vsync ⇒ 采纳
        io.vsync_edge()
        check(out.wait_frame(fid, timeout=0.5) is True, "vsync 之后 wait_frame 应成功")
        check(out.frame_latched() == fid, "FRAME_ID_LATCHED 应等于刚提交的帧号")

        # 第二块必须是后台：检查连续两帧落在不同 buf 上
        if n >= 2:
            check(((n - 1) & 1) != (n & 1), "连续两帧必须落在不同的 buffer")

    # 只发了 4 次 FRAME_ID 写入，且每次都在 vsync 前
    fid_writes = [v for (o, v) in io.writes if o == ho.REG["FRAME_ID"]]
    check(fid_writes == [1, 2, 3, 4], f"FRAME_ID 写入序列异常: {fid_writes}")
    print(f"  ✓ 4 次提交：帧号 {fid_writes} 单调、buf 交替、且只在 vsync 后变化")


# ---------------------------------------------------------------------------
# 用例 4：欠载诊断（0x2C）+ 参数校验
# ---------------------------------------------------------------------------
def test_diag_and_guards() -> None:
    print("\n[4] 欠载诊断 0x2C / 参数校验")
    m = ho.MODES[ho.MODE_720P60]
    stride = ho.align64(m.W * 3)
    io = ho.FakeRegIo()
    out = ho.HdmiOut(reg_io=io, fb_addr=FB_BASE, mode=ho.MODE_720P60)
    out.configure(FB_BASE, stride, m.W, m.H)
    io.mark_init_done()

    check(out.underflow() == 0, "初值 UNDERFLOW_CNT 应为 0")
    io.stall(3)
    check(out.underflow() == 3, f"欠载 3 行后应读 3，得到 {out.underflow()}")
    st = out.status()
    check(st["underflow"] is True and st["underflow_cnt"] == 3,
          f"status() 应为 underflow=True/cnt=3，得到 {st}")
    check(st["init_done"] is True, "init_done 应为真")

    out.soft_reset()
    check(out.underflow() == 0, "soft_reset 后 UNDERFLOW_CNT 应归零")
    check(out.status()["underflow"] is False, "soft_reset 后 STATUS[2] 应清零")

    # ---- 参数校验：坏 stride / 坏形状必须直接 ValueError（别让硬件踩）----
    bad = [
        ("stride 未 64B 对齐", dict(phys_addr=FB_BASE, stride=m.W * 3 + 32, w=m.W, h=m.H)),
        ("stride < w*3", dict(phys_addr=FB_BASE, stride=64, w=m.W, h=m.H)),
        ("地址未 64B 对齐", dict(phys_addr=FB_BASE + 8, stride=stride, w=m.W, h=m.H)),
    ]
    for desc, kw in bad:
        try:
            ho.HdmiOut(reg_io=ho.FakeRegIo()).configure(**kw)
        except ValueError:
            check(True, desc)
        else:
            check(False, f"{desc} 应该抛 ValueError")

    # 坏帧形状
    try:
        out.put_frame(np.zeros((10, 10, 3), dtype=np.uint8))
    except ValueError:
        check(True, "坏帧形状 ValueError")
    else:
        check(False, "坏帧形状应该抛 ValueError")

    print("  ✓ 欠载计数/清零/参数校验 5 组判据全部符合")


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="0,1,2,3", help="模式号，逗号分隔")
    a = ap.parse_args()
    modes = [int(x) for x in a.modes.split(",") if x != ""]

    print("=" * 78)
    print("HDMI P2 主机自检：compose() → PL 扫描模型 → 逐字节比对")
    print("=" * 78)

    test_layout(modes)
    test_regmap()
    test_handshake()
    test_diag_and_guards()

    print("\n" + "=" * 78)
    if FAILS:
        print(f"FAIL：{len(FAILS)}/{CHECKS[0]} 条判据不通过")
        for f in FAILS[:20]:
            print("   -", f)
        return 1
    print(f"PASS：{CHECKS[0]} 条判据全部通过"
          f"（{len(modes)} 模式 × {len(INPUTS)} 输入 × 双缓冲）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
