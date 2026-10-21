// tb_interop.cpp —— v2 层间算子加速器的主机侧功能验证（g++ 可跑，不需要 HLS）
//
// 验证方式：**独立参考实现**。参考不复制被测代码的整数舍入，而是用 double +
// nearbyint（round-half-to-even）算 acc/65536 —— acc 是 int64 且 |acc| < 2^53，
// 除法在 double 里精确，所以 nearbyint 的结果与被测的整数半偶舍入必须逐位一致。
// 如果被测代码的舍入写错了，这里立刻能暴露（而不是用同一套代码自证）。
//
// 覆盖：五个算子的随机 + 边界形状（C=1、H=1、W=1、奇宽、非 2 的幂）。
#include "../src/interop_accel.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <cstdlib>
#include <vector>
#include <algorithm>

// 用头文件里的类型，保证 -DUSE_HLS_TYPES 时（HLS/cosim）与内核签名一致
typedef pp_data_t s8;
typedef pp_coef_t s32;
typedef int64_t   s64;

static int g_fail = 0;
static int g_cases = 0;

static inline s8 sat8(int64_t v) {
    if (v > 127) return (s8)127;
    if (v < -128) return (s8)-128;
    return (s8)v;
}

// 参考：Q16 加权和 + 半偶舍入
static inline s8 ref_q16(int64_t acc) {
    return sat8((int64_t)nearbyint((double)acc / 65536.0));
}

// cosim 的 AXI 模型会按 depth= 上界访问主存，所以每个缓冲都按上界分配
static const int PP_DEPTH = 4096;
static const int PP_COEF_DEPTH = 2304;

static uint32_t g_seed = 12345;
static uint32_t rnd() { g_seed = g_seed * 1103515245u + 12345u; return g_seed >> 8; }
static int rnd_i8() { return (int)(rnd() % 256) - 128; }
static int32_t rnd_q16() { return (int32_t)((rnd() % 200001) - 100000); }  // ≈ [-1.53, 1.53)

static void check(const char *tag, int expected, int got, int idx) {
    ++g_cases;
    if (expected != got) {
        if (g_fail < 8) printf("  [FAIL] %s @%d: ref=%d got=%d\n", tag, idx, expected, got);
        ++g_fail;
    }
}

// ── RESCALE ────────────────────────────────────────────────────────────────
static void test_rescale(int C, int H, int W, int ch_off) {
    const size_t n_s = std::max((size_t)C * H * W, (size_t)PP_DEPTH);
    const size_t n_d = std::max((size_t)(ch_off + C) * H * W, (size_t)PP_DEPTH);
    std::vector<s8> src(n_s, (s8)0), dst(n_d, (s8)0xFF);
    std::vector<s32> coef(std::max((size_t)(C), (size_t)PP_COEF_DEPTH), 0);
    for (auto &v : src) v = (s8)rnd_i8();
    for (auto &v : coef) v = rnd_q16();
    int rc = interop_accel(src.data(), nullptr, nullptr, nullptr,
                           dst.data(), coef.data(), PP_OP_RESCALE,
                           C, H, W, 0, 0, ch_off, 1);
    if (rc != PP_OK) { printf("  [FAIL] rescale rc=%d\n", rc); ++g_fail; return; }
    for (int c = 0; c < C; ++c)
        for (int i = 0; i < H * W; ++i) {
            int64_t acc = (int64_t)src[c * H * W + i] * coef[c];
            check("rescale", ref_q16(acc), dst[(ch_off + c) * H * W + i], c * H * W + i);
        }
}

// ── ADD ────────────────────────────────────────────────────────────────────
static void test_add(int C, int H, int W) {
    const size_t n = std::max((size_t)C * H * W, (size_t)PP_DEPTH);
    std::vector<s8> a(n, (s8)0), b(n, (s8)0), dst(n, (s8)0);
    std::vector<s32> coef(std::max((size_t)(2 * C), (size_t)PP_COEF_DEPTH), 0);
    for (auto &v : a) v = (s8)rnd_i8();
    for (auto &v : b) v = (s8)rnd_i8();
    for (auto &v : coef) v = rnd_q16();
    int rc = interop_accel(a.data(), b.data(), nullptr, nullptr,
                           dst.data(), coef.data(), PP_OP_ADD,
                           C, H, W, 0, 0, 0, 2);
    if (rc != PP_OK) { printf("  [FAIL] add rc=%d\n", rc); ++g_fail; return; }
    for (int c = 0; c < C; ++c)
        for (int i = 0; i < H * W; ++i) {
            int64_t acc = (int64_t)a[c * H * W + i] * coef[2 * c]
                        + (int64_t)b[c * H * W + i] * coef[2 * c + 1];
            check("add", ref_q16(acc), dst[c * H * W + i], c * H * W + i);
        }
}

// ── SUM3W（1~4 源）─────────────────────────────────────────────────────────
static void test_sum3w(int C, int H, int W, int nsrc) {
    const size_t nv = std::max((size_t)C * H * W, (size_t)PP_DEPTH);
    std::vector<s8> s[4], dst(nv, (s8)0);
    for (int j = 0; j < 4; ++j) {
        s[j].assign(nv, (s8)0);
        if (j < nsrc) for (auto &v : s[j]) v = (s8)rnd_i8();
    }
    std::vector<s32> coef(std::max((size_t)(C * 4), (size_t)PP_COEF_DEPTH), 0);
    for (auto &v : coef) v = rnd_q16();
    int rc = interop_accel(s[0].data(), nsrc >= 2 ? s[1].data() : nullptr,
                           nsrc >= 3 ? s[2].data() : nullptr,
                           nsrc >= 4 ? s[3].data() : nullptr,
                           dst.data(), coef.data(), PP_OP_SUM3W,
                           C, H, W, 0, 0, 0, nsrc);
    if (rc != PP_OK) { printf("  [FAIL] sum3w rc=%d\n", rc); ++g_fail; return; }
    for (int c = 0; c < C; ++c)
        for (int i = 0; i < H * W; ++i) {
            int64_t acc = 0;
            for (int j = 0; j < nsrc; ++j)
                acc += (int64_t)s[j][c * H * W + i] * coef[c * 4 + j];
            check("sum3w", ref_q16(acc), dst[c * H * W + i], c * H * W + i);
        }
}

// ── INTERP（独立参考：中心对齐 + double + nearbyint）─────────────────────────
static void test_interp(int C, int H, int W, int num, int den) {
    const int H2 = H * num / den, W2 = W * num / den;
    if (H2 <= 0 || W2 <= 0) return;
    std::vector<s8> src(std::max((size_t)C * H * W, (size_t)PP_DEPTH), (s8)0);
    std::vector<s8> dst(std::max((size_t)C * H2 * W2, (size_t)PP_DEPTH), (s8)0x7F);
    std::vector<s32> coef((size_t)PP_COEF_DEPTH, 0);
    for (auto &v : src) v = (s8)rnd_i8();
    int rc = interop_accel(src.data(), nullptr, nullptr, nullptr, dst.data(),
                           coef.data(), PP_OP_INTERP, C, H, W, H2, W2, 0, 1);
    if (rc != PP_OK) { printf("  [FAIL] interp rc=%d (%dx%d -> %dx%d)\n", rc, H, W, H2, W2);
                       ++g_fail; return; }
    const double sy_scale = (double)H2 / H, sx_scale = (double)W2 / W;
    for (int c = 0; c < C; ++c) {
        for (int oy = 0; oy < H2; ++oy) {
            double sy = (oy + 0.5) / sy_scale - 0.5;
            if (sy < 0) sy = 0;
            if (sy > H - 1) sy = H - 1;
            const int y0 = (int)floor(sy);
            const int wy = (int)lrint((sy - y0) * 8.0);
            const int y1 = (y0 + 1 < H) ? y0 + 1 : H - 1;
            for (int ox = 0; ox < W2; ++ox) {
                double sx = (ox + 0.5) / sx_scale - 0.5;
                if (sx < 0) sx = 0;
                if (sx > W - 1) sx = W - 1;
                const int x0 = (int)floor(sx);
                const int wx = (int)lrint((sx - x0) * 8.0);
                const int x1 = (x0 + 1 < W) ? x0 + 1 : W - 1;
                const int32_t p00 = src[c * H * W + y0 * W + x0];
                const int32_t p01 = src[c * H * W + y0 * W + x1];
                const int32_t p10 = src[c * H * W + y1 * W + x0];
                const int32_t p11 = src[c * H * W + y1 * W + x1];
                const int64_t v64 = ((int64_t)p00 * (8 - wx) + p01 * wx) * (8 - wy)
                                  + ((int64_t)p10 * (8 - wx) + p11 * wx) * wy;
                const s8 ref = sat8((int64_t)nearbyint((double)v64 / 64.0));
                check("interp", ref, dst[c * H2 * W2 + oy * W2 + ox], oy * W2 + ox);
            }
        }
    }
}

// ── REDUCE_MEAN ────────────────────────────────────────────────────────────
static void test_reduce(int C, int H, int W) {
    std::vector<s8> src(std::max((size_t)(C * H * W), (size_t)PP_DEPTH), (s8)0);
    std::vector<s8> dst(std::max((size_t)C * 8, (size_t)PP_DEPTH), (s8)0);
    std::vector<s32>     coef(std::max((size_t)(1, 0), (size_t)PP_COEF_DEPTH), 0);          // 归约不用系数，占位即可
    for (auto &v : src) v = (s8)rnd_i8();
    int rc = interop_accel(src.data(), nullptr, nullptr, nullptr,
                           dst.data(), coef.data(), PP_OP_REDUCE_MEAN,
                           C, H, W, 0, 0, 0, 1);
    if (rc != PP_OK) { printf("  [FAIL] reduce rc=%d\n", rc); ++g_fail; return; }
    for (int c = 0; c < C; ++c) {
        int64_t s = 0;
        for (int i = 0; i < H * W; ++i) s += (int)src[c * H * W + i];
        int64_t got = 0;
        for (int k = 0; k < 8; ++k)
            got |= ((int64_t)(unsigned char)(int)dst[c * 8 + k]) << (8 * k);
        ++g_cases;
        if (s != got) {
            if (g_fail < 8) printf("  [FAIL] reduce c=%d: ref=%lld got=%lld\n",
                                   c, (long long)s, (long long)got);
            ++g_fail;
        }
    }
}

int main() {
    const bool quick = getenv("PP_TB_QUICK") != nullptr;
    struct { int C, H, W; } shapes[] = {
        {1, 1, 1}, {1, 1, 7}, {3, 1, 5}, {8, 4, 4}, {16, 3, 7},
        {32, 2, 9}, {64, 5, 3}, {96, 2, 2}, {192, 1, 3}, {352, 2, 2},
    };
    if (quick) {
        // cosim 档：每个算子只留少量小形状（RTL 仿真按用例数线性变慢）
        struct { int C, H, W; } q[] = {{1, 1, 1}, {3, 2, 3}, {8, 2, 4}};
        printf("==== [quick] 5 算子 x 3 形状 ====\n");
        for (auto &s : q) {
            test_rescale(s.C, s.H, s.W, 0);
            test_add(s.C, s.H, s.W);
            test_sum3w(s.C, s.H, s.W, 4);
            test_reduce(s.C, s.H, s.W);
        }
        test_rescale(2, 2, 3, 2);                       // 通道偏移
        test_interp(2, 4, 4, 2, 1);                     // 放大 2x
        test_interp(2, 8, 8, 1, 2);                     // 缩小 1/2
        test_interp(2, 4, 4, 1, 4);                     // 缩小 1/4
        printf("\n用例 %d，失败 %d\n", g_cases, g_fail);
        if (g_fail == 0) printf("==== v2 层间算子（quick）：全部 PASS ====\n");
        return g_fail == 0 ? 0 : 1;
    }
    printf("==== RESCALE ====\n");
    for (auto &s : shapes) { test_rescale(s.C, s.H, s.W, 0); }
    test_rescale(4, 2, 3, 5);          // 带通道偏移（cat 的用法）
    printf("==== ADD ====\n");
    for (auto &s : shapes) test_add(s.C, s.H, s.W);
    printf("==== SUM3W ====\n");
    for (auto &s : shapes)
        for (int n = 1; n <= 4; ++n) test_sum3w(s.C, s.H, s.W, n);
    printf("==== INTERP ====\n");
    const int ratios[4][2] = {{2, 1}, {4, 1}, {1, 2}, {1, 4}};
    struct { int C, H, W; } ishapes[] = {{1, 4, 4}, {1, 8, 8}, {3, 4, 16}, {8, 2, 2}, {2, 8, 4}};
    for (auto &s : ishapes)
        for (auto &r : ratios) test_interp(s.C, s.H, s.W, r[0], r[1]);
    printf("==== REDUCE_MEAN ====\n");
    for (auto &s : shapes) test_reduce(s.C, s.H, s.W);
    printf("\n用例 %d，失败 %d\n", g_cases, g_fail);
    if (g_fail == 0) printf("==== v2 层间算子：全部 PASS ====\n");
    return g_fail == 0 ? 0 : 1;
}
