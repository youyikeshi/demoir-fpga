# tb —— 测试台：谁在用、怎么编、跑出什么算过

命令一律**在 `pl/` 目录下执行**（工作区里是 `01_hls/`，相对路径同名，所以这份
说明两边通用）。编译产物丢 `build/`（已 .gitignore）。

一个前提：**用 g++ 编就不要加 `-DUSE_HLS_TYPES`**。加了会去 include `ap_int.h`，
那是 Vitis HLS 才有的头文件，g++ 找不到。不带它，`conv2d_accel.h` 会退化用
`int8_t`/`int32_t`，算法语义不变（见该头文件顶部的双环境说明）。

---

## 一、现役：改电路后必跑

### `tb_engine.cpp` —— 主测试台

单引擎（`conv2d_engine`）vs C 参考（`conv_ref_rt`）逐元素对拍，形状全走运行期参数。
**改了 `src/conv2d_engine.cpp` 或 `src_cosim/` 就必须跑它。**

编译（一行，别换行）：

    g++ -std=c++14 -O2 -I src src/conv2d_engine.cpp src/conv_ref_rt.cpp tb/tb_engine.cpp -o build/tb_engine.exe

跑：

    python scripts/fuzz_shapes.py                       # 随机形状回归：24 正例 + 4 个非法参数负例，自动造数据
    build/tb_engine.exe <数据目录> <cin> <cout> <k> <stride> <dil> <pad> [relu]

单形状跑之前，数据要用 `scripts/gen_testdata.py` 生成（见该脚本 `--help`）。
退出码：0 = 一致，1 = 不一致，2 = 参数/文件有问题。

### `conv2d_accel_tb.cpp` —— 早期内核测试台（也是 csim 的测试台）

测的是早期按形状特化的 `conv2d_accel`，被 `scripts/hls_run.tcl`、
`scripts/dse_scan.tcl`、`scripts/run_shape_test.ps1` 引用。要带形状宏编：

    g++ -std=c++14 -O2 -I src -DCIN=12 -DCOUT=48 -DKSIZE=5 -DPAD=2 -DDILATION=1 -DSTRIDE=1 tb/conv2d_accel_tb.cpp src/conv2d_accel.cpp src/conv_ref.cpp -o build/tb_shape.exe

懒得手敲就用 `scripts/run_shape_test.ps1`（它负责：生成数据 → 编译 → 跑）。

---

## 二、历史单元测试：不参与主流程，但都还能编能跑

留着当回归网与"当时怎么验证的"证据。它们没有脚本引用，**要手动编**：

| 文件 | 测什么 | 链接哪些源 | 特殊要求 |
|---|---|---|---|
| `test_edge_cases.cpp` | 边界与错误码：W 超上限、空指针、shift 越界、1×1 核… | `conv2d_accel.cpp` **+** `conv_ref.cpp` | — |
| `test_dilation_handcalc.cpp` | 膨胀卷积手算：专抓 `2*ky` 写成 `ky` 这类变异 | `conv2d_accel.cpp` | **必须按它文件头的宏编**：`-DCIN=1 -DCOUT=1 -DKSIZE=3 -DPAD=2 -DDILATION=2` |
| `test_ref_handcalc.cpp` | 参考实现手算用例 | `conv_ref.cpp` | — |
| `test_types.cpp` | 数据类型/位宽自检 | 无（纯编译期） | 不依赖形状宏 |
| `test_engine_fuzz.cpp` | 引擎的 C++ 随机回归：**W 到 260、H 到 25**（python 版只到 9） | `conv2d_engine.cpp` + `conv_ref_rt.cpp` | 无参数即跑；可选第一个参数给随机种子 |

## 三、四个坑（每个都真踩过）

1. `test_edge_cases` 只链 `conv2d_accel.cpp` → `undefined reference to conv_ref(...)`：
   `conv_ref()` 在单独的 `conv_ref.cpp` 里，要一起链。
2. `test_dilation_handcalc` 用默认形状宏编 → `FAIL: out(0,0) = 0，期望 2`：
   它构造的数据是 5×5、核 3、膨胀 2，宏不对必然失败。
3. 忘写 `-I src` → 找不到 `conv2d_accel.h`。
4. **改硬件容量（`CIN_MAX`/`COUT_MAX`）后，必须同步改 `test_engine_fuzz.cpp`
   里那两条"应被拒绝"的通道用例**。它原来写 `cin=113`/`cout=97`（v2 的上界
   112/96）；v3 扩到 352/192 后，这两个值变成**合法参数**，引擎就真的去读写
   测试里那几个只按旧上界分配的小缓冲区 → 越界写、堆损坏、进程直接崩
   （0xC0000374）。2026-10-16 已改成 353/193 并复跑通过。
   顺带记住：`build/` 里的旧 exe 可能是**改动前**编的，别拿它的 PASS 当结论
   —— 有一个"2256 用例全过"的 exe 就是这么骗过我们的，重建后其实是崩的。

---

## 四、验证记录（2026-10-16，MinGW g++ 8.1.0）

| 测试台 | 编译 | 运行结果 |
|---|---|---|
| `tb_engine.cpp` | ✅ | 随机形状 **28/28 PASS**；真实数据 5 场景 **5/5 PASS**（352×192 最坏形状 12288 个输出逐位一致） |
| `conv2d_accel_tb.cpp` | ✅ | 由 `run_shape_test.ps1` 驱动（csim 侧另有覆盖） |
| `test_edge_cases.cpp` | ✅ | PASS（边界测试全部通过） |
| `test_dilation_handcalc.cpp` | ✅ | PASS（中心抽头落在 (0,0)，纵向与横向跨步都对） |
| `test_ref_handcalc.cpp` | ✅ | PASS |
| `test_types.cpp` | ✅ | 通过（退出码 0） |
| `test_engine_fuzz.cpp` | ✅ | PASS（2256 个用例 / 1.9s；修掉旧通道上界后复跑） |

结论：**7 个测试台都编得过、跑得过**（其中 `test_engine_fuzz` 的通道上界
2026-10-16 修过一次），所以它们跟着源码一起进 git（整个 `tb/` 才 26 KB）。
