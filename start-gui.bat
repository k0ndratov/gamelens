@echo off
rem Opens the GameLens window without building an exe. Asks for admin rights,
rem because the game runs elevated and hotkeys would not reach it otherwise.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Run setup.bat first.
    pause
    exit /b 1
)
powershell -NoProfile -Command "Start-Process -FilePath '%~dp0.venv\Scripts\pythonw.exe' -ArgumentList '-m','gamelens.gui' -WorkingDirectory '%~dp0' -Verb RunAs"
