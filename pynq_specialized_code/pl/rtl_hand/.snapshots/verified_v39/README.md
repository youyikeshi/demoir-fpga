# verified_v39 —— v39 bit 对应的 RTL（2026-10-31 18:02 build）

状态：**已实测**（不是待验证）

## 文件 md5

    bin_e1e2.v            28375eec288f067e9235c0ecd486abb5   (Fix A only；knife6 版是 91b287d6，在 v38_A_unverified/)
    bin_unit_axi_wrap.v   0cf68b2e31ac106ed03e30bd5591504d   (Fix E')

## bit / 判据

    board/overlay_engine_150mhz/design_1_150v39.bit   md5 602725b86941864eeeeab3be005e1e81  3,510,798 B
    board/overlay_engine_150mhz/build_v39_manifest.txt  ← bit↔源码 md5 清单（md5sum -c 全成功）

## 已跑过的验证（全部本机实测）

    xvlog --sv 两个文件                     0 ERROR
    synthcheck.sh                          多驱动 0 / 组合环 0 / 8-4767 4（与 v38 前完全同集合）/ ERROR 0
    Verify all wrap 12 shapes              12/12 PASS，逐字节 0 差
    E1E2 直接挂单元                        106/106 命令、24 层、6,302,720 B、0 差、cycles 7,244,033
    E1E2 via wrapper UNIT=0/4              0/512 + 0/512 差，4/4 + 2/2 PASS
    227 层离线 A/B（--check-ab）           227/227 层 ref/numpy/C 三列 sha1 与 ab_pack256.json 一致

## v39 时序

    WNS -1.998 ns  TNS -9288.019  违例端点 7109（v38 是 -2.080 / -11392.258 / 8820）
    资源 LUT 20552 / FF 18963 / BRAM 35 / DSP 101 / Latch 0
