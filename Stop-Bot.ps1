$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
if (!(Test-Path -LiteralPath (Join-Path $taskRoot 'langbot\.venv\Scripts\python.exe')) -or !(Test-Path -LiteralPath (Join-Path $taskRoot 'napcat\Stop-NapCat.ps1'))) {
    & (Join-Path $taskRoot 'Public-Bot.ps1') -Action down
    return
}
$stopFailures = @()
try { & (Join-Path $taskRoot 'napcat\Stop-NapCat.ps1') } catch { $stopFailures += $_.Exception.Message }
try { & (Join-Path $taskRoot 'Stop-LangBot.ps1') } catch { $stopFailures += $_.Exception.Message }
if ($stopFailures.Count) { throw ($stopFailures -join [Environment]::NewLine) }
Write-Host 'Both managed services have stopped.'
