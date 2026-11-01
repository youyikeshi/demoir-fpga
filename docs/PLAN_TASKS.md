# 计划登记表（谁在做什么）

> **作用**：主代理的外置记忆。上下文压缩/会话续接/子代理换代后，读这张表即可恢复全部在途工作。
> **规矩**：一个计划一行；编号不复用；"判据"必须可复算；"负责"必须是具体子代理 id；
> 每次分派、收工、判据变化都更新。技术细节写 `PLAN_1second.md`，本表只管"谁、到哪一步"。
>
> **状态机**：`待分派 → 进行中 → 待复核 → 已归档` ／ `↘ 作废（必须写原因）`

## 登记表

| 编号 | 计划（一句话） | 负责子代理 | 判据（可复算） | 交付物与落点 | 状态 | 依赖 |
|---|---|---|---|---|---|---|
| T-01 | interp 双线性走 C/NEON 内核 | `3c67684d` | 主机 26 形状×3 组 + 16 边界**逐位相同**；板端同侧 4 形状逐位相同；整网 sha1 `9b1940c8/f86a7035/025757a8` 与 `bfa246d3/226bd39e/762d84b9` 全中 | `ps/pynq/bilinear_c.{c,py}` + `bin_ref_numpy.py` 挂钩；`runs/bilinear_c/` | **已归档** | — |
| T-02 | v40 时序刀 7（`p<run_len` → `rem` 倒计数器）+ 诚实时钟约束 | `0f1df57f` | 网表指纹：`rem/sw_act_q` cell ≥2 且 `issue_en0_carry` = 0 且 `Latch` = 0；TNS 优于 −47,312 | `pl/rtl_hand/modules/bin_e1e2.v` + `build_final_150v40.tcl`；`runs/v40_archive/` | **已归档（位流禁用）** | — |
| T-03 | floatconv 流式包装模式 B（融合地基） | `56c4eccd` | 模式 B vs A 逐位 0/5120 与 0/12288；拍数 parity 1.000×；上游限速 period 2/4/8 三档全 0；冷启动 N=1..60 全 0；`tbuf_rdata` 加 `ce` 门控后成立 | `_worktrees/floatconv/rtl/fconv_stream_wrap.v` + `tb_stream_wrap.v`；`docs/convfp_集成契约.md §11/§13` | **已归档** | — |
| T-04 | floatconv 集成进 BD + PS 驱动（模式 A 先用） | `aa1dae08` | 门禁 ①~⑤；**集成后 LUT ≤ 18000，超则切 4 lane**；模式 A 预取修到 ≤1.1× | `_worktrees/fconvint/` + `pl/scripts/bd_add_fconv.tcl` + `ps/pynq/fconv.py`；`docs/convfp_上板验证清单.md` | **待复核**（骨架+①②③④ 已过；缺 ⑤ 模式 A parity、缺 v41 脚本） | T-03 |
| T-05 | PS 描述符/视图/槽位开销（`desc 0.183 + views 0.194 + acq 0.253`） | `d9bb1822` | 逐字节 + 6 个 sha1；**三条失效门禁**（地址变/重载/跨形状）；`descviews ≤ 0.05 s`；`acq` 计数器先定案 | `/tmp/probe_psdesc/step1_instrument.patch`（+103/−5）；合入后 `runs/psdesc/` | **进行中**（第①步已交，第②步 `BIN_DESC_CACHE` 未开工） | 板子可用 |
| T-06 | q8 量化缓存（15 个唯一张量 / 27.5 MB） | `00abc136` | 逐字节 + sha1 三件套；预期 −0.63 s @16 槽 | `/tmp/plog/bin_engine.py`（内容指纹键）；`runs/` | **待上板** | T-10 |
| T-07 | 配置页 MMIO 去重（`cfg` 0.374 s） | 待分派 | 逐字节 + sha1；预期 −0.32 s | 未定 | **待分派** | T-10 |
| T-08 | pack 读冻结（`PF_AHEAD`，N≥2.3 → 800 MB/s） | 待分派 | 拍数下降且逐字节不变；预期 −0.47 s | `bin_unit` 读通路 | **待分派** | T-10 |
| T-09 | V2 行带流式融合（PS→PL 吞并） | 待分派 | 整网 sha1 不变 + 总时间；设计见 `docs/V2_行带流式融合_设计.md` | `docs/V2_*` + 新 RTL | **待分派** | T-03, T-04, T-10 |
| T-10 | 板端内存踩踏收尾：v40 越界写定位 + v41 重建 | 主代理（只写判据，不开工） | 三态对照 + v39/v40 A/B；门槛 **WNS ≥ 0 或逐端点证明违例全在准静态 CE 引脚** | `docs/板端事故_内存踩踏_真相.md`；`tools/board_memtest.*` | **进行中**（v40 已禁用，等换卡/重刷后重跑三态） | — |
| T-11 | 板级工具链（租约/令牌/限速/归档/bootstrap） | 主代理 | `boardlock.sh` 单次 load 令牌生效、ssh 限速 ≤6/min、`archive_build.sh` 15 项齐、manifest 16/16 | `tools/{boardlock,archive_build,board_bootstrap}.sh` | **已归档** | — |

## 依赖图（谁挡着谁）

```
T-10（板子可用）
 ├─ T-06 q8 缓存        ┐
 ├─ T-05 PS desc/views  ├─ 都需要上板 A/B
 ├─ T-07 cfg 去重       │
 └─ T-08 pack 读冻结    ┘
T-03 → T-04 → T-09（融合线，本地可推进，最后统一上板）
```

## 冻结资产（动之前必须报）

| 资产 | md5 / 版本 | 说明 |
|---|---|---|
| v39 位流 | `602725b8`（hwh `c1686bd8`） | **当前唯一可用位流**，8.096 s @150 MHz，sha1 54/54 |
| v40 位流 | `04a62581` | **禁用**：跑整网 23 s 硬挂（越界写），见 T-10 |
| `bin_e1e2.v` | `bc28ead3`（v40/刀7） | v39 版是 `28375eec`；两者仿真等价但硬件表现不同 |
| floatconv core 8 文件 | 见 `_worktrees/floatconv/README.md` | 逐位 0 diff 的硬资产 |

## 变更日志

| 日期 | 变更 |
|---|---|
| 2026-11-01 | 建表；T-01/T-02/T-03/T-11 归档；T-04 转待复核；T-05 进行中；T-10 因板端越界写阻塞其余 |
