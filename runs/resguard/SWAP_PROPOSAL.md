# 关于加 swap 的建议书（只提方案，不动手）

> 本文档**不执行任何操作**。所有命令都由用户自己决定是否运行。
> 角色边界：资源管控员只测量、只建议；改系统配置需要用户明确授权。

---

## 0. 实测现状（2026-10-31 17:48）

| 项 | 实测值 | 说明 |
|---|---|---|
| MemTotal | **30.95 GiB** | |
| SwapTotal | **0** | 一个 swap 都没有 |
| MemAvailable | 5.5–12.4 GiB（波动） | 随构建起伏 |
| Committed_AS | **59.9 GB** | 是 MemTotal 的 1.9 倍 |
| CommitLimit | **15.48 GB** | = MemTotal×50%（overcommit_ratio=50）+ swap 0 |
| vm.overcommit_memory | **0** | 启发式；CommitLimit 只做参考不做硬限 |
| vm.swappiness | 60 | 当前值（无 swap 时无意义） |
| 内核 | 6.8.0-138-generic | |
| zram.ko | **存在**，可 `modprobe zram` | `/lib/modules/6.8.0-138-generic/kernel/drivers/block/zram/zram.ko` |
| zswap | 存在但 `enabled=N`，compressor=lzo，pool=20% | 需要底层 swap 设备才有意义 |
| 根分区 `/` | ext4，98 GiB，**33.80 GiB 可用**（64% 已用） | swapfile 只能放这 |
| `/media/felix/Data` | **fuseblk (NTFS-3G)**，100 GiB 可用 | ⚠ **不能放 swapfile**：swapon 要求块设备或支持 swap 的本地文件系统，FUSE 不支持 |

**为什么这事值得讨论**：SwapTotal=0 时，内核 OOM killer 按 RSS 从大到小砍。
实测本机最大 RSS 进程是 **IDE `Isolated Web Co`（5.14 GiB）**，其次是 vivado。
也就是说**不加任何干预，内存冲高时最先被打死的是用户的 IDE，而不是构建**。
而 IDE 是工作工具，优先级高于任何构建。

---

## 1. 三个选项

### 选项 A —— zram（压缩内存盘）8 GB，zstd

把一部分内存做成压缩块设备当 swap 用。页被换出时压缩存进 RAM，不碰磁盘。

**能吸收多大的内存尖峰**
zstd 对 Vivado 的匿名页实测压缩比通常 2.5–3.5:1。
8 GiB 的 zram 设备装满 8 GiB 页时只占约 **2.4–3.2 GiB** 真实内存，
净增可承受量 ≈ **+5～5.5 GiB**。
即：MemAvailable 3 GiB 时的险境，会被推到"还能再多扛 5 GB"的位置。

**最坏情况的性能影响**
- 压缩/解压吃 CPU。本机只有约 2 核可供 EDA，激烈换页时构建可能慢 **20–50%**。
- 若被换出的页本身不可压缩（已是压缩数据、或大页 DMA 缓冲），压缩比降到 ~1.2:1，
  净收益缩到 +1.5 GiB，且 CPU 白烧。
- **不会**因为磁盘满而失败 —— 这是它相对 B 的最大优势。

**安装**（需用户自己执行，需要 sudo）
```bash
# 1) 加载模块并建 8G zram
sudo modprobe zram num_devices=1
echo zstd | sudo tee /sys/block/zram0/comp_algorithm
echo 8G   | sudo tee /sys/block/zram0/disksize
sudo mkswap /dev/zram0
sudo swapon -p 100 /dev/zram0          # 优先级高于磁盘 swap
# 2) 建议同时调低 swappiness，避免无脑换出
sudo sysctl -w vm.swappiness=20
swapon --show && free -h
```

**卸载**
```bash
sudo swapoff /dev/zram0
echo 1 | sudo tee /sys/block/zram0/reset
sudo modprobe -r zram
sudo sysctl -w vm.swappiness=60        # 恢复原值
```

---

### 选项 B —— 16 GB swapfile 放 `/`（ext4）

**能吸收多大的内存尖峰**
+16 GiB 真实容量，是三者中容量最大的。CommitLimit 从 15.48 → 31.48 GB。

**最坏情况的性能影响**
- NVMe 上换页约为内存的 1/100～1/1000 速度。一旦进入持续换页，
  一个 25–40 分钟的构建可能变成 **60–90 分钟**。
- **最危险的一点**：要吃掉 33.80 GiB 里的 16 GiB，只剩 17.8 GiB。
  而根分区刚从"只剩 1.2 GiB"的险境里爬出来（清了 35 GB 孤儿 xsim 目录）。
  一旦 swapfile + 日志 + 临时文件同时涨，会触发 **ENOSPC**，
  那比 OOM 更难恢复（构建产物写到一半失败）。
- NTFS 的 `/media/felix/Data` **放不了**（FUSE 不支持 swapon），所以没有更宽裕的选择。

**安装**（需用户自己执行，需要 sudo）
```bash
sudo fallocate -l 16G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h && df -h /
```

**卸载**
```bash
sudo swapoff /swapfile
sudo sed -i '\|^/swapfile|d' /etc/fstab
sudo rm -f /swapfile
```

---

### 选项 C —— 什么都不做，靠 `memgate` 串行 + 保 4 GiB 余量

**能吸收多大的内存尖峰**：**0**。没有任何缓冲。

**最坏情况的性能影响**
- 构建性能不受影响（这是它唯一的优点）。
- 但 MemAvailable 一旦跌破最大 RSS 进程，OOM killer 就会动手，
  **大概率砍掉 IDE（5.14 GiB）**，用户丢失未保存工作。

**安装/卸载**：无需任何操作。已交付的机制即可运转：
```bash
tools/memgate.sh 8 -- pl/scripts/vrun.sh scripts/build_final_xxx.tcl runs/xxx.log
tools/resguard.sh --status        # 20s 采样，CRIT/EMERG 只杀孤儿
tools/reap.sh                     # /tmp 白名单回收（默认 dry-run）
```

---

## 2. 对比表

| | A. zram 8G (zstd) | B. swapfile 16G on `/` | C. 不做 |
|---|---|---|---|
| 能吸收的尖峰 | **+5～5.5 GiB** | **+16 GiB** | 0 |
| 占磁盘 | **0** | 16 GiB（剩 17.8 GiB） | 0 |
| 占内存 | 2.4–3.2 GiB（仅在受压时） | 0 | 0 |
| 最坏性能影响 | 构建慢 20–50%（CPU 争用） | 构建慢 2–3 倍（IO 停顿） | 不受影响，但 IDE 可能被杀 |
| 新风险 | 压缩比差时收益缩水 | **根分区 ENOSPC** | OOM 砍 IDE |
| 可逆性 | 一条 `swapoff` + `reset` | 需 `swapoff` + 改 fstab | 无需 |

---

## 3. 推荐：**A（zram 8 GB, zstd），从 4 GB 起步观察**

**为什么是 A：**
唯一能同时满足"给 OOM 加缓冲"和"不碰只剩 33.8 GiB 的根分区"的选项。
本机的核心矛盾不是"容量不够大"，而是**"一次 8–16 GiB 的实现峰值 vs 零缓冲"** ——
zram 提供的 5 GiB 压缩缓冲刚好盖住这个缺口，而 B 要拿根分区仅剩的空间去赌 ENOSPC，
一旦赌输，损失（构建产物半途失败 + 文件系统告急）比 OOM 更难收拾。

**为什么从 4 GB 起步：**
先 `echo 4G > /sys/block/zram0/disksize` 跑一轮 25–40 分钟构建，
用 `resguard.sh --status` 观察 MemAvailable 是否稳定在 4 GiB 以上、构建耗时是否明显变长。
如果缓冲被打满（zram 使用率 > 90% 且构建变慢），再 `swapoff` 重建成 8 GB。
**不要一上来就给 8 GB** —— zram 的 CPU 成本只有在真实压力下才显现。

**附带建议（都是可选的，不改也能跑）：**
- `vm.swappiness=20`：让内核优先回收 page cache 而不是把构建进程换出。
- `zswap enabled=Y, compressor=zstd` 是另一条路，但它需要底层磁盘 swap（即选项 B），
  在根分区紧张时我们不推荐，故只作备案。

**唯一一条硬建议：不要动 `vm.overcommit_memory`。**
改成 `2`（严格）会让 Committed_AS 59.9 GB 立刻超过 CommitLimit，系统当场拒绝一切新分配，
比现在的启发式更危险。
