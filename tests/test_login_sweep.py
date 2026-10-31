"""Office 가 띄운 링크가 아닌 길로 로그인해도 멈춘 일이 풀리는지 확인해요.
실행: python3 -I tests/test_login_sweep.py [payload/office.py 경로]
서버·로그인 과정을 실행하지 않고 임시 폴더와 가짜 로그인 함수만 써요."""
import importlib.util, json, os, sys, tempfile, threading, time, unittest
from pathlib import Path
SOURCE = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and sys.argv[1].endswith(".py") else Path(__file__).resolve().parents[1] / "payload" / "office.py"


class LoginSweep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="office-login-sweep-"); self.addCleanup(self.tmp.cleanup)
        env = os.environ.copy(); self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        for key in list(os.environ):
            if key.startswith("OFFICE_"):
                del os.environ[key]
        os.environ.update(OFFICE_DIR=self.tmp.name, OFFICE_SESSION="test", OFFICE_AS="codex", OFFICE_JOB="1")
        spec = importlib.util.spec_from_file_location("login_sweep_target", SOURCE)
        self.o = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.o)
        self.assertTrue(str(self.o.D).startswith(self.tmp.name))
        self.o.server_log = lambda *a: None; self.o.current_sid = lambda: "test"
        self.o.atomic_json(self.o.sdir("test") / "state.json", dict(jobs=[], tasks=[self.auth_task()], msgs=[], seq=1), self.o.state_valid)
        self.checks = []
        self.ok = True
        self.o.login_ok = lambda name: self.checks.append(name) or self.ok
        self.office = object.__new__(self.o.Office)
        self.office.stop = threading.Event()
        self.worker = self.make_worker()
        self.office.teams = {"test": {"w": {"codex": self.worker}}}

    def auth_task(self, owner="codex"):
        now = time.time()
        return {"id": 1, "job": 1, "owner": owner, "title": "독립 검수", "status": "waiting", "created_at": now, "waiting_at": now, "wait_reason": "로그인 확인 필요",
                "user_request": {"status": "pending", "text": "로그인 확인 필요", "created_at": now, "expires_at": None, "resolved_at": None, "resolution": None,
                                 "reply_to": None, "source": "agent_auth", "resume_status": "todo"}}

    def make_worker(self, **extra):
        w = self.o.Worker.__new__(self.o.Worker)
        base = dict(name="codex", sid="test", paused=True, manual_pause=False, error="로그인 풀림", error_kind="auth", recovery=None, cont=1, chatter=1, nudge_state={"x": 1},
                    auto_retries=1, retry_at=0, retry_job=None, retry_due=None, resume_turn={"job": 1, "kind": "work"}, control_lock=threading.RLock())
        base.update(extra)
        for key, value in base.items():
            setattr(w, key, value)
        w.save_checkpoint = lambda *a, **kw: None
        return w

    def task(self):
        return json.loads((self.o.sdir("test") / "state.json").read_text())["tasks"][0]

    def notices(self):
        return [m["text"] for m in json.loads((self.o.sdir("test") / "state.json").read_text())["msgs"]]

    def test_login_outside_office_frees_task_and_turn(self):
        self.office.sweep_login()
        task = self.task()
        self.assertEqual(task["status"], "todo")
        self.assertEqual(task["user_request"]["status"], "resolved")
        self.assertNotIn("wait_reason", task)
        self.assertFalse(self.worker.paused)
        self.assertIsNone(self.worker.error_kind)
        self.assertEqual(self.worker.retry_due, {"job": 1, "kind": "work"})
        self.assertEqual(len([t for t in self.notices() if "로그인 완료" in t]), 1)

    def test_task_alone_is_freed_when_turn_was_already_resumed(self):
        self.worker.paused, self.worker.error_kind = False, None   # 사용자가 다시 시작을 먼저 누른 경우
        self.office.sweep_login()
        self.assertEqual(self.task()["status"], "todo")

    def test_nothing_happens_until_login_succeeds(self):
        self.ok = False
        self.office.sweep_login()
        self.assertEqual(self.task()["status"], "waiting")
        self.assertTrue(self.worker.paused)
        self.assertEqual(self.notices(), [])
        self.ok = True
        self.office.sweep_login()
        self.assertEqual(self.task()["status"], "todo")
        self.assertFalse(self.worker.paused)

    def test_stale_token_does_not_loop(self):
        self.office.sweep_login()
        self.worker.paused, self.worker.error_kind = True, "auth"   # 상태는 정상인데 실행하면 또 인증 오류
        self.office.sweep_login()
        self.assertTrue(self.worker.paused)
        self.assertEqual(len([t for t in self.notices() if "로그인 완료" in t]), 1)
        self.ok = False
        self.office.sweep_login()   # 진짜로 풀린 걸 보면 다음 로그인 때 다시 풀어요
        self.ok = True
        self.office.sweep_login()
        self.assertFalse(self.worker.paused)

    def test_manual_pause_stays(self):
        self.worker.manual_pause = True
        self.office.sweep_login()
        self.assertTrue(self.worker.paused)
        self.assertEqual(self.task()["status"], "todo")   # 대기 작업은 풀어도 사용자 정지는 유지해요

    def test_offered_link_is_left_to_watch_login(self):
        self.office.login_pending = {"codex"}
        self.office.sweep_login()
        self.assertEqual(self.task()["status"], "waiting")
        self.assertTrue(self.worker.paused)
        self.assertNotIn("codex", self.checks)

    def test_no_login_check_when_nothing_is_stuck(self):
        self.worker.paused, self.worker.error_kind = False, None
        self.o.atomic_json(self.o.sdir("test") / "state.json", dict(jobs=[], tasks=[], msgs=[], seq=1), self.o.state_valid)
        self.office.sweep_login()
        self.assertEqual(self.checks, [])

    def test_other_agents_task_is_left_alone(self):
        self.o.atomic_json(self.o.sdir("test") / "state.json", dict(jobs=[], tasks=[self.auth_task("claude")], msgs=[], seq=1), self.o.state_valid)
        self.worker.paused, self.worker.error_kind = False, None
        self.office.sweep_login()
        self.assertEqual(self.task()["status"], "waiting")


if __name__ == "__main__":
    unittest.main(verbosity=1)
