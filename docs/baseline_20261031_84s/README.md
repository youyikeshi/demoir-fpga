# 冻结基线：8.4 s（2026-10-31）

## 这是新的起点，不是终点

    整网 256×256 = 8.4 s（板端逐位验证）
    起点 42.28 s ⇒ 5.03×
    目标 1.0 s ⇒ 还差 8.4×

## PL 侧

    board/overlay_engine_150mhz/design_1_150v33.bit   md5 999ca562bd7c  3,510,798 B
    board/overlay_engine_150mhz/design_1_150v33.hwh   md5 c1686bd8e080  245,850 B
    运行时：Clocks.fclk0_mhz = 100      ← 实际频率由运行时设，与约束无关

    .hwh 地址：conv_uni_0 = 0x40040000 ✓   bin_unit_0 = 0x40060000 ✓
    （主机与板端 md5 完全一致）

## PS 侧（贡献 −6.42 s 的主体）

    ps/pynq/bin_net.py         3f154b75f6526a096f78001aaa1e785b
    ps/pynq/bin_engine.py      ddfe6b6ffdfea33e76959748825d7af3
    ps/pynq/bin_ref_numpy.py   16efab6086c4a9cbb30501a67230504e
    ps/pynq/q8.py              e794a43dcb8ce314f323a9b07c4274a6
    ps/pynq/q8.c               c744794c2dafc032ddc4dde3c05adb5b
    ps/pynq/qcache.py          d2dc2517f1bad49ab6c61f0c3ae96944

## 三个关键开关

    BIN_LAZY_DEQ=1        惰性反量化（198 层跳过反量化）
    BIN_LAZY_SLOTS=24     输出槽池（16 槽 ⇒ 8.79 s，24 槽 ⇒ 8.30 s）
    Q8CACHE=0             实测无效，保持关
    BIN_CAT_FUSION        未设（随 LAZY_DEQ 自动开）

## 判据（sha1，必须逐位相同）

    256²: 9b1940c8955b / f86a7035621b / 025757a83c39
     64²: bfa246d34681 / 226bd39e522a / 762d84b9618e

## 8.4 s 的桶构成

    PL       3.557 s   conv 2.4 + E1E2 1.15
    prologue 1.585 s   f32 量化 0.8 + 回落 pack 1.0
    非卷积   3.608 s   convfp 1.18 + interp 1.96 + 其它 0.47
    epilogue 0.249 s   已净

## 已知约束

    · 64² 净亏 +0.72 s ⇒ 需按层字节阈值（≥128² 才开 E1E2）
    · 板端 CMA 只剩 33.8 MB ⇒ 256² f32 单块要 143 MB ⇒ 必须重启板子才能再测
    · xilinx 不在 render 组 ⇒ 跑 PYNQ 必须 sudo
    · 跑完要 SIGTERM 不能 kill -9（防 CMA 泄漏）
