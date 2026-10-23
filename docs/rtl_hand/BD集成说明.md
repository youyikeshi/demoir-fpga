# BD 集成说明：把手写 k1 加速器（`conv_k1_engine`）挂进 `design_1`

> 对应脚本：`pynq_specialized_code/pl/scripts/bd_add_k1.tcl`
> 配套适配层：`pynq_specialized_code/pl/rtl_hand/modules/k1_axi_wrap_bd.v`
> 目标工程：`pynq_specialized_code/pl/vivado/proj_engine/proj_engine.xpr`（Vivado 2025.2.1，`xc7z015clg485-2`）
> 最后更新：2026-10-23

---

## 0. 三条结论（先看这个）

1. **脚本写完了，但不能按任务书的两条假设原样实现**。两处都是读代码/读 `.bd` 实测出来的，
   不是猜的，下面 §2 和 §4 给了完整证据：
   - 任务书要 **128KB @ `0x4000_0000`**，但这段 128KB **正好是 engine 的两个 AXI-Lite 从机**
     （`s_axi_control` @`0x4000_0000` + `s_axi_ctrl` @`0x4001_0000`，各 64KB）。
     ⇒ 默认改放 **`0x4004_0000`**（128K 对齐、当前空闲）。
   - 任务书想 `create_bd_cell -type module -reference k1_axi_wrap`，但 `k1_axi_wrap` 的端口
     **没有 `X_INTERFACE_INFO`**、4 个 master 是**精简 AXI4**（缺 `ARSIZE/ARBURST/ARCACHE/
     ARPROT/ARID/AWID/RRESP/BRESP/BID/RID`）、而且**根本没有 `ap_clk`/`ap_rst_n` 端口**
     ⇒ 加了纯引脚级适配层 `k1_axi_wrap_bd.v`，BD 里 cell 名仍是 `k1_wrap`。
2. **不动** `engine` / `ps7` / `rst_0` / `smc` 的既有连接。`smc` 只扩端口不重建；
   万一扩不出来才走"删掉重建"，且 **S00 必须还是 gmem0、S01 必须还是 gmem1**——
   脚本里是硬校验，不满足直接 `error` 退出、不保存。
3. `pynq_specialized_code/pl/vivado/` 在 `.gitignore` 里（第 109 行），**git 回滚不了**。
   所以脚本开头会自动把 `design_1.bd` 拷到 `proj_engine/backup_bd/`。

---

## 1. 动手前实测的现状

### 1.1 BD 里的 cell（读 `design_1.bd` + 各 IP 的 `.xci`）

| cell | VLNV | 实测关键配置 |
|---|---|---|
| `ps7` | `xilinx.com:ip:processing_system7:5.5` | `PCW_USE_M_AXI_GP0=1`、`PCW_USE_S_AXI_HP0=1`、`PCW_FPGA_FCLK0_ENABLE=1`（150 MHz）。**`M_AXI_GP1` 没使能**（`.bd` 的 `interface_ports` 里只有 `M_AXI_GP0`/`S_AXI_HP0`），所以"用 GP1 单独挂一个互联"这条路要重配 PS，未采用 |
| `rst_0` | `xilinx.com:ip:proc_sys_reset:5.0` | 现用引脚：`slowest_sync_clk`（←`FCLK_CLK0`）、`ext_reset_in`（←`FCLK_RESET0_N`）、**`peripheral_aresetn`**（←engine 的 `ap_rst_n` 和两个互联的 `ARESETN`）。`interconnect_aresetn` 存在但未用 |
| `engine` | `demoir:hls:conv2d_engine:1.0` | `m_axi_gmem0`：AXI4、**32-bit 数据**、`READ_ONLY`、`ADDR_WIDTH 64`、`ID_WIDTH 1`；`m_axi_gmem1`：AXI4、**64-bit 数据**、`WRITE_ONLY`、`ADDR_WIDTH 64`、`ID_WIDTH 1`；`s_axi_control`/`s_axi_ctrl`：AXI4-Lite 32-bit |
| `axi_ctrl` | `xilinx.com:ip:axi_interconnect:2.1` | `NUM_SI=1`、`NUM_MI=2`、**`XBAR_DATA_WIDTH=32`**（内部是 **AXI4-Lite 交叉开关**，MI 侧是 Lite，S00 侧经 `s00_couplers/auto_pc` 转 PS 的 AXI3） |
| `smc` | `xilinx.com:ip:smartconnect:1.0` | `NUM_SI=2`、`NUM_MI=1`、`NUM_CLKS=1`、`HAS_ARESETN=1`；M00 是 **AXI3 64-bit**（PS HP0） |

> 补充：任务书说"engine 的两个 m_axi 都声明成 64-bit"——实测 **gmem0 是 32-bit 读、gmem1 是 64-bit 写**。
> 这不影响本次改动（SmartConnect 本来就在做宽度转换），但文档里按实测写，免得后面查不下去。

### 1.2 现有时钟 / 复位网络

| 网络 | 成员 |
|---|---|
| `ps7_FCLK_CLK0` | `ps7/FCLK_CLK0`、`rst_0/slowest_sync_clk`、`ps7/M_AXI_GP0_ACLK`、`ps7/S_AXI_HP0_ACLK`、`axi_ctrl/{ACLK,S00_ACLK,M00_ACLK,M01_ACLK}`、`smc/aclk`、`engine/ap_clk` |
| `rst_0_peripheral_aresetn` | `rst_0/peripheral_aresetn`、`axi_ctrl/{ARESETN,S00_ARESETN,M00_ARESETN,M01_ARESETN}`、`smc/aresetn`、`engine/ap_rst_n` |

k1 直接挂到这两根上（与 engine 同源，任务书要求的那条）。

### 1.3 现有地址（`.bd` 的 `addressing`）

| 段 | 偏移 | 范围 |
|---|---|---|
| `/engine/s_axi_control/Reg` | `0x4000_0000` | 64K |
| `/engine/s_axi_ctrl/Reg` | `0x4001_0000` | 64K |

`build_bd_engine.tcl` 里 `PP_ENABLE=1` 时还会用 `0x4002_0000`/`0x4003_0000`（pp），本 BD 无 pp。

---

## 2. `k1_axi_wrap` 的真实端口（实测，与任务书描述有差异）

### 2.1 `k1_axi_wrap.v`（394 行，`module k1_axi_wrap #(...)`，54 个端口）

| 组 | 端口 | 方向/位宽 |
|---|---|---|
| 时钟/复位 | `s_axi_aclk`、`s_axi_aresetn` | in，1 bit（**低有效**） |
| AXI4-Lite 从机 | `s_axi_{awaddr[31:0], awvalid, awready, wdata[31:0], wstrb[3:0], wvalid, wready, bresp[1:0], bvalid, bready, araddr[31:0], arvalid, arready, rdata[31:0], rresp[1:0], rvalid, rready}` | 18 根，**没有 `awprot`/`arprot`** |
| `m_axi_in_*`（读输入） | `arvalid, arready, araddr[31:0], arlen[7:0], rvalid, rready, rdata[63:0], rlast` | 8 根 |
| `m_axi_w_*`（读权重） | 同上 | 8 根 |
| `m_axi_b_*`（读偏置） | 同上 | 8 根 |
| `m_axi_out_*`（写输出） | `awvalid, awready, awaddr[31:0], awlen[7:0], wvalid, wready, wdata[63:0], wstrb[7:0], wlast, bvalid, bready` | 11 根 |

### 2.2 三条与任务书不符的事实

1. **没有 `ap_clk`/`ap_rst_n` 端口**。它们是被例化的 `conv_k1_engine` 内部的，
   在 wrapper 里由 `s_axi_aclk`/`s_axi_aresetn` 驱动（`k1_axi_wrap.v:355-356`）。
   ⇒ BD 侧只能接 `s_axi_aclk`/`s_axi_aresetn`。
2. **端口上没有 `X_INTERFACE_INFO`**（`grep -c X_INTERFACE_INFO` = 0）。
   ⇒ `create_bd_cell -type module -reference k1_axi_wrap` 得到的是一堆**散引脚**，
   散引脚**不能** `connect_bd_intf_net` 到 `smc/S02_AXI` 这种 interface pin。
3. **4 个 master 是精简 AXI4**：只有 AR/R（或 AW/W/B）的 valid/ready/addr/len/data/last。
   缺 SmartConnect SI 必需的 `ARSIZE/ARBURST/ARCACHE/ARPROT/ARQOS/ARID` 和
   `AWSIZE/AWBURST/AWCACHE/AWPROT/AWQOS/AWID/BRESP`、`RRESP/BID/RID`。

### 2.3 于是：`k1_axi_wrap_bd.v`（新增，纯引脚级）

**不改 `k1_axi_wrap.v` 一个字符**（它是已经跑过 `xvlog -sv` + 仿真的验证对象）。

> ⚠ **适配层本身刻意写成 Verilog-2001，不是 SystemVerilog。**
> `create_bd_cell -type module -reference` 只接受 **Verilog / VHDL** 作为"reference 的
> 顶层文件"，写成 SystemVerilog 会被顶回来（2026-10-23 主控实跑踩到）：
> ```
> ERROR: [filemgmt 56-195] Reference 'k1_axi_wrap_bd' contains top file
>        '.../k1_axi_wrap_bd.v' of type SystemVerilog.
>        This type is not allowed as the top file in the reference.
> ERROR: [BD 41-1690] Unable to resolve module-source based on inputs: k1_axi_wrap_bd
> ```
> 这个限制**只针对 reference 的顶层文件**；适配层内部例化的 `k1_axi_wrap` /
> `conv_k1_engine` 仍然可以是 SystemVerilog（脚本里三者 file_type 分别是
> `Verilog` / `SystemVerilog` / `SystemVerilog`）。
> 适配层只有 ANSI 端口 + `assign` + 例化，没有 `logic`/`always_*`/`parameter int`，
> 改成 Verilog-2001 是**零语义损失**。

适配层只做四件事：

1. 把 4 个 master 补成**完整 AXI4**：`ADDR_WIDTH 64 / DATA_WIDTH 64 / ID_WIDTH 1`
   （**与 engine 的 `m_axi_gmem1` 逐参数对齐**，SmartConnect 不用为这几个 SI 换配置）：
   `ARSIZE/AWSIZE=3'b011`（8 字节/拍）、`ARBURST/AWBURST=2'b01`（INCR）、
   `ARCACHE/AWCACHE=4'b0011`、`ARPROT/AWPROT=3'b000`、`ARQOS/AWQOS=4'b0000`、
   `ARLOCK/AWLOCK=1'b0`、`ARID/AWID=1'b0`。
2. **32 位引擎地址零扩展成 64 位**（高 32 位恒 0，与 `k1_axi_wrap` 里"基址高字必须为 0"的校验一致）。
3. **补齐两条通道**：`in/w/b` 的写通道恒空闲（`awvalid=0, wvalid=0, bready=1`），
   `out` 的读通道恒空闲（`arvalid=0, rready=1`）。这样 4 个 SI 形态统一，
   不会出现"某个 SI 少一条通道"的接口不匹配。
4. AXI4-Lite 从机补上 `awprot`/`arprot`（忽略），并给 5 个接口加
   `X_INTERFACE_INFO` / `X_INTERFACE_PARAMETER` / `X_INTERFACE_MODE` 属性，
   让 BD **推断**出 interface pin：`s_axi`、`m_axi_in`、`m_axi_w`、`m_axi_b`、`m_axi_out`。

**为什么不用任务书里的备选方案 `create_bd_cell -type hier` + 手动加引脚？**
`hier` cell 上的 interface pin 没有底层 AXI 端口可以绑（hier 内部的 interface pin
仍然要靠某个子 cell 的 interface 来驱动），最后还是要一个"有 AXI 端口 + 属性"的模块。
既然那个模块必须存在，直接让它当 BD cell 更短、更少一层层次。

属性写法照抄 Vivado 自己生成的 stub（`proj_engine.cache/ip/.../design_1_smc_0_stub.v`）：

```
(* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S00_AXI ARID" *) ...
(* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S00_AXI, DATA_WIDTH 32, PROTOCOL AXI4, ID_WIDTH 1, ADDR_WIDTH 64, ..." *)
```

---

## 3. 改动清单

| BD 对象 | 动作 | 改前 | 改后 |
|---|---|---|---|
| `axi_ctrl` | 扩主端口 | `NUM_MI=2` | **`NUM_MI=3`**（M00/M01 不动，新增 M02_AXI） |
| `axi_ctrl` | 接新端口的时钟/复位 | — | `M02_ACLK ← FCLK_CLK0`、`M02_ARESETN ← peripheral_aresetn`（不接会在 validate 报 `[BD 41-758]`） |
| `smc` | 扩从端口 | `NUM_SI=2` | **`NUM_SI=6`**（S00/S01 不动，新增 S02..S05） |
| 新 cell `k1_wrap` | `create_bd_cell -type module -reference k1_axi_wrap_bd` | — | 层级 `k1_wrap → k1_axi_wrap → conv_k1_engine` |
| `smc` | 新连接 | — | `k1_wrap/m_axi_in→S02`、`m_axi_w→S03`、`m_axi_b→S04`、`m_axi_out→S05` |
| `axi_ctrl` | 新连接 | — | `M02_AXI → k1_wrap/s_axi`（Lite，直连；直连失败才插 `axi_protocol_converter`） |
| 时钟/复位 | 新连接 | — | `k1_wrap/s_axi_aclk ← ps7/FCLK_CLK0`、`s_axi_aresetn ← rst_0/peripheral_aresetn` |
| 地址 | 新段 | — | `k1_wrap/s_axi/Reg` @ `0x4004_0000` +128K；4 个 master 各 1GB DDR @0 |
| 文件 | 新增源 | — | 3 个 `.v` 加入 `sources_1`，`file_type` 分别为 `SystemVerilog` / `SystemVerilog` / **`Verilog`**（适配层不能用 SV，见 §2.3） |

### 3.1 拓扑

```
                      ┌──────────────────── design_1 ─────────────────────────┐
  PS7                 │                                                       │
  M_AXI_GP0 ─────────►│ axi_ctrl (axi_interconnect 1→3, 32-bit Lite)          │
                      │   S00 ─┬─ M00 ──► engine/s_axi_control   @0x4000_0000  │
                      │        ├─ M01 ──► engine/s_axi_ctrl      @0x4001_0000  │
                      │        └─ M02 ──► k1_wrap/s_axi          @0x4004_0000  │
                      │                                                       │
  PS7                 │ smc (smartconnect 6→1)                                │
  S_AXI_HP0 ◄─────────│   M00 ◄─┬─ S00 ◄─ engine/m_axi_gmem0  (32b, RD)        │
    (AXI3 64b)        │         ├─ S01 ◄─ engine/m_axi_gmem1  (64b, WR)        │
                      │         ├─ S02 ◄─ k1_wrap/m_axi_in     (64b)          │
                      │         ├─ S03 ◄─ k1_wrap/m_axi_w      (64b)          │
                      │         ├─ S04 ◄─ k1_wrap/m_axi_b      (64b)          │
                      │         └─ S05 ◄─ k1_wrap/m_axi_out    (64b, WR)       │
                      └───────────────────────────────────────────────────────┘
   ps7/FCLK_CLK0 ──┬─► engine/ap_clk, axi_ctrl/*ACLK, smc/aclk, k1_wrap/s_axi_aclk
   rst_0/peripheral_aresetn ─┬─► engine/ap_rst_n, axi_ctrl/*ARESETN, smc/aresetn,
                             └─► k1_wrap/s_axi_aresetn
```

### 3.2 `smc` 为什么不直接删掉重建

任务书说"保守做法是删掉重建"。本脚本的顺序相反：**先扩 `CONFIG.NUM_SI`**，因为它
**不会重排 S00/S01**（端口名不变，原网表自动保留），风险比"删掉再按顺序接回去"低。
只有在扩完之后 `S02..S05` **没长出来**时，才走删重建分支，并且立刻按
`S00=engine/m_axi_gmem0`、`S01=engine/m_axi_gmem1` 逐条恢复。
两条路径之后都有**硬校验**：

```tcl
if {![string match "*engine/m_axi_gmem0" $now_s00]} { error "S00_AXI 不再是 gmem0" }
if {![string match "*engine/m_axi_gmem1" $now_s01]} { error "S01_AXI 不再是 gmem1" }
```

不满足就 `error` 退出（此时 BD **还没 save**，磁盘上的 `.bd` 不变）。

---

## 4. 地址映射

### 4.1 为什么不是 `0x4000_0000`

`design_1.bd` 的 `addressing` 写得很清楚：

```json
"SEG_engine_Reg":   { "address_block": "/engine/s_axi_control/Reg", "offset": "0x40000000", "range": "64K" },
"SEG_engine_Reg_1": { "address_block": "/engine/s_axi_ctrl/Reg",    "offset": "0x40010000", "range": "64K" }
```

`0x4000_0000–0x4001_FFFF` **整段 128KB 被 engine 占满**。任务书同时要求
"基址 0x4000_0000" 和 "避开已有 IP 的地址"，这两条互斥，必须二选一：

| 方案 | 代价 | 本脚本 |
|---|---|---|
| k1 放 `0x4004_0000`（128K） | PS 侧 k1 驱动用新基址（反正新 IP 也要写新驱动） | **默认** |
| k1 抢 `0x4000_0000`，engine 挪走 | engine 的 PS 固定地址变了，`conv_engine.py` 要同步改 | `K1_STEAL_ENGINE=1` 可选 |

为什么选 `0x4004_0000` 而不是 `0x4002_0000`：`build_bd_engine.tcl` 里
`PP_ENABLE=1` 时 pp 会用 `0x4002_0000`/`0x4003_0000`（`pp_engine.py` 的注释也写了
`pp → 0x40030000`）。128K 窗口从 `0x4002_0000` 起会盖住 pp，所以跳一段。

### 4.2 最终地址表

| 段 | 偏移 | 范围 | 归属 |
|---|---|---|---|
| `/engine/s_axi_control/Reg` | `0x4000_0000` | 64K | 既有，**未动** |
| `/engine/s_axi_ctrl/Reg` | `0x4001_0000` | 64K | 既有，**未动** |
| （保留给 pp，本 BD 不存在） | `0x4002_0000`/`0x4003_0000` | 64K×2 | — |
| **`/k1_wrap/s_axi/Reg`** | **`0x4004_0000`** | **128K** | **新增** |
| k1 的 4 个 master（各自的地址空间） | `0x0000_0000` | 1G | DDR 低 1GB，与 engine 相同 |

### 4.3 `A[16]` 分页怎么用

`k1_axi_wrap` 用**绝对地址 bit16** 选页（`k1_axi_wrap.v` 的 `araddr_l[16]` / `awaddr_l[16]`），
所以只要 128K 窗口是 128K 对齐，第二页天然就是 `base+0x10000`：

| 页 | 偏移 | 全地址 | 寄存器 |
|---|---|---|---|
| offs（`A[16]=0`） | `base+0x0000` | **`0x4004_0000`** | IN `0x10/0x14`、WGT `0x1C/0x20`、BIAS `0x28/0x2C`、OUT `0x34/0x38`（64 位基址，低+高） |
| ctrl（`A[16]=1`） | `base+0x10000` | **`0x4005_0000`** | AP_CTRL `0x00`、AP_RETURN `0x10`、H `0x18`、W `0x20`、HOUT `0x28`、WOUT `0x30`、CIN `0x38`、COUT `0x40`、KSIZE `0x48`、STRIDE `0x50`、DIL `0x58`、PAD `0x60`、SHIFT `0x68`、RELU `0x70`、STATUS `0x94` |

PS 侧写法（PYNQ）：

```python
K1_OFFS = 0x40040000      # offs 页
K1_CTRL = 0x40050000      # ctrl 页 = K1_OFFS + 0x10000
k1_ctrl = MMIO(K1_CTRL, 0x1000)
k1_offs = MMIO(K1_OFFS, 0x1000)
```

> 注意：**不能**把窗口设成 64KB。`k1_axi_wrap` 的两套寄存器偏移有重叠
> （`0x28` 在 ctrl 页是 HOUT、在 offs 页是 BIAS 基址），64KB 窗口下 ctrl 页不可达，
> 这是 RTL 作者刻意用 `A[16]` 换来的"`conv_engine.py` 偏移零改动"。
> 脚本里对 RANGE 有复核，不是 128K 会告警。

### 4.4 可选：把 k1 放到 `0x4000_0000`（顶替 engine 的位置）

```bash
K1_BASE=0x40000000 K1_STEAL_ENGINE=1 \
  vivado -mode batch -source pynq_specialized_code/pl/scripts/bd_add_k1.tcl
```

脚本会把 engine 的两段挪到 `0x4006_0000`（`s_axi_control`）/`0x4007_0000`（`s_axi_ctrl`），
再放 k1 到 `0x4000_0000`。
**这条路径会改 engine 的 PS 侧固定地址**，`conv_engine.py` 里的
`ctrl=0x40010000 / offs=0x40000000` 必须同步改，否则老引擎的读写会落到 k1 的窗口里。
默认关闭（`K1_STEAL_ENGINE=0`）。

---

## 5. 时钟 / 复位

| k1 端口 | 接到 | 与 engine 的关系 |
|---|---|---|
| `s_axi_aclk` | `ps7/FCLK_CLK0`（150 MHz） | 同一个网络（脚本会打印并核对 `k1 时钟网络 = engine 时钟网络`） |
| `s_axi_aresetn` | `rst_0/peripheral_aresetn`（低有效） | 与 `engine/ap_rst_n` 同源 |

`k1_axi_wrap` 内部把这个时钟/复位同时给 `conv_k1_engine` 的 `ap_clk`/`ap_rst_n`，
所以 BD 侧只要一对。若 `rst_0/peripheral_aresetn` 不存在，脚本会退回
`rst_0/interconnect_aresetn` 并打印 WARN。

---

## 6. 主控要执行的命令

### 6.1 前置检查（不跑 Vivado 也能做）

```bash
cd /media/felix/Data/coding/demoir-fpga

# 1) RTL 在
ls -l pynq_specialized_code/pl/rtl_hand/modules/{conv_k1_engine.v,k1_axi_wrap.v,k1_axi_wrap_bd.v}

# 2) 工程在、当前 BD 没留半截修改（对比 mtime）
ls -l pynq_specialized_code/pl/vivado/proj_engine/proj_engine.xpr \
      pynq_specialized_code/pl/vivado/proj_engine/proj_engine.srcs/sources_1/bd/design_1/design_1.bd

# 3) 确认 Vivado 没在 GUI 里打开这个工程（会锁 .xpr）
```

### 6.2 跑脚本

```bash
cd /media/felix/Data/coding/demoir-fpga
vivado -mode batch -source pynq_specialized_code/pl/scripts/bd_add_k1.tcl 2>&1 | tee /tmp/bd_add_k1.log
```

可选环境变量：

| 变量 | 作用 | 默认 |
|---|---|---|
| `K1_BASE` | k1 的 AXI-Lite 基址（必须是 128K 对齐） | `0x40040000` |
| `K1_STEAL_ENGINE` | `1` = 让 engine 挪位、k1 占 `0x4000_0000` | `0` |
| `K1_NO_BACKUP` | `1` = 跳过 `backup_bd/` 备份（不推荐） | 备份 |
| `BD_NO_EXIT` | 设了就不 `exit`（在 GUI 里 source 时用） | batch 下退出 |

### 6.3 预期输出（成功）

关键几行（顺序与实际一致，数值以 `0x4004_0000` 默认值为例）：

```
[01] 打开工程 /media/.../proj_engine/proj_engine.xpr
    OK   part = xc7z015clg485-2，工程已打开

[02] 备份 design_1.bd
    OK   已备份 → /media/.../proj_engine/backup_bd/design_1.bd.20261023_220000

[03] add_files + file_type（sources_1）
    OK   + /media/.../rtl_hand/modules/conv_k1_engine.v
    ..   file_type = SystemVerilog
    OK   + /media/.../rtl_hand/modules/k1_axi_wrap.v
    ..   file_type = SystemVerilog
    OK   + /media/.../rtl_hand/modules/k1_axi_wrap_bd.v
    ..   file_type = Verilog
    OK   update_compile_order 完成

[04] 打开 block design design_1
    OK   current_bd_design = /design_1
    OK   时钟 = ps7/FCLK_CLK0（与 engine/ap_clk 同一根），复位 = rst_0/peripheral_aresetn

[05] 地址预检
    ...地址表（0x4000_0000 / 0x4001_0000 两行）...
    OK   k1 窗口 0x40040000..0x4005ffff 空闲

[06] smc 扩到 6 个从端口（保留 S00/S01）
    ..   扩之前：CONFIG.NUM_SI = 2
    ..   扩之前：S00_AXI ← /engine/m_axi_gmem0
    ..   扩之前：S01_AXI ← /engine/m_axi_gmem1
    ..   扩之后：CONFIG.NUM_SI = 6
    OK   S02_AXI..S05_AXI 已存在（S00/S01 未重建、连接未动）
    OK   S00=engine/m_axi_gmem0、S01=engine/m_axi_gmem1（顺序未变）
    核对 smc/S00_AXI : net = engine_m_axi_gmem0
                    成员 /engine/m_axi_gmem0
                    成员 /smc/S00_AXI
    核对 smc/S01_AXI : net = engine_m_axi_gmem1
                    成员 /engine/m_axi_gmem1
                    成员 /smc/S01_AXI

[07] axi_ctrl 扩到 3 个主端口（保留 M00/M01）
    ..   扩之后：CONFIG.NUM_MI = 3
    OK   M02_AXI 已存在

[08] 创建 k1_wrap 实例
    OK   create_bd_cell -type module -reference k1_axi_wrap_bd k1_wrap
    OK   interface 推断成功：s_axi / m_axi_in / m_axi_w / m_axi_b / m_axi_out

[09] 接时钟 / 复位
    OK   connect_bd_net /k1_wrap/s_axi_aclk → ps7_FCLK_CLK0
    OK   connect_bd_net /k1_wrap/s_axi_aresetn → rst_0_peripheral_aresetn
    OK   时钟 同源：k1 与 engine 都在 ps7_FCLK_CLK0
    OK   复位 同源：k1 与 engine 都在 rst_0_peripheral_aresetn

[10] 接 AXI4-Lite：axi_ctrl/M02_AXI → k1_wrap/s_axi
    OK   connect_bd_intf_net /axi_ctrl/M02_AXI ↔ /k1_wrap/s_axi
    OK   直连成功

[11] 接 4 个 AXI4 master → smc/S02..S05
    OK   connect_bd_intf_net /k1_wrap/m_axi_in ↔ /smc/S02_AXI
    OK   connect_bd_intf_net /k1_wrap/m_axi_w ↔ /smc/S03_AXI
    OK   connect_bd_intf_net /k1_wrap/m_axi_b ↔ /smc/S04_AXI
    OK   connect_bd_intf_net /k1_wrap/m_axi_out ↔ /smc/S05_AXI

[12] 地址分配
    OK   AXI-Lite: /k1_wrap/s_axi/Reg = 0x40040000 + 128K
    OK   OFFSET 复核通过（0x40040000）
    OK   RANGE 复核通过（128K）
       offs 页（A[16]=0）: 0x40040000 .. 0x4004ffff
       ctrl 页（A[16]=1）: 0x40050000 .. 0x4005ffff
    OK   /k1_wrap/Data_m_axi_in → /ps7/S_AXI_HP0/HP0_DDR_LOWOCM @0x00000000 +1G
    ...（w / b / out 同样四行）...

[13] 复核既有连接未被动过
    OK   engine/m_axi_gmem0 ↔ smc/S00_AXI   （engine_m_axi_gmem0）
    OK   engine/m_axi_gmem1 ↔ smc/S01_AXI   （engine_m_axi_gmem1）
    OK   ps7/M_AXI_GP0 ↔ axi_ctrl/S00_AXI
    OK   axi_ctrl/M00_AXI ↔ engine/s_axi_control
    OK   axi_ctrl/M01_AXI ↔ engine/s_axi_ctrl
    OK   smc/M00_AXI ↔ ps7/S_AXI_HP0
    ...6 个 SI 端口现状...

[14] validate_bd_design
    OK   validate_bd_design 通过
[15] save_bd_design
    OK   已保存 ...
[16] make_wrapper + update_compile_order
    OK   top = design_1_wrapper，编译顺序已更新

==================================================================
 BD 集成完成：k1（conv_k1_engine）已挂进 design_1
  ...
  备份    : /media/.../proj_engine/backup_bd/design_1.bd.20261023_220000
==================================================================
BD_INTEGRATION_OK
```

**判定标准**：最后一行必须是 `BD_INTEGRATION_OK`。看到它就是
"BD 改完 + 校验通过 + 已保存 + wrapper/编译顺序已更新"。

### 6.4 常见的失败长相

| 输出 | 含义 | 怎么办 |
|---|---|---|
| `ERROR: [Common 17-170] Unknown option '-sv'` | 2025.2 的 `add_files` 没有 `-sv`（**已修**：改成 `add_files` + `file_type`） | 用当前版本脚本即可 |
| `[filemgmt 56-195] ... of type SystemVerilog. This type is not allowed as the top file` | module reference 的顶层文件不能是 SV（**已修**：适配层改为 Verilog-2001） | 确认 `k1_axi_wrap_bd.v` 的 `file_type` 是 **Verilog**；脚本会打印它 |
| `FAIL 冲突：/engine/...` + `地址窗口与已占用段重叠` | 基址被占（比如自己传了 `K1_BASE=0x40000000`） | 去掉 `K1_BASE`，或加 `K1_STEAL_ENGINE=1` |
| `FAIL k1_wrap 上没有推断出 interface` | `X_INTERFACE_INFO` 没被 Vivado 采纳 | 见 §8 R1；先删掉 `create_bd_cell` 那一行单独试，或在 GUI 里对模块 Refresh Module |
| `error ... 仍没有 M02_AXI` | `axi_interconnect` 扩 MI 没生效 | GUI 里把 Number of Master Interfaces 设成 3，或用 `build_bd_engine.tcl` 重建 |
| `S00_AXI 不再是 engine/m_axi_gmem0` | 端口顺序被改（不该发生） | **立刻关掉 Vivado 不保存**，用 `backup_bd/` 还原 |
| `validate_bd_design` 报 `[BD 41-758]` | 有 AXI 端口的 ACLK 没接 | 看报错里点名的 pin，多半是 `M02_ACLK`；脚本已接，若还报说明版本差异 |
| `SmartConnect 没长出 S02_AXI.. ⇒ 删除 smc 重建` | 走了兜底分支 | 正常，继续看后面 `S00/S01` 校验是否为 OK |

---

## 7. 如何回滚

### 7.1 脚本中途失败（没到 `save_bd_design`）

磁盘上的 `.bd` **没变**（BD 只在最后一步保存）。关掉 Vivado 重新打开工程即可，
不需要任何操作。

### 7.2 已经 save 过，要退回改动前

```bash
# 1) 关掉所有 Vivado（否则 .bd 会被内存里的版本盖回去）
# 2) 还原
cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/vivado/proj_engine
ls -l backup_bd/                          # 挑时间戳最近、且是本次运行之前的那个
cp backup_bd/design_1.bd.<时间戳> proj_engine.srcs/sources_1/bd/design_1/design_1.bd
# 3) 重新打开工程，确认 Address Editor 只剩 engine 的两段
```

> 如果连 `backup_bd/` 都没有（第一次跑就用了 `K1_NO_BACKUP=1`），走 7.3。

### 7.3 从零重建（最彻底）

```bash
vivado -mode batch -source pynq_specialized_code/pl/scripts/build_bd_engine.tcl
```

`build_bd_engine.tcl` 是 `create_project -force` + 从空 BD 重建，产物与 k1 无关
（`PP_ENABLE` 默认 0），跑完就是 k1 接入前的状态。**但它会覆盖整个 `proj_engine` 工程**，
如果工程里有别的手工改动要另存。

### 7.4 手工撤销（不想整工程重建时）

在 Vivado Tcl Console 里：

```tcl
open_bd_design [get_files design_1.bd]
# 1) 删 k1 相关 cell（会自动带走其连接与地址段）
foreach c {k1_wrap axi_pc_k1} {
    if {[llength [get_bd_cells -quiet $c]]} { delete_bd_objs [get_bd_cells $c] }
}
# 2) 先断开 S02..S05、M02 上可能残留的空网表，再缩端口
foreach i {2 3 4 5} {
    set p [get_bd_intf_pins -quiet smc/S0${i}_AXI]
    if {[llength $p]} { catch {delete_bd_objs [get_bd_intf_nets -quiet -of_objects $p]} }
}
set_property -dict [list CONFIG.NUM_SI {2}] [get_bd_cells smc]
set_property -dict [list CONFIG.NUM_MI {2}] [get_bd_cells axi_ctrl]
validate_bd_design
save_bd_design
```

> `NUM_SI` 缩回 2 之前**必须**先断网（否则 Vivado 报 pin 不存在/引用失效）。
> 稳妥起见还是走 7.2 的备份还原。

---

## 8. 风险点

按"多大概率炸 / 炸了多难查"排序。

### R1（最高）`X_INTERFACE_INFO` 没被推断成 interface —— 直接卡在第 8 步

Vivado 对 **module reference** 的接口推断依赖 RTL 属性。如果推不出来，
脚本会在 `创建 k1_wrap 实例` 那一步 `error` 并列出它看到的 interface pin（通常为空）。
脚本设计成**在这里硬停**，而不是继续生成一个接不上的 BD。

顺带一个**可能的告警**（不是错误）：module reference 的 AXI 接口没有 `ASSOCIATED_BUSIF`
时钟关联信息，`validate_bd_design` 可能提示"某接口未关联时钟 / clock domain 未知"。
本设计里时钟是**标量引脚** `k1_wrap/s_axi_aclk`（和 `engine/ap_clk` 完全一样的接法），
连接与地址都不受影响；要消掉告警，在 GUI 里给该接口手工指定 Clock Domain 即可。

排查顺序：
1. `get_files k1_axi_wrap_bd.v` 是否存在、`FILE_TYPE` 是否 `SystemVerilog`（脚本已打印）；
2. 在 GUI 里把该模块作为 module reference 加一次，看 BD 是否显示 5 个 interface；
   显示的是散引脚 ⇒ 属性没生效（多半是属性串写错/被工具吞了）；
3. 兜底：用 `ipx::package_project` 把它打成本地 IP 再 `create_bd_cell -type ip`（属性同样的写法，但走的是 IP 打包路径）。

### R2 SmartConnect 扩 `NUM_SI` 不生效

脚本已用 `S02..S05` 是否存在来判定，失败就删重建并恢复 S00/S01（§3.2）。
重建分支的配置只设 `CONFIG.NUM_SI/NUM_MI`，其余用 IP 默认（`NUM_CLKS=1`、`HAS_ARESETN=1`，与原 xci 一致）。
**残余风险**：重建后 `smc` 的 IP 例化名/层级变了（影响仿真脚本或综合后的层次名，不影响功能与地址）。

### R3 `axi_ctrl/M02_AXI` 的协议不是 AXI4-Lite

`axi_ctrl` 的 xbar（`design_1_axi_ctrl_imp_xbar_0.xci`）是 32-bit、`PROTOCOL` 参数就是 `AXI4LITE`，
所以 M02 大概率本来就是 Lite。万一不是，脚本会 `catch` 到直连失败，自动插一个
`axi_protocol_converter`（`SI_PROTOCOL=AXI4`、`MI_PROTOCOL=AXI4LITE`）并接它的时钟复位。
**残余风险**：多一个 cell、面积 +少量 LUT；控制通路多 1~2 拍延迟（寄存器访问，无影响）。

### R4 改地址 / 抢地址

默认不碰 engine 的地址；`K1_STEAL_ENGINE=1` 才会动，且会打印 WARN。
地址冲突在**改动任何东西之前**就检查（`addr_conflicts`），冲突直接 `error`，BD 未被修改。

### R5 功能风险（RTL 层，不是 BD 层）

这四条是 `k1_axi_wrap` 的精简 AXI 与完整 AXI4 语义的差距，**BD 层无法消除**：

| # | 风险 | 说明 / 后果 | 建议 |
|---|---|---|---|
| R5.1 | **没有 4KB 边界检查** | AXI4 规定一笔 burst 不能跨 4KB。`k1_axi_wrap` 只给 `arlen`，地址由内部逻辑算；若某笔 burst 跨 4KB，HP0/SmartConnect 会 SLVERR 或返回错数据 | 上板前用仿真核对每笔 `araddr+len*8` 不跨 4KB |
| R5.2 | **`ARBURST` 恒 INCR、`ARLEN` 可达 255** | 引擎内部一次最多发多少拍未知；若 `arlen` 与真实需求不符会读多/读少 | 同上，仿真里 dump `arlen` 分布 |
| R5.3 | **ID 恒 0、无 outstanding 上限** | 4 个 master 都用 ID=0，SmartConnect 会在内部补 SI 编号，功能没问题；但**同一个 master 的内部乱序**不会被区分（引擎本身按顺序消费，问题不大） | 无 |
| R5.4 | **`RRESP/BRESP` 被忽略** | 引擎不检查错误响应：DDR 访问出错时表现为**数据错**而不是报错 | PS 侧用 `STATUS`/结果校验兜底 |

另外：`k1_axi_wrap` 的四个基地址寄存器是 **64 位**但只用低 32 位（高字必须为 0，
wrapper 里 `start` 时会校验，否则返回 `ERR_SIZE`）。适配层把地址零扩展成 64 位发给
SmartConnect，语义一致。

### R6 时序 / 资源

本次只改 BD 接线，**没有综合**，所以没有任何 Fmax/资源数据。新增逻辑只有
`k1_axi_wrap_bd` 的常量与零扩展（几乎 0 LUT），主要面积来自 `conv_k1_engine` 本身
（其单独综合数据见 `docs/rtl_hand/conv_k1_engine.md`）。
共同风险：k1 与 engine 共用 `HP0`（一个 DDR 端口），**两者同时跑会抢带宽**。

### R7 重复执行

脚本对 cell 是"先删后建"，`NUM_SI/NUM_MI` 只增不减，网络用 `ensure_net/ensure_intf`
（已连就不动）。**已知限制**：如果上一次运行已经把 `k1_wrap` 的地址段改成 `0x4004_0000`，
这次改了 `K1_BASE` 会重新分配（旧段随 cell 删除一起消失），不会残留。
另外**不要**在 GUI 打开同一工程的同时跑这个 bash 脚本（工程锁）。

---

## 9. 上板前的验证清单

- [ ] 脚本最后一行是 `BD_INTEGRATION_OK`
- [ ] Address Editor 里：`engine` 两段还在 `0x4000_0000`/`0x4001_0000`；`k1_wrap` 一段 `0x4004_0000` +128K
- [ ] `smc` 的 S00 = `engine/m_axi_gmem0`、S01 = `engine/m_axi_gmem1`（**顺序不能反**）
- [ ] `k1_wrap` 的 4 个 master 分别落在 S02/S03/S04/S05（in/w/b/out）
- [ ] `k1_wrap/s_axi_aclk` 与 `engine/ap_clk` 在**同一个** net 上
- [ ] `validate_bd_design` 无 CRITICAL WARNING
- [ ] 综合后确认 `k1_axi_wrap_bd` 里没有 latch/多驱动（都是 assign 常量）
- [ ] PS 侧：先只读 `STATUS(0x94)` 和 `AP_CTRL(0x00)`，确认 AXI-Lite 通；再跑小尺寸（cin=8, cout=8, H=W=8）
- [ ] 与 engine **分开**测吞吐（共享 HP0，同时跑数据没意义）

---

## 10. 附：脚本里几个"为什么这么写"

| 写法 | 原因 |
|---|---|
| `add_files -norecurse` + 显式 `set_property FILE_TYPE` | 2025.2 的 `add_files` **没有 `-sv` 选项**（实跑报 Unknown option）；`.v` 默认按 Verilog 编，`logic`/`always_comb` 会报错 |
| 适配层写成 Verilog-2001 | `create_bd_cell -type module -reference` 不接受 SystemVerilog 顶层（§2.3） |
| `update_compile_order` 在 `create_bd_cell -type module` **之前** | module reference 要能查到已 elaborate 的模块 |
| 直连 AXI-Lite 失败才插 `axi_protocol_converter` | 少一个 cell、少一段延迟；只有真不匹配才付这个代价 |
| `M02_ACLK/M02_ARESETN` 单独接 | 扩 MI 后新端口的时钟复位不会自动继承，漏接就是 `[BD 41-758]` |
| `save_bd_design` 放在最后 | 中途任何 `error` 都不会污染磁盘上的 `.bd` |
| 复核 6 条既有连接后才允许保存 | 任务书硬要求"不要动既有连接"，用代码兜住而不是靠人眼 |

---

## 11. 相关文档

| 文档 | 关系 |
|---|---|
| [`conv_k1_engine.md`](conv_k1_engine.md) §14 | `k1_axi_wrap` 的寄存器表 / 128KB 分页设计的原始说明（本文 §4.3 与之对齐） |
| [`00_SPEC_接口契约.md`](00_SPEC_接口契约.md) | 手写 RTL 的统一契约（C8 布局、ABI） |
| [`../部署/k1_IP上板验证清单.md`](../部署/k1_IP上板验证清单.md) | 主控上板验证的 10 步清单；它按 **`k1_wrap` + 128KB 窗口** 从 `.hwh` 的 `ip_dict` 发现地址，与本脚本建的 BD 一致 |
| [`../../pynq_specialized_code/ps/pynq/k1_engine.py`](../../pynq_specialized_code/ps/pynq/k1_engine.py) | PS 驱动，`DEFAULT_IP_NAME="k1_wrap"`、`WINDOW_SIZE=0x20000`、`CTRL_PAGE_OFF=0x10000` |

> 脚本与 `k1_engine.py` 的契约：**BD cell 名必须是 `k1_wrap`**、**地址段必须是一整块 128KB**
> （这样 `ip_dict` 里才是一条 `k1_wrap` + `range 0x20000`）。改脚本里的 cell 名或把窗口拆成
> 两个 64KB，PS 侧会直接报"窗口不是 128KB / 找不到 k1_wrap"。
