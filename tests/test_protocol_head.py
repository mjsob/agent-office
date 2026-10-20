"""이어지는 차례에 작업 지침 전문을 다시 보내지 않는지 확인해요.
실행: python3 -I tests/test_protocol_head.py [payload/office.py 경로]
서버를 실행하지 않고 임시 상태 폴더만 사용해요.
"""
import importlib.util, os, sys, tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="office-protocol-head-") as tmp:
    for key in list(os.environ):
        if key.startswith("OFFICE_"):
            del os.environ[key]
    os.environ.update(OFFICE_DIR=tmp, OFFICE_AS="codex", OFFICE_SESSION="test")
    src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
    spec = importlib.util.spec_from_file_location("office_protocol_head_test", src)
    o = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(o)
    w = object.__new__(o.Worker)
    w.name, w.other, w.sid = "claude", "codex", "test"
    o.sdir("test").mkdir(parents=True, exist_ok=True)
    full = o.PROTOCOL.format(me="claude", other="codex")
    sid_file = o.sdir("test") / "claude.sid"
    assert w.protocol_head(full, False) == full                      # 세션 파일이 없으면 첫 차례 → 전문
    sid_file.write_text("session-1")
    for _ in range(o.PROTOCOL_REFRESH_TURNS):
        head = w.protocol_head(full, False)
        assert head != full and len(head) < len(full) // 4 and "지침" in head   # 이어지는 차례 → 짧은 안내
    assert w.protocol_head(full, False) == full                      # 일정 차례가 지나면 전문을 다시 보냄(자동 압축 대비)
    assert w.protocol_head(full, False) != full                      # 전문을 보낸 뒤엔 다시 짧게
    assert w.protocol_head(full, True) == full                       # 새 세션으로 바꿨으면 전문
    assert w.protocol_head(full + " 새 규칙", False) == full + " 새 규칙"   # 지침이 바뀌면 전문
    assert w.protocol_head(full + " 새 규칙", False) != full + " 새 규칙"
    other = object.__new__(o.Worker)                                  # 다른 에이전트는 따로 셈
    other.name, other.other, other.sid = "codex", "claude", "test"
    codex_full = o.PROTOCOL.format(me="codex", other="claude")
    assert other.protocol_head(codex_full, False) == codex_full
    print("protocol head ok")
