"""大文件暂存 / 还原：把不能走 git 的大文件收集到一个目录，用移动硬盘搬到另一台机器。

背景
----
GitHub 有单文件 100MB 限制、仓库建议 1GB 以内。本项目的 teacher 权重（50.8MB）
和 UHDM 数据集（GB 级）都不适合进 git，所以用"硬盘拷贝 + 脚本还原"的方式搬运。

目录约定
--------
    <repo>/_hd_transfer/            <- 整个拷到移动硬盘带走
        README.md
        manifest.txt                <- 清单：相对路径 / 大小 / CRC32
        payload/                    <- 镜像仓库根目录结构
            weights/teacher/ghost_34large_UHDM_ep150.tar
            weights/student/uhdm4k_s512_ep30.tar
            datasets/uhdm/...
            samples/synth/...

还原 = 把 payload/ 里的内容按原相对路径放回仓库根目录，
所以目标机器不需要改任何配置（配置里存的是绝对路径，由 src/paths.py 推导）。

用法
----
    python tools/stage_hd_transfer.py --mode stage      # 收集（默认移动，--copy 则保留原件）
    python tools/stage_hd_transfer.py --mode list       # 查看当前 payload 内容
    python tools/stage_hd_transfer.py --mode verify     # 校验 payload 与 manifest 是否一致
    python tools/stage_hd_transfer.py --mode restore    # 从 payload 还原回仓库

    # 数据集下完后再跑一次收进去（幂等，已存在的会跳过）
    python tools/stage_hd_transfer.py --mode stage
"""
import argparse
import os
import shutil
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STAGE_DIR = os.path.join(ROOT, '_hd_transfer')
PAYLOAD = os.path.join(STAGE_DIR, 'payload')
MANIFEST = os.path.join(STAGE_DIR, 'manifest.txt')

# (仓库内相对路径, 类型, 说明)
#
# ⚠️ 刻意不收录的路径：
#   datasets/uhdm/train、datasets/uhdm/eval
#     它们是 pairs/ 的**硬链接**，复制会打破硬链接、三倍占用磁盘；
#     且在目标机器上一条 `python tools/make_split.py` 即可重建。
ITEMS = [
    ('weights/teacher/ghost_34large_UHDM_ep150.tar',
     'file', 'PDNet 蒸馏训练必需的 teacher 权重（论文作者训练，官方仅在网盘提供）'),
    ('weights/student/uhdm4k_s512_ep30.tar',
     'file', '我方训练出的 student 发布版权重（30 epoch）'),
    ('runs/2026-09-10_uhdm4k_s512/net_checkpoints',
     'dir', '我方训练产物的完整 checkpoint（含中间 epoch，便于续训）'),
    ('datasets/uhdm/pairs',
     'dir', 'UHDM 测试集解压后的 500 对 4K 图（评估与重新划分的源）'),
    ('datasets/uhdm/raw',
     'dir', 'UHDM 原始压缩包。⚠️ 只为评估可删掉省 5.5GB（pairs/ 已有解压数据）'),
    ('samples/synth',
     'dir', '合成冒烟数据，可由 make_synth_data.py 以固定种子重新生成'),
]

CHUNK = 8 * 1024 * 1024

# 看起来"还在下载中"的文件后缀。碰到这些就不动目录，避免把正在写入的
# 文件搬走导致下载损坏。
PARTIAL_SUFFIXES = ('.part', '.crdownload', '.download', '.tmp', '.!ut')


def find_partial(base):
    hits = []
    for p, rel in walk_files(base):
        if rel.lower().endswith(PARTIAL_SUFFIXES):
            hits.append(rel)
    return hits


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return '%.1f %s' % (n, unit)
        n /= 1024.0
    return '%.1f PB' % n


def crc32(path):
    c = 0
    with open(path, 'rb') as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            c = zlib.crc32(b, c)
    return c & 0xFFFFFFFF


def walk_files(base):
    """返回 [(绝对路径, 相对 base 的路径)]"""
    out = []
    for home, _, files in os.walk(base):
        for f in files:
            p = os.path.join(home, f)
            out.append((p, os.path.relpath(p, base).replace('\\', '/')))
    return sorted(out, key=lambda x: x[1])


def dir_size(base):
    return sum(os.path.getsize(p) for p, _ in walk_files(base))


def iter_items():
    """逐项产出 (rel, kind, desc, abs_src, exists)"""
    for rel, kind, desc in ITEMS:
        src = os.path.join(ROOT, rel.replace('/', os.sep))
        yield rel, kind, desc, src, os.path.exists(src)


def cmd_stage(args):
    os.makedirs(PAYLOAD, exist_ok=True)
    print('暂存目录: %s' % STAGE_DIR)
    print('模式: %s' % ('复制（保留原件）' if args.copy else '移动（原件移除）'))
    print()

    anything = False
    for rel, kind, desc, src, exists in iter_items():
        dst = os.path.join(PAYLOAD, rel.replace('/', os.sep))
        if not exists:
            print('[ 跳过 ] %-58s 源不存在' % rel)
            continue
        if os.path.exists(dst):
            print('[ 跳过 ] %-58s 暂存区已存在' % rel)
            anything = True
            continue
        if kind == 'dir' and not any(f for _, f in walk_files(src)):
            print('[ 跳过 ] %-58s 源目录为空' % rel)
            continue
        if kind == 'dir' and not args.force:
            partial = find_partial(src)
            if partial:
                print('[ 跳过 ] %-58s 检测到未完成的下载：%s'
                      % (rel, ', '.join(partial[:3])))
                print('          确认下载已完成后再重跑本脚本（或加 --force 强制搬移）')
                continue

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        size = os.path.getsize(src) if kind == 'file' else dir_size(src)
        if args.copy:
            if kind == 'dir':
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            action = '复制'
        else:
            shutil.move(src, dst)
            action = '移动'
        print('[%s] %-58s %s' % (action, rel, human(size)))
        anything = True

    if not anything:
        print('没有可暂存的内容。')
        print('提示：如果 UHDM 数据集还没下完，下完后重新运行本脚本即可。')
    print()
    write_manifest()
    return 0


def write_manifest():
    files = walk_files(PAYLOAD)
    if not files:
        print('payload 为空，未生成 manifest。')
        return
    total = 0
    os.makedirs(STAGE_DIR, exist_ok=True)
    with open(MANIFEST, 'w', encoding='ascii', newline='\n') as f:
        f.write('# _hd_transfer manifest  (ASCII only, safe to read anywhere)\n')
        f.write('# format: <crc32 hex>  <size bytes>  <path relative to payload/>\n')
        f.write('# created by : python tools/stage_hd_transfer.py --mode stage\n')
        f.write('# verify with: python tools/stage_hd_transfer.py --mode verify\n')
        f.write('#\n')
        for p, rel in files:
            c = crc32(p)
            s = os.path.getsize(p)
            total += s
            f.write('%08x  %12d  %s\n' % (c, s, rel))
        f.write('# total: %d files, %s\n' % (len(files), human(total)))
    print('manifest 已写入: %s' % MANIFEST)
    print('合计: %d 个文件, %s' % (len(files), human(total)))


def cmd_list(args):
    if not os.path.isdir(PAYLOAD):
        print('payload 不存在: %s' % PAYLOAD)
        return 1
    files = walk_files(PAYLOAD)
    if not files:
        print('payload 为空。')
        return 0
    total = 0
    print('%-12s  %s' % ('大小', 'payload 内相对路径'))
    print('-' * 88)
    for p, rel in files:
        s = os.path.getsize(p)
        total += s
        print('%-12s  %s' % (human(s), rel))
    print('-' * 88)
    print('合计 %d 个文件, %s' % (len(files), human(total)))
    return 0


def cmd_verify(args):
    if not os.path.exists(MANIFEST):
        print('manifest 不存在，请先执行 --mode stage')
        return 1
    expected = {}
    with open(MANIFEST, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(None, 2)
            if len(parts) != 3:
                continue
            expected[parts[2]] = (parts[0], int(parts[1]))

    actual = {rel: (None, os.path.getsize(p)) for p, rel in walk_files(PAYLOAD)}

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    bad = []
    for rel, (exp_crc, exp_size) in sorted(expected.items()):
        if rel not in actual:
            continue
        if actual[rel][1] != exp_size:
            bad.append((rel, '大小不符: %d != %d' % (actual[rel][1], exp_size)))
            continue
        got = '%08x' % crc32(os.path.join(PAYLOAD, rel.replace('/', os.sep)))
        if got != exp_crc:
            bad.append((rel, 'CRC32 不符: %s != %s' % (got, exp_crc)))

    print('manifest 记录 %d 个文件，payload 实际 %d 个文件' % (len(expected), len(actual)))
    for rel in missing:
        print('  [缺失] %s' % rel)
    for rel in extra:
        print('  [多余] %s' % rel)
    for rel, why in bad:
        print('  [损坏] %s  (%s)' % (rel, why))
    if not (missing or extra or bad):
        print('校验通过：所有文件大小与 CRC32 均一致。')
        return 0
    return 1


def cmd_restore(args):
    if not os.path.isdir(PAYLOAD):
        print('payload 不存在: %s' % PAYLOAD)
        return 1
    print('从 %s 还原到仓库根目录 %s' % (PAYLOAD, ROOT))
    print('已存在的文件会保留（除非加 --overwrite）')
    print()
    n = 0
    for p, rel in walk_files(PAYLOAD):
        dst = os.path.join(ROOT, rel.replace('/', os.sep))
        if os.path.exists(dst) and not args.overwrite:
            print('[ 跳过 ] %s' % rel)
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(p, dst)
        print('[ 还原 ] %-58s %s' % (rel, human(os.path.getsize(p))))
        n += 1
    print()
    print('已还原 %d 个文件。' % n)
    return 0


def main():
    ap = argparse.ArgumentParser(description='大文件暂存/还原（绕过 git 体积限制）')
    ap.add_argument('--mode', choices=['stage', 'list', 'verify', 'restore'],
                    default='stage')
    ap.add_argument('--copy', action='store_true',
                    help='stage 时复制而非移动（默认移动，节省磁盘）')
    ap.add_argument('--overwrite', action='store_true',
                    help='restore 时覆盖已存在的文件')
    ap.add_argument('--force', action='store_true',
                    help='stage 时忽略"未完成下载"的检测，强制搬移目录')
    a = ap.parse_args()

    return {'stage': cmd_stage, 'list': cmd_list,
            'verify': cmd_verify, 'restore': cmd_restore}[a.mode](a)


if __name__ == '__main__':
    sys.exit(main())
