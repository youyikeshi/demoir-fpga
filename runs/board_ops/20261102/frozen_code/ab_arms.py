#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ab_arms.py —— 20261102 **冻结点**双臂驱动（只做驱动，不做分析）。

## 冻结点纪律（本文件的第一设计目标）

本文件与 7 个代码文件同在 `frozen_code/`（板上 = `/home/xilinx/demoir/ps/pynq_frozen/`），
且 `sys.path.insert(0, HERE)` ⇒ **import 到的一定是冻结点代码**，不是随时被改的
`ps/pynq/`。这是对"活的树不能当基线"的机制性回答：不靠纪律，靠路径。

代码来源：`git HEAD b58964e` 的 `pynq_specialized_code/ps/pynq/<f>`（逐字节可复现）。
**未包含**工作树里两个未验证半成品（q8 加固 / 描述符缓存）。

## 已知偏离（必须写进报告，不许隐藏）

1. 本快照 `bilinear_c.py` 默认值是 `"1"`（HEAD 版），而它的 docstring 写"默认关"——
   这正是第一棒修掉的表述/开关矛盾。本驱动**每臂都显式**写 `BIN_BILINEAR_C=0/1`，
   绝不依赖默认值（`set_arm()` 同时校验"这条臂到底是不是 C 臂"）。
2. 本快照 `bin_ref_numpy.py` = `5a3bbd8b`；8.096 s 冻结基线用的是 `16efab60`。
   两者差异**只有** C 内核挂钩 + docstring（numpy 内核体逐字节相同）——
   见 `bin_ref_numpy_16efab60.py` 与 README 的 diff 说明。
   ⇒ numpy 臂的**数值**可比；**绝对时间**不可跨跑点直接比。
3. 本快照 `bin_net 3f154b75 / bin_engine ddfe6b6f`（纯 v39）与 2026-11-01 跑点的
   `cdf4b9a9 / f848a29c` **不是同一份代码**（后者多了 acq 计数器）⇒ 总时间不可直接相减。

## 判据（sha1 三件套；与 board_v39_verify.FROZEN 同源）

    64^2 : bfa246d34681 / 226bd39e522a / 762d84b9618e
    256^2: 9b1940c8955b / f86a7035621b / 025757a83c39
    x0 sha1 (256, seed=1) = cfc75bc3f15d306b

## 组（同一进程；每轮内逐次交替两臂 ⇒ 系统漂移在臂间抵消）

    ⑤ net64_150      64^2@150      numpy x5 + C x5   (seed=1，对冻结 sha1)
    ⑥a net256_150_s1 256^2@150     numpy x3 + C x3   (seed=1，新跑点自洽基线)
    ⑥b net256_150_s2 256^2@150     numpy x2 + C x2   (seed=2，判据=两臂逐位相同)
    S  selftest("full") 29 形状（C 臂）
    P  协议自证：V.run_net 原函数跑一次 256^2/seed=1（只对 sha1，不与 ⑥a 比时间）

## 口径（每个数字都要能回答"含不含一次性成本"）

    · `onetime_s` = `bilinear_c.load()`（cc 编译 + 6 形状 fast 自检），**计时外付一次**
      ⇒ 所有 `row["total_s"]` 都是**口径 b（稳态）**。
    · 口径 a（用户真实首帧）= 每组第 1 次的 `total_s` + `onetime_s`，只在报告里算。
    · 组内第 2 次起才是"热态"，中位/最好只用它们。

## 用法

主机 dry-run（不碰板子、不 load、不写 /home/xilinx）：
    python3 ab_arms.py --dry-run

板端（必须走 tools/boardlock.sh；一次 session 一次 load）：
    echo xilinx | sudo -S -p '' env XILINX_XRT=/usr \\
      /usr/local/share/pynq-venv/bin/python3 -u \\
      /home/xilinx/demoir/ps/pynq_frozen/ab_arms.py --download
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                    # ★ 冻结点优先，且必须排在所有其它路径之前

import numpy as np

FILES = ("bin_net.py", "bin_engine.py", "bin_ref_numpy.py",
         "bilinear_c.py", "bilinear_c.c", "qcache.py", "board_v39_verify.py")
SELF = "ab_arms.py"

# 11 项开关（与第一棒 board_v39_verify.py 的 _KEYS 同源 + BIN_BILINEAR_C）
SWITCH_KEYS = ("BIN_PACK", "BIN_LAZY_DEQ", "BIN_LAZY_SLOTS", "CATPACK",
               "BIN_CAT_FUSION", "BIN_CAT_STRICT", "Q8CACHE", "Q8CACHE_MB",
               "Q8_C8_IMPL", "BIN_Q8_FUSED", "BIN_BILINEAR_C")

FROZEN = {
    64: ("bfa246d34681e748311bbcd2f1b87317a56923fe",
         "226bd39e522aa373b8fc183acae5280931f8b63f",
         "762d84b9618eab8922edd6da4f07ba06de865628"),
    256: ("9b1940c8955be53ccffd7905e333deffcea31f0a",
          "f86a7035621bed133374a1bef42a27db204e7019",
          "025757a83c3993c8f592e31219b6a97906020363"),
}

R = {"groups": {}}
_LOGFH = None


# ------------------------------------------------------------------ 基础设施
def log(s=""):
    global _LOGFH
    s = str(s)
    print(s, flush=True)
    if _LOGFH is not None:
        _LOGFH.write(s + "\n")
        _LOGFH.flush()


def hdr(s):
    log("")
    log("=" * 78)
    log("  " + s)
    log("=" * 78)


def md5_of(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def code_md5():
    out = {}
    for f in FILES + (SELF,):
        try:
            out[f] = md5_of(os.path.join(HERE, f))
        except Exception as ex:                    # noqa: BLE001
            out[f] = "ERROR:%r" % (ex,)
    return out


def sha1_of(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def switches():
    return {k: os.environ.get(k, "") for k in SWITCH_KEYS}


# ------------------------------------------------------------------ 切臂
# ★ 只用**运行期重置缓存**的方式切臂，不改任何源码/数值逻辑：
#   `bilinear_c._STATE`（tried/lib/why/checked，once-per-process）
#   `bin_ref_numpy._C_KERNEL`（tried/fn，once-per-process）
#   然后调用一次 `_bilinear_c_kernel()` 让分派立刻重新判定，并把"到底装上没有"问清楚。
_ARM = {"use_c": None, "onetime_s": None}


def set_arm(use_c):
    """返回 (kernel_loaded, why, seconds)。kernel_loaded 才是"这条臂是不是 C 臂"的唯一判据。"""
    want = "1" if use_c else "0"
    os.environ["BIN_BILINEAR_C"] = want
    import bilinear_c as BC
    import bin_ref_numpy as RN
    RN._C_KERNEL = {"tried": False, "fn": None}
    BC._STATE = {"tried": False, "lib": None, "why": "reset", "checked": False}
    t0 = time.perf_counter()
    fn = RN._bilinear_c_kernel()
    dt = time.perf_counter() - t0
    _ARM["use_c"] = bool(use_c)
    if use_c and _ARM["onetime_s"] is None:
        # 一次性成本：cc 编译（.so 不存在时）+ 6 形状 fast 自检。只在第一次 C 臂计入。
        _ARM["onetime_s"] = round(dt, 4)
    return (fn is not None), str(BC.why()), round(dt, 4)


# ------------------------------------------------------------------ 整网一次
def run_net_seed(ov, hw, arena, slots, seed, tag="", V=None, BN=None, BE=None):
    """`V.run_net` 的**薄拷贝**，唯一改动是把硬编码的 seed=1 变成参数。

    ★ 忠实性自证：seed=1 时它必须复现 `V.FROZEN[hw]` 的 sha1 三件套，
      且 x0 sha1 与冻结基线一致（协议自证组 P 再做一次交叉核对）。
    """
    env = V.cfg(slots)
    os.environ["BV_FREE"] = "1" if hw >= 256 else "0"
    arena.reset()
    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=V.PACK, verbose=False)
    eng.load_weights()
    net = BN.BinNet(V.PACK, engine=eng, H=hw, W=hw, verbose=False)
    rng = np.random.default_rng(int(seed))
    x0 = rng.random((3, hw, hw), dtype=np.float32)
    xs = sha1_of(x0)
    t0 = time.time()
    outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
    dur = time.time() - t0
    sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
    same = (FROZEN[hw] == (sh["out1"], sh["out2"], sh["out3"]))
    prof = st.get("prof") or {}
    row = dict(tag=tag, hw=int(hw), seed=int(seed), slots=int(slots),
               arm="C" if _ARM["use_c"] else "numpy",
               BIN_BILINEAR_C=os.environ.get("BIN_BILINEAR_C", ""),
               env=env, switches=switches(), x0_sha1=xs,
               total_s=st.get("total"), wall_s=round(dur, 4),
               pl_s=st.get("pl"), pack_pl=st.get("pack_pl"), ps_s=st.get("ps"),
               prologue=st.get("ps_prologue"), epilogue=st.get("ps_epilogue"),
               nonconv=st.get("ps_nonconv"), overlap=st.get("overlap"),
               wait=st.get("wait"), mmio=st.get("mmio_writes"),
               interp_s=(round(float(prof.get("interp", 0.0)), 4)
                         if "interp" in prof else None),
               prof={k: round(float(v), 4) for k, v in prof.items()},
               lazy=st.get("lazy"), cat=st.get("cat"),
               sha1=sh, expect=FROZEN[hw],
               sha_ok=(bool(same) if int(seed) == 1 else None),
               n_sha_ok=(int(same) * 3 if int(seed) == 1 else None))
    log("  ── %-26s %-5s hw=%d seed=%d ──" % (tag, row["arm"], hw, seed))
    log("     总 %.3f s（墙钟 %.3f） PL %.3f | PS %.3f = pro %.3f + epi %.3f + 非卷积 %.3f"
        " | ovl %.3f wait %.3f  interp %.3f"
        % (row["total_s"] or 0, dur, row["pl_s"] or 0, row["ps_s"] or 0,
           row["prologue"] or 0, row["epilogue"] or 0, row["nonconv"] or 0,
           row["overlap"] or 0, row["wait"] or 0, row["interp_s"] or 0))
    log("     x0 sha1=%s  out=%s/%s/%s  %s"
        % (xs[:16], sh["out1"][:12], sh["out2"][:12], sh["out3"][:12],
           ("OK 3/3（=冻结基线）" if same else
            ("≠冻结基线（seed!=1 时属正常）" if int(seed) != 1 else "**✗ 不等于冻结基线**")))
           )
    del net, eng, outs
    gc.collect()
    return row


# ------------------------------------------------------------------ 主机 dry-run
def dry_run(pack, hw=64):
    """主机自测：证明"这份快照能跑"（不碰 pynq、不碰板子、不写 /home/xilinx）。"""
    hdr("主机 dry-run：py 导入 + 双臂切换 + 整网（engine=None，纯 numpy 宿主路径）")
    import bin_engine as BE
    import bin_net as BN
    import bin_ref_numpy as RN
    import bilinear_c as BC

    log("  frozen s 路径 = %s" % HERE)
    for f, m in code_md5().items():
        log("    %-24s %s" % (f, m))

    rec = {"hw": hw, "pack": pack, "arms": {}}

    # 1) C 臂：编译 + fast 自检 + full 29 形状
    ok, why, dt = set_arm(True)
    log("  set_arm(True)  → kernel_loaded=%s why=%s  %.3f s（一次性）" % (ok, why, dt))
    if not ok:
        log("  ✗✗ C 臂没装上 ⇒ dry-run 失败（冻结点不可用）")
        rec["arm_c"] = dict(loaded=False, why=why)
        return 1, rec
    t0 = time.perf_counter()
    full_ok = BC.selftest("full")
    dt_full = time.perf_counter() - t0
    n_shapes = len(BC.REAL_SHAPES) + len(BC.EDGE_SHAPES)
    log("  selftest(\"full\") = %s（%d 形状，%.3f s）" % (full_ok, n_shapes, dt_full))
    rec["arm_c"] = dict(loaded=True, why=why, onetime_s=dt,
                        selftest_full=bool(full_ok), n_shapes=n_shapes,
                        selftest_s=round(dt_full, 4))

    # 2) 双臂跑同一个 64^2 整网（engine=None），比输出逐位
    outs = {}
    for arm in (True, False):
        ok, why, dt = set_arm(arm)
        if arm and not ok:
            return 1, rec
        t0 = time.perf_counter()
        net = BN.BinNet(pack, engine=None, H=hw, W=hw, verbose=False)
        rng = np.random.default_rng(1)
        x0 = rng.random((3, hw, hw), dtype=np.float32)
        o, st = net.run(x0, pipeline=True, nbuf=2, arena=None)
        dur = time.perf_counter() - t0
        sh = {k: sha1_of(o[k]) for k in ("out1", "out2", "out3")}
        prof = st.get("prof") or {}
        outs[arm] = sh
        rec["arms"]["C" if arm else "numpy"] = dict(
            kernel_loaded=bool(ok), why=why, seconds=round(dur, 3),
            interp_s=round(float(prof.get("interp", 0.0)), 4),
            prof_keys=sorted(prof.keys()), sha1=sh,
            sha_ok=bool(FROZEN[hw] == (sh["out1"], sh["out2"], sh["out3"])))
        log("  臂 %-5s %.3f s  interp %.3f  sha1=%s  %s"
            % ("C" if arm else "numpy", dur, float(prof.get("interp", 0.0)),
               sh["out1"][:12],
               "3/3 = 冻结基线" if rec["arms"]["C" if arm else "numpy"]["sha_ok"]
               else "≠ 冻结基线"))
        del net, o
        gc.collect()

    same = outs[True] == outs[False]
    rec["arms_bitwise_same"] = bool(same)
    log("  C 臂 vs numpy 臂输出逐位相同 = %s" % ("✓ 是" if same else "✗ 否"))
    if not same:
        return 1, rec
    # 3) 复原 arm 环境（避免 .so / env 残留决定后续行为）
    os.environ["BIN_BILINEAR_C"] = "0"
    log("  dry-run 通过：%d 形状 full 自检 + 双臂整网逐位一致" % n_shapes)
    return 0, rec


# ------------------------------------------------------------------ 板端主流程
def summary_of(rows):
    tot = [float(r["total_s"]) for r in rows if r.get("total_s")]
    warm = tot[1:] if len(tot) > 1 else tot
    if not warm:
        return {}
    ws = sorted(warm)
    n = len(ws)
    med = ws[n // 2] if n % 2 else 0.5 * (ws[n // 2 - 1] + ws[n // 2])
    it = [float(r["interp_s"]) for r in rows
          if r.get("interp_s") is not None][1:]
    return dict(all=[round(t, 4) for t in tot], warm_n=len(warm),
                med=round(med, 4), best=round(min(warm), 4), worst=round(max(warm), 4),
                interp_warm=[round(x, 4) for x in it])


def board(a):
    import board_v39_verify as V
    import bin_engine as BE
    import bin_net as BN

    globals()["_LOGFH"] = open(a.log, "a", buffering=1, encoding="utf-8")
    md5_before = code_md5()
    hdr("冻结点双臂测量  运行点记录")
    log("  frozen dir = %s" % HERE)
    for f, m in md5_before.items():
        log("    %-24s %s" % (f, m))
    # 判据自证：本地 FROZEN 必须与 board_v39_verify.FROZEN 一致
    assert FROZEN == V.FROZEN, "FROZEN 判据与 board_v39_verify 不一致！"
    log("  判据自证：本地 FROZEN == board_v39_verify.FROZEN ✓")

    ov = V.step_load(a.bit, download=not a.no_download)
    V.step_addr(ov)
    ident = V.step_identity(ov, a.fclk)
    if not ident.get("ok"):
        log("  ✗✗ 复位指纹不对 ⇒ 停手")
        return 3
    try:
        bit_md5 = md5_of(a.bit)
    except Exception as ex:                        # noqa: BLE001
        bit_md5 = "ERROR:%r" % (ex,)
    R["runpoint"] = dict(
        bit=a.bit, bit_md5=bit_md5,
        fclk_target=a.fclk, fclk_readback=ident.get("fclk_readback"),
        slots=int(a.slots), code_md5=md5_before, switches=switches(),
        source="git HEAD b58964e (pynq_specialized_code/ps/pynq)",
        stamp=time.strftime("%Y-%m-%d %H:%M:%S"))
    log("  bit md5 = %s" % bit_md5)
    log("  fclk 读回 = %s（目标 %s）" % (ident.get("fclk_readback"), a.fclk))
    log("  开关 = %s" % json.dumps(switches(), ensure_ascii=False))

    # ---- 一次性成本：先付掉，再开始计时（口径 b）
    hdr("一次性成本（C 内核 load + fast 自检）—— 计时外，只付一次")
    ok, why, dt = set_arm(True)
    log("  set_arm(True) 第一次：kernel_loaded=%s why=%s  **%.3f s**" % (ok, why, dt))
    if not ok:
        log("  ✗✗ C 臂装不上 ⇒ 无法做 A/B，停手")
        return 4
    R["runpoint"]["onetime_s"] = dt
    log("  口径 a) 含一次性成本的首帧 = 每组第 1 次 total + %.3f s" % dt)
    log("  口径 b) 稳态 = 每组第 2 次起（所有 row.total_s 都是这个口径）")

    # ---- 判据门禁：full 29 形状
    hdr("S   selftest(\"full\") —— 29 形状（C 臂）")
    import bilinear_c as BC
    t0 = time.perf_counter()
    full_ok = BC.selftest("full")
    dt_full = time.perf_counter() - t0
    n_shapes = len(BC.REAL_SHAPES) + len(BC.EDGE_SHAPES)
    log("  selftest(\"full\") = %s（%d 形状，%.3f s，kernel=%s）"
        % (full_ok, n_shapes, dt_full, BC.why()))
    R["selftest_full"] = dict(ok=bool(full_ok), n_shapes=n_shapes,
                              seconds=round(dt_full, 4), why=str(BC.why()))

    plan = [("⑤ net64_150", 64, 1, a.n64),
            ("⑥a net256_150_s1", 256, 1, a.n256),
            ("⑥b net256_150_s2", 256, 2, a.n256b)]

    arena = None
    cur_hw = None
    for (gname, hw, seed, n) in plan:
        if cur_hw != hw:
            if arena is not None:
                del arena
                gc.collect()
            hdr("建 arena hw=%d（%d 槽）" % (hw, a.slots))
            arena, need = V.make_arena(hw, a.slots, "ops_arena_%d" % hw)
            R.setdefault("arena", {})[str(hw)] = dict(need_bytes=int(need))
            cur_hw = hw
        hdr("%s   hw=%d seed=%d  numpy x%d + C x%d（逐次交替）"
            % (gname, hw, seed, n, n))
        rows = []
        for i in range(n):
            for use_c in (False, True):
                ok, why, _dt = set_arm(use_c)
                if use_c and not ok:
                    log("  ✗✗ C 臂没装上（why=%s）⇒ 不能当 C 臂，停手" % why)
                    return 5
                row = run_net_seed(ov, hw, arena, a.slots, seed,
                                   tag="%s_%d" % (gname.split()[0], i + 1),
                                   V=V, BN=BN, BE=BE)
                row["kernel_loaded"] = bool(ok)
                row["kernel_why"] = why
                row["iter"] = i + 1
                rows.append(row)
                if not use_c:
                    log("     [numpy] kernel_loaded=%s why=%s" % (ok, why))
        R["groups"][gname] = dict(hw=hw, seed=seed, n=n, rows=rows,
                                  summary={("C" if r["arm"] == "C" else "numpy"): None
                                           for r in rows})
        for arm in ("numpy", "C"):
            sub = [r for r in rows if r["arm"] == arm]
            R["groups"][gname]["summary"][arm] = summary_of(sub)
        log("")
        for arm in ("numpy", "C"):
            s = R["groups"][gname]["summary"][arm]
            if s:
                log("  %s: 全部 %s" % (arm, " ".join("%.3f" % t for t in s["all"])))
                log("  %s: 第2次起 n=%d 中位 %.3f 最好 %.3f 最差 %.3f"
                    % (arm, s["warm_n"], s["med"], s["best"], s["worst"]))
        if seed == 1:
            for r in rows:
                if not r["sha_ok"]:
                    log("  ✗ %s %s sha1 ≠ 冻结基线" % (r["tag"], r["arm"]))
        else:
            # ⑥b 判据：两臂输出逐位相同（不与冻结基线比）
            pairs = {}
            for r in rows:
                pairs.setdefault(r["iter"], {})[r["arm"]] = r
            allsame = True
            for it_, pr in sorted(pairs.items()):
                if "numpy" in pr and "C" in pr:
                    same = pr["numpy"]["sha1"] == pr["C"]["sha1"]
                    same_in = pr["numpy"]["x0_sha1"] == pr["C"]["x0_sha1"]
                    allsame &= same
                    log("  第 %d 轮：同输入=%s  两臂输出逐位相同=%s"
                        % (it_, same_in, "✓" if same else "✗"))
                    log("     numpy %s / C %s"
                        % (pr["numpy"]["sha1"]["out1"][:12],
                           pr["C"]["sha1"]["out1"][:12]))
            R["groups"][gname]["arms_bitwise_same"] = bool(allsame)

    # ---- P 协议自证（V.run_net 原函数）
    hdr("P   协议自证：V.run_net 原函数跑一次 256^2/seed=1（只比 sha1）")
    set_arm(False)
    ref = V.run_net(ov, 256, arena if cur_hw == 256 else None, a.slots,
                    tag="P_V_run_net")
    R["protocol_check"] = dict(
        tag="P_V_run_net", total_s=ref.get("total_s"), x0_sha1=ref.get("x0_sha1"),
        sha1=ref.get("sha1"), sha_ok=ref.get("sha_ok"))
    my = [r for r in R["groups"]["⑥a net256_150_s1"]["rows"] if r["arm"] == "numpy"]
    if my:
        log("  V.run_net   : x0=%s out1=%s total=%.3f"
            % (str(ref.get("x0_sha1"))[:16], ref["sha1"]["out1"][:12],
               ref.get("total_s") or 0))
        log("  ab_arms 拷贝: x0=%s out1=%s total=%.3f"
            % (my[0]["x0_sha1"][:16], my[0]["sha1"]["out1"][:12],
               my[0]["total_s"] or 0))
        same = (ref.get("x0_sha1") == my[0]["x0_sha1"]
                and ref.get("sha1") == my[0]["sha1"])
        R["protocol_check"]["matches_copy"] = bool(same)
        log("  输入与三级输出逐位相同 = %s（⇒ 拷贝是忠实的）" % ("✓" if same else "✗"))
    del arena
    gc.collect()

    # ---- md5 前后（防内存踩踏）
    md5_after = code_md5()
    R["md5_before"] = md5_before
    R["md5_after"] = md5_after
    R["md5_same"] = bool(md5_before == md5_after)
    hdr("md5 前后（跑网 7 文件逐字节对比）")
    for f in FILES + (SELF,):
        b, af = md5_before.get(f), md5_after.get(f)
        log("  %-24s %s  %s" % (f, b, "✓" if b == af else "✗✗ 变了！"))
    log("  %s" % ("✓ 一致（无内存踩踏）" if R["md5_same"]
                 else "✗✗ 不一致 ⇒ 立即停手"))

    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False)
    log("  → %s" % a.json)
    return 0 if R["md5_same"] else 6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="主机自测（不碰板子）")
    ap.add_argument("--pack", default=None, help="权重 pack 目录（dry-run 用）")
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v39.bit")
    ap.add_argument("--fclk", type=float, default=150.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--n64", type=int, default=5)
    ap.add_argument("--n256", type=int, default=3)
    ap.add_argument("--n256b", type=int, default=2)
    ap.add_argument("--json", default="/home/xilinx/demoir/boardops2_frozen.json")
    ap.add_argument("--log", default="/home/xilinx/demoir/boardops2_frozen.log")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--no-download", action="store_true")
    a = ap.parse_args()

    if a.dry_run:
        pack = a.pack or os.path.join(
            os.path.dirname(os.path.dirname(HERE)),
            "deploy", "bin_weights_new")
        rc, rec = dry_run(pack)
        print(json.dumps(rec, ensure_ascii=False, indent=1))
        if os.path.exists(a.json):
            pass
        return rc
    return board(a)


if __name__ == "__main__":
    sys.exit(main())
