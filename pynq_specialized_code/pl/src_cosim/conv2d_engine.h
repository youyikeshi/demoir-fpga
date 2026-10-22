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
// relu_en：bit0=1 卷积后做 ReLU（默认，等价旧版）；bit0=0 线性输出（饱和
// [-128,127]），供 Ghost 残差投影与解码器输出层使用（v2，2026-10-15）。
// 2026-10-23 起同一个寄存器还带布局位 bit2(输入 C8)/bit3(输出 C8)，
// 见下面 C8_LAYOUT 的说明 —— **不能再用 `relu_en != 0` 判 ReLU**。

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

#ifndef C8_LAYOUT
/* 激活布局的**编译期能力开关**（2026-10-23）：
   0 = 只实现 CHW（addr = c*H*W + y*W + x），**逐字等价于改造前的版本**；
       relu_en 的 bit2/bit3 被**忽略**（硬件里没有 C8，行为与旧版完全相同）。
   1 = 硬件里同时有 CHW 与 C8 两套地址/填充通路，**按层在运行期切换**：
         relu_en bit2 = c8_in   （输入缓冲是 C8 布局）
         relu_en bit3 = c8_out  （输出缓冲是 C8 布局）
       C8 布局：addr = (c/8)*H*W*8 + y*W*8 + x*8 + (c%8)（见
       docs/设计/设计_激活tiled布局_C8交错.md）。
       输入/输出**各自独立**：网络里有 cin 对齐而 cout 不对齐的层
       （decoder_1.out_conv, cout=12），也有 cin 不对齐而 cout 对齐的层
       （encoder.conv_first, cin=12 / cout=48），一位不够用。
   要求调用方把 C8 侧的输入/输出张量按 ceil(C/8)*H*W*8 补零分配（C 不是 8 的
   倍数时，最后一个通道块的尾部 8-C%8 个通道读出的是零，永远不参与乘加）。 */
#define C8_LAYOUT 0
#endif

/* relu_en 的位定义（2026-10-23）。**不要再把它当布尔用**：
     bit0 = ReLU（原有语义）；bit2/bit3 是布局位，只有 C8_LAYOUT=1 的构建才认。
     代码里必须写 `relu_en & 1`；写 `relu_en != 0` 会在设了布局位时误开 ReLU
     （与 depthwise 方案踩过的同类隐患）。 */
#define RELU_EN_RELU_BIT  0
#define RELU_EN_C8IN_BIT  2
#define RELU_EN_C8OUT_BIT 3

#ifndef C8_CH
#define C8_CH 8          /* 布局公式里的通道块大小 */
#endif

#if C8_LAYOUT && (OC_BLOCK != C8_CH)
#error "C8_LAYOUT=1 要求 OC_BLOCK==C8_CH==8：写回时同一像素的 8 个 oc 必须是连续 8 字节"
#endif

#if C8_LAYOUT && (C8_CH != 8)
#error "填充循环用 t>>3 / t&7 拆 (列, 块内通道)，只对 C8_CH==8 成立"
#endif

#ifndef K1_WIDE_COLS
/* k=1 且 stride=1 时，把 line_buf 的 SPAN_MAX 个 row-bank 当作
   SPAN_MAX×64 列的一条宽行用（k=1 本来只用 r=0）→ 整行输入只搬一次，
   不再被多个列块各搬一遍。零额外 BRAM。1=开启 */
#define K1_WIDE_COLS 1
#endif

#if C8_LAYOUT
/* C8 与 wide1 冲突：wide1 把行 bank 当列空间用，写地址变成
   line_buf[r>>6][r&63]，而 C8 的填充按 (列, 块内通道) 线性展开，
   两者对不上。C8 下强制关闭，走普通 k=1 路径（k=1 时 span=1、
   pad=0，普通路径本来就不需要跨行复用）。 */
#ifdef K1_WIDE_COLS
#undef K1_WIDE_COLS
#endif
#define K1_WIDE_COLS 0
#endif

#if C8_LAYOUT
/* C8 的填充是 8*lb 字节的**一条长 burst**（lb≤76 ⇒ 最多 608 B）；
   32 位 m_axi 数据宽度下是 152 拍，必须把 burst 上限从 64 提到 256，
   否则 HLS 会把它拆成 3 笔（64+64+24），收益从 ÷8 掉到 ÷2.7。 */
#define RD_BURST_MAX 256
#define WR_BURST_MAX 256
#else
#define RD_BURST_MAX 64
#define WR_BURST_MAX 64
#endif

/* 2026-10-22：展开系数取 2。cosim 实测 4→2 拍数不升反降：
   k=3 369236 → 369236（相同），k=1 146674 → 138418（快 5.6%）；
   而 DSP 从 131 降到约一半，是"两引擎集成后 178 > 160 超限"的直接解法。 */
#ifndef K1_OC_UNROLL
#define K1_OC_UNROLL 2   /* 1×1 快路径：每拍并行几个输出通道（LUTRAM 分 bank 后 4 路并行读）*/
#endif

#ifndef K3_FAST
/* 3×3 快路径开关（默认 0 = 关）。
   为什么默认关：这条路径 8 车道并行要 **+51 个 DSP48**（80 → 131），而 7z015 只有
   160 个。单独跑卷积引擎时开着能省 66 s；但与 v2 层间算子引擎（约 30 个 DSP）
   同板时会 173 > 160，place 前的 DRC 直接拒绝。
   想同时要：需要先在卷积侧再腾出 ~51 个 DSP（例如 k=3 降到 4 车道、或整体降并行）。 */
#define K3_FAST 1
#endif

#ifndef K3_LANES
/* 3×3 快路径每拍并行几个输出通道。
   8（满块）时 DSP 112 —— 与 v2 层间算子引擎（30）同板会 142/160 且时序 −1.08 ns；
   4 时同一条路径的乘法器/累加器减半，给 pp 腾出余量。cosim 会给出拍数代价。 */
#define K3_LANES 4
#endif

#ifndef K3_PIPELINE_II
/* k=3 快路径 ic 流水的目标 II。取 3 而不是 2：II 越大，同一拍要同时用到的
   乘法器越少（DSP 从 131 降到约 96），代价是拍数 ×1.5。与 v2 引擎同板时
   DSP 只有 160 个，这个折中是必要的。 */
#define K3_PIPELINE_II 3
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
/* FUSE_OUT=1 时才真正启用"融合出口"（out_lut / chsum）：
   默认 0 表示这两个参数完全不使用，HLS 不会为它们生成 AXI-Lite 寄存器，
   交付版的 ABI 与资源（131 DSP / 163 BRAM）保持逐字不变。 */
#ifndef FUSE_OUT
#define FUSE_OUT 0      /* 256 项出口 LUT（cat 融合）：实测 +0.64 ns，慎用 */
#endif
#ifndef FUSE_SUM
#define FUSE_SUM 0      /* 每通道累加和（ECA 融合）：只是几条 32 位加法器，很便宜 */
#endif

int conv2d_engine(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
                  data_t *out_fm, int H, int W, int Hout, int Wout,
                  int cin, int cout, int ksize, int stride, int dil,
                  int pad, int shift, int relu_en,
                  /* 融合出口（默认 0 = 完全不使用，行为与旧版逐字一致）：
                       out_lut —— 256 项 LUT，写回时把本层标度映射到目标张量标度
                       chsum   —— 每输出通道累加和（ECA 的通道均值）
                     只有 FUSE_OUT=1 的构建才会为它们生成 AXI-Lite 寄存器。 */
                  const data_t *out_lut = 0, int32_t *chsum = 0);

// 运行期版本的参考实现（金标准，只用于验证，不综合）。
int conv_ref_rt(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
                data_t *out_fm, int H, int W, int Hout, int Wout,
                int cin, int cout, int ksize, int stride, int dil,
                int pad, int shift, int relu);

#endif  // CONV2D_ENGINE_H
