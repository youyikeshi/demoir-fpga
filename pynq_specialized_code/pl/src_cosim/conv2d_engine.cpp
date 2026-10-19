// 单引擎版卷积内核：一份电路覆盖全部卷积形态。
//
// 与 conv2d_accel.cpp 的关系：
//   - 骨架相同：行缓冲 + 权重缓冲 + 核内并行乘加 + 双累加器 + 后处理
//   - 区别一：CIN/COUT/K/STRIDE/DILATION/PAD 全部是运行期寄存器
//   - 区别二：输出按列分块（BLOCK 由 STRIDE 决定：s=1 时 64 列，s=2 时 32 列），
//             行缓冲只装当前块需要的列，不随整幅图宽增长
//   - 区别三：权重按 OC_BLOCK 个输出通道一组轮转装载，不再整层常驻
//   - v2（2026-10-15）：新增运行期 relu_en 位——=1 做 ReLU（默认，等价旧版）；
//     =0 线性输出（供 Ghost 残差投影与解码器输出层使用），饱和区间 [-128,127]
//
// 存储上界（编译期）：CIN_MAX/COUT_MAX/KMAX/SPAN_MAX/BWCOL_MAX，见 conv2d_engine.h。

#include "conv2d_engine.h"

namespace {

inline data_t saturate_int8(int32_t v) {
    if (v > 127) return (data_t)127;
    if (v < -128) return (data_t)(-128);
    return (data_t)v;
}

}  // namespace

int conv2d_engine(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
                  data_t *out_fm, int H, int W, int Hout, int Wout,
                  int cin, int cout, int ksize, int stride, int dil,
                  int pad, int shift, int relu_en) {
    // 注意：本副本仅供 C/RTL cosim 使用。depth= 只给仿真器用：
    // 包装层会按 depth 把缓冲区完整读出/写入测试向量，所以必须和
    // 当前测试数据集的真实大小精确一致（c352_o192_k1_h2：2×2 最小数据，
    // 用于"v3 大形状"的 cosim——仿真总时长压到 xsim 崩溃点以内）：
    //   in_fm  = 352*2*2     = 1408
    //   wgt    = 192*352*1*1 = 67584
    //   bias   = 192 个元素
    //   out_fm = 192*2*2     = 768
    // 换数据集跑 cosim 时要同步改这四个数。不影响综合电路。
    HLS_PRAGMA(HLS INTERFACE m_axi port=in_fm offset=slave bundle=gmem0 depth=1408)
    HLS_PRAGMA(HLS INTERFACE m_axi port=wgt offset=slave bundle=gmem0 depth=67584)
    HLS_PRAGMA(HLS INTERFACE m_axi port=bias offset=slave bundle=gmem0 depth=192)
    HLS_PRAGMA(HLS INTERFACE m_axi port=out_fm offset=slave bundle=gmem1 depth=768)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=H bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=W bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=Hout bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=Wout bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=cin bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=cout bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=ksize bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=stride bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=dil bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=pad bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=shift bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=relu_en bundle=ctrl)
    HLS_PRAGMA(HLS INTERFACE s_axilite port=return bundle=ctrl)

    if (in_fm == 0 || wgt == 0 || bias == 0 || out_fm == 0) return ERR_NULL;
    // H/W 上界 4096 是地址算术的安全上限：内部用 int32 计算
    // ic*H*W 等乘积，112*4096*4096 ≈ 1.9e9 < 2^31，不会溢出。
    if (H <= 0 || W <= 0 || H > 4096 || W > 4096) return ERR_SIZE;
    if (Hout <= 0 || Wout <= 0) return ERR_SIZE;
    // Hout/Wout 必须与卷积输出尺寸公式一致（same padding 下等价于
    // floor((n-1)/s)+1）。用乘法比较代替运行期除法；不一致直接报错，
    // 防止调用方传错尺寸导致输出越界写。
    if ((Hout - 1) * stride > H - 1) return ERR_SIZE;
    if (Hout * stride <= H - 1) return ERR_SIZE;
    if ((Wout - 1) * stride > W - 1) return ERR_SIZE;
    if (Wout * stride <= W - 1) return ERR_SIZE;
    if (cin < 1 || cin > CIN_MAX) return ERR_SIZE;
    if (cout < 1 || cout > COUT_MAX) return ERR_SIZE;
    if (ksize != 1 && ksize != 3 && ksize != 5) return ERR_SIZE;
    if (stride < 1 || stride > 2) return ERR_SIZE;
    if (dil < 1 || dil > DMAX) return ERR_SIZE;
    // same padding 检查：pad 必须等于 (k-1)*d/2
    if ((ksize - 1) * dil != 2 * pad) return ERR_SIZE;
    if (shift < 1 || shift > 31) return ERR_SHIFT;

    const int span = 2 * pad + 1;              // 窗口跨越的输入行数
    if (span > SPAN_MAX) return ERR_SIZE;
    // 每块的输出列数：s=1 时 64 列，s=2 时 32 列。
    // （更小的块做过实验：BRAM 会被 HLS 复制成 336 块，已否决。）
    const int bw = (stride == 2) ? 32 : 64;
    const int bwcol = (bw - 1) * stride + span + 2 * pad;
    if (bwcol > BWCOL_MAX) return ERR_SIZE;

    // 权重缓冲：只装当前 OC 块
    data_t wgt_buf[OC_BLOCK][CIN_MAX][KMAX][KMAX];
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt_buf complete dim=3)
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt_buf complete dim=4)

    // 行缓冲：只装当前输出块需要的列
    data_t line_buf[CIN_MAX][SPAN_MAX][BWCOL_MAX];
    HLS_PRAGMA(HLS BIND_STORAGE variable=line_buf type=RAM_2P impl=BRAM)
    // 把 7 个行拆成独立 bank：25 个抽头读点不再全部堵在同一块 RAM 上。
    // 2026-10-15 在 v3 尺寸下复测：拆分=165 块 BRAM；不拆反而 181 块，
    // factor=2/4 与 complete 同为 165——拆分是当前结构的最优选择。
    // （历史教训：块减半会让 RAM 被复制成 336 块。）
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=line_buf complete dim=2)

    // bias 很小，一次装全部
    bias_t bias_buf[COUT_MAX];

    for (int i = 0; i < cout; ++i) {
        bias_buf[i] = bias[i];
    }

    const int kk = ksize * ksize;
    const int hw = H * W;
    const int cin_kk = cin * kk;   // v3.1：权重装载地址递推的步长

    // 抽头偏移 kx*d 和 ky*d（d 是运行期值，只算一次）
    int rdoff[KMAX];
    for (int t = 0; t < KMAX; ++t) {
        rdoff[t] = t * dil;
    }

    for (int y = 0; y < Hout; ++y) {
        // v3.2（时序修复）：行起点 (y*stride - pad)*W 每行只算一次乘法；
        // r 级改用加法递推（+W），消除装载前置链里的"每行乘加"。
        const int y_row_base = (y * stride - pad) * W;
        // v3.2（时序修复）：块起点 gc0 也改为递推（x0 级加法），消除每块乘法。
        const int dgc = bw * stride;
        int gc0 = -pad;
        for (int x0 = 0; x0 < Wout; x0 += bw, gc0 += dgc) {
            const int xn = (Wout - x0 < bw) ? (Wout - x0) : bw;

            // 当前块的输入列范围 [gc0, gc0 + bwcol)，拆三段：左补零/直拷/右补零
            int la = (gc0 < 0) ? -gc0 : 0;
            if (la > bwcol) la = bwcol;
            int lb = W - gc0 - la;
            if (lb < 0) lb = 0;
            if (lb > bwcol - la) lb = bwcol - la;

            int io_r = y_row_base + gc0 + la;
            for (int r = 0; r < span; ++r, io_r += W) {
                const int iy = y * stride + r - pad;
                const bool row_ok = (iy >= 0) && (iy < H);
                // v3.1（时序修复）：输入地址由"每 ic 重算 ic*hw"改为常量递推，
                // 消掉装载循环里的 32 位乘加链（原为 14 级进位链的 WNS 边缘路径）。
                int io = io_r;
                for (int ic = 0; ic < cin; ++ic, io += hw) {
                    for (int j = 0; j < la; ++j) {
                        line_buf[ic][r][j] = (data_t)0;
                    }
                    if (row_ok) {
                        for (int j = 0; j < lb; ++j) {
                            line_buf[ic][r][la + j] = in_fm[io + j];
                        }
                    } else {
                        for (int j = 0; j < lb; ++j) {
                            line_buf[ic][r][la + j] = (data_t)0;
                        }
                    }
                    for (int j = la + lb; j < bwcol; ++j) {
                        line_buf[ic][r][j] = (data_t)0;
                    }
                }
            }

            // v3.1（时序修复）：权重基址改为按 ocb 块 / oc / ic 三级常量递推，
            // 消掉原 ((ocb+i)*cin+ic)*kk 的每迭代乘加链（WNS 边缘路径的源头）。
            int off_ocb = 0;
            for (int ocb = 0; ocb < cout; ocb += OC_BLOCK) {
                const int ocbl = (cout - ocb < OC_BLOCK) ? (cout - ocb) : OC_BLOCK;

                // 装当前 OC 块的权重。DDR 里每个 (oc,ic) 的 k×k 权重是连续的；
                // 先顺序读到寄存器组，再用静态索引摆到 wgt_buf 的 (ky,kx) 位置。
                // k=3 / k=5 分开写分支，避免运行期 k 造成的动态数组索引。
                // 注意：不要给这条循环加 HLS PIPELINE —— 试过 II=2，
                // HLS 会试图"展开再流水"，运行期循环次数展开失败后逻辑暴涨
                // （LUT 19.7k→69k、DSP 66→471），实现直接放不下。
                if (ksize == 1) {
                    // 1×1 的权重每个 (oc,ic) 只有 1 字节，单独做一条可流水的
                    // 连续读取；避免通用分支里运行期 k 造成的动态索引和长等待。
                    int off1 = off_ocb;
                    for (int i = 0; i < ocbl; ++i, off1 += cin_kk) {
                        HLS_PRAGMA(HLS PIPELINE II=1)
                        for (int ic = 0; ic < cin; ++ic) {
                            wgt_buf[i][ic][0][0] = wgt[off1 + ic];
                        }
                    }
                } else {
                    int off = off_ocb;
                    for (int i = 0; i < ocbl; ++i, off += cin_kk) {
                        int o2 = off;
                        for (int ic = 0; ic < cin; ++ic, o2 += kk) {
                            const data_t *wbase = wgt + o2;
                            if (ksize == 3) {
                            data_t wr[9];
                            for (int t = 0; t < 9; ++t) {
                                HLS_PRAGMA(HLS UNROLL)
                                wr[t] = wbase[t];
                            }
                            for (int ky = 0; ky < 3; ++ky) {
                                HLS_PRAGMA(HLS UNROLL)
                                for (int kx = 0; kx < 3; ++kx) {
                                    HLS_PRAGMA(HLS UNROLL)
                                    wgt_buf[i][ic][ky][kx] = wr[ky * 3 + kx];
                                }
                            }
                            } else if (ksize == 5) {
                            data_t wr[25];
                            for (int t = 0; t < 25; ++t) {
                                HLS_PRAGMA(HLS UNROLL)
                                wr[t] = wbase[t];
                            }
                            for (int ky = 0; ky < 5; ++ky) {
                                HLS_PRAGMA(HLS UNROLL)
                                for (int kx = 0; kx < 5; ++kx) {
                                    HLS_PRAGMA(HLS UNROLL)
                                    wgt_buf[i][ic][ky][kx] = wr[ky * 5 + kx];
                                }
                            }
                            } else {
                                wgt_buf[i][ic][0][0] = wbase[0];
                            }
                        }
                    }
                }
                off_ocb += cin_kk * OC_BLOCK;   // 下一 OC 块的起始偏移

                for (int x2 = 0; x2 < xn; ++x2) {
                    const int cb = x2 * stride;
                    for (int i = 0; i < OC_BLOCK; ++i) {
                        if (i < ocbl) {
                            acc_t acc_even = 0;
                            acc_t acc_odd = 0;

                            // ---- 1×1 专用快路径（2026-10-17）----
                            // 网络里 132/249 个卷积是 1×1，却和 5×5 走同一条 25 抽头
                            // 谓词通路：24/25 的乘法被 if 掉，但**拍数照付**。
                            // 单独一条"每 ic 一次乘加"的循环：循环携带依赖只剩 int32 加法，
                            // 宽画幅实测约 2.41 拍/单元（2×2 小画幅会被权重重装放大）。
                            // k=1 时 pad=0、span=1，line_buf 只有第 0 行，列下标就是 cb。
                            if (ksize == 1) {
                                for (int ic = 0; ic < cin; ++ic) {
                                    HLS_PRAGMA(HLS PIPELINE II=PIPELINE_II)
                                    const int32_t p = (int32_t)line_buf[ic][0][cb];
                                    const int32_t wv = (int32_t)wgt_buf[i][ic][0][0];
                                    const int32_t t1 = p * wv;
                                    if (ic & 1) {
                                        acc_odd += t1;
                                    } else {
                                        acc_even += t1;
                                    }
                                }
                            } else if (ksize == 3) {
                                // ---- 3×3 专用快路径（2026-10-19）----
                                // 通用路径按 KMAX=5 展开 25 个抽头，k=3 时仍有 16 个
                                // 谓词掉的乘法；这里只展开 9 个抽头。抽头结果分成两条
                                // 加法链再合并，缩短 3×3 的依赖链，整数求和顺序不影响结果。
                                for (int ic = 0; ic < cin; ++ic) {
                                    HLS_PRAGMA(HLS PIPELINE II=PIPELINE_II)
                                    acc_t part0 = 0;
                                    acc_t part1 = 0;
                                    for (int ky = 0; ky < 3; ++ky) {
                                        HLS_PRAGMA(HLS UNROLL)
                                        for (int kx = 0; kx < 3; ++kx) {
                                            HLS_PRAGMA(HLS UNROLL)
                                            const int32_t p = (int32_t)line_buf[ic]
                                                [rdoff[ky]][cb + rdoff[kx]];
                                            const int32_t wv = (int32_t)wgt_buf[i][ic][ky][kx];
                                            const int32_t prod = p * wv;
                                            if (((ky + kx) & 1) == 0) {
                                                part0 += prod;
                                            } else {
                                                part1 += prod;
                                            }
                                        }
                                    }
                                    const acc_t part = part0 + part1;
                                    if (ic & 1) {
                                        acc_odd += part;
                                    } else {
                                        acc_even += part;
                                    }
                                }
                            } else {
                                for (int ic = 0; ic < cin; ++ic) {
                                    HLS_PRAGMA(HLS PIPELINE II=PIPELINE_II)
                                    acc_t part = 0;
                                    for (int ky = 0; ky < KMAX; ++ky) {
                                        HLS_PRAGMA(HLS UNROLL)
                                        for (int kx = 0; kx < KMAX; ++kx) {
                                            HLS_PRAGMA(HLS UNROLL)
                                            if (ky < ksize && kx < ksize) {
                                                const int32_t p = (int32_t)line_buf[ic]
                                                    [rdoff[ky]][cb + rdoff[kx]];
                                                const int32_t wv = (int32_t)wgt_buf[i][ic][ky][kx];
                                                part += p * wv;
                                            }
                                        }
                                    }
                                    if (ic & 1) {
                                        acc_odd += part;
                                    } else {
                                        acc_even += part;
                                    }
                                }
                            }

                            const int32_t acc = (int32_t)acc_even + (int32_t)acc_odd;
                            int32_t v = acc + (int32_t)bias_buf[ocb + i];
                            v = v >> shift;
                            if (relu_en != 0 && v < 0) v = 0;   // v2：ReLU 可旁路

                            out_fm[((ocb + i) * Hout + y) * Wout + x0 + x2] =
                                saturate_int8(v);
                        }
                    }
                }
            }
        }
    }

    return ERR_OK;
}
