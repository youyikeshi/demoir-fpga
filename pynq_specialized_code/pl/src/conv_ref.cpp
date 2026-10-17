// 参考实现：三层循环直接算，不做任何优化。
// 它的作用是当金标准，慢但一定对。内核的输出要和它逐元素一致。
//
// 后处理顺序：累加 -> 加 bias -> 算术右移 -> ReLU -> 饱和到 int8

#include "conv2d_accel.h"

namespace {

inline data_t saturate_int8(int32_t v) {
    if (v > 127) return (data_t)127;
    if (v < -128) return (data_t)(-128);
    return (data_t)v;
}

}  // namespace

int conv_ref(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
             data_t *out_fm, int H, int W, int shift) {
    if (in_fm == 0 || wgt == 0 || bias == 0 || out_fm == 0) return ERR_NULL;
    if (H <= 0 || W <= 0 || W > MAX_W) return ERR_SIZE;
    if (shift < 1 || shift > 31) return ERR_SHIFT;

    const int H_out = CONV_OUT(H);
    const int W_out = CONV_OUT(W);

    for (int oc = 0; oc < COUT; ++oc) {
        for (int y = 0; y < H_out; ++y) {
            for (int x = 0; x < W_out; ++x) {
                int32_t acc = 0;

                for (int ic = 0; ic < CIN; ++ic) {
                    for (int ky = 0; ky < KSIZE; ++ky) {
                        // 膨胀：第 ky 个抽头对应输入行 y - PAD + ky*d
                        const int iy = y * STRIDE + ky * DILATION - PAD;
                        if (iy < 0 || iy >= H) continue;

                        for (int kx = 0; kx < KSIZE; ++kx) {
                            const int ix = x * STRIDE + kx * DILATION - PAD;
                            if (ix < 0 || ix >= W) continue;

                            const int32_t p = (int32_t)in_fm[(ic * H + iy) * W + ix];
                            const int32_t k =
                                (int32_t)wgt[((oc * CIN + ic) * KSIZE + ky) * KSIZE + kx];
                            acc += p * k;
                        }
                    }
                }

                int32_t v = acc + (int32_t)bias[oc];
                v = v >> shift;
                if (v < 0) v = 0;

                out_fm[(oc * H_out + y) * W_out + x] = saturate_int8(v);
            }
        }
    }

    return ERR_OK;
}
