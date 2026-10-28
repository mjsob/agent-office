"""디스크 줄이기 진행 정보(단계·퍼센트·경과·복구)가 형식을 확인해 화면으로 전달되는지 확인해요.
실행: python3 -I tests/test_storage_progress.py [payload/office.py 경로]
"""
import importlib.util, sys, time
from pathlib import Path
src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
spec = importlib.util.spec_from_file_location("office_progress_test", src)
o = importlib.util.module_from_spec(spec); spec.loader.exec_module(o)
now = time.time()
for status in ("awaiting_admin", "stopping", "cancelling"):
    assert status in o.STORAGE_COMPACT_ACTIVE
p = o.compact_progress({"status": "running", "phase": "compacting", "percent": 45, "started_at": now - 125, "can_cancel": True,
                        "message": "줄이는 중", "recovery_required": False})
assert p["phase"] == "compacting" and p["percent"] == 45 and p["can_cancel"] is True and p["recovery_required"] is False
assert 124 <= p["elapsed_seconds"] <= 128
bad = o.compact_progress({"status": "running", "phase": "Bad Phase!", "percent": 101, "before_gb": -1, "after_gb": float("nan"),
                          "can_cancel": "yes", "started_at": now + 9999, "message": 5})
assert bad == {}, bad
done = o.compact_progress({"status": "completed", "started_at": now - 50, "reclaimed_gb": 31.234, "wsl_ready": True})
assert "elapsed_seconds" not in done and done["reclaimed_gb"] == 31.2 and done["wsl_ready"] is True   # 끝난 뒤엔 경과 시간을 안 보여요
assert o.compact_progress({"status": "running", "elapsed_seconds": 70})["elapsed_seconds"] == 70
print("storage progress ok")
# 취소 요청: 진행 중이면서 can_cancel 일 때만 스크립트 --cancel 을 불러요
import os, tempfile
from unittest.mock import patch
with tempfile.TemporaryDirectory() as tmp:
    script = Path(tmp) / "run-compact.sh"; script.write_text("#")
    calls = []
    class R: returncode = 0; stdout = ""
    def fake(cmd, **kw): calls.append(cmd); return R()
    def view(status, cancel): return {"available": True, "status": status, "can_cancel": cancel}
    with patch.object(o, "STORAGE_COMPACT_SCRIPT", script), patch.object(o.subprocess, "run", fake):
        for status, cancel in (("idle", True), ("running", False), ("completed", True)):
            with patch.object(o, "storage_compact_view", lambda s=status, c=cancel: view(s, c)):
                try: o.cancel_storage_compact()
                except o.UserError: continue
                raise AssertionError((status, cancel))
        assert not calls
        with patch.object(o, "storage_compact_view", lambda: view("running", True)):
            o.cancel_storage_compact()
        assert calls and calls[0][-1] == "--cancel"
        R.returncode = 1
        with patch.object(o, "storage_compact_view", lambda: view("running", True)):
            try: o.cancel_storage_compact()
            except o.UserError: pass
            else: raise AssertionError("실패 종료 코드를 무시했어요")
print("storage cancel ok")
