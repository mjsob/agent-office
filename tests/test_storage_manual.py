"""설정에서 직접 줄이기·다시 재기: 추천 기준과 무관한 허용 조건과 예외를 확인해요."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

SOURCE = Path(sys.argv.pop(1)) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'payload/office.py'


class StorageManual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('office_manual_test', SOURCE)
        cls.o = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.o)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='office-manual-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'run-compact.sh'
        self.script.write_text('# mock\n')
        self.notice_file = self.root / 'notice.json'
        self.now = time.time()
        for p in [patch.dict(os.environ, {'WSL_DISTRO_NAME': 'test-only'}),
                  patch.object(self.o, 'refresh_later'),
                  patch.object(self.o, 'STORAGE_COMPACT_SCRIPT', self.script),
                  patch.object(self.o, 'STORAGE_COMPACT_DIR', self.root / 'state'),
                  patch.object(self.o, 'STORAGE_NOTICE_FILE', self.notice_file)]:
            p.start(); self.addCleanup(p.stop)
        self.o.STORAGE_COMPACT_PROBE.clear()
        self.threads = []
        real = threading.Thread
        def new_thread(*a, **kw):
            t = real(*a, **kw); self.threads.append(t); return t
        p = patch.object(self.o.threading, 'Thread', side_effect=new_thread)
        p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: [t.join(3) for t in self.threads if t.ident])

    def write(self, reclaim, free, status='ok', age=0):
        self.notice_file.write_text(json.dumps({
            'schema_version': 1, 'checked_at': self.now - age, 'status': status,
            'disk': {'vhdx_gb': 100 + reclaim, 'used_gb': 100, 'c_free_gb': free, 'drive': 'C:'}}))

    def run_ok(self, *args):
        with patch.object(self.o.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as run:
            self.o.start_storage_compact(*args)
            for t in self.threads: t.join(3)
            return run

    def test_ok_notice_exposes_free_and_reclaim(self):
        self.write(11.7, 36.1)
        view = self.o.storage_notice_view(self.notice_file)
        self.assertEqual((view['status'], view['free_gib'], view['reclaim_gib'], view['drive']), ('ok', 36.1, 11.7, 'C:'))
        self.assertNotIn('urgent', view)

    def test_manual_runs_below_recommendation_but_auto_does_not(self):
        self.write(11.7, 36.1)
        with self.assertRaises(self.o.UserError): self.o.start_storage_compact(True)
        self.assertEqual(self.run_ok(True, True).call_count, 1)

    def test_manual_still_requires_confirmation_positive_reclaim_and_fresh_data(self):
        self.write(11.7, 36.1)
        for confirmed in (False, 'true', 1, None):
            with self.assertRaises(self.o.UserError): self.o.start_storage_compact(confirmed, True)
        self.write(0, 36.1)
        with self.assertRaises(self.o.UserError): self.o.start_storage_compact(True, True)
        self.write(11.7, 36.1, age=self.o.STORAGE_NOTICE_MAX_AGE + 5)
        with self.assertRaises(self.o.UserError): self.o.start_storage_compact(True, True)
        self.notice_file.write_text('broken')
        with self.assertRaises(self.o.UserError): self.o.start_storage_compact(True, True)
        self.write(0, 0, status='unavailable')
        with self.assertRaises(self.o.UserError): self.o.start_storage_compact(True, True)

    def test_manual_must_be_exactly_true(self):
        self.write(11.7, 36.1)
        for manual in ('true', 1, None, False):
            with self.assertRaises(self.o.UserError): self.o.start_storage_compact(True, manual)

    def test_duplicate_manual_run_is_blocked(self):
        self.write(11.7, 36.1)
        self.run_ok(True, True)
        with self.assertRaises(self.o.UserError): self.o.start_storage_compact(True, True)

    def test_remeasure_needs_tool_and_is_rate_limited(self):
        watch = self.root / 'watch.py'
        with patch.object(self.o, 'STORAGE_WATCH_SCRIPT', watch):
            with self.assertRaises(self.o.UserError): self.o.remeasure_storage_notice()
            watch.write_text('# mock\n')
            self.o.STORAGE_NOTICE_REFRESH['at'] = 0.0
            self.write(11.7, 36.1)
            got = self.o.remeasure_storage_notice()
            self.assertTrue(got['storage_notice']['refreshing'])
            self.assertEqual(self.o.refresh_later.call_count, 1)
            self.o.remeasure_storage_notice()   # 30초 안 두 번째는 다시 안 돌려요
            self.assertEqual(self.o.refresh_later.call_count, 1)

    def test_remeasure_refused_while_compacting(self):
        watch = self.root / 'watch.py'; watch.write_text('# mock\n')
        with patch.object(self.o, 'STORAGE_WATCH_SCRIPT', watch), \
             patch.object(self.o, 'storage_compact_view', return_value={'status': 'running'}):
            with self.assertRaises(self.o.UserError): self.o.remeasure_storage_notice()

    def test_fresh_notice_keeps_refreshing_flag_and_blocks_start_during_remeasure(self):
        self.write(11.7, 36.1)
        notice = self.o.storage_notice_view(self.notice_file)
        watch = self.root / 'watch.py'; watch.write_text('# mock\n')
        with patch.object(self.o, 'STORAGE_WATCH_SCRIPT', watch), patch.object(self.o, 'REFRESHING', {'storage_notice'}):
            got = self.o.refresh_storage_notice(notice, {'status': 'idle'})
            self.assertTrue(got['refreshing'])
            with self.assertRaises(self.o.UserError): self.o.start_storage_compact(True, True)
        self.assertNotIn('refreshing', self.o.refresh_storage_notice(notice, {'status': 'idle'}))

    def test_first_measure_in_progress_reports_refreshing(self):
        with patch.object(self.o, 'REFRESHING', {'storage_notice'}):
            self.assertEqual(self.o.refresh_storage_notice(None, {'status': 'idle'}),
                             {'status': 'unavailable', 'refreshing': True})
        self.assertIsNone(self.o.refresh_storage_notice(None, {'status': 'idle'}))

    def test_handle_routes_both_refresh_names_and_manual_flag(self):
        for name in ('storage_refresh', 'storage_remeasure'):
            with patch.object(self.o, 'remeasure_storage_notice', return_value={'ok': name}) as call:
                self.assertEqual(self.o.Office.handle(object(), {'action': name}), {'ok': name})
                call.assert_called_once_with()
        with patch.object(self.o, 'start_storage_compact', return_value={}) as call:
            self.o.Office.handle(object(), {'action': 'storage_compact', 'confirmed': True, 'manual': True})
            call.assert_called_once_with(True, True)
            call.reset_mock()
            self.o.Office.handle(object(), {'action': 'storage_compact', 'confirmed': True})
            call.assert_called_once_with(True, None)


if __name__ == '__main__':
    unittest.main()
