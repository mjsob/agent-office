"""개인 도구 없이 측정하고 정확한 상태만 전달하는지 확인해요."""
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('test_'+name, ROOT/'payload'/name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DiskBundle(unittest.TestCase):
    def setUp(self):
        self.s = load('office_storage.py')
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.s.STATE = Path(self.tmp.name)
        self.s.COMPACT_NOTICE = str(self.s.STATE/'notice.json')
        self.s.COMPACT_FLOOR = str(self.s.STATE/'floor.json')
        self.s.COMPACT_STATE = self.tmp.name
        p=patch.dict(os.environ, {'WSL_DISTRO_NAME':'test'});p.start();self.addCleanup(p.stop)
    def data(self, **kw):
        return dict(distro='test',path='C:\\test\\ext4.vhdx',logical_bytes=120*2**30,allocated_bytes=120*2**30,free_bytes=60*2**30,drive='C:',**kw)
    def run_snapshot(self, value):
        st=types.SimpleNamespace(f_blocks=100,f_bfree=20,f_frsize=2**30)
        with patch.object(self.s,'helper_call',return_value=types.SimpleNamespace(stdout=json.dumps(value))),patch.object(self.s.os,'statvfs',return_value=st),patch.object(self.s,'compact_floor',return_value=(10,'ext4_metadata')),patch.object(self.s,'ext4_metadata_gib',return_value=10):
            return self.s.disk_snapshot()
    def test_measurement_combines_host_and_guest(self):
        d=self.run_snapshot(self.data())
        self.assertEqual((d['vhdx_gb'],d['used_gb'],d['c_free_gb']),(120,80,60))
        self.assertEqual(self.s.compact_estimate(d),30)
    def test_wrong_distro_and_malformed_values_fail_closed(self):
        for key,value in [('distro','other'),('allocated_bytes',float('nan')),('allocated_bytes',True),('allocated_bytes',0),('free_bytes',-1),('path',None),('drive','unexpected')]:
            d=self.data();d[key]=value
            with self.subTest(key=key,value=value): self.assertIsNone(self.run_snapshot(d))
    def test_missing_helper_measurement_uses_readonly_wrapper(self):
        wrapper=Path(self.tmp.name)/'office_storage_run.sh';wrapper.write_text('# mock')
        with patch.object(self.s,'HERE',Path(self.tmp.name)),patch.object(self.s,'helper_call',side_effect=RuntimeError('blocked')),patch.object(self.s.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout='{}')) as run:
            self.s.measure_windows('test')
            self.assertEqual(run.call_args.args[0],['bash',str(wrapper),'--dry-run'])
            self.assertNotIn('shell',run.call_args.kwargs)

    def test_unknown_floor_does_not_estimate(self):
        self.assertIsNone(self.s.compact_estimate(dict(vhdx_gb=100,used_gb=50,floor_gib=None)))
        notice=self.s.compact_notice(dict(t=1,disk=dict(vhdx_gb=100,used_gb=50,c_free_gb=1,floor_gib=None)))
        self.assertEqual(notice['status'],'ok');self.assertIsNone(notice['reclaim_gib'])
    def test_no_wsl_never_calls_helper(self):
        with patch.dict(os.environ,{'WSL_DISTRO_NAME':''}),patch.object(self.s,'helper_call') as run:
            self.assertIsNone(self.s.disk_snapshot());run.assert_not_called()
    def test_failed_measurement_replaces_stale_suggestion(self):
        Path(self.s.COMPACT_NOTICE).write_text('{"status":"suggested"}')
        with patch.object(self.s,'disk_snapshot',return_value=None):self.s.measure_notice()
        self.assertEqual(json.loads(Path(self.s.COMPACT_NOTICE).read_text())['status'],'unavailable')
    def test_floor_can_only_learn_successful_matching_production_run(self):
        now=time.time()
        d=self.data();d.update(allocated_bytes=100*2**30,logical_bytes=100*2**30)
        pre=dict(request_id='a',distro='test',path=d['path'],trim_ok=True,used_gb=80,recorded_at=now-20)
        result=dict(request_id='a',status='completed',test_mode=False,recovery_required=False,after_gb=90,updated_at=now-10)
        self.s.atomic_json(Path(self.tmp.name)/'pre-compact.json',pre)
        for change in [dict(test_mode=True),dict(status='failed'),dict(request_id='b')]:
            self.s.atomic_json(Path(self.tmp.name)/'result.json',{**result,**change});self.s.learn_compact_floor(d)
            self.assertFalse(Path(self.s.COMPACT_FLOOR).exists())
        self.s.atomic_json(Path(self.tmp.name)/'result.json',result);self.s.learn_compact_floor(d)
        self.assertEqual(json.loads(Path(self.s.COMPACT_FLOOR).read_text())['floor_gib'],10)


class Integration(unittest.TestCase):
    def setUp(self):
        self.o=load('office.py')
    def test_probe_timeout_remains_unknown(self):
        with patch.object(self.o,'IS_MAC',False),patch.dict(os.environ,{'WSL_DISTRO_NAME':'test'}),patch.object(self.o,'native_windows_call',return_value=None),patch.object(self.o.subprocess,'run',side_effect=subprocess.TimeoutExpired([],10)):
            self.assertIsNone(self.o.powershell_available(wait=True))
    def test_background_probe_does_not_wait_for_windows(self):
        entered=threading.Event(); release=threading.Event(); finished=threading.Event()
        def slow(*args,**kwargs):
            entered.set();release.wait(2);finished.set()
            return types.SimpleNamespace(returncode=0,stdout=b'OfficePowerShell:FullLanguage')
        with patch.object(self.o,'IS_MAC',False),patch.dict(os.environ,{'WSL_DISTRO_NAME':'test'}),patch.object(self.o,'native_windows_call',return_value=None),patch.object(self.o.subprocess,'run',side_effect=slow):
            try:
                self.assertIsNone(self.o.powershell_available())
                self.assertTrue(entered.wait(1));self.assertFalse(finished.is_set())
                self.assertIsNone(self.o.powershell_available())
            finally:
                release.set();self.assertTrue(finished.wait(2))

    def test_constrained_language_is_unavailable_but_helper_can_work(self):
        with patch.object(self.o,'IS_MAC',False),patch.dict(os.environ,{'WSL_DISTRO_NAME':'test'}),patch.object(self.o,'native_windows_call',return_value={'ok':True,'api':1}),patch.object(self.o.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout=b'OfficePowerShell:ConstrainedLanguage')):
            self.assertFalse(self.o.powershell_available(wait=True));self.assertTrue(self.o.POWERSHELL_CACHE['helper_available'])
    def test_native_icon_and_focus_skip_powershell(self):
        with patch.object(self.o,'native_windows_call',return_value={'updated':1}),patch.object(self.o.subprocess,'run') as run:
            self.assertTrue(self.o.configure_taskbar_icon());run.assert_not_called()
        with patch.object(self.o,'native_windows_call',return_value={'result':'behind'}),patch.object(self.o.subprocess,'run') as run:
            self.assertEqual(self.o.focus_explorer_path('C:\\data',True),'behind');run.assert_not_called()
    def test_progress_sanitizes_phase_and_heartbeat(self):
        p=self.o.compact_progress({'status':'running','phase':'finalizing','updated_at':time.time()-20,'phase_elapsed_seconds':80,'stalled_seconds':40})
        self.assertEqual((p['phase_elapsed_seconds'],p['stalled_seconds']),(80,40));self.assertGreaterEqual(p['heartbeat_seconds'],19)
        self.assertEqual(self.o.compact_progress({'status':'running','stalled_seconds':float('nan'),'phase_elapsed_seconds':-1}),{})


if __name__=='__main__':unittest.main()
