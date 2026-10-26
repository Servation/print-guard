@echo off
rem Starts print-guard's watcher. Double-click it when a print begins.
rem Extra arguments pass through, e.g.  print-guard.cmd --armed   or   print-guard.cmd --exit-when-done
cd /d "%~dp0"
if exist ".venv\Scripts\print-guard.exe" (
    ".venv\Scripts\print-guard.exe" watch %*
) else (
    uv run print-guard watch %*
)
echo.
pause
