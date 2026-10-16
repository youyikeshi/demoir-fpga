/* ===========================================================================
 * conv2d.cpp —— NHWC 直接卷积 + 可选 ReLU
 * ===========================================================================
 * 这是整条线上唯一真正吃算力的算子（247/577 条，占 >99% 的 MAC）。
 * 目前是**可综合但未优化**的形状：外层 co/oh/ow 三重循环，内层
 * (kh,kw,ci) 归约。M2 要把它换成：
 *
 *   * 通道维分块 + `#pragma HLS ARRAY_PARTITION`，让 D_k 个乘法器同时开；
 *   * 行缓冲（line buffer）替代对 DDR 的重复读：目前每个输出点都把
 *     k×k×C_in 的输入窗口重读一遍，DDR 流量是理论最小值的 k² 倍；
 *   * 权重缓存在 BRAM：一张 64→64 k3 的权重只有 147 KB/4 = 36 KB，放得下。
 *
 * 为什么要先写"慢"版本：等价性必须先钉死。融合、分块、量化都会改变
 * 累加顺序，如果此时才发现公式有错，就分不清是哪一个改动引入的。
 * ======================================================================== */
#include "demoir_ops.h"

int demoir_conv_out_size(int size, int k, int stride, int dilation, int pad) {
    return (size + 2 * pad - dilation * (k - 1) - 1) / stride + 1;
}

void demoir_conv2d(const demoir_t *in, const demoir_t *w, const demoir_t *b, demoir_t *out,
                   int C_in, int H, int W, int C_out, int k,
                   int stride, int dilation, int pad, int relu) {
    const int H_out = demoir_conv_out_size(H, k, stride, dilation, pad);
    const int W_out = demoir_conv_out_size(W, k, stride, dilation, pad);

    for (int co = 0; co < C_out; ++co) {
        const demoir_t bias = (b != 0) ? b[co] : (demoir_t)0;
        for (int oh = 0; oh < H_out; ++oh) {
            for (int ow = 0; ow < W_out; ++ow) {
                demoir_t acc = bias;
                for (int kh = 0; kh < k; ++kh) {
                    const int ih = oh * stride + kh * dilation - pad;
                    if (ih < 0 || ih >= H) continue;
                    for (int kw = 0; kw < k; ++kw) {
                        const int iw = ow * stride + kw * dilation - pad;
                        if (iw < 0 || iw >= W) continue;
                        const demoir_t *px = in + ((ih * W) + iw) * C_in;
                        const demoir_t *wk = w + ((co * C_in) * k + kh) * k + kw;
#pragma HLS PIPELINE II=1
                        for (int ci = 0; ci < C_in; ++ci) {
                            /* 输入 NHWC（步长 1），权重 OIHW（通道步长 k*k） */
                            acc += px[ci] * wk[ci * k * k];
                        }
                    }
                }
                if (relu && acc < (demoir_t)0) acc = (demoir_t)0;
                out[((oh * W_out) + ow) * C_out + co] = acc;
            }
        }
    }
}
