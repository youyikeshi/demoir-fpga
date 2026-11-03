# T-157：PF_MAX 截断闸门合入主线 —— 交付记录

时间：2026-11-03 12:26–12:30（主机侧，未上板）

## 0. 结论：**合入成功**

| 项 | 结果 |
|---|---|
| 补丁 vs 已验版本（rtl_guard） | **逐行一致**（`diff -u T-152 基线 vs rtl_guard` 与 `patch_pfmax_guard.diff` 同一 hunk，24 行） |
| 主线文件 md5 | 旧 `17164dfc3029ace1cbcb9eaa08af6213` → 新 **`f376f47de7cf53ce65f4f5877d84ce95`** |
| 改动量 | **+15 行 / -0 行**（只有 `initial` 断言块，零逻辑改动） |
| 备份 | `/tmp/wrap_t157.bak`（md5=17164dfc…，与旧主线字节相同） |

## 1. 三条检查（主线 `bin_unit_axi_wrap_v50.v`，行号实测）
- ① `:3326` `if ((PF_MAX < 0) || (PF_MAX > 7)) $fatal(...)` → **实测触发**：`PF_MAX=8` ⇒
  `Fatal: [BIN-WRAP] PF_MAX(8) 必须在 0..7 …`（Time: 0 ps，`neg_pfmax8/run.log`）
- ② `:3329` `PF_DEPTH` 必须是 ≥1 的 2 的幂 → **实测触发**：`PF_DEPTH=3` ⇒
  `Fatal: [BIN-WRAP] PF_DEPTH(3) 必须是 >=1 的 2 的幂`
- ③ `:3331` `PF_EN!=0 && PF_MAX > NSLOT-AR_SLOTS-1` → **实测触发**：`NSLOT=8 AR_SLOTS=4 PF_MAX=4` ⇒
  `Fatal: [BIN-WRAP] PF_MAX(4) > NSLOT-AR_SLOTS-1(=3) …`

默认参数 `NSLOT=16 / AR_SLOTS=4 / PF_MAX=4 / PF_DEPTH=4` ⇒ `4 <= 11`，正常配置不误杀（A/B 六次仿真全部 PASS 佐证）。

## 2. 四道门禁
1. `xvlog --sv`（`-L xpm`）：**rc=0，ERROR=0**（`xvlog/xvlog.log`）
2. manifest 刷新：第 101 行用 `md5sum` 实取值改为 `f376f47d…`（未手写）
3. `verify_rtl_root.sh runs/timing_v50/rtl_v50_manifest.txt runs/timing_v50/rtl_v50`
   ⇒ **MATCH=35 DIFF=0 EXTRA=0 MISSING=0 RESULT: PASS**
   - 注：`patch` 因 hunk offset 自动留了 `lane16/bin_unit_axi_wrap_v50.v.orig`（md5 与备份相同），
     已删除；这是第一次 verify 报 EXTRA=1 的唯一原因，删后 PASS。
4. `python3 runs/v106_audit/inv_lint.py runs/timing_v50/rtl_v50/lane16 --shapes runs/v105_stride2/golden`
   ⇒ **PASS 15 / FAIL 7 / WARN 3**，与基线逐条相同（FAIL 未增加，`inv_lint.log`）

## 3. 功能 A/B（TB：`runs/v94_wrpath/tb_v94_wrap.v`，默认 seed=0x12345678，确定性）
A = 补丁前（`rtlA`，wrap=17164dfc…），B = 补丁后（`rtlB`，wrap=f376f47d…），各独立 xvlog/xelab。

| VLANES | 形状 | 笔数 | 拍数 A/B | (addr,data,sel) |
|---|---|---|---|---|
| 8 | v94_c48_o16_8x8 | 128 | 1098 / 1098 | **IDENTICAL** |
| 8 | v94_c64_o64_8x8 | 512 | 6853 / 6853 | **IDENTICAL** |
| 8 | v94_c48_o64_32x32 | 8192 | 76028 / 76028 | **IDENTICAL** |
| 16 | v94_c48_o16_8x8 | 128 | 1707 / 1707 | **IDENTICAL** |
| 16 | v94_c64_o64_8x8 | 512 | 5274 / 5274 | **IDENTICAL** |
| 16 | v94_c48_o64_32x32 | 8192 | 55774 / 55774 | **IDENTICAL** |

`[TB-SUMMARY]`、`统计:`、`DBG_RD/DBG_WR`、`覆盖:`、`PASS/FAIL` 行逐行一致（6/6）。仿真自身 status=PASS、rd_bad=0、axi_errors=0。

## 4. 改动面
- 改：`runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v`、`runs/timing_v50/rtl_v50_manifest.txt`（第 101 行）
- 新增：`runs/v157_guard/`（A/B RTL 副本、执行脚本、日志、负向测试）
- 未动约束 / BD / 其他 RTL；未上板、未 ssh；全程 `memgate.sh … --reserve 2.5`，`pgrep -c vivado` 初始为 0

## 5. 回滚
`cp -p /tmp/wrap_t157.bak runs/timing_v50/rtl_v50/lane16/bin_unit_axi_wrap_v50.v`
并把 manifest 第 101 行改回 `17164dfc3029ace1cbcb9eaa08af6213`。
