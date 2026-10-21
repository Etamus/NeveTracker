function Set-NodeEnvironment([string]$NodeExe) {
    if (-not $NodeExe -or -not (Test-Path -LiteralPath $NodeExe -PathType Leaf)) {
        throw "Executavel do Node nao encontrado: $NodeExe"
    }

    $nodeDirectory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($NodeExe))
    $firstEntry = ($env:Path -split ';', 2)[0]
    if (-not [string]::Equals($firstEntry, $nodeDirectory, [StringComparison]::OrdinalIgnoreCase)) {
        $env:Path = "$nodeDirectory;$env:Path"
    }
}
