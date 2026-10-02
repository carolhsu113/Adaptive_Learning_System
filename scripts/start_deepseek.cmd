@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_deepseek.ps1" %*
exit /b %ERRORLEVEL%
