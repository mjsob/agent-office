"""창이 뒤에 있을 때 새로 끝난 대화가 생기면 작업 표시줄 알림을 켜고 끄는지 화면으로 확인해요.
실행: python3 tests/test_taskbar_attention_ui.py [office.html 경로]
서버 응답은 가짜예요. 실제 작업 표시줄 깜빡임은 office_win.cs 쪽에서 확인해요.
"""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

html_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "payload/office.html"
html = html_path.read_text(encoding="utf-8")
WIN = {"mac": False, "storage": True, "resources": True, "app_window": True, "claude_limits": True, "taskbar_attention": True}


def run(features):
    reply = {"rev": 1, "session": "s", "root": "/repo", "version": "1", "update": {}, "sessions": [],
             "agents": {"claude": {"status": "idle", "activity": [], "tokens": 0}, "codex": {"status": "idle", "activity": [], "tokens": 0}},
             "messages": [], "tasks": [], "jobs": [], "features": features, "file_manager": "탐색기"}
    posts, errors, bodies, focused = [], [], [], []
    notice = {"s": 1, "a": 1, "b": 1, "c": 1}
    mode = {"now": "ok"}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 900}, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36")
        context.route("http://127.0.0.1:9/**", lambda r: r.fulfill(body=html, content_type="text/html"))
        def state_route(r):
            focused.append(r.request.url.split("focused=")[1][:1] if "focused=" in r.request.url else None)
            r.fulfill(json=reply)

        def attention_route(r):
            body = json.loads(r.request.post_data)
            bodies.append(body)
            posts.append(body["active"])
            if mode["now"] == "fail":
                return r.abort()
            r.fulfill(json={"ok": True, "matched": 0 if mode["now"] == "matched0" else 1, "flashing": body["active"] and mode["now"] == "ok"})
        context.route("**/api/state*", state_route)
        context.route("**/api/window-attention", attention_route)
        context.route("**/api", lambda r: r.fulfill(json={}))
        page = context.new_page()
        page.on("pageerror", lambda e: (errors.append(str(e)), print("ERR", e)))
        page.add_init_script("window.__focus = true; document.hasFocus = () => window.__focus;")

        def set_sessions(unread):
            reply["rev"] += 1
            reply["sessions"] = [{"id": i, "title": f"대화 {i}", "open": 0, "updated": 1, "unread": i in unread and i != "s",
                                  "attention_unread": i in unread, "notice_at": notice[i]} for i in ("s", "a", "b", "c")]

        def sessions(*unread, focus=None):
            set_sessions(unread)
            if focus is not None:
                page.evaluate(f"window.__focus = {str(focus).lower()}; window.dispatchEvent(new Event('{'focus' if focus else 'blur'}'))")
            page.evaluate("refresh()")
            page.wait_for_timeout(150)
            return list(posts)

        set_sessions(("a",))
        page.goto("http://127.0.0.1:9/")
        page.wait_for_function("document.querySelector('#root').textContent === '/repo'")
        icon = page.evaluate("document.querySelector('link[rel=icon]').getAttribute('href')")
        page.evaluate("toggleSettings(true)")
        row = page.locator("#attentionRow").is_visible()
        label = page.locator("#attentionRow span").inner_text()
        steps = {}
        steps["open_with_old_unread_then_blur"] = sessions("a", focus=False)
        steps["focused_new_unread"] = (sessions("a", focus=True), sessions("a", "b"))[1]
        steps["away_new_unread"] = sessions("a", "b", "c", focus=False)
        steps["same_state_refresh"] = (sessions("a", "b", "c"), sessions("a", "b", "c"))[1]
        steps["focus_back"] = sessions("a", "b", "c", focus=True)
        steps["blur_again_no_new"] = sessions("a", "b", "c", focus=False)
        steps["read_then_done_again"] = (sessions("a", "b"), sessions("a", "b", "c"))[1]
        steps["second_new_while_flashing"] = (sessions("a", "b"), sessions("a", "b", "s"))[1]
        page.evaluate("cycleAttention()"); page.wait_for_timeout(100)
        steps["setting_off"] = list(posts)
        steps["new_while_off"] = sessions("a")
        page.evaluate("cycleAttention()"); page.wait_for_timeout(100)
        steps["setting_on_no_new"] = list(posts)
        steps["new_after_on"] = sessions("a", "b")
        notice["b"] = 2
        steps["same_chat_done_again_unread"] = sessions("a", "b")
        steps["all_read_while_away"] = sessions()
        def new_posts(fn):
            start = len(posts); fn(); return posts[start:]
        retry = {}
        mode["now"] = "matched0"; notice["c"] = 2
        retry["matched0_first"] = new_posts(lambda: sessions("c"))
        retry["matched0_retry"] = new_posts(lambda: sessions("c"))
        mode["now"] = "ok"
        retry["found_after_retry"] = new_posts(lambda: sessions("c"))
        retry["after_found_quiet"] = new_posts(lambda: (sessions("c"), sessions("c")))
        mode["now"] = "fail"
        retry["stop_fails"] = new_posts(lambda: sessions())
        mode["now"] = "ok"
        retry["stop_retry_ok"] = new_posts(lambda: (sessions(), sessions()))
        mode["now"] = "fail"; notice["a"] = 2
        retry["gives_up_after_5"] = new_posts(lambda: [sessions("a") for _ in range(8)])
        mode["now"] = "ok"
        away_query, before = focused[-1], len(focused)
        page.evaluate("window.__focus = true; window.dispatchEvent(new Event('focus'))"); page.wait_for_timeout(150)
        back_query = focused[before] if len(focused) > before else None   # 돌아오자마자 바로 다시 조회해요
        title = page.title()
        icon_after = page.evaluate("document.querySelector('link[rel=icon]').getAttribute('href')")
        browser.close()
    return {"retry": retry, "bodies": bodies, "away_query": away_query, "back_query": back_query, "row": row, "label": label, "steps": steps, "errors": errors, "title": title, "icon": [icon, icon_after]}


win = run(WIN)
print(json.dumps(win, ensure_ascii=False, indent=1))
assert not win["errors"], win["errors"]
assert win["row"] and win["label"] == "작업 표시줄 알림"
s = win["steps"]
assert s["open_with_old_unread_then_blur"] == []
assert s["focused_new_unread"] == []
assert s["away_new_unread"] == [True]
assert s["same_state_refresh"] == [True]
assert s["focus_back"] == [True, False]
assert s["blur_again_no_new"] == [True, False]
assert s["read_then_done_again"] == [True, False, True]
assert s["second_new_while_flashing"] == [True, False, True, True]
assert s["setting_off"] == [True, False, True, True, False]
assert s["new_while_off"] == s["setting_off"] and s["setting_on_no_new"] == s["setting_off"]
assert s["new_after_on"] == s["setting_off"] + [True]
assert s["same_chat_done_again_unread"] == s["setting_off"] + [True, True]
assert s["all_read_while_away"] == s["setting_off"] + [True, True, False]
r = win["retry"]
assert r["matched0_first"] == [True] and r["matched0_retry"] == [True] and r["found_after_retry"] == [True] and r["after_found_quiet"] == [], r
assert r["stop_fails"] == [False] and r["stop_retry_ok"] == [False], r
assert r["gives_up_after_5"] == [True] * 5, r
assert win["away_query"] == "0" and win["back_query"] == "1", (win["away_query"], win["back_query"])
assert all(set(b) == {"active", "title", "client"} and b["title"].endswith(" — Office") and len(b["client"]) == 36 for b in win["bodies"]), win["bodies"]
assert "(" not in win["title"] and win["icon"][0] == win["icon"][1] == "/favicon.ico"

for name, features in (("mac", {**WIN, "mac": True}), ("windows-no-helper", {**WIN, "taskbar_attention": False})):
    r = run(features)
    assert not r["errors"], r["errors"]
    assert not r["row"], name
    assert all(v == [] for v in r["steps"].values()), (name, r["steps"])
print("ok")
