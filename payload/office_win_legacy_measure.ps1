param([Parameter(Mandatory=$true)][string]$Distro)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$found = @(Get-ChildItem HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss | Get-ItemProperty | Where-Object { $_.DistributionName -ceq $Distro })
if ($found.Count -ne 1) { throw '배포판 하나를 찾지 못했어요.' }
$entry = $found[0]
if ($entry.Version -ne 2) { throw 'WSL 2 배포판이 아니에요.' }
$name = if ($entry.VhdFileName) { $entry.VhdFileName } else { 'ext4.vhdx' }
$file = Get-Item -LiteralPath (Join-Path $entry.BasePath $name)
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class VhdAllocation {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern uint GetCompressedFileSizeW(string path, out uint high);
    [DllImport("kernel32.dll")]
    public static extern void SetLastError(uint error);
}
'@
[uint32]$high = 0
[VhdAllocation]::SetLastError(0)
[uint32]$low = [VhdAllocation]::GetCompressedFileSizeW($file.FullName, [ref]$high)
$errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
if ($low -eq [uint32]::MaxValue -and $errorCode -ne 0) { throw "실점유를 읽지 못했어요: $errorCode" }
$drive = New-Object System.IO.DriveInfo([System.IO.Path]::GetPathRoot($file.FullName))
@{
    distro = $Distro
    path = $file.FullName
    logical_bytes = [uint64]$file.Length
    allocated_bytes = ([uint64]$high * 4294967296 + [uint64]$low)
    free_bytes = $drive.AvailableFreeSpace
    drive = $drive.Name.TrimEnd('\')
} | ConvertTo-Json -Compress
