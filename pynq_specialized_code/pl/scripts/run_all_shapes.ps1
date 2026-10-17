# 六种形态一次跑完：综合 + 导出 IP。
#
# 为什么要有这个脚本：
#   1) 形状 2/3 的综合数据是在加膨胀/步长之前测的，必须用当前代码重跑
#   2) 六种形态的 IP 都要导出，才能进 Vivado 的 Block Design
#
# 用法：.\run_all_shapes.ps1

$ErrorActionPreference = "Stop"

# 路径按仓库布局推导（本脚本位于 fpga/pl/scripts/）
$fpga  = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # fpga/
$base  = Split-Path -Parent $PSScriptRoot                        # fpga/pl

# 工具链：不要把本机绝对路径写死（换台机器就废）。优先级：
#   $env:VITIS_RUN → $env:XILINX_VITIS\bin\vitis-run.bat → PATH 里的 vitis-run
if ($env:VITIS_RUN) {
    $vr = $env:VITIS_RUN
} elseif ($env:XILINX_VITIS) {
    $vr = Join-Path $env:XILINX_VITIS "bin\vitis-run.bat"
    $env:PATH = (Join-Path $env:XILINX_VITIS "bin") + ";" + $env:PATH
} else {
    $vr = (Get-Command vitis-run.bat -ErrorAction SilentlyContinue).Source
}
if (-not $vr) {
    throw "找不到 vitis-run：先 source Vitis 的 settings64（会设 XILINX_VITIS），或设 `$env:VITIS_RUN"
}

# 六种形态。字段顺序：Cin, Cout, K, Pad, Dil, Stride, MaxW, 说明
$shapes = @(
    @{cin=12;  cout=48;  k=5; pad=2; dil=1; strd=1; maxw=512; note="conv_first（5×5）"},
    @{cin=48;  cout=16;  k=1; pad=0; dil=1; strd=1; maxw=512; note="Ghost 的 1×1"},
    @{cin=112; cout=64;  k=3; pad=1; dil=1; strd=1; maxw=192; note="decoder 的 3×3"},
    @{cin=16;  cout=16;  k=3; pad=2; dil=2; strd=1; maxw=512; note=" Ghost 膨胀 2"},
    @{cin=16;  cout=16;  k=3; pad=3; dil=3; strd=1; maxw=512; note=" Ghost 膨胀 3"},
    @{cin=48;  cout=96;  k=3; pad=1; dil=1; strd=2; maxw=192; note="下采样，步长 2"}
)

foreach ($s in $shapes) {
    Write-Output ""
    Write-Output ("########## " + $s.note + " ##########")

    $env:SHAPE_CIN    = "$($s.cin)"
    $env:SHAPE_COUT   = "$($s.cout)"
    $env:SHAPE_K      = "$($s.k)"
    $env:SHAPE_PAD    = "$($s.pad)"
    $env:SHAPE_DIL    = "$($s.dil)"
    $env:SHAPE_STRIDE = "$($s.strd)"
    $env:SHAPE_MAXW   = "$($s.maxw)"
    $env:SHAPE_DATA   = "$base\testdata_c$($s.cin)_o$($s.cout)_k$($s.k)_p$($s.pad)_d$($s.dil)_s$($s.strd)"
    $tag = "c$($s.cin)_o$($s.cout)_k$($s.k)_p$($s.pad)_d$($s.dil)_s$($s.strd)_w$($s.maxw)_u25_ii1"

    # 每个形状自己生成测试数据。不能复用上一次跑的结果——本机验证可能用过更大的 W，
    # 而这里综合时 MAX_W 是固定的，W 超过 MAX_W 会被内核拒绝、csim 直接失败。
    $py = if ($env:PYTHON) { $env:PYTHON } else { (Get-Command python -ErrorAction Stop).Source }
    & $py "$base\scripts\gen_testdata.py" --cin $s.cin --cout $s.cout --k $s.k `
        --h 8 --w 8 --shift 7 --out $env:SHAPE_DATA | Out-Null

    Set-Location "$base\scripts"
    $log = & $vr --mode hls --tcl hls_run.tcl 2>&1
    $log | Select-String -Pattern "PASS:|Estimated Fmax" | Select-Object -Last 2 |
        ForEach-Object { "  " + $_.Line.Trim() }

    # csim 必须真的通过，否则后面的综合和导出都没有意义
    if (-not ($log | Select-String -Pattern "CSim done with 0 errors")) {
        Write-Output "  ！！csim 没通过，跳过这个形状"
        $log | Select-String -Pattern "error" | Select-Object -First 2 |
            ForEach-Object { "    " + $_.Line.Trim() }
        continue
    }

    $log2 = & $vr --mode hls --tcl "$fpga\board\export_ip.tcl" 2>&1
    # 导出流程内部的校验步骤会因为 Windows 路径上限报错，这是已知且无害的：
    # 只要 component.xml 生成了，IP 就能用（已用 Block Design 验证过）。
    $ipdir = "$base\proj\$($tag)\solution1\impl\ip\component.xml"
    if (Test-Path $ipdir) {
        "  IP 导出完成"
    } else {
        "  ！！IP 没导出成功"
    }
}

Write-Output ""
Write-Output "########## 六种形态全部完成 ##########"
