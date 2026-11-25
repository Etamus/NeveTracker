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
    try {
        $rule = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
        if ($rule -and $rule.Enabled -eq 'True') { return }
        if ($rule) {
            Set-NetFirewallRule -Name $name -Enabled True -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Profile Any | Out-Null
        } else {
            New-NetFirewallRule -Name $name -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Profile Any | Out-Null
        }
    } catch {
        $netsh = Join-Path $env:SystemRoot 'System32\netsh.exe'
        $ruleArguments = @('enable=yes', 'dir=in', 'action=allow', 'protocol=TCP', "localport=$Port", 'remoteip=localsubnet', 'profile=any')
        & $netsh advfirewall firewall set rule "name=$name" new @ruleArguments | Out-Null
        if ($LASTEXITCODE -ne 0) {
            & $netsh advfirewall firewall add rule "name=$name" @ruleArguments | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'O Windows recusou a regra de firewall para a rede local.' }
        }
    }
}
if ($ConfigureFirewallOnly) {
    try { Set-LanFirewall; exit 0 } catch { Write-Error $_; exit 1 }
}

function Get-LoopbackJson([int]$Number, [string]$Path) {
    try { $probe.GetStringAsync("http://127.0.0.1:${Number}$Path").GetAwaiter().GetResult() | ConvertFrom-Json }
    catch { return $null }
}
function Test-Api([int]$ApiPort) {
    if ((Get-LoopbackJson $ApiPort '/').status -ne 'API Online') { return $false }
    return (Get-LoopbackJson $ApiPort '/openapi.json').info.title -eq 'NeveTracker'
}
function Test-PortFree([int]$Number) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, $Number)
    try {
        $listener.ExclusiveAddressUse = $true
        $listener.Start()
        return $true
    } catch [Net.Sockets.SocketException] { return $false } finally { $listener.Stop() }
}
function Resolve-ServerNode {
    $candidates = @(Get-ChildItem -LiteralPath (Join-Path $Root '.tools\node') -Filter node.exe -Recurse -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -ExpandProperty FullName)
    $file = Join-Path $Root '.tools\node\env.json'
    if (Test-Path -LiteralPath $file) {
        try {
            $config = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
            $candidates += $config.node_exe
        } catch {}
    }
    $candidates += (Get-Command node.exe -CommandType Application -ErrorAction SilentlyContinue).Source
    $candidates += Join-Path $env:ProgramFiles 'nodejs\node.exe'
    foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            $versionText = & $candidate --version 2>$null
            if ($LASTEXITCODE -ne 0 -or $versionText -notmatch '^v(\d+\.\d+\.\d+)') { continue }
            $version = [version]$Matches[1]
            if (($version.Major -eq 20 -and $version.Minor -ge 19) -or $version -ge [version]'22.12.0') { return $candidate }
        } catch { continue }
    }
}
function Resolve-ServerPython {
    $projectPython = Join-Path $Root 'backend\.venv\Scripts\python.exe'
    $launcher = (Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    $installedPython = Get-Command python.exe -CommandType Application -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notlike '*\WindowsApps\*' } | Select-Object -ExpandProperty Source
    $candidates = @($projectPython) + @($installedPython)
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            & $candidate -c $pythonCheck *> $null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch { continue }
    }
    if ($launcher) {
        try {
            $candidate = & $launcher -3.11 -c "$pythonCheck; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $candidate }
        } catch {}
    }
}
function Test-LanBuild([string]$Directory) {
    return ((Test-Path -LiteralPath (Join-Path $Directory 'index.html') -PathType Leaf) -and
        [bool](Get-ChildItem -LiteralPath (Join-Path $Directory 'assets') -Filter '*.js' -File -ErrorAction SilentlyContinue | Select-Object -First 1))
}
function Build-LanInterface([string]$Node, [string]$Destination) {
    if (-not $Node -or -not (Test-Path -LiteralPath $vite -PathType Leaf)) { return $false }
    Set-NodeEnvironment $Node
    Write-Host 'Compilando interface para a rede local...'
    $previousApi = $env:VITE_API_URL
    $previousBase = $env:VITE_BASE_PATH
    $env:VITE_API_URL = '/api'
    $env:VITE_BASE_PATH = '/'
    Push-Location $Root
    try {
        $ErrorActionPreference = 'Continue'
        $buildArgs = @($vite, 'build', '--outDir', $Destination, '--emptyOutDir')
        & $Node @buildArgs --configLoader runner *> (Join-Path $Logs 'build.log')
        if ($LASTEXITCODE -ne 0) {
            & $Node @buildArgs *>> (Join-Path $Logs 'build.log')
            if ($LASTEXITCODE -ne 0) { return $false }
        }
        return (Test-LanBuild $Destination)
    } finally {
        Pop-Location
        $env:VITE_API_URL = $previousApi
        $env:VITE_BASE_PATH = $previousBase
    }
}
function Repair-ServerDependencies {
    try {
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install.ps1')
        if ($LASTEXITCODE -ne 0) { Write-Warning 'Instalacao/reparo incompleto. Consulte .logs\install.log.' }
    } catch { Write-Warning "Instalacao/reparo indisponivel. $($_.Exception.Message)" }
}
function Get-LanAddresses {
    try {
        $addresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.AddressState -eq 'Preferred' } | Select-Object -ExpandProperty IPAddress
    } catch {
        $addresses = foreach ($adapter in [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
            if ($adapter.OperationalStatus -eq 'Up') {
                $adapter.GetIPProperties().UnicastAddresses | Where-Object { $_.Address.AddressFamily -eq 'InterNetwork' } |
                    ForEach-Object { $_.Address.IPAddressToString }
            }
        }
    }
    $addresses | Where-Object { $_ -notlike '127.*' -and $_ -notlike '169.254.*' } | Select-Object -Unique
}
function Open-LanBrowser {
    if ($env:NEVETRACKER_NO_BROWSER) { return }
    try { Open-ProjectBrowser "http://127.0.0.1:$Port" $Root }
    catch { Write-Warning "Abra manualmente http://127.0.0.1:$Port no navegador." }
}

$apiProcess = $null
$webProcess = $null
$lockStream = $null
$probe = $null
try {
    Add-Type -AssemblyName System.Net.Http
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $handler.AllowAutoRedirect = $false
    $probe = [Net.Http.HttpClient]::new($handler)
    $probe.Timeout = [TimeSpan]::FromSeconds(2)
    $requestedPort = $Port
    $selectedPort = $null
    foreach ($candidatePort in $requestedPort..([Math]::Min(65535, $requestedPort + 20))) {
        if (Test-PortFree $candidatePort) {
            $selectedPort = $candidatePort
            break
        }
        if ((Get-LoopbackJson $candidatePort '/server-health').service -eq 'NeveTracker LAN') {
            $Port = $candidatePort
            Write-Host "NeveTracker servidor ja esta disponivel na porta $Port."
            Open-LanBrowser
            exit 0
        }
    }
    if (-not $selectedPort) { throw "Nenhuma porta para a interface a partir de $requestedPort. Informe outra com servidor.bat -Port." }
    $Port = $selectedPort
    if ($Port -ne $requestedPort) { Write-Warning "A porta $requestedPort esta ocupada. Usando a porta $Port." }
    # Exclusive handle is released by Windows even on a crash.
    try { $lockStream = [IO.File]::Open((Join-Path $Logs 'launcher.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
    catch [IO.IOException] { throw 'Outro servidor deste projeto esta iniciando ou a pasta .logs\server esta indisponivel.' }
    $pythonCheck = 'import sys; assert sys.version_info[:2] >= (3, 11); import fastapi, uvicorn, httpx, scrapy, sqlalchemy, openpyxl, playwright'
    $python = Resolve-ServerPython
    $node = Resolve-ServerNode
    $vite = Join-Path $Root 'node_modules\vite\bin\vite.js'
    $buildRoot = Join-Path $Root '.tools\lan-build'
    $buildMarker = Join-Path $buildRoot 'current.txt'
    $savedBuild = $null
    if (Test-Path -LiteralPath $buildMarker -PathType Leaf) {
        $slot = (Get-Content -LiteralPath $buildMarker -Raw).Trim()
        if ($slot -in @('release-a', 'release-b')) {
            $savedPath = Join-Path $buildRoot $slot
            if (Test-LanBuild $savedPath) { $savedBuild = $savedPath }
        }
    }

    $repairAttempted = $false
    if (-not $python -or (-not $savedBuild -and (-not $node -or -not (Test-Path -LiteralPath $vite -PathType Leaf)))) {
        # install.ps1 can repair a virtual environment copied from another PC.
        Repair-ServerDependencies
        $repairAttempted = $true
        $python = Resolve-ServerPython
        $node = Resolve-ServerNode
    }
    if (-not $python) { throw 'Python ou dependencias do backend indisponiveis. Consulte .logs\install.log.' }

    $build = $null
    $nextSlot = if ($savedBuild -and $slot -eq 'release-a') { 'release-b' } else { 'release-a' }
    $destination = Join-Path $buildRoot $nextSlot
    New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
    $built = Build-LanInterface $node $destination
    if (-not $built -and -not $savedBuild -and -not $repairAttempted) {
        Repair-ServerDependencies
        $node = Resolve-ServerNode
        $python = Resolve-ServerPython
        if (-not $python) { throw 'Python indisponivel apos o reparo. Consulte .logs\install.log.' }
        $built = Build-LanInterface $node $destination
    }
    if ($built) {
        $build = $destination
        Set-Content -LiteralPath $buildMarker -Value $nextSlot -Encoding ASCII
    }
    if (-not $build) {
        if (-not $savedBuild) { throw 'Interface indisponivel. Consulte .logs\server\build.log e .logs\install.log.' }
        $build = $savedBuild
        Write-Warning 'Usando a ultima interface compilada para LAN. Consulte .logs\server\build.log.'
    }

    if (-not $NoFirewall) {
        try { Set-LanFirewall } catch {
            Write-Host 'O Windows precisa autorizar a regra de firewall para acesso pela rede local.'
            try {
                $helper = Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -ArgumentList @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'), '-ConfigureFirewallOnly', '-Port', "$Port") -Wait -PassThru
                if ($helper.ExitCode -ne 0) { throw 'Regra nao aplicada.' }
            } catch {
                Write-Warning 'O servidor vai iniciar. O acesso pela rede depende das regras de firewall existentes.'
            }
        }
    }
    $apiPort = $null
    foreach ($candidatePort in 8000..8099) {
        if (-not (Test-PortFree $candidatePort) -and (Test-Api $candidatePort)) { $apiPort = $candidatePort; break }
    }
    if (-not $apiPort) {
        $apiPort = 8000..8099 | Where-Object { $_ -ne $Port -and (Test-PortFree $_) } | Select-Object -First 1
        if (-not $apiPort) { throw 'Nenhuma porta local disponivel para o backend.' }
        $apiProcess = Start-Process $python -ArgumentList @('-m','uvicorn','main:app','--host','127.0.0.1','--port',"$apiPort") -WorkingDirectory (Join-Path $Root 'backend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Logs 'backend.out.log') -RedirectStandardError (Join-Path $Logs 'backend.error.log') -PassThru
        $deadline = (Get-Date).AddSeconds(90)
        while (-not (Test-Api $apiPort)) {
            if ($apiProcess.HasExited -or (Get-Date) -gt $deadline) { throw 'Backend nao iniciou. Consulte .logs\server\backend.error.log.' }
            Start-Sleep -Milliseconds 500
        }
    } else {
        Write-Host "Reutilizando backend local na porta $apiPort; a fila de extracao permanece unica."
        $controlSchema = (Get-LoopbackJson $apiPort '/openapi.json').components.schemas.CrawlerControlRequest
        if (-not $controlSchema.properties.hardware_acceleration) {
            Write-Warning 'O backend existente pode estar desatualizado. Encerre a instancia anterior e abra o NeveTracker novamente para usar a aceleracao precisa.'
        }
    }
    $env:NEVETRACKER_BACKEND_URL = "http://127.0.0.1:$apiPort"
    $env:NEVETRACKER_LAN_DIST = $build
    $webProcess = Start-Process $python -ArgumentList @('-m','uvicorn','lan_server:app','--host','0.0.0.0','--port',"$Port") -WorkingDirectory (Join-Path $Root 'backend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Logs 'web.out.log') -RedirectStandardError (Join-Path $Logs 'web.error.log') -PassThru
    $deadline = (Get-Date).AddSeconds(30)
    while ($true) {
        if ((Get-LoopbackJson $Port '/server-health').service -eq 'NeveTracker LAN') { break }
        if ($webProcess.HasExited -or (Get-Date) -gt $deadline) { throw 'Servidor de interface nao iniciou. Consulte .logs\server\web.error.log.' }
        Start-Sleep -Milliseconds 300
    }
    $addresses = @(Get-LanAddresses)
    Write-Host "`nNeveTracker servidor pronto. Nesta maquina: http://127.0.0.1:$Port"
    foreach ($address in $addresses) { Write-Host ("Na mesma rede: http://{0}:{1}" -f $address,$Port) }
    if ($NoFirewall) { Write-Host 'Firewall nao alterado; o acesso remoto depende da regra existente.' }
    Write-Host 'Mantenha este console aberto. Ctrl+C encerra os processos iniciados aqui.'
    Open-LanBrowser
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
    if ($probe) { $probe.Dispose() }
}
