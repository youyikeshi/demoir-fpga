#!/usr/bin/env python3
"""C8 交错布局的**判定/分配**辅助（PS 侧唯一真相源）。

规范：`docs/设计/设计_PS侧C8整合规范.md`（布局公式见
`docs/设计/设计_激活tiled布局_C8交错.md`）。

全局开关
--------
环境变量 ``C8_LAYOUT``（默认 **0 = 关**）：

* 关（0，默认）：所有 int8 张量按 CHW 分配、视图形状 ``(C,H,W)``；
  引擎 ``relu_en`` 只写 bit0；层间算子全走 ``ops_numpy``。
  **行为与整合前逐位一致**（这是验收项之一）。
* 开（1）：``int8`` 且 ``C % 8 == 0`` 的张量按 C8 分配，
  视图形状 ``(C/8, H, W, 8)``（C-contiguous 即规范里的地址公式），
  字节数 ``ceil(C/8)*8*H*W``；引擎 ``relu_en`` 的 bit2/bit3 分别表示
  输入/输出缓冲区是 C8；层间算子走 ``ps/c8/ops_c8.py``。

地址（元素下标）::

    addr(c, y, x) = (c // 8) * H*W*8 + y*W*8 + x*8 + (c % 8)

唯一判定规则（规范 §1）：**int8 且 C % 8 == 0 ⇒ C8，否则 CHW**。
实测 manifest_640：508 个 buffer 走 C8，8 个例外（3ch/12ch）保留 CHW
（另有 6 个 fp32 attn，本来就不适用 int8 布局）。

注意：``C % 8 == 0`` 时 ``ceil(C/8)*8*H*W == C*H*W``，所以 C8 **不会多占内存**，
`plan_pool` 的字节偏移公式一行都不用改（见规范 §3）。``nbytes()`` 仍按
``ceil`` 的通用式写，只为"万一被强行置成 C8 的非对齐张量"留出补零区；
补零通道必须为 0（引擎会读它们），分配处一律 ``np.zeros`` / 整池清零。
"""
import os

C8 = 8

# 模块目录与其上一级（仓库布局 ps/pynq -> ps，C8 支撑模块在 ps/c8/）
_HERE = os.path.dirname(os.path.abspath(__file__))
_C8_DIRS = (os.path.join(_HERE, os.pardir, "c8"), _HERE)


# ---------------------------------------------------------------- 开关

def env_flag(name, default=False):
    """环境变量开关：未设置 / 空 / 0 / false / off / no 都算关。"""
    v = os.environ.get(name, "")
    if str(v).strip().lower() in ("", "0", "false", "off", "no"):
        return bool(default) if str(v).strip() == "" else False
    return True


def enabled():
    """全局开关 C8_LAYOUT 的当前值（每次调用都读环境变量，方便同进程 A/B）。"""
    return env_flag("C8_LAYOUT")


def name(is_c8):
    """布局标签：True -> 'c8'，False -> 'chw'（ops_c8 的入参约定）。"""
    return "c8" if is_c8 else "chw"


# ---------------------------------------------------------------- 判定表

def table(manifest, on=None):
    """按 manifest 建 ``{张量名: bool}`` 布局表（True = C8）。

    ``on=None`` 时读环境变量 C8_LAYOUT（默认关 ⇒ 全 False，与整合前一致）。
    编译期算一次、运行期查表，是这个设计"零分支成本"的来源。
    """
    if on is None:
        on = enabled()
    out = {}
    for t, meta in manifest["buffers"].items():
        out[t] = bool(on) and meta["dtype"] == "int8" and int(meta["c"]) % C8 == 0
    return out


def stats(layout, manifest=None):
    """布局表统计：(c8 个数, chw 个数, 例外列表[(名字,C,H,W)], fp32 个数)。"""
    n8 = nchw = nf = 0
    exc = []
    bufs = manifest["buffers"] if manifest is not None else {}
    for t, is_c8 in layout.items():
        meta = bufs.get(t)
        if meta is not None and meta["dtype"] != "int8":
            nf += 1
            continue
        if is_c8:
            n8 += 1
        else:
            nchw += 1
            if meta is not None:
                exc.append((t, meta["c"], meta["h"], meta["w"]))
    exc.sort(key=lambda r: r[0])
    return n8, nchw, exc, nf


# ---------------------------------------------------------------- 形状/字节

def nbytes(c, h, w, is_c8):
    """缓冲区字节数（规范 §3）。C8 时通道补零到 8 的倍数。"""
    if is_c8:
        return ((int(c) + C8 - 1) // C8) * C8 * int(h) * int(w)
    return int(c) * int(h) * int(w)


def dtype_itemsize(dtype):
    """manifest 里的 dtype 字符串 -> 字节数（只支持本网络用到的 int8/fp32）。"""
    return 1 if str(dtype) == "int8" else 4


def bytes_of(meta, is_c8):
    """一个 buffer 的**字节数**（C8 时通道补零到 8 的倍数）。

    注意 `nbytes()` 返回的是**元素个数**（对 int8 恰好等于字节数）；fp32 的
    attn 张量（3C,1,1）必须再乘 itemsize，否则池里只分到 1/4 的空间
    （2026-10-23 踩过一次：PYNQArena 的 reshape 报 "size 48 into shape (144,1,1)"）。
    """
    return nbytes(meta["c"], meta["h"], meta["w"], is_c8) * dtype_itemsize(meta["dtype"])


def pad_channels(c, is_c8):
    """补零后的通道数（CHW 时就是 C）。"""
    if not is_c8:
        return int(c)
    return ((int(c) + C8 - 1) // C8) * C8


def view_shape(c, h, w, is_c8):
    """numpy 视图形状。C8 -> (C/8,H,W,8)；CHW -> (C,H,W)。"""
    if not is_c8:
        return (int(c), int(h), int(w))
    if int(c) % C8:
        raise ValueError("C8 布局要求 C 是 %d 的倍数（C=%d）；非对齐张量按规范 §1 "
                         "应保留 CHW" % (C8, int(c)))
    return (int(c) // C8, int(h), int(w), C8)


def channels(shape, is_c8):
    """numpy 形状 -> 逻辑通道数。坑：C8 的 shape[0] 是**通道块数**。"""
    return int(shape[0]) * C8 if is_c8 else int(shape[0])


def hw(shape, is_c8):
    """numpy 形状 -> (H, W)。

    CHW 与 C8 的前两维/后两维都含 H,W（C8 是 (Cb,H,W,8)），所以直接切；
    留着这个函数是为了让调用点**显式**表达"这里取的是 H/W，不是通道块"。
    """
    del is_c8
    return (int(shape[1]), int(shape[2]))


# ---------------------------------------------------------------- 通道切片

def ch_slice(arr, is_c8, off_c, c):
    """在通道维取 ``[off_c, off_c+c)``，返回**视图**（零拷贝）。

    C8 下要求 off_c 与 c 都对齐 8（否则会跨通道块断裂）；不满足时抛错而不是
    静默算错 —— cat 别名组在 C8 下的每一段都对齐 8（454/454 段，见报告）。
    """
    if not is_c8:
        return arr[int(off_c):int(off_c) + int(c)]
    if int(off_c) % C8 or int(c) % C8:
        raise ValueError("C8 的通道切片必须 8 对齐（off=%r c=%r）" % (off_c, c))
    return arr[int(off_c) // C8:(int(off_c) + int(c)) // C8]


# ---------------------------------------------------------------- 布局转换

def _c8lib():
    """惰性导入 ps/c8/c8lib.py（本机/离线路径；C8_LAYOUT=0 时从不导入）。"""
    import sys
    try:
        import c8lib
        return c8lib
    except ImportError:
        pass
    for d in _C8_DIRS:
        d = os.path.abspath(d)
        if os.path.exists(os.path.join(d, "c8lib.py")):
            if d not in sys.path:
                sys.path.insert(0, d)
            import c8lib
            return c8lib
    raise ImportError("找不到 c8lib.py（C8 转换需要它；应在 ps/c8/ 或板上扁平目录）")


_ops_c8 = None


def ops_c8():
    """惰性导入 ps/c8/ops_c8.py（层间算子适配层，C8_LAYOUT=0 时从不导入）。"""
    global _ops_c8
    if _ops_c8 is not None:
        return _ops_c8
    import sys
    try:
        import ops_c8 as m
    except ImportError:
        m = None
        for d in _C8_DIRS:
            d = os.path.abspath(d)
            if os.path.exists(os.path.join(d, "ops_c8.py")):
                if d not in sys.path:
                    sys.path.insert(0, d)
                import ops_c8 as m
                break
        if m is None:
            raise ImportError("找不到 ops_c8.py（C8 层间算子需要它；应在 ps/c8/ "
                              "或板上扁平目录）")
    _ops_c8 = m
    return m


def as_layout(arr, src_is_c8, dst_is_c8):
    """把张量从一种布局搬到另一种（同布局零拷贝返回原数组）。"""
    if bool(src_is_c8) == bool(dst_is_c8):
        return arr
    lib = _c8lib()
    return lib.from_c8(arr) if src_is_c8 else lib.to_c8(arr)


def to_chw(arr, is_c8):
    """C8 -> CHW（CHW 时零拷贝）。"""
    return _c8lib().from_c8(arr) if is_c8 else arr


def from_chw(arr, is_c8):
    """CHW -> C8（CHW 目标时零拷贝）。"""
    return _c8lib().to_c8(arr) if is_c8 else arr


def chw_chunk(arr, is_c8, c0, c1):
    """取逻辑通道 ``[c0,c1)`` 并转成 CHW（用于按通道分块、与旧 CHW 代码逐位一致）。

    C8 下 c0/c1 必须是 8 的倍数；转换只搬这一段，不会复制整块张量。
    """
    if not is_c8:
        return arr[c0:c1]
    if c0 % C8 or c1 % C8:
        raise ValueError("C8 分块的通道边界必须 8 对齐（%r..%r）" % (c0, c1))
    return _c8lib().from_c8(arr[c0 // C8:c1 // C8])


# ---------------------------------------------------------------- arena 查询

def arena_is_c8(arena, tname):
    """查 arena 里某个张量的布局（没有布局表的 arena 一律按 CHW）。"""
    lay = getattr(arena, "layout", None)
    return bool(lay.get(tname, False)) if lay else False
