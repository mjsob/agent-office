# Agent Office

Claude와 Codex가 한 팀으로 일하는 작업 공간이에요.
할 일을 보내면 둘이 나눠서 처리하고 결과를 알려 줘요.

## 시작하기 전에 확인하세요

- Windows라면 WSL이 필요해요. Linux에서도 쓸 수 있어요.
- Ubuntu 24.04 이상을 권장해요.
- 인터넷에 연결되어 있어야 해요.
- Claude 계정과 ChatGPT 계정이 있어야 해요.

WSL이 없다면 먼저 설치하세요. 설치 중에 관리자 권한이나 재부팅이 필요할 수 있어요.

## 설치하기

WSL 터미널에 아래 명령을 붙여 넣으세요.

```sh
curl -fsSL https://raw.githubusercontent.com/mjsob/agent-office/main/get.sh | bash -s -- --apply --install-system-deps
```

설치가 끝나면 Office가 바로 열려요.
로그인이 필요하면 터미널에 안내가 나와요. 안내에 따라 로그인하세요.

설치 전에 무엇이 바뀌는지 먼저 보고 싶다면 아래 명령을 쓰세요. 아무것도 설치하지 않아요.

```sh
curl -fsSL https://raw.githubusercontent.com/mjsob/agent-office/main/get.sh | bash
```

### 설치하면 이렇게 돼요

- Claude Code와 Codex CLI가 없으면 함께 설치돼요.
- 작업 폴더 `~/agent-office`가 만들어져요.
- 새 터미널에서 `office` 명령으로 실행할 수 있어요.
- Python이나 git이 없으면 함께 설치돼요. 이때 관리자 비밀번호를 물어볼 수 있어요.

## 실행하기

```sh
office
```

새 WSL 터미널을 열고 `office`를 입력하면 Office가 열려요.
`~/agent-office` 폴더에서 일을 시작해요.

다른 폴더에서 일하려면 폴더 경로를 함께 입력하세요.

```sh
office ~/내프로젝트
```

- 폴더가 없으면 열리지 않아요. `mkdir`로 먼저 만드세요.
- 대화 기록과 작업 파일은 폴더마다 따로 저장돼요.
- 설치한 버전은 `office --version`으로 확인할 수 있어요.

## 업데이트하기

설치할 때 쓴 명령을 다시 실행하세요.
최신 버전으로 바뀌어요. 작업 파일과 대화 기록은 그대로 남아요.
업데이트한 뒤에는 실행 중인 Office를 껐다 켜세요.

## 에이전트 권한

Claude와 Codex는 권한 확인 없이 실행돼요.
작업 폴더 밖의 파일도 읽고 고칠 수 있어요.

권한을 작업 폴더 안으로 제한하려면 이렇게 실행하세요.

```sh
OFFICE_SANDBOX=1 office
```

## 설치 옵션

`bash -s --` 뒤에 붙여서 써요.

| 옵션 | 설명 |
|---|---|
| `--apply` | 실제로 설치해요. 없으면 미리 보기만 해요. |
| `--install-system-deps` | Python·git이 없으면 함께 설치해요. |
| `--workspace 경로` | 작업 폴더를 바꿔요. `/`로 시작하는 전체 경로로 입력하세요. 기본값은 `~/agent-office`예요. |
| `--prefix 경로` | 프로그램 설치 폴더를 바꿔요. `/`로 시작하는 전체 경로로 입력하세요. 기본값은 `~/.local/share/office-app`이에요. |
| `--skip-login` | 로그인을 건너뛰어요. |
| `--device-auth` | 브라우저 대신 기기 코드로 Codex에 로그인해요. |
| `--no-launch` | 설치가 끝나도 Office를 열지 않아요. |

## 이미 쓰던 환경은 그대로 둬요

- 이미 설치된 Claude Code와 Codex CLI는 다시 설치하지 않아요.
- 이미 로그인되어 있으면 그 계정을 그대로 써요.
- 이미 있는 `office` 명령은 바꾸지 않아요.
- `.bashrc` 같은 설정 파일은 고치지 않아요.

## 문제가 생겼을 때

**`office` 명령을 찾을 수 없어요**
새 WSL 터미널을 열고 다시 입력하세요.
그래도 안 되면 `~/.local/share/office-app/bin/office`로 실행하세요.

**Codex 로그인 화면이 열리지 않아요**
설치 명령 끝에 `--device-auth`를 붙여 다시 실행하세요.

**Claude에 로그인되지 않아요**
터미널에서 `claude auth login`을 실행하세요.
로그인 상태는 `claude auth status`로 확인할 수 있어요.

**인증서 오류가 나요**
`SSL_CERT_FILE` 환경변수에 인증서 파일 경로를 넣고 다시 실행하세요.

**Python 버전이 낮다고 나와요**
Python 3.11 이상을 설치하세요.
Ubuntu를 새로 설치한다면 24.04 이상을 권장해요.

## 삭제하기

1. `~/.local/share/office-app` 폴더를 지우세요.
2. 설치할 때 만들어진 `~/.local/bin/office` 파일을 지우세요.

기본 작업 폴더 `~/agent-office`와 대화 기록은 남아요. 필요 없다면 직접 지우세요.
작업 폴더를 설치 폴더 안으로 바꿨다면 지우기 전에 백업하세요.
Claude Code, Codex CLI와 로그인 정보도 그대로 남아요.

## 더 알아보기

- [Claude Code 설치 안내](https://code.claude.com/docs/en/setup)
- [Codex CLI 안내](https://learn.chatgpt.com/docs/codex/cli)
