/* ===========================================================================
 * rearrange.cpp —— 重排类算子（pixel_unshuffle / pixel_shuffle / channel shuffle / concat）
 * ===========================================================================
 * 这类算子没有乘法，但在原网络里**会物化张量**，因此照样吃 DDR 带宽。
 * 它们是 M4 优化里最该被融合掉的一批：pixel_shuffle 可以和 decoder 的最后一层
 * 卷积合并（改变写出地址），concat 可以和上一层的写出偏移合并。
 * 首版先老老实实做成独立算子，把等价性钉死，再谈融合。
 * ======================================================================== */
#include "demoir_ops.h"

/* 输出的通道总宽 = 各输入通道之和；由 channels 数组在函数内算出，
 * 不再多传一个参数 —— 少一个可能与 buffers 不一致的自由度。 */

void demoir_pixel_unshuffle(const demoir_t *in, demoir_t *out, int H, int W, int C, int r) {
    const int Ho = H / r, Wo = W / r;
    for (int h = 0; h < Ho; ++h) {
        for (int w = 0; w < Wo; ++w) {
            for (int i = 0; i < r; ++i) {
#pragma HLS PIPELINE II=1
                for (int j = 0; j < r; ++j) {
                    for (int c = 0; c < C; ++c) {
                        out[((h * Wo + w) * (C * r * r)) + (c * r * r + i * r + j)] =
                            in[((h * r + i) * W + (w * r + j)) * C + c];
                    }
                }
            }
        }
    }
}

void demoir_pixel_shuffle(const demoir_t *in, demoir_t *out, int H, int W, int C, int r) {
    const int Co = C / (r * r);
    const int Wo = W * r;
    for (int h = 0; h < H; ++h) {
        for (int w = 0; w < W; ++w) {
            for (int i = 0; i < r; ++i) {
#pragma HLS PIPELINE II=1
                for (int j = 0; j < r; ++j) {
                    for (int c = 0; c < Co; ++c) {
                        out[(((h * r + i) * Wo) + (w * r + j)) * Co + c] =
                            in[((h * W + w) * C) + (c * r * r + i * r + j)];
                    }
                }
            }
        }
    }
}

void demoir_channel_shuffle(const demoir_t *in, demoir_t *out, int C, int HW, int groups) {
    const int cpg = C / groups;
    for (int p = 0; p < HW; ++p) {
#pragma HLS PIPELINE II=1
        for (int a = 0; a < groups; ++a) {
            for (int b = 0; b < cpg; ++b) {
                /* in 的 (a,b) -> out 的 (b,a)：out 线性下标 b*groups+a */
                out[p * C + (b * groups + a)] = in[p * C + (a * cpg + b)];
            }
        }
    }
}

void demoir_channel_unshuffle(const demoir_t *in, demoir_t *out, int C, int HW, int groups) {
    const int cpg = C / groups;
    for (int p = 0; p < HW; ++p) {
#pragma HLS PIPELINE II=1
        for (int a = 0; a < cpg; ++a) {
            for (int b = 0; b < groups; ++b) {
                /* in 按 (cpg,groups) 看 -> out 按 (groups,cpg) 看 */
                out[p * C + (b * cpg + a)] = in[p * C + (a * groups + b)];
            }
        }
    }
}

void demoir_concat_c(const demoir_t *const *ins, const int *channels, int n,
                     demoir_t *out, int HW) {
    int c_total = 0;
    for (int t = 0; t < n; ++t) {
        c_total += channels[t];
    }
    int offset = 0;                      /* 当前输入在输出通道轴上的起点 */
    for (int t = 0; t < n; ++t) {
        const demoir_t *src = ins[t];
        const int ch = channels[t];
        for (int p = 0; p < HW; ++p) {
#pragma HLS PIPELINE II=1
            for (int c = 0; c < ch; ++c) {
                out[p * c_total + offset + c] = src[p * ch + c];
            }
        }
        offset += ch;
    }
}
