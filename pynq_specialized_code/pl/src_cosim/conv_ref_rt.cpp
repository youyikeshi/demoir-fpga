// 运行期版本的参考实现：与 conv2d_accel.cpp 里的 conv_ref 同语义，
// 但所有形状参数都是运行期入参（供单引擎测试台做金标准比对）。

#include "conv2d_engine.h"

int conv_ref_rt(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
                data_t *out_fm, int H, int W, int Hout, int Wout,
                int cin, int cout, int ksize, int stride, int dil,
                int pad, int shift, int relu) {
    for (int oc = 0; oc < cout; ++oc) {
        for (int y = 0; y < Hout; ++y) {
            for (int x = 0; x < Wout; ++x) {
                int32_t acc = 0;
                for (int ic = 0; ic < cin; ++ic) {
                    for (int ky = 0; ky < ksize; ++ky) {
                        const int iy = y * stride + ky * dil - pad;
                        for (int kx = 0; kx < ksize; ++kx) {
                            const int ix = x * stride + kx * dil - pad;
                            if (iy >= 0 && iy < H && ix >= 0 && ix < W) {
                                const int32_t p = (int32_t)in_fm[(ic * H + iy) * W + ix];
                                const int32_t wv = (int32_t)wgt
                                    [((oc * cin + ic) * ksize + ky) * ksize + kx];
                                acc += p * wv;
                            }
                        }
                    }
                }
                int32_t v = acc + (int32_t)bias[oc];
                v = v >> shift;
                if (relu != 0 && v < 0) v = 0;   // v2：线性层可旁路 ReLU
                if (v > 127) v = 127;
                if (v < -128) v = -128;
                out_fm[(oc * Hout + y) * Wout + x] = (data_t)v;
            }
        }
    }
    return ERR_OK;
}
