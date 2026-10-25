@echo off
rem ===========================================================================
rem  Windows 上跑成对采集。用法（在本目录下）：
rem      capture_pairs.bat --src D:\shots\src --out D:\shots\run01
rem  第一次跑先自检（不连相机）：
rem      capture_pairs.bat --src D:\shots\src --out D:\shots\run01 --dry-run
rem ===========================================================================
chcp 65001 >nul
setlocal

set PY=python
if exist "%~dp0..\..\env\receiver\Scripts\python.exe" set PY="%~dp0..\..\env\receiver\Scripts\python.exe"
if exist "%~dp0..\..\env\receiver\bin\python.exe" set PY="%~dp0..\..\env\receiver\bin\python.exe"

%PY% "%~dp0capture_pairs.py" %*
set RC=%ERRORLEVEL%
echo.
echo [exit %RC%]
pause
