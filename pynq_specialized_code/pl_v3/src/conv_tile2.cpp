// conv_tile2.cpp —— v3.1：分块 + **移位寄存器行缓冲**（复用到位、抽头下标全常量）
//
// 和 v3（conv_tile.cpp）的差别
// ---------------------------------------------------------------------------
// v3 ：tile 放在 `complete dim=3` 分区的寄存器数组里，读 itile[ic][a+ky][b+kx] 时
//      ky/kx/a/b 都是运行期值 → 变成 35 选 1 的 mux，II 被拖到 ~11，
//      实测只有 2.81 MAC/拍（现引擎 8）。
// v3.1：**一行读进寄存器，横向抽头取相邻寄存器字节**。对每个 (ic, ky)：
//        1) 把这一行 (TW + K - 1) 个字节从 BRAM 读进 reg[NROW][ROWMAX]；
//        2) 之后 8 个输出通道 × TW 个像素 × 3 个 kx 全部从寄存器取（线级选通）；
//      ⇒ 每字节只读一次、被 k×OCG×TH 次复用，抽头下标是常量，II=1。
//
// 规模（默认 TW=16、TH=2、OCG=8、cin≤352、k≤5）
//   BRAM: itile[cin][TH+K-1][TW+K-1] = 352×6×20 = 42 KB  → ~21 BRAM18K
//   寄存器: reg 只放 3 行 × 20 字节 = 60 B
//   累加器: OCG×TH×TW = 8×2×16 = 256 个 26 位
//   乘法器: OCG×TW = 128（II=1 时）→ 可调 OCG 或 TW 控制
#include "conv2d_accel.h"

#ifdef USE_HLS_TYPES
#include "ap_int.h"
typedef ap_int<8>  data_t;
typedef ap_int<32> bias_t;
typedef ap_int<26> acc_t;
#else
#include <stdint.h>
typedef int8_t  data_t;
typedef int32_t bias_t;
typedef int32_t acc_t;
#endif

// ── 参数（先用一组保守值把结构跑通，再调）─────────────────────────────────
#ifndef VT2_TW
#define VT2_TW 8            // 输出 tile 宽（决定横向复用与乘法器数）
#endif
#ifndef VT2_TH
#define VT2_TH 2            // 输出 tile 高
#endif
#ifndef VT2_OCG
#define VT2_OCG 4           // 每拍并行的输出通道数（× TW 得乘法器数）
#endif
#define VT2_CMAX 352
#define VT2_KMAX 5
#define VT2_SMAX 2

static const int VT2_IH = (VT2_TH - 1) * VT2_SMAX + VT2_KMAX;   // 输入 tile 行数
static const int VT2_IW = (VT2_TW - 1) * VT2_SMAX + VT2_KMAX;   // 输入 tile 列数

#ifndef VT2_DEPTH
#define VT2_DEPTH 65536
#endif

#ifndef USE_HLS_TYPES
static inline data_t vt2_sat8(int v) {
    return (data_t)(v > 127 ? 127 : (v < -128 ? -128 : v));
}
#else
static inline data_t vt2_sat8(int v) {
    if (v > 127) v = 127;
    if (v < -128) v = -128;
    return (data_t)v;
}
#endif

int conv_tile2(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
               data_t *out_fm, int H, int W, int Hout, int Wout,
               int cin, int cout, int ksize, int stride, int dil,
               int pad, int shift, int relu_en) {
#ifdef USE_HLS_TYPES
#pragma HLS INTERFACE m_axi port=in_fm offset=slave bundle=gmem0 depth=VT2_DEPTH
#pragma HLS INTERFACE m_axi port=wgt   offset=slave bundle=gmem0 depth=VT2_DEPTH
#pragma HLS INTERFACE m_axi port=bias  offset=slave bundle=gmem0 depth=1024
#pragma HLS INTERFACE m_axi port=out_fm offset=slave bundle=gmem1 depth=VT2_DEPTH
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
    if (cin > VT2_CMAX) return ERR_SIZE;
    if (ksize != 1 && ksize != 3) return ERR_SIZE;   // 原型先支持 k=1/3（网络占 246/247 层）
    if (stride > VT2_SMAX || dil != 1) return ERR_SIZE;
    if (in_fm == 0 || out_fm == 0 || wgt == 0) return ERR_NULL;

    // 输入 tile（BRAM；不做 complete 分区，读靠 bank 端口）
    data_t itile[VT2_CMAX][VT2_IH][VT2_IW];
#ifdef USE_HLS_TYPES
#pragma HLS ARRAY_PARTITION variable=itile cyclic factor=4 dim=1   // 4 路并行读
#endif
    // 移位寄存器行缓冲：**必须装满 VT2_IH 行**（计算要读 a*stride+ky，
    // a 最大 TH-1 → 最多 (TH-1)*stride + K - 1 行；只装 ksize 行会读到脏数据）
    data_t reg[VT2_IH][VT2_IW];
#ifdef USE_HLS_TYPES
#pragma HLS ARRAY_PARTITION variable=reg complete dim=0
#endif
    // 累加器：OCG × TH × TW
    acc_t acc[VT2_OCG][VT2_TH][VT2_TW];
#ifdef USE_HLS_TYPES
#pragma HLS ARRAY_PARTITION variable=acc complete dim=0
#endif
    // 一个 oc 组的权重：抽头下标常量后可直接索引
    data_t wk[VT2_OCG][VT2_KMAX][VT2_KMAX];
#ifdef USE_HLS_TYPES
#pragma HLS ARRAY_PARTITION variable=wk complete dim=0
#endif

    const int kk = ksize * ksize;

    for (int ty = 0; ty < Hout; ty += VT2_TH) {
        for (int tx = 0; tx < Wout; tx += VT2_TW) {
            const int th = (Hout - ty < VT2_TH) ? (Hout - ty) : VT2_TH;
            const int tw = (Wout - tx < VT2_TW) ? (Wout - tx) : VT2_TW;

            // ── 装输入 tile（每个 tile 一次；左补零/连续直拷/右补零三段）──────
            {
                const int iy0 = ty * stride - pad;
                const int ix0 = tx * stride - pad;
                int lz = (ix0 < 0) ? -ix0 : 0;
                if (lz > VT2_IW) lz = VT2_IW;
                int cnt = W - (ix0 + lz);
                if (cnt < 0) cnt = 0;
                if (cnt > VT2_IW - lz) cnt = VT2_IW - lz;
                for (int ic = 0; ic < cin; ++ic) {
                    for (int iy = 0; iy < VT2_IH; ++iy) {
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
                        for (int ix = lz + cnt; ix < VT2_IW; ++ix) {
                            itile[ic][iy][ix] = (data_t)0;
                        }
                    }
                }
            }

            for (int ocg = 0; ocg < cout; ocg += VT2_OCG) {
                const int ocb = (cout - ocg < VT2_OCG) ? (cout - ocg) : VT2_OCG;

                for (int o = 0; o < VT2_OCG; ++o)
                    for (int a = 0; a < VT2_TH; ++a)
                        for (int b = 0; b < VT2_TW; ++b)
                            acc[o][a][b] = 0;

                for (int ic = 0; ic < cin; ++ic) {
                    // 权重：一个 oc 组的 k×k（下标常量，循环按 ksize 展开）
                    for (int o = 0; o < VT2_OCG; ++o) {
                        const bool ok_o = (o < ocb);
                        const int base = (ocg + o) * cin * kk + ic * kk;
                        for (int ky = 0; ky < VT2_KMAX; ++ky)
                            for (int kx = 0; kx < VT2_KMAX; ++kx)
                                wk[o][ky][kx] = (data_t)0;
                        if (ok_o) {
                            for (int ky = 0; ky < ksize; ++ky)
                                for (int kx = 0; kx < ksize; ++kx)
                                    wk[o][ky][kx] = wgt[base + ky * ksize + kx];
                        }
                    }

                    // ★ 核心：把 ksize 条输入行搬进**移位寄存器**，之后横向抽头
                    //   全部从寄存器取（下标常量 → 无 mux）。
                    for (int ky = 0; ky < VT2_IH; ++ky) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                        for (int ix = 0; ix < VT2_IW; ++ix) {
                            reg[ky][ix] = itile[ic][ky][ix];
                        }
                    }

                    // 计算：**像素维流水（II=1）+ 通道维展开**。
                    // 每拍同时算 OCG 个通道 × k² 个抽头的乘加，抽头从寄存器取
                    // （下标常量）。乘法器数 = OCG × k²（默认 4×9 = 36）。
                    for (int a = 0; a < VT2_TH; ++a) {
                        for (int b = 0; b < VT2_TW; ++b) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                            for (int o = 0; o < VT2_OCG; ++o) {
#ifdef USE_HLS_TYPES
#pragma HLS UNROLL
#endif
                                int32_t s = 0;
                                if (ksize == 1) {
                                    s = (int)reg[a * stride][b * stride] * (int)wk[o][0][0];
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
                                               * (int)wk[o][ky][kx];
                                        }
                                    }
                                }
                                acc[o][a][b] += (acc_t)s;
                            }
                        }
                    }
                }

                // 写回
                for (int o = 0; o < ocb; ++o) {
                    const int32_t obase = (ocg + o) * Hout * Wout;
                    for (int a = 0; a < th; ++a) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                        for (int b = 0; b < tw; ++b) {
                            int32_t v = (int32_t)acc[o][a][b] + (int32_t)bias[ocg + o];
                            v = v >> shift;
                            if (relu_en != 0 && v < 0) v = 0;
                            out_fm[obase + (ty + a) * Wout + tx + b] = vt2_sat8(v);
                        }
                    }
                }
            }
        }
    }
    return ERR_OK;
}
