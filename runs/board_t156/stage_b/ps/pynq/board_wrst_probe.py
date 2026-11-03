#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""board_wrst_probe.py —— 板上量"写压力 / 总拍"（DBG_E_* 硬件计数器，不靠 xsim）

背景：xsim 在 k=1/cout 大的形状上看到 `u_ce` 冻结里 81% 是写 FIFO 压力
（bf/df 压力），但这批 256² 真层没有 golden、xsim 跑不了。RTL 里已有 5 个
硬件计数器（0x230..0x240）：

    DBG_E_CYC  : run_active 拍数
    DBG_E_RDST : u_ce=0 且单元在读 ⇒ 读 miss 冻结
    DBG_E_WRST : u_ce=0 且单元不在读 ⇒ **写 FIFO 压力冻结**（本工具的主角）
    DBG_E_RD / DBG_E_WR : 真正被消费的读写拍

本工具用 **真实描述符**（deploy/e1e2_desc）+ 随机源数据（计时与数值无关）跑
指定 (层, group) 的命令，读回上面 5 个计数器。不判功能，只判"写压力占比"。

用法（板子上，需 sudo）：
    python3 board_wrst_probe.py --dry-run
    sudo python3 board_wrst_probe.py --run --fclk 150 \
        --layers "sam_blocks.0.b0.conv.conv:0,sam_blocks.0.b0.conv.conv:1,rdb.conv_layers.2.conv:0"
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PSCODE = os.path.realpath(os.path.join(HERE, *[os.pardir] * 2))
P_PACK = os.path.join(PSCODE, "deploy", "bin_weights_new")
sys.path.insert(0, HERE)

import bin_engine as BE                      # noqa: E402
import board_e1e2_smoke as S                 # noqa: E402
from bin_ref_numpy import Blob               # noqa: E402


def pick_commands(prog, specs):
    """specs = [(substr, group), ...] → 真实命令 dict 列表（按给出顺序）。"""
    out = []
    for sub, grp in specs:
        hits = [c for c in prog.commands
                if (sub in c["layer"]) and (int(c["group"]) == int(grp))]
        if not hits:
            raise SystemExit("找不到命令：layer 含 %r 且 group=%d" % (sub, grp))
        out.append(hits[0])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--bit", default="/home/xilinx/demoir/design_1_150v39.bit")
    ap.add_argument("--fclk", type=float, default=150.0)
    ap.add_argument("--repeat", type=int, default=2, help="每条命令重复几次（取中位数）")
    ap.add_argument("--layers", default=(
        "decoder_1.sam_blocks.0.b0.conv.conv:0,"
        "decoder_1.sam_blocks.0.b0.conv.conv:1,"
        "decoder_1.sam_blocks.0.b0.conv.conv:4,"
        "encoder_1.rdb.conv_post.conv:0,"
        "decoder_1.rdb.conv_layers.2.conv:0"))
    ap.add_argument("--log", default="")
    a = ap.parse_args(argv)

    d = S.find_desc_dir()
    if d is None:
        raise SystemExit("找不到 deploy/e1e2_desc")
    prog = BE.E1E2Program(desc_dir=d, verbose=False)
    blob = Blob(P_PACK)
    specs = []
    for item in a.layers.split(","):
        item = item.strip()
        if not item:
            continue
        sub, grp = item.rsplit(":", 1)
        specs.append((sub, int(grp)))
    cmds = pick_commands(prog, specs)

    logf = None
    if a.log:
        logf = open(a.log, "a", encoding="utf-8", buffering=1)

    def log(msg=""):
        print(msg, flush=True)
        if logf is not None:
            logf.write(str(msg) + "\n"); logf.flush(); os.fsync(logf.fileno())

    log("== 选中 %d 条真命令 ==" % len(cmds))
    for c in cmds:
        log("  i=%-4d %-52s grp=%d cfg_group=%d cin=%-3d %3dx%-3d words=%-5d"
            % (int(c["i"]), c["layer"][-52:], int(c["group"]), int(c["cfg_group"]),
               int(c["cfg_c"]), int(c["cfg_h"]), int(c["cfg_w"]), int(c["words"])))

    if not a.run:
        # 只算镜像大小
        _mem, layout, meta = S.build_mini(prog, blob, cmds, 0)
        log("dry-run：mini 镜像 %d B（desc=%#x thr=%#x src=%#x out=%#x），%d 条"
            % (layout["need"], layout["desc"], layout["thr"], layout["src"],
               layout["out"], len(meta)))
        log("  ⇒ 上板加 --run；本机只验证选层/建镜像")
        return 0

    try:
        import pynq.pl_server.embedded_device as _pynq_emb   # noqa: F401
    except Exception:                                        # noqa: BLE001
        pass
    from pynq import Overlay, allocate
    log("== Overlay(%s, download=False) ==" % a.bit)
    try:
        ov = Overlay(a.bit, download=False)
    except Exception as ex:                               # noqa: BLE001
        log("attach 失败（%r）⇒ 重新下载一次" % (ex,))
        ov = Overlay(a.bit)
    try:
        from pynq import Clocks
        Clocks.fclk0_mhz = float(a.fclk)
        log("fclk 请求 %.0f MHz，读回 %.1f" % (a.fclk, float(Clocks.fclk0_mhz)))
    except Exception as ex:                               # noqa: BLE001
        log("fclk 设置失败（按 %.0f 算）：%r" % (a.fclk, ex))
    core = BE.BinConvEngine(ov, unit_id=0, pack_dir=P_PACK, verbose=False)
    ee = BE.E1E2Engine(core, verbose=False, fclk_mhz=a.fclk)
    pr = ee.probe()
    log("probe: R_RUN_UNIT 4→%#x / 0→%#x  %s"
        % (pr["write4"], pr["back0"],
           "PASS" if ((pr["write4"] & 7) == 4 and (pr["back0"] & 7) == 0) else "FAIL"))

    # 板端 numpy 1.21：`np.uint64 & 0xFFFFFFFF` 会因 uint64/int64 提升失败 ⇒
    # 换成 Python int 的 object 数组（build_mini 里只做 int(...) 与 & ）
    prog.desc = np.array([int(x) for x in prog.desc], dtype=object)
    _m, layout, _ = S.build_mini(prog, blob, cmds, 0)
    buf = allocate(shape=(layout["need"] + 4096,), dtype=np.uint8)
    base = int(buf.physical_address)
    core.add_region(buf, "wrst_probe")
    mv = np.frombuffer(buf, np.uint8)
    mem_h, layout, meta = S.build_mini(prog, blob, cmds, base)
    mv[:layout["need"]] = mem_h[:layout["need"]]
    buf.flush()
    regs = core._regions_all()
    for r in meta:
        BE.BinGuard.check_region(r["desc_pa"], S.TEST_BYTES, regs, "desc")
        BE.BinGuard.check_region(r["out_pa"], r["bytes"], regs, "out")
        for sp, tp in r["lanes"]:
            BE.BinGuard.check_region(sp, int(r["H"]) * int(r["W"]) * 8, regs, "src")
            BE.BinGuard.check_region(tp, 16, regs, "thr")
    log("镜像 %d B @ pa=%#x；护栏通过" % (layout["need"], base))

    rows = []
    try:
        for r in meta:
            runs = []
            for it in range(a.repeat):
                ee._agg = dict(ncmd=0, seconds=0.0, rd=0, cyc=0, rdst=0,
                               wrst=0, wr=0)
                # 清零 DBG_RD（fill/rdreq），让每条命令的计数独立
                core._wr(BE.CFG + 0xE0, 0, force=True)
                t0 = time.perf_counter()
                ee.run_one(r["desc_pa"], r["out_pa"], r["cin"], r["H"], r["W"],
                           r["cfg_group"])
                dt = time.perf_counter() - t0
                d = ee.last_run["dbg"]
                rd_dbg = int(core._rd(BE.CFG + 0xE0))
                wr_dbg = int(core._rd(BE.CFG + 0xE8))
                pf_cnt = int(core._rd(BE.CFG + 0xF4)) & 0xFFFF
                d["fill"] = (rd_dbg >> 16) & 0xFFFF
                d["rdreq16"] = rd_dbg & 0xFFFF
                d["pf"] = pf_cnt
                d["wbeat"] = (wr_dbg >> 16) & 0xFFFF
                d["burst"] = wr_dbg & 0xFFFF
                runs.append((dt, d))
            # 取中位数（按 cyc 排序取中间那次）
            runs.sort(key=lambda x: x[1]["cyc"])
            dt, d = runs[len(runs) // 2]
            cyc = max(1, int(d["cyc"]))
            rows.append(dict(layer=r["layer"], group=r["group"], cin=r["cin"],
                             H=r["H"], W=r["W"], out_bytes=r["bytes"],
                             cyc=cyc, rd=int(d["rd"]), wr=int(d["wr"]),
                             rdst=int(d["rdst"]), wrst=int(d["wrst"]),
                             wall_ms=dt * 1e3))
            bw = d["rd"] * 8.0 * a.fclk / cyc
            fill = max(1, int(d.get("fill", 0)))
            bw_fetch = fill * 128.0 * a.fclk / cyc
            inflight = (fill / cyc) * 55.2
            log("  %-46s g=%d cin=%-3d %3dx%-3d out=%6dB cyc=%6d "
                "rd=%5d(%.2f/拍) wr=%5d rdst=%5d(%4.1f%%) wrst=%5d(%4.1f%%) "
                "fill=%4d(pf=%4d) %.1f 拍/线 在飞=%.2f 读=%6.1fMB/s "
                "抓取=%6.1fMB/s"
                % (r["layer"][-46:], r["group"], r["cin"], r["H"], r["W"],
                   r["bytes"], cyc, d["rd"], d["rd"] / cyc, d["wr"],
                   d["rdst"], 100.0 * d["rdst"] / cyc,
                   d["wrst"], 100.0 * d["wrst"] / cyc,
                   fill, d.get("pf", 0), cyc / fill, inflight, bw, bw_fetch))
    finally:
        try:
            ee.select(0, force=True)
            log("收尾：R_RUN_UNIT 写回 0，读回 %d" % (core._rd(BE.CFG + 0x200) & 7))
        except Exception as ex:                           # noqa: BLE001
            log("收尾失败：%r" % (ex,))

    # 汇总：按 k 属性分不开（描述符里没有 k），这里直接给总量
    if rows:
        cyc = sum(r["cyc"] for r in rows)
        wrst = sum(r["wrst"] for r in rows)
        rdst = sum(r["rdst"] for r in rows)
        wr = sum(r["wr"] for r in rows)
        rd = sum(r["rd"] for r in rows)
        log("== 汇总 %d 条：cyc=%d  wrst=%d (%.1f%%)  rdst=%d (%.1f%%)  "
            "wr=%d rd=%d  wr/cyc=%.3f rd/cyc=%.3f"
            % (len(rows), cyc, wrst, 100.0 * wrst / cyc, rdst,
               100.0 * rdst / cyc, wr, rd, wr / cyc, rd / cyc))
        log("   写带宽 = %.1f MB/s（wr×8B×%.0fMHz/cyc）"
            % (wr * 8.0 * a.fclk / cyc, a.fclk))
    return 0


if __name__ == "__main__":
    sys.exit(main())
