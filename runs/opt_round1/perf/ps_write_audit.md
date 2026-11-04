# PS 侧 8 字节越界审计（2026-11-04，纯静态阅读，不碰板、不改生产代码）

**范围**：`bin_engine.py` / `bin_net.py` 的三个 PS 写者 + arena 槽布局。
**方法**：逐个写者读代码 + 用容器/几何算出实际长度与地址窗口（不跑网）。

---

## 0 一句结论

> **PS 侧不存在"能把 8 字节写进 OUT 金丝雀"的写者** —— 三处逐行核过：
> ① `push_input` 按 `len(src)` 写、② `fill_canary` 与 `check_canary` 的**范围完全一致**、
> ③ f32 反量化在板上写的是**独立的 CMA 池**、长度 `n` 两侧同一个式子。
>
> **但审计发现一个更重要的结构事实（这才是"偶发"的真正解释）**：
> OUT 槽固定 `max_out + 4096`，金丝雀在 `[max_out, max_out+4096)`，
> 而 `max_out = 1 572 864 B` **只有 3 个层**达到（`decoder.decoder_1.rdb.conv_layers.{0,1,2}.conv`，
> 96ch×128²），次大一档是 1 048 576 B（**空档 512 KB**）。
> ⇒ **227 层里有 224 层越界 8 B 落在空档里，永远抓不到**。所谓"偶发"＝"只有那 3 层能触发"，
> 不是随机性，也不指向 PS/PL 的责任划分。
>
> **建议先做两件零风险的护栏改进**（§4），再决定查谁：① 金丝雀紧跟 `out_bytes`（覆盖全部 227 层）；
> ② `check_canary` 报**偏移**（现在只报字节数，"头 8 还是尾 8"从代码上**无法确定**）。

---

## 1 ① `push_input`（`bin_engine.py:1620-1634`）

```python
1622  mv = np.frombuffer(in_buf, np.uint8)
1623  src = np.frombuffer(data, np.uint8)
1624  mv[:len(src)] = src          # ← 精确按 src 长度，无 ceil/无 padding
1625  _flush(in_buf)
```

| 检查项 | 结论 | 证据 |
|---|---|---|
| 写入长度 == 目标 band 分配长度？ | **是**（越界由 numpy 直接抛异常，不会静默） | `:1624`；上界另有显式护栏 `need_in > len(in_buf)` → raise（`:1525-1527`） |
| 有没有 `ceil`/`+padding` 取整后多写？ | **没有** | 全函数 15 行，只有 `mv[:len(src)] = src` 一处写 |
| 有没有"以 float 为单位算长度、按 int32 写"的混用？ | **没有**：两侧都是 **uint8 视图**（`np.frombuffer(..., np.uint8)`），单位天然一致 | `:1622-1623` |
| 上游载荷长度对不对？ | `act_u8(A)` 返回 `(cb,H,W)` uint64 的**零拷贝视图** ⇒ `cb*H*W*8` = `L.act_bytes`（`bin_ref_numpy.py:332-348`），`quant_pack`/`binarize_pack` 同形 | `bin_engine.py:2164`、`bin_ref_numpy.py:329` |

**⇒ 无问题。**（唯一可议之处：`push_input` 不做"src 是否属于本层"的语义校验，但长度是对的。）

---

## 2 ② `fill_canary` / `check_canary`（`bin_engine.py:203-220`）

```python
204  def fill_canary(buf):
205      mv = np.frombuffer(buf, np.uint8)
206      mv[-CANARY_BYTES:] = CANARY                 # 填 [len-4096, len)

209  def check_canary(buf, n_used, tag):
210      n = len(buf); start = n - CANARY_BYTES
211      if n_used > start: raise ValueError(...)     # 只防"装不下"
212      tail = np.frombuffer(buf, np.uint8)[start:]  # 查 [len-4096, len) —— 同一个区间
213      bad = int((tail != CANARY).sum())
214      if bad: raise RuntimeError("... 金丝雀被写坏 %d/%d 字节 ..." % (bad, len(tail)))
```

| 问题 | 答案 | 证据 |
|---|---|---|
| 填的范围 == 查的范围？ | **是**（都是 `[len-4096, len)`，长度都是 4096）。**填/查的配对**：非流水路径 `bin_engine.py:1549` 填 → `:1567` 查；**流水路径**在 `bin_net.py:1026`（`_PlBackend.prepare` 里、kick 之前）填 → `bin_engine.py:1813`（`reap`）查。两条路径都成对。 | `:206` vs `:212`；`bin_net.py:1026`、`bin_engine.py:1549/1567/1813` |
| 起点是 `out_bytes` 还是"四舍五入后的值"？ | **都不是** —— 起点是 **`len(buf)-4096`**，而 `len(buf)` = OUT 槽 = **`max_out+4096`**（`bin_net.py:809`：`arena.region(nout + 4096, ...)`）  | `bin_net.py:807-809` |
| ⇒ 意味着什么 | 金丝雀**只在"本层 `out_bytes == max_out`"时贴着数据**；224 层的 `out_bytes < max_out`，`[out_bytes, max_out)` 是**空档**（arena 初始化时整块填 `0xA5`，但 check 不看那里） | `bin_engine.py:411`（`self.mv[:] = CANARY`）、`:212` |
| "坏 8/4096" 是开头还是结尾 8 B？ | **代码上无法确定**：`:213` 只累加计数，**丢掉了位置**。但由 §0 的结构可以**推断**：能触发的只有那 3 个 `max_out` 层，而它们的 `out_bytes` 正好顶到 `start` ⇒ 越界 8 B 必然落在金丝雀**开头** `[start, start+8)` | `:211-213` |

**⇒ 填/查本身一致（无 bug），但"护栏的覆盖面"有缺陷**：227 层里只有 3 层受保护。
这解释了 `v60_vd_results.md` 里那句观察——"触发与否取决于该层输出缓冲尾部是否正好贴着金丝雀"。

---

## 3 ③ f32 反量化 `out=` 路径（`bin_engine.py:4272-4300` + `F32Pool`）

```python
2189  pa = int(getattr(out_buf, "physical_address", 0) or 0)
2201  if pool is not None and FAST_PS:
2202      cg = (int(L.cout) + C8 - 1) // C8
2203      nf = cg * C8 * int(L.Hout) * int(L.Wout)      # ← 补零通道也算进来
2204      lease = pool.acquire(nf * 4)
2205      y = from_c8_deq_into(raw, L.cout, L.Hout, L.Wout, fs, lease.view)

4286  cg = (C + C8 - 1) // C8
4287  n = cg * H * W * C8                              # ← 同一个式子
4288  if dst.size < n: return None                      # ← 装不下就回落，不硬写
4290  d = dst[:n].reshape(cg, C8, H, W)                 # ← 只写 dst[:n]
```

| 检查项 | 结论 | 证据 |
|---|---|---|
| 长度会不会多/少 2 个元素？ | **不会**：`acquire(nf*4)` 与 `from_c8_deq_into` 的 `n` 是**同一个表达式**（`cg*C8*H*W`），且 `dst[:n]` 再切一刀（多余的空间根本不碰） | `:2203` ≡ `:4287`、`:4290` |
| `np.frombuffer(..., count=...)` / `reshape` / `[:n]` 有没有 `[:n+2]` 一类？ | **没有**：`read_out` 的 `raw = np.frombuffer(out_buf, np.uint8)[:L.out_bytes]` 只是**读**（`:2199`） | `:2199` |
| 池的分配/释放配对？ | `F32Pool.acquire` 只给"**≥ nbytes 且空闲**"的槽，否则返回 `None`（调用方回落 numpy）⇒ 不存在"切一个不够长的槽" | `:543-554` |
| **它会不会写到 OUT 金丝雀？** | **板上不会**：`BIN_F32POOL=1` 时池是**独立的 CMA 块**（`bin_engine.py:1038`：`[BinArena(nb, name="f32slot%d")]`），与 arena 不重叠；`BIN_F32POOL` 默认**关** ⇒ 走 `np.empty`。 | `bin_engine.py:1027-1044`、`bin_net.py:3218` |

**⇒ 无问题**（板上）。**一个只影响主机自检的隐患**：无 pynq 时 `enable_f32_pool(arena)` 会从**同一个
FakeArena** 里切池（`bin_engine.py:1045-1047`），此时"池越界"会表现为"OUT 金丝雀被写坏"——
**主机上的假阳性**，别拿它推理板端。已在 `_PlBackend` 里确认板上走的是独立 CMA。

---

## 4 arena 槽布局（`bin_net.py:806-812` + `BinArena.region`，`bin_engine.py:451-461`）

```python
806   for i in range(self.nbuf):  b,_ = arena.region(nin,        align=64, name="IN%d")
807   for i in range(self.nbuf):  b,_ = arena.region(nout+4096,  align=64, name="OUT%d")
```
`BinArena.region`：`off = align_up(self._next, 64)`，`self._next = off + n` ⇒ **子区之间只有 0~63 B 的
对齐填充，没有保护带**。

代入 256² 实测几何（`pack_v60`，227 个 conv 层）：

| 量 | 值 | 出处 |
|---|---|---|
| `max_in` | **1 179 648 B**（=`cb9×128²×8`，544ch 层） | 由 `arena 66.2 MB` 反解：`66 224 128 − 24×(1 572 864+4096) − 65 536 = 24×1 179 648` ✓ |
| `max_out` | **1 572 864 B**（96ch×128²，**3 层并列**） | 本机建图算得 |
| `nbuf` | 24（`BIN_LAZY_SLOTS=24`） | `bin_net.py:3171-3176` |
| 老人区 `1<<16` | 65 536 B（desc/scratch） | `bin_net.py:3203` |
| **总 arena** | **66 224 128 B = 66.2 MB** | 与板端日志 `[init] arena 66.2 MB` 逐字节吻合 ✓ |

**槽布局（连续、无保护带）**：

```
+0            +1 179 648      +2 359 296 …            +28 311 552
[  IN0  ] [  IN1  ] … [IN23]  ← 24 × 1 179 648 = 28 311 552
                              +28 311 552
[ OUT0 : 1 572 864 数据 | 4096 金丝雀 ]
[ OUT1 : 1 572 864 数据 | 4096 金丝雀 ]  ← 紧贴 OUT0 的金丝雀末尾
…
[ OUT23 ]
                                        +65 536 scratch
```

| 相邻关系 | 后果 |
|---|---|
| `IN_i` 的末尾 **紧贴** `IN_{i+1}` 的开头 | 任何 IN 侧多写 8 B ⇒ **静默改写下一槽的输入**（不报错！）—— 比金丝雀更隐蔽 |
| `OUT_i` 的金丝雀末尾 **紧贴** `OUT_{i+1}` 的数据开头 | PL/PS 若把 OUT_i 多写 >4096 B ⇒ 直接踩到 OUT_{i+1} 的**数据**（金丝雀会先报 8/4096 之外更多） |
| 最后一槽 `OUT23` 金丝雀后面是 64 KB scratch | scratch 若被 desc 写 ⇒ 不碰金丝雀 |

⇒ **不存在"子区重叠 by 8"**（对齐只**向上**取整，`_next = off+n` 不回收）。所以"偶发 +8"**不是**
布局算法造成的。

---

## 5 最小修复（**patch 建议，未改生产代码**）

### 5.1 让金丝雀覆盖全部 227 层（1 处函数 + 2 处调用，约 6 行）

```python
# bin_engine.py:203
-def fill_canary(buf):
-    np.frombuffer(buf, np.uint8)[-CANARY_BYTES:] = CANARY
+def fill_canary(buf, n_used=None):
+    """n_used 给了 ⇒ 金丝雀**紧跟**本层 out_bytes（覆盖每一层）；不给 ⇒ 老行为（槽尾）。"""
+    mv = np.frombuffer(buf, np.uint8)
+    start = (len(mv) - CANARY_BYTES) if n_used is None else int(n_used)
+    if start + CANARY_BYTES > len(mv):
+        start = len(mv) - CANARY_BYTES
+    mv[start:start + CANARY_BYTES] = CANARY

# 两处 fill_canary：bin_engine.py:1549（非流水 run）+ bin_net.py:1026（流水 prepare）
# 对应两处 check_canary：bin_engine.py:1567 / :1813（reap）
-            BinGuard.fill_canary(out_buf)
+            BinGuard.fill_canary(out_buf, need_out)
# check_canary 同样要按 n_used 起算：
-def check_canary(buf, n_used, tag):
-    start = len(buf) - CANARY_BYTES
+def check_canary(buf, n_used, tag):
+    start = int(n_used) if n_used + CANARY_BYTES <= len(buf) else len(buf) - CANARY_BYTES
```
* 代价：每层多写 4 KB + 比 4 KB ⇒ 227×8 KB ≈ **1.8 MB 内存流量/帧**，可忽略。
* 落点要**两处都改**：`bin_engine.py:1549`（`run`）与 **`bin_net.py:1026`**（`_PlBackend.prepare`，流水路径），
  否则流水路径还停在"只保护 3 层"的老行为。
* 风险：**零数值影响**（只在 `need_out` 之后写/比填充字节）。若某层 `out_bytes+4096 > len(slot)`，
  自动回落老行为 ⇒ 对现有调用完全兼容。
* 收益：**224 个此前无保护的层立刻进入保护**；这也是把"偶发"变成"必现/可定位"的唯一办法。

### 5.2 让报错能定位（3 行）

```python
# bin_engine.py:213
         bad = int((tail != CANARY).sum())
         if bad:
+            offs = np.flatnonzero(tail != CANARY)[:16]
             raise RuntimeError("%s: 金丝雀被写坏 %d/%d 字节（首坏偏移 +%d，前 16 个：%r）"
-                               % (tag, bad, len(tail)))
+                               % (tag, bad, len(tail), int(offs[0]), offs.tolist()))
```
有了 `offs[0]`：`+0` ⇒ 正好贴着 `out_bytes`（越界 8 B）；`+4088` ⇒ 越界的是**槽尾之后**的新写者
（例如写到 arena 尾部的 desc/scratch）。

### 5.3（可选）给 IN/OUT 子区之间留 64 B 保护带

`arena.region(n + 64)`，但**别把多出的 64 B 算进 region 登记**（`add_region` 用 `len(buf)`）——
否则护栏白名单会把保护带也算成合法写区，反而取消保护。收益有限，建议先做 5.1。

---

## 6 给你的一句话（写进日报）

* **"PS 侧是否存在能产生偶发 +8 字节的写者"** → **否**（三处逐行排除：`push_input` 按 `len(src)`、
  金丝雀填/查同区间、反量化写独立 CMA 池且长度同一式子）。
* **但"偶发"已经有确定解释**：金丝雀只覆盖 `max_out=1 572 864 B` 的 **3 个层**，其余 224 层的
  +8 落在 **512 KB 空档**里 ⇒ 报警与否取决于"这次跑的是哪一层"，不是谁随机越界。
* **下一步**：先落 §5.1+§5.2（10 行、零风险），再复跑同一张图 —— 若那时 224 层里也报警，
  就能**精确定位**到层号与偏移；若只有那 3 层报警，说明越界来自"只在这 3 层出现的路径"（96ch/128² 的
  写通道或 DESC 收尾），再回 RTL 对那 3 个形状单独做仿真。
