#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 xwd 抓屏文件转成 PNG（不依赖 ImageMagick / netpbm）
用法: xwd -root -silent > root.xwd && xwd2png.py root.xwd out.png
"""
import struct
import sys
import zlib


def read_xwd(path):
    d = open(path, "rb").read()
    f = struct.unpack(">25I", d[:100])
    (hdr_size, _ver, _fmt, depth, w, h, _xo, byte_order, _bu, _bbo, _pad,
     bpp, bpl, _vclass, rmask, gmask, bmask, _bpr, _cmap, ncolors,
     _ww, _wh, _wx, _wy, _bw) = f
    off = hdr_size + ncolors * 12
    px = d[off:off + bpl * h]
    return dict(w=w, h=h, bpp=bpp, bpl=bpl, byte_order=byte_order,
                rmask=rmask, gmask=gmask, bmask=bmask, px=px)


def mask_shift(m):
    if not m:
        return 0, 0
    s = 0
    while not (m >> s) & 1:
        s += 1
    n = 0
    while (m >> (s + n)) & 1:
        n += 1
    return s, n


def to_rgb(info):
    w, h, bpp, bpl = info["w"], info["h"], info["bpp"], info["bpl"]
    px = info["px"]
    rs, rn = mask_shift(info["rmask"])
    gs, gn = mask_shift(info["gmask"])
    bs, bn = mask_shift(info["bmask"])

    def scale(v, nbits):
        if nbits == 0:
            return 0
        if nbits == 8:
            return v
        return int(round(v * 255.0 / ((1 << nbits) - 1)))

    out = bytearray(w * h * 3)
    nbytes = bpp // 8
    little = (info["byte_order"] == 0)
    for y in range(h):
        row = y * bpl
        o = y * w * 3
        for x in range(w):
            p = row + x * nbytes
            if nbytes == 4:
                v = (px[p] | px[p+1] << 8 | px[p+2] << 16 | px[p+3] << 24) if little \
                    else (px[p] << 24 | px[p+1] << 16 | px[p+2] << 8 | px[p+3])
            elif nbytes == 3:
                v = (px[p] | px[p+1] << 8 | px[p+2] << 16) if little \
                    else (px[p] << 16 | px[p+1] << 8 | px[p+2])
            elif nbytes == 2:
                v = (px[p] | px[p+1] << 8) if little else (px[p] << 8 | px[p+1])
            else:
                v = px[p]
            out[o] = scale((v & info["rmask"]) >> rs, rn)
            out[o+1] = scale((v & info["gmask"]) >> gs, gn)
            out[o+2] = scale((v & info["bmask"]) >> bs, bn)
            o += 3
    return w, h, bytes(out)


def write_png(path, w, h, rgb):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgb[y * w * 3:(y + 1) * w * 3]

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    open(path, "wb").write(png)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "root.xwd"
    dst = sys.argv[2] if len(sys.argv) > 2 else "root.png"
    info = read_xwd(src)
    w, h, rgb = to_rgb(info)
    write_png(dst, w, h, rgb)
    print("转换完成 %s -> %s (%dx%d, bpp=%d)" % (src, dst, w, h, info["bpp"]))


if __name__ == "__main__":
    main()
