// conv_tile3.cpp —— v3.2：行缓冲 + 滑窗寄存器 + **累加器驻 BRAM**
//
// 演进（每一步都是"把上一次的装载分母做大"）
// ---------------------------------------------------------------------------
// v3  ：tile 放分区寄存器数组 → 抽头下标变运行期 → 35 选 1 mux，II≈11，2.81 MAC/拍
// v3.1：一行读进移位寄存器 → 下标常量化、II=1，但**每 (ic, ocg) 装一次**：
//       装载 127 字节 vs 计算 16 拍 → 装载压过计算，1.6 MAC/拍
// v3.2：**累加器搬到 BRAM**（cout × TH × TW × int32 = 12 KB），于是 oc 循环整个
//       塞进 ic 里面：装载只按 (tile, ic) 付一次，分母从"8 个像素 × 4 通道"变成
//       "整个 cout"。权重按 4 通道一块滚动（每块 36 字节，被 TH×TW×k² 次复用）。
//
// 账（cin=96 cout=192 TH=2 TW=8 k=3，单 tile）
//   装载: reg 91 字节 + 48 个 oc 块 × 36 字节 ≈ 1,820 拍（每 ic）
//   计算: 192 oc × 2×8 像素 × 9 抽头 = 27,648 MAC（每 ic）
//   ⇒ 约 15~20 MAC/拍（现引擎 8），且 BRAM 只需 ~70 块（现引擎 163）
#include "conv2d_accel.h"

#ifdef USE_HLS_TYPES
#include "ap_int.h"
typedef ap_int<8>  data_t;
typedef ap_int<32> bias_t;
#endif
typedef int32_t vt3_acc_t;      // 累加器在 BRAM：用 int32（acc_t 已被 conv2d_accel.h 定义成 ap_int<26>）

#ifndef VT3_TW
#define VT3_TW 8            // 输出 tile 宽
#endif
#ifndef VT3_TH
#define VT3_TH 2            // 输出 tile 高
#endif
#ifndef VT3_OCB
#define VT3_OCB 4           // 权重滚动块大小（每拍并行几个 oc）
#endif
#define VT3_CMAX 352
#define VT3_COMAX 512
#define VT3_KMAX 5
#define VT3_SMAX 2

static const int VT3_IH = (VT3_TH - 1) * VT3_SMAX + VT3_KMAX;
static const int VT3_IW = (VT3_TW - 1) * VT3_SMAX + VT3_KMAX;
static const int VT3_PX = VT3_TH * VT3_TW;

#ifndef VT3_DEPTH
#define VT3_DEPTH 65536
#endif

#ifndef USE_HLS_TYPES
static inline data_t vt3_sat8(int v) { return (data_t)(v > 127 ? 127 : (v < -128 ? -128 : v)); }
#else
static inline data_t vt3_sat8(int v) {
    if (v > 127) v = 127;
    if (v < -128) v = -128;
    return (data_t)v;
}
#endif

int conv_tile3(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
               data_t *out_fm, int H, int W, int Hout, int Wout,
               int cin, int cout, int ksize, int stride, int dil,
               int pad, int shift, int relu_en) {
#ifdef USE_HLS_TYPES
#pragma HLS INTERFACE m_axi port=in_fm offset=slave bundle=gmem0 depth=VT3_DEPTH
#pragma HLS INTERFACE m_axi port=wgt   offset=slave bundle=gmem0 depth=VT3_DEPTH
#pragma HLS INTERFACE m_axi port=bias  offset=slave bundle=gmem0 depth=1024
#pragma HLS INTERFACE m_axi port=out_fm offset=slave bundle=gmem1 depth=VT3_DEPTH
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
    if (cin > VT3_CMAX || cout > VT3_COMAX) return ERR_SIZE;
    if (ksize != 1 && ksize != 3) return ERR_SIZE;
    if (stride > VT3_SMAX || dil != 1) return ERR_SIZE;
    if (in_fm == 0 || out_fm == 0 || wgt == 0) return ERR_NULL;

    // 输入 tile（BRAM）
    data_t itile[VT3_CMAX][VT3_IH][VT3_IW];
#ifdef USE_HLS_TYPES
#pragma HLS ARRAY_PARTITION variable=itile cyclic factor=4 dim=1
#endif
    // 滑窗寄存器：一个 (ic, 输入行) 的整行
    data_t reg[VT3_IH][VT3_IW];
#ifdef USE_HLS_TYPES
#pragma HLS ARRAY_PARTITION variable=reg complete dim=0
#endif
    // 权重滚动块
    data_t wk[VT3_OCB][VT3_KMAX][VT3_KMAX];
#ifdef USE_HLS_TYPES
#pragma HLS ARRAY_PARTITION variable=wk complete dim=0
#endif
    // ★ 累加器驻 BRAM：整个 oc 范围 × tile 像素。这是 v3.2 的关键——
    //   有了它，oc 循环才能整个放进 ic 里面，装载只按 (tile, ic) 付一次。
    vt3_acc_t accb[VT3_COMAX][VT3_PX];
#ifdef USE_HLS_TYPES
#pragma HLS ARRAY_PARTITION variable=accb cyclic factor=4 dim=1
#endif

    const int kk = ksize * ksize;

    for (int ty = 0; ty < Hout; ty += VT3_TH) {
        for (int tx = 0; tx < Wout; tx += VT3_TW) {
            const int th = (Hout - ty < VT3_TH) ? (Hout - ty) : VT3_TH;
            const int tw = (Wout - tx < VT3_TW) ? (Wout - tx) : VT3_TW;

            // ── 装输入 tile（每个 tile 一次）────────────────────────────────
            {
                const int iy0 = ty * stride - pad;
                const int ix0 = tx * stride - pad;
                int lz = (ix0 < 0) ? -ix0 : 0;
                if (lz > VT3_IW) lz = VT3_IW;
                int cnt = W - (ix0 + lz);
                if (cnt < 0) cnt = 0;
                if (cnt > VT3_IW - lz) cnt = VT3_IW - lz;
                for (int ic = 0; ic < cin; ++ic) {
                    for (int iy = 0; iy < VT3_IH; ++iy) {
                        const int sy = iy0 + iy;
                        const bool rok = (sy >= 0) && (sy < H);
                        for (int ix = 0; ix < lz; ++ix) itile[ic][iy][ix] = (data_t)0;
                        if (rok) {
                            const int io = ic * H * W + sy * W + ix0 + lz;
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
                        for (int ix = lz + cnt; ix < VT3_IW; ++ix) {
                            itile[ic][iy][ix] = (data_t)0;
                        }
                    }
                }
            }

            // ── 清累加器 ───────────────────────────────────────────────────
            for (int oc = 0; oc < cout; ++oc)
                for (int p = 0; p < VT3_PX; ++p)
                    accb[oc][p] = 0;

            // ── 主循环：ic 外层 → 装载只付一次；oc 块内层滚动权重 ──────────
            for (int ic = 0; ic < cin; ++ic) {
                // 滑窗寄存器：把整块输入行装进来（每 (tile, ic) 一次）
                for (int ky = 0; ky < VT3_IH; ++ky) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                    for (int ix = 0; ix < VT3_IW; ++ix) {
                        reg[ky][ix] = itile[ic][ky][ix];
                    }
                }
                for (int ocb = 0; ocb < cout; ocb += VT3_OCB) {
                    const int nob = (cout - ocb < VT3_OCB) ? (cout - ocb) : VT3_OCB;
                    // 权重块：nob × k²，一次连续读（下标常量）
                    for (int j = 0; j < VT3_OCB; ++j) {
                        const bool ok_j = (j < nob);
                        const int base = (ocb + j) * cin * kk + ic * kk;
                        for (int ky = 0; ky < VT3_KMAX; ++ky)
                            for (int kx = 0; kx < VT3_KMAX; ++kx)
                                wk[j][ky][kx] = (data_t)0;
                        if (ok_j) {
                            for (int ky = 0; ky < ksize; ++ky)
                                for (int kx = 0; kx < ksize; ++kx)
                                    wk[j][ky][kx] = wgt[base + ky * ksize + kx];
                        }
                    }
                    // 计算：TH×TW 个像素 × OCB 个通道，抽头全从寄存器取
                    for (int a = 0; a < VT3_TH; ++a) {
                        for (int b = 0; b < VT3_TW; ++b) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                            const int p = a * VT3_TW + b;
                            for (int j = 0; j < VT3_OCB; ++j) {
#ifdef USE_HLS_TYPES
#pragma HLS UNROLL
#endif
                                int32_t s = 0;
                                if (ksize == 1) {
                                    s = (int)reg[a * stride][b * stride] * (int)wk[j][0][0];
                                } else {
#ifdef USE_HLS_TYPES
#pragma HLS UNROLL
#endif
                                    for (int ky = 0; ky < 3; ++ky) {
#ifdef USE_HLS_TYPES
#pragma HLS UNROLL
#endif
                                        for (int kx = 0; kx < 3; ++kx) {
                                            s += (int)reg[a * stride + ky][b * stride + kx]
                                               * (int)wk[j][ky][kx];
                                        }
                                    }
                                }
                                accb[ocb + j][p] += s;
                            }
                        }
                    }
                }
            }

            // ── 写回 ───────────────────────────────────────────────────────
            for (int oc = 0; oc < cout; ++oc) {
                const int32_t obase = oc * Hout * Wout;
                for (int a = 0; a < th; ++a) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                    for (int b = 0; b < tw; ++b) {
                        int32_t v = accb[oc][a * VT3_TW + b] + (int32_t)bias[oc];
                        v = v >> shift;
                        if (relu_en != 0 && v < 0) v = 0;
                        out_fm[obase + (ty + a) * Wout + tx + b] = vt3_sat8(v);
                    }
                }
            }
        }
    }
    return ERR_OK;
}
