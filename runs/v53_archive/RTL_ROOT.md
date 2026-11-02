# v50 RTL 根 / 工程副本 事实源（T-65，2026-11-02）

> 这份文件回答三个问题：**v50 构建到底读哪份 RTL、读的是不是 v39 位流那份、proj_v50 是怎么来的。**
> 任何"我们以为的 RTL 根"都必须能在这里逐条对上 md5 —— 本项目 RTL 源已错三次
> （worktree 名 / commit / HEAD），所以判据只认文件级 md5，不认目录名。

---

## 1. RTL 根

| 项 | 值 |
|---|---|
| 根目录 | `runs/timing_v50/rtl_v50/` |
| manifest | `runs/timing_v50/rtl_v50_manifest.txt`（相对路径，25 条） |
| 冻结来源 | `runs/v42e_archive/rtl_v39_exact/`（v39 位流输入逐字节复刻） |
| 核对工具 | `pynq_specialized_code/pl/scripts/verify_rtl_root.sh` |
| 构建脚本里的变量 | `set rtl .../runs/timing_v50/rtl_v50`（`build_final_150v50.tcl`） |

### 1.1 与 rtl_v39_exact 的差异

**25 个 `.v` 全部逐字节相同（不是 23 个）。** T-64 的 16-lane 代码是**新增文件**，
放在 `rtl_v50/lane16/` 子目录，**不改这 25 个文件中的任何一个**。

任务书里"新增 lane16 文件 + wrapper 换名、其余 23 个逐字节不变"的落地方式：

- 25 个基线文件全部保留原样（含两个 8-lane wrapper：`bin_unit_axi_wrap.v` /
  `bin_unit_axi_wrap_bd.v`）。它们仍参与编译，但 v50 的 BD 不再例化它们（module-ref 换成
  `bin_unit_axi_wrap_lane16`），所以只是"留在根里的基线证据"。
- "wrapper 换名"体现为 lane16/ 里**新模块名** `bin_unit_axi_wrap_lane16`（及其子模块），
  而不是去编辑旧 wrapper 文件。这样 v39 基线保持 25/25 可复算，新老两版可同时存在。
- 因此冻结判据比任务书更严：**25/25 不变 + 新增文件全部入 manifest**，不是 23/25。

### 1.2 md5 表（25 个基线文件）

| 文件 | md5 | 角色 |
|---|---|---|
| `_selftest_bad.v` | `fcc96728b5c8334ea594063ff0629703` | 非构建输入（根里保留，证明 25/25 冻结） |
| `_selftest_ok.v` | `fcc96728b5c8334ea594063ff0629703` | 非构建输入（根里保留，证明 25/25 冻结） |
| `bin_catpack.v` | `cfa8132264cb22256379d5fc4bff80bd` | 构建输入（v39 位流 manifest 命中） |
| `bin_conv_core.v` | `acaa8540cd6e2701b370e0cff85a6821` | 构建输入（v39 位流 manifest 命中） |
| `bin_conv_core8.v` | `4e9887a9b125c61116ee3663ade425ce` | 构建输入（v39 位流 manifest 命中） |
| `bin_e1e2.v` | `28375eec288f067e9235c0ecd486abb5` | 构建输入（v39 位流 manifest 命中） |
| `bin_interp.v` | `fa4198e6cba9d4e65c58866bf278bc8c` | 构建输入（v39 位流 manifest 命中） |
| `bin_rprelu.v` | `18f77f2f2ceb153605063334060d2775` | 构建输入（v39 位流 manifest 命中） |
| `bin_sam.v` | `c9ef0488fd79ca36b321924f00c92d74` | 构建输入（v39 位流 manifest 命中） |
| `bin_unit_axi_wrap.v` | `0cf68b2e31ac106ed03e30bd5591504d` | 构建输入（v39 位流 manifest 命中） |
| `bin_unit_axi_wrap_bd.v` | `a9ac6fd7ba307cfa2c256fca776953cb` | 构建输入（v39 位流 manifest 命中） |
| `conv_int8_uni.v` | `c7913f58cfc5d7a46c104a7486134ac5` | 构建输入（v39 位流 manifest 命中） |
| `conv_k1_engine.v` | `6b759e48e5a223d6a103ec39a260969c` | 非构建输入（根里保留，证明 25/25 冻结） |
| `conv_k3_engine.v` | `900ffed925d68eddadde6fb3039166b5` | 非构建输入（根里保留，证明 25/25 冻结） |
| `conv_k5_engine.v` | `5d87182e92ec4c623782c096cb8979f6` | 非构建输入（根里保留，证明 25/25 冻结） |
| `conv_uni_axi_wrap.v` | `34012562d6aa1d3d919b372ae3adb618` | 构建输入（v39 位流 manifest 命中） |
| `conv_uni_axi_wrap_bd.v` | `0877f62db82d01a89bc3a61b4e617acd` | 构建输入（v39 位流 manifest 命中） |
| `k1_axi_wrap.v` | `1f7a6ae7755210ef593ac00f09a341ca` | 非构建输入（根里保留，证明 25/25 冻结） |
| `k1_axi_wrap_bd.v` | `3a3c04dfcbd20e57329a9b96e022a6dc` | 非构建输入（根里保留，证明 25/25 冻结） |
| `k3_axi_wrap.v` | `250cffbb6b9a4c19578a89a8248039d5` | 非构建输入（根里保留，证明 25/25 冻结） |
| `k3_axi_wrap_bd.v` | `d1e3973082e7fe4335178eccc33c2e7c` | 非构建输入（根里保留，证明 25/25 冻结） |
| `k3_cfg_check.v` | `fd8ac087100300c80dc01cb1584d0f84` | 非构建输入（根里保留，证明 25/25 冻结） |
| `k5_axi_wrap.v` | `5ef4b22d57f69ed15b9f163c34fc85fd` | 非构建输入（根里保留，证明 25/25 冻结） |
| `k5_axi_wrap_bd.v` | `8e141bde4675d060d2ffa822f1cc1ace` | 非构建输入（根里保留，证明 25/25 冻结） |
| `k5_cfg_check.v` | `21ae24b3f3c7c27ad5e0734881308014` | 非构建输入（根里保留，证明 25/25 冻结） |

两个 `_selftest_*.v` 的 md5 相同（`fcc96728`），是 rtl_v39_exact 里原有的、
**不参与构建**的 selftest 残留；保留它们只是为了让 25/25 的账对得上。

### 1.3 为什么这 12 个是"构建输入"

判据是 `pynq_specialized_code/board/overlay_engine_150mhz/build_v39_manifest.txt`
（v39 位流构建后生成，记录的就是该位流的实际输入）：12 条 RTL + `design_1.v` /
`design_1_wrapper.v` / build tcl。`build_final_150v50.tcl` 的 0a 段第一步就是拿
这个 manifest 逐条比 12 个 `.v`，**任何一条 DIFF 都拒绝构建**。

---

## 2. T-64 交付契约（lane16/）

v50 只能读 `runs/timing_v50/rtl_v50/lane16/*.v`。交付方（T-64）必须满足：

1. **文件名**：随意，构建脚本用 `glob lane16/*.v` 收；但必须定义模块
   **`bin_unit_axi_wrap_lane16`**（`bd_v50.tcl` 的 module-ref 与
   `build_final_150v50.tcl` 都会硬断言这个名字）。
2. **不许重名**：lane16 里的文件不得定义 12 个基线文件里已有的模块名
   （否则 Vivado 会静默择一；`build_final_150v50.tcl` 第 2 步会 `error`）。
   新核建议叫 `bin_conv_core16`（或任何带 16 的全新名字）。
3. **BD 外壳属性**：`bin_unit_axi_wrap_lane16.v` 的 `X_INTERFACE_PARAMETER` 必须写
   `FREQ_HZ 150000000`（**不是** 40000000 —— 老外壳写 40 MHz 曾直接把 BD 重建搞挂），
   且 `s_axi`/`m_axi` 的 `PROTOCOL AXI3`、`ADDR_WIDTH 32`、`DATA_WIDTH >= 64` 保持。
4. **不许窄于 8-lane**：`bd_v50.tcl` 会在重建前后对比 `s_axi/m_axi DATA_WIDTH`
   与 BD 引脚数，变窄就 `error`。
5. **落盘后刷新 manifest**（否则 0a 第②条会把新文件判成 EXTRA）：
   ```bash
   cd runs/timing_v50/rtl_v50
   { md5sum *.v; md5sum lane16/*.v; } > ../rtl_v50_manifest.txt
   ```
   刷新后 `verify_rtl_root.sh runs/timing_v50/rtl_v50_manifest.txt runs/timing_v50/rtl_v50`
   必须 `RESULT: PASS`（26+ 条 MATCH）。
6. **行为**：16-lane 必须真的把 8-lane 核换掉。构建日志里的综合后 DSP 数是最便宜的
   指纹：v39/v43base2 是 **78**（8-lane）；v50 应显著更大（≈2×）。若仍 ≈78，
   说明新外壳还在例化 8-lane 核 —— 停，不要上板。

---

## 3. 工程副本 proj_v50

| 项 | 值 |
|---|---|
| 路径 | `pynq_specialized_code/pl/vivado/proj_v50/` |
| 内容 | `proj_engine.xpr` + `.srcs` + `.gen` + `.ip_user_files`（34 MB；不含 `.runs/.cache/.hw`，由 Vivado 重建） |
| BD 根 | 2026-11-02 **13:17** T-63 加 fconv **之前**的 v43base2 快照 |
| BD 证据 | `design_1.bd` = `023aab9887312ab92e2d3e971196683a`；`design_1.hwh` = `6b552758649abb2ae58547709de0cb4c` |

### 3.1 为什么不是"原样 cp proj_v43base2"

T-65 开工时（15:03）`proj_v43base2` **已不是 build-ready 的 v43base2**：

- 13:17 T-63 在它上面执行 `bd_add_fconv_v44.tcl`，BD 里多了 `fconv_0` + 两个 `xlconst`，
  `.xpr` 里多了 11 个 `rtl_hand/_worktrees/T-62-v44/rtl_fconv/*.v` 源文件；
- 14:26 起 T-63 的 `build_final_150v44.tcl` 正在用这个工程跑实现（本轮还在跑）。

直接 `cp -a` 会得到一个 **带 fconv 的 v44 工程**，与"v50 不许带 fconv"直接冲突。
好在 T-63 自己的备份脚本在 13:17 拍了加 fconv 之前的 5 个文件
（`proj_v43base2/backup_bd/*.20261102_131705`），其中 hwh 的 md5
`6b552758` 与 `runs/v43base2_archive/design_1_150v43base2.hwh` **完全相同**，
也与 `runs/timing_v43base2/build_t60.log` 第 207 行记录的构建输入 hwh 相同
（`hwh=6b552758649abb2ae58547709de0cb4c（≠baseline）✔ 无 pc_conv_uni ✔ 无 fconv`）
⇒ 这 5 个文件就是 v43base2 位流实际用的 BD 状态。

### 3.2 还原动作（全部只发生在 proj_v50 里，源目录只读）

1. 拷贝 `proj_engine.xpr` / `.srcs` / `.gen` / `.ip_user_files`（不拷 `.runs/.cache/.hw`）；
2. 从 13:17 快照还原：`design_1.bd`（023aab98）、`design_1.hwh`（6b552758）、
   `synth/design_1.v`（773f8bfd）、`ip/design_1_ps7_0/{dcp,stub}`（b1141a7f / a1d79faf）；
3. 删除 fconv/xlconst 的 IP 目录（`.srcs` 与 `.gen` 各 3 个）、`mref/fconv_axi_wrap_bd`、
   过期 `sim/design_1.v`、两个 `design_1.bda`；
4. 从 `.xpr` 删掉 11 个 `rtl_fconv/*.v` 的 `<File>` 条目（删后 XML 仍可解析）；
5. 结果：`grep -ri fconv proj_v50` 只剩 `ipshared/.../blk_mem_gen_v8_4_vhsyn_rfs.vhd`
   里的一段加密 base64（误报）。

没有任何 fconv / HP2 进入 proj_v50；BD 仍是 5 个组件
（ps7 / rst_0 / axi_ctrl / conv_uni_0 / bin_unit_0），地址 0x40040000 / 0x40060000，
FCLK 150 MHz。`bd_v50.tcl` 打开它后只删/建 `conv_uni_0`、`bin_unit_0`。

---

## 4. 工具自检（B1 证据）

命令与完整输出在 `runs/timing_v50/`：

| 场景 | 命令 | 结果 | 退出码 |
|---|---|---|---|
| v39 正对照（严格） | `verify_rtl_root.sh build_v39_manifest.txt rtl_v39_exact` | `MATCH=12 DIFF=0 EXTRA=13` | 1（13 个非构建输入 `.v` 按规则算 EXTRA） |
| v39 正对照（放行） | 同上 `--allow-extra '*.v'` | `MATCH=12 DIFF=0 EXTRA=0` | **0** |
| 负对照：改 1 字节 | 同上，指向 `/tmp` 坏副本 | `MATCH=11 DIFF=1` | **1** |
| 负对照：删 1 文件 | 同上，删 `bin_sam.v` | `MISSING=1` | **1** |
| v50 冻结根 | `verify_rtl_root.sh rtl_v50_manifest.txt rtl_v50` | `MATCH=25 DIFF=0 EXTRA=0` | **0** |

> 严格模式对 v39 根 `rc=1` 是**设计如此**：v39 的位流 manifest 只列 12 个构建输入，
> 而目录里有 25 个 `.v`。`MATCH=12 DIFF=0` 证明 12/12 输入逐字节相同；多出来的 13 个
> 不参与构建，用 `--allow-extra` 显式放行后 `rc=0`。相关日志：
> `verify_selftest_v39_strict.log` / `verify_selftest_v39_allow.log` /
> `verify_negctrl_diff.log` / `verify_negctrl_missing.log` / `verify_rtl_v50_root.log`。

---

## 5. T-64 RTL 到位后还差什么才能构建

1. 把 T-64 的 `.v` 放进 `runs/timing_v50/rtl_v50/lane16/`（按 §2 契约）；
2. 刷新 `runs/timing_v50/rtl_v50_manifest.txt`（§2 第 5 条），`verify_rtl_root.sh` 必须 PASS；
3. 跑 `pl/scripts/bd_v50.tcl`（需要 Vivado，**一次一个**，走 memgate）：
   期望 `REGEN_V50_OK`、hwh 里 `PCW_USE_S_AXI_HP2=0`、地址 0x40040000/0x40060000 在位、
   `.gen` 里出现 `design_1_bin_unit_0_<N>`（module-ref 指向 lane16）；
4. 跑 `pl/scripts/build_final_150v50.tcl`（同样走 memgate，`V50_ASSERT_ONLY=1` 可先只验断言）：
   日志里必须看到 0a 两条 PASS、`lane16 ... 个模块`、综合后 DSP ≈ 2×78；
5. 产物落 `runs/v50_archive/` + `MD5SUMS` + `build_v50_manifest.txt`（脚本自动生成）。

> 本轮（T-65）**只搭架子，不构建**：`bd_v50.tcl` 现在会因为 `lane16/` 为空而停在
> 第一屏（硬断言），这是有意的 —— 绝不拿 8-lane 冒充 16-lane。
