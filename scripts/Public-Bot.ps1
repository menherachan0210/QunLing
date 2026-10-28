param(
    [ValidateSet('up', 'rebuild', 'down', 'status', 'logs', 'url')]
    [string]$Action = 'up',
    [switch]$OpenBrowser
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Install and start Docker Desktop with Linux containers, then run Start-Bot.cmd again.'
}
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskCompose = @('compose', '-f', (Join-Path $taskRoot 'deploy\compose\compose.yaml'))
& docker compose version
if ($LASTEXITCODE -ne 0) { throw 'Docker Compose v2 is required.' }
function Invoke-BotCompose {
    param([string[]]$Arguments)
    & docker @taskCompose @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Docker command failed. Review the output above.' }
}
function Show-BotSetup {
    $taskOutput = @(& docker @taskCompose exec -T wizard python -m bootstrap.server --url)
    if ($LASTEXITCODE -ne 0) { throw 'Setup link unavailable. Run Start-Bot.cmd first.' }
    $taskOutput | ForEach-Object { Write-Host $_ }
    $taskLink = $taskOutput | Where-Object { $_ -match '^https?://[^\s/?#]+/#token=[A-Za-z0-9_-]+$' } | Select-Object -Last 1
    if ($OpenBrowser -and $taskLink) { Start-Process $taskLink }
}
function Test-BotUpdateVolume {
    # A missing volume is expected on first launch, including Windows
    # PowerShell 5.1 where redirected native stderr otherwise becomes an error.
    $ErrorActionPreference = 'Continue'
    $taskProject = if ($env:COMPOSE_PROJECT_NAME) { $env:COMPOSE_PROJECT_NAME } else { 'qq-ai-bot' }
    & docker volume inspect "${taskProject}_update-data" 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}
function Assert-BotUpdateIdle {
    if (Test-BotUpdateVolume) {
        Invoke-BotCompose -Arguments @('run', '--rm', '--no-deps', 'updater', 'guard')
    }
}
switch ($Action) {
    { $_ -in @('up', 'rebuild') } {
        $taskEngine = & docker info --format '{{.OSType}}/{{.Architecture}}'
        if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop and wait until its engine is ready.' }
        if ($taskEngine -notin @('linux/x86_64', 'linux/amd64')) {
            throw "This release requires Linux amd64 containers. Current engine: $taskEngine"
        }
        Assert-BotUpdateIdle
        $taskResumed = $false
        if ($Action -eq 'up' -and (Test-BotUpdateVolume)) {
            & docker @taskCompose run --rm --no-deps updater resume
            if ($LASTEXITCODE -eq 0) { $taskResumed = $true }
            elseif ($LASTEXITCODE -ne 3) { throw 'The active deployment could not be resumed. Check update status.' }
        }
        if (-not $taskResumed) {
            $env:QQBOT_REVISION = 'unknown'
            if ((Test-Path -LiteralPath (Join-Path $taskRoot '.git')) -and (Get-Command git -ErrorAction SilentlyContinue)) {
                $taskRevision = & git -C $taskRoot rev-parse HEAD 2>$null
                if ($LASTEXITCODE -eq 0 -and $taskRevision -match '^[0-9a-f]{40}$') {
                    $taskChanges = & git -C $taskRoot status --porcelain 2>$null
                    if ($LASTEXITCODE -eq 0 -and -not $taskChanges) { $env:QQBOT_REVISION = $taskRevision }
                }
            }
            Invoke-BotCompose -Arguments @('up', '-d', '--build')
            Invoke-BotCompose -Arguments @('run', '--rm', '--no-deps', 'updater', 'adopt')
        }
        Show-BotSetup
    }
    'down' { Assert-BotUpdateIdle; Invoke-BotCompose -Arguments @('down') }
    'status' { Invoke-BotCompose -Arguments @('ps') }
    'logs' { Invoke-BotCompose -Arguments @('logs', '--tail=100', 'app', 'wizard', 'napcat') }
    'url' { Show-BotSetup }
}
