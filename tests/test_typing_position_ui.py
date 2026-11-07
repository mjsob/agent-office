"""상세 문구가 바뀌어도 위 채팅의 화면 위치와 높이가 유지되는지 확인해요."""
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
         'msgs': [{'id': i, 'from': 'user', 'text': f'채팅 {i} ' + '내용 ' * 60} for i in range(1, 30)],
         'features': {}, 'update': {}}
results = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    for width in (1400, 700, 390):
        for theme in ('light', 'dark'):
            page = browser.new_page(viewport={'width': width, 'height': 900})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.route('http://office.test/**', lambda r: r.fulfill(body=html, content_type='text/html'))
            page.route('**/api/state*', lambda r: r.fulfill(json=state))
            page.route('**/api', lambda r: r.fulfill(json={}))
            page.goto(f'http://office.test/?theme={theme}')
            page.wait_for_function('lastMessages.length>0')
            page.evaluate('clearInterval(refreshTimer)')
            page.evaluate('''()=>{
              window.setAgents=(count,mode)=>{
                const now=Date.now()/1000;
                const agent=()=>({status:'working',since:now-100,doing:{kind:'work',job:177,step:'check'},activity:[]});
                const extra={
                  empty:{}, short:{tool:{since:now,label:'짧은 명령'}},
                  long:{tool:{since:now,label:'명령 '.repeat(300)}},
                  path:{tool:{since:now,label:'/home/chan/projects/'+'longpath/'.repeat(100)}},
                  gen:{gen:{kind:'tool',name:'긴 도구 이름 '.repeat(100),chars:123456}},
                  activity:{activity:[{kind:'text',ts:new Date().toLocaleTimeString('en-GB',{hour12:false}),text:'첫 문장 '.repeat(100)+'\\n두 번째 문장'}]},
                  emptyAgain:{}
                }[mode];
                updateTyping(Object.fromEntries(['codex','claude'].slice(0,count).map(n=>[n,{...agent(),...extra}])));
              };
              window.measureTyping=()=>{
                const s=document.querySelector('#tlsec'),t=document.querySelector('#typing');
                const ms=[...document.querySelectorAll('#timeline .msg')];
                const anchor=ms.find(e=>e.getBoundingClientRect().bottom>s.getBoundingClientRect().top+10);
                const last=ms.at(-1).getBoundingClientRect();
                return {typingHeight:t.getBoundingClientRect().height,chatHeight:s.getBoundingClientRect().height,
                  lastTop:last.top,lastBottom:last.bottom,anchorTop:anchor.getBoundingClientRect().top,top:s.scrollTop,
                  gap:s.scrollHeight-s.scrollTop-s.clientHeight,overflow:s.scrollWidth-s.clientWidth,
                  footerGap:document.querySelector('footer').getBoundingClientRect().top-t.getBoundingClientRect().bottom};
              };
            }''')
            # 렌더링이 끝난 뒤 화면 좌표를 읽어요.
            def settled_measure():
                return page.evaluate('async()=>{await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return measureTyping()}')
            cases = 0
            for count in (1, 2):
                for location in ('bottom', 'reading'):
                    page.evaluate('count=>setAgents(count,"empty")', count)
                    page.evaluate("const s=document.querySelector('#tlsec');s.scrollTop=" + ('s.scrollHeight' if location=='bottom' else '300'))
                    base = settled_measure()
                    for mode in ('short', 'long', 'path', 'gen', 'activity', 'emptyAgain'):
                        page.evaluate('a=>setAgents(a.count,a.mode)', {'count': count, 'mode': mode})
                        current = settled_measure()
                        label = {'width': width, 'theme': theme, 'count': count, 'location': location, 'mode': mode}
                        for key in ('typingHeight', 'chatHeight', 'lastTop', 'lastBottom'):
                            assert abs(current[key]-base[key]) < 1, (label, key, base, current)
                        if location == 'reading':
                            assert current['top'] == base['top'] and abs(current['anchorTop']-base['anchorTop']) < 1, (label, base, current)
                        else:
                            assert abs(current['gap']) <= 1, (label, current)
                        assert current['overflow'] <= 1 and abs(current['footerGap']) < 1, (label, current)
                        cases += 1
            assert not errors, errors
            if args.screenshots:
                args.screenshots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(args.screenshots / f'typing-position-{width}-{theme}.png'))
            results.append({'width': width, 'theme': theme, 'cases': cases, 'pass': True})
            page.close()
    browser.close()
print(json.dumps(results, ensure_ascii=False))
