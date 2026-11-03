"""맥과 WSL에서 설정·경로 표시가 기능에 맞게 나뉘는지 화면으로 확인해요.
실행: python3 tests/test_mac_settings_ui.py [office.html 경로] [결과 폴더]
서버 응답은 가짜예요. 실제 Safari·맥 화면을 대신하지는 않아요.
"""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

html_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "payload/office.html"
output = Path(sys.argv[2]) if len(sys.argv) > 2 else None
html = html_path.read_text(encoding="utf-8")
MAC = {"mac": True, "storage": False, "resources": False, "app_window": False, "claude_limits": False}
WSL = {"mac": False, "storage": True, "resources": True, "app_window": True, "claude_limits": True}
RES = {"supported": True, "cpu_percent": 70, "memory_high_gb": 0, "logical_cpus": 8, "physical_memory_bytes": 16 * 1024**3}
STORAGE = {"storage_notice": {"status": "suggested", "checked_at": 1, "drive": "C:", "free_gib": 5.0, "reclaim_gib": 9.0},
           "storage_compact": {"available": True, "can_start": True, "status": "idle"},
           "storage_notice_preferences": {"hidden": False, "mode": "show"}, "storage_result_closed": ""}
MESSAGE = "경로 `/Users/me/a.txt` 와 `C:\\Users\\me` 와 `/mnt/c/x` 와 /tmp/plain.txt 를 확인해요"


def state(features, file_manager, extra):
    return {"rev": 1, "session": "s", "root": "/repo", "version": "1", "update": {}, "sessions": [{"id": "s", "title": "t", "open": 0}],
            "agents": {"claude": {"status": "idle", "activity": [], "tokens": 0}, "codex": {"status": "idle", "activity": [], "tokens": 0}},
            "messages": [], "tasks": [], "jobs": [],
            "features": features, "file_manager": file_manager, **extra}


with sync_playwright() as p:
    browser = p.chromium.launch()
    for name, features, manager, extra in (("mac", MAC, "Finder", {"resources": None}), ("wsl", WSL, "탐색기", {"resources": RES, **STORAGE}), ("wsl-limited", {**WSL, "storage": False, "powershell": False}, "탐색기", {"resources": RES, **STORAGE})):
        context = browser.new_context(viewport={"width": 1500, "height": 920})
        errors = []
        reply = state(features, manager, extra)
        context.route("http://office.test/**", lambda r: r.fulfill(body=html, content_type="text/html"))
        context.route("**/api/state*", lambda r: r.fulfill(json=reply))
        context.route("**/api", lambda r: r.fulfill(json={}))
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto("http://office.test/")
        page.wait_for_function("document.querySelector('#root').textContent === '/repo'")
        page.evaluate("toggleSettings(true)")
        visible = lambda selector: page.locator(selector).is_visible()
        titles = page.evaluate("""message => {
          const box = document.createElement("div");
          box.innerHTML = md(message);
          return [...box.querySelectorAll(".path")].map(e => [e.dataset.path, e.title]);
        }""", MESSAGE)
        usage = page.evaluate("usageBlock('claude', {tokens: 0})")
        font = Path("/mnt/c/Windows/Fonts/malgun.ttf")
        if font.is_file():
            context.route("**/test-font.ttf", lambda r: r.fulfill(body=font.read_bytes(), content_type="font/ttf"))
            page.add_style_tag(content="@font-face{font-family:TestKorean;src:url('/test-font.ttf')}body,button{font-family:TestKorean,sans-serif}")
            page.evaluate("document.fonts.ready")
        if output:
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / f"settings-{name}.png"))
        assert not errors, errors
        mac = name == "mac"
        assert visible("#settings") and visible("#theme")
        limited = name == "wsl-limited"
        assert visible("#powershellNotice") is limited, name
        assert visible("#storageManualRun") == (not mac and not limited), name
        assert visible("#storageRestore") == (not mac and not limited), name
        assert visible("#resbox") is not mac, name
        assert visible("#storageNotice") is not mac, name
        assert ("사용 한도 정보 아직 없음" in usage) is not mac, usage
        assert page.locator("#root").get_attribute("title") == f"{manager}에서 열기"
        paths = [path for path, _ in titles]
        assert "/Users/me/a.txt" in paths, titles
        assert all(title == f"{manager}에서 열기" for _, title in titles), titles
        if mac:
            assert not any(path.startswith(("C:", "/mnt/")) for path in paths), titles
            assert not visible("#appWindowRow")
        else:
            assert any(path.startswith("/mnt/") for path in paths), titles
        context.close()
    browser.close()
print(json.dumps({"passed": True}))
