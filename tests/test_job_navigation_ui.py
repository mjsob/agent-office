"""이전 요청과 이어받은 요청으로 이동할 때 본문이 보이는지 확인해요.
서버 응답은 시험 자료로 바꿔요. 전체 화면 코드는 Chromium에서 실행해요.
"""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

source = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith('--') else Path(__file__).resolve().parents[1] / "payload/office.html"
html = source.read_text()
results = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    for mac, width in ((False, 1400), (True, 1400), (False, 700)):
        if '--narrow-only' in sys.argv and width != 700:
            continue
        jobs = [{"id": i, "text": f"stored request {i}", "status": "done", "started_at": 1700000000 + i} for i in range(1, 10)]
        jobs.insert(0, {"id": 0, "text": "old request without messages", "status": "done"})
        msgs = [{"id": 1, "job": 1, "from": "system", "kind": "event", "text": "continued"}]
        msgs += [{"id": i * 10, "job": i, "from": "user", "to": "all", "kind": "request", "text": f"original request {i}"} for i in range(2, 10)]
        reply = {"rev": 1, "session": "s", "root": "/repo", "version": "test", "sessions": [], "agents": {},
                 "jobs": jobs, "msgs": msgs, "tasks": [{"id": 1, "job": 1, "status": "waiting", "owner": "codex", "title": "learning"}],
                 "features": {"mac": mac}, "update": {}}
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("http://office.test/**", lambda r: r.fulfill(body=html, content_type="text/html"))
        page.route("**/api/state*", lambda r: r.fulfill(json=reply))
        page.route("**/api", lambda r: r.fulfill(json={}))
        page.goto("http://office.test/")
        page.wait_for_function("document.querySelector('#board .job')")

        def jump(job):
            # 실제 카드가 호출하는 함수를 실행해요. 좁은 화면에서는 현황판을 먼저 열어요.
            if width < 900:
                page.evaluate("document.body.classList.add('show-side')")
            page.evaluate(f"jumpTo({job})")
            page.wait_for_function(f"document.querySelector('#th-{job} .msg.user .body')")
            page.wait_for_function(f"""()=>{{const a=document.getElementById('th-{job}').getBoundingClientRect(),s=document.getElementById('tlsec').getBoundingClientRect();return a.top>=s.top-2&&a.top<s.bottom}}""")

        if width < 900:
            page.evaluate("document.body.classList.add('show-side')")
        page.locator('#board .job .tx.jump').click()
        jump(1)
        assert page.locator('#th-1 .msg.user .body').inner_text() == 'stored request 1'
        assert not page.locator('body').evaluate("e=>e.classList.contains('show-side')")
        # 완료된 옛 요청은 접힌 목록을 펼친 뒤 이동해요. 원본이 있으면 저장된 요약으로 덮지 않아요.
        jump(2)
        assert page.locator('#th-2 .msg.user .body').inner_text() == 'original request 2'
        assert page.locator('details[data-k="old"]').get_attribute('open') is not None
        jump(0)
        assert page.locator('#th-0 .msg.user .body').inner_text() == 'old request without messages'
        assert page.evaluate("!!(document.getElementById('th-0').compareDocumentPosition(document.getElementById('th-1')) & Node.DOCUMENT_POSITION_FOLLOWING)")
        # 남은 메시지가 하나도 없는 요청도 저장된 내용으로 보여요.
        reply['jobs'].append({"id": 10, "text": "no messages", "status": "working"})
        reply['rev'] += 1
        page.evaluate("lastSig='';refresh()")
        page.wait_for_function("lastJobs.some(j=>j.id===10)")
        jump(10)
        assert page.locator('#th-10 .msg.user .body').inner_text() == 'no messages'
        # 지운 요청은 복원 대상에서 계속 제외해요.
        reply['jobs'].append({"id": 11, "text": "removed", "status": "done", "removed": True})
        reply['rev'] += 1
        page.evaluate("lastSig='';refresh()")
        page.wait_for_function("lastJobs.some(j=>j.id===11)")
        assert page.locator('#th-11, #ans-11').count() == 0
        assert not errors, errors
        results.append({"mac": mac, "width": width, "cases": ["continued request card", "collapsed older request", "original request preserved", "no messages", "removed request hidden", "old empty request order"], "passed": True})
        page.close()
    browser.close()
print(json.dumps(results, ensure_ascii=False, indent=2))
