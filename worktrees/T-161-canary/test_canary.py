#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T-161 金丝雀修复的主机单测（**纯 numpy**：不需要板子、不需要 PL、不需要 Xilinx）。

被测对象：`pynq_specialized_code/ps/pynq/bin_engine.py` 的 `BinGuard.fill_canary`
/ `check_canary` / `canary_spans`（默认测同目录副本；可用 CANARY_ENGINE 指到别的
实现，变异测试就是这么跑的）。

真实几何（来自 A 段已定论的事实，不在这里重新论证）：

    max_out     = 96ch × 128² × 8bit(C8) = 1 572 864 B   ← 只有 3 层达到
    每个 OUT 槽 = max_out + 4096 = 1 576 960 B
    旧行为      = 金丝雀钉在 [len-4096, len)，与数据之间最多空 512 KB
    修复        = 主窗紧贴 [out_bytes, out_bytes+4096)；另保留槽尾窗抓"槽尾之后"的写者

运行：  python3 test_canary.py        # 全绿退出码 0，否则 1
"""
import contextlib
import importlib.util
import io
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENGINE = os.path.join(HERE, "pynq_specialized_code", "ps", "pynq",
                              "bin_engine.py")

MAX_OUT = 1_572_864          # 96ch × 128² × 8bit = 1 572 864 B
CANARY_BYTES = 4096
SLOT = MAX_OUT + CANARY_BYTES        # 1 576 960 B

_results = []


def record(name, ok, detail=""):
    _results.append((name, bool(ok)))
    print("[%s] %-10s %s   %s" % (name, "PASS" if ok else "FAIL",
                                  "." * max(1, 34 - len(name)), detail),
          flush=True)


def load_engine(path):
    path = os.path.abspath(path)
    d = os.path.dirname(path)
    if d not in sys.path:
        sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location("bin_engine_T161", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # bin_ref_numpy 从同目录导入
    return mod


def make_slot(n=SLOT):
    """0x00 的槽：证明金丝雀只出现在 fill 真正写过的窗里。"""
    return np.zeros(int(n), np.uint8)


def write_data(slot, out_bytes, val=0x3C):
    """模拟 PL 把本层数据写满 [0, out_bytes)。"""
    slot[:out_bytes] = val
    return slot


def first_offset(msg):
    """从异常消息里取**第一个探测窗**的首偏移（我们的消息先主窗、后槽尾窗）。"""
    m = re.search(r"首偏移=(\d+)", msg)
    return int(m.group(1)) if m else None


def all_offsets(msg):
    return [int(m) for m in re.findall(r"首偏移=(\d+)", msg)]


def expect_runtime_error(fn):
    try:
        fn()
    except RuntimeError as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------- U1
def u1_no_false_alarm(BE):
    slot = make_slot()
    out_bytes = 24576                       # 次大一档 1 048 576 之下的普通层
    before = slot.copy()
    BE.BinGuard.fill_canary(slot, out_bytes, tag="u1.layer")
    touched = np.flatnonzero(slot != before)
    data_untouched = bool(touched.size) and not bool(np.any(touched < out_bytes))
    write_data(slot, out_bytes, 0x3C)       # [0,out_bytes) 全是 0x3C，绝不该被查
    try:
        BE.BinGuard.check_canary(slot, out_bytes, "u1.layer")
        ok, det = True, ("out_bytes=%d len=%d 无报警；fill 只碰 %d 个金丝雀字节，"
                         "数据区 [0,%d) 一个字节没动"
                         % (out_bytes, len(slot), touched.size, out_bytes))
    except Exception as exc:                # noqa: BLE001
        ok, det = False, "误报：%s" % exc
    record("U1", ok and data_untouched, det if data_untouched
           else "fill 越界碰到数据区 [0,%d)" % out_bytes)


# ---------------------------------------------------------------- U2
def u2_catch_8_bytes(BE):
    slot = make_slot()
    out_bytes = 24576
    BE.BinGuard.fill_canary(slot, out_bytes, tag="u2.layer")
    write_data(slot, out_bytes, 0x3C)
    slot[out_bytes:out_bytes + 8] = 0x00    # 贴着数据边界外写 8 B
    msg = expect_runtime_error(
        lambda: BE.BinGuard.check_canary(slot, out_bytes, "u2.layer"))
    off = first_offset(msg) if msg else None
    record("U2", msg is not None and off == 0,
           ("报警，首偏移=%s（期望 0）" % off) if msg
           else "没报警 ⇒ 贴着 out_bytes 的越界写被漏掉")


# ---------------------------------------------------------------- U3
def u3_slot_tail_writer(BE):
    slot = make_slot()
    out_bytes = 24576
    BE.BinGuard.fill_canary(slot, out_bytes, tag="u3.layer")
    write_data(slot, out_bytes, 0x3C)
    slot[-8:] = 0x00                        # 越过整个槽的写者（desc/scratch/别的槽）
    msg = expect_runtime_error(
        lambda: BE.BinGuard.check_canary(slot, out_bytes, "u3.layer"))
    offs = all_offsets(msg) if msg else []
    record("U3", 4088 in offs,
           ("报警，偏移=%s（期望含 4088）" % offs) if msg
           else "没报警 ⇒ 槽尾之后的写者被漏掉")


# ---------------------------------------------------------------- U4
def u4_fallback(BE):
    out_bytes = 8192
    n = out_bytes + CANARY_BYTES - 100      # 放不下完整金丝雀窗
    slot = make_slot(n)
    slot[:out_bytes] = 0x3C                 # 数据（尾部 100 B 与槽尾窗重叠）
    before = slot.copy()
    buf = io.StringIO()
    ok, det = True, ""
    try:
        with contextlib.redirect_stdout(buf):
            BE.BinGuard.fill_canary(slot, out_bytes, tag="u4.layer")
        BE.BinGuard.check_canary(slot, out_bytes, "u4.layer")
    except Exception as exc:                # noqa: BLE001
        ok, det = False, "崩了：%s" % exc
    log = buf.getvalue()
    diff = np.flatnonzero(slot != before)
    only_tail = (diff.size == CANARY_BYTES and diff[0] >= n - CANARY_BYTES
                 and diff[-1] < n)
    got = ("out_bytes=%d+4096>len=%d 不崩；日志%s'回落'；只改槽尾%s"
           % (out_bytes, n, "含" if "回落" in log else "**缺**",
              " [%d,%d)" % (n - CANARY_BYTES, n) if only_tail else " ✗"))
    record("U4", ok and ("回落" in log) and only_tail and ok, got)


# ---------------------------------------------------------------- main
def main():
    path = os.environ.get("CANARY_ENGINE", DEFAULT_ENGINE)
    print("T-161 canary unit test")
    print("  被测实现: %s" % os.path.abspath(path))
    print("  槽几何:   max_out=%d B, len(slot)=max_out+4096=%d B" % (MAX_OUT, SLOT))
    print("  numpy:    %s" % np.__version__)
    BE = load_engine(path)
    for fn in (u1_no_false_alarm, u2_catch_8_bytes, u3_slot_tail_writer,
               u4_fallback):
        try:
            fn(BE)
        except Exception as exc:            # noqa: BLE001  用例自身炸了也算 FAIL
            record(fn.__name__.split("_")[0].upper(), False,
                   "用例自身异常：%r" % (exc,))
    n_pass = sum(1 for _n, ok in _results if ok)
    print("\n结果: %d/%d PASS" % (n_pass, len(_results)))
    return 0 if n_pass == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
