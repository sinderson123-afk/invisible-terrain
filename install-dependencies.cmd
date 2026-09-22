@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -m venv .venv
if errorlevel 1 goto failed
goto install
:use_python
python -m venv .venv
if errorlevel 1 goto failed
:install
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto failed
echo.
echo Dependencies installed. Run start.cmd to open the local control panel.
echo SDR drivers and native SDR libraries are not installed by this script.
pause
exit /b 0
:failed
echo.
echo Installation failed. Install Python 3.11 or newer and read the error above.
echo No administrator access or automatic device driver changes are needed here.
pause
exit /b 1
