# DESIGN_16LANE — 8-lane conv 引擎模块地图 / 数据流 / 16-lane 评估

只读分析,源 `runs/v42e_archive/rtl_v39_exact/`。**未写 RTL、未起任何工具**。
标记:`[码]`=行号可查;`[推]`=推测。

## 0. 五问结论

| # | 结论 | 证据 |
|---|---|---|
| 1 lane | **共享读**:8 lane = 8 个**连续输出通道(oc)**,同一拍**共用 1 个 64-bit 激活字**;非 8 段独立激活 ⇒ **16-lane 不需要 2× 读带宽** | `[码]` core8:26,336-346,57 |
| 2 权重 | **不是 AXI-Lite 寄存器堆**。核在 ST_WLD 期从 DDR(`base_wgt`)自拉,进 **8 块独立 256×64 BRAM**,每拍每 lane 取 1 个 64-bit 字 | `[码]` core8:234-259 |
| 3 累加 | 每 lane `p_acc[15:0]`,深度 `K=C_B*k²`(**与 cout/VLANES 无关**),上界 `64·K≤16384<2^15`。**16-lane 不改深度/位宽** | `[码]` core8:331,351,669,435-437 |
| 4 线缓存 | 直接映射 **512 条 × 16 字**,每拍只吐 **1 个** 64-bit 字喂整核。`ARLEN=15,AR_SLOTS=4,PF_AHEAD=1,PF_DEPTH=4`。**16-lane 零改动** | `[码]` wrap:313,330,429,343-346,598 |
| 5 契约 | C8 布局 + §7.1 寄存器表;kick=`AP_CTRL.bit0`,done=`AP_RETURN.bit0` 且等 `wr_all_idle`。4 条不变量见 §7.3 | `[码]` wrap:34-35,195,1090-1092 |

**一句话**:并行维度是 **oc**,不是激活。16-lane 的收益是"同一次激活读算 16 个通道";代价在权重 BRAM / 参数装载 / 写口,**不在读带宽**。

## 1. 模块地图

```
bin_unit_axi_wrap (UNIT 参数化调度器, 2784 行)
├─ AXI-Lite 从机   §1 寄存器表, A[16]=1 配置页, 偏移相对 +0x10000
├─ 读引擎          AR 队列(AR_SLOTS=4) + 预取(PF_AHEAD=1/PF_DEPTH=4), ARID=条目号
├─ 线缓存 win_q    512 条 × RD_WIN_WORDS=16 字, 直接映射, BRAM 单写口/单读口
├─ 同步 CE         缺行⇒停单元时钟;命中⇒零气泡,结果与直连 BRAM 逐位相同
├─ 写引擎          beat FIFO + burst FIFO + AW/W/B (完全串行)
└─ generate        UNIT=0→bin_conv_core(wrap:1211,1233) / 1 rprelu / 2 sam / 3 interp / 4 e1e2 / 5 catpack
bin_conv_core 47 行 端口兼容薄壳 → bin_conv_core8 (896 行, 真正核: 8 条 XNOR+popcount lane)
bin_e1e2     572 行 卷积**之前**的 int8→二值打包单元(UNIT=4), 见 §4.4
```

⚠ 命名陷阱:`bin_conv_core8` 的 `LANES=64` 是 **popcount 位宽**,`COUT_MAX` 未使用;真正 lane 数是 **`VLANES=8`**(core8:54-57)。

## 2. ASCII 数据流

```
   DDR ──AXI R 64bit (RD_WIN_WORDS=16 拍 INCR burst = 一条 128B 线)
     │
  win_q 线缓存  512 条×16 字, 每拍 1 字; miss ⇒ 停 ap_ce 填行再放行
     │ mem_rd_data[63:0]  ★ 一个字, 全部 lane 共用
     │ | pad_or_c  (末块按位 OR ~mask_last ⇒ 补零位强制 1)
     ▼ ── 以下 "for k" 是唯一要复制 16 份的部分 ──
   for k in 0..VLANES-1:
     xn_c[k] = ~(act_c ^ wrd[k]);   wrd[k] ← wmem[k][l_q]
     pc_c[k] = pop64(xn_c[k])                              7 bit
     │ 打一拍(拆 pop 树 / 进位链)
   p_acc[k](16b) += pc ; l==0 清零 ; l==last ⇒ p_snap[k](16b)   深度 K=C_B*k²
     │ 每 lane 8 级标量收尾 S1..S8
   S1 d16=2p−N_tot(16b) → prod=d16*alpha(16×16,1DSP)   S2 q=prod>>>9 静态
   S3 dd=q+bz   S4 pn=dd*slope(32×16,2DSP)   S5 sn=pn>>>8 静态
   S6 r=ddpos?dd:sn+gamma   S7 v=r>>>shift 桶形   S8 sat8 → wr_byte_q[k]
     │
   mem_wr_data[VLANES*8-1:0] lane k→byte k (C8 天然对齐)
   写口: 字地址相同则合并 ⇒ WSTRB=8'hFF; 相邻像素地址 +8 ⇒ INCR burst
```

16-lane 只把"最内层 for k"由 8 扩到 16;`act_c`/线缓存/地址链/写引擎全不动。

## 3. Q1:lane 语义(决定带宽)

**结论:共享读。** lane k = 输出通道 `o0+k`,`o0` 必为 VLANES 倍数(core8:166)。

```verilog
// core8:336-346   ★ 一个字喂全部 lane
wire [63:0] act_c = (r1_rd ? mem_rd_data : 64'd0) | pad_or_c;
for (int k = 0; k < VLANES; k++) begin
    xn_c[k] = ~(act_c ^ wrd[k]);   // act_c 无 k 下标 ⇒ 共享
    pc_c[k] = pop64(xn_c[k]);
end
```
- 头注释 core8:26 明写"8 个 lane 共用**同一次**激活读"(图见 core8:26-31);term 定义 core8:25 `l=j*C_B+b`,`K=C_B*k²` —— **不含 oc**,故 K 与 lane 数无关。
- 布局 wrap:195 `addr=(o/8)·Hout·Wout·8 + y·Wout·8 + x·8 + (o%8)`,lane 0-7 正好填满同一个 64-bit 字的 8 个字节。

**⇒ 16-lane 不需要 2× 读带宽(答 A2)。** 每拍需求恒为 **1 个 64-bit 字/term**,与 lane 数无关;总时间 ∝ 总 term 数 = `像素数 × K × ceil(cout/VLANES)`,VLANES 翻倍 ⇒ **总激活读流量反而减半**。实测 54.3 cyc/term 全在等读(延迟受限,非带宽受限)⇒ 2× 吞吐几乎白拿 `[码]` wrap:102-108。

## 4. Q2:权重

**存储**(core8:234-259):**VLANES 块独立 BRAM**,各 `[63:0] wmem[0:255]`(256×64b = 1 RAMB36/块,8 lane ⇒ 8 块)。不分一块 512-bit 宽 RAM 的理由见 core8:235-241:块数相同,但"写 512 位中的 64 位切片"要靠 byte-enable 推断,Vivado 有退化成 FF 的先例。写口**不加复位**(否则拒推断)。

**装载:PS 不经 AXI-Lite 写权重。** ST_WLD 期 `mem_rd_addr=base_wgt+偏移`,每拍 `wl_lane` 选一块 BRAM,`wmem[wl_l][wl_l] <= mem_rd_data & wkeep_c`(末块 AND `mask_last` ⇒ 补零位=0);8 拍装完 8 个 lane 的同一 term。每周期**每 lane 取 1 个 64-bit 字**。一层 8 oc 只需 `8×K×64b`(最大 K=36 ⇒ 2.25 KB),整趟复用(core8:15-21)。

**`pw[0..15]` 不是权重**(core8:267):per-oc-group 的 epilogue 参数 —— `0..1` alpha_q int16×8、`2..5` bias int32×8、`6..9` zeta、`10..13` gamma、`14..15` slope_q int16×8。ST_PRM 期经 `prm_addr(i,o0)`(core8:269-284)从 DDR 拉;每 lane 的 5 个参数都是 `pw` 的**常量位切片**(core8:296-300)⇒ **纯布线 0 LUT**。

> ⚠ 澄清:wrap:722-724 的 `pw_valid/pw_word/pw_data` 是**写口入口寄存器**,与权重同名不同物。

**`bin_e1e2.v` 的 8 lane 是另一回事**:E1/E2 = 卷积**之前**的 `sat8+阈值比较+二值打包`,`lane k 负责 bit[8k +: 8]`(e1e2:23),8 次 64-bit 读凑 1 个输出字(64 通道 = 8 个 C8 字)。它的 lane 是"输出字的 8 个子字",与 conv 的 oc-lane 正交 ⇒ 16-lane 不该动它。

**16-lane 要变成**:`wmem` 8→**16 块**(+8 RAMB36)、`wrd` 8→16 字/拍、`pw` 16→**32 字**、`mem_wr_data` 64→128 / `mem_wr_sel` 8→16。**权重总流量中性**(每 pass 翻倍,pass 数减半)`[推]`。

## 5. Q3:累加器

| 项 | 值 | 证据 |
|---|---|---|
| 每 lane 累加器 / 累加式 | `logic [15:0] p_acc [0:VLANES-1]`;`sum_c[k]=(l0?0:p_acc[k]) + {9'd0,pc}` | core8:331,351 |
| 深度 | `K=C_B*k²`,`C_B=ceil(cin/64)`,K≤256(LW=9) | core8:84,58 |
| 每 term 上界 / `N_tot` | `pop64≤64` ⇒ `64·K≤16384=2^14`;`ntot_q <= K_lc << 6` | core8:435-436,669 |
| 溢出兜底 | `BIN8-EWOVF` 断言;`d16` 截断有值域证明 | core8:884,435-437 |

`|d|=|2p−N_tot|≤2^14` ⇒ `d16`(16b) 无损,S1 乘法 32×16(2 DSP) 收窄成 16×16(1 DSP),注释明写"8 lane 少 8 个 DSP,且完全不改数值"(core8:435-438)。

**16-lane 不改每 lane 累加深度。** K 只依赖 `cin`/`k`,不依赖 `cout`/VLANES;`N_tot=64K` 同理 ⇒ 位宽、深度、快照点、饱和边界**逐位不变**,字节一致性天然保住。**红线:为省面积把 `p_acc` 收到 15b 或收窄 `d16` 立刻破一致性。**`[码]`+`[推]`

## 6. Q4:线缓存

| 参数 | 值 | 证据 |
|---|---|---|
| 线 = `RD_WIN_WORDS` 个连续 64-bit 字 | **16 字 = 128B**(注释已由 8 升 16) | wrap:311-315 |
| 条目数 / 总量 | `LC_N=512` 直接映射;512×16×64b = **64KB ≈ 14-16 RAMB36** | wrap:330,415,593 |
| 每拍输出 | **1 个 64-bit 字**(1 读口+输出寄存器) | wrap:594-598,1631 |
| `ARLEN` | `RD_WIN_WORDS-1 = 15`(AXI3 4bit,16 拍硬上限) | wrap:427-429,1854 |
| `AR_SLOTS`/`PF_AHEAD`/`PF_DEPTH` | **4 / 1 / 4** | wrap:343,345,346 |
| 未命中 | 停单元时钟 ⇒ 结果与直连 BRAM 逐位相同 | wrap:96-98 |

**16-lane 需要什么变化:零。** 每拍仍只吃 1 字;深度由 `(像素,ky,kx,b)` 工作集决定(注释 12 条 ≤16),与 cout/VLANES 无关。`win_q`/AR 引擎/预取队列是 16-lane 里**最不该复制的大件**。

## 7. Q5:PS 契约

### 7.1 寄存器表(wrap:32-69;偏移相对配置页 `+0x10000`,`A[16]=1`)
```
0x00 AP_CTRL RW b0=start(写1启动,读回0) b1=auto-restart   0x10 AP_RETURN RO b0=done b1=busy [15:8]=错误码
0x18/1C R_C/R_C_HI cin(C); HI≠0⇒ERR_SIZE    0x20/28 R_H/R_W    0x70 R_COUT cout(契约未列,本文件补)
0x40/48/50/58 R_K / R_S / R_D / R_PAD   k / stride / dilation / pad
0x60/68/6C R_SHIFT / R_ALPHA_SHIFT(复位 9) / R_SLOPE_SHIFT(复位 8)   ← 契约固定 9/8
0x80/88/90/98 B_IN / B_IN2 / B_IN3 / B_IN4   输入基址(低字;+4=高字)
0xA0 B_WGT  0xB0 B_BIAS  0xB8/BC/C0 B_ZETA / B_GAMMA / B_SLOPE (32 位, 4 字节步进)
0xD0 B_OUT  0xD8 B_ALPHA alpha_q(契约未列,本文件补)  0xE0/E8/F0 DBG_RD/DBG_WR/DBG_FLAG
```

`R_RUN_UNIT` 0=conv(复位值)/4=E1E2/5=catpack,其它值启动前 `ERR_UNIT`(wrap:525-532,1063)。

### 7.2 kick / done
- **kick** 写 `AP_CTRL.bit0=1`,该位读回 0。启动前走 13 级校验链(ERR_NULL/SIZE/SHIFT/LAYOUT/UNIT/ALIGN…,wrap:1103-1120),任一失败 ⇒ 不启动 + 错误码出 `AP_RETURN[15:8]`。
- **done** `AP_RETURN.bit0`,**必须等 `wr_all_idle`** —— PS 看到 done=1 时内存已一致(写响应也收完,wrap:246-247);`busy`=bit1。**描述符** = 上述寄存器组整体(无独立 descriptor RAM):4 基址 + 6 形状 + 3 shift + cout + 7 数据基址。

### 7.3 16-lane 保持 PS 零改动的 4 条不变量
1. **地址公式不变**(wrap:195)。16 lane 必须写成"两个 C8 plane":lane 0-7 → 组 `o0/8`,lane 8-15 → 组 `o0/8+1`(相隔 `Hout·Wout·8` 字节)。**绝不能**改成 C16 交错(`o%16` 进同字)。
2. **`cout%8==0` 不得收紧成 `%16`**(wrap:1090-1092)。收紧后 `cout=8,24,40…` 的层全部启动失败。要支持 `cout%16==8`,只能靠 lane 8-15 的参数/写口掩码做部分 pass。
3. **每像素仍是 1 个 64-bit 字 / 8 通道组**,字节位置 = 原两次 8-lane pass 的并集。
4. **done 仍晚于 `wr_all_idle`**;`AP_RETURN` 位定义与错误码不变。

## 8. 资源账(8-lane 实测 20,552 LUT / 18,963 FF / 35 BRAM / 101 DSP)

**复制(×2)** —— 每 lane 独立数据路,不可能共享:
| 逻辑 | 增量 |
|---|---|
| XNOR 64b + `pop64` 平衡树 | 最大项,~130 LUT/lane |
| `p_acc`/`p_snap` 16b + 收尾流水 S1..S8 寄存器(8 级×~32b) | +~272 FF/lane |
| S1 `d16×alpha` 16×16 (+8 DSP) / S4 `dd×slope` 32×16 (+16 DSP) | **+24 DSP** |
| `wmem` 256×64 | **+8 RAMB36** |
| `mem_wr_data` 64→128 / `sel` 8→16;5 个参数位切片 | 纯布线+少量 FF,**+0 LUT** |

**不复制(共享)**:
| 逻辑 | 理由 |
|---|---|
| 地址链 / term 计数器(`l_q,b_q,oaddr_q,K_q,ntot_q`,delta 链 a1..a7) | 全核**只有一条**(core8:405)⇒ +0 |
| `win_q`(512×16,~14-16 BRAM)+ AR/预取引擎 | 每拍只喂 1 字,与 lane 数无关 ⇒ **+0** |
| 写引擎(beat/burst FIFO、AW/W/B FSM) | 一拍仍是"1 个 128-bit 组"→2 beat,状态机复用 ⇒ 少量 |
| AXI-Lite 从机 / 寄存器 / 13 级校验 / 错误码链 | PS 契约不变 ⇒ +0 |
| 参数装载状态机 / `prm_addr` / ST_PRM;`bin_e1e2`(UNIT=4) | 字数 16→32 但控制逻辑一套;e1e2 正交不该动 ⇒ 少量/+0 |

**总量估计**:BRAM **35→~43**(+8 权重块);DSP **101→~125**(+24=8×3)`[推]`;LUT **~20.5k→~34-35k**(+13~15k,按 1-lane 版 5,845 LUT 反推边际≈1.8k/lane)`[推]`;FF **~19.0k→~26-27k**`[推]`。xc7z020(53.2k LUT)装得下但偏紧,若同时还要 E1E2/catpack 需重算;更关键的是**时序**:16 份 XNOR+pop 树并排,扇出与布线拥塞要重新过 `[推]`。

## 9. 会破坏字节一致性的改动清单

| # | 改动 | 后果 |
|---|---|---|
| 1 | `p_acc` 收窄(16→15b)或 `d16` 收窄 | 大 K 层累加溢出/截断,静默错 |
| 2 | lane↔通道映射改 C16 交错(`o%16` 同字) | 输出字节位置全错(PS 按 C8 读) |
| 3 | 末块补零只做单侧(激活 OR ~mask 或 权重 AND mask 少一个) | 补零位 XNOR 贡献非 0,popcount 系统性偏大 |
| 4 | `cout%8` 收紧成 `%16` | PS 侧 ERR_LAYOUT,一批层起不来(契约破) |
| 5 | lane 8-15 复用 lane 0-7 的 alpha/bias/zeta/gamma/slope 切片 | 参数错位,整组 8 oc 全错 |
| 6 | 静态移位 `ASH=9/SSH=8` 改为可配 | 与 ERR_SHIFT 冲突,且 40-bit 桶形×lane 面积顶爆 |
| 7 | `done` 提前到 `tail_empty` 而未等 `wr_all_idle` | PS 提前读内存 ⇒ 静默错数据 |
| 8 | `nvalid`/`mem_wr_sel` 掩码按 16 位算 | `cout%16==8` 时越界写相邻 plane |

## 10. 证据索引
- lane 共享读:core8:26-31(图),336-337(`act_c`),342-345(循环),25(K 定义);wrap:195(C8 布局),573-574(64-bit 写口)
- 权重:core8:234-259(BRAM),15-21(容量),267/296-300(`pw` 切片),269-284(`prm_addr`);e1e2:23,25-40
- 累加/值域:core8:331,351,435-438,669,884
- 线缓存/预取 + PS 契约:wrap:91-98,311-315,330,343-346,415,427-429,598,1631,1854;32-69(表),34-35(kick/done),195(地址),246-247(写一致性),1063-1096(校验)
