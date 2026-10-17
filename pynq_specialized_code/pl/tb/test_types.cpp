#include "conv2d_accel.h"

static_assert(sizeof(data_t) == 1, "data_t 必须是 1 字节");
static_assert(sizeof(acc_t) >= 4, "acc_t 至少要装得下 26 位");

int main() {
    return 0;
}
