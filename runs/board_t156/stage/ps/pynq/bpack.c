/* bpack.c —— 激活二值打包的 C 内核（compare + 64-bit 字组装），逐字节等价于
 * `bin_engine.quant_pack`（numpy 版）。
 *
 * 为什么要有它：板端 256² 整网里 prologue 25.80 s 的 97% 是这一步，而它离内存带宽
 * 下限还有 ~10-20× —— 慢在 numpy 的逐元素 ufunc/packbits，不在算法。C 版把
 * "比较 + 组字节 + 组 64-bit 字" 融成**一趟顺序读 x**，中间不落任何临时数组。
 *
 * 两个入口：
 *   bpack_f32(x, adj, cin, hw, cb, out)   bit = (x[c*hw+p] > adj[c])
 *   bpack_i8 (y, t,   cin, hw, cb, out)   bit = (y[c*hw+p] >= t[c])   ← int8 激活直通
 *
 * 输出布局（与 numpy 版逐字节相同）：
 *   out[b*hw + p] 是一个 64-bit 字，小端 8 字节；第 i 个字节 = 通道组 (8b+i) 的 8 位，
 *   字节内第 k 位 = 通道 (8b+i)*8+k；通道号 ≥ cin 的补零位恒 0。
 *
 * ★ 循环顺序是性能关键（第一版踩过坑）：
 *   x 是 (cin, hw) 行主序，通道之间隔 hw*4 字节。如果按"逐像素、内层扫通道"写，
 *   连续两次 load 相隔 hw*4 字节（128² 时 64 KB ⇒ 每次 load 换一个页/TLB，
 *   硬件预取完全失效），实测比 numpy 还慢 2.4×。
 *   所以这里**按像素分块**：外层取一块 P 个像素，内层对每个通道顺序扫这 P 个像素
 *   ⇒ 每次访问都是同一行的连续 4*P 字节，可预取、每 1024 个元素才换一次页。
 *
 * 编译（板端首次使用时由 bpack.py 现场编译，和 i8_fast.c 同样的约定）：
 *   cc -O3 -std=c99 -fPIC -shared -o bpack_armv7l.so bpack.c
 * 本文件**只做 CPU 侧计算**，不碰任何寄存器/PL 交互。
 */
#include <stdint.h>
#include <string.h>

#ifndef BPACK_P
#define BPACK_P 512          /* 像素块大小：一块 = cin * P * 4 字节的顺序读 */
#endif

void bpack_f32(const float *x, const float *adj, int cin, int hw, int cb,
               uint64_t *out)
{
    int b, i, k, p, p0;
    size_t hwz = (size_t)hw;
    memset(out, 0, sizeof(uint64_t) * (size_t)cb * hwz);
    for (p0 = 0; p0 < hw; p0 += BPACK_P) {
        int n = hw - p0;
        if (n > BPACK_P)
            n = BPACK_P;
        for (b = 0; b < cb; ++b) {
            uint64_t *o = out + (size_t)b * hwz + p0;
            for (p = 0; p < n; ++p)
                o[p] = 0;
            for (i = 0; i < 8; ++i) {
                int c0 = (b * 8 + i) * 8;
                int nk;
                if (c0 >= cin)
                    break;
                nk = cin - c0;
                if (nk > 8)
                    nk = 8;
                for (k = 0; k < nk; ++k) {
                    const float *xp = x + (size_t)(c0 + k) * hwz + p0;
                    const float th = adj[c0 + k];
                    const uint64_t sh = (uint64_t)(8 * i + k);
                    for (p = 0; p < n; ++p)
                        o[p] |= (uint64_t)(xp[p] > th) << sh;
                }
            }
        }
    }
}

void bpack_i8(const int8_t *y, const int16_t *t, int cin, int hw, int cb,
              uint64_t *out)
{
    int b, i, k, p, p0;
    size_t hwz = (size_t)hw;
    memset(out, 0, sizeof(uint64_t) * (size_t)cb * hwz);
    for (p0 = 0; p0 < hw; p0 += BPACK_P) {
        int n = hw - p0;
        if (n > BPACK_P)
            n = BPACK_P;
        for (b = 0; b < cb; ++b) {
            uint64_t *o = out + (size_t)b * hwz + p0;
            for (p = 0; p < n; ++p)
                o[p] = 0;
            for (i = 0; i < 8; ++i) {
                int c0 = (b * 8 + i) * 8;
                int nk;
                if (c0 >= cin)
                    break;
                nk = cin - c0;
                if (nk > 8)
                    nk = 8;
                for (k = 0; k < nk; ++k) {
                    const int8_t *yp = y + (size_t)(c0 + k) * hwz + p0;
                    const int th = (int)t[c0 + k];
                    const uint64_t sh = (uint64_t)(8 * i + k);
                    for (p = 0; p < n; ++p)
                        o[p] |= (uint64_t)((int)yp[p] >= th) << sh;
                }
            }
        }
    }
}

/* 让 Python 侧能确认"这是新版内核"，而不是加载到了旧 .so */
int bpack_version(void)
{
    return 3;
}
