// 单引擎测试台：同一份数据分别跑 conv2d_engine 和 conv_ref_rt，逐元素比对。
//
// 参数来源（二选一）：
//   1) 命令行：tb_engine.exe <dir> <cin> <cout> <k> <stride> <dil> <pad> [relu]
//   2) 环境变量：TB_DATA / TB_CIN / TB_COUT / TB_K / TB_S / TB_D / TB_P / TB_RELU
//      （Vitis HLS 的 csim 用环境变量最稳；两种方式都支持，命令行优先。）
//
// 数据目录里是 meta.txt（H W shift）+ input.bin + weight.bin + bias.bin，
// 与 conv2d_accel_tb.cpp 完全同格式。
//
// 退出码：0 一致，1 不一致，2 参数或文件有问题。

#include "conv2d_engine.h"

#include <cstdio>
#include <cstdlib>

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
            printf("用法: tb_engine <dir> <cin> <cout> <k> <stride> <dil> <pad> [relu=1]\n");
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
    printf("形状 cin=%d cout=%d k=%d s=%d d=%d p=%d relu=%d | H=%d W=%d shift=%d -> %dx%d\n",
           cin, cout, ksize, stride, dil, pad, relu, H, W, shift, Hout, Wout);

    const size_t n_in = (size_t)cin * H * W;
    const size_t n_w = (size_t)cout * cin * ksize * ksize;
    const size_t n_b = (size_t)cout;
    const size_t n_out = (size_t)cout * Hout * Wout;

    snprintf(path, sizeof(path), "%s/input.bin", dir);
    unsigned char *raw_in = read_exact(path, n_in);
    snprintf(path, sizeof(path), "%s/weight.bin", dir);
    unsigned char *raw_w = read_exact(path, n_w);
    snprintf(path, sizeof(path), "%s/bias.bin", dir);
    unsigned char *raw_b = read_exact(path, n_b * 4);
    if (raw_in == 0 || raw_w == 0 || raw_b == 0) return 2;

    data_t *in_fm = (data_t *)malloc(n_in);
    data_t *wgt = (data_t *)malloc(n_w);
    bias_t *bias = (bias_t *)malloc(n_b * sizeof(bias_t));
    data_t *ref = (data_t *)malloc(n_out);
    data_t *got = (data_t *)malloc(n_out);
    for (size_t i = 0; i < n_in; ++i) in_fm[i] = (data_t)(int8_t)raw_in[i];
    for (size_t i = 0; i < n_w; ++i) wgt[i] = (data_t)(int8_t)raw_w[i];
    for (size_t i = 0; i < n_b; ++i) {
        bias[i] = (bias_t)((int32_t *)(void *)raw_b)[i];
    }

    int rc = conv2d_engine(in_fm, wgt, bias, got, H, W, Hout, Wout,
                           cin, cout, ksize, stride, dil, pad, shift, relu);
    if (rc != ERR_OK) {
        printf("FAIL: conv2d_engine 返回错误码 %d\n", rc);
        return 2;
    }
    conv_ref_rt(in_fm, wgt, bias, ref, H, W, Hout, Wout,
                cin, cout, ksize, stride, dil, pad, shift, relu);

    size_t bad = 0;
    for (size_t i = 0; i < n_out; ++i) {
        if (ref[i] != got[i]) {
            if (bad < 10) {
                printf("  不一致 @%lu: ref=%d got=%d\n",
                       (unsigned long)i, (int)ref[i], (int)got[i]);
            }
            ++bad;
        }
    }
    if (bad != 0) {
        printf("FAIL: %lu / %lu 个输出不一致\n", (unsigned long)bad, (unsigned long)n_out);
        return 1;
    }
    printf("PASS: %lu 个输出全部一致\n", (unsigned long)n_out);
    return 0;
}
