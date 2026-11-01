#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""catpack.py —— `bin_catpack`（R_RUN_UNIT=5）的 PS 驱动

它把 `cat` + `binarize_pack` 合成**一趟**：PL 直接读 DDR 里的 **float32** 激活段
（逐通道一个源地址），做 IEEE-754 `>` 比较（NaN ⇒ 假、±0 相等），直接写 64 位字。
DDR 里**没有**拼接后的大张量 ⇒ 省掉 `np.concatenate` 的整份往返。

RTL 契约（`pl/rtl_hand/modules/bin_catpack.v` + `bin_unit_axi_wrap.v` §3b'，
**全部是读 RTL 核对过的，不是猜的**）：

  实例选择      R_RUN_UNIT（配置页 +0x200）= 5；0=conv、4=E1E2、5=catpack
  配置寄存器    0x18 / 0x20 / 0x28  = cfg_c / cfg_h / cfg_w（与 E1E2 复用）
               0x204 / 0x208 / 0x210 = cfg_group / base_desc / base_out
               0x20C / 0x214        = 两个高字槽（写非 0 ⇒ 启动 ERR_SIZE）
  启动/完成     AP_CTRL(+0x10000) bit0 = start（自清）；AP_RETURN(+0x10010)
               bit0=done、bit1=busy、[15:8]=err_code
  描述符        base_desc + (group*64 + i)*8 = { adj[63:32], src_pa[31:0] }
               i = 8*cb + j（cb=通道字节 0..7，j=字节内通道 0..7）
               src_pa = 该通道第 0 个像素的**绝对物理地址**
  判据          bit(64g + 8i + k) of word(g,p) = x[64g+8i+k, p] > adj[64g+8i+k]
  输出          word(g,p) 写到 base_out + p*8；group g 的输出块在
               OUT + g*HW*8（字节布局与 `bin_engine.quant_pack` 相同）
  ⚠ 对齐       读口是 **64-bit 整字**（wrapper 用 u_rd_addr[31:3] 取字，低 3 位丢弃）
               ⇒ 每个通道的第 0 像素地址必须 8 字节对齐。逐通道地址 =
               pa + c*HW*4，所以 **HW 必须是偶数**（本网所有层 8²..256² 都满足）。
               板上实测：HW=35 时所有字都错（每次读偏 4 B）；HW=40/36/56 逐字节全对。
               HW 奇数 ⇒ `build_desc` 直接 ValueError，绝不让 PL 读错位数据。
  ⚠ 尾写        尾 tile 仍然 flush 满 16 个输出字（ST_FLUSH 固定 wp=0..15），
               无效像素恒 0 ⇒ 最后一个 group 的输出块后最多多写 15 字（120 B）。
               驱动在 out 区后留 128 B slack（之后才是金丝雀），并诊断 slack 是否被清零。
  尺寸          cfg_c ≤ 576、cfg_group ≤ 8、cfg_h/cfg_w ≤ 4096，
               base_desc/base_out 非 0 且 8 字节对齐
  吞吐          tile = 16 像素（64 B = wrapper 一条读线）；每通道每个 tile
               1 拍描述符 + ceil(run/2) 拍数据；full 64 通道组 38.13 拍/字

和 E1E2 的两条**必须**照做的时序纪律（RTL 行为，不是保险起见）：

  1. `run_active` 期间写 AP_CTRL 会被**直接丢掉、不排队** ⇒ 每次 start 前必须
     先忙等 `busy == 0`（`_wait_idle`），否则会把上一跑的 done 当成本命令完成。
  2. 每次 `start_fire` 会把 DBG 计数器清零（RTL §2），所以 DBG 是"本跑"的。

⚠ 本比特流（v33）的 DBG_E 计数器接的是 **E1E2 的读使能**（实测 catpack 跑完
  0x230..0x240 全 0，而同一比特流上 E1E2 有数），所以**读带宽必须用时间反推**
  （见 `stats()` / bench 的斜率法），不能依赖 0x230。驱动不假设 DBG 可用。

用法（板端）：

    import pynq.pl_server.embedded_device  # noqa（PYNQ 3.1 不自动注册）
    from pynq import Overlay
    from bin_engine import BinConvEngine
    from catpack import CatPackEngine

    e  = BinConvEngine(Overlay("design_1_150v33.bit", download=False))
    cp = CatPackEngine(e); cp.probe()          # 确认比特流里有 catpack（写 5 读 5）
    desc, cin, H, W = CatPackEngine.build_desc([(pa, C, H, W)], adj)
    out = cp.pack_tensors([x_f32], adj)        # 在池里切 desc/out，跑完返回字节视图
    out is None  ⇒ 源在宿主内存里（拿不到 pa）⇒ 调用方回落到 PS 路径

本机自检（无板、无 pynq）：

    python3 catpack.py --selftest     # 用"契约级"模型验证描述符/分组/尾块/回退

────────────────────────────────────────────────────────────────────────────
板上实测（2026-10-31，v33，fclk=100 MHz；详见 docs/CATPACK_上板报告.md）
  功能   cin=64/544、2/3/9/11 段、NaN/±inf/±0/次正规、尾 tile —— 与 quant_pack
         **逐字节 0 差异**；整网 64²/256² 的 out sha1 与不开 catpack 完全相同。
  带宽   H*W ≤ 576：285–366 MB/s（83 拍/字，≈ E1E2 的 305 MB/s）
         H*W ≥ 1024：90–142 MB/s（252–269 拍/字）—— 平面 f32 布局的线缓存悬崖：
         每通道每 tile 只读 64 B（半条 128 B 线），8 通道工作集 8*HW*4 超过 64 KB
         后每次都是依赖型 miss。**不是 1 GB/s**。
  端到端 64² 2.68→2.83 s、256² 16.87→17.75 s（都变慢）⇒ 本版本**保持默认关**；
         真正该做的是让 E1E2 直接读 conv 的 int8 C8 OUT，而不是让 catpack 读 f32。
────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import bin_engine as BE                    # noqa: E402

CFG = BE.CFG

#: 0x200 扩展页（与 E1E2 共用；含义由 R_RUN_UNIT 决定）
CAT_REG = dict(
    RUN_UNIT=0x200, E_GROUP=0x204,
    E_DESC=0x208, E_DESC_HI=0x20C,
    E_OUT=0x210, E_OUT_HI=0x214,
    DBG_RD=0x230, DBG_CYC=0x234, DBG_RDST=0x238,
    DBG_WRST=0x23C, DBG_WR=0x240,
)

CATPACK_UNIT = 5
C_MAX = 576                 # RTL 启动前校验：cfg_c > 576 ⇒ ERR_SIZE
#: ★ 实测 RTL 行为（2026-10-31，板上）：**尾 tile 仍然 flush 满 16 个输出字**
#: （bin_catpack.v 的 ST_FLUSH 固定 wp=0..15；无效像素的 stage 恒 0）
#: ⇒ 每个 group 的输出块末尾最多多写 15 个字（120 B），内容恒 0。
#: 中间 group 的这段会被下一个 group 覆盖；**最后一个 group** 会写到输出区之后。
#: 驱动因此在 out 区后固定留 128 B slack（再往后才是金丝雀），并在 slack 上
#: 验证"被写到的部分确实是 0"（只诊断，不依赖：换 RTL 后不写也正常）。
OUT_SLACK_WORDS = 16
OUT_SLACK_BYTES = OUT_SLACK_WORDS * 8
GROUP_MAX = 8               # cfg_group > 8 ⇒ ERR_UNIT
TILE = 16                   # 像素/tile（RTL 参数，只用于拍数模型）


# ============================================================================
# 1. 描述符构造（纯 numpy，可离线测）
# ============================================================================
def build_desc(segments, adj, pad_to_64=True, check=True):
    """逐通道源地址 + 逐通道阈值 → 描述符表（uint64，小端）。

    segments : [(pa, n_channels, H, W), ...]。**段边界任意**（不要求 C%8、
               不限段数），只要每个段是 CHW 连续的 float32、pa 8 字节对齐。
    adj      : (sum n_channels,) float32 逐通道阈值（顺序 = 段的拼接顺序），
               来自 `bin_engine.pack_thresh()`；判据 `x > adj` 是 IEEE `>`。
    pad_to_64: True ⇒ 表按 64 通道（512 B）对齐到整数个 group（多出来的项填 0，
               RTL 不会读到它们：通道号 ≥ cfg_c 的 lane 不发读）。

    返回 (desc, cin, H, W)。若形状不一致 / adj 长度不符 / pa 不合法 ⇒ ValueError。
    """
    adj = np.ascontiguousarray(adj, dtype=np.float32).reshape(-1)
    _starts, _counts = [], []              # 段基址 / 段通道数（最后一次性向量化展开）
    shape = None
    for seg in segments:
        if len(seg) != 4:
            raise ValueError("segment 必须是 (pa, n_channels, H, W)，得到 %r" % (seg,))
        pa, n, h, w = (int(seg[0]), int(seg[1]), int(seg[2]), int(seg[3]))
        if n <= 0 or h <= 0 or w <= 0:
            raise ValueError("segment 尺寸非法：%r" % (seg,))
        if shape is None:
            shape = (h, w)
        elif shape != (h, w):
            raise ValueError("cat 各段的 H/W 必须相同：%r vs %r" % (shape, (h, w)))
        if check:
            if pa == 0:
                raise ValueError("segment 的物理地址为 0 ⇒ PL 读不到（pa=%#x）" % pa)
            if pa & 0x7:
                raise ValueError("segment 的物理地址 %#x 不是 8 字节对齐" % pa)
            if pa + n * h * w * 4 > 0xFFFFFFFF:
                raise ValueError("segment 越过 32 位地址空间（pa=%#x, %d ch）" % (pa, n))
        hw = h * w
        if hw % 2:
            raise ValueError(
                "H*W=%d 是奇数 ⇒ 逐通道地址 pa+c*H*W*4 无法全部 8 字节对齐，"
                "而 catpack 的读口是 64-bit 整字（addr[2:0] 被丢弃）⇒ 会读到错位"
                "数据。请用偶数 H*W（本网所有层都满足）。" % hw)
        _starts.append(int(pa))
        _counts.append(int(n))
    H, W = shape
    # 逐通道源地址 = 段基址 + (c - 段起点)*hw*4，向量化（O(cin) 在 C 里）
    counts = np.asarray(_counts, np.int64)
    starts = np.asarray(_starts, np.int64)
    cin = int(counts.sum())
    firsts = np.cumsum(counts) - counts
    # ⚠ 板端 numpy 是 32 位（intp=int32）：np.repeat 的 repeats 必须能安全转 intp，
    #   否则 "Cannot cast int64 to int32"（本机 x86_64 不会暴露）。地址本身 < 2^31。
    rep32 = counts.astype(np.intp)
    idx = np.arange(cin, dtype=np.int64)
    chans = (np.repeat(starts, rep32)
             + (idx - np.repeat(firsts, rep32)) * (H * W * 4))
    if check and chans.size and (int(chans.min()) == 0 or int((chans & 0x7).any())):
        raise ValueError("有通道的源地址为 0 或不是 8 字节对齐（pa=%s, hw=%d）"
                         % (list(zip(_starts, _counts)), H * W))
    if adj.size != cin:
        raise ValueError("adj 长度 %d != 描述符通道数 %d" % (adj.size, cin))
    if cin > C_MAX:
        raise ValueError("cin=%d > RTL 上限 %d（cfg_c > 576 ⇒ ERR_SIZE）"
                         % (cin, C_MAX))
    nlanes = ((cin + 63) // 64 * 64) if pad_to_64 else cin
    desc = np.zeros(nlanes, np.uint64)
    # 低 32 = src_pa，高 32 = adj 的**原样比特**（f32 位型，不做任何数值转换）
    desc[:cin] = ((adj.view(np.uint32).astype(np.uint64) << np.uint64(32))
                  | chans.astype(np.uint64))
    return desc, cin, H, W


def groups_of(cin):
    """cin → 64 位字组数 cb（= ceil(cin/64)，RTL output 的 group 数）。"""
    return (int(cin) + 63) // 64


def beats_model(cin, H, W):
    """按 RTL 状态机算一次完整 job（全部 group）的 64-bit 读拍数与拍数（**闭式**）。

    每 tile：ST_TINI(1) + 每趟[ST_PASS(1) + 每通道(描述符 1 + 数据 ceil(run/2))
    + ST_ROW(1)] + ST_FLUSH(16) + ST_TEND(1)；另加启动时冲刷 tag 阵列的 512 拍。
    返回 dict：beats（读拍）、data_beats、desc_beats、cycles（估算）、words。

    ★ 必须 O(1)：这个函数在每层都要用（超时估算），早先的 Python 三重循环
      实测 **2.14 ms/次**，而且按组调用 ⇒ 18 ms/job，把 PL 时间完全盖住。
    """
    cin, H, W = int(cin), int(H), int(W)
    hw = H * W
    cb = groups_of(cin)
    tiles = (hw + TILE - 1) // TILE
    full_tiles, tail = divmod(hw, TILE)
    tail_nb = (tail + 1) // 2                      # 尾 tile 的读拍（run 偶数 ⇒ run/2）
    data = desc = cyc = 0
    for g in range(cb):
        cg = min(64, cin - g * 64)
        npass = (cg + 7) // 8
        data += cg * (full_tiles * (TILE // 2) + (tail_nb if tail else 0))
        desc += cg * tiles
        # TINI+TEND 2 拍 + 每趟(ST_PASS+ST_ROW)=2 + 每通道 9 拍 + FLUSH 16
        cyc += tiles * (2 + 2 * npass + 9 * cg + 16)
    return dict(beats=int(data + desc), data_beats=int(data), desc_beats=int(desc),
                cycles=int(512 + cyc), words=int(cb * hw), tiles=int(tiles))


# ============================================================================
# 2. 驱动
# ============================================================================
class CatPackEngine:
    """`bin_unit_<id>` 的 R_RUN_UNIT=5（bin_catpack）后端。

    `core` 是 `BinConvEngine`：复用它的 IP 基址、MMIO 影子缓存、地址护栏。
    catpack 的寄存器全在 0x10000 配置页（0x200 扩展页 + 0x18/0x20/0x28）。

    默认**不**被任何东西挂上：`BinConvEngine.enable_catpack()` 或
    `CATPACK=1`（bin_net）才会创建它。`pack_*` 在源拿不到 pa 时返回 None
    ⇒ 调用方必须回落 PS，**不会**静默给假数据。
    """

    def __init__(self, core, fclk_mhz=100.0, verbose=False, arena=None,
                 canary=True, dbg_reads=False):
        self.core = core
        self.fclk_mhz = float(fclk_mhz)
        self.verbose = bool(verbose)
        self.canary = bool(canary)
        # ★ 默认**不读** DBG_E：本比特流（v33）的 0x230..0x240 接的是 E1E2 的读使能，
        #   对 catpack 恒 0；5 次 MMIO 读 = 0.16 ms/组，小层上比 PL 还贵。
        #   换到"DBG 真的接 u_rd_en"的比特流时置 True（stats() 会自动改用 dbg 口径）。
        self.dbg_reads = bool(dbg_reads)
        self._arena = None                 # BinArena / 外部缓冲（带 physical_address）
        self._arena_pa = 0
        self._arena_cap = 0
        self._probed = False
        self._busy_seen = 0
        self._runs = 0
        self._agg = dict(groups=0, seconds=0.0, read_bytes=0, out_bytes=0,
                         words=0, rd=0, cyc=0, rdst=0, wrst=0, wr=0)
        self.last = None
        self.last_pack = None
        if arena is not None:
            self._adopt(arena)

    # ---------------------------------------------------------------- 能力
    def probe(self):
        """确认比特流里真的有 catpack：写 R_RUN_UNIT=5 读回 5，写 0 读回 0。

        旧比特流的 0x200 页是保留页（写忽略、读 0）⇒ 直接拒绝。**绝不**在没有
        catpack 的比特流上假装成功（那会读到没写的输出 = 静默错数据）。
        """
        off = CFG + CAT_REG["RUN_UNIT"]
        self.core._wr(off, CATPACK_UNIT, force=True)
        got5 = self.core._rd(off)
        self.core._wr(off, 0, force=True)
        got0 = self.core._rd(off)
        if (got5 & 7) != CATPACK_UNIT or (got0 & 7) != 0:
            raise RuntimeError(
                "R_RUN_UNIT 写 5 读回 %#x、写 0 读回 %#x ⇒ 这个比特流没有 "
                "bin_catpack（0x200 页仍是保留页）。请加载含 catpack 的 wrapper；"
                "驱动不会在没有 catpack 的比特流上假装成功。" % (got5, got0))
        # 高字槽写 0 清残留的 r_e_hi_bad（否则启动 ERR_SIZE）
        self.core._wr(CFG + CAT_REG["E_DESC_HI"], 0, force=True)
        self.core._wr(CFG + CAT_REG["E_OUT_HI"], 0, force=True)
        self._probed = True
        return dict(write5=got5, back0=got0)

    def select(self, unit, force=False):
        """写 R_RUN_UNIT。允许 0/4/5（4 是 E1E2，留给共存/切换）。"""
        unit = int(unit)
        if unit not in (0, 4, CATPACK_UNIT):
            raise ValueError("R_RUN_UNIT 只能是 0(conv)/4(E1E2)/5(catpack)，"
                             "给的是 %d" % unit)
        self.core._wr(CFG + CAT_REG["RUN_UNIT"], unit, force=force)
        return unit

    # ---------------------------------------------------------------- 寄存器
    def config(self, c, h, w, base_desc, base_out, group=None):
        """写 6 个配置寄存器（0x18/0x20/0x28/0x204/0x208/0x210）。

        group 不给就只写 5 个（组号由 `run_group` 单独写）。全部走 core._wr 的
        影子缓存 ⇒ 同一 job 的多个 group 只多写 0x204/0x210 两个。
        """
        core = self.core
        c, h, w = int(c), int(h), int(w)
        if not (1 <= c <= C_MAX):
            raise ValueError("c=%d 不在 1..%d" % (c, C_MAX))
        if not (1 <= h <= BE.HW_MAX and 1 <= w <= BE.HW_MAX):
            raise ValueError("h=%d w=%d 不在 1..%d" % (h, w, BE.HW_MAX))
        desc_pa = BE.BinGuard.check_pa(base_desc, "catpack_base_desc")
        out_pa = BE.BinGuard.check_pa(base_out, "catpack_base_out")
        if group is not None:
            self.set_group(group)
        core._wr_reg("C", c)
        core._wr_reg("H", h)
        core._wr_reg("W", w)
        self._wr_e("E_DESC", desc_pa)
        self._wr_e("E_OUT", out_pa)
        return dict(c=c, h=h, w=w, base_desc=desc_pa, base_out=out_pa,
                    group=None if group is None else int(group))

    def set_group(self, g):
        g = int(g)
        if not (0 <= g <= GROUP_MAX):
            raise ValueError("cfg_group=%d 不在 0..%d（RTL 超了会 ERR_UNIT）"
                             % (g, GROUP_MAX))
        self._wr_e("E_GROUP", g)
        return g

    def _wr_e(self, name, v, force=False):
        self.core._wr(CFG + CAT_REG[name], int(v), force=force)

    def _rd_e(self, name):
        return self.core._rd(CFG + CAT_REG[name])

    def _wait_idle(self, timeout=None):
        """等单元 busy=0；**必须在写 AP_CTRL 之前**（RTL 不排队 start）。

        跑着的时候写 start 会被直接丢掉；这时如果再轮询，PS 会把上一跑的 done
        当成本命令完成 ⇒ 读到没写的输出。这里显式等 idle，并把上一跑残留的
        错误码抛出来（不覆盖）。
        """
        t0 = time.time()
        n = 0
        while True:
            st = self.core._rd_reg("AP_RET")
            if not (st & 2):                       # busy=0
                err = (st >> 8) & 0xFF
                if err:
                    raise BE.BinRunError(
                        err, msg="catpack 启动前发现上一跑残留错误 %s(%d)"
                        "（AP_RETURN=%#010x）" % (BE.decode_err(err)[1], err, st))
                return st
            dt = time.time() - t0
            if timeout is not None and dt > timeout:
                raise TimeoutError("catpack 等 idle 超时 %.1fs（上一跑还在访存，"
                                   "不能发新 start）" % dt)
            n += 1
            if n < 4:
                pass
            elif n < 64:
                time.sleep(0)
            else:
                time.sleep(min(2e-4, 2e-6 * (n - 63)))

    def start(self):
        """AP_CTRL bit0（自清）⇒ 每次都必须真的写下去。"""
        self.core._wr_reg("AP_CTRL", 1, force=True)

    def wait_done(self, timeout=None, wdt=None):
        st, dt = self.core.wait(timeout, wdt=wdt)
        err = (st >> 8) & 0xFF
        if err:
            raise BE.BinRunError(
                err, msg="catpack 报错 %s(%d)（AP_RETURN=%#010x）"
                % (BE.decode_err(err)[1], err, st))
        if (st >> 1) & 1:
            raise RuntimeError("catpack done=1 但 busy=1（AP_RETURN=%#010x）"
                               "⇒ 接线/时序可疑" % st)
        return st, dt

    def _timeout_for(self, cin, H, W):
        cyc = beats_model(cin, H, W)["cycles"]
        sec = cyc / (self.fclk_mhz * 1e6)
        return max(2.0, min(120.0, sec * 6.0 + 1.0))

    # ---------------------------------------------------------------- 单跑
    def run_group(self, desc_pa, out_pa, c, h, w, group, timeout=None,
                  wdt=None, count=True, full=True, check_idle=True):
        """一条 catpack 命令（= 一个 64 通道字组）：写寄存器 → 等 idle → start → 等 done。

        `full=False` 只写变化的两项（0x204 组号 / 0x210 输出），`check_idle=False`
        跳过 start 前的 busy 读——**只在"上一组是自己刚 wait_done 过的"时用**
        （RTL 的 done 蕴含 busy=0），这是同一 job 里省 0.25 ms/组的关键。
        """
        if not self._probed:
            raise RuntimeError("还没 probe()：不确定比特流里有没有 catpack")
        self.select(CATPACK_UNIT)
        if full:
            self.config(c, h, w, desc_pa, out_pa, group=group)
        else:
            self.set_group(group)
            self._wr_e("E_OUT", int(out_pa))
        if timeout is None:
            timeout = self._timeout_for(c, h, w)
        if check_idle:
            self._wait_idle(timeout=min(max(10.0, timeout), 120.0))
        self.start()
        st, dt = self.wait_done(timeout, wdt=wdt)
        dbg = self._read_dbg() if (count and self.dbg_reads) else {}
        if count:
            a = self._agg
            a["groups"] += 1
            a["seconds"] += dt
            a["words"] += int(h) * int(w)
            for k in ("rd", "cyc", "rdst", "wrst", "wr"):
                a[k] += int(dbg.get(k, 0))
        self.last = dict(status=st, seconds=dt, err=0, dbg=dbg,
                         desc_pa=int(desc_pa), out_pa=int(out_pa),
                         c=int(c), h=int(h), w=int(w), group=int(group))
        self._runs += 1
        return self.last

    def _read_dbg(self):
        return dict(rd=self._rd_e("DBG_RD"), cyc=self._rd_e("DBG_CYC"),
                    rdst=self._rd_e("DBG_RDST"), wrst=self._rd_e("DBG_WRST"),
                    wr=self._rd_e("DBG_WR"))

    def clear_dbg(self):
        """写 0x230 把 5 个 DBG 计数器一起清（RTL clr_e_dbg）。"""
        self._wr_e("DBG_RD", 0, force=True)

    # ---------------------------------------------------------------- 池
    def _adopt(self, buf):
        """接一块现成的物理连续缓冲（BinArena / pynq buffer / RegionBuffer）。"""
        pa = int(getattr(buf, "physical_address", 0) or 0)
        mv = BE._u8(buf)
        if pa == 0:
            raise ValueError("catpack 的 arena 没有 physical_address ⇒ PL 读不到")
        self._arena, self._arena_pa = buf, pa
        self._arena_cap = len(mv)
        self._mv = mv
        try:
            self.core.add_region(buf if hasattr(buf, "physical_address") else mv,
                                 "catpack")
        except Exception:                      # noqa: BLE001  已登记 / FakeUnit
            pass

    def _ensure_arena(self, need):
        need = int(need) + (BE.CANARY_BYTES if self.canary else 0)
        if self._arena is not None and self._arena_cap >= need:
            return
        if BE._pynq_allocate is None:
            raise RuntimeError("没有 pynq：请显式传 arena=（带 physical_address 的缓冲）")
        # 1.25× 余量，避免逐层 realloc（CMA 碎片）
        nb = (int(need * 1.25) + 4095) // 4096 * 4096
        a = BE.BinArena(nb, name="catpack")
        self._adopt(a.buf)
        if self.verbose:
            print("[catpack] arena %.2f MB @%#x" % (nb / 1e6, self._arena_pa))

    def _bufs(self, desc_bytes, out_bytes):
        """在 arena 里切 (desc 视图, desc_pa, out 视图, out_pa)。

        out 视图 = out 区 + 128 B slack + 4 KB 金丝雀（见 OUT_SLACK_BYTES）。
        """
        desc_bytes, out_bytes = int(desc_bytes), int(out_bytes)
        need = desc_bytes + out_bytes + OUT_SLACK_BYTES
        self._ensure_arena(need)
        mv = self._mv
        o_desc = 0
        o_out = (desc_bytes + 63) // 64 * 64
        tail = OUT_SLACK_BYTES + (BE.CANARY_BYTES if self.canary else 0)
        if o_out + out_bytes + tail > len(mv):
            raise MemoryError("catpack arena 不够：需要 %d B，有 %d B"
                              % (need, len(mv)))
        dv = BE.RegionBuffer(mv[o_desc:o_desc + desc_bytes],
                             self._arena_pa + o_desc, self._arena)
        ov = BE.RegionBuffer(mv[o_out:o_out + out_bytes + tail],
                             self._arena_pa + o_out, self._arena)
        return dv, int(dv.physical_address), ov, int(ov.physical_address)

    # ---------------------------------------------------------------- 打包
    def pack_segments(self, segments, adj, cin=None, out_buf=None, flush=True,
                      wdt=None, count=True, restore=True):
        """多段 float32 → 打包字节。**调用方负责 flush 源**（除非给的是池张量）。

        segments : [(pa, n_channels, H, W), ...]（`f32_segments()` 的输出格式）
        adj      : (cin,) float32 阈值
        out_buf  : 可选，写进这块缓冲（>= cb*H*W*8 + 金丝雀）；不给就用内部 arena

        返回 (uint8 只读视图, N, H, W, cb)，**在下一次 pack 之前有效**。
        段数/形状不合法 ⇒ ValueError；**不会**返回长度不对的数据。
        """
        if not self._probed:
            raise RuntimeError("还没 probe()：不确定比特流里有没有 catpack")
        t_ph = {}
        _t0 = time.perf_counter()
        desc, n, H, W = build_desc(segments, adj)
        t_ph["build"] = time.perf_counter() - _t0
        if cin is not None and int(cin) != n:
            raise ValueError("cin=%d 与描述符通道数 %d 不符" % (int(cin), n))
        hw = H * W
        cb = groups_of(n)
        out_bytes = cb * hw * 8
        if out_buf is not None:
            want_len = (out_bytes + OUT_SLACK_BYTES
                        + (BE.CANARY_BYTES if self.canary else 0))
            if len(out_buf) != want_len:
                raise ValueError("out_buf 必须是 %d B（out %d + slack %d + 金丝雀 %d），"
                                 "给的是 %d B"
                                 % (want_len, out_bytes, OUT_SLACK_BYTES,
                                    BE.CANARY_BYTES if self.canary else 0,
                                    len(out_buf)))
            out_pa = BE.BinGuard.check_pa(
                int(getattr(out_buf, "physical_address", 0)), "catpack_out")
            ov = out_buf
            # 描述符表单独放在内部 arena 里（输出用调用方的）
            self._ensure_arena(desc.nbytes)
            dv = BE.RegionBuffer(self._mv[:desc.nbytes], self._arena_pa,
                                 self._arena)
            desc_pa = int(dv.physical_address)
        else:
            dv, desc_pa, ov, out_pa = self._bufs(desc.nbytes, out_bytes)
        _t0 = time.perf_counter()
        BE._u8(dv)[:desc.nbytes] = np.frombuffer(desc.astype("<u8").tobytes(),
                                                 np.uint8)
        BE._u8(ov)[out_bytes:out_bytes + OUT_SLACK_BYTES] = 0x5A   # 尾写诊断图案
        if self.canary:
            BE._u8(ov)[out_bytes + OUT_SLACK_BYTES:
                       out_bytes + OUT_SLACK_BYTES + BE.CANARY_BYTES] = BE.CANARY
        if flush:
            BE._flush(dv)
        t_ph["setup"] = time.perf_counter() - _t0
        self.select(CATPACK_UNIT)
        if self.dbg_reads:
            self.clear_dbg()
        t0 = time.perf_counter()
        to = self._timeout_for(n, H, W)          # 一次 job 一次（beats_model 是纯函数）
        try:
            for g in range(cb):
                self.run_group(desc_pa, out_pa + g * hw * 8, n, H, W, g,
                               timeout=to, wdt=wdt, count=count,
                               full=(g == 0), check_idle=(g == 0))
        finally:
            if restore:
                self.select(0, force=True)
        wall = time.perf_counter() - t0
        t_ph["groups"] = wall
        _t0 = time.perf_counter()
        BE._invalidate(ov)
        t_ph["inval"] = time.perf_counter() - _t0
        _t0 = time.perf_counter()
        if self.canary:
            # 金丝雀从"out + slack"之后开始（slack 是被 RTL 尾写合法覆盖的区）
            BE.BinGuard.check_canary(ov, out_bytes + OUT_SLACK_BYTES, "catpack OUT")
        tail_words = (TILE - (hw % TILE)) % TILE
        tail_zeroed = None
        if tail_words:
            tail = np.frombuffer(BE._u8(ov)[out_bytes:out_bytes + tail_words * 8],
                                 np.uint8)
            tail_zeroed = bool(not tail.any())
        t_ph["final"] = time.perf_counter() - _t0
        data = np.frombuffer(BE._u8(ov)[:out_bytes], np.uint8)
        data.flags.writeable = False
        self._agg["read_bytes"] += int(sum(int(s[1]) for s in segments)) * hw * 4
        self._agg["out_bytes"] += int(out_bytes)
        self.last_pack = dict(n=n, H=H, W=W, cb=cb, groups=cb, bytes=int(out_bytes),
                              desc_bytes=int(desc.nbytes), wall_s=wall,
                              segments=len(segments), restore=bool(restore),
                              phase_s=t_ph,
                              tail_words=(TILE - (hw % TILE)) % TILE,
                              tail_zeroed=tail_zeroed,
                              out_slack_bytes=OUT_SLACK_BYTES)
        return data, n, H, W, cb

    def pack_tensors(self, tensors, adj, cin=None, flush=True, wdt=None,
                     count=True, restore=True):
        """多块 f32 张量（cat 的各段，CHW 连续）→ 打包字节。

        **任何一段拿不到 pa / 不是 f32 / 不连续 ⇒ 返回 None**（调用方回落 PS）。
        拿得到就把每一段都 `f32_flush()`（只 flush 它那个槽）再跑。
        """
        seen = []
        segs = []
        for t in tensors:
            s = BE.f32_segments(t)
            if not s:
                return None
            if flush:
                BE.f32_flush(t)
            segs.extend(s)
            seen.append(t)
        return self.pack_segments(segs, adj, cin=cin, flush=False, wdt=wdt,
                                  count=count, restore=restore)

    def pack_tensor(self, tensor, adj, cin=None, flush=True, wdt=None,
                    count=True, restore=True):
        """单源（N=1）：f32 → 1bit 打包。等价于 `quant_pack(x, adj)` 的输出。

        这是取代 E1E2 的关键用法：**不再需要 PS 量化成 int8**（省 140 ms/层）。
        拿不到 pa ⇒ None（调用方回落 PS）。
        """
        return self.pack_tensors([tensor], adj, cin=cin, flush=flush, wdt=wdt,
                                 count=count, restore=restore)

    # ---------------------------------------------------------------- 统计
    def stats(self, reset=False):
        """把累计的组数/秒数/字节数换算成读带宽与"拍/字"。

        ⚠ 本比特流的 DBG_E 对 catpack 不计数（实测 0），所以这里给两种口径：
          · `seconds`：PS 侧 start→done 的墙钟（含轮询粒度，大 job 才准）
          · `dbg_*`  ：若比特流真的把 DBG 接到 catpack，则非 0；用 `source` 标明
        拍/字 = cycles / (words)，cycles 优先用 DBG（准确），否则用墙钟 × fclk。
        """
        a = dict(self._agg)
        f = self.fclk_mhz
        use_dbg = (a["cyc"] > 0 and a["rd"] > 0)
        if use_dbg:
            cyc = a["cyc"]
            rd = a["rd"]
            sec = cyc / (f * 1e6)
            source = "dbg"
        else:
            sec = a["seconds"]
            cyc = sec * f * 1e6
            rd = 0.0
            source = "wall"
        read_bytes = a["read_bytes"]
        words = a["words"] or 1
        out = dict(source=source, groups=a["groups"], seconds=a["seconds"],
                   cycles=cyc, words=a["words"], read_bytes=read_bytes,
                   out_bytes=a["out_bytes"],
                   read_bw_mbps=(read_bytes / sec / 1e6 if sec > 0 else 0.0),
                   read_mb_per_s=(read_bytes / sec / 1e6 if sec > 0 else 0.0),
                   beats_per_cycle=(rd / cyc if (use_dbg and cyc) else 0.0),
                   cycles_per_word=(cyc / words),
                   ns_per_word=(cyc / words / f * 1e3),
                   fclk_mhz=f, dbg_cycles=a["cyc"], dbg_rd_beats=a["rd"],
                   probe_busy_seen=self._busy_seen)
        if reset:
            for k in ("groups", "seconds", "read_bytes", "out_bytes", "words",
                      "rd", "cyc", "rdst", "wrst", "wr"):
                self._agg[k] = 0
            self._runs = 0
        return out

    def summary(self):
        s = self.stats()
        return ("CatPackEngine(unit=%d, %d 组, %.3f ms, %.1f MB/s, %.2f 拍/字, "
                "source=%s)" % (CATPACK_UNIT, s["groups"], s["seconds"] * 1e3,
                                s["read_bw_mbps"], s["cycles_per_word"],
                                s["source"]))

    def __repr__(self):
        return self.summary()


# ============================================================================
# 3. 本机自检：契约级模型（不验证 RTL，只验证驱动/描述符/分组/尾块/回退）
# ============================================================================
class _ModelCore:
    """照抄 `bin_catpack.v` 的**契约**（不是逐拍时序）的寄存器/存储模型。

    · 0x200 页：R_RUN_UNIT 写/读；0x208/0x210 低字 + 高字槽；
    · AP_CTRL 只在 !busy 时登记 start；start 时校验（与 vcode 同义）并清 DBG；
    · 执行：按 group 读描述符、逐通道逐 tile 比较、写 64 位字（含尾部 dummy 读）；
    · DBG_E 模拟"本比特流"的行为：对 catpack **不计数**（可控开关）。
    """

    class _MMIO:
        def __init__(self, u):
            self.u = u

        def write(self, off, v):
            self.u.write(off, v)

        def read(self, off):
            return self.u.read(off)

    def __init__(self, base_pa=0x4006_0000, name="bin_unit_0", dbg_for_catpack=False):
        self.name = name
        self.ip_dict = {name: dict(phys_addr=int(base_pa), mem_range_size=0x20000)}
        self.mmio = _ModelCore._MMIO(self)
        self.base_pa = int(base_pa)
        self.reg = {}
        self.run_unit = 0
        self.r_egrp = self.r_edesc = self.r_eout = 0
        self.hi_bad = 0
        self.done = self.busy = self.err = 0
        self.r_ctrl = 0
        self.mem = {}
        self.dbg = dict(rd=0, cyc=0, rdst=0, wrst=0, wr=0)
        self.dbg_for_catpack = bool(dbg_for_catpack)
        self.starts = 0
        self.starts_dropped = 0
        self.reads = 0

    def __getattr__(self, k):
        """pynq 的 IP 对象是挂在 overlay 名字上的；模型也照做。"""
        if k.startswith("bin_unit"):
            return self
        raise AttributeError(k)

    def map_mem(self, pa, buf):
        self.mem[int(pa)] = buf

    def _find(self, pa, n):
        for lo, b in self.mem.items():
            if lo <= pa and pa + n <= lo + len(b):
                return b, pa - lo
        raise AssertionError("模型越界访存：pa=%#x（%d B）" % (pa, n))

    def write(self, off, v):
        off, v = int(off), int(v) & 0xFFFFFFFF
        page = off >= CFG
        o = (off - CFG) if page else off
        if not page:
            return
        if o >= 0x200:
            if o == 0x200:
                self.run_unit = v & 7
            elif o == 0x204:
                self.r_egrp = v
            elif o == 0x208:
                self.r_edesc = v
            elif o == 0x210:
                self.r_eout = v
            elif o == 0x20C:
                self.hi_bad = (self.hi_bad & 1) | (1 if v else 0)
            elif o == 0x214:
                self.hi_bad = (self.hi_bad & 2) | (2 if v else 0)
            elif o == 0x230:
                self.dbg = dict(rd=0, cyc=0, rdst=0, wrst=0, wr=0)
            return
        if o == 0x00:
            self.r_ctrl = v
            if (v & 1) and not self.busy:
                self._start()
            elif v & 1:
                self.starts_dropped += 1
            return
        self.reg[o] = v

    def read(self, off):
        off = int(off)
        page = off >= CFG
        o = (off - CFG) if page else off
        if not page:
            return 0
        if o == 0x00:
            return (self.r_ctrl >> 1 & 1) << 1
        if o == 0x10:
            return ((self.err & 0xFF) << 8) | (self.busy << 1) | self.done
        if o == 0x200:
            return self.run_unit
        if o == 0x204:
            return self.r_egrp
        if o == 0x208:
            return self.r_edesc
        if o == 0x210:
            return self.r_eout
        if o == 0x20C:
            return self.hi_bad & 1
        if o == 0x214:
            return self.hi_bad & 2
        if o in (0x230, 0x234, 0x238, 0x23C, 0x240):
            return self.dbg[{0x230: "rd", 0x234: "cyc", 0x238: "rdst",
                             0x23C: "wrst", 0x240: "wr"}[o]]
        return int(self.reg.get(o, 0))

    # xsim 同义的启动校验（vcode_e：desc/out 非 0、8B 对齐、高字、c≤576、g≤8）
    def _vcode(self):
        g = lambda o: int(self.reg.get(o, 0))
        if self.hi_bad:
            return 2
        if (g(0x20) == 0 or g(0x20) > BE.HW_MAX or g(0x28) == 0
                or g(0x28) > BE.HW_MAX):
            return 2
        if g(0x18) == 0 or g(0x18) > C_MAX:
            return 2
        if self.r_edesc == 0 or self.r_eout == 0:
            return 1
        if (self.r_edesc & 7) or (self.r_eout & 7):
            return 5
        if self.r_egrp > GROUP_MAX:
            return 6
        return 0

    def _start(self):
        self.starts += 1
        self.done = self.busy = self.err = 0
        code = self._vcode()
        if code:
            self.done, self.err = 1, code
            return
        cin, H, W = (int(self.reg[0x18]), int(self.reg[0x20]), int(self.reg[0x28]))
        g = int(self.r_egrp)
        hw = H * W
        desc_b, desc_o = self._find(self.r_edesc + g * 64 * 8, 8 * 8)
        out_b, out_o = self._find(self.r_eout, hw * 8)
        self.busy = 1
        try:
            cyc = 512
            rd = wr = 0
            # 逐通道逐 tile：与 RTL 的"通道主序 → 像素主序"逐位一致
            hw_out = np.zeros(hw, np.uint64)
            for tile in range((hw + TILE - 1) // TILE):
                p0 = tile * TILE
                run = min(TILE, hw - p0)
                nb = (run + 1) // 2
                for cb in range(8):
                    for j in range(8):
                        ch = g * 64 + cb * 8 + j
                        if ch >= cin:
                            continue
                        dpa = self.r_edesc + g * 512 + (cb * 8 + j) * 8
                        db, do = self._find(dpa, 8)
                        w = int(np.frombuffer(db[do:do + 8].tobytes(), "<u8")[0])
                        src, adj = w & 0xFFFFFFFF, np.uint32((w >> 32) & 0xFFFFFFFF)
                        adjf = adj.view(np.float32)
                        sb, so = self._find(src + p0 * 4, 4 * run)
                        xs = np.frombuffer(sb[so:so + 4 * run].tobytes(), "<f4")
                        self.reads += 1
                        rd += 1 + nb
                        cyc += 1 + 1 + nb + 1        # ST_PASS + (D0 + nb) + ROW
                        bits = np.greater(xs, adjf)   # IEEE：NaN⇒False
                        for p in range(run):
                            if bits[p]:
                                hw_out[p0 + p] |= np.uint64(1) << np.uint64(cb * 8 + j)
                cyc += 16 + 2                        # ST_FLUSH + TEND + TINI
            ob, oo = self._find(self.r_eout, hw * 8)
            ob[oo:oo + hw * 8] = np.frombuffer(
                hw_out.astype("<u8").tobytes(), np.uint8)
            wr = hw
            self.dbg = dict(rd=0, cyc=0, rdst=0, wrst=0, wr=0)   # v33 的实测行为
        finally:
            self.busy = 0
            self.done = 1


def _selftest(verbose=True):
    """驱动级自检：描述符布局 / 多 group / 尾块 / 多段 / 拿不到 pa 的回落。"""
    ok_all = True

    def chk(cond, msg):
        nonlocal ok_all
        ok_all = ok_all and bool(cond)
        if verbose:
            print("  [%s] %s" % ("OK " if cond else "FAIL", msg))

    rng = np.random.default_rng(20261031)
    unit = _ModelCore()
    core = BE.BinConvEngine(unit, unit_id=0, verbose=False)
    cp = CatPackEngine(core, verbose=False)

    # 造一块"物理"内存 + 把 f32 源放进去（RegionBuffer 让它带 PA）
    mem = BE.MemBuf(1 << 21, 0x1000_0000)
    unit.map_mem(0x1000_0000, mem)
    arena = BE.FakeArena(size=1 << 21, base_pa=0x2000_0000, unit=unit)
    cp._adopt(arena.buf)

    def run_case(cin, H, W, parts, tag):
        hw = H * W
        total = 0
        segs = []
        xs = []
        off = 0
        mv = BE._u8(mem)
        for c in parts:
            x = (rng.standard_normal((c, H, W)) * 0.7).astype(np.float32)
            if total == 0:
                x.reshape(-1)[0] = np.nan          # NaN ⇒ 假
            if x.size > 1:
                x.reshape(-1)[1] = np.float32(0.25)
            mv[off:off + x.nbytes] = np.frombuffer(x.tobytes(), np.uint8)
            segs.append((0x1000_0000 + off, c, H, W))
            xs.append(x)
            off += x.nbytes
            total += c
        adj = (rng.standard_normal(total) * 0.3).astype(np.float32)
        if total > 1:
            adj[1] = np.float32(0.25)
        data, n, hh, ww, cb = cp.pack_segments(segs, adj)
        want = BE.quant_pack(np.concatenate(xs, 0), adj)
        got = np.frombuffer(data, np.uint8).copy().view(np.uint64).reshape(cb, H, W)
        same = (n == total and hh == H and ww == W
                and np.array_equal(got, want))
        chk(same, "%s: cin=%d %dx%d parts=%d → 逐字节 == quant_pack（%d 差异）"
            % (tag, total, H, W, len(parts), int((got != want).sum())))
        return same

    if verbose:
        print("[catpack] 1) probe / 单位选择")
    info = cp.probe()
    chk(info["write5"] & 7 == 5 and info["back0"] & 7 == 0, "probe 5→5, 0→0")
    bad = False
    try:
        CatPackEngine(core).select(3)
    except ValueError:
        bad = True
    chk(bad, "select(3) 被拒（只允许 0/4/5）")

    if verbose:
        print("[catpack] 2) 单源：形状/尾块/末组补零")
    run_case(64, 8, 8, [64], "1 group 整块")
    run_case(544, 24, 24, [544], "9 groups + 末组 32 通道")
    run_case(48, 5, 8, [48], "尾 tile（hw=40，不是 16 的倍数）")
    run_case(64, 4, 9, [64], "尾 tile（hw=36）")
    run_case(112, 3, 4, [112], "极小块（hw=12）")
    run_case(1, 1, 2, [1], "cin=1 单通道（hw=2）")

    if verbose:
        print("[catpack] 3) 多源（cat 的各段）")
    run_case(112, 32, 32, [64, 48], "2 段")
    run_case(176, 16, 16, [64, 32, 48, 32], "4 段不等长")
    run_case(240, 8, 8, [32] * 11 + [8], "12 段")
    run_case(544, 24, 24, [64] * 8 + [32], "9 段（最大层）")

    if verbose:
        print("[catpack] 4) 宿主内存 ⇒ 返回 None（必须回落 PS）")
    host = np.zeros((64, 8, 8), np.float32)
    chk(cp.pack_tensor(host, np.zeros(64, np.float32)) is None,
        "宿主 numpy（无 PA）⇒ None，不伪造输出")
    chk(BE.f32_segments(host) == [], "f32_segments(宿主) == []")

    if verbose:
        print("[catpack] 5) 护栏：非 8B 对齐 / cin 超限 / adj 长度不符")
    for bad_args, tag in ((dict(segments=[(0x1000_0001, 8, 8, 8)],
                                adj=np.zeros(8, np.float32)), "pa 未对齐"),
                          (dict(segments=[(0x1000_0000, 577, 8, 8)],
                                adj=np.zeros(577, np.float32)), "cin>576"),
                          (dict(segments=[(0x1000_0000, 8, 8, 8)],
                                adj=np.zeros(9, np.float32)), "adj 长度不符"),
                          (dict(segments=[(0x1000_0000, 8, 5, 7)],
                                adj=np.zeros(8, np.float32)), "HW 奇数")):
        try:
            cp.pack_segments(**bad_args)
            chk(False, "%s 应该抛错" % tag)
        except ValueError:
            chk(True, "%s ⇒ ValueError" % tag)

    if verbose:
        print("[catpack] 6) 统计口径（本比特流 DBG 不计数 ⇒ wall）")
    st = cp.stats(reset=True)
    chk(st["source"] == "wall" and st["groups"] > 0,
        "DBG 全 0 时 stats() 自动用 wall 口径（groups=%d）" % st["groups"])
    chk(st["read_bw_mbps"] > 0 and st["cycles_per_word"] > 0,
        "读带宽/拍字非零（%.1f MB/s, %.2f 拍/字）"
        % (st["read_bw_mbps"], st["cycles_per_word"]))

    if verbose:
        print("\n[catpack] selftest: %s" % ("PASS" if ok_all else "FAIL"))
    return 0 if ok_all else 1


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="bin_catpack (R_RUN_UNIT=5) 驱动自检")
    ap.add_argument("--selftest", action="store_true", help="跑本机契约模型自检")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
