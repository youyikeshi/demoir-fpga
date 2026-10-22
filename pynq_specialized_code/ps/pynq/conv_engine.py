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
R_RELU = 0x70               # v2：relu_en 寄存器。**不是布尔**（2026-10-23 起）：
                            #   bit0 = ReLU 使能（原有语义）
                            #   bit2/bit3 = C8 布局的**声明位**（不是选路位）
                            # 纯编译期 C8 构建（C8_LAYOUT=1）要求两位**都为 1**，否则
                            # 返回 ERR_LAYOUT(4)；硬件里没有 CHW 通路，这两位不参与
                            # 任何地址计算（所以没有 mux 代价）。
                            # C8_LAYOUT=0 的综合构建完全忽略这两位的值。
# relu_en 的位定义（与 pl/src/conv2d_engine.h 的 RELU_EN_* 一一对应）
RELU_BIT, C8_IN_BIT, C8_OUT_BIT = 0, 2, 3


def relu_flags(relu=True, c8_in=False, c8_out=False):
    """拼 relu_en 寄存器值：bit0=relu、bit2=输入 C8、bit3=输出 C8（三者独立）。

    这是规范 `设计_PS侧C8整合规范.md` §2 给出的唯一公式::

        flags = (1 if relu else 0) | (4 if in_is_c8 else 0) | (8 if out_is_c8 else 0)

    C8 关闭时 c8_in/c8_out 都是 False ⇒ 返回值只有 0/1，与整合前完全一致。

    C8_LAYOUT=1 的 bit（纯编译期 C8）要求两位都为 1：整网所有 int8 张量都按
    ``ceil(C/8)*8`` 通道分配的 C8 缓冲区，所以恒传 True。
    """
    return ((1 if relu else 0)
            | (1 << C8_IN_BIT if c8_in else 0)
            | (1 << C8_OUT_BIT if c8_out else 0))


# s_axi_control 寄存器偏移（四个 m_axi 的基地址，offset=slave）
B_IN_FM, B_WGT, B_BIAS, B_OUT_FM = 0x10, 0x1C, 0x28, 0x34

# 融合出口会多出 m_axi 基地址寄存器：out_lut（FUSE_OUT=1）/ chsum（FUSE_SUM=1）。
# 这两个偏移**不能猜**，必须从该次综合导出的 xconv2d_engine_hw.h 里读：
#     Conv2dEngine(overlay, fuse_offsets=(out_lut偏移, chsum偏移))
# 两个开关是**独立**的，所以任一位置可以是 None，表示"本构建没有这个寄存器"：
#     fuse_offsets=(None, 0x40)   # 只有 FUSE_SUM=1：只写 chsum，绝不碰 out_lut
# 为 None 的那个寄存器**一个字节都不写**——AXI-Lite 里相邻的 0x40 在不同构建
# 里可能是别的端口，写错就是一次地址指飞（2026-10-22 已因此把板子挂死过一次）。
#
# 已知值（FUSE_SUM=1、FUSE_OUT=0 的 cosim 导出，见
# pl/proj/engine_v1/solution1/.autopilot/db/coregen/control.h）：
#     XCONV2D_ENGINE_CONTROL_ADDR_CHSUM_DATA = 0x40（64 位，高半字 0x44）
# 未传偏移时若调用方要求启用融合，会抛明确异常，而不是往未知地址写寄存器。
AP_START, AP_DONE = 0x1, 0x2


def norm_fuse_offset(v):
    """把"偏移"归一化成 int 或 None（None/空串/none/- 都表示本构建没有该寄存器）。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s.lower() in ("", "none", "-", "null"):
            return None
        return int(s, 0)
    return int(v)


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
        # 融合出口的两个基地址寄存器偏移；None = 当前 bit 没有这个寄存器。
        # 两个开关独立：只启用 chsum 时传 (None, 0x40)。
        if fuse_offsets is not None:
            if len(fuse_offsets) != 2:
                raise ValueError(
                    "fuse_offsets 必须是 (out_lut偏移, chsum偏移)，"
                    "没有的那个位置用 None（例：(None, 0x40) 表示只有 FUSE_SUM）")
            off_lut = norm_fuse_offset(fuse_offsets[0])
            off_sum = norm_fuse_offset(fuse_offsets[1])
            if off_lut is None and off_sum is None:
                raise ValueError(
                    "fuse_offsets 的两个偏移都是 None：没有可写的融合寄存器；"
                    "两个都不需要就别传 fuse_offsets")
            self.fuse_offsets = (off_lut, off_sum)
            # ★ 构造即清零（2026-10-22 事故后加固）：
            # 融合出口的地址寄存器是**未初始化的 AXI-Lite 寄存器**。若第一次
            # run() 之前有任何路径启动引擎，RTL 的 `if (out_lut != 0)` /
            # `if (chsum != 0)` 会判真，引擎就拿垃圾地址去读写 → 未映射 AXI
            # 访问 → HP 口死锁 → PS 挂死。所以拿到偏移后立刻写 0，不留窗口。
            for off in (off_lut, off_sum):
                if off is None:          # 本构建没这个寄存器 → 一个字节都不写
                    continue
                self.offs.write(off, 0)
                self.offs.write(off + 4, 0)
        else:
            self.fuse_offsets = None
        # 供 Runner 判断"哪些融合规划可以真的执行"（None = 该出口不存在）
        self.fuse_out_ok = self.fuse_offsets is not None and self.fuse_offsets[0] is not None
        self.fuse_sum_ok = self.fuse_offsets is not None and self.fuse_offsets[1] is not None

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
            out_lut=None, chsum=None, timeout_s=None,
            c8_in=False, c8_out=False):
        """写参数 → 启动 → 等 done。返回 (返回码, 耗时秒)。

        说明：缓冲区内容要与 PL 的可见性一致——调用前自行 flush 输入，
        完成后自行 invalidate 输出（见 test_on_board.py 的用法）。

        c8_in / c8_out（2026-10-23）：输入/输出缓冲区是否为 C8 交错布局。
        两个位**互相独立**，由调用方**逐张量**判定（`runner.PYNQEngine.conv`
        从 arena 的布局表查出来传进来）。默认 False ⇒ relu_en = 0/1，
        与整合前**逐位一致**（C8_LAYOUT=0 的 bit 根本不看这两位）。
        布局公式：addr(c,y,x) = (c/8)*H*W*8 + y*W*8 + x*8 + (c%8)；
        输入/输出缓冲按 ceil(C/8)*H*W*8 分配（补零通道必须为 0）。

        out_lut / chsum 是融合出口的可选缓冲区（256 项 int8 表 / 每输出通道一个
        int32）。默认 None：不启用融合，不写这两个寄存器，对交付版 bit 的行为与
        旧版**逐位不变**。两个出口**独立**：
          · out_lut 需要 FUSE_OUT=1 的构建 + fuse_offsets[0] 非 None；
          · chsum   需要 FUSE_SUM=1 的构建 + fuse_offsets[1] 非 None。
        只有声明了的寄存器才会被写（None 的那一个绝不去碰），否则启用会直接抛
        异常，避免往未知地址写寄存器。

        chsum 的语义（RTL conv2d_engine.cpp，FUSE_SUM=1）：
            chsum[oc] += Σ_{y,x} out[oc,y,x]
        累加的是**写回的 int8 值**（若同时有 out_lut，是查表后的值），int32
        累加、**不覆盖**，所以调用方每次启动前必须自己清零。

        timeout_s=None 时按工作量估算（见 estimate_seconds）。**不要再用 5 秒**：
        cosim 实测每"单元"（一个输出像素 × 一个输出通道 × 一个输入通道）约 12~17 拍，
        640 画幅最贵的一层就要十几秒，5 秒会在第一层大卷积上误超时。
        """
        hout = (H - 1) // s + 1
        wout = (W - 1) // s + 1
        check_limits(H, W, cin, cout, k, s)
        if out_lut is not None and not self.fuse_out_ok:
            raise RuntimeError(
                "本构建没有 out_lut 的 AXI-Lite 寄存器（fuse_offsets 第一项为 None）："
                "FUSE_OUT=1 构建综合后把偏移显式传给 Conv2dEngine(fuse_offsets=(lut, chsum))")
        if chsum is not None and not self.fuse_sum_ok:
            raise RuntimeError(
                "本构建没有 chsum 的 AXI-Lite 寄存器（fuse_offsets 第二项为 None）："
                "FUSE_SUM=1 构建综合后把偏移显式传给 "
                "Conv2dEngine(fuse_offsets=(None, 0x40))")
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
        # 1b) 融合出口的基地址（仅本构建真有该寄存器时；None 写 0 =
        #     RTL 里 `if (out_lut != 0)` / `if (chsum != 0)` 不查表、不累加，
        #     等价旧行为）。未声明的那个寄存器**完全不写**——它可能是别人的地址。
        if self.fuse_offsets is not None:
            off_lut, off_sum = self.fuse_offsets
            if off_lut is not None:
                self.offs.write(off_lut, 0 if out_lut is None else _buf_addr(out_lut))
                self.offs.write(off_lut + 4, 0)
            if off_sum is not None:
                self.offs.write(off_sum, 0 if chsum is None else _buf_addr(chsum))
                self.offs.write(off_sum + 4, 0)
        # 2) 形状参数
        self.ctrl.write(R_H, H); self.ctrl.write(R_W, W)
        self.ctrl.write(R_HOUT, hout); self.ctrl.write(R_WOUT, wout)
        self.ctrl.write(R_CIN, cin); self.ctrl.write(R_COUT, cout)
        self.ctrl.write(R_KSIZE, k); self.ctrl.write(R_STRIDE, s)
        self.ctrl.write(R_DIL, d); self.ctrl.write(R_PAD, pad)
        self.ctrl.write(R_SHIFT, shift)
        # relu_en 现在承载三个独立位（bit0 relu / bit2 输入 C8 / bit3 输出 C8）；
        # C8 关时 c8_in=c8_out=False ⇒ 值就是 0/1，与整合前完全一致。
        self.ctrl.write(R_RELU, relu_flags(relu, c8_in, c8_out))
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
