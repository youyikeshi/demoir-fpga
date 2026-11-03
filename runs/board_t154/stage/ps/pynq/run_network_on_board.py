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
    ap.add_argument("--pp-ops", default="interp,eca",
                    help="允许走 v2 加速器的层间算子，逗号分隔（interp,eca）。"
                         "add/cat/sam_sum 走 pp 都不是逐位一致（整网会把 1 LSB 放大到"
                         "~59%% 像素），即使写进来也会被 pp_ops 的守卫回落 numpy")
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
                         "**必须**配 C8_LAYOUT=1 综合的纯 C8 bit：此时整网所有 "
                         "int8 张量都补零到 8 的倍数（3ch->8 / 12ch->16），"
                         "引擎按 relu_en 的 bit2+bit3 声明位校验（缺一位即 "
                         "ERR_LAYOUT）。不给则读环境变量，默认关 = 全 CHW。"
                         "注意：全 CHW 只能配 C8_LAYOUT=0 的 bit。")
    ap.add_argument("--use-k1-ip", action="store_true", default=None,
                    help="把 k=1 卷积路由到新手写 IP（conv_k1_engine / "
                         "k1_axi_wrap），等价于环境变量 USE_K1_IP=1。默认关："
                         "出问题时一行环境变量即可回退老 HLS 引擎。"
                         "**要求 C8_LAYOUT=1**（新 IP 是纯 C8 构建）。")
    ap.add_argument("--k1-ip-name", default=None,
                    help="BD 里 k1 从机的实例名（默认 k1_wrap，也读 K1_IP_NAME）")
    ap.add_argument("--k1-fclk", type=float, default=None,
                    help="K1 IP 所在时钟 MHz，仅用于超时估算（默认 150，或读 "
                         "K1_IP_FCLK_MHZ）")
    ap.add_argument("--use-k3-ip", action="store_true", default=None,
                    help="k=3 层走新手写 IP（conv_k3_engine / k3_wrap）。"
                         "**默认就是开**（USE_K3_IP 默认 1）：新 BD 已删除 HLS "
                         "引擎，k=3 没有退路。这个开关只是把默认值说清楚")
    ap.add_argument("--no-k3-ip", action="store_true",
                    help="显式关掉 k=3 路由（USE_K3_IP=0）：只在旧 bit 的 HLS 仍"
                         "支持 k=3 时才有意义。关掉后 k=3 层走 HLS")
    ap.add_argument("--k3-ip-name", default=None,
                    help="BD 里 k3 从机的实例名（默认 k3_wrap，也读 K3_IP_NAME）")
    ap.add_argument("--use-k5-ip", action="store_true", default=None,
                    help="k=5 层走新手写 IP（conv_k5_engine / k5_wrap）。"
                         "**默认就是开**（USE_K5_IP 默认 1）")
    ap.add_argument("--no-k5-ip", action="store_true",
                    help="显式关掉 k=5 路由（USE_K5_IP=0）：新 bit 的 HLS 对 k=5 "
                         "返回 6，回退只会得到错误")
    ap.add_argument("--k5-ip-name", default=None,
                    help="BD 里 k5 从机的实例名（默认 k5_wrap，也读 K5_IP_NAME）")
    ap.add_argument("--ip-plan", action="store_true",
                    help="只预演**三个**手写 IP 的整体路由（k1/k3/k5 各能接多少层、"
                         "哪些层**没有任何 IP 可用**），不执行卷积。上板前先跑这个")
    ap.add_argument("--k1-relu", type=int, choices=(0, 1), default=None,
                    help="该 k1 bit 综合时的 RELU 参数（默认 1；只有 relu 值相同"
                         "的层会路由。RELU=0 的 bit 才能吃下 conv_proj 线性层）")
    ap.add_argument("--k1-plan", action="store_true",
                    help="只预演 K1 路由（哪些层走新 IP / 哪些回退 / 为什么），"
                         "不执行卷积、不写 K1 寄存器（仍会加载 overlay/分配 arena）")
    a = ap.parse_args()
    # C8 总开关必须在**构造 arena 之前**定下来（arena 按它决定分配/视图形状）
    if a.c8:
        os.environ["C8_LAYOUT"] = "1"
    # K1 开关同理：必须在构造 PYNQEngine 之前
    if a.use_k1_ip:
        os.environ["USE_K1_IP"] = "1"
    if a.k1_ip_name:
        os.environ["K1_IP_NAME"] = str(a.k1_ip_name)
    if a.k1_fclk:
        os.environ["K1_IP_FCLK_MHZ"] = str(a.k1_fclk)
    elif a.fclk:
        # 超时估算跟实际 FCLK 走（--k1-fclk 可单独覆盖）
        os.environ["K1_IP_FCLK_MHZ"] = str(a.fclk)
    if a.k1_relu is not None:
        os.environ["K1_IP_RELU"] = str(a.k1_relu)
    # k3/k5：默认开；--no-kX-ip 显式关（等价 USE_KX_IP=0），--use-kX-ip 只是
    # 把"默认开"写成显式（两者同时给时报错，避免"我到底开没开"的歧义）
    if a.use_k3_ip and a.no_k3_ip:
        ap.error("--use-k3-ip 与 --no-k3-ip 不能同时给")
    if a.use_k5_ip and a.no_k5_ip:
        ap.error("--use-k5-ip 与 --no-k5-ip 不能同时给")
    use_k3 = (True if a.use_k3_ip else (False if a.no_k3_ip else None))
    use_k5 = (True if a.use_k5_ip else (False if a.no_k5_ip else None))
    if use_k3 is not None:
        os.environ["USE_K3_IP"] = "1" if use_k3 else "0"
    if use_k5 is not None:
        os.environ["USE_K5_IP"] = "1" if use_k5 else "0"
    if a.k3_ip_name:
        os.environ["K3_IP_NAME"] = str(a.k3_ip_name)
    if a.k5_ip_name:
        os.environ["K5_IP_NAME"] = str(a.k5_ip_name)

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
    engine = PYNQEngine(ol, index, blob, arena, fuse_offsets=fuse_offsets,
                        use_k1=(True if a.use_k1_ip else None),
                        k1_ip_name=a.k1_ip_name,
                        use_k3=use_k3, k3_ip_name=a.k3_ip_name,
                        use_k5=use_k5, k5_ip_name=a.k5_ip_name)
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

    # K1 IP 路由预演（--k1-plan）：只读、不碰硬件、不跑整网。
    # 上板 bring-up 时先跑这个，确认"该走新 IP 的层真的会走"，再跑整网。
    k1_plan = None
    if a.k1_plan:
        if not engine.use_k1 or engine.k1 is None:
            print("K1 IP: 未接入（USE_K1_IP=%s）—— 路由预演没有意义"
                  % ("1" if engine.use_k1 else "0"))
            return
        k1_plan = engine.k1_plan(r.ops)
        n_r, n_c = len(k1_plan["routed"]), k1_plan["n_conv"]
        print("K1 路由预演: %d/%d 个卷积走 K1 IP，%d 个回退老引擎"
              "（未计 conv→cat/eca 融合规划，实际只会更少）"
              % (n_r, n_c, n_c - n_r))
        for why, n in sorted(k1_plan["reasons"].items(), key=lambda kv: -kv[1]):
            ex = k1_plan["examples"].get(why) or []
            print("  - %-56s %3d 层  例: %s" % (why, n, ", ".join(ex[:3]) or "-"))
        return

    # 三个手写 IP 的整体路由预演（--ip-plan）：只读、不跑整网。
    # 与 --k1-plan 的区别：这里把 k1/k3/k5 一起算，并明确列出"没有任何 IP
    # 可用"的层 —— 那些层会让整网直接停下，是上板前必须先解决的事。
    if a.ip_plan:
        plan = engine.ip_plan(r.ops)
        print("=" * 74)
        print("IP 路由预演：%d 个卷积  K1 %d / K3 %d / K5 %d"
              % (plan["n_conv"], plan["by_k"].get(1, 0),
                 plan["by_k"].get(3, 0), plan["by_k"].get(5, 0)))
        for tag in ("k1", "k3", "k5"):
            p = plan[tag]
            n = plan["by_k"].get({"k1": 1, "k3": 3, "k5": 5}[tag], 0)
            print("  %s: 可路由 %d / %d 层" % (tag.upper(), len(p["routed"]), n))
            for why, cnt in sorted(p["reasons"].items(), key=lambda kv: -kv[1]):
                ex = (p["examples"].get(why) or ["-"])[0]
                print("     - %-52s %3d 层  例: %s" % (why, cnt, ex))
        if plan["unroutable"]:
            print("!! 无 IP 可用 %d 层（整网会在这里停下）："
                  % len(plan["unroutable"]))
            for op, why, tag in plan["unroutable"]:
                print("   - %-44s k=%-2s %s"
                      % (op["name"], op.get("k"), why))
        print("=" * 74)
        return

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

    # 预估：按每层**实际会被谁接**挑估算函数（k1/k3/k5 各用自己的拍数公式）。
    # 没有 HLS 引擎（新 bit）时，落到 HLS 的层按 0 计（它们本来就会报错）。
    ip_est = {"k1": engine.k1, "k3": engine.k3, "k5": engine.k5}
    n_by = {"k1": 0, "k3": 0, "k5": 0, "hls": 0}
    est = 0.0
    for o in r.ops:
        if o["op"] != "conv":
            continue
        tag, _why = engine._conv_route(
            o,
            engine.arena.is_c8(o["in"][0]) if o.get("in") else False,
            engine.arena.is_c8(o["out"][0]) if o.get("out") else False)
        n_by[tag] += 1
        eng = ip_est.get(tag)
        if eng is None and tag == "hls" and engine.unit is not None:
            eng = engine.unit
        if eng is not None:
            est += eng.estimate_seconds(o["h"], o["w"], o["cin"], o["cout"],
                                        o["k"], o["s"])
    print("预计卷积耗时 ≈ %.1f 分钟（K1 %d / K3 %d / K5 %d / HLS %d 层；"
          "层间算子另计）"
          % (est / 60.0, n_by["k1"], n_by["k3"], n_by["k5"], n_by["hls"]))
    if engine.unit is None:
        print("（本 bit 没有老 HLS 引擎；任何 HLS 层的估算按 0 计）")

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
    if engine.use_k1:
        # 逐原因回退统计（A/B 时看这个判断"新 IP 到底接了几层"）
        print(engine.k1_summary())
    if engine.use_k3:
        print(engine.k3_summary())
    if engine.use_k5:
        print(engine.k5_summary())
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
