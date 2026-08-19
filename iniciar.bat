@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"
set "BACKEND_DIR=%ROOT%backend\"
set "FRONTEND_DIR=%ROOT%"
set "PREPARE_SCRIPT=%ROOT%preparar.bat"
set "RUN_BACKEND_SCRIPT=%ROOT%scripts\run_backend.bat"
set "RUN_FRONTEND_SCRIPT=%ROOT%scripts\run_frontend.bat"
set "VENV_PY=%BACKEND_DIR%.venv\Scripts\python.exe"
set "NODE_ENV_FILE=%ROOT%.tools\node\env.bat"
set "BACKEND_ENV=%BACKEND_DIR%.env"
set "FRONTEND_ENV=%FRONTEND_DIR%.env.local"
set "BACKEND_URL=http://127.0.0.1:8000"
set "FRONTEND_URL=http://127.0.0.1:5173"
set "LOCK_DIR=%ROOT%.iniciar.lock"
set "EXIT_CODE=0"
set "LAUNCHED_BACKEND=0"
set "LAUNCHED_FRONTEND=0"
set "BACKEND_WAIT_SECONDS=90"
set "FRONTEND_WAIT_SECONDS=90"

if defined NEVEPRICE_BACKEND_WAIT_SECONDS set "BACKEND_WAIT_SECONDS=%NEVEPRICE_BACKEND_WAIT_SECONDS%"
if defined NEVEPRICE_FRONTEND_WAIT_SECONDS set "FRONTEND_WAIT_SECONDS=%NEVEPRICE_FRONTEND_WAIT_SECONDS%"

echo.
echo === NevePrice: iniciar local ===
echo Backend:  %BACKEND_URL%
echo Frontend: %FRONTEND_URL%
echo.

call :create_lock
if errorlevel 1 (
  exit /b 1
)

if not exist "%PREPARE_SCRIPT%" (
  echo ERRO: preparar.bat nao foi encontrado em "%PREPARE_SCRIPT%".
  set "EXIT_CODE=1"
  goto :cleanup
)

if not exist "%RUN_BACKEND_SCRIPT%" (
  echo ERRO: Runner do backend nao encontrado em "%RUN_BACKEND_SCRIPT%".
  set "EXIT_CODE=1"
  goto :cleanup
)

if not exist "%RUN_FRONTEND_SCRIPT%" (
  echo ERRO: Runner do frontend nao encontrado em "%RUN_FRONTEND_SCRIPT%".
  set "EXIT_CODE=1"
  goto :cleanup
)

set "NEED_PREPARE=0"
if not exist "%VENV_PY%" set "NEED_PREPARE=1"
if not exist "%FRONTEND_DIR%node_modules\" set "NEED_PREPARE=1"
if not exist "%NODE_ENV_FILE%" set "NEED_PREPARE=1"

if "%NEED_PREPARE%"=="1" (
  echo Dependencias ainda nao preparadas. Executando preparar.bat...
  call "%PREPARE_SCRIPT%"
  if errorlevel 1 (
    echo ERRO: preparar.bat falhou. Corrija a mensagem acima e tente novamente.
    set "EXIT_CODE=1"
    goto :cleanup
  )
)

if not exist "%VENV_PY%" (
  echo ERRO: Ambiente virtual do backend nao encontrado em "%BACKEND_DIR%.venv".
  set "EXIT_CODE=1"
  goto :cleanup
)

if not exist "%FRONTEND_DIR%node_modules\" (
  echo ERRO: node_modules do frontend nao encontrado em "%FRONTEND_DIR%node_modules".
  set "EXIT_CODE=1"
  goto :cleanup
)

if not exist "%NODE_ENV_FILE%" (
  echo ERRO: Ambiente do Node nao encontrado em "%NODE_ENV_FILE%".
  set "EXIT_CODE=1"
  goto :cleanup
)

call "%NODE_ENV_FILE%"

set "NEED_NODE_REPAIR=0"
if not defined NODE_EXE set "NEED_NODE_REPAIR=1"
if not defined NPM_CMD set "NEED_NODE_REPAIR=1"
if defined NODE_EXE if not exist "%NODE_EXE%" set "NEED_NODE_REPAIR=1"
if defined NPM_CMD if not exist "%NPM_CMD%" set "NEED_NODE_REPAIR=1"

if "%NEED_NODE_REPAIR%"=="1" (
  echo Ambiente do Node incompleto. Reexecutando preparar.bat...
  call "%PREPARE_SCRIPT%"
  if errorlevel 1 (
    echo ERRO: preparar.bat falhou ao reparar o Node.
    set "EXIT_CODE=1"
    goto :cleanup
  )
  call "%NODE_ENV_FILE%"
)

if not defined NODE_EXE (
  echo ERRO: NODE_EXE nao foi definido pelo ambiente do Node.
  set "EXIT_CODE=1"
  goto :cleanup
)

if not defined NPM_CMD (
  echo ERRO: NPM_CMD nao foi definido pelo ambiente do Node.
  set "EXIT_CODE=1"
  goto :cleanup
)

if not exist "%NODE_EXE%" (
  echo ERRO: Node configurado nao foi encontrado em "%NODE_EXE%".
  set "EXIT_CODE=1"
  goto :cleanup
)

if not exist "%NPM_CMD%" (
  echo ERRO: npm configurado nao foi encontrado em "%NPM_CMD%".
  set "EXIT_CODE=1"
  goto :cleanup
)

echo Conferindo arquivos de ambiente locais...
set "DB_PATH_URL=%BACKEND_DIR%precos.db"
set "DB_PATH_URL=%DB_PATH_URL:\=/%"
call :set_env_key "%BACKEND_ENV%" "DATABASE_URL" "DATABASE_URL=sqlite:///%DB_PATH_URL%"
if errorlevel 1 (
  echo ERRO: Falha ao preparar o arquivo "%BACKEND_ENV%".
  set "EXIT_CODE=1"
  goto :cleanup
)

call :set_env_key "%FRONTEND_ENV%" "VITE_API_URL" "VITE_API_URL=http://127.0.0.1:8000"
if errorlevel 1 (
  echo ERRO: Falha ao preparar o arquivo "%FRONTEND_ENV%".
  set "EXIT_CODE=1"
  goto :cleanup
)

call :set_env_key "%FRONTEND_ENV%" "VITE_BASE_PATH" "VITE_BASE_PATH=/"
if errorlevel 1 (
  echo ERRO: Falha ao preparar o arquivo "%FRONTEND_ENV%".
  set "EXIT_CODE=1"
  goto :cleanup
)

call :url_ok "%BACKEND_URL%/"
if errorlevel 1 (
  echo Iniciando backend neste CMD...
  start "NevePrice Backend" /B "%COMSPEC%" /d /c ""%RUN_BACKEND_SCRIPT%" "%BACKEND_DIR%." "%VENV_PY%""
  set "LAUNCHED_BACKEND=1"
) else (
  echo Backend ja esta disponivel. Nao vou iniciar outra instancia.
)

echo Aguardando backend ficar disponivel...
call :wait_url "%BACKEND_URL%/" %BACKEND_WAIT_SECONDS%
if errorlevel 1 (
  echo ERRO: Backend nao respondeu em ate %BACKEND_WAIT_SECONDS% segundos.
  echo Verifique as mensagens deste CMD para ler o erro do backend.
  set "EXIT_CODE=1"
  goto :cleanup
)

call :url_ok "%FRONTEND_URL%/"
if errorlevel 1 (
  echo Iniciando frontend neste CMD...
  start "NevePrice Frontend" /B "%COMSPEC%" /d /c ""%RUN_FRONTEND_SCRIPT%" "%FRONTEND_DIR%." "%NODE_ENV_FILE%""
  set "LAUNCHED_FRONTEND=1"
) else (
  echo Frontend ja esta disponivel. Nao vou iniciar outra instancia.
)

echo Aguardando frontend ficar disponivel...
call :wait_url "%FRONTEND_URL%/" %FRONTEND_WAIT_SECONDS%
if errorlevel 1 (
  echo ERRO: Frontend nao respondeu em ate %FRONTEND_WAIT_SECONDS% segundos.
  echo Verifique as mensagens deste CMD para ler o erro do frontend.
  set "EXIT_CODE=1"
  goto :cleanup
) else (
  if defined NEVEPRICE_NO_BROWSER (
    echo Navegador nao aberto porque NEVEPRICE_NO_BROWSER esta definido.
  ) else (
    start "" "%FRONTEND_URL%/"
  )
)

echo.
echo Enderecos locais:
echo Backend:  %BACKEND_URL%
echo Frontend: %FRONTEND_URL%

if exist "%LOCK_DIR%" rmdir "%LOCK_DIR%" >nul 2>nul
echo.
if "%LAUNCHED_BACKEND%%LAUNCHED_FRONTEND%"=="00" (
  echo Backend e frontend ja estavam em execucao.
) else (
  echo Backend e frontend foram iniciados neste CMD.
)
echo Este console ficara aberto para manter o ambiente visivel.
echo Feche esta janela ou pressione Ctrl+C quando quiser encerrar.

if defined NEVEPRICE_EXIT_AFTER_READY exit /b 0

:keepalive
ping -n 3601 127.0.0.1 >nul 2>nul
goto :keepalive

:cleanup
if exist "%LOCK_DIR%" rmdir "%LOCK_DIR%" >nul 2>nul
if not "%EXIT_CODE%"=="0" if not defined NEVEPRICE_NO_PAUSE pause
exit /b %EXIT_CODE%

:create_lock
mkdir "%LOCK_DIR%" >nul 2>nul
if not errorlevel 1 exit /b 0

call :url_ok "%BACKEND_URL%/"
set "LOCK_BACKEND_STATUS=%ERRORLEVEL%"
call :url_ok "%FRONTEND_URL%/"
set "LOCK_FRONTEND_STATUS=%ERRORLEVEL%"

if not "%LOCK_BACKEND_STATUS%%LOCK_FRONTEND_STATUS%"=="00" (
  echo Trava antiga encontrada. Limpando "%LOCK_DIR%"...
  rmdir "%LOCK_DIR%" >nul 2>nul
  mkdir "%LOCK_DIR%" >nul 2>nul
  if not errorlevel 1 exit /b 0
)

echo Ja existe uma inicializacao em andamento.
echo Se nenhuma janela estiver abrindo, apague a pasta "%LOCK_DIR%" e tente novamente.
if not defined NEVEPRICE_NO_PAUSE pause
exit /b 1

:set_env_key
set "ENV_FILE=%~1"
set "ENV_KEY=%~2"
set "ENV_LINE=%~3"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $file='%ENV_FILE%'; $key='%ENV_KEY%'; $line='%ENV_LINE%'; $dir=Split-Path -Parent $file; if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }; $lines=@(); if (Test-Path -LiteralPath $file) { $lines=Get-Content -LiteralPath $file }; $updated=$false; $out=@(); foreach ($item in $lines) { if ($item.StartsWith($key + '=', [System.StringComparison]::OrdinalIgnoreCase)) { $out += $line; $updated=$true } else { $out += $item } }; if (-not $updated) { $out += $line }; Set-Content -LiteralPath $file -Value $out -Encoding ASCII"
exit /b %errorlevel%

:url_ok
powershell -NoProfile -ExecutionPolicy Bypass -Command "$u=[Uri]'%~1'; $port=$u.Port; if ($port -lt 0) { if ($u.Scheme -eq 'https') { $port=443 } else { $port=80 } }; $c=New-Object Net.Sockets.TcpClient; try { $t=$c.ConnectAsync($u.Host,$port); if (-not $t.Wait(1200)) { exit 1 }; if ($c.Connected) { exit 0 }; exit 1 } catch { exit 1 } finally { $c.Close() }" >nul 2>nul
exit /b %errorlevel%

:wait_url
set "WAIT_URL=%~1"
set "WAIT_LIMIT=%~2"
set "WAIT_COUNT=0"

:wait_loop
call :url_ok "%WAIT_URL%"
if not errorlevel 1 exit /b 0
set /a WAIT_COUNT+=1
if !WAIT_COUNT! GEQ %WAIT_LIMIT% exit /b 1
ping -n 2 127.0.0.1 >nul 2>nul
goto :wait_loop
