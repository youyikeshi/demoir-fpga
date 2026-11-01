#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bin_engine.py —— `bin_conv_core`（二值卷积，XNOR+popcount）的 PS 侧驱动

设计原则与 `uni_engine.py` 一致，并复用 `board_safe.Watchdog`：

  1. **启动前校验每一个物理地址**：非 0、8 字节对齐（RTL 的 ERR_ALIGN 条件）、
     且必须**完全落在本进程登记过的缓冲区内**（这是 2026-10-30 板子挂死事故的
     根因：PL 往没分配的地址写 ⇒ SoC 硬挂）。
  2. **按公式算出引擎会读/写多少字节**，确认缓冲够大，输出尾部放 0xA5 金丝雀；
     跑完复核金丝雀（越界检测）。
  3. **start 之后限时等待；超时绝不发第二次 start**。若挂着看门狗，超时后
     **停止喂狗**让板子自己复位，而不是继续在"可能有野指针在写"的状态下跑。
  4. 轮询 done 的循环里**主动喂狗**（长卷积几十秒，不喂狗会被自己的看门狗打断）。
  5. `AP_RETURN[15:8] != 0` ⇒ 抛 `BinRunError`，**不静默继续**。

寄存器映射全部来自 RTL `bin_unit_axi_wrap.v` §1（不是猜的，见文件末尾
`REG_MAP_NOTES` 里的核对结论）。**没有任何地址是硬编码的**：
IP 基址从 `overlay.ip_dict[name]['phys_addr']` 取，只在它之上加 "+0x10000 配置页"
这个**契约常量**（A[16]=1）。

用法（板子上）：

    # ★ PYNQ 3.1 本镜像不自动注册 Device 子类
    try:
        import pynq.pl_server.embedded_device as _pynq_emb  # noqa: F401
    except Exception:
        pass
    from pynq import Overlay
    from bin_engine import BinConvEngine, BinArena

    ov = Overlay("design_1.bit")
    e  = BinConvEngine(ov, unit_id=0)
    print(e.summary())                      # 基址/页/容量自检
    blob = e.load_weights()                 # 整份 bin_weights.bin 搬进 DDR（一次，带缓存）
    y = e(x_q_int8_chnw, layer)             # 二值化 + 打包 + 搬运 + 跑 + 取回

    # 或者显式三段式（分级测试用的就是这条）：
    e.config(layer, H, W); e.set_bases(layer, blob); e.run(in_buf, out_buf, wdt=wdt)

本机自检（无板、无 pynq）：
    python3 bin_engine.py --selftest        # 用"寄存器级 RTL 模型"验证驱动流程
"""
from __future__ import annotations

import json
import os
import sys
import time
import weakref

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from bin_ref_numpy import (            # noqa: E402  （无 pynq 依赖）
    Blob, LANES, C8, WBUF_WORDS, COUT_LIM, C_MAX, HW_MAX,
    ALPHA_SHIFT, SLOPE_SHIFT, ERR_NAME, out_hw, ceil64, binarize_pack, act_bytes,
    act_u8, sat8, from_c8, to_c8,
)

# pynq 只在真正要分配内存时才需要 ⇒ 本模块在 PC 上也能 import（自检/语法检查）
try:                                    # pragma: no cover
    from pynq import allocate as _pynq_allocate
except Exception:                       # noqa: BLE001
    _pynq_allocate = None

try:                                    # pragma: no cover
    from board_safe import Watchdog
except Exception:                       # noqa: BLE001
    Watchdog = None


# ============================================================================
# 0. 寄存器映射（**以 bin_unit_axi_wrap.v §1 为准**，偏移相对配置页 +0x10000）
# ============================================================================
CFG = 0x10000                 # A[16]=1 ⇒ 配置页；A[16]=0 那一页保留（写忽略、读 0）

BIN_REG = dict(
    AP_CTRL=0x00, AP_RET=0x10,
    C=0x18, C_HI=0x1C, H=0x20, W=0x28, HO=0x30, WO=0x38,
    K=0x40, S=0x48, D=0x50, PAD=0x58, SHIFT=0x60,
    ALPHA_SHIFT=0x68, SLOPE_SHIFT=0x6C, COUT=0x70,
    B_IN=0x80, B_IN2=0x88, B_IN3=0x90, B_IN4=0x98,
    B_WGT=0xA0, B_W2=0xA8, B_W3=0xAC, B_BIAS=0xB0,
    B_ZETA=0xB8, B_GAMMA=0xBC, B_SLOPE=0xC0, B_OUT=0xD0, B_ALPHA=0xD8,
    DBG_RD=0xE0, DBG_WR=0xE8, DBG_FLAG=0xF0,
)

# ⚠⚠ 只有这 8 个基址有"高 32 位槽"（hi_bit_of() 里列的就是这 8 个）：
#     B_IN/B_IN2/B_IN3/B_IN4/B_WGT/B_BIAS/B_OUT/B_ALPHA
#   B_ZETA(0xB8) / B_GAMMA(0xBC) / B_SLOPE(0xC0) 是 **32 位寄存器**，按 4 字节步进
#   紧挨着（RTL §1.1，因为契约把三个挤在一行）。对它们写 "+4" 会**覆盖下一个基址**
#   （0xB8+4=0xBC=GAMMA，0xBC+4=0xC0=SLOPE）⇒ 写完之后 gamma/slope 变 0 ⇒
#   start 时被判 ERR_NULL。uni_engine.BinUnitEngine.set_base() 就是这样无差别写 +4 的，
#   对 zeta/gamma/slope 是**错的**，本驱动用 HIGH_WORD 表规避。
HIGH_WORD = {
    "IN": 0x84, "IN2": 0x8C, "IN3": 0x94, "IN4": 0x9C,
    "WGT": 0xA4, "BIAS": 0xB4, "OUT": 0xD4, "ALPHA": 0xDC,
}

# conv 单元必须非 0 且 8 字节对齐的 7 个基址（RTL §3 的 ERR_NULL / ERR_ALIGN 条件）
CONV_BASES = ("IN", "WGT", "ALPHA", "BIAS", "ZETA", "GAMMA", "SLOPE", "OUT")

ERR_DESC = {
    1: ("ERR_NULL", "某个必需基址寄存器为 0",
        "把 7 个基址都写全（IN/WGT/ALPHA/BIAS/ZETA/GAMMA/SLOPE）＋ OUT。"
        "最常见的成因：对 B_ZETA/B_GAMMA/B_SLOPE 写了 +4 的“高字 0”，把后一个基址覆盖掉；"
        "或 load_weights() 没跑（blob_pa 是 None）。"),
    2: ("ERR_SIZE", "维度/容量越界",
        "H/W=0 或 >4096、C=0 或 >4096、cout=0 或 >192、s=0、d=0、"
        "K=ceil(cin/64)*k² > 256（wbuf 只有 256 个 64-bit 字），"
        "或某个 64 位基址的高 32 位被写成了非 0。"),
    3: ("ERR_SHIFT", "移位参数不是契约固定值",
        "alpha_shift 必须 =9、slope_shift 必须 =8（RTL 硬校验，不能改），"
        "且 shift ≤ 63。容器里每层都给全了，别自己算。"),
    4: ("ERR_LAYOUT", "C 不是 8 的倍数",
        "CHK_C8_ALL=1 ⇒ 对 conv 的 cin 也要求 %8==0（契约 §7.3-②）。"
        "本网 cin ∈ {32,48,64,96,…,544} 全部满足；出现这个码说明配置写错了寄存器。"),
    5: ("ERR_ALIGN", "基址不是 8 字节对齐",
        "in/wgt/alpha/bias/zeta/gamma/slope 都会被 64-bit 读 ⇒ 必须 [2:0]==0。"
        "容器里所有 off_* 都是 8 的倍数；出错一般是 blob_pa 本身没对齐（allocate 会给页对齐）。"),
    6: ("ERR_UNIT", "IP 的 UNIT 参数不在 0..3",
        "比特流里这个 bin_unit 不是 conv_core，综合配置错了。"),
}


class BinRunError(RuntimeError):
    """引擎报错（AP_RETURN[15:8] != 0）或超时/越界。"""

    def __init__(self, code, msg=None):
        self.code = int(code)
        name, short, hint = ERR_DESC.get(
            self.code, ("ERR_%d" % self.code, "未定义错误码", "查 RTL 的 localparam ERR_*"))
        self.name = name
        self.hint = hint
        super().__init__(msg or "bin_unit 报错 %s(%d)：%s\n  → %s"
                         % (name, self.code, short, hint))


def decode_err(code):
    """错误码 → (名字, 简述, 排查建议)。0 = OK；非 0 即 RTL 拒绝启动/校验失败。"""
    code = int(code) & 0xFF
    if code == 0:
        return (0, "OK", "引擎正常完成", "")
    return (code,) + ERR_DESC.get(code, ("ERR_%d" % code, "未定义错误码",
                                        "查 bin_unit_axi_wrap.v 的 localparam ERR_*"))



# ============================================================================
# 1. 护栏：地址 / 尺寸 / 金丝雀
# ============================================================================
DDR_LO, DDR_HI = 0x0010_0000, 0x4000_0000    # 和 uni_engine 一致：给 CMA 的合理区间
CANARY = 0xA5
CANARY_BYTES = 4096


class BinGuard:
    """所有会让 PL 直接访存的路径都必须先过这里。"""

    @staticmethod
    def check_pa(pa, name, align=8):
        """align=8 用于 7 个"会被 64-bit 读"的基址（RTL 的 ERR_ALIGN）；
        B_OUT 是字节写，RTL 不验它对齐 ⇒ 这里用 align=1。"""
        if pa is None:
            raise ValueError("%s: 物理地址为 None（权重没搬？）——拒绝启动" % name)
        pa = int(pa)
        if pa == 0:
            raise ValueError("%s: 物理地址为 0 ⇒ RTL 会判 ERR_NULL，且可能写内核内存" % name)
        if align and (pa & (align - 1)):
            raise ValueError("%s: 物理地址 %#x 不是 %d 字节对齐 ⇒ ERR_ALIGN"
                             % (name, pa, align))
        if not (DDR_LO <= pa < DDR_HI):
            raise ValueError("%s: 物理地址 %#x 不在 DDR 区间 [%#x,%#x)"
                             % (name, pa, DDR_LO, DDR_HI))
        if pa > 0xFFFF_FFFF:
            raise ValueError("%s: 物理地址 %#x 超过 32 位（引擎只支持 32 位基址）"
                             % (name, pa))
        return pa

    @staticmethod
    def check_region(pa, nbytes, regions, name):
        """(pa, pa+nbytes) 必须完全落在**登记过的**某个缓冲区里。

        这一条比"落在 DDR 区间"严得多：它保证引擎读/写不会跑到本进程没分配的地方。
        """
        pa, nbytes = int(pa), int(nbytes)
        for lo, hi, tag in regions:
            if lo <= pa and pa + nbytes <= hi:
                return tag
        raise ValueError(
            "%s: [%#x, %#x) 不落在任何已登记缓冲里（%d 个区：%s）⇒ 拒绝启动"
            % (name, pa, pa + nbytes, len(regions),
               ", ".join("%s[%#x,%#x)" % (t, lo, hi) for lo, hi, t in regions[:6])
               + ("..." if len(regions) > 6 else "")))

    @staticmethod
    def fill_canary(buf):
        mv = np.frombuffer(buf, np.uint8)
        mv[-CANARY_BYTES:] = CANARY

    @staticmethod
    def check_canary(buf, n_used, tag):
        n = len(buf)
        start = n - CANARY_BYTES
        if n_used > start:
            raise ValueError("%s: 需要 %d B，但缓冲只有 %d B 可写（尾部 %d B 是金丝雀）"
                             % (tag, n_used, start, CANARY_BYTES))
        tail = np.frombuffer(buf, np.uint8)[start:]
        bad = int((tail != CANARY).sum())
        if bad:
            raise RuntimeError("%s: 金丝雀被写坏 %d/%d 字节 ⇒ 引擎越界写，立即停机排查"
                               % (tag, bad, len(tail)))
        return True


# ============================================================================
# 2. 一层二值卷积的几何（index.json 的字段 + 运行时 H/W）
# ============================================================================
class BinLayer:
    """index.json 的一条 layer 记录 + 运行时输入分辨率。

    index.json **不含 H/W**（同一个权重跑任意分辨率，全卷积网），所以 H/W 由调用点给。
    0x20/0x28 两个寄存器因此不能从 index.json 直接抄 —— 这是任务书里
    "寄存器值都齐了"唯一不成立的两个。
    """

    def __init__(self, entry, H, W):
        L = dict(entry)
        self.d = L
        self.name = L["name"]
        self.idx = int(L.get("idx", -1))
        self.cin, self.cout = int(L["cin"]), int(L["cout"])
        self.k, self.s, self.dil, self.pad = (int(L["k"]), int(L["s"]),
                                              int(L["d"]), int(L["pad"]))
        self.shift = int(L["shift"])
        self.alpha_shift = int(L.get("alpha_shift", ALPHA_SHIFT))
        self.slope_shift = int(L.get("slope_shift", SLOPE_SHIFT))
        self.H, self.W = int(H), int(W)
        self.cb = ceil64(self.cin)
        self.K = self.cb * self.k * self.k
        self.Hout, self.Wout = out_hw(self.H, self.W, self.s)
        self._validate()

    def _validate(self):
        L = self.d
        if self.cb != int(L.get("cb", self.cb)):
            raise ValueError("%s: index 的 cb=%s != ceil(cin/64)=%d"
                             % (self.name, L.get("cb"), self.cb))
        if self.K != int(L.get("K", self.K)):
            raise ValueError("%s: index 的 K=%s != cb*k²=%d" % (self.name, L.get("K"), self.K))
        if not (1 <= self.H <= HW_MAX and 1 <= self.W <= HW_MAX):
            raise ValueError("%s: H=%d W=%d 超范围（HW_MAX=%d）"
                             % (self.name, self.H, self.W, HW_MAX))
        if not (1 <= self.cin <= C_MAX):
            raise ValueError("%s: cin=%d 超范围（C_MAX=%d）" % (self.name, self.cin, C_MAX))
        if not (1 <= self.cout <= COUT_LIM):
            raise ValueError("%s: cout=%d 超范围（COUT_LIM=%d）"
                             % (self.name, self.cout, COUT_LIM))
        if self.K > WBUF_WORDS:
            raise ValueError("%s: K=%d > wbuf %d" % (self.name, self.K, WBUF_WORDS))
        if self.cin % C8:
            raise ValueError("%s: cin=%d 不是 8 的倍数 ⇒ ERR_LAYOUT"
                             % (self.name, self.cin))
        if self.k < 1 or self.s < 1 or self.dil < 1:
            raise ValueError("%s: k/s/d 非法（%d/%d/%d）"
                             % (self.name, self.k, self.s, self.dil))
        if self.alpha_shift != ALPHA_SHIFT or self.slope_shift != SLOPE_SHIFT:
            raise ValueError("%s: alpha_shift/slope_shift = %d/%d，RTL 硬校验 9/8"
                             % (self.name, self.alpha_shift, self.slope_shift))
        if not (0 <= self.shift <= 63):
            raise ValueError("%s: shift=%d 非法" % (self.name, self.shift))

    # ---- 尺寸 ----
    @property
    def act_bytes(self):
        """输入激活（二值打包）字节数：(cb,H,W) 个 64-bit 字。"""
        return self.cb * self.H * self.W * 8

    @property
    def out_bytes(self):
        """输出 int8 C8 字节数：ceil(cout/8)*Hout*Wout*8。"""
        return ((self.cout + C8 - 1) // C8) * self.Hout * self.Wout * C8

    @property
    def wgt_bytes(self):
        return self.cout * self.k * self.k * self.cb * 8

    def regs(self):
        """写进配置页的那组值（顺序 = RTL 的寄存器顺序）。"""
        return [("C", self.cin), ("C_HI", 0), ("H", self.H), ("W", self.W),
                ("HO", 0), ("WO", 0),
                ("K", self.k), ("S", self.s), ("D", self.dil), ("PAD", self.pad),
                ("SHIFT", self.shift), ("ALPHA_SHIFT", self.alpha_shift),
                ("SLOPE_SHIFT", self.slope_shift), ("COUT", self.cout)]

    def __repr__(self):
        return ("BinLayer(%s cin=%d cout=%d k=%d s=%d d=%d pad=%d %dx%d→%dx%d "
                "cb=%d K=%d shift=%d)" % (self.name, self.cin, self.cout, self.k, self.s,
                                          self.dil, self.pad, self.H, self.W,
                                          self.Hout, self.Wout, self.cb, self.K, self.shift))


def estimate_cycles(L, fclk_mhz=150.0):
    """按 bin_conv_core 的官方拍数公式估一拍数：每 oc = 10(PRM)+2K(WLD)+Ho*Wo*K(CMP)+10(DRN)。"""
    per_oc = 10 + 2 * L.K + L.Hout * L.Wout * L.K + 10
    cyc = L.cout * per_oc
    return cyc, cyc / (fclk_mhz * 1e6)


# ============================================================================
# 2.5 池的子区视图（带 physical_address，能直接喂给引擎/看门狗）
# ============================================================================
class RegionBuffer(np.ndarray):
    """`BinArena.region()` 的返回值：numpy 视图 + 自己的物理地址 + flush/invalidate。

    有了它，`run(in_buf, out_buf)` 和 `load_weights(buf=...)` 都能直接吃"池里切出来
    的一段"，不需要每个张量单独 allocate（板子挂死事故之后统一改成单池 + 子区）。
    """

    def __new__(cls, view, pa, owner=None):
        obj = np.asarray(view).view(cls)
        obj.physical_address = int(pa)
        obj._owner = owner
        return obj

    # ★ 2026-10-31（f32 池）：切片/reshape/view 之后**物理地址必须跟着偏移**。
    #   numpy 默认**不**把子类的实例属性带进视图，所以 `region[:C]`、`x.view(f32)`
    #   都会丢掉 physical_address ⇒ `f32_segments()` 拿不到 PA。
    #   这里按"新数组 data 指针 − 父数组 data 指针"补偏移；只有真的共享内存才继承
    #   （`copy()` 出来的是新内存，绝不能继承一个假的 PA）。
    def __array_finalize__(self, obj):
        if obj is None:
            return
        pa = getattr(obj, "physical_address", None)
        if pa is not None:
            try:
                if np.may_share_memory(self, obj):
                    d0 = int(self.__array_interface__["data"][0])
                    o0 = int(obj.__array_interface__["data"][0])
                    self.physical_address = int(pa) + (d0 - o0)
            except Exception:              # noqa: BLE001
                pass
        try:
            if np.may_share_memory(self, obj):
                self._f32_slot = getattr(obj, "_f32_slot", None)
        except Exception:                  # noqa: BLE001
            pass
        if getattr(self, "_owner", None) is None:
            self._owner = getattr(obj, "_owner", None)

    def flush(self):
        if self._owner is not None:
            try:
                self._owner.flush()
            except Exception:              # noqa: BLE001
                pass

    def invalidate(self):
        if self._owner is not None:
            try:
                self._owner.invalidate()
            except Exception:              # noqa: BLE001
                pass


def _u8(buf):
    """任何缓冲（numpy / 子类 / pynq buffer）→ 可写的 uint8 一维视图。"""
    return np.asarray(buf).view(np.uint8).reshape(-1)


# ============================================================================
# 3. 单一整块 DDR 池（等价于 board_test_staged.py 的做法）
# ============================================================================
class BinArena:
    """一块 `size` 字节的物理连续缓冲，切成若干 8 字节对齐的子区。

    板端事故的教训：**不要为每个张量单独 allocate**（多处 CMA 碎片/地址不可控），
    而是拿一整块、在里面切，然后把每个子区的 (pa, len) 登记给引擎当护栏白名单。
    """

    def __init__(self, size=1 << 20, name="pool"):
        if _pynq_allocate is None:
            raise RuntimeError("BinArena 需要 pynq（本机没有）；测试请用 FakeArena")
        size = int(size) & ~0xFFF
        self.buf = _pynq_allocate(shape=(size,), dtype=np.uint8)
        self.size = size
        self.name = name
        self.mv = np.frombuffer(self.buf, np.uint8)
        self.mv[:] = CANARY
        self.flush()
        self.pa = int(self.buf.physical_address)
        self._next = 0

    def flush(self):
        try:
            self.buf.flush()
        except Exception:               # noqa: BLE001
            pass

    def invalidate(self):
        try:
            self.buf.invalidate()
        except Exception:               # noqa: BLE001
            pass

    def reset(self):
        """把切分区指针收回 0（**只在没有在用的子区时**调用：同一块 arena 复用给下一次
        `BinNet.run`，A/B 时省掉反复 allocate 几十 MB CMA）。"""
        self._next = 0
        return self

    def region(self, n, align=64, name="", keep_canary=False):
        """切一段出来，返回 (numpy view, pa)。keep_canary=True 时额外留 4 KB 尾巴。"""
        n = int(n) + (CANARY_BYTES if keep_canary else 0)
        off = (self._next + align - 1) // align * align
        if off + n > self.size:
            raise MemoryError("arena 不够：需要 %d，剩 %d" % (n, self.size - off))
        self._next = off + n
        view = RegionBuffer(self.mv[off:off + n], self.pa + off, self.buf)
        return view, self.pa + off

    def regions(self):
        return [(self.pa, self.pa + self.size, self.name)]


class FakeArena:
    """本机自检用的假池：`np.ndarray` + 一个假物理地址（映射进 FakeUnit 的存储）。"""

    def __init__(self, size=1 << 20, base_pa=0x1000_0000, unit=None, name="fake"):
        self.size = (int(size) + 4095) & ~4095
        self.buf = MemBuf(self.size, int(base_pa))      # 带 physical_address 的数组
        self.buf[:] = CANARY
        self.pa = int(base_pa)
        self.mv = self.buf
        self.name = name
        self._next = 0
        if unit is not None:
            unit.map_mem(self.pa, self.buf)

    def flush(self):
        pass

    def invalidate(self):
        pass

    def region(self, n, align=64, name="", keep_canary=False):
        n = int(n) + (CANARY_BYTES if keep_canary else 0)
        off = (self._next + align - 1) // align * align
        if off + n > self.size:
            raise MemoryError("arena 不够：需要 %d，剩 %d" % (n, self.size - off))
        self._next = off + n
        return RegionBuffer(self.mv[off:off + n], self.pa + off, None), self.pa + off

    def regions(self):
        return [(self.pa, self.pa + self.size, self.name)]


# ============================================================================
# 3.5 float32 输出池（`BIN_F32POOL=1`）—— epilogue 的地基
# ============================================================================
# 为什么要有它（两件事共用一个前提）：
#
#   A. **让 PL 能读 f32 激活**：`read_out` 现在把反量化结果 `np.empty` 在**宿主内存**，
#      PL 读不到（非连续/非 CMA）。`bin_catpack`（R_RUN_UNIT=5）要直读 DDR 里的 f32
#      段 ⇒ 这些段必须落在 arena 的物理连续区里。
#   B. **消掉每层 `np.empty`**：`runs/epi256/REPORT.md` 量到 epilogue 1.36 s 里
#      ~0.35 s 是每层 165 MB 的 mmap/缺页（64² 上已量到同量级）。
#
# 语义（默认关闭，`BIN_F32POOL` 未设时**一个字节都不走这里**）：
#   · 预分配 `nslots × slot_bytes`（slot_bytes = 全网最大层的 f32 尺寸），每个槽
#     64 B 对齐、物理连续 ⇒ `f32_segments()` 能给出 PA。
#     板端这块池是**独立**的 CMA（`BinArena`），**不**塞进 IN/OUT 那块 —— 原因见
#     `BinConvEngine.enable_f32_pool()` 的 docstring（整块 cache 维护量 ×5.5）；
#   · `read_out` 反量化**直接写进槽**（C 内核 / numpy 都支持 `out=`），
#     返回 `slot[:C]` 的**只读视图**（writeable=False），不是新分配；
#   · 归还：① 显式 `f32_release()`（bin_net 在 BV_FREE 判死点调用）；
#            ② `weakref.finalize` 兜底（最后一个引用消失就还）；
#     两条路都带 generation token ⇒ 幂等，且**旧张量的 finalizer 绝不会误放新租户的槽**；
#   · 槽满 / 单层超过 slot_bytes ⇒ 返回 None，调用方**回落到原来的 np.empty**
#     （结果逐位相同，绝不崩）。
class F32Pool:
    """arena 里的一组固定大小 f32 槽（`read_out` 的输出目的地）。"""

    def __init__(self, arena, nslots=None, slot_bytes=None, align=64, name="f32"):
        """`arena` 可以是一块 arena（从里面切 N 个槽），也可以是 **N 块 arena 的列表**
        （板端用：每槽一块独立 CMA ⇒ `tensor.flush()` 只清这个槽，见 `flush_slot`）。"""
        if slot_bytes is None:
            raise ValueError("F32Pool 需要 slot_bytes")
        slot_bytes = int(slot_bytes)
        if slot_bytes <= 0:
            raise ValueError("slot_bytes 必须 > 0，得到 %d" % slot_bytes)
        slot_bytes = (slot_bytes + align - 1) // align * align
        self.slot_bytes = slot_bytes
        self.name = name
        self._slots = []
        if isinstance(arena, (list, tuple)):        # 每槽一块独立缓冲
            arenas = list(arena)
            for a in arenas:
                sub, _pa = a.region(slot_bytes, align=align, name=name)
                self._slots.append(sub)
            self._arenas = arenas
        else:                                       # 从同一块里切
            nslots = max(1, int(nslots))
            region, _pa0 = arena.region(nslots * slot_bytes, align=align, name=name)
            for i in range(nslots):
                off = i * slot_bytes
                self._slots.append(region[off:off + slot_bytes])   # PA 已按偏移继承
            self._arenas = [arena]
        self.arena = self._arenas[0]
        self.nslots = len(self._slots)
        self.pa0 = int(getattr(self._slots[0], "physical_address", 0))
        self._pas = [int(getattr(s, "physical_address", 0)) for s in self._slots]
        self._free = list(range(self.nslots))
        self._busy = [False] * self.nslots
        self._gen = [0] * self.nslots
        self.hits = 0
        self.misses = 0
        self.releases = 0

    # ---------------------------------------------------------------- 租约
    def acquire(self, nbytes):
        """要一个 ≥ nbytes 的槽；没有空闲/装不下 ⇒ None（调用方回落）。"""
        nbytes = int(nbytes)
        if nbytes <= 0 or nbytes > self.slot_bytes or not self._free:
            self.misses += 1
            return None
        i = self._free.pop()
        self._busy[i] = True
        self._gen[i] += 1
        self.hits += 1
        view = self._slots[i][:nbytes].view(np.float32)
        return _F32Lease(self, i, self._gen[i], view)

    def release_slot(self, idx, gen):
        """归还（幂等）：token 过期 ⇒ 什么都不做。"""
        if not (0 <= int(idx) < self.nslots):
            return False
        idx = int(idx)
        if not self._busy[idx] or int(gen) != self._gen[idx]:
            return False
        self._busy[idx] = False
        self._free.append(idx)
        self.releases += 1
        return True

    def release(self, tensor):
        """按张量上挂的 `_f32_slot` 归还；不是池张量 ⇒ False。"""
        return f32_release(tensor)

    def flush_slot(self, idx):
        """只把第 `idx` 个槽冲到 DDR —— catpack 读之前就调这个（不是整块池）。"""
        try:
            a = self._arenas[idx] if len(self._arenas) > 1 else self._arenas[0]
            a.flush()
            return True
        except Exception:                  # noqa: BLE001
            return False

    def flush(self):
        """把整块池（所有槽）flush；逐层用 `flush_slot`。"""
        for a in self._arenas:
            try:
                a.flush()
            except Exception:              # noqa: BLE001
                pass

    def segments(self, idx):
        """槽 idx 的 [(pa, bytes)]（调试 / f32_segments 用）。"""
        return [(self._pas[int(idx)], self.slot_bytes)]

    def stats(self):
        return dict(slots=self.nslots, slot_bytes=self.slot_bytes,
                    bytes=self.nslots * self.slot_bytes, free=len(self._free),
                    hits=self.hits, misses=self.misses, releases=self.releases,
                    pa0=self.pa0, pas=list(self._pas))

    def __repr__(self):
        s = self.stats()
        return ("F32Pool(%d×%.2f MB @%#x, free=%d, hit=%d, miss=%d)"
                % (s["slots"], s["slot_bytes"] / 1e6, s["pa0"], s["free"],
                   s["hits"], s["misses"]))


class _F32Lease:
    """一个已租出的槽：`.view` 是可写 f32 视图（内核往里写），`.bind()` 挂归还钩子。"""

    __slots__ = ("pool", "idx", "gen", "view", "bound")

    def __init__(self, pool, idx, gen, view):
        self.pool, self.idx, self.gen, self.view = pool, idx, gen, view
        self.bound = False

    def bind(self, tensor):
        """张量判死时归还：① 显式（BV_FREE 点）；② 引用归零时 finalizer 兜底。"""
        self.bound = True
        try:
            tensor._f32_slot = (self.pool, self.idx, self.gen)
        except Exception:                  # noqa: BLE001
            pass
        try:
            weakref.finalize(tensor, _f32_slot_finalize, weakref.ref(self.pool),
                             self.idx, self.gen)
        except TypeError:                  # 不能弱引用/不能挂 finalizer ⇒ 只靠显式归还
            pass
        return tensor

    def release(self):
        self.pool.release_slot(self.idx, self.gen)
        self.view = None


def _f32_slot_finalize(pool_ref, idx, gen):
    p = pool_ref()
    if p is not None:
        try:
            p.release_slot(idx, gen)
        except Exception:                  # noqa: BLE001
            pass


def f32_release(tensor):
    """把一个池张量归还给它的槽（幂等；非池张量/已归还 ⇒ False）。

    bin_net 的 BV_FREE 判死点调它（"所有消费者都跑完"那一刻）；finalizer 是兜底。
    """
    tag = getattr(tensor, "_f32_slot", None)
    if not tag:
        return False
    try:
        pool, idx, gen = tag
        return bool(pool.release_slot(idx, gen))
    except Exception:                      # noqa: BLE001
        return False


def f32_visible(tensor):
    """这个张量在 PL 可读的物理内存里吗（有 physical_address + 连续）？"""
    return bool(f32_segments(tensor))


def f32_segments(tensor):
    """`(C,H,W) float32` 张量 → `[(pa, n_channels, H, W), ...]`（PL 可直读的物理段）。

    这正是 `bin_catpack`（R_RUN_UNIT=5）逐通道描述符要的东西：
        src_pa[c] = pa + (c - c_off) * H * W * 4
    返回 **[]** ⇒ 该张量不在 PL 可见的 arena 里（宿主 numpy / 不连续 / 非 f32）
    ⇒ catpack 不能用它，调用方必须回落 PS 打包。

    ⚠ 只返回**物理地址**，不保证 cache 一致：PL 读之前请先 `f32_flush(tensor)`
      （或 `pool.flush()`），否则可能读到旧数据。
    """
    try:
        if not isinstance(tensor, np.ndarray) or tensor.dtype != np.float32:
            return []
        if tensor.ndim != 3:
            return []
        pa = getattr(tensor, "physical_address", None)
        if pa is None:                     # 顺着 base 链找（不是 RegionBuffer 的情况）
            b = getattr(tensor, "base", None)
            while b is not None and pa is None:
                pa = getattr(b, "physical_address", None)
                b = getattr(b, "base", None)
        if pa is None:
            return []
        C, H, W = (int(v) for v in tensor.shape)
        st = tuple(int(s) for s in tensor.strides)
        if st == (H * W * 4, W * 4, 4):    # CHW 连续（read_out 的输出就是这样）
            return [(int(pa), C, H, W)]
        return []
    except Exception:                      # noqa: BLE001
        return []


def f32_flush(tensor):
    """把张量所在的**槽** flush 到 DDR（PL 读之前必须）。返回是否成功。

    池张量 ⇒ 只 flush 它那个槽（板端每槽一块独立 CMA）；非池张量 ⇒ 按普通缓冲 flush。
    """
    tag = getattr(tensor, "_f32_slot", None)
    if tag:
        try:
            pool, idx, _gen = tag
            if pool.flush_slot(idx):
                return True
        except Exception:                  # noqa: BLE001
            pass
    f = getattr(tensor, "flush", None)
    if f is None:
        return False
    try:
        f()
        return True
    except Exception:                      # noqa: BLE001
        return False


def f32pool_default_slots():
    try:
        return max(1, int(os.environ.get("BIN_F32_SLOTS", "6") or 6))
    except Exception:                      # noqa: BLE001
        return 6


def f32pool_plan(conv_ops):
    """按 `BIN_F32POOL` + 本层集合算 (nslots, slot_bytes)；关闭时返回 (0, 0)。

    开关语义（默认关闭 ⇒ 与改动前逐位相同）：
      · 未设 / "" / 0 / off / false / no ⇒ (0, 0)
      · 1 / on / true / yes            ⇒ 开，槽数 = $BIN_F32_SLOTS（默认 6）
      · ≥2 的整数                       ⇒ 开，槽数 = 该整数（上限 32）
    """
    mode = os.environ.get("BIN_F32POOL", "").strip().lower()
    if mode in ("", "0", "off", "false", "no", "n"):
        return 0, 0
    n = 0
    if mode.isdigit():
        n = int(mode)
        if n == 0:
            return 0, 0
        if n == 1:
            n = 0                         # "1" = 只表示"开"，槽数走 BIN_F32_SLOTS
    if n <= 0:
        n = f32pool_default_slots()
    n = min(int(n), 32)
    ops = list(conv_ops)
    if not ops:
        return 0, 0
    slot_bytes = 0
    for o in ops:
        L = o.layer
        cg = (int(L.cout) + C8 - 1) // C8
        slot_bytes = max(slot_bytes, cg * C8 * int(L.Hout) * int(L.Wout) * 4)
    return n, int(slot_bytes)


# ============================================================================
# 4. 驱动本体
# ============================================================================
class BinConvEngine:
    """`bin_unit_<id>`（UNIT=0，bin_conv_core）的 PS 侧驱动。"""

    def __init__(self, overlay, unit_id=0, addr_map=None, pack_dir=None, verbose=True):
        """
        overlay   : pynq.Overlay（或任何有 .ip_dict 的对象；自检时传 FakeUnit）
        unit_id   : BD 里的实例序号（bin_unit_0 / bin_unit_1 / ...）。当前比特流
                    只有 bin_unit_0；4 个实例的情况按名字自动找。
        addr_map  : 允许显式覆盖基址（默认**从 overlay.ip_dict 取**，不硬编码）
        pack_dir  : deploy/bin_weights_new 目录（给 t_q / 偏移用）
        """
        self.overlay = overlay
        self.verbose = verbose
        self.unit_id = int(unit_id)
        self.ip_name, self.base_pa, self.range_size = self._resolve_ip(overlay, addr_map)
        self.ip = self._get_ip(overlay, self.ip_name)
        self.mmio = getattr(self.ip, "mmio", self.ip)
        if not self._mmio_ok():
            raise TypeError("%s: 拿到的东西没有 .read/.write（既不是 DefaultIP 也不是 MMIO）"
                            % self.ip_name)

        # ---- 配置页自检：A[16] 必须能把 "+0x10000" 变成"另一页" ----
        if (self.base_pa >> 16) & 1:
            raise ValueError(
                "%s 的物理基址 %#x 的第 16 位是 1 ⇒ '+0x10000' 会翻到保留页，"
                "寄存器全读 0。BD 里必须把这个从机放在 64 KB 对齐且 A[16]=0 的段上"
                "（本设计是 0x4006_0000）" % (self.ip_name, self.base_pa))
        if self.range_size and self.range_size < CFG + 0x100:
            raise ValueError("%s 地址窗口只有 %#x B，装不下 +0x%x 的配置页"
                             % (self.ip_name, self.range_size, CFG))

        self.pack_dir = pack_dir or self._default_pack_dir()
        self.blob = None                  # 板上的 pynq buffer（load_weights 后有效）
        self.blob_pa = None
        self.host_blob = None             # 主机侧 numpy 副本（读 t_q / 偏移）
        self.cfg = None                   # 当前配置（BinLayer）
        self._regions = []                # [(pa, end, tag)] 护栏白名单
        self._wcache = {}                 # 权重搬运缓存
        self._scratch = {}                # __call__ 的内部缓冲
        self._writes = 0
        self._reads = 0
        # ★ MMIO 影子寄存器：只写"真的变了"的值。
        #   RTL 在 start 时把 cfg_*/base_* 全部锁存进 _q 寄存器（bin_conv_core.v
        #   ST_IDLE 分支），因此**跑一层的时候可以安全地改配置页**去准备下一层 ——
        #   这既是省写次数的手段，也是"把 MMIO 藏进 PL 时间"的前提。
        #   语义安全前提：配置页只有本进程在写（eng 独占该 IP）。AP_CTRL/DBG_* 有
        #   写副作用（start、清 sticky），一律 force=True 绕开缓存。
        self._shadow = {}
        self._writes_skipped = 0
        self._adj_cache = {}              # (layer name, fs_in) → (adj, ok)
        # ★ 2026-10-31：E1E2 打包后端（默认 None ⇒ conv 路径逐位不变）。
        #   只有 BIN_PACK=e1e2 或显式 enable_e1e2() 才会挂上；见 E1E2Engine。
        self._e1e2 = None
        self._pack_env = os.environ.get("BIN_PACK", "").strip().lower()
        # ★ 2026-10-31（f32 池）：默认 None ⇒ read_out 走原来的 np.empty（逐位不变）。
        #   只有 BinNet.run() 在 BIN_F32POOL 打开时才 enable_f32_pool()。
        self._f32pool = None
        self._f32pool_fallback = 0
        # ★ 2026-10-31（catpack）：默认 None ⇒ pack_input 一个字节都不多动；
        #   只有 CATPACK=1 / enable_catpack() 才会挂上（见 catpack.py）。
        self._catpack = None
        self._cat_env = os.environ.get("CATPACK", "").strip().lower()
        self._catpack_fallback = 0

    # ---------------------------------------------------------------- f32 池
    def enable_f32_pool(self, arena=None, nslots=None, slot_bytes=None, name="f32"):
        """预分配 f32 输出槽，并让 `read_out()` 直接写进去（默认**不**调用）。

        板端（有 pynq）⇒ 池是**独立**的一块 CMA（`BinArena`），不是塞进 IN/OUT 那块：
          每层 `read_out` 的 `_invalidate(out_buf)` 与 `push_input` 的 `_flush(in_buf)`
          都是**整块缓冲**的 cache 维护（`RegionBuffer._owner` 是整块 pynq buffer）。
          把 25 MB 的池塞进 5.6 MB 的 IN/OUT arena ⇒ 每层缓存维护量 ×5.5，
          板端实测 invalidate 是 0.27 ms/层（`runs/epi256/REPORT.md` §2.3），
          放大后很可能把那 0.35 s 的分配收益吃回去。
          池独立以后，IN/OUT 那块**一个字节都没变** ⇒ 每层缓存维护成本与今天相同。
        主机（无 pynq，`FakeArena` 自检/AB）⇒ 从传入的 `arena` 里切（没有 cache 维护）。

        槽满 / 单层超尺寸 ⇒ read_out 自动回落宿主 numpy（逐位相同，只是没进池）。
        """
        if slot_bytes is None:
            raise ValueError("enable_f32_pool 需要 slot_bytes（= 最大层的 f32 尺寸）")
        if nslots is None:
            nslots = f32pool_default_slots()
        pool_arena = None
        if _pynq_allocate is not None:
            # ★ 每个槽一块独立 CMA：`tensor.flush()` / `pool.flush_slot(i)` 只清这一槽
            #   （6.29 MB），而不是整块池（25 MB）。catpack 逐层读 f32 时这是关键：
            #   整块池 flush 在 227 层上是 ~0.27 s，按槽只剩 ~0.07 s。
            nb = (int(slot_bytes) + 4095) // 4096 * 4096
            pool_arena = [BinArena(nb, name="f32slot%d" % i)
                          for i in range(int(nslots))]
            for a in pool_arena:
                try:
                    self.add_region(a.buf, name)   # 给（将来的）catpack 护栏用
                except Exception:              # noqa: BLE001
                    pass
        else:
            if arena is None:
                raise ValueError("没有 pynq：enable_f32_pool 需要传一块 FakeArena")
            pool_arena = arena
        self._f32pool = F32Pool(pool_arena, nslots=nslots, slot_bytes=slot_bytes,
                                name=name)
        if self.verbose:
            print("[bin_engine] f32 池：%s" % (self._f32pool,))
        return self._f32pool

    def disable_f32_pool(self):
        """摘掉 f32 池（read_out 回到 np.empty）。返回被摘掉的池（可能 None）。"""
        old, self._f32pool = self._f32pool, None
        return old

    def f32pool_stats(self):
        p = self._f32pool
        if p is None:
            return None
        st = p.stats()
        st["fallback"] = int(self._f32pool_fallback)
        return st

    # ---------------------------------------------------------------- 解析
    def _resolve_ip(self, overlay, addr_map):
        idict = getattr(overlay, "ip_dict", None)
        if idict is None:                 # FakeUnit 直接给寄存器堆
            if addr_map is None:
                raise ValueError("既没有 overlay.ip_dict，也没给 addr_map")
            pa = int(addr_map.get("phys_addr", addr_map.get("BASE", 0)))
            rng = int(addr_map.get("range", addr_map.get("mem_range_size", 0x20000)))
            return addr_map.get("name", "bin_unit_%d" % self.unit_id), pa, rng
        want = "bin_unit_%d" % self.unit_id
        if want in idict:
            name = want
        else:
            cands = sorted(k for k in idict if k.startswith("bin_unit"))
            if not cands:
                raise RuntimeError("%s 里没有 bin_unit*（IPLIST=%s）"
                                   % (self.ip_name if hasattr(self, "ip_name") else "overlay",
                                      sorted(idict)))
            name = cands[min(self.unit_id, len(cands) - 1)]
            if self.verbose and name != want:
                print("[bin_engine] ⚠ 没有 %s，退回 %s（现有：%s）"
                      % (want, name, cands))
        info = idict[name]
        pa = int(info.get("phys_addr", addr_map or 0))
        rng = int(info.get("mem_range_size", 0x20000))
        return name, pa, rng

    def _get_ip(self, overlay, name):
        idict = getattr(overlay, "ip_dict", None)
        if idict is not None and name in idict:
            try:
                return getattr(overlay, name)
            except Exception:             # noqa: BLE001
                return idict[name]
        return overlay                     # FakeUnit

    def _mmio_ok(self):
        m = self.mmio
        if hasattr(m, "read") and hasattr(m, "write"):
            return True
        return False

    def _default_pack_dir(self):
        for rel in (("..", "..", "deploy", "bin_weights_new"),
                    ("..", "deploy", "bin_weights_new"),
                    ("bin_weights_new",)):
            p = os.path.abspath(os.path.join(HERE, *rel))
            if os.path.exists(os.path.join(p, "index.json")):
                return p
        return None

    # ---------------------------------------------------------------- 底层
    def _wr(self, off, v, force=False):
        """写一个寄存器。force=False 时若影子寄存器里已是该值则**跳过**。

        227 层里大多数层的 S/D/PAD/ALPHA_SHIFT/SLOPE_SHIFT/H/W/K/COUT 反复重复，
        基址的高字恒 0 ⇒ 缓存后每层真正写下去的次数从 30 降到 3~6 次。
        （MMIO 写实测 30.4 µs/次，见 perf_probe `[mem] MMIO write`。）
        """
        off = int(off)
        v = int(v) & 0xFFFFFFFF
        if not force and self._shadow.get(off, None) == v:
            self._writes_skipped += 1
            return
        self.mmio.write(off, v)
        self._shadow[off] = v
        self._writes += 1

    def _wr_reg(self, name, v, force=False):
        self._wr(CFG + BIN_REG[name], v, force=force)

    def shadow_reset(self):
        """丢掉影子寄存器（硬件被别的路径改过之后必须调用）。"""
        self._shadow.clear()

    def _rd(self, off):
        self._reads += 1
        return int(self.mmio.read(int(off))) & 0xFFFFFFFF

    def _rd_reg(self, name):
        return self._rd(CFG + BIN_REG[name])

    def set_base(self, name, pa):
        """写一个基址（低字 + 有条件的高字 0）。name ∈ CONV_BASES。"""
        key = name.upper()
        if ("B_" + key) not in BIN_REG:
            raise KeyError("未知基址 %s" % name)
        pa = BinGuard.check_pa(pa, "B_" + key)
        self._wr_reg("B_" + key, pa)
        if key in HIGH_WORD:              # ⚠ 只有这 8 个有高字槽
            self._wr(CFG + HIGH_WORD[key], 0)
        return pa

    def summary(self):
        return dict(ip=self.ip_name, base_pa=self.base_pa, range=self.range_size,
                    cfg_page=self.base_pa + CFG, pack_dir=self.pack_dir,
                    writes=self._writes, reads=self._reads)

    # ---------------------------------------------------------------- 白名单
    def add_region(self, buf, tag="buf"):
        """登记一块缓冲，之后所有基址都必须落在某个登记块里。"""
        pa = int(buf.physical_address)
        self._regions.append((pa, pa + len(buf), tag))
        return pa

    def _regions_all(self):
        return list(self._regions)

    # ---------------------------------------------------------------- 权重搬运
    def load_weights(self, buf=None, offset=0, path=None, force=False):
        """把整份 `bin_weights.bin` **一次**搬进 DDR，返回 (buf, blob_pa)。

        为什么是"整份"而不是"逐层"：容器只有 1.07 MB，而 227 层的权重分散在
        同一文件里、每层 7 个区域；逐层搬会把 227 次 memcpy 变成 227 次
        "拷-等-done"的往返，还更容易搬错。于是 `base_wgt = blob_pa + off_wgt`
        直接指过去，**零次额外拷贝**。

        缓存键 = (文件路径, 大小, mtime, id(buf), offset)：
        同一份容器重复调用（同一层被反复跑时每帧都会调）**不会重搬**。

        buf 给了就搬进 buf[offset:offset+size]（要求 8 字节对齐 + 4 KB 尾巴），
        不给就自己 allocate（size 向上取整到 4 KB）。
        """
        path = path or (os.path.join(self.pack_dir, "bin_weights.bin")
                        if self.pack_dir else None)
        if path is None or not os.path.exists(path):
            raise FileNotFoundError("找不到 bin_weights.bin（pack_dir=%s）" % self.pack_dir)
        st = os.stat(path)
        size = st.st_size
        offset = int(offset)
        if offset & 0x7:
            raise ValueError("offset=%#x 必须 8 字节对齐（base_wgt 会被 64-bit 读）" % offset)
        key = (os.path.abspath(path), size, int(st.st_mtime), id(buf), offset)
        if not force and key in self._wcache:
            self.blob, self.blob_pa = self._wcache[key]
            return self.blob, self.blob_pa

        need = offset + size
        if buf is None:
            if _pynq_allocate is None:
                raise RuntimeError("没有 pynq：请用 load_weights(buf=本地 numpy 缓冲)")
            if self.host_blob is None:
                self.host_blob = Blob(self.pack_dir)
            pad = (-need) % 4096
            buf = _pynq_allocate(shape=(need + pad,), dtype=np.uint8)
            self.add_region(buf, "blob")
        else:
            if len(buf) < need + 8:
                raise ValueError("buf 只有 %d B，装不下 %d B 容器（offset=%d）"
                                 % (len(buf), need, offset))
        mv = np.frombuffer(buf, np.uint8)
        with open(path, "rb") as f:
            data = np.frombuffer(f.read(), np.uint8)
        mv[offset:offset + size] = data
        try:
            buf.flush()
        except Exception:                 # noqa: BLE001
            pass
        if offset & 0x7 or (int(buf.physical_address) + offset) & 0x7:
            raise ValueError("blob 的物理地址没 8 字节对齐，base_wgt 会 ERR_ALIGN")
        self.blob, self.blob_pa = buf, int(buf.physical_address) + offset
        self._wcache[key] = (self.blob, self.blob_pa)
        if self.verbose:
            print("[bin_engine] 权重容器已搬入 DDR: %d B @ pa=%#x（%s）"
                  % (size, self.blob_pa, os.path.basename(path)))
        return self.blob, self.blob_pa

    def layer(self, i_or_entry, H=None, W=None):
        """取出 BinLayer：可传层下标（用 pack_dir 的 index.json）、dict、或已构造的 BinLayer。"""
        if isinstance(i_or_entry, BinLayer):
            return i_or_entry
        if isinstance(i_or_entry, dict):
            return BinLayer(i_or_entry, H, W)
        if self.host_blob is None:
            if self.pack_dir is None:
                raise RuntimeError("没有 pack_dir，无法按下标取层")
            self.host_blob = Blob(self.pack_dir)
        return BinLayer(self.host_blob.layer(int(i_or_entry)), H, W)

    # ---------------------------------------------------------------- 配置
    def config(self, layer, H=None, W=None):
        """按 index.json 的一层写配置页（**只写寄存器，不启动、不访存**）。

        layer 可以是 BinLayer / index.json 的 dict / 层下标；H/W 缺省时从 layer 里取。
        """
        L = self.layer(layer, H, W)
        for name, val in L.regs():
            self._wr_reg(name, val)
        self.cfg = L
        return L

    def config_readback(self, L=None):
        """读回配置页，返回 {寄存器: 读到值}。用于阶段 2 的 AXI-Lite 通路验证。"""
        L = L or self.cfg
        got = {}
        for name, _v in L.regs():
            got[name] = self._rd_reg(name)
        for name in ("B_IN", "B_WGT", "B_ALPHA", "B_BIAS", "B_ZETA", "B_GAMMA",
                     "B_SLOPE", "B_OUT"):
            got[name] = self._rd_reg(name)
        return got

    def config_check(self, L=None):
        """回读并与期望比对，返回 (ok, [不一致项])。AP_CTRL 读恒 0 不参与。"""
        L = L or self.cfg
        want = dict(L.regs())
        got = self.config_readback(L)
        bad = []
        for k, v in want.items():
            if got.get(k) != (int(v) & 0xFFFFFFFF):
                bad.append("%s: 期望 %#x 读到 %#x" % (k, int(v) & 0xFFFFFFFF, got.get(k)))
        return (not bad), bad

    def set_bases(self, L, blob_pa=None, in_pa=None, out_pa=None):
        """写 8 个基址（7 个参数 + OUT）。全部先过护栏。"""
        blob_pa = self.blob_pa if blob_pa is None else blob_pa
        if blob_pa is None:
            raise RuntimeError("还没 load_weights()（blob_pa=None）⇒ base_* 无处可指")
        e = L.d
        self.set_base("IN", in_pa)
        self.set_base("WGT", int(blob_pa) + int(e["off_wgt"]))
        self.set_base("ALPHA", int(blob_pa) + int(e["off_alpha"]))
        self.set_base("BIAS", int(blob_pa) + int(e["off_bias"]))
        self.set_base("ZETA", int(blob_pa) + int(e["off_zeta"]))
        self.set_base("GAMMA", int(blob_pa) + int(e["off_gamma"]))
        self.set_base("SLOPE", int(blob_pa) + int(e["off_slope"]))
        self.set_base("OUT", out_pa)
        return self.bases_now()

    def bases_now(self):
        return {n: self._rd_reg("B_" + n) for n in CONV_BASES}

    # ---------------------------------------------------------------- 运行
    def start(self):
        # ★ AP_CTRL bit0 会自清（bin_unit_axi_wrap.v: "8'h00: bit0 自清"），
        #   所以每次都必须**真的写下去**（否则核的 start_pulse 边沿检测看不到新脉冲）。
        self._wr_reg("AP_CTRL", 1, force=True)

    def start_and_check(self, timeout=2.0, wdt=None, quiet=True):
        """发一次 start 并等 done；**错误码非 0 一律抛 BinRunError**。

        分出来是因为校验类错误（ERR_*）是"同步失败"：RTL 在读线静默的下一拍就
        置 done=1 + err_code，不访存。分级测试的阶段 3 只需要这一半。
        """
        self.start()
        st, dt = self.wait(timeout, wdt=wdt)
        err = (st >> 8) & 0xFF
        if err:
            raise BinRunError(err)
        if quiet and self.verbose:
            print("[bin_engine] done %s err=0 (%.3f ms)"
                  % (self.cfg.name if self.cfg else "?", dt * 1e3))
        return st, dt

    def wait(self, timeout=30.0, wdt=None, poll=None):
        """轮询 AP_RETURN.bit0。**超时不再发 start**；有看门狗就让板子复位。

        poll=None ⇒ 自适应（2026-10-30 实测：`time.sleep(2e-4)` 在这块板上实际要
        0.29 ms，`sleep(0)` 只要 0.010 ms，MMIO 读一次 25 µs）：
          · 前几拍不睡（短层 0.3~3 ms 能立刻抓到 done，不吃 0.29 ms 的量化误差）
          · 之后 sleep(0)，再往后逐步退避到 2e-4（长层不空转抢 AXI 带宽）
        显式传 poll=<秒> 则保持旧的固定间隔行为（老脚本/对比用）。
        """
        t0 = time.time()
        n = 0
        while True:
            st = self._rd_reg("AP_RET")
            if st & 1:
                return st, time.time() - t0
            if wdt is not None:
                wdt.beat()                 # 长卷积必须边等边喂狗
            dt = time.time() - t0
            if dt > timeout:
                msg = ("bin_unit 超时 %.1fs 未 done（timeout=%s）。**不再发第二次 start**："
                       "引擎可能还在访存，继续跑会踩内存。" % (dt, timeout))
                if wdt is not None and getattr(wdt, "armed", False):
                    print("[bin_engine] " + msg, flush=True)
                    print("[bin_engine] 停止喂狗，%.0fs 后由硬件看门狗复位整板"
                          % getattr(wdt, "seconds", 0), flush=True)
                    while True:            # 交给看门狗
                        time.sleep(1.0)
                raise TimeoutError(msg)
            n += 1
            if poll is not None:
                time.sleep(poll)
            elif n < 3:
                pass                              # 紧轮询：抓 0.3 ms 级的短层
            elif n < 96:
                time.sleep(0)                     # ~10 µs
            else:
                time.sleep(min(2e-4, 2e-6 * (n - 95)))   # 逐步退避

    def run(self, in_buf, out_buf, L=None, timeout=None, wdt=None, verify_canary=True):
        """配置 + 基址 + start + 等 done + 解码错误码 + 查金丝雀。返回 RunStats。

        in_buf / out_buf: pynq allocate 出来的缓冲（或 FakeArena 切出来的视图）。
        out_buf 尾部必须留 4 KB 金丝雀（BinArena.region(..., keep_canary=True)）。
        """
        L = L or self.cfg
        if L is None:
            raise RuntimeError("还没 config()（不知道 H/W/形状）")
        in_pa = int(getattr(in_buf, "physical_address", 0))
        out_pa = int(getattr(out_buf, "physical_address", 0))
        if in_pa and out_pa:
            self.add_region(in_buf, "IN")
            self.add_region(out_buf, "OUT")

        # ---- 尺寸 + 护栏（启动前全部过一遍，任何一条不过就不 start）----
        need_in = L.act_bytes
        if need_in > len(in_buf):
            raise ValueError("输入缓冲太小：需要 %d B（cb=%d,H=%d,W=%d），实际 %d B"
                             % (need_in, L.cb, L.H, L.W, len(in_buf)))
        need_out = L.out_bytes
        avail = len(out_buf) - (CANARY_BYTES if verify_canary else 0)
        if need_out > avail:
            raise ValueError("输出缓冲太小：需要 %d B（ceil(cout/8)=%d,Hout=%d,Wout=%d）"
                             " + 金丝雀，实际只有 %d B 可写"
                             % (need_out, (L.cout + 7) // 8, L.Hout, L.Wout, avail))
        BinGuard.check_pa(in_pa, "IN")
        BinGuard.check_pa(out_pa, "OUT", align=1)   # B_OUT 是字节写，不查对齐
        regs = self._regions_all()
        BinGuard.check_region(in_pa, need_in, regs, "IN")
        BinGuard.check_region(out_pa, need_out, regs, "OUT")
        if self.blob_pa is None:
            raise RuntimeError("还没 load_weights()：base_wgt/alpha/bias/... 无处可指")
        wgt_pa = int(self.blob_pa) + int(L.d["off_wgt"])
        BinGuard.check_region(wgt_pa, L.wgt_bytes, regs, "WGT")
        for fld, n in (("alpha", L.cout * 2), ("bias", L.cout * 4), ("zeta", L.cout * 4),
                       ("gamma", L.cout * 4), ("slope", L.cout * 2)):
            BinGuard.check_region(int(self.blob_pa) + int(L.d["off_" + fld]), n, regs,
                                  fld.upper())

        if verify_canary:
            BinGuard.fill_canary(out_buf)

        # ---- 真正下发 ----
        self.config(L)
        self.set_bases(L, in_pa=in_pa, out_pa=out_pa)
        if timeout is None:
            _c, est = estimate_cycles(L)
            timeout = max(5.0, min(600.0, est * 4 + 5.0))
        self.clear_dbg()
        self.start()
        st, dt = self.wait(timeout, wdt=wdt)
        err = (st >> 8) & 0xFF
        if err:
            raise BinRunError(err)
        busy = (st >> 1) & 1
        if busy:
            raise RuntimeError("done=1 但 busy=1（AP_RETURN=%#010x）⇒ 接线/时序可疑" % st)
        if verify_canary:
            BinGuard.check_canary(out_buf, need_out, "bin_unit OUT")
        return RunStats(status=st, seconds=dt, err_code=err, layer=L,
                        hout=L.Hout, wout=L.Wout, dbg=self.dbg())

    def dbg(self):
        rd = self._rd_reg("DBG_RD")
        wr = self._rd_reg("DBG_WR")
        return dict(rd_words=rd & 0xFFFF, fill_win=rd >> 16,
                    aw=wr & 0xFFFF, w_beats=wr >> 16,
                    flags=self._rd_reg("DBG_FLAG"))

    def clear_dbg(self):
        # 这三个寄存器是"写 1 清 sticky"的**写副作用**寄存器 ⇒ 必须绕开影子缓存
        self._wr_reg("DBG_RD", 0, force=True)
        self._wr_reg("DBG_WR", 0, force=True)
        self._wr_reg("DBG_FLAG", 1, force=True)   # 写 bit0 清 sticky

    # ------------------------------------------------------------------
    # ★ 异步三分段（层间流水的地基）
    # ------------------------------------------------------------------
    # 为什么可以"提前写配置"：bin_conv_core.v 的 ST_IDLE 分支在 start_pulse 那一沿
    # 把**全部** cfg_* 和 8 个 base_* 锁进自己的 _q 寄存器（cout_q/k_q/h_q/base 的
    # act_b/wgt_b/alpha_b/... ），之后 CMP/WLD 只用锁存值。所以在 PL 跑第 n 层的
    # 同时改写 AXI-Lite 配置页去准备第 n+1 层是**硬件允许**的
    # ⇒ 每层 30 次 MMIO 写（0.9~1.35 ms）可以整个藏进上一层的 PL 时间里。
    def guard_layer(self, L, in_buf, out_buf, verify_canary=True):
        """启动前的全部护栏检查（尺寸 / 对齐 / 白名单 / 权重区）。"""
        in_pa = int(getattr(in_buf, "physical_address", 0))
        out_pa = int(getattr(out_buf, "physical_address", 0))
        need_in, need_out = L.act_bytes, L.out_bytes
        if need_in > len(in_buf):
            raise ValueError("%s: 输入缓冲太小，需要 %d B，实际 %d B"
                             % (L.name, need_in, len(in_buf)))
        avail = len(out_buf) - (CANARY_BYTES if verify_canary else 0)
        if need_out > avail:
            raise ValueError("%s: 输出缓冲太小，需要 %d B + 金丝雀，可写只有 %d B"
                             % (L.name, need_out, avail))
        BinGuard.check_pa(in_pa, "IN")
        BinGuard.check_pa(out_pa, "OUT", align=1)
        regs = self._regions_all()
        BinGuard.check_region(in_pa, need_in, regs, "IN")
        BinGuard.check_region(out_pa, need_out, regs, "OUT")
        if self.blob_pa is None:
            raise RuntimeError("还没 load_weights()：base_wgt/alpha/bias/... 无处可指")
        BinGuard.check_region(int(self.blob_pa) + int(L.d["off_wgt"]),
                              L.wgt_bytes, regs, "WGT")
        for fld, n in (("alpha", L.cout * 2), ("bias", L.cout * 4),
                       ("zeta", L.cout * 4), ("gamma", L.cout * 4),
                       ("slope", L.cout * 2)):
            BinGuard.check_region(int(self.blob_pa) + int(L.d["off_" + fld]), n, regs,
                                  fld.upper())
        return in_pa, out_pa, need_out

    def push_input(self, data, in_buf, verify=False):
        """把打包好的激活字节写进 IN 缓冲 + flush。verify=True ⇒ 写完读回逐字节比。"""
        mv = np.frombuffer(in_buf, np.uint8)
        src = np.frombuffer(data, np.uint8)
        mv[:len(src)] = src
        _flush(in_buf)
        if verify:
            _invalidate(in_buf)          # 读回前必须作废 cache，否则读到的是自己写的影子
            back = np.frombuffer(in_buf, np.uint8)[:len(src)]
            if not np.array_equal(back, src):
                nbad = int((back != src).sum())
                raise RuntimeError("IN 缓冲读回校验失败：%d/%d 字节不符 ⇒ 数据竞争/越界"
                                   % (nbad, len(src)))
            _flush(in_buf)
        return len(src)

    def arm(self, L, in_buf, out_buf, verify_canary=True, cfg_only=False):
        """写配置 + 基址（可提前调用）。cfg_only=True 时不查尺寸（只写寄存器）。"""
        if cfg_only:
            in_pa = int(getattr(in_buf, "physical_address", 0))
            out_pa = int(getattr(out_buf, "physical_address", 0))
            need_out = L.out_bytes
        else:
            in_pa, out_pa, need_out = self.guard_layer(L, in_buf, out_buf,
                                                       verify_canary=verify_canary)
        self.config(L)
        self.set_bases(L, in_pa=in_pa, out_pa=out_pa)
        return need_out

    def kick(self, timeout=None):
        """发 start，立即返回（不等 done）。返回 (t0, timeout)。"""
        if timeout is None:
            _c, est = estimate_cycles(self.cfg)
            timeout = max(5.0, min(600.0, est * 4 + 5.0))
        t0 = time.time()
        self.start()
        return t0, timeout

    def poll(self):
        """非阻塞看 done：返回 status 或 None。"""
        st = self._rd_reg("AP_RET")
        return st if (st & 1) else None

    def reap(self, t0, timeout, out_buf=None, L=None, verify_canary=True, t_done=None):
        """等 done + 解码错误码 + 查金丝雀，返回 RunStats。

        t_done 由调用方在**第一次 poll 看到 done** 时记下（异步流水里 done 的时刻
        比 reap 的时刻早）。不给就退化成"reap 时刻 - start 时刻"。
        """
        L = L or self.cfg
        st, _ = self.wait(timeout)
        err = (st >> 8) & 0xFF
        if err:
            raise BinRunError(err, msg="%s: bin_unit 报错 err_code=%d" % (L.name, err))
        if (st >> 1) & 1:
            raise RuntimeError("done=1 但 busy=1（AP_RETURN=%#010x）⇒ 接线/时序可疑" % st)
        if out_buf is not None and verify_canary:
            BinGuard.check_canary(out_buf, L.out_bytes, "bin_unit OUT")
        return RunStats(status=st, seconds=(t_done or time.time()) - t0, err_code=err,
                        layer=L, hout=L.Hout, wout=L.Wout, dbg={})

    # ---------------------------------------------------------------- 便捷入口
    def pack_input(self, x_f, L):
        """float (cin,H,W) → 打包好的激活字节（**逐字节等价**于旧的两段式）。

        旧: x_q = sat8(rint(f64(x)*2^fs_in)); A = binarize_pack(x_q, t_q)
        新: 一次比较（阈值逐层缓存）。t_q 越界时自动退回参考实现。
        """
        if self.host_blob is None:
            self.host_blob = Blob(self.pack_dir) if self.pack_dir else None
        if self.host_blob is None:
            raise RuntimeError("需要 pack_dir 来取 t_q（逐通道二值化阈值）")
        # ★ E1E2 开关：默认 _e1e2=None 且 BIN_PACK 不是 e1e2 ⇒ 下面一个字节都不多动。
        if self._e1e2 is None and self._pack_env == "e1e2":
            self.enable_e1e2()
        key = (L.name, int(L.d["act_fs_in"]))
        got = self._adj_cache.get(key)
        if got is None:
            got = pack_thresh(self.host_blob.t_q(L.d), int(L.d["act_fs_in"]))
            self._adj_cache[key] = got
        adj, ok = got
        # ★ CATPACK=1（默认关）：源是 PL 可直读的 f32（f32 池）⇒ PL 直接比较+打包，
        #   不再经过 PS 的 quantize_int8/bpack。拿不到 pa ⇒ 返回 None ⇒ 继续往下
        #   走既有路径（逐字节相同）。这条**优先于** E1E2：E1E2 要先在 PS 上
        #   quantize_int8（板端 140 ms/层），catpack 没有这个前提。
        if self._catpack is None and self._cat_env in ("1", "on", "true", "yes"):
            try:
                self.enable_catpack()
            except Exception as _ex:          # noqa: BLE001
                # 比特流没有 catpack ⇒ 明确警告后回落 PS（默认行为不变）
                print("[bin_engine] ⚠ CATPACK=1 但启用失败（%s）⇒ 回落 PS 打包" % _ex)
                self._cat_env = ""
        if self._catpack is not None and ok:
            res = self._catpack.pack_tensor(x_f, adj)      # (u8, cin, H, W, cb) | None
            if res is not None:
                return act_u8(res[0])                      # 已经是 uint8 一维视图
            self._catpack_fallback += 1
        # ★ BIN_LAZY_DEQ：只有当 E1E2 是"所有层都走 PL 打包"时才用 pack_float。
        #   C8 直读模式下，回落到 PS 的层用 C 内核 quant_pack 更快（且不在
        #   prologue 里等上一层 conv 的 done）。
        if self._e1e2 is not None and not getattr(self, "ps_pack_when_e1e2",
                                                   False):
            return self._e1e2.pack_float(
                x_f, L, self.host_blob.t_q(L.d))
        if FAST_PS and ok:
            # ★ 2026-10-31：act_u8 是零拷贝视图（原 act_bytes 做两次满尺寸拷贝）
            return act_u8(quant_pack(x_f, adj))
        x = np.ascontiguousarray(x_f, np.float32)
        x_q = sat8(np.rint(np.asarray(x, np.float64) * (1 << int(L.d["act_fs_in"]))))
        return act_u8(binarize_pack(x_q, self.host_blob.t_q(L.d)))

    def read_out(self, out_buf, L, lazy=False, on_release=None, nref=1):
        """OUT 缓冲 → float (cout,Hout,Wout)：读回 + C8 还原 + 反量化。

        ★ 2026-10-31（`BIN_F32POOL`）：开了池以后，反量化**直接写进 arena 的 f32 槽**，
          返回槽的**只读视图**（有 physical_address ⇒ `f32_segments()` 可给 catpack 用）。
          槽满 / 层太大 / 无池 ⇒ 完全走下面原来的路径（np.empty，逐位相同）。

        ★ 2026-10-31（C8 直读 / BIN_LAZY_DEQ）：`lazy=True` 时**不反量化**，返回
          `C8Tensor`（int8 C8 + 物理地址）。只有当消费者真要 f32 时才物化。
          拿不到物理地址（宿主内存）⇒ 自动退回下面的真反量化（行为不变）。
        """
        if lazy:
            pa = int(getattr(out_buf, "physical_address", 0) or 0)
            if pa:
                _invalidate(out_buf)
                return C8Tensor(out_buf, L.cout, L.Hout, L.Wout,
                                int(L.d["act_fs_out"]), engine=self, layer=L,
                                on_release=on_release, nref=nref)
        _invalidate(out_buf)
        # ★ 2026-10-31：原来这里是 `...[:n].tobytes()`（再拷一份 41 MB），
        #   from_c8_deq 内部本来就只做 np.frombuffer ⇒ 直接给视图，省掉那次拷贝。
        raw = np.frombuffer(out_buf, np.uint8)[:L.out_bytes]
        pool = self._f32pool
        if pool is not None and FAST_PS:
            cg = (int(L.cout) + C8 - 1) // C8
            nf = cg * C8 * int(L.Hout) * int(L.Wout)
            lease = pool.acquire(nf * 4)
            if lease is not None:
                y = from_c8_deq_into(raw, L.cout, L.Hout, L.Wout,
                                     int(L.d["act_fs_out"]), lease.view)
                if y is not None:
                    lease.bind(y)
                    try:
                        y.setflags(write=False)   # 消费者只能读；槽由 read_out 写
                    except Exception:             # noqa: BLE001
                        pass
                    return y
                lease.release()
            self._f32pool_fallback += 1     # 槽满/太大 ⇒ 回落宿主内存（不是错误）
        if FAST_PS:
            return from_c8_deq(raw, L.cout, L.Hout, L.Wout, int(L.d["act_fs_out"]))
        return (from_c8(raw.tobytes(), L.cout, L.Hout, L.Wout).astype(np.float32)
                / float(1 << int(L.d["act_fs_out"])))

    def __call__(self, x_q, layer, timeout=None, wdt=None, in_buf=None, out_buf=None):
        """`x_q`（int8，(cin,H,W) CHW，或 float 张量）→ float (cout,Hout,Wout)。

        自动做 PS 侧二值化+打包（RTL 侧没有"二值化单元"）
        ⇒ 也自动从容器里取该层的 `t_q`（逐输入通道阈值）。
        """
        L = self.layer(layer, x_q.shape[-2], x_q.shape[-1]) \
            if hasattr(x_q, "shape") and x_q.ndim == 3 else self.layer(layer)
        x = np.ascontiguousarray(x_q)
        if x.shape != (L.cin, L.H, L.W):
            raise ValueError("输入形状 %s != (cin=%d,H=%d,W=%d)"
                             % (x.shape, L.cin, L.H, L.W))
        # int8 输入时先按契约还原成 float（多一次转换，但保持旧语义不变）
        xf = x.astype(np.float32) / np.float32(1 << int(L.d["act_fs_in"])) \
            if x.dtype == np.int8 else x
        data = self.pack_input(xf, L)

        in_buf = self._ensure(in_buf, len(data), "IN")
        self.push_input(data, in_buf)
        out_buf = self._ensure(out_buf, L.out_bytes + CANARY_BYTES, "OUT")
        rs = self.run(in_buf, out_buf, L=L, timeout=timeout, wdt=wdt)
        return self.read_out(out_buf, L), rs

    def _ensure(self, buf, nbytes, tag):
        if buf is not None and len(buf) >= nbytes:
            return buf
        key = (tag, nbytes)
        if key not in self._scratch:
            if _pynq_allocate is None:
                raise RuntimeError("没有 pynq：请显式传 %s 缓冲" % tag)
            nb = (nbytes + 4095) // 4096 * 4096
            self._scratch[key] = _pynq_allocate(shape=(nb,), dtype=np.uint8)
            self.add_region(self._scratch[key], tag)
        return self._scratch[key]

    # ---------------------------------------------------------------- E1E2
    def enable_e1e2(self, desc_dir=None, verbose=None):
        """把 E1E2 打包后端挂到本引擎上（**默认不挂**，conv 路径完全不变）。

        会先做一次硬件能力探测（写 R_RUN_UNIT=4 → 读回 → 写 0）：旧比特流的
        0x200 页是保留页（写忽略、读 0）⇒ 直接拒绝，避免"E1E2 没跑、PS 读到
        旧输出"这种静默错数据。
        """
        if self._e1e2 is None:
            eng = E1E2Engine(self, desc_dir=desc_dir,
                             verbose=self.verbose if verbose is None else verbose)
            eng.probe()                      # 失败就不挂上，避免下次绕过 probe
            self._e1e2 = eng
        return self._e1e2

    def disable_e1e2(self):
        """摘掉 E1E2 后端并把 R_RUN_UNIT 写回 0（保证后续 conv 走原路径）。"""
        if self._e1e2 is not None:
            try:
                self._e1e2.select(0, force=True)
            finally:
                self._e1e2 = None
        return None

    # ---------------------------------------------------------------- catpack
    def enable_catpack(self, verbose=None, arena=None, fclk_mhz=100.0):
        """挂上 `bin_catpack`（R_RUN_UNIT=5）后端（**默认不挂**，conv 路径不变）。

        先做硬件能力探测（写 5 读回、写 0 读回）：旧比特流的 0x200 页是保留页
        ⇒ 直接抛错，绝不"假装跑了 catpack"。
        """
        if self._catpack is None:
            from catpack import CatPackEngine
            eng = CatPackEngine(self, fclk_mhz=fclk_mhz, arena=arena,
                                verbose=self.verbose if verbose is None else verbose)
            eng.probe()
            self._catpack = eng
        return self._catpack

    def disable_catpack(self):
        """摘掉 catpack 后端并把 R_RUN_UNIT 写回 0。"""
        if self._catpack is not None:
            try:
                self._catpack.select(0, force=True)
            finally:
                self._catpack = None
        return None

    def catpack_stats(self):
        c = self._catpack
        if c is None:
            return None
        st = c.stats()
        st["fallback"] = int(self._catpack_fallback)
        return st


# ============================================================================
# 4.4 E1E2 打包单元（R_RUN_UNIT=4）：资产解析 + 驱动 + 单层 d=0 打包后端
# ============================================================================
# 两套用法，共用同一组寄存器/命令下发逻辑：
#
#   A. **全量程序模式**（交付物原样）：一次 session 把 61.15 MB arena 分配好，
#      desc.bin / thr.bin(+16 B 哨兵) 各搬一次；之后 run_layer()/run_all()
#      按 cmds.bin 的 698 条命令逐条下发。arena 的绝对地址与 frame.json 不同时
#      做整体平移（desc 的 src/thr 高低字、cmds 的 base_desc/base_out）。
#
#   B. **prologue 打包模式**（BIN_PACK=e1e2 的接缝）：给一层 int8 (cin,H,W) +
#      t_q，在内部建一个 d=0 的小程序（THR ≡ t_q，8 条 lane 指向连续 C8 缓冲），
#      跑 ceil(cin/64) 条命令，返回 `cb*H*W*8` 字节。d=0 时
#      `x + t_q > 0  ⟺  x >= 1-t_q`，与 C 内核 `bpack_i8(x_q, 1-t_q)` 逐字节相同。
#
# ⚠ 模式 A 的 per-edge d（425/805 段 d≠0）只有在"源 buffer 本来就是生产者标度的
#   int8"时才与 bin_net 的 C 内核路径等价；模式 B 不做 d 折叠，所有层都按
#   消费者标度 fs_in 打包，因此对任何层都与 C 内核逐字节相同。
E1E2_ARENA_BASE = 0x08000000
E1E2_DESC_STRIDE = 64
E1E2_SENTINEL_I16 = -127
E1E2_ACC_DEPTH = 256

E1E2_REG = dict(
    RUN_UNIT=0x200, E_GROUP=0x204,
    E_DESC=0x208, E_DESC_HI=0x20C,
    E_OUT=0x210, E_OUT_HI=0x214,
    DBG_RD=0x230, DBG_CYC=0x234, DBG_RDST=0x238,
    DBG_WRST=0x23C, DBG_WR=0x240,
)

_PROF = os.environ.get("BIN_PROFILE", "") not in ("", "0", "false", "False")
# E1E2 直读路径的 DBG 计数器：每条命令 5 次 MMIO 读（板端 ≈0.16 ms）纯属统计用
# ⇒ 默认不读；排查带宽时 E1E2_DBG=1 打开。
_E1E2_DBG = os.environ.get("E1E2_DBG", "") not in ("", "0", "false", "False")

CMDS_DTYPE = np.dtype([
    ("base_desc", "<u4"), ("base_out", "<u4"),
    ("cfg_c", "<u2"), ("cfg_h", "<u2"), ("cfg_w", "<u2"),
    ("cfg_group", "u1"), ("flags", "u1"),
])


def _e1e2_default_dir():
    env = os.environ.get("E1E2_DESC")
    cands = [env] if env else []
    for rel in (("..", "..", "deploy", "e1e2_desc"),
                ("..", "deploy", "e1e2_desc"),
                ("deploy", "e1e2_desc")):
        cands.append(os.path.abspath(os.path.join(HERE, *rel)))
    for p in cands:
        if p and os.path.exists(os.path.join(p, "frame.json")):
            return p
    return None


class E1E2Program:
    """`deploy/e1e2_desc/` 四个资产的只读解析 + 结构自检 + 整体平移。

    不依赖 pynq，本机可 import；所有地址都是 **arena 基址 + off** 的绝对物理地址。
    """

    def __init__(self, desc_dir=None, verbose=False):
        self.dir = desc_dir or _e1e2_default_dir()
        if self.dir is None:
            raise FileNotFoundError(
                "找不到 E1E2 描述符目录（deploy/e1e2_desc）；可用 E1E2_DESC 指定")
        self.verbose = verbose
        with open(os.path.join(self.dir, "frame.json"), "r", encoding="utf-8") as f:
            self.frame = json.load(f)
        self.desc = np.fromfile(os.path.join(self.dir, "desc.bin"), "<u8")
        self.cmds = np.fromfile(os.path.join(self.dir, "cmds.bin"), CMDS_DTYPE)
        self.thr = np.fromfile(os.path.join(self.dir, "thr.bin"), np.uint8)
        self.base = int(self.frame["arena"]["base"])
        self.size = int(self.frame["arena"]["size"])
        self.desc_off = int(self.frame["desc"]["off"])
        self.desc_bytes = int(self.frame["desc"]["bytes"])
        self.desc_pa = int(self.frame["desc"]["addr"])
        self.thr_off = int(self.frame["thr"]["bin_off"])
        self.thr_bytes = int(self.frame["thr"]["bin_bytes"])
        self.thr_pa = int(self.frame["thr"]["bin_addr"])
        self.sentinel_off = self.thr_off + self.thr_bytes
        self.sentinel_pa = self.base + self.sentinel_off
        self.ncmd = int(self.frame["desc"]["ncmd"])
        self.commands = list(self.frame["commands"])
        self.buffers = list(self.frame["buffers"])
        self.i8_buffers = [b for b in self.buffers if b.get("kind") == "int8"]
        self.packed_buffers = [b for b in self.buffers if b.get("kind") == "packed"]
        self._i8 = sorted(self.i8_buffers, key=lambda b: int(b["off"]))
        self._i8_off = [int(b["off"]) for b in self._i8]
        self.by_layer = {}
        for c in self.commands:
            self.by_layer.setdefault(c["layer"], []).append(c)
        self.n_sentinel_refs = 0
        self._validate()

    def _validate(self):
        if len(self.desc) != self.ncmd * 8:
            raise ValueError("desc.bin 字数 %d != %d×8" % (len(self.desc), self.ncmd))
        if len(self.cmds) != self.ncmd:
            raise ValueError("cmds.bin 条目 %d != %d" % (len(self.cmds), self.ncmd))
        if len(self.thr) != self.thr_bytes:
            raise ValueError("thr.bin 长度 %d != frame.thr.bin_bytes %d"
                             % (len(self.thr), self.thr_bytes))
        if self.thr_pa != self.base + self.thr_off:
            raise ValueError("thr.bin_addr 与 arena 布局不符")
        if self.desc_pa != self.base + self.desc_off:
            raise ValueError("desc.addr 与 arena 布局不符")
        # 哨兵必须紧挨 thr.bin 末尾（最后一条 thr 记录的 end）
        thr_end = max(int(b["off"]) + int(b["bytes"])
                      for b in self.buffers if b.get("kind") == "thr")
        if thr_end != self.sentinel_off:
            raise ValueError("thr.bin 末尾 %#x != 哨兵偏移 %#x"
                             % (thr_end, self.sentinel_off))
        sen = [b for b in self.buffers if b.get("kind") == "thr_const"]
        if not sen or int(sen[0]["off"]) != self.sentinel_off or int(sen[0]["bytes"]) != 16:
            raise ValueError("frame.buffers 里没有 16 B 的 thr_const 哨兵记录")
        # cmds.bin ↔ frame.commands 逐字段
        for c in self.commands:
            i = int(c["i"])
            r = self.cmds[i]
            if (int(r["base_desc"]) != self.desc_pa + i * E1E2_DESC_STRIDE
                    or int(r["base_out"]) != int(c["base_out"])
                    or int(r["cfg_c"]) != int(c["cfg_c"])
                    or int(r["cfg_h"]) != int(c["cfg_h"])
                    or int(r["cfg_w"]) != int(c["cfg_w"])
                    or int(r["cfg_group"]) != int(c["cfg_group"])):
                raise ValueError("cmds.bin[%d] 与 frame.commands 不符" % i)
        # 描述符结构：真实 lane 的 src 必须落在某个 int8 buffer；全掩码 lane 的
        # thr 必须精确指向哨兵（grp8 变通依赖它）。
        for c in self.commands:
            i = int(c["i"])
            words = self.desc[i * 8:(i + 1) * 8]
            for k in range(8):
                w = int(words[k])
                src, thr = w & 0xFFFFFFFF, (w >> 32) & 0xFFFFFFFF
                ch = int(c["group"]) * 64 + k * 8
                if ch >= int(c["cfg_c"]):
                    if thr != self.sentinel_pa:
                        raise ValueError("%s g%d lane%d 全掩码但 thr=%#x != 哨兵 %#x"
                                         % (c["layer"], c["group"], k, thr, self.sentinel_pa))
                    self.n_sentinel_refs += 1
                    continue
                b = self.buf_at(src)
                if b is None:
                    raise ValueError("%s g%d lane%d src=%#x 不在任何 int8 buffer"
                                     % (c["layer"], c["group"], k, src))
                lane_bytes = int(b["H"]) * int(b["W"]) * 8
                if not (self.base + int(b["off"]) <= src
                        and src + lane_bytes <= self.base + int(b["off"]) + int(b["bytes"])):
                    raise ValueError("%s g%d lane%d src=%#x 越过 buffer %s"
                                     % (c["layer"], c["group"], k, src, b["key"]))
                if not (self.thr_pa <= thr < self.sentinel_pa):
                    raise ValueError("%s g%d lane%d thr=%#x 不在 thr.bin"
                                     % (c["layer"], c["group"], k, thr))
        if self.verbose:
            print("[E1E2] 程序自检：%d 命令 / %d 层 / 哨兵引用 %d / arena %.2f MB"
                  % (self.ncmd, len(self.by_layer), self.n_sentinel_refs,
                     self.size / 1e6))

    def buf_at(self, addr):
        """地址 → int8 buffer 记录（二分；找不到返回 None）。"""
        import bisect
        off = int(addr) - self.base
        j = bisect.bisect_right(self._i8_off, off) - 1
        if j < 0:
            return None
        b = self._i8[j]
        if off < int(b["off"]) + int(b["bytes"]):
            return b
        return None

    def buffer(self, name):
        for b in self.buffers:
            if b.get("name") == name and b.get("kind") in ("int8", "packed"):
                return b
        raise KeyError("没有 buffer %r" % (name,))

    def relocate(self, delta):
        """整体平移 delta（字节，可正可负）。返回 (desc2, cmds2, sentinel_pa2)。"""
        delta = int(delta)
        lo = (self.desc & np.uint64(0xFFFFFFFF)).astype(np.int64) + delta
        hi = (self.desc >> np.uint64(32)).astype(np.int64) + delta
        if lo.size and (int(lo.min()) < 0 or int(lo.max()) > 0xFFFFFFFF):
            raise ValueError("平移后 src 超出 32 位")
        if hi.size and (int(hi.min()) < 0 or int(hi.max()) > 0xFFFFFFFF):
            raise ValueError("平移后 thr 超出 32 位")
        desc2 = ((hi.astype(np.uint64) << np.uint64(32))
                 | lo.astype(np.uint64))
        cmds2 = self.cmds.copy()
        for fld in ("base_desc", "base_out"):
            v = cmds2[fld].astype(np.int64) + delta
            if v.size and (int(v.min()) < 0 or int(v.max()) > 0xFFFFFFFF):
                raise ValueError("平移后 cmds.%s 超出 32 位" % fld)
            cmds2[fld] = v.astype("<u4")
        return desc2, cmds2, self.sentinel_pa + delta


class E1E2Engine:
    """`bin_e1e2`（R_RUN_UNIT=4）的 PS 驱动；**不改 conv 路径**。

    `core` 是一个 `BinConvEngine`：复用它的 IP 基址解析、MMIO 影子缓存、
    地址护栏（`_regions`）和 `start()/wait()`。E1E2 的寄存器全部在 0x200 页。

    典型用法（全量程序）：
        e = BinConvEngine(ov)
        ee = E1E2Engine(e); ee.probe()
        info = ee.load_program()              # 一次：分配 arena + 搬 127.5 KB
        ee.write_int8("encoder...conv", x_q)  # 按 frame.json 的 buffer 名填源
        ee.run_layer("encoder...conv")        # 或 ee.run_all()
        print(ee.dbg_summary(fclk_mhz=100))
    """

    def __init__(self, core, desc_dir=None, verbose=False, program=None,
                 fclk_mhz=100.0):
        self.core = core
        self.verbose = bool(verbose)
        self.program = program or E1E2Program(desc_dir=desc_dir, verbose=self.verbose)
        self.fclk_mhz = float(fclk_mhz)
        self.arena = None
        self.arena_pa = None
        self.desc_pa = None
        self.thr_pa = None
        self.sentinel_pa = None
        self._loaded = False
        self._delta = 0
        self._desc_reloc = None
        self._cmds_reloc = None
        self._probed = False
        self._pbuf = None
        self._pviews = None
        self._agg = dict(ncmd=0, seconds=0.0, rd=0, cyc=0, rdst=0, wrst=0, wr=0,
                         idle=0.0)
        self.last_run = None
        self.last_pack_stats = None
        self._regions_added = set()

    # ---------------------------------------------------------------- 能力
    def probe(self):
        """确认比特流里真的有 E1E2（0x200 页可写可读）。旧比特流会被拒绝。"""
        off = CFG + E1E2_REG["RUN_UNIT"]
        self.core._wr(off, 4, force=True)
        got4 = self.core._rd(off)
        self.core._wr(off, 0, force=True)
        got0 = self.core._rd(off)
        if (got4 & 7) != 4 or (got0 & 7) != 0:
            raise RuntimeError(
                "R_RUN_UNIT 写 4 读回 %#x、写 0 读回 %#x ⇒ 这个比特流没有 E1E2 "
                "（0x200 页仍是保留页）。请先合成/加载含 bin_e1e2 的 wrapper；"
                "驱动不会在没有 E1E2 的比特流上假装成功。" % (got4, got0))
        # 高字槽写 0：清掉可能残留的 r_e_hi_bad（否则 vcode 会 ERR_SIZE）
        self._wr_e("E_DESC_HI", 0, force=True)
        self._wr_e("E_OUT_HI", 0, force=True)
        self._probed = True
        return dict(write4=got4, back0=got0)

    def select(self, unit, force=False):
        unit = int(unit)
        if unit not in (0, 4):
            raise ValueError("R_RUN_UNIT 只能是 0(conv) 或 4(E1E2)，给的是 %d" % unit)
        self.core._wr(CFG + E1E2_REG["RUN_UNIT"], unit, force=force)
        return unit

    # ---------------------------------------------------------------- 全量程序
    def load_program(self, buf=None, force=False, zero=True):
        """分配/接管 arena，搬 desc.bin / thr.bin + 16 B 哨兵（一次 session 一次）。

        返回 info dict。若 `buf` 的物理基址不是 0x08000000，会把描述符里的
        src/thr 与命令的 base_desc/base_out 全部整体平移。
        """
        if self._loaded and not force:
            return dict(arena_pa=self.arena_pa, desc_pa=self.desc_pa,
                        thr_pa=self.thr_pa, sentinel_pa=self.sentinel_pa,
                        delta=self._delta, cached=True)
        p = self.program
        if buf is None:
            if _pynq_allocate is None:
                raise RuntimeError("没有 pynq：请显式传 buf（带 physical_address）")
            try:
                nbytes = (p.size + 4095) // 4096 * 4096
                buf = _pynq_allocate(shape=(nbytes,), dtype=np.uint8)
            except Exception as ex:                    # noqa: BLE001
                raise RuntimeError(
                    "分配 %.2f MB 连续 DDR 失败（%r）。1 GB 板子上这取决于 CMA "
                    "余量；可先 `echo 0 > /proc/sys/vm/compact_memory` 或换小分辨率/"
                    "改 E1E2Program 的 arena。" % (p.size / 1e6, ex))
        pa = int(getattr(buf, "physical_address", 0))
        if pa == 0:
            raise ValueError("arena 的 physical_address 为 0 ⇒ 拒绝（RTL 会 ERR_NULL）")
        if len(buf) < p.size:
            raise ValueError("arena 只有 %d B，程序需要 %d B" % (len(buf), p.size))
        delta = pa - p.base
        if delta & 0x7:
            raise ValueError("arena 基址 %#x 与 0x%08x 的差不是 8 的倍数"
                             % (pa, p.base))
        desc2, cmds2, sent2 = p.relocate(delta)
        t_copy = time.perf_counter()
        mv = _u8(buf)
        if zero:
            mv[p.desc_off:p.desc_off + p.desc_bytes] = 0
            mv[p.thr_off:p.sentinel_off + 16] = 0
        mv[p.desc_off:p.desc_off + p.desc_bytes] = desc2.view(np.uint8)
        mv[p.thr_off:p.thr_off + p.thr_bytes] = p.thr
        sen = np.full(8, E1E2_SENTINEL_I16, "<i2").tobytes()
        mv[self.program.sentinel_off:self.program.sentinel_off + 16] = \
            np.frombuffer(sen, np.uint8)
        _flush(buf)
        copy_ms = (time.perf_counter() - t_copy) * 1e3

        self.arena, self.arena_pa = buf, pa
        self._delta = delta
        self._desc_reloc, self._cmds_reloc = desc2, cmds2
        self.desc_pa = pa + p.desc_off
        self.thr_pa = pa + p.thr_off
        self.sentinel_pa = sent2
        n_sen = int((((desc2 >> np.uint64(32)) & np.uint64(0xFFFFFFFF))
                     == np.uint64(sent2)).sum())
        if n_sen != p.n_sentinel_refs:
            raise RuntimeError("平移后哨兵引用 %d != 期望 %d"
                               % (n_sen, p.n_sentinel_refs))
        if id(buf) not in self._regions_added:
            self.core.add_region(buf, "e1e2_arena")
            self._regions_added.add(id(buf))
        self._loaded = True
        copied = p.desc_bytes + p.thr_bytes + 16
        if self.verbose:
            print("[E1E2] 程序已搬入 DDR：arena %#x（%.2f MB，delta=%+#x），"
                  "desc@%#x thr@%#x 哨兵@%#x，共 %d B"
                  % (pa, p.size / 1e6, delta, self.desc_pa, self.thr_pa,
                     self.sentinel_pa, copied))
        return dict(arena_pa=pa, desc_pa=self.desc_pa, thr_pa=self.thr_pa,
                    sentinel_pa=self.sentinel_pa, delta=delta,
                    bytes_copied=copied, n_sentinel_refs=n_sen, cached=False,
                    copy_ms=copy_ms)

    def _require_loaded(self):
        if not self._loaded:
            raise RuntimeError("还没 load_program()：没有 arena/描述符表")
        return self.program

    def write_int8(self, name, x_q, flush=True):
        """把一层 int8 (C,H,W) 按 C8 布局写进它的源 buffer（名字来自 frame.json）。"""
        from e1e2_sim import to_c8_padded
        self._require_loaded()
        b = self.program.buffer(name)
        if b.get("kind") != "int8":
            raise ValueError("%s 不是 int8 buffer（kind=%s）" % (name, b.get("kind")))
        x = np.asarray(x_q, np.int8)
        if x.ndim != 3 or x.shape[0] != int(b["C"]):
            raise ValueError("%s 期望 (%d,H,W)，给的是 %s"
                             % (name, int(b["C"]), x.shape))
        raw = to_c8_padded(x, pad_to=int(b["C"]))
        if len(raw) != int(b["bytes"]):
            raise ValueError("C8 长度 %d != buffer %d" % (len(raw), int(b["bytes"])))
        mv = _u8(self.arena)
        mv[int(b["off"]):int(b["off"]) + len(raw)] = np.frombuffer(raw, np.uint8)
        if flush:
            _flush(self.arena)
        return int(b["off"])

    def arena_bytes(self, pa, nbytes):
        """arena 内 [pa, pa+nbytes) 的 uint8 视图（调用方自己 invalidate/flush）。"""
        self._require_loaded()
        off = int(pa) - self.arena_pa
        if off < 0 or off + int(nbytes) > len(self.arena):
            raise ValueError("地址 %#x+%d 不在 arena 内" % (pa, nbytes))
        return _u8(self.arena)[off:off + int(nbytes)]

    # ---------------------------------------------------------------- 下发
    def _wr_e(self, name, v, force=False):
        self.core._wr(CFG + E1E2_REG[name], v, force=force)

    def _wait_idle(self, timeout=None):
        """等单元 busy=0；**必须在写 AP_CTRL 之前**。

        RTL 的 start 写只在 `!run_active` 时才登记 `start_pend`（bin_unit_axi_wrap.v
        §2 的 8'h00 分支）：跑着的时候写 start 会被**直接丢掉，不排队**。
        如果这时直接 start + 轮询，PS 会把上一层 conv 的 done 当成本命令完成
        ⇒ 读到没写的输出（静默错数据）。所以这里显式等 idle，并把"上一跑残留
        错误码"抛出来，而不是把它覆盖掉。
        """
        t0 = time.time()
        n = 0
        while True:
            st = self.core._rd_reg("AP_RET")
            if not (st & 2):                       # busy=0
                err = (st >> 8) & 0xFF
                if err:
                    raise BinRunError(
                        err, msg="E1E2 启动前发现上一跑残留错误 %s(%d)"
                        "（AP_RETURN=%#010x）" % (decode_err(err)[1], err, st))
                return st
            dt = time.time() - t0
            if timeout is not None and dt > timeout:
                raise TimeoutError("E1E2 等 idle 超时 %.1fs（上一跑还在访存，"
                                   "不能发新 start）" % dt)
            n += 1
            if n < 4:
                pass
            elif n < 64:
                time.sleep(0)
            else:
                time.sleep(min(2e-4, 2e-6 * (n - 63)))

    def run_one(self, desc_pa, out_pa, cfg_c, cfg_h, cfg_w, cfg_group,
                timeout=None, wdt=None, count=True, skip_idle=False):
        """一条 E1E2 命令：写 5 个寄存器 → 等 idle → start → 等 done → 读错误码/DBG。"""
        core = self.core
        if timeout is None:
            hw = int(cfg_h) * int(cfg_w)
            cyc, left = 17, hw
            while left > 0:
                run = min(E1E2_ACC_DEPTH, left)
                cyc += 9 * run + 50
                left -= run
            timeout = max(5.0, min(600.0, cyc / 50e6 * 4.0 + 5.0))
        self.select(4)
        core._wr_reg("C", int(cfg_c))
        core._wr_reg("H", int(cfg_h))
        core._wr_reg("W", int(cfg_w))
        self._wr_e("E_GROUP", int(cfg_group))
        self._wr_e("E_DESC", int(desc_pa))
        self._wr_e("E_OUT", int(out_pa))
        if not skip_idle:
            # 同一 job 的后续组：上一组刚 wait 到 done ⇒ 本来就 idle，
            # 省掉一次轮询（板端 0.1~0.3 ms × 698 条）
            _tw = time.perf_counter()
            self._wait_idle(timeout=min(max(30.0, timeout), 120.0))
            self._agg["idle"] = self._agg.get("idle", 0.0) + (
                time.perf_counter() - _tw)
        core.start()
        st, dt = core.wait(timeout, wdt=wdt)
        err = (st >> 8) & 0xFF
        if err:
            raise BinRunError(err, msg="E1E2 命令报错 %s(%d)（desc=%#x out=%#x "
                               "c=%d %dx%d g=%d）"
                               % (decode_err(err)[1], err, desc_pa, out_pa,
                                  cfg_c, cfg_h, cfg_w, cfg_group))
        if (st >> 1) & 1:
            raise RuntimeError("E1E2 done=1 但 busy=1（AP_RETURN=%#010x）" % st)
        dbg = self._read_e_dbg() if count else {}
        if count:
            self._agg["ncmd"] += 1
            self._agg["seconds"] += dt
            for k in ("rd", "cyc", "rdst", "wrst", "wr"):
                self._agg[k] += int(dbg.get(k, 0))
        self.last_run = dict(status=st, seconds=dt, err=0, dbg=dbg,
                             desc_pa=desc_pa, out_pa=out_pa,
                             cfg_c=cfg_c, cfg_h=cfg_h, cfg_w=cfg_w,
                             cfg_group=cfg_group)
        return self.last_run

    def _read_e_dbg(self):
        return dict(
            rd=self.core._rd(CFG + E1E2_REG["DBG_RD"]),
            cyc=self.core._rd(CFG + E1E2_REG["DBG_CYC"]),
            rdst=self.core._rd(CFG + E1E2_REG["DBG_RDST"]),
            wrst=self.core._rd(CFG + E1E2_REG["DBG_WRST"]),
            wr=self.core._rd(CFG + E1E2_REG["DBG_WR"]),
        )

    def clear_e_dbg(self):
        """写 0x230 把 5 个计数器一起清（RTL：clr_e_dbg）。"""
        self._wr_e("DBG_RD", 0, force=True)

    def dbg_summary(self, fclk_mhz=None):
        """把累计的 DBG_E_* 换算成读带宽 / 冻结占比。"""
        f = float(self.fclk_mhz if fclk_mhz is None else fclk_mhz)
        a = dict(self._agg)
        cyc = a["cyc"]
        rd, wr = a["rd"], a["wr"]
        out = dict(ncmd=a["ncmd"], seconds=a["seconds"], cycles=cyc,
                   rd_beats=rd, wr_beats=wr,
                   read_bytes=rd * 8, write_bytes=wr * 8,
                   rd_per_cyc=(rd / cyc if cyc else 0.0),
                   rdst_frac=(a["rdst"] / cyc if cyc else 0.0),
                   wrst_frac=(a["wrst"] / cyc if cyc else 0.0),
                   read_bw_mbps=(rd * 8 * f / cyc if cyc else 0.0),
                   fclk_mhz=f, compute_ms=(cyc / f if cyc else 0.0))
        return out

    def _cmd_addrs(self, c):
        """命令 i 的 (desc_pa, out_pa)——直接用**平移后的 cmds.bin** 字段。"""
        i = int(c["i"])
        if self._cmds_reloc is not None:
            return (int(self._cmds_reloc["base_desc"][i]),
                    int(self._cmds_reloc["base_out"][i]))
        return (self.desc_pa + i * E1E2_DESC_STRIDE,
                self.arena_pa + int(c["out_off"]))

    def run_layer(self, name, restore=True, timeout=None, wdt=None, count=True):
        """跑一层的全部字组命令（按 frame.commands 的顺序）。"""
        self._require_loaded()
        if name not in self.program.by_layer:
            raise KeyError("frame.json 里没有层 %r" % (name,))
        out = []
        try:
            for c in self.program.by_layer[name]:
                dpa, opa = self._cmd_addrs(c)
                out.append(self.run_one(dpa, opa, int(c["cfg_c"]), int(c["cfg_h"]),
                                        int(c["cfg_w"]), int(c["cfg_group"]),
                                        timeout=timeout, wdt=wdt, count=count))
        finally:
            if restore:
                self.select(0, force=True)
        return out

    def run_all(self, restore=True, timeout=None, wdt=None, count=True,
                progress=0):
        """按 cmds.bin 的顺序跑完全部 698 条命令。"""
        self._require_loaded()
        out = []
        try:
            for c in self.program.commands:
                dpa, opa = self._cmd_addrs(c)
                out.append(self.run_one(dpa, opa, int(c["cfg_c"]), int(c["cfg_h"]),
                                        int(c["cfg_w"]), int(c["cfg_group"]),
                                        timeout=timeout, wdt=wdt, count=count))
                if progress and (len(out) % int(progress) == 0):
                    print("[E1E2] %d/%d 命令，%.3f s，累计读 %.2f MB"
                          % (len(out), self.program.ncmd,
                             self._agg["seconds"], self._agg["rd"] * 8 / 1e6),
                          flush=True)
        finally:
            if restore:
                self.select(0, force=True)
        return out

    # ---------------------------------------------------------------- 打包后端
    def _pack_views(self, need_in=0, need_out=0, need_thr=0, need_desc=0):
        """BIN_PACK=e1e2 用的小 arena：in / out / thr / desc 四块（一次分配）。

        默认尺寸取**交付命令表**的上限（`_pack_views()` 原行为）；调用方也可以
        额外要求更大的块（高分辨率/自建描述符时自动扩容）。
        """
        p = self.program
        # 用命令表算 padded C8 输入上限：cb*64*HW
        # ★ 2026-10-31：**只算一次**。板端 Python 的循环开销 ~1 µs/语句
        #   （A53 650 MHz），而这里每次 pack 都要扫 698 条命令 ⇒ 实测 11.9 ms/层，
        #   占了直读路径 2.7 s。命令表在整个 session 里不变 ⇒ 缓存到 self._pmax。
        pm = getattr(self, "_pmax", None)
        if pm is None:
            _i = _o = _t = _d = 0
            for c in p.commands:
                _cin = int(c["cfg_c"])
                _hw = int(c["cfg_h"]) * int(c["cfg_w"])
                _cb = (_cin + 63) // 64
                _i = max(_i, _cb * 64 * _hw)
                _o = max(_o, _cb * _hw * 8)
                _t = max(_t, _cb * 64 * 2)
                _d = max(_d, _cb * 64)
            pm = self._pmax = (_i, _o, _t, _d)
        need_in = max(need_in, pm[0])
        need_out = max(need_out, pm[1])
        need_thr = max(need_thr, pm[2])
        need_desc = max(need_desc, pm[3])

        def al(n):
            return (int(n) + 63) // 64 * 64

        need = al(need_in) + al(need_out) + al(need_thr) + al(need_desc)
        if self._pbuf is None or len(self._pbuf) < need:
            if _pynq_allocate is None:
                raise RuntimeError("没有 pynq：请给 pack_int8(bufs=...) 显式缓冲")
            buf = _pynq_allocate(shape=(need,), dtype=np.uint8)
            self.core.add_region(buf, "e1e2_pack")
            self._pbuf = buf
            mv = _u8(buf)
            o = 0
            views = []
            for n in (need_in, need_out, need_thr, need_desc):
                views.append(RegionBuffer(mv[o:o + n],
                                          int(buf.physical_address) + o, buf))
                o += al(n)
            self._pviews = views
            self._pn = (need_in, need_out, need_thr, need_desc)
        return self._pviews

    def pack_int8(self, x_q, t_q, H=None, W=None, bufs=None, restore=True,
                  timeout=None):
        """int8 (cin,H,W) + 逐通道 t_q → **d=0** 打包字节（cb*H*W*8）。

        `bufs` = dict(in=,out=,thr=,desc=)（离线测试/复用外部缓冲）；
        不给就用内部 PYNQ 小 arena。返回 uint8 只读视图，**在下一次 pack 前有效**。

        与 C 内核逐字节相同：`bit = x + t_q > 0 ⟺ x >= 1-t_q`。
        """
        x = np.asarray(x_q)
        if x.ndim == 3:
            cin, H, W = x.shape
        elif x.ndim == 2:
            if H is None or W is None:
                raise ValueError("二维输入必须显式给 H/W")
            cin = x.shape[0]
            if x.shape[1] != int(H) * int(W):
                raise ValueError("输入 %s 与 %dx%d 不符" % (x.shape, H, W))
        else:
            raise ValueError("x_q 必须是 (cin,H,W) 或 (cin,H*W)")
        cin, H, W = int(cin), int(H), int(W)
        hw = H * W
        cb = (cin + 63) // 64
        t = np.asarray(t_q, np.int64).reshape(-1)
        if t.size != cin:
            raise ValueError("t_q 长度 %d != cin %d" % (t.size, cin))
        if x.dtype != np.int8:
            x = np.clip(x, -128, 127).astype(np.int8)
        if bufs is None:
            vin, vout, vthr, vdesc = self._pack_views()
        elif isinstance(bufs, dict):
            vin, vout, vthr, vdesc = (bufs["in"], bufs["out"],
                                      bufs["thr"], bufs["desc"])
        else:
            vin, vout, vthr, vdesc = bufs
        need_in = cb * 64 * hw
        need_out = cb * hw * 8
        need_thr = cb * 64 * 2
        need_desc = cb * 64
        if len(vin) < need_in or len(vout) < need_out \
                or len(vthr) < need_thr or len(vdesc) < need_desc:
            raise ValueError("pack 缓冲太小（需要 in=%d out=%d thr=%d desc=%d）"
                             % (need_in, need_out, need_thr, need_desc))
        from e1e2_sim import to_c8_padded
        raw = np.frombuffer(to_c8_padded(x.reshape(cin, H, W), pad_to=cb * 64),
                            np.uint8)
        mv_in = _u8(vin)
        mv_in[:len(raw)] = raw
        if len(raw) < need_in:                      # C8 尾部补 0（全掩码 lane 会读到）
            mv_in[len(raw):need_in] = 0
        th = np.full(cb * 64, E1E2_SENTINEL_I16, "<i2")
        th[:cin] = t.astype("<i2")
        mv_thr = _u8(vthr)
        mv_thr[:need_thr] = th.view(np.uint8)
        in_pa = int(vin.physical_address)
        out_pa = int(vout.physical_address)
        thr_pa = int(vthr.physical_address)
        desc = np.zeros(cb * 8, "<u8")
        for g in range(cb):
            for k in range(8):
                ch = g * 64 + k * 8
                src = in_pa + (g * 8 + k) * hw * 8
                tp = thr_pa + ch * 2
                desc[g * 8 + k] = (np.uint64(tp) << np.uint64(32)) | np.uint64(src)
        mv_desc = _u8(vdesc)
        mv_desc[:need_desc] = desc.view(np.uint8)
        # 四个视图可能来自同一个大 buffer，也可能各自独立 ⇒ 都 flush 一次
        for v in (vin, vout, vthr, vdesc):
            _flush(v)
        stats0 = dict(self._agg)
        try:
            for g in range(cb):
                self.run_one(int(vdesc.physical_address) + g * 64,
                             out_pa + g * hw * 8, cin, H, W, min(g, 7),
                             timeout=timeout, count=True)
        finally:
            if restore:
                self.select(0, force=True)
        _invalidate(vout)
        data = np.frombuffer(vout, np.uint8)[:need_out]
        data.flags.writeable = False
        self.last_pack_stats = dict(cin=cin, H=H, W=W, cb=cb, cmds=cb,
                                    bytes=int(need_out), cin_pad=cb * 64,
                                    in_bytes=int(need_in))
        return data

    def prepare_segments(self, segs, L, t_q=None, bufs=None, plan=None):
        """**只做 PS 侧**：段表 → 阈值/描述符 + f32 段量化落进 vin。**不启动 PL**。

        返回一个 job（供 `run_prepared` 用），或 None（调用方回落）。
        这样"启 E1E2"可以推迟到 `_PlBackend.kick`——那时候上一层的 conv 已经
        收完（done 位没被 E1E2 顶掉），prologue 也只剩真正的 PS 工作。
        """
        cin, H, W = int(L.cin), int(L.H), int(L.W)
        hw = H * W
        cb = (cin + 63) // 64
        if t_q is None:
            if getattr(self.core, "host_blob", None) is None:
                raise RuntimeError("prepare_segments 需要 t_q 或 core.host_blob")
            t_q = self.core.host_blob.t_q(L.d)
        t = np.asarray(t_q, np.int64).reshape(-1)
        if t.size != cin:
            raise ValueError("t_q 长度 %d != cin %d" % (t.size, cin))
        lay = self._seg_layout(segs, cin, H, W)
        if lay is None:
            return None
        for (s_, off, C) in lay:
            if s_.get("kind") == "i8" and int(s_.get("pa") or 0) == 0:
                return None
        need_in = cb * 64 * hw
        _pf = {} if _PROF else None
        _t = time.perf_counter() if _PROF else 0.0
        if bufs is None:
            try:
                views = self._pack_views(need_in=need_in, need_out=cb * hw * 8,
                                         need_thr=cb * 64 * 2,
                                         need_desc=cb * 64)
            except Exception:                  # noqa: BLE001
                return None
        else:
            views = (bufs["in"], bufs["out"], bufs["thr"], bufs["desc"])
        if _pf is not None:
            _pf["views"] = time.perf_counter() - _t
            _t = time.perf_counter()
        vin, vout, vthr, vdesc = views
        vin_pa = int(getattr(vin, "physical_address", 0) or 0)
        thr_pa = int(getattr(vthr, "physical_address", 0) or 0)
        if vin_pa == 0 or thr_pa == 0:
            return None
        # f32 段：量化 + 按全局 C8 布局落进 vin（i8 段一个字节都不拷）
        fs_in = int(L.d["act_fs_in"])
        for (s_, off, C) in lay:
            if s_.get("kind") == "i8":
                continue
            data = s_.get("data")
            if data is None:
                return None
            x = np.asarray(data, np.float32)
            if x.shape != (C, H, W):
                raise ValueError("f32 段形状 %s != (%d,%d,%d)"
                                 % (x.shape, C, H, W))
            _tq = time.perf_counter() if _PROF else 0.0
            o = (off // C8) * hw * C8
            mv = np.frombuffer(_u8(vin), np.uint8)
            qm = _q8()
            done = False
            if qm is not None:
                # ★ 一趟完成"量化 + C8 转置"（q8_c8；板端 NEON 路径 ~200 MB/s）
                try:
                    got = qm.c8_into(x, fs_in, mv[o:o + C * hw], C, H, W)
                    done = got is not None
                except Exception:              # noqa: BLE001
                    done = False
            if not done:
                q = quantize_int8(x, fs_in).reshape(C, hw)
                cg = C // C8
                dst = mv[o:o + C * hw].reshape(cg, H, W, C8)
                np.copyto(dst, q.reshape(cg, C8, H, W).transpose(0, 2, 3, 1)
                          .view(np.uint8), casting="unsafe")
                del q
            del x
            _flush(vin)
            if _pf is not None:
                _pf["q8"] = _pf.get("q8", 0.0) + (time.perf_counter() - _tq)
                _pf["q8b"] = _pf.get("q8b", 0) + int(C * hw * 4)
                _pf["q8n"] = _pf.get("q8n", 0) + 1
        if _pf is not None:
            _pf["f32"] = time.perf_counter() - _t
            _t = time.perf_counter()
        # ★ 构建期预算好的 lane 计划（段类型一致时才用；不一致 ⇒ 现算）
        fast = None
        if plan is not None:
            kinds = tuple("i8" if s_.get("kind") == "i8" else "f32"
                          for s_, _o, _c in lay)
            if kinds == plan["kinds"]:
                fast = plan
        if fast is not None:
            thr = fast["thr"]
            lanes2 = fast["lanes2"]
            pas = np.asarray([int(s_["pa"]) if s_.get("kind") == "i8"
                              else int(vin_pa) for s_ in (x[0] for x in lay)],
                             np.int64)
            src = pas[fast["si"]] + fast["lane_off"]
            if fast["fill"] is not None:
                src = src[fast["fill"]]
            tp = (int(thr_pa) + lanes2).astype(np.uint64)
            descs = (tp << np.uint64(32)) | src.astype(np.uint64)
        else:
            thr, descs = self._build_desc(L, lay, t, cin, H, W, vin_pa, 0, thr_pa)
        if thr is None:
            return None
        if _pf is not None:
            _pf["desc"] = time.perf_counter() - _t
            self._prof_add(_pf)
        return dict(cin=cin, H=H, W=W, views=views, descs=descs, thr=thr,
                    need_in=need_in, n_f32=sum(1 for _s, _o, _c in lay
                                               if _s.get("kind") != "i8"))

    def run_prepared(self, job, out_buf=None, restore=True, timeout=None):
        """把 `prepare_segments` 造好的 job 启起来（同步等 done）。"""
        return self._run_desc(job["cin"], job["H"], job["W"], job["descs"], 0,
                              out_buf=out_buf, bufs=job["views"],
                              restore=restore, timeout=timeout,
                              thr_i16=job["thr"], need_in=job["need_in"])

    def pack_segments(self, segs, L, t_q=None, out_buf=None, bufs=None,
                      restore=True, timeout=None):
        """**多段源 → 一次 E1E2 打包**（cat 在物理上不存在；PS 不碰 int8 段）。

        segs : 依次拼接的段表，每段
                 dict(kind="i8", C=, H=, W=, fs=<该 buffer 的标度>, pa=<C8 基址>)
                 dict(kind="f32", C=, H=, W=, data=<(C,H,W) float32>)
        判据与旧路径**逐字节相同**：
               i8 段：`sat8(rint(x·2^(fs_in-fs))) + t_q > 0  ⟺  x + THR > 0`
               f32 段：`sat8(rint(x·2^fs_in)) + t_q > 0`
        拿不到 pa / 段不在 8 通道组上 / 没有 pynq 缓冲 ⇒ 返回 None（调用方回落）。
        """
        job = self.prepare_segments(segs, L, t_q=t_q, bufs=bufs)
        if job is None:
            return None
        return self.run_prepared(job, out_buf=out_buf, restore=restore,
                                 timeout=timeout)
    def pack_float(self, x_f, L, t_q=None, bufs=None):
        """`BinConvEngine.pack_input` 的 E1E2 后端：float → int8 → PL 打包。

        ★ 2026-10-31：量化改用 `bin_engine.quantize_int8`（q8.py 的 C 内核，
          逐字节自检；板端 140 ms → ~10 ms）。语义与原
          `e1e2_sim.quantize_int8` **逐字节相同**。
        """
        if t_q is None:
            if getattr(self.core, "host_blob", None) is None:
                raise RuntimeError("pack_float 需要 t_q 或 core.host_blob")
            t_q = self.core.host_blob.t_q(L.d)
        x = np.asarray(x_f, np.float32)
        if x.shape != (int(L.cin), int(L.H), int(L.W)):
            raise ValueError("输入形状 %s != (cin=%d,H=%d,W=%d)"
                             % (x.shape, L.cin, L.H, L.W))
        fs_in = int(L.d["act_fs_in"])
        return self.pack_int8(quantize_int8(x, fs_in), t_q,
                              H=int(L.H), W=int(L.W), bufs=bufs)

    # ---------------------------------------------------------------- C8 直读
    def _run_desc(self, cin, H, W, descs, out_pa, out_buf=None, bufs=None,
                  restore=True, timeout=None, thr_i16=None, need_in=0,
                  extra=None):
        """把 8×cb 个描述符写进内存 → 逐组下发 → 返回打包字节视图。

        所有 pack_* 共用这一段（寄存器序列/等待/收尾只有一份）。
        `out_buf` 给了就**直接写进调用方的块**（省掉 push_input 那次 memcpy）。
        """
        cb = (int(cin) + 63) // 64
        hw = int(H) * int(W)
        need_out = cb * hw * 8
        need_thr = cb * 64 * 2
        need_desc = cb * 64
        if bufs is None:
            vin, vout, vthr, vdesc = self._pack_views(
                need_in=need_in, need_out=need_out, need_thr=need_thr,
                need_desc=need_desc)
        elif isinstance(bufs, dict):
            vin, vout, vthr, vdesc = (bufs["in"], bufs["out"],
                                      bufs["thr"], bufs["desc"])
        else:
            vin, vout, vthr, vdesc = bufs
        if thr_i16 is not None:
            _u8(vthr)[:need_thr] = np.ascontiguousarray(
                thr_i16, np.int16).view(np.uint8)
        _u8(vdesc)[:need_desc] = np.ascontiguousarray(
            descs, np.uint64).view(np.uint8)
        if out_buf is None:
            ov = vout
            out_pa = int(vout.physical_address)
        else:
            ov = out_buf
            out_pa = int(getattr(out_buf, "physical_address", 0) or out_pa)
            if out_pa == 0:
                return None
            if len(ov) < need_out:
                raise ValueError("out_buf 只有 %d B，需要 %d B"
                                 % (len(ov), need_out))
        for v in (vthr, vdesc):
            _flush(v)
        try:
            for g in range(cb):
                self.run_one(int(vdesc.physical_address) + g * 64,
                             out_pa + g * hw * 8, int(cin), int(H), int(W),
                             min(g, 7), timeout=timeout, count=_E1E2_DBG,
                             skip_idle=(g > 0))
        finally:
            if restore:
                self.select(0, force=True)
        _invalidate(ov)
        data = np.frombuffer(_u8(ov), np.uint8)[:need_out]
        data.flags.writeable = False
        self.last_pack_stats = dict(cin=int(cin), H=int(H), W=int(W), cb=cb,
                                    cmds=cb, bytes=int(need_out),
                                    cin_pad=cb * 64, in_bytes=int(need_in),
                                    direct=bool(out_buf is not None),
                                    extra=extra or {})
        return data

    @staticmethod
    def _seg_layout(segs, cin, H, W):
        """校验段表并返回 [(seg, off, nbytes_per_plane)]；不合法 ⇒ None/抛错。"""
        off = 0
        out = []
        for s in segs:
            C = int(s["C"])
            if int(s["H"]) != int(H) or int(s["W"]) != int(W):
                raise ValueError("段分辨率 (%d,%d) != 层输入 (%d,%d)"
                                 % (s["H"], s["W"], H, W))
            if C <= 0:
                raise ValueError("段 C=%d 非法" % C)
            if off % C8:
                return None                    # 段边界不在 8 通道组上 ⇒ 一个 lane 跨段
            if C % C8:
                return None
            out.append((s, off, C))
            off += C
        if off != int(cin):
            raise ValueError("段通道数 Σ=%d != cin=%d" % (off, int(cin)))
        return out

    def _build_desc(self, L, lay, t_q, cin, H, W, vin_pa, vin_off_bytes,
                    thr_pa):
        """把段表展开成 (thr int16, desc uint64[8*cb]) —— **全向量化**。

        板端每个小 numpy 调用 ~30-50 µs（A53 650 MHz）⇒ 逐段/逐 lane 的 Python
        循环会吃掉几 ms/层。这里用 `searchsorted` 把"lane → 段"一次算出来，
        与段数无关（本网最多 11 段）。
        """
        fs_in = int(L.d["act_fs_in"])
        hw = int(H) * int(W)
        cb = (int(cin) + 63) // 64
        cin = int(cin)
        t = np.asarray(t_q, np.int64).reshape(-1)
        # 逐通道 d：i8 段 = fs_in - 该 buffer 的标度；f32 段已量化到 fs_in ⇒ 0
        _ds = np.asarray([0 if s_.get("kind") != "i8"
                          else fs_in - int(s_.get("fs", fs_in))
                          for s_, _o, _c in lay], np.int64)
        d_ch = np.repeat(_ds, [int(c_) for _s, _o, c_ in lay])
        thr = np.full(cb * 64, E1E2_SENTINEL_I16, np.int16)
        thr[:cin] = closed_form_thr_vec(d_ch, t)
        # 逐 lane（8 通道一组）：lane 起点通道 = 8k
        lanes = np.arange(cb * 8, dtype=np.int64) * 8
        valid = lanes < cin
        offs = np.asarray([int(o) for _s, o, _c in lay], np.int64)
        goff = np.asarray([1 if s_.get("kind") == "i8"
                           else 0 for s_, _o, _c in lay], np.int64)
        pas = np.asarray([int(s_.get("pa") or 0) if s_.get("kind") == "i8"
                          else int(vin_pa) for s_, _o, _c in lay], np.int64)
        si = np.searchsorted(offs, lanes, side="right") - 1
        si[si < 0] = 0
        seg_off = offs[si]
        src = pas[si] + ((lanes - seg_off) // C8) * hw * C8
        # f32 段的 base 是 vin_pa、off 是 0 ⇒ 上面用的是 lanes//8，等价
        src = np.where(goff[si] > 0,
                       pas[si] + ((lanes - seg_off) // C8) * hw * C8,
                       int(vin_pa) + (lanes // C8) * hw * C8)
        if not valid.all():
            # 被掩掉的 lane（ch ≥ cin）沿用上一条合法 lane 的地址（RTL 仍会发读，
            # thr 是哨兵 ⇒ 结果恒 0；地址必须合法，不能是 0）
            idx = np.where(valid, np.arange(cb * 8), 0)
            np.maximum.accumulate(idx, out=idx)
            src = src[idx]
        if valid.any() and int((src[valid] & 7).sum()):
            raise ValueError("lane src 不是 8 字节对齐（cin=%d）" % cin)
        tp = (int(thr_pa) + lanes * 2).astype(np.uint64)
        descs = (tp << np.uint64(32)) | src.astype(np.uint64)
        return thr, descs

    def pack_c8(self, c8, L, t_q=None, out_buf=None, bufs=None, restore=True,
                timeout=None):
        """**单源 C8 直读**：conv 的 OUT 缓冲 → 打包字节（不反量化、不量化）。

        `c8` : `C8Tensor`（或任何有 `.shape/.pa/.fs_out` 的同类）。
        返回 uint8 只读视图（cb*H*W*8），在下一次 pack 前有效；做不到 ⇒ None。
        """
        if t_q is None:
            if getattr(self.core, "host_blob", None) is None:
                raise RuntimeError("pack_c8 需要 t_q 或 core.host_blob")
            t_q = self.core.host_blob.t_q(L.d)
        return self.pack_segments([dict(kind="i8", C=int(L.cin), H=int(L.H),
                                        W=int(L.W), fs=int(c8.fs_out),
                                        pa=int(c8.pa))],
                                  L, t_q=t_q, out_buf=out_buf, bufs=bufs,
                                  restore=restore, timeout=timeout)

    def _prof_add(self, ph):
        d = getattr(self, "_prof", None)
        if d is None:
            d = self._prof = {}
        for k, v in ph.items():
            d[k] = d.get(k, 0.0) + float(v)


# ============================================================================
# 4.5 快速 PS 内核（**逐字节等价**，板端实测 3.8×；见 docstring 里的证明）
# ============================================================================
# 为什么要有这一段：整网 64×64 的 PS 时间是 4.5 s，其中 **3.3 s 是
# `sat8(rint(x*2^fs)) → binarize_pack(x,t_q)`**（perf_probe 实测：大层 132+174 ms），
# 而它一点都没有和 PL 重叠的余地 —— 它必须等上一层 PL 的 done。
# 所以"把 PS 藏进 PL"最有效的一步不是流水，而是**把这段本身砍掉 3.8×**。
#
# 等价性证明（不是"近似"）：
#   令 v = x·2^fs（浮点乘法，2^fs 是 2 的幂 ⇒ **在 float32 里也是精确的**，
#   与原来 float64 算出来的值逐位相同），y = rint(v)（round-half-to-even）。
#   原判据是  bits = (sat8(y) + t > 0)。
#   记 k = 1-t（整数）。则
#       sat8(y) + t > 0  ⟺  sat8(y) ≥ k
#       sat8 是 clamp 到 [-128,127]，单调 ⇒ 当 k ≤ -128 恒真、k > 127 恒假；
#       本容器 t_q 实测 ∈ [-20,13]（227 层 40304 个通道全查过，见 audit 自检），
#       因此 k ∈ [-12,21] ⊂ (-128,127] ⇒ **clamp 恒为恒等**，判据退化成 y ≥ k。
#   又 y ≥ k ⟺ v > k-0.5（含 tie 时的 half-even 修正）：rint(k-0.5) 取偶邻居，
#   故 k 偶 ⇒ 等号算真（用 v > nextafter(k-0.5) 实现 `>=`），k 奇 ⇒ 等号算假。
#   ⇒ 整个"量化 + clip + 加 t + 比较"塌缩成 **一次 `x > adj` 比较**，
#      其中 adj 是逐通道常量（每层预先算好、缓存）。
#   若容器里出现 t_q < -126 或 > 128 的通道，`fast_ps_ok()` 返回 False，
#   本模块自动退回 `bin_ref_numpy.binarize_pack` 的慢路径（正确性优先）。
FAST_PS = True          # 置 False 可强制走参考实现（A/B 与排查用）


def pack_thresh(t_q, fs_in):
    """逐通道比较阈值（float32）：`rint(x*2^fs) >= 1-t` ⟺ `x > adj`。

    返回 (adj, ok)；ok=False 表示该层有通道落在 clamp 区间外、不能用快路径。
    """
    t = np.asarray(t_q, np.int64)
    if t.size == 0:
        return None, False
    if int(t.min()) < -126 or int(t.max()) > 128:
        return None, False
    k = 1 - t                                   # 需要 y >= k
    thr = ((k - 0.5) * (2.0 ** -float(fs_in))).astype(np.float32)
    # half-even：k 偶 ⇒ 等号算真 ⇒ 用"严格大于前一个可表示数"实现 >=
    adj = np.where((k % 2) == 0,
                   np.nextafter(thr, np.float32(-np.inf)), thr).astype(np.float32)
    return np.ascontiguousarray(adj), True


def quant_pack_ref(x_f, adj, out_dtype=np.uint64):
    """**优化前的实现**（保留做 A/B 对照与回退；语义/逐字节与下面新版相同）。"""
    x = np.asarray(x_f, np.float32)
    cin, H, W = x.shape
    hw = H * W
    cb = (cin + 63) // 64
    nby = cb * 8
    # 1) 比较直接写到 (hw,cin) 连续内存 ⇒ 把"转置"和"比较"合成一趟
    bitsT = np.greater(x.reshape(cin, hw).T, adj)          # (hw,cin) bool, C 连续
    # 2) 沿通道轴按 8 位打包：byte j 的第 i 位 = 通道 8j+i
    bb = np.packbits(bitsT, axis=-1, bitorder="little")    # (hw, ceil(cin/8))
    if bb.shape[1] != nby:                                 # 补零通道（cb*64 > cin）
        bbp = np.zeros((hw, nby), np.uint8)
        bbp[:, :bb.shape[1]] = bb
        bb = bbp
    bb = np.ascontiguousarray(bb)
    # 3) 每像素 8 字节 = 一个 64-bit 字（小端）⇒ 直接 view，再转到 (cb,hw)
    return np.ascontiguousarray(bb.view(np.uint64).T).reshape(cb, H, W)


def quant_pack(x_f, adj, out_dtype=np.uint64):
    """float32 (cin,H,W) + 逐通道阈值 → 二值打包字 (cb,H,W)。

    逐字节等价于 `binarize_pack(sat8(rint(float64(x)*2^fs)), t_q)`；
    实测 3.8×（A53）：一次比较 + packbits + 一次小转置。

    ★ 2026-10-31 再优化（**输出逐字节相同**，见 runs/ps256/REPORT.md 的 A/B）：
      1. 有 C 内核（`bpack.c`：首次使用现场编译 + 逐字节自检，失败静默回退）⇒ 直通。
         它把"比较 + 组字节 + 组 64-bit 字"融成一趟顺序读 x，中间不落临时数组。
         `BIN_PACK=numpy` 可强制走 numpy 版本。
      2. numpy 回退版的关键是**把 packbits 的输入变成 C 连续**：
         旧代码 `np.greater(x.reshape(cin,hw).T, adj)` 的输入是 F 连续的，而 numpy ufunc
         默认 order='K' ⇒ 输出的 `bitsT` 是 **F 序**；`packbits(axis=-1)` 要求最后一维连续，
         于是它自己先做一次整份转置拷贝（实测：cin=544/128² 时 F 序 20.9 ms vs C 序 0.35 ms
         = **60×**）。新版先按自然布局比较、再显式 `ascontiguousarray(bits.T)`，
         主机 1.87×，并且补零块只在必要时才建。
    语义仍是：byte j 的第 i 位 = 通道 8j+i，补零通道恒 0，字按小端拼。
    """
    x = np.asarray(x_f, np.float32)
    mod = _bpack()
    if mod is not None:
        out = _bpack_pack_f32(mod, x, adj)
        if out is not None:                  # C 内核不可用 ⇒ 下面走 numpy（绝不出 None）
            return out
    cin, H, W = x.shape
    hw = H * W
    cb = (cin + 63) // 64
    nby = cb * 8
    # 1) 自然布局比较：读 x 连续，写 bool 连续
    bits = np.greater(x.reshape(cin, hw), adj.reshape(cin, 1))   # (cin,hw) C 连续
    # 2) 转置 1 B/元素（比"跨步读 4 B/元素"便宜），再沿通道轴按 8 位打包
    bitsT = np.ascontiguousarray(bits.T).view(np.uint8)          # (hw,cin)
    bb = np.packbits(bitsT, axis=-1, bitorder="little")          # (hw, ceil(cin/8))
    if bb.shape[1] != nby:                                       # 补零通道
        bbp = np.zeros((hw, nby), np.uint8)
        bbp[:, :bb.shape[1]] = bb
        bb = bbp
    # 3) 每像素 8 字节 = 一个 64-bit 字（小端）⇒ view，再转到 (cb,hw)
    return np.ascontiguousarray(bb.view(np.uint64).T).reshape(cb, H, W)


_BPACK_STATE = {"tried": False, "mod": None}


def _bpack():
    """惰性加载 C 内核（**只尝试一次**，结果缓存）；任何异常都只让这一层退回 numpy。"""
    st = _BPACK_STATE
    if not st["tried"]:
        st["tried"] = True
        try:
            import bpack as _bp
            _bp.load()
            st["mod"] = _bp if _bp._STATE.get("lib") is not None else None
        except Exception:                    # noqa: BLE001
            st["mod"] = None
    return st["mod"]


def _bpack_pack_f32(mod, x3, adj):
    out = mod.pack_f32(x3, adj)
    return out



# ★ 2026-10-31（epilogue）：C8 还原 + 反量化的 C 内核（c8deq.py，逐位自检 + 自动回落）。
#   板端实测：原 numpy 单趟 `np.multiply(跨步视图, ...)` 在**非缓存 DMA 缓冲**上要 5.45 s
#   （1 字节/次的标量读）；C 内核 + 一次 burst memcpy 只要 0.97 s。见 runs/epi256/REPORT.md。
_c8deq = None


def _deq_kernel():
    global _c8deq
    if _c8deq is None:
        try:
            from c8deq import deq as _f
            _c8deq = _f
        except Exception:                       # noqa: BLE001
            _c8deq = False
    return _c8deq or None


def from_c8_deq(raw, C, H, W, fs_out, dequant=True):
    """C8 交错裸字节 → (C,H,W) float32（= `from_c8(...).astype(f32)/2^fs_out`）。

    原实现是 `for c in range(C)` 的逐通道 Python 循环（C 最大 544 ⇒ 544 次 numpy 调用）；
    这里换成一次 strided 拷贝。逐字节等价（除 2 的幂是精确的，乘倒数同值）。

    ★ 2026-10-31：再合掉两趟。
      · `raw` 现在可以是 **uint8 视图**（不再是 `.tobytes()` 的 bytes），省一次整份拷贝。
      · 反量化用 `np.multiply(int8 视图, k, out=f32 缓冲)`：numpy 的 ufunc 缓冲是分块
        (8 KB) 的，所以"cast 到 float32"与"乘 2^-fs"在同一次遍历里完成，
        不再有 `astype` 产出的那份中间 float32 数组（41 M 元素 → 165 MB）。
        位精确：int8→float32 转换精确，乘的 2^-fs 是 2 的幂 ⇒ 乘积精确，
        与"先 astype 再乘"逐位相同。
    """
    cg = (C + C8 - 1) // C8
    n = cg * H * W * C8
    if dequant:
        k = _deq_kernel()
        if k is not None:
            y = k(raw, C, H, W, int(fs_out))
            if y is not None:
                return y[:C]
    a = np.frombuffer(raw, np.int8, count=n).reshape(cg, H, W, C8)
    o = a.transpose(0, 3, 1, 2)                    # (cg,8,H,W)：通道 c ↔ (c//8, c%8)
    if dequant:
        buf = np.empty((cg, C8, H, W), np.float32)
        np.multiply(o, np.float32(1.0 / float(1 << int(fs_out))), out=buf,
                    casting="unsafe")
        return buf.reshape(cg * C8, H, W)[:C]
    return np.ascontiguousarray(o).reshape(cg * C8, H, W)[:C]


def from_c8_deq_into(raw, C, H, W, fs_out, dst):
    """同 `from_c8_deq`，但**写进调用方的 f32 缓冲**（f32 池的槽），不新分配。

    `dst` : 可写、C 连续、dtype=float32、元素数 ≥ cg*8*H*W 的一维/任意形状数组。
    返回 : `dst` 的 (C,H,W) 视图（**与 `from_c8_deq` 逐位相同**）；失败返回 None。

    两条实现与 `from_c8_deq` 一一对应（位精确同一论证）：
      · C 内核 `c8deq.deq_into`（BIN_DEQ=auto，默认）
      · numpy `np.multiply(跨步 int8 视图, 2^-fs, out=槽)`（BIN_DEQ=numpy / 内核不可用）
    """
    if dst is None or not isinstance(dst, np.ndarray) or dst.dtype != np.float32:
        return None
    if not dst.flags["C_CONTIGUOUS"]:
        return None
    cg = (C + C8 - 1) // C8
    n = cg * H * W * C8
    if dst.size < n:
        return None
    d = dst[:n].reshape(cg, C8, H, W)
    if _deq_kernel() is not None:
        try:
            from c8deq import deq_into as _into
            got = _into(raw, C, H, W, int(fs_out), dst[:n])
            if got is not None:
                return d.reshape(cg * C8, H, W)[:C]
        except Exception:                  # noqa: BLE001
            pass
    a = np.frombuffer(raw, np.int8, count=n).reshape(cg, H, W, C8)
    o = a.transpose(0, 3, 1, 2)
    np.multiply(o, np.float32(1.0 / float(1 << int(fs_out))), out=d,
                casting="unsafe")
    return d.reshape(cg * C8, H, W)[:C]


# ============================================================================
# 4.6 ★ C8 直读：conv OUT 的惰性 int8 句柄 + 闭式阈值（**不猜布局，见反推**）
# ============================================================================
# 布局反推（`bin_ref_numpy.to_c8/from_c8` 与 `from_c8_deq` 是**互逆**的一对）：
#
#     to_c8:   out = np.zeros((cg, H, W, C8));  out[c//8, :, :, c%8] = x[c]
#     from_c8: a = raw.reshape(cg, H, W, C8);   x[c] = a[c//8, :, :, c%8]
#
#   ⇒ 内存下标 = ((c//8)*H + y)*W*8 + x*8 + (c%8)
#   ⇒ **组主序**：先 C/8 个组（每组 H*W*8 B 连续），组内**像素主序**，
#     每个像素 8 字节 = 8 个连续通道（通道号 c%8 是**最快变化**的维，
#     即"8 通道交错"而不是"8 组交错"）。
#   ⇒ `from_c8_deq` 里 `transpose(0,3,1,2)` 正是把 (cg,H,W,8)→(cg,8,H,W)。
#
# 这条与 E1E2 描述符的 `src = base + (ch//8)*H*W*8`（ch 为 8 的倍数）**逐字节一致**：
# 一个 lane 读 `H*W` 个连续的 64-bit 字，就是该 8 通道组的整块 C8。
# 验证：`ps/tools/verify_e1e2_direct.py`（227 层逐字节）与 `bin_engine --selftest`
# 里的 golden `expected_c8.bin` 比对。


class C8Tensor:
    """conv OUT 缓冲的**惰性 int8 C8 句柄**：`read_out(lazy=True)` 的返回值。

    为什么要有它：一条 conv→conv / conv→cat→conv 的边，f32 中间量是**纯浪费**。
    旧路径 `int8 → f32（read_out，1.03 s）→ 量化（140 ms/层）→ int8` 绕了一圈；
    这里直接保留 int8 C8 + 物理地址，PL 侧 E1E2 从**原缓冲**读（PS 不碰激活）。

    生命周期（重要）：
      · 句柄"钉住"它所在的 OUT 槽；`release()` 一次把槽还回 `_PlBackend` 的空闲表。
      · `nuse`（= 会用到它的 conv 层数）在构建图时算好；每个消费者 prepare 完
        release 一次。归零 ⇒ 槽可复用。
      · `materialize()` = 旧 `read_out` 的 f32 反量化（消费者真要 f32 时）。
      · `__array__` 也走 `materialize()`：**没适配的 numpy 消费者自动拿到正确的
        f32**，不会静默错数据（这是安全网，不是快路径）。
    """

    __slots__ = ("buf", "C", "H", "W", "fs_out", "engine", "layer",
                 "_f32", "_on_release", "_rel", "nref")

    def __init__(self, buf, C, H, W, fs_out, engine=None, layer=None,
                 on_release=None, nref=1):
        self.buf = buf
        self.C, self.H, self.W = int(C), int(H), int(W)
        self.fs_out = int(fs_out)
        self.engine = engine
        self.layer = layer
        self._f32 = None
        self._on_release = on_release
        self._rel = False
        self.nref = int(nref)          # 还有多少层 conv 要读它（release 计数）

    # ---- 形状/地址 ----
    @property
    def shape(self):
        return (self.C, self.H, self.W)

    @property
    def dtype(self):
        return np.float32 if self._f32 is not None else np.int8

    @property
    def out_bytes(self):
        return ((self.C + C8 - 1) // C8) * self.H * self.W * C8

    @property
    def pa(self):
        """C8 缓冲的物理地址；已物化（无 int8 缓冲）⇒ 0。"""
        if self._f32 is not None:
            return 0
        return int(getattr(self.buf, "physical_address", 0) or 0)

    @property
    def is_int8(self):
        return self._f32 is None and self.pa != 0

    def raw(self):
        """C8 裸字节视图（只读；live 时有效）。"""
        return np.frombuffer(self.buf, np.uint8)[:self.out_bytes]

    # ---- 消费 ----
    def materialize(self):
        """反量化成 f32（= 旧 `read_out` 的结果），并立刻释放 OUT 槽。"""
        if self._f32 is None:
            self._f32 = self.engine.read_out(self.buf, self.layer, lazy=False)
        self.release(force=True)
        return self._f32

    def release(self, force=False):
        """消费一次；引用计数归零（或 force）才把钉住的 OUT 槽还回去。

        `nref` = 会读它的 conv 层数（构建期算好）；**不是**"第一次用完就还"——
        一个 rdb/ghost 的叶子会被 2~10 层 conv 通过嵌套 cat 反复读。
        """
        if self._rel:
            return False
        if not force:
            self.nref -= 1
            if self.nref > 0:
                return False
        self._rel = True
        cb = self._on_release
        self._on_release = None
        if cb is not None:
            try:
                cb()
            except Exception:                 # noqa: BLE001
                pass
        return True

    def __array__(self, dtype=None, copy=None):
        y = self.materialize()
        if dtype is None or np.dtype(dtype) == np.float32:
            return y
        return y.astype(dtype)

    def __repr__(self):
        return "C8Tensor(%s cin=%d %dx%d fs=%d %s%s)" % (
            "i8" if self.is_int8 else "f32", self.C, self.H, self.W, self.fs_out,
            "pa=%#x" % self.pa if self.is_int8 else "materialized",
            "" if self._rel else " pinned")


# ============================================================================
# 4.7 闭式阈值：`sat8(rint(x·2^d)) + t_q > 0  ⟺  x + THR > 0`（x ∈ [-128,127]）
# ============================================================================
def closed_form_thr(d, tq):
    """单通道版（与 `ps/tools/gen_e1e2_desc.closed_form_thr` 逐字一致）。"""
    d, tq = int(d), int(tq)
    need = 1 - tq                       # z >= need 才为 1
    if need <= -128:                    # z 的最小值是 -128 ⇒ 恒 1
        T = -129
    elif need > 127:                    # z 的最大值是 127 ⇒ 恒 0
        T = 127
    elif d >= 0:
        s = 1 << d
        T = -((-need) // s) - 1         # z = clamp(x*s) 单调增 ⇒ 闭式
    else:
        k = -d
        x_t = need * (1 << k) - (1 << (k - 1))
        lo = x_t + 1 if (need & 1) else x_t   # rint 是 round-half-even
        T = lo - 1
    T = min(max(T, -129), 127)
    return -T


def closed_form_thr_vec(d, tq):
    """向量化版：`d` 可为标量或与 `tq` 同形的 int 数组；返回 int16 THR。"""
    tq = np.asarray(tq, np.int64)
    need = 1 - tq
    d = np.broadcast_to(np.asarray(d, np.int64), need.shape)
    T = np.zeros(need.shape, np.int64)
    pos = d >= 0
    if pos.any():
        s = np.left_shift(np.int64(1), d[pos])
        T[pos] = -((-need[pos]) // s) - 1
    neg = ~pos
    if neg.any():
        k = -d[neg]
        p = np.left_shift(np.int64(1), k)
        x_t = need[neg] * p - (p >> 1)
        T[neg] = x_t + (need[neg] & 1) - 1
    T = np.where(need <= -128, -129, np.where(need > 127, 127, T))
    T = np.clip(T, -129, 127)
    return (-T).astype(np.int16)


# ★ 2026-10-31：量化（float32 → int8）的 C 内核（q8.py，逐位自检 + 自动回落）。
#   旧实现（e1e2_sim.quantize_int8）用 isfinite + 布尔花式索引 + float64 升格，
#   板端 544×24² 实测 140 ms；C 内核一趟顺序循环 ~10 ms（**输出逐字节相同**）。
_Q8 = None


def _q8():
    global _Q8
    if _Q8 is None:
        try:
            import q8 as _m
            _Q8 = _m
        except Exception:                   # noqa: BLE001
            _Q8 = False
    return _Q8 or None


def quantize_int8(x_f, fs_in, out=None):
    """float32 → int8：`sat8(rint(x·2^fs))`，NaN→−128、+inf→127、−inf→−128。

    与 `e1e2_sim.quantize_int8` **逐字节相同**（q8.py 自检覆盖全部 256 个 int8
    取值 × fs 0..12 × 平局点/±inf/NaN/次正规数）。
    """
    m = _q8()
    if m is not None:
        return m.quant(x_f, fs_in, out=out)
    x = np.asarray(x_f, np.float32)
    v = np.multiply(x, np.float32(1 << int(fs_in)), dtype=np.float32)
    np.rint(v, out=v)
    np.clip(v, np.float32(-128.0), np.float32(127.0), out=v)
    q = v.astype(np.int8) if out is None else np.copyto(out, v.astype(np.int8)) or out
    bad = np.isnan(x)
    if bad.any():
        q[bad] = -128
    return q


class RunStats:
    __slots__ = ("status", "seconds", "err_code", "layer", "hout", "wout", "dbg")

    def __init__(self, status, seconds, err_code, layer, hout, wout, dbg):
        self.status, self.seconds, self.err_code = status, seconds, err_code
        self.layer, self.hout, self.wout, self.dbg = layer, hout, wout, dbg

    def __iter__(self):
        return iter((self.status, self.seconds, (self.hout, self.wout)))

    def __repr__(self):
        return ("RunStats(done=%d err=%d %.3f ms %dx%d dbg=%s)"
                % (self.status & 1, self.err_code, self.seconds * 1e3,
                   self.hout, self.wout, self.dbg))


def _flush(buf):
    try:
        buf.flush()
    except Exception:                     # noqa: BLE001
        pass


def _invalidate(buf):
    try:
        buf.invalidate()
    except Exception:                     # noqa: BLE001
        pass


# ============================================================================
# 5. 本机自检：用"寄存器级 RTL 模型"跑一遍驱动（不需要板子/pynq）
# ============================================================================
class FakeUnit:
    """**寄存器级**的 bin_unit_axi_wrap(UNIT=0) 模型，只为本机验证驱动逻辑。

    它照抄 RTL §2/§3 的行为：
      · 配置页写/读、A[16] 分页、保留页读 0；
      · 只认识 hi_bit_of() 列的 8 个高字槽（对 zeta/gamma/slope 写 +4 会覆盖下一个基址）；
      · start 的校验顺序与 vcode（ERR_NULL/SIZE/SHIFT/LAYOUT/ALIGN/UNIT）；
      · 校验失败也置 done=1 + err_code（否则 PS 会永远轮询）；
      · 通过校验后按 bin_ref_numpy.conv_rtl 的语义"访存"（能抓越界/搬错）。
    这**不是** RTL 验证（那是 xsim 的事），只证明驱动写的寄存器序列、地址算式、
    金丝雀/超时/错误码处理是自洽的。
    """

    class _MMIO:
        def __init__(self, unit):
            self.u = unit

        def write(self, off, v):
            self.u.write(off, v)

        def read(self, off):
            return self.u.read(off)

    def __init__(self, base_pa=0x4006_0000, name="bin_unit_0"):
        self.name = name
        # 与 pynq.Overlay.ip_dict 同样的形状（驱动只认 phys_addr / mem_range_size）
        self.ip_dict = {name: dict(phys_addr=int(base_pa),
                                   mem_range_size=0x20000,
                                   type="xilinx.com:user:bin_unit_axi_wrap_bd:1.0")}
        self.mmio = FakeUnit._MMIO(self)
        self.reg = {}
        self.hi_bad = 0
        self.done = 0
        self.err = 0
        self.busy = 0
        self.run_active = 0
        self.r_ctrl = 0
        self.mem = {}                     # pa → np buffer
        self.ops = 0
        self.base_pa = base_pa

    def __getattr__(self, k):
        """让 `overlay.bin_unit_0` 也返回自己（pynq 的 IP 对象就是这么挂的）。"""
        if k.startswith("bin_unit"):
            return self
        raise AttributeError(k)

    # ---- 存储映射（把假物理地址指到 numpy 缓冲上）----
    def map_mem(self, pa, buf):
        self.mem[int(pa)] = buf

    def _find(self, pa, n=1):
        for lo, b in self.mem.items():
            if lo <= pa and pa + n <= lo + len(b):
                return b, pa - lo
        raise AssertionError("模型越界访存：pa=%#x 不在任何映射区（%s）"
                             % (pa, ", ".join("%#x+%d" % (k, len(v))
                                              for k, v in self.mem.items())))

    # ---- 寄存器读写 ----
    def write(self, off, v):
        off, v = int(off), int(v) & 0xFFFFFFFF
        page, o = (off >= CFG), (off - CFG) if off >= CFG else off
        o &= 0xFF
        if not page:
            return                        # 保留页：写忽略
        hi = HIGH_WORD_BY_OFF.get(o)
        if hi is not None:
            if v:
                self.hi_bad |= hi
            else:
                self.hi_bad &= ~hi
        if o == 0x00:
            self.r_ctrl = v
            if (v & 1) and not self.run_active:
                self.done, self.err = 0, 0
                self._try_start()
            return
        if o in (0xE0, 0xE8, 0xF0):
            return
        self.reg[o] = v

    def read(self, off):
        off = int(off)
        page, o = (off >= CFG), (off - CFG) if off >= CFG else off
        o &= 0xFF
        if not page:
            return 0
        if o == 0x00:
            return (self.r_ctrl >> 1 & 1) << 1
        if o == 0x10:
            return ((self.err & 0xFF) << 8) | (self.busy << 1) | self.done
        if o == 0xF0:
            return ((self.busy << 3) | ((self.hi_bad != 0) & 1))
        if o in (0xE0, 0xE8):
            return 0
        v = self.reg.get(o)
        if v is not None:
            return v
        if o == 0x68:
            return ALPHA_SHIFT
        if o == 0x6C:
            return SLOPE_SHIFT
        return 0

    # ---- 启动前校验（照抄 RTL §3 的顺序与条件）----
    def _vcode(self):
        g = lambda o: int(self.reg.get(o, 0))
        if self.hi_bad:
            return 2
        if g(0x20) == 0 or g(0x20) > HW_MAX or g(0x28) == 0 or g(0x28) > HW_MAX:
            return 2
        if g(0x18) == 0 or g(0x18) > C_MAX:
            return 2
        if g(0x60) > 63:
            return 3
        if g(0x18) % 8:                   # CHK_C8_ALL=1
            return 4
        need = (0x80, 0xA0, 0xD8, 0xB0, 0xB8, 0xBC, 0xC0, 0xD0)
        if any(g(o) == 0 for o in need):
            return 1
        for o in (0x80, 0xA0, 0xD8, 0xB0, 0xB8, 0xBC, 0xC0):
            if g(o) & 7:
                return 5
        if g(0x70) == 0 or g(0x70) > COUT_LIM:
            return 2
        if g(0x40) == 0:
            return 2
        if g(0x48) == 0 or g(0x50) == 0:
            return 2
        cb = (g(0x18) + 63) // 64
        if cb * g(0x40) * g(0x40) > WBUF_WORDS:
            return 2
        if g(0x68) != ALPHA_SHIFT or g(0x6C) != SLOPE_SHIFT:
            return 3
        return 0

    def _try_start(self):
        code = self._vcode()
        if code:
            self.done, self.err = 1, code
            return
        self.err = 0
        self.run_active = 1
        self.busy = 1
        try:
            self._execute()
        finally:
            self.run_active = 0
            self.busy = 0
            self.done = 1
        self.ops += 1

    def _execute(self):
        g = lambda o: int(self.reg.get(o, 0))
        cin, cout, k, s, d, pad = g(0x18), g(0x70), g(0x40), g(0x48), g(0x50), g(0x58)
        H, W, shift, cb = g(0x20), g(0x28), g(0x60), (g(0x18) + 63) // 64
        Ho, Wo = out_hw(H, W, s)
        # ---- 读激活 ----
        base = g(0x80)
        b, off = self._find(base, cb * H * W * 8)
        A = np.frombuffer(b[off:off + cb * H * W * 8].tobytes(), "<u8").reshape(cb, H, W)
        # ---- 读参数 / 权重 ----
        def rd_int(pa, n, dt):
            bb, oo = self._find(pa, n * np.dtype(dt).itemsize)
            return np.frombuffer(bb[oo:oo + n * np.dtype(dt).itemsize].tobytes(),
                                 dt).astype(np.int64)
        P = dict(alpha_q=rd_int(g(0xD8), cout, "<i2"),
                 bias_q=rd_int(g(0xB0), cout, "<i4"),
                 zeta_q=rd_int(g(0xB8), cout, "<i4"),
                 gamma_q=rd_int(g(0xBC), cout, "<i4"),
                 slope_q=rd_int(g(0xC0), cout, "<i2"))
        K = cb * k * k
        wp = g(0xA0)
        wb, wo = self._find(wp, cout * K * 8)
        P["words"] = np.frombuffer(wb[wo:wo + cout * K * 8].tobytes(),
                                   "<u8").reshape(cout, k * k, cb)
        from bin_ref_numpy import conv_rtl
        out, _ = conv_rtl(cin, cout, k, s, d, pad, shift, A, P)
        # ---- 按 C8 写回（这就是 bin_conv_core 的写口）----
        ob, oo = self._find(g(0xD0), ((cout + 7) // 8) * Ho * Wo * 8)
        blob = to_c8(out)
        ob[oo:oo + len(blob)] = np.frombuffer(blob, np.uint8)


# 高字槽偏移 → hi_bad 位图里的位（照抄 RTL hi_bit_of()）
HIGH_WORD_BY_OFF = {0x84: 0x01, 0x8C: 0x02, 0x94: 0x04, 0x9C: 0x08,
                    0xA4: 0x10, 0xB4: 0x20, 0xD4: 0x40, 0xDC: 0x80}


# ============================================================================
# 6. 自检入口（本机，不需要板子 / pynq）
# ============================================================================
class MemBuf(np.ndarray):
    """带"物理地址"的 numpy 缓冲：模拟 `pynq.allocate()` 的返回值（自检用）。"""

    def __new__(cls, nbytes, base_pa, zero=True):
        n = (int(nbytes) + 4095) // 4096 * 4096
        obj = np.zeros(n, np.uint8).view(cls) if zero else np.empty(n, np.uint8).view(cls)
        obj.physical_address = int(base_pa)
        return obj

    def flush(self):
        pass

    def invalidate(self):
        pass


def _synth_layer(words, prm, meta, name="golden"):
    """把一个 golden 层摆成"容器里的一段连续 blob"。实现共用 bin_ref_numpy。"""
    from bin_ref_numpy import synth_layer_blob
    blob, L = synth_layer_blob(name, meta, words, prm["alpha"], prm["bias"],
                               prm["zeta"], prm["gamma"], prm["slope"])
    return np.frombuffer(blob, np.uint8), {k: L[k] for k in L if k.startswith("off_")}, L


def _selftest(pack_dir=None, verbose=True):
    """无板自检：FakeUnit（寄存器级 RTL 模型）+ 真容器 + 真 xsim golden。"""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, os.pardir, os.pardir))
    pack_dir = pack_dir or os.path.join(root, "deploy", "bin_weights_new")
    gdir = os.path.join(root, "pl", "rtl_hand", "golden")
    fails = []

    def chk(cond, msg):
        if verbose:
            print("   %s %s" % ("\u2713" if cond else "\u2717", msg))
        if not cond:
            fails.append(msg)

    def fresh(pa=0x4006_0000):
        ov = FakeUnit(pa)
        e = BinConvEngine(ov, unit_id=0, pack_dir=pack_dir, verbose=False)
        pool = MemBuf(1 << 18, 0x1800_0000 + (fresh.n * 0x10_0000))
        fresh.n += 1
        ov.map_mem(int(pool.physical_address), pool)
        e.add_region(pool, "pool")
        return ov, e, pool
    fresh.n = 0

    if verbose:
        print("== bin_engine 自检（FakeUnit：寄存器级 RTL 模型，不是 RTL 验证）==")

    # ---------------- 1) 基址解析 / 配置页读写 ----------------
    ov, e, pool = fresh()
    chk(e.base_pa == ov.base_pa and e.range_size >= CFG + 0x100,
        "从 ip_dict 取到基址 %#x（窗口 %#x）" % (e.base_pa, e.range_size))
    bl = Blob(pack_dir)
    L0 = BinLayer(bl.layer(0), 8, 8)
    e.config(L0)
    ok, bad = e.config_check(L0)
    chk(ok, "配置页 14 个字段回读一致")
    if not ok:
        print("     不一致：", bad)

    # ---------------- 2) 错误码（PS 侧护栏 + RTL 模型）----------------
    def err_of(fn, want, tag):
        try:
            fn()
        except BinRunError as ex:
            chk(ex.code == want, "%s ⇒ %s(%d)" % (tag, ex.name, ex.code))
            return
        except ValueError as ex:
            chk(False, "%s: 抛的是 PS 护栏 ValueError，不是 RTL 错误码（%s）" % (tag, ex))
            return
        chk(False, "%s: 期望 %s(%d) 却没抛" % (tag, ERR_NAME[want], want))

    # PS 护栏先挡住：8 字节不对齐的基址根本发不出去
    try:
        e.set_base("IN", int(pool.physical_address) + 4)
        chk(False, "PS 护栏没拦住非 8 对齐基址")
    except ValueError:
        chk(True, "PS 护栏：base_in 非 8 对齐 ⇒ ValueError（不发给 PL）")
    BASE = int(pool.physical_address)

    def good_bases(ee, L):
        """摆一组合法基址（都 8 对齐、非 0），好在它上面做单点扰动。"""
        ee.set_base("IN", BASE + 0x1000)
        ee.set_base("WGT", BASE + 0x2000)
        ee.set_base("ALPHA", BASE + 0x4000)
        ee.set_base("BIAS", BASE + 0x4800)
        ee.set_base("ZETA", BASE + 0x5000)
        ee.set_base("GAMMA", BASE + 0x5800)
        ee.set_base("SLOPE", BASE + 0x6000)
        ee.set_base("OUT", BASE + 0x8000)

    # ERR_ALIGN：其余 7 个基址都合法，只把 B_IN 写成 +4
    e.config(L0)
    good_bases(e, L0)
    e._wr_reg("B_IN", BASE + 0x1004)
    err_of(lambda: e.start_and_check(0.5), 5, "RTL 模型：base_in 非 8 对齐")
    # ERR_NULL：一个基址都不写（复位后全 0）
    ov, e, pool = fresh()
    BASE = int(pool.physical_address)
    e.config(L0)
    err_of(lambda: e.start_and_check(0.5), 1, "RTL 模型：基址全 0")
    # ERR_SHIFT：alpha_shift 改成 8
    e.config(L0)
    good_bases(e, L0)
    e._wr_reg("ALPHA_SHIFT", 8)
    err_of(lambda: e.start_and_check(0.5), 3, "RTL 模型：alpha_shift=8")
    # ERR_LAYOUT：cin 不是 8 的倍数
    e.config(L0)
    good_bases(e, L0)
    e._wr_reg("C", 52)
    err_of(lambda: e.start_and_check(0.5), 4, "RTL 模型：cin=52")
    # ERR_SIZE：B_IN 高字写非 0（r_hi_bad ⇒ ERR_SIZE）
    e.config(L0)
    good_bases(e, L0)
    e._wr(CFG + 0x84, 1)
    err_of(lambda: e.start_and_check(0.5), 2, "RTL 模型：B_IN 高字非 0")
    # 写 0 能清掉 hi_bad（正字诀：高字槽写 0 是合法的）
    e._wr(CFG + 0x84, 0)
    chk(e._rd(CFG + 0xF0) & 1 == 0, "高字写 0 能清 sticky（DBG_FLAG[0]）")

    # ---------------- 3) 金丝雀 / 尺寸护栏 ----------------
    ob = pool[0:8192]
    BinGuard.fill_canary(ob)
    try:
        BinGuard.check_canary(ob, len(ob) - CANARY_BYTES + 1, "x")
        chk(False, "金丝雀：需求超界没被拦住")
    except ValueError:
        chk(True, "金丝雀：输出需求超过可写区 ⇒ 拒绝启动")
    ob[-1] = 0
    try:
        BinGuard.check_canary(ob, len(ob) - CANARY_BYTES, "x")
        chk(False, "金丝雀：尾部被写坏没被发现")
    except RuntimeError:
        chk(True, "金丝雀：尾部被写坏 ⇒ 报越界")
    BinGuard.fill_canary(ob)

    # ---------------- 4) 整层执行（真 xsim golden 逐字节）----------------
    NAMES = ("bin_1x32_32_8x8", "bin_1x64_16x16", "bin_3x32_48_8x8",
             "bin_3x32_48_d2", "bin_3x96_64_8x8")
    for nm in NAMES:
        d = os.path.join(gdir, nm)
        meta = json.load(open(os.path.join(d, "meta.json")))
        words = np.fromfile(os.path.join(d, "wgt_pack.bin"), "<u8")
        prm = dict(alpha=np.fromfile(os.path.join(d, "alpha.bin"), "<i2"),
                   bias=np.fromfile(os.path.join(d, "bias.bin"), "<i4"),
                   zeta=np.fromfile(os.path.join(d, "zeta.bin"), "<i4"),
                   gamma=np.fromfile(os.path.join(d, "gamma.bin"), "<i4"),
                   slope=np.fromfile(os.path.join(d, "slope.bin"), "<i2"))
        blob_bytes, off, Ld = _synth_layer(words, prm, meta, nm)
        ov, e, pool = fresh()
        H, W, cin = int(meta["H"]), int(meta["W"]), int(meta["cin"])
        cb = int(meta["cb"])
        Ho, Wo = out_hw(H, W, int(meta["s"]))
        nout = ((int(meta["cout"]) + 7) // 8) * Ho * Wo * 8
        A = np.fromfile(os.path.join(d, "act_pack.bin"), "<u8")
        ib = MemBuf(cb * H * W * 8 + 64, 0x1A00_0000 + fresh.n * 0x10_0000)
        ob = MemBuf(nout + CANARY_BYTES + 64, 0x1A10_0000 + fresh.n * 0x10_0000)
        bb = MemBuf(len(blob_bytes) + 64, 0x1A20_0000 + fresh.n * 0x10_0000)
        fresh.n += 1
        for b in (ib, ob, bb):
            ov.map_mem(int(b.physical_address), b)
        ib[:A.nbytes] = np.frombuffer(A.tobytes(), np.uint8)
        bb[:len(blob_bytes)] = blob_bytes
        e.blob, e.blob_pa = bb, int(bb.physical_address)   # 自检：直接接管
        e.add_region(bb, "blob")
        Lg = BinLayer(Ld, H, W)
        rs = e.run(ib, ob, L=Lg, timeout=5.0, verify_canary=True)
        exp = np.fromfile(os.path.join(d, "expected_c8.bin"), np.int8)
        got = np.frombuffer(bytes(ob[:len(exp)]), np.int8)
        nbad = int((got != exp).sum())
        chk(nbad == 0, "%-18s cin=%3d cout=%3d k=%d cb=%d  %d/%d 字节一致"
            % (nm, cin, int(meta["cout"]), int(meta["k"]), cb, len(exp) - nbad, len(exp)))

    # ---------------- 5) f32 输出池（BIN_F32POOL）----------------
    #    判据与任务书一致：池开/池关**逐位相同**、PA 真指向数据、槽满能回落不崩。
    from types import SimpleNamespace as _NS

    def _mkL(cout, Ho, Wo, fs, name="pooltest"):
        cg = (cout + 7) // 8
        return _NS(cout=cout, Hout=Ho, Wout=Wo, name=name, out_bytes=cg * 8 * Ho * Wo,
                   d={"act_fs_out": fs})

    def _pool_fresh(shapes, nslots=2):
        pa = 0x1B00_0000
        max_out = max((s[0] + 7) // 8 * 8 * s[1] * s[2] for s in shapes)
        slot = max(((s[0] + 7) // 8) * 8 * s[1] * s[2] * 4 for s in shapes)
        ov2, e2, _ = fresh()
        ar2 = FakeArena(max_out + 65536 + nslots * slot, base_pa=pa, unit=ov2)
        ob2, _ = ar2.region(max_out + 4096)
        r2 = np.random.default_rng(4242)
        ob2[:] = r2.integers(0, 256, len(ob2), dtype=np.uint8)
        e2.add_region(ar2.buf, "arena")
        return e2, ar2, ob2, slot

    shapes = [(64, 32, 32, 3), (24, 16, 16, 0), (20, 8, 8, 7), (544, 4, 4, 5)]
    e2, ar2, ob2, slot0 = _pool_fresh(shapes, nslots=2)
    want = [e2.read_out(ob2, _mkL(*s)) for s in shapes]      # 池关 = 原路径
    pool = e2.enable_f32_pool(ar2, nslots=2, slot_bytes=slot0)
    got = [e2.read_out(ob2, _mkL(*s)) for s in shapes]       # 槽满：第 3 个回落
    same = all(a.view(np.uint32).tobytes() == b.view(np.uint32).tobytes()
               for a, b in zip(want, got))
    chk(same, "f32 池：%d 层池开/池关逐位相同（含 C%%8!=0 的 cout=20）" % len(shapes))
    chk(not got[0].flags.writeable and not got[1].flags.writeable,
        "f32 池：返回的是只读视图（writeable=False）")
    _w = True
    try:
        got[0][0, 0, 0] = np.float32(1.0)     # 消费者原地写必须**报错**，不能静默改槽
    except ValueError:
        _w = False
    chk(not _w, "f32 池：消费者视图真的不可写（原地写 ⇒ ValueError，不会静默错数据）")
    pa0 = int(getattr(got[0], "physical_address", -1))
    seg = f32_segments(got[0])
    okseg = (len(seg) == 1 and seg[0][0] == pa0
             and seg[0][1:] == tuple(got[0].shape)
             and ar2.mv[pa0 - ar2.pa: pa0 - ar2.pa + got[0].nbytes].tobytes()
             == got[0].view(np.uint8).ravel().tobytes())
    chk(okseg, "f32 池：f32_segments() 给出的 PA 真指向该张量（%s）" % (seg,))
    chk(getattr(got[2], "_f32_slot", None) is None,
        "f32 池：槽满 ⇒ 回落宿主 np.empty（不崩，值仍一致）")
    st0 = pool.stats()
    chk(st0["hits"] == 2 and st0["misses"] >= 1,
        "f32 池：命中 %d / 回落 %d（2 槽 × %d 层）"
        % (st0["hits"], st0["misses"], len(shapes)))
    rel1 = f32_release(got[0])
    rel2 = f32_release(got[0])                # 幂等：第二次必须是 False
    chk(rel1 and not rel2, "f32 池：归还幂等（第一次 True / 第二次 False）")
    got[0] = None
    got[1] = None
    del got
    import gc
    gc.collect()
    chk(pool.stats()["free"] == pool.nslots,
        "f32 池：张量判死后槽全部回收（free=%d/%d）"
        % (pool.stats()["free"], pool.nslots))
    chk(f32_segments(want[0]) == [],
        "f32 池：宿主 numpy 张量 f32_segments() 返回 []（catpack 必须回退）")
    e2.disable_f32_pool()
    chk(e2._f32pool is None, "f32 池：disable_f32_pool() 之后 read_out 回到 np.empty")

    # ---------------- 7) C8 直读（惰性 int8 句柄 / 闭式阈值 / 量化内核） ----------------
    # 7a. 闭式阈值：向量化版必须与逐通道标量版逐位相同
    _tq = np.arange(-140, 141, dtype=np.int64)
    _bad = 0
    for _d in range(-10, 11):
        _bad += int((closed_form_thr_vec(_d, _tq)
                     != np.asarray([closed_form_thr(_d, int(v)) for v in _tq],
                                   np.int16)).sum())
    chk(_bad == 0, "闭式阈值：向量化 == 标量（5901 组 t_q×d，差异 %d）" % _bad)

    # 7b. 量化内核：必须与 e1e2_sim 的参考实现逐字节相同（含 NaN/±inf/平局点）
    try:
        from e1e2_sim import quantize_int8 as _quant_ref
        _nbadq = 0
        for _fs in (0, 1, 2, 3, 5, 7, 10, 12):
            _b = np.arange(-128, 128, dtype=np.float32) / np.float32(1 << _fs)
            _e = [np.float32(v) / np.float32(1 << _fs) for v in
                  (-128.0, -1.0, 0.0, 1.0, 127.0, 128.0)]
            _e += [np.nextafter(_e[0], np.float32(-np.inf)),
                   np.nextafter(_e[0], np.float32(np.inf)),
                   np.float32(np.nan), np.float32(np.inf),
                   np.float32(-np.inf), np.float32(1e-40)]
            _x = np.concatenate([_b, np.asarray(_e, np.float32)])
            _nbadq += int((quantize_int8(_x, _fs) != _quant_ref(_x, _fs)).sum())
        chk(_nbadq == 0, "量化内核 quantize_int8 == e1e2_sim 参考（8 个 fs，"
                         "全 256 取值+NaN/±inf/平局点，差异 %d）" % _nbadq)
    except Exception as _ex:                      # noqa: BLE001
        chk(False, "量化内核自检异常：%r" % _ex)

    # 7c. C8 布局：from_c8(to_c8(x)) 必须还原（= 驱动/E1E2 共用的那条公式）
    try:
        from bin_ref_numpy import to_c8, from_c8
        _x8 = np.random.default_rng(7).integers(-128, 128, (40, 5, 7), dtype=np.int8)
        chk(np.array_equal(from_c8(to_c8(_x8), 40, 5, 7), _x8),
            "C8 布局：from_c8(to_c8(x)) == x（(40,5,7)，组主序/像素主序/8 通道最快）")
    except Exception as _ex:                      # noqa: BLE001
        chk(False, "C8 布局自检异常：%r" % _ex)

    # 7d. 惰性句柄：read_out(lazy=True) 不反量化、pa 指向 OUT 缓冲、
    #     materialize() 与普通 read_out 逐位相同、release 按 nref 计数
    _rel = []
    ov7, e7, _ = fresh()
    ar7 = FakeArena(1 << 16, base_pa=0x1D00_0000, unit=ov7)
    ob7, ob7_pa = ar7.region(0x8000)
    _r7 = np.random.default_rng(99)
    ob7[:] = _r7.integers(0, 256, len(ob7), dtype=np.uint8)
    e7.add_region(ar7.buf, "lazy")
    _L7 = _mkL(40, 16, 24, 5, "lazytest")
    _h = e7.read_out(ob7, _L7, lazy=True, on_release=lambda: _rel.append(1), nref=2)
    chk(isinstance(_h, C8Tensor) and _h.is_int8,
        "惰性句柄：read_out(lazy=True) 返回未反量化的 C8Tensor")
    chk(_h.pa == ob7_pa, "惰性句柄：pa 就是 OUT 缓冲的物理地址（%#x）" % _h.pa)
    chk(np.array_equal(_h.raw(), np.frombuffer(ob7, np.uint8)[:len(_h.raw())]),
        "惰性句柄：raw() 就是 OUT 的 C8 字节（零拷贝）")
    _want7 = e7.read_out(ob7, _L7)
    _got7 = _h.materialize()
    chk(_want7.view(np.uint32).tobytes() == _got7.view(np.uint32).tobytes(),
        "惰性句柄：materialize() == 旧 read_out 逐位相同（fs=5，C%8!=0）")
    chk(len(_rel) == 1 and not _h.is_int8,
        "惰性句柄：materialize() 强制释放槽（回调 1 次）")
    _h2 = e7.read_out(ob7, _L7, lazy=True, on_release=lambda: _rel.append(2), nref=3)
    chk((_h2.release() is False) and (_h2.release() is False)
        and (_h2.release() is True) and len(_rel) == 2,
        "惰性句柄：release 按 nref=3 计数，第 3 次才还槽")
    chk(np.asarray(_h2).shape == (40, 16, 24),
        "惰性句柄：np.asarray(句柄) 自动物化（未适配的消费者拿到正确 f32）")

    # ---------------- 6) 错误码解码 ----------------
    for code in range(0, 7):
        name, short, hint = decode_err(code)[1:4]
        chk(name == ERR_NAME[code] and short, "错误码 %d → %s（带中文说明）" % (code, name))

    print("\n%s" % ("自检全部通过 \u2713" if not fails
                    else "自检失败 %d 项：%s" % (len(fails), fails)))
    return 0 if not fails else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--selftest":
        return _selftest(argv[1] if len(argv) > 1 else None)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
