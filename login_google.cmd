@echo off
setlocal
cd /d "%~dp0"
set "BUNDLED_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%BUNDLED_PYTHON%" (
  "%BUNDLED_PYTHON%" login_google.py
  goto :done
)
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
