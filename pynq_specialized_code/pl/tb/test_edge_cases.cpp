// 边界测试：覆盖设计文档 Review Focus 里的五条。
// 每条都在 conv2d_accel 和 conv_ref 上各跑一遍，要求两者返回同样的错误码。

#include "conv2d_accel.h"

#include <cstdio>
#include <cstdlib>

namespace {

int g_fail = 0;

void check(bool ok, const char *what) {
    if (!ok) {
        printf("FAIL: %s\n", what);
        ++g_fail;
    }
}

}  // namespace

int main() {
    const int H = 1;
    const int W = 1;
    const int shift = 7;

    // 尺寸按最大情况分配，避免检查缺失时越界
    const int big_w = 513;
    data_t *in_fm = (data_t *)calloc((size_t)CIN * H * big_w, 1);
    data_t *wgt = (data_t *)calloc((size_t)COUT * CIN * KSIZE * KSIZE, 1);
    bias_t *bias = (bias_t *)calloc((size_t)COUT, sizeof(bias_t));
    data_t *out = (data_t *)calloc((size_t)COUT * H * big_w, 1);

    // 第 1 条：W 超过上限
    check(conv2d_accel(in_fm, wgt, bias, out, H, big_w, shift) == ERR_SIZE,
          "W=513 时 conv2d_accel 应返回 ERR_SIZE");
    check(conv_ref(in_fm, wgt, bias, out, H, big_w, shift) == ERR_SIZE,
          "W=513 时 conv_ref 应返回 ERR_SIZE");

    // 第 2 条：H 为 0
    check(conv2d_accel(in_fm, wgt, bias, out, 0, W, shift) == ERR_SIZE,
          "H=0 时 conv2d_accel 应返回 ERR_SIZE");
    check(conv_ref(in_fm, wgt, bias, out, 0, W, shift) == ERR_SIZE,
          "H=0 时 conv_ref 应返回 ERR_SIZE");

    // 第 3 条：正向饱和。输入和权重全 127，bias 拉满，期望 127 而不是回绕成负数
    for (int i = 0; i < CIN * H * W; ++i) in_fm[i] = 127;
    for (int i = 0; i < COUT * CIN * KSIZE * KSIZE; ++i) wgt[i] = 127;
    for (int i = 0; i < COUT; ++i) bias[i] = 100000;
    out[0] = 0;

    check(conv2d_accel(in_fm, wgt, bias, out, H, W, 1) == ERR_OK,
          "饱和用例里 conv2d_accel 应返回 ERR_OK");
    check((int)out[0] == 127, "饱和用例里 conv2d_accel 输出应为 127");

    out[0] = 0;
    check(conv_ref(in_fm, wgt, bias, out, H, W, 1) == ERR_OK,
          "饱和用例里 conv_ref 应返回 ERR_OK");
    check((int)out[0] == 127, "饱和用例里 conv_ref 输出应为 127");

    // 恢复成普通数据
    for (int i = 0; i < CIN * H * W; ++i) in_fm[i] = 1;
    for (int i = 0; i < COUT * CIN * KSIZE * KSIZE; ++i) wgt[i] = 1;
    for (int i = 0; i < COUT; ++i) bias[i] = 0;

    // 第 4 条：shift 越界
    check(conv2d_accel(in_fm, wgt, bias, out, H, W, 0) == ERR_SHIFT,
          "shift=0 时 conv2d_accel 应返回 ERR_SHIFT");
    check(conv_ref(in_fm, wgt, bias, out, H, W, 0) == ERR_SHIFT,
          "shift=0 时 conv_ref 应返回 ERR_SHIFT");
    check(conv2d_accel(in_fm, wgt, bias, out, H, W, 32) == ERR_SHIFT,
          "shift=32 时 conv2d_accel 应返回 ERR_SHIFT");
    check(conv_ref(in_fm, wgt, bias, out, H, W, 32) == ERR_SHIFT,
          "shift=32 时 conv_ref 应返回 ERR_SHIFT");

    // 第 5 条：四个指针分别为空
    check(conv2d_accel(0, wgt, bias, out, H, W, shift) == ERR_NULL,
          "in_fm 为空时 conv2d_accel 应返回 ERR_NULL");
    check(conv_ref(0, wgt, bias, out, H, W, shift) == ERR_NULL,
          "in_fm 为空时 conv_ref 应返回 ERR_NULL");
    check(conv2d_accel(in_fm, 0, bias, out, H, W, shift) == ERR_NULL,
          "wgt 为空时 conv2d_accel 应返回 ERR_NULL");
    check(conv_ref(in_fm, 0, bias, out, H, W, shift) == ERR_NULL,
          "wgt 为空时 conv_ref 应返回 ERR_NULL");
    check(conv2d_accel(in_fm, wgt, 0, out, H, W, shift) == ERR_NULL,
          "bias 为空时 conv2d_accel 应返回 ERR_NULL");
    check(conv_ref(in_fm, wgt, 0, out, H, W, shift) == ERR_NULL,
          "bias 为空时 conv_ref 应返回 ERR_NULL");
    check(conv2d_accel(in_fm, wgt, bias, 0, H, W, shift) == ERR_NULL,
          "out_fm 为空时 conv2d_accel 应返回 ERR_NULL");
    check(conv_ref(in_fm, wgt, bias, 0, H, W, shift) == ERR_NULL,
          "out_fm 为空时 conv_ref 应返回 ERR_NULL");

    free(in_fm);
    free(wgt);
    free(bias);
    free(out);

    if (g_fail != 0) {
        printf("边界测试失败 %d 项\n", g_fail);
        return 1;
    }
    printf("PASS: 边界测试全部通过\n");
    return 0;
}
