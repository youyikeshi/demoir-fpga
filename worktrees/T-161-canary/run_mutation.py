#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-161 变异测试：把"起点改为 out_bytes"改回旧行为，确认 U2/U3 会变红。

M1（任务书要求的那一处变异）：`canary_spans` 的起点退回 `len(slot)-4096`，
   即回到修复前的**单槽尾窗**。预期：U2（贴着 out_bytes 的 8 B 越界）变红；
   U3（槽尾之后写者）仍是绿的 —— 这恰好是旧行为**唯一**能抓到的写者，说明
   修复的增量价值全在 U2 这一类，也说明为什么不能把槽尾窗删掉。
M2（补充变异，证明 U3 的敏感度）：删掉槽尾窗、只留紧贴窗。预期：U3 变红。
   两个变异体合起来 ⇒ U2、U3 各自都只由对应代码路径决定，没有一条断言是摆设。

变异体按"真实代码改写"生成（不是打 flag），放在 mutants/ 下，`bin_ref_numpy.py`
用符号链接指回生产目录（只读引用）。
"""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "pynq_specialized_code", "ps", "pynq")
ENGINE = os.path.join(PKG, "bin_engine.py")
MUT = os.path.join(HERE, "mutants")
LOGS = os.path.join(HERE, "logs")

BLOCK = """        spans, tail = [], n - CANARY_BYTES
        if n_used + CANARY_BYTES <= n:
            spans.append((n_used, "紧贴out_bytes", False))
        else:
            spans.append((tail, "槽尾", True))          # 回落
        if tail > n_used + CANARY_BYTES:                # 槽尾窗与主窗不重叠才查
            spans.append((tail, "槽尾", False))
        return spans
"""

M1 = """        tail = n - CANARY_BYTES
        return [(tail, "槽尾", False)]      # 变异体 M1：起点退回修复前的 len-4096
"""

M2 = """        spans, tail = [], n - CANARY_BYTES
        if n_used + CANARY_BYTES <= n:
            spans.append((n_used, "紧贴out_bytes", False))
        else:
            spans.append((tail, "槽尾", True))          # 回落
        return spans                         # 变异体 M2：删掉槽尾窗
"""


def make(kind, body):
    os.makedirs(MUT, exist_ok=True)
    src = open(ENGINE, encoding="utf-8").read()
    if src.count(BLOCK) != 1:
        sys.exit("无法定位 canary_spans 的窗构造代码块（找到 %d 处）"
                 % src.count(BLOCK))
    out = os.path.join(MUT, "bin_engine_%s.py" % kind)
    open(out, "w", encoding="utf-8").write(src.replace(BLOCK, body))
    ref = os.path.join(MUT, "bin_ref_numpy.py")
    if not os.path.lexists(ref):
        os.symlink(os.path.join(PKG, "bin_ref_numpy.py"), ref)
    return out


def run(engine, tag):
    env = dict(os.environ, CANARY_ENGINE=engine)
    p = subprocess.run([sys.executable, os.path.join(HERE, "test_canary.py")],
                       cwd=HERE, env=env, text=True, capture_output=True)
    out = p.stdout + p.stderr
    os.makedirs(LOGS, exist_ok=True)
    open(os.path.join(LOGS, tag + ".log"), "w", encoding="utf-8").write(out)
    return p.returncode, out


def status(out):
    res = {}
    for line in out.splitlines():
        m = re.match(r"\[(U\d)\]\s+(PASS|FAIL)", line)
        if m:
            res[m.group(1)] = (m.group(2) == "PASS")
    return res


def main():
    print("T-161 变异测试（真实改代码，不打 flag）")
    rc0, out0 = run(ENGINE, "mut_baseline")
    s0 = status(out0)
    print("  基线（修复后）: %s  rc=%d" % (s0, rc0))
    ok_base = rc0 == 0 and all(s0.get(k) for k in ("U1", "U2", "U3", "U4"))
    print("  [%s] 基线应全绿" % ("PASS" if ok_base else "FAIL"))

    m1 = make("m1", M1)
    rc1, out1 = run(m1, "mut_m1")
    s1 = status(out1)
    print("  M1（起点回退 len-4096）: %s  rc=%d" % (s1, rc1))
    ok_m1 = (rc1 != 0 and s1.get("U2") is False and s1.get("U3") is True)
    print("  [%s] M1 期望：U2 红、U3 绿（旧行为只能抓槽尾写者）"
          % ("PASS" if ok_m1 else "FAIL"))

    m2 = make("m2", M2)
    rc2, out2 = run(m2, "mut_m2")
    s2 = status(out2)
    print("  M2（删掉槽尾窗）: %s  rc=%d" % (s2, rc2))
    ok_m2 = (rc2 != 0 and s2.get("U3") is False and s2.get("U2") is True)
    print("  [%s] M2 期望：U3 红、U2 绿（紧贴窗负责数据边界写者）"
          % ("PASS" if ok_m2 else "FAIL"))

    ok = ok_base and ok_m1 and ok_m2
    print("\n变异测试结论: %s（日志在 logs/）" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
