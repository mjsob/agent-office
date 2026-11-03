param([Parameter(Mandatory=$true)][string]$RequestId, [ValidateRange(1,28800)][int]$WaitSeconds=28800)
. "$PSScriptRoot\common.ps1"
function Set-ReopenStatus([string]$State,[string]$Message) {
    $current=Read-CompactJson 'restore-office.json'
    if($current.request_id -cne $RequestId){return}
    $data=@{request_id=$RequestId;status=$State;message=$Message;updated_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()}
    $target="$PSScriptRoot\reopen-status.json";$temp="$target.$PID.tmp"
    [IO.File]::WriteAllText($temp,($data|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temp -Destination $target -Force
}
$config=Read-CompactJson 'restore-office.json'
if($RequestId -notmatch '^[A-Za-z0-9_-]{1,80}$' -or $config.request_id -cne $RequestId){exit 4}
if($config.available -ne $true){Set-ReopenStatus 'unavailable' 'Office 실행 정보를 찾지 못했어요. WSL에서 직접 office를 실행하세요.';exit 0}
foreach($value in @($config.distro,$config.linux_user,$config.script_linux)){if($value -isnot [string] -or $value -match '["\r\n]'){throw 'Office 복귀 정보가 올바르지 않아요.'}}
$started=Get-Date
# 지금 열려 있는 Office 전용 콘솔을 기억해 둬요. 새 콘솔이 뜬 뒤에만 이것을 닫아요.
$oldConsoles=@(Get-OfficeConsoleSnapshot $config)
try {
    Set-ReopenStatus 'waiting' '디스크 줄이기가 끝나면 Office를 새 콘솔에서 열어요.'
    while(((Get-Date)-$started).TotalSeconds -lt $WaitSeconds){
        if((Read-CompactJson 'restore-office.json').request_id -cne $RequestId){exit 0}
        $record=Read-CompactJson 'status.json'
        if($record.request_id -eq $RequestId){
            if($record.status -in @('cancelled','failed')){Set-ReopenStatus 'skipped' '줄이기가 끝나지 않아 Office 자동 열기를 생략했어요.';exit 0}
            if($record.status -eq 'completed' -and $record.wsl_ready -eq $true -and -not (Test-CompactLock 'active.lock')){break}
        }
        Start-Sleep -Seconds 1
    }
    $record=Read-CompactJson 'status.json'
    if($record.request_id -cne $RequestId -or $record.status -ne 'completed' -or $record.wsl_ready -ne $true -or (Test-CompactLock 'active.lock')){throw '완료와 디스크 사용 해제를 확인하지 못했어요.'}
    if((Read-CompactJson 'restore-office.json').request_id -cne $RequestId){exit 0}
    if(Test-OfficeReady $config.server_windows){Set-ReopenStatus 'opened' 'Office가 이미 열려 있어요. 기존 창을 사용하세요.';exit 0}
    Set-ReopenStatus 'launching' 'Office를 새 콘솔에서 열고 있어요.'
    # No redirection, hidden window, or background Linux server: Office owns this visible console.
    function Console-Argument([string]$Value){if($Value -match '\s'){return '"'+$Value+'"'};return $Value}
    $arguments='-d '+(Console-Argument $config.distro)+' -u '+(Console-Argument $config.linux_user)+' --exec /bin/bash '+(Console-Argument $config.script_linux)
    $console=Start-OfficeConsole $arguments
    for($i=0;$i -lt 45;$i++){
        if(Test-OfficeReady $config.server_windows){
            $null=Close-OfficeConsoles $oldConsoles (Read-CompactJson 'restore-office.json')
            Set-ReopenStatus 'opened' 'Office 창을 열었어요. 새 콘솔은 계속 켜 두세요.';exit 0
        }
        Start-Sleep -Seconds 2
    }
    throw 'Office 창을 확인하지 못했어요. 서버는 실행 중일 수 있으니 열린 콘솔의 안내를 확인하세요.'
}catch{
    Set-ReopenStatus 'failed' $_.Exception.Message
    exit 4
}
