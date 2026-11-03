/* =============================================================================
 * convpf_neon_shim.h —— **仅测试用**：把 convpf_c.c 里用到的那 6 个 NEON 内建
 * 在任意平台（含 x86 主机）上做逐位等价的可移植实现。
 *
 * 为什么需要：`convpf_c.c` 的 NEON tile 在 x86 上根本不会被编译
 * （`#if __ARM_NEON`），所以"索引 / 边界 tile / 行尾尾块 / 越界行取 +0.0"
 * 这些**最容易写错**的部分在主机上完全没有被验证过 —— 只能等板端首次加载时
 * 的自检去发现，那时代价是"整个内核被禁用"。用这个 shim 就能在主机上把它们
 * 全部测掉。
 *
 * 用法（只在测试构建里）：
 *     cc -O2 -std=c99 -fPIC -shared -DCPF_FAKE_NEON -o convpf_c_x86_neonsim.so convpf_c.c
 * 引擎：`ps/tools/convpf_neon_sim.py`。
 *
 * ⚠ 语义注意：本 shim 的 `vmlaq_n_f32` = **乘后舍入、再加后舍入**（即"不融合"）。
 *   这正是 ARMv7 VMLA.F32 的定义；如果真板上的 VMLA 融合，板端自检会把
 *   variant=1 判掉并自动退 variant=0（两条路都仍然逐字节正确）。
 * ⚠ 这个头**不参与**板端/主机正式构建（不加 -DCPF_FAKE_NEON 就不会被 include）。
 * ========================================================================== */
#ifndef CONVPF_NEON_SHIM_H
#define CONVPF_NEON_SHIM_H

typedef struct { float v[4]; } float32x4_t;

static inline float32x4_t vdupq_n_f32(float s)
{
    float32x4_t r = {{s, s, s, s}};
    return r;
}

static inline float32x4_t vld1q_f32(const float *p)
{
    float32x4_t r = {{p[0], p[1], p[2], p[3]}};
    return r;
}

static inline void vst1q_f32(float *p, float32x4_t a)
{
    p[0] = a.v[0]; p[1] = a.v[1]; p[2] = a.v[2]; p[3] = a.v[3];
}

static inline float32x4_t vaddq_f32(float32x4_t a, float32x4_t b)
{
    float32x4_t r;
    for (int i = 0; i < 4; i++) r.v[i] = a.v[i] + b.v[i];
    return r;
}

static inline float32x4_t vmulq_n_f32(float32x4_t a, float s)
{
    float32x4_t r;
    for (int i = 0; i < 4; i++) r.v[i] = a.v[i] * s;
    return r;
}

/* VMLA（不融合）：每条 lane 独立，先舍入乘、再舍入加 —— 与串行标量同构 */
static inline float32x4_t vmlaq_n_f32(float32x4_t a, float32x4_t b, float s)
{
    float32x4_t r;
    for (int i = 0; i < 4; i++) r.v[i] = a.v[i] + b.v[i] * s;
    return r;
}

#endif /* CONVPF_NEON_SHIM_H */
