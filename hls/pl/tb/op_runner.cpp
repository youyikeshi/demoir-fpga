/* ===========================================================================
 * op_runner.cpp —— 测试台：按清单执行 PL 算子，读写 .npy
 * ===========================================================================
 * 它**不是**要综合进 FPGA 的东西，只是让 g++ 能把 pl/src 拉起来跟
 * NumPy 参考对拍。HLS 那边综合的入口在 pl/src/demoir_top.cpp。
 *
 * 清单格式（TAB 分隔，一行一个任务，# 开头是注释）：
 *
 *   id <TAB> kind <TAB> in1.npy,in2.npy,... <TAB> out.npy <TAB> k=v,k=v,...
 *
 * 形状**从 .npy 头部读**，不在清单里重复写一遍 —— 少一处会不一致的地方。
 * 用法：  ./op_runner <manifest.txt>
 * ======================================================================== */
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "demoir_ops.h"
#include "npy_io.h"

namespace {

std::vector<std::string> split(const std::string &s, char sep) {
    std::vector<std::string> out;
    size_t start = 0;
    while (true) {
        size_t pos = s.find(sep, start);
        if (pos == std::string::npos) {
            out.push_back(s.substr(start));
            break;
        }
        out.push_back(s.substr(start, pos - start));
        start = pos + 1;
    }
    return out;
}

struct Params {
    std::vector<std::pair<std::string, double> > kv;
    double get(const char *key, double dflt) const {
        for (size_t i = 0; i < kv.size(); ++i)
            if (kv[i].first == key) return kv[i].second;
        return dflt;
    }
    bool has(const char *key) const {
        for (size_t i = 0; i < kv.size(); ++i)
            if (kv[i].first == key) return true;
        return false;
    }
};

Params parse_params(const std::string &s) {
    Params p;
    if (s.empty()) return p;
    std::vector<std::string> items = split(s, ',');
    for (size_t i = 0; i < items.size(); ++i) {
        size_t eq = items[i].find('=');
        if (eq == std::string::npos) continue;
        p.kv.push_back(std::make_pair(items[i].substr(0, eq),
                                      std::atof(items[i].c_str() + eq + 1)));
    }
    return p;
}

std::string task_id;

void emit(const std::string &path, const std::vector<size_t> &shape,
          const std::vector<float> &data) {
    npy::write(path, shape, data.empty() ? 0 : &data[0], data.size());
}

/* 每个算子一段：读输入 -> 定形状 -> 调 pl 算子 -> 写出。
 * 显式列出而不是搞多态，是为了让"这个算子收什么形状"一眼可见。 */
bool run_task(const std::string &kind, const std::vector<npy::Array> &in,
              const std::string &out_path, const Params &p) {
    const double r = p.get("r", 2);
    const double groups = p.get("groups", 3);
    const double scale = p.get("scale", 1.0);

    if (kind == "pixel_unshuffle") {
        const size_t H = in[0].shape[0], W = in[0].shape[1], C = in[0].shape[2];
        const size_t ri = (size_t)r;
        std::vector<float> o(H / ri * W / ri * C * ri * ri);
        demoir_pixel_unshuffle(&in[0].data[0], &o[0], (int)H, (int)W, (int)C, (int)ri);
        emit(out_path, std::vector<size_t>{H / ri, W / ri, C * ri * ri}, o);
        return true;
    }
    if (kind == "pixel_shuffle") {
        const size_t H = in[0].shape[0], W = in[0].shape[1], C = in[0].shape[2];
        const size_t ri = (size_t)r;
        std::vector<float> o(H * ri * W * ri * C / (ri * ri));
        demoir_pixel_shuffle(&in[0].data[0], &o[0], (int)H, (int)W, (int)C, (int)ri);
        emit(out_path, std::vector<size_t>{H * ri, W * ri, C / (ri * ri)}, o);
        return true;
    }
    if (kind == "channel_shuffle" || kind == "channel_unshuffle") {
        const size_t H = in[0].shape[0], W = in[0].shape[1], C = in[0].shape[2];
        std::vector<float> o(in[0].data.size());
        if (kind == "channel_shuffle")
            demoir_channel_shuffle(&in[0].data[0], &o[0], (int)C, (int)(H * W), (int)groups);
        else
            demoir_channel_unshuffle(&in[0].data[0], &o[0], (int)C, (int)(H * W), (int)groups);
        emit(out_path, in[0].shape, o);
        return true;
    }
    if (kind == "concat_c") {
        const size_t H = in[0].shape[0], W = in[0].shape[1];
        size_t c_total = 0;
        std::vector<const demoir_t *> ptrs;
        std::vector<int> chans;
        for (size_t i = 0; i < in.size(); ++i) {
            ptrs.push_back(&in[i].data[0]);
            chans.push_back((int)in[i].shape[2]);
            c_total += in[i].shape[2];
        }
        std::vector<float> o(H * W * c_total);
        demoir_concat_c(&ptrs[0], &chans[0], (int)in.size(), &o[0], (int)(H * W));
        emit(out_path, std::vector<size_t>{H, W, c_total}, o);
        return true;
    }
    if (kind == "conv2d") {
        const size_t H = in[0].shape[0], W = in[0].shape[1], C_in = in[0].shape[2];
        const size_t C_out = in[1].shape[0];
        const int k = (int)p.get("k", 3), stride = (int)p.get("stride", 1);
        const int dil = (int)p.get("dilation", 1), pad = (int)p.get("pad", 0);
        const int relu = p.get("relu", 0) > 0.5 ? 1 : 0;
        const demoir_t *bias = (in.size() > 2) ? &in[2].data[0] : 0;
        const int Ho = demoir_conv_out_size((int)H, k, stride, dil, pad);
        const int Wo = demoir_conv_out_size((int)W, k, stride, dil, pad);
        std::vector<float> o((size_t)Ho * Wo * C_out);
        demoir_conv2d(&in[0].data[0], &in[1].data[0], bias, &o[0], (int)C_in, (int)H, (int)W,
                      (int)C_out, k, stride, dil, pad, relu);
        emit(out_path, std::vector<size_t>{(size_t)Ho, (size_t)Wo, C_out}, o);
        return true;
    }
    if (kind == "relu" || kind == "sigmoid") {
        std::vector<float> o = in[0].data;
        if (kind == "relu") demoir_relu(&o[0], (int)o.size());
        else demoir_sigmoid(&o[0], (int)o.size());
        emit(out_path, in[0].shape, o);
        return true;
    }
    if (kind == "add") {
        std::vector<float> o(in[0].data.size());
        demoir_add(&in[0].data[0], &in[1].data[0], &o[0], (int)o.size());
        emit(out_path, in[0].shape, o);
        return true;
    }
    if (kind == "mul_channel") {
        if (in.size() < 2) { std::fprintf(stderr, "[op_runner] mul_channel 需要 2 个输入\n"); return false; }
        const size_t C = in[1].shape[2], HW = in[1].shape[0] * in[1].shape[1];
        std::vector<float> o(in[1].data.size());
        demoir_mul_channel(&in[0].data[0], &in[1].data[0], &o[0], (int)C, (int)HW,
                           (int)p.get("chunk", 0));
        emit(out_path, in[1].shape, o);
        return true;
    }
    if (kind == "global_avg_pool") {
        const size_t C = in[0].shape[2], HW = in[0].shape[0] * in[0].shape[1];
        std::vector<float> o(C);
        demoir_global_avg_pool(&in[0].data[0], &o[0], (int)C, (int)HW);
        emit(out_path, std::vector<size_t>{1, 1, C}, o);
        return true;
    }
    if (kind == "conv1d_c") {
        const size_t C = in[0].shape[2];
        const int k = (int)p.get("k", 9), pad = (int)p.get("pad", 4);
        const int relu = p.get("relu", 0) > 0.5 ? 1 : 0;
        std::vector<float> o(C);
        demoir_conv1d_c(&in[0].data[0], &in[1].data[0], &o[0], (int)C, k, pad, relu);
        emit(out_path, std::vector<size_t>{1, 1, C}, o);
        return true;
    }
    if (kind == "resize_bilinear") {
        const size_t H = in[0].shape[0], W = in[0].shape[1], C = in[0].shape[2];
        const size_t Ho = (size_t)(H * scale), Wo = (size_t)(W * scale);
        std::vector<float> o(Ho * Wo * C);
        demoir_resize_bilinear(&in[0].data[0], &o[0], (int)C, (int)H, (int)W, (int)Ho, (int)Wo,
                               (float)(1.0 / scale));
        emit(out_path, std::vector<size_t>{Ho, Wo, C}, o);
        return true;
    }
    std::fprintf(stderr, "[op_runner] 不认识的算子 kind=%s (task %s)\n",
                 kind.c_str(), task_id.c_str());
    return false;
}

}  /* namespace */

int main(int argc, char **argv) {
    if (argc < 2) {
        std::fprintf(stderr, "用法: %s <manifest.txt>\n", argv[0]);
        return 2;
    }
    std::FILE *mf = std::fopen(argv[1], "r");
    if (!mf) {
        std::fprintf(stderr, "打不开清单 %s\n", argv[1]);
        return 2;
    }
    char line[8192];
    int n_done = 0, n_fail = 0;
    while (std::fgets(line, sizeof(line), mf)) {
        std::string s(line);
        while (!s.empty() && (s[s.size() - 1] == '\n' || s[s.size() - 1] == '\r')) s.erase(s.size() - 1);
        if (s.empty() || s[0] == '#') continue;
        std::vector<std::string> f = split(s, '\t');
        if (f.size() < 5) {
            std::fprintf(stderr, "[op_runner] 字段不足（需要 5 个）: %s\n", s.c_str());
            ++n_fail;
            continue;
        }
        task_id = f[0];
        std::vector<std::string> paths = split(f[2], ',');
        std::vector<npy::Array> ins;
        for (size_t i = 0; i < paths.size(); ++i) {
            if (paths[i].empty()) continue;
            ins.push_back(npy::read(paths[i]));
        }
        if (!run_task(f[1], ins, f[3], parse_params(f[4]))) ++n_fail;
        else ++n_done;
    }
    std::fclose(mf);
    std::printf("op_runner: 完成 %d，失败 %d\n", n_done, n_fail);
    return n_fail ? 1 : 0;
}
