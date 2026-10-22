"""PYNQ 驱动 + 主机侧定点参考：v2 层间算子加速器（interop_accel）。

一个模块两套实现，语义逐位一致：
  · `PPEngine` —— 真硬件（s_axilite 写寄存器 + m_axi 存取 DDR）；
  · `PPSim`    —— 同一套 ABI 的 numpy 定点实现，用于**本机离线**跑整网与对拍。

数值契约（与 `pl_v2/src/interop_accel.cpp` 完全一致）：
    k    : Q16 int32，ratio = k / 65536
    acc  : int32 = Σ src_i * k_i（4 源 × 128 × 65536 = 3.4e7，32 位够）
    out  : sat_int8( round_half_even(acc >> 16) )

## 寄存器映射（与 HLS 生成的控制块一致）

s_axilite（`bundle=ctrl`）：HLS 把 ap_start/ap_return 放在最前，自定义标量从 0x18 起。

| 偏移 | 名称 | 偏移 | 名称 |
|---|---|---|---|
| 0x00 | ap_start | 0x30 | W |
| 0x10 | ap_return | 0x38 | out_H |
| 0x18 | OP | 0x40 | out_W |
| 0x20 | C | 0x48 | DST_CH_OFF |
| 0x28 | H | 0x50 | NSRC |

m_axi 的 `offset=slave` 地址寄存器在另一个 AXI-Lite 口（步进 12 字节，首个 0x10）：
src0 0x10 / src1 0x1C / src2 0x28 / src3 0x34 / dst 0x40 / coef 0x4C

## 板上实测的地址发现方式

PYNQ 2.7 + Vivado 2025.2 导出的 .hwh 里，`ip_dict` **每个 HLS IP 只给一个地址条目**
（键就是 BD 的 cell 名：`engine` → 0x40010000、`pp` → 0x40030000），没有
"xxx/s_axi_ctrl" 这种子条目。所以这里**以 BD 的固定分配为准**，用 hwh 的值做校验。
"""
import numpy as np

# ── 算子码（与 interop_accel.h 一致）────────────────────────────────────────
OP_RESCALE = 1
OP_ADD = 2
OP_SUM3W = 3
OP_INTERP = 4
OP_REDUCE_MEAN = 5

PP_OK = 0
PP_ERR_OP = 2
PP_ERR_SHAPE = 3
PP_ERR_NULL = 4

# ── s_axilite（标量）─────────────────────────────────────────────────────────
AP_START = 0x00
AP_RETURN = 0x10
R_OP = 0x18
R_C = 0x20
R_H = 0x28
R_W = 0x30
R_OUT_H = 0x38
R_OUT_W = 0x40
R_DST_CH_OFF = 0x48
R_NSRC = 0x50

# ── m_axi base（offset=slave 口）──────────────────────────────────────────────
B_SRC0, B_SRC1, B_SRC2, B_SRC3 = 0x10, 0x1C, 0x28, 0x34
B_DST, B_COEF = 0x40, 0x4C

Q16 = 1 << 16


# ─────────────────────────────────────────────────────────────────────────────
# 定点工具（与内核逐位一致）
# ─────────────────────────────────────────────────────────────────────────────
def q16(ratio):
    """ratio -> Q16 系数（半偶舍入到整数）。"""
    return int(np.rint(float(ratio) * Q16))


def round_q16(acc):
    """int32/int64 累加值 -> int8（半偶舍入 + 饱和），与内核的整数写法等价。"""
    acc = np.asarray(acc, dtype=np.int64)
    q = acc >> 16
    r = acc & 0xFFFF
    q = q + ((r > 0x8000) | ((r == 0x8000) & ((q & 1) == 1)))
    return np.clip(q, -128, 127).astype(np.int8)


def _buf_addr(buf):
    a = getattr(buf, "device_address", None)
    if a is None:
        a = getattr(buf, "physical_address")
    return int(a)


# ─────────────────────────────────────────────────────────────────────────────
# 主机侧定点参考
# ─────────────────────────────────────────────────────────────────────────────
class PPSim:
    """同一套 ABI 的 numpy 实现，接口与 PPEngine 相同。"""

    def run(self, op, srcs, dst, coef, C, H, W, out_H=0, out_W=0,
            dst_ch_off=0, nsrc=None):
        if nsrc is None:
            nsrc = len(srcs)
        coef = np.asarray(coef, dtype=np.int32)
        HW = H * W

        if op == OP_RESCALE:
            k = coef[:C].astype(np.int64)
            acc = srcs[0].astype(np.int64).reshape(C, HW) * k[:, None]
            dst[dst_ch_off:dst_ch_off + C] = round_q16(acc).reshape(C, H, W)
            return PP_OK

        if op == OP_ADD:
            k0 = coef[0:2 * C:2].astype(np.int64)
            k1 = coef[1:2 * C:2].astype(np.int64)
            acc = (srcs[0].astype(np.int64).reshape(C, HW) * k0[:, None]
                   + srcs[1].astype(np.int64).reshape(C, HW) * k1[:, None])
            dst[:C] = round_q16(acc).reshape(C, H, W)
            return PP_OK

        if op == OP_SUM3W:
            acc = np.zeros((C, HW), dtype=np.int64)
            for j in range(nsrc):
                kj = coef[j:4 * C:4].astype(np.int64)
                acc += srcs[j].astype(np.int64).reshape(C, HW) * kj[:, None]
            dst[:C] = round_q16(acc).reshape(C, H, W)
            return PP_OK

        if op == OP_INTERP:
            try:
                from .ops_numpy import interp as _interp      # 仓库里是包
            except ImportError:
                from ops_numpy import interp as _interp       # 板上是扁平目录
            if out_H >= H and out_H % H == 0:
                y = _interp(srcs[0], out_H // H, 1)
            elif H % out_H == 0:
                y = _interp(srcs[0], 1, H // out_H)
            else:
                raise ValueError("INTERP 只支持 2 的幂比例（H=%d out_H=%d）"
                                 % (H, out_H))
            dst[:C] = y
            return PP_OK

        if op == OP_REDUCE_MEAN:
            s = np.asarray(srcs[0], dtype=np.int64).reshape(C, HW).sum(axis=1)
            dst.view(np.int64)[:C] = s
            return PP_OK

        return PP_ERR_OP


# ─────────────────────────────────────────────────────────────────────────────
# 真硬件驱动
# ─────────────────────────────────────────────────────────────────────────────
class PPEngine:
    """v2 层间算子加速器的 PYNQ 驱动。

        pp = PPEngine(ol)
        rc, sec = pp.run(OP_ADD, [a_buf, b_buf], out_buf, coef_buf, C, H, W, nsrc=2)
    """

    PP_CTRL_ADDR = 0x40020000      # s_axilite 标量（build_bd_engine.tcl 分配）
    PP_BASE_ADDR = 0x40030000      # m_axi 的 offset=slave 地址寄存器

    def __init__(self, overlay, ip_name="pp"):
        from pynq import MMIO          # 延迟导入：本机没 pynq 也能 import 本模块
        self._MMIO = MMIO
        ctrl, base, name = self._discover(overlay, ip_name)
        self.name = name
        self.ctrl = self._MMIO(ctrl, 0x10000)
        self.base = self._MMIO(base, 0x10000)

    @staticmethod
    def _discover(ov, ip_name):
        """发现地址：以 BD 固定分配为准，用 .hwh 的值做校验。

        实测（PYNQ 2.7 + Vivado 2025.2）：ip_dict 每个 HLS IP 只有一个地址条目，
        键是 BD 的 cell 名（`pp` → 0x40030000，即 offset=slave 那个口）。
        """
        ent = None
        for k, v in ov.ip_dict.items():
            if not isinstance(v, dict):
                continue
            if ip_name in k or ip_name in str(v.get("type", "")):
                ent = (k, v)
                break
        if ent is None:
            raise RuntimeError(
                "overlay 里找不到 %s IP（已知条目：%s）——检查 .bit/.hwh 是否配对、"
                "BD 是否含该 IP" % (ip_name, list(ov.ip_dict.keys())))
        name, v = ent
        base = v.get("phys_addr")
        if base is not None and int(base) != PPEngine.PP_BASE_ADDR:
            print("WARNING: hwh 给的 %s 地址是 %#x，驱动假设 %#x（以驱动为准）"
                  % (name, int(base), PPEngine.PP_BASE_ADDR))
        return PPEngine.PP_CTRL_ADDR, PPEngine.PP_BASE_ADDR, name

    def run(self, op, srcs, dst, coef, C, H, W, out_H=0, out_W=0,
            dst_ch_off=0, nsrc=None, timeout_s=30.0):
        import time
        if nsrc is None:
            nsrc = len(srcs)
        if len(srcs) > 4:
            raise ValueError("最多 4 个源")
        srcs = list(srcs) + [None] * (4 - len(srcs))

        # 1) 缓冲区基址（64 位；高半部分显式写 0，见 conv_engine.py 的同类注释）
        for off, b in ((B_SRC0, srcs[0]), (B_SRC1, srcs[1]),
                       (B_SRC2, srcs[2]), (B_SRC3, srcs[3]),
                       (B_DST, dst), (B_COEF, coef)):
            if b is None:
                continue
            self.base.write(off, _buf_addr(b))
            self.base.write(off + 4, 0)
        # 2) 标量参数
        self.ctrl.write(R_OP, op)
        self.ctrl.write(R_C, C)
        self.ctrl.write(R_H, H)
        self.ctrl.write(R_W, W)
        self.ctrl.write(R_OUT_H, out_H)
        self.ctrl.write(R_OUT_W, out_W)
        self.ctrl.write(R_DST_CH_OFF, dst_ch_off)
        self.ctrl.write(R_NSRC, nsrc)
        # 3) 启动 + 轮询
        t0 = time.time()
        self.ctrl.write(AP_START, 0x1)
        while not (self.ctrl.read(AP_START) & 0x2):        # bit1 = ap_done
            if time.time() - t0 > timeout_s:
                raise TimeoutError("interop_accel 超时（op=%d C=%d H=%d W=%d）"
                                   % (op, C, H, W))
        return self.ctrl.read(AP_RETURN), time.time() - t0


# ─────────────────────────────────────────────────────────────────────────────
# 系数表打包（PS 侧唯一易错点，单独抽出）
# ─────────────────────────────────────────────────────────────────────────────
def pack_rescale(coefs):
    """RESCALE：每通道一个 Q16 系数。"""
    return np.asarray([q16(c) for c in coefs], dtype=np.int32)


def pack_add(k0, k1):
    """ADD：交错排布 [k0[0], k1[0], k0[1], k1[1], ...]。"""
    a = np.asarray([q16(v) for v in k0], dtype=np.int32)
    b = np.asarray([q16(v) for v in k1], dtype=np.int32)
    out = np.empty(2 * len(a), dtype=np.int32)
    out[0::2] = a
    out[1::2] = b
    return out


def pack_sum3w(ks):
    """SUM3W：按 (通道 × 源) 排布 → [k0[c], k1[c], k2[c], k3[c]]。"""
    ks = [np.asarray([q16(v) for v in k], dtype=np.int32) for k in ks]
    c = len(ks[0])
    out = np.zeros(4 * c, dtype=np.int32)
    for j, k in enumerate(ks):
        out[j::4] = k
    return out
