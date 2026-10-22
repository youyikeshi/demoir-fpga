// conv_tile.cpp —— v3 原型：分块数据流（tile-based）卷积
//
// 与现有 conv2d_engine 的根本差别
// ---------------------------------------------------------------------------
// 现引擎：逐行流过 + 行缓冲复用 → 每个输入字节被复用 span 次（k=3 时 3 次），
//         所以填充要搬 0.4 GB/图，占 conv 的 24%。
// v3    ：一个输出 tile（TH×TW）所需的输入 tile（(TH+K-1)×(TW+K-1)）**整体常驻
//         BRAM**，每个输入字节只从 DDR 搬一次，然后被 TH×TW×OCG 次复用。
//         填充量降到约 1/(TH·TW) —— 这是"少搬"的终极形态。
//
// 资源（cin=352、TH=TW=4、K=3 时）
//   itile : 352 × 6 × 6 = 12.7 KB  → ~7 BRAM18K
//   acc   : OCG × TH × TW = 8×16 = 128 个 26 位寄存器（4 k FF）
//   复用倍数：每个输入字节被 8(oc) × 9(taps) = 72 次乘加用到
//
// 本文件是**原型**：先把正确性和结构钉住（对着 conv_ref_rt 逐位对拍），
// 再决定 tile 参数与并行度。它还没有加满流水 pragma。
#include "conv2d_accel.h"   // 复用现引擎的类型定义（-I ../pl/src）

#ifdef USE_HLS_TYPES
#include "ap_int.h"
typedef ap_int<8>  data_t;
typedef ap_int<32> bias_t;
typedef ap_int<26> acc_t;
#else
#include <stdint.h>
typedef int8_t  data_t;
typedef int32_t bias_t;
typedef int32_t acc_t;      // 主机侧用 32 位（范围足够）
#endif

// ── tile 参数（编译期常量；先用保守值把结构跑通）───────────────────────────
#ifndef VT_TH
#define VT_TH 2            // 输出 tile 高
#endif
#ifndef VT_TW
#define VT_TW 16            // 输出 tile 宽
#endif
#ifndef VT_OCG
#define VT_OCG 8           // 每个 pass 处理多少输出通道
#endif
// cosim 需要的 depth 上界（AXI 模型会按它算地址空间；给大了 TB 缓冲跟不上会 SIGSEGV）。
// 4096 足够覆盖下面 quick 档的所有形状。
#ifndef VT_DEPTH
#define VT_DEPTH 65536
#endif
#ifndef VT_BIAS_DEPTH
#define VT_BIAS_DEPTH 1024
#endif

#ifndef VT_PXP
#define VT_PXP 4           // 像素并行度：每拍同时算多少个输出像素（1..VT_TH*VT_TW）
#endif
// 每拍乘法器数 = VT_PXP × VT_OCG（默认 4×8 = 32）。
// 这是整个 v3 的核心调节旋钮：
//   · 现引擎只有 8 MAC/拍（每拍一条行缓冲读 → 没活干）；
//   · 这里 tile 常驻 BRAM，同一批输入被 VT_PXP×VT_OCG 个乘加同时用，32 个乘法器
//     能一直满转 → 32 MAC/拍（4×），而乘法器只要 32 个。
static_assert((VT_TH * VT_TW) % VT_PXP == 0, "VT_PXP 必须整除 tile 像素数");
#define VT_PX (VT_TH * VT_TW)

#define VT_CIN_MAX 352
#define VT_KMAX    5
// 输入 tile 尺寸必须覆盖 stride（原型支持 s≤2、d=1）：最远的抽头在
// (VT_TH-1)*stride + (K-1) 行。
#define VT_SMAX    2
static const int VT_IH = (VT_TH - 1) * VT_SMAX + VT_KMAX;
static const int VT_IW = (VT_TW - 1) * VT_SMAX + VT_KMAX;

#ifndef USE_HLS_TYPES
#include <string.h>
static inline int sat8(int v) { return v > 127 ? 127 : (v < -128 ? -128 : v); }
#else
static inline data_t sat8(int v) {
    if (v > 127) v = 127;
    if (v < -128) v = -128;
    return (data_t)v;
}
#endif

// 卷积主入口（签名与现引擎一致，便于直接替换对比）
int conv_tile(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
              data_t *out_fm, int H, int W, int Hout, int Wout,
              int cin, int cout, int ksize, int stride, int dil,
              int pad, int shift, int relu_en) {
#ifdef USE_HLS_TYPES
#pragma HLS INTERFACE m_axi port=in_fm offset=slave bundle=gmem0 depth=VT_DEPTH
#pragma HLS INTERFACE m_axi port=wgt   offset=slave bundle=gmem0 depth=VT_DEPTH
#pragma HLS INTERFACE m_axi port=bias  offset=slave bundle=gmem0 depth=VT_BIAS_DEPTH
#pragma HLS INTERFACE m_axi port=out_fm offset=slave bundle=gmem1 depth=VT_DEPTH
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
    if (cin > VT_CIN_MAX) return ERR_SIZE;
    if (stride > VT_SMAX) return ERR_SIZE;      // 原型只支持 s≤2
    if (dil < 1 || dil > 3) return ERR_SIZE;    // 网络里 d∈{1,2,3}
    if (ksize != 1 && ksize != 3 && ksize != 5) return ERR_SIZE;
    if (in_fm == 0 || out_fm == 0 || wgt == 0) return ERR_NULL;

    // 输入 tile 缓冲：一个 tile 的全部输入通道常驻（dil 先只支持 1，与原引擎一致的限制）
    data_t itile[VT_CIN_MAX][VT_IH][VT_IW];
#ifdef USE_HLS_TYPES
#pragma HLS ARRAY_PARTITION variable=itile complete dim=3   // 列方向展开，便于抽头并行
#endif
    // 累加器：OCG × TH × TW 全部在寄存器（完全展开）
    acc_t acc[VT_OCG][VT_TH][VT_TW];
#ifdef USE_HLS_TYPES
#pragma HLS ARRAY_PARTITION variable=acc complete dim=0
#endif

    const int kk = ksize * ksize;

    for (int ty = 0; ty < Hout; ty += VT_TH) {
        for (int tx = 0; tx < Wout; tx += VT_TW) {
            const int th = (Hout - ty < VT_TH) ? (Hout - ty) : VT_TH;
            const int tw = (Wout - tx < VT_TW) ? (Wout - tx) : VT_TW;

            // ── 装输入 tile：所有输入通道一次性进 BRAM（**每个 tile 只装一次**）──
            // 必须放在 ocg 循环之外：否则 cout=192（24 个 oc 组）会把同一块输入
            // 重复从 DDR 搬 24 遍，比现在还差。
            {
                const int iy0 = ty * stride - pad;
                const int ix0 = tx * stride - pad;
                // 列方向按"左补零 / 连续直拷 / 右补零"三段拆开。
                // 为什么：原来逐字节走 `srow<0||sx>=W ? 0 : in_fm[...]` 的谓词，
                // 把 AXI burst 全废了（cosim 实测 v3 比现引擎慢 6.2×，全慢在这里）。
                // 中间段无谓词 → HLS 能并成整拍传输。
                if (dil == 1) {
                    // ── 快路径：连续直拷（左补零 / 中间无谓词 / 右补零）──────
                    int lz = (ix0 < 0) ? -ix0 : 0;
                    if (lz > VT_IW) lz = VT_IW;
                    int cnt = W - (ix0 + lz);
                    if (cnt < 0) cnt = 0;
                    if (cnt > VT_IW - lz) cnt = VT_IW - lz;
                    for (int ic = 0; ic < cin; ++ic) {
                        for (int iy = 0; iy < VT_IH; ++iy) {
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
                            for (int ix = lz + cnt; ix < VT_IW; ++ix) itile[ic][iy][ix] = (data_t)0;
                        }
                    }
                } else {
                    // ── 膨胀采样：按 dil 逐点 gather，把膨胀感受野"抓"成连续 tile，
                    //    之后计算侧照 dil=1 的形态跑（下标仍是常量）。
                    //    网络里只有 60/247 层是 d>1，逐点采样慢一点可接受。
                    for (int ic = 0; ic < cin; ++ic) {
                        for (int iy = 0; iy < VT_IH; ++iy) {
                            const int sy = iy0 + iy * dil;
                            const bool rok = (sy >= 0) && (sy < H);
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                            for (int ix = 0; ix < VT_IW; ++ix) {
                                const int sx = ix0 + ix * dil;
                                itile[ic][iy][ix] =
                                    (rok && sx >= 0 && sx < W)
                                        ? in_fm[ic * H * W + sy * W + sx] : (data_t)0;
                            }
                        }
                    }
                }
            }

            for (int ocg = 0; ocg < cout; ocg += VT_OCG) {
                const int ocb = (cout - ocg < VT_OCG) ? (cout - ocg) : VT_OCG;

                // ── 清零累加器 ─────────────────────────────────────────────
                for (int o = 0; o < VT_OCG; ++o)
                    for (int a = 0; a < VT_TH; ++a)
                        for (int b = 0; b < VT_TW; ++b)
                            acc[o][a][b] = 0;

                // ── 主循环：通道在外，tile 内全复用 ────────────────────────
                for (int ic = 0; ic < cin; ++ic) {
                    // 2) 权重：一个 oc 组的 k×k 抽头
                    data_t wk[VT_OCG][VT_KMAX][VT_KMAX];
#ifdef USE_HLS_TYPES
#pragma HLS ARRAY_PARTITION variable=wk complete dim=0
#endif
                    // 权重装载：按 (o, ky, kx) 三维展开但**没有谓词**——
                    // 越界的 o（末尾不足 VT_OCG 个通道）先整体清零，然后只对
                    // o < ocb 的做连续读。原来逐元素带谓词会把读打散成单字节事务。
                    {
                        for (int o = 0; o < VT_OCG; ++o) {
                            const bool ok_o = (o < ocb);
                            const int base = (ocg + o) * cin * kk + ic * kk;
                            for (int ky = 0; ky < VT_KMAX; ++ky) {
                                for (int kx = 0; kx < VT_KMAX; ++kx) {
                                    wk[o][ky][kx] = (data_t)0;
                                }
                            }
                            if (ok_o) {
                                HLS_PRAGMA(HLS PIPELINE II=1)
                                for (int ky = 0; ky < ksize; ++ky) {
                                    for (int kx = 0; kx < ksize; ++kx) {
                                        wk[o][ky][kx] = wgt[base + ky * ksize + kx];
                                    }
                                }
                            }
                        }
                    }

                    // 3) tile 内全复用：**抽头串行（外层）× 像素与通道并行（内层）**
                    //    每个 (ky,kx) 步，32 个乘法器一起算 VT_PXP×VT_OCG 个乘加，
                    //    II=1 → 32 MAC/拍。tile 常驻 BRAM，输入被复用 32 次。
                    for (int ky = 0; ky < ksize; ++ky) {
                        for (int kx = 0; kx < ksize; ++kx) {
                            for (int pg = 0; pg < VT_PX; pg += VT_PXP) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                                for (int p = 0; p < VT_PXP; ++p) {
#ifdef USE_HLS_TYPES
#pragma HLS UNROLL
#endif
                                    // VT_TW 是 2 的幂 → 下面两个运算是移位/掩码
                                    const int a = (pg + p) / VT_TW;
                                    const int b = (pg + p) % VT_TW;
                                    const int iv = (int)itile[ic][a * stride + ky]
                                                          [b * stride + kx];
                                    for (int o = 0; o < VT_OCG; ++o) {
#ifdef USE_HLS_TYPES
#pragma HLS UNROLL
#endif
                                        acc[o][a][b] += (acc_t)(iv * (int)wk[o][ky][kx]);
                                    }
                                }
                            }
                        }
                    }
                }

                // ── 写回：每个像素一次（而不是每个输入通道一次）───────────
                for (int o = 0; o < ocb; ++o) {
                    const int32_t obase = (ocg + o) * Hout * Wout;
                    for (int a = 0; a < th; ++a) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                        for (int b = 0; b < tw; ++b) {
                            int32_t v = (int32_t)acc[o][a][b]
                                      + (int32_t)bias[ocg + o];
                            v = v >> shift;
                            if (relu_en != 0 && v < 0) v = 0;
                            out_fm[obase + (ty + a) * Wout + tx + b] = sat8(v);
                        }
                    }
                }
            }
        }
    }
    return ERR_OK;
}
