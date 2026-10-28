"""빈 대화는 삭제하면 바로 지우고, 내용 있는 대화는 휴지통으로 옮기는지 확인해요.
실행: python3 -I tests/test_empty_session_delete.py [payload/office.py 경로]
서버를 실행하지 않고 실제 API 처리 함수와 임시 상태를 사용해요.
"""
import importlib.util, os, sys, tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="office-empty-delete-") as tmp:
    for key in list(os.environ):
        if key.startswith("OFFICE_"):
            del os.environ[key]
    os.environ.update(OFFICE_DIR=tmp, OFFICE_AS="codex", OFFICE_SESSION="keep")
    src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
    spec = importlib.util.spec_from_file_location("office_empty_delete_test", src)
    o = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(o)
    assert o.sdir("keep").is_relative_to(tmp)
    o.D.mkdir(parents=True, exist_ok=True)
    o.remove_worktree = lambda sid: None
    system = [{"id": 1, "from": "system", "to": "all", "kind": "event", "text": "접수", "ts": "00:00:00", "at": 1}]
    user = [{"id": 1, "from": "user", "to": "all", "kind": "msg", "text": "안녕", "ts": "00:00:00", "at": 1}]
    cases = {   # id: (meta 추가값, 대화 기록, 추가 파일, 바로 지워져야 하는지)
        "blank": ({}, {"msgs": [], "jobs": [], "tasks": []}, None, True),
        "sysonly": ({}, {"msgs": system, "jobs": [], "tasks": []}, None, True),
        "talked": ({}, {"msgs": user, "jobs": [], "tasks": []}, None, False),
        "named": ({"renamed": True}, {"msgs": [], "jobs": [], "tasks": []}, None, False),
        "pinned": ({"pinned": True}, {"msgs": [], "jobs": [], "tasks": []}, None, False),
        "agent": ({}, {"msgs": [], "jobs": [], "tasks": []}, "claude.sid", False),
        "bgrun": ({}, {"msgs": [], "jobs": [], "tasks": []}, "bg/1.log", False),
        "broken": ({}, None, None, False),
    }
    sessions = [{"id": "keep", "title": "keep", "deleted": False}]
    for sid, (extra, record, extra_file, _) in cases.items():
        sessions.append({"id": sid, "title": "새 대화", "deleted": False, **extra})
        o.sdir(sid).mkdir(parents=True, exist_ok=True)
        if record is not None:
            o.atomic_json(o.sdir(sid) / "state.json", {**record, "seq": 0}, o.state_valid)
        else:
            (o.sdir(sid) / "state.json").write_text("{깨진 기록")
        if extra_file:
            (o.sdir(sid) / extra_file).parent.mkdir(parents=True, exist_ok=True)
            (o.sdir(sid) / extra_file).write_text("x")
    o.atomic_json(o.D / "meta.json", {"current": "keep", "sessions": sessions}, o.meta_valid)
    office = object.__new__(o.Office)
    office.teams = {}
    office.stop_team = lambda sid: None
    office.sync_teams = lambda: None
    for sid, (_, _, _, purged) in cases.items():
        reply = office.handle({"action": "delete", "id": sid, "session": "keep"})
        with o.meta() as m:
            x = next((x for x in m["sessions"] if x["id"] == sid), None)
        if purged:
            assert reply == {"ok": "빈 대화를 지웠어요.", "purged": True}, (sid, reply)
            assert x is None and not o.sdir(sid).exists(), sid
        else:
            assert not (isinstance(reply, dict) and reply.get("purged")), (sid, reply)
            assert x is not None and x["deleted"] and o.sdir(sid).exists(), sid
print("빈 대화 삭제 시험 통과")
