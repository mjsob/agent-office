"""대상 없는 기다림 반복 거절 시험: 같은 작업을 기다릴 대상 없이 2시간 안에 4번째 거는 것을 막고,
--on 으로 걸면 통과하는지, 2시간이 지나면 다시 걸 수 있는지 본다.
사용: python3 -I tests/test_bare_wait_guard.py [payload/office.py 경로]
임시 폴더에서만 돌고 실제 Office 상태 파일은 건드리지 않는다."""
import argparse, importlib.util, json, os, sys, tempfile, time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="office-bare-wait-")
for k in [k for k in os.environ if k.startswith("OFFICE_")]:
    del os.environ[k]
os.environ.update(OFFICE_DIR=TMP, OFFICE_AS="claude", OFFICE_SESSION="t1")
src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
sp = importlib.util.spec_from_file_location("office_under_test", src)
o = importlib.util.module_from_spec(sp)
sp.loader.exec_module(o)
assert str(o.sdir("t1")).startswith(TMP)
o.server_log = lambda *a, **k: None
d = o.sdir("t1"); d.mkdir(parents=True, exist_ok=True)
NOW = time.time()
tasks = [{"id": i, "job": 1, "title": f"t{i}", "owner": "claude", "status": "doing", "note": "", "created_at": NOW - 9999} for i in (1, 2, 3)]
json.dump({"jobs": [{"id": 1, "status": "working", "lead": "claude", "text": "a"}], "tasks": tasks, "seq": 3, "msgs": []}, open(d / "state.json", "w"))


def wait(i, **kw):
    ns = argparse.Namespace(id=i, text="무언가 기다림", pid=None, log=None, on=None, minutes=None, **kw)
    o.cmd_wait(ns)


def refused(i, **kw):
    try:
        wait(i, **kw)
    except SystemExit as e:
        assert "기다림 거절" in str(e.code), e.code
        return True
    return False


for n in range(3):
    assert not refused(1), n   # 처음 3번은 통과
assert refused(1)              # 4번째는 거절
assert not refused(2)          # 다른 작업은 영향 없음
with o.state(sid="t1") as s:
    assert [x for x in s["tasks"] if x["id"] == 1][0]["status"] == "waiting"
# 대상을 걸면 통과하고 세기가 초기화된다
ns = argparse.Namespace(id=1, text="동료 작업 기다림", pid=None, log=None, on=[3], minutes=None)
o.cmd_wait(ns)
for n in range(3):
    assert not refused(1), n
assert refused(1)
# 2시간이 지나면 다시 걸 수 있다
with o.state(True, sid="t1") as s:
    t = [x for x in s["tasks"] if x["id"] == 1][0]
    t["bare_waits"] = [x - 3 * 3600 for x in t["bare_waits"]]
assert not refused(1)
print("ok: bare wait guard (limit 3 per 2h, anchored reset, other tasks unaffected)")
