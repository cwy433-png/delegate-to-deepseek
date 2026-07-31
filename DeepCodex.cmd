@echo off
setlocal
set "DEEPCODEX_ROOT=%~dp0"
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3 "%DEEPCODEX_ROOT%scripts\web_gui.py"
) else (
  python "%DEEPCODEX_ROOT%scripts\web_gui.py"
)
endlocal
