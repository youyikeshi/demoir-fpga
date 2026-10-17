// 膨胀卷积的手算用例。
//
// 必须用下面这组参数编译：
//   -DCIN=1 -DCOUT=1 -DKSIZE=3 -DPAD=2 -DDILATION=2
//
// 构造：输入 5×5，只有 (0,0) 是 1，其余 0；权重只有中心抽头 (ky=1,kx=1) 是 5；
// bias 0，shift 1。
//
// 膨胀率 2、核 3 时，抽头 (ky,kx) 落到输入 (y-2+2*ky, x-2+2*kx)。
// 中心抽头 (1,1) 落到 (y-2+2, x-2+2) = (y, x)，正好是同位。
// 所以只有 (0,0) 这一点的输出非零：期望 out(0,0) = (1*5) >> 1 = 2，其余全 0。
//
// 选中心抽头是有意的：它同时用上纵向和横向的跨步。
// 若实现把 2*ky 写成 ky，中心抽头会落到 (y-1, x)，输出跑到 (1,0)；
// 若横向跨步也写错，输出会跑到 (0,1)。两种情况这个用例都会失败。
// （用 (0,0) 那个角抽头是抓不到的：0*d 恒等于 0，变异对它没有影响。）

#include "conv2d_accel.h"

#include <cstdio>

int main() {
    const int H = 5;
    const int W = 5;
    const int shift = 1;

    data_t in_fm[CIN * H * W];
    data_t wgt[COUT * CIN * KSIZE * KSIZE];
    bias_t bias[COUT];
    data_t out[COUT * H * W];

    for (int i = 0; i < CIN * H * W; ++i) in_fm[i] = 0;
    for (int i = 0; i < COUT * CIN * KSIZE * KSIZE; ++i) wgt[i] = 0;
    for (int i = 0; i < COUT; ++i) bias[i] = 0;
    for (int i = 0; i < COUT * H * W; ++i) out[i] = -1;

    in_fm[0] = 1;  // 输入通道 0 的 (0,0)

    // 权重布局 [oc][ic][ky][kx]，只保留中心抽头 ky=1, kx=1
    wgt[((0 * CIN + 0) * KSIZE + 1) * KSIZE + 1] = 5;

    const int rc = conv2d_accel(in_fm, wgt, bias, out, H, W, shift);
    if (rc != ERR_OK) {
        printf("FAIL: conv2d_accel 返回 %d\n", rc);
        return 1;
    }

    // 期望：只有 (0,0) 等于 2
    for (int y = 0; y < H; ++y) {
        for (int x = 0; x < W; ++x) {
            const int got = (int)out[y * W + x];
            const int want = (y == 0 && x == 0) ? 2 : 0;
            if (got != want) {
                printf("FAIL: out(%d,%d) = %d，期望 %d\n", y, x, got, want);
                return 1;
            }
        }
    }

    printf("PASS: 膨胀用例（中心抽头落在 (0,0)，纵向与横向跨步都对）\n");
    return 0;
}
