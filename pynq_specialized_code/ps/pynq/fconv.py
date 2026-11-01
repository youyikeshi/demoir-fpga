"""
fconv.py —— convfp（float32 直接卷积）PL 单元的 PS 侧驱动

对应 RTL：`pl/rtl_hand/modules/fconv_axi_wrap.v`
  （fconv_axi_wrap → fconv_stream_wrap → fconv_core，8 lane 或 4 lane）
寄存器映射见该文件头 §1；本文件的常量与它一一对应，改 RTL 必须同步改这里。

设计原则（照抄 uni_engine.py，2026-10-30 板子挂死事故后定下的规矩）：
  1. 启动前校验每个物理地址：非 0、4 KB 对齐、落在 DDR 区间内
  2. 按公式算出引擎会写多少字节，确认缓冲够大，尾部放 0xA5 金丝雀
  3. start 之后限时等待；超时立刻抛异常，**绝不再发第二次 start**
  4. 跑完验证金丝雀没被写坏（越界检测）

⚠ **逐位判据**：本引擎的输出必须与 **板端 K 串行模型** 逐位相同：
      from bin_ref_numpy import conv2d_f32          # 板端走 ATLAS 的 K 串行 gemm
      y_ref = conv2d_f32(x, w, b, pad=pad)
      assert np.array_equal(y_pl.view(np.uint32), y_ref.view(np.uint32))
  主机上的 numpy/OpenBLAS **不能**当 golden（K 累加顺序不同 ⇒ 舍入不同）。
  xsim 侧的等价 golden 是 `gen_fconv_tests.py::golden`（也是 K 串行）。

用法：
    from fconv import FconvEngine
    eng = FconvEngine(ol.fconv_0)
    y = eng.launch(x_buf, w_buf, b_buf, cin=64, cout=12, k=3, stride=1, pad=1,
                   H=16, W=32, relu=0, bias_en=1)
"""
import time

import numpy as np

# ---------------------------------------------------------------- 寄存器常量
# PS 侧看的基址（BD 里 fconv_0 的 128K 窗口）：0x4008_0000
# A[16]=1 → 配置页（+0x10000）；A[16]=0 → 基址页
CFG = 0x10000
DDR_LO, DDR_HI = 0x0010_0000, 0x4000_0000     # 1 GB DDR 里给 CMA 的合理区间

OFF = dict(AP_CTRL=0x00, MODE=0x08, AP_RET=0x10,
           H=0x18, W=0x20, CIN=0x38, COUT=0x40, K=0x48,
           S=0x50, D=0x58, PAD=0x60, RELU=0x70, BIAS_EN=0x74, NLANE=0x78,
           STATUS=0x94,
           DBG_RD=0xE0, DBG_LINE=0xE4, DBG_WR=0xE8, DBG_ST=0xEC, DBG_FLAG=0xF0)
BASE = dict(X=0x10, W=0x1C, B=0x28, Y=0x34)   # 每个基址 8 字节（低字在偏移、高字 +4）

# AP_RET / STATUS 的位域
DONE = 1 << 0
BUSY = 1 << 1
ERR_BIT = 1 << 2
PIPE_IDLE = 1 << 16

# 错误码（AP_RET[15:8]）
ERR = {0: "OK", 1: "ERR_RANGE（H/W/s/d/cin/cout 越界，或 W 不是 32 的倍数）",
       2: "ERR_TAP（k 必须是 3 或 5；k=1 不支持）",
       3: "ERR_WCAP（cin·k² > 1024，wbank 装不下）",
       4: "ERR_ALIGN（某个基址不是 8 字节对齐）",
       5: "ERR_ADDR（某个基址高 32 位非 0）"}

# 模式 A/B
MODE_DDR, MODE_STREAM = 0, 1


class _Guard:
    """地址/尺寸/金丝雀的统一守卫（与 uni_engine._Guard 同款）。"""

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
        return pa

    @staticmethod
    def canary_check(buf, n_used, tag):
        n = len(buf)
        canary_start = n - 4096
        if n_used > canary_start:
            raise ValueError(f"{tag}: 需要 {n_used} B，但缓冲只有 {canary_start} B 可写")
        tail = np.frombuffer(buf, dtype=np.uint8)[canary_start:]
        bad = int((tail != 0xA5).sum())
        if bad:
            raise RuntimeError(f"{tag}: 金丝雀被写坏 {bad}/{len(tail)} 字节 "
                               f"⇒ 引擎越界写，立即停机排查")
        return True


class FconvEngine:
    """fconv_0：float32 直接卷积（K 串行、无 FMA、strided=1）。

    支持：k ∈ {3,5}、stride=1、dilation=1、pad=(k-1)/2、relu、bias。
    """

    def __init__(self, ip):
        self.ip = ip
        self._mode = MODE_DDR

    # ---- 底层访问
    def _wr(self, off, v):
        self.ip.write(int(off), int(v) & 0xFFFFFFFF)

    def _rd(self, off):
        return self.ip.read(int(off)) & 0xFFFFFFFF

    # ---- 自检
    def nlanes(self):
        """读 R_NLANE：8 = 满配（4 层 456.7 ms）/ 4 = 省 LUT 档（807.5 ms）。"""
        return self._rd(CFG + OFF["NLANE"])

    def set_mode(self, mode):
        """0 = 模式 A（x/y 走 DDR，standalone 验证用）
           1 = 模式 B（x 从 s_axis_x 流入、y 从 m_axis_y 流出，融合用）"""
        if mode not in (MODE_DDR, MODE_STREAM):
            raise ValueError(f"mode={mode} 只能是 0(A) 或 1(B)")
        self._mode = mode
        # bit1 = 读预取使能（默认开；关掉只用于性能对比实验）
        self._wr(CFG + OFF["MODE"], (mode & 1) | 0x2)

    def set_pfoff(self, off=True):
        """只关预取（性能对比实验用，不影响正确性）。"""
        self._wr(CFG + OFF["MODE"], (self._mode & 1) | (0 if off else 0x2))

    def set_base(self, name, pa):
        pa = _Guard.check_addr(pa, f"B_{name}")
        self._wr(BASE[name], pa)
        self._wr(BASE[name] + 4, 0)          # 高字必须 0（引擎只支持 32 位地址）

    def config(self, *, H, W, cin, cout, k, stride=1, pad=None,
               relu=0, bias_en=1):
        """形状配置。**W 必须是 32 的倍数**（core 按 TW=32 切 tile）。"""
        if k not in (3, 5):
            raise ValueError(f"k={k} 非法：fconv 只支持 3 / 5（k=1 无 k² 常数乘）")
        pad_chk = (k - 1) // 2
        pad = pad_chk if pad is None else int(pad)
        if pad != pad_chk:
            raise ValueError(f"pad={pad} 必须 = (k-1)/2 = {pad_chk}（core 内部硬校验）")
        if stride != 1:
            raise ValueError("fconv 只支持 stride=1（core 没有 stride 端口）")
        if W % 32:
            raise ValueError(f"W={W} 必须是 32 的倍数（tile 宽 TW=32）")
        if not (1 <= cin <= 64):
            raise ValueError(f"cin={cin} 越界（1..64）")
        if not (1 <= cout <= 48):
            raise ValueError(f"cout={cout} 越界（1..48 = 6 组 × 8 lane）")
        if cin * k * k > 1024:
            raise ValueError(f"cin·k² = {cin*k*k} > 1024（wbank 深度，RTL 会回 err=3）")
        for key, val in dict(H=H, W=W, CIN=cin, COUT=cout, K=k, S=stride,
                             D=1, PAD=pad, RELU=int(bool(relu)),
                             BIAS_EN=int(bool(bias_en))).items():
            self._wr(CFG + OFF[key], val)

    # ---- 尺寸公式（与 RTL 一致）
    @staticmethod
    def out_shape(cin, cout, k, stride, pad, H, W):
        """Hout = (H-1)/s + 1（fconv 不做 dilation，pad 只是边界偏移）。"""
        if stride != 1:
            raise ValueError("fconv 只支持 stride=1")
        return H, W

    @staticmethod
    def out_bytes(cout, H, W):
        return cout * H * W * 4

    def start(self):
        self._wr(CFG + OFF["AP_CTRL"], 1)

    def wait(self, timeout=120.0):
        t0 = time.time()
        while True:
            st = self._rd(CFG + OFF["AP_RET"])
            if st & DONE:
                return st, time.time() - t0
            if time.time() - t0 > timeout:
                raise TimeoutError(
                    f"fconv 超时 {timeout}s（已发过一次 start，**不再发第二次**，人工介入）")
            time.sleep(1e-4)

    def status(self):
        return self._rd(CFG + OFF["STATUS"])

    def dbg(self):
        rd = self._rd(CFG + OFF["DBG_RD"])
        ln = self._rd(CFG + OFF["DBG_LINE"])
        wr = self._rd(CFG + OFF["DBG_WR"])
        st = self._rd(CFG + OFF["DBG_ST"])
        return dict(hit=rd >> 16, rdreq=rd & 0xFFFF,
                    nAR=ln >> 16, fills=ln & 0xFFFF,
                    nW=wr >> 16, nAW=wr & 0xFFFF,
                    nR=st >> 16, stall=st & 0xFFFF,
                    flag=self._rd(CFG + OFF["DBG_FLAG"]))

    def clear_dbg(self):
        for k in ("DBG_RD", "DBG_LINE", "DBG_WR", "DBG_ST"):
            self._wr(CFG + OFF[k], 0)
        self._wr(CFG + OFF["DBG_FLAG"], 1)     # bit0=1 清 sticky

    # ---- 主入口
    def launch(self, x, w, b, y=None, *, cin=None, cout=None, k=None,
               stride=1, pad=None, H=None, W=None,
               relu=0, bias_en=1, timeout=120.0):
        """跑一层卷积，返回 y（shape (cout,H,W) float32）。

        参数
        ----
        x : pynq Buffer，布局 [cin][H][W] float32（**连续、8 字节对齐**）
        w : pynq Buffer，布局 [cout][cin][k][k] float32（连续）
        b : pynq Buffer，[cout] float32；bias_en=0 时可传 None（不读，但地址仍需合法）
        y : pynq Buffer 或 None。None ⇒ 本函数用 pynq.allocate 自己分配
            （缓冲尾部自动留 4 KB 金丝雀）；给了就复用（必须够大且尾部留 4 KB）
        其余见 config()；返回的是 y 的 (cout,H,W) float32 **视图**（零拷贝）
        """
        if cin is None or cout is None or k is None or H is None or W is None:
            raise ValueError("cin/cout/k/H/W 必须显式给出（本驱动不做形状推断）")
        need = self.out_bytes(cout, H, W)
        if y is None:
            import pynq                                    # 只在板端需要
            y = pynq.allocate(need + 4096, dtype=np.uint8)
            np.frombuffer(y, dtype=np.uint8)[need:need + 4096] = 0xA5
        if need > len(y) - 4096:
            raise ValueError(f"输出缓冲太小：需要 {need} B + 4 KB 金丝雀，实际 {len(y)} B")
        self.set_mode(self._mode)
        self.set_base("X", x.physical_address)
        self.set_base("W", w.physical_address)
        self.set_base("B", (b if b is not None else w).physical_address)
        self.set_base("Y", y.physical_address)
        self.config(H=H, W=W, cin=cin, cout=cout, k=k, stride=stride,
                    pad=pad, relu=relu, bias_en=bias_en)
        self.clear_dbg()
        self.start()
        st, dt = self.wait(timeout)
        err = (st >> 8) & 0xFF
        if err:
            raise RuntimeError(f"fconv 报错 errcode={err}：{ERR.get(err, '未知')}")
        _Guard.canary_check(y, need, "fconv Y")
        self.last_seconds = dt
        self.last_dbg = self.dbg()
        return np.frombuffer(y, dtype=np.float32, count=cout * H * W).reshape(cout, H, W)

    # ---- 板端等价性自检（K 串行 golden）
    def check_bitwise(self, y_pl, y_ref):
        """逐位比较（uint32）。返回 (ok, nbad, first_bad_index)。"""
        a = np.ascontiguousarray(y_pl, np.float32).view(np.uint32)
        c = np.ascontiguousarray(y_ref, np.float32).view(np.uint32)
        if a.shape != c.shape:
            raise ValueError(f"形状不一致 PL={a.shape} ref={c.shape}")
        eq = (a == c) | (np.isnan(a.view(np.float32)) & np.isnan(c.view(np.float32)))
        nbad = int((~eq).sum())
        first = int(np.argmin(eq)) if nbad else -1
        return (nbad == 0), nbad, first


# ---------------------------------------------------------------------------
# 板端自检脚本用的小工具（不依赖 PYNQ 也能 import）
# ---------------------------------------------------------------------------
def pad_of(k):
    return (k - 1) // 2


def pack_weights(w):
    """(cout,cin,k,k) float32 → 连续 float32 缓冲（与 RTL 的 [oc][ic·k²] 一致）。"""
    w = np.ascontiguousarray(w, np.float32)
    assert w.ndim == 4 and w.shape[2] == w.shape[3], f"w 形状必须 (cout,cin,k,k)，现在是 {w.shape}"
    return np.ascontiguousarray(w.reshape(w.shape[0], -1))
