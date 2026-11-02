# T-88 —— band 通路接线（契约 B 最终定稿 §13）一次接对

产物目录 `runs/v88_wiring/`：`wrap.diff`、`W3_W4_evidence.txt`、`build.sh`、
`sim/`（xvlog/xelab 日志）、`verify_strict.log`、`md5_before/after.txt`、
`bin_unit_axi_wrap_v50.v.before`、`rtl_v50_manifest.txt.before`。

## 1. 三件（+ 两处必要的连带修正）

| # | 文件 | 行 | 内容 |
|---|---|---|---|
| ① | `lane16/bin_band_fill_v50.v`（**新增**，从 IMP-B2 移植）| 端口 56-61 / 用点 84-90 | 字段来源按 §13：`w_row ← w4`、`cfg_h ← w3[15:0]`、`h_in ← w3[31:16]`、`nband ← w9[15:0]`；`rows_fill = cfg_h + 2*halo`（硬件算）；`yr = y0+r−HALO`，`yr<0 || yr≥H_in ⇒ 整行写 0、不发 DMA`。**删掉** IMP-B2 的"w3 低 16 = W" |
| ② | `lane16/bin_unit_axi_wrap_v50.v` | 1376 | `c_cfg_h = fuse_m ? seg_nband : r_h[15:0]`（= w9 低 16；**不再是 seg_h = H_in**）|
| ③ | 同上 | 1800-1818 | 例化 `bin_band_fill_v50 u_fill`：`en=u_ce&&fuse_m`、`sop=fuse_m&&S_CHK`、`ld_req=fuse_m&&seg_ld_req`；接 E 口 DMA（`seg_axi_rd_en/addr` 1597-1603）、band 写口（`.ld_q` 1737 改由填充器给字）、segq 的几何 |
| 连带 a | 同上 | 1368 / 1377 / 1640-1656 | `seg_w4 = seg_cout`（= **w4**，segq 沿用 v39 名字）；`c_cfg_w ← w4`（原为 seg_w = w3 低 16 = cfg_h ⇒ 行宽会变成 nband）；`band_words = ceil(cin/64)*fill_rows*seg_w4`（否则装载字数与填充器要写的字数不一致，带尾部写不满）|
| 连带 b | 同上 | 1820-1844 | `translate_off` 断言：`seg_w==seg_nband`、`c_cfg_h==seg_nband`、`seg_w4!=0`、`halo≥(k-1)/2`、**FUSE=0 时填充器不许发 DMA** |

`bin_segq.v` **一个字节没改**（md5 不变）。

## 2. 验收

| 判据 | 结果 |
|---|---|
| W1 `xvlog --sv`（lane16 10 文件 + 7 基线依赖）| rc=0，**0 error / 0 warning**（`sim/xvlog.log`）|
| W2 `xelab bin_unit_axi_wrap_bd_v50` | VLANES=**8** Built、VLANES=**16** Built；另 `bin_band_fill_v50` 单独 Built |
| W3 FUSE=0 结构未变 | diff 221 行 / 改 172 行：注释 111、fuse/fill 代码 46、**其余 15 行 = 填充器例化端口 + 断言块**；**10 根 `sel_fuse_q` mux 改动前后逐字相同**，被改的 6 根线 v39 右支一字未动 ⇒ **v39 逻辑被替换 0 行** |
| W4 静态追踪 | 见 `W3_W4_evidence.txt`（三条各贴 5 行）|
| W5 `verify_rtl_root.sh` 严格口径 | **MATCH=35 DIFF=0 EXTRA=0 MISSING=0 PASS**（34→35 是因为新增了 1 个 lane16 文件；`bin_band_buf.v` 的条目本来就没刷（T-83 改 §12.2 后），一并向 manifest 修好，否则 strict 永远 DIFF=1）。基线 25 条逐字节与 `v39_exact` 相同 |
| FUSE=0 静默 | 结构门控：`en/sop/ld_req` 全带 `fuse_m`，输出只进本来就被 `sel_fuse_q` 门控的 mux（`u_rd_en/u_rd_addr/segq.ld_q`）；band 写口 `.e_wr_en(fuse_m && seg_ld_we)`，而 `seg_ld_we` 只能在 `S_LOAD` 置起、`segq.kick = start_fire && fuse_m` ⇒ 不融合时 band_buf / DMA / 冲突位全静默 |

## 3. band 通路现在完整了吗？——**接线完整，功能还差 4 条**（下一步必做）

1. **conv 的 cfg_h 已对，但 `c_cfg_cout/k/s/d/pad` 仍取自 segq 的 v39 解码**（`w4→cout`、`w5→k/s/d/pad`）。
   §13 里 `w4=cfg_w`、`w5=cfg_group`，PS 的 `v50_desc` 也是这么填的 ⇒ 融合模式下 conv 的
   `cfg_k/s/d/pad/cout` 拿到的是 W/cfg_group。本轮只换了 band 通路必需的 `cfg_h/cfg_w`；
   剩下 4 根要么改 segq 解码（本任务禁改），要么按 §10.2 走段边界寄存器（需契约裁定）。
2. **`band_words` 仍是"装/排共用一个数"**：装带按填充器几何（`ceil(cin/64)·(nband+2·HALO)·W`）算，
   排空必须按 `cout` 算（面数 `ceil(cout/8)`），而 §13 描述符里没有 cout ⇒ 等第 1 条定下来再按相位拆。
3. **`band_words` 在 S_CHK 当拍用的是 segq 的 `d_*`**（要等 S_CHK 沿之后才更新）⇒ 第一条描述符
   会看到全 0 ⇒ `E_SIZE`。IMP-B2 用"包装层另存 5 拍描述符（`sh_dw`）"解决，本任务不动 segq。
4. **conv 读带的行偏移**：填充器把输入行 `y0−HALO` 放在带内第 0 行，而 conv 从 `base_act=0` 起读
   ⇒ k=3/5 需要 `base_act = HALO*W*8`（或核吃 pad）。不改核的话 k=1 的带内通路已自洽。

（也就是：**elaborates + 接线自洽 + FUSE=0 不变** 已达成；**带内端到端功能**仍未验，需 xsim。）
