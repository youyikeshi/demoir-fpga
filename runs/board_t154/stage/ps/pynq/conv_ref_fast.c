/* conv_ref_fast.c —— int8 卷积的 ARM 快速参考实现（2026-10-28）
 *
 * 语义与 ref_numpy.conv_ref_numpy 逐位一致：
 *   acc = bias[oc] + Σ_ic Σ_ky Σ_kx in[ic][y*s+ky*d-pad][x*s+kx*d-pad] * w[oc][ic][ky][kx]
 *   out = relu ? max(0, acc >> shift) : (acc >> shift)，再饱和到 int8 [-128,127]
 *
 * 结构取舍（**实测数据**，别凭直觉改）：
 *   ① 逐像素收集抽头 → 每 4 个 oc 并行点积，acc 在寄存器里：
 *      板上 2.44 s（1.47 GMAC ⇒ 0.60 GMAC/s）★当前采用
 *   ② 分块 + 抽头主序 + acc 放内存/L1：
 *      板上 2.44 s 的那一版同源，未再提速
 *   ③ 分块 + 抽头主序 + acc 常驻寄存器（tap 被 48 个 oc 各重读一遍）：
 *      板上 4.29 s —— **更慢**，别走这条路
 *   编译选项也很关键：-O2/-O3 是 7.5 s，加 -funroll-loops 6.4 s，
 *   再加 -mcpu=cortex-a9 -mfpu=neon -ftree-vectorize 才到 2.44 s。
 */
#include <stdint.h>
#include <string.h>

#define MAXNT 1024

static inline int8_t clamp_i8(int32_t v) {
    if (v > 127) return (int8_t)127;
    if (v < -128) return (int8_t)-128;
    return (int8_t)v;
}

int conv_chw_i8(const int8_t *inp, const int8_t *wgt, const int32_t *bias,
                int8_t *out, int cin, int cout, int H, int W,
                int k, int s, int d, int pad, int shift, int relu)
{
    if (cin <= 0 || cout <= 0 || H <= 0 || W <= 0 || k <= 0 || s <= 0 || d <= 0)
        return -1;
    const int Hout = (H - 1) / s + 1;
    const int Wout = (W - 1) / s + 1;
    const int NT   = k * k * cin;
    if (NT > MAXNT) return -1;

    int8_t tap[MAXNT];
    const long plane = (long)H * W;

    for (int y = 0; y < Hout; y++) {
        for (int x = 0; x < Wout; x++) {
            /* ① 收集本输出像素的全部输入抽头（越界补 0） */
            int t = 0;
            for (int ic = 0; ic < cin; ic++) {
                const int8_t *p = inp + (long)ic * plane;
                for (int ky = 0; ky < k; ky++) {
                    int yy = y * s + ky * d - pad;
                    if (yy < 0 || yy >= H) {
                        for (int z = 0; z < k; z++) tap[t++] = 0;  /* 必须清零：
                            直接 t += k 会留下上一个像素的残值（踩过） */
                        continue;
                    }
                    const int8_t *row = p + (long)yy * W;
                    for (int kx = 0; kx < k; kx++) {
                        int xx = x * s + kx * d - pad;
                        tap[t++] = (xx < 0 || xx >= W) ? 0 : row[xx];
                    }
                }
            }
            /* ② 每 4 个 oc 并行点积：tap 只读一遍、4 个累加器给足 ILP */
            int oc = 0;
            for (; oc + 3 < cout; oc += 4) {
                const int8_t *w0 = wgt + (long)(oc + 0) * NT;
                const int8_t *w1 = wgt + (long)(oc + 1) * NT;
                const int8_t *w2 = wgt + (long)(oc + 2) * NT;
                const int8_t *w3 = wgt + (long)(oc + 3) * NT;
                int32_t a0 = bias ? bias[oc + 0] : 0;
                int32_t a1 = bias ? bias[oc + 1] : 0;
                int32_t a2 = bias ? bias[oc + 2] : 0;
                int32_t a3 = bias ? bias[oc + 3] : 0;
                for (int j = 0; j < NT; j++) {
                    int32_t t8 = (int32_t)tap[j];
                    a0 += t8 * (int32_t)w0[j];
                    a1 += t8 * (int32_t)w1[j];
                    a2 += t8 * (int32_t)w2[j];
                    a3 += t8 * (int32_t)w3[j];
                }
                int32_t vv[4] = {a0, a1, a2, a3};
                for (int q = 0; q < 4; q++) {
                    int32_t v = vv[q] >> shift;
                    if (relu && v < 0) v = 0;
                    out[(long)(oc + q) * Hout * Wout + (long)y * Wout + x] = clamp_i8(v);
                }
            }
            for (; oc < cout; oc++) {
                const int8_t *wp = wgt + (long)oc * NT;
                int32_t acc = bias ? bias[oc] : 0;
                for (int j = 0; j < NT; j++) acc += (int32_t)tap[j] * (int32_t)wp[j];
                int32_t v = acc >> shift;
                if (relu && v < 0) v = 0;
                out[(long)oc * Hout * Wout + (long)y * Wout + x] = clamp_i8(v);
            }
        }
    }
    return 0;
}
