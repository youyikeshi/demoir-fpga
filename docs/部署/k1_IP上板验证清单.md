# k=1 手写 IP 上板验证清单（主控执行）

> 对象：`conv_k1_engine` + AXI-Lite 包装 `k1_axi_wrap`
> PS 代码：`pynq_specialized_code/ps/pynq/k1_engine.py`（新）、`runner.py` /
> `run_network_on_board.py`（改）、`test_k1_on_board.py`（新，单层 A/B）
> 背景与寄存器表：见 [`k1_IP接入.md`](k1_IP接入.md)
>
> **前置条件**（不满足就别往下走）：
> 1. 已有一个**包含 `k1_wrap`** 的 `.bit` + 同名 `.hwh`（本次未综合、未改 RTL）；
> 2. 该 bit 是 **C8_LAYOUT=1 的纯 C8 构建**，且 `k1_axi_wrap.RELU=1`；
> 3. 若老 HLS 引擎也要在同一 bit 里：两者 AXI-Lite 地址**不能重叠**。
>    任务书写的是 0x4000_0000–0x4001_FFFF，但当前 `pl/scripts/bd_add_k1.tcl`
>    的默认是 **0x4004_0000**（0x4000/0x4001 给老 engine），两种都支持——
>    驱动以 `.hwh` 为准，不要按常量猜。
>
> 记录原则：每一步都把 stdout 存到 `logs/k1ip_YYYYmmdd_HHMM/` 下，失败时
> 保留完整日志，不要只贴最后一行。

---

## 0. 执行顺序（一页纸）

| # | 步骤 | 判据 | 失败时 |
|---:|---|---|---|
| 1 | 文件就位 / import | 4 个 py 文件 md5 对得上；`import k1_engine` 不报错 | 回 §1 |
| 2 | 确认 `k1_wrap` 在 hwh 里 | 打印出基址 + 128KB 窗口 | **停**，bit/hwh 不对 |
| 3 | 驱动 dry-run | `describe()` / `readback()` 正常，无异常 | 回 §3 |
| 4 | 路由预演 `--k1-plan` | **108/247** 走 K1；回退 114+24+1 | 回 §4 |
| 5 | 单层 A/B（3 个代表层） | `不一致 0 / N`，两边 rc=0 | **停**，回 §5 |
| 6 | 整网 baseline（老路径） | `0/N 不一致`，记耗时 | 先查包/环境 |
| 7 | 整网 K1（新路径） | `0/N 不一致`；summary 执行 108 层 | 回 §7 |
| 8 | A/B 对比 + 计时 | hashlog 无差异；出 profile JSON | 回 §8 |
| 9 | 回退演练 | `USE_K1_IP=0` 与 baseline 逐位一致 | 回 §9 |
| 10 | 签字留档 | §10 表格填完 | — |

---

## 1. 文件就位

板上目录（扁平）：`/home/xilinx/demoir_new/`

需要的 4 个 Python 文件：

```text
k1_engine.py                 ← 新增
runner.py                    ← 修改
run_network_on_board.py      ← 修改
test_k1_on_board.py          ← 新增
```

```bash
cd /home/xilinx/demoir_new
ls -l k1_engine.py runner.py run_network_on_board.py test_k1_on_board.py
python3 -c "import k1_engine; print('k1_engine OK', k1_engine.DEFAULT_IP_NAME)"
python3 -c "import runner; print('runner OK')"
```

预期：

```text
k1_engine OK k1_wrap
runner OK
```

> 用 PC 上的 `md5sum` 和板上 `md5sum` 对一遍，避免传到旧文件。
> 若用 `make_board_bundle.py` 重新打包，它已经会带上这 4 个文件。

---

## 2. 确认 bit/hwh 里有 `k1_wrap`

```bash
cd /home/xilinx/demoir_new
sudo python3 -c "
from pynq import Overlay
ol = Overlay('/home/xilinx/demoir_new/design_1.bit')
print('--- k1 相关条目 ---')
for k, v in ol.ip_dict.items():
    if 'k1' in k.lower():
        print(k, hex(v.get('phys_addr', 0)), hex(v.get('addr_range', 0) or 0))
print('--- 老 HLS 引擎 ---')
for k, v in ol.ip_dict.items():
    if 'conv2d_engine' in k.lower():
        print(k, hex(v.get('phys_addr', 0)), hex(v.get('addr_range', 0) or 0))
"
```

预期至少有一行类似（基址可能是 0x4004_0000 或 0x4000_0000，以实际 BD 为准）：

```text
k1_wrap 0x40040000 0x20000
# 或（K1_STEAL_ENGINE=1 模式）
k1_wrap 0x40000000 0x20000
```

判据：

* 出现 `k1_wrap`，且 `addr_range` 是 `0x20000`（128KB）；
* 若同时有 `conv2d_engine*`，它的地址**不能**与 `k1_wrap` 的 128KB 窗口重叠
  （当前 BD 默认：k1=0x4004_0000，engine=0x4000_0000/0x4001_0000，合法）。

失败处理：

* 没有 `k1_wrap` → bit/hwh 不是这版，**停止**（驱动会自动回退，测不到新 IP）；
* 窗口只有 `0x10000` → BD 地址范围配错；驱动会拒绝接入（除非 hwh 确实拆成
  0x4004_0000/0x4005_0000 两页，那种情况会打印 INFO 后继续）；
* 地址冲突 → BD 里给一个 IP 换地址后重新综合，不要用 `K1_IP_BASE` 硬顶。

---

## 3. 驱动 dry-run（只读寄存器）

```bash
cd /home/xilinx/demoir_new
sudo python3 -c "
from pynq import Overlay
from k1_engine import K1Engine
ol = Overlay('/home/xilinx/demoir_new/design_1.bit')
e = K1Engine(ol)
print(e.describe())
print('readback:', e.readback())
"
```

预期：

```text
INFO: K1 IP 发现 'k1_wrap' @ 0x40040000（窗口 0x20000）
K1Engine(ip=k1_wrap, base=0x40040000, ctrl=0x40050000, offs=0x40040000,
         RELU=1, lanes=32, cin_max=352, cout_max=192, fclk=150.0MHz)
readback: {'AP_CTRL': ..., 'STATUS': ..., ...}
```

（若 BD 用 `K1_STEAL_ENGINE=1`，这里会是 0x40000000 / ctrl 0x40010000。）

判据：不抛异常；`base/ctrl/offs` 与 §2 一致；`STATUS` 的 busy=0。

失败处理：

* `overlay 里找不到 K1 IP` → 回 §2；如 hwh 实例名不是 `k1_wrap`，用
  `K1Engine(ol, ip_name='实际名字')` 或在脚本里加 `--k1-ip-name`；
* `需要 pynq.MMIO` → 用 `sudo` 跑，或确认 `/dev/mem` 权限。

---

## 4. 路由预演（不执行卷积、不写 K1 寄存器）

```bash
cd /home/xilinx/demoir_new
sudo python3 run_network_on_board.py \
    --bit design_1.bit --c8 --use-k1-ip --k1-plan \
    --manifest manifest_640.json --pack weights_pack \
    --input weights_pack/input_q.bin
```

预期（顺序可能不同）：

```text
K1 IP: 已接入 K1Engine(...)
K1 路由预演: 108/247 个卷积走 K1 IP，139 个回退老引擎（未计 conv→cat/eca 融合规划）
  - k=3 ≠ 1（本 IP 只做 1×1）                          114 层  ...
  - relu=False 与 IP 综合期 RELU=1 不符 ...              24 层  ...
  - k=5 ≠ 1（本 IP 只做 1×1）                            1 层  ...
```

判据：**108 走 K1**；回退原因恰好是 114（k3）/24（relu=False）/1（k5）。

常见偏差：

| 实际 | 含义 |
|---|---|
| 0/247 | 没加 `--c8`，或 K1 没接入（看上面的 WARN），或 bit 不是 C8 构建 |
| 132/247 | 权重/RELU 判定不对（不可能出现；出现说明代码被改过） |
| 其它数字 | 先看 `reasons`，再对照 [`k1_IP接入.md`](k1_IP接入.md) §3 |

> **变体**：如果这个 bit 是 `RELU=0` 综合的，请在命令里加 `--k1-relu 0`。
> 此时预期反过来：**24/247** 走 K1（24 个 `conv_proj` 线性层），108 个
> ReLU 层回退。两种 bit 不能混用。

---

## 5. 单层 A/B（上板 bring-up 的关键一步）

脚本对**同一块 C8 输入/权重/bias** 分别跑老 HLS 引擎和 K1，输出逐字节对比；
不经过 Runner、不读 `C8_LAYOUT`，变量最少。

### 5.1 先列层

```bash
cd /home/xilinx/demoir_new
sudo python3 test_k1_on_board.py \
    --bit design_1.bit --manifest manifest_640.json --pack weights_pack --list \
    | tee logs/k1_layers.txt
```

### 5.2 跑三个代表层（建议按这个顺序）

```bash
# ① 最小层：20×20, cin=192, cout=16（最快，先验证"能跑完 + 逐字节对"）
sudo python3 test_k1_on_board.py --bit design_1.bit \
    --manifest manifest_640.json --pack weights_pack \
    --layer encoder.encoder_3.sam_blocks.0.basic_block_4.ghost_layers.0.primary_conv

# ② 大画幅：320×320, cin=48, cout=16（验 C8 行/像素跨度和 DDR 读）
sudo python3 test_k1_on_board.py --bit design_1.bit \
    --manifest manifest_640.json --pack weights_pack \
    --layer encoder.encoder_1.rdb.ghost_layers.0.primary_conv

# ③ 大 cin：80×80, cin=320, cout=16（验权重装载/多 pass）
sudo python3 test_k1_on_board.py --bit design_1.bit \
    --manifest manifest_640.json --pack weights_pack \
    --layer encoder.encoder_3.sam_blocks.0.basic_block.ghost_layers.4.primary_conv
```

每层预期：

```text
层: ...
形状: H=..., W=..., cin=..., cout=..., shift=..., relu=True
老引擎: rc=0 耗时 X.XXXs
K1: rc=0 耗时 Y.YYYs（驱动内部 Y.YYYs）
对比: 不一致 0 / N 字节
K1 单层 A/B: PASS（逐字节相同）
```

判据：三层都 **`不一致 0`**。耗时只记录、不作通过条件。

失败处理：

* 老引擎 rc≠0 → 老路径本身有问题（先跑 `test_on_board.py`），不要怪 K1；
* K1 rc≠0 → 看错误码（§5.3）；
* 不一致但两边 rc=0 → **停止整网验证**，这是 RTL/地址/格式问题。
  保留前 10 个不一致位置的 `(c,y,x,old,new)`，优先怀疑：
  1. C8 地址公式（`c8_layout.py` 没改，两边应共用）；
  2. 权重容器（K1 读 `weight[oc][ic]`，老 HLS 读同样的 blob）；
  3. 输出地址/高字；
  4. `shift`/`relu` 参数。
* 如果这版 bit 里没有老引擎：用 `--no-old` 只能验证"K1 跑完且 rc=0"，
  正确性要靠 §6 整网 `ref_out.bin` 对拍。

### 5.3 错误码对照

| 返回码 | 含义 | 处理 |
|---:|---|---|
| 0 | 成功 | — |
| 1 | `ERR_NULL` | 四个地址有 0：缓冲区没分配/地址取错 |
| 2 | `ERR_SIZE` | k/s/d/pad、HOUT/WOUT、cin/cout、地址高字 |
| 3 | `ERR_SHIFT` | shift 不在 1..31 |
| 4 | `ERR_LAYOUT` | C8 声明不符（K1 wrapper 目前不检查） |

---

## 6. 整网 baseline（老路径，先跑一遍）

```bash
cd /home/xilinx/demoir_new
mkdir -p logs/k1ip_$(date +%Y%m%d_%H%M)
LOG=logs/k1ip_$(date +%Y%m%d_%H%M)

sudo python3 run_network_on_board.py \
    --bit design_1.bit --c8 \
    --manifest manifest_640.json --pack weights_pack \
    --input weights_pack/input_q.bin --progress 0 \
    --hashlog $LOG/hash_old.txt --dump-out $LOG/out_old.bin \
    --profile-json $LOG/prof_old.json 2>&1 | tee $LOG/run_old.log
```

预期：

```text
与部署参考对比: 0/N 不一致
```

并记下 `整网完成: XX.XXs`。这是回退基线，必须先绿。

---

## 7. 整网 K1（新路径）

```bash
cd /home/xilinx/demoir_new
LOG=<与 §6 同一个目录>

sudo python3 run_network_on_board.py \
    --bit design_1.bit --c8 --use-k1-ip \
    --manifest manifest_640.json --pack weights_pack \
    --input weights_pack/input_q.bin --progress 0 \
    --hashlog $LOG/hash_k1.txt --dump-out $LOG/out_k1.bin \
    --profile-json $LOG/prof_k1.json 2>&1 | tee $LOG/run_k1.log
```

检查 `run_k1.log`：

1. 开头有 `K1 IP: 已接入 ...`；
2. 结束时 `K1 IP: 执行 108 层（K1 累计 X.XXs），回退老引擎 139 层`，
   回退明细是 114 / 24 / 1；
3. `与部署参考对比: 0/N 不一致`。

若第 2 条不是 108：

* 0 层 → 看 `WARN` 原因（多半是 C8 或 relu）；
* 少于 108 → 检查是否开了 `--fuse-conv`（融合层会回退，属预期）；
* 多于 108 → 不可能，代码/权重包被改过。

---

## 8. A/B 对比

```bash
cd /home/xilinx/demoir_new
LOG=<同一个目录>

# 8.1 逐算子 hash：应为空
diff -u $LOG/hash_old.txt $LOG/hash_k1.txt | head -60

# 8.2 最终输出：应完全一致
cmp $LOG/out_old.bin $LOG/out_k1.bin && echo "OUTPUT IDENTICAL"

# 8.3 两个输出都应等于部署参考
cmp $LOG/out_k1.bin weights_pack/ref_out.bin && echo "REF MATCH"

# 8.4 耗时
python3 -c "
import json
o=json.load(open('$LOG/prof_old.json')); k=json.load(open('$LOG/prof_k1.json'))
print('old %.2fs  k1 %.2fs  delta %.2fs (%.1f%%)'
      % (o['seconds'], k['seconds'], o['seconds']-k['seconds'],
         100.0*(o['seconds']-k['seconds'])/max(o['seconds'],1e-9)))
"
```

判据：

* `diff` **无输出**（逐算子 hash 完全一致）；
* `OUTPUT IDENTICAL`；
* `REF MATCH`；
* `k1` 的 `seconds` 小于 `old`（不强制幅度，记录即可）。

> 若 hash 第一次发散发生在某个 k=1 层，而单层 A/B 是 0 差异：
> 优先怀疑"该层在整网里的输入缓冲区状态/生命周期"或"融合规划改变了标度"，
> 不是单纯 RTL。

---

## 9. 回退演练（必须做）

### 9.1 一行回退

```bash
cd /home/xilinx/demoir_new
LOG=<同一个目录>

sudo python3 run_network_on_board.py \
    --bit design_1.bit --c8 \
    --manifest manifest_640.json --pack weights_pack \
    --input weights_pack/input_q.bin --progress 0 \
    --hashlog $LOG/hash_fallback.txt --dump-out $LOG/out_fallback.bin \
    2>&1 | tee $LOG/run_fallback.log

cmp $LOG/out_fallback.bin $LOG/out_old.bin && echo "FALLBACK == BASELINE"
diff -u $LOG/hash_old.txt $LOG/hash_fallback.txt | head
```

判据：**完全一致**（没加 `--use-k1-ip` / 没设 `USE_K1_IP` 时行为与改动前相同）。

### 9.2 模拟"IP 不在 bit 里"（验证失败不拖垮整网）

```bash
sudo env K1_IP_NAME=definitely_not_here python3 run_network_on_board.py \
    --bit design_1.bit --c8 --use-k1-ip \
    --manifest manifest_640.json --pack weights_pack \
    --input weights_pack/input_q.bin --progress 0 \
    --dump-out $LOG/out_nok1.bin 2>&1 | tee $LOG/run_nok1.log

# 预期：WARN: ... 找不到 K1 IP → 整网回退老 HLS 引擎
cmp $LOG/out_nok1.bin $LOG/out_old.bin && echo "NO-K1 FALLBACK OK"
```

判据：打印 `WARN` 并整网仍然 `0/N 不一致`。这证明"新 IP 不可用"不会挂整网。

---

## 10. 结果记录（填完签字）

| 项 | 值 |
|---|---|
| 日期 / 执行人 | |
| bit 文件名 / sha1 | |
| hwh 文件名 / sha1 | |
| `k1_wrap` 基址 / 窗口 | |
| 老引擎基址 | |
| FCLK（MHz） | |
| 路由预演：走 K1 / 总卷积 | /247 |
| 单层 A/B：3 层结果 | |
| baseline 整网耗时 / 不一致数 | |
| K1 整网耗时 / 不一致数 | |
| hashlog diff | 空 / 非空（首个差异） |
| 回退演练 | PASS / FAIL |
| 结论（可上线 / 仅实验 / 回退） | |
| 备注（异常、错误码、日志路径） | |

---

## 11. 失败速查

| 现象 | 最可能原因 | 处理 |
|---|---|---|
| `overlay 里找不到 K1 IP` | bit/hwh 不配对；实例名不是 `k1_wrap` | 回 §2；用 `--k1-ip-name` |
| 整网全部回退（输入不是 C8） | 没 `--c8` | 加 `--c8` |
| 24 层回退 `relu=False` | 预期行为（RELU=1 的 bit） | 记录；要覆盖需 `RELU=0` 的新 bit |
| 114+1 层回退 k3/k5 | 预期行为 | 记录 |
| `ERR_SIZE` | 参数/地址高字/HOUT | 看异常里的层名与形状 |
| 超时 | FCLK 不是 150、DDR 被占、bit 卡死 | 看 `STATUS`；临时 `--timeout`；确认 FCLK |
| 单层不一致 | C8 地址 / 权重容器 / 输出地址 / shift | 停止整网，保留前 10 个差异 |
| hashlog 在 k=1 层首次发散 | 引擎或该层缓冲区状态 | 用单层 A/B 复现；查融合/标度 |
| 整网 K1 比 baseline 还慢 | DDR 带宽/主频、路由开销、K1 只覆盖 108 层 | 用 `--profile-json` 对比；先确认 summary 是 108 层 |
| `USE_K1_IP=1` 但没有任何 K1 日志 | 环境变量被 sudo 吞了 | 用 `--use-k1-ip` 命令行开关，别只靠 export |

---

## 12. 不要做的事

* 不要在 `K1_IP_BASE` 硬编码一个没确认过的地址（可能写到老引擎寄存器）；
* 不要在 K1 运行时失败后**手工**再启动老引擎（PL 可能还在 DMA）；
* 不要在一层都没走 K1 的情况下宣称"已验证 K1 提速"；
* 不要为了跑通而改 `c8_layout.py` 的地址公式（老/新 IP 必须共用）；
* 不要改 RTL 或权重包来"迁就"PS；本次只验收 Python 接入。
