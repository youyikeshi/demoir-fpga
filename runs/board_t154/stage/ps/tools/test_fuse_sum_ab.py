#!/usr/bin/env python3
"""conv→eca 融合出口（chsum / FUSE_SUM=1）的主机侧接线与等价性测试。

为什么要有这一步：chsum 出口把"ECA 前半段的整块通道扫描"搬进卷积写回路径。
语义上它必须**逐位精确**（不是"接近"），而且驱动多写一个 AXI-Lite 寄存器
就可能是往别人的地址上写（2026-10-22 已因此把板子挂死过一次）。所以分三段验：

  A. 驱动安全（mock PYNQ，无需板子）
     · fuse_offsets=(None, 0x40)：只写 chsum（0x40/0x44），**绝不碰 out_lut**；
     · 传 out_lut 但没声明它的偏移 → 直接抛异常，不写未知地址；
     · (None, None) → ValueError（没有可写的寄存器）。

  B. 合成拓扑 conv→eca 的 A/B（证明接线真的生效）
     · 配对 1 个 conv、ECA 跳过整块扫描 1 次；
     · chsum 与"拿特征图重算的通道和"逐位相同（verify_chsum）；
     · 融合前后整网输出 0 不一致；attn 与独立复算逐位相同。

  C. 真实网络整网 A/B（--full，约 2×45 s）
     · 报 conv→cat / conv→eca 配对数、跳过重标/扫描次数、不一致数、总元素数。

用法（在 pynq_specialized_code/ps 下）：
    python3 tools/test_fuse_sum_ab.py                 # A + B（秒级）
    python3 tools/test_fuse_sum_ab.py --full          # 再加 640 整网 A/B
"""
import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.dirname(HERE)
sys.path.insert(0, PS)
sys.path.insert(0, os.path.join(PS, "pynq"))

FAILS = []


def _fail(msg):
    FAILS.append(msg)
    print("[FAIL]", msg)


# ─────────────────────────────────────────────────────────────────────────────
# A. 驱动安全：只写声明过的寄存器
# ─────────────────────────────────────────────────────────────────────────────
class _RecMMIO:
    """记录所有写操作的假 MMIO（地址空间里哪个偏移被写、写了什么）。"""

    def __init__(self, addr, length):
        self.addr, self.length = int(addr), int(length)
        self.writes = {}

    def write(self, off, val):
        self.writes[int(off)] = int(val)

    def read(self, off):
        off = int(off)
        if off == 0x00:          # AP_CTRL：AP_DONE=bit1 立即置起
            return 0x2
        return 0                 # AP_RETURN = 0（ERR_OK）


class _FakeOverlay:
    def __init__(self, ip_dict):
        self.ip_dict = ip_dict


def _load_conv_engine():
    """按文件加载 conv_engine。它只在 __init__/run 里做 `from pynq import MMIO`，
    所以调用方要在整个构造/运行期间把假 `pynq` 放进 sys.modules。"""
    path = os.path.join(PS, "pynq", "conv_engine.py")
    spec = importlib.util.spec_from_file_location("conv_engine_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ns(addr):
    return types.SimpleNamespace(device_address=int(addr), physical_address=int(addr))


def _run_once(mod, eng, **kw):
    return eng.run(_ns(0x30000000), _ns(0x31000000), _ns(0x32000000),
                   _ns(0x33000000), 8, 8, 3, 4, 3, 1, 1, 1, 8, relu=True, **kw)


def test_driver_safety():
    fake_pynq = types.ModuleType("pynq")
    fake_pynq.MMIO = _RecMMIO
    ov = _FakeOverlay({
        "conv2d_engine_0/s_axi_control": {
            "type": "demoir:hls:conv2d_engine:1.0",
            "phys_addr": 0x40000000, "addr_range": 0x10000},
        "conv2d_engine_0/s_axi_ctrl": {
            "type": "demoir:hls:conv2d_engine:1.0",
            "phys_addr": 0x40010000, "addr_range": 0x10000},
    })
    real = sys.modules.get("pynq")
    sys.modules["pynq"] = fake_pynq       # Conv2dEngine 在 __init__/run 里延迟导入
    try:
        mod = _load_conv_engine()
        return _driver_checks(mod, ov)
    finally:
        if real is not None:
            sys.modules["pynq"] = real
        else:
            sys.modules.pop("pynq", None)


def _driver_checks(mod, ov):
    ok = 0

    # A1: 只有 chsum 端口 → 构造即清零，且只写 0x40/0x44
    eng = mod.Conv2dEngine(ov, fuse_offsets=(None, 0x40))
    assert eng.fuse_out_ok is False and eng.fuse_sum_ok is True, eng.fuse_offsets
    assert eng.offs.writes == {0x40: 0, 0x44: 0}, eng.offs.writes
    print("A1. (None,0x40) 构造: 只清零 chsum 0x40/0x44 → PASS")
    ok += 1

    # A2: 传 chsum 运行 → 0x40 写地址、0x44 写 0；不碰任何 out_lut 偏移
    buf = _ns(0x34000000)
    _run_once(mod, eng, chsum=buf)
    w = eng.offs.writes
    want = {0x10, 0x14, 0x1C, 0x20, 0x28, 0x2C, 0x34, 0x38, 0x40, 0x44}
    assert set(w) == want, "写了意外偏移: %s" % sorted(set(w) - want)
    assert w[0x40] == 0x34000000 and w[0x44] == 0, w
    print("A2. chsum 运行: 只写声明过的偏移（无 0x78 之类的越界写） → PASS")
    ok += 1

    # A3: 没声明 out_lut 却要查表 → 抛异常（绝不去写未知地址）
    try:
        _run_once(mod, eng, out_lut=_ns(0x35000000))
        raise AssertionError("应当抛 RuntimeError")
    except RuntimeError as e:
        assert "out_lut" in str(e), e
        print("A3. 未声明 out_lut 却启用 → 明确报错 → PASS")
        ok += 1

    # A4: 两个偏移都 None → ValueError
    try:
        mod.Conv2dEngine(ov, fuse_offsets=(None, None))
        raise AssertionError("应当抛 ValueError")
    except ValueError:
        print("A4. (None,None) → ValueError → PASS")
        ok += 1

    # A5: 字符串形式（CLI 传进来的样子）也能归一化
    eng5 = mod.Conv2dEngine(ov, fuse_offsets=("none", "0x40"))
    assert eng5.fuse_offsets == (None, 0x40), eng5.fuse_offsets
    print("A5. ('none','0x40') 归一化 → PASS")
    ok += 1

    # A6: 两个端口都声明 → 各自清零；out_lut 只在显式给缓冲时才写地址
    eng6 = mod.Conv2dEngine(ov, fuse_offsets=(0x78, 0x40))
    assert eng6.offs.writes == {0x78: 0, 0x7C: 0, 0x40: 0, 0x44: 0}, eng6.offs.writes
    _run_once(mod, eng6, out_lut=_ns(0x35000000), chsum=buf)
    w6 = eng6.offs.writes
    assert w6[0x78] == 0x35000000 and w6[0x7C] == 0, w6
    assert w6[0x40] == 0x34000000 and w6[0x44] == 0, w6
    print("A6. 两个端口都声明: out_lut/chsum 各自写对 → PASS")
    ok += 1

    print("---- A. 驱动安全 %d/6 PASS ----" % ok)
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# B. 合成拓扑：conv → eca（唯一消费者）
# ─────────────────────────────────────────────────────────────────────────────
def _buf(c, h, w, dtype="int8", tag=""):
    item = 1 if dtype == "int8" else 4
    return {"c": c, "h": h, "w": w, "dtype": dtype,
            "bytes": c * h * w * item, "tag": tag}


def build_synthetic_conv_eca(root, C=6, H=16, W=16, seed=20261022):
    """造一个最小网络：conv_a → eca → sam_sum（sam_sum 的另一支路是 conv_b）。

    关键：t1（conv_a 的输出）**唯一消费者是 eca**，正是 _plan_fuse_sum 的条件。
    ECA 输入通道数 = 3C（网络里 ECA 的输入就是三分支拼接后的 3C），所以
    conv_a 的 cout = 3C；sam_sum 的每支路通道数 = C。
    """
    from make_mock_pack import build_mock_pack

    CE = 3 * C
    m = {
        "semantics": "synthetic conv->eca，仅用于 chsum 接线测试",
        "input_hw": [H, W],
        "io": {"t_in": "t_in", "out1": "t_out"},
        "buffers": {
            "t_in": _buf(3, H, W, tag="input"),
            "t1": _buf(CE, H, W, tag="conv_a.out"),
            "t_attn": _buf(CE, 1, 1, dtype="fp32", tag="eca.attn"),
            "t3": _buf(C, H, W, tag="conv_b.out"),
            "t_out": _buf(C, H, W, tag="sam_sum.out"),
        },
        "ops": [
            {"id": 0, "name": "conv.a", "op": "conv", "in": ["t_in"],
             "out": ["t1"], "weights_key": "A", "cin": 3, "cout": CE,
             "k": 3, "s": 1, "d": 1, "pad": 1, "h": H, "w": W,
             "relu": True, "needed": True},
            {"id": 1, "name": "eca", "op": "eca", "in": ["t1"],
             "out": ["t_attn"], "weights_key": "E", "needed": True},
            {"id": 2, "name": "conv.b", "op": "conv", "in": ["t_in"],
             "out": ["t3"], "weights_key": "B", "cin": 3, "cout": C,
             "k": 1, "s": 1, "d": 1, "pad": 0, "h": H, "w": W,
             "relu": True, "needed": True},
            {"id": 3, "name": "sum", "op": "sam_sum",
             "in": ["t3", "t3", "t3", "t_attn", "t3"], "out": ["t_out"],
             "needed": True},
        ],
    }
    mpath = os.path.join(root, "manifest_synth.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    pack = os.path.join(root, "pack")
    build_mock_pack(mpath, pack, seed=seed)
    # 给几个真实量级的标度（缺省 1.0 会让均值公式的标度项测不出来）
    ipath = os.path.join(pack, "index.json")
    with open(ipath, "r", encoding="utf-8") as f:
        idx = json.load(f)
    idx["scales"] = {"t_in": 0.02, "t1": 0.0124825, "t3": 0.0083, "t_out": 0.0149}
    with open(ipath, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False)
    return mpath, pack, m


def test_synthetic(root):
    from pynq import ops_numpy as ops
    from pynq.runner import NumpyArena, RefEngine, Runner, load_pack

    mpath, pack, m = build_synthetic_conv_eca(root)
    manifest = m
    index, blob = load_pack(pack)
    rng = np.random.default_rng(7)
    img = rng.integers(-60, 60, size=(3, 16, 16)).astype(np.int8)

    def run(fuse, verify=False, sum_ok=None, probe=None):
        arena = NumpyArena(manifest)
        eng = RefEngine(index, blob, arena)
        if sum_ok is not None:
            eng.fuse_sum_ok = sum_ok
        r = Runner(mpath, pack, engine=eng, arena=arena, fuse_conv=fuse)
        r.verify_chsum = verify
        r.chsum_probe = probe
        r.record = {}
        out, _ = r.run(img)
        return np.asarray(out).copy(), r

    base, _r0 = run(False)
    fused, r1 = run(True, verify=True)

    # B1: 配对规划
    sum_plan = r1._plan_fuse_sum()
    if set(sum_plan) != {0}:
        _fail("合成网络的 conv→eca 配对应当恰好是 {0}，实际 %s" % sorted(sum_plan))
    else:
        print("B1. conv→eca 配对: %s（张量 %s, cout=%d） → PASS"
              % (sorted(sum_plan), sum_plan[0][0], sum_plan[0][1]))
    # 这个合成网络没有 cat，out_lut 那条链路应当为空
    cat_plan = r1._plan_fuse()[0]
    if cat_plan:
        _fail("合成网络不该有 conv→cat 配对（实际 %s）" % sorted(cat_plan))

    # B2: 真的走上 chsum，且次数正确
    if r1._fuse_sums != 1 or r1._fuse_sum_plan_n != 1:
        _fail("ECA 未用 chsum（_fuse_sums=%d, plan=%d）"
              % (r1._fuse_sums, r1._fuse_sum_plan_n))
    else:
        print("B2. ECA 用 chsum 1 次（跳过整块扫描 1 次） → PASS")
    # B3: chsum 与特征图重算逐位一致（verify_chsum 内部逐通道比较）
    if r1._chsum_verify_bad != 0:
        _fail("chsum 与特征图重算不一致 %d 个通道和" % r1._chsum_verify_bad)
    else:
        print("B3. chsum == 特征图逐通道和（0 个通道不一致） → PASS")

    # B4: 融合前后整网输出逐位一致
    bad = int(np.count_nonzero(base != fused))
    print("B4. 整网 A/B: 不一致 = %d / %d" % (bad, base.size))
    if bad:
        _fail("合成网络融合前后有 %d 个输出不一致" % bad)

    # B5: attn 与独立复算（不经过 Runner 内部的 chsum）逐位一致
    t1 = _r0.record["conv.a"]                     # 两次运行的 conv 输出相同
    sc = 0.0124825
    sums = t1.reshape(t1.shape[0], -1).sum(axis=1, dtype=np.int64)
    mean_c = (sums.astype(np.float64) * sc / float(t1.shape[1] * t1.shape[2])
              ).astype(np.float32)
    e = index["eca"]["E"]
    attn = ops.eca_attn_from_mean(mean_c, e["conv2"], e["conv1"])
    got = r1.record["eca"]
    if np.array_equal(attn, got):
        print("B5. attn 与独立复算逐位一致（%d 个通道） → PASS" % attn.size)
    else:
        nb = int(np.count_nonzero(attn != got))
        _fail("attn 与独立复算不一致 %d/%d" % (nb, attn.size))

    # B6: 没有声明 chsum 偏移时不该规划（也不该传缓冲）
    _out, r2 = run(True, sum_ok=False)
    if r2._fuse_sum_plan_n != 0 or r2._fuse_sums != 0:
        _fail("未声明 chsum 偏移仍在规划/使用（plan=%d, 用=%d）"
              % (r2._fuse_sum_plan_n, r2._fuse_sums))
    else:
        print("B6. 未声明 chsum 偏移 → 不规划、不传缓冲 → PASS")

    # B7: 一次性 probe（不依赖配对）——给 conv.b 挂 chsum 并当场自检
    out7, r7 = run(False, probe="conv.b")
    if r7._chsum_probe_n != 1 or r7._chsum_probe_bad != 0:
        _fail("chsum probe 失败（跑了 %d 次，不一致 %d）"
              % (r7._chsum_probe_n, r7._chsum_probe_bad))
    elif not np.array_equal(out7, base):
        _fail("chsum probe 改变了整网输出")
    else:
        print("B7. 单卷积 probe（真实通道和自检）0 不一致、输出不变 → PASS")
    return base, fused


# ─────────────────────────────────────────────────────────────────────────────
# C. 真实网络整网 A/B
# ─────────────────────────────────────────────────────────────────────────────
def run_real_net(manifest_path, pack, manifest, index, blob):
    from pynq.runner import NumpyArena, RefEngine, Runner, load_pack

    b = manifest["buffers"][manifest["io"]["t_in"]]
    path = os.path.join(pack, "input_q.bin")
    want = b["c"] * b["h"] * b["w"]
    if os.path.exists(path):
        img = np.fromfile(path, dtype=np.int8)
        if img.size != want:
            raise SystemExit("input_q.bin 尺寸 %d != %d" % (img.size, want))
        img = img.reshape(b["c"], b["h"], b["w"])
        src = path
    else:
        img = np.random.default_rng(7).integers(
            0, 128, size=(b["c"], b["h"], b["w"])).astype(np.int8)
        src = "随机输入"

    outs = {}
    for fuse in (False, True):
        arena = NumpyArena(manifest)
        eng = RefEngine(index, blob, arena)
        r = Runner(manifest_path, pack, engine=eng, arena=arena, fuse_conv=fuse)
        plan, _skip = (r._plan_fuse() if fuse else ({}, {}))
        sum_plan = (r._plan_fuse_sum() if fuse else {})
        t0 = time.time()
        out, _ = r.run(img)
        dt = time.time() - t0
        print("  fuse=%s  conv=%d  conv→cat=%d  conv→eca=%d  用时 %.1fs"
              % (fuse, r.n_conv, len(plan), len(sum_plan), dt), flush=True)
        if fuse:
            print("  cat 跳过重定标 %d 段；ECA 跳过整块扫描 %d 次"
                  % (r._fuse_skips, r._fuse_sums))
            if r._fuse_skips != len(plan):
                _fail("cat 跳过重定标 %d != 融合 conv %d"
                      % (r._fuse_skips, len(plan)))
            if r._fuse_sums != len(sum_plan):
                _fail("ECA 用 chsum %d 次 != 配对数 %d"
                      % (r._fuse_sums, len(sum_plan)))
        outs[fuse] = (np.asarray(out, dtype=np.int8).copy(), r, plan, sum_plan, src)
    return outs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true",
                    help="额外跑真实网络（640）整网 A/B（约 2×45 s）")
    ap.add_argument("--manifest", default=os.path.join(PS, "manifest_640.json"))
    ap.add_argument("--pack", default=os.path.join(PS, "..", "deploy", "weights_pack"))
    a = ap.parse_args()

    print("== A. 驱动安全（mock PYNQ） ==")
    test_driver_safety()

    print("\n== B. 合成拓扑 conv→eca 的 A/B ==")
    root = tempfile.mkdtemp(prefix="demoir_fusesum_")
    try:
        test_synthetic(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if a.full:
        from pynq.runner import load_pack
        manifest_path = os.path.abspath(a.manifest)
        pack = os.path.abspath(a.pack)
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        index, blob = load_pack(pack)
        print("\n== C. 真实网络整网 A/B（%s） ==" % os.path.basename(manifest_path))
        outs = run_real_net(manifest_path, pack, manifest, index, blob)
        base = outs[False][0]
        fused = outs[True][0]
        bad = int(np.count_nonzero(base != fused))
        print("整网 A/B: 不一致 = %d / %d（输入来源 %s）"
              % (bad, base.size, outs[False][4]))
        if bad:
            _fail("真实网络融合前后有 %d 个输出不一致" % bad)
        # 有部署参考时再对一次（证明两遍都没偏离既有语义）
        ref_path = os.path.join(pack, "ref_out.bin")
        if os.path.exists(ref_path):
            ref = np.fromfile(ref_path, dtype=np.int8)
            if ref.size == base.size:
                ref = ref.reshape(base.shape)
                for tag, arr in (("开关关", base), ("开关开", fused)):
                    nb = int(np.count_nonzero(arr != ref))
                    print("  %s 与 ref_out.bin: %d/%d 不一致" % (tag, nb, arr.size))
                    if nb:
                        _fail("%s 与 ref_out.bin 有 %d 个不一致" % (tag, nb))
        print("配对: conv→cat %d 个，conv→eca %d 个"
              % (len(outs[True][2]), len(outs[True][3])))

    print()
    if FAILS:
        print("失败 %d 项" % len(FAILS))
        return 1
    print("==== chsum 接线正确：驱动只写声明的寄存器、chsum 逐位等于通道和、"
          "融合前后逐位一致 ====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
