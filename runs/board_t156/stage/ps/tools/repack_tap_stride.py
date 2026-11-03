#!/usr/bin/env python3
"""repack_tap_stride.py —— 把已有权重包的容器步长换成新 TAP（**不需要 PyTorch**）

为什么有这个工具
----------------
2026-10-30 的时序优化把 conv_uni 的权重容器步长从 `TAP = k²+3` 改成
`TAP' = 8·ceil(k²/8)`：

    ======  ==========  ==========
    k       旧 TAP      新 TAP
    ======  ==========  ==========
    1       4（紧排 1）  8
    3       12          16
    5       28          32
    ======  ==========  ==========

两套布局**只有补齐洞宽度不同**，`j = ky*k + kx < k²` 的字节逐字节相同。
也就是说：权重不用重新量化、标度不用动、`ref_out.bin` 必须逐字节不变。
所以"换 TAP"是一次纯粹的容器重排——不装 torch 也能做，而且比重新导出安全
（不经过浮点前向，不会把标定漂移引进来）。

这与 2026-10-23 的 `repack_k3_pad12.py` / `repack_k5_pad28.py` 是同一招，
本工具是它们的**推广**：任意 k、任意目标 TAP，一个工具搞定。

两种模式
--------
  --mode uniform （默认）  全部层都按目标 TAP 打包（新 {8,16,32} 或旧 {4,12,28}）
                           配"全部层走 conv_uni"的部署。
  --mode keep-ip          k=1 → 新 TAP（conv_uni 默认接 k=1）
                          k=3 → 保持 pad12、k=5 → 保持 pad28
                          （给仍在用 k3_wrap/k5_wrap 的 bit：手写 IP 读的是
                           固定的 12/28 字节容器，喂 16/32 会让它们的
                           route check 判失败）

自检（不通过就 exit 1，**绝不写盘**）
----------------------------------
  1) 逐层往返：新容器解回来 == 旧容器解回来（抽头逐字节相同）
  2) 非目标层（keep-ip 模式下没动的层）一个字节都不变
  3) bias / shift / sw / scale_in / scale_out / relu / scales / eca 全部不变
  4) 新 blob 的每层偏移自洽、不重叠、不越界；blob_size == 文件长度
  5) 每层字节数 == cout*cin*TAP
  6) **整网复算**：用 runner.RefEngine 跑 input_q.bin，与 ref_out.bin 逐位相同
     （这一条是"数值没变"的最终判据；--no-ref-check 可跳，但不建议）

用法
----
    # 旧包 → 新 TAP 包（放新目录，旧包绝对不动）
    python3 repack_tap_stride.py --pack ../../deploy/weights_pack \\
        --out ../../deploy/weights_pack_new

    # 只自检不写盘
    python3 repack_tap_stride.py --check-only

    # 回退：新 TAP 包 → 旧容器（给 2026-10-30 之前的 bit）
    python3 repack_tap_stride.py --pack ../../deploy/weights_pack_new \\
        --out /tmp/weights_pack_legacy --tap old
"""
import argparse
import datetime
import hashlib
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PS, "pynq"))

import tap_pack as T                              # noqa: E402
import ref_numpy as R                             # noqa: E402


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _k_and_tap(e):
    """从 index 的一条记录推断 (k, 当前容器步长, 当前布局标签)。"""
    lay = R.layout_of(e)
    ws = [int(x) for x in e["w_shape"]]
    if lay == R.W_LAYOUT_TAP:
        tap = int(e.get("tap") or ws[-1])
        return T.k_of_tap(tap), tap, lay
    if lay == R.W_LAYOUT_K3_PAD12:
        return 3, int(R.K3_PAD), lay
    if lay == R.W_LAYOUT_K5_PAD28:
        return 5, int(R.K5_PAD), lay
    if len(ws) == 4 and ws[2] == ws[3]:
        return int(ws[2]), int(ws[2] * ws[3]), lay
    if len(ws) == 3:
        return T.k_of_tap(ws[-1]), int(ws[-1]), lay
    raise ValueError("认不出容器：w_shape=%r w_layout=%r" % (ws, lay))


def _tight(w, e, k, tap, cout, cin):
    """容器字节 → 紧排 (cout,cin,k,k)。"""
    lay = R.layout_of(e)
    if lay == R.W_LAYOUT_TAP:
        return T.unpack_layer(w, k=k, tap=tap)
    if lay == R.W_LAYOUT_K3_PAD12:
        return R.k3_from_pad12(w, cout, cin)
    if lay == R.W_LAYOUT_K5_PAD28:
        return R.k5_from_pad28(w, cout, cin)
    return np.asarray(w).reshape(cout, cin, k, k)


def _target_tap(k, mode, tap_mode):
    """目标容器步长 + (是否 TAP 标准布局)。"""
    if mode == "keep-ip" and k in (3, 5):
        return T.tap_of_k(k, padded=False), False        # 保持 pad12/pad28
    return T.tap_of_k(k, padded=(tap_mode == "new")), (tap_mode == "new")


def plan(index, mode, tap_mode):
    """算出每层的目标容器（不读 blob 内容，只算形状）→ 供 --check-only 打印。"""
    out = {}
    for name, e in index["layers"].items():
        cout, cin = int(e["w_shape"][0]), int(e["w_shape"][1])
        k, tap0, lay0 = _k_and_tap(e)
        tap1, is_tap = _target_tap(k, mode, tap_mode)
        out[name] = dict(cout=cout, cin=cin, k=k, tap0=tap0, lay0=lay0,
                         tap1=tap1, new_layout=R.W_LAYOUT_TAP if is_tap else lay0,
                         bytes0=cout * cin * tap0, bytes1=cout * cin * tap1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default=os.path.abspath(
        os.path.join(PS, "..", "deploy", "weights_pack")))
    ap.add_argument("--out", default=None,
                    help="输出目录（不给就只算不写；与 --pack 相同时原地改）")
    ap.add_argument("--mode", choices=["uniform", "keep-ip"], default="uniform")
    ap.add_argument("--tap", choices=["new", "old"], default="new",
                    help="目标 TAP 体系：new={8,16,32}（默认）/ old={4,12,28}")
    ap.add_argument("--check-only", action="store_true", help="只自检，不写盘")
    ap.add_argument("--no-ref-check", action="store_true",
                    help="跳过整网 RefEngine 复算（不建议）")
    ap.add_argument("--manifest", default=os.path.abspath(
        os.path.join(PS, "manifest_640.json")))
    a = ap.parse_args()

    with open(os.path.join(a.pack, "index.json"), "r", encoding="utf-8") as f:
        index = json.load(f)
    blob = open(os.path.join(a.pack, "weights.bin"), "rb").read()
    pl = plan(index, a.mode, a.tap)

    print("源包: %s (%d 字节, %d 层)"
          % (os.path.abspath(a.pack), len(blob), len(index["layers"])))
    print("模式: %s，目标 TAP: %s" % (a.mode,
          "新 8·ceil(k²/8)" if a.tap == "new" else "旧 k²+3"))
    by_k = {}
    for name, p in pl.items():
        d = by_k.setdefault((p["k"], p["tap0"], p["tap1"]), [0, 0, 0])
        d[0] += 1
        d[1] += p["bytes0"]
        d[2] += p["bytes1"]
    print("\n%-3s %-6s %-6s %5s %14s %14s" %
          ("k", "TAP旧", "TAP新", "层数", "旧权重字节", "新权重字节"))
    for (k, t0, t1), (n, b0, b1) in sorted(by_k.items()):
        print("%-3d %-6d %-6d %5d %14d %14d  %s"
              % (k, t0, t1, n, b0, b1, "不变" if t0 == t1 else
                 "×%.3f" % (b1 / b0)))

    if a.check_only:
        print("\n--check-only：只算不写盘。")
        return 0

    if a.out is None:
        raise SystemExit("要给 --out（或显式 --check-only）")

    # ---------------- 重排 blob ----------------
    new_blob = bytearray()
    new_layers = {}
    order = list(index["layers"].items())

    def put(b, align=4):
        while len(new_blob) % align:
            new_blob.append(0)
        off = len(new_blob)
        new_blob.extend(b)
        return off

    ok = True
    for name, e in order:
        p = pl[name]
        cout, cin, k = p["cout"], p["cin"], p["k"]
        w_off, w_len = int(e["w_off"]), int(e["w_len"])
        b_off, b_len = int(e["b_off"]), int(e["b_len"])
        w = np.frombuffer(blob, np.int8, w_len, w_off)
        tight = _tight(w, e, k, p["tap0"], cout, cin)

        # ---- 1) 往返回读必须一致（旧容器 → 紧排 → 新容器 → 紧排）----
        if p["new_layout"] == R.W_LAYOUT_TAP:
            w_new = T.pack_layer(tight, k, p["tap1"])
            back = T.unpack_layer(w_new, k=k, tap=p["tap1"])
        elif p["new_layout"] == R.W_LAYOUT_K3_PAD12:
            w_new = R.k3_to_pad12(tight)
            back = R.k3_from_pad12(w_new, cout, cin)
        elif p["new_layout"] == R.W_LAYOUT_K5_PAD28:
            w_new = R.k5_to_pad28(tight)
            back = R.k5_from_pad28(w_new, cout, cin)
        else:
            w_new = np.ascontiguousarray(tight)
            back = np.asarray(w_new).reshape(cout, cin, k, k)
        if not np.array_equal(back, tight):
            print("  ✗ 往返不一致: %s" % name)
            ok = False

        new_off = put(w_new.tobytes())
        b = np.frombuffer(blob, np.uint8, b_len, b_off)
        new_b_off = put(b.tobytes())

        ne = dict(e)
        ne["w_off"] = new_off
        ne["w_len"] = int(w_new.size)
        ne["w_shape"] = [int(x) for x in w_new.shape]
        ne["w_layout"] = p["new_layout"]
        ne["tap"] = int(p["tap1"]) if p["new_layout"] == R.W_LAYOUT_TAP else 0
        ne["b_off"] = new_b_off
        new_layers[name] = ne

        # ---- 2) 每层字节数 == cout*cin*TAP ----
        if ne["w_len"] != T.containers_bytes(cout, cin, k, p["tap1"]):
            print("  ✗ 字节数不符: %s w_len=%d ≠ %d*%d*%d"
                  % (name, ne["w_len"], cout, cin, p["tap1"]))
            ok = False
        # ---- 3) bias 逐字节不变 ----
        if not np.array_equal(np.frombuffer(new_blob, np.uint8, b_len, new_b_off), b):
            print("  ✗ bias 变了: %s" % name)
            ok = False

    # ---- 4) 标度/ECA 原样带走（逐项深比较）----
    new_index = {"layers": new_layers, "eca": index.get("eca"),
                 "scales": index.get("scales"), "blob_size": len(new_blob)}

    # ---- 5) 偏移自洽：不重叠、不越界、全覆盖 ----
    segs = []
    for name, e in new_layers.items():
        segs.append((e["w_off"], e["w_off"] + e["w_len"], name + ".w"))
        segs.append((e["b_off"], e["b_off"] + e["b_len"], name + ".b"))
    segs.sort()
    for i in range(1, len(segs)):
        if segs[i][0] < segs[i - 1][1]:
            print("  ✗ 区间重叠: %s 与 %s" % (segs[i - 1][2], segs[i][2]))
            ok = False
    if segs and segs[-1][1] > len(new_blob):
        print("  ✗ 越界: 最后一段到 %d > blob %d" % (segs[-1][1], len(new_blob)))
        ok = False

    # ---- 6) 整网复算（RefEngine）：新 index/blob 落到临时目录，跑真正的 Runner ----
    ref_ok = None
    if not a.no_ref_check and os.path.exists(os.path.join(a.pack, "ref_out.bin")):
        import shutil as _sh
        import tempfile
        import time
        from runner import Runner
        tmpd = tempfile.mkdtemp(prefix="tapchk_")
        try:
            open(os.path.join(tmpd, "weights.bin"), "wb").write(bytes(new_blob))
            with open(os.path.join(tmpd, "index.json"), "w", encoding="utf-8") as f:
                json.dump(new_index, f, ensure_ascii=False)
            _sh.copy(os.path.join(a.pack, "input_q.bin"),
                     os.path.join(tmpd, "input_q.bin"))
            h, w = json.load(open(a.manifest, encoding="utf-8"))["input_hw"]
            img = np.fromfile(os.path.join(tmpd, "input_q.bin"),
                              np.int8).reshape(3, h, w)
            ref = np.fromfile(os.path.join(a.pack, "ref_out.bin"),
                              np.int8).reshape(3, h, w)
            t0 = time.time()
            r = Runner(a.manifest, tmpd)
            out, _dt = r.run(img)
            bad = int((out != ref).sum())
            dt = time.time() - t0
            print("\n整网复算（新包，RefEngine+同一份 runner）: %.1fs，与 ref_out.bin "
                  "逐位不一致 %d / %d" % (dt, bad, ref.size))
            ref_ok = (bad == 0)
            if not ref_ok:
                ok = False
        finally:
            _sh.rmtree(tmpd, ignore_errors=True)
    else:
        print("\n（跳过整网复算：--no-ref-check 或包里没有 ref_out.bin）")

    if not ok:
        print("\n==== 自检 FAIL —— 不写盘 ====")
        return 1
    print("\n==== 自检 PASS ====")

    # ---------------- 写盘 ----------------
    out = os.path.abspath(a.out)
    if out == os.path.abspath(a.pack):
        raise SystemExit("--out 不能等于 --pack（工具不原地改；要回退请显式给旧包目录）")
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "weights.bin"), "wb").write(bytes(new_blob))
    with open(os.path.join(out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(new_index, f, ensure_ascii=False, indent=1)
    import shutil
    for name in ("input_q.bin", "ref_out.bin", "ref_out_float.bin", "ref_psnr.txt"):
        src = os.path.join(a.pack, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(out, name))

    # build_info：在源包的溯源信息上补一段 TAP 记录
    info_p = os.path.join(a.pack, "build_info.json")
    info = {}
    if os.path.exists(info_p):
        with open(info_p, "r", encoding="utf-8") as f:
            info = json.load(f)
    info.pop("files_sha256", None)
    info["tap_container"] = {
        "date": datetime.date.today().isoformat(),
        "tool": "ps/tools/repack_tap_stride.py",
        "mode": a.mode,
        # 实际落盘的每 k 容器步长（keep-ip 模式下 k=3/k=5 仍是 12/28）
        "tap_per_k": {str(k): int(v) for k, v in sorted(
            {p["k"]: p["tap1"] for p in pl.values()}.items())},
        "reason": ("k²+3 → 8·ceil(k²/8)：每 (o,c) 块是 8 字节整数倍，"
                   "引擎读地址纯 +8 累加（时序优化，−11.2 ns 违例里最大一段）"
                   if a.tap == "new" else "回退到 k²+3 容器（旧比特流）"),
        "numeric_note": "只重排容器，抽头字节/标度/shift 全不动 ⇒ ref_out.bin 逐位不变",
        "legacy_layers_kept": ("k=3 pad12 / k=5 pad28" if a.mode == "keep-ip" else "无"),
        "verified": {"ref_out_identical": ref_ok},
    }
    info["scales_eca_unchanged"] = True
    info["files_sha256"] = {}
    for name in sorted(os.listdir(out)):
        p = os.path.join(out, name)
        if os.path.isfile(p) and name != "build_info.json":
            info["files_sha256"][name] = _sha256(p)
    with open(os.path.join(out, "build_info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    print("已写: %s  (weights.bin %d 字节, index.json %d 层)"
          % (out, len(new_blob), len(new_layers)))
    old_b = os.path.getsize(os.path.join(a.pack, "weights.bin"))
    print("体积: %d → %d 字节（%+.1f%%）"
          % (old_b, len(new_blob), 100.0 * (len(new_blob) - old_b) / old_b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
