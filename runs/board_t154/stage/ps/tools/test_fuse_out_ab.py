#!/usr/bin/env python3
"""主机侧 A/B：卷积融合出口（out_lut）关/开的逐位等价性。

为什么要有这一步：融合出口把"cat 的重定标"搬进卷积写回路径，语义上必须
**逐位精确**（不是"接近"）。这里用 RefEngine（与 PL 引擎语义逐位一致的
numpy 参考）把整网跑两遍——开关关 / 开关开——要求最终输出 0 不一致。

同时验证：
  1. `lut_util.make_out_lut(s_from, s_to)` 与 `Runner._lut(s_from, s_to)`
     对 20 组随机标度逐位相同（同一份实现，只是 numpy 视图）；
  2. 默认开关是关的（不设 PP_FUSE_CONV、不传 fuse_conv）；
  3. 打开后 fusion 规划非空（否则这个 A/B 是空跑，测试自己判失败）。

用法：
    cd pynq_specialized_code/ps
    /data/work/venv-demoir/bin/python tools/test_fuse_out_ab.py
    /data/work/venv-demoir/bin/python tools/test_fuse_out_ab.py \
        --manifest manifest_64.json          # 小图快速冒烟
"""
import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.dirname(HERE)
sys.path.insert(0, PS)
sys.path.insert(0, os.path.join(PS, "pynq"))

from pynq import lut_util                                   # noqa: E402
from pynq.runner import (                                   # noqa: E402
    NumpyArena, RefEngine, Runner, load_pack,
)

FAILS = []


def _fail(msg):
    FAILS.append(msg)
    print("[FAIL]", msg)


def test_lut_equivalence(runner, n=20, seed=20261022):
    """20 组随机标度对：make_out_lut 必须与 Runner._lut 逐位相同。"""
    rng = np.random.default_rng(seed)
    # 网络里真实出现的是 1e-3~1e-2 量级；随机覆盖放大/缩小与极端比例
    s_from = 10.0 ** rng.uniform(-4.0, -1.0, size=n)
    s_to = 10.0 ** rng.uniform(-4.0, -1.0, size=n)
    ok = 0
    for a, b in zip(s_from, s_to):
        ref = np.frombuffer(runner._lut(float(a), float(b)), dtype=np.int8)
        got = lut_util.make_out_lut(float(a), float(b))
        if np.array_equal(ref, got):
            ok += 1
        else:
            _fail("LUT 不同: s_from=%.9g s_to=%.9g" % (a, b))
    # 再做一次端到端语义检查：把 LUT 作用到随机张量上，必须等于 _req
    bad_total = 0
    for i in range(n):
        t = rng.integers(-128, 128, size=(3, 5, 7), dtype=np.int8)
        a, b = float(s_from[i]), float(s_to[i])
        ref = runner._req(t, a, b)
        got = lut_util.apply_out_lut(t, lut_util.make_out_lut(a, b))
        bad_total += int(np.count_nonzero(ref != got))
    print("LUT 对拍: %d/%d 组逐位相同；apply_out_lut 与 _req 不一致像素 %d"
          % (ok, n, bad_total))
    if bad_total:
        _fail("apply_out_lut 与 _req 有 %d 个像素不同" % bad_total)
    return ok


def test_default_off(manifest_path, pack):
    """默认必须关：不设环境变量、不传参数时 fuse_conv=False。"""
    old = os.environ.pop("PP_FUSE_CONV", None)
    try:
        r = Runner(manifest_path, pack)
        if r.fuse_conv:
            _fail("默认开关不是关的（PP_FUSE_CONV 未设置却为 True）")
        else:
            print("默认开关: 关（OK）")
        os.environ["PP_FUSE_CONV"] = "1"
        r2 = Runner(manifest_path, pack)
        os.environ["PP_FUSE_CONV"] = "0"
        r3 = Runner(manifest_path, pack)
        if not r2.fuse_conv or r3.fuse_conv:
            _fail("PP_FUSE_CONV 环境变量解析错误（1=%s, 0=%s）"
                  % (r2.fuse_conv, r3.fuse_conv))
        else:
            print("PP_FUSE_CONV 解析: 1→开, 0→关（OK）")
    finally:
        os.environ.pop("PP_FUSE_CONV", None)
        if old is not None:
            os.environ["PP_FUSE_CONV"] = old


def _load_image(manifest, pack, rng):
    b = manifest["buffers"][manifest["io"]["t_in"]]
    path = os.path.join(pack, "input_q.bin")
    want = b["c"] * b["h"] * b["w"]
    if os.path.exists(path):
        img = np.fromfile(path, dtype=np.int8)
        if img.size == want:
            return img.reshape(b["c"], b["h"], b["w"])
    print("INFO: %s 不存在或尺寸不匹配（%d），改用固定随机输入" % (path, want))
    return rng.integers(0, 128, size=(b["c"], b["h"], b["w"])).astype(np.int8)


def compare_deploy_ref(manifest, pack, out, tag):
    """有部署参考 ref_out.bin 时再对一次：证明关/开两遍都没偏离既有语义。"""
    path = os.path.join(pack, "ref_out.bin")
    if not os.path.exists(path):
        return
    ref = np.fromfile(path, dtype=np.int8)
    if ref.size != out.size:
        print("  （ref_out.bin 尺寸 %d != 输出 %d，跳过）" % (ref.size, out.size))
        return
    ref = ref.reshape(out.shape)
    bad = int(np.count_nonzero(out != ref))
    print("  %s 与部署参考 ref_out.bin: %d/%d 不一致" % (tag, bad, out.size))
    if bad:
        _fail("%s 与 ref_out.bin 有 %d 个不一致" % (tag, bad))


def run_net(manifest, manifest_path, pack, index, blob, img, fuse):
    arena = NumpyArena(manifest)
    eng = RefEngine(index, blob, arena)
    r = Runner(manifest_path, pack, engine=eng, arena=arena, fuse_conv=fuse)
    plan = {}
    if fuse:
        plan, _skip = r._plan_fuse()
        if not plan:
            _fail("融合开关打开后 _plan_fuse() 为空（这个 A/B 是空跑）")
    print("  fuse=%s  conv=%d  融合出口 conv=%d ..."
          % (fuse, r.n_conv, len(plan)), flush=True)
    t0 = time.time()
    out, dt = r.run(img)
    print("  fuse=%s  完成 %.1fs  输出 shape=%s" % (fuse, time.time() - t0, out.shape))
    if fuse:
        # 证明 cat 的跳过分支真的被命中：否则这个 A/B 可能只是"计划了但没生效"。
        print("  cat 跳过重定标 %d 段（融合 conv %d 个）"
              % (r._fuse_skips, len(plan)))
        if r._fuse_skips != len(plan):
            _fail("cat 跳过重定标 %d 段 != 融合 conv %d 个（接线没生效）"
                  % (r._fuse_skips, len(plan)))
    return np.asarray(out, dtype=np.int8).copy(), plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=os.path.join(PS, "manifest_640.json"))
    ap.add_argument("--pack", default=os.path.join(PS, "..", "deploy", "weights_pack"))
    ap.add_argument("--lut-cases", type=int, default=20)
    a = ap.parse_args()

    manifest_path = os.path.abspath(a.manifest)
    pack = os.path.abspath(a.pack)
    import json
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    index, blob = load_pack(pack)

    print("== 1) LUT 生成对拍（%d 组随机标度） ==" % a.lut_cases)
    probe = Runner(manifest_path, pack, fuse_conv=False)
    test_lut_equivalence(probe, n=a.lut_cases)

    print("== 2) 默认开关 / 环境变量 ==")
    test_default_off(manifest_path, pack)

    print("== 3) 整网 A/B（RefEngine + NumpyArena） ==")
    rng = np.random.default_rng(7)
    img = _load_image(manifest, pack, rng)
    base, _ = run_net(manifest, manifest_path, pack, index, blob, img, False)
    compare_deploy_ref(manifest, pack, base, "开关关")
    fused, plan = run_net(manifest, manifest_path, pack, index, blob, img, True)
    compare_deploy_ref(manifest, pack, fused, "开关开")
    bad = int(np.count_nonzero(base != fused))
    total = base.size
    print("整网 A/B: 不一致 = %d / %d" % (bad, total))
    if bad:
        idx = np.argwhere(base != fused)[:5]
        for i in idx:
            i = tuple(i)
            _fail("输出不一致 @%s base=%d fused=%d"
                  % (i, int(base[i]), int(fused[i])))

    # 覆盖率：融合的 conv 数 / 能融合的 conv 数 / 全部 conv
    print("融合覆盖: %d 个 conv 直接写 cat 标度（总 conv %d）"
          % (len(plan), probe.n_conv))

    if FAILS:
        print("\n失败 %d 项" % len(FAILS))
        return 1
    print("\n==== 融合出口关/开逐位一致；默认关 ====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
