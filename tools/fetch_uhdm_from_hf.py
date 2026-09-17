"""通过 HTTP Range 远程读取 hf-mirror 上的 UHDM 大包，只抽取需要的文件。

为什么需要这个脚本
------------------
UHDM 官方只提供 Google Drive 入口（国内直连被墙），国内镜像 OpenDataLab 是
80GB 单文件整包且需要登录。本脚本走 hf-mirror（国内直连，实测 ~5.4 MB/s），
对 44.8GB 的 `uhdm.zip` 做**远程随机读取**：只下载 ZIP 中央目录 + 需要的那几个
条目，避免为了 5.7GB 测试集而下满 45GB。

原理：ZIP 的中央目录在文件末尾，HTTP Range 允许任意偏移读取，
因此可以用 zipfile 直接操作一个"按需 range 请求"的文件对象。

用法
----
    python tools/fetch_uhdm_from_hf.py --list                 # 只列包内结构
    python tools/fetch_uhdm_from_hf.py --list --filter test   # 只看 test 相关条目
    python tools/fetch_uhdm_from_hf.py --extract-test         # 抽取 500 对测试集
"""
import argparse
import io
import os
import re
import sys
import time
import urllib.request
import zipfile

URL = 'https://hf-mirror.com/datasets/leegwang/uhdm/resolve/main/uhdm.zip'
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CHUNK = 4 * 1024 * 1024          # 每次 range 请求的粒度
UA = {'User-Agent': 'Mozilla/5.0'}


class HttpRangeFile(io.RawIOBase):
    """一个可 seek 的只读文件对象，底层用 HTTP Range 按需取数。

    zipfile 需要 seek/tell，所以我们维护一个 CHUNK 大小的窗口缓存，
    让顺序读取不产生新请求。
    """

    def __init__(self, url, debug=False):
        self.url = url
        self.debug = debug
        self.pos = 0
        self.buf_start = 0
        self.buf = b''
        self.n_requests = 0
        self.n_bytes = 0
        req = urllib.request.Request(url, headers={**UA, 'Range': 'bytes=0-0'})
        with urllib.request.urlopen(req, timeout=60) as r:
            cr = r.headers.get('Content-Range')
            if not cr or '/' not in cr:
                raise RuntimeError('服务器不支持 Range，无法远程抽取')
            self.size = int(cr.split('/')[-1])

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        elif whence == io.SEEK_END:
            self.pos = self.size + offset
        return self.pos

    def _fill(self, start):
        if self.buf_start <= start < self.buf_start + len(self.buf):
            return
        end = min(start + CHUNK, self.size) - 1
        req = urllib.request.Request(
            self.url, headers={**UA, 'Range': 'bytes=%d-%d' % (start, end)})
        with urllib.request.urlopen(req, timeout=120) as r:
            self.buf = r.read()
        self.buf_start = start
        self.n_requests += 1
        self.n_bytes += len(self.buf)

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        if n == 0 or self.pos >= self.size:
            return b''
        n = min(n, self.size - self.pos)
        out = bytearray()
        while len(out) < n:
            self._fill(self.pos + len(out))
            off = (self.pos + len(out)) - self.buf_start
            take = min(n - len(out), len(self.buf) - off)
            if take <= 0:
                break
            out += self.buf[off:off + take]
        self.pos += len(out)
        return bytes(out)

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


def human(n):
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return '%.2f %s' % (n, u)
        n /= 1024.0
    return '%.2f PB' % n


def open_remote(debug=False):
    print('打开远程包: %s' % URL)
    f = HttpRangeFile(URL, debug=debug)
    print('包大小    : %s (%d bytes)' % (human(f.size), f.size))
    t0 = time.time()
    zf = zipfile.ZipFile(f)
    n = len(zf.infolist())
    print('条目数    : %d（读取中央目录耗时 %.1fs，累计 range 请求 %d 次 / %s）'
          % (n, time.time() - t0, f.n_requests, human(f.n_bytes)))
    return f, zf


def cmd_list(args):
    _, zf = open_remote()
    infos = zf.infolist()
    if args.filter:
        infos = [i for i in infos if args.filter.lower() in i.filename.lower()]
    print()
    print('=== 目录层级统计 ===')
    roots = {}
    for i in infos:
        parts = i.filename.split('/')
        key = '/'.join(parts[:2]) if len(parts) > 1 else parts[0]
        r = roots.setdefault(key, [0, 0])
        r[0] += 1
        r[1] += i.file_size
    for k in sorted(roots)[:40]:
        print('  %-42s %5d 个文件, 解压后 %s' % (k, roots[k][0], human(roots[k][1])))
    if len(roots) > 40:
        print('  ... 共 %d 个分组' % len(roots))
    print()
    print('=== 前 20 个条目 ===')
    for i in infos[:20]:
        print('  %12d  %12d  %s' % (i.compress_size, i.file_size, i.filename))
    return 0


GT_RE = re.compile(r'^(?P<num>\d{4})_(?P<kind>gt|moire)\.jpg$', re.IGNORECASE)


def fetch_entry(url, info, retries=4):
    """用一个精确的 Range 请求取回某个 ZIP 条目的内容并解压。

    只下该条目真正占用的字节（外加最多 1KB 的局部头），不像窗口式读取那样
    会顺带多为每个文件多拉几 MB。
    """
    import struct
    import zlib
    # 局部文件头 30 字节；文件名/扩展区长度未知，所以稍微多读一点
    start = info.header_offset
    end = start + 30 + 1024 + info.compress_size
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={**UA, 'Range': 'bytes=%d-%d' % (start, end - 1)})
            with urllib.request.urlopen(req, timeout=180) as r:
                raw = r.read()
            break
        except Exception as e:            # noqa: BLE001 - 网络抖动重试
            last = e
            time.sleep(1.5 * (attempt + 1))
    else:
        raise RuntimeError('取回 %s 失败: %s' % (info.filename, last))

    if len(raw) < 30:
        raise RuntimeError('局部头不完整: %s' % info.filename)
    nlen, mlen = struct.unpack_from('<HH', raw, 26)
    data_off = 30 + nlen + mlen
    comp = raw[data_off:data_off + info.compress_size]
    if len(comp) != info.compress_size:
        raise RuntimeError('压缩数据长度不符 %s: %d != %d'
                           % (info.filename, len(comp), info.compress_size))
    if info.compress_type == zipfile.ZIP_STORED:
        out = comp
    elif info.compress_type == zipfile.ZIP_DEFLATED:
        out = zlib.decompress(comp, -15)
    else:
        raise RuntimeError('不支持的压缩方式 %d: %s'
                           % (info.compress_type, info.filename))
    if len(out) != info.file_size:
        raise RuntimeError('解压大小不符 %s: %d != %d'
                           % (info.filename, len(out), info.file_size))
    return out, zlib.crc32(out) & 0xFFFFFFFF, len(raw)


def cmd_extract_subset(args):
    """按原目录结构抽取一批条目（用于 train 集，它按 pair_NN/ 分子目录）。

    train 集的文件名在每个 pair_NN 目录内从 0000 重新编号，所以**必须保留目录结构**，
    不能像 test 集那样摊平。load_data.py 用 os.walk 递归扫描，子目录可正常工作。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out_dir = args.out or os.path.join(ROOT, 'uhdm_data', 'Train')
    os.makedirs(out_dir, exist_ok=True)
    f, zf = open_remote()
    infos = zf.infolist()

    prefix = args.path_filter
    strip = args.strip_prefix or prefix
    sel = [i for i in infos
           if i.filename.startswith(prefix)
           and i.filename.lower().endswith(('.jpg', '.jpeg', '.png'))]
    sel.sort(key=lambda i: i.filename)
    if args.limit:
        sel = sel[:args.limit]
    total = sum(i.file_size for i in sel)
    print()
    print('选中条目  : %d 个文件, 解压后 %s' % (len(sel), human(total)))
    print('输出根目录: %s' % out_dir)

    tasks = []
    skipped = 0
    for i in sel:
        rel = i.filename[len(strip):] if i.filename.startswith(strip) else i.filename
        dst = os.path.join(out_dir, rel.replace('/', os.sep))
        if os.path.exists(dst) and os.path.getsize(dst) == i.file_size:
            skipped += 1
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tasks.append((i, dst))
    print('待下载    : %d 个文件, 已存在跳过 %d 个, 并发 %d'
          % (len(tasks), skipped, args.workers))
    print()

    done = bad_crc = failed = 0
    got = 0
    errors = []
    t0 = time.time()

    def work(t):
        info, dst = t
        data, crc, fetched = fetch_entry(URL, info)
        tmp = dst + '.part'
        with open(tmp, 'wb') as fo:
            fo.write(data)
        os.replace(tmp, dst)
        return info.filename, info.CRC, crc, fetched

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, t) for t in tasks]
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                name, exp_crc, crc, fetched = fut.result()
                got += fetched
                if exp_crc != crc:
                    bad_crc += 1
                    errors.append('%s CRC32 不符' % name)
                else:
                    done += 1
            except Exception as e:                              # noqa: BLE001
                failed += 1
                errors.append(str(e)[:120])
            if n % 250 == 0 or n == len(tasks):
                el = max(time.time() - t0, 1e-6)
                print('  [%d/%d] 成功 %d, CRC 错 %d, 失败 %d, 用时 %.0fs (%.2f MB/s)'
                      % (n, len(tasks), done, bad_crc, failed, el,
                         got / 1024 / 1024 / el), flush=True)

    print()
    print('完成: 新写 %d, 跳过 %d, CRC 不符 %d, 失败 %d' % (done, skipped, bad_crc, failed))
    print('实际下载流量: %s' % human(got))
    if errors:
        print('问题清单（前 10）:')
        for e in errors[:10]:
            print('  - %s' % e)
        return 1
    return 0


def cmd_extract_test(args):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out_dir = args.out or os.path.join(ROOT, 'uhdm_data', 'test')
    os.makedirs(out_dir, exist_ok=True)
    f, zf = open_remote()
    infos = zf.infolist()

    # 找出所有 XXXX_gt.jpg / XXXX_moire.jpg 条目，按编号配对
    pairs = {}
    for i in infos:
        base = i.filename.split('/')[-1]
        m = GT_RE.match(base)
        if not m:
            continue
        # 只取 test 相关的路径（若包内有 train/test 之分，优先 test）
        if args.path_filter and args.path_filter.lower() not in i.filename.lower():
            continue
        pairs.setdefault(m.group('num'), {})[m.group('kind').lower()] = i

    complete = [n for n, d in pairs.items() if 'gt' in d and 'moire' in d]
    complete.sort()
    print()
    print('配对数    : %d 个完整配对（共发现 %d 个编号）' % (len(complete), len(pairs)))
    if not complete:
        print('未找到 XXXX_gt.jpg / XXXX_moire.jpg 命名，请先 --list 查看结构')
        return 1
    if args.limit:
        complete = complete[:args.limit]
    total = sum(pairs[n][k].file_size for n in complete for k in ('gt', 'moire'))
    print('本次抽取  : %d 对, 解压后合计 %s' % (len(complete), human(total)))
    print('输出到    : %s' % out_dir)
    print()

    # 组装任务：每个文件一个精确 range 请求，CRC32 校验后落盘
    tasks = []
    skipped = 0
    for num in complete:
        for kind in ('gt', 'moire'):
            info = pairs[num][kind]
            dst = os.path.join(out_dir, '%s_%s.jpg' % (num, kind))
            if os.path.exists(dst) and os.path.getsize(dst) == info.file_size:
                skipped += 1
                continue
            tasks.append((num, kind, info, dst))
    print('待下载    : %d 个文件, 已存在跳过 %d 个, 并发 %d'
          % (len(tasks), skipped, args.workers))
    print()

    done = bad_crc = failed = 0
    got_bytes = 0
    errors = []
    t0 = time.time()
    lock_msg = []

    def work(t):
        num, kind, info, dst = t
        data, crc, fetched = fetch_entry(URL, info)
        tmp = dst + '.part'
        with open(tmp, 'wb') as fo:
            fo.write(data)
        os.replace(tmp, dst)
        return num, kind, info.CRC, crc, fetched

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, t): t for t in tasks}
        for i, fut in enumerate(as_completed(futs), 1):
            t = futs[fut]
            try:
                num, kind, exp_crc, crc, fetched = fut.result()
                got_bytes += fetched
                if exp_crc != crc:
                    bad_crc += 1
                    errors.append('%s_%s CRC32 %08x != %08x' % (num, kind, crc, exp_crc))
                else:
                    done += 1
            except Exception as e:                      # noqa: BLE001
                failed += 1
                errors.append('%s_%s %s' % (t[0], t[1], e))
            if i % 50 == 0 or i == len(tasks):
                el = time.time() - t0
                print('  [%d/%d] 成功 %d, CRC 错 %d, 失败 %d, 用时 %.0fs (%.2f MB/s)'
                      % (i, len(tasks), done, bad_crc, failed, el,
                         got_bytes / 1024 / 1024 / max(el, 1e-6)), flush=True)

    print()
    print('完成: 新写 %d 个文件（CRC32 全部校验通过）, 跳过 %d, CRC 不符 %d, 失败 %d'
          % (done, skipped, bad_crc, failed))
    print('实际下载流量: %s（含每文件最多 1KB 的局部头开销）' % human(got_bytes))
    print('（若下满整包需 44.8GB / 12 分钟以上，本次只取了测试集所需部分）')
    if errors:
        print()
        print('问题清单:')
        for e in errors[:20]:
            print('  - %s' % e)
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description='从 hf-mirror 远程抽取 UHDM 数据集')
    ap.add_argument('--list', action='store_true', help='只列包内结构')
    ap.add_argument('--filter', default='', help='--list 时的子串过滤')
    ap.add_argument('--extract-test', action='store_true', help='抽取 XXXX_gt/moire.jpg 配对')
    ap.add_argument('--extract-subset', action='store_true',
                    help='按原目录结构抽取（用于 train 集的 pair_NN/ 结构）')
    ap.add_argument('--strip-prefix', default='',
                    help='--extract-subset 时从条目路径中去掉的前缀（默认同 --path-filter）')
    ap.add_argument('--path-filter', default='', help='只取路径含该子串的条目（如 test）')
    ap.add_argument('--out', default='', help='输出目录，默认 uhdm_data/test')
    ap.add_argument('--limit', type=int, default=0, help='只取前 N 对（调试用）')
    ap.add_argument('--workers', type=int, default=8, help='并发下载线程数（默认 8）')
    a = ap.parse_args()
    if a.list:
        return cmd_list(a)
    if a.extract_test:
        return cmd_extract_test(a)
    if a.extract_subset:
        return cmd_extract_subset(a)
    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
