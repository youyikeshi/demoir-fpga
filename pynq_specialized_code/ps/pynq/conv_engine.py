"""PYNQ 驱动：demoir conv2d_engine 卷积加速器。

寄存器定义来自 HLS 导出的驱动头文件；两个 AXI-Lite 的基地址
优先从 overlay 的 .hwh（ip_dict）自动发现，找不到时退回本比特流的
固定地址（0x4001_0000 / 0x4000_0000）。
"""
import time

# s_axi_ctrl 寄存器偏移
AP_CTRL, AP_RETURN = 0x00, 0x10
R_H, R_W, R_HOUT, R_WOUT = 0x18, 0x20, 0x28, 0x30
R_CIN, R_COUT, R_KSIZE = 0x38, 0x40, 0x48
R_STRIDE, R_DIL, R_PAD, R_SHIFT = 0x50, 0x58, 0x60, 0x68
R_RELU = 0x70               # v2：relu 旁路（1=ReLU；0=线性输出）

# s_axi_control 寄存器偏移（四个 m_axi 的基地址，offset=slave）
B_IN_FM, B_WGT, B_BIAS, B_OUT_FM = 0x10, 0x1C, 0x28, 0x34

# 融合出口（FUSE_OUT=1 构建）会多出两个 m_axi 基地址寄存器：out_lut / chsum。
# 这两个偏移**不能猜**：
#   · 交付版 bit（FUSE_OUT=0）里没有这两个寄存器，旧流程也从不写它们；
#   · RTL 已在 FUSE_OUT=1 分支里声明了 m_axi（depth 256 / 4096），但还没有
#     单独导出过 FUSE_OUT=1 的 IP，因此没有权威的 xconv2d_engine_hw.h 可读，
#     寄存器偏移表目前不存在。
# 等 FUSE_OUT=1 的 bit 导出后，从生成的驱动头文件里读出两个基地址寄存器
# 偏移，显式传进来：
#     Conv2dEngine(overlay, fuse_offsets=(out_lut偏移, chsum偏移))
# 未传时若调用方要求启用融合，会抛明确异常，而不是往未知地址写寄存器。
AP_START, AP_DONE = 0x1, 0x2


def _buf_addr(buf):
    a = getattr(buf, "device_address", None)
    if a is None:
        a = getattr(buf, "physical_address")
    return int(a)


def check_limits(H, W, cin, cout, k, s):
    """把"RTL 会算错"的参数组合挡在碰硬件之前（两条上界见 conv2d_engine.cpp 顶部）。

    抽成模块级函数是为了**离线可测**：本机没有 pynq 也能跑这几个断言。
    """
    if H <= 0 or W <= 0 or cin <= 0 or cout <= 0:
        raise ValueError("H/W/cin/cout 必须为正（H=%d W=%d cin=%d cout=%d）"
                         % (H, W, cin, cout))
    # 1) 地址算术：引擎内部 ic*H*W 用 int32
    if cin * H * W > 0x7FFFFFFF:
        raise ValueError("cin*H*W = %d 超过引擎的 int32 寻址上限；"
                         "换更小画幅或更少通道" % (cin * H * W))
    # 2) 累加器：HLS 里 acc_t = ap_int<26>，两个累加器各承担一半通道
    half = (cin + 1) // 2
    if half * k * k * 127 * 128 >= (1 << 26):
        raise ValueError("cin=%d k=%d 时单侧累加最大 %d 超过 26 位累加器（2^26）；"
                         "网络里最坏是 cin=192 k=3（约 14M，安全）"
                         % (cin, k, half * k * k * 127 * 128))


class Conv2dEngine:
    def __init__(self, overlay, ip_name=None, fuse_offsets=None):
        from pynq import MMIO        # 延迟导入：本机没有 pynq 时也能 import 本模块做静态检查
        self._MMIO = MMIO
        ctrl_addr, offs_addr, name = self._discover(overlay, ip_name)
        self.name = name
        self.ctrl = self._MMIO(ctrl_addr, 0x10000)
        self.offs = self._MMIO(offs_addr, 0x10000)
        # 融合出口的两个基地址寄存器偏移；None = 当前 bit 没有这两个寄存器
        if fuse_offsets is not None:
            if len(fuse_offsets) != 2:
                raise ValueError("fuse_offsets 必须是 (out_lut偏移, chsum偏移)")
            self.fuse_offsets = (int(fuse_offsets[0]), int(fuse_offsets[1]))
        else:
            self.fuse_offsets = None

    @staticmethod
    def _discover(ov, ip_name=None):
        """从 overlay.ip_dict 发现两个 AXI 基地址。

        PYNQ 2.6+ 的 hwh 解析器会为每个 AXI 从接口建条目，名字形如
        "conv2d_engine_0/s_axi_ctrl"（phys_addr 即基地址）；
        找不到时退回本比特流的固定 BD 地址分配。
        返回 (ctrl_addr, offs_addr, name)。
        """
        ctrl = offs = None
        name = None
        found_any = False
        for k, v in ov.ip_dict.items():
            if not isinstance(v, dict):
                continue
            if ip_name is not None and not k.startswith(ip_name):
                continue
            if "conv2d_engine" not in k and "conv2d_engine" not in str(v.get("type", "")):
                continue
            found_any = True
            pa = v.get("phys_addr")
            if pa is None:
                continue
            if k.endswith("/s_axi_ctrl"):
                ctrl = int(pa)
                name = k.rsplit("/", 1)[0]
            elif k.endswith("/s_axi_control"):
                offs = int(pa)
        if not found_any:
            raise RuntimeError("overlay 里找不到 conv2d_engine IP，"
                               "检查 .bit 与 .hwh 是否为同一比特流、文件名是否配对")
        if ctrl is None or offs is None:
            if ctrl is None:
                ctrl = 0x40010000
            if offs is None:
                offs = 0x40000000
            if name is None:
                name = next((k for k in ov.ip_dict if "conv2d_engine" in k),
                            "conv2d_engine_0")
            print("INFO: 未从 hwh 条目发现完整地址，使用固定地址 "
                  "ctrl=%#x offs=%#x" % (ctrl, offs))
        return ctrl, offs, name

    def run(self, in_buf, wgt_buf, bias_buf, out_buf,
            H, W, cin, cout, k, s, d, pad, shift, relu=True,
            out_lut=None, chsum=None, timeout_s=None):
        """写参数 → 启动 → 等 done。返回 (返回码, 耗时秒)。

        说明：缓冲区内容要与 PL 的可见性一致——调用前自行 flush 输入，
        完成后自行 invalidate 输出（见 test_on_board.py 的用法）。

        out_lut / chsum 是 FUSE_OUT=1 融合出口的可选缓冲区（256 项 int8 表 /
        每输出通道一个 int32）。默认 None：不启用融合，不写这两个寄存器，
        对交付版 bit 的行为与旧版**逐位不变**。要启用必须同时满足：
          · bit 是 FUSE_OUT=1 构建；
          · 构造时传入了 fuse_offsets（两个寄存器的偏移，见文件头注释）。
        否则启用会直接抛异常，避免往未知地址写寄存器。

        注意 chsum 目前只是**接口占位**：RTL 声明了端口但三处写回还没有累加
        逻辑，Runner/RefEngine/PYNQEngine 都拒绝传非 None 的 chsum。out_lut
        才是本任务真正接线的链路。

        timeout_s=None 时按工作量估算（见 estimate_seconds）。**不要再用 5 秒**：
        cosim 实测每"单元"（一个输出像素 × 一个输出通道 × 一个输入通道）约 12~17 拍，
        640 画幅最贵的一层就要十几秒，5 秒会在第一层大卷积上误超时。
        """
        hout = (H - 1) // s + 1
        wout = (W - 1) // s + 1
        check_limits(H, W, cin, cout, k, s)
        want_fuse = (out_lut is not None) or (chsum is not None)
        if want_fuse and self.fuse_offsets is None:
            raise RuntimeError(
                "当前 bit/驱动没有 out_lut、chsum 的 AXI-Lite 寄存器偏移："
                "FUSE_OUT=1 构建综合后把 Conv2dEngine(fuse_offsets=(lut, chsum)) "
                "传进来才能启用融合出口（不要猜偏移）")
        if timeout_s is None:
            timeout_s = max(30.0, 2.0 * self.estimate_seconds(H, W, cin, cout, k, s))
        # 1) 四个缓冲区基地址（offset=slave）
        #    注意：这类地址寄存器是 64 位，MMIO 一次只写 32 位低半部分。
        #    Zynq-7000 的 DDR 都在 4GB 以下，高半部分必须显式为 0，
        #    免得上一次（或别的驱动）留下的高半字把地址指飞。
        self.offs.write(B_IN_FM, _buf_addr(in_buf))
        self.offs.write(B_IN_FM + 4, 0)
        self.offs.write(B_WGT, _buf_addr(wgt_buf))
        self.offs.write(B_WGT + 4, 0)
        self.offs.write(B_BIAS, _buf_addr(bias_buf))
        self.offs.write(B_BIAS + 4, 0)
        self.offs.write(B_OUT_FM, _buf_addr(out_buf))
        self.offs.write(B_OUT_FM + 4, 0)
        # 1b) 融合出口的两个基地址（仅 FUSE_OUT=1 的 bit 有；None 写 0 =
        #     RTL 里 `if (out_lut != 0)` 不查表、不累加，等价旧行为）
        if self.fuse_offsets is not None:
            off_lut, off_sum = self.fuse_offsets
            self.offs.write(off_lut, 0 if out_lut is None else _buf_addr(out_lut))
            self.offs.write(off_lut + 4, 0)
            self.offs.write(off_sum, 0 if chsum is None else _buf_addr(chsum))
            self.offs.write(off_sum + 4, 0)
        # 2) 形状参数
        self.ctrl.write(R_H, H); self.ctrl.write(R_W, W)
        self.ctrl.write(R_HOUT, hout); self.ctrl.write(R_WOUT, wout)
        self.ctrl.write(R_CIN, cin); self.ctrl.write(R_COUT, cout)
        self.ctrl.write(R_KSIZE, k); self.ctrl.write(R_STRIDE, s)
        self.ctrl.write(R_DIL, d); self.ctrl.write(R_PAD, pad)
        self.ctrl.write(R_SHIFT, shift)
        self.ctrl.write(R_RELU, 1 if relu else 0)
        # 3) 启动并轮询
        t0 = time.time()
        self.ctrl.write(AP_CTRL, AP_START)
        while not (self.ctrl.read(AP_CTRL) & AP_DONE):
            if time.time() - t0 > timeout_s:
                raise TimeoutError(
                    "内核超时未完成：检查 FCLK 是否在跑、参数是否合法（<4096 等）")
        rc = self.ctrl.read(AP_RETURN)
        return rc, time.time() - t0

    @staticmethod
    def estimate_seconds(H, W, cin, cout, k, s):
        """这一层在 PL 上大约要多久（秒，**保守**估计）。

        校准点（2026-10-19，150MHz C/RTL cosim）：
          · 宽画幅 1×1：2.41 拍/单元；
          · 宽画幅 3×3：4.01 拍/单元；
          · 2×2、cin=352/cout=192 的 1×1：8.8 拍/单元（权重重装被放大）。
        模型按每个输出列块计入"计算 + 权重重装"，再乘 1.5 作为等待余量；
        比旧版的 25 拍/单元、100MHz 更贴近实测，同时仍留出板端 DDR 抖动余量。
        """
        hout = (H - 1) // s + 1
        wout = (W - 1) // s + 1
        bw = 32 if s == 2 else 64
        kk = 1 if k == 1 else (9 if k == 3 else 25)
        cycles = 0.0
        for x0 in range(0, wout, bw):
            xn = min(bw, wout - x0)
            cycles += hout * cout * cin * (2.5 * xn + 15.0 * kk)
        return 1.5 * cycles / 150e6
