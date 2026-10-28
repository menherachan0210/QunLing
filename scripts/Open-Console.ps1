param(
    [string]$ProfileDirectory = (Join-Path $env:LOCALAPPDATA 'QunLing\Console'),
    [switch]$Watch,
    [switch]$NoBrowser
)
# User profile contains a restricted SSH key, pinned host key and DPAPI-encrypted
# management credential. It is never part of a release or a source checkout.
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    $env:PSModulePath = "$PSHOME\Modules;$env:SystemRoot\System32\WindowsPowerShell\v1.0\Modules;$env:ProgramFiles\WindowsPowerShell\Modules"
}
$profilePath = [IO.Path]::GetFullPath($ProfileDirectory)
$configPath = Join-Path $profilePath 'ssh_config'
$statePath = Join-Path $profilePath 'connection.json'
$entryPath = $PSCommandPath
$sshPath = (Get-Command ssh.exe -ErrorAction Stop).Source
$port = 5310
$baseUrl = "http://127.0.0.1:$port"

function Read-Connection {
    try { return Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } catch { return $null }
}
function Set-Connection([string]$Stage, [int]$SshPid = 0) {
    $value = @{watch_pid=$PID; ssh_pid=$SshPid; stage=$Stage; checked_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()}
    $temporary = Join-Path $profilePath "connection-$PID.tmp"
    [IO.File]::WriteAllText($temporary, ($value | ConvertTo-Json -Compress))
    Move-Item -LiteralPath $temporary -Destination $statePath -Force
}
function Is-ManagedProcess([int]$ProcessId, [string]$Marker) {
    if ($ProcessId -le 0) { return $false }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    return $process -and $process.CommandLine -and $process.CommandLine.Contains($Marker)
}
function Test-ConnectionReady {
    $state = Read-Connection
    if (-not $state -or -not (Is-ManagedProcess $state.ssh_pid $configPath)) { return $false }
    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if (-not ($listener | Where-Object { $_.LocalAddress -eq '127.0.0.1' -and $_.OwningProcess -eq $state.ssh_pid })) { return $false }
    try {
        $health = Invoke-RestMethod -Uri "$baseUrl/healthz" -TimeoutSec 2 -Proxy $null
        return $health.status -eq 'ok'
    } catch { return $false }
}

try {
    if (-not (Test-Path -LiteralPath $configPath)) { throw '这台电脑尚未安装群灵管理入口。请先完成一次连接配置。' }
    if ($Watch) {
        $digest = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($profilePath))).Replace('-','')
        $created = $false
        $mutex = New-Object Threading.Mutex($true, "Local\QunLingConsole-$digest", [ref]$created)
        if (-not $created) { $mutex.Dispose(); exit 0 }
        $child = $null
        try {
            while ($true) {
                $occupied = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
                if ($occupied) { Set-Connection 'port_in_use'; Start-Sleep -Seconds 5; continue }
                $logPath = Join-Path $profilePath 'connection.log'
                if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 65536) {
                    [IO.File]::WriteAllText($logPath, '')
                }
                $arguments = "-F `"$configPath`" -N -L 127.0.0.1:${port}:127.0.0.1:5310 -E `"$logPath`" qunling-console"
                $child = Start-Process -FilePath $sshPath -ArgumentList $arguments -PassThru -WindowStyle Hidden
                Set-Connection 'connecting' $child.Id
                while (-not $child.WaitForExit(5000)) {
                    Set-Connection 'connected' $child.Id
                }
                Set-Connection 'reconnecting'
                Start-Sleep -Seconds 3
            }
        } finally {
            if ($child -and -not $child.HasExited) { $child.Kill() }
            $mutex.ReleaseMutex(); $mutex.Dispose()
        }
        exit 0
    }
    $state = Read-Connection
    if (-not $state -or -not (Is-ManagedProcess $state.watch_pid $entryPath)) {
        $arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$entryPath`" -ProfileDirectory `"$profilePath`" -Watch"
        Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList $arguments -WindowStyle Hidden | Out-Null
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(35)
    while (-not (Test-ConnectionReady)) {
        if ([DateTime]::UtcNow -ge $deadline) {
            $state = Read-Connection
            if ($state.stage -eq 'port_in_use') { throw '管理端口已被其他程序占用。关闭旧的管理连接后，再打开“群灵管理”。' }
            throw '暂时无法连接服务器，后台会继续重连。请检查网络后再次打开“群灵管理”。'
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $NoBrowser) {
        $secretFile = Join-Path $profilePath 'login.dpapi'
        $url = $baseUrl
        if (Test-Path -LiteralPath $secretFile) {
            $secret = Get-Content -LiteralPath $secretFile -Raw | ConvertTo-SecureString
            $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
            try { $url += '/#token=' + [Uri]::EscapeDataString([Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)) }
            finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer); $secret.Dispose() }
        }
        Start-Process $url
        $url = $null
    }
} catch {
    if ($Watch -or $NoBrowser) { throw }
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, '群灵管理') | Out-Null
    exit 1
}
