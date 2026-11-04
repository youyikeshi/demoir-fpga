# T-163 · 100 帧运行器 `run100.py`（内存守纪律）

> 只做主机侧准备与空跑验证。**没有上板、没有跑 Vivado、没有改**
> `pynq_specialized_code/ps/pynq/`（生产目录一个字节未动，只 `sys.path` 只读引用）。
> 全部改动在本副本 `worktrees/T-163-run100/` 内，未 `git add`、未 commit、未 push。

---

## 0 为什么要有这个文件

板端"算一张、显示一张"跑 100 帧，跑到一半把整机压死（`ping` 通、`sshd` 无 banner）。
**根因（已核实，不再论证）**：

```
board_verify_numfix.py / 自写 runner 里 BV_FREE 一次都没出现
  ⇒ bin_net.py:3189 读默认 "0"
  ⇒ 396 个中间张量累计 726 MB 整帧常驻
  ⇒ + arena 66.2 MB + Python 常驻 > 880 MB 可用
  ⇒ 走 512 MB SD swap ⇒ sshd fork 失败
```

放大项：① 一次性把 100 个输入全载进 `x0_all`（≈78 MB）；② 惰性句柄跨帧钉住 OUT 槽。

`run100.py` 就是把这三条**逐条堵死**的 100 帧运行器：`BV_FREE=1`（启动第一行打印实际值）
+ 单 `BinNet` 全程复用 + 输入按需读 + 帧尾显式释放 + 内存看门狗 + `0x2C` 校验。

---

## 1 交付物

| 文件 | 说明 |
|---|---|
| `run100.py` | 100 帧运行器（板端 / `--dry-run` 主机空跑同一份代码） |
| `host_dryrun.sh` | 主机空跑 5 个场景的可复现脚本（S1~S5） |
| `logs/host_dryrun.log` | 空跑**实测**日志（含各场景 VmRSS 曲线汇总） |
| `logs/_dryrun_work/*/frames.jsonl` | 每个场景的逐帧 JSON（证据） |
| `logs/_dryrun_work/*/aborted.json` | 注入测试产生的真实中止文件 |

md5 见 `MD5SUMS`（`md5sum -c MD5SUMS` 自检）。

---

## 2 ★ 板端一条完整命令

**前置（缺一项不开跑）**：

```sh
# ① PL 必须已经是 operating：run100.py 自己**绝不下载位流**（下载是独立、需人盯的动作）
cat /sys/class/fpga_manager/fpga0/state            # 必须打印 operating
# ② 禁 core dump（板子只有几 GB SD；几百 MB 的 core 会直接写满）
ulimit -c 0
# ③ 同步一次时间/盘，别让换页把日志搞脏
sync
```

**一条命令（板端，100 帧，含上屏）**：

```sh
cd /home/xilinx/demoir/ps/pynq && \
sudo -S -p '' env XILINX_XRT=/usr PYTHONPATH=/home/xilinx/demoir/ps/pynq \
  /usr/share/pynq-venv/bin/python3 /home/xilinx/demoir/ps/pynq/run100.py \
    --pack   /home/xilinx/demoir/deploy/pack_v60 \
    --bit    /home/xilinx/demoir/design_1_v60.bit \
    --inputs /home/xilinx/demo/inputs100 \
    --frames 100 --shape 256 --slots 24 \
    --out    /home/xilinx/demo/run100_logs 2>&1 | tee /home/xilinx/demo/run100_console.log
```

* `PYTHONPATH` = PS 代码目录（`bin_net.py` / `bin_engine.py` / `display_demo.py` 都在里面）。
* 容器 = `pack_v60`（`round_q=0 × slope_shift=8 × off_thr32`，v60 唯一可部署档；
  脚本会把 `pack_layers / round_q / slope_shift` 记进 `run_meta.json` 自证配对）。
* 位流 = `design_1_v60.bit`（R1+R3 已打）。**必须先载好**——脚本只 `Overlay(..., download=False)`。
* `--inputs` 目录里放 `NNNN_crop256.npy`（256²，`(H,W,3) uint8` 或 `(3,H,W)` 都认）；
  不足 100 个就**环形复用**（`inputs100` 里少于 100 张也能凑满 100 帧）。
* 上屏走 `minimal_proof.bringup()`（能出图的那串已验证动作）+ `pipe.show(orig, proc)`；
  脚本里**一行寄存器写都没有**。找不到 `minimal_proof.py` 会自动回落
  `DisplayPipeline(mode).setup()` 并打一行日志（`--bringup direct` 可强制）。

**不想上屏（只查内存/带宽）**：同一条命令加 `--no-show`。
**先短跑一遍再上 100 帧**（建议）：把 `--frames 100` 换成 `--frames 5`，
确认 `[run100] BV_FREE=1`、`0x2C` 增量 0、`0x24` 每帧 +1，再跑满。

---

## 3 逐帧 JSON 字段（`<out>/frames.jsonl`，一行一帧、追加写 + `os.fsync`）

| 字段 | 含义 | 口径 / 出处 |
|---|---|---|
| `frame` | 帧号（0 起） | 循环下标 |
| `in_sha1` | 本帧输入的 sha1 | `(3,H,W) float32` C 连续原始字节；与 `dump_board_int8`/`board_verify_numfix` 同口径 |
| `out_sha1` | 本帧输出的 sha1 | `out1` 按**上屏口径** `round(255·clip(x,0,1))` → `(H,W,3) uint8` 的字节；与 `dump_board_int8` 的 `*_u8.npy`(HWC) 可直接对比 |
| `out_std` | `out1`（f32）的标准差 | 反量化后的 f32，未量化；判"发灰/整幅抬亮"的灵敏度指标 |
| `nonblack` | 非黑像素占比 | `max(R,G,B) > 8` 的像素比例；**V-B 判据 `≥ 88%` 同一口径** |
| `interval_ms` | 与**上一帧起点**的间隔 | 墙钟（含读盘/算/释放/上屏），首帧 = 从建网完成起 |
| `run_ms` | `net.run()` 墙钟 | **只包住 `net.run()`**，不含读盘、不含 show |
| `post_ms` | 帧尾收尾：统计量 + 显式释放 | md5/sha1、`release_frame()`、`del` |
| `show_ms` | `compose + show + wait` | 含 720p30 一个 vsync 的等待上限 `--show-timeout` |
| `rss_mb` | `/proc/self/status` `VmRSS` | 进程常驻；**内存看门狗主判据** |
| `mem_available_mb` | `/proc/meminfo` `MemAvailable` | 系统余量；看门狗第一条判据（< 150 MB 退出） |
| `cma_free_mb` | `/proc/meminfo` `CmaFree` | 主机上通常没有此项 ⇒ `-1.0`（不是 0，别误读） |
| `reg_2c` | `0x2C UNDERFLOW_CNT` | 只读 `HdmiOut.underflow()`；**增量必须 0** |
| `reg_24` | `0x24 FRAME_ID_LATCHED` | PL 已采纳帧号；100 帧应连续 +1 |
| *附加* `warmup` | 前 `--warmup-frames` 帧（默认 1） | 首帧含内核装载/首次缺页，**计时分析要剔除** |
| *附加* `in_file` | 本帧输入文件名 | 环形复用时用来对号 |
| *附加* `fid` / `latched` | 本帧 `FRAME_ID` 与是否被 vsync 采纳 | `latched=false` ⇒ 显示通路异常（画面停在旧帧，无撕裂） |
| *附加* `rel_c8` | 本帧归还的 `C8Tensor` 句柄数 | 本版 `run()` 已把网络输出 materialize，故恒 0（防御性分支） |

同目录还有：

* `run_meta.json` — 一次性元数据：argv / `BV_FREE` 实际值 / 生效的 `BIN_*` 开关 / arena MB /
  `pack_round_q` / `pack_slope_shift` / `bit_md5` / `bin_net.py`+`bin_engine.py` 的 md5 / 起始内存。
* `summary.json` — 收尾汇总：`frames_done` / `rss_delta_mb` / `reg_2c_delta` / 释放计数。
* `aborted.json` — **只在主动安全退出时出现**。含 `reason` / `frame` / `thresholds` /
  `last3`（最后 3 帧内存）/ `last_rows`（最后 3 行 JSONL）/ `reg_2c_seq`（0x2C 全序列）/
  `release_counts` / `argv`。

---

## 4 判读标准（先看这 6 条）

| # | 判据 | 通过 | 失败处置 |
|---|---|---|---|
| 1 | 启动第一行 | `[run100] BV_FREE=1` | 若是 `0` ⇒ 脚本已**拒绝开跑**（退出码 2）。要明知故犯加 `--allow-bv-free0` |
| 2 | `VmRSS` 曲线 | 首末差 **≤ 20 MB**，且第 5 帧后基本平台化 | 差 > 20 MB ⇒ **已经泄漏**，`--frames 20` 量曲线，别跑 100 帧 |
| 3 | `MemAvailable` | 全程 **> 150 MB**（看门狗线） | 触线 ⇒ 脚本写 `aborted.json` 并**主动退出**（退出码 3） |
| 4 | `reg_2c` 增量 | **恒 0**（V-B 阻断级） | > 0 ⇒ 脚本**立即停**并写 `aborted.json`，附 `0x2C` 序列 ⇒ 报"增量速率（行/帧）" |
| 5 | `reg_24` | 100 帧连续 +1（`fid == latched`） | 不推进/不采纳 ⇒ 显示通路问题（看 `fid`/`latched` 两列） |
| 6 | `nonblack` | 逐帧 **≥ 88%**（V-B 同口径） | 掉下 88% ⇒ 黑屏/发灰，先看 `out_std` 与 `out_sha1` 是否稳定 |

**时间口径（必读）**：`run_ms` 的稳态目标 ≈ **10.3 s**（同进程热态、`BV_FREE=1`、
50.79/66.67 MHz 计算域，见 `runs/opt_round1/perf/segment_breakdown.md`）。
`warmup=true` 的那一帧含 `load_kernels()`（1.4~1.9 s/个 ×3）与首次缺页，**不计入稳态**。
若仍看到 28~29 s/帧 ⇒ 先在日志里确认第 1 条（`BV_FREE=1`）与第 2 条（RSS 平坦），
**不要**去改算法。

**退出码**：`0` 正常跑完；`2` 配置拒绝（BV_FREE=0 未显式放行）；`3` 看门狗/`0x2C` 主动中止。

---

## 5 回滚方式（本项目**没有**动任何生产文件）

```sh
# ① 生产目录未被修改的自证（应当只有本次交付之前的既有改动，且 mtime 早于本次工作）
git -C /media/felix/Data/coding/demoir-fpga status --porcelain pynq_specialized_code/ps/pynq/

# ② 整个副本删掉就回到"什么都没有"（worktree 是自包含的）
rm -rf /media/felix/Data/coding/demoir-fpga/worktrees/T-163-run100

# ③ 板上只多了一个脚本和一个日志目录，删掉即可（不动 deploy/ 与位流）
rm -f  /home/xilinx/demoir/ps/pynq/run100.py
rm -rf /home/xilinx/demo/run100_logs /home/xilinx/demo/run100_console.log
```

板上跑坏了（进程被 OOM kill / `sshd` 无 banner）的恢复顺序，按项目事故规程：

```sh
sync; echo 3 | sudo tee /proc/sys/vm/drop_caches     # 内存被压过之后先清页缓存
df -h / ; df -i /                                    # SD 是否写满（swap 写多会满）
dmesg | tail -50                                     # 抓 OOM / 换页证据，先存档再重启
```

断电前 `sync; sync; poweroff`（硬断电会走 ext4 journal recovery）。

---

## 6 主机空跑结论（`logs/host_dryrun.log`，实测数字）

| 场景 | 路径 | 帧数 | VmRSS 首→末 | 结论 |
|---|---|---|---|---|
| S1 | fake 引擎 @256²（控制流 + 真 `DisplayPipeline` 假寄存器） | 20 | **45.3 → 45.4（Δ +0.1 MB）** | 字段齐全（74 行全部 14 个必填字段无缺）、逐帧追加、曲线平 |
| S2 | **真** `BinNet(engine=None)` @64²，`BV_FREE=1` | 20 | **120.4 → 127.5（Δ +7.0 MB）**，第 12 帧后平台 127.5 | 真 `run()`/`materialize`/释放路径，曲线平 |
| S3 | 同 S2 但 `BV_FREE=0` | 20 | **153.1 → 329.4（Δ +176.2 MB）**，仍在涨 | **对照**：平坦是 `BV_FREE=1` 挣来的；同形状放大 16× ≈ 2.8 GB |
| S4 | 真参考路径 @128²（中间张量 4×） | 6 | **282.9 → 295.3（Δ +12.4 MB）** | 仍 ≤ 20 MB ⇒ 不是小尺寸假象 |
| S5a | 看门狗注入（每帧漏 3 MB，阈值 1 MB） | 4（中止） | 45.4 → 53.9 | `aborted.json` 写出 `reason`/`frame=3`/`last3`/`reg_2c_seq` |
| S5b | `0x2C` 注入（第 3 帧后 +1） | 4（中止） | 45.4 → 45.5 | `aborted.json` 写出 `reg_2c_base=0`/`reg_2c_seq=[0,0,0,1]` |

> **S2 的 127.5 MB 平台 vs S3 的 329 MB 持续上涨**，就是 `bin_net.py:3184-3189` 那条注释
> 在 64² 上的缩小版：同一份代码、同一批输入、同一进程结构，**只差一个环境变量**。
> 64² → 256² 的中间张量按 16× 放大，正好对上"396 个张量 726 MB"的量级。


（具体数值以 `logs/host_dryrun.log` 末尾的"汇总"段为准；本表只给结论。）

### 6.1 主机**已覆盖**

* `os.environ.setdefault("BV_FREE","1")` 与"第一行打印实际值"、`BV_FREE=0` 拒跑门禁。
* 输入流式/环形读取（一次只读一张，`x` 出作用域即弃）+ `in_sha1` 口径。
* 单 `BinNet` 复用（S2/S4 全 20/6 帧只有一个 net 实例）。
* 真 `bin_net.BinNet.run()` 的调度、`BV_FREE` 判死、`_materialize`、`materialize→release` 链（S2/S4）。
* `release_frame()`：对每个输出 ndarray 调 `bin_engine.f32_release()`（幂等；主机恒 `False`，
  计数已记录）、`qcache.invalidate_all()`、`_catpins.clear()`、`_graph=None`、`outs.clear()`。
* **真** `display_layout.compose` + `DisplayPipeline.show_frame` + `FRAME_ID` publish + `wait()`
  （`LocalRegIo` 在写 `FRAME_ID` 时自动 vsync 采纳 ⇒ `latched=true`、`0x24` 逐帧 +1）。
* 每帧 JSON 追加写 + `os.fsync`；`summary.json`；`aborted.json`（两条注入）。
* 看门狗两条判据（绝对余量与"连续 N 帧单调增长 > 阈值"）。

### 6.2 主机**没覆盖**（上板才验，别把空跑当结论）

1. **真 PL**：`BinConvEngine` 的 AXI 提交/轮询/中断、`arena.reset()` 对物理缓冲的语义、
   `CmaFree` 的实际分配、`load_kernels()` 的 q8/c8deq/bpack 装载与逐位自检（S2/S4 走 numpy 参考，
   **不含** PL 数值）。
2. **真 `0x2C`**：主机只能靠 `FakeRegIo.stall()` 注入；真实行缓冲欠载速率只有板上能量。
3. **真 HDMI 扫描器**：`/dev/mem` mmap、`ensure_pl()` 的 sysfs 载位流、模式切换时序、
   `pix_alive` 心跳门禁、`wait()` 的真实 vsync 采纳（主机是写入即采纳，延迟为 0）。
4. **swap / OOM 的真实行为**：主机 12 GB 可用，压不出 880 MB 那条线；`sshd` fork 失败只能板上复现。
5. **SD 卡 `fsync` 成本**：主机 ext4 每帧 `fsync` ≈ 0.05 ms，板上 SD 会慢 1~2 个数量级
   （`post_ms` 会明显变大，属**预期**，不是泄漏）。
6. **BIN_F32POOL 池归还路径**：池默认关 ⇒ `f32_release()` 在主机恒 no-op；只有板上显式开池
   `BIN_F32POOL=1` 才真正走"槽归还"。**不建议开**（每帧重建 6.29 MB×nslots，见
   `runs/opt_round1/perf/perframe_analysis.md` §1.1 第 7 行）。
7. **`C8Tensor` 句柄归还分支**：本版 `run()` 返回前已 `_materialize`，故 `rel_c8` 恒 0；
   该分支是**防御性代码**，只有将来 `run()` 改成直接返回裸句柄才生效。

---

## 7 设计要点（每条纪律 ↔ 代码位置）

| 纪律 | 实现 |
|---|---|
| 默认 `BV_FREE=1` | 模块顶部 `os.environ.setdefault("BV_FREE","1")`；`main()` 第一行打印实际值；`≠1` 且无 `--allow-bv-free0` ⇒ 退出码 2 |
| 一次只建一个 `BinNet` | `build_board_net()` 在跑循环**之前**调用一次；循环里只有 `net.run()` |
| 输入按需读 | 循环内 `load_input(files[i % n], shape)`，`del x`；**不存在** `x0_all` |
| 帧尾显式释放 | `release_frame()`：`C8Tensor.release(force=True)` → `f32_release()` → `qcache.invalidate_all()` → `outs.clear()`/`_catpins.clear()`/`_graph=None`；输出只留 `out_sha1/out_std/nonblack` |
| 每帧一行 JSON | `frames.jsonl`，`buffering=1` + 每帧 `flush()` + `os.fsync()`（`--no-fsync` 可关） |
| 内存看门狗 | `watchdog_check()`：`MemAvailable < 150 MB` 或 `VmRSS` 连续 3 帧单调增长 > 20 MB ⇒ 写 `aborted.json` 退出码 3 |
| 画面上屏 | `pipe.show(orig_disp, proc_disp)`（内含 `compose`：左/右各 640²、上下各 40 px 黑边） |
| `0x2C` 校验 | 每帧 `read_reg_2c(pipe)`；增量 > 0 ⇒ 立即停 + `aborted.json` 附 `reg_2c_seq` |
| 不写寄存器 | 脚本内**零** `write32`；显示全部经 `display_demo.DisplayPipeline` / `minimal_proof.bringup()` |
| 不改生产代码 | 只 `sys.path.insert(0, PROD)` 做只读 import |

**为什么 `--slots 24` 而不是别的**：`BIN_LAZY_SLOTS=24` 是 v57 起就一直用的运行点
（`demo_pipeline.RUNPOINT` 第 10 条 / `board_v39_verify --slots 24`）。改它不是"调优"，
是换运行点 ⇒ 数值/耗时都不可与既有基线比。

---

## 8 一条命令做主机空跑

```sh
cd /media/felix/Data/coding/demoir-fpga/worktrees/T-163-run100
sh host_dryrun.sh          # ≈ 100 s，覆盖 logs/host_dryrun.log
```

单独跑最短的一遍（3 帧，只验证控制流）：

```sh
python3 run100.py --dry-run --inputs ../../runs/frozen256 --frames 3 --out logs/quick
```
