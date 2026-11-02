#!/usr/bin/env python3
# v101_pipe：before vs after 的核级写事务逐条比较（等价性判据）
#   - 写事务条数必须相同
#   - 地址序列必须逐条相同
#   - 每条写事务的拍号差必须恒为同一常数（只允许整体平移）
#   - 相邻写事务间隔（= 每 pixel 的 term 拍数）多重集合必须完全相同
import sys, os, collections

HERE = os.path.dirname(os.path.abspath(__file__))
SHAPES = ["v94_c48_o64_32x32","v94_c64_o32_8x8","v94_c64_o48_8x8","v94_c64_o64_8x8",
          "v94_c96_o96_8x8","v94_c48_o24_8x8","v94_c48_o40_8x8","v94_c48_o16_8x8",
          "v94_c48_o8_8x8","bin_3x96_64_8x8"]

def load(path):
    cyc, addr = [], []
    if not os.path.exists(path):
        return None, None
    for ln in open(path):
        p = ln.split()
        if len(p) < 2 or not p[1].startswith("addr="):
            continue
        cyc.append(int(p[0])); addr.append(p[1])
    return cyc, addr

def main(v):
    ok_all = True
    print(f"== VLANES={v}: before vs after 核级写事务逐条比较 ==")
    tot = 0
    for sh in SHAPES:
        b = load(os.path.join(HERE, f"v{v}_before", f"w101_{sh}_v{v}_before.txt"))
        a = load(os.path.join(HERE, f"v{v}_after",  f"w101_{sh}_v{v}_after.txt"))
        if b[0] is None or a[0] is None:
            print(f"  {sh:22s} MISSING (run not finished)"); ok_all = False; continue
        # 结果字节数从 run 日志读
        res = {}
        for w in ("before","after"):
            lp = os.path.join(HERE, f"v{v}_{w}", f"run_{sh}_v{v}_{w}.log")
            res[w] = "?"
            if os.path.exists(lp):
                for ln in open(lp, errors="ignore"):
                    if "逐字节一致" in ln:
                        res[w] = ln.strip().split("[TB]")[-1].strip(); break
        nb, na = len(b[0]), len(a[0])
        same_addr = (b[1] == a[1])
        d = [y-x for x,y in zip(b[0], a[0])] if nb == na else []
        dset = sorted(set(d))
        ib = collections.Counter(y-x for x,y in zip(b[0], b[0][1:]))
        ia = collections.Counter(y-x for x,y in zip(a[0], a[0][1:]))
        same_iv = (ib == ia)
        ok = (nb == na) and same_addr and len(dset) == 1 and same_iv
        ok_all &= ok
        tot += na
        print(f"  {sh:22s} 写事务 {nb:5d}/{na:5d}  addr {'同' if same_addr else '不同'}"
              f"  Δ拍={dset}  间隔分布 {'同' if same_iv else '不同'}  "
              f"结果 before[{res['before']}] after[{res['after']}]  {'OK' if ok else 'FAIL'}")
    print(f"-- VLANES={v} 合计写事务 {tot}；总判定 {'PASS' if ok_all else 'FAIL'} --")
    return 0 if ok_all else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "8"))
