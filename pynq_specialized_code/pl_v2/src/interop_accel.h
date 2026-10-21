// interop_accel.h —— v2 层间算子加速器的寄存器/算子码契约
//
// 设计依据：pynq_specialized_code/docs/设计_v2_层间算子加速器.md §1~§2。
// 一块流式多源加权引擎覆盖五个 PS 算子：RESCALE / ADD / SUM3W / INTERP / REDUCE_MEAN。
//
// 数值契约（全 Q16，int32 系数）：
//   ratio 系数 k = round(ratio * 65536)
//   acc = Σ src_i[p] * k_i           （int32 乘、int64 累加）
//   out = sat_int8(round_half_even(acc >> 16))
// 这条与 numpy 的 rint 半偶对齐；跨平台不再依赖 float。
#ifndef INTEROP_ACCEL_H
#define INTEROP_ACCEL_H

// ── 寄存器偏移（s_axilite，基址建议 0x4002_0000）────────────────────────────
#define PP_REG_CTRL        0x00   // bit0=start, bit1=done；返回值 0/2/3/4
#define PP_REG_OP          0x08   // 算子码
#define PP_REG_N           0x10   // 元素总数（C*H*W 或 interp 的输出元素数）
#define PP_REG_C           0x18
#define PP_REG_H           0x1C
#define PP_REG_W           0x20
#define PP_REG_SRC0        0x28   // 64 位（低 32 位）
#define PP_REG_SRC1        0x30
#define PP_REG_SRC2        0x38
#define PP_REG_SRC3        0x40
#define PP_REG_DST         0x48
#define PP_REG_COEF        0x50   // 系数表地址（int32 数组，逐通道）
#define PP_REG_DST_CH_OFF  0x58   // RESCALE/ADD 写入目标第几个通道起
#define PP_REG_NSRC        0x5C   // 参与源个数 1..4

// ── 算子码 ─────────────────────────────────────────────────────────────────
#define PP_OP_RESCALE      1   // dst = sat(round(src0 * k0) >> 16)，可带通道偏移（吃掉 cat 的重定标）
#define PP_OP_ADD          2   // dst = sat(round(src0*k0 + src1*k1) >> 16)
#define PP_OP_SUM3W        3   // dst[c] = sat(round(Σ src_i[c]*k_i[c] >> 16))，i=0..3
#define PP_OP_INTERP       4   // 双线性，比例由 out_H/out_W 推出（1/2、1/4、2、4）
#define PP_OP_REDUCE_MEAN  5   // 逐通道 int64 求和写 DST（C 个 int64），均值比例由 PS 算

// ── 错误码（与 conv 引擎同风格）──────────────────────────────────────────────
#define PP_OK              0
#define PP_ERR_OP          2   // 不支持的算子码
#define PP_ERR_SHAPE       3   // 形状非法
#define PP_ERR_NULL        4   // 空指针

// 逐通道系数上限（当前网络最大 352 通道，留余量到 576）
#define PP_CH_MAX          576
// INTERP 的输入行缓冲宽度上限（最宽特征图 640，按 640 列一块）
#define PP_ROW_MAX         640

// ── 函数原型（主机侧测试与 HLS 综合共用）────────────────────────────────────
#ifdef USE_HLS_TYPES
#include "ap_int.h"
typedef ap_int<8>  pp_data_t;
typedef ap_int<32> pp_coef_t;
#else
typedef signed char pp_data_t;
typedef signed int  pp_coef_t;
#endif

int interop_accel(const pp_data_t *src0, const pp_data_t *src1,
                  const pp_data_t *src2, const pp_data_t *src3,
                  pp_data_t *dst, const pp_coef_t *coef,
                  int op, int C, int H, int W,
                  int out_H, int out_W, int dst_ch_off, int nsrc);

#endif  // INTEROP_ACCEL_H
