@echo off
setlocal
if /i not "%~1"=="--minimized" (
  start "SEGIO Archive" /min "%ComSpec%" /d /s /c ""%~f0" --minimized"
  exit /b 0
)
cd /d "%~dp0"
set "SEGIO_STARTUP_TOKEN="
set "SEGIO_STARTUP_SIGNAL="
for /f "delims=" %%T in ('py -3.12 "%~dp0segio_archive\startup_notice.py" --token') do set "SEGIO_STARTUP_TOKEN=%%T"
if not defined SEGIO_STARTUP_TOKEN goto python_failed
set "SEGIO_STARTUP_SIGNAL=%TEMP%\segio-startup-%SEGIO_STARTUP_TOKEN%"
mkdir "%SEGIO_STARTUP_SIGNAL%" 2>nul
if exist "%SEGIO_STARTUP_SIGNAL%\" start "" /b py -3.12 "%~dp0segio_archive\startup_notice.py" --watch "%SEGIO_STARTUP_SIGNAL%"
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
if defined SEGIO_STARTUP_SIGNAL if exist "%SEGIO_STARTUP_SIGNAL%\" echo python_failed>"%SEGIO_STARTUP_SIGNAL%\failed"
echo.
echo Could not create the Python environment. Install Python 3.12 x64 with Python Launcher.
pause
exit /b 1
:dependencies_failed
if defined SEGIO_STARTUP_SIGNAL if exist "%SEGIO_STARTUP_SIGNAL%\" echo dependencies_failed>"%SEGIO_STARTUP_SIGNAL%\failed"
echo.
echo Could not install Python packages. Check the pip error above and Internet access.
pause
exit /b 1
:app_failed
if defined SEGIO_STARTUP_SIGNAL if exist "%SEGIO_STARTUP_SIGNAL%\" echo app_failed>"%SEGIO_STARTUP_SIGNAL%\failed"
echo.
echo Could not open the program window. Check the Python error above.
echo Browser installation is optional; it is not needed to open the login window.
pause
exit /b 1
