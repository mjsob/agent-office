"""자동 점수 줄이 배분 교훈을 밀어내지 않고, 화면 번호(요청-순번)로 말한 사용자 정정도 원담당에게 붙는지 확인해요.
실행: python3 -I tests/test_score_lesson_screen.py [payload/office.py 경로]
"""
import importlib.util, os, sys, tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="office-score-lesson-") as tmp:
    for key in list(os.environ):
        if key.startswith("OFFICE_"):
            del os.environ[key]
    os.environ.update(OFFICE_DIR=tmp, OFFICE_SESSION="test", OFFICE_AS="claude", OFFICE_JOB="7")
    src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
    spec = importlib.util.spec_from_file_location("office_score_lesson_test", src)
    o = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(o)
    record = {"jobs": [{"id": 7, "status": "working", "lead": "claude", "text": "x"}],
              "tasks": [{"id": 30, "job": 7, "status": "done", "owner": "codex", "title": "구현", "kind": "구현"},
                        {"id": 31, "job": 7, "status": "done", "owner": "codex", "title": "문서", "kind": "문서"}], "msgs": []}

    # 표본이 없으면 교훈에 안 씀
    o.save_lesson("사람이 쓴 교훈", 7)
    o.save_score_lesson(record, 7)
    assert o.SCORE_LESSON_HEAD not in o.lessons_text(30)

    # 표본이 있으면 쓰되, 점수 줄은 가장 최근 한 줄만
    record["tasks"][0]["metrics"] = {"verdict": "pass", "owner": "codex", "reviewed_by": "claude"}
    o.save_score_lesson(record, 7)
    record["tasks"][1]["metrics"] = {"verdict": "fixed:2", "owner": "codex", "reviewed_by": "claude"}
    o.save_score_lesson(record, 7)
    lines = o.lessons_text(30).splitlines()
    assert sum(o.SCORE_LESSON_HEAD in x for x in lines) == 1 and "문서" in lines[-1]
    assert any("사람이 쓴 교훈" in x for x in lines)

    # 화면 번호 7-2 → 작업 #31, 날짜·시각은 무시
    assert o.screen_task_ids(record, "7-2 다시 고쳐") == {31}
    assert o.screen_task_ids(record, "2026-10-20 09:10-11 고쳐") == set()
    assert o.screen_task_ids(record, "7-9 고쳐") == set()
    with o.state(True, "test") as s:
        s.update(record)
        o.post(s, "user", "all", "7-2 결과 이상해, 다시 고쳐", "msg", job=7)
    with o.state(sid="test") as s:
        assert s["tasks"][1]["metrics"]["corrections"] == 1 and s["msgs"][-1]["corrected_tasks"] == [31]
        assert not s["tasks"][0]["metrics"].get("corrections")
    print("score lesson screen ok")
