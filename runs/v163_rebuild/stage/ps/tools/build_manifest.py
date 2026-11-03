#!/usr/bin/env python3
"""生成整网执行清单（manifest.json）——PS 端调度的“程序”。

按 nets_student.py 的结构手工复刻一遍前向过程（本机没有 PyTorch 也能跑），
输出：
    1) ops 列表：按执行顺序排列的每一个操作（conv / 层间算子），带
       - 输入输出缓冲区、形状、权重键名（与 checkpoint 的 state_dict 键一致）
       - conv 的形状参数（cin/cout/k/s/d/pad/H/W/Hout/Wout）与 relu 标志
       - needed 标志（out_2/out_3 辅助输出分支推理时可不执行）
    2) buffers 表：每个中间张量的通道/高/宽/字节数（DDR 分配计划）
    3) 统计信息：卷积总数、六形态计数（与 shape_inventory 对账）、无 ReLU 层数

用法：
    python build_manifest.py                 # 640x640，输出 ../manifest_640.json
    python build_manifest.py --h 64 --w 64 --out ../manifest_64.json
"""
import argparse
import json
import math
import os

# 与 weights/weights.yaml 一致的结构参数（my_model(48, 32, 64, 32, 1)）
EN_F, EN_I, DE_F, DE_I = 48, 32, 64, 32
SAM_D = (1, 2, 3, 2, 1)     # SAM 内 GhostModule1 的膨胀序列
RDB_D = (1, 2, 1)           # GhostModule 的膨胀序列


class Builder:
    def __init__(self, H, W):
        self.H, self.W = H, W
        self.ops = []
        self.buffers = {}
        self.n = 0

    def buf(self, c, h, w, dtype="int8", tag=""):
        name = "t%d" % self.n
        self.n += 1
        self.buffers[name] = {
            "c": c, "h": h, "w": w, "dtype": dtype,
            "bytes": c * h * w * (1 if dtype == "int8" else 4),
            "tag": tag,
        }
        return name

    def emit(self, op, name, ins, outs, **kw):
        e = {"id": len(self.ops), "op": op, "name": name,
             "in": list(ins), "out": list(outs), "needed": True}
        e.update(kw)
        self.ops.append(e)
        return e

    # ---------- 算子原语 ----------

    def conv(self, key, name, t_in, cin, cout, k, s, p, d, h, w,
             relu=True, needed=True):
        """same padding：hout = floor((h-1)/s)+1（与硬件校验公式一致）。"""
        hout = (h - 1) // s + 1
        wout = (w - 1) // s + 1
        t_out = self.buf(cout, hout, wout, tag=name)
        self.emit("conv", name, [t_in], [t_out], weights_key=key,
                  cin=cin, cout=cout, k=k, s=s, d=d, pad=p,
                  h=h, w=w, hout=hout, wout=wout,
                  relu=relu, needed=needed)
        return t_out

    def cat(self, name, ts, channels, h, w):
        t_out = self.buf(channels, h, w, tag=name)
        self.emit("cat", name, ts, [t_out], channels=channels)
        return t_out

    def interp(self, name, t_in, c, h_out, w_out, num, den):
        """双线性插值（align_corners=False），比例 num/den。"""
        t_out = self.buf(c, h_out, w_out, tag=name)
        self.emit("interp", name, [t_in], [t_out],
                  scale_num=num, scale_den=den, mode="bilinear")
        return t_out

    def add(self, name, a, b, c, h, w):
        t_out = self.buf(c, h, w, tag=name)
        self.emit("add", name, [a, b], [t_out])
        return t_out

    # ---------- 网络模块 ----------

    def ghost_block(self, base, t, cin, inter, dil, h, w):
        """GhostBlock：primary 1x1（in→init）+ cheap 3x3 膨胀（init→new）+ cat。"""
        init = math.ceil(inter / 2)
        new = init                  # ratio=2 时为一半通道
        t1 = self.conv(base + ".primary_conv.0", base + ".primary_conv",
                       t, cin, init, 1, 1, 0, 1, h, w, relu=True)
        t2 = self.conv(base + ".cheap_operation.0", base + ".cheap_operation",
                       t1, init, new, 3, 1, dil, dil, h, w, relu=True)
        c_cat = init + new
        t_out = self.cat(base + ".cat", [t1, t2], c_cat, h, w)
        if c_cat > inter:
            t_sl = self.buf(inter, h, w, tag=base + ".slice")
            self.emit("slice", base + ".slice", [t_out], [t_sl], channels=inter)
            t_out = t_sl
        return t_out

    def ghost_module(self, base, t, cin, inter, dlist, h, w, residual):
        """GhostModule（residual=True，+x）或 GhostModule1（False）。"""
        c = cin
        cur = t
        for i, d in enumerate(dlist):
            tb = self.ghost_block("%s.ghost_layers.%d" % (base, i),
                                  cur, c, inter, d, h, w)
            c += inter
            cur = self.cat("%s.cat.%d" % (base, i), [tb, cur], c, h, w)
        # 最终 1x1 投影：网上这里是线性层（无 ReLU）
        tproj = self.conv(base + ".conv", base + ".conv_proj",
                          cur, c, cin, 1, 1, 0, 1, h, w, relu=False)
        if residual:
            return self.add(base + ".residual", tproj, t, cin, h, w)
        return tproj

    def sam(self, base, t, cin, inter, h, w):
        """多尺度注意力：三分支（1x/0.5x/0.25x）+ ECA 融合 + 残差。"""
        y0 = self.ghost_module(base + ".basic_block", t, cin, inter,
                               SAM_D, h, w, residual=False)
        t2 = self.interp(base + ".down2", t, cin, h // 2, w // 2, 1, 2)
        y2 = self.ghost_module(base + ".basic_block_2", t2, cin, inter,
                               SAM_D, h // 2, w // 2, residual=False)
        t4 = self.interp(base + ".down4", t, cin, h // 4, w // 4, 1, 4)
        y4 = self.ghost_module(base + ".basic_block_4", t4, cin, inter,
                               SAM_D, h // 4, w // 4, residual=False)
        y2u = self.interp(base + ".up2", y2, cin, h, w, 2, 1)
        y4u = self.interp(base + ".up4", y4, cin, h, w, 4, 1)
        tcat = self.cat(base + ".fuse_cat", [y0, y2u, y4u], 3 * cin, h, w)
        attn = self.buf(3 * cin, 1, 1, dtype="fp32", tag=base + ".attn")
        self.emit("eca", base + ".fusion", [tcat], [attn],
                  weights_key=base + ".fusion")
        t_out = self.buf(cin, h, w, tag=base + ".sum")
        self.emit("sam_sum", base + ".sum", [y0, y2u, y4u, attn, t], [t_out])
        return t_out

    def encoder_level(self, base, t, feat, level, h, w):
        cur = self.ghost_module(base + ".rdb", t, feat, EN_I, RDB_D, h, w,
                                residual=True)
        for s in range(1):          # sam_number = 1
            cur = self.sam("%s.sam_blocks.%d" % (base, s), cur, feat,
                           EN_I, h, w)
        if level < 3:
            down = self.conv(base + ".down.0", base + ".down",
                             cur, feat, 2 * feat, 3, 2, 1, 1,
                             h, w, relu=True)
            return cur, down
        return cur, None

    def decoder_level(self, base, t, feat, h, w, need_feat, need_out):
        cur = self.ghost_module(base + ".rdb", t, feat, DE_I, RDB_D, h, w,
                                residual=True)
        cur = self.sam(base + ".sam_blocks.0", cur, feat, DE_I, h, w)
        t_out = self.conv(base + ".conv.conv", base + ".out_conv",
                          cur, feat, 12, 3, 1, 1, 1, h, w,
                          relu=False, needed=need_out)
        t_img = self.buf(3, h * 2, w * 2, tag=base + ".img")
        self.emit("pixel_shuffle", base + ".shuffle", [t_out], [t_img],
                  factor=2, needed=need_out)
        feat_out = None
        if need_feat:
            feat_out = self.interp(base + ".feat", cur, feat, h * 2, w * 2,
                                   2, 1)
        return t_img, feat_out

    # ---------- 整网前向 ----------

    def forward(self):
        H, W = self.H, self.W
        t_in = self.buf(3, H, W, tag="input")
        t0 = self.buf(12, H // 2, W // 2, tag="unshuffle")
        self.emit("pixel_unshuffle", "pre.unshuffle", [t_in], [t0], factor=2)

        t1 = self.conv("encoder.conv_first.0", "encoder.conv_first",
                       t0, 12, EN_F, 5, 1, 2, 1, H // 2, W // 2, relu=True)

        y1, down1 = self.encoder_level("encoder.encoder_1", t1, EN_F, 1,
                                       H // 2, W // 2)
        y2, down2 = self.encoder_level("encoder.encoder_2", down1, 2 * EN_F,
                                       2, H // 4, W // 4)
        y3, _ = self.encoder_level("encoder.encoder_3", down2, 4 * EN_F,
                                   3, H // 8, W // 8)

        x3 = self.conv("decoder.preconv_3.conv.0", "decoder.preconv_3",
                       y3, 4 * EN_F, DE_F, 3, 1, 1, 1, H // 8, W // 8,
                       relu=True)
        out3, feat3 = self.decoder_level("decoder.decoder_3", x3, DE_F,
                                         H // 8, W // 8,
                                         need_feat=True, need_out=False)

        x2 = self.cat("decoder.cat2", [y2, feat3], 2 * EN_F + DE_F,
                      H // 4, W // 4)
        x2c = self.conv("decoder.preconv_2.conv.0", "decoder.preconv_2",
                        x2, 2 * EN_F + DE_F, DE_F, 3, 1, 1, 1,
                        H // 4, W // 4, relu=True)
        out2, feat2 = self.decoder_level("decoder.decoder_2", x2c, DE_F,
                                         H // 4, W // 4,
                                         need_feat=True, need_out=False)

        x1 = self.cat("decoder.cat1", [y1, feat2], EN_F + DE_F,
                      H // 2, W // 2)
        x1c = self.conv("decoder.preconv_1.conv.0", "decoder.preconv_1",
                        x1, EN_F + DE_F, DE_F, 3, 1, 1, 1,
                        H // 2, W // 2, relu=True)
        out1, _ = self.decoder_level("decoder.decoder_1", x1c, DE_F,
                                     H // 2, W // 2,
                                     need_feat=False, need_out=True)
        return {"t_in": t_in, "out1": out1, "out2": out2, "out3": out3}


def summarize(ops, buffers):
    convs = [o for o in ops if o["op"] == "conv"]
    shapes = {}
    for o in convs:
        key = (o["k"], o["s"], o["pad"], o["d"])
        shapes[key] = shapes.get(key, 0) + 1
    no_relu = [o for o in convs if not o["relu"]]
    total_bytes = sum(b["bytes"] for b in buffers.values())
    peak = max(b["bytes"] for b in buffers.values())
    return convs, shapes, no_relu, total_bytes, peak


def check_engine_limits(convs):
    """把"引擎会算错/寻址溢出"的卷积挡在生成阶段（跑板子之前就报）。

    两个上界都来自 `pl/src/conv2d_engine.cpp`：
      1) 内部用 int32 算 ic*H*W → cin*H*W 必须 < 2^31；
      2) HLS 里 acc_t = ap_int<26>，两个累加器各管一半通道 →
         ceil(cin/2)·k²·127·128 必须 < 2^26。
    """
    bad = []
    for o in convs:
        if o["cin"] * o["h"] * o["w"] > 0x7FFFFFFF:
            bad.append("%s: cin*H*W=%d 超过 int32 寻址上限"
                       % (o["name"], o["cin"] * o["h"] * o["w"]))
        half = (o["cin"] + 1) // 2
        if half * o["k"] ** 2 * 127 * 128 >= (1 << 26):
            bad.append("%s: cin=%d k=%d 单侧累加上界超过 26 位累加器"
                       % (o["name"], o["cin"], o["k"]))
    if bad:
        raise SystemExit("清单里有超出 PL 引擎能力上限的卷积：\n  " + "\n  ".join(bad[:8]))


def peak_live(ops, buffers):
    """按“最后一次使用即释放”估算同时存活内存的峰值（DDR 分配依据）。"""
    last = {}
    for op in ops:
        for t in op["in"]:
            last[t] = op["id"]
    live = 0
    peak = 0
    for op in ops:
        for t in op["out"]:
            live += buffers[t]["bytes"]
            peak = max(peak, live)
        for t in op["in"]:
            if last.get(t, -1) == op["id"]:
                live -= buffers[t]["bytes"]
    return peak


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--h", type=int, default=640)
    ap.add_argument("--w", type=int, default=640)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    for d in (16, 8, 4, 2):     # pixel_unshuffle 2 与 3 级下采样要求整除
        assert a.h % d == 0 and a.w % d == 0, "H/W 必须能被 16 整除"

    b = Builder(a.h, a.w)
    io = b.forward()
    convs, shapes, no_relu, total_bytes, peak = summarize(b.ops, b.buffers)
    peak_live_bytes = peak_live(b.ops, b.buffers)
    check_engine_limits(convs)

    print("=== 整网清单（%dx%d 输入） ===" % (a.h, a.w))
    print("ops 总数        : %d" % len(b.ops))
    print("conv 总数       : %d  （期望 249）" % len(convs))
    expect = {(1, 1, 0, 1): 132, (3, 1, 1, 1): 54, (3, 1, 2, 2): 42,
              (3, 1, 3, 3): 18, (3, 2, 1, 1): 2, (5, 1, 2, 1): 1}
    ok = True
    for key in sorted(expect):
        got = shapes.get(key, 0)
        same = "OK" if got == expect[key] else "MISMATCH"
        if got != expect[key]:
            ok = False
        print("  形态 K=%d s=%d p=%d d=%d : %3d 个  [%s]"
              % (key[0], key[1], key[2], key[3], got, same))
    print("无 ReLU 的 conv : %d 个（v2 硬件以 relu_en=0 支持线性输出）" % len(no_relu))
    print("中间张量总数    : %d，合计 %.1f MB，单张最大 %.1f MB"
          % (len(b.buffers), total_bytes / 1e6, peak / 1e6))
    print("同时存活峰值    : %.1f MB（按生命周期复用后的真实 DDR 需求）"
          % (peak_live_bytes / 1e6))

    assert len(convs) == 249, "卷积数量与网络结构不一致！"
    assert ok, "六形态计数与 shape_inventory 不一致！"

    out = a.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "manifest_%d.json" % a.h)
    doc = {
        "version": 1,
        "semantics": "signed int8 features [-128,127]; engine conv post = "
                     "sat8((acc+bias)>>shift), relu 由 relu_en 位控制(硬件 v2); "
                     "non-conv ops round+clip to [-128,127] (v1)",
        "input_hw": [a.h, a.w],
        "io": io,
        "ops": b.ops,
        "buffers": b.buffers,
        "stats": {
            "n_ops": len(b.ops),
            "n_conv": len(convs),
            "n_conv_no_relu": len(no_relu),
            "bytes_total": total_bytes,
            "bytes_peak_tensor": peak,
            "bytes_peak_live": peak_live_bytes,
        },
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("已写出: %s" % os.path.abspath(out))


if __name__ == "__main__":
    main()
