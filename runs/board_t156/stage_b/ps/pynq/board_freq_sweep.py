#!/usr/bin/env python3
"""board_freq_sweep.py —— 同一 bit、同一 PS 代码，只切 fclk0_mhz，测整网时间曲线。

用法（板上）：
    cd /home/xilinx/demoir/ps/pynq
    /usr/local/share/pynq-venv/bin/python3 board_freq_sweep.py \
        --bit design_1_150v35.bit --hw 256 --freqs 100,110,120,125 --no-download

铁律：
  * 默认 --no-download（板子已 load 过一次就不用再 load）
  * 绝不改 BD；时钟只走 PYNQ 运行时 Clocks.fclk0_mhz
  * 每个频率跑 2 次取 min；跑完把时钟恢复成第一个频率
  * 结果写 JSON + 打印表
"""
import argparse, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bit", default=None, help="overlay bit 文件名（含 .bit）")
    ap.add_argument("--hw", type=int, default=256)
    ap.add_argument("--freqs", default="100,110,120,125")
    ap.add_argument("--pack", default="/home/xilinx/demoir/deploy/bin_weights_new")
    ap.add_argument("--no-download", action="store_true", default=True)
    ap.add_argument("--out", default="freq_sweep.json")
    ap.add_argument("--reps", type=int, default=2)
    a = ap.parse_args()

    from pynq import Overlay, Clocks
    if a.bit and not a.no_download:
        ov = Overlay(a.bit)
    else:
        ov = Overlay(a.bit, download=False) if a.bit else None

    clk = Clocks.fclk0_mhz
    print(f"[sweep] 当前 fclk0 = {clk} MHz")

    import bin_net
    freqs = [float(x) for x in a.freqs.split(",") if x.strip()]
    rows = []
    for f in freqs:
        try:
            Clocks.fclk0_mhz = f
        except Exception as e:
            print(f"[sweep] 设 {f} MHz 失败: {e}")
            continue
        time.sleep(0.3)
        got = Clocks.fclk0_mhz
        best = None
        for r in range(a.reps):
            net = bin_net.BinNet(a.pack, hw=a.hw)
            t0 = time.perf_counter()
            net.run_once()
            dt = time.perf_counter() - t0
            best = dt if best is None else min(best, dt)
            sh = net.last_sha1() if hasattr(net, "last_sha1") else "?"
            print(f"[sweep] {f} MHz  rep{r}  {dt:.3f} s  sha1={sh}")
        rows.append({"req_mhz": f, "got_mhz": got, "best_s": round(best, 4)})
        print(f"[sweep] >>> {f} MHz (实际 {got})  min {best:.3f} s")

    if rows:
        rows.sort(key=lambda r: r["best_s"])
        print("\n=== 曲线（按时间排序）===")
        for r in rows:
            print(f"  {r['req_mhz']:>6.1f} MHz (实际 {r['got_mhz']:>7.4f})  {r['best_s']:.3f} s")
        json.dump(rows, open(a.out, "w"), indent=2)
        print(f"\n[sweep] 已写 {a.out}；最快是 {rows[0]['req_mhz']} MHz")

    try:
        Clocks.fclk0_mhz = freqs[0]
        print(f"[sweep] 时钟已恢复到 {Clocks.fclk0_mhz} MHz")
    except Exception:
        pass


if __name__ == "__main__":
    main()
