"""업데이트가 사용자 수정을 보존하는지 격리된 설치 폴더에서 확인해요.
실행: python3 -I tests/test_install_customizations.py [저장소 경로]
CLI는 가짜 명령을 써요. 로그인·서버·네트워크·실제 설치 폴더는 사용하지 않아요.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else Path(__file__).resolve().parents[1]
FILES = ('office.py','office.html','office.ico','office-icon.png','VERSION')


class InstallCustomizations(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='office-custom-preserve-')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.home = self.folder/'test-home'
        self.home.mkdir()
        self.pkg = self.folder/'package'
        payload = self.pkg/'payload'
        payload.mkdir(parents=True)
        shutil.copyfile(ROOT/'install.sh', self.pkg/'install.sh')
        data = {'office.py':b'print("base")\n# one\n# two\n# three\n# four\n# five\n# six\n# seven\n# eight\n# nine\n# ten\n', 'office.html':b'<p>base</p>\n', 'office.ico':b'\x00base-icon', 'office-icon.png':b'\x00base-png', 'VERSION':b'1.0.0\n'}
        for name, content in data.items():
            (payload/name).write_bytes(content)
        commands = self.folder/'commands'
        commands.mkdir()
        for name in ('codex','claude'):
            stub = commands/name
            stub.write_text('#!/bin/sh\nprintf "test-cli\\n"\n')
            stub.chmod(0o700)
        self.env={k:v for k,v in os.environ.items() if not k.startswith('OFFICE_') and k!='WSL_DISTRO_NAME'}
        self.env.update(HOME=str(self.home),PATH=str(commands)+':'+os.environ['PATH'])
        self.prefix = self.home/'.local/share/office-app'
        self.app = self.prefix/'app'
        self.bin = self.prefix/'bin'
        self.global_launcher = self.home/'.local/bin/office'

    def install(self):
        payload=self.pkg/'payload'
        (payload/'SHA256SUMS').write_text(''.join(hashlib.sha256((payload/n).read_bytes()).hexdigest()+'  '+n+'\n' for n in FILES))
        return subprocess.run(['bash',str(self.pkg/'install.sh'),'--apply','--skip-login','--no-launch','--no-shortcut'],env=self.env,capture_output=True,text=True,timeout=20)

    def first(self):
        result=self.install()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def bump(self):
        (self.pkg/'payload/VERSION').write_text('1.0.1\n')
        p=self.pkg/'payload/office.py'
        p.write_text(p.read_text().replace('# ten','# upstream ten'))

    def snapshot(self):
        paths=[self.app,self.bin,self.home/'.local/bin',self.home/'agent-office']
        result={}
        for parent in paths:
            if not parent.exists():
                continue
            for p in sorted(parent.rglob('*')):
                if p.is_symlink():
                    result[str(p.relative_to(self.home))]=('link',os.readlink(p))
                elif p.is_file():
                    result[str(p.relative_to(self.home))]=('file',p.read_bytes(),p.stat().st_mode&0o777)
                else:
                    result[str(p.relative_to(self.home))]=('dir',)
        return result

    def blocked_unchanged(self):
        before=self.snapshot()
        result=self.install()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(before,self.snapshot())
        return result

    def test_fresh_install_and_regular_upgrade(self):
        self.first()
        self.bump()
        result=self.install()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.app/'VERSION').read_text(),'1.0.1\n')
        self.assertIn('# upstream ten',(self.app/'office.py').read_text())

    def test_disjoint_edits_preserve_custom_and_new_release(self):
        self.first()
        p=self.app/'office.py'
        p.write_text(p.read_text().replace('# one','# custom one'))
        self.bump()
        result=self.install()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('# custom one',p.read_text())
        self.assertIn('# upstream ten',p.read_text())
        # 새 배포의 원본 기준과 사용자 수정은 다음 업데이트에서도 구분해요.
        original=self.app/'.office-orig/office.py'
        self.assertNotIn('# custom one',original.read_text())
        self.assertIn('# upstream ten',original.read_text())

    def test_conflicting_text_preserves_entire_install(self):
        self.first()
        p=self.app/'office.py'
        p.write_text(p.read_text().replace('# ten','# custom ten'))
        self.bump()
        self.blocked_unchanged()

    def test_binary_edit_preserves_entire_install(self):
        self.first()
        (self.app/'office.ico').write_bytes(b'\x00custom-icon')
        self.bump()
        self.blocked_unchanged()

    def test_deleted_file_stays_deleted(self):
        self.first()
        (self.app/'office.html').unlink()
        self.bump()
        self.blocked_unchanged()

    def test_file_replaced_with_directory_stays_directory(self):
        self.first()
        p=self.app/'office.html'
        p.unlink();p.mkdir();(p/'custom.txt').write_text('keep')
        self.bump()
        self.blocked_unchanged()

    def test_dangling_file_symlink_is_preserved(self):
        self.first()
        p=self.app/'office.html'
        p.unlink();p.symlink_to(self.folder/'missing-custom')
        self.bump()
        self.blocked_unchanged()

    def test_invalid_json_manifest_preserves_entire_install(self):
        self.first()
        (self.app/'.office-manifest.json').write_text('{bad')
        self.bump()
        self.blocked_unchanged()

    def test_wrong_manifest_shape_preserves_entire_install(self):
        self.first()
        (self.app/'.office-manifest.json').write_text('[]')
        self.bump()
        self.blocked_unchanged()

    def test_incomplete_manifest_preserves_entire_install(self):
        self.first()
        p=self.app/'.office-manifest.json'
        manifest=json.loads(p.read_text());del manifest['files']['office.html']
        p.write_text(json.dumps(manifest))
        (self.app/'office.html').write_text('<p>custom</p>')
        self.bump()
        self.blocked_unchanged()

    def test_incomplete_manifest_with_deleted_file_is_preserved(self):
        self.first()
        p=self.app/'.office-manifest.json'
        manifest=json.loads(p.read_text());del manifest['files']['office.html']
        p.write_text(json.dumps(manifest))
        (self.app/'office.html').unlink()
        self.bump()
        self.blocked_unchanged()

    def test_old_global_launcher_without_baseline_is_kept(self):
        self.first()
        p=self.app/'.office-manifest.json'
        manifest=json.loads(p.read_text())
        p.write_text(json.dumps({k:manifest[k] for k in ('version','files')}))
        launcher=self.global_launcher
        launcher.write_text(launcher.read_text()+'\n# global custom\n')
        before=launcher.read_bytes()
        self.bump()
        self.install()
        self.assertEqual(launcher.read_bytes(),before)

    def test_missing_baseline_preserves_entire_install(self):
        self.first()
        (self.app/'.office-orig/office.py').unlink()
        p=self.app/'office.py';p.write_text('# custom\n'+p.read_text())
        self.bump()
        self.blocked_unchanged()

    def test_tampered_baseline_preserves_entire_install(self):
        self.first()
        (self.app/'.office-orig/office.py').write_text('# counterfeit\n')
        p=self.app/'office.py';p.write_text('# custom\n'+p.read_text())
        self.bump()
        self.blocked_unchanged()

    def test_invalid_python_merge_preserves_entire_install(self):
        self.first()
        p=self.app/'office.py';p.write_text('def broken(\n'+p.read_text())
        self.bump()
        self.blocked_unchanged()

    def test_legacy_install_with_custom_files_is_preserved(self):
        self.first()
        (self.app/'.office-manifest.json').unlink()
        (self.app/'office.html').write_text('custom legacy')
        self.bump()
        self.blocked_unchanged()

    def test_local_edition_is_not_replaced_by_public(self):
        (self.pkg/'payload/VERSION').write_text('1.0.0+local\n')
        self.first()
        self.bump()
        self.blocked_unchanged()

    def test_custom_version_marker_is_preserved(self):
        self.first()
        (self.app/'VERSION').write_text('1.0.0+mine\n')
        self.bump()
        self.blocked_unchanged()

    def test_custom_private_launcher_is_preserved(self):
        self.first()
        p=self.bin/'office';p.write_text(p.read_text()+'\n# mine\n')
        self.bump()
        self.blocked_unchanged()

    def test_custom_global_launcher_is_preserved(self):
        self.first()
        p=self.global_launcher
        if p.is_symlink():
            p.unlink();p.write_text('#!/bin/sh\n# custom wrapper\n')
        else:
            p.write_text(p.read_text()+'\n# mine\n')
        before=p.read_bytes()
        self.bump()
        result=self.install()
        self.assertEqual(p.read_bytes(),before)
        if result.returncode:
            self.assertEqual((self.app/'VERSION').read_text(),'1.0.0\n')

    def test_custom_console_init_is_preserved(self):
        self.first()
        p=self.bin/'office-console.bashrc';p.write_text(p.read_text()+'\n# mine\n')
        self.bump()
        self.blocked_unchanged()

    def test_unknown_files_and_user_configuration_are_preserved(self):
        self.first()
        custom=self.app/'custom-plugin.py';custom.write_text('# keep\n')
        config=self.home/'agent-office/AGENTS.md';config.write_text('내 설정\n')
        userconfig=self.home/'.config/office/my-settings.json'
        userconfig.parent.mkdir(parents=True);userconfig.write_text('{"custom":true}\n')
        self.bump()
        result=self.install()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(custom.read_text(),'# keep\n')
        self.assertEqual(config.read_text(),'내 설정\n')
        self.assertEqual(userconfig.read_text(),'{"custom":true}\n')

    def test_old_launcher_without_baseline_is_kept(self):
        self.first()
        p=self.app/'.office-manifest.json'
        manifest=json.loads(p.read_text())
        p.write_text(json.dumps({k:manifest[k] for k in ('version','files')}))
        launcher=self.bin/'office';launcher.write_text(launcher.read_text()+'\n# old custom\n')
        before=launcher.read_bytes()
        self.bump()
        self.install()
        self.assertEqual(launcher.read_bytes(),before)

@unittest.skipUnless(shutil.which('powershell.exe') and shutil.which('wslpath'), 'Windows 바로가기 시험 도구가 없어요')
class WindowsShortcuts(unittest.TestCase):
    @staticmethod
    def powershell(script):
        import base64
        encoded=base64.b64encode(script.encode('utf-16-le')).decode()
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-EncodedCommand',encoded],capture_output=True,timeout=15)
        if result.returncode:
            raise AssertionError(result.stdout.decode(errors='replace')+result.stderr.decode(errors='replace'))
        return result.stdout.decode('utf-8',errors='replace').strip()

    @staticmethod
    def quote(value):
        return "'"+str(value).replace("'","''")+"'"

    def windows(self,path):
        return subprocess.check_output(['wslpath','-w',str(path)],text=True).strip()

    def setUp(self):
        win_temp=self.powershell('[IO.Path]::GetTempPath()')
        host_temp=subprocess.check_output(['wslpath','-u',win_temp],text=True).strip()
        temporary=tempfile.TemporaryDirectory(prefix='office-req146-shortcuts-',dir=host_temp)
        self.addCleanup(temporary.cleanup)
        self.folder=Path(temporary.name)
        for child in ('Desktop','Programs','LocalAppData'):
            (self.folder/child).mkdir()
        self.source=self.folder/'incoming.ico';self.source.write_bytes(b'incoming-test-icon')
        self.icon=self.folder/'LocalAppData/Office/office.ico'
        source=(ROOT/'install.sh').read_text().split("<<'PS1'\n",1)[1].split('\nPS1',1)[0]
        source=source.replace("@([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))", "@((Join-Path $env:OFFICE_TEST_ROOT 'Desktop'), (Join-Path $env:OFFICE_TEST_ROOT 'Programs'))")
        source=source.replace("try { & (Join-Path $env:SystemRoot 'System32\\ie4uinit.exe') -show } catch { }", '# 시험에서는 아이콘 캐시를 새로 읽지 않아요.')
        self.script=self.folder/'shortcut-test.ps1';self.script.write_text(source,encoding='utf-8-sig')

    def seed_shortcut(self,folder):
        target=self.folder/folder/'Office.lnk'
        script="$s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut("+self.quote(self.windows(target))+"); $l.TargetPath=Join-Path $env:SystemRoot 'System32\\wsl.exe'; $l.Arguments='custom-user-arguments'; $l.Description='custom-user-description'; $l.Save()"
        self.powershell(script)
        return target

    def execute(self):
        self.powershell('$env:OFFICE_TEST_ROOT='+self.quote(self.windows(self.folder))+'; $env:LOCALAPPDATA='+self.quote(self.windows(self.folder/'LocalAppData'))+'; & '+self.quote(self.windows(self.script))+" -Distro 'OfficeTest' -ShellInit 'C:\\office-test\\console.bashrc' -Icon "+self.quote(self.windows(self.source)))

    def test_existing_windows_shortcuts_and_icon_are_unchanged(self):
        self.icon.parent.mkdir();self.icon.write_bytes(b'user-custom-icon')
        paths=[self.seed_shortcut('Desktop'),self.seed_shortcut('Programs'),self.icon]
        before={p:p.read_bytes() for p in paths}
        self.execute()
        self.assertEqual(before,{p:p.read_bytes() for p in paths})

    def test_shortcut_with_only_custom_description_is_unchanged(self):
        self.icon.parent.mkdir();self.icon.write_bytes(b'user-custom-icon')
        target=self.seed_shortcut('Desktop')
        script="$s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut("+self.quote(self.windows(target))+"); $l.Arguments='-d OfficeTest --cd ~ -e bash --rcfile C:\\office-test\\console.bashrc -i'; $l.IconLocation="+self.quote(self.windows(self.icon))+'; $l.Save()'
        self.powershell(script)
        before=target.read_bytes()
        self.execute()
        self.assertEqual(target.read_bytes(),before)

    def test_missing_shortcuts_and_icon_are_created(self):
        self.execute()
        self.assertEqual(self.icon.read_bytes(),self.source.read_bytes())
        for folder in ('Desktop','Programs'):
            self.assertTrue((self.folder/folder/'Office.lnk').is_file())

    def test_new_shortcut_keeps_an_existing_custom_icon(self):
        self.icon.parent.mkdir();self.icon.write_bytes(b'user-custom-icon')
        desktop=self.seed_shortcut('Desktop');before=desktop.read_bytes()
        self.execute()
        self.assertEqual(self.icon.read_bytes(),b'user-custom-icon')
        self.assertEqual(desktop.read_bytes(),before)
        self.assertTrue((self.folder/'Programs/Office.lnk').is_file())


if __name__=='__main__':
    unittest.main()
