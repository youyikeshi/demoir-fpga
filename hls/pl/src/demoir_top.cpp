/* ===========================================================================
 * demoir_top.cpp —— HLS 综合入口（m_axi + s_axilite 接口）
 * ===========================================================================
 * 这是**唯一**带接口 pragma 的文件。分层的理由：
 *
 *   pl/src 里的 .cpp = 算法本体，能在 g++ 下编译、能跟 NumPy 对拍
 *   demoir_top.cpp = 综合边界（哪个指针挂 m_axi、哪个标量走 AXI-Lite）
 *
 * 这样"改算法"不会碰接口、"改接口"不会碰算法。M2 的验收标准之一是
 * csim 与 csynth 用同一份 src 实现，不需要为综合维护第二份实现。
 *
 * 接口选择（依据 docs/部署/对接文档_去摩尔纹PS-PL部署.md §5）：
 *   * 张量全走 AXI4 master（m_axi），IP 直接读 PS DDR，不经 AXI DMA；
 *   * 尺寸/步长/量化参数走 AXI4-Lite（s_axilite），PS 用寄存器下发；
 *   * 首版一次调用算一层，PS 按静态计划逐层发 —— 不做整网流水。
 *
 * ⚠️ 当前维度全是**运行期标量**，综合出来是一份通用卷积器，
 *    资源与 II 都不会好看。M2 会按实际网络把通道数变成编译期常量并分块。
 *    这里先跑通"能综合、能导出 IP"这条链路。
 * ======================================================================== */
#include "demoir_ops.h"

extern "C" void demoir_conv2d_top(const demoir_t *in, const demoir_t *w, const demoir_t *b,
                                  demoir_t *out,
                                  int C_in, int H, int W, int C_out,
                                  int k, int stride, int dilation, int pad, int relu) {
#pragma HLS INTERFACE m_axi port = in offset = slave bundle = gmem0 depth = 65536
#pragma HLS INTERFACE m_axi port = w offset = slave bundle = gmem1 depth = 65536
#pragma HLS INTERFACE m_axi port = b offset = slave bundle = gmem2 depth = 4096
#pragma HLS INTERFACE m_axi port = out offset = slave bundle = gmem3 depth = 65536
#pragma HLS INTERFACE s_axilite port = in bundle = control
#pragma HLS INTERFACE s_axilite port = w bundle = control
#pragma HLS INTERFACE s_axilite port = b bundle = control
#pragma HLS INTERFACE s_axilite port = out bundle = control
#pragma HLS INTERFACE s_axilite port = C_in bundle = control
#pragma HLS INTERFACE s_axilite port = H bundle = control
#pragma HLS INTERFACE s_axilite port = W bundle = control
#pragma HLS INTERFACE s_axilite port = C_out bundle = control
#pragma HLS INTERFACE s_axilite port = k bundle = control
#pragma HLS INTERFACE s_axilite port = stride bundle = control
#pragma HLS INTERFACE s_axilite port = dilation bundle = control
#pragma HLS INTERFACE s_axilite port = pad bundle = control
#pragma HLS INTERFACE s_axilite port = relu bundle = control
#pragma HLS INTERFACE s_axilite port = return bundle = control

    demoir_conv2d(in, w, b, out, C_in, H, W, C_out, k, stride, dilation, pad, relu);
}
