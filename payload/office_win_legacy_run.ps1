param([string]$Vhdx, [string]$Distro, [string]$RequestId = 'manual')
. "$PSScriptRoot\common.ps1"
$launchLock = $null
if(-not $Vhdx -and -not $Distro){
    $target=Read-CompactJson 'target.json'
    if(-not $target){throw '실행 정보를 찾지 못했어요. WSL은 끄지 않았어요.'}
    $Vhdx=$target.path;$Distro=$target.distro;$RequestId=$target.request_id
}
if(-not $Vhdx -or -not $Distro){throw '대상 디스크와 배포판 정보를 확인하지 못했어요.'}

if ($RequestId -eq 'manual' -or -not $RequestId) { $RequestId = [Guid]::NewGuid().ToString('N') }
try {
    if ($RequestId -notmatch '^[A-Za-z0-9_-]{1,80}$' -or $Vhdx -match '["\r\n]' -or $Distro -match '["\r\n]') { throw '실행 정보가 올바르지 않아요.' }
    try { $launchLock = [IO.File]::Open("$PSScriptRoot\launch.lock", 'OpenOrCreate', 'ReadWrite', 'None') } catch [IO.IOException] { exit 3 }
    if (Test-CompactLock 'active.lock') { exit 3 }
    $lease=@{request_id=$RequestId;expires_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()+600;revoked=$false}
    [IO.File]::WriteAllText("$PSScriptRoot\launch-request.json",($lease|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))
    Write-CompactStatus 'awaiting_admin' '관리자 권한을 확인하고 있어요. 승인 전에는 WSL을 끄지 않아요.' @{phase='permission'; can_cancel=$false; wsl_ready=$true; recovery_required=$false}
    Write-Host '관리자 권한을 먼저 확인해요. 승인하지 않으면 WSL은 그대로 켜져 있어요.' -ForegroundColor Cyan
    $arguments = '-NoProfile -STA -ExecutionPolicy Bypass -File "{0}\compact.ps1" -Vhdx "{1}" -Distro "{2}" -RequestId "{3}"' -f $PSScriptRoot,$Vhdx,$Distro,$RequestId
    $options = @{FilePath='powershell.exe'; ArgumentList=$arguments; WindowStyle='Normal'; PassThru=$true}
    if (-not (Test-CompactAdmin)) { $options.Verb = 'RunAs' }
    $worker = Start-Process @options
    # Confirm the UI's ownership, not merely the existence of a launched process.
    for ($i=0; $i -lt 240; $i++) {
        $worker.Refresh()
        if ($worker.HasExited) { break }
        $record = Read-CompactJson 'status.json'
        if ((Test-CompactLock 'active.lock') -and $record.request_id -eq $RequestId -and $record.status -ne 'awaiting_admin') { exit 0 }
        Start-Sleep -Milliseconds 500
    }
    $record = Read-CompactJson 'status.json'
    if ($record.request_id -eq $RequestId -and $record.status -in @('cancelled','completed','failed')) { exit 0 }
    $lease.revoked=$true
    [IO.File]::WriteAllText("$PSScriptRoot\launch-request.json",($lease|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))
    throw '실행창 준비 시간이 지나 요청을 취소했어요. 뒤늦게 열린 실행창은 WSL을 끄지 않아요.'
} catch {
    $cancelled = $_.Exception.NativeErrorCode -eq 1223 -or $_.Exception.InnerException.NativeErrorCode -eq 1223
    $state = if ($cancelled) { 'cancelled' } else { 'failed' }
    Write-CompactStatus $state ('관리자 실행창을 시작하지 못했어요. WSL은 끄지 않았어요. ' + $_.Exception.Message) @{phase='permission'; can_cancel=$false; wsl_ready=$true; recovery_required=$false}
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 4
} finally { if ($launchLock) { $launchLock.Dispose() } }
