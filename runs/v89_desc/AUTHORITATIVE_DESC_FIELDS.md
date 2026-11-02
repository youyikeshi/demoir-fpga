# v50 描述符字段**权威表**（T-89，第一阶段，只分析不改代码）

- 日期：2026-11-02 17:2x
- 唯一真相源：`runs/timing_v50/rtl_v50/lane16/bin_segq.v`（483 行，md5 `191bda10e6b6ac22b61810cbac355917`）
- 交叉核对：`bin_band_fill_v50.v`（`1b0407bd83056739ebfb8d6b01b60c68`）、
  `bin_unit_axi_wrap_v50.v`（`0229c186e1e6a46dac0f7c4b3fe95778`）、
  `bin_conv_core_v50.v`（`61568f39a5206e9f1e1c9f3b620dadb3`，只读）
- PS 侧：`ps/pynq/bin_engine.py::v50_desc`（`6a14b8b958c70515f1beb7eb8110eb39`）、
  `ps/pynq/bin_net.py::v50_seg_descs`（`5759ed502cfa4349364f0fb04cffc917`）

> 结论先行：**§3 / §12.3 / §13 三版字段表全部与 RTL 不符。**
> 而且不符的**不是"语义命名"，是"位"**：RTL 从来没读过字 1（`out_pa`），
> 它把**字 2（`cfg_c`/`cin`）**当成了输出缓冲区地址。

---

## 0. 第一性事实：`dw[i]` 是「64-bit 拍」，不是「字」

```verilog
parameter int  DESC_WORDS = 10,                              // segq:55
parameter int  HDR_BEATS  = (DESC_WORDS*4 + 7)/8,            // segq:56 → (40+7)/8 = 5
logic [63:0] dw [0:HDR_BEATS-1];                             // segq:169
dw[hdr_beat] <= hdr_q;                                       // segq:304  收一拍
hdr_addr = cur_pa + {26'd0, hdr_beat, 3'd0};                 // segq:204  + beat*8
```

⇒ 一条描述符 = 5 个 64-bit 拍，拍号 = 字节偏移 / 8：

| 拍 | 覆盖字节 | 覆盖的"字" |
|---|---|---|
| `dw[0]` | 0..7   | 字 0 ‖ 字 1 |
| `dw[1]` | 8..15  | 字 2 ‖ 字 3 |
| `dw[2]` | 16..23 | 字 4 ‖ 字 5 |
| `dw[3]` | 24..31 | 字 6 ‖ 字 7 |
| `dw[4]` | 32..39 | 字 8 ‖ 字 9 |

$$\Rightarrow\ \textbf{凡是把 } \mathtt{dw[1]}\ \text{当"字 1"的代码/注释，都错位了一个字}$$
`dw[1][31:0]` 是**字 2**（字节 8..11），不是字 1。

---

## 1. 权威字段表（逐 bit 段 → 语义 → 行号 → 消费者）

RTL 侧行号 = `bin_segq.v`；消费侧行号 = `bin_unit_axi_wrap_v50.v`。

| 拍 | bit 段 | 字节 | = 字 | RTL 锁存 | RTL 内部用途（行号） | 端口 | 消费者（wrap 行号） |
|---|---|---|---|---|---|---|---|
| `dw[0]` | `[31:0]` | 0..3 | **w0** | `f_in` (339) | `ld_base`(346) → `ld_addr`(206)；E_NULL(240)；in_arena(243) | `d_in_pa` | 填充器 `.in_pa`(1809) |
| `dw[0]` | `[63:32]` | 4..7 | **w1** | **无** | **全文件无任何读取**（`grep 'dw\[0\]\[63'` 零命中） | 无 | 无 —— **悬空** |
| `dw[1]` | `[15:0]` | 8..9 | w2[15:0] | `f_cin` (326) | 无校验 | `d_cin` | `c_cfg_cin`(1372) → conv `.cfg_cin` |
| `dw[1]` | `[31:0]` | 8..11 | **w2** | `f_out` (340) | **`st_base`(347) → `st_wa`(462)**；E_NULL(240)；**in_arena(244)** | `d_out_pa` | 无（wrap 只接不消费）；真正生效的是 `st_wa` |
| `dw[1]` | `[47:32]` | 12..13 | w3[15:0] | `f_w` (328) | 无校验 | `d_w` | 填充器 `.cfg_h`(1811)、`fill_rows`(1348)、`band_words`(1660) |
| `dw[1]` | `[63:48]` | 14..15 | w3[31:16] | `f_h` (327) | 无校验 | `d_h` | 填充器 `.h_in`(1812)（`seg_h_in` 1370） |
| `dw[2]` | `[15:0]` | 16..17 | w4[15:0] | `f_cout` (329) | 无校验 | `d_cout` | `seg_w4`(1371) → conv `.cfg_w`(1381)、填充器 `.w_row`(1813)、`band_words`(1661) |
| `dw[2]` | `[31:16]` | 18..19 | w4[31:16] | **无** | 未读 | 无 | 无 |
| `dw[2]` | `[39:32]` | 20 | w5[7:0] | `f_k` (330) | 无校验 | `d_k` | `c_cfg_k`(1374) → conv |
| `dw[2]` | `[47:40]` | 21 | w5[15:8] | `f_s` (331) | 无校验 | `d_s` | `c_cfg_s`(1375) → conv |
| `dw[2]` | `[55:48]` | 22 | w5[23:16] | `f_d` (332) | 无校验 | `d_d` | `c_cfg_d`(1376) → conv |
| `dw[2]` | `[63:56]` | 23 | w5[31:24] | `f_pad` (333) | 无校验 | `d_pad` | `c_cfg_pad`(1377) → conv |
| `dw[3]` | `[15:0]` | 24..25 | w6[15:0] | `f_y0` (334) | 无校验 | `d_band_y0` | 填充器 `.y0`(1810) |
| `dw[3]` | `[31:16]` | 26..27 | w6[31:16] | **无** | 未读 | 无 | 无 |
| `dw[3]` | `[63:32]` | 28..31 | **w7** | `f_flags` (335) | **E_NULL 要求非 0 (241)** | `d_flags` | 无（wrap 接 `seg_flags` 但**不消费**） |
| `dw[4]` | `[31:0]` | 32..35 | **w8** | `f_next` (336) | 链指针 (404, 407, 410) | `d_next_pa` | 内部 |
| `dw[4]` | `[47:32]` | 36..37 | w9[15:0] | `f_nband` (337) | **E_SIZE 要求非 0 (236)** | `d_nband` | conv `.cfg_h`(1379)、填充器 `.nband`(1814)、`band_words`(1658) |
| `dw[4]` | `[63:48]` | 38..39 | w9[31:16] | `f_opmask` (338) | **OP_MASK_SUP 检查 (238)** | `d_op_mask` | `fill_conv_en`=`seg_op_mask[0]`(1346)、`band_opmask` |

补充：`band_words` 是 segq 的**输入**（wrap:1658 驱动），同时被
`E_SIZE`(236) 与 `in_arena`(243/244) 当长度用 —— 而 `band_words` 本身由
`seg_cin / fill_rows / seg_w4`（全部是 `d_*`）算出 ⇒ **循环依赖**，这就是缺口③。

## 2. `dw[0]` / `dw[1]` 低 32 位的真相（本任务要澄清的那一条）

$$\mathtt{dw[0][31{:}0]} = \text{字节 }0..3 = \textbf{字 0} = \mathtt{in\_pa}\ (\text{地址})$$
$$\mathtt{dw[1][31{:}0]} = \text{字节 }8..11 = \textbf{字 2} = \mathtt{cfg\_c}\ (= \mathtt{cin})\ \textbf{不是地址}$$

证据链（四条，互相独立）：

1. `dw[1][15:0]` 已经单独被锁成 `f_cin`（326）。同一个 32 位段不可能既是 cin 又是地址。
2. 真正该当 `out_pa` 的 **字 1 = `dw[0][63:32]`** 在整份文件里**零读取** —— 一个"必须存在"的字段没人读，只有一个解释：写的人以为它叫 `dw[1]`。
3. segq:318-325 的注释块自述 "字2 = dw[1][31:0] {16'h0,cin}"、"字3 = dw[1][63:32]" —— 注释作者已经知道 `dw[1]` 是"字 2‖字 3"。**只有 240/244/340/347 这四行违反了同一份注释**。
4. 模块头注释（segq:20-21）写 "字0 in_pa / 字1 out_pa"，而代码把 out_pa 取到字 2 ⇒ 头注释与代码在"字 1"上分叉。

⇒ 判定：**这是 `dw` 把"字"当"拍"的 off-by-one，四处**（不是两种设计取向），
与 v39 描述符布局（`in_pa/out_pa/cin/{h,w}/cout/{pad,d,s,k}`）**完全一致**，
只错在索引。修法是把这四处的 `dw[1][31:0]` 换成 `dw[0][63:32]`。

## 3. 后果（四条，全部**静默**，不报错）

| # | 后果 | 证据 |
|---|---|---|
| ① | **排空写到 DDR 低地址**：`st_wa = st_base + cnt*8`，`st_base = cin`。PS 的 `out_pa`（w1）被整体丢弃 ⇒ 输出写到物理地址 `cin`（cin=32 ⇒ `0x20`）。8 B × `band_words` | segq:347, 462 |
| ② | E_NULL 要求 `dw[1][31:0] != 0` ⇒ **`cin=0` 的描述符被拒** | segq:240 |
| ③ | `in_arena(dw[1][31:0], band_words*8)` 把 **cin 当缓冲区**做包含性检查 ⇒ 一旦 PS 开始写 `0x318/0x31C`（arena_lo≠0），**第一条描述符就 E_RANGE 停机** | segq:244 |
| ④ | E_NULL 还要求 `dw[3][63:32] != 0` = **`flags != 0`** ⇒ 非末带且不需物化（`flags=0`）的带被判 E_NULL，违反 §10.2"未定义位忽略、不报错" | segq:241 |

为什么至今没被抓住：`ARENA_LO/HI` 复位值 = `0x0` / `0xFFFF_FFFF`（wrap:891），
PS **从不写** `0x318/0x31C`（`grep -n arena_lo ps/pynq/*.py` 零命中）
⇒ `in_arena` 恒真、E_NULL 只卡 cin==0 ⇒ **segq 一条 err 都不报，输出静默写到地址 `cin`**。
这正是本项目历史上"PL 越界写踩烂内核内存"的同一类失效（见 `docs/板端事故_内存踩踏_真相.md`）。

## 4. PS 填 ↔ RTL 读 逐条对照（12 行）

PS 侧（`v50_desc`/`v50_seg_descs`）实际填：
`w0=in_pa, w1=out_pa, w2=cfg_c(=c_in), w3=cfg_h|H_in, w4=w_in, w5=cfg_group(恒0),
w6=band_y0, w7=flags, w8=next_pa, w9=nband|op_mask`

| 字 | PS 填 | PS 含义 | RTL 读哪个位 | RTL 起的名 | RTL 实际吃到 | 判定 |
|---|---|---|---|---|---|---|
| w0 | `in_pa` | 段输入 DDR 源基址 | `dw[0][31:0]` | `d_in_pa` | ✅ 一致 | **对** |
| w1 | `out_pa` | 输出基址（不带带偏移） | **无** | — | ❌ 被丢弃 | **错位（w1 悬空）** |
| w2 | `cfg_c` | 组通道数 / 输入通道 | `dw[1][31:0]`→`d_out_pa`、`dw[1][15:0]`→`d_cin` | `d_out_pa`/`d_cin` | ⚠ 既被当 cin（对）又被当输出地址（错） | **一处对、一处错** |
| w3 低 | `cfg_h`=nband | 本带有效输出行数 | `dw[1][47:32]` | `d_w` | ✅ 值对、名错（`d_w` 实为 cfg_h） | **对（T-88 已按名改接）** |
| w3 高 | `H_in` | 输入张量高度 | `dw[1][63:48]` | `d_h` | ✅ 值对、名错（`d_h` 实为 H_in） | **对（T-88 已按名改接）** |
| w4 | `cfg_w`=w_in | 行宽 W | `dw[2][15:0]` | `d_cout` | ✅ 值对、名错（`d_cout` 实为 W） | **对（T-88 已按名改接）** |
| w5 | `cfg_group`=0 | 组数 | `dw[2][39:32]`…`[63:56]` | `d_k/d_s/d_d/d_pad` | ❌ 恒 0 ⇒ conv 拿到 k=s=d=pad=0 | **错（§13 里没有 k/s/d/pad）** |
| w6 | `band_y0` | 本带首有效输出行绝对行号 | `dw[3][15:0]` | `d_band_y0` | ✅ 一致 | **对** |
| w7 | `flags` | 末带/物化/shuffle/f32 | `dw[3][63:32]` | `d_flags` | ✅ 值对（但无人消费）＋**E_NULL 误伤** | **对但没人用** |
| w8 | `next_pa`（由 `v50_chain_words` 覆盖） | 链指针 | `dw[4][31:0]` | `d_next_pa` | ✅ 一致 | **对** |
| w9 低 | `nband` | 本带有效输出行数 | `dw[4][47:32]` | `d_nband` | ✅ 一致 | **对** |
| w9 高 | `op_mask` | 小算子掩码 | `dw[4][63:48]` | `d_op_mask` | ✅（但 wrap 传 `.OP_MASK_SUP(16'h0001)`，bit1=pack 会被 E_OPMASK 拒） | **对（配置待改）** |

$$\Rightarrow\ \textbf{6 处对不上}：\text{位置错位 }1\ (\mathtt{out\_pa}) + \text{字段不存在 }5\ (\mathtt{cout,k,s,d,pad})$$
$$\Rightarrow\ \text{另有 }1\ \text{处校验误伤（}\mathtt{flags}=0 \Rightarrow \mathtt{E\_NULL}）\text{、}1\ \text{处仍未消费（}\mathtt{flags}\text{）}$$

`cout` 的问题是**结构性**的：§13 的 10 个字里**没有 cout 的位置**
（w4 被 W 占、w5 被 cfg_group 占）。RTL 的 `d_cout` 取的是 w4[15:0] = W，
所以融合模式下 conv 的 `cfg_cout` 现在等于 **行宽 W**（例如 224），不是通道数。

## 5. 三处必须改 `bin_segq.v` 的地方（提案，未改）

```diff
@@ segq:240  E_NULL：out_pa 取字 1（dw[0][63:32]），并删掉对 flags 的非零要求
-        end else if ((dw[0][31:0] == 32'd0) || (dw[1][31:0] == 32'd0)
-                     || (dw[3][63:32] == 32'd0)) begin
+        end else if ((dw[0][31:0] == 32'd0) || (dw[0][63:32] == 32'd0)) begin
@@ segq:243-244  in_arena：第二条也用字 1
-        end else if (!in_arena({32'd0, dw[0][31:0]}, {32'd0, band_words} << 3)
-                     || !in_arena({32'd0, dw[1][31:0]}, {32'd0, band_words} << 3)) begin
+        end else if (!in_arena({32'd0, dw[0][31:0]}, {32'd0, band_words} << 3)
+                     || !in_arena({32'd0, dw[0][63:32]}, {32'd0, band_words} << 3)) begin
@@ segq:340  d_out_pa
-                    f_out    <= dw[1][31:0] & 32'hFFFF_FFFF;
+                    f_out    <= dw[0][63:32];
@@ segq:347  排空基址（这一行决定输出到底写哪儿）
-                        st_base <= dw[1][31:0];
+                        st_base <= dw[0][63:32];
```

改动量：**4 行**（+ 删掉 E_NULL 里的 `dw[3][63:32]` 一项）。不改状态机、不改时间轴、
不改端口、不改 `DESC_WORDS`，四条已验证路径（推进/链尾/出错即停/重启）语义不变。

## 6. T-89 新发现（⑨级，需要主代理裁定）：conv 核的 `cfg_h` 是**双用途**

```verilog
wire [15:0] hout_c = ((cfg_h - 1) >> s_sh_c) + 1;      // conv:156  cfg_h = 输出行数
wire [31:0] hw8_c  = (cfg_h * cfg_w) << 3;             // conv:163  cfg_h = 通道块步长乘数
wire [31:0] cp1_q  = mn_cb_q * hw8_q;                  // conv:805  (cb-1) * H*W*8
wire        pix_last = term_last && (oy_q == hout_m1_q) && ...;  // conv:230
```

- 输出行数 **和** 64-通道块步长（`[blk][row][col]` 的 blk 跨度）用**同一个 `cfg_h`**；
- band 布局（填充器 §2 的 `word = grp*(rows_fill*W) + r*W + x`）要求块步长 = `rows_fill*W*8`；
- band 输出行数要求 = `nband`；
- $$\Rightarrow\ \mathtt{halo}\neq 0\ \text{时 } \mathtt{rows\_fill} = \mathtt{nband}+2\cdot\mathtt{HALO} \neq \mathtt{nband} \Rightarrow \textbf{两者无法同时满足}$$

⇒ T-88 的修法 `c_cfg_h = nband`（wrap:1379）对"输出行数"是对的，
对 `cin>64`（`cb>1`，227 层里 `cin` 常见 128/192）的**通道块步长是错的**；
而 T-88 缺口④ 提的 `base_act = HALO*W*8` 只修首地址、修不了这个步长。
核不许改 ⇒ 只能由契约裁：① band 内不装 halo 行（pad 交回核，与 §11.3 冲突）；
② 给核加 `cfg_hblk`（改核，禁改）；③ k≥3 的 conv 段暂不进融合。
**建议**：本阶段融合只跑 `k=1`（`halo=0` 时 `nband==rows_fill`，双用途自动自洽），
k=3/5 段继续走 v39 路径 —— 这样 §12.4 的 op_mask 收敛策略不用改，风险最小。

## 7. 附：本次未消费/悬空的字段（供后续清理）

`dw[0][63:32]`（w1，本应 out_pa）、`dw[2][31:16]`（w4 高 16）、`dw[3][31:16]`（w6 高 16）
三处**零读取**；`d_out_pa`、`d_flags` 在 wrap 里**接了不消费**。
