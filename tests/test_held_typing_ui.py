"""백그라운드 학습이 차례 종료 뒤에도 상태 줄에 보이는지 확인해요."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('html', nargs='?', type=Path, default=Path(__file__).resolve().parents[1] / 'payload/office.html')
parser.add_argument('--state', type=Path)
parser.add_argument('--screenshots', type=Path)
args = parser.parse_args()
html = args.html.read_text()
state = json.loads(args.state.read_text()) if args.state else {
    'tasks': [{'id': 188, 'job': 1, 'owner': 'codex', 'status': 'waiting',
               'title': '국면학습 재개 ' + '긴 설명 ' * 80, 'wait_alive': True, 'wait_kind': 'working',
               'progress': {'done': 34595, 'total': 36288, 'state': 'working', 'text': '장기 계좌를 계산해요.'}}],
    'agents': {'codex': {'status': 'idle'}, 'claude': {'status': 'idle'}}, 'jobs': []}
state.update(session='s', root='/repo', sessions=[], features={}, update={})
state['msgs'] = [{'id': i, 'from': 'user', 'kind': 'msg', 'text': f'채팅 {i} ' + '내용 ' * 50} for i in range(1, 20)]
state['tasks'] = [t for t in state['tasks'] if t['id'] == 188]
results = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    for width, mac, theme in ((1400, False, 'light'), (700, False, 'dark'), (390, True, 'light')):
        state['features'] = {'mac': mac}
        page = browser.new_page(viewport={'width': width, 'height': 900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.route('http://office.test/**', lambda r: r.fulfill(body=html, content_type='text/html'))
        page.route('**/api/state*', lambda r: r.fulfill(json=state))
        page.route('**/api', lambda r: r.fulfill(json={}))
        page.goto(f'http://office.test/?theme={theme}')
        page.wait_for_function('lastTasks.some(t=>t.id===188)')
        page.evaluate('clearInterval(refreshTimer)')
        page.wait_for_timeout(100)
        assert page.locator('[data-held-task="188"]').is_visible()
        assert '백그라운드 실행 중' in page.locator('#typing').inner_text()
        assert format(state['tasks'][0]['progress']['done'], ',') in page.locator('#typing').inner_text()
        if args.screenshots:
            args.screenshots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(args.screenshots / f'held-{width}-{theme}.png'))
        page.evaluate('window.baseTask=structuredClone(lastTasks[0]);window.baseJobs=structuredClone(lastJobs)')
        cases = [
            ('waiting', "lastTasks[0].progress.state='waiting'", '기다리는 중'),
            ('resume', "lastTasks[0].progress.resume_at=Date.now()/1000+3600", '기다리는 중'),
            ('dead', "lastTasks[0].wait_alive=false;lastTasks[0].wait_detail='작업이 실패했어요.'", '결과 확인 대기'),
            ('user', "lastTasks[0].wait_kind='user'", '사용자 확인 대기'),
            ('peer', "lastTasks[0].wait_alive=null;lastTasks[0].wait_kind='waiting';delete lastTasks[0].progress", '기다리는 중'),
            ('active', "lastAgentState.codex={status:'working',since:Date.now()/1000,activity:[]}", '백그라운드 실행 중'),
            ('done_job', "lastJobs=[{id:1,status:'done'}]", '백그라운드 실행 중'),
            ('done', "lastTasks[0].status='done'", None),
            ('cancelled', "lastJobs=[{id:1,cancelled:true}]", None),
            ('removed', "lastJobs=[{id:1,removed:true}]", None),
            ('continued', "lastTasks[0].continued_to='other'", None),
            ('ordinary', "lastTasks[0].status='doing';delete lastTasks[0].wait_alive;lastAgentState.codex={status:'working',since:Date.now()/1000,activity:[]}", None),
        ]
        for name, change, expected in cases:
            page.evaluate("lastTasks=[structuredClone(baseTask)];lastJobs=structuredClone(baseJobs);lastAgentState={codex:{status:'idle'},claude:{status:'idle'}}")
            page.evaluate(change + ';updateTyping(lastAgentState)')
            page.wait_for_timeout(40)
            row = page.locator('[data-held-task="188"]')
            if expected:
                assert row.is_visible() and expected in row.inner_text(), (name, page.locator('#typing').inner_text())
            else:
                assert row.count() == 0, name
            m = page.evaluate('''()=>{const s=document.querySelector('#tlsec'),t=document.querySelector('#typing'),f=document.querySelector('footer');return {over:s.scrollWidth-s.clientWidth,body:document.body.scrollWidth-innerWidth,gap:t.hidden?0:f.getBoundingClientRect().top-t.getBoundingClientRect().bottom}}''')
            assert m['over'] <= 1 and m['body'] <= 1 and abs(m['gap']) <= 1, (name, m)
        # 개수 없이 진행 문구만 있으면 같은 문구를 두 번 쓰지 않아요.
        page.evaluate("lastTasks=[structuredClone(baseTask)];lastTasks[0].progress={text:'장기 계좌를 계산해요.',state:'working'};updateTyping({codex:{status:'idle'}})")
        assert page.locator('[data-held-task="188"] + .tp-act').inner_text().count('장기 계좌를 계산해요.') == 1
        # 읽던 위치와 맨 아래 따라가기를 렌더링이 끝난 뒤 확인해요.
        page.evaluate("lastTasks=[structuredClone(baseTask)];lastJobs=structuredClone(baseJobs);updateTyping(lastAgentState);document.querySelector('#tlsec').scrollTop=300")
        before = page.locator('#tlsec').evaluate('e=>e.scrollTop')
        page.evaluate("lastTasks[0].progress.done++;updateTyping(lastAgentState)")
        page.wait_for_timeout(60)
        assert page.locator('#tlsec').evaluate('e=>e.scrollTop') == before
        page.evaluate("document.querySelector('#tlsec').scrollTop=document.querySelector('#tlsec').scrollHeight;updateTyping(lastAgentState)")
        page.wait_for_timeout(60)
        assert abs(page.locator('#tlsec').evaluate('e=>e.scrollHeight-e.scrollTop-e.clientHeight')) <= 1
        assert not errors, errors
        results.append({'width': width, 'mac': mac, 'theme': theme, 'cases': len(cases)+4, 'pass': True})
        page.close()
    browser.close()
print(json.dumps(results, ensure_ascii=False))
