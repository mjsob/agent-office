#!/usr/bin/env python3
"""명시한 정적 입력이 같을 때 성공한 검증을 재사용해요.

실서버·네트워크·현재 시각에 의존하는 검증에는 사용하지 마세요.
검증 코드, 입력 자료, 설정과 잠금 파일을 모두 --input으로 지정하세요.
"""
import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time


def digest_path(path, seen=()):
    """경로의 내용과 권한을 확인하고, 순환 링크나 바뀌는 입력은 거절해요."""
    path = Path(path)
    meta = path.lstat()
    h = hashlib.sha256(str(stat.S_IMODE(meta.st_mode)).encode())
    if path.is_symlink():
        target = path.resolve(strict=True)
        if target in seen:
            raise ValueError(f'순환 링크를 확인하세요: {path}')
        h.update(b'link\0' + os.readlink(path).encode())
        h.update(digest_path(target, seen).encode())
    elif path.is_dir():
        real = path.resolve()
        if real in seen:
            raise ValueError(f'순환 링크를 확인하세요: {path}')
        for child in sorted(path.iterdir(), key=lambda x: x.name):
            h.update(child.name.encode() + b'\0' + digest_path(child, (*seen, real)).encode())
    elif path.is_file():
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(chunk)
    else:
        raise ValueError(f'일반 파일이나 폴더를 지정하세요: {path}')
    after = path.lstat()
    if (meta.st_mtime_ns, meta.st_ctime_ns, meta.st_size, meta.st_ino) != (after.st_mtime_ns, after.st_ctime_ns, after.st_size, after.st_ino):
        raise ValueError(f'확인 중 입력이 바뀌었어요: {path}')
    return h.hexdigest()


def command_files(command, outputs=()):
    paths = set()
    for arg in command[1:]:
        candidate = arg.split('=', 1)[1] if arg.startswith('-') and '=' in arg else arg
        if not candidate:
            continue
        path = Path(candidate).absolute()
        try:
            if path.exists() or path.is_symlink():
                if not any(path.resolve() == p.resolve() or path.resolve().is_relative_to(p.resolve()) for p in outputs):
                    paths.add(path)
        except OSError:
            continue
    return sorted(paths)


def fingerprint(inputs, command, outputs=()):
    executable = shutil.which(command[0])
    if not executable:
        raise ValueError(f'명령을 찾을 수 없어요: {command[0]}')
    packages = sorted([d.metadata.get('Name', ''), d.version] for d in importlib.metadata.distributions())
    # 환경 값은 원문을 저장하지 않고 해시만 남겨요.
    env = hashlib.sha256(json.dumps(dict(os.environ), sort_keys=True).encode()).hexdigest()
    return {
        'schema': 1, 'tool': digest_path(Path(__file__).resolve()),
        'cwd': str(Path.cwd()), 'command': command, 'environment': env,
        'runtime': [sys.version, platform.platform(), str(Path(sys.executable).resolve()), packages],
        'executable': [str(Path(executable).resolve()), digest_path(Path(executable).resolve())],
        'inputs': {str(p): digest_path(p) for p in inputs},
        'command_files': {str(p): digest_path(p) for p in command_files(command, outputs)},
    }


def artifacts(paths):
    return {str(p): digest_path(p) for p in paths}


def write_json(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False, encoding='utf-8') as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--static', action='store_true', required=True, help='외부 상태와 무관한 검증일 때만 선택하세요.')
    parser.add_argument('--name', required=True)
    parser.add_argument('--input', action='append', required=True, dest='inputs')
    parser.add_argument('--artifact', action='append', default=[])
    parser.add_argument('--cache-dir', type=Path, default=Path('.office/verification-cache'))
    parser.add_argument('--max-age', type=float, default=3600)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or not math.isfinite(args.max_age) or args.max_age <= 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('명령과 0보다 큰 유효기간·제한시간을 지정하세요.')
    inputs = sorted(set(Path(x).absolute() for x in args.inputs))
    outputs = sorted(set(Path(x).absolute() for x in args.artifact))
    cache = args.cache_dir.resolve()
    if any(cache == p.resolve() or cache.is_relative_to(p.resolve()) for p in inputs):
        parser.error('검증 기록 폴더를 입력 안에 둘 수 없어요. 구체적인 입력 경로를 지정하세요.')
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(args.name.encode()).hexdigest()[:24]
    record_path = cache / (key + '.json')
    log_path = cache / (key + '.log')
    with (cache / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = time.time()
        before = fingerprint(inputs, command, outputs)
        try:
            previous = json.loads(record_path.read_text())
            age = now - previous['finished_at']
            valid = (not args.force and previous['fingerprint'] == before
                     and 0 <= age <= args.max_age and previous['artifacts'] == artifacts(outputs)
                     and previous['log_sha256'] == digest_path(log_path)
                     and previous['status'] == 'passed')
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            valid = False
        if valid:
            print(json.dumps({'status': 'reused', 'record': str(record_path), 'avoided_command_seconds': previous['duration_seconds']}, ensure_ascii=False))
            return 0
        # 재실행이 실패하면 과거 성공 결과도 재사용하지 않아요.
        record_path.unlink(missing_ok=True)
        started = time.monotonic()
        try:
            with log_path.open('wb') as log:
                result = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    result.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(result.pid, signal.SIGKILL)
                    result.wait()
                    raise
        except subprocess.TimeoutExpired:
            print(f'검증 제한시간이 지났어요. 로그를 확인하세요: {log_path}', file=sys.stderr)
            return 124
        duration = round(time.monotonic() - started, 6)
        if result.returncode:
            print(f'검증에 실패했어요. 로그를 확인하세요: {log_path}', file=sys.stderr)
            return result.returncode if result.returncode > 0 else 128 - result.returncode
        if fingerprint(inputs, command, outputs) != before:
            raise ValueError('실행 중 입력이나 환경이 바뀌어서 성공 결과를 저장하지 않았어요.')
        record = {'status': 'passed', 'name': args.name, 'finished_at': time.time(),
                  'duration_seconds': duration, 'fingerprint': before,
                  'artifacts': artifacts(outputs), 'log_sha256': digest_path(log_path)}
        write_json(record_path, record)
        print(json.dumps({'status': 'passed', 'record': str(record_path), 'duration_seconds': duration}, ensure_ascii=False))
        return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RecursionError) as error:
        print(f'검증 기록을 만들지 못했어요: {error}', file=sys.stderr)
        sys.exit(2)
