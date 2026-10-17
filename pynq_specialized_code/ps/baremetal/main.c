// PS 侧上板自检程序（裸机 / Vitis standalone）——JTAG 路线
//
// 不需要 SD 卡、UART、网口：由 JTAG 调试器把本程序下载到 PS 后，程序自己
// 完成：造数据 → 刷缓存 → 编程 PL 寄存器 → 启动卷积 → 轮询完成 → 读回结果
// → 与 CPU 参考实现逐元素对拍 → 把报告写进内存（并镜像到固定地址）。
// v2（2026-10-15）：新增 relu_en 寄存器（0x70）——用例 4 验证“线性输出”模式。
//
// 报告（也镜像在 0x3000_0000，32 个 32 位字）：
//   [0] = 0xC0FFEE01 魔数
//   [1] = 用例数    [2] = 通过数
//   从 [3] 起每用例 4 个字：{返回码, 不一致个数, 轮询圈数, 保留}
//
// 本地语法检查（不需要 BSP）：
//   gcc -fsyntax-only -DHOST_CHECK fpga/ps/baremetal/main.c

#include <stdint.h>
#include <string.h>

#ifdef HOST_CHECK
static void Xil_DCacheFlushRange(uintptr_t a, uint32_t n) { (void)a; (void)n; }
static void Xil_DCacheInvalidateRange(uintptr_t a, uint32_t n) { (void)a; (void)n; }
static inline void dsb_barrier(void) { }
#else
#include "xil_cache.h"
static inline void dsb_barrier(void) { __asm__ volatile("dsb" ::: "memory"); }
#endif

#define USE_UART 0          // PS7 还没配 UART，保持 0；配好后可开打印
#if USE_UART
#include "xil_printf.h"
#endif

/* ---------------- PL 寄存器映射（详见 fpga/ps/README.md） ---------------- */
#define CTRL_BASE 0x40010000UL   /* s_axi_ctrl：形状参数 + 控制 */
#define OFFS_BASE 0x40000000UL   /* s_axi_control：四个缓冲区基地址 */
#define R_AP_CTRL   0x00
#define R_AP_RETURN 0x10
#define R_H 0x18
#define R_W 0x20
#define R_HOUT 0x28
#define R_WOUT 0x30
#define R_CIN 0x38
#define R_COUT 0x40
#define R_KSIZE 0x48
#define R_STRIDE 0x50
#define R_DIL 0x58
#define R_PAD 0x60
#define R_SHIFT 0x68
#define R_RELU  0x70               /* v2：1=ReLU；0=线性输出 */
#define B_IN_FM 0x10
#define B_WGT 0x1C
#define B_BIAS 0x28
#define B_OUT_FM 0x34

#define REPORT_MIRROR 0x30000000UL
#define POLL_TIMEOUT  100000000UL   /* 轮询上限（圈），远超实际需要 */

static inline void wr32(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static inline uint32_t rd32(uintptr_t a) { return *(volatile uint32_t *)a; }

/* ---------------- 测试用例表 ---------------- */
typedef struct {
    int cin, cout, k, s, d, pad, H, W, shift;
    uint32_t seed;
    int relu;
} case_t;

static const case_t CASES[] = {
    {112, 64, 3, 1, 1, 1, 8, 8, 7, 0x1234ABCDu, 1},  /* 与 csim/cosim 同形状 */
    {12,  48, 5, 1, 1, 2, 8, 8, 7, 0x77AA77AAu, 1},  /* conv_first 的 5x5   */
    {16,  16, 3, 1, 3, 3, 8, 8, 7, 0x0BADF00Du, 1},  /* 膨胀 3              */
    {112, 64, 3, 1, 1, 1, 8, 8, 7, 0x5EED5EEDu, 0},  /* v2：线性输出 relu=0 */
    {352, 192, 1, 1, 1, 0, 8, 8, 7, 0xC3520192u, 1}, /* v3：全网络最坏形状
                                                      （cin=352/cout=192，
                                                       覆盖扩容后的寻址边界） */
};
#define NCASES ((int)(sizeof(CASES) / sizeof(CASES[0])))

/* ---------------- 缓冲区（DDR，64 字节对齐） ---------------- */
#define MAX_IN   (112 * 16 * 16)          /* 28672 ≥ 352*8*8=22528 */
#define MAX_W    (96 * 112 * 3 * 3 + 64)  /* 96832 ≥ 192*352*1*1=67584 */
#define MAX_OUT  (96 * 16 * 16)           /* 24576 ≥ 192*8*8=12288 */
#define MAX_BIAS 192

static int8_t  g_in[MAX_IN]     __attribute__((aligned(64)));
static int8_t  g_wgt[MAX_W]     __attribute__((aligned(64)));
static int32_t g_bias[MAX_BIAS] __attribute__((aligned(64)));
static int8_t  g_out[MAX_OUT]   __attribute__((aligned(64)));
static int8_t  g_ref[MAX_OUT]   __attribute__((aligned(64)));
static uint32_t g_report[40];

static uint32_t xs32(uint32_t *s) {
    uint32_t x = *s;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    *s = x;
    return x;
}

/* CPU 参考实现：与 conv_ref_rt 语义一致（int32 累加、算术右移、ReLU 可旁路、饱和） */
static void cpu_conv_ref(const int8_t *in, const int8_t *w, const int32_t *b,
                         int8_t *out, int H, int W, int Hout, int Wout,
                         int cin, int cout, int k, int s, int d, int pad, int shift,
                         int relu) {
    for (int oc = 0; oc < cout; oc++)
        for (int y = 0; y < Hout; y++)
            for (int x = 0; x < Wout; x++) {
                int32_t acc = 0;
                for (int ic = 0; ic < cin; ic++)
                    for (int ky = 0; ky < k; ky++) {
                        int iy = y * s + ky * d - pad;
                        if (iy < 0 || iy >= H) continue;
                        for (int kx = 0; kx < k; kx++) {
                            int ix = x * s + kx * d - pad;
                            if (ix < 0 || ix >= W) continue;
                            acc += (int32_t)in[(ic * H + iy) * W + ix] *
                                   (int32_t)w[((oc * cin + ic) * k + ky) * k + kx];
                        }
                    }
                int32_t v = acc + b[oc];
                v = v >> shift;
                if (relu && v < 0) v = 0;       /* v2：线性层可旁路 ReLU */
                if (v > 127) v = 127;
                if (v < -128) v = -128;
                out[(oc * Hout + y) * Wout + x] = (int8_t)v;
            }
}

static int run_case(const case_t *c, uint32_t *detail) {
    const int hout = (c->H - 1) / c->s + 1;
    const int wout = (c->W - 1) / c->s + 1;
    const size_t n_in = (size_t)c->cin * c->H * c->W;
    const size_t n_w = (size_t)c->cout * c->cin * c->k * c->k;
    const size_t n_o = (size_t)c->cout * hout * wout;
    if (n_in > MAX_IN || n_w > MAX_W || n_o > MAX_OUT || c->cout > MAX_BIAS)
        return -100;                                    /* 用例超出缓冲区，跳过 */

    /* 1) 造数据（确定性 PRNG，硬件与 CPU 看到完全相同的输入） */
    uint32_t s = c->seed;
    for (size_t i = 0; i < n_in; i++) g_in[i] = (int8_t)(xs32(&s) & 0xFFu);
    for (size_t i = 0; i < n_w; i++) g_wgt[i] = (int8_t)(xs32(&s) & 0xFFu);
    for (int i = 0; i < c->cout; i++)
        g_bias[i] = (int32_t)(xs32(&s) % 200001u) - 100000;

    /* 2) 让 PL 能看到 DDR 里的最新数据（PL 走 HP 口，不经过 CPU 缓存） */
    Xil_DCacheFlushRange((uintptr_t)g_in, (uint32_t)n_in);
    Xil_DCacheFlushRange((uintptr_t)g_wgt, (uint32_t)n_w);
    Xil_DCacheFlushRange((uintptr_t)g_bias, (uint32_t)(c->cout * 4));
    Xil_DCacheInvalidateRange((uintptr_t)g_out, (uint32_t)n_o);

    /* 3) 编程 PL：先地址、后形状、最后启动 */
    wr32(OFFS_BASE + B_IN_FM, (uint32_t)(uintptr_t)g_in);
    wr32(OFFS_BASE + B_WGT, (uint32_t)(uintptr_t)g_wgt);
    wr32(OFFS_BASE + B_BIAS, (uint32_t)(uintptr_t)g_bias);
    wr32(OFFS_BASE + B_OUT_FM, (uint32_t)(uintptr_t)g_out);
    wr32(CTRL_BASE + R_H, (uint32_t)c->H);
    wr32(CTRL_BASE + R_W, (uint32_t)c->W);
    wr32(CTRL_BASE + R_HOUT, (uint32_t)hout);
    wr32(CTRL_BASE + R_WOUT, (uint32_t)wout);
    wr32(CTRL_BASE + R_CIN, (uint32_t)c->cin);
    wr32(CTRL_BASE + R_COUT, (uint32_t)c->cout);
    wr32(CTRL_BASE + R_KSIZE, (uint32_t)c->k);
    wr32(CTRL_BASE + R_STRIDE, (uint32_t)c->s);
    wr32(CTRL_BASE + R_DIL, (uint32_t)c->d);
    wr32(CTRL_BASE + R_PAD, (uint32_t)c->pad);
    wr32(CTRL_BASE + R_SHIFT, (uint32_t)c->shift);
    wr32(CTRL_BASE + R_RELU, (uint32_t)c->relu);
    dsb_barrier();

    wr32(CTRL_BASE + R_AP_CTRL, 1u);        /* ap_start */
    uint32_t spins = 0;
    while ((rd32(CTRL_BASE + R_AP_CTRL) & 2u) == 0u) {   /* ap_done */
        if (++spins > POLL_TIMEOUT) { detail[0] = 0xEEEE0001u; return -1; }
    }
    const int rc = (int)rd32(CTRL_BASE + R_AP_RETURN);

    /* 4) 读回结果 + CPU 参考对拍 */
    Xil_DCacheInvalidateRange((uintptr_t)g_out, (uint32_t)n_o);
    cpu_conv_ref(g_in, g_wgt, g_bias, g_ref, c->H, c->W, hout, wout,
                 c->cin, c->cout, c->k, c->s, c->d, c->pad, c->shift, c->relu);
    uint32_t bad = 0;
    for (size_t i = 0; i < n_o; i++)
        if (g_out[i] != g_ref[i]) bad++;

    detail[0] = (uint32_t)rc;
    detail[1] = bad;
    detail[2] = spins;
    return (rc == 0 && bad == 0) ? 0 : -2;
}

int main(void) {
    memset(g_report, 0, sizeof(g_report));
    g_report[0] = 0xC0FFEE01u;

    int pass = 0;
    for (int i = 0; i < NCASES; i++) {
        uint32_t d[4] = {0, 0, 0, 0};
        const int r = run_case(&CASES[i], d);
        g_report[3 + 4 * i] = (uint32_t)r;   /* 0=通过，其余为错误/超时码 */
        g_report[4 + 4 * i] = d[0];          /* PL 返回码 */
        g_report[5 + 4 * i] = d[1];          /* 不一致个数 */
        g_report[6 + 4 * i] = d[2];          /* 轮询圈数 */
        if (r == 0) pass++;
    }
    g_report[1] = (uint32_t)NCASES;
    g_report[2] = (uint32_t)pass;

    /* 镜像到固定地址：任何调试器都能直接按地址读（不依赖符号表） */
    for (int i = 0; i < 40; i++)
        ((volatile uint32_t *)REPORT_MIRROR)[i] = g_report[i];
    dsb_barrier();

#if USE_UART
    xil_printf("conv2d_engine self-test: %d/%d passed\r\n", pass, NCASES);
    for (int i = 0; i < NCASES; i++)
        xil_printf("  case %d: rc=%d bad=%lu spins=%lu\r\n", i,
                   (int)g_report[3 + 4 * i], (unsigned long)g_report[4 + 4 * i],
                   (unsigned long)g_report[6 + 4 * i]);
#else
    (void)pass;
#endif

    for (;;) { }   /* 跑完停在这里，等调试器读 g_report / 0x3000_0000 */
}
