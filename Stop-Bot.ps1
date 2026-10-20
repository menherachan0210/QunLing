$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$stopFailures = @()
try { & (Join-Path $taskRoot 'napcat\Stop-NapCat.ps1') } catch { $stopFailures += $_.Exception.Message }
try { & (Join-Path $taskRoot 'Stop-LangBot.ps1') } catch { $stopFailures += $_.Exception.Message }
if ($stopFailures.Count) { throw ($stopFailures -join [Environment]::NewLine) }
Write-Host 'Both managed services have stopped.'
