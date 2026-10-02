@echo off
rem Starts GameLens with administrator rights. Needed when the game itself runs
rem as administrator, because Windows hides its keystrokes from normal programs.
net session >nul 2>&1
if errorlevel 1 (
    echo Asking Windows for administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python -X faulthandler -m gamelens run
echo.
echo GameLens exited with code %errorlevel%
pause
