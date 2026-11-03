"""맥 분기가 맞게 도는지 확인해요. 맥이 없어도 돌도록 IS_MAC 만 켜고 이 PC의 ps 로 실행해요.
실행: python3 -I tests/test_mac_support.py [payload/office.py 경로]
open·브라우저는 실행 명령만 확인해요. 실제 맥의 동작을 대신하지는 않아요.
"""
import importlib.util, os, signal, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch

SRC = sys.argv.pop(1) if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
TMP = tempfile.TemporaryDirectory(prefix="office-mac-")
for key in list(os.environ):
    if key.startswith("OFFICE_"):
        del os.environ[key]
os.environ.update(OFFICE_DIR=TMP.name, OFFICE_AS="codex", OFFICE_SESSION="test")
spec = importlib.util.spec_from_file_location("office_mac_test", SRC)
o = importlib.util.module_from_spec(spec)
spec.loader.exec_module(o)
assert o.D.is_relative_to(TMP.name)
o.D.mkdir(parents=True, exist_ok=True)


class Processes(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(o, "IS_MAC", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def spawn(self, code="sleep 30"):
        proc = subprocess.Popen(["bash", "-c", code], start_new_session=True, stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        time.sleep(.1)
        return proc

    def test_identity_matches_record_rules(self):
        proc = self.spawn()
        identity = o.process_identity(proc.pid)
        self.assertEqual(identity["uid"], os.getuid())
        self.assertLess(abs(identity["start"] - time.time()), 5)
        self.assertEqual(identity, o.process_identity(proc.pid))
        self.assertTrue(o.process_record_valid({**identity, "pid": proc.pid, "owner": 1, "owner_start": 1}))
        self.assertIn(b"sleep 30", o.process_command(proc.pid))
        self.assertIsNone(o.process_identity(2**22 + 12345))

    def test_identity_differs_between_commands(self):
        a, b = self.spawn("sleep 31"), self.spawn("sleep 32")
        self.assertNotEqual(o.process_identity(a.pid)["command"], o.process_identity(b.pid)["command"])

    def test_zombie_and_gone(self):
        proc = self.spawn("exit 0")
        time.sleep(.2)
        self.assertEqual(o.process_state(proc.pid), "Z")
        identity = o.process_identity(proc.pid)
        task = {"wait_pid": proc.pid, "wait_start": identity and identity["start"]}
        self.assertIs(o.wait_probe(task)[0], False)
        proc.wait()
        self.assertIsNone(o.process_state(proc.pid))
        self.assertIs(o.wait_probe(task)[0], False)

    def test_wait_probe_alive(self):
        proc = self.spawn()
        identity = o.process_identity(proc.pid)
        self.assertIs(o.wait_probe({"wait_pid": proc.pid, "wait_start": identity["start"]})[0], True)
        self.assertIs(o.wait_probe({"wait_pid": proc.pid, "wait_start": identity["start"] + 5})[0], False)

    def test_started_at(self):
        proc = self.spawn()
        self.assertLess(abs(o.process_started_at(proc.pid) - time.time()), 5)

    def test_group_members(self):
        proc = self.spawn("sleep 30 & sleep 30 & wait")
        members = o.process_group_members(proc.pid)
        self.assertIn(proc.pid, members)
        self.assertEqual(len(members), 3)

    def test_terminate_background(self):
        proc = self.spawn("sleep 30 & wait")
        identity = o.process_identity(proc.pid)
        task = {"wait_pid": proc.pid, "wait_rc": "/x/rc", "wait_cmd": "sleep", "wait_process": identity, "wait_start": identity["start"]}
        self.assertTrue(o.terminate_bg_watch(task))
        proc.wait(timeout=3)
        time.sleep(.2)
        self.assertEqual(o.process_group_members(proc.pid), [])

    def test_login_alive(self):
        proc = self.spawn()
        pidfile = Path(TMP.name) / "pid"
        pidfile.write_text(str(proc.pid))
        self.assertTrue(o.login_alive(pidfile))
        proc.kill()
        proc.wait()
        self.assertFalse(o.login_alive(pidfile))


class LoginScript(unittest.TestCase):
    def paths(self, name):
        fifo, out = Path(TMP.name) / f"{name}.fifo", Path(TMP.name) / f"{name}.out"
        os.mkfifo(fifo)
        return fifo, out

    def test_uses_timeout_when_present(self):
        with patch.object(o.shutil, "which", lambda name: "/usr/bin/timeout"):
            self.assertIn("exec timeout 600 bash -c", o.login_script("/f", "/o", "x"))

    def test_without_timeout_passes_input_and_stops_on_time(self):
        fifo, out = self.paths("slow")
        with patch.object(o.shutil, "which", lambda name: None):
            script = o.login_script(fifo, out, "read line; echo got:$line; sleep 30", seconds=2)
        self.assertNotIn("timeout", script)
        proc = subprocess.Popen(["bash", "-c", script], start_new_session=True, stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        time.sleep(.2)
        with open(fifo, "w") as f:
            f.write("code\n")
        time.sleep(.3)
        self.assertIn("got:code", out.read_text())
        self.assertIsNone(proc.poll())   # 아직 시간이 안 지났어요
        proc.wait(timeout=5)             # 시간이 지나면 끝나요
        self.assertEqual(proc.returncode, -signal.SIGTERM)
        time.sleep(.2)
        self.assertEqual(o.process_group_members(proc.pid), [])   # 그룹에 남은 프로세스가 없어요

    def test_finished_login_leaves_only_the_guard(self):
        fifo, out = self.paths("quick")
        with patch.object(o.shutil, "which", lambda name: None):
            script = o.login_script(fifo, out, "echo done", seconds=2)
        proc = subprocess.Popen(["bash", "-c", script], start_new_session=True, stdin=subprocess.DEVNULL)
        proc.wait(timeout=3)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("done", out.read_text())
        time.sleep(2.4)
        self.assertEqual(o.process_group_members(proc.pid), [])


class Desktop(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(o, "IS_MAC", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        def fake_run(command, **kw):
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        run = patch.object(o.subprocess, "run", fake_run)
        run.start()
        self.addCleanup(run.stop)

    def test_window_prefers_app_mode(self):
        with patch.object(o, "mac_browser_app", lambda: Path("/Applications/Google Chrome.app")):
            self.assertTrue(o.open_window(1234))
        self.assertEqual(self.calls, [["open", "-na", "/Applications/Google Chrome.app", "--args",
                                       "--app=http://localhost:1234/", "--window-size=1500,920"]])

    def test_window_without_browser(self):
        with patch.object(o, "mac_browser_app", lambda: None):
            self.assertTrue(o.open_window(1234))
            self.assertEqual(self.calls, [["open", "http://localhost:1234/"]])
            self.assertFalse(o.open_window(1234, app_only=True))

    def test_open_path_uses_finder(self):
        folder = Path(TMP.name)
        file = folder / "a.txt"
        file.write_text("x")
        self.assertIn("Finder", o.open_report_path(str(folder), "test")["ok"])
        o.open_report_path(f"{file}:12", "test")
        self.assertEqual(self.calls, [["open", str(folder.resolve())], ["open", "-R", str(file.resolve())]])
        with self.assertRaises(o.UserError):
            o.open_report_path("/없는/경로", "test")

    def test_resources_unsupported_without_commands(self):
        o.RESOURCE_CACHE.clear()
        status = o.resource_status(refresh=True)
        self.assertFalse(status["supported"])
        self.assertIn("맥", status["reason"])
        self.assertEqual(self.calls, [])
        self.assertEqual(o.limited_command(["x"]), ["x"])
        with self.assertRaises(o.UserError):
            o.Office().handle({"action": "resource_limits", "cpu_percent": 50, "memory_high_gb": 0})
        o.RESOURCE_CACHE.clear()

    def test_unsupported_actions_blocked(self):
        for act in sorted(o.MAC_BLOCKED_ACTIONS):
            with self.assertRaisesRegex(o.UserError, "맥에서는"):
                o.Office.handle(object.__new__(o.Office), {"action": act, "confirmed": True, "cpu_percent": 50, "memory_high_gb": 0})
        self.assertEqual(self.calls, [])

    def test_features_hide_unsupported(self):
        with patch.object(o, "mac_browser_app", lambda: None), patch.object(o.Path, "home", lambda: Path(TMP.name)):
            self.assertEqual(o.platform_features(), {"mac": True, "storage": False, "resources": False,
                                                     "app_window": False, "claude_limits": False, "powershell": None, "windows_helper": None})
        with patch.object(o, "mac_browser_app", lambda: Path("/Applications/Google Chrome.app")):
            self.assertTrue(o.platform_features()["app_window"])
        self.assertIsNone(o.resource_view())
        with patch.object(o, "IS_MAC", False), patch.object(o, "mac_browser_app", lambda: None):
            features = o.platform_features()
            self.assertEqual((features["storage"], features["resources"], features["app_window"], features["claude_limits"]),
                             (bool(os.environ.get("WSL_DISTRO_NAME")) and o.STORAGE_COMPACT_SCRIPT.is_file(), True, True, True))

    def test_console_drops_unreadable_claude_row(self):
        with patch.object(o.Path, "home", lambda: Path(TMP.name)):
            with patch.object(o, "limits_for", lambda n: {}):
                hidden = o.usage_lines(120)
            with patch.object(o, "limits_for", lambda n: {"five_hour": {"used": 10}, "at": time.time()} if n == "claude" else {}):
                shown = o.usage_lines(120)
        self.assertEqual(len(hidden), len(shown))   # 제자리 갱신이라 줄 수는 그대로예요
        self.assertFalse(any("Claude" in line for line in hidden))
        self.assertTrue(any("Claude" in line for line in shown))

    def test_limit_loop_skips_keychain(self):
        stop = o.threading.Event()
        with patch.object(o.Path, "home", lambda: Path(TMP.name)), patch.object(o, "fetch_claude_limits") as fetch, \
                patch.object(o, "log_limits_failure") as failed, patch.object(stop, "wait", lambda _: stop.set()):
            o.refresh_provider_limits("claude", stop)
        fetch.assert_not_called()
        failed.assert_not_called()

    def test_claude_limits_skip_keychain(self):
        with patch.object(o.Path, "home", lambda: Path(TMP.name)):
            self.assertIsNone(o.fetch_claude_limits())
        self.assertEqual(self.calls, [])

    def test_file_limit_raised(self):
        with patch("resource.getrlimit", lambda _: (256, -1)), patch("resource.RLIM_INFINITY", -1), patch("resource.setrlimit") as set_limit:
            o.raise_mac_file_limit()
        set_limit.assert_called_once()
        self.assertEqual(set_limit.call_args.args[1], (10240, -1))


class Screen(unittest.TestCase):
    def test_paths_are_clickable(self):
        html = (Path(SRC).parent / "office.html").read_text(encoding="utf-8")
        self.assertEqual(html.count("usr|Users|Volumes|private"), 2)
        self.assertIn('s.file_manager || "탐색기"', html)
        self.assertNotIn('title="탐색기에서 열기"', html)   # 맥은 Finder로 보여요
        self.assertRegex(html, r'id="storageSettingsGroup" hidden')

    def test_installer_mac_text(self):
        script = (Path(SRC).parent.parent / "install.sh").read_text(encoding="utf-8")
        self.assertIn("macOS 13.3 이상이 필요해요", script)
        run = subprocess.run(["bash", "-c", 'uname() { echo Darwin; }; ' + script.split("fail() {")[0] + "usage"],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("윈도우", run.stdout)
        self.assertNotIn("WSL에서", run.stdout)
        linux = subprocess.run(["bash", "-c", 'uname() { echo Linux; }; ' + script.split("fail() {")[0] + "usage"],
                               capture_output=True, text=True)
        self.assertIn("--no-shortcut", linux.stdout)


if __name__ == "__main__":
    unittest.main()
