// 单引擎模糊测试：随机形状 + 随机数据 + 边界宽度，逐元素比对 conv_ref_rt。
// 每个用例以 relu=1（ReLU）与 relu=0（线性，v2）两种模式各跑一遍。
//
// 覆盖点：
//   - 分块边界：W 取 1/2/3/7/8/15/16/31/32/33/63/64/65/66/127/128/129/130/255/256/257
//   - 输出通道不整除 OC_BLOCK（cout = 1/15/16/17/31/32/96 …）
//   - 输入通道取 1..17 和小上界
//   - 全部合法 (k,s,d,pad) 组合；H=1/2/3/5 覆盖上下补零行
//   - 非法参数必须返回错误码
//   - 300 个随机用例（固定种子，可复现）

#include "conv2d_engine.h"

#include <cstdio>
#include <cstdlib>
#include <random>
#include <vector>

namespace {

int g_cases = 0;

int run_case(int H, int W, int cin, int cout, int k, int s, int d, int p,
             int shift, int relu, std::mt19937 &rng, bool verbose_fail = true) {
    const int Hout = (H + 2 * p - ((k - 1) * d + 1)) / s + 1;
    const int Wout = (W + 2 * p - ((k - 1) * d + 1)) / s + 1;
    if (Hout <= 0 || Wout <= 0) return 0;

    const size_t n_in = (size_t)cin * H * W;
    const size_t n_w = (size_t)cout * cin * k * k;
    const size_t n_b = (size_t)cout;
    const size_t n_out = (size_t)cout * Hout * Wout;

    std::vector<data_t> in(n_in), w(n_w);
    std::vector<bias_t> b(n_b);
    std::uniform_int_distribution<int> di(-128, 127), bi(-100000, 100000);
    for (auto &x : in) x = (data_t)di(rng);
    for (auto &x : w) x = (data_t)di(rng);
    for (auto &x : b) x = (bias_t)bi(rng);

    std::vector<data_t> outE(n_out), outR(n_out);
    ++g_cases;
    const int rc = conv2d_engine(in.data(), w.data(), b.data(), outE.data(),
                                 H, W, Hout, Wout, cin, cout, k, s, d, p, shift, relu);
    if (rc != ERR_OK) {
        printf("FAIL(rc=%d): H=%d W=%d cin=%d cout=%d k=%d s=%d d=%d p=%d shift=%d relu=%d\n",
               rc, H, W, cin, cout, k, s, d, p, shift, relu);
        return 1;
    }
    conv_ref_rt(in.data(), w.data(), b.data(), outR.data(),
                H, W, Hout, Wout, cin, cout, k, s, d, p, shift, relu);

    for (size_t i = 0; i < n_out; ++i) {
        if (outE[i] != outR[i]) {
            if (verbose_fail) {
                printf("FAIL: case#%d H=%d W=%d cin=%d cout=%d k=%d s=%d d=%d p=%d shift=%d relu=%d"
                       " @%lu ref=%d got=%d\n",
                       g_cases, H, W, cin, cout, k, s, d, p, shift, relu,
                       (unsigned long)i, (int)outR[i], (int)outE[i]);
            }
            return 1;
        }
    }
    return 0;
}

int run_both(int H, int W, int cin, int cout, int k, int s, int d, int p,
             int shift, std::mt19937 &rng, bool verbose_fail = true) {
    int f = run_case(H, W, cin, cout, k, s, d, p, shift, 1, rng, verbose_fail);
    f += run_case(H, W, cin, cout, k, s, d, p, shift, 0, rng, verbose_fail);
    return f;
}

struct Combo { int k, s, d, p; };

}  // namespace

int main(int argc, char **argv) {
    const unsigned seed = (argc > 1) ? (unsigned)atoi(argv[1]) : 20261012u;
    printf("==== FUZZ seed = %u ====\n", seed);
    std::mt19937 rng(seed);

    const Combo combos[] = {
        {1, 1, 1, 0}, {3, 1, 1, 1}, {3, 1, 2, 2}, {3, 1, 3, 3},
        {5, 1, 1, 2}, {3, 2, 1, 1}, {3, 2, 2, 2}, {5, 2, 1, 2}, {1, 2, 1, 0},
    };
    const int Wlist[] = {1, 2, 3, 7, 8, 15, 16, 31, 32, 33, 63, 64, 65, 66,
                         127, 128, 129, 130, 255, 256, 257};
    const int Hlist[] = {1, 2, 3, 5};

    int fails = 0;

    // 1) 结构化边界网格：小通道、全部 9 种组合 × 全部宽度 × 全部高度
    for (int k = 0; k < 9 && fails == 0; ++k) {
        for (int hi = 0; hi < 4; ++hi) {
            for (int wi = 0; wi < (int)(sizeof(Wlist) / sizeof(int)); ++wi) {
                const Combo &c = combos[k];
                fails += run_both(Hlist[hi], Wlist[wi], 3, 5,
                                  c.k, c.s, c.d, c.p, 7, rng, fails == 0);
            }
        }
    }
    printf("网格用例完成: %d 个, fails=%d\n", g_cases, fails);

    // 2) 通道余数用例：cout 不整除 OC_BLOCK、cin 边界
    const int CINs[] = {1, 2, 7, 8, 9, 16, 17, 112};
    const int COUTs[] = {1, 5, 15, 16, 17, 31, 32, 33, 96};
    for (int ci = 0; ci < 8 && fails == 0; ++ci) {
        for (int co = 0; co < 9 && fails == 0; ++co) {
            const Combo &c = combos[(ci + co) % 9];
            fails += run_both(3, 70, CINs[ci], COUTs[co],
                              c.k, c.s, c.d, c.p, 5, rng, fails == 0);
        }
    }
    printf("通道余数用例完成: %d 个, fails=%d\n", g_cases, fails);

    // 3) 随机用例：300 个随机形状
    std::uniform_int_distribution<int> dH(1, 25), dW(1, 260), dcin(1, 17),
        dcout(1, 17), dshift(1, 12), dcombo(0, 8), dExtraW(1, 4);
    for (int t = 0; t < 300 && fails == 0; ++t) {
        const Combo &c = combos[dcombo(rng)];
        int H = dH(rng), W = dW(rng);
        if (dExtraW(rng) == 1) {
            std::uniform_int_distribution<int> pick(
                0, (int)(sizeof(Wlist) / sizeof(int)) - 1);
            W = Wlist[pick(rng)];
        }
        fails += run_both(H, W, dcin(rng), dcout(rng),
                          c.k, c.s, c.d, c.p, dshift(rng), rng, fails == 0);
    }
    printf("随机用例完成: %d 个, fails=%d\n", g_cases, fails);

    // 4) 非法参数必须被拒绝（返回非 0）
    {
        std::vector<data_t> in(1 * 8 * 8, 1), w(8 * 1 * 9, 1);
        std::vector<bias_t> b(8, 0);
        std::vector<data_t> out(8 * 8 * 8, 0);
        struct Bad { const char *name; int k, s, d, p, ci, co, sh; };
        const Bad bads[] = {
            {"k=2",            2, 1, 1, 1, 1, 8, 7},
            {"k=4",            4, 1, 1, 1, 1, 8, 7},
            {"pad mismatch",   3, 1, 1, 0, 1, 8, 7},
            {"span>7",         5, 1, 3, 6, 1, 8, 7},
            // 通道上界随硬件容量走：v2 是 112/96，v3 起 CIN_MAX=352 /
            // COUT_MAX=192（见 conv2d_engine.h）。改容量时这里要一起改，
            // 否则"应被拒绝"的参数变成合法参数，引擎会真的去读写下面
            // 那几个只按旧上界分配的小缓冲区——越界写会把堆写坏（实测崩）。
            {"cin>352",        1, 1, 1, 0, 353, 8, 7},
            {"cout>192",       1, 1, 1, 0, 1, 193, 7},
            {"stride=3",       1, 3, 1, 0, 1, 8, 7},
            {"dil=4",          1, 1, 4, 0, 1, 8, 7},
            {"shift=0",        1, 1, 1, 0, 1, 8, 0},
            {"shift=32",       1, 1, 1, 0, 1, 8, 32},
        };
        for (const Bad &x : bads) {
            const int rc = conv2d_engine(in.data(), w.data(), b.data(), out.data(),
                                         8, 8, 8, 8, x.ci, x.co, x.k, x.s, x.d, x.p, x.sh, 1);
            if (rc == ERR_OK) {
                printf("FAIL: 非法参数被接受: %s\n", x.name);
                ++fails;
            }
        }
        const int rc = conv2d_engine(0, w.data(), b.data(), out.data(),
                                     8, 8, 8, 8, 1, 8, 1, 1, 1, 0, 7, 1);
        if (rc != ERR_NULL) { printf("FAIL: 空指针未被拒绝\n"); ++fails; }

        // H/W 超上界（4097）必须被拒绝
        {
            const int rc2 = conv2d_engine(in.data(), w.data(), b.data(), out.data(),
                                          4097, 8, 4097, 8, 1, 8, 1, 1, 1, 0, 7, 1);
            if (rc2 != ERR_SIZE) { printf("FAIL: H=4097 未被拒绝\n"); ++fails; }
        }
        // Hout/Wout 与公式不一致必须被拒绝
        {
            const int rc3 = conv2d_engine(in.data(), w.data(), b.data(), out.data(),
                                           8, 8, 9, 8, 1, 8, 1, 1, 1, 0, 7, 1);
            if (rc3 != ERR_SIZE) { printf("FAIL: 错的 Hout 未被拒绝\n"); ++fails; }
            const int rc4 = conv2d_engine(in.data(), w.data(), b.data(), out.data(),
                                           9, 130, 5, 66, 48, 96, 3, 2, 1, 1, 7, 1);
            if (rc4 != ERR_SIZE) { printf("FAIL: 错的 Wout(s=2) 未被拒绝\n"); ++fails; }
        }
        printf("非法参数用例完成, fails=%d\n", fails);
    }

    if (fails == 0) {
        printf("==== FUZZ ALL PASS（%d 个用例，relu=1/0 双模式）====\n", g_cases);
        return 0;
    }
    printf("==== FUZZ FAILED ====\n");
    return 1;
}
