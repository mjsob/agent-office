$ErrorActionPreference = 'Stop'
function Write-CompactStatus([string]$Status, [string]$Message, [hashtable]$Extra = @{}) {
    $data = [ordered]@{ schema_version = 1; request_id = $RequestId; status = $Status; message = $Message; updated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
    foreach ($key in $Extra.Keys) { $data[$key] = $Extra[$key] }
    $target = Join-Path $PSScriptRoot 'status.json'
    $temp = "$target.$PID.tmp"
    [IO.File]::WriteAllText($temp, ($data | ConvertTo-Json -Depth 5 -Compress), [Text.UTF8Encoding]::new($false))
    # Another reader can briefly hold the file, especially across WSL's drive bridge.
    for ($i = 0; $i -lt 5; $i++) {
        try { Move-Item -LiteralPath $temp -Destination $target -Force; return } catch { if ($i -eq 4) { throw }; Start-Sleep -Milliseconds 50 }
    }
}
function Test-CompactAdmin {
    return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Test-CompactLock([string]$Name) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $Name))) { return $false }
    try { $file = [IO.File]::Open((Join-Path $PSScriptRoot $Name), 'Open', 'ReadWrite', 'None'); $file.Dispose(); return $false } catch [IO.IOException] { return $true }
}
function Read-CompactJson([string]$Name) {
    $path = Join-Path $PSScriptRoot $Name
    if (Test-Path -LiteralPath $path) { try { return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json } catch { } }
    return $null
}

function Test-OfficeReady([string]$ServerPath) {
    try {
        $server=Get-Content -LiteralPath $ServerPath -Raw -Encoding UTF8|ConvertFrom-Json
        $port=[int]$server.port
        if($port -lt 1 -or $port -gt 65535){return $false}
        $request=[Net.HttpWebRequest]::Create("http://127.0.0.1:$port/api/window")
        $request.Proxy=$null;$request.Timeout=2000
        $response=$request.GetResponse()
        try{$reader=[IO.StreamReader]::new($response.GetResponseStream());try{$data=$reader.ReadToEnd()|ConvertFrom-Json;return $data.open -eq $true}finally{$reader.Dispose()}}finally{$response.Dispose()}
    }catch{return $false}
}

function Start-OfficeConsole([string]$Arguments) {
    if(-not ('OfficeVisibleConsole' -as [type])){Add-Type -Path "$PSScriptRoot\OfficeConsole.cs"}
    return [OfficeVisibleConsole]::Start("$env:WINDIR\System32\wsl.exe",$Arguments)
}

# 이번 복귀 설정(restore-office.json)과 명확히 이어진 Office 전용 콘솔만 골라요.
# wsl.exe 명령줄에 같은 배포판(-d)이 있고, 이 설정의 복귀 스크립트 경로나 Office 바로가기 파일 경로가 그대로 들어 있어야 해요.
# 콘솔 창을 가진 conhost가 하나뿐이고 다른 프로그램이 붙어 있지 않을 때만 대상이에요(Windows Terminal 탭 등은 제외).
function Test-OfficeConsoleCommand([string]$CommandLine,$Config) {
    if([string]::IsNullOrEmpty($CommandLine) -or $null -eq $Config -or [string]::IsNullOrEmpty([string]$Config.distro)){return $false}
    if($CommandLine -notmatch ('(^|\s)-d\s+"?'+[regex]::Escape([string]$Config.distro)+'"?(\s|$)')){return $false}
    foreach($path in @($Config.script_linux,$Config.console_rc)){
        # 경로 전체가 하나의 토큰일 때만 맞아요(앞은 공백·따옴표, 뒤는 공백·따옴표·끝). '.backup'처럼 이어 붙은 이름은 제외해요.
        if(-not [string]::IsNullOrEmpty([string]$path) -and [regex]::IsMatch($CommandLine,'(^|[\s"''])'+[regex]::Escape([string]$path)+'(["'']|\s|$)')){return $true}
    }
    return $false
}
function Select-OfficeConsoles($Processes,$Config,[int]$ExcludePid=0) {
    $all=@($Processes)
    foreach($wsl in $all){
        if($wsl.Name -ine 'wsl.exe' -or [int]$wsl.ProcessId -eq $ExcludePid){continue}
        if(-not (Test-OfficeConsoleCommand ([string]$wsl.CommandLine) $Config)){continue}
        $children=@($all|Where-Object{[int]$_.ParentProcessId -eq [int]$wsl.ProcessId})
        $hosts=@($children|Where-Object{$_.Name -in @('conhost.exe','OpenConsole.exe')})
        # 실제 콘솔은 wsl.exe 안쪽에 같은 명령줄의 wsl.exe가 하나 더 있어요. 그 밖의 프로그램이 붙어 있으면 대상이 아니에요.
        $inner=@($children|Where-Object{$_.Name -ieq 'wsl.exe' -and (Test-OfficeConsoleCommand ([string]$_.CommandLine) $Config)})
        if($hosts.Count -ne 1 -or $children.Count -ne ($hosts.Count+$inner.Count)){continue}
        [pscustomobject]@{WslPid=[int]$wsl.ProcessId;HostPid=[int]$hosts[0].ProcessId;HostName=[string]$hosts[0].Name;HostCreated=[string]$hosts[0].CreationDate}
    }
}
function Get-OfficeConsoleSnapshot($Config,[int]$ExcludePid=0) {
    try{return @(Select-OfficeConsoles (Get-CimInstance Win32_Process) $Config $ExcludePid)}catch{return @()}
}
# 닫기 직전에 설정과 프로세스 목록을 다시 맞춰 봐요. 처음 찾은 것과 하나라도 다르면(PID·생성 시각·붙은 프로그램) 닫지 않고 둬요.
function Close-OfficeConsoles($Targets,$Config,$Processes=$null) {
    if($null -eq $Processes){try{$Processes=@(Get-CimInstance Win32_Process)}catch{return @()}}
    $fresh=@(Select-OfficeConsoles $Processes $Config)
    $closed=@()
    foreach($target in @($Targets)){
        $same=@($fresh|Where-Object{$_.WslPid -eq $target.WslPid -and $_.HostPid -eq $target.HostPid -and $_.HostName -eq $target.HostName -and $_.HostCreated -eq $target.HostCreated})
        if($same.Count -ne 1){continue}
        try{Stop-Process -Id $target.HostPid -Force -ErrorAction Stop;$closed+=$target.HostPid}catch{}
    }
    return $closed
}
