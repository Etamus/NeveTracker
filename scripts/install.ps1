[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$BackendDir = Join-Path $Root "backend"
$ToolsDir = Join-Path $Root ".tools"
$LogsDir = Join-Path $Root ".logs"
$VenvDir = Join-Path $BackendDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$InstallLock = Join-Path $Root ".install.lock"
$PythonVersion = "3.11.9"

New-Item -ItemType Directory -Path $ToolsDir, $LogsDir -Force | Out-Null
$LogFile = Join-Path $LogsDir "install.log"

function Write-Step([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}

function Invoke-Logged {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [int]$Attempts = 1
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        Write-Step ("Executando: {0} {1}" -f $FilePath, ($Arguments -join " "))
        $quotedArguments = $Arguments | ForEach-Object {
            '"' + ([string]$_).Replace('"', '\"') + '"'
        }
        $startInfo = New-Object Diagnostics.ProcessStartInfo
        $startInfo.FileName = $FilePath
        $startInfo.Arguments = $quotedArguments -join " "
        $startInfo.WorkingDirectory = (Get-Location).Path
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $startInfo
        if (-not $process.Start()) { throw "Nao foi possivel iniciar: $FilePath" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $commandOutput = @($stdoutTask.Result, $stderrTask.Result) | Where-Object { $_ }
        $exitCode = $process.ExitCode
        $process.Dispose()
        if ($commandOutput) { $commandOutput | Tee-Object -FilePath $LogFile -Append }
        if ($exitCode -eq 0) { return }
        if ($attempt -lt $Attempts) {
            Write-Step "Tentativa $attempt falhou (codigo $exitCode). Tentando novamente..."
            Start-Sleep -Seconds ([Math]::Min(8, 2 * $attempt))
        }
    }

    throw "Comando falhou apos $Attempts tentativa(s): $FilePath"
}

function Test-Python311([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $version = & $Path -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        return ($LASTEXITCODE -eq 0 -and ($version | Select-Object -Last 1) -eq "3.11")
    } catch {
        return $false
    }
}

function Resolve-Python311 {
    $candidates = New-Object System.Collections.Generic.List[string]

    try {
        $launcherPath = & py -3.11 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $launcherPath) { $candidates.Add(($launcherPath | Select-Object -Last 1)) }
    } catch {}

    foreach ($commandName in @("python", "python3.11")) {
        try {
            $command = Get-Command $commandName -ErrorAction Stop
            if ($command.Source) { $candidates.Add($command.Source) }
        } catch {}
    }

    $knownPaths = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:ProgramFiles "Python311\python.exe")
    )
    if (${env:ProgramFiles(x86)}) {
        $knownPaths += Join-Path ${env:ProgramFiles(x86)} "Python311\python.exe"
    }
    foreach ($knownPath in $knownPaths) { if ($knownPath) { $candidates.Add($knownPath) } }

    foreach ($registryPath in @(
        "HKCU:\Software\Python\PythonCore\3.11\InstallPath",
        "HKLM:\Software\Python\PythonCore\3.11\InstallPath",
        "HKLM:\Software\WOW6432Node\Python\PythonCore\3.11\InstallPath"
    )) {
        try {
            $installPath = (Get-Item -LiteralPath $registryPath).GetValue("")
            if ($installPath) { $candidates.Add((Join-Path $installPath "python.exe")) }
        } catch {}
    }

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-Python311 $candidate) { return [IO.Path]::GetFullPath($candidate) }
    }
    return $null
}

function Install-Python311 {
    Write-Step "Python 3.11 nao foi encontrado. Iniciando instalacao automatica."

    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        try {
            Invoke-Logged $winget.Source @(
                "install", "--id", "Python.Python.3.11", "--exact", "--silent",
                "--scope", "user", "--accept-package-agreements", "--accept-source-agreements",
                "--disable-interactivity"
            ) 1
            $resolved = Resolve-Python311
            if ($resolved) { return $resolved }
        } catch {
            Write-Step "WinGet nao concluiu a instalacao. Usando o instalador oficial como alternativa."
        }
    } else {
        Write-Step "WinGet nao esta disponivel. Usando o instalador oficial do Python."
    }

    $architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
    switch ($architecture) {
        "arm64" { $fileName = "python-$PythonVersion-arm64.exe" }
        "x86" { $fileName = "python-$PythonVersion.exe" }
        default { $fileName = "python-$PythonVersion-amd64.exe" }
    }

    $downloadDir = Join-Path $ToolsDir "downloads"
    New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
    $installer = Join-Path $downloadDir $fileName
    $downloadUrl = "https://www.python.org/ftp/python/$PythonVersion/$fileName"

    if (Test-Path -LiteralPath $installer) {
        $existingSignature = Get-AuthenticodeSignature -LiteralPath $installer
        if (
            $existingSignature.Status -ne "Valid" -or
            $existingSignature.SignerCertificate.Subject -notmatch "Python Software Foundation"
        ) {
            Write-Step "Download anterior do Python esta incompleto ou invalido. Baixando novamente."
            Remove-Item -LiteralPath $installer -Force
        }
    }

    if (-not (Test-Path -LiteralPath $installer)) {
        Write-Step "Baixando Python $PythonVersion de python.org..."
        $downloaded = $false
        try {
            Invoke-WebRequest -Uri $downloadUrl -OutFile $installer -UseBasicParsing -TimeoutSec 180
            $downloaded = $true
        } catch {
            Write-Step "Download via PowerShell falhou. Tentando BITS/curl."
        }
        if (-not $downloaded -and (Get-Command Start-BitsTransfer -ErrorAction SilentlyContinue)) {
            try {
                Start-BitsTransfer -Source $downloadUrl -Destination $installer -ErrorAction Stop
                $downloaded = $true
            } catch {}
        }
        if (-not $downloaded) {
            $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
            if ($curl) {
                Invoke-Logged $curl.Source @("--fail", "--location", "--retry", "3", "--output", $installer, $downloadUrl) 1
                $downloaded = $true
            }
        }
        if (-not $downloaded -or -not (Test-Path -LiteralPath $installer)) {
            throw "Nao foi possivel baixar o instalador oficial do Python. Verifique internet, proxy ou antivirus."
        }
    }

    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "Python Software Foundation") {
        throw "A assinatura digital do instalador do Python nao e valida. O arquivo nao sera executado."
    }

    Write-Step "Instalando Python $PythonVersion para o usuario atual..."
    $process = Start-Process -FilePath $installer -ArgumentList @(
        "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1",
        "InstallLauncherAllUsers=0", "Include_pip=1", "Include_test=0",
        "Include_doc=0", "Shortcuts=0"
    ) -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -notin @(0, 3010)) {
        throw "O instalador do Python terminou com o codigo $($process.ExitCode)."
    }

    $resolved = Resolve-Python311
    if (-not $resolved) { throw "Python foi instalado, mas o executavel 3.11 nao foi localizado." }
    return $resolved
}

function Remove-SafeDirectory([string]$Target, [string]$Parent) {
    if (-not (Test-Path -LiteralPath $Target)) { return }
    $targetPath = [IO.Path]::GetFullPath($Target).TrimEnd('\')
    $parentPath = [IO.Path]::GetFullPath($Parent).TrimEnd('\')
    if ($targetPath -eq $parentPath -or -not $targetPath.StartsWith($parentPath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Remocao recusada fora da pasta esperada: $targetPath"
    }
    Remove-Item -LiteralPath $targetPath -Recurse -Force
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

function Resolve-NodeTools {
    $envJson = Join-Path $ToolsDir "node\env.json"
    if (Test-Path -LiteralPath $envJson) {
        try {
            $configured = Get-Content -LiteralPath $envJson -Raw | ConvertFrom-Json
            if ((Test-Path -LiteralPath $configured.node_exe) -and (Test-Path -LiteralPath $configured.npm_cmd)) {
                return @{ Node = $configured.node_exe; Npm = $configured.npm_cmd }
            }
        } catch {}
    }
    $nodes = Get-ChildItem -LiteralPath (Join-Path $ToolsDir "node") -Filter "node.exe" -Recurse -File -ErrorAction SilentlyContinue
    foreach ($node in ($nodes | Sort-Object FullName -Descending)) {
        $npm = Join-Path $node.DirectoryName "npm.cmd"
        if (Test-Path -LiteralPath $npm) { return @{ Node = $node.FullName; Npm = $npm } }
    }
    throw "Node portatil nao foi localizado depois da preparacao."
}

$lockCreated = $false
try {
    if (Test-Path -LiteralPath $InstallLock) {
        $age = (Get-Date) - (Get-Item -LiteralPath $InstallLock).LastWriteTime
        if ($age.TotalMinutes -lt 30) { throw "Ja existe uma instalacao em andamento." }
        Remove-SafeDirectory $InstallLock $Root
    }
    New-Item -ItemType Directory -Path $InstallLock -Force | Out-Null
    $lockCreated = $true

    Write-Step "=== NeveTracker: instalacao e reparo ==="
    if (-not (Test-Path -LiteralPath (Join-Path $BackendDir "main.py"))) { throw "Backend nao encontrado." }
    if (-not (Test-Path -LiteralPath (Join-Path $Root "package.json"))) { throw "Frontend nao encontrado." }

    $python = Resolve-Python311
    if (-not $python) { $python = Install-Python311 }
    Write-Step "Python pronto: $python"

    $nodeHelper = Join-Path $PSScriptRoot "ensure_node.py"
    Invoke-Logged $python @($nodeHelper, $Root) 3
    $nodeTools = Resolve-NodeTools
    Write-Step "Node pronto: $($nodeTools.Node)"

    if (-not (Test-Python311 $VenvPython)) {
        if (Test-Path -LiteralPath $VenvDir) {
            Write-Step "Recriando ambiente virtual incompleto ou incompativel."
            Remove-SafeDirectory $VenvDir $BackendDir
        }
        Invoke-Logged $python @("-m", "venv", $VenvDir) 2
    }

    Invoke-Logged $VenvPython @("-m", "ensurepip", "--upgrade") 2
    Invoke-Logged $VenvPython @("-m", "pip", "install", "--disable-pip-version-check", "--retries", "5", "--timeout", "60", "--upgrade", "pip", "setuptools", "wheel") 3
    Invoke-Logged $VenvPython @("-m", "pip", "install", "--disable-pip-version-check", "--retries", "5", "--timeout", "60", "-r", (Join-Path $BackendDir "requirements.txt")) 3

    $dbPath = (Join-Path $BackendDir "precos.db").Replace('\', '/')
    Set-EnvValue (Join-Path $BackendDir ".env") "DATABASE_URL" "sqlite:///$dbPath"
    Set-EnvValue (Join-Path $Root ".env.local") "VITE_API_URL" "http://127.0.0.1:8000"
    Set-EnvValue (Join-Path $Root ".env.local") "VITE_BASE_PATH" "/"

    Push-Location $Root
    try {
        $installArgs = if (Test-Path -LiteralPath (Join-Path $Root "package-lock.json")) { @("ci", "--no-audit", "--no-fund") } else { @("install", "--no-audit", "--no-fund") }
        try {
            Invoke-Logged $nodeTools.Npm $installArgs 2
        } catch {
            Write-Step "npm ci nao concluiu. Reparando dependencias com npm install."
            Invoke-Logged $nodeTools.Npm @("install", "--no-audit", "--no-fund") 2
        }
        Invoke-Logged $nodeTools.Npm @("run", "build") 1
    } finally {
        Pop-Location
    }

    Push-Location $BackendDir
    try {
        Invoke-Logged $VenvPython @("-c", "import fastapi, uvicorn, sqlalchemy, openpyxl; import main; print('Backend validado')") 1
    } finally {
        Pop-Location
    }

    $state = @{
        installed_at = (Get-Date).ToUniversalTime().ToString("o")
        python = $python
        node = $nodeTools.Node
        requirements_sha256 = (Get-FileHash -LiteralPath (Join-Path $BackendDir "requirements.txt") -Algorithm SHA256).Hash
        package_lock_sha256 = (Get-FileHash -LiteralPath (Join-Path $Root "package-lock.json") -Algorithm SHA256).Hash
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ToolsDir "install-state.json") -Encoding UTF8

    Write-Step "SUCESSO: ambiente validado. Use iniciar.bat."
    exit 0
} catch {
    Write-Step "ERRO: $($_.Exception.Message)"
    Write-Step "Consulte o log completo em $LogFile"
    exit 1
} finally {
    if ($lockCreated -and (Test-Path -LiteralPath $InstallLock)) {
        Remove-SafeDirectory $InstallLock $Root
    }
}
