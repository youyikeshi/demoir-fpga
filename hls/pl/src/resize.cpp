/* ===========================================================================
 * resize.cpp —— 双线性缩放（SAM 的多尺度分支 + decoder 的上采样）
 * ===========================================================================
 * ⚠️ 采样步长必须是 `1/scale_factor`，**不是** `in_size/out_size`。
 * 这两者在 `in × scale` 不是整数时会给出完全不同的结果，而 PyTorch 用前者
 * （实测，见 ps/ops_ref.py:_bilinear_taps 与 tests/test_ops_numpy_vs_torch.py）。
 * 本网络恰好全是 32 的倍数 + 2 的幂缩放，两者重合 —— 但分块推理与换分辨率会立刻踩到。
 *
 * 1/2 与 1/4 下采样在 M4 里可以直接换成"求局部平均"或抽取，
 * 但**先证明公式对**，再谈用便宜的方式近似。
 * ======================================================================== */
#include "demoir_ops.h"

/* ATen 的 area_pixel_compute_source_index(align_corners=false)：
 *   src = max(0, (dst + 0.5) * step - 0.5)
 * 然后 i0=floor(src)、i1=min(i0+1, in-1)，且 i0 触底时 lambda 归零。 */
static void taps(int in_size, float step, int i, int *i0, int *i1, demoir_t *lam) {
    float src = ((float)i + (demoir_t)0.5) * step - (demoir_t)0.5;
    if (src < (demoir_t)0) src = (demoir_t)0;
    int a = (int)src;
    demoir_t l = src - (demoir_t)a;
    if (a > in_size - 1) {          /* 数值极端时兜底，与 ATen 的钳位等价 */
        a = in_size - 1;
        l = (demoir_t)0;
    }
    int b = (a + 1 < in_size) ? a + 1 : in_size - 1;
    if (a >= in_size - 1) l = (demoir_t)0;
    *i0 = a;
    *i1 = b;
    *lam = l;
}

void demoir_resize_bilinear(const demoir_t *in, demoir_t *out,
                            int C, int H, int W, int H_out, int W_out, float step) {
    for (int oh = 0; oh < H_out; ++oh) {
        int h0, h1;
        demoir_t lh;
        taps(H, step, oh, &h0, &h1, &lh);
        for (int ow = 0; ow < W_out; ++ow) {
            int w0, w1;
            demoir_t lw;
            taps(W, step, ow, &w0, &w1, &lw);
            const demoir_t *p00 = in + (((h0 * W) + w0) * C);
            const demoir_t *p01 = in + (((h0 * W) + w1) * C);
            const demoir_t *p10 = in + (((h1 * W) + w0) * C);
            const demoir_t *p11 = in + (((h1 * W) + w1) * C);
            demoir_t *dst = out + (((oh * W_out) + ow) * C);
#pragma HLS PIPELINE II=1
            for (int c = 0; c < C; ++c) {
                const demoir_t top = p00[c] + (p01[c] - p00[c]) * lw;
                const demoir_t bot = p10[c] + (p11[c] - p10[c]) * lw;
                dst[c] = top + (bot - top) * lh;
            }
        }
    }
}
