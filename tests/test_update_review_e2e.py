"""실제 설치기와 앱 업데이트 흐름을 격리된 설치본에서 이어 확인해요.
실행: python3 -I tests/test_update_review_e2e.py [저장소 경로]
다운로드·에이전트만 가짜로 바꿔요. 실제 사용자 설치는 건드리지 않아요.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

ROOT=Path(sys.argv.pop(1)).resolve() if len(sys.argv)>1 and not sys.argv[1].startswith('-') else Path(__file__).resolve().parents[1]
sys.dont_write_bytecode=True

class EndToEnd(unittest.TestCase):
 def setUp(self):
  tmp=tempfile.TemporaryDirectory(prefix='office-review-e2e-');self.addCleanup(tmp.cleanup)
  self.root=Path(tmp.name); self.home=self.root/'home with spaces';self.home.mkdir()
  self.workspace=self.home/'workspace';self.workspace.mkdir()
  self.prefix=self.home/'application';self.app=self.prefix/'app'
  self.source=self.root/'old-package';self.source.mkdir()
  shutil.copyfile(ROOT/'install.sh',self.source/'install.sh')
  shutil.copytree(ROOT/'payload',self.source/'payload',ignore=shutil.ignore_patterns('__pycache__'))
  (self.source/'payload/VERSION').write_text('0.10.0\n')
  self.sums(self.source)
  self.commands=self.root/'commands';self.commands.mkdir()
  for name in ('claude','codex'):
   script=self.commands/name;script.write_text('#!/bin/sh\nprintf "stub-cli\\n"\n');script.chmod(0o700)
  self.env={k:v for k,v in os.environ.items() if not k.startswith('OFFICE_') and k!='WSL_DISTRO_NAME'}
  self.env.update(HOME=str(self.home),PATH=str(self.commands)+':'+os.environ['PATH'],PYTHONDONTWRITEBYTECODE='1')
  result=self.install(self.source)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.target=self.root/'new-package';shutil.copytree(self.source,self.target)
  (self.target/'payload/VERSION').write_text('0.10.1\n')
  with patch.dict(os.environ,{**self.env,'OFFICE_DIR':str(self.workspace)},clear=True):
   spec=importlib.util.spec_from_file_location('_office_e2e',self.app/'office.py')
   self.o=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.o)
  self.office=SimpleNamespace(teams={},shutdown=Mock())
  self.o.UPDATE.update(state='running',latest='0.10.1')
  self.patches=[patch.dict(os.environ,{**self.env,'OFFICE_DIR':str(self.workspace)},clear=True),patch.object(self.o.time,'sleep')]
  for item in self.patches: item.start();self.addCleanup(item.stop)

 def sums(self,package):
  files=sorted(p.name for p in (package/'payload').iterdir() if p.is_file() and p.name!='SHA256SUMS')
  (package/'payload/SHA256SUMS').write_text(''.join(hashlib.sha256((package/'payload'/name).read_bytes()).hexdigest()+'  '+name+'\n' for name in files))

 def install(self,package,*extra):
  return subprocess.run(['bash',str(package/'install.sh'),'--apply','--skip-login','--no-launch','--no-shortcut','--prefix',str(self.prefix),'--workspace',str(self.workspace),*extra],env=self.env,capture_output=True,text=True,timeout=40)

 def run_download(self):
  self.sums(self.target)
  stream=io.BytesIO()
  with tarfile.open(fileobj=stream,mode='w:gz') as archive:archive.add(self.target,arcname='office-dist')
  blob=stream.getvalue()
  def download(url,**kw):return io.BytesIO((hashlib.sha256(blob).hexdigest()+' archive.tar.gz').encode() if url.endswith('.sha256') else blob)
  with patch.object(self.o.urlrequest,'urlopen',side_effect=download):self.o.run_update(self.office)
  return self.o.update_review_store()

 def conflict(self):
  html=self.app/'office.html'; target=self.target/'payload/office.html'
  self.assertIn('<title>Office</title>',html.read_text())
  html.write_text(html.read_text().replace('<title>Office</title>','<title>My custom office</title>'))
  target.write_text(target.read_text().replace('<title>Office</title>','<title>New release office</title>'))

 def files(self):
  return {str(p.relative_to(self.prefix)):p.read_bytes() for p in self.prefix.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.name not in ('.install.lock',)}

 def test_auto_update_keeps_custom_function_and_new_release(self):
  local=self.app/'office.py';local.write_text(local.read_text()+'\ndef user_feature(): return "custom-feature"\n')
  target=self.target/'payload/office.py';target.write_text(target.read_text().replace('class UserError(Exception):','def release_feature(): return "new-feature"\n\nclass UserError(Exception):',1))
  store=self.run_download()
  self.assertEqual(self.o.UPDATE['state'],'restarting')
  self.assertEqual((self.app/'VERSION').read_text().strip(),'0.10.1')
  self.assertIn('def user_feature()',local.read_text());self.assertIn('def release_feature()',local.read_text())
  self.assertNotIn('def user_feature()',(self.app/'.office-orig/office.py').read_text())
  self.assertTrue(store.view()['can_restore']);self.office.shutdown.assert_called_once()

 def test_conflict_then_keep_preserves_whole_install(self):
  self.conflict();before=self.files();store=self.run_download()
  self.assertEqual(self.o.UPDATE['state'],'conflict');self.assertEqual(before,self.files())
  self.o.handle_update_review(self.office,{'action':'update_resolve','review_id':store.load()['id'],'choice':'keep'})
  self.assertEqual(before,self.files());self.assertEqual(store.view()['state'],'deferred')
  self.office.shutdown.assert_not_called()

 def test_upstream_choice_keeps_other_customization_and_backup(self):
  self.conflict();local=self.app/'office.py';local.write_text(local.read_text()+'\ndef untouched_custom_feature(): return 42\n')
  store=self.run_download();saved=store.load()
  self.o.run_review_install(self.office,saved['id'],'upstream')
  self.assertEqual(self.o.UPDATE['state'],'restarting')
  self.assertIn('New release office',(self.app/'office.html').read_text())
  self.assertIn('untouched_custom_feature',local.read_text())
  backup=Path(store.load()['backup'])
  self.assertIn('My custom office',(backup/'office.html').read_text())
  self.assertIn('untouched_custom_feature',(backup/'office.py').read_text())
  self.assertIn(' ',str(backup))

 def test_merged_candidate_is_applied(self):
  self.conflict();store=self.run_download();saved=store.load()
  saved=store.prepare_merge(saved['id'])
  candidate=store.candidate(saved)/'files/office.html'
  candidate.write_text((self.target/'payload/office.html').read_text().replace('New release office','My custom office with new release'))
  store.attach_job(saved,'test',1);store.verify_candidate(saved['id'])
  self.o.run_review_install(self.office,saved['id'],'merged')
  self.assertEqual(self.o.UPDATE['state'],'restarting')
  self.assertIn('My custom office with new release',(self.app/'office.html').read_text())
  self.assertIn('New release office',(self.app/'.office-orig/office.html').read_text())

 def test_updated_then_restore_then_update_again(self):
  self.conflict();store=self.run_download();token=store.load()['id']
  self.o.run_review_install(self.office,token,'upstream')
  self.assertEqual(self.o.UPDATE['state'],'restarting')
  self.office.shutdown.reset_mock();self.o.UPDATE['state']='running'
  self.o.run_review_install(self.office,token,restoring=True)
  self.assertEqual(self.o.UPDATE['state'],'restarting')
  self.assertEqual((self.app/'VERSION').read_text().strip(),'0.10.0')
  self.assertIn('My custom office',(self.app/'office.html').read_text())
  self.o.UPDATE['state']='running'
  store=self.run_download();self.assertEqual(store.view()['state'],'conflict')
  self.o.run_review_install(self.office,store.load()['id'],'upstream')
  self.assertEqual((self.app/'VERSION').read_text().strip(),'0.10.1')

 def test_changed_after_review_is_not_overwritten(self):
  self.conflict();store=self.run_download();token=store.load()['id']
  (self.app/'office.html').write_text((self.app/'office.html').read_text()+'\n<!-- new edit -->')
  before=self.files();self.o.run_review_install(self.office,token,'upstream')
  self.assertEqual(self.o.UPDATE['state'],'failed');self.assertEqual(before,self.files())
  self.office.shutdown.assert_not_called()

 def test_auto_custom_api_regression_is_rejected_before_install(self):
  local=self.app/'office.py'
  local.write_text(local.read_text().replace('return {"ui": {"theme": theme}}','return {"ui": {"theme": "wrong"}}'))
  before=self.files();self.run_download()
  self.assertEqual(self.o.UPDATE['state'],'failed');self.assertEqual(before,self.files())
  self.office.shutdown.assert_not_called()

 def test_persisted_review_survives_server_reload(self):
  self.conflict();store=self.run_download();token=store.load()['id']
  spec=importlib.util.spec_from_file_location('_office_e2e_reloaded',self.app/'office.py')
  reloaded=importlib.util.module_from_spec(spec);spec.loader.exec_module(reloaded)
  info=reloaded.update_info()
  self.assertEqual(info['state'],'conflict');self.assertEqual(info['review']['id'],token)

if __name__=='__main__':unittest.main()
