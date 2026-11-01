# 冻结基线 v39 —— 8.096 s @150 MHz（2026-10-31 18:34，上板实测）

## 一句话
原 8.4 s 基线（v33 @100 MHz）被 **v39 @150 MHz = 8.096 s** 取代。
功能判据 **54/54 sha1 全中**，与 v33 逐位等价。

## 文件指纹

| 文件 | md5 | 大小 |
|---|---|---:|
| `design_1_150v39.bit` | `602725b86941864eeeeab3be005e1e81` | 3,510,798 B |
| `design_1_150v39.hwh` | `c1686bd8e080c1c7f46181d980543202` | 245,850 B |
| `bin_net.py` | `3f154b75f6526a096f78001aaa1e785b` | — |
| `bin_engine.py` | `ddfe6b6ffdfea33e76959748825d7af3` | — |
| `bin_ref_numpy.py` | `16efab6086c4a9cbb30501a67230504e` | — |
| `q8.py` | `e794a43dcb8ce314f323a9b07c4274a6` | — |
| `q8.c` | `c744794c2dafc032ddc4dde3c05adb5b` | — |
| `qcache.py` | `d2dc2517f1bad49ab6c61f0c3ae96944` | — |

RTL: `bin_e1e2.v 28375eec288f`（Fix A）+ `bin_unit_axi_wrap.v 0cf68b2e31ac`（Fix E′）
+ `build_final_150v39.tcl`（catpack 内部 false_path，覆盖 1 模块 cell + 1621 内部 FF）
时序：WNS **−1.998 ns @10 ns**，TNS −9288，7109 setup 违例端点，**Latch 0**
资源：LUT 20552 (44.48%) / FF 18963 / BRAM 35 / DSP 101

## 开关
```
BIN_LAZY_DEQ=1   BIN_LAZY_SLOTS=24   Q8CACHE=0
```

## 判据（sha1）
```
256^2 : 9b1940c8955b / f86a7035621b / 025757a83c39     (17 次运行 51/51)
64^2  : bfa246d34681 / 226bd39e522a / 762d84b9618e     (3/3)
输入 : x0 sha1 = cfc75bc3f15d306b  (与 v33 基线一致)
```

## 实测时间（热态）
| 频率(读回) | 整网 | PL |
|---:|---:|---:|
| 100 MHz | 8.508 s | 3.573 |
| 125 MHz | 8.399 s | 3.443 |
| **150 MHz** | **8.096 s** | **3.061** |
| 166.667 MHz | 8.146 s | 3.096 |

分桶 @100 MHz：prologue 1.604 / 非卷积 3.606 / epilogue 0.265 / overlap 1.691 / wait 0.873

## 已知约束
1. **PL 线性分解 @150 MHz = 1.536 s（算力）+ 2.037 s（访存）**，访存占 57%
   ⇒ 抬时钟收益迅速消失；省时间必须减访存，这就是 V2 行带融合的理由
2. **冷启动 +1.6 s**（新进程首次 10.06~10.35 s，prologue 2.66 vs 1.60）
   ⇒ 首帧不能当稳态数字
3. **160 MHz 以上不许作工作点**：请求 175 读回 166.667，且 PL 反而回升
4. `.hwh` 与 v33 逐字节相同（BD 自 v2 未变）⇒ 可复用但**必须验地址**
   `conv_uni_0=0x40040000` / `bin_unit_0=0x40060000`
5. `board/board_bundle/design_1.hwh` 是**旧版错文件**（只有 0x40000000/0x40010000），不许用
6. 板端看门狗 `wdt_arm.py` 在跑时**不许 kill**，会触发重启

## 上板操作纪律
- 必须 `source /etc/profile.d/xrt_setup.sh`
- 一个 session 只真 load 一次 overlay，之后 `--no-download`
- 只许 SIGTERM，绝不 `kill -9` PYNQ 进程
- `pkill -f` 的 pattern 不许匹配到自己的命令行
