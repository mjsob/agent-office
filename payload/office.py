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
  office claim ID | done ID "요약" | handoff ID --to X "사유" | say --to X "말" | add "할일" [--to X] | finish "보고" | title "대화 요약 제목"
"""
import math, tomllib, hashlib, ssl
import argparse, copy, datetime, fcntl, json, os, re, shlex, shutil, signal, socket, subprocess, sys, selectors, threading, time, tempfile, unicodedata, uuid
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qs

AGENTS = ("claude", "codex")
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


def state_valid(data):
    if not isinstance(data, dict):
        return False
    if type(data.get("seq", 0)) is not int or data.get("seq", 0) < 0:
        return False
    for key in ("jobs", "tasks", "msgs"):
        if not isinstance(data.get(key, []), list):
            return False
        for item in data.get(key, []):
            if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] < 1:
                return False
            if key == "jobs" and (item.get("status") not in ("planning", "working", "review", "done")
                    or item.get("lead") not in AGENTS or not isinstance(item.get("text"), str)):
                return False
            if key == "tasks" and (item.get("status") not in ("todo", "doing", "done")
                    or item.get("owner") not in (*AGENTS, None) or not isinstance(item.get("title"), str)):
                return False
            if key == "msgs" and any(not isinstance(item.get(field), str) for field in ("ts", "from", "to", "text")):
                return False
    return True


def read_json_file(path, default, validator=None, required=False):
    check = validator or (lambda value: isinstance(value, dict))
    if not path.exists():
        return copy.deepcopy(default)
    try:
        data = json.loads(path.read_text())
        if not check(data):
            raise ValueError("schema")
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
            f.write(payload); f.flush(); os.fsync(f.fileno())
        temporary.replace(path)
        temporary = None
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
    m["sessions"].append({"id": sid, "title": title, "created": time.time(), "updated": time.time(), "deleted": False})
    m["current"] = sid
    return sid


def current_sid():
    if os.environ.get("OFFICE_SESSION"):
        return os.environ["OFFICE_SESSION"]
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


def reply_target(s, actor, explicit=None):
    """가장 이른 미응답 사용자 메시지. 방송 답변 여부는 담당별로 계산."""
    candidates = [m for m in s["msgs"] if m.get("from") == "user"
                  and m.get("to") in (actor, "all") and m.get("kind") == "msg"
                  and m.get("job") is None]
    if explicit is not None:
        if type(explicit) is not int or not any(m["id"] == explicit for m in candidates):
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
    icon = {"todo": "⬜", "doing": "🔨", "done": "✅"}
    out = ["== 요청(job) =="]
    out += [f"요청 {j['id']} [{j['status']}] 리더={j['lead']} : {j['text']}" for j in s["jobs"] if j["status"] != "done"] or ["(없음)"]
    out.append("== 작업판 ==")
    out += [f"{icon[t['status']]} #{t['id']} (요청 {t.get('job')}) [{t['owner'] or '미배정'}] {t['title']}"
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


def guarded_path(value):
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
    if path.exists() and not path.is_file():
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
    path, identity = guarded_path(a.path), reservation_owner(allow_closed=True)
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
        t.update(owner=me(), status="doing", claimed_at=time.time())
        event(s, f"작업 #{a.id} 시작", me(), job=message_job(s))
        print(f"#{a.id} 맡음")


def cmd_done(a):
    with state(True) as s:
        t = find(s, a.id)
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
        kind = "note" if getattr(a, "info", False) else "msg"
        explicit = getattr(a, "re", None)
        if explicit is not None and (me() not in AGENTS or a.to != "user" or kind != "msg"):
            raise UserError("답장 대상 확인 — --re는 담당자의 사용자 답장에만 사용할 수 있습니다.")
        reply_to = reply_target(s, me(), explicit) if me() in AGENTS and a.to == "user" and kind == "msg" and (job is None or explicit is not None) else None
        now = time.time()
        if any(m.get("re") == reply_to and m.get("from") == me() and m.get("to") == a.to and m.get("job") == job and m.get("kind") == kind and m.get("text", "").strip() == a.text.strip() and now-m.get("at", 0) < 120 for m in s["msgs"][-10:]):
            return print("중복 메시지 생략")
        post(s, me(), a.to, a.text, kind, job=job, reply_to=reply_to)
        s["msgs"][-1]["at"] = now


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
        if any(t.get("job") == j["id"] and t["status"] != "done" for t in s["tasks"]):
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
  office handoff ID --to {other} "진행상황/이유"   넘기기
  office say --to {other} "말"      동료에게 의견·리뷰요청·질문 (--to user 는 사용자)
  office finish "최종 보고"         (리더만) 일 전체 마무리하고 사용자에게 보고
  office title "대화 요약 제목"      (리더만) 대화 전체 주제를 요약한 짧은 제목 갱신 (사용자가 정한 이름은 유지)
일하는 방식:
- 네 담당 작업부터 처리. 시작할 때 `office claim ID`로 진행중 표시 (주인 없는 todo도 claim으로 가져감). 동료 담당은 건드리지 마라.
- 상대가 더 잘할 일이거나 막히면 handoff. 의견이 필요하면 say로 묻고 기다리는 동안 다른 일 해라.
- 외부 프로세스(tmux·서버·다른 CLI)에 입력을 보냈으면 실제로 들어갔는지 바로 확인해라. 결과를 1분 넘게 반복 확인하며 기다리지 마라.
- 동료 완료·답을 기다릴 때 sleep·백그라운드 작업·반복 확인으로 차례를 붙잡지 마라. 할 일이 없으면 차례를 끝내라. 새 메시지·작업 변화가 생기면 서버가 다시 깨운다(차례를 붙잡고 있으면 그 사이 넘어온 일을 못 받는다).
- 동료가 작업 중인 파일은 직접 고치지 말고 say로 조율.
- 사용자한테는 꼭 필요한 결정만 물어라. 웬만한 건 둘이 정해서 끝내라.
- 파일 수정 전에 office reserve "파일"로 예약한다. 다른 담당/대화가 예약한 파일은 읽기·검수만 한다.
- 원본 파일은 직접 Edit/Write/셸로 저장하지 말고 원본을 /tmp 임시 원고로 복사하고 원고만 부분 수정한 뒤 office write "파일" --from "임시 원고"로 저장한다. 끝나면 office release "파일".
- 작업 추가 전에 같은 작업이 있는지 확인하고 기존 ID를 사용한다. 작은 수정은 한 명이 끝내고 분리 가능한 작업·독립 검수에만 둘을 쓴다.
- 진행 알림은 office say --info로 남긴다(상대를 깨우지 않음). 일반 say는 질문·막힘·결정 등 상대가 행동해야 할 때만 쓴다.- 메시지는 필요한 것만. 인사·감사·맞장구 금지.
- 사용자에게 직접 받은 메시지는 job·작업판이 비어 있어도 요청이다. 질문에는 답하고, 수정·실행 요청은 작업을 등록·claim한 뒤 실제로 완료해라. 단독 요청을 '대기'로 무시하지 마라.
- 처리할 사용자 메시지도 담당·미배정 작업도 없을 때만 명령 실행 없이 '대기'라고 답해라.
사용자 보고 형식 (finish 및 사용자에게 보내는 say):
- 첫 줄은 40자 이내 한 줄 요약으로 쓴다. 빈 줄 뒤에 '- ' 목록으로 변경·검증·남은 일을 쓴다.
- 목록은 항목당 한 줄로 짧게 쓴다. 내용이 길면 문장을 나눠 줄바꿈한다.
- 목록 항목이 3개를 넘으면 목차별로 묶는다. '**바뀐 것**', '**확인한 것**', '**남은 일**' 같은 제목만 있는 줄을 쓰고, 빈 줄 뒤에 해당 '- ' 항목을 배치한다.
- 섹션은 해당 내용이 있을 때만 쓴다. 항목당 한 가지 내용만 쓰고, 짧은 보고(항목 3개 이하)는 제목을 생략한다.
- 줄바꿈은 실제 개행으로 전달한다. 역슬래시와 n 글자를 줄바꿈 대신 쓰지 않는다.
- ①② 같은 기호나 [구분]을 한 줄에 몰아 쓰지 않는다. 파일 이름·구현 용어는 사용자에게 필요할 때만 쓴다.
"""

PLAN = """[리더 차례: 새 요청 접수] 요청 {jid}: {text}
이 일을 끝까지 책임지는 리더는 너다. 필요하면 파일을 살펴본 뒤 작업으로 쪼개서
`office add "..." --to claude|codex` 로 둘에게 나눠 배정해라 (너 자신 포함, 병렬로 할 수 있게, 같은 파일 겹치지 않게).
쪼갤 필요 없는 작은 일이면 직접 처리하고 `office finish "보고"` 해도 된다.
구현·조사·검증 등 실제 작업을 둘에게 고르게 나눠라. 리더가 계획·검수만을 이유로 구현까지 혼자 맡지 마라.
독립적으로 진행할 작업은 먼저 동료에게 배정해 동시 진행하고, 단순 문구 변경처럼 나눌 필요 없는 작업만 직접 처리해라.
배정 전 작업판에서 각자 아직 안 끝난 작업 수를 확인해 덜 바쁜 쪽에 먼저 준다. 단 한쪽이 확실히 더 잘하는 일(그 사람이 이미 손대는 파일 포함)이면 바빠도 그쪽에 준다.
배정 후엔 네 몫부터 바로 시작해라. 사용자 보고는 첫 줄 40자 이내 요약, 빈 줄, 짧은 '- ' 목록 형식을 따른다.
아래 대화 전체 요청 이력을 참고해 전체 주제를 아우르는 15자 내외의 짧은 어구로 `office title "제목"`을 실행해라. 마지막 요청 원문을 그대로 제목으로 쓰지 마라."""

REVIEW = """[리더 차례: 검수] 요청 {jid}: {text}
배정한 작업이 전부 완료됐다. 결과물을 직접 확인해서 원래 요구사항을 다 만족하는지 검수해라.
문제가 있으면 `office add "수정할 것" --to 담당` 으로 다시 배정.
문제없으면 대화 전체 요청 이력을 참고해 전체 주제를 아우르는 15자 내외 제목으로 `office title "제목"`을 갱신한 뒤
`office finish "사용자에게 보낼 최종 보고 (뭘 했는지, 남은 이슈)"`.
보고는 첫 줄 40자 이내 요약, 빈 줄, 짧은 '- ' 목록으로 쓰고 긴 문장은 나눠라."""



ERROR_KINDS = {"auth", "capacity", "network", "crash", "timeout", "unfinished", "unknown"}


def classify_agent_error(reason, rc=0):
    text = str(reason or "").lower()
    if text.startswith("남은 작업 확인 필요"):
        return "unfinished"
    if re.search(r"invalid api key|please (?:run|use).*?/login|not logged in|not authenticated|oauth.*(?:expired|invalid)|(?:refresh|access) token.*(?:expired|invalid|revoked|failed)|token.*refresh.*(?:failed|expired)|\b401\b|unauthorized|authentication(?:_| )(?:error|failed|failure)|refresh token", text):
        return "auth"
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


def auth_help(name):
    return ("로그인 확인 필요 — 로그인이 만료되었거나 인증에 실패했습니다. 터미널에서 claude를 실행해 /login 후 다시 시작을 누르세요." if name == "claude" else
            "로그인 확인 필요 — 로그인이 만료되었거나 인증에 실패했습니다. 터미널에서 codex login 후 다시 시작을 누르세요.")


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
            atomic_json(self.checkpoint, {"read_upto": cursor, "paused": self.paused, "error": self.error, "error_kind": getattr(self, "error_kind", None), "interrupted_turn": interrupted, "fail_count": getattr(self, "fail_count", 0), "failure_key": getattr(self, "failure_key", None),
                         "auto_retries": getattr(self, "auto_retries", 0), "retry_job": getattr(self, "retry_job", None),
                         "retry_counts": dict(getattr(self, "retry_counts", {})),
                         "retry_at": getattr(self, "retry_at", 0)}, checkpoint_valid)

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
            cached = usage.get("cached_input_tokens", 0)
            tokens = max(0, usage.get("input_tokens", 0) - cached) + usage.get("output_tokens", 0)
            counted, cache_counted = self.turn_counted if self.usage_tracking else (0, 0)
            self.add_tokens(max(0, tokens - counted), cached=max(0, cached - cache_counted))
            if self.usage_tracking:
                # 파일 쓰기가 stdout 완료보다 늦어도 다음 차례에 지난 사용량을 다시 더하지 않음.
                self.rollout_expected_thread = getattr(self, "thread", None)
                self.rollout_expected = {k: self.rollout_baseline.get(k, 0) + usage.get(k, 0)
                                         for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
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
            cmd += ["--output-format", "stream-json", "--verbose"]
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
        if model_override:
            if self.name == "codex":
                cmd[-1:-1] = ["--model", model_override]
            else:
                cmd += ["--model", model_override]
        final = ""
        with tempfile.TemporaryFile() as lg:
            # 차례 준비 중 취소·중단됐으면 새 프로세스를 만들지 않는다.
            if self.stop.is_set() or self.interrupted_job is not None or self.immediate_pause:
                self.usage_tracking = False
                return ""
            proc = subprocess.Popen(cmd, cwd=wdir(self.sid), env=env, stdout=subprocess.PIPE, stderr=lg,
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
                proc.wait(timeout=2)
            finally:
                selector.close()
                if proc.poll() is None:
                    terminate_process(proc)
                self.rc = proc.returncode
                lg.seek(0, os.SEEK_END)
                lg.seek(max(0, lg.tell()-65536))
                diagnostic = safe_agent_diagnostic(lg.read().decode(errors="replace"))
                if diagnostic:
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


    def parse_claude(self, ev):
        if ev.get("type") == "system" and ev.get("subtype") == "init" and isinstance(ev.get("model"), str):
            self.primary_model = ev["model"]
        if ev.get("type") == "system" and ev.get("subtype") == "task_started":
            # 서브 에이전트·백그라운드 작업 시작. 백그라운드로 띄우면 도구 결과가 바로 오므로 이 이벤트가 실제 시작이다
            key = ev.get("tool_use_id") or ev.get("task_id")
            if key:
                if key not in self.subs:
                    desc = str(ev.get("description") or "보조 작업")[:120]
                    self.subs[key] = {"desc": desc, "since": time.time()}
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
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "text" and c["text"].strip():
                    self.act("text", c["text"].strip())
                elif c.get("type") == "tool_use":
                    if c.get("id"):
                        self.open_tools[c["id"]] = time.time()
                    i = c.get("input", {})
                    if c.get("name") in ("Agent", "Task") and c.get("id"):
                        desc = str(i.get("description") or i.get("prompt") or "보조 작업")[:120]
                        self.subs[c["id"]] = {"desc": desc, "since": time.time()}
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
                if isinstance(c, dict) and c.get("type") == "tool_result" and not self.async_launch(c):
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
               and m["from"] not in (self.name, "system") and m["to"] in (self.name, "all")
               and not (m["from"] != "user" and m["text"].strip() in ("대기", "확인", "확인했음", "알겠습니다", "고마워"))]
        body = "\n".join(f"- {m['from']}: {m['text']}" for m in new)
        jobs = [j for j in s["jobs"] if j["status"] != "done"]
        mine_lead = [j for j in jobs if j.get("acting_lead", j["lead"]) == self.name]
        planning = {j["id"] for j in jobs if j["status"] == "planning"}
        tasks = [t for t in s["tasks"] if t["status"] != "done"]
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
        assigned = mine or free
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
            return f"새로 온 메시지:\n{body}", job, ("reply" if not mine and not free else "work"), new
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

    def pause_with_error(self, reason, job):
        self.error_kind = classify_agent_error(reason, self.rc)
        self.resume_turn = self.doing if isinstance(self.doing, dict) else getattr(self, "resume_turn", None)
        self.error = auth_help(self.name) if self.error_kind == "auth" else safe_agent_diagnostic(reason)
        reason = self.error
        key = hashlib.sha256((self.error_kind + ":" + reason).encode()).hexdigest()
        self.fail_count = self.fail_count + 1 if self.failure_key == key else 1
        self.failure_key = key
        self.paused = True
        self.idle("paused")
        self.save_checkpoint()
        with state(True, self.sid) as s:
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
                self.paused, self.error = True, (str(error) if isinstance(error, UserError) else
                    "작업 처리 오류 — 작업 처리 중 오류가 발생했습니다. 디스크 용량과 폴더 권한을 확인한 뒤 재개하세요.")
                self.error_kind = classify_agent_error(self.error, self.rc)
                key = hashlib.sha256((self.error_kind + ":" + self.error).encode()).hexdigest()
                self.fail_count = self.fail_count + 1 if self.failure_key == key else 1
                self.failure_key = key
                self.idle("paused")
                server_log("worker.loop", error)
                try:
                    self.save_checkpoint()
                    with state(True, sid=self.sid) as s:
                        post(s, "system", "user", self.error, "event", job=(self.resume_turn or {}).get("job"))
                except (OSError, StorageError) as storage_error:
                    server_log("worker.error.persist", storage_error)
                self.stop.wait(POLL)

    def _loop(self, office):
        sid = self.sid
        while not (self.stop.is_set() or office.stop.is_set()):
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
                snap, total = board_text(s), len(s["msgs"])
                missed = missed_text(s["msgs"][self.read_upto:], self.name)
                title_history = request_history(s["jobs"])
            if not nt:
                self.idle(); self.stop.wait(POLL)
                continue
            tail, job, desc, new = nt
            if self.resume_turn and not self.restart_reported:
                with state(True, sid) as restored:
                    event(restored, "중단된 차례를 다시 실행합니다. 이미 변경한 파일과 완료 작업을 확인하고 이어갑니다.", job=job)
                self.restart_reported = True
            only_chat = desc == "reply" and not any(m["from"] == "user" for m in new)
            self.chatter = self.chatter + 1 if only_chat else 0
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
            self.rc, self.error, self.error_kind = 0, None, None
            self.model_notice = None
            self.primary_model = configured_model(self.name)
            self.status, self.since, self.doing = "working", time.time(), {"kind": desc, "job": job}
            self.save_checkpoint()   # 첫 차례 도중 서버가 꺼져도 재시작 후 이 메시지부터 다시 읽게 시작 위치를 남김
            self.act("start", f"{desc}|{job or ''}")
            prompt = PROTOCOL.format(me=self.name, other=self.other) + f"\n현재 상황:\n{snap}\n\n"
            if missed:
                prompt += f"네 지난 차례 이후 네가 직접 받지 못한 대화 (사용자·동료끼리 나눈 내용, 맥락 파악용):\n{missed}\n\n"
            if self.resume_turn and self.resume_turn.get("job") == job:
                prompt += "이전 차례가 중단되어 이어서 시작한다. 이미 수정한 파일과 완료 작업을 먼저 확인하고, 반쯤 진행한 작업은 검증해서 이어가라. 같은 변경을 반복하거나 덮어쓰지 마라.\n"
                prompt += "이전 실행의 파일 예약은 풀렸다. 파일을 고치기 전에 office reserve 를 다시 하고, office write 결과를 확인한 뒤에만 완료 처리해라.\n"
            prompt += "메시지에 이미지·파일 경로가 있으면 해당 파일을 직접 열어서 확인해라. Claude는 Read, Codex는 이미지 보기 도구를 사용한다.\n"
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
                    metrics["seconds"] += round(max(0, time.monotonic()-turn_started), 3)
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
                    try:
                        why = (sdir(sid) / f"{self.name}.log").read_text(errors="ignore").strip().splitlines()[-1][:200]
                    except (OSError, IndexError):
                        why = f"종료 코드 {self.rc}"
                self.read_upto = previous_read
                retry_key = "direct" if job is None else str(job)
                retries = self.retry_counts.get(retry_key, 0)
                if self.transient_failure(why) and retries < len(AUTO_RETRY_DELAYS) and not self.stop.is_set():
                    delay = AUTO_RETRY_DELAYS[retries]
                    self.auto_retries = retries + 1
                    self.retry_counts[retry_key] = retries + 1
                    self.retry_job = job
                    self.retry_at = time.time() + delay
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
                if closing:
                    summary = report or out
                    closing.update(status="done", finished_by=self.name, finished_at=time.time(), summary=summary, brief=report_brief(summary))
                    event(s, f"요청 {job} 완료 (검수 보고로 마감)", self.name, job=job)
                    if report:
                        post(s, self.name, "user", report, "report", job=job)
            self.resume_turn = None
            self.fail_count, self.failure_key = 0, None
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
    # 기본 시스템 신뢰를 유지한 채 명시적으로 지정한 인증서를 추가한다.
    # NODE_EXTRA_CA_CERTS: claude CLI가 쓰는 변수라 이것만 지정된 환경도 많음
    candidates = [Path(value) for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")
                  if (value := os.environ.get(key))]
    for path in candidates:
        if path.is_file():
            context.load_verify_locations(cafile=str(path))
            break
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
UPDATE = {"latest": None, "notes": "", "url": None, "state": "idle", "message": None}


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
    current = office_version()
    info["current"] = current
    info["available"] = bool(info["latest"]) and version_key(info["latest"]) > version_key(current)
    info["installable"] = install_paths() is not None
    if info["available"] and not info["installable"] and info["state"] == "idle":
        info["message"] = "설치기로 설치한 Office에서만 바로 업데이트할 수 있어요."
    return info


def check_update(stop):
    """깃허브 릴리스 목록에서 가장 새 버전을 찾는다. 여러 버전을 건너뛰었으면 그사이 변경 내용을 버전별로 모아 보여 준다.
    설치는 항상 최신 묶음 하나로 하므로 중간 버전을 차례로 깔 필요가 없다"""
    url = f"https://api.github.com/repos/{UPDATE_REPO}/releases?per_page=30"
    while not stop.is_set():
        try:
            req = urlrequest.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "office-update"})
            with urlrequest.urlopen(req, timeout=20, context=provider_ssl_context()) as r:
                rels = [x for x in json.loads(r.read()) if isinstance(x, dict) and not x.get("draft") and not x.get("prerelease")]
            for x in rels:
                x["version"] = str(x.get("tag_name") or "").lstrip("v")
            rels = sorted((x for x in rels if x["version"]), key=lambda x: version_key(x["version"]), reverse=True)
            current = version_key(office_version())
            newer = [x for x in rels if version_key(x["version"]) > current]
            notes = "\n\n".join(f"### {x['version']}\n\n{str(x.get('body') or '').strip()}" for x in newer) if len(newer) > 1 \
                else str((newer or rels or [{}])[0].get("body") or "")
            with UPDATE_LOCK:
                UPDATE.update(latest=rels[0]["version"] if rels else None, notes=notes[:6000],
                              url=rels[0].get("html_url") if rels else None)
        except (OSError, ValueError, TypeError, KeyError) as error:
            server_log("update.check", error)
        stop.wait(3600)   # 한 시간마다 다시 확인: 알림을 띄워 둔 사이 더 새 버전이 나와도 반영


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
    with UPDATE_LOCK:
        UPDATE.update(state="restarting", message=None)
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
            tasks_sig = tuple((t["id"], t["status"], t["owner"]) for t in s["tasks"])
            activity = (len(s["msgs"]), tasks_sig)
            tasks = [t for t in s["tasks"] if t["status"] != "done"]
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
            left_text = ", ".join(f"#{t['id']} {disp(t['owner']) if t.get('owner') else '담당 없음'} {'진행 중' if t['status'] == 'doing' else '대기'}" for t in left) or "없음(검수 차례 대기)"
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

    def busy(self, sid):
        t = self.teams.get(sid)
        return bool(t) and any(w.status == "working" for w in t["w"].values())

    def git_info(self, sid):
        if not WT or not wtree(sid).exists():
            return None
        hit = self.gitcache.get(sid)
        if hit and time.time() - hit[0] < 3:
            return hit[1]
        br = f"office/{sid}"
        changed = len([l for l in git("status", "--porcelain", cwd=wtree(sid)).stdout.splitlines() if l.strip()])
        ahead = git("rev-list", "--count", f"HEAD..{br}").stdout.strip()
        info = {"branch": br, "path": str(wdir(sid).relative_to(ROOT)), "changed": changed,
                "ahead": int(ahead or 0), "main": git("branch", "--show-current").stdout.strip() or "HEAD"}
        self.gitcache[sid] = (time.time(), info)
        return info

    def snapshot(self, sid=None, since_rev=None):
        cur = sid or current_sid()
        with meta() as m:
            if not valid_sid(cur) or not any(x["id"] == cur and not x["deleted"] for x in m["sessions"]):
                raise UserError("대화를 찾을 수 없음 — 대화가 없습니다. 대화 목록에서 다시 선택하세요.")
        self.ensure_team(cur)
        with state(sid=cur) as s:
            data = dict(s)
        with meta() as m:
            sessions = [dict(x) for x in m["sessions"]]
        notice = {}
        for x in sessions:              # 목록에 띄울 진행 상황 요약
            try:
                st = read_json_file(sdir(x["id"]) / "state.json", {}, state_valid)
                x["open"] = sum(j["status"] != "done" for j in st.get("jobs", []))
                notice[x["id"]] = last_notice(st.get("msgs", []))
            except (OSError, ValueError, StorageError) as error:
                server_log("snapshot.session", error)
                x["open"] = 0
                notice[x["id"]] = 0
        # 알림 점: 사용자에게 온 보고·메시지·요청 완료가 있는데 아직 안 본 대화. 보고 있는 대화는 본 것으로 처리
        if any("seen" not in x or (x["id"] == cur and x["seen"] < notice[x["id"]]) for x in sessions):
            with meta(True) as m:
                for y in m["sessions"]:
                    if y["id"] in notice and ("seen" not in y or y["id"] == cur):
                        y["seen"] = max(y.get("seen", 0), notice[y["id"]])
                seen = {y["id"]: y["seen"] for y in m["sessions"] if "seen" in y}
            for x in sessions:
                x["seen"] = seen.get(x["id"], notice[x["id"]])
        for x in sessions:
            # 진행 중인 요청이 남아 있으면 중간 보고라도 반짝이지 않는다. 다 끝난 뒤(또는 요청 없이 온 결과)에만 알린다
            x["unread"] = x["id"] != cur and x["open"] == 0 and notice[x["id"]] > x.get("seen", 0)
            x["busy"] = self.busy(x["id"])
            x["stopped"] = any(w.paused or w.error for w in self.teams.get(x["id"], {}).get("w", {}).values())
        data.update(root=str(ROOT), session=cur, sessions=sessions, parallel=WT, git=self.git_info(cur),
                    warnings=list(STORAGE_WARNINGS.values())[-10:], update=update_info())
        team = self.teams[cur]["w"]
        for w in team.values():
            if w.status == "working":
                w.update_codex_usage()
        with reservation_store() as reservations:
            data["file_reservations"] = [{k: value[k] for k in ("path","owner","session","job","since")} for value in reservations.values() if Path(value["path"]).is_relative_to(ROOT)]
        data["agents"] = {n: {"status": w.status, "since": w.since, "doing": w.doing, "paused": w.paused, "error": w.error, "error_kind": getattr(w, "error_kind", None), "fail_count": getattr(w, "fail_count", 0), "notice": w.notice or getattr(w, "model_notice", None),
                              "tokens": w.tokens, "cached_tokens": w.cached_tokens, "legacy_tokens": w.legacy_tokens,
                              "usage_since": w.usage_since, "limits": limits_for(n), "subs": list(getattr(w, "subs", {}).values()) if w.status == "working" else [],
                              "activity": list(w.activity)[-25:]} for n, w in team.items()}
        # since/since(epoch)는 시작 시각이며 매초 경과 숫자는 화면에서 계산한다.
        signature = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).digest()
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
                 "merge", "job", "msg", "pause", "pause_now", "cancel_job", "quit", "update"}
        if act not in known:
            raise UserError("동작 처리 불가 — 알 수 없는 동작입니다. 화면을 새로고침하고 다시 시도하세요.")
        if act in ("switch", "rename", "pin", "delete", "restore", "purge"):
            target = p.get("id")
            if not valid_sid(target):
                raise UserError("대화 선택 확인 — 올바른 대화를 다시 선택하세요.")
            with meta() as m:
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
        if act in ("job", "msg", "pause", "pause_now", "cancel_job", "merge"):
            sid = (p.get("id") or p.get("session") or current_sid()) if act == "merge" else (p.get("session") or current_sid())
            if not valid_sid(sid):
                raise UserError("대화 선택 확인 — 올바른 대화를 다시 선택하세요.")
            with meta() as m:
                if not any(x["id"] == sid and not x["deleted"] for x in m["sessions"]):
                    raise UserError("열린 대화 확인 필요 — 대화가 삭제되었거나 더 이상 열려 있지 않습니다. 대화 목록에서 다시 선택하세요.")
        if act in ("job", "msg"):
            if not isinstance(p.get("text"), str) or not p["text"].strip():
                raise UserError("보낼 내용 필요 — 보낼 내용을 입력하세요.")
            if len(p["text"]) > MAX_TEXT:
                raise UserError(f"입력 길이 초과 — 한 번에 {MAX_TEXT:,}자까지 보낼 수 있습니다. 내용을 나눠 보내세요.")
            if act == "job" and p.get("lead", "claude") not in AGENTS:
                raise UserError("리더 선택 확인 — 리더는 Claude 또는 Codex를 선택하세요.")
            if act == "msg" and p.get("to", "all") not in (*AGENTS, "all"):
                raise UserError("받는 에이전트 확인 — 메시지를 받을 에이전트를 다시 선택하세요.")
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
            with meta(True) as m:
                if any(x["id"] == p["id"] and not x["deleted"] for x in m["sessions"]):
                    m["current"] = p["id"]
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
        elif act == "merge":
            msg = self.merge(sid)
        elif act == "job":
            touch(sid, p["text"], request=True)
            with state(True, sid=sid) as s:
                jid = len(s["jobs"]) + 1
                s["jobs"].append({"id": jid, "text": p["text"], "lead": p.get("lead", "claude"),
                                  "status": "planning", "summary": "", "started_at": time.time()})
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
                    if previous is not None:
                        snapshot = office.snapshot(selected, since_rev=previous)
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


def serve():
    load_limits()   # 에이전트 시작 전에 지난 관측의 유효한 창을 복원
    office = Office()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(office))
    srv.daemon_threads = True
    office.http_server = srv
    port = srv.server_address[1]
    (D / "server.json").write_text(json.dumps({"pid": os.getpid(), "port": port}))
    detect_worktree_mode()
    office.sync_teams()
    threading.Thread(target=codex_limits, daemon=True).start()    # 과거 관측을 보조로 표시
    threading.Thread(target=check_update, args=(office.stop,), daemon=True).start()
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
    print(paint("이 콘솔은 켜 두세요 · 종료 Ctrl+C", 2))
    if not opened:
        print(paint("창을 자동으로 열지 못했어요. 위 주소를 브라우저에서 열어 보세요.", 33))
    print()
    start, i, last, drawn = time.time(), 0, None, 0
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
            office.stop.wait(0.2)
    except KeyboardInterrupt:
        office.shutdown()
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


def open_window(port):
    url = f"http://localhost:{port}/"
    for exe in (CHROME, EDGE):
        if Path(exe).exists():
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1500,920"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd="/mnt/c")
                return True
            except OSError:
                continue
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
    sub = {"stop", "board", "log", "add", "claim", "done", "handoff", "say", "finish", "title", "lesson", "shot", "reserve", "release", "write", "-h", "--help"}
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
    sp.add_parser("board").set_defaults(f=cmd_board)
    x = sp.add_parser("log"); x.add_argument("dir", nargs="?"); x.add_argument("-n", type=int, default=20); x.add_argument("-f", "--follow", action="store_true")
    x.set_defaults(f=cmd_log)
    x = sp.add_parser("reserve"); x.add_argument("path"); x.set_defaults(f=cmd_reserve)
    x = sp.add_parser("release"); x.add_argument("path"); x.set_defaults(f=cmd_release)
    x = sp.add_parser("write"); x.add_argument("path"); x.add_argument("--from", dest="source", required=True); x.set_defaults(f=cmd_write)
    x = sp.add_parser("add"); x.add_argument("text"); x.add_argument("--to", choices=AGENTS); x.set_defaults(f=cmd_add)
    x = sp.add_parser("claim"); x.add_argument("id", type=int); x.set_defaults(f=cmd_claim)
    x = sp.add_parser("done"); x.add_argument("id", type=int); x.add_argument("text", nargs="?", default=""); x.set_defaults(f=cmd_done)
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
