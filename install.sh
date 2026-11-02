#!/usr/bin/env bash
# Office bootstrap for Linux/WSL/macOS. Preview by default; never source this script.
set -Eeuo pipefail
umask 077

usage() {
  cat <<'EOF'
Office 설치기 (Linux/WSL/macOS)
  bash install.sh                         변경 없이 설치 계획만 확인
  bash install.sh --apply                 대상 PC에서 설치 → 로그인 → 실행
  bash install.sh --apply --install-system-deps
                                         누락된 시스템 의존도 apt 또는 Homebrew로 설치
옵션:
  --prefix /절대/경로     설치 폴더 (기본 ~/.local/share/office-app)
  --workspace /절대/경로  기본 작업 폴더 (기본 ~/agent-office)
  --dry-run              변경 없이 계획만 출력 (기본)
  --skip-login           로그인 확인/실행 생략
  --no-launch            Office 실행 생략
  --review              충돌을 고르는 일회성 선택 화면을 열어요(옛 앱에서 업데이트할 때는 자동)
  --review-plan 파일     미리 점검한 결과(plan JSON)가 지금 설치와 같을 때만 진행
  --resolution 파일      충돌 선택(JSON). --review-plan과 함께만 쓸 수 있어요
  --restore-backup 폴더  백업(<설치 폴더>/backups/…)으로 앱 파일을 되돌림
  --device-auth          Codex 기기 코드 로그인
  --install-system-deps  누락된 Python/git/curl을 apt 또는 Homebrew로 설치 허용
  -h, --help             도움말

기존 Office·셸 프로필·설정은 덮어쓰지 않음. ~/.local/bin/office가 없을 때만 만듦.
에이전트(Claude·Codex)는 전체 권한으로 실행됨. 제한하려면 OFFICE_SANDBOX=1로 실행.
실제 설치는 대상 PC에 필요한 CLI를 설치하고 사용자 로그인을 진행함.
EOF
  # 윈도우 전용 안내는 맥에 보이지 않게 해요
  [[ $(uname -s) == Darwin ]] || cat <<'EOF'
WSL 전용: --no-shortcut 을 붙이면 윈도우 바탕화면·시작 메뉴 바로가기를 만들지 않음.
현재 사용 중인 WSL에서 변경을 원치 않으면 --apply를 실행하지 마세요.
EOF
}
fail() { printf '설치 중단: %s\n' "$*" >&2; exit 1; }
original_path=${PATH:-}
review_plan=; resolution=; restore_backup=; review=0
apply=0; login=1; launch=1; device=0; system_deps=0; shortcut=1
prefix="${HOME:?HOME이 필요합니다}/.local/share/office-app"
workspace=$HOME/agent-office
while (($#)); do
  case "$1" in
    --apply) apply=1;;
    --dry-run) apply=0;;
    --prefix) (($# >= 2)) || fail '--prefix 뒤에 경로가 필요합니다'; prefix=$2; shift;;
    --workspace) (($# >= 2)) || fail '--workspace 뒤에 경로가 필요합니다'; workspace=$2; shift;;
    --review) review=1;;
    --review-plan) (($# >= 2)) || fail '--review-plan 뒤에 파일이 필요합니다'; review_plan=$2; shift;;
    --resolution) (($# >= 2)) || fail '--resolution 뒤에 파일이 필요합니다'; resolution=$2; shift;;
    --restore-backup) (($# >= 2)) || fail '--restore-backup 뒤에 폴더가 필요합니다'; restore_backup=$2; apply=1; login=0; launch=0; shortcut=0; shift;;
    --skip-login) login=0;;
    --no-launch) launch=0;;
    --device-auth) device=1;;
    --install-system-deps) system_deps=1;;
    --no-shortcut) shortcut=0;;
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
[[ -z $resolution || -n $review_plan ]] || fail '--resolution은 --review-plan과 함께 써야 합니다'
for opt_file in "$review_plan" "$resolution"; do
  [[ -z $opt_file || ( $opt_file == /* && -f $opt_file && ! -L $opt_file ) ]] || fail "점검·선택 파일은 실제 파일의 절대경로여야 합니다: $opt_file"
done
[[ -z $restore_backup || ( -z $review_plan && -z $resolution ) ]] || fail '--restore-backup은 다른 점검 옵션과 함께 쓸 수 없습니다'
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
payload=$script_dir/payload
[[ -f "$payload/SHA256SUMS" ]] || fail '배포 파일 누락: SHA256SUMS'
# 설치할 파일 목록은 배포 묶음의 SHA256SUMS를 따라요. 파일 이름이 이상하면 멈춰요.
files=()
while read -r _hash name extra; do
  [[ -n ${_hash:-} ]] || continue
  name=${name#\*}
  [[ -z ${extra:-} && $name =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail "배포 파일 목록이 올바르지 않습니다: $name"
  files+=("$name")
done < "$payload/SHA256SUMS"
for need in office.py VERSION office_customizations.py; do
  [[ " ${files[*]-} " == *" $need "* ]] || fail "배포 파일 목록에 $need 가 없습니다"
done
for name in "${files[@]}" SHA256SUMS; do
  [[ -f "$payload/$name" && ! -L "$payload/$name" ]] || fail "배포 파일 누락: $name"
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
platform=$(uname -s)
case $platform in
  Linux) ;;
  Darwin)
    mac_version=$(sw_vers -productVersion)
    mac_major=${mac_version%%.*}
    mac_minor=0
    [[ $mac_version != *.* ]] || { mac_minor=${mac_version#*.}; mac_minor=${mac_minor%%.*}; }
    # 13.3부터 들어 있는 Safari 16.4 이상이어야 화면이 열려요.
    [[ $mac_major -gt 13 || ( $mac_major -eq 13 && $mac_minor -ge 3 ) ]] || fail 'macOS 13.3 이상이 필요해요'
    export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
    ;;
  *) fail 'Linux/WSL 또는 macOS 13.3 이상에서 실행하세요';;
esac
if command -v sha256sum >/dev/null; then
  (cd "$payload" && sha256sum --check --strict SHA256SUMS) || fail '배포 파일 해시가 맞지 않습니다'
elif command -v shasum >/dev/null; then
  (cd "$payload" && shasum -a 256 --check --strict SHA256SUMS) || fail '배포 파일 해시가 맞지 않습니다'
else
  fail 'sha256sum 또는 shasum이 필요해요'
fi
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
  for candidate in python3.14 python3.13 python3.12 python3.11 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys, tomllib, fcntl; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      command -v "$candidate"; return 0
    fi
  done
  return 1
}
python_bin=$(find_python || true)
if [[ -n $restore_backup ]]; then
  [[ -n $python_bin ]] || fail 'Python 3.11+가 필요해요'
  "$python_bin" -I "$payload/office_customizations.py" --restore-backup "$restore_backup" --prefix "$prefix" --incoming "$payload" || exit $?
  exit 0
fi
missing=()
[[ -n $python_bin ]] || missing+=(python3)
command -v git >/dev/null || missing+=(git)
command -v curl >/dev/null || missing+=(curl)
if [[ $platform == Linux ]]; then command -v flock >/dev/null || missing+=(util-linux); fi
if [[ ${missing[*]-} ]]; then
  (( system_deps )) || fail "필요한 의존이 없습니다: ${missing[*]}. Python 3.11+와 git/curl을 설치하거나 --install-system-deps를 쓰세요."
  if [[ $platform == Darwin ]]; then
    command -v brew >/dev/null || fail 'Homebrew를 https://brew.sh 에서 설치한 뒤 같은 명령을 다시 실행하세요. Homebrew는 자동으로 설치하지 않아요.'
    brew_deps=()
    [[ -n $python_bin ]] || brew_deps+=(python@3.13)
    command -v git >/dev/null || brew_deps+=(git)
    command -v curl >/dev/null || brew_deps+=(curl)
    brew install ${brew_deps[@]+"${brew_deps[@]}"}
    # Versioned Python formulae expose python3.X even when python3 is supplied by CLT.
    if [[ -z $python_bin ]]; then
      brew_python_prefix=$(brew --prefix python@3.13)
      export PATH="$brew_python_prefix/bin:$PATH"
    fi
  else
    command -v apt-get >/dev/null || fail '자동 의존 설치는 apt 기반 배포판에서 지원해요'
    elevate=()
    if (( EUID != 0 )); then
      command -v sudo >/dev/null || fail '시스템 패키지 설치에 sudo가 필요합니다'
      elevate=(sudo)
    fi
    ${elevate[@]+"${elevate[@]}"} apt-get update
    ${elevate[@]+"${elevate[@]}"} apt-get install -y "${missing[@]}" ca-certificates
  fi
  python_bin=$(find_python || true)
fi
[[ -n $python_bin ]] || fail 'Python 3.11+가 필요해요. macOS에서는 Homebrew Python을, Ubuntu에서는 24.04 이상 환경을 사용하세요.'
for cmd in git curl; do command -v "$cmd" >/dev/null || fail "의존 설치 후에도 $cmd 명령이 없습니다"; done
if [[ $platform == Linux ]]; then command -v flock >/dev/null || fail 'flock 명령이 필요해요'; fi
# 옛 버전 앱에는 충돌 선택 화면이 없어요. 충돌이 선택으로 풀 수 있을 때만 일회성 선택 화면(bridge)을 열고,
# 선택이 끝나면 그 화면이 새 설치기를 다시 실행해요. 설치 잠금을 잡기 전에 넘겨서 잠금이 겹치지 않아요.
if [[ -z $review_plan && -z ${OFFICE_BRIDGE_ACTIVE:-} && -f $payload/office_update_bridge.py && -f $prefix/app/.office-manifest.json && ! -L $prefix/app/.office-manifest.json ]]; then
  want_bridge=$review
  if ((! want_bridge)); then
    # 자동 진입: 설치된 버전이 0.10 미만이고, 이 설치기를 부른 쪽이 이 설치의 실행 중인 앱일 때만(ps는 맥에서도 같아요)
    installed_version=$(head -n 1 -- "$prefix/app/VERSION" 2>/dev/null || true)
    IFS=. read -r v_major v_minor _ <<<"${installed_version%%+*}"
    parent_command=$(ps -o command= -p "$PPID" 2>/dev/null || true)
    if [[ ${v_major:-x} == 0 && ${v_minor:-x} =~ ^[0-9]+$ ]] && ((v_minor < 10)) && [[ $parent_command == *"$prefix/app/office.py"* ]]; then want_bridge=1; fi
  fi
  if ((want_bridge)); then
    bridge_plan=$(mktemp "${TMPDIR:-/tmp}/office-bridge-plan.XXXXXX")
    bridge_resolvable=0
    if "$python_bin" -I "$payload/office_customizations.py" --plan --prefix "$prefix" --incoming "$payload" --output "$bridge_plan" >/dev/null 2>&1; then
      bridge_resolvable=$("$python_bin" -I -c 'import json,sys; p=json.load(open(sys.argv[1])); print(1 if p.get("blocked") and p.get("resolvable") else 0)' "$bridge_plan" 2>/dev/null || echo 0)
    fi
    rm -f -- "$bridge_plan"
    if [[ $bridge_resolvable == 1 ]]; then
      bridge_args=(--prefix "$prefix" --package "$script_dir" --workspace "$workspace")
      ((review)) || bridge_args+=(--legacy-parent)
      exec "$python_bin" -I "$payload/office_update_bridge.py" "${bridge_args[@]}"
    fi
  fi
fi
mkdir -p -- "$prefix"
printf '%s\n' office-bootstrap-v1 > "$prefix/.office-install"
[[ ! -L "$prefix/.install.lock" ]] || fail '설치 잠금 파일이 심볼릭 링크입니다'
lock_dir=
staging=
swapping=0; swap_backup=
cleanup() {
  local status=$?
  # 파일을 바꾸는 도중에 멈추면 방금 만든 백업으로 되돌려요.
  if ((swapping)) && [[ -n $swap_backup ]]; then
    printf '설치 도중 멈춰서 이전 파일로 되돌려요.\n' >&2
    "$python_bin" -I "$payload/office_customizations.py" --restore-backup "$swap_backup" --prefix "$prefix" --incoming "$payload" --during-install >&2 \
      || printf '자동으로 되돌리지 못했어요. 백업: %s\n' "$swap_backup" >&2
  fi
  [[ -z $staging ]] || rm -rf -- "$staging"
  [[ -z $lock_dir ]] || rmdir -- "$lock_dir"
}
if [[ $platform == Darwin ]]; then
  mkdir -- "$prefix/.install.lock.d" 2>/dev/null || fail '이 폴더에서 다른 설치기가 실행 중이에요. 강제 종료했다면 .install.lock.d 폴더를 확인하세요.'
  lock_dir=$prefix/.install.lock.d
else
  exec 9>"$prefix/.install.lock"
  flock -n 9 || fail '이 폴더에서 다른 설치기가 실행 중입니다'
fi
trap cleanup EXIT
staging=$(mktemp -d "$prefix/.staging.XXXXXX")
for name in "${files[@]}"; do cp -- "$payload/$name" "$staging/$name"; done
chmod 700 "$staging/office.py"

OFFICE_CUSTOMIZATION_GUARD=1
# 직접 고친 부분 검사는 CLI 설치보다 먼저 해서, 멈출 때 아무것도 바뀌지 않게 한다.
# Keep pristine incoming files separate from any reapplied local edits.
apply_args=(--apply --prefix "$prefix" --incoming "$payload" --staging "$staging")
if [[ -n $review_plan ]]; then
  plan_id=$("$python_bin" -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$review_plan" 2>/dev/null) || fail '점검 파일을 읽을 수 없어요'
  apply_args+=(--expect-plan-id "$plan_id")
  [[ -z $resolution ]] || apply_args+=(--resolution "$resolution")
fi
local_rc=0
local_summary=$("$python_bin" -I "$payload/office_customizations.py" "${apply_args[@]}") || local_rc=$?
if ((local_rc)); then
  printf '%s\n' "$local_summary"
  printf '설치를 멈췄어요. 기존 설치는 그대로예요.\n' >&2
  exit "$local_rc"
fi
[[ ! -f $staging/.backup-path ]] || swap_backup=$(<"$staging/.backup-path")
# 점검 모듈이 든 묶음이면 파일을 바꾸기 전에 설치될 전체 사본을 실제로 실행해 봐요(문법·도움말·버전·설정 저장).
# 바뀐 곳은 아직 준비 폴더뿐이라, 실패해도 기존 설치는 그대로예요.
if [[ -f $staging/office_updates.py ]]; then
  smoke_error=$("$python_bin" -I -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("office_updates_smoke", sys.argv[1] + "/office_updates.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
try:
    module.smoke_candidate(sys.argv[1])
except module.ReviewError as error:
    sys.stderr.write(str(error)); sys.exit(1)
' "$staging" 2>&1) || {
    printf '설치를 멈췄어요. 기존 설치는 그대로예요.\n- 설치할 사본의 실행 검사에 실패했어요: %s\n' "$smoke_error" >&2
    exit 3
  }
fi

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

# ~/.local/bin/office 를 직접 고쳤는지 알아보려고 지난 설치 때 기록한 지문을 미리 읽어 둔다.
file_sha() { "$python_bin" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1" 2>/dev/null || true; }
old_manifest_launcher() { "$python_bin" -c 'import json,sys; print((json.load(open(sys.argv[1])).get("launchers") or {}).get(sys.argv[2],""))' "$prefix/app/.office-manifest.json" "$1" 2>/dev/null || true; }
old_office=$(old_manifest_launcher office); old_console=$(old_manifest_launcher console)
prev_ico_sha=$([[ -f $prefix/app/office.ico && ! -L $prefix/app/office.ico ]] && file_sha "$prefix/app/office.ico" || true)
kept=
old_global=$("$python_bin" -c 'import json,sys; print((json.load(open(sys.argv[1])).get("launchers") or {}).get("global",""))' "$prefix/app/.office-manifest.json" 2>/dev/null || true)
mkdir -p -- "$prefix/app" "$prefix/bin" "$workspace"
swapping=1
for name in "${files[@]}"; do mv -f -- "$staging/$name" "$prefix/app/$name"; done
# 바이트코드 캐시는 내용 해시로 검사하게 미리 만든다(수정 시각·길이가 같은 변경도 놓치지 않음).
# 만들지 못하면 캐시를 쓰지 않는 실행기로 둔다(옛 캐시가 남아 있어도 쓰지 않음).
pyc_hash=0
if "$python_bin" -c 'import py_compile, sys; py_compile.compile(sys.argv[1], doraise=True, invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)' "$prefix/app/office.py" >/dev/null 2>&1; then pyc_hash=1; fi
# Replace baseline metadata without following old metadata symlinks.
rm -rf -- "$prefix/app/.office-orig"
mv -- "$staging/.office-orig" "$prefix/app/.office-orig"
mv -f -- "$staging/.office-manifest.json" "$prefix/app/.office-manifest.json"
swapping=0
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
  if ((pyc_hash)); then
    # office.py 를 직접 실행하면 매번 컴파일해 명령마다 0.1초가 더 걸린다. 같은 파일을 __main__ 으로 불러와 바이트코드 캐시(__pycache__)를 쓴다.
    # 캐시는 내용 해시로 검사해 파일이 바뀌면 다시 컴파일한다. 폴더에 쓸 수 없으면 파이썬이 조용히 매번 컴파일한다.
    printf '%s\n' 'fi' "loader='import sys, importlib.util as u; p = sys.argv[1]; sys.argv = [p, *sys.argv[2:]]; sys.path.insert(0, p.rsplit(\"/\", 1)[0]); s = u.spec_from_file_location(\"__main__\", p); m = u.module_from_spec(s); sys.modules[\"__main__\"] = m; s.loader.exec_module(m)'"
    printf 'exec "$python_bin" -c "$loader" "$install_root/app/office.py" "$@"\n'
  else
    printf 'fi\nexec "$python_bin" "$install_root/app/office.py" "$@"\n'
  fi
} > "$staging/office-launcher"
chmod 700 "$staging/office-launcher"
cp -- "$staging/office-launcher" "$staging/office-global"
if [[ ! -e $prefix/bin/office && ! -L $prefix/bin/office ]] \
   || cmp -s -- "$staging/office-launcher" "$prefix/bin/office" \
   || { [[ -n $old_office && -f $prefix/bin/office && ! -L $prefix/bin/office && $(file_sha "$prefix/bin/office") == "$old_office" ]]; }; then
  mv -f -- "$staging/office-launcher" "$prefix/bin/office"
else
  kept="$kept office"
  printf '주의: %s/bin/office는 수정 여부를 확인할 수 없어 그대로 둬요.\n' "$prefix"
fi
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
elif [[ -f $global && ! -L $global ]] && grep -q '^install_root=' "$global" && grep -q '/app/office.py' "$global" \
     && { cmp -s -- "$staging/office-global" "$global" || [[ -n $old_global && $(file_sha "$global") == "$old_global" ]]; }; then
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
    printf '새 터미널을 열고 office를 실행하세요.\n'
    if [[ $platform == Darwin ]]; then
      printf '명령을 찾지 못하면 ~/.zprofile에 아래 줄을 추가한 뒤 새 터미널을 여세요.\n'
      printf '%s\n' 'export PATH="$HOME/.local/bin:$PATH"'
    else
      printf '명령을 찾지 못하면 ~/.profile에서 ~/.local/bin을 PATH에 추가하는지 확인하세요.\n'
    fi
    printf '현재 터미널에서 바로 실행: %q\n' "$prefix/bin/office"
    ;;
esac

# 바로가기로 열어도 서버 종료 뒤 대화형 셸을 남겨요. 바로가기가 없는 맥에는 만들지 않아요.
if [[ $platform == Linux ]]; then
  {
    printf '# Office 콘솔을 시작해요. 서버가 끝나면 이 셸을 계속 사용하세요.\n'
    printf 'if [[ -f $HOME/.bashrc ]]; then . "$HOME/.bashrc"; fi\n'
    printf 'cd -- %q\n' "$workspace"
    printf '%q\n' "$prefix/bin/office"
  } > "$staging/office-console.bashrc"
  chmod 600 "$staging/office-console.bashrc"
  if [[ ! -e $prefix/bin/office-console.bashrc && ! -L $prefix/bin/office-console.bashrc ]] \
     || cmp -s -- "$staging/office-console.bashrc" "$prefix/bin/office-console.bashrc" \
     || { [[ -n $old_console && -f $prefix/bin/office-console.bashrc && ! -L $prefix/bin/office-console.bashrc && $(file_sha "$prefix/bin/office-console.bashrc") == "$old_console" ]]; }; then
    mv -f -- "$staging/office-console.bashrc" "$prefix/bin/office-console.bashrc"
  else
    kept="$kept console"
    printf '주의: %s/bin/office-console.bashrc는 수정 여부를 확인할 수 없어 그대로 둬요.\n' "$prefix"
  fi
fi

# 실행 파일 지문을 기록해 다음 설치 때 직접 고친 파일을 알아본다.
"$python_bin" - "$prefix" "$global" "$kept" <<'PYREC'
import hashlib, json, sys
from pathlib import Path
root, glob = Path(sys.argv[1]), Path(sys.argv[2])
kept = sys.argv[3].split()
mf = root / 'app' / '.office-manifest.json'
def dg(p):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else None
data = json.loads(mf.read_text())
rec = {'office': None if 'office' in kept else dg(root / 'bin' / 'office'),
       'console': None if 'console' in kept else dg(root / 'bin' / 'office-console.bashrc')}
if rec['office'] and glob.is_file() and not glob.is_symlink() and dg(glob) == rec['office']: rec['global'] = rec['office']
data['launchers'] = {k: v for k, v in rec.items() if v}
mf.write_text(json.dumps(data, ensure_ascii=False) + '\n')
PYREC

# WSL이면 윈도우 바탕화면·시작 메뉴에 'Office' 바로가기를 둔다. 누르면 office를 실행해 앱 창으로 열고,
# 이미 떠 있으면 같은 창을 다시 연다(주소 복사·일반 탭 없이). 실패해도 설치는 계속한다.
# 같은 이름의 바로가기가 이미 있으면(누가 만들었든) 건드리지 않고, 없을 때만 만든다. 아이콘도 없거나 지난 설치 그대로일 때만 바꾼다.
if (( shortcut )) && [[ $platform == Linux && -n ${WSL_DISTRO_NAME:-} ]] && command -v powershell.exe >/dev/null && command -v wslpath >/dev/null; then
  if [[ $prefix =~ ^[A-Za-z0-9._/-]+$ && $WSL_DISTRO_NAME =~ ^[A-Za-z0-9._-]+$ ]]; then
    cat > "$staging/office-shortcut.ps1" <<'PS1'
param([string]$Distro, [string]$ShellInit, [string]$Icon, [string]$PrevSha)
$ErrorActionPreference = 'Stop'
$dir = Join-Path $env:LOCALAPPDATA 'Office'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$ico = Join-Path $dir 'office.ico'
# 직접 바꾼 아이콘은 덮지 않는다. 없거나, 지난 설치가 둔 그대로일 때만 바꾼다.
$newSha = (Get-FileHash -LiteralPath $Icon -Algorithm SHA256).Hash.ToLower()
if (-not (Test-Path -LiteralPath $ico)) { Copy-Item -LiteralPath $Icon -Destination $ico }
else {
  $curSha = (Get-FileHash -LiteralPath $ico -Algorithm SHA256).Hash.ToLower()
  if ($curSha -ne $newSha -and $PrevSha -and $curSha -eq $PrevSha.ToLower()) { Copy-Item -LiteralPath $Icon -Destination $ico -Force }
}
$wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
$shell = New-Object -ComObject WScript.Shell
$made = @()
foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
  if (-not $folder) { continue }
  $path = Join-Path $folder 'Office.lnk'
  if (Test-Path -LiteralPath $path) {
    # 이미 있는 Office.lnk는 누가 만들었는지 알 수 없어 그대로 둔다.
    continue
  }
  $lnk = $shell.CreateShortcut($path)
  $lnk.TargetPath = $wsl
  $lnk.Arguments = "-d $Distro --cd ~ -e bash --rcfile $ShellInit -i"
  $lnk.IconLocation = $ico
  $lnk.Description = 'Office'
  $lnk.Save()
  $made += $path
}
# 같은 경로의 아이콘을 바꿔도 탐색기가 예전 그림을 보여 주지 않게 아이콘 캐시를 새로 읽혀요
try { & (Join-Path $env:SystemRoot 'System32\ie4uinit.exe') -show } catch { }
$made -join "`n"
PS1
    if made=$(timeout 30 powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$(wslpath -w "$staging/office-shortcut.ps1")" \
        "$WSL_DISTRO_NAME" "$prefix/bin/office-console.bashrc" "$(wslpath -w "$prefix/app/office.ico")" "$prev_ico_sha" 2>/dev/null | tr -d '\r') && [[ -n $made ]]; then
      printf '바로가기를 만들었어요. 바탕화면이나 시작 메뉴의 Office를 누르면 앱 창으로 열려요.\n'
    else
      printf '윈도우 바로가기는 만들지 못했어요. office 명령으로 실행하세요.\n'
    fi
  else
    printf '설치 경로에 특수 문자가 있어 윈도우 바로가기는 만들지 않았어요.\n'
  fi
fi

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
if [[ $platform == Linux ]]; then
  flock -u 9
  exec 9>&-
fi
printf '%s\n' "$local_summary"
if (( launch )); then
  printf '별도 작업 폴더에서 Office를 시작합니다.\n'
  "$prefix/bin/office"
else
  printf '실행: %q\n' "$prefix/bin/office"
fi
