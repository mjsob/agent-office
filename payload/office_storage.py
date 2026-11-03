"""설치에 포함된 디스크 측정·Windows 도우미 연결이에요. 불러오기만 해서는 실행하지 않아요."""
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "office/storage"
COMPACT_STATE = str(Path.home() / ".local/state/wsl-compact")
COMPACT_FLOOR = str(STATE / "compact-floor.json")
COMPACT_NOTICE = str(STATE / "compact-notice.json")
COMPACT_CMD = "Office 설정에서 디스크 줄이기"
COMPACT_RECLAIM_GB = 20
COMPACT_LOWFREE_GB = 30
COMPACT_URGENT_GB = 15
COMPACT_SPARSE_GAP = 2**30


def helper_path():
    record = read_json(HERE.parent / "windows-helper.json")
    if not record or not isinstance(record.get("path"), str):
        return None
    path = Path(record["path"])
    return path if path.is_absolute() and path.is_file() else None


def helper_call(action, *arguments, timeout=30):
    path = helper_path()
    if path is None:
        raise RuntimeError("Windows 도우미를 찾지 못했어요. 설치 결과를 확인하세요.")
    result = subprocess.run([str(path), action, *map(str, arguments)], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(" ".join((result.stderr or result.stdout or "Windows 도우미를 실행하지 못했어요.").split())[:400])
    return result


def windows_path(path):
    return subprocess.run(["wslpath", "-w", str(path)], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, check=True, timeout=5).stdout.strip()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    """못 읽거나 객체가 아니면 None 이에요."""
    try:
        with open(path, encoding='utf-8-sig') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None

def is_number(value):
    return type(value) in (int, float) and math.isfinite(value)

def ext4_metadata_gib(st, root='/'):
    """장치 크기와 statvfs 전체의 차이예요. inode 표·저널처럼 statvfs 가 빼는 ext4 관리 공간이에요."""
    try:
        dev = os.stat(root).st_dev
        with open(f'/sys/dev/block/{os.major(dev)}:{os.minor(dev)}/size', encoding='ascii') as fh:
            device_bytes = int(fh.read().strip()) * 512
    except (OSError, ValueError):
        return None
    metadata = device_bytes - st.f_blocks * st.f_frsize
    return metadata / 2**30 if device_bytes > 0 and metadata >= 0 else None

def learn_compact_floor(d):
    """정상으로 끝난 새 줄이기가 있으면 고정분을 갱신해요. 시험·실패·fstrim 실패·sparse 는 쓰지 않아요."""
    if abs(d.get('logical_bytes', 0) - d['allocated_bytes']) > COMPACT_SPARSE_GAP:
        return   # 결과의 after_gb 는 파일 크기예요. sparse 면 실점유와 달라요.
    status_link = os.path.join(COMPACT_STATE, 'status.json')
    result = read_json(os.path.join(os.path.dirname(os.path.realpath(status_link)), 'result.json'))
    pre = read_json(os.path.join(COMPACT_STATE, 'pre-compact.json'))
    if not result or not pre or not result.get('request_id') or result['request_id'] != pre.get('request_id'):
        return
    floor = read_json(COMPACT_FLOOR) or {}
    if result['request_id'] == floor.get('request_id'):
        return
    after, used, finished = result.get('after_gb'), pre.get('used_gb'), result.get('updated_at')
    recorded = pre.get('recorded_at')
    if (result.get('status') != 'completed' or result.get('test_mode') is not False
            or result.get('recovery_required') is not False or pre.get('trim_ok') is not True
            or pre.get('distro') != d.get('distro')
            or not d.get('path') or pre.get('path') != d['path']
            or not all(is_number(v) for v in (after, used, finished, recorded))
            or used <= 0 or after < used or not recorded < finished <= time.time() + 300
            or (is_number(floor.get('compacted_at')) and finished <= floor['compacted_at'])):
        return
    atomic_json(COMPACT_FLOOR, {'schema_version': 1, 'floor_gib': after - used, 'request_id': result['request_id'],
                   'compacted_at': finished, 'distro': d.get('distro'), 'path': d.get('path')})

def compact_floor(d, metadata_gib):
    """고정분과 근거를 돌려줘요. 지난 줄이기 기록이 없으면 ext4 관리 공간 전체를 보수적으로 빼요."""
    try:
        learn_compact_floor(d)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'고정분 갱신 실패: {type(error).__name__}', file=sys.stderr)
    saved = read_json(COMPACT_FLOOR) or read_json(Path.home() / 'resource-watch/compact-floor.json')
    # 배포판을 다시 깔면 디스크 파일이 달라져요. 같은 파일의 기록만 믿어요.
    if saved and saved.get('schema_version') == 1 and saved.get('distro') == d.get('distro') \
            and saved.get('path') == d.get('path') \
            and is_number(saved.get('floor_gib')) and saved['floor_gib'] >= 0:
        return saved['floor_gib'], 'last_compact'
    if metadata_gib is not None:
        return metadata_gib, 'ext4_metadata'
    return None, None

def compact_estimate(d):
    """회수 후보(GiB)예요. 실점유에서 사용량과 고정분을 빼요. 고정분을 모르면 None 이에요."""
    if d.get('floor_gib') is None:
        return None
    return max(0.0, d['vhdx_gb'] - d['used_gb'] - d['floor_gib'])

def compact_reclaim(cur):
    """제안 조건일 때만 회수 후보(GiB)를 돌려줘요."""
    d = cur.get('disk')
    if not d:
        return None
    reclaim = compact_estimate(d)
    if reclaim is None:
        return None
    if reclaim < COMPACT_RECLAIM_GB and not (d['c_free_gb'] < COMPACT_LOWFREE_GB and reclaim >= 10):
        return None
    return reclaim

def compact_notice(cur):
    """Office 창 위쪽 표시용 상태예요. 점검할 때마다 최신 값으로 덮어써요.
    측정에 실패하면 지난 수치를 쓰지 않고 unavailable 로 남겨요. Office 는 이 값을 다시 계산하지 않아요."""
    notice = {'schema_version': 2, 'checked_at': cur['t'], 'status': 'unavailable',
              'disk': None, 'reclaim_gib': None, 'reclaim_basis': None, 'floor_gib': None,
              'urgent': False, 'command': COMPACT_CMD}
    d = cur.get('disk')
    if not d:
        return notice
    notice['disk'] = {'vhdx_gb': d['vhdx_gb'], 'used_gb': d['used_gb'],
                      'c_free_gb': d['c_free_gb'], 'drive': d.get('drive', 'C:'),
                      'distro': d.get('distro')}
    estimate = compact_estimate(d)
    if estimate is not None:
        notice['reclaim_gib'] = round(estimate, 1)
        notice['reclaim_basis'] = d['reclaim_basis']
        notice['floor_gib'] = round(d['floor_gib'], 2)
    if compact_reclaim(cur) is not None:
        notice['status'] = 'suggested'
        notice['urgent'] = d['c_free_gb'] < COMPACT_URGENT_GB
    else:
        notice['status'] = 'ok'
    return notice


def measure_windows(distro):
    try:
        return helper_call('measure', distro)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        wrapper = HERE / 'office_storage_run.sh'
        if not wrapper.is_file():
            raise RuntimeError('디스크 측정 도구를 찾지 못했어요.') from None
        result = subprocess.run(['bash', str(wrapper), '--dry-run'], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(' '.join((result.stderr or '디스크를 측정하지 못했어요.').split())[:400])
        return result


def disk_snapshot():
    """현재 배포판의 실제 점유와 Linux 사용량을 비교해요. 축소는 실행하지 않아요."""
    distro = os.environ.get('WSL_DISTRO_NAME', '')
    if not distro or sys.platform == 'darwin':
        return None
    try:
        d = json.loads(measure_windows(distro).stdout.lstrip('\ufeff'))
        if not isinstance(d, dict) or d.get('distro') != distro or not isinstance(d.get('path'), str) or not d['path']:
            raise ValueError('대상 배포판이 달라요.')
        for key in ('logical_bytes', 'allocated_bytes', 'free_bytes'):
            if not is_number(d.get(key)) or d[key] < 0:
                raise ValueError('측정값을 확인하지 못했어요.')
        if d['allocated_bytes'] <= 0 or not re.fullmatch(r'[A-Za-z]:', str(d.get('drive', ''))):
            raise ValueError('실제 점유를 확인하지 못했어요.')
        st = os.statvfs('/')
        d['used_gb'] = (st.f_blocks - st.f_bfree) * st.f_frsize / 2**30
        d['vhdx_gb'] = d['allocated_bytes'] / 2**30
        d['c_free_gb'] = d['free_bytes'] / 2**30
        d['floor_gib'], d['reclaim_basis'] = compact_floor(d, ext4_metadata_gib(st))
        return d
    except (RuntimeError, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print('가상디스크 측정 실패: ' + str(error)[:200], file=sys.stderr)
        return None


def measure_notice():
    STATE.mkdir(parents=True, exist_ok=True)
    notice = compact_notice({'t': time.time(), 'disk': disk_snapshot()})
    atomic_json(COMPACT_NOTICE, notice)
    return notice


def main():
    if len(sys.argv) == 2 and sys.argv[1] == '--disk-only':
        notice = measure_notice()
        print(json.dumps(notice, ensure_ascii=False))
        return 0 if notice['status'] != 'unavailable' else 2
    raise SystemExit('지원하지 않는 디스크 명령이에요.')


if __name__ == '__main__':
    raise SystemExit(main())
