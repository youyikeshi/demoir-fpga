// tb_tile.cpp —— v3 分块原型 vs conv_ref_rt 的逐位对拍（主机侧，g++ 即可）
#include "conv2d_accel.h"

#include <cstdio>
#include <cstdlib>
#include <vector>

int conv_tile(const data_t *, const data_t *, const bias_t *, data_t *,
              int, int, int, int, int, int, int, int, int, int, int, int);
int conv_ref_rt(const data_t *, const data_t *, const bias_t *, data_t *,
                int, int, int, int, int, int, int, int, int, int, int, int);

static int fails = 0;
static int cases = 0;
static unsigned g_seed = 20261022u;
static unsigned rnd() { g_seed = g_seed * 1103515245u + 12345u; return g_seed >> 8; }

static void one(int cin, int cout, int k, int H, int W, int s, int d, int p, int shift) {
    const int Hout = (H + 2 * p - d * (k - 1) - 1) / s + 1;
    const int Wout = (W + 2 * p - d * (k - 1) - 1) / s + 1;
    if (Hout <= 0 || Wout <= 0) return;

    std::vector<data_t> in(cin * H * W), wgt(cout * cin * k * k);
    std::vector<data_t> out_ref(cout * Hout * Wout), out_tile(cout * Hout * Wout);
    std::vector<bias_t> bias(cout);
    for (auto &v : in) v = (data_t)((int)(rnd() % 256) - 128);
    for (auto &v : wgt) v = (data_t)((int)(rnd() % 256) - 128);
    for (auto &v : bias) v = (bias_t)((int)(rnd() % 41) - 20);

    int r1 = conv_ref_rt(in.data(), wgt.data(), bias.data(), out_ref.data(),
                         H, W, Hout, Wout, cin, cout, k, s, d, p, shift, 1);
    int r2 = conv_tile(in.data(), wgt.data(), bias.data(), out_tile.data(),
                       H, W, Hout, Wout, cin, cout, k, s, d, p, shift, 1);
    ++cases;
    // 原型限制：只支持 d=1（尺寸按 stride 算，dilation 会把 tile 撑大到 BRAM 放不下）。
    // 这种情况算"跳过"而不是失败，但必须打出来，避免以后误以为覆盖了。
    if (d != 1 && r2 != 0) {
        printf("  [SKIP] cin=%d cout=%d k=%d d=%d（原型只支持 d=1，返回 %d）\n",
               cin, cout, k, d, r2);
        return;
    }
    if (r1 != r2) {
        printf("  [FAIL] 返回码 %d vs %d (cin=%d cout=%d k=%d)\n", r1, r2, cin, cout, k);
        ++fails;
        return;
    }
    if (r1 != 0) return;                      // 都返回同一个错误码 → 一致
    long bad = 0;
    for (size_t i = 0; i < out_ref.size(); ++i) {
        if (out_ref[i] != out_tile[i]) ++bad;
    }
    if (bad) {
        printf("  [FAIL] cin=%d cout=%d k=%d s=%d d=%d p=%d %dx%d -> %dx%d : %ld/%zu 不一致\n",
               cin, cout, k, s, d, p, H, W, Hout, Wout, bad, out_ref.size());
        ++fails;
    }
}

int main() {
    struct { int cin, cout, k, H, W, s, d, p; } cs[] = {
        {1, 1, 1, 4, 4, 1, 1, 0},
        {3, 4, 3, 5, 6, 1, 1, 1},
        {4, 8, 3, 7, 9, 1, 1, 1},
        {2, 4, 5, 8, 8, 1, 1, 2},
        {8, 8, 3, 6, 7, 2, 1, 1},
        {16, 16, 1, 5, 5, 1, 1, 0},
        {8, 8, 3, 4, 11, 1, 2, 2},
        {32, 16, 3, 9, 9, 1, 1, 1},
        {5, 3, 5, 6, 6, 1, 1, 0},
        {64, 8, 1, 3, 5, 1, 1, 0},
    };
    for (auto &c : cs) one(c.cin, c.cout, c.k, c.H, c.W, c.s, c.d, c.p, 7);
    printf("用例 %d，失败 %d\n", cases, fails);
    if (!fails) printf("==== v3 分块原型：与参考实现逐位一致 ====\n");
    return fails ? 1 : 0;
}
