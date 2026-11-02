**不可上板（结构缺陷已修好；时序完全未收敛：WNS −15.745 ns @ 10 ns 约束）—— 本颗只用于证明 xbar 修复生效**

# v45 存档：xbar IP 重综合（T-80，2026-11-02）—— v44 板端 SIGBUS 的唯一修法

> **唯一变量 = xbar（`design_1_axi_ctrl_imp_xbar_0`）的 IP 产物。**
> 配方、RTL 根、约束、报告顺序与 v44 **一位不动**（`build_final_150v45.tcl` 与 v44 的实体行 diff 只有
> `$bd` / `$out` / 输出名 / 新增 §1c 断言）。
> 运行点 `xc7z015clg485-2` / Vivado 2025.2.1；构建约束 **10.000 ns**（v39 配方逐字）。

## 〇、一句话结论

**修好了，但不能上板。** ①xbar 重综合让 IP 的地址译码表从 **2 段**变成 **3 段**，
补上 `0x40080000`（fconv 窗口）；②routed DCP 里 `M04_AXI_*` 从 **5 条标量** 变成 **108 条**，
`M04_AXI_AWADDR` 由 **0 → 17 条**，且 net 端点直接落在 `fconv_0/aw_addr_reg[16]_0[*]`
⇒ **fconv 的寄存器从口真的接进网表了**；③但 WNS 从 −6.076 恶化到 **−15.745 ns**（最差路径在 fconv 内部
`u_tl/ic_reg → g_lane[0].u_lane/acc_arr_reg`，35 级）——这个恶化**正是修复生效的反证**：
v44 之所以"看起来还行"，是因为 fconv 的从口根本没接，整块逻辑被剪掉了。

## 一、OOC 重综合走的哪条路（第 1 步）

| 项 | 内容 |
|---|---|
| 走的通路 | `open_project` → `reset_run design_1_axi_ctrl_imp_xbar_0_synth_1` → `launch_runs -jobs 2` → `wait_on_run` |
| 为什么不用 `synth_ip` | xbar 是**嵌套 BD**（`design_1` 的层次块 `axi_ctrl` = IP `design_1_axi_ctrl_0`）内部的核，**不在**项目级 `get_ips` 里（实测 n=0）；`synth_ip` 对 IPI 内部核还会报 12-3424 |
| 关键发现 | 重跑前该 run 的 STATUS = **"Using cached IP results"** —— 这就是陈旧产物不复发的机制本身：项目从 `proj_engine` 连 run 目录一起被拷过来，缓存命中 ⇒ 永不重跑 |
| 耗时 / 结果 | 71 s，STATUS=`synth_design Complete!` |
| 新产物 | `ip/design_1_axi_ctrl_imp_xbar_0/design_1_axi_ctrl_imp_xbar_0.dcp` **118174 B，md5 `313f49decbe3dc04fe1440eb8fb58cff`，mtime 2026-11-02 15:42:51**（旧：116679 B，md5 `58a60dff17ba2128caecd069fd800916`，10-30 09:49） |
| 新缓存 | `proj_engine.cache/ip/2025.2.1/1/e/1e97c5a0571ee89e/` 建于 15:42:51；陈旧缓存 `5/3/535a7ddc31dca4ce/` 仍是 10-30 09:39 |
| 构建实际读的 | 日志第 505 行：`Reading design checkpoint '.../proj_v45/.../design_1_axi_ctrl_imp_xbar_0.dcp' for cell 'design_1_i/axi_ctrl/xbar'` ⇒ 读的是新 DCP |

## 二、★ 判据口径的更正（必须留档，否则下一轮又会错）

T-80 的 brief 要求「新 stub 里必须能搜到 `M04_AXI_AWADDR`」。**实测证明这条对本设计不成立**：

| 证据 | 内容 |
|---|---|
| 新旧 stub 端口声明 | **逐条相同**（diff 为空）：`m_axi_awaddr[159:0]` / `m_axi_wdata[159:0]` / `m_axi_wstrb[19:0]` / `m_axi_araddr[159:0]` / `m_axi_rdata[159:0]`，各 39 条 |
| `M0x_AXI_AWADDR` 在 IP stub 内 | 旧 **0** / 新 **0** —— 编号是**扁平向量的切片名**，只存在于 BD 生成的包装模块 `design_1_axi_ctrl_0`（`design_1.v` 里命中 3 处） |
| 那么"宽度被截成 1 路"的说法 | 也**不成立**：旧产物本来就是 5 路 160 bit |

⇒ 真正能区分新旧、也正是 SIGBUS 真因的，是 **IP 的地址译码表**（详见 `STUB_DIFF_v44_vs_v45.md`）：
旧 `C_M_AXI_BASE_ADDR` 只有 `{0x40060000, 0x40040000}` 两段，**没有 0x40080000** ⇒ fconv 窗口无译码
⇒ 该组逻辑整段被剪 ⇒ PS 一访问 0x40080000 就挂。

## 三、断言（第 2 步）：5 条 + 2 个负对照

`pl/scripts/assert_v45_xbar.tcl`，由 `build_final_150v45.tcl` §1c 在 **synth_design 之前**调用：

| 编号 | 断言 | 结果 |
|---|---|---|
| A | stub 地址段 **≥3** 且含 `0x40080000` | 3 段 `{0x40080000, 0x40060000, 0x40040000}` ✔ |
| **A2** | 宽度：`awaddr/araddr = NUM_MI×ADDR_WIDTH = 160`、`wdata/rdata = NUM_MI×DATA_WIDTH = 160`、`wstrb = NUM_MI×DATA_WIDTH/8 = 20` | 160/160/160/160/20 ✔ |
| B | xbar `.dcp` 与 `_stub.v` mtime 必须**晚于** BD 三件（`.bd` / `synth/design_1.v` / `hw_handoff/design_1.hwh`） | stub=dcp=15:42:51 > design_1.v=13:17:14 ✔ |
| C | BD 包装 `design_1.v` 里 `M04_AXI_AWADDR` ≥1 | 3 ✔ |
| D | stub 里 `m_axi_awaddr[159:0]`（5 槽向量主口）在位 | 1 ✔ |

**负对照（`negctrl_xbar_assert.log`，用生产代码同一份脚本）**
- 例 1：真·旧 stub/.dcp ⇒ 断言 A 炸：`只有 2 个地址段（期望 ≥3）`
- 例 2：新 stub 但 `m_axi_awaddr` 人为截成 `[31:0]` ⇒ **A2 炸**：`位宽 32 != NUM_MI×ADDR_WIDTH = 160`
- ⚠ 注意 **A2 在旧产物上也通过**（两边都 160）⇒ 只靠宽度抓不到这次的陈旧产物，**A 才是有效判据**，故两条都留。

另：`pl/scripts/verify_rtl_root.sh` **MATCH=25 DIFF=0 EXTRA=0 MISSING=0 → RESULT: PASS**
（清单由 `gen_rtl_root_manifest_v45.sh` 按 build 脚本同一规则现算：12 条 = v39 位流 manifest + 13 条 = git `b58964e`）。

## 四、★ 第 3 步：routed DCP 只读核查（这一步决定成败）

命令：`open_checkpoint runs/v45_out/design_1_150v45_routed.dcp` + 只读查询，全在 `runs/timing_v45/dcp_*`。

| 查询 | v44 | **v45** |
|---|---|---|
| `M04_AXI_AWADDR` net 数 | **0** | **17**（`axi_ctrl_M04_AXI_AWADDR[0]`…`[16]`，17 = 128K 窗口的偏移位宽） |
| `M04_AXI_*` 全组 net 数 | 5（只有标量握手） | **108** |
| `M04_AXI_WDATA` / `ARADDR` | 0 / 0 | **32 / 17** |
| `M04_AXI_AWVALID/AWREADY/WVALID/WREADY/BVALID/BREADY/ARVALID/ARREADY/RVALID/RREADY` | 0（除 5 条标量） | **各 1** |
| fconv 的 `s_axi` 引脚数（**按名字**） | 0 | **0**（名字探针在 v45 也是 0 ⇒ 这个探针本身无效） |
| **fconv 寄存器口是否被接上（按 net 端点）** | 无 net 可查 | **接上了**：`M04_AXI_AWADDR[0..16]` 的负载 = `fconv_0/aw_addr_reg[16]_0[0..16]`，驱动 = `axi_ctrl/m_axi_awaddr[20..36]`；`M04_AXI_WDATA[0..31]` 的负载 = `fconv_0/w_dat_reg[31][*]` |
| `M02_AXI` / `M03_AXI`（对照） | 96 / 100 | **96 / 100（不变）** |
| `M00` / `M01` / `M05` | 0 / 0 / 0 | 213 / 0 / 0 |

> `M04_AXI_AWADDR` 的 17 条 net 每条只有 **2 个端点**（xbar 出、fconv 进），没有悬空 —— 这是"通道真的通了"的直接证据。
> `WSTRB/BRESP/RRESP` 在 v45 也是 0 条 net：fconv 寄存器窗按整字写、响应恒 OKAY ⇒ 被常量传播吃掉，属正常。

## 五、构件质量（X3）

| 项 | 值 | 判据 |
|---|---|---|
| Slice LUTs | **34050** | ≤ 38000 ✔（占 46200 的 73.7%） |
| Slice Registers | 30147 | — |
| Block RAM Tile | 57 | — |
| DSPs | 125 | ≥110 ✔（fconv 在位） |
| `Register as Latch`（报告口径） | **0** | 必须 0 ✔ |
| 网表指纹 `LDCE/LDPE`（网表口径） | **0** | 必须 0 ✔ |
| DRC（routed DCP，`report_drc`） | **0 Errors**，478 条全是 Warning（DPIP-1/DPOP-1/DPOP-2/DPOR-1/RBOR-1/REQP-1839/1840） | 0 Errors ✔ |
| **WNS @10 ns** | **−15.745 ns**（delay 24.908 ns / 35 级） | ✗ **不达上板门槛** |
| TNS | −30577.93 ns（违例端点 ≥2000，探针上限） | ✗ |
| 最差路径 | `fconv_0/inst/u_fconv/u_wrap/u_core/u_tl/ic_reg[2]/C` → `.../g_lane[0].u_lane/acc_arr_reg[7][28]/R` | **真实数据通路**（不是准静态 CE 引脚） |
| （对照）v44 | WNS −6.076 ns / TNS −10756.25 / LUT 32012 | — |

**为什么 v45 更差**：v44 的 fconv 从口是死的，`u_fconv` 内部大片逻辑被剪；v45 接上后它们变成真逻辑，
时序才暴露出来。⇒ 这是修复生效的**反证**，不是回归。

## 六、冻结资产（X6）

见 `FROZEN_ASSETS_v45.md`：
- `proj_engine` 与 `proj_v43base2` 中 mtime **晚于 15:38** 的文件数 = **0**
- 主工程 `design_1.v` md5 = `c5d88680b4c96d6835da221a233c78c1`、`design_1.hwh` md5 = `c1686bd8e080c1c7f46181d980543202`（与 v43base2 README 记录一致）
- `proj_v43base2` 的 xbar 旧产物仍是 **10-30 09:49**（未被本轮回写）

## 七、产物清单

| 文件 | 说明 |
|---|---|
| `design_1_150v45.bit` | 位流，**md5 `4e9853fbc69642b27943e34ba9481e1d`**，3,510,798 B（v44 = `d848bb5b60698e14b227ccc60e485097`） |
| `design_1_150v45_routed.dcp` | 布线后 checkpoint，md5 `0e5bb7c6ac2ddcf5a26251292017c1f9`，59,182,235 B |
| `design_1_150v45.hwh` | `S_AXI_HP2` 命中 89；md5 `194562af193ecfa9553f2debc454daf9` |
| `util_v45.rpt` / `timing_v45.rpt` | 资源 / 时序（md5 `4ccf2095…` / `70ccb90f…`） |
| `drc_v45.rpt` | DRC：0 Errors |
| `build_v45_manifest.txt` | bit ↔ 源码 md5 清单（27 个文件） |
| `build_final_150v45.tcl` / `assert_v45_xbar.tcl` / `gen_rtl_root_manifest_v45.sh` | 构建脚本 / 断言脚本 / 25 条账目生成器 |
| `rtl_v39_exact_25_manifest.txt` / `verify_rtl_root.log` | RTL 根 25/25 账目与核对输出 |
| `xbar_stub_OLD_1030.v` / `xbar_stub_NEW_1102.v` / `xbar_OLD_1030.dcp` / `xbar_stub_BADTRUNC32.v` | 新旧 stub 与负对照物证 |
| `STUB_DIFF_v44_vs_v45.md` | 新旧 stub 端口/地址段对比 |
| `ooc_resynth_xbar_v45.{tcl,log}` | 第 1 步的重综合脚本与日志 |
| `dcp_check_v45{,_b,_final}.{tcl,log}` / `dcp_endpoints{,_2}_v45.{tcl,log}` | 第 3 步 3 轮只读核查（前两轮各因 `get_buses` / `${ch,-12}` 写法中止，保留了作审计） |
| `negctrl_xbar_assert.{tcl,log}` | 两个负对照 |
| `build_v45.log` | 完整构建日志 |
| `FROZEN_ASSETS_v45.md` / `MD5SUMS` | 冻结资产取证 / 本目录校验和 |

## 八、下一步（不在 T-80 范围内）

1. **时序必须先收敛**才谈上板：最差路径全在 `u_fconv/u_wrap/u_core/u_tl` 的 tag 存储 → lane 累加器（35 级），
   属于 fconv 自己的结构问题（v44 被剪掉所以看不见）。要么改 fconv 流水（RTL 侧），要么先降 fclk。
2. 0x40080000 的 SIGBUS 预期已消失（译码段补上 + 从口接上），但**在上板门槛满足前不要上板验证**。
3. 若仍要独立验证"译码修好"，可只 load 位流读 `0x40080000` 寄存器窗口（不跑业务）：
   这一步不依赖时序收敛，但会占用唯一的板级租约，需主代理批准。

## 九、校验记录（本存档已实跑）

```
$ cd runs/v45_archive && md5sum -c MD5SUMS
  35 / 35 通过，失败 0

# bit ↔ 源码清单：基准目录是 pynq_specialized_code（清单里有一条相对路径 pl/scripts/...）
$ cd pynq_specialized_code && md5sum -c ../runs/v45_out/build_v45_manifest.txt
  27 / 27 通过

$ runs/v42e_archive/../  tools/../pl/scripts/verify_rtl_root.sh runs/timing_v45/rtl_v39_exact_25_manifest.txt runs/v42e_archive/rtl_v39_exact
  MATCH=25 DIFF=0 EXTRA=0 MISSING=0 → RESULT: PASS
```
