# T-101 合入说明（**我没有动线上 RTL 根**，合入由父代理执行）

线上根 `runs/timing_v50/rtl_v50/` 当前状态：35/35 与 `runs/v101_pipe/rtl_before/` **逐字节相同**
（`verify_rtl_root.sh runs/timing_v50/rtl_v50_manifest.txt runs/timing_v50/rtl_v50` → `MATCH=35 DIFF=0`）。
⇒ 合入 = **拷 1 个文件 + 刷 1 条 md5**，没有第二处改动：

```bash
cd /media/felix/Data/coding/demoir-fpga
# ① 拷（唯一改动的文件）
cp runs/v101_pipe/rtl_after/lane16/bin_conv_core_v50.v \
   runs/timing_v50/rtl_v50/lane16/bin_conv_core_v50.v
# ② 刷 manifest 里那一条 md5（其余 34 条不动）
NEW=$(md5sum runs/timing_v50/rtl_v50/lane16/bin_conv_core_v50.v | cut -d' ' -f1)   # c04302ce08a80e219233f4b220a26747
sed -i "s|^[0-9a-f]\{32\}  lane16/bin_conv_core_v50.v\$|$NEW  lane16/bin_conv_core_v50.v|" \
   runs/timing_v50/rtl_v50_manifest.txt
# ③ 判据
bash pynq_specialized_code/pl/scripts/verify_rtl_root.sh \
     runs/timing_v50/rtl_v50_manifest.txt runs/timing_v50/rtl_v50   # 期望 MATCH=35 DIFF=0 EXTRA=0 MISSING=0
```

⚠ 合入前请确认 **T-99 不在改同一个根**（它 21:20 改过 `bin_unit_axi_wrap_v50.v`，
21:33/21:41 两次 place 均"已终止"，之后 wrap 已被还原成 `f1ff43a5` = v51 基线）。
我全程**没有写** `runs/timing_v50/` 下任何文件（只读）。

## 位流可出性

- RTL 等价性：**已证到 3 层**（定向/随机 TB 逐字节、核级 24 组日志逐行相同、网表指纹 act_q=64/r2=1/l_qd=8）
- 时序（place）：WNS **+0.515 ns** @10 ns；route 后预计 **≈+0.42~+0.45**（按基线实测 place→route −0.089 换算）
- hold：place 阶段 −1.452（基线 −1.479 同量级，属 **布线前正常**；基线 route 后 hold = +2.321，由布线器修）
⇒ **可以进入 route/bitstream**，但 route 后必须复核两件：① WNS ≥ 0；② 旧锥 `win_q_reg→s1_pc_q` 仍无路径。
