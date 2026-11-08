"""하단 상태 줄에는 실행 중인 AI 에이전트만 보이는지 확인해요."""
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
    'tasks': [{'id':188,'owner':'codex','job':1,'status':'waiting','title':'국면학습 재개',
               'wait_alive':True,'wait_kind':'working','progress':{'done':34595,'total':36288,'state':'working'}}],
    'agents': {'codex':{'status':'idle'},'claude':{'status':'idle'}},'jobs':[]}
state.update(session='s',root='/repo',sessions=[],features={},update={})
state['msgs'] = [{'id':i,'from':'user','kind':'msg','text':f'채팅 {i} '+'내용 '*50} for i in range(1,20)]
results=[]
with sync_playwright() as p:
    browser=p.chromium.launch()
    for width in (1400,700,390):
        for theme in ('light','dark'):
            page=browser.new_page(viewport={'width':width,'height':900})
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.route('http://office.test/**',lambda r:r.fulfill(body=html,content_type='text/html'))
            page.route('**/api/state*',lambda r:r.fulfill(json=state))
            page.route('**/api',lambda r:r.fulfill(json={}))
            page.goto('http://office.test/?theme='+theme)
            page.wait_for_function('lastMessages.length>0')
            page.evaluate('clearInterval(refreshTimer)')
            page.evaluate('''()=>{
              window.filterScenario=(mode)=>{
                const now=Date.now()/1000;
                const work={status:'working',since:now-30,activity:[],tool:{since:now,label:'명령 '.repeat(300)}};
                const idle={status:'idle',activity:[]};
                const modes={
                  held:{codex:idle,claude:idle},
                  waiting:{codex:{status:'waiting'},claude:idle},
                  paused:{codex:{...idle,paused:true}},
                  error:{codex:{...idle,error:'오류'}},
                  one:{codex:work,claude:idle},
                  both:{codex:work,claude:work},
                  mixed:{codex:{...idle,paused:true},claude:work},
                  stopping:{codex:{...work,paused:true}},
                  empty:{codex:{...work,tool:null}},
                  done:{codex:idle,claude:idle}
                };
                updateTyping(modes[mode]);
              };
            }''')
            total=0
            for location in ('bottom','reading'):
                page.evaluate('filterScenario("held")')
                page.evaluate("const s=document.querySelector('#tlsec');s.scrollTop="+('s.scrollHeight' if location=='bottom' else '300'))
                top=page.locator('#tlsec').evaluate('e=>e.scrollTop')
                for mode,count in [('held',0),('waiting',0),('paused',0),('error',0),('one',1),('both',2),('mixed',1),('stopping',1),('empty',1),('done',0)]:
                    page.evaluate('filterScenario',mode)
                    page.evaluate('async()=>{await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))}')
                    t=page.locator('#typing')
                    assert t.locator('.tp').count()==count,(width,theme,mode,t.inner_text())
                    assert t.is_visible()==bool(count),(width,theme,mode)
                    assert t.locator('[data-held-task]').count()==0
                    if mode=='mixed':
                        assert 'Claude' in t.inner_text() and 'Codex' not in t.inner_text()
                    if mode=='empty':
                        assert t.locator('.tp-act').count()==0
                    m=page.evaluate('''()=>{const s=document.querySelector('#tlsec'),t=document.querySelector('#typing'),f=document.querySelector('footer');return {top:s.scrollTop,gap:s.scrollHeight-s.scrollTop-s.clientHeight,over:s.scrollWidth-s.clientWidth,footerGap:f.getBoundingClientRect().top-(t.hidden?s:t).getBoundingClientRect().bottom}}''')
                    assert m['over']<=1 and abs(m['footerGap'])<=1,(mode,m)
                    assert abs(m['gap'])<=1 if location=='bottom' else abs(m['top']-top)<=1,(mode,location,m)
                    total+=1
            assert not errors,errors
            if args.screenshots:
                args.screenshots.mkdir(parents=True,exist_ok=True)
                page.evaluate('filterScenario("one")')
                page.screenshot(path=str(args.screenshots/f'working-only-{width}-{theme}.png'))
            results.append(dict(width=width,theme=theme,cases=total,passed=True))
            page.close()
    browser.close()
print(json.dumps(results,ensure_ascii=False))
