"""把训练产出的 `.tar` 摊成 `weights.npz` —— PS 侧不装 torch 也能拿到权重。

为什么需要
----------
`vendor/PDNet/model/nets_student.py` 是 torch 的 `state_dict`（含结构信息与优化器状态），
PYNQ 上装不了 torch。而 `ps/run_ref.py` 只说"我要 `decoder.preconv_3.conv.0.weight`
这个数组"。本脚本负责把两者接上，并**只导出图上真正用到的张量**
（`out_2` / `out_3` 的输出头已经剪掉，它们的权重不该再占 DDR）。

顺带产出 `weights_manifest.json`：每个数组的形状/dtype/字节数/是否 NCHW，
后续 HLS 权重打包器（M2）按这份清单分块排布，不用再读 .tar。
"""
import argparse
import hashlib
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))            # demoir-fpga/
sys.path.insert(0, os.path.join(REPO, 'src'))
sys.path.insert(0, os.path.join(REPO, 'hls', 'ps'))

import paths                                             # noqa: E402
import vendor_boot                                       # noqa: E402
import graph_spec                                        # noqa: E402

paths.add_vendor_pdnet_to_path()
import torch                                             # noqa: E402
from model.nets_student import my_model                  # noqa: E402


def graph_param_keys(graph) -> dict:
    """图上引用到的全部 state_dict 键 -> 引用它的算子名列表（用于报错定位）。"""
    used = {}
    for op in graph.ops:
        for slot in ('w', 'b'):
            key = op.attrs.get(slot)
            if key:
                used.setdefault(key, []).append('%s.%s' % (op.name, slot))
    return used


def build_state_dict(ckpt, arch):
    model = my_model(arch['en_f'], arch['en_i'], arch['de_f'], arch['de_i'], arch['sam'])
    sd = torch.load(ckpt, map_location='cpu', weights_only=False)['state_dict']
    model.load_state_dict(sd, strict=True)          # 结构与权重不符立刻炸，不要静默
    model.eval()
    return model.state_dict()


def export(ckpt, arch, h, w, prune_heads, out_npz):
    graph = graph_spec.build_graph(h, w, prune_heads=prune_heads)
    used = graph_param_keys(graph)
    sd = build_state_dict(ckpt, arch)

    missing = sorted(k for k in used if k not in sd)
    if missing:
        raise SystemExit('权重里没有这些键（图与 checkpoint 不匹配）：\n  ' + '\n  '.join(missing))
    unused = sorted(k for k in sd if k not in used)

    arrays, manifest = {}, {}
    for key in sorted(used):
        arr = np.ascontiguousarray(sd[key].detach().cpu().numpy(), dtype=np.float32)
        arrays[key] = arr
        manifest[key] = {
            'shape': list(arr.shape), 'dtype': 'float32', 'bytes': int(arr.nbytes),
            'layout': 'NCHW' if arr.ndim == 4 else 'flat',
            'used_by': used[key][:3],
        }
    os.makedirs(os.path.dirname(os.path.abspath(out_npz)), exist_ok=True)
    np.savez(out_npz, **arrays)

    man = {
        'checkpoint': os.path.relpath(ckpt, REPO),
        'checkpoint_sha256': _sha256(ckpt),
        'arch': dict(arch),
        'example_input': [3, h, w],
        'prune_heads': bool(prune_heads),
        'tensors': manifest,
        'total_params': int(sum(a.size for a in arrays.values())),
        'total_bytes': int(sum(a.nbytes for a in arrays.values())),
    }
    with open(os.path.splitext(out_npz)[0] + '_manifest.json', 'w') as f:
        json.dump(man, f, indent=2, sort_keys=True)

    print('权重：%d 个张量 / %.3f M 参数 / %.2f MB (fp32)'
          % (len(arrays), man['total_params'] / 1e6, man['total_bytes'] / 1024 ** 2))
    print('导出：%s' % os.path.relpath(out_npz, REPO))
    if unused:
        print('图未使用（未导出 %d 个）：%s%s'
              % (len(unused), ', '.join(unused[:4]), ' …' if len(unused) > 4 else ''))
    return 0


def _sha256(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(chunk), b''):
            digest.update(block)
    return digest.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description='.tar → weights.npz（供 ps/run_ref.py 用）')
    ap.add_argument('--student', default='uhdm4k_s640_ep150')
    ap.add_argument('--size', type=int, default=512, help='示例输入边长（只影响清单，不改权重）')
    ap.add_argument('--keep-heads', action='store_true', help='不剪 out_2/out_3 输出头')
    ap.add_argument('--out', default=None)
    a = ap.parse_args(argv)

    ckpt, arch, desc = vendor_boot.resolve_student(a.student)
    dst = a.out or os.path.join(REPO, 'hls', 'build', 'weights', 'student.npz')
    print('student = %s' % desc)
    return export(ckpt, arch, a.size, a.size, not a.keep_heads, dst)


if __name__ == '__main__':
    sys.exit(main())
