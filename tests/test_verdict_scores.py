"""검수 결과·재작업 귀속과 작은 표본의 집계 제한을 확인해요."""
import argparse,contextlib,importlib.util,io,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
SOURCE=Path(sys.argv.pop(1)).resolve() if len(sys.argv)>1 and sys.argv[1].endswith('.py') else Path(__file__).resolve().parents[1]/'payload/office.py'
class Scores(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='office-scores-');self.addCleanup(self.tmp.cleanup);before=os.environ.copy();self.addCleanup(lambda:(os.environ.clear(),os.environ.update(before)))
  for k in list(os.environ):
   if k.startswith('OFFICE_'):del os.environ[k]
  os.environ.update(OFFICE_DIR=self.tmp.name,OFFICE_SESSION='test',OFFICE_AS='codex',OFFICE_JOB='1')
  spec=importlib.util.spec_from_file_location('scores_target',SOURCE);self.o=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.o)
  self.o.release_reservations=lambda *a:None;self.o.server_log=lambda *a:None;self.path=self.o.sdir('test')/'state.json'
  self.write([self.task(1)])
 def task(self,i,**kw):return dict(id=i,job=1,title=f't{i}',owner='codex',status='doing',kind='수정',**kw)
 def write(self,tasks):self.o.atomic_json(self.path,dict(jobs=[dict(id=1,status='working',lead='codex',text='request')],tasks=tasks,msgs=[],seq=max((t['id'] for t in tasks),default=0)),self.o.state_valid)
 def read(self):return json.loads(self.path.read_text())
 def done(self,verdict=None):self.o.cmd_done(argparse.Namespace(id=1,text='결과',verdict=verdict))
 def add(self,text,**kw):self.o.cmd_add(argparse.Namespace(text=text,to='claude',level=None,tag=None,rework_of=None,**kw))
 def test_legacy_done_without_verdict_is_unscored(self):
  self.done();self.assertNotIn('metrics',self.read()['tasks'][0]);self.assertFalse(any(self.o.task_scores(self.read()).values()))
 def test_verdicts_persist_and_repeated_done_does_not_add_samples(self):
  for verdict in ('pass','fixed:3','fail'):
   self.write([self.task(1)]);self.done(verdict);self.done(verdict)
   t=self.read()['tasks'][0];self.assertEqual(t['metrics']['verdict'],verdict);row=self.o.task_scores(self.read())['codex']['수정'];self.assertEqual(row['n'],1)
   self.assertEqual(row['defects'],3 if verdict.startswith('fixed') else 0)
 def test_invalid_verdict_is_atomic(self):
  for verdict in ('fixed:0','fixed:-1','fixed:x','PASS','pass;rm','fixed:1000000'):
   before=self.path.read_bytes()
   with self.assertRaises(self.o.UserError):self.done(verdict)
   self.assertEqual(before,self.path.read_bytes())
 def test_foreign_agent_cannot_give_itself_credit(self):
  os.environ['OFFICE_AS']='claude';before=self.path.read_bytes()
  with self.assertRaises(self.o.UserError):self.done('pass')
  self.assertEqual(before,self.path.read_bytes())
 def test_rework_is_attributed_to_original_owner_and_deduplicated(self):
  self.done('pass')
  args=argparse.Namespace(text='후속 수정',to='claude',level=None,tag=None,rework_of=1)
  self.o.cmd_add(args);self.o.cmd_add(args)
  s=self.read();self.assertEqual(s['tasks'][0]['metrics']['rework'],1);self.assertEqual(s['tasks'][1]['owner'],'claude');self.assertEqual(s['tasks'][1]['kind'],'수정');self.assertEqual(s['tasks'][1]['rework_of'],1)
  rows=self.o.task_scores(s);self.assertEqual(rows['codex']['수정']['rework'],1);self.assertFalse(rows['claude'])
 def test_leading_fix_reference_counts_but_independent_review_does_not(self):
  self.done('pass');self.add('#1 독립 검수');self.assertEqual(self.read()['tasks'][0]['metrics'].get('rework',0),0)
  self.add('#1 수정 반영');self.assertEqual(self.read()['tasks'][0]['metrics']['rework'],1)
 def test_explicit_rework_requires_completed_existing_task(self):
  for target in (1,999):
   before=self.path.read_bytes()
   with self.assertRaises(self.o.UserError):self.o.cmd_add(argparse.Namespace(text='수정',to='claude',level=None,tag=None,rework_of=target))
   self.assertEqual(before,self.path.read_bytes())
 def test_tags_and_minimum_sample_per_agent_kind(self):
  rows=[]
  for i in range(1,6):rows.append(self.task(i,metrics=dict(verdict='pass',owner='codex')))
  rows.append(dict(id=6,job=1,title='other',owner='claude',status='done',kind='수정',metrics=dict(verdict='fail',owner='claude')))
  rows.append(dict(id=7,job=1,title='other',owner='codex',status='done',kind='문서',metrics=dict(verdict='fixed:2',owner='codex')))
  self.write(rows);scores=self.o.task_scores(self.read());self.assertTrue(scores['codex']['수정']['enough']);self.assertFalse(scores['claude']['수정']['enough']);self.assertFalse(scores['codex']['문서']['enough'])
  out=io.StringIO();before=self.path.read_bytes()
  with contextlib.redirect_stdout(out):self.o.cmd_scores(argparse.Namespace())
  self.assertIn('표본 부족',out.getvalue());self.assertIn('100.0%',out.getvalue());self.assertEqual(before,self.path.read_bytes())
 def test_cli_is_compatible_and_scores_never_launches_gui(self):
  r=subprocess.run([sys.executable,str(SOURCE),'done','1','결과','--verdict','fixed:2'],env=os.environ.copy(),capture_output=True,text=True,timeout=10)
  self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(self.read()['tasks'][0]['metrics']['verdict'],'fixed:2')
  r=subprocess.run([sys.executable,str(SOURCE),'scores'],env=os.environ.copy(),capture_output=True,text=True,timeout=10)
  self.assertEqual(r.returncode,0,r.stderr);self.assertIn('표본 부족',r.stdout)
 def test_add_tag_validation_and_tag_persistence(self):
  for tag in ('','  ','x\ny','a'*41):
   with self.assertRaises(self.o.UserError):self.o.cmd_add(argparse.Namespace(text='種類',to='claude',level=None,tag=tag,rework_of=None))
  self.o.cmd_add(argparse.Namespace(text='문서 작성',to='claude',level=None,tag='문서',rework_of=None));self.assertEqual(self.read()['tasks'][-1]['kind'],'문서')
if __name__=='__main__':unittest.main()
