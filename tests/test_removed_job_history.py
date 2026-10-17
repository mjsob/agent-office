"""지운 요청이 이력과 최근 결과에서 빠지는지 확인해요."""
import copy
import importlib.util
import os
from pathlib import Path
import sys
import tempfile

with tempfile.TemporaryDirectory(prefix="office-removed-history-") as tmp:
    for key in list(os.environ):
        if key.startswith("OFFICE_"):
            del os.environ[key]
    os.environ.update(OFFICE_DIR=tmp, OFFICE_AS="codex", OFFICE_SESSION="test")
    src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
    spec = importlib.util.spec_from_file_location("office_removed_history_test", src)
    o = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(o)
    visible = [{"id": i, "text": f"visible-{i}", "status": "done", "summary": f"answer-{i}"} for i in range(1, 22)]
    hidden = [{"id": i, "text": f"hidden-{i}", "status": "done", "cancelled": True, "removed": True} for i in range(22, 42)]
    jobs = visible + hidden
    before = copy.deepcopy(jobs)
    history = o.request_history(jobs)
    results = o.recent_results(jobs)
    assert "hidden-" not in history and "hidden-" not in results
    assert "이전 요청 1~1: 1건" in history
    assert "visible-21" in history and "visible-2" in history
    assert "[요청 16]" in results and "[요청 21]" in results and "[요청 15]" not in results
    assert o.request_history(hidden) == o.recent_results(hidden) == ""
    assert o.request_history([]) == o.recent_results([]) == ""
    mixed = [{"id": 1, "text": "cancel-visible", "status": "done", "cancelled": True},
             {"id": 2, "text": "working-visible", "status": "working"}]
    assert "cancel-visible" in o.request_history(mixed)
    assert "취소됨" in o.recent_results(mixed)
    assert "working-visible" not in o.recent_results(mixed)
    assert jobs == before
    print("removed job history: passed")
