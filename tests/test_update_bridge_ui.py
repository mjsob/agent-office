"""구버전 이전 화면의 실제 체크·버튼 동작을 브라우저에서 확인해요.
실행: python3 tests/test_update_bridge_ui.py [저장소 경로] [결과 폴더]
설치 실행은 가짜 API 응답으로 바꿔요.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright
root=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parents[1]
output=Path(sys.argv[2]) if len(sys.argv)>2 else None
if output:output.mkdir(parents=True,exist_ok=True)
spec=importlib.util.spec_from_file_location('_bridge_ui',root/'payload/office_update_bridge.py')
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
base={'id':'a'*32,'labels':['앱 동작','화면'],'version':'0.10.0','working':False,'message':'','error':False,
      'can_merge':True,'merge_note':'에이전트가 사본에서 합쳐요.','can_verify':False,'can_apply_merged':False,'can_upstream':True,'finished':None}
results=[]
with sync_playwright() as p:
 browser=p.chromium.launch()
 try:
  for width,theme in [(320,'light'),(390,'dark'),(1000,'light')]:
   context=browser.new_context(viewport={'width':width,'height':920},color_scheme=theme)
   state=copy.deepcopy(base);writes=[];errors=[];requests=[]
   context.route('http://office.test/**',lambda r:r.fulfill(body=bridge.PAGE,content_type='text/html'))
   context.route('**/state',lambda r:r.fulfill(json=state))
   def post(route):
    data=route.request.post_data_json;writes.append(data);route.fulfill(json={'ok':True})
   context.route('**/act',post)
   font=Path('/mnt/c/Windows/Fonts/malgun.ttf')
   if font.is_file():context.route('**/test-font.ttf',lambda r:r.fulfill(body=font.read_bytes(),content_type='font/ttf'))
   page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:requests.append(r.url))
   page.goto('http://office.test/'+'x'*32)
   page.locator('#ok').wait_for()
   if font.is_file():
    page.add_style_tag(content="@font-face{font-family:TestKorean;src:url('/test-font.ttf')}body,button,h1{font-family:TestKorean,sans-serif}")
    page.evaluate('document.fonts.ready')
   def check(name):
    assert not errors,(name,errors)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(name,width)
    results.append({'case':name,'width':width,'theme':theme,'passed':True})
   page.locator('#ok').check()
   page.get_by_role('button',name='새 버전 사용',exact=True).click()
   page.wait_for_function('!busy')
   choice=next(row for row in reversed(writes) if row['action']=='upstream')
   assert choice['confirmed'] is True,'체크했는데 확인값이 사라졌어요'
   check('upstream-confirmation')
   page.locator('#ok').check();page.evaluate('refresh()')
   assert page.locator('#ok').is_checked(),'상태를 새로 읽으면 확인 체크가 사라져요'
   check('confirmation-survives-refresh')
   writes.clear();page.get_by_role('button',name='내 수정 유지',exact=True).click();page.wait_for_function('!busy')
   assert any(row['action']=='keep' for row in writes);check('keep')
   writes.clear();page.get_by_role('button',name='사본에서 합치기',exact=True).click();page.wait_for_function('!busy')
   assert any(row['action']=='merge' for row in writes);check('merge')
   state.update(can_merge=False,can_verify=True,can_apply_merged=False)
   page.evaluate('refresh()');page.get_by_role('button',name='합친 결과 확인',exact=True).wait_for()
   writes.clear();page.get_by_role('button',name='합친 결과 확인',exact=True).click();page.wait_for_function('!busy')
   assert any(row['action']=='verify' for row in writes);check('verify')
   state.update(can_apply_merged=True)
   page.evaluate('refresh()');page.get_by_role('button',name='합친 사본 적용',exact=True).wait_for()
   writes.clear();page.get_by_role('button',name='합친 사본 적용',exact=True).click();page.wait_for_function('!busy')
   assert any(row['action']=='merged' for row in writes);check('apply-merged')
   state.update(working=True)
   page.evaluate('refresh()');page.wait_for_function('view.working===true')
   assert page.get_by_role('button',name='새 버전 사용',exact=True).is_disabled();check('busy')
   state.update(working=False,labels=['<img src=x onerror="window.bad=true">'])
   page.evaluate('refresh()');page.wait_for_function('view.working===false')
   assert page.locator('#root img').count()==0 and page.evaluate('window.bad!==true');check('escaped-label')
   assert not any('fonts.googleapis.com' in url or 'fonts.gstatic.com' in url for url in requests),'선택 화면이 외부 글꼴에 연결해요'
   state.update(labels=base['labels']);page.evaluate('refresh()')
   if output:page.screenshot(path=str(output/f'bridge-{width}-{theme}.png'))
   context.close()
 finally:browser.close()
report={'source_sha256':hashlib.sha256((root/'payload/office_update_bridge.py').read_bytes()).hexdigest(),'passed':len(results),'failed':0,'results':results}
if output:(output/'bridge-browser-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':len(results),'failed':0}))
