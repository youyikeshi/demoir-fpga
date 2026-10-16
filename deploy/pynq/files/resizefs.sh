#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# 修正版的 PYNQ resizefs —— 替换 AMD rootfs 里那个原版
# （deploy/pynq/build_pynq_rootfs.sh 会把它装进 /usr/local/bin/resizefs.sh）
#
# 原版脚本已存档在 deploy/pynq/参考/resizefs.orig.sh（从实机 SD 卡上抄下来的）。
#
# 原版的毛病（2026-10-06 上板实测 + 读实机日志确认，日志见下面 ③）：
#
#  ① 用 fdisk 删掉分区 2 再重建，第一扇区直接敲回车取"默认值"：
#         d 2 → n p 2 → <回车：默认第一扇区> → <回车：填满> → w
#     而 fdisk 会把新分区的默认起点**向上对齐到 1 MiB（2048 扇区）**。
#     本镜像里 p2 的起点是扇区 1048584（= 512 MiB + 8 扇区），**不是** 1 MiB 对齐，
#     于是新分区表把 p2 的起点挪到了 1050624 —— 分区表相对 ext4 数据平移了 2040 扇区。
#     首次开机时内核还在用内存里的旧表，系统照常跑；**第二次开机会读新表**，
#     ext4 超级块就不在分区开头了：
#         No filesystem could mount root, tried: ext4
#         Kernel panic - not syncing: VFS: Unable to mount root fs on "/dev/mmcblk0p2"
#
#  ② `partx -u ${TGTPART}` 传的是**分区**（/dev/mmcblk0p2）而不是**整盘**（/dev/mmcblk0）。
#     内核不会因为这句话重读分区表，紧接着的 resize2fs 看到的还是旧大小。
#     ⚠️ 这是个真缺陷，但它在实测中**没有机会发作** —— 因为先被 ③ 挡住了。
#
#  ③ **真正让 20 GB 没用上的原因**：制卡用的 mke2fs 是 PetaLinux 2025.2 自带的
#     e2fsprogs **1.47.0**（`.../sysroots-components/x86_64/e2fsprogs-native/sbin/mke2fs`），
#     而 1.47 起默认打开 `orphan_file` 特性；板上 Ubuntu 22.04 自带的是 **1.46.5**，
#     不认识这个特性，resize2fs 在**特性检查那一步**就罢工了。实机日志
#     （卡片 /var/log/syslog，是脚本开头 exec 重定向写进去的）：
#         resize2fs 1.46.5 (30-Dec-2021)
#         resize2fs: Filesystem has unsupported feature(s) (/dev/mmcblk0p2)
#     结果：分区 28.6 GiB，文件系统仍 8 GiB（2097152 × 4096）。
#     根治办法见 docs/部署/流程_全链路构建.md 附录 E。
#
# 修正版的设计（每条都对应上面一个坑）：
#   1) 动分区表之前，先用 ext4 magic 确认"当前这张表"确实对得上数据，对不上就什么都不做
#   2) 分区已经撑满整盘就**根本不碰分区表**（走零风险路径）
#   3) 真要改，就显式读出当前起点再写回去（sfdisk -N 2），绝不让工具去"猜默认值"
#   4) 用整盘做 partx/probe，并且**以 /sys 里的真实大小为准**判断是否生效
#   5) 内核没能接受新大小就**不写 RESIZED 标记**，留给下次开机自动重试
#      （下次开机内核会在启动时直接读到新表，这一遍什么都不用改就能扩容成功）
# ═══════════════════════════════════════════════════════════════════════════
exec >> /var/log/syslog
exec 2>&1

TGTDEV=/dev/mmcblk0
TGTPART=/dev/mmcblk0p2
source /etc/environment

# 用 = 比较而不是 [[ -eq ]]：RESIZED 未定义时 -eq 会报 "integer expression expected"
if [ "${RESIZED}" = "1" ]; then
	echo "filesystem already resized!"
	exit 0
fi

# ── 1) 安全闸：确认分区开头就是 ext4 超级块 ──────────────────────────────
# ext4 的超级块在文件系统内偏移 1024 字节处，它的 magic 字段在超级块内偏移 56 字节，
# 也就是分区内偏移 1080 字节，值为 0x53EF（小端存成 53 ef 两个字节）。
MAGIC=$(dd if="${TGTPART}" bs=1 skip=1080 count=2 2>/dev/null | od -An -tx1 | tr -d ' \n')
if [ "${MAGIC}" != "53ef" ]; then
	echo "resizefs: ${TGTPART} 开头不是 ext4 超级块（magic=${MAGIC}），拒绝改分区表"
	exit 1
fi

# ── 2) 读出当前事实：起点在哪、整盘多大、分区现在多大 ───────────────────
CUR_START=$(cat /sys/class/block/mmcblk0p2/start 2>/dev/null)
DISK_SECTORS=$(cat /sys/class/block/mmcblk0/size 2>/dev/null)
PART_SECTORS=$(cat /sys/class/block/mmcblk0p2/size 2>/dev/null)
if [ -z "${CUR_START}" ] || [ -z "${DISK_SECTORS}" ] || [ -z "${PART_SECTORS}" ]; then
	echo "resizefs: 读不到分区信息（start=${CUR_START} disk=${DISK_SECTORS} part=${PART_SECTORS}），拒绝改分区表"
	exit 1
fi
# 撑满整盘的话，分区 2 应该有多大
WANT_SECTORS=$((DISK_SECTORS - CUR_START))

# ── 3) 该不该改分区表 ───────────────────────────────────────────────────
if [ "${PART_SECTORS}" -ge "${WANT_SECTORS}" ]; then
	echo "resizefs: 分区 2 已经撑满（${PART_SECTORS} >= ${WANT_SECTORS} 扇区），跳过改分区表"
else
	echo "resizefs: 把分区 2 从 ${PART_SECTORS} 扇区撑到 ${WANT_SECTORS} 扇区（起点保持 ${CUR_START}）"
	# 起点原样写回，只把大小撑到磁盘末尾。sfdisk 失败也不退出：
	# 分区没改成功顶多是扩不了容，而"退出"会让下面连 resize2fs 都不试。
	if ! echo "${CUR_START}, +" | sfdisk -N 2 --force "${TGTDEV}"; then
		echo "resizefs: sfdisk 改分区表失败，保持原样（不影响启动）"
	fi
	# 让内核重读分区表。根文件系统正挂在这个盘上，重读经常被内核拒绝（EBUSY），
	# 拒绝也没关系：下面重新读 /sys 看实际生效没有，没生效就不标记 RESIZED。
	partx -u "${TGTDEV}" 2>/dev/null || partprobe "${TGTDEV}" 2>/dev/null || true
	PART_SECTORS=$(cat /sys/class/block/mmcblk0p2/size 2>/dev/null)
fi

# ── 4) 扩文件系统 ───────────────────────────────────────────────────────
# 根文件系统是挂载状态，ext4 支持在线扩容（镜像里的 ext4 默认带 resize_inode）。
# ⚠️ 2026-10-06 实测：这一步在**当前镜像上一定会失败**（板上 e2fsprogs 1.46.5
#    不认识制卡工具 1.47.0 写下的 orphan_file 特性）。失败不影响启动，
#    只是扩不了容。根治办法见 docs/部署/流程_全链路构建.md 附录 E。
if resize2fs "${TGTPART}"; then
	FS_RESIZED=1
else
	FS_RESIZED=0
	echo "resizefs: resize2fs 失败（不影响启动；若是 unsupported feature，见 docs 附录 E）"
fi

# ── 5) 两个条件都满足才落标记 ───────────────────────────────────────────
#   a) 内核已经接受了新的分区大小 —— 否则 resize2fs 看到的是旧大小，会"成功"地什么都不做
#   b) resize2fs 真的返回成功 —— 否则（比如工具版本太老）标记了也白标记，
#      还断掉了"下次开机自动重试"这条路
if [ "${PART_SECTORS}" -ge "${WANT_SECTORS}" ] && [ "${FS_RESIZED}" = "1" ]; then
	echo "RESIZED=1" | tee -a /etc/environment
else
	echo "resizefs: 扩容没真正完成（分区 ${PART_SECTORS}/${WANT_SECTORS} 扇区，resize2fs=${FS_RESIZED}），本次不标记 RESIZED，下次开机自动重试"
fi

# ── 6) swap ─────────────────────────────────────────────────────────────
# 原版每次开机都无条件 append /etc/fstab，反复开机就会堆出一串重复行，这里加了判断。
if ! grep -q '^/var/swap ' /etc/fstab 2>/dev/null; then
	if [ ! -f /var/swap ]; then
		echo "Adding Swap"
		fallocate -l 512M /var/swap
		mkswap /var/swap
	fi
	echo "/var/swap none swap sw 0 0" >> /etc/fstab
fi
swapon /var/swap 2>/dev/null || true

echo "Done!"
