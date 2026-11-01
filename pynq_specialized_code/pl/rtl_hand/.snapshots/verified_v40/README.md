# verified_v40 —— v40 bit 对应的 RTL（2026-10-31 20:41 build）

状态：**已实测**（P&R 完整跑完 + bit 已出 + 逐端点对照已做；**未上板**）

## 文件 md5（与 build_v40_manifest.txt 逐位一致）

    bin_e1e2.v              bc28ead39b41e79f...   (v39 28375eec288f + 时序刀 7)
    bin_unit_axi_wrap.v     f4ba065504e06413...   (v39 0cf68b2e31ac + 时序刀 8 部分保留)
    build_final_150v40.tcl  9e94e8b0d73d244f     (v39 f8c531f0984550b6 + 6.667 ns)

## bit

    board/overlay_engine_150mhz/design_1_150v40.bit   md5 04a62581f7b5c54fa2b21e61786a2657
                                                      3,510,798 B
    board/overlay_engine_150mhz/build_v40_manifest.txt  16 个文件

    ⚠ manifest 的第 16 行（[info script] 那一行）记录的是**相对路径** pl/scripts/build_final_150v40.tcl，
      因为构建时是相对路径启动的 ⇒ `md5sum -c` 从别的 cwd 跑会报"打不开"。
      哈希列本身是对的（9e94e8b0d73d244f）。下一版构建把 [info script] 换成 [file normalize] 即可。

## 时序（150 MHz 真频，同一口径）

| | v39@6.667 | **v40@6.667** |
|---|---:|---:|
| WNS | −5.331 | **−2.138** |
| TNS | −47,312.4 | **−818.7** |
| setup 违例端点 | 20,706 | **2,363** |
| WHS / hold 违例 | +0.049 / 0 | +0.037 / 0 |
| WPWS / 脉宽违例 | 2.203 / 0 | 2.203 / 0 |
| Latch | 0 | **0** |
| LUT / FF / BRAM / DSP | 20,552 / 18,963 / 35 / 101 | 20,520 / 18,979 / 35 / 101 |

新最差路径：`linecache win_q_reg_12/CLKBWRCLK → u_eng/s1_pc_q_reg[5][5]/D`，8.671 ns / 10 级
（= BRAM 输出 → 引擎数据输入；**不再是使能锥**）。

## 已跑过的验证

    xvlog --sv                         0 ERROR
    synthcheck.sh                      多驱动 0 / 组合环 0 / ERROR 0 / 8-4767 4（同集合）
    E1E2 直接挂单元                     106/106 命令、6,302,720 B、0 差、cycles 7,244,033（= v39）
    verify_all.sh wrap 12 shapes       12/12 PASS、逐字节 0 差、12 个 cycles 全同 v39
    out_of_context latch 探针           latch = 0
    指纹① rem/sw_act_q = 10 cell       ✓
    指纹② issue_en0_carry = 0          ✓（旧比较彻底消失）
    指纹③ Latch = 0                    ✓
    P&R 后 util_v40.rpt                Register as Latch = 0 ✓

## 本轮**没有**生效的东西（诚实记录）

`(* max_fanout = 32 *)`（刀8）：指纹④ 复制 cell = **0**，opt 后高扇出报告里也没有任何 replica
⇒ **综合阶段没落地，刀8 对本轮 WNS 零贡献**。而给 `start_fire` 加该属性会引出
`start_fire_reg_LDC`（LDCE 锁存器）⇒ 已撤。

⇒ 所以 **v40 的改善 100% 归因于"刀7 + 诚实时钟约束"**，归因链干净。
