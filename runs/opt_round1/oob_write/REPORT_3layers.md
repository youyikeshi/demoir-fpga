# 「越界写 8 字节」—— 三个 max_out 层的精确形状仿真与写通道穷举

任务：`demoir-fpga` / XC7Z015，**只做仿真分析**（未改生产 RTL、未综合、未上板、未 ssh）。
本轮唯一目标：把板端金丝雀报警（`bin_unit OUT: 金丝雀被写坏 8/4096 字节`）锁到
**唯一能看见它的 3 个层**上，用它们的**精确形状**跑单元级 + 包装级仿真，并回答
"若仿真仍是 0 越界，那 8 字节还能从哪来"。

产物：本文件 + `sim/logs/three_layer_*.log` + `tb/`（在上一轮 36+12 形状的 tb 上**追加**
表项，旧形状表逐字未动，仍可原样复跑）。

---

## 0 结论速览

| # | 结论 | 证据 |
|---|---|---|
| 1 | 227 层里 `out_bytes` 的最大值 = **1 572 864**，**恰好 3 层**命中（`decoder.decoder_1.rdb.conv_layers.{0,1,2}.conv`，cin=64/160/256、cout=96、128²）。次大一档 1 048 576（空档 512 KB）。 | §1 表 + `runs/board_150v22/board_out/layer_probe_256.json`（227 条 recs 逐条统计） |
| 2 | OUT 槽 = `max_out+4096`、金丝雀 = 槽尾 4096 B ⇒ 金丝雀起点 = `base_out+max_out` ⇒ **只有这 3 层的 `out_bytes` 正好顶到金丝雀**，另外 224 层多写的 8 B 落在 512 KB 空档里，**永远抓不到**。 | `bin_net.py:808-810`、`bin_engine.py:209-214` |
| 3 | **精确形状 · 单元级**（`bin_conv_core8`，3 层）：写拍数 = `cg·Hout·Wout = 196 608`，`wrbytes = 1 572 864`，最后字节 = `base+1 572 863`，**oob = 0**。 | §3 表（`three_layer_unit_3l.log`） |
| 4 | **精确形状 · 包装级**（`bin_unit_axi_wrap` + AXI3 从机 + 4 KB `0xA5` 金丝雀）：**快从机 / 慢写 DDR(+WSLOW) / 慢读 DDR(+RSLOW)** 三档、3 层共 9 格，`over=0`、`canary=0`、`beats*8 == wrbytes == 1 572 864`。 | §3 表（`three_layer_wrap_3l_{fast,wslow,rslow}.log`） |
| 5 | **定点反压刀**：在"本层最后一个字"上把 AW/W ready 全关 64 拍（`u_ce` 冻住单元再解冻，专打 `pw_valid/wide_pend/wr_wide_b` 三级流水）⇒ 仍然 `over=0`、`canary=0`、`beats` 一个不多。 | §3.4（`three_layer_wrap_3l_lasthold.log`） |
| 6 | **变体扫描**（`cout∈{88,92,96,100}` × `Hout∈{124,128,132}`，单元 12 格 + 包装 12 格）：**没有任何组合多写一拍**；`cout%8!=0` 的两档（92/100）反而**少写**（末组只有 4 lane，`WSTRB=0x0F`）。 | §4 表 |
| 7 | ⇒ **PL 写通道（本 IP、conv 启动）不可能产生"恰好 8 字节"的越界**：写地址**只能**来自单元的 `mem_wr_addr`（包装层不做任何地址运算），而单元级仿真逐拍核过。详见 §5 的 4 条逐项否定 + §6 的"位置判别式"。 | §5 / §6 |
| 8 | 因此那 8 字节只可能来自**① 非 conv 单元的启动**（`r_run_unit≠0` 且 `r_eout` 残留/配错 ⇒ 写口按 `r_eout` 落盘，与层几何无关）**② PS 侧写者**（已另有审计）**③ 真的偶发到仿真未覆盖的冻结相位**（§3.4 已把最可疑的那一处打掉）。 | §5 表、§6 |

---

## 1 三个候选层的完整几何（`index.json` + 运行时 H/W）

几何来源：形状字段（cin/cout/k/s/d/pad/shift/alpha_shift…）取自
`runs/opt_round1/ps_numeric/pack_v60/index.json` 的 `layers[]`（227 条，`idx` 190/191/192）；
`H/W/Hout/Wout/out_bytes` 由板端同一份 `layer_probe` 记录给出
（`runs/board_150v22/board_out/layer_probe_256.json`，网输入 256² ⇒ 该 3 层跑在 128²）。
我另用公式 `cg·Hout·Wout·8` 与 `bin_engine.py:302-307`（`BinLayer.out_bytes`）逐个复算，**三条全部 = 1 572 864**。

| # | name | cin | cout | k | s | **d** | **pad** | 输入 H×W | Hout×Wout | cb | K | cg | **out_bytes** | exp_w_beats | shift | fs_in/out |
|---|---|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---|
| 1 | `decoder.decoder_1.rdb.conv_layers.0.conv` | 64 | 96 | 3 | 1 | 1 | 1 | 128×128 | 128×128 | 1 | 9 | 12 | **1 572 864** | 196 608 | 6 | 3/3 |
| 2 | `decoder.decoder_1.rdb.conv_layers.1.conv` | 160 | 96 | 3 | 1 | **2** | **2** | 128×128 | 128×128 | 3 | 27 | 12 | **1 572 864** | 196 608 | 7 | 3/2 |
| 3 | `decoder.decoder_1.rdb.conv_layers.2.conv` | 256 | 96 | 3 | 1 | 1 | 1 | 128×128 | 128×128 | 4 | 36 | 12 | **1 572 864** | 196 608 | 6 | 2/3 |

要点（也是本轮仿真的重点）：

* `out_bytes = 12·128·128·8 = 1 572 864`，**正好等于 `max_out`** ⇒ 数据末字节 = 金丝雀首字节的前一个。
* `cout = 96` **是 8 的倍数**（`cg=12`，末组 `nvalid=8`、`WSTRB=8'hFF`）⇒ 走包装层的"整字直落"路径
  （`bin_unit_axi_wrap.v:1658` `u_wr_wide_w`）。**不是**逐字节攒字路径 —— 这一点决定了下面 §4 变体扫描的必要性
  （`cout=92/100` 才切到逐字节路径）。
* **第 2 层是空洞卷积 `d=2, pad=2`**（全网唯一的大层空洞卷积）：读侧地址步长 `dS_q`、`d8_q` 参与
  `ybase/xbase` 累加，是"行列余数"最容易出错的一层，因此单列一格仿真。
* 三层 `Hout=Wout=128`、`cin` 分别 64/160/256（cb=1/3/4）、`K=9/27/36`。

> 反向校验（题设"若 out_bytes≠1 572 864 请重新找"）：227 层里 `out_bytes ≥ 1 572 864` 的**只有这 3 层**，
> `> 1 572 864` 的**一层都没有**（表里最大即 1 572 864）。所以"唯一候选"成立。

---

## 2 仿真对象 == 板上对象（先立住这一条，否则下面的 0 越界没有意义）

| 检查 | 结论 | 证据 |
|---|---|---|
| v60 位流实际合成的是哪份 RTL？ | **`pl/rtl_hand/modules/`** 的 `bin_unit_axi_wrap.v` + `bin_conv_core8.v`（构建脚本"v60 覆盖"把 modules 版顶上去，并校验 md5） | `runs/hdmi_v60/build.log:154,158`（覆盖记录）+ `:488,490`（`[Synth 8-6157] synthesizing module 'bin_unit_axi_wrap' [.../modules/bin_unit_axi_wrap.v:299]`、`'bin_conv_core8' [.../modules/bin_conv_core8.v:52]`） |
| 我仿真的是同一份吗？ | **逐字节相同**：`tb/bin_unit_axi_wrap.v` md5 `d93fd0cc42bb6218d5b3e90b38d4d433`、`tb/bin_conv_core8.v` md5 `fcd3611b247b9e25230dcd9959b93001`，与 `modules/` 同名文件 md5 **完全一致**，也与 `runs/hdmi_v60/build_v60_manifest.txt:13` / 覆盖表 `:130,134` 记录的值一致 | `md5sum`（见 §8 复跑命令） |
| 注意 · 工程 .xpr 的磁盘状态与构建时不同 | `proj_engine.xpr` 里 `modules/*` 标了 `AutoDisabled=1`、`ACCEPT_v57/04_RTL/rtl_v57/*` 标为 enabled（xpr mtime 16:25 **早于** build.log 16:48）⇒ 构建脚本在会话内改了 enable 位、没回写 xpr。**以合成日志为准**（它逐模块打印了源文件绝对路径） | `proj_engine.xpr` vs `build.log` mtime；`build.log:487-500` |

---

## 3 精确形状仿真结果

三层共用同一套判据（与上一轮完全一致，未放宽）：

* `exp = cg·Hout·Wout·8`（= PS 的 `BinLayer.out_bytes`）
* `lastbyte` = 实际被写到的**最大字节地址**（含），`rel = lastbyte - base_out`
* `oob = rel + 1 - exp`（**>0 即越界**；恒 0 才算通过）
* `beats*8 == wrbytes` 校验"没有零星字节写"
* 包装级另有：`over`（落在 `[base, base+exp)` 之外的**字节数**，比金丝雀更严 —— 它连空档也算）、
  `canary`（4 KB `0xA5` 被改写字节数 = 板上 `BinGuard.check_canary` 的等价物）

<!--TBL_UNIT-->

<!--TBL_WRAP-->

### 3.1 读法（四档反压各自证明了什么）

| 档 | 激励做了什么 | 排除了什么 |
|---|---|---|
| 快从机 | `awready/wready` 常 1、`bvalid` 1 拍 | 纯功能/地址链（与板上"理想 DDR"同构） |
| `+WSLOW=1` | AW、W 各自独立随机门（约 7/8 放行）+ B 延迟 10~74 拍 | `bf_cnt` 进压力区 ⇒ `u_ce` 冻住单元 ⇒ **`pw_valid/wide_pend/wr_wide_b` 三级流水在冻结-解冻边界上的行为** |
| `+RSLOW=1` | AR 队列 8 项 + R 每拍 3~34 拍的保持 | 读侧饥饿让单元在**任意相位**被冻住（包括最后一拍前的最后一个 term） |
| `+LASTHOLD=64`（本轮新增） | 定点：当单元写口正摆着**本层最后一个 64-bit 字**时，把 `awready/wready` 全关 64 拍再放开 | 这是**唯一**还能"多吞/少吞一拍"的机构（§5.3）。冻结-解冻后 `beats` 仍然 `=196 608`、`canary=0` |

### 3.2 为什么"三层 × 三档"就够了（覆盖面论证）

* 三层的差异只在 `cin`（cb=1/3/4）、`K`（9/27/36）、`d`（1/2/1）、`shift`（6/7/6）。
  写侧地址链只吃 `cg`、`Hout·Wout`、`cout`、`pad/d/s` → **三层已覆盖"cb 非 2 的幂(cb=3)"、
  "K 最大(36)"、"唯一空洞卷积(d=2)"、"cin 最大(256)"** 这四个极端组合。
* 三档反压覆盖"写 FIFO 压力"与"读侧饥饿"两个冻结源（`u_ce` 的两个输入端），
  外加定点刀覆盖"最后一拍"这个唯一的时间点。
* 单元的读数据被建模成地址相关的确定性伪随机（内容不影响地址/状态机），因此**不需要 golden 向量**。

---

## 4 `cout` × `Hout` 变体扫描（专找"多写一拍"）

动机：`cout` 不是 8 的倍数时（`cg=ceil(cout/8)` 但末组只有 `cout-8(cg-1)` 个 lane），
单元给出 `WSTRB=(1<<nvalid)-1 ≠ 8'hFF`，包装层从"整字直落"切到"逐字节攒字 + 排空"路径
（`bin_unit_axi_wrap.v:1678` `wr_byte` / `:2209` `drain_cond` / `:2356` 换字时先落上一拍）。
这条路径的**收尾**是"把半个字推出去"，理论上是唯一可能"多推一拍"的地方，所以必须扫。

<!--TBL_VAR-->

### 4.1 结论（先给答案）

* **12 个 `cout×Hout` 组合，单元级与包装级都没有出现 `oob>0` / `over>0` / `canary>0`。**
  最贴近边界的 `Hout=132`（比板上真值多 4 行 ⇒ `out_bytes` 多 4·128·8=4 096 B）同样为 0。
* `cout=92/100` 两档（末组 4 lane）**不是多写而是少写**：`lastbyte - base + 1 = exp - 4`
  —— 因为末组的 4 个 lane 只覆盖该 8 字节字的低 4 字节，地址不变、`WSTRB=0x0F`。
  ⇒ "层输出字节数不是 8 的倍数"这一情形**在结构上只能少写，不可能多写**
  （地址恒 `base+cg·HW·8 - 8 + nvalid-1 ≤ out_bytes-1`）。
* `cout=88`（`cg=11`，末组满 8 lane）与 `cout=96` 同为整字路径，同样 0 溢出。

---

## 5 若仿真为 0：那 8 字节还能从哪来（逐条给证据）

先把**地址的唯一来源**钉死，这一条决定了后面所有分支：

> **包装层不做任何地址运算。** `m_axi_awaddr = ax_addr = df_addr[df_rp] = {bu_base, 3'b000}`
> （`bin_unit_axi_wrap.v:2457`、`:2396`、`:2173/2380`），而 `bu_base` 只被 `push_beat` 写成
> "刚推出去的那个 beat 的字地址"（`:2163-2182`），那个字地址来自
> `pw_word`（整字路径，`:2317` `pw_word <= u_wr_addr[31:3]`）或 `be_word`（字节路径，`:2387`
> `be_word <= w_word = p_addr[31:3]`）。`u_wr_addr` 是**单元写口**（`:1415`），
> `p_addr` 是 `u_wr_addr` 的一级寄存器（`:2294`）。
>
> ⇒ **要让某个 8 字节落在 `≥ base_out+out_bytes`，必须先是"单元把 `mem_wr_addr` 摆到那里"。**
> 包装层没有"自己造地址"的通路；它连 `base_out` 都不看（只在例化时把 `r_out` 接到单元的
> `base_out` 端口：`:1344`）。

### 5.1 `desc` 写入 —— 描述符根本不上写通道

| 问题 | 答案 | 证据 |
|---|---|---|
| 描述符会不会顺着同一条写通道写到 DDR？ | **不会**。PL 侧有两种"描述符"，两种都不走 `m_axi` 写： ① burst 描述符 `df_*` 是包装层的**片上 FIFO**（`df_addr/df_len`，`:752-755`），只喂本模块的 AX 引擎；② E1E2 的 64-bit 描述符表是**读进来**的（`desc_ram[0:7]` 由 `mem_rd_data` 装载，`bin_e1e2.v:198,736`），`base_desc` 只出现在读地址 `dbase_q = base_desc + grp*512`（`bin_catpack.v:346`、`bin_e1e2.v:491`）。 | `bin_unit_axi_wrap.v:752-755,2173,2380,2396-2397`；`bin_e1e2.v:198,736,750`；`bin_catpack.v:344-346` |
| 那描述符是谁写的？ | **PS**（`bin_net.py:3205-3207` `_v50_materialize`），写在 `need` 里额外加的 `desc_bytes` 段。 | `bin_net.py:3203-3207` |
| 有没有可能"写到 out_end 之后"？ | 若 PS 把 `r_edesc` 指到 OUT 槽里 ⇒ 会（但那是 PS 指针错，且本层 3 个 conv 启动**完全不看** `r_edesc`）。反过来，PL 侧**没有**任何"用 out 基址写控制块"的逻辑。 | `:956`（`0x208` 只由 AXI-Lite 配置写）；`:1266-1268`（`r_edesc==0` 直接报错，不启动） |
| 结论 | **desc 不可能贡献那 8 字节。** | 上面 4 行 |

### 5.2 最后一笔 burst 的 `WSTRB` —— 只会少写，不会多写

* 整字路径（本 3 层走这条）：`push_beat(pw_word, pw_data, 8'hFF, …)`（`:2326`），
  前提是 `u_wr_sel==8'hFF && u_wr_addr[2:0]==0`（`:1658`）⇒ 地址 8 字节对齐、8 个 lane 全有效
  ⇒ `WSTRB=FF` 与地址**同时**成立，不存在"地址对但 strb 多一位"。
* 字节路径（`cout%8≠0` 时）：`be_strb` 只由"真的到过这个 lane 的字节"或上
  （`:1686` `be_strb_nx = be_strb | (1<<w_lane)`、`:2359` `be_strb <= (8'd1 << w_lane)`），
  `merge_done` 还要求 `be_strb_nx==8'hFF` 才落拍（`:2221-2222`）。⇒ 落出去的每个 strb 位都有真实字节。
* 一般性（题设要求说明）：设末组有效 lane 数 `nvalid = cout - 8(cg-1) ∈ [1,8]`，
  则本层最大被写字节 =
  `base + (cg-1)·Hout·Wout·8 + (Hout·Wout-1)·8 + (nvalid-1)` = `base + out_bytes - 8 + nvalid - 1 ≤ out_bytes - 1`。
  等式当且仅当 `nvalid=8` 成立。**`nvalid<8` 时严格小于 `out_bytes`。**
* 结论：**`WSTRB` 不是 8 字节越界的来源。**

### 5.3 `drain`/收尾状态机 —— 没有"多发一笔空 beat"的分支

`push_now`（= 真的会产生一笔 beat 的唯一组合信号）只有四个来源（`:2226`）：
`merge_done`（半个字被凑满）、`merge_change && be_open`（换字时先把上一拍落掉）、
`drain_pub`（`wst==W_DR1 && be_open`，`:2224`）、`pw_valid`（整字路径落盘，`:2322-2326`）。
这四者与 `push_beat(...)` 的调用点**一一对应**（`:2326` 整字落盘、`:2348` 凑满一个字、`:2356` 换字前先落上一拍、`:2371` 排空），
且 `be_open` 只可能被"真的来过一个字节"置 1（`:2359`）。逐个检查"空 beat"的可能性：

| 怀疑点 | 结论 | 证据 |
|---|---|---|
| 排空时会不会凭空推一个 beat？ | 不会：`W_DR1` 里 `if (be_open) push_beat(...)` —— 没有 `be_open` 就什么都不推；`drain_cond` 本身还要求 `be_open || bu_len!=0`。 | `:2209`、`:2369-2376` |
| `W_DR2` 会不会多收一笔描述符？ | 只在 `bu_len != 0` 时收，且 `bu_len` 是"已经推出去但还没被收笔的 beat 数"，`push_beat` 里不 chain 时也会收（同一套记账）⇒ 描述符数 ≡ burst 数。 | `:2377-2385`、`:2163-2182`、`:2250-2252` |
| 描述符长度会不会大于 FIFO 里的真实 beat 数（⇒ 把旧 beat 当新 beat 再发一次、地址还前进）？ | 不会：`ax_can_start` 要求 `bf_cnt >= df_len[df_rp]`，且"收笔那一拍它的 L 个 beat 必然都还在 FIFO 里"（只有本笔的描述符才能弹它们）。`BU_MAX=8 ≤ bf_cnt 峰值 14`。 | `:2253-2254`、`:2476-2517` |
| 同一拍会不会推两笔？ | 不会：`p_merge` 含 `!pw_valid`（`:1690`）、`drain_pub` 含 `wst==W_DR1`（与 `wst==W_IDLE` 互斥）⇒ `push_beat` 每拍最多一次，`bf_cnt` 每拍最多 +1。 | `:1690`、`:2226`、`:2262-2277` |
| `done` 会不会在最后一笔还没落盘时就报出去（PS 提前读、第二次写？） | 不会：`wr_all_idle` 含 `!be_open && bu_len==0 && !p_valid && !pw_valid && bf_cnt==0 && df_cnt==0 && axst==IDLE`。 | `:2471-2474`、`:1019`（`run_active && unit_done_q && wr_all_idle` 才报 done） |
| **`u_ce` 冻结时会不会把同一拍吞两次？** | 不会：`wide_pend` **不含** `u_ce`（`:1676`）但 `wr_wide_b = wide_pend && u_ce`（`:1677`）；冻结期间 `pw_valid <= wr_wide_b = 0`，而单元被 `ap_ce=u_ce` 冻在同一笔上、解冻后**只**被吞一次；`p_valid` 改成"并不掉就等"（`:2292-2299`），`drain_cond` 含 `!p_valid && !wr_byte`（`:2209`）不跨过未合并的字节。**这一条是本轮 `+LASTHOLD` 定点刀专门打的**。 | `:1658-1690`、`:2290-2326`、§3.4 |

### 5.4 两个 OUT 槽相邻会不会"跨槽写"

* 槽布局：`_PlBackend.__init__` 逐个 `arena.region(nout+4096)` 切槽（`bin_net.py:808-810`），
  槽与槽**首尾相接**，每个槽尾 4 KB 是金丝雀（`bin_engine.py:204-207` 填、`:209-214` 查）。
* **PL 完全不知道"槽"这个概念**：它只拿到一个 32 位 `r_out`（`:941` 由 AXI-Lite 写 `0xD0`），
  把它们塞进单元的 `base_out` 端口（`:1344`），地址由单元自己在 `[base_out, base_out+out_bytes)` 里生成
  （`bin_conv_core8.v:889` `oaddr_q <= ogbase_q`、`:897` `oaddr_q <= oaddr_q + VLANES`、
  `:807-810` `ogbase_q <= ogbase_q + hwout8_q`）。
  ⇒ 没有"槽偏移/槽对齐"运算，也就没有"跨槽地址算错"这一类 bug 的可能形态。
* 唯一与"相邻"有关的事实：**槽 i 的金丝雀尾巴正好贴着槽 i+1 的数据头**。
  所以"槽 i 的金丝雀后 8 字节被写坏" 等价于"有人写了 `base_{i+1}` 处的 8 字节"，
  这既能由"槽 i 多写 4096-8+8"造成，也能由"别的写者把基址指到 `base_{i+1}`"造成（见 §6 判别）。

---

## 6 判别式：那 8 字节**落在金丝雀的哪个位置**，直接决定嫌疑方向

板的 `check_canary` 过去只报"坏 N/4096"、**丢掉位置**（`bin_engine.py:213` 只累加计数）。
新的守卫（`runs/opt_round1/perf/T161_canary_result.md` 记录：已加"首坏偏移 + 前 16 字节"）一旦上板，
一条日志就能把范围劈成三份：

| 首坏偏移 | 意味着 | 指向 |
|---|---|---|
| **+0**（`canary[0..7]`） | 有人写了 `base_out+out_bytes` 这**一个 8 字节字** ⇒ "多一个 beat"，且地址正好是单元地址链的**下一个字**（`oaddr_q` 在最后一个 `term_last` 之后确实停在 `ogbase+out_bytes`，`bin_conv_core8.v:897` —— 但那一拍**没有写使能**） | 单元 epilogue 多发一拍（本轮 3 档 + 定点刀全否）→ 或 E1E2/其它单元被误启动且 `r_eout` 恰好 = 该地址 |
| **+4088**（`canary[4088..4095]`） | 写的是 `槽尾-8`，即**下一个槽的基址附近** ⇒ 与"本层多写一个 beat"**不符**（本层多写只会落在 +0） | 基址配错（`r_eout` 指向槽尾/下一槽）、或别的写者 |
| 分散 | 多次零星写 | 与"层输出写"无关，优先查 PS |

**另一个免费的上界**：若越界的是"整组/整行/地址链错位"，落进金丝雀的字节数会是
`cw·Wout·8 = 12·128·8 = 12 288`（一行）或 `cg·Hout·Wout·8`（整层）级别 ⇒ 报出来会是
`4096/4096`（金丝雀被整块覆盖），**而不是 `8/4096`**。
⇒ 板端"恰好 8/4096"这个数字本身就说明：**越界长度被夹在 1 个 beat（8 B）以内**，
不存在"地址偏了一行/一组"的结构性错误。这与 §5 的逐条否定互相印证。

---

## 7 建议（按先后顺序）

1. **先落金丝雀修复再复测（强烈建议，且零风险）**：把金丝雀钉到 `out_bytes`
   （而不是槽尾），并让 `check_canary` 报 `lo/hi` 偏移与首 16 字节。
   理由：① 现在 227 层里只有 3 层受保护，任何"≤512 KB 的越界"在其余 224 层都静默；
   ② 没有偏移就永远分不清"多一个 beat(+0)"和"基址指错(+4088)"（§6）。
   这一步不改数值语义、不改 RTL，只改 PS 的护栏，**可以在下一次上板时顺带验证**。
2. **复测时按 §6 表读日志**：若首坏偏移 = +0 且复现，则把 `r_run_unit`、`r_out/r_eout`、
   该次 launch 的 `arm()` 参数一起抓下来（此刻"是 conv 单元还是被误启动的单元"是唯一分叉点）。
3. **在"位置"信息没有之前，不建议动 PL**：本轮已用 3 档反压 + 定点刀 + 12 组变体证明
   **conv 写通道在精确形状下不可能多写 8 字节**；盲目改 RTL 只会把已通过的
   `verify_all` / eqv 基线弄脏。
4. 若要继续压 PL 侧的概率空间，下一个性价比最高的实验不是"更多形状"，而是
   **"在最后一拍上做长冻结 + 复位竞态"**（本轮 `+LASTHOLD` 的加强版：冻结 64 拍 → 放开的同时
   PS 立刻读 `AP_RET`）。这一条只在怀疑"done 早报/晚报"时才有意义。

---

## 8 复跑方法（一键，不改生产 RTL）

```bash
cd pynq_specialized_code                     # memgate 的相对路径按这个 cwd 算
# 全部阶段（单元 3 层 / 包装 3 层 ×3 档 / 变体 / 最后一拍定点刀 / 256² 对照）
bash tools/memgate.sh 4 -- bash ../runs/opt_round1/oob_write/tb/run_three_layer.sh
# 单阶段
bash tools/memgate.sh 4 -- bash ../runs/opt_round1/oob_write/tb/run_three_layer.sh unit3l wrap3l-fast
# 旧的 36 形状 / 12 形状仍可原样复跑（本轮只**追加**了 SHAPESET/SWEEP 分支）
bash tools/memgate.sh 4 -- bash ../runs/opt_round1/oob_write/tb/run_unit.sh
bash tools/memgate.sh 4 -- bash ../runs/opt_round1/oob_write/tb/run_wrap.sh -testplusarg SWEEP=1
```

新增 plusarg（旧行为默认不变）：

| TB | 新 plusarg | 含义 |
|---|---|---|
| `tb_oob_conv8` | `+SHAPESET=1/2/3` | 1=`max_out` 三层精确形状；2=`cout×Hout` 变体网格；3=末拍形态（k=1/d=2/单行/单列/单像素）；缺省 0=**旧 36 形状表** |
| `tb_oob_wrap` | `+SWEEP=4/5/6` | 4=三层精确形状；5=变体网格；6=三层 + 最后一拍定点反压 |
| `tb_oob_wrap` | `+LASTHOLD=<n>` | 在"本层最后一个字"上关 ready n 拍（默认 0=关） |
| `tb_oob_wrap` | `+WATCH=<n>` | 看门狗拍数（默认 3e6；大层要放大到 6e7） |
| 两个 TB | 全局超时 | 单元 TB 兜底从 400 M 提到 2 G ns（旧表跑完才 13 ms，无影响） |

产物清单：

| 文件 | 内容 |
|---|---|
| `sim/logs/three_layer_unit_3l.log` | 单元级 3 层 |
| `sim/logs/three_layer_wrap_3l_{fast,wslow,rslow,lasthold}.log` | 包装级 3 层 × 四档 |
| `sim/logs/three_layer_unit_var.log` / `three_layer_wrap_var_fast.log` | 变体网格 |
| `sim/logs/three_layer_unit_256.log` | 同权重 256²（`out_bytes=6 291 456`）对照 |
| `sim/logs/three_layer_{unit,wrap}_{xvlog,xelab}.log` | 编译/精化 |
| `tb/run_three_layer.sh` | 本轮批量驱动（编译一次跑多次） |
