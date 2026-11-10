"""로그인 필요·풀림 알림과 로그인 링크 메시지가 '진행 과정' 접힘 밖에 남는지 확인해요.
로그인 완료 알림이 오면 예전처럼 접혀요. 서버 응답은 시험 자료로 바꿔요."""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

source = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "payload/office.html"
html = source.read_text()
LINK = "Codex 로그인 링크예요. https://auth.example/login?x=1"


def msgs(done):
    m = [{"id": 1, "job": 1, "from": "user", "to": "all", "kind": "request", "text": "request one"},
         {"id": 2, "job": 1, "from": "system", "to": "user", "kind": "event", "text": "Codex 로그인 풀림\n로그인이 풀려서 Codex가 멈췄어요.\nhttps://auth.example/login?x=1"},
         {"id": 3, "job": 1, "from": "codex", "to": "user", "kind": "msg", "text": LINK},
         {"id": 4, "job": 1, "from": "codex", "to": "user", "kind": "msg", "text": "plain note A"},
         {"id": 5, "job": 1, "from": "codex", "to": "user", "kind": "msg", "text": "plain note B"},
         {"id": 6, "job": 1, "from": "codex", "to": "user", "kind": "msg", "text": "plain note C"},
         {"id": 7, "job": 1, "from": "codex", "to": "user", "kind": "msg", "text": "plain note D"}]
    if done:
        m.append({"id": 8, "job": 1, "from": "system", "to": "user", "kind": "event", "text": "Codex 로그인 완료\n이제 바로 일을 맡길 수 있어요."})
    return m


with sync_playwright() as p:
    browser = p.chromium.launch()
    for done in (False, True):
        reply = {"rev": 1, "session": "s", "root": "/repo", "version": "test", "sessions": [], "agents": {},
                 "jobs": [{"id": 1, "text": "request one", "status": "working", "started_at": 1700000001}],
                 "msgs": msgs(done), "tasks": [], "features": {}, "update": {}}
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("http://office.test/**", lambda r: r.fulfill(body=html, content_type="text/html"))
        page.route("**/api/state*", lambda r: r.fulfill(json=reply))
        page.route("**/api", lambda r: r.fulfill(json={}))
        page.goto("http://office.test/")
        page.wait_for_function("document.querySelector('#th-1')")
        # 접힘(details) 안에 들어가지 않은 메시지 본문만 모아요.
        outside = page.evaluate("""[...document.querySelectorAll('#th-1 .msg, #th-1 .event')].filter(e=>!e.closest('details')).map(e=>e.innerText)""")
        joined = "\n".join(outside)
        if done:
            assert "https://auth.example" not in joined, outside
        else:
            assert "로그인 풀림" in joined and "https://auth.example/login" in joined, outside
            assert LINK in joined, outside
        assert not errors, errors
        page.close()
        print("ok", "완료 뒤" if done else "로그인 대기")
    browser.close()
