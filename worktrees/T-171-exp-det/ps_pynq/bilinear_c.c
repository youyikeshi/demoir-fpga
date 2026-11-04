/* bilinear_c.c —— 双线性插值 f32 的 C 内核，**逐字节等价**于
 * `bin_ref_numpy.bilinear_f32` → `_bilinear_slab`。
 *
 * 为什么要有它：板端 v39 实测 26 次 interp = 1.907 s（PS 桶里最大单项），
 * 有效吞吐 57.48 MB / 1.907 s ≈ 30 MB/s —— 瓶颈是 numpy 的 4 次 fancy-index
 * gather（`np.take(..., out=)`）在 2 核 Cortex-A9 上的表现，不是算术。
 *
 * ★ 等价性来自"逐元素、同顺序"，与遍历顺序无关：
 *     r = fl(a*w00);  r = fl(r + b*w01);  r = fl(r + c*w10);  r = fl(r + d*w11)
 *   其中 a,b,c,d = x[c][ i00[p] ], x[c][ i01[p] ], x[c][ i10[p] ], x[c][ i11[p]]
 *   （flat 索引 = y*W + x；i/w 表由 PS 侧 `_idx4()` 按**现有代码**算好传进来，
 *     所以坐标与权重的舍入**完全不在本文件里**，不存在"重算权重"的风险）。
 *   每个乘、每个加都单独落 float（-ffp-contract=off ⇒ 不许合成 FMA）。
 *
 * ★ 禁止项：-ffast-math / -Ofast（会开 flush-to-zero 与重排）、FMA 收缩。
 *   编译命令见 bilinear_c.py（CFLAGS 显式带 -ffp-contract=off -fno-fast-math）。
 *
 * 契约：
 *   x     : (C,H,W) C 连续 float32，只读
 *   i00.. : n = HO*WO 个扁平源索引（int32，已按 y*W+x 算好、已钳位）
 *   w00.. : n 个 float32 权重（已按 (1-ty)(1-tx) 等算好并落 f32）
 *   out   : (C, HO*WO) C 连续 float32；c0..c1 是本线程负责的通道区间
 *   ⇒ 任意 c 区间切分的结果逐字节相同（逐元素独立）。
 *
 * 版本号改动请 +1（bilinear_c.py 用它判断 .so 是否过期，板子时钟可能比源码旧）。
 *
 * ★ v2（2026-10-31，性能）：`bilinear_slab_v2` —— 把"索引/权重表"从**每个输出元素
 *   读 8 次**降到**每像素块读 1 次、再对 C 个通道复用**：
 *     循环顺序 ho → wo 块(16) → c → j
 *   一块（16 像素）的 4 索引 + 4 权重 = 512 B，进栈（1 KB）后所有通道共用；
 *   源数据 = 2 条源行 × ~8 像素 × C × 4 B（C=64 时 4 KB）⇒ 落在 L1 里。
 *   v1 的每元素 8 次表读取（32 B/element）是 144 MB/s 的主要来源。
 *   **逐元素运算顺序与 v1 完全相同**（同一个 r 链），所以 v2 也逐字节等价。
 */
/* 板端实测（hw=256，26 个真实形状，理想流量 57.5 MB）：
 *   numpy 1.95 s (30 MB/s) → v1/v2/v3/v4 ≈0.256~0.266 s (≈223 MB/s) = 7.3~7.6×；
 *   memcpy 16 MB = 392 MB/s ⇒ 已到 memcpy 的 57%；四个变体之差在板端噪声内。
 *   默认 v4（权重现算，省掉 123 MB 的权重表读，结果逐字节相同）。
 */
#include <stdint.h>
#include <stddef.h>

int bilinear_version(void) { return 4; }

/* 单通道区间的核心：按输出行 → 通道 → 列 的顺序，使源行与索引/权重表都能留在
 * L1/L2 里（一行的表 = 4×4 B×WO + 4×4 B×WO = 32·WO B，WO=128 时 4 KB）。 */
void bilinear_slab(const float *x, int H, int W,
                   const int32_t *i00, const int32_t *i01,
                   const int32_t *i10, const int32_t *i11,
                   const float *w00, const float *w01,
                   const float *w10, const float *w11,
                   int HO, int WO, int c0, int c1, float *out)
{
    const size_t hw = (size_t)H * (size_t)W;
    const size_t n = (size_t)HO * (size_t)WO;

    for (int ho = 0; ho < HO; ++ho) {
        const size_t p0 = (size_t)ho * (size_t)WO;
        for (int c = c0; c < c1; ++c) {
            const float *xa = x + (size_t)c * hw;
            float *op = out + (size_t)c * n + p0;
            for (int wo = 0; wo < WO; ++wo) {
                const size_t p = p0 + (size_t)wo;
                /* 运算顺序与 _bilinear_slab 逐项一致：4 乘 3 加，各自落 float */
                float r = xa[i00[p]] * w00[p];
                r = r + xa[i01[p]] * w01[p];
                r = r + xa[i10[p]] * w10[p];
                r = r + xa[i11[p]] * w11[p];
                op[wo] = r;
            }
        }
    }
}

/* ---------------- v4：行驻留 + 权重现算（逐字节等价）
 * v3 剩下的最大一块流量是**权重表**：每个输出元素 4 个 f32 = 16 B，26 个算子合计
 * 4×7.68M×4 B ≈ 123 MB，比 in+out（57.5 MB）还大。
 * 但权重本来就是 `w00=(1-ty)(1-tx)`、`w01=(1-ty)tx`、`w10=ty(1-tx)`、`w11=ty*tx`，
 * 而 ty/tx 只有 HO/WO 个（几百字节，L1 常驻）。
 *  ⇒ 只传 ty[HO]、tx[WO]，在核里用 **同样的 f32 运算**现算 4 个权重：
 *     numpy 的 `(1-ty)` 是 float32 减、`*` 是 float32 乘 ⇒ 现算结果**逐位相同**。
 *     省掉 16 B/元素的表读，代价是每个元素 4 次乘（A9 上 4 拍，换 4 条 cache line）。
 * --------------------------------------------------------------------------- */
void bilinear_slab_v4(const float *restrict x, int H, int W,
                      const int32_t *restrict x0c, const int32_t *restrict x1c,
                      const int32_t *restrict row0, const int32_t *restrict row1,
                      const float *restrict ty, const float *restrict tx,
                      int HO, int WO, int c0, int c1, float *restrict out)
{
    const size_t hw = (size_t)H * (size_t)W;
    const size_t n = (size_t)HO * (size_t)WO;

    for (int ho = 0; ho < HO; ++ho) {
        const size_t p0 = (size_t)ho * (size_t)WO;
        const float by = ty[ho];
        const float ay = 1.0f - by;
        const size_t s0off = (size_t)row0[ho] * (size_t)W;
        const size_t s1off = (size_t)row1[ho] * (size_t)W;
        for (int c = c0; c < c1; ++c) {
            const float *s0 = x + (size_t)c * hw + s0off;
            const float *s1 = x + (size_t)c * hw + s1off;
            float *op = out + (size_t)c * n + p0;
            for (int wo = 0; wo < WO; ++wo) {
                const int X0 = x0c[wo], X1 = x1c[wo];
                const float bxx = tx[wo];
                const float axx = 1.0f - bxx;
                /* 与 _idx4 里 `((1-ty)*(1-tx))` 等四式**同值同序** */
                const float w00 = ay * axx;
                const float w01 = ay * bxx;
                const float w10 = by * axx;
                const float w11 = by * bxx;
                float r = s0[X0] * w00;
                r = r + s0[X1] * w01;
                r = r + s1[X0] * w10;
                r = r + s1[X1] * w11;
                op[wo] = r;
            }
        }
    }
}

/* ---------------- v3：行驻留（逐字节等价，见文件头）
 * 观察：同一输出行 ho 只用 x 的**两条源行** y0(ho)、y1(ho)；横向坐标只取决于 wo。
 *  ⇒ 把 flat 索引拆成 (行, 列)：kernel 里只保留 2 个 WO 长的列索引表（L1 常驻），
 *    源数据用 `s0 = x + c*HW + y0*W` 喷出两行指针 ⇒ 4 次载入全部是 L1 命中。
 *  v1/v2 每个元素要读 8 个 n 长的表（32 B），v3 只读 2 个小表 + 4 个权重（L1 复用）。
 *  **逐元素算式与 v1 完全一致**（同一个 r 链）⇒ 逐字节等价。
 * --------------------------------------------------------------------------- */
void bilinear_slab_v3(const float *restrict x, int H, int W,
                      const int32_t *restrict x0c, const int32_t *restrict x1c,
                      const int32_t *restrict row0, const int32_t *restrict row1,
                      const float *restrict w00, const float *restrict w01,
                      const float *restrict w10, const float *restrict w11,
                      int HO, int WO, int c0, int c1, float *restrict out)
{
    const size_t hw = (size_t)H * (size_t)W;
    const size_t n = (size_t)HO * (size_t)WO;

    for (int ho = 0; ho < HO; ++ho) {
        const size_t p0 = (size_t)ho * (size_t)WO;
        const size_t s0off = (size_t)row0[ho] * (size_t)W;
        const size_t s1off = (size_t)row1[ho] * (size_t)W;
        for (int c = c0; c < c1; ++c) {
            const float *s0 = x + (size_t)c * hw + s0off;
            const float *s1 = x + (size_t)c * hw + s1off;
            float *op = out + (size_t)c * n + p0;
            for (int wo = 0; wo < WO; ++wo) {
                const size_t p = p0 + (size_t)wo;
                const int X0 = x0c[wo], X1 = x1c[wo];
                float r = s0[X0] * w00[p];
                r = r + s0[X1] * w01[p];
                r = r + s1[X0] * w10[p];
                r = r + s1[X1] * w11[p];
                op[wo] = r;
            }
        }
    }
}

/* ---------------- v2：表复用 + 块化（逐字节等价，见文件头） ---------------- */
#define BIL_WB 16
void bilinear_slab_v2(const float *restrict x, int H, int W,
                      const int32_t *restrict i00, const int32_t *restrict i01,
                      const int32_t *restrict i10, const int32_t *restrict i11,
                      const float *restrict w00, const float *restrict w01,
                      const float *restrict w10, const float *restrict w11,
                      int HO, int WO, int c0, int c1, float *restrict out)
{
    const size_t hw = (size_t)H * (size_t)W;
    const size_t n = (size_t)HO * (size_t)WO;

    for (int ho = 0; ho < HO; ++ho) {
        const size_t p0 = (size_t)ho * (size_t)WO;
        for (int w0 = 0; w0 < WO; w0 += BIL_WB) {
            const int nb = (WO - w0 < BIL_WB) ? (WO - w0) : BIL_WB;
            const size_t pb = p0 + (size_t)w0;
            int32_t I0[BIL_WB], I1[BIL_WB], J0[BIL_WB], J1[BIL_WB];
            float W0[BIL_WB], W1[BIL_WB], W2[BIL_WB], W3[BIL_WB];
            for (int j = 0; j < nb; ++j) {
                I0[j] = i00[pb + j]; I1[j] = i01[pb + j];
                J0[j] = i10[pb + j]; J1[j] = i11[pb + j];
                W0[j] = w00[pb + j]; W1[j] = w01[pb + j];
                W2[j] = w10[pb + j]; W3[j] = w11[pb + j];
            }
            for (int c = c0; c < c1; ++c) {
                const float *xa = x + (size_t)c * hw;
                float *op = out + (size_t)c * n + pb;
                for (int j = 0; j < nb; ++j) {
                    float r = xa[I0[j]] * W0[j];
                    r = r + xa[I1[j]] * W1[j];
                    r = r + xa[J0[j]] * W2[j];
                    r = r + xa[J1[j]] * W3[j];
                    op[j] = r;
                }
            }
        }
    }
}

/* 便于自检：把 (C,H,W) 当 C 个独立 slab 一次跑完（单线程）。 */
void bilinear_all_v2(const float *x, int C, int H, int W,
                     const int32_t *i00, const int32_t *i01,
                     const int32_t *i10, const int32_t *i11,
                     const float *w00, const float *w01,
                     const float *w10, const float *w11,
                     int HO, int WO, float *out)
{
    bilinear_slab_v2(x, H, W, i00, i01, i10, i11, w00, w01, w10, w11,
                     HO, WO, 0, C, out);
}

void bilinear_all(const float *x, int C, int H, int W,
                  const int32_t *i00, const int32_t *i01,
                  const int32_t *i10, const int32_t *i11,
                  const float *w00, const float *w01,
                  const float *w10, const float *w11,
                  int HO, int WO, float *out)
{
    bilinear_slab(x, H, W, i00, i01, i10, i11, w00, w01, w10, w11,
                  HO, WO, 0, C, out);
}
