# v41 RTL 存档（2026-11-02 01:45，主代理归档）

v41 的 RTL 改动此前**只存在于工作树**（`pynq_specialized_code/pl/rtl_hand/modules/bin_conv_core8.v`），
而 `runs/v41_archive/MD5SUMS` 只覆盖构建产物（bit/hwh/dcp/log），**没有 RTL**。
⇒ 在把 main 的 RTL 还原成 v39 之前，先在这里留一份，否则 v41 的 RTL 就永久丢了。

| 文件 | md5 | 说明 |
|---|---|---|
| `bin_conv_core8.v41.v` | `203d78ebdb1180ac7631d41e5af1cedf` | v41 版模块全文（234 行 diff 的"改后"侧）|
| `rtl_v41_vs_v39.patch` | `3dfccdb3c6401692fab30f2896c28412` | `git diff`（v39 → v41），234 行 |

## 为什么把 main 的 RTL 还原成 v39
`main` 必须与冻结基线自洽：基线跑的位流是 **v39 `602725b8`**。
若 main 的 RTL 停在 v41（`64ac3dd1`，**禁用**），下一个从 main 开副本的人会以为"当前设计 = v41"，
而 v41 上板会跑 23 s 硬挂（OOB 写踩内核内存，见 `docs/板端事故_内存踩踏_真相.md`）。

## v42（T-13）的起点
从这里取，不要从 main 取：
```
cd <repo> && git apply runs/v41_archive/rtl/rtl_v41_vs_v39.patch
```
⚠ v41 已知的 parity 约束（PLAN §69.1）：读通路 +1 拍 ⇒ 收尾必须 −1 拍，否则总拍数变。
