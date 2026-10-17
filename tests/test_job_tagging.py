"""요청 묶음 붙이기 시험: 차례 글 판정(turn_log_job), say --job, retag.
사용: python3 -I tests/test_job_tagging.py [payload/office.py 경로]
임시 폴더에서만 돌고 실제 Office 상태 파일은 건드리지 않는다."""
import argparse, importlib.util, json, os, sys, tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="office-job-tagging-")
for k in [k for k in os.environ if k.startswith("OFFICE_")]:
    del os.environ[k]
os.environ.update(OFFICE_DIR=TMP, OFFICE_AS="claude", OFFICE_SESSION="t1")

src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
sp = importlib.util.spec_from_file_location("office_under_test", src)
o = importlib.util.module_from_spec(sp)
sp.loader.exec_module(o)
assert str(o.sdir("t1")).startswith(TMP), o.sdir("t1")

# --- turn_log_job
M = lambda j, k="msg", f="claude", t="user": {"from": f, "to": t, "kind": k, "job": j}
f = o.turn_log_job
assert f([M(29)], "claude", 27, "x") == (29, "x")                 # 다른 요청 하나
assert f([M(29), M(29)], "claude", 27, "x") == (29, "x")
assert f([M(27)], "claude", 27, "x") == (27, "x")                 # 원래 요청
assert f([M(28), M(29), M(29)], "claude", 27, "x") == (None, "(요청 28·29 관련) x")   # 섞임: 한쪽으로 몰지 않음
assert f([M(29), M(27)], "claude", 27, "x") == (None, "(요청 27·29 관련) x")  # 원래 요청도 혼합이면 분리
assert f([M(None), M(29)], "claude", 27, "x") == (None, "(요청 29·공통 기록 관련) x")  # 공통 기록 혼합
assert f([], "claude", 27, "x") == (27, "x")                      # 사용자 발송 없음
assert f([M(29, f="codex")], "claude", 27, "x") == (27, "x")      # 동료 메시지만
assert f([M(29, t="codex")], "claude", 27, "x") == (27, "x")      # 사용자 대상 아님
assert f([M(29, k="note")], "claude", 27, "x") == (27, "x")       # 대상 종류 아님

assert f([M(None)], "claude", 27, "x") == (None, "x")
assert f([M(None), M(27), M(29)], "claude", 27, "x") == (None, "(요청 27·29·공통 기록 관련) x")
assert f([M(27), M(27)], "claude", 27, "x") == (27, "x")

# --- say --job / retag
d = o.sdir("t1")
d.mkdir(parents=True, exist_ok=True)
job = lambda i: {"id": i, "status": "working", "lead": "codex", "text": "t"}
json.dump({"jobs": [job(27), job(29)], "tasks": [], "seq": 10, "msgs": [
    {"id": 1, "from": "user", "to": "all", "kind": "request", "job": 29, "text": "q", "ts": "t", "at": 1},
    {"id": 2, "from": "claude", "to": "user", "kind": "progress", "job": 27, "text": "x", "ts": "t", "at": 1}]},
    open(d / "state.json", "w"))
os.environ["OFFICE_JOB"] = "27"
os.environ["OFFICE_REPLY_TO"] = "1"
o.cmd_say(argparse.Namespace(text="hello", to="user", info=False, re=None, job=29))
with o.state(sid="t1") as s:
    assert s["msgs"][-1]["job"] == 29 and "re" not in s["msgs"][-1], s["msgs"][-1]
for kwargs in ({"re": 1, "job": 29}, {"re": None, "job": 99}):
    try:
        o.cmd_say(argparse.Namespace(text="bad", to="user", info=False, **kwargs))
    except o.UserError:
        pass
    else:
        raise SystemExit(f"거부되지 않음: {kwargs}")
o.cmd_retag(argparse.Namespace(job=29, ids=[2]))
with o.state(sid="t1") as s:
    m = next(x for x in s["msgs"] if x["id"] == 2)
    assert (m["job"], m["job_was"], m["text"]) == (29, 27, "x"), m
try:
    o.cmd_retag(argparse.Namespace(job=29, ids=[1]))
except o.UserError:
    pass
else:
    raise SystemExit("사용자 메시지 이동이 거부되지 않음")
print("ok")
