# Agent Office 설치

AI 에이전트가 함께 작업하는 Office를 설치합니다.
Claude Code·Codex CLI가 없으면 같이 설치하고, 로그인이 안 된 쪽만 로그인을 띄웁니다.

## 필요한 것

- 리눅스 또는 Windows의 WSL (Windows에서 바로는 안 돕니다)
- 파이썬 3.11 이상, git
- 인터넷 연결
- 로그인할 Claude 계정과 ChatGPT 계정

Windows라면 WSL부터 설치하세요. 관리자 권한과 재부팅이 필요할 수 있습니다.
Ubuntu 24.04 이상을 권장합니다. 그보다 오래된 Ubuntu는 기본 파이썬이 3.11 미만이라 설치기가 멈춥니다.

**설치할 PC에서만 `--apply`를 실행하세요.**
Claude Code·Codex CLI 공식 설치기는 그 PC 사용자 폴더(`~/.local/bin` 등)에 설치합니다. Office 설치 폴더처럼 따로 격리되지 않습니다.
지금 쓰는 WSL을 그대로 두고 싶다면 `--apply` 없이 미리 보기만 하세요.

## 먼저 미리 보기

```sh
bash install.sh
```

아무 옵션 없이 실행하면 **미리 보기만** 합니다.
무엇을 설치할지 출력만 하고 파일·네트워크·로그인은 건드리지 않습니다.

## 설치

```sh
bash install.sh --apply
```

설치기가 하는 일:

1. 파이썬·git이 없으면 설치 방법을 알려주고 멈춥니다.
2. Claude Code·Codex CLI가 없으면 공식 설치판으로 설치합니다. 있으면 그대로 씁니다.
   설치판은 파일로 먼저 받은 뒤 실행하므로, 다운로드가 중간에 끊기면 아무것도 실행하지 않습니다.
3. Office 파일을 설치 폴더에 복사하고 전용 실행 명령을 만듭니다.
4. 로그인 안 된 쪽만 로그인을 띄웁니다. 브라우저에서 직접 승인해야 합니다.
5. 끝나면 Office를 띄웁니다.

설치 폴더 기본값은 `~/.local/share/office-app` 입니다.
기본 작업 폴더는 `~/agent-office`입니다. WSL에서는 `/home/사용자/agent-office`에 해당합니다.
실행 명령은 `~/.local/share/office-app/bin/office`입니다. `~/.local/bin/office`가 없으면 설치기가 만들어 `office`로도 실행할 수 있습니다. 기존 명령이 있으면 그대로 둡니다.

## 실행

```sh
office                 # ~/agent-office에서 시작
office ~/내프로젝트     # 다른 폴더에서 시작
```

- `office` 명령이 PATH에 없거나 기존 명령이 다른 프로그램을 가리키면 `~/.local/share/office-app/bin/office`를 사용하세요.
- 다른 폴더에서 열려면 그 폴더가 이미 있어야 합니다. 새 폴더면 `mkdir`로 먼저 만드세요.
- 폴더를 주거나 아무것도 안 주고 실행하면 터미널의 에이전트 환경을 초기화해 해당 작업 폴더를 엽니다. 저장된 대화와 작업 파일은 유지됩니다.

## 옵션

| 옵션 | 하는 일 |
|---|---|
| `--apply` | 실제로 설치 (없으면 미리 보기만) |
| `--prefix 경로` | 설치 폴더 바꾸기 (절대 경로) |
| `--workspace 경로` | 기본 작업 폴더 바꾸기 (기본 `~/agent-office`, 절대 경로) |
| `--install-system-deps` | 파이썬·git이 없을 때 `apt-get`으로 설치 (관리자 권한 필요) |
| `--skip-login` | 로그인 건너뛰기 |
| `--device-auth` | 브라우저가 안 열리는 환경에서 Codex 기기 코드 로그인 |
| `--no-launch` | 설치 후 Office 띄우지 않기 |

## 이미 쓰던 환경을 건드리지 않음

- 기존 `~/.local/bin/office` 명령은 그대로 둡니다. 없을 때만 새로 만들며, `.bashrc` 같은 설정 파일은 고치지 않습니다.
- 이미 설치된 Claude·Codex는 다시 설치하지 않고, 이미 로그인돼 있으면 그 로그인을 그대로 씁니다.
- 시스템 패키지는 `--install-system-deps`를 줄 때만 설치합니다.
- 설치 폴더가 이미 있는데 설치기가 만든 폴더가 아니면 설치를 거부합니다.
- 여러 번 실행해도 결과가 같습니다.
- Office 작업 폴더는 기본 `~/agent-office`로, 설치 폴더와 따로 둡니다. `--workspace`로 바꿀 수 있습니다.

## 에이전트 권한

- Office는 기본적으로 에이전트에게 전체 권한을 줍니다. Claude는 권한 확인 없이 실행하고, Codex는 샌드박스 없이 실행합니다.
- `office reserve`·`office write`도 작업 폴더 밖 파일을 받습니다. 인증서·설정·Office 기록 폴더는 계속 막습니다.
- 작업 폴더 안으로 제한하려면 `OFFICE_SANDBOX=1 ~/.local/share/office-app/bin/office`처럼 실행하세요.

## 로그인이 안 될 때

- 브라우저가 안 열리면 `--device-auth`를 붙여 다시 실행하세요.
  계정 보안 설정에서 기기 코드 로그인을 허용해야 할 수 있습니다.
- Claude는 `claude auth login`으로 로그인합니다. 확인은 `claude auth status`로 합니다.
- 로그인은 대화형 터미널에서만 진행합니다. 자동 설치만 하려면 `--skip-login --no-launch`를 붙이세요.

공식 안내: [Claude Code 설치](https://code.claude.com/docs/en/setup) · [Claude CLI 명령](https://code.claude.com/docs/en/cli-reference) · [Codex CLI](https://learn.chatgpt.com/docs/codex/cli) · [Codex 로그인](https://learn.chatgpt.com/docs/auth)

## 지우기

기본 작업 폴더 `~/agent-office`는 설치 폴더 밖에 있으므로 설치 폴더(기본 `~/.local/share/office-app`)를 지워도 작업 파일과 `.office` 대화 기록은 남습니다. `--workspace`로 설치 폴더 안을 지정했다면 제거 전에 작업 파일과 대화 기록을 다른 위치에 백업하세요.
설치기가 만든 `~/.local/bin/office` 명령도 제거하세요. 기존에 있던 명령은 유지하세요.
Claude Code·Codex CLI와 로그인 정보는 따로 남습니다.

## 공개 배포 주소

저장소: `mjsob/agent-office` (AI 에이전트 협업 오피스)
현재 지원하는 에이전트는 Claude Code와 Codex CLI입니다.
저장소와 릴리스 게시 후 다음 명령을 사용할 수 있습니다.

```sh
# 미리 보기: 묶음을 받아 임시 폴더에서 설치 계획만 출력
curl -fsSL https://raw.githubusercontent.com/mjsob/agent-office/main/get.sh | bash

# 설치할 PC에서: 시스템 의존 설치 허용 → CLI 설치 → 사용자 로그인 → 실행
curl -fsSL https://raw.githubusercontent.com/mjsob/agent-office/main/get.sh | bash -s -- --apply --install-system-deps
```

공개판은 인증서가 필요할 때 `SSL_CERT_FILE` 또는 `REQUESTS_CA_BUNDLE` 환경변수를 사용합니다.
특정 사용자 폴더의 인증서를 자동으로 찾지 않습니다.
