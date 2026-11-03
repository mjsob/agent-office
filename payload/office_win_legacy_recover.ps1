param([switch]$CheckOnly)
. "$PSScriptRoot\common.ps1"
$target=Read-CompactJson 'target.json'
if(-not $target -or -not (Test-Path -LiteralPath $target.path) -or [IO.Path]::GetExtension($target.path) -ne '.vhdx'){throw '복구할 디스크 정보를 찾지 못했어요.'}
$RequestId=$target.request_id
if($CheckOnly){[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);@{target=$target.path;distro=$target.distro;admin=(Test-CompactAdmin);running=((Test-CompactLock 'active.lock') -or (Test-CompactLock 'launch.lock'))}|ConvertTo-Json -Compress;exit 0}
if(-not (Test-CompactAdmin)){
    try{Start-Process -FilePath powershell.exe -Verb RunAs -WindowStyle Normal -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "{0}\recover.ps1"' -f $PSScriptRoot)}catch{Write-Host '관리자 권한을 받지 못했어요. 디스크는 건드리지 않았어요.';Read-Host '닫으려면 Enter를 누르세요'}
    exit
}
try{
    if((Test-CompactLock 'active.lock') -or (Test-CompactLock 'launch.lock')){throw '줄이기 실행창에서 먼저 중단하세요. 처리 중에는 복구하지 않아요.'}
    if(Get-Process diskpart -ErrorAction SilentlyContinue){throw 'DiskPart가 실행 중이에요. 그 작업이 끝난 뒤 다시 시도하세요.'}
    $stdout="$PSScriptRoot\running-distros.$PID.txt";$stderr="$PSScriptRoot\running-distros.$PID.err"
    try{
        $probe=Start-Process -FilePath "$env:WINDIR\System32\wsl.exe" -ArgumentList '--list --running --quiet' -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -WindowStyle Hidden
        if(-not $probe.WaitForExit(15000)){$probe.Kill();throw 'WSL 상태를 확인하지 못했어요. 디스크는 건드리지 않았어요.'}
        if($probe.ExitCode -ne 0){throw 'WSL 상태를 확인하지 못했어요. 디스크는 건드리지 않았어요.'}
        $names=(Get-Content -LiteralPath $stdout -Raw -Encoding Unicode)-replace "`0",''
        if(@($names -split '[\r\n]+'|ForEach-Object{$_.Trim()}) -contains $target.distro){throw '이 배포판이 켜져 있어요. 켜진 WSL 디스크는 분리하지 않아요.'}
    }finally{if($probe){$probe.Dispose()};foreach($p in @($stdout,$stderr)){if(Test-Path -LiteralPath $p){[IO.File]::Delete($p)}}}
    $image=Get-DiskImage -ImagePath $target.path -ErrorAction Stop
    $wasAttached=[bool]$image.Attached
    if($wasAttached){
        Write-Host '이전 줄이기가 남긴 디스크 연결을 해제해요. 파일을 지우거나 줄이지 않아요.' -ForegroundColor Yellow
        Dismount-DiskImage -ImagePath $target.path -ErrorAction Stop|Out-Null
        if((Get-DiskImage -ImagePath $target.path -ErrorAction Stop).Attached){throw '디스크 연결이 아직 남아 있어요.'}
    }
    $message=if($wasAttached){'남은 디스크 연결을 해제했어요. WSL을 다시 켜세요.'}else{'남은 디스크 연결이 없어요. WSL을 다시 켜세요.'}
    Write-CompactStatus 'cancelled' $message @{phase='recovered';wsl_ready=$true;recovery_required=$false;can_cancel=$false}
    Write-Host $message -ForegroundColor Green
}catch{
    Write-Host $_.Exception.Message -ForegroundColor Red
}finally{Read-Host '닫으려면 Enter를 누르세요'}
