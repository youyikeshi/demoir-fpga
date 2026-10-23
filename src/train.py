"""训练入口。

用法
----
    python src/train.py --config uhdm_4k_s              # 用配置里的默认参数
    python src/train.py --config smoke --epochs 1        # 冒烟测试
    python src/train.py --config uhdm_4k_s --epochs 100 --crop-size 384
    python src/train.py --config uhdm_4k_s --dataset mydata      # 换数据集
    python src/train.py --config uhdm_4k_s --no-teacher          # 关掉蒸馏
    python src/train.py --config uhdm_4k_s --dry-run             # 只看解析结果

这个脚本本身不训练，它做三件事：
  1. 把配置里的**逻辑名**（数据集名、teacher 名）解析成**绝对路径**；
  2. 写一份快照 `runs/<run>/config.yaml`（全绝对路径）+ `run_info.yaml`（可读记录）；
  3. 用子进程调用 `vendor/PDNet/train.py`，日志同时落 `runs/<run>/train.log`。

于是：配置里没有任何相对路径，**从任何目录运行都一样**。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths          # noqa: E402
import vendor_boot    # noqa: E402


def build_overrides(a):
    """命令行 -> 配置键。只放用户真正指定了的项（其余保持配置原值）。"""
    ov = {}
    if a.epochs is not None:
        ov['EPOCHS'] = a.epochs
    if a.crop_size is not None:
        ov['CROP_SIZE'] = a.crop_size
    if a.batch_size is not None:
        ov['BATCH_SIZE'] = a.batch_size
    if a.loader is not None:
        ov['LOADER'] = a.loader
    if a.save_epoch is not None:
        ov['SAVE_EPOCH'] = a.save_epoch
    if a.save_iter is not None:
        ov['SAVE_ITER'] = a.save_iter
    if a.lr is not None:
        ov['BASE_LR'] = a.lr
    if a.load_epoch is not None:
        ov['LOAD_EPOCH'] = a.load_epoch
    if a.seed is not None:
        ov['SEED'] = a.seed
    if a.workers is not None:
        ov['WORKER'] = a.workers
    if a.gpu is not None:
        ov['GPU_ID'] = a.gpu
    if a.no_teacher:
        ov['USE_TEACHER'] = False
    if a.dw_groups is not None:
        ov['DW_GROUPS'] = a.dw_groups
    return ov


def print_list():
    print('可用配置（src/configs/）：')
    for f in sorted(os.listdir(paths.CONFIGS)):
        if f.endswith('.yaml'):
            print('  %s' % f[:-5])
    print()
    print('已注册权重（weights/weights.yaml）：')
    reg = vendor_boot.load_weights_registry()
    for role in ('teacher', 'student'):
        names = sorted((reg.get(role) or {}).keys())
        print('  %-8s %s' % (role, ', '.join(names) or '(无)'))
    print()
    print('已有运行目录（runs/）：')
    if os.path.isdir(paths.RUNS):
        for r in sorted(os.listdir(paths.RUNS)):
            if os.path.isdir(paths.run_dir(r)):
                ck = vendor_boot.latest_checkpoint(r)
                print('  %-28s %s' % (r, '有 checkpoint_latest' if ck else '（无 checkpoint）'))
    else:
        print('  (无)')


def main():
    ap = argparse.ArgumentParser(
        description='训练去摩尔纹 student（在 vendor/PDNet 的上游代码上包一层）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('用法\n----\n')[1] if '用法\n----\n' in __doc__ else None)

    ap.add_argument('--config', default='uhdm_4k_s', help='src/configs/ 下的名字，或一个 yaml 路径')
    ap.add_argument('--name', default=None, help='运行目录名（runs/<name>）；默认自动生成')
    ap.add_argument('--list', action='store_true', help='列出可用配置/权重/运行目录后退出')
    ap.add_argument('--dry-run', action='store_true', help='只解析并打印，不真正开始训练')

    g = ap.add_argument_group('数据集（覆盖配置里的 DATASET）')
    g.add_argument('--dataset', default=None, help='datasets/ 下的名字')
    g.add_argument('--dataset-root', default=None, help='含 train/ 与 eval/ 的任意目录')
    g.add_argument('--train-dataset', default=None, help='直接指定训练集路径')
    g.add_argument('--eval-dataset', default=None, help='直接指定评估集路径')

    g = ap.add_argument_group('模型与训练（覆盖配置里的对应键）')
    g.add_argument('--teacher', default=None, help='weights.yaml 里的 teacher 名')
    g.add_argument('--no-teacher', action='store_true', help='关掉蒸馏，只训 student')
    g.add_argument('--epochs', type=int, default=None)
    g.add_argument('--load-epoch', type=int, default=None,
                   help='续训起点；必须配合 --name 指向原来的运行目录')
    g.add_argument('--crop-size', type=int, default=None, help='必须是 32 的倍数')
    g.add_argument('--batch-size', type=int, default=None)
    g.add_argument('--loader', default=None, choices=['crop', 'resize', 'default'])
    g.add_argument('--save-epoch', type=int, default=None, help='存盘间隔，调小可降低中断代价')
    g.add_argument('--save-iter', type=int, default=None)
    g.add_argument('--lr', type=float, default=None, help='BASE_LR')
    g.add_argument('--seed', type=int, default=None)
    g.add_argument('--workers', type=int, default=None)
    g.add_argument('--gpu', type=int, default=None, help='GPU_ID')
    g.add_argument('--dw-groups', type=int, default=None, choices=[0, 1],
                   help='GhostBlock.cheap_operation: 0=稠密3x3(默认) / 1=depthwise(GhostNet 原设计)')

    a = ap.parse_args()

    if a.list:
        print_list()
        return 0

    snapshot, meta = vendor_boot.resolve_config(
        a.config, run=a.name, overrides=build_overrides(a),
        dataset=a.dataset, dataset_root=a.dataset_root,
        train_dataset=a.train_dataset, test_dataset=a.eval_dataset,
        teacher=a.teacher)

    # 早失败、早报清楚：这两类问题在上游代码里报错很难懂
    vendor_boot.check_dataset(meta['train_dataset'], '训练集')
    vendor_boot.check_dataset(meta['test_dataset'], '评估集')

    loader = meta['loader']
    crop = meta['crop_size']
    if loader == 'crop' and crop and crop % 32:
        raise SystemExit('CROP_SIZE=%d 不是 32 的倍数。'
                         '网络有 2 次 pixel_unshuffle + 4 级下采样，必须是 32 的倍数。' % crop)

    use_teacher = meta['use_teacher'] is not False

    paths.make_dirs(*paths.run_subdirs(meta['run']).values())

    print('=' * 74)
    print('运行目录 : runs/%s' % meta['run'])
    print('配置来源 : %s' % meta['source_config'])
    print('数据集   : %s' % (meta['dataset'] or '(直接指定路径)'))
    print('  训练集 : %s' % paths.pretty(meta['train_dataset']))
    print('  评估集 : %s' % paths.pretty(meta['test_dataset']))
    print('模型     : %s, crop=%s, batch 见 config.yaml' % (loader, crop))
    print('蒸馏     : %s' % ('ON  (%s)' % meta['teacher'] if use_teacher else 'OFF（只训 student）'))
    _dw = int(meta.get('dw_groups', 0) or 0)
    print('GhostBlock: %s' % ('dw_groups=1（depthwise，GhostNet 原设计）' if _dw
                             else 'dw_groups=0（稠密 3x3，历史行为）'))
    print('epochs   : %s' % meta['epochs'])
    print('快照     : runs/%s/config.yaml  +  run_info.yaml' % meta['run'])
    print('=' * 74)
    print()

    if use_teacher:
        import yaml
        with open(snapshot, 'r', encoding='utf-8') as f:
            snap = yaml.load(f, Loader=yaml.FullLoader)
        tp = (snap.get('TEACHER_MODEL') or {}).get('T_load_path')
        if not (tp and os.path.exists(tp)):
            raise SystemExit(
                'teacher 权重不存在：%s\n'
                '  从移动硬盘还原：python tools/stage_hd_transfer.py --mode restore\n'
                '  或把配置里的 USE_TEACHER 设为 False / 加 --no-teacher 只训 student' % tp)

    if a.dry_run:
        print('--dry-run：只解析，不训练。快照已写入 runs/%s/' % meta['run'])
        return 0

    log = os.path.join(paths.run_dir(meta['run']), 'train.log')
    return vendor_boot.run_vendor('train.py', snapshot, log_path=log)


if __name__ == '__main__':
    sys.exit(main())
