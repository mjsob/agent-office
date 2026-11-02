"""업데이트 점검 엔진(office_customizations.py)의 --plan·--apply 계약을 격리 폴더에서 확인해요.
실행: python3 -I tests/test_customizations_plan.py [저장소 경로]
설치 폴더·네트워크·서버는 쓰지 않아요.
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
HELPER = ROOT / 'payload/office_customizations.py'
BASE = b'print("base")\n' + b''.join(b'# line %d\n' % i for i in range(1, 21))
CORE = {'office.py': BASE, 'office.html': b'<p>base</p>\n', 'office.ico': b'\0ico', 'office-icon.png': b'\0png', 'VERSION': b'1.0.0\n'}


def sums(folder, names):
    (folder / 'SHA256SUMS').write_text(''.join(hashlib.sha256((folder / n).read_bytes()).hexdigest() + '  ' + n + '\n' for n in names))


class Plan(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix='office-plan-')
        self.addCleanup(t.cleanup)
        self.dir = Path(t.name)
        self.prefix = self.dir / 'prefix'
        self.app = self.prefix / 'app'
        self.incoming = self.dir / 'incoming'
        self.incoming.mkdir()
        self.names = list(CORE)
        for n, c in CORE.items():
            (self.incoming / n).write_bytes(c)
        sums(self.incoming, self.names)

    def run_helper(self, *args):
        return subprocess.run([sys.executable, '-I', str(HELPER), *args], capture_output=True, text=True, timeout=30)

    def install_old(self, names=None):
        """지금 incoming 내용을 설치된 것처럼 만든다(기록·원본 포함)."""
        names = names or self.names
        (self.app / '.office-orig').mkdir(parents=True)
        for n in names:
            shutil.copyfile(self.incoming / n, self.app / n)
            shutil.copyfile(self.incoming / n, self.app / '.office-orig' / n)
        manifest = {'version': (self.incoming / 'VERSION').read_text().strip(),
                    'files': {n: hashlib.sha256((self.incoming / n).read_bytes()).hexdigest() for n in names}}
        (self.app / '.office-manifest.json').write_text(json.dumps(manifest))

    def new_version(self, **changes):
        for n, c in changes.items():
            (self.incoming / n).write_bytes(c)
        if 'VERSION' not in changes:
            (self.incoming / 'VERSION').write_text('1.0.1\n')
        sums(self.incoming, self.names)

    def plan(self):
        out = self.dir / 'plan.json'
        r = self.run_helper('--plan', '--prefix', str(self.prefix), '--incoming', str(self.incoming), '--output', str(out))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(out.read_text())

    def status(self, plan):
        return {f['name']: f['status'] for f in plan['files']}

    @staticmethod
    def prefix_files(snap):
        return {k: v for k, v in snap.items() if k.startswith('prefix/') or k == 'prefix'}

    def snapshot(self):
        return {str(p.relative_to(self.dir)): (p.read_bytes() if p.is_file() else None)
                for p in sorted(self.dir.rglob('*')) if p.name != 'plan.json'}

    def test_plan_changes_nothing_but_output(self):
        self.install_old()
        self.new_version(**{'office.py': BASE.replace(b'# line 20', b'# upstream 20')})
        (self.app / 'office.py').write_bytes(BASE.replace(b'# line 1\n', b'# custom 1\n'))
        before = self.snapshot()
        plan = self.plan()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(plan['schema'], 1)
        self.assertEqual(self.status(plan)['office.py'], 'merged')
        self.assertEqual(plan['conflicts'], [])
        self.assertEqual(plan['blocked'], [])

    def test_statuses(self):
        self.install_old()
        self.new_version(**{'office.html': b'<p>upstream</p>\n'})
        plan = self.plan()
        status = self.status(plan)
        self.assertEqual(status['office.html'], 'update')
        self.assertEqual(status['office.py'], 'unchanged')

    def test_same_change_is_not_a_conflict(self):
        self.install_old()
        edited = BASE.replace(b'# line 5', b'# same 5')
        (self.app / 'office.py').write_bytes(edited)
        self.new_version(**{'office.py': edited})
        plan = self.plan()
        self.assertEqual(self.status(plan)['office.py'], 'same-change')
        self.assertEqual(plan['blocked'], [])

    def test_same_line_different_value_is_conflict(self):
        self.install_old()
        (self.app / 'office.py').write_bytes(BASE.replace(b'# line 5', b'# mine 5'))
        self.new_version(**{'office.py': BASE.replace(b'# line 5', b'# theirs 5')})
        plan = self.plan()
        self.assertEqual(plan['conflicts'], ['office.py'])
        self.assertTrue(plan['blocked'])

    def test_binary_and_missing_baseline_are_blocked_not_conflict(self):
        self.install_old()
        (self.app / 'office.ico').write_bytes(b'\0mine')
        self.new_version(**{'office.ico': b'\0theirs'})
        plan = self.plan()
        self.assertEqual(plan['conflicts'], [])
        self.assertEqual(self.status(plan)['office.ico'], 'blocked')
        (self.app / '.office-orig/office.ico').unlink()
        (self.app / 'office.ico').write_bytes(b'\0mine2')
        plan = self.plan()
        self.assertEqual(plan['conflicts'], [])
        self.assertEqual(self.status(plan)['office.ico'], 'blocked')

    def test_local_edition_blocks_public_version(self):
        self.install_old()
        (self.app / 'VERSION').write_text('1.0.0+local\n')
        self.new_version()
        plan = self.plan()
        self.assertTrue(any('+local' in b or '전용' in b for b in plan['blocked']))

    def test_new_managed_file_is_added_not_deleted(self):
        self.install_old()          # 예전 설치: 관리 파일 5개
        (self.incoming / 'office_extra.py').write_text('x = 1\n')
        self.names.append('office_extra.py')
        self.new_version()
        plan = self.plan()
        self.assertEqual(self.status(plan)['office_extra.py'], 'added')
        self.assertEqual(plan['blocked'], [])

    def test_new_managed_file_over_unknown_file_is_blocked(self):
        self.install_old()
        (self.app / 'office_extra.py').write_text('mine = 1\n')
        (self.incoming / 'office_extra.py').write_text('x = 1\n')
        self.names.append('office_extra.py')
        self.new_version()
        plan = self.plan()
        self.assertEqual(self.status(plan)['office_extra.py'], 'blocked')
        self.assertTrue(plan['blocked'])

    def test_core_file_missing_from_manifest_is_blocked(self):
        self.install_old()
        manifest = json.loads((self.app / '.office-manifest.json').read_text())
        del manifest['files']['office.html']
        (self.app / '.office-manifest.json').write_text(json.dumps(manifest))
        (self.app / 'office.html').unlink()
        self.new_version()
        self.assertTrue(self.plan()['blocked'])

    def test_plan_id_changes_when_install_changes(self):
        self.install_old()
        self.new_version()
        first = self.plan()['id']
        self.assertEqual(first, self.plan()['id'])
        (self.app / 'office.html').write_text('<p>edit</p>\n')
        self.assertNotEqual(first, self.plan()['id'])

    def test_apply_rejects_stale_plan_id(self):
        self.install_old()
        self.new_version()
        plan_id = self.plan()['id']
        (self.app / 'office.html').write_text('<p>edit</p>\n')
        staging = self.dir / 'staging'
        staging.mkdir()
        before = self.snapshot()
        r = self.run_helper('--apply', '--prefix', str(self.prefix), '--incoming', str(self.incoming), '--staging', str(staging), '--expect-plan-id', plan_id)
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(before, self.snapshot())

    def test_bad_names_are_rejected(self):
        for bad in ('../escape', '.hidden', 'sub/dir'):
            (self.incoming / 'SHA256SUMS').write_text('0' * 64 + '  ' + bad + '\n')
            out = self.dir / 'p.json'
            r = self.run_helper('--plan', '--prefix', str(self.prefix), '--incoming', str(self.incoming), '--output', str(out))
            self.assertEqual(r.returncode, 2, bad)
            self.assertFalse(out.exists())

    def test_invalid_python_merge_is_conflict(self):
        base = b'def f():\n    return 1\n' + b'# pad\n' * 10 + b'def g():\n    return 2\n'
        (self.incoming / 'office.py').write_bytes(base)
        self.install_old()
        (self.app / 'office.py').write_bytes(base.replace(b'return 1', b'return (1'))
        self.new_version(**{'office.py': base.replace(b'return 2', b'return 3')})
        plan = self.plan()
        self.assertEqual(self.status(plan)['office.py'], 'conflict')

    # ---- 후보 사본·선택·복구 ----
    def conflict_setup(self):
        self.install_old()
        (self.app / 'office.py').write_bytes(BASE.replace(b'# line 5', b'# mine 5'))
        self.new_version(**{'office.py': BASE.replace(b'# line 5', b'# theirs 5')})

    def plan_with_candidate(self):
        out = self.dir / 'plan.json'
        cand = self.dir / 'cand'
        r = self.run_helper('--plan', '--prefix', str(self.prefix), '--incoming', str(self.incoming), '--output', str(out), '--candidate-dir', str(cand))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(out.read_text()), cand

    def apply(self, *extra):
        staging = self.dir / ('staging%d' % len(list(self.dir.glob('staging*'))))
        staging.mkdir()
        for n in self.names:
            shutil.copyfile(self.incoming / n, staging / n)
        r = self.run_helper('--apply', '--prefix', str(self.prefix), '--incoming', str(self.incoming), '--staging', str(staging), *extra)
        return r, staging

    def decision(self, plan, choice, cand=None, files=None):
        d = {'schema': 1, 'plan_id': plan['id'], 'choice': choice}
        if cand:
            d['candidate_dir'] = str(cand)
            d['candidate_sha'] = {n: hashlib.sha256((cand / 'files' / n).read_bytes()).hexdigest() for n in (files or plan['problem_files'])}
        path = self.dir / ('decision-%s.json' % choice)
        path.write_text(json.dumps(d))
        return path

    def test_candidate_has_conflict_markers_and_three_sources(self):
        self.conflict_setup()
        before = self.snapshot()
        plan, cand = self.plan_with_candidate()
        self.assertTrue(plan['resolvable'])
        self.assertEqual(plan['problem_files'], ['office.py'])
        text = (cand / 'files/office.py').read_text()
        self.assertIn('<<<<<<<', text)
        for part in ('current', 'base', 'incoming'):
            self.assertTrue((cand / 'conflicts/office.py' / part).is_file())
        self.assertEqual(self.prefix_files(before), self.prefix_files(self.snapshot()))
        self.assertEqual((self.incoming / 'office.py').read_bytes(), BASE.replace(b'# line 5', b'# theirs 5'))

    def test_candidate_dir_must_be_empty(self):
        self.conflict_setup()
        cand = self.dir / 'cand'
        cand.mkdir()
        (cand / 'x').write_text('x')
        r = self.run_helper('--plan', '--prefix', str(self.prefix), '--incoming', str(self.incoming), '--output', str(self.dir / 'p.json'), '--candidate-dir', str(cand))
        self.assertEqual(r.returncode, 2)

    def test_upstream_choice_replaces_only_problem_files(self):
        self.install_old()
        (self.app / 'office.html').write_text('<p>mine</p>\n')                 # 충돌
        (self.app / 'office.py').write_bytes(BASE.replace(b'# line 2\n', b'# mine 2\n'))   # 안 겹치는 수정
        self.new_version(**{'office.html': b'<p>theirs</p>\n', 'office.py': BASE.replace(b'# line 19\n', b'# theirs 19\n')})
        plan = self.plan()
        self.assertEqual(plan['conflicts'], ['office.html'])
        d = self.decision(plan, 'upstream')
        r, staging = self.apply('--expect-plan-id', plan['id'], '--resolution', str(d))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((staging / 'office.html').read_bytes(), b'<p>theirs</p>\n')
        merged = (staging / 'office.py').read_bytes()
        self.assertIn(b'# mine 2', merged)     # 비충돌 수정은 합쳐 보존
        self.assertIn(b'# theirs 19', merged)
        self.assertEqual((staging / '.office-orig/office.py').read_bytes(), (self.incoming / 'office.py').read_bytes())
        backup = Path((staging / '.backup-path').read_text())
        self.assertEqual((backup / 'office.html').read_bytes(), b'<p>mine</p>\n')     # 내 파일은 백업에 남음

    def test_merged_choice_uses_checked_candidate(self):
        self.conflict_setup()
        plan, cand = self.plan_with_candidate()
        (cand / 'files/office.py').write_bytes(BASE.replace(b'# line 5', b'# both 5'))
        d = self.decision(plan, 'merged', cand)
        r, staging = self.apply('--expect-plan-id', plan['id'], '--resolution', str(d))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(b'# both 5', (staging / 'office.py').read_bytes())
        # 합친 결과가 기준 원본이 되면 안 돼요
        self.assertIn(b'# theirs 5', (staging / '.office-orig/office.py').read_bytes())

    def test_merged_choice_rejects_markers_syntax_and_tampered_candidate(self):
        self.conflict_setup()
        plan, cand = self.plan_with_candidate()
        before = self.snapshot()
        d = self.decision(plan, 'merged', cand)          # 충돌 표시가 그대로 남은 후보
        r, _ = self.apply('--resolution', str(d))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        (cand / 'files/office.py').write_bytes(b'def (:\n')
        d = self.decision(plan, 'merged', cand)
        r, _ = self.apply('--resolution', str(d))
        self.assertEqual(r.returncode, 3)
        (cand / 'files/office.py').write_bytes(BASE)
        d = self.decision(plan, 'merged', cand)
        (cand / 'files/office.py').write_bytes(BASE + b'# changed after\n')       # 지문 뒤에 바뀜
        r, _ = self.apply('--resolution', str(d))
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.prefix_files(before), self.prefix_files(self.snapshot()))

    def test_resolution_rejects_stale_plan_unknown_choice_and_unresolvable(self):
        self.conflict_setup()
        plan = self.plan()
        d = self.decision(plan, 'upstream')
        (self.app / 'office.html').write_text('<p>later edit</p>\n')
        r, _ = self.apply('--resolution', str(d))
        self.assertEqual(r.returncode, 4)
        plan = self.plan()
        bad = self.dir / 'bad.json'
        bad.write_text(json.dumps({'schema': 1, 'plan_id': plan['id'], 'choice': 'delete-everything'}))
        r, _ = self.apply('--resolution', str(bad))
        self.assertEqual(r.returncode, 3)
        (self.app / 'VERSION').write_text('1.0.0+local\n')
        plan = self.plan()
        self.assertFalse(plan['resolvable'])
        d = self.decision(plan, 'upstream')
        r, _ = self.apply('--resolution', str(d))
        self.assertEqual(r.returncode, 3)

    def test_normal_update_always_makes_backup_and_prunes(self):
        self.install_old()
        self.new_version(**{'office.html': b'<p>upstream</p>\n'})
        backups = self.prefix / 'backups'
        backups.mkdir(parents=True)
        for i in range(12):
            (backups / ('old-%02d' % i)).mkdir()
            (backups / ('old-%02d' % i / '.office-backup') if False else backups / ('old-%02d' % i) / '.office-backup').write_text('office-installer\n')
            os.utime(backups / ('old-%02d' % i), (1000 + i, 1000 + i))
        (backups / 'user-archive').mkdir()                       # 사용자가 만든 폴더(표시 없음)
        (backups / 'user-archive' / 'keep.txt').write_text('내 파일')
        (backups / 'legacy-no-marker').mkdir()                   # 표시 없는 옛 백업도 지우지 않아요
        os.utime(backups / 'user-archive', (1, 1))
        os.utime(backups / 'legacy-no-marker', (2, 2))
        r, staging = self.apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(Path((staging / '.backup-path').read_text()).is_dir())
        owned = [p for p in backups.iterdir() if (p / '.office-backup').is_file()]
        self.assertLessEqual(len(owned), 10)
        self.assertEqual((backups / 'user-archive' / 'keep.txt').read_text(), '내 파일')
        self.assertTrue((backups / 'legacy-no-marker').is_dir())

    def installed_pair(self):
        """1.0.0 설치 → 새 모듈이 붙은 1.0.1 설치를 흉내 내고 백업 경로를 돌려줘요."""
        self.install_old()
        (self.incoming / 'office_extra.py').write_text('x = 1\n')
        self.names.append('office_extra.py')
        self.new_version(**{'office.html': b'<p>upstream</p>\n'})
        r, staging = self.apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        backup = Path((staging / '.backup-path').read_text())
        for n in self.names + ['.office-manifest.json']:
            shutil.move(str(staging / n), str(self.app / n))
        shutil.rmtree(self.app / '.office-orig')
        shutil.move(str(staging / '.office-orig'), str(self.app / '.office-orig'))
        return backup

    def test_restore_returns_to_old_files_and_drops_new_module(self):
        backup = self.installed_pair()
        (self.app / 'config.local').write_text('keep')
        r = self.run_helper('--restore-backup', str(backup), '--prefix', str(self.prefix))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.app / 'office.html').read_bytes(), b'<p>base</p>\n')
        self.assertFalse((self.app / 'office_extra.py').exists())
        self.assertEqual((self.app / 'VERSION').read_text(), '1.0.0\n')
        self.assertEqual(json.loads((self.app / '.office-manifest.json').read_text())['version'], '1.0.0')
        self.assertEqual((self.app / 'config.local').read_text(), 'keep')
        self.assertFalse(list(self.prefix.glob('.restore-snapshot.*')))

    def test_restore_refuses_when_current_install_was_edited(self):
        backup = self.installed_pair()
        (self.app / 'office.html').write_text('<p>edited after update</p>\n')
        before = self.snapshot()
        r = self.run_helper('--restore-backup', str(backup), '--prefix', str(self.prefix))
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertEqual(before, self.snapshot())

    def test_restore_refuses_paths_outside_backups(self):
        backup = self.installed_pair()
        outside = self.dir / 'elsewhere'
        shutil.copytree(backup, outside)
        for target in (outside, self.prefix / 'backups', self.app):
            r = self.run_helper('--restore-backup', str(target), '--prefix', str(self.prefix))
            self.assertEqual(r.returncode, 5, str(target))
        link = self.prefix / 'backups' / 'link'
        link.symlink_to(backup)
        r = self.run_helper('--restore-backup', str(link), '--prefix', str(self.prefix))
        self.assertEqual(r.returncode, 5)

    def test_restore_refuses_tampered_backup(self):
        backup = self.installed_pair()
        (backup / 'office.py').write_bytes(b'tampered')
        r = self.run_helper('--restore-backup', str(backup), '--prefix', str(self.prefix))
        self.assertEqual(r.returncode, 5)
        self.assertEqual((self.app / 'office.html').read_bytes(), b'<p>upstream</p>\n')

    def test_plan_id_includes_baseline_and_tampering_rejects_choice(self):
        self.conflict_setup()
        plan = self.plan()
        d = self.decision(plan, 'upstream')
        (self.app / '.office-orig/office.html').write_text('<p>tampered base</p>\n')
        r, _ = self.apply('--resolution', str(d))
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)

    def test_candidate_contains_every_managed_file(self):
        self.conflict_setup()
        plan, cand = self.plan_with_candidate()
        for n in self.names:
            self.assertTrue((cand / 'files' / n).is_file(), n)
        self.assertEqual((cand / 'files/office.html').read_bytes(), (self.incoming / 'office.html').read_bytes())

    def test_apply_prints_json_line_with_path_with_spaces(self):
        self.install_old()
        self.new_version(**{'office.html': b'<p>x</p>\n'})
        spaced = self.dir / 'with space'
        shutil.move(str(self.prefix), str(spaced))
        self.prefix = spaced
        r, _ = self.apply()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        line = [l for l in r.stdout.splitlines() if l.startswith('OFFICE_LOCAL_JSON: ')][0]
        data = json.loads(line.split(': ', 1)[1])
        self.assertTrue(data['backup'].startswith(str(spaced)))
        self.assertFalse(data['blocked'])

    def test_link_and_directory_are_not_resolvable(self):
        for kind in ('link', 'dir'):
            with self.subTest(kind):
                shutil.rmtree(self.dir / 'prefix', ignore_errors=True)
                self.install_old()
                target = self.app / 'office.html'
                target.unlink()
                if kind == 'link':
                    target.symlink_to(self.dir / 'outside')
                else:
                    target.mkdir()
                self.new_version()
                plan = self.plan()
                self.assertFalse(plan['resolvable'])
                d = self.decision(plan, 'upstream')
                r, _ = self.apply('--resolution', str(d))
                self.assertEqual(r.returncode, 3)

    def test_deleted_file_upstream_choice_then_restore_returns_to_deleted(self):
        self.install_old()
        (self.app / 'office.html').unlink()
        self.new_version(**{'office.html': b'<p>upstream</p>\n'})
        plan = self.plan()
        self.assertTrue(plan['resolvable'])
        d = self.decision(plan, 'upstream')
        r, staging = self.apply('--expect-plan-id', plan['id'], '--resolution', str(d))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        backup = Path((staging / '.backup-path').read_text())
        for n in self.names + ['.office-manifest.json']:
            shutil.move(str(staging / n), str(self.app / n))
        shutil.rmtree(self.app / '.office-orig')
        shutil.move(str(staging / '.office-orig'), str(self.app / '.office-orig'))
        self.assertTrue((self.app / 'office.html').is_file())
        r = self.run_helper('--restore-backup', str(backup), '--prefix', str(self.prefix))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.app / 'office.html').exists())

    def test_custom_only_syntax_error_is_conflict_and_blocks_install(self):
        self.install_old()
        (self.app / 'office.py').write_bytes(b'def broken(:\n')
        self.new_version(**{'office.html': b'<p>x</p>\n'})
        plan = self.plan()
        self.assertEqual(self.status(plan)['office.py'], 'conflict')
        before = self.snapshot()
        r, _ = self.apply()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(self.prefix_files(before), self.prefix_files(self.snapshot()))

    def test_upstream_choice_over_broken_custom_file_works(self):
        self.install_old()
        (self.app / 'office.py').write_bytes(b'def broken(:\n')
        self.new_version(**{'office.html': b'<p>x</p>\n'})
        plan = self.plan()
        d = self.decision(plan, 'upstream')
        r, staging = self.apply('--expect-plan-id', plan['id'], '--resolution', str(d))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((staging / 'office.py').read_bytes(), (self.incoming / 'office.py').read_bytes())


if __name__ == '__main__':
    unittest.main()
