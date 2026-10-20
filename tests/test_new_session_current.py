"""대화를 누른 직후 새 대화·복원을 눌러도 화면이 새 대화로 넘어가는지 확인해요.
실행: python3 -I tests/test_new_session_current.py [payload/office.py 경로]
서버를 실행하지 않고 실제 API 처리 함수와 임시 상태를 사용해요. 에이전트는 띄우지 않아요.
"""
import importlib.util, os, sys, tempfile, time
from pathlib import Path


def settle(o, want, seconds=3):
    """뒤에서 저장하는 대화 전환(meta_later)이 끝날 때까지 기다린 뒤 meta.json 의 현재 대화를 돌려줘요."""
    end = time.time() + seconds
    while time.time() < end:
        if o.META_QUEUE.empty():
            time.sleep(0.05)
            with o.meta() as m:
                if m["current"] == want:
                    return want
        time.sleep(0.02)
    with o.meta() as m:
        return m["current"]


with tempfile.TemporaryDirectory(prefix="office-new-session-") as tmp:
    for key in list(os.environ):
        if key.startswith("OFFICE_"):
            del os.environ[key]
    os.environ.update(OFFICE_DIR=tmp, OFFICE_AS="claude", HOME=tmp)   # OFFICE_SESSION 이 있으면 현재 대화가 고정돼요
    src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
    spec = importlib.util.spec_from_file_location("office_new_session_test", src)
    o = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(o)
    assert str(o.D).startswith(tmp)
    o.server_log = lambda *a, **k: None
    o.Office.ensure_team = lambda self, sid: None
    o.Office.sync_teams = lambda self: None
    office = o.Office()
    o.D.mkdir(parents=True, exist_ok=True)
    with o.meta(True) as m:
        a = o.new_session(m, "A")
        b = o.new_session(m, "B")

    # 1. A 를 누르고 30초 안에 새 대화: 새 대화가 현재 대화여야 하고, 뒤에서 저장하는 A 전환이 되돌리지 않아야 해요
    office.handle({"action": "switch", "id": a, "session": b})
    assert o.current_sid() == a
    office.handle({"action": "new_session", "session": a})
    with o.meta() as m:
        created = m["sessions"][-1]["id"]
    assert created not in (a, b)
    assert o.current_sid() == created, (o.current_sid(), created)
    assert settle(o, created) == created
    o.CURRENT_HINT.update(at=0.0)   # 30초가 지난 뒤에도 그대로
    assert o.current_sid() == created

    # 2. 빠르게 A → B 를 누르면 마지막에 누른 B 가 남아야 해요
    office.handle({"action": "switch", "id": a, "session": created})
    office.handle({"action": "switch", "id": b, "session": a})
    assert settle(o, b) == b
    o.CURRENT_HINT.update(at=0.0)
    assert o.current_sid() == b

    # 3. A 를 누르고 바로 휴지통의 대화를 복원: 복원한 대화가 현재 대화예요
    office.handle({"action": "delete", "id": created, "session": b})
    office.handle({"action": "switch", "id": a, "session": b})
    office.handle({"action": "restore", "id": created, "session": a})
    assert o.current_sid() == created
    assert settle(o, created) == created

    # 4. 새 대화 준비가 실패하면 원래 누른 대화를 그대로 가리켜요
    office.handle({"action": "switch", "id": a, "session": created})
    def broken(self, sid):
        raise RuntimeError("준비 실패")
    o.Office.ensure_team = broken
    try:
        office.handle({"action": "new_session", "session": a})
        raise AssertionError("실패해야 해요")
    except o.UserError:
        pass
    assert o.current_sid() == a
    assert settle(o, a) == a
    with o.meta() as m:
        assert len(m["sessions"]) == 3, [x["title"] for x in m["sessions"]]

print("new session current ok")
