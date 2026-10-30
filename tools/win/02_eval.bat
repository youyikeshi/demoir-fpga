@echo off
REM ===========================================================================
REM
REM
REM ===========================================================================
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0..\.."

set "EP=%~1"
if "%EP%"=="" set "EP=105"
set "TAG=%~2"
if "%TAG%"=="" set "TAG=stage%EP%"

set "PY="
if exist ".venv-bin\Scripts\python.exe" set "PY=%CD%\.venv-bin\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=%CD%\.venv\Scripts\python.exe"
if not defined PY set "PY=python"

set "CKPT=runs\bin_base\net_checkpoints\checkpoint_00%EP%.tar"
if not exist "%CKPT%" (
    echo [FAIL] not found: %CKPT%
    dir /b runs\bin_base\net_checkpoints\checkpoint_0*.tar
    pause & exit /b 1
)

set DEMOIR_BIN=1
set DEMOIR_MODE=base
set DEMOIR_RDB=1

echo ============================================================
echo   eval %CKPT%  --^>  tag %TAG%
echo ============================================================
"%PY%" tools\eval_bin.py --ckpt "%CKPT%" --tag %TAG%
echo.
echo output: runs\eval_bin\%TAG%\
pause
endlocal
