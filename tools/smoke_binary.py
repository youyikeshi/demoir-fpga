"""二值化模型的 20 秒自检：不需要数据集、不需要 GPU。

它回答三个上机前必须确认的问题：

  1. 按 `src/configs/uhdm_bin_ep150.yaml` 的宽度建出来的模型，FP 参数量是不是
     论文的 5.93M 量级？（宽度抄错一个数字，后面 40 小时全白跑）
  2. 前向的三级输出形状对不对？
  3. 反向的梯度有没有真的回到 1-bit 卷积的**全精度影子权重**上？
     STE 写错时这一步会静默地全为 0，训练照跑不误，只是不收敛。

用法
----
    # 推荐：宽度直接从训练配置读
    DEMOIR_BIN=1 DEMOIR_MODE=full DEMOIR_RDB=1 \\
        python tools/smoke_binary.py --config uhdm_bin_ep150

    # 已经知道宽度时也可以手工给（跳过读配置这一步）
    python tools/smoke_binary.py --widths 48 64 64 96

退出码 0 = 全部通过，1 = 有硬性检查失败。
"""
import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(REPO, 'vendor', 'PDNet')
sys.path.insert(0, VENDOR)

import torch  # noqa: E402

# 论文 ESDNet(RDB) 的 FP 参数量（百万），用于给宽度标定做护栏
FP_REFERENCE_RDB = 5.934
FP_TOLERANCE = 0.05


def read_widths(cfg_name):
    """从 src/configs/<name>.yaml 的 MODEL 段取四个宽度。

    只做逐行的 `KEY: value` 扫描，不 import pyyaml —— 这个脚本要在"只有 torch
    的最小 venv"里也能跑，而配置是我们自己写的扁平文件，扫描足够可靠。
    """
    path = cfg_name if cfg_name.endswith('.yaml') else os.path.join(REPO, 'src', 'configs', cfg_name + '.yaml')
    if not os.path.exists(path):
        raise SystemExit('找不到配置：%s' % path)
    keys = ('EN_FEATURE_NUM', 'EN_INTER_NUM', 'DE_FEATURE_NUM', 'DE_INTER_NUM')
    found, section = {}, None
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            body = line.split('#', 1)[0].rstrip()
            if not body.strip():
                continue
            if not body.startswith((' ', '\t')) and body.rstrip().endswith(':'):
                section = body.strip().rstrip(':')
                continue
            if section != 'MODEL' or ':' not in body:
                continue
            k, v = (s.strip() for s in body.split(':', 1))
            if k in keys:
                try:
                    found[k] = int(v.strip().strip('"\''))
                except ValueError:
                    raise SystemExit('配置 %s 里 %s 的值不是整数：%r' % (path, k, v))
    missing = [k for k in keys if k not in found]
    if missing:
        raise SystemExit('配置 %s 的 MODEL 段缺键：%s' % (path, ', '.join(missing)))
    return tuple(found[k] for k in keys), path


def main():
    ap = argparse.ArgumentParser(description='二值化模型自检（形状 / 参数量 / STE 梯度）')
    ap.add_argument('--config', default='uhdm_bin_ep150', help='src/configs/ 下的名字，用来读 MODEL 宽度')
    ap.add_argument('--widths', nargs=4, type=int, metavar=('EN_F', 'EN_I', 'DE_F', 'DE_I'),
                    help='手工指定宽度，给了就不读配置')
    ap.add_argument('--size', type=int, default=128, help='输入边长，必须是 32 的倍数')
    ap.add_argument('--batch', type=int, default=2)
    a = ap.parse_args()

    if a.size % 32:
        raise SystemExit('--size 必须是 32 的倍数（网络有 2 次 pixel_unshuffle + 4 级下采样）')

    if a.widths:
        widths, src = tuple(a.widths), '(命令行 --widths)'
    else:
        widths, src = read_widths(a.config)

    from model.binary_esdnet import my_model, param_report, mabg_layers, sgra_layers

    mode = os.environ.get('DEMOIR_MODE', 'full').lower()
    rdb = int(os.environ.get('DEMOIR_RDB', '1'))
    print('=' * 72)
    print('宽度      : %s  ← %s' % (widths, src))
    print('档位      : MODE=%s RDB=%d SGRA=%s MABG_SCOPE=%s'
          % (mode, rdb, os.environ.get('DEMOIR_SGRA', '1'),
             os.environ.get('DEMOIR_MABG_SCOPE', 'boundary')))
    print('=' * 72)

    fails = []
    torch.manual_seed(0)

    # ---- 1. 参数量 -------------------------------------------------------
    net = my_model(*widths, 1)
    total, bw, equiv, nbc = param_report(net)   # 已经是百万为单位
    print('\n[1] 参数量')
    print('    FP 参数        : %.3f M' % total)
    print('    二值参数       : %.3f M' % bw)
    print('    二值等价计数量 : %.3f M%s' % (equiv, '   （论文 0.31M）' if nbc else ''))
    print('    二值卷积个数   : %d' % nbc)
    print('    MABG 层 %d / SGRA 层 %d' % (len(mabg_layers(net)), len(sgra_layers(net))))

    if rdb == 1:
        dev = (total - FP_REFERENCE_RDB) / FP_REFERENCE_RDB
        if abs(dev) > FP_TOLERANCE:
            fails.append('FP 参数量 %.3fM 偏离论文 5.93M 达 %+.1f%%：宽度或 DEMOIR_RDB 配错了'
                         % (total, dev * 100))
            print('    ✗ 偏离论文 5.93M 达 %+.1f%%（容差 %.0f%%）' % (dev * 100, FP_TOLERANCE * 100))
        else:
            print('    ✓ 与论文 5.93M 相符（%+.1f%%）' % (dev * 100))
    else:
        print('    （DEMOIR_RDB=0，Ghost 版骨架，不与论文 5.93M 比对）')

    # ---- 2. 前向形状 -----------------------------------------------------
    # forward 返回 (out1, out2, out3, [y1, y2, y3, f2, f3])：前三个是三级输出，
    # 最后一项是给损失用的中间特征，形状检查只看前三个。
    print('\n[2] 前向形状')
    net.train()
    x = torch.randn(a.batch, 3, a.size, a.size)
    outs = net(x)
    pyramid = [o for o in outs if torch.is_tensor(o)]
    print('    输入 %s -> 输出 %s' % (tuple(x.shape), [tuple(o.shape) for o in pyramid]))
    expect = [a.size, a.size // 2, a.size // 4]
    if len(pyramid) != 3:
        fails.append('应返回 3 个输出张量（全分辨率 + 2 级下采样），实际 %d' % len(pyramid))
    else:
        bad = [(i, tuple(o.shape)) for i, (o, e) in enumerate(zip(pyramid, expect))
               if o.shape[-1] != e or o.shape[1] != 3]
        for i, shp in bad:
            fails.append('第 %d 级输出应为 (B,3,%d,%d)，实际 %s' % (i + 1, expect[i], expect[i], shp))
        print('    ✓ 三级金字塔形状正确' if not bad else '    ✗ 形状不对')

    # ---- 3. STE 梯度是否活 -------------------------------------------------
    # 关键检查：1-bit 卷积的权重在计算图里是 Sign(w) 的结果，梯度必须能回到
    # 未量化的影子权重。若 STE 断链，这里所有 grad 都是 None，而训练不会有任何报错。
    print('\n[3] 反向梯度（STE 通路）')
    net.zero_grad()
    loss = sum(o.abs().mean() for o in pyramid)
    loss.backward()

    from model.binary_esdnet import BinaryConv2d
    bconvs = [(n, m) for n, m in net.named_modules() if isinstance(m, BinaryConv2d)]
    with_grad = [(n, m) for n, m in bconvs if m.weight.grad is not None]
    nonzero = [(n, m) for n, m in with_grad if m.weight.grad.abs().sum().item() > 0]
    print('    BinaryConv2d %d 个；有梯度 %d 个；梯度非零 %d 个'
          % (len(bconvs), len(with_grad), len(nonzero)))

    if not bconvs:
        print('    （DEMOIR_MODE=fp，没有二值卷积，跳过 STE 检查）')
    else:
        if len(with_grad) != len(bconvs):
            fails.append('%d 个二值卷积的权重没有梯度（STE 断链）' % (len(bconvs) - len(with_grad)))
        if len(nonzero) < len(bconvs) // 2:
            fails.append('只有 %d/%d 个二值卷积权重梯度非零，STE 可能失效' % (len(nonzero), len(bconvs)))
        if not fails:
            print('    ✓ 梯度回到了全精度影子权重')

    # ---- 收尾 -----------------------------------------------------------
    print('\n' + '=' * 72)
    if fails:
        print('自检失败：')
        for f in fails:
            print('  ✗ %s' % f)
        print('=' * 72)
        return 1
    print('自检通过。下一步：执行文档 §7.2 的单图过拟合（10 分钟），那一步才能')
    print('证明整条训练链路（数据 / 损失 / 优化器）都通。')
    print('=' * 72)
    return 0


if __name__ == '__main__':
    sys.exit(main())
