#!/usr/bin/env python3
"""PYNQ 驱动：手写 k=1 卷积加速器 ``conv_k1_engine``（AXI-Lite 包装 ``k1_axi_wrap``）。

为什么单独一个文件
------------------
老的 HLS 引擎 ``Conv2dEngine``（conv_engine.py）有两个 AXI-Lite bundle，两个
基地址。新手写 IP 把它们**合成一个 128KB 的从机窗口**，用 ``addr[16]`` 分页：

* ``A[16]=1`` → **ctrl 页**（PS 侧地址 = 基址 + 0x10000）
* ``A[16]=0`` → **offs 页**（PS 侧地址 = 基址）

基址**不是固定常量**：任务书写 0x4000_0000，但当前
``pl/scripts/bd_add_k1.tcl`` 默认放在 **0x4004_0000**（0x4000/0x4001 被老
HLS 引擎占满；``K1_STEAL_ENGINE=1`` 时才放 0x4000_0000）。所以驱动一律从
``overlay.ip_dict`` 发现，找不到就报错，不猜。

两页的寄存器偏移与老 HLS 引擎**逐字一致**，所以上层（``PYNQEngine.conv``）
只需要换一个 ``run()`` 实现，不需要改地址公式、不需要改 C8 布局。

本文件的设计目标
----------------
1. **与 ``Conv2dEngine.run()`` 同名同签名**（末尾多一个可选的 ``layer=`` 仅用于
   报错信息），上层可以按层把寄存器参数原样转交；
2. **PC 上能 import**：``pynq`` 只在 ``K1Engine.__init__`` 里延迟导入，
   离线单测可以注入 ``mmio_factory``；
3. 所有"会算错"的参数都在**碰硬件之前**挡住（``check_supported``），
   与 RTL ``k1_axi_wrap`` 的启动前校验一一对应；
4. 轮询**必须带超时**：默认 = 理论拍数 × 3 / FCLK + 1 s，绝不无限等；
5. 返回码非 0 时抛带错误码含义的异常，异常信息包含层名、形状、STATUS。

硬件事实来源（不要凭记忆改）
----------------------------
* ``pl/rtl_hand/modules/k1_axi_wrap.v`` —— AXI-Lite 寄存器映射、启动前校验
* ``pl/rtl_hand/modules/conv_k1_engine.v`` —— 数据格式（C8 激活、weight[oc][ic]）
* ``docs/rtl_hand/conv_k1_engine.md`` §2/§4 —— 形状上界、每层拍数
* ``docs/rtl_hand/00_SPEC_接口契约.md`` §2 —— C8 地址公式

已知限制（详见 ``docs/部署/k1_IP接入.md``）
--------------------------------------------
* **只支持 C8 交错布局**（输入/输出都必须是 C8）；CHW 下必须回退老引擎。
* ``RELU``：**运行期可控**（0x70 bit0 → ctrl_relu），``relu=False`` 的线性层也能走本 IP。
* 没有 ``out_lut`` / ``chsum`` 融合出口；开了融合的层必须回退老引擎。
* 权重容器必须是 ``[cout, cin, 1, 1]`` 的 ``oc_ic_ky_kx`` 连续字节。
"""

import os
import time

# ---------------------------------------------------------------- 常量

#: BD 里约定的实例名；发现逻辑同时兼容 ``k1_axi_wrap`` / ``conv_k1_engine``。
DEFAULT_IP_NAME = "k1_wrap"

#: AXI-Lite 从机窗口大小：128KB（地址由 BD 分配）。
WINDOW_SIZE = 0x20000
#: 仅作"没有 overlay / strict=False"时的兜底；**不要**在正常上板时依赖它。
#: 任务书写的 0x4000_0000；当前 bd_add_k1.tcl 默认 0x4004_0000，以 .hwh 为准。
DEFAULT_BASE = 0x40000000
CTRL_PAGE_OFF = 0x10000          # addr[16]=1 的 ctrl 页

# ---- ctrl 页寄存器偏移（与老 HLS 引擎 s_axi_ctrl / k1_axi_wrap 一致）----
AP_CTRL, AP_RETURN = 0x00, 0x10
R_H, R_W, R_HOUT, R_WOUT = 0x18, 0x20, 0x28, 0x30
R_CIN, R_COUT, R_KSIZE = 0x38, 0x40, 0x48
R_STRIDE, R_DIL, R_PAD, R_SHIFT = 0x50, 0x58, 0x60, 0x68
R_RELU = 0x70
R_STATUS = 0x94

# ---- offs 页寄存器偏移（四个 64 位缓冲区地址）----
B_IN_FM, B_WGT, B_BIAS, B_OUT_FM = 0x10, 0x1C, 0x28, 0x34

AP_START = 0x1

# STATUS 位（k1_axi_wrap.v）
ST_DONE = 1 << 0
ST_BUSY = 1 << 1
ST_ERR = 1 << 2
ST_ERR_CODE_SHIFT = 4
ST_ERR_CODE_MASK = 0xF
ST_IDLE = 1 << 8

# relu_en 的位定义（与 conv_engine.relu_flags 一致；本 wrapper 只回显 bit0，
# bit2/bit3 是 C8 声明位。K1 是纯 C8 构建，所以这里恒置两位，便于未来 RTL 校验）
RELU_BIT, C8_IN_BIT, C8_OUT_BIT = 0, 2, 3

# 本 IP 的硬约束（k1_axi_wrap 的参数默认值；如 BD 改过 CIN_MAX/COUT_MAX，
# 用 K1_IP_CIN_MAX / K1_IP_COUT_MAX 环境变量覆盖，**不要改这里的默认值**）
K1_CIN_MIN = 8
K1_CIN_MAX = 352
K1_COUT_MIN = 1
K1_COUT_MAX = 192
K1_SHIFT_MIN, K1_SHIFT_MAX = 1, 31
K1_HW_MAX = 4096
DEFAULT_LANES = 32

# AP_RETURN / STATUS[7:4] 的错误码含义（k1_axi_wrap.v 与 HLS conv2d_accel.h 一致）
ERR_MEANING = {
    0: "成功",
    1: "ERR_NULL：四个缓冲区地址里有 0",
    2: "ERR_SIZE：形状/参数越界（或 k/s/d/pad 不是 1/1/1/0，或地址高字非 0）",
    3: "ERR_SHIFT：shift 不在 1..31",
    4: "ERR_LAYOUT：C8 布局声明不符（老 HLS 引擎用；K1 wrapper 目前不检查）",
}


# ---------------------------------------------------------------- 异常

class K1Error(RuntimeError):
    """K1 IP 相关的所有异常基类（上层捕获它就够）。"""


class K1Unsupported(K1Error):
    """本层参数不在 K1 IP 的支持范围内（**在碰硬件之前**抛出）。"""


class K1Timeout(K1Error):
    """等待 done 超时。"""


class K1ReturnError(K1Error):
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
    """当前假定的 PL 时钟（Hz）。优先 K1_IP_FCLK_HZ，其次 K1_IP_FCLK_MHZ。"""
    if str(os.environ.get("K1_IP_FCLK_HZ", "")).strip():
        return _env_float("K1_IP_FCLK_HZ", 150e6)
    mhz = _env_float("K1_IP_FCLK_MHZ", 150.0)
    return mhz * 1e6 if mhz > 0 else 150e6


def _buf_addr(buf):
    """取 PYNQ 缓冲区的物理地址（兼容 PYNQ 2.x/3.x 的两种属性名）。"""
    a = getattr(buf, "device_address", None)
    if a is None:
        a = getattr(buf, "physical_address", None)
    if a is None:
        raise K1Unsupported("缓冲区没有 device_address/physical_address 属性：%r"
                            % (type(buf).__name__,))
    return int(a)


def relu_flags(relu=True, c8_in=True, c8_out=True):
    """拼 relu_en：bit0=relu、bit2=输入 C8、bit3=输出 C8（与 conv_engine 同式）。"""
    return ((1 if relu else 0)
            | (1 << C8_IN_BIT if c8_in else 0)
            | (1 << C8_OUT_BIT if c8_out else 0))


def status_text(st):
    """把 STATUS 值翻译成一行诊断文本。"""
    st = int(st) & 0xFFFFFFFF
    code = (st >> ST_ERR_CODE_SHIFT) & ST_ERR_CODE_MASK
    return ("done=%d busy=%d err=%d err_code=%d(%s) idle=%d"
            % ((st & ST_DONE) != 0, (st & ST_BUSY) != 0, (st & ST_ERR) != 0,
               code, ERR_MEANING.get(code, "未知错误码"), (st & ST_IDLE) != 0))


def theoretical_cycles(H, W, cin, cout, lanes=DEFAULT_LANES, k=1):
    """本层理论拍数（conv_k1_engine.md §4）：

        H·W·⌈cout/lanes⌉·cin

    k≠1 时乘 k²，**仅供显示/估算**（k≠1 不可路由到本 IP）。
    """
    H, W, cin, cout = int(H), int(W), int(cin), int(cout)
    lanes = max(1, int(lanes))
    passes = (cout + lanes - 1) // lanes
    cycles = max(0, H) * max(0, W) * passes * max(0, cin)
    if int(k) > 1:
        cycles *= int(k) * int(k)
    return int(cycles)


def timeout_for(H, W, cin, cout, k=1, s=1, lanes=DEFAULT_LANES):
    """单层等待上限（秒）= 理论拍数 × 系数 / FCLK + 余量。

    系数默认 12.0、余量默认 1.0 s（环境变量 ``K1_IP_TIMEOUT_FACTOR`` /
    ``K1_IP_TIMEOUT_EXTRA_S``）。

    系数为何从 3.0 提到 12.0（2026-10-27 上板实测）：
    ``theoretical_cycles`` 算的是**算力**下界（每拍一次 MAC），而本 IP 的输入
    预取是 ``m_axi_in_arlen = 0`` 的**单拍读**：一层 (320,320,cin=144,cout=48)
    要发 3.2M 笔 8 字节读，实测耗时是理论值的 **6 倍以上**（理论 0.276 s，
    3 倍系数给到 1.83 s 仍然超时；放到 30 倍才通过）。也就是说此时瓶颈是
    AXI 单拍事务而不是 MAC。系数 12 在实测 6× 之上再留一倍余量；它只影响
    「真挂死时要等多久才报错」，不影响正常情况下任何一层的判定。
    """
    del s  # 本 IP 只支持 stride=1；留着参数是为了与老引擎签名一致
    factor = _env_float("K1_IP_TIMEOUT_FACTOR", 12.0)
    extra = _env_float("K1_IP_TIMEOUT_EXTRA_S", 1.0)
    cycles = theoretical_cycles(H, W, cin, cout, lanes=lanes, k=k)
    return max(0.05, factor * cycles / max(fclk_hz(), 1.0) + extra)


# ---------------------------------------------------------------- 支持性判定

def check_supported(H, W, cin, cout, k, s, d, pad, shift, relu=True,
                    c8_in=True, c8_out=True, relu_capable=1,
                    hout=None, wout=None,
                    cin_max=K1_CIN_MAX, cout_max=K1_COUT_MAX):
    """返回**不支持的原因列表**（空列表 = 可以走 K1 IP）。

    纯函数：不导入 pynq、不碰硬件，本机可单测。判定顺序与 RTL
    ``k1_axi_wrap`` 的启动前校验一致；任何一条不满足都必须在写 AP_CTRL
    之前挡住（RTL 也会返回 ERR_SIZE，但那时已经算一次"失败启动"了）。
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
    if H > K1_HW_MAX or W > K1_HW_MAX:
        reasons.append("H/W 超过 %d（H=%d W=%d）" % (K1_HW_MAX, H, W))
    # RTL 的字节地址是 32 位（hw8_r / pass_gbase 都是 32 位）：单个张量
    # max(cin,cout)*H*W 超过 2^31-1 就可能绕回。本网络最大约 2.3e7（约 1%）。
    if max(cin, cout) * H * W > 0x7FFFFFFF:
        reasons.append("max(cin,cout)*H*W = %d 超过 2^31-1（RTL 是 32 位地址）"
                       % (max(cin, cout) * H * W))
    hout = ((H - 1) // s + 1) if hout is None and s > 0 else hout
    wout = ((W - 1) // s + 1) if wout is None and s > 0 else wout
    if hout is not None and int(hout) != H:
        reasons.append("HOUT=%s ≠ H=%d（本 IP 只支持 s=1/pad=0 的 same 形状）"
                       % (hout, H))
    if wout is not None and int(wout) != W:
        reasons.append("WOUT=%s ≠ W=%d（本 IP 只支持 s=1/pad=0 的 same 形状）"
                       % (wout, W))
    if k != 1:
        reasons.append("k=%d ≠ 1（本 IP 只做 1×1）" % k)
    if s != 1:
        reasons.append("stride=%d ≠ 1" % s)
    if d != 1:
        reasons.append("dilation=%d ≠ 1" % d)
    if pad != 0:
        reasons.append("pad=%d ≠ 0" % pad)
    if cin < K1_CIN_MIN or cin > int(cin_max):
        reasons.append("cin=%d 不在 [%d,%d]" % (cin, K1_CIN_MIN, int(cin_max)))
    elif cin % 8:
        reasons.append("cin=%d 不是 8 的倍数（C8 布局要求）" % cin)
    if cout < K1_COUT_MIN or cout > int(cout_max):
        reasons.append("cout=%d 不在 [%d,%d]"
                       % (cout, K1_COUT_MIN, int(cout_max)))
    if shift < K1_SHIFT_MIN or shift > K1_SHIFT_MAX:
        reasons.append("shift=%d 不在 [%d,%d]"
                       % (shift, K1_SHIFT_MIN, K1_SHIFT_MAX))
    # ReLU 是**运行期**可控的：wrapper 把 0x70 bit0 接到 k1_axi_wrap.v:489
    # 的 .ctrl_relu(r_relu[0])，引擎在 conv_k1_engine.v:656 用它决定是否 clamp。
    # 复位默认 = 编译期 RELU 参数，但驱动每次 run() 都会写 0x70（k1_engine.py:638
    # → relu_flags()），所以 relu=True/False 都支持。
    #   2026-10-27 由父 agent 核实：RTL 有 r_relu 寄存器（写入/复位/接线三处齐全），
    #   且仿真里 relu=0 的 3 个用例逐字节通过（133120/133120）。
    #   放松此判定使 k1 可路由层数 108 → 132（整网 24 个 conv_proj 是 relu=False）。
    # relu_capable 仅保留为「编译期默认值」的说明，不再作为硬门槛。
    if bool(relu) is False and int(relu_capable) == 0:
        pass   # 同为 0：一致，放行（无需提示）
    if not bool(c8_in):
        reasons.append("输入不是 C8 布局（本 IP 只支持 C8 交错布局）")
    if not bool(c8_out):
        reasons.append("输出不是 C8 布局（本 IP 只支持 C8 交错布局）")
    return reasons


def eligibility(H, W, cin, cout, k, s, d, pad, shift, relu=True,
                c8_in=True, c8_out=True, relu_capable=1, hout=None, wout=None,
                cin_max=K1_CIN_MAX, cout_max=K1_COUT_MAX):
    """``check_supported`` 的非抛出版本：返回 ``(ok, reason)``。"""
    reasons = check_supported(H, W, cin, cout, k, s, d, pad, shift,
                              relu=relu, c8_in=c8_in, c8_out=c8_out,
                              relu_capable=relu_capable, hout=hout, wout=wout,
                              cin_max=cin_max, cout_max=cout_max)
    return (not reasons, "; ".join(reasons))


# ---------------------------------------------------------------- MMIO 分页

class _Page(object):
    """把一整块 128KB 窗口里的某一页包装成 ``read(off)/write(off,val)``。

    这样 ``self.ctrl.write(R_H, H)`` 的写法与 ``Conv2dEngine`` 完全一致，
    实际落到 ``mmio[0x10000 + 0x18]``（= PS 地址 `<BASE>+0x10018`，addr[16]=1）。
    """

    __slots__ = ("mmio", "base_off")

    def __init__(self, mmio, base_off):
        self.mmio = mmio
        self.base_off = int(base_off)

    def read(self, off=0):
        return int(self.mmio.read(self.base_off + int(off)))

    def write(self, off, val):
        self.mmio.write(self.base_off + int(off), int(val) & 0xFFFFFFFF)


# ---------------------------------------------------------------- 驱动

class K1Engine(object):
    """``conv_k1_engine`` 的 AXI-Lite 驱动。

    参数
    ----
    overlay : pynq.Overlay
        用来从 ``ip_dict`` 自动发现 ``k1_wrap`` 的基地址。**强烈建议传**：
        找不到时默认直接抛错，避免误把老 HLS 引擎的 0x4000_0000 当成 K1。
    ip_name : str
        BD 实例名，默认 ``k1_wrap``（也接受 ``K1_IP_NAME`` 环境变量）。
    base_addr : int
        显式指定基地址（跳过发现）。仅用于离线单测 / 上板前临时验证。
    mmio_factory : callable
        默认 ``pynq.MMIO``；离线测试注入假实现即可。
    strict : bool
        True（默认）：overlay 里找不到 IP 就抛错；False：退回固定地址并大声警告。
    """

    def __init__(self, overlay=None, ip_name=None, base_addr=None,
                 mmio_factory=None, strict=True):
        self.ip_name = (ip_name
                        or os.environ.get("K1_IP_NAME", "").strip()
                        or DEFAULT_IP_NAME)
        self.discovered = None
        self.base_addr = None
        self.mmio = None
        self.ctrl = None
        self.offs = None
        self.n_runs = 0
        self.total_seconds = 0.0
        self.last_status = 0
        self.last_return = None
        self.relu_capable = 1 if _env_int("K1_IP_RELU", 1) else 0
        self.lanes = max(1, _env_int("K1_IP_LANES", DEFAULT_LANES))
        self.cin_max = _env_int("K1_IP_CIN_MAX", K1_CIN_MAX)
        self.cout_max = _env_int("K1_IP_COUT_MAX", K1_COUT_MAX)

        base = int(base_addr) if base_addr is not None else None
        if base is None:
            env_base = os.environ.get("K1_IP_BASE", "").strip()
            if env_base:
                base = int(env_base, 0)
                print("WARN: K1 IP 使用固定基地址 %#x（K1_IP_BASE，跳过 hwh 发现）"
                      "——先确认这个窗口真的是 k1_wrap，不是老引擎" % base)
            elif overlay is not None:
                found = self.discover(overlay, self.ip_name)
                if found is None:
                    msg = ("overlay 里找不到 K1 IP（实例名按 '%s' 找，也试了 "
                           "k1_axi_wrap/conv_k1_engine）；.hwh 与 .bit 是否同一版？"
                           % self.ip_name)
                    if strict:
                        raise RuntimeError(msg)
                    base = DEFAULT_BASE
                    print("WARN: %s → 回退固定基地址 %#x（危险：可能是老引擎）"
                          % (msg, base))
                else:
                    base, key, rng = found
                    self.discovered = key
                    print("INFO: K1 IP 发现 '%s' @ %#x（窗口 %#x）"
                          % (key, base, rng))
                    if rng and rng < WINDOW_SIZE:
                        msg = ("hwh 里 k1 条目窗口只有 %#x < 128KB（ctrl 页在 "
                               "+0x10000，写进去会落到窗口外/别的 IP）" % rng)
                        if self._page2_present(overlay, base):
                            print("INFO: hwh 把 128KB 拆成两页（发现 +0x10000 "
                                  "的同名条目），按整窗口继续")
                        elif strict:
                            raise RuntimeError(msg + "；请把 BD 地址范围改成 "
                                               "128KB（bd_add_k1.tcl 的 128K）")
                        else:
                            print("WARN: " + msg + "；strict=False，继续（危险）")
            else:
                msg = ("没有 overlay 也没有 K1_IP_BASE：无法定位 k1_wrap 的 "
                       "AXI-Lite 基地址（本机静态检查请只 import，不要实例化）")
                if strict:
                    raise RuntimeError(msg)
                base = DEFAULT_BASE
                print("WARN: %s → 回退固定基地址 %#x" % (msg, base))

        self.base_addr = int(base)
        if self.base_addr % WINDOW_SIZE:
            msg = ("K1 基地址 %#x 不是 128KB 对齐：RTL 用 addr[16] 分页，"
                   "非对齐基址会把 ctrl/offs 页选反" % self.base_addr)
            if strict:
                raise RuntimeError(msg)
            print("WARN: " + msg + "（strict=False，继续）")
        if mmio_factory is None:
            try:
                from pynq import MMIO
            except Exception as exc:                       # noqa: BLE001
                raise RuntimeError(
                    "构造 K1Engine 需要 pynq.MMIO（本机没有 pynq 时只能 import "
                    "本模块做静态检查）：%r" % (exc,))
            mmio_factory = MMIO
        # 一整块 128KB 窗口：A[16] 选页，所以 ctrl 页 = base + 0x10000
        self.mmio = mmio_factory(self.base_addr, WINDOW_SIZE)
        self.ctrl = _Page(self.mmio, CTRL_PAGE_OFF)
        self.offs = _Page(self.mmio, 0)

    # ------------------------------------------------------------ 地址发现

    @staticmethod
    def discover(overlay, ip_name=DEFAULT_IP_NAME):
        """从 ``overlay.ip_dict`` 找 K1 从机，返回 ``(base, key, addr_range)``。

        兼容三种 hwh 解析结果：
          1. 单个 128KB 条目 ``"k1_wrap"``；
          2. 带接口名的条目 ``"k1_wrap/s_axi"``；
          3. hwh 把窗口拆成两个 64KB 条目（取较低基地址）。
        找不到返回 None（不猜地址）。
        """
        aliases = [ip_name, DEFAULT_IP_NAME, "k1_axi_wrap", "conv_k1_engine"]
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
            if key == ip_name or top in aliases:
                exact.append((str(key), base, rng))
            elif ("k1_axi_wrap" in typ or "conv_k1_engine" in typ
                  or ("k1" in str(key).lower() and "wrap" in str(key).lower())):
                loose.append((str(key), base, rng))
        cands = exact if exact else loose
        if not cands:
            return None
        # 优先 128KB 整窗口条目；多个时优先精确实例名，再取最低基地址
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
            if ("k1" in low or "wrap" in low
                    or "k1_axi_wrap" in typ or "conv_k1_engine" in typ):
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
    def estimate_seconds(H, W, cin, cout, k=1, s=1):
        """与 ``Conv2dEngine.estimate_seconds`` 同签名；返回本 IP 的理论耗时。

        只用于进度打印/规划，不含 PS 路由开销。系数 1.1 是给 AR 延迟留的
        经验余量；**超时**另用 ``timeout_for``（3× + 1 s）。
        """
        cycles = theoretical_cycles(H, W, cin, cout, lanes=DEFAULT_LANES, k=k)
        return 1.1 * cycles / max(fclk_hz(), 1.0)

    def timeout_seconds(self, H, W, cin, cout, k=1, s=1):
        return timeout_for(H, W, cin, cout, k=k, s=s, lanes=self.lanes)

    # ------------------------------------------------------------ 主调用

    def _prepare(self, in_buf, wgt_buf, bias_buf, out_buf,
                 H, W, cin, cout, k, s, d, pad, shift, relu=True,
                 out_lut=None, chsum=None, timeout_s=None,
                 c8_in=False, c8_out=False, layer=None):
        """纯软件预检 + 地址/超时计算（**不碰硬件**）。"""
        layer = layer or "-"
        reasons = check_supported(
            H, W, cin, cout, k, s, d, pad, shift,
            relu=relu, c8_in=c8_in, c8_out=c8_out,
            relu_capable=self.relu_capable,
            cin_max=self.cin_max, cout_max=self.cout_max)
        if out_lut is not None:
            reasons.append("不支持 out_lut 融合出口（本 IP 没有该寄存器）")
        if chsum is not None:
            reasons.append("不支持 chsum 融合出口（本 IP 没有该寄存器）")
        ctx = ("layer=%s H=%d W=%d cin=%d cout=%d k=%d s=%d d=%d pad=%d "
               "shift=%s relu=%s C8(in/out)=%s/%s"
               % (layer, int(H), int(W), int(cin), int(cout), int(k), int(s),
                  int(d), int(pad), shift, bool(relu), bool(c8_in), bool(c8_out)))
        if reasons:
            raise K1Unsupported("K1 IP 不接受本层（%s）：%s"
                                % (ctx, "; ".join(reasons)))

        # 缓冲区地址（Zynq-7000 只有 32 位，高字必须为 0）
        try:
            addrs = [_buf_addr(b) for b in (in_buf, wgt_buf, bias_buf, out_buf)]
        except Exception as exc:                           # noqa: BLE001
            raise K1Error("取缓冲区物理地址失败（%s）：%r" % (ctx, exc)) from exc
        names = ("in", "wgt", "bias", "out")
        for nm, a in zip(names, addrs):
            if a <= 0:
                raise K1Unsupported("%s 缓冲区地址为 0（%s）" % (nm, ctx))
            if a > 0xFFFFFFFF:
                raise K1Unsupported(
                    "%s 缓冲区地址 %#x 超过 32 位（本 IP 只有 32 位 AXI 地址；"
                    "地址高字必须为 0，%s）" % (nm, a, ctx))

        if timeout_s is None:
            timeout_s = timeout_for(H, W, cin, cout, k=k, s=s, lanes=self.lanes)
        timeout_s = float(timeout_s)
        if timeout_s <= 0:
            raise ValueError("timeout_s 必须 > 0（收到 %r）" % timeout_s)
        cycles = theoretical_cycles(H, W, cin, cout, lanes=self.lanes, k=k)
        return addrs, timeout_s, cycles, ctx

    def start(self, in_buf, wgt_buf, bias_buf, out_buf,
              H, W, cin, cout, k, s, d, pad, shift, relu=True,
              out_lut=None, chsum=None, timeout_s=None,
              c8_in=False, c8_out=False, layer=None):
        """写全部寄存器 + ``AP_CTRL`` bit0，**返回后不等**。

        正常调用用 ``run()``。只有需要分步调试时才单独用它::

            t0, timeout_s, cycles, ctx = eng.start(...)
            rc, dt = eng.wait(timeout_s, t0, cycles, ctx)

        返回 ``(t0, timeout_s, cycles, ctx)``；``ctx`` 是给 ``wait()`` 拼错误
        信息用的上下文串。
        """
        addrs, timeout_s, cycles, ctx = self._prepare(
            in_buf, wgt_buf, bias_buf, out_buf, H, W, cin, cout, k, s, d,
            pad, shift, relu=relu, out_lut=out_lut, chsum=chsum,
            timeout_s=timeout_s, c8_in=c8_in, c8_out=c8_out, layer=layer)
        names = ("in", "wgt", "bias", "out")
        st = 0
        try:
            # 上一笔还没结束就启动会被 RTL 忽略（start 只在 !ctrl_busy 时生效），
            # 随后轮询会读到上一笔的 done → 误判。先等 busy 落下。
            st = self.status()
            if st & ST_BUSY:
                t_wait = time.time()
                limit = min(timeout_s, 5.0)
                while (st & ST_BUSY) and (time.time() - t_wait < limit):
                    time.sleep(0.0002)
                    st = self.status()
                if st & ST_BUSY:
                    raise K1Timeout(
                        "启动前上一笔 K1 任务仍 busy（等 %.1fs）：%s；%s"
                        % (limit, status_text(st), ctx))

            # 四个 64 位缓冲区基地址（低字在偏移处、高字在 +4）
            for nm, off, a in zip(names,
                                  (B_IN_FM, B_WGT, B_BIAS, B_OUT_FM), addrs):
                self.offs.write(off, a & 0xFFFFFFFF)
                self.offs.write(off + 4, (a >> 32) & 0xFFFFFFFF)

            # 形状参数（与老引擎的偏移逐一对应）
            self.ctrl.write(R_H, H)
            self.ctrl.write(R_W, W)
            self.ctrl.write(R_HOUT, (int(H) - 1) // int(s) + 1)
            self.ctrl.write(R_WOUT, (int(W) - 1) // int(s) + 1)
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
        except K1Error:
            raise
        except Exception as exc:                           # noqa: BLE001
            raise K1Error(
                "K1 IP 启动写寄存器失败（%s）：%r；最后 STATUS: %s"
                % (ctx, exc, status_text(st))) from exc
        return t0, timeout_s, cycles, ctx

    def wait(self, timeout_s, t0, cycles, ctx):
        """轮询 ``STATUS``（done/err）→ 读 ``AP_RETURN``，返回 ``(返回码, 秒)``。

        与 ``start()`` 配对使用；``run()`` 内部就是 start + wait。
        必须带超时：理论拍数 × 3 / FCLK + 1 s（见 ``timeout_for``）。
        """
        st = 0
        try:
            poll = 0.00005 if float(timeout_s) < 0.5 else 0.0001
            while True:
                st = self.status()
                if st & (ST_DONE | ST_ERR):
                    break
                if (time.time() - t0) > float(timeout_s):
                    raise K1Timeout(
                        "K1 IP 超时 %.2fs 未完成（理论 %d 拍 @ %.1f MHz ⇒ 约 "
                        "%.3fs；%s；最后 STATUS: %s）。检查 FCLK 是否在跑、"
                        "DDR 通道是否被别的 master 占死、参数是否合法"
                        % (float(timeout_s), int(cycles), fclk_hz() / 1e6,
                           int(cycles) / max(fclk_hz(), 1.0), ctx,
                           status_text(st)))
                time.sleep(poll)
            elapsed = time.time() - t0
            rc = int(self.ctrl.read(AP_RETURN))
        except K1Error:
            raise
        except Exception as exc:                           # noqa: BLE001
            raise K1Error(
                "K1 IP 轮询/读返回码失败（%s）：%r；最后 STATUS: %s"
                % (ctx, exc, status_text(st))) from exc

        self.last_status = st
        self.last_return = rc
        self.n_runs += 1
        self.total_seconds += elapsed

        if rc != 0:
            code = (st >> ST_ERR_CODE_SHIFT) & ST_ERR_CODE_MASK
            if code == 0:
                code = rc
            raise K1ReturnError(
                "K1 IP 返回错误码 %d（%s）：%s；STATUS: %s；耗时 %.3fs"
                % (rc, ERR_MEANING.get(code, "未知错误码"), ctx,
                   status_text(st), elapsed),
                return_code=rc, status=st)

        return rc, elapsed

    def run(self, in_buf, wgt_buf, bias_buf, out_buf,
            H, W, cin, cout, k, s, d, pad, shift, relu=True,
            out_lut=None, chsum=None, timeout_s=None, c8_in=False, c8_out=False,
            layer=None):
        """写参数 → 启动 → 轮询 STATUS → 读 AP_RETURN，返回 ``(返回码, 耗时秒)``。

        与 ``Conv2dEngine.run`` **同名同签名**（末尾多了可选的 ``layer``，
        只用于异常信息）。调用约定也一致：调用前自行 flush 输入，完成后自行
        invalidate 输出（由 ``PYNQEngine.conv`` 统一做）。

        ``out_lut`` / ``chsum``：本 IP **没有**这两个融合出口，传了非 None
        直接抛 ``K1Unsupported``（上层路由会提前排除，不会走到这里）。
        """
        t0, timeout_s, cycles, ctx = self.start(
            in_buf, wgt_buf, bias_buf, out_buf, H, W, cin, cout, k, s, d,
            pad, shift, relu=relu, out_lut=out_lut, chsum=chsum,
            timeout_s=timeout_s, c8_in=c8_in, c8_out=c8_out, layer=layer)
        return self.wait(timeout_s, t0, cycles, ctx)

    # ------------------------------------------------------------ 杂项

    def describe(self):
        return ("K1Engine(ip=%s, base=%#x, ctrl=%#x, offs=%#x, RELU=%d, "
                "lanes=%d, cin_max=%d, cout_max=%d, fclk=%.1fMHz)"
                % (self.discovered or self.ip_name, self.base_addr,
                   self.base_addr + CTRL_PAGE_OFF, self.base_addr,
                   self.relu_capable, self.lanes, self.cin_max, self.cout_max,
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
    "K1Engine", "K1Error", "K1Unsupported", "K1Timeout", "K1ReturnError",
    "check_supported", "eligibility", "theoretical_cycles", "timeout_for",
    "status_text", "relu_flags", "fclk_hz", "ERR_MEANING",
    "DEFAULT_IP_NAME", "DEFAULT_BASE", "WINDOW_SIZE", "CTRL_PAGE_OFF",
]
