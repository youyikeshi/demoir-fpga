# T-82 报告：D4（核重名）+ D5（BD/tcl 命名）+ D9（只读 magic `0x314`）

- 仓库 `R = /media/felix/Data/coding/demoir-fpga`，`git HEAD = 515cf29`（未提交任何 commit）
- 产物目录：`R/runs/v82_naming/`
- 口径：**只改名字 + 加一个只读常量**；未改任何逻辑/位宽/时序结构；未起综合/实现；未碰 PS、25 条基线、`proj_engine`/`proj_v43base2`

---

## 1. 改动清单（6 项，全部机械）

| # | 文件 | 改法 | md5 前 → 后 |
|---|---|---|---|
| 1 | `runs/timing_v50/rtl_v50/lane16/bin_conv_core.v` → `bin_conv_core_v50.v` | `git mv` 改名；`module bin_conv_core` → `module bin_conv_core_v50`；头注同步 | `4e6a4235…` → `61568f39…` |
| 2 | `runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v` | ① 例化 `bin_conv_core #(` → `bin_conv_core_v50 #(`（唯一一处例化，:1372）② 加 `RW_MAGIC`/`V50_CHAIN_ID` localparam + 读译码一行 | 拷入时 `ffd11d58…` → `b4896f26…` |
| 3 | `…/_worktrees/v50_impB/bin_unit_axi_wrap_v50.v` | 同 2（**让 IMP-B 续作不会把旧例化名再拷回来**） | `ffd11d58…` → `b4896f26…`（与 lane16 逐字节相同） |
| 4 | `pynq_specialized_code/pl/scripts/bd_v50.tcl` | 11 处 `bin_unit_axi_wrap_lane16` → `bin_unit_axi_wrap_bd_v50`（module-ref、断言、兜底 glob、错误文案、注释） | `274702bf…` → `5cb9946d…` |
| 5 | `pynq_specialized_code/pl/scripts/build_final_150v50.tcl` | 3 处模块名同步（`dict exists $_lane_mods …` + 错误文案 + puts），并改硬约束注释 | `c8335718…` → `d7a993a3…` |
| 6 | `runs/timing_v50/rtl_v50_manifest.txt` | 刷新：25 条基线（原顺序不动）+ 9 条 `lane16/`（含改名后的核） | `5a0f88ad…` → `d55ad00f…` |
| 附 | `docs/V50_ARCHITECTURE.md` | §9 表格 conv 核行改为 `bin_conv_core_v50`（并加"基线薄壳 `bin_conv_core` 冻结"行 + 改名理由）；§10.1 表加 `0x314 CHAIN_ID` 行 | `cd5df1a1…` → `a798ddec…` |

`lane16/` 拷入的 IMP-B 8 个文件（**原样，md5 未变**，`_worktrees/v50_impB/` 当时版本）：

```
daff1dd7…  bin_band_buf.v            e5d7b3b7…  bin_catpack_v50.v
314a6f18…  bin_interp_v50.v          dba8b606…  bin_rprelu_v50.v
8f242a27…  bin_sam_v50.v             191bda10…  bin_segq.v
8309b8e8…  bin_unit_axi_wrap_bd_v50.v
```

> ⚠ **待续作覆盖**：这 8 个文件是 IMP-B 续作正在改的那几个，本轮只拷"当前版本"；
> 续作落盘后必须重跑 `md5sum` 刷新 manifest，否则 `build_final_150v50.tcl` 第 0a② 步会判 DIFF。

---

## 2. D4：核改名（重名断言不再 error）

改名 diff 只有 2 行（`git diff -M`）：

```diff
-//  bin_conv_core.v —— 二值卷积核 v2（8/16 lane 参数化版）：**8 条并行 XNOR+popcount 通道**
+//  bin_conv_core_v50.v —— 二值卷积核 v50（8/16 lane 参数化版）：**8 条并行 XNOR+popcount 通道**
-module bin_conv_core #(
+module bin_conv_core_v50 #(
```

包装层唯一一处例化（`lane16/bin_unit_axi_wrap_v50.v:1372`）：

```verilog
        bin_conv_core_v50 #(                       // ← 原为 bin_conv_core #(
            .LANES    (LANES),          // popcount 位宽（v39 原样，恒 64）
            .COUT_MAX (COUT_MAX),       // v39 原样（核里未使用）
            .VLANES   (VLANES)          // ★ v50：真正的 lane 数（8 / 16）
        ) u_core (
```

**N6 双向证据**（`n6_dupname_check.tcl` = 从 build 脚本第 2 步原样摘出的断言逻辑，只去掉 `read_verilog`）：

- 改名后（`04_n6_dupname_ok.log`）：
  `lane16 新 RTL 9 个 / 9 个模块（含 bin_unit_axi_wrap_bd_v50），与基线无重名` → `OK（断言通过）`
- 反证：把核名退回 `bin_conv_core`（`04_n6_dupname_counterfactual.log`）：
  `lane16/bin_conv_core_v50.v 重定义了基线模块 'bin_conv_core'（已在 bin_conv_core.v 里）—— 新 RTL 必须换新模块名`
  ⇒ 与 `DEFECTS.md` D4 描述完全一致，说明断言确实生效、且现在被满足。

---

## 3. D5：BD/tcl 命名对齐 §9

§9 定：单元 `bin_unit_axi_wrap_v50` / **BD 外壳 `bin_unit_axi_wrap_bd_v50`**；并明写
"`bd_v50.tcl` 的 module-ref 必须指向 `bin_unit_axi_wrap_bd_v50`"。

`bd_v50.tcl`（N4-a）：

```
40:set _shell [file join $lane16 bin_unit_axi_wrap_bd_v50.v]
45:        if {[regexp {\mmodule[ \t]+bin_unit_axi_wrap_bd_v50[ \t(\r\n]} $_t]} { set _shell $_f; break }
96:foreach m {conv_uni_axi_wrap_bd bin_unit_axi_wrap_bd_v50} {
130:create_bd_cell -type module -reference bin_unit_axi_wrap_bd_v50 bin_unit_0
131:ok "bin_unit_0 已建（module-ref = bin_unit_axi_wrap_bd_v50）"
```

`build_final_150v50.tcl`（N4-b）：

```
206:if {![dict exists $_lane_mods bin_unit_axi_wrap_bd_v50]} {
207:    error "lane16/ 里没有模块 bin_unit_axi_wrap_bd_v50 —— bd_v50.tcl 的 module-ref 会找不到它"
209:puts "   lane16 新 RTL [llength $_lane16] 个 / [dict size $_lane_mods] 个模块（含 bin_unit_axi_wrap_bd_v50），与基线无重名"
```

旧名残留：`grep -c bin_unit_axi_wrap_lane16` 两个脚本都是 **0**。
（`build_final_150v50.tcl` 里对 `lane16/` 的 glob `[glob … $rtl lane16 *.v]` 本来就不写死文件名，
未改；重名断言 `_base_mods`/`_lane_mods` 列表逻辑一字未动，只是第 206 行的"必须存在的模块名"跟着改。）

两个脚本的 tcl 括号配平（`info complete`）：`BRACE-OK`。

---

## 4. D9：只读 magic `0x314 = 0x5030_0001`

`lane16/bin_unit_axi_wrap_v50.v:488-494`：

```verilog
    localparam [9:0]  RW_MAGIC = RW_CHAIN + 10'd20;        // 0x314 CHAIN_ID（只读 magic）
    // ★ D9（T-82）：0x314 只读 magic = "V50" + 版本 1 ⇒ 0x5030_0001。
    //   用途：PS 侧判定"链引擎在位"，**不可能被写回声骗过**（本地址写忽略，
    //   读回的是常量而不是寄存器）。只读、不参与任何逻辑、FUSE_MODE 无关
    //   （读译码只看地址，不看 r_fuse_mode）。
    localparam [31:0] V50_CHAIN_ID = 32'h5030_0001;
```

读译码 `:2957`：

```verilog
                RW_STAT:  rd_mux = {16'd0, seg_err_code, 5'd0,
                                    seg_busy, seg_err, seg_done};
                RW_MAGIC: rd_mux = V50_CHAIN_ID;      // 0x314 CHAIN_ID（只读常量）
                RW_ALO:   rd_mux = r_arena_lo;
```

- **只读**：写译码的扩展页 case（`:999-1004`）里**没有** `RW_MAGIC` 项，落到 `default: ;` ⇒ 写忽略。
- **不参与逻辑**：只有 `rd_mux` 一个消费者，没有任何 FSM/数据通路引用。
- **FUSE_MODE=0 可读**：`rd_mux` 的 `always_comb`（`:2895`）只依赖 `araddr_l`，不碰 `sel_fuse_q`/`r_fuse_mode`。
- **`0x250..0x25C` 仍空**：`grep -n "10'h25[048C]\|0x25[048C]"` 只命中两条**注释**（`:382`、`:997` 的禁区说明），无任何实现/引用。

> ⚠ **本改动在"单元层"（`bin_unit_axi_wrap_v50`，`V50_REG_BASE = 0x300`）上地址即 `0x314`。**
> 见 §6 第 1 条：BD 外壳默认 `V50_REG_BASE = 0x2E0`，会把整个块搬到 `0x2E0..0x2FC`（magic 落 `0x2F4`）
> ⇒ **按 §10.1 走 `0x314` 之前，外壳那个默认值必须先改。**

---

## 5. 验收结果

| # | 判据 | 结果 |
|---|---|---|
| N1 | `xvlog -sv` 对 `lane16/` 全部 9 文件 + 依赖（基线 `bin_conv_core.v`/`bin_conv_core8.v`/`bin_e1e2.v`/`bin_catpack.v`/`bin_sam.v`/`bin_rprelu.v`/`bin_interp.v`） | **0 warning 0 error**（`01_xvlog.log`，非 INFO 行 0 条；与基线 `bin_conv_core.v` 同编译无重名） |
| N2 | `xelab bin_unit_axi_wrap_bd_v50` `VLANES=8` / `VLANES=16` | **都 `Built`**（`elab_vlanes8`/`elab_vlanes16`）。仅有 1 条 `WARNING: [XSIM 43-3431]` = C 编译器**环境变量**提示，非 RTL |
| N3 | `verify_rtl_root.sh` | 严格清单（= 构建 0a② 步那条命令）：**`MATCH=34 DIFF=0 EXTRA=0 MISSING=0 RESULT: PASS`**；仅基线口径 `--allow-extra 'lane16/*'`：**`MATCH=25 DIFF=0`**（`ALLOWED_EXTRA=9`）—— 25 条基线 md5 与改前逐条相同 |
| N4 | 模块名与 §9 逐字一致 | 见 §3 grep；旧名命中 0 |
| N5 | `0x314` 读译码 + `0x250..0x25C` 仍空 | 见 §4 |
| N6 | 重名断言通过 | 见 §2 |

`lane16/` 现在 **9 个 .v**（1 核 + 8 个 IMP-B）。

> N3 口径说明：manifest 现在**必须**含 `lane16/` 的 9 条，因为 `build_final_150v50.tcl` 第 0a② 步
> 跑的是 `verify_rtl_root.sh $MAN50 $rtl`（**不带** `--allow-extra`），未入账的 `lane16/*.v`
> 会被判 EXTRA ⇒ 构建硬失败。所以落盘清单 = 25 + 9 = 34 条（`MATCH=34`），而同一次跑里
> **25 条基线全 MATCH DIFF=0**；只算基线的 `MATCH=25` 口径另存在
> `runs/v82_naming/manifest_baseline25.txt` + `09_verify_baseline25_allowextra.log`。

---

## 6. 顺带发现（**未改**，需 REVIEW/IMP-B 拍板）

1. 🔴 **BD 外壳默认 `V50_REG_BASE = 16'h02E0`，与单元/契约的 `0x300` 不一致**
   - `lane16/bin_unit_axi_wrap_bd_v50.v:29` `parameter int V50_REG_BASE = 16'h02E0`，
     `:126` 又 `.V50_REG_BASE (V50_REG_BASE)` 透传给单元（单元默认 `0x0300`，`:383`）。
   - `bd_v50.tcl` **没有**用 `set_property CONFIG.V50_REG_BASE` 覆盖 ⇒ 走 BD 建出来的位流里，
     `CHAIN_HEAD` 在 `0x2E0`、`CHAIN_STATUS` 在 `0x2F0`、**magic 在 `0x2F4`**，PS 读 `0x314` 得 0。
   - IMP-B 报告 `REPORT_v50_impB.md:47` 自己写的是"默认 `0x300`" ⇒ 外壳这行是遗留笔误。
   - 建议修法（一行，不属本轮授权）：`:29` 改 `16'h0300`。§10.1/§9 与 PS（`bin_engine.py` 的 `0x300/0x314`）才对得上。
2. 🟡 `lane16/bin_unit_axi_wrap_v50.v` 里还有若干**注释**写着 `bin_conv_core` / `bin_conv_core.v`
   （`:14/:72/:222/:244/:365/:526/:1109/:1214/:1320/:2486`）。本轮按"只改名字"的口径**只改了例化**，
   注释未动（其中 `:7/:13` 指的是 v39 的 1-lane 薄壳，本就不该改）。
3. 🟡 `bin_conv_core_v50.v` 里的 `$display` 标签仍是 `[bin_conv_core]`（`:898/:903/:913/:918`）——
   若已有日志抓取 grep 这个标签，改名会破坏比对，故**故意不动**；要改请统一处理。

---

## 7. 产物清单（`runs/v82_naming/`）

```
00_pre_md5.txt                        改前 md5（lane16/impB/脚本/manifest/docs）
01_xvlog.log                          xvlog -sv（16 文件，0 warn 0 err）
02_xelab_vlanes8.log / 02_xelab_vlanes16.log
04_n6_dupname_ok.log                  重名断言：通过
04_n6_dupname_counterfactual.log      重名断言：退回旧名的反证（硬 error）
05_diff_core.txt                      核改名 git diff（2 行）
06_post_md5.txt                       改后 md5
07_n4_grep.txt                        §9 名字 grep（两脚本）
08_verify_strict.log                  MATCH=34 DIFF=0 PASS（构建用这条）
09_verify_baseline25_allowextra.log   MATCH=25 DIFF=0 PASS
manifest_baseline25.txt               仅 25 条基线（给上面那条用）
n6_dupname_check.tcl                  断言逻辑摘录（tclsh 可跑）
replay.sh                             一键复现上面全部检查
```
