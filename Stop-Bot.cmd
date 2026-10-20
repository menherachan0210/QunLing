@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop-Bot.ps1"
if errorlevel 1 (
  echo Failed to stop one or more managed services. Review the error above.
  pause
  exit /b 1
)
exit /b 0
