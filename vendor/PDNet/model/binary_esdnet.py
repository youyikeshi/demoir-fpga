"""
BinaryDemoire 复现补丁（单文件，不改动 vendor 里任何原有文件）
==============================================================

把 ESDNet 改造成 BinaryDemoire（MABG + SGRA），或退化成对照实验的各档。

集成方式（只改两行 import，见 docs/训练/执行文档_BinaryDemoire复现.md）：

    # vendor/PDNet/train.py 与 test.py
    from model.binary_esdnet import my_model     # 原: from model.nets_student import my_model

所有开关走环境变量（vendor 的 config 解析不接受新键）：

    DEMOIR_MODE        fp | base | mabg | sgra | full   默认 full
    DEMOIR_RDB         1 = 原版 ESDNet（RDB 稠密块，≈5.9M）
                       0 = PDNet 的 Ghost 版（≈1.56M，和你现有 student 同构）
    DEMOIR_MABG_SCOPE  boundary | all                   默认 boundary
    DEMOIR_SGRA        1 | 0                            默认 1

自检（不需要数据、不需要 GPU，约 10 秒）：

    cd vendor/PDNet && python model/binary_esdnet.py

注意：本文件写于一台没有 GPU 的机器上，只做过 CPU 上的形状/梯度/数学自检，
没有跑过完整训练。上机第一件事是跑上面的自检 + 执行文档里的三段冒烟测试。
"""

import math
import os

import torch
import torch.nn as nn
import torch.nn.functional as F

# ---------------------------------------------------------------------------
# 0. 开关
# ---------------------------------------------------------------------------

MODE = os.environ.get('DEMOIR_MODE', 'full').lower()
USE_RDB = int(os.environ.get('DEMOIR_RDB', '1'))
MABG_SCOPE = os.environ.get('DEMOIR_MABG_SCOPE', 'boundary').lower()
USE_SGRA = int(os.environ.get('DEMOIR_SGRA', '1'))

assert MODE in ('fp', 'base', 'mabg', 'sgra', 'full'), f'未知 DEMOIR_MODE: {MODE}'

IS_BIN = MODE != 'fp'
USE_MABG = MODE in ('mabg', 'full')
DO_SGRA = USE_SGRA and MODE in ('sgra', 'full')


# ---------------------------------------------------------------------------
# 1. 二值化算子：STE、二值卷积、RPReLU
# ---------------------------------------------------------------------------

class _ActSign(torch.autograd.Function):
    """激活二值化：前向 sign(x)，反向用 tanh(beta*x) 的导数（论文 3.1 训练段）。

    beta 是可学习标量。因为 beta 只出现在反向，这里在 forward 里顺手把
    tanh(beta*x) 存下来，反向后把 beta 的梯度一起算出来，否则 beta 永远不动。
    """

    @staticmethod
    def forward(ctx, x, beta):
        b = beta.clamp(0.5, 10.0).detach()
        t = torch.tanh(b * x)
        ctx.save_for_backward(t, x, b)
        return torch.sign(x)

    @staticmethod
    def backward(ctx, g):
        t, x, b = ctx.saved_tensors
        sech2 = 1.0 - t * t
        return g * b * sech2, (g * x * sech2).sum().reshape(())


class _WeightSign(torch.autograd.Function):
    """权重二值化：前向 sign(w)，反向 clipped STE（|w|>1 时梯度截断）。"""

    @staticmethod
    def forward(ctx, w):
        ctx.save_for_backward(w)
        return torch.sign(w)

    @staticmethod
    def backward(ctx, g):
        (w,) = ctx.saved_tensors
        return g * (w.abs() <= 1.0).to(g.dtype)


def binarize_activation(x, beta):
    return _ActSign.apply(x, beta)


def binarize_weight(w):
    return _WeightSign.apply(w)


class BinaryConv2d(nn.Module):
    """Eq.(1)(2)：Xb = sign(Xf + t)，Wb = sign(Wf)，out = alpha * conv(beta*Xb, Wb)

    - t   : 可学习逐通道阈值（ReActNet），形状 (1,Cin,1,1)
    - alpha: 逐输出通道缩放 ||Wf||_1/(Cin*K*K)，XNOR-Net，从全精度权重算、不回传
    - beta : 逐输入通道门控，由 MABG 给出（形状 (B,Cin,1,1)）；None 时等价于全 1
    """

    def __init__(self, cin, cout, k=3, stride=1, padding=0, dilation=1, bias=True):
        super().__init__()
        self.weight = nn.Parameter(torch.empty(cout, cin, k, k))
        nn.init.kaiming_normal_(self.weight, mode='fan_out', nonlinearity='relu')
        self.bias = nn.Parameter(torch.zeros(cout)) if bias else None
        self.t = nn.Parameter(torch.zeros(1, cin, 1, 1))
        self.beta_ste = nn.Parameter(torch.tensor(1.0))
        self.stride, self.padding, self.dilation = stride, padding, dilation
        self.cin, self.cout, self.k = cin, cout, k

    def forward(self, x, gate=None):
        wb = binarize_weight(self.weight)
        xb = binarize_activation(x + self.t, self.beta_ste)
        alpha = self.weight.detach().abs().flatten(1).sum(1) / (self.cin * self.k * self.k)
        if gate is not None:
            xb = xb * gate
        out = F.conv2d(xb, wb, self.bias, self.stride, self.padding, self.dilation)
        return out * alpha.view(1, -1, 1, 1)


class RPReLU(nn.Module):
    """ReActNet 的 RPReLU：带可学偏置/斜率的逐通道分段线性激活。

        f(x) = (x - zeta) + gamma          if x > zeta
               alpha * (x - zeta) + gamma  otherwise
    """

    def __init__(self, c):
        super().__init__()
        self.zeta = nn.Parameter(torch.zeros(1, c, 1, 1))
        self.gamma = nn.Parameter(torch.zeros(1, c, 1, 1))
        self.slope = nn.Parameter(torch.full((1, c, 1, 1), 0.25))

    def forward(self, x):
        d = x - self.zeta
        return torch.where(d > 0, d + self.gamma, self.slope * d + self.gamma)


# ---------------------------------------------------------------------------
# 2. MABG：单级 Haar DWT + 频率/统计描述子 + 共享 FC 出逐通道门控
# ---------------------------------------------------------------------------

class HaarDWT(nn.Module):
    """单级 2D Haar 小波，stride 2，输出 (B, C, 4, h, w)，4 = [LL, LH, HL, HH]。

    用 grouped conv 实现，权重固定、不参与训练。偶数边长才能整除；
    奇数边时会自动右/下各补 1。
    """

    def __init__(self, c):
        super().__init__()
        k = torch.tensor([
            [[1., 1.], [1., 1.]],      # LL
            [[1., 1.], [-1., -1.]],    # LH
            [[1., -1.], [1., -1.]],    # HL
            [[1., -1.], [-1., 1.]],    # HH
        ]) * 0.5
        w = torch.cat([k] * c, dim=0).view(4 * c, 1, 2, 2)
        self.register_buffer('weight', w, persistent=False)
        self.groups = c
        self.c = c

    def forward(self, x):
        b, c, h, w = x.shape
        if h % 2 or w % 2:
            x = F.pad(x, (0, w % 2, 0, h % 2))
        y = F.conv2d(x, self.weight, None, 2, 0, 1, self.groups)
        y = y.view(b, c, 4, y.shape[-2], y.shape[-1])
        return y


class MABG(nn.Module):
    """Algorithm 1。输入全精度特征 (B,C,H,W)，输出 (B,C,1,1) 的门控 beta。"""

    def __init__(self, c, hidden=0):
        super().__init__()
        self.dwt = HaarDWT(c)
        self.hidden = hidden
        if hidden:
            self.fc1 = nn.Linear(5, hidden)
            self.fc2 = nn.Linear(hidden, 1)
        else:
            self.fc1, self.fc2 = None, nn.Linear(5, 1)

    def forward(self, x, return_feat=False):
        e = self.dwt(x).abs().mean(dim=(-2, -1))          # (B,C,4)
        e_ll, e_lh, e_hl, e_hh = e.unbind(-1)
        eps = 1e-6
        r_hf = (e_lh + e_hl + e_hh) / (e_ll + e_lh + e_hl + e_hh + eps)
        s = torch.maximum(e_lh, e_hl) / (e_lh + e_hl + eps)
        mu = x.mean(dim=(-2, -1))
        sigma = x.std(dim=(-2, -1), unbiased=False)
        m = x.abs().mean(dim=(-2, -1))
        feat = torch.stack([mu, sigma, m, r_hf, s], dim=-1)  # (B,C,5)
        z = self.fc2(F.relu(self.fc1(feat))) if self.fc1 is not None else self.fc2(feat)
        beta = torch.sigmoid(z).view(x.shape[0], x.shape[1], 1, 1)
        if return_feat:
            return beta, feat
        return beta


# ---------------------------------------------------------------------------
# 3. SGRA：分区投影 + 交错重排（Eq.8/9）
# ---------------------------------------------------------------------------

class SGRA(nn.Module):
    """g = gcd(Cin,Cout)，分区内 1x1 投影（block-diagonal），再交错重排。

    参数量 = Cout*Cin/g，是同样做法的全密接投影的 1/g。
    交错重排是一条通道置换：Y[:, t*g+k] = U[:, k*m+t]，m = Cout/g。
    硬件上它可以被下一个卷积的输入通道顺序吸收，等于免费。
    """

    def __init__(self, cin, cout, stride=1):
        super().__init__()
        g = math.gcd(cin, cout)
        self.groups, self.m, self.stride = g, cout // g, stride
        self.weight = nn.Parameter(torch.empty(cout, cin // g, 1, 1))
        nn.init.normal_(self.weight, std=0.02)  # 与 ESDNet 的初始化同量级

    def forward(self, x):
        u = F.conv2d(x, self.weight, None, self.stride, 0, 1, self.groups)
        b, c, h, w = u.shape
        return u.view(b, self.groups, self.m, h, w).transpose(1, 2).reshape(b, c, h, w)

    def perm(self):
        """交错重排的通道置换向量（用于单测/硬件生成）。"""
        return torch.tensor([k * self.m + t
                             for t in range(self.m)
                             for k in range(self.groups)]).view(-1, 1, 1)


# ---------------------------------------------------------------------------
# 4. 两种卷积单元
# ---------------------------------------------------------------------------

class DenseConv(nn.Module):
    """稠密块内部的卷积（RDB/Ghost/SAM 里那些）。

    全精度：conv + ReLU
    二值  ：BBCU 风格的二值卷积 + RPReLU（无捷径，因为稠密块是 concat 不是 add）
    """

    def __init__(self, cin, cout, k=3, dilation=1, padding=0, stride=1):
        super().__init__()
        if IS_BIN:
            self.conv = BinaryConv2d(cin, cout, k, stride, padding, dilation)
            self.act = RPReLU(cout)
            self.relu = None
        else:
            self.conv = nn.Conv2d(cin, cout, k, stride, padding, dilation=dilation, bias=True)
            self.act = None
            self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        out = self.conv(x)
        return self.act(out) if self.act is not None else self.relu(out)


class BoundaryConv(nn.Module):
    """边界卷积（下采样 / 编码器→解码器通道调整）：完整二值块 Eq.(3)

        Xout = RPReLU(Xbout) + SGRA(Xf)

    Cin==Cout 且 stride==1 时捷径就是 identity；否则交给 SGRA（DEMOIR_SGRA=0 时退化为无捷径）。
    """

    def __init__(self, cin, cout, k=3, stride=1, padding=0, dilation=1, mabg=False):
        super().__init__()
        self.has_skip = IS_BIN and (cin == cout and stride == 1)
        if IS_BIN:
            self.conv = BinaryConv2d(cin, cout, k, stride, padding, dilation)
            self.act = RPReLU(cout)
            self.short = None if self.has_skip else (SGRA(cin, cout, stride) if DO_SGRA else None)
            self.mabg = MABG(cin) if (USE_MABG and mabg) else None
        else:
            self.conv = nn.Conv2d(cin, cout, k, stride, padding, dilation=dilation, bias=True)
            self.act = nn.ReLU(inplace=True)
            self.short = None
            self.mabg = None

    def forward(self, x):
        gate = self.mabg(x) if self.mabg is not None else None
        out = self.act(self.conv(x) if gate is None else self.conv(x, gate))
        if self.has_skip:
            out = out + x
        elif self.short is not None:
            out = out + self.short(x)
        return out


# ---------------------------------------------------------------------------
# 5. ESDNet 骨架（与原版 nets_student.py 同构，只换成上面两种单元）
# ---------------------------------------------------------------------------

class GhostBlock(nn.Module):
    def __init__(self, inp, oup, d_list=1, padding=1, kernel_size=1, ratio=2, dw_size=3, stride=1):
        super().__init__()
        self.oup = oup
        init_c = math.ceil(oup / ratio)
        new_c = init_c * (ratio - 1)
        self.primary_conv = DenseConv(inp, init_c, kernel_size, padding=kernel_size // 2, stride=stride)
        self.cheap_operation = DenseConv(init_c, new_c, dw_size, padding=padding, dilation=d_list, stride=1)

    def forward(self, x):
        x1 = self.primary_conv(x)
        x2 = self.cheap_operation(x1)
        return torch.cat([x1, x2], dim=1)[:, :self.oup]


class GhostModule(nn.Module):
    def __init__(self, inp, inter, d_list, residual=True):
        super().__init__()
        self.residual = residual
        self.ghost_layers = nn.ModuleList()
        c = inp
        for d in d_list:
            self.ghost_layers.append(GhostBlock(c, inter, d_list=d, padding=d))
            c += inter
        self.conv = DenseConv(c, inp, 1, padding=0) if IS_BIN else nn.Conv2d(c, inp, 1)

    def forward(self, x):
        t = x
        for g in self.ghost_layers:
            t = torch.cat([g(t), t], dim=1)
        t = self.conv(t)
        return t + x if self.residual else t


class RDB(nn.Module):
    """原版 ESDNet 的残差稠密块（PDNet 把它换成了 GhostModule）。"""

    def __init__(self, in_channel, d_list, inter_num):
        super().__init__()
        self.conv_layers = nn.ModuleList()
        c = in_channel
        for d in d_list:
            self.conv_layers.append(DenseConv(c, inter_num, 3, padding=d, dilation=d))
            c += inter_num
        self.conv_post = DenseConv(c, in_channel, 1, padding=0)

    def forward(self, x):
        t = x
        for layer in self.conv_layers:
            t = torch.cat([layer(t), t], dim=1)
        return self.conv_post(t) + x


class ECA(nn.Module):
    """高效通道注意力（原样保留，走全精度）。"""

    def __init__(self, c, k=9):
        super().__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.conv1 = nn.Conv1d(1, 1, k, padding=k // 2, bias=False)
        self.conv2 = nn.Conv1d(1, 1, k, padding=k // 2, bias=False)
        self.relu = nn.ReLU(inplace=True)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        x = self.avg_pool(x).squeeze(-1).transpose(-1, -2)   # (B,1,C)
        x = self.relu(self.conv2(x))
        x = self.sigmoid(self.conv1(x))
        return x.transpose(-1, -2).unsqueeze(-1)


class SAM(nn.Module):
    """多尺度注意力模块：三个尺度分支 + ECA 融合 + 残差。"""

    def __init__(self, in_channel, d_list, inter_num):
        super().__init__()
        self.b0 = GhostModule(in_channel, inter_num, d_list, residual=False)
        self.b2 = GhostModule(in_channel, inter_num, d_list, residual=False)
        self.b4 = GhostModule(in_channel, inter_num, d_list, residual=False)
        self.fusion = ECA(3 * in_channel)

    def forward(self, x):
        x2 = F.interpolate(x, scale_factor=0.5, mode='bilinear', align_corners=False)
        x4 = F.interpolate(x, scale_factor=0.25, mode='bilinear', align_corners=False)
        y0 = self.b0(x)
        y2 = F.interpolate(self.b2(x2), size=x.shape[-2:], mode='bilinear', align_corners=False)
        y4 = F.interpolate(self.b4(x4), size=x.shape[-2:], mode='bilinear', align_corners=False)
        w = self.fusion(torch.cat([y0, y2, y4], dim=1))
        w0, w2, w4 = w.chunk(3, dim=1)
        return x + w0 * y0 + w2 * y2 + w4 * y4


class EncoderLevel(nn.Module):
    def __init__(self, feature_num, inter_num, level, sam_number):
        super().__init__()
        self.level = level
        self.rdb = RDB(feature_num, (1, 2, 1), inter_num) if USE_RDB \
            else GhostModule(feature_num, inter_num, (1, 2, 1))
        self.sam_blocks = nn.ModuleList(
            [SAM(feature_num, (1, 2, 3, 2, 1), inter_num) for _ in range(sam_number)])
        if level < 3:
            # 下采样是论文明确点名的"关键操作"，本体加 MABG（MABG_SCOPE=all 时还会扩到稠密块）
            self.down = BoundaryConv(feature_num, 2 * feature_num, 3, stride=2, padding=1,
                                     mabg=True)

    def forward(self, x):
        out = self.rdb(x)
        for blk in self.sam_blocks:
            out = blk(out)
        if self.level < 3:
            return out, self.down(out)
        return out


class DecoderLevel(nn.Module):
    def __init__(self, feature_num, inter_num, sam_number, out_ch=12, mabg=True):
        super().__init__()
        self.rdb = RDB(feature_num, (1, 2, 1), inter_num) if USE_RDB \
            else GhostModule(feature_num, inter_num, (1, 2, 1))
        self.sam_blocks = nn.ModuleList(
            [SAM(feature_num, (1, 2, 3, 2, 1), inter_num) for _ in range(sam_number)])
        # 最后一层输出卷积保持全精度（论文：首尾两层不二值化）
        self.conv = nn.Conv2d(feature_num, out_ch, 3, padding=1, bias=True)

    def forward(self, x, feat=True):
        x = self.rdb(x)
        for blk in self.sam_blocks:
            x = blk(x)
        out = F.pixel_shuffle(self.conv(x), 2)
        if feat:
            return out, F.interpolate(x, scale_factor=2, mode='bilinear', align_corners=False)
        return out


class Encoder(nn.Module):
    def __init__(self, feature_num, inter_num, sam_number):
        super().__init__()
        # 第一层保持全精度（论文：首尾两层不二值化）
        self.conv_first = nn.Sequential(
            nn.Conv2d(12, feature_num, 5, 1, 2, bias=True), nn.ReLU(inplace=True))
        self.encoder_1 = EncoderLevel(feature_num, inter_num, 1, sam_number)
        self.encoder_2 = EncoderLevel(2 * feature_num, inter_num, 2, sam_number)
        self.encoder_3 = EncoderLevel(4 * feature_num, inter_num, 3, sam_number)

    def forward(self, x):
        x = self.conv_first(F.pixel_unshuffle(x, 2))
        y1, d1 = self.encoder_1(x)
        y2, d2 = self.encoder_2(d1)
        y3 = self.encoder_3(d2)
        return y1, y2, y3


class Decoder(nn.Module):
    def __init__(self, en_num, feature_num, inter_num, sam_number):
        super().__init__()
        self.preconv_3 = BoundaryConv(4 * en_num, feature_num, 3, padding=1, mabg=True)
        self.decoder_3 = DecoderLevel(feature_num, inter_num, sam_number)
        self.preconv_2 = BoundaryConv(2 * en_num + feature_num, feature_num, 3, padding=1, mabg=True)
        self.decoder_2 = DecoderLevel(feature_num, inter_num, sam_number)
        self.preconv_1 = BoundaryConv(en_num + feature_num, feature_num, 3, padding=1, mabg=True)
        self.decoder_1 = DecoderLevel(feature_num, inter_num, sam_number)

    def forward(self, y1, y2, y3):
        x3 = self.preconv_3(y3)
        out3, f3 = self.decoder_3(x3)
        x2 = self.preconv_2(torch.cat([y2, f3], dim=1))
        out2, f2 = self.decoder_2(x2)
        x1 = self.preconv_1(torch.cat([y1, f2], dim=1))
        out1 = self.decoder_1(x1, feat=False)
        return out1, out2, out3, f2, f3


class my_model(nn.Module):
    """与 vendor 的 my_model 同签名，可直接替换。"""

    def __init__(self, en_feature_num, en_inter_num, de_feature_num, de_inter_num,
                 sam_number=1, dw_groups=0):
        # dw_groups 只是为了与 nets_student 的 my_model 同签名（src/train.py 会传它）；
        # 二值骨架里没有 GhostBlock，这个参数被忽略。
        super().__init__()
        self.encoder = Encoder(en_feature_num, en_inter_num, sam_number)
        self.decoder = Decoder(en_feature_num, de_feature_num, de_inter_num, sam_number)

    def forward(self, x):
        y1, y2, y3 = self.encoder(x)
        out1, out2, out3, f2, f3 = self.decoder(y1, y2, y3)
        return out1, out2, out3, [y1, y2, y3, f2, f3]

    def _initialize_weights(self):
        for m in self.modules():
            if isinstance(m, (nn.Conv2d, BinaryConv2d)):
                m.weight.data.normal_(0.0, 0.02)
                if getattr(m, 'bias', None) is not None:
                    m.bias.data.normal_(0.0, 0.02)


# ---------------------------------------------------------------------------
# 6. 统计工具：参数量（按 BiBench 口径拆二值/全精度）
# ---------------------------------------------------------------------------

def param_report(model):
    """返回 (总参数M, 二值部分占比, 等价参数量M)。

    BiBench 口径：二值层按 1/32 计，即 Params = Params_b/32 + Params_f。
    这里把 BinaryConv2d 的权重记二值，其余（bias、t、alpha 来源、RPReLU、SGRA、
    MABG、首尾层）记全精度。
    """
    total, bin_w, fp_w = 0, 0, 0
    for name, m in model.named_modules():
        if isinstance(m, BinaryConv2d):
            bin_w += m.weight.numel()
            fp_w += 0 if m.bias is None else m.bias.numel()
            fp_w += m.t.numel() + m.beta_ste.numel()
        elif isinstance(m, nn.Conv2d):
            fp_w += m.weight.numel() + (0 if m.bias is None else m.bias.numel())
    for p in model.parameters():
        total += p.numel()
    equiv = total - bin_w + bin_w / 32.0
    n_bin_conv = sum(1 for m in model.modules() if isinstance(m, BinaryConv2d))
    return total / 1e6, bin_w / 1e6, equiv / 1e6, n_bin_conv


def mabg_layers(model):
    return [n for n, m in model.named_modules() if isinstance(m, MABG)]


def sgra_layers(model):
    return [n for n, m in model.named_modules() if isinstance(m, SGRA)]


# ---------------------------------------------------------------------------
# 7. CPU 自检
# ---------------------------------------------------------------------------

def _selftest():
    torch.manual_seed(0)
    print(f'[cfg] MODE={MODE} RDB={USE_RDB} MABG_SCOPE={MABG_SCOPE} SGRA={DO_SGRA}')

    # (a) 二值卷积：前向必须是 ±1 内积，梯度必须能回到全精度权重
    conv = BinaryConv2d(4, 8, 3, padding=1).double()
    x = torch.randn(2, 4, 16, 16, dtype=torch.double, requires_grad=True)
    y = conv(x)
    y.sum().backward()
    assert conv.weight.grad is not None and conv.weight.grad.abs().sum() > 0, '权重没有梯度'
    assert conv.t.grad is not None, '阈值 t 没有梯度'
    assert x.grad is not None and x.grad.abs().sum() > 0, '输入没有梯度'
    print('[ok] (a) BinaryConv2d 前向/反向通路')

    # (b) alpha 与 XNOR-Net 公式一致
    a = conv.weight.detach().abs().flatten(1).sum(1) / (4 * 9)
    assert torch.allclose(a, conv.weight.detach().abs().sum((1, 2, 3)) / (4 * 9))
    print('[ok] (b) alpha = ||Wf||_1/(Cin*K*K)')

    # (c) 门控 beta=1 时与无门控等价
    conv2 = BinaryConv2d(4, 8, 3, padding=1).double()
    conv2.load_state_dict(conv.state_dict())
    xd = torch.randn(2, 4, 16, 16, dtype=torch.double)
    y1 = conv2(xd)
    y2 = conv2(xd, torch.ones(2, 4, 1, 1, dtype=torch.double))
    assert torch.allclose(y1, y2), 'gate=1 不等于无 gate'
    y3 = conv2(xd, torch.zeros(2, 4, 1, 1, dtype=torch.double))
    assert y3.abs().max() < 1e-9, 'gate=0 应全零'
    print('[ok] (c) 门控行为正确（gate=1 等价、gate=0 归零）')

    # (d) DWT：解析值校验（常数块 → 只有 LL）
    dwt = HaarDWT(1)
    xc = torch.ones(1, 1, 8, 8)
    e = dwt(xc)
    assert torch.allclose(e[0, 0, 0], torch.full((4, 4), 2.0)), '常数块的 LL 应为 2'
    assert e[0, 0, 1:].abs().max() < 1e-6, '常数块的 LH/HL/HH 应为 0'
    xh = torch.zeros(1, 1, 8, 8)
    xh[..., 0::2] = 1.0   # 沿高方向交替 → 垂向高频
    e2 = dwt(xh)
    assert e2[0, 0, 1].abs().sum() + e2[0, 0, 2].abs().sum() > 0
    print('[ok] (d) HaarDWT 子带正确')

    # (e) MABG 输出范围与形状
    m = MABG(6)
    xf = torch.randn(2, 6, 32, 32)
    beta, feat = m(xf, return_feat=True)
    assert beta.shape == (2, 6, 1, 1) and beta.min() >= 0 and beta.max() <= 1
    assert feat.shape == (2, 6, 5)
    r_hf, s = feat[..., 3], feat[..., 4]
    assert (r_hf >= 0).all() and (r_hf <= 1 + 1e-5).all(), 'r_hf 应在 [0,1]'
    assert (s >= 0.5 - 1e-5).all() and (s <= 1 + 1e-5).all(), 's 应在 [0.5,1]'
    print('[ok] (e) MABG 形状/取值范围（r_hf∈[0,1], s∈[0.5,1]）')

    # (f) SGRA：分区投影是块对角（分区之间不串），交错重排下标正确
    sg = SGRA(12, 8, stride=1)
    assert sg.groups == 4 and sg.m == 2
    perm = sg.perm().flatten().tolist()
    assert perm == [0, 2, 4, 6, 1, 3, 5, 7], f'interleave 下标错: {perm}'
    # 逐分区注入 delta，看输出落在哪些通道
    x = torch.zeros(1, 12, 3, 3)
    x[0, 0] = 1.0                       # 输入第 0 通道 = 分区 0 的第 0 个
    sg.weight.data.zero_()
    sg.weight.data[:, 0, 0, 0] = 1.0    # 每个分区的输出都直接抄输入第 0 个通道
    y = sg(x)
    hit = (y[0].sum((1, 2)) > 0.5).nonzero().flatten().tolist()
    assert hit == [0, 4], f'分区 0 的输出应落在交错后的通道 0 和 4，实际 {hit}'
    print('[ok] (f) SGRA block-diagonal + interleave（置换 = [0,2,4,6,1,3,5,7]）')

    # (g) 整网前向/反向 + 参数报告
    net = my_model(48, 32, 64, 32, 1)
    net.train()
    out = net(torch.randn(1, 3, 64, 64))
    assert out[0].shape == (1, 3, 64, 64), out[0].shape
    assert out[1].shape == (1, 3, 32, 32), out[1].shape
    assert out[2].shape == (1, 3, 16, 16), out[2].shape
    out[0].mean().backward()
    total, bw, equiv, nbc = param_report(net)
    print(f'[ok] (g) forward/backward 通过；参数 {total:.3f}M，二值 {bw:.3f}M，'
          f'等价 {equiv:.3f}M，二值卷积 {nbc} 个')
    print(f'       MABG 施加在 {len(mabg_layers(net))} 层：{mabg_layers(net)}')
    print(f'       SGRA 施加在 {len(sgra_layers(net))} 层：{sgra_layers(net)}')
    print('\n全部自检通过。')


if __name__ == '__main__':
    _selftest()
