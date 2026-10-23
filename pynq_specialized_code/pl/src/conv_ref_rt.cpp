// 运行期版本的参考实现：与 conv2d_accel.cpp 里的 conv_ref 同语义，
// 但所有形状参数都是运行期入参（供单引擎测试台做金标准比对）。

#include "conv2d_engine.h"

namespace {
// k=3 权重容器的下标（2026-10-23 第四轮）：`K3_PAD12=1` 时每个 (oc,ic) 是
// 12 字节（9 个抽头 + 3 个补零，第 q 个字的低 3 字节 = 抽头 3q..3q+2）。
// k=5 同理（2026-10-23 第五轮，`K5_PAD28=1`）：28 字节（25 个抽头 + 3 个补零，
// 第 q 个字的低 4 字节 = 抽头 4q..4q+3，最后一个字只用 1 字节）。
// **参考实现必须与引擎的装载下标逐字一致**，否则 fuzz 只是"两边一起错"；
// 更高一层的独立性由"重打包后 ref_out.bin 逐字节不变"保证。
inline int widx(int ky, int kx, int oc, int ic, int cout, int cin, int ksize) {
#if K3_PAD12
    if (ksize == 3) {
        (void)cout;
        return (oc * cin + ic) * 12 + ky * 3 + kx;
    }
#endif
#if K5_PAD28
    if (ksize == 5) {
        (void)cout;
        return (oc * cin + ic) * 28 + ky * 5 + kx;
    }
#endif
    return ((oc * cin + ic) * ksize + ky) * ksize + kx;
}
}  // namespace

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
                                    [widx(ky, kx, oc, ic, cout, cin, ksize)];
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
