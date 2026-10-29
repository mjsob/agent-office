"""진행 멈춤 오탐을 줄이는 판정을 확인해요: 다른 요청 차례 중인 담당, 순서 대기, 알림 뒤 오탐 표시.
실행: python3 -I tests/test_stall_busy.py [payload/office.py 경로]
서버를 실행하지 않고 임시 폴더와 가짜 담당만 써요."""
import copy, importlib.util, json, os, sys, tempfile, threading, time, types, unittest
from pathlib import Path
from unittest.mock import patch
SOURCE = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else Path(__file__).resolve().parents[1] / "payload" / "office.py"


class StallBusy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="office-stall-busy-"); self.addCleanup(self.tmp.cleanup)
        env = os.environ.copy(); self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        for key in list(os.environ):
            if key.startswith("OFFICE_"):
                del os.environ[key]
        os.environ.update(OFFICE_DIR=self.tmp.name, OFFICE_SESSION="test", OFFICE_AS="codex", OFFICE_JOB="1")
        spec = importlib.util.spec_from_file_location("stall_busy_target", SOURCE)
        self.o = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.o)
        self.logs = []; self.o.server_log = lambda event, error=None: self.logs.append(event)
        self.o.meta_view = lambda: {"sessions": [{"id": "test", "deleted": False}]}
        self.path = self.o.sdir("test") / "state.json"; self.now = time.time()

    def write(self, jobs, tasks):
        self.o.atomic_json(self.path, dict(jobs=copy.deepcopy(jobs), tasks=tasks, msgs=[], seq=10), self.o.state_valid)

    def job(self, i, lead="claude"):
        return dict(id=i, lead=lead, status="working", text=f"j{i}")

    def task(self, i, job, owner, status="doing"):
        return dict(id=i, job=job, owner=owner, status=status, title=f"t{i}")

    def team(self, codex_doing, claude_doing, codex_status="working", claude_status="idle"):
        codex = types.SimpleNamespace(name="codex", status=codex_status, doing=codex_doing, paused=False, error=None, since=self.now)
        claude = types.SimpleNamespace(name="claude", status=claude_status, doing=claude_doing, paused=False, error=None, since=self.now)
        team = {"stop": threading.Event(), "w": {"codex": codex, "claude": claude}}
        return types.SimpleNamespace(teams={"test": team}), team, codex

    def run_checks(self, fake, *offsets):
        for offset in offsets:
            with patch.object(self.o.time, "time", return_value=self.now + offset):
                self.o.Office.check_stall(fake, "test")

    def alerts(self):
        return [m for m in json.loads(self.path.read_text())["msgs"] if "진행 멈춤" in m["text"]]

    def test_owner_working_on_other_job_turn_is_busy(self):
        # Codex 차례는 요청 2인데 요청 1의 진행 중 작업 #1을 갖고 일하는 중 → 멈춤이 아니에요
        self.write([self.job(1), self.job(2)], [self.task(1, 1, "codex"), self.task(2, 2, "codex")])
        fake, _, _ = self.team({"job": 2}, None)
        self.run_checks(fake, 0, self.o.STALL_NOTICE + 10, 2 * self.o.STALL_NOTICE + 20)
        self.assertEqual(self.alerts(), [])

    def test_real_stall_still_alerts_and_logs_details(self):
        self.write([self.job(1)], [self.task(1, 1, "codex")])
        fake, _, _ = self.team(None, None, codex_status="idle")
        self.run_checks(fake, 0, self.o.STALL_NOTICE + 10)
        self.assertEqual(len(self.alerts()), 1)
        self.assertTrue(any(e.startswith("job.stall.nudge job=1 left=1 owners=codex:doing@None") for e in self.logs), self.logs)

    def test_queued_behind_lead_turn_is_not_a_stall(self):
        # 리더(Claude)가 요청 2 차례 중이고 요청 1은 작업 0건 → 순서 대기라 알림 없음
        self.write([self.job(1), self.job(2)], [])
        fake, _, _ = self.team(None, {"job": 2}, codex_status="idle", claude_status="working")
        self.run_checks(fake, 0, self.o.STALL_NOTICE + 10)
        self.assertEqual(self.alerts(), [])

    def test_empty_job_wording_when_lead_idle(self):
        self.write([self.job(1)], [])
        fake, _, _ = self.team(None, None, codex_status="idle")
        self.run_checks(fake, 0, self.o.STALL_NOTICE + 10)
        self.assertIn("작업 0건", self.alerts()[0]["text"])

    def test_finished_tasks_wording(self):
        self.write([self.job(1)], [self.task(1, 1, "codex", "done")])
        # 완료 작업만 있는 요청: 남은 작업 목록은 비고 문구는 '전부 완료'
        fake, _, _ = self.team(None, None, codex_status="idle", claude_status="idle")
        self.run_checks(fake, 0, self.o.STALL_NOTICE + 10)
        self.assertIn("전부 완료", self.alerts()[0]["text"])

    def test_false_alarm_logged_when_task_finishes_soon_after(self):
        self.write([self.job(1)], [self.task(1, 1, "codex"), self.task(2, 1, "codex", "todo")])
        fake, _, _ = self.team(None, None, codex_status="idle")
        self.run_checks(fake, 0, self.o.STALL_NOTICE + 10)
        self.assertEqual(len(self.alerts()), 1)
        self.write([self.job(1)], [self.task(2, 1, "codex", "todo")])   # 알림 뒤 작업 하나가 끝남
        self.run_checks(fake, self.o.STALL_NOTICE + 60)
        self.assertTrue(any(e.startswith("job.stall.false_alarm job=1") for e in self.logs), self.logs)


if __name__ == "__main__":
    unittest.main(verbosity=1)
