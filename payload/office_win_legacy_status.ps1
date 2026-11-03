param([switch]$Cancel, [string]$RequestId = '')
. "$PSScriptRoot\common.ps1"
$record=Read-CompactJson 'status.json'
$running=(Test-CompactLock 'active.lock') -or (Test-CompactLock 'launch.lock')
if($Cancel){
    if(-not $running -or -not $record -or $record.request_id -cne $RequestId -or $record.can_cancel -ne $true){throw '중단할 수 있는 작업이 없거나 실행 정보가 달라요.'}
    $target=Join-Path $PSScriptRoot 'cancel.json';$temp="$target.$PID.tmp"
    [IO.File]::WriteAllText($temp,(@{request_id=$RequestId;requested_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()}|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temp -Destination $target -Force
}
$data=@{running=$running}
if($record){$data.status=$record.status;$data.request_id=$record.request_id;$data.updated_at=$record.updated_at}
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$data|ConvertTo-Json -Compress
