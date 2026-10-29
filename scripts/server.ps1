[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)][int]$Port = 8800,
    [switch]$ConfigureFirewallOnly,
    [switch]$NoFirewall
)
$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$Logs = Join-Path $Root '.logs\server'
New-Item -ItemType Directory -Path $Logs -Force | Out-Null
. (Join-Path $PSScriptRoot 'node_environment.ps1')
. (Join-Path $PSScriptRoot 'project_browser.ps1')

function Set-LanFirewall {
    $name = "NeveTracker-LAN-$Port"
    # Restrict access to directly connected subnets, including Wi-Fi networks
    # Windows has classified as Public. No router configuration is changed.
    $rule = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
    if ($rule -and $rule.Enabled -eq 'True') { return }
    if ($rule) {
        Set-NetFirewallRule -Name $name -Enabled True -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Profile Any | Out-Null
        return
    }
    New-NetFirewallRule -Name $name -DisplayName "NeveTracker LAN TCP $Port" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Profile Any | Out-Null
}
if ($ConfigureFirewallOnly) {
    try { Set-LanFirewall; exit 0 } catch { Write-Error $_; exit 1 }
}

function Test-Api([int]$ApiPort) {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/" -TimeoutSec 2
        return $r.status -eq 'API Online'
    } catch { return $false }
}
function Test-Port([int]$Number) {
    $socket = New-Object Net.Sockets.TcpClient
    try { $connection = $socket.ConnectAsync('127.0.0.1', $Number); return ($connection.Wait(250) -and $socket.Connected) }
    catch { return $false } finally { $socket.Dispose() }
}
function Resolve-ServerNode {
    $file = Join-Path $Root '.tools\node\env.json'
    if (Test-Path -LiteralPath $file) {
        try {
            $config = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
            if ($config.node_exe -and (Test-Path -LiteralPath $config.node_exe)) { return $config.node_exe }
        } catch {}
    }
    $candidate = Get-ChildItem -LiteralPath (Join-Path $Root '.tools\node') -Filter node.exe -Recurse -File -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
    if ($candidate) { return $candidate.FullName }
    return $null
}

$apiProcess = $null
$webProcess = $null
$lockStream = $null
try {
    if (Test-Port $Port) {
        try { $health = Invoke-RestMethod "http://127.0.0.1:$Port/server-health" -TimeoutSec 2 } catch { $health = $null }
        if ($health.service -eq 'NeveTracker LAN') {
            Write-Host "NeveTracker servidor ja esta disponivel na porta $Port."
            if (-not $env:NEVETRACKER_NO_BROWSER) { Open-ProjectBrowser "http://127.0.0.1:$Port" $Root }
            exit 0
        }
        throw "A porta $Port pertence a outro programa. Use servidor.bat -Port 8801."
    }
    # Exclusive handle is released by Windows even on a crash.
    $lockStream = [IO.File]::Open((Join-Path $Logs 'launcher.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $python = Join-Path $Root 'backend\.venv\Scripts\python.exe'
    $node = Resolve-ServerNode
    $ready = $false
    if ((Test-Path -LiteralPath $python) -and $node -and (Test-Path (Join-Path $Root 'node_modules\vite\bin\vite.js'))) {
        try {
            & $python -c 'import fastapi,uvicorn,httpx,scrapy,openpyxl' 2>$null
            $ready = $LASTEXITCODE -eq 0
        } catch { $ready = $false }
    }
    if (-not $ready) {
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Instalacao/reparo falhou. Consulte .logs\install.log.' }
        $node = Resolve-ServerNode
    }
    Set-NodeEnvironment $node
    Write-Host 'Compilando interface para a rede local...'
    $previousApi = $env:VITE_API_URL
    $previousBase = $env:VITE_BASE_PATH
    try {
        $env:VITE_API_URL = '/api'
        $env:VITE_BASE_PATH = '/'
        Push-Location $Root
        try {
            & $node (Join-Path $Root 'node_modules\vite\bin\vite.js') build *> (Join-Path $Logs 'build.log')
            if ($LASTEXITCODE -ne 0) { throw 'Falha ao compilar a interface. Consulte .logs\server\build.log.' }
        } finally { Pop-Location }
    } finally { $env:VITE_API_URL = $previousApi; $env:VITE_BASE_PATH = $previousBase }

    if (-not $NoFirewall) {
        try { Set-LanFirewall } catch {
            Write-Host 'O Windows precisa autorizar a regra de firewall para acesso pela rede local.'
            $helper = Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -ArgumentList @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'), '-ConfigureFirewallOnly', '-Port', "$Port") -Wait -PassThru
            if ($helper.ExitCode -ne 0) { throw 'Nao foi possivel liberar o firewall. Execute como administrador ou configure a regra de rede local.' }
        }
    }
    $apiPort = $null
    foreach ($candidatePort in 8000..8099) {
        if ((Test-Port $candidatePort) -and (Test-Api $candidatePort)) { $apiPort = $candidatePort; break }
    }
    if (-not $apiPort) {
        $apiPort = 8000..8099 | Where-Object { $_ -ne $Port -and -not (Test-Port $_) } | Select-Object -First 1
        if (-not $apiPort) { throw 'Nenhuma porta local disponivel para o backend.' }
        $apiProcess = Start-Process $python -ArgumentList @('-m','uvicorn','main:app','--host','127.0.0.1','--port',"$apiPort") -WorkingDirectory (Join-Path $Root 'backend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Logs 'backend.out.log') -RedirectStandardError (Join-Path $Logs 'backend.error.log') -PassThru
        $deadline = (Get-Date).AddSeconds(90)
        while (-not (Test-Api $apiPort)) {
            if ($apiProcess.HasExited -or (Get-Date) -gt $deadline) { throw 'Backend nao iniciou. Consulte .logs\server\backend.error.log.' }
            Start-Sleep -Milliseconds 500
        }
    } else { Write-Host "Reutilizando backend local na porta $apiPort; a fila de extracao permanece unica." }
    $env:NEVETRACKER_BACKEND_URL = "http://127.0.0.1:$apiPort"
    $webProcess = Start-Process $python -ArgumentList @('-m','uvicorn','lan_server:app','--host','0.0.0.0','--port',"$Port") -WorkingDirectory (Join-Path $Root 'backend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Logs 'web.out.log') -RedirectStandardError (Join-Path $Logs 'web.error.log') -PassThru
    $deadline = (Get-Date).AddSeconds(30)
    while ($true) {
        try { $health = Invoke-RestMethod "http://127.0.0.1:$Port/server-health" -TimeoutSec 2; if ($health.service -eq 'NeveTracker LAN') { break } } catch {}
        if ($webProcess.HasExited -or (Get-Date) -gt $deadline) { throw 'Servidor de interface nao iniciou. Consulte .logs\server\web.error.log.' }
        Start-Sleep -Milliseconds 300
    }
    $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.AddressState -eq 'Preferred' } | Select-Object -ExpandProperty IPAddress -Unique)
    Write-Host "`nNeveTracker servidor pronto. Nesta maquina: http://127.0.0.1:$Port"
    foreach ($address in $addresses) { Write-Host ("Na mesma rede: http://{0}:{1}" -f $address,$Port) }
    if ($NoFirewall) { Write-Host 'Firewall nao alterado; o acesso remoto depende da regra existente.' }
    Write-Host 'Mantenha este console aberto. Ctrl+C encerra os processos iniciados aqui.'
    if (-not $env:NEVETRACKER_NO_BROWSER) { Open-ProjectBrowser "http://127.0.0.1:$Port" $Root }
    while ($true) {
        if ($webProcess.HasExited -or ($apiProcess -and $apiProcess.HasExited)) { throw 'Um dos servicos encerrou. Consulte .logs\server.' }
        Start-Sleep -Seconds 2
    }
} catch {
    Write-Host "ERRO: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    foreach ($ownedProcess in @($webProcess,$apiProcess)) {
        if ($ownedProcess) { try { if (-not $ownedProcess.HasExited) { Stop-Process -Id $ownedProcess.Id -Force } } catch {} }
    }
    if ($lockStream) { $lockStream.Dispose() }
}
