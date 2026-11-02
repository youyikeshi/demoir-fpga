# T-118：关预取（PF_EN=0）验证 —— **被判据外的时序红线拦停，未上板**

**一句话**：`PF_EN` 是 **综合期 Verilog parameter，不是环境变量、也没有运行时寄存器**
⇒ 关预取**必须重建位流**；v118（= v53 + `PF_EN 1->0`）已重建完成，但**自身 routed WNS =
−0.270 ns（33 个违例端点，真实数据通路）⇒ 未过本项目 WNS ≥ 0 红线，不许上板**。
收到"用户要自己用板子"的通知后**未对板子下发任何命令**，实验的板端部分**未执行**。

---

## 1. 预取开关的机制（grep 证据，回答"环境变量/常量/寄存器"）

`grep -rn "PF_EN" pynq_specialized_code/pl/…` 全仓库只命中 **RTL 参数声明**，PS 侧零命中：

```
runs/v53_archive/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:344:
    parameter int  PF_EN        = 1,      // 预取总开关（0 = 退回纯需求缺失）
runs/v53_archive/rtl_v50/lane16/bin_unit_axi_wrap_v50.v:2528:
    assign pf_push  = (PF_EN != 0) && (PF_A != {TAG_W{1'b0}}) …
runs/…:2531  assign pf_issue = (PF_EN != 0) && (pf_cnt != PFQ_Z) && …
runs/…:2533  assign pf_drop  = (PF_EN != 0) && (pf_cnt != PFQ_Z) && pf_head_known;
```

- 打包层 `bin_unit_axi_wrap_bd_v50.v` **没有**把 `PF_EN` 透传出去（只透传 VLANES/FUSE_EN/BAND_*），
  ⇒ BD 的 `CONFIG.*` 也改不到它。
- 0x300 段寄存器表（0x300…0x31C）**没有** 预取使能位；TB 里那个 `R_PF_EN=0xF8`
  是旧实现遗留，注释明写"旧实现忽略该寄存器"。
- ⇒ **结论：`PF_EN` 只能改副本 + 重建位流**（本轮即按此执行：`cp -a` 前留 md5，只改副本，
  主线 RTL/PS **一个字节未动**）。

**对第 2 步（LC_N=4096）的同一结论**：`LC_N` 同样是 `parameter int LC_N = 512`（第 330 行），
无环境变量/参数覆盖入口 ⇒ **要把线缓存加大，也必须改 RTL 副本并重建**。

## 2. 本轮做了什么（主机侧，全部可复算）

| 项 | 值 |
|---|---|
| 副本 RTL | `worktrees/T-118-pfoff/rtl_v50/`（`cp -a` 自 `runs/timing_v50/rtl_v50`，源 35/35 先过 `verify_rtl_root.sh` PASS） |
| `cp -a` 前源 md5 | `runs/board_t118/src_rtl_before.md5`（35 条） |
| 副本 = 源 | `runs/board_t118/copy_rtl_after_cp.md5` 与上逐字节相同 ✓ |
| 唯一改动 | `lane16/bin_unit_axi_wrap_v50.v:344` `PF_EN 1 -> 0`（单行 diff，其余 **34/34 逐字节不变**） |
| 该文件 md5 | v53 冻结 `17164dfc3029ace1cbcb9eaa08af6213` → v118 `9ad53f96a8dcd94b610dfc1987ea8d35` |
| T-118 manifest | `worktrees/T-118-pfoff/rtl_v50_manifest_t118.txt`（仅替换上述一行）→ 核对 MATCH=35 DIFF=0 |
| 构建脚本 | `worktrees/T-118-pfoff/build_final_150v118.tcl`（逐字派生自 v53 脚本，36 行 diff 全在路径/输出名） |
| 构建闸门 | `memgate.sh 8` + `vrun.sh`；`V50_ASSERT_ONLY=1` 预演先过 0a（12/12 v39 输入 + 35/35 冻结） |
| 构建结果 | **rc=0**，01:04:12 → 01:18:25（约 14 min） |
| 位流 | `worktrees/T-118-pfoff/out/design_1_150v118.bit` md5 **`8deb14f379b0c45fac5449b1873457bd`** |
| `.hwh` | BD 未重建 ⇒ 与 v53 **同一个** `5235764602921d03351e121a81e35613`（成对） |
| bit↔源码 | `md5sum -c build_v118_manifest.txt` → **rc=0**（含 RTL 35 + BD 3 + 脚本） |

### 2.1 ★ 时序（这是本轮真正的拦路石）

| 量 | v53（冻结基线） | **v118（PF_EN=0）** |
|---|---|---|
| routed WNS | **+0.041 ns** | **−0.270 ns** ❌ |
| TNS | 0.0 | −4.541 |
| 违例端点 | **0** | **33** |
| 最差路径 | — | `u_bin_unit/r_s_reg[12]_replica/C → g_wr_split2.pix_q_reg[12]/D`，10.149 ns / 16 级 |
| Slice LUTs | 21496 (46.53%) | 20729 (44.87%) |
| Register as Latch | 0 | **0** ✓ |
| Block RAM Tile | — | 43/95 (45.26%) |
| 最终 DSP48E1 | 126 | 126 |

- 最差路径落在 **真实数据通路的 D 引脚**（不是准静态寄存器的 CE）⇒ 按 `PLAN`/技能的 §4.3
  门槛 **不满足"WNS ≥ 0 或违例全在准静态 CE"**，构建脚本自身的 **Z3 门禁已打 `★★ Z3 FAIL ★★`**。
- 物理解释：删掉预取逻辑只应**减**逻辑；0.31 ns 的恶化来自**P&R 换了一个不同的解**（seed/空间变化），
  不是逻辑变深。若要拿它上板，正路是**重跑实现（换 seed / 加 phys_opt 轮次）直到 WNS ≥ 0**，
  而不是放宽判据。
- 参考口径：本轮实验跑点 50 MHz（20 ns），这条 10.149 ns 的路径在 20 ns 下仍有 ≈9.85 ns 余量；
  **但这是"实际跑点宽松"，不是"门禁通过"** —— 是否接受这个口径由主代理/用户定，我不自行放宽。

## 3. 板端：**零操作**（按停手指令）

| 项 | 状态 |
|---|---|
| 本 session 对板子下发的命令 | **0 条**（仅 01:01 一次 `ping`，通） |
| PL 里现在的位流 | **v39** `602725b86941864eeeeab3be005e1e81`（T-117 于 00:58 收尾时 load 的那一颗；本 session 未 load 任何位流） |
| fclk0 | **50 MHz**（同上，T-117 设定值；本 session 未改） |
| 我在板端创建的目录/文件 | **无**（`/tmp/probe_T118`、`/home/xilinx/demoir/t118_out` 只写进了脚本，**从未执行**，故板端不存在） |
| 板端残留进程 | **无**（我未启动任何板端进程） |
| 是否仍开机 / 报警 | 未做任何操作 ⇒ 与 T-117 收尾一致（当时 `sync; sync`、未 poweroff、无报警） |
| `/tmp/dsh_board.loadtoken` | **存在，但不是我建的**：mtime **01:14:56**（= 收到停手消息前后），`/tmp/dsh_board.lock` 01:17:57 仍在更新 ⇒ **别人正持着板级租约**，我未触碰（不删、不抢） |
| 主机残留 | 0 个 vivado 进程（vrun 自带整组清理已核） |

## 4. 结论与下一步（给主代理）

1. **机制结论（一行）**：`PF_EN` 与 `LC_N` 都是综合期常量 ⇒ "关预取"和"加大线缓存"两刀**都必须重建**，
   不存在运行时/环境变量开关。
2. **本轮没能给出"预取是否定罪"的硬件答案** —— 不是没跑对，是**没跑**（用户要板子），
   且即便跑也必须先过 WNS ≥ 0。
3. 若要继续这条线，最省时间的顺序是：
   ① 对 v118 重跑实现（同 RTL，`place/route` 换 seed + 多一轮 `phys_opt_design`）直到 **WNS ≥ 0**；
   ② 再申请一次板级窗口，跑 256² ×2 + 64²，判据不变（`9b1940c8/f86a7035/025757a8` + 两次相同）。
   本轮的构建脚本/探针/运行脚本都已就位（`probe_t118.py` 已含 `0x314 magic` 与**每次整网后重读 0x310** 的判据），
   换掉 `expect_bit.md5` 即可复用。

## 5. 产物

`/media/felix/Data/coding/demoir-fpga/runs/board_t118/`：
`REPORT.md`、`probe_t118.py`、`run_t118.sh`、`src_rtl_before.md5`、`copy_rtl_after_cp.md5`、
`copy_rtl_after_edit.md5`、`PF_EN_change.txt`、`t118_assert.log`、`t118_build.log`、
`t118_build_memgate.out`。
副本与位流：`/media/felix/Data/coding/demoir-fpga/worktrees/T-118-pfoff/`
（`rtl_v50/`、`rtl_v50_manifest_t118.txt`、`build_final_150v118.tcl`、`out/`）。
