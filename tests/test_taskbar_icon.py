"""작업 표시줄 아이콘 연결과 창 열기 실패 처리를 확인해요."""
import base64,importlib.util,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch, Mock
SOURCE=Path(sys.argv.pop(1)) if len(sys.argv)>1 else Path(__file__).resolve().parents[1]/'payload'/'office.py'
class TaskbarIcon(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='office-taskbar-test-');self.addCleanup(self.tmp.cleanup)
  old=os.environ.copy();self.addCleanup(lambda:(os.environ.clear(),os.environ.update(old)))
  for key in list(os.environ):
   if key.startswith('OFFICE_'):del os.environ[key]
  os.environ['OFFICE_DIR']=self.tmp.name
  sp=importlib.util.spec_from_file_location('target',SOURCE);self.o=importlib.util.module_from_spec(sp);sp.loader.exec_module(self.o);self.o.server_log=lambda *a:None
 def test_native_script_uses_resource_path_and_scopes_office_windows(self):
  with patch.object(self.o.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout=b'updated:1\r\n',stderr=b'')) as run:
   self.assertTrue(self.o.configure_taskbar_icon())
  args,kw=run.call_args;script=base64.b64decode(args[0][-1]).decode('utf-16-le')
  self.assertIn('Read(store,5)',script);self.assertIn('Key(3)',script);self.assertIn('caption.EndsWith',script)
  self.assertIn('Chrome.localhost_/',script);self.assertIn('MSEdge.localhost_/',script)
  self.assertNotIn('WM_SETICON',script);self.assertNotIn('SetForegroundWindow',script)
  self.assertEqual(kw['stdin'],subprocess.DEVNULL);self.assertEqual(kw['timeout'],15)
 def test_missing_icon_is_nonfatal(self):
  with patch.object(self.o.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout=b'missing-icon\r\n',stderr=b'')):
   self.assertFalse(self.o.configure_taskbar_icon())
 def test_failed_script_is_not_success(self):
  with patch.object(self.o.subprocess,'run',return_value=subprocess.CompletedProcess([],1,stdout=b'updated:1',stderr=b'error')):
   self.assertFalse(self.o.configure_taskbar_icon())
 def test_unavailable_powershell_and_timeout_are_nonfatal(self):
  for err in (OSError('missing'),subprocess.TimeoutExpired([],12)):
   with self.subTest(error=type(err)),patch.object(self.o.subprocess,'run',side_effect=err):self.assertFalse(self.o.configure_taskbar_icon())
 def test_diagnostic_events_keep_result_and_exit(self):
  for rc,outcome,event in ((0,b'updated:2','taskbar.icon.updated:2'),(0,b'private garbage','taskbar.icon.unexpected-output'),(1,b'updated:1','taskbar.icon.failed.exit=1')):
   with self.subTest(rc=rc,outcome=outcome),patch.object(self.o,'server_log') as log,patch.object(self.o.subprocess,'run',return_value=subprocess.CompletedProcess([],rc,stdout=outcome,stderr=b'')):
    self.o.configure_taskbar_icon();log.assert_called_once_with(event)
 def test_detection_allows_slow_browser_start(self):
  with patch.object(self.o.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout=b'updated:0',stderr=b'')) as run:
   self.o.configure_taskbar_icon()
  script=base64.b64decode(run.call_args.args[0][-1]).decode('utf-16-le')
  self.assertIn('$i -lt 30;',script)
 def test_browser_open_starts_bounded_icon_worker(self):
  created=[]
  def make_thread(*args,**kwargs):
   obj=Mock();created.append((kwargs,obj));return obj
  with patch.object(Path,'exists',return_value=True),patch.object(self.o.subprocess,'Popen') as launch,patch.object(self.o.threading,'Thread',side_effect=make_thread):
   self.assertTrue(self.o.open_window(12345,app_only=True));self.assertEqual(launch.call_count,1)
  icon_threads=[(kw,obj) for kw,obj in created if kw.get('target')==self.o.configure_taskbar_icon]
  self.assertEqual(len(icon_threads),1);kw,obj=icon_threads[0];obj.start.assert_called_once();self.assertTrue(kw['daemon'])
 def test_icon_worker_start_failure_keeps_open_browser_successful(self):
  def make_thread(*args,**kwargs):
   obj=Mock()
   if kwargs.get('target')==self.o.configure_taskbar_icon:obj.start.side_effect=RuntimeError('no threads')
   return obj
  with patch.object(Path,'exists',return_value=True),patch.object(self.o.subprocess,'Popen'),patch.object(self.o.threading,'Thread',side_effect=make_thread):
   self.assertTrue(self.o.open_window(12345,app_only=True))
 def test_no_app_browser_does_not_start_icon_worker(self):
  with patch.object(Path,'exists',return_value=False),patch.object(self.o.threading,'Thread') as thread:
   self.assertFalse(self.o.open_window(12345,app_only=True));thread.assert_not_called()
 def test_failed_browsers_do_not_start_icon_worker(self):
  with patch.object(Path,'exists',return_value=True),patch.object(self.o.subprocess,'Popen',side_effect=OSError('failed')),patch.object(self.o.threading,'Thread') as thread:
   self.assertFalse(self.o.open_window(12345,app_only=True));thread.assert_not_called()
class PlatformAvailability(unittest.TestCase):
 def setUp(self):
  spec=importlib.util.spec_from_file_location('availability_target',SOURCE)
  self.o=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.o)
 def test_storage_requires_wsl_and_script(self):
  for mac,wsl,exists,expected in [(False,'',True,False),(False,'Ubuntu',False,False),(False,'Ubuntu',True,True),(True,'Ubuntu',True,False)]:
   with self.subTest(mac=mac,wsl=wsl,exists=exists),patch.object(self.o,'IS_MAC',mac),patch.dict(os.environ,{'WSL_DISTRO_NAME':wsl}),patch.object(self.o.Path,'is_file',return_value=exists),patch.object(self.o,'powershell_available',return_value=None):
    self.assertEqual(self.o.platform_features()['storage'],expected)
 def test_probe_is_cached_and_reports_denial(self):
  o=self.o
  with patch.object(o,'IS_MAC',False),patch.dict(os.environ,{'WSL_DISTRO_NAME':'Ubuntu'}),patch.object(o.subprocess,'run',return_value=subprocess.CompletedProcess([],1,b'',b'Access is denied')) as run,patch.object(o,'server_log') as log:
   self.assertFalse(o.powershell_available());self.assertFalse(o.powershell_available())
   self.assertEqual(run.call_count,1);self.assertEqual(run.call_args.kwargs['timeout'],2)
   self.assertIn('Access is denied',str(log.call_args))
 def test_probe_success_timeout_and_non_wsl(self):
  o=self.o
  with patch.object(o,'IS_MAC',False),patch.dict(os.environ,{'WSL_DISTRO_NAME':'Ubuntu'}):
   with patch.object(o.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'',b'')):
    self.assertTrue(o.powershell_available())
   o.POWERSHELL_CACHE.clear()
   with patch.object(o.subprocess,'run',side_effect=subprocess.TimeoutExpired([],2)):
    self.assertFalse(o.powershell_available())
  with patch.dict(os.environ,{'WSL_DISTRO_NAME':''}),patch.object(o.subprocess,'run') as run:
   self.assertIsNone(o.powershell_available());run.assert_not_called()
 def test_taskbar_error_has_stderr_and_exit(self):
  o=self.o
  with patch.object(o,'IS_MAC',False),patch.dict(os.environ,{'WSL_DISTRO_NAME':'Ubuntu'}),patch.object(o.subprocess,'run',return_value=subprocess.CompletedProcess([],7,b'',b'Access denied')),patch.object(o,'server_log') as log:
   self.assertFalse(o.configure_taskbar_icon());self.assertIn('exit=7',str(log.call_args));self.assertIn('Access denied',str(log.call_args))

 def test_explorer_error_has_stderr_and_exit(self):
  o=self.o
  with patch.object(o.subprocess,'run',return_value=subprocess.CompletedProcess([],5,b'',b'Access denied')),patch.object(o,'server_log') as log:
   self.assertEqual(o.focus_explorer_path('C:\\Users',True),'unknown')
   events=str(log.call_args_list);self.assertIn('exit=5',events);self.assertIn('Access denied',events)

if __name__=='__main__':unittest.main()
