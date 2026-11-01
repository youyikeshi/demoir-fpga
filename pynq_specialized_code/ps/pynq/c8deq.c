/* c8deq.c —— C8 交错 int8 → (cg*8,H,W) float32 + 乘 2^-fs（2 的幂 ⇒ 精确）
 *
 * 为什么需要它（板端实测，见 runs/epi256/REPORT.md）：
 *   PYNQ 的 DMA 输出缓冲是**非缓存**的。`from_c8_deq` 原来那句
 *     np.multiply(int8 跨步视图, k, out=float32, casting="unsafe")
 *   会以 **1 字节/次** 的标量访存去读它（8 次跨步遍历），在 A9 上
 *   41 MB 要 5.45 s（≈37 MB/s）。旧的 `.tobytes()` 版反而只要 2.7 s
 *   —— 因为 memcpy 是 burst 读。这就是 epilogue 3.28 → 5.57 s 的全部原因。
 *
 *   本内核把"读"改成 **每 4 像素一次 32 B 连续载入**（4×uint64），
 *   并把"解交错 + int8→f32 + 乘 2^-fs"合成一趟；写是 (cg*8,H,W) 的 8 个顺序流。
 *   调用方先把 DMA 缓冲 memcpy 到缓存（`bytes`，0.18 s/41 MB），内核只读缓存。
 *
 * 位精确：int8→float32 精确；乘 2^-fs 是 2 的幂、无溢出/下溢到 0 之外 ⇒ 逐位相同。
 * 自检见 c8deq.py（0/144 用例不同才启用；否则静默回落 numpy）。
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>

static inline void one8(uint64_t v, float *o, size_t HW, float k)
{
    o[0*HW] = (float)(int8_t)(v & 0xff) * k;
    o[1*HW] = (float)(int8_t)((v >> 8) & 0xff) * k;
    o[2*HW] = (float)(int8_t)((v >> 16) & 0xff) * k;
    o[3*HW] = (float)(int8_t)((v >> 24) & 0xff) * k;
    o[4*HW] = (float)(int8_t)((v >> 32) & 0xff) * k;
    o[5*HW] = (float)(int8_t)((v >> 40) & 0xff) * k;
    o[6*HW] = (float)(int8_t)((v >> 48) & 0xff) * k;
    o[7*HW] = (float)(int8_t)((v >> 56) & 0xff) * k;
}

void c8deq_chw(const int8_t *raw, float *out, int cg, int H, int W, float k)
{
    size_t HW = (size_t)H * W;
    for (int g = 0; g < cg; ++g) {
        const int8_t *p = raw + (size_t)g * HW * 8;
        float *o0 = out + (size_t)g * 8 * HW;
        size_t i = 0;
        for (; i + 4 <= HW; i += 4) {
            uint64_t v0, v1, v2, v3;
            memcpy(&v0, p + (i + 0) * 8, 8);
            memcpy(&v1, p + (i + 1) * 8, 8);
            memcpy(&v2, p + (i + 2) * 8, 8);
            memcpy(&v3, p + (i + 3) * 8, 8);
            one8(v0, o0 + i + 0, HW, k);
            one8(v1, o0 + i + 1, HW, k);
            one8(v2, o0 + i + 2, HW, k);
            one8(v3, o0 + i + 3, HW, k);
        }
        for (; i < HW; ++i) {
            uint64_t v;
            memcpy(&v, p + i * 8, 8);
            one8(v, o0 + i, HW, k);
        }
    }
}

int c8deq_version(void) { return 1; }
