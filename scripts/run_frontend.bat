@echo off
setlocal EnableExtensions

set "FRONTEND_DIR=%~1"
set "NODE_ENV_FILE=%~2"

if not exist "%FRONTEND_DIR%\package.json" (
  echo ERRO: Frontend nao encontrado em "%FRONTEND_DIR%".
  exit /b 1
)

if not exist "%NODE_ENV_FILE%" (
  echo ERRO: Ambiente do Node nao encontrado em "%NODE_ENV_FILE%".
  exit /b 1
)

call "%NODE_ENV_FILE%"

if not defined NPM_CMD (
  echo ERRO: NPM_CMD nao foi definido pelo ambiente do Node.
  exit /b 1
)

if not exist "%NPM_CMD%" (
  echo ERRO: npm configurado nao foi encontrado em "%NPM_CMD%".
  exit /b 1
)

pushd "%FRONTEND_DIR%"
if errorlevel 1 (
  echo ERRO: Nao foi possivel acessar "%FRONTEND_DIR%".
  exit /b 1
)

call "%NPM_CMD%" run dev -- --host 127.0.0.1 --port 5173 --strictPort
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
