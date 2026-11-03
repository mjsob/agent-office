"""Windows 도우미(office-win.exe) 빌드·명령·진행창·래퍼 대체 경로를 확인해요. WSL이 아니면 건너뛰어요.
실제 WSL 종료·디스크 줄이기·관리자 승인은 건드리지 않아요(진행창은 임시 폴더의 시험 모드만 써요)."""
import json,os,re,shutil,subprocess,sys,tempfile,time,unittest
from pathlib import Path
PAYLOAD=Path(__file__).resolve().parents[1]/'payload'
WSL=bool(os.environ.get('WSL_DISTRO_NAME')) and shutil.which('cmd.exe') and shutil.which('wslpath')
def cmd(*a,**k):
 k.setdefault('cwd','/mnt/c');k.setdefault('stdin',subprocess.DEVNULL)
 return subprocess.run(list(a),capture_output=True,text=True,timeout=k.pop('timeout',90),**k)
@unittest.skipUnless(WSL,'WSL에서만 돌려요')
class WindowsHelper(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  local=subprocess.run(['cmd.exe','/c','echo %LOCALAPPDATA%'],capture_output=True,text=True,cwd='/mnt/c').stdout.strip()
  cls.root=Path(subprocess.run(['wslpath',local],capture_output=True,text=True).stdout.strip())/'Temp'/('office-win-test-%d'%os.getpid())
  cls.root.mkdir(parents=True)
  cls.build=cls.root/'bin'
  r=subprocess.run(['bash',str(PAYLOAD/'office_win_build.sh'),str(PAYLOAD)],capture_output=True,text=True,env={**os.environ,'OFFICE_WIN_TARGET_DIR':str(cls.build)},cwd='/mnt/c')
  cls.built=json.loads(r.stdout.strip().splitlines()[-1])
  cls.exe=str(cls.build/'office-win.exe')
 @classmethod
 def tearDownClass(cls):shutil.rmtree(cls.root,ignore_errors=True)
 def win(self,p):return subprocess.run(['wslpath','-w',str(p)],capture_output=True,text=True).stdout.strip()
 def work(self,name):
  d=self.root/name;d.mkdir(exist_ok=True);return d
 def run_exe(self,*a,**k):return cmd(self.exe,*a,**k)
 def test_00_build_and_rebuild(self):
  self.assertTrue(self.built['ok'],self.built);self.assertEqual(self.built['status'],'built')
  r=subprocess.run(['bash',str(PAYLOAD/'office_win_build.sh'),str(PAYLOAD)],capture_output=True,text=True,env={**os.environ,'OFFICE_WIN_TARGET_DIR':str(self.build)},cwd='/mnt/c')
  self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])['status'],'current')
 def test_build_result_has_existing_exe_path(self):
  self.assertTrue(self.built['ok']);self.assertTrue(Path(self.built['path']).is_file(),self.built)
  r=subprocess.run(['bash',str(PAYLOAD/'office_win_build.sh'),str(PAYLOAD)],capture_output=True,text=True,env={**os.environ,'OFFICE_WIN_TARGET_DIR':str(self.build)},cwd='/mnt/c')
  d=json.loads(r.stdout.strip().splitlines()[-1]);self.assertEqual(d['status'],'current');self.assertTrue(Path(d['path']).is_file())
 def test_build_failures_are_reported_not_raised(self):
  env={**os.environ,'OFFICE_WIN_TARGET_DIR':str(self.root/'f1')}
  bad=self.work('badsrc');(bad/'office_win.cs').write_text('class {')
  r=subprocess.run(['bash',str(PAYLOAD/'office_win_build.sh'),str(bad)],capture_output=True,text=True,env=env,cwd='/mnt/c')
  d=json.loads(r.stdout.strip().splitlines()[-1]);self.assertFalse(d['ok']);self.assertEqual(d['status'],'failed');self.assertNotEqual(r.returncode,0)
  self.assertFalse((self.root/'f1'/'office-win.exe').exists());self.assertFalse((self.root/'f1'/'office-win.new.exe').exists())
  r=subprocess.run(['bash',str(PAYLOAD/'office_win_build.sh'),str(PAYLOAD)],capture_output=True,text=True,env={**env,'OFFICE_WIN_CSC':'/nope'},cwd='/mnt/c')
  self.assertIn('csc.exe',json.loads(r.stdout.strip().splitlines()[-1])['reason'])
 def test_probe_measure_status(self):
  p=json.loads(self.run_exe('probe').stdout);self.assertEqual((p['ok'],p['api']),(True,1))
  m=self.run_exe('measure','__no_such_distro__');self.assertNotEqual(m.returncode,0);self.assertIn('배포판',m.stderr)
  d=self.work('status');s=json.loads(self.run_exe('status','--dir',self.win(d)).stdout);self.assertEqual(s,{'running':False})
  bad=self.run_exe('status','--dir',self.win(d),'--cancel','x');self.assertNotEqual(bad.returncode,0)
 def test_measure_matches_powershell_when_available(self):
  distro=os.environ['WSL_DISTRO_NAME'];r=self.run_exe('measure',distro)
  if r.returncode:self.skipTest('이 배포판은 WSL 2가 아니거나 등록 정보를 못 읽었어요')
  d=json.loads(r.stdout);self.assertTrue(d['path'].lower().endswith('.vhdx'));self.assertGreater(d['logical_bytes'],0);self.assertGreaterEqual(d['logical_bytes'],d['allocated_bytes']);self.assertEqual(d['distro'],distro)
  if shutil.which('powershell.exe'):
   ps=cmd('powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',self.win(PAYLOAD/'office_win_legacy_measure.ps1'),'-Distro',distro)
   if ps.returncode==0:
    q=json.loads(ps.stdout);self.assertEqual((q['path'],q['logical_bytes'],q['allocated_bytes']),(d['path'],d['logical_bytes'],d['allocated_bytes']))
 def worker(self,mode,rid,seconds=1,after=None):
  d=self.work('w-'+rid);w=self.win(d)
  proc=subprocess.Popen([self.exe,'worker','--dir',w,'--vhdx','C:\\x.vhdx','--distro','Ubuntu','--request-id',rid,'--test-mode',mode,'--test-seconds',str(seconds)],cwd='/mnt/c',stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  if after:after(d,w)
  proc.wait(timeout=60);return d,json.loads((d/'result.json').read_text('utf-8-sig'))
 def test_worker_terminal_states(self):
  for mode,status in (('success','completed'),('failure','failed'),('cancel','cancelled'),('pending100','completed')):
   with self.subTest(mode=mode):
    d,res=self.worker(mode,'t'+mode)
    self.assertEqual((res['status'],res['request_id'],res['wsl_ready'],res['can_cancel'],res['test_mode']),(status,'t'+mode,True,False,True))
    self.assertEqual(res['recovery_required'],False)
    if status=='completed':self.assertEqual((res['reclaimed_gb'],res['percent']),(2,100))
    else:self.assertEqual(res['reclaimed_gb'],0)
    s=json.loads(self.run_exe('status','--dir',self.win(d)).stdout);self.assertFalse(s['running'])
 def test_external_cancel_while_running(self):
  def act(d,w):
   deadline=time.time()+20
   while time.time()<deadline:
    s=json.loads(self.run_exe('status','--dir',w).stdout)
    if s.get('status')=='running':break
    time.sleep(0.3)
   self.assertTrue(s['running'])
   self.assertNotEqual(self.run_exe('status','--dir',w,'--cancel','wrong').returncode,0)
   self.assertEqual(self.run_exe('status','--dir',w,'--cancel','tstallx').returncode,0)
  d,res=self.worker('stall','tstallx',0,act);self.assertEqual(res['status'],'cancelled')
 def test_worker_refuses_real_run_outside_admin_and_non_temp_test(self):
  d=self.work('refuse');w=self.win(d)
  r=self.run_exe('worker','--dir',w,'--vhdx','C:\\x.vhdx','--distro','U','--request-id','rr','--no-dialog');self.assertEqual(r.returncode,4)
  st=json.loads((d/'status.json').read_text('utf-8-sig'));self.assertEqual((st['status'],st['wsl_ready'],st['can_cancel']),('failed',True,False));self.assertIn('WSL은 끄지 않았어요',st['message'])
  r=self.run_exe('worker','--dir','C:\\Windows\\Temp\\..\\office-nope','--vhdx','C:\\x','--distro','U','--request-id','q','--test-mode','success');self.assertNotEqual(r.returncode,0)
 def test_launch_handshake_and_lock(self):
  d=self.work('launch');w=self.win(d)
  r=self.run_exe('launch','--dir',w,'--vhdx','C:\\x.vhdx','--distro','Ubuntu','--request-id','lh1','--test-mode','success','--test-seconds','1',timeout=90);self.assertEqual(r.returncode,0,r.stderr)
  deadline=time.time()+20
  while time.time()<deadline and not (d/'result.json').exists():time.sleep(0.3)
  self.assertEqual(json.loads((d/'result.json').read_text('utf-8-sig'))['status'],'completed')
  lease=json.loads((d/'launch-request.json').read_text('utf-8-sig'));self.assertEqual((lease['request_id'],lease['revoked']),('lh1',False))
  hold=subprocess.Popen([self.exe,'worker','--dir',w,'--vhdx','C:\\x','--distro','U','--request-id','hold','--test-mode','stall','--test-seconds','0'],cwd='/mnt/c')
  try:
   time.sleep(3);r=self.run_exe('launch','--dir',w,'--vhdx','C:\\x.vhdx','--distro','Ubuntu','--request-id','lh2','--test-mode','success');self.assertEqual(r.returncode,3)
   self.run_exe('status','--dir',w,'--cancel','hold')
  finally:hold.wait(timeout=30)
 def test_reopen_guards(self):
  d=self.work('reopen');w=self.win(d)
  (d/'restore-office.json').write_text(json.dumps({'request_id':'a1','available':False,'distro':'Ubuntu'}))
  self.assertEqual(self.run_exe('reopen','a1','--dir',w).returncode,0);self.assertEqual(json.loads((d/'reopen-status.json').read_text())['status'],'unavailable')
  self.assertEqual(self.run_exe('reopen','other','--dir',w).returncode,4)
  (d/'restore-office.json').write_text(json.dumps({'request_id':'a2','available':True,'distro':'Ubuntu','linux_user':'u','script_linux':'/x.sh','server_windows':'C:\\none.json'}))
  (d/'status.json').write_text(json.dumps({'request_id':'a2','status':'failed'}))
  self.assertEqual(self.run_exe('reopen','a2','5','--dir',w).returncode,0);self.assertEqual(json.loads((d/'reopen-status.json').read_text())['status'],'skipped')
  (d/'restore-office.json').write_text(json.dumps({'request_id':'a3','available':True,'distro':'Ubuntu','linux_user':'u"x','script_linux':'/x.sh','server_windows':'C:\\none.json'}))
  self.assertNotEqual(self.run_exe('reopen','a3','5','--dir',w).returncode,0)
 def test_recover_check_and_shortcut_in_test_folder(self):
  d=self.work('recover');w=self.win(d)
  (d/'target.json').write_text(json.dumps({'path':'C:\\nope.vhdx','distro':'Ubuntu','request_id':'r'}))
  self.assertNotEqual(self.run_exe('recover','--dir',w,'--check').returncode,0)
  (d/'x.vhdx').write_bytes(b'x');(d/'target.json').write_text(json.dumps({'path':self.win(d/'x.vhdx'),'distro':'Ubuntu','request_id':'r'}))
  c=json.loads(self.run_exe('recover','--dir',w,'--check').stdout);self.assertEqual((c['distro'],c['admin'],c['running']),('Ubuntu',False,False))
  (d/'start').mkdir();self.assertEqual(self.run_exe('install-recovery','--dir',w,'--shortcut-dir',self.win(d/'start')).returncode,0)
  self.assertEqual([p.name for p in (d/'start').iterdir()],['Office 디스크 복구.lnk'])
 def test_selftest_creates_real_temp_vhdx_and_preserves_existing(self):
  d=self.work('selftest-create');disk=d/'sample.vhdx'
  r=self.run_exe('selftest-make-vhdx',self.win(disk))
  self.assertEqual(r.returncode,0,r.stderr);self.assertTrue(json.loads(r.stdout)['ok'])
  self.assertGreater(disk.stat().st_size,0)
  before=disk.read_bytes()
  r=self.run_exe('selftest-make-vhdx',self.win(disk))
  self.assertNotEqual(r.returncode,0);self.assertEqual(disk.read_bytes(),before)
 def test_compact_api_rejects_non_temp_vhdx(self):
  self.assertNotEqual(self.run_exe('selftest-compact','C:\\Users\\x.vhdx').returncode,0)
class Wrapper(unittest.TestCase):
 def test_wrapper_syntax_and_no_private_paths(self):
  src=(PAYLOAD/'office_storage_run.sh').read_text()
  subprocess.run(['bash','-n',str(PAYLOAD/'office_storage_run.sh')],check=True);subprocess.run(['bash','-n',str(PAYLOAD/'office_win_build.sh')],check=True)
  self.assertNotIn('/projects/wsl-compact',src);self.assertNotIn('/resource-watch',src)
  for name in re.findall(r'office_win_legacy_\$n|office_win_legacy_(\w[\w.-]*)',src):pass
  for n in 'CompactOperation.cs OfficeConsole.cs common.ps1 compact.ps1 install-recovery.ps1 measure.ps1 recover.ps1 reopen-office.ps1 run.ps1 status.ps1'.split():
   self.assertTrue((PAYLOAD/('office_win_legacy_'+n)).is_file(),n)
 @unittest.skipUnless(WSL,'WSL에서만 돌려요')
 def test_powershell_host_requires_full_language_mode(self):
  self.assertIn('LanguageMode',(PAYLOAD/'office_storage_run.sh').read_text())
 def test_status_writes_are_atomic_replace(self):
  src=(PAYLOAD/'office_win.cs').read_text();self.assertIn('File.Replace(temp, target, null)',src);self.assertNotIn('File.Delete(target)',src)
  self.assertIn('phase_elapsed_seconds',src);self.assertIn('phase_seconds',src)
 @unittest.skipUnless(WSL,'WSL에서만 돌려요')
 def test_neither_host_gives_clear_exit_5(self):
  with tempfile.TemporaryDirectory() as t:
   fake=Path(t)/'powershell.exe';fake.write_text('#!/bin/sh\necho blocked >&2\nexit 1\n');fake.chmod(0o755)
   env={**os.environ,'PATH':t+':'+os.environ['PATH'],'OFFICE_WIN_HOST':'ps'}
   r=subprocess.run(['bash',str(PAYLOAD/'office_storage_run.sh'),'--dry-run'],capture_output=True,text=True,env=env,cwd='/tmp',stdin=subprocess.DEVNULL)
   self.assertEqual(r.returncode,5);self.assertIn('쓸 수 없어요',r.stderr)
   r=subprocess.run(['bash',str(PAYLOAD/'office_storage_run.sh'),'--bogus'],capture_output=True,text=True,env=env,cwd='/tmp');self.assertEqual(r.returncode,64)
 @unittest.skipUnless(WSL,'WSL에서만 돌려요')
 def test_dry_run_uses_helper_then_falls_back_to_powershell(self):
  local=subprocess.run(['cmd.exe','/c','echo %LOCALAPPDATA%'],capture_output=True,text=True,cwd='/mnt/c').stdout.strip()
  if not (Path(subprocess.run(['wslpath',local],capture_output=True,text=True).stdout.strip())/'Office'/'office-win.exe').exists():self.skipTest('이 PC에 도우미가 아직 설치되지 않았어요')
  outs={}
  for host in ('exe','ps'):
   r=subprocess.run(['bash',str(PAYLOAD/'office_storage_run.sh'),'--dry-run'],capture_output=True,text=True,env={**os.environ,'OFFICE_WIN_HOST':host},cwd='/tmp',stdin=subprocess.DEVNULL)
   if r.returncode:self.skipTest('%s 경로를 이 PC에서 쓸 수 없어요: %s'%(host,r.stderr.strip()))
   outs[host]=json.loads(r.stdout)
  for k in ('path','logical_bytes','allocated_bytes','distro','running'):self.assertEqual(outs['exe'][k],outs['ps'][k])
if __name__=='__main__':unittest.main()
