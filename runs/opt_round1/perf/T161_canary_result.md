# T-161：金丝雀移到 `out_bytes` 之后（可定位化）—— patch + 主机自检结果

**2026-11-04 18:2x** ｜ **只改了 `/tmp/T-161-canary/` 的副本**（生产 `ps/pynq/` **一个字没动**）
**结论**：patch 生效且**不误报**；**故意多写 8 B 必被抓，报文带「层名 + 首坏偏移 +0」**；
边界（`out_bytes+4096 > len`）自动回落老行为、不崩。**可以交板端单帧验证。**

---

## 1 三行 patch（完整 diff：`T161_canary.patch`，4 个 hunk / +45 行）

| # | 文件:行（patch 后） | 改什么 |
|---|---|---|
| **1** | `bin_engine.py:203-244` | 新增 `BinGuard.canary_start(buf, n_used)`；`fill_canary(buf, n_used=None)` / `check_canary` 改为**按 `n_used` 起算**；`check_canary` 报错增加 **`首坏偏移 +N`** 与 `前 16 个` 下标 |
| **2** | `bin_engine.py:1575`（`run`）+ `:1593`（`run` 的 check）+ `:1840`（`reap` 的 check） | 传 `need_out` / `L.out_bytes`；`tag` 改成 **`"bin_unit OUT[层名]"`** |
| **3** | `bin_net.py:1023`（`_PlBackend.prepare`，**流水路径**） | `fill_canary(outb, L.out_bytes)` —— 两处 fill 都改，否则流水路径还停在"只保护 3 层" |

**回落规则**（保留兼容）：`n_used is None` / `n_used < 0` / `n_used+4096 > len(buf)` ⇒ 金丝雀仍钉**槽尾**
（老行为），`检查` 里"槽太小"仍抛 `ValueError`。

反例（一眼看懂为什么必须改）：

```python
# 老: start = len - 4096            # OUT 槽 = max_out+4096 ⇒ 只有 out_bytes==max_out 的层受保护
# 新: start = n_used if 放得下 else len - 4096
```

---

## 2 主机自检结果（`test_canary_t161.py`，纯 numpy + 寄存器级 `FakeUnit`，不需要板）

命令：`cd /tmp/T-161-canary && python3 test_canary_t161.py` ⇒ **全过（15 项）**

| 组 | 验的是什么 | 结果 |
|---|---|---|
| 1 | `out_bytes(1 048 576) < max_out(1 572 864)` 的层：填/查区间一致 | ✓ **不误报** |
| 2 | **变异：贴着 `out_bytes` 多写 8 B** | ✓ 报 `金丝雀被写坏 8/4096`、**首坏偏移 +0**、下标 `[0..7]` |
| 2' | 变异：多写 100 B | ✓ 报 `100/4096`、偏移 +0 |
| 3 | **对照（复刻老逻辑）**：同一个 +8，老写法 | ✓ **静默通过 0/4096**（就是 224 层漏报的机理）；patch 版在**同样布局**下能抓到 |
| 4 | 边界：`len == out_bytes+4096` 恰好 | ✓ 起点=槽尾=out_bytes，+8 仍被抓 |
| 4' | 边界：`n_used=-1` / 超界 | ✓ 回落槽尾、不崩、不误报 |
| 4'' | 边界：槽比 `out_bytes+4096` 还小 | ✓ 抛 `ValueError`（与老行为一致，**不是静默通过**） |
| 5 | **端到端**：`BinConvEngine.run` 走一遍真驱动（`FakeUnit` 寄存器级模型，第 0 层 8×8，槽做成"大槽装小层"） | ✓ 不误报；金丝雀区间 0 字节被动；金丝雀之后 524 288 B **一个字节没动** |
| 5' | 端到端**变异**：让 FakeUnit 多写 8 B（模拟 RTL 真 bug） | ✓ 抓到，报文：<br>`bin_unit OUT[encoder.encoder_1.rdb.conv_layers.0.conv]: 金丝雀被写坏 8/4096 字节（金丝雀起点 = len-528384 = 4096；首坏偏移 **+0**，前 16 个：[0,1,2,3,4,5,6,7]）` |

> 端到端用的是 `FakeUnit`（`bin_engine.py:4590`，寄存器级 RTL 模型，**不是 RTL 验证**）——
> 它证明的是"驱动 + 护栏 + 填/查区间"自洽，正好覆盖本次改动的落点。

---

## 3 那 3 层的完整几何（给 RTL 代理对仿真形状）

**在 256² 整网里**，`max_out = 1 572 864 B`，恰好 3 层并列（都在 decoder_1 的 128×128 段）：

| 层名 | cin | cout | k | s | d | pad | H→Ho | cb | cg | act_bytes | **out_bytes** | shift | fs_in | fs_out | K |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `decoder.decoder_1.rdb.conv_layers.0.conv` | 64 | 96 | 3 | 1 | 1 | 1 | 128→128 | 1 | 12 | 131 072 | **1 572 864** | 6 | 3 | 3 | 9 |
| `decoder.decoder_1.rdb.conv_layers.1.conv` | 160 | 96 | 3 | 1 | **2** | **2** | 128→128 | 3 | 12 | 393 216 | **1 572 864** | 7 | 3 | 2 | 27 |
| `decoder.decoder_1.rdb.conv_layers.2.conv` | 256 | 96 | 3 | 1 | 1 | 1 | 128→128 | 4 | 12 | 524 288 | **1 572 864** | 6 | 2 | 3 | 36 |

* `out_bytes = cg×8×Ho×Wo = 12×8×128×128 = 1 572 864`；`out_bytes % 8 == 0`、`% 4096 != 0`。
* 次大一档 `1 048 576 B` ⇒ **空档 524 288 B**（这就是 224 层"越界 8 B 也看不见"的原因）。
* ⚠ **同一组权重在别的输入尺寸下 out_bytes 会变大**：把它们按 `H=W=256` 造层 ⇒
  `out_bytes = 12×8×256×256 = **6 291 456 B**`（正是 RTL 代理仿真里"最大层 6 291 456 B"那个数）。
  两处都要能对上：**整网 256² 时它们跑在 128×128**，仿真用的是 256×256 的单层形状。

---

## 4 交板端单帧验证的三步（我这边不改生产代码，等合并）

```bash
# 1) 把副本覆盖上去（先备份）
cd /home/xilinx/demoir/ps/pynq
cp bin_engine.py bin_engine.py.bak_T161; cp bin_net.py bin_net.py.bak_T161
cp <this_repo>/runs/opt_round1/perf/T161_bin_engine.py bin_engine.py
cp <this_repo>/runs/opt_round1/perf/T161_bin_net.py    bin_net.py
md5sum bin_engine.py bin_net.py   # 期望 e6a665f2… / 50d46e05…

# 2) 单帧（能看到"层名 + 偏移"就算成功；不报错=不误报）
BV_FREE=1 python3 board_verify_numfix.py --pack deploy/bin_weights_new \
  --input demo/frozen256/p0000_crop256.npy \
  --golden demo/frozen256/golden/p0000_out1_fp.npy --bit design_1_v60.bit

# 3) 若报警：报文里会直接给  bin_unit OUT[<层名>] ... 首坏偏移 +N
#    +0     ⇒ 越界就发生在该层 out_bytes 之后（最有价值的一种）
#    +4088  ⇒ 写到"槽尾之后"（desc/scratch 方向的写者）
```

判据：
* **不误报** ⇒ 单帧跑通、输出 sha1 与 `28cd6860…` 那一档一致；
* **报警** ⇒ 记下 `层名 + 偏移`，这就是定位结论（RTL 侧只需仿真那个层/形状）；
* 若 224 个"小层"里也报警 ⇒ 之前的 8 B 越界**不是 max_out 层专属**，范围要扩大。

---

## 5 产物与 md5

| 文件 | md5 | 说明 |
|---|---|---|
| `T161_canary.patch` | `51cad423a9fe0915da0a912ed04557d9` | 完整 diff（102 行） |
| `T161_bin_engine.py` | `e6a665f26ed4ab348ab3fdaa6aa6a778` | patch 后的整文件（供板上直接覆盖） |
| `T161_bin_net.py` | `50d46e05484332b166cf9b692c53007d` | 同上 |
| `test_canary_t161.py` | `432b26b31fc002336305f29291c1a65e` | 主机自检（15 项全过） |
| 生产文件（**未改**） | `bin_engine.py` `f3fca8a6…`、`bin_net.py` `c95d8154…` | 与 `deploy_v60/` 里的一致 |

**改动量**：2 个文件、4 个 hunk、**+45/−14 行**；无新增依赖；`BIN_*` 开关一个没加（默认行为改变仅在
"金丝雀位置"这一处，且是**收紧**护栏，不改任何数值路径）。
