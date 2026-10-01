@echo off
setlocal
cd /d "%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_existing_dashboard.ps1"
if errorlevel 1 (
  echo Could not safely stop the existing Dashboard. New instance was not started.
  pause
  exit /b 1
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 dashboard.py --open
  goto :done
)
where python >nul 2>nul
if not errorlevel 1 (
  python dashboard.py --open
  goto :done
)
echo Cannot find Python 3.10 or newer.
:done
if errorlevel 1 pause
