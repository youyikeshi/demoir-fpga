#!/usr/bin/env python3
"""数清 student 网络里所有 Conv2d 的形状。

本机没有 PyTorch，所以这里按 nets_student.py 的结构手工复刻一遍，
只关心每个卷积的 (Cin, Cout, K, stride, pad, dilation)，不涉及数值计算。

输出：按形状分类的清单，以及需要多少种不同的编译配置。
"""

import math
from collections import Counter, defaultdict

# 来自 weights/weights.yaml 的结构参数
EN_F, EN_I, DE_F, DE_I, SAM = 48, 32, 64, 32, 1

shapes = []


def add(label, cin, cout, k, stride=1, pad=0, dil=1):
    shapes.append((cin, cout, k, stride, pad, dil, label))


def ghost_block(prefix, inp, oup, d, ratio=2, dw_size=3,
                kernel_size=1, stride=1):
    init = math.ceil(oup / ratio)
    new = init * (ratio - 1)
    add(prefix + ".primary_conv", inp, init, kernel_size, stride, kernel_size // 2)
    add(prefix + ".cheap_operation", init, new, dw_size, 1, d, d)
    return init, new


def ghost_module(prefix, inp, inter, d_list):
    c = inp
    for i, d in enumerate(d_list):
        ghost_block("%s.ghost_layers.%d" % (prefix, i), c, inter, d)
        c += inter
    add(prefix + ".conv", c, inp, 1)


def ghost_module1(prefix, inp, inter, d_list):
    ghost_module(prefix, inp, inter, d_list)


def sam(prefix, in_ch, d_list, inter):
    for name in ("basic_block", "basic_block_2", "basic_block_4"):
        ghost_module1("%s.%s" % (prefix, name), in_ch, inter, d_list)
    # ECA 里是两个 Conv1d，不属于 Conv2d，跳过


def encoder_level(prefix, feature_num, inter, level):
    ghost_module(prefix + ".rdb", feature_num, inter, (1, 2, 1))
    for s in range(SAM):
        sam("%s.sam_blocks.%d" % (prefix, s), feature_num, (1, 2, 3, 2, 1), inter)
    if level < 3:
        add(prefix + ".down", feature_num, 2 * feature_num, 3, 2, 1)


def decoder_level(prefix, feature_num, inter):
    ghost_module(prefix + ".rdb", feature_num, inter, (1, 2, 1))
    for s in range(SAM):
        sam("%s.sam_blocks.%d" % (prefix, s), feature_num, (1, 2, 3, 2, 1), inter)
    add(prefix + ".conv", feature_num, 12, 3, 1, 1)


# ---- Encoder ----
add("encoder.conv_first", 12, EN_F, 5, 1, 2)
encoder_level("encoder.encoder_1", EN_F, EN_I, 1)
encoder_level("encoder.encoder_2", 2 * EN_F, EN_I, 2)
encoder_level("encoder.encoder_3", 4 * EN_F, EN_I, 3)

# ---- Decoder ----
add("decoder.preconv_3", 4 * EN_F, DE_F, 3, 1, 1)
decoder_level("decoder.decoder_3", DE_F, DE_I)
add("decoder.preconv_2", 2 * EN_F + DE_F, DE_F, 3, 1, 1)
decoder_level("decoder.decoder_2", DE_F, DE_I)
add("decoder.preconv_1", EN_F + DE_F, DE_F, 3, 1, 1)
decoder_level("decoder.decoder_1", DE_F, DE_I)


def main():
    print("Conv2d 实例总数：%d" % len(shapes))

    by_kind = Counter((k, s, p, d) for _, _, k, s, p, d, _ in shapes)
    print("\n=== 按形态分类（K, stride, pad, dilation）===")
    for kind, n in by_kind.most_common():
        print("  K=%d s=%d p=%d d=%d : %d 个" % (kind + (n,)))

    by_shape = Counter((cin, cout, k, s, p, d) for cin, cout, k, s, p, d, _ in shapes)
    print("\n=== 按完整形状分类（Cin→Cout 也算）===")
    print("  需要 %d 种不同的编译配置" % len(by_shape))
    for shape, n in by_shape.most_common():
        cin, cout, k, s, p, d = shape
        print("  %3d→%-3d K=%d s=%d p=%d d=%d : %d 个实例"
              % (cin, cout, k, s, p, d, n))

    print("\n=== 如果按形态特化（同形态共用一份 IP，通道数运行期可配）===")
    print("  只需 %d 种形态" % len(by_kind))

    print("\n=== 每个形态里的实例数 ===")
    for kind, n in by_kind.most_common():
        k, s, p, d = kind
        cins = sorted(set(cin for cin, _, kk, ss, pp, dd, _ in shapes
                          if (kk, ss, pp, dd) == kind))
        couts = sorted(set(cout for _, cout, kk, ss, pp, dd, _ in shapes
                           if (kk, ss, pp, dd) == kind))
        print("  K=%d s=%d p=%d d=%d : %d 个实例，Cin ∈ %s，Cout ∈ %s"
              % (k, s, p, d, n, cins[:8], couts[:8]))


if __name__ == "__main__":
    main()
