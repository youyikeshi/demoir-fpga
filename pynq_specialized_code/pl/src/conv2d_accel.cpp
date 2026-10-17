// HLS 内核：通用二维卷积的硬件实现。
//
// 形状由编译期的 CIN / COUT / KSIZE / PAD / MAX_W 决定，默认值是
// encoder.conv_first（12→48，5×5，补边 2）。换层时用 -D 覆盖，见 conv2d_accel.h。
//
// 数据布局：输入和输出都是 NCHW，权重是 [oc][ic][ky][kx]。
// 所有 pragma 都用 HLS_PRAGMA 包起来，g++ 编译时整条丢掉。
//
// 执行流程：
//   1. 启动时把权重从 DDR 拷到片上缓冲
//   2. 逐输出行装 KSIZE 行到行缓冲，上下越界的行填 0，左右补零也在装载时一次做好
//   3. 逐输出像素遍历全部输出通道：核内并行乘加、加 bias、右移、ReLU、饱和

#include "conv2d_accel.h"

// 行缓冲的每行宽度：左右各补 PAD 个零，内层循环就不用再判断边界
#define LINE_W (MAX_W + 2 * PAD)

// 行缓冲需要装的行数。膨胀率为 d 时，窗口跨越 (K-1)*d+1 行输入。
#define SPAN ((KSIZE - 1) * DILATION + 1)

// 设计空间探索用的两个旋钮，可由 -D 覆盖
#ifndef UNROLL_K
#define UNROLL_K 25
#endif
#ifndef PIPELINE_II
#define PIPELINE_II 1
#endif

// 把核内并行度换算成两个维度的展开因子
#if UNROLL_K <= 1
#define UNROLL_KX 1
#define UNROLL_KY 1
#elif UNROLL_K <= 5
#define UNROLL_KX 5
#define UNROLL_KY 1
#else
#define UNROLL_KX 5
#define UNROLL_KY 5
#endif

namespace {

inline data_t saturate_int8(int32_t v) {
    if (v > 127) return (data_t)127;
    if (v < -128) return (data_t)(-128);
    return (data_t)v;
}

}  // namespace

int conv2d_accel(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
                 data_t *out_fm, int H, int W, int shift) {
    HLS_PRAGMA(HLS INTERFACE m_axi port=in_fm offset=slave bundle=gmem0)
    HLS_PRAGMA(HLS INTERFACE m_axi port=wgt offset=slave bundle=gmem1)
    HLS_PRAGMA(HLS INTERFACE m_axi port=bias offset=slave bundle=gmem2)
    HLS_PRAGMA(HLS INTERFACE m_axi port=out_fm offset=slave bundle=gmem3)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=H bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=W bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=shift bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=return bundle=ctrl)

    if (in_fm == 0 || wgt == 0 || bias == 0 || out_fm == 0) return ERR_NULL;
    if (H <= 0 || W <= 0 || W > MAX_W) return ERR_SIZE;
    if (shift < 1 || shift > 31) return ERR_SHIFT;

    // 片上权重缓冲。整个网络只有 14400 个权重，装得下。
    data_t wgt_buf[COUT][CIN][KSIZE][KSIZE];
    // 把核的两个维度拆开，保证一个周期能同时读出 25 个权重
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt_buf complete dim=3)
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt_buf complete dim=4)

    // 行缓冲：窗口跨越的所有输入行 × CIN 通道 × (宽度 + 左右补零)
    data_t line_buf[CIN][SPAN][LINE_W];
    HLS_PRAGMA(HLS BIND_STORAGE variable=line_buf type=RAM_2P impl=BRAM)

    for (int oc = 0; oc < COUT; ++oc) {
        for (int ic = 0; ic < CIN; ++ic) {
            for (int ky = 0; ky < KSIZE; ++ky) {
                for (int kx = 0; kx < KSIZE; ++kx) {
                    wgt_buf[oc][ic][ky][kx] =
                        wgt[((oc * CIN + ic) * KSIZE + ky) * KSIZE + kx];
                }
            }
        }
    }

    const int H_out = CONV_OUT(H);
    const int W_out = CONV_OUT(W);

    for (int y = 0; y < H_out; ++y) {
        // 装 SPAN 行到行缓冲。上下越界的行填 0，左右两端也填 0，
        // 这样内层循环里不再出现任何边界判断。
        // 注意：这里是整窗重装，不是滑动。滑动版试过，会让 II 从 1 退化到 5，
        // 原因见 notes/sliding_window_trial.md。
        for (int r = 0; r < SPAN; ++r) {
            const int iy = y * STRIDE + r - PAD;
            const bool row_ok = (iy >= 0) && (iy < H);
            for (int ic = 0; ic < CIN; ++ic) {
                // 拆成三段：左补零 / 中间无条件直拷 / 右补零。
                // 内层不再出现列边界判断，地址也只是简单递增，
                // 这样装载循环的关键路径才短得下来（实测原来是 6.816 ns）。
                for (int i = 0; i < PAD; ++i) line_buf[ic][r][i] = (data_t)0;

                if (row_ok) {
                    const int base = (ic * H + iy) * W;
                    for (int i = 0; i < W; ++i) {
                        line_buf[ic][r][PAD + i] = in_fm[base + i];
                    }
                } else {
                    for (int i = 0; i < W; ++i) {
                        line_buf[ic][r][PAD + i] = (data_t)0;
                    }
                }

                for (int i = 0; i < PAD; ++i) {
                    line_buf[ic][r][PAD + W + i] = (data_t)0;
                }
            }
        }

        for (int x = 0; x < W_out; ++x) {
            for (int oc = 0; oc < COUT; ++oc) {
                // 两个累加器交替使用。原因：单个 acc 每拍都要把上一拍的结果读回来
                // 再加新乘积再写回，这条反馈链约 4~5 ns，是限制频率的元凶
                // （实测关键路径：acc 累加 + 写回 + 复位选择器 + 再累加）。
                // 分成两组后每条链两拍更新一次，余量直接翻倍。
                acc_t acc_even = 0;
                acc_t acc_odd = 0;

                // 输入通道串行，核内并行
                for (int ic = 0; ic < CIN; ++ic) {
                    HLS_PRAGMA(HLS PIPELINE II=PIPELINE_II)
                    acc_t part = 0;
                    for (int ky = 0; ky < KSIZE; ++ky) {
                        HLS_PRAGMA(HLS UNROLL factor=UNROLL_KY)
                        for (int kx = 0; kx < KSIZE; ++kx) {
                            HLS_PRAGMA(HLS UNROLL factor=UNROLL_KX)
                            // 膨胀：第 ky 个抽头落在第 ky*DILATION 行，
                            // 第 kx 个抽头落在第 kx*DILATION 列
                            const int32_t p = (int32_t)line_buf[ic][ky * DILATION]
                                                          [x * STRIDE + kx * DILATION];
                            const int32_t k = (int32_t)wgt_buf[oc][ic][ky][kx];
                            part += p * k;
                        }
                    }
                    if (ic & 1) {
                        acc_odd += part;
                    } else {
                        acc_even += part;
                    }
                }

                const int32_t acc = (int32_t)acc_even + (int32_t)acc_odd;
                int32_t v = (int32_t)acc + (int32_t)bias[oc];
                v = v >> shift;
                if (v < 0) v = 0;

                out_fm[(oc * H_out + y) * W_out + x] = saturate_int8(v);
            }
        }
    }

    return ERR_OK;
}
