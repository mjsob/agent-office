#!/usr/bin/env bash
# Office 한 줄 설치기. 깃허브 최신 릴리스에서 설치 묶음을 받아 해시를 확인한 뒤 install.sh를 실행한다.
#   미리 보기:  curl -fsSL <이 파일 주소> | bash
#   설치:       curl -fsSL <이 파일 주소> | bash -s -- --apply
#   이전 버전:  curl -fsSL <이 파일 주소> | bash -s -- --apply --version 0.2.5
# 뒤에 붙인 옵션은 install.sh에 그대로 전달된다.
set -Eeuo pipefail

REPO="${OFFICE_REPO:-mjsob/agent-office}"   # 공개 저장소
ASSET="office-installer.tar.gz"             # 릴리스에 붙이는 고정 이름 (latest 주소가 늘 최신을 가리키게)
fail() { printf '설치 중단: %s\n' "$*" >&2; exit 1; }

# --version X.Y.Z 로 이전 버전을 받을 수 있다. 나머지 옵션은 install.sh로 넘긴다.
version="" args=()
while (($#)); do
  case $1 in
    --version) (($# >= 2)) || fail '--version 뒤에 버전이 필요합니다'; version=${2#v}; shift 2;;
    *) args+=("$1"); shift;;
  esac
done
[[ -z $version || $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "버전 형식이 X.Y.Z가 아닙니다: $version"
if [[ -n $version ]]; then
  BASE="https://github.com/$REPO/releases/download/v$version"
else
  BASE="https://github.com/$REPO/releases/latest/download"
fi
for cmd in curl tar sha256sum mktemp; do command -v "$cmd" >/dev/null || fail "$cmd 명령이 필요합니다"; done

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
printf 'Office 설치 묶음을 받습니다: %s\n' "$REPO"
fetch() { curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --connect-timeout 15 --max-time 180 "$1" -o "$2"; }
fetch "$BASE/$ASSET" "$work/$ASSET" || fail "설치 묶음을 받지 못했습니다${version:+ (버전 $version이 있는지 확인하세요)}"
fetch "$BASE/$ASSET.sha256" "$work/$ASSET.sha256" || fail '해시 파일을 받지 못했습니다'

# 해시 파일의 파일명과 상관없이 첫 칸(해시값)만 비교
want=$(awk '{print $1; exit}' "$work/$ASSET.sha256")
got=$(sha256sum "$work/$ASSET" | awk '{print $1}')
[[ -n $want && $want == "$got" ]] || fail '받은 파일의 해시가 맞지 않습니다'

tar -xzf "$work/$ASSET" -C "$work"
[[ -f "$work/office-dist/install.sh" ]] || fail '묶음 안에 office-dist/install.sh가 없습니다'

# curl | bash 로 실행되면 표준입력이 이 스크립트라서, 로그인 단계가 터미널을 쓰도록 /dev/tty를 연결
if [[ -t 0 ]] || ! { : </dev/tty; } 2>/dev/null; then
  bash "$work/office-dist/install.sh" "${args[@]}"
else
  bash "$work/office-dist/install.sh" "${args[@]}" </dev/tty
fi
