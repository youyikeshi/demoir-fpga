#!/usr/bin/env python3
"""机器 B（需 PyTorch）：整网权重导出 + 标定 + 部署参考，与 manifest 对齐。

步骤：
 1) 读 student checkpoint 与 manifest（默认 ../manifest_640.json）。
 2) 用 torch 按 manifest 的算子名字重算浮点前向，逐 op 记录输出 |max|（标定）。
 3) 可选 --repo：加载真实 PDNet 模型交叉验证 out_1（max|diff|<1e-3 才继续），
    防止"重算前向"与真实模型有偏差而无人发觉。
 4) 逐 op 分标度：conv → sw/ shift / b_q 折算；其余算子按规则传播标度。
 5) 写权重包（weights.bin + index.json + eca 浮点权重）与部署参考：
    input_q.bin（量化输入）、ref_out.bin（量化语义期望输出）、
    ref_out_float.bin、ref_psnr.txt。参考由 fpga/ps/pynq/runner.py 的 RefEngine
    产出——这同时是 PS 调度代码在机器 B 端的一次集成测试。

注意：本脚本在无 PyTorch 的机器上无法运行；首次在机器 B 上执行请保留完整日志。
用法示例：
  python export_network.py --ckpt <student_ckpt> --manifest ../manifest_640.json \
      --image <图1> [--image <图2> ...] --out ../../deploy/weights_pack \
      [--repo <PDNet model 目录>]
  （--image 可重复给多张标定图；每层取多图 |max| 的包络。第一张兼作部署参考。）
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import sys

import numpy as np
import torch
import torch.nn.functional as F

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pynq"))

EN_F, EN_I, DE_F, DE_I = 48, 32, 64, 32


from img_io import load_image_np        # noqa: E402  （读图拆出去：不依赖 torch）


def load_image(path, h, w, crop="center", allow_resize=False):
    """读图 → torch (1,3,h,w)，0~1。裁剪/铁律在 `img_io.load_image_np` 里。"""
    x = load_image_np(path, h, w, crop, allow_resize)
    return torch.from_numpy(x).permute(2, 0, 1).unsqueeze(0)


class FloatPass:
    """按 manifest 名字重算浮点前向，记录每个 op 输出的标定幅度。

    标定幅度默认是 **|max|**（= 现状，最保守）；
    也可以按**分位数**取（`clip_pct=99.9` 之类）——这就是上游点名的
    "摩尔纹模型激活有离群值，直接套通用量化会掉点"的应对：
    把标度按分位数压小，让绝大多数像素拿到更细的分辨率，
    代价是极端离群值被饱和。**这个改动不动硬件**（逐层仍是一个 shift），
    与"逐通道缩放"不同（那要 PL 加逐通道寄存器，见 docs/交接要求对照.md 缺口 1）。
    """

    def __init__(self, sd, ops, clip_pct=100.0, sample_cap=65536):
        self.sd = sd
        self.op = {o["name"]: o for o in ops}
        self.calib = {}
        self.clip_pct = float(clip_pct)
        self.sample_cap = int(sample_cap)
        self.samples = {}          # name -> [|x| 子采样]，仅 clip_pct<100 时收集

    def rec(self, name, t):
        if self.clip_pct < 100.0:
            # 分位数模式：留一份有上限的子采样（够估计分位数，又不吃内存）
            a = t.detach().abs().reshape(-1)
            buf = self.samples.setdefault(name, [])
            if sum(x.numel() for x in buf) < self.sample_cap:
                step = max(1, a.numel() // 4096)
                buf.append(a[::step].clone())
            v = float(a.max())
            prev = self.calib.get(name)
            self.calib[name] = v if prev is None else max(prev, v)
        else:
            v = float(t.abs().max())
            prev = self.calib.get(name)
            self.calib[name] = v if prev is None else max(prev, v)
        return t          # ← conv/interp/cat/add 都是 return self.rec(...)，别丢

    def clip_val(self, name, dflt=1.0):
        """该张量的标定幅度（写标度就用它）。

        分位数模式下用子采样估分位数；权重这类直接写 `calib` 的条目自动回退。
        """
        if self.clip_pct < 100.0 and name in self.samples:
            v = torch.cat(self.samples[name]).to(torch.float32)
            q = float(torch.quantile(v, self.clip_pct / 100.0))
            if q > 0:
                return q
        v = self.calib.get(name)
        return dflt if v is None else v

    def conv(self, name, x):
        o = self.op[name]
        w = self.sd[o["weights_key"] + ".weight"].float()
        b = self.sd.get(o["weights_key"] + ".bias")
        b = b.float() if b is not None else None
        y = F.conv2d(x, w, b, stride=o["s"], padding=o["pad"], dilation=o["d"])
        if o["relu"]:      # 与真实模块一一对应：primary/cheap/down/preconv/conv_first 带 ReLU
            y = F.relu(y)
        return self.rec(name, y)

    def interp(self, name, x):
        o = self.op[name]
        h2 = x.shape[2] * o["scale_num"] // o["scale_den"]
        w2 = x.shape[3] * o["scale_num"] // o["scale_den"]
        y = F.interpolate(x, size=(h2, w2), mode="bilinear", align_corners=False)
        return self.rec(name, y)

    def cat(self, name, ts):
        return self.rec(name, torch.cat(ts, dim=1))

    def add(self, name, a, b):
        return self.rec(name, a + b)

    def eca(self, name, x):
        o = self.op[name]
        w1 = self.sd[o["weights_key"] + ".conv1.weight"].float().reshape(-1)
        w2 = self.sd[o["weights_key"] + ".conv2.weight"].float().reshape(-1)
        v = x.mean(dim=(2, 3)).unsqueeze(1)                     # (1,1,3C)
        v = F.conv1d(v, w2.reshape(1, 1, 9), padding=4)
        v = F.relu(v)
        c = v.shape[2]
        v = v.reshape(1, 3, c // 3).transpose(1, 2).reshape(1, 1, c)   # ChannelShuffle(3)
        v = F.conv1d(v, w1.reshape(1, 1, 9), padding=4)
        v = v.reshape(1, c // 3, 3).transpose(1, 2).reshape(1, 1, c)   # ChannelUnshuffle
        attn = torch.sigmoid(v).reshape(1, c, 1, 1)
        self.calib[name] = 1.0                                   # 权重∈(0,1)
        return attn

    def sam_sum(self, name, y0, y2, y4, attn, x):
        w = attn.chunk(3, dim=1)
        y = x + w[0] * y0 + w[1] * y2 + w[2] * y4
        return self.rec(name, y)

    def ghost_block(self, base, x, cin, inter, dil):
        init = math.ceil(inter / 2)
        t1 = self.conv(base + ".primary_conv", x)
        t2 = self.conv(base + ".cheap_operation", t1)
        t = self.cat(base + ".cat", [t1, t2])
        return t[:, :inter]

    def ghost_module(self, base, x, cin, inter, dlist, residual):
        t = x
        c = cin
        for i, d in enumerate(dlist):
            tb = self.ghost_block("%s.ghost_layers.%d" % (base, i), t, c, inter, d)
            c += inter
            t = self.cat("%s.cat.%d" % (base, i), [tb, t])
        t = self.conv(base + ".conv_proj", t)
        if residual:
            t = self.add(base + ".residual", t, x)
        return t

    def sam(self, base, x, cin, inter):
        dd = (1, 2, 3, 2, 1)
        y0 = self.ghost_module(base + ".basic_block", x, cin, inter, dd, False)
        t2 = self.interp(base + ".down2", x)
        y2 = self.ghost_module(base + ".basic_block_2", t2, cin, inter, dd, False)
        t4 = self.interp(base + ".down4", x)
        y4 = self.ghost_module(base + ".basic_block_4", t4, cin, inter, dd, False)
        y2u = self.interp(base + ".up2", y2)
        y4u = self.interp(base + ".up4", y4)
        tc = self.cat(base + ".fuse_cat", [y0, y2u, y4u])
        attn = self.eca(base + ".fusion", tc)
        return self.sam_sum(base + ".sum", y0, y2u, y4u, attn, x)

    def encoder_level(self, base, x, feat, level):
        cur = self.ghost_module(base + ".rdb", x, feat, EN_I, (1, 2, 1), True)
        cur = self.sam(base + ".sam_blocks.0", cur, feat, EN_I)
        if level < 3:
            down = self.conv(base + ".down", cur)
            return cur, down
        return cur, None

    def decoder_level(self, base, x, feat, need_feat):
        cur = self.ghost_module(base + ".rdb", x, feat, DE_I, (1, 2, 1), True)
        cur = self.sam(base + ".sam_blocks.0", cur, feat, DE_I)
        t_out = self.conv(base + ".out_conv", cur)
        t_img = self.rec(base + ".shuffle", F.pixel_shuffle(t_out, 2))
        feat_t = self.interp(base + ".feat", cur) if need_feat else None
        return t_img, feat_t

    def run(self, x_in):
        self.rec("pre.unshuffle", x_in)
        x = F.pixel_unshuffle(x_in, 2)
        t1 = self.conv("encoder.conv_first", x)
        y1, down1 = self.encoder_level("encoder.encoder_1", t1, EN_F, 1)
        y2, down2 = self.encoder_level("encoder.encoder_2", down1, 2 * EN_F, 2)
        y3, _ = self.encoder_level("encoder.encoder_3", down2, 4 * EN_F, 3)
        x3 = self.conv("decoder.preconv_3", y3)
        out3, feat3 = self.decoder_level("decoder.decoder_3", x3, DE_F, True)
        x2 = self.cat("decoder.cat2", [y2, feat3])
        x2c = self.conv("decoder.preconv_2", x2)
        out2, feat2 = self.decoder_level("decoder.decoder_2", x2c, DE_F, True)
        x1 = self.cat("decoder.cat1", [y1, feat2])
        x1c = self.conv("decoder.preconv_1", x1)
        out1, _ = self.decoder_level("decoder.decoder_1", x1c, DE_F, False)
        return out1


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def write_build_info(out_dir, manifest_path, manifest, ckpt, images, crop,
                     allow_resize, psnr, cross_diff, blob_size, clip_pct=100.0):
    """写 `build_info.json`：这份部署包的**溯源快照**。

    对齐上游的做法：`runs/<run>/config.yaml` 是把"这次训练到底用了什么"钉在产物里。
    部署包同理——换机器/换人接手时，"这份包是谁、用什么、哪天生成的"必须能自答。

    另外一个用处：`files_sha256` 让另一台机器能验证包**有没有被改过**
    （`check_pack_ref.py` 查的是语义一致，这里查的是字节一致），
    搬运到板子后怀疑拷坏了，先比哈希。
    """
    info = {
        "_readme": "部署包溯源记录（export_network.py 自动生成）。手工替换过包内容请一并更新。",
        "generated": datetime.date.today().isoformat(),
        "manifest": os.path.basename(manifest_path),
        "input_hw": list(manifest["input_hw"]),
        "student_ckpt": os.path.basename(ckpt),
        "calib": {
            "images": len(images),
            "crop": crop if not allow_resize else crop + " + 允许缩放",
            "note": "只裁不缩（上游铁律：resize 会让摩尔纹频率混叠变形）；"
                    "第一张兼作部署参考",
        },
        "quant": {
            "scheme": "PTQ int8，逐张量标度",
            "detail": "conv 权重 int8、偏置 int32、逐层 shift 右移；ECA 注意力保留 fp32",
            "clip_pct": clip_pct,
            "clip_note": "标定幅度用的 |x| 分位数；100 = 按 |max|（最保守）",
            "not_done": "无 QAT；无逐通道缩放（那要 PL 加逐通道寄存器，"
                        "见 docs/交接要求对照.md 缺口 1）",
        },
        "measured": {
            "psnr_db_calib": round(float(psnr), 2),
            "blob_size": blob_size,
        },
    }
    if cross_diff is not None:
        info["measured"]["cross_check_max_abs_diff"] = cross_diff
        info["measured"]["cross_check_note"] = "与真实 PDNet 模型前向交叉验证（--repo）"

    board = os.path.abspath(os.path.join(HERE, "..", "..", "board"))
    for rel, key in (("overlay_engine_150mhz/design_1.bit", "bitstream"),
                     ("xsa/design_1.xsa", "xsa")):
        p = os.path.join(board, rel)
        if os.path.exists(p):
            info[key] = {"path": "board/" + rel, "sha256": _sha256(p)}

    info["files_sha256"] = {}
    for name in sorted(os.listdir(out_dir)):
        if name == "build_info.json" or not os.path.isfile(os.path.join(out_dir, name)):
            continue
        info["files_sha256"][name] = _sha256(os.path.join(out_dir, name))

    with open(os.path.join(out_dir, "build_info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    print("已写 build_info.json（溯源快照）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--manifest", default=os.path.join(HERE, "..", "manifest_640.json"))
    ap.add_argument("--image", action="append", default=None,
                    help="标定图，可重复给多张（第一张兼作部署参考）")
    ap.add_argument("--crop", choices=["center", "topleft"], default="center",
                    help="从原图哪里裁（默认居中）。**只裁不缩**——上游铁律，"
                         "resize 会把摩尔纹频率混叠变形，见 load_image 注释")
    ap.add_argument("--allow-resize", action="store_true",
                    help="图比目标画幅小时允许缩放（默认禁止，宁可直接报错）")
    ap.add_argument("--clip-pct", type=float, default=100.0,
                    help="标定用的分位数：100=按 |max|（现状，最保守）；"
                         "99.9 / 99.5 / 99 等=按分位裁剪，压制激活离群值"
                         "（上游 QuantDemoire 警告的那件事；不动硬件，只改标度）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", default=None, help="PDNet model 目录（可选，做交叉验证）")
    a = ap.parse_args()

    with open(a.manifest, "r", encoding="utf-8") as f:
        m = json.load(f)
    H, W = m["input_hw"]
    sd = torch.load(a.ckpt, map_location="cpu", weights_only=False)["state_dict"]
    if a.image:
        imgs = [load_image(p, H, W, a.crop, a.allow_resize) for p in a.image]
    else:
        imgs = [torch.rand(1, 3, H, W)]
        print("警告：未给 --image，使用随机输入做标定（建议传真实图片）")
    x_in = imgs[0]

    fp = FloatPass(sd, m["ops"], clip_pct=a.clip_pct)
    out1 = None
    with torch.no_grad():
        for i, xi in enumerate(imgs):
            o = fp.run(xi)
            if i == 0:
                out1 = o
            print("标定 %d/%d：%s"
                  % (i + 1, len(imgs), a.image[i] if a.image else "random"),
                  flush=True)
    print("浮点前向完成，out1 范围 [%.3f, %.3f]" % (float(out1.min()), float(out1.max())))
    if a.clip_pct < 100.0:
        print("标定口径：|x| 的 %.4g 分位数（压制离群值）" % a.clip_pct)

    cross_diff = None
    if a.repo:
        # --repo 允许给两种层级（都有人会这么写，别让一个层级问题耗掉半小时）：
        #   vendor/PDNet        仓库根  → 自动再插一层 model/
        #   vendor/PDNet/model  模型目录 → 直接用
        repo = os.path.abspath(a.repo)
        cands = [repo, os.path.join(repo, "model")]
        if not any(os.path.exists(os.path.join(p, "nets_student.py")) for p in cands):
            raise SystemExit(
                "--repo 下找不到 nets_student.py，试过：\n  %s\n"
                "请给 PDNet 的仓库根（vendor/PDNet）或模型目录（vendor/PDNet/model）。"
                % "\n  ".join(cands))
        for p in reversed(cands):
            sys.path.insert(0, p)
        from nets_student import my_model          # type: ignore
        model = my_model(EN_F, EN_I, DE_F, DE_I, 1)
        model.load_state_dict(sd, strict=True)
        model.eval()
        with torch.no_grad():
            ref = model(x_in)[0]
        d = float((ref - out1).abs().max())
        cross_diff = d
        print("交叉验证: 与真实模型 max|diff| = %.3e" % d)
        if d > 1e-3:
            raise SystemExit("交叉验证失败：重算前向与真实模型不一致，先修镜像再导出！")

    # ---- 标度与权重包 ----
    scales = {m["io"]["t_in"]: 1.0 / 127.0}
    amp = {}          # 张量名 -> 浮点 |max|（标定），供汇合点定标度
    blob = bytearray()
    layers = {}
    eca = {}
    last_conv = {}

    def put(b, align=4):
        while len(blob) % align:
            blob.append(0)
        off = len(blob)
        blob.extend(b)
        return off

    def s_of(t):
        return scales.get(t, 1.0)

    for op in m["ops"]:
        k = op["op"]
        out_t = op["out"][0]
        _cal = fp.clip_val(op["name"], None)
        if _cal is not None:
            amp[out_t] = float(_cal)
        if k == "conv":
            key = op["weights_key"]
            w = sd[key + ".weight"].float().numpy()
            b = sd.get(key + ".bias")
            b = b.float().numpy() if b is not None else np.zeros(w.shape[0], np.float32)
            s_in = s_of(op["in"][0])
            sw = max(float(np.abs(w).max()) / 127.0, 1e-12)
            b_q = np.rint(b / (s_in * sw)).astype(np.int64)
            if np.any(np.abs(b_q) > 2**31 - 1):
                print("警告: %s bias 折算溢出，已截断" % key)
                b_q = np.clip(b_q, -(2**31), 2**31 - 1)
            # 标定 → 目标输出标度；1.05 余量防量化后轻微溢出
            target = max(fp.clip_val(op["name"]) * 1.05 / 127.0, 1e-12)
            # 输出标度：s_out = s_in·sw·2^shift（右移放大标度）；
            # 选 shift 让 s_out 尽量贴近标定目标 target
            ratio = target / max(s_in * sw, 1e-12)
            shift = int(round(math.log2(ratio))) if ratio > 0 else 1
            shift = max(1, min(31, shift))
            s_out = s_in * sw * (2 ** shift)
            scales[out_t] = s_out
            w_q = np.clip(np.rint(w / sw), -128, 127).astype(np.int8)
            w_off = put(w_q.tobytes())
            b_off = put(b_q.astype("<i4").tobytes())
            layers[key] = {"w_off": w_off, "w_len": int(w_q.size),
                           "w_shape": list(w_q.shape), "b_off": b_off,
                           "b_len": int(b_q.size * 4), "shift": shift,
                           "sw": sw, "scale_in": float(s_in),
                           "scale_out": float(s_out),
                           "relu": int(bool(op["relu"]))}
            last_conv[key] = op
        elif k in ("interp", "pixel_unshuffle", "pixel_shuffle", "slice"):
            scales[out_t] = s_of(op["in"][0])
        elif k == "cat":
            # 拼接：输出标度必须容纳所有输入，否则大范围输入被 clip 到 ±127
            a_max = 0.0
            for t in op["in"]:
                a_i = amp.get(t)
                if a_i is None:
                    a_i = 127.0 * s_of(t)
                a_max = max(a_max, a_i)
            scales[out_t] = max(a_max * 1.05 / 127.0, 1e-12)
        elif k == "add":
            # 残差相加：按输出标定幅度定标度
            a_y = _cal if _cal is not None else 127.0 * max(s_of(t) for t in op["in"])
            scales[out_t] = max(a_y * 1.05 / 127.0, 1e-12)
        elif k == "sam_sum":
            a_y = _cal if _cal is not None else 127.0 * max(s_of(t) for t in op["in"][:3])
            scales[out_t] = max(a_y * 1.05 / 127.0, 1e-12)
        elif k == "eca":
            eca[op["weights_key"]] = {
                "conv1": sd[op["weights_key"] + ".conv1.weight"].float().reshape(-1).tolist(),
                "conv2": sd[op["weights_key"] + ".conv2.weight"].float().reshape(-1).tolist(),
            }

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "weights.bin"), "wb") as f:
        f.write(bytes(blob))
    index = {"layers": layers, "eca": eca, "scales": scales, "blob_size": len(blob)}
    with open(os.path.join(a.out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)

    # ---- 部署参考（跑我们的量化管线；同时是 PS 调度层的集成测试）----
    x_q = np.clip(np.rint(x_in.numpy()[0] * 127.0), 0, 127).astype(np.int8)  # (3,H,W)
    x_q.tofile(os.path.join(a.out, "input_q.bin"))
    from runner import Runner                      # noqa: E402
    r = Runner(a.manifest, a.out)
    out_q, dt = r.run(x_q)
    out_q.tofile(os.path.join(a.out, "ref_out.bin"))
    s_out1 = scales[m["io"]["out1"]]
    ref_float = np.clip(out1.numpy()[0], 0, 1)
    ref_float.tofile(os.path.join(a.out, "ref_out_float.bin"))
    rec = np.clip(out_q.astype(np.float64) * s_out1, 0, 1)
    mse = float(np.mean((rec - ref_float) ** 2))
    psnr = 99.0 if mse == 0 else 10 * math.log10(1.0 / mse)
    with open(os.path.join(a.out, "ref_psnr.txt"), "w") as f:
        f.write("PSNR(quantized-ref vs float) = %.2f dB\n" % psnr)
    write_build_info(a.out, a.manifest, m, a.ckpt, a.image or [], a.crop,
                     a.allow_resize, psnr, cross_diff, len(blob), a.clip_pct)
    print("导出完成: %d 层, blob %.1f MB, 量化参考用时 %.1fs" %
          (len(layers), len(blob) / 1e6, dt))
    print("PSNR(量化参考 vs 浮点) = %.2f dB（记录于 ref_psnr.txt）" % psnr)


if __name__ == "__main__":
    main()
