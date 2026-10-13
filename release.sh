#!/usr/bin/env bash
# Office 배포 묶음 빌드. payload/VERSION 버전으로 dist/office-installer.tar.gz 와 .sha256 을 만든다.
#   bash release.sh          묶음만 만듦 (업로드 없음)
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
fail() { printf '빌드 중단: %s\n' "$*" >&2; exit 1; }
files=(office.py office.html office.ico office-icon.png VERSION)
version=$(tr -d '[:space:]' < payload/VERSION)
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "payload/VERSION 형식이 X.Y.Z가 아닙니다: $version"
for name in "${files[@]}"; do [[ -f payload/$name ]] || fail "payload/$name 없음"; done
bash -n install.sh && bash -n get.sh

# 해시 목록은 배포 파일 기준으로 매번 새로 만든다
(cd payload && sha256sum -- "${files[@]}") > payload/SHA256SUMS

stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
mkdir -p "$stage/office-dist/payload" dist
cp -- install.sh README.md "$stage/office-dist/"
for name in "${files[@]}" SHA256SUMS; do cp -- "payload/$name" "$stage/office-dist/payload/"; done
out=dist/office-installer.tar.gz
tar --owner=0 --group=0 --numeric-owner -czf "$out" -C "$stage" office-dist
(cd dist && sha256sum office-installer.tar.gz > office-installer.tar.gz.sha256)
printf 'Office %s 묶음 완료: %s\n' "$version" "$out"
cat dist/office-installer.tar.gz.sha256
