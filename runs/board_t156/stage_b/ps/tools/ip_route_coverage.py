#!/usr/bin/env python3
"""上板前的**路由覆盖表**：整网 247 个卷积，每个到底会被谁接走？

只读工具（不碰硬件、不导入 pynq、不跑卷积）：
  * 用 ``manifest_640.json`` 的 op 形状 + 权重包 ``index.json`` 的容器信息；
  * 复用 runner 里的**同一份** ``k1_route_check / k3_route_check /
    k5_route_check``（纯函数），所以这里算出来的结果与上板时 ``conv()`` 的
    判定逐条一致（不会出现"预演说能走、上板说不能走"）；
  * 用 .hwh 判断 bit 里到底有哪些引擎（k1_wrap / k3_wrap / conv2d_engine）。

它模拟的是 **runner.PYNQEngine.conv() 的真实语义**：

  · k=1：路由失败 ⇒ 回退老 HLS（若 bit 里没有 HLS ⇒ 直接报错）；
  · k=3/k=5：开关开着却路由失败 ⇒ **硬错**（不回退 HLS）；开关关掉
    （``USE_K3_IP=0``/``USE_K5_IP=0``）才允许回退 HLS；
  · 任何核型，只要"没被手写 IP 接住 + bit 里没有 HLS" ⇒ **整网在这里停下**。

用法（在 ``pynq_specialized_code/ps`` 目录）::

    python3 tools/ip_route_coverage.py
    python3 tools/ip_route_coverage.py --use-k1 1        # 新 bit 的推荐配置
    python3 tools/ip_route_coverage.py --pack ../board/board_bundle/weights_pack/index.json
    python3 tools/ip_route_coverage.py --hwh ../board/overlay_engine_150mhz/design_1.hwh
    python3 tools/ip_route_coverage.py --c8 0            # CHW 对照（必然全灭）

判据：最后一行 ``==== 路由覆盖：... ====``；存在"无 IP 可用"的层时退出码 1
（``--warn-only`` 可改成 0）。**这是上板前必须知道的事**。
"""
import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.abspath(os.path.join(HERE, os.pardir))
PYNQ = os.path.join(PS, "pynq")
sys.path.insert(0, PYNQ)

import c8_layout                     # noqa: E402
import runner                        # noqa: E402

# 仓库里：ps/tools/*.py（HERE=ps/tools, PS=ps）
# 板上：  board_bundle/ 扁平目录（HERE=PS=board_bundle）
DEFAULT_MANIFESTS = [
    os.path.join(PS, "manifest_640.json"),
    os.path.join(HERE, "manifest_640.json"),
]
DEFAULT_PACKS = [
    os.path.join(PS, os.pardir, "deploy", "weights_pack", "index.json"),
    os.path.join(PS, os.pardir, "board", "board_bundle", "weights_pack",
                 "index.json"),
    os.path.join(HERE, "weights_pack", "index.json"),
]
DEFAULT_HWHS = [
    os.path.join(PS, os.pardir, "board", "overlay_engine_150mhz",
                 "design_1.hwh"),
    os.path.join(HERE, "design_1.hwh"),
]


def _first_existing(paths):
    return next((p for p in paths if os.path.exists(p)), None)

# hwh 模块 VLNV/实例名 -> 引擎
_ENGINE_ALIASES = (
    ("conv2d_engine", "hls"),
    ("k1_axi_wrap", "k1"),
    ("k1_wrap", "k1"),
    ("k3_axi_wrap", "k3"),
    ("k3_wrap", "k3"),
    ("k5_axi_wrap", "k5"),
    ("k5_wrap", "k5"),
    ("conv_k5_engine", "k5"),
)

_TAG_CN = {"k1": "K1", "k3": "K3", "k5": "K5", "hls": "HLS"}


def parse_hwh(path):
    """从 .hwh 读 ``{引擎: [实例名]}`` 与 ``{实例名: (base, high)}``。"""
    engines, mem = {}, {}
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:                            # noqa: BLE001
        return None, None, "解析失败：%r" % (exc,)
    mods = {}
    for m in root.iter("MODULE"):
        inst = m.get("INSTANCE") or m.get("NAME")
        if inst:
            mods[str(inst)] = str(m.get("VLNV") or "")
    for mm in root.iter("MEMRANGE"):
        inst = mm.get("INSTANCE")
        if not inst or inst == "ps7":
            continue
        try:
            base = int(str(mm.get("BASEVALUE")), 0)
            high = int(str(mm.get("HIGHVALUE")), 0)
        except (TypeError, ValueError):
            continue
        mem.setdefault(str(inst), (base, high))
    for inst, vlnv in mods.items():
        blob = (inst + " " + vlnv).lower()
        for key, eng in _ENGINE_ALIASES:
            if key in blob:
                engines.setdefault(eng, [])
                if inst not in engines[eng]:
                    engines[eng].append(inst)
    return engines, mem, None


def _fmt_shape(op):
    return ("H=%d W=%d cin=%d cout=%d k=%d s=%d d=%d pad=%d relu=%s"
            % (op["h"], op["w"], op["cin"], op["cout"], op["k"], op["s"],
               op["d"], op["pad"], bool(op.get("relu", True))))


def _check(op, entry, c8_in, c8_out):
    """按核型调对应的 route check；返回 ``(tag, ok, reason)``。"""
    tag = runner.route_tag(op)
    if tag == "k1":
        ok, why = runner.k1_route_check(op, entry, c8_in, c8_out)
    elif tag == "k3":
        ok, why = runner.k3_route_check(op, entry, c8_in, c8_out)
    elif tag == "k5":
        ok, why = runner.k5_route_check(op, entry, c8_in, c8_out)
    elif tag is None:
        ok, why = False, "不是 conv"
    else:
        ok, why = False, "k=%r 没有对应的手写 IP" % (op.get("k"),)
        tag = "hls"
    return tag, ok, why


def analyze(ops, layers, layout, use, engines, has_hwh, has_hls):
    """模拟 conv() 的取舍，返回逐层归属与"无 IP 可用"清单。"""
    out = {"n_conv": 0, "by_k": {}, "cap_ok": {}, "routed": {},
           "hls": [], "unroutable": [], "other_k": []}
    for tag in ("k1", "k3", "k5"):
        out["cap_ok"][tag] = 0
        out["routed"][tag] = 0
    for op in ops:
        if op.get("op") != "conv" or not op.get("needed", True):
            continue
        k = int(op.get("k", 0) or 0)
        out["n_conv"] += 1
        out["by_k"][k] = out["by_k"].get(k, 0) + 1
        entry = layers.get(op.get("weights_key"))
        c8_in = bool(layout.get(op["in"][0], False)) if op.get("in") else False
        c8_out = (bool(layout.get(op["out"][0], False))
                  if op.get("out") else False)
        tag, cap_ok, why = _check(op, entry, c8_in, c8_out)
        if tag not in ("k1", "k3", "k5"):
            out["other_k"].append((op, why))
            out["unroutable"].append((op, why, "hls", "核型没有手写 IP"))
            continue
        if cap_ok:
            out["cap_ok"][tag] += 1
        # bit 里有没有这个 IP（没有 .hwh 就按"有"算，只算能力）
        ip_present = (tag in engines) if has_hwh else True
        if cap_ok and ip_present and use[tag]:
            out["routed"][tag] += 1
            continue
        # ---- 没被手写 IP 接走：决定"回退 HLS"还是"无 IP 可用" ----
        if not cap_ok:
            detail = why
        elif not ip_present:
            detail = "bit 里没有 %s IP（.hwh 里找不到 %s）" % (
                _TAG_CN[tag], {"k1": "k1_wrap", "k3": "k3_wrap",
                               "k5": "k5_wrap"}[tag])
        else:
            detail = "%s=0（显式关掉）" % {"k1": "USE_K1_IP",
                                          "k3": "USE_K3_IP",
                                          "k5": "USE_K5_IP"}[tag]
        hard = (tag in ("k3", "k5") and use[tag])
        if hard:
            out["unroutable"].append(
                (op, detail, tag, "开关开着 → 硬错，不回退 HLS"))
        elif has_hls:
            out["hls"].append((op, detail, tag))
        else:
            out["unroutable"].append(
                (op, detail, tag, "本 bit 没有老 HLS 引擎"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=None,
                    help="默认依次找 ps/manifest_640.json / 脚本同目录/"
                         "manifest_640.json")
    ap.add_argument("--pack", default=None,
                    help="index.json 路径（默认先找 deploy/weights_pack，"
                         "再找 board/board_bundle/weights_pack）")
    ap.add_argument("--hwh", default=None,
                    help="overlay 的 .hwh（默认 board/overlay_engine_150mhz/"
                         "design_1.hwh；不存在则假定没有 HLS）")
    ap.add_argument("--c8", type=int, choices=(0, 1), default=1,
                    help="布局假设：1=C8_LAYOUT=1（上板唯一合法），0=CHW 对照")
    ap.add_argument("--use-k1", type=int, choices=(0, 1), default=0,
                    help="USE_K1_IP（runner 的默认值是**关**；新 bit 请传 1）")
    ap.add_argument("--use-k3", type=int, choices=(0, 1), default=1,
                    help="USE_K3_IP（默认 1）")
    ap.add_argument("--use-k5", type=int, choices=(0, 1), default=1,
                    help="USE_K5_IP（默认 1）")
    ap.add_argument("--max-list", type=int, default=0,
                    help="'无 IP 可用' 清单最多打印多少层（0=全部）")
    ap.add_argument("--warn-only", action="store_true",
                    help="即使存在无 IP 可用的层也退出 0")
    a = ap.parse_args()

    # ---- 数据来源 ----
    if a.manifest is None:
        a.manifest = _first_existing(DEFAULT_MANIFESTS)
        if a.manifest is None:
            raise SystemExit("找不到 manifest_640.json，用 --manifest 指定")
    with open(a.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    pack = a.pack
    if pack is None:
        pack = next((p for p in DEFAULT_PACKS if os.path.exists(p)), None)
        if pack is None:
            raise SystemExit("找不到权重包的 index.json，用 --pack 指定")
    with open(pack, "r", encoding="utf-8") as f:
        index = json.load(f)
    layers = index.get("layers", {})
    ops = [o for o in manifest["ops"] if o.get("needed", True)]
    layout = c8_layout.table(manifest, on=bool(a.c8))

    hwh = a.hwh or _first_existing(DEFAULT_HWHS)
    engines, mem, herr = None, None, "未提供 .hwh"
    if hwh:
        engines, mem, herr = parse_hwh(hwh)
    has_hwh = engines is not None
    if engines is None:
        engines, mem = {}, {}
    has_hls = "hls" in engines

    print("=" * 78)
    print("路由覆盖表（只读预演；与 runner.PYNQEngine.conv() 同一份判定）")
    print("=" * 78)
    print("manifest : %s" % os.path.abspath(a.manifest))
    print("权重包   : %s" % os.path.abspath(pack))
    print("布局     : %s" % ("C8_LAYOUT=1（上板唯一合法）" if a.c8
                             else "CHW（纯 C8 的手写 IP 一层都不会接）"))
    print("开关     : USE_K1_IP=%d  USE_K3_IP=%d  USE_K5_IP=%d"
          % (a.use_k1, a.use_k3, a.use_k5))
    if hwh:
        print("overlay  : %s" % os.path.abspath(hwh))
        if herr:
            print("           （.hwh %s）" % herr)
        else:
            for eng in ("hls", "k1", "k3", "k5"):
                insts = engines.get(eng) or []
                if not insts:
                    print("           %-3s : **不在 bit 里**" % _TAG_CN[eng])
                    continue
                for inst in insts:
                    base = mem.get(inst)
                    print("           %-3s : %-24s @ %s"
                          % (_TAG_CN[eng], inst,
                             ("%#010x..%#010x" % base) if base else "?"))
    else:
        print("overlay  : （没找到 .hwh）按**没有老 HLS 引擎**预演")

    convs = [o for o in ops if o.get("op") == "conv"]
    print("-" * 78)
    by_k = {}
    for o in convs:
        kk = int(o.get("k", 0) or 0)
        by_k[kk] = by_k.get(kk, 0) + 1
    print("卷积总数 : %d（needed）  按 k 分类: %s"
          % (len(convs),
             ", ".join("k=%d:%d" % kv for kv in sorted(by_k.items()))))

    use = {"k1": bool(a.use_k1), "k3": bool(a.use_k3), "k5": bool(a.use_k5)}
    res = analyze(ops, layers, layout, use, engines, has_hwh, has_hls)

    # ---- 主表 ----
    print("-" * 78)
    print("%-4s %6s %10s %9s %5s %8s   %s"
          % ("IP", "层数", "能力可路由", "bit里有IP", "开关", "实际路由", "无IP可用"))
    for tag in ("k1", "k3", "k5"):
        n = res["by_k"].get({"k1": 1, "k3": 3, "k5": 5}[tag], 0)
        cap = res["cap_ok"][tag]
        present = ("是" if engines.get(tag) else "否") if has_hwh else "?"
        routed = res["routed"][tag]
        n_un = sum(1 for _o, _w, t, _e in res["unroutable"] if t == tag)
        print("%-4s %6d %10d %9s %5d %8d   %d"
              % (_TAG_CN[tag], n, cap, present, 1 if use[tag] else 0,
                 routed, n_un))

    # ---- 原因汇总 ----
    print("-" * 78)
    cap_reasons = {}
    for op in convs:
        entry = layers.get(op.get("weights_key"))
        c8_in = bool(layout.get(op["in"][0], False)) if op.get("in") else False
        c8_out = (bool(layout.get(op["out"][0], False))
                  if op.get("out") else False)
        tag, ok, why = _check(op, entry, c8_in, c8_out)
        if not ok:
            cap_reasons[(tag, why)] = cap_reasons.get((tag, why), 0) + 1
    if cap_reasons:
        print("能力不可路由的原因：")
        for (tag, why), n in sorted(cap_reasons.items(), key=lambda kv: -kv[1]):
            print("   %3d 层  %-4s  %s" % (n, _TAG_CN.get(tag, tag), why))
    else:
        print("能力：所有层的形状/容器都能被对应 IP 接住")

    # ---- 实际路由 ----
    print("-" * 78)
    routed_n = sum(res["routed"].values())
    print("实际路由 : K1 %d 层 / K3 %d 层 / K5 %d 层（共 %d / %d）"
          % (res["routed"]["k1"], res["routed"]["k3"], res["routed"]["k5"],
             routed_n, res["n_conv"]))

    if res["hls"]:
        per = {}
        for op, why, tag in res["hls"]:
            per[(tag, why)] = per.get((tag, why), 0) + 1
        print("落到老 HLS（%d 层，逐原因）：" % len(res["hls"]))
        for (tag, why), n in sorted(per.items(), key=lambda kv: -kv[1]):
            print("   %3d 层  %-4s → HLS：%s" % (n, _TAG_CN.get(tag, tag), why))
        print("   注：K1_IN_HLS=0 / K5_IN_HLS=0 的构建对 k=1/k=5 返回 5/6 ⇒ "
              "这些层**实际会失败**（不只是变慢）")

    # ---- 无 IP 可用 ----
    if res["unroutable"]:
        print("-" * 78)
        print("!! 无 IP 可用（%d 层）—— 整网会在这里停下：" % len(res["unroutable"]))
        shown = (res["unroutable"] if a.max_list <= 0
                 else res["unroutable"][:a.max_list])
        for op, why, tag, extra in shown:
            print("   %-46s k=%-3s %s\n        %s  ← %s"
                  % (op["name"], op.get("k"), _fmt_shape(op), why, extra))
        if a.max_list > 0 and len(res["unroutable"]) > a.max_list:
            print("   …（另有 %d 层）" % (len(res["unroutable"]) - a.max_list))
    else:
        print("-" * 78)
        print("OK: 没有任何'无 IP 可用'的层（按上面的开关与 .hwh 假设）")

    # ---- 理论耗时（只算实际路由的层）----
    print("-" * 78)
    fclk = {"k1": 150e6, "k3": 150e6, "k5": 150e6}
    try:
        fclk["k1"] = runner.k1_engine.fclk_hz()
        fclk["k3"] = runner.k3_engine.fclk_hz()
        fclk["k5"] = runner.k5_engine.fclk_hz()
    except Exception:                                   # noqa: BLE001
        pass
    total = 0.0
    for tag in ("k1", "k3", "k5"):
        cyc = 0
        n = 0
        for op in convs:
            entry = layers.get(op.get("weights_key"))
            c8_in = bool(layout.get(op["in"][0], False)) if op.get("in") else False
            c8_out = (bool(layout.get(op["out"][0], False))
                      if op.get("out") else False)
            t, cap_ok, _why = _check(op, entry, c8_in, c8_out)
            present = (tag in engines) if has_hwh else True
            if t != tag or not cap_ok or not present or not use[tag]:
                continue
            try:
                if tag == "k1":
                    cyc += runner.k1_engine.theoretical_cycles(
                        op["h"], op["w"], op["cin"], op["cout"],
                        lanes=32, k=1)
                elif tag == "k3":
                    cyc += runner.k3_engine.theoretical_cycles(
                        op["h"], op["w"], op["cin"], op["cout"],
                        s=op["s"], hout=op.get("hout"), wout=op.get("wout"))
                else:
                    cyc += runner.k5_engine.theoretical_cycles(
                        op["h"], op["w"], op["cin"], op["cout"],
                        s=op["s"], hout=op.get("hout"), wout=op.get("wout"))
                n += 1
            except Exception:                           # noqa: BLE001
                pass
        sec = cyc / max(fclk[tag], 1.0)
        total += sec
        print("理论耗时 %-2s : %6d 层  %12d 拍  %8.3f s @ %.0f MHz"
              % (_TAG_CN[tag], n, cyc, sec, fclk[tag] / 1e6))
    print("理论耗时合计（不含层间算子/PS 开销）: %.3f s" % total)

    # ---- 结论 ----
    print("=" * 78)
    ok = not res["unroutable"]
    if not ok:
        print("==== 路由覆盖：%d 个卷积，%d 层**无 IP 可用** → 整网不会跑通 ===="
              % (res["n_conv"], len(res["unroutable"])))
    else:
        print("==== 路由覆盖：%d 个卷积全部有引擎承接 ====" % res["n_conv"])
        if res["hls"]:
            print("（其中 %d 层落在老 HLS；若该构建已删除对应核型，仍会失败）"
                  % len(res["hls"]))
    print("新 bit（无 HLS）的推荐配置：C8_LAYOUT=1 USE_K1_IP=1 USE_K3_IP=1 "
          "USE_K5_IP=%d" % a.use_k5)
    return 0 if (ok or a.warn_only) else 1


if __name__ == "__main__":
    sys.exit(main())
