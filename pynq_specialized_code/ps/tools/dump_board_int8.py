#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板端 int8 输出 dump —— PS↔PC 对拍（目标 1）的**板端那一半**。

用途
----
在板上对**冻结样本**跑一遍整网，把张量原样落盘，主机侧再与
`pynq_specialized_code/ps/tools/bin_fp_ref.py`（浮点 golden）逐项比对：

    A1  max|Δ(u8)| ≤ 1 LSB          A2  rel_rms ≤ 0.01
    A3  ≥3 张冻结样本               A4  变异测试（删修复 ⇒ 变红）

**本脚本只读 PL + 读内存，不碰 HDMI 寄存器**（不改 `CTRL`，不武装扫描）。

硬约束（都是踩过的坑）
--------------------
1. **同一进程只建一个 `BinNet`**：多个 net 共用惰性槽会互相抢，导致自减速且数值不稳。
   ⇒ 本脚本 `main()` 里只构造一次，然后对 N 张样本循环 `run()`。
2. **PL 必须是 operating**：`state != operating` 时碰 AXI 会挂死整板（只能断电）。
   ⇒ 开头硬门禁，不满足直接退。
3. **首跑含内核 load/自检**（q8/c8deq/bpack 各 1.4~1.9 s）⇒ 计时只认**第二跑起**；
   第 0 张标记为 `warmup=True`，比对数值照样有效。

样本约定
--------
`--samples <dir>` 下放冻结样本，命名 `NNNN_*.npy`（按名字排序，顺序固定）：

* `.npy` 支持三种形态：`(3,H,W)` float/uint8、`(H,W,3)` uint8。
* 归一化与 `bin_fp_ref.load_image` **完全一致**：`max>1.5 ⇒ /255`，并转成 `(3,H,W)`。
* 可选 `--index index.json`：`{"0000": {"sha1": "...", "md5": "..."}}`，用来断言"同一输入"。

输出（`--out <dir>`）
--------------------
```
board_dump.json                 汇总：每张的 sha1/min/max/mean/std + 环境 + 计时
0000/input_f32.npy              实际喂进去的 (3,H,W) float32（金标准对齐用）
0000/out1_f32.npy out2 out3     引擎输出（已反量化的 f32）
0000/out1_u8.npy     …          上屏口径 round(255*clip(x,0,1)) uint8
0000/out1_i8.npy+fs.json        （仅当该输出未被强制物化时）原始 int8 + frac_bits
```
主机侧拿 `out1_f32.npy` 与 `bin_fp_ref.py` 的 `--out` 直接算 A1/A2。

用法
----
```sh
# 板上（示例）
python3 dump_board_int8.py --samples /home/xilinx/demo/frozen256 \
        --pack /home/xilinx/demoir/deploy/weights_pack \
        --out /home/xilinx/demo/dump_board --shape 256

# 只查环境/门禁，不跑网络
python3 dump_board_int8.py --check-only
```
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "pynq"), "/home/xilinx/demoir/ps/pynq", HERE):
    if _p not in sys.path:
        sys.path.append(_p)

FPGA_STATE = "/sys/class/fpga_manager/fpga0/state"
SLCR = 0xF800_0000
FPGA0_CLK_CTRL = 0x170


# --------------------------------------------------------------------------
# 环境
# --------------------------------------------------------------------------
def sha1_of(a) -> str:
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()


def md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pl_state() -> str:
    try:
        with open(FPGA_STATE) as f:
            return f.read().strip()
    except OSError as e:
        return "?%s" % e


def fclk_true_mhz():
    """读 SLCR 0xF8000170 解真值（PYNQ 报数 = 真值 ×1.5，不可直接信）。"""
    import ctypes
    import mmap
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    try:
        m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED,
                      mmap.PROT_READ | mmap.PROT_WRITE, offset=SLCR)
        raw = ctypes.c_uint32.from_buffer(m, FPGA0_CLK_CTRL).value
        m.close()
    finally:
        os.close(fd)
    return (3200.0 / 3.0) / (((raw >> 8) & 0x3F) + 1), raw


def gate(check_only=False) -> dict:
    """PL 硬门禁：不是 operating 就退。返回环境字典。"""
    st = pl_state()
    env = {"fpga_state": st}
    print("[gate] fpga0 state = %s" % st)
    if st != "operating":
        raise SystemExit("[gate] !! PL 没载位流 ⇒ 拒绝任何 AXI 访问（会挂死整板）")
    try:
        mhz, raw = fclk_true_mhz()
        env.update(fclk_true_mhz=round(mhz, 2), fpga0_clk_ctrl="0x%08X" % raw)
        print("[gate] FCLK0 真值 %.2f MHz（SLCR=0x%08X）" % (mhz, raw))
    except Exception as e:
        print("[gate] 读 FCLK0 失败（不影响）：%s" % e)
    return env


# --------------------------------------------------------------------------
# 样本
# --------------------------------------------------------------------------
def norm_input(path: str) -> tuple[np.ndarray, dict]:
    """与 ``bin_fp_ref.load_image`` 同口径：→ (3,H,W) float32，max>1.5 则 /255。"""
    a = np.load(path)
    info = {"file": os.path.basename(path), "raw_shape": list(a.shape),
            "raw_dtype": str(a.dtype), "md5": md5_of(path)}
    if a.ndim == 2:
        a = np.stack([a, a, a])
    if a.ndim != 3:
        raise SystemExit("[samples] %s 不是 2/3 维：%s" % (path, a.shape))
    if a.shape[0] in (1, 3, 4):
        a = a[:3]
    elif a.shape[2] in (1, 3, 4):
        a = a[:, :, :3].transpose(2, 0, 1)
    else:
        raise SystemExit("[samples] %s 通道维认不出：%s" % (path, a.shape))
    a = a.astype(np.float64)
    if a.max() > 1.5:
        a = a / 255.0
        info["divided_by_255"] = True
    else:
        info["divided_by_255"] = False
    info["in_sha1"] = sha1_of(a.astype(np.float32))
    return np.ascontiguousarray(a, dtype=np.float32), info


def tensor_stats(a: np.ndarray) -> dict:
    a = np.asarray(a)
    return {"shape": list(a.shape), "dtype": str(a.dtype),
            "min": float(a.min()), "max": float(a.max()),
            "mean": float(a.mean()), "std": float(a.std()),
            "sha1": sha1_of(a)}


def to_u8(a: np.ndarray) -> np.ndarray:
    """上屏口径：round(255*clip(x,0,1))（与 bin_fp_ref / 板端 to_uint8 同式）。"""
    return np.rint(np.clip(np.asarray(a, dtype=np.float64), 0.0, 1.0) * 255.0).astype(np.uint8)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="板端 int8 输出 dump（PS↔PC 对拍）")
    ap.add_argument("--samples", help="冻结样本目录（NNNN_*.npy）")
    ap.add_argument("--index", default=None, help="index.json：断言 sha1/md5")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/weights_pack")
    ap.add_argument("--bit", default=None, help="位流（默认自动找）")
    ap.add_argument("--shape", type=int, default=256, help="网络边长（256/64）")
    ap.add_argument("--out", default="/home/xilinx/demo/dump_board")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 张（0=全部）")
    ap.add_argument("--slots", type=int, default=24, help="arena 槽数（板端实测 24）")
    ap.add_argument("--check-only", action="store_true", help="只查门禁/环境，不跑网络")
    ap.add_argument("--no-pipeline", action="store_true", help="关流水（严格串行，慢但好查）")
    a = ap.parse_args(argv)

    env = gate()
    env.update(pack=a.pack, shape=a.shape, slots=a.slots,
               argv=sys.argv, when=time.strftime("%F %T"))
    if a.check_only:
        print("[check-only] 环境：%s" % json.dumps(env, ensure_ascii=False))
        return 0

    if not a.samples:
        raise SystemExit("需要 --samples <目录>（冻结 256² 样本）")
    files = sorted(glob.glob(os.path.join(a.samples, "*.npy")))
    if not files:
        raise SystemExit("[samples] %s 下没有 .npy" % a.samples)
    if a.limit:
        files = files[:a.limit]
    print("[samples] %d 张：%s" % (len(files), [os.path.basename(f) for f in files]))
    if len(files) < 3:
        print("[samples] !! 判据 A3 要求 ≥3 张，当前只有 %d 张" % len(files))

    idx = {}
    if a.index and os.path.exists(a.index):
        with open(a.index, encoding="utf-8") as f:
            idx = json.load(f)
        print("[samples] 载入 index：%d 条" % len(idx))

    # ---- 只建这一次 BinNet（硬约束 1）----------------------------------
    import bin_engine as BE
    import bin_net as BN
    from pynq import Overlay

    bit = a.bit
    if bit is None:
        for c in ("/home/xilinx/demo/design_1_v58.bit",
                  "/home/xilinx/demoir/runs/hdmi_v58/design_1_v58.bit"):
            if os.path.exists(c):
                bit = c
                break
    if bit is None or not os.path.exists(bit):
        raise SystemExit("找不到位流；用 --bit 指定")
    env["bit"] = bit
    env["bit_md5"] = md5_of(bit)
    env["ps_md5"] = {os.path.basename(m): md5_of(m) for m in
                     ("bin_net.py", "bin_engine.py") if os.path.exists(m)}
    ov = Overlay(bit, download=False)          # PL 已经载好，**绝不重载**
    try:
        fclk = float(os.environ.get("DUMP_FCLK_MHZ", "0"))
        if fclk > 0:
            from pynq import Clocks
            Clocks.fclk0_mhz = fclk
            time.sleep(0.3)
            print("[clk] 已设报数 %g（真值 %g）" % (fclk, fclk / 1.5))
            env["fclk_set"] = fclk
    except Exception as e:
        print("[clk] 设置时钟失败（继续）：%s" % e)

    eng = BE.BinConvEngine(ov, unit_id=0, pack_dir=a.pack, verbose=False)
    eng.load_weights()
    net = BN.BinNet(a.pack, engine=eng, H=a.shape, W=a.shape, verbose=False)
    try:
        BE.report_deps()
    except Exception:
        pass

    os.makedirs(a.out, exist_ok=True)
    rows = []
    for i, fp in enumerate(files):
        tag = os.path.splitext(os.path.basename(fp))[0]
        x0, info = norm_input(fp)
        exp = idx.get(tag) or idx.get(os.path.basename(fp)) or {}
        if exp.get("md5") and exp["md5"] != info["md5"]:
            raise SystemExit("[samples] %s md5 不符（index %s / 实读 %s）"
                             % (tag, exp["md5"][:12], info["md5"][:12]))
        if exp.get("sha1") and exp["sha1"] != info["in_sha1"]:
            raise SystemExit("[samples] %s 输入 sha1 不符（index %s / 实读 %s）"
                             % (tag, exp["sha1"][:12], info["in_sha1"][:12]))

        d = os.path.join(a.out, "%04d" % i)
        os.makedirs(d, exist_ok=True)
        np.save(os.path.join(d, "input_f32.npy"), x0)

        t0 = time.time()
        outs, st = net.run(x0, pipeline=not a.no_pipeline, nbuf=2)
        dur = time.time() - t0

        rec = {"i": i, "tag": tag, "input": info,
               "warmup": (i == 0),           # 第 0 张含内核 load/自检，计时别用
               "wall_s": round(dur, 4),
               "total_s": st.get("total"), "pl_s": st.get("pl"),
               "ps_s": st.get("ps"), "overlap": st.get("overlap"),
               "mmio": st.get("mmio_writes"),
               "tensors": {}, "int8": {}}
        for k in ("out1", "out2", "out3"):
            if k not in outs:
                continue
            v = outs[k]
            # 惰性 C8 句柄：能拿到原始 int8 + frac_bits 就一起存
            if isinstance(v, BE.C8Tensor):
                q = np.asarray(v.buf).reshape(v.shape)
                np.save(os.path.join(d, "%s_i8.npy" % k), q)
                rec["int8"][k] = {"fs_out": int(v.fs_out),
                                  "q_min": int(q.min()), "q_max": int(q.max()),
                                  "q_sha1": sha1_of(q)}
                v = v.materialize()
            arr = np.ascontiguousarray(np.asarray(v, dtype=np.float32))
            u8 = to_u8(arr)
            np.save(os.path.join(d, "%s_f32.npy" % k), arr)
            np.save(os.path.join(d, "%s_u8.npy" % k), u8)
            rec["tensors"][k] = tensor_stats(arr)
            rec["tensors"][k]["u8"] = tensor_stats(u8)
        rows.append(rec)
        print("[run] %s  %-22s wall=%.2f s  out1[min=%.3f max=%.3f mean=%.3f]%s"
              % (tag, os.path.basename(fp), dur,
                 rec["tensors"].get("out1", {}).get("min", float("nan")),
                 rec["tensors"].get("out1", {}).get("max", float("nan")),
                 rec["tensors"].get("out1", {}).get("mean", float("nan")),
                 "  (warmup，计时别用)" if rec["warmup"] else ""), flush=True)

    dump = {"version": 1, "env": env,
            "samples": rows,
            "warm_rows": [r for r in rows if not r["warmup"]],
            "timing_note": "第 0 张含内核 load/自检（1.4~1.9 s/个）⇒ 稳态看 warm_rows",
            "gate_hint": "主机侧：A1 max|Δu8| ≤ 1 LSB；A2 rel_rms ≤ 0.01；A3 ≥3 张"}
    with open(os.path.join(a.out, "board_dump.json"), "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=1)
    print("\n[done] %s" % os.path.join(a.out, "board_dump.json"))
    if len(rows) > 1:
        w = [r["wall_s"] for r in rows[1:]]
        print("[timing] 稳态 %d 张：min %.2f / mean %.2f / max %.2f s"
              % (len(w), min(w), sum(w) / len(w), max(w)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
