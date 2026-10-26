"""사용 한도 소진 멈춤이 초기화 시각 뒤 자동으로 다시 시작되는지 확인해요.
실행: python3 -I tests/test_limit_resume.py [payload/office.py 경로]
서버를 실행하지 않고 임시 상태 폴더만 사용해요."""
import datetime, importlib.util, json, os, sys, tempfile, threading, time, unittest, zoneinfo
from pathlib import Path
SOURCE = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and sys.argv[1].endswith(".py") else Path(__file__).resolve().parents[1] / "payload/office.py"


class LimitResume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="office-limit-resume-"); self.addCleanup(self.tmp.cleanup)
        env = os.environ.copy(); self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        for key in list(os.environ):
            if key.startswith("OFFICE_"):
                del os.environ[key]
        os.environ.update(OFFICE_DIR=self.tmp.name, OFFICE_SESSION="test", OFFICE_AS="codex", OFFICE_JOB="1")
        spec = importlib.util.spec_from_file_location("limit_resume_target", SOURCE)
        self.o = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.o)
        self.o.server_log = lambda *a: None; self.o.release_reservations = lambda *a: None; self.o.LOGIN_HOOK = None
        self.path = self.o.sdir("test") / "state.json"
        self.o.atomic_json(self.path, dict(jobs=[dict(id=1, status="working", lead="codex", text="j")], tasks=[], msgs=[], seq=1), self.o.state_valid) \
            if hasattr(self.o, "state_valid") else self.path.write_text(json.dumps(dict(jobs=[dict(id=1, status="working", lead="codex", text="j")], tasks=[], msgs=[], seq=1)))

    def worker(self, **extra):
        w = self.o.Worker.__new__(self.o.Worker)
        base = dict(name="claude", sid="test", rc=0, doing={"job": 1, "kind": "work"}, resume_turn=None, fail_count=0, failure_key=None, retry_counts={},
                    stop=threading.Event(), immediate_pause=False, manual_pause=False, interrupted_job=None, read_upto=3, turn_read_start=1,
                    paused=False, retry_at=0, limit_resume_at=0, limit_tries=0, error=None, error_kind=None, recovery=None, notice=None)
        base.update(extra)
        for key, value in base.items():
            setattr(w, key, value)
        w.idle = lambda status="idle": setattr(w, "status", status); w.save_checkpoint = lambda *a, **kw: None
        return w

    def test_reset_time_formats(self):
        seoul = zoneinfo.ZoneInfo("Asia/Seoul")
        now = datetime.datetime(2026, 10, 27, 14, 0, tzinfo=seoul).timestamp()
        at = lambda y, mo, d, h, mi=0: datetime.datetime(y, mo, d, h, mi, tzinfo=seoul).timestamp()
        f = lambda text: self.o.limit_reset_time(text, "claude", now)
        self.assertEqual(f("You've hit your limit · resets 3pm (Asia/Seoul)"), at(2026, 10, 27, 15))
        self.assertEqual(f("You've hit your limit · resets 1:30 PM (Asia/Seoul)"), at(2026, 10, 28, 13, 30))   # 이미 지난 시각이면 내일
        self.assertEqual(f("You've hit your limit · resets Oct 30, 9am (Asia/Seoul)"), at(2026, 10, 30, 9))
        self.assertEqual(f("Claude AI usage limit reached|%d" % (now + 3600)), now + 3600)
        self.assertEqual(f("Claude AI usage limit reached|%d" % ((now + 3600) * 1000)), now + 3600)
        self.assertEqual(f("You've hit your usage limit. Try again in 3 days 2 hours 5 minutes."), now + 3 * 86400 + 2 * 3600 + 300)
        self.assertEqual(f("usage limit reached, resets in 2h 30m"), now + 9000)
        self.assertIsNone(f("usage limit exceeded"))
        self.assertIsNone(f("usage limit reached|%d" % (now + 30 * 86400)))   # 너무 먼 시각은 버림
        self.assertIsNone(f("usage limit reached|%d" % (now - 60)))
        self.assertIsNone(f("limit reached resets 25pm"))

    def test_window_fallback_uses_exhausted_windows_only(self):
        now = time.time()
        self.o.LIMITS["claude"] = {"five_hour": {"used": 100, "resets": now + 600}, "seven_day": {"used": 40, "resets": now + 5 * 86400}, "at": now}
        self.assertEqual(self.o.limit_reset_time("usage limit exceeded", "claude", now), now + 600)
        self.o.LIMITS["claude"] = {"five_hour": {"used": 100, "resets": now + 600}, "seven_day": {"used": 100, "resets": now + 3 * 86400}, "at": now}
        self.assertEqual(self.o.limit_reset_time("usage limit exceeded", "claude", now), now + 3 * 86400)
        self.o.LIMITS["claude"] = {"five_hour": {"used": 60, "resets": now + 600}, "at": now}
        self.assertIsNone(self.o.limit_reset_time("usage limit exceeded", "claude", now))

    def test_limit_pause_schedules_and_resumes(self):
        w = self.worker(); now = time.time()
        w.pause_with_error("You've hit your limit|%d" % (now + 3600), 1)
        self.assertTrue(w.paused); self.assertEqual(w.error_kind, "limit"); self.assertTrue(w.limit_resume_exact)
        self.assertAlmostEqual(w.limit_resume_at, now + 3600 + self.o.LIMIT_RESUME_PAD, delta=2)
        self.assertEqual(w.recovery["phase"], "limit_wait"); self.assertEqual(w.recovery["retry_at"], w.limit_resume_at)
        self.assertEqual(w.retry_counts, {})
        notice = [m["text"] for m in json.loads(self.path.read_text())["msgs"]][-1]
        self.assertIn("자동으로 다시 시작해요", notice)
        w.resume_turn = {"job": 1, "kind": "work"}
        w.resume_after_limit()
        self.assertFalse(w.paused); self.assertIsNone(w.error); self.assertIsNone(w.error_kind); self.assertEqual(w.limit_resume_at, 0)
        self.assertEqual(w.retry_due, {"job": 1, "kind": "work"})
        self.assertIn("자동으로 다시 시도해요", [m["text"] for m in json.loads(self.path.read_text())["msgs"]][-1])
        w.resume_turn = {"job": 1, "kind": "work"}   # 또 막히면 새 시각으로 다시 예약
        w.pause_with_error("You've hit your limit|%d" % (time.time() + 7200), 1)
        self.assertTrue(w.paused and w.limit_resume_at > time.time() + 7000)

    def test_unknown_time_rechecks_then_gives_up(self):
        w = self.worker(); now = time.time()
        w.pause_with_error("usage limit exceeded", 1)
        self.assertFalse(w.limit_resume_exact); self.assertAlmostEqual(w.limit_resume_at, now + self.o.LIMIT_RESUME_RECHECK, delta=2)
        self.assertIn("30분마다", [m["text"] for m in json.loads(self.path.read_text())["msgs"]][-1])
        w.limit_tries = self.o.LIMIT_RESUME_TRIES
        w.pause_with_error("usage limit exceeded", 1)
        self.assertTrue(w.paused); self.assertEqual(w.limit_resume_at, 0); self.assertEqual(w.recovery["phase"], "needs_user")

    def test_manual_stop_auth_and_other_errors_do_not_schedule(self):
        for why, flag in [("not logged in", None), ("usage limit exceeded", "manual_pause"), ("usage limit exceeded", "immediate_pause"), ("usage limit exceeded", "stop"),
                          ("usage limit exceeded", "interrupted_job"), ("network error", None)]:
            w = self.worker()
            if flag == "stop": w.stop.set()
            elif flag == "interrupted_job": w.interrupted_job = 1
            elif flag: setattr(w, flag, True)
            w.pause_with_error(why, 1)
            self.assertEqual(w.limit_resume_at, 0, (why, flag))
            if flag in ("manual_pause", "immediate_pause", "stop", "interrupted_job"):
                self.assertTrue(w.paused, (why, flag))

    def test_existing_limit_checkpoint_gets_schedule(self):
        base = dict(read_upto=1, carry_msgs=[], pending_events=[], handled_events=[], paused=True,
                    error="You've hit your limit|%d" % (time.time() + 600), error_kind="limit",
                    interrupted_turn={"job": 1, "kind": "work"})
        path = self.o.sdir("test") / "claude.worker.json"
        self.o.atomic_json(path, base, self.o.checkpoint_valid)
        w = self.o.Worker("claude", "test", threading.Event())
        self.assertGreater(w.limit_resume_at, time.time() + 600)
        self.assertEqual(w.recovery["phase"], "limit_wait")
        w.save_checkpoint()
        again = self.o.Worker("claude", "test", threading.Event())
        self.assertEqual(again.limit_resume_at, w.limit_resume_at)
        # 명시적인 사용자 정지나 재확인 횟수 소진 기록은 다시 예약하지 않아요.
        for extra in ({"manual_pause": True}, {"limit_resume_at": 0, "limit_tries": self.o.LIMIT_RESUME_TRIES}):
            self.o.atomic_json(path, dict(base, **extra), self.o.checkpoint_valid)
            stopped = self.o.Worker("claude", "test", threading.Event())
            self.assertEqual(stopped.limit_resume_at, 0)

    def test_actual_loop_resumes_when_deadline_passes(self):
        w = self.worker(limit_resume_at=time.time()-1, error_kind="limit", paused=True,
                        resume_turn={"job":1,"kind":"work"})
        w.state_mark = lambda: None
        w.idle_mark = None
        resumed = []
        original = w.resume_after_limit
        def resume():
            original(); resumed.append(True); w.stop.set()
        w.resume_after_limit = resume
        office = type("OfficeStub", (), {"stop":threading.Event()})()
        w._loop(office)
        self.assertEqual(resumed, [True])
        self.assertFalse(w.paused)
        self.assertEqual(w.retry_due, {"job":1,"kind":"work"})

    def test_checkpoint_accepts_new_fields_and_label(self):
        base = {"read_upto": 1, "carry_msgs": [], "pending_events": [], "handled_events": [], "paused": True, "limit_resume_at": time.time() + 5, "limit_tries": 2, "limit_resume_exact": True}
        self.assertTrue(self.o.checkpoint_valid(base))
        self.assertFalse(self.o.checkpoint_valid(dict(base, limit_resume_at=-1)))
        self.assertFalse(self.o.checkpoint_valid(dict(base, limit_tries="2")))
        at = datetime.datetime(2026, 10, 27, 15, 0).timestamp(); now = datetime.datetime(2026, 10, 27, 9, 0).timestamp()
        self.assertEqual(self.o.limit_resume_label(at, now), "15시")
        self.assertEqual(self.o.limit_resume_label(at + 1800 + 86400, now), "10월 28일 15시 30분")


if __name__ == "__main__":
    unittest.main(verbosity=1)
