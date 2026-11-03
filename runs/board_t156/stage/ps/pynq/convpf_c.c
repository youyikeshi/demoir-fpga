/* =============================================================================
 * convpf_c.c —— T-17：im2col-free **直接卷积**（float32，NCHW 单图，stride=1, dil=1）
 *
 * 为什么存在：T-17 归因（runs/t17_convfp/REPORT_attribution.md）证明板端 convfp
 * 1157.2 ms 里 **im2col 276.8 + pad 25.7 + gemm 内搬运 147.0 ms** 是"物化"成本，
 * 只有 ~55% 是真算力。把 im2col 融进内核（寄存器复用 k×k）后，剩余流量只剩
 * "读 x 一次 + 写 y 一次"（10.47 MB）。
 *
 * 逐字节等价（判据 J1）——本文件的**唯一**累加语义
 * --------------------------------------------------------------
 * 每个输出元素 y[co][ho][wo] 的累加，严格等于下面这段"串行标量参考"：
 *     acc = +0.0f
 *     for ci, ky, kx  (K = cin*k*k，**这个字典序**，与 w.reshape(cout,-1) 的行内序一致)
 *         v   = (越界 ? +0.0f : x[ci][ho+ky-pad][wo+kx-pad])
 *         acc = round32( acc + round32( w[co][ci][ky][kx] * v ) )     ← 两步各自舍入
 *     y = round32( acc + bias[co] )
 * 要点：
 *   ① **K 方向绝不分块、绝不重排**；乘法与加法**各自舍入**（不许合成 FMA）。
 *   ② 只在 N（输出列）方向做 4 路 SIMD、在 M（cout）方向分块 —— 4 个 lane 是
 *      **4 个不同输出元素**，各自沿 K 走完一整轮，累加序一字未变 ⇒ 逐字节相同。
 *   ③ 越界项必须取 `+0.0f` 参与乘加，**不许跳过**：跳过会丢掉 `w*±0` 的符号，
 *      在"整条点积恰好为 ±0"时改变结果的符号位（-0.0 vs +0.0）。
 *   ④ `-ffp-contract=off` 是硬要求（否则 GCC 会把 mul+add 合成 VMLA/VFMA，
 *      乘加之间不舍入 ⇒ 差 1 ulp）。另外本文件**不依赖**该开关：需要精确语义
 *      的地方用 `vmulq_n_f32` + `vaddq_f32` 两条显式指令。
 *
 * 两个变体（`variant` 参数），由 Python 加载器在**首次加载时自检后自动选择**：
 *   variant = 1 ：`vmlaq_n_f32`（乘加一条指令）。**仅当**本品's VMLA 定义为
 *                 "乘后不舍入"时它才与串行参考逐字节相同 —— ARMv7 的 VMLA.F32
 *                 按 ARM ARM 是不融合的（VFMA 是 ARMv8 才有的），但本项目历史上
 *                 观测到过 1 ulp 差，所以**不假设、现场验**：自检不过就退 0。
 *   variant = 0 ：`vmulq_n_f32` + `vaddq_f32`（两步各自舍入）。与参考**同构**，
 *                 不依赖任何 VMLA 语义，任何平台都对。
 * 非 NEON 平台（x86 主机）两者都落到标量实现 `convpf_direct_s`。
 *
 * 编译：
 *   主机 x86 : cc -O3 -std=c99 -fPIC -shared -ffp-contract=off -fno-fast-math
 *   板端 armv7: cc -O3 -std=c99 -fPIC -shared -ffp-contract=off -fno-fast-math \
 *                 -mfpu=neon -mfloat-abi=hard
 *   ⇒ 同一个源文件、同一份 md5，两边各自现场编译。
 * ========================================================================== */
#include <stddef.h>
#include <stdint.h>

#if defined(CPF_FAKE_NEON)
/* ★ 仅测试用：在任意平台仿真那 6 个 NEON 内建，好让 tile 的**索引/边界逻辑**
 *   在主机上也能被逐字节验证（见 ps/tools/convpf_neon_sim.py）。
 *   正式构建不会定义这个宏 ⇒ 板端/主机正式产物完全不受影响。 */
#include "convpf_neon_shim.h"
#define CPF_NEON 1
#elif defined(__ARM_NEON) || defined(__ARM_NEON__)
#include <arm_neon.h>
#define CPF_NEON 1
#else
#define CPF_NEON 0
#endif

int convpf_version(void) { return 1; }

/* 本产物里到底有没有 NEON 路径。**板端必须为 1**：A9 上的标量版比现有 BLAS 还慢
 * （x86 实测 0.5~0.9 GMAC/s；A9 同构标量约 5.5 cycle/MAC ⇒ 全 4 层 >1.5 s），
 * 所以 Python 加载器在 ARM 上看到 0 会**直接禁用内核**而不是悄悄跑慢路径。 */
int convpf_have_neon(void)
{
#if CPF_NEON && !defined(CPF_FAKE_NEON)
    return 1;
#else
    return 0;
#endif
}

/* ---------------------------------------------------------------- 标量 tile
 * 一个 (ho, wo0..wo0+wn) 的输出行段 × **全部** cout。整个标量参考就由它循环而成，
 * 所以"参考"和"边界 tile 回退"是**同一份代码**，不会两边走偏。
 * 累加语义见文件头 ①②③④。
 */
void convpf_tile_s(const float *x, const float *w, const float *bias, float *y,
                   int cout, int cin, int k, int H, int W, int pad,
                   int Ho, int Wo, int ho, int wo0, int wn)
{
    const size_t KK = (size_t)k * k;
    for (int co = 0; co < cout; co++) {
        const float *wc = w + (size_t)co * cin * KK;
        float *out = y + ((size_t)co * Ho + ho) * Wo + wo0;
        for (int t = 0; t < wn; t++) {
            const int wo = wo0 + t;
            float acc = 0.0f;
            for (int ci = 0; ci < cin; ci++) {
                const float *wci = wc + (size_t)ci * KK;
                const float *xci = x + (size_t)ci * H * W;
                for (int ky = 0; ky < k; ky++) {
                    const int hh = ho + ky - pad;
                    const int rok = (hh >= 0 && hh < H);
                    const float *row = xci + (size_t)(rok ? hh : 0) * W;
                    for (int kx = 0; kx < k; kx++) {
                        const int ww = wo + kx - pad;
                        const float v = (rok && ww >= 0 && ww < W) ? row[ww] : 0.0f;
                        acc = acc + wci[ky * k + kx] * v;   /* 乘、加各自舍入 */
                    }
                }
            }
            out[t] = acc + bias[co];
        }
    }
}

/* ---------------------------------------------------------------- 标量全量 */
void convpf_direct_s(const float *x, const float *w, const float *bias, float *y,
                     int cout, int cin, int k, int H, int W, int pad,
                     int Ho, int Wo)
{
    for (int ho = 0; ho < Ho; ho++)
        convpf_tile_s(x, w, bias, y, cout, cin, k, H, W, pad, Ho, Wo, ho, 0, Wo);
}

/* ---------------------------------------------------------------- NEON 主循环 */
#if CPF_NEON

#define CPF_NR 8        /* tile 宽 = 8 个输出像素（2 个 Q 寄存器） */

/* 为什么 NR=8 而不是 4：权重的标量装载摊不掉。
 * 每步 (tap) 的开销 = NR/4 个向量装载（x） + MR 个标量装载（w） + MR*NR/4 个 vmla。
 *   NR=4 , MR=4 → 1 + 4 + 4  = 9  op / 16 MAC = 1.78 MAC/op
 *   NR=8 , MR=4 → 2 + 4 + 8  = 14 op / 32 MAC = 2.29 MAC/op   ← 本文件
 *   NR=12, MR=4 → 3 + 4 + 12 = 19 op / 48 MAC = 2.53 MAC/op   （寄存器 15 个，太紧，未采用）
 * A9 的 128-bit NEON 浮点吞吐 ≈ 0.5 op/cycle ⇒ 2.29 MAC/op × 0.5 = 1.15 MAC/cycle
 * = A9 单核峰值(2 MAC/cycle)的 57%。NR=4 只有 44%，2 线程下就压不到 350 ms。
 */

/* variant=1: vmla（一条指令，乘加不舍入 —— 只有自检过了才允许用）
   variant=0: vmul + vadd（两步各自舍入，与串行参考同构）              */
#define CPF_ACC(acc, vv, s, variant)                                    \
    do {                                                                \
        if (variant) {                                                  \
            acc = vmlaq_n_f32(acc, vv, (s));                            \
        } else {                                                        \
            float32x4_t p_ = vmulq_n_f32(vv, (s));                      \
            acc = vaddq_f32(acc, p_);                                   \
        }                                                               \
    } while (0)

/* 一个 tile：列方向上 CPF_NR 个输出像素。
 *
 * ★ 边界 tile 也走向量（这是性能关键）：如果让行首/行尾的 tile 回落到标量，
 *   本网 Wo=128 时边缘 tile 占 **16/128 = 12.5% 的像素**，而标量约 5 倍成本
 *   ⇒ 白白多出 ~150 ms（板端投影），直接把 350 ms 判据顶破。
 *   做法：为 (ci,ky) 这一行在**栈上**拼一段 "padded 行" sc[0..L-1]，
 *        L = NR + k - 1，其中越界位置填 +0.0f；然后所有 tap 都从 sc 里 vld。
 *   只有"行首/行尾 tile"才付这个拷贝（每个 (ci,ky) 只拷 L ≤ 15 个 float），
 *   内部 tile 仍然直接 vld 原始行，零额外开销。
 *   数值上与"从 xpad 取"完全一样（同样是 +0.0f），所以逐字节不变。
 */
static void convpf_tile_v(const float *x, const float *w, const float *bias, float *y,
                          int cout, int cin, int k, int H, int W, int pad,
                          int Ho, int Wo, int ho, int wb, int variant)
{
    const int NR = CPF_NR;
    const int L = NR + k - 1;
    const int base = wb - pad;              /* 本 tile 需要的源列起点 */
    const int inside = (base >= 0) && (base + L <= W);
    const size_t KK = (size_t)k * k;
    const size_t ST = (size_t)cin * KK;     /* 相邻 co 的权重跨度 */

    for (int co = 0; co + 4 <= cout; co += 4) {
        float32x4_t a00 = vdupq_n_f32(0.0f), a01 = vdupq_n_f32(0.0f);
        float32x4_t a10 = vdupq_n_f32(0.0f), a11 = vdupq_n_f32(0.0f);
        float32x4_t a20 = vdupq_n_f32(0.0f), a21 = vdupq_n_f32(0.0f);
        float32x4_t a30 = vdupq_n_f32(0.0f), a31 = vdupq_n_f32(0.0f);
        const float *wc = w + (size_t)co * ST;
        for (int ci = 0; ci < cin; ci++) {
            const float *wci = wc + (size_t)ci * KK;
            const float *xci = x + (size_t)ci * H * W;
            for (int ky = 0; ky < k; ky++) {
                const int hh = ho + ky - pad;
                const int rok = (hh >= 0 && hh < H);
                const float *row = xci + (size_t)(rok ? hh : 0) * W;
                float sc[CPF_NR + 16];
                const int fast = (inside && rok);
                if (!fast) {
                    /* 越界位置必须填 **+0.0f** 并照旧参与乘加（保留 w*±0 的符号） */
                    for (int t = 0; t < L; t++) {
                        const int ww = base + t;
                        sc[t] = (rok && ww >= 0 && ww < W) ? row[ww] : 0.0f;
                    }
                }
                for (int kx = 0; kx < k; kx++) {
                    const float32x4_t v0 = fast ? vld1q_f32(row + base + kx)
                                                : vld1q_f32(sc + kx);
                    const float32x4_t v1 = fast ? vld1q_f32(row + base + kx + 4)
                                                : vld1q_f32(sc + kx + 4);
                    const float *q = wci + ky * k + kx;
                    CPF_ACC(a00, v0, q[0], variant);
                    CPF_ACC(a01, v1, q[0], variant);
                    CPF_ACC(a10, v0, q[ST], variant);
                    CPF_ACC(a11, v1, q[ST], variant);
                    CPF_ACC(a20, v0, q[2 * ST], variant);
                    CPF_ACC(a21, v1, q[2 * ST], variant);
                    CPF_ACC(a30, v0, q[3 * ST], variant);
                    CPF_ACC(a31, v1, q[3 * ST], variant);
                }
            }
        }
        for (int j = 0; j < 4; j++) {
            const int c = co + j;
            float *o = y + ((size_t)c * Ho + ho) * Wo + wb;
            const float32x4_t bv = vdupq_n_f32(bias[c]);
            float32x4_t lo, hi;
            if (j == 0) { lo = a00; hi = a01; }
            else if (j == 1) { lo = a10; hi = a11; }
            else if (j == 2) { lo = a20; hi = a21; }
            else { lo = a30; hi = a31; }
            vst1q_f32(o, vaddq_f32(lo, bv));
            vst1q_f32(o + 4, vaddq_f32(hi, bv));
        }
    }
    /* cout % 4 的尾巴**不在这里处理**：入口 `convpf_direct_rows` 要求
       cout % 4 == 0，否则整层走标量（本网 cout=12/48，都满足）。 */
}

#endif /* CPF_NEON */

/* ---------------------------------------------------------------- 入口
 * `convpf_direct_rows`：只算输出行 [ho0, ho1)。**按输出行切分不改变任何
 * 输出元素的 K 序**（每个元素仍沿 K 走完一整轮）⇒ 与整幅调用逐字节相同，
 * 所以可以安全地按行分给多个线程（板端 A9 双核）。
 */
void convpf_direct_rows(const float *x, const float *w, const float *bias, float *y,
                        int cout, int cin, int k, int H, int W, int pad,
                        int Ho, int Wo, int ho0, int ho1, int variant)
{
#if CPF_NEON
    const int TB = CPF_NR;
    if ((cout & 3) != 0 || cin <= 0 || k <= 0 || H <= 0 || W <= 0) {
        convpf_direct_s(x, w, bias, y, cout, cin, k, H, W, pad, Ho, Wo);
        return;
    }
    if (ho0 < 0) ho0 = 0;
    if (ho1 > Ho) ho1 = Ho;
    for (int ho = ho0; ho < ho1; ho++) {
        int wb = 0;
        for (; wb + TB <= Wo; wb += TB)         /* 满宽 tile 一律向量（含边界） */
            convpf_tile_v(x, w, bias, y, cout, cin, k, H, W, pad,
                          Ho, Wo, ho, wb, variant);
        if (wb < Wo)                            /* 行尾不足 4 像素 */
            convpf_tile_s(x, w, bias, y, cout, cin, k, H, W, pad, Ho, Wo,
                          ho, wb, Wo - wb);
    }
#else
    (void)variant;
    {
        /* 非 NEON：标量按行段算，同样支持行切分 */
        for (int ho = ho0; ho < ho1; ho++)
            convpf_tile_s(x, w, bias, y, cout, cin, k, H, W, pad, Ho, Wo, ho, 0, Wo);
    }
#endif
}

/* 整幅（单线程）= 行段 [0,Ho) */
void convpf_direct(const float *x, const float *w, const float *bias, float *y,
                   int cout, int cin, int k, int H, int W, int pad,
                   int Ho, int Wo, int variant)
{
    convpf_direct_rows(x, w, bias, y, cout, cin, k, H, W, pad, Ho, Wo, 0, Ho, variant);
}
