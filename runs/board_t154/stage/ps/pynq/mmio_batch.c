/* mmio_batch.c —— T-23 第 2 级：把"一批寄存器写 + start + 等 done"压进一次 C 调用
 *
 * 为什么需要它（而不是只靠 Python fast-path）：
 *   板端一次 AXI-Lite 访问 ≈ 25~30 µs（引擎空闲时实测），其中 Python 侧
 *   （闭包/属性查找/struct.pack+切片赋值/循环）占几个 µs。把这几个 µs 拿掉
 *   不能改变总线事务数，但能把**每条命令的 Python 帧数从 ~20 降到 1**，
 *   并且轮询循环里不再有解释器开销。
 *
 * 硬约束（缺一不可，见 docs/PLAN_1second.md 的 PS 侧纪律）：
 *   1. 访问必须是 `volatile`（禁止编译器合并/重排/消除 32-bit 存）；
 *   2. start 之前必须有内存屏障 `__sync_synchronize()`（否则配置写可能晚到）；
 *   3. 主机**与板端各自现场编译**（`.so` 不进 git）：-O2 -fPIC -shared；
 *   4. 库加载失败 / 任何异常 ⇒ 调用方静默回落 Python 路径（本文件不 print）；
 *   5. 本文件**不含任何语义**：影子缓存、force、done 判据全部留在 Python 侧，
 *      这里只做"按给定顺序把字打下去"和"读同一个字直到 bit0=1"。
 *
 * 与 Python 路径的等价性：
 *   同一个 (off, val) 序列 ⇒ 同一组 32-bit 存；start 的写是序列之后的那一次；
 *   done 判据仍是对 AP_RETURN 读 bit0；超时只是"返回未完成"，由 Python 抛错。
 *
 * 编译：cc -O2 -std=c99 -fPIC -shared -o mmio_batch_<arch>.so mmio_batch.c
 */
#include <stdint.h>
#include <time.h>

static volatile uint8_t *g_base = 0;

static inline int64_t mb_now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + (int64_t)ts.tv_nsec;
}

static inline volatile uint32_t *mb_p(uint32_t off)
{
    return (volatile uint32_t *)(g_base + (off & ~3u));
}

/* ---- 基础访问（给 Python 的单字读写留的后门，非热路径）---- */
void mb_set_base(void *base) { g_base = (volatile uint8_t *)base; }

uint32_t mb_read(uint32_t off) { return *mb_p(off); }

void mb_write(uint32_t off, uint32_t v) { *mb_p(off) = v; }

/* ---- 只写一批字（顺序敏感；不 start、不轮询）---- */
int mb_write_pairs(const uint32_t *offs, const uint32_t *vals, int n)
{
    int i;
    for (i = 0; i < n; i++)
        *mb_p(offs[i]) = vals[i];
    return n;
}

/* ---- 写一批字 → 屏障 → start → 轮询 done ----
 * 返回最后一次读到的 AP_RETURN（bit0=1 表示完成）。
 * 超过 budget_ns 仍未完成 ⇒ 直接返回当时的 status（Python 侧判断超时）。
 * 轮询**不睡**：一次 AXI-Lite 读本身就要 25 µs，睡眠只会把量化误差加上去。
 */
uint32_t mb_run_pairs(uint32_t ctrl_off, uint32_t ret_off,
                      const uint32_t *offs, const uint32_t *vals, int n,
                      int64_t budget_ns)
{
    int i;
    int64_t t0;
    volatile uint32_t *ret = mb_p(ret_off);

    for (i = 0; i < n; i++)
        *mb_p(offs[i]) = vals[i];

    __sync_synchronize();          /* 配置写必须对 PL 可见之后才允许 start */
    *mb_p(ctrl_off) = 1u;          /* AP_CTRL bit0：每次都要真写（自清位） */
    __sync_synchronize();

    t0 = mb_now_ns();
    for (;;) {
        uint32_t st = *ret;
        if (st & 1u)
            return st;
        if (mb_now_ns() - t0 > budget_ns)
            return st;
    }
}

/* ---- 读一个字的 bit0 直到置位（给非 E1E2 的调用方）---- */
uint32_t mb_poll_bit0(uint32_t ret_off, int64_t budget_ns)
{
    volatile uint32_t *ret = mb_p(ret_off);
    int64_t t0 = mb_now_ns();
    for (;;) {
        uint32_t st = *ret;
        if (st & 1u)
            return st;
        if (mb_now_ns() - t0 > budget_ns)
            return st;
    }
}
