@echo off
rem Start GameLens. Double-click this once the setup in README.md is done.
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python -m gamelens run
if errorlevel 1 pause
