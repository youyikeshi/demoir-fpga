// conv_tile4.cpp —— v3.4：W1+W4（权重整块预装 BRAM + 循环重排）
//
// v3.2 的实测瓶颈（反推自 2.18M 拍）：**权重搬运**。每个权重字节只被 16 个像素
// 复用，而且是 36 字节的短 burst —— 每字节摊到 ~16 拍（和当初 conv 填充同一个病）。
// 一层的权重流量只有 221 KB，理论上 0.5 ms 就能搬完，实测却吃掉 3.5M 拍。
//
// 本版的改动
// ---------------------------------------------------------------------------
// W1 权重整块预装：把 (oc_block × cin_block × k²) 的权重一次性搬进 BRAM
//    （默认 8×32×9 = 2.3 KB，一次长 burst），之后每个权重字节被 TH×TW 个像素复用。
// W4 循环重排：tile → oc_block → cin_block，让输入 tile 与权重块各自只搬一次就位：
//
//     for tile:                    # TH×TW 个输出像素
//       for oc_block:              # 8 个输出通道
//         清累加器（寄存器）
//         for cin_block:           # 32 个输入通道
//           装输入 tile（连续段直拷，4.4 KB）
//           装权重块（一次 burst，2.3 KB）
//           计算：32 ic × 64 px × 8 oc × k²（抽头从寄存器取）
//         写回
//
// 代价：输入 tile 会被重装 (cout/OCB) 次。cout=16 时 2 次（可接受）；
//       cout=192 时 24 次 —— 这一版先用小 cout 验证方向，再决定要不要
//       加"多 oc 块共用一个输入 tile"的分层缓存。
#include "conv2d_accel.h"

#ifdef USE_HLS_TYPES
#include "ap_int.h"
#endif
typedef int32_t vt4_acc_t;

#ifndef VT4_TW
#define VT4_TW 32           // 输出 tile 宽
#endif
#ifndef VT4_TH
#define VT4_TH 2            // 输出 tile 高
#endif
#ifndef VT4_OCB
#define VT4_OCB 8           // 每个 oc 块的通道数
#endif
#ifndef VT4_CINB
#define VT4_CINB 32         // 每个 cin 块的通道数
#endif
#define VT4_CMAX 352
#define VT4_KMAX 5
#define VT4_SMAX 2
#define VT4_IH_MAX ((VT4_TH - 1) * VT4_SMAX + VT4_KMAX)
#define VT4_IW_MAX ((VT4_TW - 1) * VT4_SMAX + VT4_KMAX)

#ifndef VT4_DEPTH
#define VT4_DEPTH 65536
#endif

#ifndef USE_HLS_TYPES
static inline data_t vt4_sat8(int v) { return (data_t)(v > 127 ? 127 : (v < -128 ? -128 : v)); }
#else
static inline data_t vt4_sat8(int v) {
    if (v > 127) v = 127;
    if (v < -128) v = -128;
    return (data_t)v;
}
#endif

int conv_tile4(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
               data_t *out_fm, int H, int W, int Hout, int Wout,
               int cin, int cout, int ksize, int stride, int dil,
               int pad, int shift, int relu_en) {
#ifdef USE_HLS_TYPES
#pragma HLS INTERFACE m_axi port=in_fm offset=slave bundle=gmem0 depth=VT4_DEPTH
#pragma HLS INTERFACE m_axi port=wgt   offset=slave bundle=gmem0 depth=VT4_DEPTH
#pragma HLS INTERFACE m_axi port=bias  offset=slave bundle=gmem0 depth=1024
#pragma HLS INTERFACE m_axi port=out_fm offset=slave bundle=gmem1 depth=VT4_DEPTH
#pragma HLS INTERFACE s_axilite port=H bundle=ctrl
#pragma HLS INTERFACE s_axilite port=W bundle=ctrl
#pragma HLS INTERFACE s_axilite port=Hout bundle=ctrl
#pragma HLS INTERFACE s_axilite port=Wout bundle=ctrl
#pragma HLS INTERFACE s_axilite port=cin bundle=ctrl
#pragma HLS INTERFACE s_axilite port=cout bundle=ctrl
#pragma HLS INTERFACE s_axilite port=ksize bundle=ctrl
#pragma HLS INTERFACE s_axilite port=stride bundle=ctrl
#pragma HLS INTERFACE s_axilite port=dil bundle=ctrl
#pragma HLS INTERFACE s_axilite port=pad bundle=ctrl
#pragma HLS INTERFACE s_axilite port=shift bundle=ctrl
#pragma HLS INTERFACE s_axilite port=relu_en bundle=ctrl
#pragma HLS INTERFACE s_axilite port=return bundle=ctrl
#endif

    if (cin <= 0 || cout <= 0 || H <= 0 || W <= 0) return ERR_SIZE;
    if (cin > VT4_CMAX) return ERR_SIZE;
    if (ksize != 1 && ksize != 3) return ERR_SIZE;
    if (stride > VT4_SMAX || dil != 1) return ERR_SIZE;
    if (in_fm == 0 || out_fm == 0 || wgt == 0) return ERR_NULL;

    // 输入 tile（BRAM）：cin_block 的整块输入
    data_t itile[VT4_CINB][VT4_IH_MAX][VT4_IW_MAX];
#ifdef USE_HLS_TYPES
#pragma HLS ARRAY_PARTITION variable=itile cyclic factor=4 dim=1
#endif
    // 权重块（BRAM）：oc_block × cin_block × k²
    data_t wblk[VT4_OCB][VT4_CINB][VT4_KMAX][VT4_KMAX];
#ifdef USE_HLS_TYPES
#pragma HLS ARRAY_PARTITION variable=wblk cyclic factor=4 dim=1
#pragma HLS ARRAY_PARTITION variable=wblk cyclic factor=4 dim=2
#endif
    // 累加器（寄存器）：oc_block × TH × TW = 8×2×32 = 512 个 int32
    vt4_acc_t acc[VT4_OCB][VT4_TH][VT4_TW];
#ifdef USE_HLS_TYPES
#pragma HLS ARRAY_PARTITION variable=acc complete dim=1
#endif

    const int kk = ksize * ksize;

    for (int ty = 0; ty < Hout; ty += VT4_TH) {
        for (int tx = 0; tx < Wout; tx += VT4_TW) {
            const int th = (Hout - ty < VT4_TH) ? (Hout - ty) : VT4_TH;
            const int tw = (Wout - tx < VT4_TW) ? (Wout - tx) : VT4_TW;
            const int iy0 = ty * stride - pad;
            const int ix0 = tx * stride - pad;
            const int ih = (th - 1) * stride + ksize;      // 本 tile 实际需要的输入行数
            const int iw = (tw - 1) * stride + ksize;      // 与列数

            // 列方向三段：左补零 / 连续直拷 / 右补零
            int lz = (ix0 < 0) ? -ix0 : 0;
            if (lz > iw) lz = iw;
            int cnt = W - (ix0 + lz);
            if (cnt < 0) cnt = 0;
            if (cnt > iw - lz) cnt = iw - lz;

            for (int ocb = 0; ocb < cout; ocb += VT4_OCB) {
                const int nob = (cout - ocb < VT4_OCB) ? (cout - ocb) : VT4_OCB;

                // 清累加器
                for (int o = 0; o < VT4_OCB; ++o)
                    for (int a = 0; a < VT4_TH; ++a)
                        for (int b = 0; b < VT4_TW; ++b)
                            acc[o][a][b] = 0;

                for (int cinb = 0; cinb < cin; cinb += VT4_CINB) {
                    const int ncb = (cin - cinb < VT4_CINB) ? (cin - cinb) : VT4_CINB;

                    // ── 装输入 tile（cin_block × ih × iw）─────────────────────
                    for (int ic = 0; ic < ncb; ++ic) {
                        for (int iy = 0; iy < ih; ++iy) {
                            const int sy = iy0 + iy;
                            const bool rok = (sy >= 0) && (sy < H);
                            for (int ix = 0; ix < lz; ++ix) itile[ic][iy][ix] = (data_t)0;
                            if (rok) {
                                const int io = (cinb + ic) * H * W + sy * W + ix0 + lz;
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                                for (int ix = 0; ix < cnt; ++ix) {
                                    itile[ic][iy][lz + ix] = in_fm[io + ix];
                                }
                            } else {
                                for (int ix = 0; ix < cnt; ++ix) {
                                    itile[ic][iy][lz + ix] = (data_t)0;
                                }
                            }
                            for (int ix = lz + cnt; ix < iw; ++ix) {
                                itile[ic][iy][ix] = (data_t)0;
                            }
                        }
                    }

                    // ── 装权重块（一整块连续读，一次 burst）─────────────────
                    // 布局：wblk[o][ic][ky][kx] ← wgt[(ocb+o)*cin*kk + (cinb+ic)*kk + ky*ksize + kx]
                    for (int o = 0; o < VT4_OCB; ++o) {
                        const bool ok_o = (o < nob);
                        for (int ic = 0; ic < ncb; ++ic) {
                            const int base = (ocb + o) * cin * kk + (cinb + ic) * kk;
                            if (ok_o) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                                for (int ky = 0; ky < ksize; ++ky) {
                                    for (int kx = 0; kx < ksize; ++kx) {
                                        wblk[o][ic][ky][kx] = wgt[base + ky * ksize + kx];
                                    }
                                }
                            } else {
                                for (int ky = 0; ky < VT4_KMAX; ++ky)
                                    for (int kx = 0; kx < VT4_KMAX; ++kx)
                                        wblk[o][ic][ky][kx] = (data_t)0;
                            }
                        }
                    }

                    // ── 计算：ncb × (TH×TW) × nob × k² ───────────────────────
                    for (int ic = 0; ic < ncb; ++ic) {
                        for (int a = 0; a < VT4_TH; ++a) {
                            for (int b = 0; b < VT4_TW; ++b) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                                for (int o = 0; o < VT4_OCB; ++o) {
#ifdef USE_HLS_TYPES
#pragma HLS UNROLL
#endif
                                    vt4_acc_t s = 0;
                                    if (ksize == 1) {
                                        s = (vt4_acc_t)itile[ic][a * stride][b * stride]
                                          * (vt4_acc_t)wblk[o][ic][0][0];
                                    } else {
#ifdef USE_HLS_TYPES
#pragma HLS UNROLL
#endif
                                        for (int ky = 0; ky < 3; ++ky) {
#ifdef USE_HLS_TYPES
#pragma HLS UNROLL
#endif
                                            for (int kx = 0; kx < 3; ++kx) {
                                                s += (vt4_acc_t)itile[ic][a * stride + ky]
                                                                  [b * stride + kx]
                                                   * (vt4_acc_t)wblk[o][ic][ky][kx];
                                            }
                                        }
                                    }
                                    acc[o][a][b] += s;
                                }
                            }
                        }
                    }
                }

                // ── 写回 ───────────────────────────────────────────────────
                for (int o = 0; o < nob; ++o) {
                    const int32_t obase = (ocb + o) * Hout * Wout;
                    for (int a = 0; a < th; ++a) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                        for (int b = 0; b < tw; ++b) {
                            int32_t v = acc[o][a][b] + (int32_t)bias[ocb + o];
                            v = v >> shift;
                            if (relu_en != 0 && v < 0) v = 0;
                            out_fm[obase + (ty + a) * Wout + tx + b] = vt4_sat8(v);
                        }
                    }
                }
            }
        }
    }
    return ERR_OK;
}
