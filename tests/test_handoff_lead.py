"""요청 리더는 멈춘 동료 작업을 넘겨받을 수 있고, 리더가 아닌 동료는 남의 작업을 넘기거나 끝내지 못하는지 확인해요.
실행: python3 -I tests/test_handoff_lead.py [payload/office.py 경로]
"""
import importlib.util, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace as NS

with tempfile.TemporaryDirectory(prefix="office-handoff-lead-") as tmp:
    for key in list(os.environ):
        if key.startswith("OFFICE_"):
            del os.environ[key]
    os.environ.update(OFFICE_DIR=tmp, OFFICE_SESSION="test", OFFICE_AS="claude", OFFICE_JOB="1")
    src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
    spec = importlib.util.spec_from_file_location("office_handoff_lead_test", src)
    o = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(o)
    with o.state(True, "test") as s:
        s["jobs"] = [{"id": 1, "status": "working", "lead": "claude", "text": "x"}]
        s["tasks"] = [{"id": 1, "job": 1, "status": "doing", "owner": "codex", "title": "codex 일"},
                      {"id": 2, "job": 1, "status": "doing", "owner": "claude", "title": "claude 일"}]
    o.cmd_handoff(NS(id=1, to="claude", text="멈춘 동료 작업 넘겨받음"))      # 리더 → 허용
    os.environ["OFFICE_AS"] = "codex"
    for call in (lambda: o.cmd_handoff(NS(id=2, to="codex", text="남의 작업")), lambda: o.cmd_done(NS(id=2, text="남의 작업"))):
        try:
            call()
        except o.UserError:
            pass
        else:
            raise AssertionError("리더가 아닌 동료가 남의 작업을 바꿨어요")
    with o.state(sid="test") as s:
        assert [t["owner"] for t in s["tasks"]] == ["claude", "claude"] and s["tasks"][1]["status"] == "doing"
    print("handoff lead ok")
