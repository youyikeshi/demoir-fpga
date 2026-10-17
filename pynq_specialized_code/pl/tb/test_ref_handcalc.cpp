// 手算用例：钉住 conv_ref 的行为，防止参考实现本身写错。
// 输入只有第 0 通道是 10，权重只有第 0 输出通道第 0 输入通道的中心是 2，
// bias[0] 是 3，shift 是 1。期望 (10*2 + 3) >> 1 = 11。

#include "conv2d_accel.h"
#include <cstdio>

int main() {
    const int H = 1;
    const int W = 1;
    const int shift = 1;

    data_t in_fm[CIN * H * W];
    data_t wgt[COUT * CIN * KSIZE * KSIZE];
    bias_t bias[COUT];
    data_t out[COUT * H * W];

    for (int i = 0; i < CIN * H * W; ++i) in_fm[i] = 0;
    for (int i = 0; i < COUT * CIN * KSIZE * KSIZE; ++i) wgt[i] = 0;
    for (int i = 0; i < COUT; ++i) bias[i] = 0;
    for (int i = 0; i < COUT * H * W; ++i) out[i] = -1;

    in_fm[0] = 10;
    bias[0] = 3;

    // 权重布局是 [oc][ic][ky][kx]，中心落在 ky=2, kx=2
    const int center = ((0 * CIN + 0) * KSIZE + 2) * KSIZE + 2;
    wgt[center] = 2;

    const int rc = conv_ref(in_fm, wgt, bias, out, H, W, shift);
    if (rc != ERR_OK) {
        printf("FAIL: 返回码 %d，期望 %d\n", rc, ERR_OK);
        return 1;
    }

    if (out[0] != 11) {
        printf("FAIL: out[0] = %d，期望 11\n", (int)out[0]);
        return 1;
    }

    // 输出通道 1 的权重和 bias 都是 0，期望输出 0
    if (out[1] != 0) {
        printf("FAIL: out[1] = %d，期望 0\n", (int)out[1]);
        return 1;
    }

    printf("PASS\n");
    return 0;
}
