# k=1 手写加速器（conv_k1_engine / k1_axi_wrap）PS 侧接入

> 状态：**PS 代码已完成、本机离线回归通过；未综合、未上板、未 git commit。**
> 最后更新：2026-10-23
>
> 目标：让 `k=1, stride=1, dilation=1, pad=0` 的卷积可以按层路由到新手写 IP
> `conv_k1_engine`（AXI-Lite 包装 `k1_axi_wrap`），其余层继续走老 HLS 引擎；
> 默认关闭，出问题一行环境变量回退。

---

## 0. 先看结论

| 项 | 结果 |
|---|---|
| 新驱动 | `pynq_specialized_code/ps/pynq/k1_engine.py`，`class K1Engine` |
| 路由 | `pynq_specialized_code/ps/pynq/runner.py` 的 `PYNQEngine.conv` 里加分支 |
| 默认 | **关**（`USE_K1_IP` 未设置 = 全部走老引擎，行为与改动前逐位一致） |
| 本网络实际路由层数 | **108 / 247 个卷积**（见 §3；不是 132） |
| 自动回退 | 形状/布局/relu/融合出口不满足时**逐层回退**并打印一次警告 |
| 运行时失败 | **不自动回退**，带层名/形状/返回码/STATUS 抛异常（理由见 §6.3） |
| PC 验收 | `python3 tools/test_k1_route.py`（25/25）、`tools/test_k1_dispatch.py`（17/17） |
| 板端验收 | 见 [`k1_IP上板验证清单.md`](k1_IP上板验证清单.md) |

> **最重要的两个限制**（会直接影响"能快多少"）：
> 1. 新 IP 是**纯 C8 构建**，必须 `C8_LAYOUT=1`；CHW 下一层都不会走。
> 2. `RELU` 是**综合期参数**（本版 `RELU=1`）。网络里 24 个 `relu=False`
>    的 k=1 层（`*.conv_proj`，cout=48/64/96/192）**不能**走本 IP，只能回退。
>    当前能走的是 108 个 `cout=16, relu=True` 的 k=1 层。

---

## 1. 改了哪些文件

| 文件 | 改动 |
|---|---|
| `pynq_specialized_code/ps/pynq/k1_engine.py` | **新增**。`K1Engine`：发现 `k1_wrap`、写 64 位地址低/高字、启动、带超时轮询 `STATUS`、读 `AP_RETURN`、参数预检 |
| `pynq_specialized_code/ps/pynq/runner.py` | `PYNQEngine.__init__` 增加 `use_k1` 构造参数与接入逻辑；新增纯函数 `k1_route_check()`、`PYNQEngine._k1_route/_k1_note_skip/k1_plan/k1_summary`；`PYNQEngine.conv` 加路由分支 |
| `pynq_specialized_code/ps/pynq/run_network_on_board.py` | 新增 `--use-k1-ip` / `--k1-ip-name` / `--k1-fclk` / `--k1-plan`；跑完打印 K1 路由小结 |
| `pynq_specialized_code/ps/tools/make_board_bundle.py` | 打包时把 `k1_engine.py` 收进板端扁平目录 |
| `pynq_specialized_code/ps/tools/test_k1_route.py` | **新增**。本机假 MMIO 回归：地址映射/轮询/超时/错误码/路由统计 |
| `pynq_specialized_code/ps/tools/test_k1_dispatch.py` | **新增**。本机假 PYNQ + 假老引擎，验证 `PYNQEngine.conv` 的路由分支 |
| `pynq_specialized_code/ps/pynq/test_k1_on_board.py` | **新增**。上板单层 A/B：同一 C8 缓冲区，老 HLS 引擎 vs K1，逐字节对比 |
| `docs/部署/k1_IP接入.md` | 本文 |
| `docs/部署/k1_IP上板验证清单.md` | **新增**。主控按它上板执行 |

**没有动**：

* `conv_engine.py`（老 HLS 引擎的调用路径一个字节没改，只被 `PYNQEngine` 选择）；
* `c8_layout.py`（C8 地址公式共用，必须保持一致）；
* 任何 RTL / `.bit` / `.hwh` / 权重包。

板端部署目录 `/home/xilinx/demoir_new/` 是**扁平目录**：把
`k1_engine.py`、`runner.py`、`run_network_on_board.py`、
`test_k1_on_board.py` 四个文件拷进去即可
（或用 §7 的 `make_board_bundle.py` 重新打包整个目录）。

---

## 2. 开关与用法

### 2.1 总开关

```bash
# 默认：不开，全部走老 HLS 引擎
sudo python3 run_network_on_board.py --c8 --manifest manifest_640.json \
     --pack weights_pack --input weights_pack/input_q.bin

# 打开 K1 IP（两种等价写法；命令行开关最稳，不受 sudo 环境变量影响）
sudo env USE_K1_IP=1 C8_LAYOUT=1 python3 run_network_on_board.py ...
sudo python3 run_network_on_board.py --c8 --use-k1-ip ...
```

> 打开 K1 必须同时 `--c8`（或 `C8_LAYOUT=1`）：新 IP 按 C8 地址公式读写，
> CHW 下会读错内存。驱动在 CHW 下会自动全部回退，并在启动时打印
> `WARN: 当前 C8_LAYOUT=0 ...`。

### 2.2 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `USE_K1_IP` | 关 | 总开关；`1/true/on` 开。板上脚本的 `--use-k1-ip` 等价 |
| `K1_IP_NAME` | `k1_wrap` | BD 实例名。发现逻辑也兼容 `k1_axi_wrap` / `conv_k1_engine` |
| `K1_IP_BASE` | 空 | **仅调试用**：显式指定 128KB 窗口基地址（例如 `0x40040000`），跳过 `.hwh` 发现。会打印危险警告 |
| `K1_IP_FCLK_MHZ` | `150` | 算超时用的 PL 时钟；板端脚本 `--k1-fclk` 等价，未给时自动用 `--fclk` |
| `K1_IP_FCLK_HZ` | 空 | 同上，单位 Hz（优先级更高） |
| `K1_IP_TIMEOUT_FACTOR` | `3.0` | 超时 = 理论拍数 × 该系数 / FCLK + 余量 |
| `K1_IP_TIMEOUT_EXTRA_S` | `1.0` | 超时公式里的固定余量（秒） |
| `K1_IP_RELU` | `1` | 本 bit 综合时的 `RELU` 参数；只有 `relu` 值相同的层才会被路由。板端脚本 `--k1-relu 0/1` 等价 |
| `K1_IP_LANES` | `32` | RTL 的 `LANES`，只用于理论拍数/超时估算 |
| `K1_IP_CIN_MAX` | `352` | 与 RTL 参数一致；BD 改过时同步改这里 |
| `K1_IP_COUT_MAX` | `192` | 同上 |

### 2.3 预演路由（不执行卷积）

```bash
# 在 ps/ 目录或板上扁平目录
python3 run_network_on_board.py --c8 --use-k1-ip --k1-plan \
    --manifest manifest_640.json --pack weights_pack --input weights_pack/input_q.bin
```

预期（本仓库 `manifest_640.json`）：

```text
K1 路由预演: 108/247 个卷积走 K1 IP，139 个回退老引擎（未计 conv→cat/eca 融合规划）
  - k=3 ≠ 1（本 IP 只做 1×1）                          114 层  ...
  - relu=False 与 IP 综合期 RELU=1 不符 ...              24 层  ...
  - k=5 ≠ 1（本 IP 只做 1×1）                            1 层  ...
```

`--k1-plan` 会加载 overlay、分配 arena，但**不执行任何卷积、不写 K1 寄存器**，适合上板第一步。

---

## 3. 路由条件（逐条）

`runner.k1_route_check()` 是唯一判定入口；硬约束与
`k1_engine.check_supported()` 同一份实现。

| 条件 | 不满足时 |
|---|---|
| `op == conv` | 不路由（其它算子） |
| `k == 1` | 回退（k=3 → 114 层，k=5 → 1 层） |
| `stride == 1`、`dilation == 1`、`pad == 0` | 回退 |
| `HOUT == H`、`WOUT == W` | 回退（本网络全部满足） |
| `cin ∈ [8,352]` 且 `% 8 == 0` | 回退（本网络 k=1 层 cin=48..352，全满足） |
| `max(cin,cout) * H * W ≤ 2^31-1` | 回退（RTL 是 32 位字节地址；本网络远小于） |
| `cout ∈ [1,192]` | 回退（本网络 k=1 层 cout=16..192，全满足） |
| `shift ∈ [1,31]` | 回退（本网络 k=1 层 shift=3..9，全满足） |
| 输入、输出**都是 C8** | 回退并警告（`C8_LAYOUT=0` 时全部命中） |
| `relu` 与 `K1_IP_RELU` 一致 | 回退（24 个 `conv_proj`） |
| 权重容器 `[cout,cin,1,1]` + `oc_ic_ky_kx` + `w_len==cout*cin` | 回退（防止未来换了 k=1 打包格式） |
| `bias` 长度 `4*cout` | 回退 |
| 本层没有 `out_lut` / `chsum` 融合 | 回退（K1 wrapper 无这两个出口） |

本网络统计（`manifest_640.json` + 当前 `weights_pack/index.json`）：

| 类别 | 层数 | 路由 |
|---|---:|---|
| `k=1, relu=True, cout=16` | 108 | ✅ K1 |
| `k=1, relu=False, conv_proj`（cout=48/64/96/192） | 24 | ⛔ 回退 |
| `k=3` | 114 | ⛔ 回退 |
| `k=5` | 1 | ⛔ 回退 |
| **合计** | **247** | **108 走 K1** |

> 132 个 k=1 层里：108 个走 K1、24 个回退，合计 132。**不要**按"132 层
> 全覆盖"估算收益：当前版本只覆盖 k=1 MAC 的约 37%（cout=16 组），整网提速
> 会明显小于 k=1 模块单独跑出来的 7×。
>
> 如果把 bit 改成 `RELU=0` 综合，`--k1-relu 0` 会反过来路由 24 个
> `conv_proj`（cout 48/64/96/192，覆盖 k=1 MAC 的约 63%），108 个 ReLU 层
> 改为回退。**一个 bit 只能覆盖其中一类**；要 132 层全覆盖只能改 RTL 加运行期
> relu 旁路（本次任务禁止）。
>
> **粗估收益（未实测，别当承诺）**：108 层在 K1 上理论 2.0 s @150 MHz；
> 老 HLS 的 k=1 实测 27.5 s × 37% ≈ 10.2 s ⇒ 约省 8 s，整网 52 s → ~44 s。
> 实际受 DDR 带宽、路由/清缓存开销影响，以板上 `--profile-json` 为准。

---

## 4. 地址映射与寄存器

### 4.1 AXI-Lite 窗口

```text
k1_axi_wrap（一个 AXI4-Lite 从机，128KB）
  <BASE> – <BASE>+0x1_FFFF   窗口
  A[16] = 0 → offs 页（基址 <BASE>）
  A[16] = 1 → ctrl 页（基址 <BASE> + 0x10000）
```

**`<BASE>` 不是一个固定常量，以 `.hwh` 为准**：

| 来源 | 基址 | 说明 |
|---|---|---|
| 任务书 / `k1_axi_wrap.v` 注释 | `0x4000_0000` | 包装器最初的设计目标 |
| `pl/scripts/bd_add_k1.tcl`（当前 BD 集成默认） | **`0x4004_0000`** | 0x4000/0x4001 已被老 `conv2d_engine` 占满；0x4002/0x4003 留给 pp |
| `K1_STEAL_ENGINE=1` 可选模式 | `0x4000_0000` | 把老 engine 挪到 0x4006_0000；会改老引擎地址 |

驱动**不写死** `<BASE>`：`K1Engine` 从 `overlay.ip_dict` 找 `k1_wrap` 的
`phys_addr`，所以上面两种分配都不用改 Python。`K1_IP_BASE` 只是上板
debug 的逃生口，**不要**当常规配置。

`K1Engine` 只创建一个 `MMIO(base, 0x20000)`，内部用 `_Page` 把 ctrl 页包成
`base + 0x10000`，所以 `self.ctrl.write(R_H, H)` 的写法与老
`Conv2dEngine` 完全一致。

**基地址从 `.hwh` 的 `ip_dict` 发现**，不写死：

* 优先找 128KB 的 `k1_wrap` 条目；
* 兼容 PYNQ 把窗口拆成两个 64KB 条目（取较低基地址）；
* 找不到就**抛错并让整网回退老引擎**，绝不猜地址。

> 老 HLS 引擎的默认地址是 `0x4000_0000 / 0x4001_0000`。当前 BD 集成脚本
> 把 k1 放在 `0x4004_0000`，老引擎不动；如果以后改用 `K1_STEAL_ENGINE=1`，
> 老引擎会被挪到 `0x4006_0000`——两种情况下老驱动也都从 `.hwh` 发现，
> Python 不用改。**唯一别做的是**在两个 IP 都存在时依赖硬编码回退地址。

### 4.2 ctrl 页（`addr[16]=1`，基址 `0x4001_0000`）

| 偏移 | 名称 | 方向 | 说明 |
|---:|---|---|---|
| `0x00` | `AP_CTRL` | W/R | 写 bit0=start（自清）；读 bit1=done、bit2=idle、bit3=ready |
| `0x10` | `AP_RETURN` | R | 0=成功，1=`ERR_NULL`，2=`ERR_SIZE`，3=`ERR_SHIFT`，4=`ERR_LAYOUT` |
| `0x18` | `H` | W/R | 输入高 |
| `0x20` | `W` | W/R | 输入宽 |
| `0x28` | `HOUT` | W/R | 必须等于 H |
| `0x30` | `WOUT` | W/R | 必须等于 W |
| `0x38` | `CIN` | W/R | 8..352 且 %8==0 |
| `0x40` | `COUT` | W/R | 1..192 |
| `0x48` | `KSIZE` | W/R | 必须 1 |
| `0x50` | `STRIDE` | W/R | 必须 1 |
| `0x58` | `DIL` | W/R | 必须 1 |
| `0x60` | `PAD` | W/R | 必须 0 |
| `0x68` | `SHIFT` | W/R | 1..31 |
| `0x70` | `RELU` | W/R | 回显；真实 ReLU 由综合参数决定 |
| `0x94` | `STATUS` | R | `[0]done [1]busy [2]err [7:4]err_code [8]idle` |

### 4.3 offs 页（`addr[16]=0`，基址 `0x4000_0000`）

| 偏移 | 名称 | 说明 |
|---:|---|---|
| `0x10 / 0x14` | `IN_ADDR` | 输入 64 位地址低字 / 高字 |
| `0x1C / 0x20` | `WGT_ADDR` | 权重 64 位地址低字 / 高字 |
| `0x28 / 0x2C` | `BIAS_ADDR` | 偏置 64 位地址低字 / 高字 |
| `0x34 / 0x38` | `OUT_ADDR` | 输出 64 位地址低字 / 高字 |

`K1Engine` 先检查所有地址 ≤ `0xFFFFFFFF`（RTL 只用 32 位地址，高字非 0 会
`ERR_SIZE`），再写低字 + 高字（高字为 0）。

### 4.4 权重/激活格式

* 激活：**C8 交错** `addr(c,y,x) = (c//8)*H*W*8 + y*W*8 + x*8 + (c%8)`；
* 权重：`weight[oc][ic]`，k=1 每 (oc,ic) **1 字节**，与现有
  `weights_pack/index.json` 的 `w_shape=[cout,cin,1,1]`、`w_layout=oc_ic_ky_kx`
  完全一致（路由前会校验）；
* 偏置：`int32 × cout`；
* 输出：C8；`clamp((acc+bias) >>> shift, 0, 127)`。

---

## 5. 超时与错误码

### 5.1 超时

```text
理论拍数 = H × W × ceil(cout / LANES) × cin        （LANES 默认 32）
超时     = 理论拍数 × K1_IP_TIMEOUT_FACTOR(3.0) / FCLK + K1_IP_TIMEOUT_EXTRA_S(1.0)
```

例：最坏层 `(H=W=320, cin=352, cout=192)` → `2.16e8` 拍，150 MHz 下约
1.44 s，默认超时 **5.33 s**。比老 HLS 引擎的 30 s 量级小，但仍有 3× 余量。

超时消息包含：层名、H/W/cin/cout/k/s/d/pad/shift、理论拍数、实际超时值、
最后 `STATUS` 解码。**不会死等**。

### 5.2 错误码

| 码 | 含义 | 常见原因 |
|---:|---|---|
| 0 | 成功 | — |
| 1 | `ERR_NULL` | 四个地址里有 0（缓冲区没分配/地址取错） |
| 2 | `ERR_SIZE` | k/s/d/pad 不对、HOUT/WOUT≠H/W、cin/cout 越界、地址高字非 0 |
| 3 | `ERR_SHIFT` | shift 不在 1..31 |
| 4 | `ERR_LAYOUT` | C8 声明不符（老 HLS 语义；K1 wrapper 目前不检查） |

返回码非 0 时抛 `K1ReturnError`，异常对象带 `return_code` 和 `status` 属性。

### 5.3 轮询顺序

1. 先读 `STATUS`，若 `busy=1`（上一笔没结束）先等它落下；启动写会被 RTL
   忽略，否则会读到上一笔的 done 而误判。
2. 写 offs 页地址 → 写 ctrl 页形状 → 写 `AP_CTRL=1`。
3. 轮询 `STATUS`：`done=1` 或 `err=1` 退出；否则到超时。
4. 读 `AP_RETURN`；非 0 抛异常。

驱动把"写寄存器+启动"和"轮询+读返回码"拆成 `K1Engine.start()` /
`K1Engine.wait()` 两个方法；`run()` 就是二者串联。上板分步调试时可以
`start()` 后读 `readback()` 看寄存器，再 `wait()`。

---

## 6. 回退方法

### 6.1 一键整网回退（最常用）

```bash
# 不传 --use-k1-ip、也不设 USE_K1_IP 即可
sudo python3 run_network_on_board.py --c8 --manifest ... --pack ...
```

或显式：

```bash
sudo USE_K1_IP=0 python3 run_network_on_board.py --c8 ...
```

回退后代码路径与改动前**完全一致**（老 `Conv2dEngine.run` 一个字节没改）。

### 6.2 逐层自动回退

不满足 §3 任一条件的层自动走老引擎，并打印**每个原因一次**：

```text
WARN: K1 IP 回退老引擎（relu=False 与 IP 综合期 RELU=1 不符 ...）—— 例: encoder...conv_proj(...)
```

最后 `engine.k1_summary()` 会按原因汇总层数。**不会静默算错**。

### 6.3 运行时失败为什么不自动回退

`K1Timeout` / `K1ReturnError` / AXI 异常只在**已经碰过硬件**时出现。失败可能
发生在 DMA 中途，PL 也许还在写 DDR；此时再启动老引擎会让两个 AXI master
同时写同一块内存，结果不可预测。所以策略是：

* 预检失败（形状/布局/relu/融合）→ **安全回退**（没碰硬件）；
* 预检发现**缓冲区地址非法**（0 / 超过 32 位）→ **直接停下**（换老引擎只会
  错得更隐蔽，因为老引擎用同一组地址）；
* 已经启动后失败 → **带上下文抛异常、整网停下**，由人决定 `USE_K1_IP=0`
  重跑。

---

## 7. 部署到板子

### 7.1 重新打板端 bundle（推荐）

```bash
cd pynq_specialized_code
python3 ps/tools/make_board_bundle.py --out board/board_bundle
```

`make_board_bundle.py` 已把 `k1_engine.py` 加进拷贝清单。再把
`board/board_bundle/` 同步到板上 `/home/xilinx/demoir_new/`。

### 7.2 只同步三个文件（最小改动）

```bash
scp pynq_specialized_code/ps/pynq/k1_engine.py \
    pynq_specialized_code/ps/pynq/runner.py \
    pynq_specialized_code/ps/pynq/run_network_on_board.py \
    pynq_specialized_code/ps/pynq/test_k1_on_board.py \
    xilinx@<板子IP>:/home/xilinx/demoir_new/
```

> 板上老目录里的 `runner.py` 可能比仓库旧。本次要求"只改 Python"，
> 所以**没有**直接覆盖 `board_bundle/` 里的镜像；以板子实际文件为准，
> 拷贝前先 `md5sum` 对一下，避免把板端热修覆盖掉。

### 7.3 bit / hwh

本次**没有**重新综合、没有改 RTL。板上必须已经有一个包含 `k1_wrap` 的
`.bit` + 同名 `.hwh`；若还没有，`USE_K1_IP=1` 会在接入阶段打印
`overlay 里找不到 K1 IP`，然后整网回退老引擎（不会挂）。

---

## 8. 已知限制 / 风险

1. **未上板、未综合**：所有寄存器行为来自 RTL 源码与 testbench；实际时序、
   DDR 带宽、AR 延迟没测过。首次上板务必先跑 `--k1-plan`，再 A/B。
2. **只覆盖 108/132 个 k=1 层**：24 个 `relu=False` 的 `conv_proj` 无法用
   `RELU=1` 的 bit 计算（ReLU 会截掉负值，信息不可恢复）。要覆盖它们需要
   再出一版 `RELU=0` 的 bit（或 RTL 加运行期 relu 旁路——本次禁止改 RTL）。
3. **C8-only**：必须 `C8_LAYOUT=1` 且 bit 是纯 C8 构建。
4. **无融合出口**：`out_lut` / `chsum` 层回退；与 `--fuse-conv` 同开时，
   被融合规划选中的 k=1 层会少走几层 K1。
5. **地址冲突**：`k1_wrap` 默认窗口与老 HLS 引擎默认地址重叠。同板共存必须
   在 BD 里错开；驱动虽从 `.hwh` 发现，但**不要把 `K1_IP_BASE` 当常规配置**。
6. **权重容器假设**：只接受 `[cout,cin,1,1]` 的 `oc_ic_ky_kx` 连续字节。
   当前权重包满足；未来若 k=1 改成 PAD 容器，路由会先回退（不会算错）。
7. **cout 非 8 倍数**：RTL 只写 `cout` 个通道，补零通道不动。本网络 k=1 层
   cout 全是 8 的倍数，不触发；未来若出现非 8 倍数，依赖 arena 的补零区
   恒 0 约定。
8. **超时是墙钟估算**：FCLK 不是 150 MHz 时请用 `--k1-fclk` /
   `K1_IP_FCLK_MHZ`，否则超时值不准（不影响正确性，只影响等多久）。
9. **共享 C8 地址公式**：`c8_layout.py` 没有改；新 IP 与老引擎必须继续用
   同一公式，否则逐位对拍会在第一个 k=1 层发散。

---

## 9. 本机怎么验（不接板）

```bash
cd pynq_specialized_code/ps

# 1) PC 上能 import（没有 pynq 也不报错）
python3 -c "import sys; sys.path.insert(0,'pynq'); import k1_engine; print('ok')"

# 2) 寄存器映射 / 轮询 / 超时 / 错误码 / 路由统计
python3 tools/test_k1_route.py
#   预期：==== K1 离线回归 25/25 PASS ====

# 3) PYNQEngine.conv 路由分支（假 PYNQ + 假老引擎）
python3 tools/test_k1_dispatch.py
#   预期：==== K1 路由分支回归 17/17 PASS ====

# 4) 老路径回归（确保 runner 没被改坏）
python3 tools/test_runner_local.py --manifest manifest_64.json
#   预期：==== 联合集成测试 PASS ====
```

> 两个新测试都用假 MMIO 模拟 `k1_axi_wrap`：128KB 窗口、A[16] 分页、
`start` 后轮询 done、返回码、超时。它们**不能替代上板**，但能保证
PS 侧的地址/参数/分支没有接错。

---

## 10. 快速排障

| 现象 | 先查 |
|---|---|
| `USE_K1_IP=1` 但打印"接入失败" | `.hwh` 里有没有 `k1_wrap`；`.bit/.hwh` 是否同版；用 `K1_IP_NAME` 指定实际实例名 |
| 整网全是"回退（输入不是 C8）" | 没加 `--c8` / `C8_LAYOUT=1` |
| 24 层 `relu=False` 回退 | 预期行为；需要 `RELU=0` 的另一版 bit |
| `ERR_SIZE` | 看异常里的层名/形状/地址；确认 `cin%8==0`、`HOUT==H`、地址高字为 0 |
| 超时 | 先确认 FCLK（`Clocks.fclk0_mhz`）、DDR 是否被别的 master 占用；临时用 `--timeout` 放大 |
| 整网输出与参考不一致 | 用 `--hashlog` 分别跑老/新路径，`diff` 找第一个发散算子；若第一个发散就在 k=1 层，先回退并查 K1 |
| 板子启动异常 | 与本改动无关（没动 RTL/boo）；按老流程排查 |

---

## 11. 下一步（按优先级）

1. **综合 `k1_axi_wrap` + `conv_k1_engine`，确认资源/时序**，再做 BD 地址分配；
2. 上板跑 `--k1-plan` 确认真的是 108 层；
3. 单层 A/B：`sudo python3 test_k1_on_board.py --bit design_1.bit --manifest manifest_640.json --pack weights_pack`，同一输入/权重下老引擎 vs K1 逐字节比对；
4. 整网 A/B + `--hashlog`；
5. 若要把 24 个 `relu=False` 层也吃下：**只改 RTL 综合参数**（`RELU=0` 出一版，
   用 `--k1-relu 0` 路由）或在 wrapper 里加运行期 relu 旁路；本次任务禁止改
   RTL，所以先记录为待办。注意两个版本**不能**靠一个 bit 同时覆盖 ReLU 层和
   线性层（RELU 是编译期参数）。
