"""HDMI P2 的 PS 侧驱动（**不 import pynq**，主机可单测）。

唯一需求来源
------------
- ``docs/camera/05_HDMI输出_PS_PL接口契约.md`` §2 寄存器图 / §4 P2 判据
- ``docs/camera/03_显示构图规范_纯PS.md`` §2 帧缓冲布局
- ``ps/pynq/display_layout.py`` 的 ``compose()`` / ``FrameBuffer`` 语义

本模块只做三件事
----------------
1. 按契约寄存器图把配置写进 PL（``REG`` 表集中管理偏移，不散落魔数）；
2. 把 ``compose()`` 烘好的整幅画面拷进 **双缓冲的“后台”**，然后用
   ``FRAME_ID`` 通知 PL 在 **vsync 边界** 采纳（防撕裂）；
3. 轮询 ``FRAME_ID_LATCHED`` 确认切换完成，并读 ``STATUS`` / ``UNDERFLOW_CNT``
   做现场诊断。

双缓冲约定（**与 RTL 严格一致，改一边必须改另一边**）
----------------------------------------------------
::

    buf  = frame_id & 1
    addr = FB_ADDR_LO + buf * FB_SIZE,   FB_SIZE = align64(FB_STRIDE * FB_H)

- ``put_frame()`` 第 n 次调用生成 ``frame_id = n``，写进 ``buf = n & 1``；
- PL 在 vsync 边界采纳 ``frame_id`` 后，**后续所有行**都从 ``buf = frame_id & 1`` 读；
- 所以“正在被扫描的块”与“正在被写的块”永远是两块不同的内存。

硬件抽象（可注入，便于主机单测）
--------------------------------
只要求 ``reg_io`` 提供两个方法::

    reg_io.read32(offset) -> int      # offset 相对 base 的字节偏移
    reg_io.write32(offset, value)

默认实现 ``MmapRegIo`` 用 ``/dev/mem`` + ``mmap``（板端）。测试用
``FakeRegIo``（见 ``tools/hdmi_selftest.py``）即可在主机上跑全部逻辑。

不依赖 pynq
-----------
构造时**不会** import pynq；``MmapRegIo`` 只用标准库 ``os``/``mmap``/``struct``。
"""

from __future__ import annotations

import mmap
import os
import struct
import time

import numpy as np

__all__ = [
    "REG",
    "MODE_720P60",
    "MODE_1080P60",
    "MODE_1080P30",
    "MODE_640X480P60",
    "VideoMode",
    "MODES",
    "HdmiOut",
    "MmapRegIo",
    "FakeRegIo",
    "align64",
    "RegError",
]

# ---------------------------------------------------------------------------
# 寄存器图（契约 §2 + §7.3 扩容的 0x2C）—— **唯一定义处**
# ---------------------------------------------------------------------------
REG = {
    "CTRL":              0x00,   # RW [0]enable [1]soft_reset [2]test_pattern [3]frame_swap_req
    "STATUS":            0x04,   # RO [0]init_done [1]vsync_seen [2]scan_underflow(粘滞)
    "VIDEO_MODE":        0x08,   # RW [1:0] 0=720p60 1=1080p60 2=1080p30 3=640x480p60
    "FB_ADDR_LO":        0x0C,   # RW 帧缓冲物理地址 [31:0]，64 B 对齐
    "FB_ADDR_HI":        0x10,   # RW 本板 1 GB DDR ⇒ 恒 0
    "FB_STRIDE":         0x14,   # RW 每行字节数（**必须 64 B 对齐**且 ≥ W*3）
    "FB_W":              0x18,   # RW 有效像素宽度
    "FB_H":              0x1C,   # RW 有效行数
    "FRAME_ID":          0x20,   # RW PS 写新帧号；PL 在 vsync 边界采纳
    "FRAME_ID_LATCHED":  0x24,   # RO PL 已采纳的帧号
    "PIXEL_FMT":         0x28,   # RW 0 = RGB888
    "UNDERFLOW_CNT":     0x2C,   # RO 自 soft_reset 起“被读空的行数”，饱和 32 bit
}

# CTRL 位
CTRL_ENABLE       = 1 << 0
CTRL_SOFT_RESET   = 1 << 1
CTRL_TEST_PATTERN = 1 << 2
CTRL_FRAME_SWAP   = 1 << 3

# STATUS 位
STATUS_INIT_DONE    = 1 << 0
STATUS_VSYNC_SEEN   = 1 << 1
STATUS_SCAN_UNDERFLOW = 1 << 2

# PIXEL_FMT
PIXEL_FMT_RGB888 = 0

#: 模块基址（契约 §2：独立 64 KB 块，与 0x4004/0x4006 不冲突）
DEFAULT_BASE = 0x400A_0000


class RegError(RuntimeError):
    """寄存器/参数层面的错误（不是硬件故障）。"""


# ---------------------------------------------------------------------------
# 显示模式（契约 §1）
# ---------------------------------------------------------------------------
class VideoMode:
    """一种视频模式：模式号 + 有效区尺寸 + 时序参数（PL 侧用前两项即可）。"""

    __slots__ = ("code", "name", "W", "H", "pix_clk_mhz",
                 "h_total", "h_active", "v_total", "v_active")

    def __init__(self, code, name, W, H, pix_clk_mhz, h_total, h_active, v_total, v_active):
        self.code = code
        self.name = name
        self.W = W
        self.H = H
        self.pix_clk_mhz = pix_clk_mhz
        self.h_total = h_total
        self.h_active = h_active
        self.v_total = v_total
        self.v_active = v_active

    #: 一帧像素的字节数（stride = W*3）
    @property
    def frame_bytes(self) -> int:
        return self.W * self.H * 3

    @property
    def fps(self) -> float:
        """刷新率（由像素时钟与总时序推出，与面板标称值一致到 <0.1%）。"""
        return self.pix_clk_mhz * 1e6 / (self.h_total * self.v_total)

    @property
    def pixel_rate_mpx_s(self) -> float:
        """有效像素吞吐（Mpx/s）= W*H*fps；×3 就是 DDR 需要的带宽（MB/s）。"""
        return self.W * self.H * self.fps / 1e6

    def __repr__(self):
        return (f"VideoMode({self.code}:{self.name} {self.W}x{self.H} "
                f"@{self.pix_clk_mhz}MHz)")


MODE_720P60        = 0
MODE_1080P60       = 1
MODE_1080P30       = 2
MODE_640X480P60    = 3

MODES = {
    MODE_720P60:     VideoMode(0, "720p60",     1280, 720,  74.25,  1650, 1280, 750,  720),
    MODE_1080P60:    VideoMode(1, "1080p60",    1920, 1080, 148.5,  2200, 1920, 1125, 1080),
    MODE_1080P30:    VideoMode(2, "1080p30",    1920, 1080, 74.25,  2200, 1920, 1125, 1080),
    MODE_640X480P60: VideoMode(3, "640x480p60", 640,  480,  25.175, 800,  640,  525,  480),
}


def align64(n: int) -> int:
    """向上对齐到 64 B（与 RTL 的 ``align64(FB_STRIDE*FB_H)`` 必须一致）。"""
    return (int(n) + 63) & ~63


# ---------------------------------------------------------------------------
# 两种 reg_io 实现
# ---------------------------------------------------------------------------
class MmapRegIo:
    """``/dev/mem`` + mmap 的最小寄存器访问（板端用）。

    只支持 32-bit 访问（契约 §2）；每次读都做 ``volatile`` 语义的 struct 往返。
    """

    def __init__(self, base: int = DEFAULT_BASE, size: int = 0x1000, path: str = "/dev/mem"):
        if base % 0x1000:
            raise ValueError(f"base 必须页对齐，得到 0x{base:X}")
        self.base = int(base)
        self.size = int(size)
        self._fd = os.open(path, os.O_RDWR | os.O_SYNC)
        try:
            self._mm = mmap.mmap(self._fd, self.size, mmap.MAP_SHARED,
                                 mmap.PROT_READ | mmap.PROT_WRITE,
                                 offset=self.base)
        except Exception:
            os.close(self._fd)
            raise

    def read32(self, off: int) -> int:
        return struct.unpack_from("<I", self._mm, off & ~3)[0]

    def write32(self, off: int, val: int) -> None:
        struct.pack_into("<I", self._mm, off & ~3, int(val) & 0xFFFF_FFFF)

    def close(self) -> None:
        try:
            self._mm.close()
        finally:
            os.close(self._fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeRegIo:
    """主机单测用的假寄存器堆（32 位，按偏移存放）。

    除了读写，还模拟 PL 的两个**异步**行为（由测试显式驱动，保证可重复）：

    * ``vsync_edge()``：一个场边界；此刻把 ``FRAME_ID`` 采纳到
      ``FRAME_ID_LATCHED``（前提是有新值，模拟 RTL 的“只在 vsync 采纳”）；
    * ``stall(n_pixels)``：模拟行缓冲欠载，``UNDERFLOW_CNT`` 按行累加、
      ``STATUS[2]`` 置粘滞位（用于验证驱动的 ``underflow()``）。
    """

    def __init__(self, base: int = DEFAULT_BASE):
        self.base = int(base)
        self.mem = {}
        self.writes = []            # (offset, value) 轨迹，便于断言
        self._pending_fid = None

    # --- 寄存器访问 ---
    def read32(self, off: int) -> int:
        if off == REG["FRAME_ID_LATCHED"]:
            return self.mem.get(off, 0)
        return self.mem.get(off & ~3, 0)

    def write32(self, off: int, val: int) -> None:
        off &= ~3
        val &= 0xFFFF_FFFF
        self.writes.append((off, val))
        if off == REG["FRAME_ID"]:
            self.mem[off] = val          # 真寄存器（PL 侧只读它做 vsync 采纳）
            self._pending_fid = val
            return
        if off == REG["CTRL"] and (val & CTRL_SOFT_RESET):
            # soft_reset：清 STATUS 的粘滞位与欠载计数，保留配置
            st = self.mem.get(REG["STATUS"], 0) & ~STATUS_SCAN_UNDERFLOW
            self.mem[REG["STATUS"]] = st
            self.mem[REG["UNDERFLOW_CNT"]] = 0
            return
        if off == REG["STATUS"]:
            return                      # RO
        self.mem[off] = val

    # --- 模拟 PL 行为 ---
    def vsync_edge(self) -> None:
        """一个场边界：若有待采纳的 FRAME_ID，则采纳（模拟 RTL 的 vsync 采纳）。"""
        if self._pending_fid is not None:
            self.mem[REG["FRAME_ID_LATCHED"]] = self._pending_fid
            self._pending_fid = None
        self.mem[REG["STATUS"]] = self.mem.get(REG["STATUS"], 0) | STATUS_VSYNC_SEEN

    def mark_init_done(self) -> None:
        self.mem[REG["STATUS"]] = self.mem.get(REG["STATUS"], 0) | STATUS_INIT_DONE

    def stall(self, lines: int = 1) -> None:
        """模拟 lines 行被读空（STATUS[2] 置粘滞 + UNDERFLOW_CNT 累加）。"""
        self.mem[REG["STATUS"]] = self.mem.get(REG["STATUS"], 0) | STATUS_SCAN_UNDERFLOW
        self.mem[REG["UNDERFLOW_CNT"]] = min(
            0xFFFF_FFFF, self.mem.get(REG["UNDERFLOW_CNT"], 0) + int(lines))


# ---------------------------------------------------------------------------
# 驱动主体
# ---------------------------------------------------------------------------
class HdmiOut:
    """HDMI 输出（P2 真画面）驱动。

    参数
    ----
    base : int
        AXI-Lite 基址（默认 ``0x400A0000``，契约 §2）。
    reg_io : object | None
        注入的寄存器访问对象（``read32``/``write32``）；``None`` ⇒ ``MmapRegIo``。
    fb_addr : int | None
        帧缓冲物理地址（``pynq.allocate`` 得到的设备地址）；``configure()`` 也可给。
    mode : int
        显示模式号（0~3），``configure()`` 也可给。

    典型用法（板端）::

        from pynq import allocate
        buf = allocate(2 * align64(1280*3*720), dtype=np.uint8)   # 双缓冲一次分配
        out = HdmiOut(fb_addr=buf.device_address)
        out.set_mode(0); out.test_pattern(False)
        out.configure(buf.device_address, 1280*3, 1280, 720)
        fid = out.put_frame(frame)       # 写后台
        out.wait_frame(fid, timeout=1.0) # 等 PL 在 vsync 采纳
    """

    #: 单帧最大字节数（1920*1080*3 = 6.22 MB）→ 双缓冲 12.4 MB
    MAX_FRAME_BYTES = 1920 * 1080 * 3

    def __init__(self, base: int = DEFAULT_BASE, reg_io=None,
                 fb_addr: int | None = None, mode: int = MODE_720P60):
        self.base = int(base)
        self.io = reg_io if reg_io is not None else MmapRegIo(base)
        self._owns_io = reg_io is None

        self._mode = self._check_mode(mode)
        self._fb_addr: int | None = None
        self._stride: int = 0
        self._w: int = 0
        self._h: int = 0
        self._frame_id: int = 0          # 已提交（put）的帧号
        self._bufs: list[np.ndarray | None] = [None, None]

        if fb_addr is not None:
            self.configure(fb_addr, MODES[self._mode].W * 3,
                           MODES[self._mode].W, MODES[self._mode].H)

    # ------------------------------------------------------------------
    # 基本属性
    # ------------------------------------------------------------------
    @property
    def mode(self) -> int:
        return self._mode

    @property
    def geometry(self) -> tuple[int, int, int]:
        """(w, h, stride) —— stride 由 ``configure()`` 决定（含 64 B 对齐后的值）。"""
        return self._w, self._h, self._stride

    @property
    def fb_size(self) -> int:
        """单块缓冲字节数 = align64(stride*h)（与 RTL 完全一致）。"""
        return align64(self._stride * self._h) if self._stride and self._h else 0

    @property
    def frame_id(self) -> int:
        return self._frame_id

    def __repr__(self):
        return (f"HdmiOut(base=0x{self.base:08X}, mode={MODES[self._mode].name}, "
                f"fb=0x{(self._fb_addr or 0):08X}, {self._w}x{self._h}, "
                f"stride={self._stride}, frame_id={self._frame_id})")

    # ------------------------------------------------------------------
    # 寄存器小工具
    # ------------------------------------------------------------------
    def _wr(self, name: str, value: int) -> None:
        self.io.write32(REG[name], int(value) & 0xFFFF_FFFF)

    def _rd(self, name: str) -> int:
        return int(self.io.read32(REG[name])) & 0xFFFF_FFFF

    def _rd_ctrl(self) -> int:
        return self._rd("CTRL")

    def _wr_ctrl(self, value: int) -> None:
        self._wr("CTRL", value)

    # ------------------------------------------------------------------
    # 模式 / 彩条
    # ------------------------------------------------------------------
    @staticmethod
    def _check_mode(mode: int) -> int:
        if isinstance(mode, bool) or not isinstance(mode, (int, np.integer)):
            raise ValueError(f"mode 必须是 int，得到 {type(mode).__name__}")
        m = int(mode)
        if m not in MODES:
            raise ValueError(f"mode 必须是 {sorted(MODES)} 之一，得到 {m}")
        return m

    def set_mode(self, mode: int) -> None:
        """写 ``VIDEO_MODE``（0=720p60 1=1080p60 2=1080p30 3=640x480p60）。"""
        self._mode = self._check_mode(mode)
        self._wr("VIDEO_MODE", self._mode)

    def test_pattern(self, on: bool) -> None:
        """CTRL[2]：1 = PL 出彩条（P1），0 = 走帧缓冲扫描（P2）。"""
        ctrl = self._rd_ctrl()
        ctrl = (ctrl | CTRL_TEST_PATTERN) if on else (ctrl & ~CTRL_TEST_PATTERN)
        self._wr_ctrl(ctrl)

    def enable(self, on: bool = True) -> None:
        ctrl = self._rd_ctrl()
        ctrl = (ctrl | CTRL_ENABLE) if on else (ctrl & ~CTRL_ENABLE)
        self._wr_ctrl(ctrl)

    def soft_reset(self) -> None:
        """CTRL[1]：清粘滞状态位与 ``UNDERFLOW_CNT``，**不清配置**。"""
        self._wr_ctrl(self._rd_ctrl() | CTRL_SOFT_RESET)
        self._wr_ctrl(self._rd_ctrl() & ~CTRL_SOFT_RESET)

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def configure(self, phys_addr: int, stride: int, w: int, h: int) -> None:
        """配置帧缓冲几何。

        Raises
        ------
        ValueError
            - ``stride < w*3``（行放不下）
            - ``stride % 64 != 0``（契约要求 64 B 对齐；PL 按 8 拍 = 64 B 发突发，
              不对齐会有跨 4 KB 风险）
            - ``phys_addr`` 非 64 B 对齐 / 非 4 GB 内
            - ``w``/``h`` 超出行缓冲与计数器范围
        """
        phys_addr = int(phys_addr)
        stride = int(stride)
        w = int(w)
        h = int(h)

        if not (0 < w <= 2048):
            raise ValueError(f"w 必须在 1..2048（行缓冲 2048 像素），得到 {w}")
        if not (0 < h <= 4096):
            raise ValueError(f"h 必须在 1..4096，得到 {h}")
        if stride < w * 3:
            raise ValueError(f"stride={stride} < w*3={w * 3}：一行放不下 {w} 个像素")
        if stride % 64:
            raise ValueError(
                f"stride={stride} 未按 64 B 对齐（契约 §2 硬要求；PL 以 8 拍=64 B "
                f"发突发，不对齐会退化成 1 拍跨页并可能违反 AXI 4 KB 规则）")
        if phys_addr % 64:
            raise ValueError(f"phys_addr=0x{phys_addr:X} 未按 64 B 对齐")
        if phys_addr <= 0 or phys_addr > 0xFFFF_FFFF:
            raise ValueError(f"phys_addr=0x{phys_addr:X} 超出 32 位 AXI 地址空间")
        fs = align64(stride * h)
        if fs > self.MAX_FRAME_BYTES + 4096:
            raise ValueError(f"单帧 {fs} 字节过大（> {self.MAX_FRAME_BYTES}）")

        self._fb_addr = phys_addr
        self._stride = stride
        self._w = w
        self._h = h

        self._wr("FB_ADDR_LO", phys_addr & 0xFFFF_FFFF)
        self._wr("FB_ADDR_HI", (phys_addr >> 32) & 0xFFFF_FFFF)   # 本板恒 0
        self._wr("FB_STRIDE", stride)
        self._wr("FB_W", w)
        self._wr("FB_H", h)
        self._wr("PIXEL_FMT", PIXEL_FMT_RGB888)
        self._wr("VIDEO_MODE", self._mode)
        self.enable(True)          # CTRL[0]=1（扫描器使能）

    # ------------------------------------------------------------------
    # 帧提交
    # ------------------------------------------------------------------
    def _check_frame(self, frame: np.ndarray) -> np.ndarray:
        if not isinstance(frame, np.ndarray):
            raise ValueError(f"frame 必须是 numpy.ndarray，得到 {type(frame).__name__}")
        if frame.dtype != np.uint8:
            raise ValueError(f"frame dtype 必须是 uint8，得到 {frame.dtype}")
        if frame.shape != (self._h, self._w, 3):
            raise ValueError(f"frame 形状必须是 {(self._h, self._w, 3)}，得到 {frame.shape}")
        return frame

    def put_frame(self, frame: np.ndarray) -> int:
        """把一帧写进**后台缓冲**并把新帧号交给 PL（PL 在 vsync 边界采纳）。

        Returns
        -------
        int
            新帧号（从 1 开始单调递增）；``0`` 表示还没有帧。

        Notes
        -----
        - 缓冲区选择：``buf = (new_frame_id) & 1`` —— 与 RTL 的
          ``addr = FB_ADDR + (FRAME_ID[0]) * FB_SIZE`` 严格对应；
        - 本方法**只发帧号**，不等 PL 采纳；要等请接着调 ``wait_frame()``。
        - 调用者需保证 ``frame`` 是 ``compose()`` 的输出（含黑边、RGB888、行首无 padding）。
        """
        if self._fb_addr is None:
            raise RegError("还没有 configure()：不知道该写到哪里")
        self._check_frame(frame)

        new_id = (self._frame_id + 1) & 0xFFFF_FFFF
        buf = new_id & 1
        # 后台缓冲：先把整帧拷进本地镜像（本轮用内存镜像模拟 PS 写入 DDR；
        # 板端由 pynq.allocate 出来的连续数组直接充当这块内存）
        self._bufs[buf] = np.ascontiguousarray(frame).copy()

        self._wr("FRAME_ID", new_id)
        self._frame_id = new_id
        return new_id

    def wait_frame(self, frame_id: int, timeout: float = 1.0) -> bool:
        """轮询 ``FRAME_ID_LATCHED`` 直到 PL 采纳了 ``frame_id``。

        Returns
        -------
        bool
            True = 已在 vsync 边界采纳；False = 超时（此时画面仍是旧帧，**无撕裂**）。
        """
        frame_id = int(frame_id) & 0xFFFF_FFFF
        deadline = time.monotonic() + float(timeout)
        while True:
            if self._rd("FRAME_ID_LATCHED") == frame_id:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0)          # 让出 GIL；板端换成 sleep(1e-4) 也行

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------
    def status(self) -> dict:
        """读 ``STATUS``（并顺带读 ``UNDERFLOW_CNT``）。"""
        st = self._rd("STATUS")
        return {
            "init_done": bool(st & STATUS_INIT_DONE),
            "vsync": bool(st & STATUS_VSYNC_SEEN),
            "underflow": bool(st & STATUS_SCAN_UNDERFLOW),
            "underflow_cnt": self._rd("UNDERFLOW_CNT"),
            "raw": st,
        }

    def underflow(self) -> int:
        """``UNDERFLOW_CNT``（0x2C）—— **上板诊断第一眼看它**。

        0 ⇒ 带宽余量够；> 0 ⇒ 行缓冲被读空过，需要提 ``s_axi_aclk`` 或降模式
        （1080p60 在 100 MHz AXI 下必然不 0）。
        """
        return self._rd("UNDERFLOW_CNT")

    def frame_latched(self) -> int:
        return self._rd("FRAME_ID_LATCHED")

    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._owns_io and hasattr(self.io, "close"):
            self.io.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
