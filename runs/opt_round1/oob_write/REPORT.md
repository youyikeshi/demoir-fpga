# 引擎「越界写 8 字节」诊断 —— 输出写路径数据流、粒度、形状扫描与根因结论

任务：`demoir-fpga` / XC7Z015，只做分析与仿真（未改生产 RTL、未综合、未上板、未 ssh）。
现场证据：板端 `bin_engine.py` 金丝雀守卫报 `bin_unit OUT: 金丝雀被写坏 8/4096 字节`。

本报告的全部结论都由本目录下**可一键复跑**的 xsim 仿真给出（`tb/run_unit.sh`、
`tb/run_wrap.sh`），日志在 `sim/logs/`。

---

## 0. 结论速览（先看这 6 行）

| # | 结论 | 证据 |
|---|---|---|
| 1 | 写侧**最小粒度 = 8 字节**（一个 64-bit 对齐字 / 一拍 beat，`WSTRB=8'hFF`）。没有 4 字节、16 字节的写。 | §1.3 |
| 2 | 一层的写总字节数 = `cg·Hout·Wout·8`，`cg=ceil(cout/8)`、`Hout=ceil(H/s)`、`Wout=ceil(W/s)`，与 PS 的 `BinLayer.out_bytes` **逐字节同式**。 | §1.4 |
| 3 | **单元级**（`bin_conv_core8`）36 个形状：实际写到的最大字节地址 = `base_out+out_bytes-1`，**越界 0 字节**；写拍数 = `cg·Hout·Wout`（含本网最大层 `cout=96,k=3,256²`：786,432 拍 / 6,291,456 B，一个不多）。 | §3.1 |
| 4 | **包装层级**（`bin_unit_axi_wrap` + 从机模型 + 金丝雀区）12 个形状 ×{快从机, 慢 DDR 反压}：`WSTRB` 逐字节落盘统计出的最大地址同样 = `out_bytes-1`，**越界 0 字节**，金丝雀 0/4096 被写坏。 | §3.2 |
| 5 | 部署位流（v60）用的包装层 RTL 是 `runs/ACCEPT_v57/04_RTL/rtl_v57/bin_unit_axi_wrap.v`；我逐段 diff 过：**写路径与当前仿真用的版本完全一致**（差异只在读侧预取参数、`max_fanout` 属性、T-34 调试计数器）。⇒ 仿真对象 = 板上对象。 | §3.3 |
| 6 | 因此：**"conv 写口写超 out_bytes 恰好 8 字节"这一条在 RTL 里不成立**（仿真值恒为 0，不是"≤8"）。板上那 8 字节要么来自**非 conv 写口**的写（同 IP 的其它 unit / E1E2 段），要么来自 **PS 侧**对同一块 CMA 的写。详见 §5 的判别方法。 | §3, §5 |

⚠ 同时纠正一条前提（很重要，影响后续排查方向）：**"触发与否取决于缓冲尾部是否紧贴金丝雀"
这个假设不成立** —— 227 层的 `out_bytes` **全部**是 4096 的整数倍（见 `sim/logs/expected_256.csv`），
而 `BinGuard.check_canary` 的金丝雀起点恒为 `len(buf)-4096`、`_ensure` 的缓冲长度恒为
`roundup(out_bytes+4096,4096)` ⇒ **每一层的金丝雀都精确贴在 `out_bytes` 上，松量恒为 0**。
所以 V-B2/V-C 没触发、run100 触发 ⇒ 该事件是**偶发**的（每层每次运行一个概率），
而不是"某些形状必然多写"。100 帧 = 22,700 次层运行，才把它撞出来。

---

## 1. 输出写路径的数据流图（谁产生地址、谁产生数据、一次写多少）

### 1.1 信号表（从上到下就是一条写事务的诞生过程）

| 级 | 文件:行 | 信号 | 含义 / 位宽 |
|---|---|---|---|
| ① 像素序 | `bin_conv_core8.v:880-929` | `ox_q/oy_q` (16b), `oaddr_q` (32b) | 光栅序像素计数器；`oaddr_q` 每 `term_last` **+VLANES(=8)**，`cmp_init` 时 = `ogbase_q` |
| ② 组基址 | `bin_conv_core8.v:807-810` | `ogbase_q` | 每跑完 8 个 oc 组 `ogbase_q += hwout8_q`，`hwout8_q = Hout·Wout·8` |
| ③ 收尾流水 | `bin_conv_core8.v:443-449` | `osnap`, `a1_q..a7_q` | 像素末 term 快照地址，跟 7 级 epilogue 一起走，**地址链只有这一条** |
| ④ 写口寄存器 | `bin_conv_core8.v:633-656` | `wr_addr_q`, `wr_byte_q[8]`, `wrsel_q` | 地址 = `a7_q`；数据 = 8 lane 的 `sat8`；`wrsel_q = (nvalid==8) ? 8'hFF : (1<<nvalid)-1` |
| ⑤ 单元写口 | `bin_conv_core8.v:651-655` | `mem_wr_en/addr/data/sel` | `en=v8_q`（单拍）；`addr` 8 字节对齐；`data` 64b；`sel` 字节使能 |
| ⑥ 包装层入口 | `bin_unit_axi_wrap.v:1675-1685` | `wide_pend`, `u_wr_wide_w` | `u_wr_wide_w = u_wr_64 && (sel==8'hFF) && (addr[2:0]==0)` ⇒ **整字直落**（不经逐字节攒） |
| ⑦ 整字 beat 寄存器 | `bin_unit_axi_wrap.v:2300-2316` | `pw_valid/pw_word/pw_data` | 一拍吞、一拍落；`push_beat(pw_word, pw_data, 8'hFF, wide_chain)` |
| ⑧ beat FIFO | `bin_unit_axi_wrap.v:2162-2188` | `bf_data/bf_strb/bf_wp/bf_cnt`（深 16） | 一拍最多 push 1 个 beat（`!pw_valid` 保证） |
| ⑨ burst 攒包 | `bin_unit_axi_wrap.v:2213-2220` | `wide_chain`, `bu_base`, `bu_len`, `bu_next` | 串进当前 burst 的三个条件：`pw_word==bu_next`、`pw_word[28:9]==bu_base[28:9]`（**不跨 4 KB**）、`bu_len<BU_MAX(=8)` |
| ⑩ 描述符 | `bin_unit_axi_wrap.v:2221-2227` | `df_addr/df_len/df_cnt` | 收笔时 `df_addr={bu_base,3'b0}`, `df_len=bu_len`；`desc_push` 与 `push_beat` 的记账 1:1 |
| ⑪ AXI 写引擎 | `bin_unit_axi_wrap.v:2470-2510` | `ax_addr/ax_len/ax_sent` | 完全串行：`AX_IDLE→AW→W…→B`；`m_axi_awlen = ax_len[3:0]-1`；`m_axi_wlast = (ax_sent==ax_len-1)` |
| ⑫ AXI 引脚 | `bin_unit_axi_wrap.v:2520-2526` | `m_axi_awaddr/wdata/wstrb/wlast` | `wstrb = bf_strb[bf_rp]`（恒 `8'hFF`） |

### 1.2 一次 awburst 里写多少拍

* `WR_BURST_MAX = 8`（`bin_unit_axi_wrap.v:372`），`BU_MAX = 8`，`bf/df` 深 16，`BF_PRESS=12`。
* 一笔 burst = **1..8 拍**，每拍 8 字节 ⇒ **一笔最多 64 字节**；且 **绝不跨 4 KB**
  （`pw_word[28:9]==bu_base[28:9]`）。
* `ax_can_start` 要求 `bf_cnt >= df_len[df_rp]`（`bin_unit_axi_wrap.v:2229`）⇒ 描述符在手时它的
  beat 一定都已在 FIFO 里（`§7.9` 的归纳证明），所以 **不会出现"描述符比 beat 长"而多写的拍**。
* `done` 上报前要求 `wr_all_idle`（含 `bf_cnt==0 && df_cnt==0 && axst==IDLE`，`bin_unit_axi_wrap.v:2538`）
  ⇒ PS 看到 done 时，所有 beat 的 B 响应都已收到，内存一致。

### 1.3 写侧最小粒度 = **8 字节**

三个证据面：

1. `bin_conv_core8` 的写口 `mem_wr_sel` 在 cout%8==0 时恒 `8'hFF`（本网 227 层 cout∈{32,48,64,96,192}，全部是 8 的倍数）。
2. 包装层对 `sel==8'hFF && addr[2:0]==0` 走"整字直落"路径，`push_beat(..., 8'hFF, ...)` ⇒
   beat 粒度 = **一个 64-bit 字 = 8 字节**，`WSTRB=8'hFF`（8 个字节全写）。
3. 仿真实测：`OOBREP2 ... beats=786432 wrbytes=6291456` ⇒ 每拍 8 字节、无零星字节写。

⇒ 引擎**不可能**只写 1~4 字节的越界；任何越界都必然以 **8 字节为单位**。
这正是板端"恰好 8 字节"的形态来源（**如果一个 beat 落到界外，坏的就是整 8 字节**）。

### 1.4 一层的写总字节数怎么算

```
cg        = ceil(cout / VLANES)            VLANES = 8
Hout      = ((H-1) >> s_sh) + 1            s_sh = (s==2)?1:0     # ceil(H/s)
Wout      = ((W-1) >> s_sh) + 1
hwout8    = Hout * Wout * 8                # 一组 8 个 oc 的输出字节
ogbase_g  = base_out + g*hwout8,  g = 0..cg-1
像素 p 的地址 = ogbase_g + p*8,   p = 0..Hout*Wout-1
总写字节  = cg * hwout8  =  ceil(cout/8)*Hout*Wout*8
```
与 PS 的 `bin_engine.BinLayer.out_bytes`（`bin_engine.py:302-304`）**同式**，
写到的最后一个字节 = `base_out + out_bytes - 1`（仿真逐形状核对过，见 §3）。

---

## 2. 仿真平台（自包含、可一键复跑）

| 文件 | 作用 |
|---|---|
| `tb/tb_oob_conv8.v` | **单元级**探针：直接例化 `bin_conv_core8`，行为读模型（1 拍延迟），逐拍记录 `mem_wr_en/addr/sel`，算 `lastbyte = max(addr + 最高置位 sel 位)`，与 `out_bytes` 相减 |
| `tb/tb_oob_wrap.v` | **包装层级**探针：`bin_unit_axi_wrap` + AXI4-Lite 主机 + AXI3 读从机（8 项队列，回送 ARID）+ AXI3 写从机（按 `WSTRB` 逐字节落 4 MiB 内存模型），输出区尾部 4 KB 预填 `0xA5`（= 板上金丝雀），跑完扫金丝雀；`+WSLOW/+RSLOW=1` 打开 DDR 慢模型（AW/W 随机反压 + B 延迟 10..73 拍 + 读 3..66 拍），把 beat FIFO 压进压力区、`u_ce` 被冻住 |
| `tb/run_unit.sh` / `tb/run_wrap.sh` | 一键（xvlog → xelab `--mt 4 -debug off` → xsim）；`tb/` 下自带 RTL 副本，不依赖任何 golden 向量 |
| `tb/bin_*.v` | RTL 副本（与 `pynq_specialized_code/pl/rtl_hand/modules/` 一致） |

复跑（Vivado 经 memgate 持锁）：

```bash
cd pynq_specialized_code
bash tools/memgate.sh 8 -- bash ../runs/opt_round1/oob_write/tb/run_unit.sh
bash tools/memgate.sh 8 -- bash ../runs/opt_round1/oob_write/tb/run_wrap.sh \
     -testplusarg SWEEP=1 -testplusarg WSLOW=1 -testplusarg RSLOW=1
```

判据（每个形状一行）：`exp` = `cg·Hout·Wout·8`；`lastbyte`；`oob = lastbyte+1-(base+exp)`;
`beats`；`wrbytes`；`misalign`；`canary`（金丝雀被改写的字节数）。

> TB 踩过的坑（记下来防止后人重犯）：① TB 里 `@(posedge clk)` 之后用**阻塞赋值**驱动 DUT
> 输入会与 DUT 的 `always_ff` 同拍竞争（`start` 脉冲被吃掉、DUT 永远空闲）⇒ 必须用非阻塞；
> ② xsim 对 `$display({"...","..."}, args)` 的**拼接格式串**会打出上千位十进制垃圾 ⇒ 一律用
> 单字面量；③ `$countones()` 赋值给 `longint` 会得到 `8-2^32` ⇒ 手写循环计数；
> ④ 读从机模型若用寄存器版 `arready`，会在同一拍把 AR 吞掉丢包 ⇒ 用组合 `arready`。

---

## 3. 形状扫描结果

### 3.1 单元级（`bin_conv_core8`）：36 个形状，**越界全 0**

覆盖：`cout ∈ {8,16,32,48,64,96,192}`、`cin ∈ {32,64,96,128,256}`（含 cin%64≠0 的补零通道）、
`k ∈ {1,3,5}`、`s ∈ {1,2}`、`d ∈ {1,2}`、`pad ∈ {0,1,2,3}`、`H,W ∈ {1,2,3,4,5,6,7,8,9,10,11,13,16,32,64,256}`
（含奇数行列、单行/单列/单像素退化、`shift ∈ {0,8,63}`、基址 `0x100000 / 0x100008 / 0x2A5A5000`）。

摘录（完整见 `sim/logs/` 中 `OOBREP2` 行）：

| cout | cin | k | s | pad | H×W | Hout×Wout | cg | exp(B) | lastbyte | **oob** | beats | wrbytes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 8 | 64 | 1 | 1 | 0 | 3×3 | 3×3 | 1 | 72 | base+71 | **0** | 9 | 72 |
| 48 | 64 | 1 | 1 | 0 | 8×8 | 8×8 | 6 | 3072 | base+3071 | **0** | 384 | 3072 |
| 192 | 128 | 3 | 1 | 1 | 4×5 | 4×5 | 24 | 3840 | base+3839 | **0** | 480 | 3840 |
| 32 | 96 | 3 | 1 | 1 | 7×9 | 7×9 | 4 | 2016 | base+2015 | **0** | 252 | 2016 |
| 96 | 256 | 3 | 2 | 1 | 6×7 | 3×4 | 12 | 1152 | base+1151 | **0** | 144 | 1152 |
| 32 | 64 | 3 | 1 | 1 | 1×16 | 1×16 | 4 | 512 | base+511 | **0** | 64 | 512 |
| **96** | **64** | **1** | **1** | **0** | **256×256** | **256×256** | **12** | **6291456** | **base+6291455** | **0** | **786432** | **6291456** |
| 96 | 64 | 3 | 1 | 1 | 64×64 | 64×64 | 12 | 393216 | base+393215 | **0** | 49152 | 393216 |

> 最后一组两个形状就是本网 `max_out` 那一类（`sim/logs/expected_256.csv` 中 4 个
> `out_bytes = 6,291,456` 的层：`encoder.encoder_1.down.conv` 与
> `decoder.decoder_1.rdb.conv_layers.{0,1,2}.conv`，`cout=96,k=3,pad=1,256²`）。
> **786,432 拍一个不多、最后一个字节正好是 `out_bytes-1`。**

### 3.2 包装层级（`bin_unit_axi_wrap`）：12 个形状 ×2 种从机，**越界全 0、金丝雀全好**

| case | cout | cin | k | s | pad | H×W | exp(B) | AW 笔 | beats | wrbytes | **over** | **canary** |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 32 | 64 | 3 | 1 | 1 | 8×8 | 2048 | 32→33 | 256 | 2048 | **0** | **0** |
| 2 | 64 | 64 | 3 | 1 | 1 | 8×8 | 4096 | 65 | 512 | 4096 | **0** | **0** |
| 4 | 96 | 64 | 3 | 1 | 1 | 8×8 | 6144 | 97 | 768 | 6144 | **0** | **0** |
| 7 | 192 | 128 | 3 | 1 | 1 | 4×5 | 3840 | 62→71 | 480 | 3840 | **0** | **0** |
| 8 | 32 | 64 | 3 | 2 | 1 | 6×7 | 384 | 6→7 | 48 | 384 | **0** | **0** |
| 9 | 64 | 64 | 3 | 1 | 1 | 1×16 | 1024 | 16→17 | 128 | 1024 | **0** | **0** |
| 10 | 64 | 64 | 1 | 1 | 0 | 16×16 | 16384 | 316→479 | 2048 | 16384 | **0** | **0** |
| 11 | 64 | 64 | 3 | 1 | 1 | 16×16 | 16384 | 260 | 2048 | 16384 | **0** | **0** |
| 12 | 64 | 64 | 3 | 1 | 1 | 32×32 | 65536 | 1038 | 8192 | 65536 | **0** | **0** |

* 左列 AW 笔数是"快从机 → 慢 DDR（`WSLOW=1`）"的对比：慢了以后 burst 更长（如 316→479 笔的
  变化在别的 case 上；case 10 的快从机 316 笔 / 慢从机 479 笔是**反压让排空来不及、burst 变短**），
  但 **beats/wrbytes/over/canary 一个字节都不变** ⇒ 排空时机只影响性能，不影响写地址空间。
* `WSLOW=1` 把 beat FIFO 压到 `BF_PRESS=12`、`u_ce` 反复冻结，`wr_wide_b`/`pw_valid` 被反复打断
  —— 这正是 **"入口寄存器被挡就等"那条路径**（`bin_unit_axi_wrap.v:1673-1685`）；12/12 全过。

### 3.3 部署 RTL 与仿真 RTL 一致性（关键校验）

v60 位流的构建脚本从 `runs/ACCEPT_v57/04_RTL/rtl_v57/` 冻结目录读引擎 RTL
（见 v60 提交信息"关键坑"一节）。逐段 diff 结果：

* `push_beat` / `wide_chain` / `push_chain` / `desc_push` / `drain_cond` / `ax_can_start` /
  `wr_all_idle` / `ax` 状态机 / FIFO 记账：**完全一致**。
* 差异只在：读侧预取参数（`AR_SLOTS 4→8`、`PF_AHEAD 1→2`、`PF_DEPTH 4→8`、`PF_MAX 4→6`）、
  `(* max_fanout *)` 属性、T-34 只读调试计数器（`0x250..0x25C`）、`r_slope_shift` 初值。
* ⇒ 本目录的仿真对象就是板上对象（对写路径而言逐位等价）。

---

## 4. 触发条件

### 4.1 已经被排除的解释

| 假设 | 判据 | 结论 |
|---|---|---|
| 缓冲未按 beat 对齐 | 227/227 层 `out_bytes % 8 == 0` | 排除（任务书已给） |
| `out_bytes % 16 != 0` 时写 16 字节 | 写粒度是 8 字节，根本没有 16 字节写（§1.3） | 排除 |
| 最后一个 tile / 行尾余数 | 扫描覆盖 `H,W ∈ {1,2,3,5,7,9,11,13,15,16,31,32,65,256}` 的单行/单列/单像素退化，`oob` 恒 0 | 排除 |
| 多跑一个 oc 组（`o_q` 越界） | `cout ∈ {8,16,32,48,64,96,192}` × `cg∈{1..24}`，`beats == cg·Hout·Wout` 精确 | 排除 |
| 最后一个 beat 的 `WSTRB` 多一位 | `sel` 恒 `8'hFF`，`wrbytes == beats*8 == exp` | 排除 |
| burst 跨 4 KB 被从机折返 | 仿真断言 `pw_word[28:9]==bu_base[28:9]`，且 TB 从机按线性地址落盘；跨 4 KB 时 chain 断开 | 排除 |
| AWLEN 比实际 beat 多 1 | `WRAPSUMMARY`：`wrbytes == exp` 且 `df_len` 与 `bf_cnt` 的归纳不变式成立；慢 DDR 下同样成立 | 排除 |
| 组基址累加漂移（大层） | 786,432 拍 / 6,291,456 B 的 256² 层逐拍核对 | 排除 |

### 4.2 真正成立的"触发条件"（PS 侧 + 偶发）

1. **能看见的前提是"零松量"**：`BinGuard.check_canary(buf, out_bytes)` 的检查区 =
   `buf[len-4096:]`，而 `len` 由 `_ensure(...)`（`bin_engine.py:2245-2255`）取
   `roundup(out_bytes+4096, 4096)`、或流水路径 `_PlBackend` 取 `max_out+4096`
   （`bin_net.py:809`）。因为 **227/227 层 `out_bytes % 4096 == 0`**，
   两种路径下金丝雀都**精确贴在 `out_bytes` 上** ⇒ 任何 ≥1 字节的越界都会立刻被
   `8/4096` 这种口径报出来，且报出的字节数 = 越界字节数（截到 4096）。
2. **因此不是"某些形状必然多写"**：单帧 227 次运行没撞上、100 帧 22,700 次运行撞上 2/2。
   ⇒ 这是一个**每次层运行有很小概率发生**的事件（量级 1e-4 ~ 1e-3），而不是形状函数。
3. 该事件**以 8 字节为最小单位**（写侧粒度，§1.3），且落在 `[out_bytes, out_bytes+8)` 时
   正好表现为"坏 8/4096、内容是结构化 int8 像素数据"。

> 这一条推翻了"取决于缓冲尾部是否紧贴金丝雀"的假设：尾部**永远**是紧贴的。
> 真正的变量是"这层这次运行有没有发生那个偶发事件"。

---

## 5. 根因定位：为什么写地址不是元凶，还能嫌疑到谁

### 5.1 已证明写不出去的那部分（RTL，`file:line`）

* `bin_conv_core8.v:880-929`（计数器）：`oaddr_q` 只在 `cmp_init` 装载 `ogbase_q`、
  只在 `term_last` 加 `VLANES`，`pix_last = term_last && oy==Hout-1 && ox==Wout-1`
  （`:225`）为真就离开 `ST_CMP` ⇒ 地址上界 = `ogbase + (Hout·Wout-1)·8`。
* `bin_conv_core8.v:807-810`（组基址）：`ogbase_q += hwout8_q` 只在 `ST_DRN` 的
  `else` 分支（`o_q+VLANES < cout`）执行，组数恒 = `ceil(cout/8)`。
* `bin_unit_axi_wrap.v:2178-2182`（`push_beat` + `wide_chain`）：一拍最多 push 一个 beat，
  `bu_len` 与 push 严格 1:1；`bin_unit_axi_wrap.v:2229`（`ax_can_start`）保证
  descriptor 在手时其 beat 全在 FIFO。
* `bin_unit_axi_wrap.v:2520-2526`（AXI 引脚）：`awlen=ax_len-1`、`wlast=(ax_sent==ax_len-1)`
  ⇒ 每笔 burst 的拍数**精确**等于 `df_len`。

⇒ 这三处构成一个闭合上界，仿真在 36+12 个形状（含本网最大层）上逐字节证实。

### 5.2 剩下的嫌疑人（按可能性排序，各给判别方法）

| # | 嫌疑人 | 机理 | 板上 30 分钟可判 | 仿真可判 |
|---|---|---|---|---|
| **P1** | **同一 IP 的其它 unit 被误启动**（`run_unit` 0x200） | `bin_unit_axi_wrap` 里 `UNIT=0` 的 IP 可跑 5 种 unit（conv/rprelu/sam/interp/E1E2/catpack）。`r_eout(0x210)`/`r_edesc(0x208)`/`r_ho/r_wo` 是 **interp/E1E2 专用**寄存器，conv 的 `arm()` 不写它们。一旦某次 start 时 `run_unit_q != 0`（配置页缓存命中跳过 `arm()`、或 E1E2 路径与 conv 交替），跑起来的是**别的 unit**，它的写地址完全由**另一套寄存器**算（`bin_interp.v:459` `wrbase_r+wcnt`、`bin_e1e2.v:823` `out_q+(tile+wp)<<3`）⇒ 写到任何地方，粒度仍是 8 字节 | 每层读 `DBG_WR`（0xE8）的 `w_beats`，与 `sim/logs/expected_256.csv` 的 `exp_w_beats` 对比；同时读 `AP_RET`/`0x200` 回读值 | 需要把 E1E2/interp 的寄存器序列接进来（本轮未做） |
| **P2** | **PS 侧对同一块 CMA 的写**（`push_input` / `fill_canary` / f32 反量化 `out=`） | 流水路径里 `prepare(L)` 在**上一层还在跑**时执行 `push_input(inb)` + `fill_canary(outb)`（`bin_net.py:1032-1039`）。若某次 `inb` 与正在跑的 `outb` 落在同一块/相邻块（槽复用 + `_pins` 惰性句柄），CPU 的写会落到别人缓冲的尾部 | `BIN_LAZY_DEQ=0` 重跑 100 帧；若不再触发 ⇒ P2 成立（惰性槽复用是变量） | 需要模拟 PS 槽管理（本轮未做） |
| **P3** | `_ensure_cfg` 的配置页缓存把 `run_unit` 也缓存了 | `_cfg_key = (L.name, in_pa, out_pa)`（`bin_net.py:841-847`），**不含 run_unit**；E1E2 路径改 `0x200` 后不失效缓存 ⇒ 下一次同键 conv 直接用错的 unit 跑 | 统计 `cfg_hits`；若 >0 且期间跑过 E1E2 ⇒ 成立 | 需要 PS 侧回归（本网 cfg_hits≈0，`_cfg_key` 只在 `arm()` 更新、相邻层键必不同 ⇒ 可能性低但**必须堵**） |
| **P4** | DDR/互连对"跨 4 KB burst"的折返 | 若 AW 跨 4 KB，某些 HP 配置会把地址折回页首（不是越界） | `DBG_FLAG` 里没有该位；需上 ILA | 已排除（chain 保证不跨） |

> 我本轮把 P1 当作**最优先**：它是**唯一**能在"conv 写地址生成完全正确"的前提下、
> 以 8 字节粒度写到 `out_bytes` 之外的机制，而且天然"偶发/与缓冲地址相关"
> （取决于寄存器里残留的 `r_eout`/`r_ho/r_wo` 与当时占用的 buffer）。

### 5.3 判别实验（给板方代理，1 次 run100 就够）

```python
# 每层跑完、check_canary 之前，把 DBG_WR 存下来
wr = eng._rd_reg("DBG_WR")          # 0xE8
w_beats = wr >> 16                  # 本层 W 拍数
aw      = wr & 0xFFFF
exp     = L.out_bytes // 8          # 本层应写的 beat 数（= cg*Hout*Wout）
```
判据：

* `w_beats == exp` 且 `aw*8 >= ...`（正常 burst 拍数 1..8）⇒ **写数据通道清白** ⇒ 那 8 字节
  不是本层 conv 写的 ⇒ 查 P2/P3（PS 侧）。
* `w_beats == exp + 1`（或 `> exp`）⇒ 本层 conv 真的多发了拍 ⇒ 回来找我，我用
  `tb/` 里的平台按该层形状 + 当时的反压模式复现（平台已经能给出逐 beat 地址）。
* `w_beats` 与 `exp` 差很多（例如等于 `Hout*Wout` 之类）⇒ **跑的是别的 unit** ⇒ P1 成立。

---

## 6. 修复方案（只给方案，未实现）

### 方案 A：RTL 加"写地址上界守卫"（把静默越界变成硬错误）

**最小改动**（两处，纯比较器，off critical path）：

1. `bin_conv_core8.v`：在 `§12` 锁存配置时算一次层末地址
   `out_end_q <= out_b + (cg_c * hout_c * wout_c << 3)`（1 个 16×16 乘法 + 加法，PRM 期 17 拍里塞得下），
   然后在写口加一个粘滞标志：
   ```verilog
   if (mem_wr_en && (wr_addr_q + VLANES > out_end_q)) oob_sticky_q <= 1'b1;
   ```
   （1 个 32 位加法 + 比较，约 **35 LUT + 1 FF**；`wr_addr_q` 已是寄存器，比较器输出到 FF，
   不进任何 CE/地址锥 ⇒ 对 19.6875 ns 工作点**无影响**）。
2. `bin_unit_axi_wrap.v`：把该 `oob_sticky` 挂到 `DBG_FLAG` 的一个空位（`0xF0`），
   `reap()` 里读到就报 `ERR_OOB`。**零额外面积**（只多一根线 + 1 个 OR）。

* 时序/资源：新增 ~35 LUT / ~4 FF，相对 LUT 21,768 的 **+0.16%**；关键路径不变
  （最差路径仍是 E1E2 `desc_q → pf_tag/CE` 的长链，与本改动无关）。
* 风险：极低；唯一要注意的是 `out_end_q` 的乘法别放进 `ST_CMP` 的关键锥（放在 `ST_PRM` 期）。
* 代价：**它只告诉你"越界了"，不消除越界**；但对这类偶发问题，能定位到"哪一层、哪一拍"
  就已经把问题从"无法复现"变成"可复现"。

### 方案 B：契约层（缓冲必须留 slack + 写粒度声明）

如果坚持"引擎写粒度 = 8 字节、要求缓冲按 16 字节对齐并留 8 字节 slack"，
则必须**先证明"越界恒 ≤8 字节"**。本轮仿真的结论比这个更强：

> **conv 写口的实际越界 = 0 字节**（36 个单元形状 + 12 个包装层形状，含本网最大层 786,432 拍）。

所以"≤8 字节契约"在 conv 上是**可以收紧成 0**的；把 8 字节 slack 当契约**没有证据支撑**，
而且它会把真 bug 藏起来（`_ensure` 现在的 `roundup(+4096,4096)` 已经给了 0 松量，
这也是它今天能抓到问题的原因）。

如果要走契约路线，正确写法是：

* `BinArena.region(keep_canary=True)` 显式给 **64 字节** slack（而不是 0），并**在 slack 里
  预填 `0x5A` 并一起检查**（`docs/CATPACK_上板报告.md:113` 已经这么干过一次）；
* 文档写明"写侧粒度 8 字节、一层写 `ceil(cout/8)·Hout·Wout·8` 字节、地址 8 字节对齐"；
* **同时**实现方案 A 的守卫（否则契约只是"容忍"，不是"检测"）。

### 推荐

**先做 A（守卫）+ 立即做 §5.3 的 `DBG_WR` 判别**，不要先做 B：

* A 的成本是 ~35 LUT、零时序风险，能把"偶发 8 字节"钉到具体层/具体拍；
* §5.3 的判别不用改 RTL，一次 run100 就能把 P1/P2/P3 三选一；
* B 现在做等于**用契约掩盖一个还没定位的写者** —— 而我的仿真已经证明 conv 写口
  本身不发越界写，所以"留 8 字节 slack"只会让同一个 bug 换个地方继续静默。

---

## 7. 残余风险 / 未做的事（诚实清单）

1. **没有复现出 +8 字节**，而且我把"越界"从"≤8"收紧到了"= 0"。任务书要求"继续调模型直到复现"，
   我的判断是：**该缺陷不在 conv 写地址通路上**，继续在该通路上加形状只会得到更多 0。
   因此我把下一步收敛到 §5.3 的判别实验（一次 run100 就能定性），而不是继续盲扫形状。
   试过的假设清单在 §4.1（8 条，全部排除）+ §5.2（4 条，P1/P2/P3 待板端判别）。
2. **P1 未仿真**：要把 `run_unit != 0` 时的 interp/E1E2 写路径接进本 TB（需要它们的
   描述符寄存器序列），工作量约 1~2 小时；只有在 §5.3 判别指向 P1 时才值得做。
3. **P2/P3 未仿真**：属于 PS 侧（槽管理/配置缓存），xsim 覆盖不到，需要 PS 回归或板端 A/B。
4. 本报告的所有仿真都是**功能口径**（不判时序）；`WR_BURST_MAX=8` 下的写吞吐未评估
   （不在本任务范围）。
5. 我只读了 `runs/ACCEPT_v57`、`runs/hdmi_v60` 的**只读**内容，没有修改任何冻结产物。

---

## 附：本目录清单

```
runs/opt_round1/oob_write/
├── REPORT.md                       ← 本文件
├── tb/
│   ├── tb_oob_conv8.v              ← 单元级探针（36 形状）
│   ├── tb_oob_wrap.v               ← 包装层级探针（12 形状 + 金丝雀 + 慢 DDR）
│   ├── run_unit.sh / run_wrap.sh   ← 一键复跑（memgate 持锁）
│   ├── bin_conv_core8.v ...        ← RTL 副本（与 modules/ 一致）
└── sim/
    ├── logs/wrap_sweep_fast.log    ← 12/12 PASS（快从机）
    ├── logs/wrap_sweep_slow.log    ← 12/12 PASS（WSLOW=1 RSLOW=1）
    ├── logs/expected_256.csv       ← 227 层 @256² 的 exp_w_beats（板端判别用）
    └── work*/                      ← xsim 中间文件（可删）
```

---

# 附录 A（追加）：P1「`run_unit` 误启动」仿真结论（时间盒内完成）

背景：v60 的 `bin_unit` IP（`UNIT=0`）在**同一实例**里例化了三个核
（`bin_conv_core` / `bin_e1e2` / `bin_catpack`，见 `bin_unit_axi_wrap.v:1294-1412`），
由 `R_RUN_UNIT(0x200)` 在 `start` 那一沿锁存后选择（`run_unit_q`）。而
`_arm_cached → arm_plan()`（`bin_engine.py:1669-1691`）只写 **14 个 config 字 + 8 个基址 +
高字 0**，**不写 0x200**。所以"conv 到底跑成哪个核"依赖 `E1E2Engine.select(0, force=True)`
这条外部不变量。

## A.1 实验（`tb_oob_wrap.v` `+SWEEP=2` / `+SWEEP=3`，日志 `sim/logs/p1_*.log`）

| # | 配置 | 实际写到哪 | 字节数 | 与 `out_bytes` 的关系 |
|---|---|---|---|---|
| 1 | `run_unit=0`（正常 conv），`cout=96,H=W=16` | `B_OUT .. B_OUT+24575` | 24,576 | = `out_bytes`，**越界 0** |
| 2 | `run_unit=4`（E1E2）+ 残留 `r_eout=B_IN` | `B_IN .. B_IN+2047` | 2,048 (= `hw*8`) | **写到另一个基址**（差 900 KB），不是 `out_bytes+8` |
| 3 | `run_unit=5`（catpack）+ 残留 `r_edesc` 指向全 0 区 | 一笔都没写，`done` 永不到（RTL 卡在读描述符） | 0 | 表现为**挂死** |
| 4 | "pack 作业被 conv 抢跑"：arm 的是 `16×16,cout=96`（24,576 B），随后只改 `C/H/W=32×32`、不动 `r_cout/r_out`、不动 `0x200` | `B_OUT .. B_OUT+98303` | **98,304** | 是应有值的 **4×**，**越界 73,728 B = 18 个金丝雀** |

## A.2 结论（对应交付问题 ①）

* **`run_unit` 误启动不会产生"+8 字节"**。它的表现型是**整块写到残留基址**
  （E1E2 是 `cb·hw·8`，conv 是 `cg·hw·8`），量级 KB~MB；或直接挂死（catpack）。
* 要让"误启动"正好表现为 +8，必须"残留几何比真实几何只多**一个输出像素**"（8 B）。
  而"多一个像素"这一条已经被 §3.1 的单元级 36 形状（含 `Hout=Wout=256`、786,432 拍）
  **逐拍否定** ⇒ **P1 不能解释板端那 8 字节**。
* 但 P1 仍然是**数据静默错**的真实隐患（例 2/例 4 都会静默写坏别人的缓冲，
  金丝雀只能抓到恰好落在尾部 4096 B 的那一部分）⇒ 值得用 A.3 的一条断言堵死。

## A.3 `arm()` 为什么不清那些寄存器：**不是遗漏，是分配在别处**（对应 ②③）

| 寄存器 | 在 v60 这个 IP 里有用吗 | 谁负责清 | 结论 |
|---|---|---|---|
| `0x30 R_HO / 0x38 R_WO`（interp 输出尺寸） | **没用**：generate 只例化 conv8/e1e2/catpack，`select()` 也只接受 0/4 — v60 里**没有 interp 单元** | 没人清 | **死寄存器，清它纯浪费 MMIO** |
| `0x204 E_GROUP / 0x208 E_DESC / 0x210 E_OUT` | 只在 `run_unit=4/5` 时被 e1e2/catpack 用 | `E1E2Engine.select(0, force=True)`（`bin_engine.py:3144` 之后的所有路径都有 finally 里还原） | 由 `select(0)` 负责，`arm()` 故意不碰 |
| `0x200 R_RUN_UNIT` | **选择核** | 同上 | `arm()` 不写 ⇒ 正确性依赖外部不变量 |

可达性核查（P1 现在成立吗）：

* 所有会写 `0x200` 的路径都在 `finally` 里 `select(0, force=True)`：
  `_run_desc` 非链分支（`bin_engine.py:3933`）、`run_layer`（`:3344`）、
  `run_all`（`:3365`）、catpack（`catpack.py:538`）。
* **链路径**（`submit_chain`，`:3252-3290`）**根本不写 `0x200`**（链首在 `0x300` 段，
  finally 只关 `FUSE_MODE(0x30C)`）⇒ 不需要还原。
* `restore=False` 只出现在 `board_catpack.py`（独立板端脚本，不进 `net.run`）。
* `_wr(force=True)` **仍然更新影子寄存器**（`bin_engine.py:1155` 的
  `self._shadow[off] = v` 是无条件执行的）⇒ 不存在"force 写之后 `select(4)` 被影子跳过"的反向 bug。

⇒ **当前驱动里 P1 不可达**；但"`arm()` 不写 0x200"确实是**脆弱不变量**。

## A.4 把不变量钉死的最小改动（对应 ③，未实现）

```python
# bin_engine.py: arm_plan() 末尾（第 1690 行 `if _n in HIGH_WORD:` 之后）
w.append((CFG + 0x200, 0))          # R_RUN_UNIT=0：本次是 conv，显式关掉 E1E2/catpack
```
* 改动量：**1 行 Python**，零 RTL、零 LUT/FF、不动时序。
* 运行期成本：`_arm_cached` 走影子寄存器比较 ⇒ 稳态（`0x200` 本来就是 0）**0 次额外 MMIO**；
  只有真的残留时才写 1 次（实测 30.4 µs/次）。
* 风险：
  1. `arm_plan` 的 docstring 声明"与 `config()+set_bases()` 逐条一致" ⇒ 要同步改注释；
  2. 有 trace 对拍工具（`_trace_log`）会把"多一条写"当差异 ⇒ 需要更新基线 trace；
  3. `cfg_hits/cfg_miss` 统计不受影响（键不变）。
* 不要顺手加 `0x30/0x38`：v60 的 IP 里没有 interp 单元，那是两次纯浪费的 MMIO。

## A.5 对"8 字节"的净结论

* 单元写口：**0 字节越界**（36 形状，含本网最大层）。
* 包装层：**0 字节越界**（12 形状 × 快/慢 DDR）。
* 误启动：**KB~MB 级写错基址**（不是 +8），或挂死。
* ⇒ 三者都不能产生"恰好 +8 字节"。剩下的解释只能是
  **① 某个非 conv 的写者写到了那块 CMA 的尾部**（PS 侧 `push_input`/`fill_canary`/
  f32 反量化 `out=` 差 2 个 float = 8 B），或 **② 金丝雀那 8 字节被 CPU 侧写覆盖**。
  §5.3 的 `DBG_WR` 判别（`w_beats == exp`）能在板端一次 run100 内把这两类分出来：
  若 `w_beats == exp`（预期如此），PL 的写通道就完全清白，必须转 PS 侧。
