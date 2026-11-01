# ⚠ 本目录下的 RTL 是**陈旧副本**，不要用来做架构判断

`ip_repo/*/src/` 里的 RTL 由打包脚本从 `pl/rtl_hand/modules/` 拷过来，**不保证同步**。
2026-11-02 实测：

| 文件 | `pl/rtl_hand/modules/`（**权威**）| `ip_repo/bin_unit_axi_wrap_bd/src/`（**陈旧**）|
|---|---|---|
| `bin_conv_core.v` | 2381 B 薄壳，第 33 行例化 `bin_conv_core8`（`VLANES=8` ⇒ **512 二进制 MAC/拍**）| 旧 **1-lane** 版（`output logic [7:0] mem_wr_data`）⇒ 按它会算出 **64 MAC/拍**，差 8 倍 |

⇒ 判读顺序：**`pl/rtl_hand/modules/` → `pl/scripts/build_final_150v39.tcl`（第 37 行 `set rtl $pl/rtl_hand/modules`）→ 才是构建产物**。
⇒ 本目录只用于 Vivado IP 打包，**不是**"当前设计"的证据。
