@echo off
rem Double-click to install, repair, uninstall or start Looma on Windows.
rem The work is in looma.ps1, next to this file.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0looma.ps1"
pause
