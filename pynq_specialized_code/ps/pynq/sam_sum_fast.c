/* sam_sum_fast.c —— SAM 融合的逐位一致 C 内核（可选加速，失败自动回落 numpy）
 *
 * 语义必须与 runner 的参考实现**逐元素同序**（IEEE-754 基本运算跨平台
 * 确定，但不可重排/不可融合）：
 *
 *     yf  = w0*(y0*s0) + w1*(y2*s2) + w2*(y4*s4)      // 左结合，乘积先算
 *     xf  = x*sx
 *     out = clip(rint((xf + yf) / so), -128, 127).astype(int8)
 *
 * 编译（板端首次调用时自动完成，也可手工）：
 *     gcc -O2 -std=c99 -ffp-contract=off -fPIC -shared \
 *         -o sam_sum_fast.so sam_sum_fast.c
 *
 * 为什么不能用 -ffast-math / 默认的 FP contraction：
 *   · -ffast-math 会重排结合律，末位不同；
 *   · 若编译器把 w*(y*s) + ... 融合成 FMA，舍入位置就变了（末位不同）。
 * 整网对中间特征 1 LSB 的差异会雪崩到 ~59% 像素，所以这里一位都不能差。
 */
#include <stdint.h>
#include <math.h>

/* ARMv7 的 VFPv3 没有 round-to-integral 指令，`rint()` 会变成 libm 调用
 * （实测 0.87 s/op）。用 2^51 魔数做"四舍五入 + 半值取偶"：v 与
 * M=2^52+2^51 相加时，结果的 ulp 正好是 1，IEEE 的 round-to-nearest-even
 * 会直接把它舍到最近的整数（M 是偶数 ⇒ 半值取偶与 rint 完全一致）。
 * 只在 |v| < 2^51 成立，本算子 |v| ≤ 数千，安全；全程 2 条 double 加法。
 * 不能开 -ffast-math：那会把 (v+M)-M 优化掉。 */
static inline double pp_rint(double v)
{
    const double M = 6755399441055744.0;   /* 2^52 + 2^51 */
    return (v + M) - M;
}

void sam_sum_f64(const int8_t *restrict y0, const int8_t *restrict y2,
                 const int8_t *restrict y4, const int8_t *restrict xx,
                 const float *restrict attn, int C, int64_t HW,
                 double s0, double s2, double s4, double sx, double so,
                 int8_t *restrict out)
{
    for (int c = 0; c < C; ++c) {
        const double w0 = (double)attn[c];
        const double w1 = (double)attn[C + c];
        const double w2 = (double)attn[2 * C + c];
        const int8_t *p0 = y0 + (int64_t)c * HW;
        const int8_t *p2 = y2 + (int64_t)c * HW;
        const int8_t *p4 = y4 + (int64_t)c * HW;
        const int8_t *px = xx + (int64_t)c * HW;
        int8_t *po = out + (int64_t)c * HW;
        int64_t i;
        for (i = 0; i < HW; ++i) {
            double yf = w0 * ((double)p0[i] * s0)
                      + w1 * ((double)p2[i] * s2)
                      + w2 * ((double)p4[i] * s4);
            double xf = (double)px[i] * sx;
            double v = pp_rint((xf + yf) / so);
            if (v > 127.0) v = 127.0;
            else if (v < -128.0) v = -128.0;
            po[i] = (int8_t)v;
        }
    }
}

/* ── C8 交错布局版 ────────────────────────────────────────────────────────
 * 视图 (blocks, H, W, 8)，通道 c = b*8 + cc 的元素在
 *     base + b*HW*8 + i*8 + cc        (i = y*W + x)
 * 与 CHW 版**逐元素同序**，只是寻址不同。 */
void sam_sum_c8_f64(const int8_t *restrict y0, const int8_t *restrict y2,
                    const int8_t *restrict y4, const int8_t *restrict xx,
                    const float *restrict attn, int blocks, int64_t HW,
                    double s0, double s2, double s4, double sx, double so,
                    int8_t *restrict out)
{
    const int C = blocks * 8;
    const int64_t n = HW * 8;
    for (int b = 0; b < blocks; ++b) {
        const int c0 = b * 8;
        double W0[8], W1[8], W2[8];
        for (int cc = 0; cc < 8; ++cc) {
            W0[cc] = (double)attn[c0 + cc];
            W1[cc] = (double)attn[C + c0 + cc];
            W2[cc] = (double)attn[2 * C + c0 + cc];
        }
        const int8_t *p0 = y0 + (int64_t)b * n;
        const int8_t *p2 = y2 + (int64_t)b * n;
        const int8_t *p4 = y4 + (int64_t)b * n;
        const int8_t *px = xx + (int64_t)b * n;
        int8_t *po = out + (int64_t)b * n;
        for (int64_t i = 0; i < HW; ++i) {
            const int64_t o = i * 8;
            for (int cc = 0; cc < 8; ++cc) {
                double yf = W0[cc] * ((double)p0[o + cc] * s0)
                          + W1[cc] * ((double)p2[o + cc] * s2)
                          + W2[cc] * ((double)p4[o + cc] * s4);
                double xf = (double)px[o + cc] * sx;
                double v = pp_rint((xf + yf) / so);
                if (v > 127.0) v = 127.0;
                else if (v < -128.0) v = -128.0;
                po[o + cc] = (int8_t)v;
            }
        }
    }
}

/* ── ECA 的通道和（C8 布局）：sums[c] = Σ_{y,x} src[c,y,x] ──────────────
 * 板端 C8 张量在内存里是 (blocks, H*W, 8) 连续排列；按 8 字节一组顺序扫，
 * 8 个 int64 累加器常驻寄存器。PS 侧再做 mean=(精确和)*s/(H·W)，与 pp 的
 * REDUCE_MEAN 语义一致（attn 最后量化到 2^-12，已实测整网 0 差异）。 */
void chsum_c8(const int8_t *restrict src, int blocks, int64_t HW,
              int64_t *restrict sums)
{
    for (int b = 0; b < blocks; ++b) {
        const int8_t *p = src + (int64_t)b * HW * 8;
        int64_t s0 = 0, s1 = 0, s2 = 0, s3 = 0, s4 = 0, s5 = 0, s6 = 0, s7 = 0;
        for (int64_t i = 0; i < HW; ++i) {
            const int64_t o = i * 8;
            s0 += p[o + 0]; s1 += p[o + 1]; s2 += p[o + 2]; s3 += p[o + 3];
            s4 += p[o + 4]; s5 += p[o + 5]; s6 += p[o + 6]; s7 += p[o + 7];
        }
        sums[b * 8 + 0] = s0; sums[b * 8 + 1] = s1;
        sums[b * 8 + 2] = s2; sums[b * 8 + 3] = s3;
        sums[b * 8 + 4] = s4; sums[b * 8 + 5] = s5;
        sums[b * 8 + 6] = s6; sums[b * 8 + 7] = s7;
    }
}
