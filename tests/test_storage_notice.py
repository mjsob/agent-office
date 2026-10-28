"""상단 용량 알림의 상태와 화면을 확인해요."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
from urllib.request import urlopen

SOURCE = Path(sys.argv.pop(1)) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'payload/office.py'


class StorageNotice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory(prefix='office-storage-tests-')
        cls.env = patch.dict(os.environ, {'OFFICE_DIR': cls.home.name})
        cls.env.start()
        spec = importlib.util.spec_from_file_location('office_storage_test', SOURCE)
        cls.o = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.o)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.home.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='office-storage-notice-')
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name, 'notice.json')
        self.now = 1800000000
        self.data = {'schema_version': 1, 'checked_at': self.now, 'status': 'suggested',
                     'disk': {'vhdx_gb': 147.3, 'used_gb': 118.2, 'c_free_gb': 21.6, 'drive': 'C:'},
                     'reclaim_gib': 9999}

    def view(self, data=None):
        self.file.write_text(json.dumps(self.data if data is None else data))
        return self.o.storage_notice_view(self.file, self.now)

    def test_recomputes_reclaim_from_measurements(self):
        got = self.view()
        self.assertEqual((got['status'], got['reclaim_gib'], got['free_gib'], got['urgent']),
                         ('suggested', 29.1, 21.6, False))

    def test_thresholds_keep_full_precision(self):
        for reclaim, free, expected in [(19.99, 30, 'ok'), (20, 30, 'suggested'),
                                        (10, 29.99, 'suggested'), (9.99, 1, 'ok')]:
            with self.subTest(reclaim=reclaim, free=free):
                self.data['disk'].update(vhdx_gb=120 + reclaim, used_gb=120, c_free_gb=free)
                self.assertEqual(self.view()['status'], expected)

    def test_absent_broken_oversized_and_schema(self):
        self.assertIsNone(self.o.storage_notice_view(self.file, self.now))
        for raw in ['broken', '[]', 'x' * 16385]:
            self.file.write_text(raw)
            self.assertIsNone(self.o.storage_notice_view(self.file, self.now))
        for key, bad in [('schema_version', 2), ('schema_version', True), ('status', 'unknown'),
                         ('checked_at', float('nan')), ('checked_at', self.now + 301)]:
            data = copy.deepcopy(self.data)
            data[key] = bad
            self.assertIsNone(self.view(data))

    def test_invalid_numbers_do_not_break_ui(self):
        for value in [-1, True, None, '123', float('inf'), float('nan')]:
            for key in ['vhdx_gb', 'used_gb', 'c_free_gb']:
                with self.subTest(key=key, value=value):
                    data = copy.deepcopy(self.data)
                    data['disk'][key] = value
                    self.assertIsNone(self.view(data))

    def test_old_and_failed_measurements_omit_numbers(self):
        self.data['checked_at'] -= self.o.STORAGE_NOTICE_MAX_AGE + 1
        self.assertEqual(self.view()['status'], 'stale')
        self.assertNotIn('reclaim_gib', self.view())
        self.data.update(checked_at=self.now, status='unavailable')
        self.assertEqual(self.view(), {'status': 'unavailable', 'checked_at': self.now})

    def test_drive_is_plain_text_and_urgent_is_measured(self):
        self.data['disk'].update(drive='<img src=x>', c_free_gb=14.9)
        got = self.view()
        self.assertEqual(got['drive'], '디스크')
        self.assertTrue(got['urgent'])

    def test_api_updates_notice_even_when_chat_is_unchanged(self):
        self.view()
        office = types.SimpleNamespace(snapshot=lambda *a, **kw: {'unchanged': True, 'rev': 1},
                                       stop=threading.Event(), server_instance='storage-test')
        with patch.object(self.o, 'STORAGE_NOTICE_FILE', self.file), \
                patch.object(self.o.time, 'time', return_value=self.now):
            server = self.o.ThreadingHTTPServer(('127.0.0.1', 0), self.o.make_handler(office))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = f'http://127.0.0.1:{server.server_address[1]}/api/state?since_rev=1'
                with urlopen(url, timeout=3) as response:
                    data = json.load(response)
                self.assertTrue(data['unchanged'])
                self.assertEqual(data['storage_notice']['status'], 'suggested')
                self.data['status'] = 'ok'
                self.view()
                with urlopen(url, timeout=3) as response:
                    self.assertEqual(json.load(response)['storage_notice']['status'], 'ok')
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
