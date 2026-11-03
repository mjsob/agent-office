#!/usr/bin/env bash
# Windows 도우미(office-win.exe)를 이 PC의 .NET Framework 내장 컴파일러(csc.exe)로 빌드해요.
# 사용: bash office_win_build.sh <Office 앱 폴더>     (WSL에서만 의미가 있어요)
# 마지막 줄에 JSON 한 줄: {"ok":true|false,"status":"built|current|busy|failed|unsupported","reason":"..."}
# 실패해도 설치는 계속돼요. 도우미가 없으면 PowerShell 경로를 써요.
set -uo pipefail
app=${1:-}
result() { printf '{"ok":%s,"status":"%s","reason":"%s"}\n' "$1" "$2" "$(printf '%s' "$3" | tr -d '\r"\\' | tr '\n' ' ' | head -c 200)"; [[ $1 == true ]]; exit; }
[[ -n $app && -f $app/office_win.cs ]] || result false failed "도우미 소스를 찾지 못했어요"
app=$(cd -- "$app" && pwd -P)
[[ -n ${WSL_DISTRO_NAME:-} ]] && command -v wslpath >/dev/null && command -v cmd.exe >/dev/null || result false unsupported "WSL이 아니에요"
if [[ -n ${OFFICE_WIN_TARGET_DIR:-} ]]; then target=$OFFICE_WIN_TARGET_DIR
else
  local_app=$(cmd.exe /c 'echo %LOCALAPPDATA%' 2>/dev/null </dev/null | tr -d '\r')
  [[ -n $local_app ]] || result false failed "LOCALAPPDATA를 읽지 못했어요"
  target=$(wslpath "$local_app")/Office
fi
winroot=$(wslpath -u "$(cmd.exe /c 'echo %WINDIR%' 2>/dev/null </dev/null | tr -d '\r')" 2>/dev/null || true)
csc=${OFFICE_WIN_CSC:-}
if [[ -z $csc ]]; then
  for c in "$winroot/Microsoft.NET/Framework64/v4.0.30319/csc.exe" "$winroot/Microsoft.NET/Framework/v4.0.30319/csc.exe"; do [[ -f $c ]] && { csc=$c; break; }; done
fi
[[ -n $csc && -f $csc ]] || result false failed "Windows 내장 컴파일러(csc.exe)를 찾지 못했어요"
src_sha=$(sha256sum "$app/office_win.cs" | cut -d' ' -f1)
mkdir -p "$target" 2>/dev/null || result false failed "도우미 폴더를 만들지 못했어요"
exe=$target/office-win.exe; meta=$target/office-win.json
probe_ok() { [[ -f $1 ]] && timeout 15 "$1" probe </dev/null 2>/dev/null | tr -d '\r' | grep -q '"ok":true'; }
if [[ -f $meta ]] && grep -q "\"source_sha256\":\"$src_sha\"" "$meta" && probe_ok "$exe"; then result true current "이미 최신이에요"; fi
new=$target/office-win.new.exe
rm -f -- "$new"
cd /mnt/c 2>/dev/null || true
if ! out=$("$csc" /nologo /target:exe /optimize /out:"$(wslpath -w "$new")" /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Management.dll /r:System.Web.Extensions.dll /r:Microsoft.CSharp.dll /r:System.Core.dll "$(wslpath -w "$app/office_win.cs")" 2>&1 </dev/null); then
  rm -f -- "$new"; out=$(printf '%s' "$out" | iconv -f CP949 -t UTF-8 2>/dev/null || printf '%s' "$out"); result false failed "빌드하지 못했어요: $out"
fi
chmod +x "$new" 2>/dev/null || true
if ! probe_ok "$new"; then rm -f -- "$new"; result false failed "빌드한 도우미를 실행할 수 없어요(실행 파일 제한 정책일 수 있어요)"; fi
if ! mv -f -- "$new" "$exe" 2>/dev/null; then rm -f -- "$new"; if probe_ok "$exe"; then result false busy "기존 도우미가 사용 중이라 바꾸지 못했어요"; fi; result false failed "도우미를 바꾸지 못했어요"; fi
printf '{"api":1,"source_sha256":"%s","built_at":%s}\n' "$src_sha" "$(date +%s)" > "$meta"
result true built "새로 빌드했어요"
