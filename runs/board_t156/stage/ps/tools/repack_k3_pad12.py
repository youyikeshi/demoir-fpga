#!/usr/bin/env python3
"""把已有部署包里的 k=3 权重重打包成"12 字节容器"（不需要 PyTorch）。

为什么需要这个工具：
  `export_network.py` 已经会按新容器导出（k=3 → 每 (oc,ic) 12 字节 = 9 个抽头
  + 3 个零），但那要重跑 torch 浮点前向 + 标定，产出的**标度会变**（标定图集合
  一变就变），于是 `ref_out.bin` 也变，就没法与"已交付的板上 md5 97f5a094…"
  逐字节对拍了。

  本工具只做**容器补齐**：每个 (oc,ic) 的 9 个权重原样搬到新位置、后面补 3 个 0。
  所以每个张量的数值、每个标度、ref_out.bin 全都**不变** —— 这就是本轮最重要
  的一条自检：

      **重打包后 ref_out.bin 必须逐字节不变**（用 check_pack_ref.py 复算整网）。

  代价只是 blob 体积：k=3 的权重 ×4/3（1.58 MB → 1.70 MB 量级）。

用法：
    python repack_k3_pad12.py                          # 就地改 ../deploy/weights_pack
    python repack_k3_pad12.py --pack <包> --out <新包>
    python repack_k3_pad12.py --pack <包> --check-only  # 只自检，不写盘
    python repack_k3_pad12.py --pack <新包> --revert --out <旧包>   # 回退到 9 字节容器

自检（不通过就 exit 1，绝不写盘）：
  1) 逐层 `k3_from_pad12(k3_to_pad12(w))` 与原权重逐字节相同（往返无损）；
  2) 非 k=3 层的权重一个字节都不动；bias 一个字节都不动；
  3) 所有标度 / shift / eca 逐项不变；
  4) blob 里的新旧偏移都自洽（每个张量都能按新偏移读回来）。
"""
import argparse
import hashlib
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pynq"))

from ref_numpy import (k3_to_pad12, k3_from_pad12,      # noqa: E402
                       W_LAYOUT_K3_PAD12, W_LAYOUT_OCIC, layout_of, K3_PAD)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def w_key_of(entry):
    """(cout,cin,k) —— 从 w_shape 推。旧容器 [cout,cin,k,k]；新容器 [cout,cin,12]。"""
    sh = entry["w_shape"]
    if len(sh) == 4:
        return sh[0], sh[1], sh[2]
    if len(sh) == 3 and sh[2] == K3_PAD:
        return sh[0], sh[1], 3
    raise ValueError("看不懂的 w_shape: %r" % (sh,))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default=os.path.abspath(
        os.path.join(HERE, "..", "..", "deploy", "weights_pack")))
    ap.add_argument("--out", default=None, help="输出目录（默认就地覆盖 --pack）")
    ap.add_argument("--check-only", action="store_true", help="只自检不写盘")
    ap.add_argument("--revert", action="store_true",
                    help="反向：12 字节容器 → 9 字节紧排（回退到 K3_PAD12=0 的 bit）")
    a = ap.parse_args()

    pack = os.path.abspath(a.pack)
    out = os.path.abspath(a.out) if a.out else pack
    with open(os.path.join(pack, "index.json"), encoding="utf-8") as f:
        index = json.load(f)
    blob = open(os.path.join(pack, "weights.bin"), "rb").read()
    layers = index["layers"]

    # 按旧偏移排序：重写 blob 时保持同样的先后顺序（仍然 4 字节对齐）
    order = sorted(layers.items(), key=lambda kv: kv[1]["w_off"])
    # 改写前先把每层的旧偏移存下来（后面逐位对账要用）
    old_of = {key: (e["w_off"], e["w_len"], e["b_off"], e["b_len"],
                    list(e["w_shape"]), layout_of(e))
              for key, e in order}
    new_blob = bytearray()
    n_k3 = n_other = 0
    for key, e in order:
        cout, cin, k = w_key_of(e)
        cur = layout_of(e)
        w = np.frombuffer(blob, np.int8, e["w_len"], e["w_off"])
        b = np.frombuffer(blob, np.int32, e["b_len"] // 4, e["b_off"])
        if k == 3 and not a.revert:
            n_k3 += 1
            if cur == W_LAYOUT_K3_PAD12:
                w_out = w.reshape(cout, cin, K3_PAD)
                wg = k3_from_pad12(w_out)
            else:
                wg = w.reshape(cout, cin, 3, 3)
                w_out = k3_to_pad12(wg)
            if not np.array_equal(k3_from_pad12(k3_to_pad12(wg)), wg):
                raise SystemExit("%s: 12 字节容器往返不一致（bug）" % key)
            w_len = int(w_out.size)
            w_shape = [cout, cin, K3_PAD]
            w_layout = W_LAYOUT_K3_PAD12
            data = np.ascontiguousarray(w_out).tobytes()
        elif k == 3 and a.revert:
            n_k3 += 1
            if cur != W_LAYOUT_K3_PAD12:
                raise SystemExit("%s 已经是 9 字节容器，--revert 无从下手" % key)
            wg = k3_from_pad12(w.reshape(cout, cin, K3_PAD))
            w_len = int(wg.size)
            w_shape = [cout, cin, 3, 3]
            w_layout = W_LAYOUT_OCIC
            data = np.ascontiguousarray(wg).tobytes()
        else:
            n_other += 1
            w_len = int(w.size)
            w_shape = list(e["w_shape"])
            w_layout = W_LAYOUT_OCIC
            data = w.tobytes()
        while len(new_blob) % 4:
            new_blob.append(0)
        e["w_off"] = len(new_blob)
        e["w_len"] = w_len
        e["w_shape"] = w_shape
        e["w_layout"] = w_layout
        new_blob.extend(data)
        while len(new_blob) % 4:
            new_blob.append(0)
        e["b_off"] = len(new_blob)
        new_blob.extend(b.astype("<i4").tobytes())

    # ---- 自检：新 blob 里每个张量都能按新偏移读回，且**数值与旧包逐位相同** ----
    nb = bytes(new_blob)
    for key, e in layers.items():
        cout, cin, k = w_key_of(e)
        new = np.frombuffer(nb, np.int8, e["w_len"], e["w_off"])
        if k == 3 and not a.revert:
            got = k3_from_pad12(new.reshape(cout, cin, K3_PAD))
        elif k == 3 and a.revert:
            got = new.reshape(cout, cin, 3, 3)
        else:
            got = new.reshape(e["w_shape"])
        o_off, o_len, o_boff, o_blen, o_shape, o_lay = old_of[key]
        old = np.frombuffer(blob, np.int8, o_len, o_off)
        if k == 3:
            # 旧包可能是 9 字节紧排，也可能已经是 12 字节容器（本工具可重入）
            if o_lay == W_LAYOUT_K3_PAD12:
                old_got = k3_from_pad12(old.reshape(cout, cin, K3_PAD))
            else:
                old_got = old.reshape(cout, cin, 3, 3)
            if not np.array_equal(old_got, got):
                raise SystemExit("%s: k=3 数值与旧包不一致（bug）" % key)
        else:
            if not np.array_equal(old.reshape(-1), new.reshape(-1)):
                raise SystemExit("%s: 权重字节与旧包不一致（bug）" % key)
        newb = np.frombuffer(nb, np.int32, e["b_len"] // 4, e["b_off"])
        oldb = np.frombuffer(blob, np.int32, o_blen // 4, o_boff)
        if not np.array_equal(newb, oldb):
            raise SystemExit("%s: bias 与旧包不一致（bug）" % key)

    index["blob_size"] = len(new_blob)
    print("k=3 层 %d 个，其它层 %d 个；blob %d → %d 字节（+%.1f%%）"
          % (n_k3, n_other, len(blob), len(new_blob),
             100.0 * (len(new_blob) - len(blob)) / max(1, len(blob))))
    if a.check_only:
        print("--check-only：未写盘")
        return 0

    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "weights.bin"), "wb") as f:
        f.write(nb)
    with open(os.path.join(out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    for name in ("input_q.bin", "ref_out.bin", "ref_out_float.bin", "ref_psnr.txt"):
        src = os.path.join(pack, name)
        if os.path.exists(src) and os.path.abspath(src) != os.path.join(out, name):
            with open(src, "rb") as f:
                data = f.read()
            with open(os.path.join(out, name), "wb") as f:
                f.write(data)
    info_p = os.path.join(out, "build_info.json")
    if os.path.exists(info_p):
        with open(info_p, encoding="utf-8") as f:
            info = json.load(f)
        info["k3_pad12_repack"] = {
            "date": "2026-10-23",
            "reason": "k=3 权重容器改成 12 字节/(oc,ic)（9 个抽头 + 3 个零），"
                      "装载可 32-bit 宽读（事务数 ÷9）；数值/标度/ref_out.bin 不变",
            "tool": "ps/tools/repack_k3_pad12.py",
            "reverted": bool(a.revert),
            "n_k3_layers": n_k3,
        }
        for name in ("weights.bin", "index.json"):
            p = os.path.join(out, name)
            if os.path.exists(p):
                info.setdefault("files_sha256", {})[name] = _sha256(p)
        with open(info_p, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    print("已写出：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
