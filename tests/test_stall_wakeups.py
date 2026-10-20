"""멈춤 방지 깨우기 시험: wait --on, 동료 작업 완료 깨움, --info 부탁 깨움, 대상 없는 기다림 기한 늘리기,
다른 요청 차례에 실린 메시지 다시 싣기.
사용: python3 -I tests/test_stall_wakeups.py [payload/office.py 경로]
임시 폴더에서만 돌고 실제 Office 상태 파일은 건드리지 않는다."""
import argparse, importlib.util, json, os, sys, tempfile, time, types
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="office-stall-wakeups-")
for k in [k for k in os.environ if k.startswith("OFFICE_")]:
    del os.environ[k]
os.environ.update(OFFICE_DIR=TMP, OFFICE_AS="claude", OFFICE_SESSION="t1")

src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
sp = importlib.util.spec_from_file_location("office_under_test", src)
o = importlib.util.module_from_spec(sp)
sp.loader.exec_module(o)
assert str(o.sdir("t1")).startswith(TMP), o.sdir("t1")
o.server_log = lambda *a, **k: None

d = o.sdir("t1")
d.mkdir(parents=True, exist_ok=True)
NOW = time.time()


def task(i, owner, status="doing", job=1, **kw):
    return {"id": i, "job": job, "title": f"t{i}", "owner": owner, "status": status, "note": "", "created_at": NOW - 3600, **kw}


def msg(i, frm, to, text, kind="msg", job=1, at=None, **kw):
    return {"id": i, "ts": "t", "at": NOW - 3000 if at is None else at, "from": frm, "to": to, "text": text, "kind": kind, "job": job, **kw}


def reset(tasks, msgs=()):
    jobs = [{"id": 1, "status": "working", "lead": "codex", "text": "a"}, {"id": 2, "status": "planning", "lead": "codex", "text": "b"}]
    json.dump({"jobs": jobs, "tasks": tasks, "seq": len(tasks), "msgs": list(msgs)}, open(d / "state.json", "w"))


def load():
    with o.state(sid="t1") as s:
        return s


def check():
    o.Office.check_waits(types.SimpleNamespace(), "t1")


def wait(i, text="기다림", on=None, minutes=None, who="codex"):
    os.environ["OFFICE_AS"] = who
    try:
        args = argparse.Namespace(id=i, text=text, pid=None, log=None, minutes=minutes, on=on)
        if on is None and minutes is None:
            # 새 CLI는 대상을 요구해요. 기존 자료의 대기를 만들어 감시 호환성을 확인해요.
            o.mark_waiting(args, text)
        else:
            o.cmd_wait(args)
    finally:
        os.environ["OFFICE_AS"] = "claude"


def wakes(s, to="codex"):
    return [m for m in s["msgs"] if m["to"] == to and m["from"] == "system" and m.get("wake")]


def age(i, seconds):
    """작업 i 를 seconds 초 전에 끝난 것으로 고친다"""
    s = json.load(open(d / "state.json"))
    for t in s["tasks"]:
        if t["id"] == i:
            t["done_at"] = time.time() - seconds
    for t in s["tasks"]:   # 기다림은 그 완료보다 먼저 건 것으로 맞춘다
        if t["status"] == "waiting":
            t["waiting_at"] = min(t["waiting_at"], time.time() - seconds - 10)
    json.dump(s, open(d / "state.json", "w"))


# --- wait --on: 검증
reset([task(1, "codex"), task(2, "claude"), task(3, "claude", "done", done_at=NOW - 10)])
for bad in ([1], [99], [3]):
    try:
        wait(1, on=bad)
    except o.UserError:
        pass
    else:
        raise SystemExit(f"--on {bad} 가 거부되지 않음")
wait(1, on=[2])
t = next(x for x in load()["tasks"] if x["id"] == 1)
assert t["status"] == "waiting" and t["wait_on"] == [2], t
assert 55 * 60 < t["wait_deadline"] - t["waiting_at"] < 65 * 60, t   # --on 은 60분 안전망

# --- wait --on: 기다리던 작업이 끝나면 (끝낸 쪽 말이 없을 때) 한 번 깨움
o.cmd_done(argparse.Namespace(id=2, text="결과 저장"))
check()
assert not wakes(load()), "유예 시간 안에 깨우면 안 됨"
age(2, 30)
check()
s = load()
w = wakes(s)
assert len(w) == 1 and "[기다리던 작업 끝남] 작업 #1" in w[0]["text"] and "결과 저장" in w[0]["text"], w
assert not any(m["to"] == "user" and "기다리던" in m["text"] for m in s["msgs"]), "내부 깨움을 사용자 화면에 올리면 안 됨"
check()
assert len(wakes(load())) == 1, "두 번 깨우면 안 됨"

# --- 같은 요청의 동료 작업 완료: 끝낸 쪽이 이미 말을 보냈으면 조용히 표시만
reset([task(1, "codex"), task(2, "claude")])
wait(1, text="#2 결과 대기")
o.cmd_done(argparse.Namespace(id=2, text="후보 완료"))
o.cmd_say(argparse.Namespace(text="#2 끝, 반영해줘", to="codex", info=False, re=None, job=1))
age(2, 30)
check()
s = load()
assert not wakes(s) and next(x for x in s["tasks"] if x["id"] == 1).get("wait_peer_seen"), "이미 깨운 말이 있으면 서버가 또 깨우면 안 됨"

# --- 같은 요청의 동료 작업 완료: 말 없이 done 만 남기면 서버가 깨움 (22:16 사례)
reset([task(1, "codex"), task(4, "codex", job=2), task(2, "claude"), task(5, "claude", job=2)])
wait(1, text="#2 결과 대기")
wait(4, text="요청2 대기")
o.cmd_say(argparse.Namespace(text="#2 끝: 후보 저장, 실반영 부탁", to="codex", info=True, re=None, job=None))
s = load()
assert s["msgs"][-1]["kind"] == "msg", "--info 라도 부탁이면 깨우는 메시지여야 함"
s = json.load(open(d / "state.json")); s["msgs"].pop(); json.dump(s, open(d / "state.json", "w"))   # 말 없는 경우를 보려고 지움
o.cmd_say(argparse.Namespace(text="진행 중: 3곳 확인", to="codex", info=True, re=None, job=None))
assert load()["msgs"][-1]["kind"] == "note", "단순 진행 알림은 note 로 남아야 함"
o.cmd_done(argparse.Namespace(id=2, text="1421 후보 완료"))
age(2, 30)
check()
w = wakes(load())
assert len(w) == 1 and "[동료 작업 끝남] 작업 #1" in w[0]["text"] and "#2" in w[0]["text"], w
check()
assert len(wakes(load())) == 1, "같은 완료로 두 번 깨우면 안 됨"
o.cmd_done(argparse.Namespace(id=5, text="다른 요청 완료"))
age(5, 30)
check()
w = wakes(load())
assert len(w) == 2 and "작업 #4" in w[1]["text"], w   # 요청마다 따로

# --- 다시 건 뒤엔 그 전 완료로 깨우지 않음
reset([task(1, "codex"), task(2, "claude", "done", done_at=NOW - 100)])
wait(1, text="새 대기")
check()
assert not wakes(load()), "기다림을 건 뒤 끝난 작업만 봐야 함"

# --- 자기 프로세스를 기다리는 작업은 동료 완료로 깨우지 않음
reset([task(1, "codex", "waiting", waiting_at=NOW - 500, wait_pid=os.getpid(), wait_start=o.process_identity(os.getpid())["start"]),
       task(2, "claude", "done", done_at=NOW - 100)])
check()
assert not wakes(load()), "pid 감시 중엔 그 프로세스 결과로 깨워야 함"

# --- 대상 없는 기다림: 기한 알림 뒤 다시 걸면 기한이 늘고, 사용자 화면엔 처음만
reset([task(1, "codex")])
spans = []
for n in range(6):
    wait(1, text=f"대기 {n}")
    s = json.load(open(d / "state.json"))
    t = s["tasks"][0]
    spans.append(round((t["wait_deadline"] - t["waiting_at"]) / 60))
    t["wait_deadline"] = time.time() - 1
    t.pop("bare_waits", None)   # 시간 간격을 흉내: 반복 거절은 tests/test_bare_wait_guard.py 에서 따로 본다
    json.dump(s, open(d / "state.json", "w"))
    check()
assert spans == [15, 30, 60, 120, 240, 240], spans
s = load()
assert sum(m["to"] == "user" and "기한 지남" in m["text"] for m in s["msgs"]) == 1, "사용자 알림은 처음 한 번만"
assert len(wakes(s)) == 6, "담당은 매번 깨움"
wait(1, text="짧게", minutes=5)
t = load()["tasks"][0]
assert round((t["wait_deadline"] - t["waiting_at"]) / 60) == 5, "--minutes 는 그대로"

# --- --pid 감시가 끝났을 때: 종료 코드 파일이 없으면 단정하지 않음
reset([task(1, "codex", "waiting", waiting_at=NOW - 500, wait_pid=999999, wait_start="x"),
       task(2, "codex", "waiting", waiting_at=NOW - 500, wait_pid=999998, wait_start="x", wait_rc=str(d / "none.rc"))])
check()
texts = [m["text"] for m in wakes(load())]
assert any("작업 #1" in x and "종료 코드는 알 수 없어요" in x for x in texts), texts
assert any("작업 #2" in x and "강제 종료됐을 수 있어요" in x for x in texts), texts

# --- 다른 요청 차례(기획)에 실린 깨우는 메시지는 다음 차례에 한 번 더 실림 (21:35 사례)
reset([task(1, "codex", "waiting", waiting_at=NOW - 500)],
      [msg(1, "system", "codex", "[백그라운드 끝났어요(종료 코드 0)] 작업 #1", job=1, wake=True),
       msg(2, "claude", "codex", "#150 통과", job=1),
       msg(3, "user", "all", "비용 얼마야?", kind="request", job=2)])
W = types.SimpleNamespace(sid="t1", name="codex", other="claude", read_upto=0, carry_msgs=[], stall_prompts=[], nudge_state={}, retry_counts={}, retry_due=None, cont=0)
W.waiting_on_peer = lambda s, doing: o.Worker.waiting_on_peer(W, s, doing)
s = load()
s["jobs"][1]["status"] = "planning"
s["jobs"][1]["lead"] = "codex"
tail, job, desc, new = o.Worker.next_turn(W, s)
assert (job, desc) == (2, "plan") and [m["id"] for m in new] == [1, 2, 3], (job, desc, [m["id"] for m in new])
# 차례 끝: 기획·답장·검수 차례면 이 요청이 아닌 깨우는 메시지만 다시 싣는다(사용자 메시지 제외)
o.Worker.acknowledge_events(W, new, job)
W.read_upto, previous_carry = len(s["msgs"]), []
W.carry_msgs = [m["id"] for m in new if m["from"] != "user" and m.get("job") != job and m["id"] not in previous_carry]
s["jobs"][1]["status"] = "done"
tail, job, desc, new = o.Worker.next_turn(W, s)
assert [m["id"] for m in new] == [1, 2] and "다른 요청 차례에 같이 왔던 메시지" in tail and "#1" in tail, (tail, new)
# 다시 실은 메시지는 한 번만
previous_carry, W.carry_msgs = W.carry_msgs, []
W.carry_msgs = [m["id"] for m in new if m["from"] != "user" and m.get("job") != 99 and m["id"] not in previous_carry]
assert W.carry_msgs == [], W.carry_msgs
o.Worker.acknowledge_events(W, new, 1)
assert o.Worker.next_turn(W, s) is None
src_text = Path(src).read_text()
assert 'self.carry_msgs = [m["id"] for m in new if m["from"] != "user" and m.get("job") != job and m["id"] not in previous_carry]' in src_text, "차례 끝 다시 싣기 코드가 시험과 달라짐"
assert '"carry_msgs": carry' in src_text and 'saved.get("carry_msgs", [])' in src_text, "다시 실을 메시지가 재시작 때 저장·복원돼야 함"
assert o.checkpoint_valid({"read_upto": 3, "carry_msgs": [1, 2]}) and not o.checkpoint_valid({"read_upto": 3, "carry_msgs": ["x"]})

# --- 동료가 done 뒤 '확인' 같은 짧은 답만 보내면 차례가 안 생기니 서버가 깨워야 함
reset([task(1, "codex"), task(2, "claude")])
wait(1, text="#2 결과 대기")
o.cmd_done(argparse.Namespace(id=2, text="끝"))
o.cmd_say(argparse.Namespace(text="확인", to="codex", info=False, re=None, job=1))
age(2, 30)
check()
assert len(wakes(load())) == 1, "짧은 답은 깨운 말로 세면 안 됨"

# --- 동료가 모두에게(to=all) 보낸 말도 깨운 말로 센다 (next_turn 과 같은 기준)
reset([task(1, "codex"), task(2, "claude")])
wait(1, text="#2 결과 대기")
o.cmd_done(argparse.Namespace(id=2, text="끝"))
o.cmd_say(argparse.Namespace(text="#2 끝났어, 반영해줘", to="all", info=False, re=None, job=1))
age(2, 30)
check()
assert not wakes(load()), "to=all 말이 이미 깨웠으면 서버가 또 깨우면 안 됨"

# --- 점검 도중 --on 대상을 바꿔 다시 걸면 옛 대상 완료로 새 기다림을 표시하지 않음
reset([task(1, "codex"), task(2, "claude"), task(3, "claude")])
wait(1, on=[2])
o.cmd_done(argparse.Namespace(id=2, text="끝"))
age(2, 30)
real_post = o.Office.check_waits
orig_state = o.state
calls = {"n": 0}
from contextlib import contextmanager
@contextmanager
def racing_state(write=False, sid=None):
    if write and calls["n"] == 0:   # 읽기와 쓰기 사이에 다시 건다
        calls["n"] = 1
        wait(1, on=[3])
    with orig_state(write, sid) as s:
        yield s
o.state = racing_state
check()
o.state = orig_state
t = next(x for x in load()["tasks"] if x["id"] == 1)
assert t["wait_on"] == [3] and not t.get("wait_ready") and not wakes(load()), t
o.cmd_done(argparse.Namespace(id=3, text="끝3"))
age(3, 30)
check()
assert len(wakes(load())) == 1 and "#3" in wakes(load())[0]["text"], wakes(load())
print("ok")
