// 测试台：同一份输入分别跑参考实现和内核，逐元素比对。
//
// 用法：conv2d_accel_tb.exe [数据目录]
// 默认数据目录是 testdata/（相对当前工作目录），可换成 testdata_real 做真实权重比对。
//
// 退出码：0 一致，1 不一致，2 参数或文件有问题或内核返回错误码。

#include "conv2d_accel.h"

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

}  // namespace

int main(int argc, char **argv) {
    const char *dir = (argc > 1) ? argv[1] : "testdata";
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
    const int H_out = CONV_OUT(H);
    const int W_out = CONV_OUT(W);
    printf("H=%d W=%d shift=%d -> H_out=%d W_out=%d\n", H, W, shift, H_out, W_out);

    const size_t n_in = (size_t)CIN * H * W;
    const size_t n_w = (size_t)COUT * CIN * KSIZE * KSIZE;
    const size_t n_b = (size_t)COUT;
    const size_t n_out = (size_t)COUT * H_out * W_out;

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
        // bias 是小端 32 位有符号
        const uint32_t u = (uint32_t)raw_b[i * 4] | ((uint32_t)raw_b[i * 4 + 1] << 8) |
                           ((uint32_t)raw_b[i * 4 + 2] << 16) |
                           ((uint32_t)raw_b[i * 4 + 3] << 24);
        bias[i] = (bias_t)(int32_t)u;
    }

    int rc = conv_ref(in_fm, wgt, bias, ref, H, W, shift);
    if (rc != ERR_OK) {
        printf("FAIL: conv_ref 返回 %d\n", rc);
        return 2;
    }

    rc = conv2d_accel(in_fm, wgt, bias, got, H, W, shift);
    if (rc != ERR_OK) {
        printf("FAIL: conv2d_accel 返回 %d\n", rc);
        return 2;
    }

    size_t max_err = 0;
    long first_bad = -1;
    for (size_t i = 0; i < n_out; ++i) {
        const int d = abs((int)ref[i] - (int)got[i]);
        if ((size_t)d > max_err) max_err = (size_t)d;
        if (d != 0 && first_bad < 0) first_bad = (long)i;
    }

    if (first_bad >= 0) {
        printf("FAIL: 最大误差 %lu，首个不一致下标 %ld（ref=%d，got=%d）\n",
               (unsigned long)max_err, first_bad, (int)ref[first_bad],
               (int)got[first_bad]);
        return 1;
    }

    printf("PASS: %lu 个输出全部一致\n", (unsigned long)n_out);

    // 如果数据目录里有 ref_out.bin（PyTorch 导出的参考输出），再比一次。
    // 这一次是内核与浮点参考的差距，允许一个量化单位的误差。
    snprintf(path, sizeof(path), "%s/ref_out.bin", dir);
    FILE *fr = fopen(path, "rb");
    if (fr != 0) {
        unsigned char *raw_ref = read_exact(path, n_out);
        if (raw_ref != 0) {
            long worst = -1;
            int worst_d = 0;
            for (size_t i = 0; i < n_out; ++i) {
                const int d = abs((int)(int8_t)raw_ref[i] - (int)got[i]);
                if (d > worst_d) {
                    worst_d = d;
                    worst = (long)i;
                }
            }
            printf("与 PyTorch 参考的最大差 %d 个单位", worst_d);
            if (worst >= 0) printf("（下标 %ld）", worst);
            printf("\n");
            free(raw_ref);
            if (worst_d > 1) {
                printf("FAIL: 与浮点参考的差超过 1 个单位\n");
                fclose(fr);
                return 1;
            }
        }
        fclose(fr);
    }

    return 0;
}
