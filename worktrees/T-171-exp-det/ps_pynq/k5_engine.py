#!/usr/bin/env python3
"""PYNQ 驱动：手写 k=5 卷积加速器 ``conv_k5_engine``（AXI-Lite 包装 ``k5_axi_wrap``）。

> **状态（2026-10-27）**：IP **还没进 BD**（`smc` 的 S06/S07 是预留给它的），
> 所以本文件是"等 IP 落地即可用"的驱动 + 路由代码，**未上板**。
> 基地址按任务书预留 ``0x4008_0000``（128KB 对齐）；IP 进 BD 后
> **一律以 .hwh 发现为准**，发现失败就报错（不猜）。

与 k3/k1 的关系
---------------
寄存器映射沿用 k3（k3 又是照 k1 的表做的），所以 PS 侧只换基地址/能力判定：

    offs 页（A[16]=0）                    ctrl 页（A[16]=1）
      0x10 IN_ADDR_LO/HI                    0x00 AP_CTRL  [0]start
      0x1C WGT_ADDR_LO/HI                   0x10 AP_RETURN = 错误码
      0x28 BIAS_ADDR_LO/HI                  0x18..0x70 H W HOUT WOUT CIN COUT
      0x34 OUT_ADDR_LO/HI                          KSIZE STRIDE DIL PAD SHIFT RELU
                                            0x94 STATUS [0]done [1]busy [2]err
                                                       [7:4]err_code [8]idle [9]sticky_oob
                                            0x98/0x9C WIN_LO / WIN_HI
                                            0xA0/0xA4 STALL_FILL / STALL_MAC
                                            0xA8/0xAC/0xB0 自检/四区/bytes_in

**"同结构"是按 k3 的 wrapper 约定的**：k5 的 wrapper 还没写，上面这些
k3 新增寄存器（窗口/sticky/诊断）**假设** k5 会照做。如果最终 k5 wrapper
没有窗口寄存器，用 ``K5Engine(..., set_window=False)`` 关掉写窗口；
没有 sticky 位就把 ``strict_oob`` 留 True 也不会误报（那一位恒 0）。

k5 与 k1/k3 最大的不同：**运行期形状不参与计算**
------------------------------------------------
``conv_k5_engine`` 的 ``CIN/COUT/W/H/PAD/OC_PER_PASS`` 全是**编译期参数**
（`pl/rtl_hand/modules/conv_k5_engine.v` 顶部），``ctrl_cin/ctrl_cout/
ctrl_h/ctrl_w`` 只作契约保留、不参与地址或循环。所以：

* 驱动把 IP 的编译期参数记成 ``ip_cin/ip_cout/ip_h/ip_w``
  （默认 12/48/320/320，可用 K5_IP_CIN/COUT/H/W 覆盖），
* ``eligibility()`` **要求运行期形状与它逐一相等** —— 否则 RTL 会拿固定
  的 320×320×12→48 去算，输出"看着正常"但整网崩（本项目最贵的教训）。
  真要把 k5 引擎复用到别的形状，必须**重新综合**并改这几个默认值。

另外 k5 的 ReLU 是**硬连线**的（引擎收尾 ``if (q_v<0) 0``，没有 relu 端口），
所以 ``relu=False`` 的层不可路由。

权重容器
--------
**PAD28**：每个 ``(oc, ic)`` 28 字节 = 25 抽头 + 3 零（7 个 32-bit 字）::

    offset(oc, ic, ky, kx) = (oc*cin + ic)*28 + ky*5 + kx

与 ``ref_numpy.k5_to_pad28`` / ``tools/repack_k5_pad28.py`` 同一定义。
"""

import os
import time

# ---------------------------------------------------------------- 常量

#: BD 实例名（IP 进 BD 时**务必**用这个名字；发现逻辑也兼容
#: ``k5_axi_wrap`` / ``conv_k5_engine``）。
DEFAULT_IP_NAME = "k5_wrap"

#: AXI-Lite 从机窗口大小：128KB。
WINDOW_SIZE = 0x20000
#: 预留地址（任务书）；k5 进 BD 后以 .hwh 为准。
#: 参考：k1=0x4004_0000、k3=0x4006_0000（都已实测）；0x4008_0000 是下一个
#: 128KB 空位（0x400A_0000 起留给别的）。
DEFAULT_BASE = 0x40080000
CTRL_PAGE_OFF = 0x10000

# ---- ctrl 页寄存器偏移（与 k1/k3 一致；k5 wrapper 应照抄）----
AP_CTRL, AP_RETURN = 0x00, 0x10
R_H, R_W, R_HOUT, R_WOUT = 0x18, 0x20, 0x28, 0x30
R_CIN, R_COUT, R_KSIZE = 0x38, 0x40, 0x48
R_STRIDE, R_DIL, R_PAD, R_SHIFT = 0x50, 0x58, 0x60, 0x68
R_RELU = 0x70
R_STATUS = 0x94
# k3 新增、k5 假设沿用（见文件头"同结构"）
R_WIN_LO, R_WIN_HI = 0x98, 0x9C
R_STALL_FILL, R_STALL_MAC = 0xA0, 0xA4
R_SELFTEST = 0xA8
R_WIN4 = 0xAC
R_BYTES_IN = 0xB0

# ---- offs 页寄存器偏移（四个 64 位缓冲区地址）----
B_IN_FM, B_WGT, B_BIAS, B_OUT_FM = 0x10, 0x1C, 0x28, 0x34

AP_START = 0x1

ST_DONE = 1 << 0
ST_BUSY = 1 << 1
ST_ERR = 1 << 2
ST_ERR_CODE_SHIFT = 4
ST_ERR_CODE_MASK = 0xF
ST_IDLE = 1 << 8
ST_STICKY_OOB = 1 << 9

RELU_BIT, C8_IN_BIT, C8_OUT_BIT = 0, 2, 3

# 本 IP 的**编译期**能力（conv_k5_engine.v 的参数默认值）。
# 用 K5_IP_CIN / K5_IP_COUT / K5_IP_H / K5_IP_W / K5_IP_OC_PER_PASS 覆盖，
# **不要改这里的默认值**（它们必须与综合时用的参数一致）。
K5_KSIZE = 5
K5_CIN = 12
K5_COUT = 48
K5_H = 320
K5_W = 320
K5_PAD = 2
K5_STRIDE = 1
K5_DIL = 1
K5_OC_PER_PASS = 1
K5_SHIFT_MIN, K5_SHIFT_MAX = 0, 31
K5_WGT_BYTES_PER_IC = 28         # PAD28：25 抽头 + 3 零
K5_TAPS = 25

#: 权重容器布局标签（index.json 的 ``layers[key]["w_layout"]``）。
W_LAYOUT_K5 = "k5_pad28"

# AP_RETURN 的错误码表。k5 的 wrapper 还没实现；**沿用 k3 的 cfg_check 表**
# 作为约定（k5 引擎自己没有 cfg_check，所以这些码要么由 wrapper 给，要么
# 永远为 0 —— PS 侧的 check_supported 是唯一一道防线）。
# 等 IP 进 BD 后按实际 wrapper 核对这张表。
ERR_MEANING = {
    0x0: "成功",
    0x1: "cin 非法（本 IP 固定 12）",
    0x2: "cout 非法（本 IP 固定 48）",
    0x3: "stride 非法（本 IP 固定 1）",
    0x4: "dilation 非法（本 IP 固定 1）",
    0x5: "H/W/pad/ksize 非法（本 IP 固定 320×320 / pad=2 / k=5）",
    0x6: "Hout/Wout 非正",
    0x7: "shift > 31",
    0x8: "槽容量/行缓冲不足",
    0x9: "不支持的 stride/dilation 组合",
    0xA: "地址窗口越界（四个区必须整体落在 [WIN_LO, WIN_HI] 内）",
}


# ---------------------------------------------------------------- 异常

class K5Error(RuntimeError):
    """K5 IP 相关的所有异常基类（上层捕获它就够）。"""


class K5Unsupported(K5Error):
    """本层参数不在 K5 IP 的支持范围内（**在碰硬件之前**抛出）。"""


class K5Timeout(K5Error):
    """等待 done 超时。"""


class K5ReturnError(K5Error):
    """AP_RETURN 非 0（RTL 启动了但拒绝/算错）。"""

    def __init__(self, msg, return_code=None, status=None):
        super().__init__(msg)
        self.return_code = return_code
        self.status = status


# ---------------------------------------------------------------- 小工具

def _env_flag(name, default=False):
    v = os.environ.get(name, "")
    s = str(v).strip().lower()
    if s == "":
        return bool(default)
    return s not in ("0", "false", "off", "no")


def _env_float(name, default):
    v = os.environ.get(name, "")
    if str(v).strip() == "":
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    v = os.environ.get(name, "")
    if str(v).strip() == "":
        return int(default)
    try:
        return int(str(v), 0)
    except (TypeError, ValueError):
        return int(default)


def fclk_hz():
    """当前假定的 PL 时钟（Hz）。

    优先 K5_IP_FCLK_HZ / K5_IP_FCLK_MHZ；没有再退回 K3_IP_* / K1_IP_*
    （同一个 FCLK0 驱动三个 IP，板上一条环境变量就够）。
    """
    for pfx in ("K5_IP", "K3_IP", "K1_IP"):
        if str(os.environ.get(pfx + "_FCLK_HZ", "")).strip():
            return _env_float(pfx + "_FCLK_HZ", 150e6)
    for pfx in ("K5_IP", "K3_IP", "K1_IP"):
        mhz = _env_float(pfx + "_FCLK_MHZ", 0.0)
        if mhz > 0:
            return mhz * 1e6
    return 150e6


def _buf_addr(buf):
    """取 PYNQ 缓冲区的物理地址（兼容 PYNQ 2.x/3.x 的两种属性名）。"""
    a = getattr(buf, "device_address", None)
    if a is None:
        a = getattr(buf, "physical_address", None)
    if a is None:
        raise K5Unsupported("缓冲区没有 device_address/physical_address 属性：%r"
                            % (type(buf).__name__,))
    return int(a)


def relu_flags(relu=True, c8_in=True, c8_out=True):
    """拼 relu_en：bit0=relu、bit2=输入 C8、bit3=输出 C8（与 k1/k3 同式）。

    k5 引擎的 ReLU 是硬连线的（没有 relu 端口）；这里照写寄存器只是为了
    与 k1/k3 的寄存器约定一致 + 未来 wrapper 可能用它做校验。eligibility
    会**拒绝 relu=False 的层**，所以 bit0 恒为 1。
    """
    return ((1 if relu else 0)
            | (1 << C8_IN_BIT if c8_in else 0)
            | (1 << C8_OUT_BIT if c8_out else 0))


def status_text(st):
    """把 STATUS 值翻译成一行诊断文本。"""
    st = int(st) & 0xFFFFFFFF
    code = (st >> ST_ERR_CODE_SHIFT) & ST_ERR_CODE_MASK
    return ("done=%d busy=%d err=%d err_code=%#x(%s) idle=%d sticky_oob=%d"
            % ((st & ST_DONE) != 0, (st & ST_BUSY) != 0, (st & ST_ERR) != 0,
               code, ERR_MEANING.get(code, "未知错误码"), (st & ST_IDLE) != 0,
               (st & ST_STICKY_OOB) != 0))


def selftest_text(v):
    """翻译 0xA8 自检寄存器（k5 wrapper 若照 k3 做才有意义）。"""
    v = int(v) & 0xFFFFFFFF
    return ("win_ok=%d cfg_ok=%d err=%#x(%s)"
            % ((v >> 5) & 1, (v >> 4) & 1, v & 0xF,
               ERR_MEANING.get(v & 0xF, "未知错误码")))


def win4_text(v):
    """翻译 0xAC 四区窗口结果：{ou[3], bi[2], wg[1], in[0]}。"""
    v = int(v) & 0xFFFFFFFF
    return ("in=%d wgt=%d bias=%d out=%d"
            % (v & 1, (v >> 1) & 1, (v >> 2) & 1, (v >> 3) & 1))


# ---------------------------------------------------------------- 字节/窗口

class BufferBytes(tuple):
    """四个区的字节数（与 k3 同口径，把 12 换成 28）。

    可当 4 元组解包，也可按名字取：``buffer_bytes(...).out_bytes``。
    """

    __slots__ = ()

    def __new__(cls, in_b, wgt_b, bias_b, out_b):
        return super(BufferBytes, cls).__new__(
            cls, (int(in_b), int(wgt_b), int(bias_b), int(out_b)))

    in_bytes = property(lambda self: self[0])
    wgt_bytes = property(lambda self: self[1])
    bias_bytes = property(lambda self: self[2])
    out_bytes = property(lambda self: self[3])


def ceil8(v):
    """向上取整到 8 的倍数（C8 是 8 通道一组）。"""
    v = int(v)
    return ((v + 7) // 8) * 8


def buffer_bytes(H, W, cin, cout, hout=None, wout=None, s=1):
    """四个区的字节数（与 k3 同口径，权重容器换成 PAD28）。

    * ``bytes_in  = ⌈cin/8⌉·8·H·W·8``    —— k5 引擎按 CPAD=⌈cin/8⌉·8 读满
      （cin=12 ⇒ 物理 16 通道，补零通道读出来是 0、不参与乘加）
    * ``bytes_wgt = cout·cin·28``        —— PAD28（25 抽头 + 3 零）
    * ``bytes_bias= cout·4``
    * ``bytes_out = ⌈cout/8⌉·8·Hout·Wout·8``
    """
    H, W, cin, cout = int(H), int(W), int(cin), int(cout)
    s = max(1, int(s))
    hout = ((H - 1) // s + 1) if hout is None else int(hout)
    wout = ((W - 1) // s + 1) if wout is None else int(wout)
    return BufferBytes(ceil8(cin) * H * W * 8,
                       cout * cin * K5_WGT_BYTES_PER_IC,
                       cout * 4,
                       ceil8(cout) * hout * wout * 8)


def window_of(addrs, lens):
    """四个区的 ``[WIN_LO, WIN_HI]``：``min(addr)`` .. ``max(addr+len)``。"""
    addrs = [int(a) for a in addrs]
    lens = [int(n) for n in lens]
    if len(addrs) != len(lens):
        raise ValueError("addrs/lens 长度不一致（%d vs %d）"
                         % (len(addrs), len(lens)))
    if not addrs:
        raise ValueError("addrs 为空")
    return int(min(addrs)), int(max(a + n for a, n in zip(addrs, lens)))


# ---------------------------------------------------------------- 拍数/超时

def theoretical_cycles(H, W, cin, cout, oc_per_pass=K5_OC_PER_PASS, k=5, s=1,
                       hout=None, wout=None):
    """本层理论拍数（conv_k5_engine.v 文件头 §5.5）：

        Hout · Wout · ⌈cout / OC_PER_PASS⌉ · cin

    OC_PER_PASS=1（默认，25 DSP）：320·320·48·12 = **58.98 M 拍 = 393 ms
    @150 MHz**，与 RTL 文件头的数字逐位一致（整层约 0.40 s，含装载/回写）。
    """
    del k
    H, W, cin, cout = int(H), int(W), int(cin), int(cout)
    s = max(1, int(s))
    hout = ((H - 1) // s + 1) if hout is None else int(hout)
    wout = ((W - 1) // s + 1) if wout is None else int(wout)
    ocp = max(1, int(oc_per_pass))
    passes = (cout + ocp - 1) // ocp
    return int(max(0, hout) * max(0, wout) * passes * max(0, cin))


def timeout_for(H, W, cin, cout, k=5, s=1, oc_per_pass=K5_OC_PER_PASS,
                hout=None, wout=None):
    """单层等待上限（秒）= 理论拍数 × 系数 / FCLK + 余量。

    系数默认 3.0、余量 1.0 s（K5_IP_TIMEOUT_FACTOR / K5_IP_TIMEOUT_EXTRA_S）。
    唯一那一层 ≈ 0.39 s ⇒ 默认超时 2.2 s。
    """
    del k
    factor = _env_float("K5_IP_TIMEOUT_FACTOR", 3.0)
    extra = _env_float("K5_IP_TIMEOUT_EXTRA_S", 1.0)
    cycles = theoretical_cycles(H, W, cin, cout, oc_per_pass=oc_per_pass,
                                s=s, hout=hout, wout=wout)
    return max(0.05, factor * cycles / max(fclk_hz(), 1.0) + extra)


# ---------------------------------------------------------------- 支持性判定

def check_supported(H, W, cin, cout, k, s, d, pad, shift, relu=True,
                    c8_in=True, c8_out=True,
                    ip_cin=K5_CIN, ip_cout=K5_COUT, ip_h=K5_H, ip_w=K5_W,
                    hout=None, wout=None):
    """返回**不支持的原因列表**（空列表 = 可以走 K5 IP）。

    纯函数：不导入 pynq、不碰硬件，本机可单测。k5 引擎没有 RTL 侧 cfg_check，
    所以**这里是唯一一道防线**，判据比 k3 更严（编译期特化）：

      · k == 5、stride == 1、dilation == 1、pad == 2（same padding）
      · **relu 必须为 True**（k5 的 ReLU 是硬连线的，没有运行期开关）
      · **cin/cout/H/W 必须逐个等于 IP 的编译期参数**（默认 12/48/320/320）：
        引擎不看 ctrl_cin/ctrl_cout/ctrl_h/ctrl_w，形状不符会拿固定形状去算，
        输出"看着正常"但整网崩 —— 最贵的一类故障
      · ``shift ∈ [0,31]``；输入/输出都是 C8
      · Hout/Wout（给了就校验）必须等于 ``(X-1)//s+1``
    """
    reasons = []
    try:
        H, W = int(H), int(W)
        cin, cout = int(cin), int(cout)
        k, s, d, pad = int(k), int(s), int(d), int(pad)
        shift = int(shift)
    except (TypeError, ValueError):
        return ["参数不是整数（H=%r W=%r cin=%r cout=%r k=%r s=%r d=%r pad=%r "
                "shift=%r）" % (H, W, cin, cout, k, s, d, pad, shift)]

    if k != K5_KSIZE:
        reasons.append("k=%d ≠ %d（本 IP 只做 5×5）" % (k, K5_KSIZE))
    if s != K5_STRIDE:
        reasons.append("stride=%d ≠ %d（本 IP 只做 stride=1）" % (s, K5_STRIDE))
    if d != K5_DIL:
        reasons.append("dilation=%d ≠ %d（本 IP 只做 dilation=1）" % (d, K5_DIL))
    if pad != K5_PAD:
        reasons.append("pad=%d ≠ %d（本 IP 的 5×5 same padding 固定 pad=2）"
                       % (pad, K5_PAD))
    if not bool(relu):
        reasons.append("relu=False（k5 引擎的 ReLU 是硬连线的，没有运行期开关；"
                       "该层必须换引擎）")
    if int(cin) != int(ip_cin):
        reasons.append("cin=%d ≠ 本 IP 编译期 CIN=%d（运行期形状不参与计算，"
                       "换形状必须重新综合）" % (cin, int(ip_cin)))
    if int(cout) != int(ip_cout):
        reasons.append("cout=%d ≠ 本 IP 编译期 COUT=%d（同上）"
                       % (cout, int(ip_cout)))
    if H != int(ip_h) or W != int(ip_w):
        reasons.append("H/W=%dx%d ≠ 本 IP 编译期 %dx%d（同上）"
                       % (H, W, int(ip_h), int(ip_w)))
    if shift < K5_SHIFT_MIN or shift > K5_SHIFT_MAX:
        reasons.append("shift=%d 不在 [%d,%d]"
                       % (shift, K5_SHIFT_MIN, K5_SHIFT_MAX))
    hout_d = ((H - 1) // s + 1) if s > 0 else None
    wout_d = ((W - 1) // s + 1) if s > 0 else None
    if hout is not None and hout_d is not None and int(hout) != hout_d:
        reasons.append("HOUT=%s ≠ (H-1)/s+1 = %d" % (hout, hout_d))
    if wout is not None and wout_d is not None and int(wout) != wout_d:
        reasons.append("WOUT=%s ≠ (W-1)/s+1 = %d" % (wout, wout_d))
    if not bool(c8_in):
        reasons.append("输入不是 C8 布局（本 IP 只支持 C8 交错布局）")
    if not bool(c8_out):
        reasons.append("输出不是 C8 布局（本 IP 只支持 C8 交错布局）")
    return reasons


def eligibility(H, W, cin, cout, k, s, d, pad, shift, relu=True,
                c8_in=True, c8_out=True,
                ip_cin=K5_CIN, ip_cout=K5_COUT, ip_h=K5_H, ip_w=K5_W,
                hout=None, wout=None):
    """``check_supported`` 的非抛出版本：返回 ``(ok, reason)``。"""
    reasons = check_supported(
        H, W, cin, cout, k, s, d, pad, shift, relu=relu,
        c8_in=c8_in, c8_out=c8_out, ip_cin=ip_cin, ip_cout=ip_cout,
        ip_h=ip_h, ip_w=ip_w, hout=hout, wout=wout)
    return (not reasons, "; ".join(reasons))


# ---------------------------------------------------------------- MMIO 分页

class _Page(object):
    """把一整块 128KB 窗口里的某一页包装成 ``read(off)/write(off,val)``。"""

    __slots__ = ("mmio", "base_off")

    def __init__(self, mmio, base_off):
        self.mmio = mmio
        self.base_off = int(base_off)

    def read(self, off=0):
        return int(self.mmio.read(self.base_off + int(off)))

    def write(self, off, val):
        self.mmio.write(self.base_off + int(off), int(val) & 0xFFFFFFFF)


# ---------------------------------------------------------------- 返回值

class K5RunResult(object):
    """``run()`` 的返回值：可解包成 ``(rc, seconds)``，并带诊断字段。

    与 ``K3RunResult`` 同构（见 k3_engine.py 的说明）：
    ``stall_fill`` 大 ⇒ DDR 供数不足；``stall_mac`` 应≈0。
    """

    __slots__ = ("rc", "seconds", "stall_fill", "stall_mac", "bytes_in",
                 "status", "return_code", "layer")

    def __init__(self, rc, seconds, stall_fill=0, stall_mac=0, bytes_in=0,
                 status=0, return_code=0, layer=None):
        self.rc = int(rc)
        self.seconds = float(seconds)
        self.stall_fill = int(stall_fill)
        self.stall_mac = int(stall_mac)
        self.bytes_in = int(bytes_in)
        self.status = int(status)
        self.return_code = int(return_code)
        self.layer = layer

    def __iter__(self):
        return iter((self.rc, self.seconds))

    def __len__(self):
        return 2

    def __getitem__(self, i):
        return (self.rc, self.seconds)[i]

    def as_dict(self):
        return {"rc": self.rc, "seconds": self.seconds,
                "stall_fill": self.stall_fill, "stall_mac": self.stall_mac,
                "bytes_in": self.bytes_in, "status": self.status,
                "return_code": self.return_code, "layer": self.layer}

    def __repr__(self):
        return ("K5RunResult(rc=%d, %.4fs, stall_fill=%d, stall_mac=%d, "
                "bytes_in=%d)" % (self.rc, self.seconds, self.stall_fill,
                                  self.stall_mac, self.bytes_in))


class _Prep(tuple):
    """``_prepare`` 的结果（addrs, lens, win_lo, win_hi, timeout, cycles, ctx）。"""

    __slots__ = ()

    def __new__(cls, addrs, lens, win_lo, win_hi, timeout_s, cycles, ctx):
        return super(_Prep, cls).__new__(
            cls, (addrs, lens, int(win_lo), int(win_hi), float(timeout_s),
                  int(cycles), ctx))

    addrs = property(lambda self: self[0])
    lens = property(lambda self: self[1])
    win_lo = property(lambda self: self[2])
    win_hi = property(lambda self: self[3])
    timeout_s = property(lambda self: self[4])
    cycles = property(lambda self: self[5])
    ctx = property(lambda self: self[6])


# ---------------------------------------------------------------- 驱动

class K5Engine(object):
    """``conv_k5_engine`` 的 AXI-Lite 驱动（128KB 窗口，A[16] 分页）。

    参数
    ----
    overlay : pynq.Overlay
        从 ``ip_dict`` 自动发现 ``k5_wrap`` 的基地址；找不到默认抛错。
    ip_name : str
        BD 实例名，默认 ``k5_wrap``（也接受 ``K5_IP_NAME``）。
    base_addr : int
        显式指定基地址（离线单测 / IP 刚进 BD 时核对地址）。
    mmio_factory : callable
        默认 ``pynq.MMIO``；离线测试注入假实现。
    strict : bool
        True（默认）：找不到 IP / 基址不对齐就抛错。
    strict_oob : bool
        True（默认）：``STATUS[9] sticky_oob`` 置位就抛异常。
    set_window : bool
        True（默认）：每次 run 写 ``WIN_LO/WIN_HI``（前提是 k5 wrapper 照
        k3 做了窗口检查）。若最终 wrapper 没有这两个寄存器，传 False。
    """

    def __init__(self, overlay=None, ip_name=None, base_addr=None,
                 mmio_factory=None, strict=True, strict_oob=None,
                 set_window=None):
        self.ip_name = (ip_name
                        or os.environ.get("K5_IP_NAME", "").strip()
                        or DEFAULT_IP_NAME)
        self.discovered = None
        self.base_addr = None
        self.mmio = None
        self.ctrl = None
        self.offs = None
        self.n_runs = 0
        self.total_seconds = 0.0
        self.total_stall_fill = 0
        self.total_stall_mac = 0
        self.last_status = 0
        self.last_return = None
        self.last_stall_fill = 0
        self.last_stall_mac = 0
        self.last_bytes_in = 0
        self.last_win = (0, 0)
        self.oob_ever = False
        self.strict_oob = (_env_flag("K5_IP_STRICT_OOB", True) if strict_oob
                           is None else bool(strict_oob))
        self.set_window = (_env_flag("K5_IP_SET_WINDOW", True) if set_window
                           is None else bool(set_window))
        # IP 的编译期参数（必须与综合时一致）
        self.ip_cin = _env_int("K5_IP_CIN", K5_CIN)
        self.ip_cout = _env_int("K5_IP_COUT", K5_COUT)
        self.ip_h = _env_int("K5_IP_H", K5_H)
        self.ip_w = _env_int("K5_IP_W", K5_W)
        self.oc_per_pass = max(1, _env_int("K5_IP_OC_PER_PASS",
                                           K5_OC_PER_PASS))
        self.lanes = self.oc_per_pass

        base = int(base_addr) if base_addr is not None else None
        if base is None:
            env_base = os.environ.get("K5_IP_BASE", "").strip()
            if env_base:
                base = int(env_base, 0)
                print("WARN: K5 IP 使用固定基地址 %#x（K5_IP_BASE，跳过 hwh "
                      "发现）——先确认这个窗口真的是 k5_wrap" % base)
            elif overlay is not None:
                found = self.discover(overlay, self.ip_name)
                if found is None:
                    msg = ("overlay 里找不到 K5 IP（实例名按 '%s' 找，也试了 "
                           "k5_axi_wrap/conv_k5_engine）；k5 还没进 BD？"
                           % self.ip_name)
                    if strict:
                        raise RuntimeError(msg)
                    base = DEFAULT_BASE
                    print("WARN: %s → 回退预留基地址 %#x（非常危险：该窗口可能"
                          "根本没映射）" % (msg, base))
                else:
                    base, key, rng = found
                    self.discovered = key
                    print("INFO: K5 IP 发现 '%s' @ %#x（窗口 %#x）"
                          % (key, base, rng))
                    if rng and rng < WINDOW_SIZE:
                        msg = ("hwh 里 k5 条目窗口只有 %#x < 128KB（ctrl 页在 "
                               "+0x10000）" % rng)
                        if self._page2_present(overlay, base):
                            print("INFO: hwh 把 128KB 拆成两页（发现 +0x10000 "
                                  "的同名条目），按整窗口继续")
                        elif strict:
                            raise RuntimeError(msg + "；请把 BD 地址范围改成 128KB")
                        else:
                            print("WARN: " + msg + "；strict=False，继续（危险）")
            else:
                msg = ("没有 overlay 也没有 K5_IP_BASE：无法定位 k5_wrap 的 "
                       "AXI-Lite 基地址（本机静态检查请只 import，不要实例化）")
                if strict:
                    raise RuntimeError(msg)
                base = DEFAULT_BASE
                print("WARN: %s → 回退预留基地址 %#x" % (msg, base))

        self.base_addr = int(base)
        if self.base_addr % WINDOW_SIZE:
            msg = ("K5 基地址 %#x 不是 128KB 对齐：RTL 用 addr[16] 分页"
                   % self.base_addr)
            if strict:
                raise RuntimeError(msg)
            print("WARN: " + msg + "（strict=False，继续）")
        if mmio_factory is None:
            try:
                from pynq import MMIO
            except Exception as exc:                       # noqa: BLE001
                raise RuntimeError(
                    "构造 K5Engine 需要 pynq.MMIO（本机没有 pynq 时只能 import "
                    "本模块做静态检查）：%r" % (exc,))
            mmio_factory = MMIO
        self.mmio = mmio_factory(self.base_addr, WINDOW_SIZE)
        self.ctrl = _Page(self.mmio, CTRL_PAGE_OFF)
        self.offs = _Page(self.mmio, 0)

    # ------------------------------------------------------------ 地址发现

    @staticmethod
    def discover(overlay, ip_name=DEFAULT_IP_NAME):
        """从 ``overlay.ip_dict`` 找 K5 从机，返回 ``(base, key, addr_range)``。

        兼容 128KB 单条目 / 带接口名条目 / 两页 64KB 三种 hwh 解析结果。
        找不到返回 None（不猜地址）。
        """
        aliases = [ip_name, DEFAULT_IP_NAME, "k5_axi_wrap", "conv_k5_engine"]
        exact, loose = [], []
        for key, v in getattr(overlay, "ip_dict", {}).items():
            if not isinstance(v, dict):
                continue
            pa = v.get("phys_addr")
            if pa is None:
                continue
            base = int(pa)
            rng = int(v.get("addr_range") or 0)
            top = str(key).split("/")[0]
            typ = str(v.get("type", "")).lower()
            low = str(key).lower()
            if key == ip_name or top in aliases:
                exact.append((str(key), base, rng))
            elif ("k5_axi_wrap" in typ or "conv_k5_engine" in typ
                  or ("k5" in low and ("wrap" in low or "engine" in low))):
                loose.append((str(key), base, rng))
        cands = exact if exact else loose
        if not cands:
            return None
        big = [c for c in cands if c[2] >= WINDOW_SIZE]
        if big:
            big.sort(key=lambda c: (0 if c[0] == ip_name else 1, c[1]))
            return big[0][1], big[0][0], big[0][2]
        cands.sort(key=lambda c: (c[1], -c[2]))
        return cands[0][1], cands[0][0], cands[0][2]

    @staticmethod
    def _page2_present(overlay, base):
        """hwh 是否把 128KB 窗口拆成了两页。"""
        want = int(base) + WINDOW_SIZE // 2
        for key, v in getattr(overlay, "ip_dict", {}).items():
            if not isinstance(v, dict):
                continue
            pa = v.get("phys_addr")
            if pa is None:
                continue
            try:
                if int(pa) != want:
                    continue
            except (TypeError, ValueError):
                continue
            low = str(key).lower()
            typ = str(v.get("type", "")).lower()
            if ("k5" in low or "wrap" in low
                    or "k5_axi_wrap" in typ or "conv_k5_engine" in typ):
                return True
        return False

    # ------------------------------------------------------------ 寄存器读写

    def status(self):
        st = self.ctrl.read(R_STATUS)
        self.last_status = st
        return st

    def status_text(self):
        return status_text(self.last_status)

    def diag(self):
        """把诊断寄存器全读回来（只读）。k5 wrapper 没做这些寄存器时读到 0。"""
        return {
            "ip": self.discovered or self.ip_name,
            "base": self.base_addr,
            "STATUS": self.ctrl.read(R_STATUS),
            "AP_RETURN": self.ctrl.read(AP_RETURN),
            "WIN_LO": self.ctrl.read(R_WIN_LO),
            "WIN_HI": self.ctrl.read(R_WIN_HI),
            "SELFTEST": self.ctrl.read(R_SELFTEST),
            "WIN4": self.ctrl.read(R_WIN4),
            "BYTES_IN_HW": self.ctrl.read(R_BYTES_IN),
            "STALL_FILL": self.ctrl.read(R_STALL_FILL),
            "STALL_MAC": self.ctrl.read(R_STALL_MAC),
            "IN_LO": self.offs.read(B_IN_FM),
            "WGT_LO": self.offs.read(B_WGT),
            "BIAS_LO": self.offs.read(B_BIAS),
            "OUT_LO": self.offs.read(B_OUT_FM),
        }

    def diag_text(self, indent="  "):
        d = self.diag()
        lines = [
            "WIN=[%#010x, %#010x)%s"
            % (d["WIN_LO"], d["WIN_HI"],
               "" if self.set_window else "（本引擎不写窗口）"),
            "addr: in=%#010x wgt=%#010x bias=%#010x out=%#010x"
            % (d["IN_LO"], d["WGT_LO"], d["BIAS_LO"], d["OUT_LO"]),
            "0xA8 自检: %s" % selftest_text(d["SELFTEST"]),
            "0xAC 四区: %s" % win4_text(d["WIN4"]),
            "0xB0 bytes_in(RTL)=%d" % d["BYTES_IN_HW"],
            "STALL_FILL=%d STALL_MAC=%d"
            % (d["STALL_FILL"], d["STALL_MAC"]),
            "STATUS: %s" % status_text(d["STATUS"]),
            "AP_RETURN=%d(%s)"
            % (d["AP_RETURN"],
               ERR_MEANING.get(d["AP_RETURN"], "未知错误码")),
        ]
        return "\n".join(indent + s for s in lines)

    def readback(self):
        """把 PS 侧能读到的寄存器全读回来（上板 bring-up 用）。"""
        return {
            "ip": self.discovered or self.ip_name,
            "base": self.base_addr,
            "AP_CTRL": self.ctrl.read(AP_CTRL),
            "AP_RETURN": self.ctrl.read(AP_RETURN),
            "STATUS": self.ctrl.read(R_STATUS),
            "H": self.ctrl.read(R_H), "W": self.ctrl.read(R_W),
            "HOUT": self.ctrl.read(R_HOUT), "WOUT": self.ctrl.read(R_WOUT),
            "CIN": self.ctrl.read(R_CIN), "COUT": self.ctrl.read(R_COUT),
            "KSIZE": self.ctrl.read(R_KSIZE), "STRIDE": self.ctrl.read(R_STRIDE),
            "DIL": self.ctrl.read(R_DIL), "PAD": self.ctrl.read(R_PAD),
            "SHIFT": self.ctrl.read(R_SHIFT), "RELU": self.ctrl.read(R_RELU),
            "WIN_LO": self.ctrl.read(R_WIN_LO),
            "WIN_HI": self.ctrl.read(R_WIN_HI),
            "STALL_FILL": self.ctrl.read(R_STALL_FILL),
            "STALL_MAC": self.ctrl.read(R_STALL_MAC),
            "SELFTEST": self.ctrl.read(R_SELFTEST),
            "WIN4": self.ctrl.read(R_WIN4),
            "BYTES_IN_RTL": self.ctrl.read(R_BYTES_IN),
            "IN_LO": self.offs.read(B_IN_FM),
            "IN_HI": self.offs.read(B_IN_FM + 4),
            "WGT_LO": self.offs.read(B_WGT),
            "WGT_HI": self.offs.read(B_WGT + 4),
            "BIAS_LO": self.offs.read(B_BIAS),
            "BIAS_HI": self.offs.read(B_BIAS + 4),
            "OUT_LO": self.offs.read(B_OUT_FM),
            "OUT_HI": self.offs.read(B_OUT_FM + 4),
        }

    # ------------------------------------------------------------ 估算

    @staticmethod
    def estimate_seconds(H, W, cin, cout, k=5, s=1):
        """与 ``K1Engine.estimate_seconds`` 同签名；返回本 IP 的理论耗时。

        ``oc_per_pass`` 取 ``K5_IP_OC_PER_PASS``（默认 1 = 25 DSP / 393 ms）。
        """
        cycles = theoretical_cycles(
            H, W, cin, cout, s=s,
            oc_per_pass=max(1, _env_int("K5_IP_OC_PER_PASS", K5_OC_PER_PASS)))
        if int(k) != K5_KSIZE:
            cycles = int(cycles * (int(k) ** 2) / float(K5_KSIZE ** 2))
        return 1.1 * cycles / max(fclk_hz(), 1.0)

    def timeout_seconds(self, H, W, cin, cout, k=5, s=1):
        return timeout_for(H, W, cin, cout, k=k, s=s,
                           oc_per_pass=self.oc_per_pass)

    def eligibility(self, H, W, cin, cout, k, s, d, pad, shift, relu=True,
                    c8_in=True, c8_out=True, hout=None, wout=None):
        """实例版 ``eligibility``（自动带上本 IP 的编译期形状）。"""
        return eligibility(
            H, W, cin, cout, k, s, d, pad, shift, relu=relu,
            c8_in=c8_in, c8_out=c8_out, ip_cin=self.ip_cin,
            ip_cout=self.ip_cout, ip_h=self.ip_h, ip_w=self.ip_w,
            hout=hout, wout=wout)

    # ------------------------------------------------------------ 预检

    def _prepare(self, in_buf, wgt_buf, bias_buf, out_buf,
                 H, W, cin, cout, k, s, d, pad, shift, relu=True,
                 out_lut=None, chsum=None, timeout_s=None,
                 c8_in=False, c8_out=False, layer=None):
        """纯软件预检 + 地址/窗口/超时计算（**不碰硬件**）。"""
        layer = layer or "-"
        H, W, cin, cout = int(H), int(W), int(cin), int(cout)
        k, s, d, pad = int(k), int(s), int(d), int(pad)
        reasons = check_supported(
            H, W, cin, cout, k, s, d, pad, shift,
            relu=relu, c8_in=c8_in, c8_out=c8_out,
            ip_cin=self.ip_cin, ip_cout=self.ip_cout,
            ip_h=self.ip_h, ip_w=self.ip_w)
        if out_lut is not None:
            reasons.append("不支持 out_lut 融合出口（k5 wrapper 没有该寄存器）")
        if chsum is not None:
            reasons.append("不支持 chsum 融合出口（k5 wrapper 没有该寄存器）")
        ctx = ("layer=%s H=%d W=%d cin=%d cout=%d k=%d s=%d d=%d pad=%d "
               "shift=%s relu=%s C8(in/out)=%s/%s"
               % (layer, H, W, cin, cout, k, s, d, pad, shift, bool(relu),
                  bool(c8_in), bool(c8_out)))
        if reasons:
            raise K5Unsupported("K5 IP 不接受本层（%s）：%s"
                                % (ctx, "; ".join(reasons)))

        try:
            addrs = [_buf_addr(b) for b in (in_buf, wgt_buf, bias_buf, out_buf)]
        except Exception as exc:                           # noqa: BLE001
            raise K5Error("取缓冲区物理地址失败（%s）：%r" % (ctx, exc)) from exc
        names = ("in", "wgt", "bias", "out")
        for nm, a in zip(names, addrs):
            if a <= 0:
                raise K5Unsupported("%s 缓冲区地址为 0（%s）" % (nm, ctx))
            if a > 0xFFFFFFFF:
                raise K5Unsupported(
                    "%s 缓冲区地址 %#x 超过 32 位（%s）" % (nm, a, ctx))

        hout = (H - 1) // s + 1
        wout = (W - 1) // s + 1
        bb = buffer_bytes(H, W, cin, cout, hout=hout, wout=wout)
        win_lo, win_hi = window_of(addrs, bb)
        if win_hi > 0xFFFFFFFF:
            raise K5Unsupported(
                "窗口上界 %#x 超过 32 位（RTL 的地址是 32 位；%s）"
                % (win_hi, ctx))

        if timeout_s is None:
            timeout_s = timeout_for(H, W, cin, cout, s=s,
                                    oc_per_pass=self.oc_per_pass,
                                    hout=hout, wout=wout)
        timeout_s = float(timeout_s)
        if timeout_s <= 0:
            raise ValueError("timeout_s 必须 > 0（收到 %r）" % timeout_s)
        cycles = theoretical_cycles(H, W, cin, cout,
                                    oc_per_pass=self.oc_per_pass, s=s,
                                    hout=hout, wout=wout)
        return _Prep(addrs, bb, win_lo, win_hi, timeout_s, cycles, ctx)

    def start(self, in_buf, wgt_buf, bias_buf, out_buf,
              H, W, cin, cout, k, s, d, pad, shift, relu=True,
              out_lut=None, chsum=None, timeout_s=None,
              c8_in=False, c8_out=False, layer=None):
        """写全部寄存器（含窗口，可选）+ ``AP_CTRL`` bit0，**返回后不等**。"""
        prep = self._prepare(
            in_buf, wgt_buf, bias_buf, out_buf, H, W, cin, cout, k, s, d,
            pad, shift, relu=relu, out_lut=out_lut, chsum=chsum,
            timeout_s=timeout_s, c8_in=c8_in, c8_out=c8_out, layer=layer)
        self._last_prep = prep
        self.last_win = (prep.win_lo, prep.win_hi)
        names = ("in", "wgt", "bias", "out")
        st = 0
        try:
            st = self.status()
            if st & ST_BUSY:
                t_wait = time.time()
                limit = min(prep.timeout_s, 5.0)
                while (st & ST_BUSY) and (time.time() - t_wait < limit):
                    time.sleep(0.0002)
                    st = self.status()
                if st & ST_BUSY:
                    raise K5Timeout(
                        "启动前上一笔 K5 任务仍 busy（等 %.1fs）：%s；%s"
                        % (limit, status_text(st), prep.ctx))

            if self.set_window:
                self.ctrl.write(R_WIN_LO, prep.win_lo)
                self.ctrl.write(R_WIN_HI, prep.win_hi)

            for nm, off, a in zip(names,
                                  (B_IN_FM, B_WGT, B_BIAS, B_OUT_FM),
                                  prep.addrs):
                self.offs.write(off, a & 0xFFFFFFFF)
                self.offs.write(off + 4, (a >> 32) & 0xFFFFFFFF)

            hout = (int(H) - 1) // int(s) + 1
            wout = (int(W) - 1) // int(s) + 1
            self.ctrl.write(R_H, H)
            self.ctrl.write(R_W, W)
            self.ctrl.write(R_HOUT, hout)
            self.ctrl.write(R_WOUT, wout)
            self.ctrl.write(R_CIN, cin)
            self.ctrl.write(R_COUT, cout)
            self.ctrl.write(R_KSIZE, k)
            self.ctrl.write(R_STRIDE, s)
            self.ctrl.write(R_DIL, d)
            self.ctrl.write(R_PAD, pad)
            self.ctrl.write(R_SHIFT, shift)
            self.ctrl.write(R_RELU, relu_flags(relu, c8_in, c8_out))

            t0 = time.time()
            self.ctrl.write(AP_CTRL, AP_START)
        except K5Error:
            raise
        except Exception as exc:                           # noqa: BLE001
            raise K5Error(
                "K5 IP 启动写寄存器失败（%s）：%r；最后 STATUS: %s"
                % (prep.ctx, exc, status_text(st))) from exc
        return t0, prep.timeout_s, prep.cycles, prep.ctx

    def wait(self, timeout_s, t0, cycles, ctx):
        """轮询 ``STATUS``（done/err）→ 读 ``AP_RETURN`` + 诊断寄存器。"""
        st = 0
        try:
            poll = 0.00005 if float(timeout_s) < 0.5 else 0.0001
            while True:
                st = self.status()
                if st & (ST_DONE | ST_ERR):
                    break
                if (time.time() - t0) > float(timeout_s):
                    self._print_diag("超时", ctx)
                    raise K5Timeout(
                        "K5 IP 超时 %.2fs 未完成（理论 %d 拍 @ %.1f MHz ⇒ 约 "
                        "%.3fs；%s；最后 STATUS: %s）。检查 FCLK 是否在跑、"
                        "DDR 通道是否被占死、参数是否合法"
                        % (float(timeout_s), int(cycles), fclk_hz() / 1e6,
                           int(cycles) / max(fclk_hz(), 1.0), ctx,
                           status_text(st)))
                time.sleep(poll)
            elapsed = time.time() - t0
            rc = int(self.ctrl.read(AP_RETURN))
            stall_fill = int(self.ctrl.read(R_STALL_FILL))
            stall_mac = int(self.ctrl.read(R_STALL_MAC))
            bytes_in_hw = int(self.ctrl.read(R_BYTES_IN))
        except K5Error:
            raise
        except Exception as exc:                           # noqa: BLE001
            raise K5Error(
                "K5 IP 轮询/读返回码失败（%s）：%r；最后 STATUS: %s"
                % (ctx, exc, status_text(st))) from exc

        self.last_status = st
        self.last_return = rc
        self.last_stall_fill = stall_fill
        self.last_stall_mac = stall_mac
        self.last_bytes_in = bytes_in_hw
        self.n_runs += 1
        self.total_seconds += elapsed
        self.total_stall_fill += stall_fill
        self.total_stall_mac += stall_mac

        sticky = bool(st & ST_STICKY_OOB)
        if sticky:
            self.oob_ever = True
        if sticky and self.strict_oob:
            self._print_diag("sticky_oob（越界读被钳制过）", ctx, st=st)
            raise K5Error(
                "K5 IP sticky_oob 置位：合并读口做过地址钳制 ⇒ 本层输出不可信"
                "（%s）。STATUS=%s。sticky 是**粘性**的，只有 PL 复位"
                "（重新加载 overlay）才清。" % (ctx, status_text(st)))

        if rc != 0:
            code = (st >> ST_ERR_CODE_SHIFT) & ST_ERR_CODE_MASK
            if code == 0:
                code = rc
            reason = ERR_MEANING.get(rc, ERR_MEANING.get(code, "未知错误码"))
            if rc == 0xA:
                why = ("地址窗口越界：四个区 [addr, addr+bytes) 必须整体落在 "
                       "WIN=[%#x, %#x) 内。k5 的 wrapper 还没实现，先核对"
                       "它是否真的有窗口寄存器（没有就把 set_window=False）"
                       % (self.last_win[0], self.last_win[1]))
            else:
                why = reason
            self._print_diag("AP_RETURN=%#x" % rc, ctx, st=st)
            raise K5ReturnError(
                "K5 IP 返回错误码 %#x（%s）：%s；STATUS: %s；耗时 %.3fs；"
                "STALL_FILL=%d STALL_MAC=%d"
                % (rc, reason, why, status_text(st), elapsed, stall_fill,
                   stall_mac),
                return_code=rc, status=st)

        return K5RunResult(rc, elapsed, stall_fill=stall_fill,
                           stall_mac=stall_mac, bytes_in=bytes_in_hw,
                           status=st, return_code=rc, layer=None)

    def _print_diag(self, tag, ctx, st=None):
        print("K5 IP 诊断（%s）：%s" % (tag, ctx), flush=True)
        try:
            print(self.diag_text(), flush=True)
        except Exception as exc:                           # noqa: BLE001
            print("  （诊断寄存器读取失败：%r）" % (exc,), flush=True)
        if st is not None:
            print("  STATUS: %s" % status_text(st), flush=True)

    def run(self, in_buf, wgt_buf, bias_buf, out_buf,
            H, W, cin, cout, k, s, d, pad, shift, relu=True,
            out_lut=None, chsum=None, timeout_s=None, c8_in=False, c8_out=False,
            layer=None):
        """写参数/窗口 → 启动 → 轮询 STATUS → 读 AP_RETURN，返回 ``K5RunResult``。"""
        t0, timeout_s, cycles, ctx = self.start(
            in_buf, wgt_buf, bias_buf, out_buf, H, W, cin, cout, k, s, d,
            pad, shift, relu=relu, out_lut=out_lut, chsum=chsum,
            timeout_s=timeout_s, c8_in=c8_in, c8_out=c8_out, layer=layer)
        res = self.wait(timeout_s, t0, cycles, ctx)
        res.layer = layer
        return res

    # ------------------------------------------------------------ 杂项

    def describe(self):
        return ("K5Engine(ip=%s, base=%#x, ctrl=%#x, offs=%#x, k=5, "
                "CIN=%d COUT=%d HxW=%dx%d pad=%d OCPP=%d, window=%s, "
                "fclk=%.1fMHz)"
                % (self.discovered or self.ip_name, self.base_addr,
                   self.base_addr + CTRL_PAGE_OFF, self.base_addr,
                   self.ip_cin, self.ip_cout, self.ip_h, self.ip_w, K5_PAD,
                   self.oc_per_pass, "on" if self.set_window else "off",
                   fclk_hz() / 1e6))

    def close(self):
        for obj in (self.mmio,):
            fn = getattr(obj, "close", None)
            if fn is not None:
                try:
                    fn()
                except Exception:                          # noqa: BLE001
                    pass
        self.mmio = self.ctrl = self.offs = None


__all__ = [
    "K5Engine", "K5Error", "K5Unsupported", "K5Timeout", "K5ReturnError",
    "K5RunResult", "BufferBytes", "buffer_bytes", "window_of",
    "check_supported", "eligibility", "theoretical_cycles", "timeout_for",
    "status_text", "selftest_text", "win4_text", "relu_flags", "fclk_hz",
    "ceil8", "ERR_MEANING", "W_LAYOUT_K5", "DEFAULT_IP_NAME", "DEFAULT_BASE",
    "WINDOW_SIZE", "CTRL_PAGE_OFF", "K5_CIN", "K5_COUT", "K5_H", "K5_W",
    "K5_PAD", "K5_OC_PER_PASS", "K5_TAPS", "K5_SHIFT_MIN", "K5_SHIFT_MAX",
]
