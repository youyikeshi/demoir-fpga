#!/usr/bin/env python3
"""上板：加载 overlay → PYNQEngine 跑整网 → 与权重包里的部署参考逐位对拍。

用法（板子上，board_bundle/ 里本脚本与 manifest、weights_pack/ 同目录）：
    sudo python3 run_network_on_board.py --bit design_1.bit \
        --manifest manifest_640.json --pack weights_pack --input weights_pack/input_q.bin
参考文件由机器 B 的 export_network.py 生成（ref_out.bin 为量化语义的期望输出）。
"""
import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from pynq import Overlay                       # noqa: E402
from runner import Runner, PYNQEngine, PYNQArena, load_pack   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default=os.path.join(HERE, "design_1.bit"))
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--pack", required=True)
    ap.add_argument("--input", required=True, help="int8 (3,H,W) 原始文件")
    ap.add_argument("--timeout", type=float, default=None,
                    help="单次卷积等待上限（秒）。默认按工作量估算（见 conv_engine.estimate_seconds）")
    ap.add_argument("--progress", type=int, default=25,
                    help="每多少个卷积打印一行进度（0=不打印）")
    ap.add_argument("--fclk", type=float, default=150.0,
                    help="加载 overlay 后把 FCLK0 设成该频率（MHz）。默认 150（本版 "
                         "implement WNS = +0.217 ns @150 MHz，实测整网逐位一致）；"
                         "时序余量不足的旧 bit 请显式传 125")
    ap.add_argument("--dump-out", default=None,
                    help="把板上输出（int8 (3,H,W)）另存为原始文件，便于本机差异分析")
    ap.add_argument("--pp-ops", default="interp,eca,sam_sum",
                    help="允许走 v2 加速器的层间算子，逗号分隔（add,cat,sam_sum）。"
                         "默认空 = 全部走 numpy（保持逐位一致）")
    ap.add_argument("--no-pp", action="store_true",
                    help="不启用 v2 层间算子加速器（用于 A/B 对照）")
    ap.add_argument("--fuse-conv", action="store_true", default=None,
                    help="启用卷积融合出口（要求对应构建的 bit + --fuse-offsets）；"
                         "不给则读环境变量 PP_FUSE_CONV（默认关）")
    ap.add_argument("--fuse-offsets", default=None,
                    help="out_lut,chsum 两个 AXI-Lite 基地址寄存器偏移，逗号分隔。"
                         "没有该端口的位置写 none。例：只有 FUSE_SUM=1 的 bit 传 "
                         "none,0x40（= 只写 chsum，绝不碰 out_lut）；只有 FUSE_OUT=1 "
                         "的 bit 传 0x78,none")
    ap.add_argument("--verify-chsum", action="store_true",
                    help="板端 chsum 自检：每个融合的 ECA 都拿特征图重算一次通道和，"
                         "与 PL 累加结果逐位对拍（会让那段退化回扫描，仅用于 bring-up）")
    ap.add_argument("--chsum-probe", default=None,
                    help="板端 chsum 端口自检：给指定的卷积（op id 或名字）挂一块 "
                         "chsum 缓冲，跑完拿特征图重算的通道和对拍。本网络没有 "
                         "conv→eca 配对，这是唯一能在真实权重/形状下验证 0x40 端口、"
                         "int32 累加语义与 PS 清零的办法")
    ap.add_argument("--profile", action="store_true",
                    help="按算子类型统计耗时，跑完打印分布表")
    ap.add_argument("--profile-layers", action="store_true",
                    help="逐层计时（写进 --profile-json 的 layers 字段）")
    ap.add_argument("--profile-json", default=None,
                    help="把 profile/总耗时/FCLK/池大小/输出 sha1 写成 JSON（便于留档对比）")
    ap.add_argument("--hashlog", default=None,
                    help="逐算子记录 SHA1 到该文件（与本机对比可定位第一个发散算子）")
    ap.add_argument("--c8", action="store_true", default=None,
                    help="启用 C8 交错布局（等价于环境变量 C8_LAYOUT=1）。"
                         "要求 C8_LAYOUT=1 综合的 bit（relu_en 的 bit2/bit3）；"
                         "不给则读环境变量，默认关 = 全 CHW（与旧版逐位一致）")
    a = ap.parse_args()
    # C8 总开关必须在**构造 arena 之前**定下来（arena 按它决定分配/视图形状）
    if a.c8:
        os.environ["C8_LAYOUT"] = "1"

    import json
    with open(a.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    h, w = manifest["input_hw"]
    img = np.fromfile(a.input, dtype=np.int8)
    assert img.size == 3 * h * w, "输入尺寸 %d != 3*%d*%d" % (img.size, h, w)
    img = img.reshape(3, h, w)

    print("加载 overlay:", a.bit)
    ol = Overlay(a.bit)
    fclk_now = None
    try:
        from pynq.ps import Clocks
        print("FCLK0 = %.1f MHz（overlay 默认；目标 %.1f）"
              % (Clocks.fclk0_mhz, a.fclk if a.fclk else 0))
        if a.fclk:
            Clocks.fclk0_mhz = a.fclk
            print("FCLK0 手动设为 %.1f MHz" % Clocks.fclk0_mhz)
        fclk_now = float(Clocks.fclk0_mhz)
    except Exception as e:
        print("时钟自检不可用（%s）；请手动确认 FCLK0 = 150 MHz" % e)
    index, blob = load_pack(a.pack)
    arena = PYNQArena(manifest)
    print("内存池: %.1f MB" % (arena.size / 1e6))
    n_c8 = sum(1 for v in arena.layout.values() if v)
    print("激活布局: %s（C8 张量 %d 个 / CHW %d 个）"
          % ("C8 交错（relu_en bit2/bit3 逐张量置位）" if n_c8 else "CHW（默认）",
             n_c8, len(arena.layout) - n_c8))
    fuse_offsets = None
    if a.fuse_offsets:
        from conv_engine import norm_fuse_offset
        parts = [norm_fuse_offset(x) for x in a.fuse_offsets.split(",")]
        if len(parts) != 2:
            ap.error("--fuse-offsets 需要两个值：out_lut偏移,chsum偏移"
                     "（没有该端口的位置写 none，例：none,0x40）")
        if parts[0] is None and parts[1] is None:
            ap.error("--fuse-offsets 的两个位置都是 none：没有可写的融合寄存器")
        fuse_offsets = (parts[0], parts[1])
    engine = PYNQEngine(ol, index, blob, arena, fuse_offsets=fuse_offsets)
    if fuse_offsets is not None:
        print("融合寄存器: out_lut=%s  chsum=%s"
              % ("None（不写）" if fuse_offsets[0] is None else hex(fuse_offsets[0]),
                 "None（不写）" if fuse_offsets[1] is None else hex(fuse_offsets[1])))
    engine.timeout_s = a.timeout
    engine.progress_every = a.progress
    engine.n_layers = sum(1 for o in manifest["ops"] if o["op"] == "conv" and o["needed"])
    r = Runner(a.manifest, a.pack, engine=engine, arena=arena,
               fuse_conv=a.fuse_conv)
    r.verify_chsum = bool(a.verify_chsum)
    r.chsum_probe = a.chsum_probe
    if a.chsum_probe is not None and not engine.fuse_sum_ok:
        ap.error("--chsum-probe 需要 chsum 寄存器偏移（--fuse-offsets none,0x40）")
    if r.fuse_conv:
        if fuse_offsets is None:
            ap.error("启用融合出口需要 --fuse-offsets（从该次综合导出的 "
                     "xconv2d_engine_hw.h 读；只有 FUSE_SUM=1 时写 none,0x40）；"
                     "没有偏移就不能往未知地址写寄存器")
        print("卷积融合出口: 已要求启用（out_lut %s，chsum %s）"
              % ("可用" if engine.fuse_out_ok else "本构建没有",
                 "可用" if engine.fuse_sum_ok else "本构建没有"))

    # v2 层间算子加速器（可选；--no-pp 可关掉做 A/B 对照）
    if not getattr(a, "no_pp", False):
        try:
            try:
                from .pp_engine import PPEngine     # 作为包
            except ImportError:
                from pp_engine import PPEngine      # 板上扁平目录
            r.pp = PPEngine(ol)
            r.pp_ops_en = set(x for x in (a.pp_ops or "").split(",") if x)
            print("v2 层间算子加速器: 已启用（%s），启用算子=%s"
                  % (r.pp.name, sorted(r.pp_ops_en) or "无（全走 numpy）"))
        except Exception as e:                       # noqa: BLE001
            print("v2 层间算子加速器不可用（%s）：层间算子继续走 numpy" % e)

    est = sum(engine.unit.estimate_seconds(o["h"], o["w"], o["cin"], o["cout"], o["k"], o["s"])
              for o in r.ops if o["op"] == "conv")
    print("预计卷积耗时 ≈ %.1f 分钟（150MHz cosim 校准 + 1.5× 余量；层间算子另计）"
          % (est / 60.0))

    t0 = time.time()
    if a.profile:
        r.profile = {}
    if a.profile_layers:
        r.profile_ops = []
        if r.profile is None:
            r.profile = {}
    if a.hashlog:
        r.hashlog = []
    out, _ = r.run(img)
    dt = time.time() - t0
    print("整网完成: %.2fs（%d 个算子，%d 个卷积）" % (dt, len(r.ops), r.n_conv))
    if r.fuse_conv:
        print("融合出口统计: conv→cat %d 段（cat 跳过重定标 %d 段），"
              "conv→eca %d 段（ECA 跳过整块扫描 %d 次）"
              % (r._fuse_plan_n, r._fuse_skips,
                 r._fuse_sum_plan_n, r._fuse_sums))
    if a.chsum_probe is not None:
        print("chsum 端口自检（probe=%s）: 跑了 %d 次，与特征图重算不一致 %d 个通道和"
              % (a.chsum_probe, r._chsum_probe_n, r._chsum_probe_bad))
        if r._chsum_probe_bad or not r._chsum_probe_n:
            print("ERROR: chsum 自检失败（偏移不对 / 清零没做 / 累加语义不符）"
                  "——不要相信这版接线")
            sys.exit(2)
    if a.verify_chsum:
        print("chsum 自检: ECA 用 chsum %d 次，与特征图重算不一致 %d 个通道和"
              % (r._fuse_sums, r._chsum_verify_bad))
        if r._chsum_verify_bad:
            print("ERROR: chsum 与特征图重算不一致（清零没做 / 地址写错 / "
                  "RTL 累加语义不符）——不要相信本次结果")
            sys.exit(2)

    if a.hashlog:
        with open(a.hashlog, "w") as f:
            for name, k, shape, h in r.hashlog:
                f.write("%s|%s|%s|%s\n" % (name, k, shape, h))
        print("逐算子哈希已写: %s（%d 条）" % (a.hashlog, len(r.hashlog)))

    if a.profile:
        prof = r.profile
        total = sum(v[0] for v in prof.values())
        print("---- 逐算子类型耗时（%.1fs 合计）----" % total)
        for k, (sec, n) in sorted(prof.items(), key=lambda kv: -kv[1][0]):
            print("  %-16s %8.1fs  %6.1f%%  %4d 次  平均 %.3fs"
                  % (k, sec, 100.0 * sec / max(total, 1e-9), n, sec / max(n, 1)))

    if a.dump_out:
        np.asarray(out, dtype=np.int8).tofile(a.dump_out)
        print("板上输出已存: %s" % a.dump_out)

    if a.profile_json:
        import hashlib
        import json as _json
        import platform
        rec = {
            "when": time.strftime("%Y-%m-%d %H:%M:%S"),
            "host": platform.node(),
            "python": platform.python_version(),
            "numpy": np.__version__,
            "fclk_mhz": fclk_now,
            "pool_mb": arena.size / 1e6,
            "cat_folded": arena.alias["groups_n"] if hasattr(arena, "alias") else None,
            "c8_layout": bool(r.c8),
            "c8_tensors": sum(1 for v in r.layout.values() if v),
            "seconds": dt,
            "n_ops": len(r.ops),
            "n_conv": r.n_conv,
            "profile": {k: {"sec": v[0], "n": v[1]} for k, v in (r.profile or {}).items()},
            "layers": [{"id": i, "name": n, "op": k, "sec": sec}
                       for (i, n, k, sec) in (r.profile_ops or [])],
            "out_sha1": hashlib.sha1(np.ascontiguousarray(out).tobytes()).hexdigest(),
        }
        with open(a.profile_json, "w", encoding="utf-8") as f:
            _json.dump(rec, f, ensure_ascii=False, indent=2)
        print("profile 已写: %s" % a.profile_json)

    ref_path = os.path.join(a.pack, "ref_out.bin")
    if os.path.exists(ref_path):
        ref = np.fromfile(ref_path, dtype=np.int8).reshape(out.shape)
        bad = int((out != ref).sum())
        total = out.size
        print("与部署参考对比: %d/%d 不一致" % (bad, total))
        sys.exit(0 if bad == 0 else 1)
    print("（没有 ref_out.bin，跳过对拍；输出已就绪）")


if __name__ == "__main__":
    main()
