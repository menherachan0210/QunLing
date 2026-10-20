@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Bot.ps1"
if errorlevel 1 (
  echo Failed to start. Review the error above and the local logs.
  pause
  exit /b 1
)
exit /b 0
