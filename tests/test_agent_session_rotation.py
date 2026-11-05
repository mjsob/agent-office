"""작업 세션을 새로 시작해도 대화와 학습을 유지하는지 확인해요."""
import contextlib, importlib.util, io, os, sys, tempfile, unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
SOURCE=Path(sys.argv.pop(1)).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parent.parent/'payload/office.py'
sys.path.insert(0,os.environ.get('OFFICE_TEST_PAYLOAD',str(SOURCE.parent)))
spec=importlib.util.spec_from_file_location('office_rotation_test',SOURCE)
o=importlib.util.module_from_spec(spec);spec.loader.exec_module(o)
class Rotation(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='office-rotation-');self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'OFFICE_SESSION':'same','OFFICE_AS':'codex','OFFICE_JOB':'1'})
        self.env.start();o.set_root(self.root);o.sdir('same').mkdir(parents=True)
        with o.state(True) as s:
            s.update(jobs=[{'id':1,'text':'학습','lead':'codex','status':'working'}],tasks=[{'id':188,'job':1,'title':'학습','owner':'codex','status':'waiting'}])
        self.pointer=o.sdir('same')/'codex.sid';self.pointer.write_text('old-model-thread')
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def worker(self,tokens):
        w=object.__new__(o.Worker);w.sid='same';w.name='codex';w.ctx_job=1;w.ctx_tokens=tokens;w.agent_log=lambda text:None
        return w
    def test_same_job_rotates_at_next_turn(self):
        w=self.worker(o.ROTATE_CTX+1);self.assertTrue(w.rotate_session(1,'continue'));self.assertFalse(self.pointer.exists());self.assertEqual(w.ctx_tokens,0)
    def test_direct_chat_rotates_too(self):
        self.assertTrue(self.worker(o.ROTATE_CTX+1).rotate_session(None,'chat'))
    def test_at_threshold_keeps_session(self):
        self.assertFalse(self.worker(o.ROTATE_CTX).rotate_session(1,'continue'));self.assertEqual(self.pointer.read_text(),'old-model-thread')
    def test_rotation_preserves_job_and_task(self):
        before=o.state_view('same');self.worker(o.ROTATE_CTX+1).rotate_session(1,'continue');self.assertEqual(before,o.state_view('same'))
    def fresh(self):
        with contextlib.redirect_stdout(io.StringIO()):o.cmd_fresh(Namespace(name=None))
    def test_fresh_preserves_chat_and_task(self):
        before=o.state_view('same');self.fresh();after=o.state_view('same')
        self.assertEqual(before['jobs'],after['jobs']);self.assertEqual(before['tasks'],after['tasks'])
        self.assertFalse(self.pointer.exists());self.assertEqual(self.pointer.with_suffix('.sid.previous').read_text(),'old-model-thread')
    def test_repeated_fresh_is_idempotent(self):
        self.fresh();before=o.state_view('same');self.fresh();self.assertEqual(before,o.state_view('same'))
    def test_other_agent_cannot_reset(self):
        with patch.dict(os.environ,{'OFFICE_AS':'claude'}):
            with self.assertRaises(o.UserError):o.cmd_fresh(Namespace(name='codex'))
        self.assertTrue(self.pointer.exists())
    def test_active_claude_cannot_be_reset(self):
        folder=o.sdir('same');(folder/'claude.sid').write_text('old-claude')
        with (folder/'claude.run.lock').open('a') as lease:
            o.fcntl.flock(lease,o.fcntl.LOCK_EX|o.fcntl.LOCK_NB)
            with patch.dict(os.environ,{'OFFICE_AS':'claude'}):
                with self.assertRaises(o.UserError):o.cmd_fresh(Namespace(name='claude'))
        self.assertTrue((folder/'claude.sid').exists())
    def test_current_codex_can_prepare_next_session(self):
        folder=o.sdir('same');o.atomic_json(folder/'codex.run.json',{'token':'test','status':'running'})
        with (folder/'codex.run.lock').open('a') as lease:
            o.fcntl.flock(lease,o.fcntl.LOCK_EX|o.fcntl.LOCK_NB)
            with patch.dict(os.environ,{'OFFICE_RUN_TOKEN':'test'}):self.fresh()
        self.assertFalse(self.pointer.exists())
    def test_wrong_execution_token_is_rejected(self):
        folder=o.sdir('same');o.atomic_json(folder/'codex.run.json',{'token':'correct','status':'running'})
        with (folder/'codex.run.lock').open('a') as lease:
            o.fcntl.flock(lease,o.fcntl.LOCK_EX|o.fcntl.LOCK_NB)
            with patch.dict(os.environ,{'OFFICE_RUN_TOKEN':'wrong'}):
                with self.assertRaises(o.UserError):self.fresh()
        self.assertTrue(self.pointer.exists())
if __name__=='__main__':unittest.main()
