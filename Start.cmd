@echo off
rem ============================================================
rem  local-video-catalog - double-click launcher
rem
rem  Encoded in Shift-JIS (CP932): cmd.exe reads batch files with
rem  the OEM code page, not UTF-8. The Japanese below is intended.
rem
rem  It always goes through launch.py, never a module file.
rem  Running src\local_video_catalog\gui\app.py directly makes it
rem  __main__ rather than part of the package, so its relative
rem  imports fail at once. launch.py sets up the import path and
rem  reports any startup failure to the user.
rem
rem  Flow is flat on purpose: goto and labels only, no errorlevel
rem  tests inside parenthesised blocks. Those blocks are parsed as
rem  a unit and the earlier nested version silently fell through
rem  to the "Python not found" message even though Python was
rem  present. A launcher a general user depends on has to be dull.
rem ============================================================
chcp 932 >nul 2>&1
setlocal

set "ROOT=%~dp0"
set "LAUNCH=%ROOT%launch.py"
set "PY="

if not exist "%ROOT%app-root.marker" goto broken
if not exist "%LAUNCH%" goto broken

rem --- 1. 同梱 runtime（利用者が Python を用意しなくてよい経路）---
if not exist "%ROOT%runtime\pythonw.exe" goto try_pyw
if exist "%ROOT%runtime\tcl\tcl8.6" set "TCL_LIBRARY=%ROOT%runtime\tcl\tcl8.6"
if exist "%ROOT%runtime\tcl\tk8.6" set "TK_LIBRARY=%ROOT%runtime\tcl\tk8.6"
set "PY=%ROOT%runtime\pythonw.exe"
goto launch

rem --- 2. Python launcher の pyw（コンソールを残さない）---
:try_pyw
for %%P in (pyw.exe) do set "PY=%%~$PATH:P"
if not defined PY goto try_pythonw
call :check_version "pyw"
if errorlevel 1 goto try_pythonw
goto launch

rem --- 3. PATH 上の pythonw ---
:try_pythonw
set "PY="
for %%P in (pythonw.exe) do set "PY=%%~$PATH:P"
if not defined PY goto try_python
call :check_version "pythonw"
if errorlevel 1 goto try_python
goto launch

rem --- 4. PATH 上の python（最後の手段。コンソールが残る）---
:try_python
set "PY="
for %%P in (python.exe) do set "PY=%%~$PATH:P"
if not defined PY goto no_python
call :check_version "python"
if errorlevel 1 goto no_python
goto launch

:launch
start "" "%PY%" -X utf8 "%LAUNCH%"
exit /b 0

rem --- バージョン確認。3.13 以降なら 0、それ以外は 1 ---
rem     pyw / pythonw は結果を返せないので、対になる python で確かめる。
:check_version
if "%~1"=="pyw" goto check_with_py
if "%~1"=="pythonw" goto check_with_python
"%PY%" -c "import sys; sys.exit(0 if sys.version_info>=(3,13) else 1)" >nul 2>&1
exit /b %errorlevel%

:check_with_py
set "CHECKER="
for %%P in (py.exe) do set "CHECKER=%%~$PATH:P"
if not defined CHECKER exit /b 1
"%CHECKER%" -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,13) else 1)" >nul 2>&1
exit /b %errorlevel%

:check_with_python
set "CHECKER="
for %%P in (python.exe) do set "CHECKER=%%~$PATH:P"
if not defined CHECKER exit /b 1
"%CHECKER%" -c "import sys; sys.exit(0 if sys.version_info>=(3,13) else 1)" >nul 2>&1
exit /b %errorlevel%

:no_python
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

:broken
echo.
echo アプリの構成が壊れています。
echo.
echo 次のファイルが必要です:
echo   app-root.marker
echo   launch.py
echo.
echo 配布されたフォルダー一式を展開し直してください。
echo.
echo 何かキーを押すと閉じます。
pause >nul
exit /b 1
