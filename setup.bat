@echo off
rem One-time setup: virtual environment and dependencies.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH. Install Python 3.10+ from python.org,
    echo ticking "Add python.exe to PATH" in the installer, then run this again.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Creating the virtual environment...
    python -m venv .venv || goto :failed
)

call ".venv\Scripts\activate.bat"
echo Installing dependencies - this takes a few minutes the first time...
python -m pip install --upgrade pip
pip install -r requirements.txt || goto :failed

if not exist "config.yaml" (
    copy "config.example.yaml" "config.yaml" >nul
    echo Created config.yaml from config.example.yaml.
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Created .env - open it and paste your Anthropic API key in.
)

echo.
echo Done. Next:
echo   1. put the game in Borderless Window mode, with subtitles on
echo   2. pause it on a line of dialogue
echo   3. run:  python -m gamelens calibrate
echo   4. then: python -m gamelens doctor
echo.
pause
exit /b 0

:failed
echo.
echo Setup failed - see the error above.
pause
exit /b 1
