"""새 대화 인계와 이전 진행 알림의 연결을 확인해요."""
import threading
import contextlib, copy, importlib.util, io, json, os, sys, tempfile, unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
SOURCE=Path(sys.argv.pop(1)).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parent.parent/'payload/office.py'
sys.path.insert(0,os.environ.get('OFFICE_TEST_PAYLOAD',str(SOURCE.parent)))
spec=importlib.util.spec_from_file_location('office_continue_test',SOURCE)
o=importlib.util.module_from_spec(spec)
spec.loader.exec_module(o)

class ContinueSession(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='office-continue-')
        self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'OFFICE_DIR':str(self.root),'OFFICE_SESSION':'source','OFFICE_AS':'codex','OFFICE_JOB':'49'})
        self.env.start()
        o.set_root(self.root)
        o.CURRENT_HINT.update(sid=None,at=0)
        self.brief=self.root/'brief.txt';self.brief.write_text('완료 기록을 확인하고 학습을 이어가세요.')
        self.running=patch.object(o,'running',return_value=None);self.running.start()
        self.args=Namespace(job=49,brief_file=str(self.brief),title='국면학습 이어가기')
        with o.meta(True) as m:
            m.update(current='source',sessions=[{'id':'source','title':'원래 대화','deleted':False}])
        with o.state(True,'source') as s:
            s.update(seq=315,jobs=[{'id':49,'lead':'codex','text':'장기 학습','status':'working'},
                                   {'id':63,'lead':'codex','text':'다른 요청','status':'done'}],
                     tasks=[{'id':188,'job':49,'title':'학습 감시','owner':'codex','status':'waiting','wait_log':'/tmp/progress.log','wait_deadline':99999999999},
                            {'id':189,'job':49,'title':'끝난 검수','owner':'claude','status':'done'}],
                     msgs=[{'id':i+1,'ts':'00:00:00','from':'system','to':'all','kind':'event','text':'old '+str(i),'job':49} for i in range(1381)])
    def tearDown(self):
        self.running.stop();self.env.stop();self.tmp.cleanup()
    def go(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):o.cmd_continue(self.args)
        return json.loads(out.getvalue())['session']
    def test_short_transfer_preserves_source_and_task_id(self):
        sid=self.go(); old=o.state_view('source'); new=o.state_view(sid)
        self.assertEqual(len(new['msgs']),1);self.assertEqual(len(new['tasks']),1)
        self.assertEqual(new['tasks'][0]['id'],188);self.assertEqual(new['tasks'][0]['job'],1)
        self.assertEqual(new['seq'],315);self.assertEqual(new['jobs'][0]['id'],1)
        self.assertEqual(len(old['msgs']),1382);self.assertEqual(old['tasks'][0]['continued_to'],sid)
        self.assertNotIn('execution',old['tasks'][0]);self.assertEqual(o.meta_view()['current'],sid)
        self.assertEqual(old['jobs'][1]['id'],63)
    def test_retry_is_idempotent(self):
        a=self.go();b=self.go();self.assertEqual(a,b);self.assertEqual(len(o.meta_view()['sessions']),2)
    def test_progress_from_old_service_reaches_new_task(self):
        sid=self.go()
        a=Namespace(words=['188','계산 중이에요.'],done=14382,total=36288,resume_at=None,state='working',notify=False)
        with contextlib.redirect_stdout(io.StringIO()):o.cmd_progress(a)
        self.assertEqual(o.state_view(sid)['tasks'][0]['progress']['done'],14382)
        self.assertNotIn('progress',o.state_view('source')['tasks'][0])
    def test_busy_task_is_rejected(self):
        with o.state(True,'source') as s:s['tasks'][0]['status']='doing'
        with self.assertRaises(o.UserError):self.go()
        self.assertEqual(len(o.meta_view()['sessions']),1)
    def test_wrong_leader_is_rejected(self):
        with patch.dict(os.environ,{'OFFICE_AS':'claude'}):
            with self.assertRaises(o.UserError):self.go()
        self.assertEqual(o.state_view('source')['jobs'][0]['status'],'working')
    def test_brief_over_limit_is_rejected(self):
        self.brief.write_text('가'*6001)
        with self.assertRaises(o.UserError):self.go()
    def test_metadata_failure_recovers_without_copying_again(self):
        original=o.atomic_json
        def failing(path,*a,**kw):
            if path==o.D/'meta.json':raise OSError('simulated disk error')
            return original(path,*a,**kw)
        with patch.object(o,'atomic_json',failing):
            with self.assertRaises(OSError):self.go()
        target=o.state_view('source')['jobs'][0]['continued_to']
        self.assertEqual(len(o.meta_view()['sessions']),1)
        self.assertEqual(self.go(),target);self.assertEqual(len(o.meta_view()['sessions']),2)
    def test_destination_failure_leaves_source_open(self):
        original=o.atomic_json
        def failing(path,*a,**kw):
            if path.parent.name!='source' and path.name=='state.json':raise OSError('simulated disk error')
            return original(path,*a,**kw)
        with patch.object(o,'atomic_json',failing):
            with self.assertRaises(OSError):self.go()
        self.assertEqual(o.state_view('source')['jobs'][0]['status'],'working')
        self.assertEqual(len(o.meta_view()['sessions']),1)
    def test_progress_destination_is_rechecked_under_lock(self):
        sid=self.go()
        a=Namespace(words=['188','계산 중이에요.'],done=14383,total=36288,resume_at=None,state='working',notify=False)
        with patch.object(o,'continued_progress_sid',return_value='source'):
            with contextlib.redirect_stdout(io.StringIO()):o.cmd_progress(a)
        self.assertEqual(o.state_view(sid)['tasks'][0]['progress']['done'],14383)
    def test_live_server_is_asked_to_activate_target(self):
        class Opener:
            def open(inner,url,timeout):
                self.assertIn('/api/state?session=',url)
                sid=url.split('session=')[1]
                return io.StringIO(json.dumps({'session':sid}))
        out=io.StringIO()
        with patch.object(o,'running',return_value={'port':12345}), patch.object(o.urlrequest,'build_opener',return_value=Opener()):
            with contextlib.redirect_stdout(out):o.cmd_continue(self.args)
        self.assertTrue(json.loads(out.getvalue())['team_activated'])
    def test_new_worker_sees_handoff_as_unread(self):
        sid=self.go()
        worker=o.Worker('codex',sid,threading.Event())
        self.assertEqual(worker.read_upto,0)
        self.assertEqual(worker.pending_events,[1])
        self.assertIsNotNone(worker.next_turn(o.state_view(sid)))
    def test_progress_cycle_is_rejected(self):
        with o.state(True,'source') as s:s['tasks'][0]['continued_to']='source'
        with self.assertRaises(o.UserError):o.continued_progress_sid('source',188)
    def test_repeated_rollover_progress_follows_chain(self):
        a=self.go()
        with patch.dict(os.environ,{'OFFICE_SESSION':a,'OFFICE_JOB':'1'}):
            self.args.job=1;b=self.go()
        self.assertEqual(o.continued_progress_sid('source',188),b)

if __name__=='__main__':unittest.main()
