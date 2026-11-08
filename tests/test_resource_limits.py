"""여러 프로젝트의 공통 한도와 저장·거절·지원 범위를 독립 확인해요."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE=Path(sys.argv.pop(1)).resolve() if len(sys.argv)>1 and sys.argv[1].endswith('.py') else Path(__file__).resolve().parents[1]/'payload/office.py'

class GlobalLimits(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='office-global-limits-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        env={k:v for k,v in os.environ.items() if not k.startswith('OFFICE_')}
        env.update(OFFICE_DIR=str(self.root/'a'),OFFICE_SESSION='s',OFFICE_AS='codex',OFFICE_JOB='1',OFFICE_RESOURCE_LIMITS=str(self.root/'global/resource-limits.json'))
        ep=patch.dict(os.environ,env,clear=True);ep.start();self.addCleanup(ep.stop)
        spec=importlib.util.spec_from_file_location('resource_limits_target',SOURCE)
        self.o=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.o)
        self.o.D.mkdir(parents=True,exist_ok=True)
        self.o.RESOURCE_CACHE.clear()

    def test_all_projects_use_one_quota(self):
        o=self.o
        units=[]
        for project in ('a','b','c'):
            with patch.object(o,'ROOT',self.root/project),patch.object(o,'resource_status',return_value={'supported':True}):
                units.append(next(x for x in o.limited_command(['test-command']) if x.startswith('--slice=')))
        self.assertEqual(units,['--slice=office-work.slice']*3)

    def test_first_legacy_value_is_persisted_and_shared(self):
        o=self.o
        (o.D/'resource-limits.json').write_text('{"cpu_percent":55,"memory_high_gb":4}')
        first=o.resource_limits()
        self.assertEqual(first,{'cpu_percent':55,'memory_high_gb':4})
        self.assertTrue(o.RESOURCE_FILE.exists(),'최초 설정을 공통 파일에 보관해야 해요.')
        other=self.root/'b/.office';other.mkdir(parents=True)
        (other/'resource-limits.json').write_text('{"cpu_percent":90,"memory_high_gb":0}')
        with patch.object(o,'D',other),patch.object(o,'ROOT',other.parent):
            self.assertEqual(o.resource_limits(),first)

    def test_api_saves_globally_and_other_project_reads_it(self):
        o=self.o
        with patch.object(o,'IS_MAC',False),patch.object(o,'physical_memory',return_value=64*1024**3),patch.object(o,'apply_resource_limits') as apply,patch.object(o,'resource_status',return_value={'supported':True}):
            o.Office().handle({'action':'resource_limits','cpu_percent':30,'memory_high_gb':2})
        apply.assert_called_once_with(30,2)
        self.assertEqual(o.read_json_file(o.RESOURCE_FILE,{}),{'cpu_percent':30,'memory_high_gb':2})
        with patch.object(o,'D',self.root/'other/.office'),patch.object(o,'ROOT',self.root/'other'):
            self.assertEqual(o.resource_limits(),{'cpu_percent':30,'memory_high_gb':2})

    def test_invalid_values_do_not_change_budget(self):
        o=self.o
        bad=[(0,0),(101,0),(True,0),(50.5,0),(70,-1),(70,65),(70,float('nan')),(70,float('inf')),(70,True)]
        with patch.object(o,'IS_MAC',False),patch.object(o,'physical_memory',return_value=64*1024**3),patch.object(o,'apply_resource_limits') as apply:
            for cpu,memory in bad:
                with self.subTest(cpu=cpu,memory=memory),self.assertRaises(o.UserError):
                    o.Office().handle({'action':'resource_limits','cpu_percent':cpu,'memory_high_gb':memory})
        apply.assert_not_called()
        self.assertFalse(o.RESOURCE_FILE.exists())

    def test_mac_does_not_execute_resource_commands(self):
        o=self.o
        with patch.object(o,'IS_MAC',True),patch.object(o.subprocess,'run') as run:
            self.assertFalse(o.resource_status(refresh=True)['supported'])
            self.assertEqual(o.limited_command(['test-command']),['test-command'])
            with self.assertRaises(o.UserError):
                o.Office().handle({'action':'resource_limits','cpu_percent':70,'memory_high_gb':0})
        run.assert_not_called()

    def test_commented_markers_do_not_capture_unrelated_service(self):
        o=self.o
        units=self.root/'units';units.mkdir()
        (units/'commented.service').write_text('[Service]\n# Environment=OFFICE_JOB=1\nExecStart=/bin/true\n')
        (units/'real.service').write_text('[Service]\nEnvironment="OFFICE_SESSION=s"\n')
        with patch.object(o,'SYSTEMD_USER_DIR',units):
            self.assertEqual(o.office_services(),['real.service'])

if __name__=='__main__':
    unittest.main()
