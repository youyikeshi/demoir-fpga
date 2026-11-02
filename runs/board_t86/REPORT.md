# T-86 上板报告：v45 的 `0x40080000` 不再 SIGBUS + HP2 数据通路冒烟

**日期** 2026-11-02 16:47–16:51 ｜ **板** `192.168.7.2`（xilinx）｜ **租约** `boardlock`（T86 / T86d）
**运行点** v45 位流 · `fclk0 = 25 MHz`（按 R8：WNS −15.745 ns @10 ns ⇒ 真实最差周期 25.745 ns）
**产物** `runs/board_t86/` ｜ 探针 `t86_probe.py` / `t86_board.sh` / `run_on_board.sh` / `t86_flag_probe.py`

---

## 〇、一句话结论

1. **`0x40080000` 读通了（v44 的 SIGBUS 已消除）**：`R_NLANE` 读回 **4**，同一次会话里
   `0x400A0000`（未映射）**仍然 SIGBUS** ⇒ 探针方式正确，读数可信。
2. **HP2 数据通路跑通**：4 KB 恒等卷积 `md5(y) == md5(x)`（`0c1781de…`），4096/4096 字节全等、
   1024/1024 个 f32 逐位相同，4 个 DBG 计数全非 0，金丝雀 0/4096 被写坏。
3. 判据里**唯一没满足**的是 `DBG_FLAG[2]`（写 burst 跨 4 KB）。已用**可证伪的负对照**证明它是
   **RTL 标志逻辑的假阳性**（陈旧 `wstart` 与首拍页比较），不是真的跨页事件、不是数据通路故障。

---

## 一、第 1 段：load v45

| 项 | 值 |
|---|---|
| 位流 | `runs/v45_archive/design_1_150v45.bit`，md5 **`4e9853fbc69642b27943e34ba9481e1d`** ✔ |
| `.hwh` | 同目录 `design_1_150v45.hwh`，md5 **`194562af193ecfa9553f2debc454daf9`** ✔（成对） |
| 板端门禁 | `.bit`/`.hwh` 成对 md5 在板端复核通过；`alpha_shift` 等读之前**没有**载错位流 |
| 加载 | `Overlay(bit, download=True)`，**本会话仅此一次**，用时 **2.56 s** |
| load 前 | 清空 `pynq/pl_server/global_pl_state_.json` + `_current_metadata.pkl` |
| 部署 | `ps/`（156 文件 md5 全过）+ `deps_closure.txt`（`93155d7c…`）同时同步；板端 `ps/pynq` 依赖闭包 `md5sum -c` **20/20 OK** |

**load 后读回**

| 项 | 值 | 判读 |
|---|---|---|
| `bin_unit_0` 复位指纹 `alpha_shift` / `slope_shift` | **0 / 0** | RW 寄存器，**裸载读 0 正常**（brief 已注明），不作失败 |
| `AP_CTRL` | `0x0` | 空 ✓ |
| `Clocks.fclk0_mhz`（load 后默认） | **125.0** | — |
| `conv_uni_0` 基址 | **0x40040000** | 与期望一致 ✓ |
| `bin_unit_0` 基址 | **0x40060000** | 与期望一致 ✓ |
| `fconv_0` 基址 | **0x40080000** | 与期望一致 ✓（`.hwh` 里也是 `0x40080000..0x4009FFFF`，128 KB） |

---

## 二、第 2 段：`0x40080000` 还能不能读（本轮核心判据）

- `Clocks.fclk0_mhz` 设 **25** → 读回 **25.0** ✓
- 用 `ps/pynq/fconv.py: FconvEngine` 只读寄存器（探针的 `MMIO(0x40080000, 0x20000)` + `_Shim`）：

| 寄存器 | 读回 | 说明 |
|---|---|---|
| **`R_NLANE`**（`+0x10078`） | **4** | 资源档位 = 4 lane ✓（期望 4） |
| `STATUS`（`+0x10094`） | `0x00000080` | 只有 `[8] idle=1`；`done=0`、`busy=0`、`err=0` |
| `AP_RETURN`（`+0x10010`） | `0x00010000` | `[16]` 写管道空；`done=0`；err_code=0 |
| `R_MODE`（`+0x10008`） | `0x00000002` | 模式 A + 读预取使能（复位值）✓ |
| `B_X`（`+0x10` 基址页） | `0x00000000` | 基址页可读（未初始化）✓ |

$$\Rightarrow\ \textbf{读通了}\ \Rightarrow\ \text{v44 的 SIGBUS 已消除（这是本轮最重要的一条）}$$

**对照（未映射 `0x400A0000`）**：子进程被 **SIGBUS (7)** 打死 ⇒ 仍 SIGBUS ✓
⇒ 探针方式正确（同一套 `pynq.MMIO` 路径，一个通一个挂，证明"通"不是探针假象）。

> 实现注：可能 SIGBUS 的访问全部在 `fork()` 出的子进程里做（父进程 surviving），
> 所以 v44 那种"进程直接没了、什么证据都不剩"的结局不会重演；`t86_result.json` 每步 fsync。

---

## 三、第 3 段：HP2 4 KB 恒等卷积冒烟

配置（照 `HP2_SMOKE_TEST.md` §2 最小版）：`H=W=32`、`cin=cout=1`、`k=3`、`pad=1`、`relu=0`、
`bias_en=0`、`mode=0`、权重只有中心 tap = 1.0 ⇒ 理论上 `y ≡ x`。
`x[i] = float32(i % 251 + 1)/7.0`（4096 B）。缓冲：`x=0x30046000`、`w=0x30047000`、`y=0x30048000`。

| 判据 | 实测 | |
|---|---|---|
| **`md5(y) == md5(x)`** | `0c1781de10dee75d103e13921d6be448` **=** 同值 | **✓** |
| 字节差异 | **0 / 4096** | ✓ |
| f32 逐位差异 | **0 / 1024** | ✓ |
| `DBG_RD[15:0]` 需求读拍 (rdreq) | **3345** | ✓ 非 0 |
| `DBG_LINE[31:16]` AR 笔数 (nAR) | **162**（≥ 64 笔的参考量级） | ✓ 非 0 |
| `DBG_WR[15:0]` AW 笔数 (nAW) | **32**（= 512 拍 / 16） | ✓ 非 0 |
| `DBG_WR[31:16]` W 拍数 | **512**（= 4096 B / 8 B） | ✓ 非 0 |
| `DBG_ST[31:16]` R 拍数 (nR) | **1296** | ✓ 非 0 |
| `STATUS` | `0x00000081`（`done=1`、`err=0`） | ✓ |
| `AP_RETURN` | `0x00010001`（`done=1`、写管道空、err_code=0） | ✓ |
| 用时 | 0.0050 s @25 MHz | 合理 |
| 金丝雀（y 尾 4 KB） | **0 / 4096 被写坏** | ✓ 无越界写 |
| **`DBG_FLAG[0..2]`** | **`0x4`（bit2 置位）** | **✗ 判据未满足（见 §4）** |

`x` 头 32 B 与 `y` 头 32 B 完全一致（`[37,73,18,62, 37,73,146,62, …]`）⇒ 不是"两边都没动"的假通过
（AR/AW/R 计数已从旁佐证总线真的动了）。

---

## 四、`DBG_FLAG[2]` 的判读：假阳性，已用负对照证明

**RTL 取证**（`pl/rtl_hand/_worktrees/T-62-v44/rtl_fconv/fconv_axi_wrap.v`，即 v45 构建用的那份）：

```verilog
777  wire w_break = w_push && (wr_wr_addr != wstart + (wlen << 3));
778  wire w_b4k   = w_push && (wr_wr_addr[31:12] != wstart[31:12]);   // ← 置 flg[2]
779  wire w_close = (wlen != 0) && (w_break || (wlen >= WR_BURST_MAX) || w_b4k || …);
786  wlen <= 0; wstart <= 0;                                          // 复位
798  if (wlen == 0 || w_close) wstart <= wr_wr_addr;                   // 起新段
```
`w_b4k` 用的是**更新前**的 `wstart`；复位后首个 beat 时 `wlen==0`、`wstart==0`（页 0）
⇒ `w_b4k = (页 ≠ 0) = 1` **必然置位**；而 `w_close` 要求 `wlen != 0`，那一下**不会多切一笔 burst**。

**可证伪的负对照**（`t86_flag_probe.py`，`Overlay(download=False)`，**未 reload 位流**）：
同一块缓冲连跑两次，再换一块**新页**的 y2 连跑两次：

| 运行 | y 地址 | 相对陈旧 `wstart` 页 | md5 相等 | nAW | nW | `DBG_FLAG` |
|---|---|---|---|---|---|---|
| A1 | `0x30048000` | 同页 | ✓ | 32 | 512 | **0x0** |
| A2 | `0x30048000` | 同页 | ✓ | 32 | 512 | **0x0** |
| **B1** | `0x3004a000` | **不同页（首跑）** | ✓ | 32 | 512 | **0x4** |
| B2 | `0x3004a000` | 同页（`wstart` 已更新） | ✓ | 32 | 512 | **0x0** |

预测（B1 置位、B2 不置位、nAW 恒 32）**逐项命中** ⇒
`bit2` 是"首拍页 ≠ 上一段起始页"的**诊断性假阳性**，不是真实跨 4 KB 切 burst：
`4096 B` 输出整块落在页 `0x30048` / `0x3004a` 内，物理上不可能跨页；`nAW` 四次恒为
$512/16=32$，**没有多出一笔**；数据逐字节正确、金丝雀完好。

**⇒ 判读：HP2/AFI 通，fconv 逻辑正确；`DBG_FLAG[2]` 是标志语义缺陷（建议后续修 RTL：
首拍不比较 `wstart`，或在 `w_close` 起新段时同拍清 `w_b4k`），**不是**本轮数据通路的失效点。

---

## 五、纪律与板子最终状态

| 项 | 记录 |
|---|---|
| 段数 | 严格三段，另加一次**只读**追加判读（`download=False`，不占 load 令牌） |
| load 次数 | **1 次**（`BOARD_ALLOW_LOAD=1` 唯一令牌；`rm -f /tmp/dsh_board.loadtoken` 先执行） |
| ssh/scp | 4 + 2 + 2 = 8 条，跨 ~8 分钟，无重试风暴；全程走 `tools/boardlock.sh` |
| `ulimit -c 0` | 两侧都设；**无 core 文件产生** |
| `pkill -f` | 未使用 |
| 报警/段错误/超时 | 无金丝雀报警、无意外段错误（SIGBUS 只出现在**预期的**未映射对照里） |

**板子最终状态（未 poweroff，只 `sync; sync`）**
- **仍开机**（uptime 正常，`MemFree 803 MB` / `CmaFree 127 MB`，磁盘/inode 正常）
- **PL = v45**：`pl_server/global_pl_state_.json` → `bitfile_name = /tmp/probe_T86/design_1_150v45.bit`
  （追加判读用 `download=False` 复用，未重载）
- **`fclk0 = 25 MHz`**（读回确认）
- 板端历史 `design_1_150v39.bit` 仍在（`602725b8…`，未被覆盖）
- `ps/` 已更新为当前冻结点（旧目录备份为 `ps.bak_t86`）；依赖闭包清单两处一致（`93155d7c…`）

---

## 六、对下一步的含义（不在 T-86 范围，供主代理决策）

1. **v44 的 SIGBUS 根因（陈旧 xbar 地址译码表缺 `0x40080000`）确认修复**：译码段补上 + `M04_AXI` 组 108 条
   net（其中 `AWADDR` 17 条）接地之后，PS 能正常读写 fconv 寄存器窗，且在 25 MHz 下 fconv 的 AXI 主口经 **HP2**
   能完整读写 DDR。
2. **HP2/AFI 不再是未知数**：stock PYNQ `BOOT.BIN` 的 `ps7_init` **确实**配置了 `S_AXI_HP2`
   （`HP2_SMOKE_TEST.md` §0 标为"未验证的 R2 风险" ⇒ 现在可判为"通"）。
3. **fconv 逻辑在 `k=3/cin=cout=1/32×32` 这一档正确**（逐位）。更大形状（多通道、`k=5`、多 lane 并行）
   仍未验证——那需要另行提判据，且必须先解决 §五 的时序（WNS −15.745 ns 只在 25 MHz 下能跑）。
4. `DBG_FLAG[2]` 建议加一条 RTL 修正 + 仿真门禁（首拍不比较 `wstart`），否则后续每一次
   "换新页首跑"都会误报，会让判据长期失信。
