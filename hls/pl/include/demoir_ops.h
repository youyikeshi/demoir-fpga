/* ===========================================================================
 * demoir_ops.h —— 去摩尔纹加速器的算子接口（PL 侧）
 * ===========================================================================
 *
 * 这批函数有两个身份，必须同时满足：
 *
 *   1. **能编译成普通 C++**，在 x86 上跟 `hls/ps/ops_ref.py`（NumPy）逐算子对拍。
 *      这是 `hls/tests/run_equiv.py` 干的事。
 *   2. **能直接被 Vitis HLS 综合**。所以循环结构是"行缓冲 + 通道向量化"的形状，
 *      而不是图省事的 im2col；所有 `#pragma HLS` 在 g++ 下会被忽略
 *      （编译时加 `-Wno-unknown-pragmas`）。
 *
 * 三条贯穿全文件的约定，改任何一条都要同步改 ps/ops_ref.py 与 ps/graph_spec.py：
 *
 *   约定 A —— 布局：张量一律 **NHWC**，通道最内层。
 *       理由：HLS 里通道并行 = 最内层向量化；`m_axi` 突发读写天然连续。
 *       PyTorch 侧是 NCHW，对照前用 `ps/ops_ref.py: to_hw/from_hw` 转换。
 *       NHWC 还有一个附带好处：`concat`（拼接）退化成"写指针偏移"，
 *       不需要搬数据 —— 见 demoir_concat_c 的说明。
 *
 *   约定 B —— 数据类型：`demoir_t`。现在 = float（先证明算法对），
 *       量化阶段换成定点/ap_int 只改这一处。
 *
 *   约定 C —— 权重布局：卷积权重保持 PyTorch 的 **OIHW**，
 *       即 w[((co*C_in + ci)*k + kh)*k + kw]。这样权重导出不用重排；
 *       硬件上的分块（按并行通道宽度切）留到 M2 打包时做。
 * ======================================================================== */
#ifndef DEMOIR_OPS_H
#define DEMOIR_OPS_H

typedef float demoir_t;

#ifdef __cplusplus
extern "C" {
#endif

/* ---- 形状工具 ---------------------------------------------------------- */
int demoir_conv_out_size(int size, int k, int stride, int dilation, int pad);

/* ---- 重排类（纯搬运，无乘法） ------------------------------------------ */

/* (H,W,C) -> (H/r, W/r, C*r*r)，与 F.pixel_unshuffle 同序 */
void demoir_pixel_unshuffle(const demoir_t *in, demoir_t *out, int H, int W, int C, int r);

/* (H,W,C) -> (H*r, W*r, C/(r*r))，与 F.pixel_shuffle 同序 */
void demoir_pixel_shuffle(const demoir_t *in, demoir_t *out, int H, int W, int C, int r);

/* 通道重排/反重排（ECA 用），语义与 nets_student.ChannelShuffle 一致。
 * 输入按 (HW, C) 展开，HW 是空间点数（ECA 里 HW=1）。 */
void demoir_channel_shuffle(const demoir_t *in, demoir_t *out, int C, int HW, int groups);
void demoir_channel_unshuffle(const demoir_t *in, demoir_t *out, int C, int HW, int groups);

/* 通道拼接。硬件上首版就是逐块 memcpy；再往后应改成"多缓冲读"或写偏移，见文档。 */
void demoir_concat_c(const demoir_t *const *ins, const int *channels, int n,
                     demoir_t *out, int HW);

/* ---- 卷积 ------------------------------------------------------------- */

/* NHWC 直接卷积 + 可选 ReLU。
 * w: OIHW（见约定 C）；b 可为 NULL；pad 是 symmetric 零填充。 */
void demoir_conv2d(const demoir_t *in, const demoir_t *w, const demoir_t *b, demoir_t *out,
                   int C_in, int H, int W, int C_out, int k,
                   int stride, int dilation, int pad, int relu);

/* ---- 逐元素 / 归约 ---------------------------------------------------- */
void demoir_relu(demoir_t *x, int n);
void demoir_sigmoid(demoir_t *x, int n);
void demoir_add(const demoir_t *a, const demoir_t *b, demoir_t *out, int n);

/* attn: (1,1,chunks*C) 广播乘到 y: (H,W,C) 上。
 * `chunk` 选第几段 —— 对应 SAM 里的 torch.chunk(attn, 3)[i]。
 * 硬件上这一段就是一个地址偏移，不需要先把 attn 切出来拷一份，
 * 所以这里传索引而不是传切好的指针。 */
void demoir_mul_channel(const demoir_t *attn, const demoir_t *y, demoir_t *out,
                        int C, int HW, int chunk);

/* (H,W,C) -> (1,1,C)。注意：**必须等整幅特征算完**，不能流水着吃（见文档"屏障算子"）。 */
void demoir_global_avg_pool(const demoir_t *in, demoir_t *out, int C, int HW);

/* 沿通道轴的 1D 卷积（ECA）。in/out 都是 (1,1,C)，w 是 (1,1,k)。 */
void demoir_conv1d_c(const demoir_t *in, const demoir_t *w, demoir_t *out,
                     int C, int k, int pad, int relu);

/* ---- 双线性缩放 ------------------------------------------------------- */

/* 与 F.interpolate(mode='bilinear', align_corners=False) 等价。
 * step = 1/scale_factor —— **不是** in/out，见 ps/ops_ref.py:_bilinear_taps。 */
void demoir_resize_bilinear(const demoir_t *in, demoir_t *out,
                            int C, int H, int W, int H_out, int W_out, float step);

/* ---- ECA 融合（PS 少发 6 次任务；等价于上面几个原语的串联） ------------
 * w_conv2 是**先**作用的那个（net 里的命名就是先 conv2 再 conv1，别按 forward 顺序读名字）。
 * scratch 需要 C_total 个元素的临时空间。 */
void demoir_eca_fused(const demoir_t *in, const demoir_t *w_conv2, const demoir_t *w_conv1,
                      demoir_t *out, demoir_t *scratch, int C_total, int HW);

#ifdef __cplusplus
}
#endif

#endif /* DEMOIR_OPS_H */
