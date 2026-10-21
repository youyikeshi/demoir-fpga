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
    ap.add_argument("--fclk", type=float, default=None,
                    help="加载 overlay 后把 FCLK0 设成该频率（MHz），例如 150")
    ap.add_argument("--dump-out", default=None,
                    help="把板上输出（int8 (3,H,W)）另存为原始文件，便于本机差异分析")
    ap.add_argument("--profile", action="store_true",
                    help="按算子类型统计耗时，跑完打印分布表")
    ap.add_argument("--profile-layers", action="store_true",
                    help="逐层计时（写进 --profile-json 的 layers 字段）")
    ap.add_argument("--profile-json", default=None,
                    help="把 profile/总耗时/FCLK/池大小/输出 sha1 写成 JSON（便于留档对比）")
    ap.add_argument("--hashlog", default=None,
                    help="逐算子记录 SHA1 到该文件（与本机对比可定位第一个发散算子）")
    a = ap.parse_args()

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
        print("FCLK0 = %.1f MHz（期望 150）" % Clocks.fclk0_mhz)
        if a.fclk:
            Clocks.fclk0_mhz = a.fclk
            print("FCLK0 手动设为 %.1f MHz" % Clocks.fclk0_mhz)
        fclk_now = float(Clocks.fclk0_mhz)
    except Exception as e:
        print("时钟自检不可用（%s）；请手动确认 FCLK0 = 150 MHz" % e)
    index, blob = load_pack(a.pack)
    arena = PYNQArena(manifest)
    print("内存池: %.1f MB" % (arena.size / 1e6))
    engine = PYNQEngine(ol, index, blob, arena)
    engine.timeout_s = a.timeout
    engine.progress_every = a.progress
    engine.n_layers = sum(1 for o in manifest["ops"] if o["op"] == "conv" and o["needed"])
    r = Runner(a.manifest, a.pack, engine=engine, arena=arena)

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
