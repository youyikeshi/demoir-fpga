"""PS 侧显示流水线：原图/处理图 → 整幅画面 → 写后台块 → ``FRAME_ID`` 换帧（默认 720p30）。

需求来源（唯一）
----------------
``docs/camera/05_HDMI输出_PS_PL接口契约.md``（§2 寄存器图 / §7.2 双缓冲 / §8.2+§8.9 心跳 /
§8.10+§8.11 720p30）与 ``docs/camera/03_显示构图规范_纯PS.md``。``display_layout.compose()``、
``hdmi_out.HdmiOut``+``REG``+``align64`` —— **只调用，不改**。

一帧
----
``compose(1280,720)`` ⇒ 1280×720×3 = 2,764,800 B（上下各 40 px 黑边已烘进缓冲）
⇒ ``np.copyto`` 块[``FRAME_ID & 1``] ⇒ ``write32(FRAME_ID, +1)`` ⇒ PL 在 vsync 采纳、扫另一块。

块地址（与 RTL 严格一致）：``buf = FRAME_ID & 1``、``addr = FB_ADDR_LO + buf*FB_SIZE``、
``FB_SIZE = align64(stride*H)`` ⇒ 两块**必须一次连续分配**（``allocator(2*FB_SIZE)``）。
``stride = W*3 = 3840`` 是 64 的整数倍，``FB_SIZE = align64(3840*720) = 2,764,800``。

模式（契约 §8.11）：0=720p60 74.21875 MHz / 1=1080p60 148.4375 / 2=1080p30 74.21875 /
**3=720p30 37.109375 MHz（默认，H/V=1650/1280, 750/720）**。

**已知冲突（未改 hdmi_out.py，只报告）**：``hdmi_out.MODES[3]`` 仍是旧口径 ``640x480p60``，
与契约 §8.11 矛盾 ⇒ 本模块**不读** ``MODES[mode]`` 的几何，只用自带 ``MODES_PS``，并把几何
显式传给 ``HdmiOut.configure(addr, stride, W, H)``。
**已知缺口**：``0x30 PIX_HB``（§8.2/§8.9）不在 ``hdmi_out.REG``（截至 ``0x2C``）⇒ 自带
``PIX_HB_OFF``；半周期 = 2^16 个像素时钟 ⇒ ``f_pix = 沿数 × 2^16 / 窗口秒数``，判据是
**有界窗口 ≥100 ms 且 ≥5 个沿**（“两次读不同”不可靠）。

**为什么首帧号是 1**：``enable=1`` 后 PL 立刻扫块 0；首帧写块 0 会被边写边扫 ⇒ 撕裂。
故首帧写块 1，块序 ``1,0,1,0``（交替、不重复、帧号严格递增）；要字面 ``0,1,0,1`` 就把
``FIRST_FRAME_ID`` 设 0，但首帧有撕裂风险。

可注入（**不 import pynq 也能跑**）：``reg_io`` = ``read32``/``write32``（默认 ``MmapRegIo``）；
``allocator`` = ``f(nbytes)->块``，块需有 ``device_address``、可被 numpy 访问、有 ``flush()``
就调用（cacheable 不回写 ⇒ PL 读到旧内容）。默认 ``pynq.allocate``，**只有它 import pynq**。
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from display_layout import compose  # noqa: E402  已板端验证，只调用
import hdmi_out as ho  # noqa: E402  只调用

__all__ = ["PIX_HB_OFF", "HB_HALF_DIV", "ModeDef", "MODES_PS", "DEFAULT_MODE",
           "DisplayPipeline", "default_allocate"]

PIX_HB_OFF = 0x30      #: 契约 §8.2 像素域心跳（hdmi_out.REG 尚未收录）
HB_MASK = 1 << 0       #: 0x30[0] 翻转位
HB_HALF_DIV = 1 << 16  #: 心跳半周期 = 2^16 个像素时钟
DEFAULT_MODE = 3       #: 720p30（契约 §8.10）


@dataclass(frozen=True)
class ModeDef:
    """一种显示模式（契约 §8.11）；``code`` 即 ``VIDEO_MODE`` 寄存器值。"""

    code: int
    name: str
    W: int
    H: int
    h_total: int
    v_total: int
    pix_clk_mhz: float

    @property
    def fps(self) -> float:
        return self.pix_clk_mhz * 1e6 / (self.h_total * self.v_total)

    @property
    def frame_period_s(self) -> float:
        return 1.0 / self.fps

    @property
    def stride(self) -> int:
        """``W*3``（行首无 padding；3840/5760 都是 64 的整数倍）。"""
        return self.W * 3

    @property
    def frame_bytes(self) -> int:
        return self.stride * self.H

    @property
    def block_bytes(self) -> int:
        """``align64(stride*H)`` —— 与 RTL 的块大小公式完全一致。"""
        return ho.align64(self.frame_bytes)


MODES_PS: dict[int, ModeDef] = {
    m.code: m for m in (
        ModeDef(0, "720p60", 1280, 720, 1650, 750, 74.21875),
        ModeDef(1, "1080p60", 1920, 1080, 2200, 1125, 148.4375),
        ModeDef(2, "1080p30", 1920, 1080, 2200, 1125, 74.21875),
        ModeDef(3, "720p30", 1280, 720, 1650, 750, 37.109375),
    )
}


def _check_mode(mode: object) -> int:
    if isinstance(mode, bool) or not isinstance(mode, (int, np.integer)):
        raise ValueError(f"mode 必须是 int，得到 {type(mode).__name__}")
    m = int(mode)
    if m not in MODES_PS:
        raise ValueError(f"mode 必须是 {sorted(MODES_PS)} 之一，得到 {m}")
    return m


def default_allocate(nbytes: int):
    """板端默认分配器：``pynq.allocate``（**唯一会 import pynq 的地方**）。"""
    from pynq import allocate  # 主机上没有 pynq ⇒ 传 allocator= 即可

    return allocate(int(nbytes), dtype=np.uint8)


def _alias_probe(block: object, view: np.ndarray) -> bool:
    """写哨兵字节确认视图与分配块同源 —— 防 ``np.asarray`` 静默拷贝（板端最坏情况）。"""
    try:
        old = int(view[0])
        probe = 0xA5 if old != 0xA5 else 0x5A
        view[0] = probe
        same = int(block[0]) == probe  # type: ignore[index]
        view[0] = old
        return same
    except Exception:
        return False


def _u8_view(block: object, nbytes: int) -> np.ndarray | None:
    """取分配块的一维可写 uint8 视图（不拷贝）；拿不到就返回 ``None``。"""
    getters = (
        lambda: block if isinstance(block, np.ndarray) else None,
        lambda: getattr(block, "array", None),
        lambda: np.asarray(block),
        lambda: np.frombuffer(memoryview(block), dtype=np.uint8),
    )
    for get in getters:
        try:
            arr = get()
        except Exception:
            continue
        if not isinstance(arr, np.ndarray) or arr.dtype != np.uint8:
            continue
        if not arr.flags.writeable or arr.nbytes < nbytes:
            continue
        view = arr.reshape(-1)[:nbytes]
        if _alias_probe(block, view):
            return view
    return None


class DisplayPipeline:
    """``compose`` → 双缓冲 → ``FRAME_ID`` 换帧 的端到端流水线（默认 720p30）。

    板端::

        pipe = DisplayPipeline().setup()          # mmap + pynq.allocate + 切模式协议
        if not pipe.pix_alive(100)[0]:            # 契约 §8.2：心跳不动就不要碰 AXI
            raise SystemExit("像素时钟没跑")
        pipe.wait(pipe.show(orig, proc), 0.5)     # 写后台块 → FRAME_ID+1 → 等 vsync 采纳
        print(pipe.stats()); pipe.close()

    主机自检时注入 ``reg_io`` / ``allocator`` 即可：不需要 pynq、不需要板子。
    """

    FIRST_FRAME_ID = 1                     #: 首帧号（见模块文档“为什么首帧号是 1”）
    BASE = ho.DEFAULT_BASE                 #: AXI-Lite 基址（契约 §2）

    def __init__(self, reg_io=None, mode: int = DEFAULT_MODE, allocator=None):
        self._mode = _check_mode(mode)
        self._io = reg_io
        self._owns_io = False
        self._alloc = allocator
        self._out: ho.HdmiOut | None = None
        self._blocks = None
        self._u8: np.ndarray | None = None
        self._fb_addr = 0
        self._block_bytes = 0
        self._frame_id = self.FIRST_FRAME_ID - 1
        self._shown = 0
        self._mode_switches = 0
        self._last_pix: tuple[bool, float] = (False, 0.0)
        self._pix_window = 0.0

    # ------------- 只读属性 -------------
    @property
    def mode_def(self) -> ModeDef:
        return MODES_PS[self._mode]

    @property
    def mode(self) -> int:
        return self._mode

    @property
    def frame_id(self) -> int:
        """已提交的最后一帧号；``FIRST_FRAME_ID-1`` 表示还没有帧。"""
        return self._frame_id

    @property
    def block_bytes(self) -> int:
        return self._block_bytes

    @property
    def fb_addr(self) -> int: return self._fb_addr

    # ------------- 寄存器小工具 -------------
    def _wr(self, name: str, value: int) -> None:
        self._io.write32(ho.REG[name], int(value) & 0xFFFF_FFFF)

    def _rd(self, name: str) -> int:
        return int(self._io.read32(ho.REG[name])) & 0xFFFF_FFFF

    # ------------- 生命周期 -------------
    def setup(self) -> "DisplayPipeline":
        """分配双缓冲 → 切模式（契约协议）→ 写几何 → ``enable=1``。"""
        d = self.mode_def
        self._check_stride(d.stride)
        if self._io is None:
            self._io = ho.MmapRegIo(self.BASE)
            self._owns_io = True
        if self._out is None:
            # 只借寄存器读写；几何一律由 configure() 显式给（MODES[3] 口径有冲突）
            self._out = ho.HdmiOut(self.BASE, reg_io=self._io, mode=self._mode)
        self._ensure_buffers()
        self._apply_mode()
        return self

    def _ensure_buffers(self) -> None:
        want = self.mode_def.block_bytes
        if self._blocks is not None and want == self._block_bytes:
            return
        self._free_buffers()
        alloc = self._alloc if self._alloc is not None else default_allocate
        blk = alloc(2 * want)  # 一次连续分配：PL 用 FB_ADDR + buf*FB_SIZE 算第二块
        addr = int(getattr(blk, "device_address", 0) or getattr(blk, "physical_address", 0))
        if addr <= 0:
            raise ho.RegError("分配块没有 device_address/physical_address，PL 无法访问")
        if addr % 64:
            raise ValueError(f"帧缓冲物理地址 0x{addr:X} 未按 64 B 对齐（契约 §2）")
        self._blocks, self._block_bytes, self._fb_addr = blk, want, addr
        self._u8 = _u8_view(blk, 2 * want)  # None ⇒ 退回分配块自身切片赋值

    def _apply_mode(self) -> None:
        """契约 §8.11：``enable=0`` → 写 ``VIDEO_MODE`` → 等 ≥2 帧 → ``enable=1``。"""
        d = self.mode_def
        self._out.enable(False)
        self._out.set_mode(self._mode)           # 顺便同步 HdmiOut 内部状态
        time.sleep(2.05 * d.frame_period_s)      # ≥2 整帧，让 BUFGMUX/时序稳下来
        # configure() 写 FB_ADDR/STRIDE/W/H/PIXEL_FMT/VIDEO_MODE，收尾 enable(True)
        self._out.configure(self._fb_addr, d.stride, d.W, d.H)
        self._mode_switches += 1

    def set_mode(self, mode: int) -> None:
        """切模式（同一套协议）；几何变了会自动重分配并重写帧缓冲地址。"""
        self._mode = _check_mode(mode)
        if self._out is None:
            self.setup()
            return
        self._ensure_buffers()
        self._apply_mode()

    # ------------- 帧提交 -------------
    @staticmethod
    def _slot(frame_id: int) -> int:
        """块序号 = ``FRAME_ID & 1``（契约 §7.2，对应 RTL ``addr = FB_ADDR + FRAME_ID[0]*FB_SIZE``）。"""
        return int(frame_id) & 1

    def show(self, orig: np.ndarray, proc: np.ndarray) -> int:
        """构图 → 写**后台块** → ``FRAME_ID += 1``（顺序即原子性）→ 返回新帧号。"""
        if self._out is None or self._blocks is None:
            raise ho.RegError("还没 setup()：先分配双缓冲并配置 PL")
        d = self.mode_def
        frame = compose(orig, proc, d.W, d.H)  # 含上下黑边；只调用 compose
        self._check_frame(frame)
        fid = (self._frame_id + 1) & 0xFFFF_FFFF
        self._write_block(self._slot(fid), frame)  # ① 整帧先落地
        self._publish(fid)                         # ② 再原子换帧
        self._frame_id = fid
        self._shown += 1
        return fid

    def _write_block(self, slot: int, frame: np.ndarray) -> None:
        """把一帧拷进第 ``slot`` 块（**必须早于** ``FRAME_ID`` 写入；测试据此判原子性）。"""
        start = int(slot) * self._block_bytes
        flat = frame.reshape(-1) if frame.flags["C_CONTIGUOUS"] else np.ascontiguousarray(frame)
        n = flat.size
        if self._u8 is not None:
            np.copyto(self._u8[start : start + n], flat.reshape(-1), casting="no")
        else:
            self._blocks[start : start + n] = flat  # pynq Buffer 的切片赋值
        flush = getattr(self._blocks, "flush", None)
        if flush is not None:
            flush()  # cacheable 分配必须回写，否则 PL 读到旧内容

    def _publish(self, frame_id: int) -> None:
        self._wr("FRAME_ID", frame_id)   # PL 在 vsync 边界采纳

    def wait(self, frame_id: int, timeout: float = 0.5) -> bool:
        """轮询 ``FRAME_ID_LATCHED`` 直到 PL 采纳；超时 False（画面仍旧帧，无撕裂）。"""
        fid = int(frame_id) & 0xFFFF_FFFF
        deadline = time.monotonic() + float(timeout)
        while True:
            if int(self._io.read32(ho.REG["FRAME_ID_LATCHED"])) & 0xFFFF_FFFF == fid:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0)

    # ------------- 诊断 -------------
    def pix_alive(self, window_ms: float = 100) -> tuple[bool, float]:
        """有界窗口内数 ``0x30[0]`` 的翻转沿 ⇒ ``(活?, 反推像素时钟 MHz)``。

        判活只要 100 ms（720p30 下仍有 ≈56 个沿，远超 ≥5）。**频率精度受沿量化限制**：
        误差 ≈ 1/窗口内沿数（720p30 沿周期 1.77 ms ⇒ 100 ms 只有 ±1.8%，±0.1% 需 ≈2 s）。
        """
        if not (window_ms > 0):
            raise ValueError(f"window_ms 必须 > 0，得到 {window_ms}")
        t0 = time.monotonic()
        t_end = t0 + window_ms / 1000.0
        last = int(self._io.read32(PIX_HB_OFF)) & HB_MASK
        edges = 0
        while time.monotonic() < t_end:   # 只受墙钟约束（不限 poll 次数，否则窗口被悄悄截短）
            v = int(self._io.read32(PIX_HB_OFF)) & HB_MASK
            if v != last:
                edges += 1
                last = v
        dt = max(time.monotonic() - t0, 1e-9)
        mhz = edges * HB_HALF_DIV / dt / 1e6
        alive = edges >= 5
        self._last_pix, self._pix_window = (alive, mhz), dt
        return alive, mhz

    def stats(self) -> dict:
        d = self.mode_def
        st = {"mode": self._mode, "mode_name": d.name, "W": d.W, "H": d.H,
              "stride": d.stride, "frame_bytes": d.frame_bytes,
              "block_bytes": self._block_bytes, "fb_addr": self._fb_addr,
              "frame_id": self._frame_id, "shown": self._shown,
              "mode_switches": self._mode_switches,
              "pix_clk_mhz_expected": d.pix_clk_mhz,
              "pix_clk_mhz_measured": round(self._last_pix[1], 4),
              "pix_window_ms": round(self._pix_window * 1e3, 2),
              "pix_alive": self._last_pix[0]}
        if self._out is not None:
            st.update(self._out.status())
            st["frame_id_latched"] = self._out.frame_latched()
        return st

    # ------------- 校验 -------------
    def _check_stride(self, stride: int) -> None:
        d = self.mode_def
        if stride < d.W * 3:
            raise ValueError(f"stride={stride} < W*3={d.W * 3}：一行放不下 {d.W} 个像素")
        if stride % 64:
            raise ValueError(f"stride={stride} 未按 64 B 对齐（契约 §2：PL 以 8 拍=64 B 发突发）")
        if ho.align64(stride * d.H) > ho.HdmiOut.MAX_FRAME_BYTES + 4096:
            raise ValueError(f"单块 {ho.align64(stride * d.H)} B 超出驱动上限")

    def _check_frame(self, frame: np.ndarray) -> None:
        d = self.mode_def
        if not isinstance(frame, np.ndarray):
            raise ValueError(f"frame 必须是 numpy.ndarray，得到 {type(frame).__name__}")
        if frame.dtype != np.uint8:
            raise ValueError(f"frame dtype 必须是 uint8，得到 {frame.dtype}；不做静默 cast")
        if frame.shape != (d.H, d.W, 3):
            raise ValueError(f"frame 形状必须是 {(d.H, d.W, 3)}，得到 {frame.shape}")
        if frame.nbytes != d.frame_bytes:
            raise ValueError(f"frame 字节数 {frame.nbytes} != stride*H={d.frame_bytes}")

    # ------------- 清理 -------------
    def _free_buffers(self) -> None:
        blk, self._blocks = self._blocks, None
        self._u8, self._block_bytes, self._fb_addr = None, 0, 0
        free = getattr(blk, "freebuffer", None)
        if free is not None:
            try:
                free()
            except Exception:
                pass

    def close(self) -> None:
        self._free_buffers()
        if self._out is not None:
            self._out.close()
            self._out = None
        if self._owns_io and self._io is not None and hasattr(self._io, "close"):
            self._io.close()
        self._owns_io = False

    def __repr__(self) -> str:
        d = self.mode_def
        return (f"DisplayPipeline(mode={self._mode}:{d.name} {d.W}x{d.H} stride={d.stride} "
                f"fb=0x{self._fb_addr:08X} frame_id={self._frame_id})")
