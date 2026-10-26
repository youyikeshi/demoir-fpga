#ifndef CONV2D_ACCEL_H
#define CONV2D_ACCEL_H

// 双环境编译：g++ 走原生类型，Vitis HLS 走任意位宽类型。
// 选 g++ 是因为本机没装 HLS 也能验证算法；选 ap_int 是因为硬件位宽要精确控制。
#ifdef USE_HLS_TYPES
  #include "ap_int.h"
  typedef ap_int<8>  data_t;
  typedef ap_int<26> acc_t;
  typedef ap_int<32> bias_t;
  // 两段式字符串化：先展开参数里的宏（例如 PIPELINE_II），再交给 _Pragma
  #define HLS_PRAGMA_STR(x) #x
  #define HLS_PRAGMA_CAT(x) HLS_PRAGMA_STR(x)
  #define HLS_PRAGMA(x) _Pragma(HLS_PRAGMA_CAT(x))
#else
  #include <cstdint>
  typedef int8_t  data_t;
  typedef int32_t acc_t;
  typedef int32_t bias_t;
  #define HLS_PRAGMA(x)
#endif

// 卷积参数。默认值是 encoder.conv_first（12→48，5×5，补边 2）。
// 其他层用 -D 覆盖，例如：
//   -DCIN=48 -DCOUT=16 -DKSIZE=1 -DPAD=0     (Ghost 的 1×1 卷积)
//   -DCIN=112 -DCOUT=64 -DKSIZE=3 -DPAD=1    (decoder 的 3×3 预卷积)
#ifndef CIN
#define CIN    12
#endif
#ifndef COUT
#define COUT   48
#endif
#ifndef KSIZE
#define KSIZE  5
#endif
#ifndef PAD
#define PAD    2
#endif

#ifndef MAX_W
#define MAX_W  512
#endif

// 膨胀率。默认 1 是普通卷积。
// Ghost 模块的 cheap_operation 用 dilation 1/2/3，配 padding=d、kernel=3。
#ifndef DILATION
#define DILATION 1
#endif

// 步长。默认 1。encoder 的前两级下采样用步长 2。
#ifndef STRIDE
#define STRIDE 1
#endif

// 输出边长。same padding 且步长 1 时等于输入边长。
#define CONV_OUT(n) ((((n) + 2 * PAD - ((KSIZE - 1) * DILATION + 1)) / STRIDE) + 1)

// 本实现按"same padding"工作：补边必须等于 (K-1)*d/2。
// 不满足时编译期直接报错，避免跑出错误结果却看不出来。
#if (PAD * 2) != ((KSIZE - 1) * DILATION)
#error "本实现要求 PAD == (KSIZE-1)*DILATION/2，即 same padding"
#endif

// 返回值：0 表示成功，其余为错误码
enum {
    ERR_OK     = 0,
    ERR_NULL   = 1,
    ERR_SIZE   = 2,
    ERR_SHIFT  = 3,
    /* C8_LAYOUT=1 的构建只认 C8 布局：调用方必须把 relu_en 的 bit2/bit3 都置 1
       （= "我按 C8 分配了缓冲区"的声明）。声明不符直接报这个错，而不是静默地把
       CHW 数据当 C8 读（那会算出一堆看似正常的垃圾）。 */
    ERR_LAYOUT = 4,
    /* 2026-10-26：k=1 已从 conv2d_engine 移除（改由手写 RTL conv_k1_engine 承担）。
       K1_IN_HLS=0 的构建收到 ksize==1 时**返回这个码**，而不是落到 k=3/k=5
       的通路上去静默算错（通用通路只会算 kx=ky=0，输出"看着正常"但整网崩）。
       只新增枚举值：不新增/不移动任何 AXI-Lite 寄存器，返回码仍在同一个 32 位
       ap_return 里，PS 侧按"非 0 即错误"处理即可。见 docs/部署/HLS移除k1分支.md。 */
    ERR_K1_UNSUPPORTED = 5,
    /* 2026-10-26（第二刀）：k=5 已从 conv2d_engine 移除（改由手写 RTL conv_k5_engine
       承担，全网只有 1 层：encoder.conv_first）。K5_IN_HLS=0 的构建收到 ksize==5
       时**返回这个码**，而不是落到通用通路上静默算错。
       同样只新增枚举值：AXI-Lite 寄存器映射、端口、位宽、函数签名全都不变，
       返回码仍在同一个 32 位 ap_return 里。见 docs/部署/HLS再移除k5分支.md。 */
    ERR_K5_UNSUPPORTED = 6
};

// HLS 综合目标
int conv2d_accel(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
                 data_t *out_fm, int H, int W, int shift);

// 本机参考实现，用于比对
int conv_ref(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
             data_t *out_fm, int H, int W, int shift);

#endif  // CONV2D_ACCEL_H
