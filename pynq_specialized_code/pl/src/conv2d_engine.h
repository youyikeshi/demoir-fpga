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
// 2026-10-23 起同一个寄存器还带 bit2/bit3（C8 布局的**声明位**，不是选路位），
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
/* 激活布局的**编译期开关**（2026-10-23；第二轮改为纯编译期，无运行期 mux）：
   0 = 只实现 CHW（addr = c*H*W + y*W + x），**逐字等价于改造前的版本**；
       relu_en 的 bit2/bit3 被**忽略**（硬件里没有 C8，行为与旧版完全相同）。
   1 = 只实现 C8 交错布局（addr = (c/8)*H*W*8 + y*W*8 + x*8 + (c%8)，见
       docs/设计/设计_激活tiled布局_C8交错.md）。**硬件里没有 CHW 通路**：
       填充与写回是唯一一份 C8 代码，数据通路里没有任何布局 mux。
       relu_en 的 bit2/bit3 在这个构建里是**声明位**：调用方声明"输入/输出
       缓冲区按 ceil(C/8)*8*H*W 分配并清零、按 C8 地址读写"；两位都必须为 1，
       否则返回 ERR_LAYOUT（不选路，只做一次入参校验）。
   为什么不做运行期位（上一版的教训）：运行期 select 让写回循环多 1~2 拍，
   cosim 339,120 → 340,500、板上 conv 129.8 → 139.6 s（+7.5%），C8 的收益
   被吃光；而"两种布局都要支持"本身又让 LUT +2,559、DSP +13。
   现在的前提是 PS 侧把所有 int8 张量补零到 8 的倍数（3ch -> 8、12ch -> 16），
   于是整网只有一种布局。非 8 倍数的 cin/cout 依然被正确支持：
   cin 侧读满最后一个 8 通道块（补零区是 0）、乘加只跑 ic<cin；
   cout 侧只写 cout 个通道，尾部补零通道保持 0。 */
#define C8_LAYOUT 0
#endif

/* relu_en 的位定义（2026-10-23）。**不要再把它当布尔用**：
     bit0 = ReLU（原有语义）；
     bit2/bit3 = C8 布局的声明位：C8_LAYOUT=1 的构建**要求**两位都为 1
     （否则 ERR_LAYOUT），不参与任何地址计算；C8_LAYOUT=0 的构建完全忽略。
     代码里必须写 `relu_en & 1`；写 `relu_en != 0` 会在设了声明位时误开 ReLU
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

/* ⚠️ 2026-10-23 核实：这个宏**已经空转**（改 2/4/8 的 csynth 结果逐项一个数都不差：
   LUT 31,882 / DSP 100 / FF 21,793 / BRAM 175，k=1 快路径本来就是 8 个乘法器 + II=1，
   外层 II=1 约束下 HLS 把内层循环完全展开，factor 不起作用）。
   保留定义只为兼容旧脚本；**不要再拿它当旋钮调 k=1 并行度**。
   2026-10-23 第十轮：k=1 快路径已不再引用这个宏（改用 K1_LANES），彻底死掉。
   旧注释（"4→2 快 5.6%、DSP 减半"）描述的是 2026-10-22 之前的循环结构，已失效。 */
#ifndef K1_OC_UNROLL
#define K1_OC_UNROLL 2   /* 1×1 快路径：每拍并行几个输出通道（LUTRAM 分 bank 后 4 路并行读）*/
#endif

#ifndef K3_FAST
/* ⚠️ 2026-10-23 核实：K3_FAST / K3_LANES / K3_PIPELINE_II / K3_OC_UNROLL 这四个宏
   在 2026-10-22 的 k=3 快速路径重写之后**已经没有任何代码引用**（grep -c = 0），
   全是死宏；改它们 csynth 结果一个数都不变。下面几段旧注释描述的是重写前的结构，
   留作历史记录，不要照着它做预算。 */
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

#ifndef K3_PAD12
/* k=3 权重的**容器布局**与装载宽度（2026-10-23 第四轮；编译期定死，无运行期 mux）。

   1 = 导出侧把每个 (oc,ic) 的 9 个抽头**紧排在前 9 字节**、再补 3 个 0，凑成
       **12 字节 = 3 个 32-bit 字**（抽头 k 在第 k>>2 个字的第 k&3 字节），
       容器 = [oc][ic][12]。
       好处（全在 `conv2d_engine.cpp` 的装载循环里）：
         · 每个 (oc,ic) 起点 (oc·cin+ic)·12 恒 4 字节对齐 ⇒ 一笔读 3 个字，
           事务数 ÷9、每笔 12 字节；
         · 写侧仍是 9 个**编译期常量** bank、每秒 9 字节上界 ⇒ 读侧数组形状、
           72 个 LUTRAM bank 划分、计算路径的 DSP/FF/LUT 全都不动；
         · 没有"cin 是否 4 的倍数"的分支 —— 任何 cin 都走同一条路径。
       代价：权重体积 ×4/3（k=3 的 357 MB → 476 MB）。
   0 = 基线：容器 [oc][ic][9]，每字节一笔 1-byte 事务（ARLEN=1）。
       这个构建必须配"旧容器"的权重包（`ps/tools/repack_k3_pad12.py --revert`），
       1 必须配新容器，混用不会报错但会算出垃圾 —— 所以由导出一侧定版本、
       `check_consistency.py` 上板前拦一道。

   ⚠️ 本轮试过并否决的两条（都有实测，别再走）：
     · 把 wgt3_buf 拍平成 [oc][cin*9]：读侧 9 个抽头挤进一个 bank，LUT 91%。
     · 导出侧按 (ky,kx) 拆 9 个平面（体积不变）：写 bank 由 (oc,平面) 两个运行期
       下标决定，HLS **拒绝**把装载循环做成流水（实测 5 拍/字节），cosim
       331,226 → 458,786 拍；且 LUT 36,775 → 38,235、DSP 104 → 111。
     · 不改容器、按 4 个 ic 一组读 9 个字（体积不变）：一个循环体 36 条静态写，
       写端口多路器 +3.8k LUT（88%，超红线）。
   详见 docs/设计/记录_20261023_k3权重装载宽读.md。 */
#define K3_PAD12 1
#endif

#ifndef K5_PAD28
/* k=5 权重的**容器布局**与装载宽度（2026-10-23 第五轮；与 K3_PAD12 同一招）。

   1 = 导出侧把每个 (oc,ic) 的 25 个抽头紧排在前 25 字节、再补 3 个 0，凑成
       **28 字节 = 7 个 32-bit 字**（抽头 k 在第 k>>2 个字的第 k&3 字节），
       容器 = [oc][ic][28]。
       好处与 k=3 的 12 字节容器完全同构：
         · 每个 (oc,ic) 起点 (oc·cin+ic)·28 恒 4 字节对齐 ⇒ 一笔读 7 个字，
           事务数从"每字节一笔"降到"每 28 字节一笔"；
         · 写侧仍是 25 个**编译期常量** bank（wgt_buf 的 dim3/dim4 完全拆分，
           与改造前逐字相同）⇒ 计算路径的 DSP/FF/LUT 不动；
         · 没有"cin 是否 4 的倍数"的分支。
       代价：k=5 权重体积 ×28/25 = 1.12（全网络只有 encoder.conv_first 一层，
       1.47 MB → 1.65 MB 量级）。
   0 = 基线：容器 [oc][ic][25]，每字节一笔 1-byte 事务（ARLEN=1）。
       ⚠️ 与 K3_PAD12 一样，混用配错包**不会报错**只会算出垃圾，
       由 `check_consistency.py` 上板前拦一道。 */
#define K5_PAD28 1
#endif

/* ============================================================================
   K1_IN_HLS —— **HLS 侧是否保留 k=1 快路径**（2026-10-26）

   默认 **0：本构建里没有 k=1 通路**。k=1 已由手写 RTL 模块 conv_k1_engine
   完全替代（132/132 层、逐字节一致、6.5× 提速），HLS 里那套 16 车道快路径
   只剩下资源占用（BD 总 LUT 55,390 > 器件 46,200，布局失败）。

   一键恢复：把下面的 0 改回 1（等价于构建时加 `-DK1_IN_HLS=1`）。
   所有 k=1 代码块**原地保留**在 `#if K1_IN_HLS` 里，恢复后与 2026-10-23
   第十轮的版本逐字相同（K1_LANES=16 / K1_DSP_LANES=8 / K1_WIDE_COLS 默认）。

   =0 时发生什么：
     · 函数入口对 `ksize == 1` 直接 `return ERR_K1_UNSUPPORTED`（码 5）——
       **宁可报错，绝不静默算错**（掉进通用通路只会算 kx=ky=0，结果"看着正常"）；
     · 16 车道阵列 `acc0/acc1[K1_LANES]`、`wgt1_buf`（16 bank LUTRAM）、k=1
       权重装载、k=1 收尾、wbuf 的后 8 个 bank、第二段 out_copy 全部被预处理掉
       ⇒ 综合时零资源；`wide1`（448 列宽列块）恒假，填充/读地址的 >>6/&63
       分支常量折叠；
     · 下面两组宏**不再定义**——任何残留引用会直接编译失败，而不是悄悄拿到
       一个默认值（这比"能编过但语义不对"安全）。
   =1 时：回到完整 k=1 快路径，与改造前逐字等价。
   ========================================================================== */
#ifndef K1_IN_HLS
#define K1_IN_HLS 0
#endif

#if K1_IN_HLS
#ifndef K1_LANES
/* k=1 快路径的**车道数**（每拍几个 MAC），2026-10-23 第十轮。
   8  = 一个 OC 块（原有行为；这一档与原实现逐字等价）；
   16 = 两个 OC 块：`ocb` 循环仍按 OC_BLOCK 步进，但 k=1 时**跳过奇数块**，
        一个 pass 算 ocb..ocb+15。
   为什么 16 车道不撞 line_buf：16 个 oc 共用**同一个**像素 `p`，line_buf 每拍
   仍然只读 1 次 ⇒ 不动 91% 的 BRAM、不动填充、不动 k=3/k=5；
   加倍的只有 wgt1_buf（+8 个 LUTRAM bank）、wbuf（+8 个 bank）、累加器（16×2 条链）。
   实测：k=1 计算 13.8 s（1.276×理想）→ ~6.9 s；见
   docs/设计/记录_20261023_cin×cout二维网格与conv成本模型.md 与第十轮记录。 */
#define K1_LANES 16
#endif

#ifndef K1_DSP_LANES
/* 前这么多个 k=1 车道用 BIND_OP 强制进 DSP48，其余留在 LUT 乘法器。
   为什么要混搭：`mul_8s_8s_16` 41 LUT/个，全塞 DSP 要 +16 DSP；
   混搭 8/8 实测只 +8 DSP、+328 LUT（DSP 预算留给以后）。 */
#define K1_DSP_LANES 8
#endif

#if (K1_LANES != 8) && (K1_LANES != 16)
#error "K1_LANES 只能是 8 或 16"
#endif
#if (K1_DSP_LANES < 0) || (K1_DSP_LANES > K1_LANES)
#error "K1_DSP_LANES 必须在 [0, K1_LANES] 内"
#endif
#endif  // K1_IN_HLS：关掉时 K1_LANES / K1_DSP_LANES 不存在（残留引用=编译错误）

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
