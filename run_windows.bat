@echo off
setlocal
if /i not "%~1"=="--minimized" (
  start "SEGIO Archive" /min "%ComSpec%" /d /s /c ""%~f0" --minimized"
  exit /b 0
)
cd /d "%~dp0"
if not defined PLAYWRIGHT_BROWSERS_PATH (
  if defined LOCALAPPDATA (
    set "PLAYWRIGHT_BROWSERS_PATH=%LOCALAPPDATA%\SEGIOArchive\Browsers"
  )
)
if not exist ".venv\Scripts\python.exe" (
  py -3.12 -m venv .venv
  if errorlevel 1 goto python_failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto dependencies_failed
echo.
echo Starting SEGIO Archive. No Chrome download is required.
echo Installed Microsoft Edge or Google Chrome will be used for collection.
".venv\Scripts\python.exe" -m segio_archive
if errorlevel 1 goto app_failed
exit /b 0
:python_failed
echo.
echo Could not create the Python environment. Install Python 3.12 x64 with Python Launcher.
pause
exit /b 1
:dependencies_failed
echo.
echo Could not install Python packages. Check the pip error above and Internet access.
pause
exit /b 1
:app_failed
echo.
echo Could not open the program window. Check the Python error above.
echo Browser installation is optional; it is not needed to open the login window.
pause
exit /b 1
