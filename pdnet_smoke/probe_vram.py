"""探测 student/teacher 前向+反向的显存占用，用于确定冒烟测试/训练的分辨率。

在 refer_code/PDNet/PDNet 目录下运行：
    python ../../pdnet_smoke/probe_vram.py
"""
import os
import sys
import time

import torch

sys.path.insert(0, os.getcwd())

from model.nets_student import my_model
from model.nets_teacher import T_my_model

CKPT = 'Teacher_checkpoint/ghost_34large/UHDM/checkpoint_000150.tar'


def build_student():
    m = my_model(en_feature_num=48, en_inter_num=32, de_feature_num=64,
                 de_inter_num=32, sam_number=1)
    m._initialize_weights()
    return m


def build_teacher():
    m = T_my_model(48, 48, 64, 64, 1)
    sd = torch.load(CKPT, map_location='cpu', weights_only=True)['state_dict']
    m.load_state_dict(sd)
    for p in m.parameters():
        p.requires_grad = False
    return m


def count_params(m):
    return sum(p.numel() for p in m.parameters())


def probe(size, batch=1, train_step=False, teacher=True):
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    s = build_student().cuda()
    t = build_teacher().cuda() if teacher else None
    opt = torch.optim.Adam(s.parameters(), 2e-4)

    x = torch.randn(batch, 3, size, size, device='cuda')
    y = torch.randn(batch, 3, size, size, device='cuda')
    try:
        if train_step:
            s.train()
            outs = s(x)
            loss = sum(o.pow(2).mean() for o in outs[:3])
            if t is not None:
                with torch.no_grad():
                    touts = t(x)
                loss = loss + sum(o.pow(2).mean() for o in touts[:3])
            loss.backward()
            opt.step()
        else:
            s.eval()
            with torch.no_grad():
                s(x)
                if t is not None:
                    t(x)
        torch.cuda.synchronize()
        peak = torch.cuda.max_memory_allocated() / 1024 ** 2
        status = 'OK'
    except torch.cuda.OutOfMemoryError as e:
        peak = float('nan')
        status = 'OOM'
    finally:
        del s, opt
        if t is not None:
            del t
        torch.cuda.empty_cache()
    return status, peak


if __name__ == '__main__':
    s = build_student()
    t = build_teacher()
    print(f'student params: {count_params(s)/1e6:.3f} M')
    print(f'teacher params: {count_params(t)/1e6:.3f} M')
    print()
    print(f'{"size":>6} {"train step (s+t)":>18} {"infer (s)":>12} {"infer (s+t)":>14}')
    for size in (128, 192, 256, 320, 384, 512):
        st1, p1 = probe(size, train_step=True, teacher=True)
        st2, p2 = probe(size, train_step=False, teacher=False)
        st3, p3 = probe(size, train_step=False, teacher=True)
        fmt = lambda v: f'{v:8.0f} MB' if v == v else '     OOM '
        print(f'{size:>6} {st1:>10}{fmt(p1)} {st2:>8}{fmt(p2)} {st3:>10}{fmt(p3)}')
