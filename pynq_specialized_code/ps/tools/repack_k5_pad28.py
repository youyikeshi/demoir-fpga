#!/usr/bin/env python3
"""把已有部署包里的 k=5 权重重打包成"28 字节容器"（不需要 PyTorch）。

与 `repack_k3_pad12.py` 完全同构（2026-10-23 第五轮，同一招换个 k）：

  全网络只有 `encoder.conv_first` 是 5×5（5.40 s，占 conv 10%），每个 (oc,ic)
  25 字节 —— 25 不是 4 的倍数 ⇒ 装载只能按 1 字节/笔访问（`ARLEN=1`，
  板端 16.3 拍/字节）。把 25 个抽头紧排在前 25 字节、再补 3 个 0，凑成
  **28 字节 = 7 个 32-bit 字**：

      offset(oc, ic, ky, kx) = (oc*cin + ic)*28 + ky*5 + kx

  恒 4 字节对齐 ⇒ 装载一拍读一个字、事务数 ÷25；写侧仍是 25 个编译期常量
  bank，计算路径一个门都不动。代价只是体积 ×28/25。

本工具只做**容器补齐**：每个 (oc,ic) 的 25 个权重原样搬到新位置、后面补 3 个 0。
所以每个张量的数值、每个标度、ref_out.bin 全都**不变** —— 这是本轮最重要的
一条自检：

    **重打包后 ref_out.bin 必须逐字节不变**（用 check_pack_ref.py 复算整网）。

用法：
    python repack_k5_pad28.py                          # 就地改 ../deploy/weights_pack
    python repack_k5_pad28.py --pack <包> --out <新包>
    python repack_k5_pad28.py --pack <包> --check-only  # 只自检，不写盘
    python repack_k5_pad28.py --pack <新包> --revert --out <旧包>   # 回退到 25 字节容器

自检（不通过就 exit 1，绝不写盘）：
  1) 逐层 `k5_from_pad28(k5_to_pad28(w))` 与原权重逐字节相同（往返无损）；
  2) 非 k=5 层的权重一个字节都不动（**含 k=3 的 12 字节容器层**，本工具不碰）；
     bias 一个字节都不动；
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

from ref_numpy import (k5_to_pad28, k5_from_pad28,      # noqa: E402
                       W_LAYOUT_K5_PAD28, W_LAYOUT_OCIC, layout_of, K5_PAD)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default=os.path.abspath(
        os.path.join(HERE, "..", "..", "deploy", "weights_pack")))
    ap.add_argument("--out", default=None, help="输出目录（默认就地覆盖 --pack）")
    ap.add_argument("--check-only", action="store_true", help="只自检不写盘")
    ap.add_argument("--revert", action="store_true",
                    help="反向：28 字节容器 → 25 字节紧排（回退到 K5_PAD28=0 的 bit）")
    a = ap.parse_args()

    pack = os.path.abspath(a.pack)
    out = os.path.abspath(a.out) if a.out else pack
    with open(os.path.join(pack, "index.json"), encoding="utf-8") as f:
        index = json.load(f)
    blob = open(os.path.join(pack, "weights.bin"), "rb").read()
    layers = index["layers"]

    # 按旧偏移排序：重写 blob 时保持同样的先后顺序（仍然 4 字节对齐）
    order = sorted(layers.items(), key=lambda kv: kv[1]["w_off"])
    old_of = {key: (e["w_off"], e["w_len"], e["b_off"], e["b_len"],
                    list(e["w_shape"]), layout_of(e))
              for key, e in order}
    new_blob = bytearray()
    n_k5 = n_other = 0
    for key, e in order:
        cin = e["w_shape"][1]
        cout = e["w_shape"][0]
        cur = layout_of(e)
        w = np.frombuffer(blob, np.int8, e["w_len"], e["w_off"])
        b = np.frombuffer(blob, np.int32, e["b_len"] // 4, e["b_off"])
        # 只有 w_shape 的第 3/4 维都是 5（旧容器）或第 3 维是 28（新容器）才是 k=5
        if len(e["w_shape"]) == 4:
            k = e["w_shape"][2] if e["w_shape"][2] == e["w_shape"][3] else None
        else:
            k = 5 if (len(e["w_shape"]) == 3 and e["w_shape"][2] == K5_PAD) else None
        if k == 5 and not a.revert:
            n_k5 += 1
            if cur == W_LAYOUT_K5_PAD28:
                w_out = w.reshape(cout, cin, K5_PAD)
                wg = k5_from_pad28(w_out)
            else:
                wg = w.reshape(cout, cin, 5, 5)
                w_out = k5_to_pad28(wg)
            if not np.array_equal(k5_from_pad28(k5_to_pad28(wg)), wg):
                raise SystemExit("%s: 28 字节容器往返不一致（bug）" % key)
            w_len = int(w_out.size)
            w_shape = [cout, cin, K5_PAD]
            w_layout = W_LAYOUT_K5_PAD28
            data = np.ascontiguousarray(w_out).tobytes()
        elif k == 5 and a.revert:
            n_k5 += 1
            if cur != W_LAYOUT_K5_PAD28:
                raise SystemExit("%s 已经是 25 字节容器，--revert 无从下手" % key)
            wg = k5_from_pad28(w.reshape(cout, cin, K5_PAD))
            w_len = int(wg.size)
            w_shape = [cout, cin, 5, 5]
            w_layout = W_LAYOUT_OCIC
            data = np.ascontiguousarray(wg).tobytes()
        else:
            # 非 k=5 层（含 k=3 的 12 字节容器）原样搬运，字节不动
            n_other += 1
            w_len = int(w.size)
            w_shape = list(e["w_shape"])
            w_layout = cur
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
        cout, cin = e["w_shape"][0], e["w_shape"][1]
        new = np.frombuffer(nb, np.int8, e["w_len"], e["w_off"])
        o_off, o_len, o_boff, o_blen, o_shape, o_lay = old_of[key]
        old = np.frombuffer(blob, np.int8, o_len, o_off)
        is_k5 = (len(o_shape) == 4 and o_shape[2] == 5) or \
                (len(o_shape) == 3 and o_shape[2] == K5_PAD)
        if is_k5:
            got = (k5_from_pad28(new.reshape(cout, cin, K5_PAD))
                   if len(e["w_shape"]) == 3
                   else new.reshape(cout, cin, 5, 5))
            old_got = (k5_from_pad28(old.reshape(cout, cin, K5_PAD))
                       if o_lay == W_LAYOUT_K5_PAD28
                       else old.reshape(cout, cin, 5, 5))
            if not np.array_equal(old_got, got):
                raise SystemExit("%s: k=5 数值与旧包不一致（bug）" % key)
        else:
            if not np.array_equal(old.reshape(-1), new.reshape(-1)):
                raise SystemExit("%s: 权重字节与旧包不一致（bug）" % key)
        newb = np.frombuffer(nb, np.int32, e["b_len"] // 4, e["b_off"])
        oldb = np.frombuffer(blob, np.int32, o_blen // 4, o_boff)
        if not np.array_equal(newb, oldb):
            raise SystemExit("%s: bias 与旧包不一致（bug）" % key)

    index["blob_size"] = len(new_blob)
    print("k=5 层 %d 个，其它层 %d 个；blob %d → %d 字节（+%.2f%%）"
          % (n_k5, n_other, len(blob), len(new_blob),
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
        info["k5_pad28_repack"] = {
            "date": "2026-10-23",
            "reason": "k=5 权重容器改成 28 字节/(oc,ic)（25 个抽头 + 3 个零），"
                      "装载可 32-bit 宽读（事务数 ÷25）；数值/标度/ref_out.bin 不变",
            "tool": "ps/tools/repack_k5_pad28.py",
            "reverted": bool(a.revert),
            "n_k5_layers": n_k5,
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
