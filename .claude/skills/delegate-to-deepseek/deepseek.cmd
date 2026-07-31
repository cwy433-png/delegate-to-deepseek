@echo off
rem Launch DeepSeek V4 Flash as a bounded subagent from Claude Code on Windows.
rem
rem Windows twin of the POSIX `deepseek` wrapper; keep the two in step. Defaults
rem are prepended rather than conditional: argparse takes the last occurrence of
rem an option, so anything the caller passes wins. That also avoids scanning %*,
rem which would split a quoted --task argument on its spaces.
rem
rem delegate-to-deepseek

setlocal

set "LAUNCHER="
if defined DELEGATE_TO_DEEPSEEK_HOME if exist "%DELEGATE_TO_DEEPSEEK_HOME%\scripts\delegate.py" set "LAUNCHER=%DELEGATE_TO_DEEPSEEK_HOME%\scripts\delegate.py"
if not defined LAUNCHER if exist "%~dp0..\..\..\scripts\delegate.py" set "LAUNCHER=%~dp0..\..\..\scripts\delegate.py"
if not defined LAUNCHER if exist "%USERPROFILE%\.codex\skills\delegate-to-deepseek\scripts\delegate.py" set "LAUNCHER=%USERPROFILE%\.codex\skills\delegate-to-deepseek\scripts\delegate.py"

if not defined LAUNCHER (
  echo delegate.py not found. Set DELEGATE_TO_DEEPSEEK_HOME to the repository root, 1>&2
  echo or clone https://github.com/cwy433-png/delegate-to-deepseek into %%USERPROFILE%%\.codex\skills\. 1>&2
  exit /b 2
)

set "PY=python"
where python >nul 2>&1 || set "PY=py -3"

%PY% "%LAUNCHER%" --timeout 480 --backend claude %*
exit /b %ERRORLEVEL%
