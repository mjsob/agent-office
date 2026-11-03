param(
    [Parameter(Mandatory=$true)][string]$Vhdx,
    [Parameter(Mandatory=$true)][string]$Distro,
    [string]$RequestId = 'manual',
    [ValidateSet('none','success','cancel','failure','stall','pending100','unknown')][string]$TestMode = 'none',
    [int]$TestSeconds = 1
)
. "$PSScriptRoot\common.ps1"
$testing = $TestMode -ne 'none'
if ($testing -and -not ([IO.Path]::GetFullPath($PSScriptRoot).StartsWith([IO.Path]::GetFullPath($env:TEMP), [StringComparison]::OrdinalIgnoreCase))) { throw '시험은 Windows 임시 폴더에서만 실행해요.' }
if (-not $testing -and -not (Test-CompactAdmin)) { throw '관리자 권한이 필요해요. WSL은 끄지 않았어요.' }
function Test-LaunchLease {
    $lease=Read-CompactJson 'launch-request.json'
    return $lease -and $lease.request_id -ceq $RequestId -and $lease.revoked -ne $true -and $lease.expires_at -gt [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}
if (-not $testing -and -not (Test-LaunchLease)) { throw '유효한 시작 요청이 없어요. WSL은 끄지 않았어요.' }

$lock = $null
try { $lock = [IO.File]::Open("$PSScriptRoot\active.lock", 'OpenOrCreate', 'ReadWrite', 'None') } catch { exit 3 }
try {
    Add-Type -Path "$PSScriptRoot\CompactOperation.cs"
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [Windows.Forms.Application]::EnableVisualStyles()
    $script:operation = $null; $script:shutdownProcess = $null; $script:phase = 'countdown'; $script:terminal = $false
    $script:cancelRequested = $false; $script:started = Get-Date; $script:changed = Get-Date; $script:lastPercent = -1
    $script:finalizingStarted = $null; $script:shutdownStarted = $false; $script:errorText = ''; $script:tickBusy = $false
    $countdown = if ($testing) { [Math]::Max(0,$TestSeconds) } else { 60 }
    $script:shutdownAt = (Get-Date).AddSeconds($countdown)
    $script:before = if ($testing) { 10.0 } else { (Get-Item -LiteralPath $Vhdx).Length / 1GB }
    $drive = if (-not $testing) { [IO.DriveInfo]::new([IO.Path]::GetPathRoot($Vhdx)) } else { $null }
    $script:freeBefore = if ($testing) { 20.0 } else { $drive.AvailableFreeSpace / 1GB }
    $form = [Windows.Forms.Form]::new(); $form.Text = if ($testing) { 'WSL 디스크 줄이기 (시험)' } else { 'WSL 디스크 줄이기' }
    $form.ClientSize = [Drawing.Size]::new(560,330); $form.StartPosition='CenterScreen'; $form.FormBorderStyle='FixedDialog'; $form.MaximizeBox=$false
    $form.Font=[Drawing.Font]::new('Malgun Gothic',10); $form.BackColor=[Drawing.Color]::White
    $heading=[Windows.Forms.Label]::new();$heading.Location=[Drawing.Point]::new(24,22);$heading.Size=[Drawing.Size]::new(510,32);$heading.Font=[Drawing.Font]::new('Malgun Gothic',14,[Drawing.FontStyle]::Bold)
    $detail=[Windows.Forms.Label]::new();$detail.Location=[Drawing.Point]::new(24,66);$detail.Size=[Drawing.Size]::new(510,65)
    $bar=[Windows.Forms.ProgressBar]::new();$bar.Location=[Drawing.Point]::new(24,145);$bar.Size=[Drawing.Size]::new(420,20);$bar.Style='Continuous'
    $progressLabel=[Windows.Forms.Label]::new();$progressLabel.Location=[Drawing.Point]::new(450,140);$progressLabel.Size=[Drawing.Size]::new(84,30);$progressLabel.TextAlign='MiddleCenter'
    $timing=[Windows.Forms.Label]::new();$timing.Location=[Drawing.Point]::new(24,178);$timing.Size=[Drawing.Size]::new(510,35)
    $hint=[Windows.Forms.Label]::new();$hint.Location=[Drawing.Point]::new(24,215);$hint.Size=[Drawing.Size]::new(510,45);$hint.ForeColor=[Drawing.Color]::DimGray
    $cancel=[Windows.Forms.Button]::new();$cancel.Text='중단하기';$cancel.Location=[Drawing.Point]::new(410,275);$cancel.Size=[Drawing.Size]::new(124,35)
    $startNow=[Windows.Forms.Button]::new();$startNow.Text='바로 실행';$startNow.Location=[Drawing.Point]::new(276,275);$startNow.Size=[Drawing.Size]::new(124,35)
    $form.Controls.AddRange(@($heading,$detail,$bar,$progressLabel,$timing,$hint,$startNow,$cancel))
    function Publish([string]$State,[string]$Message,[hashtable]$Extra=@{}) {
        $fields=@{phase=$script:phase; elapsed_seconds=[int]((Get-Date)-$script:started).TotalSeconds; can_cancel=(-not $script:terminal -and -not $script:cancelRequested); wsl_ready=(-not $script:shutdownStarted); recovery_required=$false; test_mode=$testing}
        foreach($key in $Extra.Keys){$fields[$key]=$Extra[$key]}
        Write-CompactStatus $State $Message $fields
    }
    function Finish([string]$State,[string]$Message) {
        if($script:operation){$script:operation.Dispose();$script:operation=$null}
        $script:terminal=$true;$script:phase='finished';$timer.Stop();$progressLabel.Text=if($State -eq 'completed'){'완료'}elseif($State -eq 'cancelled'){'중단됨'}else{'오류'}
        $after=if($testing){if($State -eq 'completed'){8.0}else{10.0}}else{(Get-Item -LiteralPath $Vhdx).Length/1GB}
        $freeAfter=if($testing){22.0}else{$drive.AvailableFreeSpace/1GB}
        $info=@{wsl_ready=$true;can_cancel=$false;before_gb=[Math]::Round($script:before,2);after_gb=[Math]::Round($after,2);reclaimed_gb=[Math]::Round([Math]::Max([double]0,[double]($script:before-$after)),2);c_free_before_gb=[Math]::Round($script:freeBefore,2);c_free_after_gb=[Math]::Round($freeAfter,2)}
        if($State -eq 'completed'){$info.percent=100;$bar.Style='Continuous';$bar.Value=100;$heading.Text='디스크 줄이기가 끝났어요.'}
        elseif($State -eq 'cancelled'){$heading.Text='중단하고 디스크 사용을 해제했어요.';$bar.Style='Continuous'}
        else{$heading.Text='디스크를 줄이지 못했어요.';$bar.Style='Continuous'}
        $detail.Text=$Message+"`r`n"+('디스크 파일 {0:N2}GiB → {1:N2}GiB' -f $script:before,$after)
        $reopen=Read-CompactJson 'reopen-status.json'
        $auto=$State -eq 'completed' -and $reopen.request_id -eq $RequestId -and $reopen.status -in @('waiting','launching','opened')
        $hint.Text=if($auto){'Office를 새 콘솔에서 자동으로 열어요. 이 창은 닫아도 돼요.'}else{'WSL 터미널을 열고 Office를 다시 켜세요. 이 창을 닫아도 돼요.'}
        $timing.Text=('걸린 시간: {0:N0}초' -f ((Get-Date)-$script:started).TotalSeconds)
        $cancel.Text='닫기';$cancel.Enabled=$true;$startNow.Visible=$false
        Publish $State $Message $info
        [IO.File]::WriteAllText("$PSScriptRoot\result.json",((Read-CompactJson 'status.json')|ConvertTo-Json -Depth 5 -Compress),[Text.UTF8Encoding]::new($false))
        # The result window can remain open; it no longer owns the operation lock.
        $lock.Dispose();$script:lock=$null
        if($auto -and -not $testing){$reopenTimer.Start()}
        if($testing){$form.Close()}
    }
    function Request-Cancel {
        if($script:terminal){$form.Close();return}
        if($script:cancelRequested){return}
        $script:cancelRequested=$true;$cancel.Enabled=$false;$startNow.Enabled=$false;$heading.Text='안전하게 중단하고 있어요.';$progressLabel.Text='중단 중'
        $hint.Text='중단 완료를 확인할 때까지 창을 유지해요. WSL을 아직 켜지 마세요.'
        if($script:operation){$script:operation.Cancel()}
        Publish 'cancelling' '중단을 요청했어요. 디스크 사용이 끝날 때까지 기다려요.' @{can_cancel=$false}
    }
    $startNow.Add_Click({if($script:phase -eq 'countdown' -and -not $script:cancelRequested){$script:shutdownAt=Get-Date;$startNow.Enabled=$false}})
    $cancel.Add_Click({Request-Cancel})
    $form.Add_FormClosing({param($sender,$e) if(-not $script:terminal){$e.Cancel=$true;Request-Cancel}})
    $reopenTimer=[Windows.Forms.Timer]::new();$reopenTimer.Interval=1000
    $reopenTimer.Add_Tick({$r=Read-CompactJson 'reopen-status.json';if($r.request_id -eq $RequestId){if($r.status -eq 'opened'){$hint.Text='Office 창을 열었어요. 새 콘솔은 계속 켜 두세요.';$reopenTimer.Stop()}elseif($r.status -in @('failed','unavailable')){$hint.Text='Office를 자동으로 열지 못했어요. WSL 터미널에서 office를 실행하세요.';$reopenTimer.Stop()}}})
    $timer=[Windows.Forms.Timer]::new();$timer.Interval=500
    $timer.Add_Tick({
        if($script:tickBusy){return};$script:tickBusy=$true
        try {
            $request=Read-CompactJson 'cancel.json'
            if($request.request_id -eq $RequestId){Request-Cancel}
            $elapsed=[int]((Get-Date)-$script:started).TotalSeconds
            $timing.Text="진행 시간: ${elapsed}초"
            if($script:phase -eq 'countdown') {
                if($script:cancelRequested){Finish 'cancelled' 'WSL을 끄기 전에 취소했어요.';return}
                $left=[Math]::Max(0,[int][Math]::Ceiling(($script:shutdownAt-(Get-Date)).TotalSeconds))
                $heading.Text="${left}초 뒤에 WSL을 꺼요.";$progressLabel.Text='시작 대기 중'
                $detail.Text='관리자 권한을 확인했어요. Office를 포함한 WSL 작업이 모두 멈춰요.'
                $hint.Text='지금 중단하면 WSL은 켜진 상태로 유지돼요.'
                $bar.Value=if($countdown -eq 0){100}else{[Math]::Min(100,[int](100*($countdown-$left)/$countdown))}
                Publish 'countdown' "${left}초 뒤에 WSL을 꺼요. 중단하려면 [중단하기]를 누르세요." @{shutdown_at=([DateTimeOffset]$script:shutdownAt).ToUnixTimeSeconds();remaining_seconds=$left}
                if($left -gt 0){return}
                if(-not $testing -and -not (Test-LaunchLease)){Finish 'cancelled' '시작 요청이 만료됐어요. WSL은 끄지 않았어요.';return}
                $startNow.Visible=$false;$bar.Value=0; $script:phase='stopping';$script:shutdownStarted=$true;$script:changed=Get-Date
                $heading.Text='WSL이 종료되기를 기다려요.';$hint.Text='이 Windows 창에서 작업 상태를 계속 보여줘요.';$bar.Style='Marquee';$progressLabel.Text='진행 중'
                if(-not $testing){$script:shutdownProcess=Start-Process -FilePath "$env:WINDIR\System32\wsl.exe" -ArgumentList '--shutdown' -WindowStyle Hidden -PassThru}
                Publish 'stopping' 'WSL이 종료되기를 기다리고 있어요.';return
            }
            if($script:phase -eq 'stopping') {
                if($script:shutdownProcess -and -not $script:shutdownProcess.HasExited){
                    if(((Get-Date)-$script:changed).TotalSeconds -gt 120){$script:shutdownProcess.Kill();throw 'WSL 종료가 2분 안에 끝나지 않았어요. 디스크 축소는 시작하지 않았어요.'}
                    Publish $(if($script:cancelRequested){'cancelling'}else{'stopping'}) 'WSL 종료가 끝나기를 기다리고 있어요.';return
                }
                if($script:shutdownProcess -and $script:shutdownProcess.ExitCode -ne 0){throw 'WSL을 종료하지 못했어요. 디스크 축소는 시작하지 않았어요.'}
                if($script:cancelRequested){Finish 'cancelled' 'WSL 종료 뒤 축소를 시작하기 전에 취소했어요.';return}
                $script:phase='running';$script:changed=Get-Date;$script:operationStarted=Get-Date
                if(-not $testing){$script:operation=[CompactOperation]::new($Vhdx)}
                $heading.Text='디스크를 줄이고 있어요.';$hint.Text='중단할 수 있어요. 중단이 끝난 뒤 WSL을 다시 켜세요.';return
            }
            if($script:phase -in @('running','finalizing')) {
                if($testing) {
                    if($TestMode -eq 'cancel' -and -not $script:cancelRequested){Request-Cancel}
                    if($TestMode -eq 'unknown'){$percent=-1}elseif($TestMode -eq 'pending100'){$percent=100}elseif($TestMode -eq 'stall'){$percent=45}else{$percent=[Math]::Min(100,[int](((Get-Date)-$script:operationStarted).TotalSeconds*35))}
                    $complete=($TestMode -eq 'unknown' -and ((Get-Date)-$script:operationStarted).TotalSeconds -ge 5) -or ($percent -ge 100 -and ($TestMode -ne 'pending100' -or ((Get-Date)-$script:operationStarted).TotalSeconds -ge 5)) -or $script:cancelRequested
                    $code=if($script:cancelRequested){995}elseif($TestMode -eq 'failure'){5}else{0}
                } else {
                    $script:operation.Poll();$percent=$script:operation.Percent;$complete=$script:operation.Complete;$code=$script:operation.ErrorCode
                }
                if($complete){
                    if($code -eq 0){Finish 'completed' '줄이기를 마쳤어요. 실제 확보 용량은 아래 결과를 확인하세요.'}
                    elseif($code -eq 995){Finish 'cancelled' '중단을 마쳤어요. 디스크 사용을 해제했어요.'}
                    else{Finish 'failed' ("축소가 끝나지 않았어요. Windows 오류 코드: $code. 디스크 사용을 해제했어요.")};return
                }
                if($percent -ne $script:lastPercent){$script:lastPercent=$percent;$script:changed=Get-Date}
                $stalled=[int]((Get-Date)-$script:changed).TotalSeconds
                $fields=@{stalled_seconds=$stalled}
                if($percent -ge 100){
                    $script:phase='finalizing'
                    if(-not $script:finalizingStarted){$script:finalizingStarted=Get-Date}
                    $heading.Text=if($script:cancelRequested){'안전하게 중단하고 있어요.'}else{'디스크 줄이기 진행 중이에요.'}
                    $bar.Value=0;$bar.Style='Marquee';$progressLabel.Text=if($script:cancelRequested){'중단 중'}else{'진행 중'}
                    $detail.Text=''
                }else{
                    $script:phase='running';$script:finalizingStarted=$null
                    if(-not $script:cancelRequested){$heading.Text='디스크를 줄이고 있어요.'}
                    if($percent -ge 0){$bar.Style='Continuous';$bar.Value=$percent;$progressLabel.Text="${percent}%";$detail.Text='';$fields.percent=$percent}else{$bar.Value=0;$bar.Style='Marquee';$progressLabel.Text='진행 중';if(-not $script:cancelRequested){$heading.Text='디스크 줄이기 진행 중이에요.'};$detail.Text=''}
                    if($percent -ge 0 -and $stalled -ge 30){$detail.Text+="`r`n${stalled}초 동안 같은 진행률이에요. 처리 중에도 숫자가 멈출 수 있어요."}
                }
                if($script:cancelRequested){$progressLabel.Text='중단 중'}
                Publish $(if($script:cancelRequested){'cancelling'}else{'running'}) $detail.Text $fields
            }
        } catch {
            $script:errorText=$_.Exception.Message
            if($script:operation -and -not $script:operation.Complete){
                try{Request-Cancel}catch{}
                $heading.Text='작업 상태를 확인하고 있어요.';$detail.Text=$script:errorText
                Publish 'uncertain' ('상태 확인에 실패했어요. 이 창에서 중단 결과를 기다리세요. '+$script:errorText) @{can_cancel=$false}
            }else{Finish 'failed' $script:errorText}
        } finally {$script:tickBusy=$false}
    })
    $form.Add_Shown({
        # Publish before the launcher releases its lock, then keep all progress in Windows.
        Publish 'countdown' '관리자 권한을 확인했어요. Windows 진행창에서 남은 시간을 확인하세요.' @{shutdown_at=([DateTimeOffset]$script:shutdownAt).ToUnixTimeSeconds()}
        $form.Activate();$timer.Start()
    })
    [void]$form.ShowDialog()
} catch {
    Write-CompactStatus 'failed' ('실행창을 준비하지 못했어요. '+$_.Exception.Message) @{phase='initialization';wsl_ready=$true;can_cancel=$false;recovery_required=$false;test_mode=$testing}
    if(-not $testing){try{[Windows.Forms.MessageBox]::Show($_.Exception.Message,'디스크 줄이기 오류')}catch{}}
    exit 4
} finally {
    if($reopenTimer){$reopenTimer.Dispose()}
    if($lock){$lock.Dispose()}
    if($script:shutdownProcess){$script:shutdownProcess.Dispose()}
}
