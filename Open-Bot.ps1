$ErrorActionPreference = 'Stop'
Start-Process 'http://127.0.0.1:5300/'
& (Join-Path $PSScriptRoot 'napcat\Open-NapCat.ps1')
