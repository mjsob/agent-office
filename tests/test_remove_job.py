"""취소한 요청만 대화에서 지울 수 있는지 확인해요.
실행: python3 -I tests/test_remove_job.py [payload/office.py 경로]
서버를 실행하지 않고 실제 API 처리 함수와 임시 상태를 사용해요.
"""
import copy, importlib.util, os, sys, tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="office-remove-job-") as tmp:
    for key in list(os.environ):
        if key.startswith("OFFICE_"):
            del os.environ[key]
    os.environ.update(OFFICE_DIR=tmp, OFFICE_AS="codex", OFFICE_SESSION="test")
    src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
    spec = importlib.util.spec_from_file_location("office_remove_job_test", src)
    o = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(o)
    assert o.sdir("test").is_relative_to(tmp)
    o.D.mkdir(parents=True, exist_ok=True)
    o.atomic_json(o.D / "meta.json", {"current": "test", "sessions": [
        {"id": "test", "title": "test", "deleted": False},
        {"id": "other", "title": "other", "deleted": False},
        {"id": "trash", "title": "trash", "deleted": True}]}, o.meta_valid)
    jobs = [{"id": i, "status": status, "cancelled": cancelled, "lead": "codex", "text": str(i), "summary": ""}
            for i, status, cancelled in [(1, "done", True), (2, "done", False), (3, "working", True),
                                          (4, "working", False), (5, "done", True)]]
    tasks = [{"id": i, "job": job, "title": str(i), "owner": "codex", "status": status,
              "note": "", "created_at": 1} for i, job, status in [(1, 1, "done"), (2, 2, "done"), (3, 5, "doing")]]
    msgs = [{"id": i, "from": "user", "to": "all", "kind": "request", "job": i, "text": str(i),
             "ts": "00:00:00", "at": 1} for i in [1, 2]]
    original = {"jobs": jobs, "tasks": tasks, "msgs": msgs, "seq": 3}
    for sid in ["test", "other"]:
        o.atomic_json(o.sdir(sid) / "state.json", copy.deepcopy(original), o.state_valid)
    office = object.__new__(o.Office)
    office.teams = {}
    office.take_login_code = lambda *a: False

    def load(sid="test"):
        return copy.deepcopy(o.state_view(sid))

    def remove(**values):
        return office.handle({"action": "remove_job", "session": "test", "id": 1, **values})

    # 잘못된 번호·정상 완료·처리 중·취소가 덜 끝난 요청을 거절해요.
    for values in [{"id": x} for x in [None, True, False, "1", 1.5, 0, -1, 99, 2, 3, 4, 5]] + [
            {"session": "missing"}, {"session": "trash"}, {"session": "../test"}]:
        before = load()
        try:
            remove(**values)
        except o.UserError:
            pass
        else:
            raise AssertionError(("invalid removal accepted", values))
        assert load() == before, values

    # 삭제 표지만 남겨 번호·다른 요청·메시지·작업과 백업을 유지해요.
    before = load()
    result = remove()
    after = load()
    assert result == "취소한 요청을 대화에서 지웠어요."
    assert after["jobs"][0]["removed"] is True and after["jobs"][0]["removed_at"] > 0
    assert after["jobs"][1:] == before["jobs"][1:]
    assert after["tasks"] == before["tasks"] and after["msgs"] == before["msgs"] and after["seq"] == before["seq"]
    assert load("other") == original
    backup = o.sdir("test") / "state.json.bak"
    assert backup.is_file()
    assert o.read_json_file(backup, {}, o.state_valid, required=True) == before
    # 같은 요청을 다시 지워도 번호나 삭제 시각을 바꾸지 않아요.
    assert remove() == result and load() == after
    # 다른 대화에서 같은 요청 번호를 지워도 선택한 대화에만 적용해요.
    assert remove(session="other") == result and load() == after
    assert load("other")["jobs"][0]["removed"]
    # 숨긴 요청 번호를 재사용하지 않고 다음 요청을 만들어요.
    office.handle({"action": "job", "session": "test", "text": "new request"})
    latest = load()
    assert len(latest["jobs"]) == 6 and latest["jobs"][-1]["id"] == 6
    assert latest["jobs"][0] == after["jobs"][0]
print("ok: cancelled-only removal, input/session guards, idempotence, preservation, backup and next request ID")
