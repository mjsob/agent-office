"""새 대기는 대상을 요구하고 기존 대기 기록은 읽을 수 있는지 확인해요."""
import argparse,importlib.util,json,os,sys,tempfile
from pathlib import Path
with tempfile.TemporaryDirectory(prefix='office-wait-target-') as tmp:
 for k in list(os.environ):
  if k.startswith('OFFICE_'):del os.environ[k]
 os.environ.update(OFFICE_DIR=tmp,OFFICE_AS='claude',OFFICE_SESSION='t1')
 src=sys.argv[1] if len(sys.argv)>1 else str(Path(__file__).resolve().parent.parent/'payload'/'office.py')
 spec=importlib.util.spec_from_file_location('o',src);o=importlib.util.module_from_spec(spec);spec.loader.exec_module(o)
 d=o.sdir('t1');d.mkdir(parents=True,exist_ok=True)
 record={'jobs':[{'id':1,'status':'working','lead':'claude','text':'test'}],'tasks':[{'id':i,'title':str(i),'owner':'claude','status':'doing','job':1} for i in (1,2)],'msgs':[],'seq':2}
 (d/'state.json').write_text(json.dumps(record))
 def args(**kw):return argparse.Namespace(**{'id':1,'text':'결과를 기다려요','pid':None,'log':None,'on':None,'minutes':None,**kw})
 for _ in range(4):
  try:o.cmd_wait(args());raise AssertionError('대상 없는 새 대기를 허용했어요')
  except o.UserError as e:assert '기다릴 대상' in str(e)
 assert json.loads((d/'state.json').read_text())==record
 o.cmd_wait(args(on=[2]));assert o.state_view('t1')['tasks'][0]['wait_on']==[2]
 o.cmd_wait(args(minutes=3));assert o.state_view('t1')['tasks'][0]['wait_deadline']
 for minutes in (0,-1,float('nan')):
  try:o.cmd_wait(args(minutes=minutes));raise AssertionError('잘못된 기한을 허용했어요')
  except o.UserError:pass
 with o.state(True,'t1') as s:
  s['tasks'][0]['status']='waiting';s['tasks'][0].pop('wait_deadline',None)
 assert o.state_view('t1')['tasks'][0]['status']=='waiting'
 print('ok: explicit wait targets, deadlines, unchanged rejection and legacy compatibility')
