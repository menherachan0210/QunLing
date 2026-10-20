$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
if (!(Test-Path -LiteralPath (Join-Path $taskRoot 'langbot\.venv\Scripts\python.exe')) -or !(Test-Path -LiteralPath (Join-Path $taskRoot 'napcat\Open-NapCat.ps1'))) {
    & (Join-Path $PSScriptRoot 'Public-Bot.ps1') -Action url -OpenBrowser
    return
}
Start-Process 'http://127.0.0.1:5300/'
& (Join-Path $taskRoot 'napcat\Open-NapCat.ps1')
