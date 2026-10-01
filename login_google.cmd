@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 login_google.py
  goto :done
)
where python >nul 2>nul
if not errorlevel 1 (
  python login_google.py
  goto :done
)
echo Cannot find Python 3.10 or newer.
:done
if errorlevel 1 pause
