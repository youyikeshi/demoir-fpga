#!/usr/bin/env python3
"""检查 `src/` 与 `src_cosim/` 是否只差 cosim 专用的 `depth=` 与那段注释。

为什么要有这个检查：`hls_run_engine.tcl` 用 `src/`，`hls_cosim_engine.tcl` 用
`src_cosim/`。改了电路只改一边，**综合是新的、cosim 是旧的**，两边结论就对不上。
2026-10-17 真踩过：给 src 加了 1×1 快路径，cosim 跑的仍是旧副本，
周期数一模一样（4,694,784），差点误判成"优化无效"。

用法：python scripts/check_src_sync.py      # 不一致时 exit 1
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PL = os.path.abspath(os.path.join(HERE, ".."))
PAIRS = ["conv2d_engine.cpp", "conv2d_engine.h", "conv_ref_rt.cpp",
         "conv2d_accel.h"]


def normalize(text):
    """去掉只在 cosim 副本里出现的东西，使两份可以直接比。"""
    # m_axi 的 depth=NNN
    text = re.sub(r"\s*depth\s*=\s*\d+", "", text)
    # cosim-only 的注释块（从"注意：本副本仅供"到紧跟的第一条 pragma）
    text = re.sub(r"\n\s*//\s*注意：本副本仅供[\s\S]*?(?=\n\s*HLS_PRAGMA)", "", text)
    # 1×1 快路径里那句"与 src 保持一致"的注释
    text = re.sub(r"\n\s*//\s*与 src/conv2d_engine\.cpp 保持一致.*", "", text)
    # 行尾空白与空行统一
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{2,}", "\n\n", text).strip()


def main():
    bad = 0
    for name in PAIRS:
        a = os.path.join(PL, "src", name)
        b = os.path.join(PL, "src_cosim", name)
        if not (os.path.exists(a) and os.path.exists(b)):
            print("[跳过] %s 有一边不存在" % name)
            continue
        with open(a, encoding="utf-8") as f:
            ta = normalize(f.read())
        with open(b, encoding="utf-8") as f:
            tb = normalize(f.read())
        if ta == tb:
            print("[OK]   %-20s 只差 depth=（逻辑一致）" % name)
        else:
            bad += 1
            print("[FAIL] %-20s 逻辑不一致！改电路时两边都要改" % name)
            # 给出第一处不同，便于定位
            la, lb = ta.splitlines(), tb.splitlines()
            for i, (x, y) in enumerate(zip(la, lb)):
                if x != y:
                    print("       首个差异在规范化后第 %d 行：" % (i + 1))
                    print("         src      : %s" % x.strip()[:100])
                    print("         src_cosim: %s" % y.strip()[:100])
                    break
    print("==== src ↔ src_cosim %s ====" % ("一致" if bad == 0 else "不一致"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
