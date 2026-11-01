# ①~④ 代码改动的**主机侧**证据（不是板端实测）

落盘时间：2026-11-02（北京时间 23:45 前后），由"基线可复现化工程师"生成。
**这里没有任何板端数字**；板端 ⑤⑥ 的数字见同目录上层（本批尚未产生）。

## 1. `old_HEAD_BIN_BILINEAR_C=1.json` / `new_optin_BIN_BILINEAR_C=1.json`

- **目的**：证明 ① 改默认值**没有**改变任何数值行为。
- **方法**：把 `git show HEAD:.../bilinear_c.py`（旧，md5 `48134b7b…`）与改后文件（新，md5 `cd43b1d6…`）
  各放一份到 `/tmp/vopt/{old,new}/`，**同一台主机（x86_64）、同一个 `.so`、同一组输入**
  （`default_rng(20261102)`，6 个形状含 4 个 FAST_SHAPES 与 2 个 EDGE_SHAPES），
  在 `BIN_BILINEAR_C=1` 下各跑一遍 `bilinear_f32_c`，比较输出 `md5`。
- **结果**：`old c_md5 == new c_md5` → **6/6 形状逐位相同**；
  同时两版 `selftest("full")`（29 形状）都通过，C 输出与 numpy 输出逐位相同。
- **口径**：主机 x86_64，不是板端 ARM；这是"代码等价性"证据，**不是**性能证据。

## 2. 默认值语义（同一探针 `ab.py`，`/tmp/vopt/new`）

| `BIN_BILINEAR_C` | `load()` | `why()` |
|---|---|---|
| 未设置 | `None`（走 numpy） | `BIN_BILINEAR_C 未设置（默认关/opt-in）` |
| `0` | `None`（走 numpy） | `BIN_BILINEAR_C=0（显式关）` |
| `1` | 内核已加载 | `ok`（fast 自检 6 形状通过、full 29 形状通过） |
| 未设置（旧 HEAD 代码） | **内核已加载** | `ok` ⇒ 旧默认确实是**开** |

## 3. `driver_dryrun.{log,json}`

- **目的**：在没有板子的主机上把 `board_v39_verify.main()` 的新代码路径**跑通一遍**
  （`pynq` 用桩模块、`Overlay/engine/net` 全假），防止把语法/属性错误带上板。
- **方法**：假 `Overlay`（`/tmp/vopt/stub/pynq`）、假 `BinConvEngine/BinNet/BinArena`、
  假 `step_load/step_addr/step_identity`；`--skip rlat,sweep`。
- **这些数字是假的（6.500 s 是桩里写死的），只用于验证"口径 a / 口径 b / runpoint / md5"这几条打印与落盘**。
- 验证到的点：`runpoint` = 位流 md5 + 五项代码 md5 + 11 项开关 + stamp；
  首个 run 有 `onetime_s` / `total_incl_onetime_s`，后续 run 只有 `total_s`；
  汇总同时打印 口径 a 与 口径 b；跑后 md5 与开跑时一致。
