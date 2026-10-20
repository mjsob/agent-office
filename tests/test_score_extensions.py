"""정정 근거·같은 종류 속도·고정 기준 A/B·API 점수를 확인해요."""
import argparse,contextlib,copy,importlib.util,io,json,os,subprocess,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
SOURCE=Path(sys.argv.pop(1)).resolve() if len(sys.argv)>1 and sys.argv[1].endswith('.py') else Path(__file__).resolve().parents[1]/'payload/office.py'
class Extensions(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='office-score-ext-');self.addCleanup(self.tmp.cleanup);before=os.environ.copy();self.addCleanup(lambda:(os.environ.clear(),os.environ.update(before)))
  for key in list(os.environ):
   if key.startswith('OFFICE_'):del os.environ[key]
  os.environ.update(OFFICE_DIR=self.tmp.name,OFFICE_SESSION='test',OFFICE_AS='codex',OFFICE_JOB='1')
  spec=importlib.util.spec_from_file_location('extensions',SOURCE);self.o=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.o)
  self.o.server_log=lambda *a:None;self.path=self.o.sdir('test')/'state.json';self.write([])
 def task(self,i,owner='codex',duration=10,level='normal',kind='수정',**extra):
  return dict(id=i,job=1,title=f't{i}',owner=owner,completed_by=owner,status='done',claimed_at=100,done_at=100+duration,kind=kind,level=level,metrics=dict(owner=owner,verdict='pass',reviewed_by='claude' if owner=='codex' else 'codex'),**extra)
 def write(self,tasks,msgs=None):self.o.atomic_json(self.path,dict(jobs=[dict(id=1,status='working',lead='codex',text='test')],tasks=tasks,msgs=[dict(to='all',kind='request',ts='t',at=1,**m) for m in msgs or []],seq=max([t['id'] for t in tasks],default=0)),self.o.state_valid)
 def read(self):return json.loads(self.path.read_text())
 def add(self,**kwargs):
  args=dict(text='작은 비교',to=None,level=None,tag='수정',rework_of=None,ab=True,criteria='같은 입력 3개로 정확한 결과를 확인해요.');args.update(kwargs);return self.o.cmd_add(argparse.Namespace(**args))
 def test_auto_correction_requires_explicit_task_reference_and_is_idempotent(self):
  self.write([self.task(1)])
  with self.o.state(True) as s:
   self.o.post(s,'user','all','문제점 #1 수정해',kind='request',job=1)
   self.assertNotIn('corrections',s['tasks'][0]['metrics'])
   self.o.post(s,'user','all','작업 #1 되돌려. 다시 수정해',kind='request',job=1)
   msg=s['msgs'][-1];self.o.auto_user_correction(s,msg);self.assertEqual(s['tasks'][0]['metrics']['corrections'],1);self.assertEqual(msg['corrected_tasks'],[1])
  self.o.cmd_correction(argparse.Namespace(targets='1',re=2));self.assertEqual(self.read()['tasks'][0]['metrics']['corrections'],1)
 def test_quoted_or_negative_correction_is_not_counted(self):
  self.write([self.task(1)])
  for text in ('작업 #1 재작업 필요 없어', '작업 #1 수정해? 아니 하지 마', '> 작업 #1 원복해', '```작업 #1 다시 고쳐```'):
   with self.o.state(True) as s:self.o.post(s,'user','all',text,kind='request',job=1)
  self.assertNotIn('corrections',self.read()['tasks'][0]['metrics'])
 def test_correction_multiple_targets_rejects_incomplete_atomically(self):
  a,b=self.task(1),self.task(2);b['status']='doing';self.write([a,b],msgs=[dict(id=1,job=1,**{'from':'user'},text='재작업해')]);before=self.path.read_bytes()
  with self.assertRaises(self.o.UserError):self.o.cmd_correction(argparse.Namespace(targets='1,2',re=1))
  self.assertEqual(before,self.path.read_bytes())
 def test_explicit_correction_links_same_job_once_without_reference(self):
  self.write([self.task(1)],msgs=[dict(id=1,job=1,**{'from':'user'},text='원복하고 재작업해')]);a=argparse.Namespace(targets='1',re=1)
  self.o.cmd_correction(a);self.o.cmd_correction(a);row=self.o.task_scores(self.read())['codex']['수정'];self.assertEqual(row['corrections'],1)
 def test_correction_rejects_wrong_message_or_job_and_does_not_mutate(self):
  self.write([self.task(1)],msgs=[dict(id=1,job=9,**{'from':'user'},text='원복해'),dict(id=2,job=1,**{'from':'claude'},text='원복해'),dict(id=3,job=1,**{'from':'user'},text='잘했어')])
  for mid in (1,2,3,999):
   before=self.path.read_bytes()
   with self.assertRaises(self.o.UserError):self.o.cmd_correction(argparse.Namespace(targets='1',re=mid))
   self.assertEqual(before,self.path.read_bytes())
 def test_correction_without_quality_sample_does_not_invent_verdict(self):
  t=self.task(1);t['metrics']={};self.write([t],msgs=[dict(id=1,job=1,**{'from':'user'},text='다시 고쳐')]);self.o.cmd_correction(argparse.Namespace(targets='1',re=1));row=self.o.task_scores(self.read())['codex']['수정'];self.assertEqual((row['n'],row['corrections'],row['enough']),(0,1,False))
 def test_speed_uses_kind_level_median_and_five_measured_samples(self):
  tasks=[self.task(i,duration=d,owner='codex' if i==1 else 'claude') for i,d in enumerate((10,20,30,40,50),1)]
  tasks += [self.task(i,duration=100,level='heavy') for i in range(6,11)]+[self.task(i,duration=1000,kind='문서') for i in range(11,16)]
  self.write(tasks);score=self.o.task_scores(self.read());self.assertEqual(score['claude']['수정']['speed_ratio'],1.167);self.assertEqual(score['codex']['수정']['speed_ratio'],1.0);self.assertEqual(score['codex']['수정']['speed_n'],6)
  self.write(tasks[:4]);self.assertIsNone(self.o.task_scores(self.read())['codex']['수정']['speed_ratio'])
 def test_missing_and_zero_times_are_not_fast_samples(self):
  tasks=[self.task(i) for i in range(1,6)];tasks[-1].pop('claimed_at');self.write(tasks);self.assertIsNone(self.o.task_scores(self.read())['codex']['수정']['speed_ratio'])
  for bad in (0,-1):
   task=self.task(9,duration=bad);self.assertIsNone(self.o.measured_task_seconds(task))
  task=self.task(9);task.update(claimed_at=-1e308,done_at=1e308);self.assertIsNone(self.o.measured_task_seconds(task))
 def test_ab_creates_exact_pair_fixed_same_criteria_and_no_duplicate(self):
  self.add();self.add();s=self.read();self.assertEqual(len(s['tasks']),2);a,b=s['tasks'];self.assertEqual({a['owner'],b['owner']},set(self.o.AGENTS));self.assertEqual(a['ab'],b['ab']);self.assertEqual(a['level'],'light');self.assertEqual(a['title'],b['title']);self.assertIn('formula',a['ab']);self.assertEqual(a['ab']['formula']['version'],1)
 def test_ab_bad_options_are_atomic(self):
  for kwargs in ({'criteria':None},{'criteria':''},{'criteria':'x'*2001},{'to':'codex'},{'level':'heavy'},{'ab':False}):
   before=self.path.read_bytes()
   with self.assertRaises(self.o.UserError):self.add(**kwargs)
   self.assertEqual(before,self.path.read_bytes())
  self.add();before=self.path.read_bytes()
  with self.assertRaises(self.o.UserError):self.add(criteria='바뀐 기준')
  self.assertEqual(before,self.path.read_bytes())
 def test_ab_compares_quality_before_speed_and_requires_independent_review(self):
  self.add();s=self.read()
  for t in s['tasks']:t.update(status='done',claimed_at=100,done_at=110 if t['owner']=='codex' else 120,completed_by=t['owner'],metrics=dict(owner=t['owner'],verdict='fixed:1' if t['owner']=='codex' else 'pass',reviewed_by='claude' if t['owner']=='codex' else 'codex'))
  result=self.o.ab_comparisons(s)[0];self.assertEqual((result['status'],result['winner']),('compared',s['tasks'][0]['id'])) # AGENTS=claude,codex
  s['tasks'][0]['metrics']['verdict']='fixed:1';self.assertEqual(self.o.ab_comparisons(s)[0]['winner'],s['tasks'][1]['id'])
  s['tasks'][0]['metrics']['reviewed_by']=s['tasks'][0]['owner'];self.assertEqual(self.o.ab_comparisons(s)[0]['status'],'pending')
 def test_review_and_finish_save_one_line_score_lesson(self):
  self.write([self.task(i) for i in range(1,6)]);s=self.read();self.assertIn('배분 점수',self.o.task_metrics(s,1));self.o.cmd_finish(argparse.Namespace(text='완료',also=None));s=self.read();self.assertIn('통과 5/5',s['jobs'][0]['score_lesson']);self.assertIn('배분 점수',(self.o.D/'lessons.md').read_text());self.assertIn('--verdict',self.o.REVIEW)
 def test_lesson_write_failure_does_not_block_finish(self):
  with patch.object(self.o,'save_lesson',side_effect=OSError('unwritable')):self.o.cmd_finish(argparse.Namespace(text='완료',also=None))
  self.assertEqual(self.read()['jobs'][0]['status'],'done')
 def test_snapshot_scores_contract_and_changes_revision_without_server(self):
  self.write([self.task(1)]);o=self.o;meta=dict(current='test',sessions=[dict(id='test',title='test',deleted=False,seen=999)])
  office=o.Office();office.ensure_team=lambda sid:None;office.teams['test']={'w':{}};office.git_info=lambda sid:{};office.busy=lambda sid:False;office.job_wait_reason=lambda *args:None
  with patch.object(o,'meta_view',return_value=meta),patch.object(o,'resource_view',return_value={}),patch.object(o,'update_info',return_value={}),patch.object(o,'meta_later'):
   data=office.snapshot('test');row=data['scores']['codex']['수정'];self.assertTrue({'n','pass','fixed','fail','rework','corrections','speed_ratio','enough'}.issubset(row));self.assertFalse(row['enough']);rev=data['rev']
   with o.state(True) as s:s['tasks'][0]['metrics']['corrections']=1
   new=office.snapshot('test',since_rev=rev);self.assertNotEqual(new['rev'],rev);self.assertEqual(new['scores']['codex']['수정']['corrections'],1)
 def test_cli_ab_and_correction_do_not_launch_server(self):
  r=subprocess.run([sys.executable,str(SOURCE),'add','독립 작은 검토','--ab','--criteria','같은 기준','--tag','검수'],env=os.environ.copy(),capture_output=True,text=True,timeout=10);self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(len(self.read()['tasks']),2)
  self.write([self.task(1)],msgs=[dict(id=1,job=1,**{'from':'user'},text='다시 고쳐')])
  r=subprocess.run([sys.executable,str(SOURCE),'correction','1','--re','1'],env=os.environ.copy(),capture_output=True,text=True,timeout=10);self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(self.read()['tasks'][0]['metrics']['corrections'],1)
if __name__=='__main__':unittest.main()
