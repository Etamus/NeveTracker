[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$BackendDir = Join-Path $Root "backend"
$VenvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
$LogsDir = Join-Path $Root ".logs"
$InstallScript = Join-Path $PSScriptRoot "install.ps1"
$InstallSchemaVersion = 2
. (Join-Path $PSScriptRoot "node_environment.ps1")
. (Join-Path $PSScriptRoot "project_browser.ps1")
$script:StartedProcesses = New-Object System.Collections.Generic.List[System.Diagnostics.Process]

New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null

function Write-Step([string]$Message) {
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Message)
}

function Test-HttpService([string]$Url, [string]$ExpectedText = "") {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 500) { return $false }
        return (-not $ExpectedText -or $response.Content -match [Regex]::Escape($ExpectedText))
    } catch {
        return $false
    }
}

function Test-TcpPort([int]$Port) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        return ($task.Wait(350) -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-FreePort([int]$Preferred, [int]$Maximum) {
    foreach ($port in $Preferred..$Maximum) {
        if (-not (Test-TcpPort $port)) { return $port }
    }
    throw "Nenhuma porta livre entre $Preferred e $Maximum."
}

function Set-EnvValue([string]$Path, [string]$Key, [string]$Value) {
    $lines = if (Test-Path -LiteralPath $Path) { @(Get-Content -LiteralPath $Path) } else { @() }
    $output = New-Object System.Collections.Generic.List[string]
    $found = $false
    foreach ($line in $lines) {
        if ($line.StartsWith("$Key=", [StringComparison]::OrdinalIgnoreCase)) {
            $output.Add("$Key=$Value")
            $found = $true
        } else { $output.Add($line) }
    }
    if (-not $found) { $output.Add("$Key=$Value") }
    Set-Content -LiteralPath $Path -Value $output -Encoding ASCII
}

function Resolve-Node {
    $envJson = Join-Path $Root ".tools\node\env.json"
    if (Test-Path -LiteralPath $envJson) {
        try {
            $configured = Get-Content -LiteralPath $envJson -Raw | ConvertFrom-Json
            if (
                (Test-Path -LiteralPath $configured.node_exe -PathType Leaf) -and
                (Test-Path -LiteralPath (Join-Path ([IO.Path]::GetDirectoryName($configured.node_exe)) "npm.cmd") -PathType Leaf)
            ) { return $configured.node_exe }
        } catch {}
    }
    $nodeRoot = Join-Path $Root ".tools\node"
    $nodes = Get-ChildItem -LiteralPath $nodeRoot -Filter "node.exe" -Recurse -File -ErrorAction SilentlyContinue
    foreach ($node in ($nodes | Sort-Object FullName -Descending)) {
        if (Test-Path -LiteralPath (Join-Path $node.DirectoryName "npm.cmd")) { return $node.FullName }
    }
    return $null
}

function Test-ProjectReady {
    if (-not (Test-Path -LiteralPath $VenvPython)) { return $false }
    $node = Resolve-Node
    if (-not $node) { return $false }
    Set-NodeEnvironment $node
    if (-not (Test-Path -LiteralPath (Join-Path $Root "node_modules\vite\bin\vite.js"))) { return $false }
    if (-not (Test-Path -LiteralPath (Join-Path $Root "dist\index.html"))) { return $false }
    $statePath = Join-Path $Root ".tools\install-state.json"
    if (-not (Test-Path -LiteralPath $statePath)) { return $false }
    try {
        $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        if ([int]$state.schema_version -ne $InstallSchemaVersion) { return $false }
        if (-not $state.playwright_executable -or -not (Test-Path -LiteralPath $state.playwright_executable)) { return $false }
        if (-not $state.frontend_index -or -not (Test-Path -LiteralPath $state.frontend_index)) { return $false }
        if ($state.node -ne $node -or $state.frontend_index -ne (Join-Path $Root "dist\index.html")) { return $false }
        $requirementsHash = (Get-FileHash -LiteralPath (Join-Path $BackendDir "requirements.txt") -Algorithm SHA256).Hash
        $packageLockHash = (Get-FileHash -LiteralPath (Join-Path $Root "package-lock.json") -Algorithm SHA256).Hash
        if ($state.requirements_sha256 -ne $requirementsHash -or $state.package_lock_sha256 -ne $packageLockHash) {
            return $false
        }
        $package = Get-Content -LiteralPath (Join-Path $Root "package.json") -Raw | ConvertFrom-Json
        $packageNames = @($package.dependencies.PSObject.Properties.Name) + @($package.devDependencies.PSObject.Properties.Name)
        foreach ($packageName in $packageNames) {
            if (-not (Test-Path -LiteralPath (Join-Path $Root "node_modules\$packageName\package.json"))) { return $false }
        }
        & $node --version 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { return $false }
        $esbuild = Join-Path $Root "node_modules\esbuild\bin\esbuild"
        if (-not (Test-Path -LiteralPath $esbuild -PathType Leaf)) { return $false }
        & $node $esbuild --version 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { return $false }
        & $VenvPython -m pip check 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { return $false }
        & $VenvPython -c "import fastapi, uvicorn, sqlalchemy, openpyxl, multipart, scrapy, playwright; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Wait-ForService([string]$Url, [string]$ExpectedText, [int]$Seconds, [Diagnostics.Process]$Process) {
    for ($attempt = 0; $attempt -lt $Seconds; $attempt++) {
        if (Test-HttpService $Url $ExpectedText) { return $true }
        if ($Process -and $Process.HasExited) { return $false }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Show-LogTail([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        Write-Host ""
        Write-Host "Ultimas mensagens de $Path"
        Get-Content -LiteralPath $Path -Tail 25
    }
}

try {
    Write-Host ""
    Write-Step "=== NeveTracker: inicializacao local ==="

    if (-not (Test-ProjectReady)) {
        Write-Step "Ambiente ausente ou incompleto. Executando instalacao/reparo automatico..."
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $InstallScript
        if ($LASTEXITCODE -ne 0 -or -not (Test-ProjectReady)) {
            throw "A instalacao automatica nao conseguiu validar todas as dependencias."
        }
    }

    $node = Resolve-Node
    Set-NodeEnvironment $node
    $backendPort = 8000
    $backendUrl = "http://127.0.0.1:$backendPort"
    $backendAlreadyRunning = Test-HttpService "$backendUrl/" "API Online"
    if (-not $backendAlreadyRunning -and (Test-TcpPort $backendPort)) {
        $backendPort = Get-FreePort 8001 8099
        $backendUrl = "http://127.0.0.1:$backendPort"
        Write-Step "A porta 8000 pertence a outro programa. Usando $backendPort."
    }

    $frontendPort = 5173
    $frontendUrl = "http://127.0.0.1:$frontendPort"
    $frontendAlreadyRunning = Test-HttpService "$frontendUrl/" "NeveTracker"
    if ($backendPort -ne 8000 -and $frontendAlreadyRunning) {
        $frontendAlreadyRunning = $false
    }
    if (-not $frontendAlreadyRunning -and (Test-TcpPort $frontendPort)) {
        $frontendPort = Get-FreePort 5174 5273
        $frontendUrl = "http://127.0.0.1:$frontendPort"
        Write-Step "A porta 5173 pertence a outro programa. Usando $frontendPort."
    }

    $dbPath = (Join-Path $BackendDir "precos.db").Replace('\', '/')
    Set-EnvValue (Join-Path $BackendDir ".env") "DATABASE_URL" "sqlite:///$dbPath"
    Set-EnvValue (Join-Path $Root ".env.local") "VITE_API_URL" $backendUrl
    Set-EnvValue (Join-Path $Root ".env.local") "VITE_BASE_PATH" "/"

    $backendProcess = $null
    if (-not $backendAlreadyRunning) {
        $backendOut = Join-Path $LogsDir "backend.out.log"
        $backendErr = Join-Path $LogsDir "backend.error.log"
        Write-Step "Iniciando backend em $backendUrl..."
        $backendProcess = Start-Process -FilePath $VenvPython -ArgumentList @(
            "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "$backendPort"
        ) -WorkingDirectory $BackendDir -WindowStyle Hidden -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr -PassThru
        $script:StartedProcesses.Add($backendProcess)
    } else {
        Write-Step "Backend existente validado em $backendUrl."
    }

    Write-Step "Aguardando backend responder..."
    if (-not (Wait-ForService "$backendUrl/" "API Online" 90 $backendProcess)) {
        Show-LogTail (Join-Path $LogsDir "backend.error.log")
        throw "Backend nao respondeu em ate 90 segundos."
    }

    $frontendProcess = $null
    if (-not $frontendAlreadyRunning) {
        $vite = Join-Path $Root "node_modules\vite\bin\vite.js"
        $frontendOut = Join-Path $LogsDir "frontend.out.log"
        $frontendErr = Join-Path $LogsDir "frontend.error.log"
        $quotedVite = '"' + $vite + '"'
        Write-Step "Iniciando frontend em $frontendUrl..."
        $frontendProcess = Start-Process -FilePath $node -ArgumentList @(
            $quotedVite, "--host", "127.0.0.1", "--port", "$frontendPort", "--strictPort"
        ) -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr -PassThru
        $script:StartedProcesses.Add($frontendProcess)
    } else {
        Write-Step "Frontend existente encontrado em $frontendUrl."
    }

    Write-Step "Aguardando frontend responder..."
    if (-not (Wait-ForService "$frontendUrl/" "NeveTracker" 90 $frontendProcess)) {
        Show-LogTail (Join-Path $LogsDir "frontend.error.log")
        throw "Frontend nao respondeu em ate 90 segundos."
    }

    Write-Host ""
    Write-Step "NeveTracker pronto."
    Write-Host "Backend:  $backendUrl"
    Write-Host "Frontend: $frontendUrl"
    Write-Host "Logs:     $LogsDir"

    if (-not $env:NEVETRACKER_NO_BROWSER) {
        try {
            Open-ProjectBrowser $frontendUrl $Root
        } catch {
            Write-Step "Nao foi possivel abrir o navegador automaticamente. Acesse $frontendUrl manualmente."
        }
    }
    if ($env:NEVETRACKER_EXIT_AFTER_READY) { exit 0 }

    Write-Host ""
    Write-Host "Este console permanecera aberto. Pressione Ctrl+C para encerrar os processos iniciados aqui."
    while ($true) {
        foreach ($process in $script:StartedProcesses) {
            if ($process.HasExited) {
                $component = if ($backendProcess -and $process.Id -eq $backendProcess.Id) { "Backend" } else { "Frontend" }
                $errorLog = if ($component -eq "Backend") { "backend.error.log" } else { "frontend.error.log" }
                Show-LogTail (Join-Path $LogsDir $errorLog)
                throw "$component encerrou inesperadamente (PID $($process.Id), codigo $($process.ExitCode)). Consulte $errorLog."
            }
        }
        Start-Sleep -Seconds 2
    }
} catch {
    Write-Host ""
    Write-Host "ERRO: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    foreach ($process in $script:StartedProcesses) {
        try {
            if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
        } catch {}
    }
}
