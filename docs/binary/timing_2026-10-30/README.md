# 时序违例全景分析（2026-10-30，LUT 瘦身后）

## 一、当前实测

| 指标 | 值 |
|---|---|
| LUT | 18,359（39.7%）|
| FF | 29,080 |
| BRAM tile | 4 |
| DSP | 67 |
| **WNS @ 150 MHz** | **−11.449 ns** |
| 违例端点 | 8,769 |

### 关键判断：不是拥塞，是逻辑太深

```
Logic Levels:  35  (CARRY4=12  LUT1=1 LUT2=3 LUT3=4 LUT4=3 LUT5=4 LUT6=8)
Data Path Delay: 16.406 ns  (logic 6.525 ns 39.8%  +  route 9.881 ns 60.2%)
```

LUT 只占 39.7%，布线余量充足 ⇒ **不需要 floorplan / pblock，需要拆流水线**。

## 二、级数预算核算

$$T_{\text{avail}} = 6.667 - t_{\text{skew}} - t_{\text{setup}} \approx 6.20\ \text{ns}$$

实测每级平均代价：

$$\bar{t} = \frac{16.406}{35} \approx 0.469\ \text{ns/级}$$

可容许级数：

$$N_{\max} = \left\lfloor \frac{6.20}{0.469} \right\rfloor = \mathbf{13}\ \text{级}$$

$$\Rightarrow\ \textbf{目标是把 35 级降到 } \le 13 \text{ 级，即至少拆成 3 拍}$$

FF 预算：$92400 - 29080 = 63320$，充裕。

## 三、违例端点家族（全部在 `conv_uni_0`）

| 端点寄存器 | 路径数 | 平均级数 | 最差 slack | 职责 |
|---|---:|---:|---:|---|
| `bf_data_reg[0][*]` | 2,733 | 35.0 | **−11.449** | burst FIFO 数据 |
| `ent_tag_reg[0..31]` | 54 × 32 = 1,728 | 28.6 | −9.535 | CAM 标签写入 |
| `cache_a_q_reg` | 128 | 32.2 | −10.522 | 线缓存输出寄存器 |
| `cache_b_q_reg` | 64 | 32.0 | −10.324 | 同上（第 2 份）|
| `be_data_reg` | 64 | 33.0 | −10.371 | 写打包缓冲 |
| `pf_plane_addr_q_reg` | 58 | 30.2 | −9.853 | 预取平面地址 |

`bin_unit_0` 与 PS7 互连**无违例**。

## 四、五个瓶颈族的共同点

它们全部属于**线缓存的"查表 → 命中 → 取数 → 写回"链路**：

```
AXI 读回 ──> 条目分配(优先级编码) ──> ent_tag 写入 ──┐
                                                      ├──> 命中判定(32 路比较)
引擎请求地址 ──> tag 比较 ──> 条目号编码 ──> 槽号 ──┘         │
                                                            v
                                             BRAM 地址 ──> cache_a_q/cache_b_q
                                                            │
                                    引擎写回 ──> be_data/bf_data 打包 ──> AXI 写
```

$$\Rightarrow\ \text{这些在旧设计里就存在（40 MHz 下 13 级 ≈ 6.5 ns 刚好用满），}$$
$$\Rightarrow\ \text{150 MHz 下必须拆成 2~3 拍}$$

## 五、为什么"拆拍"在架构上是安全的

引擎的内存口契约：

$$\text{读：} t \text{ 发 } \texttt{mem\_rd\_en} + \texttt{mem\_rd\_addr}，\ t+1 \text{ 用 } \texttt{mem\_rd\_data}$$

意味着 **CAM 查表、命中判定、条目号编码、槽位计算全部可以占用一整个周期**，
不需要组合完成。当前 RTL 把它们压在一拍里，这是 35 级的根源。

写侧：`ST_DRN` 有 **12 拍排空窗口**，可以吸收 1~2 拍的额外写延迟。

## 六、文件清单

| 文件 | 内容 |
|---|---|
| `endpoint_families.csv` | 按端点寄存器聚合的违例统计 |
| `unique_paths_full.rpt.gz` | 500 条完整路径（含每级 cell 名与延时）|
| `unique_paths.csv` | 去重后的路径摘要 |
| `timing_summary.rpt` | Vivado 时序汇总 |
| `utilization.rpt` | 资源报告 |

---

# 七、根因定位结果（4 个子代理交叉分析 + 实测）

## 7.1 已修复：线缓存溶成 65,536 个触发器

```
Synth 8-4767] Trying to implement RAM 'cache_a_reg' in registers
Reason is one or more of the following :
	1: RAM is sensitive to asynchronous reset signal. this RTL style is not supported.
Synth 8-13159] dissolved into (32768) registers bits
```

写口 `cache_a[rd_wr_idx] <= m_axi_rdata;` 原来在
`always_ff @(posedge s_axi_aclk or negedge s_axi_aresetn)` 里。**BRAM 不支持异步复位**。

修复：移到独立的无复位 `always_ff @(posedge s_axi_aclk)`。

**效果**：
| 指标 | 修前 | 修后 |
|---|---:|---:|
| LUT | 18,359 (39.7%) | **11,472 (24.8%)** |
| BRAM tile | 4 | **6**（两个线缓存成功进 RAMB36）|
| WNS | −11.449 | −11.200 |

## 7.2 剩余瓶颈：引擎的读地址生成（不是缓存 mux）

逐 cell 拆解最差路径后，**35 级里的真实构成**：

| 段 | 功能 | 级数 | 延时 | 归属 |
|---|---|---:|---:|---|
| **A** | **ST_WLD 权重装载地址**：`kk_q → wl_t比较 → wl_left → wl_nb → wl_tend → wl_rowend → wl_rbn/wl_j0n`（`wbase+wl_rb+wl_j0` **三级串联 32 位加法**）| **16** | **6.28 ns (38%)** | 引擎 `conv_int8_uni.v:398-411` |
| B | 32 bit 加法 + 3:1 读源 mux → `u_rd_addr` | 4 | 2.01 ns | 引擎 `:410-411, 424-426` |
| C | 26 bit tag 比较 ×32（已与 512:1 mux 选择树合并）| 5 | 4.70 ns | 包装 `:801` |
| D | `\|cam_rdy → rd_hit` | 1 | — | 包装 `:806` |
| E | `u_ce` 归约 → `u_ce_lat` | 1 | — | 包装 `:1198` |
| F | → 消费端 CE | 4 | 2.56 ns | 包装 `:1193-1194` |

$$\Rightarrow\ \text{引擎侧 } 20/35 \text{ 级，包装层 } 15/35 \text{ 级}$$

## 7.3 三个结构性发现

### (a) `u_ce` 是门控时钟，不是同步 CE

```verilog
always_latch if (!s_axi_aclk) u_ce_lat = u_ce;
assign u_clk = s_axi_aclk & u_ce_lat;      // → LUT2 + BUFG，第二套时钟网络
...
.ap_clk (u_clk)                            // 引擎 22,923 个 FF 挂在这里
```

后果：
- 实测 **SCD=1.949 / DCD=0.439 ⇒ −1.510 ns skew**，白扣在每条跨域路径上
- 形成自引用闭环：`引擎 FF → 地址 → CAM → u_ce → u_clk → 引擎 FF`

### (b) 级数预算：13 级是陷阱

每级代价**不是常数**：

| 级类型 | 逻辑 | 布线 | 合计 |
|---|---:|---:|---:|
| LUT1~LUT6 级 | 0.166 | **0.389** | **0.555 ns** |
| CARRY4 链内级 | 0.100 | 0.008 | **0.108 ns** |

校验：$23\times0.555 + 12\times0.108 + 4\times0.37 \approx 15.5\ \text{ns} \approx 16.4$ ✓

可用组合延时：

$$T_{\text{avail}} = 6.667 - 0.397_{\text{Tco}} - 0.068_{\text{setup}} - 0.035_{\text{unc}} - 1.510_{\text{skew}} = 4.66\ \text{ns}$$

$$\Rightarrow \text{每拍 } \le 8 \text{ 个 LUT 级；去门控后 } \le 11 \text{ 个}$$

$$\Rightarrow\ \textbf{13 个全 LUT 级 = 7.2 ns，放不下}$$

### (c) 降频不是退路，是死路

| 频率 | 周期 | 可用 | 现有 35 级 slack | 帧时间 |
|---|---:|---:|---:|---:|
| 150 MHz | 6.667 | 4.66 | −11.75 | 0.80 s |
| 125 MHz | 8.000 | 5.99 | −10.42 | 0.94 s |
| 100 MHz | 10.000 | 7.99 | **−8.42** | 1.15 s |
| 54 MHz | 18.52 | 16.51 | +0.10 | — |

$$\Rightarrow \textbf{100 MHz 也过不了}（\text{slack} -8.42），\text{且帧时间 1.15 s 已超 1 fps 预算}$$

## 7.4 修改清单（按收益/风险）

| # | 改动 | 位置 | 收益 | 风险 | 状态 |
|---|---|---|---|---|---|
| P0 | RAM 写口去异步复位 | 包装 `:997` | LUT −6,887，BRAM +2 | 无 | ✅ **已完成** |
| **P2** | **`mem_rd_addr` 改寄存地址指针 + `wl_addr` 改累加** | 引擎 `:398-411, 424-426` | **删除 20 级 / 8.3 ns** | 中 | 🔄 进行中 |
| **P1** | **`rd_live = 1'b1`** | 包装 `:842` | CE 路径 −17 级（192 条违例消失）| 低 | 🔄 进行中 |
| P5 | `rd_ent` 优先级链 → 显式 OR 树 | 包装 `:812-816` | −1~2 级 | 极低 | 🔄 进行中 |
| P3 | 门控时钟 → 同步 CE | 包装 + 引擎全局 | **+1.51 ns skew** + 2~3 级 | 中高 | 待做 |
| P4 | `dr_q == 11` → `>= 11`（重检粒度 16 拍 → 1 拍）| 引擎 `:486` | 修 bug | 无 | 🔄 进行中 |
| — | 写合并/预取地址拆拍 | 包装 | +1.3 ns | 低中 | 待做 |

### 绝对不要做

- **`rd_ent` 直接映射成 `tag[4:0]`**：平面步长是 2048 B 的倍数 ⇒ tag 低位跨平面全撞。
- **pblock / floorplan**：逻辑 35 级是深度问题，圈 pblock 只会加剧拥塞（7z015 太小）。
- **`-directive PerformanceOptimized`**：只值 0.5~2 ns，LUT +10~25%，解决不了 11 ns，不是主线。

## 7.5 需要注意的两点方法论

1. **`/tmp/rt_unique.rpt` 的 500 条路径是同一条的 500 份拷贝**（source 全是 `kk_q_reg[2]/C`）。`-unique_pins` 只按端点去重。真实多样性统计要用 `fam.csv`。
2. **−11.2 ns 是综合后 wireload 估计**（所有 net 标 `unplaced`），时钟偏斜 −1.510 ns 尤其不可信。**结构改完后 P&R 的真实值会差 ±1~2 ns，综合阶段要留 ≥ +1.5 ns 余量。**
