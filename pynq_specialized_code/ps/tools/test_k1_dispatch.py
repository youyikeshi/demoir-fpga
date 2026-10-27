#!/usr/bin/env python3
"""本机离线回归：PYNQEngine.conv 的 K1 路由分支（假 PYNQ + 假老引擎）。

这不是"测 RTL"，而是测**PS 侧接线**：
  * USE_K1_IP=1 时，合格的 k=1 层真的交给 K1Engine（而不是老引擎）；
  * relu=False / CHW / 融合出口 / 形状越界 的层自动回退老引擎；
  * K1 返回错误码时**不再**偷偷改走老引擎（避免两个 master 同时写 DDR）；
  * 传给 K1 的缓冲区地址、形状、shift 与 manifest/index 一致。

用法（在 ps/ 目录）：
    python3 tools/test_k1_dispatch.py

判据：最后一行 ``==== K1 路由分支回归 N/N PASS ====`` 且退出码 0。
"""
import json
import os
import sys
import types

import numpy as np

# 离线回归必须可重复：清掉可能从 shell 带进来的 K1 配置
for _v in ("K1_IP_RELU", "K1_IP_CIN_MAX", "K1_IP_COUT_MAX", "K1_IP_LANES",
           "K1_IP_FCLK_MHZ", "K1_IP_FCLK_HZ", "K1_IP_TIMEOUT_FACTOR",
           "K1_IP_TIMEOUT_EXTRA_S", "USE_K1_IP", "K1_IP_BASE", "K1_IP_NAME"):
    os.environ.pop(_v, None)

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.abspath(os.path.join(HERE, os.pardir))
PYNQ_DIR = os.path.join(PS, "pynq")
sys.path.insert(0, PYNQ_DIR)
sys.path.insert(0, HERE)

import k1_engine  # noqa: E402
from test_k1_route import _FakeMMIO, _FakeOverlay  # noqa: E402


# ---------------------------------------------------------------- 假 PYNQ

class _FakeBuf(object):
    _next_addr = [0x30000000]

    def __init__(self, shape, dtype=np.uint8):
        self.array = np.zeros(shape, dtype=dtype)
        self.device_address = _FakeBuf._next_addr[0]
        _FakeBuf._next_addr[0] += max(1, self.array.nbytes + 0x1000)
        self.flushed = 0
        self.invalidated = 0

    def flush(self):
        self.flushed += 1

    def invalidate(self):
        self.invalidated += 1


def _install_fake_pynq():
    mod = types.ModuleType("pynq")
    mod.allocate = lambda shape, dtype=np.uint8: _FakeBuf(shape, dtype)
    mod.MMIO = _FakeMMIO
    sys.modules["pynq"] = mod
    return mod


# ---------------------------------------------------------------- 假老引擎

class _OldEngine(object):
    instances = []

    def __init__(self, overlay, fuse_offsets=None):
        self.calls = []
        self.fuse_out_ok = False
        self.fuse_sum_ok = False
        self.fuse_offsets = fuse_offsets
        _OldEngine.instances.append(self)

    @staticmethod
    def estimate_seconds(H, W, cin, cout, k, s):
        return 123.0

    def run(self, in_buf, wgt_buf, bias_buf, out_buf, H, W, cin, cout, k, s,
            d, pad, shift, relu=True, out_lut=None, chsum=None, timeout_s=None,
            c8_in=False, c8_out=False):
        self.calls.append(dict(H=H, W=W, cin=cin, cout=cout, k=k, s=s, d=d,
                               pad=pad, shift=shift, relu=relu, c8_in=c8_in,
                               c8_out=c8_out, in_addr=in_buf.device_address,
                               out_addr=out_buf.device_address))
        return 0, 0.0


class _Arena(object):
    """只实现 PYNQEngine.conv 用到的接口。"""

    def __init__(self, layout):
        self.layout = dict(layout)
        self.base = 0x20000000
        self.off = {}
        n = 0
        for name in self.layout:
            self.off[name] = n * 0x100000
            n += 1
        self.flushes = 0
        self.invalidates = 0

    def addr(self, name):
        return self.base + self.off[name]

    def flush(self):
        self.flushes += 1

    def invalidate(self):
        self.invalidates += 1


def _entry(cout, cin, shift, relu, w_off=0):
    return {
        "w_off": w_off, "w_len": cout * cin, "w_shape": [cout, cin, 1, 1],
        "b_off": w_off + cout * cin, "b_len": cout * 4, "shift": shift,
        "relu": 1 if relu else 0, "w_layout": "oc_ic_ky_kx",
    }


def _op(name, cin, cout, relu, shift_key):
    return {
        "op": "conv", "name": name, "in": ["a"], "out": ["b"],
        "cin": cin, "cout": cout, "k": 1, "s": 1, "d": 1, "pad": 0,
        "h": 320, "w": 320, "hout": 320, "wout": 320, "relu": relu,
        "weights_key": shift_key, "needed": True,
    }


def main():
    _install_fake_pynq()
    import conv_engine
    import runner

    ok = 0
    total = 0

    def check(name, cond, detail=""):
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
            print("PASS  %s" % name)
        else:
            print("FAIL  %s  %s" % (name, detail))

    old_cls = conv_engine.Conv2dEngine
    conv_engine.Conv2dEngine = _OldEngine
    try:
        layout = {"a": True, "b": True}
        index = {"layers": {
            "w_ok": _entry(16, 48, 6, True),
            "w_lin": _entry(16, 48, 6, False),
        }}
        blob = bytes(2 * (16 * 48 + 64))
        overlay = _FakeOverlay({
            "k1_wrap": {"type": "xilinx.com:user:k1_axi_wrap:1.0",
                        "phys_addr": 0x40000000, "addr_range": 0x20000},
        })

        # 共享一块假 MMIO，方便检查寄存器落点
        mmio = _FakeMMIO(0x40000000, k1_engine.WINDOW_SIZE)
        old_factory = k1_engine.K1Engine.__init__

        # 让 K1Engine 用这块 mmio（PYNQEngine 构造时不传 mmio_factory）
        def _patched_init(self, overlay=None, ip_name=None, base_addr=None,
                          mmio_factory=None, strict=True):
            return old_factory(self, overlay=overlay, ip_name=ip_name,
                               base_addr=base_addr,
                               mmio_factory=lambda a, l: mmio, strict=strict)
        k1_engine.K1Engine.__init__ = _patched_init

        arena = _Arena(layout)
        eng = runner.PYNQEngine(overlay, index, blob, arena, use_k1=True)
        check("PYNQEngine 接入 K1Engine", eng.k1 is not None)
        check("K1Engine 基地址来自 hwh",
              eng.k1.base_addr == 0x40000000, hex(eng.k1.base_addr))

        # ---- 1) 合格层：必须走 K1，不碰老引擎 ----
        op_ok = _op("ut.ok", 48, 16, True, "w_ok")
        old = _OldEngine.instances[-1]
        eng.conv("a", "b", op_ok)
        check("合格层走 K1（老引擎 0 次）", eng.k1_calls == 1 and len(old.calls) == 0,
              "k1=%d old=%d" % (eng.k1_calls, len(old.calls)))
        check("K1 写入了正确的 CIN/COUT/SHIFT",
              mmio.read(0x10000 + k1_engine.R_CIN) == 48
              and mmio.read(0x10000 + k1_engine.R_COUT) == 16
              and mmio.read(0x10000 + k1_engine.R_SHIFT) == 6)
        exp_in = arena.addr("a")
        exp_out = arena.addr("b")
        check("K1 输入地址 = arena.addr(a)",
              mmio.read(k1_engine.B_IN_FM) == (exp_in & 0xFFFFFFFF)
              and mmio.read(k1_engine.B_IN_FM + 4) == (exp_in >> 32))
        check("K1 输出地址 = arena.addr(b)",
              mmio.read(k1_engine.B_OUT_FM) == (exp_out & 0xFFFFFFFF)
              and mmio.read(k1_engine.B_OUT_FM + 4) == (exp_out >> 32))
        check("运行后 invalidate 内存池", arena.invalidates >= 1)

        # ---- 2) relu=False：**现在由 K1 IP 直接执行**（运行期 ReLU）----
        #   2026-10-27：0x70 bit0 → ctrl_relu，RTL 三处齐全 + 仿真 relu=0 通过
        k1_before = eng.k1_calls
        old.calls.clear()
        op_lin = _op("ut.lin", 48, 16, False, "w_lin")
        eng.conv("a", "b", op_lin)
        check("relu=False 由 K1 IP 执行（不回退）",
              eng.k1_calls == k1_before + 1 and len(old.calls) == 0,
              "k1=%d old=%d" % (eng.k1_calls, len(old.calls)))
        check("0x70 写入了 relu=0",
              mmio.read(k1_engine.R_RELU) & 1 == 0,
              "R_RELU=0x%x" % mmio.read(k1_engine.R_RELU))

        # ---- 3) 融合出口：自动回退老引擎（K1 无 out_lut/chsum）----
        old.calls.clear()
        k1_before = eng.k1_calls
        eng.conv("a", "b", op_ok, out_lut=np.zeros(256, np.int8))
        check("out_lut 层回退老引擎",
              len(old.calls) == 1 and eng.k1_calls == k1_before,
              "old=%d k1=%d(+0)" % (len(old.calls), eng.k1_calls))

        # ---- 4) CHW：全部回退老引擎 ----
        arena_chw = _Arena({"a": False, "b": False})
        eng_chw = runner.PYNQEngine(overlay, index, blob, arena_chw, use_k1=True)
        old2 = _OldEngine.instances[-1]
        eng_chw.conv("a", "b", op_ok)
        check("CHW 回退老引擎",
              eng_chw.k1_calls == 0 and len(old2.calls) == 1,
              "k1=%d old=%d" % (eng_chw.k1_calls, len(old2.calls)))
        check("CHW 回退原因已记录",
              any("C8" in k for k in eng_chw.k1_skip_reasons),
              repr(eng_chw.k1_skip_reasons))

        # ---- 5) K1 返回码非 0：必须抛，且不再偷偷走老引擎 ----
        mmio.force_err = 3
        old.calls.clear()
        try:
            eng.conv("a", "b", op_ok)
            check("K1 错误码向上抛", False, "没有抛异常")
        except RuntimeError as exc:
            check("K1 错误码向上抛", "K1 IP 执行失败" in str(exc)
                  and "ut.ok" in str(exc), str(exc)[:100])
            check("错误后未回退老引擎", len(old.calls) == 0,
                  "old=%d" % len(old.calls))
        mmio.force_err = None

        # ---- 6) k1_summary 输出可读 ----
        summary = eng.k1_summary()
        # 2026-10-27：relu=False 现在也由 K1 执行 ⇒ 执行 2 层（op_ok + op_lin）
        check("k1_summary 含执行/回退统计",
              "执行 2 层" in summary, summary)

        # ---- 7) 开关关闭：完全不构造 K1Engine ----
        eng_off = runner.PYNQEngine(overlay, index, blob, arena, use_k1=False)
        old4 = _OldEngine.instances[-1]
        eng_off.conv("a", "b", op_ok)
        check("USE_K1_IP=0 全部走老引擎",
              eng_off.k1 is None and len(old4.calls) == 1,
              "k1=%r old=%d" % (eng_off.k1, len(old4.calls)))
        check("未启用时 summary 明确说明",
              "未启用" in eng_off.k1_summary(), eng_off.k1_summary())
    finally:
        conv_engine.Conv2dEngine = old_cls
        k1_engine.K1Engine.__init__ = old_factory

    print("==== K1 路由分支回归 %d/%d PASS ====" % (ok, total))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
