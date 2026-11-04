# v60 验收：V-C 通过 / V-D 失败 / 100 帧端到端被金丝雀拦下

## V-C 重新武装 ✅（CTRL 0x01→0x00→0x01 ×3）

| 次 | 回读 CTRL | `0x2C` 三读 | 增量 | fid / latch | `0x24` | nonblack |
|---|---|---|---|---|---|---|
| 1 | 0x01 | 1,1,1,1 | **0,0,0** | 1 / True | 1→1 | 0.8888 |
| 2 | 0x01 | 2,2,2,2 | **0,0,0** | 2 / True | 1→2 | 0.8888 |
| 3 | 0x01 | 4,4,4,4 | **0,0,0** | 3 / True | 2→3 | 0.8888 |

- 判据（稳态增量 0）**三条全过**，且三次都出图（`latch=True`、`nonblack` 恒 0.8888）。
- 备注：`0x2C` 的**绝对值**在每次 `0x00→0x01` 切换瞬间会 +1/+1/+2（暂态），切换完成后 1 s 内不再增长。
- 全过程中网输出 `out1 sha1 = 28cd6860…`（与 V-B2 逐位相同）。

## V-D 失败：`board_verify_numfix.py` 在 p0000 上 **Segmentation fault**

- 5 张全都没产出 JSON（`/tmp/v60_p0000.json` 不存在），聚合步骤因此 `FileNotFoundError`。
- **副作用**：这次崩溃之后，`pack/index.json`（282565 B）与 `pack/bin_weights.bin`（1,286,368 B）
  **长度不变但 md5 变了**（`df0b7f70…→550ce525…`、`706a1d22…→47cebe54…`），`json.load(index.json)` 报
  `UnicodeDecodeError: 0xf9 at 270336`。
- 已从主机重推一份干净副本到 `pack_clean/`（md5 与交接清单一致，`json.load` 正常，`layers=227`）；
  `pack_clean` 在跑完下面的测试后 md5 **未变** ⇒ 正常跑网不会改写容器，**改写只发生在那次崩溃前后**。

## 100 帧端到端：**确定性失败 2/2**（不是偶发）

```
[init] 100 帧输入
[init] in_sha1 预检: 100/100 一致        ← 输入侧没问题
[init] CTRL=0x01 VIDEO_MODE=0 0x2C基线=0 0x24=0
[init] arena 66.2 MB
Traceback (most recent call last):
  File "/tmp/run100.py", line 58, in <module>
    outs, st = net.run(x0_all[i], pipeline=True, nbuf=2, arena=arena)
  File ".../deploy/v60/ps/bin_net.py", line 3291, in run
    y, rs, t_e = be.finish(pl_task)
  File ".../bin_net.py", line 1085, in finish
    rs = e.reap(task.t0, task.timeout, out_buf=task.out_buf, L=task.layer, ...)
  File ".../bin_engine.py", line 1813, in reap
    BinGuard.check_canary(out_buf, L.out_bytes, "bin_unit OUT")
RuntimeError: bin_unit OUT: 金丝雀被写坏 8/4096 字节 ⇒ 引擎越界写，立即停机排查
```

**关键事实**：同样的 `net.run(..., arena=...)` 调用在 17:11（V-B2）和 17:20（V-C）**没有触发**金丝雀，
17:24 与 17:25 两次都触发 ⇒ **"写超 `out_bytes` 8 字节"这件事很可能一直存在，只是取决于缓冲尾部有没有正好贴着金丝雀**。
金丝雀 = 输出缓冲末尾 4096 B（`CANARY_BYTES=4096`），被写坏 8 B。

## 结论与阻塞

- V-C **通过**（v60 的重武装能力成立）。
- V-D **未取得任何数值**（段错误）。
- 100 帧端到端 **跑不起来**（被越界写保护拦下，2/2 复现）。
- **需要 RTL 方定位**：哪一层的输出写口多写 8 字节（很可能是行/尾对齐的最后一个元素）。
