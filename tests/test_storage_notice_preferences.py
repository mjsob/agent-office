"""디스크 알림 표시 설정(보기·오늘 숨기기·계속 숨기기) 저장을 확인해요.
실행: python3 -I tests/test_storage_notice_preferences.py [payload/office.py 경로]
"""
import importlib.util, os, sys, tempfile, time
from pathlib import Path
with tempfile.TemporaryDirectory(prefix="office-sn-") as tmp:
    for k in list(os.environ):
        if k.startswith("OFFICE_"): del os.environ[k]
    os.environ.update(OFFICE_DIR=tmp, OFFICE_AS="codex", OFFICE_SESSION="test")
    spec = importlib.util.spec_from_file_location("o", sys.argv[1] if len(sys.argv)>1 else str(Path(__file__).resolve().parent.parent/"payload"/"office.py")); o = importlib.util.module_from_spec(spec); spec.loader.exec_module(o)
    o.D.mkdir(parents=True, exist_ok=True)
    off = o.Office()
    v = o.storage_notice_preferences_view(); assert v == {"mode":"show","hidden":False,"hidden_on":None}
    off.handle({"action":"ui_settings","theme":"dark"})
    r = off.handle({"action":"storage_notice_preferences","mode":"today","hidden_on":"1999-01-01"})["storage_notice_preferences"]
    assert r["hidden"] and r["hidden_on"] == time.strftime("%Y-%m-%d")   # 클라이언트 날짜 무시
    f = o.D/"storage-notice-preferences.json"
    f.write_text('{"mode":"today","hidden_on":"2000-01-01"}'); assert not o.storage_notice_preferences_view()["hidden"]
    r = off.handle({"action":"storage_notice_preferences","mode":"never"})["storage_notice_preferences"]
    assert r == {"mode":"never","hidden":True,"hidden_on":None}
    assert o.read_json_file(o.D/"ui-settings.json",{}) == {"theme":"dark"}   # 테마 보존
    for bad in ("x","",None,1,["show"]):
        try: off.handle({"action":"storage_notice_preferences","mode":bad})
        except o.UserError: continue
        raise AssertionError(bad)
    assert o.storage_notice_preferences_view()["mode"] == "never"
    f.write_text("깨짐"); assert o.storage_notice_preferences_view()["mode"] == "show"
    f.write_text('[1]'); assert o.storage_notice_preferences_view()["mode"] == "show"
    off.handle({"action":"storage_notice_preferences","mode":"show"}); assert not o.storage_notice_preferences_view()["hidden"]
    off2 = o.Office(); assert not o.storage_notice_preferences_view()["hidden"]
    print("storage notice preferences ok")
