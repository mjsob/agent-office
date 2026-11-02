#!/usr/bin/env python3
"""실제 0.8.0~0.9.0 설치를 출발점으로 새 설치기·점검 엔진을 반복 이전하는 대량 시험이에요.
실행: python3 -I tests/test_update_matrix.py [--workers N] [--out 폴더] [--versions 0.8.0,0.9.0] [--only 시나리오,...]
격리된 HOME·가짜 CLI만 써요. 실제 설치·서버·대화 데이터는 건드리지 않아요.
- 출발점: 실제 배포 커밋의 설치기·묶음(역사 원본 그대로)
- 목표: 현재 저장소의 설치기와 payload (VERSION만 시험 사본에서 바꿈)
- 가상 미래 버전(0.10.1/0.11.0)은 연속 이전·모듈 변화 시험에만 쓰고 결과에 따로 표시해요.
사례마다 입력·예상·실제·종료코드·소요시간을 JSON 줄로 남겨요.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path('/tmp/req157/corpus')
REVS = [('0.8.0', 'v0.8.0'), ('0.8.1', 'v0.8.1'), ('0.8.2', 'v0.8.2'), ('0.8.3', 'v0.8.3'),
        ('0.8.4', 'v0.8.4'), ('0.8.5', 'v0.8.5'), ('0.8.6', 'v0.8.6'), ('0.8.7', 'v0.8.7'),
        ('0.8.8', 'b5d636e'), ('0.9.0', '638d71f')]
CORE = ('office.py', 'office.html', 'office.ico', 'office-icon.png', 'VERSION')
FLAGS = ['--apply', '--skip-login', '--no-launch', '--no-shortcut']


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git_show(rev, path):
    return subprocess.run(['git', '-C', str(ROOT), 'show', '%s:%s' % (rev, path)], capture_output=True, check=True).stdout


def extract(version, rev):
    d = CACHE / version
    if (d / '.done').exists():
        return d
    tmp = d.with_name(d.name + '.tmp')
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / 'payload').mkdir(parents=True)
    names = subprocess.run(['git', '-C', str(ROOT), 'ls-tree', '-r', '--name-only', rev], capture_output=True, text=True, check=True).stdout.split()
    for path in names:
        if path == 'install.sh' or path.startswith('payload/'):
            (tmp / path).write_bytes(git_show(rev, path))
    (tmp / '.done').write_text('ok')
    shutil.rmtree(d, ignore_errors=True)
    os.rename(tmp, d)
    return d


def payload_names(payload):
    return list(CORE) + sorted(p.name for p in Path(payload).glob('office_*.py'))


def build_pkg(dst, version='0.10.0', edits=None, extra=None, drop=()):
    """현재 저장소의 설치기·payload로 배포 묶음 사본을 만들어요(VERSION·시험용 수정만 다름)."""
    dst = Path(dst)
    (dst / 'payload').mkdir(parents=True)
    shutil.copyfile(ROOT / 'install.sh', dst / 'install.sh')
    names = [n for n in payload_names(ROOT / 'payload') if n not in drop]
    for n in names:
        shutil.copyfile(ROOT / 'payload' / n, dst / 'payload' / n)
    for n, data in (extra or {}).items():
        (dst / 'payload' / n).write_bytes(data)
        names.append(n)
    for n, fn in (edits or {}).items():
        p = dst / 'payload' / n
        p.write_bytes(fn(p.read_bytes()))
    (dst / 'payload' / 'VERSION').write_text(version + '\n')
    (dst / 'payload' / 'SHA256SUMS').write_text(''.join('%s  %s\n' % (sha((dst / 'payload' / n).read_bytes()), n) for n in names))
    return dst


def insert_line(text, n):
    def edit(data):
        lines = data.split(b'\n')
        lines.insert(n, text.encode())
        return b'\n'.join(lines)
    return edit


def oracle(cur, base, target, name='office.py'):
    """독립 기준: git merge-file로 직접 3자 병합해 보고 예상 결과를 정해요. ('ok', bytes) 또는 ('conflict'|'blocked', None)."""
    if cur == target:
        return 'ok', cur
    if target == base:
        return 'ok', cur
    if any(b'\0' in d for d in (cur, base, target)):
        return 'blocked', None
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        for n, d in (('cur', cur), ('base', base), ('target', target)):
            (t / n).write_bytes(d)
        r = subprocess.run(['git', 'merge-file', '-p', str(t / 'cur'), str(t / 'base'), str(t / 'target')], capture_output=True)
    if r.returncode != 0:
        return 'conflict', None
    if name.endswith('.py'):
        try:
            compile(r.stdout, name, 'exec')
        except (SyntaxError, ValueError):
            return 'conflict', None
    return 'ok', r.stdout


def first_changed_line(old, new):
    """old에서 new가 바꾼 첫 줄 번호(1부터)와 그 줄. 순수 추가가 아닌 변경만 찾아요."""
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        (t / 'a').write_bytes(old)
        (t / 'b').write_bytes(new)
        out = subprocess.run(['git', 'diff', '--no-index', '--unified=0', str(t / 'a'), str(t / 'b')], capture_output=True, text=True, errors='replace').stdout
    import re
    for m in re.finditer(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', out, re.M):
        start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        if count >= 1:
            return start
    return None


class Ctx:
    def __init__(self, base, version, rev, target):
        self.dir = Path(base)
        self.version, self.rev = version, rev
        self.target = Path(target)                 # 목표 묶음(0.10.0)
        self.home = self.dir / 'home'
        self.cmds = self.dir / 'cmds'
        self.home.mkdir()
        self.cmds.mkdir()
        for n in ('codex', 'claude'):
            (self.cmds / n).write_text('#!/bin/sh\nprintf "stub-cli\\n"\n')
            (self.cmds / n).chmod(0o700)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('OFFICE_') and k != 'WSL_DISTRO_NAME'}
        self.env.update(HOME=str(self.home), PATH=str(self.cmds) + ':' + os.environ['PATH'])
        self.prefix = self.home / '.local/share/office-app'
        self.app = self.prefix / 'app'
        self.old_dir = extract(version, rev)
        self.old_payload = self.old_dir / 'payload'

    # --- 실행
    def sh(self, script, *args, env=None, timeout=120):
        return subprocess.run(['bash', str(script), *args], env=env or self.env, capture_output=True, text=True, timeout=timeout)

    def install_old(self):
        r = self.sh(self.old_dir / 'install.sh', *FLAGS)
        if r.returncode != 0:
            r = self.sh(self.old_dir / 'install.sh', '--apply', '--skip-login', '--no-launch')
        assert r.returncode == 0, '옛 설치기 실패: ' + r.stdout[-400:] + r.stderr[-400:]
        assert (self.app / 'VERSION').read_text().strip() == self.version, 'VERSION이 달라요: ' + (self.app / 'VERSION').read_text()
        return r

    def run_new(self, pkg=None, *extra, env=None):
        return self.sh((pkg or self.target) / 'install.sh', *FLAGS, *extra, env=env)

    def helper(self, *args, pkg=None):
        pkg = pkg or self.target
        return subprocess.run([sys.executable, '-I', str(pkg / 'payload/office_customizations.py'), *args], capture_output=True, text=True, timeout=60)

    def plan(self, pkg=None, candidate=False, tag='plan'):
        pkg = pkg or self.target
        out = self.dir / (tag + '.json')
        args = ['--plan', '--prefix', str(self.prefix), '--incoming', str(pkg / 'payload'), '--output', str(out)]
        if candidate:
            args += ['--candidate-dir', str(self.dir / (tag + '-cand'))]
        r = self.helper(*args, pkg=pkg)
        assert r.returncode == 0, '점검 실패: ' + r.stdout + r.stderr
        return out, json.loads(out.read_text())

    def decision(self, plan, choice, cand=None, shas=None, tag='decision'):
        d = {'schema': 1, 'plan_id': plan['id'], 'choice': choice}
        if cand:
            d['candidate_dir'] = str(cand)
            d['candidate_sha'] = shas if shas is not None else {n: sha((cand / 'files' / n).read_bytes()) for n in plan['problem_files'] if (cand / 'files' / n).is_file()}
        p = self.dir / (tag + '.json')
        p.write_text(json.dumps(d))
        return p

    # --- 상태
    def snap(self, managed_only=False):
        out = {}
        for p in sorted(self.home.rglob('*')):
            rel = str(p.relative_to(self.home))
            if managed_only and ('/backups' in '/' + rel or '__pycache__' in rel or '.restore' in rel or rel.endswith('.install.lock')):
                continue
            if p.is_symlink():
                out[rel] = ('link', os.readlink(p))
            elif p.is_dir():
                out[rel] = ('dir',)
            else:
                out[rel] = ('file', sha(p.read_bytes()), p.stat().st_mode & 0o777)
        return out

    def app_bytes(self, name):
        return (self.app / name).read_bytes()

    def tpay(self, name, pkg=None):
        return ((pkg or self.target) / 'payload' / name).read_bytes()

    def opay(self, name):
        return (self.old_payload / name).read_bytes()

    def manifest(self):
        return json.loads((self.app / '.office-manifest.json').read_text())


def ok(expected, actual, rc=None, **extra):
    return {'expected': expected, 'actual': actual, 'rc': rc, **extra}


def must(cond, msg):
    if not cond:
        raise AssertionError(msg)


def check_fully_updated(c, pkg=None, version=None):
    pkg = pkg or c.target
    names = [l.split()[1] for l in (pkg / 'payload/SHA256SUMS').read_text().splitlines()]
    man = c.manifest()
    must(man['version'] == (version or (pkg / 'payload/VERSION').read_text().strip()), '기록 버전이 달라요')
    for n in names:
        must(sha(c.tpay(n, pkg)) == man['files'][n], '기록 지문이 새 버전 원본과 달라요: ' + n)
        must(c.app_bytes('.office-orig/' + n) == c.tpay(n, pkg), '.office-orig 가 새 버전 원본이 아니에요: ' + n)
        must(sha(c.app_bytes(n)) == man['installed'][n], 'installed 지문이 실제 파일과 달라요: ' + n)


def merged_expect(c, name, cur):
    return oracle(cur, c.opay(name), c.tpay(name), name)


# ------------------------------------------------------------------ 시나리오
def s_plain(c):
    c.install_old()
    old = {n: c.app_bytes(n) for n in CORE}
    r = c.run_new()
    must(r.returncode == 0, '정상 업데이트 실패: ' + r.stdout[-500:] + r.stderr[-500:])
    names = payload_names(c.target / 'payload')
    for n in names:
        must(c.app_bytes(n) == c.tpay(n), '새 버전 파일이 아니에요: ' + n)
    check_fully_updated(c)
    backups = list((c.prefix / 'backups').iterdir())
    must(len(backups) == 1, '백업이 하나여야 해요')
    for n in CORE:
        must((backups[0] / n).read_bytes() == old[n], '백업이 옛 파일과 달라요: ' + n)
    return ok('rc0, 새 버전·기록·백업', 'rc0', 0)


def custom_append(name, data):
    def scenario(c):
        c.install_old()
        cur = c.app_bytes(name) + data
        (c.app / name).write_bytes(cur)
        before = c.snap()
        exp, merged = merged_expect(c, name, cur)
        r = c.run_new()
        if exp == 'ok':
            must(r.returncode == 0, '합쳐져야 하는데 실패: ' + r.stdout[-300:] + r.stderr[-400:])
            must(c.app_bytes(name) == merged, '합친 결과가 독립 병합과 달라요')
            must(data in c.app_bytes(name), '내 수정이 사라졌어요')
            if name == 'office.py':
                compile(c.app_bytes(name), name, 'exec')
            check_fully_updated(c)
            return ok('merged(독립 병합 ok)', 'merged', 0)
        must(r.returncode == 3, '충돌인데 rc가 3이 아니에요: %d' % r.returncode)
        must(c.snap() == before, '막혔는데 설치가 바뀌었어요')
        return ok('blocked(독립 병합 %s)' % exp, 'blocked', 3)
    return scenario


s_html_far = custom_append('office.html', b'\n<!-- custom-screen-note -->\n')
s_py_far = custom_append('office.py', b'\n# custom feature\ndef custom_feature():\n    return 42\n')


def s_same_change(c):
    c.install_old()
    for n in ('office.html', 'office.py'):
        (c.app / n).write_bytes(c.tpay(n))
    r = c.run_new()
    must(r.returncode == 0, '양쪽 같은 수정은 충돌이 아니어야 해요: ' + r.stdout[-300:] + r.stderr[-400:])
    for n in ('office.html', 'office.py'):
        must(c.app_bytes(n) == c.tpay(n), n)
    check_fully_updated(c)
    return ok('same-change 통과', 'ok', 0)


def conflict_state(c, also_html=False):
    c.install_old()
    old, new = c.opay('office.py'), c.tpay('office.py')
    line = first_changed_line(old, new)
    must(line is not None, '옛 버전과 새 버전의 office.py에 겹칠 변경이 없어요')
    lines = old.split(b'\n')
    lines[line - 1] = lines[line - 1] + b'  # custom-conflict'
    (c.app / 'office.py').write_bytes(b'\n'.join(lines))
    if also_html:
        (c.app / 'office.html').write_bytes(c.app_bytes('office.html') + b'\n<!-- keep-me-html -->\n')


def s_conflict_block(c):
    conflict_state(c)
    before = c.snap()
    r = c.run_new()
    must(r.returncode == 3, 'rc3이어야 해요: %d' % r.returncode)
    must(c.snap() == before, '막혔는데 설치가 바뀌었어요')
    _, plan = c.plan()
    must('office.py' in plan['conflicts'] and plan['resolvable'], '충돌/선택 가능 표시가 달라요')
    must(c.snap() == before, '점검이 설치를 바꿨어요')
    return ok('rc3, conflicts=office.py, resolvable', 'rc3', 3)


def s_keep_mine(c):
    conflict_state(c, True)
    before = c.snap()
    _, plan = c.plan(candidate=True)
    c.plan(candidate=False, tag='plan2')
    after = {k: v for k, v in c.snap().items()}
    must(not any(k.startswith('home') for k in after) and after == before, '내 수정 유지(적용 안 함)인데 설치가 바뀌었어요')
    return ok('점검만 하면 설치 불변', '불변', 0)


def s_choose_upstream(c):
    conflict_state(c, True)
    custom_py = c.app_bytes('office.py')
    html_exp = merged_expect(c, 'office.html', c.app_bytes('office.html'))
    plan_file, plan = c.plan()
    d = c.decision(plan, 'upstream')
    r = c.run_new(None, '--review-plan', str(plan_file), '--resolution', str(d))
    must(r.returncode == 0, '새 버전 선택 실패: ' + r.stdout[-300:] + r.stderr[-400:])
    must(c.app_bytes('office.py') == c.tpay('office.py'), '충돌 파일이 새 버전이 아니에요')
    if html_exp[0] == 'ok':
        must(b'keep-me-html' in c.app_bytes('office.html'), '다른 커스텀(html)이 사라졌어요')
    backups = list((c.prefix / 'backups').iterdir())
    must(any((b / 'office.py').read_bytes() == custom_py for b in backups), '백업에 내 파일이 없어요')
    check_fully_updated(c)
    return ok('office.py 새 버전, html 커스텀 보존(%s), 백업에 원본' % html_exp[0], 'ok', 0)


def s_choose_merged(c):
    conflict_state(c, True)
    plan_file, plan = c.plan(candidate=True)
    cand = Path(plan['candidate'])
    final = c.tpay('office.py') + b'\n# agent-merged custom\n'
    (cand / 'files/office.py').write_bytes(final)
    d = c.decision(plan, 'merged', cand)
    r = c.run_new(None, '--review-plan', str(plan_file), '--resolution', str(d))
    must(r.returncode == 0, '합친 후보 적용 실패: ' + r.stdout[-300:] + r.stderr[-400:])
    must(c.app_bytes('office.py') == final, '후보가 적용되지 않았어요')
    must(c.app_bytes('.office-orig/office.py') == c.tpay('office.py'), '합친 결과가 기준 원본이 됐어요')
    check_fully_updated(c)
    return ok('후보 적용, 기준은 순수 새 버전', 'ok', 0)


def merged_bad(kind):
    def scenario(c):
        conflict_state(c)
        plan_file, plan = c.plan(candidate=True)
        cand = Path(plan['candidate'])
        good = c.tpay('office.py') + b'\n# agent-merged custom\n'
        shas = None
        if kind == 'markers':
            pass   # 후보에는 충돌 표시가 그대로 남아 있어요
        elif kind == 'syntax':
            (cand / 'files/office.py').write_bytes(b'def broken(:\n')
        elif kind == 'sha':
            (cand / 'files/office.py').write_bytes(good)
            shas = {'office.py': sha(good)}
            (cand / 'files/office.py').write_bytes(good + '# 지문 뒤에 바뀜\n'.encode())
        elif kind == 'missing':
            (cand / 'files/office.py').unlink()
            shas = {'office.py': sha(good)}
        before = c.snap()
        d = c.decision(plan, 'merged', cand, shas)
        r = c.run_new(None, '--review-plan', str(plan_file), '--resolution', str(d))
        must(r.returncode == 3, '거절(rc3)이어야 해요: %d %s' % (r.returncode, r.stderr[-200:]))
        must(c.snap() == before, '거절했는데 설치가 바뀌었어요')
        return ok('rc3 무변경(%s)' % kind, 'rc3', 3)
    return scenario


def stale(kind):
    def scenario(c):
        c.install_old()
        # 안 겹치는 수정으로 점검이 통과하는 상태: 내 파일에 새 파일을 하나 더하는 대신 html 끝에 덧붙임(독립 병합이 ok일 때만 의미 있음)
        (c.app / 'office.html').write_bytes(c.app_bytes('office.html') + b'\n<!-- stale-test -->\n')
        plan_file, plan = c.plan()
        pkg = c.target
        if kind == 'current':
            (c.app / 'office.html').write_bytes(c.app_bytes('office.html') + '<!-- 점검 뒤 수정 -->\n'.encode())
        elif kind == 'incoming':
            pkg = build_pkg(c.dir / 'pkg-changed', edits={'office.html': lambda d: d + '<!-- 다른 새 버전 -->\n'.encode()})
        elif kind == 'launcher':
            with open(c.prefix / 'bin/office', 'a') as f:
                f.write('# 직접 고침\n')
        elif kind == 'manifest':
            m = c.manifest()
            m['launchers'] = {'office': 'x' * 64}
            (c.app / '.office-manifest.json').write_text(json.dumps(m))
        before = c.snap()
        r = c.run_new(pkg, '--review-plan', str(plan_file))
        must(r.returncode == 4, 'rc4(점검 뒤 바뀜)이어야 해요: %d %s' % (r.returncode, r.stderr[-300:]))
        must(c.snap() == before, '거절했는데 설치가 바뀌었어요')
        return ok('rc4 무변경(%s)' % kind, 'rc4', 4)
    return scenario


def variant(kind):
    def scenario(c):
        c.install_old()
        target = c.app / 'office.html'
        if kind == 'deleted':
            target.unlink()
        elif kind == 'symlink':
            outside = c.dir / 'outside.txt'
            outside.write_text('바깥 파일')
            target.unlink()
            target.symlink_to(outside)
        elif kind == 'directory':
            target.unlink()
            target.mkdir()
        before = c.snap()
        r = c.run_new()
        must(r.returncode == 3, 'rc3이어야 해요: %d %s' % (r.returncode, r.stderr[-300:]))
        must(c.snap() == before, '막혔는데 설치가 바뀌었어요')
        if kind == 'symlink':
            must((c.dir / 'outside.txt').read_text() == '바깥 파일', '링크 대상이 바뀌었어요')
        return ok('rc3 무변경(%s)' % kind, 'rc3', 3)
    return scenario


def s_deleted_upstream_restore(c):
    c.install_old()
    (c.app / 'office.html').unlink()
    plan_file, plan = c.plan()
    must(plan['resolvable'], '삭제는 선택으로 풀 수 있어야 해요')
    d = c.decision(plan, 'upstream')
    r = c.run_new(None, '--review-plan', str(plan_file), '--resolution', str(d))
    must(r.returncode == 0, '새 버전 선택 실패: ' + r.stderr[-300:])
    must(c.app_bytes('office.html') == c.tpay('office.html'), '새 버전이 아니에요')
    backup = list((c.prefix / 'backups').iterdir())[0]
    r = c.run_new(None, '--restore-backup', str(backup))
    must(r.returncode == 0, '되돌리기 실패: ' + r.stdout[-300:] + r.stderr[-300:])
    must(not (c.app / 'office.html').exists(), '지운 상태로 돌아오지 않았어요')
    return ok('삭제→새 버전 선택→되돌리기 = 지운 상태', 'ok', 0)


def s_orig_tampered_after_plan(c):
    conflict_state(c)
    plan_file, plan = c.plan()
    d = c.decision(plan, 'upstream')
    (c.app / '.office-orig/office.html').write_bytes('점검 뒤 바뀐 원본'.encode())
    before = c.snap()
    r = c.run_new(None, '--review-plan', str(plan_file), '--resolution', str(d))
    must(r.returncode == 4, 'rc4이어야 해요: %d' % r.returncode)
    must(c.snap() == before, '거절했는데 설치가 바뀌었어요')
    return ok('rc4 무변경(점검 뒤 원본 변조)', 'rc4', 4)


def s_candidate_complete(c):
    conflict_state(c, True)
    _, plan = c.plan(candidate=True)
    cand = Path(plan['candidate'])
    for n in payload_names(c.target / 'payload'):
        must((cand / 'files' / n).is_file(), '후보에 빠진 파일: ' + n)
    must((cand / 'files/VERSION').read_bytes() == c.tpay('VERSION'), 'VERSION 후보가 새 버전이 아니에요')
    return ok('후보 사본에 관리 파일 전체', 'ok', 0)


def s_binary(c):
    c.install_old()
    (c.app / 'office.ico').write_bytes(b'\0custom-icon')
    (c.app / 'office-icon.png').write_bytes(b'\0custom-png')
    before = c.snap()
    r = c.run_new()
    same = c.tpay('office.ico') == c.opay('office.ico') and c.tpay('office-icon.png') == c.opay('office-icon.png')
    if same:
        must(r.returncode == 0, '새 버전이 아이콘을 안 바꿨으면 내 아이콘을 두고 진행해야 해요: ' + r.stderr[-300:])
        must(c.app_bytes('office.ico') == b'\0custom-icon' and c.app_bytes('office-icon.png') == b'\0custom-png', '내 아이콘이 사라졌어요')
        return ok('내 아이콘 유지 후 진행', 'kept', 0)
    must(r.returncode == 3, '새 버전도 아이콘을 바꿨으니 막아야 해요: %d' % r.returncode)
    must(c.snap() == before, '막혔는데 설치가 바뀌었어요')
    return ok('rc3 무변경(양쪽 바이너리 변경)', 'rc3', 3)


def baseline(kind):
    def scenario(c):
        c.install_old()
        if not (c.app / '.office-orig/office.py').is_file():
            return ok('옛 설치에 .office-orig 없음 → 건너뜀', 'skip', None, skipped=True)
        (c.app / 'office.py').write_bytes(c.app_bytes('office.py') + b'\n# custom\n')
        if kind == 'deleted':
            (c.app / '.office-orig/office.py').unlink()
        else:
            (c.app / '.office-orig/office.py').write_bytes('# 바뀐 원본\n'.encode())
        before = c.snap()
        r = c.run_new()
        must(r.returncode == 3, 'rc3이어야 해요: %d' % r.returncode)
        must(c.snap() == before, '막혔는데 설치가 바뀌었어요')
        return ok('rc3 무변경(원본 %s)' % kind, 'rc3', 3)
    return scenario


def no_manifest(custom):
    def scenario(c):
        c.install_old()
        if not (c.app / '.office-manifest.json').is_file():
            return ok('옛 설치에 기록 없음 → 건너뜀', 'skip', None, skipped=True)
        (c.app / '.office-manifest.json').unlink()
        if custom:
            (c.app / 'office.html').write_bytes(c.app_bytes('office.html') + b'\n<!-- custom -->\n')
        before = c.snap()
        r = c.run_new()
        must(r.returncode == 3, 'rc3이어야 해요: %d %s' % (r.returncode, r.stderr[-300:]))
        must(c.snap() == before, '막혔는데 설치가 바뀌었어요')
        return ok('rc3 무변경(기록 없음, 수정 %s)' % ('있음' if custom else '없음'), 'rc3', 3)
    return scenario


def s_extras(c):
    c.install_old()
    extras = {c.app / 'user-notes.txt': '메모'.encode(), c.prefix / 'workspace/data.json': b'{"a":1}',
              c.home / '.local/bin/other-tool': b'#!/bin/sh\n', c.home / 'agent-office/chat.json': b'{}'}
    for p, d in extras.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(d)
    r = c.run_new()
    must(r.returncode == 0, '관리 밖 파일 때문에 막히면 안 돼요: ' + r.stderr[-300:])
    for p, d in extras.items():
        must(p.read_bytes() == d, '관리 밖 파일이 바뀌었어요: ' + str(p))
    return ok('rc0, 관리 밖 파일 보존', 'rc0', 0)


def s_launcher_modified(c):
    c.install_old()
    launcher = c.prefix / 'bin/office'
    with open(launcher, 'a') as f:
        f.write('# 내가 추가한 줄\n')
    mine = launcher.read_bytes()
    before = c.snap()
    r = c.run_new()
    must(r.returncode in (0, 3), '예상 밖 종료코드 %d' % r.returncode)
    must(launcher.read_bytes() == mine, '직접 고친 실행 파일을 덮어썼어요')
    if r.returncode == 3:
        must(c.snap() == before, '막혔는데 설치가 바뀌었어요')
    return ok('실행 파일 수정은 덮지 않음(rc0 또는 rc3)', 'rc%d' % r.returncode, r.returncode)


def update_to_t0(c):
    c.install_old()
    r = c.run_new()
    must(r.returncode == 0, 'T0 업데이트 실패: ' + r.stdout[-300:] + r.stderr[-400:])


def s_chain(c):
    update_to_t0(c)
    (c.app / 'office.py').write_bytes(c.app_bytes('office.py') + b'# chain-custom\n')
    (c.app / 'office.html').write_bytes(c.app_bytes('office.html') + b'<!-- chain-custom -->\n')
    t1 = build_pkg(c.dir / 't1', '0.10.1', edits={'office.py': insert_line('# upstream-v1', 5), 'office.html': lambda d: b'<!-- upstream-v1 -->\n' + d})
    r = c.run_new(t1)
    must(r.returncode == 0, 'T1 실패: ' + r.stdout[-300:] + r.stderr[-400:])
    must(b'# chain-custom' in c.app_bytes('office.py') and b'# upstream-v1' in c.app_bytes('office.py'), 'T1 병합 결과가 틀려요')
    check_fully_updated(c, t1)
    t2 = build_pkg(c.dir / 't2', '0.11.0', edits={'office.py': lambda d: insert_line('# upstream-v2', 5)(insert_line('# upstream-v1', 5)(d)), 'office.html': lambda d: b'<!-- upstream-v1 -->\n' + d})
    r = c.run_new(t2)
    must(r.returncode == 0, 'T2 실패: ' + r.stdout[-300:] + r.stderr[-400:])
    must(b'# chain-custom' in c.app_bytes('office.py') and b'# upstream-v2' in c.app_bytes('office.py'), 'T2 병합 결과가 틀려요')
    check_fully_updated(c, t2)
    at_t2 = {n: c.app_bytes(n) for n in ('office.py', 'office.html')}
    r = c.run_new(t2)
    must(r.returncode == 0, '같은 버전 재설치 실패: ' + r.stdout[-300:] + r.stderr[-400:])
    must({n: c.app_bytes(n) for n in at_t2} == at_t2, '재설치가 파일을 바꿨어요')
    backup = sorted((p for p in (c.prefix / 'backups').iterdir() if p.name.startswith('0.11.0-')))[-1]
    r = c.run_new(None, '--restore-backup', str(backup))
    must(r.returncode == 0, '되돌리기 실패: ' + r.stdout[-300:] + r.stderr[-400:])
    must(c.app_bytes('office.py') == at_t2['office.py'], '같은 버전 백업으로 되돌린 결과가 달라요')
    # 이전(0.10.1) 백업으로 한 번 더 되돌려 내 수정이 남는지 확인
    prev = sorted((p for p in (c.prefix / 'backups').iterdir() if p.name.startswith('0.10.1-')))[-1]
    r = c.run_new(None, '--restore-backup', str(prev))
    must(r.returncode == 0, '이전 버전 되돌리기 실패: ' + r.stdout[-300:] + r.stderr[-400:])
    must((c.app / 'VERSION').read_text().strip() == '0.10.1', '되돌린 버전이 아니에요')
    must(b'# chain-custom' in c.app_bytes('office.py') and b'# upstream-v1' in c.app_bytes('office.py') and b'# upstream-v2' not in c.app_bytes('office.py'), '되돌린 내용이 달라요')
    r = c.run_new(t2)
    must(r.returncode == 0, '되돌린 뒤 다시 업데이트 실패: ' + r.stdout[-300:] + r.stderr[-400:])
    must(b'# chain-custom' in c.app_bytes('office.py') and b'# upstream-v2' in c.app_bytes('office.py'), '다시 업데이트 뒤 내 수정/새 변경이 사라졌어요')
    check_fully_updated(c, t2)
    return ok('A→B→C→재설치→되돌리기×2→재업데이트, 내 수정·기준 유지', 'ok', 0)


def s_restore_plain(c):
    c.install_old()
    old = {n: c.app_bytes(n) for n in CORE}
    r = c.run_new()
    must(r.returncode == 0, '업데이트 실패: ' + r.stderr[-300:])
    backup = list((c.prefix / 'backups').iterdir())[0]
    r = c.run_new(None, '--restore-backup', str(backup))
    must(r.returncode == 0, '되돌리기 실패: ' + r.stdout[-300:] + r.stderr[-400:])
    for n in CORE:
        must(c.app_bytes(n) == old[n], '옛 파일로 돌아오지 않았어요: ' + n)
    for extra in payload_names(c.target / 'payload'):
        if extra not in CORE:
            must(not (c.app / extra).exists(), '새 모듈이 남았어요: ' + extra)
    return ok('옛 버전 원상복구, 새 모듈 제거', 'ok', 0)


def restore_refuse(kind):
    def scenario(c):
        update_to_t0(c)
        (c.app / 'office.py').write_bytes(c.app_bytes('office.py') + b'# custom-before-update\n')
        t1 = build_pkg(c.dir / 't1', '0.10.1', edits={'office.py': insert_line('# up1', 5)})
        r = c.run_new(t1)
        must(r.returncode == 0, 'T1 실패: ' + r.stderr[-300:])
        backup = sorted(p for p in (c.prefix / 'backups').iterdir() if p.name.startswith('0.10.0-'))[-1]
        if kind == 'edited-after':
            (c.app / 'office.html').write_bytes(c.app_bytes('office.html') + '<!-- 업데이트 뒤 수정 -->\n'.encode())
        elif kind == 'tampered':
            (backup / 'office.py').write_bytes(b'tampered')
        elif kind == 'outside':
            outside = c.dir / 'elsewhere'
            shutil.copytree(backup, outside)
            backup = outside
        before = c.snap()
        r = c.run_new(t1, '--restore-backup', str(backup))
        must(r.returncode == 5, 'rc5이어야 해요: %d %s' % (r.returncode, r.stderr[-300:]))
        must(c.snap() == before, '거절했는데 설치가 바뀌었어요')
        return ok('rc5 무변경(%s)' % kind, 'rc5', 5)
    return scenario


def s_restore_custom_ok(c):
    update_to_t0(c)
    custom = c.app_bytes('office.py') + b'# custom-before-update\n'
    (c.app / 'office.py').write_bytes(custom)
    t1 = build_pkg(c.dir / 't1', '0.10.1', edits={'office.py': insert_line('# up1', 5)})
    r = c.run_new(t1)
    must(r.returncode == 0, 'T1 실패: ' + r.stderr[-300:])
    backup = sorted(p for p in (c.prefix / 'backups').iterdir() if p.name.startswith('0.10.0-'))[-1]
    r = c.run_new(t1, '--restore-backup', str(backup))
    must(r.returncode == 0, '내 수정이 합쳐 보존된 설치인데 되돌리기가 거절됐어요: ' + r.stdout[-300:] + r.stderr[-400:])
    must(c.app_bytes('office.py') == custom, '되돌린 파일이 업데이트 직전과 달라요')
    return ok('합쳐 보존된 설치도 되돌리기 가능', 'ok', 0)


def mv_fail(position):
    def scenario(c):
        c.install_old()
        before = c.snap(managed_only=True)
        counter = c.dir / 'mvcount'
        (c.cmds / 'mv').write_text('#!/bin/sh\nn=$(cat %s 2>/dev/null || echo 0); n=$((n+1)); echo $n > %s\ncase "$*" in *app/*) m=$(cat %s.app 2>/dev/null || echo 0); m=$((m+1)); echo $m > %s.app; if [ "$m" -eq %d ]; then exit 1; fi;; esac\nexec /bin/mv "$@"\n' % (counter, counter, counter, counter, position))
        (c.cmds / 'mv').chmod(0o700)
        r = c.run_new()
        (c.cmds / 'mv').unlink()
        must(r.returncode != 0, '실패해야 하는데 성공했어요')
        must('되돌려요' in r.stderr, '자동 복원 안내가 없어요: ' + r.stderr[-300:])
        after = c.snap(managed_only=True)
        must(after == before, '실패 뒤 이전 파일로 돌아오지 않았어요: ' + str([k for k in set(before) | set(after) if before.get(k) != after.get(k)][:5]))
        return ok('중간 실패 → 이전 파일 복원(%d번째 이동)' % position, 'restored', r.returncode)
    return scenario


def module_scenario(kind):
    def scenario(c):
        update_to_t0(c)
        extra = {'office_extra.py': b''.join(b'value_%d = %d\n' % (i, i) for i in range(1, 31))}
        if kind == 'preexisting':
            (c.app / 'office_extra.py').write_text('내가 만든 파일\n')
            before = c.snap()
            t1 = build_pkg(c.dir / 't1', '0.10.1', extra=extra)
            r = c.run_new(t1)
            must(r.returncode == 3, '같은 이름 기존 파일은 막아야 해요: %d' % r.returncode)
            must(c.snap() == before, '막혔는데 설치가 바뀌었어요')
            return ok('rc3 무변경(같은 이름 기존 파일)', 'rc3', 3)
        t1 = build_pkg(c.dir / 't1', '0.10.1', extra=extra)
        r = c.run_new(t1)
        must(r.returncode == 0, '새 모듈 추가 업데이트가 막혔어요: ' + r.stdout[-300:] + r.stderr[-400:])
        must((c.app / 'office_extra.py').is_file() and 'office_extra.py' in c.manifest()['files'], '새 모듈이 설치되지 않았어요')
        if kind == 'add':
            return ok('새 모듈 추가 rc0', 'ok', 0)
        if kind == 'edit-merge':
            mine = c.app_bytes('office_extra.py').replace(b'value_2 = 2\n', b'value_2 = 200\n')
            (c.app / 'office_extra.py').write_bytes(mine)
            t2_extra = {'office_extra.py': extra['office_extra.py'].replace(b'value_28 = 28\n', b'value_28 = 2800\n')}
            t2 = build_pkg(c.dir / 't2', '0.10.2', extra=t2_extra)
            r = c.run_new(t2)
            must(r.returncode == 0, '모듈 병합 실패: ' + r.stderr[-300:])
            data = c.app_bytes('office_extra.py')
            must(b'value_2 = 200' in data and b'value_28 = 2800' in data, '모듈 수정/새 변경이 사라졌어요')
            return ok('모듈 수정+새 변경 병합', 'merged', 0)
        if kind == 'deleted':
            (c.app / 'office_extra.py').unlink()
            before = c.snap()
            t2 = build_pkg(c.dir / 't2', '0.10.2', extra=extra)
            r = c.run_new(t2)
            must(r.returncode == 3, '직접 지운 모듈은 막아야 해요: %d' % r.returncode)
            must(c.snap() == before, '막혔는데 설치가 바뀌었어요')
            return ok('rc3 무변경(직접 지운 모듈)', 'rc3', 3)
        if kind == 'dropped':
            t2 = build_pkg(c.dir / 't2', '0.10.2')          # 모듈 목록에서 빠짐
            r = c.run_new(t2)
            must(r.returncode == 0, '모듈이 빠진 업데이트 실패: ' + r.stderr[-300:])
            must('office_extra.py' not in c.manifest()['files'], '기록에 남았어요')
            must((c.app / 'office_extra.py').is_file(), '관리에서 빠진 파일을 지웠어요')
            return ok('관리 목록에서 빠져도 파일 유지', 'ok', 0)
        raise AssertionError(kind)
    return scenario


def mac_scenario(kind):
    def scenario(c):
        c.install_old()
        (c.cmds / 'uname').write_text('#!/bin/sh\nif [ "$1" = "-s" ]; then echo Darwin; else exec /usr/bin/uname "$@"; fi\n')
        (c.cmds / 'sw_vers').write_text('#!/bin/sh\necho 14.0\n')
        for n in ('uname', 'sw_vers'):
            (c.cmds / n).chmod(0o700)
        (c.app / 'office.html').write_bytes(c.app_bytes('office.html') + b'\n<!-- mac-custom -->\n')
        exp, merged = merged_expect(c, 'office.html', c.app_bytes('office.html'))
        if kind == 'update':
            r = c.run_new()
            if exp == 'ok':
                must(r.returncode == 0, '맥 분기 업데이트 실패: ' + r.stdout[-300:] + r.stderr[-400:])
                must(c.app_bytes('office.html') == merged, '맥 분기 병합 결과가 달라요')
                must(not (c.prefix / '.install.lock.d').exists(), '맥 잠금 폴더가 남았어요')
            else:
                must(r.returncode == 3, 'rc3이어야 해요')
            return ok('맥 분기 모의 업데이트', 'rc%d' % r.returncode, r.returncode)
        if kind == 'stale':
            plan_file, plan = c.plan()
            (c.app / 'office.html').write_bytes(c.app_bytes('office.html') + '<!-- 점검 뒤 -->\n'.encode())
            before = c.snap()
            r = c.run_new(None, '--review-plan', str(plan_file))
            must(r.returncode == 4, 'rc4이어야 해요: %d' % r.returncode)
            must(c.snap() == before, '거절했는데 설치가 바뀌었어요')
            return ok('맥 분기 모의 stale rc4', 'rc4', 4)
        if kind == 'restore':
            (c.app / 'office.html').write_bytes(c.opay('office.html'))
            r = c.run_new()
            must(r.returncode == 0, '업데이트 실패: ' + r.stderr[-300:])
            backup = list((c.prefix / 'backups').iterdir())[0]
            r = c.run_new(None, '--restore-backup', str(backup))
            must(r.returncode == 0, '맥 분기 되돌리기 실패: ' + r.stdout[-300:] + r.stderr[-400:])
            must((c.app / 'VERSION').read_text().strip() == c.version, '버전이 돌아오지 않았어요')
            return ok('맥 분기 모의 되돌리기', 'ok', 0)
        raise AssertionError(kind)
    return scenario


SCENARIOS = {
    'S01-정상직행': s_plain,
    'S02-화면추가(html끝)': s_html_far,
    'S03-파이썬기능추가(py끝)': s_py_far,
    'S04-양쪽같은수정': s_same_change,
    'S05-같은곳다른값충돌': s_conflict_block,
    'S06-내수정유지(적용안함)': s_keep_mine,
    'S07-새버전선택': s_choose_upstream,
    'S08-후보사본합치기': s_choose_merged,
    'S09a-합치기실패(충돌표시)': merged_bad('markers'),
    'S09b-합치기실패(문법)': merged_bad('syntax'),
    'S09c-합치기실패(후보변조)': merged_bad('sha'),
    'S09d-합치기실패(후보없음)': merged_bad('missing'),
    'S10a-점검뒤수정': stale('current'),
    'S10b-점검뒤새버전변경': stale('incoming'),
    'S10c-점검뒤런처수정': stale('launcher'),
    'S10d-점검뒤기록수정': stale('manifest'),
    'S11a-삭제': variant('deleted'),
    'S11b-링크': variant('symlink'),
    'S11c-디렉터리': variant('directory'),
    'S10e-점검뒤원본변조': s_orig_tampered_after_plan,
    'S11d-삭제→새버전선택→되돌리기': s_deleted_upstream_restore,
    'S11e-후보사본전체파일': s_candidate_complete,
    'S12-바이너리수정': s_binary,
    'S13a-원본삭제': baseline('deleted'),
    'S13b-원본변조': baseline('tampered'),
    'S14a-기록삭제+수정': no_manifest(True),
    'S14b-기록삭제': no_manifest(False),
    'S15-관리밖파일': s_extras,
    'S16-런처수정': s_launcher_modified,
    'S17-연속이전A→B→C': s_chain,
    'S18a-되돌리기(수정없음)': s_restore_plain,
    'S18b-되돌리기(합쳐보존)': s_restore_custom_ok,
    'S18c-되돌리기거절(사후수정)': restore_refuse('edited-after'),
    'S18d-되돌리기거절(백업변조)': restore_refuse('tampered'),
    'S18e-되돌리기거절(백업폴더밖)': restore_refuse('outside'),
    'S19a-중간실패복원(2번째)': mv_fail(2),
    'S19b-중간실패복원(5번째)': mv_fail(5),
    'S19c-중간실패복원(마지막)': mv_fail(9),
    'S20a-새모듈추가': module_scenario('add'),
    'S20b-같은이름기존파일': module_scenario('preexisting'),
    'S20c-모듈수정병합': module_scenario('edit-merge'),
    'S20d-모듈직접삭제': module_scenario('deleted'),
    'S20e-모듈목록에서제거': module_scenario('dropped'),
}
MAC = {'S21a-맥모의업데이트': mac_scenario('update'), 'S21b-맥모의점검뒤변경': mac_scenario('stale'), 'S21c-맥모의되돌리기': mac_scenario('restore')}
MAC_VERSIONS = ('0.8.0', '0.8.8', '0.9.0')
VIRTUAL = {'S17-연속이전A→B→C', 'S18b-되돌리기(합쳐보존)', 'S18c-되돌리기거절(사후수정)', 'S18d-되돌리기거절(백업변조)', 'S18e-되돌리기거절(백업폴더밖)',
           'S20a-새모듈추가', 'S20b-같은이름기존파일', 'S20c-모듈수정병합', 'S20d-모듈직접삭제', 'S20e-모듈목록에서제거'}


def run_case(job):
    version, rev, name, target, work = job
    fn = SCENARIOS.get(name) or MAC[name]
    started = time.time()
    record = {'version': version, 'scenario': name, 'kind': '가상 미래 버전 포함' if name in VIRTUAL else '실제 원본→현재 제품',
              'start_rev': rev}
    base = Path(tempfile.mkdtemp(prefix='m-', dir=work))
    try:
        c = Ctx(base, version, rev, target)
        result = fn(c)
        record.update(result=('skip' if result.get('skipped') else 'pass'), **{k: v for k, v in result.items() if k != 'skipped'})
    except AssertionError as error:
        record.update(result='fail', error=str(error)[:800])
    except Exception as error:   # 시험 도구 자체 오류도 숨기지 않아요
        record.update(result='error', error=''.join(traceback.format_exception_only(type(error), error))[:800] + traceback.format_exc()[-600:])
    record['seconds'] = round(time.time() - started, 2)
    shutil.rmtree(base, ignore_errors=True)
    return record


def progress(done, total, state):
    try:
        subprocess.run(['office', 'progress', '--done', str(done), '--total', str(total), '--state', 'working', state], capture_output=True, timeout=20)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--out', default=str(ROOT / 'evidence/review/update-migration-2026-11-03'))
    ap.add_argument('--versions')
    ap.add_argument('--only')
    ap.add_argument('--no-progress', action='store_true')
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    versions = [v for v in REVS if not args.versions or v[0] in args.versions.split(',')]
    only = set(args.only.split(',')) if args.only else None
    CACHE.mkdir(parents=True, exist_ok=True)
    for v, rev in versions:
        extract(v, rev)
    work = Path(tempfile.mkdtemp(prefix='update-matrix-', dir='/tmp'))
    target = build_pkg(work / 'target', '0.10.0')
    fingerprints = {n: sha((ROOT / n).read_bytes()) for n in ('install.sh', 'release.sh', 'payload/office_customizations.py', 'payload/office.py', 'payload/office.html')}
    jobs = []
    for v, rev in versions:
        for name in list(SCENARIOS) + (list(MAC) if v in MAC_VERSIONS else []):
            if only and not any(name.startswith(o) for o in only):
                continue
            jobs.append((v, rev, name, str(target), str(work)))
    total = len(jobs)
    records, started = [], time.time()
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        for i, rec in enumerate(pool.map(run_case, jobs), 1):
            records.append(rec)
            if not args.no_progress and (i % 15 == 0 or i == total):
                fails = sum(1 for r in records if r['result'] in ('fail', 'error'))
                progress(i, total, '실제 원본 이전 시험 %d/%d, 실패 %d' % (i, total, fails))
    (out / 'cases.jsonl').write_text(''.join(json.dumps(dict(r, source=fingerprints), ensure_ascii=False) + '\n' for r in records))
    counts = {k: sum(1 for r in records if r['result'] == k) for k in ('pass', 'fail', 'error', 'skip')}
    summary = {'total': total, 'counts': counts, 'seconds': round(time.time() - started, 1), 'fingerprints': fingerprints,
               'virtual_cases': sum(1 for r in records if r['kind'].startswith('가상')), 'real_cases': sum(1 for r in records if not r['kind'].startswith('가상'))}
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1) + '\n')
    for r in records:
        if r['result'] in ('fail', 'error'):
            print('실패 %s %s: %s' % (r['version'], r['scenario'], r.get('error', '')[:300]))
    print(json.dumps(summary, ensure_ascii=False))
    shutil.rmtree(work, ignore_errors=True)
    return 1 if counts['fail'] or counts['error'] else 0


if __name__ == '__main__':
    sys.exit(main())
