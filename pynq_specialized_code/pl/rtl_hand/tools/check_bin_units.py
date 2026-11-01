#!/usr/bin/env python3
"""check_bin_units.py —— 二值引擎各单元的**集成就绪度**静态检查

四个单元（bin_conv_core / bin_rprelu / bin_sam / bin_interp）要能被同一个
AXI 包装层套住、接到同一条流水线上，就必须满足同一套接口契约。本脚本把
"能不能集成"变成可机检的项，而不是等到搭 wrapper 时才发现对不上。

检查项：
  1. 必备端口齐全（ctrl_* 握手 + 单主内存口 + cfg_*/base_*）
  2. 禁止出现 AXI 端口（集成阶段由 wrapper 负责，单元内不许有）
  3. 内存口位宽一致：rd_en 1 / rd_addr 32 / rd_data 64 / wr_en 1 / wr_addr 32 / wr_data 8
  4. 地址一律 32 bit（与现有 k1/k3/k5 及 TB 的 1MB 存储一致）
  5. 复用既有门禁：无悬空 output、拼接宽度
  6. 端口的 input/output 方向与契约一致

用法：
    python3 tools/check_bin_units.py            # 检查全部四个
    python3 tools/check_bin_units.py bin_sam    # 只查一个
退出码：0 全部就绪；1 有问题
"""
import os
import re
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODDIR = os.path.join(ROOT, "modules")

UNITS = ["bin_conv_core", "bin_rprelu", "bin_sam", "bin_interp"]

# 契约：端口名 -> (方向, 位宽或 None 表示不检查)
REQUIRED = {
    "ap_clk":        ("input", 1),
    "ap_rst_n":      ("input", 1),
    "ctrl_start":    ("input", 1),
    "ctrl_done":     ("output", 1),
    "ctrl_busy":     ("output", 1),
    "mem_rd_en":     ("output", 1),
    "mem_rd_addr":   ("output", 32),
    "mem_rd_data":   ("input", 64),
    "mem_wr_en":     ("output", 1),
    "mem_wr_addr":   ("output", 32),
    "mem_wr_data":   ("output", 8),
}
# 单元特有的 cfg_*/base_*：只要求方向对
CFG_IN = re.compile(r"^(cfg_|base_)")


def parse_ports(path):
    """抓第一个 module 的端口表，返回 {名字: (方向, 位宽字符串)}。

    端口表里可能有 parameter 列表，先跳过 #(...) 段。
    """
    src = open(path, encoding="utf-8", errors="replace").read()
    m = re.search(r"\bmodule\s+(\w+)", src)
    if not m:
        raise SystemExit("找不到 module 声明: %s" % path)
    name = m.group(1)
    i = m.end()
    # 跳过 #( ... ) 参数表（按括号配平）
    j = src.find("(", i)
    if j >= 0 and src[i:j].strip().startswith("#"):
        depth = 0
        k = j
        while k < len(src):
            if src[k] == "(":
                depth += 1
            elif src[k] == ")":
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
        i = k
    j = src.find("(", i)
    depth = 0
    k = j
    while k < len(src):
        if src[k] == "(":
            depth += 1
        elif src[k] == ")":
            depth -= 1
            if depth == 0:
                break
        k += 1
    portblock = src[j + 1:k]

    ports = {}
    for raw in portblock.split(","):
        t = " ".join(raw.split())
        if not t:
            continue
        mm = re.match(r"(input|output|inout)\s+(?:logic|wire|reg|bit)?\s*"
                      r"(?:signed\s*)?(?:\[(\d+):(\d+)\])?\s*(\w+)$", t)
        if not mm:
            # 允许参数化位宽，如 [GW-1:0]，位宽记为 "param"
            mm2 = re.match(r"(input|output|inout)\s+(?:logic|wire|reg|bit)?\s*"
                           r"(?:signed\s*)?(\[[^\]]+\])?\s*(\w+)$", t)
            if not mm2:
                continue
            d, w, n = mm2.group(1), mm2.group(2), mm2.group(3)
            ports[n] = (d, "param")
            continue
        d = mm.group(1)
        if mm.group(2) is not None:
            width = int(mm.group(2)) - int(mm.group(3)) + 1
        else:
            width = 1
        ports[mm.group(4)] = (d, width)
    return name, ports


def check_one(mod):
    path = os.path.join(MODDIR, mod + ".v")
    problems = []
    if not os.path.exists(path):
        return ["文件不存在: " + path], None
    name, ports = parse_ports(path)
    if name != mod:
        problems.append("module 名是 %s，文件名是 %s.v" % (name, mod))

    # 1&3&6: 必备端口、方向、位宽
    for p, (want_dir, want_w) in REQUIRED.items():
        if p not in ports:
            problems.append("缺少必备端口 %s" % p)
            continue
        d, w = ports[p]
        if d != want_dir:
            problems.append("%s 方向是 %s，契约为 %s" % (p, d, want_dir))
        if w != "param" and want_w is not None and w != want_w:
            problems.append("%s 位宽 %s，契约为 %s" % (p, w, want_w))

    # 2: 不许出现 AXI 端口
    for p in ports:
        if re.match(r"^[ms]_axi", p):
            problems.append("出现 AXI 端口 %s（单主内存口的契约不允许）" % p)

    # cfg_*/base_* 必须都是 input
    for p, (d, w) in ports.items():
        if CFG_IN.match(p) and d != "input":
            problems.append("%s 应为 input，实际 %s" % (p, d))

    # 4: 其余端口不应出现 > 64 bit 的（地址/数据都在中心化）
    for p, (d, w) in ports.items():
        if w != "param" and w > 64:
            problems.append("%s 位宽 %d > 64，契约不允许" % (p, w))
    return problems, (name, len(ports))


def main():
    targets = sys.argv[1:] or UNITS
    print("=" * 66)
    print(" 二值引擎单元 · 集成就绪度检查")
    print("=" * 66)
    bad_total = 0
    for mod in targets:
        problems, info = check_one(mod)
        if info:
            nports = info[1]
        else:
            nports = 0
        status = "就绪" if not problems else "有问题"
        print("\n[%s] 端口数 %d   %s" % (mod, nports, status))
        for p in problems:
            print("   ! %s" % p)
        bad_total += len(problems)

    # 5: 复用既有门禁
    print("\n--- 复用既有静态门禁 ---")
    for script, label in (("check_undriven_out.py", "未驱动 output"),
                          ("check_concat_width.py", "拼接宽度")):
        sp = os.path.join(HERE, script)
        if not os.path.exists(sp):
            print("  跳过（不存在）%s" % script)
            continue
        r = subprocess.run([sys.executable, sp], capture_output=True, text=True)
        tail = (r.stdout or r.stderr).strip().splitlines()
        print("  %-16s %s" % (label, tail[-1] if tail else "(无输出)"))
        if r.returncode != 0:
            bad_total += 1

    print("\n" + "=" * 66)
    if bad_total == 0:
        print(" 结论：4 个单元接口一致，可被同一个 AXI wrapper 套住")
    else:
        print(" 结论：%d 处不一致，必须先修再集成" % bad_total)
    print("=" * 66)
    return 0 if bad_total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
