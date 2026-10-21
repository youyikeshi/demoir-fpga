#ifndef CONV2D_ENGINE_H
#define CONV2D_ENGINE_H

// 单引擎版卷积加速器的接口与资源上界。
//
// 设计目标：一份电路覆盖全部 6 种卷积形态（249 个实例）。
// 形状参数（通道数、核尺寸、步长、膨胀、补边）全部走 s_axilite 寄存器，
// 存储按"全网络最坏组合"分配一次：
//
//   CIN_MAX=352   （全网络最大输入通道：encoder_3 的 1×1 投影 conv_proj）
//   COUT_MAX=192  （1×1 投影与下采样的最大输出通道）
//   KMAX=5        （conv_first 的 5×5）
//   SPAN_MAX=7    （K=3、膨胀 3 时窗口跨 7 行）
//   BWCOL_MAX=76  （横向分块：(64-1)*1+4*3+1 = 76；(32-1)*2+4*1+1 = 67）
//   OC_BLOCK=8    （权重按 8 个输出通道一组轮转装载；v3 从 16 减半，
//                  换 BRAM 余量——CIN_MAX 扩到 352 后行/权重缓冲是 BRAM 大头）
//
// 运行期约束：
//   · H、W ≤ 4096；**且 cin*H*W 必须 < 2^31**（内核用 int32 算 ic*H*W 这类地址）。
//     v3 把 CIN_MAX 从 112 提到 352 后，"4096 就安全"不再成立
//     （352×4096×4096 ≈ 5.9e9 会溢出）；实际网络 640×640×352 ≈ 1.4e8，余量很大。
//     这条由 PS 侧 build_manifest.py / conv_engine.py 先行拦截（内核校验不增加逻辑）。
//   · 累加器 acc_t = ap_int<26>，两个累加器各管一半通道 →
//     ceil(cin/2)·k²·127·128 必须 < 2^26；网络最坏是 cin=192,k=3（约 14M，安全）。
//     同样由 PS 侧拦截。
//   · Hout/Wout 必须等于 same-padding 卷积的输出尺寸 floor((n-1)/stride)+1，内核会校验。
// relu_en：=1 卷积后做 ReLU（默认，等价旧版）；=0 线性输出（饱和 [-128,127]），
// 供 Ghost 残差投影与解码器输出层使用（v2，2026-10-15）。

#include "conv2d_accel.h"  // 复用 data_t / acc_t / bias_t / ERR_* / HLS_PRAGMA

#ifndef CIN_MAX
#define CIN_MAX   352
#endif
#ifndef COUT_MAX
#define COUT_MAX  192
#endif
#ifndef KMAX
#define KMAX      5
#endif
#ifndef DMAX
#define DMAX      3
#endif
#ifndef SPAN_MAX
#define SPAN_MAX  7
#endif
#ifndef BWCOL_MAX
#define BWCOL_MAX 76
#endif
#ifndef OC_BLOCK
#define OC_BLOCK  8
#endif

#ifndef K1_OC_UNROLL
#define K1_OC_UNROLL 4   /* 1×1 快路径：每拍并行几个输出通道（LUTRAM 分 bank 后 4 路并行读）*/
#endif

#ifndef K3_OC_UNROLL
#define K3_OC_UNROLL 2   /* 3×3 快路径：oc 展开系数（先取 2，cosim 过了再谈加）*/
#endif

#ifndef PIPELINE_II
#define PIPELINE_II 1
#endif

// 单引擎卷积。所有形状参数运行期给定，语义与 conv2d_accel 完全一致：
//   out[oc][y][x] = sat8( relu( (sum + bias[oc]) >> shift ) )
//   sum = Σ_{ic,ky,kx} in[ic][y*s+ky*d-pad][x*s+kx*d-pad] * wgt[oc][ic][ky][kx]
// 要求 same padding：(k-1)*d == 2*pad。
// Hout/Wout 由调用方传入（避免内核里做运行期除法）。
int conv2d_engine(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
                  data_t *out_fm, int H, int W, int Hout, int Wout,
                  int cin, int cout, int ksize, int stride, int dil,
                  int pad, int shift, int relu_en);

// 运行期版本的参考实现（金标准，只用于验证，不综合）。
int conv_ref_rt(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
                data_t *out_fm, int H, int W, int Hout, int Wout,
                int cin, int cout, int ksize, int stride, int dil,
                int pad, int shift, int relu);

#endif  // CONV2D_ENGINE_H
