#!/usr/bin/env bash
# bash publish.sh [--dry-run] patch|minor "릴리스 제목" 노트파일
# --dry-run은 임시 사본에서 검사해요. 버전·커밋·공개 릴리스·설치본은 바꾸지 않아요.
set -Eeuo pipefail
umask 077
fail() { printf '배포를 중단했어요: %s\n' "$*" >&2; exit 1; }
usage() { printf '사용법: bash publish.sh [--dry-run] patch|minor "릴리스 제목" 노트파일\n'; }
dry=0
args=()
for arg in "$@"; do
  if [[ $arg == --dry-run ]]; then dry=1; else args+=("$arg"); fi
done
[[ ${#args[@]} == 3 ]] || { usage >&2; exit 2; }
kind=${args[0]} title=${args[1]} notes=${args[2]}
[[ $kind == patch || $kind == minor ]] || fail '버전 종류는 patch 또는 minor로 입력하세요.'
[[ -n ${title//[[:space:]]/} ]] || fail '릴리스 제목을 입력하세요.'
[[ -s $notes && -f $notes ]] || fail '내용이 있는 노트 파일을 지정하세요.'
notes=$(realpath -- "$notes")
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
root=$(pwd -P)
for command in git bash python3 curl tar sha256sum mktemp grep flock; do
  command -v "$command" >/dev/null || fail "$command 명령이 필요해요."
done
[[ $(git rev-parse --show-toplevel) == "$root" ]] || fail '저장소 루트에서 실행하세요.'
[[ $(git branch --show-current) == main ]] || fail 'main 브랜치에서 실행하세요.'
# 같은 저장소에서 동시에 배포하지 않아요. 잠금 파일은 Git 내부에 둬요.
exec 9>"$(git rev-parse --git-path office-publish.lock)"
flock -n 9 || fail '다른 배포가 진행 중이에요.'
[[ -z $(git status --porcelain --untracked-files=all) ]] || fail '변경 파일을 먼저 커밋하세요.'
current=$(tr -d '[:space:]' < payload/VERSION)
next=$(python3 - "$current" "$kind" <<'PY'
import re,sys
if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)',sys.argv[1]):
    sys.exit('버전은 X.Y.Z 형식으로 입력하세요.')
a,b,c=map(int,sys.argv[1].split('.'))
print(f'{a}.{b}.{c+1}' if sys.argv[2]=='patch' else f'{a}.{b+1}.0')
PY
) || fail '현재 버전을 확인하세요.'
tag=v$next
work=$(mktemp -d)
release_id='' published=0 auth=''
api_base=https://api.github.com/repos/mjsob/agent-office
cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n $release_id && $published == 0 && -n $auth ]]; then
    # 공개 요청의 응답만 끊겼을 수도 있어요. 아직 초안일 때만 삭제해요.
    if curl --config "$auth" --fail --silent --show-error --max-time 30 "$api_base/releases/$release_id" -o "$work/cleanup.json" &&
       python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("draft") is True else 1)' "$work/cleanup.json"; then
      if ! curl --config "$auth" --fail --silent --show-error --max-time 30 -X DELETE "$api_base/releases/$release_id" -o /dev/null; then
        printf '임시 릴리스를 지우지 못했어요. GitHub에서 초안 릴리스를 확인하세요.\n' >&2
      fi
    else
      printf '릴리스가 초안인지 확인하지 못해 삭제하지 않았어요. GitHub에서 상태를 확인하세요.\n' >&2
    fi
  fi
  rm -rf -- "$work"
  if (( status )); then
    printf '배포를 끝내지 못했어요. 이미 만든 커밋과 푸시 결과는 유지해요.\n' >&2
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# 원고가 검사 중 바뀌어도 검사를 통과한 내용만 배포해요.
cp -- "$notes" "$work/notes.md"
printf '%s\n' "$title" > "$work/title.txt"
mkdir "$work/source" "$work/unpacked"
git archive HEAD | tar -x -C "$work/source"
printf '%s\n' "$next" > "$work/source/payload/VERSION"
(cd "$work/source" && bash release.sh)
tar -xzf "$work/source/dist/office-installer.tar.gz" -C "$work/unpacked"
# 금지 표현 목록은 공개 저장소에 두지 않고 이 PC에만 둬요.
forbidden=${OFFICE_FORBIDDEN_FILE:-$HOME/.config/office-publish/forbidden.txt}
[[ -s $forbidden ]] || fail "공개 전 검사 목록이 없어요: $forbidden"
pattern=$(tr -d '\n' < "$forbidden")
# 일치 내용은 출력하지 않아요. 공개하면 안 되는 정보가 로그에 남지 않게 해요.
if grep -rqiE -- "$pattern" "$work/unpacked" "$work/notes.md" "$work/title.txt"; then
  fail '배포 파일이나 노트에 공개하면 안 되는 표현이 있어요.'
else
  result=$?
  [[ $result == 1 ]] || fail '공개 전 검사를 실행하지 못했어요.'
fi
(cd "$work/unpacked/office-dist/payload" && sha256sum --check --strict SHA256SUMS)
(cd "$work/source/dist" && sha256sum --check --strict office-installer.tar.gz.sha256)
printf '검사를 마쳤어요: %s → %s\n' "$current" "$next"
if (( dry )); then
  printf '미리 보기를 마쳤어요. 업로드와 설치는 실행하지 않았어요.\n'
  exit 0
fi
remote=$(git remote get-url origin)
[[ $remote == https://github.com/mjsob/agent-office || $remote == https://github.com/mjsob/agent-office.git || $remote == git@github.com:mjsob/agent-office.git ]] || fail 'origin이 mjsob/agent-office 저장소인지 확인하세요.'
# 토큰은 명령 인자·출력에 넣지 않고 권한이 제한된 curl 설정으로 전달해요.
auth=$work/curl.conf
python3 - "$HOME/.git-credentials" "$auth" <<'PY'
import json,os,sys
from pathlib import Path
from urllib.parse import urlsplit,unquote
try:
    token=None
    for line in Path(sys.argv[1]).read_text().splitlines():
        url=urlsplit(line.strip())
        if url.hostname=='github.com':
            token=unquote(url.password or url.username or '')
            if token:break
    if not token or any(c in token for c in '\r\n'):raise ValueError()
    Path(sys.argv[2]).write_text('header = '+json.dumps('Authorization: Bearer '+token)+'\nheader = "Accept: application/vnd.github+json"\nheader = "X-GitHub-Api-Version: 2022-11-28"\n')
    os.chmod(sys.argv[2],0o600)
except (OSError,ValueError):
    sys.exit('GitHub 인증 정보를 읽지 못했어요.')
PY
api() {
  local method=$1 url=$2 data=${3:-} type=${4:-application/json}
  local options=(--config "$auth" --silent --show-error --fail-with-body --connect-timeout 15 --max-time 180 -X "$method" -H "Content-Type: $type" -o "$work/response.json")
  if [[ -n $data ]]; then options+=(--data-binary "@$data"); fi
  if ! curl "${options[@]}" "$url"; then
    printf 'GitHub 요청을 처리하지 못했어요. 배포 상태를 확인하세요.\n' >&2
    return 1
  fi
}
# 기존 태그를 덮어쓰지 않아요. 네트워크 조회 실패도 배포 중단으로 처리해요.
git ls-remote --tags origin "refs/tags/$tag" > "$work/tags"
[[ ! -s $work/tags ]] || fail '같은 버전의 태그가 이미 있어요.'
api GET https://api.github.com/user
# 검사한 묶음과 같은 버전·해시 목록으로 커밋해요.
cp -- "$work/source/payload/VERSION" payload/VERSION
cp -- "$work/source/payload/SHA256SUMS" payload/SHA256SUMS
mkdir -p dist
cp -- "$work/source/dist/office-installer.tar.gz" "$work/source/dist/office-installer.tar.gz.sha256" dist/
git add -- payload/VERSION payload/SHA256SUMS
git commit -m "Office v$next" -m 'Co-Authored-By: Claude <noreply@anthropic.com>
Co-Authored-By: Codex <noreply@openai.com>'
commit=$(git rev-parse HEAD)
git push origin HEAD:main
python3 - "$tag" "$title" "$work/notes.md" "$commit" "$work/create.json" <<'PY'
import json,sys
from pathlib import Path
tag,title,notes,commit,out=sys.argv[1:]
Path(out).write_text(json.dumps(dict(tag_name=tag,target_commitish=commit,name=f"Office {tag}",body=Path(notes).read_text(),draft=True,prerelease=False)))
PY
api POST "$api_base/releases" "$work/create.json"
release_id=$(python3 - "$work/response.json" <<'PY'
import json,sys
r=json.load(open(sys.argv[1]));value=r.get('id')
if type(value) is not int or not r.get('draft'):sys.exit('초안 릴리스를 확인하지 못했어요.')
print(value)
PY
)
for asset in office-installer.tar.gz office-installer.tar.gz.sha256; do
  api POST "https://uploads.github.com/repos/mjsob/agent-office/releases/$release_id/assets?name=$asset" "$work/source/dist/$asset" application/octet-stream
done
printf '{"draft":false,"make_latest":"true"}\n' > "$work/publish.json"
api PATCH "$api_base/releases/$release_id" "$work/publish.json"
published=1
api GET "$api_base/releases/latest"
python3 - "$work/response.json" "$tag" <<'PY'
import json,sys
r=json.load(open(sys.argv[1]))
assets={a['name']:a for a in r.get('assets',[])}
expected={'office-installer.tar.gz','office-installer.tar.gz.sha256'}
if r.get('tag_name')!=sys.argv[2] or not expected.issubset(assets) or any(assets[n].get('size',0)<=0 for n in expected):
    sys.exit('공개 버전과 설치 파일을 확인하지 못했어요.')
PY
# 설치는 서버를 열지 않아요. 현재 대화를 중단하지 않게 해요.
python3 - "$work/unpacked/office-dist/install.sh" <<'PY'
import os,subprocess,sys
env={k:v for k,v in os.environ.items() if not k.startswith('OFFICE_')}
subprocess.run(['bash',sys.argv[1],'--apply','--skip-login','--no-launch'],env=env,check=True)
PY
printf '배포와 설치를 마쳤어요: %s\n' "$tag"
