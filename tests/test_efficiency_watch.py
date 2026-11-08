import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('watch', Path(__file__).resolve().parents[1] / 'tools/efficiency_watch.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / '.office/sessions'
        self.state = dict(exclude='monitor', sessions={}, queue=[], first_pending_at=None, bootstrapping=True)

    def put(self, sid, **data):
        path = self.folder / sid / 'state.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def baseline(self):
        w.scan(self.root, self.state, 1)
        self.state.pop('bootstrapping')

    def test_baseline_self_exclusion_and_unchanged(self):
        self.put('other', tasks=[dict(id=1, status='working')])
        self.put('monitor', tasks=[dict(id=244, status='working')])
        self.baseline()
        self.assertEqual(set(self.state['sessions']), {'other'})
        self.assertEqual(w.scan(self.root, self.state, 2), 0)
        self.assertEqual(self.state['queue'], [])

    def test_progress_noise_and_real_wait_transition(self):
        self.put('other', tasks=[dict(id=1, status='working', progress={'done': 1})])
        self.baseline()
        self.put('other', tasks=[dict(id=1, status='working', progress={'done': 2})], msgs=[dict(id=2, kind='progress', text='2/9')])
        self.assertEqual(w.scan(self.root, self.state, 2), 0)
        self.put('other', tasks=[dict(id=1, status='waiting', wait_pid=22)])
        self.assertEqual(w.scan(self.root, self.state, 3), 1)

    def test_new_session_correction_and_turn(self):
        self.baseline()
        self.put('new', tasks=[dict(id=1, status='done', metrics={'corrections': 1})], msgs=[dict(id=2, kind='turn', text='actual result')])
        self.assertEqual(w.scan(self.root, self.state, 2), 2)
        self.assertEqual(w.scan(self.root, self.state, 3), 0)
        self.put('new', tasks=[dict(id=1, status='done', metrics={'corrections': 2})])
        self.assertEqual(w.scan(self.root, self.state, 4), 1)

    def test_parse_failure_retry(self):
        path = self.put('other', tasks=[dict(id=1, status='working')])
        self.baseline()
        path.write_text('{')
        self.assertEqual(w.scan(self.root, self.state, 2), 0)
        self.assertEqual(len(self.state['errors']), 1)
        self.put('other', tasks=[dict(id=1, status='done')])
        self.assertEqual(w.scan(self.root, self.state, 3), 1)
        self.assertEqual(self.state['errors'], [])

    def test_batch_persists_across_restart_and_does_not_overwrite(self):
        self.baseline()
        self.put('other', tasks=[dict(id=1, status='done')])
        w.scan(self.root, self.state, 2)
        self.assertFalse(w.publish(self.root, self.state, 10, 15))
        self.assertTrue(w.publish(self.root, self.state, 17, 15))
        pending = self.state['pending'].copy()
        self.state = json.loads(json.dumps(self.state))
        self.put('other', tasks=[dict(id=1, status='waiting')])
        w.scan(self.root, self.state, 20)
        self.assertTrue(w.publish(self.root, self.state, 20, 15))
        self.assertEqual(self.state['pending'], pending)
        self.assertEqual(len(w.load(Path(pending['path']))['events']), 1)
        self.assertEqual(len(self.state['queue']), 2)

    def test_archive_mutation_is_quiet_but_new_message_is_collected(self):
        self.put('other', msgs=[dict(id=1, kind='turn', text='x' * 900)])
        self.baseline()
        self.put('other', msgs=[dict(id=1, kind='turn', text='x' * 300, archived=True, resolved=True),
                                dict(id=2, kind='msg', text='new')])
        self.assertEqual(w.scan(self.root, self.state, 2), 1)
        self.assertEqual(self.state['queue'][0]['key'], 'msg:2')

    def test_nested_progress_and_efficiency_fields(self):
        self.put('other', tasks=[dict(id=1, status='waiting', progress={'state': 'waiting'})],
                 jobs=[dict(id=1, rework_count=0)])
        self.baseline()
        self.put('other', tasks=[dict(id=1, status='waiting', progress={'state': 'working'},
                                     handoffs=[{'to': 'claude'}], wait_overdue_count=1)],
                 jobs=[dict(id=1, rework_count=1)])
        self.assertEqual(w.scan(self.root, self.state, 2), 2)
        task = self.state['queue'][0]['record']
        self.assertEqual(task['progress_state'], 'working')
        self.assertEqual(task['wait_overdue_count'], 1)
        self.assertEqual(task['handoffs'], [{'to': 'claude'}])

    def test_continuation_chain_and_outgoing_notice_are_excluded(self):
        self.baseline()
        self.put('a-child', continued_from={'session': 'monitor', 'job': 1},
                 msgs=[dict(id=1, kind='turn', text='self')])
        self.put('a-grandchild', continued_from={'session': 'a-child', 'job': 1},
                 tasks=[dict(id=1, status='working')])
        self.put('other', msgs=[dict(id=1, kind='msg', **{'from': 'codex'},
                                    text=w.NOTICE_PREFIX + ' suggestion')])
        self.assertEqual(w.scan(self.root, self.state, 2), 0)
        self.assertIn('a-grandchild', self.state['excluded_sessions'])
        self.put('other', msgs=[dict(id=2, kind='msg', **{'from': 'claude'}, text='reply')])
        self.assertEqual(w.scan(self.root, self.state, 3), 1)

    def test_persistent_error_notifies_once_then_recovery_resets(self):
        path = self.put('other', tasks=[])
        self.baseline()
        path.write_text('{')
        for now in range(2, 5):
            w.scan(self.root, self.state, now)
        self.assertEqual(len(self.state['queue']), 1)
        self.assertEqual(self.state['queue'][0]['key'], 'read_error')
        for now in range(5, 9):
            w.scan(self.root, self.state, now)
        self.assertEqual(len(self.state['queue']), 1)
        self.put('other', tasks=[])
        w.scan(self.root, self.state, 9)
        path.write_text('{')
        for now in range(10, 13):
            w.scan(self.root, self.state, now)
        self.assertEqual(len(self.state['queue']), 2)

    def test_v1_cursor_upgrade_preserves_real_changes_without_schema_noise(self):
        self.put('other', tasks=[dict(id=1, status='working', kind='general'),
                                 dict(id=2, status='done')], jobs=[dict(id=1, lead='codex')])
        self.baseline()
        for key, row in w.records(w.load(self.folder / 'other/state.json')).items():
            fields = w.V2_TASK_FIELDS if key.startswith('task:') else w.V2_JOB_FIELDS
            self.state['sessions']['other']['hashes'][key] = w.digest({k: v for k, v in row.items() if k not in fields})
        self.state['version'] = 1
        self.put('other', tasks=[dict(id=1, status='working', kind='general'),
                                 dict(id=2, status='waiting')], jobs=[dict(id=1, lead='codex')])
        self.assertEqual(w.scan(self.root, self.state, 2), 1)
        self.assertEqual(self.state['queue'][0]['key'], 'task:2')
        self.assertEqual(self.state['version'], 2)


if __name__ == '__main__':
    unittest.main()
