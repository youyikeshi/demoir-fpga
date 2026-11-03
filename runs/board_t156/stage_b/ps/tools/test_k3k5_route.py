#!/usr/bin/env python3
"""本机离线回归：k=3 / k=5 手写 IP 的**驱动 + 路由接线**（不需要板子、不需要 pynq）。

这不是"测 RTL"，而是测 PS 侧最容易错、也最难上板排查的那几件事：

  * ``K3Engine`` 的寄存器落点（含 k3 新增的 ``WIN_LO/WIN_HI``）；
  * 地址窗口的**记账口径**（bytes_in/wgt/bias/out）—— 写错就是上板 0xA；
  * ``sticky_oob`` 必须抛异常并打印 0xA8/0xAC/0xB0 诊断（听诊器）；
  * 错误码 1..9 / 0xA 的区分与人类可读原因；
  * ``STALL_FILL/STALL_MAC`` 进 ``run()`` 的返回值（可解包成 (rc, 秒)）；
  * 预检不过时**一个寄存器都不写**（不碰硬件）；
  * ``PYNQEngine.conv``：k=3 走 K3、k=5 走 K5；**路由失败必须抛错、不回退 HLS**；
  * 新 bit 没有 HLS 引擎时 PYNQEngine 仍能构造（否则整网一段都跑不了）；
  * 融合规划把 k=3/k=5 层排除（它们没有 out_lut/chsum 出口）；
  * 用真实 manifest + 权重包统计"能不能路由"（与 tools/ip_route_coverage.py 同源）。

用法（在 ``pynq_specialized_code/ps`` 目录）：
    python3 tools/test_k3k5_route.py

判据：最后一行 ``==== K3/K5 离线回归 N/N PASS ====`` 且退出码 0。
"""
import json
import os
import sys
import types

import numpy as np

# 离线回归必须可重复：清掉可能从 shell 带进来的 IP 配置
for _v in ("K3_IP_BASE", "K3_IP_NAME", "K3_IP_RELU", "K3_IP_LANES",
           "K3_IP_CIN_MAX", "K3_IP_COUT_MAX", "K3_IP_HW_MAX", "K3_IP_CAP_B",
           "K3_IP_FCLK_MHZ", "K3_IP_FCLK_HZ", "K3_IP_STRICT_OOB",
           "K5_IP_BASE", "K5_IP_NAME", "K5_IP_CIN", "K5_IP_COUT", "K5_IP_H",
           "K5_IP_W", "K5_IP_OC_PER_PASS", "K5_IP_SET_WINDOW",
           "K5_IP_STRICT_OOB", "K5_IP_FCLK_MHZ", "K5_IP_FCLK_HZ",
           "USE_K1_IP", "USE_K3_IP", "USE_K5_IP", "C8_LAYOUT", "PP_FUSE_CONV"):
    os.environ.pop(_v, None)

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.abspath(os.path.join(HERE, os.pardir))
PYNQ_DIR = os.path.join(PS, "pynq")
sys.path.insert(0, PYNQ_DIR)
sys.path.insert(0, HERE)

import c8_layout   # noqa: E402
import k3_engine   # noqa: E402
import k5_engine   # noqa: E402
import runner      # noqa: E402

MANIFEST = os.path.join(PS, "manifest_640.json")
PACK_CANDIDATES = [
    os.path.join(PS, os.pardir, "deploy", "weights_pack", "index.json"),
    os.path.join(PS, os.pardir, "board", "board_bundle", "weights_pack",
                 "index.json"),
]


# ---------------------------------------------------------------- 假 MMIO
class _FakeWrapMMIO(object):
    """模拟 k3_axi_wrap / 未来的 k5 wrapper（128KB 窗口，A[16] 分页）。

    行为按 RTL 逐条抄：
      * 启动前校验（k3_cfg_check 的 9 条）→ 错误码写 AP_RETURN + 自打 done；
      * 四区地址窗口检查 → 0xA；
      * 越界钳制 → ``sticky``（由 ``sticky=1`` 直接注入，模拟"之前已经钳过"）；
      * start 清零 STALL 计数，跑完停在 done。
    另外把 RTL 的 bytes 记账按同一公式算出来，供测试核对窗口。
    """

    def __init__(self, base, length, wgt_bpc=12, mode="k3"):
        if length < k3_engine.WINDOW_SIZE:
            raise AssertionError("窗口长度 %#x < 128KB" % length)
        self.base = base
        self.length = length
        self.wgt_bpc = int(wgt_bpc)
        self.mode = str(mode)          # "k3"=带 9 条 cfg_check；"k5"=引擎没有它
        self.reg = [0] * (k3_engine.WINDOW_SIZE // 4)
        self.start_writes = 0
        self.busy = 0
        self.done = 0
        self.err = 0
        self.err_code = 0
        self.ret = 0
        self.pending = 0
        self.sticky = 0
        self.stall_fill = 0
        self.stall_mac = 0
        self.force_rc = None          # 非 None：启动时直接按该错误码失败
        self.never_done = False
        self.force_window_fail = False
        self.addrs = None             # 启动时锁存的四个地址
        self.lens = None              # RTL 口径的四个字节数
        self.win = None               # (WIN_LO, WIN_HI)

    # ---- 寄存器下标 ----
    def _idx(self, offset):
        offset = int(offset)
        if offset < 0 or offset + 4 > self.length:
            raise AssertionError("MMIO 越界: %#x（窗口 %#x）" % (offset, self.length))
        return offset // 4

    def write(self, offset, data):
        data = int(data) & 0xFFFFFFFF
        idx = self._idx(offset)
        ctrl = (int(offset) & 0x10000) != 0
        off = int(offset) & 0xFF
        if ctrl and off == k3_engine.AP_CTRL:
            # 只认 bit0；写 1 触发一次启动（与 RTL 的 start 上升沿同义）
            if data & 1:
                self.start_writes += 1
                self._start()
            return
        self.reg[idx] = data

    def read(self, offset=0, length=4, word_order="little"):
        del length, word_order
        idx = self._idx(offset)
        ctrl = (int(offset) & 0x10000) != 0
        off = int(offset) & 0xFF
        if ctrl and off == k3_engine.R_STATUS:
            if self.pending > 0:
                self.pending -= 1
                if self.pending == 0:
                    self.busy = 0
                    self.done = 1
            return ((1 if self.done else 0) | (2 if self.busy else 0)
                    | (4 if self.err else 0) | ((self.err_code & 0xF) << 4)
                    | (0 if self.busy else (1 << 8))
                    | ((self.sticky & 1) << 9))
        if ctrl and off == k3_engine.AP_RETURN:
            return self.ret
        if ctrl and off == k3_engine.R_STALL_FILL:
            return self.stall_fill
        if ctrl and off == k3_engine.R_STALL_MAC:
            return self.stall_mac
        if ctrl and off == k3_engine.R_BYTES_IN:
            return int(self.lens[0]) if self.lens else 0
        if ctrl and off == k3_engine.R_WIN4:
            return 0xF if self.win else 0
        return self.reg[idx]

    # ---- 启动前校验（k3_cfg_check + 窗口）----
    def _rd_ctrl(self, off):
        return self.reg[(k3_engine.CTRL_PAGE_OFF + off) // 4]

    def _rd_offs(self, off):
        return self.reg[off // 4]

    def _start(self):
        if self.busy:
            return
        self.done = self.err = 0
        self.ret = self.err_code = 0
        if self.force_rc is not None:
            self.done = 1
            self.err = 1
            self.err_code = int(self.force_rc) & 0xF
            self.ret = int(self.force_rc)
            return
        h = self._rd_ctrl(k3_engine.R_H)
        w = self._rd_ctrl(k3_engine.R_W)
        cin = self._rd_ctrl(k3_engine.R_CIN)
        cout = self._rd_ctrl(k3_engine.R_COUT)
        ks = self._rd_ctrl(k3_engine.R_KSIZE)
        stride = self._rd_ctrl(k3_engine.R_STRIDE)
        dil = self._rd_ctrl(k3_engine.R_DIL)
        pad = self._rd_ctrl(k3_engine.R_PAD)
        shift = self._rd_ctrl(k3_engine.R_SHIFT)
        lo = [self._rd_offs(k3_engine.B_IN_FM), self._rd_offs(k3_engine.B_WGT),
              self._rd_offs(k3_engine.B_BIAS), self._rd_offs(k3_engine.B_OUT_FM)]
        win_lo = self._rd_ctrl(k3_engine.R_WIN_LO)
        win_hi = self._rd_ctrl(k3_engine.R_WIN_HI)
        self.win = (win_lo, win_hi)
        self.addrs = list(lo)
        code = 0
        ngrp_i = (cin + 7) // 8
        ngrp_o = (cout + 7) // 8
        hout = (h - 1) // stride + 1 if stride in (1, 2) else 0
        wout = (w - 1) // stride + 1 if stride in (1, 2) else 0
        need_cap = ngrp_i * w * (2 if dil != 1 else 1)
        if self.mode == "k3":
            if cin < 8 or cin > 192 or cin % 8:
                code = 1
            elif cout == 0 or cout > 192:
                code = 2
            elif stride not in (1, 2):
                code = 3
            elif dil not in (1, 2, 3):
                code = 4
            elif h == 0 or w == 0 or h > 320 or w > 320:
                code = 5
            elif hout < 1 or wout < 1:
                code = 6
            elif shift > 31:
                code = 7
            elif need_cap > 4608:
                code = 8
            elif stride != 1 and dil != 1:
                code = 9
        if code:
            self.done = 1
            self.err = 1
            self.err_code = code
            self.ret = code
            return
        # RTL 的 bytes（三级流水的最终值）+ 四区窗口
        self.lens = [ngrp_i * 8 * h * w * 8,
                     cout * cin * self.wgt_bpc,
                     cout * 4,
                     ngrp_o * 8 * hout * wout * 8]
        ok = all(a >= win_lo and a + n <= win_hi
                 for a, n in zip(lo, self.lens))
        if self.force_window_fail or not ok:
            self.done = 1
            self.err = 1
            self.err_code = 0xA
            self.ret = 0xA
            return
        self.busy = 1
        self.stall_fill = 12          # 造两个非零值，验证"进返回值"
        self.stall_mac = 0
        self.pending = (10 ** 9) if self.never_done else 3


class _FakeOverlay(object):
    def __init__(self, ip_dict):
        self.ip_dict = ip_dict


def _ns(addr):
    return types.SimpleNamespace(device_address=int(addr),
                                 physical_address=int(addr))


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

    def _alloc(shape, dtype=np.uint8):
        return _FakeBuf(shape, dtype)

    mod.allocate = _alloc
    # MMIO 按基址返回同一个行为 fake（只会有 k3 一个从机被构造）
    mod.MMIO = lambda base, length: _FakeWrapMMIO(base, length)
    sys.modules["pynq"] = mod
    return mod


class _OldEngine(object):
    """假老 HLS 引擎（只记录调用）。"""

    instances = []
    fail_with = None

    def __init__(self, overlay, fuse_offsets=None):
        if _OldEngine.fail_with is not None:
            raise RuntimeError(_OldEngine.fail_with)
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
                               c8_out=c8_out))
        return 0, 0.0


class _Arena(object):
    """只实现 PYNQEngine.conv / ip_plan 用到的接口。"""

    def __init__(self, layout):
        self.layout = dict(layout)
        self.base = 0x20000000
        self._off = {}
        n = 0
        for name in self.layout:
            self._off[name] = n * 0x100000
            n += 1
        self.flushes = 0
        self.invalidates = 0

    def addr(self, name):
        return self.base + self._off.get(name, 0)

    def flush(self):
        self.flushes += 1

    def invalidate(self):
        self.invalidates += 1


# ---------------------------------------------------------------- 数据
def _k3_entry(cout, cin, shift=8, w_layout="k3_pad12", w_shape=None):
    w_len = (12 if w_layout == "k3_pad12" else 0) * cout * cin
    if w_shape is None:
        w_shape = [cout, cin, 12] if w_layout == "k3_pad12" else [cout, cin, 3, 3]
    return {"w_off": 0, "w_len": w_len, "w_shape": w_shape,
            "b_off": w_len, "b_len": cout * 4, "shift": shift,
            "relu": 1, "w_layout": w_layout}


def _k5_entry(cout, cin, shift=9, w_layout="k5_pad28", w_shape=None):
    w_len = (28 if w_layout == "k5_pad28" else 0) * cout * cin
    if w_shape is None:
        w_shape = [cout, cin, 28] if w_layout == "k5_pad28" else [cout, cin, 5, 5]
    return {"w_off": 0, "w_len": w_len, "w_shape": w_shape,
            "b_off": w_len, "b_len": cout * 4, "shift": shift,
            "relu": 1, "w_layout": w_layout}


def _op(name, cin, cout, k, s=1, d=1, pad=None, relu=True, h=320, w=320,
        wkey="w"):
    if pad is None:
        pad = (k - 1) * d // 2
    return {"op": "conv", "name": name, "in": ["a"], "out": ["b"],
            "cin": cin, "cout": cout, "k": k, "s": s, "d": d, "pad": pad,
            "h": h, "w": w, "hout": (h - 1) // s + 1,
            "wout": (w - 1) // s + 1, "relu": relu,
            "weights_key": wkey, "needed": True}


# ---------------------------------------------------------------- 测试
def main():
    _install_fake_pynq()
    import conv_engine

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

    def expect_raise(name, exc_type, fn, contains=None, detail=""):
        nonlocal ok, total
        total += 1
        try:
            fn()
        except exc_type as exc:
            if contains is not None and contains not in str(exc):
                print("FAIL  %s  异常信息缺 %r：%s" % (name, contains, str(exc)[:200]))
            else:
                ok += 1
                print("PASS  %s" % name)
            return
        except Exception as exc:                           # noqa: BLE001
            print("FAIL  %s  抛了 %s：%s"
                  % (name, type(exc).__name__, str(exc)[:200]))
            return
        print("FAIL  %s  没有抛异常  %s" % (name, detail))

    # ================= 1) K3Engine：地址/窗口/返回值 =================
    ov = _FakeOverlay({
        "k3_wrap": {"type": "xilinx.com:user:k3_axi_wrap_bd:1.0",
                    "phys_addr": 0x40060000, "addr_range": 0x20000},
        "ps7": {},
    })
    found = k3_engine.K3Engine.discover(ov, "k3_wrap")
    check("discover 找到 k3_wrap @0x40060000",
          found == (0x40060000, "k3_wrap", 0x20000), repr(found))
    check("discover 找不到返回 None（不猜）",
          k3_engine.K3Engine.discover(_FakeOverlay({"ps7": {}}), "k3_wrap")
          is None)

    mm = _FakeWrapMMIO(0x40060000, k3_engine.WINDOW_SIZE)
    eng = k3_engine.K3Engine(base_addr=0x40060000,
                             mmio_factory=lambda a, l: mm)
    inb, wgb, bib, outb = (_ns(0x10000000), _ns(0x10800000),
                           _ns(0x10A00000), _ns(0x11000000))
    res = eng.run(inb, wgb, bib, outb, 320, 320, 16, 16, 3, 1, 1, 1, 8,
                  relu=True, c8_in=True, c8_out=True, layer="ut.k3")
    rc, dt = res                                   # 接口与 k1 一致：可解包
    check("k3 run 返回 (0, 秒)", rc == 0, "rc=%r" % (rc,))
    check("k3 run 返回 stall 字段", res.stall_fill == 12 and res.stall_mac == 0,
          repr(res))
    check("k3 start 只写一次", mm.start_writes == 1, mm.start_writes)
    # 窗口：min(四地址) .. max(地址+长度)
    bb = k3_engine.buffer_bytes(320, 320, 16, 16, hout=320, wout=320)
    exp_lo = min(0x10000000, 0x10800000, 0x10A00000, 0x11000000)
    exp_hi = max(0x10000000 + bb.in_bytes, 0x10800000 + bb.wgt_bytes,
                 0x10A00000 + bb.bias_bytes, 0x11000000 + bb.out_bytes)
    check("WIN_LO = min(四地址) / WIN_HI = max(地址+长度)",
          mm.win == (exp_lo, exp_hi), "%r != %r" % (mm.win, (exp_lo, exp_hi)))
    check("四区字节数 = RTL 口径（in/wgt/bias/out）",
          mm.lens == [bb.in_bytes, bb.wgt_bytes, bb.bias_bytes, bb.out_bytes],
          "%r != %r" % (mm.lens, list(bb)))
    check("ctrl 页 H/CIN/KSIZE 落点",
          mm.read(0x10000 + k3_engine.R_H) == 320
          and mm.read(0x10000 + k3_engine.R_CIN) == 16
          and mm.read(0x10000 + k3_engine.R_KSIZE) == 3)
    check("offs 页 IN/OUT 低字落点",
          mm.read(k3_engine.B_IN_FM) == 0x10000000
          and mm.read(k3_engine.B_OUT_FM) == 0x11000000)
    check("readback 里能读到 0xB0 bytes_in",
          eng.readback()["BYTES_IN_RTL"] == bb.in_bytes)
    check("describe 提到 base 与 cap_b",
          "0x40060000" in eng.describe() and "cap_b=4608" in eng.describe(),
          eng.describe())

    # ---- 1b) 平铺布局的计算（缓冲不连续时窗口仍要包住四段）----
    mm2 = _FakeWrapMMIO(0x40060000, k3_engine.WINDOW_SIZE)
    eng2 = k3_engine.K3Engine(base_addr=0x40060000,
                              mmio_factory=lambda a, l: mm2)
    r2 = eng2.run(_ns(0x10100000), _ns(0x10000000), _ns(0x10F00000),
                  _ns(0x10200000), 80, 80, 192, 192, 3, 1, 1, 1, 4,
                  c8_in=True, c8_out=True, layer="ut.k3.big")
    check("乱序地址也能算对窗口（sticky 不触发）", r2.rc == 0, repr(r2))
    bb2 = k3_engine.buffer_bytes(80, 80, 192, 192, hout=80, wout=80)
    check("窗口包住最小地址与最大末端",
          mm2.win[0] == 0x10000000
          and mm2.win[1] == max(0x10100000 + bb2.in_bytes,
                                0x10000000 + bb2.wgt_bytes,
                                0x10F00000 + bb2.bias_bytes,
                                0x10200000 + bb2.out_bytes), repr(mm2.win))

    # ---- 1c) sticky_oob 必须抛（并且诊断要能打印）----
    mm3 = _FakeWrapMMIO(0x40060000, k3_engine.WINDOW_SIZE)
    mm3.sticky = 1
    eng3 = k3_engine.K3Engine(base_addr=0x40060000,
                              mmio_factory=lambda a, l: mm3)
    expect_raise("sticky_oob 抛 K3Error（带粘性提示）", k3_engine.K3Error,
                 lambda: eng3.run(inb, wgb, bib, outb, 320, 320, 16, 16, 3, 1,
                                  1, 1, 8, c8_in=True, c8_out=True),
                 contains="sticky_oob", detail=repr(mm3.ret))
    check("sticky 记录到 oob_ever", eng3.oob_ever is True)
    mm3.sticky = 0
    check("诊断文本含 0xA8/0xAC/0xB0 与 STALL",
          all(s in eng3.diag_text() for s in ("0xA8", "0xAC", "0xB0", "STALL")),
          eng3.diag_text())

    # ---- 1d) 错误码 1..9 / 0xA ----
    for code in (1, 2, 3, 4, 5, 6, 7, 8, 9, 0xA):
        mmx = _FakeWrapMMIO(0x40060000, k3_engine.WINDOW_SIZE)
        mmx.force_rc = code
        ex = k3_engine.K3Engine(base_addr=0x40060000,
                                mmio_factory=lambda a, l, _m=mmx: _m)
        expect_raise("AP_RETURN=%#x 抛 K3ReturnError（带原因）" % code,
                     k3_engine.K3ReturnError,
                     lambda e=ex: e.run(inb, wgb, bib, outb, 320, 320, 16, 16,
                                        3, 1, 1, 1, 8, c8_in=True,
                                        c8_out=True),
                     contains=k3_engine.ERR_MEANING[code].split("（")[0][:6])
    # 0xA 的窗口提示要提到 WIN
    mmw = _FakeWrapMMIO(0x40060000, k3_engine.WINDOW_SIZE)
    mmw.force_window_fail = True
    engw = k3_engine.K3Engine(base_addr=0x40060000,
                              mmio_factory=lambda a, l: mmw)
    expect_raise("窗口越界(0xA) 的异常提到 WIN/窗口", k3_engine.K3ReturnError,
                 lambda: engw.run(inb, wgb, bib, outb, 320, 320, 16, 16, 3, 1,
                                  1, 1, 8, c8_in=True, c8_out=True),
                 contains="地址窗口越界")

    # ---- 1d2) 超时：引擎永远 busy ----
    mmt = _FakeWrapMMIO(0x40060000, k3_engine.WINDOW_SIZE)
    mmt.never_done = True
    engt = k3_engine.K3Engine(base_addr=0x40060000,
                              mmio_factory=lambda a, l: mmt)
    expect_raise("k3 超时抛 K3Timeout", k3_engine.K3Timeout,
                 lambda: engt.run(inb, wgb, bib, outb, 320, 320, 16, 16, 3, 1,
                                  1, 1, 8, c8_in=True, c8_out=True,
                                  timeout_s=0.02), contains="超时")

    # ---- 1e) 预检不过：一个寄存器都不写 ----
    mm4 = _FakeWrapMMIO(0x40060000, k3_engine.WINDOW_SIZE)
    eng4 = k3_engine.K3Engine(base_addr=0x40060000,
                              mmio_factory=lambda a, l: mm4)
    bad_cases = [
        ("s=2 且 d=2 被挡住", dict(s=2, d=2, pad=2)),
        ("pad≠d 被挡住", dict(s=1, d=2, pad=1)),
        ("cin 非 8 倍数被挡住", dict(cin=12)),
        ("k=3 之外的核型被挡住", dict(k=1)),
        ("shift=32 被挡住", dict(shift=32)),
        ("H>320 被挡住", dict(h=321)),
        ("cout>192 被挡住", dict(cout=200)),
    ]
    for label, kw in bad_cases:
        args = dict(cin=16, cout=16, k=3, s=1, d=1, pad=1, shift=8,
                    h=320, w=320)
        args.update(kw)
        try:
            eng4.run(inb, wgb, bib, outb, args["h"], args["w"], args["cin"],
                     args["cout"], args["k"], args["s"], args["d"],
                     args["pad"], args["shift"], c8_in=True, c8_out=True)
            check(label, False, "没有抛异常")
        except k3_engine.K3Unsupported:
            check(label, mm4.start_writes == 0,
                  "start_writes=%d" % mm4.start_writes)
    # 槽容量：cin=192, W=320, d=2 ⇒ 24*320*2 = 15360 > 4608
    check("槽容量口径 need_cap", k3_engine.need_cap(112, 320, 1) == 4480
          and k3_engine.need_cap(112, 320, 2) == 8960)
    try:
        eng4.run(inb, wgb, bib, outb, 320, 320, 192, 64, 3, 1, 2, 2, 8,
                 c8_in=True, c8_out=True)
        check("need_cap>4608 被挡住", False, "没有抛异常")
    except k3_engine.K3Unsupported as exc:
        check("need_cap>4608 被挡住", "槽容量" in str(exc), str(exc)[:120])

    # ---- 1f) C8 声明：CHW 不可路由 ----
    try:
        eng4.run(inb, wgb, bib, outb, 320, 320, 16, 16, 3, 1, 1, 1, 8,
                 c8_in=False, c8_out=False)
        check("CHW 被挡住", False, "没有抛异常")
    except k3_engine.K3Unsupported as exc:
        check("CHW 被挡住", "C8" in str(exc), str(exc)[:100])

    # ---- 1g) 融合出口（k3 没有）----
    try:
        eng4.run(inb, wgb, bib, outb, 320, 320, 16, 16, 3, 1, 1, 1, 8,
                 c8_in=True, c8_out=True, out_lut=np.zeros(256, np.int8))
        check("out_lut 被挡住", False, "没有抛异常")
    except k3_engine.K3Unsupported as exc:
        check("out_lut 被挡住", "out_lut" in str(exc), str(exc)[:100])

    # ---- 1h) 拍数公式：全网 114 层 = 259.20 M 拍（设计文档结论）----
    with open(MANIFEST, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    convs = [o for o in manifest["ops"] if o["op"] == "conv" and o["needed"]]
    cyc3 = sum(k3_engine.theoretical_cycles(o["h"], o["w"], o["cin"], o["cout"],
                                            s=o["s"], hout=o["hout"],
                                            wout=o["wout"])
               for o in convs if int(o["k"]) == 3)
    check("k3 全网理论拍数 = 259.20 M", cyc3 == 259200000, cyc3)
    cyc5 = sum(k5_engine.theoretical_cycles(o["h"], o["w"], o["cin"], o["cout"],
                                            s=o["s"], hout=o["hout"],
                                            wout=o["wout"])
               for o in convs if int(o["k"]) == 5)
    check("k5 唯一层 = 58.98 M 拍（393 ms）", cyc5 == 58982400, cyc5)

    # ================= 2) K5Engine（基地址/形状断言/窗口）=================
    ov5 = _FakeOverlay({
        "k5_wrap": {"type": "xilinx.com:user:k5_axi_wrap_bd:1.0",
                    "phys_addr": 0x40080000, "addr_range": 0x20000},
    })
    found5 = k5_engine.K5Engine.discover(ov5, "k5_wrap")
    check("discover 找到 k5_wrap @0x40080000",
          found5 == (0x40080000, "k5_wrap", 0x20000), repr(found5))
    check("k5 默认基地址是预留的 0x40080000", k5_engine.DEFAULT_BASE == 0x40080000)
    mm5 = _FakeWrapMMIO(0x40080000, k5_engine.WINDOW_SIZE, wgt_bpc=28, mode="k5")
    eng5 = k5_engine.K5Engine(base_addr=0x40080000,
                              mmio_factory=lambda a, l: mm5)
    r5 = eng5.run(inb, wgb, bib, outb, 320, 320, 12, 48, 5, 1, 1, 2, 9,
                  relu=True, c8_in=True, c8_out=True, layer="ut.k5")
    check("k5 run 通过（12/48/320/pad2）", r5.rc == 0, repr(r5))
    bb5 = k5_engine.buffer_bytes(320, 320, 12, 48)
    check("k5 bytes: wgt = cout*cin*28 = 16128",
          bb5.wgt_bytes == 48 * 12 * 28, bb5.wgt_bytes)
    check("k5 bytes: in 按 16 通道（12→补零）",
          bb5.in_bytes == 16 * 320 * 320 * 8, bb5.in_bytes)
    check("k5 run 的窗口包住四段",
          mm5.win == (min(0x10000000, 0x10800000, 0x10A00000, 0x11000000),
                      max(0x10000000 + bb5.in_bytes, 0x10800000 + bb5.wgt_bytes,
                          0x10A00000 + bb5.bias_bytes,
                          0x11000000 + bb5.out_bytes)), repr(mm5.win))
    check("k5 start 只写一次", mm5.start_writes == 1, mm5.start_writes)

    # k5 形状是编译期特化 ⇒ 任何不同都必须挡住（RTL 不看 ctrl_h/w）
    for label, kw, contains in [
        ("k5 relu=False 被挡住", dict(relu=False), "relu"),
        ("k5 cin 不是 12 被挡住", dict(cin=16), "CIN"),
        ("k5 cout 不是 48 被挡住", dict(cout=64), "COUT"),
        ("k5 H!=320 被挡住", dict(h=160, w=320), "H/W"),
        ("k5 pad!=2 被挡住", dict(pad=1), "pad"),
        ("k5 s!=1 被挡住", dict(s=2), "stride"),
        ("k5 核型不是 5 被挡住", dict(k=3), "k="),
    ]:
        args = dict(cin=12, cout=48, k=5, s=1, d=1, pad=2, shift=9,
                    h=320, w=320)
        args.update(kw)
        mm5b = _FakeWrapMMIO(0x40080000, k5_engine.WINDOW_SIZE, wgt_bpc=28, mode="k5")
        e5 = k5_engine.K5Engine(base_addr=0x40080000,
                                mmio_factory=lambda a, l, _m=mm5b: _m)
        try:
            e5.run(inb, wgb, bib, outb, args["h"], args["w"], args["cin"],
                   args["cout"], args["k"], args["s"], args["d"], args["pad"],
                   args["shift"], relu=args.get("relu", True), c8_in=True,
                   c8_out=True)
            check(label, False, "没有抛异常")
        except k5_engine.K5Unsupported as exc:
            check(label, (contains in str(exc)) and mm5b.start_writes == 0,
                  "start=%d msg=%s" % (mm5b.start_writes, str(exc)[:120]))

    # ================= 3) route check / 权重容器 =================
    e3 = _k3_entry(16, 16)
    op3 = _op("ut.k3", 16, 16, 3)
    good, why = runner.k3_route_check(op3, e3, True, True)
    check("k3 route: PAD12 层可路由", good, why)
    bad_e = _k3_entry(16, 16, w_layout="oc_ic_ky_kx")
    good, why = runner.k3_route_check(op3, bad_e, True, True)
    check("k3 route: 旧容器(3,3)被拒", not good and "k3_pad12" in why, why)
    good, why = runner.k3_route_check(op3, e3, False, False)
    check("k3 route: CHW 被拒", not good and "C8" in why, why)
    good, why = runner.k3_route_check(op3, e3, True, True,
                                      out_lut=np.zeros(256, np.int8))
    check("k3 route: out_lut 被拒", not good and "out_lut" in why, why)
    good, why = runner.k3_route_check(_op("ut.k1", 16, 16, 1), e3, True, True)
    check("k3 route: k=1 不归它管", not good and "≠ 3" in why, why)

    e5 = _k5_entry(48, 12)
    op5 = _op("ut.k5", 12, 48, 5, pad=2)
    good, why = runner.k5_route_check(op5, e5, True, True)
    check("k5 route: PAD28 层可路由", good, why)
    good, why = runner.k5_route_check(op5, _k5_entry(48, 12,
                                                      w_layout="oc_ic_ky_kx"),
                                      True, True)
    check("k5 route: 旧容器(5,5)被拒（board_bundle 的包就是这种）",
          not good and "k5_pad28" in why, why)
    good, why = runner.k5_route_check(_op("ut.k5", 12, 48, 5, pad=2,
                                          relu=False), e5, True, True)
    check("k5 route: relu=False 被拒", not good and "relu" in why, why)

    # 分流器
    check("route_tag 分流 k=1/3/5/其它",
          runner.route_tag(_op("a", 8, 8, 1)) == "k1"
          and runner.route_tag(_op("b", 8, 8, 3)) == "k3"
          and runner.route_tag(_op("c", 8, 8, 5, pad=2)) == "k5"
          and runner.route_tag(_op("d", 8, 8, 7, pad=3)) == "hls"
          and runner.route_tag({"op": "cat"}) is None)
    good, why = runner.conv_route_check(op3, e3, True, True)
    check("conv_route_check 分到 k3", good, why)
    good, why = runner.conv_route_check(_op("d", 8, 8, 7, pad=3), None,
                                        True, True)
    check("conv_route_check 对 k=7 明确说没有 IP",
          not good and "没有对应的手写 IP" in why, why)

    # ================= 4) PYNQEngine.conv 的路由接线 =================
    old_cls = conv_engine.Conv2dEngine
    conv_engine.Conv2dEngine = _OldEngine
    try:
        layout = {"a": True, "b": True}
        index = {"layers": {"w": _k3_entry(16, 16),
                            "w5": _k5_entry(48, 12)}}
        blob = bytes(1024)
        overlay = _FakeOverlay({
            "k1_wrap": {"type": "xilinx.com:user:k1_axi_wrap:1.0",
                        "phys_addr": 0x40040000, "addr_range": 0x20000},
            "k3_wrap": {"type": "xilinx.com:user:k3_axi_wrap_bd:1.0",
                        "phys_addr": 0x40060000, "addr_range": 0x20000},
        })
        arena = _Arena(layout)
        eng = runner.PYNQEngine(overlay, index, blob, arena,
                                use_k1=False, use_k3=True, use_k5=True)
        check("PYNQEngine 接入 K3Engine", eng.k3 is not None)
        check("K3Engine 基地址来自 hwh", eng.k3.base_addr == 0x40060000,
              hex(eng.k3.base_addr))
        old = _OldEngine.instances[-1]

        # 4a) 合格 k=3 层：走 k3，不碰老引擎
        shared_mm = _FakeWrapMMIO(0x40060000, k3_engine.WINDOW_SIZE)
        eng.k3.mmio = shared_mm
        eng.k3.ctrl = k3_engine._Page(shared_mm, k3_engine.CTRL_PAGE_OFF)
        eng.k3.offs = k3_engine._Page(shared_mm, 0)
        eng.conv("a", "b", op3)
        check("k=3 层走 K3（老引擎 0 次）",
              eng.k3_calls == 1 and len(old.calls) == 0,
              "k3=%d old=%d" % (eng.k3_calls, len(old.calls)))
        check("K3 写入了 CIN/COUT/KSIZE",
              shared_mm.read(0x10000 + k3_engine.R_CIN) == 16
              and shared_mm.read(0x10000 + k3_engine.R_COUT) == 16
              and shared_mm.read(0x10000 + k3_engine.R_KSIZE) == 3)
        check("K3 写入窗口（k3 特有）",
              shared_mm.read(0x10000 + k3_engine.R_WIN_LO) != 0
              and shared_mm.read(0x10000 + k3_engine.R_WIN_HI) != 0)
        check("k3 stall 统计进 PYNQEngine", eng.k3_stall_fill == 12,
              eng.k3_stall_fill)
        check("k3 summary 含执行/回退与 stall",
              "执行 1 层" in eng.k3_summary()
              and "stall" in eng.k3_summary(), eng.k3_summary())

        # 4b) k=3 但容器不对：必须抛错、不回退老引擎
        old.calls.clear()
        bad_op = _op("ut.badcontainer", 16, 16, 3, wkey="w5")
        expect_raise("k=3 路由失败 → 抛错（不回退 HLS）", RuntimeError,
                     lambda: eng.conv("a", "b", bad_op),
                     contains="不会回退 HLS")
        check("抛错后老引擎未被调用", len(old.calls) == 0, len(old.calls))

        # 4c) k=5 但 IP 未接入：必须抛错
        old.calls.clear()
        op5b = _op("ut.k5", 12, 48, 5, pad=2, wkey="w5")
        expect_raise("k=5 无 IP → 抛错（不回退 HLS）", RuntimeError,
                     lambda: eng.conv("a", "b", op5b),
                     contains="不会回退 HLS")
        check("k=5 抛错后老引擎未被调用", len(old.calls) == 0, len(old.calls))

        # 4d) conv_fuse_ok：k3 不参与融合，k1 保持）
        check("k=3 层不参与融合规划", eng.conv_fuse_ok(op3, out_lut=True) is False)
        check("k=1 层仍可融合（k1 行为不变）",
              eng.conv_fuse_ok(_op("ut.k1", 16, 16, 1), out_lut=True) is True)

        # 4e) 无 HLS 的 bit：PYNQEngine 仍可构造，落到 HLS 的层明确报错
        _OldEngine.fail_with = "overlay 里找不到 conv2d_engine IP，检查 .bit"
        try:
            eng_nohls = runner.PYNQEngine(overlay, index, blob, arena,
                                          use_k1=False, use_k3=True,
                                          use_k5=False)
            check("无 HLS 时 PYNQEngine 仍可构造", eng_nohls.unit is None
                  and eng_nohls.hls_missing is True)
            old.calls.clear()
            expect_raise("无 HLS 时 k=1 层明确报错", RuntimeError,
                         lambda: eng_nohls.conv(
                             "a", "b", _op("ut.k1", 16, 16, 1)),
                         contains="没有老 HLS 卷积引擎")
        finally:
            _OldEngine.fail_with = None

        # 4f) 其它异常（不是"找不到 conv2d_engine"）必须原样抛
        _OldEngine.fail_with = "pynq.MMIO 打不开"
        try:
            runner.PYNQEngine(overlay, index, blob, arena, use_k1=False,
                              use_k3=True, use_k5=False)
            check("非'缺 HLS'异常原样抛", False, "没有抛异常")
        except RuntimeError as exc:
            check("非'缺 HLS'异常原样抛", "MMIO" in str(exc), str(exc)[:80])
        finally:
            _OldEngine.fail_with = None

        # ================= 5) 真实 manifest 的 ip_plan =================
        with open(MANIFEST, "r", encoding="utf-8") as f:
            m640 = json.load(f)
        pack_path = next((p for p in PACK_CANDIDATES if os.path.exists(p)), None)
        if pack_path is None:
            print("SKIP  权重包不在仓库路径下，跳过第 5 组")
        else:
            with open(pack_path, "r", encoding="utf-8") as f:
                index640 = json.load(f)
            lay640 = c8_layout.table(m640, on=True)
            arena640 = _Arena(lay640)
            real_overlay = _FakeOverlay({
                "k3_wrap": {"type": "xilinx.com:user:k3_axi_wrap_bd:1.0",
                            "phys_addr": 0x40060000, "addr_range": 0x20000},
            })
            e640 = runner.PYNQEngine(real_overlay, index640, blob, arena640,
                                     use_k1=False, use_k3=True, use_k5=True)
            ops640 = [o for o in m640["ops"] if o.get("needed")]
            plan = e640.ip_plan(ops640)
            check("真实 manifest: k=3 全部 114 层可路由",
                  len(plan["k3"]["routed"]) == 114,
                  len(plan["k3"]["routed"]))
            un = [(o["name"], tag) for o, _w, tag in plan["unroutable"]]
            check("真实 manifest: 唯一无 IP 可用的是 k=5（IP 未进 BD）",
                  un == [("encoder.conv_first", "k5")], un)
            check("ip_plan 的 by_k 与 manifest 一致",
                  plan["by_k"] == {1: 132, 3: 114, 5: 1}, plan["by_k"])

            # 权重包容器自检：board_bundle 的 k=5 还是旧容器 ⇒ k5 不可路由
            bb_index = os.path.join(PS, os.pardir, "board", "board_bundle",
                                    "weights_pack", "index.json")
            if os.path.exists(bb_index):
                with open(bb_index, "r", encoding="utf-8") as f:
                    bb = json.load(f)
                op5c = next(o for o in ops640
                         if o.get("op") == "conv" and int(o["k"]) == 5)
                e5c = bb["layers"][op5c["weights_key"]]
                good, why = runner.k5_route_check(op5c, e5c, True, True)
                check("board_bundle 的 k=5 权重是旧容器 ⇒ 不可路由",
                      not good and "k5_pad28" in why, why)

            # ================= 6) 融合规划排除 k=3 =================
            try:
                r = object.__new__(runner.Runner)      # 不分配 150MB 竞技场
                r.m = m640
                r.scales = index640.get("scales", {})
                r.ops = ops640
                r.layout = lay640
                r.alias = runner.compute_cat_alias(m640, is_c8=lay640)

                # A/B：如果"什么都能融合"（老引擎/HLS），规划出 98 层
                # —— 这 98 层**全是 k=3 的 cheap_operation**（它们的输出直接
                # 进 cat）。这正好说明：k3 IP 接走它们以后，FUSE_OUT 一分钱
                # 收益都没有（k3 wrapper 没有 out_lut 端口）。
                r.engine = types.SimpleNamespace(
                    conv_fuse_ok=lambda op, out_lut=False, chsum=False: True)
                fp_all, _sk = r._plan_fuse()
                ks_all = set(int(o["k"]) for o in r.ops
                             if o["id"] in fp_all)
                check("无 IP 融合能力时规划出 98 层（全是 k=3）",
                      len(fp_all) == 98 and ks_all == {3},
                      "n=%d ks=%r" % (len(fp_all), ks_all))

                # 真引擎：k=3 层被排除 ⇒ 融合规划为空
                r.engine = e640
                fuse_plan, _skip = r._plan_fuse()
                check("融合规划把 k=3 层全部排除（IP 无 out_lut）",
                      fuse_plan == {} and len(_skip) == 0, len(fuse_plan))
            except Exception as exc:                       # noqa: BLE001
                check("融合规划把 k=3 层全部排除（IP 无 out_lut）", False,
                      repr(exc))
    finally:
        conv_engine.Conv2dEngine = old_cls

    print("==== K3/K5 离线回归 %d/%d PASS ====" % (ok, total))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
