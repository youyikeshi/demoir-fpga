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

/* ---------------------------------------------------------------------------
 * ★ T-18 刀④（`BIN_DEQ_TILE=1`，默认关）：tile 化版 `c8deq_chw_t`
 *
 * 动机（为什么不是"再调调参"）：上面那一版对**每个像素**写 8 个相距 HW*4 B 的
 * f32 ⇒ 8 条交错写流，每条流的相邻写之间隔了 4 个 cache line。A9 的 L2 是
 * PL310（32 B/line、write-allocate）⇒ 这种"半线写"每个 line 都要先 RFO 读回来，
 * 写 33 MB 实际要在 DDR 上走 66 MB，而读只有 8 MB ⇒ 有效速率 0.2 GB/s
 * （板端实测 41.66 MB / 0.194 s，见 runs/t18_smallops/REPORT_attribution.md §3）。
 *
 * 这一版把它拆成两相：**先把一小片（TI 个像素 ≈ 8 KB）读进 L1**，
 * 再**按通道**把这一片整块写出去（每次 TI 个连续 float = 4~8 KB = 整条整条的
 * cache line 覆盖写）⇒ 写不再需要 RFO，读仍然是一次顺序扫过。
 * 位精确：每个元素仍是 `(float)(int8_t)b * k`，与 `one8()` 逐条相同。
 * ------------------------------------------------------------------------- */
#define C8DEQ_TI 512

void c8deq_chw_t(const int8_t *raw, float *out, int cg, int H, int W, float k)
{
    size_t HW = (size_t)H * W;
    float tile[8][C8DEQ_TI];          /* 8 通道 × TI 像素 ≈ 16 KB，常驻 L1 */
    for (int g = 0; g < cg; ++g) {
        const int8_t *p0 = raw + (size_t)g * HW * 8;
        float *o0 = out + (size_t)g * 8 * HW;
        size_t i0 = 0;
        while (i0 < HW) {
            size_t n = HW - i0;
            if (n > (size_t)C8DEQ_TI) n = (size_t)C8DEQ_TI;
            const int8_t *p = p0 + i0 * 8;
            /* ① 一趟顺序读：每 8 B 是一个像素的 8 个通道 */
            for (size_t j = 0; j < n; ++j) {
                uint64_t v;
                memcpy(&v, p + j * 8, 8);
                tile[0][j] = (float)(int8_t)(v & 0xff) * k;
                tile[1][j] = (float)(int8_t)((v >> 8) & 0xff) * k;
                tile[2][j] = (float)(int8_t)((v >> 16) & 0xff) * k;
                tile[3][j] = (float)(int8_t)((v >> 24) & 0xff) * k;
                tile[4][j] = (float)(int8_t)((v >> 32) & 0xff) * k;
                tile[5][j] = (float)(int8_t)((v >> 40) & 0xff) * k;
                tile[6][j] = (float)(int8_t)((v >> 48) & 0xff) * k;
                tile[7][j] = (float)(int8_t)((v >> 56) & 0xff) * k;
            }
            /* ② 每个通道写一整块连续 f32（整条 cache line 覆盖写 ⇒ 不该再有 RFO） */
            for (int c = 0; c < 8; ++c)
                memcpy(o0 + (size_t)c * HW + i0, tile[c], n * sizeof(float));
            i0 += n;
        }
    }
}

int c8deq_version(void) { return 2; }
