#!/usr/bin/env python3
"""本机离线回归：K1Engine 的地址映射 / 轮询 / 超时 / 错误码 / 路由统计。

不需要板子、不需要 pynq：
  * 用 ``_FakeMMIO`` 模拟 ``k1_axi_wrap`` 的 128KB 窗口（A[16] 分页）；
  * 用 ``_FakeOverlay`` 模拟 PYNQ 的 ``ip_dict``（128KB 单条目 / 64KB 拆分条目）；
  * 用真实 manifest + weights_pack 统计"哪些层会走 K1"。

用法（在 ps/ 目录）：
    python3 tools/test_k1_route.py

判据：最后一行 ``==== K1 离线回归 N/N PASS ====`` 且退出码 0。
"""
import json
import os
import sys
import types

# 离线回归必须可重复：清掉可能从 shell 带进来的 K1 配置
for _v in ("K1_IP_RELU", "K1_IP_CIN_MAX", "K1_IP_COUT_MAX", "K1_IP_LANES",
           "K1_IP_FCLK_MHZ", "K1_IP_FCLK_HZ", "K1_IP_TIMEOUT_FACTOR",
           "K1_IP_TIMEOUT_EXTRA_S", "USE_K1_IP", "K1_IP_BASE", "K1_IP_NAME"):
    os.environ.pop(_v, None)

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.abspath(os.path.join(HERE, os.pardir))
PYNQ_DIR = os.path.join(PS, "pynq")
sys.path.insert(0, PYNQ_DIR)

import k1_engine  # noqa: E402
import runner      # noqa: E402


# ---------------------------------------------------------------- 假 MMIO

class _FakeMMIO(object):
    """模拟 k1_axi_wrap：一个 128KB 窗口，A[16] 分页，start 后 3 次读完成。"""

    def __init__(self, base, length):
        if length < k1_engine.WINDOW_SIZE:
            raise AssertionError("窗口长度 %#x < 128KB" % length)
        self.base = base
        self.length = length
        self.reg = [0] * (k1_engine.WINDOW_SIZE // 4)
        self.busy = 0
        self.done = 0
        self.err = 0
        self.err_code = 0
        self.return_code = 0
        self.start_writes = 0
        self.pending = 0
        self.force_err = None       # 非 None：start 时直接按该错误码失败
        self.never_done = False     # True：start 后永远 busy（测超时）

    # ---- 寄存器下标：ctrl 页 = 0x10000，offs 页 = 0 ----
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
        if ctrl and off == k1_engine.AP_CTRL:
            if data & k1_engine.AP_START:
                self.start_writes += 1
                self._start()
            return
        self.reg[idx] = data

    def read(self, offset=0, length=4, word_order="little"):
        del length, word_order
        idx = self._idx(offset)
        ctrl = (int(offset) & 0x10000) != 0
        off = int(offset) & 0xFF
        if ctrl and off == k1_engine.R_STATUS:
            # 每次读 STATUS 推进一步（模拟引擎跑 3 个"拍"）
            if self.pending > 0:
                self.pending -= 1
                if self.pending == 0:
                    self.busy = 0
                    self.done = 1
        if ctrl and off == k1_engine.AP_CTRL:
            return ((1 << 1) if self.done else 0) | \
                   (0 if self.busy else ((1 << 2) | (1 << 3)))
        if ctrl and off == k1_engine.AP_RETURN:
            return self.return_code
        if ctrl and off == k1_engine.R_STATUS:
            st = (1 if self.done else 0) | (2 if self.busy else 0) \
                | (4 if self.err else 0) | ((self.err_code & 0xF) << 4) \
                | (0 if self.busy else (1 << 8))
            return st
        return self.reg[idx]

    # ---- 启动前校验：与 k1_axi_wrap.v 一一对应 ----
    def _rd_ctrl(self, off):
        return self.reg[(k1_engine.CTRL_PAGE_OFF + off) // 4]

    def _rd_offs(self, off):
        return self.reg[off // 4]

    def _start(self):
        if self.busy:
            return
        self.done = self.err = 0
        self.return_code = self.err_code = 0
        if self.force_err is not None:
            self.done = 1
            self.err = 1
            self.err_code = int(self.force_err)
            self.return_code = int(self.force_err)
            return
        hi = [self._rd_offs(k1_engine.B_IN_FM + 4),
              self._rd_offs(k1_engine.B_WGT + 4),
              self._rd_offs(k1_engine.B_BIAS + 4),
              self._rd_offs(k1_engine.B_OUT_FM + 4)]
        lo = [self._rd_offs(k1_engine.B_IN_FM), self._rd_offs(k1_engine.B_WGT),
              self._rd_offs(k1_engine.B_BIAS), self._rd_offs(k1_engine.B_OUT_FM)]
        h = self._rd_ctrl(k1_engine.R_H)
        w = self._rd_ctrl(k1_engine.R_W)
        hout = self._rd_ctrl(k1_engine.R_HOUT)
        wout = self._rd_ctrl(k1_engine.R_WOUT)
        cin = self._rd_ctrl(k1_engine.R_CIN)
        cout = self._rd_ctrl(k1_engine.R_COUT)
        ks = self._rd_ctrl(k1_engine.R_KSIZE)
        stride = self._rd_ctrl(k1_engine.R_STRIDE)
        dil = self._rd_ctrl(k1_engine.R_DIL)
        pad = self._rd_ctrl(k1_engine.R_PAD)
        shift = self._rd_ctrl(k1_engine.R_SHIFT)
        code = 0
        if any(hi):
            code = 2
        elif any(x == 0 for x in lo):
            code = 1
        elif h == 0 or w == 0 or h > 4096 or w > 4096:
            code = 2
        elif hout != h or wout != w:
            code = 2
        elif cin < 8 or cin > 352 or (cin & 7):
            code = 2
        elif cout < 1 or cout > 192:
            code = 2
        elif ks != 1 or stride != 1 or dil != 1 or pad != 0:
            code = 2
        elif shift < 1 or shift > 31:
            code = 3
        if code:
            self.done = 1
            self.err = 1
            self.err_code = code
            self.return_code = code
            return
        self.busy = 1
        self.pending = (10 ** 9) if self.never_done else 3


class _FakeOverlay(object):
    def __init__(self, ip_dict):
        self.ip_dict = ip_dict


def _ns(addr):
    return types.SimpleNamespace(device_address=int(addr),
                                 physical_address=int(addr))


# ---------------------------------------------------------------- 测试

def main():
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

    # ---- 1) 地址发现：128KB 单条目 ----
    ov1 = _FakeOverlay({
        "k1_wrap": {"type": "xilinx.com:user:k1_axi_wrap:1.0",
                    "phys_addr": 0x40000000, "addr_range": 0x20000},
        "conv2d_engine_0/s_axi_ctrl": {"phys_addr": 0x40010000},
    })
    found = k1_engine.K1Engine.discover(ov1, "k1_wrap")
    check("discover 128KB 单条目", found == (0x40000000, "k1_wrap", 0x20000),
          repr(found))

    # ---- 2) 地址发现：hwh 拆成两个 64KB 条目 → 取较低基地址 ----
    ov2 = _FakeOverlay({
        "k1_wrap/s_axi": {"type": "xilinx.com:user:k1_axi_wrap:1.0",
                         "phys_addr": 0x40000000, "addr_range": 0x10000},
        "k1_wrap/s_axi_ctrl": {"phys_addr": 0x40010000,
                               "addr_range": 0x10000},
    })
    found = k1_engine.K1Engine.discover(ov2, "k1_wrap")
    check("discover 64KB 拆分条目取低基址", found is not None
          and found[0] == 0x40000000, repr(found))

    # ---- 2b) hwh 拆成两页时可接入；只有一页 64KB 时拒绝 ----
    ov_split = _FakeOverlay({
        "k1_wrap/s_axi": {"type": "xilinx.com:user:k1_axi_wrap:1.0",
                          "phys_addr": 0x40040000, "addr_range": 0x10000},
        "k1_wrap/s_axi_ctrl": {"phys_addr": 0x40050000,
                               "addr_range": 0x10000},
    })
    mm_split = _FakeMMIO(0x40040000, k1_engine.WINDOW_SIZE)
    eng_split = k1_engine.K1Engine(overlay=ov_split,
                                   mmio_factory=lambda a, l: mm_split)
    check("64KB 两页（128KB）可接入",
          eng_split.base_addr == 0x40040000, hex(eng_split.base_addr))

    ov_bad = _FakeOverlay({
        "k1_wrap": {"type": "xilinx.com:user:k1_axi_wrap:1.0",
                    "phys_addr": 0x40040000, "addr_range": 0x10000},
    })
    try:
        k1_engine.K1Engine(overlay=ov_bad,
                           mmio_factory=lambda a, l: _FakeMMIO(a, l))
        check("只有一页 64KB 时拒绝接入", False, "没有抛异常")
    except RuntimeError as exc:
        check("只有一页 64KB 时拒绝接入", "128KB" in str(exc), str(exc)[:80])

    # ---- 2c) 非 128KB 对齐基址必须拒绝（A[16] 分页会错）----
    try:
        k1_engine.K1Engine(base_addr=0x40041000,
                           mmio_factory=lambda a, l: _FakeMMIO(0x40040000, l))
        check("非 128KB 对齐基址被拒绝", False, "没有抛异常")
    except RuntimeError as exc:
        check("非 128KB 对齐基址被拒绝", "128KB" in str(exc), str(exc)[:80])

    # ---- 3) 找不到 → None（不猜地址）----
    check("discover 找不到返回 None",
          k1_engine.K1Engine.discover(_FakeOverlay({"ps7": {}}), "k1_wrap") is None)

    # ---- 4) 正常一层：寄存器落点 + 轮询 + 返回码 ----
    mm = _FakeMMIO(0x40000000, k1_engine.WINDOW_SIZE)
    eng = k1_engine.K1Engine(base_addr=0x40000000,
                             mmio_factory=lambda a, l: mm)
    # 让缓冲区地址是固定值（不依赖 PYNQ）
    inb, wgb, bib, outb = (_ns(0x10000000), _ns(0x10100000),
                           _ns(0x10200000), _ns(0x10300000))
    rc, dt = eng.run(inb, wgb, bib, outb, 320, 320, 48, 16, 1, 1, 1, 0, 6,
                     relu=True, c8_in=True, c8_out=True, layer="ut.valid")
    check("valid run 返回 0", rc == 0, "rc=%r" % (rc,))
    check("start 只写一次", mm.start_writes == 1, mm.start_writes)
    # ctrl 页（+0x10000）；offs 页（+0）
    check("H 写到 ctrl+0x18",
          mm.read(0x10000 + k1_engine.R_H) == 320)
    check("CIN 写到 ctrl+0x38",
          mm.read(0x10000 + k1_engine.R_CIN) == 48)
    check("SHIFT 写到 ctrl+0x68",
          mm.read(0x10000 + k1_engine.R_SHIFT) == 6)
    check("IN 低字/高字写在 offs+0x10/0x14",
          mm.read(k1_engine.B_IN_FM) == 0x10000000
          and mm.read(k1_engine.B_IN_FM + 4) == 0)
    check("OUT 低字/高字写在 offs+0x34/0x38",
          mm.read(k1_engine.B_OUT_FM) == 0x10300000
          and mm.read(k1_engine.B_OUT_FM + 4) == 0)
    check("readback 读回形状", eng.readback()["CIN"] == 48)

    # ---- 4b) start()/wait() 分步 API ----
    mm5 = _FakeMMIO(0x40000000, k1_engine.WINDOW_SIZE)
    eng5 = k1_engine.K1Engine(base_addr=0x40000000,
                              mmio_factory=lambda a, l: mm5)
    t0, to, cy, ctx = eng5.start(_ns(0x10000000), _ns(0x10100000),
                                 _ns(0x10200000), _ns(0x10300000),
                                 320, 320, 48, 16, 1, 1, 1, 0, 6,
                                 c8_in=True, c8_out=True, layer="ut.split")
    rc_split, _ = eng5.wait(to, t0, cy, ctx)
    check("start()/wait() 分步返回 0", rc_split == 0, rc_split)

    # ---- 5) 参数不支持：必须在碰硬件之前抛，且不写 start ----
    mm2 = _FakeMMIO(0x40000000, k1_engine.WINDOW_SIZE)
    eng2 = k1_engine.K1Engine(base_addr=0x40000000,
                              mmio_factory=lambda a, l: mm2)
    try:
        eng2.run(_ns(0x10000000), _ns(0x10100000), _ns(0x10200000),
                 _ns(0x10300000), 320, 320, 48, 16, 3, 1, 1, 0, 6,
                 c8_in=True, c8_out=True)
        check("k=3 被挡住", False, "没有抛异常")
    except k1_engine.K1Unsupported:
        check("k=3 被挡住", mm2.start_writes == 0,
              "start_writes=%d" % mm2.start_writes)

    # 5b) relu 不匹配 / CHW / cin 越界 都不碰硬件
    for label, kwargs in [
        # relu=False 已不再被挡（运行期 ReLU，见 k1_engine.check_supported 的注释）

        ("CHW 被挡住", dict(relu=True, c8_in=False, c8_out=False)),
        ("cin=12 被挡住", dict(relu=True, c8_in=True, c8_out=True)),
    ]:
        mmx = _FakeMMIO(0x40000000, k1_engine.WINDOW_SIZE)
        ex = k1_engine.K1Engine(base_addr=0x40000000,
                                mmio_factory=lambda a, l, _m=mmx: _m)
        cin = 12 if "cin=12" in label else 48
        try:
            ex.run(_ns(0x10000000), _ns(0x10100000), _ns(0x10200000),
                   _ns(0x10300000), 320, 320, cin, 16, 1, 1, 1, 0, 6,
                   **kwargs)
            check(label, False, "没有抛异常")
        except k1_engine.K1Unsupported:
            check(label, mmx.start_writes == 0,
                  "start_writes=%d" % mmx.start_writes)

    # ---- 6) RTL 返回码非 0：应抛 K1ReturnError 并带错误码 ----
    mm3 = _FakeMMIO(0x40000000, k1_engine.WINDOW_SIZE)
    mm3.force_err = 2
    eng3 = k1_engine.K1Engine(base_addr=0x40000000,
                              mmio_factory=lambda a, l: mm3)
    try:
        eng3.run(_ns(0x10000000), _ns(0x10100000), _ns(0x10200000),
                 _ns(0x10300000), 320, 320, 48, 16, 1, 1, 1, 0, 6,
                 c8_in=True, c8_out=True, layer="ut.err2")
        check("ERR_SIZE 抛 K1ReturnError", False, "没有抛异常")
    except k1_engine.K1ReturnError as exc:
        check("ERR_SIZE 抛 K1ReturnError",
              exc.return_code == 2 and "ERR_SIZE" in str(exc),
              "rc=%r msg=%s" % (exc.return_code, exc))

    # ---- 7) 超时：引擎永远 busy ----
    mm4 = _FakeMMIO(0x40000000, k1_engine.WINDOW_SIZE)
    mm4.never_done = True                    # 永远不完成
    eng4 = k1_engine.K1Engine(base_addr=0x40000000,
                              mmio_factory=lambda a, l: mm4)
    try:
        eng4.run(_ns(0x10000000), _ns(0x10100000), _ns(0x10200000),
                 _ns(0x10300000), 320, 320, 48, 16, 1, 1, 1, 0, 6,
                 c8_in=True, c8_out=True, timeout_s=0.02, layer="ut.timeout")
        check("超时抛 K1Timeout", False, "没有抛异常")
    except k1_engine.K1Timeout as exc:
        check("超时抛 K1Timeout", "超时" in str(exc), str(exc)[:80])

    # ---- 8) 真实 manifest 路由统计（不需要 pynq）----
    manifest_path = os.path.join(PS, "manifest_640.json")
    pack = os.path.join(PS, os.pardir, "board", "board_bundle", "weights_pack",
                        "index.json")
    pack = os.path.abspath(pack)
    if os.path.exists(manifest_path) and os.path.exists(pack):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        with open(pack, "r", encoding="utf-8") as f:
            index = json.load(f)
        ops = [o for o in manifest["ops"] if o.get("needed")]
        routed = 0
        reasons = {}
        for op in ops:
            if op["op"] != "conv":
                continue
            e = index["layers"][op["weights_key"]]
            good, why = runner.k1_route_check(op, e, True, True, None, None, 1)
            if good:
                routed += 1
            else:
                reasons[why] = reasons.get(why, 0) + 1
        # 2026-10-27：ReLU 改为运行期可控（0x70 bit0 → ctrl_relu，RTL 三处齐全，
        #   仿真 relu=0 的三个用例逐字节通过）⇒ 24 个 conv_proj 也能路由
        check("manifest C8 路由 132 层（含 24 个 relu=False）",
              routed == 132,
              "routed=%d reasons=%r" % (routed, reasons))
        check("k=3 全部回退（单条原因）",
              reasons.get("k=3 ≠ 1（本 IP 只做 1×1）") == 114, repr(reasons))
        check("k=5 回退", reasons.get("k=5 ≠ 1（本 IP 只做 1×1）") == 1,
              repr(reasons))
        # CHW 下必须全部回退
        n_chw = sum(1 for o in ops if o["op"] == "conv"
                    and runner.k1_route_check(
                        o, index["layers"][o["weights_key"]], False, False)[0])
        check("CHW 下 0 层路由", n_chw == 0, n_chw)
    else:
        print("SKIP  manifest/weights_pack 不在仓库路径下，跳过第 8 组")

    print("==== K1 离线回归 %d/%d PASS ====" % (ok, total))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
