"""실제 옛(0.9.0) Office API 처리기를 격리 폴더에서 띄워, 선택 화면이 쓰는 호출(상태·일시정지·재개·작업 요청)이 맞게 동작하는지 확인해요.
실행: python3 -I tests/test_update_bridge_real_server.py [저장소 경로]
실제 Office·로그인·브라우저는 쓰지 않아요(가짜 claude/codex 명령, 임시 HOME).
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mx = load('matrix_for_real_server', ROOT / 'tests/test_update_matrix.py')
SERVER = '''
import importlib.util, json, os, sys, threading, time
from http.server import ThreadingHTTPServer
spec = importlib.util.spec_from_file_location("office_old", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.D.mkdir(parents=True, exist_ok=True)
m.current_sid()
office = m.Office()
srv = ThreadingHTTPServer(("127.0.0.1", 0), m.make_handler(office)); srv.daemon_threads = True
office.http_server = srv
(m.D / "server.json").write_text(json.dumps({"pid": os.getpid(), "port": srv.server_address[1]}))
office.sync_teams()
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(600)
'''


class RealOldServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='office-real-old-')
        base = Path(cls.tmp.name)
        cls.home, cls.workspace = base / 'home', base / 'workspace'
        cls.home.mkdir()
        cls.workspace.mkdir()
        cmds = base / 'cmds'
        cmds.mkdir()
        for name in ('codex', 'claude'):
            (cmds / name).write_text('#!/bin/sh\nprintf "stub\\n"\n')
            (cmds / name).chmod(0o700)
        env = {k: v for k, v in os.environ.items() if not k.startswith('OFFICE_') and k != 'WSL_DISTRO_NAME'}
        env.update(HOME=str(cls.home), OFFICE_DIR=str(cls.workspace), PATH=str(cmds) + ':/usr/bin:/bin')
        old = mx.extract('0.9.0', '638d71f') / 'payload/office.py'
        subprocess.run(['git', 'init', '-q', str(cls.workspace)], check=True)
        cls.proc = subprocess.Popen([sys.executable, '-c', SERVER, str(old)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        info = cls.workspace / '.office/server.json'
        for _ in range(100):
            if info.exists() or cls.proc.poll() is not None:
                break
            time.sleep(0.1)
        if not info.exists():
            raise RuntimeError('옛 서버가 켜지지 않았어요: ' + (cls.proc.stderr.read() if cls.proc.poll() is not None else ''))
        cls.bridge = load('bridge_real', ROOT / 'payload/office_update_bridge.py')
        cls.server = cls.bridge.Server([cls.workspace / '.office'])

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()
        cls.tmp.cleanup()

    def test_server_found_and_state_shape(self):
        self.assertTrue(self.server.info)
        ids = self.server.session_ids()
        self.assertTrue(ids)
        agents = self.server.state(ids[0]).get('agents', {})
        self.assertEqual(set(agents), {'claude', 'codex'})
        self.assertIn('paused', agents['claude'])
        self.assertFalse(self.server.busy())

    def test_lead_agent_is_chosen_from_real_state(self):
        sid = self.server.session_ids()[0]
        self.assertEqual(self.server.lead(sid), ('claude', ''))
        self.server.call('/api', {'action': 'pause', 'session': sid, 'agent': 'claude', 'paused': True})
        try:
            self.assertEqual(self.server.lead(sid)[0], 'codex')
        finally:
            self.server.call('/api', {'action': 'pause', 'session': sid, 'agent': 'claude', 'paused': False})

    def test_job_request_is_visible_in_state(self):
        sid = self.server.session_ids()[0]
        text = '업데이트 사본 합치기 시험 요청'
        self.server.call('/api', {'action': 'job', 'session': sid, 'text': text, 'lead': 'claude', 'verification': 'thorough'})
        jobs = self.server.state(sid)['jobs']
        job = next((j for j in jobs if j.get('text') == text), None)
        self.assertIsNotNone(job)
        self.assertIn('status', job)

    def test_unknown_action_is_reported_as_error(self):
        with self.assertRaises(RuntimeError):
            self.server.call('/api', {'action': 'no-such-action'})


if __name__ == '__main__':
    unittest.main()
