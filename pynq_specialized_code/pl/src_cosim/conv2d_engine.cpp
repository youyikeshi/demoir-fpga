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
    // 2026-10-21：显式指定突发长度与**在途请求数**。
    // 板端实测行缓冲填充只有 16 拍/字节（≈8 MB/s，而 HP0+DDR 能力 ≥500 MB/s），
    // 反推每搬一行（68 B）就要等一次完整往返、相邻行完全不重叠 —— 瓶颈是
    // 并发度而不是带宽。默认的 num_read_outstanding 太小，这里放大到 16。
    HLS_PRAGMA(HLS INTERFACE m_axi port=in_fm offset=slave bundle=gmem0 \
        max_read_burst_length=64 num_read_outstanding=16)
    HLS_PRAGMA(HLS INTERFACE m_axi port=wgt offset=slave bundle=gmem0 \
        max_read_burst_length=64 num_read_outstanding=16)
    HLS_PRAGMA(HLS INTERFACE m_axi port=bias offset=slave bundle=gmem0 \
        max_read_burst_length=64 num_read_outstanding=16)
    HLS_PRAGMA(HLS INTERFACE m_axi port=out_fm offset=slave bundle=gmem1 \
        max_write_burst_length=64 num_write_outstanding=16)
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
    // 2026-10-22：k=1 且 s=1 时用"宽列块"——把 SPAN_MAX 个 row-bank 当作
    // SPAN_MAX×64 列的一条宽行用（k=1 本来只用 r=0）：整行输入只需搬一次，
    // 不再被多个列块各搬一遍。目标与"宽读 64B/事务"相同（减少每条输入行的
    // 往返次数），但不动 AXI 位宽，因此没有"8 字节写同一 bank → HLS 复制存储器
    // → 布线拥塞"的问题（那次实测 397 个重叠节点）。零额外 BRAM。
    const bool wide1 = (K1_WIDE_COLS != 0) && (ksize == 1) && (stride == 1);
    const int bw = wide1 ? (SPAN_MAX * 64) : ((stride == 2) ? 32 : 64);
    const int bwcol = (bw - 1) * stride + span + 2 * pad;
    if (bwcol > (wide1 ? (SPAN_MAX * 64) : BWCOL_MAX)) return ERR_SIZE;

    // 权重缓冲：只装当前 OC 块
    data_t wgt_buf[OC_BLOCK][CIN_MAX][KMAX][KMAX];
    // 1×1 专用权重缓冲（2026-10-21 性能改造）：
    //   k=1 占 132/249 层，走 wgt_buf 时每个 oc 只能串行读（BRAM 双端口），
    //   这里单独放一份，并按输出通道完全分 bank（分布式 RAM，不占 BRAM），
    //   于是每拍可并行读 K1_OC_UNROLL 个权重 → 每拍 K1_OC_UNROLL 个 MAC。
    data_t wgt1_buf[OC_BLOCK][CIN_MAX];
    // 3×3 专用权重缓冲（2026-10-22）：按 oc 完全分 bank，每拍可并行读 8 个 oc。
    // 目的：把 oc 从"串行 8 个"改成"内层展开"，消除每个 oc 重开一次 ic 流水的
    // 气泡（板端实测 k=3 是 6.69 拍/单元，微基准 4.01）。
    // 8×352×9 = 25.3 KB，放 LUTRAM（约 +3.2k LUT），不动已经很紧的 BRAM。
    data_t wgt3_buf[OC_BLOCK][CIN_MAX][3][3];
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt_buf complete dim=3)
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt_buf complete dim=4)
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt1_buf complete dim=1)
    HLS_PRAGMA(HLS BIND_STORAGE variable=wgt1_buf type=RAM_2P impl=LUTRAM)
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt3_buf complete dim=1)
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt3_buf complete dim=3)
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt3_buf complete dim=4)
    HLS_PRAGMA(HLS BIND_STORAGE variable=wgt3_buf type=RAM_2P impl=LUTRAM)

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

    // ---- 2026-10-22：列块外层、行内层 + 行缓冲复用 ----
    // 原结构是"行外层、列块内层"，所以每个 (行, 列块) 都要把 span 行输入整块
    // 从 DDR 重搬一遍（k=3 时 3 行）。板端实测填充项 105 s，占 conv 的 41%。
    // 交换顺序后，同一列块内相邻输出行共享 span-stride 行：只在 BRAM 内下移
    // 这些行，从 DDR 只装 stride 行（k=3、s=1：装 1 行，省 2/3）。
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

        bool row_buf_valid = false;      // 本列块的第一行需要整块装载
        for (int y = 0; y < Hout; ++y) {
            const int need0 = y * stride - pad;      // 本行需要的最上面那条输入行
            const int io_base = need0 * W + gc0 + la;
            // 复用：BRAM 内下移 span-stride 行；DDR 只装剩下的 stride 行
            const int keep = (span > stride) ? (span - stride) : 0;
            if (row_buf_valid && keep > 0) {
                for (int r = 0; r < keep; ++r) {
                    for (int ic = 0; ic < cin; ++ic) {
                        for (int j = 0; j < bwcol; ++j) {
                            line_buf[ic][r][j] = line_buf[ic][r + stride][j];
                        }
                    }
                }
            }
            const int r_begin = (row_buf_valid && keep > 0) ? keep : 0;
            for (int r = r_begin; r < span; ++r) {
                const int iy = need0 + r;
                const bool row_ok = (iy >= 0) && (iy < H);
                const int io_row = io_base + r * W;
                for (int ic = 0; ic < cin; ++ic) {
                    // 行缓冲写入统一入口：wide1 时"bank 当列空间"，其余路径不变。
                    // 注意三处补零也必须走同一映射，否则会写到 [76] 维度之外。
                    for (int j = 0; j < la; ++j) {
                        if (wide1) {
                            line_buf[ic][j >> 6][j & 63] = (data_t)0;
                        } else {
                            line_buf[ic][r][j] = (data_t)0;
                        }
                    }
                    if (row_ok) {
                        const int io = io_row + ic * hw;
                        HLS_PRAGMA(HLS PIPELINE II=1)
                        for (int j = 0; j < lb; ++j) {
                            if (wide1) {
                                line_buf[ic][(la + j) >> 6][(la + j) & 63] = in_fm[io + j];
                            } else {
                                line_buf[ic][r][la + j] = in_fm[io + j];
                            }
                        }
                    } else {
                        for (int j = 0; j < lb; ++j) {
                            if (wide1) {
                                line_buf[ic][(la + j) >> 6][(la + j) & 63] = (data_t)0;
                            } else {
                                line_buf[ic][r][la + j] = (data_t)0;
                            }
                        }
                    }
                    // 尾部补零：wide1 时**不需要**——k=1 的 pad=0，计算只会读
                    // 到 x2 < xn == lb，lb 之后的列永远不会被读。若照旧补到
                    // bwcol(=448)，小图（如 16×16）会白写 432 个零，实测把
                    // 该用例从 122.7 ms 拖到 148.8 ms。
                    if (!wide1) {
                        for (int j = la + lb; j < bwcol; ++j) {
                            line_buf[ic][r][j] = (data_t)0;
                        }
                    }
                }
            }
            row_buf_valid = true;

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
                                HLS_PRAGMA(HLS PIPELINE II=1)
                                wgt1_buf[i][ic] = wgt[off1 + ic];
                            }
                        }
                    } else if (ksize == 3) {
                        // 【2026-10-21】"142 拍/字节"的主因在这一条循环：
                        //   ① 步长 kk 是**运行期值** → HLS 无法做跨迭代仿射分析，
                        //      9 个连续读被当成 9 个独立标量事务；
                        //   ② 循环**没有流水** → 每次迭代都要等上一批 9 字节的
                        //      AXI 返回才能开始下一次。
                        // 改法：按 k 拆分支把步长变成编译期常量 9，指针递推 +
                        // II=1，让 9 个读被预取。这里不涉及任何谓词，访问仍是
                        // 连续常量偏移，因此**不会**像"填充循环"那样破坏 burst。
                        int off = off_ocb;
                        for (int i = 0; i < ocbl; ++i, off += cin_kk) {
                            const data_t *wp = wgt + off;
                            HLS_PRAGMA(HLS PIPELINE II=1)
                            for (int ic = 0; ic < cin; ++ic, wp += 9) {
                                HLS_PRAGMA(HLS UNROLL)
                                for (int ky = 0; ky < 3; ++ky) {
                                    HLS_PRAGMA(HLS UNROLL)
                                    for (int kx = 0; kx < 3; ++kx) {
                                        wgt3_buf[i][ic][ky][kx] = wp[ky * 3 + kx];
                                    }
                                }
                            }
                        }
                    } else {   // ksize == 5（ksize 已被上界校验限制为 1/3/5）
                        int off = off_ocb;
                        for (int i = 0; i < ocbl; ++i, off += cin_kk) {
                            const data_t *wp = wgt + off;
                            HLS_PRAGMA(HLS PIPELINE II=1)
                            for (int ic = 0; ic < cin; ++ic, wp += 25) {
                                HLS_PRAGMA(HLS UNROLL)
                                for (int ky = 0; ky < 5; ++ky) {
                                    HLS_PRAGMA(HLS UNROLL)
                                    for (int kx = 0; kx < 5; ++kx) {
                                        wgt_buf[i][ic][ky][kx] = wp[ky * 5 + kx];
                                    }
                                }
                            }
                        }
                    }
                    off_ocb += cin_kk * OC_BLOCK;   // 下一 OC 块的起始偏移

                    for (int x2 = 0; x2 < xn; ++x2) {
                        const int cb = x2 * stride;

                        // ---- 2026-10-21 性能改造（Stage 1）：1×1 快路径重排 ----
                        // 原结构 oc 在外层串行：每个 (像素, 输出通道) 都重启一次内层
                        // II=1 流水，实测约 2.4 拍/MAC。改为：
                        //   · ic 在外层流水（每拍只读一次 line_buf）；
                        //   · oc 在内层按 K1_OC_UNROLL 展开，累加器全展开成寄存器；
                        //   · 8 个 oc 一次性参与流水，不存在 per-oc 重启气泡。
                        // acc0/acc1 仍按 ic 奇偶分两条加法链（缩短依赖）；整数加法
                        // 可交换，求和顺序变化不影响结果（g++/csim/cosim 三重对拍）。
                        if (ksize == 1) {
                            acc_t acc0[OC_BLOCK];
                            acc_t acc1[OC_BLOCK];
                            HLS_PRAGMA(HLS ARRAY_PARTITION variable=acc0 complete dim=1)
                            HLS_PRAGMA(HLS ARRAY_PARTITION variable=acc1 complete dim=1)
                            for (int i = 0; i < OC_BLOCK; ++i) {
                                HLS_PRAGMA(HLS UNROLL)
                                acc0[i] = 0;
                                acc1[i] = 0;
                            }
                            for (int ic = 0; ic < cin; ++ic) {
                                HLS_PRAGMA(HLS PIPELINE II=PIPELINE_II)
                                const int32_t p = wide1
                                    ? (int32_t)line_buf[ic][cb >> 6][cb & 63]
                                    : (int32_t)line_buf[ic][0][cb];
                                for (int i = 0; i < OC_BLOCK; ++i) {
                                    HLS_PRAGMA(HLS UNROLL factor=K1_OC_UNROLL)
                                    const int32_t t1 = p * (int32_t)wgt1_buf[i][ic];
                                    if (ic & 1) {
                                        acc1[i] += (acc_t)t1;
                                    } else {
                                        acc0[i] += (acc_t)t1;
                                    }
                                }
                            }
                            for (int i = 0; i < ocbl; ++i) {
                                const int32_t acc = (int32_t)acc0[i] + (int32_t)acc1[i];
                                int32_t v = acc + (int32_t)bias_buf[ocb + i];
                                v = v >> shift;
                                if (relu_en != 0 && v < 0) v = 0;
                                out_fm[((ocb + i) * Hout + y) * Wout + x0 + x2] =
                                    saturate_int8(v);
                            }

                        } else if (ksize == 3) {
                            // ---- 3×3 快路径重排（2026-10-22）：ic 外层流水 + oc 展开 ----
                            // 原结构 oc 在外层串行 → 每算一个输出通道就重开一次 ic 流水，
                            // 板端 6.69 拍/单元（微基准 4.01），重启开销占大头。
                            // 现在：8 个累加器全展开成寄存器；ic 外层流水；9 个抽头每 ic 只读
                            // 一次、8 个 oc 复用；权重从 wgt3_buf（每 oc 独立 bank）并行读。
                            acc_t acc0[OC_BLOCK];
                            acc_t acc1[OC_BLOCK];
                            HLS_PRAGMA(HLS ARRAY_PARTITION variable=acc0 complete dim=1)
                            HLS_PRAGMA(HLS ARRAY_PARTITION variable=acc1 complete dim=1)
                            for (int i = 0; i < OC_BLOCK; ++i) {
                                HLS_PRAGMA(HLS UNROLL)
                                acc0[i] = 0;
                                acc1[i] = 0;
                            }
                            for (int ic = 0; ic < cin; ++ic) {
                                HLS_PRAGMA(HLS PIPELINE II=PIPELINE_II)
                                const int32_t q0 = (int32_t)line_buf[ic][rdoff[0]][cb + rdoff[0]];
                                const int32_t q1 = (int32_t)line_buf[ic][rdoff[0]][cb + rdoff[1]];
                                const int32_t q2 = (int32_t)line_buf[ic][rdoff[0]][cb + rdoff[2]];
                                const int32_t q3 = (int32_t)line_buf[ic][rdoff[1]][cb + rdoff[0]];
                                const int32_t q4 = (int32_t)line_buf[ic][rdoff[1]][cb + rdoff[1]];
                                const int32_t q5 = (int32_t)line_buf[ic][rdoff[1]][cb + rdoff[2]];
                                const int32_t q6 = (int32_t)line_buf[ic][rdoff[2]][cb + rdoff[0]];
                                const int32_t q7 = (int32_t)line_buf[ic][rdoff[2]][cb + rdoff[1]];
                                const int32_t q8 = (int32_t)line_buf[ic][rdoff[2]][cb + rdoff[2]];
                                int32_t h0[OC_BLOCK];
                                int32_t h1[OC_BLOCK];
                                HLS_PRAGMA(HLS ARRAY_PARTITION variable=h0 complete dim=1)
                                HLS_PRAGMA(HLS ARRAY_PARTITION variable=h1 complete dim=1)
                                for (int i = 0; i < OC_BLOCK; ++i) {
                                    HLS_PRAGMA(HLS UNROLL factor=K3_OC_UNROLL)
                                    h0[i] = q0 * (int32_t)wgt3_buf[i][ic][0][0]
                                          + q2 * (int32_t)wgt3_buf[i][ic][0][2]
                                          + q4 * (int32_t)wgt3_buf[i][ic][1][1]
                                          + q6 * (int32_t)wgt3_buf[i][ic][2][0]
                                          + q8 * (int32_t)wgt3_buf[i][ic][2][2];
                                    h1[i] = q1 * (int32_t)wgt3_buf[i][ic][0][1]
                                          + q3 * (int32_t)wgt3_buf[i][ic][1][0]
                                          + q5 * (int32_t)wgt3_buf[i][ic][1][2]
                                          + q7 * (int32_t)wgt3_buf[i][ic][2][1];
                                }
                                for (int i = 0; i < OC_BLOCK; ++i) {
                                    HLS_PRAGMA(HLS UNROLL)
                                    if (ic & 1) {
                                        acc1[i] += (acc_t)(h0[i] + h1[i]);
                                    } else {
                                        acc0[i] += (acc_t)(h0[i] + h1[i]);
                                    }
                                }
                            }
                            for (int i = 0; i < ocbl; ++i) {
                                const int32_t acc = (int32_t)acc0[i] + (int32_t)acc1[i];
                                int32_t v = acc + (int32_t)bias_buf[ocb + i];
                                v = v >> shift;
                                if (relu_en != 0 && v < 0) v = 0;
                                out_fm[((ocb + i) * Hout + y) * Wout + x0 + x2] = saturate_int8(v);
                            }
                        } else {
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
                        }   // end else (ksize != 1)
                    }
                }
            }
        }


    return ERR_OK;
}
