"""점검 보관·선택·사본 검증·복구 요청의 실제 계약을 확인해요.
실행: python3 -I tests/test_update_review.py [저장소 경로]
네트워크·실제 설치·에이전트 프로세스는 사용하지 않아요.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else Path(__file__).resolve().parents[1]

def load(name, source):
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

u = load('review_module', ROOT / 'payload/office_updates.py')
BASE = '''import json,os,sys
from pathlib import Path
D=Path(os.environ['OFFICE_DIR'])/'.office'
def office_version(): return (Path(__file__).parent/'VERSION').read_text().strip()
class Office:
    def handle(self,p):
        D.mkdir(parents=True,exist_ok=True)
        (D/'ui-settings.json').write_text(json.dumps({'theme':p['theme']}))
        return {'ui':{'theme':p['theme']}}
if __name__=='__main__': print('Office help' if '--help' in sys.argv else 'Office '+office_version())
''' + ''.join(f'# line {i}\n' for i in range(25))

class Review(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='office-review-contract-')
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.prefix = self.temp / 'prefix'
        self.app = self.prefix / 'app'
        self.app.mkdir(parents=True)
        self.workspace = self.temp / 'workspace'
        self.workspace.mkdir()
        self.package = self.temp / 'incoming'
        self.payload = self.package / 'payload'
        self.payload.mkdir(parents=True)
        self.data = {'office.py': BASE.encode(), 'office.html': b'<p>base</p>\n', 'VERSION': b'1.0.0\n',
                     'office.ico': b'\x00ico', 'office-icon.png': b'\x00png'}
        originals = self.app / '.office-orig'
        originals.mkdir()
        for name, data in self.data.items():
            (self.app/name).write_bytes(data)
            (originals/name).write_bytes(data)
            (self.payload/name).write_bytes(data)
        manifest = {'version': '1.0.0', 'files': {name: hashlib.sha256(data).hexdigest() for name,data in self.data.items()}}
        (self.app/'.office-manifest.json').write_text(json.dumps(manifest))
        shutil.copyfile(ROOT/'payload/office_customizations.py', self.payload/'office_customizations.py')
        self.data['office_customizations.py'] = (self.payload/'office_customizations.py').read_bytes()
        (self.package/'install.sh').write_text('#!/bin/sh\nexit 0\n')
        (self.payload/'VERSION').write_text('1.0.1\n')
        self.sums()
        self.store = u.ReviewStore(self.temp/'review')

    def sums(self):
        (self.payload/'SHA256SUMS').write_text(''.join(hashlib.sha256((self.payload/n).read_bytes()).hexdigest()+'  '+n+'\n' for n in self.data))

    def create(self, conflict=False):
        if conflict:
            (self.app/'office.py').write_text(BASE.replace('# line 5\n','# custom 5\n'))
            (self.payload/'office.py').write_text(BASE.replace('# line 5\n','# upstream 5\n'))
            self.sums()
        return self.store.create(self.package, self.prefix, self.workspace)

    def merged(self):
        saved = self.create(True)
        saved = self.store.prepare_merge(saved['id'])
        candidate = self.store.candidate(saved)/'files'
        (candidate/'office.py').write_text(BASE.replace('# line 5\n','# custom 5; upstream 5\n'))
        return self.store.attach_job(saved,'test-session',1)

    def test_clean_plan_preserves_installed_files(self):
        before=u.tree_hashes(self.app)
        saved=self.create()
        self.assertEqual(saved['state'],'ready')
        self.assertEqual(before,u.tree_hashes(self.app))
        self.assertEqual(self.store.load()['id'],saved['id'])

    def test_conflict_is_persistent(self):
        saved=self.create(True)
        self.assertEqual(saved['state'],'conflict')
        self.assertEqual(self.store.view()['labels'],['앱 동작'])
        self.assertTrue(self.store.view()['can_merge'])

    def test_keep_changes_no_installed_file(self):
        saved=self.create(True)
        before=u.tree_hashes(self.app)
        self.store.defer(saved['id'])
        self.assertEqual(self.store.view()['state'],'deferred')
        self.assertEqual(before,u.tree_hashes(self.app))

    def test_invalid_tokens_are_rejected(self):
        self.create(True)
        for token in ('../bad','b'*32,3,[],{'id':'x'}):
            with self.subTest(token=token),self.assertRaises(u.ReviewError): self.store.load(token)

    def test_malformed_record_is_rejected(self):
        self.create(True)
        self.store.record.write_text('[]')
        with self.assertRaises(u.ReviewError): self.store.load()

    def test_symlink_record_is_rejected(self):
        self.create(True)
        self.store.record.unlink()
        self.store.record.symlink_to(self.app/'VERSION')
        with self.assertRaises(u.ReviewError): self.store.load()

    def test_current_change_invalidates_plan(self):
        saved=self.create(True)
        (self.app/'office.html').write_text('new edit')
        with self.assertRaises(u.ReviewError): self.store.recheck(saved)

    def test_original_change_invalidates_plan(self):
        saved=self.create(True)
        (self.app/'.office-orig/office.py').write_text('# altered base\n')
        with self.assertRaises(u.ReviewError): self.store.recheck(saved)

    def test_manifest_change_invalidates_plan(self):
        saved=self.create(True)
        manifest=self.app/'.office-manifest.json'
        manifest.write_text(manifest.read_text()+'\n')
        with self.assertRaises(u.ReviewError): self.store.recheck(saved)

    def test_package_changes_are_rejected(self):
        for name in ('install.sh','payload/office.py','payload/office_customizations.py','payload/SHA256SUMS'):
            with self.subTest(name=name):
                saved=self.create(True)
                target=self.store.package(saved)/name
                target.write_bytes(target.read_bytes()+b'\n# change')
                with self.assertRaises(u.ReviewError): self.store.recheck(saved)

    def test_package_symlink_rejected(self):
        (self.package/'link').symlink_to(self.app/'VERSION')
        with self.assertRaises(u.ReviewError): self.create()

    def test_unknown_choice_rejected(self):
        saved=self.create(True)
        with self.assertRaises(u.ReviewError): self.store.install_command(saved['id'],'skip-everything')

    def test_conflicts_need_explicit_choice(self):
        saved=self.create(True)
        with self.assertRaises(u.ReviewError): self.store.install_command(saved['id'])

    def test_upstream_choice_has_matching_plan(self):
        saved=self.create(True)
        current,command=self.store.install_command(saved['id'],'upstream')
        self.assertIn('--review-plan',command)
        decision=json.loads(Path(command[command.index('--resolution')+1]).read_text())
        self.assertEqual(decision['plan_id'],saved['plan_id'])
        self.assertEqual(decision['choice'],'upstream')

    def test_merged_requires_successful_check(self):
        saved=self.merged()
        with self.assertRaises(u.ReviewError): self.store.install_command(saved['id'],'merged')

    def test_candidate_markers_rejected(self):
        saved=self.create(True)
        saved=self.store.prepare_merge(saved['id'])
        self.store.attach_job(saved,'test-session',1)
        with self.assertRaises(u.ReviewError): self.store.verify_candidate(saved['id'])

    def test_successful_candidate_checks_cli_and_api(self):
        saved=self.merged()
        checked=self.store.verify_candidate(saved['id'])
        self.assertEqual(len(checked['checks']),3)
        self.assertTrue(self.store.view()['can_apply_merged'])
        _,command=self.store.install_command(saved['id'],'merged')
        decision=json.loads(Path(command[-1]).read_text())
        self.assertEqual(set(decision['candidate_sha']),{'office.py'})

    def test_checked_candidate_changes_rejected(self):
        saved=self.merged()
        self.store.verify_candidate(saved['id'])
        (self.store.candidate(saved)/'files/office.py').write_text(BASE)
        with self.assertRaises(u.ReviewError): self.store.install_command(saved['id'],'merged')

    def test_nonconflict_candidate_change_rejected(self):
        saved=self.merged()
        (self.store.candidate(saved)/'files/office.html').write_text('changed outside conflict')
        with self.assertRaises(u.ReviewError): self.store.verify_candidate(saved['id'])

    def test_new_candidate_file_rejected(self):
        saved=self.merged()
        (self.store.candidate(saved)/'files/surprise.py').write_text('print(1)')
        with self.assertRaises(u.ReviewError): self.store.verify_candidate(saved['id'])

    def test_invalid_python_rejected(self):
        saved=self.merged()
        (self.store.candidate(saved)/'files/office.py').write_text('def broken(')
        with self.assertRaises(u.ReviewError): self.store.verify_candidate(saved['id'])

    def test_failed_import_rejected(self):
        saved=self.merged()
        (self.store.candidate(saved)/'files/office.py').write_text('raise RuntimeError("bad")\n')
        with self.assertRaises(u.ReviewError): self.store.verify_candidate(saved['id'])

    def test_successful_exit_does_not_bypass_probe(self):
        saved=self.merged()
        (self.store.candidate(saved)/'files/office.py').write_text('import sys;sys.exit(0)\n')
        with self.assertRaises(u.ReviewError):self.store.verify_candidate(saved['id'])

    def test_missing_api_cannot_fake_cli_success(self):
        saved=self.merged()
        code='import sys\nif __name__=="__main__": print("Office 1.0.1");sys.exit(0)\nsys.exit(0)\n'
        (self.store.candidate(saved)/'files/office.py').write_text(code)
        with self.assertRaises(u.ReviewError):self.store.verify_candidate(saved['id'])

    def test_wrong_version_output_rejected(self):
        saved=self.merged()
        (self.store.candidate(saved)/'files/office.py').write_text(BASE.replace("else 'Office '+office_version()","else 'Office 0.0.0'"))
        with self.assertRaises(u.ReviewError):self.store.verify_candidate(saved['id'])

    def test_api_regression_rejected(self):
        saved=self.merged()
        (self.store.candidate(saved)/'files/office.py').write_text(BASE.replace("return {'ui':{'theme':p['theme']}}","return {}"))
        with self.assertRaises(u.ReviewError): self.store.verify_candidate(saved['id'])

    def test_retry_preserves_previous_candidate(self):
        saved=self.merged()
        previous=(self.store.candidate(saved)/'files/office.py').read_text()
        self.store.save(saved,state='failed')
        fresh=self.store.prepare_merge(saved['id'],retry=True)
        archived=list(self.store._directory(saved['id']).glob('previous-candidate-*'))
        self.assertEqual(len(archived),1)
        self.assertEqual((archived[0]/'files/office.py').read_text(),previous)
        self.assertIsNone(fresh.get('merge_job'))
        self.assertIn('<<<<<<<',(self.store.candidate(saved)/'files/office.py').read_text())

    def test_old_downloads_are_bounded(self):
        for index in range(5):self.create()
        folders=[p for p in self.store.root.iterdir() if p.is_dir()]
        self.assertLessEqual(len(folders),3)
        self.assertTrue(self.store.package(self.store.load()).is_dir())

    def test_cleanup_preserves_user_folder(self):
        self.store.root.mkdir()
        user=self.store.root/('c'*32);user.mkdir();(user/'user.txt').write_text('keep')
        for index in range(5):self.create()
        self.assertEqual((user/'user.txt').read_text(),'keep')

    def test_cleanup_preserves_merge_draft(self):
        saved=self.merged()
        candidate=self.store.candidate(saved)/'files/office.py'
        content=candidate.read_bytes()
        for index in range(5):self.create()
        self.assertEqual(candidate.read_bytes(),content)

    def test_candidate_can_only_be_created_once(self):
        saved=self.merged()
        with self.assertRaises(u.ReviewError): self.store.prepare_merge(saved['id'])

    def test_while_merging_defer_rejected(self):
        saved=self.merged()
        with self.assertRaises(u.ReviewError): self.store.defer(saved['id'])

    def test_blocked_local_edition_cannot_override(self):
        (self.app/'VERSION').write_text('1.0.0+local\n')
        saved=self.create()
        self.assertEqual(saved['state'],'blocked')
        with self.assertRaises(u.ReviewError): self.store.install_command(saved['id'],'upstream')

    def test_recorded_applied_update_offers_restore(self):
        saved=self.create()
        backup=self.prefix/'backups/1.0.0-before'
        shutil.copytree(self.app,backup)
        self.store.installed(saved,f'OFFICE_LOCAL: backup={backup}\n')
        self.assertTrue(self.store.view()['can_restore'])
        _,command=self.store.restore_command(saved['id'])
        self.assertIn('--restore-backup',command)

    def test_modified_after_install_cannot_restore(self):
        saved=self.create()
        backup=self.prefix/'backups/1.0.0-before'
        shutil.copytree(self.app,backup)
        self.store.installed(saved,f'OFFICE_LOCAL: backup={backup}\n')
        (self.app/'office.html').write_text('later user edit')
        with self.assertRaises(u.ReviewError): self.store.restore_command(saved['id'])

    def test_automatic_install_is_smoke_tested(self):
        saved=self.create()
        checks,hashes=self.store.validate_install(saved)
        self.assertEqual(len(checks),3)
        self.assertEqual(set(hashes),set(saved['plan']['incoming']))

    def test_upstream_choice_is_smoke_tested(self):
        saved=self.create(True)
        checks,hashes=self.store.validate_install(saved,'upstream')
        self.assertEqual(hashes['office.py'],saved['plan']['incoming']['office.py'])
        self.assertEqual(len(checks),3)

    def test_custom_only_broken_python_cannot_apply(self):
        (self.app/'office.py').write_text('def broken(')
        saved=self.create()
        with self.assertRaises(u.ReviewError): self.store.validate_install(saved)

    def test_custom_only_broken_api_cannot_apply(self):
        (self.app/'office.py').write_text(BASE.replace("return {'ui':{'theme':p['theme']}}","return {}"))
        saved=self.create()
        with self.assertRaises(u.ReviewError): self.store.validate_install(saved)

    def test_structured_result_preserves_spaces(self):
        saved=self.create()
        backup=self.prefix/'backups/version with spaces'
        shutil.copytree(self.app,backup)
        output='OFFICE_LOCAL: backup=bad truncated\nOFFICE_LOCAL_JSON: '+json.dumps({'backup':str(backup)})+'\n'
        self.store.installed(saved,output)
        self.assertEqual(self.store.load()['backup'],str(backup))

    def test_unmanaged_user_link_survives_snapshot(self):
        saved=self.create()
        (self.app/'user-link').symlink_to(self.workspace)
        backup=self.prefix/'backups/before'
        shutil.copytree(self.app,backup,symlinks=True)
        self.store.installed(saved,f'OFFICE_LOCAL: backup={backup}\n')
        self.store.restore_command(saved['id'])
        self.assertTrue((self.app/'user-link').is_symlink())

    def test_external_backup_cannot_restore(self):
        saved=self.create()
        backup=self.temp/'external'
        shutil.copytree(self.app,backup)
        self.store.installed(saved,f'OFFICE_LOCAL: backup={backup}\n')
        with self.assertRaises(u.ReviewError): self.store.restore_command(saved['id'])


class API(Review):
    # 같은 저장소 시험을 상속해 중복 실행하지 않아요.
    def setUp(self):
        super().setUp()
        with patch.dict(os.environ,{'OFFICE_DIR':str(self.workspace)}):
            self.o=load('review_api_module',ROOT/'payload/office.py')
        self.o._UPDATE_REVIEW_MODULE=u
        self.o.UPDATE.update(state='idle')
        self.office=SimpleNamespace(teams={})
        self.saved=self.create(True)
        self.patches=[patch.object(self.o,'update_review_store',return_value=self.store),
                      patch.object(self.o.threading,'Thread')]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)

    def call(self,action='update_resolve',**values):
        return self.o.handle_update_review(self.office,{'action':action,'review_id':self.saved['id'],**values},'test-session')

    def test_api_upstream_needs_confirmation(self):
        with self.assertRaises(self.o.UserError): self.call(choice='upstream')
        self.assertEqual(self.o.UPDATE['state'],'idle')

    def test_api_explicit_upstream_is_queued(self):
        self.call(choice='upstream',confirmed=True)
        self.assertEqual(self.o.UPDATE['state'],'running')
        self.o.threading.Thread.assert_called_once()

    def test_api_keep_is_synchronous(self):
        self.call(choice='keep')
        self.assertEqual(self.store.view()['state'],'deferred')
        self.o.threading.Thread.assert_not_called()

    def test_api_duplicate_submit_rejected(self):
        self.call(choice='upstream',confirmed=True)
        with self.assertRaises(self.o.UserError): self.call(choice='upstream',confirmed=True)

    def test_api_working_agent_blocks_apply(self):
        self.office.teams={'test':{'w':{'codex':SimpleNamespace(status='working')}}}
        with self.assertRaises(self.o.UserError): self.call(choice='upstream',confirmed=True)

    def test_api_wrong_id_rejected(self):
        with self.assertRaises(self.o.UserError):
            self.o.handle_update_review(self.office,{'action':'update_resolve','review_id':'b'*32,'choice':'keep'})

    def test_api_none_id_rejected(self):
        with self.assertRaises(self.o.UserError):
            self.o.handle_update_review(self.office,{'action':'update_resolve','choice':'keep'})

    def test_login_success_reenables_merge(self):
        self.store.save(self.saved,merge_available=False)
        self.o.update_review_login_ready()
        self.assertIs(self.store.view()['merge_available'],True)

    def test_login_success_does_not_race_install(self):
        self.store.save(self.saved,merge_available=False)
        self.o.UPDATE['state']='running'
        self.o.update_review_login_ready()
        self.assertIs(self.store.view()['merge_available'],False)

    def test_retry_while_previous_job_working_is_rejected(self):
        self.store.save(self.saved,state='merging',merge_job=1,merge_session='test')
        with patch.object(self.o,'update_review_job',return_value={'status':'working'}):
            with self.assertRaises(self.o.UserError):self.call(action='update_merge')
        self.o.threading.Thread.assert_not_called()

    def test_api_installer_gate_blocks_new_requests(self):
        office=self.o.Office()
        office.update_installing=True
        for action in ('job','msg','new_session','merge'):
            with self.subTest(action=action),self.assertRaises(self.o.UserError):
                office.handle({'action':action,'text':'new work'})

    def test_begin_install_blocks_working_worker(self):
        self.office.teams={'test':{'w':{'codex':SimpleNamespace(status='working')}}}
        with self.assertRaises(self.o.UserError): self.o.begin_update_install(self.office)
        self.assertFalse(getattr(self.office,'update_installing',False))

    def test_failed_installer_keeps_server(self):
        self.office.shutdown=Mock()
        result=subprocess.CompletedProcess([],1,stdout='',stderr='install failed')
        with patch.object(self.o,'install_paths',return_value=(str(self.prefix),str(self.workspace))),patch.object(self.o.subprocess,'run',return_value=result):
            # 점검은 실제 엔진으로 마친 뒤 설치 호출만 실패시켜요.
            command=(self.saved,['bash','stub'])
            with patch.object(self.store,'install_command',return_value=command),patch.object(self.store,'validate_install',return_value=([],{})):
                self.o.run_review_install(self.office,self.saved['id'],'upstream')
        self.office.shutdown.assert_not_called()
        self.assertFalse(self.office.update_installing)
        self.assertEqual(self.o.UPDATE['state'],'failed')

    def test_successful_installer_records_restart(self):
        self.office.shutdown=Mock()
        result=subprocess.CompletedProcess([],0,stdout='OFFICE_LOCAL: changed=0 backup=-\n',stderr='')
        with patch.object(self.o,'install_paths',return_value=(str(self.prefix),str(self.workspace))),patch.object(self.o.subprocess,'run',return_value=result),patch.object(self.o.time,'sleep'):
            with patch.object(self.store,'install_command',return_value=(self.saved,['bash','stub'])),patch.object(self.store,'validate_install',return_value=([],{})):
                self.o.run_review_install(self.office,self.saved['id'],'upstream')
        self.office.shutdown.assert_called_once()
        self.assertEqual(self.o.UPDATE['state'],'restarting')
        self.assertEqual(self.store.view()['state'],'applied')
        self.assertEqual(self.office.restart[1],str(self.prefix/'app/office.py'))

    def test_resolved_success_message_is_not_failure(self):
        text=self.o.local_changes_text('OFFICE_LOCAL: changed=1 conflicts=office.py merged=- backup=- resolved=upstream')
        self.assertIn('선택한 방식',text)
        self.assertNotIn('반영할 수 없어요',text)

# API는 공통 준비만 재사용해요. 상위 테스트를 다시 세지 않아요.
for _name in list(Review.__dict__):
    if _name.startswith('test_') and _name not in API.__dict__:
        setattr(API,_name,None)

if __name__=='__main__': unittest.main()
