# T-90 独立验收（只读）—— T-89 第二阶段产出

- 时间：2026-11-02 17:28–17:40
- 被测快照（= 当前文件，逐字节相同）：`runs/v90_stage2/verify/snapB/`
  - `lane16/bin_segq.v` md5 **266454b8**
  - `lane16/bin_unit_axi_wrap_v50.v` md5 **0f7dd0ae**
  - `lane16/bin_band_fill_v50.v` md5 `1b0407bd`（未改）
  - `ps/pynq/bin_engine.py` md5 **48548b8d** / `ps/pynq/bin_net.py` md5 **68343d5c**
- 只读声明：未改 `lane16/`、PS、docs 任何文件；产物全部在 `runs/v90_stage2/`。
  改前/改后 mtime+md5 见 `verify/mtime_start.txt` 与 `verify/mtime_end.txt`。

## V1 四条 FSM 路径重跑（语义"改前 == 改后"）

| 被测 | 日志 | 结果 |
|---|---|---|
| 新 segq `266454b8` | `verify/v1/xsim_stdout.log` | **21 ok / 0 fail，B3 PASS** |
| 旧 segq `191bda10`（`v88_wiring/bin_segq.v.before`） | `verify/v1_old/xsim_stdout.log` | **21 ok / 0 fail，B3 PASS** |
| 两份检查行逐字 diff | `verify/A_new.txt` vs `A_old.txt` | **IDENTICAL（0 行差异）** |

⇒ 推进 / 链尾 / 出错即停 / 重启 四条路径的判据在 4 行索引修正前后**逐字相同**。
（T-89 另用扩展 TB 报 28 ok/0 fail vs 旧 22/6 —— 多出的 7 条正是 V2 的三类新行为，
与本报告的 V2 独立结论一致。）

## V2 越界 `out_pa` ⇒ E_RANGE（本轮最重要判据）

`verify/v2/xsim_stdout.log`（`tb_v90_range.v`，16 项检查，0 fail）：

| 用例 | 条件 | 结果 |
|---|---|---|
| A | `out_pa=0x8000` 越出 arena[0x2000,0x4000) | **err=1, code=2 (E_RANGE)**，`n_wr==0`（一个写都没发），done=1 |
| B | `in_pa=0x8000` 越界 | err=1, code=2，`n_wr==0` |
| C | 合法；**cin=0 / flags=0** | err=0，progress=1，done=1；**首条 `st_wa == out_pa(0x3000)`**、末条 0x3018 ⇒ out_pa 真的落地（旧版写 cin=8） |
| D | 回落窗口 0x3000_0000..0x3FFF_FFFF 内描述符 + 窗口外 out_pa=0x2000_0000 | err=1, code=2 |
| E | 同窗口内 out_pa=0x3002_0000 | err=0, done=1, 首条 st_wa = out_pa |

**V2b：ARENA 默认值 ⇒ 拒绝启动**（`verify/v2b/xsim_stdout.log`，wrap 级 TB）：

| 判据 | 结果 |
|---|---|
| A2 arena 默认（0 / 0xFFFF_FFFF）时 `m_axi_arvalid` 恒 0 | **PASS**（kick 被门控，一个读都不发） |
| A3 `AP_RETURN[15:8] == 2 (E_RANGE)`（§15.2 声称） | **FAIL：读到 0** |
| B1/B2 写完 arena 后 `m_axi_arvalid` 拉起、ARADDR=0x3000_1000 | PASS |

⇒ **发现 ②**：`u_err` 里确实 OR 了 `arena_err`（wrap:1686），但
`err_show = ((run_unit_q==3'd4) && !err_from_cfg) ? u_err : err_code`（wrap:3100）
⇒ 融合模式 `run_unit_q==0`，AP_RETURN 只显示 `err_code`（=0）。
结果：拒绝启动是"静默拒绝"，PS 只能靠超时退出；`0x310` 的 seg_err 也是 0。

## V3 FUSE=0 整网 sha1

`verify/v3f/fuse0.log`（`taskset -c 6-7`，未设 `OMP_NUM_THREADS`、未单核）：

```
A 基线   sha1 = ('104710f69c4d','2ac6efec2325','8ab595fcf7cd')
B lazy   sha1 = ('104710f69c4d','2ac6efec2325','8ab595fcf7cd')
C 非lazy sha1 = ('104710f69c4d','2ac6efec2325','8ab595fcf7cd')   PASS（4.1 s）
```

归一化（去掉耗时数字）后与 v87 冻结基线 `R1_before_fuse0.log` **0 差异**（`verify/v3f/fuse0.norm`）。

## V4 FUSE=1：只融合 k=1

`verify/v4f/fuse1.log` + `verify/v4f/plan_audit.log` + `verify/v4f/submit_gate.log`：

- 223 段 ⇒ **融合 90 段 / 140 条描述符**；133 段不融合。
- 不融合原因：单级门禁 129 条（2/3/4/5 级算子段）+ **k=3 的 2 条**；0 条超预算。
- 90 个融合段：**全部是单级 conv、k=1、s=1、d=1、cout∈{32,48}**（`geom_check.log`）。
- **op_mask 全部 = 0x1 ⊆ OP_MASK_SUP=0x3 ⇒ 不会报 E_OPMASK**（0/90 越界）。
- 运行时门禁（段边界寄存器齐备性）90/90 可提交、0 被 MissingPiece 拒。
- FUSE=1 整网 sha1 与 FUSE=0 逐位相同（FakePL 无链引擎 ⇒ 回落 v5，`PASS`）。

## V5 编译/结构门禁

| 判据 | 结果 | 日志 |
|---|---|---|
| `xvlog -sv`（23 基线 + 10 lane16 = 33 文件） | rc=0，**0 error / 0 warning** | `verify/v5/xvlog.log` |
| `xelab bin_unit_axi_wrap_bd_v50 VLANES=8` | **Built** | `verify/v5/xelab_v8.log` |
| `xelab ... VLANES=16` | **Built** | `verify/v5/xelab_v16.log` |
| `verify_rtl_root.sh` 严格口径 | **MATCH=35 DIFF=0 EXTRA=0 MISSING=0 PASS** | `verify/v5/verify_rtl_root.log` |
| FUSE=0 结构不变（T-88 final `0229c186` vs T-89 `0f7dd0ae`） | 9 根 `u_*` mux **逐字相同**；唯一差异是 `u_err` 的 fuse 支多 OR 了 `arena_err` | `verify/v6/t89.diff`、`/tmp/u88.txt` vs `u89.txt` |

## V6 §14 契约符合性（字段 → RTL 读 → PS 填）

| §14 字段 | segq 读（行） | wrap 消费（行） | PS 填（`v50_desc`） | 判定 |
|---|---|---|---|---|
| 字0 `in_pa` | `dw[0][31:0]` 342 | `sh_in_pa` 1388 → filler `.in_pa` | w0 | ✅ |
| 字1 `out_pa` | `dw[0][63:32]` 343 / `st_base` 350 | `sh_out_pa` 1389 → `st_wa`(segq 465) | w1 | ✅（4 行修正生效） |
| 字2 低 `cin` | `dw[1][15:0]` 329 | `sh_cin` 1390 → `c_cfg_cin`/`geo_gin` | w2 | ✅ |
| 字3 低 `cfg_h` | `dw[1][47:32]` 331 | `sh_cfg_h` 1391 → `fill_rows`/`band_words` | w3 低 | ✅ |
| 字3 高 `H_in` | `dw[1][63:48]` 330 | `sh_h_in` 1392 → filler `.h_in` | w3 高 | ✅ |
| 字4 低 `W` | `dw[2][15:0]` 332 | `sh_w_row` 1393 → `c_cfg_w`/filler/`band_words` | w4 | ✅ |
| 字4 高 | 未读 | — | 0 | ✅（悬空已登记） |
| 字5 `k/s/d/pad` | 333-336 | **不再消费**（`c_cfg_k/s/d/pad = r_*` 1438-1442） | w5 恒 0 | ✅（走段边界寄存器，§14.5 裁定） |
| 字6 低 `band_y0` | `dw[3][15:0]` 337 | `sh_y0` 1394 → filler `.y0` | w6 | ✅（排空侧未用，见发现①） |
| 字7 `flags` | `dw[3][63:32]` 338 | 不消费 | w7 | ✅（E_NULL 不再要求非 0） |
| 字8 `next_pa` | `dw[4][31:0]` 339 | segq 内部 | w8 | ✅ |
| 字9 低 `nband` | `dw[4][47:32]` 340 | `sh_nband` 1397 → `c_cfg_h`/drain | w9 低 | ✅ |
| 字9 高 `op_mask` | `dw[4][63:48]` 341 | `sh_omask` 1398 → `fill_conv_en` | w9 高 | ✅ |
| 描述符外 `cout` | — | `c_cfg_cout = r_cout` 1437 | PS 写 0x70（`_v50_seg_cfg_pairs`，CHAIN_HEAD 前） | ✅ |
| E_NULL 判据 | 243：字0/字1 非 0 | — | — | ✅（cin=0/flags=0 不再误判） |
| in_arena 判据 | 245-246：字0/字1 + `band_words*8` | — | — | ✅ |

## 发现清单（按严重度）

### ① 【静默错数】排空基址只用 `out_pa`，没加带偏移/面间距 ⇒ 多带段输出互相覆盖

- `bin_segq.v:465`：`st_wa <= st_base + {cnt_q,3'd0}`；`st_base = dw[0][63:32] = out_pa`（350）。
- `bin_unit_axi_wrap_v50.v:1672`：`fuse_wr_addr = seg_st_we ? seg_st_wa : e_wr_addr`（**无任何偏移**）。
- `bin_net.py:1261`：同一段的**每条**描述符都传同一个 `out_pa`（带偏移只由 `band_y0` 表达，D3 契约）。
- band 内布局 `grp*(rows*W*8)+r*(W*8)+x*8`（`bin_band_buf.v:12`）是**按带压紧**的，而 DDR 张量是
  `grp*(h_out*W*8)+y*(W*8)+x*8`。⇒ 排空必须逐面跳到 `out_pa + grp*h_out*W*8 + y0*W*8`，现在是线性写。
- 具体数值（融合段 S6：cout=32, W=32, h_out=32, nband=8 ⇒ 4 带 × 4 面）：
  每带 1024 字 = 8 KB；4 带都写到 `out_pa..out_pa+8KB`，张量实际 32 KB
  ⇒ 3/4 字节从未写（陈旧），面间距 8 KB ≪ 需要的 32 KB。
- **命中范围：30/90 融合段**（h_out=32 的 10 段 = 4 带；h_out=16 的 20 段 = 2 带）。
  h_out≤8 的 60 段只有 1 带 ⇒ band 行 == 张量行、facet 间距也恰好相等 ⇒ **排空正确**。
  所以这条只在 h≥16 的层错，且在 h=32 层最严重（4 带全写同一处，3/4 张量是陈旧值）。
- 装侧是对的（filler 用绝对 `yr` 算 DDR 地址），所以这是排空单独漏掉的一半。

### ② 【不报错】arena 未配置：kick 确实被门控，但 `AP_RETURN[15:8]` 恒 0（V2b A3）

- 原因：`err_show`（wrap:3100）只有在 `run_unit_q==4`（E1E2）时才选 `u_err`；融合模式用 `err_code`。
- 影响：直接写寄存器启动 ⇒ busy 卡住、PS 只能超时退出（`0x310` 也看不出）。PS 侧现在强制 arena，
  所以从 PS 走不到；但 §15.2 的"u_err 报 E_RANGE"这句话在 AP_RETURN 上不成立。

### ③ `band_words` 排空字数（我上一轮提的"少一半"）—— **已修正，复验通过**

`geo_goutw = (cout+7)>>3`（wrap:1737），与 VLANES 无关。数值：

| cout | 排空字/像素（V8=V16=正确） | 旧 `ceil(cout/VLANES)*VLANES/8` 在 V16 |
|---|---|---|
| 32 | 4 | 2 |
| 48 | 6 | 3 |
| 64 | 8 | 4 |
| 96 | 12 | 6 |
| 192 | 24 | 12 |

90 个融合段 cout∈{32,48}：V8 与 V16 **0 段不一致**。结论：**不会少写一半**。

### ④ 【低危/仿真噪声】`translate_off` 断言在 fuse 启动后、第一条描述符到达前用 `sh_*==0` 判几何
（wrap:1925/1937、`bin_band_fill_v50.v:160`）⇒ 每拍 `$error` 刷屏（V2b case A 打了数百条）。
建议加 `seg_state != S_IDLE`（或"已收到 ≥1 条描述符"）门控，否则真错误会被噪声淹没。

### ⑤ 【风险】融合模式仍要 v39 配置页合法（vcode 链不变，wrap:1160-1235）
PS 融合提交只写 `cout/k/s/d/pad`（`bin_net.py:_v50_seg_cfg_pairs`）⇒ 若某段之前没跑过任何 v39 层，
`r_c/r_h/r_w/8 个基址` 为 0 ⇒ ERR_NULL/ERR_SIZE 拒启动。本网第一个融合段是 S6（S0-S5 已走 v39）⇒ 当前不触发。

## "v50 现在能不能出位流"

- **位流：能出。** FUSE=0 的 9 根 `u_*` mux 逐字未动（唯一差异 `u_err` 的 **fuse 支**），
  xvlog 0/0、两个 VLANES 都 Built、manifest 严格 MATCH=35 PASS、整网 sha1 冻结值全中。
- **FUSE=1 上板：不能。** 阻断 4 条：
  1. 本报告的发现①（排空不带偏移/面间距，**30/90** 段静默错数；h≥16 的层）；
  2. T-89 §15.6-① 带级联（fill→conv→band→drain）无集成 TB；
  3. T-89 §15.6-② 无带内级联序列器 ⇒ 只能单级 conv（本次 90 段），conv→cat 45 段吃不到；
  4. T-89 §15.6-③ k≥3 段不能融合（cfg_h 双用途）。
