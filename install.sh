#!/usr/bin/env bash
# Office bootstrap for Linux/WSL. Preview by default; never source this script.
set -Eeuo pipefail
umask 077

usage() {
  cat <<'EOF'
Office 설치기 (Linux/WSL)
  bash install.sh                         변경 없이 설치 계획만 확인
  bash install.sh --apply                 대상 PC에서 설치 → 로그인 → 실행
  bash install.sh --apply --install-system-deps
                                         누락된 시스템 의존도 apt로 설치
옵션:
  --prefix /절대/경로     설치 폴더 (기본 ~/.local/share/office-app)
  --workspace /절대/경로  기본 작업 폴더 (기본 ~/agent-office)
  --dry-run              변경 없이 계획만 출력 (기본)
  --skip-login           로그인 확인/실행 생략
  --no-launch            Office 실행 생략
  --device-auth          Codex 기기 코드 로그인
  --install-system-deps  누락된 Python/git/curl을 apt로 설치 허용
  -h, --help             도움말

기존 Office·셸 프로필·설정은 덮어쓰지 않음. ~/.local/bin/office가 없을 때만 만듦.
에이전트(Claude·Codex)는 전체 권한으로 실행됨. 제한하려면 OFFICE_SANDBOX=1로 실행.
실제 설치는 대상 PC에 필요한 CLI를 설치하고 사용자 로그인을 진행함.
현재 사용 중인 WSL에서 변경을 원치 않으면 --apply를 실행하지 마세요.
EOF
}
fail() { printf '설치 중단: %s\n' "$*" >&2; exit 1; }
original_path=${PATH:-}
apply=0; login=1; launch=1; device=0; system_deps=0
prefix="${HOME:?HOME이 필요합니다}/.local/share/office-app"
workspace=$HOME/agent-office
while (($#)); do
  case "$1" in
    --apply) apply=1;;
    --dry-run) apply=0;;
    --prefix) (($# >= 2)) || fail '--prefix 뒤에 경로가 필요합니다'; prefix=$2; shift;;
    --workspace) (($# >= 2)) || fail '--workspace 뒤에 경로가 필요합니다'; workspace=$2; shift;;
    --skip-login) login=0;;
    --no-launch) launch=0;;
    --device-auth) device=1;;
    --install-system-deps) system_deps=1;;
    -h|--help) usage; exit 0;;
    *) fail "알 수 없는 옵션: $1";;
  esac
  shift
done
[[ $prefix == /* && $prefix != / && $prefix != "$HOME" ]] || fail '설치 경로는 홈 자체가 아닌 절대경로여야 합니다'
[[ $prefix != *$'\n'* && $prefix != *$'\r'* ]] || fail '경로에 줄바꿈을 넣을 수 없습니다'
[[ $workspace == /* && $workspace != / && $workspace != "$HOME" ]] || fail '작업 폴더는 홈 자체가 아닌 절대경로여야 합니다'
[[ $workspace != *$'\n'* && $workspace != *$'\r'* ]] || fail '경로에 줄바꿈을 넣을 수 없습니다'
[[ ! -e $workspace || -d $workspace ]] || fail "작업 폴더 경로가 폴더가 아닙니다: $workspace"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
payload=$script_dir/payload
files=(office.py office.html office.ico office-icon.png VERSION)
for name in "${files[@]}" SHA256SUMS; do
  [[ -f "$payload/$name" ]] || fail "배포 파일 누락: $name"
done
printf '설치 폴더: %s\n' "$prefix"
printf '기본 작업 폴더: %s\n' "$workspace"
if [[ -e $HOME/.local/bin/office || -L $HOME/.local/bin/office ]]; then
  printf '기존 office 명령(~/.local/bin/office)은 이 설치기가 만든 것이면 새 설치로 바꾸고, 아니면 그대로 둡니다.\n'
else
  printf 'office 명령을 ~/.local/bin/office에 만듭니다.\n'
fi
printf '에이전트는 전체 권한으로 실행합니다. 제한하려면 OFFICE_SANDBOX=1로 실행하세요.\n'
if (( ! apply )); then
  printf '미리보기입니다. 파일 생성·다운로드·설치·로그인을 하지 않습니다.\n'
  printf '대상 PC에서 --apply를 붙이면 필요한 CLI 설치 후 로그인과 실행을 진행합니다.\n'
  printf '시스템 패키지 설치 허용: %s / 로그인: %s / 실행: %s\n' "$system_deps" "$login" "$launch"
  exit 0
fi
[[ $(uname -s) == Linux ]] || fail '현재 설치기는 Linux/WSL만 지원합니다'
command -v sha256sum >/dev/null || fail 'sha256sum이 필요합니다'
(cd "$payload" && sha256sum --check --strict SHA256SUMS) || fail '배포 파일 해시가 맞지 않습니다'
# Existing install trees and symlink components are never silently adopted.
probe=$prefix
while [[ $probe != / ]]; do
  [[ ! -L $probe ]] || fail "심볼릭 링크 경로에는 설치하지 않습니다: $probe"
  probe=$(dirname -- "$probe")
done
if [[ -e $prefix ]]; then
  [[ -d $prefix && -f "$prefix/.office-install" && ! -L "$prefix/.office-install" ]] || fail '기존 폴더는 이 설치기로 만든 Office 폴더가 아닙니다'
  [[ $(cat -- "$prefix/.office-install") == office-bootstrap-v1 ]] || fail '설치 폴더 표식이 맞지 않습니다'
  for name in app bin workspace; do
    [[ ! -L "$prefix/$name" ]] || fail "기존 $name 폴더가 심볼릭 링크입니다"
    [[ ! -e "$prefix/$name" || -d "$prefix/$name" ]] || fail "기존 $name 경로가 폴더가 아닙니다"
  done
  [[ ! -L "$prefix/bin/office" ]] || fail '기존 전용 런처가 심볼릭 링크입니다'
fi
find_python() {
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys, tomllib, fcntl; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      command -v "$candidate"; return 0
    fi
  done
  return 1
}
python_bin=$(find_python || true)
missing=()
[[ -n $python_bin ]] || missing+=(python3)
command -v git >/dev/null || missing+=(git)
command -v curl >/dev/null || missing+=(curl)
command -v flock >/dev/null || missing+=(util-linux)
if ((${#missing[@]})); then
  (( system_deps )) || fail "필요한 의존이 없습니다: ${missing[*]}. Python 3.11+와 git/curl/flock 설치 또는 --install-system-deps 사용(대상 PC만)."
  command -v apt-get >/dev/null || fail '자동 의존 설치는 apt 기반 배포판만 지원합니다'
  elevate=()
  if (( EUID != 0 )); then
    command -v sudo >/dev/null || fail '시스템 패키지 설치에 sudo가 필요합니다'
    elevate=(sudo)
  fi
  "${elevate[@]}" apt-get update
  "${elevate[@]}" apt-get install -y "${missing[@]}" ca-certificates
  python_bin=$(find_python || true)
  [[ -n $python_bin ]] || fail '이 배포판의 기본 Python은 3.11 미만입니다. Ubuntu 24.04 이상 환경을 사용하세요. Python 저장소를 임의로 추가하지 않습니다.'
fi
[[ -n $python_bin ]] || fail 'Python 3.11+가 필요합니다'
for cmd in git curl flock; do command -v "$cmd" >/dev/null || fail "의존 설치 후에도 $cmd 명령이 없습니다"; done
mkdir -p -- "$prefix"
printf '%s\n' office-bootstrap-v1 > "$prefix/.office-install"
[[ ! -L "$prefix/.install.lock" ]] || fail '설치 잠금 파일이 심볼릭 링크입니다'
exec 9>"$prefix/.install.lock"
flock -n 9 || fail '이 폴더에서 다른 설치기가 실행 중입니다'
staging=$(mktemp -d "$prefix/.staging.XXXXXX")
cleanup() { rm -rf -- "$staging"; }
trap cleanup EXIT
for name in "${files[@]}"; do cp -- "$payload/$name" "$staging/$name"; done
chmod 700 "$staging/office.py"

export PATH="$HOME/.local/bin:$PATH"
# Download official installers as files; an HTTP failure never executes a partial script.
install_cli() {
  local name=$1 url=$2 shell=$3
  if command -v "$name" >/dev/null; then
    printf '%s: 이미 설치됨, 재설치하지 않습니다.\n' "$name"
    return
  fi
  printf '%s 공식 설치기를 받습니다.\n' "$name"
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --connect-timeout 15 --max-time 180 "$url" -o "$staging/$name-install.sh"
  # Installer processes may use their own global installation locations on the target PC.
  DISABLE_AUTOUPDATER=1 "$shell" "$staging/$name-install.sh"
}
install_cli codex https://chatgpt.com/codex/install.sh sh
install_cli claude https://claude.ai/install.sh bash
# Official native installers put launchers here. This changes only this process PATH.
export PATH="$HOME/.local/bin:$PATH"
for cmd in codex claude; do command -v "$cmd" >/dev/null || fail "$cmd 설치 후 명령을 찾지 못했습니다. 설치기 출력과 PATH를 확인하세요"; done
codex --version
DISABLE_AUTOUPDATER=1 claude --version

# Keep pristine incoming files separate from any reapplied local edits.
local_summary=$("$python_bin" - "$prefix" "$staging" "${files[@]}" <<'PYLOCAL'
import datetime, hashlib, json, os, shutil, subprocess, sys
from pathlib import Path
root, incoming = map(Path, sys.argv[1:3])
names = sys.argv[3:]
app = root / 'app'
def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()
manifest_path = app / '.office-manifest.json'
old = None
try:
    old = json.loads(manifest_path.read_text())
    if not isinstance(old.get('files'), dict): old = None
except (OSError, ValueError, TypeError):
    pass
changed = []
for name in names:
    current = app / name
    if old and name in old['files'] and not current.exists() and not current.is_symlink():
        changed.append(name)
    elif current.exists() or current.is_symlink():
        # An older install has no trustworthy baseline: preserve every differing file.
        expected = old['files'].get(name) if old else digest(incoming / name)
        if current.is_symlink() or not current.is_file() or digest(current) != expected:
            changed.append(name)
backup = None
merged, conflicts = [], []
legacy = old is None and app.is_dir() and any(app.iterdir())
if changed or legacy:
    version = str((old or {}).get('version', 'legacy'))
    version = ''.join(c if c.isalnum() or c in '.-+' else '_' for c in version)[:80] or 'legacy'
    backups = root / 'backups'
    if backups.is_symlink(): raise RuntimeError('백업 폴더가 심볼릭 링크예요')
    backups.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup = backups / (version + '-' + stamp)
    shutil.copytree(app, backup, symlinks=True)
# Write baselines before merging. Never treat a locally edited result as upstream.
orig = incoming / '.office-orig'
orig.mkdir()
new_manifest = {'version': (incoming / 'VERSION').read_text().strip(),
                'files': {name: digest(incoming / name) for name in names}}
for name in names:
    shutil.copyfile(incoming / name, orig / name)
for name in (changed if old else []):
    current, base, target = app / name, app / '.office-orig' / name, incoming / name
    # A missing/invalid baseline or binary edit stays in the backup only.
    if (not old or name == 'VERSION' or not base.is_file() or base.is_symlink()
            or not current.is_file() or current.is_symlink()
            or digest(base) != old['files'].get(name)):
        conflicts.append(name); continue
    if any(b'\0' in p.read_bytes() for p in (base, current, target)):
        conflicts.append(name); continue
    if shutil.which('git'):
        cmd = ['git', 'merge-file', '-p', str(current), str(base), str(target)]
    elif shutil.which('diff3'):
        cmd = ['diff3', '-m', str(current), str(base), str(target)]
    else:
        conflicts.append(name); continue
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode == 0:
        target.write_bytes(result.stdout); merged.append(name)
    else:
        conflicts.append(name)
(incoming / '.office-manifest.json').write_text(json.dumps(new_manifest, ensure_ascii=False) + '\n')
print('OFFICE_LOCAL: changed=%d merged=%s conflicts=%s backup=%s' %
      (len(changed) if old else 0, ','.join(merged) or '-', ','.join(conflicts) or '-', str(backup) if backup else '-') + (' legacy=1' if legacy else ''))
PYLOCAL
) || fail '로컬 수정 백업 또는 병합에 실패했어요. 기존 설치를 그대로 둬요.'

mkdir -p -- "$prefix/app" "$prefix/bin" "$workspace"
for name in "${files[@]}"; do mv -f -- "$staging/$name" "$prefix/app/$name"; done
# Replace baseline metadata without following old metadata symlinks.
rm -rf -- "$prefix/app/.office-orig"
mv -- "$staging/.office-orig" "$prefix/app/.office-orig"
mv -f -- "$staging/.office-manifest.json" "$prefix/app/.office-manifest.json"
# The launcher always starts in the chosen workspace; ~/.local/bin/office is created only when absent.
# printf %q safely handles spaces/quotes without evaluating paths as shell code.
{
  printf '#!/usr/bin/env bash\nset -euo pipefail\n'
  printf 'install_root=%q\npython_bin=%q\nwork_dir=%q\n' "$prefix" "$python_bin" "$workspace"
  printf 'export PATH="$install_root/bin:$HOME/.local/bin:$PATH"\n'
  printf 'export DISABLE_AUTOUPDATER=1\n'
  printf 'if (($# == 0)); then\n'
  printf '  unset OFFICE_DIR OFFICE_SESSION OFFICE_JOB OFFICE_AS; cd -- "$work_dir"\n'
  printf 'elif [[ -d "$1" ]]; then\n'
  printf '  unset OFFICE_DIR OFFICE_SESSION OFFICE_JOB OFFICE_AS\n'
  printf 'elif [[ -z ${OFFICE_DIR:-} ]]; then\n'
  printf '  cd -- "$work_dir"\n'
  printf 'fi\nexec "$python_bin" "$install_root/app/office.py" "$@"\n'
} > "$staging/office-launcher"
chmod 700 "$staging/office-launcher"
cp -- "$staging/office-launcher" "$staging/office-global"
mv -f -- "$staging/office-launcher" "$prefix/bin/office"
printf 'Office 설치 완료: %s/bin/office\n' "$prefix"
# ~/.local/bin/office: 없으면 만들고, 예전에 이 설치기가 만든 실행 파일이면 새 설치로 바꾼다.
# 사용자가 직접 만든 파일·링크는 바꾸지 않고 경고만 한다.
global=$HOME/.local/bin/office
if [[ ! -e $global && ! -L $global ]]; then
  mkdir -p -- "$HOME/.local/bin"
  chmod 755 "$staging/office-global"
  mv -- "$staging/office-global" "$global"
  printf 'office 명령 생성: %s\n' "$global"
elif [[ -L $global && $(readlink -f -- "$global") == $(readlink -f -- "$prefix/bin/office") ]]; then
  :   # 이 설치의 실행 파일을 가리키는 링크면 그대로 둔다
elif [[ -f $global && ! -L $global ]] && grep -q '^install_root=' "$global" && grep -q '/app/office.py' "$global"; then
  if ! cmp -s -- "$staging/office-global" "$global"; then
    chmod 755 "$staging/office-global"
    mv -f -- "$staging/office-global" "$global"
    printf 'office 명령을 새 설치로 바꿨습니다: %s\n' "$global"
  fi
else
  printf '주의: %s 는 이 설치기가 만든 파일이 아니라서 그대로 둡니다.\n' "$global"
  printf '새 Office를 쓰려면 그 파일을 지우거나 이름을 바꾼 뒤 설치 명령을 다시 실행하세요.\n'
fi
# PATH 앞쪽에 다른 office가 있으면 새 설치가 가려진다
found=$(PATH=$original_path command -v office 2>/dev/null || true)
if [[ -n $found && $(readlink -f -- "$found") != "$(readlink -f -- "$global")" && $(readlink -f -- "$found") != "$(readlink -f -- "$prefix/bin/office")" ]]; then
  printf '주의: office 명령이 %s 를 먼저 실행합니다.\n' "$found"
  printf '새 Office는 %s 입니다. 그 파일을 지우거나 이름을 바꾸세요.\n' "$prefix/bin/office"
fi

case ":$original_path:" in
  *":$HOME/.local/bin:"*) ;;
  *)
    printf '새 WSL 터미널을 열고 office를 실행하세요.\n'
    printf '명령을 찾지 못하면 ~/.profile에서 ~/.local/bin을 PATH에 추가하는지 확인하세요.\n'
    printf '현재 터미널에서 바로 실행: %q\n' "$prefix/bin/office"
    ;;
esac

if (( login )); then
  [[ -t 0 ]] || fail '로그인은 대화형 터미널에서 진행하세요. 자동 설치만 하려면 --skip-login --no-launch 사용.'
  if ! codex login status >/dev/null 2>&1; then
    printf 'Codex 로그인: 브라우저에서 직접 승인하세요.\n'
    if (( device )); then codex login --device-auth; else codex login; fi
    codex login status >/dev/null 2>&1 || fail 'Codex 로그인이 완료되지 않았습니다'
  fi
  if ! DISABLE_AUTOUPDATER=1 claude auth status >/dev/null 2>&1; then
    printf 'Claude 로그인: 브라우저에서 직접 승인하세요.\n'
    DISABLE_AUTOUPDATER=1 claude auth login
    DISABLE_AUTOUPDATER=1 claude auth status >/dev/null 2>&1 || fail 'Claude 로그인이 완료되지 않았습니다'
  fi
fi
cleanup
trap - EXIT
# Do not retain the installer lock in the long-running GUI.
flock -u 9
exec 9>&-
printf '%s\n' "$local_summary"
if (( launch )); then
  printf '별도 작업 폴더에서 Office를 시작합니다.\n'
  "$prefix/bin/office"
else
  printf '실행: %q\n' "$prefix/bin/office"
fi
