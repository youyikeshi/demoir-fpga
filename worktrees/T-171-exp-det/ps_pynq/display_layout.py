"""显示构图（纯 PS 阶段）：左原图 / 右处理图 -> 撑满宽度 + 上下填黑。

规范来源（唯一需求来源）
------------------------
``docs/camera/03_显示构图规范_纯PS.md``

一句话
------
画布 = ``512x256``（左 ``orig`` 256²、右 ``proc`` 256²），**先撑满显示宽度等比放大**，
剩余高度**上下对称填黑**（letterbox）。因为 ``512:256 = 2:1`` 比 16:9(1.78) 与 4:3(1.33)
都宽，所以高度必然有余，永远走"撑满宽度"分支。

公式（与世界坐标无关，纯整数实现）
----------------------------------
::

    Wd, Hd = 显示模式宽高
    s      = Wd / 512                     # 宽度适配的缩放比
    Hc     = 256 * s  =  (256*Wd)//512    # 整数实现，= Wd//2
    y0     = floor((Hd - Hc) / 2)         # 上黑边；下黑边 = Hd - Hc - y0

- 若 ``Hc > Hd``（只可能发生在 ``Wd > 2*Hd`` 的畸形模式上）⇒ 回退为按高度适配：
  ``s = Hd/256``、``Hc = Hd``、``Wc = 512*s``，左右填黑 ``x0 = floor((Wd-Wc)/2)``。
- 上黑边优先取整向下：``(H-Hc)`` 为奇数时上边少 1 像素。

缩放
----
**整数索引最近邻**（不做任何浮点插值）：预生成两张索引表

::

    idx_x[i] = (i * 512) // Wc      # 显示列 -> 画布列，值域 [0, 511]
    idx_y[j] = (j * 256) // Hc      # 显示行 -> 画布行，值域 [0, 255]

按 ``(Wd, Hd)`` 缓存，一次 ``canvas[np.ix_(idx_y, idx_x)]`` 完成放大。
最近邻的等价"绘制法"是：画布第 k 列占据显示列
``[ceil(k*Wc/512), ceil((k+1)*Wc/512))``——两者逐字节等价，自检脚本用后者做独立交叉验证。

契约（判据靠它，违反即 ``ValueError``，绝不静默 reshape）
---------------------------------------------------------
输入
    - ``orig`` / ``proc`` 必须是 ``numpy.ndarray``、``dtype == uint8``、
      形状 **严格** ``(256, 256, 3)``、RGB 通道顺序。
    - 形状/dtype/类型不符 ⇒ 直接 ``ValueError``；不做 reshape、不做 cast、不做裁剪。
输出
    - ``(H, W, 3)``、``uint8``、C 连续（``out.flags['C_CONTIGUOUS'] is True``）。
    - 整幅显示画面**已含黑边**，下游显示通路不需要理解 letterbox（规范 §2）。
布局
    - **左右不镜像**：``x < W//2`` 的列来自 ``orig``，``x >= W//2`` 的列来自 ``proc``。
      四种规定模式下 ``idx_x[W//2] == 256`` 恒成立，即右半第一列严格是 ``proc[:,0]``。
    - 黑边像素必须严格 ``(0, 0, 0)``；内容区不允许出现第三种来源的颜色。
    - 等比判据（**不许拉伸变形**）：横向与纵向缩放比都等于 ``s``（同一 ``s``），
      实测方式见 ``tools/display_selftest.py``：``256`` 个源像素横跨正好 ``256*s`` 个显示像素，
      ``32`` 个源像素（棋盘格宽）横跨正好 ``32*s`` 个显示像素且横向/纵向一致。
纯度
    - ``compose()`` 是纯函数：无隐藏状态，同输入两次调用**逐字节相同**；
      索引表缓存只是 ``(Wd,Hd)`` 的纯函数缓存，不影响输出。
    - 不 import ``pynq`` / ``cv2`` / ``PIL``，只依赖 ``numpy``，故主机可直接跑。

双缓冲（本阶段只定接口，不接显示）
----------------------------------
::

    fb = FrameBuffer(1280, 720)
    fb.put(frame)        # 写背缓冲 -> 原子换帧
    frame = fb.current() # 读当前前台（只读视图）

一帧 ``1280x720x3 = 2.76 MB``、``1920x1080x3 = 6.22 MB``（规范 §2），换帧频率 ~0.17 fps，
带宽与 CPU 都可忽略。**已知限制**：只有两块缓冲，producer 若在 consumer 读完前再次
``put()``，consumer 可能读到正在被写的那块——真实显示通路建议改三缓冲；本阶段不接显示，
故按规范只做两块 + 原子换帧。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

__all__ = [
    "SIDE",
    "CANVAS_W",
    "CANVAS_H",
    "CHANNELS",
    "ModeSpec",
    "SPEC_MODES",
    "LayoutPlan",
    "plan",
    "compose",
    "FrameBuffer",
    "spec_for",
]

#: 单张输入图的边长（原图与处理图都是 256²）
SIDE = 256
#: 中间画布宽 = 左右两张拼起来
CANVAS_W = 2 * SIDE  # 512
#: 中间画布高 = 单张图高
CANVAS_H = SIDE  # 256
#: RGB888
CHANNELS = 3


# ---------------------------------------------------------------------------
# 规范 §1.1 的四种显示模式（实测取整表）——作为判据锚点，自检脚本据此断言
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModeSpec:
    """一种显示模式的规范期望值（``docs/camera/03_显示构图规范_纯PS.md`` §1.1）。"""

    W: int
    H: int
    scale: float  # s = W/512
    content_h: int  # Hc = 256*s
    top_bar: int  # y0
    bottom_bar: int  # H - Hc - y0

    @property
    def content_w(self) -> int:
        return self.W  # 宽度适配分支下内容宽度恒等于显示宽度


SPEC_MODES: tuple[ModeSpec, ...] = (
    ModeSpec(1280, 720, 2.5, 640, 40, 40),
    ModeSpec(1920, 1080, 3.75, 960, 60, 60),
    ModeSpec(1024, 768, 2.0, 512, 128, 128),
    ModeSpec(2560, 1440, 5.0, 1280, 80, 80),
)

_MODES_BY_WH = {(m.W, m.H): m for m in SPEC_MODES}


def spec_for(W: int, H: int) -> ModeSpec | None:
    """返回该显示尺寸的规范期望值；不在规范表里则返回 ``None``。"""
    return _MODES_BY_WH.get((int(W), int(H)))


# ---------------------------------------------------------------------------
# 布局计划（纯函数，按 (W,H) 缓存索引表）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class LayoutPlan:
    """一次 ``(W, H)`` 的完整布局计划：缩放比、内容框、索引表。"""

    W: int
    H: int
    scale_num: int  # s = scale_num / scale_den（精确有理数，避免浮点误差）
    scale_den: int
    content_w: int  # Wc：内容区宽（显示像素）
    content_h: int  # Hc：内容区高（显示像素）
    x0: int  # 左黑边宽
    y0: int  # 上黑边高
    idx_x: np.ndarray  # (Wc,) intp，显示列 -> 画布列 ∈ [0, 511]
    idx_y: np.ndarray  # (Hc,) intp，显示行 -> 画布行 ∈ [0, 255]

    @property
    def scale(self) -> float:
        """缩放比 s（浮点，仅用于打印/断言）。"""
        return self.scale_num / self.scale_den

    @property
    def left_bar(self) -> int:
        return self.x0

    @property
    def right_bar(self) -> int:
        return self.W - self.content_w - self.x0

    @property
    def top_bar(self) -> int:
        return self.y0

    @property
    def bottom_bar(self) -> int:
        return self.H - self.content_h - self.y0

    @property
    def width_fitted(self) -> bool:
        """True = 撑满宽度（规范主分支）；False = 回退按高度适配。"""
        return self.x0 == 0 and self.content_w == self.W

    def describe(self) -> str:
        return (
            f"{self.W}x{self.H} s={self.scale:g} "
            f"content={self.content_w}x{self.content_h} "
            f"bars top/bottom={self.top_bar}/{self.bottom_bar} "
            f"left/right={self.left_bar}/{self.right_bar}"
        )


def _check_display_dim(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name} 必须是 int，得到 {type(value).__name__}")
    v = int(value)
    if v <= 0:
        raise ValueError(f"{name} 必须为正，得到 {v}")
    return v


@lru_cache(maxsize=32)
def _build_plan(W: int, H: int) -> LayoutPlan:
    """构造并缓存布局计划（纯函数：同 (W,H) 恒等输出）。"""
    # --- 主分支：撑满宽度 ---------------------------------------------------
    num, den = W, CANVAS_W  # s = W/512
    content_h = (CANVAS_H * num) // den  # Hc = 256*W/512 = W//2
    content_w = W
    x0 = 0

    # --- 回退分支：Hc > Hd 时改按高度适配（规范 §1）--------------------------
    if content_h > H:
        num, den = H, CANVAS_H  # s = H/256
        content_h = H
        content_w = (CANVAS_W * num) // den  # = 2*H < W
        x0 = (W - content_w) // 2  # 左右对称填黑

    if content_h < 1 or content_w < 1:
        raise ValueError(
            f"显示尺寸 {W}x{H} 过小，无法容纳 512x256 内容（Hc={content_h}, Wc={content_w}）"
        )

    y0 = (H - content_h) // 2

    # --- 整数索引表（预生成 + 缓存，一次 fancy-index 完成放大）--------------
    idx_x = ((np.arange(content_w, dtype=np.int64) * CANVAS_W) // content_w).astype(np.intp)
    idx_y = ((np.arange(content_h, dtype=np.int64) * CANVAS_H) // content_h).astype(np.intp)

    # 边界自证：索引必须落在画布内（最近邻不做边界复制，靠公式保证）
    if idx_x.max(initial=0) >= CANVAS_W or idx_y.max(initial=0) >= CANVAS_H:
        raise AssertionError("索引表越界，说明缩放公式实现有误")

    # 冻结：这两张表是缓存的共享对象，下游误改会污染所有后续调用
    idx_x.setflags(write=False)
    idx_y.setflags(write=False)

    return LayoutPlan(
        W=W,
        H=H,
        scale_num=num,
        scale_den=den,
        content_w=content_w,
        content_h=content_h,
        x0=x0,
        y0=y0,
        idx_x=idx_x,
        idx_y=idx_y,
    )


def plan(W: int = 1280, H: int = 720) -> LayoutPlan:
    """返回 ``(W,H)`` 的布局计划（缩放比、内容框、黑边、索引表）。纯函数、带缓存。"""
    return _build_plan(_check_display_dim(W, "W"), _check_display_dim(H, "H"))


# ---------------------------------------------------------------------------
# 输入校验：不合规直接 ValueError，绝不静默 reshape / cast
# ---------------------------------------------------------------------------
def _check_image(a: object, name: str) -> np.ndarray:
    if not isinstance(a, np.ndarray):
        raise ValueError(f"{name} 必须是 numpy.ndarray，得到 {type(a).__name__}")
    expected = (SIDE, SIDE, CHANNELS)
    if a.shape != expected:
        raise ValueError(
            f"{name} 形状必须是 {expected}（uint8 RGB），得到 {a.shape}；"
            f"本函数不静默 reshape"
        )
    if a.dtype != np.uint8:
        raise ValueError(f"{name} dtype 必须是 uint8，得到 {a.dtype}；本函数不静默 cast")
    return a


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------
def compose(orig: np.ndarray, proc: np.ndarray, W: int = 1280, H: int = 720) -> np.ndarray:
    """把 ``orig``（左）/ ``proc``（右）拼成整幅显示画面（含上下黑边）。

    Parameters
    ----------
    orig : np.ndarray
        ``(256,256,3)`` ``uint8`` RGB，原图，落在显示画面的**左半**。
    proc : np.ndarray
        ``(256,256,3)`` ``uint8`` RGB，去摩尔纹后的图，落在显示画面的**右半**。
    W, H : int
        显示模式宽高，默认 ``1280x720``；规范表内还有 ``1920x1080``、``1024x768``、
        ``2560x1440``。

    Returns
    -------
    np.ndarray
        ``(H, W, 3)`` ``uint8``、C 连续的 RGB888 整幅画面。

    Notes
    -----
    - 缩放：整数索引最近邻，无浮点插值；索引表按 ``(W,H)`` 缓存。
    - 布局：撑满宽度优先，``Hc = 256*W//512``；``y0 = (H-Hc)//2``；
      ``Hc > H`` 时回退按高度适配并在左右填黑。
    - 左右**不镜像**：``x < W//2`` 来自 ``orig``，其余来自 ``proc``。
    - 黑边恒为 ``(0,0,0)``。
    - 纯函数：同输入两次调用逐字节相同。

    Raises
    ------
    ValueError
        ``orig``/``proc`` 的类型、``dtype`` 或形状不符；``W``/``H`` 非正整数或过小。
    """
    orig = _check_image(orig, "orig")
    proc = _check_image(proc, "proc")
    p = plan(W, H)

    # 1) 拼中间画布 512x256：左 orig，右 proc（不镜像、不转置）
    canvas = np.empty((CANVAS_H, CANVAS_W, CHANNELS), dtype=np.uint8)
    canvas[:, :SIDE, :] = orig
    canvas[:, SIDE:, :] = proc

    # 2) 一次 fancy-index 最近邻放大
    content = canvas[np.ix_(p.idx_y, p.idx_x)]

    # 3) 贴进整幅黑底画面（黑边 already baked in）
    out = np.zeros((p.H, p.W, CHANNELS), dtype=np.uint8)
    out[p.y0 : p.y0 + p.content_h, p.x0 : p.x0 + p.content_w, :] = content
    return np.ascontiguousarray(out)


# ---------------------------------------------------------------------------
# 双缓冲（本阶段只写接口，不接显示）
# ---------------------------------------------------------------------------
class FrameBuffer:
    """两块整幅缓冲 + 帧号 + 原子换帧。

    用法::

        fb = FrameBuffer(1280, 720)
        fb.put(compose(orig, proc))   # 写背缓冲，写完再换帧
        view = fb.current()           # 前台只读视图，frame_id 单调递增

    语义
    ----
    - ``put(frame)`` 先整块拷进**背缓冲**，拷完才把 ``_front`` 指过去（单次属性赋值，
      CPython 下原子）⇒ 前台永远不会是半张图。
    - ``frame_id`` 从 ``0`` 开始（``0`` = 还没有帧），每次成功 ``put`` 加 1。
    - ``current()`` 返回前台缓冲的**只读视图**（防止下游误改正在被扫描的一帧）；
      尚无帧时返回全零缓冲。
    - 缓冲内容 RGB888、行首无 padding、与 ``compose()`` 输出同形（黑边已烘进去）。
    - **已知限制**：只有两块缓冲，producer 若在 consumer 读完前再次 ``put()``，
      consumer 可能读到正在被写的那块；真实显示通路建议升到三缓冲。本阶段不接显示。
    """

    __slots__ = ("_shape", "_bufs", "_front", "_frame_id", "_puts")

    def __init__(self, W: int = 1280, H: int = 720, channels: int = CHANNELS):
        channels = _check_display_dim(channels, "channels")
        p = plan(W, H)  # 复用同一套校验与缓存
        self._shape = (p.H, p.W, channels)
        self._bufs = [
            np.zeros(self._shape, dtype=np.uint8),
            np.zeros(self._shape, dtype=np.uint8),
        ]
        self._front = 0
        self._frame_id = 0
        self._puts = 0

    # -- 只读属性 ---------------------------------------------------------
    @property
    def shape(self) -> tuple[int, int, int]:
        return self._shape

    @property
    def W(self) -> int:
        return self._shape[1]

    @property
    def H(self) -> int:
        return self._shape[0]

    @property
    def frame_id(self) -> int:
        """已提交的帧号，单调递增；``0`` 表示还没有帧。"""
        return self._frame_id

    @property
    def has_frame(self) -> bool:
        return self._frame_id > 0

    @property
    def nbytes(self) -> int:
        """单块缓冲字节数（规范 §2：1280x720 为 2.76 MB）。"""
        return int(np.prod(self._shape))

    # -- 换帧 -------------------------------------------------------------
    def put(self, frame: np.ndarray) -> int:
        """把一帧整块写入背缓冲，然后原子换帧；返回新的 ``frame_id``。"""
        if not isinstance(frame, np.ndarray):
            raise ValueError(f"frame 必须是 numpy.ndarray，得到 {type(frame).__name__}")
        if frame.shape != self._shape:
            raise ValueError(f"frame 形状必须是 {self._shape}，得到 {frame.shape}")
        if frame.dtype != np.uint8:
            raise ValueError(f"frame dtype 必须是 uint8，得到 {frame.dtype}")

        back = 1 - self._front
        # 整块拷贝（自动处理非连续源），拷贝完成前不动 _front
        np.copyto(self._bufs[back], frame, casting="no")
        self._front = back  # 单次赋值 = 原子换帧
        self._frame_id += 1
        self._puts += 1
        return self._frame_id

    def current(self) -> np.ndarray:
        """前台缓冲的只读视图（形状 ``(H,W,3)``、uint8）。"""
        view = self._bufs[self._front].view()
        view.flags.writeable = False
        return view

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return (
            f"FrameBuffer({self.W}x{self.H}, shape={self._shape}, "
            f"frame_id={self._frame_id}, front={self._front}, {self.nbytes / 1e6:.2f} MB/buf)"
        )
