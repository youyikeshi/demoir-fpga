# pl_v2 —— 层间算子加速器（v2）

一块流式多源加权引擎，覆盖 PS 侧五个算子：`RESCALE` / `ADD` / `SUM3W` /
`INTERP` / `REDUCE_MEAN`。设计见
[`../docs/设计_v2_层间算子加速器.md`](../docs/设计_v2_层间算子加速器.md)。

## 状态

| 项 | 状态 |
|---|---|
| 内核（`src/interop_accel.cpp`） | ✅ 五个算子全部实现 |
| 主机侧逐位验证（`tb/tb_interop.cpp`） | ✅ **34,608 用例 / 0 失败** |
| HLS csim | ✅ PASS（快速档 435 断言） |
| csynth | ✅ BRAM **5** / DSP **30** / LUT ≈13k，Fmax 估 **205 MHz** |
| C/RTL cosim | ✅ **PASS**（`PP_TB_QUICK=1 DO_COSIM=1`） |
| PS 驱动 `ps/pynq/pp_engine.py` | ⬜ 未写 |
| BD 集成 + 上板 | ⬜ 未做 |

## 数值契约（必须逐位一致）

```
k_i  : Q16 int32，ratio = k/65536
acc  : int64 = Σ src_i * k_i
out  : sat_int8( round_half_even(acc >> 16) )
```

半偶舍入的整数写法对负数同样成立（算术右移 + 低 16 位掩码）：

```c
q = acc >> 16; r = acc & 0xFFFF;
if (r > 0x8000) ++q;
else if (r == 0x8000 && (q & 1)) ++q;
```

`INTERP` 与 `ps/pynq/ops_numpy.py` 的整数路径严格对齐：

```
位置 = 中心对齐  sx = (o+0.5)/scale - 0.5，clamp 到 [0, in-1]
权重 = rint((sx - x0) * 8)      两段 ×8 → 分母 64
收尾 = half-even ÷64 + 饱和
整数化：pos8 = (2o+1)·in·8/(2·out) − 4，除法在 in/out 为 2 的幂时精确；
        不精确（权重不在 1/8 网格）直接返回 ERR_SHAPE，由 PS 回退浮点。
```

`REDUCE_MEAN` 只做逐通道 int64 求和（`dst` 按 int64 解释），均值/Sigmoid/Conv1d
仍留在 PS（只有 576 个值）。

## cosim 的两个坑（都踩过了）

1. **m_axi 必须给 `depth=`**，否则 cosim 报
   `A depth specification is required for interface port 'src0'`。
   v2 用**上界**（4096）而不是精确值，换数据集不用改源码。
2. **`depth` 不能给太大**：给 1 MB 时 cosim 的 AXI 模型会按 depth 访问主存，
   而 TB 的缓冲只有几十字节 → **SIGSEGV**。现在 TB 里所有缓冲都按上界分配
   （`PP_DEPTH`/`PP_COEF_DEPTH`），depth 也压到 4096。

## 怎么跑

```bash
# 主机侧功能验证（不需要 HLS / 不需要板子）
cd pynq_specialized_code/pl_v2
g++ -std=c++14 -O2 -I src tb/tb_interop.cpp src/interop_accel.cpp -o /tmp/tb_interop.exe
/tmp/tb_interop.exe          # 期望：用例 34608，失败 0

# HLS：csim + csynth
vitis-run --mode hls --tcl scripts/hls_run_pp.tcl
# HLS：加上 C/RTL cosim（必须用快速档，RTL 比 C 慢千倍）
PP_TB_QUICK=1 DO_COSIM=1 vitis-run --mode hls --tcl scripts/hls_run_pp.tcl
```

## 下一步（按依赖顺序）

1. **HLS 流程**：`scripts/hls_run_pp.tcl`（csim + csynth），确认 II 与资源
   （预估 BRAM 6~10、DSP ~8、LUT 3~5k）。
2. **C/RTL cosim**：`REDUCE_MEAN`（累加器）与 `INTERP`（行内邻域）是两条有状态的
   路径，必须过 cosim；纯流式的 `RESCALE`/`ADD`/`SUM3W` 同样要过一遍。
3. **PS 驱动** `ps/pynq/pp_engine.py`：风格照抄 `conv_engine.py`（.hwh 发现基址 +
   超时估算）。
4. **对拍** `ps/tools/test_pp_vs_numpy.py`：直接拿 `ops_numpy` 的真实现当参考
   （主机侧已用独立 double 参考验过舍入，这一步验"语义一致"）。
5. **接入 runner**：五种非卷积算子改走 `pp.run(op=...)`；`RefEngine` 保持 numpy。
6. **BD + 上板**：新增 `s_axi_pp` 基址（建议 `0x4002_0000`），与 conv 引擎共
   HP0、串行执行。

## 已知前置条件

**BRAM 预算**：conv 引擎现在 163 BRAM18K（86%），v2 再加 6~10 块会到 92%+。
两个可选前置动作（见设计稿 §7）：

- `BWCOL_MAX` 76 → 68（列块 64 → 56）；
- 或按 `span` 实际值拆分行缓冲（k≤3 时只需要 3 行，而不是 7 行）。
