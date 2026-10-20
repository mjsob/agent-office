"""이어지는 차례엔 현재 상황 중 바뀐 줄만 보내고, 끝난 요청의 오래된 작업 기록은 따로 옮기는지 확인해요.
실행: python3 -I tests/test_turn_context.py [payload/office.py 경로]
서버를 실행하지 않고 임시 상태 폴더만 사용해요.
"""
import importlib.util, json, os, sys, tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="office-turn-context-") as tmp:
    for key in list(os.environ):
        if key.startswith("OFFICE_"):
            del os.environ[key]
    os.environ.update(OFFICE_DIR=tmp, OFFICE_AS="claude", OFFICE_SESSION="test")
    src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
    spec = importlib.util.spec_from_file_location("office_turn_context_test", src)
    o = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(o)
    o.sdir("test").mkdir(parents=True, exist_ok=True)
    w = object.__new__(o.Worker)
    w.name, w.other, w.sid = "claude", "codex", "test"

    # ② 현재 상황: 첫 차례는 전부, 이어지는 차례는 바뀐 줄 + 요청 줄 + 내 안 끝난 작업만
    s = {"jobs": [{"id": 1, "status": "working", "lead": "claude", "text": "요청"}],
         "tasks": [{"id": 1, "job": 1, "status": "done", "owner": "codex", "title": "끝난 일"},
                   {"id": 2, "job": 1, "status": "doing", "owner": "claude", "title": "내 일"},
                   {"id": 3, "job": 1, "status": "todo", "owner": "codex", "title": "동료 일"}],
         "msgs": [{"ts": "09:00:00", "from": "codex", "to": "all", "text": "옛 말", "kind": "msg"}]}
    first = o.board_text(s)
    assert w.snapshot_delta(first, False) == first                   # 저장된 게 없으면 전부
    s["msgs"].append({"ts": "09:01:00", "from": "codex", "to": "claude", "text": "새 말", "kind": "msg"})
    s["tasks"][2]["status"] = "doing"
    delta = w.snapshot_delta(o.board_text(s), False)
    assert "새 말" in delta and "옛 말" not in delta                  # 새 대화만
    assert "#3" in delta and "#1 " not in delta                       # 바뀐 작업만, 그대로인 끝난 작업은 뺌
    assert "#2" in delta and "요청 1 [working]" in delta              # 내 작업·요청 줄은 늘 보냄
    assert "같은 줄" in delta and "office board" in delta
    assert w.snapshot_delta(o.board_text(s), True) == o.board_text(s)   # 전문 차례면 전부

    # 요청 이력: 같으면 None 이 아닌 이전 값, 전문 차례면 None
    assert w.turn_context("hist", "이력 A", False) is None
    assert w.turn_context("hist", "이력 A", False) == "이력 A"
    assert w.turn_context("hist", "이력 A", True) is None

    # ③④⑤ 지침 문구
    assert "/tmp/req<요청번호>/<작업번호>.md" in o.PROTOCOL
    assert "쪼개는 기준" in o.PLAN and "10분" in o.PLAN and "결과: 바뀐 파일·커밋 / 확인한 것 / 남은 일" in o.PLAN

    # ⑦ 끝난 요청의 오래된 작업 기록만 옮기고, 번호·개수는 그대로
    long = "가" * 1000
    with o.state(True, "test") as st:
        st["jobs"] = [{"id": 1, "status": "done", "lead": "claude", "text": "끝난 요청"},
                      {"id": 2, "status": "working", "lead": "claude", "text": "열린 요청"}]
        o.post(st, "claude", "log", long, "turn", job=1)               # 옮김
        o.post(st, "claude", "log", long, "turn", job=2)               # 열린 요청 → 그대로
        o.post(st, "claude", "log", "짧은 기록", "turn", job=1)          # 짧음 → 그대로
        o.post(st, "claude", "user", long, "turn")                     # 단독 대화 답변 → 그대로
        for _ in range(o.TURN_ARCHIVE_KEEP):
            o.post(st, "codex", "all", "새 말", "msg")
        o.post(st, "claude", "log", long, "turn", job=1)               # 최근 → 그대로
        count = len(st["msgs"])
        assert o.archive_old_turns(st, "test") == 1
        assert o.archive_old_turns(st, "test") == 0                    # 두 번 옮기지 않음
    with o.state(sid="test") as st:
        assert len(st["msgs"]) == count and [m["id"] for m in st["msgs"]] == list(range(1, count + 1))
        m1, m2, m3, m4 = st["msgs"][:4]
        assert m1["archived"] and len(m1["text"]) < 400 and m1["text"].startswith("가")
        assert not m2.get("archived") and m2["text"] == long
        assert not m3.get("archived") and not m4.get("archived") and not st["msgs"][-1].get("archived")
    assert o.archived_turn("test", 1) == long
    assert o.archived_turn("test", 2) is None and o.archived_turn("nope", 1) is None
    # 지난 저장이 줄 중간에서 끊긴 보관 파일 뒤에 이어 써도 새 본문을 읽을 수 있음
    archive = o.sdir("test") / "turns-archive.jsonl"
    archive.write_text(archive.read_text() + '{"id": 99, "text": "끊긴')
    with o.state(True, "test") as st:
        st["jobs"][1]["status"] = "done"
        assert o.archive_old_turns(st, "test") == 1                   # 열린 요청이었던 #2 기록
    assert o.archived_turn("test", 2) == long and o.archived_turn("test", 1) == long
    print("turn context ok")
