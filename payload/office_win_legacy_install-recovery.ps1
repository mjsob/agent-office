$ErrorActionPreference='Stop'
$folder=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$shortcut=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $folder 'Office 디스크 복구.lnk'))
$shortcut.TargetPath="$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$shortcut.Arguments='-NoProfile -ExecutionPolicy Bypass -File "'+$PSScriptRoot+'\recover.ps1"'
$shortcut.WorkingDirectory=$PSScriptRoot
$shortcut.Description='WSL을 켜지 않고 이전 디스크 줄이기의 연결을 확인해요.'
$shortcut.Save()
