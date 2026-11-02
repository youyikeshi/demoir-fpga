# v75 — v44 routed DCP 只读核查：axi_ctrl 的 M04_AXI 与 fconv.s_axi

- DCP：`runs/v44_archive/design_1_150v44_routed.dcp`（57,357,526 B，Vivado 2025.2.1 建）
- 方式：`open_checkpoint` + 只读查询，6 轮 batch，全在 `runs/v75_dcpcheck/`，未改任何文件、未综合、未上板
- 日志：`vivado.log`（pass1）、`vivado_pass2..6.log`

## 1. 硬结果

| 查询 | 结果 |
|---|---|
| `M04_PINS`（`NAME=~*M04_AXI*`，含带下标位级探针） | **0** |
| `M04_NETS`（`NAME=~*M04_AXI*`） | **5**（全为标量握手：ARREADY / AWREADY / BVALID / RVALID / WREADY） |
| `M05_PINS` / `M05_NETS` | 0 / 0 |
| `M02_NETS`（conv_uni 寄存器窗） | 96（完整 AXI4-Lite 总线） |
| `M03_NETS`（bin_unit 寄存器窗） | 100（完整 AXI4-Lite 总线） |
| `M00_NETS` / `M01_NETS` | 0 / 0 |
| xbar 实例 | `design_1_i/axi_ctrl/xbar` + `/inst`（=`axi_crossbar_v2_1_38`） |
| xbar 端口形态 | **单条扁平 `m_axi` 总线**：awaddr/araddr/wdata/rdata 各 **160 bit**（=5×32）、wstrb 20（5×4）、arlen/awlen 40（5×8）、*prot 15（5×3）、*resp 10（5×2）、*ready/*valid/*burst 各 5 bit ⇒ 位宽上 5 路，**但没有 M00..M04 编号端口** |
| fconv 在网表 | 在。`design_1_i/fconv_0`（REF `design_1_fconv_0_0` → `fconv_axi_wrap_bd`），约 2.5 万叶子单元 |
| fconv 的 `s_axi` | **0 个引脚**（`NAME=~*fconv_0/s_axi*` = 0；`s_axi_awaddr[0]/[31]`、`s_axi_wdata[0]`、`s_axi_rdata[0]` 位级探针全 MISS） |
| fconv 的 `s_axi_awaddr` 连到哪个 net | **不存在该引脚**，无从连 |
| fconv 实际有的 AXI 引脚 | 扁平 `m_axi_*`：`wdata[63:0]`、`rdata[63:0]`、`wvalid/wready/bvalid/bready/rvalid/rready/awvalid/awready/arready` —— **无 awaddr / araddr / wstrb / awlen / arid**，且 `awvalid` 为 dangling |

## 2. 关键不匹配

1. `M04_AXI_*` 只以**网名**残留 5 条，全部是"从设备→主机"方向的标量响应信号；**地址/数据通道（AWADDR/AWVALID/WDATA/WSTRB/ARADDR/ARVALID）一条 net 都没有**。
2. 这 5 条 net 落在 `axi_ctrl` 的**扁平位**上，而不是 IP stub 定义的 M04 位段：
   - stub（10-30 09:49）定义 M04 = `m_axi_awaddr[159:128]`、`m_axi_awready[4]`
   - 实际网表：`axi_ctrl/m_axi_awready[2]` ← `fconv_0/m_axi_awready[0]`；`[4]`/`[128]` 全部 **ABSENT**
3. fconv 侧没有任何 `s_axi` 从口 ⇒ 0x40080000 窗口在网表里**没有任何从设备可译码**。
4. 构建脚本读的是**陈旧 IP 产物**：`ip/design_1_axi_ctrl_imp_xbar_0/{.dcp,_stub.v}` 时间戳 **10月30 09:49**（v44 的 BD 改动在 11-02），而 `design_1.v` wrapper / `.hwh`（11-02 13:17 / 13:1x）是按编号 M04_AXI 生成的。Vivado 只能按名字接上 5 条标量，向量通道整段丢弃。

## 3. 结论

**构建漏了**（陈旧 xbar IP DCP/stub，端口形态为扁平 `m_axi`，与 BD 的编号 M04_AXI 不匹配）⇒ M04 地址通道与 fconv 的 `s_axi` 从口都没进网表 ⇒ 板上 0x40080000 无译码 ⇒ SIGBUS。

不是"下载没生效"：第二次 load 不会改变，换时钟也不会改变。

修法：重新 OOC 综合 `design_1_axi_ctrl_imp_xbar_0`（让 IP 产物与 11-02 的 BD 一致，产出新 .dcp + 编号端口 stub），确认 stub 里出现 `M04_AXI_AWADDR` 等编号端口后再重建。
