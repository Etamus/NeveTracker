@echo off
setlocal EnableExtensions

set "STARTER=%~dp0scripts\start.ps1"
if not exist "%STARTER%" (
  echo ERRO: Inicializador nao encontrado em "%STARTER%".
  if not defined NEVETRACKER_NO_PAUSE pause
  exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%STARTER%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo O NeveTracker nao iniciou. Consulte os arquivos da pasta .logs.
  if not defined NEVETRACKER_NO_PAUSE pause
)

exit /b %EXIT_CODE%
