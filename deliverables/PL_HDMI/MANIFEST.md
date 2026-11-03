# 打包清单（自动生成，勿手改）

| 项 | 值 |
|---|---|
| 生成时间 | 2026-11-03 16:46:31 +0800 |
| git 分支 | `restructure1` |
| git commit | `78e797d` |
| 文件数 | 30 |
| 重建命令 | `tools/pack_hdmi_pl.sh` |
| 校验命令 | `tools/pack_hdmi_pl.sh --check` |

## 源路径对照

| 包内路径 | 权威源 |
|---|---|
| `rtl/hdmi_out_top.v` | `pynq_specialized_code/pl/rtl_hand/modules/hdmi_out_top.v` |
| `rtl/hdmi_timing_gen.v` | `pynq_specialized_code/pl/rtl_hand/modules/hdmi_timing_gen.v` |
| `rtl/hdmi_color_bar.v` | `pynq_specialized_code/pl/rtl_hand/modules/hdmi_color_bar.v` |
| `rtl/sil9134_init.v` | `pynq_specialized_code/pl/rtl_hand/modules/sil9134_init.v` |
| `rtl/hdmi_fb_scan.v` | `pynq_specialized_code/pl/rtl_hand/modules/hdmi_fb_scan.v` |
| `rtl/bd/hdmi_p1_top.v` | `pynq_specialized_code/pl/vivado/hdmi_p1/rtl/hdmi_p1_top.v` |
| `tb/tb_hdmi_timing.v` | `pynq_specialized_code/pl/rtl_hand/tb/tb_hdmi_timing.v` |
| `tb/tb_sil9134_i2c.v` | `pynq_specialized_code/pl/rtl_hand/tb/tb_sil9134_i2c.v` |
| `tb/tb_hdmi_top_cfg.v` | `pynq_specialized_code/pl/rtl_hand/tb/tb_hdmi_top_cfg.v` |
| `tb/tb_hdmi_top_scan.v` | `pynq_specialized_code/pl/rtl_hand/tb/tb_hdmi_top_scan.v` |
| `tb/tb_hdmi_fb_scan.v` | `pynq_specialized_code/pl/rtl_hand/tb/tb_hdmi_fb_scan.v` |
| `constraints/hdmi_out_pins.xdc` | `pynq_specialized_code/pl/constraints/hdmi_out_pins.xdc` |
| `constraints/hdmi_out_ooc.xdc` | `pynq_specialized_code/pl/constraints/hdmi_out_ooc.xdc` |
| `constraints/hdmi_p1_clocks.xdc` | `pynq_specialized_code/pl/vivado/hdmi_p1/constraints/hdmi_p1_clocks.xdc` |
| `constraints/p1_clock_groups.tcl` | `pynq_specialized_code/pl/vivado/hdmi_p1/constraints/p1_clock_groups.tcl` |
| `scripts/build_hdmi_p1.tcl` | `pynq_specialized_code/pl/vivado/hdmi_p1/build_hdmi_p1.tcl` |
| `scripts/run_hdmi_p1_sim.sh` | `pynq_specialized_code/pl/rtl_hand/tools/run_hdmi_p1_sim.sh` |
| `scripts/run_hdmi_scan_sim.sh` | `pynq_specialized_code/pl/rtl_hand/tools/run_hdmi_scan_sim.sh` |
| `scripts/synthcheck_hdmi_p1.sh` | `pynq_specialized_code/pl/scripts/synthcheck_hdmi_p1.sh` |
| `scripts/ooc_hdmi_p1.sh` | `pynq_specialized_code/pl/scripts/ooc_hdmi_p1.sh` |
| `reports/util.rpt` | `runs/hdmi_p1/util.rpt` |
| `reports/util_hier.rpt` | `runs/hdmi_p1/util_hier.rpt` |
| `reports/timing.rpt` | `runs/hdmi_p1/timing.rpt` |
| `reports/clocks.rpt` | `runs/hdmi_p1/clocks.rpt` |
| `reports/drc.rpt` | `runs/hdmi_p1/drc.rpt` |
| `reports/gate_summary.txt` | `runs/hdmi_p1/gate_summary.txt` |
| `reports/BD_NOTES.md` | `runs/hdmi_p1/BD_NOTES.md` |
| `reports/build_manifest.txt` | `runs/hdmi_p1/build_manifest.txt` |
| `docs/05_HDMI输出_PS_PL接口契约.md` | `docs/camera/05_HDMI输出_PS_PL接口契约.md` |
| `docs/03_显示构图规范_纯PS.md` | `docs/camera/03_显示构图规范_纯PS.md` |
