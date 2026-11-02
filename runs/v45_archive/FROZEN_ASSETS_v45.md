# 冻结资产取证（T-80）

## a) proj_engine 与 proj_v43base2 里会话开始后（15:38）被改过的文件数（应为 0）
```
$ find proj_engine proj_v43base2 -newermt '2026-11-02 15:38' -type f | wc -l
0
```

## b) 主工程关键产物 md5（与 v43base2/README 记录的设计态应一致）
```
c5d88680b4c96d6835da221a233c78c1  /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/vivado/proj_engine/proj_engine.gen/sources_1/bd/design_1/synth/design_1.v
c1686bd8e080c1c7f46181d980543202  /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/vivado/proj_engine/proj_engine.gen/sources_1/bd/design_1/hw_handoff/design_1.hwh
```

## c) proj_v43base2 的 xbar 旧产物仍是 10-30（未被本轮回写）
```
2026-10-30 09:49:34.000000000 +0800  /media/felix/Data/coding/demoir-fpga/pynq_specialized_code/pl/vivado/proj_v43base2/proj_engine.gen/sources_1/bd/design_1/ip/design_1_axi_ctrl_imp_xbar_0/design_1_axi_ctrl_imp_xbar_0.dcp
```
