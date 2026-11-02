#!/usr/bin/env python3
"""설치·업데이트 때 사용자가 직접 고친 파일을 지키는 점검 엔진이에요.

  --plan            설치를 바꾸지 않고 점검 결과(JSON)만 --output 경로에 써요.
                    --candidate-dir 를 주면 합친 후보 사본도 그 폴더에 만들어요.
  --apply           설치기가 쓰는 준비 폴더(--staging)에 기준 기록을 만들고 겹치지 않는 수정을 합쳐요.
                    막힐 이유가 있으면 아무것도 바꾸지 않고 종료 코드 3으로 멈춰요.
                    --resolution 으로 사용자의 선택(새 버전 / 합친 후보)을 받아 파일 단위 문제를 풀 수 있어요.
  --restore-backup  백업으로 앱 파일을 되돌려요.

관리 파일 목록은 --incoming 폴더의 SHA256SUMS에서 읽어요.
종료 코드: 0 성공 / 2 점검 못 함 / 3 막힘 / 4 점검 뒤 설치가 바뀜 / 5 되돌릴 수 없음.
표준 라이브러리만 써요.
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA = 1
# 예전 설치(관리 파일 5개)에서도 항상 기록에 있던 파일이에요. 이 파일이 기록에 없으면 기록이 깨진 거예요.
CORE = ('office.py', 'office.html', 'office.ico', 'office-icon.png', 'VERSION')
MANIFEST = '.office-manifest.json'
ORIG = '.office-orig'
BACKUP_STATE = '.backup-state.json'
BACKUP_OWNER = '.office-backup'      # 설치기가 만든 백업 표시. 이 표시가 있는 폴더만 오래된 것부터 지워요
KEEP_BACKUPS = 10
MARKERS = (b'<<<<<<<', b'>>>>>>>')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_names(incoming):
    """SHA256SUMS에 적힌 관리 파일 이름 목록. 이름이 이상하면 ValueError."""
    sums = Path(incoming) / 'SHA256SUMS'
    names = []
    for line in sums.read_text().splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise ValueError('SHA256SUMS 형식이 달라요: ' + line)
        name = parts[1].strip().lstrip('*')
        if (not name or '/' in name or '\\' in name or name.startswith('.') or name in ('.', '..')
                or any(ord(c) < 32 for c in name)):
            raise ValueError('관리 파일 이름이 안전하지 않아요: ' + name)
        if name in names:
            raise ValueError('관리 파일이 두 번 적혀 있어요: ' + name)
        file = Path(incoming) / name
        if file.is_symlink() or not file.is_file():
            raise ValueError('배포 파일이 없어요: ' + name)
        names.append(name)
    for need in ('office.py', 'VERSION'):
        if need not in names:
            raise ValueError('필수 배포 파일이 목록에 없어요: ' + need)
    return names


def load_manifest(app):
    try:
        old = json.loads((app / MANIFEST).read_text())
        if not isinstance(old, dict) or not isinstance(old.get('files'), dict):
            return None
        return old
    except (OSError, ValueError, TypeError):
        return None


def fingerprint(path):
    """파일 상태를 지문 하나로. 없음·링크·폴더도 구분해요."""
    path = Path(path)
    if path.is_symlink():
        return 'link'
    if not path.exists():
        return 'missing'
    if not path.is_file():
        return 'dir'
    return digest(path)


def merge_text(current, base, target, keep_markers=False):
    """(상태, 합친 바이트 또는 사유). 상태: ok / conflict / blocked.
    keep_markers면 충돌 때 충돌 표시가 든 본문을 돌려줘요(후보 사본용)."""
    data = [p.read_bytes() for p in (base, current, target)]
    if any(b'\0' in d for d in data):
        return 'blocked', '바이너리 파일을 직접 고쳤어요'
    if shutil.which('git'):
        cmd = ['git', 'merge-file', '-p', str(current), str(base), str(target)]
    elif shutil.which('diff3'):
        cmd = ['diff3', '-m', str(current), str(base), str(target)]
    else:
        return 'blocked', '합치는 도구(git, diff3)가 없어요'
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        if keep_markers and result.stdout:
            return 'conflict', result.stdout
        return 'conflict', '같은 곳을 서로 다르게 바꿨어요'
    if current.name.endswith('.py'):
        try:
            compile(result.stdout, current.name, 'exec')
        except (SyntaxError, ValueError):
            if keep_markers:
                return 'conflict', result.stdout
            return 'conflict', '합친 결과가 실행되지 않아요'
    return 'ok', result.stdout


def analyze(root, incoming, names, candidate=None):
    """설치를 바꾸지 않고 점검해요. (plan, merged, old) — merged는 {이름: 합친 바이트}.
    candidate 폴더를 주면 합친 후보 사본을 만들어요(설치는 그대로)."""
    root, incoming = Path(root), Path(incoming)
    app = root / 'app'
    old = load_manifest(app)
    legacy = old is None and app.is_dir() and any(app.iterdir())
    files, merged = [], {}
    file_blocked, global_blocked = [], []
    current_fp = {name: fingerprint(app / name) for name in names}
    incoming_fp = {name: digest(incoming / name) for name in names}
    if candidate:
        (candidate / 'files').mkdir(parents=True)
        (candidate / 'conflicts').mkdir()

    def add(name, status, reason=''):
        files.append({'name': name, 'status': status, 'reason': reason})

    def candidate_for(name, body, cur, base):
        if not candidate:
            return
        (candidate / 'files' / name).write_bytes(body)
        folder = candidate / 'conflicts' / name
        folder.mkdir()
        if cur.is_file() and not cur.is_symlink():
            shutil.copyfile(cur, folder / 'current')
        if base.is_file() and not base.is_symlink():
            shutil.copyfile(base, folder / 'base')
        shutil.copyfile(incoming / name, folder / 'incoming')

    for name in names:
        cur, base, target = app / name, app / ORIG / name, incoming / name
        exists = cur.exists() or cur.is_symlink()
        recorded = old['files'].get(name) if old else None
        in_record = bool(old) and name in old['files']
        if old and not in_record and name not in CORE:
            # 이번에 새로 생긴 관리 파일이에요. 없다고 직접 지운 걸로 보지 않아요.
            if not exists:
                add(name, 'added'); continue
            if cur.is_file() and not cur.is_symlink() and digest(cur) == incoming_fp[name]:
                add(name, 'unchanged'); continue
            add(name, 'blocked', '같은 이름의 파일이 이미 있어서 덮지 않아요')
            file_blocked.append('새 파일 이름과 같은 파일이 이미 있어요: ' + name)
            if cur.is_file() and not cur.is_symlink():
                candidate_for(name, cur.read_bytes(), cur, base)
            continue
        if not exists:
            if old:
                add(name, 'blocked', '직접 지운 파일이에요')
                file_blocked.append('직접 지운 파일이 있어요: ' + name)
                candidate_for(name, target.read_bytes(), cur, base)
            else:
                add(name, 'added')
            continue
        expected = recorded if old else incoming_fp[name]
        if cur.is_symlink() or not cur.is_file():
            add(name, 'blocked', '파일이 아닌 것으로 바뀌어 있어요')
            global_blocked.append('파일이 링크나 폴더로 바뀌어 있어요: ' + name)   # 선택으로도 덮지 않아요
            continue
        cur_digest = digest(cur)
        if cur_digest == expected:
            add(name, 'unchanged' if incoming_fp[name] == expected else 'update')
            continue
        # 여기부터는 사용자가 고친 파일이에요.
        if not old:
            add(name, 'blocked', '설치 기록이 없어서 어디를 고쳤는지 알 수 없어요')
            candidate_for(name, cur.read_bytes(), cur, base)
            continue
        if name == 'VERSION':
            add(name, 'blocked', '버전 표시를 직접 바꿨어요')
            file_blocked.append('버전 표시(VERSION)를 직접 바꿨어요')
            candidate_for(name, cur.read_bytes(), cur, base)
            continue
        if base.is_symlink() or not base.is_file() or digest(base) != recorded:
            add(name, 'blocked', '예전 원본이 없거나 달라서 비교할 수 없어요')
            file_blocked.append('예전 원본을 확인할 수 없어요: ' + name)
            candidate_for(name, cur.read_bytes(), cur, base)
            continue
        if cur_digest == incoming_fp[name]:
            add(name, 'same-change', '새 버전과 같은 수정이에요')
            continue
        if incoming_fp[name] == recorded:
            mine = cur.read_bytes()
            if name.endswith('.py'):
                try:
                    compile(mine, name, 'exec')
                except (SyntaxError, ValueError):
                    add(name, 'conflict', '내 수정이 실행되지 않아요')
                    candidate_for(name, mine, cur, base)
                    continue
            merged[name] = mine   # 새 버전에서 바뀐 게 없으니 내 파일을 그대로 둬요
            add(name, 'custom-only', '내 수정만 있어요')
            continue
        state, value = merge_text(cur, base, target, keep_markers=bool(candidate))
        if state == 'ok':
            merged[name] = value
            add(name, 'merged', '서로 다른 곳을 고쳐서 합쳤어요')
        elif state == 'conflict':
            reason = '같은 곳을 서로 다르게 바꿨어요'
            add(name, 'conflict', reason)
            if candidate and isinstance(value, bytes):
                candidate_for(name, value, cur, base)
        else:
            add(name, 'blocked', value)
            file_blocked.append('%s: %s' % (name, value))
            candidate_for(name, cur.read_bytes(), cur, base)
    if candidate:
        # 후보 사본은 새 버전 전체 위에 비충돌 병합 결과를 얹고, 충돌 파일만 표시본으로 덮어요.
        for name in names:
            out = candidate / 'files' / name
            if not out.exists():
                out.write_bytes(merged.get(name, (incoming / name).read_bytes()))
    conflicts = [f['name'] for f in files if f['status'] == 'conflict']
    if conflicts:
        file_blocked.append('직접 고친 파일을 새 버전과 자동으로 합칠 수 없어요: ' + ','.join(conflicts))
    # 실행 파일 보호: 선택으로 풀 수 없어요.
    recorded_launchers = (old or {}).get('launchers') or {}
    for label, path, key in (('실행 파일 bin/office', root / 'bin' / 'office', 'office'),
                             ('콘솔 파일 bin/office-console.bashrc', root / 'bin' / 'office-console.bashrc', 'console')):
        if not (path.exists() or path.is_symlink()):
            continue
        if path.is_symlink() or not path.is_file():
            global_blocked.append(label + '를 직접 고친 것 같아요')
        elif recorded_launchers.get(key) and digest(path) != recorded_launchers[key]:
            global_blocked.append(label + '를 직접 고친 것 같아요')
    try:
        installed_version = (app / 'VERSION').read_text().strip()
    except OSError:
        installed_version = ''
    incoming_version = (incoming / 'VERSION').read_text().strip()
    if '+local' in installed_version and '+local' not in incoming_version:
        global_blocked.append('이 PC 전용 버전(%s) 위에 공개 버전을 덮어쓸 수 없어요' % installed_version)
    changed = [f['name'] for f in files if f['status'] in ('merged', 'conflict', 'blocked', 'custom-only', 'same-change')]
    if legacy and changed:
        file_blocked.append('설치 기록이 없는 옛 설치라 어디를 직접 고쳤는지 알 수 없어요: ' + ','.join(changed))
    blocked = file_blocked + global_blocked
    launcher_fp = {k: fingerprint(root / 'bin' / v) for k, v in (('office', 'office'), ('console', 'office-console.bashrc'))}
    orig_fp = {name: fingerprint(app / ORIG / name) for name in names}
    state = {'current': current_fp, 'incoming': incoming_fp, 'launchers': launcher_fp, 'orig': orig_fp,
             'manifest': fingerprint(app / MANIFEST), 'installed_version': installed_version}
    plan_id = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]
    plan = {'schema': SCHEMA, 'id': plan_id, 'version': incoming_version,
            'installed_version': installed_version, 'legacy': bool(legacy), 'files': files,
            'conflicts': conflicts, 'blocked': blocked,
            'resolvable': bool(blocked) and not global_blocked,
            'problem_files': [f['name'] for f in files if f['status'] in ('conflict', 'blocked')],
            'candidate': str(candidate) if candidate else None,
            'current': current_fp, 'incoming': incoming_fp}
    if candidate:
        (candidate / 'candidate.json').write_text(json.dumps(
            {'plan_id': plan_id, 'names': plan['problem_files']}, ensure_ascii=False) + '\n')
    return plan, merged, old


def cmd_plan(args):
    names = read_names(args.incoming)
    candidate = None
    if args.candidate_dir:
        candidate = Path(args.candidate_dir)
        if candidate.exists() and (candidate.is_symlink() or not candidate.is_dir() or any(candidate.iterdir())):
            raise ValueError('후보 폴더는 비어 있는 폴더여야 해요: ' + str(candidate))
        candidate.mkdir(parents=True, exist_ok=True)
    plan, _, _ = analyze(args.prefix, args.incoming, names, candidate)
    out = Path(args.output)
    tmp = out.with_name(out.name + '.tmp')
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + '\n')
    os.replace(tmp, out)
    print('OFFICE_PLAN: id=%s blocked=%d conflicts=%d' % (plan['id'], len(plan['blocked']), len(plan['conflicts'])))
    return 0


def load_resolution(path, plan):
    """선택 파일을 검사해요. 문제가 있으면 ValueError."""
    try:
        decision = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise ValueError('선택 파일을 읽을 수 없어요: %s' % error)
    if not isinstance(decision, dict) or decision.get('schema') != SCHEMA:
        raise ValueError('선택 파일 형식이 달라요')
    if decision.get('plan_id') != plan['id']:
        raise ValueError('선택한 뒤 설치가 바뀌었어요')
    if decision.get('choice') not in ('upstream', 'merged'):
        raise ValueError('알 수 없는 선택이에요')
    if not plan['resolvable']:
        raise ValueError('이 문제는 선택으로 풀 수 없어요')
    return decision


def candidate_files(decision, plan):
    """합친 후보 파일을 검사하고 {이름: 바이트}로 돌려줘요."""
    folder = Path(str(decision.get('candidate_dir') or ''))
    shas = decision.get('candidate_sha')
    if not folder.is_dir() or folder.is_symlink() or not isinstance(shas, dict):
        raise ValueError('후보 폴더나 지문이 없어요')
    result = {}
    for name in plan['problem_files']:
        file = folder / 'files' / name
        if file.is_symlink() or not file.is_file():
            raise ValueError('후보 파일이 없어요: ' + name)
        data = file.read_bytes()
        if shas.get(name) != hashlib.sha256(data).hexdigest():
            raise ValueError('후보 파일 지문이 달라요: ' + name)
        if any(re_line_marker(data, m) for m in MARKERS):
            raise ValueError('충돌 표시가 남아 있어요: ' + name)
        if name.endswith('.py'):
            try:
                compile(data, name, 'exec')
            except (SyntaxError, ValueError):
                raise ValueError('후보가 실행되지 않아요: ' + name)
        result[name] = data
    return result


def re_line_marker(data, marker):
    return any(line.startswith(marker) for line in data.splitlines())


def prune_backups(backups, keep_path):
    try:
        entries = sorted((p for p in backups.iterdir() if p.is_dir() and not p.is_symlink()
                          and (p / BACKUP_OWNER).is_file() and not (p / BACKUP_OWNER).is_symlink()), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for p in entries[:-KEEP_BACKUPS]:
        if p != keep_path:
            shutil.rmtree(p, ignore_errors=True)


def cmd_apply(args):
    names = read_names(args.incoming)
    root, incoming, staging = Path(args.prefix), Path(args.incoming), Path(args.staging)
    plan, merged, old = analyze(root, incoming, names)
    if args.expect_plan_id and args.expect_plan_id != plan['id']:
        sys.stderr.write('점검한 뒤 설치 상태가 바뀌었어요. 다시 점검해 주세요. 기존 설치는 그대로예요.\n')
        return 4
    resolved = {}
    if args.resolution:
        try:
            decision = load_resolution(args.resolution, plan)
            if decision['choice'] == 'merged':
                resolved = candidate_files(decision, plan)
        except ValueError as error:
            sys.stderr.write('선택을 적용하지 않았어요: %s. 기존 설치는 그대로예요.\n' % error)
            return 4 if '바뀌었어요' in str(error) else 3
        plan['blocked'] = []
    if plan['blocked']:
        print('OFFICE_LOCAL: blocked=1 conflicts=%s' % (','.join(plan['conflicts']) or '-'))
        print('OFFICE_LOCAL_JSON: ' + json.dumps({'blocked': True, 'conflicts': plan['conflicts']}, ensure_ascii=False))
        sys.stderr.write('아래 이유로 설치를 멈췄어요. 기존 설치는 그대로예요.\n')
        for reason in plan['blocked']:
            sys.stderr.write('- ' + reason + '\n')
        return 3
    app = root / 'app'
    # files = 새 버전 원본 지문(다음 비교의 기준), installed = 실제로 설치되는 파일 지문(되돌리기 검사용)
    installed = {n: plan['incoming'][n] for n in names}
    for name, data in {**merged, **resolved}.items():
        installed[name] = hashlib.sha256(data).hexdigest()
    new_manifest = {'version': plan['version'], 'files': {n: plan['incoming'][n] for n in names}, 'installed': installed}
    orig = staging / ORIG
    orig.mkdir()
    for name in names:
        shutil.copyfile(incoming / name, orig / name)
    # 합친 결과는 준비 폴더에만 써요. 새 버전 원본(.office-orig)은 그대로 둬요.
    for name, data in merged.items():
        (staging / name).write_bytes(data)
    for name, data in resolved.items():
        (staging / name).write_bytes(data)
    # 마지막 안전 확인: 설치될 모든 파이썬 파일이 실행 가능해야 해요(백업·설치 변경 전에 멈춰요)
    for name in names:
        if name.endswith('.py'):
            try:
                compile((staging / name).read_bytes(), name, 'exec')
            except (SyntaxError, ValueError):
                sys.stderr.write('설치될 %s 가 실행되지 않아요. 기존 설치는 그대로예요.\n' % name)
                return 3
    changed = [f['name'] for f in plan['files'] if f['status'] in ('merged', 'custom-only', 'same-change', 'conflict', 'blocked')]
    backup = None
    if app.is_dir() and any(app.iterdir()):
        version = str((old or {}).get('version', 'legacy'))
        version = ''.join(c if c.isalnum() or c in '.-+' else '_' for c in version)[:80] or 'legacy'
        backups = root / 'backups'
        if backups.is_symlink():
            raise RuntimeError('백업 폴더가 심볼릭 링크예요')
        backups.mkdir(exist_ok=True)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        backup = backups / (version + '-' + stamp)
        shutil.copytree(app, backup, symlinks=True)
        # 백업 때의 실제 파일 지문. 내가 고친 파일도 있어서 기록(manifest)과 다를 수 있어요.
        state = {n: fingerprint(backup / n) for n in (old or {}).get('files', {})}
        (backup / BACKUP_STATE).write_text(json.dumps(state, ensure_ascii=False) + '\n')
        (backup / BACKUP_OWNER).write_text('office-installer\n')
        prune_backups(backups, backup)
        (staging / '.backup-path').write_text(str(backup))   # 설치기가 실패하면 이 백업으로 되돌려요
    (staging / MANIFEST).write_text(json.dumps(new_manifest, ensure_ascii=False) + '\n')
    print('OFFICE_LOCAL: changed=%d merged=%s conflicts=%s backup=%s' %
          (len(changed) if old else 0, ','.join(merged) or '-', ','.join(sorted(resolved)) or '-', str(backup) if backup else '-')
          + (' legacy=1' if plan['legacy'] else '')
          + (' resolved=%s' % json.loads(Path(args.resolution).read_text())['choice'] if args.resolution else ''))
    # 경로에 공백이 있어도 안전하게 읽을 수 있는 같은 내용의 JSON 한 줄
    print('OFFICE_LOCAL_JSON: ' + json.dumps({
        'blocked': False, 'changed': len(changed) if old else 0, 'merged': sorted(merged), 'conflicts': sorted(resolved),
        'backup': str(backup) if backup else None, 'legacy': bool(plan['legacy']),
        'resolved': json.loads(Path(args.resolution).read_text())['choice'] if args.resolution else None}, ensure_ascii=False))
    return 0


def installed_files(manifest):
    """설치된 실제 파일 지문. 옛 기록에는 없으니 그땐 원본 지문과 같다고 봐요."""
    got = manifest.get('installed')
    return got if isinstance(got, dict) else manifest['files']


def cmd_restore(args):
    """백업의 앱 파일로 되돌려요. 대화·작업 폴더와 관리 목록 밖 파일은 건드리지 않아요."""
    root = Path(args.prefix)
    backup = Path(args.restore_backup)
    app, backups = root / 'app', root / 'backups'
    if (backups.is_symlink() or not backups.is_dir() or backup.is_symlink() or not backup.is_dir()
            or backup.resolve().parent != backups.resolve()):
        sys.stderr.write('이 백업은 되돌릴 수 없어요: 설치 폴더의 backups 바로 아래 폴더만 쓸 수 있어요.\n')
        return 5
    old = load_manifest(backup)
    if old is None:
        sys.stderr.write('이 백업은 되돌릴 수 없어요: 설치 기록이 없어요.\n')
        return 5
    try:
        expected = json.loads((backup / BACKUP_STATE).read_text())
        if not isinstance(expected, dict) or set(expected) != set(old['files']):
            raise ValueError
    except (OSError, ValueError):
        expected = installed_files(old)    # 옛 백업: 기록과 같아야 되돌려요
    for name, recorded in expected.items():
        file = backup / name
        if '/' in name or name.startswith('.') or fingerprint(file) != recorded:
            sys.stderr.write('이 백업은 되돌릴 수 없어요: 백업 파일이 기록과 달라요 (%s).\n' % name)
            return 5
    current = load_manifest(app) or {'files': {}}
    if not args.during_install:
        # 설치한 뒤 직접 고친 파일이 있으면 덮지 않아요.
        for name, recorded in installed_files(current).items():
            if fingerprint(app / name) != recorded:
                sys.stderr.write('지금 설치가 기록과 달라서 되돌리지 않았어요 (%s). 기존 설치는 그대로예요.\n' % name)
                return 5
    drop = set(current['files']) - set(old['files'])
    if args.incoming:
        drop |= set(read_names(args.incoming)) - set(old['files'])
    touched = sorted(set(old['files']) | drop | {MANIFEST, ORIG})
    snapshot = Path(tempfile.mkdtemp(prefix='.restore-snapshot.', dir=str(root)))
    try:
        for name in touched:
            src = app / name
            if src.is_symlink() or src.is_file():
                shutil.copy2(src, snapshot / name, follow_symlinks=False)
            elif src.is_dir():
                shutil.copytree(src, snapshot / name, symlinks=True)
        try:
            app.mkdir(exist_ok=True)
            for name in old['files']:
                kind = expected[name]
                dest = app / name
                if kind == 'missing':          # 업데이트 때 내가 지운 상태였으면 지운 상태로
                    if dest.is_dir() and not dest.is_symlink():
                        shutil.rmtree(dest)
                    elif dest.exists() or dest.is_symlink():
                        dest.unlink()
                    continue
                tmp = app / ('.restore-' + name)
                shutil.rmtree(tmp, ignore_errors=True) if tmp.is_dir() and not tmp.is_symlink() else (tmp.unlink() if tmp.exists() or tmp.is_symlink() else None)
                if kind == 'link':
                    os.symlink(os.readlink(backup / name), tmp)
                elif kind == 'dir':
                    shutil.copytree(backup / name, tmp, symlinks=True)
                else:
                    shutil.copyfile(backup / name, tmp)
                    shutil.copymode(backup / name, tmp)
                if dest.is_dir() and not dest.is_symlink():
                    shutil.rmtree(dest)
                os.replace(tmp, dest)
            for name in drop:
                target = app / name
                if target.is_file() or target.is_symlink():
                    target.unlink()
            for meta in (ORIG,):
                if (backup / meta).is_dir():
                    tmp = app / ('.restore-' + meta)
                    shutil.rmtree(tmp, ignore_errors=True)
                    shutil.copytree(backup / meta, tmp, symlinks=True)
                    shutil.rmtree(app / meta, ignore_errors=True)
                    os.replace(tmp, app / meta)
            tmp = app / ('.restore-' + MANIFEST)
            if installed_files(old) == expected:
                shutil.copyfile(backup / MANIFEST, tmp)      # 기록 그대로 돌려놔요
            else:
                restored = dict(old, installed=dict(expected))   # 내가 고친 파일이 든 백업: 실제 지문을 적어요
                tmp.write_text(json.dumps(restored, ensure_ascii=False) + '\n')
            os.replace(tmp, app / MANIFEST)
        except Exception:
            # 복원이 중간에 실패하면 시작 때 찍어 둔 상태로 되돌려요.
            for name in touched:
                src, dst = snapshot / name, app / name
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst, ignore_errors=True)
                elif dst.exists() or dst.is_symlink():
                    dst.unlink()
                if src.is_dir() and not src.is_symlink():
                    shutil.copytree(src, dst, symlinks=True)
                elif src.exists() or src.is_symlink():
                    shutil.copy2(src, dst, follow_symlinks=False)
            raise
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)
    print('OFFICE_RESTORED: version=%s backup=%s' % (old.get('version', '?'), backup.name))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--restore-backup', metavar='BACKUP')
    p.add_argument('--prefix', required=True)
    p.add_argument('--incoming')
    p.add_argument('--output')
    p.add_argument('--staging')
    p.add_argument('--candidate-dir')
    p.add_argument('--resolution')
    p.add_argument('--expect-plan-id')
    p.add_argument('--during-install', action='store_true')
    args = p.parse_args(argv)
    try:
        if args.restore_backup:
            return cmd_restore(args)
        if not args.incoming:
            p.error('--incoming이 필요해요')
        if args.plan:
            if not args.output:
                p.error('--plan에는 --output이 필요해요')
            return cmd_plan(args)
        if not args.staging:
            p.error('--apply에는 --staging이 필요해요')
        return cmd_apply(args)
    except (OSError, ValueError, RuntimeError, shutil.Error) as error:
        sys.stderr.write('점검하지 못했어요: %s\n' % error)
        return 2


if __name__ == '__main__':
    sys.exit(main())
