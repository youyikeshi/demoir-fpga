#!/usr/bin/env python3
"""PYNQ 驱动：手写 k=3 卷积加速器 ``conv_k3_engine``（AXI-Lite 包装 ``k3_axi_wrap``）。

与 ``k1_engine.py`` 的关系
--------------------------
寄存器映射**逐字沿用 k1**（见 ``pl/rtl_hand/modules/k3_axi_wrap.v`` 文件头）：

    offs 页（A[16]=0）                    ctrl 页（A[16]=1）
      0x10 IN_ADDR_LO/HI                    0x00 AP_CTRL  [0]start
      0x1C WGT_ADDR_LO/HI                   0x10 AP_RETURN = 错误码
      0x28 BIAS_ADDR_LO/HI                  0x18..0x70 H W HOUT WOUT CIN COUT
      0x34 OUT_ADDR_LO/HI                          KSIZE STRIDE DIL PAD SHIFT RELU
                                            0x94 STATUS [0]done [1]busy [2]err
                                                       [7:4]err_code [8]idle [9]sticky_oob
                                            ---- 以下 k3 新增 ----
                                            0x98/0x9C WIN_LO / WIN_HI
                                            0xA0/0xA4 STALL_FILL / STALL_MAC
                                            0xA8 自检 {win_ok[5], ok[4], err[3:0]}
                                            0xAC 四区窗口结果 {ou,bi,wg,in}[3:0]
                                            0xB0 bytes_in（RTL 记账口径）

**k3 比 k1 多出来的三件事**（本驱动必须做，否则上板会踩）：

1. **地址窗口**：RTL 把四个区 ``[addr, addr+bytes)`` 与 ``[WIN_LO, WIN_HI]``
   比较，任一区越界就**不启动**、返回 ``0xA``。所以 ``run()`` 每次都写
   ``WIN_LO = min(四地址)``、``WIN_HI = max(地址+长度)``。长度口径按 RTL
   的 ``bytes_in/bytes_wgt/bytes_bias/bytes_out``（见 ``buffer_bytes()``）。
2. **sticky_oob**（``STATUS[9]``）：合并读口把越界 AR **钳**在区内（读最后一个
   合法 8 字节）并置 sticky。钳过说明"地址算错了" ⇒ 本次输出不可信，必须抛
   异常。注意它是**粘性**的，只有 PL 复位（重新加载 overlay）才清。
3. **诊断寄存器**：``STALL_FILL``（行末等 DDR 供数）/ ``STALL_MAC``（抽头或
   输出 FIFO 未就绪，应≈0）+ ``0xA8/0xAC/0xB0``。失败时一并打印（"听诊器"），
   成功时放进 ``run()`` 的返回值（``K3RunResult``，见下）。

``run()`` 的返回值
------------------
``K3RunResult`` 是一个**可解包成 ``(rc, seconds)``** 的对象（与 ``K1Engine.run``
的调用约定一致：``rc, dt = eng.run(...)`` 照旧可用），另外带
``stall_fill / stall_mac / bytes_in / status / return_code`` 字段::

    res = eng.run(...)
    print(res.stall_fill, res.stall_mac, res.bytes_in)

硬件事实来源（不要凭记忆改）
----------------------------
* ``pl/rtl_hand/modules/k3_axi_wrap.v`` —— 寄存器映射、窗口检查、sticky、诊断
* ``pl/rtl_hand/modules/k3_cfg_check.v`` —— 9 条启动前校验与错误码 1..9
* ``pl/rtl_hand/modules/conv_k3_engine.v`` —— C8 数据格式、PAD12 权重、拍数公式
* ``docs/rtl_hand/conv_k3_engine_设计.md`` —— 114 层形状清单/拍数推导
* ``pynq_specialized_code/ps/pynq/ref_numpy.py`` —— PAD12 容器定义（导出侧同源）

已知限制
--------
* **只支持 C8 交错布局**（输入/输出都必须是 C8）。CHW 下不可路由。
* 权重容器必须是 **PAD12**（``[cout, cin, 12]``，9 抽头 + 3 零，
  ``offset = (oc*cin+ic)*12 + ky*3 + kx``）。旧容器（``[cout,cin,3,3]``）读不了。
* 没有 ``out_lut`` / ``chsum`` 融合出口（k3 wrapper 没有这些寄存器）。
* ``H/W ≤ 320``、``cin ≤ 192 且 %8==0``、``cout ≤ 192``、
  ``shift ∈ [0,31]``、``stride ∈ {1,2}``、``dilation ∈ {1,2,3}``、
  ``pad == dilation``，且**不能同时** ``stride≠1 && dilation≠1``。
* 槽容量 ``⌈cin/8⌉·W·(d≠1 ? 2 : 1) ≤ 4608``（RTL 的 ``CAP_B``），超了返回 8。
"""

import os
import time

# ---------------------------------------------------------------- 常量

#: BD 实例名（`pl/vivado/.../design_1.hwh` 里就是 ``k3_wrap``）；
#: 发现逻辑同时兼容 ``k3_axi_wrap`` / ``conv_k3_engine``。
DEFAULT_IP_NAME = "k3_wrap"

#: AXI-Lite 从机窗口大小：128KB（地址由 BD 分配）。
WINDOW_SIZE = 0x20000
#: 没有 overlay / strict=False 时的兜底。**正常上板一律从 .hwh 发现**：
#: `pl/scripts/bd_add_k3.tcl` 默认 ``0x4006_0000``（实测 hwh：
#: ``k3_wrap 0x40060000..0x4007FFFF``）。
DEFAULT_BASE = 0x40060000
CTRL_PAGE_OFF = 0x10000          # addr[16]=1 的 ctrl 页

# ---- ctrl 页寄存器偏移（与 k1 / 老 HLS 引擎一致）----
AP_CTRL, AP_RETURN = 0x00, 0x10
R_H, R_W, R_HOUT, R_WOUT = 0x18, 0x20, 0x28, 0x30
R_CIN, R_COUT, R_KSIZE = 0x38, 0x40, 0x48
R_STRIDE, R_DIL, R_PAD, R_SHIFT = 0x50, 0x58, 0x60, 0x68
R_RELU = 0x70
R_STATUS = 0x94

# ---- k3 新增（k3_axi_wrap.v §1 / §2）----
R_WIN_LO, R_WIN_HI = 0x98, 0x9C          # 允许访问的地址窗口 [lo, hi]
R_STALL_FILL, R_STALL_MAC = 0xA0, 0xA4   # 只读诊断计数（start 清零）
R_SELFTEST = 0xA8                        # {26'd0, win_ok[5], ok[4], err[3:0]}
R_WIN4 = 0xAC                            # {28'd0, ou[3], bi[2], wg[1], in[0]}
R_BYTES_IN = 0xB0                        # RTL 算出来的 bytes_in

# ---- offs 页寄存器偏移（四个 64 位缓冲区地址）----
B_IN_FM, B_WGT, B_BIAS, B_OUT_FM = 0x10, 0x1C, 0x28, 0x34

AP_START = 0x1

# STATUS 位（k3_axi_wrap.v）
ST_DONE = 1 << 0
ST_BUSY = 1 << 1
ST_ERR = 1 << 2
ST_ERR_CODE_SHIFT = 4
ST_ERR_CODE_MASK = 0xF
ST_IDLE = 1 << 8
ST_STICKY_OOB = 1 << 9           # 粘性：越界读被钳制过（只有 PL 复位能清）

# relu_en 的位定义（与 conv_engine.relu_flags 一致）。k3 的 wrapper 只用
# bit0（``cfg_relu = cfg_relu_d[0]``）；bit2/bit3 是 C8 声明位，留给未来校验。
RELU_BIT, C8_IN_BIT, C8_OUT_BIT = 0, 2, 3

# 本 IP 的硬约束（k3_cfg_check.v + conv_k3_engine.v 的参数默认值）。
# 如 BD 改过 CIN_MAX/COUT_MAX/CAP_B，用 K3_IP_CIN_MAX / K3_IP_COUT_MAX /
# K3_IP_CAP_B 覆盖，**不要改这里的默认值**。
K3_KSIZE = 3
K3_CIN_MIN = 8
K3_CIN_MAX = 192
K3_COUT_MIN = 1
K3_COUT_MAX = 192
K3_HW_MAX = 320
K3_SHIFT_MIN, K3_SHIFT_MAX = 0, 31
K3_CAP_B = 4608                  # 每 bank 槽容量（行缓冲单元数）
K3_STRIDES = (1, 2)
K3_DILS = (1, 2, 3)
DEFAULT_LANES = 8                # 8 个 oc 车道（8×9 = 72 DSP）
K3_WGT_BYTES_PER_IC = 12         # PAD12：9 抽头 + 3 零

#: 权重容器布局标签（index.json 的 ``layers[key]["w_layout"]``）。
W_LAYOUT_K3 = "k3_pad12"

# AP_RETURN / STATUS[7:4] 的错误码含义（k3_cfg_check.v 逐条对应）。
# 注意：0xA 不是 cfg_check 给的，是 k3_axi_wrap 的**地址窗口**检查给的。
ERR_MEANING = {
    0x0: "成功",
    0x1: "cin 非法（不是 8 的倍数 / <8 / >192）",
    0x2: "cout 非法（0 或 >192）",
    0x3: "stride 非法（不在 {1,2}）",
    0x4: "dilation 非法（不在 1..3）",
    0x5: "H/W 非法（0 或 >320）",
    0x6: "Hout/Wout 非正（(X-1)/S+1 < 1）",
    0x7: "shift > 31",
    0x8: "槽容量不足（need_cap = ⌈cin/8⌉·W·(d≠1?2:1) > 4608）",
    0x9: "不支持 stride≠1 且 dilation≠1（本 IP 明确拒绝，不在真实路径上）",
    0xA: "地址窗口越界（四个区必须整体落在 [WIN_LO, WIN_HI] 内）",
}


# ---------------------------------------------------------------- 异常

class K3Error(RuntimeError):
    """K3 IP 相关的所有异常基类（上层捕获它就够）。"""


class K3Unsupported(K3Error):
    """本层参数不在 K3 IP 的支持范围内（**在碰硬件之前**抛出）。"""


class K3Timeout(K3Error):
    """等待 done 超时。"""


class K3ReturnError(K3Error):
    """AP_RETURN 非 0（RTL 启动了但拒绝/算错，含 0xA 窗口越界）。"""

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

    优先本 IP 的 K3_IP_FCLK_HZ / K3_IP_FCLK_MHZ；没有再退回 K1_IP_FCLK_*
    （k1/k3/k5 由同一个 FCLK0 驱动，板上通常一条环境变量就够）。
    """
    for pfx in ("K3_IP", "K1_IP"):
        if str(os.environ.get(pfx + "_FCLK_HZ", "")).strip():
            return _env_float(pfx + "_FCLK_HZ", 150e6)
    for pfx in ("K3_IP", "K1_IP"):
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
        raise K3Unsupported("缓冲区没有 device_address/physical_address 属性：%r"
                            % (type(buf).__name__,))
    return int(a)


def relu_flags(relu=True, c8_in=True, c8_out=True):
    """拼 relu_en：bit0=relu、bit2=输入 C8、bit3=输出 C8（与 k1 同式）。

    k3 wrapper 只用 bit0；两位 C8 声明位照写（k3 是纯 C8 构建，声明恒为真）。
    """
    return ((1 if relu else 0)
            | (1 << C8_IN_BIT if c8_in else 0)
            | (1 << C8_OUT_BIT if c8_out else 0))


def status_text(st):
    """把 STATUS 值翻译成一行诊断文本（k3 多了 sticky_oob）。"""
    st = int(st) & 0xFFFFFFFF
    code = (st >> ST_ERR_CODE_SHIFT) & ST_ERR_CODE_MASK
    return ("done=%d busy=%d err=%d err_code=%#x(%s) idle=%d sticky_oob=%d"
            % ((st & ST_DONE) != 0, (st & ST_BUSY) != 0, (st & ST_ERR) != 0,
               code, ERR_MEANING.get(code, "未知错误码"), (st & ST_IDLE) != 0,
               (st & ST_STICKY_OOB) != 0))


def selftest_text(v):
    """翻译 0xA8 自检寄存器：{26'd0, win_ok[5], ok[4], err[3:0]}。"""
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
    """四个区的字节数（RTL ``bytes_in/bytes_wgt/bytes_bias/bytes_out`` 口径）。

    可当 4 元组解包 ``in_b, wgt_b, bias_b, out_b = buffer_bytes(...)``，
    也可按名字取 ``buffer_bytes(...).out``（上板算窗口时更不容易错位）。
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
    """四个区的字节数（**与 RTL 的记账逐条对齐**，别按元素数改）。

    * ``bytes_in  = ⌈cin/8⌉·8·H·W·8``      —— ceil(cin/8) 个 C8 组
    * ``bytes_wgt = cout·cin·12``          —— PAD12 容器（9 抽头 + 3 零）
    * ``bytes_bias= cout·4``               —— int32 × cout
    * ``bytes_out = ⌈cout/8⌉·8·Hout·Wout·8``—— 输出按 8 oc 一组补齐（尾组写 0）

    ``Hout/Wout`` 缺省按 ``(X-1)//s + 1`` 推。
    """
    H, W, cin, cout = int(H), int(W), int(cin), int(cout)
    s = max(1, int(s))
    hout = ((H - 1) // s + 1) if hout is None else int(hout)
    wout = ((W - 1) // s + 1) if wout is None else int(wout)
    return BufferBytes(ceil8(cin) * H * W * 8,
                       cout * cin * K3_WGT_BYTES_PER_IC,
                       cout * 4,
                       ceil8(cout) * hout * wout * 8)


def window_of(addrs, lens):
    """四个区的 ``[WIN_LO, WIN_HI]``：``min(addr)`` .. ``max(addr+len)``。

    RTL 的判定是 ``addr >= win_lo && addr + bytes <= win_hi``（33 位比较），
    所以上界取 ``max(addr+len)``（不是 ``-1``）才能让四个区都通过。
    """
    addrs = [int(a) for a in addrs]
    lens = [int(n) for n in lens]
    if len(addrs) != len(lens):
        raise ValueError("addrs/lens 长度不一致（%d vs %d）"
                         % (len(addrs), len(lens)))
    if not addrs:
        raise ValueError("addrs 为空")
    lo = min(addrs)
    hi = max(a + n for a, n in zip(addrs, lens))
    return int(lo), int(hi)


# ---------------------------------------------------------------- 拍数/超时

def theoretical_cycles(H, W, cin, cout, lanes=DEFAULT_LANES, k=3, s=1,
                       hout=None, wout=None):
    """本层理论拍数（与 ``docs/rtl_hand/conv_k3_engine_设计.md`` 同式）：

        Hout · Wout · ⌈cout/8⌉ · cin

    推导：8 个 oc 车道 × 9 抽头 = 72 DSP，II=1 ⇒ 每个输出像素每一组
    （8 oc）花 ``cin`` 拍（每拍一个 ic，9 个抽头并行），一共 ``⌈cout/8⌉`` 组。
    用这个式子复算全网 114 层 = **259.20 M 拍 = 1.728 s @150 MHz**，与设计
    文档的结论逐位一致（所以不要套 k1 的 ``⌈cout/lanes⌉·cin·k²``：那会多算 9 倍）。

    ``lanes`` / ``k`` 只为与 ``K1Engine`` 的签名保持一致：本 IP 的 oc 车道
    固定 8、核型固定 3×3，传别的值不参与计算（``describe()`` 会显示实际值）。
    """
    del lanes
    H, W, cin, cout = int(H), int(W), int(cin), int(cout)
    s = max(1, int(s))
    hout = ((H - 1) // s + 1) if hout is None else int(hout)
    wout = ((W - 1) // s + 1) if wout is None else int(wout)
    passes = (cout + DEFAULT_LANES - 1) // DEFAULT_LANES
    return int(max(0, hout) * max(0, wout) * passes * max(0, cin))


def timeout_for(H, W, cin, cout, k=3, s=1, lanes=DEFAULT_LANES,
                hout=None, wout=None):
    """单层等待上限（秒）= 理论拍数 × 系数 / FCLK + 余量。

    系数默认 3.0、余量 1.0 s（K3_IP_TIMEOUT_FACTOR / K3_IP_TIMEOUT_EXTRA_S）。
    最贵一层 ``decoder.preconv_1``（cin=112/cout=64/320²）≈ 91.75 M 拍 ≈ 0.61 s
    ⇒ 默认超时 2.8 s。
    """
    factor = _env_float("K3_IP_TIMEOUT_FACTOR", 3.0)
    extra = _env_float("K3_IP_TIMEOUT_EXTRA_S", 1.0)
    cycles = theoretical_cycles(H, W, cin, cout, lanes=lanes, k=k, s=s,
                                hout=hout, wout=wout)
    return max(0.05, factor * cycles / max(fclk_hz(), 1.0) + extra)


def need_cap(cin, W, d):
    """RTL 的槽容量需求 ``⌈cin/8⌉ · W · (d≠1 ? 2 : 1)``（≤ CAP_B=4608）。

    ``d≠1`` 时引擎走乒乓（两套 bank），容量翻倍 —— 这是 0x8 错误码的判据，
    也是 ``decoder.preconv_1``（cin=112/W=320/d=1 ⇒ 4480）能过、而同形状
    d=2 会挂（8960>4608）的原因。
    """
    cin, W, d = int(cin), int(W), int(d)
    ngrp = (cin + 7) // 8
    return ngrp * W * (2 if d != 1 else 1)


# ---------------------------------------------------------------- 支持性判定

def check_supported(H, W, cin, cout, k, s, d, pad, shift, relu=True,
                    c8_in=True, c8_out=True, relu_capable=None,
                    hout=None, wout=None,
                    cin_max=K3_CIN_MAX, cout_max=K3_COUT_MAX,
                    hw_max=K3_HW_MAX, cap_b=K3_CAP_B):
    """返回**不支持的原因列表**（空列表 = 可以走 K3 IP）。

    纯函数：不导入 pynq、不碰硬件，本机可单测。判定**与 RTL 的启动前校验
    一一对应**（``k3_cfg_check.v`` 的 9 条 + ``k3_axi_wrap`` 的窗口/容量）：

      · k == 3、stride ∈ {1,2}、dilation ∈ {1,2,3}、pad == dilation
      · **不能**同时 ``stride≠1 && dilation≠1``（RTL 错误码 9，明确拒绝）
      · ``H, W ∈ [1, 320]``；``cin ∈ [8,192]`` 且 ``%8==0``；``cout ∈ [1,192]``
      · ``shift ∈ [0,31]``
      · ``need_cap = ⌈cin/8⌉·W·(d≠1?2:1) ≤ 4608``
      · Hout/Wout（给了就校验）必须等于 ``(X-1)//s+1``
      · 输入/输出都必须是 C8 布局

    与 k1 的一处**语义差异**：k3 的 ReLU 是**运行期**寄存器位
    （``k3_axi_wrap.v`` 的 ``cfg_relu_d[0]`` ⇒ 引擎 ``ctrl_relu``），所以
    默认 ``relu_capable=None`` = 不检查 relu。只有当该 bit 的 wrapper 把
    ReLU 综合成常量时（未来版本）才传 0/1。
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

    if H <= 0 or W <= 0:
        reasons.append("H/W 必须为正（H=%d W=%d）" % (H, W))
    if H > int(hw_max) or W > int(hw_max):
        reasons.append("H/W 超过 %d（H=%d W=%d）" % (int(hw_max), H, W))
    if k != K3_KSIZE:
        reasons.append("k=%d ≠ %d（本 IP 只做 3×3）" % (k, K3_KSIZE))
    if s not in K3_STRIDES:
        reasons.append("stride=%d 不在 {1,2}" % s)
    if d not in K3_DILS:
        reasons.append("dilation=%d 不在 {1,2,3}" % d)
    if s in K3_STRIDES and d in K3_DILS and s != 1 and d != 1:
        reasons.append("stride=%d 且 dilation=%d 同时非 1（本 IP 明确拒绝，"
                       "错误码 9；网络里没有这种层）" % (s, d))
    if d in K3_DILS and pad != d:
        reasons.append("pad=%d ≠ dilation=%d（本 IP 只做 same padding，"
                       "pad 必须等于 dilation）" % (pad, d))
    hout_d = ((H - 1) // s + 1) if s > 0 else None
    wout_d = ((W - 1) // s + 1) if s > 0 else None
    if hout is not None and hout_d is not None and int(hout) != hout_d:
        reasons.append("HOUT=%s ≠ (H-1)/s+1 = %d" % (hout, hout_d))
    if wout is not None and wout_d is not None and int(wout) != wout_d:
        reasons.append("WOUT=%s ≠ (W-1)/s+1 = %d" % (wout, wout_d))
    if hout_d is not None and hout_d < 1:
        reasons.append("Hout=%d 非正" % hout_d)
    if wout_d is not None and wout_d < 1:
        reasons.append("Wout=%d 非正" % wout_d)
    if cin < K3_CIN_MIN or cin > int(cin_max):
        reasons.append("cin=%d 不在 [%d,%d]"
                       % (cin, K3_CIN_MIN, int(cin_max)))
    elif cin % 8:
        reasons.append("cin=%d 不是 8 的倍数（C8 布局要求）" % cin)
    if cout < K3_COUT_MIN or cout > int(cout_max):
        reasons.append("cout=%d 不在 [%d,%d]"
                       % (cout, K3_COUT_MIN, int(cout_max)))
    if shift < K3_SHIFT_MIN or shift > K3_SHIFT_MAX:
        reasons.append("shift=%d 不在 [%d,%d]"
                       % (shift, K3_SHIFT_MIN, K3_SHIFT_MAX))
    if cin >= K3_CIN_MIN and W > 0 and d in K3_DILS:
        nc = need_cap(cin, W, d)
        if nc > int(cap_b):
            reasons.append("槽容量不足：⌈cin/8⌉·W·(d≠1?2:1) = %d > %d"
                           "（错误码 8；改 W 或换更小的 cin）"
                           % (nc, int(cap_b)))
    if relu_capable is not None:
        if bool(relu) != bool(int(relu_capable)):
            reasons.append("relu=%s 与 IP 综合期 RELU=%d 不符"
                           % (bool(relu), int(relu_capable)))
    if not bool(c8_in):
        reasons.append("输入不是 C8 布局（本 IP 只支持 C8 交错布局）")
    if not bool(c8_out):
        reasons.append("输出不是 C8 布局（本 IP 只支持 C8 交错布局）")
    return reasons


def eligibility(H, W, cin, cout, k, s, d, pad, shift, relu=True,
                c8_in=True, c8_out=True, relu_capable=None,
                hout=None, wout=None, cin_max=K3_CIN_MAX,
                cout_max=K3_COUT_MAX, hw_max=K3_HW_MAX, cap_b=K3_CAP_B):
    """``check_supported`` 的非抛出版本：返回 ``(ok, reason)``。"""
    reasons = check_supported(
        H, W, cin, cout, k, s, d, pad, shift, relu=relu,
        c8_in=c8_in, c8_out=c8_out, relu_capable=relu_capable,
        hout=hout, wout=wout, cin_max=cin_max, cout_max=cout_max,
        hw_max=hw_max, cap_b=cap_b)
    return (not reasons, "; ".join(reasons))


# ---------------------------------------------------------------- MMIO 分页

class _Page(object):
    """把一整块 128KB 窗口里的某一页包装成 ``read(off)/write(off,val)``。

    写法与 ``K1Engine`` / ``Conv2dEngine`` 一致：``self.ctrl.write(R_H, H)``
    实际落到 ``mmio[0x10000 + 0x18]``（= ``<BASE>+0x10018``，addr[16]=1）。
    """

    __slots__ = ("mmio", "base_off")

    def __init__(self, mmio, base_off):
        self.mmio = mmio
        self.base_off = int(base_off)

    def read(self, off=0):
        return int(self.mmio.read(self.base_off + int(off)))

    def write(self, off, val):
        self.mmio.write(self.base_off + int(off), int(val) & 0xFFFFFFFF)


# ---------------------------------------------------------------- 返回值

class K3RunResult(object):
    """``run()`` 的返回值：可解包成 ``(rc, seconds)``，并带诊断字段。

    为什么要这个类型：上层（``runner.PYNQEngine.conv``）的调用约定是
    ``rc, dt = eng.run(...)``（与 k1 完全一致），但 k3 的诊断计数
    （STALL_FILL/STALL_MAC/BYTES_IN）也要能拿到。所以做成"可解包的 2 元组
    + 额外字段"：

        rc, dt = res                      # 兼容 k1 的调用约定
        res.stall_fill, res.stall_mac      # k3 独有
        res.bytes_in                       # RTL 算出来的 bytes_in

    判读：``stall_fill`` 大 ⇒ DDR 供数不足（行末等填充）；``stall_mac`` 应≈0，
    一旦明显大于 0 说明内层掉了 II（抽头预取/输出 FIFO 反压）。
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

    # 兼容 `rc, dt = eng.run(...)`
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
        return ("K3RunResult(rc=%d, %.4fs, stall_fill=%d, stall_mac=%d, "
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

class K3Engine(object):
    """``conv_k3_engine`` 的 AXI-Lite 驱动（128KB 窗口，A[16] 分页）。

    参数
    ----
    overlay : pynq.Overlay
        用来从 ``ip_dict`` 自动发现 ``k3_wrap`` 的基地址。**强烈建议传**：
        找不到时默认直接抛错（不猜地址，免得写到 k1/别的从机上）。
    ip_name : str
        BD 实例名，默认 ``k3_wrap``（也接受 ``K3_IP_NAME`` 环境变量）。
    base_addr : int
        显式指定基地址（跳过发现）。仅用于离线单测 / 上板前临时验证。
    mmio_factory : callable
        默认 ``pynq.MMIO``；离线测试注入假实现即可。
    strict : bool
        True（默认）：overlay 里找不到 IP / 基址不是 128KB 对齐就抛错；
        False：退回固定地址并大声警告。
    strict_oob : bool / None
        None（默认）：读 ``K3_IP_STRICT_OOB``（默认 1）；
        True：``STATUS[9] sticky_oob`` 置位就抛异常；
        False：只在异常信息外继续（上板 bring-up 时应急用 —— 但这意味着
        可能把"被钳制过的错误输出"当结果用，仅在确知要这么做时关）。
    """

    def __init__(self, overlay=None, ip_name=None, base_addr=None,
                 mmio_factory=None, strict=True, strict_oob=None):
        self.ip_name = (ip_name
                        or os.environ.get("K3_IP_NAME", "").strip()
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
        self.strict_oob = (_env_flag("K3_IP_STRICT_OOB", True)
                           if strict_oob is None else bool(strict_oob))
        # k3 的 ReLU 是运行期位，默认**不**按编译期 RELU 卡（None = 不检查）。
        # 只有该 bit 的 wrapper 把 ReLU 综合成常量时才设 K3_IP_RELU=0/1。
        env_relu = os.environ.get("K3_IP_RELU", "").strip()
        try:
            self.relu_capable = (None if env_relu == ""
                                 else (1 if int(env_relu, 0) else 0))
        except (TypeError, ValueError):
            self.relu_capable = None
            print("WARN: K3_IP_RELU=%r 不是 0/1，按'运行期 ReLU'处理"
                  % (env_relu,))
        self.lanes = max(1, _env_int("K3_IP_LANES", DEFAULT_LANES))
        if self.lanes != DEFAULT_LANES:
            print("WARN: K3_IP_LANES=%d 与 IP 固定的 %d 不符（只影响拍数估算）"
                  % (self.lanes, DEFAULT_LANES))
        self.cin_max = _env_int("K3_IP_CIN_MAX", K3_CIN_MAX)
        self.cout_max = _env_int("K3_IP_COUT_MAX", K3_COUT_MAX)
        self.hw_max = _env_int("K3_IP_HW_MAX", K3_HW_MAX)
        self.cap_b = _env_int("K3_IP_CAP_B", K3_CAP_B)

        base = int(base_addr) if base_addr is not None else None
        if base is None:
            env_base = os.environ.get("K3_IP_BASE", "").strip()
            if env_base:
                base = int(env_base, 0)
                print("WARN: K3 IP 使用固定基地址 %#x（K3_IP_BASE，跳过 hwh "
                      "发现）——先确认这个窗口真的是 k3_wrap，不是 k1/别的 IP"
                      % base)
            elif overlay is not None:
                found = self.discover(overlay, self.ip_name)
                if found is None:
                    msg = ("overlay 里找不到 K3 IP（实例名按 '%s' 找，也试了 "
                           "k3_axi_wrap/conv_k3_engine）；.hwh 与 .bit 是否同一版？"
                           % self.ip_name)
                    if strict:
                        raise RuntimeError(msg)
                    base = DEFAULT_BASE
                    print("WARN: %s → 回退固定基地址 %#x（危险）" % (msg, base))
                else:
                    base, key, rng = found
                    self.discovered = key
                    print("INFO: K3 IP 发现 '%s' @ %#x（窗口 %#x）"
                          % (key, base, rng))
                    if rng and rng < WINDOW_SIZE:
                        msg = ("hwh 里 k3 条目窗口只有 %#x < 128KB（ctrl 页在 "
                               "+0x10000，写进去会落到窗口外/别的 IP）" % rng)
                        if self._page2_present(overlay, base):
                            print("INFO: hwh 把 128KB 拆成两页（发现 +0x10000 "
                                  "的同名条目），按整窗口继续")
                        elif strict:
                            raise RuntimeError(msg + "；请把 BD 地址范围改成 "
                                               "128KB（bd_add_k3.tcl 的 128K）")
                        else:
                            print("WARN: " + msg + "；strict=False，继续（危险）")
            else:
                msg = ("没有 overlay 也没有 K3_IP_BASE：无法定位 k3_wrap 的 "
                       "AXI-Lite 基地址（本机静态检查请只 import，不要实例化）")
                if strict:
                    raise RuntimeError(msg)
                base = DEFAULT_BASE
                print("WARN: %s → 回退固定基地址 %#x" % (msg, base))

        self.base_addr = int(base)
        if self.base_addr % WINDOW_SIZE:
            msg = ("K3 基地址 %#x 不是 128KB 对齐：RTL 用 addr[16] 分页，"
                   "非对齐基址会把 ctrl/offs 页选反" % self.base_addr)
            if strict:
                raise RuntimeError(msg)
            print("WARN: " + msg + "（strict=False，继续）")
        if mmio_factory is None:
            try:
                from pynq import MMIO
            except Exception as exc:                       # noqa: BLE001
                raise RuntimeError(
                    "构造 K3Engine 需要 pynq.MMIO（本机没有 pynq 时只能 import "
                    "本模块做静态检查）：%r" % (exc,))
            mmio_factory = MMIO
        self.mmio = mmio_factory(self.base_addr, WINDOW_SIZE)
        self.ctrl = _Page(self.mmio, CTRL_PAGE_OFF)
        self.offs = _Page(self.mmio, 0)

    # ------------------------------------------------------------ 地址发现

    @staticmethod
    def discover(overlay, ip_name=DEFAULT_IP_NAME):
        """从 ``overlay.ip_dict`` 找 K3 从机，返回 ``(base, key, addr_range)``。

        兼容三种 hwh 解析结果：
          1. 单个 128KB 条目 ``"k3_wrap"``（本 BD 的实际情况）；
          2. 带接口名的条目 ``"k3_wrap/s_axi"``；
          3. hwh 把窗口拆成两个 64KB 条目（取较低基地址）。
        找不到返回 None（不猜地址）。
        """
        aliases = [ip_name, DEFAULT_IP_NAME, "k3_axi_wrap", "conv_k3_engine"]
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
            elif ("k3_axi_wrap" in typ or "conv_k3_engine" in typ
                  or ("k3" in low and ("wrap" in low or "engine" in low))):
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
        """hwh 是否把 128KB 窗口拆成了两页（base 与 base+0x10000 各一条）。"""
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
            if ("k3" in low or "wrap" in low
                    or "k3_axi_wrap" in typ or "conv_k3_engine" in typ):
                return True
        return False

    # ------------------------------------------------------------ 寄存器读写

    def status(self):
        """读 STATUS 并缓存（轮询/诊断共用）。"""
        st = self.ctrl.read(R_STATUS)
        self.last_status = st
        return st

    def status_text(self):
        return status_text(self.last_status)

    def diag(self):
        """把 k3 的诊断寄存器全读回来（**只读，不改状态**）。

        上板遇到 0xA / sticky_oob 时，这一份就是"听诊器"：
          · ``WIN_LO/WIN_HI`` + 四个地址 → 看窗口是不是自己写错了；
          · ``0xA8`` → RTL 认为配置合法吗（cfg_ok/win_ok/err）；
          · ``0xAC`` → 四个区各自有没有落在窗口里（in/wgt/bias/out）；
          · ``0xB0`` → RTL 算的 bytes_in（与 PS 侧 ``buffer_bytes()`` 对比）；
          · ``STALL_FILL/STALL_MAC`` → DDR 供数还是内层掉 II。
        """
        d = {
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
        return d

    def diag_text(self, indent="  "):
        """``diag()`` 的人类可读版（一行一项，失败时打印用）。"""
        d = self.diag()
        lines = [
            "WIN=[%#010x, %#010x)" % (d["WIN_LO"], d["WIN_HI"]),
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
        rb = {
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
        return rb

    # ------------------------------------------------------------ 估算

    @staticmethod
    def estimate_seconds(H, W, cin, cout, k=3, s=1):
        """与 ``K1Engine.estimate_seconds`` 同签名；返回本 IP 的理论耗时。

        只用于进度打印/规划，不含 PS 路由开销。系数 1.1 是给 AR 延迟留的
        经验余量；**超时**另用 ``timeout_for``（3× + 1 s）。

        k≠3 时按 ``(k/3)²`` 粗放外推（仅显示用；k≠3 不可路由到本 IP）。
        """
        cycles = theoretical_cycles(H, W, cin, cout, s=s)
        if int(k) != K3_KSIZE:
            cycles = int(cycles * (int(k) ** 2) / float(K3_KSIZE ** 2))
        return 1.1 * cycles / max(fclk_hz(), 1.0)

    def timeout_seconds(self, H, W, cin, cout, k=3, s=1):
        return timeout_for(H, W, cin, cout, k=k, s=s, lanes=self.lanes)

    def eligibility(self, H, W, cin, cout, k, s, d, pad, shift, relu=True,
                    c8_in=True, c8_out=True, hout=None, wout=None):
        """实例版 ``eligibility``（自动带上本引擎的 cin/cout/H/W/CAP_B 上界）。"""
        return eligibility(
            H, W, cin, cout, k, s, d, pad, shift, relu=relu,
            c8_in=c8_in, c8_out=c8_out, relu_capable=self.relu_capable,
            hout=hout, wout=wout, cin_max=self.cin_max,
            cout_max=self.cout_max, hw_max=self.hw_max, cap_b=self.cap_b)

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
            relu_capable=self.relu_capable, hout=None, wout=None,
            cin_max=self.cin_max, cout_max=self.cout_max,
            hw_max=self.hw_max, cap_b=self.cap_b)
        if out_lut is not None:
            reasons.append("不支持 out_lut 融合出口（k3 wrapper 没有该寄存器）")
        if chsum is not None:
            reasons.append("不支持 chsum 融合出口（k3 wrapper 没有该寄存器）")
        ctx = ("layer=%s H=%d W=%d cin=%d cout=%d k=%d s=%d d=%d pad=%d "
               "shift=%s relu=%s C8(in/out)=%s/%s"
               % (layer, H, W, cin, cout, k, s, d, pad, shift, bool(relu),
                  bool(c8_in), bool(c8_out)))
        if reasons:
            raise K3Unsupported("K3 IP 不接受本层（%s）：%s"
                                % (ctx, "; ".join(reasons)))

        # 缓冲区地址（Zynq-7000 只有 32 位，高字必须为 0）
        try:
            addrs = [_buf_addr(b) for b in (in_buf, wgt_buf, bias_buf, out_buf)]
        except Exception as exc:                           # noqa: BLE001
            raise K3Error("取缓冲区物理地址失败（%s）：%r" % (ctx, exc)) from exc
        names = ("in", "wgt", "bias", "out")
        for nm, a in zip(names, addrs):
            if a <= 0:
                raise K3Unsupported("%s 缓冲区地址为 0（%s）" % (nm, ctx))
            if a > 0xFFFFFFFF:
                raise K3Unsupported(
                    "%s 缓冲区地址 %#x 超过 32 位（本 IP 只有 32 位 AXI 地址；"
                    "地址高字必须为 0，%s）" % (nm, a, ctx))

        hout = (H - 1) // s + 1
        wout = (W - 1) // s + 1
        bb = buffer_bytes(H, W, cin, cout, hout=hout, wout=wout)
        win_lo, win_hi = window_of(addrs, bb)
        if win_hi > 0xFFFFFFFF:
            raise K3Unsupported(
                "窗口上界 %#x 超过 32 位（RTL 的地址是 32 位：addr+bytes 会绕回；"
                "%s）" % (win_hi, ctx))

        if timeout_s is None:
            timeout_s = timeout_for(H, W, cin, cout, k=k, s=s,
                                    lanes=self.lanes, hout=hout, wout=wout)
        timeout_s = float(timeout_s)
        if timeout_s <= 0:
            raise ValueError("timeout_s 必须 > 0（收到 %r）" % timeout_s)
        cycles = theoretical_cycles(H, W, cin, cout, lanes=DEFAULT_LANES,
                                    k=k, s=s, hout=hout, wout=wout)
        return _Prep(addrs, bb, win_lo, win_hi, timeout_s, cycles, ctx)

    def start(self, in_buf, wgt_buf, bias_buf, out_buf,
              H, W, cin, cout, k, s, d, pad, shift, relu=True,
              out_lut=None, chsum=None, timeout_s=None,
              c8_in=False, c8_out=False, layer=None):
        """写全部寄存器（含 WIN_LO/WIN_HI）+ ``AP_CTRL`` bit0，**返回后不等**。

        正常调用用 ``run()``。只有需要分步调试时才单独用它::

            t0, timeout_s, cycles, ctx = eng.start(...)
            rc, dt = eng.wait(timeout_s, t0, cycles, ctx)

        返回 ``(t0, timeout_s, cycles, ctx)``（与 k1 同签名）。
        """
        prep = self._prepare(
            in_buf, wgt_buf, bias_buf, out_buf, H, W, cin, cout, k, s, d,
            pad, shift, relu=relu, out_lut=out_lut, chsum=chsum,
            timeout_s=timeout_s, c8_in=c8_in, c8_out=c8_out, layer=layer)
        self._last_prep = prep
        self.last_win = (prep.win_lo, prep.win_hi)
        names = ("in", "wgt", "bias", "out")
        st = 0
        try:
            # 上一笔还没结束就启动会被 RTL 忽略（start 只在 !busy 时生效），
            # 随后轮询会读到上一笔的 done → 误判。先等 busy 落下。
            st = self.status()
            if st & ST_BUSY:
                t_wait = time.time()
                limit = min(prep.timeout_s, 5.0)
                while (st & ST_BUSY) and (time.time() - t_wait < limit):
                    time.sleep(0.0002)
                    st = self.status()
                if st & ST_BUSY:
                    raise K3Timeout(
                        "启动前上一笔 K3 任务仍 busy（等 %.1fs）：%s；%s"
                        % (limit, status_text(st), prep.ctx))

            # ---- k3 特有：地址窗口（不写 ⇒ 默认 [0,0xFFFFFFFF] 等于不防护；
            #      而且未来版本若默认上界为 0 就会直接 0xA）----
            self.ctrl.write(R_WIN_LO, prep.win_lo)
            self.ctrl.write(R_WIN_HI, prep.win_hi)

            # 四个 64 位缓冲区基地址（低字在偏移处、高字在 +4）。
            # 注意 k3 的 RTL **忽略高字**（只锁 32 位），照写是为了与 k1 一致。
            for nm, off, a in zip(names,
                                  (B_IN_FM, B_WGT, B_BIAS, B_OUT_FM),
                                  prep.addrs):
                self.offs.write(off, a & 0xFFFFFFFF)
                self.offs.write(off + 4, (a >> 32) & 0xFFFFFFFF)

            # 形状参数（与 k1 / 老引擎的偏移逐一对应）
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
        except K3Error:
            raise
        except Exception as exc:                           # noqa: BLE001
            raise K3Error(
                "K3 IP 启动写寄存器失败（%s）：%r；最后 STATUS: %s"
                % (prep.ctx, exc, status_text(st))) from exc
        return t0, prep.timeout_s, prep.cycles, prep.ctx

    def wait(self, timeout_s, t0, cycles, ctx):
        """轮询 ``STATUS``（done/err）→ 读 ``AP_RETURN`` + 诊断寄存器。

        返回 ``K3RunResult``（可解包成 ``(返回码, 秒)``）。
        - ``sticky_oob`` 置位 ⇒ 打印 0xA8/0xAC/0xB0 与窗口/地址，抛 ``K3Error``；
        - ``AP_RETURN`` 非 0 ⇒ 区分 "1..9 配置非法" 与 "0xA 窗口越界"，
          打印人类可读原因，抛 ``K3ReturnError``；
        - 成功 ⇒ 把 STALL_FILL/STALL_MAC/BYTES_IN 放进返回值。
        """
        st = 0
        try:
            poll = 0.00005 if float(timeout_s) < 0.5 else 0.0001
            while True:
                st = self.status()
                if st & (ST_DONE | ST_ERR):
                    break
                if (time.time() - t0) > float(timeout_s):
                    self._print_diag("超时", ctx)
                    raise K3Timeout(
                        "K3 IP 超时 %.2fs 未完成（理论 %d 拍 @ %.1f MHz ⇒ 约 "
                        "%.3fs；%s；最后 STATUS: %s）。检查 FCLK 是否在跑、"
                        "DDR 通道是否被别的 master 占死、WIN/参数是否合法"
                        % (float(timeout_s), int(cycles), fclk_hz() / 1e6,
                           int(cycles) / max(fclk_hz(), 1.0), ctx,
                           status_text(st)))
                time.sleep(poll)
            elapsed = time.time() - t0
            rc = int(self.ctrl.read(AP_RETURN))
            # k3 诊断（start 时清零，所以这里是**本层**的计数）
            stall_fill = int(self.ctrl.read(R_STALL_FILL))
            stall_mac = int(self.ctrl.read(R_STALL_MAC))
            bytes_in_hw = int(self.ctrl.read(R_BYTES_IN))
        except K3Error:
            raise
        except Exception as exc:                           # noqa: BLE001
            raise K3Error(
                "K3 IP 轮询/读返回码失败（%s）：%r；最后 STATUS: %s"
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
            raise K3Error(
                "K3 IP sticky_oob 置位：合并读口做过地址钳制 ⇒ 本层输出不可信"
                "（%s）。STATUS=%s。sticky 是**粘性**的，只有 PL 复位"
                "（重新加载 overlay）才清 —— 修好地址/形状后必须重载 bit。"
                % (ctx, status_text(st)))

        if rc != 0:
            code = (st >> ST_ERR_CODE_SHIFT) & ST_ERR_CODE_MASK
            if code == 0:
                code = rc
            reason = ERR_MEANING.get(rc, ERR_MEANING.get(code, "未知错误码"))
            if rc == 0xA:
                why = ("地址窗口越界：四个区 [addr, addr+bytes) 必须整体落在 "
                       "WIN=[%#x, %#x) 内（WIN_LO=min(地址)、WIN_HI=max(地址+"
                       "长度) 由本驱动按 RTL 记账口径写入；若 0xAC 显示某个区"
                       "为 0，先核对那个区的 bytes 口径）"
                       % (self.last_win[0], self.last_win[1]))
            else:
                why = reason
            self._print_diag("AP_RETURN=%#x" % rc, ctx, st=st)
            raise K3ReturnError(
                "K3 IP 返回错误码 %#x（%s）：%s；STATUS: %s；耗时 %.3fs；"
                "STALL_FILL=%d STALL_MAC=%d"
                % (rc, reason, why, status_text(st), elapsed, stall_fill,
                   stall_mac),
                return_code=rc, status=st)

        return K3RunResult(rc, elapsed, stall_fill=stall_fill,
                           stall_mac=stall_mac, bytes_in=bytes_in_hw,
                           status=st, return_code=rc, layer=None)

    def _print_diag(self, tag, ctx, st=None):
        """失败时打印"听诊器"（读寄存器本身可能再失败，不能让它盖住原因）。"""
        print("K3 IP 诊断（%s）：%s" % (tag, ctx), flush=True)
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
        """写参数/窗口 → 启动 → 轮询 STATUS → 读 AP_RETURN，返回 ``K3RunResult``。

        与 ``K1Engine.run`` **同名同签名同调用约定**（末尾多了可选的 ``layer``，
        只用于异常信息）：``rc, dt = eng.run(...)`` 照旧可用，另外能从返回值
        取 ``stall_fill/stall_mac/bytes_in``（见 ``K3RunResult``）。

        调用前自行 flush 输入，完成后自行 invalidate 输出（由
        ``PYNQEngine.conv`` 统一做）。

        ``out_lut`` / ``chsum``：k3 **没有**这两个融合出口，传了非 None 直接抛
        ``K3Unsupported``（上层路由会提前排除）。
        """
        t0, timeout_s, cycles, ctx = self.start(
            in_buf, wgt_buf, bias_buf, out_buf, H, W, cin, cout, k, s, d,
            pad, shift, relu=relu, out_lut=out_lut, chsum=chsum,
            timeout_s=timeout_s, c8_in=c8_in, c8_out=c8_out, layer=layer)
        res = self.wait(timeout_s, t0, cycles, ctx)
        res.layer = layer
        return res

    # ------------------------------------------------------------ 杂项

    def describe(self):
        return ("K3Engine(ip=%s, base=%#x, ctrl=%#x, offs=%#x, k=3, lanes=%d, "
                "cin_max=%d, cout_max=%d, H/W_max=%d, cap_b=%d, relu=%s, "
                "fclk=%.1fMHz)"
                % (self.discovered or self.ip_name, self.base_addr,
                   self.base_addr + CTRL_PAGE_OFF, self.base_addr,
                   self.lanes, self.cin_max, self.cout_max, self.hw_max,
                   self.cap_b,
                   "runtime" if self.relu_capable is None
                   else str(self.relu_capable),
                   fclk_hz() / 1e6))

    def close(self):
        """尽力释放 MMIO（PYNQ 的 MMIO 在 3.x 上有 close()）。"""
        for obj in (self.mmio,):
            fn = getattr(obj, "close", None)
            if fn is not None:
                try:
                    fn()
                except Exception:                          # noqa: BLE001
                    pass
        self.mmio = self.ctrl = self.offs = None


__all__ = [
    "K3Engine", "K3Error", "K3Unsupported", "K3Timeout", "K3ReturnError",
    "K3RunResult", "BufferBytes", "buffer_bytes", "window_of", "need_cap",
    "check_supported", "eligibility", "theoretical_cycles", "timeout_for",
    "status_text", "selftest_text", "win4_text", "relu_flags", "fclk_hz",
    "ceil8", "ERR_MEANING", "W_LAYOUT_K3", "DEFAULT_IP_NAME", "DEFAULT_BASE",
    "WINDOW_SIZE", "CTRL_PAGE_OFF", "K3_CAP_B", "K3_CIN_MAX", "K3_COUT_MAX",
    "K3_HW_MAX", "K3_SHIFT_MIN", "K3_SHIFT_MAX", "DEFAULT_LANES",
]
