// ============================================================================
//  golden_driver.cpp —— make_golden.py 的 C++ 后端
//
//  作用：把 golden/<shape>/ 下的 input_c8.bin / weight.bin / bias.bin 喂给
//        conv_ref_rt.cpp（与 RTL 逐位一致的参考实现），按契约 §4 生成
//        expected_c8.bin（C8 布局、输出通道补齐到 8 的倍数）。
//
//  为什么单独写一个 driver，而不是在 python 里重算：
//    黄金输出必须由**唯一**的参考实现产生。python 只做搬运和校验，不做算术，
//    否则"两边一起错"的风险又回来了。
//
//  编译（由 make_golden.py 自动完成）：
//    g++ -std=c++14 -O2 -I <pl/src> golden_driver.cpp <pl/src>/conv_ref_rt.cpp
//
//  用法：
//    golden_driver <dir> <cin> <cout> <k> <s> <d> <pad> <H> <W> <shift> <relu>
//  可选第 12 个参数：CHW 输出路径（调试/交叉校验用）。
//
//  退出码：0 成功；2 参数/文件错误。
// ============================================================================
#include "conv2d_engine.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace {

bool read_file(const std::string &path, std::vector<unsigned char> &buf,
               size_t want, const char *what) {
    FILE *f = fopen(path.c_str(), "rb");
    if (f == nullptr) {
        fprintf(stderr, "FAIL: 打不开 %s（%s）\n", path.c_str(), what);
        return false;
    }
    fseek(f, 0, SEEK_END);
    const long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (n != static_cast<long>(want)) {
        fprintf(stderr, "FAIL: %s 是 %ld 字节，期望 %zu（%s）\n",
                path.c_str(), n, want, what);
        fclose(f);
        return false;
    }
    buf.resize(want);
    if (want != 0 && fread(buf.data(), 1, want, f) != want) {
        fprintf(stderr, "FAIL: 读 %s 出错\n", path.c_str());
        fclose(f);
        return false;
    }
    fclose(f);
    return true;
}

bool write_file(const std::string &path, const void *data, size_t n) {
    FILE *f = fopen(path.c_str(), "wb");
    if (f == nullptr) {
        fprintf(stderr, "FAIL: 打不开 %s 写入\n", path.c_str());
        return false;
    }
    if (n != 0 && fwrite(data, 1, n, f) != n) {
        fprintf(stderr, "FAIL: 写 %s 出错\n", path.c_str());
        fclose(f);
        return false;
    }
    fclose(f);
    return true;
}

inline size_t c8_idx(int c, int y, int x, int H, int W, int C8) {
    return static_cast<size_t>(c / C8) * static_cast<size_t>(H) * W * C8
         + static_cast<size_t>(y) * W * C8
         + static_cast<size_t>(x) * C8
         + static_cast<size_t>(c % C8);
}

int arg_int(char **argv, int i, const char *name) {
    char *end = nullptr;
    const long v = strtol(argv[i], &end, 10);
    if (end == argv[i] || *end != '\0') {
        fprintf(stderr, "FAIL: 参数 %s 不是整数: %s\n", name, argv[i]);
        exit(2);
    }
    return static_cast<int>(v);
}

}  // namespace

int main(int argc, char **argv) {
    if (argc != 12 && argc != 13) {
        fprintf(stderr,
                "用法: %s <dir> <cin> <cout> <k> <s> <d> <pad> <H> <W> "
                "<shift> <relu> [chw_out]\n",
                argv[0]);
        return 2;
    }
    const std::string dir = argv[1];
    const int cin   = arg_int(argv, 2, "cin");
    const int cout  = arg_int(argv, 3, "cout");
    const int ksize = arg_int(argv, 4, "k");
    const int stride= arg_int(argv, 5, "s");
    const int dil   = arg_int(argv, 6, "d");
    const int pad   = arg_int(argv, 7, "pad");
    const int H     = arg_int(argv, 8, "H");
    const int W     = arg_int(argv, 9, "W");
    const int shift = arg_int(argv, 10, "shift");
    const int relu  = arg_int(argv, 11, "relu");

    if (cin <= 0 || cout <= 0 || H <= 0 || W <= 0 || ksize <= 0) {
        fprintf(stderr, "FAIL: 形状参数必须为正\n");
        return 2;
    }
    if (shift < 0 || shift > 31) {
        fprintf(stderr, "FAIL: shift=%d 超出 [0,31]\n", shift);
        return 2;
    }
    // same padding（契约 §2.3 / conv2d_accel.h 的编译期约束）
    if ((ksize - 1) * dil != 2 * pad) {
        fprintf(stderr, "FAIL: 不满足 same padding: (k-1)*d=%d != 2*pad=%d\n",
                (ksize - 1) * dil, 2 * pad);
        return 2;
    }
    const int Hout = (H + 2 * pad - ((ksize - 1) * dil + 1)) / stride + 1;
    const int Wout = (W + 2 * pad - ((ksize - 1) * dil + 1)) / stride + 1;
    if (Hout <= 0 || Wout <= 0) {
        fprintf(stderr, "FAIL: 输出尺寸非正 (%dx%d)\n", Hout, Wout);
        return 2;
    }
    const int C8 = 8;
    const int cin_pad  = ((cin + C8 - 1) / C8) * C8;
    const int cout_pad = ((cout + C8 - 1) / C8) * C8;
    const size_t n_in_c8 = static_cast<size_t>(cin_pad) * H * W;
    const size_t n_w = static_cast<size_t>(cout) * cin *
        (ksize == 3 ? 12 : (ksize == 5 ? 28 : ksize * ksize));
    const size_t n_b = static_cast<size_t>(cout) * 4;
    const size_t n_out_c8 = static_cast<size_t>(cout_pad) * Hout * Wout;

    std::vector<unsigned char> raw_in, raw_w, raw_b;
    if (!read_file(dir + "/input_c8.bin", raw_in, n_in_c8, "C8 输入")) return 2;
    if (!read_file(dir + "/weight.bin", raw_w, n_w, "权重")) return 2;
    if (!read_file(dir + "/bias.bin", raw_b, n_b, "偏置")) return 2;

    // C8 -> CHW（参考实现要 CHW）。补零通道不参与计算。
    std::vector<data_t> in_chw(static_cast<size_t>(cin) * H * W, 0);
    for (int c = 0; c < cin; ++c)
        for (int y = 0; y < H; ++y)
            for (int x = 0; x < W; ++x)
                in_chw[static_cast<size_t>(c * H + y) * W + x] =
                    static_cast<data_t>(static_cast<int8_t>(
                        raw_in[c8_idx(c, y, x, H, W, C8)]));

    std::vector<data_t> wgt(n_w);
    for (size_t i = 0; i < n_w; ++i)
        wgt[i] = static_cast<data_t>(static_cast<int8_t>(raw_w[i]));
    std::vector<bias_t> bias(static_cast<size_t>(cout));
    memcpy(bias.data(), raw_b.data(), n_b);

    std::vector<data_t> out_chw(static_cast<size_t>(cout) * Hout * Wout, 0);
    const int rc = conv_ref_rt(in_chw.data(), wgt.data(), bias.data(),
                               out_chw.data(), H, W, Hout, Wout,
                               cin, cout, ksize, stride, dil, pad, shift, relu);
    if (rc != ERR_OK) {
        fprintf(stderr, "FAIL: conv_ref_rt 返回 %d\n", rc);
        return 2;
    }

    // CHW -> C8，补齐通道写 0（契约 §2.1 / §4）
    std::vector<unsigned char> out_c8(n_out_c8, 0);
    for (int c = 0; c < cout; ++c)
        for (int y = 0; y < Hout; ++y)
            for (int x = 0; x < Wout; ++x)
                out_c8[c8_idx(c, y, x, Hout, Wout, C8)] =
                    static_cast<unsigned char>(static_cast<int8_t>(
                        out_chw[static_cast<size_t>(c * Hout + y) * Wout + x]));

    if (!write_file(dir + "/expected_c8.bin", out_c8.data(), out_c8.size()))
        return 2;

    if (argc == 13) {
        if (!write_file(argv[12], out_chw.data(),
                        out_chw.size() * sizeof(data_t)))
            return 2;
    }

    printf("OK cin=%d(->%d) cout=%d(->%d) k=%d s=%d d=%d p=%d %dx%d shift=%d "
           "relu=%d Hout=%d Wout=%d expected=%zu B\n",
           cin, cin_pad, cout, cout_pad, ksize, stride, dil, pad, H, W,
           shift, relu, Hout, Wout, n_out_c8);
    return 0;
}
