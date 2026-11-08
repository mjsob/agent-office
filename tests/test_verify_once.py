"""검증 재사용이 변경과 실패를 놓치지 않는지 확인해요."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SOURCE = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'tools/verify_once.py'
spec = importlib.util.spec_from_file_location('verify_once', SOURCE)
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)


class Verification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='office-verify-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.input = self.root / 'input.txt'; self.input.write_text('a')
        self.script = self.root / 'check.py'
        self.script.write_text('from pathlib import Path\np=Path("counter");p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n')
        self.cache = self.root / 'cache'
        self.cmd = [sys.executable, str(SOURCE), '--static', '--name', 'sample', '--input', str(self.input), '--input', str(self.script), '--cache-dir', str(self.cache)]
        self.command = [sys.executable, '-I', str(self.script)]

    def run_check(self, extra=(), command=None, env=None, expect=0):
        r = subprocess.run([*self.cmd, *extra, '--', *(command or self.command)], cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, expect, r.stdout + r.stderr)
        return json.loads(r.stdout) if not expect else r

    def count(self):
        return int((self.root / 'counter').read_text())

    def test_success_reuse_and_content_invalidation(self):
        self.assertEqual(self.run_check()['status'], 'passed')
        self.assertEqual(self.run_check()['status'], 'reused'); self.assertEqual(self.count(), 1)
        self.input.write_text('b')
        self.assertEqual(self.run_check()['status'], 'passed'); self.assertEqual(self.count(), 2)

    def test_undeclared_command_script_changes_are_detected(self):
        self.cmd = self.cmd[:self.cmd.index('--input') + 2] + ['--cache-dir', str(self.cache)]
        self.run_check()
        self.script.write_text('raise SystemExit(7)\n')
        self.run_check(expect=7)
        self.assertFalse(list(self.cache.glob('*.json')))

    def test_output_argument_is_not_mistaken_for_input(self):
        output = self.root / 'result.txt'
        self.script.write_text('import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text("ok")\n')
        command = [*self.command, str(output)]
        extra = ['--artifact', str(output)]
        self.assertEqual(self.run_check(extra, command=command)['status'], 'passed')
        self.assertEqual(self.run_check(extra, command=command)['status'], 'reused')

    def test_environment_and_command_invalidation(self):
        self.run_check()
        self.assertEqual(self.run_check(env={**os.environ, 'OFFICE_VERIFY_SAMPLE': 'x'})['status'], 'passed')
        self.assertEqual(self.run_check(command=[sys.executable, str(self.script)])['status'], 'passed')
        self.assertEqual(self.count(), 3)

    def test_artifact_change_and_missing_invalidate(self):
        art = self.root / 'output.txt'
        self.script.write_text(self.script.read_text() + 'Path("output.txt").write_text("ok")\n')
        extra = ['--artifact', str(art)]
        self.run_check(extra); self.assertEqual(self.run_check(extra)['status'], 'reused')
        art.write_text('changed'); self.assertEqual(self.run_check(extra)['status'], 'passed')
        art.unlink(); self.assertEqual(self.run_check(extra)['status'], 'passed'); self.assertEqual(self.count(), 3)

    def test_missing_input_and_missing_artifact_fail(self):
        self.run_check(); self.input.unlink(); self.run_check(expect=2)
        self.input.write_text('a')
        self.run_check(['--artifact', str(self.root / 'not-created')], expect=2)

    def test_failure_never_reuses_old_success(self):
        self.run_check()
        self.script.write_text(self.script.read_text() + 'raise SystemExit(9)\n')
        self.run_check(expect=9); self.run_check(expect=9)
        self.assertEqual(self.count(), 3); self.assertFalse(list(self.cache.glob('*.json')))

    def test_forced_failure_removes_identical_old_success(self):
        self.script.write_text(self.script.read_text() + 'raise SystemExit(8 if Path("fail").exists() else 0)\n')
        self.run_check()
        # 입력 변경 없이 강제 재실행이 실패하는 상황을 재현해요.
        (self.root / 'fail').write_text('1')
        self.run_check(['--force'], expect=8)
        self.assertFalse(list(self.cache.glob('*.json')))

    def test_expiry_future_timestamp_and_corrupt_record(self):
        self.run_check(); record = next(self.cache.glob('*.json'))
        data = json.loads(record.read_text()); data['finished_at'] -= 4000; record.write_text(json.dumps(data))
        self.assertEqual(self.run_check()['status'], 'passed')
        data = json.loads(record.read_text()); data['finished_at'] += 4000; record.write_text(json.dumps(data))
        self.assertEqual(self.run_check()['status'], 'passed')
        record.write_text('{bad'); self.assertEqual(self.run_check()['status'], 'passed')

    def test_input_changes_during_command_are_rejected(self):
        self.script.write_text(self.script.read_text() + 'Path("input.txt").write_text("changed")\n')
        self.run_check(expect=2); self.assertFalse(list(self.cache.glob('*.json')))

    def test_log_corruption_invalidates(self):
        self.run_check(); next(self.cache.glob('*.log')).write_text('tampered')
        self.assertEqual(self.run_check()['status'], 'passed'); self.assertEqual(self.count(), 2)

    def test_timeout_has_no_success_record(self):
        self.script.write_text('import time\ntime.sleep(10)\n')
        self.run_check(['--timeout', '.1'], expect=124)
        self.assertFalse(list(self.cache.glob('*.json')))

    def test_concurrent_identical_checks_run_only_once(self):
        self.script.write_text('import time\ntime.sleep(.2)\n' + self.script.read_text())
        cmd = [*self.cmd, '--', *self.command]
        first = subprocess.Popen(cmd, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen(cmd, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        a, ae = first.communicate(timeout=10); b, be = second.communicate(timeout=10)
        self.assertEqual((first.returncode, second.returncode), (0, 0), (ae, be))
        self.assertEqual({json.loads(a)['status'], json.loads(b)['status']}, {'passed', 'reused'})
        self.assertEqual(self.count(), 1)

    def test_directory_symlink_and_cycle(self):
        directory = self.root / 'inputs'; directory.mkdir(); (directory / 'x').write_text('1')
        link = self.root / 'link'; link.symlink_to(directory, target_is_directory=True)
        before = v.digest_path(link); (directory / 'x').write_text('2'); self.assertNotEqual(before, v.digest_path(link))
        (directory / 'loop').symlink_to(directory, target_is_directory=True)
        with self.assertRaises(ValueError): v.digest_path(link)

    def test_cache_inside_declared_input_rejected(self):
        self.run_check(['--input', str(self.root)], expect=2)

    def test_mac_has_no_linux_only_runtime_dependency(self):
        with patch.object(v.platform, 'platform', return_value='macOS-test'):
            self.assertEqual(v.fingerprint([self.input], self.command)['runtime'][1], 'macOS-test')
        self.assertNotIn('/proc', SOURCE.read_text())


if __name__ == '__main__':
    unittest.main()
