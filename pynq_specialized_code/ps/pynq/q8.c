/* q8.c —— 激活量化（float32 → int8）的 C 内核，**逐字节等价**于
 * `e1e2_sim.quantize_int8`（= 旧 `E1E2Engine.pack_float` 用的那份 numpy 实现）。
 *
 * 为什么要有它：板端实测 `quantize_int8`（isfinite + 布尔花式索引 + float64 升格）
 * 在 544×24² 上要 **140 ms**；C 内核 ~26 ms（7×）。C8 直读路径里 f32 段
 * 有 36.6 M 元素（256²），再用 numpy 做一次 C8 转置还要 1 s ⇒ 这里再给一个
 * **边量化边写 C8 布局**的入口（q8_c8），一趟完成。
 *
 * 语义（与 numpy 参考逐位）：
 *     v = x * 2^fs            （float32 乘 2 的幂 = 精确，与参考的 float64 乘法同值）
 *     q = rint(v)             （round-half-to-even，与 np.rint 相同）
 *     q = clamp(q, -128, 127) （±inf 因此落到 127 / -128）
 *     x 是 NaN  ⇒ q = -128    （参考实现显式置 -128）
 *
 * NEON 路径的 round-half-even 用"加 1.5·2^23 再减回去"的经典技巧（VFP 默认
 * round-to-nearest-even）——ARMv7 NEON 没有 VRINT，这是唯一可向量的写法。
 * 自检（q8.py）会逐字节对照 numpy 参考；任何一位不同就整个弃用。
 *
 * 编译（板端首次使用时由 q8.py 现场编译）：
 *   cc -O3 -std=c99 -fPIC -shared -mfpu=neon -mfloat-abi=hard -o ... q8.c
 * 本文件只做 CPU 侧计算，不碰任何寄存器/PL 交互。
 */
#include <stdint.h>
#include <string.h>
#include <math.h>

#ifdef __ARM_NEON
#include <arm_neon.h>
#define Q8_NEON 1
#endif

static inline void q8_span(const float *x, int8_t *q, int n, float scale)
{
    int i = 0;
#ifdef Q8_NEON
    const float32x4_t vs = vdupq_n_f32(scale);
    const float32x4_t magic = vdupq_n_f32(12582912.0f);   /* 1.5 * 2^23 */
    const float32x4_t lo = vdupq_n_f32(-128.0f);
    const float32x4_t hi = vdupq_n_f32(127.0f);
    const int32x4_t m128 = vdupq_n_s32(-128);
    for (; i + 8 <= n; i += 8) {
        float32x4_t a = vmulq_f32(vld1q_f32(x + i), vs);
        float32x4_t b = vmulq_f32(vld1q_f32(x + i + 4), vs);
        /* round-half-to-even：|v| < 2^22 时精确；更大的值本来就要饱和 */
        a = vsubq_f32(vaddq_f32(a, magic), magic);
        b = vsubq_f32(vaddq_f32(b, magic), magic);
        a = vminq_f32(vmaxq_f32(a, lo), hi);   /* NaN 会传播成 NaN */
        b = vminq_f32(vmaxq_f32(b, lo), hi);
        int32x4_t ia = vcvtq_s32_f32(a);       /* NaN → 0；±inf 已被夹住 */
        int32x4_t ib = vcvtq_s32_f32(b);
        uint32x4_t na = vmvnq_u32(vceqq_f32(a, a));   /* NaN 掩码 */
        uint32x4_t nb = vmvnq_u32(vceqq_f32(b, b));
        ia = vbslq_s32(na, m128, ia);
        ib = vbslq_s32(nb, m128, ib);
        int16x8_t s16 = vcombine_s16(vqmovn_s32(ia), vqmovn_s32(ib));
        vst1_s8(q + i, vqmovn_s16(s16));
    }
#endif
    for (; i < n; ++i) {
        float v = x[i] * scale;
        float r;
        if (v != v) {                 /* NaN：参考实现显式置 -128 */
            q[i] = (int8_t)-128;
            continue;
        }
        r = rintf(v);                 /* nearest-even，与 np.rint 一致 */
        if (r > 127.0f)
            r = 127.0f;               /* +inf / 溢出 → 127 */
        else if (r < -128.0f)
            r = -128.0f;              /* -inf / 溢出 → -128 */
        q[i] = (int8_t)r;
    }
}

/* 行主序入口：(N,) → (N,) int8 */
void q8_quant(const float *x, int8_t *q, long n, float scale)
{
    long i = 0;
    while (i < n) {
        long k = n - i;
        if (k > 1 << 20)
            k = 1 << 20;
        q8_span(x + i, q + i, (int)k, scale);
        i += k;
    }
}

/* C8 布局入口：x 是 (C,H,W) 行主序，dst 是 (C/8,H,W,8) 的 C8 交错字节。
 * 按 8 通道一组、像素块 256 展开（读连续、写连续），一趟完成量化 + 转置。 */
#define Q8_TILE 256
void q8_c8(const float *x, int8_t *dst, int C, int H, int W, float scale)
{
    int hw = H * W;
    int cg = C >> 3;
    int8_t tmp[8][Q8_TILE];
    int g, j, p0, p;
    for (g = 0; g < cg; ++g) {
        const float *xg = x + (size_t)g * 8 * (size_t)hw;
        int8_t *dg = dst + (size_t)g * (size_t)hw * 8;
        for (p0 = 0; p0 < hw; p0 += Q8_TILE) {
            int n = hw - p0;
            if (n > Q8_TILE)
                n = Q8_TILE;
            for (j = 0; j < 8; ++j)
                q8_span(xg + (size_t)j * (size_t)hw + p0, tmp[j], n, scale);
            for (p = 0; p < n; ++p) {
                uint8_t b[8];
                for (j = 0; j < 8; ++j)
                    b[j] = (uint8_t)tmp[j][p];
                memcpy(dg + (size_t)(p0 + p) * 8, b, 8);
            }
        }
    }
}

int q8_version(void)
{
    return 2;
}
