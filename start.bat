@echo off
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 start_server.py
) else (
    python start_server.py
)
if errorlevel 1 goto :startup_failed
exit /b 0

:startup_failed
set "VIGI_EXIT_CODE=%ERRORLEVEL%"
echo [VIGI] Startup failed with exit code %VIGI_EXIT_CODE%.
pause
exit /b %VIGI_EXIT_CODE%
