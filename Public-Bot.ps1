param(
    [ValidateSet('up', 'down', 'status', 'logs', 'url')]
    [string]$Action = 'up',
    [switch]$OpenBrowser
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Install and start Docker Desktop with Linux containers, then run Start-Bot.cmd again.'
}
$taskCompose = @('compose', '-f', (Join-Path $PSScriptRoot 'deploy\compose\compose.yaml'))
& docker compose version
if ($LASTEXITCODE -ne 0) { throw 'Docker Compose v2 is required.' }
function Invoke-BotCompose {
    param([string[]]$Arguments)
    & docker @taskCompose @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Docker command failed. Review the output above.' }
}
function Show-BotSetup {
    $taskOutput = @(& docker @taskCompose run --rm --no-deps wizard access)
    if ($LASTEXITCODE -ne 0) { throw 'Setup link unavailable. Run Start-Bot.cmd first.' }
    $taskOutput | ForEach-Object { Write-Host $_ }
    $taskLink = $taskOutput | Where-Object { $_ -match '^http://127\.0\.0\.1:5310/#token=[A-Za-z0-9_-]+$' } | Select-Object -Last 1
    if ($OpenBrowser -and $taskLink) { Start-Process $taskLink }
}
switch ($Action) {
    'up' {
        $taskEngine = & docker info --format '{{.OSType}}/{{.Architecture}}'
        if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop and wait until its engine is ready.' }
        if ($taskEngine -notin @('linux/x86_64', 'linux/amd64')) {
            throw "This release requires Linux amd64 containers. Current engine: $taskEngine"
        }
        Invoke-BotCompose -Arguments @('up', '-d', '--build')
        Show-BotSetup
    }
    'down' { Invoke-BotCompose -Arguments @('down') }
    'status' { Invoke-BotCompose -Arguments @('ps') }
    'logs' { Invoke-BotCompose -Arguments @('logs', '--tail=100', 'app', 'wizard', 'napcat') }
    'url' { Show-BotSetup }
}
