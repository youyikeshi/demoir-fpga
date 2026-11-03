"""一次完整流程：拍照 → v57 去摩尔纹 → 构图 → HDMI 双缓冲换帧。

权威出处（**不要在这里发明语义**）：协议 ``docs/camera/07_端到端流水线_协议与模块图.md``；
构图/显示 ``docs/camera/03``、``05``；开关 ``06_REPRODUCE.md`` §3.4（9 条）+ ``--slots 24``
（见 ``RUNPOINT``）；引擎调用照抄 ``runs/ACCEPT_v57/05_PS/`` 与 ``board_v39_verify.run_net()``。

运行点（``runs/ACCEPT_v57/README.md`` 首行 + ``07_KNOWN_LIMITS.md``）：**v57 = 75 MHz**，
``Clocks.fclk0_mhz = 75``。**不要**套 HDMI 位流的"报数 = 真值 1.5×"口径（IO PLL 不同：
HDMI 1000 MHz vs v57 1800 MHz / DIV0=4 / DIV1=3）；100 MHz sha1 0/7、150 MHz −3.07 ⇒ 只有 75。

合并时钟树（``07_合并方案`` §1.1/§1.2）：HDMI 的 AXI/扫描器共用 FCLK0（实跑 75）；**像素时钟
由板上 50 MHz 晶振**经 MMCM（VCO 1187.5）派生、与 FCLK0 **解耦** ⇒ 可独立校验，但**不能**
用它反推 ACLK。

阶段（打印成**一行**日志）：capture ``cam.shot()`` 0.1~0.5 s ｜ compute ``BinNet.run(x0,
pipeline=True, nbuf=2, arena)`` **≈6.2 s @75 MHz** ｜ compose ``compose(orig, proc, W, H)``
<0.05 s ｜ display ``show_frame()+wait()`` <0.05 s ⇒ 一轮 ≈ **6.3 s**（256² 墙钟实测
6.198~6.234 s @75 MHz；v39 @150 MHz 曾是 5.25~5.32 s）。

门禁：**先**比 ``sha1(ascontiguousarray(out1).tobytes())[:12]`` 与冻结基线（§3.5），**再**
``clip→transpose→×255→round→uint8`` 转图；sha1 不符抛 ``Sha1Mismatch``。不静默降级：引擎失败
/ sha1 不符 / 换帧未被采纳 / 像素时钟不对 / 位流缺 HDMI 从设备 ⇒ 抛（不回退 numpy）。板端待验：
① fclk=75 的读回与真实 ACLK（只打印、不换算）；② 合并位流 v58 未产出 ⇒ 缺 HDMI 从设备时
``setup()`` 在碰 ``0x400A0000`` 前就报错；③ 像素时钟 37.109 MHz 应与设计差 <1%。

``--fake`` 用本地图 + ``FakeV57Net`` + 假 reg_io + 本地内存块走**同一条** ``DisplayPipeline``
路径（假引擎**显式注入**），跳过 sha1 但日志写 ``sha1_gate=skipped(fake)``。用法::

    python3 demo_pipeline.py --fake                      # 主机自检（不需要板子）
    python3 demo_pipeline.py --runs 3 --json out.json    # 板端（256² / 720p30 / fclk=75）
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from display_layout import compose          # noqa: E402  板端已验证，只调用
import display_demo as dd                   # noqa: E402  只调用（新增 show_frame 供分段计时）
import hdmi_out as ho                       # noqa: E402  自带 REG/MODES/假 reg_io

__all__ = ["RUNPOINT", "RUNPOINT_EFFECTIVE", "apply_runpoint", "Pipeline", "PipelineError",
           "Sha1Mismatch", "FakeV57Net", "FakeCam", "LocalAllocator", "LocalBlock", "LocalRegIo",
           "build_v57_net", "to_uint8", "to_float_tensor", "sha1_prefix"]

SIDE = 256                                  # v57 输入 / compose 输入的唯一边长
DISPLAY_WAIT_S = 0.5                        # 等 PL 在 vsync 采纳（720p30 一帧 33 ms）
FCLK_TARGET = 75.0                          # ← v57 最终运行点（写 75 就是 75）
DEFAULT_BIT = "/home/xilinx/demoir/design_1_150v57.bit"   # 合并位流出来后换成 v58
PACK_DEFAULT = "/home/xilinx/demoir/deploy/bin_weights_new"

#: 冻结 sha1（``06_REPRODUCE.md`` §3.5）：out1/out2/out3 的**前 12 位** hex。
FROZEN_SHA1 = {(256, 256): ("9b1940c8", "f86a7035", "025757a8"),
               (128, 128): ("27e25cf6", "b82b83f3", "6db4e9bb"),
               (64, 64): ("bfa246d3", "226bd39e", "762d84b9")}
#: v57 定版 PS 代码的 md5（``06_REPRODUCE.md`` §3.3）—— 不是这两份就别谈冻结 sha1。
CODE_MD5 = {"bin_net.py": "0990948de490eb6ba6d4aaefbcafa355",
            "bin_engine.py": "2fd3d0990c47151fe3bc06544b8adb64"}

# 运行点：**单一常量块**（权威出处 ``runs/ACCEPT_v57/06_REPRODUCE.md`` §3.4）
RUNPOINT = {
    # §3.4 的 9 条，逐字照抄；少一条就会偏离冻结 sha1
    "BIN_PACK": "e1e2", "BIN_LAZY_DEQ": "1", "BIN_CAT_FUSION": "1",
    "BIN_BILINEAR_C": "1", "BIN_PRECOMP": "1",
    # ★ Q8CACHE=1 是 **v57 档**；=0 是 v39 档（board_v39_verify 的旧写法），别抄错
    "Q8CACHE": "1",
    "BIN_F32DIRECT": "1", "BIN_CAT_ECA_FUSE": "1", "BIN_EPI_NOINV": "1",
    # 第 10 条：惰性槽数（§3.4 未列，属驱动参数；来自 `board_v39_verify --slots 24`）
    "BIN_LAZY_SLOTS": "24",
}


def apply_runpoint() -> dict:
    """把 ``RUNPOINT`` 写进 ``os.environ``，返回**实际生效值**与现场多出来的开关。

    ★ 必须在 ``import bin_net`` / ``import bin_engine`` **之前**调用：有开关是在**模块导入
    时**读走的（如 ``_fast_eca_env`` / ``_LAZY_CACHE_ON``），导入后再设会静默不生效。
    现场残留的其它 ``BIN_*/Q8CACHE*/CATPACK*`` 不改动，但记录并打进日志（它们能改数值，
    sha1 门禁是最后一道防线）。
    """
    os.environ.update(RUNPOINT)
    return {"switches": {k: os.environ.get(k, "") for k in RUNPOINT},
            "stray": {k: v for k, v in os.environ.items()
                      if k.startswith(("BIN_", "Q8CACHE", "CATPACK")) and k not in RUNPOINT}}


RUNPOINT_EFFECTIVE = apply_runpoint()       # ← 先于任何 bin_net/bin_engine 导入


class PipelineError(RuntimeError):
    """流水线层面的失败（时钟不对 / 换帧没采纳 / 形状不符 / 位流缺 IP …）。"""


class Sha1Mismatch(PipelineError):
    """引擎输出与冻结 sha1 不符 —— **数值错了**，不许显示。"""


def sha1_prefix(a) -> str:
    """与 ``board_v39_verify.sha1_of()`` 同口径：C 连续原始字节的 sha1 前 12 位。"""
    return hashlib.sha1(np.ascontiguousarray(np.asarray(a)).tobytes()).hexdigest()[:12]


def _md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _md5_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def to_float_tensor(img: np.ndarray) -> np.ndarray:
    """``(256,256,3) uint8`` RGB → ``(3,256,256) float32 ∈[0,1]``（v57 的输入口径）。"""
    a = np.asarray(img)
    if a.shape != (SIDE, SIDE, 3) or a.dtype != np.uint8:
        raise ValueError(f"相机帧必须是 ({SIDE},{SIDE},3) uint8，得到 {a.shape}/{a.dtype}")
    return np.ascontiguousarray(a.transpose(2, 0, 1), dtype=np.float32) / np.float32(255.0)


def to_uint8(out: np.ndarray) -> np.ndarray:
    """``(3,H,W) float32`` → ``(H,W,3) uint8``：``clip(0,1)→transpose(1,2,0)→×255→round``；
    口径照 ``tools/infer_dir_bin.py``；形状不符直接抛（不 reshape）。"""
    a = np.asarray(out, dtype=np.float32)
    if a.ndim != 3 or a.shape[0] != 3:
        raise PipelineError(f"引擎输出必须是 (3,H,W)，得到 {a.shape}；本函数不 reshape")
    return (np.clip(a, 0.0, 1.0).transpose(1, 2, 0) * np.float32(255.0)).round().astype(np.uint8)


# 主机替身（**显式注入**才生效，绝不是"引擎失败就回落"）
class LocalBlock(np.ndarray):
    """``pynq.allocate`` 的主机替身：ndarray 子类 + ``device_address`` + ``flush()``。"""

    def __new__(cls, nbytes: int, addr: int = 0x1F00_0000):
        o = np.zeros(int(nbytes), dtype=np.uint8).view(cls)
        o.device_address = int(addr)
        return o

    def flush(self) -> None:
        pass


class LocalAllocator:
    """``allocator=nbytes -> block`` 的最小实现（每块间隔 16 MB，天然 64 B 对齐）。"""

    def __init__(self, base: int = 0x1F00_0000):
        self.base, self.blocks = int(base), []

    def __call__(self, nbytes: int) -> LocalBlock:
        blk = LocalBlock(nbytes, self.base + len(self.blocks) * 0x0010_0000)
        self.blocks.append(blk)
        return blk

class LocalRegIo(ho.FakeRegIo):
    """假寄存器堆：写 ``FRAME_ID`` 时立刻模拟一次 vsync 采纳（主机没有真 PL）。"""

    def write32(self, off: int, val: int) -> None:
        super().write32(off, val)
        if (int(off) & ~3) == ho.REG["FRAME_ID"]:
            self.vsync_edge()


class FakeCam:
    """``--fake`` 用的本地相机：给一张可复算的图，不连网络。"""

    def __init__(self, image: np.ndarray | None = None):
        if image is None:                     # 三通道互不相同 ⇒ 通道错位会立刻暴露
            y, x = np.mgrid[0:SIDE, 0:SIDE]
            image = np.stack([x * 7 & 0xFF, y * 5 & 0xFF, (x * 3 + y * 11) & 0xFF],
                             -1).astype(np.uint8)
        self.image, self.shots = np.asarray(image, dtype=np.uint8), 0

    def shot(self, w: int = SIDE, h: int = SIDE, crop="center", exp_us=None, gain=None):
        if (int(w), int(h)) != (self.image.shape[1], self.image.shape[0]):
            raise ValueError(f"FakeCam 只备了 {self.image.shape[:2]} 的图，请求 {w}x{h}")
        self.shots += 1
        return self.image.copy()


class FakeV57Net:
    """v57 引擎的主机替身：接口与 ``BinNet`` 一致，输出确定性 ``y = x/2 + 0.25``；形状/量纲按
    真引擎口径 ``{"out1": (3,H,W) float32 ∈[0,1], out2, out3}``。**必须显式注入**；真引擎
    报错时**不会**走到这里（不许当成降级路径）。"""

    def run(self, x0, pipeline=True, nbuf=2, arena=None):
        x = np.asarray(x0, dtype=np.float32)
        out1 = np.clip(x * np.float32(0.5) + np.float32(0.25), 0.0, 1.0)
        return ({"out1": out1, "out2": out1[:, ::2, ::2], "out3": out1[:, ::4, ::4]},
                {"fake": True, "total": 0.0, "pl": 0.0, "ps": 0.0})


# 板端：构造 v57 引擎（照抄 runs/ACCEPT_v57/05_PS + board_v39_verify.run_net/make_arena）
def build_v57_net(overlay, pack_dir: str = PACK_DEFAULT, slots: int = 24,
                  verbose: bool = False):
    """返回 ``(net, arena)``，与 ``board_v39_verify.run_net()`` 逐行对应：

    - arena ``(max_in + max_out + 4096) * max(2, slots) + 64 KiB``（同一公式）；
    - ``BinConvEngine(overlay, unit_id=0)`` + ``load_weights()``；基址**从 hwh 解析、不硬编码**
      （v57 hwh：``bin_unit_0=0x4006_0000``、``conv_uni_0=0x4004_0000``，与 §3.2 一致）；
    - 跑法 ``net.run(x0, pipeline=True, nbuf=2, arena=arena)``；``load_kernels()`` 先把一次性
      内核装载做掉，别算进首跑计时。
    """
    import bin_engine as BE                                        # noqa: E402
    import bin_net as BN                                           # noqa: E402

    BE.load_kernels()
    n0 = BN.BinNet(pack_dir, engine=None, H=SIDE, W=SIDE, verbose=False)
    g, _o, _t = n0.build_graph(np.zeros((3, SIDE, SIDE), np.float32))
    conv = [o for o in g.ops if o.kind == "conv"]
    need = (max(o.layer.act_bytes for o in conv)
            + max(o.layer.out_bytes for o in conv) + 4096) * max(2, int(slots)) + (1 << 16)
    del n0, g
    gc.collect()

    arena = BE.BinArena(need, name="pipeline_arena")
    eng = BE.BinConvEngine(overlay, unit_id=0, pack_dir=pack_dir, verbose=verbose)
    eng.load_weights()
    return BN.BinNet(pack_dir, engine=eng, H=SIDE, W=SIDE, verbose=False), arena


# ---- 主编排 ----
class Pipeline:
    """``shot → v57 引擎 → compose → 双缓冲换帧``；``net``/``cam``/``disp`` 全部可注入。"""

    def __init__(self, net=None, cam=None, disp=None, *, fake: bool = False,
                 mode: int = dd.DEFAULT_MODE, slots: int = 24, bit: str = DEFAULT_BIT,
                 pack_dir: str = PACK_DEFAULT, overlay=None, verbose: bool = False,
                 log=print, strict_code: bool = True, pix_tol: float = 0.01):
        self.fake, self.mode, self.slots = bool(fake), int(mode), int(slots)
        self.bit, self.pack_dir, self.verbose = bit, pack_dir, bool(verbose)
        self.log, self.strict_code, self.pix_tol = log, bool(strict_code), float(pix_tol)
        self._net, self._cam, self._disp = net, cam, disp
        self._overlay_obj, self._io, self._owns_io = overlay, None, False
        self._alloc = self._arena = None
        self._ready = False
        self._fclk = (FCLK_TARGET, None)     # (目标, 读回)
        self._pix = (None, None)             # (心跳活?, 实测 MHz)
        self._code_md5, self._frame_id, self.runs = {}, 0, 0

    # ---------------- 生命周期 ----------------
    def setup(self) -> "Pipeline":
        if self._ready:
            return self
        if self.fake:
            self._ensure_fake()
        else:
            self._ensure_overlay()           # ① 先 load：load 之前 ZOCL 无上下文
            self._gate_code_md5()            # ② 必须是 v57 定版 PS 代码（§3.3）
            self._gate_has_hdmi()            # ③ 位流没 HDMI 从设备就别碰 0x400A0000
            self._ensure_io()
            self._set_fclk()                 # ④ 写 75（运行点），打印目标/读回
            self._hb_pregate()               # ⑤ 先读 0x30 心跳，再碰其它寄存器（05 §8.2）
        self._disp.setup()
        if not self.fake:
            self._pix_gate()                 # ⑥ 50 MHz 晶振链的像素时钟要对得上设计值
        if self._cam is None:
            from cam_client import CamClient
            self._cam = CamClient()
        if self._net is None:
            self._net, self._arena = build_v57_net(self._overlay_obj, self.pack_dir,
                                                   self.slots, self.verbose)
        self._ready = True
        self.log(self.runpoint_line("setup"))
        return self

    def _ensure_fake(self) -> None:
        self._cam = self._cam if self._cam is not None else FakeCam()
        self._net = self._net if self._net is not None else FakeV57Net()
        if self._disp is None:
            self._alloc, self._io = LocalAllocator(), LocalRegIo()
            self._disp = dd.DisplayPipeline(reg_io=self._io, mode=self.mode,
                                            allocator=self._alloc)
    def _ensure_overlay(self) -> None:
        if self._overlay_obj is None:
            from pynq import Overlay                              # 只有板端有
            self._overlay_obj = Overlay(self.bit)
    def _gate_has_hdmi(self) -> None:
        """位流里必须有 HDMI 从设备；否则写 ``0x400A0000`` 会挂死总线（05 §8.1 同类事故）。"""
        ips = getattr(self._overlay_obj, "ip_dict", None)
        if ips is None:
            return                            # 调用方自己给了 disp/io，不归我们判
        if not [k for k in ips if "hdmi" in str(k).lower()]:
            raise PipelineError(
                f"位流里找不到 HDMI 从设备（ip_dict 无 *hdmi*）：{sorted(ips)[:8]}… ⇒ "
                f"合并位流（07_合并方案 的 v58）还没出？不许对不存在的 0x400A0000 写寄存器")
    def _ensure_io(self) -> None:                                 # noqa: E301
        if self._io is None:
            self._io, self._owns_io = ho.MmapRegIo(ho.DEFAULT_BASE), True
        if self._disp is None:
            self._disp = dd.DisplayPipeline(reg_io=self._io, mode=self.mode)
    def _set_fclk(self) -> None:
        """运行点 = **75 MHz**（``runs/ACCEPT_v57/README.md`` 首行）。

        ★ 写 75 就是 75：**不要**套 HDMI 位流的"报数 = 真值 1.5×"口径（IO PLL 不同）；
        ``06_REPRODUCE §3.1`` 的 ``assert abs(...-100)<1`` 已作废。只打印目标与读回并要求
        读回≈目标；真实 ACLK 无独立探针（像素时钟与 ACLK 解耦）⇒ 数值由 sha1 门禁裁决。
        """
        from pynq import Clocks
        Clocks.fclk0_mhz = FCLK_TARGET
        time.sleep(0.2)
        self._fclk = (FCLK_TARGET, float(Clocks.fclk0_mhz))
        if abs(self._fclk[1] - FCLK_TARGET) > 0.5:
            raise PipelineError(
                f"fclk0 没设上：目标 {FCLK_TARGET:g} MHz，读回 {self._fclk[1]:g} MHz ⇒ 停")
    def _hb_pregate(self, window_ms: float = 100.0, min_edges: int = 5) -> None:
        """05 §8.2：**任何其它 AXI 访问之前**先读 ``0x30`` 心跳；不动就立刻停。"""
        t_end, last, edges = time.monotonic() + window_ms / 1000.0, None, 0
        while time.monotonic() < t_end:                    # 有界窗口内数翻转沿
            v = int(self._io.read32(ho.REG["PIX_HB"])) & 1
            edges += int(last is not None and v != last)
            last = v
        if edges < min_edges:
            raise PipelineError(
                f"0x30 心跳 {window_ms:g} ms 内只有 {edges} 个沿（要求 ≥{min_edges}）"
                f" ⇒ 像素时钟没跑；按 05 §8.2 立刻停，不再做任何 AXI 访问")
    def _pix_gate(self, window_ms: float = 2000.0) -> None:
        """像素时钟由 50 MHz 晶振经 MMCM 精确派生（合并方案 §1.2）⇒ 可独立校验；2 s 窗口
        精度 ≈±0.09%（05 §8.13）。它与 FCLK0 **解耦**，所以**不能**反推 ACLK。"""
        want = ho.MODES[self.mode].pix_clk_mhz
        alive, mhz = self._disp.pix_alive(window_ms)
        self._pix = (bool(alive), float(mhz))
        if not alive:
            raise PipelineError("像素时钟心跳 <5 沿 ⇒ 立刻停（05 §8.2），不许跑整网")
        if want > 0 and abs(mhz / want - 1.0) > self.pix_tol:
            raise PipelineError(
                f"像素时钟实测 {mhz:.3f} MHz / 设计 {want:.3f} MHz 超出 ±{self.pix_tol:.1%}"
                f" ⇒ 显示时钟树不对（50 MHz 晶振 Y14 / MMCM M=23.75？见合并方案 §1.2），停")
    def _gate_code_md5(self) -> None:
        for name, want in CODE_MD5.items():
            path = os.path.join(_HERE, name)
            got = _md5_file(path) if os.path.exists(path) else "missing"
            self._code_md5[name] = got
            if got != want:
                raise PipelineError(f"{name} md5={got} != v57 定版 {want}"
                                    f"（06_REPRODUCE §3.3）⇒ 冻结 sha1 无意义，拒绝继续")
    def close(self) -> None:
        if self._disp is not None:
            try:
                self._disp.close()
            except Exception:                                  # noqa: BLE001
                pass
            self._disp = None
        if self._owns_io and hasattr(self._io, "close"):
            self._io.close()
        self._io, self._owns_io, self._ready, self._arena = None, False, False, None
        gc.collect()

    # ---------------- 运行点日志 ----------------
    def runpoint_line(self, phase: str = "setup") -> str:
        """一行打印**实际生效**的运行点：10 条开关 + fclk 目标/读回（+ 实测像素时钟）。"""
        rp = ",".join(f"{k}={v}" for k, v in RUNPOINT_EFFECTIVE["switches"].items())
        tg, rb = self._fclk
        mhz, want = self._pix[1], ho.MODES[self.mode].pix_clk_mhz
        fclk_s = "fake" if self.fake else f"{tg:g}->{'?' if rb is None else format(rb, 'g')}"
        pix_s = "fake" if self.fake else ("?" if mhz is None else
                                          f"{mhz:.3f}MHz(design{want:.3f})")
        stray = ",".join(f"{k}={v}" for k, v in sorted(RUNPOINT_EFFECTIVE["stray"].items())) or "-"
        md5s = ",".join(f"{k}={v[:8]}" for k, v in sorted(self._code_md5.items())) or "-"
        return (f"[PIPE] runpoint phase={phase} fake={int(self.fake)} mode={self.mode} "
                f"slots={self.slots} fclk={fclk_s} pix={pix_s} ps_md5={md5s} "
                f"switches[{len(RUNPOINT)}]={rp} stray={stray}")

    # ---------------- 主流程 ----------------
    def run_once(self, w: int = 256, h: int = 256) -> dict:
        """跑一轮：采集 → 计算 → 构图 → 换帧；返回本轮统计字典（含各阶段计时）。"""
        self.setup()
        if (int(w), int(h)) != (SIDE, SIDE):
            raise ValueError(f"compose() 只接受 {SIDE}x{SIDE}（docs/camera/03 §3，**不许 "
                             f"resize**），得到 {w}x{h}")
        geom = ho.MODES[self.mode]
        t_all = time.perf_counter()

        t0 = time.perf_counter()                                     # ① capture
        img = self._cam.shot(int(w), int(h))
        t_cap = time.perf_counter() - t0
        if not isinstance(img, np.ndarray) or img.shape != (SIDE, SIDE, 3) or img.dtype != np.uint8:
            raise PipelineError(f"相机帧不合契约：{getattr(img, 'shape', None)}/"
                                f"{getattr(img, 'dtype', None)}")

        x0 = to_float_tensor(img)                                    # ② compute
        t0 = time.perf_counter()
        outs, net_stats = self._net.run(x0, pipeline=True, nbuf=2, arena=self._arena)
        t_cmp = time.perf_counter() - t0
        miss = [k for k in ("out1", "out2", "out3") if k not in outs]
        if miss:                       # 三个都必须在：少一个就等于少一道冻结判据
            raise PipelineError(f"引擎没返回 {miss}（键：{sorted(outs)}）")
        # ★ 门禁顺序：**先**比 sha1（原始 float32），**再**转图
        sha1s = tuple(sha1_prefix(outs[k]) for k in ("out1", "out2", "out3"))
        gate = self._gate_sha1(sha1s, int(w), int(h))
        proc = to_uint8(outs["out1"])

        t0 = time.perf_counter()                                     # ③ compose
        frame = compose(img, proc, geom.W, geom.H)
        t_cps = time.perf_counter() - t0

        t0 = time.perf_counter()                                     # ④ display
        fid = self._disp.show_frame(frame)                           # 先写后台块
        latched = bool(self._disp.wait(fid, timeout=DISPLAY_WAIT_S))  # 再等 vsync 采纳
        t_dsp = time.perf_counter() - t0
        if not latched:
            raise PipelineError(
                f"FRAME_ID={fid} 在 {DISPLAY_WAIT_S}s 内没被 PL 在 vsync 采纳 ⇒ "
                f"画面还停在旧帧（不静默降级，直接抛）")
        self._frame_id, self.runs = int(fid), self.runs + 1

        t_tot = time.perf_counter() - t_all
        m = [_md5_bytes(x.tobytes())[:12] for x in (img, proc, frame)]
        line = (f"[PIPE] stage capture={t_cap:.3f} compute={t_cmp:.3f} compose={t_cps:.3f} "
                f"display={t_dsp:.3f} total={t_tot:.3f} s | frame_id={fid} latched={int(latched)}"
                f" | md5 orig={m[0]} proc={m[1]} frame={m[2]} | sha1_gate={gate} "
                f"out1={sha1s[0] if sha1s else '-'} | runpoint fake={int(self.fake)} "
                f"switches={len(RUNPOINT)} q8cache={RUNPOINT['Q8CACHE']} slots={self.slots} "
                f"fclk={'fake' if self.fake else '%g->%g' % self._fclk} | "
                f"{geom.name} {geom.W}x{geom.H}")
        self.log(line)

        return {"w": int(w), "h": int(h), "mode": self.mode, "mode_name": geom.name,
                "frame_id": int(fid), "latched": latched, "fake": self.fake,
                "t_capture_s": round(t_cap, 6), "t_compute_s": round(t_cmp, 6),
                "t_compose_s": round(t_cps, 6), "t_display_s": round(t_dsp, 6),
                "t_total_s": round(t_tot, 6), "sha1_gate": gate, "log_line": line,
                "sha1_out1": sha1s[0] if sha1s else None, "net_stats": net_stats,
                "md5_orig": _md5_bytes(img.tobytes()), "md5_proc": _md5_bytes(proc.tobytes()),
                "md5_frame": _md5_bytes(frame.tobytes()),
                "runpoint": {"switches": dict(RUNPOINT_EFFECTIVE["switches"]),
                             "stray": dict(RUNPOINT_EFFECTIVE["stray"]),
                             "slots": self.slots}}

    def _gate_sha1(self, sha1s, w: int, h: int) -> str:
        """冻结 sha1 门禁。返回 ``"ok"`` / ``"skipped(fake)"``（两种都写进日志）。"""
        if self.fake:
            return "skipped(fake)"
        exp = FROZEN_SHA1.get((w, h))
        if exp is None:
            raise PipelineError(f"{w}x{h} 没有冻结 sha1 基线（06_REPRODUCE §3.5 只有 "
                                f"64/128/256）⇒ 拒绝在无门禁下跑整网")
        bad = [(k, g, e) for k, g, e in zip(("out1", "out2", "out3"), sha1s, exp) if g != e]
        if bad:
            raise Sha1Mismatch("引擎输出与冻结 sha1 不符（**数值错了**，图也是废的，不送显示）："
                               + "; ".join(f"{k} {g} != {e}" for k, g, e in bad))
        return "ok"

    def stats(self) -> dict:
        return {"fake": self.fake, "mode": self.mode, "runs": self.runs,
                "frame_id": self._frame_id,
                "fclk": {"target": self._fclk[0], "readback": self._fclk[1]},
                "pix": {"alive": self._pix[0], "mhz": self._pix[1]},
                "code_md5": dict(self._code_md5),
                "runpoint": dict(RUNPOINT_EFFECTIVE["switches"])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="拍照 → 去摩尔纹 → 构图 → HDMI 一次完整流程")
    ap.add_argument("--fake", action="store_true",
                    help="主机自检：本地图 + 假 reg_io + 显式注入的假引擎（不连相机/硬件）")
    ap.add_argument("--w", type=int, default=SIDE)
    ap.add_argument("--h", type=int, default=SIDE)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--mode", type=int, default=dd.DEFAULT_MODE,
                    help="显示模式（3=720p30 默认 / 0=720p60 / 1=1080p60 / 2=1080p30）")
    ap.add_argument("--bit", default=DEFAULT_BIT)
    ap.add_argument("--pack", default=PACK_DEFAULT, help="权重包目录")
    ap.add_argument("--slots", type=int, default=24, help="惰性槽数（实测 24）")
    ap.add_argument("--json", default="", help="把各轮统计写到这个 json")
    a = ap.parse_args(argv)

    pipe = Pipeline(fake=a.fake, mode=a.mode, slots=a.slots, bit=a.bit, pack_dir=a.pack)
    rows = []
    try:
        for _ in range(max(1, a.runs)):
            rows.append(pipe.run_once(a.w, a.h))
    finally:
        stats = pipe.stats() if rows else None
        pipe.close()

    t = [r["t_total_s"] for r in rows]
    print(f"[PIPE] done runs={len(rows)} total_min={min(t):.3f} total_max={max(t):.3f} s "
          f"frame_id={rows[-1]['frame_id']} sha1_gate={rows[-1]['sha1_gate']}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"runs": rows, "stats": stats}, f, indent=1, ensure_ascii=False)
        print(f"[PIPE] json → {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
