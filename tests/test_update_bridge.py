"""옛 버전용 일회성 선택 화면(office_update_bridge.py)과 설치기 진입을 확인해요.
실행: python3 -I tests/test_update_bridge.py [저장소 경로]
가짜 옛 서버·가짜 브라우저를 쓰고 실제 설치 폴더는 건드리지 않아요. 브라우저는 열지 않아요.
"""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mx = load('matrix_for_bridge', ROOT / 'tests/test_update_matrix.py')


class FakeOffice:
    """옛 Office 서버처럼 /api/state 와 POST /api(job)에 답해요."""

    def __init__(self):
        self.busy = False
        self.jobs = []
        self.posts = []
        self.paused = {'claude': False, 'codex': False}
        self.pause_error = False
        self.error_kind = {}
        owner = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def trusted(self):
                return self.headers.get('Host') == '127.0.0.1:%d' % owner.port

            def send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if not self.trusted():
                    return self.send(403, {'error': 'host'})
                if self.path.startswith('/api/state'):
                    agents = {n: {'status': 'working' if owner.busy and n == 'claude' else 'idle', 'paused': p, 'error_kind': owner.error_kind.get(n)} for n, p in owner.paused.items()}
                    return self.send(200, {'session': 's1', 'sessions': [{'id': 's1', 'busy': owner.busy}], 'jobs': owner.jobs, 'agents': agents})
                self.send(404, {})

            def do_POST(self):
                if not self.trusted():
                    return self.send(403, {'error': 'host'})
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.posts.append(payload)
                if payload.get('action') == 'pause':
                    if owner.pause_error:
                        return self.send(400, {'error': '멈출 수 없어요'})
                    owner.paused[payload['agent']] = payload['paused']
                    return self.send(200, {})
                if payload.get('action') == 'job':
                    owner.jobs.append({'id': len(owner.jobs) + 1, 'text': payload['text'], 'status': 'planning'})
                    return self.send(200, {})
                self.send(400, {'error': '알 수 없는 요청'})

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()


class Bridge(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='office-bridge-')
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.target = mx.build_pkg(self.dir / 'target', '0.10.0')
        (self.dir / 'case').mkdir()
        self.c = mx.Ctx(self.dir / 'case', '0.9.0', '638d71f', self.target)
        self.workspace = self.c.home / 'agent-office'
        self.procs = []
        self.addCleanup(self.cleanup_procs)
        self.fake = None

    def cleanup_procs(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
        if self.fake:
            self.fake.stop()

    def with_server(self):
        self.fake = FakeOffice()
        (self.workspace / '.office').mkdir(parents=True, exist_ok=True)
        # 가짜 서버 프로세스: 적용 중에 얼렸다 풀어도 안전한 빈 프로세스(명령줄에 office.py 가 있어요)
        self.dummy = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)', 'office.py'])
        self.procs.append(self.dummy)
        (self.workspace / '.office/server.json').write_text(json.dumps({'pid': self.dummy.pid, 'port': self.fake.port}))

    # --- 실행 도우미
    def start_parent(self, extra_env=None, args=()):
        announce = self.dir / 'announce.txt'
        announce.unlink(missing_ok=True)
        env = dict(self.c.env, OFFICE_BRIDGE_ANNOUNCE=str(announce), OFFICE_BRIDGE_CALM_POLLS='2', **(extra_env or {}))
        result = self.dir / 'result.txt'
        result.unlink(missing_ok=True)
        code = ('import subprocess,sys; a=sys.argv; r=subprocess.run(["bash",a[1],"--apply","--skip-login","--no-launch","--no-shortcut",'
                '"--prefix",a[2],"--workspace",a[3],*a[6:]],capture_output=True,text=True); '
                'open(a[4],"w").write(str(r.returncode)+"\\n"+r.stdout+"\\n--ERR--\\n"+r.stderr)')
        proc = subprocess.Popen([sys.executable, '-c', code, str(self.target / 'install.sh'), str(self.c.prefix), str(self.workspace), str(result),
                                 str(self.c.prefix / 'app/office.py'), *args], env=env)
        self.procs.append(proc)
        return proc, announce, result

    def start_review(self, extra_env=None):
        """직접 `--review`로 연 모습(부모가 옛 앱이 아니에요). 끝나면 proc.communicate()로 결과를 받아요."""
        announce = self.dir / 'announce.txt'
        announce.unlink(missing_ok=True)
        env = dict(self.c.env, OFFICE_BRIDGE_ANNOUNCE=str(announce), **(extra_env or {}))
        proc = subprocess.Popen(['bash', str(self.target / 'install.sh'), '--apply', '--review', '--skip-login', '--no-launch', '--no-shortcut'],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(proc)
        return proc, announce

    def wait_for(self, path, seconds=30):
        end = time.time() + seconds
        while time.time() < end:
            if path.exists() and path.read_text():
                return path.read_text()
            time.sleep(0.1)
        self.fail('기다린 파일이 생기지 않았어요: %s' % path)

    def url(self, announce):
        text = self.wait_for(announce)
        base = text.strip()
        self.host, rest = base[len('http://'):].split('/', 1)
        self.token = rest.strip('/')
        return base

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection(self.host.split(':')[0], int(self.host.split(':')[1]), timeout=15)
        h = {'Host': host or self.host}
        if body is not None:
            h['Content-Type'] = 'application/json'
        h.update(headers or {})
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data.decode(errors='replace')

    def act(self, action, **extra):
        return self.request('POST', '/%s/act' % self.token, dict(action=action, **extra), {'Origin': 'http://' + self.host})

    def state(self):
        return self.request('GET', '/%s/state' % self.token)[1]

    def finished(self, result):
        text = self.wait_for(result, 60)
        code, rest = text.split('\n', 1)
        out, err = rest.split('\n--ERR--\n')
        return int(code), out, err

    # --- 시험
    def test_auto_opens_for_old_app_and_keep_changes_nothing(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        before = self.c.snap()
        proc, announce, result = self.start_parent()
        self.url(announce)
        status, page = self.request('GET', '/%s/' % self.token)
        self.assertEqual(status, 200)
        self.assertIn('내 수정과 새 버전이 겹쳐요', page)
        state = self.state()
        self.assertEqual(state['conflicts'], ['office.py'])
        self.assertFalse(state['can_upstream'])       # 옛 앱이 불렀는데 서버를 못 찾으면 적용하지 않아요
        self.assertFalse(state['can_merge'])          # 서버 없음 → 합치기 끔
        self.assertIn('Office', state['merge_note'])
        self.assertEqual(self.act('keep')[0], 200)
        code, out, err = self.finished(result)
        self.assertEqual(code, 3)
        self.assertIn('보류', err)
        self.assertIn('설치를 멈췄어요', err)      # 옛 앱이 이유를 그대로 보여 줘요
        self.assertEqual(self.app_state(before), self.app_state(self.c.snap()))

    def app_state(self, snap):
        return {k: v for k, v in snap.items() if k.startswith('.local/share/office-app/app') and '__pycache__' not in k}

    def test_not_opened_without_old_app_parent(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        announce = self.dir / 'announce.txt'
        r = subprocess.run(['bash', str(self.target / 'install.sh'), '--apply', '--skip-login', '--no-launch', '--no-shortcut'],
                           env=dict(self.c.env, OFFICE_BRIDGE_ANNOUNCE=str(announce)), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 3)
        self.assertFalse(announce.exists())

    def test_explicit_review_opens_without_parent(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        announce = self.dir / 'announce.txt'
        proc = subprocess.Popen(['bash', str(self.target / 'install.sh'), '--apply', '--review', '--skip-login', '--no-launch', '--no-shortcut'],
                                env=dict(self.c.env, OFFICE_BRIDGE_ANNOUNCE=str(announce)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(proc)
        self.url(announce)
        self.act('keep')
        proc.wait(timeout=30)
        self.assertEqual(proc.returncode, 3)

    def test_review_without_conflict_installs_normally(self):
        self.c.install_old()
        announce = self.dir / 'announce.txt'
        r = subprocess.run(['bash', str(self.target / 'install.sh'), '--apply', '--review', '--skip-login', '--no-launch', '--no-shortcut'],
                           env=dict(self.c.env, OFFICE_BRIDGE_ANNOUNCE=str(announce)), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(announce.exists())

    def test_local_edition_never_opens_bridge(self):
        self.c.install_old()
        (self.c.app / 'VERSION').write_text('0.9.0+local\n')
        announce = self.dir / 'announce.txt'
        proc, announce, result = self.start_parent()
        code, out, err = self.finished(result)
        self.assertEqual(code, 3)
        self.assertFalse(announce.exists())
        self.assertIn('전용', err)

    def test_security_checks(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        proc, announce, result = self.start_parent()
        self.url(announce)
        origin = {'Origin': 'http://' + self.host}
        self.assertEqual(self.request('GET', '/%s/state' % self.token, host='evil.example')[0], 403)
        self.assertEqual(self.request('GET', '/%s/state' % self.token, headers={'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/wrong-token/')[0], 404)
        self.assertEqual(self.request('POST', '/%s/act' % self.token, {'action': 'keep'})[0], 403)                 # Origin 없음
        self.assertEqual(self.request('POST', '/%s/act' % self.token, {'action': 'keep'}, {'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.request('POST', '/%s/act' % self.token, {'action': 'rm -rf'}, origin)[0], 400)
        self.assertEqual(self.request('POST', '/%s/act' % self.token, {'action': 'upstream', 'path': '/etc', 'choice': 'x'}, origin)[0], 400)   # 확인 없이는 안 돼요
        self.assertEqual(self.act('upstream')[0], 400)
        self.assertEqual(self.act('upstream', confirmed='yes')[0], 400)
        self.assertEqual(self.act('merged')[0], 400)       # 합친 사본 없이는 안 돼요
        self.act('keep')
        self.finished(result)

    def test_upstream_after_confirmation_installs_when_office_is_closed(self):
        self.c.install_old()
        mx.conflict_state(self.c, True)
        custom = self.c.app_bytes('office.py')
        proc, announce = self.start_review()
        self.url(announce)
        self.act('ping')
        self.assertEqual(self.act('upstream', confirmed=True)[0], 200)
        out, err = proc.communicate(timeout=90)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertIn('OFFICE_LOCAL', out)
        self.assertEqual(self.c.app_bytes('office.py'), self.c.tpay('office.py'))
        self.assertIn(b'keep-me-html', self.c.app_bytes('office.html'))
        mx.check_fully_updated(self.c)
        self.assertTrue(any((b / 'office.py').read_bytes() == custom for b in (self.c.prefix / 'backups').iterdir()))
    def test_duplicate_submit_is_rejected(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        proc, announce = self.start_review()
        self.url(announce)
        results = []
        def go():
            results.append(self.act('upstream', confirmed=True)[0])
        threads = [threading.Thread(target=go) for _ in range(3)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(results.count(200), 1, results)
        proc.communicate(timeout=90)
        self.assertEqual(proc.returncode, 0)
    def test_merge_flow_with_fake_server_and_apply_after_office_closes(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        self.with_server()
        proc, announce, result = self.start_parent()
        self.url(announce)
        self.assertTrue(self.state()['can_merge'])
        self.assertEqual(self.act('merge')[0], 200)
        self.assertEqual(len(self.fake.jobs), 1)
        self.assertIn('후보 사본', self.fake.jobs[0]['text'])
        self.assertEqual(self.fake.posts[0]['session'], 's1')
        status, body = self.act('verify')                    # 아직 안 끝남
        self.assertEqual(status, 400)
        self.assertIn('끝나지', body['error'])
        review = next(p for p in (self.workspace / '.office/update-review').iterdir() if p.is_dir())
        final = self.c.tpay('office.py') + b'\n# agent merged custom\n'
        (review / 'candidate/files/office.py').write_bytes(final)
        self.fake.jobs[0]['status'] = 'done'
        self.assertEqual(self.act('verify')[0], 200)
        self.assertTrue(self.state()['can_apply_merged'])
        before = self.app_state(self.c.snap())
        self.assertEqual(self.act('merged')[0], 200)          # Office가 켜져 있으니 파일은 바꾸지 않고 선택만 저장해요
        code, out, err = self.finished(result)
        self.assertEqual(code, 3)
        self.assertIn('install.sh --apply --review', err)
        self.assertEqual(before, self.app_state(self.c.snap()))
        # Office를 닫은 뒤 같은 선택을 이어서 적용해요(합치기 사본 보존)
        self.dummy.kill(); self.dummy.wait()
        (self.workspace / '.office/server.json').unlink()
        proc2, announce2 = self.start_review()
        self.url(announce2)
        self.assertIn('저장해 뒀어요', self.state()['message'])
        self.assertTrue(self.state()['can_apply_merged'])
        self.assertEqual(self.act('merged')[0], 200)
        out, err = proc2.communicate(timeout=90)
        self.assertEqual(proc2.returncode, 0, out + err)
        self.assertEqual(self.c.app_bytes('office.py'), final)
        self.assertEqual(self.c.app_bytes('.office-orig/office.py'), self.c.tpay('office.py'))
    def test_merge_without_server_is_explained(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        proc, announce, result = self.start_parent()
        self.url(announce)
        status, body = self.act('merge')
        self.assertEqual(status, 400)
        self.assertIn('Office', body['error'])
        self.act('keep')
        self.finished(result)

    def test_timeout_closes_window_and_first_ping_missing_leave_install_alone(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        before = self.app_state(self.c.snap())
        for env, key in (({'OFFICE_BRIDGE_FIRST_PING': '2'}, '열리지'), ):
            proc, announce, result = self.start_parent(env)
            code, out, err = self.finished(result)
            self.assertEqual(code, 3)
            self.assertIn(key, err)
        self.assertEqual(before, self.app_state(self.c.snap()))

    def test_closed_window_after_ping_leaves_install_alone(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        before = self.app_state(self.c.snap())
        proc, announce, result = self.start_parent({'OFFICE_BRIDGE_IDLE': '2'})
        self.url(announce)
        self.act('ping')
        code, out, err = self.finished(result)
        self.assertEqual(code, 3)
        self.assertIn('닫혀', err)
        self.assertEqual(before, self.app_state(self.c.snap()))

    def test_selection_timeout_leaves_install_alone(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        before = self.app_state(self.c.snap())
        announce = self.dir / 'announce.txt'
        proc = subprocess.run([sys.executable, '-I', str(self.target / 'payload/office_update_bridge.py'), '--prefix', str(self.c.prefix),
                               '--package', str(self.target), '--workspace', str(self.workspace), '--timeout', '2'],
                              env=dict(self.c.env, OFFICE_BRIDGE_ANNOUNCE=str(announce)), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 3)
        self.assertIn('시간이 지나', proc.stderr)
        self.assertEqual(before, self.app_state(self.c.snap()))

    def test_browser_cannot_open_is_explained(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        env = dict(self.c.env, PATH=str(self.c.cmds), BROWSER='false')
        env.pop('OFFICE_BRIDGE_ANNOUNCE', None)
        proc = subprocess.run([sys.executable, '-I', str(self.target / 'payload/office_update_bridge.py'), '--prefix', str(self.c.prefix),
                               '--package', str(self.target), '--workspace', str(self.workspace), '--timeout', '5'],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 3)
        self.assertIn('브라우저를 열지 못했어요', proc.stderr)

    def test_old_app_parent_without_server_never_installs(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        before = self.app_state(self.c.snap())
        proc, announce, result = self.start_parent()
        self.url(announce)
        self.assertFalse(self.state()['can_upstream'])
        status, body = self.act('upstream', confirmed=True)
        self.assertEqual(status, 400)
        self.assertIn('서버', body['error'])
        self.assertEqual(before, self.app_state(self.c.snap()))
        self.act('keep')
        self.finished(result)

    def test_explicit_review_without_server_can_install_when_office_is_closed(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        announce = self.dir / 'announce.txt'
        proc = subprocess.Popen(['bash', str(self.target / 'install.sh'), '--apply', '--review', '--skip-login', '--no-launch', '--no-shortcut'],
                                env=dict(self.c.env, OFFICE_BRIDGE_ANNOUNCE=str(announce)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(proc)
        self.url(announce)
        self.assertEqual(self.act('upstream', confirmed=True)[0], 200)
        proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.c.app_bytes('office.py'), self.c.tpay('office.py'))

    def test_stale_server_json_does_not_hide_the_live_server_data_dir(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        self.with_server()
        stale = self.dir / 'stale-office'
        (stale / '.office').mkdir(parents=True)
        (stale / '.office/server.json').write_text(json.dumps({'pid': 2 ** 22 + 7, 'port': 1}))
        proc, announce, result = self.start_parent({'OFFICE_DIR': str(stale)})
        self.url(announce)
        self.assertTrue(self.state()['can_merge'])
        self.assertTrue((self.workspace / '.office/update-review').is_dir())
        self.assertFalse((stale / '.office/update-review').exists())
        self.act('keep')
        self.finished(result)

    def test_merge_can_be_retried_after_the_job_is_cancelled(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        self.with_server()
        proc, announce, result = self.start_parent()
        self.url(announce)
        self.assertEqual(self.act('merge')[0], 200)
        status, body = self.act('merge')                      # 아직 합치는 중
        self.assertEqual(status, 400)
        self.assertIn('합치는 중', body['error'])
        self.fake.jobs[0]['cancelled'] = True
        self.assertTrue(self.state()['can_merge'])
        self.assertEqual(self.act('merge')[0], 200)
        self.assertEqual(len(self.fake.jobs), 2)
        review = next(p for p in (self.workspace / '.office/update-review').iterdir() if p.is_dir())
        self.assertTrue(any(q.name.startswith('previous-candidate-') for q in review.iterdir()))
        self.act('keep')
        self.finished(result)

    def test_merge_picks_a_logged_in_agent(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        self.with_server()
        self.fake.error_kind = {'claude': 'auth'}
        proc, announce, result = self.start_parent()
        self.url(announce)
        self.assertTrue(self.state()['can_merge'])
        self.assertEqual(self.act('merge')[0], 200)
        self.assertEqual(self.fake.posts[0]['lead'], 'codex')
        self.act('keep')
        self.finished(result)

    def test_merge_is_off_when_no_agent_can_work(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        self.with_server()
        self.fake.error_kind = {'claude': 'auth', 'codex': 'auth'}
        proc, announce, result = self.start_parent()
        self.url(announce)
        state = self.state()
        self.assertFalse(state['can_merge'])
        self.assertIn('로그아웃', state['merge_note'])
        status, body = self.act('merge')
        self.assertEqual(status, 400)
        self.assertEqual(self.fake.posts, [])
        review = [p for p in (self.workspace / '.office/update-review').iterdir() if p.is_dir()][0]
        self.assertFalse((review / 'candidate').exists())          # 사본도 만들지 않았어요
        self.fake.error_kind = {'claude': 'auth'}
        self.fake.paused['codex'] = True
        self.assertFalse(self.state()['can_merge'])
        self.assertIn('재개', self.state()['merge_note'])
        self.act('keep')
        self.finished(result)

    def test_live_office_never_gets_files_replaced(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        self.with_server()
        before = self.app_state(self.c.snap())
        proc, announce, result = self.start_parent()
        self.url(announce)
        self.assertEqual(self.act('upstream', confirmed=True)[0], 200)
        code, out, err = self.finished(result)
        self.assertEqual(code, 3)
        self.assertIn('Office를 닫은 뒤', err)
        self.assertEqual(before, self.app_state(self.c.snap()))
        self.assertEqual(self.fake.posts, [])                 # 에이전트를 멈추거나 건드리지도 않아요

    def test_running_office_without_server_record_also_blocks_apply(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        before = self.app_state(self.c.snap())
        ghost = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)', str(self.c.app / 'office.py')])
        self.procs.append(ghost)
        proc, announce = self.start_review()
        self.url(announce)
        self.assertEqual(self.act('upstream', confirmed=True)[0], 200)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 3, out + err)
        self.assertIn('Office를 닫은 뒤', err)
        self.assertEqual(before, self.app_state(self.c.snap()))

    def test_ui_wording_has_no_polite_please_forms(self):
        source = (ROOT / 'payload/office_update_bridge.py').read_text()
        self.assertNotIn('주세요', source)
        self.assertNotIn('해 줘', source)

    def test_page_is_offline_and_keeps_confirmation_before_render(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        proc, announce, result = self.start_parent()
        self.url(announce)
        status, page = self.request('GET', '/%s/' % self.token)
        self.assertNotIn('http://', page.replace('http://127.0.0.1', ''))
        self.assertNotIn('https://', page)
        self.assertIn('confirmed=this.checked', page)                       # 체크 상태를 따로 기억해요
        self.assertLess(page.index('const sure=confirmed===true'), page.index('busy=true; render();'))   # 다시 그리기 전에 읽어요
        self.act('keep')
        self.finished(result)

    def test_token_is_not_logged_to_stdout_or_stderr(self):
        self.c.install_old()
        mx.conflict_state(self.c)
        proc, announce, result = self.start_parent()
        self.url(announce)
        self.act('keep')
        code, out, err = self.finished(result)
        self.assertNotIn(self.token, out + err)


if __name__ == '__main__':
    unittest.main()
