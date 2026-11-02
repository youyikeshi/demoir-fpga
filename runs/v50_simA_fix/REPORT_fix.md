# v50_simA_fix —— IMP-A `bin_conv_core.v` 的 F1/F2 修复与复跑验收（T-78）

工具：`/media/felix/325/xilinx/2025.2/2025.2.1/Vivado/bin/{xvlog -sv, xelab, xsim}`（2025.2.1）
被测（修好的唯一交付物）：`pynq_specialized_code/pl/rtl_hand/_worktrees/v50_impA_fix/bin_conv_core.v`
　925 行，**md5 `4e6a42358a20d6488a508b39073f44fb`**
修前：`_worktrees/v50_impA/bin_conv_core.v`（919 行，md5 `e2ed125f1c34b53c24dc11162a4756d5`，**未改动**）
TB：`runs/v50_simA/tb_bin_conv_core_v50.v`（md5 `ac42c8c1…`）**原样拷贝**，`tb_bin_conv_core_v50.v` 与之逐字节相同。
未做任何综合/实现、未上板、未碰 `bin_unit_axi_wrap*`/`bin_band_buf`/`bin_segq`/PS/tcl。一次只跑一个 xsim。

---

## 1. 改了什么（只有这两处 + 变量改名）

| # | 位置（修复后行号） | 修前 | 修后 |
|---|---|---|---|
| **F1** | `:696` 赋值，`:186` 声明，`:635` 复位，`:757` 使用 | `hwout8_q <= ({16'd0,hout_q}*{16'd0,wout_q}) << 3;` | `plane_stride_q <= ({16'd0,hout_q}*{16'd0,wout_q}) * VLANES;` |
| **F2** | `:762-766`（`ST_DRN` 组循环） | `nvalid_q <= (cout_q - o_q > VLANES) ? VLANES : (cout_q - o_q);`（`o_q` 当拍被非阻塞赋值 ⇒ 读到旧值） | `nvalid_q <= (cout_q - (o_q+VLANES) > VLANES) ? VLANES : ((cout_q > (o_q+VLANES)) ? (cout_q-(o_q+VLANES)) : 32'd0);` |

F1 只换名不换语义的地方：声明/复位/使用三处。文件其余部分与 `v50_impA` 逐字节相同（`diff -u` 只有这 5 个 hunk）。
**没有**做 SIM 报告里的 O1~O5。

## 2. 复跑了什么（全部用现成 TB）

| 标签 | RTL | TB | 日志 |
|---|---|---|---|
| `before` | v50_impA（F1+F2 都在） | 原样 24 组 | `xvlog_before.log` `xelab_before.log` `xsim_before.log` |
| `after` | **v50_impA_fix** | 原样 24 组 | `xvlog_after.log` `xelab_after.log` `xsim_after.log` |
| `extA` | v50_impA | `_ext`（28 组） | `xsim_extA.log` |
| `extB` | F1 单独修（`variants/bin_conv_core_f1only.v`） | `_ext`（28 组） | `xsim_extB.log` |
| `extC` | **v50_impA_fix** | `_ext`（28 组） | `xsim_extC.log` |

`_ext` 版是**纯增量**副本：原 24 组激励一字未改，只加 (a) `+EXT` 才跑的 4 个定点用例
`cout=17/20/24/33`（`h=5,w=8,s=1 ⇒ npix=40`，与报告 F1 取证同构），(b) 每笔写事务的
`TXSIG` 打印（`n` + 对 addr/128-bit data/16-bit sel 的 FNV-1a 64 哈希），(c) `MAXPASS 4→8`。
`variants/bin_conv_core_f1only.v` 只为隔离 F2 而存在，**不是交付物**。

## 3. 验收结果

### V1 `VLANES=8`
- **`cout%8==0` 的 16 组（cout∈{8,16,24,32}，含 4 组边界）: 逐 bit 0 diff，写事务数/地址/数据与修前完全一致。**
  证据：`xsim_after.log` 这 16 组 `S1=ok`；`TXSIG cid∈{0,2,5,6,7,8,10,12,13,16,19,20,21,22,26}` 的
  `dut=0(v39)` 与 `dut=1(new8, S1)` 修前↔修后哈希与 `n` 全等。
- 写事务总数 **1389 → 1389**（`SUMMARY: 1389 write-tx`，修前修后相同）。
- 剩余 8 组（cout∈{13,17}）`S1=FAIL`，但**只有 SEL 位不同：167 条 SEL、0 条 ADDR、0 条 DATA**
  （`grep '\[S1 cid' xsim_after.log | … | uniq -c ⇒ 167 SEL`）。这 167 条正好等于 v39 自身的
  `MASK_ORG=167`，即 simA 报告已定罪、且 v39 原版继承的 F2 —— **v39 不是 cout%8≠0 时的正确基准**，
  所以"24/24 对 v39 0 diff"在修掉 F2 后不可能成立；正确的表述是"16/24 组 0 diff，其余 8 组仅字节使能不同，
  且差异恰为 v39 的 167 个末组掩码错"。

### V2 `VLANES=16`（24 组）
`S2 n16 vs ceil(c/8)×8 : 24/24 cases lane-0-diff, 9977 lane compared`（修前也是 24/24，lane 侧的等价性本来就成立）。
绝对值掩码：`MASK_N16 186 → 0`；`OVERRUN 2230 → 0`。

### V3 定点用例（`cout` 非倍数，`VLANES=16`）
| cid | cout | 16-lane 组数 | 修前 addr 错 | **修后 addr 错** | 修前 overrun/mask |
|---|---|---|---|---|---|
| 1 / 24 | 17 | 2 | 40（`t=40 ADDR exp=280 act=140`，与报告一致） | **0** | 有 → 0 |
| 25 | 20 | 2（末组 4 lane） | 40 | **0** | 有 → 0 |
| 26 | 24 | 2（末组 8 lane） | 40 | **0** | 有 → 0 |
| 27 | 33 | 3（末组 1 lane） | 80 | **0** | 有 → 0 |

全量（24 组）：addr 错 **278 → 0**；`cout=17` 单组案例贡献 40 笔（`cid=1`），
278 = 6+40+56+32+48+30+24+6+20+16（cid 0,1,2,5,6,9,10,16,17,18）。修后两类日志里 `ADDR DIFF` 行 **完全消失**。
掩码：`MASK_ORG/MASK_NEW/MASK_N16 = 167/167/186 → 167/0/0`（v39 保持 167，说明只修了新版）。
`S2 24/24 ok` 且 `OVERRUN=0` ⇒ 16-lane 末组与 8-lane 分趟逐 lane 一致。

### V4 编译/展开
- `xvlog -sv bin_conv_core.v`（单独、`v4check/xvlog_rtl_only.log`）：**0 WARNING / 0 ERROR**（只有 2 条 INFO）。
- 带 TB 的 `xvlog -sv` 同样 0 warning 0 error（`xvlog_after.log`）。
- `xelab`: `Compiling module work.bin_conv_core(VLANES=16)`，`Built simulation snapshot simF`；
  唯一 warning 是本设计无关的 `XSIM 43-3431`（`LIBRARY_PATH` 环境变量，simA 里也有）。

### V5 F2 在 `cout%VLANES==0` 时是否逐字节不变 —— **是，给了仿真证据**
隔离实验：`extB`（只修 F1）vs `extC`（F1+F2），比 `TXSIG`（该 DUT 本次运行的全部写事务：
笔数 + 每笔 addr/128-bit data/byte-enable 的哈希）。28 组 × 4 DUT 共 157 条比得上 136 条（
S2 的多趟 DUT 按趟比），**21 条不同，且 21 条全部落在 `cout%VLANES≠0`**：

- `dut=1`（VLANES=8）只在 cout∈{13,17,20,33}（`%8≠0`）时不同；cout∈{8,16,24,32}（`%8==0`）**全等**；
- `dut=3`（VLANES=16）只在 cout∈{17,20,24,33}（`%16≠0` **且有多组**）时不同；cout∈{16,32}（`%16==0`）**全等**，
  逐条列出：cid 0/5/6/16（cout=32）、7/12/13/19/20/21/22（cout=16）`B==C = True`；
  cout=8/13/3/5 等单组（`cout≤16`）也全等（ST_DRN 的组循环不进入，F2 天然是 no-op）。
- `dut=0`（v39 参照）28 组全等。
因为 `TXSIG` 覆盖了"写了哪些字节、每个字节写什么、有没有写"三件事全同，所以 `cout%VLANES==0` 时
**内存最终内容逐字节相同**；`cout%VLANES≠0` 时的差异正是 `sel` 由全 1 收窄到 `(1<<remaining)-1`
（有效 lane 的 data 也一致：修后 `S2 lane-0-diff 28/28`，参考 DUT 本身不受 F2 影响）。
另外 F1 在 `VLANES=8` 上是纯 no-op：`extA` vs `extB` 的 `dut=0/1` 共 129 条 TXSIG **全部相同**
（`* VLANES` 在 8 lane 下恒等于 `<<3`）。

## 4. 附带结论（未改，仅告知）
- `S2_ADDR_BAD` 在修后为 0，说明 `ogbase_q` 现在按 `Hout·Wout·VLANES` 递增，`cout>16` 不会再压掉前一组平面。
- 修 F2 后 `cout%8≠0` 时新版与 v39 不再逐 bit 相同是**预期**（v39 有 167 个掩码错）；若下游有以 v39 为
  golden 的回归，需要把 golden 更新为"末组掩码 = (1<<剩余)-1"。

## 5. 复现
```bash
cd runs/v50_simA_fix
./run.sh                                    # 修后 24 组（默认 RTL_NEW=v50_impA_fix）
RTL_NEW=<path> TAG=<name> ./run.sh          # 换 RTL/日志前缀
TB=tb_bin_conv_core_v50_ext.v TAG=extC ./run.sh -testplusarg EXT   # 28 组 + TXSIG
```
