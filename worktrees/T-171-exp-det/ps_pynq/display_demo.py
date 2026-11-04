"""PS 侧显示流水线：原图/处理图 → 整幅画面 → 写后台块 → ``FRAME_ID`` 换帧（默认 720p30）。

需求来源（唯一）
----------------
``docs/camera/05_HDMI输出_PS_PL接口契约.md``（§2 寄存器图 / §7.2 双缓冲 / §8.2+§8.9 心跳 /
§8.10+§8.11 720p30 / §8.13 裁定）与 ``docs/camera/03_显示构图规范_纯PS.md``。

**几何与寄存器只有一个来源**（不再保留任何本地副本）：
``hdmi_out.MODES``（模式号/宽高/像素时钟/时序）与 ``hdmi_out.REG``（含 ``PIX_HB=0x30``）；
``display_layout.compose()`` 与 ``hdmi_out.HdmiOut`` 只调用、不改。

一帧
----
``compose(1280,720)`` ⇒ 1280×720×3 = 2,764,800 B（上下各 40 px 黑边已烘进缓冲）
⇒ ``np.copyto`` 块[``FRAME_ID & 1``] ⇒ ``write32(FRAME_ID, +1)`` ⇒ PL 在 vsync 采纳、扫另一块。

块地址（与 RTL 严格一致）：``buf = FRAME_ID & 1``、``addr = FB_ADDR_LO + buf*FB_SIZE``、
``FB_SIZE = align64(stride*H)`` ⇒ 两块**必须一次连续分配**（``allocator(2*FB_SIZE)``）。
``stride = W*3 = 3840`` 是 64 的整数倍，``FB_SIZE = align64(3840*720) = 2,764,800``。

模式（``hdmi_out.MODES``，契约 §8.11）：0=720p60 / 1=1080p60 / 2=1080p30 /
**3=720p30（默认，37.109375 MHz，H/V=1650/1280, 750/720）**。

**首帧号是 1**：``enable=1`` 后 PL 立刻扫块 0，首帧写块 0 会边写边被扫 ⇒ 撕裂；故首帧写块 1，
块序 ``1,0,1,0``（交替、不重复、帧号严格递增）。**并在 ``setup()`` 里把两块都预填黑**，
否则 ``enable=1`` 到首帧之间屏幕上是未初始化内存的垃圾。

心跳（``REG["PIX_HB"]=0x30``，§8.2/§8.9）：``[0]`` 由像素域 ``hb_cnt[16]`` 翻转，
半周期 = $2^{16}$ 个像素时钟 ⇒ ``f_pix = 沿数 × 2^16 / 窗口秒数``。
**``pix_alive(100 ms)`` 只判“活着”（≥5 沿），不判频率**；频率精度 ≈ ``1/N``（N = 窗口内沿数）：

=========  ==========  ==========
窗口        720p30 沿数  精度
=========  ==========  ==========
100 ms     ≈57         **±1.8%**
500 ms     ≈283        ±0.35%
2 s        ≈1132       **±0.09%**
=========  ==========  ==========

⇒ 要 ``±0.1%`` 必须 ``≥2 s`` 窗口，且**报数时必须注明窗口**。

可注入（**不 import pynq 也能跑**）：``reg_io`` = ``read32``/``write32``（默认 ``MmapRegIo``）；
``allocator`` = ``f(nbytes)->块``，块需有 ``device_address``、可被 numpy 访问、有 ``flush()``
就调用（cacheable 不回写 ⇒ PL 读到旧内容）。默认 ``pynq.allocate``，**只有它 import pynq**。
"""

from __future__ import annotations

import os
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from display_layout import compose  # noqa: E402  已板端验证，只调用
import hdmi_out as ho  # noqa: E402  只调用

__all__ = ["HB_MASK", "HB_HALF_DIV", "DEFAULT_MODE", "stride_of", "block_bytes_of",
           "DisplayPipeline", "default_allocate"]

HB_MASK = 1 << 0       #: ``REG["PIX_HB"]`` 的 ``[0]`` 翻转位
HB_HALF_DIV = 1 << 16  #: 心跳半周期 = 2^16 个像素时钟
DEFAULT_MODE = 3       #: 720p30（契约 §8.10）


def stride_of(m) -> int:
    """``W*3``（行首无 padding；3840/5760 都是 64 的整数倍）。"""
    return m.W * 3


def block_bytes_of(m) -> int:
    """``align64(stride*H)`` —— 与 RTL 的块大小公式完全一致。"""
    return ho.align64(stride_of(m) * m.H)


def _check_mode(mode: object) -> int:
    if isinstance(mode, bool) or not isinstance(mode, (int, np.integer)):
        raise ValueError(f"mode 必须是 int，得到 {type(mode).__name__}")
    m = int(mode)
    if m not in ho.MODES:
        raise ValueError(f"mode 必须是 {sorted(ho.MODES)} 之一，得到 {m}")
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
    def mode_def(self):
        """当前模式的 ``hdmi_out.VideoMode``（几何唯一来源）。"""
        return ho.MODES[self._mode]

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
    def _ensure_io(self):
        """按需创建 ``MmapRegIo``（**允许在 setup() 之前发生**）。

        为什么必须有这个：契约 §8.2 的板端协议是"**心跳先行**"——上板第一件事
        只读 ``0x30``，确认像素时钟在跳，才允许碰别的寄存器（否则曾整板挂死）。
        而 ``setup()`` 内部会写 ``enable`` / ``VIDEO_MODE`` 等寄存器，
        所以"读心跳"绝不可能等到 ``setup()`` 之后。
        原先 ``_io`` 只在 ``setup()`` 里创建 ⇒ ``pix_alive()`` 提前调必崩
        （``AttributeError: 'NoneType' object has no attribute 'read32'``）。

        注入 ``reg_io`` 时（主机自检）此函数是纯 no-op，不碰 pynq。
        """
        if self._io is None:
            self._io = ho.MmapRegIo(self.BASE)
            self._owns_io = True
        return self._io

    def _wr(self, name: str, value: int) -> None:
        self._ensure_io().write32(ho.REG[name], int(value) & 0xFFFF_FFFF)

    def _rd(self, name: str) -> int:
        return int(self._ensure_io().read32(ho.REG[name])) & 0xFFFF_FFFF

    # ------------- 生命周期 -------------
    def setup(self) -> "DisplayPipeline":
        """分配双缓冲 → 切模式（契约协议）→ 写几何 → ``enable=1``。"""
        d = self.mode_def
        self._check_stride(stride_of(d))
        self._ensure_io()
        if self._out is None:
            self._out = ho.HdmiOut(self.BASE, reg_io=self._io, mode=self._mode)
        self._ensure_buffers()
        self._apply_mode()
        return self

    def _ensure_buffers(self) -> None:
        want = block_bytes_of(self.mode_def)
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
        self._prefill_blocks()              # enable=1 之前两块必须是黑的（§8.13）

    def _prefill_blocks(self) -> None:
        """两块都预填黑 ``(0,0,0)``：``enable=1`` 到首帧之间 PL 扫的就是这块内存（§8.13）。"""
        n = 2 * self._block_bytes
        if self._u8 is not None:
            self._u8[:n] = 0
        else:
            self._blocks[0:n] = np.zeros(n, dtype=np.uint8)   # 不透明块只能切片赋值
        flush = getattr(self._blocks, "flush", None)
        if flush is not None:
            flush()

    def _apply_mode(self) -> None:
        """契约 §8.11：``enable=0`` → 写 ``VIDEO_MODE`` → 等 ≥2 帧 → ``enable=1``。"""
        d = self.mode_def
        self._out.enable(False)
        self._out.set_mode(self._mode)       # 顺便同步 HdmiOut 内部状态
        time.sleep(2.05 / d.fps)             # ≥2 整帧，让 BUFGMUX/时序稳下来
        # configure() 写 FB_ADDR/STRIDE/W/H/PIXEL_FMT/VIDEO_MODE，收尾 enable(True)
        self._out.configure(self._fb_addr, stride_of(d), d.W, d.H)
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
        """构图 → :meth:`show_frame`（写后台块 → ``FRAME_ID += 1``）→ 返回新帧号。"""
        if self._out is None or self._blocks is None:
            raise ho.RegError("还没 setup()：先分配双缓冲并配置 PL")
        d = self.mode_def
        return self.show_frame(compose(orig, proc, d.W, d.H))  # 含上下黑边；只调用 compose

    def show_frame(self, frame: np.ndarray) -> int:
        """把**已经 compose 好的**整幅画面写后台块并换帧（语义与 :meth:`show` 相同）。

        为什么要拆出来：分段计时需要"构图"与"写块+换帧"各自计时；只给
        ``show(orig, proc)`` 的话，调用方为了计时就得把 ``compose()`` 跑两遍。
        本方法是**新增**，``show()`` 改为委派给它 —— 顺序（①整帧先落地 → ②再翻
        ``FRAME_ID``）、寄存器时序、返回值一字未动，且 ``tools/display_e2e_selftest.py``
        的 103 条判据覆盖这条路径。
        """
        if self._out is None or self._blocks is None:
            raise ho.RegError("还没 setup()：先分配双缓冲并配置 PL")
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
    def mmcm_locked(self) -> bool:
        """``0x30[1]``：MMCM 是否锁定（**只上报状态，绝不进复位**，契约 §8.5）。"""
        return bool(self._rd("PIX_HB") & 0x2)

    def pix_alive(self, window_ms: float = 100) -> tuple[bool, float]:
        """有界窗口内数 ``0x30[0]`` 的翻转沿 ⇒ ``(活?, 反推像素时钟 MHz)``。

        判活只要 100 ms（720p30 下仍有 ≈56 个沿，远超 ≥5）。**频率精度受沿量化限制**：
        误差 ≈ 1/窗口内沿数（720p30 沿周期 1.77 ms ⇒ 100 ms 只有 ±1.8%，±0.1% 需 ≈2 s）。
        """
        if not (window_ms > 0):
            raise ValueError(f"window_ms 必须 > 0，得到 {window_ms}")
        self._ensure_io()          # 循环外解析一次：setup() 之前也能读，且热循环不加开销
        t_end = time.monotonic() + window_ms / 1000.0
        last, edges = None, 0
        t_first = t_last = time.monotonic()
        while True:  # 只受墙钟约束（不限 poll 次数，否则窗口被悄悄截短）
            t = time.monotonic()
            v = int(self._io.read32(ho.REG["PIX_HB"])) & HB_MASK
            if last is None:
                t_first = t
            elif v != last:
                edges += 1
            last, t_last = v, t
            if t >= t_end:
                break
        # 只算「首末取样的跨度」：含循环起停开销会把 dt 拉长、把频率压低
        # （实测 148 MHz/2 s 档曾因此偏低 0.11%，正是“精度声称必须可信”要防的事）
        dt = max(t_last - t_first, 1e-9)
        mhz = edges * HB_HALF_DIV / dt / 1e6
        alive = edges >= 5
        self._last_pix, self._pix_window = (alive, mhz), dt
        return alive, mhz

    def stats(self) -> dict:
        d = self.mode_def
        st = {"mode": self._mode, "mode_name": d.name, "W": d.W, "H": d.H,
              "stride": stride_of(d), "frame_bytes": d.frame_bytes,
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
        return (f"DisplayPipeline(mode={self._mode}:{d.name} {d.W}x{d.H} stride={stride_of(d)} "
                f"fb=0x{self._fb_addr:08X} frame_id={self._frame_id})")
