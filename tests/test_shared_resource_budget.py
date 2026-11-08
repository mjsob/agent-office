"""CPU·메모리 예산이 Office 전체에 하나인지, Office 서비스가 빠지지 않는지 확인해요.
실행: python3 -I tests/test_shared_resource_budget.py [payload/office.py 경로]
실제 systemd 는 건드리지 않고 임시 폴더와 가짜 명령으로 확인해요."""
import importlib.util, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

SRC = sys.argv.pop(1) if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
TMP = tempfile.TemporaryDirectory(prefix="office-budget-")
for key in list(os.environ):
    if key.startswith("OFFICE_"):
        del os.environ[key]
os.environ.update(OFFICE_DIR=TMP.name, OFFICE_AS="codex", OFFICE_SESSION="test",
                  OFFICE_RESOURCE_LIMITS=str(Path(TMP.name) / "global" / "resource-limits.json"))
spec = importlib.util.spec_from_file_location("office_budget_test", SRC)
o = importlib.util.module_from_spec(spec)
spec.loader.exec_module(o)
o.D.mkdir(parents=True, exist_ok=True)


class Budget(unittest.TestCase):
    def setUp(self):
        self.units = Path(TMP.name) / "units"
        self.units.mkdir(exist_ok=True)
        for f in self.units.rglob("*"):
            if f.is_file():
                f.unlink()
        patcher = patch.object(o, "SYSTEMD_USER_DIR", self.units)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        run = patch.object(o.subprocess, "run", lambda cmd, **kw: self.calls.append(cmd) or o.subprocess.CompletedProcess(cmd, 0, "", ""))
        run.start()
        self.addCleanup(run.stop)

    def test_one_slice_for_every_project(self):
        self.assertEqual(o.resource_unit(), "office-work.slice")
        first = o.resource_unit()
        with patch.object(o, "ROOT", Path("/somewhere/else")):
            self.assertEqual(o.resource_unit(), first)

    def test_limits_are_global_and_inherit_old_project_value(self):
        o.RESOURCE_FILE.unlink(missing_ok=True)
        (o.D / "resource-limits.json").write_text('{"cpu_percent": 55, "memory_high_gb": 4}')
        self.assertEqual(o.resource_limits(), {"cpu_percent": 55, "memory_high_gb": 4})
        o.atomic_json(o.RESOURCE_FILE, {"cpu_percent": 40, "memory_high_gb": 0})
        self.assertEqual(o.resource_limits()["cpu_percent"], 40)

    def test_office_services_are_found_and_adopted(self):
        (self.units / "a.service").write_text("[Service]\nEnvironment=OFFICE_JOB=3\n")
        (self.units / "b.service.d").mkdir()
        (self.units / "b.service.d" / "x.conf").write_text("[Service]\nEnvironment=OFFICE_SESSION=s\n")
        (self.units / "plain.service").write_text("[Service]\nExecStart=/bin/true\n")
        self.assertEqual(o.office_services(), ["a.service", "b.service"])
        with patch.object(o, 'service_properties', return_value={'ActiveState':'inactive'}):
            o.adopt_services()
        for unit in ("a.service", "b.service"):
            self.assertEqual((self.units / (unit + ".d") / o.RESOURCE_DROPIN).read_text(), "[Service]\nSlice=office-work.slice\n")
        self.assertFalse((self.units / "plain.service.d").exists())
        self.assertEqual(self.calls, [["systemctl", "--user", "daemon-reload"]])
        self.calls.clear()
        with patch.object(o, 'service_properties', return_value={'ActiveState':'inactive'}):
            o.adopt_services()   # 이미 들어 있으면 다시 읽지 않아요
        self.assertEqual(self.calls, [])
        self.assertEqual(o.office_services(), ["a.service", "b.service"])   # 우리 drop-in 은 표식으로 세지 않아요

    def test_failed_query_is_not_reported_idle(self):
        (self.units / "learn.service").write_text("[Service]\nEnvironment=OFFICE_JOB=1\n")
        with patch.object(o.subprocess, "run", side_effect=OSError("query failed")):
            self.assertEqual(o.service_budget_state(), [{"unit":"learn.service", "state":"unknown"}])

    def test_status_distinguishes_running_outside_slice(self):
        (self.units / "learn.service").write_text("[Service]\nEnvironment=OFFICE_JOB=1\n")
        outside = "ActiveState=active\nControlGroup=/user.slice/user@1000.service/app.slice/learn.service\n"
        inside = "ActiveState=active\nControlGroup=/user.slice/user@1000.service/office.slice/office-work.slice/learn.service\n"
        for text, state in ((outside, "waiting"), (inside, "inside"), ("ActiveState=inactive\nControlGroup=\n", "idle")):
            with patch.object(o.subprocess, "run", lambda cmd, **kw: o.subprocess.CompletedProcess(cmd, 0, text, "")):
                self.assertEqual(o.service_budget_state(), [{"unit": "learn.service", "state": state}])


if __name__ == "__main__":
    unittest.main()
