"""같은 재시도 정책·요청별 진척 번호·산출물 근거 분리를 확인해요."""
import argparse,contextlib,importlib.util,io,json,os,sys,tempfile,threading,time,types,unittest
from pathlib import Path
from unittest.mock import patch
SOURCE=Path(sys.argv.pop(1)).resolve() if len(sys.argv)>1 and sys.argv[1].endswith('.py') else Path(__file__).resolve().parents[1]/'payload/office.py'
class RetryProgressCompletion(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='office-progress-completion-');self.addCleanup(self.tmp.cleanup);env=os.environ.copy();self.addCleanup(lambda:(os.environ.clear(),os.environ.update(env)))
  for key in list(os.environ):
   if key.startswith('OFFICE_'):del os.environ[key]
  os.environ.update(OFFICE_DIR=self.tmp.name,OFFICE_SESSION='test',OFFICE_AS='codex',OFFICE_JOB='1')
  spec=importlib.util.spec_from_file_location('progress_target',SOURCE);self.o=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.o)
  self.o.server_log=lambda *a:None;self.o.release_reservations=lambda *a:None;self.o.LOGIN_HOOK=None
  self.path=self.o.sdir('test')/'state.json';self.write()
 def write(self):
  self.o.atomic_json(self.path,dict(jobs=[dict(id=i,status='working',lead='codex',text=f'j{i}') for i in (1,2)],tasks=[dict(id=1,job=1,title='task',owner='codex',status='doing')],msgs=[],seq=1),self.o.state_valid)
 def read(self):return json.loads(self.path.read_text())
 def revision(self,jid):return next(j for j in self.read()['jobs'] if j['id']==jid).get('progress_revision',0)
 def worker(self):
  w=self.o.Worker.__new__(self.o.Worker)
  for key,value in dict(name='codex',sid='test',rc=0,doing={'job':1,'kind':'work'},resume_turn=None,fail_count=0,failure_key=None,retry_counts={},stop=threading.Event(),immediate_pause=False,manual_pause=False,interrupted_job=None,read_upto=3,turn_read_start=1,paused=False,retry_at=0).items():setattr(w,key,value)
  w.idle=lambda status='idle':setattr(w,'status',status);w.save_checkpoint=lambda *a,**kw:None
  return w
 def done(self,evidence=None):self.o.cmd_done(argparse.Namespace(id=1,text='실행 결과',verdict=None,evidence=evidence))
 def test_retry_budget_one_counter_per_job_and_shared_failure_path(self):
  w=self.worker()
  with patch.object(self.o.time,'time',return_value=10000):
   for attempt,delay in enumerate(self.o.AUTO_RETRY_DELAYS,1):
    w.pause_with_error('network connection reset',1);self.assertEqual(w.retry_counts['1'],attempt);self.assertEqual(w.retry_at,10000+delay);self.assertFalse(w.paused);self.assertEqual(w.read_upto,1)
   w.pause_with_error('응답 없는 실행 종료 — 결과가 없어요.',1);self.assertTrue(w.paused);self.assertEqual(w.retry_counts['1'],3)
   w.doing={'job':2};w.pause_with_error('network error',2);self.assertEqual(w.retry_counts['2'],1);self.assertEqual(w.retry_at,10015)
 def test_auth_limit_manual_stop_and_cancel_do_not_schedule(self):
  for why,flag in [('not logged in',None),('usage limit exceeded',None),('network error','manual_pause'),('network error','immediate_pause'),('network error','interrupted_job'),('network error','stop')]:
   w=self.worker()
   if flag=='stop':w.stop.set()
   elif flag:setattr(w,flag,1 if flag=='interrupted_job' else True)
   self.assertFalse(w.schedule_retry(why,1));self.assertEqual(w.retry_counts,{})
 def test_progress_revision_is_local_and_ignores_chatter(self):
  with self.o.state(True,'test') as s:s['tasks'][0]['status']='waiting'
  self.assertEqual((self.revision(1),self.revision(2)),(1,0))
  with self.o.state(True,'test') as s:self.o.post(s,'claude','codex','진행 중이라고 말함','msg',job=1)
  self.assertEqual(self.revision(1),1)
  with self.o.state(True,'test') as s:self.o.post(s,'user','all','다른 요청의 정정','msg',job=2)
  self.assertEqual((self.revision(1),self.revision(2)),(1,1))
  with self.o.state(True,'test') as s:s['tasks'][0]['progress']={'at':time.time(),'done':1,'total':3}
  self.assertEqual((self.revision(1),self.revision(2)),(2,1))
 def test_file_change_bumps_only_selected_job(self):
  with self.o.state(True,'test') as s:self.o.bump_job_progress(s,2)
  self.assertEqual((self.revision(1),self.revision(2)),(0,1))
 def test_no_evidence_records_execution_success_but_artifact_unverified(self):
  self.done();t=self.read()['tasks'][0];self.assertEqual(t['status'],'done');self.assertEqual(t['execution']['status'],'succeeded');self.assertEqual(t['completion']['status'],'unverified')
  self.assertIn('산출물 미확인 작업: #1',self.o.task_metrics(self.read(),1))
  self.o.cmd_finish(argparse.Namespace(text='검증 제한을 보고해요.',also=None));j=self.read()['jobs'][0]
  self.assertEqual(j['status'],'done');self.assertEqual(j['completion']['unverified_task_ids'],[1])
 def test_check_evidence_and_file_evidence_are_recorded(self):
  path=Path(self.tmp.name)/'한글 결과.txt';path.write_text('verified')
  self.done([str(path),'check:회귀 시험 4건을 통과했어요.']);t=self.read()['tasks'][0]
  self.assertTrue(self.o.artifact_verified(t,True));self.assertEqual(len(t['completion']['evidence']),2);self.assertEqual(t['completion']['evidence'][0]['sha256'],self.o.file_digest(path))
  self.o.cmd_finish(argparse.Namespace(text='근거를 확인했어요.',also=None));self.assertEqual(self.read()['jobs'][0]['completion']['status'],'verified')
 def test_missing_or_empty_evidence_rejects_atomically(self):
  for evidence in [['check:   '],[str(Path(self.tmp.name)/'missing')]]:
   before=self.path.read_bytes()
   with self.assertRaises(self.o.UserError):self.done(evidence)
   self.assertEqual(before,self.path.read_bytes())
 def test_changed_evidence_is_reported_unverified_at_finish_without_blocking(self):
  path=Path(self.tmp.name)/'result.txt';path.write_text('before');self.done([str(path)]);path.write_text('after different')
  self.o.cmd_finish(argparse.Namespace(text='파일 변경을 알렸어요.',also=None));j=self.read()['jobs'][0]
  self.assertEqual(j['status'],'done');self.assertEqual(j['completion']['status'],'unverified');self.assertEqual(j['completion']['unverified_task_ids'],[1])
 def test_large_file_uses_metadata_without_expensive_hash(self):
  path=Path(self.tmp.name)/'large.bin'
  with path.open('wb') as f:f.truncate(9*1024*1024)
  with patch.object(self.o,'file_digest',side_effect=AssertionError('large hash')):proof=self.o.collect_completion_evidence([str(path)])
  self.assertIsNone(proof[0]['sha256']);self.assertEqual(proof[0]['size'],9*1024*1024)
 def test_user_confirmation_is_separate_and_kept(self):
  with self.o.state(True,'test') as s:s['tasks'][0].update(status='waiting',user_request=dict(status='pending',text='영상을 확인하세요.'))
  self.o.cmd_finish(argparse.Namespace(text='자동 작업은 끝났어요.',also=None));j=self.read()['jobs'][0]
  self.assertEqual(j['completion']['needs_user_task_ids'],[1]);self.assertEqual(j['completion']['unverified_task_ids'],[]);self.assertIsNotNone(self.o.pending_user_request(self.read()['tasks'][0]))
if __name__=='__main__':unittest.main()
