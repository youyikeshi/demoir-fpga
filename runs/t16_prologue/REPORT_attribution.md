# T-16 第一阶段：PS `prologue` 1.595 s 归因（离线，板子断电）

- 日期：2026-11-02 02:0x（基线 `baseline-v3` = `c0e9afe`，位流 `602725b8`，150 MHz，24 槽，256²）
- 副本：`worktrees/T-16-prologue/`（自证：`ps/pynq/*.py` 与 tag `baseline-v3` 逐文件 md5 全同；
  `verify_lazy_net.py --hw 64` = **PASS**，sha1 `104710f69c4d / 2ac6efec2325 / 8ab595fcf7cd`）
  ⚠ 顺带发现（属基线卫生，不属 T-16）：`ps/tools/verify_lazy_net.py`、`verify_e1e2_path.py`、
  `verify_e1e2_direct.py` **都不在 tag `baseline-v3` 里**（untracked 工作树文件）⇒ 判据脚本本身没被冻结点覆盖，
  后人 `git archive` 出来的树跑不了这条判据。建议 T-30 一并入库（只入库，不改内容）。
- 探针：`runs/t16_prologue/probe/`（`bin_net_instr.py` = 原 `bin_net.py` **只加计时行**，逐行 diff 见 §7；
  其余为 `mk_instr.py`（生成器）/`probe_v2.py`（v2，正文数字来源）/`probe_attrib.py`（v1）/`micro.py`/`static_counts2.py`/`attrib_v2_256.json`/`diff_bin_net.txt`）
- **本报告不含任何板端新数据**：板端数字全部来自已归档的真板 JSON（`runs/board_ops/`），
  主机数字一律标注为「host」，且只用来取**结构计数**与**比例**。

## 0. `prologue` 的口径（必须先写在前面，含一条口径 bug）

$$\texttt{prologue}=\sum_{\text{227 层}}\max\bigl(0,\ dt-\texttt{prep\_wait}\bigr),\qquad dt=\text{包住 }\texttt{be.prepare(op)}\text{ 的墙钟}$$

- `bin_net.py:1840/1869`：`t_pro += max(0, dt − prep_wait)`；`bin_engine.py:2178 prepare_segments()` 是主入口
  （段表 → 阈值/描述符 + f32 段量化落进 vin）。**不含 kick 之后的等待**（那部分是 `wait`/`overlap`）。
- ⚠ **`prep_wait` 是死字段**：全仓 `prep_wait` 只有 `_Task.__init__` 里的 `= 0.0` 与两处读取，
  **从来没有任何一行给它赋过非零值**（`grep -n prep_wait bin_net.py` ⇒ 4 处，全是 init/read）。
  $$\Rightarrow\ \texttt{prologue}\equiv\sum dt\ (\text{prepare 全墙钟})\ \Rightarrow\ \text{其中含 }27\text{ 层 }\texttt{pack\_input}\text{ 里\textbf{同步跑的 PL }E1E2}$$
  $$\Rightarrow\ \text{那部分 PL 时间既算进 PS 的 prologue、又不在 }pl\_s\text{ 里} \Rightarrow\ \text{账上 PL 少计（本报告 §5 定量：}\approx61\text{ ms）}$$
- 另一条口径事实（影响 §6）：`board_v39_verify.run_net()` **每次 run 都新建 `BinConvEngine`+`BinNet`**
  （`board_v39_verify.py:295-297`）⇒ `enable_e1e2()` 的描述符表装载、`_pack_views()` 的 698 条命令扫描、
  E1E2 `_pbuf` 的 CMA 分配**都是每帧付费**；只有 66 MB arena 是复用（`arena.reset()`，`run(..., arena=arena)`）。

## 1. 归因表（板端投影；桶互不重叠，桶和 = 1 595.0 ms vs 实测 1 595.5 ms，**−0.03%**）

| # | 桶 | ms | 占 | 板端投影依据 | 置信度 |
|---|---|---:|---:|---|---|
| 1 | **q8**：f32 段量化+C8 布局（33.5 M elem / 134 MB，122 段） | **604.0** | 37.9% | **板端直测** `ops_stats_p1.json: e1e2.q8`；独立校验：$134\,\text{MB}/0.604=222$ MB/s = §43.4 的 q8_c8 板端 232 MB/s（差 4.3%） | 高 |
| 2 | **回落层 pack_input**（27 层：31 MB f32→C8 + 1.13 MB 位图 PL 打包 + push） | **257.5** | 16.1% | **板端直测** `cat.pack_s.single_f32`；$31\,\text{MB}/0.2575=120$ MB/s，落在 §42.2 的 blocked 122–134 MB/s 邻域 | 高 |
| 3 | **cfg**：`guard_layer`+`config`+`set_bases`（227 层） | **≈350** | 21.9% | 残差法 + 双路交叉：① host 实测 **2 861 次 MMIO 写**（确定性）× 板端 30.37 µs = **86.9 ms**；② perf_probe 板端 `config+set_bases` = **1.35 ms/层**（15 写）+ `guard_layer` ≈1.6 ms/层 × 227 = 363 ms | 中高 |
| 4 | **desc**：阈值 + 描述符组装（200 层，表已在构建期算好） | **85.5** | 5.4% | **板端直测** `e1e2.desc` | 高 |
| 5 | **acq**：挑槽 4.6 + **8 次强制物化 87.4**（15.4 MB c8deq = 176 MB/s） | **92.0** | 5.8% | **板端直测** `acq`（`n=227, starved=8`） | 高 |
| 6 | **views**：`_pack_views`（首调扫描 698 条命令 + pbuf 分配 + 每层 1 次调用） | **61.3** | 3.8% | **板端直测** `e1e2.views`；首调单价经 host 定标：698 cmd 板端 11.9 ms（源码注释实测）⇒ 剩 ~50 ms 为每层摊销+分配 | 高 |
| 7 | **段表 glue**：`_segments()`+class 判定+`plan` 选取+`_unpin`（200 层） | **81.8** | 5.1% | 板端派生：`pack_s(cat+i8 类) 886.0 − prepare_segments 804.2` | 中高 |
| 8 | **prepare_segments 入口**：`t_q` 取用 + `_seg_layout`（200 层） | **42.9** | 2.7% | **板端直测** `descviews − views` | 高 |
| 9 | canary 填充（227 × 4 KB `np.frombuffer`+切片写） | ≈9 | 0.6% | host 1.0 ms × 28（见 §2 比率） | 中 |
| 10 | `push_input`（27 层 1.13 MB memcpy + 27 次 flush） | ≈8 | 0.5% | $1.13/0.30\text{ GB/s}+27\times0.15$ ms（板端 memcpy 300 MB/s、flush 0.15 ms，§perf114x） | 中 |
| 11 | `_Task`/tail/计时器本身 | ≈3 | 0.2% | host 比例 | 中 |
| **Σ** | | **1 595.0** | 100% | 对上 1 595.5 ms | |

**必须显式写出的残差**：桶 3（cfg 350 ms）是**唯一的派生桶**（其余 10 桶都是板端直测或被板端直测夹住）。
它由「1 595.5 − 其余 10 桶」唯一确定，所以 §1 的闭合是**恒等式**，残差 0.5 ms 不代表独立验证。
对 cfg 的**独立**验证只有上面两条（86.9 ms 的 MMIO 侧 + 363 ms 的 perf_probe 侧），二者与残差 350 ms 相差 ≤4%。

## 2. host→board 比率是怎么来的（不许拿主机时间当板端时间）

三个比率，各自来自**同一段代码路径**的板端实测：

| 比率 | 板端锚点 | host 实测（本机 Ultra 7 155H / Python 3.10.12） | 比率 |
|---|---|---|---:|
| **纯 Python + dict + `int()`**（R_py） | `_pack_views` 的 698 条命令扫描 = **11.9 ms**（源码注释，板端实测） | 首调 0.430 ms（0.62 µs/cmd） | **≈28×**（区间 26–34） |
| 同上，第二锚点 | `config+set_bases` = 1.35 ms/层（15 写 ⇒ 456 µs MMIO + 894 µs Python） | 60 µs/层（host，含 FakeMMIO） | 26× |
| **量化+转置**（数据搬运） | q8_c8 **232 MB/s**（§43.4） | q8 桶 121.5 ms / 28.8 M elem | 4.3× |
| **memcpy / memset（PYNQ 缓冲）** | **300 MB/s**（perf114x，1 MiB 平均 208/302/302） | 51 GB/s（cache 热） | ≈170×（不可用，只用于板端侧算） |

⇒ **没有一个"统一比率"**：数据类桶（1、2）差 4×，Python 类桶（3、6、7、8、9）差 26–34×。
因此本报告一律**逐桶用板端锚点**，不用全局比率；无法定标的桶（§6 的 gap 子项）给区间并标注假设。

## 3. 结构性计数（不是墙钟推理，全部可复算）

| 计数 | 值 | 来源 |
|---|---:|---|
| conv 层 / PS op / 总 op | 227 / 168 / 395 | `build_graph` |
| prepare 路径分布 | **cat 类 110 + i8 直读 90 + f32 回落 27 = 227** | 静态分类（`_needs_f32`+producer.kind）与板端 `direct_layers=200`、`pack_s` 分类**一致** |
| prepare_segments 调用 / f32 段数 | 200 / 122（板端 `q8n=122`） | `e1e2_prof` |
| f32 段元素（板端） | 33,497,088（= q8b 133.99 MB / 4） | `lazy.f32_bytes` |
| 回落层 f32 元素 / push 字节 | 7.75 M / **1.13 MB** | 静态（27 层 `cb·HW·8` 求和） |
| `arm` 的实际 MMIO 写 | **2 861 次 / 227 层 = 12.6 次/层**（22 次尝试，影子寄存器跳 5 881） | host 计数（决定性，与板端同码） |
| `kick` 的 start 写 / E1E2 job 写 | 227 / 2 835 | host 计数（板端总 6 027，host 5 927，差 1.7% 来自 8 次 starve 的额外组） |
| 227 层位图总量 | 19.07 MB（直读 17.94 + 回落 1.13） | 静态 |
| 循环迭代 / poll 次数 | 622 / 227（host FakePL 即时完成；板端真实 poll 次数未记） | 打点 |

## 4. 那 0.573 s（`PS+PL−overlap+wait` 之外的缺口）

$$\textbf{6.489} - \underbrace{(3.819+2.222+0.627-0.752)}_{\text{5.916}} = \mathbf{0.573\ s}$$

结论：**它不是漏掉的算子，是「每帧重做一次、只由形状/地址决定的装配 + 循环簿记」**。
枚举 `run()` 内所有不进任何桶的代码（`bin_net_instr.py` 的 6 个 mark + 10 个包装），host 实测如下：

| 子项 | host ms | 板端投影 ms | 依据 |
|---|---:|---:|---|
| `build_graph`（395 op / 396 张量） | 2.72 | 76 (70–95) | 纯 Python ×28 |
| `enable_e1e2`（描述符表装载 + probe） | 11.24 | 62 (45–110) | JSON+numpy，比率 4–10×（**假设**） |
| `_analyze_lazy`：纯 Python 部分 | 0.72 | 21 (19–25) | ×28 |
| `_analyze_lazy`：`_build_lane_plans`×2 | 16.17 | 97 (80–160) | numpy 小数组，比率 5–10×（**假设**） |
| 装配（`_PlBackend` 48 个 region / 队列 / `f32pool_plan`） | 1.4 | 35 (28–42) | ×25 |
| 循环簿记（622 次迭代 + poll 25 µs + heap/release） | ~1 | 40 (35–55) | 板端单位成本 |
| 循环后 stats 汇总 | 2.49 | 70 (65–83) | ×28 |
| 输出物化 / 计时器 `perf_counter` 本身 | ~0 | ≈0 | — |
| **模型合计** | 36.5 | **401 (342–570)** | 加总 |
| 实测缺口 | — | **573** | 板端 |
| **未归因残差** | — | **+3 … +231（中值 +172）** | **显式写出，不藏** |

- 反解：$573/36.5=15.7\times$ —— 落在「纯 Python 28×、numpy 小数组 5–10×」的加权区间内（本块 mix 恰在两者之间），
  故**缺口完全可解释**；残差方向是「模型偏小」，不构成"还有第二个隐藏大项"的证据。
- 交叉检查：两个臂的缺口不一致（numpy 臂 0.436 s vs C 臂 0.573 s，差 0.137 s），
  而两臂的装配代码逐字相同 ⇒ 0.137 s ≈ 板端 run-to-run 抖动（§43.5 记 5–6%）+ 比率假设的不确定度，
  ⇒ **任何 run 级归因的固有误差 ≈±0.15 s**，这也正好是上表残差的量级。
- **强烈建议**（板子回来后第一件事）：把 `bin_net_instr.py` 的 6 个 mark 原样搬进一次板端 run，
  这 0.573 s 就能从「模型 0.40–0.57 s」变成直测（成本：一次上板，不改任何语义）。

## 5. 顺带发现的两个口径问题（会影响后续计划）

1. **prologue 里含 PL 时间 ≈61 ms**：27 层 `pack_input` 的 E1E2 打包是同步跑的；按板端位图速率
   $17.94\,\text{MB}/0.970=18.5$ MB/s 推，27 层的 1.13 MB ⇒ **61 ms**。
   $$\Rightarrow\ \text{真 PS prologue}\approx1.53\ \text{s};\quad pl\_s\ \text{少计同量} \Rightarrow \text{两者都不该按"纯"读}$$
2. **`t_pl` 未开 `NET_TPOLL`**（板端 env 里没有）⇒ `pl_s` 含 PS 轮询延迟，`−overlap` 只是近似抵消
   （§58.1 已量过同一现象：TPOLL=1 时 PL 3.32→1.71）。T-19 用 PL 2.222 s 做预算时要注意这一项。

## 6. 最便宜的 3 个改动（板端投影；按"秒/风险比"排序）

$$\textbf{核心洞察：227 层的 cfg+desc+视图+段表全部只由 (形状, 槽地址, 权重) 决定，与数据无关}$$
$$\Rightarrow\ \text{现在每帧用 Python 重算一遍} \Rightarrow\ \text{桶 3+4+6+7+8}\approx621\ \text{ms 里，只有 }\mathbf{86.9\ ms}\ \text{是 MMIO 本体的硬底}$$

| 序 | 改动 | 从 | 到 | **省 ms（板端投影）** | 置信度 | 风险 |
|---|---|---|---:|---:|---:|---|---|
| **A** | **把「层 × 槽」的配置+描述符+视图+段表预计算成一张表**（构建期一次，帧内只查表 + 12.6 次真写） | 621 | ~120 | **−500** | **中高**：86.9 ms 是 30.37 µs×2861 的算术硬底；其余 534 ms 是 Python 组装（host 60 µs/层×227 与残差 350 ms 双路吻合） | 地址陈旧性 ⇒ 键必须含 `(layer, in_pa, out_pa, thr_pa, vdesc_pa, 段 pa 列表)`；T-05 前车之鉴：只做 desc/views **上限只有 0.1 s**，必须**连 cfg 一起做**才有 0.5 s |
| **B** | **q8 缓存复活（T-08）**：f32 叶子量化结果按 `(tensor 内容哈希, fs)` 复用 | 604 | 288–343 | **−261 … −316** | **中**：T-08 已给该投影，但五条加固未验完、板上未测 | 陈旧性/`(tensor,fs)` 多条目/fill 直写 arena；必须配负例 |
| **C** | **27 层回落路改走 `q8_c8` 融合 + 直写 IN**（现走 `quantize_int8`+`to_c8_padded` 120 MB/s，改走 q8_c8 232 MB/s，且省 plc 打包与 1.13 MB 拷贝） | 257.5 | ~135 | **−120** | **中高**：两个速率都是板端实测（§43.4 / §42.2），语义等价性可逐字节验 | 逐字节（`sat8(rint(x·2^fs))` 同值）；需处理"f32 段落在 C8 组边界"的既有约束 |
| Σ | A+B+C（B 取乐观端） | 1 595 | **≈640–700** | **−900 … −950** | | |

$$\Rightarrow\ \textbf{A+B+C 到不了 ≤550：还差 ~90–150 ms} \Rightarrow \text{两条后手（本阶段不实施，只登记）}$$
- **D. acq 的 8 次强制物化 87.4 ms**：`starved=8`（24 槽在飞时被钉满）⇒ 改 pin 淘汰策略或加槽，**−87 ms**，
  但 25 槽起 CMA 就没余量（现 66 MB arena / `CmaFree` 0.5 MB），必须先证容量。
- **E. MMIO 单次 30.37 µs 本身不合理**（AXI-Lite 一次写 ~0.2 µs 级）：若 driver 用 C/`/dev/mem` 直写，
  **A 的 86.9 ms 硬底可再降 ~80 ms**，且对**所有**桶都生效（E1E2 job 的 2 835 次写也在里面）。
  ⇒ 这一刀比 D 更值，但属驱动改动，需要单独计划与更严的门禁（写序/屏障）。

**如果允许把改动放到 prologue 之外**（不计入 A/B/C 的 1 595 目标）：§4 的 0.573 s 里 **100% 是形状决定的**
⇒ 加一条「帧间装配缓存（同 H/W 第二帧起复用 build_graph/lexer 计划/描述符表/pbuf）」，**再省 0.40–0.55 s**，
按风险比它其实是全项目最便宜的一刀（判据：同进程第 2 帧数字应与第 1 帧一致）。

## 7. 复现与自证

```
# 副本自证（已跑）
cd worktrees/T-16-prologue/pynq_specialized_code
md5sum ps/pynq/bin_engine.py ps/pynq/bin_net.py     # eec568b2… / 17d072ab…（= 台账）
python3 ps/tools/verify_lazy_net.py --hw 64         # PASS，A=B=C=104710f69c4d/2ac6efec2325/8ab595fcf7cd
# 归因探针（离线，FakePL，hw=256，约 33 s）
python3 runs/t16_prologue/probe/probe_v2.py         # → out/attrib_v2_256.json
```
- `probe/bin_net_instr.py` 与 `baseline-v3` 的 `bin_net.py` 的差异：**只有 8 处插入的计时行 + 1 行
  `PROBE_OUT = {}`**（用 `mk_instr.py` 生成，每处 `s.count(old)==1` 断言；diff 见本文件同目录
  `probe/diff_bin_net.txt`），**没有任何行为改动**。
- 主机 hw=256 的 FakePL sha1 = `392d1ae02761 / 7f68fb00d2b5 / aa03751a2d1c`，**等于冻结的主机 256² 常量**
  ⇒ 被计时的那条路径就是判据路径。
- 第 72 条纪律：本次**没有**设 `OMP/OPENBLAS/MKL_NUM_THREADS`，故对照的是默认线程口径的冻结常量；
  第二阶段的 A/B/C 会跑两种口径（`OMP=1` 与默认），判据是"同口径内 A=B=C"。

## 8. 明确的"没做到"

1. **没有板端新数据**（板子断电）。桶 1/2/4/5/6/8 是板端直测（来自既有归档），桶 3 是残差+双路交叉，
   桶 9/10/11 是 host 定标——**这三档的可信度不同，已在表里逐行标注**。
2. `prep_wait` 死字段这件事，**没有改代码**（本阶段只归因；改动要等"继续"）。
3. §6 的 A 需要新的失效门禁设计（负例），本阶段只给了键的构成，**没有实现**。
