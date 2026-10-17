# 按指定形状生成测试数据、编译内核与参考实现、跑比对。
# 参数要和 C++ 侧用 -D 传的一致，否则头文件里的尺寸和数据的尺寸对不上。
#
# 用法示例：
#   .\run_shape_test.ps1 -Cin 12  -Cout 48 -K 5 -Pad 2     # conv_first
#   .\run_shape_test.ps1 -Cin 48  -Cout 16 -K 1 -Pad 0     # Ghost 的 1×1
#   .\run_shape_test.ps1 -Cin 112 -Cout 64 -K 3 -Pad 1     # decoder 的 3×3

param(
    [int]$Cin = 12,
    [int]$Cout = 48,
    [int]$K = 5,
    [int]$Pad = 2,
    [int]$Dilation = 1,
    [int]$Stride = 1,
    [int]$H = 8,
    [int]$W = 8,
    [int]$Shift = 7
)

$ErrorActionPreference = "Stop"

# 路径按仓库布局推导（本脚本位于 fpga/pl/scripts/）；$py/$gpp 按本机环境调整
$pl     = Split-Path -Parent $PSScriptRoot                        # fpga/pl

# 工具链：优先环境变量，其次 PATH，最后报错（不写死本机路径）
$gpp = if ($env:GPP) { $env:GPP } else { (Get-Command g++ -ErrorAction SilentlyContinue).Source }
$py  = if ($env:PYTHON) { $env:PYTHON } else { (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $gpp) { throw "找不到 g++：把 MinGW 的 bin 加进 PATH，或设 `$env:GPP" }
if (-not $py)  { throw "找不到 python：加进 PATH，或设 `$env:PYTHON" }
$tag    = "c${Cin}_o${Cout}_k${K}_p${Pad}_d${Dilation}_s${Stride}"
$data   = "$pl\testdata_$tag"
$exe    = "$pl\build\tb_$tag.exe"
$incs   = "-I$pl\src"
# 注意：每个 -D 必须作为独立参数传给 g++，拼成一个字符串会被当成单个宏定义
$defs   = @("-DCIN=$Cin", "-DCOUT=$Cout", "-DKSIZE=$K", "-DPAD=$Pad",
            "-DDILATION=$Dilation", "-DSTRIDE=$Stride")

Write-Output "===== 形状 $tag ====="

& $py "$pl\scripts\gen_testdata.py" --cin $Cin --cout $Cout --k $K `
    --h $H --w $W --shift $Shift --out $data | Out-Null

& $gpp -std=c++14 -O2 -Wall $incs @defs -o $exe `
    "$pl\tb\conv2d_accel_tb.cpp" `
    "$pl\src\conv2d_accel.cpp" `
    "$pl\src\conv_ref.cpp"
if ($LASTEXITCODE -ne 0) { Write-Output "编译失败"; exit 1 }

& $exe $data
$rc = $LASTEXITCODE
Write-Output "退出码 $rc"

if ($rc -ne 0) { exit 1 }
Write-Output "形状 $tag 通过"
