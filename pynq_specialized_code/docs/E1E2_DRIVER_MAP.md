# E1E2 驱动接入：模块地图与数据流（阶段 1，只读分析）

> 日期：2026-10-31
> 范围：只分析、不改 RTL / 不跑 vivado。文中数字都是对
> `deploy/e1e2_desc/`、`ps/pynq/bin_engine.py`、`ps/pynq/bin_net.py`、
> `pl/rtl_hand/modules/bin_unit_axi_wrap.v`（md5 `6f03b111`）实读/实算得到的。

---

## 0. 一句话

E1E2 是**打包单元**，不是卷积单元：它把 `int8 (cin,H,W)` 变成
`packed uint64 (ceil(cin/64),H,W)`，判据 `bit = en && (sext(src) + THR > 0)`。
它和 conv 复用同一个 `bin_unit_0`，由 `R_RUN_UNIT`（0x200，复位值 0）在运行期二选一
⇒ 旧驱动一个字不改，默认行为逐位不变。

---

## 1. 模块地图

```
┌─ 资产层 deploy/e1e2_desc/（只读交付物，227 层 / 698 命令）
│   desc.bin   44,672 B   698 × 8 个 u64：{thr<<32 | src}
│   cmds.bin   11,168 B   698 × 16 B：{base_desc,base_out,cfg_c,h,w,cfg_group,flags}
│   thr.bin    82,816 B   逐层 int16 阈值 + 记录间 64 B 对齐填充
│   frame.json 455,279 B  commands/buffers/arena/layer_thr/统计
│
├─ PS 驱动层 ps/pynq/
│   bin_engine.py
│     BinGuard        地址/对齐/DDR 区间/缓冲区白名单/金丝雀
│     BinLayer        一层 conv 的几何（index 记录 + 运行时 H/W）
│     BinConvEngine   conv 路径：config → set_bases → run → wait → read_out
│                      + 影子寄存器 _shadow（只写变化的值）+ 异步 arm/kick/reap
│     pack_thresh     (t_q,fs_in) → float32 逐通道比较阈值 adj（快路径）
│     quant_pack      浮点比较 → 64-bit 字（numpy 版）
│     bpack.py/.c     C 内核：pack_f32（浮点比较）/ pack_i8（int8 直通）
│   bin_net.py
│     NetPlan/BinNet  227 层执行图 + 单 PL 流水调度
│     _PlBackend.prepare  prologue = pack_input → push_input → arm（可提前）
│     _PlBackend.kick/poll/finish  start / 轮询 / read_out
│
├─ PL 配置页（相对 bin_unit 基址 +0x10000）
│   conv 旧段 0x00..0xF0：AP_CTRL/AP_RET/C/H/W/K/S/D/PAD/SHIFT/COUT/8 基址/DBG
│   E1E2 新段 0x200..0x240：RUN_UNIT/E_GROUP/E_DESC(+HI)/E_OUT(+HI)/DBG_E_*
│   C/H/W 在 0x18/0x20/0x28 **与 conv 复用**
│
└─ RTL pl/rtl_hand/modules/bin_unit_axi_wrap.v（UNIT=0 实例）
    ├─ AXI4-Lite 从机 + 配置页 + vcode 启动校验（conv 13 级链拆两段）
    ├─ 读侧线缓存 + AR 队列 + 预取；miss 停 CE（= DBG_E_RDST）
    ├─ 写侧 64-bit 整字直落
    └─ generate: bin_conv_core8 + bin_e1e2 同时例化
         start/CE/done/busy/mem_* 由 sel_e1e2_q 二选一
         run_unit_q 在 start 同拍锁存 ⇒ 跑的时候改 0x200 不会切 mux
```

---

## 2. 现有 conv 路径（`BinConvEngine`）逐步

### 2.1 同步单层

```
config(L)                 写 C/C_HI/H/W/HO/WO/K/S/D/PAD/SHIFT/ALPHA_SHIFT/SLOPE_SHIFT/COUT
set_bases(L, in_pa, out_pa)
                          B_IN/B_WGT/B_ALPHA/B_BIAS/B_ZETA/B_GAMMA/B_SLOPE/B_OUT
                          + 8 个高字槽写 0（只有 IN/WGT/BIAS/OUT/ALPHA 有高字槽）
clear_dbg()               DBG_RD=0 / DBG_WR=0 / DBG_FLAG=1（写副作用，force=True）
start()                   AP_CTRL=1（自清位，每次必须真写）
wait(timeout, wdt)        轮询 AP_RETURN.bit0；自适应 sleep；超时绝不二次 start
                          错误：AP_RETURN[15:8] != 0 ⇒ BinRunError
read_out(out_buf, L)      invalidate → from_c8_deq（C8 交错 → (C,H,W) f32 / 2^fs_out）
```

启动前 `guard_layer()` 把 8 个基址、IN/OUT/WGT/alpha/bias/... 的尺寸全部过一遍
`BinGuard.check_pa/check_region`，OUT 尾部 4 KB 金丝雀。

### 2.2 异步三分段（流水的地基）

```
arm(L,in,out) = guard_layer + config + set_bases   # 可在上一层还在跑时写（RTL start 时锁存）
kick()        = start（不等）
poll()        = 读一次 AP_RETURN
reap()        = wait + 错误码 + 金丝雀
```

⚠ 这个"可以提前写配置"成立的前提是：**配置页只有本进程在写，且写不会启动 PL**。
E1E2 的 pack 会**启动**同一个单元，所以它不能在上一层 conv 还没跑完时硬插进去；
RTL 的 start 闸门会等 `!run_active`，结果是"等 conv 完 → 跑 E1E2 → 再发下一层"。

### 2.3 影子寄存器

`_wr(off,v,force=False)`：`_shadow[off] == v` 就跳过。
227 层里 C/H/W/K/S/... 大量重复，每层真正写下去的次数从 ~30 降到 3~6。
`AP_CTRL/DBG_*` 有写副作用 ⇒ 一律 `force=True`。

---

## 3. `frame.json`：698 条命令怎么映射到 227 层

### 3.1 顶层键

| 键 | 内容 |
|---|---|
| `arena` | `{base:0x08000000, size:61,148,224}` |
| `desc` | 描述符表 `{off:0, addr:base, ncmd:698, stride:64, bytes:44672}` |
| `thr` | `{bin_off:44672, bin_addr:base+44672, bin_bytes:82816, bytes:80624}` |
| `cmds` | `cmds.bin` 布局（16 B/条，u32,u32,u16,u16,u16,u8,u8） |
| `commands[698]` | 每条命令：`i/layer/group/cfg_c/h/w/cfg_group/grp_hack/desc_off/base_out/out_off/words/tiles/cycles/lanes_real` |
| `buffers[683]` | `desc(1) + thr(227) + thr_const(1) + int8(227) + packed(227)`，每条有 `{key,kind,off,bytes,C,H,W,fs,op,...}` |
| `layer_thr[227]` | 每层阈值数组 `{off,addr,cin,bytes}` |
| `stats` | 命令数/拍数/覆盖/lane 统计/段统计 |

### 3.2 一层的命令数

`一层命令数 = ceil(cin/64)`（= `cb`，即 64 位字组数）。
分布 `{1:107, 2:10, 3:26, 4:24, 5:11, 6:22, 7:15, 8:3, 9:9}`，Σ = 698。

- 命令 `(layer, g)` 覆盖该层逻辑通道 `64g..64g+63`、全部 `H*W` 像素；
- 输出地址 = `base_out + p*8`，`p=y*W+x`；同一层各组的 `base_out` 相差 `H*W*8`，
  所以一个 packed buffer 被 `g=0..cb-1` 顺序铺满（B1b 检查：缺口 0）。
- `cfg_group` 通常是 `g`；只有 `cin=544` 的第 9 组（`g=8`）写成 **7**（grp8 变通）。

### 3.3 一条命令的 8 条 lane

命令 `i` 的 `desc.bin[i*64:(i+1)*64]` 是 8 个 u64：

```
word[k] = (thr_base[k] << 32) | src_base[k]      k=0..7
src 地址 = src_base[k] + p*8                    # 一个 64-bit 字 = 8 个 C8 通道
thr 地址 = thr_base[k]（读两次 8 B → 8 个 int16）
逻辑通道 = 64*group + 8*k + j                   # j=0..7
en[j]   = (64*cfg_group + 8*k + j < cfg_c)
bit     = en[j] && (sext(src[j]) + thr[j] > 0)
```

这就是"cat 免费"的原因：一条 lane 可以指向任意生产者的 int8 buffer，
cat 在物理上根本不存在；同一个 64 位字组的不同 lane 可以来自不同段。

### 3.4 `thr.bin` 的坑（实测）

- 文件 82,816 B = `thr.bin_off(44,672) … 哨兵地址` 的**完整 span**（含记录间 64 B 对齐填充）。
- `frame.thr.bytes = 80,624` 只是"有效阈值字节数"，**不是文件长度**；不要用它做 memcpy 长度。
- 哨兵地址 = `thr_bin_addr + 82,816`，正好是文件末尾之后 16 B。
- 全表 **546 条 lane** 的描述符 `thr` 指向这个哨兵：546 个全掩码 lane
  （其中 9×4=36 个是 cin=544 的 grp8 变通）。哨兵必须是 **8 × int16(-127)**，
  否则 cin=544 第 9 组的 lane 4..7 会把"正源字节"置 1（静默错 9 层）。

实算核对：
- `cmds.bin` 与 `frame.commands` 698/698 字段一致；
- 40304 个真实通道的 `thr.bin` 值与 `closed_form(d=fs_in−buf.fs, t_q)` **0 不一致**；
- 546 个哨兵引用全部等于 `thr_bin_addr+82,816`，**0 例外**。

### 3.5 arena 布局

```
+0x00000  desc      44,672 B
+0x0AE80  thr       82,816 B   （+ 文件后 16 B 哨兵）
+0x1F200  int8      41.95 MB   (227 个 C8 buffer，源)
+...      packed    19.07 MB   (227 个 C64 buffer，E1E2 输出)
合计 61.15 MB
```

`desc/cmds` 里所有地址都是**绝对物理地址**（arena.base + off）。
驱动若把 arena 放在别的物理地址，必须做**整体平移** `delta = new_base − 0x08000000`：
- `desc.bin` 每个字的低 32 位（src）和高 32 位（thr）各 +delta；
- `cmds.bin` 的 `base_desc/base_out` +delta；
- `frame` 的 `base_out/out_off/desc_addr` 等按 `new_base+off` 重算。
`delta` 必须 8 字节对齐；所有地址仍要 < 2^32。

---

## 4. `gen_e1e2_desc.py` 的两个验证入口（澄清一处任务描述）

- `--verify-all`：在本文件内**按 FSM 语义模拟描述符**（tile→lane→p→bit→flush），
  与 `sat8(rint(x_log·2^d)) + t_q` 的 numpy 参考逐字节比。
  结果：**227/227 层、698/698 命令、0 差异**（`check_report.txt`）。
- `--check-ab` **不在** `ps/tools/gen_e1e2_desc.py` 里，而在
  `pl/rtl_hand/_worktrees/e1e2int/tools/gen_e1e2_cverify.py`（RTL 集成子代理新增）。
  它按 `runs/ps256/ab_pack.py::make_data` 的 seed/数据重放 227 层，
  复现 `runs/ps256/ab_pack256.json` 的 **ref/numpy/C 三列 sha1**；
  实跑：**227/227 一致**。
- 生成器的数值自检是"描述符路径 vs 参考"，**不覆盖驱动**（寄存器序列/搬运/哨兵）。

---

## 5. `bin_net.py` 的 prologue：`quant_pack` 在哪、输入输出是什么

```
BinNet.run(x0)
 └ _PlBackend.prepare(op)                    # 可能在上层 conv 还在跑时调用
    ├ data = e.pack_input(op.deps[0].val, L)
    │         x_f : float32 (cin,H,W)，值域 = 上一层 read_out 的 v = int8/2^fs_out
    │               （或 cat/add/sam/interp 的 PS 浮点结果）
    │         L.d["act_fs_in"] = 本层输入标度
    │         → adj = pack_thresh(t_q, fs_in)          # 逐通道 float32 阈值
    │         → A   = quant_pack(x_f, adj)             # C 内核 bpack.pack_f32（默认）
    │         输出 A : uint8 一维视图，长度 cb*H*W*8（即 packed uint64 字）
    ├ _ensure_cfg(L, inb, outb) → e.arm()     # config + set_bases（影子缓存）
    ├ e.push_input(data, inb)                 # memcpy 进 IN 槽（双缓冲）
    └ BinGuard.fill_canary(outb)
 _PlBackend.kick → start；poll → done；finish → read_out(C8→float)
```

因此"把打包搬到 PL"的接缝就是 **`BinConvEngine.pack_input` 的返回值**：
只要 E1E2 能产出与 `quant_pack(x_f, adj)` **逐字节相同**的 `cb*H*W*8` 字节，
`_PlBackend` 的其余部分（push_input/arm/kick/finish）一行都不用改。

⚠ 但这只搬走了"浮点比较+打包"。输入仍是 float，所以 PS 还要做
`x_q = sat8(rint(x_f·2^fs_in))` 再写 int8 给 E1E2 —— 这段比 `quant_pack` 便宜，
但**不是零**。真正的"全 int8 链路"需要 bin_net 保留每层 int8 C8 缓冲，
那是下一阶段（全 arena 流程）的事；见 §7 风险 1。

---

## 6. 阶段 2/3/4 的落点（本文件之后实现）

| 阶段 | 文件 | 内容 |
|---|---|---|
| 2 | `ps/pynq/bin_engine.py` | `E1E2Program`（读 4 个资产、校验、平移）+ `E1E2Engine`（probe / load_program / run_one / run_layer / run_all / dbg_e / pack_int8 / pack_float）+ `pack_input` 顶部一个 `BIN_PACK=e1e2` 分派（默认 `_e1e2=None`，行为不变） |
| 3 | `ps/pynq/e1e2_sim.py` + `ps/tools/verify_e1e2_path.py` | FSM 模拟器；227 层描述符 vs C 内核；112 个全 d=0 层复现 ab_pack sha1；FakeE1E2Unit 跑驱动寄存器序列/平移/开关 |
| 4 | `ps/pynq/board_e1e2_smoke.py` | `download=False` 接手 PL；R_RUN_UNIT 写读回；5 条真命令（8²/16²）；逐行 fsync；`--run/--dry-run`；DBG_E 读带宽 |

---

## 7. 已识别的风险 / 不确定点（先写在这里，最终报告继续跟踪）

1. **d≠0 的二次舍入 vs "C 内核浮点路径"**
   E1E2 读的是**生产者标度 s** 的 int8；C 内核 `quant_pack` 读的是**消费者标度 fs_in** 的 float。
   当 `d = fs_in − s < 0`（425/805 段）时，E1E2 的参考是
   `sat8(rint(x_s·2^d))`，与"直接从 float 量化到 fs_in"可能差 ≤½ LSB。
   - 对 conv 生产者：`x_s` 本来就是 bin_net 上一层的 int8 输出，PS 也是这么做的
     ⇒ 完全等价；
   - 对 14 个被不同 fs_in 读的 PS 物化张量：物化 buffer 只有一份（s=max fs_in），
     较粗的消费者会经历二次舍入。这是交付物 README §6.2 已声明的性质，
     **不是驱动 bug**；阶段 3 的判据必须明确是"同一 int8 源"下的 C 内核。
2. **单实例串行**：E1E2 pack 与 conv 抢同一个 bin_unit。`prepare` 里启动 E1E2 会等
   上一层 conv 结束（RTL start 闸门），所以 `BIN_PACK=e1e2` 是"更快但不再重叠"，
   不等于报告里的 0.26 s 全流水。全流水需要 227 层 int8 全在 DDR 的 arena 流程。
3. **NaN**：float 路径 `NaN > adj = False`；`sat8(rint(NaN))` 在本机 numpy 2.2.6 变 0，
   若 t_q>0 会变 1。真实的 bin_net 激活没有 NaN；阶段 3 的 float→int8 量化显式把
   NaN 置 −128，保持与 C 内核浮点路径逐字节一致。
4. **旧比特流**：0x200 页在旧 RTL 里是保留页（写忽略、读 0）⇒ 必须先 probe 写 4 读回 4，
   否则 E1E2 会读到没被写的输出（静默错数据）。驱动在 `enable_e1e2()` 里硬校验。
5. **61 MB arena**：全量描述符的地址是绝对地址，驱动必须整块分配 + 平移；
   1 GB 板子的 CMA 是否够、`pynq.allocate` 能否给 61 MB 连续块，未上板验证。
6. **频率/带宽**：21,930,932 拍是计算下限；100 MHz = 219 ms，150 MHz = 146 ms。
   0.15 s 还要求 PL 持续读 ≥1.03 GB/s；DBG_E_RD/CYC 是唯一实测判据。
7. **grp8 变通**依赖哨兵 16 B；驱动若只 memcpy `thr.bin` 会静默错 9 层（cin=544）。
