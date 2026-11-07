"""긴 작업 상태 글이 채팅 폭과 높이를 바꾸지 않는지 확인해요.
전체 화면 코드를 Chromium에서 실행해요. 서버 응답은 시험 자료로 바꿔요.
"""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('html', nargs='?', type=Path, default=Path(__file__).resolve().parents[1] / 'payload/office.html')
parser.add_argument('--screenshots', type=Path)
args = parser.parse_args()
html = args.html.read_text()
state = {'session': 's', 'root': '/repo', 'sessions': [], 'agents': {}, 'jobs': [], 'tasks': [],
         'msgs': [{'id': i, 'from': 'user', 'to': 'all', 'kind': 'msg', 'job': None, 'text': f'채팅 {i} ' + '내용 ' * 50} for i in range(1, 20)],
         'features': {}, 'update': {}}
results = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    for width in (1400, 700, 390):
        for theme in ('light', 'dark'):
            page = browser.new_page(viewport={'width': width, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('http://office.test/**', lambda route: route.fulfill(body=html, content_type='text/html'))
            page.route('**/api/state*', lambda route: route.fulfill(json=state))
            page.route('**/api', lambda route: route.fulfill(json={}))
            page.goto(f'http://office.test/?theme={theme}')
            page.wait_for_function('lastMessages.length > 0')
            page.evaluate('clearInterval(refreshTimer)')
            cases = page.evaluate('''()=>{
              const s=document.getElementById('tlsec'),t=document.getElementById('typing'),c=document.querySelector('.conversation');
              const now=Date.now()/1000,ts=new Date().toLocaleTimeString('en-GB',{hour12:false});
              const agent=extra=>({status:'working',since:now-31,activity:[],...extra});
              const show=(extra,count)=>updateTyping(Object.fromEntries(['codex','claude'].slice(0,count).map(n=>[n,agent(extra)])));
              const measure=()=>({width:s.clientWidth,height:s.clientHeight,overflow:s.scrollWidth>s.clientWidth,inside:s.getBoundingClientRect().right<=c.getBoundingClientRect().right+1,gap:document.querySelector('footer').getBoundingClientRect().top-t.getBoundingClientRect().bottom});
              const out=[];
              for(const count of [1,2]){
                show({tool:{since:now,label:'short'}},count);s.scrollTop=400;
                const base=measure();
                for(const [mode,extra] of Object.entries({
                  tool:{tool:{since:now,label:'export OFFICE_JOB=17 office reserve /home/chan/projects/'+'long-path/'.repeat(50)}},
                  activity:{activity:[{kind:'tool',ts,text:'office reserve /home/chan/projects/'+'W'.repeat(300)}]},
                  gen:{gen:{kind:'tool',name:'긴도구이름'.repeat(200),chars:123456}}
                })){
                  const before=s.scrollTop;show(extra,count);const m=measure();
                  const detail=t.querySelector('.tp-act');
                  out.push({count,mode,...m,baseWidth:base.width,baseHeight:base.height,scrollDelta:s.scrollTop-before,detailCount:t.querySelectorAll('.tp-act').length,clipped:detail.scrollWidth>detail.clientWidth,ellipsis:getComputedStyle(detail).textOverflow});
                }
              }
              s.scrollTop=s.scrollHeight;show({tool:{since:now,label:'W'.repeat(500)}},2);
              out.push({mode:'bottom',delta:s.scrollHeight-s.scrollTop-s.clientHeight});
              updateTyping({});out.push({mode:'idle',hidden:t.hidden,fill:Math.abs(s.getBoundingClientRect().bottom-document.querySelector('footer').getBoundingClientRect().top)});
              show({activity:[{kind:'tool',ts,text:'office reserve /home/chan/projects/'+'W'.repeat(300)}]},2);s.scrollTop=400;
              return out;
            }''')
            for case in cases:
                if case['mode'] == 'bottom':
                    assert abs(case['delta']) < 1, case
                elif case['mode'] == 'idle':
                    assert case['hidden'] and case['fill'] < 1, case
                else:
                    assert case['width'] == case['baseWidth'] and case['height'] == case['baseHeight'], case
                    assert not case['overflow'] and case['inside'] and abs(case['gap']) < 1, case
                    assert case['detailCount'] == case['count'] and case['ellipsis'] == 'ellipsis', case
                    if case['mode'] != 'tool':
                        assert case['clipped'], case
                    assert case['scrollDelta'] == 0, case
            assert not errors, errors
            if args.screenshots:
                args.screenshots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(args.screenshots / f'typing-long-{width}-{theme}.png'))
            results.append({'width': width, 'theme': theme, 'cases': len(cases), 'passed': True})
            page.close()
    browser.close()
print(json.dumps(results, indent=2))
