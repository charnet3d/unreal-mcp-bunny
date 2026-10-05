:: install_service.bat — (re)install bunny as a Windows service via nssm.
::
:: Run FROM AN ELEVATED shell (right-click > Run as administrator, or:
::   powershell -NoProfile -Command "Start-Process .\install_service.bat -Verb RunAs")
:: from the folder the proxy should live in (its config.json / data / logs live
:: in that folder).
::
:: Why LocalSystem: a service runs in session 0, which has no interactive
:: desktop. Running AS THE USER is not enough — the service process then lacks
:: SeImpersonatePrivilege/SeTcbPrivilege, so bunny cannot borrow the console
:: session's token and every editor launch falls back to session 0, where the
:: GUI editor dies with a D3D12 RHI fatal (D3D12Util.cpp:1062, exit 3).
:: SeImpersonate + SeTcb: bunny then uses WTSQueryUserToken +
:: CreateProcessAsUserW and UnrealEditor.exe lands on your desktop.
:: (Full story: docs\implementation_details.md)
::
:: NOTE: the nssm service account CANNOT be changed with `sc config`
:: (access denied on object changes) — it must go through nssm set.
@echo off
setlocal
cd /d "%~dp0"
set NSSM=nssm.exe
if exist "%NSSM%" goto :have_nssm
where nssm >nul 2>nul
if not errorlevel 1 for /f "delims=" %%i in ('where nssm') do (set NSSM=%%i& goto :have_nssm)
echo [install_service] nssm not found — set NSSM= in this file to your nssm.exe path.
exit /b 1
:have_nssm
"%NSSM%" stop ue-mcp-bunny >nul 2>nul
"%NSSM%" set ue-mcp-bunny Application   "%~dp0.venv\Scripts\python.exe" || goto :fail
"%NSSM%" set ue-mcp-bunny AppParameters -m bunny.server        || goto :fail
"%NSSM%" set ue-mcp-bunny AppDirectory  "%~dp0."               || goto :fail
"%NSSM%" set ue-mcp-bunny ObjectName    LocalSystem            || goto :fail
:: no ObjectPassword: LocalSystem needs none, and this nssm build rejects the
:: parameter outright (it aborts the script before ObjectName lands).
"%NSSM%" set ue-mcp-bunny Start          SERVICE_AUTO_START    || goto :fail
"%NSSM%" install ue-mcp-bunny service    >nul 2>nul
"%NSSM%" start  ue-mcp-bunny                                      || goto :fail
echo [install_service] OK — ue-mcp-bunny running as LocalSystem.
sc query ue-mcp-bunny | findstr /C:"STATE"
sc qc     ue-mcp-bunny | findstr /C:"SERVICE_START_NAME"
exit /b 0
:fail
echo [install_service] FAILED (%errorlevel%) — rerun from an elevated shell.
exit /b 1
