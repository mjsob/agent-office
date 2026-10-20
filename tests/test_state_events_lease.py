"""상태 전이·이벤트 처리·실행 lease의 경합과 복구를 확인해요."""
import argparse,copy,importlib.util,json,os,sys,tempfile,time,unittest,threading,subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
TMP=tempfile.TemporaryDirectory(prefix='office-state-events-')
for k in list(os.environ):
 if k.startswith('OFFICE_'):del os.environ[k]
os.environ.update(OFFICE_DIR=TMP.name,OFFICE_AS='codex',OFFICE_SESSION='t')
src=sys.argv.pop(1) if len(sys.argv)>1 else str(Path(__file__).resolve().parent.parent/'payload'/'office.py')
spec=importlib.util.spec_from_file_location('o',src);o=importlib.util.module_from_spec(spec);spec.loader.exec_module(o)
class Checks(unittest.TestCase):
 def setUp(self):
  self.d=o.sdir('t');self.d.mkdir(parents=True,exist_ok=True)
  self.s={'jobs':[{'id':1,'lead':'claude','text':'one','status':'working'},{'id':2,'lead':'claude','text':'two','status':'working'}],'tasks':[],'msgs':[],'seq':0}
  for f in self.d.glob('*'):
   if f.is_file():f.unlink()
  (self.d/'state.json').write_text(json.dumps(self.s));self.w=object.__new__(o.Worker)
  for k,v in {'sid':'t','name':'codex','other':'claude','read_upto':0,'carry_msgs':[],'pending_events':[],'handled_events':set(),'retry_counts':{},'stall_prompts':[],'nudge_state':{},'rc':0,'error':None,'cont':0}.items():setattr(self.w,k,v)
 def task(self,**kw):return {'id':1,'title':'t','status':'doing','owner':'codex',**kw}
 def test_wait_without_target_rejected_without_mutation(self):
  t=self.task();old=copy.deepcopy(t)
  with self.assertRaises(o.UserError):o.state_transition(t,'waiting')
  self.assertEqual(t,old)
 def test_wait_target_accepted_and_completion_cleans_fields(self):
  t=self.task();o.state_transition(t,'waiting',wait_on=[2]);o.state_transition(t,'done');self.assertNotIn('wait_on',t)
 def test_pending_user_cannot_be_started(self):
  t=self.task(status='waiting',user_request={'status':'pending','text':'answer'})
  with self.assertRaises(o.UserError):o.state_transition(t,'doing')
 def test_cancelled_job_cannot_reopen(self):
  j={**self.s['jobs'][0],'status':'done','cancelled':True}
  with self.assertRaises(o.UserError):o.state_transition(j,'working')
 def test_completed_task_cannot_claim(self):
  with self.assertRaises(o.UserError):o.state_transition(self.task(status='done'),'doing')
 def test_invalid_scope_state(self):
  with self.assertRaises(o.UserError):o.state_transition(self.task(),'working')
 def test_worker_status_uses_reducer(self):
  self.w.status='working';self.assertEqual(self.w._execution['transition_revision'],1)
  with self.assertRaises(o.UserError):self.w.status='bogus'
 def test_event_ids_unique_and_info_not_actionable(self):
  o.post(self.s,'claude','codex','same',job=1);o.post(self.s,'claude','codex','same','note',job=1)
  a,b=self.s['msgs'];self.assertNotEqual(a['event_id'],b['event_id']);self.assertTrue(a['requires_action']);self.assertFalse(b['requires_action'])
 def test_other_job_user_event_survives_success(self):
  o.post(self.s,'user','codex','first',job=1);o.post(self.s,'user','codex','second',job=2)
  nt=self.w.next_turn(self.s);self.assertIsNotNone(nt);self.w.read_upto=2
  self.w.acknowledge_events(nt[3],1);self.assertEqual(self.w.pending_events,[2]);nt=self.w.next_turn(self.s);self.assertEqual(nt[1],2)
 def test_duplicated_event_identity_does_not_reexecute(self):
  o.post(self.s,'user','codex','one',job=None);nt=self.w.next_turn(self.s);self.w.acknowledge_events(nt[3],None)
  self.w.read_upto=1;self.s['msgs'].append({**self.s['msgs'][0],'id':2})
  self.assertIsNone(self.w.next_turn(self.s))
 def test_note_does_not_wake(self):
  o.post(self.s,'claude','codex','info','note',job=1);self.assertIsNone(self.w.next_turn(self.s))
 def test_legacy_event_still_wakes(self):
  self.s['msgs']=[{'id':1,'kind':'msg','from':'user','to':'codex','text':'old','job':None}]
  self.assertIsNotNone(self.w.next_turn(self.s))
 def test_revision_and_external_write_conflict(self):
  with o.state(True,'t') as s:s['seq']=1
  self.assertEqual(json.loads((self.d/'state.json').read_text())['revision'],1)
  with self.assertRaises(o.StorageError):
   with o.state(True,'t') as s:
    s['seq']=2;raw=json.loads((self.d/'state.json').read_text());raw['seq']=8;(self.d/'state.json').write_text(json.dumps(raw))
  self.assertEqual(json.loads((self.d/'state.json').read_text())['seq'],8)
 def test_lease_excludes_second_start_even_expired(self):
  with o.execution_lease(self.w,1) as lease:
   lease['expires_at']=0;o.atomic_json(self.d/'codex.run.json',lease)
   with self.assertRaises(o.UserError):
    with o.execution_lease(self.w,1):self.fail('double start')
 def test_old_release_does_not_erase_new_token(self):
  with o.execution_lease(self.w,1) as lease:o.atomic_json(self.d/'codex.run.json',{'token':'replacement','status':'running'})
  self.assertEqual(json.loads((self.d/'codex.run.json').read_text())['token'],'replacement')
 def test_exception_marks_failed(self):
  with self.assertRaises(RuntimeError):
   with o.execution_lease(self.w,1):raise RuntimeError('failed')
  self.assertEqual(json.loads((self.d/'codex.run.json').read_text())['status'],'failed')
 def test_live_orphan_child_prevents_new_start(self):
  ident=o.process_identity(os.getpid());o.atomic_json(self.d/'codex.process.json',dict(ident,pid=os.getpid(),owner=os.getpid(),owner_start=ident['start']))
  with self.assertRaises(o.UserError):
   with o.execution_lease(self.w,1):self.fail('orphan ignored')
 def test_revoked_token_cannot_write(self):
  with patch.dict(os.environ,{'OFFICE_RUN_TOKEN':'old'}):
   with self.assertRaises(o.UserError):o.require_run_token()
 def test_cli_bare_wait_rejected(self):
  with self.assertRaises(o.UserError):o.cmd_wait(argparse.Namespace(id=1,text='wait',pid=None,log=None,on=None,minutes=None))
 def test_checkpoint_new_fields_validated(self):
  self.assertTrue(o.checkpoint_valid({'read_upto':2,'pending_events':[1],'handled_events':['a']}));self.assertFalse(o.checkpoint_valid({'pending_events':['one']}))
 def test_wait_dependency_cycle_rejected(self):
  a=self.task(status='doing');b=self.task(id=2,status='waiting',wait_on=[1]);r={'tasks':[a,b]}
  with self.assertRaises(o.UserError):o.state_transition(a,'waiting',record=r,wait_on=[2])
  self.assertEqual(a['status'],'doing')
 def test_finished_dependency_checked_inside_transition(self):
  a=self.task();b=self.task(id=2,status='done')
  with self.assertRaises(o.UserError):o.state_transition(a,'waiting',record={'tasks':[a,b]},wait_on=[2])
 def test_noop_transaction_keeps_revision_and_file(self):
  before=(self.d/'state.json').read_bytes();key=o.file_key(self.d/'state.json')
  with o.state(True,'t'):pass
  self.assertEqual((self.d/'state.json').read_bytes(),before);self.assertEqual(o.file_key(self.d/'state.json'),key)
 def test_checkpoint_restores_unhandled_event_and_processed_identity(self):
  o.post(self.s,'user','codex','first',job=1);o.post(self.s,'user','codex','second',job=2);o.atomic_json(self.d/'state.json',self.s)
  w=o.Worker('codex','t',threading.Event());w.read_upto=0;new=w.next_turn(self.s)[3];w.read_upto=2;w.acknowledge_events(new,1);w.save_checkpoint(processed=True)
  restored=o.Worker('codex','t',threading.Event());self.assertEqual(restored.pending_events,[2]);self.assertIn(self.s['msgs'][0]['event_id'],restored.handled_events)
  self.assertEqual(restored.next_turn(self.s)[1],2)
 def test_wrapper_persists_execution_phase_without_model(self):
  w=o.Worker('codex','t',threading.Event());w._execute_agent=lambda *args:'result'
  self.assertEqual(w.run_agent('prompt',1),'result')
  run=json.loads((self.d/'codex.run.json').read_text());checkpoint=json.loads((self.d/'codex.worker.json').read_text())
  self.assertEqual(run['status'],'completed');self.assertEqual(run['run_id'],checkpoint['run_id'])
 def test_lease_excludes_a_different_process(self):
  script="import importlib.util,sys;sp=importlib.util.spec_from_file_location('o',sys.argv[1]);o=importlib.util.module_from_spec(sp);sp.loader.exec_module(o);w=type('W',(),{'sid':'t','name':'codex','rc':0,'error':None})();ctx=o.execution_lease(w,1);ctx.__enter__()"
  with o.execution_lease(self.w,1):
   r=subprocess.run([sys.executable,'-I','-c',script,src],capture_output=True,text=True,timeout=10)
   self.assertNotEqual(r.returncode,0);self.assertIn('중복 실행 방지',r.stderr)
 def test_duplicate_worker_does_not_reap_current_child(self):
  with o.execution_lease(self.w,1),patch.object(o,'recover_recorded_process') as recover:
   with self.assertRaises(o.UserError):o.Worker('codex','t',threading.Event())
   recover.assert_not_called()
 def test_heartbeat_cannot_restore_revoked_lease(self):
  with o.execution_lease(self.w,1) as lease:
   lease['expires_at']=0;o.atomic_json(self.d/'codex.run.json',{'status':'running','token':'other'})
   with self.assertRaises(o.UserError):self.w.heartbeat_run()
   self.assertEqual(json.loads((self.d/'codex.run.json').read_text())['token'],'other')
 def test_token_accepted_only_during_current_run(self):
  with o.execution_lease(self.w,1) as lease:
   token=lease['token']
   with patch.dict(os.environ,{'OFFICE_RUN_TOKEN':token}):o.require_run_token()
  with patch.dict(os.environ,{'OFFICE_RUN_TOKEN':token}):
   with self.assertRaises(o.UserError):o.require_run_token()
 def write_fixture(self):
  path=self.d/'user-file.txt';path.write_text('original');draft=self.d/'draft.txt';draft.write_text('updated')
  identity={'root':str(o.ROOT),'session':'t','job':1,'owner':'codex'}
  records={str(path):dict(identity,version=o.file_digest(path))}
  @contextmanager
  def store(write=False):yield records
  return path,draft,identity,store
 def test_reserved_write_rejects_changed_original(self):
  path,draft,identity,store=self.write_fixture();path.write_text('external')
  with patch.object(o,'guarded_path',return_value=path),patch.object(o,'reservation_owner',return_value=identity),patch.object(o,'reservation_store',store):
   with self.assertRaises(o.UserError):o.cmd_write(argparse.Namespace(path=str(path),source=str(draft)))
  self.assertEqual(path.read_text(),'external')
 def test_write_rechecks_version_before_replace(self):
  path,draft,identity,store=self.write_fixture();digest=o.file_digest(path)
  with patch.object(o,'guarded_path',return_value=path),patch.object(o,'reservation_owner',return_value=identity),patch.object(o,'reservation_store',store),patch.object(o,'file_digest',side_effect=[digest,'changed']):
   with self.assertRaises(o.UserError):o.cmd_write(argparse.Namespace(path=str(path),source=str(draft)))
  self.assertEqual(path.read_text(),'original')
 def test_write_token_revoked_at_last_check(self):
  path,draft,identity,store=self.write_fixture()
  with patch.object(o,'guarded_path',return_value=path),patch.object(o,'reservation_owner',return_value=identity),patch.object(o,'reservation_store',store),patch.object(o,'require_run_token',side_effect=[None,o.UserError('expired')]):
   with self.assertRaises(o.UserError):o.cmd_write(argparse.Namespace(path=str(path),source=str(draft)))
  self.assertEqual(path.read_text(),'original')
 def test_replace_failure_keeps_original_and_cleans_temp(self):
  path,draft,identity,store=self.write_fixture();before=set(self.d.iterdir())
  with patch.object(o,'guarded_path',return_value=path),patch.object(o,'reservation_owner',return_value=identity),patch.object(o,'reservation_store',store),patch.object(o.os,'replace',side_effect=OSError('disk')):
   with self.assertRaises(OSError):o.cmd_write(argparse.Namespace(path=str(path),source=str(draft)))
  self.assertEqual(path.read_text(),'original');self.assertEqual(set(self.d.iterdir()),before)
 def test_close_policy_preserves_user_action_wait(self):
  t=self.task(status='waiting',job=1,user_request={'status':'pending','text':'answer'})
  self.assertFalse(o.remaining_job_tasks([t],1));self.assertEqual(t['status'],'waiting')

 def test_handoff_keeps_user_waiting(self):
  t=self.task(status='waiting',user_request={'status':'pending','text':'answer'})
  o.state_transition(t,'todo',owner='claude');self.assertEqual(t['status'],'waiting');self.assertEqual(t['owner'],'claude')
 def test_handoff_keeps_dependency_waiting(self):
  t=self.task(status='waiting',wait_on=[2]);o.state_transition(t,'todo',owner='claude')
  self.assertEqual(t['status'],'waiting');self.assertEqual(t['wait_on'],[2])
 def test_resume_cleans_dead_wait_metadata(self):
  t=self.task(status='waiting',wait_on=[2],wait_log='/missing',wait_pid=99999999)
  o.state_transition(t,'doing');self.assertNotIn('wait_on',t);self.assertNotIn('wait_pid',t)
 def test_request_wakes_only_its_leader(self):
  self.s['jobs'][0]['status']='planning';o.post(self.s,'user','all','request','request',job=1)
  row=self.s['msgs'][0];self.assertTrue(row['requires_action']);self.assertFalse(o.event_for_worker(row,self.w,self.s['jobs']))
  self.w.name='claude';self.assertTrue(o.event_for_worker(row,self.w,self.s['jobs']))
 def test_closed_request_does_not_replay_pending_request(self):
  o.post(self.s,'user','all','request','request',job=1);self.s['jobs'][0]['status']='done'
  self.w.pending_events=[1];self.w.read_upto=1;self.w.name='claude'
  turn=self.w.next_turn(self.s);self.assertTrue(turn is None or not turn[3]);self.assertEqual(self.w.pending_events,[])
 def test_atomic_state_replace_failure_keeps_previous_json(self):
  path=self.d/'atomic.json';o.atomic_json(path,{'seq':1});before=path.read_bytes()
  with patch.object(Path,'replace',side_effect=OSError('disk')):
   with self.assertRaises(o.StorageError):o.atomic_json(path,{'seq':2})
  self.assertEqual(path.read_bytes(),before);self.assertFalse(list(self.d.glob('atomic.json.*.tmp')))

if __name__=='__main__':unittest.main()
