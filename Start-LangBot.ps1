$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskApp = Join-Path $taskRoot 'langbot'
$taskPython = Join-Path $taskApp '.venv\Scripts\python.exe'
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

    if (Test-Path -LiteralPath $taskPidFile) {
        $taskSaved = Get-Content -LiteralPath $taskPidFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $taskExisting = Get-Process -Id $taskSaved.pid -ErrorAction SilentlyContinue
        if ($taskExisting -and $taskExisting.StartTime.ToUniversalTime().Ticks -eq ([datetime]$taskSaved.started).ToUniversalTime().Ticks -and $taskExisting.Path -eq $taskSaved.executable) {
            Write-Output 'LangBot is already running: http://127.0.0.1:5300'
            return
        }
    }
    if (Get-NetTCPConnection -State Listen -LocalPort 5300 -ErrorAction SilentlyContinue) {
        throw 'Port 5300 is occupied by another process. It was not stopped.'
    }
    if (-not (Test-Path -LiteralPath $taskPython)) { throw 'The LangBot Python environment is missing.' }

    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:ANONYMIZED_TELEMETRY = 'False'
    $env:PATH = (Join-Path $taskApp '.venv\Scripts') + ';' + (Join-Path $taskRoot '.tools\Scripts') + ';' + $env:PATH
    $taskProcess = Start-Process -FilePath $taskPython -ArgumentList @('-u', 'main.py') -WorkingDirectory $taskApp -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskState 'langbot.stdout.log') -RedirectStandardError (Join-Path $taskState 'langbot.stderr.log')
    @{ pid = $taskProcess.Id; started = $taskProcess.StartTime.ToUniversalTime().ToString('o'); executable = $taskPython } | ConvertTo-Json | Set-Content -LiteralPath $taskPidFile -Encoding UTF8
    Write-Output ('LangBot started, PID ' + $taskProcess.Id + '. WebUI: http://127.0.0.1:5300')
} finally {
    if ($null -ne $taskLock) { $taskLock.Dispose() }
}
