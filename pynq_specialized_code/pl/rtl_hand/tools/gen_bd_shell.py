#!/usr/bin/env python3
"""gen_bd_shell.py —— 从 SV wrapper 的端口表自动生成 BD 引脚外壳 `*_bd.v`

为什么需要：Vivado 的 `create_bd_cell -type module -reference` 只吃 Verilog/VHDL，
而我们的 wrapper 是 SystemVerilog；且端口上必须带 `X_INTERFACE_INFO` 属性，BD 才能
把它们聚成 AXI 接口。手抄 ~40 个端口极易漏标注，所以脚本生成。

用法：
    python3 tools/gen_bd_shell.py bin_unit_axi_wrap
    python3 tools/gen_bd_shell.py conv_uni_axi_wrap --wr-prefix m_axi_out
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODDIR = os.path.join(ROOT, "modules")

# 每个 AXI 接口的信号 → 标准 AXI4 名字
AXI_SIGS = [
    ("AWADDR", "awaddr", "I"), ("AWPROT", "awprot", "I"), ("AWVALID", "awvalid", "I"),
    ("AWREADY", "awready", "O"),
    ("WDATA", "wdata", "I"), ("WSTRB", "wstrb", "I"), ("WVALID", "wvalid", "I"),
    ("WREADY", "wready", "O"), ("WLAST", "wlast", "I"),
    ("BRESP", "bresp", "O"), ("BVALID", "bvalid", "O"), ("BREADY", "bready", "I"),
    ("ARADDR", "araddr", "I"), ("ARPROT", "arprot", "I"),
    ("ARVALID", "arvalid", "I"), ("ARREADY", "arready", "O"),
    ("ARLEN", "arlen", "I"), ("ARSIZE", "arsize", "I"), ("ARBURST", "arburst", "I"),
    ("ARID", "arid", "I"),
    ("RDATA", "rdata", "O"), ("RRESP", "rresp", "O"), ("RVALID", "rvalid", "O"),
    ("RREADY", "rready", "I"), ("RLAST", "rlast", "O"), ("RID", "rid", "O"),
]


def parse_ports(path):
    s = open(path, encoding="utf-8", errors="replace").read()
    m = re.search(r"\bmodule\s+(\w+)", s)
    name = m.group(1)
    i = m.end()
    j = s.find("(", i)
    if j >= 0 and s[i:j].strip().startswith("#"):
        d = 0
        k = j
        while k < len(s):
            if s[k] == "(":
                d += 1
            elif s[k] == ")":
                d -= 1
                if d == 0:
                    k += 1
                    break
            k += 1
        i = k
    j = s.find("(", i)
    d = 0
    k = j
    while k < len(s):
        if s[k] == "(":
            d += 1
        elif s[k] == ")":
            d -= 1
            if d == 0:
                break
        k += 1
    ports = []
    # 端口表里可能带 (* attr *) 与注释，先剥掉
    blk = re.sub(r"/\*.*?\*/", "", s[j + 1:k], flags=re.S)
    blk = re.sub(r"//[^\n]*", "", blk)
    blk = re.sub(r"\(\*.*?\*\)", "", blk, flags=re.S)
    for raw in blk.split(","):
        t = " ".join(raw.split())
        if not t:
            continue
        mm = re.match(r"(input|output|inout)\s+(?:logic|wire|reg)?\s*(?:signed\s*)?"
                      r"(\[[^\]]+\])?\s*(\w+)$", t)
        if mm:
            ports.append((mm.group(3), mm.group(1), mm.group(2) or "", t))
    return name, ports


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("module")
    ap.add_argument("--wr-prefix", default=None,
                    help="写通道前缀（默认 m_axi_wr）")
    a = ap.parse_args()
    mod = a.module
    wr = a.wr_prefix or "m_axi_wr"
    src = os.path.join(MODDIR, mod + ".v")
    name, ports = parse_ports(src)
    if not ports:
        sys.exit("解析不出 %s 的端口（%d 个）" % (src, len(ports)))

    RD, SA = "m_axi_rd", "s_axi"

    def axi_attr(ifname, sig):
        return '    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 %s %s" *)' % (ifname, sig)

    # FREQ_HZ 必须与 BD 里 PS7 的 FCLK 一致：实测本设计只能跑 40~48 MHz，
    # 这里统一写 50000000（写 150000000 会让 BD 报 BD 41-237 FREQ_HZ 不匹配）。
    # AXI 接口必须带 X_INTERFACE_PARAMETER（FREQ_HZ / PROTOCOL 等），否则
    # BD 的 apply_bd_automation 会报 "m_axi is not associated to any clock pin"
    # 和 "FREQ_HZ does not match"。踩过。
    def axi_param(ifname):
        return ('    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME %s, '
                'PROTOCOL AXI3, ADDR_WIDTH 32, DATA_WIDTH 64, '
                'FREQ_HZ 40000000, HAS_BURST 1, HAS_CACHE 0, HAS_QOS 0, '
                'HAS_REGION 0, HAS_LOCK 0, HAS_PROT 1, HAS_WSTRB 1, '
                'MAX_BURST_LENGTH 16, NUM_READ_OUTSTANDING 16, '
                'NUM_WRITE_OUTSTANDING 16" *)' % ifname)

    def sig_attr(kind, name, extra=""):
        # kind: clock / reset
        return ('    (* X_INTERFACE_INFO = "xilinx.com:signal:%s:1.0 %s %s" *)' %
                (kind, name, "CLK" if kind == "clock" else "RST"))

    def sig_param(name, extra):
        return '    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME %s, %s" *)' % (name, extra)

    out = []
    out.append("// ============================================================================")
    out.append("//  %s_bd.v —— %s 的 BD 引脚外壳（**自动生成**，勿手改）" % (mod, mod))
    out.append("//  生成器：tools/gen_bd_shell.py %s" % mod)
    out.append("//")
    out.append("//  为什么需要：BD 的 create_bd_cell -type module -reference 只吃 Verilog/VHDL，")
    out.append("//  而 %s 是 SystemVerilog；且端口需要 X_INTERFACE_INFO 属性，BD 才能" % mod)
    out.append("//  把它们聚成 AXI 接口。本文件内部例化的 %s 仍可以是 SV。" % mod)
    out.append("// ============================================================================")
    out.append("`timescale 1ns/1ps")
    # 端口位宽里可能引用 wrapper 的 localparam（如 AXI_ID_W），外壳必须自带
    src_txt = open(src, encoding="utf-8", errors="replace").read()
    lps = (re.findall(r"localparam\s+int\s+(\w+)\s*=\s*(\d+)\s*;", src_txt)
           + re.findall(r"parameter\s+int\s+(\w+)\s*=\s*(\d+)\s*,", src_txt)
           + re.findall(r"parameter\s+int\s+(\w+)\s*=\s*(\d+)\s*\)", src_txt))
    used = set()
    for (_pn, _d, width, _r) in ports:
        for ident in re.findall(r"[A-Za-z_]\w*", width or ""):
            if ident not in ("signed",):
                used.add(ident)
    extra = [(n_, v) for (n_, v) in lps if n_ in used]
    # 参数必须在模块名之后的 #(...) 里声明（Verilog-2001 语法），
    # 不能塞进 ANSI 端口列表内部
    if extra:
        out.append("module %s_bd #(" % mod)
        out.append(",\n".join("    parameter %s = %s" % (n_, v) for n_, v in extra))
        out.append(") (")
    else:
        out.append("module %s_bd (" % mod)
    decls = []
    _param_done = set()
    for (pn, dirn, width, _raw) in ports:
        d = "input " if dirn == "input" else "output"
        # ⚠ 必须先判时钟/复位：它们是 s_axi_aclk / s_axi_aresetn，
        #   会被下面的 s_axi_ 前缀分支误抓成 AXI 信号 ACLK/ARESETN。
        if pn in ("s_axi_aclk", "ap_clk"):
            sig = None; ifname = None
        elif pn in ("s_axi_aresetn", "ap_rst_n"):
            sig = None; ifname = None
        elif pn.startswith(SA + "_"):
            sig = pn[len(SA) + 1:].upper()
            ifname, is_m = SA, False
        elif pn.startswith("m_axi_"):
            # 读/写通道已统一成 m_axi_ 前缀（ar*/r* + aw*/w*/b*），
            # 前缀推导会自动给出单个 m_axi 接口。
            sig = pn[len("m_axi_"):].upper()
            ifname, is_m = "m_axi", True
        elif pn.startswith(RD + "_"):
            # 读通道与写通道**合并成同一个 AXI 接口**（m_axi）：
            # rd 贡献 ar*/r*，wr 贡献 aw*/w*/b*，信号名无冲突。
            # 这样 BD 看到的是 1 个完整 AXI 口，可以直接连 PS 的 HP 口，
            # 不需要互联——互联会把两个 6-bit ARID 拼成 7 bit，超过 HP 的限制，
            # 并且会把 ARID 重映射掉（conv_uni 的线缓存依赖 ARID=条目号）。
            sig = pn[len(RD) + 1:].upper()
            ifname, is_m = "m_axi", True
        elif pn.startswith(wr + "_"):
            sig = pn[len(wr) + 1:].upper()
            ifname, is_m = "m_axi", True
        else:
            sig = None
        w = (" " + width) if width else "      "
        body = "    %s%s %s" % (d, w, pn)
        pre = []
        if sig is not None:
            pre.append(axi_attr(ifname, sig))
            # 每个 AXI 接口只在该接口的第一个信号上加 PARAMETER
            if ifname not in _param_done:
                _param_done.add(ifname)
                pre.insert(0, axi_param(ifname))
        elif pn in ("s_axi_aclk", "ap_clk"):
            pre.append(sig_param("s_axi_aclk", "ASSOCIATED_BUSIF s_axi:m_axi, FREQ_HZ 40000000"))
            pre.append(sig_attr("clock", "s_axi_aclk"))
        elif pn in ("s_axi_aresetn", "ap_rst_n"):
            pre.append(sig_param("s_axi_aresetn", "POLARITY ACTIVE_LOW"))
            pre.append(sig_attr("reset", "s_axi_aresetn"))
        # 标注必须和端口声明组成**同一个元素**，否则 join 出来的逗号会落在标注后面
        decls.append(("\n".join(pre) + "\n" + body) if pre else body)
    out.append(",\n".join(decls))
    out.append(");")
    # 例化
    out.append("    %s u_%s (" % (mod, mod.replace("_axi_wrap", "")))
    inst = []
    for (pn, dirn, width, _raw) in ports:
        inst.append("        .%s(%s)" % (pn, pn))
    out.append(",\n".join(inst))
    out.append("    );")
    out.append("endmodule")
    open(os.path.join(MODDIR, mod + "_bd.v"), "w").write("\n".join(out) + "\n")
    print("生成 %s_bd.v（%d 个端口）" % (mod, len(ports)))


if __name__ == "__main__":
    main()
