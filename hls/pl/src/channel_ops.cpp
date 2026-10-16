/* ===========================================================================
 * channel_ops.cpp —— 逐元素、归约、ECA 的通道轴卷积
 * ===========================================================================
 * 这一批算子单个都不贵，但条数多（add 24 / concat 224 / sigmoid 6 / …），
 * 且每一个都意味着一次 DDR 往返。M4 的优化重点是**把它们和卷积融合**，
 * 而不是把它们做快。
 *
 * 两个必须留意的语义细节：
 *   1. `global_avg_pool` 是**屏障算子**：要等整幅特征写完才能出结果，
 *      所以它把"整网无停顿流水"的假设打断。整网流水线要在这里分段。
 *   2. `mul_channel` 是 (1,1,C) 对 (H,W,C) 的**广播**乘，
 *      不是逐元素乘 —— 别按逐元素去实现。
 * ======================================================================== */
#include "demoir_ops.h"

#include <math.h>

void demoir_relu(demoir_t *x, int n) {
#pragma HLS PIPELINE II=1
    for (int i = 0; i < n; ++i) {
        if (x[i] < (demoir_t)0) x[i] = (demoir_t)0;
    }
}

void demoir_sigmoid(demoir_t *x, int n) {
#pragma HLS PIPELINE II=1
    for (int i = 0; i < n; ++i) {
        x[i] = (demoir_t)(1.0 / (1.0 + exp(-(double)x[i])));
    }
}

void demoir_add(const demoir_t *a, const demoir_t *b, demoir_t *out, int n) {
#pragma HLS PIPELINE II=1
    for (int i = 0; i < n; ++i) {
        out[i] = a[i] + b[i];
    }
}

void demoir_mul_channel(const demoir_t *attn, const demoir_t *y, demoir_t *out,
                        int C, int HW, int chunk) {
    const demoir_t *w = attn + chunk * C;        /* 切片 = 指针偏移，零拷贝 */
    for (int p = 0; p < HW; ++p) {
#pragma HLS PIPELINE II=1
        for (int c = 0; c < C; ++c) {
            out[p * C + c] = w[c] * y[p * C + c];
        }
    }
}

void demoir_global_avg_pool(const demoir_t *in, demoir_t *out, int C, int HW) {
    for (int c = 0; c < C; ++c) {
        out[c] = (demoir_t)0;
    }
    /* 通道在最内层 -> 每个通道一个独立累加器，天然是分块求和，不是长链累加。
     * 定点阶段这个累加器要单独定位宽：HW 最大 65536，float 尚可，int8 直接溢出。 */
    for (int p = 0; p < HW; ++p) {
#pragma HLS PIPELINE II=1
        for (int c = 0; c < C; ++c) {
            out[c] += in[p * C + c];
        }
    }
    const demoir_t inv = (demoir_t)(1.0 / (double)HW);
#pragma HLS PIPELINE II=1
    for (int c = 0; c < C; ++c) {
        out[c] *= inv;
    }
}

void demoir_conv1d_c(const demoir_t *in, const demoir_t *w, demoir_t *out,
                     int C, int k, int pad, int relu) {
    for (int c = 0; c < C; ++c) {
        demoir_t acc = (demoir_t)0;
#pragma HLS PIPELINE II=1
        for (int j = 0; j < k; ++j) {
            const int idx = c + j - pad;
            if (idx < 0 || idx >= C) continue;      /* 零填充 */
            acc += in[idx] * w[j];
        }
        if (relu && acc < (demoir_t)0) acc = (demoir_t)0;
        out[c] = acc;
    }
}

void demoir_eca_fused(const demoir_t *in, const demoir_t *w_conv2, const demoir_t *w_conv1,
                      demoir_t *out, demoir_t *scratch, int C_total, int HW) {
    /* 顺序照抄 nets_student.ECA.forward：先 conv2+relu，再 shuffle，再 conv1，再 unshuffle */
    demoir_global_avg_pool(in, scratch, C_total, HW);
    demoir_conv1d_c(scratch, w_conv2, out, C_total, 9, 4, 1);
    demoir_channel_shuffle(out, scratch, C_total, 1, 3);
    demoir_conv1d_c(scratch, w_conv1, out, C_total, 9, 4, 0);
    demoir_channel_unshuffle(out, scratch, C_total, 1, 3);
    demoir_sigmoid(scratch, C_total);
#pragma HLS PIPELINE II=1
    for (int c = 0; c < C_total; ++c) {
        out[c] = scratch[c];
    }
}
