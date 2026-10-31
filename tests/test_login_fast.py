"""로그인이 풀렸을 때 링크를 빨리 받고 코드를 바로 넣는지 확인해요.
실행: python3 -I tests/test_login_fast.py [payload/office.py 경로]
서버·로그인 과정을 실행하지 않고 임시 폴더와 가짜 로그인 함수만 써요."""
import importlib.util, json, os, sys, tempfile, threading, time, unittest
from pathlib import Path
SOURCE = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and sys.argv[1].endswith(".py") else Path(__file__).resolve().parents[1] / "payload" / "office.py"
CODE = "bj1gxEabcdefghijklmnopqrstuvwxyz0123456789#abcdefghijklmnop"


class LoginFast(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="office-login-fast-"); self.addCleanup(self.tmp.cleanup)
        env = os.environ.copy(); self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        for key in list(os.environ):
            if key.startswith("OFFICE_"):
                del os.environ[key]
        os.environ.update(OFFICE_DIR=self.tmp.name, OFFICE_SESSION="test", OFFICE_AS="codex", OFFICE_JOB="1")
        spec = importlib.util.spec_from_file_location("login_fast_target", SOURCE)
        self.o = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.o)
        self.o.server_log = lambda *a: None; self.o.release_reservations = lambda *a: None; self.o.LOGIN_HOOK = None
        for sid in ("test", "other"):
            self.o.atomic_json(self.o.sdir(sid) / "state.json", dict(jobs=[], tasks=[], msgs=[], seq=1), self.o.state_valid)

    def worker(self, **extra):
        w = self.o.Worker.__new__(self.o.Worker)
        base = dict(name="claude", sid="test", rc=0, doing={"job": 1, "kind": "work"}, resume_turn=None, fail_count=0, failure_key=None, retry_counts={},
                    stop=threading.Event(), immediate_pause=False, manual_pause=False, interrupted_job=None, read_upto=3, turn_read_start=1,
                    paused=False, retry_at=0, limit_resume_at=0, limit_tries=0, error=None, error_kind=None, recovery=None, notice=None,
                    control_lock=threading.RLock(), auto_retries=0, retry_job=None)
        base.update(extra)
        for key, value in base.items():
            setattr(w, key, value)
        w.idle = lambda status="idle": setattr(w, "status", status); w.save_checkpoint = lambda *a, **kw: None
        return w

    def test_auth_error_pauses_at_once_without_retries(self):
        w = self.worker()
        w.pause_with_error("Error: 401 unauthorized — please run /login", 1)
        self.assertTrue(w.paused); self.assertEqual(w.error_kind, "auth"); self.assertEqual(w.retry_counts, {}); self.assertEqual(w.retry_at, 0)

    def test_other_errors_still_retry(self):
        w = self.worker()
        w.pause_with_error("segmentation fault", 1)
        self.assertFalse(w.paused); self.assertEqual(w.retry_counts, {"1": 1})

    def test_pasted_code_with_bell_and_url_is_fed_clean(self):
        fed = []
        self.o.login_feed = lambda name, code: fed.append((name, code))
        office = object.__new__(self.o.Office); office.login_pending = {"claude"}
        pasted = CODE + "\x07https://claude.com/cai/oauth/authorize?code=true"
        self.assertTrue(office.take_login_code("test", pasted))
        self.assertEqual(fed, [("claude", CODE)])
        fed.clear()
        self.assertTrue(office.take_login_code("test", CODE + "\n"))   # 평범한 붙여넣기도 그대로 돼요
        self.assertEqual(fed, [("claude", CODE)])
        fed.clear()
        self.assertFalse(office.take_login_code("test", "이 코드가 맞나요? 로그인 어떻게 해요"))   # 일반 말은 요청으로 가요
        self.assertEqual(fed, [])

    def test_login_notice_reaches_the_chat_the_user_is_viewing(self):
        self.o.current_sid = lambda: "other"
        office = object.__new__(self.o.Office)
        office.login_post("test", "Claude 로그인 풀림", "링크\nhttps://example.com/login")
        for sid in ("test", "other"):
            texts = [m["text"] for m in json.loads((self.o.sdir(sid) / "state.json").read_text())["msgs"]]
            self.assertTrue(any("https://example.com/login" in t for t in texts), sid)

    def test_midway_offer_checks_both_agents(self):
        asked, started = [], []
        self.o.login_ok = lambda name: asked.append(name) or False
        self.o.login_start = lambda name: started.append(name) or f"https://example.com/{name}"
        self.o.current_sid = lambda: "test"
        office = object.__new__(self.o.Office); office.stop = threading.Event()
        office.watch_login = lambda sid: None
        office.offer_login("test", ["claude"], True)
        self.assertEqual(sorted(started), ["claude", "codex"])   # claude 만 멈췄어도 둘 다 풀렸으면 둘 다 링크를 받아요


if __name__ == "__main__":
    unittest.main(verbosity=1)
