# 板端下一次 session 的固定顺序（2026-11-02 02:45 写死）

> 板子当前**断电**。通电后**严格按本顺序**，一次 boot 跑完，**不要临时加实验**。
> 原则：**先花 5 分钟把"决定设计方向的三个数"拿到**，再跑昂贵的长测；任何一档失败就停，不连试。

## 0. 前置（必须全过才往下）
```bash
ping -c 2 -W 2 192.168.7.2                       # 不通 ⇒ 停，看 docs/板端事故_内存踩踏_真相.md §9 判定树
cd /home/xilinx/demoir
df -h / ; df -i /                                 # SD 满 ⇒ 停
sudo -S -p '' true                                # sudo 段错误 ⇒ sync; echo 3 > /proc/sys/vm/drop_caches 后再试
python3 -c "from pynq import Overlay; print('ok')"
ulimit -c 0                                       # 禁 core dump（SD 只有几 GB）
```
- **ssh 限速**：60 s 内对本板最多 6 次连接（超过 10 个未认证连接会让 sshd 丢 banner，症状是"能连但没 banner"）。
- **一次 boot 只能加载一次 overlay**；加载前清残留：
  `sudo rm -f $(python3 -c "import pynq,os;print(os.path.dirname(pynq.__file__))")/pl_server/global_pl_state_.json`
- 所有板端命令走 `tools/boardlock.sh`（ping 前置 + 全局 flock + 单 load 令牌 + ssh 限速）。**不许 `pkill -f`**（会杀自己）。
- `sudo -S -p '' env XILINX_XRT=/usr /usr/share/pynq-venv/bin/python3 ...`（**不要** `sudo -E`）。

## 1. 三个"决定方向"的数（合计 <10 分钟）

| # | 跑什么 | 回答什么 | 判据 |
|---|---|---|---|
| 1 | `BOARD_TAG=T23 BOARD_ALLOW_LOAD=1 tools/boardlock.sh bash runs/t23_mmio/run_on_board.sh` | 引擎**空闲/忙**时单次 MMIO 读写多少 µs；698 命令整段墙钟 | 若"忙时"≫30 µs ⇒ **减少访问次数**是唯一杠杆（软件可做，见 T-32）|
| 2 | `BOARD_TAG=T17 BOARD_ALLOW_LOAD=1 tools/boardlock.sh bash -c 'cd /home/xilinx/demoir && python3 ps/tools/probe_serial_k.py'` | 板端 sgemm 的 K 方向是不是**纯串行累加**（K=300/576）| PASS ⇒ T-17 的 NEON 内核可上板；FAIL ⇒ 逐字节不可达，改走 PL floatconv 路线 |
| 3 | `BOARD_TAG=T19 BOARD_ALLOW_LOAD=1 tools/boardlock.sh bash runs/t19_budget/run_on_board.sh` | 逐层真实拍数 / `fill_win` / `rd_words`；验证 conv 的 stall 账（现为**残差** 0.8368 s）| 与模型差 ≤5% ⇒ 账可信；否则以实测为准 |

⚠ 第 2 项**必须用默认 BLAS 线程口径**（不要 `OMP_NUM_THREADS=1`），否则比对对象就变了（第 72 条纪律）。

## 2. 基线复测（任何补丁落地后都要跑）
```bash
BOARD_TAG=BASE BOARD_ALLOW_LOAD=1 tools/boardlock.sh bash -c \
  'cd /home/xilinx/demoir && python3 tools/board_repeat.py --hw 256 --slots 24 --n 3'
```
- 判据：`[DEPS]` **全部 fast** 且 `deps_closure_ok=True`；sha1 3/3；`run_valid=True`；`q8≈0.605` / `epilogue≈0.268` 自校准通过。
- **测量协议**：同进程重复 N 次，取**第 2 次起**的中位/最好；首跑永远偏大（一次性内核装载 3.377 s）。
- 对照基线：**6.489 s（C）/ 8.480 s（numpy）**（`docs/baselines.md`）。

## 3. 收尾（★ 2026-11-02 改：**别自动关机**）
```bash
sync                      # 一定要 sync；但【不要】自动 poweroff
```
**理由（当晚踩到）**：T-39 按旧规矩在 session 末尾 `poweroff`，正好把紧接着的 T-40 的整网 A/B 撞掉——
板子每次开机都要人按电源，而人不在时这一整天就废了。
- **用户在场 / 当天还要继续跑 ⇒ 只 `sync`，保持开机**（PL 计时不受影响，新 boot 反而有冷启动偏差）。
- **当天收工 / 用户要走 ⇒ 才 `sync; sync; poweroff`**，硬断电会让 ext4 走 journal recovery。
- 关机前必须：产物已拷回主机、`git status` 干净、写清"已断电/仍开机"。

## 4. 候选补丁的 A/B 顺序（只跑已合进 main 的）
1. `BIN_DESC_CACHE` 一类的**帧间预计算**（T-16 A）
2. `Q8CACHE=1`（T-16 B）
3. `BIN_CONVPF_C=1`（T-17，**仅当第 2 项 PASS**）
4. `BIN_MMIO_POLL`（T-32 读次数减少，**仅当第 1 项证实忙时访问贵**）
每次只开一个，**奇偶趟换序**（避免后跑的臂白拿缓存预热 —— T-17 已踩过一次）。

## 5. 风险红线
- **位流只用 v39 `602725b8`**。v40 `04a62581` / v41 `64ac3dd1` **禁用**（越界写踩烂内核页缓存，症状是 sudo/find 随机段错误、文件 md5 自己变）。
- 出现 sudo/find 段错误 ⇒ `sync; echo 3 > /proc/sys/vm/drop_caches`，再 md5 同一文件；若 md5 变回正常 ⇒ 是**页缓存被写坏**，磁盘没坏 ⇒ 停手查 PL 越界写。
- 金丝雀报警（缓冲区尾部图案被改写）**一次都不许解释成噪声**。
- 板子只有 2 核 / ~1 GB RAM；CMA 256 MB，arena 24 槽恰好 66.22 MB（32 槽算术上越界）。
