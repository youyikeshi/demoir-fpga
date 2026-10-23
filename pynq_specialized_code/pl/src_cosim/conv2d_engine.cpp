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

// ---- 2026-10-23 第三轮：DDR 访问加宽的公共位宽类型 ----
// 动机见 docs/设计/记录_20261023_conv相位拆解与DDR窄访问根因.md：
// HLS 只为 `data_t*`（ap_int<8>）指针发 1 字节的 AXI 请求（ARLEN=1），
// 一个 beat 只用 1/4 带宽。这里把指针按 32 bit 重新解释，让每笔请求带 4 字节。
// g++ 侧没有 ap_uint，用原生整数顶上，保证同一份代码两个环境都能编。
#ifdef USE_HLS_TYPES
typedef ap_uint<32> w32_t;
typedef ap_uint<64> w64_t;
#define W32_BYTE(v, n) ((data_t)(v).range(8 * (n) + 7, 8 * (n)))    /* n 必须是常量 */
#else
typedef uint32_t w32_t;
typedef uint64_t w64_t;
#define W32_BYTE(v, n) ((data_t)(uint8_t)(((v) >> (8 * (n))) & 0xFFu))
#endif

// k=1 快路径一拍覆盖几个 OC 块（1 = 8 车道，2 = 16 车道，见 conv2d_engine.h 的 K1_LANES）
#define K1_BLK (K1_LANES / OC_BLOCK)
#if (K1_BLK * OC_BLOCK) != K1_LANES
#error "K1_LANES 必须是 OC_BLOCK 的整数倍"
#endif

// 输出地址（2026-10-23，**编译期**决定，无运行期选路）。
// OC_BLOCK 按 8 对齐轮转 ⇒ 同一像素的 8 个 oc 在 C8 布局里正好是**连续 8 字节**：
//   addr(oc,y,x) = (oc/8)*Hout*Wout*8 + y*Wout*8 + x*8 + (oc%8)
// = (((ocb/8)*Hout + y)*Wout + x)*8 + i      （ocb 是 8 的倍数，i = oc%8 ∈ [0,8)）
// 写回仍然按 i 逐字节写 8 次（**不要**改成 64 位宽写：2026-10-21 的"读宽写窄"
// 实验里，宽写诱使 HLS 复制存储器 → 397 个重叠节点 → 布线崩）。
//
// ⚠️ 2026-10-23 第二轮：布局**不做运行期选路**。
// 上一版把 c8_in/c8_out 做成 relu_en 的运行期位，写回循环里多一次 select
// （即使提到循环外也要复制一份后处理），代价是 cosim 339,120 → 340,500 拍、
// 板上 conv 129.8 → 139.6 s（+7.5%）—— C8 的收益全被吃掉。
// 现在：布局由 C8_LAYOUT 在**编译期**定死，本宏就是取值方式，
// 数据通路（填充/写回）里没有任何 mux；运行期只剩一次"声明位"校验
// （ERR_LAYOUT，每层一次、不在任何循环里）。
#if C8_LAYOUT
#define OUT_ADDR(ocb_, i_, y_, x_) \
    ((((ocb_ >> 3) * Hout + y_) * Wout + x_) * C8_CH + i_)
#else
#define OUT_ADDR(ocb_, i_, y_, x_) (((ocb_ + i_) * Hout + y_) * Wout + x_)
#endif

// ---- 写回落点（2026-10-23 第四轮：改成"行缓冲 + 独立拷贝循环"）----
// C8：写回只落本行块的行缓冲 wbuf，等 x2 循环跑完再由一条**纯拷贝循环**
// 按 64-bit 宽写整行出去（见 out_copy）。CHW：保持原来的逐字节直写。
// 形参约定：i_ = 块内 oc 号（0..7），x2_ = 块内列号；
// 隐式依赖同作用域的 v8 / ocb / y / x0 / wbuf（三条写回路径共用）。
#if C8_LAYOUT
#define WB_STORE(i_, x2_) (wbuf[(i_)][(x2_)] = v8)
#else
#define WB_STORE(i_, x2_) (out_fm[OUT_ADDR(ocb, (i_), y, x0 + (x2_))] = v8)
#endif

int conv2d_engine(const data_t *in_fm, const data_t *wgt, const bias_t *bias,
                  data_t *out_fm, int H, int W, int Hout, int Wout,
                  int cin, int cout, int ksize, int stride, int dil,
                  int pad, int shift, int relu_en,
               const data_t *out_lut, int32_t *chsum) {
    // 2026-10-21：显式指定突发长度与**在途请求数**。
    // 板端实测行缓冲填充只有 16 拍/字节（≈8 MB/s，而 HP0+DDR 能力 ≥500 MB/s），
    // 反推每搬一行（68 B）就要等一次完整往返、相邻行完全不重叠 —— 瓶颈是
    // 并发度而不是带宽。默认的 num_read_outstanding 太小，这里放大到 16。
    // 2026-10-23：突发长度改成宏。C8 布局下一笔逻辑 burst 是 8*lb ≤ 608 B
    // （32 位数据宽度 = 152 拍），必须允许到 256 拍才能合成一笔；CHW 下仍是 64
    // （68 B = 17 拍），保证 C8_LAYOUT=0 的 RTL 逐字不变。
    // 注意：本副本仅供 cosim —— 只比 src/ 多 m_axi 的 depth=（见 check_cosim_dims.py）
    HLS_PRAGMA(HLS INTERFACE m_axi port=in_fm offset=slave bundle=gmem0 \
        max_read_burst_length=RD_BURST_MAX num_read_outstanding=16 depth=20480)
    HLS_PRAGMA(HLS INTERFACE m_axi port=wgt offset=slave bundle=gmem0 \
        max_read_burst_length=RD_BURST_MAX num_read_outstanding=16 depth=6144)
    HLS_PRAGMA(HLS INTERFACE m_axi port=bias offset=slave bundle=gmem0 \
        max_read_burst_length=RD_BURST_MAX num_read_outstanding=16 depth=16)
    HLS_PRAGMA(HLS INTERFACE m_axi port=out_fm offset=slave bundle=gmem1 \
        max_write_burst_length=WR_BURST_MAX num_write_outstanding=16 depth=10240)
    // 融合出口。**必须**声明 m_axi + depth：否则 HLS 推断成 ap_none，
    // 那两个指针就不是 AXI-Lite 寄存器，PS 写不进地址（2026-10-22 已踩两次）。
    // 两个端口**分别**按各自的开关声明 —— chsum 属于 FUSE_SUM，别绑在 FUSE_OUT 上。
#if FUSE_OUT
    HLS_PRAGMA(HLS INTERFACE m_axi port=out_lut offset=slave bundle=gmem0 depth=256)
#endif
#if FUSE_SUM
    HLS_PRAGMA(HLS INTERFACE m_axi port=chsum offset=slave bundle=gmem0 depth=4096)
#endif
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

    // ---- 2026-10-23：relu_en 的位语义（**不是布尔**）----
    //   bit0 = ReLU。
    //   bit2/bit3（只有 C8_LAYOUT=1 的构建看）**不是布局选路位**，而是调用方的
    //   "声明位"：PS 说"我的输入/输出缓冲区是按 ceil(C/8)*8 通道分配的 C8 缓冲"。
    //   编译期 C8 的构建没有 CHW 通路可退，声明不符就报 ERR_LAYOUT，
    //   而不是把 CHW 数据当 C8 读（那样会算出一堆"看起来正常"的垃圾，
    //   板上表现为整网精度崩但引擎不报错 —— 最难查的一类）。
    const int relu_on = (relu_en >> RELU_EN_RELU_BIT) & 1;
#if C8_LAYOUT
    if ((relu_en & ((1 << RELU_EN_C8IN_BIT) | (1 << RELU_EN_C8OUT_BIT)))
        != ((1 << RELU_EN_C8IN_BIT) | (1 << RELU_EN_C8OUT_BIT))) {
        return ERR_LAYOUT;
    }
#endif

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
    //   于是每拍可并行读 OC_BLOCK*K1_BLK 个权重 → 每拍同样多个 MAC（第十轮 16 车道）。
    data_t wgt1_buf[OC_BLOCK * K1_BLK][CIN_MAX];
    // 3×3 专用权重缓冲（2026-10-22）：按 oc 完全分 bank，每拍可并行读 8 个 oc。
    // 目的：把 oc 从"串行 8 个"改成"内层展开"，消除每个 oc 重开一次 ic 流水的
    // 气泡（板端实测 k=3 是 6.69 拍/单元，微基准 4.01）。
    // 8×352×9 = 25.3 KB，放 LUTRAM（约 +3.2k LUT），不动已经很紧的 BRAM。
    // 2026-10-23 第三轮：k=3 权重装载的宽读**试过又退回**（记录在此，别再试）：
    //   要宽读就得让"一个 oc 的 cin*9 字节连续"，最自然的写法是把
    //   wgt3_buf 拍平成 [oc][cin*9]。结果：装载确实能宽读了，但**读侧垮了** ——
    //   计算时每个 ic 的 9 个抽头原来靠 (ky,kx) 各占一个 bank（8*3*3=72 个
    //   LUTRAM bank，每个抽头一个读口），拍平后 9 个抽头挤在同一 bank 上，
    //   8 路 oc 展开 × 9 抽头 ⇒ 一个 bank 要 9 个读口。csynth：DSP 111→119、
    //   FF 24,956→28,595、LUT 38,056→42,464（82%→91%），不能上板。
    //   正解（第四轮已落地）：容器布局**不动**，只把装载循环按 4 个 ic 分组 ——
    //   4×9 = 36 字节 = 9 个 32-bit 字，且每组起点恒 4 字节对齐。这样 DDR 侧
    //   变成"每 36 字节一笔 burst"，而写侧仍是 9 个**常量** bank ⇒ 读侧、资源、
    //   权重包、参考实现全都不变。见 `#if K3_WIDE_LOAD` 那段。
    //   （同时试过的"按 (ky,kx) 拆 9 个平面"导出布局更差：LUT 38,235、DSP 111、
    //   cosim 反而涨到 458,786 拍 —— 因为写 bank 由两个运行期下标决定，HLS 拒绝
    //   把它做成流水。已弃用。）
    // 形状 [oc][ic][9]：第三维就是 (ky,kx) 展平后的**平面号** p = ky*3+kx，
    // complete 拆分后是 8×9 = 72 个 LUTRAM bank（每个 bank 深度 CIN_MAX），
    // 与改造前逐字等价（[3][3] → [9] 只是展平了下标）。
    data_t wgt3_buf[OC_BLOCK][CIN_MAX][9];
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt_buf complete dim=3)
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt_buf complete dim=4)
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt1_buf complete dim=1)
    HLS_PRAGMA(HLS BIND_STORAGE variable=wgt1_buf type=RAM_2P impl=LUTRAM)
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt3_buf complete dim=1)
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wgt3_buf complete dim=3)
    HLS_PRAGMA(HLS BIND_STORAGE variable=wgt3_buf type=RAM_2P impl=LUTRAM)

    // 行缓冲：只装当前输出块需要的列
    data_t line_buf[CIN_MAX][SPAN_MAX][BWCOL_MAX];
    HLS_PRAGMA(HLS BIND_STORAGE variable=line_buf type=RAM_2P impl=BRAM)
    // 把 7 个行拆成独立 bank：25 个抽头读点不再全部堵在同一块 RAM 上。
    // 2026-10-15 在 v3 尺寸下复测：拆分=165 块 BRAM；不拆反而 181 块，
    // factor=2/4 与 complete 同为 165——拆分是当前结构的最优选择。
    // （历史教训：块减半会让 RAM 被复制成 336 块。）
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=line_buf complete dim=2)
#if C8_LAYOUT
    // 2026-10-23 第三轮（实测结论，别再试）：**不要**给 dim1 加 cyclic 拆分。
    // 试过 `cyclic factor=4 dim=1`，让填充一拍并行写 4 个 bank（II=1）。
    // 功能完全正确、BRAM 一点不涨（仍是 172），但 csynth LUT 从 35,361 涨到
    // 42,920（76% → 92%）、FF +1,438：28 个 bank 之后，**每一处** line_buf 读写
    // 都要生成 28 份"地址 + 片选"，而 line_buf 的读写点在三条计算路径里一共
    // 40 来个 ⇒ 光 mux/译码就是 +5k LUT。当前 impl 已 80.66%，这条路不能走。
    // 现在的取舍：填充"每拍写 1 字节、每 4 拍读 1 个 32-bit 字"（II=4），
    // 事务宽度一样是 4 字节（ARLEN 1→4，这才是板端收益的来源），
    // 而 line_buf 保持原样 ⇒ 资源逐项不变。
#endif

    // bias 很小，一次装全部
    bias_t bias_buf[COUT_MAX];

#if C8_LAYOUT
    // 写回行缓冲（2026-10-23 第四轮）。8 个 oc 落在 8 个**编译期常量** bank 上
    // （dim1 完全拆分），于是"计算时逐字节写、拷贝时一拍读 8 个 bank 拼成一个
    // 64-bit 字"两侧都无需运行期 bank 号 —— 这是本轮不碰 bank 常量化原则的关键。
    // 宽度：k=1 的宽列块把 line_buf 的 SPAN_MAX 个 row-bank 当 SPAN_MAX*64 列用，
    // 所以 xn 最大是 448；其余路径 xn ≤ 64。8×448 = 3.5 KB，放 LUTRAM
    // （约 +450~900 LUT），不动已经 90% 的 BRAM。
    data_t wbuf[OC_BLOCK * K1_BLK][SPAN_MAX * 64];
    HLS_PRAGMA(HLS ARRAY_PARTITION variable=wbuf complete dim=1)
    HLS_PRAGMA(HLS BIND_STORAGE variable=wbuf type=RAM_2P impl=LUTRAM)
#endif

    for (int i = 0; i < cout; ++i) {
        bias_buf[i] = bias[i];
    }

    const int kk = ksize * ksize;
    const int hw = H * W;
    const int cin_kk = cin * kk;   // v3.1：权重装载地址递推的步长
#if C8_LAYOUT
    const int hw8 = hw * C8_CH;    // C8：一个通道块的行跨度（H*W*8）
#endif

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
#if C8_LAYOUT
            // ---- 2026-10-23：C8 交错布局的填充 ----
            //   addr(c,y,x) = (c/8)*H*W*8 + y*W*8 + x*8 + (c%8)
            // 同一个 (输入行 iy, 列块, 通道块) 的 8 个通道在 DDR 里是**连续
            // 8 字节**，整个「8 通道 × lb 列」= 连续 8*lb 字节 ⇒ **一笔长 burst**
            // （CHW 下是每通道一笔 68 B，共 cin 笔 ⇒ 这里是 ceil(cin/8) 笔）。
            // 关键：把 (列 j, 块内通道 ic) 合并成一条**线性**下标 t = j*8 + ic，
            // 即 ic 必须在内层。若保持 ic 外层、j 内层，就退化成 8 次跨步 8 的
            // 散读，比 CHW 还差（见 设计_激活tiled布局_C8交错.md §1、
            // 记录_20261023_C8软件侧实现与逐位验证.md §7 坑 7）。
            for (int r = r_begin; r < span; ++r) {
                const int iy = need0 + r;
                const bool row_ok = (iy >= 0) && (iy < H);
                const int io_row = io_base + r * W;
                // 左/右补零与通道无关（不碰 DDR），先在通道维度上补干净。
                // 2026-10-23 第四轮：**wide1（k=1 s=1）不需要右补零** —— 计算只读
                // 到 cb = x2 < xn == lb，[lb, bwcol) 这 128 列（448-320）一次都
                // 不会被读。原来这里每行每通道白写 (bwcol-lb) 个零：全网络合计
                // 624.7 MB ≈ 4.2 s（150 MHz、1 字节/拍），比有效输入（52 MB）还多
                // 一个数量级。CHW 分支早就有这个 `if (!wide1)`，C8 分支漏了。
                for (int ic = 0; ic < cin; ++ic) {
                    for (int j = 0; j < la; ++j) {
                        line_buf[ic][r][j] = (data_t)0;
                    }
                    if (!wide1) {
                        for (int j = la + lb; j < bwcol; ++j) {
                            line_buf[ic][r][j] = (data_t)0;
                        }
                    }
                }
                for (int cinb = 0; cinb < cin; cinb += C8_CH) {
                    // 基址用递推量拼：io_row*8 已经含了 (gc0+la)*8，
                    // 通道块之间的偏移是 (cinb/8)*H*W*8（hw8）；循环体内不做乘加。
                    const int iob = io_row * C8_CH + (cinb >> 3) * hw8;
                    // 最后一个通道块若 cin%8 != 0 **仍然读满 8 个通道**：补零区读出
                    // 的是 0（调用方按 ceil(C/8)*H*W*8 分配缓冲区并清零），而乘加
                    // 只跑 ic < cin，所以补零通道永远不参与计算。好处是填充路径只有
                    // 一种形状，HLS 容易把 8*lb 个连续读折成一笔 burst。
                    //
                    // 2026-10-23 第二轮起 cin%8 != 0 成了**正常**输入而不是例外：
                    // PS 侧把所有 int8 张量都补零到 8 的倍数（RGB 3ch -> 8ch、
                    // pixel_shuffle 边界 12ch -> 16ch），全网络只剩一种布局，
                    // 编译期 C8 就够用，不再需要运行期布局位。补齐的通道：
                    //   · 输入侧 —— 缓冲区尾部 4/5 个通道恒为 0；权重侧**不重新
                    //     打包**（cin 上界仍是 12，乘加只跑 ic < 12），零打包改动；
                    //   · 输出侧 —— 引擎只写 cout 个通道，尾部补零通道保持 0，
                    //     下游按逻辑通道数（12）读前 12 个。
                    const int nwide = lb * C8_CH;
                    // ---- 2026-10-23 第三轮：填充改成 32-bit 读 ----
                    // 原来 `in_fm[iob + t]` 每次只读 1 字节 ⇒ RTL 里
                    // `m_axi_gmem0_0_ARLEN = 64'd1`（长度按**字节**算），
                    // 一个 32 位 beat 只搬 1 个有效字节，板端 10.5 拍/字节。
                    // C8 布局下连续 4 字节 = 同一像素的 4 个通道，所以按
                    // `ap_uint<32>*` 读：每笔请求 4 字节 ⇒ 事务数 ÷4。
                    // 对齐前提（两条都恒成立，已逐项核对）：
                    //   · iob = io_row*8 + (cinb>>3)*H*W*8 → 恒为 8 的倍数；
                    //   · lb*8 恒为 4 的倍数（其实恒为 8 的倍数）。
                    //
                    // 写侧**必须保持"每拍 1 字节"**：line_buf 只按行（dim2）
                    // 分了 7 个 bank，同一拍写 4 个不同 c 会撞同一个 bank 的
                    // 单写口，HLS 就会把 line_buf 按通道再拆成 28 个 bank ——
                    // 实测 LUT 35,361 → 40,996（+5.6k，76%→88%），impl 放不下。
                    // 所以这里用"循环携带寄存器"：每 4 拍读进一个 32-bit 字
                    // （`hold`），其余 3 拍从它里面取字节，一拍只写 1 个字节。
                    // 循环形态与改造前完全相同（1 写/拍、II=1），只有 DDR 那次
                    // 读变宽 —— 资源逐项不变。
                    if (row_ok) {
                        const w32_t *p32 =
                            reinterpret_cast<const w32_t *>(in_fm + iob);
                        w32_t hold = 0;
                        for (int t = 0; t < nwide; ++t) {
                            if ((t & 3) == 0) {
                                hold = p32[t >> 2];
                            }
                            line_buf[cinb + (t & (C8_CH - 1))][r][la + (t >> 3)] =
                                (data_t)(hold >> ((t & 3) << 3));
                        }
                    } else {
                        for (int t = 0; t < nwide; ++t) {
                            line_buf[cinb + (t & (C8_CH - 1))][r][la + (t >> 3)] =
                                (data_t)0;
                        }
                    }
                }
            }
#else
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
#endif
            row_buf_valid = true;

                // v3.1（时序修复）：权重基址改为按 ocb 块 / oc / ic 三级常量递推，
                // 消掉原 ((ocb+i)*cin+ic)*kk 的每迭代乘加链（WNS 边缘路径的源头）。
                int off_ocb = 0;
                // k=3（12 字节对齐容器）的块基址递推：每块 +cin*12*OC_BLOCK
                int off_ocb12 = 0;
                // k=5（28 字节对齐容器）的块基址递推：每块 +cin*28*OC_BLOCK
                int off_ocb28 = 0;
                // 补齐后的通道数（C8 写回按 8 的整块走）
                const int cpadded = ((cout + OC_BLOCK - 1) / OC_BLOCK) * OC_BLOCK;
                for (int ocb = 0; ocb < cout; ocb += OC_BLOCK) {
                    const int ocbl = (cout - ocb < OC_BLOCK) ? (cout - ocb) : OC_BLOCK;
                    // 本 pass 实际算的通道上界：k=1 是 K1_LANES（两个 OC 块），其余是 OC_BLOCK
                    const int ocbl1 = (cout - ocb < OC_BLOCK * K1_BLK) ? (cout - ocb)
                                                                      : OC_BLOCK * K1_BLK;
                    // 三个权重基址递推**先取本块值、再无条件推进**：
                    // k=1 的 16 车道会 `continue` 跳过奇数块，跳过时递推也不能错位。
                    const int off_b = off_ocb;
                    const int off12_b = off_ocb12;
                    const int off28_b = off_ocb28;
                    off_ocb += cin_kk * OC_BLOCK;       // 下一 OC 块的起始偏移
                    off_ocb12 += cin * 12 * OC_BLOCK;   // 同上（k=3 的 12 字节容器）
                    off_ocb28 += cin * 28 * OC_BLOCK;   // 同上（k=5 的 28 字节容器）
                    // k=1 的 16 车道一次吃掉两个 OC 块 ⇒ 奇数块整块跳过。
                    // （只对 k=1 生效；k=3/k=5 仍是每块 8 个 oc。）
                    if ((K1_BLK > 1) && (ksize == 1) && ((ocb & OC_BLOCK) != 0)) continue;

                    // 装当前 OC 块的权重。DDR 里每个 (oc,ic) 的 k×k 权重是连续的；
                    // 先顺序读到寄存器组，再用静态索引摆到 wgt_buf 的 (ky,kx) 位置。
                    // k=3 / k=5 分开写分支，避免运行期 k 造成的动态数组索引。
                    // 注意：不要给这条循环加 HLS PIPELINE —— 试过 II=2，
                    // HLS 会试图"展开再流水"，运行期循环次数展开失败后逻辑暴涨
                    // （LUT 19.7k→69k、DSP 66→471），实现直接放不下。
                    if (ksize == 1) {
                        // 1×1 的权重每个 (oc,ic) 只有 1 字节，单独做一条可流水的
                        // 连续读取；避免通用分支里运行期 k 造成的动态索引和长等待。
                        // 2026-10-23 第三轮：改成 32-bit 读。C8/CHW 两种布局下
                        // 权重包都是 `[cout][cin][1][1]` **逐 oc 连续**，所以
                        // 同一个 oc 的 cin 个权重就是一段连续字节，连续 4 个
                        // cin 可以一次读 4 字节（事务数 ÷4）。
                        // 对齐：off_ocb 每次加 cin_kk*OC_BLOCK = cin*8 ⇒ 恒为 8
                        // 的倍数；块内 off1 = off_ocb + i*cin，只有 cin%4==0 时
                        // 才保证 4 对齐 ⇒ cin%4!=0 走原来的逐字节慢路径
                        // （否则末尾那个字会读到下一个 oc 的地界外）。
                        // 写侧仍是每拍 1 字节（wgt1_buf 的 dim1 按 oc 分 bank，
                        // dim2 只有 1 个写口），所以用"每 4 拍取一个字"的
                        // 循环携带寄存器写法，循环形态与改造前一致。
                        int off1 = off_b;
                        if ((cin & 3) == 0) {
                            for (int i = 0; i < ocbl1; ++i, off1 += cin_kk) {
                                const w32_t *pw1 = reinterpret_cast<const w32_t *>(
                                    wgt + off1);
                                w32_t hold1 = 0;
                                for (int ic = 0; ic < cin; ++ic) {
                                    if ((ic & 3) == 0) {
                                        hold1 = pw1[ic >> 2];
                                    }
                                    wgt1_buf[i][ic] =
                                        (data_t)(hold1 >> ((ic & 3) << 3));
                                }
                            }
                        } else {
                            for (int i = 0; i < ocbl1; ++i, off1 += cin_kk) {
                                HLS_PRAGMA(HLS PIPELINE II=1)
                                for (int ic = 0; ic < cin; ++ic) {
                                    wgt1_buf[i][ic] = wgt[off1 + ic];
                                }
                            }
                        }
                    } else if (ksize == 3) {
#if K3_PAD12
                        /* ---- k=3 权重装载加宽：容器按 12 字节/（oc,ic）补齐 ----
                           导出侧把每个 (oc,ic) 的 9 个抽头**紧排在前 9 字节**、再补
                           3 个 0，凑成 3 个 32-bit 字（抽头 k 在第 k>>2 个字的第
                           k&3 字节），于是
                             · 每个 (oc,ic) 的起点 (oc·cin+ic)·12 恒 4 字节对齐
                               ⇒ 与 cin 是否 4 的倍数无关，**没有回退路径**；
                             · 一笔逻辑 burst = 12 字节（3 个字），原来是 9 笔
                               单字节事务（ARLEN=1，板端 16.3 拍/字节）；
                             · 写侧仍是 9 个**编译期常量** bank、每拍一个地址
                               ⇒ 吞吐上界 9 字节/拍（与改造前相同）；
                             · 读侧 `wgt3_buf[oc][ic][p]` 与 72 个 LUTRAM bank 的
                               划分逐字未变 ⇒ 计算路径的 DSP/FF/LUT 不变。
                           代价：权重体积 ×12/9 = 1.33（k=3 的 357 MB → 476 MB），
                           换来事务数 ÷9 且每次搬 12 字节。实测（csynth/上板）
                           见 docs/设计/记录_20261023_k3权重装载宽读.md。
                           为什么不用"每 4 个 ic 一组读 9 个字"（不膨胀体积）：
                           那个写法要在一个循环体里放 36 条静态写，HLS 的写端口
                           多路器 LUT +3.8k（36,775 → 40,950 = 88%，超红线）。 */
                        const int cin12 = cin * 12;
                        int off = off12_b;              // = ocb*cin*12（递推）
                        for (int i = 0; i < ocbl; ++i, off += cin12) {
                            const w32_t *pw =
                                reinterpret_cast<const w32_t *>(wgt + off);
                            HLS_PRAGMA(HLS PIPELINE II=1)
                            for (int ic = 0; ic < cin; ++ic) {
                                // 容器内字节序：抽头 k（= ky*3+kx）落在
                                // 第 (k>>2) 个字的第 (k&3) 字节；k=9..11 是补零。
                                HLS_PRAGMA(HLS UNROLL)
                                for (int k = 0; k < 9; ++k) {
                                    const w32_t wv = pw[3 * ic + (k >> 2)];
                                    wgt3_buf[i][ic][k] =
                                        (data_t)(wv >> (8 * (k & 3)));
                                }
                            }
                        }
#else
                        /* ---- 基线（K3_PAD12=0）：每 ic 逐字节读 9 个抽头 ----
                           板端 16.3 拍/字节（ARLEN=1），只作对照构建。 */
                        int off = off_b;
                        for (int i = 0; i < ocbl; ++i, off += cin_kk) {
                            const data_t *wp = wgt + off;
                            HLS_PRAGMA(HLS PIPELINE II=1)
                            for (int ic = 0; ic < cin; ++ic, wp += 9) {
                                HLS_PRAGMA(HLS UNROLL)
                                for (int ky = 0; ky < 3; ++ky) {
                                    HLS_PRAGMA(HLS UNROLL)
                                    for (int kx = 0; kx < 3; ++kx) {
                                        wgt3_buf[i][ic][ky * 3 + kx] = wp[ky * 3 + kx];
                                    }
                                }
                            }
                        }
#endif
                    } else {   // ksize == 5（ksize 已被上界校验限制为 1/3/5）
#if K5_PAD28
                        /* ---- k=5 权重装载加宽：容器按 28 字节/（oc,ic）补齐 ----
                           导出侧把每个 (oc,ic) 的 25 个抽头**紧排在前 25 字节**、
                           再补 3 个 0，凑成 7 个 32-bit 字（抽头 k 在第 k>>2 个
                           字的第 k&3 字节），于是
                             · 起点 (oc·cin+ic)·28 恒 4 字节对齐 ⇒ 没有回退路径；
                             · 一笔逻辑 burst = 28 字节（7 个字），原来是 25 笔
                               单字节事务（ARLEN=1）；
                             · 写侧仍是 25 个**编译期常量** bank（wgt_buf 的
                               dim3/dim4 完全拆分，与改造前逐字相同）⇒ 计算路径的
                               DSP/FF/LUT 不变，II 也不会退化。
                           与 k=3 的 12 字节容器是同一招，详见
                           docs/设计/记录_20261023_k5权重装载宽读.md。 */
                        const int cin28 = cin * 28;
                        int off = off28_b;              // = ocb*cin*28（递推）
                        for (int i = 0; i < ocbl; ++i, off += cin28) {
                            const w32_t *pw =
                                reinterpret_cast<const w32_t *>(wgt + off);
                            HLS_PRAGMA(HLS PIPELINE II=1)
                            for (int ic = 0; ic < cin; ++ic) {
                                // 容器内字节序：抽头 k = ky*5+kx 落在第 (k>>2)
                                // 个字的第 (k&3) 字节；k=25..27 是补零。
                                HLS_PRAGMA(HLS UNROLL)
                                for (int ky = 0; ky < 5; ++ky) {
                                    HLS_PRAGMA(HLS UNROLL)
                                    for (int kx = 0; kx < 5; ++kx) {
                                        const int kk = ky * 5 + kx;
                                        const w32_t wv = pw[7 * ic + (kk >> 2)];
                                        wgt_buf[i][ic][ky][kx] =
                                            (data_t)(wv >> (8 * (kk & 3)));
                                    }
                                }
                            }
                        }
#else
                        /* ---- 基线（K5_PAD28=0）：每 ic 逐字节读 25 个抽头 ---- */
                        int off = off_b;
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
#endif
                    }

#if FUSE_SUM
                    // ECA 融合：本 OC 块在本行的每通道累加和（寄存器，跨 x2 累加，
                    // 行末再一次性加到全局 chsum，避免写回循环里对外部内存做读改写）。
                    int32_t csum_l[OC_BLOCK * K1_BLK];
                    HLS_PRAGMA(HLS ARRAY_PARTITION variable=csum_l complete dim=1)
                    for (int i = 0; i < OC_BLOCK; ++i) {
                        HLS_PRAGMA(HLS UNROLL)
                        csum_l[i] = 0;
                    }
#endif

#if C8_LAYOUT
                    // ---- 2026-10-23 第四轮：写回改成"整行缓冲 + 独立宽写循环" ----
                    // 上面三条写回路径（k=1 / k=3 / 通用）现在只写 wbuf，不碰 DDR。
                    // 为什么要隔一层（上一轮"在计算循环里直接打 64-bit"实测慢 5.6%）：
                    // 核对生成的 RTL（engine_v1/syn/verilog/…LOOP_610_31.v:627）：
                    // 流水里的 store 是 **AWLEN=32'd0、WSTRB=1** ⇒ 每字节一笔 1-beat
                    // AXI 写事务，板端 140.5 MB 要 ~13 s（≈15 拍/字节），瓶颈是
                    // 每笔事务的往返延迟 × 在途字节数，不是事务宽度。挪到独立循环后
                    // 每笔事务带满 **8 字节**（WSTRB=8'd255、端口 64 bit）⇒
                    // 事务数 ÷8、在途字节 16 B → 128 B，实测 17.4 → 8.3 拍/字节。
                    // ⚠️ 注意 HLS **仍没**给出长 burst（本轮 RTL 依旧是
                    // `AWLEN = 32'd0`，即每像素一笔 1-beat 64-bit 事务）；把
                    // AWLEN>0 做出来是下一步（见 docs/设计/记录_20261023_写回宽写与填充零浪费.md §4）。
                    // 代价：每 (y, ocb) 块多 xn 拍串行拷贝 ⇒ 全网络 140.5 MB/8
                    // ≈ 17.6 M 拍 ≈ 0.12 s，可以忽略。
                    //
                    // 尾部通道：ocbl < 8 时（cout%8 != 0）8 字节整字会写到补零通道，
                    // 必须显式写 0（PS 侧按 ceil(cout/8)*8 分配缓冲区并期望那里是 0）。
                    if (ksize == 1) {
                        // k=1：车道数 K1_LANES，尾部按本 pass 的上界 ocbl1 补零
                        if (ocbl1 < OC_BLOCK * K1_BLK) {
                            for (int i = 0; i < OC_BLOCK * K1_BLK; ++i) {
                                if (i >= ocbl1) {
                                    HLS_PRAGMA(HLS UNROLL)
                                    for (int px = 0; px < xn; ++px) {
                                        wbuf[i][px] = (data_t)0;
                                    }
                                }
                            }
                        }
                    } else if (ocbl < OC_BLOCK) {
                        for (int i = 0; i < OC_BLOCK; ++i) {
                            if (i >= ocbl) {
                                HLS_PRAGMA(HLS UNROLL)
                                for (int px = 0; px < xn; ++px) {
                                    wbuf[i][px] = (data_t)0;
                                }
                            }
                        }
                    }
#endif

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
                            // ---- 2026-10-23 第十轮：车道 8 → K1_LANES（默认 16）----
                            // 16 个 oc 共用同一个 `p`（line_buf 每拍仍只读 1 次），所以
                            // **存储器/填充/k=3/k=5 一行都不用改**，加倍的只有寄存器与乘法器。
                            // 乘法器按 K1_DSP_LANES 混搭：前 K1_DSP_LANES 条强制进 DSP48
                            // （第九轮实测：一条 8×8 LUT 乘法 41 LUT → DSP=1、LUT=0），
                            // 其余留在 LUT 乘法器上，避免把 DSP 预算吃光。
                            acc_t acc0[K1_LANES];
                            acc_t acc1[K1_LANES];
                            HLS_PRAGMA(HLS ARRAY_PARTITION variable=acc0 complete dim=1)
                            HLS_PRAGMA(HLS ARRAY_PARTITION variable=acc1 complete dim=1)
                            for (int i = 0; i < K1_LANES; ++i) {
                                HLS_PRAGMA(HLS UNROLL)
                                acc0[i] = 0;
                                acc1[i] = 0;
                            }
                            for (int ic = 0; ic < cin; ++ic) {
                                HLS_PRAGMA(HLS PIPELINE II=PIPELINE_II)
                                const int32_t p = wide1
                                    ? (int32_t)line_buf[ic][cb >> 6][cb & 63]
                                    : (int32_t)line_buf[ic][0][cb];
                                // 前 K1_DSP_LANES 条：BIND_OP 强制 DSP48
                                for (int i = 0; i < K1_DSP_LANES; ++i) {
                                    HLS_PRAGMA(HLS UNROLL)
                                    const int32_t t1 = p * (int32_t)wgt1_buf[i][ic];
                                    HLS_PRAGMA(HLS BIND_OP variable=t1 op=mul impl=DSP)
                                    if (ic & 1) {
                                        acc1[i] += (acc_t)t1;
                                    } else {
                                        acc0[i] += (acc_t)t1;
                                    }
                                }
                                // 其余车道：留在 LUT 乘法器（mul_8s_8s_16，41 LUT/个）
                                for (int i = K1_DSP_LANES; i < K1_LANES; ++i) {
                                    HLS_PRAGMA(HLS UNROLL)
                                    const int32_t t2 = p * (int32_t)wgt1_buf[i][ic];
                                    if (ic & 1) {
                                        acc1[i] += (acc_t)t2;
                                    } else {
                                        acc0[i] += (acc_t)t2;
                                    }
                                }
                            }
// 2026-10-23 第三轮：写回**试过 64-bit 打包，实测无收益，已退回逐字节**
                            // 记录：C8 下同一像素 8 个 oc 是连续 8 字节，本以为
                            // 8 次单字节写 → 1 次 64-bit 写能省 8×（写回 13.1 s 的大头）。
                            // 板上实测相反：write_heavy_8_192 2341 → 2472 ms（慢 5.6%），
                            // swap_8_64 820.6 → 832.2 ms（慢 1.4%）。
                            // 当时的解释（"HLS 早就把 8 次单字节写合成了一笔 burst"）
                            // 是**错的**：第四轮核对生成 RTL ——
                            //   LOOP_610_31.v:627  assign m_axi_gmem1_0_AWLEN = 32'd0;
                            //                      WSTRB = 1'd1
                            // ⇒ 每字节一笔 1-beat AXI 写事务。csynth 里这条循环
                            // achieved II=1（不是没流水！），板端却仍是 ≈15 拍/字节：
                            // 瓶颈是事务往返延迟 × 在途字节数（16 笔 × 1 B）。
                            // 所以加宽单笔事务没用，而"打包"又把 store 塞回了计算
                            // 流水（8 个值攒齐才能打包）。正解：先落行缓冲 wbuf，
                            // 再用**独立拷贝循环**连续宽写（见 out_copy）。
                            // 2026-10-23 第七轮：收尾**完全展开**（原来 ocbl 是运行期上界、8 个通道串行
// 过一条流水，实测每个 (pixel, ocb) 块有 ~50 拍的固定开销 —— 这部分不是 DDR，
// 是收尾序列）。改成"常量 8 路 + 谓词"，8 路 shift/relu/saturate/wbuf 存并行。
                            HLS_PRAGMA(HLS UNROLL)
                            for (int i = 0; i < K1_LANES; ++i) {
                                if (i < ocbl1) {
                                const int32_t acc = (int32_t)acc0[i] + (int32_t)acc1[i];
                                int32_t v = acc + (int32_t)bias_buf[ocb + i];
                                v = v >> shift;
                                if (relu_on && v < 0) v = 0;
                            {
                                data_t v8 = saturate_int8(v);
#if FUSE_OUT
                                if (out_lut != 0) v8 = out_lut[(int)v8 + 128];
#endif
#if FUSE_SUM
                                csum_l[i] += (int32_t)v8;   // ECA 的通道和
#endif
                                WB_STORE(i, x2);
                            }
                                }
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
                                    h0[i] = q0 * (int32_t)wgt3_buf[i][ic][0]
                                          + q2 * (int32_t)wgt3_buf[i][ic][2]
                                          + q4 * (int32_t)wgt3_buf[i][ic][4]
                                          + q6 * (int32_t)wgt3_buf[i][ic][6]
                                          + q8 * (int32_t)wgt3_buf[i][ic][8];
                                    h1[i] = q1 * (int32_t)wgt3_buf[i][ic][1]
                                          + q3 * (int32_t)wgt3_buf[i][ic][3]
                                          + q5 * (int32_t)wgt3_buf[i][ic][5]
                                          + q7 * (int32_t)wgt3_buf[i][ic][7];
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
// 2026-10-23 第三轮：写回**试过 64-bit 打包，实测无收益，已退回逐字节**
                            // 记录：C8 下同一像素 8 个 oc 是连续 8 字节，本以为
                            // 8 次单字节写 → 1 次 64-bit 写能省 8×（写回 13.1 s 的大头）。
                            // 板上实测相反：write_heavy_8_192 2341 → 2472 ms（慢 5.6%），
                            // swap_8_64 820.6 → 832.2 ms（慢 1.4%）。
                            // 当时的解释（"HLS 早就把 8 次单字节写合成了一笔 burst"）
                            // 是**错的**：第四轮核对生成 RTL ——
                            //   LOOP_610_31.v:627  assign m_axi_gmem1_0_AWLEN = 32'd0;
                            //                      WSTRB = 1'd1
                            // ⇒ 每字节一笔 1-beat AXI 写事务。csynth 里这条循环
                            // achieved II=1（不是没流水！），板端却仍是 ≈15 拍/字节：
                            // 瓶颈是事务往返延迟 × 在途字节数（16 笔 × 1 B）。
                            // 所以加宽单笔事务没用，而"打包"又把 store 塞回了计算
                            // 流水（8 个值攒齐才能打包）。正解：先落行缓冲 wbuf，
                            // 再用**独立拷贝循环**连续宽写（见 out_copy）。
                            // 2026-10-23 第七轮：收尾**完全展开**（原来 ocbl 是运行期上界、8 个通道串行
// 过一条流水，实测每个 (pixel, ocb) 块有 ~50 拍的固定开销 —— 这部分不是 DDR，
// 是收尾序列）。改成"常量 8 路 + 谓词"，8 路 shift/relu/saturate/wbuf 存并行。
                            HLS_PRAGMA(HLS UNROLL)
                            for (int i = 0; i < OC_BLOCK; ++i) {
                                if (i < ocbl) {
                                const int32_t acc = (int32_t)acc0[i] + (int32_t)acc1[i];
                                int32_t v = acc + (int32_t)bias_buf[ocb + i];
                                v = v >> shift;
                                if (relu_on && v < 0) v = 0;
                            {
                                data_t v8 = saturate_int8(v);
#if FUSE_OUT
                                if (out_lut != 0) v8 = out_lut[(int)v8 + 128];
#endif
#if FUSE_SUM
                                csum_l[i] += (int32_t)v8;   // ECA 的通道和
#endif
                                WB_STORE(i, x2);
                            }
                                }
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
                                if (relu_on && v < 0) v = 0;   // v2：ReLU 可旁路

                            {
                                data_t v8 = saturate_int8(v);
#if FUSE_OUT
                                if (out_lut != 0) v8 = out_lut[(int)v8 + 128];
#endif
#if FUSE_SUM
                                csum_l[i] += (int32_t)v8;   // ECA 的通道和
#endif
                                WB_STORE(i, x2);
                            }
                            }
                        }
                        }   // end else (ksize != 1)
                    }

#if C8_LAYOUT
                    // ---- 本 (y, ocb) 行块一次性宽写出去 ----
                    // 纯拷贝循环：8 个 bank 同址并行读 → 拼 64-bit → 连续写。
                    // 基址 OUT_ADDR(ocb,0,y,x0) 恒为 8 的倍数（C8 布局每像素 8 字节
                    // + x 以 8 递增），PS 侧缓冲区也是页对齐，所以 reinterpret 安全。
                    // 注意：**不要**把它并回计算循环（见上面 64-bit 打包实测）。
                    {
                        const int obase = (int)(OUT_ADDR(ocb, 0, y, x0));
                        w64_t *pw = reinterpret_cast<w64_t *>(out_fm + obase);
                        HLS_PRAGMA(HLS PIPELINE II=1)
                        for (int px = 0; px < xn; ++px) {
                            // 8 个 bank 的同址并行读 → 8 个**常量**移位拼字。
                            // 不用 .range(8*i+7,8*i)：那样 i 是循环变量，HLS 会
                            // 先建一个 64-bit 桶形移位再靠 UNROLL 常量折叠，写死更稳。
                            w64_t w = (w64_t)(uint8_t)(wbuf[0][px]);
                            w |= (w64_t)(uint8_t)(wbuf[1][px]) << 8;
                            w |= (w64_t)(uint8_t)(wbuf[2][px]) << 16;
                            w |= (w64_t)(uint8_t)(wbuf[3][px]) << 24;
                            w |= (w64_t)(uint8_t)(wbuf[4][px]) << 32;
                            w |= (w64_t)(uint8_t)(wbuf[5][px]) << 40;
                            w |= (w64_t)(uint8_t)(wbuf[6][px]) << 48;
                            w |= (w64_t)(uint8_t)(wbuf[7][px]) << 56;
                            pw[px] = w;
                        }
                    }
                    // k=1 的 16 车道：本 pass 还算了 ocb+8..ocb+15，它们是**另一段**
                    // 8 字节（C8 布局里 oc 块号相差 1），所以再用一条同样形态的
                    // 拷贝循环写出去 —— 每笔事务仍是 8 字节 1-beat，DDR 事务形态不变。
                    if ((K1_BLK > 1) && (ksize == 1) && (ocb + OC_BLOCK < cpadded)) {
                        const int obase1 = (int)(OUT_ADDR(ocb + OC_BLOCK, 0, y, x0));
                        w64_t *pw1 = reinterpret_cast<w64_t *>(out_fm + obase1);
                        HLS_PRAGMA(HLS PIPELINE II=1)
                        for (int px = 0; px < xn; ++px) {
                            w64_t w = (w64_t)(uint8_t)(wbuf[OC_BLOCK + 0][px]);
                            w |= (w64_t)(uint8_t)(wbuf[OC_BLOCK + 1][px]) << 8;
                            w |= (w64_t)(uint8_t)(wbuf[OC_BLOCK + 2][px]) << 16;
                            w |= (w64_t)(uint8_t)(wbuf[OC_BLOCK + 3][px]) << 24;
                            w |= (w64_t)(uint8_t)(wbuf[OC_BLOCK + 4][px]) << 32;
                            w |= (w64_t)(uint8_t)(wbuf[OC_BLOCK + 5][px]) << 40;
                            w |= (w64_t)(uint8_t)(wbuf[OC_BLOCK + 6][px]) << 48;
                            w |= (w64_t)(uint8_t)(wbuf[OC_BLOCK + 7][px]) << 56;
                            pw1[px] = w;
                        }
                    }
#endif

#if FUSE_SUM
                    // 本行、本 OC 块的通道和一次性加到全局（chsum 由 PS 侧清零）。
                    // 放在 x2 循环之外：避免在写回流水里对外部内存做读改写。
                    if (chsum != 0) {
                        for (int i = 0; i < (ksize == 1 ? ocbl1 : ocbl); ++i) {
                            chsum[ocb + i] += csum_l[i];
                        }
                    }
#endif
}
            }
        }


    return ERR_OK;
}
