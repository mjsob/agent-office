"""화면 업데이트와 한 줄 설치가 구형 설치기로 사용자 수정을 덮지 않는지 확인해요.
실행: python3 -I tests/test_update_customizations.py [저장소 경로]
다운로드와 설치 실행은 임시 파일과 가짜 응답으로 대체해요.
"""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else Path(__file__).resolve().parents[1]


def archive(script):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode='w:gz') as tar:
        entry = tarfile.TarInfo('office-dist/install.sh')
        data = script.encode()
        entry.size = len(data)
        entry.mode = 0o700
        tar.addfile(entry, io.BytesIO(data))
    return result.getvalue()


class AppUpdate(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='office-update-guard-')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        env = {k:v for k,v in os.environ.items() if not k.startswith('OFFICE_')}
        env['OFFICE_DIR'] = str(self.folder)
        with patch.dict(os.environ, env, clear=True):
            spec = importlib.util.spec_from_file_location('office_update_guard', ROOT / 'payload/office.py')
            self.o = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.o)
        self.office = SimpleNamespace(shutdown=Mock())
        self.o.server_log = Mock()
        self.o.UPDATE['state'] = 'installing'
        # 이 PC 전용 저장소에서 돌려도 공개판 경로를 시험해요.
        version = patch.object(self.o, 'office_version', return_value='1.0.0')
        version.start()
        self.addCleanup(version.stop)

    def run_update(self, marked, returncode=0, output='', stderr='수정 파일을 보존해요.'):
        blob = archive('#!/bin/bash\n' + ('OFFICE_CUSTOMIZATION_GUARD=1\n' if marked else '') + 'exit 0\n')
        def download(url, **kwargs):
            return io.BytesIO((hashlib.sha256(blob).hexdigest() + '  archive.tar.gz').encode() if url.endswith('.sha256') else blob)
        with patch.object(self.o, 'install_paths', return_value=(str(self.folder/'app-root'), str(self.folder/'workspace'))), patch.object(self.o, 'provider_ssl_context', return_value=None), patch.object(self.o.urlrequest, 'urlopen', side_effect=download), patch.object(self.o.subprocess, 'run', return_value=subprocess.CompletedProcess([], returncode, stdout=output, stderr=stderr if returncode else '')) as run, patch.object(self.o.time, 'sleep'):
            self.o.run_update(self.office)
        return run

    def test_old_installer_is_not_executed(self):
        run = self.run_update(False)
        run.assert_not_called()
        self.office.shutdown.assert_not_called()
        self.assertFalse(hasattr(self.office, 'restart'))
        self.assertEqual(self.o.UPDATE['state'], 'failed')
        self.assertIn('기존 설치를 유지', self.o.UPDATE['message'])

    def test_preservation_failure_does_not_restart(self):
        run = self.run_update(True, returncode=1)
        run.assert_called_once()
        self.office.shutdown.assert_not_called()
        self.assertFalse(hasattr(self.office, 'restart'))
        self.assertEqual(self.o.UPDATE['state'], 'failed')

    def test_general_failure_shows_last_line(self):
        self.run_update(True, returncode=1, stderr='앞줄\n- 목록 같은 줄\n설치 중단: flock 명령이 필요해요\n')
        self.assertEqual(self.o.UPDATE['message'], '설치에 실패했어요. 설치 중단: flock 명령이 필요해요')

    def test_published_installer_local_block_is_not_blamed_on_edits(self):
        # 이미 공개된 0.9.0 설치기가 내는 그대로의 출력이에요.
        stderr = ('직접 고친 부분이 있어 설치를 멈췄어요. 기존 설치는 그대로예요.\n'
                  '- 이 PC 전용 버전(0.8.8+local) 위에 공개 버전을 덮어쓸 수 없어요\n'
                  '설치 중단: 직접 고친 부분을 지킬 수 없어서 설치를 멈췄어요. 기존 설치는 그대로예요.\n')
        self.run_update(True, returncode=1, stderr=stderr)
        self.assertEqual(self.o.UPDATE['message'], '이 PC 전용 버전이라 공개 버전으로 바꾸지 않았어요. 직접 고친 파일 때문이 아니에요. 기존 설치는 그대로예요.')
        self.office.shutdown.assert_not_called()

    def test_real_conflict_names_the_file(self):
        stderr = ('아래 이유로 설치를 멈췄어요. 기존 설치는 그대로예요.\n'
                  '- 직접 고친 파일을 새 버전과 자동으로 합칠 수 없어요: office.html\n'
                  '설치 중단: 설치를 멈췄어요. 기존 설치는 그대로예요.\n')
        self.run_update(True, returncode=1, stderr=stderr)
        self.assertEqual(self.o.UPDATE['message'], '직접 고친 파일을 새 버전과 자동으로 합칠 수 없어요: office.html. 기존 설치는 그대로예요.')

    def test_local_and_real_edit_show_both(self):
        stderr = ('아래 이유로 설치를 멈췄어요. 기존 설치는 그대로예요.\n'
                  '- 실행 파일 bin/office를 직접 고친 것 같아요\n'
                  '- 이 PC 전용 버전(0.8.8+local) 위에 공개 버전을 덮어쓸 수 없어요\n')
        self.run_update(True, returncode=1, stderr=stderr)
        message = self.o.UPDATE['message']
        self.assertIn('이 PC 전용 버전이라', message)
        self.assertIn('bin/office를 직접 고친 것 같아요', message)
        self.assertNotIn('때문이 아니에요', message)

    def test_local_edition_does_not_download(self):
        self.o.office_version.return_value = '0.8.8+local'
        with patch.object(self.o, 'install_paths', return_value=('/x', '/y')), patch.object(self.o.urlrequest, 'urlopen') as urlopen, patch.object(self.o.subprocess, 'run') as run:
            self.o.run_update(self.office)
        urlopen.assert_not_called()
        run.assert_not_called()
        self.assertEqual(self.o.UPDATE['state'], 'failed')
        self.assertIn('이 PC 전용 버전', self.o.UPDATE['message'])
        self.office.shutdown.assert_not_called()

    def info(self, current, latest, state='idle', message=None, paths=('/x', '/y')):
        self.o.office_version.return_value = current
        self.o.UPDATE.update(latest=latest, state=state, message=message, error=None)
        with patch.object(self.o, 'install_paths', return_value=paths):
            return self.o.update_info()

    def test_local_edition_with_newer_public_has_no_button(self):
        info = self.info('0.8.8+local', '0.9.0')
        self.assertTrue(info['available'])
        self.assertFalse(info['installable'])
        self.assertEqual(info['message'], self.o.LOCAL_EDITION_UPDATE_TEXT)

    def test_local_edition_same_version_clears_old_failure(self):
        info = self.info('0.9.0+local', '0.9.0', 'failed', '설치에 실패했어요.')
        self.assertFalse(info['available'])
        self.assertEqual((info['state'], info['message']), ('idle', None))

    def test_local_edition_newer_public_replaces_old_failure(self):
        info = self.info('0.8.8+local', '0.9.0', 'failed', '설치에 실패했어요.')
        self.assertEqual((info['state'], info['message']), ('idle', self.o.LOCAL_EDITION_UPDATE_TEXT))

    def test_public_failure_stays_while_update_is_available(self):
        info = self.info('0.8.8', '0.9.0', 'failed', '설치에 실패했어요.')
        self.assertTrue(info['installable'])
        self.assertEqual((info['state'], info['message']), ('failed', '설치에 실패했어요.'))

    def test_public_not_from_installer_message_is_unchanged(self):
        info = self.info('0.8.8', '0.9.0', paths=None)
        self.assertFalse(info['installable'])
        self.assertIn('설치기로 설치한', info['message'])

    def test_safe_success_restarts(self):
        run = self.run_update(True, output='OFFICE_LOCAL: changed=1 merged=office.html conflicts=- backup=/tmp/test\n')
        run.assert_called_once()
        self.office.shutdown.assert_called_once()
        self.assertEqual(self.o.UPDATE['state'], 'restarting')
        self.assertIn('새 버전에 다시 넣었어요', self.o.UPDATE['message'])


class Bootstrap(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='office-bootstrap-guard-')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.home = self.folder/'test-home'
        self.home.mkdir()
        self.bin = self.folder/'commands'
        self.bin.mkdir()
        curl = self.bin/'curl'
        curl.write_text('#!' + sys.executable + '\nimport os,sys,shutil\nshutil.copyfile(os.environ["TEST_BLOB"] + (".sha256" if sys.argv[-3].endswith(".sha256") else ""), sys.argv[-1])\n')
        curl.chmod(0o700)
        self.sentinel = self.folder/'installer-ran'
        self.env = {**os.environ, 'HOME':str(self.home), 'PATH':str(self.bin)+':'+os.environ['PATH'], 'TEST_RAN':str(self.sentinel)}

    def execute(self, *args, marked=False):
        blob = archive('#!/bin/bash\n' + ('OFFICE_CUSTOMIZATION_GUARD=1\n' if marked else '') + 'printf ran > "$TEST_RAN"\n')
        path = self.folder/'installer.tar.gz'
        path.write_bytes(blob)
        path.with_suffix('.gz.sha256').write_text(hashlib.sha256(blob).hexdigest()+'  installer.tar.gz\n')
        self.env['TEST_BLOB'] = str(path)
        return subprocess.run(['bash',str(ROOT/'get.sh'),*args],env=self.env,capture_output=True,text=True,timeout=10)

    def test_current_installer_has_guard(self):
        self.assertIn('OFFICE_CUSTOMIZATION_GUARD=1', (ROOT/'install.sh').read_text().splitlines())

    def test_existing_default_install_rejects_old(self):
        (self.home/'.local/share/office-app/app').mkdir(parents=True)
        r=self.execute('--apply','--version','0.8.8')
        self.assertNotEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertFalse(self.sentinel.exists())

    def test_custom_prefix_with_spaces_rejects_old(self):
        prefix=self.folder/'custom install'
        (prefix/'app').mkdir(parents=True)
        r=self.execute('--prefix',str(prefix),'--apply')
        self.assertNotEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertFalse(self.sentinel.exists())

    def test_existing_launcher_rejects_old(self):
        launcher=self.home/'.local/bin/office'
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to('/missing/custom-launcher')
        r=self.execute('--apply')
        self.assertNotEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertFalse(self.sentinel.exists())
        self.assertTrue(launcher.is_symlink())

    def test_marked_installer_is_allowed(self):
        (self.home/'.local/share/office-app/app').mkdir(parents=True)
        r=self.execute('--apply',marked=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertTrue(self.sentinel.exists())

    def test_fresh_install_without_customizations_is_allowed(self):
        r=self.execute('--apply')
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertTrue(self.sentinel.exists())

    def test_final_dry_run_does_not_apply_guard(self):
        (self.home/'.local/share/office-app/app').mkdir(parents=True)
        r=self.execute('--apply','--dry-run')
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)


if __name__=='__main__':
    unittest.main()
