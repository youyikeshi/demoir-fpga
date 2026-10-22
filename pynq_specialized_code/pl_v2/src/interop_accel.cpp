// interop_accel.cpp —— v2 层间算子加速器（一块流式多源加权引擎）
//
// 覆盖 5 个 PS 算子：RESCALE / ADD / SUM3W / INTERP / REDUCE_MEAN。
// 设计见 pynq_specialized_code/docs/设计_v2_层间算子加速器.md。
//
// 数值契约（**必须逐位一致**，PS 侧 test_pp_vs_numpy.py 会对拍）：
//   k_i  : Q16（int32），ratio = k/65536
//   acc  : int64 = Σ src_i * k_i
//   out  : sat_int8( round_half_even(acc >> 16) )
//
// round_half_even 的整数写法（对负数也成立：C++ 的 >> 是算术右移，
// 低 16 位用掩码取的是补码小数部分）：
//   q = acc >> 16; r = acc & 0xFFFF;
//   r > 0x8000 → q++;  r == 0x8000 → (q & 1) ? q++ : q;  否则不变。
//
// 与 conv 引擎不同，这里**没有跨拍循环依赖**：主流水是纯流式（每拍处理 8 个
// 元素），所以不需要 cosim 花招；但 interp 有行缓冲、reduce 有累加器，这两条
// 仍要走 C/RTL cosim（见 scripts/README）。
#include "interop_accel.h"

#ifdef USE_HLS_TYPES
#include "ap_int.h"
typedef ap_int<8>   data_t;
typedef ap_int<32>  coef_t;
typedef ap_int<64>  acc_t;
typedef ap_int<32>  idx_t;
#else
#include <stdint.h>
typedef int8_t      data_t;
typedef int32_t     coef_t;
typedef int64_t     acc_t;
typedef int32_t     idx_t;
#endif

// ─────────────────────────────────────────────────────────────────────────────
// 定点工具
// ─────────────────────────────────────────────────────────────────────────────
// 累加器用 int32：4 源 × 128 × 65536 = 3.4e7 < 2^31，绝对安全。
// 原来用 int64 让 HLS 把每个累加都映射成 DSP48 的"Block Arithmetic"（宽加法链），
// 与 conv 引擎同板时 DSP 超限。32 位加法一半宽度，DSP 用量显著下降。
static inline int32_t pp_round_q16(int32_t acc) {
    // 半偶舍入（与 numpy.rint 一致）
    int32_t q = acc >> 16;
    const int32_t r = acc & 0xFFFF;
    if (r > 0x8000) {
        ++q;
    } else if (r == 0x8000) {
        if (q & 1) ++q;          // 恰好半个 → 取偶
    }
    if (q > 127)  q = 127;
    if (q < -128) q = -128;
    return (int32_t)q;
}

// ─────────────────────────────────────────────────────────────────────────────
// 主入口
// ─────────────────────────────────────────────────────────────────────────────
int interop_accel(const data_t *src0, const data_t *src1, const data_t *src2,
                  const data_t *src3, data_t *dst, const coef_t *coef,
                  int op, int C, int H, int W,
                  int out_H, int out_W, int dst_ch_off, int nsrc) {
    // depth= 是 cosim 必需的（"A depth specification is required for
    // interface port"），给**上界**即可：v2 的张量最大 352x640x640 远超实际，
    // 所以这里只给一个够用的上界（4096 元素），cosim 的 TB 会把缓冲按上界
    // 分配（否则 AXI 模型按 depth 访问会 SIGSEGV）。换数据集不需要改源码。
    // （conv 引擎那份是精确值，因为它的 cosim 数据集固定；代价是换数据要改。）
#ifdef USE_HLS_TYPES
#pragma HLS INTERFACE m_axi port=src0 offset=slave bundle=gmem0 depth=4096 \
    max_read_burst_length=64 num_read_outstanding=16
#pragma HLS INTERFACE m_axi port=src1 offset=slave bundle=gmem0 depth=4096 \
    max_read_burst_length=64 num_read_outstanding=16
#pragma HLS INTERFACE m_axi port=src2 offset=slave bundle=gmem0 depth=4096 \
    max_read_burst_length=64 num_read_outstanding=16
#pragma HLS INTERFACE m_axi port=src3 offset=slave bundle=gmem0 depth=4096 \
    max_read_burst_length=64 num_read_outstanding=16
#pragma HLS INTERFACE m_axi port=dst  offset=slave bundle=gmem1 depth=4096 \
    max_write_burst_length=64 num_write_outstanding=16
#pragma HLS INTERFACE m_axi port=coef offset=slave bundle=gmem2 depth=2304
#pragma HLS INTERFACE s_axilite port=op    bundle=ctrl
#pragma HLS INTERFACE s_axilite port=C     bundle=ctrl
#pragma HLS INTERFACE s_axilite port=H     bundle=ctrl
#pragma HLS INTERFACE s_axilite port=W     bundle=ctrl
#pragma HLS INTERFACE s_axilite port=out_H bundle=ctrl
#pragma HLS INTERFACE s_axilite port=out_W bundle=ctrl
#pragma HLS INTERFACE s_axilite port=dst_ch_off bundle=ctrl
#pragma HLS INTERFACE s_axilite port=nsrc  bundle=ctrl
#pragma HLS INTERFACE s_axilite port=return bundle=ctrl
#endif

#ifdef USE_HLS_TYPES
    // 2026-10-22：这两个算子块是**纯访存型**（每张图读 0.5~1.5 GB，计算只有几亿次
    // 8x32 乘），乘法器少几个几乎不影响时间。与 conv 引擎同板时 DSP 只有 160 个，
    // conv 已占 143，所以这里把 mul 硬限到 8 个（原来综合出 30 个）。
// 2026-10-22：PP 是**访存型**（约 0.8 MAC/byte），II 放大几乎不增加墙钟时间，
// 但能把乘法器共享掉 —— 这是"把 PP 做小"最直接的一招。
// limit=2 时 32×32 乘法器最多 2 个（≈8 DSP），而 DDR 往返时间完全不变。
#pragma HLS ALLOCATION operation instances=mul limit=2
#endif
    if (op < PP_OP_RESCALE || op > PP_OP_REDUCE_MEAN) return PP_ERR_OP;
    if (C <= 0 || H <= 0 || W <= 0) return PP_ERR_SHAPE;
    if (C > PP_CH_MAX) return PP_ERR_SHAPE;
    if (src0 == 0 || dst == 0) return PP_ERR_NULL;
    if (coef == 0) return PP_ERR_NULL;

    // 系数不整表预装：每个算子都是"外层走通道、内层走元素"，每条通道只需
    // 1~4 个系数，放寄存器，不需要 C×nsrc 的片上表（SUM3W 会到 4C = 9 KB）。
    const int HW = H * W;

    switch (op) {
    // ── RESCALE：单位置缩放（吃掉 cat 的重定标）──────────────────────────────
    // dst[(dst_ch_off + c)*H*W + i] = sat(round(src0[c*H*W + i] * kbuf[c]) >> 16)
    case PP_OP_RESCALE: {
        // 2026-10-22：**LUT 化**。数据是 int8（只有 256 种取值），所以
        //   dst = clip(round(src * k / 2^16))
        // 对固定 k 就是一个 256 项表。查表与乘法**逐位等价**（主机对拍
        // 819 万样本 0 不一致），但把每元素一次 32×32 乘法换成一次 ROM 读。
        // 建表本身只剩 1 个乘法器（256 拍建完，HW 通常几千，开销 <5%）。
        for (int c = 0; c < C; ++c) {
            const coef_t k = coef[c];
            const int32_t base_s = c * HW;
            const int32_t base_d = (dst_ch_off + c) * HW;
            data_t lut[256];
#ifdef USE_HLS_TYPES
// 256 项表放 LUTRAM（分布式 RAM）：不占 BRAM 站点，避免与 conv 的 163 块抢位
#pragma HLS BIND_STORAGE variable=lut type=RAM_1P impl=LUTRAM
#pragma HLS PIPELINE II=1
#endif
            for (int t = 0; t < 256; ++t) {
                lut[t] = (data_t)pp_round_q16((int32_t)(t - 128) * (int32_t)k);
            }
            for (int i = 0; i < HW; ++i) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                dst[base_d + i] = lut[(int)src0[base_s + i] + 128];
            }
        }
        break;
    }

    // ── ADD：两源加权相加 ────────────────────────────────────────────────────
    case PP_OP_ADD: {
        if (nsrc < 2 || src1 == 0) return PP_ERR_NULL;
        // 注意：这里**不能**用两张 LUT 相加——原语义是"先加权求和、再一次性
        // 舍入"，两张表各自舍入后再加会分岔（csim 实测 ref=8 got=9）。
        // 只有 RESCALE 这种单源运算才能安全 LUT 化。
        for (int c = 0; c < C; ++c) {
            const coef_t k0 = coef[2 * c + 0];
            const coef_t k1 = coef[2 * c + 1];
            const int32_t b = c * HW;
            for (int i = 0; i < HW; ++i) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=2      // 两源共用一个乘法器
#endif
                const int32_t acc = (int32_t)((int32_t)src0[b + i] * k0)
                                  + (int32_t)((int32_t)src1[b + i] * k1);
                dst[b + i] = (data_t)pp_round_q16(acc);
            }
        }
        break;
    }

    // ── SUM3W：多源逐通道加权和（sam_sum 的形态：3~4 源 + 逐通道权重）────────
    // 系数排布：kbuf[c*4 + j] 是第 j 个源对通道 c 的 Q16 系数。
    case PP_OP_SUM3W: {
        if (nsrc < 1 || nsrc > 4) return PP_ERR_SHAPE;
        if ((nsrc >= 2 && src1 == 0) || (nsrc >= 3 && src2 == 0) ||
            (nsrc >= 4 && src3 == 0)) return PP_ERR_NULL;
        for (int c = 0; c < C; ++c) {
            const coef_t k0 = coef[4 * c + 0];
            const coef_t k1 = coef[4 * c + 1];
            const coef_t k2 = coef[4 * c + 2];
            const coef_t k3 = coef[4 * c + 3];
            const int32_t b = c * HW;
            // 源用**运行时循环**而不是 4 条展开的乘加：HLS 只会实例化 1 个乘法器
            // （II≈nsrc），DSP 从 4 个降到 1 个。SUM3W 是纯访存型，II=4 仍然远快于
            // DDR 带宽，因此不影响实际速度。
            for (int i = 0; i < HW; ++i) {
#ifdef USE_HLS_TYPES
                // II 必须是**常量**：写成 II=nsrc（运行时）HLS 会忽略，仍按 II=1
                // 排 4 个乘法器。固定 II=4 → 四个源共用一个乘法器。
                // SUM3W 是纯访存型，II=4 仍远快于 DDR。
#pragma HLS PIPELINE II=4
#endif
                int32_t acc = (int32_t)src0[b + i] * k0;
                if (nsrc >= 2) acc += (int32_t)src1[b + i] * k1;
                if (nsrc >= 3) acc += (int32_t)src2[b + i] * k2;
                if (nsrc >= 4) acc += (int32_t)src3[b + i] * k3;
                dst[b + i] = (data_t)pp_round_q16(acc);
            }
        }
        break;
    }

    // ── INTERP：双线性，比例 1/2、1/4、2/1、4/1（权重只落 1/8 网格）─────────
    // **必须与 ps/pynq/ops_numpy.py 的整数路径逐位一致**：
    //   位置 = 中心对齐：src = (o+0.5)/scale - 0.5，clamp 到 [0, in-1]
    //   权重 w8 = rint((src - i0) * 8)，两段 ×8 → 合计分母 64
    //   最后 half-even ÷64 再饱和到 int8
    // 整数化：pos8 = (2o+1)*in*8/(2*out) - in*8/2，当 in/out 是 2 的幂时该除法精确；
    // 不精确说明权重不在 1/8 网格上（PS 侧会回退浮点），这里直接拒绝。
    case PP_OP_INTERP: {
        if (out_H <= 0 || out_W <= 0) return PP_ERR_SHAPE;
        const int in8h = H * 8, in8w = W * 8;
        for (int c = 0; c < C; ++c) {
            const int32_t sb = c * HW;
            const int32_t db = c * out_H * out_W;
            int32_t numy = in8h;                    // (2*0+1)*in8h
            for (int oy = 0; oy < out_H; ++oy, numy += 2 * in8h) {
                if (numy % (2 * out_H) != 0) return PP_ERR_SHAPE;
                int posy = numy / (2 * out_H) - 4;   // -0.5 个源像素 = -4/8
                if (posy < 0) posy = 0;
                if (posy > (H - 1) * 8) posy = (H - 1) * 8;
                const int y0 = posy >> 3;
                const int wy = posy & 7;
                const int y1 = (y0 + 1 < H) ? (y0 + 1) : (H - 1);
                int32_t numx = in8w;
                for (int ox = 0; ox < out_W; ++ox, numx += 2 * in8w) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                    int posx = numx / (2 * out_W) - 4;
                    if (posx < 0) posx = 0;
                    if (posx > (W - 1) * 8) posx = (W - 1) * 8;
                    const int x0 = posx >> 3;
                    const int wx = posx & 7;
                    const int x1 = (x0 + 1 < W) ? (x0 + 1) : (W - 1);
                    const int32_t p00 = src0[sb + y0 * W + x0];
                    const int32_t p01 = src0[sb + y0 * W + x1];
                    const int32_t p10 = src0[sb + y1 * W + x0];
                    const int32_t p11 = src0[sb + y1 * W + x1];
                    // 两段 ×8（先横后纵，与"先纵后横"在整数上等价）
                    const int32_t top = p00 * (8 - wx) + p01 * wx;
                    const int32_t bot = p10 * (8 - wx) + p11 * wx;
                    const int32_t v64 = top * (8 - wy) + bot * wy;   // 单位 64
                    int32_t q = v64 >> 6;                            // 半偶
                    const int32_t rem = v64 & 63;
                    if (rem > 32) ++q;
                    else if (rem == 32 && (q & 1)) ++q;
                    if (q > 127) q = 127;
                    if (q < -128) q = -128;
                    dst[db + oy * out_W + ox] = (data_t)q;
                }
            }
        }
        break;
    }

    // ── REDUCE_MEAN：逐通道 int64 求和写回（PS 只再做 576 次除法/加法）───────
    case PP_OP_REDUCE_MEAN: {
        // 逐通道 int64 求和写回。**不用 int64* 强转**（HLS 不接受 ap_int<8>* 的
        // 重解释），改成按小端拆成 8 个字节写进字节缓冲：PS 侧 `np.frombuffer(..,
        // np.int64)` 直接读。576 通道 × 8 字节 = 4.6 KB，开销可忽略。
        for (int c = 0; c < C; ++c) {
            int64_t s = 0;
            const int32_t b = c * HW;
            for (int i = 0; i < HW; ++i) {
#ifdef USE_HLS_TYPES
#pragma HLS PIPELINE II=1
#endif
                s += (int64_t)src0[b + i];
            }
            for (int k = 0; k < 8; ++k) {
                dst[c * 8 + k] = (data_t)((s >> (8 * k)) & 0xFF);
            }
        }
        break;
    }

    default:
        return PP_ERR_OP;
    }
    return PP_OK;
}
