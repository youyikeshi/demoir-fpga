# REPORT B — 把 ECA 的 `exp` 放进 float64 再回落 float32（T-171 阶段 B）

**一句话结论：`exp` 修法本身**成立**（ECA 后半段输出已能跨平台逐位相同：`15181fbad51b` 两侧一致），
但**不足以**让 int8 层逐字节相同 —— 因为 idx 37 的 `x` 还有一个**上游的第二个根因**：
`encoder.conv_first.0` 的 **f32 卷积**本身就跨平台不同（板端 `f65e0a04584f` vs 主机 `8b70516715ca`），
它走 SAM 的 `x` 恒等分支，绕过了任何二值化。⇒ 判据（`idx37 A` 相同 且 `out1_u8` 相同）**不成立**，
第一个仍不一致的层仍是 **idx 37 `encoder.encoder_1.down.conv`**。**

---

## 1. 改了什么（只在副本里）

| 位置 | 改动 |
|---|---|
| `worktrees/T-171-exp-det/ps_pynq/bin_ref_numpy.py` `eca_attn_f32_from_mean` | `(1.0/(1.0+np.exp(-z))).astype(f32)` → `e = np.exp(-np.asarray(z, np.float64)); (1.0/(1.0+e)).astype(f32)` |
| `worktrees/T-171-exp-det/ps_pynq/bin_net.py` `eca_attn_ref_from_mean` | 同上（**主机 FAST_ECA=False 走的就是这条 ref 路径**，不改它主机根本不会变——见 §3.1） |
| 板端 | 只部署到 `/home/xilinx/demoir/ps/pynq_expdet/`（`bin_ref_numpy.py` + 由板端自己的 `deploy/v60/ps/bin_net.py` 就地打补丁的副本，md5 分别与源一致 + 唯一补丁），`PYTHONPATH` 置首 |
| 生产代码 / `pack_clean` / `runs/` | **零修改** |

## 2. 主机侧结果（`engine=None`，`pack_v60`，ENV_FULL）

| 项 | 旧主机 | 补丁后主机 |
|---|---|---|
| `out1` f32 sha1 | `2b1cd63cc7ef` | **`35f2806981e4`** |
| `out1_u8` CHW / HWC | `2d699f822df7` / `1ceb7d667649` | **`1a9216d96cff`** / `a2af28b71c2c` |
| idx37 `x`（f32） | `a726e0f7da79` | **`cabe5df94853`**（再开 `BIN_FAST_ECA=1`：`d6790efda0c8`） |
| idx37 `A`（打包字节） | `cc76cfea63ce` | **`cc76cfea63ce`（没变）** |
| idx37 `out`（int8） | `46356d5caec9` | `46356d5caec9`（没变） |
| 227 个 int8 层 vs 旧主机 | — | **变了 0 层** |

> 227 个 int8 层一个都没变、只有 `out1` f32/u8 变：因为 int8 层里 ECA 的亚 LSB 差被二值化吸收，
> 而 `out1` 的 f32 尾巴（`decoder_1` 的 4 个 f32 层里的 SAM/ECA）直接吃 f32 权重。
> ⇒ 这是一个**纯 f32 尾巴**上的变化，与 int8 主链无关。

## 3. 板端结果（v60，`FCLK0` 报数 75，容器 `pack_clean`，`.bit.bin` md5 `37a163f7…`）

| 项 | 旧板端（历史） | 补丁后板端 |
|---|---|---|
| `out1` f32 sha1 | `85d5e42bc927` | **`e57b6849990d`** |
| `out1_u8` HWC | `d709f02c0b1a` | **`221b45d13d68`**（CHW `16a84f2df2c6`） |
| idx37 `x`（f32） | — | `a9024f4eb440` |
| idx37 `A` | 与主机不同 | **`3f0451e8473d`**（≠ 主机 `cc76cfea63ce`） |
| idx37 `out`（int8） | `43bc3158968b` | `43bc3158968b`（**没变**） |
| 227 个 int8 层 vs 补丁后主机 | — | **相同 37 / 不同 190**（与补丁前同一剖面，第 37 层起连续不同） |
| 日志自证 | — | `bin_ref_numpy=/home/xilinx/demoir/ps/pynq_expdet/... patch_T171_exp64=True`、`bin_net=.../pynq_expdet/bin_net.py` |

再加一步等价控制：板端 `FAST_ECA=True`（走 `bin_ref_numpy`），把 `bin_net.py` 也打上补丁**不改变任何 sha1**
⇒ 板端根本没走 `bin_net` 的 ref 路径（与主机正好相反）。

**判据判定：✗ 不成立。** `idx37 A` 不同、`out1_u8` 不同；第一个仍不一致的 int8 层 = **idx 37**。

## 4. 为什么 exp 修好了还不一致 —— 用 f32 前驱链逐算子记账定死

给 idx 37 的整条 f32 扇入装只读钩子（`probe_sam2_t171.py`：`eca_*`、`bilinear_f32`、
`sam_mix_f32`、`add_f32` 的入/出 sha1），两侧各跑一次（`phaseB/sam2_t171.json` = 主机、
`phaseB/board_sam2.json` = 板端）：

```
 n  kind       输入 sha1（主机/板端）                                  out（主机 / 板端）
 0  add        a,b 未采（>4096 元素）                                  6f4c422b7fca / 4041d9071c1a   DIFF ← 起点
 5  eca(m,w1,w2) 7b1a2ba4bb39 3c10bcb9252e d600f22c1b63  （两侧全同）  8b9b9c3c7736 / 15181fbad51b   DIFF(见下)
 6  sam_mix    w0 57bf9677deea/11644b051ccf* ...                       cabe5df94853 / a9024f4eb440   DIFF
```

决定性的一步：**主机加 `BIN_FAST_ECA=1`（强制走与板端同一个 `_windowed_dot_exact` 内核）+ 已有 f64 exp**，
第 5 行的 ECA 输出变成 **`15181fbad51b` = 板端值，逐字节相同**（`run_host_sam2_fasteca`）。

⇒ 结论分两层：

1. **`exp` 是 ECA 内部的分歧源，且已经被 f64-再回落 修掉。**
   ECA 剩下的唯一分歧是**滑窗 `k=9` 内积的内核不同**：主机 `FAST_ECA=False` 走
   `bin_net.eca_attn_ref_from_mean`（逐通道 `np.dot` ⇒ x86 BLAS sdot + FMA），
   板端 `FAST_ECA=True` 走 `bin_ref_numpy._windowed_dot_exact`（ufunc 串行 f32 累加）。
   两者**不是同一个求和解**，1 ULP 的 `z` 差经过 sigmoid 就变成整条权重向量不同。
   统一内核（都开 `FAST_ECA=1`）+ f64 exp ⇒ ECA 输出逐位相同。
2. **但 idx 37 的 `x` 分歧的主因在 ECA 之前**：第 0 行 `add_f32` 的输出两侧就不同，
   它的一项输入是 **`encoder.conv_first.0` 的 f32 卷积结果**（板端 `f65e0a04584f` vs 主机 `8b70516715ca`，
   T-166 已测 `max|Δ|=8.3e-6`、相对 1.8 ULP、60% 元素不同 —— BLAS gemm 求和序）。
   这条张量是 SAM 的 `x` 恒等分支，**不经过任何二值化**，直接进入 `sam_mix_f32`。
   ⇒ 即使 ECA 完全逐位相同，`weighted_sum` 仍然不同（实测：主机 `FAST_ECA=1` 时 idx37 `x`
   仍是 `d6790efda0c8` ≠ 板端 `a9024f4eb440`）。

T-166 曾判定 "conv_first 的 f32 差被二值化完整吸收"——那是对 **idx 0 的 `A`** 成立；
这里它绕道 SAM 恒等分支进了 f32 域，只在 idx 37 的打包里被吸收掉大部分（393216 里约 162 个元素跨阈值）。

## 5. 下一步（给下一轮的最小充分实验）

1. **双侧统一 ECA 内核**：`BIN_FAST_ECA=1`（主机当前是 auto→ref）+ f64 exp ⇒ ECA 已可逐位相同（本轮已证）。
2. **剩下唯一已知根因 = f32 卷积内核**（`conv2d_f32`：`FAST_NONCONV` BLAS 路径 vs 参考路径）。
   请按同一套路：两侧强制**同一个** f32 conv 实现（或对 f32 激活做 `2^-12` 之类的定点量化，
   像 `ops_numpy.eca_attn_from_mean` 里对 sigmoid 权重做的那样），再跑 p0067。
   期望：`x`(idx37) 相同 ⇒ `A` 相同 ⇒ 227 层全同。
3. 注意：`out1` 的 f32/u8 尾巴（4 个 f32 层）对 ECA 的 `exp` 是**敏感**的 —— 修 ECA 会让
   `out1_u8` 的 sha1 变化（主机 `2d699f822df7→1a9216d96cff`，板端 `d709f02c0b1a→221b45d13d68`），
   这类变化不是回归，是"两侧各自离 float golden 更近/更远"的问题，需按 float golden 复核。

## 6. 收尾（已做，v58 演示态 + 播放态验收）

| 项 | 实测 |
|---|---|
| 写回完整帧 | 8 帧，`latch=OK 8/8`，`0x24` 0→8 逐帧 +1，`nonblack=88.9%` |
| 节奏 | `--start-delay 6.2 --period 6.2`；`dt=6.199995…6.200013`，`dev ≤ 3.1e-5 s` |
| `uf`（`0x2C`） | 全程 **0**（触发前/播放中/6.5 s 采样 +0） |
| CTRL | 播放 `0x01`；`0x24` 6.5 s 内 1→2 |
| 9999 | `LISTEN 0 8 0.0.0.0:9999` |
| 位流 | `stop → 载 design_1_v58.bin → start`；`state=operating`；`design_1_v58.bin` md5 `65b4f88463a00d76ba19319b8ebe5a3d`；`design_1_v60.bit.bin` md5 `37a163f7d32e1b1ff661e36b70561142`（**未覆盖**） |
| FCLK0 / FCLK1 | PYNQ 报数 **100 / 150**（FCLK0 ⇒ 真值 66.67 MHz）；按 T-166 口径，本板 SLCR `0x174` 恒读 0，不记账 |
| 进程 | `demo_player.py --pairs /home/xilinx/demo/pairs100 --loops 0 --start-delay 6.2 --period 6.2`（与接管前一致） |
| `sync` | `rc=0` |

## 7. 产物

`runs/opt_round1/ps_numeric/exp_det/phaseB/`：`probe_expdet_host.py`（主机 227 层 + A 钩子）、
`probe_sam2_t171.py`（f32 前驱链记账，主机/板端同一份）、`board_probe12a.py`（板端 227 层 + A）、
`run_t171.sh` / `run_cleanup_t171.sh`、`host_expdet.json`、`host_index_patched.json`、
`sam2_t171.json`、`board_sam2.json`、`board_console_t171*.txt`、`board_sam2_console.txt`、`board_cleanup1.txt`。
副本代码：`worktrees/T-171-exp-det/ps_pynq/`（两处 exp 已改，生产代码零修改）。
