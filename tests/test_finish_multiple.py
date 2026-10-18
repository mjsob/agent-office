"""한 보고로 여러 요청을 마감해도 미완료 요청이 섞이지 않는지 확인해요."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE=Path(sys.argv.pop(1)).resolve() if len(sys.argv)>1 and not sys.argv[1].startswith('-') else Path(__file__).resolve().parents[1]/'payload/office.py'


class FinishMultiple(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='office-finish-multiple-')
        self.addCleanup(self.tmp.cleanup)
        self.before_env=os.environ.copy()
        self.addCleanup(lambda:(os.environ.clear(),os.environ.update(self.before_env)))
        for k in list(os.environ):
            if k.startswith('OFFICE_'):del os.environ[k]
        os.environ.update(OFFICE_DIR=self.tmp.name,OFFICE_SESSION='test',OFFICE_AS='codex',OFFICE_JOB='1')
        spec=importlib.util.spec_from_file_location('finish_test_target',SOURCE)
        self.o=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.o)
        self.path=self.o.sdir('test')/'state.json'
        self.jobs=[dict(id=i,status='working',lead='codex',text=f'question-{i}') for i in (1,2,3)]
        self.write()

    def write(self,jobs=None,tasks=None):
        self.o.atomic_json(self.path,dict(jobs=copy.deepcopy(jobs or self.jobs),tasks=tasks or [],msgs=[],seq=0),self.o.state_valid)

    def read(self):return json.loads(self.path.read_text())
    def finish(self,also=None):self.o.cmd_finish(argparse.Namespace(text='함께 해결했어.\n두 번째 줄',also=also))

    def reject(self,also):
        before=self.path.read_bytes()
        with self.assertRaises((SystemExit,self.o.UserError)):self.finish(also)
        self.assertEqual(self.path.read_bytes(),before)

    def test_one_report_updates_each_job_and_links_events(self):
        self.finish('2,3')
        s=self.read();reports=[m for m in s['msgs'] if m.get('kind')=='report']
        self.assertEqual(len(reports),1)
        self.assertEqual((reports[0]['job'],reports[0]['jobs']),(1,[1,2,3]))
        for j in s['jobs']:
            self.assertEqual(j['status'],'done')
            self.assertEqual(j['summary'],reports[0]['text'])
            self.assertEqual(j['brief'],'함께 해결했어.')
            self.assertEqual(j['finished_by'],'codex')
        self.assertEqual(len({j['finished_at'] for j in s['jobs']}),1)
        events=[m for m in s['msgs'] if m.get('kind')=='event']
        self.assertEqual([m['job'] for m in events],[1,2,3])
        self.assertIn('요청 1 보고에 함께 답함',events[1]['text'])
        self.assertTrue(self.o.state_valid(s))

    def test_cli_and_duplicates_include_current_only_once(self):
        result=subprocess.run([sys.executable,str(SOURCE),'finish','하나의 답','--also','2,1,2,3'],env=os.environ.copy(),capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.read()['msgs'][-1]['jobs'],[1,2,3])

    def test_cancelled_closed_missing_removed_and_invalid_ids_are_atomic(self):
        for change in [dict(cancelled=True),dict(status='done'),dict(removed=True)]:
            jobs=copy.deepcopy(self.jobs);jobs[2].update(change);self.write(jobs)
            self.reject('2,3')
        for ids in ['2,99','0','-1','2,,3','2,','x','1.0','']:
            self.write();self.reject(ids)

    def test_all_unfinished_task_states_rejected_but_unrelated_work_allowed(self):
        for status in ('todo','doing','waiting'):
            task=dict(id=1,job=3,title='remaining',owner='claude',status=status)
            self.write(tasks=[task]);self.reject('2,3')
        self.write(tasks=[dict(id=1,job=99,title='unrelated',owner='claude',status='doing'),
                          dict(id=2,job=2,title='finished',owner='codex',status='done')])
        self.finish('2');self.assertEqual(self.read()['jobs'][2]['status'],'working')

    def test_permissions_apply_to_every_job_and_effective_delegate(self):
        jobs=copy.deepcopy(self.jobs);jobs[1]['lead']='claude';self.write(jobs);self.reject('2')
        jobs[1]['acting_lead']='codex';self.write(jobs);self.finish('2')
        jobs=copy.deepcopy(self.jobs);jobs[0]['acting_lead']='claude';self.write(jobs);self.reject('2')
        jobs=copy.deepcopy(self.jobs);jobs[1]['acting_lead']='claude';self.write(jobs);self.reject('2')

    def test_single_finish_keeps_legacy_report_and_does_not_overwrite_done(self):
        self.finish()
        report=self.read()['msgs'][-1]
        self.assertEqual(report['job'],1);self.assertNotIn('jobs',report)
        self.assertEqual(self.read()['jobs'][1]['status'],'working')
        self.reject(None)


if __name__=='__main__':unittest.main()
