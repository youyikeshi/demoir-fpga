# T-94：16-lane 写通路修复（包装层级仿真取证）

**一句话**：包装层 TB **复现了**板上签名（改前：94.8–97.5 % 字节不符 + 每层越界 128–3371 B）；
根因是 **16-lane 串行化第一拍的像素地址照抄了核地址**，而核的像素步长 = `VLANES`（16 B），
C8 契约要求 **8 B/像素/平面**。改包装层 3 处（不动核、不动 segq）后：
`VLANES=16` **10 个形状 / 11328 笔写事务逐笔 0 diff、0 越界**，`VLANES=8` 全 PASS。

运行点：xsim（Vivado 2025.2.1）、`bin_unit_axi_wrap_v50 #(.UNIT(0), .VLANES(8|16))`、
RTL = `runs/timing_v50/rtl_v50`（manifest 35 条）、读延迟 1..20 拍 + 随机写反压（WBP=1，seed 12345678）。

---

## 0. 判据总览

| 判据 | 内容 | 结果 |
|---|---|---|
| **W1** | 包装层能否复现板上"越界 + 大面积不符" | **复现**：改前 3/3 形状 FAIL（见 §2）|
| **W2** | `VLANES=16` 逐笔 (addr,data,WSTRB) 与 8-lane 参照 | **10/10 形状，11328 笔，0 差异** ✅ |
| **W3** | `VLANES=16` 逐字节 vs golden(`expected_c8.bin`) | **10/10 形状 0/总字节 不符** ✅ |
| **W4** | `VLANES=16` 输出区尾部越界（窗口 8192 B） | **全部 0 B** ✅ |
| **W5** | `VLANES=8` 回归（同 TB，8-lane 路径） | **10/10 形状 PASS**（路径未改） ✅ |
| **W6** | `cout%VLANES≠0` 末组掩码 | `cout=24/40`（%16=8）**0 diff**；`cout=8`（单组无 beat1）**0 diff** ✅ |
| **W7** | 核级回归（`bin_conv_core_v50` vs v39） | S2 **24/24 ok**；S1 仅 `cout%8≠0` 的 **SEL 位**不同（F2 的预期差异，见 §6） |
| **W8** | manifest + `verify_rtl_root.sh` | **MATCH=35 DIFF=0 RESULT: PASS** ✅ |

> `cout=17` 走不通（`cout%8==0` 是启动硬门禁 `ERR_LAYOUT`）⇒ 末组掩码用**合法**的
> `cout=24/40`（`cout%16=8`）覆盖，效果等价。

---

## 1. 根因（一条主因 + 两条被它掩盖的次要错）

### ① 主因：**第一拍的像素地址**（不是第二拍、不是 plane_delta）

`bin_conv_core_v50.v:851`（核，本任务禁改）

```verilog
oaddr_q <= oaddr_q + VLANES;      // 相邻像素 +VLANES 字节
```

- `VLANES=8` ⇒ +8，正好是 C8 的 8 B/像素 ⇒ 正确；
- `VLANES=16` ⇒ **+16**，即核吐出的 `mem_wr_addr` 是 **C16 交错的"像素对基址"**：
  `addr = pair_base + p*16`，而 `plane_stride_q = hout*wout*16` 是对的。

包装层原来**照抄**这个地址当 beat0、再 `+plane_delta(=Hout*Wout*8)` 当 beat1 ⇒
组 2g 的像素 p 落到 `p*16`（每 16 B 才写 8 B，半平面空洞），组 2g+1 落到
`pair_base + p*16 + P*8` ⇒ 越过本层末尾 `P*8` 的一半。

这违反 `runs/lane16_rtl/DESIGN_16LANE.md §7.3 不变量③`
（"每像素仍是 1 个 64-bit 字 / 8 通道组"）。**plane_delta 本身是对的**，
所以怀疑清单里的"② wr_sel 拆分 / ④ 末组掩码"都不是本次主因。

xsim 逐笔铁证（`cout=64, H=W=8`，P=64，B_OUT=0x200000）：

| # | 8-lane 参照 (addr/data) | 改前 16-lane | 应为 |
|---|---|---|---|
| 0 | `200000 80b180e4b495aee3` | `200000 80b180e4b495aee3` ✓ | `200000` |
| 1 | `200008 80ae80e8bb809be3` | **`200200`** 80af8001d6a2baf1 | `200008`/`200208` |
| 2 | `200010 80ae80e6bb889ce2` | **`200010`** 80ae80e8bb809be3 | `200010` |
| 3 | `200018 80b380e5be96a6e4` | **`200210`** 80ac8001d3adbcf1 | `200018`/`200218` |

即 beat0 步长 0x10（应 0x8）、beat1 = beat0 + 0x200（plane_delta 正确）。
与板上签名完全同构：越界落点 `off 0..8184`（= 1 个平面 8 KB）、**非连续**、
每层 904–4096 B；已写区间 93–98 % 不符。

### ② 次要错 A：`wr_accept_ok = u_wr_en && !wr_fifo_press` 在压力线上**静默丢拍**

16-lane 的 beat0 恰好摆在 `p1_v=1` 那一拍，那一拍 `u_ce` 必为 0（串行化停核）
⇒ beat0 **只能**靠 `wr_xfer` 落地。压力线（`BF_PRESS=BF-4=12`）一到就 `wr_xfer=0`
⇒ 这一拍既不进 FIFO 也不报错。xsim 实测：`cout=64/8x8` **丢 69/512 拍，
全部是 pair 的 lane0-7 那一拍**（落点仍是 0xA5 哨兵）。这一条在地址修好后才暴露
（地址错时它被掩盖），板上会表现为"地址对了但零散字节不对"。

### ③ 次要错 B：把 `wr_fifo_press` 直接删掉 ⇒ 破坏"每拍最多一次 `push_beat`"

`wr_xfer = u_wr_en`（无条件）会让排空状态机 `W_DR1/W_DR2` 里也放行 ⇒ 同拍
`push_beat` 被调用两次（`drain_pub` + `pw_valid`），而 `bf_wp` 只 +1
⇒ **丢一拍 + 描述符错位**。xsim 实测：64 个地址被写两遍、64 个没写
（`beats=512` 但 `unique=448`）。正确形态是**只放过 `wr_split_busy` 这一项**。

---

## 2. 改前 / 改后（同一 TB、同一 golden，`T94FILL=1` 预铺 0xA5）

| 形状 | 改前 逐字节不符 | 改前 越界写 | 改后 不符 | 改后 越界 |
|---|---|---|---|---|
| `cin48 cout64 k3 d1 32×32`（**板上第 1 层**） | **62 144 / 65 536（94.8 %）** | **3371 B** | **0 / 65 536** | **0 B** |
| `cin64 cout64 k3 8×8` | **3992 / 4096（97.5 %）** | 224 B | **0 / 4096** | 0 B |
| `cin48 cout24 k1 8×8`（`cout%16=8`） | **1417 / 1536（92.3 %）** | 128 B | **0 / 1536** | 0 B |

（改前对照 = `runs/v50_archive/rtl_v50/lane16/bin_unit_axi_wrap_v50.v`，md5 `0f7dd0ae…`，
与改前仓库根逐字节相同；`before_rtl/` 是它的副本。）

---

## 3. 改了哪几行（只改 `lane16/bin_unit_axi_wrap_v50.v`，核/segq 一字节未动）

| 位置（新行号，文件共 3490 行） | 改动 |
|---|---|
| **1624–1723** `g_wr_split2` | 串行化器重写：① beat0 地址改为 **`pbase_q + pix_q*8`**（`pbase_q` 在每组 `pix_q==0` 那一拍从核地址取，与核的像素步长无关）；② 捕获从"无条件"改为 **`c_wr_en && u_ce`**；③ 从"每拍强制清 `cw_beat_en`"改为 **hold-until-accepted**（被挡就等下一拍，不丢拍）；④ `pix_q` 在 `npix=Hout*Wout` 处回绕，`c_start` 复位；⑤ 新增 `wr_split_pend_i`（缓冲非空）|
| **568 / 1623 / 1722** | 新信号 `wr_split_pend_i`：8-lane 恒 0；16-lane = `p1_v \|\| cw_beat_en` |
| **1810–1811** | `seg_band_done` 追加 `&& !wr_split_pend_i`（融合带"写完"必须等整条缓冲空，hold 设计下 `p1_v` 不够）|
| **2267–2271** | 新增 `u_ce_ns`（= 原 `u_ce` 去掉 `!wr_split_busy` 那一项）、`wr_accept_ok = u_ce_ns`（**只放过串行化自己的停核**，压力/排空忙/读 miss 照旧挡）|
| **2299** | `assign u_ce = u_ce_ns && !wr_split_busy;`（语义等价、结构更清楚）|

`VLANES=8` 时 `wr_split_busy ≡ 0 ⇒ u_ce_ns ≡ u_ce`，且 `g_wr_direct` 分支一字未改
⇒ **8-lane 与 v39 逐位等价**（下面是实测）。

---

## 4. `VLANES=16` 全矩阵（10 形状 × 2 VLANES）

本目录 golden 由 `pynq_specialized_code/pl/rtl_hand/tools/gen_bin_golden.py` 生成
（`PYTHONHASHSEED=0`，落在 `runs/v94_wrpath/golden/`，**不写仓库 golden/**）。

| 形状（cin/cout/k/pad/H×W） | v8 | v16 | v16 逐笔 (addr,data,sel) |
|---|---|---|---|
| 48/64/3/1/32×32 | 65536/65536 | **65536/65536** | 8192 笔 0 差异 |
| 64/32/3/1/8×8 | 2048/2048 | **2048/2048** | 256 笔 0 |
| 64/48/3/1/8×8 | 3072/3072 | **3072/3072** | 384 笔 0 |
| 64/64/3/1/8×8 | 4096/4096 | **4096/4096** | 512 笔 0 |
| 96/96/1/0/8×8 | 6144/6144 | **6144/6144** | 768 笔 0 |
| 48/24/1/0/8×8 (`%16=8`) | 1536/1536 | **1536/1536** | 192 笔 0 |
| 48/40/1/0/8×8 (`%16=8`, 2.5 组) | 2560/2560 | **2560/2560** | 320 笔 0 |
| 48/16/1/0/8×8（正好 1 对） | 1024/1024 | **1024/1024** | 128 笔 0 |
| 48/8/1/0/8×8（单组、无 beat1） | 512/512 | **512/512** | 64 笔 0 |
| `bin_3x96_64_8x8`（仓库 golden） | 4096/4096 | **4096/4096** | 512 笔 0 |
| **合计** | — | **全 0 diff** | **11328 笔，0 差异** |

尾部越界：10/10 形状 **0 B**（窗口 `total..total+8192`，与板上探针同口径）。
`axi_errors=0`、`rd_bad=0`、无 X/Z。

## 5. 拍数（同 TB，读延迟 1..20 拍；只作方向参考）

| 形状 | v8 | v16 改后 | v16 改前（数据错） |
|---|---|---|---|
| 48/64/3/1/32×32 | 76 028 | **55 774（0.73×）** | 43 984 |
| 64/64/3/1/8×8 | 6 853 | **5 274（0.77×）** | 4 862 |
| 48/24/1/0/8×8 | 1 263 | 1 851（**1.47×，变慢**） | 2 125 |

⇒ **`cout≤24` 的层 16-lane 不划算**（2 组 ×16 lane 把 8 个 lane 算空 + 双拍开销），
`cout≥32` 才赚。这是 PS 侧"按 cout 选 lane"的事，不是 RTL 的事。

## 6. 附带发现（不属本次改动，需主代理记账）

1. **核级 S1 的"24/24 0 diff"与当前 RTL 不符**：用当前
   `lane16/bin_conv_core_v50.v`（md5 `61568f39`）跑 `runs/v50_simA` 的 TB，
   S1 有 8/24 例在 **SEL（末组掩码）** 上不同（全部是 `cout%8≠0`：cout=13/17），
   来自 F2 的 nvalid 修正 —— 是**预期改进**，不是回归；**S2（16-lane vs 8-lane 逐 lane
   数据）仍 24/24 ok**。`REPORT_simA.md` 里"24/24 组 0 diff"是 F2 落地前的口径。
   （`runs/v50_simA/run.sh` 编的是 `_worktrees/v50_impA/bin_conv_core.v`，那是 **F1 未修**的
   旧副本 md5 `e2ed125f`；本轮用 `runs/v94_wrpath/core_reg/` 直接编当前 lane16 核。）
2. **核的像素步长仍是 `+VLANES`**：本修复把它当成"核的既有行为"在包装层换算。
   若将来把核改成 `+8`（一行），包装层**同样正确**（`pix_q==0` 那拍的语义不变），
   但核级 TB 的地址期望（`pix*16`）要一起改，等于重开核验证 ⇒ **建议不动核**。
3. `_worktrees/lane16/modules/bin_unit_axi_wrap.v`（md5 `6b882088`）是**改前**的旧副本，
   不是构建输入；构建只读 `runs/timing_v50/rtl_v50`。

## 7. 产物 / 复现命令

```
runs/v94_wrpath/
  tb_v94_wrap.v                 # 包装层 TB（由 tb/tb_bin_unit_axi_wrap.v 派生：
                                #   DUT→bin_unit_axi_wrap_v50 + VLANES 宏、逐笔写记录、
                                #   0xA5 预铺、8192 B 越界窗口、内部探针）
  build.sh {8|16}               # xvlog + xelab（一次一个进程）
  run.sh   {8|16} <shape>       # 跑一个形状（写 wr_*.txt 逐笔记录）
  gen_gold.py                   # 本目录 golden 生成器（不动仓库 golden/）
  build_before.sh / before/     # 改前对照（v50_archive 原样 wrapper）
  core_reg/                     # 核级回归（当前 lane16 核 vs v39 core8）
  v8/ v16/ before/              # 日志 + 逐笔记录
  diff_before_after.diff        # 改前/改后 RTL diff（171 行，+100/−33）
  verify_rtl_root_t94.log       # MATCH=35 DIFF=0 RESULT: PASS
```

```bash
cd runs/v94_wrpath && ./build.sh 16 && ./run.sh 16 v94_c48_o64_32x32
```

## 8. 未覆盖 / 风险

1. **融合（band）通路没跑**：本 TB 只走 `FUSE_MODE=0`（v39 等价路径）。串行化器
   在 `fuse_m=1` 时同样被使用，且 `c_start = seg_band_start`（每带复位像素计数）——
   静态上成立（`sh_nband` 在带排空期间不变），但**没有仿真覆盖**。
2. **P 很大（H·W > 4096·4096）未测**：`pix_q` 是 32 位，`npix` 32 位，`p*h` 处不会溢出；
   最坏 `npix=16.7 M`、`pix8` 128 MB，仍在 32 位内。
3. **时序未验**：本轮禁综合。新增逻辑 = 1 个 32 位加法器（`pbase_q+pix8`）+ 1 个
   32 位比较（`pix_q+1>=npix`）+ ~70 FF（`pix_q/pbase_q/hold`），全部在**已寄存器的
   数据路**上（`c_wr_addr` 是核输出寄存器 → 加法器 → `cw_beat_addr` 寄存器）；
   `wr_accept_ok` 与 `u_ce` 同源（8-lane 下就是同一根网），没有新的组合环。
   ⚠ 但 `u_ce_ns` 的扇出比 `u_ce` 多（新增写引擎 enable + 串行化 CE），**下颗位流
   必须看 `report_timing`**。
4. **挂死（64² 第 3 层）未复现**：wrapper 级 TB 里同几何层跑完并正常 done；
   10 个形状 v16 全部 done（`wr_all_idle` 内含 `!p_valid/!pw_valid`，新增的 hold 逻辑
   在 done 前会自然排空）。按"越界写踩坏后续层输入/描述符"的次生假设，需上板确认。

---

## 9. 最终指纹（2026-11-02 20:05）

| 文件 | md5 |
|---|---|
| `runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v` | `f1ff43a53985969b408942a1dc0d1377` |
| `runs/timing_v50/rtl_v50/lane16/bin_conv_core_v50.v`（**未改**） | `61568f39a5206e9f1e1c9f3b620dadb3` |
| `runs/timing_v50/rtl_v50/lane16/bin_segq.v`（**未改**） | `266454b87d8b7e4124d1fb1323d78178` |
| `runs/timing_v50/rtl_v50_manifest.txt` | 已刷新该 1 条；`verify_rtl_root.sh` → `MATCH=35 DIFF=0 RESULT: PASS` |

改前对照（`runs/v50_archive/rtl_v50/lane16/bin_unit_axi_wrap_v50.v`）：`0f7dd0ae908eebc8c360c0a6f00a4b8e`。
全矩阵：`MATRIX_final.txt`；逐笔等价：`beat_equiv_final.txt`。
