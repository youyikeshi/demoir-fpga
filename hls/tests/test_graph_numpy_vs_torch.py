"""第二段等价性：整网 NumPy 参考 ↔ PyTorch `my_model` 前向。

这一段验证的是**图结构**，不是单个算子：
`ps/graph_spec.py` 把 nets_student 的模块嵌套摊平成了 577 条算子，
如果有人把某个 `torch.cat` 的顺序写反、把某层的 dilation 记错、
或者漏掉了 SAM 的一条分支，逐算子测试是发现不了的，这里会立刻炸。

跑法（需要 torch）：
    .venv-hls/bin/python hls/tests/test_graph_numpy_vs_torch.py --size 128
    .venv-hls/bin/python hls/tests/test_graph_numpy_vs_torch.py --size 512 --image <path>
"""
import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, 'hls', 'ps'))
sys.path.insert(0, os.path.join(REPO, 'src'))

import graph_spec                                          # noqa: E402
import run_ref                                             # noqa: E402

import paths                                               # noqa: E402
import vendor_boot                                         # noqa: E402

paths.add_vendor_pdnet_to_path()
import torch                                               # noqa: E402
from model.nets_student import my_model                    # noqa: E402

DEFAULT_IMAGE = os.path.join(paths.SAMPLES, 'phone')
EXT = ('.jpg', '.jpeg', '.png', '.bmp', '.webp')


def pick_image(explicit):
    if explicit:
        return explicit
    if os.path.isdir(DEFAULT_IMAGE):
        for f in sorted(os.listdir(DEFAULT_IMAGE)):
            if f.lower().endswith(EXT):
                return os.path.join(DEFAULT_IMAGE, f)
    raise SystemExit('找不到测试图：给 --image，或往 %s 放一张' % DEFAULT_IMAGE)


def to_input(image_path, size):
    """读图 → 缩放到 size×size（**只为让测试快**）→ 补边到 32 的倍数。

    ⚠️ 正式推理不做缩放（见 src/infer.py 文件头）；这里缩放是为了让测试
    在几秒内跑完，不改变等价性结论。
    """
    from PIL import Image
    im = Image.open(image_path).convert('RGB')
    if size:
        im = im.resize((size, size), Image.LANCZOS)
    rgb = np.asarray(im, dtype=np.float32) / 255.0
    h, w, _ = rgb.shape
    wp, hp, wo, ho = run_ref.pad_amounts(h, w)
    return run_ref.pad_image(rgb, wp, hp, wo, ho), (wp, hp, wo, ho)


def torch_forward(model, x3hw):
    t = torch.from_numpy(np.ascontiguousarray(x3hw, dtype=np.float32))[None]
    with torch.no_grad():
        return model(t)[0][0].numpy()


def torch_infer_image(model, rgb, half=False):
    """与 `ps/run_ref.infer_image` 同一条路径：补边 → 整网 → 裁回原尺寸。

    单独测这个是因为"补边/裁回"最容易出 off-by-one（左补 w_pad、右补 w_odd，
    而且 w_odd = w_pad + (w%2)），而它只在图像边界几行体现，整网 PSNR 看不出来。
    """
    h, w, _ = rgb.shape
    wp, hp, wo, ho = run_ref.pad_amounts(h, w)
    x = run_ref.pad_image(rgb, wp, hp, wo, ho)
    t = torch.from_numpy(x)[None]
    if half:
        t = t.half()
        model = model.half()
    else:
        model = model.float()
    with torch.no_grad():
        out = model(t)[0]          # my_model 返回 (out_1, out_2, out_3, feature_map)
    out = out[0].float().numpy()   # 去掉 batch 维
    return np.clip(out[:, hp:hp + h, wp:wp + w], 0, 1)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--student', default='uhdm4k_s640_ep150')
    ap.add_argument('--image', default=None)
    ap.add_argument('--size', type=int, default=256, help='先缩放到 size×size（0 = 原尺寸）')
    ap.add_argument('--atol', type=float, default=3e-4)
    a = ap.parse_args(argv)

    img = pick_image(a.image)
    ckpt, arch, desc = vendor_boot.resolve_student(a.student)
    model = my_model(arch['en_f'], arch['en_i'], arch['de_f'], arch['de_i'], arch['sam'])
    model.load_state_dict(torch.load(ckpt, map_location='cpu', weights_only=False)['state_dict'],
                          strict=True)
    model.eval()

    x, _ = to_input(img, a.size)
    print('图  = %s' % os.path.basename(img))
    print('输入 = 3x%dx%d（已补边）' % (x.shape[1], x.shape[2]))
    print('权重 = %s' % desc)

    t0 = time.time()
    want_raw = torch_forward(model, x)
    t_torch = time.time() - t0

    with np.load(os.path.join(REPO, 'hls', 'build', 'weights', 'student.npz')) as z:
        weights = {k: np.ascontiguousarray(z[k], dtype=np.float32) for k in z.files}

    t0 = time.time()
    graph = graph_spec.build_graph(x.shape[1], x.shape[2])
    env = run_ref.execute(graph, {graph.input_name: np.ascontiguousarray(x, dtype=np.float32)},
                          weights, release=True)
    got_raw = env[graph.output_name]
    t_np = time.time() - t0

    if got_raw.shape != want_raw.shape:
        print('FAIL 形状 %s != %s' % (got_raw.shape, want_raw.shape))
        return 1

    # 两条口径都报：
    #   raw  —— 证明网络本体逐元素一致（不受 clamp 掩盖）
    #   clip —— 对齐 src/infer.py 的最终产物（clamp 之后）
    diff_raw = np.abs(got_raw - want_raw)
    got, want = np.clip(got_raw, 0, 1), np.clip(want_raw, 0, 1)
    diff = np.abs(got - want)
    psnr = 10 * np.log10(1.0 / max(float((diff ** 2).mean()), 1e-20))

    print()
    print('torch  %.2f s      numpy  %.2f s' % (t_torch, t_np))
    print('未 clamp：max|Δ| = %.3e   mean|Δ| = %.3e   p99.9 = %.3e'
          % (diff_raw.max(), diff_raw.mean(), np.percentile(diff_raw, 99.9)))
    print('clamp 后：max|Δ| = %.3e   PSNR = %.1f dB' % (diff.max(), psnr))

    # ---- 端到端：把"补边 → 整网 → 裁回"整条路走一遍 --------------------------
    from PIL import Image
    im = Image.open(img).convert('RGB')
    if a.size:
        im = im.resize((a.size, a.size), Image.LANCZOS)
    rgb = np.asarray(im, dtype=np.float32) / 255.0
    if a.size:
        # 故意裁成长宽都不整除 32 的尺寸，否则补边量全是 0，"补边/裁回"那一段
        # 就等于没测（本项目第一次就是这么被骗过去的：端到端数字与整网完全相同）。
        rgb = np.ascontiguousarray(rgb[:a.size - 13, :a.size - 7])
    wp, hp, wo, ho = run_ref.pad_amounts(rgb.shape[0], rgb.shape[1])
    print('端到端  ：%dx%d → 补边 (%d,%d,%d,%d) → 裁回'
          % (rgb.shape[1], rgb.shape[0], wp, hp, wo, ho))
    e_torch = torch_infer_image(model, rgb)
    e_np = np.clip(run_ref.infer_image(rgb, weights), 0, 1)
    if e_torch.shape != e_np.shape:
        print('FAIL 端到端形状 %s != %s' % (e_torch.shape, e_np.shape))
        return 1
    d_e = np.abs(e_torch - e_np)
    print('端到端  ：max|Δ| = %.3e   mean|Δ| = %.3e   PSNR = %.1f dB'
          % (d_e.max(), d_e.mean(), 10 * np.log10(1.0 / max(float((d_e ** 2).mean()), 1e-20))))

    ok = diff_raw.max() <= a.atol and d_e.max() <= a.atol
    print()
    print('%s 整网 NumPy 参考与 PyTorch 一致（容差 %.0e）' % ('ok  ' if ok else 'FAIL', a.atol))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
