#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_e1e2_path.py —— E1E2 驱动/描述符路径的离线等价性证明（无板、无 RTL）

判据（三层，全部逐字节）：
  A. **描述符路径 vs C 内核**（227 层 / 698 命令）
     源：deploy/e1e2_desc 的 desc.bin / thr.bin(+16 B 哨兵)，按 FSM 模拟；
     参考：同一份 int8 源 + 同一套逐边 d，经 `bpack.pack_i8`（C 内核）打包。
  B. **复现 runs/ps256/ab_pack256.json 的 ref/numpy/C 三列 sha1**
     对 112 个"全段 d=0"的层，按 ab_pack.py::make_data 的 seed/数据重放，
     再走描述符路径；sha1 必须等于 JSON 里记录的三列。
  C. **驱动代码本身**（FakeE1E2Unit：寄存器级 E1E2 模型）
     · R_RUN_UNIT 写 4 读回 4 / 写 0 读回 0
     · load_program()（含 arena 平移）+ run_layer()：输出 == 描述符模拟
     · pack_int8()：输出 == C 内核 `bpack_i8`
     · BIN_PACK=e1e2 的 `BinConvEngine.pack_input`：输出 == C 内核 `bpack_f32`

用法：
  python3 ps/tools/verify_e1e2_path.py                 # 全量（约 1~3 分钟）
  python3 ps/tools/verify_e1e2_path.py --layers 40     # A 只跑抽检层（仍需全部解析）
  python3 ps/tools/verify_e1e2_path.py --no-ab         # 跳过 B
  python3 ps/tools/verify_e1e2_path.py --quiet         # 只打汇总
输出：
  runs/e1e2_driver/verify_e1e2.json     结构化结果
  runs/e1e2_driver/verify_e1e2.md       人看的表
  runs/e1e2_driver/layers_a.tsv         A 的全部 227 行（层号/命令/字节/差异）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PSCODE = os.path.realpath(os.path.join(HERE, *[os.pardir] * 2))
ROOT = os.path.dirname(PSCODE)
P_PYNQ = os.path.join(PSCODE, "ps", "pynq")
P_PACK = os.path.join(PSCODE, "deploy", "bin_weights_new")
P_DESC = os.path.join(PSCODE, "deploy", "e1e2_desc")
OUTDIR = os.path.join(ROOT, "runs", "e1e2_driver")
AB_JSON = os.path.join(ROOT, "runs", "ps256", "ab_pack256.json")
sys.path.insert(0, P_PYNQ)

import bin_engine as BE                    # noqa: E402
import bpack                               # noqa: E402
import bin_ref_numpy as BR                 # noqa: E402
from bin_ref_numpy import Blob             # noqa: E402
from e1e2_sim import (sim_command, quantize_int8,  # noqa: E402
                      to_c8_padded)

BASE = 0x08000000


def sha16(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()[:16]


def closed_form_thr(d, tq):
    """(d=fs_in-fs_out, t_q) → int16 THR（与 gen_e1e2_desc.closed_form_thr 逐字一致）。"""
    return _closed(d, tq)


def _closed(d, tq):
    d, tq = int(d), int(tq)
    need = 1 - tq
    if need <= -128:
        T = -129
    elif need > 127:
        T = 127
    elif d >= 0:
        s = 1 << d
        T = -((-need) // s) - 1
    else:
        k = -d
        x_t = need * (1 << k) - (1 << (k - 1))
        lo = x_t + 1 if (need & 1) else x_t
        T = lo - 1
    T = min(max(T, -129), 127)
    return -T


def make_data(cin, H, W, adj, rng):
    """与 runs/ps256/ab_pack.py::make_data 逐字一致（含 NaN/±inf/阈值边界）。"""
    x = (rng.random((cin, H, W), dtype=np.float32) * 2.0 - 0.5)
    hw = H * W
    xs = x.reshape(cin, hw)
    for c in range(cin):
        th = np.float32(adj[c])
        xs[c, 0::7] = th
        xs[c, 1::7] = np.nextafter(th, np.float32(-np.inf))
        xs[c, 2::7] = np.nextafter(th, np.float32(np.inf))
        xs[c, 3::13] = np.float32(0.0)
        xs[c, 4::17] = np.float32(np.nan)
    xs[0, 0] = np.float32(np.inf)
    xs[-1, -1] = np.float32(-np.inf)
    return x


# ============================================================================
# 0. 资产自检 + 每层的 d 集合 / 源 buffer / 段数
# ============================================================================
def analyze_layers(prog, blob):
    rows = []
    for name, cmds in prog.by_layer.items():
        L = blob.layer_by_name(name)
        fsin = int(L["act_fs_in"])
        ds, bufs, lanes_real = set(), {}, 0
        for c in cmds:
            i = int(c["i"])
            w = prog.desc[i * 8:(i + 1) * 8]
            for k in range(8):
                ch = int(c["group"]) * 64 + k * 8
                if ch >= int(c["cfg_c"]):
                    continue
                src = int(w[k] & 0xFFFFFFFF)
                b = prog.buf_at(src)
                if b is None:
                    raise AssertionError("src 不在 int8 buffer：%s g%d k%d" % (name, c["group"], k))
                ds.add(fsin - int(b["fs"]))
                bufs[b["key"]] = b
                lanes_real += 1
        H, W = int(cmds[0]["cfg_h"]), int(cmds[0]["cfg_w"])
        rows.append(dict(name=name, cin=int(L["cin"]), cout=int(L["cout"]),
                         k=int(L["k"]), H=H, W=W, cb=(int(L["cin"]) + 63) // 64,
                         ncmd=len(cmds), d=sorted(ds), segs=len(bufs),
                         lanes_real=lanes_real,
                         all_d0=(ds == {0}),
                         bytes=sum(int(c["words"]) * 8 for c in cmds),
                         idx=int(L.get("idx", -1))))
    rows.sort(key=lambda r: r["idx"])
    return rows


def check_assets(prog, blob, rows, log):
    """核对 thr.bin 每个真实通道 == closed_form(d,t_q)；哨兵引用 546。"""
    bad = n = 0
    thr16 = prog.thr.view("<i2")
    for r in rows:
        L = blob.layer_by_name(r["name"])
        fsin = int(L["act_fs_in"])
        tq = blob.params(L)["t_q"]
        for c in prog.by_layer[r["name"]]:
            i = int(c["i"])
            w = prog.desc[i * 8:(i + 1) * 8]
            for k in range(8):
                ch = int(c["group"]) * 64 + k * 8
                if ch >= int(c["cfg_c"]):
                    continue
                src = int(w[k] & 0xFFFFFFFF)
                thr = int(w[k] >> 32)
                b = prog.buf_at(src)
                d = fsin - int(b["fs"])
                off = (thr - prog.thr_pa) // 2
                for j in range(8):
                    n += 1
                    if int(thr16[off + j]) != _closed(d, int(tq[ch + j])):
                        bad += 1
    sent = int((((prog.desc >> np.uint64(32)) & np.uint64(0xFFFFFFFF))
                == np.uint64(prog.sentinel_pa)).sum())
    log("  thr.bin 逐通道 == closed_form(d,t_q)：%d/%d（不一致 %d）"
        % (n - bad, n, bad))
    log("  哨兵引用：%d 条 lane（期望 %d）" % (sent, prog.n_sentinel_refs))
    log("  arena %.2f MB；desc %d B；thr %d B + 16 B 哨兵"
        % (prog.size / 1e6, prog.desc_bytes, prog.thr_bytes))
    return dict(thr_checked=n, thr_bad=bad, sentinel_refs=sent)


# ============================================================================
# 1. 描述符路径 vs C 内核（全部 227 层）
# ============================================================================
def fill_random_leaves(prog, mem, name, rng):
    """给该层用到的每个 int8 源 buffer 填随机 int8（掺 -128/127/0）。"""
    leaves = {}
    for c in prog.by_layer[name]:
        i = int(c["i"])
        w = prog.desc[i * 8:(i + 1) * 8]
        for k in range(8):
            ch = int(c["group"]) * 64 + k * 8
            if ch >= int(c["cfg_c"]):
                continue
            b = prog.buf_at(int(w[k] & 0xFFFFFFFF))
            if b["key"] in leaves:
                continue
            C, H, W = int(b["C"]), int(b["H"]), int(b["W"])
            arr = rng.integers(-128, 128, (C, H, W), dtype=np.int8)
            flat = arr.reshape(-1)
            flat[::97] = -128
            flat[1::97] = 127
            flat[2::97] = 0
            raw = np.frombuffer(to_c8_padded(arr, pad_to=C), np.uint8)
            o = int(b["off"])
            mem[o:o + len(raw)] = raw
            leaves[b["key"]] = arr
    return leaves


def ref_command_C(prog, mem, c, blob):
    """同一份 int8 源 + 逐边 d → C 内核（bpack_i8）的 64 位字组字节。"""
    L = blob.layer_by_name(c["layer"])
    fsin = int(L["act_fs_in"])
    tq = blob.params(L)["t_q"]
    cin, H, W = int(c["cfg_c"]), int(c["cfg_h"]), int(c["cfg_w"])
    hw = H * W
    grp = int(c["group"])
    i = int(c["i"])
    w = prog.desc[i * 8:(i + 1) * 8]
    xq = np.full((64, hw), -128, np.int8)
    tt = np.full(64, 127, np.int16)              # 127 ⇒ x>=-128 恒 false
    for k in range(8):
        ch = grp * 64 + k * 8
        if ch >= cin:
            continue
        src = int(w[k] & 0xFFFFFFFF)
        b = prog.buf_at(src)
        d = fsin - int(b["fs"])
        o = src - prog.base
        lane = np.frombuffer(mem, np.int8, count=hw * 8, offset=o).reshape(hw, 8)
        q = BR.sat8(np.rint(lane.astype(np.float64) * (2.0 ** d)))
        for j in range(8):
            if ch + j < cin:
                xq[k * 8 + j] = q[:, j]
                tt[k * 8 + j] = np.int16(1 - int(tq[ch + j]))
    A = bpack.pack_i8(xq.reshape(64, H, W), tt)   # (1,H,W) uint64
    return np.ascontiguousarray(A).view(np.uint8).tobytes()


def part_a(prog, blob, rows, mem, log, limit=0, rng_seed=20261031):
    log("== A. 描述符路径 vs C 内核（%s 层）=="
        % ("全部 %d" % len(rows) if not limit else "抽检 %d" % limit))
    mem[:] = 0
    # 描述符 + 阈值 + 哨兵先铺好（之后不再动）
    mem[prog.desc_off:prog.desc_off + prog.desc_bytes] = prog.desc.view(np.uint8)
    mem[prog.thr_off:prog.thr_off + prog.thr_bytes] = prog.thr
    sen = np.full(8, BE.E1E2_SENTINEL_I16, "<i2").tobytes()
    mem[prog.sentinel_off:prog.sentinel_off + 16] = np.frombuffer(sen, np.uint8)

    use = rows[:limit] if limit else rows
    out_rows, t0 = [], time.time()
    total_bytes = total_diff = total_cmd = 0
    for n, r in enumerate(use):
        rng = np.random.default_rng(rng_seed + n)
        fill_random_leaves(prog, mem, r["name"], rng)
        nb = nd = 0
        for c in prog.by_layer[r["name"]]:
            i = int(c["i"])
            got = sim_command(mem, int(c["cfg_c"]), int(c["cfg_h"]), int(c["cfg_w"]),
                              int(c["cfg_group"]),
                              prog.desc[i * 8:(i + 1) * 8], prog.desc_pa, prog.base)
            exp = ref_command_C(prog, mem, c, blob)
            diff = sum(1 for a, b in zip(got, exp) if a != b)
            nb += len(got)
            nd += diff
        out_rows.append(dict(r, bytes=nb, diff=nd, cmds=r["ncmd"]))
        total_bytes += nb
        total_diff += nd
        total_cmd += r["ncmd"]
        if nd:
            log("  ✗ %s：%d 字节差异" % (r["name"], nd))
        elif not limit and (n + 1) % 40 == 0:
            log("   … %d/%d 层，累计 %d 命令 / %.2f MB，0 差异"
                % (n + 1, len(use), total_cmd, total_bytes / 1e6))
    dt = time.time() - t0
    log("  ⇒ %d/%d 层、%d 命令、%.3f MB，差异 %d（%.2f s）"
        % (sum(1 for r in out_rows if r["diff"] == 0), len(out_rows),
           total_cmd, total_bytes / 1e6, total_diff, dt))
    return dict(rows=out_rows, bytes=total_bytes, diff=total_diff,
                cmds=total_cmd, seconds=dt)


# ============================================================================
# 2. 复现 ab_pack256 三列 sha1（全段 d=0 的层）
# ============================================================================
def fill_layer_from_xq(prog, mem, name, xq, H, W):
    """把消费者标度 fs_in 的 x_q 按描述符的通道→buffer 映射散进各源 buffer。"""
    leaves = {}
    for c in prog.by_layer[name]:
        i = int(c["i"])
        w = prog.desc[i * 8:(i + 1) * 8]
        for k in range(8):
            ch = int(c["group"]) * 64 + k * 8
            if ch >= int(c["cfg_c"]):
                continue
            src = int(w[k] & 0xFFFFFFFF)
            b = prog.buf_at(src)
            if b["key"] not in leaves:
                leaves[b["key"]] = (b, np.zeros((int(b["C"]), H, W), np.int8))
            b2, arr = leaves[b["key"]]
            q = (src - (prog.base + int(b2["off"]))) // (H * W * 8)
            for j in range(8):
                arr[q * 8 + j] = xq[ch + j]
    for key, (b, arr) in leaves.items():
        raw = np.frombuffer(to_c8_padded(arr, pad_to=int(b["C"])), np.uint8)
        o = int(b["off"])
        mem[o:o + len(raw)] = raw
    return len(leaves)


def part_b(prog, blob, rows, mem, log):
    if not os.path.exists(AB_JSON):
        log("== B. --check-ab：找不到 %s，跳过 ==" % AB_JSON)
        return dict(ok=0, total=0, skipped=True)
    ref_rows = json.load(open(AB_JSON))["rows"]
    d0 = {r["name"] for r in rows if r["all_d0"]}
    log("== B. 复现 ab_pack256 三列 sha1（全段 d=0 的 %d 层）==" % len(d0))
    mem[:] = 0
    mem[prog.desc_off:prog.desc_off + prog.desc_bytes] = prog.desc.view(np.uint8)
    mem[prog.thr_off:prog.thr_off + prog.thr_bytes] = prog.thr
    sen = np.full(8, BE.E1E2_SENTINEL_I16, "<i2").tobytes()
    mem[prog.sentinel_off:prog.sentinel_off + 16] = np.frombuffer(sen, np.uint8)
    rng = np.random.default_rng(20261031)
    nbad = nok = 0
    out_rows, t0 = [], time.time()
    for j, rr in enumerate(ref_rows):
        name = rr["name"]
        L = blob.layer_by_name(name)
        fs = int(L["act_fs_in"])
        tq = blob.params(L)["t_q"]
        adj, ok = BE.pack_thresh(tq, fs)
        if not ok:
            raise AssertionError("ab 层 %s 的 pack_thresh 不可用" % name)
        x = make_data(int(rr["cin"]), int(rr["H"]), int(rr["W"]), adj, rng)
        if name not in d0:
            continue
        xq = quantize_int8(x, fs)
        fill_layer_from_xq(prog, mem, name, xq, int(rr["H"]), int(rr["W"]))
        chunks = []
        for c in prog.by_layer[name]:
            i = int(c["i"])
            chunks.append(sim_command(
                mem, int(c["cfg_c"]), int(c["cfg_h"]), int(c["cfg_w"]),
                int(c["cfg_group"]), prog.desc[i * 8:(i + 1) * 8],
                prog.desc_pa, prog.base))
        got = sha16(np.frombuffer(b"".join(chunks), np.uint8))
        good = (got == rr["sha_ref"] == rr["sha_np"] == rr["sha_c"])
        nok += int(good)
        nbad += int(not good)
        out_rows.append(dict(i=j, name=name, cin=int(rr["cin"]), H=int(rr["H"]),
                             W=int(rr["W"]), k=int(rr["k"]),
                             sha=got, want=rr["sha_c"], ok=good))
        if not good:
            log("  ✗ %s：got %s want %s" % (name, got, rr["sha_c"]))
        elif (nok % 25) == 0:
            log("   … %d/%d 层 sha1 一致" % (nok, len(d0)))
    dt = time.time() - t0
    log("  ⇒ %d/%d 层 ref/numpy/C 三列 sha1 一致（%.2f s）" % (nok, len(d0), dt))
    return dict(rows=out_rows, ok=nok, total=len(d0), bad=nbad, seconds=dt)


# ============================================================================
# 3. FakeE1E2Unit：寄存器级模型（只模拟 E1E2，不模拟 conv）
# ============================================================================
class FakeE1E2Unit:
    """照抄 wrapper 的 0x200 页 + bin_e1e2 FSM 语义（数据面用 e1e2_sim）。"""

    class _MMIO:
        def __init__(self, u):
            self.u = u

        def write(self, off, v):
            self.u.write(off, v)

        def read(self, off):
            return self.u.read(off)

    def __init__(self, base_pa=0x4006_0000, name="bin_unit_0"):
        self.name = name
        self.ip_dict = {name: dict(phys_addr=int(base_pa),
                                   mem_range_size=0x20000,
                                   type="xilinx.com:user:bin_unit_axi_wrap_bd:1.0")}
        self.mmio = FakeE1E2Unit._MMIO(self)
        self.reg = {}
        self.done = 0
        self.busy = 0
        self.err = 0
        self.run_unit_q = 0
        self.r_ctrl = 0
        self.hi = [0, 0]
        self.cnt = dict(rd=0, cyc=0, rdst=0, wrst=0, wr=0)
        self.busy_ticks = 0          # 测试用：让 busy 保持 N 次读再落 0
        self.flat = None
        self.flat_pa = 0
        self.ops = 0
        self.base_pa = int(base_pa)

    def __getattr__(self, k):
        if k.startswith("bin_unit"):
            return self
        raise AttributeError(k)

    def map_mem(self, pa, buf):
        self.flat, self.flat_pa = buf, int(pa)

    def _view(self, pa, n):
        if self.flat is None or not (self.flat_pa <= pa
                                     and pa + n <= self.flat_pa + len(self.flat)):
            raise AssertionError("模型越界访存：%#x+%d（flat=%#x+%d）"
                                 % (pa, n, self.flat_pa, 0 if self.flat is None else len(self.flat)))
        o = int(pa) - self.flat_pa
        return self.flat[o:o + n]

    # ---- 寄存器 ----
    def write(self, off, v):
        off, v = int(off), int(v) & 0xFFFFFFFF
        if off < BE.CFG:
            return                                   # A[16]=0：保留页
        o = (off - BE.CFG) & 0x3FF
        if o == 0x00:
            self.r_ctrl = v
            if (v & 1) and not self.busy:
                self._start()
            return
        if o == 0x230:                               # 写 0x230 清 5 个计数器
            self.cnt = dict(rd=0, cyc=0, rdst=0, wrst=0, wr=0)
            return
        if o == 0x20C:
            self.hi[0] = 1 if v else 0
            return
        if o == 0x214:
            self.hi[1] = 1 if v else 0
            return
        self.reg[o] = v

    def read(self, off):
        off = int(off)
        if off < BE.CFG:
            return 0
        o = (off - BE.CFG) & 0x3FF
        if o == 0x00:
            return (self.busy & 1) << 1
        if o == 0x10:
            if self.busy_ticks > 0:
                self.busy = 1
                self.busy_ticks -= 1
                if self.busy_ticks == 0:
                    self.busy = 0
            return ((self.err & 0xFF) << 8) | ((self.busy & 1) << 1) | (self.done & 1)
        if o == 0x200:
            return self.reg.get(0x200, 0) & 7
        if o >= 0x230:
            return {"rd": self.cnt["rd"], "cyc": self.cnt["cyc"],
                    "rdst": self.cnt["rdst"], "wrst": self.cnt["wrst"],
                    "wr": self.cnt["wr"]}.get(
                        {0x230: "rd", 0x234: "cyc", 0x238: "rdst",
                         0x23C: "wrst", 0x240: "wr"}.get(o, ""), 0)
        return self.reg.get(o, 0)

    def _start(self):
        self.done = self.err = 0
        ru = int(self.reg.get(0x200, 0)) & 7
        self.run_unit_q = ru
        if ru != 4:
            self.err = 99                              # 本模型不模拟 conv
            self.done = 1
            return
        g = lambda o: int(self.reg.get(o, 0))
        c, h, w, grp = g(0x18), g(0x20), g(0x28), g(0x204)
        desc, out = g(0x208), g(0x210)
        if desc == 0 or out == 0:
            self.err = 1
        elif (desc & 7) or (out & 7):
            self.err = 5
        elif any(self.hi):
            self.err = 2
        elif c == 0 or c > 576 or h == 0 or w == 0:
            self.err = 2
        elif grp > 8:
            self.err = 6
        else:
            hw = h * w
            words = np.frombuffer(self._view(desc, 64).tobytes(), "<u8")
            got = sim_command(self.flat, c, h, w, grp, words, desc, self.flat_pa)
            self._view(out, hw * 8)[:] = np.frombuffer(got, np.uint8)
            cyc, left = 17, hw
            while left > 0:
                run = min(BE.E1E2_ACC_DEPTH, left)
                cyc += 9 * run + 50
                left -= run
            self.cnt = dict(rd=8 + 8 * 2 + 8 * hw, cyc=cyc, rdst=0,
                            wrst=0, wr=hw)
            self.ops += 1
        self.done = 1


class Alloc:
    """把 BE._pynq_allocate 换成 MemBuf 分配器（离线跑 pack/load_program 用）。"""

    def __init__(self, unit, base=0x3000_0000):
        self.unit = unit
        self.next = int(base)

    def __call__(self, shape, dtype=np.uint8):
        n = int(np.prod(shape))
        b = BE.MemBuf(n, self.next)
        self.unit.map_mem(self.next, b)
        self.next += ((n + 4095) // 4096 * 4096) + 0x10_0000
        return b


# ============================================================================
# 3. 驱动级测试
# ============================================================================
def part_c0(blob, rows, log):
    """默认路径 A/B：BIN_PACK 未设时，新 bin_engine.pack_input 与改动前逐字节相同。"""
    bak = os.path.join(P_PYNQ, "bin_engine.py.bak_e1e2_20261031")
    if not os.path.exists(bak):
        log("== C0. 默认路径 A/B：找不到 %s，跳过 ==" % os.path.basename(bak))
        return dict(skipped=True)
    from importlib.machinery import SourceFileLoader
    old = SourceFileLoader("bin_engine_pre_e1e2", bak).load_module()
    picks = []
    for want in (lambda r: r["cin"] == 544, lambda r: r["cin"] == 48,
                 lambda r: r["cin"] == 368 and r["H"] == 128,
                 lambda r: r["cin"] == 32 and r["H"] == 8,
                 lambda r: r["cin"] == 512):
        for r in rows:
            if want(r) and all(q["name"] != r["name"] for q in picks):
                picks.append(r)
                break
    env = os.environ.pop("BIN_PACK", None)
    try:
        out = []
        for n, r in enumerate(picks):
            L = blob.layer_by_name(r["name"])
            rng = np.random.default_rng(314 + n)
            xf = (rng.random((int(L["cin"]), int(r["H"]), int(r["W"])), np.float32)
                  * np.float32(2.0) - np.float32(0.5))
            un = FakeE1E2Unit()
            uo = old.FakeUnit()
            en = BE.BinConvEngine(un, pack_dir=P_PACK, verbose=False)
            eo = old.BinConvEngine(uo, pack_dir=P_PACK, verbose=False)
            a = eo.pack_input(xf, old.BinLayer(L, r["H"], r["W"]))
            b = en.pack_input(xf, BE.BinLayer(L, r["H"], r["W"]))
            nd = sum(1 for x, y in zip(a, b) if x != y)
            out.append(dict(name=r["name"], cin=r["cin"], H=r["H"], W=r["W"],
                            bytes=len(a), diff=nd))
            log("   %-44s cin=%-3d %dx%d 字节=%d 差异=%d %s"
                % (r["name"][-44:], r["cin"], r["H"], r["W"], len(a), nd,
                   "PASS" if nd == 0 else "FAIL"))
    finally:
        if env is not None:
            os.environ["BIN_PACK"] = env
    return dict(rows=out, skipped=False)


def part_c(prog, blob, rows, log, quiet=False):
    log("== C. 驱动代码（FakeE1E2Unit：寄存器级 E1E2 模型）==")
    res = {}
    res["default_ab"] = part_c0(blob, rows, log)
    unit = FakeE1E2Unit()
    core = BE.BinConvEngine(unit, pack_dir=P_PACK, verbose=False)
    ee = BE.E1E2Engine(core, verbose=False)
    pr = ee.probe()
    log("  R_RUN_UNIT 写 4 读回 %#x，写 0 读回 %#x ⇒ %s"
        % (pr["write4"], pr["back0"],
           "PASS" if ((pr["write4"] & 7) == 4 and (pr["back0"] & 7) == 0) else "FAIL"))
    res["probe"] = pr

    # ---- C1. 全量程序 + run_layer（3 层，含 cin=544/g8、cin=48、最大段数层） ----
    pick = []
    for want in (lambda r: r["cin"] == 544,
                 lambda r: r["cin"] == 48,
                 lambda r: r["segs"] == max(x["segs"] for x in rows),
                 lambda r: r["H"] == 128 and r["k"] == 1,
                 lambda r: r["H"] == 8):
        for r in rows:
            if want(r) and all(r["name"] != x["name"] for x in pick):
                pick.append(r)
                break
    pick = pick[:5]
    arena = BE.MemBuf(prog.size, prog.base)
    unit.map_mem(prog.base, arena)
    info = ee.load_program(buf=arena)
    log("  load_program：arena=%#x delta=%+#x，desc/thr/哨兵 = %#x/%#x/%#x，"
        "%d B，memcpy %.3f ms"
        % (info["arena_pa"], info["delta"], info["desc_pa"],
           info["thr_pa"], info["sentinel_pa"], info["bytes_copied"],
           info["copy_ms"]))
    c1 = []
    for n, r in enumerate(pick):
        rng = np.random.default_rng(777 + n)
        fill_random_leaves(prog, arena, r["name"], rng)
        exp = []
        for c in prog.by_layer[r["name"]]:
            i = int(c["i"])
            exp.append(sim_command(arena, int(c["cfg_c"]), int(c["cfg_h"]),
                                   int(c["cfg_w"]), int(c["cfg_group"]),
                                   prog.desc[i * 8:(i + 1) * 8], prog.desc_pa,
                                   prog.base))
        ee._agg = dict(ncmd=0, seconds=0.0, rd=0, cyc=0, rdst=0, wrst=0, wr=0)
        runs = ee.run_layer(r["name"])
        got = []
        for c in prog.by_layer[r["name"]]:
            o = int(c["out_off"])
            nby = int(c["words"]) * 8
            got.append(bytes(arena[o:o + nby]))
        nd = sum(1 for a, b in zip(b"".join(got), b"".join(exp)) if a != b)
        c1.append(dict(name=r["name"], cmds=len(runs), bytes=sum(len(x) for x in got),
                       diff=nd, dbg=ee.dbg_summary()))
        if not quiet:
            log("   run_layer %-44s cin=%-3d %dx%d 命令=%d 字节=%d 差异=%d"
                % (r["name"][-44:], r["cin"], r["H"], r["W"], len(runs),
                   c1[-1]["bytes"], nd))
    res["run_layer"] = c1
    # R_RUN_UNIT 必须已经复位
    ru = unit.read(BE.CFG + 0x200) & 7
    log("  run_layer 后 R_RUN_UNIT=%d（期望 0）%s" % (ru, "PASS" if ru == 0 else "FAIL"))
    res["run_unit_restore"] = ru

    # ---- C1c. start 闸门：busy 时必须等；上一跑残留错误不能被覆盖 ----
    r0 = pick[0]
    dpa, opa = ee._cmd_addrs(prog.by_layer[r0["name"]][0])
    unit.err, unit.done, unit.busy = 3, 1, 0
    raised = False
    try:
        ee.run_one(dpa, opa, r0["cin"], r0["H"], r0["W"],
                   int(prog.by_layer[r0["name"]][0]["cfg_group"]))
    except BE.BinRunError:
        raised = True
    unit.err, unit.done, unit.busy = 0, 0, 0
    unit.busy, unit.busy_ticks = 1, 5
    okbusy = True
    try:
        ee.run_one(dpa, opa, r0["cin"], r0["H"], r0["W"],
                   int(prog.by_layer[r0["name"]][0]["cfg_group"]))
    except Exception:                                      # noqa: BLE001
        okbusy = False
    unit.busy = unit.busy_ticks = 0
    log("  start 闸门：残留 err 抛错=%s；busy 中等待后成功=%s"
        % (raised, okbusy))
    res["start_gate"] = dict(err_raised=raised, busy_waited=okbusy)

    # ---- C1b. run_all()：698 条命令一条不落，拍数/读拍与成本模型核对 ----
    total_words = sum(int(c["words"]) for c in prog.commands)
    ee._agg = dict(ncmd=0, seconds=0.0, rd=0, cyc=0, rdst=0, wrst=0, wr=0)
    w0, s0 = core._writes, core._writes_skipped
    t0 = time.time()
    runs = ee.run_all()
    agg = ee.dbg_summary()
    rd_exp = len(prog.commands) * 24 + 8 * total_words
    log("  run_all：%d 命令，rd=%d（期望 %d），cyc=%d（期望 21930932），"
        "wr=%d（期望 %d），MMIO 写 %d 次（影子跳过 %d），%.2f s"
        % (len(runs), agg["rd_beats"], rd_exp, agg["cycles"], agg["wr_beats"],
           total_words, core._writes - w0, core._writes_skipped - s0,
           time.time() - t0))
    res["run_all"] = dict(cmds=len(runs), rd=agg["rd_beats"], rd_exp=rd_exp,
                          cycles=agg["cycles"], cycles_exp=21930932,
                          wr=agg["wr_beats"], wr_exp=total_words,
                          seconds=time.time() - t0,
                          writes=core._writes - w0,
                          writes_skipped=core._writes_skipped - s0)

    # ---- C2. 平移：arena 放到 0x2000_0000 ----
    base2 = 0x2000_0000
    unit2 = FakeE1E2Unit()
    core2 = BE.BinConvEngine(unit2, pack_dir=P_PACK, verbose=False)
    ee2 = BE.E1E2Engine(core2, verbose=False)
    arena2 = BE.MemBuf(prog.size, base2)
    unit2.map_mem(base2, arena2)
    info2 = ee2.load_program(buf=arena2)
    r = pick[0]
    rng = np.random.default_rng(4242)
    fill_random_leaves(prog, arena2, r["name"], rng)
    # 期望用重定位后的描述符/地址，在 arena2 上重算
    d2, c2, s2 = prog.relocate(base2 - prog.base)
    exp2 = []
    for c in prog.by_layer[r["name"]]:
        i = int(c["i"])
        exp2.append(sim_command(arena2, int(c["cfg_c"]), int(c["cfg_h"]),
                                int(c["cfg_w"]), int(c["cfg_group"]),
                                d2[i * 8:(i + 1) * 8], info2["desc_pa"], base2))
    ee2.run_layer(r["name"])
    got2 = b"".join(bytes(arena2[int(c["out_off"]):int(c["out_off"]) + int(c["words"]) * 8])
                    for c in prog.by_layer[r["name"]])
    nd2 = sum(1 for a, b in zip(got2, b"".join(exp2)) if a != b)
    log("  平移 delta=%+#x：%s 命令=%d 字节=%d 差异=%d %s"
        % (info2["delta"], r["name"][-40:], len(exp2), len(got2), nd2,
           "PASS" if nd2 == 0 else "FAIL"))
    res["relocate"] = dict(delta=info2["delta"], diff=nd2, bytes=len(got2))

    # ---- C2b. 自动分配 arena（板端路径）：_pynq_allocate + 平移 ----
    unitA = FakeE1E2Unit()
    coreA = BE.BinConvEngine(unitA, pack_dir=P_PACK, verbose=False)
    eeA = BE.E1E2Engine(coreA, verbose=False)
    old_alloc = BE._pynq_allocate
    BE._pynq_allocate = Alloc(unitA, 0x3000_0000)
    try:
        infoA = eeA.load_program()
        okA = (infoA["arena_pa"] == 0x3000_0000
               and infoA["delta"] == 0x3000_0000 - prog.base
               and infoA["n_sentinel_refs"] == prog.n_sentinel_refs)
        log("  自动分配 arena：pa=%#x delta=%+#x 哨兵=%d %s"
            % (infoA["arena_pa"], infoA["delta"], infoA["n_sentinel_refs"],
               "PASS" if okA else "FAIL"))
        res["auto_arena"] = dict(arena_pa=infoA["arena_pa"], delta=infoA["delta"],
                                 sentinel=infoA["n_sentinel_refs"], ok=okA)
    finally:
        BE._pynq_allocate = old_alloc

    # ---- C3. pack_int8 vs C 内核（5 层：544/512/48/32/128²/8²/k1/k3） ----
    pack_pick = []
    for want in (lambda r: r["cin"] == 544,
                 lambda r: r["cin"] == 512,
                 lambda r: r["cin"] == 48,
                 lambda r: r["cin"] == 32 and r["H"] == 8,
                 lambda r: r["cin"] == 64 and r["H"] == 16):
        for r in rows:
            if want(r) and all(r["name"] != x["name"] for x in pack_pick):
                pack_pick.append(r)
                break
    unit3 = FakeE1E2Unit()
    core3 = BE.BinConvEngine(unit3, pack_dir=P_PACK, verbose=False)
    ee3 = BE.E1E2Engine(core3, verbose=False)
    c3 = []
    for n, r in enumerate(pack_pick):
        L = blob.layer_by_name(r["name"])
        tq = blob.params(L)["t_q"].astype(np.int64)
        cin, H, W = int(L["cin"]), int(r["H"]), int(r["W"])
        hw = H * W
        cb = (cin + 63) // 64
        need_in, need_out = cb * 64 * hw, cb * hw * 8
        need_thr, need_desc = cb * 64 * 2, cb * 64
        al = lambda z: (z + 63) // 64 * 64
        flat = BE.MemBuf(al(need_in) + al(need_out) + al(need_thr) + al(need_desc),
                         0x4000_0000)
        unit3.map_mem(0x4000_0000, flat)
        o = 0
        views = []
        for z in (need_in, need_out, need_thr, need_desc):
            views.append(BE.RegionBuffer(flat[o:o + z], 0x4000_0000 + o, flat))
            o += al(z)
        bufs = dict(zip(("in", "out", "thr", "desc"), views))
        rng = np.random.default_rng(1000 + n)
        xq = rng.integers(-128, 128, (cin, H, W), dtype=np.int8)
        got = ee3.pack_int8(xq, tq, bufs=bufs)
        exp = bpack.pack_i8(xq, (1 - tq).astype(np.int16)).view(np.uint8).tobytes()
        nd = sum(1 for a, b in zip(got, exp) if a != b)
        c3.append(dict(name=r["name"], cin=cin, H=H, W=W, cb=cb,
                       bytes=len(got), diff=nd))
        log("   pack_int8 %-44s cin=%-3d %dx%d cb=%d 字节=%d 差异=%d %s"
            % (r["name"][-44:], cin, H, W, cb, len(got), nd,
               "PASS" if nd == 0 else "FAIL"))
    res["pack_int8"] = c3

    # ---- C3b. cin 不是 8 的倍数（en 掩码/尾部补零）：合成 52 通道 ----
    cin, H, W = 52, 8, 8
    hw = H * W
    cb = (cin + 63) // 64
    al = lambda z: (z + 63) // 64 * 64
    need_in, need_out = cb * 64 * hw, cb * hw * 8
    need_thr, need_desc = cb * 64 * 2, cb * 64
    flat = BE.MemBuf(al(need_in) + al(need_out) + al(need_thr) + al(need_desc),
                     0x4100_0000)
    unit3.map_mem(0x4100_0000, flat)
    o = 0
    views = []
    for z in (need_in, need_out, need_thr, need_desc):
        views.append(BE.RegionBuffer(flat[o:o + z], 0x4100_0000 + o, flat))
        o += al(z)
    bufs = dict(zip(("in", "out", "thr", "desc"), views))
    rng = np.random.default_rng(5150)
    xq = rng.integers(-128, 128, (cin, H, W), dtype=np.int8)
    tq = rng.integers(-20, 14, cin, dtype=np.int64)
    got = ee3.pack_int8(xq, tq, bufs=bufs)
    exp = bpack.pack_i8(xq, (1 - tq).astype(np.int16)).view(np.uint8).tobytes()
    nd = sum(1 for a, b in zip(got, exp) if a != b)
    log("   pack_int8 合成 cin=52（非 8 倍数）：字节=%d 差异=%d %s"
        % (len(got), nd, "PASS" if nd == 0 else "FAIL"))
    res["pack_masked"] = dict(cin=cin, bytes=len(got), diff=nd)

    # ---- C4. BIN_PACK=e1e2 的 pack_input 分派（自动 enable + probe + pack） ----
    old_alloc = BE._pynq_allocate
    BE._pynq_allocate = Alloc(FakeE1E2Unit())
    try:
        unit4 = FakeE1E2Unit()
        BE._pynq_allocate = Alloc(unit4)
        core4 = BE.BinConvEngine(unit4, pack_dir=P_PACK, verbose=False)
        core4._pack_env = "e1e2"
        r = [x for x in rows if x["cin"] == 48 and x["H"] == 128][0]
        L = blob.layer_by_name(r["name"])
        fs = int(L["act_fs_in"])
        tq = blob.params(L)["t_q"]
        adj, ok = BE.pack_thresh(tq, fs)
        rng = np.random.default_rng(99)
        xf = (rng.random((int(L["cin"]), int(r["H"]), int(r["W"])), np.float32)
              * np.float32(2.0) - np.float32(0.5))
        adj3 = np.asarray(adj, np.float32).reshape(int(L["cin"]), 1, 1)
        xf[:, 0::7, :] = adj3
        xf[:, 1::7, :] = np.nextafter(adj3, np.float32(-np.inf))
        xf[:, 2::7, :] = np.nextafter(adj3, np.float32(np.inf))
        got = core4.pack_input(xf, BE.BinLayer(L, int(r["H"]), int(r["W"])))
        exp = bpack.pack_f32(xf, adj).view(np.uint8).tobytes()
        nd = sum(1 for a, b in zip(got, exp) if a != b)
        log("   BIN_PACK=e1e2 pack_input %s：字节=%d 差异=%d %s"
            % (r["name"][-40:], len(got), nd, "PASS" if nd == 0 else "FAIL"))
        core4.disable_e1e2()
        ru = unit4.read(BE.CFG + 0x200) & 7
        res["pack_input"] = dict(diff=nd, bytes=len(got), run_unit=ru)

        # C4b. 旧比特流（0x200 读恒 0）：必须拒绝，且不能把失败的引擎挂上
        class OldUnit(FakeE1E2Unit):
            def read(self, off):
                if ((int(off) - BE.CFG) & 0x3FF) == 0x200:
                    return 0
                return super().read(off)
        core5 = BE.BinConvEngine(OldUnit(), pack_dir=P_PACK, verbose=False)
        core5._pack_env = "e1e2"
        refused = False
        try:
            core5.pack_input(xf, BE.BinLayer(L, int(r["H"]), int(r["W"])))
        except RuntimeError:
            refused = True
        log("   旧比特流拒绝：%s；失败后未挂引擎：%s"
            % (refused, core5._e1e2 is None))
        res["old_bitstream"] = dict(refused=refused,
                                    detached=(core5._e1e2 is None))
    finally:
        BE._pynq_allocate = old_alloc
    return res


# ============================================================================
# 4. 落盘
# ============================================================================
def emit(prog, rows_a, res_a, res_b, res_c, log):
    os.makedirs(OUTDIR, exist_ok=True)
    # A 全量 TSV
    with open(os.path.join(OUTDIR, "layers_a.tsv"), "w", encoding="utf-8") as f:
        f.write("idx\tlayer\tcin\tcout\tk\tH\tW\tcb\tncmd\tbytes\td_segs\tsegs\tdiff\n")
        for r in res_a["rows"]:
            f.write("%d\t%s\t%d\t%d\t%d\t%d\t%d\t%d\t%d\t%d\t%s\t%d\t%d\n"
                    % (r["idx"], r["name"], r["cin"], r["cout"], r["k"],
                       r["H"], r["W"], r["cb"], r["ncmd"], r["bytes"],
                       ",".join(map(str, r["d"])), r["segs"], r["diff"]))
    # 选 48 行做 markdown
    picks = [r for r in res_a["rows"] if r["cin"] == 544]   # 9 个 cb=9 全部进表
    seen = {(r["H"], r["k"], r["cin"]) for r in picks}
    for r in res_a["rows"]:
        if len(picks) >= 48:
            break
        key = (r["H"], r["k"], r["cin"])
        if key not in seen:
            picks.append(r)
            seen.add(key)
    for r in res_a["rows"]:
        if len(picks) >= 48:
            break
        if all(r["idx"] != q["idx"] for q in picks):
            picks.append(r)
    picks = sorted(picks, key=lambda r: r["idx"])
    md = []
    md.append("# E1E2 离线逐字节验证（%s）\n" % time.strftime("%Y-%m-%d %H:%M"))
    md.append("## A. 描述符路径 vs C 内核\n")
    md.append("全部 **%d 层 / %d 命令 / %.3f MB**，差异 **%d**。\n"
              % (len(res_a["rows"]), res_a["cmds"], res_a["bytes"] / 1e6,
                 res_a["diff"]))
    md.append("| 层号 | 层 | cin | k | H×W | cb | 命令 | 字节 | 差异 |")
    md.append("|---:|---|---:|---:|---|---:|---:|---:|---:|")
    for r in picks:
        md.append("| %d | `%s` | %d | %d | %d×%d | %d | %d | %d | **%d** |"
                  % (r["idx"], r["name"], r["cin"], r["k"], r["H"], r["W"],
                     r["cb"], r["ncmd"], r["bytes"], r["diff"]))
    md.append("\n（全量 227 行见 `layers_a.tsv`；表中按形状/通道/段数抽样 48 行，"
              "包含全部 9 个 cin=544 层。）\n")
    if not res_b.get("skipped"):
        md.append("## B. 复现 ab_pack256 三列 sha1\n")
        md.append("全段 d=0 的 **%d 层**，sha1 一致 **%d**，不一致 **%d**。\n"
                  % (res_b["total"], res_b["ok"], res_b["bad"]))
        md.append("| ab 行 | 层 | cin | H×W | k | sha1（本次 = ref = numpy = C） |")
        md.append("|---:|---|---:|---|---:|---|")
        for r in res_b["rows"][::max(1, len(res_b["rows"]) // 30)][:30]:
            md.append("| %d | `%s` | %d | %d×%d | %d | `%s` |"
                      % (r["i"], r["name"], r["cin"], r["H"], r["W"], r["k"],
                         r["sha"]))
        md.append("")
    md.append("## C. 驱动代码（寄存器级模型）\n")
    if not res_c:
        md.append("（本次 `--no-drv` 跳过）\n")
    else:
        md.append("| 项 | 结果 |")
        md.append("|---|---|")
        md.append("| R_RUN_UNIT 写 4/读回、写 0/读回 | `%#x` / `%#x` |"
                  % (res_c["probe"]["write4"], res_c["probe"]["back0"]))
        da = res_c["default_ab"]
        if not da.get("skipped"):
            md.append("| 默认路径 `pack_input` vs 改动前 | %d 层，差异 %d |"
                      % (len(da["rows"]), sum(r["diff"] for r in da["rows"])))
        ob = res_c["old_bitstream"]
        md.append("| 旧比特流拒绝 / 不挂失败引擎 | %s / %s |"
                  % ("PASS" if ob["refused"] else "FAIL",
                     "PASS" if ob["detached"] else "FAIL"))
        sg = res_c["start_gate"]
        md.append("| start 闸门（busy 等待 / 残留错误） | %s / %s |"
                  % ("PASS" if sg["busy_waited"] else "FAIL",
                     "PASS" if sg["err_raised"] else "FAIL"))
        ra = res_c["run_all"]
        md.append("| run_all 698 命令 | rd=%d/%d、cyc=%d/%d、wr=%d/%d |"
                  % (ra["rd"], ra["rd_exp"], ra["cycles"], ra["cycles_exp"],
                     ra["wr"], ra["wr_exp"]))
        md.append("| load_program 平移 delta | `%+#x` |" % res_c["relocate"]["delta"])
        md.append("| 自动分配 arena（板端路径） | pa=%#x、delta=%+#x、哨兵 %d |"
                  % (res_c["auto_arena"]["arena_pa"], res_c["auto_arena"]["delta"],
                     res_c["auto_arena"]["sentinel"]))
        md.append("| 平移后 run_layer | %d 字节，差异 %d |"
                  % (res_c["relocate"]["bytes"], res_c["relocate"]["diff"]))
        for r in res_c["run_layer"]:
            md.append("| run_layer `%s` | 命令 %d / 字节 %d / 差异 %d |"
                      % (r["name"], r["cmds"], r["bytes"], r["diff"]))
        for r in res_c["pack_int8"]:
            md.append("| pack_int8 `%s` | cin=%d %d×%d 字节 %d / 差异 %d |"
                      % (r["name"], r["cin"], r["H"], r["W"], r["bytes"], r["diff"]))
        md.append("| pack_int8 合成 cin=52（非 8 倍数） | 字节 %d / 差异 %d |"
                  % (res_c["pack_masked"]["bytes"], res_c["pack_masked"]["diff"]))
        md.append("| BIN_PACK=e1e2 pack_input | 字节 %d / 差异 %d / RUN_UNIT %d |"
                  % (res_c["pack_input"]["bytes"], res_c["pack_input"]["diff"],
                     res_c["pack_input"]["run_unit"]))
    md.append("")
    md.append("## 结论\n")
    okA = res_a["diff"] == 0
    okB = res_b.get("skipped") or (res_b["ok"] == res_b["total"])
    okC = (not res_c) or (res_c["relocate"]["diff"] == 0
           and all(r["diff"] == 0 for r in res_c["run_layer"])
           and all(r["diff"] == 0 for r in res_c["pack_int8"])
           and res_c["pack_masked"]["diff"] == 0
           and res_c["pack_input"]["diff"] == 0
           and res_c["run_unit_restore"] == 0
           and (res_c["default_ab"].get("skipped")
                or all(r["diff"] == 0 for r in res_c["default_ab"]["rows"]))
           and res_c["run_all"]["rd"] == res_c["run_all"]["rd_exp"]
           and res_c["run_all"]["cycles"] == res_c["run_all"]["cycles_exp"])
    md.append("- A 描述符 vs C：%s" % ("**PASS**（0 差异）" if okA else "**FAIL**"))
    md.append("- B ab_pack sha1：%s" % ("**PASS**（三列全中）" if okB else "**FAIL**"))
    md.append("- C 驱动寄存器序列：%s" % ("**PASS**" if okC else "**FAIL**"))
    md.append("")
    with open(os.path.join(OUTDIR, "verify_e1e2.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    with open(os.path.join(OUTDIR, "verify_e1e2.json"), "w", encoding="utf-8") as f:
        json.dump(dict(gen="verify_e1e2_path.py", when=time.strftime("%Y-%m-%d %H:%M:%S"),
                       program=dict(dir=prog.dir, base=prog.base, size=prog.size,
                                    ncmd=prog.ncmd, nlayers=len(prog.by_layer),
                                    sentinel_pa=prog.sentinel_pa,
                                    sentinel_refs=prog.n_sentinel_refs),
                       a=res_a, b=res_b, c=res_c), f, indent=1, ensure_ascii=False)
    log("落盘：%s" % OUTDIR)


def main(argv=None):
    ap = argparse.ArgumentParser(description="E1E2 驱动的离线逐字节等价性证明")
    ap.add_argument("--layers", type=int, default=0, help="A 只跑前 N 层（0=全部 227）")
    ap.add_argument("--no-ab", action="store_true", help="跳过 B")
    ap.add_argument("--no-drv", action="store_true", help="跳过 C")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    t_all = time.time()

    def log(s):
        if not a.quiet or s.startswith(("==", "  ⇒", "落盘", "  ✗")):
            print(s, flush=True)

    log("== E1E2 离线验证：%s ==" % os.path.join(PSCODE, "deploy", "e1e2_desc"))
    prog = BE.E1E2Program(verbose=False)
    blob = Blob(P_PACK)
    rows = analyze_layers(prog, blob)
    log("资产：%d 命令 / %d 层 / %.2f MB arena；%d 个全段 d=0 层"
        % (prog.ncmd, len(rows), prog.size / 1e6, sum(1 for r in rows if r["all_d0"])))
    asset = check_assets(prog, blob, rows, log)

    mem = np.zeros(prog.size, np.uint8)
    res_a = part_a(prog, blob, rows, mem, log, limit=a.layers)
    res_b = dict(ok=0, total=0, bad=0, skipped=True)
    if not a.no_ab:
        res_b = part_b(prog, blob, rows, mem, log)
    res_c = {}
    if not a.no_drv:
        res_c = part_c(prog, blob, rows, log, quiet=a.quiet)
    emit(prog, rows, res_a, res_b, res_c, log)

    okA = res_a["diff"] == 0
    okB = res_b.get("skipped") or res_b["ok"] == res_b["total"]
    okC = (not res_c or (res_c["relocate"]["diff"] == 0
                         and all(r["diff"] == 0 for r in res_c["run_layer"])
                         and all(r["diff"] == 0 for r in res_c["pack_int8"])
           and res_c["pack_masked"]["diff"] == 0
                         and res_c["pack_input"]["diff"] == 0
                         and res_c["run_unit_restore"] == 0))
    log("")
    log("== 汇总（%.1f s）==" % (time.time() - t_all))
    log("  A 描述符 vs C 内核：%s（%d 层 / %d 命令 / %d 差异）"
        % ("PASS" if okA else "FAIL", len(res_a["rows"]), res_a["cmds"], res_a["diff"]))
    if not res_b.get("skipped"):
        log("  B ab_pack256 sha1：%s（%d/%d）"
            % ("PASS" if okB else "FAIL", res_b["ok"], res_b["total"]))
    if res_c:
        log("  C 驱动：%s" % ("PASS" if okC else "FAIL"))
    log("  ⇒ %s" % ("全部 PASS" if (okA and okB and okC) else "有失败项"))
    return 0 if (okA and okB and okC) else 1


if __name__ == "__main__":
    sys.exit(main())
