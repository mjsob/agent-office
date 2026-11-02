"""충돌 선택 화면을 실제 브라우저에서 확인해요.
실행: python3 tests/test_update_review_ui.py [office.html 경로] [결과 폴더]
설치와 에이전트 실행은 API 가짜 응답으로 바꿔요.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import time
from playwright.sync_api import sync_playwright

source=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parents[1]/'payload/office.html'
output=Path(sys.argv[2]) if len(sys.argv)>2 else None
if output: output.mkdir(parents=True,exist_ok=True)
html=source.read_text()
token='a'*32
base={'current':'0.9.0','latest':'0.10.0','available':True,'installable':True,'state':'conflict','checked_at':time.time(),
      'review':{'id':token,'state':'conflict','version':'0.10.0','conflicts':['office.py'],'labels':['앱 동작'],
                'blocked':['겹쳐요'],'resolvable':True,'can_merge':True,'merge_available':True,'can_verify':False,
                'can_apply_merged':False,'can_restore':False}}
results=[]
with sync_playwright() as p:
 browser=p.chromium.launch()
 try:
  for width,theme in [(320,'light'),(320,'dark'),(390,'light'),(390,'dark'),(1500,'light'),(1500,'dark')]:
   context=browser.new_context(viewport={'width':width,'height':920})
   snapshot={'unchanged':True,'rev':1,'update':copy.deepcopy(base),'storage_notice_preferences':{'hidden':True,'mode':'never'}}
   writes,errors=[],[]
   accepting={'value':False}
   context.route('http://office.test/**',lambda r:r.fulfill(body=html,content_type='text/html'))
   context.route('**/api/state*',lambda r:r.fulfill(json=snapshot))
   def post(route):
    payload=route.request.post_data_json; writes.append(payload)
    route.fulfill(json={'ok':'선택을 받았어요.'})
   context.route('**/api',post)
   font=Path('/mnt/c/Windows/Fonts/malgun.ttf')
   if font.is_file(): context.route('**/test-font.ttf',lambda r:r.fulfill(body=font.read_bytes(),content_type='font/ttf'))
   page=context.new_page()
   page.on('pageerror',lambda error:errors.append(str(error)))
   page.on('dialog',lambda dialog:dialog.accept() if accepting['value'] else dialog.dismiss())
   page.goto('http://office.test/?theme='+theme)
   page.wait_for_function('updateState.review?.id === "'+token+'"')
   if font.is_file():
    page.add_style_tag(content="@font-face{font-family:TestKorean;src:url('/test-font.ttf')}body,button{font-family:TestKorean,sans-serif}")
    page.evaluate('document.fonts.ready')
   def show(update):
    snapshot['update']=copy.deepcopy(update)
    page.evaluate('u=>{updatePanelOpen=true;renderUpdate(u)}',snapshot['update'])
   def record(name):
    assert not errors,(name,errors)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),(name,width)
    box=page.locator('#updateNotice').bounding_box()
    assert box and box['x']>=0 and box['x']+box['width']<=width+1,(name,box,width)
    results.append({'case':name,'width':width,'theme':theme,'passed':True})
   show(base)
   assert page.locator('#updateButton').is_hidden()
   for ident in ('updateKeep','updateMerge','updateUpstream'): assert page.locator('#'+ident).is_visible()
   page.locator('#updateUpstream').click()
   assert not writes,'취소했는데 요청이 나갔어요'
   record('upstream-cancel')
   accepting['value']=True
   page.locator('#updateUpstream').click()
   page.wait_for_function('!updateSubmitting')
   assert writes[-1]['action']=='update_resolve' and writes[-1]['confirmed'] is True and writes[-1]['choice']=='upstream'
   record('upstream-confirm')
   writes.clear(); show(base)
   page.locator('#updateKeep').click(); page.wait_for_function('!updateSubmitting')
   assert writes[-1]['choice']=='keep'
   record('keep')
   writes.clear(); show(base)
   page.locator('#updateMerge').click(); page.wait_for_function('!updateSubmitting')
   assert writes[-1]['action']=='update_merge'
   record('merge')
   logged_out=copy.deepcopy(base); logged_out['review']['merge_available']=False
   show(logged_out)
   assert page.locator('#updateMerge').is_disabled() and '로그인' in page.locator('#updateReviewReason').inner_text()
   record('logged-out')
   merging=copy.deepcopy(base); merging.update(state='merging'); merging['review'].update(state='merging',can_merge=False,can_verify=True)
   writes.clear(); show(merging)
   assert page.locator('#updateUpstream').is_hidden() and page.locator('#updateKeep').is_hidden()
   page.locator('#updateVerify').click(); page.wait_for_function('!updateSubmitting')
   assert writes[-1]['action']=='update_verify'
   record('verify')
   ready=copy.deepcopy(merging); ready.update(state='ready'); ready['review'].update(state='ready',can_apply_merged=True)
   writes.clear(); show(ready)
   assert page.locator('#updateVerify').is_hidden()
   page.locator('#updateApplyMerged').click(); page.wait_for_function('!updateSubmitting')
   assert writes[-1]['choice']=='merged'
   record('apply-merged')
   blocked=copy.deepcopy(base); blocked.update(state='blocked'); blocked['review'].update(state='blocked',resolvable=False,can_merge=False,blocked=['예전 원본을 확인할 수 없어요.'])
   show(blocked)
   assert page.locator('#updateMerge').is_hidden() and page.locator('#updateUpstream').is_hidden() and page.locator('#updateKeep').is_visible()
   record('blocked')
   busy=copy.deepcopy(base); busy.update(state='running')
   show(busy)
   assert page.locator('#updateKeep').is_disabled() and page.locator('#updateUpstream').is_disabled()
   record('busy')
   applied=copy.deepcopy(base); applied.update(state='applied',available=False); applied['review'].update(state='applied',can_restore=True,can_merge=False)
   writes.clear(); show(applied)
   page.locator('#updateRestore').click(); page.wait_for_function('!updateSubmitting')
   assert writes[-1]['action']=='update_restore' and writes[-1]['confirmed'] is True
   record('restore')
   injected=copy.deepcopy(base); injected['review']['labels']=['<img src=x onerror="window.injection=true">']
   show(injected)
   assert page.locator('#updateReviewFiles img').count()==0 and page.evaluate('window.injection !== true')
   record('escaped-label')
   show(base)
   if output:
    page.screenshot(path=str(output/f'conflict-{width}-{theme}.png'))
   assert not errors,errors
   context.close()
 finally: browser.close()
report={'html_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'results':results,'passed':len(results),'failed':0}
if output: (output/'browser-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':len(results),'failed':0},ensure_ascii=False))
