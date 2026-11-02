#!/usr/bin/env python3
"""显示流水线端到端主机自检 —— **里程碑 1：只做判据 1~3**。

被测：``ps/pynq/display_demo.DisplayPipeline``（compose → 双缓冲 → ``FRAME_ID`` 换帧）
互补：``tools/hdmi_selftest.py`` 管的是“PL 按 stride/8 拍扫描能否读回 compose 的字节”+
寄存器图/握手；本文件**不重复**它，只判“PS 侧流水线把字节写对、块选对、换帧有序”。

判据
----
1. **字节流一致**：假 allocator 抓内存 ⇒ 写进第 ``FRAME_ID&1`` 块的字节与
   ``compose()` 输出**逐字节相同**（比 2,764,800 B 全量，不看抽样）。
2. **双缓冲**：连续 4 帧 ⇒ 块序号严格交替（相邻不同）且等于 ``FRAME_ID & 1``、
   ``FRAME_ID`` 严格递增；每块内容 == 本帧；**正被扫的另一块在换帧前不被覆盖**。
3. **原子性**：内存写与寄存器写放进**同一条事件时间线** ⇒ 断言“写缓冲内容”(``block_write``)
   全部早于“写 ``FRAME_ID``”(``fid_write``)；并在写 ``FRAME_ID`` 的那一刻给目标块拍 md5
   快照，断言快照 == 本帧 ``compose()`` 的 md5（不是“写完了、碰巧后面才一致”）。

用法 / 退出码
-------------
``python3 tools/display_e2e_selftest.py`` ⇒ 全过 ``rc=0`` 且末行打印 ``PASS``。
不 import pynq、不碰板子（192.168.7.2 全程零访问）。
"""

from __future__ import annotations

import hashlib
import os
import sys
import traceback

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))


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


_ROOT = _find_root(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "pynq_specialized_code", "ps", "pynq"))

from display_layout import compose                      # noqa: E402  只调用
import hdmi_out as ho                                   # noqa: E402  只调用
import display_demo as dd                               # noqa: E402  被测

W, H, STRIDE = 1280, 720, 3840
FRAME_BYTES = STRIDE * H                                # 2,764,800
BLOCK_BYTES = ho.align64(FRAME_BYTES)
FB_BASE = 0x1F00_0000

FAILS: list[str] = []
CHECKS = [0]


def check(cond: bool, msg: str) -> bool:
    CHECKS[0] += 1
    if not cond:
        FAILS.append(msg)
        print(f"  ❌ {msg}")
    return bool(cond)


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


# ---------------------------------------------------------------------------
# 三种输入（纯 numpy，可复现；判据 2 用 k 变化的纯色，便于定位“哪一帧写错块”）
# ---------------------------------------------------------------------------
def _input_color() -> tuple[np.ndarray, np.ndarray]:
    y, x = np.mgrid[0:256, 0:256]
    orig = np.stack([x, y, (x ^ y)], -1).astype(np.uint8)
    proc = np.stack([255 - x, 255 - y, (x + y) & 0xFF], -1).astype(np.uint8)
    return orig, proc


def _input_mark(k: int) -> tuple[np.ndarray, np.ndarray]:
    orig = np.full((256, 256, 3), (40 * k + 3) % 256, dtype=np.uint8)
    proc = np.full((256, 256, 3), (255 - 40 * k) % 256, dtype=np.uint8)
    orig[::8, :, 0] = (k * 32) % 256          # 一点结构，避免整块纯色被巧合掩盖
    proc[:, ::8, 1] = (k * 16 + 7) % 256
    return orig, proc


# ---------------------------------------------------------------------------
# 主机侧测试替身
# ---------------------------------------------------------------------------
class TraceLog:
    """内存写与寄存器写共用的一条有序事件时间线（判“先后”必须有同一条序列）。"""

    def __init__(self):
        self.events: list[tuple[str, object]] = []

    def add(self, kind: str, detail=None) -> int:
        self.events.append((kind, detail))
        return len(self.events) - 1

    def indices(self, kind: str) -> list[int]:
        return [i for i, (k, _) in enumerate(self.events) if k == kind]


class FakeBlock:
    """假分配块：连续 uint8 内存 + ``device_address``（模拟 ``pynq.allocate`` 的返回值）。"""

    def __init__(self, nbytes: int, addr: int):
        self.buf = np.zeros(int(nbytes), dtype=np.uint8)
        self.device_address = int(addr)
        self.physical_address = int(addr)
        self.flushes = 0

    @property
    def array(self) -> np.ndarray:
        return self.buf

    def __array__(self, dtype=None, copy=None):
        return self.buf if dtype is None else self.buf.astype(dtype)

    def __getitem__(self, i):
        return self.buf[i]

    def __setitem__(self, i, v):
        self.buf[i] = v

    def __len__(self):
        return len(self.buf)

    def flush(self) -> None:
        self.flushes += 1


class FakeAllocator:
    """连续、64 B 对齐的假分配器：PL 用 ``FB_ADDR + buf*FB_SIZE`` 找第二块 ⇒ 两块必须紧邻。"""

    def __init__(self, log: TraceLog, base: int = FB_BASE):
        self.log = log
        self.base = int(base)
        self.blocks: list[FakeBlock] = []
        self.requests: list[tuple[int, int]] = []

    def __call__(self, nbytes: int) -> FakeBlock:
        used = sum(b.buf.nbytes for b in self.blocks)
        addr = ho.align64(self.base + used)
        blk = FakeBlock(int(nbytes), addr)
        self.blocks.append(blk)
        self.requests.append((int(nbytes), addr))
        self.log.add("alloc", (int(nbytes), addr))
        return blk

    @property
    def memory(self) -> np.ndarray:
        return self.blocks[0].buf


class RecordingRegIo(ho.FakeRegIo):
    """假寄存器堆 + 有序日志；写 ``FRAME_ID`` 的瞬间给目标块拍 md5 快照（判原子性）。"""

    def __init__(self, log: TraceLog, snapshot=None):
        super().__init__()
        self.log = log
        self._snapshot = snapshot

    def write32(self, off: int, val: int) -> None:
        off &= ~3
        val &= 0xFFFF_FFFF
        super().write32(off, val)
        if off == ho.REG["FRAME_ID"]:
            snap = None if self._snapshot is None else self._snapshot(val)
            self.log.add("fid_write", (val, snap))
        else:
            self.log.add("reg_write", (off, val))


class TracedPipeline(dd.DisplayPipeline):
    """在 ``_write_block``（写块 ↔ 换帧之间的接缝）上记事件，供判据 3 判先后。"""

    def __init__(self, log: TraceLog, **kw):
        self.log = log
        super().__init__(**kw)

    def _write_block(self, slot: int, frame: np.ndarray) -> None:
        super()._write_block(slot, frame)
        self.log.add("block_write", int(slot))


def _new(log: TraceLog, snapshot=None):
    alloc = FakeAllocator(log)
    pipe = TracedPipeline(log, reg_io=RecordingRegIo(log, snapshot), mode=3, allocator=alloc)
    return alloc, pipe


# ---------------------------------------------------------------------------
# 判据 1：字节流一致
# ---------------------------------------------------------------------------
def judge1_bytes() -> None:
    print("\n[判据 1] 字节流一致：写进缓冲的字节 vs compose() 输出（全量逐字节）")
    log = TraceLog()
    alloc, pipe = _new(log)
    pipe.setup()
    orig, proc = _input_color()
    fid = pipe.show(orig, proc)

    expected = compose(orig, proc, W=W, H=H)
    slot = fid & 1
    off = slot * pipe.block_bytes
    got = alloc.memory[off:off + expected.nbytes]
    n = int(expected.nbytes)

    check(expected.shape == (H, W, 3) and expected.dtype == np.uint8,
          f"compose 输出应为 ({H},{W},3)/uint8，实测 {expected.shape}/{expected.dtype}")
    check(n == FRAME_BYTES == STRIDE * H, f"单帧字节数应为 {FRAME_BYTES}，实测 {n}")
    check(BLOCK_BYTES == FRAME_BYTES,
          f"块大小 align64(stride*H)={BLOCK_BYTES} 应等于帧字节数 {FRAME_BYTES}（无行尾 padding）")
    check(alloc.requests == [(2 * BLOCK_BYTES, FB_BASE)],
          f"应一次连续分配到两块（{2 * BLOCK_BYTES} B @0x{FB_BASE:X}），实测 {alloc.requests}")
    check(pipe.block_bytes == BLOCK_BYTES, f"pipeline.block_bytes 应为 {BLOCK_BYTES}")

    same = np.array_equal(got, expected.reshape(-1))
    diff = 0 if same else int(np.count_nonzero(got != expected.reshape(-1)))
    first_bad = -1 if same else int(np.argmax(got != expected.reshape(-1)))
    check(same, f"块内字节与 compose 输出不一致：{diff} B 不同，首失配偏移 {first_bad}")
    check(got.tobytes() == expected.tobytes(), "按字节串比较应为完全相同（bytes 相等）")

    print(f"  compose : {n:,} B  md5={md5(expected.tobytes())}")
    print(f"  块{slot}内存: {n:,} B  md5={md5(got.tobytes())}  偏移 FB_ADDR+{slot}*{BLOCK_BYTES}")
    print(f"  ⇒ 逐字节比较 {n:,} B（{n / 2**20:.2f} MiB），不同 {diff} B"
          + ("，✅ 完全一致" if same else f"，首失配 {first_bad}"))
    check(got.any(), "块内容不应全零（否则“一致”可能是空写）")
    check(not expected[:40].any() and not expected[-40:].any() and expected[40:680].any(),
          "构图自查：上下各 40 行必须是黑边、内容区必须非全黑")


# ---------------------------------------------------------------------------
# 判据 2：双缓冲
# ---------------------------------------------------------------------------
def judge2_double_buffer() -> None:
    print("\n[判据 2] 双缓冲：4 帧的块序号 / 帧号 / 内容")
    log = TraceLog()
    alloc, pipe = _new(log)
    pipe.setup()

    ids, slots, content_ok, other_untouched = [], [], [], []
    prev = None
    for k in range(4):
        orig, proc = _input_mark(k)
        fid = pipe.show(orig, proc)
        exp = compose(orig, proc, W=W, H=H)
        slot = fid & 1
        off = slot * pipe.block_bytes
        got = alloc.memory[off:off + exp.nbytes]
        ids.append(int(fid))
        slots.append(int(slot))
        content_ok.append(bool(np.array_equal(got, exp.reshape(-1))))
        if prev is not None:                      # 另一块（PL 正在扫）必须还是上一帧
            ooff = (1 - slot) * pipe.block_bytes
            other_untouched.append(
                bool(np.array_equal(alloc.memory[ooff:ooff + prev.nbytes], prev.reshape(-1))))
        prev = exp

    alt = all(slots[i] != slots[i + 1] for i in range(len(slots) - 1))
    print(f"  帧号序列 : {ids}")
    print(f"  块序号   : {slots}   (= FRAME_ID & 1；相邻不同={alt})")
    print(f"  块地址   : " + ", ".join(
        f"帧{f}:0x{FB_BASE + s * BLOCK_BYTES:X}" for f, s in zip(ids, slots)))

    check(len(alloc.blocks) == 1, f"应只有一次连续分配，实测 {len(alloc.blocks)} 次")
    check(alloc.requests[0][0] == 2 * BLOCK_BYTES, "一次分配应覆盖两块")
    check(ids == [1, 2, 3, 4], f"FRAME_ID 应从 1 起严格递增到 4，实测 {ids}")
    check(all(ids[i] < ids[i + 1] for i in range(3)), f"FRAME_ID 必须严格递增，实测 {ids}")
    check(slots == [fid & 1 for fid in ids], "块序号必须严格等于 FRAME_ID & 1（契约 §7.2）")
    check(alt, f"块序号必须严格交替，实测 {slots}")
    check(sorted(set(slots)) == [0, 1], f"4 帧必须两块都用过，实测 {sorted(set(slots))}")
    check(all(content_ok), f"每帧内容必须落在自己那块，实测 {content_ok}")
    check(all(other_untouched),
          f"换帧前不得覆盖正在被扫的另一块（3 次检查），实测 {other_untouched}")
    check(pipe._slot(ids[0]) == ids[0] & 1 and pipe._slot(ids[3]) == ids[3] & 1,
          "_slot() 必须等于 FRAME_ID & 1（与 RTL 的 addr=buf*FB_SIZE 对应）")
    check(FB_BASE + 0 * BLOCK_BYTES >= pipe.fb_addr and
          FB_BASE + 1 * BLOCK_BYTES < pipe.fb_addr + 2 * BLOCK_BYTES,
          "两块地址必须落在同一次分配内（否则 PL 会读到别人的内存）")


# ---------------------------------------------------------------------------
# 判据 3：原子性
# ---------------------------------------------------------------------------
def judge3_atomic() -> None:
    print("\n[判据 3] 原子性：写块内容必须全部早于写 FRAME_ID")
    log = TraceLog()
    state: dict = {}

    def snapshot(fid: int):
        """写 FRAME_ID 那一刻，给「PL 将要采纳的那块」拍 md5。"""
        alloc, pipe = state.get("alloc"), state.get("pipe")
        if alloc is None or pipe is None or not pipe.block_bytes:
            return None
        off = (fid & 1) * pipe.block_bytes
        mem = alloc.memory
        return md5(mem[off:off + pipe.mode_def.frame_bytes].tobytes())

    alloc, pipe = _new(log, snapshot=snapshot)
    state.update(alloc=alloc, pipe=pipe)
    pipe.setup()

    frames = []
    for k in range(4):
        orig, proc = _input_mark(k)
        frames.append((pipe.show(orig, proc), compose(orig, proc, W=W, H=H)))

    bw = log.indices("block_write")
    fw = log.indices("fid_write")
    print(f"  事件时间线（长度 {len(log.events)}）：" + " > ".join(
        f"{k}({d[0] if k == 'fid_write' else d})" for k, d in log.events
        if k in ("block_write", "fid_write")))

    check(len(bw) == 4 and len(fw) == 4, f"应有 4 次写块 + 4 次写 FRAME_ID，实测 {len(bw)}/{len(fw)}")
    order = [bw[i] < fw[i] for i in range(min(len(bw), len(fw)))]
    check(all(order), f"每帧必须「先写块、后写 FRAME_ID」，实测 {order}")
    check(max(bw) < min(fw) or all(order), "整体上不得出现“先换帧、后补内容”")

    snap_ok, fid_ok = [], []
    for i, (fid, exp) in enumerate(frames):
        got_fid, snap = log.events[fw[i]][1]      # fid_write 的 detail = (frame_id, md5)
        fid_ok.append(int(got_fid) == int(fid))
        snap_ok.append(snap == md5(exp.tobytes()))
        print(f"  帧{fid}: block_write(seq {bw[i]}) < fid_write(seq {fw[i]})  "
              f"换帧瞬间块{int(fid) & 1} md5={snap}")
    check(all(fid_ok), f"写入的 FRAME_ID 必须就是 show() 的返回值，实测 {fid_ok}")
    check(all(snap_ok), f"写 FRAME_ID 的那一刻，目标块必须已是本帧完整内容，实测 {snap_ok}")
    check(all(order) and snap_ok.count(True) == 4,
          "“内容先于帧号”在 4 帧上必须全部成立，不允许任何一帧靠事后一致")


# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 74)
    print("显示流水线端到端自检（里程碑 1：判据 1~3；mode=3 / 720p30 / 1280x720）")
    print("=" * 74)
    judges = (judge1_bytes, judge2_double_buffer, judge3_atomic)
    try:
        for fn in judges:
            fn()
    except Exception:
        traceback.print_exc()
        print(f"\nFAIL: 自检异常中断（{len(FAILS)} 条已判失败 / {CHECKS[0]} 条检查）")
        return 1

    print("\n" + "-" * 74)
    if FAILS:
        print(f"FAIL: {len(FAILS)} 条判据未过（共 {CHECKS[0]} 条检查）")
        return 1
    print(f"OK: {CHECKS[0]} 条检查全部通过")
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
