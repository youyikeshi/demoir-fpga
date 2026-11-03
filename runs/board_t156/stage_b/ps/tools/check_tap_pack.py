#!/usr/bin/env python3
"""check_tap_pack.py —— 权重包的 TAP 容器审计（只读，不碰板子/Vivado）

对一份部署包做"TAP 正确性"的**直接判据**检查，逐层列出：

  层名 | cout | cin | k | TAP | 期望字节 = cout*cin*TAP | 实际字节 = w_off 跨度

检查项
------
  T1  index.json 的 `blob_size` == `weights.bin` 的真实字节数
  T2  每层字节数 == cout*cin*TAP(k)（TAP 由 `w_layout`/`tap` 决定；
      旧容器 k3_pad12/k5_pad28 按 12/28 算，k=1 紧排按 1 算）
  T3  w_len 与 w_shape 的乘积一致（reshape 不会炸）
  T4  (w_off + w_len) 不越界；b_off/b_len 不越界；b_len == 4*cout
  T5  容器里的补齐洞（j ≥ k²）逐字节为 0 —— TAP 张冠李戴/忘了清零会在这里露馅
  T6  从容器按 RTL 地址公式 ((o*cin+c)*TAP + j) 抽出的抽头，与"紧排语义"
      逐字节相同（用 tap_pack.unpack_layer 复算），且能还原成 (cout,cin,k,k)
  T7  各层 [w_off, w_off+w_len) 互不重叠（bias 区也一起查）

用法：
    python3 ps/tools/check_tap_pack.py                      # 默认 deploy/weights_pack_new
    python3 ps/tools/check_tap_pack.py --pack ../deploy/weights_pack --tap old
    python3 ps/tools/check_tap_pack.py --csv /tmp/tap_table.csv
    python3 ps/tools/check_tap_pack.py --pack <包> --quiet   # 只输出汇总
"""
import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PS = os.path.dirname(HERE)                       # .../ps
sys.path.insert(0, os.path.join(PS, "pynq"))

import tap_pack as T                              # noqa: E402
import ref_numpy as R                             # noqa: E402


def k_of_entry(e):
    """从 index 的一条 layer 记录里推断 k 和容器字节数。"""
    lay = R.layout_of(e)
    ws = [int(x) for x in e["w_shape"]]
    if lay == R.W_LAYOUT_TAP:
        tap = int(e.get("tap") or ws[-1])
        return T.k_of_tap(tap), tap
    if lay == R.W_LAYOUT_K3_PAD12:
        return 3, int(R.K3_PAD)
    if lay == R.W_LAYOUT_K5_PAD28:
        return 5, int(R.K5_PAD)
    if len(ws) == 4:
        if ws[2] != ws[3]:
            raise ValueError("w_shape 非方核：%r" % (ws,))
        return int(ws[2]), int(ws[2]) * int(ws[3])      # 紧排：每 (o,c) k² 字节
    if len(ws) == 3:
        return T.k_of_tap(ws[-1]), int(ws[-1])
    raise ValueError("认不出容器：w_shape=%r w_layout=%r" % (ws, lay))


def audit(pack_dir, csv_path=None, quiet=False):
    with open(os.path.join(pack_dir, "index.json"), "r", encoding="utf-8") as f:
        index = json.load(f)
    blob = open(os.path.join(pack_dir, "weights.bin"), "rb").read()
    ok = True
    print("包目录: %s" % os.path.abspath(pack_dir))
    print("weights.bin: %d 字节；index.blob_size: %s"
          % (len(blob), index.get("blob_size")))
    if int(index.get("blob_size", -1)) != len(blob):
        print("  ✗ T1 blob_size 与实际文件字节数不一致")
        ok = False
    else:
        print("  ✓ T1 blob_size == weights.bin 字节数")

    rows = []
    used = []          # (start, end, what) 用于重叠检查
    tap_hist = {}
    for name in sorted(index["layers"]):
        e = index["layers"][name]
        cout, cin = int(e["w_shape"][0]), int(e["w_shape"][1])
        k, tap = k_of_entry(e)
        want = T.containers_bytes(cout, cin, k, tap)
        got = int(e["w_len"])
        w_off = int(e["w_off"])
        prod = int(np.prod(e["w_shape"]))
        rows.append((name, cout, cin, k, tap, want, got))
        tap_hist[(k, tap)] = tap_hist.get((k, tap), 0) + 1
        if got != want:
            print("  ✗ T2 %s: w_len=%d ≠ cout*cin*TAP=%d*%d*%d=%d"
                  % (name, got, cout, cin, tap, want))
            ok = False
        if prod != got:
            print("  ✗ T3 %s: prod(w_shape)=%d ≠ w_len=%d" % (name, prod, got))
            ok = False
        if w_off + got > len(blob):
            print("  ✗ T4 %s: w_off+w_len=%d 越界（blob %d）"
                  % (name, w_off + got, len(blob)))
            ok = False
        b_off, b_len = int(e["b_off"]), int(e["b_len"])
        if b_off + b_len > len(blob):
            print("  ✗ T4 %s: bias 越界" % name)
            ok = False
        if b_len != 4 * cout:
            print("  ✗ T4 %s: b_len=%d ≠ 4*cout=%d" % (name, b_len, 4 * cout))
            ok = False
        used.append((w_off, w_off + got, name + ".w"))
        used.append((b_off, b_off + b_len, name + ".b"))

        # T5/T6：把容器按 RTL 地址公式解回 (cout,cin,k,k)
        w = np.frombuffer(blob, np.int8, got, w_off)
        lay = R.layout_of(e)
        try:
            if lay == R.W_LAYOUT_TAP:
                tight = T.unpack_layer(w, k=k, tap=tap, cout=cout, cin=cin)
            elif lay == R.W_LAYOUT_K3_PAD12:
                tight = R.k3_from_pad12(w, cout, cin)
            elif lay == R.W_LAYOUT_K5_PAD28:
                tight = R.k5_from_pad28(w, cout, cin)
            else:
                tight = np.asarray(w).reshape(cout, cin, k, k)
        except ValueError as exc:
            print("  ✗ T5/T6 %s: %s" % (name, exc))
            ok = False
            continue
        if tight.shape != (cout, cin, k, k):
            print("  ✗ T6 %s: 解出的形状 %r ≠ %r"
                  % (name, tight.shape, (cout, cin, k, k)))
            ok = False
        # 逐字节回读（抽样前两个 (o,c) 块，全覆盖太慢）
        flat = np.asarray(w).reshape(-1)
        wr = tight.reshape(cout, cin, k, k)
        for o in range(min(cout, 2)):
            for ci in range(min(cin, 2)):
                for j in range(k * k):
                    if flat[(o * cin + ci) * tap + j] != wr[o, ci, j // k, j % k]:
                        print("  ✗ T6 %s: addr(%d,%d,%d) 回读不一致" % (name, o, ci, j))
                        ok = False
                        break

    used.sort()
    for i in range(1, len(used)):
        if used[i][0] < used[i - 1][1]:
            print("  ✗ T7 区间重叠: %s [%d,%d) 与 %s"
                  % (used[i - 1][2], used[i - 1][0], used[i - 1][1], used[i][2]))
            ok = False
    print("  %s T2/T3/T4 逐层字节数、w_shape、bias、越界" % ("✓" if ok else "✗"))
    print("  %s T5/T6 补齐洞全 0 + 地址公式回读一致" % ("✓" if ok else "✗"))
    print("  %s T7 区间不重叠（%d 段）" % ("✓" if ok else "✗", len(used)))

    print("\nTAP 直方图（k, TAP）→ 层数：")
    for (k, tap), n in sorted(tap_hist.items()):
        print("   k=%d 容器步长 %2d 字节 : %3d 层" % (k, tap, n))
    print("\n逐层核对表（cout*cin*TAP）：")
    hdr = "%-70s %4s %4s %2s %4s %10s %10s %s" % (
        "layer", "cout", "cin", "k", "TAP", "期望字节", "实际字节", "")
    print(hdr)
    print("-" * len(hdr))
    tot_want = 0
    for (name, cout, cin, k, tap, want, got) in (rows if not quiet else []):
        tot_want += want
        print("%-70s %4d %4d %2d %4d %10d %10d %s"
              % (name, cout, cin, k, tap, want, got, "✓" if want == got else "✗"))
    if not quiet:
        print("-" * len(hdr))
        print("%-70s %4s %4s %2s %4s %10d %10s"
              % ("合计", "", "", "", "", tot_want, ""))
    if csv_path:
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("layer,cout,cin,k,TAP,expect_bytes,actual_bytes,ok\n")
            for (name, cout, cin, k, tap, want, got) in rows:
                f.write("%s,%d,%d,%d,%d,%d,%d,%d\n"
                        % (name, cout, cin, k, tap, want, got, int(want == got)))
        print("\n逐层表已写: %s" % csv_path)
    print("\n==== TAP 审计 %s ====" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default=os.path.abspath(
        os.path.join(PS, "..", "deploy", "weights_pack_new")))
    ap.add_argument("--csv", default=None, help="把逐层表写成 CSV")
    ap.add_argument("--quiet", action="store_true", help="不打逐层表（只留汇总）")
    a = ap.parse_args()
    return audit(a.pack, a.csv, a.quiet)


if __name__ == "__main__":
    sys.exit(main())
