#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_cma.py —— ① 「今天 CMA 被谁占着」取证 + ② 态 A（同配置 256² numpy 臂）

设计原则：**只读 + 只测量**，不改冻结点代码一个字节。
    - 冻结点代码从 `/home/xilinx/demoir/ps/pynq_frozen` import（第一路径，不可绕过）；
    - 唯一的"改"是运行期把 `bin_engine._PROF` 置真（HEAD 里只累积不输出，
      所以我直接读 `eng._prof` 与 `net._prep_prof`，不改源码）；
    - 整网驱动是 `V.run_net` 的**薄拷贝**（只为把 eng/net 引用留下来），
      忠实性判据 = sha1 三件套必须等于 `V.FROZEN[256]`，且另跑一次**原** `V.run_net` 交叉核对。

输出：`/tmp/probe_cma/probe_cma_stateA.json`（全部原始数据，含每项口径）

用法（板端；本进程只 download=True 一次）：
    echo xilinx | sudo -S -p '' env XILINX_XRT=/usr \
      /usr/local/share/pynq-venv/bin/python3 -u /tmp/probe_cma/probe_cma.py --download
"""
from __future__ import annotations

import argparse
import gc
import glob
import hashlib
import json
import os
import subprocess
import sys
import time

FROZEN = "/home/xilinx/demoir/ps/pynq_frozen"
sys.path.insert(0, FROZEN)
os.environ.setdefault("BIN_PROFILE", "1")      # HEAD 的 _PROF 在 import 时读；我们还会显式改

import numpy as np                             # noqa: E402

OUTDIR = "/tmp/probe_cma"
R = {"stamp": time.strftime("%Y-%m-%d %H:%M:%S"), "sections": {}}


def say(s=""):
    print(s, flush=True)


def hdr(s):
    say("")
    say("=" * 78)
    say("  " + s)
    say("=" * 78)


# ---------------------------------------------------------------- 基础采样
def meminfo():
    d = {}
    for ln in open("/proc/meminfo"):
        k, _, rest = ln.partition(":")
        v = rest.split()
        if v:
            try:
                d[k] = int(v[0])
            except ValueError:
                pass
    return d


def cma_mb():
    d = meminfo()
    return dict(total_mb=round(d.get("CmaTotal", 0) / 1024.0, 3),
                free_mb=round(d.get("CmaFree", 0) / 1024.0, 3))


def mem_summary():
    d = meminfo()
    keys = ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached",
            "SwapCached", "Active", "Inactive", "Unevictable", "Mlocked",
            "SwapTotal", "Dirty", "Writeback", "AnonPages", "Mapped", "Shmem",
            "Slab", "SReclaimable", "SUnreclaim", "PageTables",
            "CmaTotal", "CmaFree")
    return {k: round(d.get(k, 0) / 1024.0, 2) for k in keys}      # 一律 MB


def cpufreq():
    out = {}
    for p in sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")):
        cd = os.path.dirname(p)
        core = cd.split("/")[-2]
        def rd(n):
            q = os.path.join(cd, n)
            try:
                return open(q).read().strip()
            except Exception:                                  # noqa: BLE001
                return None
        cur, mx = rd("scaling_cur_freq"), rd("cpuinfo_max_freq")
        out[core] = dict(cur_mhz=(round(int(cur) / 1000.0, 1) if cur else None),
                         max_mhz=(round(int(mx) / 1000.0, 1) if mx else None),
                         governor=rd("scaling_governor"),
                         min_mhz=(round(int(rd("scaling_min_freq")) / 1000.0, 1)
                                  if rd("scaling_min_freq") else None))
    return out


def buddy():
    out = {}
    try:
        for ln in open("/proc/buddyinfo"):
            f = ln.split()
            node, zone = f[0] + " " + f[1], f[3]
            orders = [int(x) for x in f[4:]]
            out["%s %s" % (f[1], zone)] = orders
    except Exception as ex:                                    # noqa: BLE001
        out["err"] = repr(ex)
    return out


def top_procs(n=18):
    rows = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            st = open("/proc/%s/status" % pid).read()
            rss = name = None
            for ln in st.splitlines():
                if ln.startswith("VmRSS:"):
                    rss = int(ln.split()[1])
                elif ln.startswith("Name:"):
                    name = ln.split("\t")[-1].strip()
            try:
                cmd = open("/proc/%s/cmdline" % pid, "rb").read()
                cmd = cmd.replace(b"\x00", b" ").decode("utf8", "replace").strip()[:160]
            except Exception:                                  # noqa: BLE001
                cmd = ""
            rows.append(dict(pid=int(pid), name=name, rss_kb=rss or 0, cmd=cmd))
        except Exception:                                      # noqa: BLE001
            pass
    rows.sort(key=lambda r: -r["rss_kb"])
    tot = sum(r["rss_kb"] for r in rows) / 1024.0
    return dict(top=rows[:n], n_proc=len(rows),
                rss_sum_mb=round(tot, 1),
                rss_top_mb=round(sum(r["rss_kb"] for r in rows[:n]) / 1024.0, 1))


def cma_debug():
    base = "/sys/kernel/debug/cma"
    if not os.path.isdir(base):
        return dict(exists=False, note="没有 /sys/kernel/debug/cma（CMA 区不是 named region）")
    reg = {}
    for r in os.listdir(base):
        d = {}
        for f in ("size", "count", "allocated", "free"):
            p = os.path.join(base, r, f)
            if os.path.exists(p):
                try:
                    d[f] = open(p).read().strip()
                except Exception as ex:                        # noqa: BLE001
                    d[f] = "ERR:%r" % (ex,)
        reg[r] = d
    return dict(exists=True, regions=reg)


def dmesg_cma():
    try:
        out = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=40).stdout
    except Exception as ex:                                    # noqa: BLE001
        return dict(err=repr(ex))
    low = out.lower().splitlines()
    keep = [ln for ln in low if ("cma" in ln or "reserved" in ln or "memory:" in ln
                                 or "cma_reserve" in ln)]
    return dict(lines=keep[:60], n_all=len(low))


def iomem():
    try:
        lines = open("/proc/iomem").read().splitlines()
        return [ln for ln in lines if ("System RAM" in ln or "reserved" in ln or "dma" in ln.lower()
                                       or "cma" in ln.lower())][:40]
    except Exception as ex:                                    # noqa: BLE001
        return ["ERR:%r" % (ex,)]


def uptime():
    try:
        u = float(open("/proc/uptime").read().split()[0])
        return dict(up_s=round(u, 1), up_h=round(u / 3600.0, 3),
                    loadavg=[round(float(x), 2) for x in open("/proc/loadavg").read().split()[:3]])
    except Exception as ex:                                    # noqa: BLE001
        return dict(err=repr(ex))


# ---------------------------------------------------------------- pagemap / kpageflags
PS = 4096
MASK55 = (1 << 55) - 1


def pagemap_runs(va, nbytes):
    """这块缓冲的**物理页连续性**：n_runs=1 就是物理连续（CMA 的特征）。"""
    a0 = va & ~(PS - 1)
    a1 = (va + int(nbytes) + PS - 1) & ~(PS - 1)
    n = (a1 - a0) // PS
    if n <= 0:
        return dict(err="empty")
    fd = os.open("/proc/self/pagemap", os.O_RDONLY)
    try:
        os.lseek(fd, (a0 // PS) * 8, os.SEEK_SET)
        raw = b""
        want = n * 8
        while len(raw) < want:
            c = os.read(fd, min(1 << 20, want - len(raw)))
            if not c:
                break
            raw += c
    finally:
        os.close(fd)
    ent = [int.from_bytes(raw[i * 8:i * 8 + 8], "little") for i in range(len(raw) // 8)]
    present = [e & MASK55 for e in ent if (e >> 63) & 1]
    n_zero_pfn = sum(1 for e in ent if ((e >> 63) & 1) and (e & MASK55) == 0)
    runs = []
    if present:
        cur = [present[0]]
        for p in present[1:]:
            if p == cur[-1] + 1:
                cur.append(p)
            else:
                runs.append(cur)
                cur = [p]
        runs.append(cur)
    out = dict(n_pages=len(ent), n_present=len(present), n_zero_pfn=n_zero_pfn,
               n_runs=len(runs),
               longest_run_pages=max((len(r) for r in runs), default=0),
               first_run_lens=[len(r) for r in runs[:10]],
               phys_first_mb=(round(present[0] * PS / 1e6, 4) if present else None),
               va=hex(va), nbytes=int(nbytes))
    out["contiguous"] = bool(len(runs) == 1 and len(present) == len(ent))
    return out


def kpage_survey(pfn0, npages):
    """CMA 区里每一页的 kpageflags 归类 ⇒ 「谁在占 CMA」的直接证据。"""
    cnt = dict(free_buddy=0, slab=0, anon=0, file_lru=0, compound_head=0,
               compound_tail=0, unevictable=0, locked=0, unused_zero=0, other=0,
               n=0)
    try:
        fd = os.open("/proc/kpageflags", os.O_RDONLY)
        try:
            os.lseek(fd, int(pfn0) * 8, os.SEEK_SET)
            raw = b""
            want = int(npages) * 8
            while len(raw) < want:
                c = os.read(fd, min(1 << 20, want - len(raw)))
                if not c:
                    break
                raw += c
        finally:
            os.close(fd)
    except Exception as ex:                                    # noqa: BLE001
        return dict(err=repr(ex))
    for i in range(len(raw) // 8):
        f = int.from_bytes(raw[i * 8:i * 8 + 8], "little")
        cnt["n"] += 1
        if f == 0:
            cnt["unused_zero"] += 1
            continue
        if (f >> 10) & 1:
            cnt["free_buddy"] += 1
            continue
        if (f >> 16) & 1:
            cnt["compound_tail"] += 1
            continue
        if (f >> 15) & 1:
            cnt["compound_head"] += 1
            continue
        if (f >> 7) & 1:
            cnt["slab"] += 1
            continue
        if (f >> 12) & 1:
            cnt["anon"] += 1
            continue
        if (f >> 5) & 1:
            cnt["file_lru"] += 1
            continue
        if (f >> 18) & 1:
            cnt["unevictable"] += 1
            continue
        cnt["other"] += 1
    return cnt


# ---------------------------------------------------------------- 测量工具
def sha1_of(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def bench_op(fn, n=10):
    ts, err = [], None
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception as ex:                                # noqa: BLE001
            err = repr(ex)
            break
        ts.append(time.perf_counter() - t0)
    if not ts:
        return dict(err=err, n=0)
    ts.sort()
    return dict(n=len(ts), min_ms=round(ts[0] * 1e3, 3),
                med_ms=round(ts[len(ts) // 2] * 1e3, 3),
                max_ms=round(ts[-1] * 1e3, 3), err=err)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v39.bit")
    ap.add_argument("--fclk", type=float, default=150.0)
    ap.add_argument("--slots", type=int, default=24)
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--gap", type=float, default=10.0)
    ap.add_argument("--json", default=os.path.join(OUTDIR, "probe_cma_stateA.json"))
    a = ap.parse_args()
    os.makedirs(OUTDIR, exist_ok=True)

    # ================================================ ① 静态 + 采样
    hdr("① 开机状态采样（%d 次，间隔 %g s）" % (a.samples, a.gap))
    R["sections"]["uptime"] = uptime()
    say("  uptime = %s（开机 %.1f h，loadavg %s）"
        % (R["sections"]["uptime"].get("up_s"), R["sections"]["uptime"].get("up_h", 0),
           R["sections"]["uptime"].get("loadavg")))
    R["sections"]["cpu"] = dict(model=(open("/proc/cpuinfo").read().split("model name")[1].split("\n")[0].strip(" :")
                                      if "model name" in open("/proc/cpuinfo").read() else None),
                                n_cpu=int(subprocess.run(["nproc"], capture_output=True,
                                                         text=True).stdout.strip() or 0),
                                freq=cpufreq())
    say("  cpu n=%s freq=%s" % (R["sections"]["cpu"]["n_cpu"],
                                json.dumps(R["sections"]["cpu"]["freq"], ensure_ascii=False)))

    R["sections"]["env"] = dict(sorted(os.environ.items()))
    say("  env 键数 = %d（全部记入 JSON）" % len(R["sections"]["env"]))

    samples = []
    for i in range(a.samples):
        s = dict(t=round(time.time(), 3), cma=cma_mb(), mem=mem_summary(),
                 freq={k: v["cur_mhz"] for k, v in cpufreq().items()})
        samples.append(s)
        say("  [采样 %d/%d] CmaFree %.3f / %.1f MB   MemFree %.1f  MemAvail %.1f  "
            "Cached %.1f  AnonPages %.1f  Slab %.1f  freq %s"
            % (i + 1, a.samples, s["cma"]["free_mb"], s["cma"]["total_mb"],
               s["mem"]["MemFree"], s["mem"]["MemAvailable"], s["mem"]["Cached"],
               s["mem"]["AnonPages"], s["mem"]["Slab"], s["freq"]))
        if i + 1 < a.samples:
            time.sleep(a.gap)
    R["sections"]["samples"] = samples

    R["sections"]["buddyinfo"] = buddy()
    say("  buddyinfo = %s" % json.dumps(R["sections"]["buddyinfo"], ensure_ascii=False))
    R["sections"]["cma_debug"] = cma_debug()
    say("  /sys/kernel/debug/cma = %s" % json.dumps(R["sections"]["cma_debug"], ensure_ascii=False)[:600])
    R["sections"]["dmesg_cma"] = dmesg_cma()
    for ln in R["sections"]["dmesg_cma"].get("lines", []):
        say("  dmesg| " + ln)
    R["sections"]["iomem"] = iomem()
    R["sections"]["procs"] = top_procs()
    say("  进程 RSS 合计 %.1f MB / %d 个；前 18 名合计 %.1f MB"
        % (R["sections"]["procs"]["rss_sum_mb"], R["sections"]["procs"]["n_proc"],
           R["sections"]["procs"]["rss_top_mb"]))
    for p in R["sections"]["procs"]["top"]:
        say("    %6d %8.1f MB  %-22s %s"
            % (p["pid"], p["rss_kb"] / 1024.0, (p["name"] or "")[:22], p["cmd"][:90]))

    # CMA 区页级归类：用 dmesg 的 "cma: Reserved 256 MiB at 0x..." 定位，再看每页 kpageflags
    cma_base = cma_size = None
    for ln in R["sections"]["dmesg_cma"].get("lines", []):
        if "cma" in ln.lower() and "reserved" in ln.lower():
            toks = ln.replace(",", " ").replace("(", " ").split()
            for i, tk in enumerate(toks):
                if tk.startswith("0x") and len(tk) >= 8:
                    try:
                        cma_base = int(tk, 16)
                    except ValueError:
                        continue
                    for j in range(max(0, i - 4), i):
                        try:
                            cma_size = int(toks[j])
                        except ValueError:
                            continue
                    break
            if cma_base:
                break
    R["sections"]["cma_base"] = cma_base
    R["sections"]["cma_size_mb_guess"] = cma_size
    say("  CMA 区猜测：base=%s  size=%s MB"
        % (hex(cma_base) if cma_base else None, cma_size))
    if cma_base:
        npages = int((cma_size or 256) * 1024 * 1024 // PS)
        R["sections"]["cma_kpage_survey"] = dict(
            base=hex(cma_base), npages=npages,
            flags=kpage_survey(cma_base // PS, npages))
        say("  CMA 区 %d 页的 kpageflags 归类（页数）：%s"
            % (npages, json.dumps(R["sections"]["cma_kpage_survey"]["flags"],
                                  ensure_ascii=False)))


    # ================================================ 二 加载 overlay + arena
    hdr("二 加载 overlay（本 session 唯一一次 download）与建 arena")
    import board_v39_verify as V
    import bin_engine as BE
    import bin_net as BN

    import pynq
    try:
        import xrt  # noqa: F401
        xrt_ver = "ok"
    except Exception as ex:                                    # noqa: BLE001
        xrt_ver = repr(ex)
    R["sections"]["versions"] = dict(pynq=str(getattr(pynq, "__version__", "?")),
                                     xrt=xrt_ver,
                                     numpy=np.__version__)
    say("  pynq=%s xrt=%s numpy=%s" % (R["sections"]["versions"]["pynq"],
                                       xrt_ver, np.__version__))
    from pynq.buffer import PynqBuffer
    import inspect
    for meth in ("flush", "invalidate", "sync"):
        if hasattr(PynqBuffer, meth):
            try:
                src = inspect.getsource(getattr(PynqBuffer, meth))
            except Exception as ex:                            # noqa: BLE001
                src = "ERR:%r" % (ex,)
            R["sections"]["versions"]["PynqBuffer." + meth] = src
            say("  --- PynqBuffer.%s ---" % meth)
            for ln in src.splitlines():
                if ln.strip():
                    say("     " + ln)

    cma0 = cma_mb()
    say("  CmaFree 开工前 = %.3f / %.1f MB" % (cma0["free_mb"], cma0["total_mb"]))
    t0 = time.time()
    try:
        ov = V.step_load(a.bit, download=bool(a.download))
        dl = True
    except Exception as ex:                                    # noqa: BLE001
        say("  ✗ download=True 失败：%r ⇒ 回落 no-download" % (ex,))
        ov = V.step_load(a.bit, download=False)
        dl = False
    R["sections"]["load"] = dict(download=dl, seconds=round(time.time() - t0, 4),
                                 cmafree_before=cma0, cmafree_after=cma_mb())
    say("  Overlay(download=%s) %.3f s ⇒ CmaFree %.3f MB"
        % (dl, R["sections"]["load"]["seconds"], R["sections"]["load"]["cmafree_after"]["free_mb"]))
    V.step_addr(ov)
    ident = V.step_identity(ov, a.fclk)
    R["sections"]["identity"] = ident
    say("  identity = %s" % json.dumps(ident, ensure_ascii=False)[:400])
    if not ident.get("ok"):
        say("  ✗✗ 复位指纹不对 ⇒ 停手（下面数据不可信）")

    t0 = time.time()
    arena, need = V.make_arena(256, a.slots, "probe_arena_256")
    R["sections"]["arena"] = dict(need_bytes=int(need), build_s=round(time.time() - t0, 4),
                                  pa=hex(int(arena.buf.physical_address)),
                                  cmafree_after=cma_mb())
    say("  arena %.2f MB @%s  CmaFree %.3f MB"
        % (need / 1e6, R["sections"]["arena"]["pa"],
           R["sections"]["arena"]["cmafree_after"]["free_mb"]))

    # --- 物理连续性：arena 与一块普通 heap
    va_arena = int(arena.buf.__array_interface__["data"][0])
    R["sections"]["pagemap"] = dict(
        arena=pagemap_runs(va_arena, arena.size),
        arena_declared_pa=hex(int(arena.buf.physical_address)))
    heap = np.empty(64 * 1024 * 1024, np.uint8)
    heap[::4096] = 1
    R["sections"]["pagemap"]["heap_64MB"] = pagemap_runs(
        int(heap.__array_interface__["data"][0]), heap.nbytes)
    del heap
    gc.collect()
    say("  pagemap arena : %s" % json.dumps(R["sections"]["pagemap"]["arena"], ensure_ascii=False))
    say("  pagemap heap64: %s" % json.dumps(R["sections"]["pagemap"]["heap_64MB"], ensure_ascii=False))

    # --- flush / invalidate 微基准（这直接量化"cache 维护成本"）
    hdr("三 flush/invalidate 微基准（RegionBuffer.flush 是**整块 owner** flush）")
    mb = arena.size / 1e6
    fb = dict(size_mb=round(mb, 3))
    fb["flush"] = bench_op(arena.buf.flush, 10)
    fb["invalidate"] = bench_op(arena.buf.invalidate, 10)
    reg, _pa = arena.region(4 << 20, name="bench4mb")
    fb["flush_4MB_view"] = bench_op(reg.flush, 10)
    R["sections"]["flush_bench"] = fb
    for k in ("flush", "invalidate", "flush_4MB_view"):
        v = fb[k]
        say("  %-14s n=%s min %.3f ms  med %.3f ms  max %.3f ms  %s"
            % (k, v.get("n"), v.get("min_ms", -1), v.get("med_ms", -1),
               v.get("max_ms", -1), v.get("err") or ""))
    if fb["flush"].get("med_ms"):
        say("  ⇒ %.2f MB / %.2f ms = %.1f MB/s 的 cache 维护吞吐"
            % (mb, fb["flush"]["med_ms"], mb / (fb["flush"]["med_ms"] / 1e3)))
    arena.reset()

    # --- CMA 区页级归类
    if R["sections"].get("cma_base_guess_int"):
        pass

    # ================================================ ④ 态 A：整网
    hdr("④ 态 A：256² numpy 臂（同配置；BIN_PROFILE 开/关各一次 + 原函数交叉核对）")

    def my_run(tag, profile):
        BE._PROF = bool(profile)
        env = V.cfg(a.slots)
        os.environ["BV_FREE"] = "1"
        arena.reset()
        eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=V.PACK, verbose=False)
        eng.load_weights()
        try:
            eng._prof = None            # 只计整网，不计 load_weights
        except Exception:              # noqa: BLE001
            pass
        net = BN.BinNet(V.PACK, engine=eng, H=256, W=256, verbose=False)
        rng = np.random.default_rng(1)
        x0 = rng.random((3, 256, 256), dtype=np.float32)
        xs = sha1_of(x0)
        t0 = time.time()
        outs, st = net.run(x0, pipeline=True, nbuf=2, arena=arena)
        dur = time.time() - t0
        sh = {k: sha1_of(outs[k]) for k in ("out1", "out2", "out3")}
        ok = (V.FROZEN[256] == (sh["out1"], sh["out2"], sh["out3"]))
        prof = st.get("prof") or {}
        pf = dict(getattr(eng, "_prof", None) or {})
        prep = dict(getattr(net, "_prep_prof", None) or {})
        prep.pop("pin_top", None)
        row = dict(tag=tag, profile=bool(profile), hw=256, slots=a.slots,
                   env=env, total_s=st.get("total"), wall_s=round(dur, 4),
                   pl_s=st.get("pl"), pack_pl=st.get("pack_pl"), ps_s=st.get("ps"),
                   prologue=st.get("ps_prologue"), epilogue=st.get("ps_epilogue"),
                   nonconv=st.get("ps_nonconv"), overlap=st.get("overlap"),
                   wait=st.get("wait"), mmio=st.get("mmio_writes"),
                   prof={k: round(float(v), 4) for k, v in prof.items()},
                   e1e2={k: round(float(v), 4) for k, v in pf.items()},
                   prep={k: (round(float(v), 4) if isinstance(v, (int, float)) and
                             not isinstance(v, bool) else v) for k, v in prep.items()},
                   lazy=st.get("lazy"), cat=st.get("cat"), acq=st.get("acq"),
                   x0_sha1=xs, sha1=sh, sha_ok=bool(ok),
                   cmafree_mb=cma_mb())
        say("  ── %-22s profile=%d ──" % (tag, int(profile)))
        say("     总 %.3f s（墙钟 %.3f） PL %.3f | PS %.3f = pro %.3f + epi %.3f + 非卷积 %.3f"
            " | ovl %.3f wait %.3f  interp %.3f  CmaFree %.3f MB"
            % (row["total_s"] or 0, dur, row["pl_s"] or 0, row["ps_s"] or 0,
               row["prologue"] or 0, row["epilogue"] or 0, row["nonconv"] or 0,
               row["overlap"] or 0, row["wait"] or 0,
               row["prof"].get("interp", -1), row["cmafree_mb"]["free_mb"]))
        say("     prologue 亚项（engine._prof，s）：%s"
            % json.dumps({k: round(v * 1e3, 1) for k, v in row["e1e2"].items()},
                         ensure_ascii=False) + "  ← 单位 ms")
        say("     net._prep_prof（s）：%s"
            % json.dumps({k: (round(v * 1e3, 1) if isinstance(v, float) else v)
                          for k, v in row["prep"].items()}, ensure_ascii=False) + "  ← ms")
        say("     out1=%s %s  x0=%s" % (sh["out1"][:12],
                                        "OK 3/3" if ok else "**✗ ≠ 冻结基线**", xs[:16]))
        del net, eng, outs
        gc.collect()
        return row

    rows = [my_run("A1_noprof", False), my_run("A2_prof", True)]
    # 原函数交叉核对（证明薄拷贝忠实）
    hdr("五 原函数交叉核对 V.run_net（协议自证）")
    BE._PROF = False
    V.env_reset = None
    ref = V.run_net(ov, 256, arena, a.slots, tag="ref_V_run_net")
    same = (ref.get("x0_sha1") == rows[0]["x0_sha1"]
            and ref.get("sha1") == rows[0]["sha1"])
    R["sections"]["protocol_check"] = dict(ref=ref, matches_copy=bool(same))
    say("  V.run_net: x0=%s out1=%s total=%.3f"
        % (str(ref.get("x0_sha1"))[:16], ref["sha1"]["out1"][:12], ref.get("total_s") or 0))
    say("  与薄拷贝逐位相同 = %s" % ("✓" if same else "✗"))
    R["sections"]["runs"] = rows

    # ================================================ 六 收尾状态
    hdr("六 跑完后的状态（与开工前对比）")
    R["sections"]["after"] = dict(cma=cma_mb(), mem=mem_summary(),
                                 freq={k: v["cur_mhz"] for k, v in cpufreq().items()},
                                 buddyinfo=buddy())
    say("  CmaFree %.3f / %.1f MB   MemAvail %.1f MB   freq %s"
        % (R["sections"]["after"]["cma"]["free_mb"],
           R["sections"]["after"]["cma"]["total_mb"],
           R["sections"]["after"]["mem"]["MemAvailable"],
           R["sections"]["after"]["freq"]))
    say("  buddyinfo = %s" % json.dumps(R["sections"]["after"]["buddyinfo"], ensure_ascii=False))

    del arena
    gc.collect()
    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False)
    say("  → %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
