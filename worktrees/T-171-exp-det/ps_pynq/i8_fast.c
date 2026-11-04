/* i8_fast.c —— int8 的**逐位精确**快速内核（cat / 标度重定标 / 拷贝用）。
 *
 * 为什么需要它（2026-10-23 第六轮，板上实测，见 ps/tools/cat_phase_diag.py）：
 *   cat 的 5.66 s 里，每段要么是"整块拷贝"、要么是"按 256 项查找表重定标"。
 *   原来的重定标走 `np.frombuffer(v.tobytes().translate(tbl))`：
 *     · tobytes()        145 MB/s（多一遍读 + 一次 4.9 MB 的临时分配）
 *     · bytes.translate   69 MB/s（CPython 的逐字节循环）
 *     · 再赋值回 arena    183 MB/s（多一遍写）
 *   合计 **42.9 MB/s** —— 与整网 cat 的实测 46 MB/s 完全对得上。
 *   而同一块内存上纯 memcpy 是 183 MB/s ⇒ 重定标本可以只比拷贝慢一点点。
 *
 * 本文件给出三个函数（全部**一次读写**，无临时数组）：
 *   i8_copy   —— 64 字节/迭代的 NEON 拷贝
 *   i8_lut    —— 异处查表（cat 的 copy 段）
 *   i8_lut_ip —— 原地查表（src == dst，cat 的 direct 段重定标）
 *
 * 为什么用查找表而不是定点乘法：标度重定标必须与 `lut_util.rescale_lut_bytes`
 * 的 float64 + rint + clip **逐位相同**；表查的语义天然逐位相同，定点乘法则在
 * .5 边界上会分叉（整网对 1 LSB 差异混沌放大到 59%，见 pp 文档）。
 *
 * 256 项表的 NEON 实现：ARMv7 的 `vtbl4_u8` 只能查 32 项（4×8 字节），
 * 所以把 256 项切成 8 组、每组 32 项，做 8 次 tbl + 掩码选择。
 * 算力有富余（约 3 op/byte），瓶颈在内存（见 cat_phase_diag 的对照数字）。
 *
 * 编译：板端首次使用时由 ops_numpy 用 cc -O3 现场编译（与 sam_sum_fast.c 同一套），
 * 编译或自检失败就静默回落到 numpy 实现 —— 结果永远一致。
 */
#include <stdint.h>
#include <string.h>

#if defined(__ARM_NEON) || defined(__ARM_NEON__)
#include <arm_neon.h>
#define HAVE_NEON 1
#else
#define HAVE_NEON 0
#endif

void i8_copy(int8_t *dst, const int8_t *src, long n) {
    long i = 0;
#if HAVE_NEON
    for (; i + 64 <= n; i += 64) {
        int8x16_t a = vld1q_s8(src + i);
        int8x16_t b = vld1q_s8(src + i + 16);
        int8x16_t c = vld1q_s8(src + i + 32);
        int8x16_t d = vld1q_s8(src + i + 48);
        vst1q_s8(dst + i, a);
        vst1q_s8(dst + i + 16, b);
        vst1q_s8(dst + i + 32, c);
        vst1q_s8(dst + i + 48, d);
    }
#endif
    if (i < n) memcpy(dst + i, src + i, (size_t)(n - i));
}

#if HAVE_NEON
/* 8 组 × 32 项：每组 = 4 个 uint8x8_t。g 组覆盖 lut[32g .. 32g+31]。 */
typedef struct { uint8x8x4_t g[8]; } lut256_t;

static inline void lut256_load(lut256_t *T, const uint8_t *lut) {
    for (int g = 0; g < 8; ++g) {
        T->g[g].val[0] = vld1_u8(lut + 32 * g + 0);
        T->g[g].val[1] = vld1_u8(lut + 32 * g + 8);
        T->g[g].val[2] = vld1_u8(lut + 32 * g + 16);
        T->g[g].val[3] = vld1_u8(lut + 32 * g + 24);
    }
}

static inline uint8x8_t lut256_apply(uint8x8_t v, const lut256_t *T) {
    const uint8x8_t idx = vand_u8(v, vdup_n_u8(31));
    const uint8x8_t grp = vshr_n_u8(v, 5);
    uint8x8_t r = vtbl4_u8(T->g[0], idx);
    for (int g = 1; g < 8; ++g) {
        uint8x8_t c = vtbl4_u8(T->g[g], idx);
        r = vbsl_u8(vceq_u8(grp, vdup_n_u8((uint8_t)g)), c, r);
    }
    return r;
}
#endif

void i8_lut(int8_t *dst, const int8_t *src, long n, const uint8_t *lut) {
    long i = 0;
#if HAVE_NEON
    lut256_t T;
    lut256_load(&T, lut);
    for (; i + 64 <= n; i += 64) {                /* 64 字节/迭代，理由同上 */
        uint8x8_t v0 = vld1_u8((const uint8_t *)(src + i));
        uint8x8_t v1 = vld1_u8((const uint8_t *)(src + i + 8));
        uint8x8_t v2 = vld1_u8((const uint8_t *)(src + i + 16));
        uint8x8_t v3 = vld1_u8((const uint8_t *)(src + i + 24));
        uint8x8_t v4 = vld1_u8((const uint8_t *)(src + i + 32));
        uint8x8_t v5 = vld1_u8((const uint8_t *)(src + i + 40));
        uint8x8_t v6 = vld1_u8((const uint8_t *)(src + i + 48));
        uint8x8_t v7 = vld1_u8((const uint8_t *)(src + i + 56));
        vst1_u8((uint8_t *)(dst + i), lut256_apply(v0, &T));
        vst1_u8((uint8_t *)(dst + i + 8), lut256_apply(v1, &T));
        vst1_u8((uint8_t *)(dst + i + 16), lut256_apply(v2, &T));
        vst1_u8((uint8_t *)(dst + i + 24), lut256_apply(v3, &T));
        vst1_u8((uint8_t *)(dst + i + 32), lut256_apply(v4, &T));
        vst1_u8((uint8_t *)(dst + i + 40), lut256_apply(v5, &T));
        vst1_u8((uint8_t *)(dst + i + 48), lut256_apply(v6, &T));
        vst1_u8((uint8_t *)(dst + i + 56), lut256_apply(v7, &T));
    }
#endif
    for (; i < n; ++i) dst[i] = (int8_t)lut[(uint8_t)src[i]];
}

void i8_lut_ip(int8_t *p, long n, const uint8_t *lut) {
    i8_lut(p, p, n, lut);
}

/* 标量版本（对照 / 兜底；不依赖 NEON） */
void i8_lut_scalar(int8_t *dst, const int8_t *src, long n, const uint8_t *lut) {
    for (long i = 0; i < n; ++i) dst[i] = (int8_t)lut[(uint8_t)src[i]];
}

/* ─────────────────────────────────────────────────────────────────────────
 * 整数仿射重定标：y = sat8((x*A + C) >> S)
 *
 * 参数由 Python 侧 lut_util.fit_affine 给出，并且**对全部 256 个 int8 逐项
 * 验证过与 256 项查找表一致** —— 所以本函数与 i8_lut 逐位等价，只是快得多：
 * 查表路线在 Cortex-A9 上要 8 次 vtbl+选择（NEON 只有 64 位），实测 62 MB/s；
 * 仿射路线只要 ~24 条整数指令处理 16 字节，瓶颈回到内存（memcpy 183 MB/s）。
 * ───────────────────────────────────────────────────────────────────────── */
#if HAVE_NEON
static inline int8x16_t scale16_s32(int8x16_t x, int32x4_t vA, int32x4_t vC,
                                    int32x4_t vSh) {
    int16x8_t xlo = vmovl_s8(vget_low_s8(x));
    int16x8_t xhi = vmovl_s8(vget_high_s8(x));
    int32x4_t a0 = vmovl_s16(vget_low_s16(xlo));
    int32x4_t a1 = vmovl_s16(vget_high_s16(xlo));
    int32x4_t a2 = vmovl_s16(vget_low_s16(xhi));
    int32x4_t a3 = vmovl_s16(vget_high_s16(xhi));
    a0 = vshlq_s32(vaddq_s32(vmulq_s32(a0, vA), vC), vSh);
    a1 = vshlq_s32(vaddq_s32(vmulq_s32(a1, vA), vC), vSh);
    a2 = vshlq_s32(vaddq_s32(vmulq_s32(a2, vA), vC), vSh);
    a3 = vshlq_s32(vaddq_s32(vmulq_s32(a3, vA), vC), vSh);
    int16x8_t n0 = vcombine_s16(vqmovn_s32(a0), vqmovn_s32(a1));
    int16x8_t n1 = vcombine_s16(vqmovn_s32(a2), vqmovn_s32(a3));
    return vcombine_s8(vqmovn_s16(n0), vqmovn_s16(n1));
}
#endif

void i8_scale(int8_t *dst, const int8_t *src, long n, int A, int S, int C) {
    long i = 0;
#if HAVE_NEON
    const int32x4_t vA = vdupq_n_s32(A);
    const int32x4_t vC = vdupq_n_s32(C);
    const int32x4_t vSh = vdupq_n_s32(-S);        /* VSHL 负数位移 = 算术右移 */
    for (; i + 64 <= n; i += 64) {               /* 64 字节/迭代，理由同上 */
        int8x16_t x0 = vld1q_s8(src + i);
        int8x16_t x1 = vld1q_s8(src + i + 16);
        int8x16_t x2 = vld1q_s8(src + i + 32);
        int8x16_t x3 = vld1q_s8(src + i + 48);
        vst1q_s8(dst + i, scale16_s32(x0, vA, vC, vSh));
        vst1q_s8(dst + i + 16, scale16_s32(x1, vA, vC, vSh));
        vst1q_s8(dst + i + 32, scale16_s32(x2, vA, vC, vSh));
        vst1q_s8(dst + i + 48, scale16_s32(x3, vA, vC, vSh));
    }
    for (; i + 16 <= n; i += 16) {
        int8x16_t x = vld1q_s8(src + i);
        vst1q_s8(dst + i, scale16_s32(x, vA, vC, vSh));
    }
#endif
    for (; i < n; ++i) {
        long long v = ((long long)src[i] * (long long)A + (long long)C) >> S;
        if (v > 127) v = 127;
        else if (v < -128) v = -128;
        dst[i] = (int8_t)v;
    }
}

void i8_scale_ip(int8_t *p, long n, int A, int S, int C) {
    i8_scale(p, p, n, A, S, C);
}

/* 编译期是否启用了 NEON —— 板端第一次加载时打印出来，避免"以为在跑 NEON
   其实在跑标量"（标量逐字节访问 CMA 非缓存内存只有 6 MB/s，比改造前还慢）。 */
int i8_have_neon(void) { return HAVE_NEON; }

/* ─────────────────────────────────────────────────────────────────────────
 * Q15 版重定标：y = sat8( VQRDMULH(x << m, A) )，即 round(x·2^m·A / 2^15)
 *
 * 为什么再要一个版本：int32 仿射（i8_scale）每 16 字节要 ~24 条 NEON 指令，
 * 在 Cortex-A9（64 位 NEON，128 位指令拆成 2 拍）上实测只有 60 MB/s —— 又是
 * 算力受限。改成 16 位通道后只要 ~11 条指令（0.7 op/byte）：
 *     vld1q → vmovl.s8 ×2 → vshl.s16 ×2 → vqrdmulh.s16 ×2 → vqmovn.s16 ×2 → vst1q
 * 饱和两次（int16 → int8）等价于 clip 到 [-128,127]（单调，逐级饱和不改变方向）。
 *
 * 参数 (m, A) 同样由 Python 侧**逐项验证**给出：只有当它对全部 256 个 int8
 * 都与 256 项查找表完全一致时才会被采用（整网 363 对标度里 354 对被 Q15 命中，
 * 其余 9 对退回 int32 仿射或查表）。
 * ───────────────────────────────────────────────────────────────────────── */
#if HAVE_NEON
static inline int8x16_t q15_16(int8x16_t x, int16x8_t vA, int16x8_t vM) {
    int16x8_t lo = vshlq_s16(vmovl_s8(vget_low_s8(x)), vM);
    int16x8_t hi = vshlq_s16(vmovl_s8(vget_high_s8(x)), vM);
    return vcombine_s8(vqmovn_s16(vqrdmulhq_s16(lo, vA)),
                       vqmovn_s16(vqrdmulhq_s16(hi, vA)));
}
#endif

void i8_scale_q15(int8_t *dst, const int8_t *src, long n, int m, int A) {
    long i = 0;
#if HAVE_NEON
    const int16x8_t vA = vdupq_n_s16((int16_t)A);
    const int16x8_t vM = vdupq_n_s16((int16_t)m);
    /* 一拍处理 64 字节（= 一条 cache line）：非缓存内存上一行要等一次完整
       突发，16 字节/迭代时"读→算→写"串行、每条线只摊 16 字节，实测只有
       71 MB/s；64 字节/迭代能到接近 memcpy 的 ~170 MB/s。 */
    for (; i + 64 <= n; i += 64) {
        int8x16_t x0 = vld1q_s8(src + i);
        int8x16_t x1 = vld1q_s8(src + i + 16);
        int8x16_t x2 = vld1q_s8(src + i + 32);
        int8x16_t x3 = vld1q_s8(src + i + 48);
        vst1q_s8(dst + i, q15_16(x0, vA, vM));
        vst1q_s8(dst + i + 16, q15_16(x1, vA, vM));
        vst1q_s8(dst + i + 32, q15_16(x2, vA, vM));
        vst1q_s8(dst + i + 48, q15_16(x3, vA, vM));
    }
    for (; i + 16 <= n; i += 16) {
        vst1q_s8(dst + i, q15_16(vld1q_s8(src + i), vA, vM));
    }
#endif
    for (; i < n; ++i) {
        long long v = (((long long)src[i] << m) * (long long)A * 2 + (1 << 15)) >> 16;
        if (v > 127) v = 127;
        else if (v < -128) v = -128;
        dst[i] = (int8_t)v;
    }
}

void i8_scale_q15_ip(int8_t *p, long n, int m, int A) {
    i8_scale_q15(p, p, n, m, A);
}

/* ─────────────────────────────────────────────────────────────────────────
 * 逐元素饱和相加：dst = sat8(a + b)
 *
 * 与 ops_numpy.add 逐位相同（那儿是 int16 相加再 clip，等价于饱和加法）。
 * 原来走 numpy：`a.astype(int16)` + `+=` + `np.clip` + `.astype(int8)`
 * 一共 5 遍内存、两份 int16 临时数组（板端 6 个 add 共 1.15 s）；
 * NEON 的 VQADD.S8 一条指令就同时完成"加 + 饱和"，1 读 2 + 1 写。
 * ───────────────────────────────────────────────────────────────────────── */
void i8_add(int8_t *dst, const int8_t *a, const int8_t *b, long n) {
    long i = 0;
#if HAVE_NEON
    for (; i + 64 <= n; i += 64) {
        int8x16_t a0 = vld1q_s8(a + i), b0 = vld1q_s8(b + i);
        int8x16_t a1 = vld1q_s8(a + i + 16), b1 = vld1q_s8(b + i + 16);
        int8x16_t a2 = vld1q_s8(a + i + 32), b2 = vld1q_s8(b + i + 32);
        int8x16_t a3 = vld1q_s8(a + i + 48), b3 = vld1q_s8(b + i + 48);
        vst1q_s8(dst + i, vqaddq_s8(a0, b0));
        vst1q_s8(dst + i + 16, vqaddq_s8(a1, b1));
        vst1q_s8(dst + i + 32, vqaddq_s8(a2, b2));
        vst1q_s8(dst + i + 48, vqaddq_s8(a3, b3));
    }
#endif
    for (; i < n; ++i) {
        int v = (int)a[i] + (int)b[i];
        if (v > 127) v = 127;
        else if (v < -128) v = -128;
        dst[i] = (int8_t)v;
    }
}

/* ─────────────────────────────────────────────────────────────────────────
 * 双线性插值（8 通道一组，int16 车道）—— 与 interp_c8 逐位相同，但快得多
 *
 * 原实现（sam_sum_fast.c 的 interp_c8）是纯标量：每个输出像素要过 8 个通道，
 * 每个通道 4 次乘加 + 一段"四舍五入到偶数"的分支。板端实测 ≈1200 拍/像素
 * （A9 333 MHz），整网 26 次 interp 共 3.3 s，是层间算子里的第二大项。
 *
 * 这里把 8 个通道一次装进 int16x8（通道在 C8 视图里就是连续的 8 字节），
 * 于是每个输出像素只要 ~14 条 NEON 指令，且**没有分支**：
 *
 *   top = p00*(8-wx) + p01*wx        ∈ [-1024, 1016]   （int16 足够）
 *   bot = p10*(8-wx) + p11*wx
 *   v   = top*(8-wy) + bot*wy        ∈ [-8192, 8128]
 *   q   = v >> 6                     （算术右移 = floor，与 C 的 int32 >> 一致）
 *   r   = v & 63
 *   q  += (r > 32) | (r == 32 && (q & 1))      ← 半值取偶
 *   out = sat8(q)
 *
 * 全部是精确整数运算（int16 不溢出），所以与标量版逐位相同；
 * `i8_interp_selftest()` 在加载时用标量版做全量对拍（随机 + 边界值 + 权重全组合）。
 * ───────────────────────────────────────────────────────────────────────── */
#if HAVE_NEON
static inline int8x8_t interp8_neon(const int8_t *p00, const int8_t *p01,
                                    const int8_t *p10, const int8_t *p11,
                                    int wx, int wy) {
    const int16x8_t a = vmovl_s8(vld1_s8(p00));
    const int16x8_t b = vmovl_s8(vld1_s8(p01));
    const int16x8_t c = vmovl_s8(vld1_s8(p10));
    const int16x8_t d = vmovl_s8(vld1_s8(p11));
    const int16x8_t top = vaddq_s16(vmulq_n_s16(a, (int16_t)(8 - wx)),
                                    vmulq_n_s16(b, (int16_t)wx));
    const int16x8_t bot = vaddq_s16(vmulq_n_s16(c, (int16_t)(8 - wx)),
                                    vmulq_n_s16(d, (int16_t)wx));
    const int16x8_t v = vaddq_s16(vmulq_n_s16(top, (int16_t)(8 - wy)),
                                  vmulq_n_s16(bot, (int16_t)wy));
    int16x8_t q = vshrq_n_s16(v, 6);
    const int16x8_t r = vandq_s16(v, vdupq_n_s16(63));
    /* 比较类内建函数返回**无符号**掩码（uint16x8_t），要显式转成 int16x8_t
       （0xFFFF 位模式不变，仍是 -1）——ARM 的 gcc 不接受隐式转换。 */
    int16x8_t m = vreinterpretq_s16_u16(vcgtq_s16(r, vdupq_n_s16(32)));
    /* "r == 32 且 q 为奇数"：两个掩码都是 0/-1，AND 之后仍是 -1。
       （踩过：直接把 (q & 1) 当实数 1 去 AND，得到的掩码是 1 而不是 -1，
         后面 q - 1 就变成减一了，整片输出差 1。） */
    const int16x8_t odd = vreinterpretq_s16_u16(
        vcgtq_s16(vandq_s16(q, vdupq_n_s16(1)), vdupq_n_s16(0)));
    const int16x8_t m2 = vreinterpretq_s16_u16(
        vandq_u16(vceqq_s16(r, vdupq_n_s16(32)),
                  vreinterpretq_u16_s16(odd)));
    m = vorrq_s16(m, m2);
    q = vsubq_s16(q, m);                     /* m 为 -1 ⇒ q+1 */
    return vqmovn_s16(q);
}
#endif

/* 标量参考（与 sam_sum_fast.c 的 interp_c8 内层逐字相同，供自检用） */
static inline int interp8_scalar_one(int p00, int p01, int p10, int p11,
                                     int wx, int wy) {
    int top = p00 * (8 - wx) + p01 * wx;
    int bot = p10 * (8 - wx) + p11 * wx;
    int v = top * (8 - wy) + bot * wy;
    int q = v >> 6;
    int r = v & 63;
    if (r > 32) ++q;
    else if (r == 32 && (q & 1)) ++q;
    if (q > 127) q = 127;
    else if (q < -128) q = -128;
    return q;
}

void i8_interp(const int8_t *restrict src, int8_t *restrict dst,
               int blocks, int H, int W, int oH, int oW,
               const int32_t *restrict i0h, const int32_t *restrict i1h,
               const int32_t *restrict wh,
               const int32_t *restrict i0w, const int32_t *restrict i1w,
               const int32_t *restrict ww) {
    const long HW = (long)H * W;
    for (int b = 0; b < blocks; ++b) {
        const int8_t *ps = src + (long)b * HW * 8;
        int8_t *pd = dst + (long)b * oH * oW * 8;
        for (int oy = 0; oy < oH; ++oy) {
            const int y0 = i0h[oy], y1 = i1h[oy], wy = wh[oy];
            for (int ox = 0; ox < oW; ++ox) {
                const int x0 = i0w[ox], x1 = i1w[ox], wx = ww[ox];
                const int8_t *p00 = ps + ((long)y0 * W + x0) * 8;
                const int8_t *p01 = ps + ((long)y0 * W + x1) * 8;
                const int8_t *p10 = ps + ((long)y1 * W + x0) * 8;
                const int8_t *p11 = ps + ((long)y1 * W + x1) * 8;
                int8_t *po = pd + ((long)oy * oW + ox) * 8;
#if HAVE_NEON
                vst1_s8(po, interp8_neon(p00, p01, p10, p11, wx, wy));
#else
                for (int cc = 0; cc < 8; ++cc) {
                    po[cc] = (int8_t)interp8_scalar_one(p00[cc], p01[cc],
                                                        p10[cc], p11[cc], wx, wy);
                }
#endif
            }
        }
    }
}

/* 自检：NEON 版 vs 标量版。覆盖
     · 全部 81 种 (wx,wy) 组合 × 随机像素
     · 边界像素值组合（-128/-127/-1/0/1/63/64/127 的 4 元组）
   返回不一致个数（0 = 通过）。Python 侧在加载时调用一次。 */
long i8_interp_selftest(void) {
    long bad = 0;
    int8_t buf[4][8];
    int8_t got[8];                  /* 注意：必须 int8_t —— vst1_s8 写 8 个字节 */
    /* 1) 全权重组合 × 4096 个随机/边界像素 */
    static const int edge[8] = {-128, -127, -64, -1, 0, 1, 63, 127};
    for (int wx = 0; wx <= 8; ++wx) {
        for (int wy = 0; wy <= 8; ++wy) {
            for (int t = 0; t < 4096; ++t) {
                for (int j = 0; j < 4; ++j) {
                    for (int cc = 0; cc < 8; ++cc) {
                        unsigned h = (unsigned)(t * 2654435761u + j * 40503u
                                                + cc * 2246822519u);
                        h ^= h >> 13;
                        buf[j][cc] = (int8_t)((h & 0x10000u) ? edge[h & 7] : (int)(h & 0xFF));
                    }
                }
#if HAVE_NEON
                int8x8_t o = interp8_neon(buf[0], buf[1], buf[2], buf[3], wx, wy);
                vst1_s8(got, o);
#else
                for (int cc = 0; cc < 8; ++cc)
                    got[cc] = (int8_t)interp8_scalar_one(buf[0][cc], buf[1][cc],
                                                         buf[2][cc], buf[3][cc],
                                                         wx, wy);
#endif
                for (int cc = 0; cc < 8; ++cc) {
                    int want = interp8_scalar_one(buf[0][cc], buf[1][cc],
                                                  buf[2][cc], buf[3][cc], wx, wy);
                    if ((int)got[cc] != want) ++bad;
                }
            }
        }
    }
    return bad;
}
