#!/usr/bin/env bash
# Windows 독립 실행창에서 진행률을 보고 중단해요. 관리자 승인 전에는 WSL을 끄지 않아요.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# Office 앱 폴더에서 실행해요. Windows 쪽 실행 파일(office-win.exe)이 되면 그걸 쓰고, 안 되면 PowerShell 파일로 대신해요.
legacy_names="CompactOperation.cs OfficeConsole.cs common.ps1 compact.ps1 install-recovery.ps1 measure.ps1 recover.ps1 reopen-office.ps1 run.ps1 status.ps1"
mode=${1:-run}
case "$mode" in run|--dry-run|--status|--result|--cancel|--recover|--install) ;; *) echo "알 수 없는 옵션이에요: $mode" >&2; exit 64 ;; esac
distro=${WSL_DISTRO_NAME:-}
[ -n "$distro" ] || { echo 'WSL 배포판 이름을 알 수 없어요.' >&2; exit 2; }
win_local=$(wslpath "$(cmd.exe /c 'echo %LOCALAPPDATA%' 2>/dev/null | tr -d '\r')")
dest="$win_local/wsl-compact"
exe="$win_local/Office/office-win.exe"
dest_w=$(wslpath -w "$dest")
exe_w=$(wslpath -w "$exe")
host=''
if [ "${OFFICE_WIN_HOST:-}" != ps ] && [ -f "$exe" ] && timeout 15 "$exe" probe </dev/null 2>/dev/null | tr -d '\r' | grep -q '"ok":true';then host=exe
elif [ "${OFFICE_WIN_HOST:-}" != exe ] && command -v powershell.exe >/dev/null && ps_note=$(timeout 20 powershell.exe -NoProfile -NonInteractive -Command 'exit 0' 2>&1 </dev/null);then host=ps;fi
if [ -z "$host" ] && [ "$mode" != '--result' ];then
  echo '이 PC에서는 디스크 줄이기를 쓸 수 없어요: PowerShell과 Office 도우미(office-win.exe)를 모두 실행할 수 없어요.' >&2;exit 5
fi
state_dir="$HOME/.local/state/wsl-compact"
probe() {
  if [ "$host" = exe ]; then
    "$exe" status --dir "$dest_w" </dev/null | tr -d '\r'
  elif [ -f "$dest/common.ps1" ] && [ -f "$dest/status.ps1" ]; then
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w "$dest/status.ps1")" </dev/null | tr -d '\r'
  elif [ -e "$dest/active.lock" ]; then
    local lock;lock=$(wslpath -w "$dest/active.lock")
    powershell.exe -NoProfile -Command "try{\$f=[IO.File]::Open('$lock','Open','ReadWrite','None');\$f.Close();'{\"running\":false}'}catch{'{\"running\":true}'}" </dev/null | tr -d '\r'
  else printf '%s\n' '{"running":false}'; fi
}
running(){ probe | python3 -c 'import json,sys;sys.exit(0 if json.load(sys.stdin)["running"] else 1)'; }
if [ "$mode" = '--status' ];then probe;exit 0;fi
if [ "$mode" = '--result' ];then [ -f "$dest/result.json" ] || exit 1;tr -d '\r\n' < "$dest/result.json";echo;exit 0;fi
if [ "$mode" = '--cancel' ];then
  if [ "$host" != exe ];then [ -f "$dest/common.ps1" ] || { echo '이전 버전 실행은 Windows 창에서 확인하세요.' >&2;exit 4; };fi
  rid=${WSL_COMPACT_REQUEST_ID:-$(python3 - "$dest/status.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1],encoding='utf-8-sig'))['request_id'])
PY
)}
  if [ "$host" = exe ];then "$exe" status --dir "$dest_w" --cancel "$rid" </dev/null;else
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w "$dest/status.ps1")" -Cancel -RequestId "$rid" </dev/null;fi
  exit $?
fi
# Measurement is read-only and doesn't overwrite scripts used by an active worker.
if [ "$host" = exe ];then info=$("$exe" measure "$distro" </dev/null | tr -d '\r') || { echo '대상 디스크를 확인하지 못했어요.' >&2;exit 2; }
else info=$(powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w "$here/office_win_legacy_measure.ps1")" -Distro "$distro" </dev/null | tr -d '\r') || { echo '대상 디스크를 확인하지 못했어요.' >&2;exit 2; };fi
vhdx_w=$(printf '%s' "$info" | python3 -c 'import json,sys;print(json.load(sys.stdin)["path"])')
vhdx=$(wslpath "$vhdx_w")
[ -f "$vhdx" ] || { echo '대상 디스크 파일이 없어요.' >&2;exit 2; }
if [ "$mode" = '--dry-run' ];then
  snapshot=$(probe)
  printf '%s' "$info" | python3 -c 'import json,sys;d=json.load(sys.stdin);d["running"]=json.loads(sys.argv[1])["running"];print(json.dumps(d,ensure_ascii=False))' "$snapshot"
  exit 0
fi
mkdir -p "$state_dir"
exec 9>"$state_dir/run.lock"
flock -n 9 || { echo '이미 실행을 준비하고 있어요.' >&2;exit 3; }
snapshot=$(probe) || { echo 'Windows 실행 상태를 확인하지 못했어요. 새 실행은 시작하지 않아요.' >&2;exit 4; }
active=$(printf '%s' "$snapshot" | python3 -c 'import json,sys;v=json.load(sys.stdin).get("running");sys.exit(4) if type(v) is not bool else print(str(v).lower())') || exit 4
if [ "$active" = true ];then echo '이미 디스크 줄이기 실행창이 열려 있어요.' >&2;exit 3;fi
mkdir -p "$dest"
if [ "$host" = ps ];then
  for n in $legacy_names;do cp "$here/office_win_legacy_$n" "$dest/$n";done
fi
ln -sfn "$dest/status.json" "$state_dir/status.json"
rid=${WSL_COMPACT_REQUEST_ID:-}
if [ -z "$rid" ];then
  if [ "$mode" = 'run' ];then rid=$(python3 -c 'import uuid;print(uuid.uuid4().hex)');else
    rid=$(python3 - "$state_dir/office-launch.json" <<'PY'
import json,sys
try:print(json.load(open(sys.argv[1]))['request_id'])
except (OSError,ValueError,KeyError):print('manual')
PY
)
  fi
fi
python3 - "$dest/target.json" "$distro" "$rid" "$info" <<'PY'
import json,os,sys
path,distro,rid,raw=sys.argv[1:];data=json.loads(raw)
if any(c in data['path']+distro for c in '\r\n"'):raise SystemExit('대상 경로에 지원하지 않는 문자가 있어요.')
with open(path+'.tmp','w',encoding='utf-8') as f:json.dump({'path':data['path'],'distro':distro,'request_id':rid},f,ensure_ascii=False)
os.replace(path+'.tmp',path)
PY
if [ "$host" = exe ];then
  printf '@echo off\r\n"%s" recover --dir "%s"\r\n' "$exe_w" "$dest_w" > "$dest/recover-wsl.cmd"
  "$exe" install-recovery --dir "$dest_w" </dev/null >/dev/null
else
  cat > "$dest/recover-wsl.cmd" <<'CMD'
@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0recover.ps1"
CMD
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w "$dest/install-recovery.ps1")" </dev/null >/dev/null
fi
# Remember the original Linux identity and workspace; the Windows watcher uses a visible console.
linux_user=$(id -un)
office_command=$(command -v office || true)
workspace=${WSL_COMPACT_WORKSPACE:-${OFFICE_DIR:-$HOME/agent-office}}
python3 - "$dest" "$distro" "$rid" "$linux_user" "$office_command" "$workspace" <<'PYRESTORE'
import json,os,shlex,sys
from pathlib import Path
folder,distro,rid,user,command,workspace=sys.argv[1:]
root=Path(folder);workspace=Path(workspace).expanduser().resolve()
available=bool(command and Path(command).is_file() and workspace.is_dir())
server='\\\\wsl.localhost\\'+distro+str(workspace).replace('/','\\')+'\\.office\\server.json'
# Office 바로가기 콘솔 파일: office 명령이 가리키는 설치의 bin 폴더에 있어요(없으면 비워 둬요).
launcher=Path(command).resolve() if command else None
rc=launcher.parent/'office-console.bashrc' if launcher and launcher.name=='office' else None
console_rc=str(rc) if rc and rc.is_file() else ''
config={'request_id':rid,'available':available,'distro':distro,'linux_user':user,'workspace':str(workspace),'script_linux':str(root/'reopen-office.sh'),'console_rc':console_rc,'server_windows':server}
(root/'restore-office.json').write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
if available:
 text='#!/usr/bin/env bash\nset -euo pipefail\nunset OFFICE_DIR OFFICE_SESSION OFFICE_JOB OFFICE_AS\ncd -- '+shlex.quote(str(workspace))+'\nexec '+shlex.join([command,str(workspace)])+'\n'
 (root/'reopen-office.sh').write_text(text,encoding='utf-8');(root/'reopen-office.sh').chmod(0o700)
PYRESTORE
if [ "$mode" = '--install' ];then echo '진행창과 Windows 시작 메뉴의 [Office 디스크 복구]를 설치했어요. WSL은 끄지 않았어요.';exit 0;fi
if [ "$mode" = '--recover' ];then
  if [ "$host" = exe ];then cmd.exe /c start '""' "$exe_w" recover --dir "$dest_w" </dev/null;else
    powershell.exe -NoProfile -Command "Start-Process powershell.exe -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"$(wslpath -w "$dest/recover.ps1")\"'" </dev/null;fi
  exit $?
fi
python3 - "$dest/status.json" "$rid" "$state_dir/office-launch.json" "${WSL_COMPACT_REQUEST_ID:-}" <<'PY'
import json,os,sys,time
path,rid,launch,external=sys.argv[1:]
data={'schema_version':1,'request_id':rid,'status':'preparing','phase':'trim','updated_at':time.time(),'message':'빈 공간을 표시하고 있어요. 아직 WSL을 끄지 않았어요.','can_cancel':False,'wsl_ready':True}
for target in ([path] if external else [path,launch]):
 with open(target+'.tmp','w',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False)
 os.replace(target+'.tmp',target)
PY
echo '빈 공간을 표시해요(fstrim). 아직 WSL은 켜져 있어요.'
trim_ok=true
wsl.exe -d "$distro" -u root -e /usr/sbin/fstrim -v / || { trim_ok=false;echo '빈 공간 표시를 마치지 못했어요. 이미 표시한 공간만 회수할 수 있어요.'; }
# 줄인 뒤 크기와 이 사용량의 차이가 다음 회수 후보에서 뺄 고정분이에요. 기록 실패는 실행을 막지 않아요.
python3 - "$state_dir/pre-compact.json" "$rid" "$distro" "$trim_ok" "$vhdx_w" <<'PYPRE' || echo '사용량 기록을 남기지 못했어요. 다음 회수 후보는 이전 기록으로 계산해요.'
import json,os,sys,time
path,rid,distro,trim,vhdx=sys.argv[1:];st=os.statvfs('/')
data={'schema_version':1,'request_id':rid,'distro':distro,'path':vhdx,'trim_ok':trim=='true','used_gb':(st.f_blocks-st.f_bfree)*st.f_frsize/2**30,'recorded_at':time.time()}
with open(path+'.tmp','w',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False)
os.replace(path+'.tmp',path)
PYPRE
# This helper runs with the logged-in user's normal token and survives WSL shutdown.
reopen_task="OfficeAfterCompact-$rid"
reopen_ps1=$(wslpath -w "$dest/reopen-office.ps1")
reopen_ok=0
if [ "$host" = exe ];then reopen_cmd="\"$exe_w\" reopen $rid --dir \"$dest_w\"";else reopen_cmd="powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"$reopen_ps1\" -RequestId \"$rid\"";fi
if schtasks.exe /create /f /it /rl LIMITED /tn "$reopen_task" /sc once /st 23:59 /tr "$reopen_cmd" >/dev/null && schtasks.exe /run /tn "$reopen_task" >/dev/null;then
  for _ in {1..10};do
    if python3 - "$dest/reopen-status.json" "$rid" <<'PYREOPEN'
import json,sys
try:d=json.load(open(sys.argv[1],encoding='utf-8-sig'))
except (OSError,ValueError):raise SystemExit(1)
raise SystemExit(0 if d.get('request_id')==sys.argv[2] else 1)
PYREOPEN
    then reopen_ok=1;break;fi
    sleep 1
  done
fi
schtasks.exe /delete /tn "$reopen_task" /f >/dev/null 2>&1 || true
if [ "$reopen_ok" != 1 ];then
  python3 - "$dest/reopen-status.json" "$rid" <<'PYREOPENFAIL'
import json,sys,time
with open(sys.argv[1],'w',encoding='utf-8') as f:json.dump({'request_id':sys.argv[2],'status':'unavailable','updated_at':time.time(),'message':'Office 자동 복귀를 준비하지 못했어요. 끝나면 WSL에서 office를 실행하세요.'},f,ensure_ascii=False)
PYREOPENFAIL
fi
ps1=$(wslpath -w "$dest/run.ps1")
task="WslCompact-$rid"
# /IT makes the permission prompt and independent progress window visible in the logged-in desktop.
if [ "$host" = exe ];then run_cmd="\"$exe_w\" launch --dir \"$dest_w\"";else run_cmd="powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"$ps1\"";fi
if ! schtasks.exe /create /f /it /tn "$task" /sc once /st 23:59 /tr "$run_cmd" >/dev/null || ! schtasks.exe /run /tn "$task" >/dev/null;then
  schtasks.exe /delete /tn "$task" /f >/dev/null 2>&1 || true
  python3 - "$dest/status.json" "$rid" <<'PY'
import json,os,sys,time
path,rid=sys.argv[1:]
with open(path+'.tmp','w',encoding='utf-8') as f:json.dump({'schema_version':1,'request_id':rid,'status':'failed','updated_at':time.time(),'message':'Windows 실행창을 시작하지 못했어요. WSL은 끄지 않았어요.','wsl_ready':True,'can_cancel':False},f,ensure_ascii=False)
os.replace(path+'.tmp',path)
PY
  echo 'Windows 실행창을 시작하지 못했어요. WSL은 끄지 않았어요.' >&2;exit 4
fi
started=0
for _ in {1..12};do running && { started=1;break; };sleep 1;done
schtasks.exe /delete /tn "$task" /f >/dev/null 2>&1 || true
if [ "$started" != 1 ];then
  # A prompt can be denied and the worker can exit before the first probe; keep its terminal result.
  python3 - "$dest/status.json" "$rid" <<'PY'
import json,os,sys,time
path,rid=sys.argv[1:]
try:d=json.load(open(path,encoding='utf-8-sig'))
except (OSError,ValueError):d={}
if d.get('request_id')==rid and d.get('status') in ('failed','cancelled','completed'):raise SystemExit(0)
d={'schema_version':1,'request_id':rid,'status':'uncertain','updated_at':time.time(),'message':'실행창을 확인하지 못했어요. Windows 알림과 관리자 권한 창을 확인하세요.','can_cancel':False}
with open(path+'.tmp','w',encoding='utf-8') as f:json.dump(d,f,ensure_ascii=False)
os.replace(path+'.tmp',path)
PY
  echo '실행창 상태를 확인하세요. 상태 확인 전에는 다시 실행하지 마세요.';exit 4
fi
echo 'Windows에서 관리자 권한을 먼저 확인해요. 승인 후 독립 진행창에서 남은 시간과 진행률을 보여줘요.'
