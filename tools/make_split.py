"""把 UHDM 解压后的图片按"图"划分为训练/评估两组。

为什么需要
----------
官方 tar 解压后只有一组图（500 对）。要在本地做训练+评估，必须自己划分，
而且**必须按整张图划分**——同一张图的不同裁块不能同时出现在训练和评估集里，
否则就是数据泄漏（评估分数会虚高）。

用硬链接而非复制：不占额外磁盘（实测 500 对约 5.7GB，复制会翻倍）。

用法
----
    python tools/make_split.py                       # datasets/uhdm/pairs -> train/ + eval/ (400/100)
    python tools/make_split.py --n-train 400         # 指定训练集图数
    python tools/make_split.py --src <dir> --out <dir>
    python tools/make_split.py --copy                # 用复制代替硬链接（跨盘时必需）
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_SRC = os.path.join(ROOT, 'datasets', 'uhdm', 'pairs')


def human(b):
    for u in ('B', 'KB', 'MB', 'GB'):
        if b < 1024:
            return '%.1f %s' % (b, u)
        b /= 1024.0
    return '%.1f TB' % b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=DEFAULT_SRC, help='源目录（含 XXXX_gt.jpg / XXXX_moire.jpg）')
    ap.add_argument('--out', default=os.path.join(ROOT, 'datasets', 'uhdm'),
                    help='数据集根目录，会创建 train/ 与 eval/ 两个子目录')
    ap.add_argument('--n-train', type=int, default=400, help='训练集图数')
    ap.add_argument('--copy', action='store_true',
                    help='用复制代替硬链接（源与目标不在同一卷时必需）')
    ap.add_argument('--force', action='store_true', help='清空已存在的输出目录')
    a = ap.parse_args()

    if not os.path.isdir(a.src):
        print('源目录不存在: %s' % a.src)
        return 1

    gts = sorted(f for f in os.listdir(a.src) if f.endswith('_gt.jpg'))
    if not gts:
        print('源目录里没有 *_gt.jpg: %s' % a.src)
        return 1

    train_dir = os.path.join(a.out, 'train')
    eval_dir = os.path.join(a.out, 'eval')

    if a.force:
        for d in (train_dir, eval_dir):
            if os.path.isdir(d):
                shutil.rmtree(d)
    for d in (train_dir, eval_dir):
        os.makedirs(d, exist_ok=True)

    n_train = min(a.n_train, len(gts))
    print('源     : %s  (%d 对)' % (a.src, len(gts)))
    print('训练集 : %s  (%d 对)' % (train_dir, n_train))
    print('评估集 : %s  (%d 对)' % (eval_dir, len(gts) - n_train))
    print('方式   : %s' % ('复制' if a.copy else '硬链接'))
    print()

    made = skipped = 0
    total = 0
    for i, gt in enumerate(gts):
        num = gt[:4]
        dst_dir = train_dir if i < n_train else eval_dir
        for suf in ('_gt.jpg', '_moire.jpg'):
            src = os.path.join(a.src, num + suf)
            dst = os.path.join(dst_dir, num + suf)
            if not os.path.exists(src):
                print('  [警告] 缺少配对文件: %s' % src)
                continue
            if os.path.exists(dst):
                skipped += 1
                continue
            if a.copy:
                shutil.copy2(src, dst)
            else:
                try:
                    os.link(src, dst)
                except OSError:
                    # 跨卷或文件系统不支持硬链接 -> 退回复制
                    shutil.copy2(src, dst)
            made += 1
            total += os.path.getsize(dst)

    print('新建 %d 个文件，跳过 %d 个（已存在）' % (made, skipped))
    if made:
        print('新增占用: %s%s' % (human(total),
                                '（硬链接，实际不占额外磁盘）' if not a.copy else ''))
    print()
    print('划分完成。数据已就位，配置里只需写数据集名字：')
    print('  DATA:')
    print('    DATASET: %s' % os.path.basename(os.path.normpath(a.out)))
    print('（训练：python src/train.py --config uhdm_4k_s）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
