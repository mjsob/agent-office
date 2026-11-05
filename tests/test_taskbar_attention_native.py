"""실제 Windows 창으로 작업 표시줄 알림의 선택·중지를 확인해요."""
import json,os,shutil,subprocess,tempfile,time,unittest,uuid
from pathlib import Path
REPO=Path(__file__).resolve().parents[1]
if not (REPO/'payload').is_dir():REPO=Path('/home/chan/agent-office')
def win(p):return subprocess.check_output(['wslpath','-w',str(p)],text=True).strip()
@unittest.skipUnless(os.environ.get('WSL_DISTRO_NAME') and shutil.which('cmd.exe'),'WSL에서만 확인해요')
class NativeAttention(unittest.TestCase):
 def test_unique_browser_minimized_start_stop_and_decoys(self):
  local=subprocess.check_output(['cmd.exe','/d','/c','echo %LOCALAPPDATA%'],cwd='/mnt/c',text=True).strip()
  root=Path(subprocess.check_output(['wslpath',local],text=True).strip())/'Temp'/('OfficeAttention165-'+uuid.uuid4().hex[:10]);root.mkdir()
  try:
   env={**os.environ,'OFFICE_WIN_TARGET_DIR':str(root/'helper')}
   result=subprocess.run(['bash',str(REPO/'payload/office_win_build.sh'),str(REPO/'payload')],capture_output=True,text=True,env=env,cwd='/mnt/c',timeout=30)
   self.assertEqual(result.returncode,0,result.stdout)
   fixture=Path(__file__).with_name('taskbar_attention_window.cs')
   if not fixture.exists():fixture=Path('/tmp/req165/AttentionWindow.cs')
   csc='/mnt/c/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe'
   r=subprocess.run([csc,'/nologo','/target:winexe','/r:System.Windows.Forms.dll','/out:'+win(root/'chrome.exe'),win(fixture)],capture_output=True,cwd='/mnt/c',timeout=30);self.assertEqual(r.returncode,0,r.stdout)
   exe=str(root/'helper/office-win.exe')
   def call(mode,title):
    r=subprocess.run([exe,'attention',mode,title],capture_output=True,text=True,cwd='/mnt/c',timeout=15);self.assertEqual(r.returncode,0,r.stderr);return json.loads(r.stdout)
   for count,brand,matched in [(1,'Chrome.localhost_/',1),(2,'Chrome.localhost_/',2),(1,'Chrome.other_app',0)]:
    with self.subTest(windows=count,brand=brand):
     title='Office attention '+uuid.uuid4().hex[:8]+' — Office';ready=root/'ready.txt';ready.unlink(missing_ok=True);Path(str(ready)+'.stop').unlink(missing_ok=True)
     proc=subprocess.Popen([str(root/'chrome.exe'),win(ready),title,brand,str(count)],cwd='/mnt/c')
     try:
      deadline=time.monotonic()+10
      while not ready.exists() and proc.poll() is None and time.monotonic()<deadline:time.sleep(.05)
      self.assertTrue(ready.exists());self.assertEqual(call('inspect',title)['matched'],matched)
      start=call('start',title);self.assertEqual(start['matched'],matched)
      if matched==1:
       self.assertTrue(start['flashing']);self.assertEqual(start['flags'],14);self.assertFalse(start['foreground'])
       stop=call('stop',title);self.assertFalse(stop['flashing']);self.assertEqual(stop['flags'],0)
     finally:
      Path(str(ready)+'.stop').write_text('stop')
      try:proc.wait(timeout=5)
      except subprocess.TimeoutExpired:proc.wait(timeout=15)
   for mode,title in [('invalid','Office'),('start','not Office'),('start','x\n — Office')]:
    r=subprocess.run([exe,'attention',mode,title],cwd='/mnt/c',capture_output=True);self.assertEqual(r.returncode,64)
  finally:
   # Windows가 닫힌 실행 파일의 핸들을 놓을 때까지만 짧게 다시 시도해요.
   for attempt in range(20):
    try:shutil.rmtree(root);break
    except OSError:
     if attempt==19:raise
     time.sleep(.1)
if __name__=='__main__':unittest.main()
