# 05 · 两个 AXI master 适配器内部机制（gmem1 写 / gmem0 读）

对象：`/data/work/hls_c8_csynth/r9_k1_l16/solution1/syn/verilog/`
- `conv2d_engine_gmem1_m_axi.v`（3,067 行，写口）
- `conv2d_engine_gmem0_m_axi.v`（2,443 行，读口）
- `conv2d_engine.v`（17,588 行，只取驱动/握手局部）

对照：`solution1/syn/report/conv2d_engine_csynth.rpt`、`solution1/solution1.log`

方法与边界：**纯静态读 RTL**。未编译、未仿真、未上板、未 commit。
凡"实测/已知"数字均注明是用户给定；凡推断标「推定」；凡没读到标「未覆盖」。
行号均为上述文件的**绝对行号**（不是 `sed -n` 相对偏移）。

> 阅读约定：`gmem1:456` = `conv2d_engine_gmem1_m_axi.v:456`；`top:7600` = `conv2d_engine.v:7600`。
> 两个适配器是 Vitis 2025.2.1 按 bundle **特化生成**的，不是同一份代码改参数：gmem1 文件里 load 单元是空的（`gmem1:238-240` 只有注释），gmem0 文件里 store 单元是空的（`gmem0:185-187`）。这一点在 §7 合并问题上是决定性的。

---

## 0. 一句话结论

1. 写口 gmem1 的适配器是**单 burst 引擎**：AW 的发出与 W burst 的描述符**硬耦合**（`gmem1:2598`），同一时刻只可能有 **1 笔 AW + 1 个 W burst** 在 AXI 引脚上；16 深的 B 信用（`gmem1:1377-1391`）永远用不满。
2. `USER_MAXREQS=4` 只决定 `fifo_wreq` / `user_resp` 的**缓冲能力**，它是第 5 / 第 6 级约束；真正先卡住的是**内核 FSM：每块只发 1 笔 AW 然后阻塞等 B**（`top:16035-16079` + `top:16565/16569`），以及**适配器 throttle 里 AW 与 W 的锁步**（`gmem1:2598`）。所以改 `num_write_outstanding` 只是把永远不满的队列加高。
3. 读口 gmem0 能跑满 burst，是因为它把「HLS 请求」和「AXI burst」**解耦**了：16 个 `fifo_rctl` 信用（`gmem0:995-1009`）+ 4096 拍 RBUFF（`gmem0:334,463-478`）让 AR 可以跑到 16 笔，而 R 数据不依赖内核消费速度（`out_AXI_RREADY = RBUFF 非满`，`gmem0:472`）。
4. 写侧没有对应物：W 数据虽然也有 512 拍缓冲（`gmem1:382,621-636`），但 AW 不许跑到 W 前面，缓冲深度用不上。
5. 两个 master 合并**不会在适配器内部互锁**（两套 FIFO/信用/状态机完全独立，只共享 AXI 端口），但会强制其中一路走「宽总线/窄用户」分支（`gmem1:647-1059` 或 `gmem0:506-785`，这两段在本版里都是**未被 elaborated 的死代码**），代价是额外的拆包/偏移 FIFO 逻辑。真正的带宽仲裁在 PS 互连里，**本文件未覆盖**。

---

## 1. 参数全表

### 1.1 顶层模块 `conv2d_engine_gmem1_m_axi`（`gmem1:10-36`）

实例化在 `top:7598-7620`，实际值以实例化覆盖为准。

| 参数 | 默认 | **实际值** | 含义 | 在本 build 里是否真的起作用 |
|---|---|---|---|---|
| `C_M_AXI_ID_WIDTH` | 1 | **1** | AXI ID 位宽 | 起作用；但 ID 恒为 0（`gmem1:1223-1238` 的压缩函数把任何 ID 映射成 0）|
| `C_M_AXI_ADDR_WIDTH` | 32 | **64** | 地址位宽 | 起作用（`top:329`）|
| `C_M_AXI_DATA_WIDTH` | 32 | **64** | 数据位宽 = 8 B/beat | 起作用（`top:330`）|
| `C_M_AXI_AW/AR/W/R/BUSER_WIDTH` | 1 | **1** | USER 位宽 | 恒驱 0（`gmem1:1277,1281`）|
| `C_TARGET_ADDR` | 0 | **0** | 地址加性偏移 | 值为 0 ⇒ 无平移、等价旁路（`gmem1:503`）|
| `C_USER_VALUE` | 0 | **0** | AWUSER/WUSER 常量 | 起作用（`gmem1:1277,1281`）|
| `C_PROT_VALUE` | 3'b000 | **0** | AWPROT/ARPROT | 起作用（`gmem1:1276`）|
| `C_CACHE_VALUE` | 4'b0011 | **3** | AWCACHE/ARCACHE | 起作用（`gmem1:1275`）→ 引脚 `AWCACHE=0011` |
| `CONSERVATIVE` | 0 | **1** | 写 burst 节流模式 | **关键**：=1 走 `conservative_gen`（`gmem1:531-613`）和 throttle 的 conservative 分支（`gmem1:2556-2602`）；=0 时那套 beat 计数/WLEN 握手整段不生成 |
| `MAX_READ_BURST_LENGTH` | 16 | **16** | 读 burst 上限 | **完全没用**：本实例 `NUM_READ_PORTS=0`（`gmem1:115`），read 单元未实例化（`gmem1:238-240` 空段、`gmem1:157-175` 全部 tie-off）|
| `MAX_WRITE_BURST_LENGTH` | 16 | **256** | 单 AXI burst 最多 256 beat | 起作用（`gmem1:1290` 传入 converter）|
| `NUM_READ_OUTSTANDING` | 16 | **0** | 全局读在途 | 没用（read 未实例化）|
| `NUM_WRITE_OUTSTANDING` | 16 | **16** | 全局写在途 | 部分起作用：只喂 `fifo_burst`/`fifo_wrsp`（`gmem1:548,1066`）和 bus_wide 死分支；**喂不到 AXI 侧信用**，见 §3.3 |
| `USER_MAXREQS` | 16 | **4** | HLS 侧请求 FIFO 深度 | **关键**：`fifo_wreq` 4 深（`gmem1:456-460`）、`user_resp` 4 深（`gmem1:1080-1083`）|
| `USER_LEN_WIDTH` | 32 | **32** | HLS 侧长度位宽 | 起作用；注意这个长度单位是**字**不是字节，见 §2.2 |
| `CH0_USER_DW` | 32 | **64** | 内核侧数据位宽 | = 总线宽 → 走 `bus_equal_gen`（`gmem1:640-646`），WSTRB 直通 |
| `CH0_USER_AW` | 32 | **64** | 内核侧地址位宽 | 起作用（`gmem1:473` 打成 96 位包）|
| `CH0_NUM_READ_OUTSTANDING` | 2 | **16** | 通道读在途 | 没用（read 未实例化）|
| `CH0_NUM_WRITE_OUTSTANDING` | 2 | **16** | 通道写在途 | **这才是 AXI 侧信用深度**（`gmem1:258` → `ID0_NUM_WRITE_OUTSTANDING`）|
| `CH0_USER_RFIFONUM_WIDTH` | 6 | **9** | R FIFO 计数位宽 | 只决定 tie-off 端口位宽（`gmem1:175`）|
| `MAXI_BUFFER_IMPL` | "block" | **"block"** | 大 FIFO 的 RAM 风格 | 起作用（`gmem1:207`）；`WBUFF_DEPTH=512>32` ⇒ `PREFERRED_IMPL="block"`（`gmem1:383`）|

派生 localparam（`gmem1:373-385`）：

| 名字 | 式子 | 实际值 |
|---|---|---|
| `USER_DATA_WIDTH` | `calc_data_width(64)` | 64 |
| `USER_DATA_ALIGN` | 64 | 64 |
| `USER_ADDR_ALIGN` | `log2(8)` | 3 |
| `WREQ_PACK_WIDTH` | `64+32` | **96** |
| `BUS_DATA_BYTES` / `BUS_ADDR_ALIGN` | `64/8` / `log2(8)` | 8 / 3 |
| `WBUFF_DEPTH` | `(USER_DATA_ALIGN==BUS_DATA_WIDTH) ? 2*256 : 256` | **512** |
| `PREFERRED_IMPL` | `(512<=32)?"shiftreg":BUFFER_IMPL` | **"block"** |
| `BURST_LEN_WIDTH` | `max(log2(513),8)` | 10 |
| `TARGET_ADDR` | `0 & (...)` | 0 |

### 1.2 子模块 `..._store`（`gmem1:318-331`）

| 参数 | 值 | 位置 |
|---|---|---|
| `CONSERVATIVE` | 1 | `gmem1:320` |
| `C_TARGET_ADDR` | 0 | `gmem1:321` |
| `NUM_WRITE_OUTSTANDING` | **16**（= `CH0_NUM_WRITE_OUTSTANDING`）| `gmem1:199,322` |
| `MAX_WRITE_BURST_LENGTH` | 256 | `gmem1:323` |
| `BUS_ADDR_WIDTH` / `BUS_DATA_WIDTH` | 64 / 64 | `gmem1:324-325` |
| `BYTE_ADDR_MODE` | **0**（默认，未覆盖）| `gmem1:326` |
| `USER_DW` / `USER_AW` / `USER_LEN_WIDTH` | 64 / 64 / 32 | `gmem1:327-329` |
| `USER_MAXREQS` | **4** | `gmem1:330` |
| `BUFFER_IMPL` | "block" | `gmem1:331` |

`BYTE_ADDR_MODE=0` 的效果：`wreq_addr_byte = wreq_addr << 3`、`wreq_len_byte = wreq_len << 3`（`gmem1:488-491`）。即 **HLS 侧长度单位是 64-bit 字**，模块内先左移 3 变成字节数。这个命名（端口叫 `AWLEN`）非常容易误读，见 §2.2。

### 1.3 子模块 `..._write`（`gmem1:1113-1129`）

| 参数 | 值 | 位置 |
|---|---|---|
| `CONSERVATIVE` | 1 | `gmem1:1114` |
| `BUS_ADDR_WIDTH` / `BUS_DATA_WIDTH` | 64 / 64 | `gmem1:1122-1123` |
| `MAX_WRITE_BURST_LENGTH` | 256 | `gmem1:1125` |
| `NUM_WRITE_OUTSTANDING` | 16 | `gmem1:1126` |
| `ID0_NUM_WRITE_OUTSTANDING` | **16** | `gmem1:1127` |
| `NUM_WRITE_PORTS` | **1** | `gmem1:1128` |

`NUM_WRITE_PORTS=1` 决定 converter 走 `burst_sequential` 而不是 `burst_interleave`（`gmem1:1438` 的条件 `(INTERLEAVE==1)&&(NUM_PORTS>1)` 为假 → `gmem1:1468`）。

### 1.4 throttle（`gmem1:2409-2417`）

| 参数 | 值 | 说明 |
|---|---|---|
| `CONSERVATIVE` | 1 | 选 `gmem1:2556-2602` 分支 |
| `NUM_OUTSTANDING` | 16 | **形参接了但从未被引用**（全模块只有 `ID0_NUM_OUTSTANDING` 进 `num_outstanding_val`，`gmem1:2474-2482`）|
| `ID0_NUM_OUTSTANDING` | **16** | 真正决定 `fifo_resp`/`fifo_req` 深度（`gmem1:2567-2568,1379-1380`）|

### 1.5 `burst_sequential` 内部 localparam（`gmem1:2041-2046`）

```
PACK_WIDTH     = ID_WIDTH+ADDR_WIDTH+LEN_WIDTH = 1+64+32 = 97
DATA_BYTES     = 64/8 = 8
ADDR_ALIGN     = log2(8) = 3
BOUNDARY_BEATS = {12-3{1'b1}} = 9'b111111111  // 4KB 边界内的最大 beat 数
NUM_BEAT_WIDTH = log2(256) = 8
```

### 1.6 gmem0 侧（读）

顶层（`gmem0:10-36`，实例化 `top:7506-7528`）：

| 参数 | 实际值 | 备注 |
|---|---|---|
| `C_M_AXI_ID_WIDTH` / `ADDR_WIDTH` / `DATA_WIDTH` | **1 / 64 / 32** | 4 B/beat（`top:316-318`）|
| `C_CACHE_VALUE` / `PROT` | 3 / 0 | 引脚 `ARCACHE=0011` |
| `CONSERVATIVE` | **1** | 读路径没有 throttle，这个参数只影响写单元（未实例化）→ 实际无作用 |
| `MAX_READ_BURST_LENGTH` | **256** | 起作用（`gmem0:953`）|
| `MAX_WRITE_BURST_LENGTH` | 16 | 没用（`NUM_WRITE_PORTS=0`，`gmem0:116`）|
| `NUM_READ_OUTSTANDING` | **16** | 传进 `read` 模块（`gmem0:239`），但通道 0 实际用 `ID0_` 那份（`gmem0:884`）|
| `NUM_WRITE_OUTSTANDING` | 0 | 没用 |
| `USER_MAXREQS` | **7** | `fifo_rreq` 7 深（`gmem0:394-397`）|
| `CH0_NUM_READ_OUTSTANDING` | **16** | 真正的 AR 信用深度（`gmem0:241` → `ID0_NUM_READ_OUTSTANDING`）|
| `CH0_USER_DW` / `CH0_USER_AW` | **32 / 64** | = 总线宽 → `bus_equal_gen`（`gmem0:497-505`）|
| `CH0_USER_RFIFONUM_WIDTH` | **13** | `log2(4096)+1`，与 RBUFF 一致 |
| `MAXI_BUFFER_IMPL` | "block" | RBUFF 用 block RAM |

`load` 模块派生（`gmem0:327-336`）：

```
RREQ_PACK_WIDTH = 64+32 = 96
RBUFF_DEPTH     = NUM_READ_OUTSTANDING * MAX_READ_BURST_LENGTH
                = 16 * 256 = 4096        // gmem0:334
PREFERRED_IMPL  = (4096<=32) ? "shiftreg" : BUFFER_IMPL = "block"
```

即 **RBUFF 4096 × 34 bit**（32 数据 + 2 位 `RLAST`，`gmem0:465`）——与已知的「4096×34」口径一致。

---

## 2. 写通道结构（gmem1）

### 2.1 结构图

```
 conv2d_engine.v (内核 FSM)                    conv2d_engine_gmem1_m_axi.v
 ─────────────────────────                    ───────────────────────────────────────────────
 state151/159
 gmem1_0_AWVALID ─┐
 gmem1_0_AWLEN=64 │   (单位: 64-bit 字)
 gmem1_0_AWADDR ──┴──► I_CH0_AW* ──► fifo_wreq  4 × 96b   (gmem1:456-470)
                                     │  full → I_CH0_AWREADY
                                     ▼  next_wreq = valid && ready_for_wreq && wrsp_ready (476)
                                  [AW 预处理]  单寄存器 tmp_valid  (508-518)
                                     │  ① 字→字节左移 3 (488-491)
                                     │  ② tmp_len = len_byte-1; tmp_addr = addr_byte (501-504)
                                     │  out_AXI_AWLEN / AWADDR / AWVALID (480-482)
                                     ▼
 ─────────────────────────── 写模块 bus_write ───────────────────────────
  burst_converter(sequential)  ← rs_req 2 深 skid (2116-2126)
     · 4KB 切段 + 256-beat 切 burst (2180-2191, 2297-2342)
     · read_req：一条 HLS 请求的最后一个 burst 发完才收下一条 (2142)
     · 每 burst 输出 {ADDR,LEN,VALID} + 一个 ctrl 信用 (2320-2323)
                                     │
                                     ▼
  throttle (conservative)        fifo_req  16 × 64b  (2564-2580)
     · rs_burst 单描述符 {WID,WLEN} (2582-2592)
     · ASK: local_BURST_AWVALID = burst_valid[WID] && local_BURST_WVALID && ~burst_handling
            ↑ AW 必须等这一笔的 W 描述符就位 (2598)   ★ 本节最关键的一行
     · rs_req 2 深 skid → out_BUS_AW* (2511-2521)
                                     │
 ────────────────────────────────────┼──────────────────────────────────────
                                     ▼
                              AXI4 引脚 (64b, ID=0, INCR, AWSIZE=3)
                                     │
 state152-153 / 160-161
 gmem1_0_WVALID ─┐
 gmem1_0_WDATA  ─┼──► buff_wdata 512×72b ──► bus_equal_gen ──► W beat 引擎 ──► WDATA/WSTRB/WLAST
 gmem1_0_WSTRB  ─┘      (621-636)             (640-646)        (2614-2672)      (到引脚)
                          │ full_n = I_CH0_WREADY (644)         单 WVALID/WDATA/WLAST 寄存器
                          │                                       num_beat_cnt 0..WLEN
                          ▼
                    内核最多跑前 512 拍
 ─────────────── 响应/反压回路（与 W 数据流向相反）───────────────
 state158/166 ◄── gmem1_0_BVALID ◄── user_resp 4×1b (1080-1094) ◄── fifo_wrsp 16×1b (1064-1078)
 gmem1_0_BREADY ──────────────────►  out_AXI_BREADY = wrsp_type && ursp_ready (1099)
                                     ▲
                              write 模块 fifo_resp 16×1b (1375-1393)
                              out_AXI_BVALID = ost_resp_info & resp_valid (1401)
```

### 2.2 AW FIFO → 命令处理

**① `fifo_wreq`（`gmem1:456-470`）**

```verilog
.DATA_WIDTH (WREQ_PACK_WIDTH=96)   // {AWLEN[31:0], AWADDR[63:0]}
.ADDR_WIDTH (log2(USER_MAXREQS)=2)
.DEPTH      (USER_MAXREQS=4)
...
.if_full_n  (out_HLS_AWREADY)      // 464: 满 → 内核 AWREADY 拉低
.if_empty_n (wreq_valid)
.if_read    (next_wreq)
```
DEPTH=4 且 `MEM_STYLE` 缺省 `"shiftreg"`（`gmem1:2783`）→ 走 `_srl` 实现（`gmem1:2862-2876`），96×4 的移位寄存器组。

**② 弹出条件（`gmem1:476`）**

```verilog
assign next_wreq      = wreq_valid && ready_for_wreq && wrsp_ready;
assign ready_for_wreq = ~tmp_valid || (in_AXI_AWREADY && wreq_ready);   // 477
```
`wrsp_ready` 是 `fifo_wrsp` 的 `full_n`（16 深，`gmem1:1072`），即"还有 B 信用"。

**③ AW 预处理只有一个出口寄存器（`gmem1:508-518`）**

```verilog
if (next_wreq && valid_length)   tmp_valid <= 1'b1;
else if (in_AXI_AWREADY && wreq_ready) tmp_valid <= 1'b0;
```
`out_AXI_AWVALID = tmp_valid && wreq_ready`（482）。**同一时刻适配器只向总线呈现 1 笔 AW**；下一笔必须等 `fifo_wreq` 里的第 2 条被弹出来，再等一个时钟沿。

**④ 单位换算（最容易踩的坑）**

- 内核给的 `I_CH0_AWLEN` 是**字数**（64-bit 字），不是 AXI 的 beat 数，也不是字节数。
- `gmem1:488-491`：`wreq_len_byte = wreq_len << 3`。
- `gmem1:502`：`tmp_len = wreq_len_byte - 1` → 这是"字节数 − 1"，挂在 `out_AXI_AWLEN` 上（480），**它并不是 AXI AWLEN**。
- 真正的 AXI AWLEN 在 converter 里算：`end_addr_tmp = req_len_tmp + req_addr_tmp[11:0]`（`gmem1:2139`），`beat_total_tmp = (req_len_tmp[11:0] + addr_off) >> 3`（`gmem1:2140`），再切段/切 burst 后由 `out_BURST_LEN` 输出（`gmem1:2316`）。
- 反推：实测引脚 `AWLEN=63`（64 beat × 8 B = 512 B）⇔ 内核 `I_CH0_AWLEN=64` ⇔ `tmp_len=511` ⇔ `beat_total=511>>3=63`。**三者自洽**，也确认了「单位是字」这一判断。

### 2.3 burst 拆分与 beat 计数（conservative 模式）

`CONSERVATIVE=1` 时 store 里有一段**独立的 beat 计数器**（`gmem1:531-613`）：

- `fifo_burst`：8 bit × **16** 深（`gmem1:546-560`），写入端是 `in_BURST_AWVALID`（来自 write 模块的每 burst 描述符），读出端是 `burst_len`。
- `next_burst = burst_valid && burst_ready && ~num_beat_pred_br10[BURST_LEN_WIDTH]`（565）
- `beat_write = local_AXI_WREADY && local_AXI_WVALID`（566）
- `num_beat_cnt` 做加减预测（590-611）

这段的作用：store 一边数已经发出去的 beat（`beat_write`），一边预估当前这笔还剩几拍（`num_beat_pred_br10 = num_beat_cnt - burst_len - 1`，591），在还剩 1 拍时就提前把 `fifo_burst` 里的**下一笔 WLEN** 弹出来（`next_burst`，565）。这样 throttle 的 `local_BURST_WLEN` 能无缝切换、它自己的 `num_beat_cnt == WLEN` 比较（2619）不会出现空泡。它是**描述符流水**，不碰数据。

### 2.4 W beat 发送与 WSTRB 生成

- **WSTRB 不生成，直通**：`USER_DATA_ALIGN(64) == BUS_DATA_WIDTH(64)` → `bus_equal_gen`，`assign local_AXI_WSTRB = in_HLS_WSTRB`（`gmem1:642`）。内核两处写流水线都把它写成常量 `8'd255`（`..._Pipeline_VITIS_LOOP_945_50.v:545`、`..._967_51.v:546`），所以引脚上永远是全字节写。
- **WLAST 由适配器按 burst 长度生成**：`num_beat_cnt == local_BURST_WLEN`（2619）→ `next_burst` → `local_BUS_WLAST <= 1`（2655-2656），下一拍随最后一个 beat 送出。内核侧接口**没有 WLAST 端口**（`gmem1:98-101`），流水线里那个 `WLAST=1'b0`（945_50:543、967_51:544）是悬空输出。
- **W 数据缓冲**：`buff_wdata` 512 × (64+8)（`gmem1:621-636`），`if_full_n = local_AXI_WREADY` → 内核 `I_CH0_WREADY`（644）。所以内核最多可以比 AXI 引脚**跑前 512 拍**。
  - 但注意 `gmem1:382`：`WBUFF_DEPTH = 2*MAX_WRITE_BURST_LENGTH = 512`，是"2 个最大 burst"，**不是** `NUM_WRITE_OUTSTANDING × MAX_WRITE_BURST_LENGTH`（那会是 4096）。这是读/写两侧缓冲哲学差异的直接体现。
- **每拍吞吐**：`next_beat = in_AXI_WVALID && local_AXI_WREADY`（2620），`local_BUS_WDATA/WSTRB/WVALID` 是单级输出寄存器（2622-2648）。WREADY 常高时可**连续 1 beat/拍**，无气泡。
- **AW 与 W 的锁步**：`local_AXI_WREADY = local_BURST_WVALID && ready_for_burst && ready_for_beat`（2614）——没有活跃 W 描述符时，**W 数据一拍都发不出去**（即使 `buff_wdata` 是满的）。
- **`burst_handling` 把 AWVALID 压成"每 burst 一个单拍脉冲"**（2523-2533, 2598）：`burst_handling` 在 burst 的第一个活动拍之后就被置 1 并保持到最后一拍（因为 `local_BURST_WREADY` 只在最后一拍为 1），`~burst_handling` 于是把 AWVALID 限制在每笔 burst 的**第一拍**。若这一拍 `AWREADY=0`，`ready_for_burst=0` ⇒ `local_AXI_WREADY=0`，**整个 burst 的 W beat 一起等**，直到 AW 被接收才放行。
  ⇒ 因此 **AW 的握手延迟是逐 burst 串在关键路径上的**，不是被流水隐藏的；这是"写侧跑不满 burst"在适配器内部最直接的机制。
- AXI 允许 W 早于 AW，所以上面这种"W 等 AW"不是协议错误，纯粹是这套 RTL 的保守实现。

### 2.5 B 响应收集

三级：

| 级 | 位置 | 深度/位宽 | 作用 |
|---|---|---|---|
| `fifo_resp`（总线侧信用）| `gmem1:1375-1393` | **16 × 1b**，写 `ost_ctrl_info` | 每个 burst 压一个"是否本请求最后一个 burst"标记；`if_full_n = ost_ctrl_ready` 反压 converter 的 `next_loop`（2327）|
| `fifo_wrsp`（适配器→内核）| `gmem1:1064-1078` | 16 × 1b，写 `valid_length` | 每笔 **HLS 请求**一个条目 |
| `user_resp`（给内核的 B）| `gmem1:1080-1094` | **4 × 1b** | `if_empty_n = I_CH0_BVALID` |

握手（`gmem1:1097-1099`）：

```verilog
assign ursp_write     = wrsp_valid && (!wrsp_type || in_AXI_BVALID);  // 无效长度请求直接造 B
assign wrsp_read      = ursp_ready && ursp_write;
assign out_AXI_BREADY = wrsp_type && ursp_ready;                     // BREADY 组合依赖 user_resp 非满
```

总线上（`gmem1:1363-1402`）：

```verilog
rs_resp : s_valid=in_BUS_BVALID, s_ready=out_BUS_BREADY      // 1363-1373
resp_ready = ost_resp_valid[resp_id] && (in_AXI_BREADY[resp_id] || ~ost_resp_info[resp_id]);  // 1398
out_AXI_BVALID = ost_resp_info & bit_set(resp_id, resp_valid);   // 1401  ← 只有"本请求最后一 burst"才给内核 B
```

三个必须指出的点：

1. **`BRESP` 完全没用**。`in_BUS_BRESP` 只是端口（`gmem1:1158`），全文件再无引用；`BUSER` 同样（1159）。**写错误（SLVERR/DECERR）被静默丢弃**，内核永远看到"成功"。
2. **`BID` 只用于选 FIFO，且被压缩成 0**：`local_BUS_BID = compress_axi_id(in_BUS_BID)`（1402），而 `compress_axi_id` 对任何输入都返回 0（`gmem1:1223-1229`）。`ID_WIDTH=1` ⇒ 本来就只有 ID 0，等价于忽略。
3. **一笔 HLS 请求只回一个 B**（`ost_ctrl_info = last_loop && last_sect_buf`，`gmem1:2321`），不是每个 AXI burst 一个 B。本设计 64 beat < 256，所以一个请求就是一个 burst，两者重合。

---

## 3. `USER_MAXREQS=4` 到底卡在哪一级

### 3.1 逐级深度表（按"先满"排序）

| # | 级 | 深度 | 位置 | 会不会先满 |
|---|---|---|---|---|
| 1 | **内核写 FSM**：一块只发 1 笔 AW，然后等 B | **1** | `top:16035-16079` + `top:16565/16569` | **✔ 先卡，而且根本不会超过 1** |
| 2 | **throttle 的 AW/W 锁步**：AWVALID 需要本笔 W 描述符，且被 `burst_handling` 压成单拍 | **1 burst** | `gmem1:2598`（+ `2582-2592` 单描述符、`2523-2533` 脉冲成形）| **✔ 第二卡**；AW 引脚在途恒 ≤1，且 AW 延迟逐 burst 串行 |
| 3 | converter 的 HLS 请求接收 | **1 请求** | `gmem1:2142`（`read_req = last_sect_tmp & next_sect \| ~req_handling`）| 只有"连续小请求"时才卡 |
| 4 | store 的 `tmp_valid` | **1 AW** | `gmem1:508-518` | 同上 |
| 5 | `fifo_wreq` | **4** | `gmem1:456-460` | ✘ 前 4 级已经限流 |
| 6 | `user_resp`（B 给内核） | **4** | `gmem1:1080-1083` | ✘ 内核每块只取 1 个 B |
| 7 | `fifo_wrsp` | 16 | `gmem1:1064-1068` | ✘ |
| 8 | `fifo_resp` / `fifo_req` | 16 / 16 | `gmem1:1377-1380` / `2564-2568` | ✘ |

**结论：卡点是 #1 和 #2，不是 4 深 FIFO。** `USER_MAXREQS=4` 是"能装 4 笔"的缓冲，但上游每块只递 1 笔、下游每笔要占满一个 burst，所以它永远不会被填满。

### 3.2 内核侧的"1 笔 AW → 等 B"证据链

1. **AW 只发一拍，且组合依赖 AWREADY**（`top:9803-9813`）：
   ```verilog
   if ((gmem1_0_AWREADY==1'b1) & (state159|state151)) gmem1_0_AWVALID = 1'b1;
   ```
2. **状态迁移也依赖 AWREADY**（`top:16035-16041`）：`state151 → state152` 仅在 `AWREADY==1` 时发生。一拍一笔，没有第二笔。
3. **state152/153 跑写数据流水线**（`top:16042-16051` → `LOOP_945_50`；`state160/161` → `LOOP_967_51`）。
4. **然后阻塞等 B**：
   ```verilog
   ap_block_state158 = ((gmem1_0_BVALID == 1'b0) & (icmp_ln657_reg_8651 == 1'd1));   // top:16565
   ap_block_state166 = ((ap_predicate_op1744_writeresp_state166 == 1'b1) &
                        (gmem1_0_BVALID == 1'b0));                                   // top:16569
   ```
   BREADY 只在被阻塞时才拉高（`top:9815-9825`），即"等到 B 才走"。
5. 所以每一块的时间 = AW 1 拍 + 64 个 W beat + **B 往返延迟**；此外适配器内部的 W 首拍还要等 AW 在引脚上被接收（§2.4），这段延迟也是串行暴露的。两笔之间不可能重叠。

补一条**与 doc 04 的出入**：04 的结尾写「写回仍是 1-beat 事务（`AWLEN=0`）」。`AWLEN=0` 是 `LOOP_945_50` 里的**悬空输出**（`945_50:533` 把 `m_axi_gmem1_0_AWVALID` 常量 0，`519` 把 `AWLEN` 常量 0，构建日志 `solution1.log:766-777` 也报了 "Setting dangling out port"）。真正上总线的 AW 由**顶层 FSM** 发（`top:9775-9813`），长度 63。**这条以本文为准。**

### 3.3 为什么改 `num_write_outstanding` 无效（RTL 级）

三个独立原因，任何一条都足以让收益为 0：

**(a) 它改的不是 AXI 侧那份信用。**
本 build 里两个数都硬写 16：`NUM_WRITE_OUTSTANDING(16)`（`top:7620`）和 `CH0_NUM_WRITE_OUTSTANDING(16)`（`top:7615`）。前者只用于 `fifo_burst`（`gmem1:548`）、`fifo_wrsp`（1066）和 bus_wide 死分支（720/944）；后者才是 `ID0_NUM_WRITE_OUTSTANDING`（`gmem1:258`）→ `fifo_resp`/`fifo_req` 的 16 深。**如果当时只改了 pragma 的全局值而没有等量改到通道值，AXI 侧深度根本没变。**

**(b) throttle 的 `NUM_OUTSTANDING` 是个死参数。**
`gmem1:1323` 把 `NUM_WRITE_OUTSTANDING` 接到 `.NUM_OUTSTANDING`，但 throttle 模块体内（`gmem1:2408-2674`）**没有任何一处引用 `NUM_OUTSTANDING`**；`num_outstanding_val()` 只看 `ID0_NUM_OUTSTANDING`（`gmem1:2478`）。所以这条路连"改大一点"的物理通路都不存在。

**(c) 就算两边都改到 64，也没有第 5 笔请求来填。**
#1 内核 FSM 每块只发 1 笔 AW；#2 AW 引脚在途恒 ≤1。**队列再深，也没有并发可装。** 这与 13 个微基准逐项一致的实测结果完全吻合。

同理，改 IP 声明的 `HAS_BURST=1` 零收益也可以直接解释：**这两个 RTL 文件里根本没有 `HAS_BURST` 这个字符串**（`grep -c HAS_BURST` = 0/0/0），`AWBURST` 是硬连的 `2'b01`（`gmem1:1273`）、`ARBURST` 也是 `2'b01`（`gmem0:938`）。`HAS_BURST` 只影响 IP 打包/互连侧的元数据，不会改变这份已生成 RTL 的任何行为。

---

## 4. 读通道结构（gmem0）

### 4.1 结构图

```
 conv2d_engine.v (内核 FSM)                    conv2d_engine_gmem0_m_axi.v
 ─────────────────────────                    ───────────────────────────────────────────────
 gmem0_0_ARVALID ──► I_CH0_AR* ──► fifo_rreq  7 × 96b  (gmem0:394-408)
 gmem0_0_ARLEN (字)                 │  full → I_CH0_ARREADY
                                     ▼  next_rreq (414)   ← 注意：这里**没有** wrsp 那种 B 信用
                                  [AR 预处理] 单寄存器 tmp_valid (432-456)
                                     │  字→字节 <<2 (427-428); tmp_len = len_byte-1 (440)
                                     ▼
 ─────────────────────────── 读模块 bus_read ────────────────────────────
  burst_converter(sequential)  (947-973, 1656-2048)
     · 4KB 切段 + 256-beat 切 burst (1831, 1940-1973)
     · ost_ctrl_valid = next_loop (1963)；next_loop = read_loop && ost_ctrl_ready (1970)
                                     │ 每 burst 一个信用
                                     ▼
                        fifo_rctl  16 × 1b  (995-1009)   ← ★ AR 在途 = 16 笔
                                     │  ost_ctrl_ready = full_n
                                     ▼
                              ARVALID (out_BUS_ARVALID = burst_valid)
                                     │
 ────────────────────────────────────┼──────────────────────────────────────
                                     ▼
                              AXI4 AR (32b, ID=0, INCR, ARSIZE=2)
                                     │
                              R beat ──► rs_rdata (981-991, 34b 单级)
                                     ▼
                        buff_rdata 4096 × 34b  (463-478)   ← ★ 16 KB 弹性缓冲
                          if_full_n = out_AXI_RREADY (472)  ← 只受缓冲限制，不受内核限制
                                     │
 gmem0_0_RREADY ◄── out_HLS_RVALID ◄─┘ (bus_equal_gen 497-505, 直通)
 gmem0_0_RDATA  ◄── local_AXI_RDATA
 gmem0_0_RFIFONUM ◄─ buff_rdata.if_num_data_valid (13 bit)
```

### 4.2 AR 侧

- `fifo_rreq` **7 深**（`gmem0:394-397`，`USER_MAXREQS=7`）：这是内核能"提前递几张 AR 单"的深度。注意它比写侧的 4 深**宽**，但这不是读快的原因（原因在下面两级）。
- `next_rreq = rreq_valid && ready_for_rreq`（`gmem0:414`），**没有**写侧那个 `wrsp_ready` 项——读不需要等任何"响应信用"才收新请求。
- `tmp_valid` 同样只有一个（`gmem0:446-456`），所以**HLS 请求一次也只递一笔**给 read 模块；但一笔请求会在 converter 里展开成很多 burst。

### 4.3 真正的并行度：`fifo_rctl` 16 笔 + 4096 拍缓冲

```verilog
fifo_rctl : DATA_WIDTH=1, DEPTH=num_outstanding_val(idx)=ID0_NUM_READ_OUTSTANDING=16   // gmem0:995-1009
fifo_burst: 同深度 16，记录"本 burst 是否请求最后一个"                                  // gmem0:1011-1025
ost_ctrl_write = bit_set(ost_ctrl_id, ost_ctrl_valid)                                   // 1029
next_ctrl      = in_BURST_RREADY & ost_ctrl_empty_n                                     // 1032
next_burst     = last_beat && beat_valid && beat_ready                                   // 1033
```

- `ost_ctrl_ready = fifo_rctl.full_n`（`gmem0:1921/1966` 经 `in_CTRL_READY` 进来）→ converter 的 `next_loop` 被它门控（1970）→ **AR 最多 16 笔在途**。
- 信用归还条件是"某笔 burst 的 RLAST beat 被**内核**取走"：`ready_for_outstanding` 在 `local_AXI_RREADY && local_AXI_RVALID` 时采样 `RLAST[1]`（`gmem0:481-492`）。所以信用是被"消费"退回的，不是被"接收"退回的——这正是深缓冲存在的意义。
- `in_BURST_RREADY` 进到 read 模块后，`out_AXI_RLAST[1]` 才标"整个 HLS 请求的最后一 beat"（`gmem0:1038`）。

### 4.4 为什么读能跑满 burst、写不行

| 维度 | 读（gmem0） | 写（gmem1） |
|---|---|---|
| 地址与数据是否解耦 | **是**：AR 由 `fifo_rctl` 信用驱动，R 数据独立流入 4096 深缓冲 | **否**：`AWVALID` 硬依赖本笔的 W 描述符（`gmem1:2598`）|
| 在途上限 | **16 笔 burst**（`fifo_rctl`）| **1 笔 burst**（单 W 描述符 + 单 W 引擎）|
| 数据缓冲 | 4096 拍 = 16 × 256，正好覆盖 16 笔最大 burst（`gmem0:334`）| 512 拍 = 2 × 256（`gmem1:382`），只够 2 笔最大 burst |
| 反压来源 | 只受 RBUFF 满（`gmem0:472`）| 受 W 描述符 + AW 握手 + WREADY 三重（`gmem1:2614`）|
| 有没有响应通道拖累 | 无 | 有 B，且内核每块等 B |
| 每拍能力 | 1 beat/拍 = **4 B/拍** | 1 beat/拍 = **8 B/拍** |

一句话：**读侧把"HLS 请求 → AXI burst"这一层做成了带 16 个信用的流水；写侧把"AW → W burst → B"做成了单条串行链。**

---

## 5. 内核侧握手：没有 `busy`，只有 `blk_n` / `ap_block`

- **两个适配器文件里 `busy` 出现 0 次**（`grep -c busy` = 0/0）。HLS m_axi 没有 busy 语义。
- 内核侧的字面握手信号只有：`I_CH0_AWVALID/AWREADY/AWADDR/AWLEN`、`I_CH0_WVALID/WREADY/WDATA/WSTRB`、`I_CH0_BVALID/BREADY`（`gmem1:94-103`）以及读侧的 `AR*/R*/RFIFONUM`（`gmem1:104-111`）。
- 内核 FSM 把它们包成两级"阻塞条件"：
  - `gmem1_blk_n_AW = m_axi_gmem1_AWREADY`，只在 state151/159 有效（`top:9857-9863`）
  - `gmem1_blk_n_B = m_axi_gmem1_BVALID`，只在 state158/166 有效（`top:9865-9871`）
  - 展开成 `ap_block_state158/166`（`top:16565/16569`）
- 因此"AW→W→B 串行"不是适配器强加的协议，而是**内核 FSM 用 `ap_block_state*` 自己串起来的**；适配器只是"照着做"。

**"发一笔等一次 B"在适配器内部的投影**（三处）：

1. `gmem1:476`：`next_wreq` 需要 `wrsp_ready`（B 信用）。如果内核真把 B 压满 16 个，这里会停——但内核每块只取 1 个，所以永不触发。
2. `gmem1:1398`（`resp_ready`）与 `gmem1:1401` 的 `out_AXI_BVALID`：只有"本请求最后一个 burst"才产生 B。
3. `gmem1:2614-2619`：W 引擎的 `next_burst` 把描述符在最后一拍弹出，下一笔 AW 要等这一刻之后——**AW 永远不会跑到 W 前面**。

---

## 6. 想做 N 笔在途写，改哪里

### 6.1 改适配器 RTL（生成物，会被下次 csynth 覆盖；仅适合验证/手工集成）

按依赖顺序，缺一不可：

| # | 位置 | 现状 | 要改成 |
|---|---|---|---|
| 1 | `gmem1:456-460` + `top:7600` | `fifo_wreq` 4 深 | 深度 ≥ N；`USER_MAXREQS` ≥ N |
| 2 | `gmem1:508-518` | 单 `tmp_valid` AW 出口 | N 深 AW 输出队列（或让 `fifo_wreq` 直接驱动 `out_AXI_AW*`，靠 `in_AXI_AWREADY` 反压）|
| 3 | `gmem1:2598` | `AWVALID = burst_valid && WVALID && ~burst_handling` | **解耦**：AWVALID 只看 `burst_valid[aw_ptr]`（地址 FIFO 已 16 深），不等 W 描述符 |
| 4 | `gmem1:2582-2592` | `rs_burst` 单 W 描述符 | N 深 W 描述符 FIFO（`store` 的 `fifo_burst` 本来就是 16 深，可复用）|
| 5 | `gmem1:2662-2672` | 单 `num_beat_cnt` | 每描述符一个 beat 计数（或至少流水化成"当前/下一"两级）|
| 6 | `gmem1:1080-1083` | `user_resp` 4 深 | ≥ N（否则内核没及时取走的 B 会反向堵住 BREADY→fifo_resp→AW）|
| 7 | `gmem1:1064-1068`、`1377-1380` | 16 深 | N ≤ 16 时不用动 |
| 8 | `gmem1:382` | `WBUFF_DEPTH = 2*256 = 512` | 若想让 W 数据也跑前 N 笔，改成 `NUM_WRITE_OUTSTANDING*MAX_WRITE_BURST_LENGTH`（16×256=4096 拍 × 72 bit ≈ 36 KiB）；按当前 `512×72 → 4 个 BRAM18`（`rpt:137`）线性外推约 32 个 BRAM18，直接爆掉这颗器件的余量 |
| 9 | `top:16035-16107`、`16565/16569` | 每块 1 AW 后等 B | 内核不改成"先发 k 笔 AW 再统一收 B"，上面 8 条全白改 |

**其中 #3 是分水岭。** 只要 AW 还被 `local_BURST_WVALID` 门控，AW 引脚在途就恒为 1，改多少深度都没用。改完 #3 后，AW 的发出速率可以从"1 笔/每 burst 时长"变成"1 笔/拍"，B 延迟就能被后续 burst 覆盖。

### 6.2 绕过适配器，自己写 AXI 状态机（推荐，若本来就要手写 kernel）

理由（都有 RTL 依据）：

1. **功能上**：适配器把 `BRESP`/`BUSER` 直接丢弃（`gmem1:1158-1159` 之后再无引用），你要做写错误检测必须绕开。
2. **结构上**：AW/W 锁步（2598）+ 单 W 引擎（2614-2672）不是参数能关掉的（`CONSERVATIVE=0` 只是换成另一种同样单 burst 的架构，`gmem1:2536-2554`）。
3. **成本上**：gmem1 适配器占 **727 FF / 844 LUT / 4 BRAM18**（`rpt:137`），gmem0 占 **675 FF / 662 LUT / 8 BRAM18**（`rpt:136`）。一个 64-bit INCR 写 master（AW 队列 + W 计数器 + B 计数）大概 300-500 LUT，比适配器小。
4. **代价**：你要自己处理 4 KB 边界切段、WLAST、B 超时/错误、以及 ID 顺序。本设计 ID 恒 0（`gmem1:1223-1238`），所以顺序简单。

**建议**：如果最终走手写 kernel（doc 04 的结论也指向这条），直接自己写 master，把 #3/#4/#5 一次做对；**不要**在生成的 `*_m_axi.v` 上打补丁——它是 csynth 的产物，下一次综合就没了。

---

## 7. 两个 master 能否合并成一个

### 7.1 内部的读/写会不会互锁 → **不会**

RTL 证据：

- 两套数据通路**没有任何共享寄存器/FIFO/信用**：写侧 `fifo_wreq/fifo_burst/buff_wdata/fifo_wrsp/user_resp/fifo_resp/fifo_req`，读侧 `fifo_rreq/buff_rdata/fifo_rctl`，各自独立。
- 唯一的交汇点是顶层那组**端口数组映射**（`gmem1:177-190`：`local_CHN_*[0]` ←→ 单一 AXI 总线）和 `NUM_READ_PORTS/NUM_WRITE_PORTS`（`gmem1:114-116`）。
- 所以在**适配器内部**，读不会等写、写不会等读；唯一可能的相互影响在 PS 侧 HP 口的仲裁与带宽（**未覆盖**，不在这两个文件里）。

### 7.2 但"合并"不是改个参数的事

这两个文件是**按 bundle 特化生成**的：

- `gmem1` 里 load 单元整段是空的：`gmem1:238-240` 只有 `// ================== LOAD UNITS ==================`，读端口全部 tie-off（`gmem1:157-175`，包括 `I_CH0_ARREADY=0`、`I_CH0_RVALID=0`、`RREADY=0`）。
- `gmem0` 里 store 单元整段是空的：`gmem0:185-187`，写端口全部 tie-off（`gmem0:147-170`）。
- 顶层模块**同时声明了** `I_CH0_AW*` 和 `I_CH0_AR*`（`gmem1:94-111` 两套都在），所以"同一个 channel 0 同时读写"在**端口层面**是合法的；缺的是对应的 store/load 单元实体，需要在 HLS 源码里用同一个 `bundle=` 绑定读写端口后**重新 csynth** 生成合并版。

### 7.3 合并的隐藏代价：必须有一路走"宽总线/窄用户"分支

两个 bundle 的数据宽不同（gmem1=64，gmem0=32）。合并后只能选一个宽度：

| 选 64-bit 合并 | 后果 |
|---|---|
| gmem1 写（64-bit）| 走 `bus_equal_gen`（现状，`gmem1:640-646`），无额外代价 |
| gmem0 读（32-bit）| 被迫走 `bus_wide_gen`（`gmem0:506-785`，**本版是未被 elaborated 的死代码**）：每拍 64-bit 拆成 2 个 32-bit，多出 `rreq_offset` FIFO（551-565）、`rs_rdata_int`（567-577）、`split_cnt/first_split/last_split` 一套拆分状态机（595-658）|

| 选 32-bit 合并 | 后果 |
|---|---|
| gmem0 读 | 无额外代价 |
| gmem1 写 | 被迫走 `bus_wide_gen` 写分支（`gmem1:647-1059`，**同样是死代码**）：每 2 个 32-bit beat 拼成 64-bit，多出 `wreq_offset`（718-732）、`buff_wdata_in`（734+）、padding 选择（787-905）、`strb_buf/data_buf`（1027-1057）。**写带宽还从 8 B/拍掉到 4 B/拍。** |

也就是说：**合并省一个 HP 口，代价是给其中一路补 300~400 行拆分逻辑，且 32-bit 合并方案会让写口带宽减半。** 由于本设计读（≤4 B/拍）和写在时间上基本不重叠（写集中在 state151-166，读在其余状态），合并的收益是"省端口/省布线"，不是"提速"。

### 7.4 结论

- **内部不互锁**，可以合并；正确性上唯一要注意的是 ID 恒 0（合并后仍成立，读写在 AXI 上本来就不保序）。
- **合并的实际收益是省一个 HP 口和一份适配器实例**（约 700 FF/800 LUT/4-8 BRAM），代价是重新 csynth 一次 + 一路走拆分路径。
- **如果目标只是"写回更快"，合并没有帮助**——瓶颈不在这两个实例的接口数量上（§8）。
- 若只是为了减少端口而合并，**建议合并到 64-bit**（保住写带宽），让 32-bit 的 gmem0 去走 `bus_wide_gen` 读路径；读路径本来就有 4096 拍缓冲，加一层拆分对吞吐影响远小于写。

---

## 8. 与实测数字的数量级核对（算术，不是仿真）

前提：用户给的三个数是"拍/字节"（cycle per byte）。

| 通道 | 引脚能力 | 实测 | 占引脚能力的比例 |
|---|---|---|---|
| gmem1 写 | 8 B/拍 | 5.30 拍/字节 ⇒ 0.189 B/拍 | **2.4%** |
| gmem0 填充读 | 4 B/拍 | 1.08 拍/字节 ⇒ 0.926 B/拍 | **23%** |
| gmem0 权重读 | 4 B/拍 | 2.76 拍/字节 ⇒ 0.362 B/拍 | 9% |

推论（明确标「推定」）：

1. 三条通道都离引脚结构上限很远，**适配器的串行化不可能是主因**。这与"13 个微基准逐项一致"的零收益结论自洽。
2. 写口 5.3 拍/字节意味着平均 **42 拍/beat**（8 B/beat）。按 §2.4，适配器每块 512 B 的理论开销约为「AW 往返延迟 + 64 拍 W + B 往返延迟」；即使把 AW 和 B 的往返各按 30 拍估，一块也只需 ~124 拍 ≈ **0.24 拍/字节**。差一个数量级以上 ⇒ 时间花在**数据生产侧**（内核流水线/上游依赖），不在 AXI 适配器。**注意这一条恰好说明：把 B 等待藏掉（N 笔在途）最多也只能省掉那 ~30 拍/块（约 0.06 拍/字节），救不了 5.3。**
3. 读口 1.08 拍/字节对 4 B/beat 的端口是 ~4.3 拍/beat，也远低于 1 拍/beat 的能力 ⇒ 读侧同样没吃饱；`fifo_rctl` 的 16 笔信用没有被用满。
4. ⚠️ 口径存疑：如果"拍/字节"不是端口级（总拍数/总字节），而是"每输出字节摊到的拍数（含计算）"，则以上第 2 条要重算。**这一点没有在 RTL 里读到定义，标「存疑」。**

---

## 9. 没读到 / 未覆盖

1. **PS 侧 HP 口仲裁与 DDR 时序**：不在这两个文件内。合并两个 master 后的实际带宽要上板才知道。
2. **实测"读在途 16~32 笔"中的 32**：RTL 的硬上限是 **16**（`fifo_rctl`，`gmem0:997-998` / `gmem0:241`）。若观测到 32，可能来自别的 build 或测量口径（例如把 AR 与 R 分开计），**未覆盖**。
3. **`BRESP` 非 OKAY 时的行为**：RTL 里没有任何处理（`gmem1:1158` 之后再无引用）；软件侧是否会因此静默丢数据，**未覆盖**。
4. **`C_TARGET_ADDR`**：本 build 为 0，非 0 时的行为没验证。
5. **`bus_wide_gen` 两条分支**（`gmem1:647-1059`、`gmem0:506-785`）：本 build 未 elaborated，只读了结构，未逐行核对（合并方案若选错宽度会用到，届时需重读）。
6. **`burst_interleave`**（`gmem1:1501-2011`、`gmem0:1144-1654`）：`NUM_PORTS=1` 未使用，未读。
7. **`MAXI_BUFFER_IMPL="block"` 下 `_mem` 的实际 BRAM 映射**：只看了 csynth 的 BRAM18 计数（`rpt:136-137`），没有读 `_mem` 的位宽拼接细节。
8. **`srl` 实现**（`gmem1:2994-3032`）：`fifo_wreq` 用它，只确认了深度语义，没看移位链细节。

---

## 10. 行号索引（速查）

### gmem1（`conv2d_engine_gmem1_m_axi.v`）

| 行 | 内容 |
|---|---|
| 10-36 | 顶层参数表 |
| 114-116 | `NUM_READ_PORTS=0 / NUM_WRITE_PORTS=1` |
| 157-175 | 读通道全部 tie-off |
| 177-190 | 通道数组 → 单一 AXI 总线映射 |
| 196-236 / 243-307 | store / write 实例化（参数在 199-207 / 244-259）|
| 318-331 | store 参数（`BYTE_ADDR_MODE` 326、`USER_MAXREQS` 330）|
| 373-385 | store 派生参数（`WBUFF_DEPTH` 382、`PREFERRED_IMPL` 383）|
| 456-470 | `fifo_wreq` 4×96b |
| 476-478 | `next_wreq` / `ready_for_wreq` / `valid_length` |
| 480-482 | AW 三个输出 |
| 485-492 | 字→字节（`BYTE_ADDR_MODE=0`）|
| 494-506 | `tmp_len/tmp_addr` |
| 508-518 | `tmp_valid` 单寄存器 |
| 524-614 | 保守模式 beat 计数（`fifo_burst` 546-560）|
| 621-636 | `buff_wdata` 512×72 |
| 640-646 | `bus_equal_gen`（WSTRB 直通 642，WREADY 644）|
| 1064-1078 | `fifo_wrsp` 16×1 |
| 1080-1094 | `user_resp` 4×1 |
| 1097-1099 | `ursp_write` / `wrsp_read` / `out_AXI_BREADY` |
| 1113-1129 | write 模块参数 |
| 1223-1238 | ID 压缩/解压（恒 0）|
| 1271-1281 | AXI 属性硬连（`AWSIZE=3`、`AWBURST=01`、`AWCACHE=C_CACHE_VALUE`）|
| 1284-1309 | converter 实例 |
| 1312-1313 | `out_BURST_AWVALID` / `out_BURST_WREADY` |
| 1318-1356 | throttle 实例 |
| 1363-1402 | B 通道（`fifo_resp` 1375-1393、`resp_ready` 1398、`out_AXI_BVALID` 1401）|
| 1407-1498 | converter 包装（1468 选 sequential）|
| 2013-2046 | `burst_sequential` 参数 |
| 2116-2126 | converter 的 `rs_req` |
| 2139-2140 | 字节数→beat 数 |
| 2142-2143 | `read_req` / `next_req`（1 请求在途）|
| 2167-2177 | `req_handling` |
| 2180-2191 | 4KB 切段 |
| 2262-2264 | ctrl 输出 |
| 2297-2342 | 多 burst 分支（`out_BURST_*` 2315-2323、`read_loop/next_loop` 2325-2330）|
| 2408-2482 | throttle 参数 + `num_outstanding_val`（`NUM_OUTSTANDING` 未被引用）|
| 2511-2521 | throttle AW 的 `rs_req` |
| 2523-2533 | `burst_handling` |
| 2556-2602 | 保守模式（`fifo_req` 2564-2580、`rs_burst` 2582-2592、**AWVALID 2598**）|
| 2606-2620 | W 通道 assign（`local_AXI_WREADY` 2614、`next_burst` 2619、`next_beat` 2620）|
| 2622-2672 | W 数据/WVALID/WLAST/beat 计数 |

### gmem0（`conv2d_engine_gmem0_m_axi.v`）

| 行 | 内容 |
|---|---|
| 10-36 | 顶层参数表 |
| 114-116 | `NUM_READ_PORTS=1 / NUM_WRITE_PORTS=0` |
| 147-170 | 写通道全部 tie-off |
| 189-221 / 228-277 | load / read 实例化（参数 190-200 / 229-242）|
| 285-297 | load 参数（`BYTE_ADDR_MODE` 291、`USER_MAXREQS` 295）|
| 327-336 | 派生（**`RBUFF_DEPTH = 16*256 = 4096`，334**）|
| 394-408 | `fifo_rreq` 7×96 |
| 414-415 | `next_rreq` / `ready_for_rreq` |
| 418-420 | AR 输出 |
| 423-430 | 字→字节 |
| 432-456 | `tmp_valid` |
| 463-478 | **`buff_rdata` 4096×34**，`out_AXI_RREADY` 472 |
| 481-492 | `ready_for_outstanding`（信用归还）|
| 497-505 | `bus_equal_gen` |
| 796-1045 | read 模块 |
| 947-973 | converter 实例 |
| 981-991 | `rs_rdata` |
| 995-1009 | **`fifo_rctl` 16×1** |
| 1011-1025 | `fifo_burst` 16×1 |
| 1029-1041 | `ost_ctrl_write` / `next_ctrl` / `out_AXI_RLAST` / `beat_ready` |
| 1656-2048 | `burst_sequential`（`read_req` 1785、`could_multi_bursts` 1940、`out_BURST_*` 1958-1966）|

### conv2d_engine.v（只列本文用到的）

| 行 | 内容 |
|---|---|
| 316-338 | gmem0/gmem1 的 AXI 参数（`DATA_WIDTH` 318/330、`CACHE` 326/338）|
| 7506-7596 | gmem0_m_axi 实例（参数 7507-7528）|
| 7598-7688 | gmem1_m_axi 实例（**`USER_MAXREQS=4` 7600、`CH0_NUM_WRITE_OUTSTANDING=16` 7615**）|
| 9775-9801 | AWADDR/AWLEN 多路选择 |
| 9803-9813 | `gmem1_0_AWVALID` |
| 9815-9825 | `gmem1_0_BREADY` |
| 9827-9855 | W 数据/STRB/VALID 多路选择 |
| 9857-9871 | `gmem1_blk_n_AW` / `gmem1_blk_n_B` |
| 16035-16079 | `state151→152→153→…→158→159→160→161` |
| 16102-16107 | `state166` 退出条件 |
| 16565 / 16569 | `ap_block_state158` / `ap_block_state166`（**等 `BVALID`**）|

### 其它文件

| 文件:行 | 内容 |
|---|---|
| `..._Pipeline_VITIS_LOOP_945_50.v:369-375` | `WVALID=1`（受 `WREADY` 阻塞）|
| 同:533/535/543/545 | `AWVALID=0` / `BREADY=0` / `WLAST=0` / `WSTRB=255`（前三个是悬空输出）|
| `..._Pipeline_VITIS_LOOP_967_51.v:373/534/536/544/546` | 同上 |
| `conv2d_engine_csynth.rpt:136-137` | gmem0 8 BRAM18 / gmem1 4 BRAM18 |
| `solution1.log:766-777`、`785-796` | 两条写流水线的 `AWVALID/BREADY` 悬空告警 |

---

## 11. 给下一步的三条判断

1. **不要再调 `num_write_outstanding` / `HAS_BURST`**——RTL 里没有能让它生效的并发（§3.3 三条原因，任一条都足够）。
2. **写侧唯一的结构性开关是"AW 与 W 解耦"**（`gmem1:2598`）：让地址 FIFO（已是 16 深）自己往外发 AW，W 描述符单独排队。但先算收益：按 §8，在 5.3 拍/字节的量级上，B 延迟最多值 ~0.06 拍/字节（≈1%），**现在做它没有意义**。只有当内核数据生产快到 ~1 拍/字节量级（每块 ~512 拍）时，每块省下的 ~60 拍才值 ~12%。而且它必须和内核 FSM 一起改（`top:16035-16107` 是 HLS 调度的产物，改不了 RTL，只能改源码），单改适配器等于白改。
3. **如果本就要手写 kernel：直接写自己的 AXI master**，不要继承这套 AW/W 锁步 + 丢弃 `BRESP` 的适配器。省下的逻辑（727+675 FF / 844+662 LUT / 12 BRAM18）足够覆盖一个清爽的 AW 队列 + W 计数器 + B 计数，而且顺手拿到 `BRESP` 和任意 burst 长度。
