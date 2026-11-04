#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run100.py —— **内存守纪律**的 100 帧"算一张、显示一张"运行器（T-163）

为什么要有这个文件
==================
板端用 ``board_verify_numfix.py`` / 自写 runner 跑 100 帧，跑到一半把整机压死
（``ping`` 通、``sshd`` 无 banner）。已核实的代码证据：

* ``board_verify_numfix.py`` 里 **``BV_FREE`` 一次都没出现** ⇒ ``bin_net.py:3189``
  读到默认 ``"0"`` ⇒ 396 个中间张量常驻 **726 MB**；加上 arena 66.2 MB + Python 常驻
  > 880 MB 可用 ⇒ 走 512 MB SD swap ⇒ ``sshd`` fork 失败（同时解释 28~29 s/帧）。
* 另两个放大项：① 一次性把 100 个输入全载进 ``x0_all``（≈78 MB）；
  ② 惰性句柄跨帧钉住 OUT 槽。

本脚本只用**一条**内存纪律：``BV_FREE=1``（第一行日志打印实际取值）+ 单 ``BinNet``
全程复用 + 输入按需读 + 帧尾显式释放 + 内存看门狗 + ``0x2C`` 校验。
**不写任何寄存器**：显示走 ``display_layout.compose`` + ``DisplayPipeline.show``
（板端经 ``minimal_proof.bringup()`` 的已验证路径）。

用法
====
板端一条完整命令见 ``README.md``；主机空跑::

    python3 run100.py --dry-run --inputs <frozen256> --frames 20 \\
        --out logs/host_dryrun_meta

主机空跑覆盖：输入流式读 + 每帧 JSON 追加 + 计时/内存记录 + 看门狗 + 0x2C 校验
+ compose/show/wait（假寄存器 + 本地内存块，真 ``DisplayPipeline`` 代码路径）。
**不覆盖**：真 PL 交换、CMA、真实 ``0x2C`` 计数、真 ``BinConvEngine``（后者需
``--engine ref`` 用 numpy 参考路径覆盖数值/释放路径，但仍是 host 内存）。

字段与判读见 ``README.md``。**本文件不修改生产目录**（只 ``sys.path`` 只读引用）。
"""
from __future__ import annotations

import os
import sys

# ★★ 第一纪律：默认 BV_FREE=1，且**在 import 任何 bin_* 之前**落进环境。★★
#    （bin_net 在 run() 内部读它；但 BIN_*/Q8CACHE 一类开关是在**模块导入时**读走的，
#    所以统一提前设。这里 setdefault：外面显式给 0 时不会被悄悄改回来，而是**报错**。）
os.environ.setdefault("BV_FREE", "1")

import argparse          # noqa: E402
import gc                # noqa: E402
import glob              # noqa: E402
import hashlib           # noqa: E402
import json              # noqa: E402
import time              # noqa: E402

import numpy as np       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
#: 仓库根（主机布局：worktrees/T-163-run100/ 往上两层）
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
#: 生产 PS 代码目录（**只读引用**，一个字节都不改）
PROD = os.path.join(REPO, "pynq_specialized_code", "ps", "pynq")

#: 板端默认路径（README 里的一条命令用同一组常量）
BOARD_PACK = "/home/xilinx/demoir/deploy/pack_v60"
BOARD_BIT = "/home/xilinx/demoir/design_1_v60.bit"
BOARD_INPUTS = "/home/xilinx/demo/inputs100"

FPGA_STATE = "/sys/class/fpga_manager/fpga0/state"
SLCR = 0xF800_0000
FPGA0_CLK_CTRL = 0x170


# ===========================================================================
# 0. 基础工具
# ===========================================================================
def sha1_of(a) -> str:
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _mem_kb(key: str) -> int:
    """``/proc/meminfo`` 里某项的 kB；缺失 ⇒ -1。"""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(key + ":"):
                    return int(line.split()[1])
    except OSError:
        pass
    return -1


def sample_mem() -> dict:
    """内存三元组（MB）。**每帧一次**；不可读的项记 -1.0。"""
    rss = -1
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                    break
    except OSError:
        pass
    avail = _mem_kb("MemAvailable")
    cma = _mem_kb("CmaFree")
    return {"rss_mb": round(rss / 1024.0, 2) if rss >= 0 else -1.0,
            "mem_available_mb": round(avail / 1024.0, 2) if avail >= 0 else -1.0,
            "cma_free_mb": round(cma / 1024.0, 2) if cma >= 0 else -1.0}


def fclk_true_mhz():
    """读 SLCR ``0xF8000170`` 解 FCLK0 真值（PYNQ 报数 = 真值 ×1.5，不可直接信）。

    只读；失败返回 ``(None, None)``（主机 / 无权限时）。
    """
    try:
        import ctypes
        import mmap
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        try:
            m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED,
                          mmap.PROT_READ | mmap.PROT_WRITE, offset=SLCR)
            raw = ctypes.c_uint32.from_buffer(m, FPGA0_CLK_CTRL).value
            m.close()
        finally:
            os.close(fd)
        return (3200.0 / 3.0) / (((raw >> 8) & 0x3F) + 1), raw
    except Exception:                       # noqa: BLE001
        return None, None


# ===========================================================================
# 1. 输入：**一次只读一张**（流式/环形）
# ===========================================================================
def list_inputs(d: str) -> list:
    files = sorted(glob.glob(os.path.join(d, "*.npy")))
    if not files:
        raise SystemExit("[inputs] %s 下没有 *.npy（需要 NNNN_crop256.npy 一类）" % d)
    return files


def load_input(path: str, side: int) -> tuple:
    """读一张 → ``(3,side,side) float32 ∈[0,1]`` + 输入信息。**用完即弃**。

    归一化口径与 ``board_verify_numfix.load_input`` / ``dump_board_int8.norm_input``
    完全一致（``max>1.5 ⇒ /255``；``(H,W,3)``/``(3,H,W)`` 都认）。``side`` 与文件边长
    不同时按**整数索引最近邻**降采样（只为主机空跑降成本，不改口径）。
    """
    a = np.load(path)
    raw = {"file": os.path.basename(path), "raw_shape": list(a.shape),
           "raw_dtype": str(a.dtype), "md5": md5_of(path)}
    if a.ndim == 2:
        a = np.stack([a, a, a])
    if a.ndim != 3:
        raise SystemExit("[inputs] %s 不是 2/3 维：%s" % (path, a.shape))
    if a.shape[0] in (1, 3, 4):
        a = a[:3]
    elif a.shape[2] in (1, 3, 4):
        a = a[:, :, :3].transpose(2, 0, 1)
    else:
        raise SystemExit("[inputs] %s 通道维认不出：%s" % (path, a.shape))
    a = a.astype(np.float64)
    if a.max() > 1.5:
        a = a / 255.0
        raw["divided_by_255"] = True
    else:
        raw["divided_by_255"] = False
    if a.shape[1] != side or a.shape[2] != side:
        iy = (np.arange(side) * a.shape[1]) // side
        ix = (np.arange(side) * a.shape[2]) // side
        a = a[:, iy][:, :, ix]
        raw["decimated_to"] = [side, side]
    x = np.ascontiguousarray(a, dtype=np.float32)
    raw["in_sha1"] = sha1_of(x)
    del a
    return x, raw


def to_u8_chw(x) -> np.ndarray:
    """``(3,H,W) float32`` → ``(3,H,W) uint8``（上屏口径 round(255·clip)）。"""
    a = np.asarray(x, dtype=np.float32)
    return np.rint(np.clip(a, 0.0, 1.0) * np.float32(255.0)).astype(np.uint8)


def to_hwc(u8_chw: np.ndarray) -> np.ndarray:
    """``(3,H,W) uint8`` → ``(H,W,3) uint8``（``display_layout.compose`` 契约）。"""
    return np.ascontiguousarray(np.asarray(u8_chw).transpose(1, 2, 0))


def _fit_for_display(img_hwc: np.ndarray, side: int = 256) -> np.ndarray:
    """把显示输入规整成 ``(256,256,3) uint8``（compose 的硬契约；非 256 时最近邻缩放）。"""
    a = np.ascontiguousarray(img_hwc)
    if a.shape[:2] == (side, side):
        return a
    iy = (np.arange(side) * a.shape[0]) // side
    ix = (np.arange(side) * a.shape[1]) // side
    return np.ascontiguousarray(a[iy][:, ix])


def nonblack_frac(img_hwc: np.ndarray, thr: int = 8) -> float:
    """非黑像素占比：``max(R,G,B) > thr`` 的比例（V-B 的 ``nonblack ≥ 88%`` 同一口径）。"""
    a = np.asarray(img_hwc)
    if a.ndim == 2:
        return float((a > thr).mean())
    return float((a.max(axis=2) > thr).mean())


# ===========================================================================
# 2. 引擎 / 网络
# ===========================================================================
class FakeNet:
    """主机空跑用的确定性替身：接口与 ``BinNet.run`` 一致，输出 ``clip(0.8x+0.1)``。

    **只在 ``--dry-run --engine fake`` 下使用**（绝不会在真引擎失败时"降级"到这里）。
    它刻意保留每帧的临时 f32 数组（``out1`` = 786 KB），用来证明"帧尾释放 + 不留引用"
    的内存曲线是平的。
    """

    def __init__(self, side: int = 256, verbose: bool = False):
        self.side, self.verbose = int(side), bool(verbose)
        self.stats = {"fake": True, "total": 0.0, "pl": 0.0, "ps": 0.0}
        self._graph = None
        self._qcache = None
        self._catpins = {}

    def run(self, x0, pipeline=True, nbuf=2, arena=None, verify=False):
        t0 = time.time()
        x = np.asarray(x0, dtype=np.float32)
        out1 = np.clip(x * np.float32(0.8) + np.float32(0.1), 0.0, 1.0)
        # 模拟一层 upsample/downsample 的结构（out2/out3 与真网同形，便于校验形状假设）
        out2 = np.ascontiguousarray(out1[:, ::2, ::2])
        out3 = np.ascontiguousarray(out1[:, ::4, ::4])
        self.stats = {"fake": True, "total": time.time() - t0, "pl": 0.0, "ps": 0.0}
        return {"out1": out1, "out2": out2, "out3": out3}, self.stats


def build_board_net(pack: str, bit: str, side: int, slots: int,
                    fclk: float | None, log=print):
    """板端：``BinNet`` + ``BinArena`` **各只建一次**（N 帧复用）。

    与 ``demo_pipeline.build_v57_net`` / ``dump_board_int8.main`` 同一套已验证写法：
    ① ``load_kernels()`` 把一次性内核装载（1.4~1.9 s/个 ×3）挪出首帧计时；
    ② 用 ``engine=None`` 的临时 net 只做**建图**，量出 arena 尺寸（不跑数值）；
    ③ arena 公式与 ``bin_net.run(arena=None)`` 内部一致：
       ``(max_in + max_out + 4096) * slots + 64 KiB``。
    """
    import bin_engine as BE
    import bin_net as BN

    BE.load_kernels()                       # 一次性：内核 load + 逐位自检

    n0 = BN.BinNet(pack, engine=None, H=side, W=side, verbose=False)
    g0, _o0, _t0 = n0.build_graph(np.zeros((3, side, side), np.float32))
    conv = [o for o in g0.ops if o.kind == "conv"]
    need = (max(o.layer.act_bytes for o in conv)
            + max(o.layer.out_bytes for o in conv) + 4096) * max(2, int(slots)) + (1 << 16)
    del n0, g0, _o0, _t0
    gc.collect()

    arena = BE.BinArena(need, name="run100_arena")

    from pynq import Overlay
    ov = Overlay(bit, download=False)      # ★ PL 必须已 operating：**绝不重载**
    try:
        if fclk:
            from pynq import Clocks
            Clocks.fclk0_mhz = float(fclk)
            time.sleep(0.3)
            log("[clk] 已设报数 %g → 读回报数 %.1f" % (fclk, float(Clocks.fclk0_mhz)))
    except Exception as _ex:                # noqa: BLE001
        log("[clk] 设置时钟失败（继续）：%s" % _ex)
    mhz, raw = fclk_true_mhz()
    log("[clk] FCLK0 SLCR=%s ⇒ 真值 %s MHz"
        % ("0x%08X" % raw if raw is not None else "N/A",
           "%.2f" % mhz if mhz is not None else "N/A"))

    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=pack, verbose=False)
    eng.load_weights()                      # 一次性（run() 内部不再装载）
    net = BN.BinNet(pack, engine=eng, H=side, W=side, verbose=False)
    return net, arena, need


def build_ref_net(pack: str, side: int, log=print):
    """主机参考路径：``BinNet(engine=None)`` = 纯 numpy 参考（无 PL、无 CMA、无 arena）。"""
    import bin_net as BN
    log("[net] engine=None（numpy 参考路径，无 PL）")
    return BN.BinNet(pack, engine=None, H=side, W=side, verbose=False), None, 0


# ===========================================================================
# 3. 帧尾显式释放
# ===========================================================================
def release_frame(net, outs: dict, log=print) -> dict:
    """**帧尾显式释放**：把这一帧的张量句柄全部断开，只留统计量。

    依次做四件事（都是**已有**的幂等路径，不发明新语义）：

    1. 惰性 int8 句柄（``bin_engine.C8Tensor``）：``release(force=True)`` 把钉住的
       OUT 槽还回 ``_PlBackend`` 空闲表 —— 这正是"惰性句柄跨帧钉住 OUT 槽"的解药。
    2. f32 池张量：``bin_engine.f32_release(t)``（幂等；非池张量返回 False）。
       池默认关，所以通常是 no-op，但留着是**幂等**的，不会错。
    3. 量化缓存 ``net._qcache.invalidate_all()``：槽可能被复用 ⇒ 旧量化结果必须失效
       （与 BV_FREE 判死点同一纪律，见 ``qcache.py:253`` 的 docstring）。
    4. ``outs.clear()`` + ``net._catpins.clear()`` + ``net._graph = None``：
       断掉帧内图对本帧数组的引用，否则下一帧开始前 RSS 不会降回去。

    返回计数（写进 run_meta 便于事后核对"释放路径真的走了"）。
    """
    cnt = {"c8": 0, "f32": 0, "ndarray": 0, "qc": 0, "total": 0}
    try:
        import bin_engine as BE
        C8Tensor = getattr(BE, "C8Tensor", None)
        f32_release = getattr(BE, "f32_release", None)
    except Exception:                       # noqa: BLE001  （fake 模式不 import bin_engine）
        C8Tensor, f32_release = None, None

    for _k, v in list(outs.items()):
        if C8Tensor is not None and isinstance(v, C8Tensor):
            try:
                v.release(force=True)
                cnt["c8"] += 1
            except Exception:               # noqa: BLE001
                pass
            v = None
        if isinstance(v, np.ndarray):
            cnt["ndarray"] += 1
            if f32_release is not None:
                try:
                    if f32_release(v):
                        cnt["f32"] += 1
                except Exception:           # noqa: BLE001
                    pass
        cnt["total"] += 1
    outs.clear()

    qc = getattr(net, "_qcache", None)
    if qc is not None:
        try:
            qc.invalidate_all()
            cnt["qc"] = 1
        except Exception:                   # noqa: BLE001
            pass
    catpins = getattr(net, "_catpins", None)
    if isinstance(catpins, dict):
        catpins.clear()
    try:
        net._graph = None                   # 断掉帧内图（否则张量/层包装跟着 net 活到下一帧）
    except Exception:                       # noqa: BLE001
        pass
    return cnt


# ===========================================================================
# 4. 显示（**不写寄存器**：全部走已有驱动）
# ===========================================================================
def _pl_state() -> str:
    try:
        with open(FPGA_STATE) as f:
            return f.read().strip()
    except OSError as e:
        return "?%s" % e


def _import_minimal_proof(path_arg: str | None):
    """把 ``minimal_proof.py`` 当**库**加载（能出图的那份唯一可信实现）。"""
    import importlib.util
    cands = []
    if path_arg:
        cands.append(path_arg)
    cands += [os.path.join(REPO, "runs", "demo_pairs", "minimal_proof.py"),
              "/home/xilinx/demo/minimal_proof.py",
              "/home/xilinx/demoir/ps/pynq/minimal_proof.py"]
    for p in cands:
        if p and os.path.exists(p):
            spec = importlib.util.spec_from_file_location("minimal_proof", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod, p
    return None, None


def setup_display(args, log=print):
    """返回 ``(pipe, how)``；``--no-show`` 时返回 ``(None, "disabled")``。

    * 板端（``--bringup auto|minimal_proof``）：``minimal_proof.bringup()``
      —— 已肉眼验证过的 bringup（CTRL=0x15 → 1 s → ``DisplayPipeline.setup()`` → CTRL=0x01）。
    * 板端 ``direct``：``DisplayPipeline(mode).setup()`` + ``pix_alive`` 心跳门禁。
    * 主机（``--dry-run``）：``LocalRegIo`` + ``LocalAllocator`` 注入 ⇒ **真**
      ``compose``/``show_frame``/``wait`` 代码路径在 host 上跑通（无 PL/AXI）。
    """
    if args.no_show:
        return None, "disabled(--no-show)"

    if args.dry_run:
        import demo_pipeline as dp          # 主机替身都在这里（显式注入，不是降级）
        import display_demo as _dd
        io, alloc = dp.LocalRegIo(), dp.LocalAllocator()
        pipe = _dd.DisplayPipeline(reg_io=io, mode=args.mode, allocator=alloc).setup()
        log("[disp] 主机替身：LocalRegIo + LocalAllocator ⇒ 真 DisplayPipeline "
            "（mode=%d，块 %.2f MB）" % (args.mode, pipe.block_bytes / 1e6))
        return pipe, "fake(LocalRegIo+LocalAllocator)"

    st = _pl_state()
    log("[disp] fpga0 state = %s" % st)
    if st != "operating":
        raise SystemExit("[disp] !! PL 没载位流（state=%r）⇒ 拒绝 mmap/AXI（会挂死整板）。"
                         "先按 README 载 design_1_v60.bit，再重跑。" % st)
    if args.bringup in ("auto", "minimal_proof"):
        mp, path = _import_minimal_proof(args.minimal_proof)
        if mp is not None:
            log("[disp] 用 minimal_proof（%s）的已验证 bringup" % path)
            pipe, _ctx = mp.bringup()
            return pipe, "minimal_proof(%s)" % path
        if args.bringup == "minimal_proof":
            raise SystemExit("[disp] --bringup minimal_proof 但找不到 minimal_proof.py")
        log("[disp] ⚠ 找不到 minimal_proof.py ⇒ 回落 DisplayPipeline 直连")
    import display_demo as _dd
    pipe = _dd.DisplayPipeline(mode=args.mode).setup()
    try:
        alive, mhz = pipe.pix_alive(200)
    except Exception as _ex:                # noqa: BLE001
        alive, mhz = None, 0.0
        log("[disp] ⚠ pix_alive 失败：%s" % _ex)
    log("[disp] pix 心跳=%s  ≈%.2f MHz（200 ms 窗口）" % (alive, mhz))
    if alive is False:
        raise SystemExit("[disp] !! 像素时钟没跑（0x30[0] 不翻转）⇒ 不碰 AXI")
    return pipe, "direct(DisplayPipeline.setup)"


def read_reg_2c(pipe) -> int:
    """读 ``UNDERFLOW_CNT``（0x2C）。**只读**；优先走 ``HdmiOut.underflow()``。"""
    if pipe is None:
        return -1
    try:
        out = getattr(pipe, "_out", None)
        if out is not None and hasattr(out, "underflow"):
            return int(out.underflow())
    except Exception:                       # noqa: BLE001
        pass
    try:
        return int(pipe._rd("UNDERFLOW_CNT"))
    except Exception:                       # noqa: BLE001
        return -1


def read_reg_24(pipe) -> int:
    """读 ``FRAME_ID_LATCHED``（0x24）：PL 已采纳的帧号（V-B 判据"每帧 +1"）。"""
    if pipe is None:
        return -1
    try:
        out = getattr(pipe, "_out", None)
        if out is not None and hasattr(out, "frame_latched"):
            return int(out.frame_latched())
    except Exception:                       # noqa: BLE001
        pass
    try:
        return int(pipe._rd("FRAME_ID_LATCHED"))
    except Exception:                       # noqa: BLE001
        return -1


# ===========================================================================
# 5. 看门狗
# ===========================================================================
def watchdog_check(samples: list, args) -> list:
    """看门狗：返回**中止原因列表**（空 ⇒ 继续）。

    ``samples`` 是**本帧**之前的 RSS 序列（含本帧）。两条判据：

    1. ``MemAvailable < mem_floor_mb``（默认 150 MB）—— 绝对余量；
    2. ``VmRSS`` **连续 ``rss_window`` 帧单调增长**且累计 ``> rss_growth_mb``（默认 20 MB）
       —— 抓"每帧漏一点"的慢性泄漏（726 MB 那条就是这种形状）。
    """
    reasons = []
    cur = samples[-1]
    if cur["mem_available_mb"] >= 0 and cur["mem_available_mb"] < args.mem_floor_mb:
        reasons.append("MemAvailable %.1f MB < 下限 %.1f MB"
                       % (cur["mem_available_mb"], args.mem_floor_mb))
    w = max(2, int(args.rss_window))
    if len(samples) >= w + 1:
        win = samples[-(w + 1):]
        rss = [s["rss_mb"] for s in win]
        mono = all(rss[i + 1] > rss[i] for i in range(w))
        grow = rss[-1] - rss[0]
        if mono and grow > args.rss_growth_mb:
            reasons.append("VmRSS 连续 %d 帧单调增长 %.1f MB > %.1f MB（%s ⇒ %s）"
                           % (w, grow, args.rss_growth_mb,
                              ",".join("%.1f" % r for r in rss[:-1]), "%.1f" % rss[-1]))
    return reasons


def preflight(a, engine_mode: str, log=print) -> None:
    """开跑前门禁：**每条都只读**，失败就一句话退出（不留半截状态、不碰 AXI）。

    * ``board``：PL 必须已 ``operating``（本脚本永不下载位流）+ 位流文件在 + 容器 ``index.json`` 在；
    * ``ref``  ：容器 ``index.json`` 在（numpy 参考也要层定义）；
    * ``fake`` ：无前置（纯控制流）。
    """
    if engine_mode in ("board", "ref"):
        idx = os.path.join(a.pack, "index.json")
        if not os.path.isfile(idx):
            raise SystemExit(
                "[preflight] 容器不对：找不到 %s\n"
                "            板上应指向 pack_v60（例：--pack /home/xilinx/demoir/deploy/pack_v60）" % idx)
        log("[preflight] 容器 = %s（%s）" % (a.pack, idx))
    if engine_mode == "board":
        st = _pl_state()
        if st != "operating":
            raise SystemExit(
                "[preflight] !! fpga0 state = %r（不是 operating）⇒ 拒绝 mmap/AXI（会挂死整板）。\n"
                "            先按 README 把 design_1_v60.bit 载好（run100.py 自己绝不下载位流），"
                "再重跑。" % st)
        if not os.path.isfile(a.bit):
            raise SystemExit("[preflight] 位流不存在：%s" % a.bit)
        log("[preflight] PL state = operating；位流 = %s（md5 %s）"
            % (a.bit, md5_of(a.bit)))


def write_aborted(out_dir: str, payload: dict) -> str:
    p = os.path.join(out_dir, "aborted.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    return p


# ===========================================================================
# 6. 主流程
# ===========================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="内存守纪律的 100 帧运行器（BV_FREE=1 / 单 net / 流式输入 / 帧尾释放 / 看门狗）")
    ap.add_argument("--pack", default=BOARD_PACK, help="权重容器目录（v60：pack_v60）")
    ap.add_argument("--bit", default=BOARD_BIT, help="位流（**必须先载好**；本脚本不下载）")
    ap.add_argument("--inputs", default=BOARD_INPUTS, help="输入目录（*.npy，按名排序）")
    ap.add_argument("--frames", type=int, default=100, help="总帧数（环形复用输入）")
    ap.add_argument("--shape", type=int, default=256, help="网络边长")
    ap.add_argument("--out", default=None, help="输出目录（frames.jsonl / run_meta.json）")
    ap.add_argument("--mode", type=int, default=3, help="显示模式（3=720p30 默认）")
    ap.add_argument("--slots", type=int, default=24, help="arena 槽数（板端实测 24）")
    ap.add_argument("--no-show", action="store_true", help="不上屏（只算 + 只记账）")
    ap.add_argument("--show-timeout", type=float, default=0.5, help="等 vsync 采纳的秒数")
    ap.add_argument("--dry-run", action="store_true",
                    help="主机空跑：不碰 PL/CMA/相机；显示用假寄存器替身")
    ap.add_argument("--engine", choices=("auto", "fake", "ref"), default="auto",
                    help="auto: dry-run→fake，否则板端真引擎；ref: BinNet(engine=None) numpy 参考")
    ap.add_argument("--bringup", choices=("auto", "minimal_proof", "direct"), default="auto",
                    help="板端显示初始化路径（默认优先 minimal_proof 的已验证路径）")
    ap.add_argument("--minimal-proof", default=None, help="minimal_proof.py 的显式路径")
    ap.add_argument("--fclk", type=float, default=None,
                    help="（可选）写 FCLK0 报数；只读回 SLCR 真值记账，默认不动时钟")
    # --- 看门狗阈值（可人为调小做注入测试）---
    ap.add_argument("--mem-floor-mb", type=float, default=150.0,
                    help="MemAvailable 下限（默认 150 MB）")
    ap.add_argument("--rss-growth-mb", type=float, default=20.0,
                    help="连续 rss-window 帧的总增长上限（默认 20 MB）")
    ap.add_argument("--rss-window", type=int, default=3, help="连续增长判定的帧数（默认 3）")
    # --- 注入测试旋钮（**只为主机验证**，板上不要开）---
    ap.add_argument("--test-leak-mb", type=float, default=0.0,
                    help="[测试] 每帧故意泄漏 N MB（验证看门狗真的会触发）")
    ap.add_argument("--test-0x2c-bump-at", type=int, default=0,
                    help="[测试] 在第 N 帧后让假寄存器 0x2C +1（验证 0x2C 门禁）")
    ap.add_argument("--allow-bv-free0", action="store_true",
                    help="允许 BV_FREE=0 继续跑（默认**直接退出**：那正是 100 帧跑崩的原因）")
    ap.add_argument("--no-fsync", action="store_true",
                    help="不用 os.fsync（默认每帧 fsync：崩机后日志必须还在）")
    ap.add_argument("--warmup-frames", type=int, default=1,
                    help="前 N 帧标 warmup=true（首帧含内核装载/首次缺页，计时别用）")
    a = ap.parse_args(argv)

    # ★★ 启动日志第一行：BV_FREE 的**实际取值** ★★
    bv = os.environ.get("BV_FREE", "<unset>")
    print("[run100] BV_FREE=%s  （启动第一行；默认由 os.environ.setdefault 设为 1）" % bv,
          flush=True)
    if bv not in ("1", "on", "true", "yes", "True"):
        if not a.allow_bv_free0:
            print("[run100] ✗ BV_FREE=%r ⇒ **拒绝开跑**。这条默认值就是 100 帧压死整机的"
                  "根因（396 个中间张量 726 MB 常驻 ⇒ swap ⇒ sshd fork 失败）。\n"
                  "          要明知故犯请显式加 --allow-bv-free0。" % bv, flush=True)
            return 2
        print("[run100] ⚠ BV_FREE=0 且显式 --allow-bv-free0 ⇒ 内存将不受控，计时不可用",
              flush=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = a.out or os.path.join(HERE, "logs", "run100_" + ts)
    os.makedirs(out_dir, exist_ok=True)
    jsonl_path = os.path.join(out_dir, "frames.jsonl")
    meta_path = os.path.join(out_dir, "run_meta.json")

    print("[run100] 参数：frames=%d shape=%d slots=%d dry_run=%s engine=%s out=%s"
          % (a.frames, a.shape, a.slots, a.dry_run, a.engine, out_dir), flush=True)

    # ---- 只读引用生产代码（不修改、不复制写入）----
    if PROD not in sys.path:
        sys.path.insert(0, PROD)

    engine_mode = a.engine
    if engine_mode == "auto":
        engine_mode = "fake" if a.dry_run else "board"

    # ---- 前置门禁（**在建网/碰 AXI 之前**，失败就一句话退出，不留半截状态）----
    preflight(a, engine_mode, log=print)

    # ---- 输入清单（**不预读**：只列路径）----
    files = list_inputs(a.inputs)
    print("[inputs] %d 个文件，%d 帧 ⇒ %s"
          % (len(files), a.frames,
             "环形复用" if a.frames > len(files) else "顺序一遍"), flush=True)

    # ---- 网络：**全程只建一个**（多 net 共用 24 个惰性槽会自减速）----
    t_init = time.time()
    arena = None
    need = 0
    net = None
    if engine_mode == "board":
        net, arena, need = build_board_net(a.pack, a.bit, a.shape, a.slots, a.fclk)
        print("[net] 板端 BinNet + BinArena %.2f MB（%d 槽）—— 全程复用"
              % (need / 1e6, max(2, a.slots)), flush=True)
    elif engine_mode == "ref":
        net, arena, need = build_ref_net(a.pack, a.shape)
        print("[net] 参考路径 BinNet(engine=None) —— 全程复用", flush=True)
    else:
        net = FakeNet(a.shape)
        print("[net] FakeNet（--dry-run 控制流替身；**不覆盖真引擎数值/CMA/PL**）",
              flush=True)
    init_s = time.time() - t_init
    print("[net] 建网 %.2f s" % init_s, flush=True)

    # ---- 显示 ----
    pipe, disp_how = setup_display(a, log=print)
    print("[disp] 路径 = %s" % disp_how, flush=True)

    # ---- 0x2C 基线（静置值；之后每帧读一次比较增量）----
    reg2c0 = read_reg_2c(pipe)
    reg2c_seq = []
    print("[disp] 0x2C 基线 = %s；0x24 基线 = %s" % (reg2c0, read_reg_24(pipe)),
          flush=True)

    mem0 = sample_mem()
    print("[mem] 开跑前：rss=%.1f MB  available=%.1f MB  cma=%.1f MB"
          % (mem0["rss_mb"], mem0["mem_available_mb"], mem0["cma_free_mb"]), flush=True)
    rss_samples = []
    leak_ballast = []                       # 仅 --test-leak-mb 注入用
    rows = 0
    aborted = None
    prev_start = None
    rel_cnt_total = {}
    warm = []

    meta = {"when": time.strftime("%F %T"), "argv": list(sys.argv),
            "bv_free": bv, "engine": engine_mode, "dry_run": bool(a.dry_run),
            "display": disp_how, "pack": a.pack, "bit": a.bit,
            "inputs": a.inputs, "n_inputs": len(files), "frames": a.frames,
            "shape": a.shape, "slots": a.slots, "arena_bytes": int(need),
            "arena_mb": round(need / 1e6, 2), "init_s": round(init_s, 3),
            "mem_at_start": mem0, "reg_2c_base": reg2c0,
            "env_runpoint": {k: v for k, v in sorted(os.environ.items())
                             if k.startswith(("BIN_", "Q8CACHE", "CATPACK", "BV_"))},
            "py": sys.version.split()[0], "np": np.__version__}
    if os.path.exists(a.pack) and os.path.isfile(os.path.join(a.pack, "index.json")):
        try:
            _idx = json.load(open(os.path.join(a.pack, "index.json")))["layers"]
            meta["pack_layers"] = len(_idx)
            meta["pack_round_q"] = sorted({int(l.get("round_q") or 0) for l in _idx})
            meta["pack_slope_shift"] = sorted({int(l.get("slope_shift") or 8) for l in _idx})
        except Exception as _ex:            # noqa: BLE001
            meta["pack_read_error"] = str(_ex)[:200]
    if os.path.isfile(a.bit):
        meta["bit_md5"] = md5_of(a.bit)
        meta["bit"] = a.bit
    try:
        meta["prod_md5"] = {m: md5_of(os.path.join(PROD, m))
                            for m in ("bin_net.py", "bin_engine.py")
                            if os.path.exists(os.path.join(PROD, m))}
    except Exception:                       # noqa: BLE001
        pass
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    # ---- 主循环：一帧一读一算一显示一记账 ----
    logf = open(jsonl_path, "a", encoding="utf-8", buffering=1)   # 行缓冲（要求的最小档）
    try:
        for i in range(int(a.frames)):
            t_start = time.monotonic()
            interval_ms = 0.0 if prev_start is None else (t_start - prev_start) * 1e3
            prev_start = t_start
            path = files[i % len(files)]

            # ① 按需读一张（**用完即弃**，绝不 x0_all 预载 100 张）
            x, raw = load_input(path, a.shape)
            t1 = time.monotonic()

            # ② 算一张
            if arena is not None:
                arena.reset()
            outs, st = net.run(x, pipeline=True, nbuf=2, arena=arena)
            t2 = time.monotonic()

            # ③ 只留统计量（**不把 numpy 累积进列表**）
            out1 = np.asarray(outs["out1"], dtype=np.float32)
            u8 = to_u8_chw(out1)                      # (3,H,W) uint8
            img_hwc = to_hwc(u8)                      # (H,W,3) uint8
            out_sha1 = sha1_of(img_hwc)
            out_std = float(np.asarray(out1, np.float64).std())
            nb = nonblack_frac(img_hwc)
            proc_disp = _fit_for_display(img_hwc)
            orig_disp = _fit_for_display(to_hwc(to_u8_chw(x)))
            del out1, img_hwc
            rel = release_frame(net, outs, log=print)
            for k, v in rel.items():
                rel_cnt_total[k] = rel_cnt_total.get(k, 0) + v
            del outs, u8
            del x
            t3 = time.monotonic()

            # ④ 上屏：compose + show + wait（**不自己写寄存器**）
            show_ms, fid, latched = 0.0, -1, None
            if pipe is not None:
                fid = pipe.show(orig_disp, proc_disp)
                latched = pipe.wait(fid, a.show_timeout)
                show_ms = (time.monotonic() - t3) * 1e3
            del orig_disp, proc_disp

            # ⑤ 0x2C / 0x24 / 内存（每帧一次）
            reg2c = read_reg_2c(pipe)
            if reg2c >= 0:
                reg2c_seq.append(int(reg2c))
            reg24 = read_reg_24(pipe)
            if a.test_0x2c_bump_at and i + 1 == a.test_0x2c_bump_at:
                # [测试] 让假寄存器欠载计数 +1（真板不需要这个旋钮）
                io = getattr(pipe, "_io", None) if pipe is not None else None
                if io is not None and hasattr(io, "stall"):
                    io.stall(1)
                    print("[run100][TEST] 已注入 0x2C +1（第 %d 帧后）" % (i + 1), flush=True)
            mem = sample_mem()
            rss_samples.append(mem)
            if a.test_leak_mb > 0:
                leak_ballast.append(np.zeros(int(a.test_leak_mb * 1e6), np.uint8))

            row = {
                "frame": i,
                "in_sha1": raw["in_sha1"],
                "out_sha1": out_sha1,
                "out_std": round(out_std, 6),
                "nonblack": round(nb, 6),
                "interval_ms": round(interval_ms, 2),
                "run_ms": round((t2 - t1) * 1e3, 2),
                "post_ms": round((t3 - t2) * 1e3, 2),
                "show_ms": round(show_ms, 2),
                "rss_mb": mem["rss_mb"],
                "mem_available_mb": mem["mem_available_mb"],
                "cma_free_mb": mem["cma_free_mb"],
                "reg_2c": int(reg2c),
                "reg_24": int(reg24),
                # —— 以下为附加字段（分析用；README 有说明）——
                "warmup": bool(i < a.warmup_frames),
                "in_file": raw["file"],
                "fid": int(fid),
                "latched": latched,
                "rel_c8": rel["c8"],
            }
            logf.write(json.dumps(row, ensure_ascii=False) + "\n")
            logf.flush()
            if not a.no_fsync:
                os.fsync(logf.fileno())
            rows += 1
            warm.append(row)

            print("[frame %3d/%d] %-24s run=%8.1f ms post=%6.1f ms show=%5.1f ms "
                  "rss=%7.1f MB avail=%7.1f MB 0x2C=%d 0x24=%d nb=%.3f out=%s%s"
                  % (i + 1, a.frames, raw["file"], row["run_ms"], row["post_ms"],
                     row["show_ms"], row["rss_mb"], row["mem_available_mb"],
                     reg2c, reg24, nb, out_sha1[:12],
                     "  (warmup)" if row["warmup"] else ""), flush=True)

            # ⑥ 0x2C 门禁：**增量 > 0 ⇒ 立即停**
            reasons = []
            if reg2c >= 0 and reg2c0 >= 0 and reg2c > reg2c0:
                reasons.append("0x2C 增量 %d（基线 %d ⇒ 现在 %d）> 0：行缓冲被读空"
                               % (reg2c - reg2c0, reg2c0, reg2c))
            # ⑦ 内存看门狗
            reasons += watchdog_check(rss_samples, a)

            if reasons:
                aborted = {
                    "reason": "；".join(reasons),
                    "reasons": reasons,
                    "frame": i,
                    "frames_done": rows,
                    "when": time.strftime("%F %T"),
                    "thresholds": {"mem_floor_mb": a.mem_floor_mb,
                                   "rss_growth_mb": a.rss_growth_mb,
                                   "rss_window": a.rss_window},
                    "last3": rss_samples[-3:],
                    "mem_at_start": mem0,
                    "last_rows": warm[-3:],
                    "reg_2c_base": reg2c0,
                    "reg_2c_seq": reg2c_seq,
                    "release_counts": rel_cnt_total,
                    "jsonl": jsonl_path,
                    "argv": list(sys.argv),
                }
                p = write_aborted(out_dir, aborted)
                print("[run100] ✗ 中止于第 %d 帧：%s" % (i + 1, aborted["reason"]),
                      flush=True)
                print("[run100] → %s" % p, flush=True)
                break
        del logf
    finally:
        try:
            logf.close()
        except Exception:                   # noqa: BLE001
            pass

    # ---- 收尾：内存曲线 + 结论 ----
    if rss_samples:
        rss = [m["rss_mb"] for m in rss_samples]
        avail = [m["mem_available_mb"] for m in rss_samples]
        print("[mem] 末态：rss=%.1f MB  available=%.1f MB  cma=%.1f MB"
              % (rss[-1], avail[-1], rss_samples[-1]["cma_free_mb"]), flush=True)
        print("[mem] VmRSS：首帧 %.1f → 末帧 %.1f（Δ %+.1f MB，max %.1f，min %.1f）"
              % (rss[0], rss[-1], rss[-1] - rss[0], max(rss), min(rss)), flush=True)
        print("[mem] MemAvailable：首帧 %.1f → 末帧 %.1f（Δ %+.1f MB）"
              % (avail[0], avail[-1], avail[-1] - avail[0]), flush=True)
        if reg2c >= 0 and reg2c0 >= 0:
            print("[disp] 0x2C：基线 %d → 末帧 %d（增量 %d）; 0x24 末值 %s"
                  % (reg2c0, reg2c, reg2c - reg2c0, reg24), flush=True)
    summary = {"frames_done": rows, "aborted": bool(aborted), "aborted_file":
               os.path.join(out_dir, "aborted.json") if aborted else None,
               "jsonl": jsonl_path, "release_counts": rel_cnt_total,
               "rss_first_mb": rss_samples[0]["rss_mb"] if rss_samples else None,
               "rss_last_mb": rss_samples[-1]["rss_mb"] if rss_samples else None,
               "rss_delta_mb": (round(rss_samples[-1]["rss_mb"] - rss_samples[0]["rss_mb"], 2)
                                if rss_samples else None),
               "reg_2c_base": reg2c0, "reg_2c_last": reg2c,
               "reg_2c_delta": (reg2c - reg2c0) if (reg2c >= 0 and reg2c0 >= 0) else None}
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print("[run100] 完成 %d/%d 帧%s → %s"
          % (rows, a.frames, "（**已中止**）" if aborted else "", jsonl_path), flush=True)
    if aborted:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
