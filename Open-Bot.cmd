@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Open-Bot.ps1"
if errorlevel 1 (
  echo Failed to open the management pages. Review the error above.
  pause
  exit /b 1
)
exit /b 0
