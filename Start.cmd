@echo off
rem ============================================================
rem  local-video-catalog - double-click launcher
rem
rem  This file is encoded in Shift-JIS (CP932) because cmd.exe
rem  reads batch files with the OEM code page, not UTF-8.
rem  The Japanese messages below are intentional.
rem ============================================================
chcp 932 >nul 2>&1
setlocal

set "ROOT=%~dp0"
set "APP=%ROOT%src\local_video_catalog\gui\app.py"

if not exist "%ROOT%app-root.marker" (
    echo.
    echo アプリの構成が壊れています。app-root.marker が見つかりません。
    echo 配布されたフォルダー一式を展開し直してください。
    echo.
    echo 何かキーを押すと閉じます。
    pause >nul
    exit /b 1
)

rem --- Python 3.13 以降を探す ---
set "LAUNCHER="
for %%P in (py.exe) do if not defined LAUNCHER set "LAUNCHER=%%~$PATH:P"
if defined LAUNCHER (
    "%LAUNCHER%" -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,13) else 1)" >nul 2>&1
    if not errorlevel 1 (
        start "" "%LAUNCHER%" -3 -X utf8 "%APP%"
        exit /b 0
    )
)

set "PYTHON="
for %%P in (python.exe) do if not defined PYTHON set "PYTHON=%%~$PATH:P"
if defined PYTHON (
    "%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info>=(3,13) else 1)" >nul 2>&1
    if not errorlevel 1 (
        start "" "%PYTHON%" -X utf8 "%APP%"
        exit /b 0
    )
)

echo.
echo Python 3.13 以降が見つかりません。
echo.
echo このアプリの実行には Python 3.13 以降が必要です。
echo Microsoft Store または python.org からインストールしてから、
echo もう一度実行してください。
echo.
echo 何かキーを押すと閉じます。
pause >nul
exit /b 1
