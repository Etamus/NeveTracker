@echo off
setlocal EnableExtensions

set "INSTALLER=%~dp0scripts\install.ps1"
if not exist "%INSTALLER%" (
  echo ERRO: Instalador nao encontrado em "%INSTALLER%".
  if not defined NEVETRACK_NO_PAUSE pause
  exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo A instalacao nao foi concluida. Consulte .logs\install.log.
  if not defined NEVETRACK_NO_PAUSE pause
)

exit /b %EXIT_CODE%
