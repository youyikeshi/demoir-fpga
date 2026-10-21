# build_ps7_xsa.tcl —— 从零生成带 S_AXI_HP0 的 PS7 XSA（用于重建 BOOT.BIN）
#
# 背景：基础镜像的 PS7 没有启用 S_AXI_HP0（实测 BOOT.BIN 里 AFI 寄存器 0 处引用），
# PL 在 PYNQ 里读不到 DDR（卷积输出全 0）。本脚本按板卡手册
# （DDR3 = 2×H5TQ4G63AFR-PBI，兼容 MT41J256M16RE-125，32bit / 1066Mbps）
# 与 deploy/xsa/README.md 的 MIO 表从零建 PS7，并打开 HP0。
#
# 用法（任意目录）：
#   vivado -mode batch -source deploy/xsa/build_ps7_xsa.tcl
# 产物：
#   /data/work/ps7_rebuild/ps7_only.xsa   （给 Vitis 生成 FSBL 用）
#
# 说明：本 XSA 只服务 BOOT.BIN/FSBL 重建；overlay 的 bit/hwh 由
#       pynq_specialized_code/pl/scripts/ 下的脚本生成，与此无关。

set out_dir /data/work/ps7_rebuild
set proj    $out_dir/proj_ps7
file mkdir $out_dir

create_project -force proj_ps7 $proj -part xc7z015clg485-2
create_bd_design ps7_only
create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7 ps7
set ps [get_bd_cells ps7]

set cfg [list \
    CONFIG.PCW_UIPARAM_DDR_PARTNO          {MT41J256M16 RE-125} \
    CONFIG.PCW_UIPARAM_DDR_BUS_WIDTH       {32 Bit} \
    CONFIG.PCW_UIPARAM_DDR_FREQ_MHZ        {533.333333} \
    CONFIG.PCW_DDR_RAM_HIGHADDR            {0x3FFFFFFF} \
    CONFIG.PCW_APU_PERIPHERAL_FREQMHZ      {666.666667} \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ    {50} \
    CONFIG.PCW_EN_CLK0_PORT                {1} \
    CONFIG.PCW_EN_RST0_PORT                {1} \
    CONFIG.PCW_USE_M_AXI_GP0               {1} \
    CONFIG.PCW_USE_S_AXI_HP0               {1} \
    CONFIG.PCW_S_AXI_HP0_DATA_WIDTH        {64} \
    CONFIG.PCW_EN_QSPI                     {1} \
    CONFIG.PCW_QSPI_PERIPHERAL_ENABLE      {1} \
    CONFIG.PCW_QSPI_QSPI_IO                {MIO 1 .. 6} \
    CONFIG.PCW_QSPI_GRP_SINGLE_SS_ENABLE   {1} \
    CONFIG.PCW_QSPI_GRP_SINGLE_SS_IO       {MIO 1 .. 6} \
    CONFIG.PCW_EN_SDIO0                    {1} \
    CONFIG.PCW_SD0_PERIPHERAL_ENABLE       {1} \
    CONFIG.PCW_SD0_SD0_IO                  {MIO 40 .. 45} \
    CONFIG.PCW_SD0_GRP_CD_ENABLE           {1} \
    CONFIG.PCW_SD0_GRP_CD_IO               {MIO 10} \
    CONFIG.PCW_EN_UART1                    {1} \
    CONFIG.PCW_UART1_PERIPHERAL_ENABLE     {1} \
    CONFIG.PCW_UART1_UART1_IO              {MIO 12 .. 13} \
    CONFIG.PCW_EN_SDIO1                    {1} \
    CONFIG.PCW_SD1_PERIPHERAL_ENABLE       {1} \
    CONFIG.PCW_SD1_SD1_IO                  {MIO 46 .. 51} \
    CONFIG.PCW_EN_ENET0                    {1} \
    CONFIG.PCW_ENET0_PERIPHERAL_ENABLE     {1} \
    CONFIG.PCW_ENET0_ENET0_IO              {MIO 16 .. 27} \
    CONFIG.PCW_ENET0_GRP_MDIO_ENABLE       {1} \
    CONFIG.PCW_ENET0_GRP_MDIO_IO           {MIO 52 .. 53} \
    CONFIG.PCW_ENET0_PERIPHERAL_FREQMHZ    {1000 Mbps} \
    CONFIG.PCW_EN_USB0                     {1} \
    CONFIG.PCW_USB0_PERIPHERAL_ENABLE      {1} \
    CONFIG.PCW_USB0_USB0_IO                {MIO 28 .. 39} \
    CONFIG.PCW_USB0_PERIPHERAL_FREQMHZ     {60} \
    CONFIG.PCW_EN_GPIO                     {1} \
    CONFIG.PCW_GPIO_PERIPHERAL_ENABLE      {1} \
    CONFIG.PCW_GPIO_MIO_GPIO_ENABLE        {1} \
]
foreach {k v} $cfg {
    if {[catch {set_property $k $v $ps} err]} {
        puts "FAIL  $k = $v -> $err"
    } else {
        puts "OK    $k = $v"
    }
}

# ENET0 PHY 复位（MIO7）——必须在 GPIO 之后（见 deploy/xsa/README.md 的第二个坑）
set_property CONFIG.PCW_ENET0_RESET_ENABLE {1} $ps
set_property CONFIG.PCW_ENET0_RESET_IO     {MIO 7} $ps
puts "ENET0 复位: ENABLE=[get_property CONFIG.PCW_ENET0_RESET_ENABLE $ps] IO=[get_property CONFIG.PCW_ENET0_RESET_IO $ps]"

# AXI 时钟引脚（PS7-only 设计没有 PL 时钟源，接 FCLK_CLK0；否则 validate 报 41-758）
connect_bd_net [get_bd_pins ps7/FCLK_CLK0] \
    [get_bd_pins ps7/M_AXI_GP0_ACLK] [get_bd_pins ps7/S_AXI_HP0_ACLK]

validate_bd_design
save_bd_design

# 校验关键项
puts "=== 结果核对 ==="
foreach p {CONFIG.PCW_EN_DDR CONFIG.PCW_EN_QSPI CONFIG.PCW_EN_SDIO0 CONFIG.PCW_EN_SDIO1 \
           CONFIG.PCW_EN_ENET0 CONFIG.PCW_EN_USB0 CONFIG.PCW_EN_GPIO CONFIG.PCW_EN_UART1 \
           CONFIG.PCW_USE_S_AXI_HP0 CONFIG.PCW_USE_M_AXI_GP0} {
    puts "  [string map {CONFIG.PCW_ {}} $p] = [get_property $p $ps]"
}
puts "  DDR_PARTNO = [get_property CONFIG.PCW_UIPARAM_DDR_PARTNO $ps]"

set bd_file [get_files ps7_only.bd]
generate_target all $bd_file

file mkdir $out_dir
write_hw_platform -fixed -force -file $out_dir/ps7_only.xsa
puts "XSA: $out_dir/ps7_only.xsa ([file size $out_dir/ps7_only.xsa] 字节)"
exit