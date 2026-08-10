@echo off
setlocal EnableExtensions

set "BACKEND_DIR=%~1"
set "VENV_PY=%~2"

if not exist "%BACKEND_DIR%\main.py" (
  echo ERRO: Backend nao encontrado em "%BACKEND_DIR%".
  exit /b 1
)

if not exist "%VENV_PY%" (
  echo ERRO: Python do ambiente virtual nao encontrado em "%VENV_PY%".
  exit /b 1
)

pushd "%BACKEND_DIR%"
if errorlevel 1 (
  echo ERRO: Nao foi possivel acessar "%BACKEND_DIR%".
  exit /b 1
)

"%VENV_PY%" -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
