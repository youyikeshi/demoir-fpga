#!/usr/bin/env python3
# ============================================================================
#  model_k3_cycles.py —— conv_k3_engine 的**逐拍模型**（时序/相位，不算数值）
#
#  为什么要它（任务书硬约束 6，k=1 的真实教训）：
#    k=1 的输出队列里 `wr_pop` 是**寄存**的：换组（grp_adv）发生在第 t 拍，弹槽（wr_pop）
#    晚一拍。当时只把 grp_adv 盖住、没盖 wr_pop，于是在"弹槽后立刻有下一块已提交"时
#    用**旧地址 + 新数据**发了一拍（conv_k1_engine.v:833-839）。仿真全过、模型说 PASS、
#    RTL 错。教训不是"修个 bug"，而是：
#      **光复刻状态机不够，必须复刻"寄存器输出 + 寄存的控制信号"带来的相位差。**
#    所以本文件有两个模式，缺一不可：
#      · `--demo`  ：把流水线寄存器一个个建出来，逐拍跑，打印相位表 + 断言（相位保真）；
#      · `--all`   ：闭式估算（拍数 = 计算 + 前导 + 装权重 + 填充受限 + 排空），
#                    给验收判据里的"目标拍数"（速度快，能扫 114 层）。
#
#  用法：
#    python3 rtl_hand/tools/model_k3_cycles.py --demo            # 相位演示 + 断言
#    python3 rtl_hand/tools/model_k3_cycles.py --typo            # 三个典型层
#    python3 rtl_hand/tools/model_k3_cycles.py --all             # 全网 114 层
#    python3 rtl_hand/tools/model_k3_cycles.py --all --lanes 12 --ddr-bw 8
#    python3 rtl_hand/tools/model_k3_cycles.py --layer 112 64 320 320 1 1 1
#
#  口径：1 拍 = 1 个 ic 通道字节 = 9×LANES 次 MAC（LANES=8 时 72 MAC/拍）
# ============================================================================
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RTL_ROOT = os.path.dirname(HERE)                 # .../pl/rtl_hand
PL_ROOT = os.path.dirname(RTL_ROOT)              # .../pl
PS_ROOT = os.path.dirname(PL_ROOT)               # .../pynq_specialized_code
MANIFEST = os.path.join(PS_ROOT, "ps", "manifest_640.json")

C8 = 8
CIN_MAX = 192


def cpad(c):
    return (c + C8 - 1) // C8 * C8


def hw_out(n, k=3, s=1, d=1, pad=1):
    return (n + 2 * pad - ((k - 1) * d + 1)) // s + 1


# ============================================================================
#  相位表（RTL 与模型必须逐条对齐；改 RTL 就回来改这里）
# ============================================================================
PHASES = """\
 拍   信号                          驱动源                    说明
 ──   ────────────────────────────  ────────────────────────  ──────────────────────────────────
 t    tap_raddr[bank][col]          cx + (col-1)*D           行缓冲同步读（地址=寄存器加法）
 t+1  tapw_next[bank][col]          mem[tap_raddr]           读到数据（单一赋值源，不复位）
 t+1  pf_cnt                        +1                       3 拍读完 9 字（每 bank 1 字/拍）
 t+3  tapw_cur ⇄ tapw_next          组边界                   下一组 9 字已就绪（双缓冲）
 t    wgt_raddr = ic                计数器                    权重 BRAM 同步读
 t+1  w_row_q                       mem[wgt_raddr]           576 bit = 8 lane × 9 tap
 t    p_byte[k] = tapw_cur[k][b]    组合 8:1 字节选择         b = ic[2:0]
 t+1  pb_r[k] / w_row_q             **两者同相**（像素字节打一拍）→ 组合乘
 t+2  prod_b[lane][tap]             8×8 乘结果打拍（issue(t) 的乘积）
 t+2  tree_sum[lane]                9→1 加法树（19b）         组合
 t+3  tsum_r[lane]                  树输出打拍                时序修复 #3（砍树+累加）
 t+3  acc_e/acc_o[lane]             acc + tsum_r              奇偶双链（v_c/par_c 同相）
 t+3  sum27[lane]                   acc_e+acc_o+tsum_r        **仅 last_c**，同拍清零 acc
 t+4  v32[lane]                     sext(sum27)+bias          E1（bias 是 pass 级寄存器）
 t+5  sat8[lane]                    >>>shift / relu / sat8    E2
 t+5  push                          beat FIFO 写              比最后一个 ic 的 issue 晚 5 拍
 t+?  pop                           FIFO 读口 + hold_q        pop 比 push 晚；hold_q 必须盖住
                                                              pop 那一拍（k=1 的坑）
"""


# ============================================================================
#  模式 1：逐拍演示（把流水线寄存器真的建出来）
# ============================================================================
class PipelineDemo:
    """逐拍推进 RTL 里真实存在的那些寄存器（只跑前若干像素）。

    目的不是算拍数（那是模式 2），而是**证明相位**：
      · push 的间隔必须恰好 = cin_p（稳态 II=1）；
      · 连续 push 的地址差 = 8 字节（C8 一组 8 字节）；
      · FIFO 空时绝不 pop（含"读口 1 拍延迟"那一拍）；
      · `hold_q` 覆盖"pop 那一拍"（k=1 踩的坑）。

    流水线（与 PHASES 严格一致，全部是寄存器）：
        issue(t) → prod_b(t+1) → sum27/acc(t+2) → v32/E1(t+3) → sat8/E2(t+4)=push
    也就是说：**最后一个 ic 的 push 比它自己的 issue 晚 4 拍**。
    """

    PUSH_LAT = 5         # issue → push 的固定延迟（拍；含 pb_r 与 tsum_r 两级）

    def __init__(self, cin=16, cout=16, n_pix=6, lanes=8, fifo_depth=4):
        self.cin_p = cpad(cin)
        self.lanes = lanes
        self.n_pix = n_pix
        self.fifo_depth = fifo_depth
        self.cyc = 0
        self.ic = 0
        self.pix = 0
        # tap 双缓冲（RTL 里就是 tapw_cur / tapw_next 两组寄存器）
        self.cur_ok = 0      # 当前组 9 字已就绪
        self.next_ok = 0     # 下一组 9 字已就绪（后台预取）
        self.pf_cnt = 0      # 下一组的读入进度 0..3（每拍 1 个 bank×col）
        # 流水线：只搬 "这是一个像素的最后一个 ic" 标志 + 该像素的输出字节地址
        self.pipe = [(0, 0, 0)] * self.PUSH_LAT
        # 输出侧
        self.occ = 0
        self.rd_vld = 0
        self.hold_q = 0
        self.beats = []
        self.viol = []

    def step(self):
        c = self.cyc
        last_ic = (self.ic == self.cin_p - 1)
        # issue 只看**当前组**的 tapw_cur 是否就绪（与 RTL 的 issue_en 一致）
        issue = self.cur_ok and (self.occ < self.fifo_depth)

        # ---- 流水线移位（每拍一级，全寄存器）----
        head = (1 if (issue and last_ic) else 0, 8 * self.pix, self.pix)
        push_v, push_addr, push_pix = self.pipe[-1]
        self.pipe = [head] + self.pipe[:-1]

        # ---- E2 拍 push 进 beat FIFO ----
        if push_v:
            if self.occ >= self.fifo_depth:
                self.viol.append("cyc %d: FIFO 满时 push" % c)
            self.occ += 1
            self.beats.append((c, push_addr, push_pix))

        # ---- 输出写引擎：FIFO 读口 1 拍延迟 + hold_q ----
        w_hs = self.rd_vld          # WREADY 恒 1（常态）
        if w_hs:
            if self.occ == 0:
                self.viol.append("cyc %d: pop 空 FIFO" % c)
            self.occ -= 1
        self.rd_vld = 1 if (self.occ - (1 if w_hs else 0)) > 0 else 0
        self.hold_q = 0 if w_hs else 1     # 没弹成就必须保持数据（k=1 的坑）

        # ---- tap 预取：为"下一组"后台读 9 字（每拍 1 个 bank×col，共 3 拍）----
        if not self.next_ok:
            self.pf_cnt += 1
            if self.pf_cnt >= 3:
                if not self.cur_ok:
                    self.cur_ok = 1      # 预热：第一组直接装进 cur（RTL 的 S_PROLOGUE）
                else:
                    self.next_ok = 1
                self.pf_cnt = 0

        # ---- 计数器 + 组边界换缓冲 ----
        if issue:
            grp_end = last_ic or ((self.ic % 8) == 7)
            if last_ic:
                self.ic = 0
                self.pix += 1
            else:
                self.ic += 1
            if grp_end:
                # 组边界：tapw_cur ⇄ tapw_next（双缓冲交换）
                if self.next_ok:
                    self.cur_ok = 1
                    self.next_ok = 0
                    self.pf_cnt = 0
                else:
                    self.cur_ok = 0    # 没预取完 ⇒ 下一拍 stall（RTL 的 issue_en=0）
        self.cyc += 1
        return issue

    def run(self):
        guard = 0
        while self.pix < self.n_pix and guard < 200000:
            self.step()
            guard += 1
        return self

    def report(self):
        print("── 逐拍相位演示：cin=%d（每像素 %d 拍）LANES=%d，跑 %d 个像素 ──"
              % (self.cin_p, self.cin_p, self.lanes, self.n_pix))
        if not self.beats:
            print("  ❌ 没有任何 push（流水线没跑起来）")
            return 1
        print("  push 事件（cycle, addr, pixel）：")
        for e in self.beats[:8]:
            print("    cyc=%3d addr=%4d pix=%d" % e)
        gaps = sorted({self.beats[i + 1][0] - self.beats[i][0]
                       for i in range(len(self.beats) - 1)})
        addrs = sorted({self.beats[i + 1][1] - self.beats[i][1]
                        for i in range(len(self.beats) - 1)})
        print("  push 间隔 = %s（期望 {%d}）" % (gaps, self.cin_p))
        print("  地址增量 = %s（期望 {8}）" % (addrs,))
        ok = (gaps == [self.cin_p]) and (addrs == [8]) and not self.viol
        print("  断言：%s" % ("✅ 全部通过" if ok else "❌ " + str(self.viol[:3])))
        return 0 if ok else 1


# ============================================================================
#  模式 2：闭式估算（给"目标拍数"）
# ============================================================================
class LayerEst:
    def __init__(self, cin, cout, H, W, s=1, d=1, pad=1, lanes=8,
                 ddr_bw=8.0, wt=None):
        self.cin, self.cout, self.H, self.W = cin, cout, H, W
        self.s, self.d, self.pad = s, d, pad
        self.LANES = lanes
        self.cin_p = cpad(cin)
        self.NGRP = self.cin_p // C8
        self.P = (cout + lanes - 1) // lanes
        self.WT = wt if wt else W
        self.ddr_bpc = ddr_bw / 8.0                  # beat/拍
        self.Hout = hw_out(H, 3, s, d, pad)
        self.Wout = hw_out(W, 3, s, d, pad)
        self.nstrip = (W + self.WT - 1) // self.WT
        self.Wout_s = hw_out(min(W, self.WT), 3, s, d, pad)
        self.mode = "rolling" if d == 1 else "pingpong"

    def run(self):
        L = self.LANES
        comp = self.Hout * self.Wout * self.P * self.cin_p     # 纯计算
        rows_per_out = self.s if self.mode == "rolling" else 3
        cols = min(self.W, self.WT) + 2 * self.d   # 行缓冲存的是**输入**列
        # 前导：每 pass 每片 灌 d+1 行
        prolog = self.P * self.nstrip * (self.d + 1) * self.NGRP * cols
        # 权重：每 pass 2 拍/(oc,ic)
        wgt = self.P * 2 * L * self.cin_p
        # 行尾尾填充：滚动模式最后两列只能串行补（每行 2*NGRP*S），
        # 但可以借下一行的空档 ⇒ 只算每 pass 最后一次
        tail = self.P * 2 * self.NGRP * self.s
        # 每行边界排空（E1/E2 流水 + 行尾）
        drain = self.P * self.Hout * 4
        # 填充是否受限：fill_rate(beat/拍) vs DDR 供数
        fill_total = self.P * self.Hout * self.nstrip * rows_per_out * self.NGRP * cols
        fill_rate = fill_total / max(1, comp)
        self.fill_rate = fill_rate
        self.fill_bound = fill_rate > self.ddr_bpc
        extra = 0
        if self.fill_bound:
            # 供不上 ⇒ 这一 pass 的填充时间成为下限
            extra = int(fill_total / self.ddr_bpc) - comp
        self.comp, self.prolog, self.wgt = comp, prolog, wgt
        self.tail, self.drain, self.extra = tail, drain, max(0, extra)
        self.cyc = comp + prolog + wgt + tail + drain + self.extra
        return self


def fmt(name, m):
    return ("%-38s cin%4d cout%4d %3dx%-3d s%d d%d p%d | 组%2d P%2d 像素%5d 拍 | "
            "计算 %8.3fM | 目标 %8.3fM 拍 %7.1f ms | 填充 %.3f beat/拍%s") % (
        name[:38], m.cin, m.cout, m.W, m.H, m.s, m.d, m.pad, m.NGRP, m.P,
        m.P * m.cin_p, m.comp / 1e6, m.cyc / 1e6, m.cyc / 150e6 * 1e3,
        m.fill_rate, "  ⚠填充受限" if m.fill_bound else "")


def load_k3_layers():
    with open(MANIFEST) as f:
        man = json.load(f)
    return [o for o in man["ops"]
            if o.get("op") == "conv" and o.get("needed", True) and o["k"] == 3]


def main():
    ap = argparse.ArgumentParser(description="conv_k3_engine 逐拍模型")
    ap.add_argument("--demo", action="store_true", help="逐拍相位演示 + 断言")
    ap.add_argument("--typo", action="store_true", help="三个典型层")
    ap.add_argument("--all", action="store_true", help="全网 114 层")
    ap.add_argument("--layer", nargs=7, type=int,
                    metavar=("cin", "cout", "H", "W", "s", "d", "pad"))
    ap.add_argument("--lanes", type=int, default=8)
    ap.add_argument("--ddr-bw", type=float, default=8.0, help="DDR 字节/拍")
    ap.add_argument("--wt", type=int, default=None, help="条带宽度（默认不分）")
    args = ap.parse_args()

    if args.demo or not (args.all or args.typo or args.layer):
        print(PHASES)
        rc = PipelineDemo(cin=16, cout=16, n_pix=6, lanes=args.lanes).run().report()
        print()
        rc2 = PipelineDemo(cin=48, cout=96, n_pix=4, lanes=args.lanes).run().report()
        if not (args.all or args.typo or args.layer):
            return rc or rc2

    kw = dict(lanes=args.lanes, ddr_bw=args.ddr_bw, wt=args.wt)

    if args.layer:
        cin, cout, H, W, s, d, pad = args.layer
        m = LayerEst(cin, cout, H, W, s, d, pad, **kw).run()
        print(fmt("custom", m))
        print("  分解：计算 %.3fM + 前导 %.3fM + 权重 %.4fM + 尾 %.4fM + 排空 %.4fM + 填充受限 %.3fM"
              % (m.comp / 1e6, m.prolog / 1e6, m.wgt / 1e6, m.tail / 1e6,
                 m.drain / 1e6, m.extra / 1e6))
        print("  每像素每 oc 拍 = %.2f" % (m.P * m.cin_p / m.cout))
        return 0

    if args.typo:
        print("三个典型层（LANES=%d, DDR %.1f B/拍）：" % (args.lanes, args.ddr_bw))
        typ = [
            ("decoder.preconv_3 (cin=192)", 192, 64, 80, 80, 1, 1, 1),
            ("encoder.encoder_1.down (s=2)", 48, 96, 320, 320, 2, 1, 1),
            ("cheap_operation (cout=16)", 16, 16, 320, 320, 1, 1, 1),
            ("cheap_operation (d=3)", 16, 16, 320, 320, 1, 3, 3),
            ("decoder.preconv_1 (最大层)", 112, 64, 320, 320, 1, 1, 1),
        ]
        for name, cin, cout, H, W, s, d, pad in typ:
            print(fmt(name, LayerEst(cin, cout, H, W, s, d, pad, **kw).run()))

    if args.all:
        ops = load_k3_layers()
        tot = tot_comp = 0
        bounded = []
        for o in ops:
            m = LayerEst(o["cin"], o["cout"], o["h"], o["w"], o["s"], o["d"],
                         o["pad"], **kw).run()
            tot += m.cyc
            tot_comp += m.comp
            if m.fill_bound:
                bounded.append((m.fill_rate, o["name"], m.cyc))
        print("全网 %d 个 k=3 层：目标 %0.2f M 拍 = %.0f ms @150MHz（纯计算 %0.2f M，"
              "开销 %.2f%%）；HLS 口径 607.86 M ⇒ %.2fx"
              % (len(ops), tot / 1e6, tot / 150e6 * 1e3, tot_comp / 1e6,
                 100.0 * (tot - tot_comp) / tot_comp, 607.86e6 / tot))
        if bounded:
            print("  ⚠ 填充受限的层（DDR %.1f B/拍）：" % args.ddr_bw)
            for r, n, c in sorted(bounded, reverse=True)[:5]:
                print("    %-40s 需 %.3f beat/拍（%.1f B/拍）" % (n[:40], r, r * 8))
        else:
            print("  ✅ 全部层填充不受限（DDR %.1f B/拍）" % args.ddr_bw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
