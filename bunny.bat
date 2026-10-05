:: bunny — start the always-on UE MCP proxy
:: Usage: run_bunny.bat            (uses config.json in this folder)
@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe -m bunny.server %*
