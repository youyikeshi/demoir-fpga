#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bin_ref_numpy.py —— 二值网的 **PS 侧 numpy 参考实现 + 容器访问层**（无 pynq、无 torch）

这个文件承担三件事，三件事都必须在**本机**可跑（板子上也跑，但不碰 DMA）：

  1. **容器访问**：`Blob` 把 `deploy/bin_weights_new/{index.json,bin_weights.bin}`
     打开，按层切出原始字节 / 64-bit 字 / 6 组参数。驱动只需要 `base_* = blob_pa + off_*`。
  2. **RTL 语义的整数参考**：`conv_rtl()` 逐位复现 `bin_conv_core.v` 的
     XNOR+popcount / 补零掩码 / 三次算术右移 / sat8，**逐字节**（不是"近似"）。
     它是分级测试阶段 5/6 的 golden 来源，也是 `--dry-run` 整网的执行单元。
  3. **层间算子的 PS 回退**：`binarize_pack` / `rprelu_int8` / `sam_int8` /
     `bilinear_int8` / `pixel_shuffle` / `pixel_unshuffle`。
     `bin_sam` / `bin_interp` / `bin_rprelu` 三个 PL 单元**没有 PS 驱动、也没有
     单独的 IP 实例**（当前 BD 里只有一个 `bin_unit_0`，UNIT=0=conv），
     所以整网只能在 PS 上用这几个函数顶上；它们与对应 `.v` 逐条对齐。

⚠ 本文件**不许** import pynq：板子外的审计/自检也用它。

用法（本机）：
    python3 bin_ref_numpy.py audit  <pack_dir>          # 容器偏移/尺寸/约束自洽
    python3 bin_ref_numpy.py golden <golden_dir>        # 用 xsim 黄金向量验本参考实现
    python3 bin_ref_numpy.py slice  <pack_dir> <idx>    # 切一层出来看看
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading

import numpy as np

# ---------------------------------------------------------------- 常量
LANES = 64          # 一次 64-bit 读 = 64 个二值通道（一个"通道组"）
C8 = 8              # 输出/层间张量的 C8 交错：8 通道挤在 8 个连续字节里
WBUF_WORDS = 256    # bin_conv_core 的 wbuf：K = C_B*k*k ≤ 256
COUT_LIM = 192      # COUT_MAX
C_MAX = 4096        # bin_unit_axi_wrap 的 C_MAX
HW_MAX = 4096       # HW_MAX
ALPHA_SHIFT = 9     # 契约固定（wrapper 硬校验）
SLOPE_SHIFT = 8     # 契约固定

# 错误码（**以 RTL 为准**：bin_unit_axi_wrap.v §0 localparam [7:0] ERR_*）
#   注意 README §1.5 / §7 里把 ERR_NULL 与 ERR_SIZE 的编号写反了，RTL 才是权威。
ERR_NAME = {
    0: "OK",
    1: "ERR_NULL",    # 某必需基址为 0
    2: "ERR_SIZE",    # 维度 / 容量越界
    3: "ERR_SHIFT",   # 移位参数不是契约固定值
    4: "ERR_LAYOUT",  # C % 8 != 0
    5: "ERR_ALIGN",   # 基址非 8 字节对齐
    6: "ERR_UNIT",    # UNIT 参数不存在
}

# 8 字节 popcount 查询表（**不用 np.bitwise_count**：板端 numpy 可能 < 2.0）
POP8 = np.array([bin(i).count("1") for i in range(256)], np.int64)


# ---------------------------------------------------------------- 堆调优
# ★ 2026-10-31：阈值从 32 MB 抬到 256 MB。原因（**与线程无关的独立收益**）：
#   256² 整网有**两块 >32 MB 的单块分配**（实测扫描）：
#     · im2col 的 cols 37.75 MB（decoder_1 的 (64,3,3,128,128)）
#     · cat 的目标数组 35.65 MB（448+48+48 = 544 × 128×128 × 4B）
#   旧的 32 MB 阈值刚好把它们推到 mmap 上 ⇒ 每次 np.empty 都要 mmap+缺页+munmap。
#   主机实测同一份 35.65 MB 的 concat（同形状同数据）：
#     32 MB 阈值 **6.79 ms** → 256 MB 阈值 **2.66 ms**（**2.55×**）
#   仍然不改任何数值，只改 glibc 分配策略。
def tune_malloc(mmap_threshold=None, trim_threshold=None):
    """把 glibc 的 mmap 阈值抬上去（**这一步比任何算子优化都值钱**）。

    板端 armv7l 实测：一块 2.2 MB 的 `np.concatenate` 目标数组
      默认（>128 KB 走 mmap）：**11.95 ms**  ← 每次都要 mmap + 缺页 + munmap
      阈值抬高后（走 sbrk 堆、可复用）：**6.89 ms**   ← **1.73×，一行代码**
    收益对所有"分配 → 填 → 释放"的算子成立（cat / interp / convfp / sam / …），
    且**不改任何数值**。

    阈值（默认 256 MB / 512 MB，可用 `BIN_MALLOPT_MMAP_MB` / `BIN_MALLOPT_TRIM_MB`
    覆盖）：256² 整网有两块 >32 MB 的单块分配（37.75 MB im2col / 35.65 MB cat），
    32 MB 的旧阈值刚好把它们推到 mmap 上；抬到 256 MB 后走堆、页可复用。
    trim 更大一点，保证空闲块不被提前还给内核（否则又要缺页）。
    ⚠ 板子只有 1 GB：阈值抬高 ⇒ 峰值堆 = 峰值"同时存活"分配（不变）+ 碎片，
    且不再归还 OS。本网是"分配→填→释放"的固定形状循环，glibc 的 bin 复用得起来；
    若板端发现 RSS 异常增长，把 `BIN_MALLOPT_MMAP_MB` 调回 32 即可（纯运行期开关）。

    `BIN_MALLOPT=0` 或 `BIN_NUMPY_NO_MALLOPT=1` ⇒ 完全不动 glibc（= 旧行为）。
    返回 (mmap_ok, trim_ok)；非 glibc 平台返回 (False, False) 并静默跳过。
    """
    if os.environ.get("BIN_MALLOPT", "1") in ("0", "", "false", "False"):
        return (False, False)
    if os.environ.get("BIN_NUMPY_NO_MALLOPT"):
        return (False, False)
    try:
        if mmap_threshold is None:
            mmap_threshold = int(float(os.environ.get("BIN_MALLOPT_MMAP_MB", "256")) * (1 << 20))
        if trim_threshold is None:
            trim_threshold = int(float(os.environ.get("BIN_MALLOPT_TRIM_MB", "512")) * (1 << 20))
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        M_MMAP_THRESHOLD, M_TRIM_THRESHOLD = -3, -1
        r1 = bool(libc.mallopt(M_MMAP_THRESHOLD, int(mmap_threshold)))
        r2 = bool(libc.mallopt(M_TRIM_THRESHOLD, int(trim_threshold)))
        return (r1, r2)
    except Exception:
        return (False, False)


MALLOPT_OK = tune_malloc()



def popcount_u64(a):
    """uint64 数组 → int64 popcount（按字节查表，与位序无关）。"""
    a = np.ascontiguousarray(a)
    return POP8[a.view(np.uint8).reshape(a.shape + (8,))].sum(axis=-1)


def sat8(v):
    """sat_int8（RTL 的 sat8）：>127→127，<-128→-128。"""
    return np.clip(v, -128, 127).astype(np.int8)


def ceil64(n):
    return (n + LANES - 1) // LANES


def out_hw(H, W, s):
    """**RTL 的输出分辨率**（bin_conv_core.v：hout_c=((H-1)>>s_shift)+1）。

    ⚠ 只看 stride，**不看 pad / dilation**。踩过的坑：按
    (H+2p-d(k-1)-1)//s+1 算，d=3 的层会算错。本网 227 层都满足 pad==d(k=3)
    或 pad=0(k=1)，两套公式恰好同值（`audit()` 会逐个形状验证这一点），
    所以磁盘上 verify_bin_weights.py 的 out_geom() 暂时没出事 —— 但驱动必须
    用 RTL 这条。
    """
    s = int(s)
    if s <= 0:
        raise ValueError("stride 必须 > 0")
    return (int(H) - 1) // s + 1, (int(W) - 1) // s + 1


def _i16(buf, n, off):
    return np.frombuffer(buf, "<i2", count=int(n), offset=int(off)).astype(np.int64)


def _i32(buf, n, off):
    return np.frombuffer(buf, "<i4", count=int(n), offset=int(off)).astype(np.int64)


# ============================================================================
# 1. 容器
# ============================================================================
class Blob:
    """`bin_weights_new` 容器：index.json + bin_weights.bin（只读）。"""

    def __init__(self, pack_dir, load_bin=True):
        # 兼容直接传 bin_weights.bin / index.json 的写法（板上排查时手滑很常见）
        if os.path.isfile(pack_dir):
            pack_dir = os.path.dirname(os.path.abspath(pack_dir))
        self.dir = pack_dir
        with open(os.path.join(pack_dir, "index.json"), "r", encoding="utf-8") as f:
            self.idx = json.load(f)
        self.layers = self.idx["layers"]
        b = self.idx["blob"]
        self.path = os.path.join(pack_dir, b["file"])
        self.size = int(b["size"])
        self.sha256 = b.get("sha256")
        self.buf = np.fromfile(self.path, np.uint8) if load_bin else None
        if self.buf is not None and self.buf.size != self.size:
            raise ValueError("blob 大小不符：磁盘 %d != index %d"
                             % (self.buf.size, self.size))

    # ---- 头部 ----
    def header(self):
        raw = self.buf[:64].tobytes()
        return dict(magic=raw[:8].rstrip(b"\x00").decode("ascii", "replace"),
                    version=int(np.frombuffer(raw[8:12], "<u4")[0]),
                    n_layers=int(np.frombuffer(raw[12:16], "<u4")[0]))

    def sha256_actual(self):
        h = hashlib.sha256()
        with open(self.path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    # ---- 逐层 ----
    def layer(self, i):
        return self.layers[int(i)]

    def layer_by_name(self, name):
        for L in self.layers:
            if L["name"] == name:
                return L
        raise KeyError(name)

    def raw(self, off, n):
        """容器里 [off, off+n) 的原始字节（驱动搬运用）。"""
        off, n = int(off), int(n)
        if off < 0 or n < 0 or off + n > self.size:
            raise ValueError("越界切片 [%#x,+%d) 超出 %d B 容器" % (off, n, self.size))
        return self.buf[off:off + n]

    def wgt_bytes(self, L):
        """该层权重区原始字节（cout*k²*cb 个 8 字节字）。"""
        return self.raw(L["off_wgt"], L["sz_wgt"])

    def wgt_words(self, L):
        """(cout, k*k, cb) uint64。字序 l = j*C_B + b（b 在内层）。"""
        cout, k, cb = int(L["cout"]), int(L["k"]), int(L["cb"])
        w = np.frombuffer(self.wgt_bytes(L).tobytes(), "<u8")
        if w.size != cout * k * k * cb:
            raise ValueError("%s: wgt 字数 %d != cout*k²*cb = %d"
                             % (L["name"], w.size, cout * k * k * cb))
        return w.reshape(cout, k * k, cb)

    def t_q(self, L):
        """**只取逐输入通道阈值**（int64）。`params()` 会顺带算 `wgt_words()`（整份
        权重解包，板端 ~ms 级）；打包热路径只要 t_q ⇒ 用这个，别用 params()。"""
        return _i16(self.buf, int(L["cin"]), L["off_thr"])

    def params(self, L):
        """该层 6 组整数参数 + 逐输入通道阈值（全部 int64）。"""
        b = self.buf
        cout, cin = int(L["cout"]), int(L["cin"])
        return dict(alpha_q=_i16(b, cout, L["off_alpha"]),
                    bias_q=_i32(b, cout, L["off_bias"]),
                    zeta_q=_i32(b, cout, L["off_zeta"]),
                    gamma_q=_i32(b, cout, L["off_gamma"]),
                    slope_q=_i16(b, cout, L["off_slope"]),
                    t_q=_i16(b, cin, L["off_thr"]),
                    words=self.wgt_words(L))

    def bases(self, i, blob_pa):
        """该层的 7 个 `base_*` 物理地址（**8 字节对齐、非 0** 由容器保证）。

        ⚠ `thr` 区域**没有**对应寄存器：`bin_conv_core` 不读它，它归
        PS 侧的二值化（`binarize_pack`）。RTL 里也不存在 `base_thr`。
        """
        L = self.layer(i)
        return dict(IN=None,                       # 运行时才定（见 bin_engine）
                    WGT=int(blob_pa) + int(L["off_wgt"]),
                    ALPHA=int(blob_pa) + int(L["off_alpha"]),
                    BIAS=int(blob_pa) + int(L["off_bias"]),
                    ZETA=int(blob_pa) + int(L["off_zeta"]),
                    GAMMA=int(blob_pa) + int(L["off_gamma"]),
                    SLOPE=int(blob_pa) + int(L["off_slope"]),
                    OUT=None)

    def fp_layer(self, name):
        """fp_layers 里的一个权重（float32 数组 + bias）。

        ⚠ **必须缓存**：原来每次调用都 `open(...).read()` 一次 SD 卡。整网一次前向会
        调 16 次（4 个 fp conv + 6 个 ECA × 2 个 conv 权重），板端实测光这一项就
        耗掉 ~0.5 s，比所有非卷积算子加起来还多。缓存后 → 0。
        权重是只读的，返回的数组被调用方共享（**不要原地改**）。
        """
        c = getattr(self, "_fpcache", None)
        if c is None:
            c = self._fpcache = {}
        hit = c.get(name)
        if hit is not None:
            return hit
        for f in self.idx["fp_layers"]:
            if f["name"] == name:
                with open(os.path.join(self.dir, f["file"]), "rb") as fh:
                    raw = fh.read()
                arr = np.frombuffer(raw, "<f4")
                shape = tuple(int(x) for x in
                              f["layout"].split("shape")[1].strip(" ()").split(","))
                v = (arr.reshape(shape).astype(np.float32), f.get("bias"))
                v[0].flags.writeable = False          # 防止调用方原地改坏缓存
                c[name] = v
                return v
        raise KeyError(name)


# ============================================================================
# 2. RTL 语义的整数前向（逐字节）
# ============================================================================
def binarize_pack(x_q, t_q, cin=None):
    """int8 激活 (cin,H,W) → 二值打包字 (cb,H,W) uint64。

    RTL 判据：`x_q + t_q > 0` ⇒ bit=1（**tie 记 0/-1**，不是 PyTorch 的 sign(0)=0）。
    word(b,y,x) 的第 i 位 = 通道 b*64+i；补零通道（c ≥ cin）恒 0。
    """
    x_q = np.asarray(x_q)
    cin = int(cin if cin is not None else x_q.shape[0])
    H, W = int(x_q.shape[1]), int(x_q.shape[2])
    cb = ceil64(cin)
    t = np.asarray(t_q, np.int16).reshape(cin, 1, 1)
    hw = H * W
    # 一次算完所有通道的 bit（int16 就够：x_q∈[-128,127]，t_q 是 int16）
    bits = (x_q.astype(np.int16) + t) > 0
    # 沿通道轴按 8 位打包（little）：byte j 的第 i 位 = 通道 8j+i
    bb = np.packbits(bits.reshape(cin, hw), axis=0, bitorder="little")
    bbp = np.zeros((cb * 8, hw), np.uint8)      # 补零通道恒 0
    bbp[: bb.shape[0]] = bb
    A = np.empty((cb, hw), np.uint64)
    for b in range(cb):
        # 每像素 8 字节 = 一个 64-bit 字（小端）
        A[b] = np.ascontiguousarray(bbp[b * 8:(b + 1) * 8].T).view(np.uint64).ravel()
    return A.reshape(cb, H, W)


def act_bytes(A):
    """(cb,H,W) uint64 → 8 字节/字的裸字节流（板端 `base_in` 要的那份）。"""
    return np.ascontiguousarray(A.astype("<u8")).tobytes()


def act_u8(A):
    """`act_bytes` 的**零拷贝**版：uint8 一维视图（同一块内存，逐字节相同）。

    ★ 2026-10-31：`act_bytes` = `astype`（一次整份拷贝）+ `tobytes`（第二次整份拷贝），
      而唯一的消费方 `BinConvEngine.push_input()` 只是
      `np.frombuffer(data, np.uint8)[:n] = src` —— 它接受任何 buffer 对象，
      包括 ndarray 视图。所以这里直接返回视图，**省掉两次满尺寸拷贝**。
      返回的是只读视图：调用方只读（push_input 只做 memcpy 的源）。
    """
    v = np.ascontiguousarray(A).view(np.uint8).reshape(-1)
    v.flags.writeable = False
    return v


def conv_rtl(cin, cout, k, s, d, pad, shift, A, P,
             alpha_shift=ALPHA_SHIFT, slope_shift=SLOPE_SHIFT):
    """**逐位**复现 `bin_conv_core.v`。返回 (cout,Hout,Wout) int8 + 未饱和前的值。

    地址/位序全部按 RTL：
        权重  addr = base_wgt + o*K*8 + l*8,  K = cb*k²,  l = j*cb + b
        激活  addr = base_in  + b*H*W*8 + iy*W*8 + ix*8,  第 i 位 = 通道 b*64+i
        越界  ⇒ 激活字用 0（**不是** XNOR 的 +1）
        补零  ⇒ mask_last 掩掉（补零位贡献 -1，而不是 XNOR 的 +1）
    """
    A = np.asarray(A, np.uint64)
    cb, H, W = A.shape
    cin, cout, k, s, d, pad = int(cin), int(cout), int(k), int(s), int(d), int(pad)
    words = P["words"]
    Ho, Wo = out_hw(H, W, s)

    n_pad = cin % LANES
    mask = np.full(cb, np.uint64(0xFFFFFFFFFFFFFFFF), np.uint64)
    if n_pad:
        mask[-1] = np.uint64((1 << n_pad) - 1)

    Y = np.arange(Ho, dtype=np.int64) * s
    X = np.arange(Wo, dtype=np.int64) * s
    acc = np.zeros((cout, Ho, Wo), np.int64)
    for ky in range(k):
        iy = Y + ky * d - pad
        vy = (iy >= 0) & (iy < H)
        iyc = np.clip(iy, 0, H - 1)
        for kx in range(k):
            ix = X + kx * d - pad
            vx = (ix >= 0) & (ix < W)
            ixc = np.clip(ix, 0, W - 1)
            Aw = A[:, iyc][:, :, ixc]                      # (cb,Ho,Wo)
            v = (vy[:, None] & vx[None, :])[None, :, :]
            Aw = np.where(v, Aw, np.uint64(0))             # 越界 ⇒ 字 0
            j = ky * k + kx
            Wj = words[:, j, :]                            # (cout,cb)
            xnor = ~(Aw[None, :, :, :] ^ Wj[:, :, None, None])
            xnor &= mask[None, :, None, None]              # 补零位从 popcount 里去掉
            acc += popcount_u64(xnor).sum(axis=1)

    dint = 2 * acc - int(cb) * LANES * k * k
    q = (dint * P["alpha_q"].reshape(-1, 1, 1)) >> alpha_shift        # 截断右移！
    q = q + P["bias_q"].reshape(-1, 1, 1)
    dd = q - P["zeta_q"].reshape(-1, 1, 1)
    r_pos = dd + P["gamma_q"].reshape(-1, 1, 1)
    r_neg = ((dd * P["slope_q"].reshape(-1, 1, 1)) >> slope_shift) + P["gamma_q"].reshape(-1, 1, 1)
    r = np.where(dd > 0, r_pos, r_neg)                                # 严格 > 0 走正支
    return sat8(r >> shift), r


def to_c8(x):
    """逻辑张量 (C,H,W) int8 → C8 交错裸字节（= 板上 `bin_conv_core` 的输出布局）。"""
    x = np.asarray(x, np.int8)
    C, H, W = x.shape
    cg = (C + C8 - 1) // C8
    out = np.zeros((cg, H, W, C8), np.int8)
    for c in range(C):
        out[c // C8, :, :, c % C8] = x[c]
    return np.ascontiguousarray(out).tobytes()


def from_c8(raw, C, H, W):
    """C8 交错裸字节 → 逻辑张量 (C,H,W) int8（只取前 C 个通道）。"""
    cg = (C + C8 - 1) // C8
    a = np.frombuffer(raw, np.int8, count=cg * H * W * C8).reshape(cg, H, W, C8)
    out = np.zeros((C, H, W), np.int8)
    for c in range(C):
        out[c] = a[c // C8, :, :, c % C8]
    return out


# ============================================================================
# 3. 层间算子的 PS 回退（与 bin_*.v 逐条对齐，int8 + C8）
# ============================================================================
def rprelu_int8(x, zeta_q, gamma_q, slope_q, shift, slope_shift=SLOPE_SHIFT):
    """bin_rprelu.v：dd=x-zeta; dd>0? dd+gamma : (dd*slope)>>slope_shift + gamma; sat8(>>shift)。

    x:(C,H,W) int8，*_q 逐通道。
    """
    x = np.asarray(x, np.int64)
    z = np.asarray(zeta_q, np.int64).reshape(-1, 1, 1)
    g = np.asarray(gamma_q, np.int64).reshape(-1, 1, 1)
    sl = np.asarray(slope_q, np.int64).reshape(-1, 1, 1)
    dd = x - z
    r = np.where(dd > 0, dd + g, ((dd * sl) >> slope_shift) + g)
    return sat8(r >> int(shift))


def sam_int8(x, y0, y2, y4, w0, w2, w4, shift):
    """bin_sam.v：acc=(x<<shift)+w0*y0+w2*y2+w4*y4; sat8(acc>>shift)。w 为 int16 逐通道。"""
    x = np.asarray(x, np.int64)
    acc = (x << int(shift))
    for w, y in ((w0, y0), (w2, y2), (w4, y4)):
        acc = acc + np.asarray(w, np.int64).reshape(-1, 1, 1) * np.asarray(y, np.int64)
    return sat8(acc >> int(shift))


def _axis_weights(in_len, out_len):
    """bilinear(align_corners=False) 的一维**精确有理数**权重。

    返回 (i0, i1, w0num, w1num, den)。fy = ((2*oy+1)*H - HO) / (2*HO)。
    """
    H, HO = int(in_len), int(out_len)
    den = 2 * HO
    num = (2 * np.arange(HO, dtype=np.int64) + 1) * H - HO
    y0 = np.floor_divide(num, den)                 # 向下取整（负数也对）
    tnum = num - y0 * den                          # ∈ [0, den)
    y0 = np.maximum(y0, 0)                         # 上采样首行：ty=0
    tnum = np.where(np.floor_divide(num, den) < 0, 0, tnum)
    y1 = np.minimum(y0 + 1, H - 1)                 # 先算 y1（照抄参考的顺序）
    y0 = np.minimum(y0, H - 1)                     # 再钳高位
    return y0, y1, den - tnum, tnum, den


def bilinear_int8(x, HO, WO):
    """bin_interp.v / F.interpolate(bilinear, align_corners=False) 的整数版。

    v = Σ kij*aij / D，out = sat8(round_half_even(v))。全程整数，无浮点。
    """
    x = np.asarray(x, np.int64)
    C, H, W = x.shape
    y0, y1, vy0, vy1, dy = _axis_weights(H, HO)
    x0, x1, vx0, vx1, dx = _axis_weights(W, WO)
    D = dy * dx
    out = np.zeros((C, HO, WO), np.int64)
    for (ty, tv) in ((y0, vy0), (y1, vy1)):
        for (tx, tv2) in ((x0, vx0), (x1, vx1)):
            kk = (tv[:, None] * tv2[None, :])                      # (HO,WO)
            out += kk[None, :, :] * x[:, ty][:, :, tx]
    q = out // D                                                   # 向 -inf
    rem = out - q * D                                              # [0,D)
    up = (2 * rem > D) | ((2 * rem == D) & (q & 1 == 1))           # 半偶
    q = q + up
    return sat8(q)


def pixel_unshuffle(x, r=2):
    """(C,H,W) → (C*r*r, H/r, W/r)，与 torch.nn.PixelUnshuffle 同序。"""
    C, H, W = x.shape
    x = np.asarray(x).reshape(C, H // r, r, W // r, r)
    return np.ascontiguousarray(x.transpose(0, 2, 4, 1, 3).reshape(C * r * r, H // r, W // r))


def pixel_shuffle(x, r=2):
    """(C*r*r, H, W) → (C, H*r, W*r)，与 torch.nn.PixelShuffle 同序。"""
    c, H, W = x.shape
    C = c // (r * r)
    x = np.asarray(x).reshape(C, r, r, H, W)
    return np.ascontiguousarray(x.transpose(0, 3, 1, 4, 2).reshape(C, H * r, W * r))


# ============================================================================
# 3.5 **整网 float32 前向的快速算子**（与原 bin_net.py 内联版逐位等价）
# ============================================================================
# 背景：板端（armv7l / numpy 1.21.5 / 参考 BLAS / 无 SIMD）实测
#   · `copy` 的有效带宽只有 ~190~400 MB/s，**内存流量就是时间**；
#   · 一次 numpy 调用开销 ~5~20 µs，**Python 层循环是毒药**；
#   · 每次 `np.empty` 大数组的缺页成本 ~40% 额外流量，**少分配 = 快**。
# 所以下面每个函数都只做三件事：少搬字节、少分配、把 Python 循环变成一次向量化。
# 每个都有 `--check` 对拍（`runs/psops/net2.py`），sha1 逐字节一致才算"等价"。

_AXIS_CACHE = {}


# ----------------------------------------------------------------------------
# 3.4 **非卷积算子的双线程切分**（零 RTL、零数值风险；与 conv2d_f32 的 gemm 切分同源）
# ----------------------------------------------------------------------------
# 切分只决定"哪几段由哪个线程算"，**每个输出元素的运算序列一个字节都没变**
# ⇒ 与单线程逐位相同（2026-10-31 在真实整网激活 / 真实调用顺序上逐字节 A/B：0 差异）。
#
# ⚠ 两条**必须先量再切**的经验（主机实测，详见 runs/psthreads/）：
#   ① `np.take` 在 C 层**持有 GIL**（2 线程 1.86 → 1.83 ms，0 加速），而 ufunc
#      （multiply/add）和大块 `np.copyto` **释放 GIL**（add 9.22 → 7.34 ms = 1.26×）。
#      ⇒ bilinear 的 4 次 gather 不能并行，只有乘加/写能并行 ⇒ 只有**上采样**
#      （HO*WO ≥ H*W，写是主体）切；**下采样**（gather 是主体）切了反而慢
#      （主机 0.26~0.92×），所以直接用单线程。
#   ② 线程 start+join 在主机上 ~42 µs、板上更贵 ⇒ 太小的张量不切（_PS_MIN_BYTES）。
#
# 关掉：`BIN_PS_THREADS=1`（也接受 `BIN_GEMM_THREADS=1` 作为全局总闸）。
_PS_THREADS_MAX = 2       # 板端是双核 A9；再多只会争同一对核
_PS_MIN_BYTES = 1 << 18   # 256 KB：低于此，线程开销 > 收益（本网 interp/sam 的最小调用 = 262 KB）


def ps_threads():
    """非卷积算子的并行度：默认 min(2, cpu_count)。

    覆盖顺序：`BIN_PS_THREADS` → `BIN_GEMM_THREADS` → 默认。
    `=1` ⇒ 单线程，逐字节等于 2026-10-31 之前的行为（可回退）。
    `cpu_count()==1` 自然落到单线程。
    """
    for env in ("BIN_PS_THREADS", "BIN_GEMM_THREADS"):
        raw = os.environ.get(env)
        if raw is None or raw == "":
            continue
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return max(1, min(_PS_THREADS_MAX, n))
    return max(1, min(_PS_THREADS_MAX, os.cpu_count() or 1))


def _split_edges(n, nth):
    """把 [0,n) 切成 nth 段（前 nth-1 段等长，余数给最后一段）。"""
    return [(n * i) // nth for i in range(nth + 1)]


def _run_split(works):
    """并发跑若干闭包；全部成功返回 True，任何失败返回 False（调用方串行重算）。

    线程里抛异常不会传回主线程 ⇒ 收集到 err 里。起线程失败（资源耗尽）也走同一条
    回落路径。**回落前一定 join 完所有已启动的线程**，否则"串行重算"会和残余线程
    同时写同一块 out（数据竞争 ⇒ 结果不确定）。
    """
    err = []

    def wrap(fn):
        try:
            fn()
        except BaseException as e:      # noqa: BLE001  —— 线程里不能抛
            err.append(e)

    ths = []
    try:
        for w in works:
            t = threading.Thread(target=wrap, args=(w,), daemon=True)
            t.start()
            ths.append(t)
    except BaseException as e:          # 起不来：已起的先收掉
        err.append(e)
    for t in ths:
        t.join()
    return not err


def _axis_w(in_len, out_len):
    """bilinear(align_corners=False) 的一维权重（float32），按形状缓存。

    公式与原 `bin_net.interpolate` **逐字相同**（含"ty 用未钳位的 floor(yy) 算"
    这个容易踩的细节），只是缓存起来，26 次插值只算 ~6 组。
    """
    key = (int(in_len), int(out_len))
    v = _AXIS_CACHE.get(key)
    if v is None:
        H, HO = int(in_len), int(out_len)
        f = (np.arange(HO, dtype=np.float64) + 0.5) * (H / HO) - 0.5
        i0 = np.floor(f).astype(np.int64)
        t = f - i0                       # ← 用**未钳位**的 i0 算 ty（照抄原实现）
        i0 = np.maximum(i0, 0)
        i1 = np.minimum(i0 + 1, H - 1)
        # armv7l 上 np.intp == int32；np.take 不接受 int64 索引（safe cast 会报错）
        v = (i0.astype(np.intp), i1.astype(np.intp), t.astype(np.float32))
        _AXIS_CACHE[key] = v
    return v


_IDX4_CACHE = {}


def _idx4(H, HO, W, WO):
    """bilinear 的 4 个 flat 索引 + 4 个权重（按形状缓存）。

    flat 索引 = `y*W + x`，于是 `np.take(x.reshape(C,H*W), idx, axis=1)` 一步
    完成纵向+横向两次 gather。数值与"先取行再取列"完全相同（同一个元素）。
    """
    key = (int(H), int(HO), int(W), int(WO))
    v = _IDX4_CACHE.get(key)
    if v is None:
        y0, y1, ty = _axis_w(H, HO)
        x0, x1, tx = _axis_w(W, WO)
        ty = ty[:, None]
        tx = tx[None, :]
        v = ((y0[:, None] * W + x0[None, :]).ravel(),
             (y0[:, None] * W + x1[None, :]).ravel(),
             (y1[:, None] * W + x0[None, :]).ravel(),
             (y1[:, None] * W + x1[None, :]).ravel(),
             ((1 - ty) * (1 - tx)).ravel(), ((1 - ty) * tx).ravel(),
             (ty * (1 - tx)).ravel(), (ty * tx).ravel())
        _IDX4_CACHE[key] = v
    return v


def _bilinear_slab(x, c0, c1, i00, i01, i10, i11, w00, w01, w10, w11, out):
    """只算 `out[c0:c1]`；与整块算法**逐位相同**（同一组 gather/乘加，只是范围不同）。

    `out` 是 (C, HO*WO) C 连续数组 ⇒ `out[c0:c1]` 是**连续视图**，直接 `out=` 写入，
    零拷贝、零额外流量。（这也是选"沿通道切"而不是"沿输出行切"的原因：切行会让
    每个线程写到跨步视图，或者必须先算再拷一遍全量。）
    """
    C, H, W = x.shape
    xf = x.reshape(C, H * W)[c0:c1]
    o = out[c0:c1]
    t = np.empty_like(o)
    g = np.empty_like(o)
    np.take(xf, i00, axis=1, out=g)
    np.multiply(g, w00, out=o)
    np.take(xf, i01, axis=1, out=g)
    np.multiply(g, w01, out=t)
    o += t
    np.take(xf, i10, axis=1, out=g)
    np.multiply(g, w10, out=t)
    o += t
    np.take(xf, i11, axis=1, out=g)
    np.multiply(g, w11, out=t)
    o += t


# ★ 2026-10-31（合入，2026-11-01 板端复验通过）：C 内核 `bilinear_c.c`，默认开（`BIN_BILINEAR_C=0` 关）。
#   为什么：板端 v39 256² 的 26 次 interp = **1.907 s**（非卷积最大单项、全网 22.4%），
#   有效吞吐只有 30 MB/s —— 瓶颈是 4 次 fancy-index gather 在 2 核 A9 上的表现。
#   同进程板端实测：**1.994 → 0.412 s（4.83×）**，整网 13.099 → 11.236 s（−1.86 s），
#   sha1 三件套逐位相同；整网里 26 次 interp 的**真实激活**上逐字节 0 差异（in-situ）。
#   等价性：4 乘 3 加各自落 float、顺序与 `_bilinear_slab` 逐项一致，编译带
#   `-ffp-contract=off`（禁 VMLA/FMA）。坐标/权重表仍由本文件 `_idx4()` 算 ⇒ 零风险。
#   任何一环失败（cc 编不出 / 版本不符 / 自检不过）都**静默回落** numpy 路径。
_C_KERNEL = {"tried": False, "fn": None}


def _bilinear_c_kernel():
    """惰性加载 C 内核（只尝试一次）；不可用 ⇒ None（调用方走 numpy）。"""
    if not _C_KERNEL["tried"]:
        _C_KERNEL["tried"] = True
        try:
            import bilinear_c as _bc
            if _bc.load() is not None:
                _C_KERNEL["fn"] = _bc.bilinear_f32_c
        except Exception:                    # noqa: BLE001  任何异常都只是回落
            _C_KERNEL["fn"] = None
    return _C_KERNEL["fn"]


def bilinear_f32(x, size=None, scale=None):
    """`bin_net.interpolate` 的逐位等价快速版（bilinear, align_corners=False）。

    与原实现的**运算顺序完全一致**（sha1 逐字节相同，板上已对拍），差别只有：
      ① 纵向取行 y0/y1 各只做一次（原来 y0、y1 各被取了两次，共 4 次 gather）；
      ② 把"纵向取行 + 横向取列"的**两次 gather 合并成一次一维 flat gather**
         （索引 `y*W + x` 按形状预计算并缓存）—— 少读一整个 (C,HO,W)；
      ③ 4 个 gather 用 `np.take(..., out=)` 写进复用缓冲 —— **零分配**；
      ④ 4 个乘积写进同一个输出缓冲 + 1 个临时缓冲（原来 4 个满尺寸临时数组）。
    ★ 分辨率必须写清（2026-10-31 复核）：这句「**222.2 → 156.0 ms**」是
      **hw=64** 的 26 次插值（`runs/psops/micro.py` best-of-3，形状表是 64² 的
      `(48,32,32)…`），**不是 256²**。256² 同批 26 次的实测是
      **1.907 s**（v39，`runs/board_v39/`：26 名字 × 7~8 次重复求和）。
      两者相差 ≈16×（= 面积比），不是"同一批 workload 的 12× 矛盾"。

    ★ 2026-10-31：再加**沿通道 C 切 2 线程**（`BIN_PS_THREADS`，默认 2）。
      切分不改任何元素的运算顺序 ⇒ 逐位相同；只在**上采样**（HO*WO ≥ H*W）时切，
      因为下采样的主体是 `np.take`，而它**不释放 GIL**（实测 0 加速，见 3.4 注释）。
      主机真实激活实测：26 次插值 46.9~51.5 → 35.3~40.0 ms（**1.29~1.42×**，多次运行），
      逐字节 0 差异。
    ★ 2026-10-31（合入）：默认走 C 内核（`bilinear_c.c`，逐字节等价）；
      numpy 版保留为回落路径（`BIN_BILINEAR_C=0` 或 C 不可用时）。
    """
    _c = _bilinear_c_kernel()
    if _c is not None:
        return _c(x, size=size, scale=scale)
    return _bilinear_f32_numpy(x, size=size, scale=scale)


def _bilinear_f32_numpy(x, size=None, scale=None):
    """**numpy 参考实现**（= C 内核合入前的线上路径）。

    C 内核的逐字节自检与回落都调这个函数（不是 `bilinear_f32`，否则会递归）。
    """
    x = np.asarray(x, np.float32)
    if not x.flags.c_contiguous:            # flat 索引要求 C 连续
        x = np.ascontiguousarray(x)
    C, H, W = x.shape
    if size is None:
        size = (max(1, int(round(H * scale))), max(1, int(round(W * scale))))
    HO, WO = int(size[0]), int(size[1])
    i00, i01, i10, i11, w00, w01, w10, w11 = _idx4(H, HO, W, WO)
    n = HO * WO
    out = np.empty((C, n), np.float32)
    nth = ps_threads()
    if (nth <= 1 or C < 2 or n < H * W          # 下采样 ⇒ gather 主导，不切
            or out.nbytes < _PS_MIN_BYTES):
        _bilinear_slab(x, 0, C, i00, i01, i10, i11, w00, w01, w10, w11, out)
        return out.reshape(C, HO, WO)
    edges = _split_edges(C, nth)
    works = [(lambda a, b: lambda: _bilinear_slab(
        x, a, b, i00, i01, i10, i11, w00, w01, w10, w11, out))(edges[i], edges[i + 1])
        for i in range(nth) if edges[i + 1] > edges[i]]
    if not _run_split(works):                   # 任何异常 ⇒ 静默回落，整块重算
        _bilinear_slab(x, 0, C, i00, i01, i10, i11, w00, w01, w10, w11, out)
    return out.reshape(C, HO, WO)


def _windowed_dot(v, w, n):
    """`[float(np.dot(w, v[i:i+len(w)])) for i in range(n)]` 的向量化版。

    v 已被 pad 过；返回 float32 (n,)。用 as_strided 造 (n,len(w)) 的滑窗视图，
    一次 gemv 代替 n 次 np.dot（每次 ~19 µs 的调用开销是 ECA 慢的全部原因）。
    """
    w = np.ascontiguousarray(w, np.float32).reshape(-1)
    k = w.size
    M = np.lib.stride_tricks.as_strided(
        v, shape=(n, k), strides=(v.strides[0], v.strides[0]), writeable=False)
    return (M @ w).astype(np.float32, copy=False)


def _windowed_dot_exact(v, w, n):
    """`[float(np.dot(w, v[i:i+len(w)])) for i in range(n)]` 的**逐位等价**向量化版。

    与上面的 `_windowed_dot`（gemv）不同，这里**不做 BLAS 调用**，而是显式按
    k=0..K-1 的**串行顺序**累加 float32：

        acc = w[0]*x[0]
        acc = acc + w[1]*x[1]      # 每一步都是独立、正确舍入的 float32 ufunc
        ...

    这正是 BLAS `sdot` 的定义式累加顺序（也不做 FMA 融合），所以与 `np.dot`
    **逐位相同（0 ULP）** —— 前提是底层 BLAS 的 sdot 对 K=9 这种小长度没有改成
    多累加器/向量化重排。该前提**必须实测**（`tools/test_eca_batch_ab.py`：本网
    6 个真实 ECA 权重 + 真实量级激活、1584 个通道逐位比对；另有大样本
    sdot≡串行 的独立验证）。

    一次滑窗视图 + 2K-1 = 17 次 ufunc，取代 C(=576) 次 `np.dot` 的 ~19 µs 调用开销。
    """
    w = np.ascontiguousarray(w, np.float32).reshape(-1)
    k = w.size
    M = np.lib.stride_tricks.sliding_window_view(v, k)      # (n,k) 只读视图，零拷贝
    acc = np.empty(n, np.float32)
    tmp = np.empty(n, np.float32)
    np.multiply(M[:, 0], w[0], out=acc)
    for j in range(1, k):
        np.multiply(M[:, j], w[j], out=tmp)
        np.add(acc, tmp, out=acc)
    return acc


def eca_attn_f32(x, w1, w2):
    """ECA 通道注意力：C 通道 → C 个 sigmoid 权重（float32）。**逐位等价 + 快**。

    原实现是两段 `[float(np.dot(...)) for i in range(C)]` Python 循环，
    C 最大 576 ⇒ 每层 2C=1152 次 `np.dot` ⇒ 板端 22 ms/次，全是调用开销。

    ★ 2026-10-31：改用 `_windowed_dot_exact`（串行累加，与 sdot 同序）⇒ **0 ULP**，
      同时把 1152 次 BLAS 调用压成每层 34 次 ufunc。**默认可用**
      （`bin_net.FAST_ECA=True` 不再破坏冻结基线 sha1）。

      历史：更早的 `_windowed_dot`（`as_strided(...) @ w` 一次 gemv）快但**不逐位等价**
      ——1584 个通道里 614 个不同、最大 16 ULP（随机数据测不出来，必须用真实激活）。
      那个版本保留在上方只作对照，不要打开。
    """
    C = int(x.shape[0])
    m = np.asarray(x, np.float32).mean(axis=(1, 2)).reshape(-1)
    v = np.pad(m, (4, 4))
    y = _windowed_dot_exact(v, w2, C)
    y = np.maximum(y, 0)
    v2 = np.pad(y, (4, 4))
    z = _windowed_dot_exact(v2, w1, C)
    return (1.0 / (1.0 + np.exp(-z))).astype(np.float32)


def _sam_slab(x, y0, y2, y4, w0, w2, w4, c0, c1, out):
    """只算通道 [c0,c1)；与整块算法**逐位相同**（每通道独立、累加顺序不变）。

    `out` 是 (C,H,W) C 连续 ⇒ `out[c0:c1]` 连续视图，零拷贝写。
    """
    o = out[c0:c1]
    t = np.empty_like(o)
    np.multiply(y0[c0:c1], np.reshape(w0[c0:c1], (-1, 1, 1)), out=o)
    np.multiply(y2[c0:c1], np.reshape(w2[c0:c1], (-1, 1, 1)), out=t)
    o += t
    np.multiply(y4[c0:c1], np.reshape(w4[c0:c1], (-1, 1, 1)), out=t)
    o += t
    o += x[c0:c1]


def sam_mix_f32(x, y0, y2, y4, w0, w2, w4):
    """SAM 三分支加权 `x + (w0*y0 + w2*y2 + w4*y4)`，逐位等价（加法可交换）。

    原式 5 个满尺寸临时数组 → 这里 1 个输出 + 1 个复用临时。

    ★ 2026-10-31：**沿输出通道切 2 线程**（`BIN_PS_THREADS`，默认 2）。
      四个源/目的都是按通道独立的全 ufunc（release GIL）⇒ 主机真实激活实测
      6.25~6.54 → 4.83~5.32 ms（**1.23~1.40×**，多次运行），逐字节 0 差异。
    """
    if (y0.dtype != np.float32 or x.dtype != np.float32):   # 本网不会发生：保语义
        out = np.multiply(y0, np.reshape(w0, (-1, 1, 1)), dtype=np.float32)
        tmp = np.empty_like(out)
        np.multiply(y2, np.reshape(w2, (-1, 1, 1)), out=tmp)
        out += tmp
        np.multiply(y4, np.reshape(w4, (-1, 1, 1)), out=tmp)
        out += tmp
        out += x
        return out
    C = int(y0.shape[0])
    out = np.empty(y0.shape, np.float32)
    nth = ps_threads()
    if nth <= 1 or C < 2 or out.nbytes < _PS_MIN_BYTES:
        _sam_slab(x, y0, y2, y4, w0, w2, w4, 0, C, out)
        return out
    edges = _split_edges(C, nth)
    works = [(lambda a, b: lambda: _sam_slab(
        x, y0, y2, y4, w0, w2, w4, a, b, out))(edges[i], edges[i + 1])
        for i in range(nth) if edges[i + 1] > edges[i]]
    if not _run_split(works):                   # 任何异常 ⇒ 静默回落，整块重算
        _sam_slab(x, y0, y2, y4, w0, w2, w4, 0, C, out)
    return out


def add_f32(a, b):
    """残差 `a + b`；`b` 是 float32 时走 out= 复用，省一次分配。"""
    return np.add(a, b)


# ---------------------------------------------------------------- T-17 开关
# 四个都是 **opt-in（默认关）**；全关时 `conv2d_f32` 与改动前**逐字节相同**。
# 口径：见 runs/t17_convfp/REPORT_attr*.md；A/B 各自独立开关，可任意组合。
def _env_on(name):
    return os.environ.get(name, "").strip().lower() in ("1", "on", "true", "yes")


CONVPF_NOPAD = _env_on("BIN_CONVPF_NOPAD")     # 改动 1：np.pad 不物化
CONVPF_FUSE = _env_on("BIN_CONVPF_FUSE")       # 改动 2：bias/relu 就地
GEMM_POOL = _env_on("BIN_GEMM_POOL")           # 改动 3：gemm 线程池
CONVPF_C = _env_on("BIN_CONVPF_C")             # 改动 4：直接卷积 C 内核

_cpf_why = None          # C 路径回落原因（只打印一次，不污染正常输出）


def _convpf_c_try(x, w, b, cout, cin, k, H, W, Ho, Wo, pad, dil):
    """调 C 内核；**任何异常/自检不过/形状不支持 ⇒ 返回 None 并记原因**（静默回落）。"""
    global _cpf_why
    if b is None or dil != 1:
        _cpf_note("不支持 b=None 或 dil!=1")
        return None
    try:
        import convpf_c
        y = convpf_c.conv_direct(x, w, np.asarray(b, np.float32), cout, cin, k,
                                 H, W, Ho, Wo, pad)
        if y is None:
            _cpf_note(convpf_c.state().get("why") or "conv_direct 返回 None")
        return y
    except Exception as _ex:                      # noqa: BLE001
        _cpf_note("C 内核异常：%r" % (_ex,))
        return None


def _cpf_note(why):
    """回落原因**只打印一次**（不污染正常输出，也不在 227 层里刷屏）。"""
    global _cpf_why
    if _cpf_why != why:
        _cpf_why = why
        # 有的 why 自带 `[TAG]` 前缀（如 `[CONVPF] 非 ARM 平台…`）⇒ 不再叠一层
        print("  %s%s ⇒ 回落 numpy 路径（结果不变）"
              % ("" if why.startswith("[") else "[CONVPF_C] ", why), flush=True)


def convpf_c_status():
    """给报告/门禁用：(是否启用, 是否可用, 原因)。"""
    try:
        import convpf_c
        return convpf_c.state()
    except Exception as _ex:                      # noqa: BLE001
        return dict(on=bool(CONVPF_C), ok=False, why="import convpf_c 失败：%r" % (_ex,))


def conv2d_f32(x, w, b=None, stride=1, pad=0, dil=1, row_tile=0):
    """float32 卷积（NCHW 单图），im2col + 一次 gemm。与 `bin_net.conv2d_f32` 等价。

    优化点（相对 bin_net 内联版，运算顺序完全不变）：
      ① im2col 用 **slice 步进**代替 `x[:, ys][:, :, xs]` 的两次 fancy-gather
         —— 省掉一半取数流量和一个 (cin,Ho,Wp) 中间数组；
      ② `row_tile>0` 时按输出行分块（把 cols 压在 L2 里）。
         ⚠ **板上实测分块更慢**（conv_first t0=48.6 / t8=79.6 / t16=49.8 ms），
         因为参考 BLAS 的 gemm 把 K 也分块，cols 本来就不是主要瓶颈，多调用
         反而摊薄了 gemm 的效率。**所以默认 row_tile=0（不分块）**。
      板端 4 个真实层实测：**64.8+7.2+22.6+78.4 = 173 → 48.6+4.0+13.3+38.6 = 104.5 ms（1.66×）**。

    T-17 的四个 opt-in 加速（**默认全关；全关时与旧路径逐字节相同**）
      · `BIN_CONVPF_NOPAD=1`  不物化 `np.pad`，边界 tap 直接把 +0.0 写进 cols
      · `BIN_CONVPF_FUSE=1`   `bias` 用 `out=` 就地加（relu 的在 `bin_net._fp_conv`）
      · `BIN_GEMM_POOL=1`     线程池代替每次新建 Thread（见 `gemm_split_m`）
      · `BIN_CONVPF_C=1`      **im2col-free 直接卷积 C 内核**（见 `convpf_c.py`）
                              ⚠ **非 ARM 平台默认不启用**（本内核是 NEON 内核；
                              x86 上它的严格串行累加序必然与主机 BLAS 差末位 ⇒
                              sha1 必变，属设计使然。`BIN_CONVPF_C_FORCE=1` 强制，
                              仅供门禁测试；判据是"内核 vs 串行参考 0 差"）
    """
    x = np.asarray(x, np.float32)
    w = np.asarray(w, np.float32)
    cout, cin, k, _ = w.shape
    H, W = x.shape[1], x.shape[2]
    # NOPAD / C 内核只在 stride=1 & dil=1 时走（本网 4 层全部如此）；其他情况保持原路径。
    simple = bool((CONVPF_NOPAD or CONVPF_C) and stride == 1 and dil == 1)
    nopad = bool(simple and pad > 0)
    if pad and not nopad:
        x = np.pad(x, ((0, 0), (pad, pad), (pad, pad)), mode="constant")
    Hp, Wp = (H + 2 * pad, W + 2 * pad) if nopad else (x.shape[1], x.shape[2])
    Ho = (Hp - dil * (k - 1) - 1) // stride + 1
    Wo = (Wp - dil * (k - 1) - 1) // stride + 1
    w2 = w.reshape(cout, cin * k * k)
    ys = [slice(ky * dil, ky * dil + stride * (Ho - 1) + 1, stride) for ky in range(k)]
    xs = [slice(kx * dil, kx * dil + stride * (Wo - 1) + 1, stride) for kx in range(k)]

    def _cols(r0, r1):
        c = np.empty((cin, k, k, r1 - r0, Wo), np.float32)
        if not nopad:
            for ky in range(k):
                for kx in range(k):
                    c[:, ky, kx] = x[:, ys[ky], xs[kx]][:, r0:r1]
            return c.reshape(cin * k * k, (r1 - r0) * Wo)
        # ★ NOPAD：直接从**未填充**的 x 取内部，越界部分写 +0.0。
        #   数值与 np.pad 逐字节相同（constant 模式填的就是 +0.0），省掉
        #   一次 (cin,Hp,Wp) 的写 + 一次 (cin,H,W) 的读。
        for ky in range(k):
            h0 = ky - pad
            rlo = max(r0, -h0, 0)
            rhi = min(r1, H - h0, Ho)
            for kx in range(k):
                w0 = kx - pad
                pl = c[:, ky, kx]                      # (cin, r1-r0, Wo)
                clo = max(0, -w0)
                chi = min(Wo, W - w0)
                if rhi <= rlo or chi <= clo:
                    pl[...] = 0.0
                    continue
                pl[:, rlo - r0:rhi - r0, clo:chi] = \
                    x[:, h0 + rlo:h0 + rhi, w0 + clo:w0 + chi]
                if rlo > r0:
                    pl[:, :rlo - r0, :] = 0.0
                if rhi < r1:
                    pl[:, rhi - r0:, :] = 0.0
                if clo > 0:
                    pl[:, rlo - r0:rhi - r0, :clo] = 0.0
                if chi < Wo:
                    pl[:, rlo - r0:rhi - r0, chi:] = 0.0
        return c.reshape(cin * k * k, (r1 - r0) * Wo)

    y = None
    if row_tile and Ho > row_tile:
        y = np.empty((cout, Ho * Wo), np.float32)
        for r0 in range(0, Ho, row_tile):
            r1 = min(r0 + row_tile, Ho)
            y[:, r0 * Wo:r1 * Wo] = w2 @ _cols(r0, r1)
    elif CONVPF_C and simple:
        # ★ T-17 主菜：im2col-free 直接卷积 C 内核（K 方向严格串行累加）。
        #   任何异常 ⇒ 记 `_cpf_why` 并**静默回落**下面的 numpy 路径（结果不变）。
        y = _convpf_c_try(x, w, b, cout, cin, k, H, W, Ho, Wo, pad, dil)
        if y is not None:
            return y.reshape(cout, Ho, Wo)            # 内核已含 bias
    if y is None:
        # ★ 2026-10-31：沿 M(=cout) 切两线程算 gemm —— **逐位相同**（见 gemm_split_m）。
        #   只改这一行；row_tile 那条路原样保留（板上实测它更慢，别删也别默认开）。
        #   关掉：BIN_GEMM_THREADS=1
        y = gemm_split_m(w2, _cols(0, Ho))
        if b is not None:
            bb = np.asarray(b, np.float32).reshape(-1, 1)
            if CONVPF_FUSE:
                y += bb        # 就地：与 `y = y + bb` 逐字节相同（同一 ufunc、同一操作数）
            else:
                y = y + bb
    return y.reshape(cout, Ho, Wo)


_GEMM_MIN_ROWS = 4      # 每线程至少这么多输出行（cout=12 的 decoder 层切 2×6 也要能开）
_GEMM_THREADS_MAX = 2   # 板端是双核 A9；再多只会争同一对核


def gemm_threads():
    """并行度：默认 min(2, cpu_count)，可用 `BIN_GEMM_THREADS` 覆盖（1 = 关掉）。"""
    try:
        n = int(os.environ.get("BIN_GEMM_THREADS", "0"))
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = min(_GEMM_THREADS_MAX, os.cpu_count() or 1)
    return max(1, n)


def gemm_split_m(a, b, out=None):
    """`a @ b`，沿 **输出行 M** 切成 n 段并行算（默认 n=2）。

    为什么这条是**零数值风险**的加速
    --------------------------------
    切分只决定"哪几行由哪个线程算"，**每个输出元素在 K 方向上的点积一点没变**
    —— 还是同一个 BLAS 内核、同样的累加顺序 ⇒ 与单线程 `a @ b` **逐位相同**。
    板端实测（A9 双核、numpy 链接通用 libblas）：
        (48,300,16384): 658.1 ms → 388.0 ms (1.70×)，`array_equal` = True，max ULP = 0
        conv_first 全程: 776.9 ms → 504.7 ms (1.54×)，同样逐位相同

    为什么不沿 N 切
    ---------------
    cols 是 `(cin*k*k, ho*wo)` C 连续；`b[:, a:b]` 是**非连续**切片，BLAS 会先抄一份
    （实测会直接 ValueError / 变慢）。而 `a[a:b]`、`out[a:b]` 是行切片 ⇒ 零拷贝。

    为什么**绝不能**沿 K 切
    -----------------------
    沿 K 分块累加会改变求和顺序 ⇒ 不再是逐位相同（历史上 `eca` 就是这么翻车的）

    失败/单核时**静默回落**单线程，结果不变。

    `BIN_GEMM_POOL=1`（T-17 改动 3，默认关）：把"每次调用新建 n 个 `Thread`"换成
    一个**常驻 `ThreadPoolExecutor`**。提交的仍是同一批
    `np.dot(a[r0:r1], b, out=out[r0:r1])`（同样的切片、同样的 BLAS 调用）
    ⇒ 逐字节相同，只是省掉 4 层 × 2 次线程创建/销毁（A9 上 ~0.3~0.5 ms/次）。
    池子在**首次成功**后常驻；`num` 变了就重建。
    """
    nth = gemm_threads()
    m = a.shape[0]
    if nth <= 1 or m < 2 * _GEMM_MIN_ROWS:
        return a @ b if out is None else np.dot(a, b, out=out)
    if out is None:
        out = np.empty((m, b.shape[1]), np.float32)
    edges = [(m * i) // nth for i in range(nth + 1)]

    if GEMM_POOL:
        try:
            ex = _gemm_pool(nth)
            futs = [ex.submit(_gemm_slice, a, b, out, edges[i], edges[i + 1])
                    for i in range(nth) if edges[i + 1] > edges[i]]
            bad = [f.result() for f in futs]
            if not any(bad):
                return out
        except Exception:                     # noqa: BLE001
            pass                              # 池子出问题 ⇒ 落到下面的老路径（结果不变）

    err = []
    ths = []

    for i in range(nth):
        r0, r1 = edges[i], edges[i + 1]
        if r1 <= r0:
            continue

        def work(r0=r0, r1=r1):
            try:
                np.dot(a[r0:r1], b, out=out[r0:r1])
            except BaseException as e:          # 线程里不能抛，收集后回落
                err.append(e)

        t = threading.Thread(target=work, daemon=True)
        ths.append(t)
        t.start()
    for t in ths:
        t.join()
    if err:                                      # 任何异常 ⇒ 静默回落（结果不变）
        return np.dot(a, b, out=out)
    return out


# ---- 常驻线程池（BIN_GEMM_POOL=1）----
_GEMM_EX = None
_GEMM_EX_N = 0
_GEMM_EX_LOCK = threading.Lock()


def _gemm_pool(n):
    """取/建常驻线程池。**不在持锁时做 dot**（只需保护创建）。"""
    global _GEMM_EX, _GEMM_EX_N
    if _GEMM_EX is not None and _GEMM_EX_N == n:
        return _GEMM_EX
    with _GEMM_EX_LOCK:
        if _GEMM_EX is None or _GEMM_EX_N != n:
            import concurrent.futures as _cf
            _GEMM_EX = _cf.ThreadPoolExecutor(max_workers=n,
                                              thread_name_prefix="gemm")
            _GEMM_EX_N = n
        return _GEMM_EX


def _gemm_slice(a, b, out, r0, r1):
    """池里的任务体：与老路径**同一个表达式**。返回 False=成功，True=出错。"""
    try:
        np.dot(a[r0:r1], b, out=out[r0:r1])
        return False
    except BaseException:                        # noqa: BLE001
        return True


# ----------------------------------------------------------------------------
# cat（沿"源段"切 2 线程）—— **默认关闭**，留给板端一键 A/B
# ----------------------------------------------------------------------------
# 为什么候选是对的：concat 的目标数组里，第 i 个 part 只落在 [c0_i, c1_i) 这个
# 连续区间；区间互不重叠、也没有任何跨段依赖 ⇒ 各线程各写各的，**逐字节相同**
# （不是"数值上等价"，是 memcpy 的字节完全一样）。
#
# 为什么默认关：主机 116 次真实 cat 实测 **0.67×**（变慢）。原因有两个，且都只属于主机：
#   ① 主机每次 cat 只 0.33 ms，而起 2 线程要 ~60 µs（空线程 42 µs）；
#      板端每次平均 21 ms（慢 65×）⇒ 开销占比从 ~18% 掉到 ~1%。
#   ② >32 MB 的目标数组在旧 mallopt 阈值下每次走 mmap（见 tune_malloc）；
#      已单独用 256 MB 阈值修掉，和线程正交。
# ⇒ 板端很可能 >1.2×，所以留 `BIN_CAT_THREADS=2` 给板端 A/B，默认行为不变。
_CAT_THREADS_MAX = 2


def cat_threads():
    """cat 并行度：**默认 1（关闭）**；`BIN_CAT_THREADS=2` 打开。

    `=1`/未设/非法 ⇒ 1（= 今天的 `np.concatenate`，逐字节相同）。
    """
    try:
        n = int(os.environ.get("BIN_CAT_THREADS", "1"))
    except (TypeError, ValueError):
        return 1
    return max(1, min(_CAT_THREADS_MAX, n)) if n > 0 else 1


def _cat_split(parts):
    """`np.concatenate(parts, 0)` 沿**源段**切 2 线程（逐字节相同）。

    非 float32 一律走 `np.concatenate`（把 dtype promote / safe-cast 规则原样留给它，
    不去复现那些边界语义）。
    """
    dt = np.result_type(*parts)
    if dt != np.float32:
        return np.concatenate(parts, 0)
    C = sum(int(p.shape[0]) for p in parts)
    out = np.empty((C,) + tuple(parts[0].shape[1:]), dt)
    edges = [0]
    for p in parts:
        edges.append(edges[-1] + int(p.shape[0]))
    jobs = [(parts[i], edges[i], edges[i + 1]) for i in range(len(parts))
            if edges[i + 1] > edges[i]]
    nth = cat_threads()
    if nth <= 1 or len(jobs) < 2 or out.nbytes < _PS_MIN_BYTES:
        for p, c0, c1 in jobs:
            np.copyto(out[c0:c1], p)
        return out
    # 把 jobs 均分成 min(nth, len(jobs)) 组（相邻组合并），一组一个线程
    ng = min(nth, len(jobs))
    groups = [[] for _ in range(ng)]
    for i, j in enumerate(jobs):
        groups[(i * ng) // len(jobs)].append(j)

    def mk(g):
        def work():
            for p, c0, c1 in g:
                np.copyto(out[c0:c1], p)
        return work

    if not _run_split([mk(g) for g in groups if g]):
        for p, c0, c1 in jobs:                       # 静默回落
            np.copyto(out[c0:c1], p)
    return out


def cat_last(parts):
    """`np.concatenate(parts, 0)` 的显式写法（语义完全相同）。

    - `BIN_CAT_THREADS=1`（默认）⇒ 就是 `np.concatenate`；
    - `=2` ⇒ 沿源段切 2 线程（逐位相同，见 `_cat_split`）。

    ⚠ `len(parts)==1` 仍返回**拷贝**（与 `np.concatenate` 一致）；旧版本这里返回
    `parts[0]` 本身，会产生别名（下游就地改写会污染源），已改掉。
    """
    if len(parts) == 1:
        return np.concatenate(parts, 0)
    if cat_threads() > 1:
        return _cat_split(parts)
    return np.concatenate(parts, 0)



# ============================================================================
# 3.7 把"一层的数据"摆成容器布局（给分级测试的 golden 层用）
# ============================================================================
def synth_layer_blob(name, meta, words, alpha, bias, zeta, gamma, slope):
    """把一层（或一个 xsim golden）拼成**和容器同样布局**的一段 blob。

    返回 (bytes, layer_dict)。layer_dict 的字段与 index.json 的层记录同义，
    所以可以直接 `BinLayer(layer_dict, H, W)` / `blob.bases()` 用。
    区域顺序 wgt→alpha→bias→zeta→gamma→slope，每区 8 字节对齐（RTL 的 ERR_ALIGN 要求）。
    """
    def _bytes(arr, dt, tag):
        """数组 → 裸字节（**必须是 view**：astype 会把 8 字节字截成 1 字节）。"""
        a = np.ascontiguousarray(arr)
        if a.dtype.itemsize > 1:
            a = a.astype(dt, copy=False)
        return a.view(np.uint8)               # 与 dt 同宽 ⇒ 纯字节视图（不复制）

    parts = [("wgt", _bytes(words, "<u8", "wgt")),
             ("alpha", _bytes(alpha, "<i2", "alpha")),
             ("bias", _bytes(bias, "<i4", "bias")),
             ("zeta", _bytes(zeta, "<i4", "zeta")),
             ("gamma", _bytes(gamma, "<i4", "gamma")),
             ("slope", _bytes(slope, "<i2", "slope"))]
    out = bytearray()
    off = {}
    for nm, arr in parts:
        pad = (-len(out)) % 8
        out += b"\x00" * pad
        off["off_" + nm] = len(out)
        off["sz_" + nm] = int(arr.size)
        out += arr.tobytes()
    out += b"\x00" * ((-len(out)) % 8)
    L = dict(name=str(name), cin=int(meta["cin"]), cout=int(meta["cout"]),
             k=int(meta["k"]), s=int(meta["s"]), d=int(meta["d"]), pad=int(meta["pad"]),
             shift=int(meta["shift"]), cb=int(meta["cb"]),
             K=int(meta["cb"]) * int(meta["k"]) ** 2,
             alpha_shift=int(meta.get("alpha_shift", ALPHA_SHIFT)),
             slope_shift=int(meta.get("slope_shift", SLOPE_SHIFT)),
             n_pad=int(meta["cb"]) * LANES - int(meta["cin"]),
             n_pad_total=(int(meta["cb"]) * LANES - int(meta["cin"])) * int(meta["k"]) ** 2)
    L.update(off)
    return bytes(out), L


def load_golden(gdir, name):
    """读一个 xsim golden 目录 → (bytes, layer_dict, act_bytes, expected_c8)。"""
    d = os.path.join(gdir, name)
    meta = json.load(open(os.path.join(d, "meta.json")))
    words = np.fromfile(os.path.join(d, "wgt_pack.bin"), "<u8")
    rd = lambda f, dt: np.fromfile(os.path.join(d, f), dt)
    blob, L = synth_layer_blob(name, meta, words,
                              rd("alpha.bin", "<i2"), rd("bias.bin", "<i4"),
                              rd("zeta.bin", "<i4"), rd("gamma.bin", "<i4"),
                              rd("slope.bin", "<i2"))
    act = rd("act_pack.bin", "<u8").tobytes()
    exp = rd("expected_c8.bin", np.int8)
    return blob, L, act, exp, meta


# ============================================================================
# 4. 容器审计（本机可验证的部分）
# ============================================================================
def audit(pack_dir, verbose=True):
    """检查 index.json / blob 的自洽性。返回 (problems, notes)。"""
    P, N = [], []

    def bad(msg):
        P.append(msg)

    def ok(msg):
        N.append(msg)

    bl = Blob(pack_dir)
    h = bl.header()
    if h["magic"] != bl.idx["blob"].get("magic", "BINWGT01"):
        bad("头 magic=%r 与 index 的 %r 不符" % (h["magic"], bl.idx["blob"].get("magic")))
    if h["n_layers"] != len(bl.layers):
        bad("头层数 %d != index 层数 %d" % (h["n_layers"], len(bl.layers)))
    ok("头：magic=%s version=%d layers=%d" % (h["magic"], h["version"], h["n_layers"]))

    if bl.size != bl.idx["blob"]["size"]:
        bad("blob.size %d != 磁盘/索引 %d" % (bl.size, bl.idx["blob"]["size"]))
    if bl.sha256:
        got = bl.sha256_actual()
        (ok if got == bl.sha256 else bad)(
            "sha256 %s" % (got if got == bl.sha256 else "%s != %s" % (got, bl.sha256)))

    FIELDS = (("wgt", "wgt", lambda L: L["cout"] * L["k"] * L["k"] * L["cb"] * 8),
              ("alpha", "alpha", lambda L: L["cout"] * 2),
              ("bias", "bias", lambda L: L["cout"] * 4),
              ("zeta", "zeta", lambda L: L["cout"] * 4),
              ("gamma", "gamma", lambda L: L["cout"] * 4),
              ("slope", "slope", lambda L: L["cout"] * 2),
              ("thr", "thr", lambda L: L["cin"] * 2))
    end_prev = 64
    n_shift_bad = 0
    for L in bl.layers:
        i, nm = L["idx"], L["name"]
        cin, cout, k, cb, K = int(L["cin"]), int(L["cout"]), int(L["k"]), int(L["cb"]), int(L["K"])
        if cb != ceil64(cin):
            bad("%s: cb=%d != ceil(cin/64)=%d" % (nm, cb, ceil64(cin)))
        if K != cb * k * k:
            bad("%s: K=%d != cb*k²=%d" % (nm, K, cb * k * k))
        if int(L["ntot"]) != cb * LANES * k * k:
            bad("%s: ntot=%d != cb*64*k²=%d" % (nm, L["ntot"], cb * LANES * k * k))
        if int(L["n_pad_total"]) != (cb * LANES - cin) * k * k:
            bad("%s: n_pad_total=%d != (cb*64-cin)*k²=%d"
                % (nm, L["n_pad_total"], (cb * LANES - cin) * k * k))
        if int(L["n_pad"]) != cb * LANES - cin:
            bad("%s: n_pad=%d != cb*64-cin=%d" % (nm, L["n_pad"], cb * LANES - cin))
        # ---- 硬件约束 ----
        if K > WBUF_WORDS:
            bad("%s: K=%d > wbuf %d（会 ERR_SIZE）" % (nm, K, WBUF_WORDS))
        if cout > COUT_LIM:
            bad("%s: cout=%d > %d（会 ERR_SIZE）" % (nm, cout, COUT_LIM))
        if cin > C_MAX:
            bad("%s: cin=%d > C_MAX %d" % (nm, cin, C_MAX))
        if cin % C8:
            bad("%s: cin=%d 非 8 的倍数（CHK_C8_ALL=1 ⇒ ERR_LAYOUT）" % (nm, cin))
        if int(L["alpha_shift"]) != ALPHA_SHIFT or int(L["slope_shift"]) != SLOPE_SHIFT:
            bad("%s: alpha_shift/slope_shift=%s/%s 不是 9/8（ERR_SHIFT）"
                % (nm, L["alpha_shift"], L["slope_shift"]))
        if int(L["shift"]) != int(L["q_frac_bits"]) - int(L["act_fs_out"]):
            n_shift_bad += 1
        if not (0 <= int(L["shift"]) <= 63):
            bad("%s: shift=%d 非法" % (nm, L["shift"]))
        # ---- 区域：8 对齐 / 非 0 / 公式尺寸 / 不重叠 ----
        for fld, nmf, sizef in FIELDS:
            off, sz = int(L["off_" + fld]), int(L["sz_" + fld])
            if off % 8:
                bad("%s: off_%s=%#x 非 8 字节对齐（ERR_ALIGN）" % (nm, fld, off))
            if off == 0:
                bad("%s: off_%s=0（ERR_NULL）" % (nm, fld))
            if sz != sizef(L):
                bad("%s: sz_%s=%d != 公式 %d" % (nm, fld, sz, sizef(L)))
            if off + sz > bl.size:
                bad("%s: %s 区越出 blob（%#x+%d > %d）" % (nm, fld, off, sz, bl.size))
        # 区域按 wgt→alpha→bias→zeta→gamma→slope→thr 顺序、且层间不重叠
        cur = int(L["off_wgt"])
        for fld, _n, sizef in FIELDS:
            if int(L["off_" + fld]) != cur:
                bad("%s: %s 区起始 %#x != 上一区结束 %#x（布局非紧凑）"
                    % (nm, fld, L["off_" + fld], cur))
            cur += sizef(L)
        if int(L["off_wgt"]) < end_prev:
            bad("%s: 与前一层区域重叠（off_wgt=%#x < %#x）"
                % (nm, L["off_wgt"], end_prev))
        end_prev = cur
        # ---- 输出分辨率：两套公式在本网必须同值 ----
        for H in (8, 15, 16, 32, 33, 64, 65, 128, 256):
            a = (H - 1) // int(L["s"]) + 1
            s_, d_, p_, k_ = int(L["s"]), int(L["d"]), int(L["pad"]), k
            b = (H + 2 * p_ - d_ * (k_ - 1) - 1) // s_ + 1
            if a != b:
                bad("%s: H=%d 时 RTL 分辨率 %d != 补边公式 %d（pad=%d d=%d s=%d）"
                    % (nm, H, a, b, p_, d_, s_))
        # ---- 权重字数与位用量 ----
        if int(L["wgt_words"]) != cout * k * k * cb:
            bad("%s: wgt_words=%d != cout*k²*cb" % (nm, L["wgt_words"]))
        used = cin * cout * k * k
        if int(L["wgt_bits_used"]) != used:
            bad("%s: wgt_bits_used=%d != cin*cout*k²=%d" % (nm, L["wgt_bits_used"], used))

    if bl.buf is not None and end_prev > bl.size:
        bad("层区域结束 %#x > blob %d" % (end_prev, bl.size))
    ok("逐层区域：7 区紧凑、8 字节对齐、非 0、按层不重叠（结束于 %#x / %d B）"
       % (end_prev, bl.size))

    # ---- 汇总字段 ----
    c = bl.idx.get("counts", {})
    if c.get("layers") != len(bl.layers):
        bad("counts.layers=%s != %d" % (c.get("layers"), len(bl.layers)))
    if c.get("wgt_words") != sum(int(L["wgt_words"]) for L in bl.layers):
        bad("counts.wgt_words=%s != Σ层字数 %d"
            % (c.get("wgt_words"), sum(int(L["wgt_words"]) for L in bl.layers)))
    if c.get("bin_weights") != sum(int(L["cin"]) * int(L["cout"]) * int(L["k"]) ** 2
                                   for L in bl.layers):
        bad("counts.bin_weights=%s != Σcin*cout*k²" % (c.get("bin_weights"),))
    ok("counts：layers=%s wgt_words=%s bin_weights=%s"
       % (c.get("layers"), c.get("wgt_words"), c.get("bin_weights")))
    if n_shift_bad:
        bad("%d 层 shift != q_frac_bits - act_fs_out" % n_shift_bad)

    # ---- fp_layers 文件齐全 ----
    miss = [f["file"] for f in bl.idx["fp_layers"]
            if not os.path.exists(os.path.join(pack_dir, f["file"]))]
    if miss:
        bad("fp_layers 缺文件：%s" % ", ".join(miss))
    else:
        ok("fp_layers：%d 个文件齐全（%.1f KB）"
           % (len(bl.idx["fp_layers"]),
              sum(f["bytes"] for f in bl.idx["fp_layers"]) / 1024.0))
    if len(bl.idx["fp_layers"]) != 16:
        bad("fp_layers 数量 %d != 16" % len(bl.idx["fp_layers"]))

    if verbose:
        print("== 容器审计：%s ==" % pack_dir)
        for m in N:
            print("  ✓ " + m)
        if P:
            print("  ✗ %d 个问题：" % len(P))
            for m in P:
                print("    - " + m)
        else:
            print("  全部通过（%d 层 / %d B）" % (len(bl.layers), bl.size))
    return P, N


# ============================================================================
# 5. 用 xsim 黄金向量验证本参考实现（"我的参考 == RTL 语义"的本地证据）
# ============================================================================
GOLDEN_NAMES = ("bin_1x32_32_8x8", "bin_1x64_16x16", "bin_3x32_48_8x8",
                "bin_3x32_48_d2", "bin_3x96_64_8x8")


def check_goldens(gdir, names=GOLDEN_NAMES, verbose=True):
    P = []
    for nm in names:
        d = os.path.join(gdir, nm)
        if not os.path.isdir(d):
            P.append("%s: 目录不存在" % nm)
            continue
        meta = json.load(open(os.path.join(d, "meta.json")))
        cin, cout, k = meta["cin"], meta["cout"], meta["k"]
        s, dd, pad = meta["s"], meta["d"], meta["pad"]
        H, W, shift, cb = meta["H"], meta["W"], meta["shift"], meta["cb"]
        A = np.fromfile(os.path.join(d, "act_pack.bin"), "<u8").reshape(cb, H, W)
        words = np.fromfile(os.path.join(d, "wgt_pack.bin"), "<u8").reshape(cout, k * k, cb)
        Pq = dict(alpha_q=np.fromfile(os.path.join(d, "alpha.bin"), "<i2").astype(np.int64),
                  bias_q=np.fromfile(os.path.join(d, "bias.bin"), "<i4").astype(np.int64),
                  zeta_q=np.fromfile(os.path.join(d, "zeta.bin"), "<i4").astype(np.int64),
                  gamma_q=np.fromfile(os.path.join(d, "gamma.bin"), "<i4").astype(np.int64),
                  slope_q=np.fromfile(os.path.join(d, "slope.bin"), "<i2").astype(np.int64),
                  words=words)
        exp = np.fromfile(os.path.join(d, "expected_c8.bin"), np.int8)
        got, _ = conv_rtl(cin, cout, k, s, dd, pad, shift, A, Pq,
                          meta["alpha_shift"], meta["slope_shift"])
        Ho, Wo = out_hw(H, W, s)
        if (Ho, Wo) != (meta["Hout"], meta["Wout"]):
            P.append("%s: 分辨率 %s != meta %s" % (nm, (Ho, Wo), (meta["Hout"], meta["Wout"])))
        mine = np.frombuffer(to_c8(got), np.int8)
        if mine.size != exp.size:
            P.append("%s: 输出字节 %d != 期望 %d" % (nm, mine.size, exp.size))
            continue
        nbad = int((mine != exp).sum())
        if verbose:
            print("  %-20s cin=%3d cout=%3d k=%d cb=%d  %d/%d 字节一致 %s"
                  % (nm, cin, cout, k, cb, exp.size - nbad, exp.size,
                     "✓" if nbad == 0 else "✗"))
        if nbad:
            P.append("%s: %d/%d 字节不符" % (nm, nbad, exp.size))
    return P


# ============================================================================
# CLI
# ============================================================================
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, os.pardir, os.pardir))
    default_pack = os.path.join(root, "deploy", "bin_weights_new")
    default_gold = os.path.join(root, "pl", "rtl_hand", "golden")

    cmd = argv[0] if argv else "audit"
    if cmd == "audit":
        pack = argv[1] if len(argv) > 1 else default_pack
        problems, _ = audit(pack)
        return 1 if problems else 0
    if cmd == "golden":
        gdir = argv[1] if len(argv) > 1 else default_gold
        print("== 用 xsim 黄金向量校验 bin_ref_numpy.conv_rtl ==")
        problems = check_goldens(gdir)
        print("结果：%s" % ("全部逐字节一致 ✓" if not problems else problems))
        return 1 if problems else 0
    if cmd == "slice":
        pack = argv[1] if len(argv) > 1 else default_pack
        i = int(argv[2]) if len(argv) > 2 else 0
        bl = Blob(pack)
        L = bl.layer(i)
        print(json.dumps(L, indent=1, ensure_ascii=False))
        print("wgt 字节 %d / 字 %d" % (len(bl.wgt_bytes(L)), bl.wgt_bytes(L).size // 8))
        print("参数:", {k: (v.tolist()[:8] if hasattr(v, "tolist") else v)
                        for k, v in bl.params(L).items() if k != "words"})
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
