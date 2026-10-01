# AX7015B PS7 配置补全脚本（Vivado 批量模式）
#
# 用途
# ----
# 工作区里那份 `xilinx_project/moro_v1/design_1_wrapper.xsa` 曾经只配了
# DDR + UART1，导致 PetaLinux 生成的设备树里 **SD 卡、网口、USB、QSPI 全是
# status="disabled"**，镜像根本无法从 SD 卡启动。本脚本按
# `AX7015B_LINUX教程_2025.2版/PetaLinux_创建流程_XSA到上板.md` 第 1.3 节的
# 配置表把 PS7 补全，并重新导出 XSA。
#
# 用法
# ----
#   /home/zheyang/AMD/2025.2/Vivado/bin/vivado -mode batch \
#       -source ax7015b_ps7_complete.tcl -nojournal -log /tmp/ps7.log
#
# 之后会：
#   1) 覆盖 xilinx_project/moro_v1/design_1_wrapper.xsa
#   2) 把新的 XSA 复制到 <构建根>/hardware/（该目录只能有一个 xsa）
#      构建根默认 /home/zheyang/SRP/qiansaiFPGA/peta_prj，见 deploy/env.sh
#   3) 提示你接着跑 petalinux-config / petalinux-build
#
# ★ 顺序陷阱（踩过）
#   `PCW_ENET0_RESET_ENABLE/IO` 必须在 `PCW_GPIO_MIO_GPIO_ENABLE=1` **之后**设置。
#   反过来的话 GPIO 会把 MIO7 抢回去，ENET0 复位被静默回退成 0，
#   结果 PHY 复位无人驱动 —— 网口可能起不来。

set XPR   /home/zheyang/SRP/qiansaiFPGA/xilinx_project/moro_v1/moro_v1.xpr
set XSA   /home/zheyang/SRP/qiansaiFPGA/xilinx_project/moro_v1/design_1_wrapper.xsa
set HWOUT /home/zheyang/SRP/qiansaiFPGA/peta_prj/hardware

open_project $XPR
set bdf [get_files design_1.bd]
open_bd_design $bdf
set ps [get_bd_cells processing_system7_0]

# ── 1) 外设使能与 MIO 分配（依据教程 1.3 节的表）──────────────────────
set cfg [list \
    CONFIG.PCW_EN_QSPI                    {1} \
    CONFIG.PCW_QSPI_PERIPHERAL_ENABLE      {1} \
    CONFIG.PCW_QSPI_QSPI_IO                {MIO 1 .. 6} \
    CONFIG.PCW_QSPI_GRP_SINGLE_SS_ENABLE   {1} \
    CONFIG.PCW_QSPI_GRP_SINGLE_SS_IO       {MIO 1 .. 6} \
    CONFIG.PCW_EN_SDIO0                    {1} \
    CONFIG.PCW_SD0_PERIPHERAL_ENABLE       {1} \
    CONFIG.PCW_SD0_SD0_IO                  {MIO 40 .. 45} \
    CONFIG.PCW_SD0_GRP_CD_ENABLE           {1} \
    CONFIG.PCW_SD0_GRP_CD_IO               {MIO 10} \
    CONFIG.PCW_EN_SDIO1                    {1} \
    CONFIG.PCW_SD1_PERIPHERAL_ENABLE       {1} \
    CONFIG.PCW_SD1_SD1_IO                  {MIO 46 .. 51} \
    CONFIG.PCW_EN_ENET0                    {1} \
    CONFIG.PCW_ENET0_PERIPHERAL_ENABLE     {1} \
    CONFIG.PCW_ENET0_ENET0_IO              {MIO 16 .. 27} \
    CONFIG.PCW_ENET0_GRP_MDIO_ENABLE       {1} \
    CONFIG.PCW_ENET0_GRP_MDIO_IO           {MIO 52 .. 53} \
    CONFIG.PCW_EN_USB0                     {1} \
    CONFIG.PCW_USB0_PERIPHERAL_ENABLE      {1} \
    CONFIG.PCW_USB0_USB0_IO                {MIO 28 .. 39} \
    CONFIG.PCW_EN_GPIO                     {1} \
    CONFIG.PCW_GPIO_PERIPHERAL_ENABLE      {1} \
    CONFIG.PCW_GPIO_MIO_GPIO_ENABLE        {1} \
]
foreach {k v} $cfg {
    if {[catch {set_property $k $v $ps} err]} { puts "FAIL  $k = $v -> $err" } \
    else { puts "OK    $k = $v" }
}

# ── 2) ENET0 PHY 复位（MIO7）—— 必须在 GPIO 之后 ──────────────────────
set_property CONFIG.PCW_ENET0_RESET_ENABLE {1} $ps
set_property CONFIG.PCW_ENET0_RESET_IO     {MIO 7} $ps
puts "ENET0 复位: ENABLE=[get_property CONFIG.PCW_ENET0_RESET_ENABLE $ps] IO=[get_property CONFIG.PCW_ENET0_RESET_IO $ps]"

# ── 3) 校验 ───────────────────────────────────────────────────────────
validate_bd_design
save_bd_design

puts "=== 结果：PCW_EN_* = 1（应含 DDR/CLK0/RST0/QSPI/SDIO0/SDIO1/ENET0/USB0/GPIO/UART1）==="
foreach p [lsort [list_property $ps]] {
    if {[string match "CONFIG.PCW_EN_*" $p] && [get_property $p $ps] == 1} {
        puts "  [string map {CONFIG. {}} $p]"
    }
}

# ── 4) 重新生成输出产物再导出 ─────────────────────────────────────────
# 关键：只改 BD 就 write_hw_platform，导出的 HWH 会是**上次生成的缓存**，
# 外设改动不会体现。必须先 reset_target + generate_target 重新生成 HWH。
reset_target all $bdf
generate_target all $bdf
save_bd_design

file mkdir $HWOUT
file delete -force $XSA
write_hw_platform -fixed -force -file $XSA
puts "导出完成: $XSA ([file size $XSA] 字节)"

# ── 5) 同步到 PetaLinux 硬件目录（该目录只能有一个 xsa）────────────────
foreach f [glob -nocomplain $HWOUT/*.xsa] { file delete -force $f }
file copy -force $XSA $HWOUT/
puts "已复制到 $HWOUT/"

puts "\n下一步（在 bash 里）："
puts "  source /opt/pkg/petalinux/settings.sh"
puts "  source /home/zheyang/AMD/2025.2/Vitis/settings64.sh"
puts "  cd /home/zheyang/SRP/qiansaiFPGA/peta_prj/ax_peta"
puts "  petalinux-config --get-hw-description ../hardware/ --silentconfig"
puts "  # ★ 之后必须检查 project-spec/configs/config："
puts "  #   --get-hw-description 会把外设选择重置成 MANUAL_SELECT，"
puts "  #   且不会自动开 DHCP，导致板上 eth0 没有 IP 配置。"
puts "  #   需手工改回（或参照同目录 README.md 的命令）。"
puts "  petalinux-build"
puts "  petalinux-package boot --u-boot --force"
