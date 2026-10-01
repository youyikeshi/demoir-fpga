"""下载并解压 UHDM 数据集（ESDNet / PDNet 官方数据）。

这三个 Google Drive id 直接取自 vendor/PDNet/scripts/download_data.sh。
支持断点续传：若下载中断，重跑本脚本即可接着下。

⚠️ 走 VPN 耗流量。国内建议优先用 `download_uhdm_opendatalab.py`，
或在训练机上下载，详见 `docs/训练/方案_数据集获取.md`。

用法：
    python tools/download_uhdm.py                 # 下全部三个包并解压
    python tools/download_uhdm.py --skip-origin   # 跳过 test_origin
    python tools/download_uhdm.py --extract-only  # 只解压已下好的包

产物路径（与 datasets/README.md 的约定一致）：
    datasets/uhdm/raw/   *.tar.gz           原始压缩包
    datasets/uhdm/pairs/ 解压后的图片
"""
import argparse
import os
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(ROOT, 'datasets', 'uhdm', 'raw')
DEFAULT_EXTRACT = os.path.join(ROOT, 'datasets', 'uhdm', 'pairs')

# name -> (google drive file id, 解压后目录名)
FILES = [
    ('train.tar.gz',       '1NiTohc65FZZXvmFpI80OTczp5Vc6EGDe', 'Train'),
    ('test.tar.gz',        '1bAZRuDq_bxZgxvnGtLwORV9pFKq2_kM_', 'test'),
    ('test_origin.tar.gz', '1SGVXv1Ce8UXL1eI3WaTmArAo6huUYgC0', 'test_origin'),
]


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return '%.1f %s' % (n, unit)
        n /= 1024.0
    return '%.1f TB' % n


def download(name, fid, out_dir):
    import gdown
    url = 'https://drive.google.com/uc?id=%s' % fid
    dest = os.path.join(out_dir, name)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print('[skip ] %s 已存在 (%s)' % (name, human(os.path.getsize(dest))))
        return dest

    print('[down ] %s <- %s' % (name, url))
    t0 = time.time()
    # resume=True 让中断后可以续传；gdown 会写 .part / 断点文件
    gdown.download(url, dest, quiet=False, resume=True)
    if not os.path.exists(dest):
        raise RuntimeError('下载失败：%s（Google Drive 可能限流，稍后重试或用浏览器手动下）' % name)
    dt = time.time() - t0
    size = os.path.getsize(dest)
    print('[done ] %s  %s  用时 %.0fs  (%.1f MB/s)'
          % (name, human(size), dt, size / 1024 / 1024 / max(dt, 1e-6)))
    return dest


def extract(path, out_dir, target_name):
    dest_dir = os.path.join(out_dir, target_name)
    if os.path.isdir(dest_dir) and os.listdir(dest_dir):
        print('[skip ] 已解压 -> %s' % dest_dir)
        return
    print('[untar] %s ...' % os.path.basename(path))
    with tarfile.open(path, 'r:gz') as tf:
        members = tf.getmembers()
        print('        %d entries' % len(members))
        if sys.version_info >= (3, 12):
            tf.extractall(out_dir, filter='data')
        else:
            tf.extractall(out_dir)
    print('[done ] -> %s' % dest_dir)


def count_images(d):
    n = 0
    for home, _, files in os.walk(d):
        n += sum(1 for f in files if f.lower().endswith(('.jpg', '.jpeg', '.png')))
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=DEFAULT_OUT, help='压缩包存放目录')
    ap.add_argument('--extract-to', default=DEFAULT_EXTRACT, help='解压目标目录')
    ap.add_argument('--skip-origin', action='store_true',
                    help='跳过 test_origin.tar.gz（它只是未配对的原始测试图，评估用不到）')
    ap.add_argument('--extract-only', action='store_true', help='只解压，不下载')
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    os.makedirs(a.extract_to, exist_ok=True)
    print('压缩包目录: %s' % os.path.abspath(a.out))
    print('解压目录  : %s' % os.path.abspath(a.extract_to))
    print()

    todo = [f for f in FILES if not (a.skip_origin and f[2] == 'test_origin')]
    for name, fid, target in todo:
        dest = os.path.join(a.out, name)
        if not a.extract_only:
            dest = download(name, fid, a.out)
        if os.path.exists(dest):
            extract(dest, a.extract_to, target)
        print()

    print('=== 结果 ===')
    for name, fid, target in FILES:
        d = os.path.join(a.extract_to, target)
        if os.path.isdir(d):
            print('%-12s %6d 张图片  %s' % (target, count_images(d), d))
        else:
            print('%-12s (未下载)' % target)

    print()
    print('下一步：把解压出来的某组图变成 train/eval 划分')
    print('  python tools/make_split.py --src %s'
          % os.path.join(a.extract_to, 'test'))
    print('  python tools/make_split.py                 # 默认就是 datasets/uhdm/pairs')


if __name__ == '__main__':
    main()
