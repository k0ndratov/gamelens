@echo off
rem Builds GameLens.exe. Run once after setup.bat, and again after updates.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup.bat first.
    pause
    exit /b 1
)
echo Installing the packager (first time only)...
".venv\Scripts\python.exe" -m pip install --quiet pyinstaller || goto :failed

echo Building GameLens.exe - this takes a few minutes...
".venv\Scripts\python.exe" build_exe.py || goto :failed

echo.
echo Running a quick self-test of the built app (Windows will ask for admin)...
powershell -NoProfile -Command "Start-Process -FilePath '%~dp0dist\GameLens\GameLens.exe' -ArgumentList '--cli','selftest' -WorkingDirectory '%~dp0dist\GameLens' -Verb RunAs -Wait"
echo Self-test finished - results are in dist\GameLens\gamelens-debug.log
echo.
echo Done. Start GameLens from the desktop shortcut, or dist\GameLens\GameLens.exe
pause
exit /b 0

:failed
echo.
echo Build failed - see the messages above. Everything is also saved in build.log.
pause
exit /b 1
