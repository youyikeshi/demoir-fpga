# T-95 事件取证：generate_target -force 清掉 auto_pc 的 OOC 产物（2026-11-02 20:10）

## 现象
`bd_v50.tcl`（BD 重建 + `generate_target all -force`）之后，四个 Xilinx IP 里**只有**
`design_1_axi_ctrl_imp_auto_pc_0` 的 `*.dcp` 与 `*_stub.v` 消失（其余三个完好）：

```
design_1_ps7_0              : dcp(17:58) + stub  ✔
design_1_rst_0_0            : dcp(10-30) + stub  ✔
design_1_axi_ctrl_imp_xbar_0: dcp(17:58) + stub  ✔
design_1_axi_ctrl_imp_auto_pc_0 : **缺 dcp + stub**   ← 构建在 "缺 DCP" 处停下（rc=1）
```
首次构建日志：`runs/timing_v50/build_t95_attempt1_fail_auto_pc.log`（20:10:42 退出，未烧任何综合/实现时间）。

## 处置（沿用 v50 `ooc_resynth_t92.tcl` 第 1 步的既有配方）
auto_pc 在 proj_v50 里**没有 OOC run 目录**（`proj_engine.runs/` 只有 xbar 与 ps7），
所以不能 reset_run 重出，只能复用「参数 0 差异」的旧产物：

```
src: /media/felix/Data/tmp/probe_t92/proj_v50_pre_bd_t92/.../ip/design_1_axi_ctrl_imp_auto_pc_0/
     design_1_axi_ctrl_imp_auto_pc_0.dcp      224,636 B  11-02 11:06
     design_1_axi_ctrl_imp_auto_pc_0_stub.v    11,187 B  11-02 11:06
dst: proj_v50/.../ip/design_1_axi_ctrl_imp_auto_pc_0/（同名）
```

## 复用合法性证据（产物级，不是"看起来一样"）
| 证据 | 结果 |
|---|---|
| 组件描述 `*.xml` 文件大小 | 222,187 B == 222,187 B |
| `*.xml` diff 总行数 | **10 行（5 对）**，全部是 `<spirit:value>Mon Nov 02 …</spirit:value>` **时间戳** |
| 非时间戳差异 | **0** |
| `*_ooc.xdc` md5 | `567484adbf95e378cbd6345542afb401` == 同值 |
⇒ auto_pc 的端口表/参数表与产出该 DCP 时**逐字节相同**（只差生成时间），DCP 可直接复用。
（与 v50 对 rst_0 / auto_pc 的"76 参数 0 差异 ⇒ 复用"判据同类，且此处更强：整份 xml 只差时间戳。）

## 未改动项
BD 拓扑 / 地址段（0x4004/0x4006）/ FCLK 150 MHz / HP2=0 —— 全部未动；
本处置只补回 `generate_target` 删掉的**构建输入产物**，不改任何设计内容。
