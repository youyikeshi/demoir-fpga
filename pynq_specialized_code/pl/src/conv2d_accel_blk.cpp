// 横向分块版卷积内核（实验版本，独立于 conv2d_accel.cpp）。
//
// 动机：原版行缓冲按"整幅图宽"分配（CIN × SPAN × (MAX_W + 2P)），是六个
// 特化 IP 放不进一片 FPGA 的最大单一开销。这里把输出按列切成 BLOCK_W 宽的
// 小块、逐块处理：行缓冲只需装当前块覆盖的那几列输入。
//
// 行缓冲数据量从 CIN×SPAN×(MAX_W+2P) 降到 CIN×SPAN×BWCOL，其中
//   BWCOL = (BLOCK_W-1)*STRIDE + SPAN + 2*PAD   （SPAN = (K-1)*d + 1）
// 对 K=3/s1/d1/PAD=1/BLOCK_W=64：BWCOL = 68（原来按 194~516 列分配）。
//
// 边界处理：块内左/右越界的位置补零（只有首块和尾块真正发生），中间块
// 直接从 DDR 相邻位置拷贝，不需要逐列边界判断。
//
// 形状由编译期常量决定，和原版一致；BLOCK_W 用 -D 覆盖。

#include "conv2d_accel.h"

// 输出列的分块宽度
#ifndef BLOCK_W
#define BLOCK_W 64
#endif

// 窗口跨越的输入行数。膨胀率为 d 时，窗口跨越 (K-1)*d+1 行输入。
#define SPAN ((KSIZE - 1) * DILATION + 1)

// 一个输出块覆盖的输入列数（含左右补零）。
#define BWCOL ((BLOCK_W - 1) * STRIDE + SPAN + 2 * PAD)

// 设计空间探索旋钮，与原版一致
#ifndef UNROLL_K
#define UNROLL_K 25
#endif
#ifndef PIPELINE_II
#define PIPELINE_II 1
#endif

#if UNROLL_K <= 1
#define UNROLL_KX 1
#define UNROLL_KY 1
#elif UNROLL_K <= 5
#define UNROLL_KX 5
#define UNROLL_KY 1
#else
#define UNROLL_KX 5
#define UNROLL_KY 5
#endif

namespace {

inline data_t saturate_int8(int32_t v) {
    if (v > 127) return (data_t)127;
    if (v < -128) return (data_t)(-128);
    return (data_t)v;
}

}  // namespace

int conv2d_accel(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
                 data_t *out_fm, int H, int W, int shift) {
    HLS_PRAGMA(HLS INTERFACE m_axi port=in_fm offset=slave bundle=gmem0)
    HLS_PRAGMA(HLS INTERFACE m_axi port=wgt offset=slave bundle=gmem1)
    HLS_PRAGMA(HLS INTERFACE m_axi port=bias offset=slave bundle=gmem2)
    HLS_PRAGMA(HLS INTERFACE m_axi port=out_fm offset=slave bundle=gmem3)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=H bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=W bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=shift bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=return bundle=ctrl)

    if (in_fm == 0 || wgt == 0 || bias == 0 || out_fm == 0) return ERR_NULL;
    // 分块后不再受 MAX_W 限制，只做防溢出的宽松检查。
    if (H <= 0 || W <= 0 || W > 65536 || H > 65536) return ERR_SIZE;
    if (shift < 1 || shift > 31) return ERR_SHIFT;

    // 权重整片装片上，和原版一致
    data_t wgt_buf[COUT][CIN][KSIZE][KSIZE];
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt_buf complete dim=3)
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt_buf complete dim=4)

    // 行缓冲：只装当前输出块需要的列
    data_t line_buf[CIN][SPAN][BWCOL];
    HLS_PRAGMA(HLS BIND_STORAGE variable=line_buf type=RAM_2P impl=BRAM)

    for (int oc = 0; oc < COUT; ++oc) {
        for (int ic = 0; ic < CIN; ++ic) {
            for (int ky = 0; ky < KSIZE; ++ky) {
                for (int kx = 0; kx < KSIZE; ++kx) {
                    wgt_buf[oc][ic][ky][kx] =
                        wgt[((oc * CIN + ic) * KSIZE + ky) * KSIZE + kx];
                }
            }
        }
    }

    const int H_out = CONV_OUT(H);
    const int W_out = CONV_OUT(W);

    for (int y = 0; y < H_out; ++y) {
        for (int x0 = 0; x0 < W_out; x0 += BLOCK_W) {
            const int xn = (W_out - x0 < BLOCK_W) ? (W_out - x0) : BLOCK_W;

            // 当前块的输入列范围是 [gc0, gc0 + BWCOL)，gc0 = x0*S - PAD。
            // 拆成三段：左补零 / 直拷 / 右补零。
            const int gc0 = x0 * STRIDE - PAD;
            int la = (gc0 < 0) ? -gc0 : 0;
            if (la > BWCOL) la = BWCOL;
            int lb = W - gc0 - la;
            if (lb < 0) lb = 0;
            if (lb > BWCOL - la) lb = BWCOL - la;

            for (int r = 0; r < SPAN; ++r) {
                const int iy = y * STRIDE + r - PAD;
                const bool row_ok = (iy >= 0) && (iy < H);
                for (int ic = 0; ic < CIN; ++ic) {
                    for (int j = 0; j < la; ++j) line_buf[ic][r][j] = (data_t)0;
                    if (row_ok) {
                        const int base = (ic * H + iy) * W + gc0 + la;
                        for (int j = 0; j < lb; ++j) {
                            line_buf[ic][r][la + j] = in_fm[base + j];
                        }
                    } else {
                        for (int j = 0; j < lb; ++j) {
                            line_buf[ic][r][la + j] = (data_t)0;
                        }
                    }
                    for (int j = la + lb; j < BWCOL; ++j) {
                        line_buf[ic][r][j] = (data_t)0;
                    }
                }
            }

            for (int x2 = 0; x2 < xn; ++x2) {
                for (int oc = 0; oc < COUT; ++oc) {
                    acc_t acc_even = 0;
                    acc_t acc_odd = 0;

                    for (int ic = 0; ic < CIN; ++ic) {
                        HLS_PRAGMA(HLS PIPELINE II=PIPELINE_II)
                        acc_t part = 0;
                        for (int ky = 0; ky < KSIZE; ++ky) {
                            HLS_PRAGMA(HLS UNROLL factor=UNROLL_KY)
                            for (int kx = 0; kx < KSIZE; ++kx) {
                                HLS_PRAGMA(HLS UNROLL factor=UNROLL_KX)
                                const int32_t p = (int32_t)line_buf[ic][ky * DILATION]
                                                              [x2 * STRIDE + kx * DILATION];
                                const int32_t k = (int32_t)wgt_buf[oc][ic][ky][kx];
                                part += p * k;
                            }
                        }
                        if (ic & 1) {
                            acc_odd += part;
                        } else {
                            acc_even += part;
                        }
                    }

                    const int32_t acc = (int32_t)acc_even + (int32_t)acc_odd;
                    int32_t v = (int32_t)acc + (int32_t)bias[oc];
                    v = v >> shift;
                    if (v < 0) v = 0;

                    out_fm[(oc * H_out + y) * W_out + x0 + x2] = saturate_int8(v);
                }
            }
        }
    }

    return ERR_OK;
}
