#!/usr/bin/env python3
"""汇总设计空间探索的结果。

扫描 proj/dse/*/solution1/syn/report/conv2d_accel_csynth.rpt，
提取资源占比与估算周期，按预算筛掉超标的，再按估算周期排序，
输出 notes/dse_results.csv 和 notes/dse_summary.md。

用法：python dse_summary.py
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
FPGA = os.path.dirname(os.path.dirname(HERE))         # fpga/
DSE = os.path.join(os.path.dirname(HERE), "proj", "dse")
NOTES = os.path.join(FPGA, "docs")

# 预算，取自设计文档 Global Constraints
BUDGET = {"dsp_pct": 62, "bram_pct": 50, "lut_pct": 50, "ff_pct": 40}

REPORT = "solution1/syn/report/conv2d_accel_csynth.rpt"


def parse_report(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()

    m = re.search(r"\|ap_clk\s*\|\s*([\d.]+) ns\|\s*([\d.]+) ns\|\s*([\d.]+) ns\|", text)
    target, est, unc = (float(m.group(1)), float(m.group(2)), float(m.group(3))) if m else (0.0, 0.0, 0.0)

    m = re.search(
        r"\|Utilization \(%\)\s*\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|", text)
    bram_pct, dsp_pct, ff_pct, lut_pct = (
        (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))) if m else (0, 0, 0, 0))

    m = re.search(r"\|Total\s*\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|\s*(\d+)\|", text)
    bram, dsp, ff, lut = (
        (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))) if m else (0, 0, 0, 0))

    return {
        "target_ns": target, "est_ns": est, "unc_ns": unc,
        "fmax_mhz": (1000.0 / est) if est > 0 else 0.0,
        "bram": bram, "dsp": dsp, "ff": ff, "lut": lut,
        "bram_pct": bram_pct, "dsp_pct": dsp_pct, "ff_pct": ff_pct, "lut_pct": lut_pct,
    }


def main():
    rows = []
    if not os.path.isdir(DSE):
        print("找不到 %s，先跑 dse_scan.tcl" % DSE)
        return 1

    for name in sorted(os.listdir(DSE)):
        rpt = os.path.join(DSE, name, REPORT)
        if not os.path.isfile(rpt):
            continue
        r = parse_report(rpt)
        r["config"] = name
        r["over_budget"] = (
            r["dsp_pct"] > BUDGET["dsp_pct"] or r["bram_pct"] > BUDGET["bram_pct"]
            or r["lut_pct"] > BUDGET["lut_pct"] or r["ff_pct"] > BUDGET["ff_pct"])
        rows.append(r)

    if not rows:
        print("没有解析到任何报告")
        return 1

    os.makedirs(NOTES, exist_ok=True)

    cols = ["config", "dsp", "dsp_pct", "bram", "bram_pct", "lut", "lut_pct",
            "ff", "ff_pct", "est_ns", "fmax_mhz", "over_budget"]
    with open(os.path.join(NOTES, "dse_results.csv"), "w", encoding="utf-8") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join(str(r[c]) for c in cols) + "\n")

    ok = [r for r in rows if not r["over_budget"]]
    ok.sort(key=lambda r: r["est_ns"])

    with open(os.path.join(NOTES, "dse_summary.md"), "w", encoding="utf-8") as f:
        f.write("# 设计空间探索结果\n\n")
        f.write("扫描 %d 组配置，其中 %d 组在预算内。\n\n" % (len(rows), len(ok)))
        f.write("预算：DSP ≤ %d%%，BRAM ≤ %d%%，LUT ≤ %d%%，FF ≤ %d%%。\n\n"
                % (BUDGET["dsp_pct"], BUDGET["bram_pct"], BUDGET["lut_pct"], BUDGET["ff_pct"]))
        f.write("## 全部配置\n\n")
        f.write("| 配置 | DSP | BRAM | LUT | FF | 估算周期 ns | Fmax MHz | 超预算 |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        for r in rows:
            f.write("| %s | %d (%d%%) | %d (%d%%) | %d (%d%%) | %d (%d%%) | %.3f | %.1f | %s |\n"
                    % (r["config"], r["dsp"], r["dsp_pct"], r["bram"], r["bram_pct"],
                       r["lut"], r["lut_pct"], r["ff"], r["ff_pct"],
                       r["est_ns"], r["fmax_mhz"], "是" if r["over_budget"] else "否"))
        f.write("\n## 推荐的三个候选\n\n")
        if not ok:
            f.write("没有配置满足预算。\n")
        else:
            for r in ok[:3]:
                f.write("- `%s`：估算周期 %.3f ns（Fmax %.1f MHz），"
                        "DSP %d%%，BRAM %d%%，LUT %d%%，FF %d%%\n"
                        % (r["config"], r["est_ns"], r["fmax_mhz"],
                           r["dsp_pct"], r["bram_pct"], r["lut_pct"], r["ff_pct"]))
        f.write("\n排序依据：先按预算筛除，再按估算周期从短到长。\n")

    print("共 %d 组，预算内 %d 组" % (len(rows), len(ok)))
    for r in ok[:3]:
        print("  %-10s est=%6.3f ns  fmax=%6.1f MHz  dsp=%d%% bram=%d%% lut=%d%% ff=%d%%"
              % (r["config"], r["est_ns"], r["fmax_mhz"],
                 r["dsp_pct"], r["bram_pct"], r["lut_pct"], r["ff_pct"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
