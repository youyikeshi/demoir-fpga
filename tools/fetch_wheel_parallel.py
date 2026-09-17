"""多连接并行下载大 wheel 文件（应对官方 CDN 不可用 / 单连接镜像过慢）。

背景（实测于本机）
------------------
- `download.pytorch.org` 的索引页可访问，但实际 .whl 下载会挂死 → 文档里的
  `pip install torch --index-url https://download.pytorch.org/whl/cu118` 无法完成
- 国内 pytorch-wheels 镜像里只有 aliyun 与 sjtu 托管 cu118 Windows wheel；
  单连接约 0.2~0.3 MB/s，8 连接后 sjtu 聚合可达 5.3 MB/s（aliyun 仅 2.1）

做法：把文件切成 N 段，每段一个 Range 请求并发下载，用 f.seek 写进预分配文件。

用法
----
    python fetch_wheel_parallel.py <url> <输出路径> [--workers 16]
"""
import argparse
import os
import socket
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = {'User-Agent': 'pip/26'}
CHUNK = 1024 * 1024


def probe(url):
    req = urllib.request.Request(url, headers={**UA, 'Range': 'bytes=0-0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        cr = r.headers.get('Content-Range')
        if not cr or '/' not in cr:
            raise RuntimeError('不支持 Range: %s' % url)
        return int(cr.split('/')[-1])


def fetch_segment(url, start, end, buf, fh, lock, counter):
    """下载 [start, end] 闭区间到 buf，然后按偏移写入 fh。"""
    attempts = 0
    while True:
        attempts += 1
        try:
            req = urllib.request.Request(
                url, headers={**UA, 'Range': 'bytes=%d-%d' % (start, end)})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if len(data) != end - start + 1:
                raise RuntimeError('段长度不符 %d != %d' % (len(data), end - start + 1))
            with lock:
                fh.seek(start)
                fh.write(data)
                counter[0] += len(data)
            return len(data)
        except Exception as e:                                  # noqa: BLE001
            if attempts >= 5:
                raise RuntimeError('段 %d-%d 失败: %s' % (start, end, e))
            time.sleep(1.5 * attempts)


def download(url, dst, workers=16):
    size = probe(url)
    print('文件大小: %d bytes (%.2f MB)' % (size, size / 1024 / 1024))
    # 分段：每段约 size/workers/4，避免段太碎导致请求数过多
    seg = max(4 * 1024 * 1024, size // (workers * 4))
    ranges = [(s, min(s + seg - 1, size - 1)) for s in range(0, size, seg)]
    print('切分    : %d 段 x ~%.1f MB, 并发 %d' % (len(ranges), seg / 1024 / 1024, workers))

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    lock = threading.Lock()
    counter = [0]
    t0 = time.time()
    with open(dst, 'wb') as fh:
        fh.truncate(size)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(fetch_segment, url, s, e, None, fh, lock, counter)
                    for s, e in ranges]
            done = 0
            for fut in as_completed(futs):
                fut.result()
                done += 1
                if done % 20 == 0 or done == len(futs):
                    el = max(time.time() - t0, 1e-6)
                    print('  [%d/%d] %.0f MB / %.0f MB, %.2f MB/s, 已用 %.0fs'
                          % (done, len(futs), counter[0] / 1024 / 1024, size / 1024 / 1024,
                             counter[0] / 1024 / 1024 / el, el), flush=True)
    got = os.path.getsize(dst)
    el = time.time() - t0
    print('完成    : %d bytes, 用时 %.0fs (平均 %.2f MB/s)' % (got, el, got / 1024 / 1024 / max(el, 1e-6)))
    if got != size:
        print('!! 大小不符，期望 %d 实际 %d' % (size, got))
        return 1
    # 用 zipfile 自检（wheel 就是 zip），能发现截断/错位
    import zipfile
    try:
        with zipfile.ZipFile(dst) as z:
            bad = z.testzip()
        print('zip 完整性: %s' % ('OK' if bad is None else '损坏于 %s' % bad))
        return 0 if bad is None else 1
    except Exception as e:                                      # noqa: BLE001
        print('zip 校验失败: %s' % e)
        return 1


def main():
    ap = argparse.ArgumentParser(description='多连接并行下载 wheel')
    ap.add_argument('url')
    ap.add_argument('dst')
    ap.add_argument('--workers', type=int, default=16)
    a = ap.parse_args()
    socket.setdefaulttimeout(120)
    return download(a.url, a.dst, a.workers)


if __name__ == '__main__':
    sys.exit(main())
