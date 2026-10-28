"""작업 표시줄 아이콘 연결과 창 열기 실패 처리를 확인해요."""
import base64,importlib.util,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
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
  self.assertEqual(kw['stdin'],subprocess.DEVNULL);self.assertEqual(kw['timeout'],12)
 def test_missing_icon_is_nonfatal(self):
  with patch.object(self.o.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout=b'missing-icon\r\n',stderr=b'')):
   self.assertFalse(self.o.configure_taskbar_icon())
 def test_failed_script_is_not_success(self):
  with patch.object(self.o.subprocess,'run',return_value=subprocess.CompletedProcess([],1,stdout=b'updated:1',stderr=b'error')):
   self.assertFalse(self.o.configure_taskbar_icon())
 def test_unavailable_powershell_and_timeout_are_nonfatal(self):
  for err in (OSError('missing'),subprocess.TimeoutExpired([],12)):
   with self.subTest(error=type(err)),patch.object(self.o.subprocess,'run',side_effect=err):self.assertFalse(self.o.configure_taskbar_icon())
 def test_browser_open_starts_bounded_icon_worker(self):
  with patch.object(Path,'exists',return_value=True),patch.object(self.o.subprocess,'Popen') as launch,patch.object(self.o.threading,'Thread') as thread:
   self.assertTrue(self.o.open_window(12345,app_only=True));self.assertEqual(launch.call_count,1);thread.return_value.start.assert_called_once()
   self.assertEqual(thread.call_args.kwargs['target'],self.o.configure_taskbar_icon);self.assertTrue(thread.call_args.kwargs['daemon'])
 def test_icon_worker_start_failure_keeps_open_browser_successful(self):
  with patch.object(Path,'exists',return_value=True),patch.object(self.o.subprocess,'Popen'),patch.object(self.o.threading,'Thread') as thread:
   thread.return_value.start.side_effect=RuntimeError('no threads');self.assertTrue(self.o.open_window(12345,app_only=True))
 def test_no_app_browser_does_not_start_icon_worker(self):
  with patch.object(Path,'exists',return_value=False),patch.object(self.o.threading,'Thread') as thread:
   self.assertFalse(self.o.open_window(12345,app_only=True));thread.assert_not_called()
 def test_failed_browsers_do_not_start_icon_worker(self):
  with patch.object(Path,'exists',return_value=True),patch.object(self.o.subprocess,'Popen',side_effect=OSError('failed')),patch.object(self.o.threading,'Thread') as thread:
   self.assertFalse(self.o.open_window(12345,app_only=True));thread.assert_not_called()
if __name__=='__main__':unittest.main()
