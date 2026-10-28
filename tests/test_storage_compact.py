"""디스크 실행 버튼의 확인·중복 방지·실패 복구를 모의 실행으로 확인해요."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SOURCE = Path(sys.argv.pop(1)) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'payload/office.py'


class StorageCompact(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('office_compact_test', SOURCE)
        cls.o = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.o)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='office-compact-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.o.STORAGE_COMPACT_PROBE.clear()
        self.probe_refresh = patch.object(self.o, 'refresh_later')
        self.probe_refresh.start(); self.addCleanup(self.probe_refresh.stop)
        self.script = self.root / 'run-compact.sh'
        self.script.write_text('# mock script\n')
        self.env = patch.dict(os.environ, {'WSL_DISTRO_NAME': 'test-only'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.patches = [patch.object(self.o, 'STORAGE_COMPACT_SCRIPT', self.script),
                        patch.object(self.o, 'STORAGE_COMPACT_DIR', self.root / 'state'),
                        patch.object(self.o, 'storage_notice_view', return_value={'status': 'suggested'})]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)
        self.threads = []
        real_thread = threading.Thread
        def new_thread(*a, **kw):
            thread = real_thread(*a, **kw)
            self.threads.append(thread)
            return thread
        p = patch.object(self.o.threading, 'Thread', side_effect=new_thread)
        p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.join)

    def join(self):
        for thread in self.threads:
            if thread.ident:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())

    def record(self, name, status, **extra):
        self.o.STORAGE_COMPACT_DIR.mkdir(exist_ok=True)
        path = self.o.STORAGE_COMPACT_DIR / name
        data = dict(schema_version=1, status=status, updated_at=time.time(), request_id='sample', **extra)
        path.write_text(json.dumps(data))
        return data

    def test_confirmation_must_be_boolean_true(self):
        for v in [None, False, 1, 'true']:
            with self.assertRaises(self.o.UserError):
                self.o.start_storage_compact(v)
        self.assertFalse(self.o.STORAGE_COMPACT_DIR.exists())

    def test_missing_script_and_non_wsl_do_not_launch(self):
        with patch.object(self.o.subprocess, 'run') as runner:
            with patch.dict(os.environ, {'WSL_DISTRO_NAME': ''}):
                with self.assertRaises(self.o.UserError): self.o.start_storage_compact(True)
            self.script.unlink()
            with self.assertRaises(self.o.UserError): self.o.start_storage_compact(True)
            runner.assert_not_called()

    def test_stale_notice_rejects_execution(self):
        for value in [None, {'status': 'stale'}, {'status': 'unavailable'}, {'status': 'ok'}]:
            with patch.object(self.o, 'storage_notice_view', return_value=value), patch.object(self.o.subprocess, 'run') as runner:
                with self.assertRaises(self.o.UserError): self.o.start_storage_compact(True)
                runner.assert_not_called()

    def test_concurrent_click_launches_only_once_and_uses_fixed_path(self):
        entered, finish = threading.Event(), threading.Event()
        def run(*a, **kw):
            entered.set()
            finish.wait(2)
            return types.SimpleNamespace(returncode=0)
        with patch.object(self.o.subprocess, 'run', side_effect=run) as runner:
            reply = self.o.Office.handle(object(), {'action': 'storage_compact', 'confirmed': True,
                                                   'script': '/tmp/untrusted.sh', 'session': 'bad'})
            self.assertIn('storage_compact', reply)
            self.assertTrue(entered.wait(1))
            with self.assertRaises(self.o.UserError): self.o.start_storage_compact(True)
            finish.set(); self.join()
            self.assertEqual(runner.call_count, 1)
            args, kw = runner.call_args
            self.assertEqual(args[0], ['bash', str(self.script)])
            self.assertEqual(kw['stdin'], subprocess.DEVNULL)
            self.assertIn('WSL_COMPACT_REQUEST_ID', kw['env'])
            self.assertEqual(self.o.storage_compact_view()['status'], 'launched')
            with self.assertRaises(self.o.UserError): self.o.start_storage_compact(True)

    def test_failed_launch_can_retry(self):
        with patch.object(self.o.subprocess, 'run', return_value=types.SimpleNamespace(returncode=1)):
            self.o.start_storage_compact(True); self.join()
            self.assertEqual(self.o.storage_compact_view()['status'], 'failed')
            self.assertTrue(self.o.storage_compact_view()['can_start'])

    def test_timeout_stays_uncertain_to_prevent_duplicate(self):
        with patch.object(self.o.subprocess, 'run', side_effect=subprocess.TimeoutExpired('mock', 120)):
            self.o.start_storage_compact(True); self.join()
            self.assertEqual(self.o.storage_compact_view()['status'], 'uncertain')
            self.assertFalse(self.o.storage_compact_view()['can_start'])

    def test_external_state_matches_request_and_survives_reload(self):
        self.record('office-launch.json', 'launched')
        self.record('status.json', 'countdown')
        self.assertEqual(self.o.storage_compact_view()['status'], 'countdown')
        self.record('status.json', 'completed')
        self.assertTrue(self.o.storage_compact_view()['can_start'])
        data = self.record('status.json', 'cancelled')
        data['request_id'] = 'older-request'
        (self.o.STORAGE_COMPACT_DIR / 'status.json').write_text(json.dumps(data))
        self.assertEqual(self.o.storage_compact_view()['status'], 'launched')
        self.assertFalse(self.o.storage_compact_view()['can_start'])

    def test_bad_or_old_active_state_cannot_repeat(self):
        self.record('office-launch.json', 'running')
        data = json.loads((self.o.STORAGE_COMPACT_DIR / 'office-launch.json').read_text())
        data['updated_at'] -= 4000
        (self.o.STORAGE_COMPACT_DIR / 'office-launch.json').write_text(json.dumps(data))
        self.assertEqual(self.o.storage_compact_view()['status'], 'uncertain')
        (self.o.STORAGE_COMPACT_DIR / 'office-launch.json').write_text('broken')
        self.assertFalse(self.o.storage_compact_view()['can_start'])

    def test_closed_countdown_window_recovers_only_with_fresh_negative_probe(self):
        self.record('office-launch.json', 'launched')
        data = self.record('status.json', 'countdown')
        data['updated_at'] -= 20
        (self.o.STORAGE_COMPACT_DIR / 'status.json').write_text(json.dumps(data))
        self.assertFalse(self.o.storage_compact_view()['can_start'])
        for running, expected in [(True, False), (None, False), (False, True)]:
            self.o.STORAGE_COMPACT_PROBE.update(request_id='sample', at=time.time(), running=running)
            self.assertEqual(self.o.storage_compact_view()['can_start'], expected)
        self.o.STORAGE_COMPACT_PROBE.update(request_id='other', at=time.time(), running=False)
        self.assertFalse(self.o.storage_compact_view()['can_start'])
        self.o.STORAGE_COMPACT_PROBE.update(request_id='sample', at=time.time()-60, running=False)
        self.assertFalse(self.o.storage_compact_view()['can_start'])

    def test_uncertain_and_interrupted_preparation_can_recover(self):
        for status in ['uncertain', 'launched', 'running', 'preparing']:
            data = self.record('office-launch.json', status)
            data['updated_at'] -= 200
            (self.o.STORAGE_COMPACT_DIR / 'office-launch.json').write_text(json.dumps(data))
            self.o.STORAGE_COMPACT_PROBE.update(request_id='sample', at=time.time(), running=False)
            view = self.o.storage_compact_view()
            self.assertEqual(view['status'], 'cancelled')
            self.assertTrue(view['can_start'])

    def test_probe_reads_only_boolean_success_and_never_launches(self):
        for result, expected in [(types.SimpleNamespace(returncode=0, stdout='{"running":false}'), False),
                                 (types.SimpleNamespace(returncode=0, stdout='{"running":true}'), True),
                                 (types.SimpleNamespace(returncode=0, stdout='{"running":0}'), None),
                                 (types.SimpleNamespace(returncode=1, stdout='{"running":false}'), None),
                                 (types.SimpleNamespace(returncode=0, stdout='broken'), None)]:
            with patch.object(self.o.subprocess, 'run', return_value=result) as runner:
                self.o.probe_storage_compact('sample')
                self.assertIs(self.o.STORAGE_COMPACT_PROBE['running'], expected)
                self.assertEqual(runner.call_args.args[0], ['bash', str(self.script), '--status'])

    def test_real_http_rejects_foreign_origin_and_returns_compact_state(self):
        office = types.SimpleNamespace(snapshot=lambda *a, **kw: {'unchanged': True, 'rev': 1},
                                       stop=threading.Event(), server_instance='test',
                                       handle=lambda p: self.o.Office.handle(object(), p))
        server = self.o.ThreadingHTTPServer(('127.0.0.1', 0), self.o.make_handler(office))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_address[1]}'
        try:
            request = Request(url + '/api', data=json.dumps({'action': 'storage_compact', 'confirmed': True}).encode(),
                              headers={'Origin': 'https://foreign.example'})
            with patch.object(self.o.subprocess, 'run') as runner:
                with self.assertRaises(HTTPError) as caught: urlopen(request, timeout=2)
                self.assertEqual(caught.exception.code, 403)
                caught.exception.close()
                runner.assert_not_called()
                with urlopen(url + '/api/state?since_rev=1', timeout=2) as response:
                    data = json.load(response)
                self.assertEqual(data['storage_compact']['status'], 'idle')
                self.assertTrue(data['unchanged'])
                self.assertTrue(data['storage_compact']['can_start'])
                runner.assert_not_called()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == '__main__': unittest.main()
