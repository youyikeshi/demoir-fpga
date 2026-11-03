# v58 真结果上屏 · 第一步：去摩尔纹单独跑通（HDMI 停在彩条）

**时间**：主机 2026-11-03 19:24–19:27（板钟 `2025-05-09 19:01`，板子 RTC 不准，勿用板钟对时）
**板**：`192.168.7.2` / PYNQ 3.1.0 / Linux 6.12.40 armv7l / uptime 1966.7 s
**位流**：`design_1_v58.bit` md5 `555c22cf29a1482f8d6050645190fe92`（3,510,798 B）+ `.hwh` `7037857451f5ba35020977a253e106a1`
⇒ **`Overlay(download=False)` 接手**板端 `/tmp/probe_v58_v58a/design_1_v58.bit`（本 session **0 次**真 load）
**PS 代码**：`runs/ACCEPT_v57/05_PS/` 冻结闭包（20 文件，14 `.py` + 6 `.c`），逐文件 md5 与主机相同
**探针**：`/tmp/probe_v58_real_r1/realdemo_step1.py`（md5 `08ab3560285550ab1abef75e278713c7`，本目录同名副本）

$$\Rightarrow\ \textbf{第一步结论：通过。三路 sha1 与 v57 golden 逐字符相同，真值时钟 50.00 MHz。}$$

---

## 1. 判据（逐条给数字）

| # | 判据 | 实测 | 结论 |
|---|---|---|---|
| 1 | 板端 PS 文件 md5 与主机逐一相同 | **21 / 21**（20 闭包 + 探针），见 §2 | ✅ |
| 2 | `FCLK0_CTRL` 解出真值 = 50 MHz（写 75） | `0x00101400` ⇒ DIV0=20 / DIV1=1 / product=20 ⇒ **1000/20 = 50.00 MHz** | ✅ |
| 3 | 去摩尔纹输出 sha1 与 v57 golden 逐字符相同 | **out1/out2/out3 = 3 / 3**，见 §3 | ✅ |
| 4 | 读一次 `0x2C`（彩条模式，扫描器关） | 跑前 **0**、跑后 **0**（仅记录，**无判别力**：扫描器本就没开） | 记录 |
| 5 | 无 ssh 超时 / 无 core dump | 6 次连接全部 rc=0；`ulimit -c 0` 由 `boardlock.sh` 统一设 | ✅ |
| 6 | 位流未二次 load | `download=False`，`fpga0` 全程 `operating`（跑前=跑后） | ✅ |

**耗时（真值 50 MHz 运行点）**

| 项 | 秒 |
|---|---|
| `Overlay(download=False)` | 1.43 |
| `BinNet(H=W=256)` 整网（227 层二值卷积 + 4 fp + 482 op） | **19.41** |
| 　其中 PL 段（Σ 每层 done，硬件计） | 5.24 |
| 　其中 PS 段 = prologue 7.44 + epilogue 4.20 + 非卷积 5.91 | 17.55（重叠 4.38）|
| 纯等 done | 4.37 |

---

## 2. 板端 PS 文件 md5（与 `runs/ACCEPT_v57/05_PS/deps_closure.txt` 逐条对齐）

| 文件 | 板端 = 主机 md5 |
|---|---|
| `bin_net.py` | `0990948de490eb6ba6d4aaefbcafa355` |
| `bin_engine.py` | `2fd3d0990c47151fe3bc06544b8adb64` |
| `bin_ref_numpy.py` | `337fbe3a354e3939b60d03c39b909610` |
| `qcache.py` | `95636f8cad44ca2434e978468a70b38e` |
| `fconv.py` | `67ea5cbb5c41c2e478d7c033800fde57` |
| `board_safe.py` | `d4d62fd06f74c963b643cec59566bf54` |
| `mmio_batch.py` / `.c` | `fe1bacff0fd4ca66da2c932725700772` / `a51ec90452d80597da85564e41039362` |
| `catpack.py` | `30149d936c9eea6cd510b200d1cd6d5c` |
| `e1e2_sim.py` | `2c4a6180fb74f88abb5e13b8e641b489` |
| `bpack.py` / `.c` | `63d6a9368827d36f81f64bc5f62c864b` / `2a8e6c553cb3dec95460223fa800a0f6` |
| `c8deq.py` / `.c` | `41c61d97ddd266655a02e86855ce28de` / `8cd77f1c4290b81d3b1d74d971390ac7` |
| `q8.py` / `.c` | `e794a43dcb8ce314f323a9b07c4274a6` / `c744794c2dafc032ddc4dde3c05adb5b` |
| `bilinear_c.py` / `.c` | `48134b7b0056ea71be77a965f0adf633` / `1cd97282ee30a092425602c0d09ef7c1` |
| `convpf_c.py` / `.c` | `e251b047f9078c9c18c76fbf78c1f61e` / `10b31d43578355887ae2cbbcc45d3704` |
| `realdemo_step1.py`（探针） | `08ab3560285550ab1abef75e278713c7` |

板端运行时内核全走 C（**无静默回落**）：
```
[DEPS] q8=fast c8deq=fast bpack=fast e1e2=fast catpack=fast safe=fast  ⇒ deps_closure_ok=True
```
权重容器：板上 `/home/xilinx/demoir/deploy/bin_weights_new/bin_weights.bin`
sha256 `9277bbf4…953ac0`（= 主机同一份），1,125,152 B → DDR `pa=0x30100000`。

---

## 3. sha1 对拍（golden = v57 验收冻结值，来自 `docs/baselines.md` v4 行 / `runs/board_150v22/board_out/net256_v22.log`）

| 输出 | shape | golden（v57 冻结） | 本轮 v58 实测 | 判定 |
|---|---|---|---|---|
| `x0`（输入，seed=1） | 3×256×256 | `cfc75bc3f15d306b07787325fdc9ae715cfadd0a` | `cfc75bc3f15d306b07787325fdc9ae715cfadd0a` | ✅ |
| `out1` | 3×256×256 | `9b1940c8955be53ccffd7905e333deffcea31f0a` | `9b1940c8955be53ccffd7905e333deffcea31f0a` | ✅ |
| `out2` | 3×128×128 | `f86a7035621bed133374a1bef42a27db204e7019` | `f86a7035621bed133374a1bef42a27db204e7019` | ✅ |
| `out3` | 3×64×64 | `025757a83c3993c8f592e31219b6a97906020363` | `025757a83c3993c8f592e31219b6a97906020363` | ✅ |

统计量也与 v57 逐位一致：`out1 min=0.1269 max=0.6003 mean=0.3760`、`out2 0.5665/1.3644/0.8659`、`out3 0.2224/0.7953/0.5534`。

**落盘复核**：`out1_256_seed1.npy` 从板子取回后在主机重算 sha1 = `9b1940c8…`（传输无损），即第二步可直接当作 `proc` 用。
⇒ **合并进 v58 没有改变去摩尔纹的数值行为**（这是我最初最担心的失败模式，判据已排除）。

---

## 4. 时钟：写 75 ⇒ 真值 50.00 MHz

```
[clk] 报告 125.0 -> 75.0 ; FCLK0_CTRL=0x00101400 DIV0=20 DIV1=1 prod=20 真值=50.00 MHz
```
| 量 | 值 |
|---|---|
| `Clocks.fclk0_mhz` 写值 | **75**（保持 v57 验收口径，未擅自改动）|
| 读回（PYNQ 口径） | 75.0 |
| `SLCR 0xF8000170` | `0x00101400` |
| DIV0 = `[13:8]` | 20 |
| DIV1 = `[25:20]` | 1 |
| `f_true = 1000 / (DIV0×DIV1)` | **50.00 MHz** |

⇒ 与 `README` 口径表一致（写 75 ⇔ product 20 ⇔ 50 MHz），与"报告值 = 真值 × 1.5"自洽（75 / 1.5 = 50）。
**HDMI 像素时钟不受影响**：`clocks.rpt` 显示 `clk_pix_*` 由 `clk_50m (Y14)` 经 MMCM M=23.75 生成，与 FCLK0 无依赖；故第二步把 FCLK0 压到 50 MHz 不会破坏 720p60 出图。

---

## 5. HDMI 寄存器（全程彩条，`CTRL=0x15`）

| 时刻 | CTRL | STATUS | VMODE | FRAME_ID | LATCHED | **0x2C** | PIX_HB |
|---|---|---|---|---|---|---|---|
| 整网跑前 | `0x15` | `0x03` | 0 | 3 | 3 | **0** | `0x03` |
| 整网跑后 | `0x15` | `0x03` | 0 | 3 | 3 | **0** | `0x02` |

⚠ **`0x2C=0` 这一段不许当带宽证据**：`CTRL=0x15` 含 `test_pattern=1`，RTL `cfg_enable = ctrl_enable && !ctrl_test` ⇒ 扫描器根本没跑。仅作"跑整网前后 HDMI 状态未被打扰"的记录（`FRAME_ID/LATCHED` 未变、`init_done` 仍 1、`fpga0=operating`）。

---

## 6. 运行点与开关（本轮的准确口径）

```
BIN_BILINEAR_C=1 BIN_PRECOMP=1 Q8CACHE=1 BIN_F32DIRECT=1 BIN_EPI_NOINV=1 BV_FREE=1
# 未开：BIN_PACK=e1e2 / BIN_LAZY_DEQ / BIN_CAT_FUSION（原因见 §7）
```
$$BV\_FREE=1\ \text{是 } 256^2\ \text{必需（张量总量 726 MB > 可用 880 MB）}$$
CMA 口径记录（**只记录，不作判据**）：分配前 `CmaFree=368 kB` → 跑完 `4576 kB`；
本轮 256² 整网（多次 MB 级 `allocate`）**一次成功、零重试** ⇒ 再次印证"分配前 `CmaFree` 不能当判据"。

---

## 7. ★ 本轮踩到的坑（留给第二步/下一位）

1. **`BIN_PACK=e1e2` 在"只有闭包"的部署上会硬失败，且 `bin_net` 的"回落"是空头承诺**
   - 现象：`⚠ BIN_PACK=e1e2 但 E1E2 启用失败（找不到 E1E2 描述符目录）⇒ 回落 PS 打包` 之后，
     `net.run()` 仍在 `prepare → pack_input → enable_e1e2` 抛 `FileNotFoundError`，整轮 rc=90。
   - 机理：`bin_net.run()` 失败后写的是 `os.environ["BIN_PACK"]=""`，而
     `BinConvEngine._pack_env` 在 `__init__`（`bin_engine.py:808`）**已缓存** "e1e2"，
     `pack_input`（`bin_engine.py:2117`）读的是缓存值 ⇒ 回落分支进不去。
   - 数据面：`deploy/e1e2_desc/`（`cmds.bin / desc.bin / thr.bin / frame.json`）是**数据**，
     不在 `deps_closure.txt`（只要 `.py`/`.c`）里 ⇒ "闭包 md5 全过"≠"e1e2 可用"。
   - 处置：本轮改走**不含 e1e2 的路径**——即 v33/v4 产生 golden 的那条路径（见 §3，sha1 全中）。
     失败现场留证 `step1_fail_e1e2.json`。
   - **不影响第一步结论**：e1e2 只改速度不改数值（其自述"逐字节等价"），且 golden 本身就出自无 e1e2 的跑点。
2. `engine.fclk_mhz` 属性名在本版本不存在（取到 `None`）：超时估算实际由 `Clocks.fclk0_mhz` 报告值 75 驱动 ⇒
   `timeout = 4×est + 5 s`，真实比 est 慢 1.5×（真值 50） ⇒ 仍有 **2.67×** 余量，实测 227 层零超时。
3. 板钟是 `2025-05-09`（比主机慢约半年），`tar` 会刷 "time stamp in the future" 警告——**无害**，别当错误。

---

## 8. 文件

| 文件 | 内容 |
|---|---|
| `step1.json` | 本轮**有效**记录：时钟/寄存器/三路 sha1/统计/内存/md5 清单 |
| `step1_fail_e1e2.json` | `BIN_PACK=e1e2` 失败那一轮（时钟仍是 `125→75` 的**首次**切换证据）|
| `out1_256_seed1.npy` | **PL 真算出来的 3×256×256 f32 结果**（sha1 = golden），第二步的 `proc` |
| `x0_256_seed1.npy` | 同一张输入图 3×256×256 f32（sha1 `cfc75bc3…`），第二步的 `orig` |
| `realdemo_step1.py` | 板端探针源码（与板上跑的那份 md5 相同） |
| `expected_md5.txt` | 主机侧 21 文件 md5 清单（判据 1 的对照表）。前 20 行相对 `pynq_specialized_code/ps/pynq/`，末行 `realdemo_step1.py` 相对本目录 |

## 9. 第二步的前置状态（未执行，等用户在场）

- 板子现处于：`VIDEO_MODE=0`(720p60) / `CTRL=0x15`(彩条) / `fpga0=operating` / 无残留缓冲、无进程
- FCLK0 **仍是写 75（真值 50 MHz）** —— 第二步沿用（DDR 供给 8 B/clk × 50 MHz = **400 MB/s**，
  需求 3 B/px × 74.21875 Mpx/s = **223 MB/s** ⇒ 余量 **1.79×**，所以 `0x2C` 必须**实测**，不许算账代替）
- `proc` = `out1_256_seed1.npy`；`orig` = `x0_256_seed1.npy`（同一张输入图）
