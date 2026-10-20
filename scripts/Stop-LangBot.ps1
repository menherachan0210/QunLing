$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskState = Join-Path $taskRoot '.local'
$taskPidFile = Join-Path $taskState 'langbot-process.json'
New-Item -ItemType Directory -Path $taskState -Force | Out-Null
$taskLockFile = Join-Path $taskState 'langbot-operation.lock'
$taskLock = $null
try {
    for ($taskLockAttempt = 0; $taskLockAttempt -lt 100; $taskLockAttempt++) {
        try {
            $taskLock = [System.IO.File]::Open($taskLockFile, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
            break
        } catch [System.IO.IOException] {
            if ($taskLockAttempt -eq 99) { throw ('Could not acquire the LangBot start/stop lock: ' + $_.Exception.Message) }
            Start-Sleep -Milliseconds 100
        }
    }

    if (-not (Test-Path -LiteralPath $taskPidFile)) { Write-Output 'No managed LangBot process.'; return }
    $taskSaved = Get-Content -LiteralPath $taskPidFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskExisting = Get-Process -Id $taskSaved.pid -ErrorAction SilentlyContinue
    if ($taskExisting) {
        if ($taskExisting.StartTime.ToUniversalTime().Ticks -ne ([datetime]$taskSaved.started).ToUniversalTime().Ticks -or $taskExisting.Path -ne $taskSaved.executable) {
            throw 'Process ownership check failed; no process was stopped.'
        }
        & taskkill.exe /PID $taskSaved.pid /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Failed to stop the managed process tree.' }
    }
    Remove-Item -LiteralPath $taskPidFile -Force
    Write-Output 'LangBot stopped.'
} finally {
    if ($null -ne $taskLock) { $taskLock.Dispose() }
}
