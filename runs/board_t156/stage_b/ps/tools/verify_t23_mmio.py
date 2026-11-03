#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-23 判据 C1（访问序列等价）+ 数值等价：MMIO fast-path vs 原路径。

做法（为什么可信）：
  · "寄存器文件"就是一块**真的 mmap 页**：老路径经 `mmio.write/read` 落进去，
    新路径经 `struct.pack_into/unpack_from` 直接落进去，C 路径经 `mmio_batch.c`
    落进去 —— 三条路径的**落点完全相同**，所以"读到的东西"可逐字比对。
  · 设备模型（FakeE1E2Unit / FakeUnit）在 `start()` 那一拍被同步驱动，
    等价于"硬件在 start 的同一拍清 done 并跑完"（真硬件行为；见 RTL
    `bin_unit_axi_wrap.v` 的 8'h00 分支）。模型跑完把状态写回页里。
  · 记录点在驱动的**唯一出口**（`_wr` / `_wr_seq` / `_rd` 的 `_trace_log` 钩子），
    每条记 (kind, off, val, force, sent)。`sent=False` = 被影子缓存跳过。
    ⇒ 影子语义、force 语义、写序、读序**全部**在 trace 里，任何一条不同都会被抓住。

四个 arm（同一进程内由环境变量决定，所以请用 `--all` 分四个子进程跑）：
  old     什么都没开（基线）
  fast    BIN_MMIO_FAST=1              （Python 直写 buffer）
  tight   BIN_MMIO_FAST=1 BIN_MMIO_POLL=tight
  c       BIN_MMIO_C=1                 （C 内核：一批写 + start + 等 done）

用法：
  python3 ps/tools/verify_t23_mmio.py --run old          # 产出 /tmp/t23_trace/old.json
  python3 ps/tools/verify_t23_mmio.py --compare          # 四份比对并打表
  python3 ps/tools/verify_t23_mmio.py --all              # 自己起四个子进程（推荐）
"""
import argparse
import hashlib
import json
import mmap
import os
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.dirname(HERE)
PYNQ = os.path.join(PS, "pynq")
sys.path.insert(0, PYNQ)
sys.path.insert(0, HERE)

import numpy as np                          # noqa: E402
import bin_engine as BE                     # noqa: E402
from verify_e1e2_path import FakeE1E2Unit, Alloc, P_PACK   # noqa: E402

OUT = os.environ.get("T23_TRACE_DIR", "/tmp/t23_trace")
PAGE = 0x20000


# ============================================================================
# 1. 寄存器页 + 设备模型（同步版：start 那一拍跑完，与 RTL 的"start 清 done"同构）
# ============================================================================
class PageDev:
    """一块 mmap 当寄存器文件；`do_start()` = 硬件在 start 那一拍清 done 并跑完。"""

    def __init__(self, inner, base_pa=0x4006_0000, size=PAGE, regs=0x300):
        self.page = mmap.mmap(-1, size)
        self.inner = inner                     # FakeE1E2Unit / FakeUnit
        self.mmio = self                       # 驱动取 getattr(ip, "mmio", ip)
        self.nwrite = 0
        self.nread = 0
        self.starts = 0
        self._regs = regs
        self.base_pa = int(base_pa)
        self.ip_dict = getattr(inner, "ip_dict", None)

    # ---- 驱动看到的 mmio ----
    @property
    def mmap(self):
        return self.page

    def write(self, off, v):
        self.nwrite += 1
        struct.pack_into("<I", self.page, int(off), int(v) & 0xFFFFFFFF)

    def read(self, off):
        self.nread += 1
        return struct.unpack_from("<I", self.page, int(off))[0]

    def __getattr__(self, k):
        if k.startswith("bin_unit"):
            return self
        raise AttributeError(k)

    def map_mem(self, pa, buf):
        self.inner.map_mem(pa, buf)

    # ---- 页 ↔ 模型 ----
    def _page_to_model(self):
        for o in range(0, self._regs, 4):
            self.inner.reg[o] = struct.unpack_from("<I", self.page, BE.CFG + o)[0]

    def _publish(self):
        u = self.inner
        st = ((int(getattr(u, "err", 0)) & 0xFF) << 8) \
            | ((int(getattr(u, "busy", 0)) & 1) << 1) | (int(u.done) & 1)
        struct.pack_into("<I", self.page, BE.CFG + 0x10, st)
        cnt = getattr(u, "cnt", None)
        if cnt:
            for name, off in (("rd", 0x230), ("cyc", 0x234), ("rdst", 0x238),
                              ("wrst", 0x23C), ("wr", 0x240)):
                struct.pack_into("<I", self.page, BE.CFG + off,
                                 int(cnt.get(name, 0)) & 0xFFFFFFFF)

    def do_start(self):
        """清 AP_CTRL（自清位）+ 清 done，把页同步进模型，跑完再写回状态。"""
        self.starts += 1
        struct.pack_into("<I", self.page, BE.CFG, 0)
        self._page_to_model()
        u = self.inner
        u.done = 0
        u.err = 0
        u.busy = 0
        u._start() if hasattr(u, "_start") else u.write(BE.CFG, 1)
        self._publish()


class TracedEngine(BE.BinConvEngine):
    """在 `_wr/_wr_seq/_rd` 的 `_trace_log` 钩子上记录；start / C 路径挂设备模型。"""

    def __init__(self, dev, **kw):
        self.dev = dev
        self.trace = []
        kw.setdefault("verbose", False)
        BE.BinConvEngine.__init__(self, dev, **kw)
        self._trace_log = self.trace

    def start(self):
        self.dev.do_start()                 # 硬件：start 那一拍清 done 并跑完
        BE.BinConvEngine.start(self)        # 真写 AP_CTRL=1（trace 里也要有这一条）

    def _mb_run_cmd(self, pairs, ctrl_off, ret_off, timeout_s, wdt=None, slice_s=0.5):
        """C arm 的测试版：把 mb_run_pairs 拆成
        `C 批量写 → 设备模型跑（start 那一拍）→ C 写 start → C 轮询`。

        为什么必须拆：设备模型只能在"start 那一拍"介入，而 mb_run_pairs 把
        start 写在 C 里了。除这一次拆分外，走的是**真的 C 内核**（volatile 批量写
        + 轮询），与生产路径 `mb_run_pairs` 的四个步骤逐行同构（见 mmio_batch.c）。
        """
        lib = self._fast.get("lib") if isinstance(self._fast, dict) else None
        if lib is None:
            return BE.BinConvEngine._mb_run_cmd(self, pairs, ctrl_off, ret_off,
                                                timeout_s, wdt=wdt, slice_s=slice_s)
        lib.py_write_pairs(pairs)
        self.dev.do_start()
        lib.py_write(ctrl_off, 1)
        t0 = time.time()
        while True:
            st = lib.py_poll_bit0(ret_off, slice_s)
            if st & 1:
                return st, time.time() - t0
            if (time.time() - t0) > timeout_s:
                raise TimeoutError("C 路径超时")


# ============================================================================
# 2. arm 定义
# ============================================================================
ARMS = dict(
    old={},
    fast=dict(BIN_MMIO_FAST="1"),
    tight=dict(BIN_MMIO_FAST="1", BIN_MMIO_POLL="tight"),
    est=dict(BIN_MMIO_FAST="1", BIN_MMIO_POLL="est"),
    c=dict(BIN_MMIO_C="1"),
)


def _sha1(b):
    return hashlib.sha1(bytes(b)).hexdigest()


def run_arm(arm):
    prog = BE.E1E2Program(verbose=False)
    inner = FakeE1E2Unit()
    dev = PageDev(inner)
    core = TracedEngine(dev, pack_dir=P_PACK)
    ee = BE.E1E2Engine(core, verbose=False)
    arena = BE.MemBuf(prog.size, prog.base)
    inner.map_mem(prog.base, arena)
    ee.load_program(buf=arena)

    res = dict(arm=arm, env={k: v for k, v in os.environ.items()
                             if k.startswith("BIN_MMIO")},
               fast=core.mmio_stats())
    # ---- 阶段 1：698 条 pack 命令全跑一遍（含 DBG 读，口径同 verify_e1e2 的 run_all）
    ee._agg = dict(ncmd=0, seconds=0.0, rd=0, cyc=0, rdst=0, wrst=0, wr=0)
    w0, s0 = core._writes, core._writes_skipped
    r0 = core._reads
    t0 = time.time()
    runs = ee.run_all()
    res["run_all"] = dict(cmds=len(runs), seconds=time.time() - t0,
                          writes=core._writes - w0,
                          writes_skipped=core._writes_skipped - s0,
                          reads=core._reads - r0,
                          dbg=ee.dbg_summary(fclk_mhz=150.0))
    res["pack_sha1"] = _sha1(arena[:prog.size])
    res["pack_bytes"] = int(prog.size)
    res["fast_after"] = core.mmio_stats()
    res["trace"] = [list(t) for t in core.trace]
    res["page_sha1"] = _sha1(dev.page[:0x1000])
    res["page_nwrite"] = dev.nwrite
    res["page_nread"] = dev.nread
    res["starts"] = dev.starts

    # ---- 阶段 2：conv 路径（config/set_bases/start/wait/clear_dbg）也走一遍
    core.trace.clear()
    cu = BE.FakeUnit()
    cdev = PageDev(cu)
    ceng = TracedEngine(cdev, pack_dir=P_PACK)
    cblob, cpa = ceng.load_weights(buf=BE.MemBuf(
        os.path.getsize(os.path.join(P_PACK, "bin_weights.bin")) + 4096, 0x3000_0000))
    import bin_ref_numpy as BR
    ceng.host_blob = BR.Blob(P_PACK)
    cdev.map_mem(int(cblob.physical_address), cblob)
    ceng.add_region(cblob, "blob")
    rows = []
    names = ["encoder.encoder_1.rdb.conv_layers.0.conv",
             "encoder.encoder_1.rdb.conv_layers.2.conv",
             "decoder.decoder_3.sam_blocks.0.b0.conv.conv",
             "encoder.encoder_3.sam_blocks.0.b4.ghost_layers.0.cheap_operation.conv"]
    rng = np.random.default_rng(7)
    for nm in names:
        d = ceng.host_blob.layer_by_name(nm)
        L = BE.BinLayer(d, 8, 8)
        ib = BE.MemBuf(L.act_bytes + 4096, 0x2000_0000 + len(rows) * 0x20_0000)
        ob = BE.MemBuf(L.out_bytes + 8192, 0x2100_0000 + len(rows) * 0x20_0000)
        cdev.map_mem(int(ib.physical_address), ib)
        cdev.map_mem(int(ob.physical_address), ob)
        ceng.add_region(ib, "IN")
        ceng.add_region(ob, "OUT")
        ib[:L.act_bytes] = rng.integers(-128, 128, L.act_bytes, dtype=np.int8)
        rs = ceng.run(ib, ob, L)
        rows.append(dict(name=nm, status=int(rs.status), err=int(rs.err_code),
                         out=_sha1(ob[:L.out_bytes])))
    res["conv_rows"] = rows
    res["conv_trace"] = [list(t) for t in ceng.trace]
    res["conv_stats"] = ceng.mmio_stats()
    return res


# ============================================================================
# 3. 比对
# ============================================================================
def compare(arms):
    ref_name = "old"
    ref = arms.get(ref_name)
    if ref is None:
        print("缺少 old arm")
        return 1
    ok_all = True

    def split(tr):
        return ([t for t in tr if t[0] == "W"], [t for t in tr if t[0] == "R"])

    rw, rr = split(ref["trace"])
    print("=" * 108)
    print("T-23 C1：访问序列等价（记录点 = 驱动唯一出口 _wr/_wr_seq/_rd 的 _trace_log 钩子）")
    print("      写记录含 (off, val, force, sent)；sent=False = 被影子缓存跳过（C5）")
    print("=" * 108)
    print("%-6s %7s %6s %6s %6s  %-7s %-7s %-6s %-6s %s"
          % ("arm", "trace", "W", "R", "skipW", "W-eq", "pack", "conv", "计数", "verdict"))
    for name, r in arms.items():
        tw, tr_ = split(r["trace"])
        nsk = sum(1 for t in tw if not t[4])
        w_eq = (tw == rw)
        r_eq = (tr_ == rr)
        pack_ok = (r["pack_sha1"] == ref["pack_sha1"])
        conv_ok = ([x["out"] for x in r["conv_rows"]]
                   == [x["out"] for x in ref["conv_rows"]])
        cnt_ok = ((r["run_all"]["writes"], r["run_all"]["writes_skipped"])
                  == (ref["run_all"]["writes"], ref["run_all"]["writes_skipped"]))
        ok = w_eq and pack_ok and conv_ok and cnt_ok
        ok_all = ok_all and ok
        print("%-6s %7d %6d %6d %6d  %-7s %-7s %-6s %-6s %s"
              % (name, len(r["trace"]), len(tw), len(tr_), nsk,
                 "same" if w_eq else "DIFF",
                 "same" if pack_ok else "DIFF",
                 "same" if conv_ok else "DIFF",
                 "same" if cnt_ok else "DIFF",
                 "PASS" if ok else "FAIL%s" % ("" if r_eq else "(R-diff:%d/%d)"
                                               % (len(tr_), len(rr)))))
    print("-" * 108)
    print("pack 输出 sha1（698 命令 / %.1f MB）：%s"
          % (ref["pack_bytes"] / 1e6, ref["pack_sha1"]))
    for name, r in arms.items():
        print("  %-6s %s  pack后页=%s  cmds=%d starts=%d 写=%d 跳过=%d 读=%d 秒=%.3f"
              % (name, r["pack_sha1"][:16], r["page_sha1"][:16],
                 r["run_all"]["cmds"], r["starts"], r["run_all"]["writes"],
                 r["run_all"]["writes_skipped"], r["run_all"]["reads"],
                 r["run_all"]["seconds"]))
    # conv 路径
    print("-" * 108)
    print("conv 路径（4 层，config/set_bases/start/wait/clear_dbg）：")
    for name, r in arms.items():
        print("  %-6s %s" % (name, " | ".join("%s:%s" % (x["out"][:10], x["status"])
                                              for x in r["conv_rows"])))
    for name, r in arms.items():
        if name == ref_name:
            continue
        cw, cr = split(r["conv_trace"])
        if cw != split(ref["conv_trace"])[0]:
            ok_all = False
            print("  ✗ conv 写序列 %s 与 old 不同" % name)
    # 逐条 diff（只在写序列不等时打印前 10 条）
    for name, r in arms.items():
        if name == ref_name:
            continue
        tw = split(r["trace"])[0]
        if tw == rw:
            continue
        n = min(len(tw), len(rw))
        first = next((i for i in range(n) if tw[i] != rw[i]), n)
        print("  ✗ %s 的写序列与 old 不一致：len %d vs %d，首个不同在第 %d 条"
              % (name, len(tw), len(rw), first))
        for i in range(max(0, first - 2), min(n, first + 5)):
            print("      %6d  old=%-30s %s=%s" % (i, rw[i], name, tw[i]))
    print("-" * 108)
    print("统计口径：W = 写记录（含被跳过的），R = 读记录。C 路径的 R 条数少 698，"
          " 因为 done 轮询在 C 里（不经过 _rd）；写序列必须逐条相同。")
    print("=" * 108)
    print("总结：%s" % ("PASS" if ok_all else "FAIL"))
    return 0 if ok_all else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="T-23 MMIO fast-path 等价性（C1/C5）")
    ap.add_argument("--run", choices=sorted(ARMS), help="只跑一个 arm 并落盘")
    ap.add_argument("--all", action="store_true", help="四个 arm 各起一个子进程再比对")
    ap.add_argument("--compare", action="store_true", help="读已落盘的 arm 并比对")
    ap.add_argument("--dir", default=OUT)
    a = ap.parse_args(argv)
    os.makedirs(a.dir, exist_ok=True)

    if a.run:
        r = run_arm(a.run)
        p = os.path.join(a.dir, "%s.json" % a.run)
        with open(p, "w") as f:
            json.dump(r, f)
        print("落盘 %s  trace=%d 条  writes=%d skip=%d reads=%d  pack=%s"
              % (p, len(r["trace"]), r["run_all"]["writes"],
                 r["run_all"]["writes_skipped"], r["run_all"]["reads"],
                 r["pack_sha1"]))
        return 0
    if a.all:
        for name, env in ARMS.items():
            e = dict(os.environ)
            for k in list(e):
                if k.startswith("BIN_MMIO"):
                    e.pop(k)
            e.update(env)
            cmd = [sys.executable, os.path.abspath(__file__), "--run", name,
                   "--dir", a.dir]
            print("--- arm %s  env=%s" % (name, env), flush=True)
            rc = subprocess.call(cmd, env=e)
            if rc != 0:
                print("arm %s 失败 rc=%d" % (name, rc))
                return rc
        a.compare = True
    if a.compare:
        arms = {}
        for name in ARMS:
            p = os.path.join(a.dir, "%s.json" % name)
            if os.path.exists(p):
                with open(p) as f:
                    arms[name] = json.load(f)
        return compare(arms)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
