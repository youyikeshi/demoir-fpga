#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""board_e1e2_smoke.py —— E1E2 板端冒烟（5 条真命令 + DBG 读带宽）

不 overlay、不下载：用 `Overlay(bit, download=False)` 接手**已经加载**的 PL，
只做 AXI-Lite 读写。默认 `--dry-run`（只算不写）。

它跑的是 `deploy/e1e2_desc/cmds.bin` 里**真实的 5 条命令**（8²/16² 小形状，
含 cin=544 的 grp8 变通），但把 src 重定位到本地小缓冲、thr 逐 lane 复制
交付物里的 16 B（保留真实阈值/哨兵）。每条命令都与 C 内核 `bpack_i8` 比对。

用法（板子上）：
    python3 board_e1e2_smoke.py --dry-run                 # 只算，不碰硬件
    python3 board_e1e2_smoke.py --run                     # 真跑 5 条
    python3 board_e1e2_smoke.py --run --bit design_1.bit --fclk 100
    python3 board_e1e2_smoke.py --run --n 9 --log smoke.log

输出（每行都 flush + os.fsync，板子挂了也不丢）：
    层 / group / cin / 形状 / 命令字节 / C 内核差异
    DBG_E: rd/cyc/rdst/wrst/wr → rd/cyc、读带宽 MB/s、miss 冻结占比
    ⇒ 全网红线：155.3 MB / 实测带宽 = 读下限；与 219 ms 计算取 max
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PSCODE = os.path.realpath(os.path.join(HERE, *[os.pardir] * 2))
ROOT = os.path.dirname(PSCODE)
P_PACK = os.path.join(PSCODE, "deploy", "bin_weights_new")
sys.path.insert(0, HERE)

import bin_engine as BE                    # noqa: E402
import bpack                               # noqa: E402
from bin_ref_numpy import Blob, sat8        # noqa: E402
from e1e2_sim import sim_command            # noqa: E402

# gen_e1e2_desc / RTL REPORT 的口径
NET_COMPUTE_CYCLES = 21_930_932
NET_READ_MB = 152.54          # 8 lane × 全部字组（含 546 条全掩码 lane 的白读）
NET_USEFUL_READ_MB = 136.22   # 只算真实通道
TEST_BYTES = 64


def c8_of(x):
    """(cin,H,W) int8 → C8 交错裸字节（与 RTL 输出布局一致）。"""
    from e1e2_sim import to_c8_padded
    return np.frombuffer(to_c8_padded(x, pad_to=int(x.shape[0])), np.uint8)


class FsyncLog:
    """每行 flush + os.fsync（板子可能挂，日志不能丢）。"""

    def __init__(self, path=None):
        self.f = None
        if path:
            self.f = open(path, "a", encoding="utf-8", buffering=1)
            self.f.write("\n# ---- board_e1e2_smoke %s ----\n"
                         % time.strftime("%Y-%m-%d %H:%M:%S"))
            self._sync()

    def _sync(self):
        if self.f is not None:
            self.f.flush()
            os.fsync(self.f.fileno())

    def __call__(self, msg=""):
        line = str(msg)
        print(line, flush=True)
        if self.f is not None:
            self.f.write(line + "\n")
            self._sync()

    def close(self):
        if self.f is not None:
            self._sync()
            self.f.close()
            self.f = None


# ============================================================================
# 选命令 + 建小镜像 + 算参考
# ============================================================================
def pick_commands(prog, n=5):
    """从真实命令表里挑 n 条 8²/16² 的小命令（含 grp8 hack、不同 cin/层）。"""
    small = [c for c in prog.commands
             if int(c["cfg_h"]) == int(c["cfg_w"]) and int(c["cfg_h"]) in (8, 16)]
    picks = []

    def add(pred):
        for c in small:
            if pred(c) and all(c["i"] != q["i"] for q in picks):
                picks.append(c)
                return True
        return False

    add(lambda c: bool(c.get("grp_hack")) and int(c["cfg_h"]) == 8)  # cin=544 8²
    add(lambda c: int(c["cfg_h"]) == 8 and int(c["cfg_c"]) == 512)
    add(lambda c: int(c["cfg_h"]) == 8 and int(c["cfg_c"]) == 48)
    add(lambda c: int(c["cfg_h"]) == 16 and int(c["cfg_c"]) == 48)
    add(lambda c: int(c["cfg_h"]) == 16 and int(c["cfg_c"]) == 512)
    for c in small:                                        # 凑满且尽量不同层
        if len(picks) >= n:
            break
        if len({q["layer"] for q in picks}
               | {c["layer"]}) > len({q["layer"] for q in picks}):
            add(lambda cc, c=c: cc["i"] == c["i"])
    return picks[:n]


def build_mini(prog, blob, cmds, base):
    """把选中的命令摆进一个扁平地址空间（desc/thr/src/out 四段，64 B 对齐）。

    返回 (mem, layout, refs)，refs[i] = C 内核参考字节。
    """
    al = lambda n: (int(n) + 63) // 64 * 64
    desc_n = len(cmds) * TEST_BYTES
    thr_n = len(cmds) * 8 * 16
    src_n = sum(int(c["cfg_h"]) * int(c["cfg_w"]) * 8 * 8 for c in cmds)
    out_n = sum(int(c["words"]) * 8 for c in cmds)
    off_desc = 0
    off_thr = off_desc + al(desc_n)
    off_src = off_thr + al(thr_n)
    off_out = off_src + al(src_n)
    need = off_out + al(out_n)
    mem = np.zeros(need, np.uint8)
    layout = dict(desc=off_desc, thr=off_thr, src=off_src, out=off_out,
                  need=need, ncmd=len(cmds))
    rng = np.random.default_rng(20261031)
    refs = []
    meta = []
    src_cursor = off_src
    out_cursor = off_out
    thr_cursor = off_thr
    desc_cursor = off_desc
    for c in cmds:
        i = int(c["i"])
        cin = int(c["cfg_c"])
        H, W = int(c["cfg_h"]), int(c["cfg_w"])
        hw = H * W
        grp = int(c["group"])
        L = blob.layer_by_name(c["layer"])
        fsin = int(L["act_fs_in"])
        tq = blob.params(L)["t_q"]
        words = prog.desc[i * 8:(i + 1) * 8].copy()
        lane_src, lane_thr, lane_val = [], [], []
        xq = np.full((64, hw), -128, np.int8)
        tt = np.full(64, 127, np.int16)
        for k in range(8):
            ch = grp * 64 + k * 8
            orig_src = int(words[k] & 0xFFFFFFFF)
            orig_thr = int(words[k] >> 32)
            # 交付物里的 16 B 阈值：真实 lane 从 thr.bin 取；全掩码 lane 用哨兵值
            if orig_thr == prog.sentinel_pa:
                tbytes = np.full(8, BE.E1E2_SENTINEL_I16, "<i2").tobytes()
                b = None
            else:
                o = orig_thr - prog.thr_pa
                tbytes = bytes(prog.thr[o:o + 16])
                b = prog.buf_at(orig_src)
            lane_thr.append(tbytes)
            if ch >= cin:
                lane_src.append(None)
                lane_val.append(None)
                continue
            d = fsin - int(b["fs"])
            arr = rng.integers(-128, 128, (hw, 8), dtype=np.int8)
            flat = arr.reshape(-1)
            flat[::37] = -128
            flat[1::41] = 127
            flat[2::43] = 0
            lane_src.append(arr.tobytes())
            lane_val.append((arr, d))
            q = sat8(np.rint(arr.astype(np.float64) * (2.0 ** d)))
            for j in range(8):
                if ch + j < cin:
                    xq[k * 8 + j] = q[:, j]
                    tt[k * 8 + j] = np.int16(1 - int(tq[ch + j]))
        # C 内核参考（bpack_i8 对 64 通道；掩码通道 tt=127 ⇒ 恒 0）
        refs.append(bpack.pack_i8(xq.reshape(64, H, W), tt).view(np.uint8).tobytes())
        # 写 desc / thr / src，并记下新地址
        new_words = np.zeros(8, "<u8")
        lanes_pa = []
        for k in range(8):
            tp = base + thr_cursor + 16 * k
            if lane_src[k] is None:
                # 全掩码 lane：src 指一个合法地址（本镜像内），thr 用哨兵值
                sp = base + off_src
            else:
                sp = base + src_cursor
                arr = np.frombuffer(lane_src[k], np.uint8)
                mem[src_cursor:src_cursor + len(arr)] = arr
                src_cursor += al(len(arr))
            tarr = np.frombuffer(lane_thr[k], np.uint8)
            mem[thr_cursor + 16 * k:thr_cursor + 16 * k + 16] = tarr
            new_words[k] = (np.uint64(tp) << np.uint64(32)) | np.uint64(sp)
            lanes_pa.append((sp, tp))
        mem[desc_cursor:desc_cursor + TEST_BYTES] = \
            new_words.view(np.uint8)
        opa = base + out_cursor
        meta.append(dict(i=i, layer=c["layer"], group=grp, cfg_group=c["cfg_group"],
                         cin=cin, H=H, W=W, hw=hw, ncmd=1, bytes=hw * 8,
                         desc_pa=base + desc_cursor, out_pa=opa,
                         words=new_words, lanes=lanes_pa,
                         ref=refs[-1]))
        desc_cursor += TEST_BYTES
        thr_cursor += 8 * 16
        out_cursor += al(hw * 8)
    return mem, layout, meta


# ============================================================================
# dry-run：软件预演（参考 == FSM 模拟）
# ============================================================================
def dry_run(prog, blob, cmds, log, fclk):
    base = 0x1000_0000
    mem, layout, meta = build_mini(prog, blob, cmds, base)
    log("== dry-run：只算不写 ==")
    log("镜像 %d B（desc=%#x thr=%#x src=%#x out=%#x）"
        % (layout["need"], layout["desc"], layout["thr"], layout["src"], layout["out"]))
    bad = 0
    for r in meta:
        got = sim_command(mem, r["cin"], r["H"], r["W"], r["cfg_group"],
                          np.frombuffer(r["words"].tobytes(), np.uint64),
                          r["desc_pa"], base)
        nd = sum(1 for a, b in zip(got, r["ref"]) if a != b)
        bad += nd
        log("  %-44s g=%d cin=%-3d %2dx%-2d 命令=%d 字节=%-5d 差异=%d"
            % (r["layer"][-44:], r["group"], r["cin"], r["H"], r["W"],
               1, len(got), nd))
    log("  ⇒ FSM 模拟 vs C 内核：%d/%d 命令一致，差异 %d"
        % (len(meta), len(meta), bad))
    cyc = sum(17 + 9 * r["hw"] + 50 for r in meta)
    log("  预测：%d 命令 / %d 拍 @%.0f MHz = %.3f ms；读拍 = %d（%.2f MB）"
        % (len(meta), cyc, fclk, cyc / fclk / 1e3,
           sum(8 + 16 + 8 * r["hw"] for r in meta),
           sum(8 + 16 + 8 * r["hw"] for r in meta) * 8 / 1e6))
    log("  ⇒ %s" % ("PASS（上板前软件预演一致）" if bad == 0 else "FAIL"))
    return 0 if bad == 0 else 1


# ============================================================================
# run：接手已加载的 PL，跑真命令
# ============================================================================
def run_board(prog, blob, cmds, log, args):
    try:
        import pynq.pl_server.embedded_device as _pynq_emb   # noqa: F401
    except Exception:                                        # noqa: BLE001
        pass
    from pynq import Overlay, allocate
    if args.fclk <= 0:
        try:
            from pynq import Clocks
            args.fclk = float(Clocks.fclk0_mhz)
            log("fclk 未指定，读回 %.1f MHz" % args.fclk)
        except Exception:                                  # noqa: BLE001
            args.fclk = 100.0
            log("fclk 读取失败，按 100 MHz 算")

    bit = args.bit
    if not bit:
        for c in (os.path.join(HERE, "design_1.bit"),
                  os.path.join(HERE, os.pardir, "design_1.bit"),
                  os.environ.get("BIT", "")):
            if c and os.path.exists(c):
                bit = c
                break
    if not bit or not os.path.exists(bit):
        log("找不到比特流：请 --bit <design_1.bit>（不会下载，只读 ip_dict）")
        return 2
    log("== run：Overlay(%s, download=False) ==" % bit)
    ov = Overlay(bit, download=False)
    core = BE.BinConvEngine(ov, unit_id=args.unit, pack_dir=P_PACK,
                            verbose=not args.quiet)
    ee = BE.E1E2Engine(core, verbose=not args.quiet, fclk_mhz=args.fclk)
    pr = ee.probe()
    log("R_RUN_UNIT 写 4 读回 %#x；写 0 读回 %#x  %s"
        % (pr["write4"], pr["back0"],
           "PASS" if ((pr["write4"] & 7) == 4 and (pr["back0"] & 7) == 0) else "FAIL"))
    if ((pr["write4"] & 7) != 4 or (pr["back0"] & 7) != 0):
        log("⇒ 这个比特流没有 E1E2（0x200 页写不进去）；先加载含 bin_e1e2 的 wrapper。")
        return 2

    # 小镜像：一块连续 CMA，按 desc/thr/src/out 切
    al = lambda n: (int(n) + 63) // 64 * 64
    probe_mem = np.zeros(1, np.uint8)
    _m, layout, _meta = build_mini(prog, blob, cmds, 0)
    buf = allocate(shape=(layout["need"] + 4096,), dtype=np.uint8)
    base = int(buf.physical_address)
    core.add_region(buf, "e1e2_smoke")
    mv = np.frombuffer(buf, np.uint8)
    mem_h, layout2, meta = build_mini(prog, blob, cmds, base)
    mv[:layout["need"]] = mem_h[:layout["need"]]
    buf.flush()
    # 启动前把每个地址都过一遍护栏（2026-10-30 挂板事故的教训）
    regs = core._regions_all()
    hw_of = lambda r: int(r["H"]) * int(r["W"])
    for r in meta:
        BE.BinGuard.check_region(r["desc_pa"], TEST_BYTES, regs, "desc")
        BE.BinGuard.check_region(r["out_pa"], r["bytes"], regs, "out")
        for sp, tp in r["lanes"]:
            BE.BinGuard.check_region(sp, hw_of(r) * 8, regs, "src")
            BE.BinGuard.check_region(tp, 16, regs, "thr")
    log("  护栏：%d 条命令的 desc/out/src/thr 全部落在 mini 缓冲内" % len(meta))
    log("镜像 %d B @ pa=%#x（desc=%#x src=%#x out=%#x）"
        % (layout["need"], base, base + layout["desc"], base + layout["src"],
           base + layout["out"]))
    if args.verify_src:
        for r in meta:
            o = r["desc_pa"] - base
            if not np.array_equal(mv[o:o + 64], mem_h[o:o + 64]):
                log("  ✗ desc 回读不一致 @%#x" % r["desc_pa"])
                return 3
        log("  desc 表读回逐字节一致")

    total = dict(rd=0, cyc=0, rdst=0, wrst=0, wr=0, cmd=0, bytes=0, diff=0)
    try:
        for r in meta:
            t0 = time.perf_counter()
            ee._agg = dict(ncmd=0, seconds=0.0, rd=0, cyc=0, rdst=0, wrst=0, wr=0)
            ee.run_one(r["desc_pa"], r["out_pa"], r["cin"], r["H"], r["W"],
                       r["cfg_group"], timeout=args.timeout)
            dt = time.perf_counter() - t0
            buf.invalidate()
            got = bytes(mv[r["out_pa"] - base:r["out_pa"] - base + r["bytes"]])
            nd = sum(1 for a, b in zip(got, r["ref"]) if a != b)
            d = ee.last_run["dbg"]
            for k in ("rd", "cyc", "rdst", "wrst", "wr"):
                total[k] += int(d.get(k, 0))
            total["cmd"] += 1
            total["bytes"] += len(got)
            total["diff"] += nd
            bw = (d["rd"] * 8.0 * args.fclk / d["cyc"]) if d.get("cyc") else 0.0
            log("  %-40s g=%d cin=%-3d %2dx%-2d 字节=%-5d 差异=%-3d "
                "rd=%d cyc=%d rdst=%d wr=%d  rd/cyc=%.3f  读带宽=%7.1f MB/s  "
                "wall=%.3f ms"
                % (r["layer"][-40:], r["group"], r["cin"], r["H"], r["W"],
                   len(got), nd, d["rd"], d["cyc"], d["rdst"], d["wr"],
                   (d["rd"] / d["cyc"] if d.get("cyc") else 0.0), bw, dt * 1e3))
    finally:
        try:
            ee.select(0, force=True)
            ru = core._rd(BE.CFG + 0x200) & 7
            log("收尾：R_RUN_UNIT 写回 0，读回 %d  %s" % (ru, "PASS" if ru == 0 else "FAIL"))
        except Exception as ex:                            # noqa: BLE001
            log("收尾失败（板子可能已挂）：%r" % (ex,))

    cyc = total["cyc"]
    rd = total["rd"]
    if cyc:
        rpc = rd / cyc
        bw = rd * 8.0 * args.fclk / cyc
        log("== 汇总（%d 条命令，%d 字节，差异 %d）=="
            % (total["cmd"], total["bytes"], total["diff"]))
        log("  DBG_E 合计：rd=%d cyc=%d rdst=%d wrst=%d wr=%d"
            % (rd, cyc, total["rdst"], total["wrst"], total["wr"]))
        log("  读拍/拍 = %.4f；读 miss 冻结占比 = %.1f%%；写压力冻结 = %.1f%%"
            % (rpc, 100.0 * total["rdst"] / cyc, 100.0 * total["wrst"] / cyc))
        log("  **PL 持续读带宽 = %.1f MB/s**（rd×8B×%.0fMHz / cyc）"
            % (bw, args.fclk))
        t_read = NET_READ_MB * 1e6 / (bw * 1e6) if bw else float("inf")
        t_comp = NET_COMPUTE_CYCLES / (args.fclk * 1e6)
        log("  ⇒ 全网外推：计算 %.1f ms / 读 %.1f ms（%.1f MB）⇒ max = %.1f ms"
            % (t_comp * 1e3, t_read * 1e3, NET_READ_MB, max(t_comp, t_read) * 1e3))
        log("     若 rd/cyc ≥ %.3f 且 fclk=%.0f MHz，读下限 ≤150 ms（0.15 s 门槛）"
            % (NET_READ_MB * 1e6 / (0.150 * args.fclk * 1e6 * 8), args.fclk))
        t_read_u = NET_USEFUL_READ_MB * 1e6 / (bw * 1e6) if bw else float("inf")
        log("     只算真实通道 %.1f MB ⇒ 读 %.1f ms"
            % (NET_USEFUL_READ_MB, t_read_u * 1e3))
    log("  ⇒ %s" % ("PASS（5 条命令逐字节 == C 内核）" if total["diff"] == 0 else "FAIL"))
    return 0 if total["diff"] == 0 else 1


def find_desc_dir():
    for p in (os.environ.get("E1E2_DESC"),
              os.path.join(PSCODE, "deploy", "e1e2_desc")):
        if p and os.path.exists(os.path.join(p, "frame.json")):
            return p
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="E1E2 板端冒烟（默认 dry-run）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--run", action="store_true", help="真跑（写寄存器/访存）")
    g.add_argument("--dry-run", action="store_true", help="只算不写（默认）")
    ap.add_argument("--bit", default="", help="比特流路径（只读 ip_dict，不下载）")
    ap.add_argument("--unit", type=int, default=0, help="bin_unit 实例号")
    ap.add_argument("--fclk", type=float, default=100.0, help="PL 时钟 MHz（算带宽用）")
    ap.add_argument("--n", type=int, default=5, help="跑几条命令（默认 5）")
    ap.add_argument("--timeout", type=float, default=20.0, help="每条命令超时 s")
    ap.add_argument("--log", default="", help="日志路径（每行 fsync；默认自动）")
    ap.add_argument("--no-log", action="store_true", help="不写日志文件")
    ap.add_argument("--verify-src", action="store_true", help="回读 desc 表校验")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    d = find_desc_dir()
    if d is None:
        print("找不到 deploy/e1e2_desc（可用 E1E2_DESC 指定）")
        return 2
    logpath = None if a.no_log else (a.log or os.path.join(
        HERE, "e1e2_smoke_%s.log" % time.strftime("%Y%m%d_%H%M%S")))
    log = FsyncLog(logpath)
    try:
        prog = BE.E1E2Program(desc_dir=d, verbose=False)
        blob = Blob(P_PACK)
        cmds = pick_commands(prog, a.n)
        if len(cmds) < a.n:
            log("⚠ 只挑到 %d 条 8²/16² 命令（要 %d）" % (len(cmds), a.n))
        log("== E1E2 smoke：%s ==" % d)
        log("选中 %d 条命令：" % len(cmds))
        for c in cmds:
            log("  i=%-3d %-44s g=%d cfg_group=%d cin=%-3d %2dx%-2d%s"
                % (int(c["i"]), c["layer"][-44:], int(c["group"]),
                   int(c["cfg_group"]), int(c["cfg_c"]), int(c["cfg_h"]),
                   int(c["cfg_w"]), "  [grp8 hack]" if c.get("grp_hack") else ""))
        if a.run:
            return run_board(prog, blob, cmds, log, a)
        return dry_run(prog, blob, cmds, log, a.fclk)
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
