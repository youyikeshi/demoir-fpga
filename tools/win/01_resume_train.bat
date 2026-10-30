@echo off
REM ===========================================================================
REM
REM
REM
REM ===========================================================================
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0..\.."

set "START=%~1"
if "%START%"=="" set "START=105"
set "TARGET=%~2"
if "%TARGET%"=="" set "TARGET=150"
set "CROP=384"
set "BATCH=2"
set "NAME=bin_base"

set "PY="
if exist ".venv-bin\Scripts\python.exe" set "PY=%CD%\.venv-bin\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=%CD%\.venv\Scripts\python.exe"
if not defined PY set "PY=python"

set "SUFFIX=ai\demoir-fpga\demoir-fpga-main\uhdm_data\Train"
set "TRAIN="
for %%D in (E F G H I D C) do (
    if not defined TRAIN if exist "%%D:\%SUFFIX%\pair_00" set "TRAIN=%%D:\%SUFFIX%"
)
if not defined TRAIN (
    echo [FAIL] train set not found; run tools\win\00_check_env.bat first
    pause & exit /b 1
)

set "CKPT=runs\%NAME%\net_checkpoints\checkpoint_00%START%.tar"
if not exist "%CKPT%" (
    echo [FAIL] not found: %CKPT%
    dir /b runs\%NAME%\net_checkpoints\checkpoint_0*.tar
    pause & exit /b 1
)

echo ============================================================
echo   resume: ep%START% --^> ep%TARGET%
echo   Python : %PY%
echo   trainset : %TRAIN%
echo   crop/batch : %CROP% / %BATCH%
echo   ckpt     : %CKPT%
echo ============================================================
echo.
echo  log: runs\%NAME%\train_win.log
echo  interrupt: Ctrl+C any time; checkpoint every 5 epochs,
echo        just rerun this script to continue from the latest checkpoint.
echo.

set DEMOIR_BIN=1
set DEMOIR_MODE=base
set DEMOIR_RDB=1
set OMP_NUM_THREADS=8

"%PY%" src\train.py ^
    --config uhdm_bin_ep150 ^
    --name %NAME% ^
    --epochs %TARGET% ^
    --load-epoch %START% ^
    --save-epoch 5 ^
    --crop-size %CROP% ^
    --batch-size %BATCH% ^
    --train-dataset "%TRAIN%" ^
    --eval-dataset datasets\uhdm\eval ^
    2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath 'runs\%NAME%\train_win.log'"

echo.
echo ============================================================
echo   done. next: tools\win\02_eval.bat
echo ============================================================
pause
endlocal
