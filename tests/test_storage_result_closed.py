"""닫은 디스크 줄이기 결과가 서버 파일에 남아 서버를 다시 켜도 유지되는지 확인해요.
실행: python3 -I tests/test_storage_result_closed.py [payload/office.py 경로]"""
import importlib.util, os, sys, tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="office-storage-closed-") as tmp:
    for key in list(os.environ):
        if key.startswith("OFFICE_"):
            del os.environ[key]
    os.environ.update(OFFICE_DIR=tmp, OFFICE_AS="codex", OFFICE_SESSION="test")
    src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
    spec = importlib.util.spec_from_file_location("office_storage_closed_test", src)
    o = importlib.util.module_from_spec(spec); spec.loader.exec_module(o)
    o.D.mkdir(parents=True, exist_ok=True)
    office = object.__new__(o.Office)
    assert o.read_json_file(o.D / "storage-result-closed.json", {}) == {}
    reply = office.handle({"action": "storage_result_close", "key": "abc123"})
    assert reply == {"storage_result_closed": "abc123"}, reply
    # 새 서버(새 모듈)에서도 같은 값이 읽혀요
    spec2 = importlib.util.spec_from_file_location("office_storage_closed_test2", src)
    o2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(o2)
    assert o2.read_json_file(o2.D / "storage-result-closed.json", {}) == {"key": "abc123"}
    for bad in (None, "", "a" * 65, "../x", "a b", 5):
        try:
            office.handle({"action": "storage_result_close", "key": bad})
        except o.UserError:
            continue
        raise AssertionError(bad)
    assert o.read_json_file(o.D / "storage-result-closed.json", {}) == {"key": "abc123"}   # 잘못된 값은 기존 기록을 안 바꿔요
print("닫은 결과 저장 시험 통과")
