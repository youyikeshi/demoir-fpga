#!/usr/bin/env python3
"""显示流水线端到端主机自检 —— **判据 1~8**（里程碑 1 的 1~3 + 里程碑 2 的 4~8）。

被测：``ps/pynq/display_demo.DisplayPipeline``（compose → 双缓冲 → ``FRAME_ID`` 换帧）
互补：``tools/hdmi_selftest.py``（119 条）管的是“PL 按 stride/8 拍扫描能否读回 compose 的字节”
+ 寄存器图 + 握手；本文件**不重复**它，只判“PS 侧流水线：字节写对、块选对、换帧有序、
参数守住、心跳反推可信、模式切换合规”。

判据
----
1. **字节流一致**：假 allocator 抓内存 ⇒ 写进第 ``FRAME_ID&1`` 块的字节与 ``compose()``
   输出**逐字节相同**（2,764,800 B 全量，不抽样）。
2. **双缓冲**：连续 4 帧 ⇒ 块序号严格交替且等于 ``FRAME_ID & 1``、``FRAME_ID`` 严格递增；
   每块内容 == 本帧；**正被扫的另一块在换帧前不被覆盖**。
3. **原子性**：内存写与寄存器写放进**同一条事件时间线** ⇒ “写块”(``block_write``) 全部早于
   “写 ``FRAME_ID``”(``fid_write``)；并在写 ``FRAME_ID`` 那一刻给目标块拍 md5 快照。
4. **参数校验**：坏 mode / 坏 shape / 坏 dtype / 非数组 / stride 未 64 B 对齐 / 地址未对齐 /
   无 ``device_address`` / 未 ``setup()`` / ``pix_alive`` 窗口非法 —— 必须直接抛，不许静默降级。
5. **心跳反推**（契约 §8.13 口径）：``pix_alive(100 ms)`` **只判活着**（≥5 沿）；
   **精度声明必须 ≥2 s 窗口**（N≈1132 沿 ⇒ ±0.09%），故 2 s 窗口下断言相对误差 ≤0.1%。
6. **模式切换写入顺序**：``enable=0 → 写 VIDEO_MODE → 等 ≥2 整帧 → enable=1``（用事件时间戳量）。
7. **三种图案 × 两种模式**（720p30 + 720p60，另加 1080p60 几何重分配）：字节仍逐字节一致。
8. **``setup()`` 后两块全黑**：预填必须发生在 ``enable=1`` 之前，否则首帧前屏幕是垃圾内存。

用法 / 退出码
-------------
``python3 tools/display_e2e_selftest.py`` ⇒ 全过 ``rc=0`` 且末行打印 ``PASS``。
不 import pynq、不碰板子（192.168.7.2 全程零访问）。**判据 5 需要 ~6 s 墙钟**（三个 2 s 窗口）。
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
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


def _input_checker() -> tuple[np.ndarray, np.ndarray]:
    y, x = np.mgrid[0:256, 0:256]
    k = (((x // 32) + (y // 32)) & 1).astype(np.uint8)
    orig = np.stack([k * 255, k * 255, k * 255], -1).astype(np.uint8)
    proc = np.stack([255 - k * 255, (x * 2) & 0xFF, (y * 2) & 0xFF], -1).astype(np.uint8)
    return orig, proc


def _input_photo() -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(20261103)
    base = rng.integers(0, 256, size=(16, 16, 3), dtype=np.uint8)
    orig = np.repeat(np.repeat(base, 16, axis=0), 16, axis=1)
    proc = np.clip(orig.astype(np.int16) // 2 + 64, 0, 255).astype(np.uint8)
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
        self.times: list[float] = []          # 与 events 一一对应（判据 6 量停顿用）

    def add(self, kind: str, detail=None) -> int:
        self.events.append((kind, detail))
        self.times.append(time.monotonic())
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

    def _prefill_blocks(self) -> None:
        super()._prefill_blocks()
        self.log.add("prefill", None)          # 判据 8：预填必须早于 enable=1


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
# ---------------------------------------------------------------------------
# 判据 4：参数校验
# ---------------------------------------------------------------------------
class _NoAddrAlloc:
    """分配块没有 ``device_address``（非 PYNQ 后端返回裸数组）⇒ 必须 ``RegError``。"""

    def __call__(self, nbytes: int) -> np.ndarray:
        return np.zeros(int(nbytes), dtype=np.uint8)


def judge4_guards() -> None:
    print("\n[判据 4] 参数校验：坏输入必须直接抛（不许静默降级）")

    def expect(exc, fn, desc) -> None:
        try:
            fn()
        except exc as e:
            check(True, f"{desc} ⇒ {type(e).__name__}")
            print(f"  ✅ {desc:<34s} ⇒ {type(e).__name__}: {str(e)[:46]}")
        except Exception as e:  # noqa: BLE001
            check(False, f"{desc} 抛了 {type(e).__name__}（应为 {exc.__name__}）: {e}")
        else:
            check(False, f"{desc} 没有抛异常")

    log = TraceLog()
    pipe = TracedPipeline(log, reg_io=RecordingRegIo(log), mode=3, allocator=FakeAllocator(log))
    orig, proc = _input_color()

    for bad in (4, -1, "3", None, True, 3.5):
        expect(ValueError, lambda b=bad: dd.DisplayPipeline(reg_io=RecordingRegIo(TraceLog()), mode=b),
               f"mode={bad!r}")
    expect(ho.RegError, lambda: pipe.show(orig, proc), "未 setup() 就 show()")

    pipe.setup()
    expect(ValueError, lambda: pipe.show(orig[:255], proc), "orig 形状 (255,256,3)")
    expect(ValueError, lambda: pipe.show(orig.astype(np.float32), proc), "orig dtype float32")
    expect(ValueError, lambda: pipe.show(orig.tolist(), proc), "orig 是 list")
    expect(ValueError, lambda: pipe.show(orig, proc[:, :100]), "proc 形状 (256,100,3)")

    expect(ValueError, lambda: pipe._check_stride(3841), "_check_stride(3841) 未 64B 对齐")
    expect(ValueError, lambda: pipe._check_stride(1280), "_check_stride(1280) < W*3")
    try:
        pipe._check_stride(3840)
        check(True, "_check_stride(3840) 合法（3840 = 60*64）")
    except ValueError as e:
        check(False, f"_check_stride(3840) 不该炸: {e}")

    expect(ValueError,
           lambda: dd.DisplayPipeline(reg_io=RecordingRegIo(TraceLog()), mode=3,
                                      allocator=lambda n: FakeBlock(int(n), 0x1F00_0008)).setup(),
           "分配地址未 64 B 对齐")
    expect(ho.RegError,
           lambda: dd.DisplayPipeline(reg_io=RecordingRegIo(TraceLog()), mode=3,
                                      allocator=_NoAddrAlloc()).setup(),
           "分配块没有 device_address")

    for bad in (0, -1, 0.0):
        expect(ValueError, lambda b=bad: pipe.pix_alive(b), f"pix_alive(window_ms={bad})")
    fb = pipe._rd("FB_STRIDE")
    check(fb == 3840, f"FB_STRIDE 应写 3840，实测 {fb}")


# ---------------------------------------------------------------------------
# 判据 5：心跳反推（§8.13 口径）
# ---------------------------------------------------------------------------
class HbIo:
    """物理自洽的假 0x30：按**真实流逝时间**推进像素时间轴（不是按读次数），
    这样“沿数 × 2^16 / 墙钟秒数”才等价于真实像素时钟。"""

    def __init__(self, mhz: float):
        self.f = float(mhz) * 1e6
        self.t0 = time.monotonic()

    def read32(self, off: int) -> int:
        if off != ho.REG["PIX_HB"]:
            return 0
        return int(((time.monotonic() - self.t0) * self.f) // dd.HB_HALF_DIV) & 1

    def write32(self, off: int, v: int) -> None:  # pragma: no cover
        pass


class DeadHbIo:
    """像素时钟停摆：``0x30[0]`` 恒 0（``[1]`` 还给 1，即“锁了但不翻”这种更坑的反例）。"""

    def read32(self, off: int) -> int:
        return 0x2 if off == ho.REG["PIX_HB"] else 0

    def write32(self, off: int, v: int) -> None:  # pragma: no cover
        pass


def judge5_heartbeat() -> None:
    print("\n[判据 5] 心跳反推（§8.13：100 ms 只判活；2 s 才谈精度）")

    mhz = 37.109375
    p = dd.DisplayPipeline(reg_io=HbIo(mhz), mode=3, allocator=lambda n: None)
    alive, got = p.pix_alive(100)
    err = 100 * (got - mhz) / mhz if got else 0.0
    edges = int(round(got * 1e6 * p.stats()["pix_window_ms"] / 1e3 / dd.HB_HALF_DIV))
    check(alive, f"{mhz} MHz 下 100 ms 窗口应判“活着”（≥5 沿）")
    check(abs(err) <= 10.0, f"100 ms 只判活（容忍 ±10%），实测 {err:+.2f}%")
    check(edges >= 5, f"沿数应 ≥5，实测 {edges}")
    print(f"  100 ms : {got:.4f} MHz（{err:+.2f}%，沿数 {edges}）⇒ 只判“活着”，"
          f"**不作精度声称**（理论 ±1.8%）")

    p = dd.DisplayPipeline(reg_io=DeadHbIo(), mode=3, allocator=lambda n: None)
    alive, got = p.pix_alive(100)
    check(alive is False, "0x30[0] 不翻必须判“死”（哪怕 [1] mmcm_locked=1）")
    check(got == 0.0, f"死时钟反推应为 0.0 MHz，实测 {got}")
    print(f"  停摆   : alive={alive}  {got:.4f} MHz  ⇒ 立刻停手，不要继续 AXI 访问（§8.2）")

    for mhz in (37.109375, 74.21875, 148.4375):
        p = dd.DisplayPipeline(reg_io=HbIo(mhz), mode=3, allocator=lambda n: None)
        t0 = time.monotonic()
        alive, got = p.pix_alive(2000)
        dt = time.monotonic() - t0
        win = p.stats()["pix_window_ms"]
        err = 100 * (got - mhz) / mhz
        n_edges = int(round(got * 1e6 * win / 1e3 / dd.HB_HALF_DIV))
        check(alive, f"{mhz} MHz 在 2 s 窗口应判活")
        check(abs(err) <= 0.1, f"{mhz} MHz 在 2 s 窗口误差应 ≤0.1%，实测 {err:+.4f}%")
        check(win >= 1900.0, f"2 s 窗口不得被截短，实测 {win:.0f} ms")
        print(f"  2 s    : {mhz:>10.6f} → {got:10.4f} MHz  err {err:+.4f}%  "
              f"(窗口 {win:.0f} ms, dt {dt:.3f} s, 沿数 ≈{n_edges})")


# ---------------------------------------------------------------------------
# 判据 6：模式切换写入顺序
# ---------------------------------------------------------------------------
CTRL_EN = 1 << 0


def _switch_report(log: TraceLog, mode_code: int, fps: float, tag: str) -> None:
    ev = log.events
    i_en0 = next((i for i, (k, d) in enumerate(ev)
                  if k == "reg_write" and d[0] == ho.REG["CTRL"] and not (d[1] & CTRL_EN)), None)
    i_vm = next((i for i, (k, d) in enumerate(ev)
                 if k == "reg_write" and d[0] == ho.REG["VIDEO_MODE"] and d[1] == mode_code), None)
    i_en1 = next((i for i in range(len(ev) - 1, -1, -1)
                  if ev[i][0] == "reg_write" and ev[i][1][0] == ho.REG["CTRL"]
                  and (ev[i][1][1] & CTRL_EN)), None)
    ok = None not in (i_en0, i_vm, i_en1) and i_en0 < i_vm < i_en1
    gap = (log.times[i_en1] - log.times[i_en0]) if ok else -1.0
    need = 2.0 / fps
    mid = [i for i in range(i_en0 + 1, i_en1)
           if ev[i][0] == "reg_write" and ev[i][1][0] == ho.REG["CTRL"] and (ev[i][1][1] & CTRL_EN)] \
        if ok else [-1]
    check(ok, f"{tag}: 写序必须 enable=0 < VIDEO_MODE={mode_code} < enable=1（实测 {i_en0}/{i_vm}/{i_en1}）")
    check(gap >= need, f"{tag}: enable=0→enable=1 应 ≥2 帧 {need * 1e3:.1f} ms，实测 {gap * 1e3:.1f} ms")
    check(not mid, f"{tag}: 切模式过程中不许提前 enable=1（实测 {mid}）")
    print(f"  {tag:<16s}: enable=0(seq {i_en0}) → VIDEO_MODE={mode_code}(seq {i_vm}) → "
          f"停 {gap * 1e3:5.1f} ms（≥{need * 1e3:.1f}）→ enable=1(seq {i_en1})")


def judge6_mode_switch() -> None:
    print("\n[判据 6] 模式切换写入顺序（契约 §8.11 协议）")
    log = TraceLog()
    alloc = FakeAllocator(log)
    pipe = TracedPipeline(log, reg_io=RecordingRegIo(log), mode=3, allocator=alloc)
    pipe.setup()
    _switch_report(log, 3, ho.MODES[3].fps, "setup(720p30)")

    log.events.clear()
    log.times.clear()
    fb0, nb0 = pipe.fb_addr, len(alloc.blocks)
    pipe.set_mode(0)
    _switch_report(log, 0, ho.MODES[0].fps, "3→0(720p60)")
    check(len(alloc.blocks) == nb0 and pipe.fb_addr == fb0,
          "720p30→720p60 几何相同 ⇒ 必须复用缓冲，不许重分配")
    check(pipe._rd("VIDEO_MODE") == 0, "VIDEO_MODE 应已写 0")

    log.events.clear()
    log.times.clear()
    pipe.set_mode(1)
    _switch_report(log, 1, ho.MODES[1].fps, "0→1(1080p60)")
    m1 = ho.MODES[1]
    stride1 = m1.W * 3
    blk1 = ho.align64(stride1 * m1.H)
    check(pipe.block_bytes == blk1, f"1080p60 块大小应 {blk1}，实测 {pipe.block_bytes}")
    check(len(alloc.blocks) == nb0 + 1, "几何变化必须重分配恰好一次")
    check(alloc.requests[-1][0] == 2 * blk1, f"必须一次分到两块 {2 * blk1} B")
    check(pipe.fb_addr % 64 == 0, f"新帧缓冲地址必须 64 B 对齐（实测 0x{pipe.fb_addr:X}）")
    check(pipe._rd("FB_STRIDE") == stride1, f"FB_STRIDE 应写 {stride1}")
    check(pipe._rd("FB_W") == 1920 and pipe._rd("FB_H") == 1080, "FB_W/FB_H 应写 1920/1080")
    print(f"  几何变化: 复用到 1080p60 ⇒ 重分配 1 次，{2 * blk1:,} B，stride={stride1}")


# ---------------------------------------------------------------------------
# 判据 7：三种图案 × 两种模式
# ---------------------------------------------------------------------------
def judge7_patterns_modes() -> None:
    print("\n[判据 7] 三种图案 × 两种模式（720p30 / 720p60）+ 1080p60 补一点")
    patterns = (("color", _input_color), ("checker", _input_checker), ("photo", _input_photo))

    for mode_code in (3, 0):
        log = TraceLog()
        alloc = FakeAllocator(log)
        pipe = TracedPipeline(log, reg_io=RecordingRegIo(log), mode=mode_code, allocator=alloc)
        pipe.setup()
        m = ho.MODES[mode_code]
        blk = ho.align64(m.W * 3 * m.H)
        for name, gen in patterns:
            orig, proc = gen()
            fid = pipe.show(orig, proc)
            exp = compose(orig, proc, m.W, m.H)
            off = (fid & 1) * pipe.block_bytes
            got = alloc.memory[off:off + exp.nbytes]
            check(np.array_equal(got, exp.reshape(-1)),
                  f"{m.name}/{name}: 字节应逐字节相同（{exp.nbytes:,} B）")
            check(pipe.block_bytes == blk,
                  f"{m.name}/{name}: 块大小应为 align64(stride*H)={blk}")
            check((fid & 1) == pipe._slot(fid),
                  f"{m.name}/{name}: 块序号应等于 FRAME_ID&1")
        print(f"  {m.name:<8s}: {m.W}x{m.H} stride={m.W * 3} block={blk:,} B × 3 图案 ⇒ 逐字节一致")

    log = TraceLog()
    alloc = FakeAllocator(log)
    pipe = TracedPipeline(log, reg_io=RecordingRegIo(log), mode=1, allocator=alloc)
    pipe.setup()
    m = ho.MODES[1]
    orig, proc = _input_color()
    fid = pipe.show(orig, proc)
    exp = compose(orig, proc, m.W, m.H)
    off = (fid & 1) * pipe.block_bytes
    check(np.array_equal(alloc.memory[off:off + exp.nbytes], exp.reshape(-1)),
          "1080p60/color: 字节应逐字节相同（6,220,800 B）")
    print(f"  1080p60 : {m.W}x{m.H} block={pipe.block_bytes:,} B ⇒ 逐字节一致")

    check(ho.MODES[3].W == ho.MODES[0].W == 1280 and ho.MODES[3].H == ho.MODES[0].H == 720,
          "720p30 与 720p60 几何应相同（1280x720），只有像素时钟不同")
    check(abs(ho.MODES[3].pix_clk_mhz - 37.109375) < 1e-9
          and abs(ho.MODES[0].pix_clk_mhz - 74.25) < 1e-6,
          f"像素时钟口径：mode3={ho.MODES[3].pix_clk_mhz} / mode0={ho.MODES[0].pix_clk_mhz}")


# ---------------------------------------------------------------------------
# 判据 8：setup() 后两块全黑
# ---------------------------------------------------------------------------
def judge8_prefill() -> None:
    print("\n[判据 8] setup() 后两块缓冲必须全黑（预填早于 enable=1）")
    log = TraceLog()
    alloc = FakeAllocator(log)
    pipe = TracedPipeline(log, reg_io=RecordingRegIo(log), mode=3, allocator=alloc)
    pipe.setup()

    mem = alloc.memory
    b = pipe.block_bytes
    nz0 = int(np.count_nonzero(mem[:b]))
    nz1 = int(np.count_nonzero(mem[b:2 * b]))
    check(mem.nbytes == 2 * b, f"分配应覆盖两块 {2 * b:,} B，实测 {mem.nbytes:,} B")
    check(nz0 == 0, f"块0 在 setup() 返回后必须全 0，实测非零 {nz0} B")
    check(nz1 == 0, f"块1 在 setup() 返回后必须全 0，实测非零 {nz1} B")

    i_pf = next((i for i, (k, _) in enumerate(log.events) if k == "prefill"), None)
    en1 = [i for i, (k, d) in enumerate(log.events)
           if k == "reg_write" and d[0] == ho.REG["CTRL"] and (d[1] & CTRL_EN)]
    check(i_pf is not None, "必须存在预填动作")
    check(bool(en1) and i_pf < en1[-1],
          f"预填(seq {i_pf}) 必须早于最后一次 enable=1(seq {en1[-1] if en1 else None})")
    print(f"  两块 {b:,} B × 2 = {2 * b:,} B 全零；prefill seq {i_pf} < enable=1 seq {en1[-1]}"
          f" ⇒ 首帧之前屏幕是黑的，不是未初始化内存")


def main() -> int:
    print("=" * 74)
    print("显示流水线端到端自检（判据 1~8；默认 mode=3 / 720p30 / 1280x720）")
    print("=" * 74)
    judges = (judge1_bytes, judge2_double_buffer, judge3_atomic, judge4_guards,
              judge5_heartbeat, judge6_mode_switch, judge7_patterns_modes, judge8_prefill)
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
