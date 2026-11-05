"""작업 표시줄 요청과 뒤에 있는 대화의 읽음 처리를 확인해요."""
import copy,importlib.util,json,os,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from contextlib import ExitStack
class Attention(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='office-attention-');self.addCleanup(self.tmp.cleanup)
  env=patch.dict(os.environ,{'OFFICE_DIR':self.tmp.name,'OFFICE_SESSION':'t','OFFICE_AS':'codex'});env.start();self.addCleanup(env.stop)
  source=Path(__file__).resolve().parents[1]/'payload/office.py'
  if not source.is_file():source=Path('/home/chan/agent-office/payload/office.py')
  spec=importlib.util.spec_from_file_location('office_attention_test',source);self.o=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.o)
  self.office=SimpleNamespace(window_owner='owner')
  self.p={'client':'owner','active':True,'title':'대화 — Office'}
 def test_native_start_stop(self):
  o=self.o
  with patch.object(o,'IS_MAC',False),patch.dict(os.environ,{'WSL_DISTRO_NAME':'Ubuntu'}),patch.object(o,'native_windows_call',return_value={'ok':True,'matched':1,'flashing':True}) as native:
   self.assertTrue(o.window_attention(self.office,self.p)['flashing']);native.assert_called_with('attention','start','대화 — Office')
   o.window_attention(self.office,{**self.p,'active':False});native.assert_called_with('attention','stop','대화 — Office')
 def test_mac_and_non_wsl_blocked_before_native(self):
  o=self.o
  for mac,wsl in [(True,'Ubuntu'),(False,'')]:
   with patch.object(o,'IS_MAC',mac),patch.dict(os.environ,{'WSL_DISTRO_NAME':wsl}),patch.object(o,'native_windows_call') as native:
    with self.assertRaises(o.UserError):o.window_attention(self.office,self.p)
    native.assert_not_called()
 def test_malformed_and_other_client_cannot_control_window(self):
  o=self.o
  bad=[None,[],{}, {**self.p,'active':1},{**self.p,'active':'true'},{**self.p,'title':'not Office'},{**self.p,'title':'x\n — Office'},{**self.p,'title':'a'*1001+' — Office'},{**self.p,'client':'stale'}]
  with patch.object(o,'IS_MAC',False),patch.dict(os.environ,{'WSL_DISTRO_NAME':'Ubuntu'}),patch.object(o,'native_windows_call') as native:
   for p in bad:
    with self.subTest(payload=p),self.assertRaises(o.UserError):o.window_attention(self.office,p)
   native.assert_not_called()
 def test_missing_old_helper_reports_failure_and_no_match_is_not_success(self):
  o=self.o
  with patch.object(o,'IS_MAC',False),patch.dict(os.environ,{'WSL_DISTRO_NAME':'Ubuntu'}):
   for result in [None,{}, {'matched':'1'}]:
    with patch.object(o,'native_windows_call',return_value=result),self.assertRaises(o.UserError):o.window_attention(self.office,self.p)
   with patch.object(o,'native_windows_call',return_value={'ok':False,'matched':0,'flashing':False}):self.assertFalse(o.window_attention(self.office,self.p)['ok'])
 def test_http_endpoint_validates_origin_and_routes_attention(self):
  import http.client
  o=self.o;off=self.office
  server=o.ThreadingHTTPServer(('127.0.0.1',0),o.make_handler(off));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   with patch.object(o,'IS_MAC',False),patch.dict(os.environ,{'WSL_DISTRO_NAME':'Ubuntu'}),patch.object(o,'native_windows_call',return_value={'ok':True,'matched':1,'flashing':True}) as native:
    for origin,code in [('https://example.com',403),(None,200)]:
     conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
     headers={'Origin':origin} if origin else {}
     conn.request('POST','/api/window-attention',json.dumps(self.p),headers)
     response=conn.getresponse();body=json.loads(response.read());conn.close();self.assertEqual(response.status,code,body)
    self.assertEqual(native.call_count,1)
  finally:server.shutdown();server.server_close();thread.join(5)
 def snapshot(self,focused,busy=False):
  o=self.o;off=o.Office();off.teams={'t':{'w':{}}};off.ensure_team=lambda sid:None;off.git_info=lambda sid:None;off.busy=lambda sid:False
  sessions=[{'id':'t','title':'one','seen':1,'deleted':False}]
  record={'jobs':[{'id':1,'status':'working' if busy else 'done','lead':'codex','text':'one'}],'tasks':[],'msgs':[]}
  changes=[]
  with ExitStack() as stack:
   for name,value in {'meta_view':lambda:{'sessions':copy.deepcopy(sessions)},'state_view_cached':lambda sid:(copy.deepcopy(record),2,None),'current_sid':lambda:'t','meta_later':lambda fn:changes.append(fn),'resource_view':lambda:None,'update_info':lambda:{},'task_scores':lambda data:{},'read_json_file':lambda *a,**k:{}}.items():stack.enter_context(patch.object(o,name,value))
   result=off.snapshot('t',mark_seen=focused)
  return result['sessions'][0],changes
 def test_minimized_current_completion_stays_unread(self):
  row,changes=self.snapshot(False);self.assertTrue(row['attention_unread']);self.assertEqual(row['notice_at'],2);self.assertEqual(row['seen'],1);self.assertEqual(changes,[])
 def test_focused_current_completion_is_read(self):
  row,changes=self.snapshot(True);self.assertFalse(row['attention_unread']);self.assertEqual(row['seen'],2);self.assertEqual(len(changes),1)
 def test_busy_session_does_not_flash_on_interim_report(self):
  row,_=self.snapshot(False,True);self.assertFalse(row['attention_unread'])
if __name__=='__main__':unittest.main()
