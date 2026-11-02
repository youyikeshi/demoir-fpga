这些是 load #2（t129_071228）的产物，**采集口径是错的**，不能当硬件证据：
我在 `_PlBackend.finish()` 里、`super().finish()`（内含 reap 阻塞等 done）**之前**读了 OUT 槽，
读到的是"跑到一半"的缓冲（首 ~5 KB 是新数据，其余是上一轮残留 / arena 初始 0xA5 填充）。
所以这里的 "89% 错字节" 是探针 bug，不是硬件错。
第 3/4 轮（probe_t129b.py / probe_t129c.py）改成 reap 之后读，结果全对。
