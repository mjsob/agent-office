"""화면 색 모드가 서버 파일에 저장되는지 확인해요.
실행: python3 -I tests/test_ui_settings.py [payload/office.py 경로]
서버를 실행하지 않고 실제 API 처리 함수와 임시 상태를 사용해요.
"""
import importlib.util, os, sys, tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="office-ui-settings-") as tmp:
    for key in list(os.environ):
        if key.startswith("OFFICE_"):
            del os.environ[key]
    os.environ.update(OFFICE_DIR=tmp, OFFICE_AS="codex", OFFICE_SESSION="test")
    src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
    spec = importlib.util.spec_from_file_location("office_ui_settings_test", src)
    o = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(o)
    assert o.D.is_relative_to(tmp)
    o.D.mkdir(parents=True, exist_ok=True)
    office = o.Office()
    for theme in ("dark", "light", "auto"):
        assert office.handle({"action": "ui_settings", "theme": theme}) == {"ui": {"theme": theme}}
        assert o.read_json_file(o.D / "ui-settings.json", {}) == {"theme": theme}   # 파일에 남아 서버를 다시 켜도 읽어요
    for bad in ("blue", "", None, 1):
        try:
            office.handle({"action": "ui_settings", "theme": bad})
        except o.UserError:
            continue
        raise AssertionError(f"잘못된 색 모드를 받았어요: {bad!r}")
    assert o.read_json_file(o.D / "ui-settings.json", {}) == {"theme": "auto"}   # 거절하면 저장값이 안 바뀌어요
    print("ui settings ok")
