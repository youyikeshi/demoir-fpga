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
import shutil
import struct
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
        # ★ v50（16-lane / PRM 帧 32 字）：分组参数是从 DDR 的**逐 oc 连续数组**读的，
        #   每组读 VLANES 个 oc。cout 不是 VLANES 的倍数 ⇒ 最后一组会读进**相邻数组**
        #   ⇒ 静默错数据（16-lane 上板硬前提）。默认 VLANES=8 且本网 cout∈{32,48,64,
        #   96,192} 全是 8/16 的倍数 ⇒ 下面这句在默认配置下一个字节都不动。
        _v = vlanes_now()
        if _v > C8 and self.cout % _v:
            raise ValueError(
                "%s: cout=%d 不是 VLANES=%d 的倍数 ⇒ 最后一组 PRM 会读进相邻参数数组"
                "（契约 §5-② 的反面：RTL 的 cout%%8 校验不许收紧，但 PS 必须保证"
                "cout%%VLANES==0 才能安全上 16 lane）" % (self.name, self.cout, _v))

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
        # ★ T-23：MMIO fast-path 状态（默认 None ⇒ 用原路径；见 _mmio_fast_init）
        self._fast = None                 # None=尚未探测 / False=不可用 / dict=可用
        self._fast_why = ""               # 回落原因（只打印一次）
        self._poll_mode = _MMIO_POLL_ENV  # "" | "tight" | "est"
        # est 模式：睡 k × 解析拍数。默认 0.55 是**安全值**——`E1E2Engine.fclk_mhz`
        # 历史上默认 100 MHz（真板 150），est 可能偏大 1.5×，0.55×1.5=0.83<1 不会睡过；
        # 若调用方确认 fclk 正确（探针的 --fclk 会设进去），可 `BIN_MMIO_PRED_K=0.9`
        # 把读次数压到最低（见 runs/t23_mmio/REPORT.md 的投影表）。
        try:
            self._pred_k = min(1.0, max(0.2, float(os.environ.get("BIN_MMIO_PRED_K",
                                                                 "0.55"))))
        except Exception:                                  # noqa: BLE001
            self._pred_k = 0.55
        self._pred_note = ""              # 最近一次 est 轮询的自述（探针/日志用）
        self._pred_done = None            # conv 异步路径：预计完成时刻（time.time）
        self._pred_est = 0.0              # 该次预测的 est_s
        self._rd_us = None                # 实测"一次 MMIO 读"要多少秒（est 模式用）
        self._wr_merged = 0               # 合并成一次 pack_into 的字数（统计）
        self._fast_w = 0                  # 走 fast-path 的写次数（统计）
        self._fast_r = 0                  # 走 fast-path 的读次数（统计）
        self._c_cmds = 0                  # 走 C 路径的命令数（统计）
        # ★ T-23 C1 判据用的诊断钩子：给了 list 就把每一次"逻辑访问"记进去
        #   (kind, off, val, force, sent) —— sent=False 表示被影子缓存跳过。
        #   三个 arm 的记录必须逐条相同（见 ps/tools/verify_t23_mmio.py）。
        self._trace_log = None
        # ★ v50 描述符链状态（默认全空 ⇒ v5 路径一个字节都不受影响）：
        #   _v50_chain  = 正在飞的链（None = 空闲）；非 None 时 kick() 直接拒绝
        #   _v50_poison = 上一次链报错/超时的原因（非 None ⇒ 禁止再提交）
        #   _v50_ok     = BAND_ROWS 探针结论（None = 未探过）
        self._v50_chain = None
        self._v50_poison = None
        self._v50_ok = None
        self._v50_why = "尚未探测"
        self._v50_stats = dict(chains=0, descs=0, seconds=0.0, progress=0)
        self.mmio_fast_init()             # 三个开关都没开时立即返回（几乎零成本）

    # ------------------------------------------------------------ T-23 MMIO
    def mmio_fast_init(self):
        """探测一次底层 buffer + C 驱动，返回 self._fast（dict 或 False）。

        fast-path 的取法（按优先级）：
          1. `self.mmio.mem`（pynq 3.x 的 MMIO.mem 是 memoryview）；
          2. `self.mmio.mmap`（pynq 2.x/3.x 的 MMIO.mmap 是 mmap 对象）；
          3. `self.immio.buf` / `self.mmio.array`（其它实现）。
        拿到以后一次性转成 memoryview，之后 `struct.pack_into/unpack_from` 直接落。
        **不复制、不缓存值**：仍然是同一块 mapper，语义与 pynq.MMIO.write 等价。
        """
        if self._fast is not None:
            return self._fast
        if not (MMIO_FAST_ON or MMIO_C_ON or self._poll_mode):
            self._fast = False
            return self._fast
        info = dict(mv=None, lib=None, pvmode=False)
        if MMIO_FAST_ON or MMIO_C_ON:
            m = self.mmio
            mv = None
            why = ""
            for attr in ("mem", "mmap", "buf", "array"):
                obj = getattr(m, attr, None)
                if obj is None:
                    continue
                try:
                    mv = obj if isinstance(obj, memoryview) else memoryview(obj)
                    if mv.readonly:
                        raise BufferError("只读")
                    break
                except Exception as e:                      # noqa: BLE001
                    mv, why = None, "%s: %r" % (attr, e)
            if mv is None:
                self._fast_why = ("拿不到底层 buffer（%s）⇒ 用 pynq.MMIO 原路径"
                                  % (why or "没有 .mem/.mmap/.buf"))
                self._fast = False
                if self.verbose:
                    print("[bin_engine][T23] %s" % self._fast_why)
                return self._fast
            info["mv"] = mv
            info["pvmode"] = bool(MMIO_FAST_ON)   # _wr/_rd 走直写（BIN_MMIO_FAST=1）
            if MMIO_C_ON:
                try:
                    import mmio_batch
                    info["lib"] = mmio_batch.load(mv)
                except Exception as e:                      # noqa: BLE001
                    self._fast_why = "C 驱动不可用（%r）⇒ 回落 Python 路径" % (e,)
                    if self.verbose:
                        print("[bin_engine][T23] %s" % self._fast_why)
            if info["lib"] is None and not (MMIO_FAST_ON or info["pvmode"]):
                self._fast = False
                return self._fast
        self._fast = info
        if self.verbose and (MMIO_FAST_ON or info["lib"] is not None):
            print("[bin_engine][T23] fast-path=on  %s  poll=%r"
                  % ("C+py" if info["lib"] is not None else "py", self._poll_mode or "adaptive"))
        return self._fast

    def _fast_off(self, why):
        """一次性打印原因并永久回落到 `self.mmio.write/read`。"""
        self._fast = False
        self._fast_why = why
        if self.verbose:
            print("[bin_engine][T23] fast-path 关闭（%s）⇒ 回落 pynq.MMIO" % why)

    def mmio_stats(self):
        f = self._fast if isinstance(self._fast, dict) else None
        return dict(fast=bool(f), c=bool(f and f.get("lib") is not None),
                    poll=self._poll_mode or "adaptive", fast_w=self._fast_w,
                    fast_r=self._fast_r, merged=self._wr_merged,
                    c_cmds=self._c_cmds, why=self._fast_why,
                    writes=self._writes, writes_skipped=self._writes_skipped)

    def _start_planned(self):
        """C 路径专用：AP_CTRL 的写由 C 完成，这里只做**记账**（shadow/计数/trace）。

        不碰总线。这样 `_writes` / `_writes_skipped` / trace 与 Python 路径逐条一致
        （C1 判据要求 force 写也必须出现在记录里），而总线上仍然只有一次 start 写。
        """
        off = CFG + BIN_REG["AP_CTRL"]
        if self._trace_log is not None:
            self._trace_log.append(("W", off, 1, True, True))
        self._shadow[off] = 1
        self._writes += 1

    def _write_pairs_raw(self, pairs):
        """把已经"登记过"的 (off, v) 真的写下去，**不动影子/计数**（回落专用）。"""
        if not pairs:
            return
        f = self._fast
        mv = f["mv"] if (isinstance(f, dict) and f.get("pvmode")) else None
        run = []

        def flush_run():
            if not run:
                return
            if mv is not None:
                try:
                    if len(run) == 1:
                        struct.pack_into("<I", mv, run[0][0], run[0][1])
                    else:
                        struct.pack_into("<%dI" % len(run), mv, run[0][0],
                                         *[x[1] for x in run])
                    run.clear()
                    return
                except Exception:                          # noqa: BLE001
                    self._fast_off("_write_pairs_raw 的 pack_into 失败")
            for o, v in run:
                self.mmio.write(o, v)
            run.clear()

        for off, v in pairs:
            if run and off != run[-1][0] + 4:
                flush_run()
            run.append((off, v))
        flush_run()

    def _mb_run_cmd(self, pairs, ctrl_off, ret_off, timeout_s, wdt=None,
                    slice_s=0.5):
        """★ T-23 第 2 级：一次 C 调用完成 "写一批 + start + 等 done"。

        语义与 `[self.mmio.write]* + start() + wait()` 逐项相同：
          · 写序列 = pairs 的顺序（每项 32-bit volatile store）；
          · start 之前有 `__sync_synchronize()`（保证配置写先于 start 到达 AXI）；
          · 轮询只读 ret_off，判 bit0；**超时不再发第二次 start**；
          · 按 slice_s 切片返回，好让调用方继续喂狗（长命令也不会饿死看门狗）。
        返回 (status, elapsed_秒)。超时抛 TimeoutError，错误码由调用方解码。
        """
        lib = self._fast.get("lib")
        if lib is None:
            raise RuntimeError("没有 C 驱动")
        t0 = time.time()
        st = 0
        while True:
            budget = min(float(slice_s), max(1e-3, timeout_s - (time.time() - t0)))
            st = lib.run_pairs(ctrl_off, ret_off, pairs, budget,
                               1 if self._poll_mode == "tight" else 0)
            if st & 1:
                return st, time.time() - t0
            if wdt is not None:
                wdt.beat()
            if (time.time() - t0) > timeout_s:
                msg = ("bin_unit 超时 %.1fs 未 done（C 路径 timeout=%s）。"
                       "**不再发第二次 start**：引擎可能还在访存，继续跑会踩内存。"
                       % (time.time() - t0, timeout_s))
                if wdt is not None and getattr(wdt, "armed", False):
                    print("[bin_engine] " + msg, flush=True)
                    print("[bin_engine] 停止喂狗，%.0fs 后由硬件看门狗复位整板"
                          % getattr(wdt, "seconds", 0), flush=True)
                    while True:
                        time.sleep(1.0)
                raise TimeoutError(msg)

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

        ★ T-23：fast-path 只换"一次 32-bit 写怎么落到 mapper"（见模块头的开关），
        **影子缓存的判断与计数一个字节都不动** ⇒ trace 等价、`_writes_skipped`
        次数不变。任何异常永久回落（打印一行原因）。
        """
        off = int(off)
        v = int(v) & 0xFFFFFFFF
        lg = self._trace_log
        if not force and self._shadow.get(off, None) == v:
            self._writes_skipped += 1
            if lg is not None:
                lg.append(("W", off, v, bool(force), False))
            return
        f = self._fast
        if f is None:
            f = self._fast = False
        if f is not False and f["pvmode"]:
            try:
                struct.pack_into("<I", f["mv"], off, v)
                self._fast_w += 1
            except Exception as e:                     # noqa: BLE001
                self._fast_off("写 %#x 时 struct.pack_into 失败：%r" % (off, e))
                self.mmio.write(off, v)
        else:
            self.mmio.write(off, v)
        if lg is not None:
            lg.append(("W", off, v, bool(force), True))
        self._shadow[off] = v
        self._writes += 1

    def _wr_reg(self, name, v, force=False):
        self._wr(CFG + BIN_REG[name], v, force=force)

    def _wr_seq(self, pairs, collect=None):
        """按顺序写一组 ((off, v[, force]), ...) 并**合并连续偏移**。

        语义与逐个 `_wr()` 完全一致：影子判断、跳过计数、`_writes` 计数、
        `force` 语义逐条相同；唯一的区别是"连续偏移的多字"用**一次**
        `struct.pack_into`（例如 0x204/0x208 两字一次落地），减少 Python 调用次数。
        地址升序写入与逐个写下去在 AXI-Lite 上不可区分（同序、同值、同宽度）。

        collect 给了 list 时**不真的写**，而是把要发的 (off, v) 追加进去
        （给 `mmio_batch` 的 C 路径用：影子/计数照旧，由 C 一次发完）。
        """
        mv = None
        lg = self._trace_log
        f = self._fast
        if f is not None and f is not False and f["pvmode"]:
            mv = f["mv"]
        run = []                       # 待合并的连续字 [(off, v), ...]
        nw = 0

        def flush_run():
            nonlocal nw
            if not run:
                return
            if collect is not None:
                collect.extend(run)
            elif mv is not None:
                if len(run) == 1:
                    struct.pack_into("<I", mv, run[0][0], run[0][1])
                else:
                    struct.pack_into("<%dI" % len(run), mv, run[0][0],
                                     *[x[1] for x in run])
                    self._wr_merged += len(run) - 1
                self._fast_w += len(run)
                nw += 1
            else:
                for o, v in run:
                    self.mmio.write(o, v)
            run.clear()

        for item in pairs:
            off = int(item[0])
            v = int(item[1]) & 0xFFFFFFFF
            force = bool(item[2]) if len(item) > 2 else False
            if not force and self._shadow.get(off, None) == v:
                self._writes_skipped += 1
                if lg is not None:
                    lg.append(("W", off, v, bool(force), False))
                continue
            if lg is not None:
                lg.append(("W", off, v, bool(force), True))
            if run and off != run[-1][0] + 4:
                flush_run()
            run.append((off, v))
            self._shadow[off] = v
            self._writes += 1
        flush_run()
        return nw

    def shadow_reset(self):
        """丢掉影子寄存器（硬件被别的路径改过之后必须调用）。"""
        self._shadow.clear()

    def _rd(self, off):
        off = int(off)
        self._reads += 1
        lg = self._trace_log
        f = self._fast
        if f is not None and f is not False and f["pvmode"]:
            try:
                self._fast_r += 1
                val = struct.unpack_from("<I", f["mv"], off)[0]
                if lg is not None:
                    lg.append(("R", off, val, False, True))
                return val
            except Exception as e:                     # noqa: BLE001
                self._fast_off("读 %#x 时 struct.unpack_from 失败：%r" % (off, e))
        val = int(self.mmio.read(off)) & 0xFFFFFFFF
        if lg is not None:
            lg.append(("R", off, val, False, True))
        return val

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

    def wait(self, timeout=30.0, wdt=None, poll=None, est_s=None):
        """轮询 AP_RETURN.bit0。**超时不再发 start**；有看门狗就让板子复位。

        poll=None ⇒ 自适应（2026-10-30 实测：`time.sleep(2e-4)` 在这块板上实际要
        0.29 ms，`sleep(0)` 只要 0.010 ms，MMIO 读一次 25 µs）：
          · 前几拍不睡（短层 0.3~3 ms 能立刻抓到 done，不吃 0.29 ms 的量化误差）
          · 之后 sleep(0)，再往后逐步退避到 2e-4（长层不空转抢 AXI 带宽）
        显式传 poll=<秒> 则保持旧的固定间隔行为（老脚本/对比用）。

        ★ T-23：`BIN_MMIO_POLL=tight|est` 换轮询策略（默认为空 ⇒ 上面这条原路径）：
          · tight —— 一次都不睡。每次读本身就要 25 µs，紧轮询最多多花一次读的钱，
            却省掉 `sleep(0)` 的系统调用往返；done 后**最坏也只晚一次读**。
          · est   —— "按预测拍数先粗睡、再收窄"：
                    先睡 `k·est_s`（k 是固定的安全系数，见 `BIN_MMIO_PRED_K`），
                    之后**紧轮询**（不再睡）。`est_s` 来自解析拍数模型
                    （`17 + Σ(9·run+50)` 拍 ÷ f），是**下界**：读通路 stall 只会
                    让真实时长更长 ⇒ 早醒只会多读几次，不会睡过头。
        两种模式都**不改时间语义**（timeout 仍从 t0 起算），也不改任何寄存器写。

        **不会"因为睡着而错过 done"**（三条独立保证）：
          1. done 在 RTL 里是 sticky（`done_sticky`），只有下一次 start 才清
             ⇒ 晚读只影响"什么时候看见"，不影响"是否看见"；
          2. 睡完**一定**进紧轮询，且紧轮询没有次数上限，直到 done 或 timeout；
          3. 单次睡眠封顶 `0.02 s`（且 ≤ 0.9×timeout），所以 227 层里最长的那层
             也不可能"睡过去"；`k` 只在 [0.2, 1.0] 里动（见 `BIN_MMIO_PRED_K`）。
        """
        mode = self._poll_mode if poll is None else ""
        t0 = time.time()
        n = 0
        sleep_s = 0.0
        slept = False
        k_done = 0
        while True:
            if mode == "est":
                _tr = time.perf_counter()
                st = self._rd_reg("AP_RET")
                _rdt = time.perf_counter() - _tr
                # 实测"一次读"的成本（取历史最小，避开偶发抖动）：
                # 用它判断"这一层值不值得先睡"——睡不到 3 次读的钱就别睡。
                self._rd_us = _rdt if self._rd_us is None else min(self._rd_us, _rdt)
            else:
                st = self._rd_reg("AP_RET")
            if st & 1:
                dt = time.time() - t0
                if mode == "est" and est_s and sleep_s > 0.0:
                    # 只记录、不自校正：`est_s` 是**解析拍数下界**，睡 0.9×下界
                    # 永远不会睡过真实完成时刻；真实更慢时醒后紧轮询，读数与
                    # 原自适应同阶（不会更差）。任何"因为一读就中而缩短 k"的
                    # 反馈都会被"读本身就要 25~100 µs"污染 ⇒ 干脆不要。
                    self._pred_note = ("k=%.2f est=%.1fµs sleep=%.1fµs 醒后读=%d 实测=%.1fµs"
                                       % (self._pred_k, est_s * 1e6, sleep_s * 1e6,
                                          k_done, dt * 1e6))
                return st, dt
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
            if mode == "tight":
                continue                              # 不睡：下一次读就是下一次等
            if mode == "est" and est_s and n == 1 and not slept:
                slept = True
                # 只有"睡得过一次读的钱"才睡：短层（est < 3×实测读成本）直接紧轮询
                _s = float(self._pred_k) * float(est_s)
                _rd = float(self._rd_us or 0.0)
                if _rd > 0.0 and _s < 3.0 * _rd:
                    _s = 0.0
                sleep_s = min(max(0.0, _s), 0.02, max(0.0, 0.9 * float(timeout)))
                if sleep_s > 0.0:
                    _sleep_until(time.time() + sleep_s)   # 精确等到预测时刻
                    continue
                # 不值得睡 ⇒ 落到下面的紧轮询
            if mode == "est" and est_s:
                k_done += 1                           # 醒后紧轮询：每次读本身就是等待
                continue
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
        """写配置 + 基址（可提前调用）。cfg_only=True 时不查尺寸（只写寄存器）。

        ★ T-16 A（`BIN_PRECOMP=1`，默认关）：把 config+set_bases 的 (offset, value)
          序列按 `(层, 输入/输出物理地址, 权重容器)` 缓存成表，之后逐条重放。
          键含**喂给这两个函数的全部输入** ⇒ 地址一变必 miss、必重跑 guard_layer。
        """
        if not cfg_only and precomp_on():
            _r = self._arm_cached(L, in_buf, out_buf, verify_canary)
            if _r is not None:
                return _r
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

    # ---------------------------------------------------------------- T-16 A
    def _arm_key(self, L, in_pa, out_pa):
        """喂给 config+set_bases 的**全部**输入 ⇒ 任何一个变了就不可能命中。"""
        e = L.d
        return (int(self.blob_pa or 0), str(L.name), int(L.cin), int(L.cout),
                int(L.k), int(L.s), int(L.dil), int(L.pad), int(L.shift),
                int(L.alpha_shift), int(L.slope_shift), int(L.H), int(L.W),
                int(in_pa), int(out_pa),
                int(e["off_wgt"]), int(e["off_alpha"]), int(e["off_bias"]),
                int(e["off_zeta"]), int(e["off_gamma"]), int(e["off_slope"]))

    def arm_plan(self, L, in_pa, out_pa):
        """**不写寄存器**：只生成 arm() 会写的那串 `(offset, value)`。

        值、顺序与 `config(L)` + `set_bases(L, in_pa, out_pa)` **逐条一致**
        （config 的 14 条按 `L.regs()` 顺序，set_bases 的 8 个基址按
        IN/WGT/ALPHA/BIAS/ZETA/GAMMA/SLOPE/OUT 顺序，带高字槽的再补一条 0）。
        """
        w = [(CFG + BIN_REG[_n], int(_v) & 0xFFFFFFFF) for _n, _v in L.regs()]
        e = L.d
        blob_pa = int(self.blob_pa)
        for _n, _pa in (("IN", int(in_pa)),
                        ("WGT", blob_pa + int(e["off_wgt"])),
                        ("ALPHA", blob_pa + int(e["off_alpha"])),
                        ("BIAS", blob_pa + int(e["off_bias"])),
                        ("ZETA", blob_pa + int(e["off_zeta"])),
                        ("GAMMA", blob_pa + int(e["off_gamma"])),
                        ("SLOPE", blob_pa + int(e["off_slope"])),
                        ("OUT", int(out_pa))):
            BinGuard.check_pa(_pa, "B_" + _n)      # 与 set_base 里同一条护栏
            w.append((CFG + BIN_REG["B_" + _n], _pa & 0xFFFFFFFF))
            if _n in HIGH_WORD:
                w.append((CFG + HIGH_WORD[_n], 0))
        return tuple(w)

    def _arm_cached(self, L, in_buf, out_buf, verify_canary=True):
        """命中 ⇒ 逐条重放（走 `_wr` ⇒ 影子寄存器跳过逻辑与原来逐位相同）。"""
        try:
            in_pa = int(getattr(in_buf, "physical_address", 0))
            out_pa = int(getattr(out_buf, "physical_address", 0))
            key = self._arm_key(L, in_pa, out_pa)
            ent = _ARM_PLANS.get(key)
            if ent is None:
                # 首次：guard_layer 的全部护栏照跑（尺寸/对齐/白名单/权重区）
                g_in, g_out, need_out = self.guard_layer(
                    L, in_buf, out_buf, verify_canary=verify_canary)
                if int(g_in) != in_pa or int(g_out) != out_pa:
                    return None            # 护栏看到的地址与键不符 ⇒ 不缓存
                ent = (self.arm_plan(L, g_in, g_out), need_out)
                if len(_ARM_PLANS) >= _PRECOMP_MAX:
                    precomp_clear()
                    _PRECOMP_STATS["cleared"] += 1
                _ARM_PLANS[key] = ent
                _PRECOMP_STATS["arm_miss"] += 1
            else:
                _PRECOMP_STATS["arm_hit"] += 1
            writes, need_out = ent
            # 逐条重放。**不用** `self._wr()`：函数调用在 A53 上 ~3~5 µs，
            # 22 条 × 227 层就是 ~20 ms。这里把 `_wr` 的三件事（掩码已在建表时做完、
            # 影子寄存器比较、计数）内联，语义与 `_wr` 逐条相同（含两个计数器）。
            _sh = self._shadow
            _mw = self.mmio.write
            _nw = 0
            _ns = 0
            for _off, _val in writes:
                if _sh.get(_off, None) != _val:
                    _mw(_off, _val)
                    _sh[_off] = _val
                    _nw += 1
                else:
                    _ns += 1
            self._writes += _nw
            self._writes_skipped += _ns
            # `config()` 的**唯一**副作用（`kick()` 要用它估拍数/超时）
            self.cfg = L
            # 注：原路径的 `set_bases()` 末尾还会 `bases_now()` 回读 8 个基址
            #     （8 次 MMIO 读，板端 25 µs/次 ⇒ 227 层 ≈ 45 ms）——纯回读、无副作用、
            #     返回值被 `arm()` 丢弃 ⇒ 快路径不再回读。这一点在本文件头注释里写明。
            return need_out
        except Exception as _ex:           # noqa: BLE001
            _precomp_fallback("arm", _ex)
            return None

    def kick(self, timeout=None):
        """发 start，立即返回（不等 done）。返回 (t0, timeout)。

        ★ T-23（`BIN_MMIO_POLL=est`）：顺便算出"预计完成时刻"，让 `poll()`
        在到点之前**干脆不做那次 MMIO 读**（一次读 25~100 µs，比 sleep(0) 贵两个量级）。

        ★ v50（P3）：链在飞时**禁止**第二次 kick（契约 D3：「出错即停，不再取下一个
        描述符」；同一时刻只允许一条链在跑）。默认 `_v50_chain is None` ⇒ 无影响。
        """
        if self._v50_chain is not None:
            raise RuntimeError("v50 链在飞行中（head=%#x，progress=%d/%d）："
                               "禁止第二次 kick"
                               % (self._v50_chain["head"],
                                  self._v50_chain["progress"],
                                  self._v50_chain["n"]))
        if timeout is None:
            _c, est = estimate_cycles(self.cfg)
            timeout = max(5.0, min(600.0, est * 4 + 5.0))
        t0 = time.time()
        self._pred_done = None
        self._pred_est = 0.0
        self._pred_polls = 0
        if self._poll_mode == "est" and self.cfg is not None:
            try:
                _c, est = estimate_cycles(self.cfg)
                self._pred_est = float(est)
                self._pred_done = t0 + min(max(0.0, self._pred_k * float(est)),
                                           0.02, max(0.0, 0.9 * float(timeout)))
            except Exception:                       # noqa: BLE001
                self._pred_done = None
        self.start()
        return t0, timeout

    def poll(self):
        """非阻塞看 done：返回 status 或 None。

        ★ T-23（est 模式）：预计完成时刻之前**不发 MMIO 读**，只 `sleep(0)` 让出 CPU；
        到点之后回到原行为（每次调用读一次）。done 是 sticky ⇒ 晚读不会漏。
        """
        p = self._pred_done
        if p is not None:
            now = time.time()
            if now < p:
                time.sleep(0)
                return None
            self._pred_done = None
        st = self._rd_reg("AP_RET")
        if p is not None:
            self._pred_polls = getattr(self, "_pred_polls", 0) + 1
        return st if (st & 1) else None

    def reap(self, t0, timeout, out_buf=None, L=None, verify_canary=True, t_done=None):
        """等 done + 解码错误码 + 查金丝雀，返回 RunStats。

        t_done 由调用方在**第一次 poll 看到 done** 时记下（异步流水里 done 的时刻
        比 reap 的时刻早）。不给就退化成"reap 时刻 - start 时刻"。
        """
        L = L or self.cfg
        est_s = None
        if self._poll_mode == "est" and L is not None:
            try:
                _c, est_s = estimate_cycles(L)
            except Exception:                       # noqa: BLE001
                est_s = None
        self._pred_done = None          # 进阻塞等待：改用 wait() 的"先粗睡再收窄"
        st, _ = self.wait(timeout, est_s=est_s)
        err = (st >> 8) & 0xFF
        if err:
            raise BinRunError(err, msg="%s: bin_unit 报错 err_code=%d" % (L.name, err))
        if (st >> 1) & 1:
            raise RuntimeError("done=1 但 busy=1（AP_RETURN=%#010x）⇒ 接线/时序可疑" % st)
        if out_buf is not None and verify_canary:
            BinGuard.check_canary(out_buf, L.out_bytes, "bin_unit OUT")
        return RunStats(status=st, seconds=(t_done or time.time()) - t0, err_code=err,
                        layer=L, hout=L.Hout, wout=L.Wout, dbg={})

    # ================================================================ v50 链
    # P2/P3：**一次提交链首 + 等整链 done**，轮询 `CHAIN_STATUS`；err 置位即抛异常并停。
    # 全部由 `BIN_V50_FUSE=1` 把关；默认关时这些方法一个都不会被调用。
    # ★ 契约 R1（`docs/V50_ARCHITECTURE.md` §10.1）：v50 寄存器在 **0x300 段**，
    #   `0x200..0x20C` 是 E1E2 的 RUN_UNIT/E_GROUP/E_DESC/E_DESC_HI，**一个字节都不动**
    #   ⇒ FUSE=1 时 E1E2 可以照常挂（两者不再互斥，见 `enable_e1e2`）。
    # ★ 契约 R1/D9：`0x314 CHAIN_ID` 是**只读 magic**（`0x5030_0001`，R3 §12.1）；
    #   读不回等值就说明这条位流里没有链引擎 ⇒ 拒绝走链。这是**机制**，不是人工开关：
    #   `BIN_V50_CHAIN_RTL=1` 已退役（只保留给老脚本做兼容性 no-op）。
    def v50_chain_magic(self):
        """链引擎 magic 的**期望值**（默认 0x50300001，`BIN_V50_CHAIN_MAGIC` 可改期望值）。"""
        v = os.environ.get("BIN_V50_CHAIN_MAGIC", "").strip()
        if not v:
            return int(V50_CHAIN_MAGIC)
        try:
            return int(v, 0)
        except Exception:                                 # noqa: BLE001
            return int(V50_CHAIN_MAGIC)

    def v50_chain_status(self, pre_kick=False):
        """读 `0x310 CHAIN_STATUS` 并解析（契约 R1 §10.1）。

        bit0 = done / bit1 = err / bit2 = busy / bit8~15 = err_code。

        ★ 契约 R3 §12.2：**`err` 只在 kick 之后有意义**。
          机理（T-83 实测）：复位态 `bin_band_buf` 的 `cfg_c=cfg_w=0` 让
          `shape_bad` 恒真 ⇒ `cap_err` 粘滞 ⇒ `band_bad` ⇒ `segq` 进 `S_ERR`，
          于是空闲态 `0x310` 读成 `0x0403`（done=1, err=1, code=4=E_CANARY）。
          RTL 侧已加 `cfg_valid` 门控把它修掉；PS 侧在这里同时立规矩：
          `pre_kick=True` 时**只信 busy/done，err/code 一律按 0 处理**
          （读的是同一笔 MMIO，但语义上"还没开跑"不存在运行期错误）。
          `v50_submit_chain()` 的 kick 前自检就用 `pre_kick=True`。
        """
        raw = int(self._rd(CFG + V50_CHAIN_REG["CHAIN_STATUS"]))
        st = dict(raw=raw, done=raw & 1, err=(raw >> 1) & 1,
                  busy=(raw >> 2) & 1, code=(raw >> 8) & 0xFF)
        if pre_kick:
            # 未 kick：err/code 无意义（RTL 复位态曾误报 E_CANARY，见上）
            st["err"] = 0
            st["code"] = 0
            st["err_raw"] = (raw >> 1) & 1          # 只留痕，不参与判断
        return st

    def v50_chain_ready(self):
        """PL 侧 `bin_segq` 是否真的就位（**默认 False，绝不假装**）。

        两个条件同时成立才返回 True：
          ① `BIN_V50_FUSE=1`；
          ② **`0x314 CHAIN_ID` 读回 magic**（期望 `0x50300001`，契约 R3 §12.1）
              —— 这是唯一判据。
             写 `0x308 BAND_ROWS` 再读回只是**第二道自检**（magic 对但 BAND_ROWS
             读不回 ⇒ 报错并拒绝），因为"写什么读回什么"的寄存器回声（v39 的
             `0x208`）本质上无法区分，只有 RTL 里硬写死的常量能区分。
        结果缓存（探针只做一次）；`BIN_V50_REPROBE=1` 可强制重探。
        """
        if not v50_fuse_on():
            self._v50_why = "BIN_V50_FUSE 未开"
            return False
        if (getattr(self, "_v50_ok", None) is not None
                and not _v50_env_on("BIN_V50_REPROBE")):
            return bool(self._v50_ok)
        self._v50_ok = False
        self._v50_why = ""
        try:
            want = int(self.v50_chain_magic())
            got = int(self._rd(CFG + V50_CHAIN_REG["CHAIN_ID"]))
            if got != want:
                self._v50_why = (
                    "CHAIN_ID(magic @%#x) 期望 %#010x 读回 %#010x ⇒ 这条位流里"
                    "没有链引擎（v50 寄存器在 0x300 段，0x200 段是 E1E2）"
                    % (CFG + V50_CHAIN_REG["CHAIN_ID"], want, got))
                return False
            wr = int(v50_band_rows())
            self._wr(CFG + V50_CHAIN_REG["BAND_ROWS"], wr, force=True)
            back = int(self._rd(CFG + V50_CHAIN_REG["BAND_ROWS"]))
            if back != wr:
                self._v50_why = ("BAND_ROWS(@%#x) 写 %d 读回 %d ⇒ 寄存器不通"
                                 "（RTL 侧 bin_segq 未综合进这条位流）"
                                 % (CFG + V50_CHAIN_REG["BAND_ROWS"], wr, back))
                return False
            self._wr(CFG + V50_CHAIN_REG["FUSE_MODE"], 0, force=True)
            self._v50_magic = got
            self._v50_ok = True
            self._v50_why = ""
            return True
        except Exception as _ex:                          # noqa: BLE001
            self._v50_why = "链寄存器探针失败：%r" % (_ex,)
            return False

    def v50_chain_why(self):
        return getattr(self, "_v50_why", "")

    def v50_submit_chain(self, head_pa, n_desc, band_rows=None, timeout=None,
                         wdt=None):
        """**一次**提交整条链：BAND_ROWS / FUSE_MODE / CHAIN_HEAD → start。

        红线（契约 D3 + 板端事故教训）：
          · 链在飞时**禁止**第二次 kick（`self._v50_chain is not None` ⇒ 抛）；
          · 上一次链报错后**禁止**再提交（`self._v50_poison` ⇒ 抛），
            必须人工介入 / `v50_reset_chain()`。
        """
        if self._v50_chain is not None:
            raise RuntimeError("v50 链还在飞（head=%#x，progress=%d/%d）："
                               "禁止第二次 kick（契约 D3）"
                               % (self._v50_chain["head"],
                                  self._v50_chain["progress"],
                                  self._v50_chain["n"]))
        if self._v50_poison:
            raise RuntimeError("v50 链上一次已报错（%s）：**不再发第二次 kick**，"
                               "修好/重载后再来（或显式 v50_reset_chain()）"
                               % (self._v50_poison,))
        n = int(n_desc)
        if n <= 0:
            raise ValueError("空链（n_desc=%d）拒绝提交" % n)
        head_pa = int(head_pa)
        if head_pa & (V50_DESC_STRIDE - 1):
            raise ValueError("链首 %#x 不是 4 KB 对齐（契约 B）" % head_pa)
        if not head_pa:
            raise ValueError("链首物理地址为 0，拒绝启动（会让引擎读内核内存）")
        br = int(v50_band_rows() if band_rows is None else band_rows)
        if timeout is None:
            timeout = max(10.0, min(600.0, 2.0 * n))
        # ★ 契约 R3 §12.2：kick 前**只判 busy/done，不读 err**。
        #   理由：复位态的 `0x310` 曾把"形状还没装载"误报成 err=1/code=4
        #   （RTL 侧已用 `cfg_valid` 门控修掉；PS 侧也不许依赖它）。
        #   这里唯一的用途是"上一次运行真的停了没有"，那靠 busy 就够。
        #   读不到（任何异常）就跳过——kick 前自检不该成为新的失败点，
        #   所以异常**只**用来决定"跳过"，绝不复用成"拒绝提交"的理由。
        try:
            st0 = self.v50_chain_status(pre_kick=True)
        except Exception as _ex:                          # noqa: BLE001
            st0 = None
            if _v50_env_on("BIN_V50_DEBUG"):
                print("[v50] kick 前 busy 自检跳过（%r）" % (_ex,))
        if st0 is not None and st0["busy"]:
            raise RuntimeError(
                "kick 前 CHAIN_STATUS 仍 busy（raw=%#010x，progress 未知）："
                "上一次运行没停 ⇒ **拒绝提交**（契约 D3；err 在 kick 前"
                "无意义，这里不看 err，R3 §12.2）" % (st0["raw"],))
        self._wr_seq(((CFG + V50_CHAIN_REG["BAND_ROWS"], br),
                      (CFG + V50_CHAIN_REG["FUSE_MODE"], 1),
                      (CFG + V50_CHAIN_REG["CHAIN_HEAD"], head_pa)), collect=None)
        t0 = time.time()
        self.start()                      # AP_CTRL.bit0 = 1（自清位）
        self._v50_chain = dict(head=head_pa, n=n, band_rows=br, t0=t0,
                               timeout=float(timeout), progress=0, err=0,
                               done=False, polls=0)
        if not isinstance(getattr(self, "_v50_stats", None), dict):
            self._v50_stats = dict(chains=0, descs=0, seconds=0.0, progress=0)
        self._v50_stats["chains"] += 1
        return self._v50_chain

    def v50_poll_chain(self):
        """非阻塞：读 `0x310 CHAIN_STATUS` + `0x304 PROGRESS`（+ `AP_RET` 交叉校验）。

        完成 ⇒ 返回链 dict；否则 None。
        **err 置位 ⇒ 立刻抛 `BinRunError`**（err_code 来自 `CHAIN_STATUS[15:8]`），
        并把链状态清掉（RTL 已停止推进，PS 不许再 kick；契约 D3 / 不变量 ⑪）。
        ★ 契约 R1：done/err/busy/code 全部由 `0x310` 给出；`AP_RET` 只作交叉校验
          （链模式下整机只有这条链在跑，两边 err 位必须一致；不一致 ⇒ 直接报错）。
        ★ D9 自证：`PROGRESS` 必须**单调不减**；倒退 ⇒ 链被复位/寄存器接错 ⇒ 报错。
        """
        c = self._v50_chain
        if c is None:
            return None
        c["polls"] += 1
        cs = self.v50_chain_status()
        c["status_raw"] = cs["raw"]
        try:
            prog = int(self._rd(CFG + V50_CHAIN_REG["PROGRESS"]))
        except Exception:                                 # noqa: BLE001
            prog = -1
        if prog >= 0:
            if prog < int(c.get("progress", 0)):
                self._v50_chain = None
                self._v50_poison = ("PROGRESS 倒退 %d→%d @head=%#x"
                                    % (c["progress"], prog, c["head"]))
                raise BinRunError(0xE0, msg="v50 链 PROGRESS 倒退（%d→%d，head=%#x）"
                                   "⇒ 链被复位或寄存器接错，**不再发第二次 kick**"
                                   % (c["progress"], prog, c["head"]))
            c["progress"] = prog
        apret = int(self._rd(CFG + BIN_REG["AP_RET"]))
        err = int(cs["code"]) if cs["err"] else 0
        ap_err = (apret >> 8) & 0xFF
        if not err and ap_err:
            err = ap_err
        if cs["err"] and not err:
            err = 0xFF
        if err:
            c["err"] = err
            c["done"] = True
            self._v50_chain = None
            self._v50_poison = ("err=%d %s @progress=%d/%d head=%#x status=%#010x"
                                % (err, decode_err(err)[0], c["progress"],
                                   c["n"], c["head"], cs["raw"]))
            raise BinRunError(err, msg="v50 链报错：%s（progress=%d/%d，head=%#x，"
                               "CHAIN_STATUS=%#010x）；RTL 已停止推进，"
                               "**不再发第二次 kick**"
                               % (decode_err(err)[1], c["progress"], c["n"],
                                  c["head"], cs["raw"]))
        if cs["done"] and not cs["busy"]:
            # done 且不 busy。PROGRESS 读不到（-1）时只认 done 位，不硬判
            if prog < 0 or prog >= c["n"] or not self._v50_strict_progress():
                c["done"] = True
                c["status"] = apret
                c["chain_status"] = cs["raw"]
                return c
        return None

    @staticmethod
    def _v50_strict_progress():
        """PROGRESS 必须 == n_desc 才算 done（默认开；`BIN_V50_LOOSE=1` 放宽）。"""
        return not _v50_env_on("BIN_V50_LOOSE")

    def v50_wait_chain(self, timeout=None, wdt=None):
        """**提交后唯一的等待点**：轮询 PROGRESS 直到整链 done。

        返回 `dict(head=, n=, seconds=, status=, progress=)`；
        超时 / err ⇒ 抛（并把链状态清掉，绝不重试）。
        """
        c = self._v50_chain
        if c is None:
            raise RuntimeError("没有在飞的链（先 v50_submit_chain）")
        tmo = float(c["timeout"] if timeout is None else timeout)
        t0 = c["t0"]
        spin = 0
        while True:
            r = self.v50_poll_chain()
            if r is not None:
                break
            if wdt is not None:
                try:
                    wdt.kick()
                except Exception:                         # noqa: BLE001
                    pass
            now = time.time()
            if now - t0 > tmo:
                cc = self._v50_chain
                self._v50_chain = None
                self._v50_poison = "timeout %.1fs @progress=%s/%d" % (
                    tmo, (cc or {}).get("progress"), (cc or {}).get("n", 0))
                raise TimeoutError(
                    "v50 链超时 %.1f s（progress=%s/%s，head=%#x）；"
                    "**不再发第二次 kick**，人工介入"
                    % (tmo, (cc or {}).get("progress"), (cc or {}).get("n"),
                       (cc or {}).get("head", 0)))
            spin += 1
            # 自适应：前 8 次紧轮询，之后 sleep(0)，再之后 20 µs 粒度的短睡
            if spin > 640:
                time.sleep(2e-5)
            elif spin > 8:
                time.sleep(0)
        dt = time.time() - t0
        out = dict(head=c["head"], n=c["n"], seconds=dt,
                   status=c.get("status", 0), progress=c["progress"],
                   chain_status=c.get("chain_status", c.get("status_raw", 0)),
                   band_rows=c["band_rows"], polls=c["polls"])
        st_ = getattr(self, "_v50_stats", None)
        if isinstance(st_, dict):
            st_["descs"] = st_.get("descs", 0) + int(c["n"])
            st_["seconds"] = st_.get("seconds", 0.0) + dt
            st_["progress"] = c["progress"]
        self._v50_chain = None
        return out

    def v50_reset_chain(self):
        """清掉链状态与 poison（只在确认 RTL 已复位/重载后调用）。"""
        self._v50_chain = None
        self._v50_poison = None
        return True

    def v50_chain_stats(self):
        s = getattr(self, "_v50_stats", None)
        if not isinstance(s, dict):
            return dict(chains=0, descs=0, seconds=0.0, progress=0)
        return dict(s)

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

        ★ T-18 刀③（`BIN_EPI_NOINV=1`，默认关）：惰性分支**不再** `_invalidate`。
          安全性论证（不是"大概没事"）：本网 CPU 对这片 OUT 字节的**所有**读取都
          汇到 `read_out(lazy=False)` 一处（`C8Tensor.materialize()` / `_materialize()`），
          而那条路径在下面第一行就会 `_invalidate`；PL 侧走 AXI 读 DDR，与 CPU cache
          无关。⇒ 跳过这一句不会读到旧影子，省的是每层 0.27 ms（板端实测）的固定开销，
          在 198/227 层上**纯属浪费**（那些层 CPU 一个字节都不读）。
        """
        if lazy:
            pa = int(getattr(out_buf, "physical_address", 0) or 0)
            if pa:
                if not _epi_noinv():
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

        ★ 2026-11-02（D1 / 契约修订 R1 §10.1）：v50 寄存器搬到 **0x300 段**，
          `0x200` 仍然是 E1E2 的 `R_RUN_UNIT` ⇒ **FUSE=1 时也照常挂 E1E2**
          （原先"0x200 是 CHAIN_HEAD ⇒ 互斥"的护栏已撤；融合模式下打包/量化
          的 PS 加速不再被牺牲）。
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

# ============================================================================
# ★ v50：段计划 → 描述符链（契约 A/B/D，见 `docs/V50_ARCHITECTURE.md` §3）
# ----------------------------------------------------------------------------
# **全部 opt-in**（`BIN_V50_FUSE=1`）；默认关 ⇒ 本节每个函数都不会被调用，
# v5 的寄存器序列 / sha1 / stdout 一个字节都不变（C-C3 的第一条）。
#
# 契约 A（寄存器，偏移相对配置页 +0x10000）——**契约修订 R1（§10.1）后的地址**：
#     0x300 CHAIN_HEAD   链首描述符物理地址（写 1 次）
#     0x304 PROGRESS     已完成描述符数（RO，轮询它而不是逐 op 等 done）
#     0x308 BAND_ROWS    每带行数（参数，默认 8）
#     0x30C FUSE_MODE    0 = v39 行为 / 1 = 段融合链
#     0x310 CHAIN_STATUS bit0 done / bit1 err / bit2 busy / bit8~15 err_code（RO）
#     0x314 CHAIN_ID     **只读 magic** = 0x5030_0001（链引擎在位的唯一判据，
#                        契约 R3 §12.1；与 RTL 的 V50_CHAIN_ID 逐位一致）
#   ★ 0x200..0x20C 是 **E1E2 的** RUN_UNIT/E_GROUP/E_DESC/E_DESC_HI，一个字节都不动
#     ⇒ FUSE=1 与 E1E2 **可以同时挂**（原先的"0x200 互斥"护栏已撤）。
#   ★ `0x250..0x25C` 仍禁止（v42d 教训）。
#
# 契约 B（描述符 = 10 字 = 40 B，**每条 4 KB 对齐**，`next_pa` 链式）：
#     w0 desc      段输入的**源基址**。★ 二义（契约 R3 §12.5）：
#                  FUSE_MODE=1 ⇒ 段输入在 DDR 的源基址（填充器按
#                  in_pa + grp*(H_in*W*8) + yr*(W*8) + x*8 展开，带偏移只由
#                  w6/填行号给）；FUSE_MODE=0 ⇒ 旧语义"段输入张量基址"。
#     w1 out       本带输出基址（低 32 位）== 段输出张量基址，**不含带偏移**
#                  （带偏移只由 w6 `band_y0` 表达，见契约修订 R1 §10.2 / D3）
#     w2 cfg_c     本段输入通道数 cin（C8 对齐后）
#     w3 cfg_h     低 16 = 本带**有效输出行数** nband（R2 §11.5：**不含 halo**）
#                  高 16 = **H_in**（该段输入张量高度，契约 R3 §12.3）
#                  ⇒ 填充器判 yr >= H_in 时整行写 0。**两个 16 位都要填**
#     w4 cfg_w     宽度 W
#     w5 cfg_group 权重组 / E1E2 组号（段内唯一；本实现恒 0）
#     w6 band_y0   本带在**输出**张量里的起始行（绝对行号，< h_out）
#     w7 flags     bit0=本段最后一带 bit1=段输出要物化 bit2=含 shuffle bit3=f32 输出
#     w8 next_pa   下一个描述符物理地址（0 = 链尾）
#     w9 nband(低 16) | op_mask(高 16) — nband = 本带**有效输出行数**（R2 §11.1，
#                  末带可 < BAND_ROWS；与 w3 低 16 同值）
V50_DESC_WORDS = 10
V50_DESC_STRIDE = 4096                 # 契约 B：4 KB 对齐 ⇒ 步进 4 KB
V50_BUDGET_BYTES = 270 * 1024          # 契约 D4：BRAM 剩 ≈270 KB
V50_CHAIN_REG = dict(CHAIN_HEAD=0x300, PROGRESS=0x304,
                     BAND_ROWS=0x308, FUSE_MODE=0x30C,
                     CHAIN_STATUS=0x310, CHAIN_ID=0x314)
# 链引擎 magic（RTL 里硬写死的只读常量）：`0x314` 读回它就说明真在位。
# ★ 契约 R3 §12.1（2026-11-02）：**以 RTL 为准 = 0x5030_0001**。
#   改前 PS 这里是 `0x7635_5000`（T-81 的旧常量），而 RTL 的
#   `localparam [31:0] V50_CHAIN_ID = 32'h5030_0001`（"V50"+版本 1，T-82/D9）
#   在 xsim 里逐项验过 ⇒ 两边不一致时**只可能是 PS 错**，故改 PS 的常量。
#   `BIN_V50_CHAIN_MAGIC` 仍可覆盖期望值（见 `v50_chain_magic()`）；
#   "读不回等值就不走链、整跑回落 v5"的机制**一个字都不许动**。
V50_CHAIN_MAGIC = 0x5030_0001
# 段内小算子掩码的位序（契约 B 的 op_mask 高 16 位）
V50_OP_BITS = dict(conv=0, cat=1, add=2, interp=3, eca=4, sam=5,
                   shuffle=6, unshuffle=7, convfp=8)
V50_OP_MASK_ALL = (1 << len(V50_OP_BITS)) - 1

_ON_VALS = {"1": True, "on": True, "true": True, "yes": True}


def _v50_env_on(name):
    return _ON_VALS.get(os.environ.get(name, "").strip().lower(), False)


def v50_fuse_on():
    """`BIN_V50_FUSE=1` ⇒ 走段计划 + 描述符链。**默认关**（C-C3）。"""
    return _v50_env_on("BIN_V50_FUSE")


def v50_chain_rtl_on():
    """**已退役**（2026-11-02，D9/D1）：链在不在位改由 `0x314 CHAIN_ID` magic 判定。

    留这个函数只为兼容老脚本的 `if v50_chain_rtl_on():` 判断；它**不再**是
    `v50_chain_ready()` 的前置条件 —— 人工开关会被"寄存器回声"骗过（v39 的
    `0x208` 就是写什么读回什么），而 RTL 里硬写死的 magic 骗不过。
    """
    return _v50_env_on("BIN_V50_CHAIN_RTL")


def v50_pl_quant_on():
    """`BIN_V50_PLQ8=1` ⇒ 量化搬 PL（CPU 量化路径跳过）。默认关。"""
    return _v50_env_on("BIN_V50_PLQ8")


def v50_band_rows():
    try:
        return max(1, min(256, int(os.environ.get("BIN_V50_BAND_ROWS", "8"))))
    except Exception:                                    # noqa: BLE001
        return 8


# ----------------------------------------------------------------------------
# ★ v50：PRM 参数帧宽 / 段偏移 —— **必须随 VLANES 变**（16-lane 上板硬前提）
# ----------------------------------------------------------------------------
# 口径（IMP-A 的 RTL 侧已参数化，这里是 PS 侧唯一的"帧宽定义点"）：
#   `pw[0:PRM_W-1]` 是 RTL 内部的 per-oc-group 参数行，由 `prm_addr(i, o0)` 从
#   DDR 的 **5 个逐 oc 连续数组**按 8 B 步长拉；每 word = 64 bit：
#       alpha_q int16 ⇒ 4 个/word；bias/zeta/gamma int32 ⇒ 2 个/word；
#       slope_q int16 ⇒ 4 个/word
#   ⇒ 每 lane 的字数固定，VLANES=8 得 16 字，VLANES=16 得 32 字。
#     VLANES=8 时 PRM_W=16 / 段基址 0,2,6,10,14 —— 与 RTL 原硬编码**逐值相同**，
#     也与 v39 的 DDR 布局逐字节相同（所以这是纯定义，不改任何数据）。
#   ⚠ 16 lane 时如果还按 16 字给（PRM_W 没抬到 32），lane 8..15 的参数会读到
#     **相邻的另一个参数数组**（alpha 的尾巴读成 bias）⇒ **静默错数据**。
_PRM_SEG_ORDER = ("alpha", "bias", "zeta", "gamma", "slope")


def vlanes_now():
    """当前 lane 数：`BIN_VLANES`（默认 8）。`LANES=64` 是 popcount 位宽，别混。"""
    try:
        v = int(os.environ.get("BIN_VLANES", "8"))
    except Exception:                                    # noqa: BLE001
        return C8
    if v not in (1, 2, 4, 8, 16):
        raise ValueError("BIN_VLANES=%r 非法（只允许 1/2/4/8/16；"
                         "注意 LANES=64 是 popcount 位宽，不是 lane 数）" % (v,))
    return v


def prm_layout(vlanes=None):
    """PRM 帧宽与段偏移（**按 VLANES 推**，VLANES=8 时退化为 16 / 0,2,6,10,14）。

    返回 `dict(VLANES=, PRM_W=, PB_S=, PB_B=, PB_Z=, PB_G=, PB_SL=, seg={名字:(起,止)})`。
    公式（每 lane 的字数）：alpha = V/4，bias/zeta/gamma = V/2，slope = V/4
    ⇒ PRM_W = V/4*2 + V/2*3 = 2V（V=8 ⇒ 16；V=16 ⇒ 32）。
    """
    V = int(vlanes_now() if vlanes is None else vlanes)
    if V % 4:
        raise ValueError("VLANES=%d 必须是 4 的倍数（每 word 装 4 个 int16 / "
                         "2 个 int32）" % V)
    wa, wb, wz, wg, ws = V // 4, V // 2, V // 2, V // 2, V // 4
    pb_s = 0
    pb_b = pb_s + wa
    pb_z = pb_b + wb
    pb_g = pb_z + wz
    pb_sl = pb_g + wg
    prm_w = pb_sl + ws
    return dict(VLANES=V, PRM_W=prm_w, PB_S=pb_s, PB_B=pb_b, PB_Z=pb_z,
                PB_G=pb_g, PB_SL=pb_sl,
                seg=dict(alpha=(pb_s, pb_b), bias=(pb_b, pb_z),
                         zeta=(pb_z, pb_g), gamma=(pb_g, pb_sl),
                         slope=(pb_sl, prm_w)),
                words=dict(alpha=wa, bias=wb, zeta=wz, gamma=wg, slope=ws))


def prm_words_per_group(vlanes=None):
    """每个 oc 组的 PRM 帧字数（= 2*VLANES）。段计划里每段按此累加 PRM 字节。"""
    return int(prm_layout(vlanes)["PRM_W"])


def v50_prm_bytes(cout, vlanes=None):
    """一段/一层需要的 PRM 字节 = ceil(cout/VLANES) 组 × PRM_W 字 × 4 B。"""
    lay = prm_layout(vlanes)
    ng = (int(cout) + lay["VLANES"] - 1) // lay["VLANES"]
    return int(ng) * int(lay["PRM_W"]) * 4


def check_prm_capacity(L, vlanes=None):
    """16-lane 上板前的硬校验：cout 必须是 VLANES 的倍数。

    为什么必须查：5 个参数数组在 blob 里是**逐 oc 连续**的。cout 不是 VLANES 的
    倍数时，最后一组会读到相邻数组的字节 ⇒ PRM 数值静默错（不会报错）。
    VLANES=8 时本网 cout∈{32,48,64,96,192} 全过 ⇒ 默认路径行为不变。
    """
    V = int(vlanes_now() if vlanes is None else vlanes)
    cout = int(getattr(L, "cout", 0) or 0)
    if V > C8 and cout % V:
        raise ValueError("%s: cout=%d 不是 VLANES=%d 的倍数 ⇒ 最后一组 PRM 会读进"
                         "相邻参数数组（静默错数据）" % (getattr(L, "name", "?"),
                                                       cout, V))
    return True


def v50_desc(out_pa, cfg_c, cfg_h, cfg_w, cfg_group=0, band_y0=0,
             op_mask=0, flags=0, nband=1, in_pa=0, h_in=0):
    """造一条 10 字描述符（契约 B + 契约修订 R3）。

    ★ 字 0（w0）= `in_pa`，**语义随 `0x30C FUSE_MODE` 二义**（契约 R3 §12.5）：
        · `FUSE_MODE=0`（v5 逐 op）：段输入张量基址（旧语义，逐字节不变）；
        · `FUSE_MODE=1`（融合链）：**段输入在 DDR 的源基址**——填充器
          （`bin_band_fill_v50`）用它算
          `addr = in_pa + grp*(H_in*W*8) + yr*(W*8) + x*8`，
          带偏移**只**由 `w6 band_y0` 与填行号算，**不许**在这里预先加偏移。
      两种用法共用同一个字，所以 `in_pa` 必须传"段输入张量/整图块的源基址"。

    ★ 字 3（w3）= `cfg_h`（低 16）‖ `h_in`（高 16）（契约 R3 §12.3）：
        · 低 16 = 本带**有效输出行数** nband（不含 halo，R2 §11.5）；
        · 高 16 = 该段**输入张量高度** `H_in` ⇒ 填充器判 `yr ≥ H_in` 时
          整行写 0（pad 由填充路径做，conv 读侧零边界分支）。
        ⚠ 只填低 16（旧行为）⇒ `H_in=0` ⇒ **整带全 0**（sha1 会立刻暴露，
          这正是 R3 要的"错得响"）。所以 `h_in` 缺省 0 只用于离线打印/旧用例，
          运行时构造器（`v50_seg_descs`）会拒绝 `h_in==0`。
    """
    w3 = (int(cfg_h) & 0xFFFF) | ((int(h_in) & 0xFFFF) << 16)
    return [int(in_pa) & 0xFFFFFFFF, int(out_pa) & 0xFFFFFFFF,
            int(cfg_c) & 0xFFFFFFFF, w3,
            int(cfg_w) & 0xFFFFFFFF, int(cfg_group) & 0xFFFFFFFF,
            int(band_y0) & 0xFFFFFFFF, int(flags) & 0xFFFFFFFF,
            0, ((int(nband) & 0xFFFF) | ((int(op_mask) & 0xFFFF) << 16))]


def v50_chain_words(descs, base_pa):
    """N 条描述符串成链：`next_pa = base_pa + (i+1)*4 KB`，链尾 = 0。

    `base_pa` 必须是 4 KB 对齐的**首条描述符**物理地址（契约 B）。
    """
    n = len(descs)
    base_pa = int(base_pa)
    if n and base_pa & (V50_DESC_STRIDE - 1):
        raise ValueError("链首 %#x 不是 4 KB 对齐（契约 B）" % base_pa)
    words = []
    for i, d in enumerate(descs):
        w = list(d)[:V50_DESC_WORDS]
        if len(w) < V50_DESC_WORDS:
            w += [0] * (V50_DESC_WORDS - len(w))
        w[8] = 0 if i == n - 1 else base_pa + (i + 1) * V50_DESC_STRIDE
        words.append([int(x) & 0xFFFFFFFF for x in w])
    return words


def v50_flat_words(words):
    """把链里每条描述符的 10 个字按顺序摊平（离线打印 / 逐字节比对用）。"""
    out = []
    for w in words:
        out.extend(int(x) & 0xFFFFFFFF for x in w)
    return out


def v50_chain_bytes(n):
    """一条 N 条描述符的链需要的 arena 字节（含一次 4 KB 对齐余量）。"""
    return int(n) * V50_DESC_STRIDE + V50_DESC_STRIDE


def v50_chain_store(view, view_pa, descs, base_pa=None):
    """把一条链写进**已注册 arena 的视图**（链首 4 KB 对齐）。

    返回 `(链首 pa, 逐条 10 字)`。`base_pa` 不给 ⇒ 取 `view_pa` 之上第一个
    4 KB 边界。字节不够就抛（不静默截断）。
    """
    n = len(descs)
    if n == 0:
        return 0, []
    view_pa = int(view_pa)
    if base_pa is None:
        base_pa = (view_pa + V50_DESC_STRIDE - 1) // V50_DESC_STRIDE \
            * V50_DESC_STRIDE
    base_pa = int(base_pa)
    if base_pa & (V50_DESC_STRIDE - 1):
        raise ValueError("链首 %#x 不是 4 KB 对齐" % base_pa)
    off = base_pa - view_pa
    if off < 0:
        raise ValueError("链首 %#x 在视图 %#x 之前" % (base_pa, view_pa))
    need = off + n * V50_DESC_STRIDE
    if len(view) < need:
        raise ValueError("链缓冲不够：需要 %d B（%d 条 × 4 KB + 对齐 %d），"
                         "只有 %d B" % (need, n, off, len(view)))
    words = v50_chain_words(descs, base_pa)
    mv = np.frombuffer(_u8(view), np.uint8)
    for i, w in enumerate(words):
        o = off + i * V50_DESC_STRIDE
        mv[o:o + V50_DESC_WORDS * 4] = np.asarray(w, np.uint32).view(np.uint8)
    _flush(view)
    return base_pa, words


def v50_format_chain(words, head_pa=0):
    """离线打印（D2 证据形态）：每条 10 个字 + 链拓扑 + 链尾判据。"""
    lines = []
    for i, w in enumerate(words):
        lines.append("    [#%03d] " % i + " ".join("%08x" % x for x in w))
    if words:
        lines.append("    链拓扑：head=%#010x  %s  （共 %d 条，末条 = 链尾）"
                     % (int(head_pa) & 0xFFFFFFFF,
                        " -> ".join("#%d" % i for i in range(len(words))),
                        len(words)))
        lines.append("    链尾检查：words[-1][8] = %#x（契约 B：0 = 链尾）"
                     % words[-1][8])
        lines.append("    对齐检查：%s" % (
            "每条 next_pa 都 4 KB 对齐"
            if all((w[8] == 0) or (w[8] % V50_DESC_STRIDE == 0) for w in words)
            else "⚠ 有 next_pa 不是 4 KB 对齐"))
    return "\n".join(lines)

# ============================================================================
# ★ T-23：MMIO 访问层 fast-path（**全部默认关**，opt-in；关掉时逐字节走原路径）
# ----------------------------------------------------------------------------
# 三个独立开关（互不依赖，可以只开一个）：
#   BIN_MMIO_FAST=1   直接对底层 buffer（pynq MMIO 的 mmap / memoryview）做
#                     struct.pack_into / unpack_from，绕开 pynq.MMIO.write 的
#                     struct.pack + 切片赋值（主机实测 0.80 µs → 0.11 µs/次）。
#   BIN_MMIO_POLL=tight|est
#                     done 轮询策略。默认（不设）= 原来的自适应（前 3 次紧轮询
#                     → sleep(0) → 指数退避）。tight = 一次都不睡；est = 先按
#                     解析拍数模型睡 60% 的估计时长再紧轮询。
#   BIN_MMIO_C=1      用 ps/pynq/mmio_batch.c（首次使用时 cc -O2 现场编译）把
#                     "一批写 + start + 等 done + 读状态"压进**一次 C 调用**。
# ----------------------------------------------------------------------------
# 语义红线（三条都不可放宽）：
#   1. 影子缓存（`_shadow` / `_writes_skipped`）**逻辑一个字节不改**：fast-path 只
#      替换"一次 32-bit 写怎么落到 mapper"，不替换"哪些写该发、发的顺序"。
#   2. `force=True` 的有副作用寄存器（start / 清 sticky）仍然每次都真写。
#   3. 任何异常（拿不到 buffer / C 库加载失败 / struct 报错）⇒ 打印一行原因，
#      **永久回落**到 `self.mmio.write/read`，绝不抛给调用方。
_MMIO_FAST_ENV = os.environ.get("BIN_MMIO_FAST", "").strip().lower()
MMIO_FAST_ON = _MMIO_FAST_ENV not in ("", "0", "off", "false", "no", "n")
_MMIO_POLL_ENV = os.environ.get("BIN_MMIO_POLL", "").strip().lower()
_MMIO_C_ENV = os.environ.get("BIN_MMIO_C", "").strip().lower()
MMIO_C_ON = _MMIO_C_ENV not in ("", "0", "off", "false", "no", "n")

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


def _fclk_mhz_now(explicit=None, fallback=150.0):
    """E1E2 的**实际**时钟（MHz）。取值顺序：显式参数 > pynq 的 FCLK0 > 150。

    ★ 为什么需要它（T-23 §7.2 记录的老 bug，本轮合并时修掉）：
      `E1E2Engine.fclk_mhz` 历史上默认 **100 MHz**，而设计/板端实跑 **150 MHz**
      ⇒ `dbg_summary()` 的 compute_ms 与 `BIN_MMIO_POLL=est` 的预测时长**都偏大 1.5×**。
      上游不传参数的地方（`enable_e1e2()`）就吃这个默认值，所以错的是"默认"而不是调用方。

    不引入任何新依赖：拿不到 pynq（离线/主机）就回落到设计频率 150。
    """
    if explicit:
        try:
            f = float(explicit)
            if f > 0.0:
                return f
        except Exception:                    # noqa: BLE001
            pass
    try:
        from pynq.ps import Clocks
        f = float(Clocks.fclk0_mhz)
        if 1.0 <= f <= 2000.0:
            return f
    except Exception:                        # noqa: BLE001
        pass
    return float(fallback)


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
        print(ee.dbg_summary())
    """

    def __init__(self, core, desc_dir=None, verbose=False, program=None,
                 fclk_mhz=None):
        self.core = core
        self.verbose = bool(verbose)
        self.program = program or E1E2Program(desc_dir=desc_dir, verbose=self.verbose)
        # ★ 实际时钟：不传就取 FCLK0，拿不到按 150（设计跑 150；旧默认 100 是错的）
        self.fclk_mhz = _fclk_mhz_now(fclk_mhz)
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
        # ★ 2026-11-02（D1 / 契约修订 R1）：v50 寄存器在 0x300 段，`0x200` 仍是
        #   E1E2 的 R_RUN_UNIT ⇒ 这里**不需要**任何"链模式互斥"护栏；链首写 0x300
        #   与 R_RUN_UNIT 写 0x200 互不影响（FUSE=1 时 E1E2 照常可用）。
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
                timeout=None, wdt=None, count=True, skip_idle=False, chain=None):
        """一条 E1E2 命令：写 5 个寄存器 → 等 idle → start → 等 done → 读错误码/DBG。

        ★ v50（P2）：`chain` 给了 list 时**一条 MMIO 都不写**，只把这条命令追加成
        一条 10 字描述符（40 B，契约 B）进链；真正的下发由 `submit_chain()` 一次完成。
        默认 `chain=None` ⇒ 下面的代码路径与改动前**逐字节相同**（C-C3）。

        ★ T-23：写序列改成一次 `_wr_seq`（合并连续偏移），并可选把
        "这批写 + start + 等 done" 压进**一次 C 调用**（`BIN_MMIO_C=1`）。
        寄存器集合、顺序、影子跳过、`force` 语义、done 判据**完全相同**。
        """
        core = self.core
        cyc = None
        if timeout is None:
            hw = int(cfg_h) * int(cfg_w)
            cyc, left = 17, hw
            while left > 0:
                run = min(E1E2_ACC_DEPTH, left)
                cyc += 9 * run + 50
                left -= run
            timeout = max(5.0, min(600.0, cyc / 50e6 * 4.0 + 5.0))
        est_s = None
        if cyc is None:                       # 给了 timeout 也要能估（C/est 用）
            hw = int(cfg_h) * int(cfg_w)
            cyc, left = 17, hw
            while left > 0:
                run = min(E1E2_ACC_DEPTH, left)
                cyc += 9 * run + 50
                left -= run
        est_s = cyc / (float(getattr(self, "fclk_mhz", 150.0)) * 1e6)

        if chain is not None:
            # ★ v50 链模式：**只登记描述符，不发 MMIO、不等 done**（P2/P3）。
            #   select(4) 不必做：D1 之后链首在 0x300，`0x200` 仍是 R_RUN_UNIT
            #   （E1E2 自己的寄存器块），两者不再互相踩。
            chain.append(v50_desc(
                in_pa=int(desc_pa), out_pa=int(out_pa),
                cfg_c=int(cfg_c), cfg_h=int(cfg_h), cfg_w=int(cfg_w),
                cfg_group=int(cfg_group), band_y0=0,
                op_mask=(1 << V50_OP_BITS["conv"]), flags=0, nband=1))
            return dict(status=0, seconds=0.0, err=0, dbg={}, queued=True,
                        n_chain=len(chain))

        self.select(4)
        lib = self.core._fast.get("lib") if isinstance(self.core._fast, dict) else None
        pairs = [] if lib is not None else None
        # C/H/W 与 E_GROUP/E_DESC/E_OUT：地址不连续的两段，各自内部合并。
        # shadow/计数/force 语义在 _wr_seq 里照旧；collect 给了就只登记不发。
        core._wr_seq((((CFG + BIN_REG["C"]), int(cfg_c)),
                      ((CFG + BIN_REG["H"]), int(cfg_h)),
                      ((CFG + BIN_REG["W"]), int(cfg_w))), collect=pairs)
        core._wr_seq((((CFG + E1E2_REG["E_GROUP"]), int(cfg_group)),
                      ((CFG + E1E2_REG["E_DESC"]), int(desc_pa)),
                      ((CFG + E1E2_REG["E_OUT"]), int(out_pa))), collect=pairs)
        if not skip_idle:
            # 同一 job 的后续组：上一组刚 wait 到 done ⇒ 本来就 idle，
            # 省掉一次轮询（板端 0.1~0.3 ms × 698 条）
            _tw = time.perf_counter()
            self._wait_idle(timeout=min(max(30.0, timeout), 120.0))
            self._agg["idle"] = self._agg.get("idle", 0.0) + (
                time.perf_counter() - _tw)
        if lib is not None:
            # ---- C 路径：一次调用完成 写 + start + 等 done（切片喂狗）----
            core._start_planned()          # start 的记账（C 负责真写）
            try:
                st, dt = core._mb_run_cmd(pairs, CFG, CFG + BIN_REG["AP_RET"],
                                          timeout, wdt=wdt)
                core._c_cmds += 1
            except Exception as e:                       # noqa: BLE001
                core._fast_off("C 路径失败（%r）⇒ 本轮回落 Python" % (e,))
                core._write_pairs_raw(pairs)
                core.start()
                st, dt = core.wait(timeout, wdt=wdt, est_s=est_s)
        else:
            core.start()
            st, dt = core.wait(timeout, wdt=wdt, est_s=est_s)
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

    # ------------------------------------------------------------ v50 描述符链
    def chain_on(self):
        """链模式是否真的可用（`BIN_V50_FUSE=1` 且 RTL 探针通过）。"""
        try:
            return bool(v50_fuse_on() and self.core.v50_chain_ready())
        except Exception:                                 # noqa: BLE001
            return False

    def chain_descs(self, cin, H, W, out_pa, desc_base_pa):
        """把 ceil(cin/64) 个"逐组命令"摊成 N 条 10 字描述符（契约 B）。

        `desc_base_pa` = 该层的 lane 描述符表基址（8 条 × 64 B = 512 B 一组）；
        第 g 条描述符的 w0 指向 `desc_base_pa + g*64`，w1 = 输出第 g 组。
        之前 `_run_desc` 是**逐组 kick 一次**（每条 run_one 一次 start），
        这里变成**一条链、一次 kick**（P2/P3）。
        """
        cin, H, W = int(cin), int(H), int(W)
        cb = (cin + 63) // 64
        hw = H * W
        descs = []
        for g in range(cb):
            d = v50_desc(in_pa=int(desc_base_pa) + g * E1E2_DESC_STRIDE,
                         out_pa=int(out_pa) + g * hw * 8,
                         cfg_c=cin, cfg_h=H, cfg_w=W, cfg_group=min(g, 7),
                         band_y0=0, op_mask=(1 << V50_OP_BITS["conv"]),
                         flags=0, nband=1)
            if g == cb - 1:                       # 链尾那一带的 flags bit0
                d[7] = (d[7] | 0x1)
            descs.append(d)
        return descs

    def submit_chain(self, descs, out_len=0, bufs=None, restore=True,
                     timeout=None, tag="e1e2"):
        """**一次**把整条链写进 arena 并提交链首，等整链 done（P2/P3）。

        返回 `dict(head=, n=, seconds=, progress=, words=)`；不可用 ⇒ None（调用方回落）。
        """
        n = len(descs)
        if n == 0:
            return None
        core = self.core
        if not self.chain_on():
            return None
        need_desc = v50_chain_bytes(n)
        if bufs is None:
            views = self._pack_views(need_desc=need_desc, need_out=int(out_len))
        elif isinstance(bufs, dict):
            views = (bufs["in"], bufs["out"], bufs["thr"], bufs["desc"])
        else:
            views = bufs
        vdesc = views[3]
        desc_pa = int(getattr(vdesc, "physical_address", 0) or 0)
        if desc_pa == 0:
            return None
        head, words = v50_chain_store(vdesc, desc_pa, descs)
        try:
            core.v50_submit_chain(head, n, timeout=timeout)
            st = core.v50_wait_chain(timeout=timeout)
        finally:
            if restore:
                # ★ D1：链首在 0x300，`0x200` 是 E1E2 的 R_RUN_UNIT ⇒ 这里只关
                #   FUSE_MODE（`0x30C`），不碰 E1E2 的寄存器块；复位语义由 RTL 的
                #   FUSE_MODE 位负责（IMP-B R4），PS 只写一次 0。
                try:
                    core._wr(CFG + V50_CHAIN_REG["FUSE_MODE"], 0, force=True)
                except Exception:                         # noqa: BLE001
                    pass
        st.update(words=words, tag=tag)
        self.last_chain = st
        return st

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
            # ★ T-16 A：这块 pack arena 的**物理地址必须跨帧稳定**，否则
            #   thr_pa/vdesc_pa/vin_pa 每帧都变 ⇒ 预计算表每帧全 miss（A 白干）。
            #   板端 `run_net()` 每帧都新建 engine ⇒ 原逻辑每帧都重新 CMA 分配。
            #   ⇒ `BIN_PRECOMP=1` 时按 (need) 做**进程级复用**：同一尺寸只有一个实例。
            #   前提：整网 run 是同步的（同一时刻只有一个 engine 在跑），与本仓现状一致。
            if precomp_on():
                # 键含**分配器的身份**：同一块"内存脊"（板端 = 同一个 pynq 分配函数/
                # 同一个 CMA 池）里同尺寸的视图才能复用；测试里每跑一次换一个
                # SubAlloc ⇒ 键自然不同 ⇒ 不会跨"已释放的映射"复用（那是假的）。
                _sh = _PVIEWS_SHARED.get((id(_pynq_allocate), int(need)))
                if _sh is not None:
                    _buf, _views, _vpn = _sh
                    self._pbuf, self._pviews, self._pn = _buf, _views, _vpn
                    try:
                        _pa = int(_buf.physical_address)
                        if not any(int(_r[0]) == _pa for _r in self.core._regions):
                            self.core.add_region(_buf, "e1e2_pack")
                    except Exception:              # noqa: BLE001
                        pass
                    return self._pviews
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
            if precomp_on():
                if len(_PVIEWS_SHARED) >= 4:
                    _PVIEWS_SHARED.clear()
                _PVIEWS_SHARED[(id(_pynq_allocate), int(need))] = (
                    self._pbuf, self._pviews, self._pn)
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

        ★ v50（P2/P6）：链模式就绪时 job 里多带三个键 ——
          `n_desc`（整层摊成多少条描述符）、`chain`（是否一次提交整链）、
          `pl_quant`（量化搬到 PL ⇒ 本函数**不再**做 CPU 量化）。
          默认（`BIN_V50_FUSE=0` 或 RTL 未就位）三个键都不出现，逐字节不变。
        """
        cin, H, W = int(L.cin), int(L.H), int(L.W)
        hw = H * W
        cb = (cin + 63) // 64
        # ★ v50 P6：量化搬 PL（`BIN_V50_PLQ8=1` 且链模式就绪）⇒ 本函数不做 CPU 量化
        _pl_q8 = bool(v50_pl_quant_on() and self.chain_on())
        # ★ T-16 A（`BIN_PRECOMP=1`，默认关）：段表/阈值/描述符/视图只算一次。
        #   键 = (层名, cin/H/W, fs_in, vin_pa, thr_pa, vdesc_pa, 逐段 (kind,C,H,W,fs,pa))
        #   ⇒ 任何运行时地址变化都会 miss ⇒ 重算（不会读到陈旧表）。
        if precomp_on() and bufs is None and not _pl_q8:
            _r = self._prepare_segments_cached(segs, L, t_q, plan)
            if _r is not None:
                return _r
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
            if _pl_q8:
                # ★ v50 P6：量化搬到 PL（链模式的 band 通路读 f32 段自己量化）
                #   ⇒ **CPU 一个字节都不写**。默认关 ⇒ 下面逐句与原来相同。
                continue
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
                                               if _s.get("kind") != "i8"),
                    # ★ v50：链模式的元数据（默认路径下这三个键**不出现**）
                    **({"n_desc": int(cb), "chain": bool(self.chain_on()),
                        "pl_quant": bool(_pl_q8)} if (_pl_q8 or v50_fuse_on())
                       else {}))

    # ---------------------------------------------------------------- T-16 A
    @staticmethod
    def _seg_sig(segs):
        """段表的**全部信息**（含逐个物理地址）：任何一个变了就不可能命中。

        只放 `(C, H, W, fs, pa)`：`kind` 被 `(pa==0)` 蕴含（i8 段必须有 pa，
        查不到 pa 就直接 return None）。**字符串化与 int() 全部去掉**
        —— 在 A53 上"每层建一次键"的成本里这两样占大头，而它们不是信息。
        """
        return tuple((s["C"], s["H"], s["W"], s.get("fs") or 0,
                      s.get("pa") or 0) for s in segs)

    def _prepare_segments_cached(self, segs, L, t_q, plan):
        """`prepare_segments` 的预计算版：命中 ⇒ 只做"f32 段量化落 vin"这一段数据活。

        与 `prepare_segments` **逐字节等价**：命中的只是与数据无关的三件事
        （`t_q` 取用 / `_seg_layout` / 阈值+描述符组装 / `_pack_views`），
        真正写 vin 的 f32 量化循环与原来逐句相同。
        任何异常 ⇒ 返回 None（调用方走原路径），并打一行原因。
        """
        try:
            cin, H, W = int(L.cin), int(L.H), int(L.W)
            hw = H * W
            cb = (cin + 63) // 64
            fs_in = int(L.d["act_fs_in"])
            need_in = cb * 64 * hw
            _pf = {} if _PROF else None
            _t = time.perf_counter() if _PROF else 0.0
            # `_pack_views` 的返回值对全层是**同一组视图**（尺寸取命令表最大值
            # `pmax`），所以"有效需求 == 上次切块时的需求"⇒ 直接复用，等价于调用。
            _v = getattr(self, "_pviews", None)
            _pm = getattr(self, "_pmax", None)
            _pn = getattr(self, "_pn", None)
            if (_v is not None and _pm is not None and _pn is not None
                    and (max(need_in, _pm[0]), max(cb * hw * 8, _pm[1]),
                         max(cb * 64 * 2, _pm[2]),
                         max(cb * 64, _pm[3])) == _pn):
                views = _v
            else:
                try:
                    views = self._pack_views(need_in=need_in, need_out=cb * hw * 8,
                                             need_thr=cb * 64 * 2,
                                             need_desc=cb * 64)
                except Exception:              # noqa: BLE001
                    return None
            if _pf is not None:
                _pf["views"] = time.perf_counter() - _t
            vin, vout, vthr, vdesc = views
            vin_pa = int(getattr(vin, "physical_address", 0) or 0)
            thr_pa = int(getattr(vthr, "physical_address", 0) or 0)
            vdesc_pa = int(getattr(vdesc, "physical_address", 0) or 0)
            if vin_pa == 0 or thr_pa == 0:
                return None
            sig = getattr(segs, "sig", None)
            if sig is None or len(sig) != len(segs):
                sig = self._seg_sig(segs)
            key = (str(L.name), cin, H, W, fs_in, vin_pa, thr_pa, vdesc_pa, sig)
            ent = _SEG_PLANS.get(key)
            if ent is None:
                _t = time.perf_counter() if _PROF else 0.0
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
                for (s_, _o, _c) in lay:
                    if s_.get("kind") == "i8" and int(s_.get("pa") or 0) == 0:
                        return None
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
                                      else int(vin_pa)
                                      for s_ in (x[0] for x in lay)], np.int64)
                    src = pas[fast["si"]] + fast["lane_off"]
                    if fast["fill"] is not None:
                        src = src[fast["fill"]]
                    tp = (int(thr_pa) + lanes2).astype(np.uint64)
                    descs = (tp << np.uint64(32)) | src.astype(np.uint64)
                else:
                    thr, descs = self._build_desc(L, lay, t, cin, H, W,
                                                  vin_pa, 0, thr_pa)
                if thr is None:
                    return None
                n_f32 = sum(1 for _s, _o, _c in lay if _s.get("kind") != "i8")
                ent = (tuple((str(s_.get("kind")), int(o), int(c))
                             for s_, o, c in lay),
                       np.ascontiguousarray(thr, np.int16),
                       np.ascontiguousarray(descs, np.uint64),
                       int(need_in), int(n_f32))
                if len(_SEG_PLANS) >= _PRECOMP_MAX:
                    precomp_clear()
                    _PRECOMP_STATS["cleared"] += 1
                _SEG_PLANS[key] = ent
                _PRECOMP_STATS["seg_miss"] += 1
            else:
                _PRECOMP_STATS["seg_hit"] += 1
            if _pf is not None:
                _pf["desc"] = time.perf_counter() - _t
            lay_k, thr, descs, need_in, n_f32 = ent
            if len(lay_k) != len(segs):
                return None                    # 防御：结构不符就不缓存命中
            # ---- 数据相关部分：f32 段量化 + C8 布局落 vin（逐句同原路径）----
            _t = time.perf_counter() if _PROF else 0.0
            for (kind, off, C), s_ in zip(lay_k, segs):
                if kind == "i8":
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
                    try:
                        got = qm.c8_into(x, fs_in, mv[o:o + C * hw], C, H, W)
                        done = got is not None
                    except Exception:          # noqa: BLE001
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
                self._prof_add(_pf)
            return dict(cin=cin, H=H, W=W, views=views, descs=descs, thr=thr,
                        need_in=need_in, n_f32=n_f32)
        except Exception as _ex:               # noqa: BLE001
            _precomp_fallback("prepare_segments", _ex)
            return None

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
        _chain_used = None
        if self.chain_on():
            # ★ v50（P2/P3）：整层 cb 组 →**一条 10 字描述符链、一次 kick**，
            #   不再"每组一次 start + 一次等 done"。拿不到链 ⇒ 回落逐命令（不变）。
            try:
                _chain_used = self.submit_chain(
                    self.chain_descs(int(cin), int(H), int(W), out_pa,
                                     int(vdesc.physical_address)),
                    out_len=need_out, bufs=(vin, vout, vthr, vdesc),
                    restore=restore, timeout=timeout, tag="e1e2_pack")
            except Exception:                      # noqa: BLE001
                _chain_used = None
        if _chain_used is None:
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
                                    chain=(_chain_used or {}).get("n", 0),
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
_c8deq_t = None          # ★ T-18 刀④：tile 化入口（惰性 import）


def _deq_kernel():
    global _c8deq
    if _c8deq is None:
        try:
            from c8deq import deq as _f
            _c8deq = _f
        except Exception:                       # noqa: BLE001
            _c8deq = False
    return _c8deq or None


def _deq_kernel_tiled():
    """★ T-18 刀④：`c8deq.deq_tiled`（tile 化）。不可用 ⇒ None（调用方回落）。"""
    global _c8deq_t
    if _c8deq_t is None:
        try:
            from c8deq import deq_tiled as _f
            _c8deq_t = _f
        except Exception:                       # noqa: BLE001
            _c8deq_t = False
    return _c8deq_t or None


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
        # ★ T-18 刀④：`BIN_DEQ_TILE=1` 走 tile 化内核；不可用 ⇒ 回落旧内核 ⇒ 回落 numpy
        if _deq_tile():
            kt = _deq_kernel_tiled()
            if kt is not None:
                try:
                    y = kt(raw, C, H, W, int(fs_out))
                    if y is not None:
                        return y[:C]
                except Exception:             # noqa: BLE001
                    pass
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
            if _deq_tile():
                try:
                    from c8deq import deq_into_tiled as _into_t
                    got = _into_t(raw, C, H, W, int(fs_out), dst[:n])
                    if got is not None:
                        return d.reshape(cg * C8, H, W)[:C]
                except Exception:          # noqa: BLE001
                    pass
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


def _sleep_until(t_end, tail=3e-4):
    """睡/忙等到 `t_end`（time.time() 口径）。**末尾 tail 秒忙等**：精确到 µs。

    为什么不能只用 `time.sleep()`：Linux 上一次 sleep 的实际时长有 ~50 µs 量级的
    下限/过冲（本项目实测 `sleep(2e-4)` 实际 0.29 ms、`sleep(1e-6)` 实际 52 µs），
    拿它去"睡 180 µs"会睡成 300~500 µs，反而比不睡更慢。
    长等待仍然走 sleep（不烧 CPU），最后 `tail` 秒用 `perf_counter` 忙等收口。
    """
    while True:
        rem = t_end - time.time()
        if rem <= 0.0:
            return
        if rem > tail:
            time.sleep(rem - tail)
        else:
            t_c = time.perf_counter()
            while time.perf_counter() - t_c < rem:
                pass
            return


def _invalidate(buf):
    try:
        buf.invalidate()
    except Exception:                     # noqa: BLE001
        pass


# ============================================================================
# T-18 的四个开关（**全部 opt-in，默认关**；全关时逐字节等于改动前）
# ============================================================================
#   刀① `BIN_CAT_ECA_FUSE=1`  bin_net：cat3 不物化、eca 逐叶求均值
#   刀② `BIN_SAM_TILE=1`      bin_ref_numpy：sam 分片单趟（`BIN_SAM_SLAB_KB`）
#   刀③ `BIN_EPI_NOINV=1`     bin_engine：惰性 read_out 跳过 invalidate
#   刀④ `BIN_DEQ_TILE=1`      c8deq.c 的 tile 化内核（新入口，旧入口保留）
def _env_on(name):
    return os.environ.get(name, "").strip().lower() in ("1", "on", "true", "yes")


def _epi_noinv():
    return _env_on("BIN_EPI_NOINV")


def _deq_tile():
    return _env_on("BIN_DEQ_TILE")


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


# ============================================================================
# 6.4 ★ 依赖可见性：让「内核没装上 ⇒ 静默回落」不可能被忽略
# ============================================================================
# 2026-11-02 事故：冻结点漏了 q8/c8deq/bpack 三个内核模块，`_q8()` / `_deq_kernel()`
# / `_bpack()` 全部静默回落 numpy，**一行日志都没有** ⇒ 同一个 bin_net、同一块板、
# 同一个 boot 上跑出 prologue 5.948→1.611 s（×3.69）、epilogue 1.189→0.258 s（×4.63）、
# total 11.752→6.497 s，而"慢的那一档"被当成了结果记了两天。
# 证据：runs/board_ops/20261102/REPORT_rootcause_missing_deps.md。
#
# 本节**不动任何算法**，只做两件事：
#   ① `report_deps()` 打一行 `[DEPS] q8=fast c8deq=fast ...  ⇒ deps_closure_ok=True`
#      （**每进程一次**；不健康时再补几行"缺哪个/为什么/怎么修"）；
#   ② `kernel_deps()` 把同一份状态交给调用方写进跑点：`bin_net.run()` 的
#      `stats["kernel_deps"] / stats["deps_closure_ok"]`、`board_v39_verify.runpoint()`。
#
# ★ 关键设计：**判状态不许有副作用**。
#   `_q8()` / `_deq_kernel()` / `_bpack()` 一旦被调用就会 `cc -O3` 现场编译 .so（~1.5 s），
#   那会把"一次性编译成本"从 prologue/epilogue 里**挪走** ⇒ 计时口径被改。
#   ⇒ 本节探测只做三件事：
#       · `find_spec`（只查能否解析，**不 import**）；
#       · 读模块自己的 `_STATE`（本进程已试过 load() ⇒ 用**权威**结果）；
#       · 看 `.c` / `.so` / `cc` 是否在位（预测"首次使用会不会成功"）。
#   跑完再调一次 `kernel_deps()`：`_STATE` 已被真实路径填好 ⇒ 得到**权威**状态。
#   ⇒ 所以：开头那行是"装配前预测"，`stats["kernel_deps"]` 是"跑完实测"；
#     两者不一致时 `deps_drift()` 会给出差异，`bin_net.run()` 会补一行 ⚠ 并把
#     `deps_closure_ok` 判成 False（宁可报错，不许再静默）。
DEPS_NAMES = ("q8", "c8deq", "bpack", "e1e2", "catpack", "safe")
_DEPS_MOD = {"q8": "q8", "c8deq": "c8deq", "bpack": "bpack",
             "e1e2": "e1e2_sim", "catpack": "catpack", "safe": "board_safe"}
_DEPS_NATIVE = ("q8", "c8deq", "bpack")          # 有 .c ⇒ 首次使用现场编译 .so
_DEPS_HINT = {
    "q8": "f32→int8 量化（prologue；缺 ⇒ prologue ×3.69）",
    "c8deq": "C8→f32 反量化（epilogue；缺 ⇒ epilogue ×4.63）",
    "bpack": "f32→uint64 二值打包（prologue 小头；缺 ⇒ +0.45 s）",
    "e1e2": "E1E2 描述符/C8 填充（冷路径）",
    "catpack": "CATPACK=1 的 cat 直读后端（冷路径）",
    "safe": "看门狗 board_safe.Watchdog（冷路径）",
}
_DEPS_STATE = {"reported": None}                 # 只打一行，不刷屏


def _deps_arch():
    import platform
    return platform.machine().lower()


def _deps_md5(path):
    try:
        import hashlib
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:                            # noqa: BLE001
        return None


def _deps_spec(modname):
    """`find_spec`：只查"能不能解析到"，**不 import**（不触发 .so 编译）。"""
    try:
        import importlib.util as _iu
        sp = _iu.find_spec(modname)
    except Exception:                            # noqa: BLE001
        return None
    if sp is None or not getattr(sp, "origin", None):
        return None
    return sp.origin


def _deps_tried_state(modname):
    """若本进程已试过 `load()`，返回模块自己记的权威状态 (state|None, why)。"""
    m = sys.modules.get(modname)
    st = getattr(m, "_STATE", None) if m is not None else None
    if not isinstance(st, dict) or not st.get("tried"):
        return None, ""
    if st.get("lib") is not None:
        return "fast", "已装载（%s）" % (st.get("why") or "ok")
    return "fallback-numpy", "已回落：%s" % (st.get("err") or st.get("why") or "?")


def _deps_native_state(modname):
    """q8/c8deq/bpack：权威优先（已试过），否则做**零副作用**的预测。"""
    st, why = _deps_tried_state(modname)
    origin = _deps_spec(modname)
    so = os.path.join(HERE, "%s_%s.so" % (modname, _deps_arch()))
    if st is not None:
        return dict(state=st, why=why, source="runtime", module=origin, so=so)
    if origin is None:
        return dict(state="fallback-numpy", source="probe", module=None, so=so,
                    why="模块不在 sys.path（缺 %s.py）" % modname)
    csrc = os.path.join(HERE, modname + ".c")
    if not os.path.exists(csrc):
        return dict(state="fallback-numpy", source="probe", module=origin, so=so,
                    why="缺 %s.c：只有 .py 编不出 .so ⇒ 只能 numpy 回落" % modname)
    if os.path.exists(so):
        try:
            import ctypes as _ct
            _ct.CDLL(so)                          # 只加载，不调函数（不编译、不自检）
            return dict(state="fast", source="probe", module=origin, so=so,
                        why=".so 已在位且可加载（本次探测未触发编译）")
        except Exception as ex:                  # noqa: BLE001
            why_so = "现有 .so 不可加载（%r）" % (ex,)
    else:
        why_so = ".so 尚未生成"
    cc = os.environ.get("CC", "cc")
    if shutil.which(cc) is None:
        return dict(state="fallback-numpy", source="probe", module=origin, so=so,
                    why="%s 且找不到编译器 %r ⇒ 只能 numpy 回落" % (why_so, cc))
    return dict(state="fast", source="probe", module=origin, so=so,
                why="%s，但 %s.c 与 %s 都在 ⇒ 首次使用会现场编译"
                    % (why_so, modname, cc))


def kernel_deps():
    """6 个内核模块的运行时状态（**零副作用**：不 import、不编译）。

    返回 `{name: dict(state, why, source, module, so, so_md5, hint)}`

    状态取值（就这三个，别的都算 bug）：
      · `fast`           —— 内核/模块可用，跑的就是快路径；
      · `fallback-numpy` —— **装不上，已静默回落到 numpy 纯 PS 实现**（只可能出现在
                            q8/c8deq/bpack：它们是"有 C 用 C、没 C 用 numpy"）；
      · `missing`        —— 可选模块不在 sys.path（e1e2/catpack/safe：缺了 = 该功能不可用）。
    `deps_closure_ok` 只要求 `DEPS_NAMES`（任务书那 6 个）全为 `fast`。
    `bilinear_c` 是 **opt-in**（`BIN_BILINEAR_C=1` 才加载 .so，默认关）⇒ 不进这一行；
    它的**文件齐不齐**由 `deps_closure.txt`（ps/tools/deps_closure.py --check）负责。
    """
    d = {}
    for name in _DEPS_NATIVE:
        d[name] = _deps_native_state(_DEPS_MOD[name])
    # safe：`from board_safe import Watchdog` 在本模块 import 时就试过一次 ⇒ 权威
    d["safe"] = dict(state=("fast" if Watchdog is not None else "missing"),
                     source="runtime", module=_deps_spec("board_safe"), so=None,
                     why=("Watchdog 已挂上" if Watchdog is not None else
                          "board_safe 不在 sys.path 或 import 失败 ⇒ 本跑无看门狗"))
    for name in ("e1e2", "catpack"):
        origin = _deps_spec(_DEPS_MOD[name])
        d[name] = dict(state=("fast" if origin else "missing"), source="probe",
                       module=origin, so=None,
                       why=("模块可解析" if origin else
                            "模块不在 sys.path（缺 %s.py）" % _DEPS_MOD[name]))
    for k, v in list(d.items()):
        v["hint"] = _DEPS_HINT[k]
        v["so_md5"] = _deps_md5(v["so"]) if v.get("so") else None
    return d


def deps_closure_ok(deps=None):
    """任务书那 6 个内核是否全部 `fast`（= 这一跑跑在"应跑的内核"上）。"""
    d = kernel_deps() if deps is None else deps
    return all(d[k]["state"] == "fast" for k in DEPS_NAMES)


def deps_line(deps=None):
    """`[DEPS] q8=fast ... ⇒ deps_closure_ok=True` —— 唯一新增的默认输出。"""
    d = kernel_deps() if deps is None else deps
    return ("[DEPS] " + " ".join("%s=%s" % (k, d[k]["state"]) for k in DEPS_NAMES)
            + "  ⇒ deps_closure_ok=%s" % deps_closure_ok(d))


def report_deps(force=False, stream=None):
    """打一行 `[DEPS] ...`（每进程一次）+ 不健康时补"缺哪个/为什么/怎么修"。"""
    if _DEPS_STATE["reported"] is not None and not force:
        return _DEPS_STATE["reported"]
    d = kernel_deps()
    out = stream or sys.stdout
    print(deps_line(d), file=out, flush=True)
    if not deps_closure_ok(d):
        # 只在**已经出事**的时候多打几行 ⇒ 健康跑点的输出只多那一行
        print("[DEPS] ⚠ 依赖闭包不完整：这一跑跑的不是快路径，**数字不可当基线**",
              file=out, flush=True)
        for k in DEPS_NAMES:
            if d[k]["state"] == "fast":
                continue
            print("[DEPS]   ✗ %-8s %-9s %s\n[DEPS]             原因：%s"
                  % (k, d[k]["state"], d[k]["hint"], d[k]["why"]),
                  file=out, flush=True)
        print("[DEPS]   修法：把 ps/pynq/{q8,c8deq,bpack}.{py,c} 与 e1e2_sim.py / "
              "catpack.py / board_safe.py 放进同一个目录（冻结点 = 依赖闭包，不是文件清单）；\n"
              "[DEPS]         清单见 runs/board_ops/20261102/deps_closure.txt，"
              "校验 `python3 ps/tools/deps_closure.py --check --dir <冻结点目录>`。",
              file=out, flush=True)
        print("[DEPS]   sys.path 前 6 项：%s" % (sys.path[:6],), file=out, flush=True)
    _DEPS_STATE["reported"] = d
    # ★ §③ fail-fast（默认关）：`BIN_REQUIRE_KERNELS=1` ⇒ 打完诊断再 raise。
    #   先打印再抛：日志里既有"一行 [DEPS]"也有完整修法，异常里也有（10 秒能修）。
    if require_kernels() and not deps_closure_ok(d):
        raise deps_require_error(d)
    return d


def deps_drift():
    """"开跑前预测" vs "现在实测" 的状态差异 ⇒ `[(name, before, after)]`。

    非空 = 某个内核**首次使用时才现原形**（预测说 fast、实际回落了）。
    `bin_net.run()` 在写 stats 时调它：有差异就补一行 ⚠，并把 `deps_closure_ok=False`。
    """
    before = _DEPS_STATE["reported"]
    if not before:
        return []
    now = kernel_deps()
    return [(k, before[k]["state"], now[k]["state"]) for k in DEPS_NAMES
            if before[k]["state"] != now[k]["state"]]

# ============================================================================
# 6.5 ★ fail-fast 开关 + 性能门槛告警（**两个都默认关 ⇒ 线上行为不变**）
# ============================================================================
# ① `BIN_REQUIRE_KERNELS=1`：任一内核没装上就 **raise**。
#    语义：这个开关就是"本次跑点必须全快路径"的声明。配上它，2026-11-02 那种
#    "13.4 s 跑到报告里"的事故会当场变成异常，而不是一个漂亮的数字。
#    错误信息要能让人 **10 秒修好**：缺哪个模块 / 期望在哪 / 现在解析到哪 /
#    .so 状态 / `sys.path` 前几项 / 闭包清单在哪。
#
# ② 性能门槛告警（`BIN_PROFILE=1` 下）：`prologue.q8` 或 `epilogue` 超过历史值的
#    1.5× ⇒ 醒目告警 + `stats["run_valid"]=False`。**不自动修正，只报警。**
#    为什么需要它：依赖齐了**不等于**内核真在跑（`CC` 坏、`.so` 自检不过、
#    环境变量把开关关了，都能让回落照样发生）；而回落是**数值等价**的，
#    逐字节/sha1/拍数三件套**全部通过**。唯一能看见它的，是"这两个桶不该这么慢"。
#
#    历史门槛（256² 热态，来自 2026-11-02 的板端实测与 docs/PLAN_1second.md）：
#      prologue.q8 ≈ 0.61 s、epilogue ≈ 0.26 s（C 内核）；numpy 回落时
#      q8 落在 ≈ 3.6 s、epilogue ≈ 1.19 s ⇒ 差距 4~6×，1.5× 的门槛**不会误报**、
#      也**不会漏报**（哪怕只回落了一个模块）。
#    ⚠ 只在 256²（net 的 H0/W0 = 256）上判：其它分辨率没有历史值，
#      硬套门槛会把正常跑点判成无效。这不是"没做"，是"只对已知基线判"。
REQUIRE_KERNELS_ENV = "BIN_REQUIRE_KERNELS"

PERF_GATE = dict(
    ratio=1.5,
    hw=256,
    q8_s=0.61,
    epilogue_s=0.26,
    source="2026-11-02 板端 256² 热态（runs/board_ops/20261102/ 与 docs/PLAN_1second.md）；"
           "numpy 回落时为 q8≈3.6 s / epilogue≈1.19 s",
)


def require_kernels():
    """`BIN_REQUIRE_KERNELS=1` ⇒ 任一内核没装上就 raise（**默认关**）。"""
    v = os.environ.get(REQUIRE_KERNELS_ENV, "").strip().lower()
    return v not in ("", "0", "off", "false", "no", "n", "none")


def deps_warm():
    """三个 native 内核在本进程里**是否已经真的试过 `load()`**。

    = True ⇒ 这一跑**不会**付"首次装载 + 自检"的一次性成本 ⇒ 桶里是**热态**，
      性能门槛（`prologue.q8 ≈0.61 s / epilogue ≈0.26 s`）才有意义；
    = False ⇒ 这一跑是**口径 a**（含一次性装载）。此时门槛**不判**——否则每次
      session 的首跑都会被判 `run_valid=False`，报警就变成了噪声。

    ★ 这条是**板端实测逼出来的**（2026-11-02 12:13，同一 boot、同一闭包）：
      首跑 epilogue = **1.640 s**、热态 = **0.257 s**（×6.4）；首跑 prologue =
      3.572 s、热态 = 1.613 s。原因不是回落，而是 `.so` 虽已编好，
      `q8/c8deq/bpack` 的 `load()` + 逐位自检仍要 1.4~1.9 s，且发生在**首跑的
      prologue/epilogue 桶里**。⇒ 与 `bilinear_c` 一样，它必须被当成"一次性成本"
      显式计出去（见 `load_kernels()` / `board_v39_verify.step_kernels()`）。
    """
    d = kernel_deps()
    return all(d[k]["source"] != "probe" for k in _DEPS_NATIVE)


def load_kernels():
    """显式装载 `q8/c8deq/bpack`（**一次性成本，单独计时**；零数值影响）。

    与 `board_v39_verify.step_bilinear()` 对 `bilinear_c` 的处理完全同构：
    在**计时区间之外**先把内核装上，并把这次成本写进运行点，
    这样后面每一跑的 `prologue.q8 / epilogue` 才是**热态**口径（历史基线 8.096/6.485
    就是热态）。不调用它也能跑，只是首跑会被判"口径 a"、门槛不判。

    返回 `{name: {loaded, seconds, why}}`。
    """
    out = {}
    for name in _DEPS_NATIVE:
        t0 = time.perf_counter()
        try:
            m = __import__(_DEPS_MOD[name])
            lib = m.load()
            out[name] = dict(loaded=bool(lib is not None),
                             seconds=round(time.perf_counter() - t0, 4),
                             why=str(getattr(m, "why", lambda: "")()))
        except Exception as ex:                    # noqa: BLE001
            out[name] = dict(loaded=False,
                             seconds=round(time.perf_counter() - t0, 4),
                             why=repr(ex)[:200])
    return out


def deps_require_error(deps=None):
    """构造 fail-fast 的 `RuntimeError`（**10 秒能修**：缺什么 / 在哪 / 现在解析到哪）。"""
    d = kernel_deps() if deps is None else deps
    miss = [k for k in DEPS_NAMES if d[k]["state"] != "fast"]
    L = ["%s=1：%d/%d 个内核没装上 ⇒ 拒绝在 numpy 回落路径上跑"
         % (REQUIRE_KERNELS_ENV, len(miss), len(DEPS_NAMES)), ""]
    for k in miss:
        e = d[k]
        L.append("  ✗ %-8s state=%s" % (k, e["state"]))
        L.append("      用途    ：%s" % _DEPS_HINT.get(k, ""))
        L.append("      期望模块：%s.py%s" % (_DEPS_MOD[k],
                                            "（+ %s.c，.so 由 cc 现场编）" % _DEPS_MOD[k]
                                            if k in _DEPS_NATIVE else ""))
        L.append("      现在解析：%s" % (e.get("module") or "<未解析到 —— 不在 sys.path 上>"))
        if e.get("so"):
            L.append("      .so     ：%s%s" % (
                e["so"], "（存在，md5=%s）" % e["so_md5"] if e.get("so_md5")
                else "（不存在 ⇒ 首次使用要现场 cc 编译）"))
        L.append("      原因    ：%s" % e.get("why"))
    L += ["", "  期望位置（本模块所在目录，依赖必须在这里闭包内）：",
          "    %s" % HERE,
          "  %s 现在的 sys.path 前 8 项：" % sys.executable]
    L += ["    [%d] %s" % (i, p) for i, p in enumerate(sys.path[:8])]
    L += ["", "  修法（任选其一，都别改代码）：",
          "    ① 把缺的文件整份拷到上面那个目录（冻结点 = 依赖闭包，不是文件清单）：",
          "       runs/board_ops/20261102/deps_closure.txt 里列了全部 11 个 .py + 4 个 .c；",
          "    ② 或把包含它们的目录加到 PYTHONPATH / sys.path **前面**；",
          "    ③ 校验：python3 ps/tools/deps_closure.py --check --dir <冻结点目录>",
          "  注意：只拷 .py 不够 —— q8/c8deq/bpack/bilinear_c 还要各自的 .c 才编得出 .so。"]
    return RuntimeError("\n".join(L))


# ============================================================================
# 6.5 ★ T-16 A：形状+地址预计算表（`BIN_PRECOMP=1`，**默认关**）
# ============================================================================
# 为什么：227 层的 cfg / 阈值 / 描述符 / 视图 **只由 (形状, 槽地址, 权重容器) 决定，
# 与数据无关**，但原来每帧都用 Python 重算一遍。板端实测这一族 = 621 ms
# （cfg 350 + desc 85.5 + views 61.3 + prepare_segments 入口 42.9 + 段表 glue 81.8），
# 其中只有 86.9 ms 是 MMIO 本体（2861 次写 × 30.37 µs）的硬底
# ⇒ 剩下 ~530 ms 全是 Python 组装（证据：runs/t16_prologue/REPORT_attribution.md）。
#
# 正确性的**结构性**保证（不靠"人记得 invalidate"）：
#   · arm 的键含 `(blob_pa, 层名, cin/cout/k/s/dil/pad/shift/两个 shift, H, W,
#     in_pa, out_pa, 六个权重区偏移)` —— 即**喂给 config+set_bases 的全部输入**；
#   · prepare_segments 的键含 `(层名, cin/H/W, fs_in, vin_pa, thr_pa, vdesc_pa,
#     逐段的 (kind, C, H, W, fs, pa))` —— 即**喂给 _build_desc 的全部输入**；
#   ⇒ 任何一个运行时地址变化 ⇒ 键变 ⇒ 必 miss ⇒ 重算（并重跑全部护栏）。
#   ⇒ 表里只有整数/小数组，**不持有任何缓冲的强引用** ⇒ 不拖住 arena 内存。
#
# 护栏不减：`guard_layer()` 的全部检查（尺寸/对齐/白名单/权重区）在**miss 时**照跑；
#   hit 时跳过是因为"同一组地址 + 同一层 + 同一权重容器"已经验证过一次。
_PRECOMP_MAX = 8192                    # 表的条数上限（超了整表清空，行为确定）
_PRECOMP_STATS = dict(arm_hit=0, arm_miss=0, seg_hit=0, seg_miss=0,
                      cleared=0, falls=0)
_PVIEWS_SHARED = {}                    # need_bytes -> (pbuf, views, pn)：跨帧复用
_ARM_PLANS = {}                        # key -> (writes, need_out)
_SEG_PLANS = {}                        # key -> (lay_k, thr, descs, need_in, n_f32)
_PRECOMP_NOTE = {}


_ON_VALS = {"1": True, "on": True, "true": True, "yes": True}


def precomp_on():
    """`BIN_PRECOMP=1/on/true/yes` ⇒ 开（默认关；关时一个字节的路径都不变）。"""
    return _ON_VALS.get(os.environ.get("BIN_PRECOMP", "").strip().lower(), False)


def precomp_clear():
    _ARM_PLANS.clear()
    _SEG_PLANS.clear()
    _PVIEWS_SHARED.clear()


def precomp_stats():
    d = dict(_PRECOMP_STATS)
    d["arm_n"] = len(_ARM_PLANS)
    d["seg_n"] = len(_SEG_PLANS)
    d["on"] = bool(precomp_on())
    return d


def precomp_reset_stats():
    for k in _PRECOMP_STATS:
        _PRECOMP_STATS[k] = 0


def precomp_print_stats(st=None):
    """只在 `BIN_PRECOMP_STATS=1` 时被调用（正常 stdout 逐字不变）。"""
    d = precomp_stats() if st is None else dict(st)
    print("[PRECOMP] arm hit/miss = %d/%d  seg hit/miss = %d/%d  表 %d/%d 条"
          "  清表 %d  回落 %d"
          % (d.get("arm_hit", 0), d.get("arm_miss", 0),
             d.get("seg_hit", 0), d.get("seg_miss", 0),
             d.get("arm_n", 0), d.get("seg_n", 0),
             d.get("cleared", 0), d.get("falls", 0)), flush=True)


def _precomp_fallback(where, ex):
    """异常**静默回落**原路径 + 打一行原因（每进程每处只打一次）。"""
    _PRECOMP_STATS["falls"] += 1
    k = str(where)
    if k not in _PRECOMP_NOTE:
        _PRECOMP_NOTE[k] = 1
        print("  ⚠ BIN_PRECOMP=1 但在 %s 上失败（%s: %s）⇒ 该处回落原路径"
              % (k, type(ex).__name__, ex), flush=True)


def perf_gate(q8_s=None, epilogue_s=None, hw=None):
    """256² 热态的门槛检查。返回 `[alert, ...]`（**空 = 通过**；只报警、不修正）。

    `q8_s`      = `BIN_PROFILE=1` 时 `prepare_segments` 的 `q8` 桶（query+量化+落 vin）
    `epilogue_s`= `run()` 的 `ps_epilogue` 桶
    门槛 = 历史值 × `PERF_GATE["ratio"]`（1.5）。
    """
    out = []
    if hw is not None and int(hw) != int(PERF_GATE["hw"]):
        return out                              # 非 256²：没有历史值，不判（见节头）
    for key, got in (("q8", q8_s), ("epilogue", epilogue_s)):
        if got is None:
            continue
        base = float(PERF_GATE["%s_s" % key])
        lim = base * float(PERF_GATE["ratio"])
        if float(got) > lim:
            out.append(dict(bucket=key, got_s=float(got), base_s=base,
                            limit_s=lim, ratio=float(PERF_GATE["ratio"]),
                            hw=(int(hw) if hw is not None else PERF_GATE["hw"])))
    return out


def perf_alert_text(a):
    """一条告警的可读形式（`run()` 与 driver 共用，格式统一）。"""
    what = ("prologue.q8" if a["bucket"] == "q8" else "epilogue")
    guess = ("疑似 numpy 回落（q8 未装上）" if a["bucket"] == "q8"
             else "疑似 numpy 跨步回落（c8deq 未装上）")
    return ("[PERF] ⚠⚠ %s = %.3f s > 门槛 %.3f s（历史 %.3f s × %.2f）"
            "⇒ %s ⇒ run_valid=False，本跑点不许进基线表"
            % (what, a["got_s"], a["limit_s"], a["base_s"], a["ratio"], guess))

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
