$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
if (!(Test-Path -LiteralPath (Join-Path $taskRoot 'langbot\.venv\Scripts\python.exe')) -or !(Test-Path -LiteralPath (Join-Path $taskRoot 'napcat\Start-NapCat.ps1'))) {
    & (Join-Path $PSScriptRoot 'Public-Bot.ps1') -Action up -OpenBrowser
    return
}
& (Join-Path $PSScriptRoot 'Start-LangBot.ps1')
$langBotReady = $false
$taskReadyDeadline = [DateTime]::UtcNow.AddSeconds(180)
while ([DateTime]::UtcNow -lt $taskReadyDeadline) {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:5300/' -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) { $langBotReady = $true; break }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
if (!$langBotReady) { throw 'LangBot did not become ready within 180 seconds. Check .local\langbot.stderr.log.' }
& (Join-Path $taskRoot 'napcat\Start-NapCat.ps1')
Write-Host ''
Write-Host 'Both services are running in the background.'
Write-Host 'LangBot: http://127.0.0.1:5300/'
Write-Host 'NapCat:  http://127.0.0.1:6099/webui/'
Write-Host 'Use Open-Bot.cmd to open both management pages.'
