// 本地工具：用 C 参考实现（conv_ref_rt）对某个数据集求输出，写 out_ref.bin。
// 用途：校验 PYNQ 脚本里的 numpy 金标准（ref_numpy.py）与 C 参考逐位一致。
//
// 编译：
//   g++ -std=c++14 -O2 -I fpga/pl/src fpga/ps/tools/dump_ref.cpp fpga/pl/src/conv_ref_rt.cpp -o fpga/pl/build/dump_ref.exe
// 运行：
//   dump_ref.exe <数据目录> <cin> <cout> <k> <stride> <dil> <pad>

#include "conv2d_engine.h"

#include <cstdio>
#include <cstdlib>
#include <vector>

static unsigned char *read_exact(const char *path, size_t want) {
    FILE *f = fopen(path, "rb");
    if (!f) { printf("打不开 %s\n", path); return 0; }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    if ((size_t)n != want) { printf("%s 大小 %ld，期望 %zu\n", path, n, want); fclose(f); return 0; }
    unsigned char *buf = (unsigned char *)malloc(want);
    if (fread(buf, 1, want, f) != want) { printf("读 %s 出错\n", path); fclose(f); return 0; }
    fclose(f);
    return buf;
}

int main(int argc, char **argv) {
    if (argc < 8) {
        printf("用法: dump_ref.exe <dir> <cin> <cout> <k> <stride> <dil> <pad> [relu=1]\n");
        return 2;
    }
    const char *dir = argv[1];
    int cin = atoi(argv[2]), cout = atoi(argv[3]), k = atoi(argv[4]);
    int s = atoi(argv[5]), d = atoi(argv[6]), pad = atoi(argv[7]);
    int relu = (argc >= 9) ? atoi(argv[8]) : 1;

    char path[1024];
    int H = 0, W = 0, shift = 0;
    snprintf(path, sizeof(path), "%s/meta.txt", dir);
    FILE *fm = fopen(path, "r");
    if (!fm || fscanf(fm, "%d %d %d", &H, &W, &shift) != 3) { printf("meta.txt 读取失败\n"); return 2; }
    fclose(fm);

    const int Hout = (H - 1) / s + 1;
    const int Wout = (W - 1) / s + 1;
    const size_t n_in = (size_t)cin * H * W;
    const size_t n_w = (size_t)cout * cin * k * k;
    const size_t n_b = (size_t)cout;
    const size_t n_o = (size_t)cout * Hout * Wout;

    snprintf(path, sizeof(path), "%s/input.bin", dir);
    unsigned char *ri = read_exact(path, n_in);
    snprintf(path, sizeof(path), "%s/weight.bin", dir);
    unsigned char *rw = read_exact(path, n_w);
    snprintf(path, sizeof(path), "%s/bias.bin", dir);
    unsigned char *rb = read_exact(path, n_b * 4);
    if (!ri || !rw || !rb) return 2;

    std::vector<data_t> in(n_in), w(n_w);
    std::vector<bias_t> b(n_b);
    std::vector<data_t> out(n_o);
    for (size_t i = 0; i < n_in; i++) in[i] = (data_t)(int8_t)ri[i];
    for (size_t i = 0; i < n_w; i++) w[i] = (data_t)(int8_t)rw[i];
    for (size_t i = 0; i < n_b; i++) b[i] = ((int32_t *)(void *)rb)[i];

    conv_ref_rt(in.data(), w.data(), b.data(), out.data(),
                H, W, Hout, Wout, cin, cout, k, s, d, pad, shift, relu);

    snprintf(path, sizeof(path), "%s/out_ref.bin", dir);
    FILE *fo = fopen(path, "wb");
    if (!fo) { printf("写 %s 失败\n", path); return 2; }
    fwrite(out.data(), 1, n_o, fo);
    fclose(fo);
    printf("out_ref.bin 已写出：%zu 个输出（%dx%d，cout=%d）\n", n_o, Hout, Wout, cout);
    return 0;
}
