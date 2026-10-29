@echo off
setlocal EnableExtensions
set "SERVER=%~dp0scripts\server.ps1"
if not exist "%SERVER%" (
  echo ERRO: Inicializador de servidor nao encontrado em "%SERVER%".
  if not defined NEVETRACKER_NO_PAUSE pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SERVER%" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo Consulte os logs em .logs\server.
  if not defined NEVETRACKER_NO_PAUSE pause
)
exit /b %EXIT_CODE%
