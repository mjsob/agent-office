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
        self.data = {'schema_version': 2, 'checked_at': self.now, 'status': 'suggested',
                     'disk': {'vhdx_gb': 164.3, 'used_gb': 118.2, 'c_free_gb': 21.6, 'drive': 'C:'},
                     'reclaim_gib': 12.1, 'reclaim_basis': 'last_compact', 'urgent': False}

    def view(self, data=None):
        self.file.write_text(json.dumps(self.data if data is None else data))
        return self.o.storage_notice_view(self.file, self.now)

    def test_uses_producer_estimate_without_rebuilding_old_difference(self):
        got = self.view()
        self.assertEqual((got['status'], got['reclaim_gib'], got['free_gib'], got['urgent']),
                         ('suggested', 12.1, 21.6, False))

    def test_actual_post_compaction_does_not_suggest_another_11_gib(self):
        self.data['disk'].update(vhdx_gb=140.8701171875, used_gb=129.43646621704102,
                                 metadata_gib=17.146068572998047, c_free_gb=24.721294403076172)
        self.data.update(status='ok', reclaim_gib=0)
        got = self.view()
        self.assertEqual((got['status'], got['reclaim_gib']), ('ok', 0))

    def test_legacy_is_stale_and_refreshes_without_exposing_candidate(self):
        self.data['schema_version'] = 1
        got = self.view()
        self.assertEqual(got, {'status': 'stale', 'checked_at': self.now})
        with patch.object(self.o, 'STORAGE_WATCH_SCRIPT', self.file), \
                patch.object(self.o.time, 'time', return_value=self.now), \
                patch.object(self.o, 'refresh_later') as refresh:
            self.o.STORAGE_NOTICE_REFRESH['at'] = 0
            self.assertTrue(self.o.refresh_storage_notice(got, {'status': 'idle'})['refreshing'])
            refresh.assert_called_once()

    def test_unknown_estimate_preserves_free_space_but_never_suggests(self):
        self.data.update(status='ok', reclaim_gib=None, reclaim_basis=None)
        got = self.view()
        self.assertIsNone(got['reclaim_gib'])
        self.assertEqual((got['free_gib'], got['status']), (21.6, 'ok'))
        self.data['status'] = 'suggested'
        self.assertIsNone(self.view())

    def test_producer_status_and_urgent_are_not_recomputed(self):
        self.data.update(status='ok', reclaim_gib=29.1)
        self.data['disk']['c_free_gb'] = 1
        self.assertEqual(self.view()['status'], 'ok')
        self.data.update(status='suggested', urgent=False)
        self.assertFalse(self.view()['urgent'])
        self.data['urgent'] = True
        self.assertTrue(self.view()['urgent'])

    def test_invalid_estimate_and_basis_do_not_fall_back_to_old_difference(self):
        for value in [-1, True, '11.4', float('inf'), float('nan'), 9999]:
            self.data['reclaim_gib'] = value
            self.assertIsNone(self.view())
        self.data.update(reclaim_gib=12.1, reclaim_basis='old')
        self.assertIsNone(self.view())

    def test_absent_broken_oversized_and_schema(self):
        self.assertIsNone(self.o.storage_notice_view(self.file, self.now))
        for raw in ['broken', '[]', 'x' * 16385]:
            self.file.write_text(raw)
            self.assertIsNone(self.o.storage_notice_view(self.file, self.now))
        for key, bad in [('schema_version', 3), ('schema_version', True), ('status', 'unknown'),
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
        self.data['urgent'] = True
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
