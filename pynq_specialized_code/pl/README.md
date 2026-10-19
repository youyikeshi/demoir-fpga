# pl —— PL 侧：单卷积引擎（HLS C++）

> 一块电路覆盖全网络 **249 个卷积调用**：形状（cin/cout/k/s/d/pad/shift/relu）
> 全部走 **s_axilite 寄存器**，运行期可改；输入/权重/偏置/输出走 **m_axi** 直接读写 DDR。

---

## 1. 文件清单

| 文件 | 作用 |
|---|---|
| `src/conv2d_engine.cpp` | **电路本体**（唯一要改的文件）：行缓冲 + 权重分块 + 核内并行乘加 + 双累加器 + 后处理 |
| `src/conv2d_engine.h` | 接口声明 + 编译期上界（CIN_MAX/COUT_MAX/KMAX/SPAN_MAX/BWCOL_MAX/OC_BLOCK） |
| `src/conv2d_accel.h` | 双环境类型（`USE_HLS_TYPES` → `ap_int`；否则 `int8_t`）+ 错误码 |
| `src/conv_ref_rt.cpp` | 运行期形状的 C 参考实现（测试台的金标准，**不综合**） |
| `src/conv_ref.cpp`、`src/conv2d_accel.cpp`、`src/conv2d_accel_blk.cpp` | 早期按形状特化的内核与参考（历史对照，不参与当前综合） |
| `src_cosim/` | C/RTL cosim 专用副本：**只差 m_axi 的 `depth=`**（仿真器按它读测试向量） |
| `tb/tb_engine.cpp` | 主测试台：同一份数据跑引擎与 C 参考，逐元素对拍 |
| `tb/test_engine_fuzz.cpp` | 引擎穷举回归（网格 + 通道余数 + 随机 + 非法参数） |
| `tb/test_edge_cases.cpp`、`test_dilation_handcalc.cpp`、`test_ref_handcalc.cpp`、`test_types.cpp` | 早期手工单测（边界 / 膨胀手算 / 参考手算 / 类型） |
| `scripts/hls_run_engine.tcl` | **主流程**：csim → csynth → `export_design`（导出 IP） |
| `scripts/hls_cosim_engine.tcl` | C/RTL 协同仿真（用 `src_cosim/`） |
| `scripts/hls_csynth_cap.tcl` | 只做综合、看资源上限 |
| `scripts/hls_export_only.tcl`、`hls_run.tcl`、`hls_csim_extreme.tcl`、`smoke.tcl` | 导出 / 早期按形状流程 / 极端数据 csim / 工具链冒烟 |
| `scripts/fuzz_shapes.py` | 随机形状回归驱动（**会先检查 exe 是否比源码旧**，防止拿旧二进制"假绿"） |
| `scripts/gen_testdata.py` | 生成测试数据（固定种子） |
| `scripts/check_src_sync.py` | 检查 `src/` 与 `src_cosim/` 是否只差 `depth=` |
| `scripts/check_regmap.py` | 检查 PS 驱动偏移与 HLS 生成的硬件头是否一致（golden：`xconv2d_engine_hw.h`） |
| `scripts/xconv2d_engine_hw.h` | HLS 导出 IP 时生成的驱动头（**golden**，接口的权威描述） |
| `scripts/shape_inventory.py`、`dse_scan.tcl`、`dse_summary.py`、`export_layer.py`、`run_all_shapes.ps1`、`run_shape_test.ps1` | 形状统计 / 设计空间扫描 / 单层导出 / 批量综合（历史工具） |

---

## 2. 电路行为（唯一权威定义）

```
out[oc][y][x] = sat8( relu_en ? relu((Σ + bias[oc]) >> shift) : ((Σ + bias[oc]) >> shift) )
Σ = Σ_{ic,ky,kx} in[ic][y*s + ky*d - pad][x*s + kx*d - pad] * wgt[oc][ic][ky][kx]
```

- **必须 same padding**：`(k-1)*d == 2*pad`；`shift ∈ [1,31]`；`relu_en` 为 0 时输出饱和到 `[-128,127]`。
- 权重/输入/输出都是 **int8**，偏置 **int32**；累加在 HLS 里是 `ap_int<26>`（两个累加器各管一半通道）。
- 输出/权重布局：`out[oc][y][x]`（CHW）；权重 `wgt[oc][ic][ky][kx]`；两者都与部署包的 `index.json` 一致。

---

## 3. 编译期上界与运行期校验

| 参数 | 值 | 说明 |
|---|---|---|
| `CIN_MAX` | 352 | 全网络最大输入通道（1×1 投影） |
| `COUT_MAX` | 192 | 最大输出通道 |
| `KMAX` | 5 | 支持 k = 1 / 3 / 5 |
| `SPAN_MAX` | 7 | k=3、d=3 时窗口跨 7 行 |
| `BWCOL_MAX` | 76 | 横向分块宽度：s=1 时 64 列、s=2 时 32 列 |
| `OC_BLOCK` | 8 | 权重按 8 个输出通道一组轮转装载 |

运行期拒绝：`H,W ∉ (0,4096]`、`Hout/Wout` 与 `floor((n-1)/s)+1` 不符、`cin/cout` 越界、
`k ∉ {1,3,5}`、`s ∉ {1,2}`、`d ∉ [1,3]`、`(k-1)d ≠ 2pad`、`shift ∉ [1,31]`、`span>7`、`bwcol>76`。
另外两条由 PS 侧先行拦截（避免 RTL 静默算错）：
`cin*H*W < 2^31`（int32 地址算术）、`ceil(cin/2)*k²*127*128 < 2^26`（26 位累加器）。

---

## 4. 寄存器接口（PS 就是这么驱动它的）

基地址（本工程 Block Design）：`s_axi_ctrl = 0x4001_0000`、`s_axi_control = 0x4000_0000`。

| 偏移 | 名称 | 含义 |
|---|---|---|
| `0x00` | AP_CTRL | bit0=start，**bit1=done**（轮询用） |
| `0x10` | AP_RETURN | 返回码：0=成功，2=尺寸/参数错，3=shift 错，4=空指针 |
| `0x18 / 0x20` | H / W | 输入高 / 宽 |
| `0x28 / 0x30` | Hout / Wout | 输出高 / 宽（引擎会校验与公式一致） |
| `0x38 / 0x40` | cin / cout | 输入 / 输出通道 |
| `0x48 / 0x50` | ksize / stride | 核大小 / 步长 |
| `0x58 / 0x60` | dil / pad | 膨胀率 / 补边 |
| `0x68 / 0x70` | shift / relu_en | 右移位数 / 1=ReLU、0=线性 |
| `0x10 / 0x1c / 0x28 / 0x34`（`s_axi_control`） | in_fm / wgt / bias / out_fm | 四个缓冲区的 **64 位** DDR 地址（写低 32 位后要写 0 到 `+4`） |

`scripts/check_regmap.py` 会拿 `xconv2d_engine_hw.h`（HLS 生成）逐项比对这张表。

---

## 5. HLS 流程与验证

```bash
cd <repo>
# 主流程（从任意 cwd 均可；脚本自己 cd 到 scripts 目录）
TB_DATA=<数据目录> TB_CIN=12 TB_COUT=48 TB_K=5 TB_S=1 TB_D=1 TB_P=2 \
  vitis-run --mode hls --tcl pynq_specialized_code/pl/scripts/hls_run_engine.tcl

# C 级回归（g++，不需要 HLS）
g++ -std=c++14 -O2 -I pynq_specialized_code/pl/src \
    pynq_specialized_code/pl/src/conv2d_engine.cpp \
    pynq_specialized_code/pl/src/conv_ref_rt.cpp \
    pynq_specialized_code/pl/tb/tb_engine.cpp -o build/tb_engine.exe
python pynq_specialized_code/pl/scripts/fuzz_shapes.py     # 30/30
python pynq_specialized_code/pl/scripts/check_src_sync.py  # src ↔ src_cosim
python pynq_specialized_code/pl/scripts/check_regmap.py    # 接口对表
```

**2026-10-19 实测**：csim PASS；csynth 165 BRAM_18K / 58 DSP / 15,630 FF / 21,491 LUT；
cosim PASS 768 输出（352×192 k=1，2×2 图，**2,385,312 拍**）；实现 **WNS +0.024 ns**
（6.40ns 过约束口径，等效 150MHz 约 +0.29ns）、WHS +0.036 ns；BRAM 82 块（86.3%）、
DSP 62（38.8%）、LUT 12,571（27.2%）。

宽画幅基准（H=2、W=320）：1×1 从 857,552 降到 **790,952 拍**（2.41 拍/单元）；
3×3 从 1,775,052 降到 **1,314,252 拍**（4.01 拍/单元）。640 整网 PL 约 4.6 分钟。

---

## 6. 要改电路时

1. 只改 `src/conv2d_engine.cpp`（或 `.h`），**同步 `src_cosim/`**（只保留 `depth=` 差异），
   跑 `check_src_sync.py`；
2. 跑 C 级回归（上表）+ `check_regmap.py`；
3. Vitis HLS：csim → csynth → 必要时 cosim → `export_design`；
4. Vivado：重建 Block Design/位流（时序必须 WNS ≥ 0），再导出 XSA；
5. PS 侧相应用新包重跑 `check_pack_ref.py`，再按 `docs/on_board_checklist.md` 上板。

> 注意：`src_cosim/` 里的 `depth=` 必须与 cosim 用的数据集尺寸一致
> （当前为 352×192 k=1 的 2×2 数据：in 1408 / wgt 67584 / bias 192 / out 768）。
