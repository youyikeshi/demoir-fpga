"""
uni_engine.py —— 新架构（conv_uni_0 / bin_unit_0）的 PS 侧驱动

设计原则（2026-10-30 板子挂死事故后）：**任何让 PL 直接访存的操作，启动前必须**：
  1. 校验每个物理地址：非 0、4 KB 对齐、落在 DDR 区间内
  2. 按自己的公式算出引擎会写多少字节，确认缓冲够大，并在尾部放 0xA5 金丝雀
  3. start 之后限时等待；超时立刻抛异常，**绝不再发第二次 start**
  4. 跑完验证金丝雀没被写坏（越界检测）

寄存器映射来自 RTL 注释（conv_uni_axi_wrap.v / bin_unit_axi_wrap.v 的 §寄存器映射）。
"""
import time
import numpy as np

CFG = 0x10000          # A[16]=1 → 配置页
DDR_LO, DDR_HI = 0x0010_0000, 0x4000_0000     # 1 GB DDR 里给 CMA 的合理区间

# ---------------------------------------------------------------- conv_uni_0
CONV_OFF = dict(AP_CTRL=0x00, AP_RET=0x10, H=0x18, W=0x20, CIN=0x38, COUT=0x40,
                K=0x48, S=0x50, D=0x58, PAD=0x60, SHIFT=0x68, RELU=0x70,
                TAP=0x74, DBG_RD=0xE0, DBG_AR=0xE4, DBG_WR=0xE8, DBG_FLAG=0xF0)
CONV_BASE = dict(IN=0x10, WGT=0x1C, BIAS=0x28, OUT=0x34)

# ---------------------------------------------------------------- 权重容器步长
# 每个 (o,c) 占 TAP 字节，前 k² 字节是真权重（j = ky*k + kx），其余是补齐洞：
#
#     地址 = base_wgt + ((o*cin + c)*TAP + j)
#
# 2026-10-30 时序优化：TAP 从 k²+3 改成 8·ceil(k²/8)
#   padded=True （新，默认）TAP = 8·ceil(k²/8) ∈ {8,16,32}
#       每个 (o,c) 块恰好 8 字节的整数倍 ⇒ 引擎读地址是纯 +8 累加、低 3 位恒 0，
#       可以从关键路径上删掉三级串联加法链（−11.2 ns 违例里最大的一段，占 38%）。
#   padded=False（旧，兼容）TAP = k²+3 ∈ {4,12,28}
#
# 两套布局只有"补齐洞"宽度不同，j < k² 的字节逐字节相同 ⇒ 权重数值不用
# 重新量化，只要按新步长 repack（或重新导出）。⚠ TAP 必须与比特流一致：
# 送错值时 conv_uni_axi_wrap 会返回 ERR_TAP(2)，不会静默算错。
TAP_OF_K        = {1: 8,  3: 16, 5: 32}     # 新：8·ceil(k²/8)
TAP_OF_K_LEGACY = {1: 4,  3: 12, 5: 28}     # 旧：k²+3


def tap_of_k(k, padded=True):
    """TAP(k)。padded=True → 8·ceil(k²/8)（新，默认）；False → k²+3（旧，兼容）。"""
    return tap_of_k_choices(k)[0 if padded else 1]


def tap_of_k_choices(k):
    """k 的所有合法 TAP 值 (新, 旧)，给 config() 做交叉校验用。"""
    if k not in TAP_OF_K:
        raise ValueError(f"k={k} 非法（只支持 1/3/5）")
    return TAP_OF_K[k], TAP_OF_K_LEGACY[k]


class _Guard:
    """地址/尺寸/金丝雀的统一守卫。"""
    @staticmethod
    def check_addr(pa, name):
        if pa is None:
            raise ValueError(f"{name}: 物理地址为 None，拒绝启动")
        pa = int(pa)
        if pa == 0:
            raise ValueError(f"{name}: 物理地址为 0，拒绝启动（会让引擎写内核内存）")
        if pa & 0xFFF:
            raise ValueError(f"{name}: 物理地址 {pa:#x} 未 4 KB 对齐")
        if not (DDR_LO <= pa < DDR_HI):
            raise ValueError(f"{name}: 物理地址 {pa:#x} 不在 DDR 区间 "
                             f"[{DDR_LO:#x},{DDR_HI:#x})，拒绝启动")
        if pa + (1 << 32) >= (1 << 33):
            raise ValueError(f"{name}: 超出 32 位")
        return pa

    @staticmethod
    def canary_check(buf, n_used, tag):
        """buf 尾部 4 KB 应为 0xA5；被改过说明引擎越界写了。"""
        n = len(buf)
        canary_start = n - 4096
        if n_used > canary_start:
            raise ValueError(f"{tag}: 需要 {n_used} B 但缓冲只有 {canary_start} B 可写，"
                             f"拒绝启动")
        tail = np.frombuffer(buf, dtype=np.uint8)[canary_start:]
        bad = int((tail != 0xA5).sum())
        if bad:
            raise RuntimeError(f"{tag}: 金丝雀被写坏 {bad}/{len(tail)} 字节 "
                               f"⇒ 引擎越界写，立即停机排查")
        return True


class ConvUniEngine:
    """conv_uni_0：统一 int8 卷积（k=1/3/5, s=1/2, d=1/2/3）。

    权重容器步长 TAP 见 ``tap_of_k()``：默认新格式 8·ceil(k²/8)，
    ``padded=False`` 走旧格式 k²+3（与 2026-10-30 之前的比特流配套）。
    """

    def __init__(self, ip):
        self.ip = ip

    # ---- 底层访问
    def _wr(self, off, v):
        self.ip.write(int(off), int(v) & 0xFFFFFFFF)

    def _rd(self, off):
        return self.ip.read(int(off)) & 0xFFFFFFFF

    def set_base(self, name, pa):
        pa = _Guard.check_addr(pa, f"B_{name}")
        self._wr(CONV_BASE[name], pa)
        self._wr(CONV_BASE[name] + 4, 0)          # 高字必须 0（引擎只支持 32 位）

    def config(self, H, W, cin, cout, k, s=1, d=1, pad=None, shift=0,
               relu=1, tap=None, padded=True):
        """padded=True（默认）：TAP = 8·ceil(k²/8) = {8,16,32}（新）
           padded=False       ：TAP = k²+3       = {4,12,28}（旧，兼容）

        tap 显式给出时只做"这是不是 k 的合法 TAP"的校验（新旧两套都放行），
        真正的匹配检查由 RTL 做（不匹配回 ERR_TAP=2）——这样验证期可以
        在两套布局之间来回切而不用改调用点。
        """
        pad = k // 2 if pad is None else pad
        tap_new, tap_old = tap_of_k_choices(k)
        if tap is None:
            tap = tap_new if padded else tap_old
        elif tap not in (tap_new, tap_old):
            raise ValueError(f"tap={tap} 与 k={k} 不匹配"
                             f"（合法值：{tap_new} 新 / {tap_old} 旧）")
        for key, val in dict(H=H, W=W, CIN=cin, COUT=cout, K=k, S=s, D=d,
                             PAD=pad, SHIFT=shift, RELU=relu, TAP=tap).items():
            self._wr(CFG + CONV_OFF[key], val)

    @staticmethod
    def out_bytes(cin, cout, k, s, d, pad, H, W):
        """引擎会写多少字节（C8 布局：通道向上取 8 的倍数）。

        ⚠ 面积公式以 RTL 为准（conv_int8_uni.v 第 235-236 行）：
            Hout = (H-1)//s + 1 ；Wout = (W-1)//s + 1
        **不含** pad/dilation 修正 —— 补边是边界偏移，输出分辨率只由 stride 决定。
        踩过：按 (H+2p-keff)//s+1 算，d=3 那个 golden 会算成 2304 而实际是 4096。
        """
        Hout = (H - 1) // s + 1
        Wout = (W - 1) // s + 1
        cpad = ((cout + 7) // 8) * 8
        return (cpad // 8) * Hout * Wout * 8, Hout, Wout

    def start(self):
        self._wr(CFG + CONV_OFF["AP_CTRL"], 1)

    def wait(self, timeout=30.0):
        t0 = time.time()
        while True:
            st = self._rd(CFG + CONV_OFF["AP_RET"])
            if st & 1:
                return st, time.time() - t0
            if time.time() - t0 > timeout:
                raise TimeoutError(f"conv_uni 超时 {timeout}s（不再发 start，人工介入）")
            time.sleep(2e-4)

    def dbg(self):
        ar = self._rd(CFG + CONV_OFF["DBG_AR"])
        wr = self._rd(CFG + CONV_OFF["DBG_WR"])
        return dict(nAR=ar & 0xFFFF, nR=ar >> 16, nAW=wr & 0xFFFF,
                    nW=wr >> 16, flags=self._rd(CFG + CONV_OFF["DBG_FLAG"]))

    def run(self, in_buf, wgt_buf, bias_buf, out_buf, *,
            H, W, cin, cout, k, s=1, d=1, pad=None, shift=0, relu=1, tap=None,
            padded=True, timeout=30.0):
        """padded/tap 语义见 ``config()``：默认新容器 TAP'=8·ceil(k²/8)。"""
        pad = k // 2 if pad is None else pad
        need, Hout, Wout = self.out_bytes(cin, cout, k, s, d, pad, H, W)
        if need > len(out_buf) - 4096:
            raise ValueError(f"输出缓冲太小：需要 {need} B + 4 KB 金丝雀，"
                             f"实际 {len(out_buf)} B")
        self.set_base('IN', in_buf.physical_address)
        self.set_base('WGT', wgt_buf.physical_address)
        self.set_base('BIAS', bias_buf.physical_address)
        self.set_base('OUT', out_buf.physical_address)
        self.config(H=H, W=W, cin=cin, cout=cout, k=k, s=s, d=d, pad=pad,
                    shift=shift, relu=relu, tap=tap, padded=padded)
        self.start()
        st, dt = self.wait(timeout)
        err = (st >> 8) & 0xFF
        if err:
            raise RuntimeError(f"conv_uni 报错 errcode={err}"
                               f"（1=范围 2=tap 3=地址）")
        _Guard.canary_check(out_buf, need, "conv_uni OUT")
        return st, dt, (Hout, Wout)


# ---------------------------------------------------------------- bin_unit_0
BIN_OFF = dict(AP_CTRL=0x00, AP_RET=0x10, C=0x18, C_HI=0x1C, H=0x20, W=0x28,
               HO=0x30, WO=0x38, K=0x40, S=0x48, D=0x50, PAD=0x58, SHIFT=0x60,
               ALPHA_SHIFT=0x68, SLOPE_SHIFT=0x6C, COUT=0x70,
               B_IN=0x80, B_IN2=0x88, B_IN3=0x90, B_IN4=0x98, B_WGT=0xA0,
               B_W2=0xA8, B_W3=0xAC, B_BIAS=0xB0, B_ZETA=0xB8, B_GAMMA=0xBC,
               B_SLOPE=0xC0, B_OUT=0xD0, B_ALPHA=0xD8,
               DBG_RD=0xE0, DBG_WR=0xE8, DBG_FLAG=0xF0)

BIN_OP = {0: 'CONV', 1: 'RPRELU', 2: 'SAM', 3: 'INTERP'}


class BinUnitEngine:
    """bin_unit_0：二值 conv / rprelu / sam / interp 四个单元共用一个 AXI 从机。"""

    def __init__(self, ip):
        self.ip = ip

    def _wr(self, off, v):
        self.ip.write(int(off), int(v) & 0xFFFFFFFF)

    def _rd(self, off):
        return self.ip.read(int(off)) & 0xFFFFFFFF

    # RTL（bin_unit_axi_wrap.v:461-468 / 549-560）里**只有 8 个基址有高字槽**：
    #   0x84 B_IN / 0x8C B_IN2 / 0x94 B_IN3 / 0x9C B_IN4
    #   0xA4 B_WGT / 0xB4 B_BIAS / 0xD4 B_OUT / 0xDC B_ALPHA
    # 而 ZETA(0xB8) / GAMMA(0xBC) / SLOPE(0xC0) 是 32 位**紧挨着**的，没有高字槽：
    #   写 0xB8+4=0xBC 会把 GAMMA 清零，写 0xBC+4=0xC0 会把 SLOPE 清零
    #   ⇒ 引擎判 ERR_NULL(1) 直接拒跑。SLOPE+4=0xC4 未定义（无害但无意义）。
    _BIN_HI_SLOTS = frozenset({'IN', 'IN2', 'IN3', 'IN4', 'WGT', 'BIAS', 'OUT', 'ALPHA'})

    def set_base(self, name, pa):
        pa = _Guard.check_addr(pa, f"{name}")
        self._wr(CFG + BIN_OFF[name], pa)
        if name in self._BIN_HI_SLOTS:
            self._wr(CFG + BIN_OFF[name] + 4, 0)   # 只有这 8 个有高字槽
        # ZETA/GAMMA/SLOPE 没有高字：绝不写 +4，否则毁掉下一个基址

    def select_op(self, op):
        """选单元：0 conv / 1 rprelu / 2 sam / 3 interp。
        由 R_C 之外的保留寄存器承载——用 AP_CTRL 的高位（bit8..9）选单元。"""
        if op not in BIN_OP:
            raise ValueError(f"op={op} 非法")
        self._op = op

    def start(self):
        self._wr(CFG + BIN_OFF['AP_CTRL'], 1 | (getattr(self, '_op', 0) << 8))

    def wait(self, timeout=30.0):
        t0 = time.time()
        while True:
            st = self._rd(CFG + BIN_OFF['AP_RET'])
            if st & 1:
                return st, time.time() - t0
            if time.time() - t0 > timeout:
                raise TimeoutError(f"bin_unit 超时 {timeout}s")
            time.sleep(2e-4)

    def dbg(self):
        rd = self._rd(CFG + BIN_OFF['DBG_RD'])
        wr = self._rd(CFG + BIN_OFF['DBG_WR'])
        return dict(nRD=rd & 0xFFFF, nFill=rd >> 16, nAW=wr & 0xFFFF,
                    nW=wr >> 16, flags=self._rd(CFG + BIN_OFF['DBG_FLAG']))
