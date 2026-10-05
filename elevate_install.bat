@echo off
:: elevate-run install_service.bat from THIS folder (the deployment folder)
cd /d "%~dp0"
net session >nul 2>nul
if %errorlevel%==0 (
  call install_service.bat
) else (
  powershell -NoProfile -Command "Start-Process -FilePath '%~dp0install_service.bat' -Verb RunAs -WindowStyle Hidden"
)
