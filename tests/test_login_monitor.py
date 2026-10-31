"""로그인 창을 연 뒤 성공·실패·만료를 자동으로 확인하는지 살펴봐요.
실행하려면 python3 -I tests/test_login_monitor.py [payload/office.py 경로]를 입력하세요.
실제 로그인 대신 가짜 시계와 로그인 상태를 사용해요.
"""
import importlib.util
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

SOURCE = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and sys.argv[1].endswith('.py') else Path(__file__).resolve().parents[1] / 'payload' / 'office.py'


class ClockStop:
    def __init__(self):
        self.now = 1000.0
        self.stopped = False
        self.waits = 0

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.now += seconds
        self.waits += 1
        if self.waits > 700:
            raise AssertionError('로그인 감시가 끝나지 않아요.')
        return self.stopped


class LoginMonitor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='office-login-monitor-')
        self.addCleanup(self.tmp.cleanup)
        env = os.environ.copy()
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        for key in list(os.environ):
            if key.startswith('OFFICE_'):
                del os.environ[key]
        os.environ.update(OFFICE_DIR=self.tmp.name, OFFICE_SESSION='test', OFFICE_AS='codex', OFFICE_JOB='1')
        spec = importlib.util.spec_from_file_location('login_monitor_target', SOURCE)
        self.o = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.o)
        self.clock = ClockStop()
        self.o.time = types.SimpleNamespace(time=lambda: self.clock.now)
        self.o.server_log = lambda *args: None
        self.o.login_paths = lambda name: (None, None, name)
        self.alive_until = {'codex': 2000, 'claude': 2000}
        self.ok_after = {'codex': float('inf'), 'claude': float('inf')}
        self.checks, self.notices, self.resumed, self.started = [], [], [], []
        self.o.login_alive = lambda name: self.clock.now < self.alive_until[name]
        self.o.login_ok = self.check_ok
        self.o.login_start = lambda name: self.started.append(name) or 'https://example.invalid/login'
        self.office = object.__new__(self.o.Office)
        self.office.stop = self.clock
        self.office.login_lock = threading.RLock()
        self.office.login_pending = {'codex'}
        self.office.login_watching = True
        self.office.login_sid = {'codex': 'original', 'claude': 'original'}
        self.worker = types.SimpleNamespace(name='codex', sid='original')
        self.office.teams = {'original': {'w': {'codex': self.worker}}}
        self.office.login_post = lambda sid, title, body: self.notices.append((sid, title, body))
        self.office.login_resume_worker = lambda w: self.resumed.append(w)

    def check_ok(self, name):
        self.checks.append((name, self.clock.now))
        return self.clock.now >= self.ok_after[name]

    def run_watch(self):
        self.office.watch_login('test')

    def test_live_login_process_does_not_hide_success(self):
        self.ok_after['codex'] = 1012
        self.run_watch()
        self.assertLessEqual(self.clock.now, 1018)
        self.assertEqual(self.resumed, [self.worker])
        self.assertEqual(len(self.notices), 1)
        self.assertIn('로그인 완료', self.notices[0][1])
        self.assertEqual(self.notices[0][0], 'original')
        self.assertFalse(self.office.login_pending)
        self.assertFalse(self.office.login_watching)

    def test_status_is_rechecked_while_waiting_without_busy_polling(self):
        self.ok_after['codex'] = 1021
        self.run_watch()
        checks = [at for name, at in self.checks if name == 'codex']
        self.assertGreaterEqual(len(checks), 4)
        self.assertLessEqual(max(checks), 1026)
        self.assertTrue(all(4 <= b - a <= 5 for a, b in zip(checks, checks[1:])), checks)

    def test_failed_process_reports_once_without_resuming(self):
        self.alive_until['codex'] = 1002
        self.run_watch()
        self.assertEqual(self.resumed, [])
        self.assertEqual(len(self.notices), 1)
        self.assertIn('로그인 안 됨', self.notices[0][1])
        self.assertFalse(self.office.login_pending)
        self.assertEqual(self.started, [])

    def test_expired_link_stops_monitoring_without_reissuing(self):
        self.alive_until['codex'] = 1600
        self.run_watch()
        self.assertGreaterEqual(self.clock.now, 1600)
        self.assertLess(self.clock.now, 1630)
        self.assertEqual(self.resumed, [])
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.started, [])
        self.assertFalse(self.office.login_pending)

    def test_one_agent_success_does_not_wait_for_the_other(self):
        self.office.login_pending.add('claude')
        peer = types.SimpleNamespace(name='claude', sid='original')
        self.office.teams['original']['w']['claude'] = peer
        self.ok_after.update(codex=1003, claude=1017)
        self.run_watch()
        self.assertEqual(self.resumed, [self.worker, peer])
        self.assertEqual(len(self.notices), 2)
        self.assertLess(self.clock.now, 1025)

    def test_process_exit_success_is_detected_immediately(self):
        self.alive_until['codex'] = 1002
        self.ok_after['codex'] = 1002
        self.run_watch()
        self.assertLessEqual(self.clock.now, 1004)
        self.assertEqual(self.resumed, [self.worker])

    def test_success_reaches_all_loaded_chats(self):
        second = types.SimpleNamespace(name='codex', sid='second')
        self.office.teams['second'] = {'w': {'codex': second}}
        self.ok_after['codex'] = 1000
        self.run_watch()
        self.assertEqual(self.resumed, [self.worker, second])
        self.assertEqual(len(self.notices), 1)

    def test_stopped_office_does_not_poll_or_resume(self):
        self.clock.stopped = True
        self.run_watch()
        self.assertEqual(self.checks, [])
        self.assertEqual(self.resumed, [])
        self.assertEqual(self.notices, [])


if __name__ == '__main__':
    unittest.main(verbosity=1)
