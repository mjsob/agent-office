#!/usr/bin/env python3
"""office — Claude·Codex 2인 협업 오피스 (GUI)

  office [폴더]        GUI 창 띄움 (폴더 기본=현재, git 저장소 권장). 서버는 이 콘솔에서 돌고, 콘솔을 닫거나 Ctrl+C면 종료
                       이미 다른 콘솔에서 돌고 있으면 창만 다시 엶
  office stop [폴더]   다른 콘솔에서 돌고 있는 Office 종료
  office board         작업판 텍스트로 보기
  office log [-n N] [-f]  대화 최근 N건(기본 20) 출력. -f면 tail -f처럼 계속 따라감 (Ctrl+C 종료)
  office --version     설치된 Office 버전 출력

GUI에서 '작업 요청'으로 보내면: 리더가 쪼개서 분배 → 둘이 메시지로 조율하며 처리 → 리더가 검수 후 보고.
각자에게 직접 지시(@claude / @codex)도 가능.

에이전트용 (OFFICE_AS 환경변수로 누가 하는지 자동 지정):
  office claim ID | done ID "요약" | wait ID "기다리는 것" | handoff ID --to X "사유" | say --to X "말" | add "할일" [--to X] | finish "보고" | title "대화 요약 제목"
"""
import math, tomllib, hashlib, ssl
import argparse, queue, copy, datetime, fcntl, json, os, re, shlex, shutil, signal, socket, subprocess, sys, selectors, threading, time, tempfile, unicodedata, uuid
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qs

AGENTS = ("claude", "codex")
ROTATE_CTX = int(os.environ.get("OFFICE_ROTATE_CTX") or 150_000)   # 새 요청 시작 때 이보다 긴 세션은 새로 시작
IDLE_RECHECK = 10      # 초: 할 일 없이 쉬는 동안 기록이 그대로여도 장애 전환·멈춤 점검은 이 간격으로 다시 해요
POLL = 1              # 초: 작업 배정·사용자 메시지를 빠르게 반영
MODEL_FALLBACKS = {"codex": ["gpt-6-sol", "gpt-6-luna"], "claude": ["sonnet", "opus"]}
MAX_CONT = 2          # 하던 일 남았는데 조용할 때 "이어서 해" 재촉 횟수
MAX_CHATTER = 4       # 작업 변화 없이 둘이 말만 주고받는 연속 횟수 → 넘으면 사용자 호출
AUTO_RETRY_DELAYS = (15, 60, 180)   # 초: 연결 끊김·서버 오류 같은 일시 오류는 이 간격으로 자동 재시도 후에야 멈춤
STALL_NOTICE = 180    # 초: 열린 요청이 이 시간 동안 아무 진척 없이 멈춰 있으면 사용자에게 알리고 리더에게 점검 차례를 줌
EDGE = "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
CHROME = "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"   # 있으면 우선 (Edge는 텍스트 선택 시 미니 메뉴가 뜸)
LIMITS = {}         # 구독 사용 한도 + 관측시각 at(epoch)
LIMITS_LOCK = threading.RLock()
HERE = Path(__file__).resolve().parent

ROOT = Path(os.environ.get("OFFICE_DIR") or os.getcwd()).resolve()
D = ROOT / ".office"


class UserError(Exception):
    pass


class StorageError(UserError):
    pass


def ui_notice(title, detail):
    """가변 원문에 구분자가 있어도 화면의 제목·상세 경계는 하나만 둔다."""
    return str(title)[:25] + " — " + str(detail).replace(" — ", ": ")


SERVER_LOG_LOCK = threading.Lock()
STORAGE_WARNINGS = {}
MAX_BODY = 256 * 1024
MAX_TEXT = 20000
AGENT_QUIET_NOTICE = 25 * 60
AGENT_QUIET_TIMEOUT = 30 * 60
AGENT_SLOW_TOOL = 2 * 60   # 도구 하나가 이보다 오래 돌면 화면에 표시하고 로그에 남김 (멈춘 것처럼 보이는 구간 추적)
AGENT_TOOL_TIMEOUT = 3 * 60 * 60   # 명령·서브 에이전트가 도는 중엔 출력이 없어도 이 시간까지 기다림 (긴 빌드·테스트)
RESULT_GRACE = 3      # 초: Claude 최종 답(result) 뒤 프로그램이 스스로 끝나길 기다리는 시간. 넘으면 차례를 끝낸다
LEAD_BUSY_TAKEOVER = int(os.environ.get("OFFICE_LEAD_BUSY") or 120)   # 초: 담당이 다른 차례를 이만큼 붙잡고 있으면 쉬는 쪽이 새 요청 계획·미착수 작업을 맡는다
WAIT_DEADLINE = 15 * 60   # 초: office wait 기본 기한(감시 프로세스 없을 때), 백그라운드 로그가 이만큼 안 바뀌어도 담당을 한 번 깨운다


def server_log(event, error=None):
    """알려진 단계와 오류 종류만 기록. 요청/인증/예외 본문은 기록하지 않음."""
    detail = f" {type(error).__name__}" if error is not None else ""
    if isinstance(error, OSError):
        detail += f" errno={error.errno}"
    if event == "http.get" and isinstance(error, UserError):
        public = str(error).split(":", 1)[0]
        allowed = {
            "대화를 찾을 수 없음 — 대화가 없습니다. 대화 목록에서 다시 선택하세요.",
            "대화 선택 확인 — 올바른 대화를 다시 선택하세요.",
            "이전 작업 정리 중 — 이전 작업을 정리 중입니다. 잠시 후 다시 시도하세요.",
            "작업 프로세스 정리 중 — 작업 프로세스를 아직 정리 중입니다. 잠시 후 다시 시도하세요.",
            "Git 응답 시간 초과 — git 작업 응답 시간이 초과됐습니다. 저장소 상태를 확인하세요.",
            "작업 폴더를 만들지 못했습니다",
            "이전 Office 실행 중 — 이전 Office가 아직 실행 중입니다. 종료한 뒤 재개하세요.",
            "이전 실행 확인 실패 — 이전 작업 프로세스를 안전하게 확인하지 못했습니다.",
        }
        allowed |= {message.split(" — ", 1)[-1] for message in allowed}
        detail += " message=" + (public if public in allowed else "대화 상태 처리 오류(상세 내용 제외)")
    try:
        D.mkdir(parents=True, exist_ok=True)
        with SERVER_LOG_LOCK, open(D / "server.log", "a") as log:
            log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {event}{detail}\n")
    except OSError:
        pass


def valid_sid(sid):
    return isinstance(sid, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", sid))


def meta_valid(data):
    return (isinstance(data, dict) and isinstance(data.get("sessions"), list)
            and all(isinstance(x, dict) and valid_sid(x.get("id")) and isinstance(x.get("title"), str)
                    and isinstance(x.get("deleted"), bool) for x in data["sessions"]))


CHAT_OPTION_VALUES = {"mode": ("job", "claude", "codex"), "lead": AGENTS,
                      "verification": ("quick", "standard", "thorough")}
CHAT_OPTION_DEFAULTS = {"mode": "job", "lead": "claude", "verification": "standard"}


def chat_options_view(value):
    """옛 대화의 미설정 상태와 저장된 선택을 구분하고 잘못된 값은 기본값으로 보여줘요."""
    if not isinstance(value, dict):
        return None
    return {key: value.get(key) if value.get(key) in allowed else CHAT_OPTION_DEFAULTS[key]
            for key, allowed in CHAT_OPTION_VALUES.items()}


def state_valid(data):
    if not isinstance(data, dict):
        return False
    if type(data.get("seq", 0)) is not int or data.get("seq", 0) < 0:
        return False
    for key in ("jobs", "tasks", "msgs"):
        items = data.get(key, [])
        if not isinstance(items, list):
            return False
        for item in items:   # 대화가 길면 msgs 가 수천 개라 매 항목 검사를 가볍게 유지해요
            if not isinstance(item, dict):
                return False
            get = item.get
            if type(get("id")) is not int or item["id"] < 1:
                return False
            if key == "msgs":
                if not (isinstance(get("ts"), str) and isinstance(get("from"), str) and isinstance(get("to"), str) and isinstance(get("text"), str)):
                    return False
            elif key == "jobs":
                if get("status") not in ("planning", "working", "review", "done") or get("lead") not in AGENTS or not isinstance(get("text"), str):
                    return False
            elif get("status") not in ("todo", "doing", "waiting", "done") or get("owner") not in (*AGENTS, None) or not isinstance(get("title"), str):
                return False
    return True


_GOOD_FILES = {}   # 경로 -> 검사를 통과한 파일 식별값. 저장할 때 직전 정상본을 또 해석하지 않으려고 둬요


def file_key(path):
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size, st.st_ino
    except OSError:
        return None


def read_json_file(path, default, validator=None, required=False):
    check = validator or (lambda value: isinstance(value, dict))
    if not path.exists():
        return copy.deepcopy(default)
    try:
        key = file_key(path)   # 읽기 전에 구해 둬요: 읽는 사이 바뀌었으면 다음 저장 때 다시 검사해요
        data = json.loads(path.read_text())
        if not check(data):
            raise ValueError("schema")
        _GOOD_FILES[str(path)] = key
        return data
    except (ValueError, UnicodeError) as error:
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            data = json.loads(backup.read_text())
            if not check(data):
                raise ValueError("backup schema")
        except (OSError, ValueError, UnicodeError):
            STORAGE_WARNINGS[str(path)] = f"기록 파일 손상 — {path.name}: 원본을 보존했습니다. 백업을 확인하세요."
            server_log("storage.invalid", error)
            if required:
                raise StorageError(STORAGE_WARNINGS[str(path)]) from None
            return copy.deepcopy(default)
        STORAGE_WARNINGS[str(path)] = f"백업으로 기록 복구 — {path.name}: 마지막 정상 백업으로 읽고 있습니다. 원본은 보존됩니다."
        server_log("storage.backup", error)
        return data
    except OSError as error:
        server_log("storage.read", error)
        raise StorageError(f"파일 읽기 실패 — {path.name}을 읽지 못했습니다. 디스크 용량과 폴더 권한을 확인하세요.") from None


def atomic_json(path, value, validator=None):
    check = validator or (lambda data: isinstance(data, dict))
    if not check(value):
        raise StorageError(f"저장 내용 검증 실패 — {path.name} 저장 내용을 검증하지 못했습니다. 원본은 유지됩니다.")
    temporary = None
    try:
        payload = json.dumps(value, ensure_ascii=False, indent=1, allow_nan=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if _GOOD_FILES.get(str(path)) == file_key(path):   # 우리가 방금 읽었거나 쓴 그대로면 다시 해석하지 않아도 정상본이에요
                good = True
            else:
                try:
                    old = json.loads(path.read_text())
                    good = check(old)
                except (ValueError, UnicodeError):
                    good = False
            target = path.with_suffix(path.suffix + ".bak") if good else path.with_name(path.name + ".corrupt-" + uuid.uuid4().hex[:8])
            shutil.copy2(path, target)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            f.write(payload); f.flush()   # fsync 는 하지 않음: 디스크가 바쁠 때 잠금을 쥔 채 수 초씩 멈추던 원인. 직전 정상본(.bak)으로 복구함
        temporary.replace(path)
        temporary = None
        _GOOD_FILES[str(path)] = file_key(path)
    except (OSError, ValueError) as error:
        server_log("storage.write", error)
        raise StorageError(f"파일 저장 실패 — {path.name}을 저장하지 못했습니다. 원본은 유지됩니다. 디스크 용량과 폴더 권한을 확인하세요.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                server_log("storage.temporary", error)



def configured_model(name):
    try:
        if name == "codex":
            path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
            return tomllib.loads(path.read_text()).get("model", "gpt-6.1-sol")
        path = Path.home() / ".claude" / "settings.json"
        return os.environ.get("ANTHROPIC_MODEL") or json.loads(path.read_text()).get("model", "opus")
    except (OSError, ValueError):
        return "gpt-6.1-sol" if name == "codex" else "opus"


MODEL_LEVELS = {   # 차례 무게별 모델·추론 수준. 빈 값은 각 CLI 기본 설정 그대로. ~/.config/office/models.json 으로 덮어씀
    "light": {"claude": {"model": "sonnet", "effort": "low"}, "codex": {"effort": "low"}},
    "normal": {"claude": {}, "codex": {}},
    "heavy": {"claude": {"effort": "high"}, "codex": {"effort": "high"}},
}
TURN_LEVELS = {"plan": "heavy", "review": "light", "chat": "light", "reply": "normal", "work": "normal", "continue": "normal"}
LEVELS = tuple(MODEL_LEVELS)


def model_levels():
    """(차례 무게별 표 또는 None=끔, 차례 종류→무게). 설정 파일: {"enabled": false} 로 끄거나 levels·turns 일부만 덮어씀"""
    table, turns = copy.deepcopy(MODEL_LEVELS), dict(TURN_LEVELS)
    if os.environ.get("OFFICE_MODEL_LEVELS") == "0":
        return None, turns
    try:
        custom = json.loads(Path(os.environ.get("OFFICE_MODELS") or Path.home() / ".config" / "office" / "models.json").read_text())
    except (OSError, ValueError):
        custom = {}
    if not isinstance(custom, dict):
        return table, turns
    if custom.get("enabled") is False:
        return None, turns
    for level, agents in (custom.get("levels") or {}).items():
        if level in table and isinstance(agents, dict):
            for n, value in agents.items():
                if n in AGENTS and isinstance(value, dict):
                    table[level][n] = {k: str(x) for k, x in value.items() if k in ("model", "effort") and x}
    for kind, level in (custom.get("turns") or {}).items():
        if kind in turns and level in table:
            turns[kind] = level
    return table, turns


def turn_choice(name, desc, task_level=None):
    """이번 차례 무게(light|normal|heavy)와 실행에 붙일 모델·effort. 작업에 --level 이 있으면 그 작업 차례는 그 무게로"""
    table, turns = model_levels()
    level = task_level if task_level in LEVELS and desc in ("work", "continue") else turns.get(desc, "normal")
    return level, ({} if table is None else dict(table[level].get(name) or {}))


def fallback_model(name, primary):
    return next((x for x in MODEL_FALLBACKS[name] if x != primary and (name != "claude" or x not in primary)), None)


def valid_limits(record, now=None):
    if not isinstance(record, dict):
        return None
    now = time.time() if now is None else now
    out = {}
    for key, window in record.items():
        if key == "at" or not isinstance(window, dict):
            continue
        used, resets = window.get("used"), window.get("resets")
        if type(used) not in (int, float) or not math.isfinite(used):
            continue
        if isinstance(resets, str):
            try:
                resets = datetime.datetime.fromisoformat(resets.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
        if isinstance(resets, (int, float)) and resets > 1e11:
            resets /= 1000
        if resets is not None and (type(resets) not in (int, float) or not math.isfinite(resets) or resets <= now):
            continue   # 초기화 이후 실제 사용량은 모르므로 0%로 단정하지 않음
        out[key] = dict(window, used=max(0, min(100, used)), resets=resets)
    if not out:
        return None
    if isinstance(record.get("at"), (int, float)):
        out["at"] = record["at"]
    if record.get("source") in ("direct", "stream", "rollout"):
        out["source"] = record["source"]
    return out


def limits_for(name):
    with LIMITS_LOCK:
        return valid_limits(LIMITS.get(name))


def load_limits():
    try:
        saved = read_json_file(D / "limits.json", {})
    except (OSError, ValueError):
        return
    if not isinstance(saved, dict):
        return
    with LIMITS_LOCK:
        for name in AGENTS:
            record = valid_limits(saved.get(name))
            if record:
                LIMITS[name] = record


def record_limits(name, windows, at=None, source=None):
    record = valid_limits(dict(windows, at=time.time() if at is None else at, source=source))
    if not record:
        return
    with LIMITS_LOCK:
        if record["at"] < (LIMITS.get(name) or {}).get("at", 0):
            return   # 과거 로그 재조회로 최신 관측을 덮지 않음
        LIMITS[name] = record
        try:
            D.mkdir(parents=True, exist_ok=True)
            with open(D / "limits.lock", "a") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                path = D / "limits.json"
                saved = read_json_file(path, {})
                if not isinstance(saved, dict):
                    return
                previous = saved.get(name)
                previous_at = previous.get("at", 0) if isinstance(previous, dict) else 0
                if isinstance(previous_at, (int, float)) and record["at"] < previous_at:
                    return
                saved[name] = record
                atomic_json(path, saved)
        except (OSError, ValueError, StorageError) as error:
            server_log("limits.cache", error)

def set_root(path):
    global ROOT, D
    ROOT = Path(path).resolve(); D = ROOT / ".office"


# ── 대화(세션) 목록 (.office/meta.json) ─────────────────────
# 대화마다 .office/sessions/<id>/ 에 state.json · 에이전트 세션 id · 로그를 따로 둠
# 삭제는 deleted 플래그(휴지통)만 세우고, 영구 삭제는 휴지통에서 따로 해야 지워짐
def sdir(sid):
    if not valid_sid(sid):
        raise UserError("대화 선택 확인 — 올바른 대화를 다시 선택하세요.")
    return D / "sessions" / sid


@contextmanager
def meta(write=False):
    D.mkdir(exist_ok=True)
    exc = ROOT / ".git" / "info" / "exclude"
    try:
        if exc.parent.is_dir() and ".office/" not in (exc.read_text() if exc.exists() else ""):
            with exc.open("a") as f:
                f.write(".office/\n")
    except OSError as error:
        server_log("git.exclude", error)
    with open(D / "meta.lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        f = D / "meta.json"
        m = read_json_file(f, {"current": None, "sessions": []}, meta_valid, required=True)
        yield m
        if write:
            atomic_json(f, m, meta_valid)


def last_notice(msgs):
    """사용자가 확인할 소식(보고·사용자에게 온 메시지·요청 완료)의 마지막 메시지 번호."""
    last = 0
    for x in msgs:
        mid = x.get("id")
        if type(mid) is not int:
            continue
        if (x.get("to") == "user" and x.get("from") != "user" and x.get("kind") in ("msg", "report")) \
                or (x.get("kind") == "event" and re.match(r"요청 \d+ 완료", str(x.get("text", "")))):
            last = max(last, mid)
    return last


def new_session(m, title="새 대화"):
    sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    sdir(sid).mkdir(parents=True)
    with state(True, sid) as saved:
        saved["chat_options"] = dict(CHAT_OPTION_DEFAULTS)
    m["sessions"].append({"id": sid, "title": title, "created": time.time(), "updated": time.time(), "deleted": False})
    m["current"] = sid
    return sid


META_QUEUE = queue.Queue()
CURRENT_HINT = {"sid": None, "at": 0.0}


def meta_later(change):
    """화면 클릭에 바로 답해야 하는 저장(대화 전환·읽음 표시)은 뒤에서 한 줄로 저장해요.
    디스크가 바쁠 때 저장이 몇 초 걸려도 클릭이 같이 멈추지 않아요."""
    META_QUEUE.put(change)
    if not getattr(meta_later, "started", False):
        meta_later.started = True
        threading.Thread(target=meta_writer, daemon=True).start()


def meta_writer():
    while True:
        change = META_QUEUE.get()
        try:
            with meta(True) as m:
                change(m)
                while True:   # 밀린 변경은 한 번에 저장
                    try:
                        META_QUEUE.get_nowait()(m)
                    except queue.Empty:
                        break
        except Exception as error:
            server_log("meta.later", error)


def current_sid():
    if os.environ.get("OFFICE_SESSION"):
        return os.environ["OFFICE_SESSION"]
    if CURRENT_HINT["sid"] and time.time() - CURRENT_HINT["at"] < 30:   # 방금 클릭한 대화(저장이 끝나기 전)
        return CURRENT_HINT["sid"]
    try:   # 고칠 게 없으면 잠금·저장 없이 읽기만 해요(조회마다 meta.json 을 다시 쓰지 않게)
        m = meta_view()
        if any(x["id"] == m.get("current") and not x["deleted"] for x in m["sessions"]):
            return m["current"]
    except (StorageError, KeyError, TypeError):
        pass
    with meta(True) as m:
        live = [x for x in m["sessions"] if not x["deleted"]]
        if not m["sessions"] and (D / "state.json").exists():      # 세션 기능 전 기록 → '이전 대화'로 이관
            sid = new_session(m, "이전 대화")
            for f in ("state.json", "claude.sid", "codex.sid", "claude.log", "codex.log"):
                if (D / f).exists():
                    (D / f).replace(sdir(sid) / f)
        elif not any(x["id"] == m["current"] for x in live):
            if live:
                m["current"] = max(live, key=lambda x: x["updated"])["id"]
            else:
                new_session(m)
        return m["current"]


def touch(sid, text=None, request=False):
    """대화 갱신 시각 기록. 제목은 첫 입력으로 임시 지정하고, 이후 에이전트가 대화 전체를 요약 (직접 바꾼 이름은 유지)"""
    with meta(True) as m:
        for x in m["sessions"]:
            if x["id"] == sid:
                x["updated"] = time.time()
                if text and text.strip() and not x.get("renamed") and x["title"] == "새 대화":
                    x["title"] = text.strip().splitlines()[0][:40]


# ── 대화별 상태 저장소 (sessions/<id>/state.json, flock) ─────
@contextmanager
def state(write=False, sid=None):
    d = sdir(sid or current_sid())
    d.mkdir(parents=True, exist_ok=True)
    STATE = d / "state.json"
    with open(d / "lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        s = read_json_file(STATE, {}, state_valid, required=True)
        for k in ("jobs", "tasks", "msgs"):
            s.setdefault(k, [])
        s.setdefault("seq", 0)
        yield s
        if write:
            atomic_json(STATE, s, state_valid)


def state_view(sid):
    """화면용 읽기. 저장은 임시 파일을 바꿔치기하므로 잠금 없이 읽어도 항상 온전해요.
    다른 명령이 잠금을 오래 쥐고 있어도 화면 조회가 같이 멈추지 않게 해요."""
    s = read_json_file(sdir(sid) / "state.json", {}, state_valid, required=True)
    for k in ("jobs", "tasks", "msgs"):
        s.setdefault(k, [])
    s.setdefault("seq", 0)
    return s


_STATE_CACHE = {}   # 경로 -> (파일 식별값, 읽은 상태, 마지막 소식 번호). 파일이 그대로면 매초 다시 읽고 검사하지 않음
_STATE_CACHE_LOCK = threading.Lock()


def state_view_cached(sid):
    """state_view 와 같지만 파일이 안 바뀌었으면 읽어 둔 값을 그대로 줘요. 돌려준 값은 고치지 말고 읽기만 해요."""
    path = sdir(sid) / "state.json"
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        key = None
    with _STATE_CACHE_LOCK:
        hit = _STATE_CACHE.get(str(path))
    if key is not None and hit and hit[0] == key:
        return hit[1], hit[2], key
    s = state_view(sid)
    notice = last_notice(s.get("msgs", []))
    if key is not None and not STORAGE_WARNINGS.get(str(path)):   # 백업으로 읽었거나 손상이면 담아 두지 않고 매번 확인
        with _STATE_CACHE_LOCK:
            _STATE_CACHE[str(path)] = (key, s, notice)
            if len(_STATE_CACHE) > 64:
                _STATE_CACHE.pop(next(iter(_STATE_CACHE)))
        return s, notice, key
    return s, notice, None


def meta_view():
    return read_json_file(D / "meta.json", {"current": None, "sessions": []}, meta_valid, required=True)


def reply_target(s, actor, explicit=None):
    """가장 이른 미응답 사용자 메시지. 방송 답변 여부는 담당별로 계산."""
    candidates = [m for m in s["msgs"] if m.get("from") == "user"
                  and m.get("to") in (actor, "all") and m.get("kind") == "msg"
                  and m.get("job") is None]
    if explicit is not None:
        if type(explicit) is not int or not any(m["id"] == explicit and m.get("from") == "user" and m.get("to") in (actor, "all") and m.get("kind") in ("msg", "request") for m in s["msgs"]):
            raise UserError("답장 대상 확인 — 해당 담당에게 온 사용자 메시지 ID를 지정하세요.")
        return explicit
    legacy_end = max((m["id"] for m in s["msgs"] if m.get("from") == actor
                      and m.get("job") is None and m.get("re") is None
                      and (m.get("to") == "user" and m.get("kind") in ("msg", "report")
                           or m.get("to") == "log" and m.get("kind") == "turn")), default=0)
    answered = {m.get("re") for m in s["msgs"] if m.get("from") == actor}
    return next((m["id"] for m in candidates if m["id"] > legacy_end
                 and m["id"] not in answered), None)


def post(s, frm, to, text, kind="msg", job: int | None = None, reply_to=None):
    if frm in AGENTS and (to == "user" and kind == "msg"
                          or kind == "turn" and job is None):
        if job is None or reply_to is not None:
            reply_to = reply_target(s, frm, reply_to)
    # ts 는 오늘 기준 시:분:초, at 은 날짜까지 알 수 있는 시각(화면에서 오늘이 아니면 날짜로 표시)
    row = {"id": len(s["msgs"]) + 1, "ts": time.strftime("%H:%M:%S"), "at": round(time.time(), 3),
           "from": frm, "to": to, "text": text, "kind": kind, "job": job}
    if reply_to is not None:
        row["re"] = reply_to
    s["msgs"].append(row)
    if job is not None and frm in AGENTS and to == "user" and kind in ("progress", "msg", "report"):
        request = next((j for j in s["jobs"] if j["id"] == job), None)
        if request:
            request["last_user_notice_at"] = row["at"]
            request.setdefault("first_user_notice_at", row["at"])
            if request.get("started_at"):
                request.setdefault("first_user_notice_seconds", round(max(0, row["at"] - request["started_at"]), 3))


def event(s, text, actor="system", job: int | None = None):
    post(s, actor, "all", text, "event", job=job)


def disp(name):
    return {"claude": "Claude", "codex": "Codex", "user": "사용자"}.get(name, name)


def find(s, tid):
    for t in s["tasks"]:
        if t["id"] == tid:
            return t
    sys.exit(f"작업 #{tid} 없음")


def open_job(s):
    jid = os.environ.get("OFFICE_JOB")
    if jid:
        return next((j for j in s["jobs"] if j["id"] == int(jid)), None)
    if os.environ.get("OFFICE_DIRECT") == "1":
        return None
    return next((j for j in reversed(s["jobs"]) if j["status"] != "done"), None)


def message_job(s):
    """에이전트 차례의 요청 번호를 우선하고, 없으면 진행 중 요청을 사용."""
    jid = os.environ.get("OFFICE_JOB")
    if jid:
        return int(jid)
    reply = os.environ.get("OFFICE_REPLY_TO")
    if reply and reply.isdecimal():
        source = next((m for m in s["msgs"] if m["id"] == int(reply)), None)
        if source is not None:
            return source.get("job")
    if os.environ.get("OFFICE_DIRECT") == "1":
        return None
    j = open_job(s)
    return j["id"] if j else None



def request_history(jobs):
    """최근 6건 원문, 앞선 14건 한 줄 요약, 더 오래된 요청은 범위와 건수."""
    lines = []
    if len(jobs) > 20:
        older = jobs[:-20]
        lines.append(f"- 이전 요청 {older[0]['id']}~{older[-1]['id']}: {len(older)}건 (과거 기록)")
    kept = jobs[-20:]
    cut = max(0, len(kept) - 6)
    for j in kept[:cut]:
        first = report_brief(j.get("text", ""))
        answer = report_brief(j.get("brief") or j.get("summary") or "")
        if not answer:
            answer = "취소됨" if j.get("cancelled") else "완료" if j.get("status") == "done" else "진행 중"
        lines.append(f"- 요청 {j['id']}: {first} → {answer}")
    for j in kept[cut:]:
        lines.append(f"- 요청 {j['id']}: {j.get('text', '')[:200]}")
    return "\n".join(lines)

def recent_results(jobs, count=6, cap=700):
    """새 세션은 이전 대화를 기억 못 하니, 최근 끝난 요청의 사용자 원문과 최종 보고를 넘겨 이어받게 한다"""
    rows = []
    for j in [j for j in jobs if j.get("status") == "done"][-count:]:
        answer = "취소됨" if j.get("cancelled") else (j.get("summary") or "").strip()
        if len(answer) > cap:
            answer = answer[:cap] + " …(생략)"
        rows.append(f"[요청 {j['id']}] 사용자: {j.get('text', '')[:300]}\n→ 최종 보고: {answer or '보고 없음'}")
    return "\n\n".join(rows)

def missed_text(msgs, me, limit=6000):
    """지난 차례 이후 내가 직접 받지 못한 대화(사용자↔동료 1:1, 동료의 최종 답변)를 최신 위주로 묶음"""
    rows = [m for m in msgs if m["from"] not in (me, "system") and m.get("kind") in ("msg", "turn")
            and (m.get("kind") == "turn" or m["to"] not in (me, "all"))]
    out, used = [], 0
    for m in reversed(rows):
        text = m["text"] if len(m["text"]) <= 1500 else m["text"][:1500] + " …(생략)"
        line = f"[{m['ts']}] {m['from']}{' 답변' if m.get('kind') == 'turn' else ' → ' + m['to']}: {text}"
        if used + len(line) > limit:
            out.append(f"(이전 {len(rows) - len(out)}건 생략)")
            break
        out.append(line); used += len(line)
    return "\n".join(reversed(out))


def board_text(s, n_msgs=15):
    icon = {"todo": "⬜", "doing": "🔨", "waiting": "⏳", "done": "✅"}
    out = ["== 요청(job) =="]
    out += [f"요청 {j['id']} [{j['status']}] 리더={j['lead']} : {j['text']}" for j in s["jobs"] if j["status"] != "done"] or ["(없음)"]
    out.append("== 작업판 ==")
    out += [f"{icon[t['status']]} #{t['id']} (요청 {t.get('job')}) [{t['owner'] or '미배정'}] {t['title']}"
            + (f" — 기다리는 중: {t['wait_reason']}" if t["status"] == "waiting" and t.get("wait_reason") else "")
            + (f" — 사용자 확인 필요: {pending_user_request(t)['text']}" if pending_user_request(t) else "")
            + (f" — 진행: {progress_text(t['progress'])}" if t["status"] != "done" and t.get("progress") else "")
            + (f" — {t['note']}" if t.get("note") else "") for t in s["tasks"] if t["status"] != "done" or
            any(j["id"] == t.get("job") and j["status"] != "done" for j in s["jobs"])] or ["(비어있음)"]
    out.append("== 최근 대화 ==")
    out += [f"[{m['ts']}] {m['from']}{'' if m['to'] in ('all', 'log') else ' → ' + m['to']}: {m['text'][:150]}"
            for m in s["msgs"][-n_msgs:] if m.get("kind") != "turn"]
    return "\n".join(out)


# ── CLI 명령 (사람·에이전트 공용) ───────────────────────────
def me():
    return os.environ.get("OFFICE_AS", "user")


def file_digest(path):
    try:
        with path.open("rb") as source:
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(1024*1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except FileNotFoundError:
        return None


def guarded_path(value, allow_dir=False):
    sid = current_sid()
    base = wdir(sid).resolve()
    raw = Path(value).expanduser()
    path = (raw if raw.is_absolute() else base/raw).resolve()
    # 작업 폴더 밖 파일은 OFFICE_SANDBOX=1(제한 모드)일 때만 막는다
    if os.environ.get("OFFICE_SANDBOX") == "1" and not path.is_relative_to(base):
        raise UserError("작업 폴더 안의 파일만 예약·저장할 수 있습니다.")
    protected = [Path.home()/name for name in ("ref", "certs", ".codex", ".aws", ".agents")]
    if not WT:
        protected += [D.resolve(), (Path.home()/".office").resolve()]
    if any(path.is_relative_to(folder.resolve()) for folder in protected):
        raise UserError("참고 자료·인증서·설정·Office 기록은 수정 대상으로 예약할 수 없습니다.")
    if path.exists() and not path.is_file() and not allow_dir:
        raise UserError("일반 파일만 예약할 수 있습니다.")
    return path


def reservations_valid(value):
    if not isinstance(value, dict):
        return False
    for key, record in value.items():
        if not isinstance(key, str) or not isinstance(record, dict):
            return False
        if record.get("path") != key or not Path(key).is_absolute() or "\x00" in key:
            return False
        if record.get("owner") not in (*AGENTS, "user") or not valid_sid(record.get("session")):
            return False
        if record.get("job") is not None and (type(record["job"]) is not int or record["job"] < 1):
            return False
        if not isinstance(record.get("root"), str) or not Path(record["root"]).is_absolute():
            return False
        if type(record.get("since")) not in (int, float) or not math.isfinite(record["since"]):
            return False
        if record.get("version") is not None and (not isinstance(record["version"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["version"])):
            return False
    return True


def reservation_notice(waiter, path, reservation=None):
    """Write wait/release notices after releasing the global reservation lock."""
    folder = Path(waiter["root"])/".office"/"sessions"/waiter["session"]
    state_path = folder/"state.json"
    if not state_path.exists():
        return
    with (folder/"lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read_json_file(state_path, {}, state_valid, required=True)
        pending = [m for m in data["msgs"] if m.get("file_wait") == str(path)
                   and m.get("job") == waiter.get("job") and not m.get("resolved")]
        if reservation is None:
            if not pending:
                return
            for m in pending:
                m["resolved"] = True
            text = f"파일 예약 풀림 — {Path(path).name} 반영 가능"
        else:
            if pending:
                return
            metadata = read_json_file(Path(reservation["root"])/".office"/"meta.json", {}, meta_valid)
            title = next((x["title"] for x in metadata.get("sessions", [])
                          if x["id"] == reservation["session"]), reservation["session"])
            request = f"요청 {reservation['job']}" if reservation.get("job") else "직접 지시"
            text = f"파일 대기 중 — {Path(path).name} 다른 대화 「{title}」 {request}에서 수정 중이라 반영을 기다립니다."
        post(data, "system", "user", text, "event", job=waiter.get("job"))
        if reservation is not None:
            data["msgs"][-1]["file_wait"] = str(path)
        atomic_json(state_path, data, state_valid)


def notify_released_reservation(path, reservation):
    for waiter in reservation.get("waiters", []):
        try:
            reservation_notice(waiter, path)
        except (OSError, UserError) as error:
            server_log("reservation.release.notice", error)


@contextmanager
def reservation_store(write=False):
    folder = Path.home()/".office"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder/"reservations.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = folder/"reservations.json"
        data = read_json_file(path, {}, reservations_valid, required=path.exists())
        original = json.dumps(data, sort_keys=True)
        released = []
        for key, value in list(data.items()):
            record_path = Path(value.get("root", str(ROOT)))/".office"/"sessions"/value["session"]/"state.json"
            try:
                record = read_json_file(record_path, {}, state_valid, required=record_path.exists())
                closed = next((job for job in record.get("jobs", []) if job["id"] == value["job"]), None)
                process = value.get("process")
                stale = bool(process and process_record_valid(process) and process_identity(process["pid"]) != {k: process[k] for k in ("start", "uid", "command")})
                if stale or closed and (closed["status"] == "done" or closed.get("cancelled")):
                    released.append((key, data.pop(key)))
            except (OSError, UserError):
                pass
        yield data
        if write and json.dumps(data, sort_keys=True) != original:
            atomic_json(path, data, reservations_valid)
    if write:
        for key, record in released:
            notify_released_reservation(key, record)


def reservation_owner(allow_closed=False):
    with state() as record:
        job = message_job(record)
        current = next((j for j in record["jobs"] if j["id"] == job), None)
        if current and not allow_closed and (current["status"] == "done" or current.get("cancelled")):
            raise UserError("종료된 요청에는 파일을 저장할 수 없습니다. 새 작업에서 예약하세요.")
    return {"owner": me(), "session": current_sid(), "job": job, "root": str(ROOT)}


def owns_reservation(record, identity):
    return all(record.get(key) == identity.get(key) for key in ("owner","session","job","root"))


def cmd_reserve(a):
    path, identity = guarded_path(a.path), reservation_owner()
    conflict = None
    with reservation_store(True) as records:
        old = records.get(str(path))
        if old and not owns_reservation(old, identity):
            waiters = old.setdefault("waiters", [])
            if not any(all(w.get(k) == identity.get(k) for k in ("root", "session", "job")) for w in waiters):
                waiters.append(identity)
            conflict = dict(old)
        elif not old:
            process = read_json_file(sdir(identity["session"])/f"{identity['owner']}.process.json", {}, process_record_valid)
            if process and process_identity(process["pid"]) != {k: process[k] for k in ("start", "uid", "command")}:
                process = None
            records[str(path)] = dict(identity, path=str(path), since=time.time(), version=file_digest(path), process=process or None)
    if conflict:
        reservation_notice(identity, path, conflict)
        raise UserError(f"파일 예약 중 — {disp(conflict.get('owner'))}, 대화 {conflict['session']}, 요청 {conflict.get('job') or '직접 지시'}에서 수정 중입니다. {path.name}")
    print(f"파일 예약함: {path}")


def cmd_release(a):
    path, identity = guarded_path(a.path, allow_dir=True), reservation_owner(allow_closed=True)   # 예약 뒤 폴더가 생겨도 풀 수 있게
    with reservation_store(True) as records:
        old = records.get(str(path))
        if old and not owns_reservation(old, identity):
            raise UserError("다른 담당자가 예약한 파일은 해제할 수 없습니다.")
        released = records.pop(str(path), None)
    if released:
        notify_released_reservation(path, released)
    print(f"파일 예약 해제: {path}")


def release_reservations(session, job, owner):
    released = []
    with reservation_store(True) as records:
        for key, value in list(records.items()):
            if value.get("session") == session and value.get("job") == job and value.get("owner") == owner and value.get("root") == str(ROOT):
                released.append((key, records.pop(key)))
    for key, value in released:
        notify_released_reservation(key, value)



def cmd_write(a):
    path, identity = guarded_path(a.path), reservation_owner()
    source = Path(a.source).expanduser().resolve()
    if source == path:
        raise UserError("원본을 직접 수정하지 말고 별도 임시 원고를 지정하세요.")
    content = source.read_bytes()
    saved = False
    try:
        with reservation_store(True) as records:
            old = records.get(str(path))
            if not old or not owns_reservation(old, identity):
                raise UserError("파일 예약 필요 — 먼저 office reserve로 파일을 예약하세요.")
            if file_digest(path) != old["version"]:
                raise UserError("원본 변경 감지 — 다른 변경이 있어 저장하지 않았습니다. 원본을 다시 읽고 원고를 조정한 뒤 예약을 갱신하세요.")
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
                    temporary = Path(target.name)
                    target.write(content); target.flush(); os.fsync(target.fileno())
                    os.fchmod(target.fileno(), mode)
                if file_digest(path) != old["version"]:
                    raise UserError("원본 변경 감지 — 저장 직전에 다른 변경이 생겨 저장하지 않았습니다.")
                os.replace(temporary, path)
                saved = True
                old["version"] = hashlib.sha256(content).hexdigest()
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
    except StorageError:
        if saved:
            raise UserError("파일은 저장됐지만 예약 기록 갱신에 실패했습니다. 원본을 확인한 뒤 예약을 다시 잡으세요.") from None
        raise
    try:
        with state(True) as record:
            record["work_revision"] = record.get("work_revision", 0) + 1
    except (OSError, StorageError) as error:
        server_log("write.progress", error)
    print(f"안전 저장 완료: {path}")


def cmd_add(a):
    with state(True) as s:
        j = open_job(s)
        if j and j.get("cancelled"):
            sys.exit("사용자가 취소한 요청에는 작업을 추가할 수 없습니다. 새 요청으로 진행하세요.")
        normalized = " ".join(a.text.split()).casefold()
        existing = next((task for task in s["tasks"] if task.get("job") == (j and j["id"]) and task["status"] != "done" and " ".join(task["title"].split()).casefold() == normalized), None)
        if existing:
            return print(f"#{existing['id']} 기존 작업 유지 (중복 등록 생략)")
        s["seq"] += 1
        s["tasks"].append({"id": s["seq"], "job": j and j["id"], "title": a.text, "owner": a.to,
                           "status": "todo", "note": "", "created_at": time.time()})
        if getattr(a, "level", None):
            s["tasks"][-1]["level"] = a.level
        if j:
            j.setdefault("started_at", time.time())
            if j["status"] in ("review", "done"):
                j["rework_count"] = j.get("rework_count", 0) + 1
        if j and j["status"] in ("planning", "review", "done"):
            if j["status"] == "done":
                event(s, f"요청 {j['id']} 후속 작업으로 다시 진행", me(), job=j["id"])
            j["status"] = "working"
            j.pop("finished_at", None)
        event(s, f"작업 #{s['seq']} 추가: {a.text}" + (f" (담당 {disp(a.to)})" if a.to else ""), me(), job=message_job(s))
        print(f"#{s['seq']} 추가됨")


def cmd_claim(a):
    with state(True) as s:
        t = find(s, a.id)
        if t["owner"] not in (None, me()) or t["status"] == "done":
            sys.exit(f"#{a.id} 는 이미 {t['owner']} 담당/{t['status']} — 못 가져감")
        if pending_user_request(t):
            raise UserError("사용자 확인 대기 — 받은 자료나 답을 확인한 뒤 resolve-user로 해결하세요.")
        resumed = t["status"] == "waiting"
        keep = live_bg_watch(t)
        for key in WAIT_FIELDS:
            t.pop(key, None)
        t.update(keep)   # 아직 도는 백그라운드는 claim 뒤에도 계속 감시한다
        t.update(owner=me(), status="doing", claimed_at=t.get("claimed_at") if resumed and t.get("claimed_at") else time.time())
        event(s, f"작업 #{a.id} " + ("다시 시작" if resumed else "시작"), me(), job=message_job(s))
        print(f"#{a.id} 맡음")


BG_FIELDS = ("wait_pid", "wait_start", "wait_log", "wait_rc", "wait_cmd")
WAIT_FIELDS = ("wait_reason", "waiting_at", "wait_pid", "wait_start", "wait_log", "wait_rc", "wait_cmd", "wait_deadline",
               "wait_notified", "wait_overdue", "wait_stale")


def wait_probe(t):
    """기다림 작업의 감시 대상 상태: alive(True/False/None=감시 대상 없음), log_at(로그 마지막 수정 epoch)"""
    alive = None
    if t.get("wait_pid"):
        ident = process_identity(t["wait_pid"])
        alive = bool(ident) and (not t.get("wait_start") or ident["start"] == t["wait_start"])
        if alive:
            try:   # 끝났는데 부모가 회수 안 한 좀비는 죽은 것으로 본다
                alive = (Path("/proc") / str(t["wait_pid"]) / "stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
            except (OSError, IndexError):
                alive = False
    try:
        log_at = Path(t["wait_log"]).stat().st_mtime if t.get("wait_log") else None
    except OSError:
        log_at = None
    return alive, log_at


def task_wait_status(t, now=None):
    """차례 대기와 실제 백그라운드 실행을 나눠요. PID 생존보다 명시한 대기 상태를 우선해요."""
    now = time.time() if now is None else now
    alive, log_at = wait_probe(t)
    user_request = pending_user_request(t, now)
    if user_request:
        return {"wait_alive": alive, "wait_log_at": log_at, "wait_kind": "user", "wait_detail": user_request["text"]}
    progress = t.get("progress") or {}
    detail = progress.get("text") or t.get("wait_reason") or "다음 차례를 기다려요."
    if alive is False:
        try:
            rc = int(Path(t["wait_rc"]).read_text().strip()) if t.get("wait_rc") else None
        except (OSError, ValueError):
            rc = None
        detail = ("백그라운드 작업이 끝나 결과 확인을 기다려요." if rc == 0 else
                  f"백그라운드 작업이 실패했어요. 종료 코드는 {rc}예요." if rc is not None else
                  "백그라운드 작업이 멈춰 확인을 기다려요.")
        kind = "waiting"
    elif progress.get("resume_at") and progress["resume_at"] > now:
        kind = "waiting"
    elif progress.get("state") == "waiting":
        kind = "waiting"
    else:
        kind = "working" if alive else "waiting"
    return {"wait_alive": alive, "wait_log_at": log_at, "wait_kind": kind, "wait_detail": detail}


def job_display_status(job, tasks, workers=None, now=None):
    """저장한 작업 단계와 현재 실행 상태를 구분해요. 작업 이력은 바꾸지 않아요."""
    pending = [t for t in tasks if t.get("job") == job["id"] and t.get("status") != "done"]
    if job.get("cancelled") or job["status"] == "done" and not pending:
        return "done", ""
    active = [w for w in (workers or {}).values() if w.get("status") == "working"
              and not w.get("error")
              and (w.get("doing") or {}).get("job") == job["id"]]
    if active:
        return job["status"] if job["status"] in ("planning", "review") else "working", ""
    waits = [t for t in pending if t.get("status") == "waiting"]
    states = [task_wait_status(t, now) for t in waits]
    if any(t["wait_kind"] == "working" for t in states):
        return "working", next(t["wait_detail"] for t in states if t["wait_kind"] == "working")
    reasons = list(dict.fromkeys(t["wait_detail"] for t in states if t["wait_detail"]))
    if pending and len(waits) == len(pending):
        return "waiting", " · ".join(reasons)
    # 아직 담당 차례가 오지 않았거나, 다른 작업을 처리하고 있어요.
    return "waiting", " · ".join(reasons) or "담당자가 시작할 차례를 기다려요."


def session_display_status(record, workers=None, now=None):
    counts = dict.fromkeys(("working", "waiting", "planning", "review"), 0)
    reasons = []
    for job in record.get("jobs", []):
        kind, detail = job_display_status(job, record.get("tasks", []), workers, now)
        if kind in counts:
            counts[kind] += 1
        if detail and detail not in reasons:
            reasons.append(detail)
    kind = next((k for k in ("working", "review", "planning", "waiting") if counts[k]), "done")
    return {"open": sum(counts.values()), "status_counts": counts, "status_kind": kind,
            "status_reason": " · ".join(reasons)}


def live_bg_watch(t):
    """작업에 걸린 백그라운드 프로세스가 아직 살아 있으면 그 감시 필드를 돌려준다(다시 wait·claim 해도 감시가 안 풀리게)"""
    if not t.get("wait_pid") or not wait_probe(t)[0]:
        return {}
    return {k: t[k] for k in BG_FIELDS if t.get(k) is not None}


def mark_waiting(a, reason, **watch):
    with state(True) as s:
        t = find(s, a.id)
        if t["status"] == "done":
            sys.exit(f"#{a.id} 는 이미 끝난 작업입니다.")
        if t["owner"] not in (None, me()):
            sys.exit(f"#{a.id} 는 {t['owner']} 담당 — 담당자만 기다림으로 바꿀 수 있습니다.")
        watch = {k: v for k, v in watch.items() if v is not None}
        if not watch.get("wait_pid"):
            watch = {**live_bg_watch(t), **watch}
        for key in WAIT_FIELDS:
            t.pop(key, None)
        minutes = getattr(a, "minutes", None)
        deadline = time.time() + minutes * 60 if minutes else (None if watch.get("wait_pid") else time.time() + WAIT_DEADLINE)
        t.update(owner=me(), status="waiting", wait_reason=reason, waiting_at=time.time(), **watch)
        if deadline:
            t["wait_deadline"] = deadline
        t.setdefault("claimed_at", time.time())
        event(s, f"작업 #{a.id} 기다리는 중 · {reason}", me(), job=message_job(s))
        return t.get("job")


def cmd_wait(a):
    """백그라운드 작업·외부 결과를 기다리는 동안 작업을 열어 둔 채 재촉·진행 멈춤 점검에서 뺀다.
    --pid·--log 를 주면 서버가 그 프로세스·로그를 지켜보다 죽거나 멈추면 담당을 깨운다. 기한(--minutes, 기본 15분)이 지나도 깨운다"""
    reason = " ".join((a.text or "").split())[:200]
    if not reason:
        raise UserError("입력 내용 확인 — 무엇을 기다리는지 적어 주세요.")
    pid = getattr(a, "pid", None)
    ident = process_identity(pid) if pid else None
    if pid and not ident:
        raise UserError(f"프로세스 확인 — pid {pid} 가 이미 없습니다. 끝났는지 확인하고 office done 하거나 office bg 로 다시 띄우세요.")
    log = str(Path(a.log).expanduser().resolve()) if getattr(a, "log", None) else None
    mark_waiting(a, reason, wait_pid=pid, wait_start=ident and ident["start"], wait_log=log)
    print(f"#{a.id} 기다림" + (f" · pid {pid} 감시" if pid else "") + (f" · 로그 {log}" if log else ""))


def pending_user_request(t, now=None):
    request = t.get("user_request") or {}
    if t.get("status") == "done" or request.get("status") not in ("pending", "expired"):
        return None
    request = dict(request)
    if request.get("expires_at") and request["expires_at"] <= (time.time() if now is None else now):
        request["status"] = "expired"
    return request


def cmd_need_user(a):
    text = a.text.strip()
    if not text or len(text) > 2000:
        raise UserError("필요한 행동 확인 — 필요한 자료나 행동과 제출 방법을 2,000자 안으로 적으세요.")
    expires = parse_resume_at(getattr(a, "expires_at", None))
    with state(True) as record:
        task = find(record, a.id)
        if task["status"] == "done" or task.get("owner") not in (None, me()):
            raise UserError("담당 작업 확인 — 끝나지 않은 본인 작업에만 요청을 남길 수 있어요.")
        old = pending_user_request(task)
        request = {"status": "pending", "text": text, "created_at": old.get("created_at") if old else time.time(),
                   "expires_at": expires, "resolved_at": None, "resolution": None, "reply_to": None}
        task["user_request"] = request
        task.update(owner=me(), status="waiting", waiting_at=time.time(), wait_reason=text[:200])
        task.pop("wait_deadline", None)
        task.pop("user_expired_notified", None)
        if not old or old.get("text") != text or old.get("expires_at") != expires:
            post(record, me(), "user", "사용자 확인 필요 — " + text, "progress", job=task.get("job"))
    print(f"#{a.id} 사용자 확인 대기")


def cmd_resolve_user(a):
    if not a.text.strip():
        raise UserError("확인 결과 필요 — 자료나 답을 확인한 결과를 적으세요.")
    with state(True) as record:
        task = find(record, a.id)
        if task.get("owner") != me() or not pending_user_request(task):
            raise UserError("사용자 요청 확인 — 본인 작업의 미해결 요청을 선택하세요.")
        reply = getattr(a, "re", None)
        if reply is not None and not any(m["id"] == reply and m["from"] == "user" for m in record["msgs"]):
            raise UserError("받은 메시지 확인 — 자료나 답을 보낸 사용자 메시지 번호를 적으세요.")
        task["user_request"].update(status="resolved", resolved_at=time.time(), resolution=a.text.strip(), reply_to=reply)
        task.update(status="doing", claimed_at=time.time())
        for key in ("wait_reason", "wait_deadline", "wait_overdue", "wait_stale"):
            task.pop(key, None)
        post(record, me(), "user", "사용자 확인 해결 — " + a.text.strip(), "progress", job=task.get("job"))
    print(f"#{a.id} 사용자 요청 해결·작업 재개")


def progress_remaining(p):
    """남은 시간(초). 측정 시작(since) 뒤 처리 속도로 계산하고, 1분 넘게 쌓이고 개수가 늘었을 때만 낸다. 못 내면 None"""
    done, total, since, base, at = p.get("done"), p.get("total"), p.get("since"), p.get("since_done"), p.get("at")
    if not (total and done is not None and since and base is not None and at) or done >= total:
        return None
    if done <= base or at - since < 60:
        return None
    return (total - done) * (at - since) / (done - base)


def remaining_text(seconds):
    minutes = max(1, int(-(-seconds // 60)))
    return f"남은 시간 약 {minutes // 60}시간" + (f" {minutes % 60}분" if minutes % 60 else "") if minutes >= 60 else f"남은 시간 약 {minutes}분"


def progress_text(p, with_age=True):
    """작업 진행 한 줄: '109/829 (13%) · 남은 시간 약 40분 · 유튜브 403으로 쉬는 중 · 20:10 다시 시작 · 3분 전 갱신'"""
    now, bits = time.time(), []
    if p.get("total"):
        bits.append(f"{p.get('done') or 0}/{p['total']} ({int(100 * (p.get('done') or 0) / p['total'])}%)")
        if (p.get("done") or 0) < p["total"] and p.get("state") != "waiting" and not (p.get("resume_at") and p["resume_at"] > now):   # 쉬는 중엔 남은 시간을 안 냄
            left = progress_remaining(p)
            bits.append(remaining_text(left) if left is not None else "남은 시간 산정 중")
    elif p.get("done") is not None:
        bits.append(f"{p['done']}개")
    if p.get("text"):
        bits.append(p["text"])
    if p.get("resume_at"):
        bits.append(time.strftime("%H:%M", time.localtime(p["resume_at"])) + (" 다시 시작" if p["resume_at"] > now else " 다시 시작 예정이었음"))
    if with_age and p.get("at"):
        minutes = int((now - p["at"]) // 60)
        bits.append(f"{minutes}분 전 갱신" if minutes > 0 else "방금 갱신")
    return " · ".join(bits)


def parse_resume_at(value):
    """'20:10', '+15m', '+90s', ISO 시각, epoch 초를 epoch 로 바꾼다"""
    if value is None or value == "":
        return None
    v = str(value).strip()
    try:
        if v.startswith("+"):
            unit = {"s": 1, "m": 60, "h": 3600}.get(v[-1], 60)
            return time.time() + float(v[1:].rstrip("smh")) * unit
        if re.fullmatch(r"\d{1,2}:\d{2}", v):
            h, m = map(int, v.split(":"))
            at = datetime.datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
            if at.timestamp() < time.time() - 60:
                at += datetime.timedelta(days=1)
            return at.timestamp()
        if re.fullmatch(r"\d+(\.\d+)?", v):
            return float(v)
        at = datetime.datetime.fromisoformat(v)
        return at.timestamp()
    except ValueError:
        raise UserError(f"입력 내용 확인 — 다시 시작 시각 '{v}' 을 못 읽었어요. 20:10, +15m, ISO 시각 중 하나로 적어 주세요.")


def cmd_progress(a):
    """오래 도는 작업의 진행(몇 개 중 몇 개, 지금 상태, 다시 시작 시각)을 작업에 남긴다. 화면·작업판에 늘 보이고 상대를 깨우지 않는다.
    office bg 로 띄운 명령 안에서는 OFFICE_TASK 가 잡혀 있어 ID 를 생략할 수 있다"""
    words = list(a.words or [])
    task_id = int(words.pop(0)) if words and words[0].isdigit() else int(os.environ.get("OFFICE_TASK") or 0)
    if not task_id:
        raise UserError("입력 내용 확인 — 작업 ID 를 적어 주세요 (office bg 안에서는 생략 가능).")
    text = " ".join(" ".join(words).split())[:160]
    if a.done is not None and a.done < 0 or a.total is not None and a.total < 0:
        raise UserError("입력 내용 확인 — 개수는 0 이상으로 적어 주세요.")
    resume_at = parse_resume_at(a.resume_at)
    # 설명 글자만 바뀐 잦은 갱신(몇 초마다)은 저장하지 않음: 저장마다 상태 파일 잠금을 쥐어 화면 조회와 대화 전환이 늦어짐
    try:
        previous = next((x.get("progress") for x in state_view(current_sid())["tasks"] if x["id"] == task_id), None) or {}
    except (StorageError, OSError, KeyError, TypeError):
        previous = {}
    if (previous and not a.notify and getattr(a, "state", None) is None and time.time() - previous.get("at", 0) < 5
            and previous.get("done") == a.done and previous.get("total") == a.total and previous.get("resume_at") == resume_at):
        return print(f"#{task_id} 진행 기록 생략 · 5초 안에 이미 기록함")
    with state(True) as s:
        t = find(s, task_id)
        if t["status"] == "done":
            sys.exit(f"#{task_id} 는 이미 끝난 작업입니다.")
        old = t.get("progress") or {}
        now = time.time()
        p = {"text": text or old.get("text") or "", "done": a.done if a.done is not None else old.get("done"),
             "total": a.total if a.total is not None else old.get("total"), "resume_at": resume_at, "at": now,
             "state": getattr(a, "state", None) or old.get("state")}
        # 남은 시간 계산용 측정 시작점. 전체 개수가 바뀌거나 개수가 줄면(새로 시작) 다시 잰다
        keep = (old.get("since") and old.get("since_done") is not None and p["done"] is not None
                and p["total"] == old.get("total") and p["done"] >= old["since_done"])
        p["since"], p["since_done"] = (old["since"], old["since_done"]) if keep else (now if p["done"] is not None else None, p["done"])
        t["progress"] = p
        for message in s["msgs"]:
            if message.get("from") == "system" and message.get("job") == t.get("job") and not message.get("resolved_at") and re.search(r"로그 멈춤|진행 멈춤|무설명|진행 설명.*없", message.get("text", "")):
                message["resolved_at"], message["resolution"] = now, "진행 갱신을 확인했어요."
        if a.notify:   # 상태가 바뀌는 순간(쉬기 시작·다시 시작·끝)만 사용자에게 한 줄 알린다
            post(s, "system", "user", f"[진행] 작업 #{task_id} · {progress_text(p, False)}", "progress", job=t.get("job"))
    print(f"#{task_id} 진행 기록 · {progress_text(p)}")


def cmd_bg(a):
    """오래 걸리는 명령을 Office 가 관리하는 백그라운드로 띄운다. 차례가 끝나도 안 죽고, 끝나면(실패·강제 종료 포함) 담당을 한 번 깨운다"""
    command = (a.command or "").strip()
    if not command:
        raise UserError("입력 내용 확인 — 백그라운드로 돌릴 명령을 적어 주세요.")
    reason = " ".join((a.text or command).split())[:200]
    with state() as s:
        t = find(s, a.id)
        if t["status"] == "done" or t["owner"] not in (None, me()):
            sys.exit(f"#{a.id} 는 끝났거나 다른 담당 작업입니다.")
        task_job = t.get("job")
    folder = sdir(current_sid()) / "bg"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log = Path(a.log).expanduser().resolve() if a.log else folder / f"{a.id}-{stamp}.log"
    rc_file = folder / f"{a.id}-{stamp}.rc"
    log.parent.mkdir(parents=True, exist_ok=True)
    script = f"( {command}\n) >>{shlex.quote(str(log))} 2>&1 </dev/null; echo $? >{shlex.quote(str(rc_file))}"
    # 새 세션으로 띄워 에이전트 실행 도구·차례 종료가 프로세스 그룹을 정리해도 같이 죽지 않게 한다
    # 명령 안에서 office progress 를 ID 없이 쓰고, 띄운 차례의 요청이 먼저 끝나도 작업이 속한 요청으로 파일을 저장하게
    env = {**os.environ, "OFFICE_TASK": str(a.id), **({"OFFICE_JOB": str(task_job)} if task_job else {})}
    proc = subprocess.Popen(limited_command(["bash", "-c", script]), cwd=os.getcwd(), env=env, start_new_session=True, close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ident = process_identity(proc.pid)
    mark_waiting(a, reason, wait_pid=proc.pid, wait_start=ident and ident["start"], wait_log=str(log), wait_rc=str(rc_file), wait_cmd=command[:500])
    print(f"#{a.id} 백그라운드 시작 · pid {proc.pid} · 로그 {log}\n끝나면 서버가 깨워 줘요. 지금 차례는 끝내도 돼요.")


def cmd_done(a):
    with state(True) as s:
        t = find(s, a.id)
        if pending_user_request(t):
            raise UserError("사용자 확인 대기 — 필요한 자료나 답을 확인한 뒤 resolve-user로 해결하세요.")
        t.update(status="done", note=a.text, done_at=time.time())
        event(s, f"작업 #{a.id} 완료" + (f" · {a.text}" if a.text else ""), me(), job=message_job(s))
        print(f"#{a.id} 완료")
        released_job = t.get("job")
        remaining = any(x.get("job") == released_job and x.get("owner") == me() and x["status"] != "done" for x in s["tasks"])
    # 같은 담당의 병렬 작업 예약까지 풀지 않는다. 개별 파일은 release, 차례 종료 때는 전체 정리한다.
    if not remaining:
        release_reservations(current_sid(), released_job, me())


def cmd_handoff(a):
    with state(True) as s:
        t = find(s, a.id)
        t.pop("failover_from", None)
        t.pop("failover_to", None)
        t.setdefault("handoffs", []).append({"from": t.get("owner"), "to": a.to, "at": time.time(), "claimed_at": t.get("claimed_at")})
        t.pop("claimed_at", None)
        t.pop("done_at", None)
        t["created_at"] = time.time()
        t.update(owner=a.to, status="todo", note=f"{disp(me())}에게서 넘겨받음" + (f": {a.text}" if a.text else ""))
        event(s, f"작업 #{a.id}을(를) {disp(a.to)}에게 넘김" + (f" · {a.text}" if a.text else ""), me(), job=message_job(s))
        print(f"#{a.id} → {a.to}")
        released_job = t.get("job")
        remaining = any(x.get("job") == released_job and x.get("owner") == me() and x["status"] != "done" for x in s["tasks"])
    # 같은 담당의 병렬 작업 예약까지 풀지 않는다. 개별 파일은 release, 차례 종료 때는 전체 정리한다.
    if not remaining:
        release_reservations(current_sid(), released_job, me())


def cmd_say(a):
    with state(True) as s:
        job = message_job(s)
        kind = ("progress" if a.to == "user" else "note") if getattr(a, "info", False) else "msg"   # --to user --info: 사용자 진행 알림(차례·결과 카드 아님)
        explicit = getattr(a, "re", None)
        received = os.environ.get("OFFICE_REPLY_TO", "")
        if explicit is None and a.to == "user" and kind == "msg" and received.isdecimal() and any(m["id"] == int(received) and m.get("from") == "user" and m.get("to") in (me(), "all") for m in s["msgs"]):
            explicit = int(received)
        if explicit is not None:
            source = next((m for m in s["msgs"] if m["id"] == explicit and m["from"] == "user"), None)
            if source is None:
                raise UserError("답장 대상 확인 — 받은 사용자 메시지를 선택하세요.")
            job = source.get("job")
        if explicit is not None and (me() not in AGENTS or a.to != "user" or kind != "msg"):
            raise UserError("답장 대상 확인 — --re는 담당자의 사용자 답장에만 사용할 수 있습니다.")
        reply_to = reply_target(s, me(), explicit) if me() in AGENTS and a.to == "user" and kind == "msg" and (job is None or explicit is not None) else None
        now = time.time()
        if any(m.get("re") == reply_to and m.get("from") == me() and m.get("to") == a.to and m.get("job") == job and m.get("kind") == kind and m.get("text", "").strip() == a.text.strip() and now-m.get("at", 0) < 120 for m in s["msgs"][-10:]):
            return print("중복 메시지 생략")
        post(s, me(), a.to, a.text, kind, job=job, reply_to=reply_to)
        s["msgs"][-1]["at"] = now
        if a.to == me() and kind == "msg":
            s["msgs"][-1]["wake"] = True   # 자기 백그라운드 스크립트가 보낸 완료 알림도 깨운다


def report_brief(text):
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")[:40]


def cmd_finish(a):
    with state(True) as s:
        j = open_job(s)
        if not j:
            sys.exit("진행 중인 일 없음")
        if j.get("cancelled"):
            sys.exit("사용자가 취소한 요청입니다. 완료 보고를 덮어쓰지 않습니다.")
        if me() != j.get("acting_lead", j["lead"]):
            sys.exit("선택된 리더 또는 대행 리더만 완료 보고할 수 있습니다.")
        left = [t for t in s["tasks"] if t.get("job") == j["id"] and t["status"] != "done"]
        if any(t["status"] == "waiting" for t in left):
            sys.exit("기다리는 작업이 남아 있습니다. 결과가 나와 완료한 뒤 보고하세요. 중간 상황은 office say --to user 로 알리세요.")
        if left:
            sys.exit("남은 작업을 완료한 뒤 보고하세요.")
        jid = message_job(s)
        j.update(status="done", finished_by=me(), finished_at=time.time(), summary=a.text, brief=report_brief(a.text))
        event(s, f"요청 {j['id']} 완료", me(), job=jid)
        post(s, me(), "user", a.text, "report", job=jid)
        print(f"요청 {j['id']} 완료 보고함")



def lessons_text(n=10):
    try:
        return "\n".join((D / "lessons.md").read_text().splitlines()[-n:])
    except FileNotFoundError:
        return "(아직 없음)"


def cmd_lesson(a):
    text = " ".join(a.text.split())[:300]
    if not text:
        raise UserError("입력 내용 확인 — 교훈을 한 줄로 입력하세요.")
    D.mkdir(parents=True, exist_ok=True)
    with (D / "lessons.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lines = lessons_text(30).splitlines()
        lines = [x for x in lines if x != "(아직 없음)" and x.split(" | ", 1)[-1] != text]
        with state() as record:
            jid = message_job(record)
        lines.append(f"{time.strftime('%Y-%m-%d')} 요청 {jid or '-'} | {text}")
        path = D / "lessons.md"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=D, delete=False, encoding="utf-8") as out:
                temporary = Path(out.name)
                out.write("\n".join(lines[-30:]) + "\n")
                out.flush(); os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    print("배분 교훈 기록함")


def task_metrics(record, jid):
    rows = []
    for name in AGENTS:
        tasks = [t for t in record["tasks"] if t.get("job") == jid and t.get("owner") == name]
        elapsed = sum(max(0, t["done_at"]-t["claimed_at"]) for t in tasks if t.get("done_at") and t.get("claimed_at"))
        wait = sum(max(0, t["claimed_at"]-t["created_at"]) for t in tasks if t.get("claimed_at") and t.get("created_at"))
        measured = sum(bool(t.get("done_at") and t.get("claimed_at")) for t in tasks)
        waiting = sum(bool(t.get("claimed_at") and t.get("created_at")) for t in tasks)
        rows.append(f"{name}: 작업 {len(tasks)}건, 작업시간 {int(elapsed)}초({measured}건 측정·{len(tasks)-measured}건 미측정), 대기 {int(wait)}초({waiting}건 측정)")
    job = next((j for j in record["jobs"] if j["id"] == jid), {})
    for actor, metrics in job.get("execution_metrics", {}).items():
        rows.append(f"{actor} 실행 {metrics['turns']}회·단순대기답변 {metrics['waiting_replies']}회·실행 {metrics['seconds']}초·새 토큰 {metrics['tokens']}")
    return "\n".join(rows) + f"\n검수·완료 이후 작업 추가 {job.get('rework_count', 0)}회(후속작업 포함, 실패 횟수 아님)."


LOGIN_CMDS = {"claude": "DISABLE_AUTOUPDATER=1 claude auth login", "codex": "codex login"}


def login_paths(name):
    folder = D / "login"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{name}.fifo", folder / f"{name}.out", folder / f"{name}.pid"


LOGIN_PROCS = {}   # 서버가 띄운 로그인 과정: 끝나면 회수해야 좀비로 남아 '아직 진행 중'처럼 보이지 않는다


def login_alive(pidfile):
    try:
        pid = int(pidfile.read_text())
        proc = LOGIN_PROCS.get(pid)
        if proc is not None and proc.poll() is not None:   # 내가 띄운 과정이면 회수까지
            LOGIN_PROCS.pop(pid, None)
            return False
        os.kill(pid, 0)
        # 다른 프로세스가 띄운 과정이 좀비(Z)로 남았으면 끝난 것으로 본다
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rindex(")") + 2] != "Z"
    except (OSError, ValueError):
        return False


def login_ok(name):
    """로그인돼 있는지: claude auth status / codex login status 가 성공하면 됨"""
    try:
        return subprocess.run(["claude", "auth", "status"] if name == "claude" else ["codex", "login", "status"],
                              capture_output=True, text=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def login_start(name):
    """로그인 과정을 띄우고 로그인 링크를 돌려준다. 브라우저는 열지 않는다(사용자가 링크를 직접 엶). 10분 지나면 정리된다."""
    fifo, out, pidfile = login_paths(name)
    if login_alive(pidfile):
        os.killpg(int(pidfile.read_text()), signal.SIGTERM)
    fifo.unlink(missing_ok=True)
    os.mkfifo(fifo, 0o600)
    out.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_CODE_", "OFFICE_")) and k != "CLAUDECODE"}
    env["BROWSER"] = "true"      # 브라우저 자동 열기 막기
    script = f'exec 3<>{shlex.quote(str(fifo))}; exec timeout 600 bash -c {shlex.quote(LOGIN_CMDS[name])} <&3 >>{shlex.quote(str(out))} 2>&1'
    proc = subprocess.Popen(["bash", "-c", script], env=env, cwd=str(Path.home()), start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pidfile.write_text(str(proc.pid))
    LOGIN_PROCS[proc.pid] = proc
    text = ""
    for _ in range(60):
        text = ANSI_SGR.sub("", re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.read_text(errors="replace")))
        found = re.search(r"https://\S+", text)
        if found:
            return found.group(0)
        if proc.poll() is not None:
            break
        time.sleep(.5)
    raise UserError(f"로그인 링크를 받지 못했어요 — {text.strip().splitlines()[-1][:200] if text.strip() else '응답이 없어요'}")


def login_feed(name, code):
    """사용자가 준 로그인 코드를 띄워 둔 로그인 과정에 넣는다"""
    fifo, out, pidfile = login_paths(name)
    if not login_alive(pidfile):
        raise UserError("로그인 과정이 없어요 — office login start 로 링크부터 다시 받으세요.")
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, (code.strip() + "\n").encode())
    finally:
        os.close(fd)


def cmd_login(a):
    """대화에서 로그인: start 는 로그인 링크를 받아 출력, code 는 사용자가 준 코드를 로그인 과정에 넣는다."""
    name = a.name
    fifo, out, pidfile = login_paths(name)
    if a.step == "start":
        url = login_start(name)
        print(f"{disp(name)} 로그인 링크예요. 브라우저에서 열어 주세요.")
        print(url)
        print("로그인 후 화면에 나온 코드를 채팅에 붙여 넣어 주세요." if name == "claude"
              else "이 PC 브라우저에서 열면 로그인이 자동으로 끝나요.")
        return
    if a.step == "code":
        if not a.code:
            raise UserError("로그인 코드 확인 — 코드를 함께 입력하세요.")
        login_feed(name, a.code)
    for _ in range(60):     # code 다음이나 status: 끝날 때까지 최대 30초
        if not login_alive(pidfile):
            break
        time.sleep(.5)
    if login_ok(name):
        print(f"{disp(name)} 로그인이 끝났어요.")
    else:
        tail = [l for l in ANSI_SGR.sub("", out.read_text(errors="replace")).splitlines() if l.strip()]
        raise UserError(f"{disp(name)} 로그인이 아직 안 됐어요 — {tail[-1][:200] if tail else '코드를 다시 확인해 주세요'}")


IMPORT_SKIP = (".sid", ".worker.json", ".worker.json.bak", ".lock", "lock")


def cmd_import(a):
    """다른 작업 폴더의 대화를 지금 폴더로 가져온다. 같은 ID가 이미 있으면
    같은 대화(생성 시각 같음)는 건너뛰고, 다른 대화면 새 ID를 붙여 가져온다.
    에이전트 세션·작업 잠금 파일은 옛 폴더 기준이라 복사하지 않는다(새 차례부터 새 세션)."""
    src_root = Path(a.source).expanduser().resolve()
    src = src_root / ".office"
    if not (src / "meta.json").is_file() and not (src / "state.json").is_file():
        raise UserError(f"가져올 대화가 없어요 — {src_root} 에 Office 대화 기록이 없습니다.")
    if src.resolve() == D.resolve():
        raise UserError("가져오기 확인 — 지금 폴더와 같은 폴더예요. 다른 작업 폴더를 지정하세요.")
    old = read_json_file(src / "meta.json", {"current": None, "sessions": []}, meta_valid) \
        if (src / "meta.json").is_file() else {"current": None, "sessions": []}
    items = [(x, src / "sessions" / x["id"]) for x in old["sessions"] if not x.get("deleted") and valid_sid(x["id"])]
    if not old["sessions"] and (src / "state.json").is_file():    # 대화 목록 기능 전 기록
        items = [({"id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4], "title": "이전 대화",
                   "created": (src / "state.json").stat().st_mtime, "updated": (src / "state.json").stat().st_mtime}, src)]
    added, skipped = [], 0
    with meta(True) as m:
        mine = {x["id"]: x for x in m["sessions"]}
        for entry, folder in items:
            if not folder.is_dir():
                continue
            sid = entry["id"]
            # 같은 시각에 만들어진 대화가 이미 있으면 예전에 가져온 것 — ID가 바뀌었어도 건너뜀
            if any(abs(float(x.get("created", 0)) - float(entry.get("created", 0))) < 1 for x in mine.values()):
                skipped += 1
                continue
            if sid in mine or sdir(sid).exists():
                while sid in mine or sdir(sid).exists():
                    sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
            target = sdir(sid)
            target.mkdir(parents=True)
            names = ["state.json", "claude.log", "codex.log", "usage.json"] if folder == src else \
                [f.name for f in folder.iterdir() if f.is_file() and not f.name.endswith(IMPORT_SKIP)]
            for name in names:
                if (folder / name).is_file():
                    shutil.copy2(folder / name, target / name)
            row = {"id": sid, "title": str(entry.get("title") or "가져온 대화"), "created": entry.get("created", time.time()),
                   "updated": entry.get("updated", time.time()), "deleted": False}
            m["sessions"].append(row)
            mine[sid] = row
            added.append(row["title"])
    print(f"대화 {len(added)}개를 가져왔어요." + (f" 이미 있는 대화 {skipped}개는 건너뛰었어요." if skipped else ""))
    for title in added[:20]:
        print(f"- {title}")
    if added and running():
        print("열려 있는 Office 창을 새로고침하면 목록에 보여요. 안 보이면 Office를 다시 시작하세요.")


def cmd_title(a):
    title = " ".join(a.text.split())[:40]
    if not title:
        sys.exit("제목을 입력하세요")
    sid = current_sid()
    with meta(True) as m:
        for x in m["sessions"]:
            if x["id"] == sid:
                if x.get("renamed"):
                    return print("사용자가 정한 제목 유지")
                x["title"] = title
                return print(f"대화 제목: {title}")
    sys.exit("현재 대화 없음")

def cmd_board(a):
    with state() as s:
        print(board_text(s))


def log_line(m):
    to = "" if m["to"] in ("all", "log") else f" → {disp(m['to'])}"
    tag = {"event": "· ", "report": "📋 ", "turn": "💬 "}.get(m.get("kind"), "")
    lines = (m["text"] or "").splitlines() or [""]
    return f"[{m['ts']}] {tag}{disp(m['from'])}{to}: " + "\n           ".join(lines)


def log_working_status(sid):
    """GUI의 실시간 워커 상태를 조회만 한다. 연결 실패 때 저장된 상태로 추측하지 않는다."""
    try:
        info = json.loads((D / "server.json").read_text())
        port = info.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            return ""
        opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/state", timeout=0.5) as response:
            data = json.load(response)
        if data.get("session") != sid:
            return ""
        parts, now = [], time.time()
        for name in AGENTS:
            worker = data.get("agents", {}).get(name, {})
            if worker.get("status") != "working":
                continue
            last = worker.get("since", now)
            since = last
            activity = worker.get("activity") or []
            if activity:
                stamp = activity[-1].get("ts", "")
                try:
                    last = time.mktime(time.strptime(time.strftime("%Y-%m-%d ") + stamp, "%Y-%m-%d %H:%M:%S"))
                    if last - now > 12 * 3600:
                        last -= 24 * 3600
                except (ValueError, OverflowError):
                    pass
            age = max(0, int(now - max(last, since)))
            ago = ("방금" if age < 5 else f"{age}초 전" if age < 60
                   else f"{age // 60}분 전" if age < 3600 else f"{age // 3600}시간 전")
            parts.append(f"{disp(name)} 작업 중 · 마지막 움직임 {ago}")
        return " | ".join(parts)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return ""


def cmd_log(a):
    """대화 메시지를 tail처럼 출력. -f면 새 메시지를 계속 따라감 (현재 대화가 바뀌면 그쪽으로 넘어감)"""
    if a.n < 0:
        raise UserError("로그 개수 확인 — -n은 0 이상의 정수여야 합니다.")
    sid, seen = None, 0
    interactive = a.follow and sys.stdout.isatty()
    drawn = False
    def clear_status():
        nonlocal drawn
        if drawn:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            drawn = False
    try:
        while True:
            m = read_json_file(D / "meta.json", {"current": None, "sessions": []}, meta_valid, required=True)
            live = [x for x in m["sessions"] if not x["deleted"]]
            cur = os.environ.get("OFFICE_SESSION") or m.get("current")
            selected = next((x for x in live if x["id"] == cur), None)
            if selected is None:
                if not live:
                    raise UserError("표시할 대화 없음 — 이 폴더에 표시할 Office 대화가 없습니다. office log <폴더>로 지정하세요.")
                selected = max(live, key=lambda x: x["updated"])
                cur = selected["id"]
            if cur != sid:
                clear_status()
                title = selected["title"]
                print(f"── {title} ──", flush=True)
                sid, seen = cur, None
            s = read_json_file(sdir(sid) / "state.json", {}, state_valid)
            msgs = s.get("msgs", [])
            if seen is None:
                seen = max(0, len(msgs) - a.n)
            if msgs[seen:]:
                clear_status()
            for m in msgs[seen:]:
                print(log_line(m), flush=True)
            seen = len(msgs)
            if not a.follow:
                return
            if interactive:
                status = log_working_status(sid)
                clear_status()
                if status:
                    width = max(0, shutil.get_terminal_size(fallback=(100, 24)).columns - 1)
                    sys.stdout.write("\r" + fit_console(status, width))
                    sys.stdout.flush()
                    drawn = True
            time.sleep(POLL)
    except KeyboardInterrupt:
        pass
    finally:
        clear_status()


# ── 워커 ────────────────────────────────────────────────────
PROTOCOL = """너는 '{me}'. 동료 '{other}'와 둘이 한 팀으로 이 저장소에서 일한다. 'user'는 이 일을 맡긴 사용자다.
작업판·메신저는 `office` 셸 명령으로만 다룬다 (네 이름은 자동으로 붙음):
  office add "할일" --to 이름       작업 만들어 배정 (리더가 일 쪼갤 때)
  office claim ID                  주인 없는 작업 가져가기
  office done ID "결과 요약"        끝냄
  office wait ID "기다리는 것" [--pid N] [--log 파일] [--minutes M]   외부 결과를 기다리는 동안 열어 두기 (재촉 안 받음, 새 메시지·감시 프로세스 종료·기한 지나면 깨어남)
  office bg ID "설명" "명령" [--log 파일]   몇 분 넘는 명령을 Office가 관리하는 백그라운드로 실행 (차례 끝나도 안 죽고, 끝나면 담당을 깨움)
  office progress ID "지금 상태" [--done N --total M] [--resume-at 20:10] [--notify]   오래 도는 일의 진행을 화면에 남기기 (bg 명령 안에선 ID 생략)
  office handoff ID --to {other} "진행상황/이유"   넘기기
  office say --to {other} "말"      동료에게 의견·리뷰요청·질문 (--to user 는 사용자, --to user --info 는 사용자에게 진행 한 줄)
  office finish "최종 보고"         (리더만) 일 전체 마무리하고 사용자에게 보고
  office title "대화 요약 제목"      (리더만) 대화 전체 주제를 요약한 짧은 제목 갱신 (사용자가 정한 이름은 유지)
일하는 방식:
- 네 담당 작업부터 처리. 시작할 때 `office claim ID`로 진행중 표시 (주인 없는 todo도 claim으로 가져감). 동료 담당은 건드리지 마라.
- 상대가 더 잘할 일이거나 막히면 handoff. 의견이 필요하면 say로 묻고 기다리는 동안 다른 일 해라.
- 외부 프로세스(tmux·서버·다른 CLI)에 입력을 보냈으면 실제로 들어갔는지 바로 확인해라. 결과를 1분 넘게 반복 확인하며 기다리지 마라.
- 동료 완료·답을 기다릴 때 sleep·백그라운드 작업·반복 확인으로 차례를 붙잡지 마라. 할 일이 없으면 차례를 끝내라. 새 메시지·작업 변화가 생기면 서버가 다시 깨운다(차례를 붙잡고 있으면 그 사이 넘어온 일을 못 받는다).
- 동료가 작업 중인 파일은 직접 고치지 말고 say로 조율.
- 파일을 만들기 전에 기존 폴더 구조를 확인하고 같은 목적의 파일이 있는 곳에 함께 두세요. 새 프로젝트는 `~/projects/<프로젝트명>/`에 두고, 문서·입력 자료·코드·결과물은 기존 분류를 따라 나누세요.
- 홈 최상위에 임시·시험·백업·1회용 스크립트를 흩어 놓지 마세요. 한 번 쓰는 파일은 `/tmp/<작업명>/`에, 계속 보관할 결과는 해당 프로젝트의 결과 폴더에 두세요. 이름만 보고 목적과 내용을 알 수 있게 하세요.
- 경로를 바꾸면 스크립트·예약 실행·설정·문서·메모리의 참조도 함께 바꾸고 실제 동작을 확인하세요. 기본 설정 폴더·실행 중인 서비스·가상환경은 경로 의존성을 먼저 확인하세요.
- 작업이 끝나면 직접 만든 임시 파일과 산출물 위치를 점검하세요. 삭제가 허용된 임시 파일을 정리하고 사용자에게 결과물의 최종 경로를 알리세요.
- 몇 분 넘게 걸리는 수집·다운로드·빌드는 `office bg ID "설명" "명령"`으로 띄우고 차례를 끝내라. nohup·&·실행 도구의 백그라운드 기능은 차례가 끝날 때 같이 죽는다. 끝나면 서버가 깨우니 `office claim ID`로 다시 시작한다. 이미 따로 띄운 프로세스는 `office wait ID "사유" --pid N --log 파일`로 감시를 건다.
- 수십 분 넘게 도는 수집·반복 작업은 사용자가 묻지 않아도 '몇 개 중 몇 개 했고, 쉬는 중이면 언제 다시 시작하는지' 화면에서 보이게 해라. 스크립트가 진행이 바뀔 때마다 `office progress --done N --total M "상태"`(쉬는 중이면 `--resume-at`)를 부르게 만들고, 쉬기 시작·다시 시작·끝처럼 상태가 바뀔 때만 `--notify` 를 붙인다.
- 실제 처리와 입력·선행 작업 대기를 구분해 `office progress --state working|waiting "상태"`로 기록한다. 감시 프로세스가 살아 있어도 선행 다운로드·사용자 입력을 기다리면 waiting, 처리가 시작되면 working으로 바꾼다.
- 파일 제공·로그인·선택·승인처럼 사용자가 해야 할 일이 생기면 `office need-user ID "필요한 것과 제출 방법"`으로 남기고 차례를 끝내라. 인증 코드는 `--expires-at`으로 만료 시각을 붙여라. 자료나 답을 확인한 뒤 `office resolve-user ID "확인 결과" --re 메시지번호`로 해결하고 이어가라. 새 메시지만으로 해결 처리하지 마라. 외부 처리·동료 답 대기는 기존 wait를 써라.
- 사용자한테는 꼭 필요한 결정만 물어라. 웬만한 건 둘이 정해서 끝내라.
- 파일 수정 전에 office reserve "파일"로 예약한다. 다른 담당/대화가 예약한 파일은 읽기·검수만 한다.
- 원본 파일은 직접 Edit/Write/셸로 저장하지 말고 원본을 /tmp 임시 원고로 복사하고 원고만 부분 수정한 뒤 office write "파일" --from "임시 원고"로 저장한다. 끝나면 office release "파일".
- 작업 추가 전에 같은 작업이 있는지 확인하고 기존 ID를 사용한다. 작은 수정은 한 명이 끝내고 분리 가능한 작업·독립 검수에만 둘을 쓴다.
- 동료에게 남기는 진행 알림은 office say --info로 남긴다(상대를 깨우지 않음). 일반 say는 질문·막힘·결정 등 상대가 행동해야 할 때만 쓴다.
- 몇 분 넘는 요청에선 단계가 끝났을 때·동료 결과가 왔을 때·막히거나 가정이 틀렸거나 예상보다 길어질 때 `office say --to user --info "한 줄"`로 사용자에게 알린다(사용자 차례를 만들지 않음).
- 메시지는 필요한 것만. 인사·감사·맞장구 금지.
- 사용자에게 직접 받은 메시지는 job·작업판이 비어 있어도 요청이다. 질문에는 답하고, 수정·실행 요청은 작업을 등록·claim한 뒤 실제로 완료해라. 단독 요청을 '대기'로 무시하지 마라.
- 사용자가 '로그인 해줘'라고 하면 각자 `office login start 자기이름`을 실행해 나온 링크를 office say --to user 로 그대로 올린다. 사용자가 Claude 코드를 주면 claude가 `office login code claude 코드`로 넣고 결과를 알린다. codex는 링크를 열면 끝나므로 `office login status codex`로 확인해 알린다.
- 처리할 사용자 메시지도 담당·미배정 작업도 없을 때만 명령 실행 없이 '대기'라고 답해라.
사용자 보고 형식 (finish 및 사용자에게 보내는 say) — ChatGPT·Claude 답변처럼 읽기 좋게:
- 첫 줄은 결론 한 문장(40자 안팎)이다. 화면에서 굵은 요약으로 보인다.
- 본문은 기본이 짧은 문단이다. 이유·설명·한두 가지 사실은 목록 없이 2~3문장 문단으로 쓴다.
- 목록은 나란한 항목이 3개 이상이거나 순서가 있는 단계일 때만 쓴다. 단계는 '1.' 번호 목록으로 쓴다.
- 내용이 많을 때만 '### 바뀐 것'·'### 확인한 것'·'### 남은 일' 같은 소제목으로 나눈다. 짧은 답엔 소제목을 쓰지 않는다.
- 비교·수치는 표(| 항목 | 값 |)로 정리하면 좋다. 핵심 단어만 **굵게**, 명령·경로는 `코드`로 쓴다.
- 문단·목록·소제목 사이엔 빈 줄을 둔다. 줄바꿈은 실제 개행으로 전달하고 역슬래시 n 글자를 쓰지 않는다.
- 인사·맞장구·같은 말 반복은 빼고, 파일 이름·구현 용어는 사용자에게 필요할 때만 쓴다.
"""

PLAN = """[리더 차례: 새 요청 접수] 요청 {jid}: {text}
이 일을 끝까지 책임지는 리더는 너다. 필요하면 파일을 살펴본 뒤 작업으로 쪼개서
`office add "..." --to claude|codex` 로 둘에게 나눠 배정해라 (너 자신 포함, 병렬로 할 수 있게, 같은 파일 겹치지 않게).
쪼갤 필요 없는 작은 일이면 직접 처리하고 `office finish "보고"` 해도 된다.
구현·조사·검증 등 실제 작업을 둘에게 고르게 나눠라. 리더가 계획·검수만을 이유로 구현까지 혼자 맡지 마라.
독립적으로 진행할 작업은 먼저 동료에게 배정해 동시 진행하고, 단순 문구 변경처럼 나눌 필요 없는 작업만 직접 처리해라.
배정 전 작업판에서 각자 아직 안 끝난 작업 수를 확인해 덜 바쁜 쪽에 먼저 준다. 단 한쪽이 확실히 더 잘하는 일(그 사람이 이미 손대는 파일 포함)이면 바빠도 그쪽에 준다.
작업이 2개 이상이거나 몇 분 넘게 걸릴 것 같으면 배정 직후 `office say --to user --info "계획"`으로 누가 무엇을, 대략 얼마나 걸리는지 2~3줄 먼저 알리고 답을 기다리지 말고 진행해라. 짧은 요청은 결과만 보고한다.
작업이 가볍거나(문구·확인) 무거우면(장애 분석·큰 구현) `office add ... --level light|heavy`로 표시한다(그 무게에 맞는 모델로 돈다).
배정 후엔 네 몫부터 바로 시작해라. 사용자 보고는 위 '사용자 보고 형식'(결론 한 줄, 문단 중심, 목록은 필요할 때만)을 따른다.
아래 대화 전체 요청 이력을 참고해 전체 주제를 아우르는 15자 내외의 짧은 어구로 `office title "제목"`을 실행해라. 마지막 요청 원문을 그대로 제목으로 쓰지 마라."""

REVIEW = """[리더 차례: 검수] 요청 {jid}: {text}
배정한 작업이 전부 완료됐다. 결과물을 직접 확인해서 원래 요구사항을 다 만족하는지 검수해라.
문제가 있으면 `office add "수정할 것" --to 담당` 으로 다시 배정.
문제없으면 대화 전체 요청 이력을 참고해 전체 주제를 아우르는 15자 내외 제목으로 `office title "제목"`을 갱신한 뒤
`office finish "사용자에게 보낼 최종 보고 (뭘 했는지, 남은 이슈)"`.
보고는 위 '사용자 보고 형식'(결론 한 줄, 문단 중심, 목록은 필요할 때만)을 따른다."""



ERROR_KINDS = {"auth", "limit", "capacity", "network", "crash", "timeout", "unfinished", "unknown"}


def classify_agent_error(reason, rc=0):
    text = str(reason or "").lower()
    if text.startswith("남은 작업 확인 필요"):
        return "unfinished"
    if re.search(r"invalid api key|please (?:run|use).*?/login|not logged in|not authenticated|oauth.*(?:expired|invalid)|(?:refresh|access) token.*(?:expired|invalid|revoked|failed)|token.*refresh.*(?:failed|expired)|\b401\b|unauthorized|authentication(?:_| )(?:error|failed|failure)|refresh token", text):
        return "auth"
    # 요금제 사용 한도 소진: 로그인 문제가 아니라서 로그인 링크 대신 초기화 시각 안내를 한다
    if re.search(r"usage[ _]limit|hit your [^.\n]{0,40}?(?:limit|budget)|spend limit|credit balance is too low|limit[ _](?:reached|exceeded)|out of (?:extra )?usage|insufficient_quota|quota exceeded|upgrade to (?:pro|max|plus)", text):
        return "limit"
    if re.search(r"at capacity|overloaded|overload_error|\b529\b|capacity exceeded", text):
        return "capacity"
    if re.search(r"timed? ?out|timeout|응답.*(?:중단|초과)|시간.*초과", text):
        return "timeout"
    if re.search(r"connection|network|websocket|dns|connect|ssl|certificate|stream disconnected|연결", text):
        return "network"
    if rc or re.search(r"segmentation|signal|crash|프로세스|실행 실패", text):
        return "crash"
    return "unknown"


def missing_claude_session(reason):
    return bool(re.search(r"no (?:conversation|session) found|session[^\n]{0,100}(?:not found|does not exist)", str(reason or ""), re.I))


def safe_agent_diagnostic(text):
    text = str(text)
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[숨김]", text)
    text = re.sub(r"(?i)((?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token)\s*[=:]\s*)[^\s,}]+", r"\1[숨김]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[숨김]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[숨김]", text)
    return text


LOGIN_HOOK = None   # Office.offer_login — 실행 중 로그인이 풀리면 Worker 가 불러 로그인 링크를 띄운다


def auth_help(name):
    return ("로그인 확인 필요 — 로그인이 풀렸어요. 대화창에 뜬 로그인 링크로 다시 로그인하면 자동으로 이어서 해요. 링크가 없으면 터미널에서 claude를 실행해 /login 후 다시 시작을 누르세요." if name == "claude" else
            "로그인 확인 필요 — 로그인이 풀렸어요. 대화창에 뜬 로그인 링크로 다시 로그인하면 자동으로 이어서 해요. 링크가 없으면 터미널에서 codex login 후 다시 시작을 누르세요.")


def checkpoint_valid(value):
    if not isinstance(value, dict):
        return False
    cursor = value.get("read_upto")
    return ((cursor is None or type(cursor) is int and cursor >= 0)
            and isinstance(value.get("paused", False), bool)
            and (value.get("error") is None or isinstance(value["error"], str))
            and (value.get("error_kind") is None or isinstance(value.get("error_kind"), str) and value.get("error_kind") in ERROR_KINDS)
            and (value.get("interrupted_turn") is None or isinstance(value.get("interrupted_turn"), dict))
            and type(value.get("fail_count", 0)) is int and value.get("fail_count", 0) >= 0
            and (value.get("failure_key") is None or isinstance(value.get("failure_key"), str))
            and type(value.get("auto_retries", 0)) is int and value.get("auto_retries", 0) >= 0
            and isinstance(value.get("retry_counts", {}), dict)
            and all(isinstance(k, str) and (k == "direct" or k.isdecimal() and int(k) > 0)
                    and type(v) is int and v >= 0 for k, v in value.get("retry_counts", {}).items())
            and (value.get("retry_job") is None or type(value.get("retry_job")) is int and value["retry_job"] > 0)
            and type(value.get("retry_at", 0)) in (int, float) and math.isfinite(value.get("retry_at", 0)) and value.get("retry_at", 0) >= 0)


def usage_valid(value):
    if not isinstance(value, dict) or not isinstance(value.get("usage_v2", {}), dict):
        return False
    for name in AGENTS:
        if name in value and (type(value[name]) is not int or value[name] < 0):
            return False
        record = value.get("usage_v2", {}).get(name, {})
        if not isinstance(record, dict):
            return False
        if any(type(record.get(k, 0)) is not int or record.get(k, 0) < 0 for k in ("tokens", "cached_tokens")):
            return False
        if not isinstance(record.get("since", 0), (int, float)):
            return False
    return True


def terminate_process(proc, timeout=2):
    """Office가 시작한 프로세스·도구 그룹을 정리하고 부모를 회수."""
    group = getattr(proc, "_office_group", False)
    def send(sig):
        try:
            if group:
                os.killpg(proc.pid, sig)
            elif proc.poll() is None:
                proc.send_signal(sig)
        except ProcessLookupError:
            pass
    send(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        send(signal.SIGKILL)
        proc.wait(timeout=timeout)
    if group:
        send(signal.SIGKILL)   # 부모 종료 뒤 남은 하위 도구도 정리


def process_identity(pid):
    try:
        folder = Path("/proc") / str(pid)
        stat = (folder / "stat").read_text().rsplit(")", 1)[1].split()
        return {"start": int(stat[19]), "uid": folder.stat().st_uid,
                "command": hashlib.sha256((folder / "cmdline").read_bytes()).hexdigest()}
    except (OSError, ValueError, IndexError):
        return None


def process_record_valid(value):
    return isinstance(value, dict) and (not value or all(type(value.get(k)) is int and value[k] > 0 for k in ("pid", "start", "owner", "owner_start")) and type(value.get("uid")) is int and isinstance(value.get("command"), str))


def recover_recorded_process(path):
    record = read_json_file(path, {}, process_record_valid)
    if not record:
        return False
    identity = process_identity(record["pid"])
    if identity and all(identity[k] == record[k] for k in ("start", "uid", "command")):
        owner = process_identity(record["owner"])
        if record["owner"] != os.getpid() and owner and owner["start"] == record["owner_start"]:
            raise UserError("이전 Office 실행 중 — 이전 Office가 아직 실행 중입니다. 종료한 뒤 재개하세요.")
        try:
            group = os.getpgid(record["pid"])
        except ProcessLookupError:
            path.unlink(missing_ok=True)
            return False
        if identity["uid"] != os.getuid() or group != record["pid"]:
            raise UserError("이전 실행 확인 실패 — 이전 작업 프로세스를 안전하게 확인하지 못했습니다.")
        try:
            os.killpg(record["pid"], signal.SIGTERM)
            time.sleep(.1)
            # 같은 프로세스임을 재확인한 뒤 하위 도구까지 정리한다.
            current = process_identity(record["pid"])
            if current and current["start"] == record["start"]:
                os.killpg(record["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        path.unlink(missing_ok=True)
        return True
    path.unlink(missing_ok=True)   # 종료됐거나 PID가 재사용된 기록.
    return False


# 차례 안 세부 단계(화면 표시용). 최근 도구 활동 하나로 판별하고 글·시작·종료·명령 완료 줄은 무시
STEP_WRAP = re.compile(r"\boffice (?:done|finish|handoff|release)\b|\boffice say\b.*--to user")
STEP_EDIT = re.compile(r"\boffice (?:reserve|write)\b|\bsed -i|\bperl -[a-z]*i|\bapply_patch\b|\bgit apply\b|\bpatch -p")
STEP_CHECK = re.compile(r"py_compile|pytest|unittest|node --check|bash -n\b|npm (?:run )?test|\boffice shot\b"
                        r"|\bdiff\b|json\.tool|--headless|--screenshot|\bshellcheck\b|\bcurl\b")


def activity_step(kind, line):
    if kind == "edit":
        return "edit"
    if kind != "tool" or line.startswith("명령 "):
        return None
    for step, pattern in (("wrap", STEP_WRAP), ("edit", STEP_EDIT), ("check", STEP_CHECK)):
        if pattern.search(line):
            return step
    return "look"


class Worker:
    """대화 하나에 붙은 에이전트 한 명. 대화마다 claude·codex 한 쌍이 따로 돎"""
    def __init__(self, name, sid, stop):
        self.name, self.other = name, AGENTS[1 - AGENTS.index(name)]
        self.sid, self.stop = sid, stop
        self.idle_mark = None   # (기록 파일 식별값, 시각): 할 일 없이 쉬는 중이면 기록이 그대로인 동안 전체 점검을 건너뜀
        self.status, self.since, self.doing = "idle", time.time(), None
        self.paused = False
        self.error = None
        self.rc = 0
        self.notice = None
        self.usage_lock = threading.RLock()
        self.usage_tracking = False
        self.turn_counted = (0, 0)
        self.rollout_root = Path.home() / ".codex" / "sessions"
        self.rollout_file = None
        self.rollout_pos = 0
        self.rollout_total = {}
        self.rollout_baseline = {}
        self.rollout_expected = {}
        self.rollout_expected_thread = None
        self.usage_polled = 0
        self.proc = None
        self.proc_lock = threading.Lock()
        self.interrupted_job = None
        self.immediate_pause = False
        self.process_file = sdir(sid) / f"{name}.process.json"
        recovered = recover_recorded_process(self.process_file)
        self.subs = {}
        self.codex_message_rows = {}
        self.activity = deque(maxlen=60)
        self.checkpoint = sdir(sid) / f"{name}.worker.json"
        self.checkpoint_lock = threading.Lock()
        try:
            saved = read_json_file(self.checkpoint, {}, checkpoint_valid)
        except StorageError as error:
            server_log("worker.checkpoint.read", error)
            saved = {"paused": True, "error": "진행 기록 읽기 실패 — 진행 기록을 읽지 못했습니다. 폴더 권한을 확인하세요."}
        self.read_upto = saved.get("read_upto")
        self.paused = saved.get("paused", False)
        self.manual_pause = saved.get("manual_pause", self.paused and not saved.get("error"))
        self.error = saved.get("error")
        self.error_kind = saved.get("error_kind")
        if not self.error_kind or self.error_kind == "unknown":
            self.error_kind = classify_agent_error(self.error) if self.error else None
        self.resume_turn = saved.get("interrupted_turn")
        self.auto_retries = saved.get("auto_retries", 0)
        self.retry_job = saved.get("retry_job")
        self.retry_counts = dict(saved.get("retry_counts", {}))
        if self.auto_retries:
            self.retry_counts.setdefault("direct" if self.retry_job is None else str(self.retry_job), self.auto_retries)
        self.recovery = saved.get("recovery")
        self.retry_at = saved.get("retry_at", 0)
        self.retry_due = dict(self.resume_turn) if self.resume_turn and not self.paused and not self.retry_at else None
        self.fail_count = saved.get("fail_count", 0)
        self.failure_key = saved.get("failure_key")
        if self.error_kind == "auth":
            self.error = auth_help(self.name)
        try:
            with state(sid=sid) as s:
                count = len(s["msgs"])
            self.read_upto = count if self.read_upto is None else min(self.read_upto, count)
        except (OSError, StorageError) as error:
            self.read_upto = 0
            self.paused, self.error = True, "대화 기록 읽기 실패 — 대화 기록을 읽지 못했습니다. 백업과 폴더 권한을 확인하세요."
            server_log("worker.initialize", error)
        if self.paused:
            self.status = "paused"
        if recovered:
            self.notice = "중단된 작업 재개 — 이전 서버에서 남은 실행을 정리했습니다. 중단된 작업을 이어갑니다."
        if self.retry_at and self.resume_turn:
            delay = max(0, math.ceil(self.retry_at - time.time()))
            self.notice = f"자동 재시도 대기 — 일시 오류로 {delay}초 뒤 다시 시도합니다."
        self.restart_reported = False
        self.cont = self.chatter = 0
        self.ufile = sdir(sid) / "usage.json"
        # 예전 총합은 캐시 분리 불가: 원본을 보존하고 새 기준으로 따로 집계.
        self.ulock = sdir(sid) / "usage.lock"
        self.tokens = self.cached_tokens = self.legacy_tokens = 0
        self.usage_since = time.time()
        try:
            u = read_json_file(self.ufile, {}, usage_valid)
            self.legacy_tokens = u.get(name, 0)
            v = u.get("usage_v2", {}).get(name, {})
            self.tokens = v.get("tokens", 0)
            self.cached_tokens = v.get("cached_tokens", 0)
            self.usage_since = v.get("since", self.usage_since)
        except (OSError, ValueError, StorageError) as error:
            server_log("usage.initialize", error)
            self.notice = "사용량 기록 읽기 실패 — 사용량 기록을 읽지 못했습니다. 폴더 권한을 확인하세요."

    def save_checkpoint(self, processed=False):
        # 실행 중 일시정지하면 아직 완료하지 않은 메시지는 재시작 후 다시 읽음.
        cursor = getattr(self, "turn_read_start", self.read_upto) if self.status == "working" and not processed else self.read_upto
        with self.checkpoint_lock:
            interrupted = self.doing if self.status == "working" and not processed else (getattr(self, "resume_turn", None) if not processed else None)
            atomic_json(self.checkpoint, {"read_upto": cursor, "paused": self.paused, "manual_pause": getattr(self, "manual_pause", False), "error": self.error, "error_kind": getattr(self, "error_kind", None), "interrupted_turn": interrupted, "fail_count": getattr(self, "fail_count", 0), "failure_key": getattr(self, "failure_key", None),
                         "auto_retries": getattr(self, "auto_retries", 0), "retry_job": getattr(self, "retry_job", None),
                         "retry_counts": dict(getattr(self, "retry_counts", {})),
                         "retry_at": getattr(self, "retry_at", 0), "recovery": getattr(self, "recovery", None)}, checkpoint_valid)

    def add_tokens(self, n, cached=0):
        # 두 Worker가 동시에 쓰므로 usage.json 읽기·수정·저장을 잠금으로 보호.
        with open(self.ulock, "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            u = read_json_file(self.ufile, {}, usage_valid)
            v = u.setdefault("usage_v2", {}).setdefault(self.name,
                    {"tokens": self.tokens, "cached_tokens": self.cached_tokens, "since": self.usage_since})
            v["tokens"] = max(v["tokens"], self.tokens) + max(0, n)
            v["cached_tokens"] = max(v["cached_tokens"], self.cached_tokens) + max(0, cached)
            try:
                atomic_json(self.ufile, u, usage_valid)
            except StorageError as error:
                self.notice = "사용량 기록 저장 실패 — 사용량 기록 저장에 실패했습니다. 디스크 용량과 폴더 권한을 확인하세요."
                server_log("usage.cache", error)
            self.tokens, self.cached_tokens = v["tokens"], v["cached_tokens"]
            self.usage_since = v["since"]

    def read_rollout_usage(self):
        thread = getattr(self, "thread", None)
        if not thread:
            return
        if self.rollout_file is None:
            self.rollout_file = next(self.rollout_root.glob(f"**/rollout-*{thread}.jsonl"), None)
        if self.rollout_file is None:
            return
        with self.rollout_file.open("rb") as f:
            if self.rollout_pos > self.rollout_file.stat().st_size:
                self.rollout_pos = 0
            f.seek(self.rollout_pos)
            while True:
                line = f.readline()
                if not line or not line.endswith(b"\n"):
                    break   # 쓰는 중인 미완성 줄은 다음 조회에서 읽음
                self.rollout_pos = f.tell()
                if b'"token_count"' not in line and b'"token_usage_record"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                payload = ev.get("payload") or {}
                if ev.get("type") == "event_msg" and payload.get("type") == "token_count":
                    total = (payload.get("info") or {}).get("total_token_usage")
                    last = (payload.get("info") or {}).get("last_token_usage")
                    if isinstance(last, dict) and last.get("input_tokens"):
                        self.ctx_tokens = last["input_tokens"]   # 마지막 모델 호출에 실린 컨텍스트 크기
                elif ev.get("type") == "token_usage_record":
                    total = payload.get("thread_token_usage")
                else:
                    continue
                if isinstance(total, dict):
                    self.rollout_total = total

    def begin_codex_usage(self, thread):
        with self.usage_lock:
            self.thread = thread
            self.rollout_file, self.rollout_pos, self.rollout_total = None, 0, {}
            try:
                self.read_rollout_usage()
            except OSError:
                pass
            self.rollout_baseline = dict(self.rollout_total)
            if thread and thread == self.rollout_expected_thread:
                self.rollout_baseline = {k: max(self.rollout_baseline.get(k, 0), self.rollout_expected.get(k, 0))
                                         for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
            self.turn_counted = (0, 0)
            self.usage_polled = 0
            self.usage_tracking = True

    def update_codex_usage(self, force=False):
        with self.usage_lock:
            if self.name != "codex" or not self.usage_tracking:
                return
            now = time.monotonic()
            if not force and now - self.usage_polled < 1:
                return
            self.usage_polled = now
            try:
                self.read_rollout_usage()
            except OSError:
                return   # 로그 파일 생성이 늦어져도 실행을 멈추지 않음
            if not self.rollout_total:
                return
            delta = {k: max(0, self.rollout_total.get(k, 0) - self.rollout_baseline.get(k, 0))
                     for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
            cached = delta["cached_input_tokens"]
            tokens = max(0, delta["input_tokens"] - cached) + delta["output_tokens"]
            counted, cache_counted = self.turn_counted
            new_tokens, new_cached = max(0, tokens - counted), max(0, cached - cache_counted)
            if new_tokens or new_cached:
                self.add_tokens(new_tokens, cached=new_cached)
                self.turn_counted = (max(counted, tokens), max(cache_counted, cached))

    def complete_codex_usage(self, usage):
        with self.usage_lock:
            # 재개 완료 이벤트가 thread 누적값이면 시작값을 빼고 이번 차례만 세요.
            baseline = self.rollout_baseline if self.usage_tracking else {}
            keys = ("input_tokens", "cached_input_tokens", "output_tokens")
            cumulative = bool(baseline) and all(usage.get(k, 0) >= baseline.get(k, 0) for k in keys)
            delta = {k: max(0, usage.get(k, 0) - baseline.get(k, 0)) if cumulative else usage.get(k, 0) for k in keys}
            # 완료 직전 rollout도 읽고 stdout에 사용량이 없으면 보완해요.
            if self.usage_tracking:
                self.update_codex_usage(force=True)
                if not usage and self.rollout_total:
                    delta = {k: max(0, self.rollout_total.get(k, 0) - baseline.get(k, 0)) for k in keys}
            cached = delta["cached_input_tokens"]
            tokens = max(0, delta["input_tokens"] - cached) + delta["output_tokens"]
            counted, cache_counted = self.turn_counted if self.usage_tracking else (0, 0)
            self.add_tokens(max(0, tokens - counted), cached=max(0, cached - cache_counted))
            if self.usage_tracking:
                self.rollout_expected_thread = getattr(self, "thread", None)
                self.rollout_expected = {k: max(self.rollout_total.get(k, 0), baseline.get(k, 0) + delta[k]) for k in keys}
            self.usage_tracking = False

    def agent_log(self, text):
        """대화별 에이전트 로그(sessions/<id>/<이름>.log)에 시각과 함께 한 줄 남김"""
        try:
            with open(sdir(self.sid) / f"{self.name}.log", "a") as log:
                log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
        except OSError as error:
            server_log("agent.log", error)

    def tool_done(self, key):
        started = self.open_tools.pop(key, None)
        label = self.tool_labels.pop(key, None) or "도구"
        if key in self.slow_tools:
            self.slow_tools.discard(key)
            self.agent_log(f"오래 걸린 도구 끝남 {int(time.time() - started) if started else '?'}초: {label}")
            if (self.notice or "").startswith("도구 실행 중"):
                self.notice = None

    def watch_slow_tools(self):
        """2분 넘게 결과가 없는 도구를 화면 알림과 로그로 드러냄. 멈춘 건지 일하는 중인지 구분하게"""
        now = time.time()
        slow = [(now - t, k) for k, t in self.open_tools.items() if now - t >= AGENT_SLOW_TOOL]
        if not slow:
            return
        age, key = max(slow)
        label = self.tool_labels.get(key, "도구")
        if key not in self.slow_tools:
            self.slow_tools.add(key)
            server_log("agent.slow_tool")
            self.agent_log(f"도구가 {AGENT_SLOW_TOOL // 60}분 넘게 도는 중: {label}")
        self.notice = f"도구 실행 중 {int(age // 60)}분째 — {label[:80]}"

    def act(self, kind, line):   # kind: text | tool | edit | start | end
        self.activity.append({"ts": time.strftime("%H:%M:%S"), "kind": kind, "text": line[:400]})
        # 리더가 계획 차례에 직접 고치기 시작하면(파일 수정·office reserve/write) 그때부터 '작업 중'으로 표시
        doing = self.doing if isinstance(self.doing, dict) else {}
        step = activity_step(kind, line)
        if doing and step and doing.get("step") != step:
            self.doing = doing = {**doing, "step": step}
        # 실제 실행한 명령만 본다(Claude는 'Bash: …', Codex는 '$ …'). 설명 글·검색어 속 문구는 무시
        runs = kind == "tool" and re.match(r"(?:Bash: |\$ )(?:[^\n]*?(?:&&|;|\|\|)\s*)?office (?:reserve|write)\b", line)
        if doing.get("kind") == "plan" and doing.get("job") and (kind == "edit" or runs):
            self.doing = {**doing, "kind": "work"}
            with state(True, self.sid) as s:
                for j in s["jobs"]:
                    if j["id"] == doing["job"] and j["status"] == "planning":
                        j["status"] = "working"

    # 에이전트 한 차례 실행. 진행상황을 activity로 흘리고 마지막 답을 돌려줌
    def run_agent(self, prompt, job, model_override=None):
        self.rc, self.error, self.notice = 0, None, None
        self.subs.clear()
        self.open_tools = {}   # 결과를 아직 안 받은 도구 호출: 이게 있으면 조용해도 명령이 도는 중
        self.tool_labels, self.slow_tools = {}, set()   # 도구 호출 id → 짧은 설명, 오래 걸린다고 기록한 id
        self.gen = None   # 지금 생성 중인 블록(생각·답·도구 입력)과 받은 글자 수. Claude 부분 메시지로만 채움
        self.codex_message_rows.clear()
        self.claude_result_received = False
        if self.immediate_pause:
            return ""
        with state(sid=self.sid) as record:
            if any(j["id"] == job and j.get("cancelled") for j in record["jobs"]):
                self.interrupted_job = job
                return ""
        if self.interrupted_job != job:
            self.interrupted_job = None
        env = dict(os.environ, OFFICE_AS=self.name, OFFICE_DIR=str(ROOT), OFFICE_SESSION=self.sid)
        env.pop("OFFICE_JOB", None)
        env.pop("OFFICE_DIRECT", None)
        env.pop("OFFICE_REPLY_TO", None)
        if getattr(self, "reply_message_id", None):
            env["OFFICE_REPLY_TO"] = str(self.reply_message_id)
        if job is None:
            env["OFFICE_DIRECT"] = "1"
        self.sid_file = sdir(self.sid) / f"{self.name}.sid"
        if job:
            env["OFFICE_JOB"] = str(job)
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION"):
            env.pop(k, None)
        sid = self.sid_file.read_text().strip() if self.sid_file.exists() else None
        # 기본은 전체 권한(작업 폴더 밖 파일도 읽고 씀). OFFICE_SANDBOX=1 이면 예전처럼 작업 폴더로 제한
        sandbox = os.environ.get("OFFICE_SANDBOX") == "1"
        if self.name == "claude":
            if not sid:
                sid = str(uuid.uuid4())
                cmd = ["claude", "-p", prompt, "--session-id", sid]
            else:
                cmd = ["claude", "-p", prompt, "--resume", sid]
            # 부분 메시지: 긴 생각·긴 파일 쓰기처럼 완성 전까지 출력이 없는 구간도 글자 수로 보여 멈춘 걸로 안 보이게
            cmd += ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]
            cmd += (["--permission-mode", "acceptEdits", "--allowedTools", "Bash(office *)", "Read"] if sandbox
                    else ["--permission-mode", "bypassPermissions"])
        else:
            # git 저장소 아닌 폴더(홈 등)에서도 돌게 --skip-git-repo-check
            cmd = ["codex", "exec", "--skip-git-repo-check"] + (["resume", sid] if sid else [])
            cmd += ["-c", f'sandbox_mode="{"workspace-write" if sandbox else "danger-full-access"}"',
                    "-c", 'model_reasoning_summary="concise"',
                    "-c", "show_raw_agent_reasoning=false"]
            # 작업판이 작업폴더 밖에 있는 경우에만 별도 쓰기 경로 등록.
            if sandbox and not sdir(self.sid).is_relative_to(wdir(self.sid)):
                cmd += ["-c", f"sandbox_workspace_write.writable_roots={json.dumps([str(sdir(self.sid))])}"]
            cmd += ["--json", prompt]
            self.begin_codex_usage(sid)
        pick = getattr(self, "turn_pick", None) or {}
        model, effort = model_override or pick.get("model"), pick.get("effort")
        if effort:
            if self.name == "codex":
                cmd[-1:-1] = ["-c", f'model_reasoning_effort="{effort}"']
            else:
                cmd += ["--effort", effort]
        if model:
            if self.name == "codex":
                cmd[-1:-1] = ["--model", model]
            else:
                cmd += ["--model", model]
        self.turn_effort = effort
        final = ""
        result_at, ended_after_result, self.bg_dropped = None, False, []
        with tempfile.TemporaryFile() as lg:
            # 차례 준비 중 취소·중단됐으면 새 프로세스를 만들지 않는다.
            if self.stop.is_set() or self.interrupted_job is not None or self.immediate_pause:
                self.usage_tracking = False
                return ""
            proc = subprocess.Popen(limited_command(cmd), cwd=wdir(self.sid), env=env, stdout=subprocess.PIPE, stderr=lg,
                                    stdin=subprocess.DEVNULL, start_new_session=True, bufsize=0)
            proc._office_group = True
            try:
                identity, owner = process_identity(proc.pid), process_identity(os.getpid())
                if identity and owner:
                    atomic_json(self.process_file, dict(identity, pid=proc.pid, owner=os.getpid(), owner_start=owner["start"]), process_record_valid)
            except Exception:
                terminate_process(proc)
                if proc.stdout:
                    proc.stdout.close()
                raise
            with self.proc_lock:
                self.proc = proc
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            pending, received = b"", time.monotonic()
            try:
                while True:
                    if self.stop.is_set() or self.interrupted_job is not None or self.immediate_pause:
                        terminate_process(proc)
                        break
                    quiet = time.monotonic() - received
                    running_tool = bool(self.open_tools or self.subs)
                    self.watch_slow_tools()
                    if quiet >= (AGENT_TOOL_TIMEOUT if running_tool else AGENT_QUIET_TIMEOUT):
                        self.error = "응답 대기 시간 초과 — 오랫동안 새 응답이 없어 실행을 중단했습니다. 재개 버튼으로 다시 시도하세요."
                        server_log("agent.timeout")
                        terminate_process(proc)
                        break
                    if result_at and time.monotonic() - result_at >= RESULT_GRACE and proc.poll() is None \
                            and not any(v.get("kind") == "agent" for v in self.subs.values()):
                        # 최종 답을 냈는데 차례 안에서 띄운 백그라운드 명령 때문에 안 끝나는 경우: 차례를 끝낸다.
                        # Claude는 끝날 때 그 명령도 정리하므로 무엇이 정리됐는지 대화에 남긴다(오래 걸리는 일은 office bg)
                        self.bg_dropped = [v["desc"] for v in self.subs.values()]
                        ended_after_result = True
                        self.agent_log("최종 답 뒤 끝나지 않아 차례 종료" + (f" · 정리된 백그라운드 {len(self.bg_dropped)}개" if self.bg_dropped else ""))
                        terminate_process(proc)
                        break
                    if quiet >= AGENT_QUIET_NOTICE and not running_tool:
                        self.notice = "응답 대기 중 — 새 응답을 기다리는 중입니다. 오래 걸리면 연결 상태를 확인하세요."
                    if not selector.select(.5):
                        if proc.poll() is not None:
                            break
                        continue
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    received = time.monotonic()
                    self.last_output = time.time()
                    if self.notice == "응답 대기 중 — 새 응답을 기다리는 중입니다. 오래 걸리면 연결 상태를 확인하세요.":
                        self.notice = None
                    pending += chunk
                    if len(pending) > 2 * 1024 * 1024:
                        raise ValueError("agent output limit")
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(ev, dict):
                            continue
                        final = (self.parse_claude(ev) if self.name == "claude" else self.parse_codex(ev, sid)) or final
                        if self.name == "claude" and ev.get("type") == "result":
                            result_at = time.monotonic() if self.claude_result_received else None
                proc.wait(timeout=2)
            finally:
                selector.close()
                if proc.poll() is None:
                    terminate_process(proc)
                self.rc = 0 if ended_after_result else proc.returncode
                lg.seek(0, os.SEEK_END)
                lg.seek(max(0, lg.tell()-65536))
                diagnostic = safe_agent_diagnostic(lg.read().decode(errors="replace"))
                self.last_diagnostic = {"at": time.time(), "exit_code": self.rc, "stderr": diagnostic[-1200:], "job": job}
                if diagnostic and not ended_after_result:
                    with open(sdir(self.sid) / f"{self.name}.log", "a") as log:
                        log.write(diagnostic)
                    if self.rc and not self.error:
                        self.error = diagnostic.strip()[-400:]
                proc.stdout.close()
                with self.proc_lock:
                    if self.proc is proc:
                        self.proc = None
                self.update_codex_usage(force=True)
                self.usage_tracking = False
                self.process_file.unlink(missing_ok=True)
                release_reservations(self.sid, job, self.name)
        if self.name == "claude" and self.claude_result_received and self.rc == 0 and not self.error:
            self.sid_file.write_text(sid)
        return final.strip()


    def stream_progress(self, e, parent):
        """부분 메시지로 지금 생성 중인 블록을 따라간다. 보조 에이전트 생성은 보조 작업 줄로 따로 보인다"""
        if parent or not isinstance(e, dict):
            return
        kind = e.get("type")
        if kind == "content_block_start":
            block = e.get("content_block") or {}
            what = {"thinking": "thinking", "redacted_thinking": "thinking", "text": "text", "tool_use": "tool"}.get(block.get("type"))
            self.gen = {"kind": what, "name": str(block.get("name") or "")[:40], "chars": 0, "since": time.time()} if what else None
        elif kind == "content_block_delta" and self.gen:
            d = e.get("delta") or {}
            grown = len(d.get("text") or d.get("thinking") or d.get("partial_json") or "")
            if grown:
                self.gen = {**self.gen, "chars": self.gen["chars"] + grown}   # 화면 스냅샷이 도중에 읽어도 깨지지 않게 통째로 바꿈
        elif kind in ("content_block_stop", "message_stop"):
            self.gen = None

    def parse_claude(self, ev):
        if ev.get("type") == "stream_event":
            self.stream_progress(ev.get("event"), ev.get("parent_tool_use_id"))
            return None
        if ev.get("type") == "system" and ev.get("subtype") == "init" and isinstance(ev.get("model"), str):
            self.primary_model = ev["model"]
        if ev.get("type") == "system" and ev.get("subtype") == "task_started":
            # 서브 에이전트·백그라운드 작업 시작. 백그라운드로 띄우면 도구 결과가 바로 오므로 이 이벤트가 실제 시작이다
            key = ev.get("tool_use_id") or ev.get("task_id")
            if key:
                if key not in self.subs:
                    desc = str(ev.get("description") or "보조 작업")[:120]
                    self.subs[key] = {"desc": desc, "since": time.time(), "kind": "bg"}   # 백그라운드 명령: 차례가 끝나면 Claude가 같이 정리함
                    self.act("sub", "보조 시작: " + desc)
                self.subs[key]["task"] = ev.get("task_id")
        elif ev.get("type") == "system" and ev.get("subtype") in ("task_notification", "task_updated"):
            status = ev.get("status") or (ev.get("patch") or {}).get("status")
            if status in ("completed", "failed", "killed", "stopped", "cancelled", "error"):
                key = ev.get("tool_use_id") if ev.get("tool_use_id") in self.subs else next(
                    (k for k, v in self.subs.items() if ev.get("task_id") and v.get("task") == ev.get("task_id")), None)
                sub = self.subs.pop(key, None) if key else None
                if sub:
                    self.act("sub", "보조 종료: " + sub["desc"])
        if ev.get("type") == "assistant":
            u = ev.get("message", {}).get("usage")
            if isinstance(u, dict) and not ev.get("parent_tool_use_id"):   # 보조 에이전트 말고 본 세션의 지금 컨텍스트 크기
                self.ctx_tokens = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "text" and c["text"].strip():
                    self.act("text", c["text"].strip())
                elif c.get("type") == "tool_use":
                    if c.get("id"):
                        self.open_tools[c["id"]] = time.time()
                    i = c.get("input", {})
                    if c.get("name") in ("Agent", "Task") and c.get("id"):
                        desc = str(i.get("description") or i.get("prompt") or "보조 작업")[:120]
                        self.subs[c["id"]] = {"desc": desc, "since": time.time(), "kind": "agent"}
                        self.act("sub", "보조 시작: " + desc)
                    arg = str(i.get("command") or i.get("file_path") or i.get("pattern")
                              or json.dumps(i, ensure_ascii=False)[:150]).replace(str(wdir(self.sid)) + "/", "")
                    self.act("edit" if c["name"] in ("Edit", "Write", "MultiEdit") else "tool", f"{c['name']}: {arg}")
                    if c.get("id"):
                        self.tool_labels[c["id"]] = f"{c['name']}: {arg}"[:160]
        elif ev.get("type") == "user":
            for c in ev.get("message", {}).get("content", []):
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    self.tool_done(c.get("tool_use_id"))
                if isinstance(c, dict) and c.get("type") == "tool_result" and not self.async_launch(c) \
                        and (self.subs.get(c.get("tool_use_id")) or {}).get("kind") != "bg":   # 백그라운드 명령은 끝남 알림 때 뺀다
                    sub = self.subs.pop(c.get("tool_use_id"), None)
                    if sub:
                        self.act("sub", "보조 종료: " + sub["desc"])
        elif ev.get("type") == "rate_limit_event":
            w = (ev.get("rate_limit_info") or {}).get("unifiedWindows") or {}
            record_limits("claude", {k: {"used": round(v.get("utilization", 0) * 100), "resets": v.get("resetsAt")}
                                     for k, v in w.items() if k in ("five_hour", "seven_day")}, source="stream")
        elif ev.get("type") == "result":
            self.claude_result_received = not bool(ev.get("is_error"))
            if ev.get("is_error"):
                self.error = str(ev.get("result") or ev.get("errors") or "에이전트 실행 오류")[:400]
            u = ev.get("usage") or {}
            self.add_tokens(sum(u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "output_tokens")),
                            cached=u.get("cache_read_input_tokens", 0))
            return ev.get("result", "")

    @staticmethod
    def async_launch(result):
        """백그라운드로 띄운 서브 에이전트의 '시작됨' 도구 결과인지 (이때는 아직 안 끝났다)"""
        content = result.get("content")
        text = content if isinstance(content, str) else " ".join(x.get("text", "") for x in content if isinstance(x, dict)) if isinstance(content, list) else ""
        return text.lstrip().startswith("Async agent launched")

    def codex_public_message(self, item):
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        key = item.get("id")
        row = self.codex_message_rows.get(key) if key else None
        if row is None:
            row = {"ts": time.strftime("%H:%M:%S"), "kind": "text", "text": text[:400]}
            self.activity.append(row)
            if key:
                self.codex_message_rows[key] = row
                if len(self.codex_message_rows) > 64:
                    self.codex_message_rows.pop(next(iter(self.codex_message_rows)))
        elif row["text"] != text[:400]:
            row.update(text=text[:400], ts=time.strftime("%H:%M:%S"))

    def codex_progress(self, item):
        """진행 상태·명시적 공개 요약만 사용한다. 추론 원문/암호화 내용은 읽지 않는다."""
        kind = item.get("type")
        if kind == "reasoning":
            summary = item.get("summary")
            if isinstance(summary, str):
                candidates = [summary]
            elif isinstance(summary, list):
                candidates = [x.get("text") for x in summary if isinstance(x, dict) and x.get("type") == "summary_text"]
            else:
                candidates = []
            public = next((x.strip().splitlines()[0] for x in candidates if isinstance(x, str) and x.strip()), "")
            text = "진행 검토: " + safe_agent_diagnostic(public)[:180] if public else "작업 방법 검토 중"
        else:
            tasks = item.get("items") or item.get("plan")
            if not isinstance(tasks, list):
                return
            tasks = [x for x in tasks if isinstance(x, dict) and isinstance(x.get("text") or x.get("step"), str) and (x.get("text") or x.get("step")).strip()]
            if not tasks:
                return
            completed = lambda x: x.get("completed") is True or x.get("status") == "completed"
            done = sum(completed(x) for x in tasks)
            active = next((x for x in tasks if x.get("status") == "in_progress"), None)
            active = active or next((x for x in tasks if not completed(x)), None)
            text = f"작업 단계 {done}/{len(tasks)} 완료"
            if active:
                text += " · " + safe_agent_diagnostic(active.get("text") or active["step"]).strip().splitlines()[0][:180]
        key = item.get("id")
        self.codex_public_message({"id": f"progress:{kind}:{key}" if key else f"progress:{kind}", "text": text})

    def parse_codex(self, ev, sid):
        t, it = ev.get("type"), ev.get("item") or {}
        if (t or "").startswith("item.") or t in ("turn.completed", "thread.started", "token_count"):
            self.notice = None   # 정상 이벤트가 돌아오면 연결 복구 안내를 지움
        if t in ("turn.failed", "error"):
            err = ev.get("error") or ev.get("message") or "에이전트 실행 오류"
            reason = str(err.get("message", err) if isinstance(err, dict) else err)[:400]
            if t == "error" and re.search(r"Reconnecting\.\.\.\s+\d+/\d+", reason):
                self.notice = "연결 재시도 중 — 연결을 다시 시도 중입니다. 잠시 기다려주세요."
            else:
                self.notice = None
                self.error = reason
        elif t == "thread.started":
            self.thread = ev["thread_id"]
            if not sid:
                self.sid_file.write_text(ev["thread_id"])
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            self.complete_codex_usage(u)
            codex_limits(getattr(self, "thread", None))
        elif t in ("item.started", "item.updated", "item.completed") and it.get("type") in ("reasoning", "todo_list", "plan"):
            self.codex_progress(it)
        elif t in ("item.started", "item.updated") and it.get("type") == "agent_message":
            self.codex_public_message(it)
        if t == "item.started" and it.get("type") in ("command_execution", "mcp_tool_call", "dynamic_tool_call", "web_search", "collab_tool_call") and it.get("id"):
            self.open_tools[it["id"]] = time.time()
        elif t == "item.completed" and it.get("id"):
            self.tool_done(it["id"])
        if t == "item.started" and it.get("type") == "command_execution":
            cmd = safe_agent_diagnostic(str(it.get("command", "")))
            m = re.match(r"^/bin/(?:ba)?sh -lc (['\"])(.*)\1$", cmd, re.S)   # codex가 씌우는 셸 껍데기 벗김
            self.act("tool", "$ " + (m.group(2) if m else cmd))
            if it.get("id"):
                self.tool_labels[it["id"]] = ("$ " + (m.group(2) if m else cmd))[:160]
        elif t == "item.started" and it.get("type") in ("mcp_tool_call", "dynamic_tool_call", "web_search", "collab_tool_call"):
            kind = it["type"]
            if kind == "web_search":
                detail = "검색: " + str(it.get("query") or "웹 검색")[:100]
            elif kind == "collab_tool_call":
                detail = "협업: " + str(it.get("tool") or "작업 전달")[:100]
            else:
                namespace = it.get("server") or it.get("namespace") or "도구"
                detail = "MCP: " + str(namespace)[:50] + "/" + str(it.get("tool") or "호출")[:50]
            args = it.get("arguments") or it.get("input")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = None
            if isinstance(args, dict):
                value = next((args[k] for k in ("cmd", "command", "file_path", "path", "query", "description") if isinstance(args.get(k), str)), "")
                if value:
                    detail += ": " + safe_agent_diagnostic(value).replace(str(wdir(self.sid)) + "/", "")[:240]
            self.act("tool", detail)
        elif t == "item.completed":
            if it.get("type") == "command_execution":
                code = it.get("exit_code")
                status = "완료" if code in (0, None) and it.get("status") != "failed" else f"종료 {code}" if code is not None else "실패"
                command = safe_agent_diagnostic(str(it.get("command") or "명령"))
                self.act("tool", f"명령 {status}: {command}")
            elif it.get("type") == "file_change":
                self.act("edit", ", ".join(c.get("path", "?").replace(str(wdir(self.sid)) + "/", "")
                                           for c in it.get("changes", [])))
            elif it.get("type") == "agent_message":
                self.codex_public_message(it)
                return it.get("text", "")

    def next_turn(self, s):
        """이번에 할 일 있으면 (prompt 꼬리, job id, 설명, 새 메시지) 반환"""
        cancelled = {j["id"] for j in s["jobs"] if j.get("cancelled")}
        new = [m for m in s["msgs"][self.read_upto:] if m.get("kind", "msg") == "msg"
               and m.get("job") not in cancelled
               and (m["from"] not in (self.name, "system") or m.get("wake") and m["to"] == self.name) and m["to"] in (self.name, "all")
               and not (m["from"] != "user" and m["text"].strip() in ("대기", "확인", "확인했음", "알겠습니다", "고마워"))]
        body = "\n".join(f"- {m['from']}: {m['text']}" for m in new)
        jobs = [j for j in s["jobs"] if j["status"] != "done"]
        mine_lead = [j for j in jobs if j.get("acting_lead", j["lead"]) == self.name]
        planning = {j["id"] for j in jobs if j["status"] == "planning"}
        tasks = [t for t in s["tasks"] if t["status"] not in ("done", "waiting")]   # 기다림 작업은 재촉하지 않는다
        waiting = [t for t in s["tasks"] if t["status"] == "waiting" and t["owner"] == self.name]
        mine = [t for t in tasks if t["owner"] == self.name]
        free = [t for t in tasks if t["owner"] is None and t.get("job") not in planning]

        open_ids = {j["id"] for j in jobs if not j.get("cancelled")}
        self.retry_counts = {k: v for k, v in getattr(self, "retry_counts", {}).items() if k == "direct" or k in {str(j) for j in open_ids}}
        retry = getattr(self, "retry_due", None)
        if retry is not None:   # 일시 오류 자동 재시도: 기획·검수·기다림 판정보다 먼저 끊긴 차례를 다시 실행
            self.retry_due = None
            # 기획·검수·답장 차례가 아직 그대로 유효하면 아래에서 원래 프롬프트로 다시 골라진다(읽기 위치도 되돌림)
            rj = next((j for j in jobs if j["id"] == retry.get("job")), None)
            natural = retry.get("kind") == "reply" or rj is not None and (
                retry.get("kind") == "plan" and rj["status"] == "planning"
                or retry.get("kind") == "review" and all(t["status"] == "done" for t in s["tasks"] if t.get("job") == rj["id"]))
            if not natural and (retry.get("job") is None or retry.get("job") in open_ids):
                return "이전 차례가 일시 오류로 끊겼다. 이어서 해라." + (f"\n\n새로 온 메시지:\n{body}" if new else ""), retry.get("job"), retry.get("kind") or "work", new
        direct = [m for m in new if m["from"] == "user" and m["to"] == self.name and m.get("job") is None]
        if direct:
            self.cont = 0
            return f"직접 받은 단독 메시지를 처리해라. 팀 요청의 리더 작업과 별개다.\n새로 온 메시지:\n{body}", None, "reply", new

        # 기획·검수 차례에도 그 사이 온 메시지를 실어 보낸다 (안 실으면 읽은 것으로 처리돼 사라짐)
        extra = f"\n\n새로 온 메시지:\n{body}" if new else ""
        for j in mine_lead:
            if j["status"] == "planning":
                return PLAN.format(jid=j["id"], text=j["text"]) + "\n지난 배분 교훈:\n" + lessons_text() + extra, j["id"], "plan", new
            jt = [t for t in s["tasks"] if t.get("job") == j["id"]]
            if j["status"] in ("working", "review") and all(t["status"] == "done" for t in jt):
                return REVIEW.format(jid=j["id"], text=j["text"]) + "\n이번 배분 측정:\n" + task_metrics(s, j["id"]) + '\n배분 관점 교훈 한 줄을 office lesson "..."으로 남겨라. 누락된 시각은 미측정이므로 0초 성과로 해석하지 마라.' + extra, j["id"], "review", new
        assigned = mine or free or waiting
        job = assigned[0].get("job") if assigned else (jobs[0]["id"] if jobs else None)
        queue = [x for x in getattr(self, "stall_prompts", []) if x[1] in open_ids]   # 끝난·취소된 요청의 점검은 버림
        if queue:   # 진행 멈춤 점검: 서버가 리더에게 주는 차례 (요청별로 하나씩)
            text, stall_job = queue.pop(0)
            self.stall_prompts = queue
            return text + (f"\n\n새로 온 메시지:\n{body}" if new else ""), stall_job, "work", new
        self.stall_prompts = []
        if new:
            self.cont = 0
            self.nudge_state = {}
            held = "".join(f"\n- #{t['id']} (요청 {t.get('job')}): {t.get('wait_reason') or '기다리는 중'}" for t in waiting)
            if held:
                held = "\n\n기다리는 중인 네 작업:" + held + "\n기다리던 결과가 왔으면 `office claim ID`로 다시 시작하거나 끝냈으면 `office done ID`. 아직이면 그대로 두고 차례를 끝내라."
            source = next((m for m in reversed(new) if m["from"] == "user"), new[-1])
            return f"새로 온 메시지:\n{body}{held}", source.get("job"), ("reply" if not mine and not free and not waiting else "work"), new
        if not (free or mine):
            return None
        # 새 메시지 없이 남은 작업만 있으면 요청별로 재촉한다. 재촉은 그 요청에 진척(작업 상태 변화·동료 활동)이
        # 있을 때만 다시 한다. 진척 없이 되풀이하면 토큰만 쓰고, 멈춰 세우면 동료 답이 와도 못 깨어난다.
        # 시작 안 한 작업이 있는 요청을 먼저 보고, 한 요청이 막혀도 다른 요청은 진행한다.
        self.nudge_state = state_by_job = {k: v for k, v in getattr(self, "nudge_state", {}).items() if k in open_ids or k is None}
        pending = free + mine
        order = sorted({t.get("job") for t in pending}, key=lambda j: (not any(t["status"] == "todo" and t.get("job") == j for t in pending),
                                                                       min(t["id"] for t in pending if t.get("job") == j)))
        for jid in order:
            tasks_here = [t for t in pending if t.get("job") == jid]
            todo = any(t["status"] == "todo" for t in tasks_here)
            related = sum(m.get("job") == jid and m["from"] not in (self.name, "system") for m in s["msgs"])
            sig = (related, tuple((t["id"], t["status"], t["owner"]) for t in s["tasks"] if t.get("job") == jid))
            prev_sig, count = state_by_job.get(jid, (None, 0))
            if sig != prev_sig:
                count = 0
            if count >= MAX_CONT or not todo and self.waiting_on_peer(s, [t for t in tasks_here if t["owner"] == self.name]):
                state_by_job[jid] = (sig, count)
                continue
            state_by_job[jid] = (sig, count + 1)
            tail = "할 일이 있다. 작업판 보고 진행해라." if todo else "하던 작업이 아직 안 끝났다. 이어서 해라."
            return tail, jid, ("work" if todo else "continue"), new
        return None

    def waiting_on_peer(self, s, doing_tasks):
        """이 요청에서 진행 중 작업을 잡은 뒤 동료에게 보낸 메시지에 아직 답이 없으면 답을 기다리는 중"""
        doing = [t for t in doing_tasks if t["status"] == "doing"]
        if not doing:
            return False
        jobs = {t.get("job") for t in doing}
        since = min(t.get("claimed_at", 0) for t in doing)
        for m in reversed(s["msgs"]):
            if m.get("kind", "msg") != "msg" or m.get("job") not in jobs:
                continue
            if m["from"] == self.other and m["to"] in (self.name, "all"):
                return False
            if m["from"] == self.name and m["to"] in (self.other, "all"):
                return m.get("at", time.time()) >= since
        return False

    def idle(self, status="idle"):
        if self.status != status:
            self.status, self.since, self.doing = status, time.time(), None

    def progress_signature(self, record):
        tasks = [{k: v for k, v in task.items() if k not in ("wait_stale", "wait_overdue")}
                 for task in record["tasks"]]
        visible = [(m["id"], m.get("text")) for m in record["msgs"] if m.get("from") == self.name
                   and m.get("kind") in ("progress", "report")]
        try:
            root = wdir(self.sid)
            result = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root,
                                    capture_output=True, text=True, timeout=2)
            files = []
            for line in result.stdout.splitlines()[:300]:
                path = root / line[3:].split(" -> ")[-1].strip('"')
                try:
                    stat = path.stat()
                    files.append((str(path), stat.st_mtime_ns, stat.st_size))
                except OSError:
                    files.append((str(path), None, None))
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            files, head = [], None
        return hashlib.sha256(json.dumps([tasks, visible[-20:], files, head, record.get("work_revision", 0)], sort_keys=True).encode()).hexdigest()

    def set_recovery(self, phase, reason, attempt=0, retry_at=None):
        self.recovery = {"phase": phase, "reason": safe_agent_diagnostic(reason)[:400],
                         "attempt": attempt, "limit": len(AUTO_RETRY_DELAYS), "at": time.time(), "retry_at": retry_at}


    def pause_with_error(self, reason, job):
        self.error_kind = classify_agent_error(reason, self.rc)
        self.resume_turn = self.doing if isinstance(self.doing, dict) else getattr(self, "resume_turn", None)
        self.error = auth_help(self.name) if self.error_kind == "auth" else safe_agent_diagnostic(reason)
        reason = self.error
        key = hashlib.sha256((self.error_kind + ":" + reason).encode()).hexdigest()
        self.fail_count = self.fail_count + 1 if self.failure_key == key else 1
        self.failure_key = key
        self.set_recovery("diagnosing", reason)
        retry_key = "direct" if job is None else str(job)
        retries = self.retry_counts.get(retry_key, 0)
        # The previous process is already reaped by run_agent. Keep the same cursor and resume turn.
        if self.error_kind in ("crash", "unknown", "network", "timeout", "capacity") and retries < len(AUTO_RETRY_DELAYS) and not self.stop.is_set() and not self.immediate_pause and not getattr(self, "manual_pause", False):
            delay = AUTO_RETRY_DELAYS[retries]
            self.retry_counts[retry_key] = retries + 1
            self.auto_retries, self.retry_job = retries + 1, job
            self.retry_at = time.time() + delay
            self.retry_due = None
            self.read_upto = getattr(self, "turn_read_start", self.read_upto)
            self.paused = False
            self.set_recovery("recovering", reason, retries + 1, self.retry_at)
            self.notice = f"자동 복구 중 — {delay}초 뒤 중단된 차례를 확인하고 이어가요."
            self.error, self.error_kind = None, None
            self.idle()
            self.save_checkpoint()
            with state(True, self.sid) as s:
                event(s, f"{disp(self.name)} 자동 복구 · {retries + 1}/{len(AUTO_RETRY_DELAYS)}회 · {delay}초 뒤 재개를 확인해요. 원인: {reason[:200]}", job=job)
            return
        self.paused = True
        self.set_recovery("needs_user" if self.error_kind in ("auth", "limit") else "failed", reason, retries)
        self.idle("paused")
        self.save_checkpoint()
        if self.error_kind == "auth" and LOGIN_HOOK:
            threading.Thread(target=LOGIN_HOOK, args=(self.sid, [self.name], True), daemon=True).start()
        with state(True, self.sid) as s:
            if self.error_kind == "auth":
                task = next((t for t in s["tasks"] if t.get("job") == job and t.get("owner") == self.name and (t.get("user_request") or {}).get("source") == "agent_auth" and pending_user_request(t)), None)
                task = task or next((t for t in s["tasks"] if t.get("job") == job and t.get("owner") == self.name and t["status"] != "done" and not pending_user_request(t)), None)
                if task is None:
                    s["seq"] += 1
                    task = {"id": s["seq"], "job": job, "owner": self.name, "title": disp(self.name) + " 로그인 확인", "status": "todo", "created_at": time.time(), "auth_task": True}
                    s["tasks"].append(task)
                task["user_request"] = {"status": "pending", "text": auth_help(self.name), "created_at": time.time(), "expires_at": None,
                                        "resolved_at": None, "resolution": None, "reply_to": None, "source": "agent_auth", "resume_status": (task.get("user_request") or {}).get("resume_status", task["status"])}
                task.update(status="waiting", waiting_at=time.time(), wait_reason=auth_help(self.name))
                task.pop("wait_deadline", None)
            guide = "" if re.search(r"(?:다시 시도|재개|시작)(?:하세요|을 누르세요)\.?$", reason.strip()) else " 확인 후 재개 버튼으로 다시 시도하세요."
            post(s, "system", "user", ui_notice(f"{disp(self.name)} 일시정지", f"{reason}{guide}"),
                 "event", job=job)

    def transient_failure(self, why):
        """잠깐 기다리면 풀릴 오류인지: 연결 끊김·시간 초과·혼잡·서버 5xx·결과 없이 끝난 실행"""
        text = str(why or "")
        kind = classify_agent_error(text, self.rc)
        if kind in ("network", "timeout", "capacity"):
            return True
        if re.search(r"\b(?:500|502|503|504)\b|internal server error|bad gateway|service unavailable|overloaded", text, re.I):
            return True
        return not text or text.startswith("응답 없는 실행 종료")

    def terminate_agent(self):
        with self.proc_lock:
            proc = self.proc
        if proc is not None:
            terminate_process(proc)

    def loop(self, office):
        while not (self.stop.is_set() or office.stop.is_set()):
            try:
                self._loop(office)
            except Exception as error:
                self.terminate_agent()
                self.read_upto = getattr(self, "turn_read_start", self.read_upto)
                self.resume_turn = self.doing if isinstance(self.doing, dict) else self.resume_turn
                server_log("worker.loop", error)
                reason = str(error) if isinstance(error, UserError) else f"현재 차례 처리 중 {type(error).__name__} 오류가 생겼어요."
                try:
                    self.pause_with_error(reason, (self.resume_turn or {}).get("job"))
                except (OSError, StorageError) as storage_error:
                    self.paused, self.error = True, "기록 저장을 확인하지 못해 자동 복구를 멈췄어요. 디스크와 폴더 권한을 확인하세요."
                    self.error_kind = "unknown"
                    self.set_recovery("failed", self.error)
                    self.idle("paused")
                    server_log("worker.error.persist", storage_error)
                self.stop.wait(POLL)

    def rotate_session(self, job, desc):
        """세션이 길어지면 차례마다 이전 대화를 통째로 다시 읽어 느려지고 사용량도 많이 든다.
        새 요청을 처음 맡는 차례에서 컨텍스트가 ROTATE_CTX 토큰을 넘었으면 새 세션으로 시작한다(요청 도중엔 안 바꿈)"""
        new_request = job is not None and job != getattr(self, "ctx_job", None)
        if new_request:
            self.ctx_job = job
        if not new_request or getattr(self, "ctx_tokens", 0) <= ROTATE_CTX:
            return False
        (sdir(self.sid) / f"{self.name}.sid").unlink(missing_ok=True)
        self.agent_log(f"대화가 길어져 새 세션으로 이어서 시작해요 (요청 {job}, 기억 {self.ctx_tokens:,}토큰 > 기준 {ROTATE_CTX:,}토큰). 최근 요청·보고는 넘겨줘요")
        server_log("agent.rotate." + self.name)
        self.ctx_tokens = 0
        return True

    def state_mark(self):
        try:
            st = (sdir(self.sid) / "state.json").stat()
            return st.st_mtime_ns, st.st_size, st.st_ino
        except OSError:
            return None

    def wait_change(self, timeout):
        """할 일이 없을 때 쉬는 시간. 작업판·메시지(state.json)가 바뀌면 바로 깨어나 다음 차례를 빨리 잡는다.
        office 명령은 별도 프로세스라 파일 변화로만 알 수 있어 0.1초마다 수정 시각만 본다(읽기·해석은 안 함)"""
        path = sdir(self.sid) / "state.json"
        def mark():
            try:
                st = path.stat()
                return st.st_mtime_ns, st.st_size
            except OSError:
                return None
        before, end = mark(), time.monotonic() + timeout
        while not self.stop.is_set() and time.monotonic() < end:
            if self.stop.wait(0.25) or mark() != before:
                return

    def _loop(self, office):
        sid = self.sid
        while not (self.stop.is_set() or office.stop.is_set()):
            mark_now = self.state_mark()
            idle = self.idle_mark
            if idle and mark_now == idle[0] and not self.paused and not getattr(self, "retry_at", 0) and time.monotonic() - idle[1] < IDLE_RECHECK:
                self.wait_change(POLL)   # 할 일이 없고 기록도 그대로면 매초 전체를 읽지 않고, 변화가 생기면 바로 깨어나요
                continue
            self.idle_mark = None
            if hasattr(office, "reconcile_failover"):
                office.reconcile_failover(sid)
            if hasattr(office, "check_stall"):
                office.check_stall(sid)
            if self.paused:
                self.idle("paused"); time.sleep(1); continue
            if getattr(self, "retry_at", 0) > time.time():   # 일시 오류 자동 재시도 대기
                self.idle(); self.stop.wait(POLL); continue
            if getattr(self, "retry_at", 0):   # 대기 끝: 다음 차례로 끊긴 차례를 다시 실행
                self.retry_at = 0
                self.retry_due = dict(self.resume_turn) if isinstance(self.resume_turn, dict) else None   # 취소로 지워졌으면 재시도 안 함
            with state(sid=sid) as s:
                if self.read_upto is None:
                    self.read_upto = len(s["msgs"])
                nt = self.next_turn(s)
                task_level = next((t.get("level") for t in s["tasks"] if nt and t.get("job") == nt[1] and t.get("owner") == self.name
                                   and t["status"] in ("todo", "doing") and t.get("level")), None)
                my_todo = [t["id"] for t in s["tasks"] if t.get("owner") == self.name and t["status"] == "todo"]
                snap, total = board_text(s), len(s["msgs"])
                missed = missed_text(s["msgs"][self.read_upto:], self.name)
                title_history = request_history(s["jobs"])
                carry = recent_results(s["jobs"])
                selected_job = next((j for j in s["jobs"] if nt and j["id"] == nt[1]), {})
                verification = selected_job.get("verification", "standard")
            if not nt:
                self.idle(); self.idle_mark = (mark_now, time.monotonic()); self.wait_change(POLL)
                continue
            tail, job, desc, new = nt
            if self.resume_turn and not self.restart_reported:
                with state(True, sid) as restored:
                    event(restored, "중단된 차례를 다시 실행합니다. 이미 변경한 파일과 완료 작업을 확인하고 이어갑니다.", job=job)
                self.restart_reported = True
            only_chat = desc == "reply" and not any(m["from"] == "user" for m in new)
            with state(sid=sid) as record:
                signature = self.progress_signature(record)
            changed = signature != getattr(self, "chatter_signature", None)
            self.chatter_signature = signature
            self.chatter = self.chatter + 1 if only_chat and not changed else 0
            if self.chatter > MAX_CHATTER:
                self.pause_with_error("자동 진행 일시정지 — 작업 진척 없이 에이전트끼리 대화가 반복되어 자동 진행을 멈췄습니다.", job)
                self.chatter = 0
                continue
            if desc == "review":
                with state(True, sid) as s:
                    for j in s["jobs"]:
                        if j["id"] == job and j["status"] == "working":
                            j["status"] = "review"
            previous_read = self.turn_read_start = self.read_upto
            self.read_upto = total
            self.reply_message_id = next((m["id"] for m in reversed(new) if m["from"] == "user"), new[-1]["id"] if new else None)
            if getattr(self, "recovery", None):
                if self.recovery["phase"] == "recovered":
                    self.recovery = None
                elif self.recovery["phase"] in ("recovering", "verifying"):
                    self.set_recovery("verifying", self.recovery["reason"], self.recovery["attempt"])
            self.turn_progress_signature = signature
            self.rc, self.error, self.error_kind = 0, None, None
            self.model_notice = None
            self.primary_model = configured_model(self.name)
            self.turn_level, self.turn_pick = turn_choice(self.name, "chat" if only_chat else desc, task_level)
            if self.turn_pick.get("model"):
                self.primary_model = self.turn_pick["model"]
            self.last_output = time.time()
            self.status, self.since, self.doing = "working", time.time(), {"kind": desc, "job": job}
            self.save_checkpoint()   # 첫 차례 도중 서버가 꺼져도 재시작 후 이 메시지부터 다시 읽게 시작 위치를 남김
            self.act("start", f"{desc}|{job or ''}")
            rotated = self.rotate_session(job, desc)
            prompt = PROTOCOL.format(me=self.name, other=self.other) + f"\n현재 상황:\n{snap}\n\n"
            verification_scope = {"quick": "빠르게: 요청한 결과와 핵심 동작만 확인", "standard": "기본: 핵심 동작과 영향 받는 부분까지 확인", "thorough": "꼼꼼하게: 추가 사례·예외·독립 검수까지 확인"}.get(verification, "기본")
            prompt += f"이 요청의 검증 수준은 {verification_scope}이다. 통과한 같은 검증은 새 변경·실패 없이 반복하지 마라. 재시도는 입력·코드·설정이 그대로인 완료 결과를 확인해 재사용하고 실패한 부분부터 이어가라. 더 깊은 검증이 필요하면 이유와 추가 예상 시간을 먼저 알려라. 최종 보고에 선택 수준·실제 확인한 범위·확인 못 한 부분을 짧게 남겨라.\n"
            if rotated:
                prompt += ("이번 차례부터 새 세션이다(이전 세션이 너무 길어져 속도를 위해 바꿨다). 이전 대화 내용은 기억나지 않으니 "
                           "위 현재 상황·최근 대화와 아래 요청 이력, 저장소 파일로 맥락을 파악해라.\n\n")
                if carry:
                    prompt += f"이전 세션에서 끝낸 최근 요청과 최종 보고 (이미 한 약속·결정은 이어서 지켜라):\n{carry}\n\n"
            if missed:
                prompt += f"네 지난 차례 이후 네가 직접 받지 못한 대화 (사용자·동료끼리 나눈 내용, 맥락 파악용):\n{missed}\n\n"
            if self.resume_turn and self.resume_turn.get("job") == job:
                if getattr(self, "recovery", None):
                    prompt += "자동 복구 진단: " + self.recovery.get("reason", "") + "\n중단된 실행은 정리됐다. 저장된 결과와 현재 프로세스를 확인하고 실패한 부분만 이어가라. 실행 중인 외부 작업을 중복 시작하지 마라.\n"
                prompt += "이전 차례가 중단되어 이어서 시작한다. 이미 수정한 파일과 완료 작업을 먼저 확인하고, 반쯤 진행한 작업은 검증해서 이어가라. 같은 변경을 반복하거나 덮어쓰지 마라.\n"
                prompt += "이전 실행의 파일 예약은 풀렸다. 파일을 고치기 전에 office reserve 를 다시 하고, office write 결과를 확인한 뒤에만 완료 처리해라.\n"
            prompt += "메시지에 이미지·파일 경로가 있으면 해당 파일을 직접 열어서 확인해라. Claude는 Read, Codex는 이미지 보기 도구를 사용한다.\n"
            peer = office.teams.get(sid, {}).get("w", {}).get(self.other) if hasattr(office, "teams") else None
            if peer is not None and peer.status == "idle" and not peer.paused and len(my_todo) > 1:
                prompt += f"동료 {self.other}는 지금 쉬는 중이다. 네 몫 중 아직 시작 안 한 작업({', '.join('#' + str(x) for x in my_todo)})은 오래 걸릴 것 같으면 `office handoff ID --to {self.other}`로 넘겨라.\n"
            prompt += f"{tail}\n\n대화 전체 요청 이력 (제목 요약 참고):\n{title_history}"
            turn_started = time.monotonic()
            tokens_before = self.tokens
            try:
                out = self.run_agent(prompt, job)
            except Exception as e:
                out = ""
                self.error = f"에이전트 실행 실패 — 실행 실패 ({type(e).__name__}). 실행 환경을 확인한 뒤 재개하세요."
                server_log("agent.execute", e)
            if self.name == "claude" and missing_claude_session(self.error) and not self.stop.is_set() and self.interrupted_job is None:
                (sdir(self.sid) / "claude.sid").unlink(missing_ok=True)
                self.notice = "새 세션에서 재개 — 이전 세션을 찾지 못해 새 세션에서 작업을 이어갑니다."
                server_log("agent.session_recovery")
                try:
                    out = self.run_agent(prompt + "\n이전 세션 기록이 없어 새 세션으로 재개한다. 작업판과 이미 수정한 파일을 확인하고 이어서 진행해라.", job)
                except Exception as error:
                    out, self.error = "", "새 세션 실행 실패 — 새 세션 실행 실패. 다시 시작 버튼으로 재개하세요."
                    server_log("agent.session_recovery.failed", error)
            if self.error and classify_agent_error(self.error, self.rc) == "capacity" and not self.stop.is_set() and self.interrupted_job is None and fallback_model(self.name, self.primary_model):
                alternate = fallback_model(self.name, self.primary_model)
                self.model_notice = f"모델 임시 전환 — {self.primary_model} 모델 혼잡으로 {alternate}로 임시 전환"
                self.act("start", self.model_notice)
                server_log("agent.model_fallback." + self.name)
                try:
                    out = self.run_agent(prompt + "\n이전 실행이 모델 혼잡으로 중단됐다. 이미 완료한 작업은 반복하지 말고 현재 작업판을 확인해 이어서 진행해라.", job, model_override=alternate)
                except Exception as error:
                    out, self.error = "", "대체 모델 실행 실패 — 대체 모델 실행 실패. 재개 버튼으로 다시 시도하세요."
                    server_log("agent.fallback", error)
            if self.immediate_pause:
                self.immediate_pause = False
                self.read_upto = previous_read
                self.resume_turn = dict(self.doing)
                self.paused, self.error, self.error_kind = True, None, None
                self.idle("paused")
                self.save_checkpoint()
                self.act("end", "")
                continue
            if self.stop.is_set() or not sdir(sid).exists():   # 일하는 사이 대화가 삭제됨
                self.act("end", ""); break
            with state(True, sid=sid) as measured:
                measured_job = next((j for j in measured["jobs"] if j["id"] == job), None)
                if measured_job:
                    metrics = measured_job.setdefault("execution_metrics", {}).setdefault(self.name, {"turns":0,"waiting_replies":0,"seconds":0,"tokens":0})
                    metrics["turns"] += 1
                    metrics["waiting_replies"] += int(out.strip() == "대기")
                    elapsed = round(max(0, time.monotonic()-turn_started), 3)
                    metrics["seconds"] += elapsed
                    phases = metrics.setdefault("phase_seconds", {})
                    phase = "verification" if desc == "review" else "planning" if desc == "plan" else "reply" if desc == "reply" else "execution"
                    phases[phase] = round(phases.get(phase, 0) + elapsed, 3)
                    if measured_job.get("started_at"):
                        metrics.setdefault("first_turn_wait_seconds", round(max(0, self.since - measured_job["started_at"]), 3))
                    metrics["tokens"] += max(0, self.tokens-tokens_before)
            with state(sid=sid) as current:
                cancelled_turn = any(j["id"] == job and j.get("cancelled") for j in current["jobs"])
            if cancelled_turn or self.interrupted_job is not None and self.interrupted_job == job:
                self.interrupted_job = None
                self.error_kind, self.resume_turn = None, None
                self.notice, self.error = None, None
                self.save_checkpoint(processed=True)
                self.act("end", "")
                continue
            touch(sid)
            if self.error or self.rc or not out:
                why = self.error
                if not why and self.rc:
                    why = f"현재 차례가 종료 코드 {self.rc}로 끝났어요. 현재 오류 출력이 없어 원인을 확인하지 못했어요."
                self.read_upto = previous_read
                retry_key = "direct" if job is None else str(job)
                retries = self.retry_counts.get(retry_key, 0)
                if self.transient_failure(why) and retries < len(AUTO_RETRY_DELAYS) and not self.stop.is_set() and not getattr(self, "manual_pause", False):
                    delay = AUTO_RETRY_DELAYS[retries]
                    self.auto_retries = retries + 1
                    self.retry_counts[retry_key] = retries + 1
                    self.retry_job = job
                    self.retry_at = time.time() + delay
                    self.set_recovery("recovering", why or "응답 없이 실행이 끝났어요.", retries + 1, self.retry_at)
                    self.resume_turn = self.doing if isinstance(self.doing, dict) else self.resume_turn
                    self.restart_reported = True
                    self.error, self.error_kind = None, None
                    self.notice = f"자동 재시도 대기 — 일시 오류로 {delay}초 뒤 다시 시도합니다."
                    self.idle()
                    self.save_checkpoint()
                    with state(True, sid) as s:
                        reason = re.sub(r"\s*(?:재개 버튼으로 다시 시도하세요|확인 후 재개 버튼으로 다시 시도하세요)\.?$", "", safe_agent_diagnostic(why or "응답 없이 실행이 끝났습니다.").strip())
                        event(s, f"{disp(self.name)} 일시 오류 · {delay}초 뒤 자동으로 다시 시도합니다 ({retries + 1}/{len(AUTO_RETRY_DELAYS)}) — {reason[:160]}", job=job)
                    server_log("agent.auto_retry." + self.name)
                    self.act("end", ""); continue
                self.pause_with_error(why or "응답 없는 실행 종료 — 응답 없이 실행이 끝났습니다.", job)
                self.act("end", ""); continue
            with state(True, sid) as s:
                sent_to_user = any(m["from"] == self.name and m["to"] == "user"
                                   and m.get("kind") in ("msg", "report") for m in s["msgs"][total:])
                # 진행 알림(msg)은 최종 보고가 아니다. 이 요청의 report 가 있어야 보고한 것으로 본다
                reported = any(m["from"] == self.name and m["to"] == "user" and m.get("kind") == "report"
                               and m.get("job") == job for m in s["msgs"][total:])
                # 검수 후 finish 안 했으면 자동 마감. 단 검수 중 이 요청에 사용자 메시지가 왔으면 다음 차례에 처리하도록 남김
                closing = next((j for j in s["jobs"] if j["id"] == job and j["status"] == "review" and desc == "review"
                                and all(t["status"] == "done" for t in s["tasks"] if t.get("job") == job)
                                and not any(m["from"] == "user" and m.get("job") == job for m in s["msgs"][total:])), None)
                report = None
                if closing and not reported:   # 보고 없이 끝냈으면 사용자에게 결과를 대신 보고
                    report = out if out.strip() not in ("", "대기") else (
                        "리더가 보고 없이 검수를 마쳐 작업 결과로 대신 보고합니다.\n\n"
                        + "\n".join(f"- #{t['id']} {disp(t['owner']) if t.get('owner') else '담당 없음'}: {t.get('note') or t['title']}"
                                    for t in s["tasks"] if t.get("job") == job))
                if out and out.strip() != "대기" and not (job is None and sent_to_user) and report is None:
                    post(s, self.name, "log", out, "turn", job=job)
                for j in s["jobs"]:   # 기획만 하고 아무것도 안 했으면 진행으로 넘김
                    if j["id"] == job and j["status"] == "planning" and desc == "plan":
                        j["status"] = "working"
                planned = [t for t in s["tasks"] if t.get("job") == job and t["status"] != "done"] if desc == "plan" and job else []
                told = any(m["from"] == self.name and m["to"] == "user" and m.get("kind") in ("progress", "msg") and m.get("job") == job for m in s["msgs"][total:])
                if len(planned) >= 2 and not told:   # 리더가 계획 알림을 빠뜨려도 누가 무엇을 하는지 한 줄씩 보이게
                    lines = "\n".join(f"- {disp(t['owner']) if t.get('owner') else '담당 없음'}: {t['title'][:80]}" for t in planned)
                    post(s, "system", "user", f"진행 계획 · 작업 {len(planned)}개\n{lines}", "progress", job=job)
                if getattr(self, "bg_dropped", None):
                    names = ", ".join(self.bg_dropped)[:200]
                    post(s, "system", "user", ui_notice("백그라운드 작업 정리됨", f"{disp(self.name)} 차례가 끝나며 차례 안에서 띄운 백그라운드 작업이 같이 멈췄어요: {names}"), "event", job=job)
                    post(s, "system", self.name, f"네 차례가 끝나며 차례 안에서 띄운 백그라운드 명령이 같이 정리됐다: {names}. 계속 돌려야 하면 `office bg ID \"설명\" \"명령\"`으로 다시 띄워라.", "msg", job=job)
                    s["msgs"][-1]["wake"] = True
                    self.bg_dropped = []
                if closing:
                    summary = report or out
                    closing.update(status="done", finished_by=self.name, finished_at=time.time(), summary=summary, brief=report_brief(summary))
                    event(s, f"요청 {job} 완료 (검수 보고로 마감)", self.name, job=job)
                    if report:
                        post(s, self.name, "user", report, "report", job=job)
            if getattr(self, "recovery", None) and self.recovery["phase"] == "verifying":
                self.set_recovery("recovered", "정상 응답과 차례 완료를 확인했어요.", self.recovery["attempt"])
                with state(True, sid) as recovered:
                    event(recovered, f"{disp(self.name)} 자동 복구 완료 · 중단된 차례가 정상으로 끝났어요.", job=job)
            self.resume_turn = None
            self.fail_count, self.failure_key = 0, None
            with state(sid=sid) as record:
                made_progress = self.progress_signature(record) != self.turn_progress_signature
            if made_progress or any(m["from"] == "user" for m in new) or not only_chat:
                self.retry_counts.pop("direct" if job is None else str(job), None)
            if job == getattr(self, "retry_job", None) or not getattr(self, "auto_retries", 0):
                self.auto_retries, self.retry_at, self.retry_job = 0, 0, None
            if (self.notice or "").startswith("자동 재시도 대기"):
                self.notice = None
            self.save_checkpoint(processed=True)
            self.act("end", "")
        self.idle("paused" if self.paused else "idle")


def codex_limits(thread=None):
    """codex exec --json엔 한도 정보가 없어서, codex가 남기는 세션 기록(rollout)의 마지막 rate_limits를 읽음"""
    root = Path.home() / ".codex" / "sessions"
    files = sorted(root.glob(f"**/rollout-*{thread}.jsonl" if thread else "**/rollout-*.jsonl"),
                   key=lambda f: f.stat().st_mtime)
    for f in reversed(files[-5:]):
        for line in reversed(f.read_text(errors="ignore").splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                ev = json.loads(line)
                rl = ev.get("payload", {}).get("rate_limits") or {}
            except ValueError:
                continue
            out = {}
            for k in ("primary", "secondary"):
                w = rl.get(k)
                if w:
                    name = {300: "five_hour", 10080: "seven_day"}.get(w.get("window_minutes"), f"{w.get('window_minutes')}m")
                    out[name] = {"used": round(w.get("used_percent", 0)), "resets": w.get("resets_at")}
            if out:
                try:
                    at = datetime.datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00")).timestamp()
                except (KeyError, ValueError, TypeError):
                    at = f.stat().st_mtime
                record_limits("codex", out, at=at, source="rollout")
                return



class NoUsageRedirect(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None   # 인증 헤더가 다른 호스트로 전달되지 않게 함



CLAUDE_USAGE_AGENT = None


def claude_usage_agent():
    global CLAUDE_USAGE_AGENT
    if CLAUDE_USAGE_AGENT is None:
        executable = shutil.which("claude")
        version = Path(executable).resolve().name if executable else ""
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=3)
            match = re.search(r"\d+\.\d+\.\d+", result.stdout)
            version = match.group() if match else "unknown"
        # 설치 CLI getUserAgent()의 형식과 같은 버전/외부 CLI 표기.
        CLAUDE_USAGE_AGENT = f"claude-cli/{version} (external, cli)"
    return CLAUDE_USAGE_AGENT


def log_limits_failure(name, error=None):
    # 예외 메시지/응답 본문/헤더는 절대 기록하지 않음.
    detail = "no_usage_data" if error is None else type(error).__name__
    if isinstance(error, HTTPError):
        detail += f" HTTP={error.code}"
    elif isinstance(error, URLError):
        reason = error.reason
        detail += f" reason={type(reason).__name__}"
        number = getattr(reason, "errno", None)
        if isinstance(number, int):
            detail += f" errno={number}"
    try:
        D.mkdir(parents=True, exist_ok=True)
        with open(D / "server.log", "a") as log:
            log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] limits {name}: {detail}\n")
    except OSError:
        pass

def provider_ssl_context():
    context = ssl.create_default_context()
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        defaults = ssl.get_default_verify_paths()
        cafile = defaults.openssl_cafile if defaults.openssl_cafile and Path(defaults.openssl_cafile).is_file() else None
        capath = defaults.openssl_capath if defaults.openssl_capath and Path(defaults.openssl_capath).is_dir() else None
        if cafile or capath:
            context.load_verify_locations(cafile=cafile, capath=capath)
    # 파이썬 배포판(conda 등)의 인증서 묶음엔 OS에 추가한 인증서가 없을 수 있어 OS 묶음도 항상 같이 읽는다
    for bundle in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/cert.pem"):
        try:
            if Path(bundle).is_file():
                context.load_verify_locations(cafile=bundle)
                break
        except (OSError, ssl.SSLError):
            continue
    # 명시적으로 지정한 인증서도 모두 추가한다.
    # NODE_EXTRA_CA_CERTS: claude CLI가 쓰는 변수라 이것만 지정된 환경도 많음
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"):
        path = Path(os.environ.get(key) or "")
        try:
            if os.environ.get(key) and path.is_file():
                context.load_verify_locations(cafile=str(path))
        except (OSError, ssl.SSLError):
            continue
    return context


def fetch_claude_limits():
    credentials = json.loads((Path.home() / ".claude" / ".credentials.json").read_text())
    if not isinstance(credentials, dict):
        return None
    oauth = credentials.get("claudeAiOauth") or {}
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    if not token or (oauth.get("expiresAt") and oauth["expiresAt"] <= time.time() * 1000):
        return None
    # 설치된 Claude CLI의 기본 BASE_API_URL 및 /api/oauth/usage 조회 경로.
    req = urlrequest.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": claude_usage_agent()})
    opener = urlrequest.build_opener(NoUsageRedirect(), urlrequest.HTTPSHandler(context=provider_ssl_context()))
    with opener.open(req, timeout=8) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        return None
    out = {}
    for key in ("five_hour", "seven_day"):
        window = data.get(key) or {}
        if not isinstance(window, dict):
            continue
        used = window.get("utilization")
        resets = window.get("resets_at")
        if not isinstance(used, (int, float)):
            continue
        if isinstance(resets, str):
            resets = datetime.datetime.fromisoformat(resets.replace("Z", "+00:00")).timestamp()
        out[key] = {"used": round(used), "resets": resets}
    return out or None


def fetch_codex_limits():
    # 공식 account/rateLimits/read RPC만 호출. 모델 차례나 사용자 작업을 만들지 않음.
    proc = subprocess.Popen(["codex", "app-server", "--listen", "stdio://"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    def send(message):
        proc.stdin.write((json.dumps(message) + "\n").encode()); proc.stdin.flush()
    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {
            "name": "office_limits", "title": "Office limits", "version": "1.0"}}})
        pending = b""
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if not selector.select(.5):
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                return None
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("id") == 1:
                    if "error" in message:
                        return None
                    send({"method": "initialized"})
                    send({"id": 2, "method": "account/rateLimits/read"})
                if message.get("id") != 2:
                    continue
                result = message.get("result") or {}
                if not isinstance(result, dict):
                    return None
                windows = (result.get("rateLimitsByLimitId") or {}).get("codex") or result.get("rateLimits") or {}
                if not isinstance(windows, dict):
                    return None
                out = {}
                for key in ("primary", "secondary"):
                    window = windows.get(key) or {}
                    if not isinstance(window, dict):
                        continue
                    minutes, used = window.get("windowDurationMins"), window.get("usedPercent")
                    if not isinstance(used, (int, float)) or not isinstance(minutes, (int, float)):
                        continue
                    name = {300: "five_hour", 10080: "seven_day"}.get(minutes, f"{minutes}m")
                    out[name] = {"used": round(used), "resets": window.get("resetsAt")}
                return out or None
        return None
    finally:
        selector.close()
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()
        proc.stdin.close(); proc.stdout.close()


def refresh_provider_limits(name, stop):
    fetch = fetch_claude_limits if name == "claude" else fetch_codex_limits
    while not stop.is_set():
        try:
            windows = fetch()
            if windows:
                record_limits(name, windows, source="direct")
            else:
                log_limits_failure(name)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            log_limits_failure(name, error)
        stop.wait(300)

# ── 새 버전 확인·업데이트: 깃허브 최신 릴리스와 비교하고, 설치기로 같은 경로에 다시 설치 ──
UPDATE_REPO = os.environ.get("OFFICE_REPO", "mjsob/agent-office")
UPDATE_ASSET = "office-installer.tar.gz"
UPDATE_LOCK = threading.Lock()
UPDATE = {"latest": None, "notes": "", "url": None, "state": "idle", "message": None, "checked_at": None, "error": None, "date": None, "checking": False, "retry_at": 0, "etag": None}
UPDATE_WAKE = threading.Event()   # 화면의 '지금 확인'이 깨움


def update_error_text(error):
    """확인 실패를 화면에 보여 줄 짧은 사유로. 로그에는 종류·세부를 따로 남긴다"""
    reason = getattr(error, "reason", None)
    if isinstance(error, HTTPError):
        if error.code in (403, 429):
            return "깃허브 조회 한도를 넘었어요"
        return f"깃허브 응답 오류 {error.code}"
    if isinstance(reason, ssl.SSLCertVerificationError) or isinstance(error, ssl.SSLCertVerificationError):
        return "인증서를 확인하지 못했어요"
    if isinstance(reason, (TimeoutError, socket.timeout)) or isinstance(error, (TimeoutError, socket.timeout)):
        return "응답이 없어요"
    if isinstance(reason, socket.gaierror):
        return "인터넷에 연결되지 않았어요"
    return "연결하지 못했어요"


def version_key(text):
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:3]) or (0,)


def install_paths():
    """설치기로 깐 앱이면 (설치 폴더, 작업 폴더). 개발용 사본처럼 표식이 없으면 None"""
    app = Path(__file__).resolve().parent
    prefix = app.parent
    try:
        if app.name != "app" or (prefix / ".office-install").read_text().strip() != "office-bootstrap-v1":
            return None
        launcher = (prefix / "bin" / "office").read_text()
    except OSError:
        return None
    found = re.search(r"^work_dir=(.+)$", launcher, re.M)
    try:
        workspace = shlex.split(found.group(1))[0] if found else str(ROOT)
    except ValueError:
        workspace = str(ROOT)
    return str(prefix), workspace


def update_info():
    with UPDATE_LOCK:
        info = dict(UPDATE)
    if info.get("error") and (info.get("retry_at") or 0) > time.time():
        minutes = max(1, math.ceil((info["retry_at"] - time.time()) / 60))
        info["error"] = f"깃허브 조회 한도를 넘었어요. {minutes}분 뒤 다시 확인해요"
    current = office_version()
    info["current"] = current
    info["available"] = bool(info["latest"]) and version_key(info["latest"]) > version_key(current)
    info["installable"] = install_paths() is not None
    if info["available"] and not info["installable"] and info["state"] == "idle":
        info["message"] = "설치기로 설치한 Office에서만 바로 업데이트할 수 있어요."
    return info


def update_retry_at(error):
    now = time.time()
    times = [now + 60]
    headers = getattr(error, "headers", None) or {}
    for name, relative in (("X-RateLimit-Reset", False), ("Retry-After", True)):
        try:
            value = float(headers.get(name, ""))
            if math.isfinite(value) and value > 0:
                times.append(now + value if relative else value)
        except (TypeError, ValueError):
            pass
    return max(times)


UPDATE_CACHE_FIELDS = ("latest", "notes", "url", "date", "checked_at", "etag", "retry_at", "error")


def load_update_cache():
    try:
        saved = read_json_file(D / "update-cache.json", {})
        if not isinstance(saved, dict) or saved.get("repo") != UPDATE_REPO:
            return
        stamp = saved.get("checked_at")
        if stamp is not None and (type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0):
            return
        retry = saved.get("retry_at", 0)
        if type(retry) not in (int, float) or not math.isfinite(retry) or retry < 0:
            return
        for key in ("latest", "notes", "url", "date", "etag", "error"):
            if saved.get(key) is not None and not isinstance(saved[key], str):
                return
        with UPDATE_LOCK:
            UPDATE.update({k: saved[k] for k in UPDATE_CACHE_FIELDS if k in saved})
            UPDATE["checking"] = False
    except (OSError, UserError) as error:
        server_log("update.cache.load", error)


def save_update_cache():
    try:
        with UPDATE_LOCK:
            saved = {"repo": UPDATE_REPO, **{k: UPDATE.get(k) for k in UPDATE_CACHE_FIELDS}}
        atomic_json(D / "update-cache.json", saved)
    except (OSError, UserError) as error:
        server_log("update.cache.save", error)


def check_update(stop):
    """최근 성공 결과를 쓰고 확인 실패 때도 마지막 업데이트 정보를 남겨요."""
    load_update_cache()
    url = f"https://api.github.com/repos/{UPDATE_REPO}/releases?per_page=30"
    with UPDATE_LOCK:
        next_check = (UPDATE.get("retry_at") or 0) if UPDATE.get("error") else (UPDATE.get("checked_at") or 0) + 1800
    while not stop.is_set():
        now = time.time()
        with UPDATE_LOCK:
            retry_at = UPDATE.get("retry_at") or 0
        manual = UPDATE_WAKE.is_set()
        if now < retry_at or (now < next_check and not manual):
            UPDATE_WAKE.clear()
            with UPDATE_LOCK:
                UPDATE["checking"] = False
            stop.wait(.25)
            continue
        UPDATE_WAKE.clear()
        with UPDATE_LOCK:
            UPDATE["checking"] = True
            etag = UPDATE.get("etag")
            had_success = UPDATE.get("checked_at") is not None
        try:
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "office-update"}
            if etag and had_success:
                headers["If-None-Match"] = etag
            req = urlrequest.Request(url, headers=headers)
            try:
                response = urlrequest.urlopen(req, timeout=20, context=provider_ssl_context())
            except HTTPError as error:
                if error.code != 304 or not had_success:
                    raise
                error.close()
                response = None
            if response is not None:
                with response as r:
                    rels = [x for x in json.loads(r.read()) if isinstance(x, dict) and not x.get("draft") and not x.get("prerelease")]
                    new_etag = r.headers.get("ETag")
                for x in rels:
                    x["version"] = str(x.get("tag_name") or "").lstrip("v")
                rels = sorted((x for x in rels if x["version"]), key=lambda x: version_key(x["version"]), reverse=True)
                current = version_key(office_version())
                newer = [x for x in rels if version_key(x["version"]) > current]
                day = lambda x: str(x.get("published_at") or "")[:10]
                notes = "\n\n".join(f"### {x['version']}" + (f" · {day(x)}" if day(x) else "") + f"\n\n{str(x.get('body') or '').strip()}" for x in (newer or rels[:1]))
                with UPDATE_LOCK:
                    UPDATE.update(latest=rels[0]["version"] if rels else None, notes=notes[:6000],
                                  url=rels[0].get("html_url") if rels else None,
                                  date=(day(rels[0]) or None) if rels else None, etag=new_etag)
        except (OSError, ValueError, TypeError, KeyError) as error:
            server_log("update.check", error)
            rate = isinstance(error, HTTPError) and error.code in (403, 429)
            with UPDATE_LOCK:
                UPDATE.update(error=update_error_text(error), checking=False,
                              retry_at=update_retry_at(error) if rate else 0)
            next_check = time.time() + (60 if rate else 3600)
            save_update_cache()
        else:
            with UPDATE_LOCK:
                UPDATE.update(checked_at=time.time(), error=None, checking=False, retry_at=0)
            next_check = time.time() + 3600
            save_update_cache()


def local_changes_text(output):
    """install.sh 의 'OFFICE_LOCAL: changed=N merged=a,b conflicts=c backup=경로' 줄을 화면 문구로"""
    line = next((x for x in reversed((output or "").splitlines()) if x.startswith("OFFICE_LOCAL:")), "")
    fields = dict(part.split("=", 1) for part in line[len("OFFICE_LOCAL:"):].split() if "=" in part)
    try:
        changed = int(fields.get("changed", 0))
    except ValueError:
        return None
    if not changed:
        backup = fields.get("backup", "-")
        return f"이번 업데이트부터 로컬 수정을 지켜 줘요. 예전 파일은 {backup}에 백업해 뒀어요." if fields.get("legacy") == "1" and backup != "-" else None
    merged = [x for x in fields.get("merged", "").split(",") if x not in ("", "-")]
    conflicts = [x for x in fields.get("conflicts", "").split(",") if x not in ("", "-")]
    text = f"로컬 수정 {changed}개를 찾았어요."
    if merged:
        text += f" {', '.join(merged)}는 새 버전에 다시 넣었어요."
    if conflicts:
        text += f" {', '.join(conflicts)}는 새 버전과 겹쳐서 새 버전으로 바꿨어요."
    if fields.get("backup", "-") != "-":
        text += f" 예전 파일은 {fields['backup']}에 있어요."
    return text


def run_update(office):
    """최신 설치 묶음을 받아 해시를 확인하고 install.sh로 같은 경로에 다시 설치한 뒤 새 프로세스로 다시 시작"""
    def fail(text):
        with UPDATE_LOCK:
            UPDATE.update(state="failed", message=text)
    paths = install_paths()
    if not paths:
        return fail("설치기로 설치한 Office에서만 바로 업데이트할 수 있어요.")
    prefix, workspace = paths
    base = f"https://github.com/{UPDATE_REPO}/releases/latest/download/{UPDATE_ASSET}"
    try:
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            ctx = provider_ssl_context()
            for name in (UPDATE_ASSET, UPDATE_ASSET + ".sha256"):
                with urlrequest.urlopen(base.replace(UPDATE_ASSET, name), timeout=120, context=ctx) as r:
                    (work / name).write_bytes(r.read())
            want = (work / (UPDATE_ASSET + ".sha256")).read_text().split()[0]
            if hashlib.sha256((work / UPDATE_ASSET).read_bytes()).hexdigest() != want:
                return fail("받은 설치 파일의 해시가 맞지 않아요. 잠시 뒤 다시 시도해 보세요.")
            import tarfile
            with tarfile.open(work / UPDATE_ASSET) as tar:
                tar.extractall(work, filter="data")
            script = work / "office-dist" / "install.sh"
            if not script.is_file():
                return fail("설치 파일에 설치기가 없어요.")
            r = subprocess.run(["bash", str(script), "--apply", "--skip-login", "--no-launch",
                                "--prefix", prefix, "--workspace", workspace],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900)
            if r.returncode:
                server_log("update.install", (r.stderr or r.stdout)[-2000:])
                tail = (r.stderr or r.stdout).strip().splitlines()
                return fail(f"설치에 실패했어요. {tail[-1] if tail else ''}".strip())
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
        server_log("update.run", error)
        return fail("업데이트 파일을 받지 못했어요. 인터넷 연결을 확인해 보세요.")
    local = local_changes_text(r.stdout)
    if local:   # 다시 켜진 뒤 대화창에 알리려고 남긴다(메모리 상태는 재시작 때 사라짐)
        server_log("update.local_changes")
        try:
            (D / "update-note.txt").write_text(local)
        except OSError as error:
            server_log("update.local_note", error)
    with UPDATE_LOCK:
        UPDATE.update(state="restarting", message=local)
    office.restart = [sys.executable, str(Path(prefix) / "app" / "office.py"), *sys.argv[1:]]
    time.sleep(2)   # 화면이 restarting 상태를 한 번은 받아 가게
    office.shutdown()


# ── git worktree: 대화마다 작업 폴더·브랜치 분리 ─────────────
WT = False          # git 저장소(커밋 1개 이상)면 True → 대화별 worktree로 동시 실행
REL = Path(".")     # 저장소 루트 기준 ROOT 위치 (하위 폴더에서 띄운 경우)




def git(*args, cwd=None):
    try:
        return subprocess.run(["git", *args], cwd=cwd or ROOT, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise UserError("Git 응답 시간 초과 — git 작업 응답 시간이 초과됐습니다. 저장소 상태를 확인하세요.") from None


def detect_worktree_mode():
    global WT, REL
    top = git("rev-parse", "--show-toplevel")
    WT = top.returncode == 0 and git("rev-parse", "--verify", "-q", "HEAD").returncode == 0
    if WT:
        REL = ROOT.relative_to(Path(top.stdout.strip()).resolve())


def wtree(sid):
    return D / "worktrees" / sid


def wdir(sid):
    return wtree(sid) / REL if WT else ROOT


def ensure_worktree(sid):
    if not WT or wtree(sid).exists():
        return
    br = f"office/{sid}"
    r = git("worktree", "add", "-b", br, str(wtree(sid)), "HEAD")
    if r.returncode and "already exists" in r.stderr:      # 브랜치만 남아 있으면 그걸로 다시 붙임
        r = git("worktree", "add", str(wtree(sid)), br)
    if r.returncode:
        raise UserError(ui_notice("작업 폴더 생성 실패", r.stderr.strip()))


def remove_worktree(sid):
    """작업 폴더만 지우고 브랜치(office/<id>)는 남김 — 병합 안 한 작업 보호"""
    if WT and wtree(sid).exists():
        result = git("worktree", "remove", "--force", str(wtree(sid)))
        if result.returncode:
            raise UserError("작업 폴더 정리 실패 — 작업 폴더 정리에 실패했습니다. 대화 기록은 유지됩니다.")
    if wtree(sid).exists():
        shutil.rmtree(wtree(sid))
    if WT:
        git("worktree", "prune")


def ident():
    return [] if git("config", "user.email").stdout.strip() else ["-c", "user.name=Office", "-c", "user.email=office@localhost"]


# ── GUI 서버 ────────────────────────────────────────────────
RESOURCE_LOCK = threading.RLock()
RESOURCE_CACHE = {}
RESOURCE_SAMPLE = None


def resource_unit():
    return "office-work-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:12] + ".slice"


def physical_memory():
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return 0


def resource_limits():
    saved = read_json_file(D / "resource-limits.json", {})
    return {"cpu_percent": saved.get("cpu_percent", 70), "memory_high_gb": saved.get("memory_high_gb", 0)}


def apply_resource_limits(cpu_percent, memory_high_gb):
    quota = cpu_percent * (os.cpu_count() or 1)
    memory = str(int(memory_high_gb * 1024**3)) if memory_high_gb else "infinity"
    unit = resource_unit()
    # Start a harmless command once so the shared slice exists before setting its budget.
    command = ["systemd-run", "--user", "--quiet", "--wait", "--pipe", "--slice=" + unit, "/usr/bin/true"]
    subprocess.run(command, capture_output=True, timeout=8, check=True)
    subprocess.run(["systemctl", "--user", "set-property", "--runtime", unit,
                    f"CPUQuota={quota}%", "CPUAccounting=yes", "MemoryAccounting=yes", "MemoryHigh=" + memory],
                   capture_output=True, timeout=5, check=True)


def resource_status(refresh=False):
    global RESOURCE_SAMPLE
    with RESOURCE_LOCK:
        now = time.time()
        if not refresh and RESOURCE_CACHE.get("root") == str(ROOT) and now - RESOURCE_CACHE.get("at", 0) < 3:
            return dict(RESOURCE_CACHE["value"])
        limits = resource_limits()
        data = {**limits, "supported": False, "reason": "이 PC에서는 작업 리소스 제한을 적용하지 못했어요.",
                "cpu_quota_percent": limits["cpu_percent"] * (os.cpu_count() or 1),
                "logical_cpus": os.cpu_count() or 1, "physical_memory_bytes": physical_memory(),
                "memory_current_bytes": None, "cpu_usage_percent": None,
                "applies_to": "새로 시작하는 Linux 에이전트·백그라운드 작업",
                "uncontrolled": "기존 실행·Windows·GPU·원격 API·브라우저"}
        try:
            if RESOURCE_CACHE.get("configured") != (str(ROOT), limits["cpu_percent"], limits["memory_high_gb"]):
                apply_resource_limits(**limits)
                RESOURCE_CACHE["configured"] = (str(ROOT), limits["cpu_percent"], limits["memory_high_gb"])
            result = subprocess.run(["systemctl", "--user", "show", resource_unit(), "--property=ControlGroup"],
                                    capture_output=True, text=True, timeout=3, check=True)
            group = result.stdout.strip().removeprefix("ControlGroup=")
            if not group.startswith("/") or ".." in group.split("/"):
                raise ValueError("cgroup path")
            folder = Path("/sys/fs/cgroup") / group.lstrip("/")
            usage = int(dict(line.split() for line in (folder / "cpu.stat").read_text().splitlines())["usage_usec"])
            data["memory_current_bytes"] = int((folder / "memory.current").read_text())
            previous = RESOURCE_SAMPLE
            if previous and previous[0] == str(ROOT) and now > previous[1]:
                data["cpu_usage_percent"] = round(max(0, (usage - previous[2]) / ((now-previous[1])*1_000_000*(os.cpu_count() or 1))*100), 1)
            RESOURCE_SAMPLE = (str(ROOT), now, usage)
            data.update(supported=True, reason="CPU는 전체 논리 CPU 기준 평균 상한이에요. 메모리는 느리게 처리하는 소프트 한도예요.")
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            RESOURCE_CACHE.pop("configured", None)
        RESOURCE_CACHE.update(root=str(ROOT), at=now, value=data)
        return dict(data)


def resource_view():
    """화면용: systemctl 을 기다리지 않게 마지막 값을 바로 돌려주고, 3초 지났으면 뒤에서 다시 구해요.
    값이 아직 없는 서버 시작 직후 한 번만 직접 구해요(없다고 잘못 보이지 않게)"""
    cache = dict(RESOURCE_CACHE)
    if cache.get("root") != str(ROOT) or "value" not in cache:
        return resource_status()
    if time.time() - cache.get("at", 0) >= 3:
        refresh_later("resources", resource_status)
    return dict(cache["value"])


REFRESHING = set()
REFRESHING_LOCK = threading.Lock()


def refresh_later(key, fn):
    """같은 값을 동시에 두 번 구하지 않게 key 하나에 뒤 작업 하나만 돌려요"""
    with REFRESHING_LOCK:
        if key in REFRESHING:
            return
        REFRESHING.add(key)
    def run():
        try:
            fn()
        except Exception as error:
            server_log("refresh." + key.split(":")[0], error)
        finally:
            with REFRESHING_LOCK:
                REFRESHING.discard(key)
    threading.Thread(target=run, daemon=True).start()


def limited_command(command):
    info = resource_status()
    if not info["supported"]:
        return command
    # Every agent and detached bg command shares the same slice, not one quota per process.
    return ["systemd-run", "--user", "--scope", "--quiet", "--slice=" + resource_unit(),
            "--unit=office-task-" + uuid.uuid4().hex + ".scope", "--", *command]


def open_report_path(raw, sid):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096 or any(ord(c) < 32 for c in raw):
        raise UserError("경로 확인 — 열 파일이나 폴더 경로를 확인하세요.")
    value = re.sub(r":\d+(?::\d+)?$", "", raw.strip())
    try:
        if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"):
            result = subprocess.run(["wslpath", "-u", value], capture_output=True, text=True, timeout=5, check=True)
            path = Path(result.stdout.strip())
        else:
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = wdir(sid) / path
        path = path.resolve(strict=True)
        result = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True, timeout=5, check=True)
        windows = result.stdout.strip()
        if not windows:
            raise ValueError("empty path")
        args = ["explorer.exe", windows] if path.is_dir() else ["explorer.exe", "/select," + windows]
        # Explorer may return 1 after handing the request to an existing window.
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd="/mnt/c")
    except (OSError, ValueError, subprocess.SubprocessError):
        raise UserError("경로를 열지 못했어요. 경로와 WSL 연결을 확인하거나 경로를 복사하세요.") from None
    return {"ok": "탐색기에서 경로를 열었어요."}


class Office:
    def __init__(self):
        self.stop = threading.Event()
        self.cleanup_done = threading.Event()
        self.teams = {}            # 대화 id → {"stop": Event, "w": {이름: Worker}}
        self.lock = threading.Lock()
        self.gitcache = {}
        self.rev_lock = threading.Lock()
        self.rev_counter = time.time_ns() // 1000
        self.revisions = {}

    def ensure_team(self, sid):
        with self.lock:
            existing = self.teams.get(sid)
            if existing:
                if existing["stop"].is_set():
                    if any(thread.is_alive() for thread in existing.get("threads", [])):
                        raise UserError("이전 작업 정리 중 — 이전 작업을 정리 중입니다. 잠시 후 다시 시도하세요.")
                    self.teams.pop(sid)
                else:
                    return
            ensure_worktree(sid)
            stop = threading.Event()
            ws = {n: Worker(n, sid, stop) for n in AGENTS}
            threads = [threading.Thread(target=w.loop, args=(self,), daemon=True) for w in ws.values()]
            threads.append(threading.Thread(target=self.watch_team, args=(sid, stop), daemon=True))
            self.teams[sid] = {"stop": stop, "w": ws, "threads": threads}
            for thread in threads:
                thread.start()

    def stop_team(self, sid):
        with self.lock:
            team = self.teams.get(sid)
            if not team:
                return
            team["stop"].set()
        for worker in team["w"].values():
            worker.stop.set()
            worker.terminate_agent()
        for thread in team.get("threads", []):
            if thread is not threading.current_thread():
                thread.join(timeout=3)
        if any(thread.is_alive() for thread in team.get("threads", [])):
            raise UserError("작업 프로세스 정리 중 — 작업 프로세스를 아직 정리 중입니다. 잠시 후 다시 시도하세요.")
        with self.lock:
            if self.teams.get(sid) is team:
                self.teams.pop(sid)

    def sync_teams(self):
        with meta() as m:
            live = [x["id"] for x in m["sessions"] if not x["deleted"]]
        for sid in live:
            self.ensure_team(sid)
        for sid in list(self.teams):
            if sid not in live:
                self.stop_team(sid)

    def watch_team(self, sid, stop):
        """둘 다 차례를 붙잡고 있어도 도는 감시: 바쁜 리더 대신 계획·미착수 작업 넘기기, 기다림 작업의 프로세스·기한 확인"""
        while not (stop.is_set() or self.stop.is_set()):
            for check in (self.reconcile_busy, self.check_waits):
                try:
                    check(sid)
                except (OSError, StorageError, UserError) as error:
                    server_log("team.watch", error)
            stop.wait(2)

    def reconcile_busy(self, sid):
        """담당이 다른 차례를 LEAD_BUSY_TAKEOVER 초 넘게 붙잡고 있으면 쉬는 쪽에게 새 요청 계획(리더 대행)과 미착수 작업을 넘긴다.
        오류 복구(reconcile_failover)와 달리 원래 담당이 쉬어도 계획 중엔 되돌리지 않고, 계획이 끝난 뒤 둘 다 쉴 때 리더만 되돌린다"""
        team = self.teams.get(sid)
        if not team or team["stop"].is_set():
            return
        workers, now = team["w"], time.time()
        def free(w):
            return w is not None and w.status == "idle" and not w.paused and not w.error and getattr(w, "retry_at", 0) <= now
        def held(w, job):   # 다른 일로 오래 붙잡고 있음
            doing = w.doing if isinstance(w.doing, dict) else {}
            return w is not None and w.status == "working" and now - w.since >= LEAD_BUSY_TAKEOVER and (job is None or doing.get("job") != job)
        def plan(record):
            changes = []
            active = {j["id"]: j for j in record["jobs"] if j["status"] != "done" and not j.get("cancelled")}
            for j in active.values():
                lead = j.get("acting_lead", j["lead"]); other = next(n for n in AGENTS if n != lead)
                if j["status"] == "planning" and now - j.get("started_at", now) >= LEAD_BUSY_TAKEOVER and held(workers.get(lead), j["id"]) and free(workers.get(other)):
                    j.update(acting_lead=other, acting_reason="busy")
                    changes.append((j["id"], f"{disp(lead)}가 다른 일로 바빠 {disp(other)}가 요청 {j['id']} 리더 대행"))
                elif j.get("acting_reason") == "busy" and j["status"] != "planning" and free(workers.get(j["lead"])) and free(workers.get(j.get("acting_lead"))):
                    j.pop("acting_lead", None); j.pop("acting_reason", None)
                    changes.append((j["id"], f"요청 {j['id']} 원래 리더 {disp(j['lead'])} 복구"))
            for t in record["tasks"]:
                if t["status"] != "todo" or t.get("job") not in active or t.get("owner") not in AGENTS or t.get("failover_from"):
                    continue
                source = t["owner"]; target = next(n for n in AGENTS if n != source)
                if t.get("handoffs") and t["handoffs"][-1].get("to") == source:
                    continue   # 일부러 넘겨받은 작업은 그대로 둔다
                if now - t.get("created_at", now) >= LEAD_BUSY_TAKEOVER and held(workers.get(source), None) and free(workers.get(target)):
                    t.update(owner=target, failover_from=source, failover_to=target, failover_at=now, failover_reason="busy")
                    changes.append((t.get("job"), f"{disp(source)}가 다른 차례를 오래 잡고 있어 미착수 작업 #{t['id']}을 {disp(target)}에게 넘김"))
            return changes
        with state(sid=sid) as record:
            if not plan(copy.deepcopy(record)):
                return
        with state(True, sid) as record:
            for job, text in plan(record):
                event(record, text, job=job)
                post(record, "system", "user", text, "progress", job=job)

    def check_waits(self, sid):
        """기다림 작업: 감시 프로세스가 끝나면(실패·강제 종료 포함), 기한이 지나면, 로그가 오래 멈추면 담당을 한 번씩 깨운다"""
        now = time.time()
        with state(sid=sid) as s:
            waits = [dict(t) for t in s["tasks"] if t["status"] == "waiting" and t.get("owner") in AGENTS]
        notes = []
        for t in waits:
            user_request = pending_user_request(t, now)
            if user_request:
                if user_request["status"] == "expired" and not t.get("user_expired_notified"):
                    notes.append((t, "user_expired_notified", f"[사용자 확인 기한 지남] 작업 #{t['id']} · {user_request['text']}", ""))
                continue
            alive, log_at = wait_probe(t)
            label = f"작업 #{t['id']} · {t.get('wait_reason') or '기다림'}"
            if alive is False and not t.get("wait_notified"):
                rc = None
                try:
                    rc = int(Path(t["wait_rc"]).read_text().strip()) if t.get("wait_rc") else None
                except (OSError, ValueError):
                    pass
                tail = ""
                try:
                    if t.get("wait_log"):
                        with open(t["wait_log"], "rb") as f:
                            f.seek(max(0, Path(t["wait_log"]).stat().st_size - 1500))
                            tail = safe_agent_diagnostic(f.read().decode(errors="replace")).strip()[-600:]
                except OSError:
                    pass
                result = (f"끝났어요(종료 코드 0)" if rc == 0 else f"실패했어요(종료 코드 {rc})" if rc is not None
                          else "중간에 멈췄어요(종료 코드 없음 — 강제 종료됐을 수 있어요)")
                notes.append((t, "wait_notified", f"[백그라운드 {result}] {label}", tail))
            elif t.get("wait_deadline") and now >= t["wait_deadline"] and not t.get("wait_overdue"):
                notes.append((t, "wait_overdue", f"[기다림 기한 지남] {label} · {int((now - t.get('waiting_at', now)) // 60)}분째", ""))
            elif (alive and log_at and now - max(log_at, (t.get("progress") or {}).get("at") or 0, t.get("waiting_at") or 0) >= WAIT_DEADLINE
                  and now >= ((t.get("progress") or {}).get("resume_at") or 0) + 300 and not t.get("wait_stale")):
                notes.append((t, "wait_stale", f"[로그 멈춤] {label} · 프로세스는 살아 있는데 로그가 {int((now - log_at) // 60)}분째 안 바뀌어요", ""))
        if not notes:
            return
        with state(True, sid) as s:
            for t, flag, head, tail in notes:
                cur = next((x for x in s["tasks"] if x["id"] == t["id"]), None)
                if not cur or cur["status"] != "waiting" or cur.get(flag):
                    continue
                cur[flag] = now
                post(s, "system", "user", head, "progress", job=cur.get("job"))
                body = head + (f"\n로그 끝:\n{tail}" if tail else "") + f"\n확인한 뒤 `office claim {cur['id']}`로 이어가거나 끝났으면 `office done {cur['id']}`. 더 기다릴 거면 `office wait {cur['id']} \"사유\"`로 다시 걸어라."
                post(s, "system", cur["owner"], body, "msg", job=cur.get("job"))
                s["msgs"][-1]["wake"] = True
                server_log("task.wait." + flag)

    def reconcile_failover(self, sid):
        team = self.teams.get(sid)
        if not team or team.get("stop", threading.Event()).is_set():
            return
        workers = team["w"]
        failed = {name for name, worker in workers.items() if worker.paused and worker.error}
        healthy = {name for name, worker in workers.items() if not worker.paused and not worker.error and not getattr(worker, "stop", threading.Event()).is_set()}
        def reconcile(record):
            changes = []
            active = {j["id"] for j in record["jobs"] if j["status"] != "done" and not j.get("cancelled")}
            for task in record["tasks"]:
                if task.get("job") not in active or task["status"] != "todo":
                    continue
                original = task.get("failover_from")
                if task.get("failover_reason") == "busy":
                    continue
                if original and original in healthy and task.get("owner") == task.get("failover_to"):
                    task["owner"] = original
                    task.pop("failover_from", None); task.pop("failover_to", None)
                    changes.append((task.get("job"), f"작업 #{task['id']} 원래 담당 {disp(original)} 복구"))
                elif not original and task.get("owner") in failed and healthy and not (task.get("handoffs") and task["handoffs"][-1].get("to") == task["owner"]):
                    source = task["owner"]; target = next(n for n in AGENTS if n in healthy)
                    task.update(owner=target, failover_from=source, failover_to=target, failover_at=time.time())
                    changes.append((task.get("job"), f"{disp(source)} 오류로 미착수 작업 #{task['id']}을 {disp(target)}에게 재배정"))
            for job in record["jobs"]:
                if job["id"] not in active:
                    continue
                lead = job["lead"]
                acting_worker = workers.get(job.get("acting_lead"))
                acting_busy = acting_worker and acting_worker.status == "working" and isinstance(acting_worker.doing, dict) and acting_worker.doing.get("job") == job["id"]
                if job.get("acting_reason") == "busy" and lead not in failed:
                    continue
                if lead in healthy and job.get("acting_lead") and not acting_busy:
                    job.pop("acting_lead", None)
                    changes.append((job["id"], f"요청 {job['id']} 원래 리더 {disp(lead)} 복구"))
                elif lead in failed and healthy:
                    acting = next(n for n in AGENTS if n in healthy)
                    if job.get("acting_lead") != acting:
                        job["acting_lead"] = acting
                        changes.append((job["id"], f"{disp(lead)} 오류로 {disp(acting)}가 요청 {job['id']} 리더 대행"))
            return changes
        with state(sid=sid) as record:
            if not reconcile(copy.deepcopy(record)):
                return
        with state(True, sid) as record:
            for job, text in reconcile(record):
                event(record, text, job=job)

    def first_run_login(self):
        """Office 를 켤 때 Claude·Codex 로그인 상태를 보고, 안 된 쪽만 로그인 링크를 받아 대화창에 띄운다. 에이전트는 로그인 전엔 실행 자체가 안 되므로 서버가 직접 챙긴다."""
        # 처음 쓰는 사람뿐 아니라 로그인이 풀린 사람도 같은 안내를 받는다(로그인돼 있으면 아무것도 안 띄움).
        # 그래야 '링크 만료·실패 → Office 다시 켜기' 안내가 언제나 통한다.
        try:
            sid = current_sid()
        except (OSError, UserError) as error:
            server_log("login.first_run", error)
            return
        self.offer_login(sid, AGENTS)

    def offer_login(self, sid, names, midway=False):
        """names 중 로그인이 안 된 쪽만 로그인 링크를 받아 대화창에 띄우고 결과를 지켜본다.
        midway=True 는 쓰는 도중 로그인이 풀려 멈춘 경우(한 달쯤 지나 토큰이 만료되는 등)"""
        if not hasattr(self, "login_lock"):
            self.login_lock = threading.Lock()
        with self.login_lock:
            if not hasattr(self, "login_pending"):
                self.login_pending = set()
            need = [n for n in names if n not in self.login_pending and not login_ok(n)]
            if not need or self.stop.is_set():
                return
            self._offer_login(sid, need, midway)
            if self.login_pending and not getattr(self, "login_watching", False):
                self.login_watching = True
                threading.Thread(target=self.watch_login, args=(sid,), daemon=True).start()

    def _offer_login(self, sid, need, midway):
        self.login_sid = getattr(self, "login_sid", {})
        for n in need:
            self.login_sid[n] = sid
            try:
                url = login_start(n)
            except (OSError, UserError) as error:
                server_log("login.first_run.start", error)
                with state(True, sid) as s:
                    post(s, "system", "user", ui_notice(f"{disp(n)} 로그인 필요", f"로그인 링크를 받지 못했어요. 터미널에서 {'claude 실행 후 /login' if n == 'claude' else 'codex login'}으로 로그인하세요."), "event")
                continue
            self.login_pending.add(n)
            how = ("링크를 열어 로그인한 뒤, 화면에 나온 코드를 이 채팅창에 붙여 넣어 보내세요." if n == "claude"
                   else "링크를 이 PC 브라우저에서 열면 로그인이 끝나요.")
            with state(True, sid) as s:
                if midway:
                    post(s, "system", "user", ui_notice(f"{disp(n)} 로그인 풀림", f"로그인이 풀려서 {disp(n)}가 멈췄어요. {how} 로그인이 끝나면 하던 일을 이어서 해요.\n{url}"), "event")
                else:
                    post(s, "system", "user", ui_notice(f"{disp(n)} 로그인 필요", f"쓰려면 로그인이 필요해요. {how}\n{url}"), "event")

    LOGIN_TRIES = 1          # 만료 뒤에는 자동 재발급하지 않고 사용자 복귀 때 새 링크를 받아요.
    LOGIN_WAIT = 620         # 링크 하나를 기다리는 시간(로그인 과정은 600초 뒤 스스로 끝남)

    def watch_login(self, sid):
        """띄운 로그인 과정이 끝나면 결과를 알린다. 실패·만료면 새 링크를 받아 다시 안내하고(최대 LOGIN_TRIES번),
        성공하면 로그인 오류로 멈춘 에이전트를 다시 움직인다"""
        tries, started, deadline = {}, {}, {}
        while not self.stop.is_set():
            with self.login_lock:
                if not self.login_pending:
                    self.login_watching = False
                    return
                pending = list(self.login_pending)
            for n in pending:
                if n not in started:   # 지켜보는 도중에 새로 로그인이 풀린 쪽
                    tries[n], started[n] = 1, time.time()
                    deadline[n] = started[n] + self.LOGIN_WAIT
                if login_alive(login_paths(n)[2]) and time.time() < deadline[n]:
                    continue
                ok = login_ok(n)
                if ok:
                    self.login_pending.discard(n)
                    with state(True, self.login_sid.get(n, sid)) as s:
                        post(s, "system", "user", ui_notice(f"{disp(n)} 로그인 완료", "이제 바로 일을 맡길 수 있어요."), "event")
                    for team in list(self.teams.values()):
                        w = team["w"].get(n)
                        if w:
                            with state(True, w.sid) as record:
                                for task in record["tasks"]:
                                    request = task.get("user_request") or {}
                                    if task.get("owner") == n and request.get("source") == "agent_auth" and pending_user_request(task):
                                        request.update(status="resolved", resolved_at=time.time(), resolution="로그인 성공을 확인했어요.")
                                        task["status"] = "done" if task.get("auth_task") else request.get("resume_status", "doing")
                                        if task["status"] == "done":
                                            task["done_at"] = time.time()
                                        task.pop("wait_reason", None)
                        if w and w.paused and not getattr(w, "manual_pause", False) and getattr(w, "error_kind", None) == "auth":
                            # 사용자 정지는 유지하고 인증 오류로 멈춘 차례만 다시 실행해요.
                            w.set_recovery("verifying", "로그인 성공을 확인했어요. 중단된 차례의 재개를 확인해요.")
                            w.paused, w.error, w.error_kind = False, None, None
                            w.cont = w.chatter = 0
                            w.nudge_state = {}
                            w.auto_retries, w.retry_at, w.retry_job = 0, 0, None
                            w.retry_due = dict(w.resume_turn) if isinstance(w.resume_turn, dict) else None
                            w.save_checkpoint()
                    for d in (tries, started, deadline):
                        d.pop(n, None)
                    continue
                # 로그인 과정은 600초 뒤 timeout 으로 먼저 끝나므로, 그 무렵 끝났으면 만료로 본다
                expired = time.time() - started[n] >= min(600, self.LOGIN_WAIT) - 5
                url = None
                if tries[n] < self.LOGIN_TRIES:
                    try:
                        url = login_start(n)
                    except (OSError, UserError) as error:
                        server_log("login.retry.start", error)
                if url:
                    tries[n] += 1
                    started[n] = time.time()
                    deadline[n] = started[n] + self.LOGIN_WAIT
                    how = ("새 링크로 다시 로그인한 뒤, 화면에 나온 코드를 이 채팅창에 붙여 넣어 보내세요." if n == "claude"
                           else "새 링크를 이 PC 브라우저에서 열면 로그인이 끝나요.")
                    why = "10분 안에 로그인이 끝나지 않아 링크를 새로 받았어요." if expired else "로그인이 끝나지 않았어요."
                    with state(True, self.login_sid.get(n, sid)) as s:
                        post(s, "system", "user", ui_notice(f"{disp(n)} 로그인 다시 시도", f"{why} {how}\n{url}"), "event")
                    continue
                self.login_pending.discard(n)
                with state(True, self.login_sid.get(n, sid)) as s:
                    post(s, "system", "user", ui_notice(f"{disp(n)} 로그인 안 됨",
                         f"로그인을 {tries[n]}번 시도했지만 끝나지 않았어요. 터미널에서 {'claude 를 실행한 뒤 /login' if n == 'claude' else 'codex login'} 으로 로그인한 뒤 Office 를 다시 켜세요."), "event")
                for d in (tries, started, deadline):
                    d.pop(n, None)
            self.stop.wait(1)

    def take_login_code(self, sid, text):
        """Claude 로그인 중에 사용자가 보낸 코드면 요청으로 만들지 않고 로그인에 넣는다.
        로그인 과정이 이미 끝났는데 Claude 코드 모양(코드#상태)이 오면, 요청으로 새지 않게 막고 안내만 한다"""
        code = text.strip()
        if "claude" in getattr(self, "login_pending", ()) and re.fullmatch(r"[A-Za-z0-9_#~.\-]{20,}", code):
            login_feed("claude", code)
            with state(True, sid) as s:
                post(s, "system", "user", "Claude 로그인 코드를 넣었어요. 확인하는 중이에요.", "event")
            return True
        if re.fullmatch(r"[A-Za-z0-9_\-]{20,}#[A-Za-z0-9_\-]{10,}", code):
            # 로그인 과정이 끝난 뒤 늦게 붙여 넣은 코드는 로그인 성공 여부와 상관없이 요청으로 만들지 않는다
            body = ("Claude 는 이미 로그인돼 있어요. 붙여 넣은 코드는 쓰지 않았어요." if login_ok("claude")
                    else "로그인 링크가 이미 끝나서 코드를 넣지 못했어요. Office 를 다시 켜면 새 링크를 받아요.")
            with state(True, sid) as s:
                post(s, "system", "user", ui_notice("Claude 로그인 코드 확인", body), "event")
            return True
        return False

    def check_stall(self, sid):
        """열린 요청이 STALL_NOTICE 초 동안 아무도 일하지 않고 진척도 없으면 사용자에게 한 번 알리고 리더에게 점검 차례를 준다"""
        team = self.teams.get(sid)
        now = time.time()
        if not team or team["stop"].is_set() or now - team.get("stall_checked", 0) < 2:
            return
        team["stall_checked"] = now
        workers = team["w"]
        with state(sid=sid) as s:
            jobs = [j for j in s["jobs"] if j["status"] != "done"]
            tasks_sig = tuple((t["id"], t["status"], t["owner"], (t.get("progress") or {}).get("at")) for t in s["tasks"])
            activity = (len(s["msgs"]), tasks_sig)
            tasks = [t for t in s["tasks"] if t["status"] != "done"]
            # 열린 작업이 모두 기다림인 요청은 멈춘 게 아니라 결과를 기다리는 중이다
            jobs = [j for j in jobs if not ((left := [t for t in tasks if t.get("job") == j["id"]]) and all(t["status"] == "waiting" for t in left))]
        busy = any(w.status == "working" or getattr(w, "retry_at", 0) > now for w in workers.values())
        prev = team.get("stall")
        if not jobs or busy or not prev or prev[0] != activity:
            team["stall"] = (activity, now)
            return
        if now - prev[1] < STALL_NOTICE or team.get("stall_notified") == (tasks_sig, tuple(j["id"] for j in jobs)):
            return
        team["stall_notified"] = (tasks_sig, tuple(j["id"] for j in jobs))
        def who(w):
            return f"{disp(w.name)}(" + ("오류로 멈춤" if w.error else "사용자가 멈춤") + ")"
        stopped = [who(w) for w in workers.values() if w.paused]
        for j in jobs:
            left = [t for t in tasks if t.get("job") == j["id"]]
            left_text = ", ".join(f"#{t['id']} {disp(t['owner']) if t.get('owner') else '담당 없음'} {'진행 중' if t['status'] == 'doing' else '기다리는 중' if t['status'] == 'waiting' else '대기'}" for t in left) or "없음(검수 차례 대기)"
            detail = f"남은 작업 {left_text}" + (f" · 멈춘 담당 {', '.join(stopped)}" if stopped else "")
            lead_name = j.get("acting_lead", j["lead"])
            lead = workers.get(lead_name)
            nudged = lead is not None and not lead.paused
            with state(True, sid) as record:
                post(record, "system", "user", ui_notice(f"요청 {j['id']} 진행 멈춤",
                     f"{int(now - prev[1])}초 동안 진척이 없습니다. {detail}." + (" 리더에게 점검을 요청했습니다." if nudged else " 멈춘 담당을 재개하거나 메시지로 지시하세요.")),
                     "event", job=j["id"])
            if nudged and not any(x[1] == j["id"] for x in getattr(lead, "stall_prompts", [])):
                lead.stall_prompts = getattr(lead, "stall_prompts", []) + [(
                    f"[진행 멈춤 점검] 요청 {j['id']}가 {int(now - prev[1])}초 동안 진척이 없다. {detail}.\n"
                    "작업판을 보고 막힌 원인을 풀어라. 동료 답을 기다리는 중이면 필요한 내용을 다시 정리해 보내라.\n"
                    f"오류로 멈춘 담당의 작업은 `office handoff 번호 --to {lead_name}` 로 넘겨받아 이어서 해도 된다.\n"
                    "사용자 결정이 필요하면 `office say --to user` 로 묻고, 할 게 없으면 '대기'라고만 답해라.", j["id"])]
                server_log("job.stall.nudge")

    @staticmethod
    def live_view(w, msgs):
        """화면이 '멈췄나?'를 풀어 주는 값: 어디까지 읽었는지, 결과를 기다리는 도구, Claude가 지금 쓰는 블록"""
        cursor = w.read_upto if isinstance(w.read_upto, int) else 0
        read_id = msgs[min(cursor, len(msgs)) - 1].get("id", 0) if cursor and msgs else 0
        tool = gen = None
        if w.status == "working":
            tools = dict(getattr(w, "open_tools", {}))
            if tools:
                key, since = min(tools.items(), key=lambda item: item[1])
                tool = {"label": getattr(w, "tool_labels", {}).get(key, "도구"), "since": since}
            gen = getattr(w, "gen", None)
        if gen:   # 글자 수는 100자 단위로만 바꿔 화면 갱신이 토큰마다 일어나지 않게
            gen = {**gen, "chars": gen["chars"] // 100 * 100}
        return {"read_id": read_id, "tool": tool, "gen": gen}

    @staticmethod
    def job_wait_reason(data, j, team):
        """열린 요청을 지금 아무도 안 잡고 있을 때 화면에 보일 이유 한 줄. 일하는 중·일시정지·오류·기다림은 화면이 따로 보여 준다"""
        now = time.time()
        jid = j["id"]
        def doing(w):
            return w.doing if isinstance(w.doing, dict) else {}
        def elsewhere(name):   # 다른 일로 차례를 붙잡고 있으면 그 일 이름, 아니면 None
            w = team.get(name)
            if w is None or w.status != "working" or doing(w).get("job") == jid:
                return None
            other_job = doing(w).get("job")
            return f"요청 {other_job}" if other_job else "직접 받은 메시지"
        def free(name):
            w = team.get(name)
            return w is not None and w.status == "idle" and not w.paused and not w.error and getattr(w, "retry_at", 0) <= now
        if any(w.status == "working" and doing(w).get("job") == jid for w in team.values()) \
                or any(w.paused or w.error or getattr(w, "retry_at", 0) > now for w in team.values()):
            return None
        left = [t for t in data["tasks"] if t.get("job") == jid and t["status"] != "done"]
        if left and all(t["status"] == "waiting" for t in left):
            return None
        lead = j.get("acting_lead", j["lead"])
        other = next((n for n in AGENTS if n != lead), None)
        takeover = f" {max(1, LEAD_BUSY_TAKEOVER // 60)}분 넘으면 {disp(other)}가 대신 맡아요." if other and free(other) else ""
        if j["status"] == "planning":
            busy = elsewhere(lead)
            return f"리더 {disp(lead)}가 {busy} 처리 중이라 계획을 기다려요.{takeover}" if busy else "리더가 곧 계획을 세워요."
        if not left:
            busy = elsewhere(lead)
            return f"작업이 다 끝났어요. 리더 {disp(lead)}가 {busy} 처리를 마치면 결과를 확인해요." if busy else "작업이 다 끝났어요. 리더가 곧 결과를 확인해요."
        for t in left:
            owner = t.get("owner")
            if owner is None:
                continue
            busy = elsewhere(owner)
            if busy:
                return f"작업 #{t['id']} 대기 · {disp(owner)}가 {busy} 처리 중이에요."
            if t["status"] == "doing" and free(owner) and now - team[owner].since >= 10:   # 차례 사이 1초 쉬는 틈은 빼고
                w = team[owner]
                if w.waiting_on_peer(data, [t]):
                    return f"{disp(owner)}가 {disp(w.other)} 답을 기다려요."
                return f"{disp(owner)}가 작업 #{t['id']} 진행을 멈췄어요. {STALL_NOTICE // 60}분 넘게 진척이 없으면 리더가 점검해요."
        if any(t.get("owner") is None for t in left):
            return "담당 없는 작업이 있어요. 쉬는 쪽이 곧 가져가요."
        return None

    def busy(self, sid):
        t = self.teams.get(sid)
        return bool(t) and any(w.status == "working" for w in t["w"].values())

    def git_info(self, sid):
        """화면용: 마지막 값을 바로 돌려주고 3초 지났으면 뒤에서 다시 구해요.
        디스크가 밀리면 git 실행이 몇 초씩 걸려 조회 응답이 같이 멈췄어요. 처음 한 번은 값 없이(None) 답해요"""
        if not WT or not wtree(sid).exists():
            return None
        hit = self.gitcache.get(sid)
        if not hit or time.time() - hit[0] >= 3:
            refresh_later("git:" + sid, lambda: self.read_git_info(sid))
        return hit[1] if hit else None

    def read_git_info(self, sid):
        br = f"office/{sid}"
        # 화면 조회만 하므로 index.lock 을 잡지 않게(에이전트의 git 작업과 부딪히지 않게)
        changed = len([l for l in git("--no-optional-locks", "status", "--porcelain", cwd=wtree(sid)).stdout.splitlines() if l.strip()])
        ahead = git("rev-list", "--count", f"HEAD..{br}").stdout.strip()
        info = {"branch": br, "path": str(wdir(sid).relative_to(ROOT)), "changed": changed,
                "ahead": int(ahead or 0), "main": git("branch", "--show-current").stdout.strip() or "HEAD"}
        self.gitcache[sid] = (time.time(), info)
        return info

    def snapshot(self, sid=None, since_rev=None, since_msgs=None):
        cur = sid or current_sid()
        m = meta_view()
        if not valid_sid(cur) or not any(x["id"] == cur and not x["deleted"] for x in m["sessions"]):
            raise UserError("대화를 찾을 수 없음 — 대화가 없습니다. 대화 목록에서 다시 선택하세요.")
        self.ensure_team(cur)
        cached, _, msgs_key = state_view_cached(cur)
        data = dict(cached)   # 겉복사: 아래에서 키를 더하고 jobs·tasks 는 복사해서 고쳐요
        data["chat_options_supported"] = True
        data["chat_options"] = chat_options_view(data.get("chat_options"))
        data["tasks"] = [dict(t) for t in data.get("tasks", [])]
        display_now = time.time()
        def worker_status(session):
            return {n: {"status": w.status, "doing": w.doing, "paused": w.paused, "error": w.error}
                    for n, w in self.teams.get(session, {}).get("w", {}).items()}
        for t in data["tasks"]:
            if t["status"] == "waiting":
                t.update(task_wait_status(t, display_now))
        data["jobs"] = [dict(j) for j in data.get("jobs", [])]
        for j in data["jobs"]:
            j["needs_user"] = [{"task_id": t["id"], **request} for t in data["tasks"] if t.get("job") == j["id"] and (request := pending_user_request(t, display_now))]
            j["verification"] = j.get("verification", "standard")
            j["display_status"], j["display_reason"] = job_display_status(j, data["tasks"], worker_status(cur), display_now)
        sessions = [dict(x) for x in m["sessions"]]
        notice, display_busy = {}, {}
        for x in sessions:              # 목록에 띄울 진행 상황 요약
            try:
                st, notice[x["id"]], _ = state_view_cached(x["id"])
                x.update(session_display_status(st, worker_status(x["id"]), display_now))
                x["needs_user"] = sum(bool(pending_user_request(t, display_now)) for t in st.get("tasks", []))
                completed = {j["id"] for j in st.get("jobs", []) if j["status"] == "done"}
                display_busy[x["id"]] = any(w.status == "working" and (w.doing or {}).get("job") not in completed for w in self.teams.get(x["id"], {}).get("w", {}).values())
            except (OSError, ValueError, StorageError) as error:
                server_log("snapshot.session", error)
                x.update(open=0, status_counts={}, status_kind="done", status_reason="")
                notice[x["id"]] = 0
        # 알림 점: 사용자에게 온 보고·메시지·요청 완료가 있는데 아직 안 본 대화. 보고 있는 대화는 본 것으로 처리
        if any("seen" not in x or (x["id"] == cur and x["seen"] < notice[x["id"]]) for x in sessions):
            marks = {x["id"]: max(x.get("seen", 0), notice[x["id"]]) for x in sessions
                     if "seen" not in x or x["id"] == cur}
            def save_seen(m):
                for y in m["sessions"]:
                    if y["id"] in marks:
                        y["seen"] = max(y.get("seen", 0), marks[y["id"]])
            meta_later(save_seen)   # 조회 응답이 저장을 기다리지 않게
            for x in sessions:
                x["seen"] = marks.get(x["id"], x.get("seen", notice[x["id"]]))
        for x in sessions:
            # 진행 중인 요청이 남아 있으면 중간 보고라도 반짝이지 않는다. 다 끝난 뒤(또는 요청 없이 온 결과)에만 알린다
            x["unread"] = x["id"] != cur and x["open"] == 0 and notice[x["id"]] > x.get("seen", 0)
            x["busy"] = display_busy.get(x["id"], self.busy(x["id"]))
            x["stopped"] = any(w.paused or w.error for w in self.teams.get(x["id"], {}).get("w", {}).values())
        data.update(root=str(ROOT), session=cur, sessions=sessions, parallel=WT, git=self.git_info(cur),
                    warnings=list(STORAGE_WARNINGS.values())[-10:], update=update_info(), resources=resource_view())
        team = self.teams[cur]["w"]
        for w in team.values():
            if w.status == "working":
                w.update_codex_usage()
        reservations = read_json_file(Path.home()/".office"/"reservations.json", {}, reservations_valid)   # 화면용: 잠금·정리 없이 읽기만
        data["file_reservations"] = [{k: value[k] for k in ("path","owner","session","job","since")} for value in reservations.values() if Path(value["path"]).is_relative_to(ROOT)]
        data["agents"] = {n: {"status": w.status, "since": w.since, "doing": w.doing, "paused": w.paused, "error": w.error, "error_kind": getattr(w, "error_kind", None), "fail_count": getattr(w, "fail_count", 0), "recovery": getattr(w, "recovery", None), "notice": w.notice or getattr(w, "model_notice", None),
                              "model": getattr(w, "primary_model", None) or configured_model(n),
                              "effort": getattr(w, "turn_effort", None) if w.status == "working" else None,
                              "level": getattr(w, "turn_level", None) if w.status == "working" else None,
                              "last_output": getattr(w, "last_output", None) if w.status == "working" else None,
                              "tokens": w.tokens, "cached_tokens": w.cached_tokens, "legacy_tokens": w.legacy_tokens,
                              "usage_since": w.usage_since, "limits": limits_for(n), "subs": list(getattr(w, "subs", {}).values()) if w.status == "working" else [],
                              "activity": list(w.activity)[-25:], **self.live_view(w, data["msgs"])} for n, w in team.items()}
        data["jobs"] = [dict(j) for j in data.get("jobs", [])]   # 화면용 값을 붙여도 저장된 상태엔 안 섞이게
        for j in data["jobs"]:
            j["wait_why"] = self.job_wait_reason(data, j, team) if j["status"] != "done" and not j.get("cancelled") else None
        # since/since(epoch)는 시작 시각이며 매초 경과 숫자는 화면에서 계산한다.
        # 가장 큰 msgs 는 파일이 그대로면 같으니 파일 식별값으로 갈음하고 나머지만 해시해요
        sign = {k: v for k, v in data.items() if k != "msgs"} if msgs_key else data
        signature = hashlib.sha256(json.dumps([sign, msgs_key], ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).digest()
        with self.rev_lock:
            prior = self.revisions.get(cur)
            if prior is None or prior[0] != signature:
                self.rev_counter += 1
                prior = self.revisions[cur] = (signature, self.rev_counter)
            rev = prior[1]
        if since_rev == rev:
            return {"unchanged": True, "rev": rev}
        data["rev"] = rev
        data["version"] = office_version()
        # 대화 기록(msgs)은 가장 크고 진행 표시만 바뀔 땐 그대로라서, 화면이 가진 기록 표식과 같으면 빼고 보내요
        data["msgs_key"] = "-".join(map(str, msgs_key)) if msgs_key else None
        if since_msgs and since_msgs == data["msgs_key"]:
            data["msgs"] = None
            data["msgs_unchanged"] = True
        return data

    def merge(self, sid):
        if not WT:
            raise UserError("병합할 브랜치 없음 — git 저장소가 아니라 병합할 브랜치가 없습니다.")
        if self.busy(sid):
            raise UserError("작업 중 병합 불가 — 에이전트가 작업 중이라 지금은 병합할 수 없습니다. 작업이 끝난 뒤 다시 시도해 주세요.")
        with meta() as m:
            title = next((x["title"] for x in m["sessions"] if x["id"] == sid), sid)
        if git("status", "--porcelain").stdout.strip():
            raise UserError("미저장 변경으로 병합 보류 — 메인 폴더에 커밋하지 않은 변경이 있어 병합하지 않았습니다. 먼저 정리하세요.")
        wt, br = wtree(sid), f"office/{sid}"
        if git("status", "--porcelain", cwd=wt).stdout.strip():
            git("add", "-A", cwd=wt)
            r = git(*ident(), "commit", "-m", f"office: {title}", cwd=wt)
            if r.returncode:
                raise UserError(ui_notice("작업 내용 저장 실패", r.stderr.strip() or r.stdout.strip()))
        if git("status", "--porcelain").stdout.strip():
            raise UserError("미저장 변경으로 병합 보류 — 메인 폴더에 커밋하지 않은 변경이 있어 병합하지 않았습니다. 먼저 커밋하거나 정리해 주세요.")
        if git("rev-list", "--count", f"HEAD..{br}").stdout.strip() == "0":
            return "병합할 변경이 없습니다."
        r = git(*ident(), "merge", "--no-ff", "--no-edit", br)
        if r.returncode:
            conflicts = git("diff", "--name-only", "--diff-filter=U").stdout.split()
            git("merge", "--abort")
            raise UserError(ui_notice("충돌로 병합 취소", f"충돌 파일: {', '.join(conflicts)}" if conflicts else r.stderr.strip()))
        self.gitcache.pop(sid, None)
        main = git("branch", "--show-current").stdout.strip() or "HEAD"
        try:
            with state(True, sid) as s:
                event(s, f"작업 내용을 메인 폴더({main})에 병합함")
        except StorageError as error:
            server_log("merge.record", error)
            return {"ok": f"메인 폴더({main})에 병합했습니다.", "warning": "병합은 완료했지만 대화 기록 저장에 실패했습니다."}
        return f"메인 폴더({main})에 병합했습니다."

    def purge(self, m, x):
        self.stop_team(x["id"])
        remove_worktree(x["id"])
        try:
            shutil.rmtree(sdir(x["id"]))
        except FileNotFoundError:
            pass
        except OSError as error:
            server_log("session.purge", error)
            raise UserError("대화 삭제 실패 — 대화를 삭제하지 못했습니다. 휴지통 기록은 유지됩니다. 폴더 권한을 확인하세요.") from None
        m["sessions"].remove(x)

    def handle(self, p):
        if not isinstance(p, dict) or not isinstance(p.get("action"), str):
            raise UserError("요청 형식 확인 — 요청 형식이 올바르지 않습니다.")
        act, msg = p["action"], None
        known = {"hello", "new_session", "switch", "rename", "pin", "delete", "restore", "purge", "empty_trash",
                 "merge", "job", "msg", "pause", "pause_now", "cancel_job", "quit", "update", "update_check", "open_path", "open_window", "resource_limits", "chat_options"}
        if act not in known:
            raise UserError("동작 처리 불가 — 알 수 없는 동작입니다. 화면을 새로고침하고 다시 시도하세요.")
        if act in ("switch", "rename", "pin", "delete", "restore", "purge"):
            target = p.get("id")
            if not valid_sid(target):
                raise UserError("대화 선택 확인 — 올바른 대화를 다시 선택하세요.")
            m = meta_view()
            found = next((x for x in m["sessions"] if x["id"] == target), None)
            if not found:
                raise UserError("대화를 찾을 수 없음 — 대화가 없습니다. 대화 목록을 새로고침하세요.")
            if act in ("switch", "rename", "pin", "delete") and found["deleted"]:
                raise UserError("삭제된 대화 — 삭제된 대화입니다. 휴지통에서 복원하세요.")
            if act in ("restore", "purge") and not found["deleted"]:
                raise UserError("휴지통 대화 확인 — 휴지통에 있는 대화만 처리할 수 있습니다.")
        if act == "rename" and (not isinstance(p.get("title"), str) or not p["title"].strip()):
            raise UserError("대화 제목 필요 — 대화 제목을 입력하세요.")
        if act == "cancel_job" and (type(p.get("id")) is not int or p["id"] < 1):
            raise UserError("요청 선택 확인 — 올바른 요청을 선택하세요.")
        sid = None
        if act in ("job", "msg", "pause", "pause_now", "cancel_job", "merge", "open_path", "chat_options"):
            sid = (p.get("id") or p.get("session") or current_sid()) if act == "merge" else (p.get("session") or current_sid())
            if not valid_sid(sid):
                raise UserError("대화 선택 확인 — 올바른 대화를 다시 선택하세요.")
            m = meta_view()
            if not any(x["id"] == sid and not x["deleted"] for x in m["sessions"]):
                raise UserError("열린 대화 확인 필요 — 대화가 삭제되었거나 더 이상 열려 있지 않습니다. 대화 목록에서 다시 선택하세요.")
        if act in ("job", "msg"):
            if not isinstance(p.get("text"), str) or not p["text"].strip():
                raise UserError("보낼 내용 필요 — 보낼 내용을 입력하세요.")
            if len(p["text"]) > MAX_TEXT:
                raise UserError(f"입력 길이 초과 — 한 번에 {MAX_TEXT:,}자까지 보낼 수 있습니다. 내용을 나눠 보내세요.")
            if act == "job" and p.get("verification", "standard") not in ("quick", "standard", "thorough"):
                raise UserError("검증 수준 확인 — 빠르게, 기본, 꼼꼼하게 중에서 선택하세요.")
            if act == "job" and p.get("lead", "claude") not in AGENTS:
                raise UserError("리더 선택 확인 — 리더는 Claude 또는 Codex를 선택하세요.")
            if act == "msg" and p.get("to", "all") not in (*AGENTS, "all"):
                raise UserError("받는 에이전트 확인 — 메시지를 받을 에이전트를 다시 선택하세요.")
        if act == "chat_options":
            changes = {key: p[key] for key in CHAT_OPTION_VALUES if key in p}
            if not changes or any(value not in CHAT_OPTION_VALUES[key] for key, value in changes.items()):
                raise UserError("대화 선택을 저장하지 못했어요. 대상·리더·검증을 다시 선택하세요.")
            if "initialize_only" in p and type(p["initialize_only"]) is not bool:
                raise UserError("대화 선택을 저장하지 못했어요. 화면을 새로고침하세요.")
            with state(True, sid) as saved:
                options = chat_options_view(saved.get("chat_options"))
                if options is None or not p.get("initialize_only", False):
                    options = {**(options or CHAT_OPTION_DEFAULTS), **changes}
                    saved["chat_options"] = options
            return {"chat_options": options}
        if act == "hello":                      # 새로 열린 창이 주인. 이전 창은 owner:false 를 받고 스스로 닫힌다
            client = p.get("client")
            if not isinstance(client, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", client):
                raise UserError("창 확인 실패 — 화면을 새로고침하세요.")
            self.window_owner, self.window_seen = client, time.time()
            return msg
        if act == "new_session":
            with meta(True) as m:
                created = new_session(m)
                try:
                    self.ensure_team(created)
                except Exception as error:
                    server_log("session.prepare", error)
                    try:
                        self.stop_team(created)
                        remove_worktree(created)
                        shutil.rmtree(sdir(created))
                    except (OSError, UserError) as cleanup_error:
                        server_log("session.prepare.cleanup", cleanup_error)
                    raise UserError("새 대화 준비 실패 — 새 대화를 준비하지 못했습니다. 기존 대화는 유지됩니다.") from None
        elif act == "switch":
            CURRENT_HINT.update(sid=p["id"], at=time.time())
            def save_current(m, sid=p["id"]):
                if any(x["id"] == sid and not x["deleted"] for x in m["sessions"]):
                    m["current"] = sid
            meta_later(save_current)
        elif act == "rename":
            with meta(True) as m:
                for x in m["sessions"]:
                    if x["id"] == p["id"] and p.get("title", "").strip():
                        x["title"] = p["title"].strip()[:60]; x["renamed"] = True
        elif act == "pin":                      # 목록 맨 위에 고정 / 고정 해제
            if "pinned" in p and type(p["pinned"]) is not bool:
                raise UserError("고정 설정 확인 — 고정 상태는 참 또는 거짓이어야 합니다.")
            with meta(True) as m:
                for x in m["sessions"]:
                    if x["id"] == p["id"]:
                        x["pinned"] = p.get("pinned", not x.get("pinned", False))
                        if x["pinned"]:
                            x.setdefault("pinned_at", time.time())
                        else:
                            x.pop("pinned_at", None)
        elif act in ("delete", "restore"):
            with meta(True) as m:
                for x in m["sessions"]:
                    if x["id"] == p["id"]:
                        x["deleted"] = act == "delete"; x["deleted_at"] = time.time()
                        if act == "restore":
                            m["current"] = x["id"]
        elif act == "purge":                    # 휴지통에 있는 것만 영구 삭제
            with meta(True) as m:
                x = next((x for x in m["sessions"] if x["id"] == p["id"] and x["deleted"]), None)
                if x:
                    self.purge(m, x)
        elif act == "empty_trash":
            with meta(True) as m:
                for x in [x for x in m["sessions"] if x["deleted"]]:
                    self.purge(m, x)
        elif act == "resource_limits":
            cpu, memory = p.get("cpu_percent"), p.get("memory_high_gb")
            if type(cpu) is not int or not 1 <= cpu <= 100 or type(memory) not in (int, float) or not math.isfinite(memory) or not 0 <= memory <= physical_memory() / 1024**3:
                raise UserError("리소스 한도 확인 — CPU는 1~100%, 메모리는 이 PC 용량 안에서 설정하세요. 메모리 0은 한도를 끄는 값이에요.")
            try:
                with RESOURCE_LOCK:
                    apply_resource_limits(cpu, memory)
                    atomic_json(D / "resource-limits.json", {"cpu_percent": cpu, "memory_high_gb": memory})
                    RESOURCE_CACHE.clear()
            except (OSError, subprocess.SubprocessError):
                raise UserError("리소스 제한을 적용하지 못했어요. 이 PC의 사용자 systemd·cgroup 지원을 확인하세요.") from None
            return {"ok": "새로 시작하는 Linux 작업부터 한도를 적용해요.", "resources": resource_status(refresh=True)}
        elif act == "open_window":
            if not hasattr(self, "http_server") or not open_window(self.http_server.server_address[1], app_only=True):
                raise UserError("앱 창을 열지 못했어요. Chrome이나 Edge 설치를 확인하세요.")
            return {"ok": "앱 창으로 열었어요."}
        elif act == "open_path":
            return open_report_path(p.get("path"), sid)
        elif act == "merge":
            msg = self.merge(sid)
        elif act in ("job", "msg") and self.take_login_code(sid, p["text"]):
            msg = "로그인 코드를 넣었어요."
        elif act == "job":
            touch(sid, p["text"], request=True)
            with state(True, sid=sid) as s:
                jid = len(s["jobs"]) + 1
                s["jobs"].append({"id": jid, "text": p["text"], "lead": p.get("lead", "claude"),
                                  "status": "planning", "summary": "", "started_at": time.time(),
                                  "verification": p.get("verification", "standard")})
                post(s, "user", "all", p["text"], "request", job=jid)   # 리더만 PLAN으로 깨움, 다른 쪽은 안 깨움
                event(s, f"요청 {jid} 접수 · 리더 {disp(p.get('lead', 'claude'))}", job=jid)
        elif act == "msg":
            touch(sid, p["text"])
            with state(True, sid=sid) as s:
                active = next((j for j in reversed(s["jobs"]) if j["status"] != "done"), None)
                recipient = p.get("to", "all")
                post(s, "user", recipient, p["text"], job=active["id"] if recipient == "all" and active else None)
        elif act == "pause_now":
            if p.get("agent") not in AGENTS:
                raise UserError("에이전트 선택 확인 — 에이전트를 다시 선택하세요.")
            self.ensure_team(sid)
            worker = self.teams[sid]["w"][p["agent"]]
            worker.paused = True
            worker.manual_pause = True
            worker.recovery = None
            worker.retry_at, worker.retry_due = 0, None
            if worker.status == "working":
                worker.immediate_pause = True
                worker.terminate_agent()
            else:
                worker.idle("paused")
            worker.save_checkpoint()
            with state(True, sid) as record:
                event(record, f"{disp(worker.name)} 지금 중단 · 이미 변경한 파일은 유지됩니다. 다시 시작하면 이어서 진행합니다.", job=worker.doing.get("job") if isinstance(worker.doing, dict) else None)
            msg = "작업을 중단했습니다."
        elif act == "pause":
            if p.get("agent") not in AGENTS:
                raise UserError("에이전트 선택 확인 — 에이전트를 다시 선택하세요.")
            self.ensure_team(sid)
            w = self.teams[sid]["w"][p["agent"]]
            if "paused" in p and type(p["paused"]) is not bool:
                raise UserError("멈춤 설정 확인 — 멈춤 상태는 참 또는 거짓이어야 합니다.")
            if not p.get("paused", not w.paused) and getattr(w, "immediate_pause", False):
                raise UserError("중단한 프로세스 정리 중 — 중단한 프로세스를 정리 중입니다. 잠시 후 다시 시작하세요.")
            w.paused = p.get("paused", not w.paused)
            w.manual_pause = w.paused
            w.recovery = None
            if w.paused:
                w.retry_at, w.retry_due = 0, None
            msg = "일시정지함" if w.paused else "재개함"
            if not w.paused:
                w.error = None
                w.error_kind = None
                w.cont = w.chatter = 0
                w.nudge_state = {}
                if isinstance(w.resume_turn, dict):
                    resumed_job = w.resume_turn.get("job")
                    w.retry_counts.pop("direct" if resumed_job is None else str(resumed_job), None)
                w.auto_retries, w.retry_at, w.retry_job = 0, 0, None
                w.retry_due = dict(w.resume_turn) if isinstance(w.resume_turn, dict) else None
            w.save_checkpoint()
        elif act == "cancel_job":
            with state(True, sid=sid) as s:
                if not any(j["id"] == p["id"] for j in s["jobs"]):
                    raise UserError("요청을 찾을 수 없음 — 요청이 없습니다. 요청 현황을 새로고침하세요.")
                for j in s["jobs"]:
                    if j["id"] == p["id"] and (j["status"] != "done" or any(t.get("job") == j["id"] and t["status"] != "done" for t in s["tasks"])):
                        j["status"] = "done"; j["summary"] = "사용자가 취소함"; j["cancelled"] = True; j["finished_at"] = time.time()
                        for t in s["tasks"]:
                            if t.get("job") == j["id"] and t["status"] != "done":
                                t.update(status="done", note="취소됨", done_at=time.time())
                        event(s, f"요청 {j['id']} 취소됨 · 이미 변경한 파일은 유지됩니다.", job=j["id"])
            team = self.teams.get(sid)
            if team:
                for worker in team["w"].values():
                    if isinstance(worker.doing, dict) and worker.doing.get("job") == p["id"]:
                        if worker.status == "working":
                            # 아직 Popen 전이어도 취소 표시를 남겨 준비 중인 실행을 막는다.
                            worker.interrupted_job = p["id"]
                            worker.terminate_agent()
                    if getattr(worker, "resume_turn", None) and worker.resume_turn.get("job") == p["id"]:
                        worker.error, worker.error_kind, worker.resume_turn = None, None, None
                        worker.retry_at, worker.retry_due = 0, None   # 취소한 요청의 자동 재시도 예약도 버림
                        worker.retry_counts.pop(str(p["id"]), None)
                        if getattr(worker, "retry_job", None) == p["id"]:
                            worker.auto_retries, worker.retry_job = 0, None
                        if (worker.notice or "").startswith("자동 재시도 대기"):
                            worker.notice = None
                        worker.fail_count, worker.failure_key = 0, None
                        worker.save_checkpoint()
        elif act == "quit":
            self.shutdown()
        elif act == "update_check":
            with UPDATE_LOCK:
                blocked = (UPDATE.get("retry_at") or 0) > time.time()
                if blocked:
                    minutes = max(1, math.ceil((UPDATE["retry_at"] - time.time()) / 60))
                    msg = f"깃허브 조회 한도를 넘었어요. {minutes}분 뒤 다시 확인해요"
                else:
                    UPDATE["checking"] = True
            if not blocked:
                UPDATE_WAKE.set()
        elif act == "update":
            if not update_info()["available"]:
                raise UserError("이미 최신 버전이에요.")
            if any(w.status == "working" for t in self.teams.values() for w in t["w"].values()):
                raise UserError("일하는 중인 에이전트가 있어요. 작업이 끝난 뒤 업데이트해 보세요.")
            with UPDATE_LOCK:
                if UPDATE["state"] in ("running", "restarting"):
                    raise UserError("이미 업데이트하는 중이에요.")
                UPDATE.update(state="running", message=None)
            threading.Thread(target=run_update, args=(self,), daemon=True).start()
        else:
            raise UserError("동작 처리 불가 — 알 수 없는 동작입니다. 화면을 새로고침하고 다시 시도하세요.")
        if act in ("new_session", "delete", "restore", "purge", "empty_trash"):
            self.sync_teams()
        return msg

    def shutdown(self):
        if self.stop.is_set():
            return
        self.stop.set()
        print("\r\033[KOffice를 종료했습니다.", flush=True)
        def cleanup():
            for sid in list(self.teams):
                try:
                    self.stop_team(sid)
                except (OSError, UserError) as error:
                    server_log("shutdown.team", error)
            try:
                (D / "server.json").unlink(missing_ok=True)
            except OSError as error:
                server_log("shutdown.metadata", error)
            server = getattr(self, "http_server", None)
            if server:
                server.shutdown()
                server.server_close()
        # 종료 요청 응답은 먼저 보냄. non-daemon 정리 스레드가 끝날 때까지 Python이 기다림.
        def finalize():
            try:
                cleanup()
            except Exception as error:
                server_log("shutdown.cleanup", error)
            finally:
                self.cleanup_done.set()
        threading.Thread(target=finalize, daemon=False).start()


def make_handler(office):
    class H(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass

        def reply(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if isinstance(body, str) else ""))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                server_log("http.disconnected")

        def json_reply(self, code, value):
            self.reply(code, json.dumps(value, ensure_ascii=False, allow_nan=False))

        def trusted_request(self):
            port = self.server.server_address[1]
            hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
            if self.headers.get("Host") not in hosts:
                return False
            origin = self.headers.get("Origin")
            return not origin or origin in {"http://" + host for host in hosts}

        def do_GET(self):
            if not self.trusted_request():
                return self.json_reply(403, {"error": "Office 접근 불가 — 이 주소에서는 Office에 접근할 수 없습니다."})
            path = urlsplit(self.path).path
            try:
                if path == "/":
                    self.reply(200, (HERE / "office.html").read_text(), "text/html")
                elif path in ("/favicon.ico", "/icon.png"):
                    f = HERE / ("office.ico" if path == "/favicon.ico" else "office-icon.png")
                    self.reply(200, f.read_bytes(), "image/x-icon" if f.suffix == ".ico" else "image/png")
                elif path == "/api/state":
                    query = parse_qs(urlsplit(self.path).query)
                    selected = query.get("session", [None])[0]
                    value = query.get("since_rev", [None])[0]
                    if value is not None and (not value.isascii() or not value.isdigit() or len(value) > 16):
                        return self.json_reply(400, {"error": "변경 번호 확인 — 변경 번호가 올바르지 않습니다."})
                    previous = int(value) if value is not None else None
                    since_msgs = query.get("since_msgs", [None])[0]
                    if since_msgs is not None and (len(since_msgs) > 64 or not re.fullmatch(r"[0-9-]+", since_msgs)):
                        since_msgs = None
                    if previous is not None or since_msgs:
                        snapshot = office.snapshot(selected, since_rev=previous, since_msgs=since_msgs)
                    else:
                        snapshot = office.snapshot(selected) if selected else office.snapshot()
                    # 창은 하나만: 가장 최근에 인사한 창이 주인. 서버가 꺼지는 중이면 closing 으로 창을 닫게 한다
                    client = query.get("client", [None])[0]
                    if client:
                        if getattr(office, "window_owner", None) is None:
                            office.window_owner = client
                        snapshot["owner"] = client == office.window_owner
                        if snapshot["owner"]:
                            office.window_seen = time.time()
                    snapshot["closing"] = office.stop.is_set()
                    self.json_reply(200, snapshot)
                elif path == "/api/window":
                    self.json_reply(200, {"open": time.time() - getattr(office, "window_seen", 0) < 6})
                else:
                    self.json_reply(404, {"error": "주소를 찾을 수 없음 — 주소를 찾을 수 없습니다."})
            except (UserError, OSError, ValueError) as error:
                server_log("http.get", error)
                message = str(error) if isinstance(error, UserError) else "화면 정보 읽기 실패 — 화면 정보를 읽지 못했습니다. 디스크와 폴더 권한을 확인하세요."
                self.json_reply(500, {"error": message})
            except Exception as error:
                server_log("http.get.unexpected", error)
                self.json_reply(500, {"error": "화면 정보 처리 실패 — 화면 정보를 처리하지 못했습니다. 잠시 후 다시 시도하세요."})

        def do_POST(self):
            if urlsplit(self.path).path != "/api":
                return self.json_reply(404, {"error": "주소를 찾을 수 없음 — 주소를 찾을 수 없습니다."})
            if not self.trusted_request():
                return self.json_reply(403, {"error": "요청 출처 확인 필요 — 다른 웹사이트에서 보낸 요청은 허용하지 않습니다."})
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0:
                    raise ValueError("length")
                if length > MAX_BODY:
                    self.close_connection = True
                    return self.json_reply(413, {"error": "내용 크기 초과 — 내용이 너무 큽니다. 나눠서 보내세요."})
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("incomplete")
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                self.close_connection = True
                return self.json_reply(400, {"error": "요청 형식 확인 — 요청 형식이 올바르지 않습니다."})
            except (TimeoutError, OSError) as error:
                server_log("http.read", error)
                self.close_connection = True
                return self.json_reply(408, {"error": "요청 수신 시간 초과 — 요청 수신 시간이 초과됐습니다. 다시 보내세요."})
            try:
                message = office.handle(payload)
                self.json_reply(200, message if isinstance(message, dict) else ({"ok": message} if message else {}))
            except UserError as error:
                self.json_reply(400, {"error": str(error)})
            except Exception as error:
                server_log("http.post", error)
                self.json_reply(500, {"error": "요청 처리 오류 — 요청 처리 중 오류가 발생했습니다. 디스크와 폴더 권한을 확인하세요."})
    return H


def post_update_note():
    """업데이트 때 찾은 로컬 수정 결과를 다시 켜진 뒤 지금 대화에 한 번 알린다"""
    note = D / "update-note.txt"
    try:
        text = note.read_text().strip()
        note.unlink()
        if text:
            with state(True, current_sid()) as s:
                post(s, "system", "user", ui_notice("업데이트하며 로컬 수정을 확인했어요", text), "event")
    except FileNotFoundError:
        pass
    except (OSError, UserError) as error:
        server_log("update.local_note", error)


def serve():
    load_limits()   # 에이전트 시작 전에 지난 관측의 유효한 창을 복원
    post_update_note()
    office = Office()
    global LOGIN_HOOK
    LOGIN_HOOK = office.offer_login

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(office))
    srv.daemon_threads = True
    office.http_server = srv
    port = srv.server_address[1]
    (D / "server.json").write_text(json.dumps({"pid": os.getpid(), "port": port}))
    detect_worktree_mode()
    office.sync_teams()
    threading.Thread(target=codex_limits, daemon=True).start()    # 과거 관측을 보조로 표시
    threading.Thread(target=check_update, args=(office.stop,), daemon=True).start()
    threading.Thread(target=office.first_run_login, daemon=True).start()   # 처음 쓰는 사람은 로그인부터
    for name in AGENTS:
        threading.Thread(target=refresh_provider_limits, args=(name, office.stop), daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGHUP):     # office stop / 콘솔 창 닫기
        signal.signal(sig, lambda *a: office.shutdown())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    opened = open_window(port)
    status_line(office, port, opened=opened)
    if office.stop.is_set():
        office.cleanup_done.wait()   # 정리가 끝나기 전에 서버 수명 잠금을 풀지 않는다.
        if getattr(office, "restart", None):
            # 새 버전으로 다시 시작: 서버 잠금 파일은 exec 때 닫혀 풀리고, 새 프로세스가 새 창을 연다
            os.execv(office.restart[0], office.restart)


def until(ts):
    """초기화까지 남은 시간을 짧게: '2시간 뒤', '3일 뒤'"""
    if not ts:
        return ""
    m = max(0, int((ts - time.time()) // 60))
    return f"{m}분 뒤" if m < 60 else f"{m // 60}시간 뒤" if m < 1440 else f"{m // 1440}일 뒤"


def pad(text, width):
    return text + " " * max(0, width - console_width(text))


# 콘솔 색: 대화형 터미널에서만 켬(status_line에서 정함). 파일·파이프 출력과 NO_COLOR에선 글자만
CONSOLE_COLOR = False
ANSI_SGR = re.compile(r"\033\[[0-9;]*m")


def paint(text, *codes):
    return f"\033[{';'.join(map(str, codes))}m{text}\033[0m" if CONSOLE_COLOR and text else text


# 잔량 배터리: 10칸에 남은 양(100-사용률)을 채움. 조금이라도 남으면 한 칸은 채움. 50% 초과 초록·20% 초과 노랑·그 이하 빨강
# ▰▱는 동아시아 폭이 N이라 터미널마다 한 칸으로 같게 보임(█·■는 A라 두 칸으로 그리는 터미널이 있음)
def battery(left):
    fill = min(10, max(1 if left > 0 else 0, round(left / 10)))
    color = 32 if left > 50 else 33 if left > 20 else 31
    return paint("▰" * fill, color) + paint("▱" * (10 - fill), 2)


# 콘솔 아래쪽 사용량 표: 에이전트별 5시간·7일 창을 전부, 모르는 칸은 '정보 없음'. 줄 수는 항상 고정(제자리 갱신용)
def usage_lines(width):
    cell = 36
    def one(w, known):
        if not w or w.get("used") is None:
            # 다른 창은 받았는데 이 창만 없으면 그 요금제엔 해당 한도가 없는 것 (예: Codex는 7일 한도만)
            return pad(paint("-" if known else "정보 없음", 2), cell)
        # 모든 칸을 왼쪽 정렬: '-'·막대·제목이 같은 열에서 시작. ⚠는 터미널마다 폭이 달라 정렬 깨져서 '!'로
        left = max(0, min(100, 100 - int(w["used"])))
        num = paint(pad(f"{left}%{'!' if left <= 20 else ''}", 5), 1, *((31,) if left <= 20 else ()))
        return pad(f"{battery(left)} {num} {paint(f"{until(w.get('resets'))} 초기화" if w.get("resets") else "", 2)}", cell)
    # 에이전트마다 이름 색을 달리하고 사이에 구분선을 둬서 두 줄이 섞여 보이지 않게 함
    names = {"claude": 33, "codex": 36}
    rows, seen = [pad(paint("  남은 양", 1), 10) + pad(paint("5시간", 2), cell) + paint("7일", 2)], False
    for i, n in enumerate(AGENTS):
        L = limits_for(n) or {}
        seen = seen or bool(L.get("at"))
        known = any((L.get(k) or {}).get("used") is not None for k in ("five_hour", "seven_day"))
        # 기준 시각은 에이전트별로 따로: 그 에이전트 값이 10분 넘게 묵었을 때만 그 줄 끝에 표시
        mins = int((time.time() - L["at"]) // 60) if L.get("at") else 0
        stale = paint(f" {mins}분 전 값", 33) if mins >= 10 else ""
        if i:
            rows.append(paint("  " + "-" * 70, 2))
        rows.append(pad("  " + paint(disp(n), 1, names.get(n, 37)), 10) + one(L.get("five_hour"), known)
                    + one(L.get("seven_day"), known) + stale)
    # 줄 수는 고정해야 해서(제자리 갱신용) 받은 값이 있으면 빈 줄
    rows.append("" if seen else paint("  아직 받은 사용량 정보 없음", 2))
    return [fit_console(r, width) for r in rows]


def console_width(text):
    return sum(0 if unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me", "Cf")
               else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in ANSI_SGR.sub("", text))


# 색 코드(SGR)는 폭 0으로 보고 살림. 그 밖의 출력 불가 문자는 버림. 잘랐으면 색을 끝에서 되돌림
def fit_console(text, width):
    parts = [t for t in re.split(f"({ANSI_SGR.pattern})", text) if t]
    parts = [t if ANSI_SGR.fullmatch(t) else "".join(c for c in t if c.isprintable()) for t in parts]
    text = "".join(parts)
    width = max(0, width)
    if console_width(text) <= width:
        return text
    out, used, colored = [], 0, False
    for t in parts:
        if ANSI_SGR.fullmatch(t):
            out.append(t); colored = True
            continue
        for c in t:
            size = console_width(c)
            if used + size > width - 1:
                return "".join(out) + ("\033[0m" if colored else "") + ("…" if width else "")
            out.append(c); used += size
    return "".join(out)


def console_status(office, up, frame):
    ws = [w for t in list(office.teams.values()) for w in t["w"].values()]
    working = [w for w in ws if w.status == "working"]
    stopped = sorted({disp(w.name) for w in ws if w.status == "paused" or w.error})
    stopping = sorted({disp(w.name) for w in ws if w.paused and w.status == "working" and not w.error})
    notices = ([paint(f"⚠ {', '.join(stopped)} 멈춤 · 화면 확인", 1, 31)] if stopped else [])
    if stopping:
        notices.append(paint(f"{', '.join(stopping)} 차례 끝나면 멈춤", 33))
    doing = paint("모두 대기", 2)
    if working:
        who = ", ".join(sorted({disp(w.name) for w in working})) if len(office.teams) == 1 else f"{len(working)}명"
        doing = paint(f"지금 일하는 중: {who}", 1, 32)
    parts = notices + [f"실행 중 {paint(f'{up // 3600:02d}:{up // 60 % 60:02d}:{up % 60:02d}', 1)}",
                       f"대화 {paint(str(len(office.teams)), 1)}개", doing]
    width = max(0, shutil.get_terminal_size(fallback=(100, 24)).columns - 1)
    table = usage_lines(width)
    spin = paint('⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'[frame % 10], 1, 36)
    lines = [fit_console(f"{spin} " + paint(" · ", 2).join(parts), width), ""] + table
    # 비TTY 중복 억제용 키: 스피너·경과시간·'N분 전'처럼 매번 바뀌는 건 빼고 상태와 사용률만
    usage_key = tuple((n, tuple(((limits_for(n) or {}).get(k) or {}).get("used") for k in ("five_hour", "seven_day")))
                      for n in AGENTS)
    return lines, (len(office.teams), tuple(w.name for w in working), tuple(stopped), tuple(stopping), usage_key)


# 좁은 터미널에서도 갱신 줄이 아래로 쌓이지 않게 화면 폭보다 한 칸 짧게 출력.
def status_line(office, port, opened=True):
    global CONSOLE_COLOR
    interactive = sys.stdout.isatty()
    CONSOLE_COLOR = interactive and not os.environ.get("NO_COLOR")
    if interactive:   # top·less처럼 대체 화면에서 그림: 이전 터미널 내용이 안 보이고, 끝나면 원래 화면으로 깔끔히 복귀
        print("\033[?1049h\033[2J\033[H", end="", flush=True)
    try:
        status_loop(office, port, opened, interactive)
    finally:
        if interactive:
            print("\033[?1049l", end="", flush=True)
            if office.stop.is_set():
                print("Office를 종료했습니다.", flush=True)


def status_loop(office, port, opened, interactive):
    print(f"{paint(' Office ', 1, 30, 46)}  {paint(f'http://localhost:{port}/', 4, 36)}")
    print(f"{paint('폴더', 2)}    {ROOT}")
    print(paint("이 콘솔은 켜 두세요 · 앱 창 다시 열기 R · 종료 Ctrl+C", 2))
    if not opened:
        print(paint("창을 자동으로 열지 못했어요. 위 주소를 브라우저에서 열어 보세요.", 33))
    print()
    start, i, last, drawn = time.time(), 0, None, 0
    # 키를 눌러도 화면에 글자·줄바꿈이 찍히지 않게 입력 에코를 끈다(Enter 가 찍히면 커서가 내려가 상태 줄이 아래로 쌓임)
    tty_fd, tty_saved = None, None
    if interactive and sys.stdin.isatty():
        try:
            import termios
            tty_fd = sys.stdin.fileno()
            tty_saved = termios.tcgetattr(tty_fd)
            raw = termios.tcgetattr(tty_fd)
            raw[3] &= ~(termios.ECHO | termios.ICANON)
            termios.tcsetattr(tty_fd, termios.TCSADRAIN, raw)
        except (ImportError, OSError, termios.error):
            tty_fd = tty_saved = None
    try:
        while not office.stop.is_set():
            lines, key = console_status(office, int(time.time() - start), i)
            if interactive:   # 여러 줄을 제자리에서 다시 그림: 앞서 그린 줄 수만큼 위로 올라가 한 줄씩 지우고 씀
                up_ = f"\033[{drawn - 1}F" if drawn > 1 else "\r"
                sys.stdout.write(up_ + "\n".join("\033[K" + l for l in lines))
                sys.stdout.flush()
                drawn = len(lines)
            elif key != last:
                print("\n".join(lines), flush=True)
            last = key
            i += 1
            if interactive and sys.stdin.isatty():
                import select
                if select.select([sys.stdin], [], [], 0)[0]:
                    if tty_fd is None:
                        pressed = sys.stdin.readline().strip().lower() == "r"
                    else:
                        pressed = "r" in os.read(tty_fd, 256).decode(errors="ignore").lower()
                    if pressed:
                        open_window(port, app_only=True)
            office.stop.wait(0.2)
    except KeyboardInterrupt:
        office.shutdown()
    finally:
        if tty_fd is not None:
            import termios
            try:
                termios.tcsetattr(tty_fd, termios.TCSADRAIN, tty_saved)
            except (OSError, termios.error):
                pass
    time.sleep(1)


def running():
    f = D / "server.json"
    if not f.exists():
        return None
    try:
        info = json.loads(f.read_text())
        if (not isinstance(info, dict) or type(info.get("pid")) is not int or info["pid"] <= 0
                or type(info.get("port")) is not int or not 1 <= info["port"] <= 65535):
            raise ValueError("잘못된 실행 정보")
        os.kill(info["pid"], 0)
    except (ValueError, KeyError, ProcessLookupError, FileNotFoundError):
        f.unlink(missing_ok=True)
        return None
    except PermissionError:
        return info   # 접근이 막혔다고 이미 켜진 서버를 새로 띄우지 않음
    try:
        socket.create_connection(("127.0.0.1", info["port"]), timeout=1).close()
    except ConnectionRefusedError:
        f.unlink(missing_ok=True)
        return None
    except OSError:
        pass   # 시간 초과·접근 제한은 서버가 종료됐다는 근거가 아님
    return info


def open_window(port, app_only=False):
    url = f"http://localhost:{port}/"
    for exe in (CHROME, EDGE):
        if Path(exe).exists():
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1500,920"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd="/mnt/c")
                return True
            except OSError:
                continue
    if app_only:
        return False
    try:
        subprocess.Popen(["explorer.exe", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def window_open(port):
    try:
        opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/window", timeout=3) as response:
            return bool(json.loads(response.read()).get("open"))
    except (OSError, ValueError):
        return False


def process_started_at(pid):
    """프로세스 시작 시각(epoch). /proc/<pid>/stat의 부팅 후 틱 + 부팅 시각"""
    try:
        ticks = int((Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19])
        boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime "))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def restart_stale_server(info):
    """설치 명령을 다시 실행해 앱 파일이 바뀌었는데 예전 서버가 돌고 있으면 끄고 새 버전으로 띄운다.
    실행 중 서버가 보고하는 버전은 디스크의 VERSION을 읽어서 믿을 수 없으므로 프로세스 시작 시각과 파일 수정 시각을 비교한다.
    끄고 새로 띄우면 True, 그대로 둬야 하면 False"""
    started = process_started_at(info["pid"])
    try:
        changed = Path(__file__).resolve().stat().st_mtime
    except OSError:
        return False
    if started is None or changed <= started + 1:
        return False
    try:
        opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{info['port']}/api/state", timeout=5) as response:
            state = json.loads(response.read())
    except (OSError, ValueError):
        return False
    if any(x.get("busy") for x in state.get("sessions") or []):
        print(f"새 버전 {office_version()}을 설치했어요. 에이전트가 일하는 중이라 지금은 그대로 둘게요.")
        print("작업이 끝나면 office를 다시 실행하세요. 새 버전으로 바뀌어요.")
        return False
    print(f"새 버전 {office_version()}을 적용하려고 실행 중인 Office를 다시 시작해요.")
    try:
        os.kill(info["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return True
    for _ in range(300):    # 정리(에이전트 종료·잠금 해제)가 끝날 때까지 최대 30초
        try:
            os.kill(info["pid"], 0)
        except ProcessLookupError:
            return True
        time.sleep(.1)
    raise UserError("Office 종료 대기 시간 초과 — 실행 중인 Office가 꺼지지 않았습니다. office stop 후 다시 실행하세요.")


def cmd_gui(a):
    info = running()
    if info and restart_stale_server(info):
        info = running()
    if info and window_open(info["port"]):   # 서버도 창도 하나만: 이미 열린 창이 있으면 새로 띄우지 않는다
        return print("Office가 이미 실행 중이에요 · 열려 있는 창을 쓰세요.")
    if info:                     # 다른 콘솔에서 이미 돌고 있으면 창만 다시 엶
        if open_window(info["port"]):
            return print("Office 실행 중 · 창을 다시 열었습니다.")
        return print(f"Office 실행 중 · 브라우저에서 http://localhost:{info['port']}/ 를 열어주세요.")
    D.mkdir(parents=True, exist_ok=True)
    with (D / "server.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UserError("Office 시작·종료 중 — Office가 시작되거나 종료되는 중입니다. 잠시 후 다시 실행하세요.") from None
        # 서로 다른 콘솔이 동시에 시작해도 잠금 뒤 실행 정보를 다시 확인한다.
        info = running()
        if info:
            open_window(info["port"])
            return print("Office 실행 중 · 창을 다시 열었습니다.")
        current_sid()
        serve()


def cmd_stop(a):
    info = running()
    if not info:
        return print("실행 중인 Office가 없습니다.")
    try:
        os.kill(info["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return print("실행 중인 Office가 없습니다.")
    print("Office를 종료했습니다.")


def cmd_shot(a):
    """지금 떠 있는 Office 화면을 윈도우 Chrome/Edge(창 없이)로 캡처해 .office/shots/에 PNG로 저장하고 경로를 출력.
    에이전트가 Read로 열어 실제 화면을 확인하는 용도. 크기: wide=1500x920, narrow=1000x800"""
    if a.session and not valid_sid(a.session):
        raise UserError("캡처 대화 확인 — 올바른 대화 ID를 지정하세요.")
    info = running()
    if not info:
        sys.exit("실행 중인 Office가 없어 캡처할 수 없습니다.")
    exe = next((p for p in (CHROME, EDGE) if Path(p).exists()), None)
    if not exe:
        sys.exit("Chrome/Edge를 찾지 못했습니다.")
    sizes = {"wide": "1500,920", "narrow": "1000,800"}
    # 테마는 ?theme=로 화면에 직접 지정(브라우저 강제 다크는 색을 왜곡하고 OS 설정에 휘둘림)
    theme = "dark" if a.dark else a.theme
    q = "&".join(x for x in (f"session={a.session}" if a.session else "", f"theme={theme}" if theme else "") if x)
    url = f"http://localhost:{info['port']}/" + (f"?{q}" if q else "")
    out_dir = D / "shots"; out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for size in ([a.size] if a.size != "both" else ["wide", "narrow"]):
        # 윈도우 브라우저가 쓸 수 있는 공용 폴더에 찍고 WSL 쪽으로 옮김. 실행 중인 브라우저와 안 겹치게 별도 프로필
        win_tmp = Path("/mnt/c/Users/Public") / f"office-shot-{stamp}-{size}.png"
        args = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={sizes[size]}",
                "--virtual-time-budget=5000", r"--user-data-dir=C:\Users\Public\office-shot-profile",
                f"--screenshot=C:\\Users\\Public\\{win_tmp.name}", url]
        try:
            subprocess.run(args, cwd="/mnt/c", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            sys.exit(f"캡처 실패({size}): {type(e).__name__}")
        if not win_tmp.exists():
            sys.exit(f"캡처 실패({size}): 브라우저가 이미지를 만들지 못했습니다.")
        dst = out_dir / f"{stamp}-{size}{f'-{theme}' if theme else ''}.png"
        shutil.move(str(win_tmp), dst)
        print(dst)


def office_version():
    try:
        return (Path(__file__).resolve().parent / "VERSION").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def main():
    argv = sys.argv[1:]
    if argv[:1] in (["--version"], ["-V"], ["version"]):
        print(f"Office {office_version()}")
        return
    sub = {"login", "import", "stop", "board", "log", "add", "claim", "done", "wait", "need-user", "resolve-user", "bg", "progress", "handoff", "say", "finish", "title", "lesson", "shot", "reserve", "release", "write", "-h", "--help"}
    if not argv or argv[0] not in sub:          # office [폴더]  → GUI
        if argv:
            # 오타난 명령이 새 작업 폴더로 열리지 않게, 이미 있는 폴더만 받는다
            if not Path(argv[0]).expanduser().is_dir():
                raise UserError(f"모르는 명령이거나 없는 폴더입니다: {argv[0]}\n명령은 office -h, 버전은 office --version으로 확인하세요.")
            set_root(argv[0])
        return cmd_gui(None)
    p = argparse.ArgumentParser(prog="office", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    x = sp.add_parser("stop"); x.add_argument("dir", nargs="?"); x.set_defaults(f=cmd_stop)
    x = sp.add_parser("login", help="대화에서 로그인: start·code·status"); x.add_argument("step", choices=("start", "code", "status"))
    x.add_argument("name", choices=AGENTS); x.add_argument("code", nargs="?"); x.set_defaults(f=cmd_login)
    x = sp.add_parser("import", help="다른 작업 폴더의 대화 가져오기"); x.add_argument("source"); x.set_defaults(f=cmd_import)
    sp.add_parser("board").set_defaults(f=cmd_board)
    x = sp.add_parser("log"); x.add_argument("dir", nargs="?"); x.add_argument("-n", type=int, default=20); x.add_argument("-f", "--follow", action="store_true")
    x.set_defaults(f=cmd_log)
    x = sp.add_parser("reserve"); x.add_argument("path"); x.set_defaults(f=cmd_reserve)
    x = sp.add_parser("release"); x.add_argument("path"); x.set_defaults(f=cmd_release)
    x = sp.add_parser("write"); x.add_argument("path"); x.add_argument("--from", dest="source", required=True); x.set_defaults(f=cmd_write)
    x = sp.add_parser("add"); x.add_argument("text"); x.add_argument("--to", choices=AGENTS)
    x.add_argument("--level", choices=LEVELS, help="작업 무게: light(문구·확인) normal heavy(장애 분석·큰 구현)"); x.set_defaults(f=cmd_add)
    x = sp.add_parser("claim"); x.add_argument("id", type=int); x.set_defaults(f=cmd_claim)
    x = sp.add_parser("done"); x.add_argument("id", type=int); x.add_argument("text", nargs="?", default=""); x.set_defaults(f=cmd_done)
    x = sp.add_parser("wait", help="작업을 기다림으로 두기"); x.add_argument("id", type=int); x.add_argument("text")
    x.add_argument("--pid", type=int, help="감시할 프로세스 (끝나면 깨움)"); x.add_argument("--log", help="진행 로그 파일 (오래 안 바뀌면 깨움)")
    x.add_argument("--minutes", type=float, help="기한(분). 지나면 깨움. 기본 15분, --pid 를 주면 기한 없음"); x.set_defaults(f=cmd_wait)
    x = sp.add_parser("need-user", help="사용자에게 필요한 자료·행동을 남기기"); x.add_argument("id", type=int); x.add_argument("text")
    x.add_argument("--expires-at"); x.set_defaults(f=cmd_need_user)
    x = sp.add_parser("resolve-user", help="사용자 자료·답 확인 후 작업 재개"); x.add_argument("id", type=int); x.add_argument("text")
    x.add_argument("--re", type=int); x.set_defaults(f=cmd_resolve_user)
    x = sp.add_parser("bg", help="오래 걸리는 명령을 Office가 관리하는 백그라운드로 실행"); x.add_argument("id", type=int)
    x.add_argument("text", help="무엇을 하는지 짧게"); x.add_argument("command", help="실행할 셸 명령"); x.add_argument("--log", help="로그 파일 (기본: 대화 폴더 bg/)")
    x.add_argument("--minutes", type=float, help="기한(분). 지나면 깨움"); x.set_defaults(f=cmd_bg)
    x = sp.add_parser("progress", help="오래 도는 작업의 진행 남기기 (화면에 늘 보임)")
    x.add_argument("words", nargs="*", help="[작업 ID] 지금 상태 한 줄"); x.add_argument("--done", type=int); x.add_argument("--total", type=int)
    x.add_argument("--state", choices=("working", "waiting"), help="실제 실행 중인지, 입력·다른 작업을 기다리는지 지정")
    x.add_argument("--resume-at", help="쉬는 중이면 다시 시작할 시각 (20:10, +15m, ISO)")
    x.add_argument("--notify", action="store_true", help="사용자에게도 한 줄 알림 (상태가 바뀔 때만)"); x.set_defaults(f=cmd_progress)
    x = sp.add_parser("handoff"); x.add_argument("id", type=int); x.add_argument("--to", choices=AGENTS, required=True)
    x.add_argument("text", nargs="?", default=""); x.set_defaults(f=cmd_handoff)
    x = sp.add_parser("say"); x.add_argument("text"); x.add_argument("--to", default="all", choices=(*AGENTS, "user", "all"))
    x.add_argument("--re", type=int, help="답장할 사용자 메시지 ID (생략하면 자동 연결)")
    x.add_argument("--info", action="store_true", help="진행 알림만 남기고 상대를 깨우지 않음")
    x.set_defaults(f=cmd_say)
    x = sp.add_parser("finish"); x.add_argument("text"); x.set_defaults(f=cmd_finish)
    x = sp.add_parser("lesson"); x.add_argument("text"); x.set_defaults(f=cmd_lesson)
    x = sp.add_parser("title"); x.add_argument("text"); x.set_defaults(f=cmd_title)
    x = sp.add_parser("shot"); x.add_argument("size", nargs="?", default="both", choices=("wide", "narrow", "both"))
    x.add_argument("--dark", action="store_true"); x.add_argument("--theme", choices=["light", "dark"])
    x.add_argument("--session"); x.set_defaults(f=cmd_shot)
    a = p.parse_args(argv)
    if getattr(a, "dir", None):
        set_root(a.dir)
    a.f(a)


if __name__ == "__main__":
    try:
        main()
    except UserError as error:
        if len(sys.argv) < 2 or sys.argv[1] != "log":
            server_log("cli.user", error)
        sys.exit(str(error))
    except OSError as error:
        if len(sys.argv) < 2 or sys.argv[1] != "log":
            server_log("cli.storage", error)
        sys.exit(f"Office 파일 처리 실패 ({type(error).__name__}, errno={error.errno}). 폴더 권한·디스크 용량과 server.log를 확인하세요.")
    except Exception as error:
        server_log("cli.unexpected", error)
        sys.exit(f"Office 처리 오류 ({type(error).__name__}). server.log를 확인하세요.")
