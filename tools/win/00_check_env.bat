@echo off
REM ===========================================================================
REM
REM      tools\win\00_check_env.bat
REM
REM ===========================================================================
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0..\.."
echo.
echo ============================================================
echo   BinaryDemoire 
echo   : %CD%
echo ============================================================
echo.

REM ---- 1. Python -------------------------------------------------------------
set "PY="
if exist ".venv-bin\Scripts\python.exe" set "PY=%CD%\.venv-bin\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=%CD%\.venv\Scripts\python.exe"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
    echo [FAIL] [FAIL] python not found. edit this file and set PY to your python.exe
    echo        e.g.  set "PY=C:\Users\you\miniconda3\envs\esdnet\python.exe"
    goto :end
)
echo [ OK ] Python: %PY%
"%PY%" -c "import sys;print('        :', sys.version.split()[0])"

"%PY%" -c "import torch" 2>nul
if errorlevel 1 (
    echo [FAIL] [FAIL] torch missing. pip install torch==2.7.0 torchvision==0.22.0 --index-url https://download.pytorch.org/whl/cu118
    goto :end
)
"%PY%" -c "import torch;print('        torch:', torch.__version__, '| CUDA:', torch.cuda.is_available(), '|', (torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'no gpu'))"

"%PY%" -c "import tensorboardX, lpips" 2>nul
if errorlevel 1 (
    echo [FAIL] [FAIL] missing tensorboardX/lpips. pip install tensorboardX lpips
    goto :end
)
echo [ OK ] tensorboardX / lpips

set "SUFFIX=ai\demoir-fpga\demoir-fpga-main\uhdm_data\Train"
set "TRAIN="
for %%D in (E F G H I D C) do (
    if not defined TRAIN if exist "%%D:\%SUFFIX%\pair_00" set "TRAIN=%%D:\%SUFFIX%"
)
if not defined TRAIN (
    echo [FAIL] [FAIL] train set not found on E/F/G/H/I/D/C, suffix: %SUFFIX%
    echo        edit this file and hardcode TRAIN, e.g.
    echo        set "TRAIN=F:\ai\demoir-fpga\demoir-fpga-main\uhdm_data\Train"
    goto :end
)
echo [ OK ] : %TRAIN%
set /a N=0
for /d %%P in ("%TRAIN%\pair_*") do set /a N+=1
echo        pair_* subdirs: %N%  (expect 28)

REM ---- 3. checkpoint --------------------------------------------------------
set "CKPT=runs\bin_base\net_checkpoints\checkpoint_000105.tar"
if not exist "%CKPT%" (
    echo [FAIL] [FAIL] resume point not found: %CKPT%
    echo        available checkpoints:
    dir /b runs\bin_base\net_checkpoints\checkpoint_0*.tar 2>nul
    goto :end
)
for %%F in ("%CKPT%") do echo [ OK ] checkpoint: %%F  %%~zF 

echo.
echo ---- ---- binary model self-test (about 10 s) --------
set DEMOIR_BIN=1
set DEMOIR_MODE=base
set DEMOIR_RDB=1
"%PY%" tools\smoke_binary.py --config uhdm_bin_ep150
if errorlevel 1 goto :end
echo.
echo ============================================================
echo   all checks passed. next: 01_resume_train.bat
echo ============================================================

:end
echo.
pause
endlocal
