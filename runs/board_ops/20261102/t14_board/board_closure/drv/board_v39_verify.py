#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""board_v39_verify.py —— v39 单次 load 真机验证**驱动**。

本文件只做编排，不碰算法：
    load 一次 → 地址/身份 → 整网 256² → 整网 64² → 单层 RLAT → 频率曲线
  整网：bin_net.BinNet（原样调用，含 BIN_PACK/BIN_LAZY_DEQ/BIN_CAT_FUSION 语义）
  单层：bin_test_staged.stage5（原样调用，只为拿 cyc/rd/fill ⇒ RLAT）

判据（冻结基线，与 board_cat_fusion_ab.FROZEN 同源）：
    256: 9b1940c8955b / f86a7035621b / 025757a83c39
     64: bfa246d34681 / 226bd39e522a / 762d84b9618e
配置（任务书指定）：BIN_LAZY_DEQ=1  BIN_LAZY_SLOTS=24  Q8CACHE=0
    + 基线同款 BIN_PACK=e1e2  BIN_CAT_FUSION=1  （否则拿不到 8.3 s 那一档）

用法（板上）：
  source /etc/profile.d/xrt_setup.sh
  /usr/local/share/pynq-venv/bin/python3 -u board_v39_verify.py \
      --bit /home/xilinx/demoir/design_1_150v39.bit --fclk 100 \
      --freqs 125,150 --slots 24 --json /home/xilinx/demoir/v39_verify.json
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import sys
import time
import traceback
from types import SimpleNamespace

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, "pynq")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

try:                                                   # PYNQ 3.1 不自动注册 Device
    import pynq.pl_server.embedded_device as _emb      # noqa: F401
except Exception:                                      # noqa: BLE001
    pass
from pynq import Overlay, Clocks                       # noqa: E402

import bin_engine as BE                                # noqa: E402
import bin_net as BN                                   # noqa: E402

PACK = "/home/xilinx/demoir/deploy/bin_weights_new"
GOLDEN = "/home/xilinx/demoir/pl/rtl_hand/golden"
FROZEN = {
    64: ("bfa246d34681e748311bbcd2f1b87317a56923fe",
         "226bd39e522aa373b8fc183acae5280931f8b63f",
         "762d84b9618eab8922edd6da4f07ba06de865628"),
    256: ("9b1940c8955be53ccffd7905e333deffcea31f0a",
          "f86a7035621bed133374a1bef42a27db204e7019",
          "025757a83c3993c8f592e31219b6a97906020363"),
}
# 基线输入（seed=1，(3,hw,hw) float32 均匀分布）的 sha1，用来证明"同一输入"
X0_SHA1 = {256: "cfc75bc3f15d306b"}                    # 只记了 256；64 现场打印

LOGPATH = "/home/xilinx/demoir/v39_verify.log"
_fh = None
R = {"steps": {}}


def log(s=""):
    global _fh
    s = str(s)
    print(s, flush=True)
    if _fh is None:
        _fh = open(LOGPATH, "a", buffering=1, encoding="utf-8")
    _fh.write(s + "\n")
    _fh.flush()
    try:
        os.fsync(_fh.fileno())
    except Exception:                                  # noqa: BLE001
        pass


def hdr(s):
    log("")
    log("=" * 74)
    log("  " + s)
    log("=" * 74)


class _Tee:
    """把 stage5 的 print 同时写进本脚本的日志（只为留证，不改其逻辑）。"""

    def __init__(self, fh, real):
        self.fh = fh
        self.real = real

    def write(self, s):
        self.real.write(s)
        if self.fh is not None:
            self.fh.write(s)
        return len(s)

    def flush(self):
        self.real.flush()
        if self.fh is not None:
            self.fh.flush()


def sha1_of(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def dump(path):
    """落盘。★ 这里**再刷一次内核状态**：dump 发生在所有 run 之后 ⇒ 此时
    `q8/c8deq/bpack._STATE` 已是实测结果（权威），覆盖开跑前的 probe 值。"""
    try:
        rp = R.setdefault("runpoint", {})
        d, ok = kernel_deps()
        rp["kernel_deps"] = d
        rp["deps_closure_ok"] = bool(ok)
        rp["kernel_deps_note"] = ("kernel_deps = 跑完后的实测（权威）；"
                                  "kernel_deps_probe = 开跑前的预测")
    except Exception as ex:                            # noqa: BLE001
        R.setdefault("runpoint", {})["kernel_deps_error"] = repr(ex)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False)


# ---------------------------------------------------------------- 配置开关
# ★ 2026-11-02（②）：`BIN_BILINEAR_C` 也进 `_KEYS` ⇒ 每次 run 的 `row["env"]`、
#   `R["env_at_start"]`、`R["runpoint"]["switches"]` 都会留证。
#   它是 opt-in 开关（默认关），所以由 `--bilinear-c`（**默认 1 = 基线运行点**）
#   **显式**落成环境变量——否则"默认关"会把基线悄悄变成 numpy 臂（口径漂移）。
_KEYS = ("BIN_PACK", "BIN_LAZY_DEQ", "BIN_LAZY_SLOTS", "CATPACK",
         "BIN_CAT_FUSION", "BIN_CAT_STRICT", "Q8CACHE", "Q8CACHE_MB",
         "Q8_C8_IMPL", "BIN_Q8_FUSED", "BIN_BILINEAR_C")

# 基线运行点：C 内核算 interp（显式 =1）。--bilinear-c 覆盖。
_BC = {"want": "1"}


def _switch_values(slots, bc=None):
    """`cfg()` 会落成环境变量的那套开关（**不碰环境变量**，供开跑前的 runpoint 记录）。"""
    return {"BIN_PACK": "e1e2",              # 基线同款：E1E2 硬打包
            "BIN_LAZY_DEQ": "1",             # 任务书指定
            "BIN_LAZY_SLOTS": str(int(slots)),   # 任务书指定 24
            "BIN_CAT_FUSION": "1",           # S2 融合（8.3 s 那一档）
            "Q8CACHE": "0",                  # 任务书指定
            "BIN_Q8_FUSED": "1",
            "BIN_BILINEAR_C": _BC["want"] if bc is None else str(bc)}


def cfg(slots, bc=None):
    for k in _KEYS:
        os.environ.pop(k, None)
    for k, v in _switch_values(slots, bc).items():
        os.environ[k] = v
    return {k: os.environ.get(k, "") for k in _KEYS}


# ---------------------------------------------------------------- 运行点自记录
# ★ 2026-11-02（②）：每份 JSON 都必须能回答"这一跑用的是哪份代码"。
#   复核者就卡在这里：6.455 s 那组只有时间，没有代码 md5 ⇒ 无法证明跑的是哪份代码。
#   五份代码 = 本脚本实际 import / 编译的那几份（HERE 目录，即板端真正执行的文件）。
CODE_FILES = ("bin_net.py", "bin_engine.py", "bin_ref_numpy.py",
              "bilinear_c.py", "bilinear_c.c")


def kernel_deps():
    """`bin_engine.kernel_deps()` 的薄封装：返回 (dict, deps_closure_ok)。

    ★ **零副作用**：只读模块自己的 `_STATE` + `find_spec`，不会 import 内核模块，
      也不会触发 `cc` 现场编译（否则会把一次性编译成本从 prologue 里挪走）。
    """
    try:
        import bin_engine as _BE
        d = _BE.kernel_deps()
        return d, _BE.deps_closure_ok(d)
    except Exception as ex:                            # noqa: BLE001
        return {"error": repr(ex)}, False


def deps_summary():
    """一行摘要（打日志用）：`q8=fast c8deq=fast ... ⇒ deps_closure_ok=True`。"""
    d, ok = kernel_deps()
    if "error" in d:
        return "[DEPS] <读不到：%s>" % d["error"]
    order = ("q8", "c8deq", "bpack", "e1e2", "catpack", "safe")
    return ("[DEPS] " + " ".join("%s=%s" % (k, d[k]["state"]) for k in order
                                 if k in d)
            + "  ⇒ deps_closure_ok=%s" % bool(ok))


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_md5():
    out = {}
    for name in CODE_FILES:
        try:
            out[name] = md5_of(os.path.join(HERE, name))
        except Exception as ex:                        # noqa: BLE001
            out[name] = "ERROR:%r" % (ex,)
    return out


def runpoint(bit=None, fclk_target=None, slots=None, switches=None):
    """运行点自记录（写进 R["runpoint"]）：位流 md5 + 五项代码 md5 + 开关 + 频率/槽数。"""
    rp = R.setdefault("runpoint", {})
    rp["code_md5"] = code_md5()
    if bit is not None:
        rp["bit"] = bit
        try:
            rp["bit_md5"] = md5_of(bit) if os.path.exists(bit) else "MISSING"
        except Exception as ex:                        # noqa: BLE001
            rp["bit_md5"] = "ERROR:%r" % (ex,)
    if fclk_target is not None:
        rp["fclk_target"] = fclk_target
    if slots is not None:
        rp["slots"] = int(slots)
    rp["switches"] = ({k: os.environ.get(k, "") for k in _KEYS}
                      if switches is None else dict(switches))
    # ★ 2026-11-02（§② 依赖可见性）：跑点必须能回答"这一跑跑在什么内核上"。
    #   2026-11-02 那次 1.65× 假数字，直接原因就是三份 JSON 只记了时间、没记内核状态
    #   （prologue/epilogue 从 C 内核换成 numpy 回落，而看 JSON 完全看不出来）。
    #   ⇒ 从今天起：`deps_closure_ok=False` 的跑点**不许进基线表**。
    _d, _ok = kernel_deps()
    rp["kernel_deps_probe"] = _d           # 开跑前（零副作用预测；不触发 cc 编译）
    rp["deps_closure_ok"] = bool(_ok)      # dump() 时会被实测值覆盖
    rp["stamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return rp


# ---------------------------------------------------------------- 步骤 0：一次性成本
# ★ 2026-11-02（④）：`bilinear_c.load()`（cc 编译 + 6 形状 fast 自检）是**一次性成本**，
#   原来它落在第一次 run 的计时里 ⇒ 首跑 8.77 s 里有 ≈0.73 s 不是稳态。
#   现在：
#     ① 在这里**单独计时一次并打印**，写进 `R["runpoint"]["bilinear_c"]`；
#     ② 本 session 首个 run 的行里追认 `total_incl_onetime_s = total_s + 一次性成本`
#        （口径 a：用户真实首帧），而 `total_s` 永远是**不含**它的稳态（口径 b）；
#     ③ 两个口径**都打印**——不许只报好看的那个。
_ONETIME = {"boot_s": None, "charged": False}


def step_bilinear():
    hdr("步骤 0：C 内核一次性成本（load + fast 自检）—— 单独计时，不进稳态")
    rp = R.setdefault("runpoint", {})
    rec = dict(env=os.environ.get("BIN_BILINEAR_C", "<未设置>"))
    t0 = time.perf_counter()
    try:
        import bilinear_c as bc
    except Exception as ex:                            # noqa: BLE001
        dt = time.perf_counter() - t0
        log("  import bilinear_c 失败：%r ⇒ 永久走 numpy" % (ex,))
        rec.update(status="import_error", error=repr(ex)[:300],
                   seconds=round(dt, 4))
        rp["bilinear_c"] = rec
        return rec
    lib = bc.load()                        # 内含 fast 自检（6 形状）
    dt = time.perf_counter() - t0
    rec.update(loaded=bool(lib is not None), why=str(bc.why()),
               seconds=round(dt, 4),
               note="seconds = 本进程 load()+fast 自检；.so 已存在时不含 cc 编译")
    try:
        so = os.path.join(HERE, "bilinear_c_%s.so" % bc.ARCH)
        rec["so"] = os.path.basename(so)
        rec["so_md5"] = md5_of(so) if os.path.exists(so) else "MISSING"
    except Exception as ex:                            # noqa: BLE001
        rec["so_md5"] = "ERROR:%r" % (ex,)
    if lib is not None:
        _ONETIME["boot_s"] = dt
        log("  BIN_BILINEAR_C=%s → 内核已加载（why=%s，fast 自检 6 形状通过）"
            % (rec["env"], bc.why()))
        log("  **一次性成本 = %.3f s**（本进程只付一次）" % dt)
        log("  口径 a) 含一次性成本的首帧 = 首个 run 的 total + %.3f s（用户真实体验）" % dt)
        log("  口径 b) 不含它的稳态 = 首个 run 本身的 total（技术对比）")
    else:
        log("  BIN_BILINEAR_C=%s → 未启用（why=%s）；本次无一次性成本 ⇒ 口径 a = 口径 b"
            % (rec["env"], bc.why()))
    rp["bilinear_c"] = rec
    return rec


# ---------------------------------------------------------------- 运行期切臂（⑤ 用）
# ★ 2026-11-02（⑤）：同进程 A/B 必须能在**运行期**切 interp 内核 —— `bilinear_c._STATE`
#   （tried/lib）与 `bin_ref_numpy._C_KERNEL`（tried/fn）都是 once-per-process 缓存。
#   本函数**只重置这两个缓存**并落 `BIN_BILINEAR_C`（同时更新 `_BC["want"]`，
#   否则下一次 `run_net→cfg()` 会把臂切回默认值），**不改任何源码/数值逻辑**。
#   返回 `(kernel_loaded, why)`：调用方**必须**用它判断"这条臂到底是不是 C 臂"，
#   不许只看环境变量（C 编不出时 set_arm(True) 会返回 False）。
_ARM = {"use_c": None}


def set_arm(use_c):
    os.environ["BIN_BILINEAR_C"] = "1" if use_c else "0"
    _BC["want"] = os.environ["BIN_BILINEAR_C"]
    _ARM["use_c"] = bool(use_c)
    import bin_ref_numpy as _rn
    import bilinear_c as _bc
    _rn._C_KERNEL = {"tried": False, "fn": None}
    _bc._STATE = {"tried": False, "lib": None, "why": "reset", "checked": False}
    fn = _rn._bilinear_c_kernel()
    return (fn is not None), str(_bc.why())


# ---------------------------------------------------------------- 步骤 0b：内核装载
def step_kernels():
    """`q8/c8deq/bpack` 的**一次性装载成本**：单独计时，不进稳态。

    ★ 为什么非要有这一步（2026-11-02 12:13 板端实测）：
      三个内核都是"首次使用时才 load()"（`.so` 已编好也要 load + 逐位自检
      1.4~1.9 s/个）。这块成本落在**首个整网的 prologue/epilogue 桶里**：

        | 桶        | 首跑（含装载） | 第二次跑（热态） |
        | prologue  | 3.572 s        | 1.613 s          |
        | epilogue  | 1.632 s        | 0.257 s          |

      而 `PERF_GATE` 的门槛是热态值（q8 0.61 s / epi 0.26 s）⇒ 不先把装载挪出去，
      每次 session 首跑都会被判 `run_valid=False`（误报），报警就没人看了。
      这与 `step_bilinear()` 对 `bilinear_c` 的处理**完全同构**：一次性成本显式计时，
      并且**不偷偷加进 total**（口径 a/b 都打印）。
    """
    hdr("步骤 0b：q8/c8deq/bpack 一次性装载（单独计时，不进稳态）")
    rec = BE.load_kernels()
    rp = R.setdefault("runpoint", {})
    rp["kernel_load"] = rec
    tot = 0.0
    for k, v in rec.items():
        tot += float(v.get("seconds") or 0.0)
        log("  %-6s load=%.3f s  loaded=%s  why=%s"
            % (k, v.get("seconds") or 0, v.get("loaded"), v.get("why")))
    rp["kernel_load_seconds"] = round(tot, 4)
    log("  **一次性装载合计 = %.3f s**（从这一行起，后面的 prologue/epilogue 才是热态）"
        % tot)
    if not all(v.get("loaded") for v in rec.values()):
        log("  ⚠ 有内核没装上 ⇒ 本跑点 deps_closure_ok 会是 False（见 [DEPS] 行）")
    return rec


# ---------------------------------------------------------------- 步骤 1：load
def step_load(bit, download=True):
    hdr("步骤 1：加载 overlay（本 session 唯一一次真正的 download）")
    t0 = time.time()
    ov = Overlay(bit, download=download)
    dt = time.time() - t0
    log("  Overlay(%s, download=%s) 用时 %.3f s" % (bit, download, dt))
    R["load"] = dict(bit=bit, download=bool(download), seconds=round(dt, 4))
    return ov


# ---------------------------------------------------------------- 步骤 2：地址
def step_addr(ov):
    hdr("步骤 2：地址核对（从**已加载的** overlay 实测）")
    want = {"conv_uni_0": 0x40040000, "bin_unit_0": 0x40060000}
    got = {}
    for name, exp in want.items():
        try:
            ip = getattr(ov, name)
            ba = int(ip.mmio.base_addr)
            ln = int(ip.mmio.length)
            got[name] = dict(base_addr=ba, base_addr_hex="%#010x" % ba,
                             length=ln, expected_hex="%#010x" % exp,
                             ok=bool(ba == exp))
            log("  %-12s mmio.base_addr = %#010x  length = %#x  （期望 %#010x）  %s"
                % (name, ba, ln, exp, "✓" if ba == exp else "✗✗"))
        except Exception as ex:                        # noqa: BLE001
            got[name] = dict(error=repr(ex)[:300])
            log("  %-12s ✗ 读不到：%r" % (name, ex))
    try:
        log("  overlay 里的 IP 清单：%s" % sorted(ov.ip_dict.keys()))
    except Exception:                                  # noqa: BLE001
        pass
    R["addr"] = got
    return got


# ---------------------------------------------------------------- 步骤 3：身份
def step_identity(ov, fclk):
    hdr("步骤 3：时钟读回 + 复位指纹")
    Clocks.fclk0_mhz = fclk
    time.sleep(0.4)
    rb = Clocks.fclk0_mhz
    log("  Clocks.fclk0_mhz 目标 %g → **读回 = %g**  %s"
        % (fclk, rb, "✓" if abs(rb - fclk) < 0.5 else "✗ 没设上"))
    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    ash = e._rd_reg("ALPHA_SHIFT")
    ssh = e._rd_reg("SLOPE_SHIFT")
    ctl = e._rd_reg("AP_CTRL")
    ap = e._rd_reg("AP_RET")
    dfl = e._rd_reg("DBG_FLAG")
    resv = e._rd(0x18)
    log("  复位指纹：alpha_shift=%d slope_shift=%d （期望 9 / 8）" % (ash, ssh))
    log("  AP_CTRL=%#010x AP_RETURN=%#010x DBG_FLAG=%#010x" % (ctl, ap, dfl))
    log("  保留页 ResvPage[0x18] = %#x （期望 0）" % resv)
    log("  引擎认到的 IP=%s base=%#x range=%#x"
        % (e.ip_name, e.base_pa, e.range_size))
    ok = (ash, ssh) == (9, 8) and resv == 0
    d = dict(alpha_shift=ash, slope_shift=ssh, ap_ctrl=ctl, ap_ret=ap,
             dbg_flag=dfl, resv_0x18=resv, fclk_readback=rb, fclk_target=fclk,
             ip=e.ip_name, base=e.base_pa, range=e.range_size, ok=bool(ok))
    R["identity"] = d
    del e
    gc.collect()
    if not ok:
        log("  ✗✗ 指纹不对 ⇒ bit 或地址错，停止")
    return d


# ---------------------------------------------------------------- 整网一次
def make_arena(hw, slots, name):
    n0 = BN.BinNet(PACK, engine=None, H=hw, W=hw, verbose=False)
    g, _o, _t = n0.build_graph(np.zeros((3, hw, hw), np.float32))
    conv = [o for o in g.ops if o.kind == "conv"]
    max_in = max(o.layer.act_bytes for o in conv)
    max_out = max(o.layer.out_bytes for o in conv)
    need = (max_in + max_out + 4096) * max(2, int(slots)) + (1 << 16)
    del n0, g
    gc.collect()
    arena = BE.BinArena(need, name=name)
    log("  arena %-14s %.2f MB（%d 槽 × (in %.2f + out %.2f MB)）@%#x"
        % (name, need / 1e6, max(2, int(slots)), max_in / 1e6, max_out / 1e6,
           int(arena.buf.physical_address)))
    return arena, need


def run_net(ov, hw, arena, slots, tag="", bc=None, seed=1):
    env = cfg(slots, bc)
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    arena.reset()
    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    eng.load_weights()
    net = BN.BinNet(PACK, engine=eng, H=hw, W=hw, verbose=False)
    rng = np.random.default_rng(int(seed))
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    t0 = time.time()
    outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
    dur = time.time() - t0
    sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
    ok = (FROZEN[hw] == (sh["out1"], sh["out2"], sh["out3"]))
    # ⑥（2026-11-02）：seed ≠ 1 时 sha1 **必然**与冻结基线不同（那是另一个输入）
    #   ⇒ 判据换成"numpy臂=C臂 逐位相同"（由 driver 比对），本行不冒充"通过"。
    frozen_cmp = (int(seed) == 1)
    prof = st.get("prof") or {}
    row = dict(tag=tag, hw=hw, slots=int(slots), seed=int(seed), env=env,
               x0_sha1=xs,
               total_s=st.get("total"), wall_s=round(dur, 4),
               pl_s=st.get("pl"), pack_pl=st.get("pack_pl"),
               ps_s=st.get("ps"), prologue=st.get("ps_prologue"),
               epilogue=st.get("ps_epilogue"), nonconv=st.get("ps_nonconv"),
               overlap=st.get("overlap"), wait=st.get("wait"),
               mmio=st.get("mmio_writes"),
               prof={k: round(float(v), 4) for k, v in prof.items()},
               lazy=st.get("lazy"), cat=st.get("cat"),
               sha1=sh, expect=FROZEN[hw],
               sha_frozen_ok=bool(ok),
               sha_ok=(bool(ok) if frozen_cmp else None),
               frozen_comparable=bool(frozen_cmp),
               n_sha_ok=(int(ok) * 3 if frozen_cmp else None),
               # ★ 2026-11-02（§④）：这一跑的**内核状态**与**性能门槛**都进行。
               #   `run_valid=False` 的行不许进基线表（回落数值等价 ⇒ sha1 全过，
               #   sha_frozen_ok 抓不到这类事故）。
               deps_closure_ok=st.get("deps_closure_ok"),
               kernel_deps={k: v.get("state") for k, v in
                            (st.get("kernel_deps") or {}).items()},
               perf_gate=st.get("perf_gate") or [],
               # ★ 门槛的**输入**也要留证：`perf_gate` 空 ≠ 没判，判过要知道数是多少
               #   （`e1e2_prof.q8` 就是 `prologue.q8` 桶；只有 BIN_PROFILE=1 才出）。
               e1e2_prof=st.get("e1e2_prof"),
               # ★ 热态（口径 a/b）：False ⇒ 这一跑含一次性内核装载，桶不是热态值
               #   ⇒ 基线表只收 perf_warm=True 的行（2026-11-02 板端实测：
               #   首跑 epilogue 1.640 s vs 热态 0.257 s）。
               perf_warm=st.get("perf_warm"),
               perf_note=st.get("perf_note"),
               run_valid=st.get("run_valid"))
    # ④ 两种口径：total_s 永远是不含一次性成本的稳态（b）；只有本 session 首个 run
    #    追认 total_incl_onetime_s（a = 稳态 + 一次性 load/自检）。之后的行 a = b。
    boot = _ONETIME.get("boot_s")
    if boot and not _ONETIME["charged"]:
        tot = row["total_s"]
        row["onetime_s"] = round(float(boot), 4)
        row["total_incl_onetime_s"] = (round(float(tot) + float(boot), 4)
                                       if tot is not None else None)
        _ONETIME["charged"] = True
        log("     口径 a) 含一次性成本 %.3f s 的首帧 = %.3f s  |  "
            "口径 b) 稳态 = %.3f s（上面的 total 即 b）"
            % (boot, (tot or 0.0) + boot, tot or 0.0))
    else:
        row["total_incl_onetime_s"] = row["total_s"]
    log("  ── %s hw=%d ──" % (tag or "net", hw))
    if row.get("run_valid") is False:
        log("     ⚠⚠ run_valid=False（deps_closure_ok=%s, perf_gate=%s）"
            " ⇒ 本行数字**不许进基线表**"
            % (row.get("deps_closure_ok"),
               [a.get("bucket") for a in row.get("perf_gate") or []]))
    log("     总 %.3f s（墙钟 %.3f） PL %.3f（其中 E1E2 %.3f）| PS %.3f = "
        "prologue %.3f + epilogue %.3f + 非卷积 %.3f | overlap %.3f wait %.3f"
        % (row["total_s"] or 0, dur, row["pl_s"] or 0, row["pack_pl"] or 0,
           row["ps_s"] or 0, row["prologue"] or 0, row["epilogue"] or 0,
           row["nonconv"] or 0, row["overlap"] or 0, row["wait"] or 0))
    log("     非卷积分项 %s" % json.dumps(row["prof"], ensure_ascii=False))
    if frozen_cmp:
        log("     输入 sha1=%s %s" % (xs[:16],
                                     "(与冻结基线一致)" if hw != 256 or
                                     xs.startswith(X0_SHA1[256]) else "(⚠ 与基线不同)"))
        log("     sha1 out1=%s out2=%s out3=%s  %s"
            % (sh["out1"][:12], sh["out2"][:12], sh["out3"][:12],
               "OK/3" if ok else "**≠冻结基线**"))
    else:
        log("     seed=%d ⇒ 输入 sha1=%s（**故意**与冻结基线不同）；"
            "sha1 out1=%s out2=%s out3=%s"
            % (int(seed), xs[:16], sh["out1"][:12], sh["out2"][:12],
               sh["out3"][:12]))
        log("     本行不与冻结基线比（另一个输入）；判据是 numpy臂 vs C臂 输出逐位相同")
    del net, eng, outs
    gc.collect()
    return row


# ---------------------------------------------------------------- 步骤 4/5
def sha_label(r):
    """sha1 判据的口径标签：seed≠1 时**不许**写成"通过"（那是另一个输入）。"""
    if r.get("sha_ok") is None:
        return "跳过(seed≠1)"
    return "3/3 OK" if r["sha_ok"] else "**≠**"


def step_net(ov, hw, slots, fclk, seed=1):
    hdr("步骤 4：整网 %d² 功能判据（fclk 读回 %g MHz，seed=%d）"
        % (hw, fclk, int(seed)))
    arena, need = make_arena(hw, slots, "v39_arena_%d" % hw)
    row = run_net(ov, hw, arena, slots, tag="net%d" % hw, seed=seed)
    R["net_%d" % hw] = row
    del arena
    gc.collect()
    return row


# ---------------------------------------------------------------- 步骤 6：RLAT
def step_rlat(ov, fclk):
    hdr("步骤 6：单层 RLAT（复用 bin_test_staged.stage5，只为拿 cyc/rd/fill）")
    import bin_test_staged as BS
    pool = BE.BinArena(8 << 20, name="rlat_pool")
    e = BE.BinConvEngine(ov, unit_id=0, pack_dir=PACK, verbose=False)
    e.add_region(pool.buf, "pool")
    bl = BS.Blob(PACK)          # Blob 由 bin_ref_numpy 提供，bin_test_staged 已导入
    minL, cyc = BS.pick_min_layer(bl)
    L0 = BE.BinLayer(minL, 8, 8)
    out_buf, out_pa = pool.region(L0.out_bytes, name="OUT", keep_canary=True)
    a = SimpleNamespace(golden=GOLDEN, seed=20261031, fclk=fclk)
    real = sys.stdout
    sys.stdout = _Tee(_fh, real)
    try:
        BS.stage5(e, bl, pool, out_buf, out_pa, a, None)   # wdt 要对象不要秒数
        R["rlat"] = dict(status="ok", note="明细见下方日志 cyc/rd/fill 行")
    except Exception as ex:                            # noqa: BLE001
        traceback.print_exc()
        R["rlat"] = dict(status="error", error=repr(ex)[:500])
    finally:
        sys.stdout = real
        log("  （stage5 结束）")
    del pool, e, bl
    gc.collect()


# ---------------------------------------------------------------- 步骤 7：曲线
def step_sweep(ov, hw, slots, freqs, reps, fclk0, seed=1):
    hdr("步骤 7：频率曲线（同 bit / 同 PS 代码，只切 Clocks.fclk0_mhz）")
    arena, need = make_arena(hw, slots, "v39_sweep_%d" % hw)
    rows = []
    for f in freqs:
        try:
            Clocks.fclk0_mhz = f
        except Exception as ex:                        # noqa: BLE001
            log("  设 %g MHz 失败：%r" % (f, ex))
            continue
        time.sleep(0.4)
        got = Clocks.fclk0_mhz
        best = None
        for r in range(max(1, reps)):
            try:
                row = run_net(ov, hw, arena, slots,
                              tag="sweep@%g rep%d" % (f, r), seed=seed)
            except Exception as ex:                    # noqa: BLE001
                log("  %g MHz rep%d 异常：%r" % (f, r, ex))
                row = dict(tag="sweep@%g" % f, error=repr(ex)[:400])
            if "error" in row:
                best = best or row
                continue
            if best is None or "error" in best or row["total_s"] < best["total_s"]:
                best = row
        if best is not None and "error" not in best:
            best["req_mhz"] = f
            best["got_mhz"] = got
            best["reps"] = reps
            rows.append(best)
            log("  >>> 请求 %g MHz（读回 %g）→ min 总 %.3f s / PL %.3f s  sha1 %s"
                % (f, got, best["total_s"], best["pl_s"] or 0,
                   sha_label(best)))
            R["sweep"] = rows
            dump(R["_json"])
        else:
            rows.append(dict(req_mhz=f, got_mhz=got, error=str(best)[:300]))
            R["sweep"] = rows
            dump(R["_json"])
    del arena
    gc.collect()
    try:
        Clocks.fclk0_mhz = fclk0
        log("  时钟已恢复到 %g MHz（读回 %g）" % (fclk0, Clocks.fclk0_mhz))
        R["fclk_restored"] = Clocks.fclk0_mhz
    except Exception as ex:                            # noqa: BLE001
        log("  ⚠ 恢复时钟失败：%r" % (ex,))
    return rows


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v39.bit")
    ap.add_argument("--fclk", type=float, default=100.0)
    ap.add_argument("--freqs", default="125,150",
                    help="曲线频率（100 已在功能判据里测过；175 需自行判断余量）")
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--json", default="/home/xilinx/demoir/v39_verify.json")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--skip", default="", help="rlat,sweep,net64")
    ap.add_argument("--bilinear-c", type=int, choices=(0, 1), default=1,
                    help="1=interp 走 C 内核（**基线运行点，opt-in 必须显式**）；"
                         "0=numpy 回落臂。默认 1 并显式写进环境变量与 runpoint。")
    ap.add_argument("--seed", type=int, default=1,
                    help="整网输入 rng=default_rng(seed)。默认 1 = 冻结基线输入；"
                         "换 seed 时 sha1 必然不同 ⇒ 只能比 numpy臂 vs C臂 逐位相同。")
    a = ap.parse_args(argv)
    skip = set(x.strip() for x in a.skip.split(",") if x.strip())
    _BC["want"] = str(a.bilinear_c)
    os.environ["BIN_BILINEAR_C"] = _BC["want"]   # opt-in 开关：显式落环境变量，不靠默认值
    R["_json"] = a.json
    R["args"] = vars(a)
    R["started"] = time.strftime("%Y-%m-%d %H:%M:%S")
    R["env_at_start"] = {k: os.environ.get(k, "") for k in _KEYS}
    rp = runpoint(a.bit, a.fclk, a.slots,
                  switches=_switch_values(a.slots, a.bilinear_c))
    log("  运行点自记录：bit_md5=%s" % rp.get("bit_md5"))
    for _k in CODE_FILES:
        log("    %-18s %s" % (_k, rp["code_md5"].get(_k)))
    # ★ 2026-11-02（§②）：日志里也打一行内核状态 —— 报告作者不用翻 JSON 就能看到
    log("  " + deps_summary())
    if not rp.get("deps_closure_ok"):
        log("  ⚠ deps_closure_ok=False ⇒ 本次跑点**不许进基线表**（跑的不是应跑的内核）")
    dump(a.json)

    ov = step_load(a.bit, download=not a.no_download)
    step_addr(ov)
    ident = step_identity(ov, a.fclk)
    dump(a.json)
    if not ident.get("ok"):
        log("!! 指纹不对，停止后续测量")
        R["fatal"] = "fingerprint"
        R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        dump(a.json)
        return 3

    # ④ 一次性成本单独计时（必须在任何 run 之前，否则会被算进首跑）
    step_bilinear()
    # ★ 2026-11-02（§④）：三个 native 内核的装载也必须在这里**一次性做掉**，
    #   否则首个 256² 的 prologue/epilogue 桶会被 load+自检抬高
    #   （板端实测首跑 epi 1.640 s vs 热态 0.257 s）⇒ 门槛误报。
    step_kernels()
    dump(a.json)

    # 256²（最重要）
    r256 = step_net(ov, 256, a.slots, Clocks.fclk0_mhz, a.seed)
    dump(a.json)
    if a.seed == 1 and not r256.get("sha_ok"):
        log("!! 256² sha1 ≠ 冻结基线 ⇒ 真问题，停在这里（不做 64/曲线）")
        R["fatal"] = "net256 sha1 mismatch"
        R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        dump(a.json)
        return 4
    if a.seed != 1:
        log("  seed=%d：本跑不与冻结基线比 sha1（另一个输入）；"
            "等价性判据在 driver 里比 numpy臂 vs C臂" % a.seed)

    # 64²
    if "net64" not in skip:
        r64 = step_net(ov, 64, a.slots, Clocks.fclk0_mhz, a.seed)
        dump(a.json)
        if a.seed == 1 and not r64.get("sha_ok"):
            log("!! 64² sha1 ≠ 冻结基线 ⇒ 真问题")
            R["fatal"] = "net64 sha1 mismatch"

    if "rlat" not in skip:
        try:
            step_rlat(ov, Clocks.fclk0_mhz)
        except Exception:                              # noqa: BLE001
            traceback.print_exc()
            R["rlat"] = dict(status="crash")
        dump(a.json)

    if "sweep" not in skip:
        freqs = [float(x) for x in a.freqs.split(",") if x.strip()]
        step_sweep(ov, 256, a.slots, freqs, a.reps, a.fclk, a.seed)

    # ---- 汇总（②：把运行点与"跑后 md5"写进同一份 JSON）
    hdr("汇总")
    after = code_md5()
    rp["code_md5_after"] = after
    rp["code_md5_same"] = bool(after == rp.get("code_md5"))
    R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    dump(a.json)
    log("  运行点：bit_md5=%s  fclk 目标 %g / 读回 %g  槽 %d  BIN_BILINEAR_C=%s"
        % (rp.get("bit_md5"), rp.get("fclk_target"),
           (R.get("identity") or {}).get("fclk_readback"),
           rp.get("slots"), os.environ.get("BIN_BILINEAR_C")))
    log("  代码 md5（五项）：%s"
        % json.dumps(rp.get("code_md5"), ensure_ascii=False))
    log("  跑后 md5：%s" % ("✓ 与开跑时逐字节一致（无内存踩踏）"
                          if rp["code_md5_same"] else "✗✗ 变了 ⇒ 立即停手"))
    for key in ("net_256", "net_64"):
        r = R.get(key)
        if not r:
            continue
        log("  %-8s 总 %7.3f  PL %6.3f  prologue %6.3f  epilogue %5.3f  "
            "非卷积 %5.3f  sha1 %s"
            % (key, r["total_s"] or 0, r["pl_s"] or 0, r["prologue"] or 0,
               r["epilogue"] or 0, r["nonconv"] or 0, sha_label(r)))
        if r.get("onetime_s"):
            log("          口径 a) 含一次性 %.3f s 的首帧 = %7.3f s  |  "
                "口径 b) 稳态 = %7.3f s"
                % (r["onetime_s"], r["total_incl_onetime_s"] or 0,
                   r["total_s"] or 0))
    for r in R.get("sweep") or []:
        if "error" in r:
            log("  @%g MHz ERROR %s" % (r["req_mhz"], r["error"]))
            continue
        log("  @%-6g MHz（读回 %g）总 %7.3f  PL %6.3f  sha1 %s"
            % (r["req_mhz"], r["got_mhz"], r["total_s"], r["pl_s"] or 0,
               sha_label(r)))
    log("  → %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
