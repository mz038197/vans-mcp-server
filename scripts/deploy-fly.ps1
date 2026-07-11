#requires -Version 5.1
param(
    [switch]$SecretsOnly,
    [switch]$SkipSecrets,
    [string]$AppName = 'vans-mcp-server'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$SecretsFile = Join-Path $HOME '.vans-mcp-server\fly.secrets.env'
$ExampleSecrets = Join-Path $RepoRoot 'config\fly.secrets.env.example'

function Get-FlyCmd {
    foreach ($name in @('fly', 'flyctl')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    Write-Host 'flyctl not found. Install: winget install Fly-io.flyctl' -ForegroundColor Yellow
    exit 1
}

function Invoke-Fly {
    param(
        [Parameter(Mandatory)]
        [string[]]$FlyArgs,
        [switch]$Quiet
    )
    $fly = Get-FlyCmd
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = & $fly @FlyArgs 2>&1
        $code = $LASTEXITCODE
        $lines = @($raw | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() }
            else { "$_" }
        })
        $lines = @($lines | Where-Object { $_ -and $_ -notmatch 'Metrics token unavailable' })
        if (-not $Quiet) { foreach ($line in $lines) { Write-Host $line } }
    } finally {
        $ErrorActionPreference = $prev
    }
    return @{ ExitCode = $code; Lines = $lines; Text = ($lines -join [Environment]::NewLine) }
}

function Test-FlyAuth {
    $result = Invoke-Fly -FlyArgs @('auth', 'whoami') -Quiet
    if ($result.ExitCode -ne 0 -and $result.Text -notmatch '@') {
        Write-Host 'Not logged in. Run: fly auth login' -ForegroundColor Yellow
        exit 1
    }
}

function Ensure-SecretsFile {
    if (Test-Path $SecretsFile) { return }
    $dir = Split-Path $SecretsFile -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    if (Test-Path $ExampleSecrets) {
        Copy-Item $ExampleSecrets $SecretsFile
        Write-Host "Created $SecretsFile — fill in values, then re-run." -ForegroundColor Yellow
        exit 1
    }
    exit 1
}

function Read-SecretsLines {
    param([string]$Path)
    $text = [System.IO.File]::ReadAllText($Path)
    if ($text.Length -gt 0 -and [int][char]$text[0] -eq 0xFEFF) { $text = $text.Substring(1) }
    return @($text -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object {
        $_ -and $_ -notmatch '^\s*#' -and $_ -match '='
    })
}

function Import-FlySecrets {
    Ensure-SecretsFile
    $payload = Read-SecretsLines -Path $SecretsFile
    $setArgs = @('secrets', 'set')
    foreach ($line in $payload) {
        if ($line -match '^([^=]+)=(.*)$') {
            $name = $Matches[1].Trim()
            if ($name -match '[^\w]') { Write-Host "Invalid secret name: $name"; exit 1 }
            $setArgs += "$name=$($Matches[2])"
        }
    }
    if ($setArgs.Count -le 2) { Write-Host 'No secrets parsed'; exit 1 }
    $setArgs += '--app', $AppName
    $result = Invoke-Fly -FlyArgs $setArgs
    if ($result.ExitCode -ne 0) { exit $result.ExitCode }
}

Set-Location $RepoRoot
Test-FlyAuth

$list = Invoke-Fly -FlyArgs @('apps', 'list') -Quiet
if ($list.Text -notmatch [regex]::Escape($AppName)) {
    Invoke-Fly -FlyArgs @('apps', 'create', $AppName) | Out-Null
}

if (-not $SkipSecrets) { Import-FlySecrets }
if ($SecretsOnly) { Write-Host 'Secrets updated.'; exit 0 }

$deploy = Invoke-Fly -FlyArgs @('deploy', '--app', $AppName, '--ha=false')
exit $deploy.ExitCode
