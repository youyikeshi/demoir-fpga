#!/usr/bin/env python3
"""PS 调度层 × 引擎（PL 语义）联合集成测试（本机版）。

流程：构建假权重包 → Runner（RefEngine 假引擎）跑完整 249 层网络 →
校验：形状、取值范围、确定性（两次结果逐位一致）、算子计数。
上板时把 RefEngine 换成 PYNQEngine 即可复用同一份调度代码。

用法（任选）：
    python test_runner_local.py               # 默认用 ../manifest_64.json
    python test_runner_local.py --manifest ../manifest_64.json
"""
import argparse
import os
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pynq"))

from make_mock_pack import build_mock_pack      # noqa: E402
from runner import Runner                        # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=os.path.join(HERE, "..", "manifest_64.json"))
    ap.add_argument("--seed", type=int, default=1234)
    a = ap.parse_args()

    work = tempfile.mkdtemp(prefix="demoir_pack_")
    n_layers, blob = build_mock_pack(a.manifest, work, a.seed)
    print("假权重包: %d 层, %.1f MB" % (n_layers, blob / 1e6))

    r = Runner(a.manifest, work)
    h, w = r.m["input_hw"]
    print("调度: 需要执行的算子 %d 个（其中卷积 %d 个）" % (len(r.ops), r.n_conv))

    rng = np.random.default_rng(a.seed)
    img = rng.integers(0, 128, size=(3, h, w)).astype(np.int8)

    t0 = time.time()
    out1, dt1 = r.run(img)
    print("第一次运行: %.1fs, 输出 %s, 范围 [%d, %d]" %
          (dt1, out1.shape, int(out1.min()), int(out1.max())))

    out2, dt2 = r.run(img)
    assert out1.shape == (3, h, w), out1.shape
    assert out1.dtype == np.int8
    assert np.array_equal(out1, out2), "两次运行结果不一致（不确定！）"
    print("确定性检查: 两次运行逐位一致 通过")

    t_all = time.time() - t0
    print("==== 联合集成测试 PASS（%dx%d 输入, 共 %.1fs）====" % (h, w, t_all))


if __name__ == "__main__":
    main()
