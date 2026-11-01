# snap.wdb 根治补丁

## 根因链（实测确认）

```
xelab -debug typical        # 默认是 off；typical 打开 wave(波形生成) 能力
  └─ 快照名 -s snap / snap_$MOD
       └─ xsim snap -R      在【当前工作目录】写 <快照名>.wdb  → snap.wdb
            └─ 工作目录 = mktemp -d /tmp/vwrap${U}.XXXXXX   ← 硬编码 /tmp = 根分区(98G)
                 └─ 长仿真 + timeout 杀进程 → 目录永不清理 → 单个 snap.wdb 涨到 13 GB
```

### 实证依据

| 组 | 脚本 | xelab 参数 | 目录数 | `.wdb` 文件数 |
|---|---|---|---|---|
| A | `run_bin_unit.sh` | `xelab --relax`（**无 -debug**） | 676 | **0** |
| B | `run_module.sh` / `run_xsim.sh` / `verify_all.sh` / `bench_lat.sh` | `xelab -debug typical` | — | **194**（清理前实测） |

`xelab -help` 原文：`--debug arg ... wave: waveform generation ... (Default: off)`。
A 组 676 个目录零产出，B 组全部产出 —— 波形库的唯一开关就是 `-debug typical`。

> 结论：这几个脚本只读 `$display` 里的 `status=PASS`，**根本不需要波形**。
> 拿掉 `-debug typical` 不损失任何验证能力，只是不再产 wdb。

---

## 最小改动（三层，从根上堵）

### L1 · 源头关掉波形（一次性根治，推荐）

`-debug off` 本来就是默认值，所以**直接删掉 `-debug typical` 这几个字**即可。

活脚本（当前在用，5 处）：

```bash
cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/rtl_hand

# 先看将改什么（务必先 dry-run）
grep -rn --include='*.sh' -E 'xelab.*-debug' . | grep -v -E '\.snapshots|_worktrees'

# 应用（备份后改）
for f in tools/bench_lat.sh tools/run_module.sh tools/run_xsim.sh tools/verify_all.sh; do
    cp -n "$f" "$f.bak-wdb"
    sed -i 's/xelab -debug typical/xelab -debug off/g' "$f"
done

# 校验
grep -rn --include='*.sh' -E 'xelab.*-debug' ./tools | grep typical || echo "已无 -debug typical"
```

`_worktrees/*` 里还有各子代理的私有副本，同样要改（它们各自 rhardcode 了 `debug typical`）：

```bash
cd /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/rtl_hand
grep -rln --include='*.sh' 'debug typical' _worktrees \
  | while read -r f; do cp -n "$f" "$f.bak-wdb"; sed -i 's/debug typical/debug off/g' "$f"; echo "patched $f"; done
```

> ⚠️ **不要在构建正在跑的时候改这些脚本**。bash 是按需增量读取脚本的，
> 运行中被 `sed -i`（换 inode）或改写会导致执行错乱。等构建收尾再改。

### L2 · 位置挪到大分区（防漏网）

即使哪天有人手动加了 `-debug`，也不该写进根分区。5 处硬编码 `/tmp` 工作目录：

| 文件 | 行 | 现状 |
|---|---|---|
| `tools/bench_lat.sh` | 88 | `WORKROOT=$(mktemp -d /tmp/blat.XXXXXX)` |
| `tools/run_bin_unit.sh` | 96 | `WORK="/tmp/binunit_${MOD}_${SH}_$$"` |
| `tools/run_xsim.sh` | 36 | `WORK="${WORK:-/tmp/xsim_$MOD}"` |
| `tools/selftest.sh` | 22 | `TMP="$(mktemp -d /tmp/rtl_hand_selftest.XXXXXX)"` |
| `tools/verify_all.sh` | 31 / 100 | `mktemp -d /tmp/vconv.XXXXXX` / `/tmp/vwrap${U}.XXXXXX` |

统一改成：

```bash
ET="${EDA_TMPDIR:-/media/felix/Data/tmp}"; mkdir -p "$ET"
# 例：WORKROOT=$(mktemp -d "$ET/blat.XXXXXX")
```

`memgate.sh` 已经导出 `TMPDIR=/media/felix/Data/tmp`，因此子进程里
`${TMPDIR:-/tmp}` 也能直接用；但**显式写 `/tmp` 的 mktemp 不会受 TMPDIR 影响**，必须按上表逐处改。

### L3 · 兜底回收（已交付，无需改动）

```bash
tools/reap.sh             # dry-run，打印将删的绝对路径
tools/reap.sh --yes       # 真删（lsof +D 验证过才删）
```
`resguard.sh` 在 CRIT/EMERG 会自动调用它。

---

## 已知的第二个 wdb 来源

`pl/proj/engine_cosim/cosim1/sim/verilog/` 下是 HLS C/RTL 协同仿真工程，
其 `xsim` 调用带 `-autoloadwcfg`（`xsim -help`：`Load already saved waveform configuration file`）
→ 同样会产 `.wdb`。这个目录在 Data 分区内，不撑根分区，优先级低；
跑完删掉 `xsim.dir/*/snap.wdb` 即可。属于一次性 cosim，不必改造。
