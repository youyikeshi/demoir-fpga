# E1E2 驱动接入报告（`BIN_PACK=e1e2`，默认仍走 C 内核）

> 日期：2026-10-31
> 工作目录：`pynq_specialized_code/`
> 约束：**未改 RTL、未跑 vivado、未上板**；现有 conv 路径未改一行；默认行为 A/B 逐字节相同。

---

## 0. 结论

| 项 | 结果 |
|---|---|
| 驱动实现 | `bin_engine.py` 新增 `E1E2Program` + `E1E2Engine`；`BIN_PACK=e1e2` 才挂上，默认 `_e1e2=None` |
| 全量程序模式 | `load_program()` 一次搬 **127,504 B**（desc 44,672 + thr 82,816 + 哨兵 16）；`run_all()` 698 条命令 |
| 离线判据 A | **227/227 层、698/698 命令、19,067,392 B、0 差异**（描述符路径 vs C 内核） |
| 离线判据 B | **112/112 个全段 d=0 层**复现 `ab_pack256.json` 的 ref/numpy/C **三列 sha1** |
| 离线判据 C | 驱动寄存器序列/平移/打包/开关在 FakeE1E2Unit 上全部 PASS；`run_all` 拍数 **21,930,932**（与成本模型 0 差） |
| 默认路径 A/B | `pack_input` 与改动前备份 5 层逐字节相同（0 差异）；`bin_engine --selftest` 全过 |
| 板端脚本 | `ps/pynq/board_e1e2_smoke.py`，默认 `--dry-run`；`--run` 用 `download=False` 接手 PL |

**关键教训（已修）**：RTL 的 AP_CTRL start 写只在 `!run_active` 时登记，
跑着时写会被**直接丢掉、不排队**。驱动在每条 E1E2 命令前显式 `_wait_idle()`，
并拒绝覆盖上一跑的残留错误码；否则会把上一层 conv 的 done 当成 E1E2 完成（静默错数据）。

---

## 1. 交付文件

| 文件 | 作用 |
|---|---|
| `docs/E1E2_DRIVER_MAP.md` | **阶段 1**：模块地图 + 数据流（只读分析） |
| `ps/pynq/bin_engine.py` | `E1E2Program` / `E1E2Engine` / `enable_e1e2()` / `BIN_PACK=e1e2` 分派 |
| `ps/pynq/e1e2_sim.py` | `bin_e1e2.v` FSM 的逐状态 Python 模拟 + float→int8 量化 |
| `ps/tools/verify_e1e2_path.py` | 阶段 3 的 A/B/C 三层离线证明；落盘 `runs/e1e2_driver/` |
| `ps/pynq/board_e1e2_smoke.py` | 阶段 4：板端 5 条真命令 + DBG 读带宽；每行 fsync |
| `runs/e1e2_driver/verify_e1e2.md` | 人看的逐字节验证表（含全部 9 个 cin=544 层） |
| `runs/e1e2_driver/layers_a.tsv` | A 的全部 227 行（层号/命令数/字节/差异） |
| `runs/e1e2_driver/verify_e1e2.json` | 结构化结果 |

备份：`ps/pynq/bin_engine.py.bak_e1e2_20261031`（改动前，sha1 `815e9b4e…`）。

---

## 2. 驱动 API 与数据流

### 2.1 资产解析（`E1E2Program`）

读 `deploy/e1e2_desc/{desc.bin,cmds.bin,thr.bin,frame.json}`，启动时做结构自检：

- `cmds.bin` 与 `frame.commands` 698 条逐字段一致；
- 真实 lane 的 `src` 落在某个 int8 buffer 内；
- 全掩码 lane 的 `thr` **精确指向** `thr.bin` 末尾的哨兵（546 条）；
- `thr.bin` 长度 = 82,816 = 从 `thr.bin_off` 到哨兵地址的完整 span；
- 40304 个真实通道的 `thr` 值 = `closed_form(d=fs_in−buf.fs, t_q)`（0 不一致，验证工具里跑）。

**地址平移**：`arena.base` 是 `0x08000000`，板上实际 CMA 地址几乎不可能相同。
`program.relocate(delta)` 对：
- `desc.bin` 每个字的低 32 位（src）与高 32 位（thr）各 +delta；
- `cmds.bin` 的 `base_desc/base_out` +delta。

`load_program()` 把平移后的 desc/thr 写进 arena 并补 16 B 哨兵
（**8 × int16(-127)**），再核对平移后哨兵引用数 == 546。

### 2.2 寄存器序列（每条命令）

```
0x200 R_RUN_UNIT = 4            （每条；影子缓存，实际只写一次）
0x18/0x20/0x28 C/H/W            （影子缓存；只有跨层形状变化才写）
0x204 R_E_GROUP = cfg_group     （grp8 变通时是 7，不是 8）
0x208 B_E_DESC  = desc_pa + i*64
0x210 B_E_OUT   = base_out
--- _wait_idle()：轮询 AP_RETURN.busy，直到 0；残留 err != 0 直接抛 ---
0x00 AP_CTRL = 1                （force，自清位）
轮询 0x10 AP_RETURN.bit0；err=(st>>8)&0xFF != 0 ⇒ BinRunError
读 0x230..0x240 DBG_E_*（每条命令 start 会清计数器 ⇒ 逐条累加）
收尾 R_RUN_UNIT = 0（force；run_layer/run_all 的 finally）
```

实测 698 条命令的 MMIO 写 **2,943 次**（影子跳过 2,642 次）⇒ 约 **4.2 写/命令**，
与 RTL 报告的 "5~60 ms MMIO" 预算一致。

### 2.3 两种用法

**A. 全量程序模式**（交付物原样）：

```python
ee = E1E2Engine(core)            # core = BinConvEngine(ov)
ee.probe()                       # 写4读4/写0读0；旧比特流直接拒绝
info = ee.load_program()         # 分配 61.15 MB arena + 搬 127.5 KB（一次）
ee.write_int8("encoder.encoder_1.rdb.conv_layers.0.conv", x_q)  # C8
ee.run_layer("encoder.encoder_1.rdb.conv_layers.0.conv")
# 或 ee.run_all()
print(ee.dbg_summary(fclk_mhz=100))
```

**B. prologue 打包模式**（`BIN_PACK=e1e2` 的接缝）：

```python
e = BinConvEngine(ov)            # 环境变量 BIN_PACK=e1e2
y = e.pack_input(x_float, L)     # 第一次调用自动 enable_e1e2() + probe()
# 内部：x_q = sat8(rint(x·2^fs_in)) → E1E2 d=0 小程序 → cb*H*W*8 字节
```

d=0 时 `THR ≡ t_q`，判据 `x + t_q > 0 ⟺ x >= 1−t_q`，与 C 内核
`bpack_i8(x_q, 1−t_q)` **逐字节相同**；`pack_int8` 用连续 C8 输入 +
`ceil(cin/64)` 条命令，cin=544 时第 9 组自动走 `cfg_group=7`。
返回的是内部缓冲的**只读视图**，在下一次 pack 前有效（`push_input` 立即 memcpy）。

开关语义：
- 默认 `BIN_PACK` 未设 ⇒ `_e1e2=None`，`pack_input` 走原 C 内核（A/B 已证 0 差异）；
- `BIN_PACK=e1e2` ⇒ 第一次 `pack_input` 时 probe + 挂载；
- probe 失败（旧比特流 0x200 页读 0）⇒ 抛 `RuntimeError`，**不会把失败的引擎挂上**；
- `disable_e1e2()` 摘掉后端并把 `R_RUN_UNIT` 写回 0。

---

## 3. 阶段 3 验证结果（逐字节）

### 3.1 A. 描述符路径 vs C 内核（全部 227 层）

- 数据：每层每个源 int8 buffer 随机 + 极值（−128/127/0）；同一份源走两条路：
  - 描述符路径：`desc.bin` + `thr.bin` + 哨兵，按 `tile→lane→p→bit→flush` 模拟；
  - C 内核：`x_q = sat8(rint(src·2^d))`，`bpack_i8(x_q, 1−t_q)`。
- 结果：**227/227 层、698/698 命令、19,067,392 B、差异 0**（1.7 s）。
- 覆盖：`cin=544/cb=9`（全部 9 层）、`cin=48`（50 层）、`{8,16,32,64,128}²`、
  `k=1/k=3`、段数最多 11、d∈{−3..+1}、grp8 变通 9 条。
- 表见 `runs/e1e2_driver/verify_e1e2.md` 与 `layers_a.tsv`。

### 3.2 B. 复现 `runs/ps256/ab_pack256.json` 三列 sha1

- 条件：该层所有段的 `d == 0`（源 buffer 标度 = 消费者 fs_in），共 **112 层**。
- 数据：按 `ab_pack.py::make_data` 的 seed/数据（含阈值、±1 ULP、±0、±inf、NaN）
  重放，直接量化到 fs_in 后走描述符路径。
- 结果：**112/112 层**，本次 sha1 == `sha_ref` == `sha_np` == `sha_c`。
- 例：`encoder.encoder_1.rdb.conv_layers.0.conv` → `76b616158363c007`（与 JSON 一致）。

> 说明：ab_pack 的 NaN 在 float 路径判 False；int8 量化对 NaN 显式置 −128
> （numpy 2.2.6 默认 cast 成 0，t_q>0 时会变 1），这样 int8 路径才与 float 路径一致。

### 3.3 C. 驱动代码（FakeE1E2Unit：寄存器级模型）

| 测试 | 结果 |
|---|---|
| `R_RUN_UNIT` 写 4 读 4 / 写 0 读 0 | PASS |
| `load_program`（delta=0） | arena `0x08000000`，desc/thr/哨兵 = `0x8000000/0x800ae80/0x801f200`，127,504 B，memcpy 0.06 ms |
| `run_layer` 5 层（544/48/368/240/192，含 786,432 B 大层） | 命令 9/1/6/4/3，字节全对，差异 0 |
| 平移 `delta=+0x18000000` 后 `run_layer` | 差异 0 |
| 自动分配 arena（板端 `_pynq_allocate` 路径） | pa=`0x30000000`，delta=`+0x28000000`，哨兵 546 |
| `run_all()` 698 条命令 | rd=19,084,144（期望 19,084,144）、wr=2,383,424、**cyc=21,930,932（与成本模型 0 差）** |
| `pack_int8` 5 层（544/512/48/32/64） | 字节差异 0 |
| `pack_int8` 合成 `cin=52`（非 8 倍数，en 尾部掩码） | 512 B，差异 0 |
| `BIN_PACK=e1e2` 的 `pack_input` | 差异 0；收尾 `R_RUN_UNIT=0` |
| start 闸门（busy 中等待 / 残留 err） | PASS：busy 时等待；残留 err 抛 `BinRunError` |
| 旧比特流（0x200 读恒 0） | PASS：拒绝，且不挂失败引擎 |

### 3.4 默认路径 A/B

- `ps/pynq/bin_engine.py.bak_e1e2_20261031` 与新版 `pack_input` 在 5 层
  （544/48/368/32/512）上逐字节相同，差异 0。
- `python3 bin_engine.py --selftest`：全部通过（conv 的 5 个 xsim golden 仍逐字节）。
- `python3 bin_net.py --dry-run --hw 32`：整网参考后端跑通（未改 `bin_net.py`）。

---

## 4. 板端脚本

```bash
# 1) 先预演（不碰硬件；验证小镜像 FSM == C 内核）
python3 ps/pynq/board_e1e2_smoke.py --dry-run

# 2) 真跑（PL 已经加载好含 E1E2 的 wrapper；不下载比特流）
python3 ps/pynq/board_e1e2_smoke.py --run --fclk 100
#   --bit design_1.bit：只用来读 ip_dict，Overlay(..., download=False)
#   --unit 0 --n 5 --timeout 20 --log smoke.log
```

脚本行为：
1. `Overlay(bit, download=False)` 接手已加载 PL；`BinConvEngine` 从 `ip_dict` 取基址。
2. `probe()`：`R_RUN_UNIT` 写 4 读回、写 0 读回；失败直接退出码 2（不会假装成功）。
3. 从**交付的 `cmds.bin`** 里选 5 条 8²/16² 真命令（含 cin=544 的 grp8 变通，
   和 cin=512/48 与 16² 各一条），把 src 重定位到本地小缓冲，
   `thr` 逐 lane 复制交付物里的 16 B（保留真实阈值/哨兵）。
4. 启动前 `BinGuard.check_region` 把 desc/out/src/thr 全部过一遍。
5. 每条命令：`run_one` → 读输出 → 与 C 内核 `bpack_i8` 逐字节比 → 读 `DBG_E_*`。
6. 每行 `flush + os.fsync`；收尾 `R_RUN_UNIT` 写回 0 并读回。
7. 打印：
   - `rd/cyc`、`读带宽 = rd×8B×fclk/cyc`、`rdst/cyc`（读 miss 冻结）、`wrst/cyc`；
   - 全网外推：`计算 = 21,930,932/fclk`，`读 = 152.54 MB / 实测带宽`，取 max；
   - 0.15 s 门槛需要的 `rd/cyc`。

> `--dry-run` 实测：5/5 命令模拟 == C 内核；预测 6,671 拍 / 5,752 读拍。

---

## 5. 不确定 / 未验证（诚实清单）

1. **未上板**：没有真实 AXI 时序/DDR/CMA 数据。`load_program` 的 61.15 MB 连续分配
   在 1 GB 板子上是否成功、`rd/cyc` 实际多少，只能由 `board_e1e2_smoke.py --run`
   回答。板端结果请以 DBG_E_* 为准，不要引用本文的 0.26 s/0.15 s 外推当实测。
2. **全量程序模式的 d≠0 语义**：它要求每个源 buffer 已经是"生产者标度"的 int8。
   对 conv 输出这是 bin_net 原本就有的；对 14 个被不同 fs_in 读的 PS 物化张量，
   较粗消费者会有 ≤½ LSB 的二次舍入（交付物 README §6.2 已声明）。因此
   阶段 3 的判据明确为"**同一 int8 源**下的 C 内核"，而不是"从 float 直接量化"。
3. **`BIN_PACK=e1e2` 与单实例流水**：E1E2 pack 和 conv 抢同一个 `bin_unit`；
   `_PlBackend.prepare` 里启动 E1E2 时若上一层 conv 还在跑，`_wait_idle` 会等它结束
   ⇒ **正确但不再重叠**。所以这个开关不等于报告里的 0.26 s 全流水；要拿全流水，
   需要 227 层 int8 全在 DDR 的 arena 流程（本报告的模式 A 提供了引擎，尚未重排 bin_net）。
4. **`R_RUN_UNIT` 旧比特流**：0x200 页在旧 RTL 里是保留页（写忽略、读 0）。
   驱动已用 probe 硬拒绝；但**比特流必须包含本次合并的 wrapper**（IP repo 里那份
   旧副本要先整份覆盖，见 RTL 报告 §6.9）。
5. **NaN**：真实激活无 NaN；若出现，float C 内核判 False，而 numpy 的 NaN→int8
   是平台相关。`e1e2_sim.quantize_int8` 显式把 NaN 置 −128（判 False），
   与 C 内核浮点路径一致；但直接用 `sat8(np.rint(...))` 的旧代码在 NaN 上不保证。
6. **未跑综合/时序**：150 MHz 未收敛是已知现状；0.15 s 还要求 PL 持续读 ≥1.03 GB/s。
7. **`bin_net.py` 在我工作期间被另一路修改**（sha1 `ce2bf28f…` → `2c64dd07…`，
   mtime 04:51:06）；**我没有改它**。我用修改后的版本做了 `bin_net --dry-run --hw 32`
   回归并通过；`bin_engine` 的默认 A/B 是对改动前备份做的。
8. **115 个 d≠0 层没有进 B**：`ab_pack256` 的 sha 是"单标度（d=0）"口径；
   这些层的等价性由 A 覆盖（同一份 producer int8 源 vs C 内核），但拿不到
   ab_pack 的历史 sha 做第二重独立对照。

---

## 6. 复现

```bash
cd pynq_specialized_code

# 阶段 3（无板，约 4 s）
python3 ps/tools/verify_e1e2_path.py
cat runs/e1e2_driver/verify_e1e2.md

# 默认 conv 路径回归
python3 ps/pynq/bin_engine.py --selftest
python3 ps/pynq/bin_net.py --dry-run --hw 32

# 板端
python3 ps/pynq/board_e1e2_smoke.py --dry-run
python3 ps/pynq/board_e1e2_smoke.py --run --fclk 100
```
