// C8 交错布局测试台（2026-10-23）：输入按 C8 布局喂给引擎，输出按 C8 布局读回，
// 再转成 CHW 与 conv_ref_rt（CHW 金标准）逐元素比对。
//
// 与 tb_engine.cpp 的差别只有一处：输入/输出缓冲区的**内存布局**
//     addr(c,y,x) = (c/8)*H*W*8 + y*W*8 + x*8 + (c%8)
// 数据集目录里的 input_c8.bin 由 scripts/fuzz_c8_shapes.py 用
// pynq_specialized_code/ps/c8/c8lib.py 的 to_c8(..., pad=True) 生成
// （C 不是 8 的倍数时尾部补零到 ceil(C/8)*8 个通道）。
//
// 因此这个测试台同时验证两件事：
//   ① 引擎内部的 C8 地址公式与 numpy 版 c8lib 完全一致（否则读到的输入就错位）；
//   ② 输出按 C8 写回后转回 CHW，与 CHW 金标准逐位相同。
//
// 参数来源（二选一）：
//   1) 命令行：tb_engine_c8.exe <dir> <cin> <cout> <k> <stride> <dil> <pad> [relu]
//   2) 环境变量：TB_DATA / TB_CIN / TB_COUT / TB_K / TB_S / TB_D / TB_P / TB_RELU
//
// 退出码：0 一致，1 不一致，2 参数或文件有问题。

#include "conv2d_engine.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {

unsigned char *read_exact(const char *path, size_t want) {
    FILE *f = fopen(path, "rb");
    if (f == 0) {
        printf("FAIL: 打不开 %s\n", path);
        return 0;
    }
    fseek(f, 0, SEEK_END);
    const long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (n != (long)want) {
        printf("FAIL: %s 是 %ld 字节，期望 %lu\n", path, n, (unsigned long)want);
        fclose(f);
        return 0;
    }
    unsigned char *buf = (unsigned char *)malloc(want);
    if (fread(buf, 1, want, f) != want) {
        printf("FAIL: 读 %s 出错\n", path);
        fclose(f);
        free(buf);
        return 0;
    }
    fclose(f);
    return buf;
}

int env_int(const char *name, int fallback) {
    const char *v = getenv(name);
    return (v == 0) ? fallback : atoi(v);
}

// C8 元素下标（与 c8lib.c8_addr 同一公式）
inline size_t c8_idx(int c, int y, int x, int H, int W, int C8) {
    return (size_t)(c / C8) * (size_t)(H * W * C8)
         + (size_t)y * (W * C8) + (size_t)x * C8 + (c % C8);
}

}  // namespace

int main(int argc, char **argv) {
    const char *dir;
    int cin, cout, ksize, stride, dil, pad, relu;

    if (argc >= 8) {
        dir = argv[1];
        cin = atoi(argv[2]);
        cout = atoi(argv[3]);
        ksize = atoi(argv[4]);
        stride = atoi(argv[5]);
        dil = atoi(argv[6]);
        pad = atoi(argv[7]);
        relu = (argc >= 9) ? atoi(argv[8]) : 1;
    } else {
        const char *d = getenv("TB_DATA");
        if (d == 0) {
            printf("用法: tb_engine_c8 <dir> <cin> <cout> <k> <stride> <dil> <pad> [relu=1]\n");
            printf("或设置环境变量 TB_DATA/TB_CIN/TB_COUT/TB_K/TB_S/TB_D/TB_P/TB_RELU\n");
            return 2;
        }
        dir = d;
        cin = env_int("TB_CIN", 12);
        cout = env_int("TB_COUT", 48);
        ksize = env_int("TB_K", 5);
        stride = env_int("TB_S", 1);
        dil = env_int("TB_D", 1);
        pad = env_int("TB_P", 2);
        relu = env_int("TB_RELU", 1);
    }

    char path[1024];
    int H = 0, W = 0, shift = 0;
    snprintf(path, sizeof(path), "%s/meta.txt", dir);
    FILE *fm = fopen(path, "r");
    if (fm == 0) {
        printf("FAIL: 打不开 %s\n", path);
        return 2;
    }
    if (fscanf(fm, "%d %d %d", &H, &W, &shift) != 3) {
        printf("FAIL: meta.txt 格式不对\n");
        fclose(fm);
        return 2;
    }
    fclose(fm);

    const int Hout = (H + 2 * pad - ((ksize - 1) * dil + 1)) / stride + 1;
    const int Wout = (W + 2 * pad - ((ksize - 1) * dil + 1)) / stride + 1;
    const int c8 = C8_CH;
    const int cin_pad = ((cin + c8 - 1) / c8) * c8;    // 补零到 8 的倍数
    const int cout_pad = ((cout + c8 - 1) / c8) * c8;
    printf("C8 形状 cin=%d(->%d) cout=%d(->%d) k=%d s=%d d=%d p=%d relu=%d | "
           "H=%d W=%d shift=%d -> %dx%d\n",
           cin, cin_pad, cout, cout_pad, ksize, stride, dil, pad, relu,
           H, W, shift, Hout, Wout);

    const size_t n_in_chw = (size_t)cin * H * W;
    const size_t n_in_c8 = (size_t)cin_pad * H * W;         // = Cb*H*W*8
    const size_t n_w = (size_t)cout * cin * ksize * ksize;
    const size_t n_b = (size_t)cout;
    const size_t n_out_chw = (size_t)cout * Hout * Wout;
    const size_t n_out_c8 = (size_t)cout_pad * Hout * Wout;

    snprintf(path, sizeof(path), "%s/input_c8.bin", dir);
    unsigned char *raw_in8 = read_exact(path, n_in_c8);
    snprintf(path, sizeof(path), "%s/weight.bin", dir);
    unsigned char *raw_w = read_exact(path, n_w);
    snprintf(path, sizeof(path), "%s/bias.bin", dir);
    unsigned char *raw_b = read_exact(path, n_b * 4);
    if (raw_in8 == 0 || raw_w == 0 || raw_b == 0) return 2;

    data_t *in_c8 = (data_t *)malloc(n_in_c8);
    data_t *in_chw = (data_t *)malloc(n_in_chw);
    data_t *wgt = (data_t *)malloc(n_w);
    bias_t *bias = (bias_t *)malloc(n_b * sizeof(bias_t));
    data_t *out_c8 = (data_t *)malloc(n_out_c8);
    data_t *got = (data_t *)malloc(n_out_chw);
    data_t *ref = (data_t *)malloc(n_out_chw);
    if (!in_c8 || !in_chw || !wgt || !bias || !out_c8 || !got || !ref) {
        printf("FAIL: malloc 失败\n");
        return 2;
    }
    for (size_t i = 0; i < n_in_c8; ++i) in_c8[i] = (data_t)(int8_t)raw_in8[i];
    for (size_t i = 0; i < n_w; ++i) wgt[i] = (data_t)(int8_t)raw_w[i];
    for (size_t i = 0; i < n_b; ++i) {
        bias[i] = (bias_t)((int32_t *)(void *)raw_b)[i];
    }
    // C8 -> CHW：纯粹的重排（金标准要的是 CHW 输入）
    for (int c = 0; c < cin; ++c) {
        for (int y = 0; y < H; ++y) {
            for (int x = 0; x < W; ++x) {
                in_chw[(size_t)(c * H + y) * W + x] = in_c8[c8_idx(c, y, x, H, W, c8)];
            }
        }
    }
    // 可选：把 C8->CHW 的还原结果落盘，供 Python 侧与 c8lib.from_c8 对拍
    // （证明测试台用的地址公式 == 软件侧 c8lib 的布局，而不是"两边一起错"）
    const char *dump_in = getenv("TB_DUMP_IN");
    if (dump_in != 0) {
        FILE *fd = fopen(dump_in, "wb");
        if (fd != 0) {
            fwrite(in_chw, 1, n_in_chw, fd);
            fclose(fd);
            printf("已写 CHW 还原结果 %s（%lu 字节）\n", dump_in,
                   (unsigned long)n_in_chw);
        }
    }
    // 输出缓冲先清零：C8 下引擎只写 cout 个通道，尾部补零通道必须保持 0，
    // 这样"写越界"会立刻表现为不一致（而不是静默通过）。
    memset(out_c8, 0, n_out_c8 * sizeof(data_t));

    // 显式传 18 个参数：HLS 的 cosim 会把 conv2d_engine 宏替换成
    // apatb_conv2d_engine_sw()，而**默认实参在那条路径上不生效**
    // （2026-10-23 踩过：16 参调用 → "requires 18 arguments, but 16 were provided"）。
    int rc = conv2d_engine(in_c8, wgt, bias, out_c8, H, W, Hout, Wout,
                           cin, cout, ksize, stride, dil, pad, shift, relu,
                           (const data_t *)0, (int32_t *)0);
    if (rc != ERR_OK) {
        printf("FAIL: conv2d_engine 返回错误码 %d\n", rc);
        return 2;
    }
    conv_ref_rt(in_chw, wgt, bias, ref, H, W, Hout, Wout,
                cin, cout, ksize, stride, dil, pad, shift, relu);

    // C8 -> CHW
    for (int c = 0; c < cout; ++c) {
        for (int y = 0; y < Hout; ++y) {
            for (int x = 0; x < Wout; ++x) {
                got[(size_t)(c * Hout + y) * Wout + x] =
                    out_c8[c8_idx(c, y, x, Hout, Wout, c8)];
            }
        }
    }

    size_t bad = 0;
    for (size_t i = 0; i < n_out_chw; ++i) {
        if (ref[i] != got[i]) {
            if (bad < 10) {
                printf("  不一致 @%lu: ref=%d got=%d\n",
                       (unsigned long)i, (int)ref[i], (int)got[i]);
            }
            ++bad;
        }
    }
    // 尾部补零通道必须没被写脏
    size_t dirt = 0;
    for (int c = cout; c < cout_pad; ++c) {
        for (size_t i = 0; i < (size_t)Hout * Wout; ++i) {
            const int y = (int)(i / Wout), x = (int)(i % Wout);
            if ((int)out_c8[c8_idx(c, y, x, Hout, Wout, c8)] != 0) ++dirt;
        }
    }
    if (bad != 0 || dirt != 0) {
        printf("FAIL: %lu / %lu 个输出不一致，补零通道被写脏 %lu 处\n",
               (unsigned long)bad, (unsigned long)n_out_chw, (unsigned long)dirt);
        return 1;
    }
    printf("PASS: %lu 个输出全部一致（C8 布局，含 %d 个补零输出通道）\n",
           (unsigned long)n_out_chw, cout_pad - cout);
    return 0;
}
