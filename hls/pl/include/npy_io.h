/* ===========================================================================
 * npy_io.h —— 极简 .npy 读写（**仅测试台使用，不参与综合**）
 * ===========================================================================
 * 为什么自己写而不引第三方库：
 *   * 测试台要能在只有 g++ 的机器上直接 `make csim` 跑起来，
 *     不能要求先装 numpy C API 或 OpenCV；
 *   * .npy 的头部是纯文本字典，格式稳定，支持 float32 + C 序足够用了。
 * 只支持 '<f4' / '|f4'（float32，C 连续）——测试数据由 ps/ops_ref.py 生成，
 * 我们控制得住格式。遇到其它 dtype 直接报错，不做静默转换。
 * ======================================================================== */
#ifndef DEMOIR_NPY_IO_H
#define DEMOIR_NPY_IO_H

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace npy {

struct Array {
    std::vector<size_t> shape;
    std::vector<float> data;
    size_t numel() const {
        size_t n = 1;
        for (size_t i = 0; i < shape.size(); ++i) n *= shape[i];
        return n;
    }
};

inline void fail(const std::string &msg) {
    std::fprintf(stderr, "[npy] %s\n", msg.c_str());
    std::exit(2);
}

/* 只解析我们关心的三项：descr / fortran_order / shape */
inline Array read(const std::string &path) {
    std::FILE *f = std::fopen(path.c_str(), "rb");
    if (!f) fail("打不开 " + path);
    char magic[6] = {0};
    if (std::fread(magic, 1, 6, f) != 6) fail("读不了 magic: " + path);
    const std::string want("\x93NUMPY", 6);
    if (std::string(magic, 6) != want) fail("不是 .npy: " + path);

    unsigned char ver[2];
    if (std::fread(ver, 1, 2, f) != 2) fail("读不了版本: " + path);
    size_t hlen = 0;
    if (ver[0] == 1) {
        unsigned char b[2];
        if (std::fread(b, 1, 2, f) != 2) fail("读不了头长度: " + path);
        hlen = (size_t)b[0] | ((size_t)b[1] << 8);
    } else {
        unsigned char b[4];
        if (std::fread(b, 1, 4, f) != 4) fail("读不了头长度: " + path);
        hlen = (size_t)b[0] | ((size_t)b[1] << 8) | ((size_t)b[2] << 16) | ((size_t)b[3] << 24);
    }
    std::string header(hlen, '\0');
    if (std::fread(&header[0], 1, hlen, f) != hlen) fail("读不了头部: " + path);

    if (header.find("'fortran_order': True") != std::string::npos)
        fail("只支持 C 连续数组: " + path);
    if (header.find("f4") == std::string::npos)
        fail("只支持 float32（'<f4'）: " + path);

    Array a;
    size_t pos = header.find("'shape':");
    if (pos == std::string::npos) fail("头部没有 shape: " + path);
    pos = header.find('(', pos);
    size_t end = header.find(')', pos);
    if (pos == std::string::npos || end == std::string::npos) fail("shape 解析失败: " + path);
    const char *p = header.c_str() + pos + 1;
    while (p < header.c_str() + end) {
        while (p < header.c_str() + end && (*p == ' ' || *p == ',')) ++p;
        if (p >= header.c_str() + end) break;
        char *next = 0;
        long v = std::strtol(p, &next, 10);
        if (next == p) break;
        a.shape.push_back((size_t)v);
        p = next;
    }

    a.data.resize(a.numel());
    if (a.numel() > 0 && std::fread(&a.data[0], sizeof(float), a.numel(), f) != a.numel())
        fail("数据段长度不符: " + path);
    std::fclose(f);
    return a;
}

inline void write(const std::string &path, const std::vector<size_t> &shape,
                  const float *data, size_t count) {
    std::string dict = "{'descr': '<f4', 'fortran_order': False, 'shape': (";
    char buf[64];
    for (size_t i = 0; i < shape.size(); ++i) {
        std::snprintf(buf, sizeof(buf), "%zu%s", shape[i], (i + 1 < shape.size()) ? ", " : "");
        dict += buf;
    }
    if (shape.size() == 1) dict += ",";          /* numpy 的单元素 tuple 有个逗号 */
    dict += "), }";
    /* 头部（含 magic/version/长度域）补齐到 64 字节，末尾必须是 '\n' */
    size_t used = 6 + 2 + 2 + dict.size() + 1;
    size_t pad = (64 - (used % 64)) % 64;
    dict += std::string(pad, ' ');
    dict += "\n";
    uint16_t hlen = (uint16_t)dict.size();

    std::FILE *f = std::fopen(path.c_str(), "wb");
    if (!f) fail("写不了 " + path);
    std::fwrite("\x93NUMPY", 1, 6, f);
    unsigned char v[2] = {1, 0};
    std::fwrite(v, 1, 2, f);
    unsigned char lb[2] = {(unsigned char)(hlen & 0xff), (unsigned char)(hlen >> 8)};
    std::fwrite(lb, 1, 2, f);
    std::fwrite(dict.data(), 1, dict.size(), f);
    if (count) std::fwrite(data, sizeof(float), count, f);
    std::fclose(f);
}

}  /* namespace npy */

#endif /* DEMOIR_NPY_IO_H */
