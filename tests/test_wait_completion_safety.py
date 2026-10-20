"""대기·소유권·취소·사용자 확인 마감의 회귀를 격리 상태에서 확인해요."""
import argparse,copy,importlib.util,json,os,signal,subprocess,sys,tempfile,threading,time,types,unittest
from pathlib import Path
from unittest.mock import patch
SOURCE=Path(sys.argv.pop(1)).resolve() if len(sys.argv)>1 and not sys.argv[1].startswith('-') else Path(__file__).resolve().parents[1]/'payload/office.py'
class Safety(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='office-safety-');self.addCleanup(self.tmp.cleanup)
  self.env=os.environ.copy();self.addCleanup(lambda:(os.environ.clear(),os.environ.update(self.env)))
  for k in list(os.environ):
   if k.startswith('OFFICE_'):del os.environ[k]
  os.environ.update(OFFICE_DIR=self.tmp.name,OFFICE_SESSION='test',OFFICE_AS='codex',OFFICE_JOB='1')
  spec=importlib.util.spec_from_file_location('safety_target',SOURCE);self.o=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.o)
  self.o.server_log=lambda *a,**kw:None;self.o.release_reservations=lambda *a:None
  self.path=self.o.sdir('test')/'state.json';self.now=time.time()
  self.jobs=[dict(id=i,lead='codex',status='working',text=f'j{i}') for i in (1,2)]
  self.o.meta_view=lambda:{'sessions':[{'id':'test','deleted':False}]}
 def task(self,i=1,owner='codex',status='doing',job=1,**kw):return dict(id=i,owner=owner,status=status,job=job,title=f't{i}',**kw)
 def write(self,tasks,jobs=None):self.o.atomic_json(self.path,dict(jobs=copy.deepcopy(jobs or self.jobs),tasks=tasks,msgs=[],seq=10),self.o.state_valid)
 def read(self):return json.loads(self.path.read_text())
 def test_done_handoff_reject_foreign_owner_atomically(self):
  for fn,args in [(self.o.cmd_done,dict(id=1,text='done')),(self.o.cmd_handoff,dict(id=1,to='codex',text='move'))]:
   self.write([self.task(owner='claude')]);before=self.path.read_bytes()
   with self.assertRaises(self.o.UserError):fn(argparse.Namespace(**args))
   self.assertEqual(before,self.path.read_bytes())
 def test_own_and_unassigned_legacy_done_still_work(self):
  for owner in ('codex',None):
   self.write([self.task(owner=owner)]);self.o.cmd_done(argparse.Namespace(id=1,text='done'))
   self.assertEqual(self.read()['tasks'][0]['status'],'done')
 def test_need_user_finish_preserves_open_confirmation(self):
  for status in ('pending','expired'):
   t=self.task(status='waiting',user_request=dict(status=status,text='파일을 보내세요.',created_at=self.now))
   self.write([t]);self.o.cmd_finish(argparse.Namespace(text='자동 처리는 끝났어요.',also=None));s=self.read()
   self.assertEqual(s['jobs'][0]['status'],'done');self.assertEqual(s['tasks'][0],t)
   self.assertIsNotNone(self.o.pending_user_request(s['tasks'][0]))
   self.assertEqual(self.o.job_display_status(s['jobs'][0],s['tasks'])[0],'done')
 def test_finish_still_blocks_external_wait_and_mixed_work(self):
  for tasks in [[self.task(status='waiting')],[self.task(status='waiting',user_request={'status':'pending','text':'확인하세요.'}),self.task(2,status='doing')]]:
   self.write(tasks);before=self.path.read_bytes()
   with self.assertRaises(SystemExit):self.o.cmd_finish(argparse.Namespace(text='끝',also=None))
   self.assertEqual(before,self.path.read_bytes())
 def test_log_only_stall_once_and_progress_resume_suppression(self):
  log=Path(self.tmp.name)/'work.log';log.write_text('data');os.utime(log,(self.now-4000,self.now-4000))
  self.write([self.task(status='waiting',wait_log=str(log),waiting_at=self.now-4000)])
  self.o.Office.check_waits(types.SimpleNamespace(),'test');s=self.read();self.assertTrue(s['tasks'][0].get('wait_stale'));count=len(s['msgs'])
  self.o.Office.check_waits(types.SimpleNamespace(),'test');self.assertEqual(len(self.read()['msgs']),count)
  for p in ({'at':time.time()},{'resume_at':time.time()+500}):
   self.write([self.task(status='waiting',wait_log=str(log),waiting_at=self.now-4000,progress=p)])
   self.o.Office.check_waits(types.SimpleNamespace(),'test');self.assertFalse(self.read()['tasks'][0].get('wait_stale'))
 def test_missing_log_only_still_wakes(self):
  self.write([self.task(status='waiting',wait_log=str(Path(self.tmp.name)/'missing'),waiting_at=self.now-4000)])
  self.o.Office.check_waits(types.SimpleNamespace(),'test');self.assertTrue(self.read()['tasks'][0].get('wait_stale'))
 def test_stall_is_per_job_and_not_reset_by_other_job_activity(self):
  self.write([self.task(),self.task(2,owner='claude',job=2)])
  codex=types.SimpleNamespace(name='codex',status='idle',doing=None,paused=False,error=None)
  claude=types.SimpleNamespace(name='claude',status='working',doing={'job':2},paused=False,error=None)
  team={'stop':threading.Event(),'w':{'codex':codex,'claude':claude}}
  fake=types.SimpleNamespace(teams={'test':team})
  self.o.Office.check_stall(fake,'test')
  with self.o.state(True,'test') as s:self.o.post(s,'claude','user','다른 요청 진행','progress',job=2)
  with patch.object(self.o.time,'time',return_value=self.now+self.o.STALL_NOTICE+10):self.o.Office.check_stall(fake,'test')
  s=self.read();alerts=[m for m in s['msgs'] if '진행 멈춤' in m['text']]
  self.assertEqual([m['job'] for m in alerts],[1]);self.assertEqual(codex.stall_prompts[0][1],1)
  with patch.object(self.o.time,'time',return_value=self.now+2*self.o.STALL_NOTICE+20):self.o.Office.check_stall(fake,'test')
  self.assertEqual(len([m for m in self.read()['msgs'] if '진행 멈춤' in m['text']]),1)
 def test_cancel_kills_owned_bg_and_preserves_other_job_external_process(self):
  child=subprocess.Popen(['bash','-c','sleep 30; echo done'],stdout=subprocess.DEVNULL,start_new_session=True);self.addCleanup(lambda:child.poll() is None and self.cleanup_proc(child))
  other=subprocess.Popen(['sleep','30'],start_new_session=True);self.addCleanup(lambda:other.poll() is None and self.cleanup_proc(other))
  ident=self.o.process_identity(child.pid)
  self.write([self.task(status='waiting',wait_pid=child.pid,wait_start=ident['start'],wait_process=ident,wait_cmd='sleep 30',wait_rc=str(Path(self.tmp.name)/'rc')),
              self.task(2,job=2,status='waiting',wait_pid=other.pid,wait_start=self.o.process_identity(other.pid)['start'])])
  office=self.o.Office();office.handle({'action':'cancel_job','id':1,'session':'test'})
  child.wait(timeout=3);self.assertIsNone(other.poll());self.assertTrue(self.read()['jobs'][0]['cancelled'])
  self.assertEqual(self.read()['tasks'][1]['status'],'waiting')
 def test_bg_identity_mismatch_does_not_kill(self):
  child=subprocess.Popen(['sleep','30'],start_new_session=True);self.addCleanup(lambda:child.poll() is None and self.cleanup_proc(child))
  ident=self.o.process_identity(child.pid)
  for fields in [dict(wait_start=ident['start']+1),dict(wait_start=ident['start'],wait_process={**ident,'command':'wrong'})]:
   t=dict(wait_pid=child.pid,wait_rc='test.rc',wait_cmd='sleep 30',**fields)
   self.assertFalse(self.o.terminate_bg_watch(t));self.assertIsNone(child.poll())
 def cleanup_proc(self,p):
  try:os.killpg(p.pid,signal.SIGKILL)
  except ProcessLookupError:pass
  p.wait(timeout=3)
if __name__=='__main__':unittest.main()
