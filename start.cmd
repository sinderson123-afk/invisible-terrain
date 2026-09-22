@echo off
setlocal
cd /d "%~dp0app"
if exist "%~dp0.venv\Scripts\python.exe" (
    set "TERRAIN_PYTHON=%~dp0.venv\Scripts\python.exe"
) else (
    set "TERRAIN_PYTHON=python"
)
if "%~1"=="" (
    "%TERRAIN_PYTHON%" original_bridge.py --open --keep-wallpaper
) else (
    "%TERRAIN_PYTHON%" original_bridge.py %*
)
set "TERRAIN_EXIT_CODE=%errorlevel%"
if not "%TERRAIN_EXIT_CODE%"=="0" (
    echo.
    echo Invisible Terrain exited with an error. Run install-dependencies.cmd first.
    echo Keep this window's error message when reporting a problem.
    pause
)
exit /b %TERRAIN_EXIT_CODE%
